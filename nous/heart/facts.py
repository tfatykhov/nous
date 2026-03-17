"""Fact management — semantic memory (what we know).

Manages facts with provenance, deduplication, superseding, and contradiction.
All methods follow Brain's session injection pattern (P1-1).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from nous.brain.embeddings import EmbeddingProvider
from nous.heart.admission import AdmissionController, AdmissionResult
from nous.heart.schemas import ContradictionWarning, FactDetail, FactInput, FactRejected, FactSummary
from nous.heart.search import hybrid_search
from nous.storage.database import Database
from nous.storage.models import Episode, Event, Fact, GraphEdge

logger = logging.getLogger(__name__)


class FactManager:
    """Manages semantic memory — what we know."""

    # Threshold for domain fact count before emitting compaction event
    DOMAIN_COMPACTION_THRESHOLD = 10

    # Re-emit threshold event every N facts above threshold
    DOMAIN_COMPACTION_INTERVAL = 5

    # Similarity range for contradiction detection (between dedup and unrelated)
    CONTRADICTION_SIMILARITY_MIN = 0.85
    CONTRADICTION_SIMILARITY_MAX = 0.95  # Above this is dedup, not contradiction

    def __init__(
        self,
        db: Database,
        embeddings: EmbeddingProvider | None,
        agent_id: str,
        admission_controller: AdmissionController | None = None,
    ) -> None:
        self.db = db
        self.embeddings = embeddings
        self.agent_id = agent_id
        self._admission_controller = admission_controller

    # ------------------------------------------------------------------
    # Event helper
    # ------------------------------------------------------------------

    async def _emit_event(self, session: AsyncSession, event_type: str, data: dict) -> None:
        """Insert event in same session (P2-1)."""
        event = Event(
            agent_id=self.agent_id,
            event_type=event_type,
            data=data,
        )
        session.add(event)

    async def _create_graph_edge(
        self,
        source_id: UUID,
        target_id: UUID,
        source_type: str,
        target_type: str,
        relation: str,
        weight: float,
        session: AsyncSession,
    ) -> None:
        """F022: Create a graph edge as side effect of fact operations.

        Uses a nested savepoint so failures don't abort the outer transaction.
        """
        try:
            async with session.begin_nested():
                stmt = (
                    pg_insert(GraphEdge)
                    .values(
                        source_id=source_id,
                        target_id=target_id,
                        source_type=source_type,
                        target_type=target_type,
                        agent_id=self.agent_id,
                        relation=relation,
                        weight=weight,
                        auto_linked=True,
                    )
                    .on_conflict_do_nothing(
                        index_elements=["source_id", "target_id", "relation"],
                    )
                )
                await session.execute(stmt)
        except Exception:
            logger.debug("F022 graph edge creation failed for %s->%s", source_id, target_id)

    # ------------------------------------------------------------------
    # learn()
    # ------------------------------------------------------------------

    async def learn(
        self,
        input: FactInput,
        exclude_ids: list[UUID] | None = None,
        check_contradictions: bool = True,
        session: AsyncSession | None = None,
        encoded_frame: str | None = None,
        encoded_censors: list[str] | None = None,
    ) -> FactDetail | FactRejected:
        """Store a new fact with deduplication.

        Args:
            input: Fact data to store.
            exclude_ids: Fact IDs to exclude from dedup check (P1-2).
                Used by supersede/contradict to avoid matching the old fact.
            check_contradictions: Whether to check for contradictions and
                domain thresholds. Set False for bulk imports. Default True.
            session: Optional session for transaction injection.
            encoded_frame: Frame active when this fact was learned (003.2).
            encoded_censors: Censors active when this fact was learned (003.2).
        """
        if session is None:
            async with self.db.session() as session:
                result = await self._learn(
                    input,
                    list(exclude_ids or []),
                    check_contradictions,
                    session,
                    encoded_frame=encoded_frame,
                    encoded_censors=encoded_censors,
                )
                await session.commit()
                return result
        return await self._learn(
            input,
            list(exclude_ids or []),
            check_contradictions,
            session,
            encoded_frame=encoded_frame,
            encoded_censors=encoded_censors,
        )

    async def _learn(
        self,
        input: FactInput,
        exclude_ids: list[UUID],
        check_contradictions: bool,
        session: AsyncSession,
        *,
        encoded_frame: str | None = None,
        encoded_censors: list[str] | None = None,
    ) -> FactDetail | FactRejected:
        # Generate embedding
        embedding = None
        if self.embeddings:
            try:
                embedding = await self.embeddings.embed(input.content)
            except Exception:
                logger.warning("Embedding generation failed for fact learn")

        # Near-duplicate detection: cosine similarity > 0.95
        if embedding is not None:
            dupe = await self._find_duplicate(embedding, exclude_ids, session)
            if dupe is not None:
                # Confirm existing fact instead of creating new
                return await self._confirm(dupe.id, session)

        # F023: Admission gate — score candidate before storage
        admission_result: AdmissionResult | None = None
        if self._admission_controller is not None:
            max_sim = await self._find_max_similarity(embedding, exclude_ids, session) if embedding else None
            source_text = await self._get_source_text(input, session)

            admission_result = await self._admission_controller.score(
                input, embedding, max_sim, source_text, session
            )
            if not admission_result.admitted:
                logger.info(
                    "Fact rejected by admission: %s — %s",
                    input.content[:80], admission_result.explanation,
                )
                await self._emit_event(session, "fact_rejected", {
                    "content": input.content[:200],
                    "source": input.source,
                    "scores": admission_result.scores,
                    "composite_score": admission_result.composite_score,
                })
                return FactRejected(
                    content=input.content,
                    composite_score=admission_result.composite_score,
                    threshold=admission_result.threshold,
                    scores=admission_result.scores,
                    explanation=admission_result.explanation,
                )

        fact = Fact(
            agent_id=self.agent_id,
            content=input.content,
            category=input.category,
            subject=input.subject,
            confidence=input.confidence,
            source=input.source,
            source_episode_id=input.source_episode_id,
            source_decision_id=input.source_decision_id,
            contradiction_of=input.contradiction_of,
            tags=input.tags or None,
            embedding=embedding,
            encoded_frame=encoded_frame,
            encoded_censors=encoded_censors,
            admission_score=admission_result.composite_score if admission_result else None,
        )
        session.add(fact)
        await session.flush()

        # Subject + similarity supersession (006.2)
        if check_contradictions and input.subject and embedding is not None:
            await self._supersede_by_subject(
                fact.id, input.subject, embedding, session
            )

        await self._emit_event(
            session,
            "fact_learned",
            {
                "fact_id": str(fact.id),
                "category": input.category,
                "subject": input.subject,
            },
        )

        detail = self._to_detail(fact)

        if check_contradictions:
            # Contradiction detection: similarity 0.85-0.95 with different content
            if embedding is not None:
                safe_excludes = list(exclude_ids) + [fact.id]
                contradiction = await self._find_contradiction(embedding, fact.content, safe_excludes, session)
                if contradiction is not None:
                    detail.contradiction_warning = contradiction
                    logger.info(
                        "Contradiction detected for fact %s: similar to %s (%.2f)",
                        fact.id,
                        contradiction.existing_fact_id,
                        contradiction.similarity,
                    )

            # Domain compaction check: emit event if too many facts in same category
            if input.category:
                await self._check_domain_threshold(input.category, session)

        return detail

    async def _find_contradiction(
        self,
        embedding: list[float],
        new_content: str,
        exclude_ids: list[UUID],
        session: AsyncSession,
    ) -> ContradictionWarning | None:
        """Detect potential contradictions: similar embedding (0.85-0.95) but different content.

        A contradiction is when two facts talk about the same thing but say
        different things. High similarity means same topic; below dedup
        threshold means different content.
        """
        if not embedding:
            return None

        embedding_str = "[" + ",".join(str(float(v)) for v in embedding) + "]"

        params: dict = {
            "embedding": embedding_str,
            "agent_id": self.agent_id,
            "sim_min": self.CONTRADICTION_SIMILARITY_MIN,
            "sim_max": self.CONTRADICTION_SIMILARITY_MAX,
        }

        exclude_clause = ""
        if exclude_ids:
            placeholders = ", ".join(f":excl_{i}" for i in range(len(exclude_ids)))
            exclude_clause = f"AND id NOT IN ({placeholders})"
            for i, eid in enumerate(exclude_ids):
                params[f"excl_{i}"] = eid

        sql = text(f"""
            SELECT id, content,
                   1 - (embedding <=> CAST(:embedding AS vector)) AS similarity
            FROM heart.facts
            WHERE agent_id = :agent_id
              AND active = true
              AND embedding IS NOT NULL
              AND 1 - (embedding <=> CAST(:embedding AS vector)) > :sim_min
              AND 1 - (embedding <=> CAST(:embedding AS vector)) <= :sim_max
              {exclude_clause}
            ORDER BY embedding <=> CAST(:embedding AS vector)
            LIMIT 1
        """)

        result = await session.execute(sql, params)
        row = result.first()
        if row is None:
            return None

        return ContradictionWarning(
            existing_fact_id=row.id,
            existing_content=row.content[:500],
            similarity=float(row.similarity),
            message=f"Potential contradiction detected (similarity {row.similarity:.2f}). "
            f"Existing fact: '{row.content[:100]}' — review and resolve.",
        )

    async def _check_domain_threshold(
        self,
        category: str,
        session: AsyncSession,
    ) -> None:
        """Emit event if active fact count in a category exceeds threshold.

        To avoid event spam (P1-1 fix), only emits when count first crosses
        the threshold or at every DOMAIN_COMPACTION_INTERVAL facts above it.
        """
        sql = text("""
            SELECT COUNT(*) AS cnt
            FROM heart.facts
            WHERE agent_id = :agent_id
              AND category = :category
              AND active = true
        """)
        result = await session.execute(sql, {"agent_id": self.agent_id, "category": category})
        count = result.scalar() or 0

        if count <= self.DOMAIN_COMPACTION_THRESHOLD:
            return

        # Only emit at threshold+1, threshold+1+interval, threshold+1+2*interval, ...
        excess = count - self.DOMAIN_COMPACTION_THRESHOLD
        if excess == 1 or excess % self.DOMAIN_COMPACTION_INTERVAL == 0:
            await self._emit_event(
                session,
                "fact_threshold_exceeded",
                {
                    "category": category,
                    "count": count,
                    "threshold": self.DOMAIN_COMPACTION_THRESHOLD,
                },
            )

    async def _supersede_by_subject(
        self,
        new_fact_id: UUID,
        subject: str,
        embedding: list[float],
        session: AsyncSession,
    ) -> None:
        """Supersede older facts with same subject AND similar content (006.2).

        Only supersedes when both conditions are met:
        1. Same subject (case-insensitive exact match)
        2. Cosine similarity > 0.80 (content about same aspect)

        This prevents "Nous version 0.2" from nuking "Nous uses PostgreSQL"
        while correctly superseding "Nous version 0.1".
        """
        result = await session.execute(
            select(Fact).where(
                Fact.agent_id == self.agent_id,
                Fact.active == True,  # noqa: E712
                func.lower(Fact.subject) == subject.lower(),
                Fact.id != new_fact_id,
            )
        )
        for old in result.scalars().all():
            if old.embedding is not None:
                similarity = self._cosine_similarity(embedding, old.embedding)
                if similarity > 0.80:
                    old.active = False
                    old.superseded_by = new_fact_id
                    logger.info(
                        "Superseded fact %s (subject=%s, sim=%.2f) by %s",
                        old.id, subject, similarity, new_fact_id,
                    )

    @staticmethod
    def _cosine_similarity(a: list[float], b: list[float]) -> float:
        """Cosine similarity between two embedding vectors."""
        dot = sum(x * y for x, y in zip(a, b))
        norm_a = sum(x * x for x in a) ** 0.5
        norm_b = sum(x * x for x in b) ** 0.5
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return dot / (norm_a * norm_b)

    async def _find_duplicate(
        self,
        embedding: list[float],
        exclude_ids: list[UUID],
        session: AsyncSession,
    ) -> Fact | None:
        """Find a near-duplicate fact by cosine similarity > 0.95."""
        embedding_str = "[" + ",".join(str(float(v)) for v in embedding) + "]"

        # Build exclude clause (P1-2)
        exclude_clause = ""
        params: dict = {
            "embedding": embedding_str,
            "agent_id": self.agent_id,
            "threshold": 0.95,
        }
        if exclude_ids:
            placeholders = ", ".join(f":excl_{i}" for i in range(len(exclude_ids)))
            exclude_clause = f"AND id NOT IN ({placeholders})"
            for i, eid in enumerate(exclude_ids):
                params[f"excl_{i}"] = eid

        sql = text(f"""
            SELECT id, 1 - (embedding <=> CAST(:embedding AS vector)) AS similarity
            FROM heart.facts
            WHERE agent_id = :agent_id
              AND active = true
              AND embedding IS NOT NULL
              AND 1 - (embedding <=> CAST(:embedding AS vector)) > :threshold
              {exclude_clause}
            ORDER BY embedding <=> CAST(:embedding AS vector)
            LIMIT 1
        """)

        result = await session.execute(sql, params)
        row = result.first()
        if row is None:
            return None

        # Fetch the ORM object
        fact_result = await session.execute(select(Fact).where(Fact.id == row.id))
        return fact_result.scalars().first()

    async def _find_max_similarity(
        self,
        embedding: list[float],
        exclude_ids: list[UUID],
        session: AsyncSession,
    ) -> float | None:
        """Find highest cosine similarity to any existing active fact.

        Used by admission controller for novelty scoring.
        Returns None if no facts exist or no embedding available.
        """
        if not embedding:
            return None

        embedding_str = "[" + ",".join(str(float(v)) for v in embedding) + "]"
        params: dict = {"embedding": embedding_str, "agent_id": self.agent_id}

        exclude_clause = ""
        if exclude_ids:
            placeholders = ", ".join(f":excl_{i}" for i in range(len(exclude_ids)))
            exclude_clause = f"AND id NOT IN ({placeholders})"
            for i, eid in enumerate(exclude_ids):
                params[f"excl_{i}"] = eid

        sql = text(f"""
            SELECT 1 - (embedding <=> CAST(:embedding AS vector)) AS similarity
            FROM heart.facts
            WHERE agent_id = :agent_id
              AND active = true
              AND embedding IS NOT NULL
              {exclude_clause}
            ORDER BY embedding <=> CAST(:embedding AS vector)
            LIMIT 1
        """)

        result = await session.execute(sql, params)
        row = result.first()
        return float(row.similarity) if row else None

    async def _get_source_text(
        self,
        fact_input: FactInput,
        session: AsyncSession,
    ) -> str | None:
        """Retrieve original source text for ROUGE-L grounding check.

        Fetches episode.content by PK if source_episode_id present.
        Episode content includes tool call outputs (web_search, bash, etc.).
        """
        if not fact_input.source_episode_id:
            return None

        episode = await session.get(Episode, fact_input.source_episode_id)
        if episode and episode.content:
            return episode.content

        return None

    # ------------------------------------------------------------------
    # confirm()
    # ------------------------------------------------------------------

    async def confirm(self, fact_id: UUID, session: AsyncSession | None = None) -> FactDetail:
        """Confirm a fact is still true."""
        if session is None:
            async with self.db.session() as session:
                result = await self._confirm(fact_id, session)
                await session.commit()
                return result
        return await self._confirm(fact_id, session)

    async def _confirm(self, fact_id: UUID, session: AsyncSession) -> FactDetail:
        fact = await self._get_fact_orm(fact_id, session)
        if fact is None:
            raise ValueError(f"Fact {fact_id} not found")

        # P2-9: NULL-safe counter increment
        fact.confirmation_count = (fact.confirmation_count or 0) + 1
        fact.last_confirmed = datetime.now(UTC)
        await session.flush()

        await self._emit_event(
            session,
            "fact_confirmed",
            {
                "fact_id": str(fact_id),
                "confirmation_count": fact.confirmation_count,
            },
        )

        return self._to_detail(fact)

    # ------------------------------------------------------------------
    # supersede()
    # ------------------------------------------------------------------

    async def supersede(
        self,
        old_fact_id: UUID,
        new_fact: FactInput,
        session: AsyncSession | None = None,
    ) -> FactDetail:
        """Replace a fact with a newer version."""
        if session is None:
            async with self.db.session() as session:
                result = await self._supersede(old_fact_id, new_fact, session)
                await session.commit()
                return result
        return await self._supersede(old_fact_id, new_fact, session)

    async def _supersede(
        self,
        old_fact_id: UUID,
        new_fact: FactInput,
        session: AsyncSession,
    ) -> FactDetail:
        # Verify old fact exists
        old_fact = await self._get_fact_orm(old_fact_id, session)
        if old_fact is None:
            raise ValueError(f"Fact {old_fact_id} not found")

        # F023: Bypass admission gate for intentional replacements
        bypass_input = new_fact.model_copy(update={"source": "supersede"})
        new_detail = await self._learn(bypass_input, [old_fact_id], False, session)
        if isinstance(new_detail, FactRejected):
            raise RuntimeError("Supersede bypass failed — admission should not reject bypassed sources")

        # Update old fact
        old_fact.superseded_by = new_detail.id
        old_fact.active = False
        await session.flush()

        # F022: Bridge — also create graph edge
        await self._create_graph_edge(
            new_detail.id, old_fact_id, "fact", "fact", "supersedes", 1.0, session
        )

        await self._emit_event(
            session,
            "fact_superseded",
            {
                "old_fact_id": str(old_fact_id),
                "new_fact_id": str(new_detail.id),
            },
        )

        return new_detail

    # ------------------------------------------------------------------
    # contradict()
    # ------------------------------------------------------------------

    async def contradict(
        self,
        fact_id: UUID,
        contradicting_fact: FactInput,
        session: AsyncSession | None = None,
    ) -> FactDetail:
        """Store a fact that contradicts an existing one."""
        if session is None:
            async with self.db.session() as session:
                result = await self._contradict(fact_id, contradicting_fact, session)
                await session.commit()
                return result
        return await self._contradict(fact_id, contradicting_fact, session)

    async def _contradict(
        self,
        fact_id: UUID,
        contradicting_fact: FactInput,
        session: AsyncSession,
    ) -> FactDetail:
        # Verify target fact exists
        old_fact = await self._get_fact_orm(fact_id, session)
        if old_fact is None:
            raise ValueError(f"Fact {fact_id} not found")

        # F023: Bypass admission gate for intentional contradictions
        bypass_input = contradicting_fact.model_copy(update={"source": "contradict"})
        new_detail = await self._learn(bypass_input, [fact_id], False, session)
        if isinstance(new_detail, FactRejected):
            raise RuntimeError("Contradict bypass failed — admission should not reject bypassed sources")

        # Set contradiction_of on the new fact
        new_fact_orm = await self._get_fact_orm(new_detail.id, session)
        if new_fact_orm is not None:
            new_fact_orm.contradiction_of = fact_id
            await session.flush()

        # F022: Bridge — also create graph edge
        await self._create_graph_edge(
            new_detail.id, fact_id, "fact", "fact", "contradicts", 1.0, session
        )

        # Reduce confidence of old fact by 0.2 (min 0.0)
        old_confidence = old_fact.confidence or 1.0
        old_fact.confidence = max(0.0, old_confidence - 0.2)
        await session.flush()

        # Re-read new fact to get updated contradiction_of
        updated = await self._get_fact_orm(new_detail.id, session)
        return self._to_detail(updated)

    # ------------------------------------------------------------------
    # get()
    # ------------------------------------------------------------------

    async def get(self, fact_id: UUID, session: AsyncSession | None = None) -> FactDetail | None:
        """Fetch a single fact."""
        if session is None:
            async with self.db.session() as session:
                return await self._get(fact_id, session)
        return await self._get(fact_id, session)

    async def _get(self, fact_id: UUID, session: AsyncSession) -> FactDetail | None:
        fact = await self._get_fact_orm(fact_id, session)
        if fact is None:
            return None
        return self._to_detail(fact)

    # ------------------------------------------------------------------
    # list_by_category() — Tier 1 always-on facts
    # ------------------------------------------------------------------

    async def list_by_category(
        self,
        categories: list[str],
        active_only: bool = True,
        limit: int = 20,
        session: AsyncSession | None = None,
    ) -> list[FactSummary]:
        """Load facts by category without semantic search.

        Used for Tier 1 always-on context (preference, person, rule facts).
        """
        if session is None:
            async with self.db.session() as session:
                return await self._list_by_category(categories, active_only, limit, session)
        return await self._list_by_category(categories, active_only, limit, session)

    async def _list_by_category(
        self,
        categories: list[str],
        active_only: bool,
        limit: int,
        session: AsyncSession,
    ) -> list[FactSummary]:
        stmt = (
            select(Fact)
            .where(
                Fact.agent_id == self.agent_id,
                Fact.category.in_(categories),
            )
        )
        if active_only:
            stmt = stmt.where(Fact.active == True)  # noqa: E712
        stmt = stmt.order_by(Fact.confidence.desc()).limit(limit)
        result = await session.execute(stmt)
        facts = result.scalars().all()
        return [
            FactSummary(
                id=f.id,
                content=f.content,
                category=f.category,
                subject=f.subject,
                confidence=f.confidence or 1.0,
                active=f.active if f.active is not None else True,
                score=1.0,  # Tier 1: always-on, no relevance ranking
            )
            for f in facts
        ]

    # ------------------------------------------------------------------
    # search()
    # ------------------------------------------------------------------

    async def search(
        self,
        query: str,
        limit: int = 10,
        category: str | None = None,
        active_only: bool = True,
        exclude_categories: list[str] | None = None,
        session: AsyncSession | None = None,
    ) -> list[FactSummary]:
        """Hybrid search over facts."""
        if session is None:
            async with self.db.session() as session:
                return await self._search(query, limit, category, active_only, exclude_categories, session)
        return await self._search(query, limit, category, active_only, exclude_categories, session)

    async def _search(
        self,
        query: str,
        limit: int,
        category: str | None,
        active_only: bool,
        exclude_categories: list[str] | None,
        session: AsyncSession,
    ) -> list[FactSummary]:
        # Generate query embedding
        embedding = None
        if self.embeddings:
            try:
                embedding = await self.embeddings.embed(query)
            except Exception:
                logger.warning("Embedding generation failed for fact search")

        extra_where = ""
        extra_params: dict = {}
        if category:
            extra_where += " AND t.category = :category"
            extra_params["category"] = category
        if exclude_categories:
            # Tier 3: exclude Tier 1 categories from semantic search
            placeholders = ", ".join(f":exc_{i}" for i in range(len(exclude_categories)))
            extra_where += f" AND (t.category IS NULL OR t.category NOT IN ({placeholders}))"
            for i, cat in enumerate(exclude_categories):
                extra_params[f"exc_{i}"] = cat

        # Note: hybrid_search always applies active=true filter.
        # For active_only=False, we need a different approach.
        if not active_only:
            # Override the default active filter by using raw search
            # The hybrid_search helper always filters active=true,
            # so for inactive facts we do a simpler query.
            return await self._search_all(query, embedding, limit, category, session)

        results = await hybrid_search(
            session=session,
            table="heart.facts",
            embedding=embedding,
            query_text=query,
            agent_id=self.agent_id,
            extra_where=extra_where,
            extra_params=extra_params,
            limit=limit,
        )

        if not results:
            return []

        ids = [r[0] for r in results]
        scores = {r[0]: r[1] for r in results}

        fact_result = await session.execute(select(Fact).where(Fact.id.in_(ids)))
        facts = {f.id: f for f in fact_result.scalars().all()}

        return [
            FactSummary(
                id=f.id,
                content=f.content,
                category=f.category,
                subject=f.subject,
                confidence=f.confidence or 1.0,
                active=f.active if f.active is not None else True,
                score=scores.get(f.id),
            )
            for fid in ids
            if (f := facts.get(fid)) is not None
        ]

    async def _search_all(
        self,
        query: str,
        embedding: list[float] | None,
        limit: int,
        category: str | None,
        session: AsyncSession,
    ) -> list[FactSummary]:
        """Search all facts including inactive (no active filter)."""
        params: dict = {
            "agent_id": self.agent_id,
            "query_text": query,
            "limit": limit,
        }
        filter_extra = ""
        if category:
            filter_extra = "AND t.category = :category"
            params["category"] = category

        if embedding is not None:
            params["query_embedding"] = "[" + ",".join(str(float(v)) for v in embedding) + "]"
            sql = text(f"""
                SELECT t.id,
                    (COALESCE(1 - (t.embedding <=> CAST(:query_embedding AS vector)), 0) * 0.7
                     + COALESCE(
                         ts_rank_cd(t.search_tsv, plainto_tsquery('english', :query_text))
                         / (1.0 + ts_rank_cd(t.search_tsv, plainto_tsquery('english', :query_text))),
                       0) * 0.3
                    ) AS combined_score
                FROM heart.facts t
                WHERE t.agent_id = :agent_id {filter_extra}
                  AND (t.embedding IS NOT NULL OR t.search_tsv @@ plainto_tsquery('english', :query_text))
                ORDER BY combined_score DESC
                LIMIT :limit
            """)
        else:
            sql = text(f"""
                SELECT t.id,
                    ts_rank_cd(t.search_tsv, plainto_tsquery('english', :query_text))
                    / (1.0 + ts_rank_cd(t.search_tsv, plainto_tsquery('english', :query_text))) AS score
                FROM heart.facts t
                WHERE t.search_tsv @@ plainto_tsquery('english', :query_text)
                  AND t.agent_id = :agent_id {filter_extra}
                ORDER BY score DESC
                LIMIT :limit
            """)

        result = await session.execute(sql, params)
        rows = result.all()
        if not rows:
            return []

        ids = [row.id for row in rows]
        scores = {row.id: float(row[1]) for row in rows}

        fact_result = await session.execute(select(Fact).where(Fact.id.in_(ids)))
        facts = {f.id: f for f in fact_result.scalars().all()}

        return [
            FactSummary(
                id=f.id,
                content=f.content,
                category=f.category,
                subject=f.subject,
                confidence=f.confidence or 1.0,
                active=f.active if f.active is not None else True,
                score=scores.get(f.id),
            )
            for fid in ids
            if (f := facts.get(fid)) is not None
        ]

    # ------------------------------------------------------------------
    # get_current() — P3-5: recursive CTE
    # ------------------------------------------------------------------

    async def get_current(self, fact_id: UUID, session: AsyncSession | None = None) -> FactDetail:
        """Follow superseded_by chain to find current version of a fact."""
        if session is None:
            async with self.db.session() as session:
                return await self._get_current(fact_id, session)
        return await self._get_current(fact_id, session)

    async def _get_current(self, fact_id: UUID, session: AsyncSession) -> FactDetail:
        sql = text("""
            WITH RECURSIVE chain AS (
                SELECT id, superseded_by, 1 AS depth
                FROM heart.facts
                WHERE id = :start_id AND agent_id = :agent_id
                UNION ALL
                SELECT f.id, f.superseded_by, c.depth + 1
                FROM heart.facts f
                JOIN chain c ON f.id = c.superseded_by
                WHERE c.depth < 10
            )
            SELECT id FROM chain WHERE superseded_by IS NULL
        """)

        result = await session.execute(sql, {"start_id": fact_id, "agent_id": self.agent_id})
        row = result.first()
        if row is None:
            raise ValueError(f"Fact {fact_id} not found")

        current_fact = await self._get_fact_orm(row.id, session)
        if current_fact is None:
            raise ValueError(f"Current fact for {fact_id} not found")

        return self._to_detail(current_fact)

    # ------------------------------------------------------------------
    # deactivate()
    # ------------------------------------------------------------------

    async def deactivate(self, fact_id: UUID, session: AsyncSession | None = None) -> None:
        """Soft-delete a fact."""
        if session is None:
            async with self.db.session() as session:
                await self._deactivate(fact_id, session)
                await session.commit()
                return
        await self._deactivate(fact_id, session)

    async def _deactivate(self, fact_id: UUID, session: AsyncSession) -> None:
        fact = await self._get_fact_orm(fact_id, session)
        if fact is None:
            raise ValueError(f"Fact {fact_id} not found")
        fact.active = False
        await session.flush()

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _get_fact_orm(self, fact_id: UUID, session: AsyncSession) -> Fact | None:
        """Fetch Fact ORM scoped by agent_id."""
        result = await session.execute(select(Fact).where(Fact.id == fact_id).where(Fact.agent_id == self.agent_id))
        return result.scalars().first()

    def _to_detail(self, fact: Fact) -> FactDetail:
        """Convert ORM Fact to FactDetail DTO."""
        return FactDetail(
            id=fact.id,
            agent_id=fact.agent_id,
            content=fact.content,
            category=fact.category,
            subject=fact.subject,
            confidence=fact.confidence or 1.0,
            source=fact.source,
            source_episode_id=fact.source_episode_id,
            source_decision_id=fact.source_decision_id,
            learned_at=fact.learned_at,
            last_confirmed=fact.last_confirmed,
            confirmation_count=fact.confirmation_count or 0,
            superseded_by=fact.superseded_by,
            contradiction_of=fact.contradiction_of,
            active=fact.active if fact.active is not None else True,
            tags=fact.tags or [],
            created_at=fact.created_at,
        )
