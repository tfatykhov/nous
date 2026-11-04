"""2026-11-03 memory review, PR-S: spreading over the edges that carry information.

- B-7 (``spreading_query_gate_order_only``): with the query gate on, the
  activation floor compares the RAW activation; the gated value still orders
  the rows for the result cap and is still their score.
- B-5 (``spreading_query_gate_unembedded_factor``): a node without an embedding
  gets this factor instead of the neutral 1.0, in every gate mode.
- ``spreading_associative_edges_only``: the traversal crosses associative edges
  (``co_occurred`` / ``co_mention``) and nothing else, with no weight multiplier.

Every setting defaults to today's behaviour. Pipeline tests reuse the mocked
Stage 4 of tests/test_spreading_v2_gate.py; traversal tests run the CTE on
Postgres with bare-UUID nodes (``graph_edges`` has no foreign key to them).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from spreading_v2_helpers import SNAPSHOT, insert_edge, render
from test_retrieval_pipeline import DECISION_ONE_ID  # noqa: E402
from test_spreading_v2_gate import (  # noqa: E402  (test-local helper reuse)
    NA,
    NB,
    NC,
    _candidates,
    _run,
    _sa_rows,
)

from nous.brain.graph_constants import associative_edge_sql, spreading_exclusion_sql
from nous.brain.spreading_activation import gate_factor, spreading_activation_search
from nous.config import Settings
from nous.observability.retrieval_trace import BELOW_FLOOR, SLICED_OFF

# The quantile gate at the F105 replay's anchors (0.06 / 0.19).
QUANTILE = {"enabled": True, "mode": "quantile", "q_lo": 0.06, "q_hi": 0.19, "seed_min": 0.19}


def _q(cos: float) -> float:
    return gate_factor(cos, None, "quantile", 0.06, 0.19, 0.19)


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


def test_alignment_settings_defaults_and_bounds():
    s = Settings(_env_file=None)
    assert s.spreading_query_gate_order_only is False
    assert s.spreading_query_gate_unembedded_factor == 1.0
    assert s.spreading_associative_edges_only is False
    for bad in (-0.01, 1.01):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, spreading_query_gate_unembedded_factor=bad)


# ---------------------------------------------------------------------------
# B-7: the floor reads the raw activation; the gate orders
# ---------------------------------------------------------------------------


async def test_order_only_floor_reads_raw_activation():
    """A typical depth-1 row (0.18) at cos 0.10 gates to 0.055, which the 0.1
    floor drops today. Order-only keeps it, at its gated score, and keeps the
    row the gate scores 0 at score 0. A row whose RAW activation is under the
    floor is still dropped, and the trace records that raw value."""
    activated = [(NA, "fact", 0.18, 1), (NB, "fact", 0.18, 1), (NC, "fact", 0.08, 1)]
    seeds_out = {NA: DECISION_ONE_ID, NB: DECISION_ONE_ID, NC: DECISION_ONE_ID}
    cosines = {NA: 0.10, NB: 0.02, NC: 0.10, DECISION_ONE_ID: 0.30}

    today, *_ = await _run(gate=QUANTILE, activated=activated, seeds_out=seeds_out, cosines=cosines)
    assert _sa_rows(today) == {}, "today the gate drops both rows at the floor"
    assert _candidates(today)[str(NA)]["disposition"] == BELOW_FLOOR
    assert _candidates(today)[str(NC)]["entry_score"] == pytest.approx(0.08 * _q(0.10)), "today: the gated value"

    tr, *_ = await _run(
        gate={**QUANTILE, "order_only": True}, activated=activated, seeds_out=seeds_out, cosines=cosines,
    )
    rows = _sa_rows(tr)
    assert list(rows) == [str(NA), str(NB)], "gated order: NA (0.055) before NB (0.0)"
    assert rows[str(NA)]["edge_weight"] == pytest.approx(0.18 * _q(0.10))
    assert rows[str(NA)]["path_strength"] == 0.18
    assert rows[str(NB)]["edge_weight"] == 0.0, "the gate scores NB 0; it survives at score 0"
    cand = _candidates(tr)[str(NC)]
    assert (cand["disposition"], cand["disposition_stage"]) == (BELOW_FLOOR, "spreading_activation_floor")
    assert cand["entry_score"] == 0.08, "the floor drop records the raw activation it compared"


async def test_order_only_cap_keeps_the_best_gated_rows():
    """21 rows clear the floor raw. The cap keeps the 20 best GATED rows: the
    row the gate demoted below the floor is sliced off by the cap, not by the
    floor as it is today."""
    ids = [uuid4() for _ in range(21)]
    activated = [(nid, "fact", 0.9 - 0.01 * i, 1) for i, nid in enumerate(ids)]
    cosines: dict[UUID, float | None] = {nid: 0.5 for nid in ids}
    cosines[ids[0]] = 0.07  # 0.9 * 0.077 = 0.069: under the floor gated, first raw
    cosines[DECISION_ONE_ID] = 0.3
    seeds_out = {nid: DECISION_ONE_ID for nid in ids}

    today, *_ = await _run(gate=QUANTILE, activated=activated, seeds_out=seeds_out, cosines=cosines)
    assert _candidates(today)[str(ids[0])]["disposition"] == BELOW_FLOOR

    tr, *_ = await _run(
        gate={**QUANTILE, "order_only": True}, activated=activated, seeds_out=seeds_out, cosines=cosines,
    )
    rows = _sa_rows(tr)
    assert set(rows) == {str(nid) for nid in ids[1:]}
    cand = _candidates(tr)[str(ids[0])]
    assert (cand["disposition"], cand["disposition_stage"]) == (SLICED_OFF, "spreading_result_cap")


async def test_order_only_without_the_gate_changes_nothing():
    """The flag only moves the floor of a gated run: with the gate off, the
    rows are those of a run without the flag."""
    activated = [(NA, "fact", 0.5, 1), (NB, "fact", 0.08, 1)]
    seeds_out = {NA: DECISION_ONE_ID, NB: DECISION_ONE_ID}
    runs = [
        await _run(gate=g, activated=activated, seeds_out=seeds_out)
        for g in ({"enabled": False, "order_only": True}, None)
    ]
    (on, *_), (old, *_) = runs
    assert on.to_dict()["expansions"] == old.to_dict()["expansions"]
    assert list(_sa_rows(on)) == [str(NA)]


async def test_order_only_off_is_today():
    """The flag at False matches settings that lack the field, with the gate on."""
    activated = [(NA, "fact", 0.18, 1), (NB, "fact", 0.4, 1)]
    seeds_out = {NA: DECISION_ONE_ID, NB: DECISION_ONE_ID}
    cosines = {NA: 0.10, NB: 0.30, DECISION_ONE_ID: 0.30}
    runs = [
        await _run(gate=g, activated=activated, seeds_out=seeds_out, cosines=cosines)
        for g in ({**QUANTILE, "order_only": False}, QUANTILE)
    ]
    (off, *_), (old, *_) = runs
    assert off.to_dict()["expansions"] == old.to_dict()["expansions"]
    assert [(c["id"], c["disposition"], c["entry_score"]) for c in off.to_dict()["candidates"]] == [
        (c["id"], c["disposition"], c["entry_score"]) for c in old.to_dict()["candidates"]]
    assert list(_sa_rows(off)) == [str(NB)]


# ---------------------------------------------------------------------------
# B-5: a node without an embedding is not favoured
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["seed_relative", "quantile", "raw"])
def test_unembedded_factor_in_every_mode(mode: str):
    assert gate_factor(None, 0.5, mode, 0.06, 0.19, 0.19) == 1.0, "default: neutral, as today"
    assert gate_factor(None, 0.5, mode, 0.06, 0.19, 0.19, 0.4) == 0.4
    assert gate_factor(None, None, mode, 0.06, 0.19, 0.19, 0.4) == 0.4
    assert gate_factor(None, 0.5, mode, 0.06, 0.19, 0.19, 1.5) == 1.0, "clamped: never amplifies"
    assert gate_factor(0.13, 0.5, mode, 0.06, 0.19, 0.19, 0.4) == gate_factor(
        0.13, 0.5, mode, 0.06, 0.19, 0.19), "an embedded node is untouched"


def test_unembedded_factor_leaves_a_seed_without_embedding_neutral():
    assert gate_factor(0.1, None, "seed_relative", 0.0, 1.0, 0.19, 0.4) == 1.0


async def test_unembedded_node_no_longer_outranks_embedded_rows():
    """Quantile gate: unembedded NA (0.5) kept 0.5 and ranked above embedded NB
    (0.45 at factor 1). At factor 0.5, NA scores 0.25 and ranks second."""
    activated = [(NA, "fact", 0.5, 1), (NB, "fact", 0.45, 1)]
    seeds_out = {NA: DECISION_ONE_ID, NB: DECISION_ONE_ID}
    cosines = {NA: None, NB: 0.30, DECISION_ONE_ID: 0.30}

    today, *_ = await _run(gate=QUANTILE, activated=activated, seeds_out=seeds_out, cosines=cosines)
    assert list(_sa_rows(today)) == [str(NA), str(NB)]
    assert _sa_rows(today)[str(NA)]["edge_weight"] == 0.5

    tr, *_ = await _run(
        gate={**QUANTILE, "unembedded_factor": 0.5}, activated=activated, seeds_out=seeds_out, cosines=cosines,
    )
    rows = _sa_rows(tr)
    assert list(rows) == [str(NB), str(NA)]
    assert rows[str(NA)]["edge_weight"] == pytest.approx(0.25)
    assert rows[str(NB)]["edge_weight"] == 0.45


# ---------------------------------------------------------------------------
# Associative edges only
# ---------------------------------------------------------------------------


def _settings(**over: Any) -> Settings:
    """Settings defaults with no .env file (decay 0.5, max depth 2)."""
    return Settings(_env_file=None, **over)


def _only(**over: Any) -> Settings:
    return _settings(spreading_associative_edges_only=True, **over)


async def _act(session, agent: str, seeds, settings, **kwargs: Any) -> dict[UUID, tuple[float, int]]:
    """``node -> (activation, depth)`` over the whole (limit=100) result."""
    rows = await spreading_activation_search(session, agent, seeds, settings, limit=100, **kwargs)
    return {r[0]: (r[2], r[3]) for r in rows}


def test_associative_only_predicate():
    """Associative edges, minus lineage, negative and temporal relations; the
    associative flag does not change it."""
    pred = spreading_exclusion_sql("e.", associative_only=True)
    assert pred == spreading_exclusion_sql("e.", allow_associative=True, associative_only=True)
    assert associative_edge_sql("e.") in pred
    assert "e.relation NOT IN ('contradicts', 'happened_before', 'supersedes')" in pred
    assert spreading_exclusion_sql("e.") != pred


async def test_associative_only_sql():
    """The traversal and the fan degree count use the associative predicate;
    no multiplier fragment is added. Off, the SQL is the snapshot."""
    pred = spreading_exclusion_sql("e.", associative_only=True)
    sql = await render(_only(spreading_associative_edges_enabled=True))
    assert f"AND {pred}" in sql
    assert "assoc_mult" not in sql
    fan = await render(_only(spreading_fan_normalisation_enabled=True))
    assert f"AND {spreading_exclusion_sql('e2.', associative_only=True)}" in fan

    snapshot = SNAPSHOT.read_text(encoding="utf-8")
    base = _settings()
    lacking = SimpleNamespace(**{
        k: getattr(base, k) for k in Settings.model_fields if k != "spreading_associative_edges_only"})
    assert await render(lacking) == snapshot
    assert await render(_settings(spreading_associative_edges_only=False)) == snapshot


@pytest.mark.postgres_only
async def test_associative_only_traversal(session):
    """S has a co_mention, a co_occurred, an inferred, a contradicts and a
    disguised (supersedes carrying co_mention) edge. Only the two associative
    ones are crossed, at weight x decay (no multiplier), and from N only the
    associative edge leads on (depth 2)."""
    agent = f"assoc-only-{uuid4().hex[:8]}"
    s, n, c, a, x, y, neg, disguised = (uuid4() for _ in range(8))
    await insert_edge(session, agent, s, n, weight=0.9, method="co_mention")
    await insert_edge(session, agent, s, c, weight=0.8, relation="co_occurred", method="co_occurrence")
    await insert_edge(session, agent, s, a, weight=1.0)
    await insert_edge(session, agent, s, neg, weight=1.0, relation="contradicts", method="co_mention")
    await insert_edge(session, agent, s, disguised, weight=1.0, relation="supersedes", method="co_mention")
    await insert_edge(session, agent, n, x, weight=1.0)
    await insert_edge(session, agent, n, y, weight=1.0, relation="co_occurred", method="co_occurrence")
    seeds = [(s, "decision", 0.8)]

    default = await _act(session, agent, seeds, _settings())
    assert set(default) == {s, a}, "non-vacuous: today the inferred edge is crossed, the associative ones not"

    for settings in (_only(), _only(spreading_associative_edges_enabled=True, spreading_associative_weight_mult=0.3)):
        on = await _act(session, agent, seeds, settings)
        assert set(on) == {s, n, c, y}
        assert on[n] == (pytest.approx(0.8 * 0.9 * 0.5), 1)
        assert on[c] == (pytest.approx(0.8 * 0.8 * 0.5), 1)
        assert on[y] == (pytest.approx(0.8 * 0.9 * 0.5 * 1.0 * 0.5), 2)
