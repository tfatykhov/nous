"""F105 Task 1: the read-only spreading activation replay (scripts/diag/sa_replay.py).

The pure tests pin the metrics, the expansions parser and the base config the
replay builds every arm on. The postgres_only tests pin that a replayed arm is
exactly a direct ``spreading_activation_search`` call, and that seeds and
degrees are read for the right agent and edges only.
"""

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from spreading_v2_helpers import insert_edge
from sqlalchemy import text

from nous.api.retrieval_pipeline import _score_memory_neighbor
from nous.brain.brain import Brain
from nous.brain.schemas import NeighborResult, ReasonInput, RecordInput
from nous.brain.spreading_activation import spreading_activation_search
from nous.config import Settings
from scripts.diag import sa_replay
from scripts.diag.sa_replay import (
    BASE_CONFIG_KEYS,
    ArmRun,
    EdgeHop,
    ReplaySeed,
    base_config_sources,
    build_settings,
    check_dsn_host,
    edge_kind,
    embed_queries,
    hub_share,
    jaccard_at_k,
    last_hop_edges,
    load_base_config,
    load_candidate_seeds,
    load_degrees,
    load_replay_seeds,
    parse_arm,
    parse_candidate_seeds,
    parse_replay_seeds,
    percentile,
    replay,
    run_arm,
)

A, B, C, D = (UUID(f"0000000{i}-0000-0000-0000-000000000000") for i in range(1, 5))
SA = "stage4_spreading_activation"

# The real A2.env records keys prod leaves unset as "# unset: KEY" comment
# lines; the parity keys appear in it that way. Fake values, same shape.
_A2_LIKE = """\
# A2 arm (fake values)
NOUS_SPREADING_ACTIVATION_ENABLED=true
NOUS_GRAPH_NEIGHBOR_SEED_SCORE_ENABLED=true
# unset: NOUS_SPREADING_ACTIVATION_DECAY
# unset: NOUS_SPREADING_SCORE_DEPTH1_PARITY
# unset: NOUS_GRAPH_INFERRED_EDGE_PENALTY
"""


def _env_file(tmp_path: Path, body: str, name: str = "A2.env") -> Path:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


def _exp(seed_id, seed_type="fact", seed_score=0.8, stage=SA, neighbor=None):
    return {
        "hop": 1, "stage": stage, "seed_id": str(seed_id), "seed_type": seed_type,
        "seed_score": seed_score, "neighbor_id": str(neighbor or uuid4()),
        "neighbor_type": "fact", "edge_relation": "spreading_activation",
        "edge_weight": 0.3, "path_strength": 0.3, "won_best_path": True,
        "extraction_method": None,
    }


def _log_row(expansions, rid="r1"):
    return {"id": rid, "agent_id": "agent-x", "query": "q", "expansions": expansions}


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def test_jaccard_at_k():
    assert jaccard_at_k([A, B, C], [B, C, D], 3) == 0.5
    # Only the top k of each list count.
    assert jaccard_at_k([A, B, C], [A, B, D], 2) == 1.0
    # Two empty windows are the same window.
    assert jaccard_at_k([], [], 20) == 1.0
    assert jaccard_at_k([A], [], 20) == 0.0


def test_percentile_p95():
    xs = [float(x) for x in (7, 1, 20, 3, 15, 9, 12, 2, 18, 5, 11, 4, 16, 8, 19, 6, 14, 10, 17, 13)]
    # Linear interpolation between closest ranks: rank 0.95 * 19 = 18.05.
    assert percentile(xs, 95) == pytest.approx(19.05)
    assert percentile(xs, 50) == pytest.approx(10.5)
    assert percentile(xs, 100) == 20.0
    with pytest.raises(ValueError):
        percentile([], 95)


def test_hub_share():
    hub_seed, other_seed, hub_parent, small_parent = (uuid4() for _ in range(4))
    r1, r2, r3, r4 = (uuid4() for _ in range(4))
    rows = [
        (r1, "fact", 0.4, 1),  # leaves its degree-60 seed
        (r2, "fact", 0.2, 2),  # leaves a degree-60 via node
        (r3, "fact", 0.3, 1),  # leaves a degree-5 seed
        (r4, "fact", 0.15, 2),  # leaves a degree-3 via node
    ]
    parents = {r1: hub_seed, r2: hub_parent, r3: other_seed, r4: small_parent}
    degree = {hub_seed: 60, hub_parent: 60, other_seed: 5, small_parent: 3}
    seeds = {hub_seed, other_seed}
    assert hub_share(rows, parents, seeds, degree, 50) == 0.5
    # A seed's own (depth-0) row leaves nothing and is not an SA row.
    assert hub_share(rows + [(hub_seed, "fact", 0.9, 0)], parents, seeds, degree, 50) == 0.5
    with pytest.raises(ValueError):
        hub_share([], {}, seeds, degree, 50)


