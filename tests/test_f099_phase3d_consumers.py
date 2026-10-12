"""F099 Phase 3d: a lineage grade is evidence for autonomy, not a strategy."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from test_procedure_learner import _build_learner

from nous.brain.schemas import DecisionDetail
from nous.config import Settings
from nous.handlers.decision_reviewer import AUTO_REVIEWER, INTENTION_REVIEWER, NON_DISTILLING_REVIEWERS
from nous.handlers.strategy_card_distiller import StrategyCardDistiller

# ---- the distiller doubles (copied from tests/test_strategy_card_distiller.py) ----------------------------------


def _make_settings(**overrides: Any) -> MagicMock:
    s = MagicMock()
    s.strategy_cards_enabled = True
    s.strategy_cards_retrieval_enabled = True
    s.strategy_cards_max_per_turn = 1
    s.background_model = "claude-haiku-4-5-20251001"
    for k, v in overrides.items():
        setattr(s, k, v)
    return s


@pytest.fixture
def mock_brain():
    b = MagicMock()
    b.agent_id = "test-agent"
    return b


@pytest.fixture
def mock_heart():
    h = MagicMock()
    h.db = MagicMock()
    h.procedures = MagicMock()
    session_cm = MagicMock()
    session_cm.__aenter__ = AsyncMock(return_value=MagicMock())
    session_cm.__aexit__ = AsyncMock(return_value=None)
    session_cm.__aenter__.return_value.execute = AsyncMock(
        return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=None))
    )
    session_cm.__aenter__.return_value.commit = AsyncMock()
    h.db.session = MagicMock(return_value=session_cm)
    return h


@pytest.fixture
def mock_llm():
    return MagicMock()


def _distiller(brain, heart, llm) -> StrategyCardDistiller:
    return StrategyCardDistiller(brain=brain, heart=heart, settings=_make_settings(), bus=None, llm_client=llm)


# ---- the distiller ------------------------------------------------------------------------------------------------


def test_the_non_distilling_reviewers():
    assert NON_DISTILLING_REVIEWERS == frozenset({AUTO_REVIEWER, INTENTION_REVIEWER}) == frozenset({"auto", "f099"})


@pytest.mark.asyncio
@pytest.mark.parametrize("as_bus_event", [False, True], ids=["dict", "bus_event"])
async def test_an_f099_review_schedules_the_reconcile_and_no_distillation(
    mock_brain, mock_heart, mock_llm, as_bus_event
):
    """Event path. Mutation: drop INTENTION_REVIEWER from NON_DISTILLING_REVIEWERS and the event is scheduled
    as a distillation."""
    from nous.events import Event as BusEvent

    distiller = _distiller(mock_brain, mock_heart, mock_llm)
    decision_id = uuid4()
    distiller._deactivate_card_for_decision = AsyncMock()
    distiller._distil = AsyncMock()
    payload = {"decision_id": str(decision_id), "outcome": "success", "reviewer": "f099"}
    await distiller._on_decision_reviewed(
        BusEvent(type="decision_reviewed", agent_id="a", data=payload) if as_bus_event else payload
    )
    await distiller.shutdown()
    distiller._deactivate_card_for_decision.assert_awaited_once_with(decision_id)
    distiller._distil.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("reviewer", ["auto", "f099"])
async def test_the_row_check_keeps_no_card_for_a_heuristic_or_lineage_grade(mock_brain, mock_heart, mock_llm, reviewer):
    """A decision graded `success` by `auto` or `f099` keeps no card: `_retire_stale_cards` returns None.
    The `f099` case fails on the base (the decision is returned, so a card may be minted).
    Mutation: revert the row check to `== AUTO_REVIEWER`."""
    distiller = _distiller(mock_brain, mock_heart, mock_llm)
    decision_id = uuid4()
    mock_brain.get = AsyncMock(
        return_value=DecisionDetail(
            id=decision_id,
            agent_id="test-agent",
            description="Deploy feature to production",
            confidence=0.8,
            category="tooling",
            stakes="low",
            tags=[],
            reasons=[],
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
            reviewed_at=datetime.now(UTC),
            outcome="success",
            outcome_result="Blue-green worked smoothly",
            reviewer=reviewer,
            superseded_by=None,
            bridge=None,
        )
    )
    session = MagicMock()
    session.execute = AsyncMock(return_value=MagicMock(rowcount=0))
    assert await distiller._retire_stale_cards(decision_id, session) is None


# ---- the procedure learner ----------------------------------------------------------------------------------------


def _learning(**flags) -> Settings:
    return Settings(
        _env_file=None,
        result_inbox_enabled=True,
        intentions_enabled=True,
        continuation_enabled=True,
        procedure_learning_enabled=True,
        **flags,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(("flags", "excluded"), [({"intention_calibration_enabled": True}, True), ({}, False)])
async def test_the_procedure_learners_decision_windows_leave_out_arrival_decisions(flags, excluded):
    """Both list_decisions calls (the success window and the partial window) carry the keyword with the flag on
    and do not carry it with the flag off (PIN). A wiring assertion: the filter itself is pinned on a real
    database by test_f099_phase3d_exclusion.py::test_a_reviewed_linked_decision_is_excluded_from_the_graded_window_too.
    Mutation: drop one spread and its call's assertion fails."""
    learner, brain, _heart, _embeddings, _llm = _build_learner(settings=_learning(**flags))
    brain.list_decisions.side_effect = [([], 0), ([], 0)]
    await learner.run_sleep_learning()
    calls = brain.list_decisions.await_args_list
    assert [call.kwargs["outcome"] for call in calls] == ["success", "partial"]
    for call in calls:
        assert ("exclude_intention_arrivals" in call.kwargs) is excluded
        if excluded:
            assert call.kwargs["exclude_intention_arrivals"] is True
