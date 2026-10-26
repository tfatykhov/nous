"""F105 Task 8: spreading activation re-scores the recall candidates it reaches.

With ``spreading_rescore_existing_enabled`` on, Stage 4 no longer excludes the
existing candidates from the CTE: it passes them as ``rescore_ids``, and their
activation (``spreading_aggregation``'s, as for any row) comes back through
``rescore_out`` without entering the main list or its LIMIT. Before the
``rerank_by_score`` sort each such candidate's score becomes
``min(1, score * (1 + beta * a))``: a boost only, so a direct hit is never
demoted, and seeds and spreading rows are never touched. Flag off, the CTE's SQL
and every result are today's (tests/test_spreading_sql_identity.py).

CTE graphs use bare UUIDs (``graph_edges`` has no foreign key to the node
tables) and the Settings defaults: decay 0.5, max depth 2. Order assertions use
distinct activations: the CTE has no id tie-break (R9).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from spreading_v2_helpers import SEEDS, SNAPSHOT, insert_edge, render
from test_retrieval_pipeline import (  # noqa: E402  (test-local helper reuse)
    DECISION_ONE_ID,
    DECISION_TWO_ID,
    EPISODE_ID,
    FACT_ID,
    _make_brain,
    _make_decision_summaries,
    _make_heart,
    _make_recall_results,
    _make_settings,
)

from nous.api.retrieval_pipeline import (
    PipelineResult,
    _apply_spreading_rescore,
    run_recall_pipeline,
)
from nous.brain.spreading_activation import spreading_activation_search
from nous.config import Settings
from nous.observability.retrieval_trace import RetrievalTrace

SA = "spreading_activation"
AT = datetime(2026, 1, 5, tzinfo=UTC)
MODES = ("max", "sat", "noisy_or")
NA = UUID("eeeeeeee-0000-0000-0000-000000000001")


def _settings(**over: Any) -> Settings:
    return Settings(_env_file=None, **over)


def _agent() -> str:
    return f"rescore-{uuid4().hex[:8]}"


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


def test_rescore_settings(monkeypatch: pytest.MonkeyPatch):
    s = _settings()
    assert (s.spreading_rescore_existing_enabled, s.spreading_rescore_beta) == (False, 0.15)
    _settings(spreading_rescore_beta=1.0)
    for bad in (0.0, -0.1, 1.1):
        with pytest.raises(ValidationError):
            _settings(spreading_rescore_beta=bad)
    monkeypatch.setenv("NOUS_SPREADING_RESCORE_EXISTING_ENABLED", "true")
    monkeypatch.setenv("NOUS_SPREADING_RESCORE_BETA", "0.05")
    s = Settings(_env_file=None)
    assert (s.spreading_rescore_existing_enabled, s.spreading_rescore_beta) == (True, 0.05)


# ---------------------------------------------------------------------------
# spreading_activation_search: rescore_ids / rescore_out (real Postgres)
# ---------------------------------------------------------------------------


async def test_rescore_off_sql_is_the_snapshot():
    """None or an empty set: today's statement, byte for byte."""
    snapshot = SNAPSHOT.read_text(encoding="utf-8")
    assert await render(_settings(), rescore_ids=None, rescore_out={}) == snapshot
    assert await render(_settings(), rescore_ids=set(), rescore_out={}) == snapshot
    seed_only = {SEEDS[1][0]}
    assert await render(_settings(), rescore_ids=seed_only, rescore_out={}) == snapshot, "seeds are dropped first"
    on = await render(_settings(), rescore_ids={uuid4()}, rescore_out={})
    assert "UNION ALL" in on and "is_rescore" in on


@pytest.mark.postgres_only
async def test_rescore_off_search_identical(session):
    agent = _agent()
    s, c, n = uuid4(), uuid4(), uuid4()
    await insert_edge(session, agent, s, c, weight=1.0)
    await insert_edge(session, agent, s, n, weight=0.8)
    seeds = [(s, "decision", 0.8)]
    baseline = await spreading_activation_search(session, agent, seeds, _settings(), limit=100, exclude_ids={s})
    rescore_out: dict[UUID, float] = {}
    off = await spreading_activation_search(
        session, agent, seeds, _settings(), limit=100, exclude_ids={s},
        rescore_ids=None, rescore_out=rescore_out,
    )
    assert off == baseline
    assert [r[0] for r in off] == [c, n], "non-vacuous: both rows are returned"
    assert rescore_out == {}


