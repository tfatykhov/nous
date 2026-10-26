"""F105 Task 5: the query-conditioned gate on spreading activation, after the CTE.

With the flag on, Stage 4 embeds the query once, reads ``cos(node, q)`` for each
activated row and for its winning seed (``query_cosines``), and multiplies the
row's activation by ``gate_factor`` before the floor and the cap. The factor is
in [0, 1], so the gate never raises a score; a missing embedding is neutral
(1.0), and any failure leaves the rows ungated. The CTE's SQL does not change
(tests/test_spreading_sql_identity.py).

Pipeline seeds are the two mocked decisions: DECISION_ONE (score 0.75) and
DECISION_TWO (score None, so 0.5 as a spreading seed).
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import text
from test_retrieval_pipeline import (  # noqa: E402  (test-local helper reuse)
    DECISION_ONE_ID,
    DECISION_TWO_ID,
    _make_brain,
    _make_decision_summaries,
    _make_heart,
    _make_settings,
)

from nous.api.retrieval_pipeline import run_recall_pipeline
from nous.brain.spreading_activation import gate_factor, query_cosines
from nous.config import Settings
from nous.observability.retrieval_trace import BELOW_FLOOR, SLICED_OFF, RetrievalTrace

AT = datetime(2026, 1, 5, tzinfo=UTC)
DIM = 1536
SA_STAGE = "stage4_spreading_activation"
NA, NB, NC = (UUID(f"cccccccc-0000-0000-0000-00000000000{i}") for i in range(1, 4))


def _vec(**coords: float) -> str:
    """A 1536-dim pgvector literal: ``_vec(e1=1.0)`` is the first basis vector."""
    v = [0.0] * DIM
    for name, value in coords.items():
        v[int(name[1:]) - 1] = value
    return "[" + ",".join(f"{x:.6f}" for x in v) + "]"


def _e1() -> list[float]:
    return [1.0] + [0.0] * (DIM - 1)


# ---------------------------------------------------------------------------
# gate_factor: pure
# ---------------------------------------------------------------------------


def test_gate_factor_modes():
    def sr(node: float | None, seed: float | None) -> float:
        return gate_factor(node, seed, "seed_relative", 0.0, 1.0, 0.3)

    assert sr(0.35, 0.5) == pytest.approx(0.7)
    assert sr(0.6, 0.5) == 1.0, "capped: a node more relevant than its seed is not amplified"
    assert sr(-0.1, 0.5) == 0.0
    assert sr(0.15, 0.0) == pytest.approx(0.5), "the denominator is s_min"
    assert sr(0.15, -0.2) == pytest.approx(0.5), "a negative seed still gates"
    assert sr(0.15, 0.1) == pytest.approx(0.5), "a seed below s_min uses s_min"
    assert sr(0.1, None) == 1.0, "the seed has no embedding: neutral"
    assert sr(None, 0.5) == 1.0, "the node has no embedding: neutral"

    def qt(node: float | None) -> float:
        return gate_factor(node, 0.9, "quantile", 0.2, 0.6, 0.3)

    assert qt(0.1) == 0.0
    assert qt(0.4) == pytest.approx(0.5)
    assert qt(0.7) == 1.0
    assert qt(None) == 1.0

    def raw(node: float | None) -> float:
        return gate_factor(node, 0.9, "raw", 0.0, 1.0, 0.3)

    assert raw(0.35) == pytest.approx(0.35)
    assert raw(-0.2) == 0.0
    assert raw(None) == 1.0

    for mode in ("seed_relative", "quantile", "raw"):
        for node in (-1.0, -0.3, 0.0, 0.05, 0.2, 0.5, 0.99, 1.0, 1.0000001, None):
            for seed in (-0.5, 0.0, 0.1, 0.5, 1.0, None):
                f = gate_factor(node, seed, mode, 0.2, 0.6, 0.3)
                assert 0.0 <= f <= 1.0, (mode, node, seed, f)
    with pytest.raises(ValueError):
        gate_factor(0.5, 0.5, "boost", 0.0, 1.0, 0.3)


def test_gate_settings_bounds():
    """Off by default; the quantile anchors must be ordered; s_min is in (0, 1]."""
    s = Settings(_env_file=None)
    assert s.spreading_query_gate_enabled is False
    assert s.spreading_query_gate_mode == "seed_relative"
    assert (s.spreading_query_gate_q_lo, s.spreading_query_gate_q_hi) == (0.0, 1.0)
    assert 0.0 < s.spreading_query_gate_seed_min <= 1.0
    Settings(_env_file=None, spreading_query_gate_q_lo=0.1, spreading_query_gate_q_hi=0.5)
    for bad in (
        {"spreading_query_gate_q_lo": 0.5, "spreading_query_gate_q_hi": 0.5},
        {"spreading_query_gate_q_lo": 0.6, "spreading_query_gate_q_hi": 0.5},
        {"spreading_query_gate_q_lo": -0.1},
        {"spreading_query_gate_q_hi": 0.0},
        {"spreading_query_gate_seed_min": 0.0},
        {"spreading_query_gate_seed_min": 1.1},
        {"spreading_query_gate_mode": "boost"},
    ):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, **bad)


# ---------------------------------------------------------------------------
# query_cosines: real Postgres
# ---------------------------------------------------------------------------


async def _fact(session, agent: str, embedding: str | None) -> UUID:
    fid = uuid4()
    await session.execute(text(
        "INSERT INTO heart.facts (id, agent_id, content, active) VALUES (:id, :a, 'gate test', true)"),
        {"id": fid, "a": agent})
    if embedding is not None:
        await session.execute(text(
            "UPDATE heart.facts SET embedding = CAST(:v AS vector) WHERE id = :id"),
            {"v": embedding, "id": fid})
    await session.flush()
    return fid


@pytest.mark.postgres_only
async def test_query_cosines_values(session):
    agent = f"gate-{uuid4().hex[:8]}"
    a = await _fact(session, agent, _vec(e1=1.0))
    b = await _fact(session, agent, _vec(e1=1 / math.sqrt(2), e2=1 / math.sqrt(2)))
    c = await _fact(session, agent, _vec(e2=1.0))
    d = await _fact(session, agent, None)
    missing, odd = uuid4(), uuid4()
    nodes = [(a, "fact"), (b, "fact"), (c, "fact"), (d, "fact"), (missing, "fact"), (odd, "unknown")]
    cos = await query_cosines(session, agent, nodes, _e1())
    assert cos[a] == pytest.approx(1.0)
    assert cos[b] == pytest.approx(0.7071, abs=1e-4)
    assert cos[c] == pytest.approx(0.0, abs=1e-6)
    assert cos[d] is None, "NULL embedding"
    assert cos[missing] is None, "no row"
    assert cos[odd] is None, "a node type with no table"
    assert await query_cosines(session, agent, [], _e1()) == {}


@pytest.mark.postgres_only
async def test_query_cosines_reads_every_node_type(session):
    """Each node type reads its own table (decision, fact, episode, procedure, chunk)."""
    agent = f"gate-{uuid4().hex[:8]}"
    e1 = _vec(e1=1.0)
    ids = {t: uuid4() for t in ("decision", "fact", "episode", "procedure", "chunk")}
    inserts = {
        "decision": "INSERT INTO brain.decisions (id, agent_id, description, confidence, category, stakes, "
                    "embedding) VALUES (:id, :a, 'gate', 0.8, 'architecture', 'low', CAST(:v AS vector))",
        "fact": "INSERT INTO heart.facts (id, agent_id, content, embedding) "
                "VALUES (:id, :a, 'gate', CAST(:v AS vector))",
        "episode": "INSERT INTO heart.episodes (id, agent_id, summary, embedding) "
                   "VALUES (:id, :a, 'gate', CAST(:v AS vector))",
        "procedure": "INSERT INTO heart.procedures (id, agent_id, name, embedding) "
                     "VALUES (:id, :a, 'gate', CAST(:v AS vector))",
        "chunk": "INSERT INTO heart.episode_chunks (id, agent_id, episode_id, chunk_index, content, embedding) "
                 "VALUES (:id, :a, :ep, 0, 'gate', CAST(:v AS vector))",
    }
    for t in ("decision", "fact", "episode", "procedure", "chunk"):
        await session.execute(text(inserts[t]), {"id": ids[t], "a": agent, "v": e1, "ep": ids["episode"]})
    await session.flush()
    cos = await query_cosines(session, agent, [(nid, t) for t, nid in ids.items()], _e1())
    assert cos == {nid: pytest.approx(1.0) for nid in ids.values()}
    # A node read under the wrong type is not found.
    assert (await query_cosines(session, agent, [(ids["fact"], "episode")], _e1())) == {ids["fact"]: None}


@pytest.mark.postgres_only
async def test_gate_is_agent_scoped(session):
    """Another agent's fact never resolves, whatever its embedding."""
    agent = f"gate-{uuid4().hex[:8]}"
    foreign = await _fact(session, f"{agent}-other", _vec(e1=1.0))
    own = await _fact(session, agent, _vec(e1=1.0))
    cos = await query_cosines(session, agent, [(foreign, "fact"), (own, "fact")], _e1())
    assert cos[foreign] is None
    assert cos[own] == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Stage 4: the gate in the pipeline (mocked CTE and cosines)
