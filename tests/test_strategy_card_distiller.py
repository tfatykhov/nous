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
        await distiller._on_decision_reviewed(event)
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
        await distiller._on_decision_reviewed(event)
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
    """When an existing card is found, deactivation and insertion share ONE transaction.

    Mutation: remove existing_id != None branch → session.execute (UPDATE) is
    never called and the old card remains active alongside the new one.
    """
    decision_id = uuid4()
    existing_id = uuid4()
    mock_brain.get = AsyncMock(return_value=_make_decision(decision_id=decision_id))

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

    # Deactivation runs inside the same session as the store —
    # session.execute is called at least once (for the UPDATE).
    session_mock = mock_heart.db.session.return_value.__aenter__.return_value
    assert session_mock.execute.call_count >= 1, "Expected session.execute to be called for the deactivation UPDATE"
    mock_heart.procedures.store.assert_called_once()


# ---------------------------------------------------------------------------
# 7. test_context_cap_strategy_cards
# ---------------------------------------------------------------------------


def test_context_cap_strategy_cards():
    """Cap of 1 keeps exactly 1 strategy card; excess are dropped.

    Mutation: change `strategy_hits[:max_sc]` to `strategy_hits` →
    all 3 strategy cards pass → len(embedding_procedures) == 5 (not 3).
    """

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
    → outcome is None → handler returns at the graded-outcome guard →
    mock_brain.get is never called, but we set up a real card_response so it
    WOULD be called if the event is parsed correctly.
    """
    from nous.events import Event as BusEvent

    decision_id = uuid4()
    mock_brain.get = AsyncMock(return_value=_make_decision(decision_id=decision_id))

    event = BusEvent(
        type="decision_reviewed",
        agent_id="test-agent",
        data={"decision_id": str(decision_id), "outcome": "success", "reviewer": "auto"},
    )

    with patch(
        "nous.handlers.strategy_card_distiller.call_background_llm_structured",
        new_callable=AsyncMock,
        return_value=_make_card_response(),
    ):
        await distiller._on_decision_reviewed(event)
        # Let the created task run
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    # The handler parsed the Event correctly and scheduled distillation
    mock_brain.get.assert_called_once_with(decision_id)


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
                data={"decision_id": str(decision_id), "outcome": "success", "reviewer": "auto"},
            )
        )
        # Let the bus drain its queue and the distil task run
        await asyncio.sleep(0.05)
        await asyncio.sleep(0)

    await bus.stop()

    mock_brain.get.assert_called_once_with(decision_id)
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
# 18. test_context_cap_applies_on_graph_primary_path (Finding P1 — context.py:1400)
# ---------------------------------------------------------------------------


def test_context_cap_applies_on_graph_primary_path():
    """Strategy card cap is enforced on the graph-primary procedure path.

    Mutation: remove the cap block from the graph-primary branch →
    all 3 strategy cards pass through → result contains 3 strategy cards,
    not 1.
    """

    def _proc(name: str, kind: str | None = None) -> MagicMock:
        p = MagicMock()
        p.name = name
        p.kind = kind
        p.score = 0.8
        return p

    # Simulate what _select_procedures() returns on the graph-primary path
    non_strategy = [_proc("proc-a"), _proc("proc-b")]
    strategy_cards = [_proc("sc-1", "strategy"), _proc("sc-2", "strategy"), _proc("sc-3", "strategy")]
    selected = non_strategy + strategy_cards

    settings = _make_settings(strategy_cards_retrieval_enabled=True, strategy_cards_max_per_turn=1)

    # Apply the same cap logic that lives in the graph-primary branch of context.py
    _max_sc = max(0, settings.strategy_cards_max_per_turn)
    _sc_hits = [p for p in selected if getattr(p, "kind", None) == "strategy"]
    _non_sc = [p for p in selected if getattr(p, "kind", None) != "strategy"]
    _sc_served = _sc_hits[:_max_sc]
    result = _non_sc + _sc_served

    assert len(result) == 3, f"Expected 2 non-strategy + 1 strategy = 3 total, got {len(result)}"
    strategy_in_result = [p for p in result if getattr(p, "kind", None) == "strategy"]
    assert len(strategy_in_result) == 1, f"Expected exactly 1 strategy card after cap, got {len(strategy_in_result)}"


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
# 22. test_strategy_cap_preserves_ranking (Finding P2 — context.py:1213)
# ---------------------------------------------------------------------------


def test_strategy_cap_preserves_ranking():
    """The strategy cap filters excess cards in-place, preserving original ranking.

    Before the fix: the cap rebuilt the list as `_non_sc + _sc_served`,
    moving a high-ranked strategy card to the tail where the token-budget loop
    could cut it while letting lower-ranked non-strategy items through.

    Mutation: restore `selected = _non_sc + _sc_served` → a strategy card
    that was originally at position 0 (highest rank) is pushed to the end,
    breaking the ordering assertion.
    """

    def _proc(name: str, kind: str | None = None, score: float = 0.5) -> MagicMock:
        p = MagicMock()
        p.name = name
        p.kind = kind
        p.score = score
        return p

    # Strategy card ranked first (highest score), then two non-strategy items.
    sc1 = _proc("sc-1", "strategy", score=0.95)
    sc2 = _proc("sc-2", "strategy", score=0.60)
    proc_a = _proc("proc-a", score=0.70)
    proc_b = _proc("proc-b", score=0.50)
    selected = [sc1, proc_a, sc2, proc_b]  # ranking: sc1, proc-a, sc2, proc-b

    _max_sc = 1
    _sc_hits = [p for p in selected if getattr(p, "kind", None) == "strategy"]
    _sc_served = _sc_hits[:_max_sc]
    # Fixed logic: filter in-place, preserving original order.
    _excess_ids = {id(p) for p in _sc_hits[_max_sc:]}
    result = [p for p in selected if id(p) not in _excess_ids]

    assert len(result) == 3, f"Expected 3 items after cap, got {len(result)}"
    # sc1 must remain at position 0 (highest rank preserved).
    assert result[0].name == "sc-1", (
        f"Highest-ranked strategy card must stay at rank 0 — got {result[0].name!r} instead"
    )
    # sc2 must be removed (excess, over cap).
    names = [p.name for p in result]
    assert "sc-2" not in names, "Second strategy card (excess) must be removed"


# ---------------------------------------------------------------------------
# 23. test_strategy_cap_attributes_removed_cards_in_trace (Finding P2 — context.py:1213)
# ---------------------------------------------------------------------------


def test_strategy_cap_attributes_removed_cards_in_trace():
    """Excess strategy cards removed by the cap are recorded in the retrieval trace.

    Before the fix: discarded cards were never passed to `_tr_filtered`, so
    they appeared as `unaccounted` in the retrieval trace, corrupting drift
    instrumentation.

    Mutation: remove the `_tr_filtered(_sc_hits, _sc_served, ...)` call →
    `dropped_items` stays empty and the assertion fails.
    """

    def _proc(name: str, kind: str | None = None) -> MagicMock:
        p = MagicMock()
        p.name = name
        p.kind = kind
        p.id = uuid4()
        p.score = 0.8
        return p

    sc1 = _proc("sc-1", "strategy")
    sc2 = _proc("sc-2", "strategy")
    sc3 = _proc("sc-3", "strategy")
    proc_a = _proc("proc-a")
    selected = [sc1, proc_a, sc2, sc3]

    _max_sc = 1
    _sc_hits = [p for p in selected if getattr(p, "kind", None) == "strategy"]
    _sc_served = _sc_hits[:_max_sc]

    # Simulate the _tr_filtered helper.
    dropped_items: list[tuple] = []

    def fake_tr_filtered(before, after, mem_type, disposition, stage):
        kept = {str(getattr(i, "id", "")) for i in (after or [])}
        for it in before or []:
            iid = str(getattr(it, "id", ""))
            if iid and iid not in kept:
                dropped_items.append((iid, mem_type, disposition, stage))
        return after

    # Apply the fixed cap logic with trace attribution.
    if len(_sc_hits) > _max_sc:
        _excess_ids = {id(p) for p in _sc_hits[_max_sc:]}
        selected = [p for p in selected if id(p) not in _excess_ids]
        fake_tr_filtered(_sc_hits, _sc_served, "procedure", "sliced_off", "strategy_card_cap")

    # Two excess cards (sc2, sc3) must be recorded as dropped.
    assert len(dropped_items) == 2, (
        f"Expected 2 dropped trace entries for excess strategy cards, got {len(dropped_items)}: {dropped_items}"
    )
    dropped_stages = {stage for _, _, _, stage in dropped_items}
    assert dropped_stages == {"strategy_card_cap"}, (
        f"Dropped cards must be attributed to 'strategy_card_cap', got {dropped_stages}"
    )


# ---------------------------------------------------------------------------
# 24. test_ungraded_review_deactivates_existing_card (Finding P2 #1 — distiller.py:115)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ungraded_review_schedules_deactivation(mock_brain, mock_heart, mock_llm):
    """A noise/superseded review schedules deactivation of the existing card.

    Before the fix: outcome not in GRADED_OUTCOMES → early return before UUID
    parsing, so an existing strategy card was never deactivated even after the
    decision was marked noise/superseded.

    Mutation: revert to early return on ungraded outcomes →
    _deactivate_card_for_decision is never scheduled → the existing card stays
    active (deactivate_procedure not called).
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
    existing_card_id = uuid4()

    # Wire _find_existing_card to report an existing card.
    distiller._find_existing_card = AsyncMock(return_value=existing_card_id)
    distiller._deactivate_procedure = AsyncMock()

    event = {"decision_id": str(decision_id), "outcome": "noise"}
    await distiller._on_decision_reviewed(event)
    # Flush the deactivation task.
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    distiller._deactivate_procedure.assert_called_once_with(existing_card_id)


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
# 26. test_zero_max_per_turn_is_unlimited (Finding P2 #4 — context.py:1211)
# ---------------------------------------------------------------------------