async def _window_graph(session, agent: str) -> tuple[UUID, UUID, UUID, list[UUID]]:
    """Seed S; candidates C1, C2 (w=1.0, 0.95) above new nodes N1, N2, N3 (w=0.9, 0.8, 0.7)."""
    s, c1, c2 = uuid4(), uuid4(), uuid4()
    ns = [uuid4() for _ in range(3)]
    await insert_edge(session, agent, s, c1, weight=1.0)
    await insert_edge(session, agent, s, c2, weight=0.95)
    for n, w in zip(ns, (0.9, 0.8, 0.7), strict=True):
        await insert_edge(session, agent, s, n, weight=w)
    return s, c1, c2, ns


@pytest.mark.postgres_only
@pytest.mark.parametrize("mode", MODES)
async def test_rescore_out_values(session, mode: str):
    """C1 and C2 come back through rescore_out with their activation and are
    absent from the main list, whose window they do not use: limit 2 gives
    exactly [N1, N2], as with C1 and C2 excluded."""
    agent = _agent()
    s, c1, c2, (n1, n2, n3) = await _window_graph(session, agent)
    seeds = [(s, "decision", 0.8)]
    settings = _settings(spreading_aggregation=mode)
    rescore_out: dict[UUID, float] = {}
    seed_out: dict[UUID, UUID] = {}
    parent_out: dict[UUID, UUID] = {}
    main = await spreading_activation_search(
        session, agent, seeds, settings, limit=2, exclude_ids={s},
        rescore_ids={c1, c2}, rescore_out=rescore_out, seed_out=seed_out, parent_out=parent_out,
    )
    assert [r[0] for r in main] == [n1, n2]
    assert [r[2] for r in main] == sorted((r[2] for r in main), reverse=True)
    assert [r[2] for r in main] == [pytest.approx(0.36), pytest.approx(0.32)]
    assert rescore_out == {c1: pytest.approx(0.4), c2: pytest.approx(0.38)}
    assert seed_out == parent_out == {n1: s, n2: s}, "seed_out and parent_out describe the main rows only"
    excluded = await spreading_activation_search(
        session, agent, seeds, settings, limit=2, exclude_ids={s, c1, c2})
    assert main == excluded
    assert n3 not in {r[0] for r in main}


@pytest.mark.postgres_only
async def test_rescore_without_exclude_ids(session):
    """Without exclude_ids the main branch still leaves out the rescore ids;
    the seed, not excluded, is a main row as it is today."""
    agent = _agent()
    s, c1, c2, (n1, n2, n3) = await _window_graph(session, agent)
    rescore_out: dict[UUID, float] = {}
    main = await spreading_activation_search(
        session, agent, [(s, "decision", 0.8)], _settings(), limit=100,
        rescore_ids={c1, c2}, rescore_out=rescore_out,
    )
    assert [r[0] for r in main] == [s, n1, n2, n3]
    assert [r[2] for r in main] == sorted((r[2] for r in main), reverse=True)
    assert set(rescore_out) == {c1, c2}


@pytest.mark.postgres_only
async def test_rescore_id_also_excluded_is_rescored(session):
    """An id in both sets (a Path A decision that is also a brain hit) is a
    rescore row: it gets its activation and stays out of the main list."""
    agent = _agent()
    s, c1, c2, _ns = await _window_graph(session, agent)
    rescore_out: dict[UUID, float] = {}
    main = await spreading_activation_search(
        session, agent, [(s, "decision", 0.8)], _settings(), limit=100,
        exclude_ids={s, c1}, rescore_ids={c1, c2}, rescore_out=rescore_out,
    )
    assert rescore_out == {c1: pytest.approx(0.4), c2: pytest.approx(0.38)}
    assert not {c1, c2} & {r[0] for r in main}


@pytest.mark.postgres_only
async def test_rescore_excludes_seeds(session):
    """A seed passed in rescore_ids is not a rescore id: it never appears in
    rescore_out, even when another seed reaches it."""
    agent = _agent()
    s1, s2, c = uuid4(), uuid4(), uuid4()
    await insert_edge(session, agent, s1, s2, weight=1.0)
    await insert_edge(session, agent, s1, c, weight=1.0)
    seeds = [(s1, "decision", 0.8), (s2, "decision", 0.1)]
    rescore_out: dict[UUID, float] = {}
    await spreading_activation_search(
        session, agent, seeds, _settings(), limit=100, exclude_ids={s1, s2},
        rescore_ids={s1, s2, c}, rescore_out=rescore_out,
    )
    assert rescore_out == {c: pytest.approx(0.4)}


