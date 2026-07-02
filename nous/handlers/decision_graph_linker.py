"""Decision Graph Linker — reverse cross-type linking on decision creation.

Listens to: decision_recorded (in-process EventBus)
Creates: fact→decision (evidence_for) and episode→decision (discussed_in) edges.

F040 Phase 5: When a decision is recorded, searches for existing facts and
episodes that should be linked to it — the reverse of FactGraphLinker.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from nous.brain.brain import Brain
from nous.brain.embeddings import EmbeddingProvider
from nous.brain.graph_linker import GraphLinker, common_template_text
from nous.config import Settings
from nous.events import Event, EventBus
from nous.storage.models import GraphEdge

logger = logging.getLogger(__name__)


class DecisionGraphLinker:
    """Links newly recorded decisions to related facts and episodes.

    Subscribes to decision_recorded events on the in-process EventBus.
    Fetches the full decision (event only carries id + category), embeds
    via common template, then searches for existing facts and episodes
    that should be linked.
    """

    def __init__(
        self,
        brain: Brain,
        graph_linker: GraphLinker,
        embedder: EmbeddingProvider | None,
        settings: Settings,
        bus: EventBus,
    ) -> None:
        self._brain = brain
        self._linker = graph_linker
        self._embedder = embedder
        self._settings = settings
        bus.on("decision_recorded", self.handle)

    async def handle(self, event: Event) -> None:
        """Handle decision_recorded — link decision to related facts and episodes."""
        if not self._settings.cross_type_linking_enabled:
            return

        decision_id_str = event.data.get("decision_id")
        if not decision_id_str:
            return

        try:
            decision_id = UUID(decision_id_str)
        except ValueError:
            logger.debug("F040: Invalid decision_id in decision_recorded event: %s", decision_id_str)
            return

        if not self._embedder:
            return

        try:
            # Fetch full decision — event only has id + category
            decision = await self._brain.get(decision_id)
            if not decision or not decision.description:
                return

            # Embed using common template for fair cross-type comparison
            template = common_template_text("decision", decision.description)
            dec_emb = await self._embedder.embed(template)
            emb_str = "[" + ",".join(str(float(v)) for v in dec_emb) + "]"

            async with self._linker.db.session() as session:
                edges_created = 0

                # 1. Find facts related to this decision
                fact_threshold = self._settings.graph_threshold_fact_decision
                _window_days = self._settings.graph_link_candidate_window_days
                if _window_days > 0:
                    cutoff = datetime.now(UTC) - timedelta(days=_window_days)
                else:
                    cutoff = datetime.min.replace(tzinfo=UTC)  # no cutoff — keeps SQL shape identical
                fact_sql = text("""
                    SELECT id, content,
                           1 - (embedding <=> CAST(:embedding AS vector)) AS similarity
                    FROM heart.facts
                    WHERE agent_id = :agent_id
                      AND active = true
                      AND embedding IS NOT NULL
                      AND learned_at >= :cutoff
                      AND 1 - (embedding <=> CAST(:embedding AS vector)) >= :threshold
                    ORDER BY embedding <=> CAST(:embedding AS vector)
                    LIMIT 5
                """)
                fact_result = await session.execute(fact_sql, {
                    "embedding": emb_str,
                    "agent_id": self._linker.agent_id,
                    "cutoff": cutoff,
                    "threshold": fact_threshold * 0.9,
                })
                fact_candidates = fact_result.all()

                for row in fact_candidates:
                    # Re-embed fact via common template for fair comparison
                    fact_template = common_template_text("fact", row.content)
                    try:
                        fact_emb = await self._embedder.embed(fact_template)
                    except Exception:
                        continue

                    similarity = GraphLinker._cosine_similarity(dec_emb, fact_emb)
                    if similarity >= fact_threshold:
                        edge = await self._linker.create_edge(
                            source_id=row.id,
                            target_id=decision_id,
                            source_type="fact",
                            target_type="decision",
                            relation="evidence_for",
                            weight=float(similarity),
                            session=session,
                        )
                        if edge:
                            edges_created += 1

                # 2. Find episodes discussing this topic
                ep_threshold = self._settings.graph_threshold_fact_episode
                ep_sql = text("""
                    SELECT id, summary,
                           1 - (embedding <=> CAST(:embedding AS vector)) AS similarity
                    FROM heart.episodes
                    WHERE agent_id = :agent_id
                      AND active = true
                      AND embedding IS NOT NULL
                      AND started_at >= :cutoff
                      AND 1 - (embedding <=> CAST(:embedding AS vector)) >= :threshold
                    ORDER BY embedding <=> CAST(:embedding AS vector)
                    LIMIT 5
                """)
                ep_result = await session.execute(ep_sql, {
                    "embedding": emb_str,
                    "agent_id": self._linker.agent_id,
                    "cutoff": cutoff,
                    "threshold": ep_threshold * 0.9,
                })
                ep_candidates = ep_result.all()

                for row in ep_candidates:
                    ep_template = common_template_text("episode", row.summary)
                    try:
                        ep_emb = await self._embedder.embed(ep_template)
                    except Exception:
                        continue

                    similarity = GraphLinker._cosine_similarity(dec_emb, ep_emb)
                    if similarity >= ep_threshold:
                        edge = await self._linker.create_edge(
                            source_id=row.id,
                            target_id=decision_id,
                            source_type="episode",
                            target_type="decision",
                            relation="discussed_in",
                            weight=float(similarity),
                            session=session,
                        )
                        if edge:
                            edges_created += 1

                # F044: commit reinforcement-only sessions too (re-derived edges
                # increment LTP but create no new rows). Flag-gated to preserve
                # default-prod commit semantics when F044 is off.
                if edges_created > 0 or getattr(self._settings, "tinyhippo_lite_enabled", False):
                    await session.commit()
                    if edges_created > 0:
                        logger.debug(
                            "F040: Linked decision %s to %d existing facts/episodes",
                            decision_id, edges_created,
                        )
        except asyncio.CancelledError:
            raise  # Let the EventBus handle cancellation for clean shutdown
        except Exception:
            logger.debug("F040 decision graph linking failed for decision %s", decision_id_str)
