"""F105 Task 6: a budget on spreading activation rows in the merged recall list.

``recall_deep`` delivers the whole merged list (no ``[:limit]`` slice, exempt
from SmartCompress), and Stage 4's own 20-row cap acts before the merge, so the
bound has to act on the merged list. With the flag on, an SA row scoring below
the ``cutline_rank``-th best non-SA score is dropped, then the ``max_rows``
best of the rest are kept. It runs after the F071 filter, so a row F071 removes
does not take a slot. Non-SA rows and the order of the survivors are untouched.

Pipeline seeds are the two mocked decisions: DECISION_ONE (score 0.75) and
DECISION_TWO (score None, so 0.0 as a result). An SA row scores its activation
x ``graph_recall_decay`` (0.7 in ``_make_settings``).
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest
from pydantic import ValidationError
from test_retrieval_pipeline import (  # noqa: E402  (test-local helper reuse)
    _make_brain,
    _make_decision_summaries,
    _make_heart,
    _make_settings,
)

from nous.api.retrieval_pipeline import (
    PipelineResult,
    _apply_spreading_budget,
    run_recall_pipeline,
)
from nous.config import Settings
from nous.observability.retrieval_trace import (
    BUDGET_TRUNCATED,
    F071_EXCLUDED,
    RENDERED,
    RetrievalTrace,
)

SA = "spreading_activation"
AT = datetime(2026, 1, 5, tzinfo=UTC)
NON_SA_SCORES = [0.95, 0.9, 0.85, 0.8, 0.7, 0.6, 0.5]
SA_SCORES = [0.8, 0.6, 0.5, 0.4, 0.3, 0.2, 0.15]
NA, NB, NC = (UUID(f"dddddddd-0000-0000-0000-00000000000{i}") for i in range(1, 4))


# ---------------------------------------------------------------------------
# _apply_spreading_budget: pure
# ---------------------------------------------------------------------------


def _row(score: float, *, sa: bool, n: int) -> PipelineResult:
    return PipelineResult(
        id=UUID(int=n), type="fact", description=f"row {n}", score=score,
        source=SA if sa else "heart",
    )


def _mix(non_sa: list[float], sa: list[float]) -> list[PipelineResult]:
    """Stage order: the non-SA rows, then the SA rows (ids 1.. and 101..)."""
    return ([_row(s, sa=False, n=i + 1) for i, s in enumerate(non_sa)]
            + [_row(s, sa=True, n=101 + i) for i, s in enumerate(sa)])


def _budget(**fields: Any) -> SimpleNamespace:
    """Settings holding the ``spreading_budget_*`` fields; on, 5 rows, no cutline."""
    values = {"enabled": True, "max_rows": 5, "cutline_rank": 0, **fields}
    return SimpleNamespace(**{f"spreading_budget_{k}": v for k, v in values.items()})


def _traced(rows: list[PipelineResult]) -> RetrievalTrace:
    tr = RetrievalTrace(query="q")
    for r in rows:
        tr.add(r.id, r.type, "graph_expanded" if r.source == SA else "heart_primary", score=r.score)
    return tr


def _dropped(tr: RetrievalTrace) -> dict[str, tuple[str, str | None]]:
    return {c["id"]: (c["disposition"], c["disposition_stage"])
            for c in tr.to_dict()["candidates"] if c["disposition"] != "unaccounted"}


def _sa_scores(rows: list[PipelineResult]) -> list[float]:
    return [r.score for r in rows if r.source == SA]


def test_budget_off_returns_input_unchanged():
    """Off (or settings without the fields) is a no-op even at max_rows 0 and a cutline."""
    for settings in (_budget(enabled=False, max_rows=0, cutline_rank=1), SimpleNamespace()):
        rows = _mix(NON_SA_SCORES, SA_SCORES)
        snapshot = list(rows)
        tr = _traced(rows)
        out = _apply_spreading_budget(rows, settings, tr)
        assert out is rows
        assert out == snapshot
        assert _dropped(tr) == {}


def test_budget_cap_keeps_top_sa_by_score():
    rows = _mix(NON_SA_SCORES, SA_SCORES)
    tr = _traced(rows)
    out = _apply_spreading_budget(rows, _budget(max_rows=3), tr)
    assert _sa_scores(out) == [0.8, 0.6, 0.5]
    assert [r for r in out if r.source != SA] == rows[:7]
    cut = [r for r in rows if r.source == SA and r.score < 0.5]
    assert len(cut) == 4
    assert _dropped(tr) == {str(r.id): (BUDGET_TRUNCATED, "spreading_budget") for r in cut}


def test_budget_cutline_rank():
    """The 5th-best non-SA score is 0.7: only the SA row at 0.8 clears it."""
    rows = _mix(NON_SA_SCORES, SA_SCORES)
    tr = _traced(rows)
    out = _apply_spreading_budget(rows, _budget(max_rows=5, cutline_rank=5), tr)
    assert _sa_scores(out) == [0.8]
    assert len(_dropped(tr)) == 6
    assert len(out) == 8


def test_budget_cutline_is_strict():
    rows = _mix(NON_SA_SCORES, [0.7, 0.69])
    tr = _traced(rows)
    out = _apply_spreading_budget(rows, _budget(max_rows=5, cutline_rank=5), tr)
    assert _sa_scores(out) == [0.7], "equal to the cutline is kept"
    assert _dropped(tr) == {str(rows[-1].id): (BUDGET_TRUNCATED, "spreading_budget")}


def test_budget_edge_sizes():
    tr = RetrievalTrace(query="q")
    assert _apply_spreading_budget([], _budget(max_rows=0, cutline_rank=3), tr) == []

    no_sa = _mix(NON_SA_SCORES, [])
    assert _apply_spreading_budget(no_sa, _budget(max_rows=0, cutline_rank=3), tr) == no_sa

    only_sa = _mix([], SA_SCORES)
    out = _apply_spreading_budget(only_sa, _budget(max_rows=2, cutline_rank=5), tr)
    assert _sa_scores(out) == [0.8, 0.6], "no non-SA row: no cutline, the cap alone"

    few_non_sa = _mix([0.95, 0.9], [0.3, 0.2])
    out = _apply_spreading_budget(few_non_sa, _budget(max_rows=5, cutline_rank=3), tr)
    assert out == few_non_sa, "2 non-SA rows, rank 3: no cutline (rank 2 would cut both)"

    rows = _mix(NON_SA_SCORES, SA_SCORES)
    out = _apply_spreading_budget(rows, _budget(max_rows=0), tr)
    assert out == rows[:7], "max_rows 0 drops every SA row"

    under_cap = _mix(NON_SA_SCORES, [0.4, 0.3])
    assert _apply_spreading_budget(under_cap, _budget(max_rows=5), tr) == under_cap


def test_budget_preserves_non_sa_order_and_identity():
    """Interleaved list: survivors keep their positions relative to each other
    and to the non-SA rows, which are the same objects in the same order."""
    n1, n2, n3, n4 = (_row(s, sa=False, n=i) for i, s in enumerate([0.5, 0.95, 0.7, 0.85], 1))
    s1, s2, s3, s4 = (_row(s, sa=True, n=100 + i) for i, s in enumerate([0.2, 0.8, 0.6, 0.15], 1))
    rows = [n1, s1, n2, s2, n3, s3, s4, n4]
    out = _apply_spreading_budget(rows, _budget(max_rows=2), _traced(rows))
    assert out == [n1, n2, s2, n3, s3, n4]
    assert all(a is b for a, b in zip([r for r in out if r.source != SA], [n1, n2, n3, n4], strict=True))


def test_budget_ties_keep_list_order():
    """Equal scores at the cap boundary: the earlier row in the list is kept."""
    rows = _mix([0.9], [0.4, 0.4, 0.4])
    out = _apply_spreading_budget(rows, _budget(max_rows=2), _traced(rows))
    assert out == rows[:3]


def test_budget_defaults_match_settings():
    """Settings without the parameter fields fall back to the Settings defaults."""
    assert Settings.model_fields["spreading_budget_max_rows"].default == 5
    assert Settings.model_fields["spreading_budget_cutline_rank"].default == 0
    rows = _mix(NON_SA_SCORES, SA_SCORES)
    out = _apply_spreading_budget(rows, SimpleNamespace(spreading_budget_enabled=True), _traced(rows))
    assert _sa_scores(out) == [0.8, 0.6, 0.5, 0.4, 0.3], "5 rows, no cutline"


def test_budget_settings_bounds(monkeypatch: pytest.MonkeyPatch):
    s = Settings(_env_file=None)
    assert (s.spreading_budget_enabled, s.spreading_budget_max_rows, s.spreading_budget_cutline_rank) == (
        False, 5, 0)
    Settings(_env_file=None, spreading_budget_max_rows=0)
    Settings(_env_file=None, spreading_budget_max_rows=20, spreading_budget_cutline_rank=20)
    for bad in (
        {"spreading_budget_max_rows": -1},
        {"spreading_budget_max_rows": 21},
        {"spreading_budget_cutline_rank": -1},
    ):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, **bad)
    monkeypatch.setenv("NOUS_SPREADING_BUDGET_ENABLED", "true")
    monkeypatch.setenv("NOUS_SPREADING_BUDGET_MAX_ROWS", "7")
    monkeypatch.setenv("NOUS_SPREADING_BUDGET_CUTLINE_RANK", "20")
    s = Settings(_env_file=None)
    assert (s.spreading_budget_enabled, s.spreading_budget_max_rows, s.spreading_budget_cutline_rank) == (
        True, 7, 20)


# ---------------------------------------------------------------------------
# The budget in the pipeline (mocked CTE)
# ---------------------------------------------------------------------------


ACTIVATED = [(NA, "fact", 0.5, 1), (NB, "fact", 0.4, 1), (NC, "fact", 0.3, 2)]


async def _run(
    *,
    budget: dict[str, Any] | None,
    activated: list[tuple[UUID, str, float, int]] = ACTIVATED,
    exclude_ids: dict[str, set[str]] | None = None,
    spreading: str = "true",
) -> tuple[list[PipelineResult], RetrievalTrace]:
    """Run the pipeline with a real trace; ``budget`` holds the
    ``spreading_budget_*`` fields (None: the settings lack them)."""
    heart = _make_heart(recall_results=[])
    brain = _make_brain(neighbors_by_node={}, contradictions=[],
                        decision_results=_make_decision_summaries())
    brain._resolve_node_descriptions = AsyncMock(
        return_value={nid: (f"row {nid}", AT) for nid, *_ in activated})
    settings = _make_settings(spreading_activation_enabled=spreading)
    for key, value in (budget or {}).items():
        setattr(settings, f"spreading_budget_{key}", value)
    tr = RetrievalTrace(query="q")
    with patch(
        "nous.brain.spreading_activation.spreading_activation_search",
        AsyncMock(return_value=list(activated)),
    ), patch(
        "nous.brain.spreading_activation.compute_graph_density", AsyncMock(return_value=5.0),
    ):
        results, _stats = await run_recall_pipeline(
            query="q", heart=heart, brain=brain, settings=settings, limit=10,
            exclude_ids=exclude_ids, trace=tr,
        )
    return results, tr


def _legs(tr: RetrievalTrace) -> dict[str, dict]:
    return {leg["name"]: leg for leg in tr.to_dict()["legs"]}


def _candidates(tr: RetrievalTrace) -> dict[str, dict]:
    return {c["id"]: c for c in tr.to_dict()["candidates"]}


def _sa_ids(results: list[PipelineResult]) -> list[UUID]:
    return [r.id for r in results if r.source == SA]


async def test_budget_off_is_identical_in_pipeline():
    """Flag off: the same rows, scores and order, and the same trace, as
    settings that predate the budget fields, even at max_rows 0."""
    (old, old_tr), (off, off_tr) = [
        await _run(budget=b) for b in (None, {"enabled": False, "max_rows": 0, "cutline_rank": 1})]
    assert [(r.id, r.type, r.score, r.source) for r in old] == [
        (r.id, r.type, r.score, r.source) for r in off]
    assert _sa_ids(off) == [NA, NB, NC], "non-vacuous: every SA row is delivered"
    old_d, off_d = old_tr.to_dict(), off_tr.to_dict()
    for key in ("legs", "candidates", "expansions", "disposition_counts"):
        assert old_d[key] == off_d[key], key


async def test_budget_in_pipeline():
    """The leg still reports what the stage produced; the cut rows carry the
    budget disposition and are counted in the leg's n_dropped."""
    results, tr = await _run(budget={"enabled": True, "max_rows": 1, "cutline_rank": 0})
    assert _sa_ids(results) == [NA]
    legs = _legs(tr)
    assert legs[SA]["n_returned"] == 3
    assert legs[SA]["n_dropped"] == 2
    assert legs[SA]["counted_in"] == "graph_expanded"
    assert legs["graph_expanded"]["n_returned"] == 3
    cands = _candidates(tr)
    assert cands[str(NA)]["disposition"] == RENDERED
    for nid in (NB, NC):
        assert (cands[str(nid)]["disposition"], cands[str(nid)]["disposition_stage"]) == (
            BUDGET_TRUNCATED, "spreading_budget")
    assert len([e for e in tr.to_dict()["expansions"] if e["stage"] == "stage4_spreading_activation"]) == 3


async def test_budget_on_without_spreading_reports_no_leg():
    """No SA row, no cut: the budget must not invent a spreading leg."""
    _results, tr = await _run(budget={"enabled": True, "max_rows": 0}, spreading="false")
    assert SA not in _legs(tr)


async def test_budget_f071_removed_row_does_not_consume_slot():
    """F071 removes NA (the top SA row) first, so the one slot goes to NB."""
    control, _ = await _run(budget={"enabled": True, "max_rows": 1})
    assert _sa_ids(control) == [NA], "without F071 the top row takes the slot"
    results, tr = await _run(budget={"enabled": True, "max_rows": 1},
                             exclude_ids={"fact": {str(NA)}})
    assert _sa_ids(results) == [NB]
    cands = _candidates(tr)
    assert cands[str(NA)]["disposition"] == F071_EXCLUDED
    assert cands[str(NC)]["disposition"] == BUDGET_TRUNCATED
    assert _legs(tr)[SA]["n_dropped"] == 1