@pytest.mark.postgres_only
@pytest.mark.parametrize(("mode", "expected"), [("max", 0.4), ("sat", 0.7), ("noisy_or", 1 - 0.6 * 0.7)])
async def test_rescore_honours_aggregation(session, mode: str, expected: float):
    """Seeds S1 (0.8) and S2 (0.6) both reach C (w=1.0): its rescore activation
    is the mode's combination, as a main row's would be."""
    agent = _agent()
    s1, s2, c = uuid4(), uuid4(), uuid4()
    await insert_edge(session, agent, s1, c)
    await insert_edge(session, agent, s2, c)
    rescore_out: dict[UUID, float] = {}
    await spreading_activation_search(
        session, agent, [(s1, "decision", 0.8), (s2, "decision", 0.6)],
        _settings(spreading_aggregation=mode), limit=100, exclude_ids={s1, s2},
        rescore_ids={c}, rescore_out=rescore_out,
    )
    assert rescore_out == {c: pytest.approx(expected)}


# ---------------------------------------------------------------------------
# _apply_spreading_rescore: pure
# ---------------------------------------------------------------------------


def _row(n: int, score: float | None, source: str = "heart") -> PipelineResult:
    return PipelineResult(
        id=UUID(int=n), type="fact", description=f"row {n}", score=score, source=source,  # type: ignore[arg-type]
    )


def _traced(rows: list[PipelineResult]) -> RetrievalTrace:
    tr = RetrievalTrace(query="q")
    for r in rows:
        tr.add(r.id, r.type, "graph_expanded" if r.source == SA else "heart_primary", score=r.score)
    return tr


def _mutations(tr: RetrievalTrace) -> dict[str, list[dict]]:
    return {c["id"]: c["mutations"] for c in tr.to_dict()["candidates"] if c["mutations"]}


def test_rescore_formula():
    rows = [_row(1, 0.5), _row(2, 0.98)]
    tr = _traced(rows)
    out = _apply_spreading_rescore(rows, {UUID(int=1): 0.4, UUID(int=2): 1.0}, 0.15, tr)
    assert [r.score for r in out] == [pytest.approx(0.53), 1.0]
    assert _mutations(tr)[str(UUID(int=1))] == [
        {"stage": "spreading_rescore", "score_before": 0.5, "score_after": pytest.approx(0.53), "reason": None}]


def test_rescore_only_raises_and_skips_seeds():
    """Every score is >= its input. Unchanged, and the same object: the seed
    (Stage 4 gives a seed no entry: test_rescore_excludes_seeds and
    test_rescore_in_pipeline), the spreading row although it has an entry, a
    candidate with no activation, one with a zero activation, a score of None,
    and a score the adjacency boost already put above 1, which ``min(1, ...)``
    must not pull down."""
    seed, sa_row, raised, no_act, zero, none_score, above_one = (
        _row(1, 0.9), _row(2, 0.6, SA), _row(3, 0.5), _row(4, 0.45), _row(5, 0.4), _row(6, None), _row(7, 1.1))
    rows = [seed, sa_row, raised, no_act, zero, none_score, above_one]
    rescore = {UUID(int=2): 0.9, UUID(int=3): 0.4, UUID(int=5): 0.0, UUID(int=6): 0.5, UUID(int=7): 0.5}
    tr = _traced(rows)
    out = _apply_spreading_rescore(rows, rescore, 0.15, tr)
    assert [r.id for r in out] == [r.id for r in rows], "positions are kept: the sort is the caller's"
    for before, after in zip(rows, out, strict=True):
        assert (after.score or 0.0) >= (before.score or 0.0)
    for unchanged in (seed, sa_row, no_act, zero, none_score, above_one):
        assert out[rows.index(unchanged)] is unchanged
    assert out[2].score == pytest.approx(0.53)
    assert set(_mutations(tr)) == {str(raised.id)}


def test_rescore_empty_inputs():
    tr = RetrievalTrace(query="q")
    assert _apply_spreading_rescore([], {UUID(int=1): 0.5}, 0.15, tr) == []
    rows = [_row(1, 0.5)]
    assert _apply_spreading_rescore(rows, {}, 0.15, tr) == rows


# ---------------------------------------------------------------------------
# The rescore in the pipeline (mocked CTE)
# ---------------------------------------------------------------------------