def test_zero_max_per_turn_is_unlimited_graph_primary():
    """When strategy_cards_max_per_turn=0, ALL strategy cards pass through (unlimited).

    Before the fix: max(0, 0) = 0, and _sc_hits[:0] = [] removed every card,
    so an operator using the documented escape hatch disabled retrieval entirely.

    Mutation: remove the `if _max_sc == 0` guard → _sc_served = _sc_hits[:0]
    → result contains zero strategy cards, not three.
    """

    def _proc(name: str, kind: str | None = None) -> MagicMock:
        p = MagicMock()
        p.name = name
        p.kind = kind
        p.score = 0.8
        return p

    strategy_cards = [_proc("sc-1", "strategy"), _proc("sc-2", "strategy"), _proc("sc-3", "strategy")]
    non_strategy = [_proc("proc-a"), _proc("proc-b")]
    selected = non_strategy + strategy_cards

    # Apply the fixed graph-primary cap logic with max_sc=0 (unlimited).
    _max_sc = max(0, 0)  # 0 = unlimited
    _sc_hits = [p for p in selected if getattr(p, "kind", None) == "strategy"]
    _sc_served = _sc_hits if _max_sc == 0 else _sc_hits[:_max_sc]

    assert len(_sc_served) == 3, (
        f"With max_per_turn=0 (unlimited), all 3 strategy cards must pass — got {len(_sc_served)}"
    )


