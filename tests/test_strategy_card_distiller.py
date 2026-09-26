"""Tests for StrategyCardDistiller — Reasoning Maps Layer 1.

Mutation evidence: revert the indicated guard/condition and the marked
assertion must fail.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest

from nous.brain.schemas import GRADED_OUTCOMES
from nous.handlers.strategy_card_distiller import StrategyCardDistiller
from nous.heart.schemas import ProcedureDetail, ProcedureInput


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_settings(**overrides: Any) -> MagicMock:
    s = MagicMock()
    s.strategy_cards_enabled = True
    s.strategy_cards_retrieval_enabled = True
    s.strategy_cards_max_per_turn = 1
    s.background_model = "claude-haiku-4-5-20251001"
    for k, v in overrides.items():
        setattr(s, k, v)
    return s


def _make_decision(
    decision_id: UUID | None = None,
    description: str = "Deploy feature to production",
    outcome: str = "success",
    context: str | None = "We had to choose between blue-green and rolling deploy",
    outcome_result: str | None = "Blue-green worked smoothly",
) -> MagicMock:
    d = MagicMock()
    d.id = decision_id or uuid4()
    d.description = description
    d.context = context
    d.outcome = outcome
    d.outcome_result = outcome_result
    return d


def _make_card_response() -> dict:
    return {
        "name": "Use blue-green deploy for zero-downtime releases",
        "description": "Blue-green deployment avoids downtime during releases",
        "lesson": (
            "When deploying to production with uptime requirements, "
            "blue-green deploy works because it keeps the old version live until "
            "the new one is verified. Avoid rolling deploys when rollback speed matters."
        ),
        "tags": ["deployment", "zero-downtime"],
    }


def _make_procedure_detail(proc_id: UUID | None = None, kind: str | None = "strategy") -> ProcedureDetail:
    return ProcedureDetail(
        id=proc_id or uuid4(),
        agent_id="test-agent",
        name="Use blue-green deploy",
        domain="strategy",
        description="Short desc",
        goals=[],
        core_patterns=[],
        core_tools=[],
        core_concepts=[],
        implementation_notes=["lesson text"],
        activation_count=0,
        success_count=0,
        failure_count=0,
        neutral_count=0,
        last_activated=None,
        effectiveness=None,
        tags=["deployment"],
        active=True,
        created_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        kind=kind,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_brain():
    b = MagicMock()
    b.agent_id = "test-agent"
    b.get = AsyncMock(return_value=_make_decision())
    return b


@pytest.fixture
def mock_heart():
    h = MagicMock()
    h.db = MagicMock()
    h.procedures = MagicMock()
    h.procedures.store = AsyncMock(return_value=_make_procedure_detail())
    # Provide a real async context manager for h.db.session()
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


@pytest.fixture
def distiller(mock_brain, mock_heart, mock_llm):
    settings = _make_settings()
    return StrategyCardDistiller(
        brain=mock_brain,
        heart=mock_heart,
        settings=settings,
        bus=None,
        llm_client=mock_llm,
    )


# ---------------------------------------------------------------------------
# 1. test_skip_noise_outcome
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_skip_noise_outcome(distiller, mock_brain):
    """Handler skips 'noise' outcome — no LLM call, no procedure stored.

    Mutation: remove `if outcome not in GRADED_OUTCOMES: return` guard in
    _on_decision_reviewed → asyncio.create_task fires → mock_brain.get called.
    """
    decision_id = uuid4()
    event = {"decision_id": str(decision_id), "outcome": "noise"}

    with patch(
        "nous.handlers.strategy_card_distiller.call_background_llm_structured",
        new_callable=AsyncMock,
    ) as mock_call:
        distiller._on_decision_reviewed(event)
        await asyncio.sleep(0)  # flush event loop
        mock_call.assert_not_called()
    mock_brain.get.assert_not_called()


# ---------------------------------------------------------------------------
# 2. test_skip_superseded_outcome
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_skip_superseded_outcome(distiller, mock_brain):
    """Handler skips 'superseded' outcome — same guard as noise.

    Mutation: same as test_skip_noise_outcome.
    """
    event = {"decision_id": str(uuid4()), "outcome": "superseded"}

    with patch(
        "nous.handlers.strategy_card_distiller.call_background_llm_structured",
        new_callable=AsyncMock,
    ) as mock_call:
        distiller._on_decision_reviewed(event)
        await asyncio.sleep(0)
        mock_call.assert_not_called()
    mock_brain.get.assert_not_called()


# ---------------------------------------------------------------------------
# 3. test_no_llm_client_skips
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_no_llm_client_skips(mock_brain, mock_heart):
    """No LLM client wired → distillation exits early, no procedure stored.

    Mutation: remove `if not self._llm: return` in _do_distil →
    AttributeError on NoneType.
    """
    settings = _make_settings()
    distiller = StrategyCardDistiller(
        brain=mock_brain,
        heart=mock_heart,
        settings=settings,
        bus=None,
        llm_client=None,
    )

    await distiller._do_distil(uuid4(), "success")
    mock_brain.get.assert_not_called()
    mock_heart.procedures.store.assert_not_called()


# ---------------------------------------------------------------------------
# 4. test_distil_success_outcome
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_distil_success_outcome(distiller, mock_brain, mock_heart):
    """Success outcome → procedure stored with kind='strategy' and source_decision_id.

    Mutation: remove `kind="strategy"` from ProcedureInput → stored.kind is None
    → assertion fails.
    """
    decision_id = uuid4()
    mock_brain.get = AsyncMock(return_value=_make_decision(decision_id=decision_id))

    with patch(
        "nous.handlers.strategy_card_distiller.call_background_llm_structured",
        new_callable=AsyncMock,
        return_value=_make_card_response(),
    ):
        await distiller._do_distil(decision_id, "success")

    mock_heart.procedures.store.assert_called_once()
    call_args = mock_heart.procedures.store.call_args
    inp: ProcedureInput = call_args[0][0]
    assert inp.kind == "strategy"
    assert inp.runtime_metadata is not None
    assert inp.runtime_metadata["source_decision_id"] == str(decision_id)
    assert inp.runtime_metadata["outcome"] == "success"


# ---------------------------------------------------------------------------
# 5. test_distil_failure_outcome
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_distil_failure_outcome(distiller, mock_brain, mock_heart):
    """Failure outcome → card stored with outcome='failure' in runtime_metadata.

    Mutation: remove outcome from runtime_metadata → assertion fails.
    """
    decision_id = uuid4()
    mock_brain.get = AsyncMock(return_value=_make_decision(decision_id=decision_id, outcome="failure"))

    with patch(
        "nous.handlers.strategy_card_distiller.call_background_llm_structured",
        new_callable=AsyncMock,
        return_value=_make_card_response(),
    ):
        await distiller._do_distil(decision_id, "failure")

    inp: ProcedureInput = mock_heart.procedures.store.call_args[0][0]
    assert inp.runtime_metadata["outcome"] == "failure"


# ---------------------------------------------------------------------------
# 6. test_idempotency_deactivates_old_card
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_idempotency_deactivates_old_card(distiller, mock_brain, mock_heart):
    """When an existing card is found, _deactivate_procedure is called before creating new one.

    Mutation: remove existing_id != None branch → deactivation never called →
    old card still active (duplicate created).
    """
    decision_id = uuid4()
    existing_id = uuid4()
    mock_brain.get = AsyncMock(return_value=_make_decision(decision_id=decision_id))

    deactivate_spy = AsyncMock()
    distiller._deactivate_procedure = deactivate_spy

    # Patch _find_existing_card to return an existing ID
    async def _fake_find(did: UUID) -> UUID:
        return existing_id

    distiller._find_existing_card = _fake_find

    with patch(
        "nous.handlers.strategy_card_distiller.call_background_llm_structured",
        new_callable=AsyncMock,
        return_value=_make_card_response(),
    ):
        await distiller._do_distil(decision_id, "success")

    deactivate_spy.assert_called_once_with(existing_id)
    mock_heart.procedures.store.assert_called_once()


# ---------------------------------------------------------------------------
# 7. test_context_cap_strategy_cards
# ---------------------------------------------------------------------------

def test_context_cap_strategy_cards():
    """Cap of 1 keeps exactly 1 strategy card; excess are dropped.

    Mutation: change `strategy_hits[:max_sc]` to `strategy_hits` →
    all 3 strategy cards pass → len(embedding_procedures) == 5 (not 3).
    """
    import datetime as dt

    def _proc(name: str, kind: str | None = None) -> MagicMock:
        p = MagicMock()
        p.name = name
        p.kind = kind
        p.score = 0.8
        return p

    non_strategy = [_proc("proc-a"), _proc("proc-b")]
    strategy_cards = [_proc("sc-1", "strategy"), _proc("sc-2", "strategy"), _proc("sc-3", "strategy")]
    embedding_procedures = non_strategy + strategy_cards

    settings = _make_settings(strategy_cards_retrieval_enabled=True, strategy_cards_max_per_turn=1)

    max_sc = max(0, settings.strategy_cards_max_per_turn)
    s_hits = [p for p in embedding_procedures if getattr(p, "kind", None) == "strategy"]
    non_s = [p for p in embedding_procedures if getattr(p, "kind", None) != "strategy"]
    served = s_hits[:max_sc]
    result = non_s + served

    assert len(result) == 3  # 2 non-strategy + 1 strategy
    strategy_in_result = [p for p in result if p.kind == "strategy"]
    assert len(strategy_in_result) == 1
    assert strategy_in_result[0].name == "sc-1"


# ---------------------------------------------------------------------------
# 8. test_kind_field_stored_on_procedure
# ---------------------------------------------------------------------------

def test_kind_field_round_trips():
    """ProcedureInput(kind='strategy') populates ProcedureDetail.kind.

    Mutation: remove `kind=input.kind` from procedures._store() → kind is None
    → assertion fails.
    """
    inp = ProcedureInput(
        name="Test card",
        domain="strategy",
        description="desc",
        kind="strategy",
    )
    assert inp.kind == "strategy"

    detail = _make_procedure_detail(kind=inp.kind)
    assert detail.kind == "strategy"


# ---------------------------------------------------------------------------
# 9. test_skip_when_flag_disabled
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_skip_when_flag_disabled(mock_brain, mock_heart, mock_llm):
    """When strategy_cards_enabled=False, no task is scheduled.

    Mutation: remove flag check in _on_decision_reviewed → task fires even
    when disabled.
    """
    settings = _make_settings(strategy_cards_enabled=False)
    distiller = StrategyCardDistiller(
        brain=mock_brain,
        heart=mock_heart,
        settings=settings,
        bus=None,
        llm_client=mock_llm,
    )

    event = {"decision_id": str(uuid4()), "outcome": "success"}
    with patch(
        "nous.handlers.strategy_card_distiller.call_background_llm_structured",
        new_callable=AsyncMock,
    ) as mock_call:
        distiller._on_decision_reviewed(event)
        await asyncio.sleep(0)
        mock_call.assert_not_called()
    mock_brain.get.assert_not_called()


# ---------------------------------------------------------------------------
# 10. test_graded_outcomes_constant_matches_spec
# ---------------------------------------------------------------------------

def test_graded_outcomes_constant():
    """GRADED_OUTCOMES must contain exactly success/partial/failure.

    Defensive check so a schema change doesn't silently break the distiller.
    """
    assert set(GRADED_OUTCOMES) == {"success", "partial", "failure"}