# ---------------------------------------------------------------------------


def _settings(gate: dict[str, Any] | None) -> SimpleNamespace:
    """Pipeline settings with spreading forced on; ``gate`` holds the
    ``spreading_query_gate_*`` fields (None: the settings lack them)."""
    s = _make_settings(spreading_activation_enabled="true")
    for key, value in (gate or {}).items():
        setattr(s, f"spreading_query_gate_{key}", value)
    return s


GATE_ON = {"enabled": True, "mode": "seed_relative", "q_lo": 0.0, "q_hi": 1.0, "seed_min": 0.3}


async def _run(
    *,
    gate: dict[str, Any] | None,
    activated: list[tuple[UUID, str, float, int]],
    seeds_out: dict[UUID, UUID] | None = None,
    cosines: dict[UUID, float | None] | None = None,
    embedder: Any = "default",
    cosines_error: Exception | None = None,
) -> tuple[RetrievalTrace, Any, AsyncMock, Any]:
    """Run the pipeline with a real trace; returns (trace, stats, query_cosines mock, embedder)."""
    heart = _make_heart(recall_results=[])
    brain = _make_brain(neighbors_by_node={}, contradictions=[], decision_results=_make_decision_summaries())
    brain._resolve_node_descriptions = AsyncMock(
        return_value={nid: (f"row {nid}", AT) for nid, *_ in activated})
    if embedder == "default":
        embedder = MagicMock()
        embedder.embed = AsyncMock(return_value=_e1())
    brain.embeddings = embedder

    async def fake_search(*_a, seed_out=None, **_k):
        if seed_out is not None and seeds_out:
            seed_out.update(seeds_out)
        return list(activated)

    qc = AsyncMock(return_value=cosines or {}, side_effect=cosines_error)
    tr = RetrievalTrace(query="q")
    with patch(
        "nous.brain.spreading_activation.spreading_activation_search", AsyncMock(side_effect=fake_search),
    ), patch(
        "nous.brain.spreading_activation.compute_graph_density", AsyncMock(return_value=5.0),
    ), patch("nous.brain.spreading_activation.query_cosines", qc):
        _results, stats = await run_recall_pipeline(
            query="q", heart=heart, brain=brain, settings=_settings(gate), limit=10, trace=tr,
        )
    return tr, stats, qc, embedder