def test_score_retrieval_judges_depth2_rows_by_via_node():
    """A depth-2 row leaves its via node, not its seed. Here the hub is reached
    at hop 1 and left at hop 2, which the seed's degree (1) cannot show."""
    seed, hub, beyond = uuid4(), uuid4(), uuid4()
    rs = ReplaySeed(retrieval_id="r", agent_id="agent-x", query="q", seeds=[(seed, "fact", 0.8)])
    run = ArmRun(
        rows=[(hub, "fact", 0.4, 1), (beyond, "fact", 0.2, 2)], seconds=0.001,
        seed_of={hub: seed, beyond: seed}, parent_of={hub: seed, beyond: hub},
    )
    out = sa_replay._score_retrieval(
        rs, {"baseline": run}, {"baseline": Settings(_env_file=None)}, {seed: 1, hub: 60})
    assert out["arms"]["baseline"]["hub_share"] == 0.5
    summary = sa_replay._summarise([out], ["baseline"])["baseline"]
    assert summary[f"hub_share_{sa_replay.HUB_SHARE_METHOD}"] == 0.5
    assert sa_replay.HUB_SHARE_METHOD == "via_node"


# ---------------------------------------------------------------------------
# Seeds from retrieval_log.expansions
# ---------------------------------------------------------------------------


def test_load_replay_seeds_skips_multi_seed_type():
    """Only post-#807 SA rows carry the real winning seed. A "multi" placeholder
    or a null score is the pre-#807 shape, and a Path A (stage2b) expansion is
    not a spreading seed at all."""
    rows = [
        _log_row([_exp(A, seed_type="multi", seed_score=None)], "pre807"),
        _log_row([_exp(A, seed_score=None)], "null-score"),
        _log_row([_exp(A, stage="stage2b_heart_graph_memory")], "path-a"),
        _log_row([_exp(A, seed_type="multi"), _exp(B, seed_score=0.7)], "mixed"),
    ]
    out = parse_replay_seeds(rows)
    assert [rs.retrieval_id for rs in out] == ["mixed"]
    assert out[0].seeds == [(B, "fact", 0.7)]
    assert out[0].agent_id == "agent-x"
    assert out[0].query == "q"


def test_load_replay_seeds_dedupes_within_retrieval():
    one = parse_replay_seeds([_log_row([_exp(A, seed_score=0.9) for _ in range(3)])])
    assert one[0].seeds == [(A, "fact", 0.9)]
    two = parse_replay_seeds([_log_row(
        [_exp(B, seed_score=0.6), _exp(A, seed_score=0.9), _exp(B, seed_score=0.6)])])
    assert two[0].seeds == [(B, "fact", 0.6), (A, "fact", 0.9)], "first-seen order"
    # expansions as delivered by a driver that does not decode JSONB.
    raw = parse_replay_seeds([_log_row(json.dumps([_exp(A, seed_score=0.9)]))])
    assert raw[0].seeds == [(A, "fact", 0.9)]


# ---------------------------------------------------------------------------
# Seeds from retrieval_log.candidates (--seeds-from candidates)
# ---------------------------------------------------------------------------


def _cand(cid, leg, ctype="fact", score=0.8, stage="final", restored_from=None):
    return {"id": str(cid), "type": ctype, "entry_leg": leg, "entry_score": score,
            "entry_rank": None, "final_rank": None, "disposition": "rendered",
            "disposition_stage": stage, "mutations": [], "restored_from": restored_from}


