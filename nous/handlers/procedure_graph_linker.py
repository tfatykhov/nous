"""Procedure Graph Linker — cross-type linking on procedure creation.

Listens to: procedure_stored (in-process EventBus)
Calls: GraphLinker.create_edge() for procedure->fact and procedure->decision links.

F040 Task 6: Wires procedure->fact (informed_by) and procedure->decision (caused_by)
auto-linking when a new procedure is stored.
"""

from __future__ import annotations

import asyncio
import logging
from uuid import UUID

from sqlalchemy import text

from nous.brain.embeddings import EmbeddingProvider
from nous.brain.graph_linker import GraphLinker, common_template_text
from nous.config import Settings
from nous.events import Event, EventBus

logger = logging.getLogger(__name__)


class ProcedureGraphLinker:
    """Links newly stored procedures to related facts and decisions.

    Subscribes to procedure_stored events on the in-process EventBus.
    Calls GraphLinker.create_edge() in an isolated DB session so failures
    never affect the originating procedure creation transaction.
    """

    def __init__(
        self,
        graph_linker: GraphLinker,
        embedder: EmbeddingProvider | None,
        settings: Settings,
        bus: EventBus,
    ) -> None:
        self._linker = graph_linker
        self._embedder = embedder
        self._settings = settings
        bus.on("procedure_stored", self.handle)

    async def handle(self, event: Event) -> None:
        """Handle procedure_stored — link procedure to related facts and decisions."""
        if not self._settings.cross_type_linking_enabled:
            return

        proc_id_str = event.data.get("procedure_id")
        description = event.data.get("description", "")
        domain = event.data.get("domain", "")

        if not proc_id_str or not description or not self._embedder:
            return

        try:
            proc_id = UUID(proc_id_str)
        except ValueError:
            logger.debug("F040: Invalid procedure_id in procedure_stored event: %s", proc_id_str)
            return

        search_text = f"{domain}: {description}" if domain else description
        template = common_template_text("procedure", search_text)

        try:
            proc_emb = await self._embedder.embed(template)
        except Exception:
            logger.debug("F040: Failed to embed procedure %s for graph linking", proc_id_str)
            return

        emb_str = "[" + ",".join(str(float(v)) for v in proc_emb) + "]"
        threshold = self._settings.graph_threshold_procedure_any

        try:
            async with self._linker.db.session() as session:
                edges_created = 0

                # 1. Find related facts (informed_by)
                fact_sql = text("""
                    SELECT id,
                           1 - (embedding <=> CAST(:embedding AS vector)) AS similarity
                    FROM heart.facts
                    WHERE agent_id = :agent_id
                      AND active = true
                      AND embedding IS NOT NULL
                      AND 1 - (embedding <=> CAST(:embedding AS vector)) >= :threshold
                    ORDER BY embedding <=> CAST(:embedding AS vector)
                    LIMIT 5
                """)
                fact_result = await session.execute(fact_sql, {
                    "embedding": emb_str,
                    "agent_id": self._linker.agent_id,
                    "threshold": threshold * 0.9,
                })
                for row in fact_result.all():
                    if row.similarity >= threshold:
                        edge = await self._linker.create_edge(
                            source_id=proc_id,
                            target_id=row.id,
                            source_type="procedure",
                            target_type="fact",
                            relation="informed_by",
                            weight=float(row.similarity),
                            session=session,
                        )
                        if edge:
                            edges_created += 1

                # 2. Find related decisions (caused_by)
                decision_sql = text("""
                    SELECT id,
                           1 - (embedding <=> CAST(:embedding AS vector)) AS similarity
                    FROM brain.decisions
                    WHERE agent_id = :agent_id
                      AND embedding IS NOT NULL
                      AND 1 - (embedding <=> CAST(:embedding AS vector)) >= :threshold
                    ORDER BY embedding <=> CAST(:embedding AS vector)
                    LIMIT 5
                """)
                decision_result = await session.execute(decision_sql, {
                    "embedding": emb_str,
                    "agent_id": self._linker.agent_id,
                    "threshold": threshold * 0.9,
                })
                for row in decision_result.all():
                    if row.similarity >= threshold:
                        edge = await self._linker.create_edge(
                            source_id=proc_id,
                            target_id=row.id,
                            source_type="procedure",
                            target_type="decision",
                            relation="caused_by",
                            weight=float(row.similarity),
                            session=session,
                        )
                        if edge:
                            edges_created += 1

                if edges_created > 0:
                    await session.commit()
                    logger.debug(
                        "F040: Linked procedure %s to %d nodes",
                        proc_id, edges_created,
                    )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("F040: procedure graph linking failed for procedure %s", proc_id_str)
