"""F105 Task 7: how the spreading activation CTE combines several seeds' activation on a node.

Each seed's contribution to a node is first its best path to it (one row per
(node, seed)), so parallel edges and cycles from one seed never count twice.
The contributions are then combined across seeds: ``max`` (today, the default),
``sat`` = ``min(1, sum)``, ``noisy_or`` = ``1 - prod(1 - a)``. The base graph:
seeds S1 (0.8) and S2 (0.6), each linked to X with w=1.0; decay 0.5, so the
single-seed activations of X are 0.4 and 0.3. S1 is given the larger id, so a
seed-id tie-break could never be what makes it win.

Nodes are bare UUIDs (``graph_edges`` has no foreign key to the node tables),
and every search passes ``limit=100``: the CTE's final ORDER BY has no tie key.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from spreading_v2_helpers import SNAPSHOT, insert_edge, render

from nous.api.retrieval_pipeline import _graph_expanded_to_pipeline, _score_memory_neighbor
from nous.brain.schemas import NeighborResult
from nous.brain.spreading_activation import spreading_activation_search
from nous.config import Settings

AGG_FIELDS = {"spreading_aggregation"}
COMBINING = ("sat", "noisy_or")
MODES = ("max", *COMBINING)


def _settings(**over: Any) -> Settings:
    """Settings defaults with no .env file (decay 0.5, max depth 2)."""
    return Settings(_env_file=None, **over)


def _mode(mode: str, **over: Any) -> Settings:
    return _settings(spreading_aggregation=mode, **over)


def _agent() -> str:
    return f"agg-{uuid4().hex[:8]}"


def _desc_ids() -> tuple[UUID, UUID]:
    """Two fresh ids, the larger first."""
    a, b = uuid4(), uuid4()
    return (a, b) if a > b else (b, a)


async def _two_seeds(session, agent: str) -> tuple[UUID, UUID, UUID]:
    """S1 - X and S2 - X, w=1.0. Returns (S1, S2, X), S1 the larger id."""
    s1, s2 = _desc_ids()
    x = uuid4()
    await insert_edge(session, agent, s1, x)
    await insert_edge(session, agent, s2, x)
    return s1, s2, x


async def _act(session, agent: str, seeds, settings, **kwargs: Any) -> dict[UUID, tuple[float, int]]:
    """``node -> (activation, depth)`` over the whole (limit=100) result."""
    rows = await spreading_activation_search(session, agent, seeds, settings, limit=100, **kwargs)
    return {r[0]: (r[2], r[3]) for r in rows}


def _parity(**over: Any) -> Settings:
    """Depth-1 parity on, under the policy that defines it (prod's): seed-score
    branch on, no inferred-edge penalty."""
    return _settings(
        spreading_score_depth1_parity=True, graph_neighbor_seed_score_enabled=True,
        graph_inferred_edge_penalty=1.0, **over,
    )


def _score(node: UUID, activation: float, settings: Settings) -> float:
    """The score the pipeline gives a spreading row (``_graph_expanded_to_pipeline``)."""
    row = NeighborResult(
        id=node, node_type="decision", description="n", edge_relation="spreading_activation",
        edge_weight=activation, created_at=datetime.now(UTC),
    )
    return _graph_expanded_to_pipeline([row], settings)[0].score


def test_aggregation_setting(monkeypatch):
    assert _settings().spreading_aggregation == "max"
    for mode in MODES:
        assert _mode(mode).spreading_aggregation == mode
    with pytest.raises(ValidationError):
        _mode("sum")
    monkeypatch.setenv("NOUS_SPREADING_AGGREGATION", "noisy_or")
    assert Settings(_env_file=None).spreading_aggregation == "noisy_or"


async def test_unknown_aggregation_raises():
    """A settings object that skipped validation gets an error, not today's SQL."""
    base = _settings()
    bad = SimpleNamespace(**{**{k: getattr(base, k) for k in Settings.model_fields}, "spreading_aggregation": "sum"})
    with pytest.raises(ValueError, match="sum"):
        await render(bad)


@pytest.mark.postgres_only
async def test_aggregation_max_default(session):
    """``max`` is today's code path: the SQL is the snapshot, and the rows equal
    those of settings that predate the aggregation field."""
    assert AGG_FIELDS <= set(Settings.model_fields), "the fields this test leaves out must exist"
    agent = _agent()
    s1, s2, x = await _two_seeds(session, agent)
    seeds = [(s1, "decision", 0.8), (s2, "decision", 0.6)]
    base = _settings()
    lacking = SimpleNamespace(**{k: getattr(base, k) for k in Settings.model_fields if k not in AGG_FIELDS})

    default = await spreading_activation_search(session, agent, seeds, base, limit=100)
    old = await spreading_activation_search(session, agent, seeds, lacking, limit=100)  # type: ignore[arg-type]
    assert default == old
    assert await spreading_activation_search(session, agent, seeds, _mode("max"), limit=100) == old
    assert {r[0]: (r[2], r[3]) for r in default}[x] == (pytest.approx(0.4), 1)

    snapshot = SNAPSHOT.read_text(encoding="utf-8")
    assert await render(lacking) == snapshot
    assert await render(_mode("max")) == snapshot
    assert await render(_mode("max"), parent_out={}) == await render(lacking, parent_out={})
    for mode in COMBINING:
        assert "per_seed" in await render(_mode(mode))


@pytest.mark.postgres_only
async def test_aggregation_sat(session):
    agent = _agent()
    s1, s2, x = await _two_seeds(session, agent)
    act = await _act(session, agent, [(s1, "decision", 0.8), (s2, "decision", 0.6)], _mode("sat"))
    assert act[x] == (pytest.approx(0.7), 1)


@pytest.mark.postgres_only
async def test_aggregation_noisy_or(session):
    agent = _agent()
    s1, s2, x = await _two_seeds(session, agent)
    act = await _act(session, agent, [(s1, "decision", 0.8), (s2, "decision", 0.6)], _mode("noisy_or"))
    assert act[x] == (pytest.approx(1 - 0.6 * 0.7), 1)  # 0.58


@pytest.mark.postgres_only
async def test_parallel_edges_do_not_inflate(session):
    """Three edges between S1 and X (two relations one way, one the other way)
    are three paths from one seed: X keeps one contribution, 0.4. Y, over a
    lone ``supports`` edge, shows that relation is traversed."""
    agent = _agent()
    s1, x, y = uuid4(), uuid4(), uuid4()
    await insert_edge(session, agent, s1, x, relation="related_to")
    await insert_edge(session, agent, s1, x, relation="supports")
    await insert_edge(session, agent, x, s1, relation="related_to")
    await insert_edge(session, agent, s1, y, relation="supports")
    for mode in MODES:
        act = await _act(session, agent, [(s1, "decision", 0.8)], _mode(mode))
        assert act[x] == (pytest.approx(0.4), 1), mode
        assert act[y] == (pytest.approx(0.4), 1), mode


@pytest.mark.postgres_only
async def test_cycle_does_not_inflate(session):
    """Triangle S1-A-B-S1 with X off A, max depth 3. X has two paths from S1
    (0.2 via A, 0.1 via B then A) and S1 one back to itself; each keeps its
    best. With one seed every mode returns max's rows, and X equals X on the
    same graph without the B-S1 edge (fresh ids: the edge key has no agent)."""
    agent = _agent()
    s1, a, b, x = (uuid4() for _ in range(4))
    t1, ta, tb, tx = (uuid4() for _ in range(4))
    for src, tgt in ((s1, a), (a, b), (b, s1), (a, x), (t1, ta), (ta, tb), (ta, tx)):
        await insert_edge(session, agent, src, tgt)
    seeds = [(s1, "decision", 0.8)]
    deep = {"spreading_activation_max_depth": 3}

    by_max = await _act(session, agent, seeds, _mode("max", **deep))
    baseline = await _act(session, agent, [(t1, "decision", 0.8)], _mode("max", **deep))
    assert baseline[tx] == (pytest.approx(0.2), 2)
    x_baseline = baseline[tx][0]
    for mode in MODES:
        act = await _act(session, agent, seeds, _mode(mode, **deep))
        assert act[x] == (pytest.approx(x_baseline), 2), mode
        assert act[s1] == (pytest.approx(0.8), 0), mode
        assert act == {n: (pytest.approx(v), d) for n, (v, d) in by_max.items()}, mode


@pytest.mark.postgres_only
async def test_aggregation_bounded(session):
    """Five seeds at 1.0 into one node: sat caps at 1.0, noisy-OR stays below
    it. Each seed's own row is a path at 1.0, which ``LN(1 - a)`` cannot take:
    the guard keeps the statement from failing. At decay 1.0, X's five paths
    are all 1.0 as well; noisy-OR is then 1 - 1e-30, which is 1.0 in a double."""
    agent = _agent()
    x = uuid4()
    seeds = [(uuid4(), "decision", 1.0) for _ in range(5)]
    for sid, _, _ in seeds:
        await insert_edge(session, agent, sid, x)

    for decay in (0.5, 1.0):
        sat = await _act(session, agent, seeds, _mode("sat", spreading_activation_decay=decay))
        noisy = await _act(session, agent, seeds, _mode("noisy_or", spreading_activation_decay=decay))
        assert set(sat) == set(noisy) == {x} | {s[0] for s in seeds}
        assert all(v <= 1.0 for v, _ in (*sat.values(), *noisy.values())), decay
        assert sat[x][0] == pytest.approx(1.0)
        if decay == 0.5:
            assert noisy[x][0] == pytest.approx(1 - 0.5 ** 5)
            assert noisy[x][0] < 1.0
            assert all(noisy[s[0]][0] < 1.0 for s in seeds), "own path 1.0 is capped at 0.999999"

    lone = uuid4()
    rows = await spreading_activation_search(session, agent, [(lone, "decision", 1.0)], _mode("noisy_or"))
    assert rows == [(lone, "decision", pytest.approx(0.999999), 0)]


@pytest.mark.postgres_only
async def test_winning_seed_is_largest_contributor(session):
    agent = _agent()
    s1, s2, x = await _two_seeds(session, agent)
    seeds = [(s1, "decision", 0.8), (s2, "decision", 0.6)]
    for mode in COMBINING:
        seed_of: dict[UUID, UUID] = {}
        act = await _act(session, agent, seeds, _mode(mode), seed_out=seed_of)
        assert seed_of[x] == s1, mode
        assert act[x][1] == 1, mode


@pytest.mark.postgres_only
async def test_parent_out_with_aggregation(session):
    """S1 (0.8) reaches X by two depth-2 paths, via A (0.2) and via B (w 0.5,
    0.1); S2 (0.3) reaches it directly (0.15). S1 contributes most, through its
    best path: X reports seed S1, depth 2 (not S2's shorter 1) and parent A."""
    agent = _agent()
    s1, s2, a, b, x = (uuid4() for _ in range(5))
    await insert_edge(session, agent, s1, a)
    await insert_edge(session, agent, a, x)
    await insert_edge(session, agent, s1, b, weight=0.5)
    await insert_edge(session, agent, b, x)
    await insert_edge(session, agent, s2, x)
    seeds = [(s1, "decision", 0.8), (s2, "decision", 0.3)]
    expected = {"sat": 0.35, "noisy_or": 1 - 0.8 * 0.85}
    for mode in COMBINING:
        parents: dict[UUID, UUID] = {}
        seed_of: dict[UUID, UUID] = {}
        act = await _act(session, agent, seeds, _mode(mode), parent_out=parents, seed_out=seed_of)
        assert act == await _act(session, agent, seeds, _mode(mode)), "parent_out changes no row"
        assert act[x] == (pytest.approx(expected[mode]), 2), mode
        assert (seed_of[x], parents[x]) == (s1, a), mode
        assert parents[a] == s1
        assert parents[s2] == s2


@pytest.mark.postgres_only
async def test_aggregation_composes_with_fan_and_assoc(session):
    """Fan normalisation and associative edges scale each path before the
    per-seed reduction. S1 (0.8) has degree 41 (40 leaves and X), so its hop is
    damped by sqrt(10 / 41); S2 (0.6) reaches X over a co_mention edge, so its
    hop pays the 0.5 multiplier. The modes combine those two values."""
    agent = _agent()
    s1, s2 = _desc_ids()
    x = uuid4()
    await insert_edge(session, agent, s1, x)
    for _ in range(40):
        await insert_edge(session, agent, s1, uuid4())
    await insert_edge(session, agent, s2, x, method="co_mention")
    seeds = [(s1, "decision", 0.8), (s2, "decision", 0.6)]
    flags = {"spreading_fan_normalisation_enabled": True, "spreading_associative_edges_enabled": True}
    a1 = 0.8 * 0.5 * (10 / 41) ** 0.5  # 0.1976
    a2 = 0.6 * 0.5 * 0.5  # 0.15
    expected = {"max": a1, "sat": a1 + a2, "noisy_or": 1 - (1 - a1) * (1 - a2)}

    by_max = await _act(session, agent, seeds, _mode("max", **flags))
    for mode in MODES:
        seed_of: dict[UUID, UUID] = {}
        act = await _act(session, agent, seeds, _mode(mode, **flags), seed_out=seed_of)
        assert act[x] == (pytest.approx(expected[mode]), 1), mode
        assert seed_of[x] == s1, mode
        leaves = [n for n in act if n not in {s1, s2, x}]
        assert len(leaves) == 40
        assert all(act[n] == (pytest.approx(by_max[n][0]), 1) for n in leaves), "one seed reaches a leaf"


@pytest.mark.postgres_only
async def test_parity_score_is_capped_at_one(session):
    """Under depth-1 parity a spreading row scores activation / decay. Two seeds
    at 1.0 give X 1.0 under sat (0.75 under noisy-OR), which would score 2.0
    (1.5), above every direct hit. The cap holds the score at 1.0."""
    agent = _agent()
    x = uuid4()
    seeds = [(uuid4(), "decision", 1.0), (uuid4(), "decision", 1.0)]
    for sid, _, _ in seeds:
        await insert_edge(session, agent, sid, x)
    raw = {"sat": 1.0, "noisy_or": 1 - 0.5 * 0.5}
    for mode in COMBINING:
        act = await _act(session, agent, seeds, _parity(spreading_aggregation=mode))
        assert act[x] == (pytest.approx(raw[mode]), 1), mode
        assert _score(x, act[x][0], _parity()) == pytest.approx(1.0), mode


@pytest.mark.postgres_only
async def test_parity_cap_is_a_no_op_under_max(session):
    """Under max a depth-1 row's parity score is seed x w <= 1, so the cap never
    binds. X (two seeds at 1.0, w 1.0) has activation 0.5 and scores 0.5 / 0.5 =
    1.0; Y (seed 0.8, w 0.9) has 0.36 and scores 0.72, the 1-hop leg's score for
    the same seed and edge."""
    agent = _agent()
    x, y, s = uuid4(), uuid4(), uuid4()
    seeds = [(uuid4(), "decision", 1.0), (uuid4(), "decision", 1.0)]
    for sid, _, _ in seeds:
        await insert_edge(session, agent, sid, x)
    await insert_edge(session, agent, s, y, weight=0.9)
    settings = _parity()

    act = await _act(session, agent, seeds, settings)
    assert act[x] == (pytest.approx(0.5), 1)
    assert _score(x, act[x][0], settings) == pytest.approx(0.5 / 0.5)

    act = await _act(session, agent, [(s, "decision", 0.8)], settings)
    assert act[y] == (pytest.approx(0.8 * 0.9 * 0.5), 1)
    one_hop = NeighborResult(
        id=y, node_type="decision", description="n", edge_relation="related_to", edge_weight=0.9,
        created_at=datetime.now(UTC), extraction_method="inferred", seed_score=0.8,
    )
    assert _score(y, act[y][0], settings) == pytest.approx(0.36 / 0.5)
    assert _score(y, act[y][0], settings) == pytest.approx(_score_memory_neighbor(one_hop, settings))
