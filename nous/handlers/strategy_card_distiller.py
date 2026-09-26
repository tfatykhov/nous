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
            "description": (
                "The full strategy or guardrail in 2-4 sentences: "
                "when X, do/avoid Y, because Z"
            ),
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
        if bus is not None:
            bus.on("decision_reviewed", self._on_decision_reviewed)

    # ------------------------------------------------------------------
    # Event entry-point (sync — called by the event bus)
    # ------------------------------------------------------------------

    def _on_decision_reviewed(self, event: Any) -> None:
        """Sync shim: schedule async distillation as a fire-and-forget task."""
        if not getattr(self._settings, "strategy_cards_enabled", False):
            return
        outcome = (
            event.get("outcome") if isinstance(event, dict)
            else getattr(event, "outcome", None)
        )
        if outcome not in GRADED_OUTCOMES:
            return
        decision_id_raw = (
            event.get("decision_id") if isinstance(event, dict)
            else getattr(event, "decision_id", None)
        )
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
        asyncio.create_task(
            self._distil(decision_id, outcome),
            name=f"strategy_card_distil_{decision_id}",
        )

    # ------------------------------------------------------------------
    # Core distillation (async, errors are swallowed)
    # ------------------------------------------------------------------

    async def _distil(self, decision_id: UUID, outcome: str) -> None:
        """Outer wrapper — all errors logged, never propagated."""
        try:
            await self._do_distil(decision_id, outcome)
        except Exception:
            logger.warning(
                "StrategyCardDistiller: distillation failed for decision %s",
                decision_id,
                exc_info=True,
            )

    async def _do_distil(self, decision_id: UUID, outcome: str) -> None:
        if not self._llm:
            logger.debug(
                "StrategyCardDistiller: no LLM client wired, skipping %s", decision_id
            )
            return

        decision = await self._brain.get(decision_id)
        if decision is None:
            logger.warning(
                "StrategyCardDistiller: decision %s not found, skipping", decision_id
            )
            return

        # Idempotency: find existing active strategy card for this decision
        existing_id = await self._find_existing_card(decision_id)

        user_msg = (
            f"Decision: {decision.description}\n"
            f"Context: {decision.context or '(none)'}\n"
            f"Outcome: {outcome}\n"
            f"Result notes: {decision.outcome_result or '(none)'}\n\n"
            f"Extract a strategy card for this {outcome} outcome."
        )

        background_model = getattr(
            self._settings, "background_model", "claude-haiku-4-5-20251001"
        )
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
        tags = (
            [str(t)[:100] for t in raw_tags[:6]]
            if isinstance(raw_tags, list) else []
        )

        if not name or not lesson:
            logger.warning(
                "StrategyCardDistiller: incomplete card for decision %s (name=%r), skipping",
                decision_id, name,
            )
            return

        # Deactivate old card before creating the new one (update semantics)
        if existing_id is not None:
            await self._deactivate_procedure(existing_id)

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

        async with self._heart.db.session() as session:
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

        logger.info(
            "StrategyCardDistiller: distilled %s card for decision %s → procedure %s",
            outcome, decision_id, detail.id,
        )

    # ------------------------------------------------------------------
    # Idempotency helpers
    # ------------------------------------------------------------------

    async def _find_existing_card(self, decision_id: UUID) -> UUID | None:
        """Return the id of an existing active strategy card for this decision."""
        from sqlalchemy import select

        from nous.storage.models import Procedure

        async with self._heart.db.session() as session:
            result = await session.execute(
                select(Procedure.id)
                .where(Procedure.agent_id == self._brain.agent_id)
                .where(Procedure.kind == "strategy")
                .where(Procedure.active.is_(True))
                .where(
                    Procedure.runtime_metadata["source_decision_id"].astext
                    == str(decision_id)
                )
                .limit(1)
            )
            return result.scalar_one_or_none()

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
