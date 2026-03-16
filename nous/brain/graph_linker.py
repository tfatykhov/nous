"""F022 Phase 2 — Cross-type graph linking with common-template re-embedding.

Provides auto-linking between different memory types (decisions, facts,
episodes, procedures) using a normalized embedding format for fair
cross-type similarity comparison.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from nous.brain.embeddings import EmbeddingProvider
from nous.brain.schemas import GraphEdgeInfo
from nous.config import Settings
from nous.storage.database import Database
from nous.storage.models import GraphEdge

logger = logging.getLogger(__name__)


def common_template_text(node_type: str, content: str) -> str:
    """Format content using common template for cross-type embedding comparison."""
    return f"{node_type}: {content}"


class GraphLinker:
    """Cross-type auto-linking engine."""

    def __init__(
        self,
        db: Database,
        embedder: EmbeddingProvider | None,
        settings: Settings,
        agent_id: str,
    ) -> None:
        self.db = db
        self.embedder = embedder
        self.settings = settings
        self.agent_id = agent_id

    async def link_fact_to_decisions(
        self,
        fact_id: UUID,
        fact_content: str,
        session: AsyncSession,
    ) -> list[GraphEdgeInfo]:
        """Find and link a new fact to related decisions via common-template re-embedding."""
        if not self.embedder or not self.settings.cross_type_linking_enabled:
            return []

        template_text = common_template_text("fact", fact_content)
        try:
            fact_embedding = await self.embedder.embed(template_text)
        except Exception:
            logger.warning("Failed to embed fact %s for cross-type linking", fact_id)
            return []

        embedding_str = "[" + ",".join(str(float(v)) for v in fact_embedding) + "]"

        cutoff = datetime.now(UTC) - timedelta(days=30)
        sql = text("""
            SELECT id, description,
                   1 - (embedding <=> CAST(:embedding AS vector)) AS similarity
            FROM brain.decisions
            WHERE agent_id = :agent_id
              AND embedding IS NOT NULL
              AND created_at >= :cutoff
              AND 1 - (embedding <=> CAST(:embedding AS vector)) >= :threshold
            ORDER BY embedding <=> CAST(:embedding AS vector)
            LIMIT 5
        """)
        result = await session.execute(sql, {
            "embedding": embedding_str,
            "agent_id": self.agent_id,
            "cutoff": cutoff,
            "threshold": self.settings.cross_type_threshold * 0.9,
        })
        candidates = result.all()

        edges = []
        for row in candidates:
            decision_template = common_template_text("decision", row.description)
            try:
                decision_embedding = await self.embedder.embed(decision_template)
            except Exception:
                continue

            similarity = self._cosine_similarity(fact_embedding, decision_embedding)
            if similarity >= self.settings.cross_type_threshold:
                stmt = (
                    pg_insert(GraphEdge)
                    .values(
                        source_id=fact_id,
                        target_id=row.id,
                        source_type="fact",
                        target_type="decision",
                        agent_id=self.agent_id,
                        relation="evidence_for",
                        weight=float(similarity),
                        auto_linked=True,
                    )
                    .on_conflict_do_nothing(index_elements=["source_id", "target_id", "relation"])
                )
                await session.execute(stmt)
                edges.append(GraphEdgeInfo(
                    source_id=fact_id,
                    target_id=row.id,
                    source_type="fact",
                    target_type="decision",
                    relation="evidence_for",
                    weight=float(similarity),
                    auto_linked=True,
                ))

        return edges

    async def link_fact_to_facts(
        self,
        fact_id: UUID,
        fact_content: str,
        session: AsyncSession,
    ) -> list[GraphEdgeInfo]:
        """Find and link a new fact to similar existing facts via embedding similarity."""
        if not self.embedder or not self.settings.cross_type_linking_enabled:
            return []

        template_text = common_template_text("fact", fact_content)
        try:
            fact_embedding = await self.embedder.embed(template_text)
        except Exception:
            logger.warning("Failed to embed fact %s for fact-to-fact linking", fact_id)
            return []

        embedding_str = "[" + ",".join(str(float(v)) for v in fact_embedding) + "]"

        sql = text("""
            SELECT id, content,
                   1 - (embedding <=> CAST(:embedding AS vector)) AS similarity
            FROM heart.facts
            WHERE agent_id = :agent_id
              AND active = true
              AND id != :fact_id
              AND embedding IS NOT NULL
              AND 1 - (embedding <=> CAST(:embedding AS vector)) >= :threshold
            ORDER BY embedding <=> CAST(:embedding AS vector)
            LIMIT 5
        """)
        result = await session.execute(sql, {
            "embedding": embedding_str,
            "agent_id": self.agent_id,
            "fact_id": fact_id,
            "threshold": self.settings.cross_type_same_threshold * 0.9,
        })
        candidates = result.all()

        edges = []
        for row in candidates:
            if row.similarity >= self.settings.cross_type_same_threshold:
                stmt = (
                    pg_insert(GraphEdge)
                    .values(
                        source_id=fact_id,
                        target_id=row.id,
                        source_type="fact",
                        target_type="fact",
                        agent_id=self.agent_id,
                        relation="related_to",
                        weight=float(row.similarity),
                        auto_linked=True,
                    )
                    .on_conflict_do_nothing(index_elements=["source_id", "target_id", "relation"])
                )
                await session.execute(stmt)
                edges.append(GraphEdgeInfo(
                    source_id=fact_id,
                    target_id=row.id,
                    source_type="fact",
                    target_type="fact",
                    relation="related_to",
                    weight=float(row.similarity),
                    auto_linked=True,
                ))

        return edges

    async def link_episode_deterministic(
        self,
        episode_id: UUID,
        decision_ids: list[UUID],
        fact_ids: list[UUID],
        session: AsyncSession,
    ) -> list[GraphEdgeInfo]:
        """Create deterministic edges from episode to decisions and facts."""
        edges = []

        for dec_id in decision_ids:
            stmt = (
                pg_insert(GraphEdge)
                .values(
                    source_id=episode_id,
                    target_id=dec_id,
                    source_type="episode",
                    target_type="decision",
                    agent_id=self.agent_id,
                    relation="discussed_in",
                    weight=1.0,
                    auto_linked=True,
                )
                .on_conflict_do_nothing(index_elements=["source_id", "target_id", "relation"])
            )
            await session.execute(stmt)
            edges.append(GraphEdgeInfo(
                source_id=episode_id, target_id=dec_id,
                source_type="episode", target_type="decision",
                relation="discussed_in", weight=1.0, auto_linked=True,
            ))

        for fact_id in fact_ids:
            stmt = (
                pg_insert(GraphEdge)
                .values(
                    source_id=fact_id,
                    target_id=episode_id,
                    source_type="fact",
                    target_type="episode",
                    agent_id=self.agent_id,
                    relation="extracted_from",
                    weight=1.0,
                    auto_linked=True,
                )
                .on_conflict_do_nothing(index_elements=["source_id", "target_id", "relation"])
            )
            await session.execute(stmt)
            edges.append(GraphEdgeInfo(
                source_id=fact_id, target_id=episode_id,
                source_type="fact", target_type="episode",
                relation="extracted_from", weight=1.0, auto_linked=True,
            ))

        return edges

    @staticmethod
    def _cosine_similarity(a: list[float], b: list[float]) -> float:
        dot = sum(x * y for x, y in zip(a, b))
        norm_a = sum(x * x for x in a) ** 0.5
        norm_b = sum(x * x for x in b) ** 0.5
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return dot / (norm_a * norm_b)