def _sa_rows(tr: RetrievalTrace) -> dict[str, dict]:
    return {e["neighbor_id"]: e for e in tr.to_dict()["expansions"] if e["stage"] == SA_STAGE}


def _candidates(tr: RetrievalTrace) -> dict[str, dict]:
    return {c["id"]: c for c in tr.to_dict()["candidates"]}


async def test_gate_off_is_identical():
    """Flag off: no embed call, no cosine read, and the same rows, activations
    and order as settings that predate the gate fields."""
    activated = [(NA, "fact", 0.5, 1), (NB, "fact", 0.3, 2), (NC, "fact", 0.05, 1)]
    seeds_out = {NA: DECISION_ONE_ID, NB: DECISION_TWO_ID, NC: DECISION_ONE_ID}
    runs = [await _run(gate=g, activated=activated, seeds_out=seeds_out)
            for g in (None, {**GATE_ON, "enabled": False})]
    for tr, stats, qc, embedder in runs:
        embedder.embed.assert_not_awaited()
        qc.assert_not_awaited()
        assert "spreading_query_gate" not in stats.n_stage_errors
    (old, _, _, _), (off, _, _, _) = runs
    assert old.to_dict()["expansions"] == off.to_dict()["expansions"]
    assert [(c["id"], c["disposition"], c["entry_score"]) for c in old.to_dict()["candidates"]] == [
        (c["id"], c["disposition"], c["entry_score"]) for c in off.to_dict()["candidates"]]
    rows = _sa_rows(off)
    assert list(rows) == [str(NA), str(NB)], "NC is below the floor"
    assert [(r["edge_weight"], r["path_strength"]) for r in rows.values()] == [(0.5, 0.5), (0.3, 0.3)]