def test_parse_candidate_seeds_mirrors_stage4():
    """Seeds are the first max_expand brain decisions (score or 0.5) and the first
    three scored heart_primary facts, in recorded order; the heart leg's own cut
    is not a seed, also when another leg brought it back. The exclusion is every
    pre-Stage-4 candidate less the heart and chunk cuts, plus the Path A
    neighbours; Stage 4's own rows and later legs are not excluded."""
    d1, d2, cut_fact, f1, f2, f3, f4, unscored, episode = (uuid4() for _ in range(9))
    chunk, chunk_cut, path_a, path_2, sa_row, sa_low, parent_ep = (uuid4() for _ in range(7))
    rescued_by_sa, rescued_by_path_a = uuid4(), uuid4()
    rescued = "sliced_off@heart_recall_limit"
    candidates = [
        _cand(cut_fact, "heart_primary", score=0.99, stage="heart_recall_limit"),
        _cand(rescued_by_sa, "heart_primary", score=0.98, restored_from=rescued),
        _cand(rescued_by_path_a, "heart_primary", score=0.97, stage="script_limit", restored_from=rescued),
        _cand(f1, "heart_primary", score=0.9),
        _cand(episode, "heart_primary", ctype="episode", score=0.95),
        _cand(unscored, "heart_primary", score=None),
        _cand(f2, "heart_primary", score=0.7, stage="script_limit"),
        _cand(f3, "heart_primary", score=0.6),
        _cand(f4, "heart_primary", score=0.5),
        _cand(chunk, "chunk", ctype="chunk"),
        _cand(chunk_cut, "chunk", ctype="chunk", stage="chunk_rrf_merge"),
        _cand(path_a, "heart_graph_memory", ctype="chunk"),
        _cand(path_2, "heart_graph", ctype="decision"),
        _cand(d1, "brain", ctype="decision", score=None),
        _cand(d2, "brain", ctype="decision", score=0.4),
        _cand(sa_row, "graph_expanded"),
        _cand(sa_low, "spreading_activation", stage="spreading_activation_floor"),
        _cand(parent_ep, "parent_episode", ctype="episode"),
    ]
    expansions = [
        _exp(f1, stage="stage2b_heart_graph_memory", neighbor=rescued_by_path_a),
        _exp(f1, stage="stage2b_heart_graph_memory", neighbor=path_a),
        _exp(f1, neighbor=sa_row),
    ]
    row = {"id": "r1", "agent_id": "agent-x", "query": "q", "candidates": candidates,
           "expansions": expansions}
    [rs] = parse_candidate_seeds([row], max_expand=1)
    assert rs.seeds == [(d1, "decision", 0.5), (f1, "fact", 0.9), (f2, "fact", 0.7), (f3, "fact", 0.6)]
    assert rs.exclude == {
        f1, episode, unscored, f2, f3, f4, chunk, path_a, path_2, d1, d2, rescued_by_path_a}
    assert parse_candidate_seeds([row], max_expand=2)[0].seeds[:2] == [
        (d1, "decision", 0.5), (d2, "decision", 0.4)]
    # A driver that does not decode JSONB; a retrieval with no seed is dropped.
    raw = parse_candidate_seeds(
        [{**row, "candidates": json.dumps(candidates), "expansions": json.dumps(expansions)}], 1)
    assert (raw[0].seeds, raw[0].exclude) == (rs.seeds, rs.exclude)
    assert parse_candidate_seeds([{**row, "candidates": [_cand(chunk, "chunk", ctype="chunk")]}], 1) == []


def test_edge_kind():
    assert edge_kind("co_occurred", "co_occurrence") == "co_occurred"
    assert edge_kind("related_to", "co_mention") == "co_mention"
    assert edge_kind("related_to", "inferred") == "other"
    assert edge_kind("related_to", None) == "other"


def test_score_retrieval_reports_edge_kinds():
    seed, n1, n2, n3 = (uuid4() for _ in range(4))
    e1, e2 = uuid4(), uuid4()
    rs = ReplaySeed(retrieval_id="r", agent_id="agent-x", query="q", seeds=[(seed, "fact", 0.8)])
    run = ArmRun(
        rows=[(n1, "fact", 0.4, 1), (n2, "fact", 0.2, 1), (n3, "fact", 0.05, 2)], seconds=0.001,
        parent_of={n1: seed, n2: seed, n3: n1},
        edge_of={n1: EdgeHop("other", e1), n2: EdgeHop("co_occurred", e2), n3: EdgeHop("co_mention", e1)},
    )
    out = sa_replay._score_retrieval(
        rs, {"baseline": run}, {"baseline": Settings(_env_file=None)}, {}, edge_kinds=True)
    arm = out["arms"]["baseline"]
    assert arm["edge_kinds"] == {"other": 1, "co_occurred": 1}, "n3 is below the floor"
    assert arm["assoc_rows"] == [
        {"id": str(n2), "kind": "co_occurred", "edge_id": str(e2), "depth": 1, "activation": 0.2}]
    summary = sa_replay._summarise([out, out], ["baseline"])["baseline"]
    assert summary["edge_kinds_kept"] == {"co_occurred": 2, "co_mention": 0, "other": 2}
    assert summary["retrievals_with_assoc_rows_kept"] == 2
    plain = sa_replay._score_retrieval(rs, {"baseline": run}, {"baseline": Settings(_env_file=None)}, {})
    assert "edge_kinds" not in plain["arms"]["baseline"]
    assert "edge_kinds_kept" not in sa_replay._summarise([plain], ["baseline"])["baseline"]


