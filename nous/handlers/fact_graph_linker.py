"""Fact Graph Linker — cross-type linking on fact creation.

Listens to: fact_learned (in-process EventBus)
Calls: GraphLinker.link_fact_to_decisions() and GraphLinker.link_fact_to_facts()

F022 Phase 2: Wires fact->decision and fact->fact auto-linking.
"""

from __future__ import annotations

import asyncio
import logging
from uuid import UUID

from nous.brain.graph_linker import GraphLinker
from nous.config import Settings
from nous.events import Event, EventBus

logger = logging.getLogger(__name__)


class FactGraphLinker:
    """Links newly learned facts to related decisions via embedding similarity.

    Subscribes to fact_learned events on the in-process EventBus.
    Calls GraphLinker.link_fact_to_decisions() in an isolated DB session
    so failures never affect the originating fact creation transaction.
    """

    def __init__(
        self,
        graph_linker: GraphLinker,
        settings: Settings,
        bus: EventBus,
    ) -> None:
        self._graph_linker = graph_linker
        self._settings = settings
        bus.on("fact_learned", self.handle)

    async def handle(self, event: Event) -> None:
        """Handle fact_learned — link fact to related decisions."""
        if not self._settings.cross_type_linking_enabled:
            return

        fact_id_str = event.data.get("fact_id")
        fact_content = event.data.get("content", "")
        if not fact_id_str or not fact_content:
            return

        try:
            fact_id = UUID(fact_id_str)
        except ValueError:
            logger.debug("F022: Invalid fact_id in fact_learned event: %s", fact_id_str)
            return

        try:
            async with self._graph_linker.db.session() as link_session:
                decision_edges = await self._graph_linker.link_fact_to_decisions(
                    fact_id=fact_id,
                    fact_content=fact_content,
                    session=link_session,
                )
                fact_edges = await self._graph_linker.link_fact_to_facts(
                    fact_id=fact_id,
                    fact_content=fact_content,
                    session=link_session,
                )
                all_edges = decision_edges + fact_edges
                # F044: commit even when no new edges were created — a re-derivation
                # may have only incremented LTP counters (reinforcement). Gated on the
                # flag so default-prod (F044 off) keeps the original commit semantics.
                if all_edges or getattr(self._settings, "tinyhippo_lite_enabled", False):
                    await link_session.commit()
                    logger.debug(
                        "F022: Linked fact %s to %d decisions + %d facts",
                        fact_id, len(decision_edges), len(fact_edges),
                    )
        except asyncio.CancelledError:
            raise  # Let the EventBus handle cancellation for clean shutdown
        except Exception:
            logger.debug("F022 fact graph linking failed for fact %s", fact_id_str)