async def _run(
    rescore: dict[str, Any] | None,
    *,
    rescore_out: dict[UUID, float] | None = None,
    resolved: bool = True,
    rerank_by_score: bool = True,
) -> tuple[list[PipelineResult], RetrievalTrace, dict[str, Any]]:
    """Heart: FACT (0.9, a top-3 fact seed) and EPISODE (0.8, rescorable);
    brain: DECISION_ONE and DECISION_TWO (seeds, and ``seen_ids``). The CTE
    activates NA and fills ``rescore_out`` when asked. ``rescore`` holds the
    ``spreading_rescore_*`` fields (None: the settings lack them). Returns the
    results, the trace and the search's keyword arguments."""
    heart = _make_heart(recall_results=_make_recall_results())
    brain = _make_brain(neighbors_by_node={}, contradictions=[], decision_results=_make_decision_summaries())
    brain._resolve_node_descriptions = AsyncMock(return_value={NA: ("spread row", AT)} if resolved else {})
    settings = _make_settings(spreading_activation_enabled="true")
    for key, value in (rescore or {}).items():
        setattr(settings, f"spreading_rescore_{key}", value)
    seen: dict[str, Any] = {}

    async def fake_search(*_a: Any, **kwargs: Any) -> list[tuple[UUID, str, float, int]]:
        seen.update(kwargs)
        if kwargs.get("rescore_out") is not None:
            kwargs["rescore_out"].update(
                rescore_out if rescore_out is not None else {EPISODE_ID: 1.0, FACT_ID: 1.0})
        return [(NA, "fact", 0.5, 1)]

    tr = RetrievalTrace(query="q")
    with patch(
        "nous.brain.spreading_activation.spreading_activation_search", AsyncMock(side_effect=fake_search),
    ), patch(
        "nous.brain.spreading_activation.compute_graph_density", AsyncMock(return_value=5.0),
    ):
        results, _stats = await run_recall_pipeline(
            query="q", heart=heart, brain=brain, settings=settings, limit=10,
            rerank_by_score=rerank_by_score, trace=tr,
        )
    return results, tr, seen


ON = {"existing_enabled": True, "beta": 0.15}


def _scores(results: list[PipelineResult]) -> list[tuple[UUID, float, str]]:
    return [(r.id, r.score, r.source) for r in results]


async def test_rescore_off_is_identical_in_pipeline():
    """Flag off: today's exclusion (seeds, brain hits and candidates), no
    rescore arguments, and the same rows, scores, order and trace as settings
    that predate the fields."""
    runs = [await _run(r) for r in (None, {**ON, "existing_enabled": False})]
    for _results, _tr, kwargs in runs:
        assert kwargs["exclude_ids"] == {DECISION_ONE_ID, DECISION_TWO_ID, FACT_ID, EPISODE_ID}
        assert "rescore_ids" not in kwargs and "rescore_out" not in kwargs
    (old, old_tr, _), (off, off_tr, _) = runs
    assert _scores(old) == _scores(off)
    assert [r.id for r in off][:2] == [FACT_ID, EPISODE_ID]
    old_d, off_d = old_tr.to_dict(), off_tr.to_dict()
    for key in ("legs", "candidates", "expansions", "disposition_counts"):
        assert old_d[key] == off_d[key], key


async def test_rescore_in_pipeline():
    """EPISODE (0.8, activation 1.0) becomes 0.92 and moves past FACT (0.9);
    FACT is a seed, so the entry the CTE mock wrongly gives it is ignored."""
    results, tr, kwargs = await _run(ON)
    assert kwargs["exclude_ids"] == {DECISION_ONE_ID, DECISION_TWO_ID, FACT_ID}
    assert kwargs["rescore_ids"] == {EPISODE_ID}
    by_id = {r.id: r for r in results}
    assert by_id[EPISODE_ID].score == pytest.approx(0.92)
    assert by_id[FACT_ID].score == 0.9
    assert [r.id for r in results][:2] == [EPISODE_ID, FACT_ID]
    assert _mutations(tr) == {str(EPISODE_ID): [
        {"stage": "spreading_rescore", "score_before": 0.8, "score_after": pytest.approx(0.92), "reason": None}]}
    assert by_id[NA].source == SA, "the spreading row is still appended"


async def test_rescore_keeps_stage_order_without_rerank():
    results, _tr, _kw = await _run(ON, rerank_by_score=False)
    assert [r.id for r in results][:2] == [FACT_ID, EPISODE_ID]
    assert {r.id: r.score for r in results}[EPISODE_ID] == pytest.approx(0.92)


async def test_rescore_skipped_when_spreading_falls_back():
    """No spreading row resolves, so Stage 4 falls back to 1-hop: the CTE run
    was abandoned, and so is its rescore."""
    results, tr, kwargs = await _run(ON, resolved=False)
    assert "rescore_ids" in kwargs, "non-vacuous: the CTE was asked"
    assert {r.id: r.score for r in results}[EPISODE_ID] == 0.8
    assert _mutations(tr) == {}


async def test_rescore_defaults_match_settings():
    """Settings without beta use the Settings default."""
    assert Settings.model_fields["spreading_rescore_beta"].default == 0.15
    results, _tr, _kw = await _run({"existing_enabled": True}, rescore_out={EPISODE_ID: 0.5})
    assert {r.id: r.score for r in results}[EPISODE_ID] == pytest.approx(0.8 * (1 + 0.15 * 0.5))