# ---------------------------------------------------------------------------
# Base config
# ---------------------------------------------------------------------------


def test_load_base_config_named_keys_only(tmp_path):
    path = _env_file(tmp_path, _A2_LIKE + "SECRET=x\nNOUS_OTHER_FLAG=1\n")
    values = load_base_config(path)
    assert values == {
        "NOUS_SPREADING_ACTIVATION_ENABLED": "true",
        "NOUS_GRAPH_NEIGHBOR_SEED_SCORE_ENABLED": "true",
    }
    assert "SECRET" not in values and "x" not in values.values()
    assert set(values) <= set(BASE_CONFIG_KEYS)

    # A parity key with no line at all (neither set nor "# unset:") raises.
    missing = _env_file(tmp_path, _A2_LIKE.replace(
        "# unset: NOUS_GRAPH_INFERRED_EDGE_PENALTY\n", ""), "missing.env")
    with pytest.raises(ValueError, match="NOUS_GRAPH_INFERRED_EDGE_PENALTY"):
        load_base_config(missing)


def test_base_config_marks_defaults(tmp_path):
    path = _env_file(tmp_path, _A2_LIKE)
    sources = base_config_sources(path)
    assert sources["NOUS_GRAPH_NEIGHBOR_SEED_SCORE_ENABLED"] == "file"
    assert sources["NOUS_SPREADING_SCORE_DEPTH1_PARITY"] == "unset"
    # Absent from the file and not a parity key: the Settings default.
    assert sources["NOUS_SPREADING_ACTIVATION_FLOOR"] == "default"
    assert set(sources) == set(BASE_CONFIG_KEYS)
    # A "# unset:" parity key is not passed on, so it resolves to the Settings default.
    values = load_base_config(path)
    assert "NOUS_SPREADING_ACTIVATION_DECAY" not in values
    base = build_settings(values)
    assert base.spreading_activation_decay == Settings.model_fields["spreading_activation_decay"].default
    assert base.spreading_score_depth1_parity is False
    assert base.graph_inferred_edge_penalty == 1.0

    no_decay = _env_file(tmp_path, _A2_LIKE.replace(
        "# unset: NOUS_SPREADING_ACTIVATION_DECAY\n", ""), "no_decay.env")
    with pytest.raises(ValueError, match="NOUS_SPREADING_ACTIVATION_DECAY"):
        load_base_config(no_decay)


def test_base_config_ignores_ambient_env(tmp_path, monkeypatch):
    """A developer's shell NOUS_* variables and a .env in the cwd must not leak
    into the base: either would silently change every arm."""
    monkeypatch.setenv("NOUS_SPREADING_ACTIVATION_FLOOR", "0.9")
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        "NOUS_GRAPH_RECALL_DECAY=0.2\nNOUS_SPREADING_ACTIVATION_MAX_DEPTH=3\n", encoding="utf-8")
    base = build_settings(load_base_config(_env_file(tmp_path, _A2_LIKE)))
    assert base.spreading_activation_floor == 0.1
    assert base.graph_recall_decay == 0.7
    assert base.spreading_activation_max_depth == 2
    assert base.graph_neighbor_seed_score_enabled is True
    # The ambient environment is restored afterwards.
    assert os.environ["NOUS_SPREADING_ACTIVATION_FLOOR"] == "0.9"


