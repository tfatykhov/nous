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

from nous.brain.edge_provenance import classify  # F065
from nous.brain.embeddings import EmbeddingProvider
from nous.brain.schemas import GraphEdgeInfo
from nous.config import Settings
from nous.storage.database import Database
from nous.storage.models import GraphEdge

logger = logging.getLogger(__name__)

# F040: Per-relation weight multipliers for edge confidence scoring.
# Certain relation types are inherently stronger signals than others.
RELATION_WEIGHT_MULTIPLIERS: dict[str, float] = {
    "supports": 1.0,
    "contradicts": 1.0,
    "supersedes": 0.9,
    "related_to": 0.8,
    "caused_by": 1.0,
    "informed_by": 0.9,
    "evidence_for": 1.0,
    "discussed_in": 0.7,
    "extracted_from": 0.7,
    # F070: chunk graph edges. `part_of` is a structural anchor (FK as edge);
    # we want the full passed weight to survive. `summarized_by` already has
    # cosine similarity baked into `weight` at the call site, so no extra
    # discount needed beyond the (already empirical) cosine gating.
    "part_of": 1.0,
    "summarized_by": 1.0,
}


def edge_confidence(
    similarity: float,
    shared_tags: int,
    shared_subject: bool,
    temporal_proximity_days: float,
) -> float:
    """Compute a multi-signal confidence score for a candidate edge.

    Combines embedding similarity with tag overlap, subject match, and
    temporal proximity into a single [0, 1] score.
    """
    score = similarity * 0.6
    score += min(shared_tags * 0.05, 0.15)
    score += 0.10 if shared_subject else 0.0
    score += max(0, 0.15 - temporal_proximity_days * 0.001)
    return min(score, 1.0)


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

    async def create_edge(
        self,
        source_id: UUID,
        target_id: UUID,
        source_type: str,
        target_type: str,
        relation: str,
        weight: float,
        session: AsyncSession,
        *,
        weight_multiplier_override: float | None = None,
        provenance_source: str = "auto_linker",
    ) -> GraphEdgeInfo | None:
        """Create a graph edge with relation-aware weight multiplier.

        Applies RELATION_WEIGHT_MULTIPLIERS to the raw weight and uses
        ON CONFLICT DO NOTHING to skip duplicates.  Returns the edge info
        on success, or None if the edge already exists.

        ``weight_multiplier_override`` skips the relation-table lookup for
        callers that need to bypass the per-relation discount — e.g. F070
        sequential chunk adjacency, which is structural (weight=1.0) but
        reuses the ``related_to`` relation (multiplier 0.8). Without the
        override, the persisted weight would be 0.8, not 1.0.

        ``provenance_source`` is the writer-identity tag forwarded to F065's
        ``classify(relation, source=...)`` to set ``extraction_method``.
        Default ``"auto_linker"`` preserves existing behavior. Pass
        ``"structural"`` for FK-derived / index-derived anchors (F070
        chunk→episode part_of and chunk→chunk sequential adjacency) so
        ``graph_inferred_edge_penalty`` does not down-weight them.
        """
        multiplier = (
            weight_multiplier_override
            if weight_multiplier_override is not None
            else RELATION_WEIGHT_MULTIPLIERS.get(relation, 0.8)
        )
        adjusted_weight = weight * multiplier

        stmt = (
            pg_insert(GraphEdge)
            .values(
                source_id=source_id,
                target_id=target_id,
                source_type=source_type,
                target_type=target_type,
                agent_id=self.agent_id,
                relation=relation,
                weight=adjusted_weight,
                auto_linked=True,
                extraction_method=classify(relation, source=provenance_source),  # F065
            )
            .on_conflict_do_nothing(index_elements=["source_id", "target_id", "relation"])
        )
        result = await session.execute(stmt)

        if result.rowcount == 0:
            # F044-STC-HOOK: conflict = this edge was re-derived. Reinforce its
            # LTP counter — but only for LIVE similarity links. Deterministic
            # structural rebuilds (F070 chunk anchors) re-derive the same edges
            # every cycle and would inflate the counter uniformly, so they are
            # excluded by provenance.
            if (
                getattr(self.settings, "tinyhippo_lite_enabled", False)
                and provenance_source != "structural"
            ):
                from nous.brain.tinyhippo_lite import increment_ltp_on_rederivation
                await increment_ltp_on_rederivation(
                    session, source_id, target_id, relation
                )
            return None

        return GraphEdgeInfo(
            source_id=source_id,
            target_id=target_id,
            source_type=source_type,
            target_type=target_type,
            relation=relation,
            weight=adjusted_weight,
            auto_linked=True,
        )

    async def link_fact_to_decisions(
        self,
        fact_id: UUID,
        fact_content: str,
        session: AsyncSession,
    ) -> list[GraphEdgeInfo]:
        """Find and link a new fact to related decisions via common-template re-embedding."""
        if not self.embedder or not self.settings.cross_type_linking_enabled:
            return []

        # F022 audit (2026-04-30): empty/near-empty source content was the
        # dominant cause of NO/WEAK edge verdicts. Skip the link entirely
        # when the new fact is too short to anchor a reliable relation.
        min_chars = self.settings.cross_type_link_min_content_chars
        if min_chars > 0 and len((fact_content or "").strip()) < min_chars:
            return []

        template_text = common_template_text("fact", fact_content)
        try:
            fact_embedding = await self.embedder.embed(template_text)
        except Exception:
            logger.warning("Failed to embed fact %s for cross-type linking", fact_id)
            return []

        embedding_str = "[" + ",".join(str(float(v)) for v in fact_embedding) + "]"

        cutoff = datetime.now(UTC) - timedelta(days=30)
        # F022 audit fix: gate candidate decisions on description length so
        # rows with empty/near-empty bodies never enter the link set.
        sql = text("""
            SELECT id, description,
                   1 - (embedding <=> CAST(:embedding AS vector)) AS similarity
            FROM brain.decisions
            WHERE agent_id = :agent_id
              AND embedding IS NOT NULL
              AND created_at >= :cutoff
              AND 1 - (embedding <=> CAST(:embedding AS vector)) >= :threshold
              AND length(coalesce(description, '')) >= :min_chars
            ORDER BY embedding <=> CAST(:embedding AS vector)
            LIMIT 5
        """)
        result = await session.execute(sql, {
            "embedding": embedding_str,
            "agent_id": self.agent_id,
            "cutoff": cutoff,
            "threshold": self.settings.cross_type_threshold * 0.9,
            "min_chars": min_chars,
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
                        extraction_method=classify("evidence_for", source="auto_linker"),
                    )
                    .on_conflict_do_nothing(index_elements=["source_id", "target_id", "relation"])
                )
                res = await session.execute(stmt)
                if res.rowcount == 0 and getattr(
                    self.settings, "tinyhippo_lite_enabled", False
                ):
                    # F044-STC-HOOK: conflict = re-derived live evidence_for edge.
                    # This upsert path bypasses create_edge(), so reinforce here too.
                    from nous.brain.tinyhippo_lite import increment_ltp_on_rederivation
                    await increment_ltp_on_rederivation(
                        session, fact_id, row.id, "evidence_for"
                    )
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

        # F022 audit guard: short source content cannot anchor a reliable
        # related_to edge. Same threshold as link_fact_to_decisions.
        min_chars = self.settings.cross_type_link_min_content_chars
        if min_chars > 0 and len((fact_content or "").strip()) < min_chars:
            return []

        template_text = common_template_text("fact", fact_content)
        try:
            fact_embedding = await self.embedder.embed(template_text)
        except Exception:
            logger.warning("Failed to embed fact %s for fact-to-fact linking", fact_id)
            return []

        embedding_str = "[" + ",".join(str(float(v)) for v in fact_embedding) + "]"

        # Gate candidates on content length to mirror the source-side guard.
        sql = text("""
            SELECT id, content,
                   1 - (embedding <=> CAST(:embedding AS vector)) AS similarity
            FROM heart.facts
            WHERE agent_id = :agent_id
              AND active = true
              AND id != :fact_id
              AND embedding IS NOT NULL
              AND 1 - (embedding <=> CAST(:embedding AS vector)) >= :threshold
              AND length(coalesce(content, '')) >= :min_chars
            ORDER BY embedding <=> CAST(:embedding AS vector)
            LIMIT 5
        """)
        result = await session.execute(sql, {
            "embedding": embedding_str,
            "agent_id": self.agent_id,
            "fact_id": fact_id,
            "threshold": self.settings.cross_type_same_threshold * 0.9,
            "min_chars": min_chars,
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
                        extraction_method=classify("related_to", source="auto_linker"),
                    )
                    .on_conflict_do_nothing(index_elements=["source_id", "target_id", "relation"])
                )
                res = await session.execute(stmt)
                if res.rowcount == 0 and getattr(
                    self.settings, "tinyhippo_lite_enabled", False
                ):
                    # F044-STC-HOOK: conflict = re-derived live related_to edge.
                    # This upsert path bypasses create_edge(), so reinforce here too.
                    from nous.brain.tinyhippo_lite import increment_ltp_on_rederivation
                    await increment_ltp_on_rederivation(
                        session, fact_id, row.id, "related_to"
                    )
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
                    # F065: explicit episode-token reference, NOT cosine-derived.
                    # source="structural" override yields 'deterministic'.
                    extraction_method=classify("discussed_in", source="structural"),
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
                    # F065: explicit episode-token reference, NOT cosine-derived.
                    # source="structural" override yields 'deterministic'.
                    extraction_method=classify("extracted_from", source="structural"),
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
