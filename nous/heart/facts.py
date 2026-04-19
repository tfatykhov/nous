"""Fact management — semantic memory (what we know).

Manages facts with provenance, deduplication, superseding, and contradiction.
All methods follow Brain's session injection pattern (P1-1).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from nous.brain.embeddings import EmbeddingProvider
from nous.heart.admission import AdmissionController, AdmissionResult
from nous.heart.schemas import ContradictionWarning, FactDetail, FactInput, FactRejected, FactSummary
from nous.heart.search import hybrid_search
from nous.storage.database import Database
from nous.storage.models import Episode, Event, Fact, GraphEdge

if TYPE_CHECKING:
    from nous.handlers import LLMClient
    from nous.heart.actionability import ActionabilityClassifier

logger = logging.getLogger(__name__)

# F027: JSON schema for structured supersession classifier output
_SUPERSESSION_CLASSIFIER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "relation": {
            "type": "string",
            "enum": ["UPDATE", "CONTRADICTION", "REFINEMENT", "UNRELATED"],
            "description": "How the two facts relate to each other",
        },
        "current_fact": {
            "type": "string",
            "enum": ["new", "old"],
            "description": "Which fact represents the current/correct state of affairs",
        },
        "confidence": {
            "type": "number",
            "minimum": 0.0,
            "maximum": 1.0,
            "description": "Confidence in this classification (0.0 to 1.0)",
        },
    },
    "required": ["relation", "current_fact", "confidence"],
}


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
        actionability_classifier: "ActionabilityClassifier | None" = None,
    ) -> None:
        self.db = db
        self.embeddings = embeddings
        self.agent_id = agent_id
        self._admission_controller = admission_controller
        # F027: LLM client for write-time supersession classifier
        self._llm: LLMClient | None = None
        self._llm_model: str = "claude-haiku-4-5-20251001"
        # F047: Actionability classifier for write-time verdict
        self._actionability_classifier = actionability_classifier

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
    # F027: LLM client setter
    # ------------------------------------------------------------------

    def set_llm_client(self, client: LLMClient, model: str | None = None) -> None:
        """F027: Configure LLM client for write-time supersession classifier."""
        self._llm = client
        if model:
            self._llm_model = model

    # ------------------------------------------------------------------
    # F027: Access tracking
    # ------------------------------------------------------------------

    async def track_access(self, fact_ids: list[UUID]) -> None:
        """Update recall_count and last_recalled_at for accessed facts."""
        if not fact_ids:
            return
        try:
            async with self.db.session() as session:
                stmt = (
                    update(Fact)
                    .where(Fact.id.in_(fact_ids))
                    .values(
                        recall_count=Fact.recall_count + 1,
                        last_recalled_at=datetime.now(UTC),
                    )
                )
                await session.execute(stmt)
                await session.commit()
        except Exception:
            logger.debug("F027: access tracking failed for %d facts", len(fact_ids))

    def _fire_track_access(self, fact_ids: list[UUID]) -> None:
        """Fire-and-forget access tracking."""
        if fact_ids:
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self.track_access(fact_ids))
            except RuntimeError:
                pass  # No running event loop

    # ------------------------------------------------------------------
    # F027: LLM supersession classifier
    # ------------------------------------------------------------------

    async def _classify_fact_pair(
        self, old_content: str, new_content: str
    ) -> dict | None:
        """Classify relationship between two facts using LLM.

        Returns {relation, current_fact, confidence} or None on failure.
        """
        if self._llm is None:
            return None

        from nous.handlers import call_background_llm_structured

        prompt = (
            f"Compare these two facts and classify their relationship.\n\n"
            f"OLD fact: {old_content[:500]}\n\n"
            f"NEW fact: {new_content[:500]}\n\n"
            f"Classify as:\n"
            f"- UPDATE: One replaces the other (newer info)\n"
            f"- CONTRADICTION: They disagree on the same topic\n"
            f"- REFINEMENT: New fact adds detail to old fact\n"
            f"- UNRELATED: Different topics despite surface similarity"
        )

        return await call_background_llm_structured(
            client=self._llm,
            model=self._llm_model,
            system_prompt="You are a memory management classifier. Analyze fact relationships precisely.",
            user_message=prompt,
            tool_name="classify_facts",
            tool_description="Classify the relationship between two facts.",
            output_schema=_SUPERSESSION_CLASSIFIER_SCHEMA,
            max_tokens=300,
        )

    # ------------------------------------------------------------------
    # F027: Retrieval soft suppression
    # ------------------------------------------------------------------

    @staticmethod
    def apply_supersession_filter(results: list[FactSummary]) -> list[FactSummary]:
        """Apply soft suppression to superseded facts in search results.

        - If superseded_by is set and superseder is in result set -> drop old fact
        - If superseded_by is set but superseder absent -> score *= 0.3
        - If confidence < 0.5 -> score *= confidence
        Uses model_copy for immutability. Re-sorts by adjusted score.
        """
        if not results:
            return results

        result_ids = {r.id for r in results}
        filtered: list[FactSummary] = []

        for r in results:
            if r.superseded_by is not None:
                if r.superseded_by in result_ids:
                    # Superseder present in results — drop old fact entirely
                    continue
                # Superseder absent — soft penalty
                adjusted_score = (r.score or 0.0) * 0.3
                r = r.model_copy(update={"score": adjusted_score})

            if r.confidence < 0.5:
                adjusted_score = (r.score or 0.0) * r.confidence
                r = r.model_copy(update={"score": adjusted_score})

            filtered.append(r)

        # Re-sort by adjusted score descending
        filtered.sort(key=lambda x: x.score or 0.0, reverse=True)
        return filtered

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
        # F038-1.2: Reject facts with content < 30 characters
        if len(input.content.strip()) < 30:
            return FactRejected(
                content=input.content,
                composite_score=0.0,
                threshold=0.0,
                scores={},
                explanation="Content too short (< 30 chars)",
            )

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

        # F047: Classify actionability at learn time.
        # Failure is non-fatal — fact still saved with actionable=NULL and
        # the heartbeat falls back to the legacy heuristic path for it.
        actionable_verdict: bool | None = None
        actionable_conf: float | None = None
        if self._actionability_classifier is not None:
            try:
                actionable_verdict, actionable_conf, _tier = await self._actionability_classifier.classify(
                    input.content,
                    input.category,
                    list(input.tags or []),
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("F047: actionability classify failed for new fact", exc_info=True)

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
            admission_scores=(
                admission_result.scores if admission_result and not admission_result.bypassed and admission_result.scores
                else None
            ),
            actionable=actionable_verdict,
            actionable_confidence=actionable_conf,
        )
        session.add(fact)
        await session.flush()

        # Subject + similarity supersession (006.2)
        if check_contradictions and input.subject and embedding is not None:
            await self._supersede_by_subject(
                fact.id, input.subject, embedding, session,
                new_content=input.content,
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
                contradiction = await self._find_contradiction(
                    embedding, fact.content, safe_excludes, session,
                    new_fact_id=fact.id,
                )
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
        new_fact_id: UUID | None = None,
    ) -> ContradictionWarning | None:
        """Detect potential contradictions: similar embedding (0.85-0.95) but different content.

        A contradiction is when two facts talk about the same thing but say
        different things. High similarity means same topic; below dedup
        threshold means different content.

        F027: When LLM is available, classifies the pair to route to the
        correct action (UNRELATED/REFINEMENT/UPDATE/CONTRADICTION).
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

        # F027: LLM classification for precise routing
        if self._llm is not None and new_fact_id is not None:
            classification = await self._classify_fact_pair(row.content, new_content)
            if classification:
                relation = classification.get("relation", "")
                current = classification.get("current_fact", "")
                conf = float(classification.get("confidence", 0.0))

                if relation == "UNRELATED":
                    return None

                if relation == "REFINEMENT":
                    await self._create_graph_edge(
                        new_fact_id, row.id, "fact", "fact",
                        "refines", 0.8, session,
                    )
                    return None

                if relation == "UPDATE" and conf >= 0.8:
                    if current == "new":
                        # Supersede old fact
                        old_fact = await self._get_fact_orm(row.id, session)
                        if old_fact:
                            old_fact.superseded_by = new_fact_id
                            old_fact.active = False
                            old_fact.confidence = max(0.0, (old_fact.confidence or 1.0) * 0.3)
                        await self._create_graph_edge(
                            new_fact_id, row.id, "fact", "fact",
                            "supersedes", 1.0, session,
                        )
                        return None
                    else:
                        # Old is current — deactivate new fact
                        new_fact = await self._get_fact_orm(new_fact_id, session)
                        if new_fact:
                            new_fact.active = False
                        return None

                if relation == "UPDATE" and conf < 0.8:
                    # Low confidence UPDATE — fall through to ContradictionWarning
                    pass

                if relation == "CONTRADICTION":
                    # P1 fix: set contradiction_of, create edge, reduce confidence
                    if new_fact_id:
                        new_fact = await self._get_fact_orm(new_fact_id, session)
                        if new_fact:
                            new_fact.contradiction_of = row.id
                    await self._create_graph_edge(
                        new_fact_id, row.id, "fact", "fact",
                        "contradicts", 1.0, session,
                    )
                    old_fact = await self._get_fact_orm(row.id, session)
                    if old_fact:
                        old_fact.confidence = max(0.0, (old_fact.confidence or 1.0) - 0.2)
                    # Fall through to return ContradictionWarning

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
        new_content: str = "",
    ) -> None:
        """Supersede older facts with same subject AND similar content (006.2).

        Only supersedes when both conditions are met:
        1. Same subject (case-insensitive exact match)
        2. Cosine similarity > 0.80 (content about same aspect)

        F027: For ambiguous range (0.80-0.95) with LLM available, use classifier
        to disambiguate UNRELATED/REFINEMENT/UPDATE before superseding.

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
                    # F027: LLM disambiguation for ambiguous range
                    if similarity <= 0.95 and self._llm is not None and new_content:
                        classification = await self._classify_fact_pair(
                            old.content, new_content
                        )
                        if classification:
                            relation = classification.get("relation", "")
                            current = classification.get("current_fact", "")
                            if relation == "UNRELATED":
                                continue  # Skip — not actually related
                            if relation == "REFINEMENT":
                                await self._create_graph_edge(
                                    new_fact_id, old.id, "fact", "fact",
                                    "refines", 0.8, session,
                                )
                                continue  # Keep both
                            if relation == "UPDATE" and current == "old":
                                # Old is current — deactivate new fact
                                new_fact = await self._get_fact_orm(new_fact_id, session)
                                if new_fact:
                                    new_fact.active = False
                                continue
                            # UPDATE + current=="new" or unknown → fall through to supersede

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
        """Retrieve source text for ROUGE-L grounding check.

        Priority: FactInput.source_text > Episode.transcript > Episode.summary.
        F025 P2-E: source_text passthrough avoids grounding against lossy summary.
        """
        # F025 P2-E: Use passed-through transcript if available
        if fact_input.source_text:
            return fact_input.source_text

        if not fact_input.source_episode_id:
            return None

        episode = await session.get(Episode, fact_input.source_episode_id)
        if not episode:
            return None

        # F025 P3-C: Prefer persisted transcript over lossy summary
        if episode.transcript:
            return episode.transcript
        if episode.summary:
            return episode.summary

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
                actionable=f.actionable,
                actionable_confidence=f.actionable_confidence,
                tags=list(f.tags or []),
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

        summaries = [
            FactSummary(
                id=f.id,
                content=f.content,
                category=f.category,
                subject=f.subject,
                confidence=f.confidence or 1.0,
                active=f.active if f.active is not None else True,
                score=scores.get(f.id),
                superseded_by=f.superseded_by,
                actionable=f.actionable,
                actionable_confidence=f.actionable_confidence,
                tags=list(f.tags or []),
            )
            for fid in ids
            if (f := facts.get(fid)) is not None
        ]

        # F027: Apply supersession filter and fire access tracking
        summaries = self.apply_supersession_filter(summaries)
        self._fire_track_access([s.id for s in summaries])
        return summaries

    async def _search_all(
        self,
        query: str,
        embedding: list[float] | None,
        limit: int,
        category: str | None,
        session: AsyncSession,
    ) -> list[FactSummary]:
        """Search all facts including inactive (no active filter).

        Uses RRF (Reciprocal Rank Fusion) for hybrid search — same approach
        as hybrid_search() but intentionally omits the active=true filter so
        superseded/inactive facts are included.
        """
        from nous.heart.search import _resolve_vector_weight, _resolve_rrf_k, _rrf_merge

        vw = _resolve_vector_weight()
        rrf_k = _resolve_rrf_k()

        params: dict = {
            "agent_id": self.agent_id,
            "query_text": query,
            "limit": limit,
            "limit_expanded": limit * 3,
        }
        filter_extra = ""
        if category:
            filter_extra = "AND t.category = :category"
            params["category"] = category

        vector_results: list[tuple] = []
        keyword_results: list[tuple] = []

        if embedding is not None:
            params["query_embedding"] = "[" + ",".join(str(float(v)) for v in embedding) + "]"
            vector_sql = text(f"""
                SELECT t.id, 1 - (t.embedding <=> CAST(:query_embedding AS vector)) AS score
                FROM heart.facts t
                WHERE t.embedding IS NOT NULL
                  AND t.agent_id = :agent_id {filter_extra}
                ORDER BY t.embedding <=> CAST(:query_embedding AS vector)
                LIMIT :limit_expanded
            """)
            result = await session.execute(vector_sql, params)
            vector_results = [(row.id, float(row.score)) for row in result.all()]

        keyword_sql = text(f"""
            SELECT t.id,
                ts_rank_cd(t.search_tsv, plainto_tsquery('english', :query_text))
                / (1.0 + ts_rank_cd(t.search_tsv, plainto_tsquery('english', :query_text))) AS score
            FROM heart.facts t
            WHERE t.search_tsv @@ plainto_tsquery('english', :query_text)
              AND t.agent_id = :agent_id {filter_extra}
            ORDER BY score DESC
            LIMIT :limit_expanded
        """)
        result = await session.execute(keyword_sql, params)
        keyword_results = [(row.id, float(row.score)) for row in result.all()]

        if embedding is None:
            ranked = keyword_results[:limit]
        else:
            ranked = _rrf_merge(vector_results, keyword_results, rrf_k, vw, limit)

        if not ranked:
            return []

        ids = [r[0] for r in ranked]
        scores = {r[0]: r[1] for r in ranked}

        fact_result = await session.execute(select(Fact).where(Fact.id.in_(ids)))
        facts = {f.id: f for f in fact_result.scalars().all()}

        summaries = [
            FactSummary(
                id=f.id,
                content=f.content,
                category=f.category,
                subject=f.subject,
                confidence=f.confidence or 1.0,
                active=f.active if f.active is not None else True,
                score=scores.get(f.id),
                superseded_by=f.superseded_by,
                actionable=f.actionable,
                actionable_confidence=f.actionable_confidence,
                tags=list(f.tags or []),
            )
            for fid in ids
            if (f := facts.get(fid)) is not None
        ]

        # F027: Apply supersession filter and fire access tracking
        summaries = self.apply_supersession_filter(summaries)
        self._fire_track_access([s.id for s in summaries])
        return summaries

    # ------------------------------------------------------------------
    # list_all() — F021 dashboard browse mode
    # ------------------------------------------------------------------

    async def list_all(
        self,
        limit: int = 50,
        offset: int = 0,
        category: str | None = None,
        active_only: bool = True,
        confidence_min: float | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        sort: str = "created_at",
        order: str = "desc",
        session: AsyncSession | None = None,
    ) -> tuple[list[FactSummary], int]:
        """Return paginated facts without search. Used by dashboard browse mode."""
        if session is None:
            async with self.db.session() as session:
                return await self._list_all(
                    limit, offset, category, active_only,
                    confidence_min, date_from, date_to, sort, order, session,
                )
        return await self._list_all(
            limit, offset, category, active_only,
            confidence_min, date_from, date_to, sort, order, session,
        )

    async def _list_all(
        self,
        limit: int,
        offset: int,
        category: str | None,
        active_only: bool,
        confidence_min: float | None,
        date_from: str | None,
        date_to: str | None,
        sort: str,
        order: str,
        session: AsyncSession,
    ) -> tuple[list[FactSummary], int]:
        from sqlalchemy import func as sa_func

        conditions = [Fact.agent_id == self.agent_id]
        if active_only:
            conditions.append(Fact.active == True)  # noqa: E712
        if category:
            conditions.append(Fact.category == category)
        if confidence_min is not None:
            conditions.append(Fact.confidence >= confidence_min)
        if date_from:
            conditions.append(Fact.created_at >= date_from)
        if date_to:
            conditions.append(Fact.created_at <= date_to)

        # Count
        count_q = select(sa_func.count()).select_from(Fact).where(*conditions)
        total = (await session.execute(count_q)).scalar() or 0

        # Sort — VALIDATE against allowlist to prevent attribute injection
        ALLOWED_SORTS = {"created_at", "confidence", "category", "subject"}
        if sort not in ALLOWED_SORTS:
            sort = "created_at"
        if order not in ("asc", "desc"):
            order = "desc"
        sort_col = getattr(Fact, sort)
        order_clause = sort_col.desc() if order == "desc" else sort_col.asc()

        # Fetch
        q = select(Fact).where(*conditions).order_by(order_clause).limit(limit).offset(offset)
        result = await session.execute(q)
        facts = list(result.scalars().all())

        # F047: FactSummary now carries tags + actionable; keep this site
        # in sync with _search / _search_all / _list_by_category.
        summaries = [
            FactSummary(
                id=f.id,
                content=f.content,
                category=f.category,
                subject=f.subject,
                confidence=f.confidence or 1.0,
                active=f.active if f.active is not None else True,
                actionable=f.actionable,
                actionable_confidence=f.actionable_confidence,
                tags=list(f.tags or []),
            )
            for f in facts
        ]
        return summaries, total

    # ------------------------------------------------------------------
    # count_stale() — F034: Heartbeat health check
    # ------------------------------------------------------------------

    async def count_stale(self, older_than_days: int = 30, session: AsyncSession | None = None) -> int:
        """Count active facts not updated in N days."""
        if session is None:
            async with self.db.session() as session:
                return await self._count_stale(older_than_days, session)
        return await self._count_stale(older_than_days, session)

    async def _count_stale(self, older_than_days: int, session: AsyncSession) -> int:
        from datetime import timedelta
        cutoff = datetime.now(UTC) - timedelta(days=older_than_days)
        result = await session.execute(
            select(func.count())
            .select_from(Fact)
            .where(Fact.agent_id == self.agent_id)
            .where(Fact.active == True)  # noqa: E712
            .where(Fact.updated_at < cutoff)
        )
        return result.scalar() or 0

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
            actionable=fact.actionable,
            actionable_confidence=fact.actionable_confidence,
        )

    # ------------------------------------------------------------------
    # find_contradiction_candidates() — F031
    # ------------------------------------------------------------------

    async def find_contradiction_candidates(
        self,
        limit: int = 10,
        session: AsyncSession | None = None,
    ) -> list[dict]:
        """Find active fact pairs with same subject and high embedding similarity.

        Returns dicts with: fact1_id, fact2_id, content1, content2, date1, date2, subject, category, similarity.
        These are contradiction candidates that slipped past write-time detection.
        Uses similarity range 0.75-0.95 (below 0.75 is unrelated, above 0.95 is near-dupe).
        """
        if session is None:
            async with self.db.session() as session:
                return await self._find_contradiction_candidates(limit, session)
        return await self._find_contradiction_candidates(limit, session)

    async def _find_contradiction_candidates(
        self,
        limit: int,
        session: AsyncSession,
    ) -> list[dict]:
        sql = text("""
            SELECT f1.id AS fact1_id, f2.id AS fact2_id,
                   f1.content AS content1, f2.content AS content2,
                   f1.created_at AS date1, f2.created_at AS date2,
                   f1.subject AS subject, f1.category AS category,
                   1 - (f1.embedding <=> f2.embedding) AS similarity
            FROM heart.facts f1
            JOIN heart.facts f2 ON f1.agent_id = f2.agent_id
              AND f1.id < f2.id
              AND f2.active = true
              AND f2.embedding IS NOT NULL
              AND f2.subject IS NOT NULL
              AND LOWER(f1.subject) = LOWER(f2.subject)
              AND 1 - (f1.embedding <=> f2.embedding) > 0.75
              AND 1 - (f1.embedding <=> f2.embedding) < 0.95
            WHERE f1.agent_id = :agent_id
              AND f1.active = true
              AND f1.embedding IS NOT NULL
              AND f1.subject IS NOT NULL
            ORDER BY similarity DESC
            LIMIT :limit
        """)
        result = await session.execute(sql, {"agent_id": self.agent_id, "limit": limit})
        return [
            {
                "fact1_id": row.fact1_id,
                "fact2_id": row.fact2_id,
                "content1": row.content1,
                "content2": row.content2,
                "date1": row.date1,
                "date2": row.date2,
                "subject": row.subject,
                "category": row.category,
                "similarity": float(row.similarity),
            }
            for row in result.all()
        ]