def test_base_config_makes_parity_active(tmp_path):
    """Under the A2 base the parity branch is live (a depth-1 row scores
    activation / decay = 2.0x); on a bare base the same flag is inert (0.7x,
    today's graph_recall_decay). That difference is why the replay needs A2."""
    parity = {"NOUS_SPREADING_SCORE_DEPTH1_PARITY": "true"}
    a2 = build_settings(load_base_config(_env_file(tmp_path, _A2_LIKE)), parity)
    bare = build_settings({}, parity)
    assert a2.graph_neighbor_seed_score_enabled is True
    assert bare.graph_neighbor_seed_score_enabled is False
    row = NeighborResult(
        id=uuid4(), node_type="fact", description="d", edge_relation="spreading_activation",
        edge_weight=0.4, created_at=datetime.now(UTC), seed_score=None,
    )
    assert _score_memory_neighbor(row, a2) / 0.4 == pytest.approx(2.0)
    assert _score_memory_neighbor(row, bare) / 0.4 == pytest.approx(0.7)


def test_base_config_keys_are_settings_fields():
    for key in BASE_CONFIG_KEYS:
        assert key.startswith("NOUS_")
        assert key[len("NOUS_"):].lower() in Settings.model_fields, key


# ---------------------------------------------------------------------------
# CLI pieces
# ---------------------------------------------------------------------------


def test_parse_arm():
    assert parse_arm("baseline") == ("baseline", {})
    assert parse_arm("deep=NOUS_SPREADING_ACTIVATION_MAX_DEPTH=3") == (
        "deep", {"NOUS_SPREADING_ACTIVATION_MAX_DEPTH": "3"})
    # A JSON value keeps its commas; only ",NOUS_KEY=" starts a new override.
    name, over = parse_arm(
        'mix=NOUS_DECISION_OUTCOME_SCORE_FACTORS={"superseded": 0.3, "noise": 0.1},'
        "NOUS_SPREADING_ACTIVATION_FLOOR=0.2")
    assert name == "mix"
    assert over == {
        "NOUS_DECISION_OUTCOME_SCORE_FACTORS": '{"superseded": 0.3, "noise": 0.1}',
        "NOUS_SPREADING_ACTIVATION_FLOOR": "0.2",
    }
    # With extra="ignore" an unknown key would silently replay the baseline.
    with pytest.raises(ValueError, match="NOUS_NO_SUCH_FLAG"):
        parse_arm("typo=NOUS_NO_SUCH_FLAG=true")
    with pytest.raises(ValueError):
        parse_arm("baseline=NOUS_SPREADING_ACTIVATION_FLOOR=0.2")
    with pytest.raises(ValueError):
        parse_arm("nameonly")


def test_refuses_prod_host(monkeypatch):
    monkeypatch.setenv("PROD_HOST", "db.example.internal")
    prod = "postgresql://u:p@DB.Example.Internal:5432/nous"
    with pytest.raises(SystemExit):
        check_dsn_host(prod, allow_prod=False)  # case-insensitive
    check_dsn_host(prod, allow_prod=True)
    # A prod host is named, so another remote host is known not to be it.
    check_dsn_host("postgresql://u:p@replica.example.internal/nous", allow_prod=False)
    # --prod-host names it as well as the environment does.
    monkeypatch.delenv("PROD_HOST")
    with pytest.raises(SystemExit):
        check_dsn_host(prod, allow_prod=False, prod_host="db.example.internal")


def test_refuses_unknown_remote_without_prod_host(monkeypatch):
    """With no prod host named, a remote host might be prod: fail closed."""
    monkeypatch.delenv("PROD_HOST", raising=False)
    remote = "postgresql://u:p@db.example.internal:5432/nous"
    with pytest.raises(SystemExit):
        check_dsn_host(remote, allow_prod=False)
    check_dsn_host(remote, allow_prod=True)
    with pytest.raises(SystemExit):
        check_dsn_host("postgresql:///nous", allow_prod=False)  # no readable host


def test_loopback_passes_without_prod_host(monkeypatch):
    monkeypatch.delenv("PROD_HOST", raising=False)
    for host in ("localhost", "LOCALHOST", "127.0.0.1", "127.8.9.10", "[::1]"):
        check_dsn_host(f"postgresql://u:p@{host}:5433/clone", allow_prod=False)


class _CountingEmbedder:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def embed(self, text: str) -> list[float]:
        self.calls.append(text)
        return [float(len(text)), 1.0]