async def test_gate_records_raw_and_gated():
    """The expansion row keeps the raw activation as path_strength and the gated
    one as edge_weight; cosines are read for the rows and their winning seeds."""
    activated = [(NA, "fact", 0.5, 1), (NB, "fact", 0.4, 2)]
    seeds_out = {NA: DECISION_ONE_ID, NB: DECISION_TWO_ID}
    cosines = {NA: 0.35, NB: 0.6, DECISION_ONE_ID: 0.5, DECISION_TWO_ID: 0.8}
    tr, stats, qc, embedder = await _run(gate=GATE_ON, activated=activated, seeds_out=seeds_out, cosines=cosines)
    embedder.embed.assert_awaited_once_with("q")
    qc.assert_awaited_once()
    _session, agent, nodes, vec = qc.await_args.args
    assert agent == "nous-test-agent"
    assert sorted(nodes) == sorted(
        [(NA, "fact"), (NB, "fact"), (DECISION_ONE_ID, "decision"), (DECISION_TWO_ID, "decision")])
    assert vec == _e1()
    rows = _sa_rows(tr)
    assert rows[str(NA)]["path_strength"] == 0.5
    assert rows[str(NA)]["edge_weight"] == pytest.approx(0.5 * gate_factor(0.35, 0.5, "seed_relative", 0, 1, 0.3))
    assert rows[str(NA)]["edge_weight"] == pytest.approx(0.35)
    assert rows[str(NB)]["path_strength"] == 0.4
    assert rows[str(NB)]["edge_weight"] == pytest.approx(0.4 * 0.75)
    assert "spreading_query_gate" not in stats.n_stage_errors


async def test_gate_missing_embedding_is_neutral():
    activated = [(NA, "fact", 0.5, 1), (NB, "fact", 0.4, 1)]
    seeds_out = {NA: DECISION_ONE_ID, NB: DECISION_ONE_ID}
    cosines = {NA: 0.2, NB: None, DECISION_ONE_ID: 0.5}
    tr, _stats, _qc, _e = await _run(gate=GATE_ON, activated=activated, seeds_out=seeds_out, cosines=cosines)
    rows = _sa_rows(tr)
    assert rows[str(NB)]["edge_weight"] == rows[str(NB)]["path_strength"] == 0.4
    assert rows[str(NA)]["edge_weight"] == pytest.approx(0.5 * 0.4), "non-vacuous: NA is gated"


async def test_gate_seed_without_embedding_is_neutral():
    activated = [(NA, "fact", 0.5, 1), (NB, "fact", 0.4, 1)]
    seeds_out = {NA: DECISION_ONE_ID, NB: DECISION_TWO_ID}
    cosines = {NA: 0.05, NB: 0.25, DECISION_ONE_ID: None, DECISION_TWO_ID: 0.5}
    tr, _stats, _qc, _e = await _run(gate=GATE_ON, activated=activated, seeds_out=seeds_out, cosines=cosines)
    rows = _sa_rows(tr)
    assert rows[str(NA)]["edge_weight"] == rows[str(NA)]["path_strength"] == 0.5
    assert rows[str(NB)]["edge_weight"] == pytest.approx(0.4 * 0.5), "non-vacuous: NB's seed has a cosine"


@pytest.mark.parametrize("embedder", [None, "empty"])
async def test_gate_skipped_without_embedder(embedder):
    if embedder == "empty":
        embedder = MagicMock()
        embedder.embed = AsyncMock(return_value=[])
    activated = [(NA, "fact", 0.5, 1), (NB, "fact", 0.3, 1)]
    tr, stats, qc, _e = await _run(
        gate=GATE_ON, activated=activated, seeds_out={NA: DECISION_ONE_ID, NB: DECISION_ONE_ID},
        cosines={NA: 0.0, NB: 0.0, DECISION_ONE_ID: 0.5}, embedder=embedder,
    )
    qc.assert_not_awaited()
    rows = _sa_rows(tr)
    assert [(r["edge_weight"], r["path_strength"]) for r in rows.values()] == [(0.5, 0.5), (0.3, 0.3)]
    assert "spreading_query_gate" not in stats.n_stage_errors


