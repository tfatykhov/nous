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
    ReplaySeed,
    base_config_sources,
    build_settings,
    check_dsn_host,
    embed_queries,
    hub_share,
    jaccard_at_k,
    load_base_config,
    load_degrees,
    load_replay_seeds,
    parse_arm,
    parse_replay_seeds,
    percentile,
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