async def test_embed_queries_cache_makes_rerun_free(tmp_path):
    cache = tmp_path / "emb.json"
    first = _CountingEmbedder()
    got = await embed_queries(["a", "bb", "a"], first, cache)
    assert got == {"a": [1.0, 1.0], "bb": [2.0, 1.0]}
    assert first.calls == ["a", "bb"], "each distinct query is embedded once"
    again = _CountingEmbedder()
    assert await embed_queries(["bb", "a"], again, cache) == got
    assert again.calls == [], "a rerun reads the cache"


# ---------------------------------------------------------------------------
# Postgres: the replay against the real CTE
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def brain(db, settings):
    b = Brain(database=db, settings=settings)
    yield b
    await b.close()


def _inp(description: str) -> RecordInput:
    return RecordInput(
        description=description, confidence=0.8, category="architecture",
        stakes="low", reasons=[ReasonInput(type="analysis", text="sa replay test")],
    )


async def _edge(session, agent_id, src, tgt, weight, relation="related_to", method="inferred"):
    """`flush`, never `commit`: the session fixture rolls back."""
    await session.execute(text(
        "INSERT INTO brain.graph_edges (source_id,target_id,source_type,target_type,"
        "agent_id,relation,weight,auto_linked,extraction_method) "
        "VALUES (:s,:t,'decision','decision',:a,:r,:w,true,:m)"),
        {"s": str(src), "t": str(tgt), "a": agent_id, "r": relation, "w": weight, "m": method})


@pytest.mark.postgres_only
async def test_run_arm_baseline_matches_direct_call(brain, session):
    """Distinct edge weights so the activation order cannot tie."""
    d1, d2, d3 = [
        (await brain.record(_inp(f"Replay identity decision {i} for spreading"), session=session)).id
        for i in range(1, 4)
    ]
    await _edge(session, brain.agent_id, d1, d2, 0.9)
    await _edge(session, brain.agent_id, d2, d3, 0.6)
    await _edge(session, brain.agent_id, d1, d3, 0.3)
    await session.flush()
    rs = ReplaySeed(retrieval_id="r", agent_id=brain.agent_id, query="q",
                    seeds=[(d1, "decision", 0.9)])
    settings = Settings()

    run = await run_arm(session, rs, settings)
    direct = await spreading_activation_search(
        session, brain.agent_id, rs.seeds, settings,
        limit=sa_replay.REPLAY_LIMIT, exclude_ids={d1},
    )
    assert run.rows == direct
    assert [r[0] for r in run.rows] == [d2, d3], "non-vacuous: both neighbours, seed excluded"
    assert run.seed_of == {d2: d1, d3: d1}
    assert run.seconds > 0


@pytest.mark.postgres_only
async def test_run_arm_reports_the_via_node(session):
    """run_arm asks the CTE for each row's via node: T, two hops out, maps to H."""
    agent = f"sa-replay-{uuid4().hex[:8]}"
    s, h, t = uuid4(), uuid4(), uuid4()
    await insert_edge(session, agent, s, h)
    await insert_edge(session, agent, h, t)
    rs = ReplaySeed(retrieval_id="r", agent_id=agent, query="q", seeds=[(s, "fact", 0.8)])
    run = await run_arm(session, rs, Settings(_env_file=None))
    assert [r[0] for r in run.rows] == [h, t]
    assert run.parent_of == {h: s, t: h}
    assert run.seed_of == {h: s, t: s}


@pytest.mark.postgres_only
async def test_load_replay_seeds_reads_sa_stage_for_agent(session):
    """Agent-scoped, from `since` on, SA-stage expansions only, oldest first."""
    agent = f"sa-replay-{uuid4().hex[:8]}"
    now = datetime.now(UTC)
    rows = [
        ("old", agent, now - timedelta(days=2), [_exp(A)]),
        ("first", agent, now - timedelta(hours=2), [_exp(A, seed_score=0.9), _exp(B)]),
        ("path-a-only", agent, now - timedelta(hours=1), [_exp(C, stage="stage2b_heart_graph_memory")]),
        ("second", agent, now, [_exp(C, seed_score=0.5)]),
        ("foreign", f"{agent}-other", now, [_exp(D)]),
    ]
    for rid, ag, ts, exps in rows:
        await session.execute(text(
            "INSERT INTO nous_system.retrieval_log (id, agent_id, timestamp, path, query, expansions) "
            "VALUES (:id, :a, :ts, 'pipeline', :q, CAST(:e AS jsonb))"),
            {"id": f"{agent}-{rid}", "a": ag, "ts": ts, "q": rid, "e": json.dumps(exps)})
    await session.flush()

    got = await load_replay_seeds(session, agent, now - timedelta(days=1), limit=10)
    assert [rs.query for rs in got] == ["first", "second"]
    assert got[0].seeds == [(A, "fact", 0.9), (B, "fact", 0.8)]
    assert (await load_replay_seeds(session, agent, now - timedelta(days=1), limit=1))[0].query == "first"


