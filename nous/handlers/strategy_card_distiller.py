"""Strategy Card Distiller — Reasoning Maps Layer 1.

Listens to decision_reviewed events and distils a strategy card
(kind='strategy' procedure) for graded outcomes (success/partial/failure).
Skips noise and superseded. Idempotent per decision_id.

Flags:
  NOUS_STRATEGY_CARDS_ENABLED=false  (distillation off by default)
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any
from uuid import UUID

if TYPE_CHECKING:
    from nous.brain.brain import Brain
    from nous.brain.graph_linker import GraphLinker
    from nous.config import Settings
    from nous.events import EventBus
    from nous.heart.heart import Heart

from sqlalchemy.exc import IntegrityError

from nous.brain.schemas import GRADED_OUTCOMES
from nous.handlers import LLMClient, call_background_llm_structured
from nous.heart.schemas import ProcedureInput

logger = logging.getLogger(__name__)

_CARD_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {
            "type": "string",
            "description": "Short title for the strategy card (<=80 chars)",
        },
        "description": {
            "type": "string",
            "description": "One-sentence summary of the lesson",
        },
        "lesson": {
            "type": "string",
            "description": ("The full strategy or guardrail in 2-4 sentences: when X, do/avoid Y, because Z"),
        },
        "tags": {
            "type": "array",
            "items": {"type": "string"},
            "description": "2-4 lowercase tags",
        },
    },
    "required": ["name", "description", "lesson"],
}

_SYSTEM_PROMPT = (
    "You extract concise strategy cards from decision outcomes.\n\n"
    "A strategy card captures a transferable lesson from a past decision outcome:\n"
    "- success → validated strategy: 'when X, doing Y works because Z'\n"
    "- partial → qualified lesson: 'when X, Y partially works but note Z'\n"
    "- failure → guardrail: 'when X, avoid Y because Z'\n\n"
    "Keep the lesson actionable, free of proper nouns, and at most 4 sentences."
)


class StrategyCardDistiller:
    """Distils strategy cards from resolved decisions (Reasoning Maps L1).

    Subscribes to 'decision_reviewed' on the event bus. Distillation runs
    off the hot path via asyncio.create_task; the resolve call returns before
    distillation starts.
    """

    def __init__(
        self,
        brain: Brain,
        heart: Heart,
        settings: Settings,
        bus: EventBus | None,
        llm_client: LLMClient | None = None,
        graph_linker: GraphLinker | None = None,
    ) -> None:
        self._brain = brain
        self._heart = heart
        self._settings = settings
        self._llm = llm_client
        self._graph_linker = graph_linker
        # In-flight set: prevents two concurrent distillations for the same
        # decision from racing to insert duplicate strategy cards.
        self._in_flight: set[str] = set()
        # Pending outcomes: when a re-review arrives while distillation is in
        # flight, the latest outcome is stored here so it runs after the
        # current distillation completes (finding #3).
        self._pending: dict[str, str] = {}
        # Tracked tasks: all asyncio.Tasks spawned by this distiller, so
        # shutdown() can await them before the process exits.
        self._tasks: set = set()
        if bus is not None:
            bus.on("decision_reviewed", self._on_decision_reviewed)

    # ------------------------------------------------------------------
    # Event entry-point (async — called by the event bus via await handler(event))
    # ------------------------------------------------------------------

    async def _on_decision_reviewed(self, event: Any) -> None:
        """Async handler: schedule distillation as a fire-and-forget task."""
        if not getattr(self._settings, "strategy_cards_enabled", False):
            return
        # Extract payload from either a nous.events.Event or a plain dict.
        # Event objects carry payload in .data; dicts are passed directly.
        if isinstance(event, dict):
            outcome = event.get("outcome")
            decision_id_raw = event.get("decision_id")
        else:
            data: dict = getattr(event, "data", {}) or {}
            outcome = data.get("outcome")
            decision_id_raw = data.get("decision_id")
        if not decision_id_raw:
            return
        try:
            decision_id = UUID(str(decision_id_raw))
        except (ValueError, AttributeError):
            logger.warning(
                "StrategyCardDistiller: invalid decision_id %r, skipping",
                decision_id_raw,
            )
            return
        if outcome not in GRADED_OUTCOMES:
            # Ungraded review (noise/superseded): retire any existing card.
            # Coalesce with any in-flight distillation: record the ungraded
            # outcome in _pending so the follow-up deactivates rather than
            # creating a fresh card for a decision that is now noise/superseded.
            key = str(decision_id)
            if key in self._in_flight:
                self._pending[key] = outcome
            else:
                self._track_task(
                    asyncio.create_task(
                        self._deactivate_card_for_decision(decision_id),
                        name=f"strategy_card_deactivate_{decision_id}",
                    )
                )
            return
        self._track_task(
            asyncio.create_task(
                self._distil(decision_id, outcome),
                name=f"strategy_card_distil_{decision_id}",
            )
        )

    def _track_task(self, task: asyncio.Task) -> asyncio.Task:
        """Track a task so it can be awaited during shutdown."""
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    # ------------------------------------------------------------------
    # Core distillation (async, errors are swallowed)
    # ------------------------------------------------------------------

    async def _distil(self, decision_id: UUID, outcome: str) -> None:
        """Outer wrapper — all errors logged, never propagated.

        Uses an in-flight set to prevent two concurrent distillations for the
        same decision from racing to insert duplicate strategy cards.

        If a re-review arrives while this distillation is running, the latest
        outcome is stored in _pending and re-run after the current run
        completes (finding #3 — preserves a newer review outcome).
        """
        key = str(decision_id)
        if key in self._in_flight:
            # Coalesce: record the latest outcome so it runs after the current
            # distillation finishes, rather than being silently dropped.
            logger.debug(
                "StrategyCardDistiller: %s already in flight, queuing outcome %r",
                decision_id,
                outcome,
            )
            self._pending[key] = outcome
            return
        self._in_flight.add(key)
        try:
            await self._do_distil(decision_id, outcome)
        except Exception:
            logger.warning(
                "StrategyCardDistiller: distillation failed for decision %s",
                decision_id,
                exc_info=True,
            )
        finally:
            self._in_flight.discard(key)
            # If a newer review arrived while we were running, dispatch the
            # appropriate follow-up: re-distil for a graded outcome, or
            # deactivate for an ungraded one (noise/superseded).
            pending_outcome = self._pending.pop(key, None)
            if pending_outcome is not None:
                if pending_outcome in GRADED_OUTCOMES:
                    self._track_task(
                        asyncio.create_task(
                            self._distil(decision_id, pending_outcome),
                            name=f"strategy_card_distil_{decision_id}_followup",
                        )
                    )
                else:
                    # Latest review was noise/superseded: deactivate any card
                    # that was just created by this distillation.
                    self._track_task(
                        asyncio.create_task(
                            self._deactivate_card_for_decision(decision_id),
                            name=f"strategy_card_deactivate_{decision_id}_followup",
                        )
                    )

    async def _do_distil(self, decision_id: UUID, outcome: str) -> None:
        if not self._llm:
            logger.debug("StrategyCardDistiller: no LLM client wired, skipping %s", decision_id)
            return

        decision = await self._brain.get(decision_id)
        if decision is None:
            logger.warning("StrategyCardDistiller: decision %s not found, skipping", decision_id)
            return

        user_msg = (
            f"Decision: {decision.description}\n"
            f"Context: {decision.context or '(none)'}\n"
            f"Outcome: {outcome}\n"
            f"Result notes: {decision.outcome_result or '(none)'}\n\n"
            f"Extract a strategy card for this {outcome} outcome."
        )

        background_model = getattr(self._settings, "background_model", "claude-haiku-4-5-20251001")
        card = await call_background_llm_structured(
            client=self._llm,
            model=background_model,
            system_prompt=_SYSTEM_PROMPT,
            user_message=user_msg,
            tool_name="emit_strategy_card",
            tool_description="Emit the distilled strategy card",
            output_schema=_CARD_SCHEMA,
            max_tokens=512,
        )
        if not card:
            logger.warning(
                "StrategyCardDistiller: LLM returned no card for decision %s, skipping",
                decision_id,
            )
            return

        name = (card.get("name") or "")[:500].strip()
        description = (card.get("description") or "")[:1000].strip()
        lesson = (card.get("lesson") or "")[:2000].strip()
        raw_tags = card.get("tags") or []
        tags = [str(t)[:100] for t in raw_tags[:6]] if isinstance(raw_tags, list) else []

        if not name or not lesson:
            logger.warning(
                "StrategyCardDistiller: incomplete card for decision %s (name=%r), skipping",
                decision_id,
                name,
            )
            return

        inp = ProcedureInput(
            name=name,
            domain="strategy",
            description=description,
            implementation_notes=[lesson],
            tags=tags,
            kind="strategy",
            runtime_metadata={
                "source_decision_id": str(decision_id),
                "outcome": outcome,
            },
        )

        # Deactivate old card and create new one in a single transaction so a
        # failure on insertion or edge creation preserves the previous card.
        # The idempotency lookup is done INSIDE the transaction so the check
        # and the deactivate/insert are atomic — preventing a concurrent task
        # that also passed the _in_flight guard from racing to insert a second
        # active card for the same decision.
        #
        # Retry on IntegrityError: two *different* decisions whose LLM calls
        # return the same generic name can both pass _make_unique_name before
        # either commits, then race to insert the same name.  A fresh session
        # on the retry will see the already-committed row and pick a different
        # suffix, so the second decision gets its card rather than silently
        # being left without one.
        _MAX_NAME_RETRIES = 3
        original_inp_name = inp.name
        detail: Any = None
        for _attempt in range(_MAX_NAME_RETRIES):
            inp = inp.model_copy(update={"name": original_inp_name})
            try:
                async with self._heart.db.session() as session:
                    existing_id = await self._find_existing_card_in_session(decision_id, session)
                    if existing_id is not None:
                        from sqlalchemy import update as sa_update

                        from nous.storage.models import Procedure

                        await session.execute(
                            sa_update(Procedure)
                            .where(Procedure.id == existing_id)
                            .where(Procedure.agent_id == self._brain.agent_id)
                            .values(active=False)
                        )
                    # Disambiguate name before insert: append a counter suffix when an
                    # active procedure already has the same case-insensitive name, so a
                    # generic title ('Validate Before Deploying') produced by two different
                    # decisions does not cause a unique-constraint violation on the second
                    # insert and silently leave that decision without a card.
                    unique_name = await self._make_unique_name(inp.name, session)
                    if unique_name != inp.name:
                        inp = inp.model_copy(update={"name": unique_name})
                    detail = await self._heart.procedures.store(inp, session=session)
                    if self._graph_linker is not None:
                        try:
                            await self._graph_linker.create_edge(
                                source_id=detail.id,
                                source_type="procedure",
                                target_id=decision_id,
                                target_type="decision",
                                relation="extracted_from",
                                weight=1.0,
                                session=session,
                                provenance_source="strategy_card_distiller",
                            )
                        except Exception:
                            logger.warning(
                                "StrategyCardDistiller: edge creation failed for card %s",
                                detail.id,
                                exc_info=True,
                            )
                    await session.commit()
                break  # committed successfully
            except IntegrityError:
                if _attempt == _MAX_NAME_RETRIES - 1:
                    raise
                logger.warning(
                    "StrategyCardDistiller: name conflict on attempt %d for decision %s, retrying",
                    _attempt + 1,
                    decision_id,
                )

        logger.info(
            "StrategyCardDistiller: distilled %s card for decision %s → procedure %s",
            outcome,
            decision_id,
            detail.id,
        )

    # ------------------------------------------------------------------
    # Idempotency helpers
    # ------------------------------------------------------------------

    async def _find_existing_card(self, decision_id: UUID) -> UUID | None:
        """Return the id of an existing active strategy card for this decision.

        Opens its own session — use only outside a transaction. For transactional
        callers use _find_existing_card_in_session instead.
        """
        from sqlalchemy import select

        from nous.storage.models import Procedure

        async with self._heart.db.session() as session:
            result = await session.execute(
                select(Procedure.id)
                .where(Procedure.agent_id == self._brain.agent_id)
                .where(Procedure.kind == "strategy")
                .where(Procedure.active.is_(True))
                .where(Procedure.runtime_metadata["source_decision_id"].astext == str(decision_id))
                .limit(1)
            )
            return result.scalar_one_or_none()

    async def _find_existing_card_in_session(self, decision_id: UUID, session: Any) -> UUID | None:
        """Return the id of an existing active strategy card within a session.

        Runs inside the caller's transaction so the lookup is atomic with any
        subsequent deactivation and insertion.
        """
        from sqlalchemy import select

        from nous.storage.models import Procedure

        result = await session.execute(
            select(Procedure.id)
            .where(Procedure.agent_id == self._brain.agent_id)
            .where(Procedure.kind == "strategy")
            .where(Procedure.active.is_(True))
            .where(Procedure.runtime_metadata["source_decision_id"].astext == str(decision_id))
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def _deactivate_card_for_decision(self, decision_id: UUID) -> None:
        """Deactivate any active strategy card for a decision (noise/superseded review)."""
        try:
            existing_id = await self._find_existing_card(decision_id)
            if existing_id is not None:
                await self._deactivate_procedure(existing_id)
                logger.info(
                    "StrategyCardDistiller: deactivated card %s for decision %s (ungraded review)",
                    existing_id,
                    decision_id,
                )
        except Exception:
            logger.warning(
                "StrategyCardDistiller: failed to deactivate card for decision %s",
                decision_id,
                exc_info=True,
            )

    async def _make_unique_name(self, name: str, session: Any) -> str:
        """Return a name unique among active procedures, appending ' (N)' if needed.

        Runs inside the caller's transaction so the uniqueness check is
        consistent with any preceding deactivation within the same session.
        """
        from sqlalchemy import func, select

        from nous.storage.models import Procedure

        base = name[:495]  # reserve room for ' (NN)' suffix
        candidate = base
        for suffix_n in range(2, 21):
            result = await session.execute(
                select(Procedure.id)
                .where(Procedure.agent_id == self._brain.agent_id)
                .where(func.lower(Procedure.name) == func.lower(candidate))
                .where(Procedure.active.is_(True))
                .limit(1)
            )
            if result.scalar_one_or_none() is None:
                return candidate
            candidate = f"{base} ({suffix_n})"
        # Exhausted retries; return the last candidate and let the store call
        # raise the constraint error rather than silently losing the card.
        return candidate

    async def _deactivate_procedure(self, procedure_id: UUID) -> None:
        """Soft-delete an existing strategy card."""
        from sqlalchemy import update

        from nous.storage.models import Procedure

        try:
            async with self._heart.db.session() as session:
                await session.execute(
                    update(Procedure)
                    .where(Procedure.id == procedure_id)
                    .where(Procedure.agent_id == self._brain.agent_id)
                    .values(active=False)
                )
                await session.commit()
        except Exception:
            logger.warning(
                "StrategyCardDistiller: failed to deactivate old card %s",
                procedure_id,
                exc_info=True,
            )

    async def shutdown(self) -> None:
        """Await all in-flight distillation tasks before process shutdown.

        Loops until the tracked set is empty: a task's finally block can
        schedule follow-ups via _track_task while gather() is running, so
        a single snapshot misses those newly registered tasks.  After each
        gather we yield once so call_soon-scheduled done callbacks
        (_tasks.discard) have a chance to fire before the next while-check.
        """
        while self._tasks:
            tasks = list(self._tasks)
            logger.debug("StrategyCardDistiller: draining %d in-flight task(s)", len(tasks))
            await asyncio.gather(*tasks, return_exceptions=True)
            # Yield to the event loop so call_soon-scheduled _tasks.discard
            # callbacks fire before the next iteration checks self._tasks.
            await asyncio.sleep(0)