async def test_gate_zero_factor_falls_below_floor():
    """cos <= 0 under seed_relative: 0.4 becomes 0.0, which the floor drops."""
    activated = [(NA, "fact", 0.4, 1), (NB, "fact", 0.3, 1)]
    seeds_out = {NA: DECISION_ONE_ID, NB: DECISION_ONE_ID}
    cosines = {NA: -0.05, NB: 0.5, DECISION_ONE_ID: 0.5}
    tr, _stats, _qc, _e = await _run(gate=GATE_ON, activated=activated, seeds_out=seeds_out, cosines=cosines)
    assert str(NA) not in _sa_rows(tr)
    cand = _candidates(tr)[str(NA)]
    assert (cand["disposition"], cand["disposition_stage"]) == (BELOW_FLOOR, "spreading_activation_floor")
    assert cand["entry_score"] == 0.0, "the floor saw the gated value"
    assert _sa_rows(tr)[str(NB)]["edge_weight"] == 0.3


async def test_gate_cap_sees_gated_order():
    """21 rows clear the floor: the cap keeps the 20 best GATED rows, so the
    row the gate demoted from first is the one sliced off."""
    ids = [uuid4() for _ in range(21)]
    activated = [(nid, "fact", 0.9 - 0.01 * i, 1) for i, nid in enumerate(ids)]
    cosines: dict[UUID, float | None] = {nid: None for nid in ids}
    cosines[ids[0]] = 0.06  # 0.9 * 0.06 / 0.3 = 0.18: above the floor, below every other row
    cosines[DECISION_ONE_ID] = 0.2
    tr, _stats, _qc, _e = await _run(
        gate=GATE_ON, activated=activated, seeds_out={nid: DECISION_ONE_ID for nid in ids}, cosines=cosines)
    rows = _sa_rows(tr)
    assert str(ids[0]) not in rows
    assert set(rows) == {str(nid) for nid in ids[1:]}
    cand = _candidates(tr)[str(ids[0])]
    assert (cand["disposition"], cand["disposition_stage"]) == (SLICED_OFF, "spreading_result_cap")


async def test_gate_defaults_match_settings():
    """Settings without the parameter fields fall back to the Settings defaults:
    here seed_min, the denominator of a seed whose cosine is below it."""
    s_min = Settings.model_fields["spreading_query_gate_seed_min"].default
    assert s_min == 0.19, "the Task 5 replay's median seed cosine"
    activated = [(NA, "fact", 0.4, 1)]
    tr, _stats, _qc, _e = await _run(
        gate={"enabled": True}, activated=activated, seeds_out={NA: DECISION_ONE_ID},
        cosines={NA: 0.095, DECISION_ONE_ID: 0.05},
    )
    assert _sa_rows(tr)[str(NA)]["edge_weight"] == pytest.approx(0.4 * 0.095 / s_min)


async def test_gate_pruning_every_row_falls_back_to_one_hop():
    """Not a failure: a gate that prunes every row leaves spreading nothing to
    append, and the existing n_appended == 0 branch hands over to the 1-hop
    fallback, which the gate does not filter."""
    tr, stats, _qc, _e = await _run(
        gate=GATE_ON, activated=[(NA, "fact", 0.4, 1)], seeds_out={NA: DECISION_ONE_ID},
        cosines={NA: -0.1, DECISION_ONE_ID: 0.5},
    )
    assert _sa_rows(tr) == {}
    assert "spreading_query_gate" not in stats.n_stage_errors
    legs = {leg["name"]: leg for leg in tr.to_dict()["legs"]}
    assert legs["spreading_activation"]["n_returned"] == 0
    assert "brain_graph" in legs, "the 1-hop fallback ran"


async def test_gate_failure_fails_open():
    """A failing cosine read leaves the rows ungated and spreading in use: the
    1-hop fallback does not take over."""
    activated = [(NA, "fact", 0.5, 1)]
    tr, stats, _qc, _e = await _run(
        gate=GATE_ON, activated=activated, seeds_out={NA: DECISION_ONE_ID},
        cosines_error=RuntimeError("pgvector down"),
    )
    assert stats.n_stage_errors["spreading_query_gate"] == 1
    assert "spreading_activation" not in stats.n_stage_errors
    row = _sa_rows(tr)[str(NA)]
    assert row["edge_weight"] == row["path_strength"] == 0.5