@pytest.mark.postgres_only
async def test_load_degrees_counts_eligible_edges_for_agent(session):
    """Degree is the node's fan in the traversal: undirected, agent-scoped,
    and blind to the edges the CTE refuses (co_occurred, co_mention)."""
    agent = f"sa-replay-{uuid4().hex[:8]}"
    hub, x, y, z, w = (uuid4() for _ in range(5))
    await _edge(session, agent, hub, x, 0.5)
    await _edge(session, agent, y, hub, 0.5)  # inbound counts too
    await _edge(session, agent, hub, z, 0.5, method="co_mention")
    await _edge(session, f"{agent}-other", hub, w, 0.5)
    await session.flush()
    degree = await load_degrees(session, agent)
    assert degree[hub] == 2
    assert degree[x] == 1 and degree[y] == 1
    assert z not in degree and w not in degree


def _log_row_sql(agent, rid, ts, *, legs, candidates, truncated=False):
    return {"id": f"{agent}-{rid}", "a": agent, "ts": ts, "q": rid, "legs": json.dumps(legs),
            "c": json.dumps(candidates), "tr": truncated}


_SA_LEG = {"name": "spreading_activation", "attempted": True, "n_returned": 0}


@pytest.mark.postgres_only
async def test_load_candidate_seeds_reads_sa_attempted_for_agent(session):
    """Retrievals whose SA leg was attempted and whose candidates are complete,
    agent-scoped and oldest first, also when SA recorded no expansion."""
    agent = f"sa-replay-{uuid4().hex[:8]}"
    now = datetime.now(UTC)
    fact = [_cand(A, "heart_primary", score=0.9)]
    rows = [
        _log_row_sql(agent, "first", now - timedelta(hours=2), legs=[_SA_LEG], candidates=fact),
        _log_row_sql(agent, "no-sa", now - timedelta(hours=1),
                     legs=[{"name": "heart_primary", "attempted": True}], candidates=fact),
        _log_row_sql(agent, "sa-off", now - timedelta(minutes=30),
                     legs=[{**_SA_LEG, "attempted": False}], candidates=fact),
        _log_row_sql(agent, "truncated", now - timedelta(minutes=20), legs=[_SA_LEG], candidates=fact,
                     truncated=True),
        _log_row_sql(agent, "second", now, legs=[_SA_LEG], candidates=[_cand(B, "heart_primary", score=0.6)]),
        _log_row_sql(f"{agent}-other", "foreign", now, legs=[_SA_LEG], candidates=fact),
    ]
    for r in rows:
        await session.execute(text(
            "INSERT INTO nous_system.retrieval_log (id, agent_id, timestamp, path, query, legs, candidates, "
            "expansions, truncated) VALUES (:id, :a, :ts, 'pipeline', :q, CAST(:legs AS jsonb), "
            "CAST(:c AS jsonb), '[]'::jsonb, :tr)"), r)
    await session.flush()

    since = now - timedelta(days=1)
    got = await load_candidate_seeds(session, agent, since, limit=10, max_expand=5)
    assert [rs.query for rs in got] == ["first", "second"]
    assert got[0].seeds == [(A, "fact", 0.9)]
    assert got[0].exclude == {A}
    assert agent in await sa_replay.list_replay_agents(session, since, "candidates")
    assert agent not in await sa_replay.list_replay_agents(session, since), "no SA expansion recorded"


@pytest.mark.postgres_only
async def test_run_arm_excludes_live_candidates(session):
    """rs.exclude leaves known candidates out of the window, and activation
    still passes through them (S -> A -> C)."""
    agent = f"sa-replay-{uuid4().hex[:8]}"
    s, a, b, c = uuid4(), uuid4(), uuid4(), uuid4()
    await insert_edge(session, agent, s, a, weight=0.9)
    await insert_edge(session, agent, s, b, weight=0.6)
    await insert_edge(session, agent, a, c, weight=0.8)
    rs = ReplaySeed(retrieval_id="r", agent_id=agent, query="q", seeds=[(s, "fact", 0.8)], exclude={a})
    run = await run_arm(session, rs, Settings(_env_file=None))
    assert [r[0] for r in run.rows] == [b, c]