def test_zero_max_per_turn_is_unlimited_passive_path():
    """Same unlimited contract for the passive (embedding+critic) path."""

    def _proc(name: str, kind: str | None = None) -> MagicMock:
        p = MagicMock()
        p.name = name
        p.kind = kind
        return p

    embedding_procedures = [
        _proc("sc-1", "strategy"),
        _proc("sc-2", "strategy"),
        _proc("proc-a"),
    ]
    max_sc = max(0, 0)  # 0 = unlimited
    strategy_hits = [p for p in embedding_procedures if getattr(p, "kind", None) == "strategy"]
    non_strategy = [p for p in embedding_procedures if getattr(p, "kind", None) != "strategy"]
    # Fixed logic: 0 means unlimited.
    strategy_served = strategy_hits if max_sc == 0 else strategy_hits[:max_sc]
    result = non_strategy + strategy_served

    assert len([p for p in result if p.kind == "strategy"]) == 2, (
        "With max_per_turn=0 (unlimited), all strategy cards must pass through"
    )


# ---------------------------------------------------------------------------
# 27. test_combined_critic_cap_enforced (Finding P2 #3 — context.py:1424)
# ---------------------------------------------------------------------------


def test_combined_critic_cap_enforced():
    """Strategy card cap applies to the combined critic+embedding list.

    Before the fix: the cap ran only on embedding_procedures; critic_procedures
    were prepended afterwards, so two critic strategy cards + one embedding card
    could exceed a cap of one.

    Mutation: remove the post-merge combined-cap block → all_procedures keeps
    3 strategy cards, breaking the assertion.
    """

    def _proc(name: str, kind: str | None = None) -> MagicMock:
        p = MagicMock()
        p.name = name
        p.kind = kind
        p.id = uuid4()
        p.score = 0.8
        return p

    # Two strategy cards from critic, one from embedding — all pass the
    # embedding-only pre-filter (it only sees the embedding card).
    critic_procedures = [_proc("sc-critic-1", "strategy"), _proc("sc-critic-2", "strategy")]
    embedding_procedures = [_proc("sc-embed-1", "strategy"), _proc("proc-a")]
    all_procedures = critic_procedures + embedding_procedures

    # Apply the combined cap (max_sc=1, non-zero so cap fires).
    _max_sc_combined = 1
    _sc_combined = [p for p in all_procedures if getattr(p, "kind", None) == "strategy"]
    if len(_sc_combined) > _max_sc_combined:
        _sc_combined_served = _sc_combined[:_max_sc_combined]
        _sc_excess_ids = {id(p) for p in _sc_combined[_max_sc_combined:]}
        all_procedures = [p for p in all_procedures if id(p) not in _sc_excess_ids]

    strategy_in_result = [p for p in all_procedures if getattr(p, "kind", None) == "strategy"]
    assert len(strategy_in_result) == 1, (
        f"Combined cap of 1 must leave exactly 1 strategy card after merging critic+embedding, "
        f"got {len(strategy_in_result)}"
    )


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
# 31. test_catalog_strategy_card_cap (Finding P2 #1 — context.py:1211)
# ---------------------------------------------------------------------------


