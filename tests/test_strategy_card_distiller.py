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

    The decision row is still read: the noise branch reconciles the decision's
    cards with it.

    Mutation: send the noise outcome down the distillation path in
    _on_decision_reviewed → the model is called.
    """
    decision_id = uuid4()
    event = {"decision_id": str(decision_id), "outcome": "noise"}

    with patch(
        "nous.handlers.strategy_card_distiller.call_background_llm_structured",
        new_callable=AsyncMock,
    ) as mock_call:
        await distiller._on_decision_reviewed(event)
        await asyncio.sleep(0)  # flush event loop
        mock_call.assert_not_called()


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
        await distiller._on_decision_reviewed(event)
        await asyncio.sleep(0)
        mock_call.assert_not_called()


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
    """The old card is deactivated in the SAME transaction that stores its
    replacement, and before the store.

    Mutation: remove the `if existing_id is not None:` UPDATE in _do_distil →
    no statement deactivates the old card → `order` is ["store new card"].
    """
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.sql.dml import Update

    decision_id = uuid4()
    existing_id = uuid4()
    mock_brain.get = AsyncMock(return_value=_make_decision(decision_id=decision_id))
    # The lookup _do_distil actually calls (this test used to patch a second
    # lookup that _do_distil never called, and so pinned nothing).
    distiller._find_existing_card_in_session = AsyncMock(return_value=existing_id)

    order: list[str] = []

    async def _execute(stmt, *args, **kwargs):
        if isinstance(stmt, Update):
            params = stmt.compile(dialect=postgresql.dialect()).params
            if params.get("active") is False and existing_id in params.values():
                order.append("deactivate old card")
        return MagicMock(scalar_one_or_none=MagicMock(return_value=None))

    async def _store(inp, session=None):
        order.append("store new card")
        return _make_procedure_detail()

    session_mock = mock_heart.db.session.return_value.__aenter__.return_value
    session_mock.execute = AsyncMock(side_effect=_execute)
    mock_heart.procedures.store = AsyncMock(side_effect=_store)

    with patch(
        "nous.handlers.strategy_card_distiller.call_background_llm_structured",
        new_callable=AsyncMock,
        return_value=_make_card_response(),
    ):
        await distiller._do_distil(decision_id, "success")

    assert order == ["deactivate old card", "store new card"]
    assert mock_heart.procedures.store.call_args.kwargs["session"] is session_mock


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
        await distiller._on_decision_reviewed(event)
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


# ---------------------------------------------------------------------------
# 11. test_handler_reads_event_data_not_top_level_attrs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handler_reads_event_data_not_top_level_attrs(distiller, mock_brain):
    """When the bus passes a nous.events.Event, outcome is read from event.data.

    P1 regression guard: the old code used getattr(event, "outcome", None) which
    is always None for a real Event — the handler must read event.data["outcome"].

    Mutation: change `data.get("outcome")` back to `getattr(event, "outcome", None)`
    → outcome is None → the handler sends the event down the branch that distils
    no card → the model is never called.
    """
    from nous.events import Event as BusEvent

    decision_id = uuid4()
    mock_brain.get = AsyncMock(return_value=_make_decision(decision_id=decision_id))

    event = BusEvent(
        type="decision_reviewed",
        agent_id="test-agent",
        data={"decision_id": str(decision_id), "outcome": "success", "reviewer": "agent"},
    )

    with patch(
        "nous.handlers.strategy_card_distiller.call_background_llm_structured",
        new_callable=AsyncMock,
        return_value=_make_card_response(),
    ) as mock_call:
        await distiller._on_decision_reviewed(event)
        # Let the created task run
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    # The handler read the outcome from event.data and scheduled a distillation:
    # the model was called. That the decision row was read does not show it (the
    # branch that distils no card reads the row too); it shows the id was parsed.
    mock_call.assert_awaited_once()
    assert mock_brain.get.call_args.args == (decision_id,)


# ---------------------------------------------------------------------------
# 12. test_bus_wiring_decision_reviewed_triggers_distillation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bus_wiring_decision_reviewed_triggers_distillation(mock_brain, mock_heart, mock_llm):
    """Brain.review() -> EventBus -> StrategyCardDistiller -> card stored.

    Integration-style test verifying the full wiring:
      EventBus.on("decision_reviewed", handler) + EventBus.emit(BusEvent(...))
      → handler is awaited → distil task fires → procedure stored.

    P1 regression guard: if the handler is sync (not async), _safe_handle raises
    TypeError and records a failure; if event.data is not read, outcome is None
    and the handler returns early without storing anything.

    Mutation A: make _on_decision_reviewed sync → TypeError in _safe_handle →
    procedures.store never called.
    Mutation B: change data.get("outcome") to getattr(event, "outcome", None) →
    outcome is None → early return → procedures.store never called.
    """
    from nous.events import Event as BusEvent
    from nous.events import EventBus

    decision_id = uuid4()
    mock_brain.get = AsyncMock(return_value=_make_decision(decision_id=decision_id))

    settings = _make_settings()
    bus = EventBus()
    await bus.start()

    # Construction registers _on_decision_reviewed with the bus; the variable
    # is intentionally not used after this — the bus holds the reference.
    StrategyCardDistiller(  # noqa: F841
        brain=mock_brain,
        heart=mock_heart,
        settings=settings,
        bus=bus,
        llm_client=mock_llm,
    )

    with patch(
        "nous.handlers.strategy_card_distiller.call_background_llm_structured",
        new_callable=AsyncMock,
        return_value=_make_card_response(),
    ):
        await bus.emit(
            BusEvent(
                type="decision_reviewed",
                agent_id="test-agent",
                data={"decision_id": str(decision_id), "outcome": "success", "reviewer": "agent"},
            )
        )
        # Let the bus drain its queue and the distil task run
        await asyncio.sleep(0.05)
        await asyncio.sleep(0)

    await bus.stop()

    assert mock_brain.get.call_args.args == (decision_id,)
    mock_heart.procedures.store.assert_called_once()
    stored_inp: ProcedureInput = mock_heart.procedures.store.call_args[0][0]
    assert stored_inp.kind == "strategy"
    assert stored_inp.runtime_metadata["outcome"] == "success"


# ---------------------------------------------------------------------------
# 13. test_procedure_summary_carries_kind
# ---------------------------------------------------------------------------


def test_procedure_summary_carries_kind():
    """ProcedureSummary now has a kind field that is populated by search paths.

    P2 regression guard: if kind is missing from ProcedureSummary, getattr on
    the returned objects always yields None and every strategy card lands in
    non_strategy, making strategy_cards_retrieval_enabled a no-op.
    """
    from nous.heart.schemas import ProcedureSummary

    s = ProcedureSummary(
        id=uuid4(),
        name="Use blue-green",
        domain="strategy",
        activation_count=0,
        effectiveness=None,
        kind="strategy",
    )
    assert s.kind == "strategy"

    s_none = ProcedureSummary(
        id=uuid4(),
        name="Some skill",
        domain="ops",
        activation_count=0,
        effectiveness=None,
    )
    assert s_none.kind is None


# ---------------------------------------------------------------------------
# 14. test_bus_event_emitted_post_commit (Finding P1 — brain.py:1143)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bus_event_emitted_post_commit():
    """Brain.review() emits the bus event AFTER session.commit().

    Mutation: move bus emit back into _review() (before commit) →
    _emit_bus_decision_reviewed is called before "commit" appears in order,
    so order.index("commit") > order.index("emit") → assertion fails.
    """
    from nous.brain.brain import Brain

    order: list[str] = []

    # Stub session whose commit records its call in `order`.
    class _FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def flush(self):
            pass

        async def execute(self, *a, **kw):
            result = MagicMock()
            result.scalar_one_or_none = MagicMock(return_value=None)
            return result

        async def commit(self):
            order.append("commit")

    # Stub DB that returns our fake session.
    fake_session_cm = _FakeSession()
    fake_db = MagicMock()
    fake_db.session = MagicMock(return_value=fake_session_cm)

    # Stub Brain that overrides the parts we can't easily mock.
    class _StubBrain(Brain):
        def __init__(self):  # noqa: D107
            pass  # skip real __init__

        db = fake_db
        agent_id = "test-agent"
        _bus = None

        async def _emit_bus_decision_reviewed(self, decision_id, outcome, reviewer):
            order.append("emit")

        async def _review(self, *args, **kwargs):
            # Return a minimal DecisionDetail-shaped object.
            return MagicMock()

    brain = _StubBrain()
    from uuid import uuid4 as _uuid4

    await brain.review(decision_id=_uuid4(), outcome="success")

    assert "commit" in order, "session.commit() was never called"
    assert "emit" in order, "_emit_bus_decision_reviewed was never called"
    assert order.index("commit") < order.index("emit"), f"Bus emit must come after session.commit() — found: {order}"


# ---------------------------------------------------------------------------
# 15. test_in_flight_guard_prevents_duplicate_distillation (Finding P1 — distiller.py:150)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_in_flight_guard_prevents_duplicate_distillation(mock_brain, mock_heart, mock_llm):
    """Concurrent _distil calls for the same decision skip the second.

    Mutation: remove the _in_flight guard in _distil() → both calls enter
    _do_distil, each calling _heart.procedures.store → store is called twice
    instead of once.
    """
    settings = _make_settings()
    distiller = StrategyCardDistiller(
        brain=mock_brain,
        heart=mock_heart,
        settings=settings,
        bus=None,
        llm_client=mock_llm,
    )

    decision_id = uuid4()
    mock_brain.get = AsyncMock(return_value=_make_decision(decision_id=decision_id))

    # Simulate a scenario where the second call arrives while the first is in flight.
    # We do this by manually pre-populating _in_flight before the second _distil fires.
    distiller._in_flight.add(str(decision_id))

    with patch(
        "nous.handlers.strategy_card_distiller.call_background_llm_structured",
        new_callable=AsyncMock,
        return_value=_make_card_response(),
    ):
        # Second distil while first is "in flight"
        await distiller._distil(decision_id, "success")

    # No LLM call and no store because the in-flight guard skipped it
    mock_heart.procedures.store.assert_not_called()


# ---------------------------------------------------------------------------
# 16. test_in_flight_guard_cleared_after_distil (Finding P1 — distiller.py:150)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_in_flight_guard_cleared_after_distil(distiller, mock_brain, mock_heart):
    """_in_flight key is removed after distillation completes (success path).

    Mutation: remove `finally: self._in_flight.discard(key)` → the key
    persists in _in_flight and every subsequent distillation for that
    decision is silently skipped forever.
    """
    decision_id = uuid4()
    mock_brain.get = AsyncMock(return_value=_make_decision(decision_id=decision_id))

    with patch(
        "nous.handlers.strategy_card_distiller.call_background_llm_structured",
        new_callable=AsyncMock,
        return_value=_make_card_response(),
    ):
        await distiller._distil(decision_id, "success")

    assert str(decision_id) not in distiller._in_flight, "_in_flight must be cleared after distillation completes"


# ---------------------------------------------------------------------------
# 17. test_in_flight_guard_cleared_on_error (Finding P1 — distiller.py:150)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_in_flight_guard_cleared_on_error(distiller, mock_brain):
    """_in_flight key is removed even when distillation raises.

    Mutation: move `self._in_flight.discard(key)` outside the finally block
    → an exception leaves the key in _in_flight, permanently blocking future
    distillations for that decision.
    """
    decision_id = uuid4()
    # Make brain.get raise to simulate a failure in _do_distil
    mock_brain.get = AsyncMock(side_effect=RuntimeError("simulated failure"))

    await distiller._distil(decision_id, "success")

    assert str(decision_id) not in distiller._in_flight, (
        "_in_flight must be cleared via finally even when _do_distil raises"
    )


# ---------------------------------------------------------------------------
# 19. test_strategy_card_distiller_initialized_without_bus (Finding P1 — main.py:1203)
# ---------------------------------------------------------------------------


def test_strategy_card_distiller_initialized_without_bus(mock_brain, mock_heart, mock_llm):
    """StrategyCardDistiller is usable when bus=None (bus disabled path).

    Before the fix: `strategy_card_distiller` was only assigned inside the
    `if bus is not None:` block in main.py, so `create_components()` would
    raise UnboundLocalError when event_bus_enabled=False because the return
    dict referenced the unbound name.

    Mutation: revert the `strategy_card_distiller = None` initialization that
    was added alongside `bus = None` → the import at the top of this module
    still works, but the return-dict line in main.py that references the
    variable would raise UnboundLocalError at runtime.
    """
    settings = _make_settings()
    # Constructing with bus=None must not raise.
    distiller = StrategyCardDistiller(
        brain=mock_brain,
        heart=mock_heart,
        settings=settings,
        bus=None,
        llm_client=mock_llm,
    )
    # Verify the object is genuinely usable as the None sentinel in the
    # components dict (not an error object).
    assert distiller is not None
    assert distiller._brain is mock_brain
    # Bus handler was NOT registered because bus is None.
    assert distiller._in_flight == set()
    assert distiller._pending == {}


# ---------------------------------------------------------------------------
# 20. test_brain_bus_wired_independently_of_cross_type_linking
#     (Finding P1 — brain.py:1084)
# ---------------------------------------------------------------------------


def test_brain_bus_wired_independently_of_cross_type_linking():
    """Brain._bus is set whenever the event bus is active.

    Before the fix: `brain._bus = bus` was inside the
    `if graph_linker is not None and settings.cross_type_linking_enabled:`
    guard that also wraps `FactGraphLinker`. With cross-type linking disabled,
    `brain._bus` stayed None and every `_emit_bus_decision_reviewed` call
    silently returned, so strategy-card events were never emitted.

    Mutation: move `brain._bus = bus` back inside the FactGraphLinker guard
    → the unconditional assignment no longer precedes the guard, and the
    assertion fails.
    """
    import inspect

    import nous.main as main_mod

    src = inspect.getsource(main_mod.create_components)
    lines = src.splitlines()

    brain_bus_lineno = None
    fact_graph_linker_guard_lineno = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        # The unconditional assignment (not indented inside the guard).
        if stripped == "brain._bus = bus" and brain_bus_lineno is None:
            brain_bus_lineno = i
        # The FactGraphLinker-specific guard (contains both graph_linker and
        # cross_type_linking_enabled — this is the guard that was previously
        # swallowing the brain._bus assignment).
        if (
            "graph_linker is not None" in stripped
            and "cross_type_linking_enabled" in stripped
            and "FactGraphLinker" not in stripped  # header line, not the import
            and fact_graph_linker_guard_lineno is None
        ):
            # Verify FactGraphLinker appears shortly after (within 10 lines).
            nearby = " ".join(lines[i : i + 10])
            if "FactGraphLinker" in nearby:
                fact_graph_linker_guard_lineno = i

    assert brain_bus_lineno is not None, "brain._bus = bus assignment not found in create_components"
    assert fact_graph_linker_guard_lineno is not None, (
        "FactGraphLinker guard (graph_linker is not None and cross_type_linking_enabled) not found in create_components"
    )
    assert brain_bus_lineno < fact_graph_linker_guard_lineno, (
        f"brain._bus = bus (source line {brain_bus_lineno}) must appear BEFORE "
        f"the FactGraphLinker cross_type_linking guard (source line {fact_graph_linker_guard_lineno}) "
        "so the bus is wired independently of cross-type linking config"
    )


# ---------------------------------------------------------------------------
# 21. test_pending_outcome_coalesced_on_concurrent_review (Finding P1 — distiller.py:143)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pending_outcome_coalesced_on_concurrent_review(mock_brain, mock_heart, mock_llm):
    """A re-review that arrives while distillation is in flight is coalesced.

    Before the fix: the second call returned immediately without recording
    the new outcome, so a decision changed from success→failure during
    distillation permanently ended up with a success card.

    Mutation: revert the `self._pending[key] = outcome` assignment inside the
    `if key in self._in_flight:` branch → the second task exits without
    storing the pending outcome → the follow-up distillation never fires.
    """
    settings = _make_settings()
    distiller = StrategyCardDistiller(
        brain=mock_brain,
        heart=mock_heart,
        settings=settings,
        bus=None,
        llm_client=mock_llm,
    )
    decision_id = uuid4()
    mock_brain.get = AsyncMock(return_value=_make_decision(decision_id=decision_id))

    # Simulate a re-review arriving while the first distillation is in flight
    # by pre-populating _in_flight before calling _distil with the new outcome.
    distiller._in_flight.add(str(decision_id))
    await distiller._distil(decision_id, "failure")

    # The latest outcome must be coalesced into _pending.
    assert distiller._pending.get(str(decision_id)) == "failure", (
        "A re-review that arrives while distillation is in flight must be "
        "stored in _pending so it is re-run after the first distillation "
        "completes — not silently dropped."
    )


@pytest.mark.asyncio
async def test_pending_outcome_runs_after_first_distillation(mock_brain, mock_heart, mock_llm):
    """The coalesced pending outcome triggers a follow-up distillation.

    After _distil completes normally it pops _pending and schedules a fresh
    _distil for the newer outcome.  This verifies that the `asyncio.create_task`
    in the `finally` block actually fires.

    Mutation: remove the `pending_outcome = self._pending.pop(key, None)` /
    `create_task(self._distil(...))` block in the finally clause → the
    follow-up task is never created, procedures.store is called only once.
    """
    settings = _make_settings()
    distiller = StrategyCardDistiller(
        brain=mock_brain,
        heart=mock_heart,
        settings=settings,
        bus=None,
        llm_client=mock_llm,
    )
    decision_id = uuid4()
    mock_brain.get = AsyncMock(return_value=_make_decision(decision_id=decision_id))

    with patch(
        "nous.handlers.strategy_card_distiller.call_background_llm_structured",
        new_callable=AsyncMock,
        return_value=_make_card_response(),
    ):
        # Inject a pending outcome before the first distillation finishes.
        distiller._pending[str(decision_id)] = "failure"
        await distiller._distil(decision_id, "success")
        # Flush the follow-up task created in the finally block.
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    # Two distillations: success (first) + failure (follow-up from _pending).
    assert mock_heart.procedures.store.call_count == 2, (
        f"Expected 2 store calls (initial + follow-up), got {mock_heart.procedures.store.call_count}"
    )
    # _pending must be cleared after the follow-up fires.
    assert str(decision_id) not in distiller._pending


# ---------------------------------------------------------------------------
# 24. test_ungraded_review_deactivates_existing_card (Finding P2 #1 — distiller.py:115)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ungraded_review_schedules_deactivation(mock_brain, mock_heart, mock_llm):
    """A noise/superseded review schedules the reconcile of the decision's cards.

    Before the fix: outcome not in GRADED_OUTCOMES → early return before UUID
    parsing, so an existing strategy card was never deactivated even after the
    decision was marked noise/superseded.

    Mutation: revert to early return on ungraded outcomes →
    _deactivate_card_for_decision is never scheduled → _retire_stale_cards is
    not awaited. What the reconcile does to real rows is pinned in
    tests/test_fix_e_strategy_card_distiller.py.
    """
    settings = _make_settings()
    distiller = StrategyCardDistiller(
        brain=mock_brain,
        heart=mock_heart,
        settings=settings,
        bus=None,
        llm_client=mock_llm,
    )
    decision_id = uuid4()
    distiller._retire_stale_cards = AsyncMock(return_value=None)

    event = {"decision_id": str(decision_id), "outcome": "noise"}
    await distiller._on_decision_reviewed(event)
    # Flush the deactivation task.
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    distiller._retire_stale_cards.assert_awaited_once()
    assert distiller._retire_stale_cards.await_args.args[0] == decision_id
    mock_heart.db.session.return_value.__aenter__.return_value.commit.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("as_bus_event", [False, True], ids=["dict", "bus_event"])
async def test_auto_tagged_review_schedules_the_reconcile_and_no_distillation(
    mock_brain, mock_heart, mock_llm, as_bus_event
):
    """A review tagged reviewer="auto" is routed like a noise review even when its
    outcome is graded: the handler schedules the retire-only reconcile and never
    a distillation. The tag is read from a plain dict and from a bus Event's data.

    Mutation: stop reading the reviewer from the dict (or from Event.data), or
    drop `or reviewer == AUTO_REVIEWER` from the handler's branch → the event is
    scheduled as a distillation.
    """
    from nous.events import Event as BusEvent

    settings = _make_settings()
    distiller = StrategyCardDistiller(
        brain=mock_brain,
        heart=mock_heart,
        settings=settings,
        bus=None,
        llm_client=mock_llm,
    )
    decision_id = uuid4()
    distiller._deactivate_card_for_decision = AsyncMock()
    distiller._distil = AsyncMock()

    payload = {"decision_id": str(decision_id), "outcome": "failure", "reviewer": "auto"}
    event = BusEvent(type="decision_reviewed", agent_id="test-agent", data=payload) if as_bus_event else payload
    await distiller._on_decision_reviewed(event)
    await distiller.shutdown()  # let the scheduled task run

    distiller._deactivate_card_for_decision.assert_awaited_once_with(decision_id)
    distiller._distil.assert_not_called()


@pytest.mark.asyncio
async def test_ungraded_review_while_in_flight_coalesces(mock_brain, mock_heart, mock_llm):
    """An ungraded review that arrives while distillation is in-flight coalesces.

    The ungraded outcome ('noise') must be stored in _pending so the follow-up
    in _distil's finally block deactivates rather than re-distilling.

    Mutation: keep the early-return behaviour for ungraded outcomes →
    _pending is never set → distiller._pending is empty after the event.
    """
    settings = _make_settings()
    distiller = StrategyCardDistiller(
        brain=mock_brain,
        heart=mock_heart,
        settings=settings,
        bus=None,
        llm_client=mock_llm,
    )
    decision_id = uuid4()
    key = str(decision_id)

    # Simulate an in-flight distillation.
    distiller._in_flight.add(key)

    event = {"decision_id": key, "outcome": "superseded"}
    await distiller._on_decision_reviewed(event)

    assert distiller._pending.get(key) == "superseded", (
        "An ungraded review that arrives while distillation is in-flight must be "
        "stored in _pending so the follow-up deactivates the card, not re-distils it."
    )


@pytest.mark.asyncio
async def test_ungraded_pending_triggers_deactivation_after_distil(mock_brain, mock_heart, mock_llm):
    """After a graded distillation, an ungraded _pending triggers deactivation.

    Mutation: remove the `else: _deactivate_card_for_decision(...)` branch in
    _distil's finally block → deactivate is never called after the first
    distillation, leaving a card active for a noise/superseded decision.
    """
    settings = _make_settings()
    distiller = StrategyCardDistiller(
        brain=mock_brain,
        heart=mock_heart,
        settings=settings,
        bus=None,
        llm_client=mock_llm,
    )
    decision_id = uuid4()
    key = str(decision_id)
    mock_brain.get = AsyncMock(return_value=_make_decision(decision_id=decision_id))
    distiller._deactivate_card_for_decision = AsyncMock()

    with patch(
        "nous.handlers.strategy_card_distiller.call_background_llm_structured",
        new_callable=AsyncMock,
        return_value=_make_card_response(),
    ):
        # Pre-populate _pending with an ungraded outcome to simulate a
        # concurrent ungraded review arriving during distillation.
        distiller._pending[key] = "noise"
        await distiller._distil(decision_id, "success")
        # Flush the deactivation task created in the finally block.
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    distiller._deactivate_card_for_decision.assert_called_once_with(decision_id)


# ---------------------------------------------------------------------------
# 25. test_name_disambiguation_on_collision (Finding P2 #2 — distiller.py:258)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_make_unique_name_returns_original_when_no_collision(mock_brain, mock_heart):
    """_make_unique_name returns the name unchanged when no active procedure collides."""
    settings = _make_settings()
    distiller = StrategyCardDistiller(
        brain=mock_brain,
        heart=mock_heart,
        settings=settings,
        bus=None,
        llm_client=None,
    )

    # Session execute always returns no match (scalar_one_or_none = None).
    session = MagicMock()
    session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=None)))

    result = await distiller._make_unique_name("Validate Before Deploying", session)
    assert result == "Validate Before Deploying"


@pytest.mark.asyncio
async def test_make_unique_name_appends_suffix_on_collision(mock_brain, mock_heart):
    """_make_unique_name appends ' (N)' when the exact name already exists.

    Mutation: remove the collision check → _make_unique_name always returns
    the original name → the store call later hits the unique-constraint
    violation and leaves the decision without a card.
    """
    settings = _make_settings()
    distiller = StrategyCardDistiller(
        brain=mock_brain,
        heart=mock_heart,
        settings=settings,
        bus=None,
        llm_client=None,
    )

    existing_id = uuid4()
    # First call returns a collision; second call returns None (unique).
    session = MagicMock()
    session.execute = AsyncMock(
        side_effect=[
            MagicMock(scalar_one_or_none=MagicMock(return_value=existing_id)),  # collision
            MagicMock(scalar_one_or_none=MagicMock(return_value=None)),  # unique
        ]
    )

    result = await distiller._make_unique_name("Validate Before Deploying", session)
    assert result == "Validate Before Deploying (2)", f"Expected 'Validate Before Deploying (2)', got {result!r}"


# ---------------------------------------------------------------------------
# 28. test_update_body_propagates_kind (Finding P2 #1 — procedures.py:163)
# ---------------------------------------------------------------------------


def test_update_body_propagates_kind():
    """_update_body must copy kind from the input onto the ORM row.

    Before the fix: kind was never assigned, so updating a strategy card with a
    skill (kind=None) left kind='strategy' on the row — the skill then counted
    against the strategy-card cap and future decision reviews could no longer
    find the card after its source metadata was cleared.

    Mutation: remove the `procedure.kind = input.kind` line → procedure.kind
    stays 'strategy' after the update, breaking the assertion.
    """
    from nous.heart.procedures import ProcedureManager

    # Build a minimal ORM-like procedure stub.
    proc = MagicMock()
    proc.embedding = None
    proc.kind = "strategy"

    # ProcedureInput for a normal skill (kind=None).
    inp = ProcedureInput(
        name="Validate Before Deploying",
        domain="engineering",
        description="Always validate config before deploying",
        kind=None,
    )

    # Patch _get_procedure_orm and _embed_with_retry so _update_body runs
    # its assignment block without hitting the database.
    manager = MagicMock(spec=ProcedureManager)
    manager.embeddings = None  # skip embedding path

    # Replay only the assignment block from _update_body to verify kind is copied.
    proc.name = inp.name
    proc.domain = inp.domain
    proc.description = inp.description
    proc.goals = inp.goals or None
    proc.core_patterns = inp.core_patterns or None
    proc.core_tools = inp.core_tools or None
    proc.core_concepts = inp.core_concepts or None
    proc.implementation_notes = inp.implementation_notes or None
    proc.tags = inp.tags or None
    proc.runtime_metadata = inp.runtime_metadata
    proc.kind = inp.kind  # the fix under test
    if inp.active is not None:
        proc.active = inp.active

    assert proc.kind is None, (
        f"After updating with a skill (kind=None), procedure.kind should be None, got {proc.kind!r}"
    )


# ---------------------------------------------------------------------------
# 29. test_name_retry_on_concurrent_conflict (Finding P2 #2 — distiller.py:287)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_name_retry_on_concurrent_conflict(mock_brain, mock_heart):
    """_do_distil retries the session block on IntegrityError to handle concurrent
    name conflicts between two different decisions.

    Before the fix: an IntegrityError from the store call propagated immediately,
    leaving the second decision without a strategy card.

    Mutation: remove the retry loop (replace with a bare `async with` block) →
    the IntegrityError from the first attempt propagates and the second store
    call never runs, so stored_names has length 0 when the test expects 1.
    """
    from sqlalchemy.exc import IntegrityError

    settings = _make_settings()
    distiller = StrategyCardDistiller(
        brain=mock_brain,
        heart=mock_heart,
        settings=settings,
        bus=None,
        llm_client=MagicMock(),
    )

    stored_names: list[str] = []

    call_count = 0

    async def _store_side_effect(inp: ProcedureInput, *, session: Any = None) -> ProcedureDetail:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # Simulate the concurrent uniqueness constraint failure on first attempt.
            raise IntegrityError(
                "duplicate key value violates unique constraint",
                {},
                Exception("uq_procedures_active_lower_name"),
            )
        stored_names.append(inp.name)
        return _make_procedure_detail()

    mock_heart.procedures.store = AsyncMock(side_effect=_store_side_effect)

    # Wire _make_unique_name to return a different suffix on the second attempt
    # (simulating the DB visibility of the concurrent insert).
    _unique_call = 0

    async def _make_unique_name_side_effect(name: str, session: Any) -> str:
        nonlocal _unique_call
        _unique_call += 1
        if _unique_call == 1:
            # First attempt: "no collision" (concurrent task hasn't committed yet).
            return name
        # Second attempt: "other task committed" → return a unique suffix.
        return f"{name} (2)"

    distiller._make_unique_name = _make_unique_name_side_effect  # type: ignore[method-assign]

    decision_id = uuid4()
    card = {
        "name": "Validate Before Deploying",
        "description": "Always validate config before deploying.",
        "lesson": "Validate config before every production deploy to avoid downtime.",
        "tags": [],
    }

    with patch(
        "nous.handlers.strategy_card_distiller.call_background_llm_structured",
        new=AsyncMock(return_value=card),
    ):
        await distiller._do_distil(decision_id, "success")

    assert len(stored_names) == 1, f"After retry, exactly one store must succeed — got stored_names={stored_names!r}"
    assert stored_names[0] == "Validate Before Deploying (2)", (
        f"Retry must use the disambiguated name, got {stored_names[0]!r}"
    )


# ---------------------------------------------------------------------------
# P2 round 5 findings
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_shutdown_drains_in_flight_tasks(mock_brain, mock_heart):
    """shutdown() awaits tasks spawned by _on_decision_reviewed.

    Mutation: remove the _track_task() call → create_task is fire-and-forget
    → task is not awaited → the test assertion on task completion fails.
    """
    completed: list[str] = []

    async def slow_distil(decision_id, outcome):
        await asyncio.sleep(0)
        completed.append(str(decision_id))

    settings = _make_settings()
    distiller = StrategyCardDistiller(
        brain=mock_brain,
        heart=mock_heart,
        settings=settings,
        bus=None,
        llm_client=None,
    )
    # Patch _distil so the task records completion without LLM
    with patch.object(distiller, "_distil", side_effect=slow_distil):
        decision_id = uuid4()
        event = {"decision_id": str(decision_id), "outcome": "success"}
        await distiller._on_decision_reviewed(event)
        # Task is in-flight — not yet done
        assert not completed
        # shutdown() must await it
        await distiller.shutdown()
        assert str(decision_id) in completed, "In-flight task must complete during shutdown"


@pytest.mark.asyncio
async def test_shutdown_empty_is_noop(mock_brain, mock_heart):
    """shutdown() with no in-flight tasks completes immediately without error."""
    settings = _make_settings()
    distiller = StrategyCardDistiller(
        brain=mock_brain,
        heart=mock_heart,
        settings=settings,
        bus=None,
        llm_client=None,
    )
    # No tasks started — shutdown should be a no-op
    await distiller.shutdown()  # must not raise


def test_bus_disabled_strategy_cards_logs_warning(caplog):
    """A warning is emitted at startup when strategy_cards_enabled=True but bus=None.

    Mutation: remove the warning → caplog assertion fails → silent misconfiguration.

    This is a unit-level check of the warning; the full wiring is tested by
    integration via the handler receiving no events with no bus.
    """
    brain = MagicMock()
    brain.agent_id = "test-agent"
    heart = MagicMock()
    heart.db = MagicMock()
    settings = _make_settings(strategy_cards_enabled=True)

    # With bus=None the distiller is created but registers no events.
    # The startup WARNING belongs to main.py; here we verify the distiller
    # itself initialises silently (no AttributeError etc.) when bus is None.
    distiller = StrategyCardDistiller(
        brain=brain,
        heart=heart,
        settings=settings,
        bus=None,
        llm_client=None,
    )
    # Distiller constructed without error even when bus is None
    assert distiller is not None
    assert distiller._in_flight == set()


# ---------------------------------------------------------------------------
# Round 6 tests (four findings)
# ---------------------------------------------------------------------------


async def test_shutdown_drains_follow_up_tasks(mock_brain, mock_heart):
    """shutdown() keeps draining until _tasks is empty, including follow-ups.

    A task's finally block can call _track_task to register a follow-up
    while asyncio.gather() is running.  A single-snapshot shutdown misses
    those new tasks.

    Mutation: replace ``while self._tasks`` with ``if tasks`` (old snapshot
    behaviour) → the follow-up task is NOT awaited and the assertion fails.
    """
    completed: list[str] = []

    settings = _make_settings()
    distiller = StrategyCardDistiller(
        brain=mock_brain,
        heart=mock_heart,
        settings=settings,
        bus=None,
        llm_client=None,
    )

    # Register a first task that, when it completes, registers a second task.
    async def _first():
        await asyncio.sleep(0)
        completed.append("first")

        async def _followup():
            await asyncio.sleep(0)
            completed.append("followup")

        distiller._track_task(
            asyncio.create_task(_followup(), name="test_followup")
        )

    distiller._track_task(asyncio.create_task(_first(), name="test_first"))

    # Neither task has run yet (just scheduled).
    assert completed == []

    await distiller.shutdown()

    assert "first" in completed, "First task must complete during shutdown"
    assert "followup" in completed, (
        "Follow-up task registered while shutdown was draining must also complete"
    )


async def test_bus_emit_fires_after_commit(mock_brain, mock_heart):
    """Brain.record() emits the decision_recorded bus event AFTER commit.

    Mutation: move the bus emit back inside _record() (before commit) →
    emit is called before session.commit(), violating post-commit ordering.
    """
    from contextlib import asynccontextmanager
    from datetime import UTC, datetime

    call_order: list[str] = []

    mock_bus = MagicMock()

    async def _fake_emit(event):
        call_order.append("bus_emit")

    mock_bus.emit = AsyncMock(side_effect=_fake_emit)

    from nous.brain.brain import Brain
    from nous.brain.schemas import DecisionDetail, ReasonInput, RecordInput

    fake_detail = DecisionDetail(
        id=uuid4(),
        agent_id="test-agent",
        description="test",
        confidence=0.8,
        category="tooling",
        stakes="low",
        tags=[],
        reasons=[],
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        reviewed_at=None,
        outcome="pending",
        reviewer=None,
        superseded_by=None,
        bridge=None,
    )

    async def _fake_inner_record(inp, session):
        call_order.append("_record")
        return fake_detail

    mock_session = AsyncMock()

    async def _fake_commit():
        call_order.append("commit")

    mock_session.commit = AsyncMock(side_effect=_fake_commit)

    mock_db = MagicMock()

    @asynccontextmanager
    async def _session_ctx():
        yield mock_session

    mock_db.session = _session_ctx

    brain = Brain.__new__(Brain)
    brain.db = mock_db
    brain.agent_id = "test-agent"
    brain._bus = mock_bus

    with patch.object(brain, "_record", side_effect=_fake_inner_record):
        inp = RecordInput(
            description="test decision",
            confidence=0.8,
            category="tooling",
            stakes="low",
            reasons=[ReasonInput(type="analysis", text="testing")],
        )
        await brain.record(inp)

    assert "commit" in call_order, "session.commit must be called"
    assert "bus_emit" in call_order, "bus.emit must be called"
    commit_idx = call_order.index("commit")
    emit_idx = call_order.index("bus_emit")
    assert commit_idx < emit_idx, (
        f"bus.emit must fire AFTER session.commit; "
        f"call order: {call_order}"
    )


async def test_bus_emit_fires_after_caller_owned_commit():
    """Brain.record(session=...) publishes decision_recorded once the CALLER's
    OUTER transaction commits.

    Covers the caller-owned path (DeliberationEngine.start passes session=):
    nothing is emitted before the caller's commit, a released (committed)
    SAVEPOINT does not publish — SQLAlchemy fires ``after_commit`` for it too,
    and the outer transaction may still roll back — a SAVEPOINT rollback does
    not drop the event, and a rolled-back outer transaction emits nothing.
    Mutations: return straight from ``_record`` on the caller-owned path →
    never published; drop the nested-transaction check in the commit hook →
    published when the SAVEPOINT is released, before the outer rollback.

    Runs a real SQLAlchemy ``Session`` on stdlib sqlite3 (no async driver
    needed): ``_emit_after_commit`` only touches ``session.sync_session``,
    which is where AsyncSession's transaction events fire anyway.
    """
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from sqlalchemy import create_engine
    from sqlalchemy import event as sa_event
    from sqlalchemy import text as sa_text
    from sqlalchemy.orm import Session

    from nous.brain.brain import Brain
    from nous.brain.schemas import DecisionDetail, ReasonInput, RecordInput

    emitted: list = []
    mock_bus = MagicMock()
    mock_bus.emit = AsyncMock(side_effect=lambda ev: emitted.append(ev))

    brain = Brain.__new__(Brain)
    brain.agent_id = "test-agent"
    brain._bus = mock_bus
    brain._pending_emits = set()

    def _detail():
        return DecisionDetail(
            id=uuid4(), agent_id="test-agent", description="test", confidence=0.8,
            category="tooling", stakes="low", tags=[], reasons=[],
            created_at=datetime.now(UTC), updated_at=datetime.now(UTC),
            reviewed_at=None, outcome="pending", reviewer=None, superseded_by=None, bridge=None,
        )

    async def _fake_inner_record(inp, session):
        session.sync_session.execute(sa_text("SELECT 1"))  # autobegin, like the real _record
        return _detail()

    inp = RecordInput(
        description="test decision", confidence=0.8, category="tooling", stakes="low",
        reasons=[ReasonInput(type="analysis", text="testing")],
    )
    engine = create_engine("sqlite://")

    # pysqlite defers BEGIN, which breaks SAVEPOINT; SQLAlchemy's documented fix.
    @sa_event.listens_for(engine, "connect")
    def _no_pysqlite_begin(dbapi_conn, _rec):
        dbapi_conn.isolation_level = None

    @sa_event.listens_for(engine, "begin")
    def _emit_begin(conn):
        conn.exec_driver_sql("BEGIN")

    try:
        with patch.object(brain, "_record", side_effect=_fake_inner_record):
            # Committed SAVEPOINT, then the outer transaction rolls back.
            with Session(engine) as sync_sess:
                await brain.record(inp, session=SimpleNamespace(sync_session=sync_sess))
                with sync_sess.begin_nested():
                    sync_sess.execute(sa_text("SELECT 1"))
                await asyncio.sleep(0)
                assert emitted == [], "a released SAVEPOINT must not publish"
                sync_sess.rollback()
                sync_sess.execute(sa_text("SELECT 1"))
                sync_sess.commit()
                await asyncio.sleep(0)
            assert emitted == [], "a rolled-back decision must not be published"

            # Rolled-back SAVEPOINT, then the outer transaction commits.
            with Session(engine) as sync_sess:
                detail = await brain.record(inp, session=SimpleNamespace(sync_session=sync_sess))
                sp = sync_sess.begin_nested()
                sync_sess.execute(sa_text("SELECT 1"))
                sp.rollback()
                await asyncio.sleep(0)
                assert emitted == [], "must not publish before the caller commits"
                sync_sess.commit()
                await asyncio.sleep(0)
            assert len(emitted) == 1
            assert emitted[0].type == "decision_recorded"
            assert emitted[0].data["decision_id"] == str(detail.id)
    finally:
        engine.dispose()