@pytest.mark.postgres_only
async def test_last_hop_edges_picks_the_carrying_edge(session):
    """Parallel edges S-N: co_mention 0.9 and inferred 0.3 (stored N -> S). Under
    mult 0.5 the co_mention one carries N (0.45 > 0.3); under mult 0.3 the
    inferred one does (0.27 < 0.3); flag off, co_mention is not eligible. On a
    tie (S-M, 0.6 x 0.5 = 0.3 against 0.3) the non-associative edge wins. T is two hops out,
    carried by H -co_occurred-> T."""
    agent = f"sa-replay-{uuid4().hex[:8]}"
    s, n, h, t, m = (uuid4() for _ in range(5))
    await insert_edge(session, agent, s, n, weight=0.9, method="co_mention")
    await insert_edge(session, agent, n, s, weight=0.3)
    await insert_edge(session, agent, s, h, weight=1.0)
    await insert_edge(session, agent, h, t, weight=0.8, relation="co_occurred", method="co_occurrence")
    await insert_edge(session, agent, s, m, weight=0.6, method="co_mention")
    await insert_edge(session, agent, m, s, weight=0.3)
    run = ArmRun(rows=[], seconds=0.0, parent_of={n: s, h: s, t: h, m: s})
    rows = [n, h, t, m]

    def kinds(hops: dict[UUID, EdgeHop]) -> dict[UUID, str]:
        return {k: v.kind for k, v in hops.items()}

    assoc = Settings(_env_file=None, spreading_associative_edges_enabled=True)
    assert kinds(await last_hop_edges(session, agent, run, rows, assoc)) == {
        n: "co_mention", h: "other", t: "co_occurred", m: "other"}
    low = Settings(_env_file=None, spreading_associative_edges_enabled=True,
                   spreading_associative_weight_mult=0.3)
    assert kinds(await last_hop_edges(session, agent, run, rows, low))[n] == "other"
    assert kinds(await last_hop_edges(session, agent, run, rows, Settings(_env_file=None))) == {
        n: "other", h: "other", m: "other"}
    assert await last_hop_edges(session, agent, run, [], assoc) == {}


@pytest.mark.postgres_only
async def test_replay_edge_kinds_end_to_end(session, monkeypatch):
    """A CR-shaped graph: the seed's only edges are co_occurred, and one
    neighbour is already a live candidate. The baseline keeps nothing; the
    associative arm keeps the other neighbour, carried by a co_occurred edge."""

    async def _keep_fixture_transaction() -> None:
        """replay() ends its read-only transaction per retrieval; here that
        transaction is the fixture's, which must stay open to roll back."""

    monkeypatch.setattr(session, "rollback", _keep_fixture_transaction)
    agent = f"sa-replay-{uuid4().hex[:8]}"
    s, known, fresh = uuid4(), uuid4(), uuid4()
    await insert_edge(session, agent, s, known, weight=0.9, relation="co_occurred", method="co_occurrence")
    await insert_edge(session, agent, s, fresh, weight=0.9, relation="co_occurred", method="co_occurrence")
    rs = ReplaySeed(retrieval_id="r", agent_id=agent, query="q", seeds=[(s, "fact", 0.9)], exclude={s, known})
    arms = {
        "baseline": Settings(_env_file=None),
        "assoc": Settings(_env_file=None, spreading_associative_edges_enabled=True),
    }
    out = await replay(session, {agent: [rs]}, arms, edge_kinds=True)
    summary = sa_replay._summarise(out, list(arms))
    assert summary["baseline"]["retrievals_with_rows_above_floor"] == 0
    assert summary["assoc"]["rows_above_floor"] == 1
    assert summary["assoc"]["edge_kinds_kept"] == {"co_occurred": 1, "co_mention": 0, "other": 0}
    [row] = out[0]["arms"]["assoc"]["assoc_rows"]
    assert row["id"] == str(fresh)
    assert row["activation"] == pytest.approx(0.9 * 0.9 * 0.5 * 0.5)