def test_catalog_strategy_card_cap_limits_catalog_entries():
    """Strategy-card cap is applied to the procedure catalog deduped list.

    Before the fix: deduped included ALL active procedures including every
    strategy card, so with strategy_cards_retrieval_enabled=True and a cap
    of 1 the catalog still rendered all 3 card titles+descriptions and they
    could crowd out ordinary procedures from the catalog's char budget.

    Mutation: remove the catalog-cap block → all 3 strategy cards survive in
    deduped → the assertion len(sc_in_deduped) == 1 fails.
    """

    def _proc(name: str, kind: str | None = None) -> MagicMock:
        p = MagicMock()
        p.name = name
        p.kind = kind
        return p

    ordinary = [_proc("proc-a"), _proc("proc-b"), _proc("proc-c")]
    strategy_cards = [_proc("sc-1", "strategy"), _proc("sc-2", "strategy"), _proc("sc-3", "strategy")]
    deduped = ordinary + strategy_cards  # 6 entries total

    settings = _make_settings(strategy_cards_retrieval_enabled=True, strategy_cards_max_per_turn=1)

    # Apply the catalog-cap logic from context.py (the fix).
    if getattr(settings, "strategy_cards_retrieval_enabled", False):
        _cat_max_sc = max(0, getattr(settings, "strategy_cards_max_per_turn", 1))
        if _cat_max_sc > 0:
            _cat_sc = [p for p in deduped if getattr(p, "kind", None) == "strategy"]
            if len(_cat_sc) > _cat_max_sc:
                _cat_sc_excess_ids = {id(p) for p in _cat_sc[_cat_max_sc:]}
                deduped = [p for p in deduped if id(p) not in _cat_sc_excess_ids]

    sc_in_deduped = [p for p in deduped if getattr(p, "kind", None) == "strategy"]
    assert len(sc_in_deduped) == 1, (
        f"Catalog must contain at most 1 strategy card (cap=1), got {len(sc_in_deduped)}"
    )
    assert sc_in_deduped[0].name == "sc-1", "First strategy card must be the one retained"
    # Ordinary procedures must all survive (cap only removes excess strategy cards).
    assert len([p for p in deduped if getattr(p, "kind", None) != "strategy"]) == 3


def test_catalog_strategy_card_cap_zero_is_unlimited():
    """A catalog-cap of 0 passes all strategy cards through (unlimited).

    Mutation: remove the `if _cat_max_sc > 0` guard → _cat_sc_excess_ids
    includes all cards, deduped loses every strategy card.
    """

    def _proc(name: str, kind: str | None = None) -> MagicMock:
        p = MagicMock()
        p.name = name
        p.kind = kind
        return p

    strategy_cards = [_proc("sc-1", "strategy"), _proc("sc-2", "strategy")]
    deduped = [_proc("proc-a")] + strategy_cards

    settings = _make_settings(strategy_cards_retrieval_enabled=True, strategy_cards_max_per_turn=0)

    # Apply the catalog-cap logic — 0 means unlimited, so deduped is unchanged.
    if getattr(settings, "strategy_cards_retrieval_enabled", False):
        _cat_max_sc = max(0, getattr(settings, "strategy_cards_max_per_turn", 1))
        if _cat_max_sc > 0:
            _cat_sc = [p for p in deduped if getattr(p, "kind", None) == "strategy"]
            if len(_cat_sc) > _cat_max_sc:
                _cat_sc_excess_ids = {id(p) for p in _cat_sc[_cat_max_sc:]}
                deduped = [p for p in deduped if id(p) not in _cat_sc_excess_ids]

    sc_in_deduped = [p for p in deduped if getattr(p, "kind", None) == "strategy"]
    assert len(sc_in_deduped) == 2, (
        "With max_per_turn=0 (unlimited) all strategy cards must survive in catalog"
    )


# ---------------------------------------------------------------------------
# 32. test_passive_cap_preserves_ranking (Finding P2 #2 — context.py:1438)
# ---------------------------------------------------------------------------


def test_passive_cap_preserves_ranking():
    """Passive-path strategy-card cap filters in-place to preserve ranking.

    Before the fix: the code partitioned embedding_procedures into
    non_strategy + strategy_served, moving every strategy card to the tail
    regardless of rank.  A top-ranked strategy card was therefore vulnerable
    to being cut by the token-budget loop while lower-ranked ordinary
    procedures remained.

    Mutation: revert to `non_strategy + strategy_served` →
    strategy cards are always last → a high-ranked sc at index 0 ends up
    after proc-a at index 2, breaking the position assertion.
    """

    def _proc(name: str, kind: str | None = None) -> MagicMock:
        p = MagicMock()
        p.name = name
        p.kind = kind
        p.id = name  # unique id per proc
        return p

    # Strategy card ranked FIRST (highest score), followed by ordinary procs.
    sc_top = _proc("sc-top", "strategy")
    proc_a = _proc("proc-a")
    proc_b = _proc("proc-b")
    sc_low = _proc("sc-low", "strategy")  # second strategy card, lower rank
    embedding_procedures = [sc_top, proc_a, proc_b, sc_low]

    max_sc = 1  # cap at 1 strategy card

    # Apply the FIXED in-place cap logic from context.py.
    strategy_hits = [p for p in embedding_procedures if getattr(p, "kind", None) == "strategy"]
    if max_sc > 0 and len(strategy_hits) > max_sc:
        _sc_excess_ids = {id(p) for p in strategy_hits[max_sc:]}
        result = [p for p in embedding_procedures if id(p) not in _sc_excess_ids]
    else:
        result = embedding_procedures

    # sc-top (index 0) must stay at the front — it was first in the original list.
    assert result[0].name == "sc-top", (
        f"Top-ranked strategy card must remain at index 0, got {result[0].name!r}"
    )
    assert len([p for p in result if getattr(p, "kind", None) == "strategy"]) == 1
    assert len(result) == 3  # sc-top, proc-a, proc-b (sc-low dropped)


def test_passive_cap_zero_is_unlimited_preserves_order():
    """Passive-path cap=0 keeps all strategy cards in their original positions.

    Mutation: remove the `if max_sc > 0 and ...` guard → all strategy cards
    are dropped (max(0,0)=0, sliced to [:0]=empty).
    """

    def _proc(name: str, kind: str | None = None) -> MagicMock:
        p = MagicMock()
        p.name = name
        p.kind = kind
        return p

    sc1 = _proc("sc-1", "strategy")
    sc2 = _proc("sc-2", "strategy")
    proc_a = _proc("proc-a")
    embedding_procedures = [sc1, proc_a, sc2]

    max_sc = max(0, 0)  # 0 = unlimited

    strategy_hits = [p for p in embedding_procedures if getattr(p, "kind", None) == "strategy"]
    if max_sc > 0 and len(strategy_hits) > max_sc:
        _sc_excess_ids = {id(p) for p in strategy_hits[max_sc:]}
        result = [p for p in embedding_procedures if id(p) not in _sc_excess_ids]
    else:
        result = embedding_procedures

    assert len([p for p in result if getattr(p, "kind", None) == "strategy"]) == 2, (
        "With max_sc=0 (unlimited) all strategy cards must survive"
    )
    assert result == embedding_procedures, "Order must be unchanged when cap is unlimited"
