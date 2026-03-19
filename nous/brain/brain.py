"""Main Brain class — public API for decision intelligence.

All methods are async and accept an optional session parameter for test
fixture compatibility (P1-2). When session is None, the method creates
its own session from the database connection pool.
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import delete, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from nous.brain.bridge import BridgeExtractor
from nous.brain.calibration import CalibrationEngine
from nous.brain.embeddings import EmbeddingProvider
from nous.brain.guardrails import GuardrailEngine
from nous.brain.quality import QualityScorer
from nous.brain.schemas import (
    BridgeInfo,
    CalibrationReport,
    DecisionDetail,
    DecisionSummary,
    GraphEdgeInfo,
    GuardrailResult,
    NeighborResult,
    ReasonInput,
    RecordInput,
    ReviewInput,
    ThoughtInfo,
)
from nous.config import Settings
from nous.storage.database import Database
from nous.storage.models import (
    CalibrationSnapshot,
    Decision,
    DecisionBridge,
    DecisionReason,
    DecisionTag,
    Event,
    GraphEdge,
    Thought,
)

logger = logging.getLogger(__name__)

# Noise indicators — short descriptions with no alternatives/reasoning signal
_NOISE_KEYWORDS = frozenset({
    "completed", "done", "finished", "success", "started",
    "status", "progress", "update", "checked", "confirmed",
})


class Brain:
    """Decision intelligence organ for Nous agents."""

    def __init__(
        self,
        database: Database,
        settings: Settings,
        embedding_provider: EmbeddingProvider | None = None,
    ) -> None:
        self.db = database
        self.settings = settings
        self.embeddings = embedding_provider
        self.quality = QualityScorer()
        self.guardrails = GuardrailEngine()
        self.calibration = CalibrationEngine()
        self.bridge_extractor = BridgeExtractor()
        self.agent_id = settings.agent_id

    # --- Lifecycle (P2-11) ---

    async def close(self) -> None:
        """Close owned resources (embedding provider httpx client)."""
        if self.embeddings:
            await self.embeddings.close()

    async def __aenter__(self) -> Brain:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()

    # ------------------------------------------------------------------
    # list_decisions()
    # ------------------------------------------------------------------

    async def list_decisions(
        self,
        limit: int = 20,
        offset: int = 0,
        agent_id: str | None = None,
        category: str | None = None,
        stakes: str | None = None,
        outcome: str | None = None,
        confidence_min: float | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        reviewed: bool | None = None,
        sort: str = "created_at",
        order: str = "desc",
        session: AsyncSession | None = None,
    ) -> tuple[list[DecisionSummary], int]:
        """List decisions with optional filters. Returns (decisions, total_count)."""
        if session is None:
            async with self.db.session() as session:
                return await self._list_decisions(
                    limit, offset, agent_id, category, stakes, outcome,
                    confidence_min, date_from, date_to, reviewed, sort, order, session,
                )
        return await self._list_decisions(
            limit, offset, agent_id, category, stakes, outcome,
            confidence_min, date_from, date_to, reviewed, sort, order, session,
        )

    async def _list_decisions(
        self,
        limit: int,
        offset: int,
        agent_id: str | None,
        category: str | None,
        stakes: str | None,
        outcome: str | None,
        confidence_min: float | None,
        date_from: str | None,
        date_to: str | None,
        reviewed: bool | None,
        sort: str,
        order: str,
        session: AsyncSession,
    ) -> tuple[list[DecisionSummary], int]:
        from sqlalchemy import func as sa_func

        _agent_id = agent_id or self.agent_id

        conditions = [Decision.agent_id == _agent_id]
        if category:
            conditions.append(Decision.category == category)
        if stakes:
            conditions.append(Decision.stakes == stakes)
        if outcome:
            conditions.append(Decision.outcome == outcome)
        if confidence_min is not None:
            conditions.append(Decision.confidence >= confidence_min)
        if date_from:
            conditions.append(Decision.created_at >= date_from)
        if date_to:
            conditions.append(Decision.created_at <= date_to)
        if reviewed is True:
            conditions.append(Decision.reviewed_at.isnot(None))
        elif reviewed is False:
            conditions.append(Decision.reviewed_at.is_(None))

        # Count
        count_q = select(sa_func.count()).select_from(Decision).where(*conditions)
        total = (await session.execute(count_q)).scalar() or 0

        # Sort — VALIDATE against allowlist to prevent attribute injection
        ALLOWED_SORTS = {"created_at", "confidence", "category", "stakes"}
        if sort not in ALLOWED_SORTS:
            sort = "created_at"
        if order not in ("asc", "desc"):
            order = "desc"
        sort_col = getattr(Decision, sort)
        order_clause = sort_col.desc() if order == "desc" else sort_col.asc()

        # Fetch
        result = await session.execute(
            select(Decision).where(*conditions).order_by(order_clause).limit(limit).offset(offset)
        )
        decisions = list(result.scalars().all())

        if not decisions:
            return [], total

        # Fetch tags (P2-17: separate query)
        decision_ids = [d.id for d in decisions]
        tag_result = await session.execute(select(DecisionTag).where(DecisionTag.decision_id.in_(decision_ids)))
        tags_by_id: dict[UUID, list[str]] = defaultdict(list)
        for t in tag_result.scalars().all():
            tags_by_id[t.decision_id].append(t.tag)

        summaries = [
            DecisionSummary(
                id=d.id,
                description=d.description,
                confidence=d.confidence,
                category=d.category,
                stakes=d.stakes,
                outcome=d.outcome or "pending",
                pattern=d.pattern,
                tags=tags_by_id.get(d.id, []),
                created_at=d.created_at,
            )
            for d in decisions
        ]
        return summaries, total

    # ------------------------------------------------------------------
    # get_recent_decisions() — 009.5
    # ------------------------------------------------------------------

    async def get_recent_decisions(
        self,
        agent_id: str,
        since: datetime,
        limit: int = 5,
        session_id: str | None = None,
        session: AsyncSession | None = None,
    ) -> list[DecisionSummary]:
        """Fetch recent decisions since a cutoff time, optionally scoped to session (009.5)."""
        if session is None:
            async with self.db.session() as session:
                return await self._get_recent_decisions(agent_id, since, limit, session_id, session)
        return await self._get_recent_decisions(agent_id, since, limit, session_id, session)

    async def _get_recent_decisions(
        self,
        agent_id: str,
        since: datetime,
        limit: int,
        session_id: str | None,
        session: AsyncSession,
    ) -> list[DecisionSummary]:
        stmt = (
            select(Decision)
            .where(
                Decision.agent_id == agent_id,
                Decision.created_at >= since,
            )
            .order_by(Decision.created_at.desc())
            .limit(limit)
        )
        if session_id is not None:
            stmt = stmt.where(Decision.session_id == session_id)

        result = await session.execute(stmt)
        decisions = list(result.scalars().all())

        return [
            DecisionSummary(
                id=d.id,
                description=d.description,
                confidence=d.confidence,
                category=d.category,
                stakes=d.stakes,
                outcome=d.outcome or "pending",
                pattern=d.pattern,
                tags=[],
                created_at=d.created_at,
            )
            for d in decisions
        ]

    # ------------------------------------------------------------------
    # record()
    # ------------------------------------------------------------------

    async def record(self, input: RecordInput, session: AsyncSession | None = None) -> DecisionDetail:
        """Record a new decision with all associated data."""
        if session is None:
            async with self.db.session() as session:
                result = await self._record(input, session)
                await session.commit()
                return result
        return await self._record(input, session)

    def _is_noise_decision(self, description: str, reasons: list[ReasonInput]) -> bool:
        """Lightweight pre-check to detect obvious non-decisions.

        Returns True if the description looks like a status report
        rather than a real decision. Checks:
        1. Very short description (<20 chars) with no reasons
        2. Description is mostly noise keywords with no reasoning

        This is a SOFT filter — it logs a warning but does not block.
        The frame instruction changes (C1) are the primary fix.
        """
        desc_lower = description.lower().strip()

        # Very short with no reasons — almost certainly noise
        if len(desc_lower) < 20 and not reasons:
            return True

        # P1-4: Use regex tokenization to strip punctuation
        # "completed." -> "completed" (matches _NOISE_KEYWORDS)
        words = set(re.findall(r'\w+', desc_lower))
        if not words:
            return True
        noise_count = len(words & _NOISE_KEYWORDS)
        # If >50% of words are noise keywords and no reasons provided
        if noise_count / len(words) > 0.5 and not reasons:
            return True

        return False

    async def _record(self, input: RecordInput, session: AsyncSession) -> DecisionDetail:
        """Internal record implementation using provided session.

        Steps 4-7 use ORM cascade — single session.add(decision) inserts
        the decision, tags, reasons, and bridge together (P1-1).
        """
        # C2: Noise check — warn but still record (soft filter)
        if self._is_noise_decision(input.description, input.reasons):
            logger.warning(
                "Possible noise decision detected: '%s' — "
                "consider if this is a real choice between alternatives",
                input.description[:80],
            )

        # 1. Compute quality score
        reasons_dicts = [r.model_dump() for r in input.reasons]
        quality_score = self.quality.compute(
            tags=input.tags,
            reasons=reasons_dicts,
            pattern=input.pattern,
            context=input.context,
        )

        # 2. Generate embedding (P1-6: graceful degradation)
        embedding = None
        if self.embeddings:
            embed_text = f"{input.description} {input.context or ''} {input.pattern or ''}".strip()
            try:
                embedding = await self.embeddings.embed(embed_text)
            except Exception:
                logger.warning("Embedding generation failed, recording without embedding")

        # 3. Extract bridge
        bridge_info = self.bridge_extractor.extract(input.description, input.context, input.pattern)

        # 4-7. Insert decision with cascade-populated relationships
        decision = Decision(
            agent_id=self.agent_id,
            description=input.description,
            context=input.context,
            pattern=input.pattern,
            confidence=input.confidence,
            category=input.category,
            stakes=input.stakes,
            quality_score=quality_score,
            embedding=embedding,
            session_id=input.session_id,
        )

        # Populate relationships for ORM cascade
        decision.tags = [DecisionTag(tag=t) for t in input.tags]
        decision.reasons = [DecisionReason(type=r.type, text=r.text) for r in input.reasons]
        if bridge_info.structure or bridge_info.function:
            decision.bridge = DecisionBridge(
                structure=bridge_info.structure,
                function=bridge_info.function,
            )

        session.add(decision)
        await session.flush()  # Populate server-generated fields (id, created_at, etc.)

        # 9. Emit event (P2-9: same session as main operation)
        await self._emit_event(
            session,
            "decision_recorded",
            {"decision_id": str(decision.id), "category": input.category},
        )

        # 8. Auto-link (isolated in nested savepoint + try/except — P1-1)
        # Nested savepoint ensures SQL errors in auto_link don't abort the
        # parent transaction.
        try:
            async with session.begin_nested():
                await self._auto_link(decision.id, session)
        except Exception:
            logger.warning("auto_link failed for decision %s, continuing", decision.id)

        # Re-fetch with eager loading to avoid lazy-load issues
        decision = await self._get_decision_orm(decision.id, session)
        return self._decision_to_detail(decision)

    # ------------------------------------------------------------------
    # update()
    # ------------------------------------------------------------------

    async def update(
        self,
        decision_id: UUID,
        description: str | None = None,
        context: str | None = None,
        pattern: str | None = None,
        confidence: float | None = None,
        tags: list[str] | None = None,
        session: AsyncSession | None = None,
    ) -> DecisionDetail:
        """Update a decision's description, context, pattern, confidence, or tags."""
        if session is None:
            async with self.db.session() as session:
                result = await self._update(decision_id, description, context, pattern, confidence, tags, session)
                await session.commit()
                return result
        return await self._update(decision_id, description, context, pattern, confidence, tags, session)

    async def _update(
        self,
        decision_id: UUID,
        description: str | None,
        context: str | None,
        pattern: str | None,
        confidence: float | None,
        tags: list[str] | None,
        session: AsyncSession,
    ) -> DecisionDetail:
        """Internal update implementation.

        Re-computes quality using current tags/reasons with the updated fields.
        When tags are provided, replaces existing DecisionTag rows entirely.
        """
        decision = await self._get_decision_orm(decision_id, session)
        if decision is None:
            raise ValueError(f"Decision {decision_id} not found")

        changed = False
        if description is not None:
            decision.description = description
            changed = True
        if context is not None:
            decision.context = context
            changed = True
        if pattern is not None:
            decision.pattern = pattern
            changed = True
        if confidence is not None:
            decision.confidence = confidence
            changed = True
        if tags is not None:
            # Replace existing tags: delete old, insert new
            await session.execute(delete(DecisionTag).where(DecisionTag.decision_id == decision_id))
            decision.tags = [DecisionTag(tag=t) for t in tags]
            changed = True

        if not changed:
            return self._decision_to_detail(decision)

        # Re-compute quality with updated fields + current tags/reasons
        current_tags = tags if tags is not None else [t.tag for t in decision.tags]
        current_reasons = [{"type": r.type, "text": r.text} for r in decision.reasons]
        decision.quality_score = self.quality.compute(
            tags=current_tags,
            reasons=current_reasons,
            pattern=decision.pattern,
            context=decision.context,
        )

        # Re-generate embedding if text changed (P1-6: graceful degradation)
        if self.embeddings:
            embed_text = f"{decision.description} {decision.context or ''} {decision.pattern or ''}".strip()
            try:
                decision.embedding = await self.embeddings.embed(embed_text)
            except Exception:
                logger.warning("Embedding re-generation failed during update")

        # Re-extract bridge
        bridge_info = self.bridge_extractor.extract(decision.description, decision.context, decision.pattern)
        if decision.bridge:
            decision.bridge.structure = bridge_info.structure
            decision.bridge.function = bridge_info.function
        elif bridge_info.structure or bridge_info.function:
            decision.bridge = DecisionBridge(
                structure=bridge_info.structure,
                function=bridge_info.function,
            )

        await session.flush()

        # Emit event (P2-9)
        await self._emit_event(
            session,
            "decision_updated",
            {"decision_id": str(decision_id)},
        )

        return self._decision_to_detail(decision)

    # ------------------------------------------------------------------
    # delete()
    # ------------------------------------------------------------------

    async def delete(
        self,
        decision_id: UUID,
        session: AsyncSession | None = None,
    ) -> None:
        """Delete a decision and its related records (tags, reasons, thoughts).

        Used to clean up deliberation records for non-decisions (informational
        responses that were pre-registered but turned out not to be decisions).
        """
        if session is None:
            async with self.db.session() as session:
                await self._delete(decision_id, session)
                await session.commit()
                return
        await self._delete(decision_id, session)

    async def _delete(self, decision_id: UUID, session: AsyncSession) -> None:
        """Internal delete — cascading removal of decision + related records.

        Most FK references use CASCADE (auto-handled by Postgres).
        Two NO ACTION FKs need explicit NULL-out: heart.facts.source_decision_id
        and heart.censors.learned_from_decision.
        """
        # NULL-out NO ACTION FK references in heart tables
        await session.execute(
            text("UPDATE heart.facts SET source_decision_id = NULL WHERE source_decision_id = :did"),
            {"did": decision_id},
        )
        await session.execute(
            text("UPDATE heart.censors SET learned_from_decision = NULL WHERE learned_from_decision = :did"),
            {"did": decision_id},
        )
        # Delete the decision — CASCADE handles brain.thoughts, decision_tags,
        # decision_reasons, decision_bridge, graph_edges, episode_decisions
        await session.execute(
            text("DELETE FROM brain.decisions WHERE id = :did"),
            {"did": decision_id},
        )

    # ------------------------------------------------------------------
    # think()
    # ------------------------------------------------------------------

    async def think(
        self,
        decision_id: UUID,
        text_content: str,
        session: AsyncSession | None = None,
    ) -> ThoughtInfo:
        """Attach a deliberation thought to a decision."""
        if session is None:
            async with self.db.session() as session:
                result = await self._think(decision_id, text_content, session)
                await session.commit()
                return result
        return await self._think(decision_id, text_content, session)

    async def _think(
        self,
        decision_id: UUID,
        text_content: str,
        session: AsyncSession,
    ) -> ThoughtInfo:
        thought = Thought(
            decision_id=decision_id,
            agent_id=self.agent_id,
            text=text_content,
        )
        session.add(thought)
        await session.flush()
        return ThoughtInfo(
            id=thought.id,
            text=thought.text,
            created_at=thought.created_at,
        )

    # ------------------------------------------------------------------
    # get()
    # ------------------------------------------------------------------

    async def get(self, decision_id: UUID, session: AsyncSession | None = None) -> DecisionDetail | None:
        """Fetch a single decision with all relations."""
        if session is None:
            async with self.db.session() as session:
                return await self._get(decision_id, session)
        return await self._get(decision_id, session)

    async def _get(self, decision_id: UUID, session: AsyncSession) -> DecisionDetail | None:
        decision = await self._get_decision_orm(decision_id, session)
        if decision is None:
            return None
        return self._decision_to_detail(decision)

    # ------------------------------------------------------------------
    # query() — hybrid search
    # ------------------------------------------------------------------

    async def query(
        self,
        query_text: str,
        limit: int = 10,
        category: str | None = None,
        stakes: str | None = None,
        outcome: str | None = None,
        bridge_side: str | None = None,
        session: AsyncSession | None = None,
    ) -> list[DecisionSummary]:
        """Perform hybrid search (vector + keyword) with optional filters."""
        if session is None:
            async with self.db.session() as session:
                return await self._query(query_text, limit, category, stakes, outcome, bridge_side, session)
        return await self._query(query_text, limit, category, stakes, outcome, bridge_side, session)

    async def _query(
        self,
        query_text: str,
        limit: int,
        category: str | None,
        stakes: str | None,
        outcome: str | None,
        bridge_side: str | None,
        session: AsyncSession,
    ) -> list[DecisionSummary]:
        """Internal query implementation.

        Hybrid search with normalized scores (P2-8), filters inside CTEs (P2-16),
        separate tag query (P2-17), keyword-only weight=1.0 (P2-14),
        bridge_side as ILIKE (P2-10), raw SQL for search_tsv (P2-21).
        """
        # Generate embedding for query (P1-6: graceful degradation)
        query_embedding = None
        if self.embeddings:
            try:
                query_embedding = await self.embeddings.embed(query_text)
            except Exception:
                logger.warning("Embedding generation failed for query, falling back to keyword-only")

        # Build filter clause fragments
        filter_clauses = "AND d.agent_id = :agent_id"
        if category:
            filter_clauses += " AND d.category = :category"
        if stakes:
            filter_clauses += " AND d.stakes = :stakes"
        if outcome:
            filter_clauses += " AND d.outcome = :outcome"
        else:
            # Exclude abandoned decisions (outcome='failure', confidence=0.0)
            # unless caller explicitly requests a specific outcome
            filter_clauses += " AND NOT (d.outcome = 'failure' AND d.confidence = 0.0)"

        # Bridge-side filter (P2-10: ILIKE on bridge columns)
        bridge_join = ""
        if bridge_side in ("structure", "function"):
            bridge_join = f"""
                JOIN brain.decision_bridge db ON db.decision_id = d.id
                    AND db.{bridge_side} ILIKE '%' || :query_text || '%'
            """

        params: dict = {
            "agent_id": self.agent_id,
            "query_text": query_text,
            "limit": limit,
            "limit_expanded": limit * 3,
        }
        if category:
            params["category"] = category
        if stakes:
            params["stakes"] = stakes
        if outcome:
            params["outcome"] = outcome

        if query_embedding is not None:
            # Full hybrid search (P2-8: normalized ts_rank_cd)
            # Format as pgvector string without spaces
            params["query_embedding"] = "[" + ",".join(str(float(v)) for v in query_embedding) + "]"
            sql = text(f"""
                WITH semantic AS (
                    SELECT d.id, 1 - (d.embedding <=> CAST(:query_embedding AS vector)) AS score
                    FROM brain.decisions d
                    {bridge_join}
                    WHERE d.embedding IS NOT NULL {filter_clauses}
                    ORDER BY d.embedding <=> CAST(:query_embedding AS vector)
                    LIMIT :limit_expanded
                ),
                keyword AS (
                    SELECT d.id,
                        ts_rank_cd(d.search_tsv, plainto_tsquery('english', :query_text))
                        / (1.0 + ts_rank_cd(d.search_tsv, plainto_tsquery('english', :query_text))) AS score
                    FROM brain.decisions d
                    {bridge_join}
                    WHERE d.search_tsv @@ plainto_tsquery('english', :query_text)
                        {filter_clauses}
                    LIMIT :limit_expanded
                )
                SELECT COALESCE(s.id, k.id) AS id,
                    (COALESCE(s.score, 0) * 0.7 + COALESCE(k.score, 0) * 0.3) AS combined_score
                FROM semantic s
                FULL OUTER JOIN keyword k ON s.id = k.id
                ORDER BY combined_score DESC
                LIMIT :limit
            """)
        else:
            # Keyword-only fallback (P2-14: weight=1.0)
            sql = text(f"""
                SELECT d.id,
                    ts_rank_cd(d.search_tsv, plainto_tsquery('english', :query_text))
                    / (1.0 + ts_rank_cd(d.search_tsv, plainto_tsquery('english', :query_text))) AS score
                FROM brain.decisions d
                {bridge_join}
                WHERE d.search_tsv @@ plainto_tsquery('english', :query_text)
                    {filter_clauses}
                ORDER BY score DESC
                LIMIT :limit
            """)

        result = await session.execute(sql, params)
        rows = result.all()

        if not rows:
            return []

        decision_ids = [row.id for row in rows]
        scores_by_id = {
            row.id: float(row.combined_score if hasattr(row, "combined_score") else row.score) for row in rows
        }

        # Fetch decision data
        decisions_result = await session.execute(select(Decision).where(Decision.id.in_(decision_ids)))
        decisions = {d.id: d for d in decisions_result.scalars().all()}

        # Separate tag query (P2-17)
        tag_result = await session.execute(select(DecisionTag).where(DecisionTag.decision_id.in_(decision_ids)))
        tags_by_id: dict[UUID, list[str]] = defaultdict(list)
        for tag_row in tag_result.scalars().all():
            tags_by_id[tag_row.decision_id].append(tag_row.tag)

        # Build results preserving search order
        summaries = []
        for did in decision_ids:
            d = decisions.get(did)
            if d is None:
                continue
            summaries.append(
                DecisionSummary(
                    id=d.id,
                    description=d.description,
                    confidence=d.confidence,
                    category=d.category,
                    stakes=d.stakes,
                    outcome=d.outcome or "pending",
                    pattern=d.pattern,
                    tags=tags_by_id.get(d.id, []),
                    score=scores_by_id.get(d.id),
                    created_at=d.created_at,
                )
            )

        return summaries

    # ------------------------------------------------------------------
    # check()
    # ------------------------------------------------------------------

    async def check(
        self,
        description: str,
        stakes: str,
        confidence: float,
        category: str | None = None,
        tags: list[str] | None = None,
        reasons: list[dict] | None = None,
        pattern: str | None = None,
        quality_score: float | None = None,
        context: dict | None = None,
        session: AsyncSession | None = None,
    ) -> GuardrailResult:
        """Evaluate guardrails before action.

        Args:
            context: Arbitrary key-value dict accessible as decision.context in CEL.
                     Used to pass custom fields for guardrail evaluation.
        """
        if session is None:
            async with self.db.session() as session:
                result = await self.guardrails.check(
                    session,
                    self.agent_id,
                    description=description,
                    stakes=stakes,
                    confidence=confidence,
                    category=category,
                    tags=tags,
                    reasons=reasons,
                    pattern=pattern,
                    quality_score=quality_score,
                    context=context,
                )
                await session.commit()
                return result
        return await self.guardrails.check(
            session,
            self.agent_id,
            description=description,
            stakes=stakes,
            confidence=confidence,
            category=category,
            tags=tags,
            reasons=reasons,
            pattern=pattern,
            quality_score=quality_score,
            context=context,
        )

    # ------------------------------------------------------------------
    # review()
    # ------------------------------------------------------------------

    async def review(
        self,
        decision_id: UUID,
        outcome: str,
        result: str | None = None,
        reviewer: str | None = None,
        session: AsyncSession | None = None,
    ) -> DecisionDetail:
        """Record outcome for a decision."""
        if session is None:
            async with self.db.session() as session:
                detail = await self._review(decision_id, outcome, result, reviewer, session)
                await session.commit()
                return detail
        return await self._review(decision_id, outcome, result, reviewer, session)

    async def _review(
        self,
        decision_id: UUID,
        outcome: str,
        result_text: str | None,
        reviewer: str | None,
        session: AsyncSession,
    ) -> DecisionDetail:
        # Validate via Pydantic (P2-18)
        validated = ReviewInput(outcome=outcome, result=result_text, reviewer=reviewer)

        decision = await self._get_decision_orm(decision_id, session)
        if decision is None:
            raise ValueError(f"Decision {decision_id} not found")

        decision.outcome = validated.outcome
        decision.outcome_result = validated.result
        decision.reviewed_at = datetime.now(UTC)
        decision.reviewer = validated.reviewer

        await session.flush()

        # Emit event (P2-9)
        await self._emit_event(
            session,
            "decision_reviewed",
            {
                "decision_id": str(decision_id),
                "outcome": validated.outcome,
                "reviewer": validated.reviewer,
            },
        )

        return self._decision_to_detail(decision)

    # ------------------------------------------------------------------
    # get_session_decisions()
    # ------------------------------------------------------------------

    async def get_session_decisions(
        self, session_id: str, session: AsyncSession | None = None,
    ) -> list[DecisionSummary]:
        """Fetch decisions made during a specific session."""
        if session is None:
            async with self.db.session() as session:
                return await self._get_session_decisions(session_id, session)
        return await self._get_session_decisions(session_id, session)

    async def _get_session_decisions(
        self, session_id: str, session: AsyncSession,
    ) -> list[DecisionSummary]:
        stmt = (
            select(Decision)
            .where(Decision.agent_id == self.agent_id, Decision.session_id == session_id)
            .order_by(Decision.created_at)
        )
        result = await session.execute(stmt)
        return [self._decision_to_summary(d) for d in result.scalars().all()]

    # ------------------------------------------------------------------
    # get_unreviewed()
    # ------------------------------------------------------------------

    async def get_unreviewed(
        self, max_age_days: int = 30, stakes: str | None = None,
        session: AsyncSession | None = None,
    ) -> list[DecisionSummary]:
        """Fetch unreviewed decisions, optionally filtered by stakes."""
        if session is None:
            async with self.db.session() as session:
                return await self._get_unreviewed(max_age_days, stakes, session)
        return await self._get_unreviewed(max_age_days, stakes, session)

    async def _get_unreviewed(
        self, max_age_days: int, stakes: str | None, session: AsyncSession,
    ) -> list[DecisionSummary]:
        cutoff = datetime.now(UTC) - timedelta(days=max_age_days)
        stmt = (
            select(Decision)
            .where(
                Decision.agent_id == self.agent_id,
                Decision.reviewed_at.is_(None),
                Decision.created_at >= cutoff,
            )
            .order_by(Decision.created_at)
        )
        if stakes:
            stmt = stmt.where(Decision.stakes == stakes)
        result = await session.execute(stmt)
        return [self._decision_to_summary(d) for d in result.scalars().all()]

    # ------------------------------------------------------------------
    # generate_calibration_snapshot()
    # ------------------------------------------------------------------

    async def generate_calibration_snapshot(
        self, session: AsyncSession | None = None,
    ) -> CalibrationReport:
        """Compute calibration metrics and store a snapshot."""
        if session is None:
            async with self.db.session() as session:
                report = await self.calibration.compute(session, self.agent_id)
                snapshot = CalibrationSnapshot(
                    agent_id=self.agent_id,
                    total_decisions=report.total_decisions,
                    reviewed_decisions=report.reviewed_decisions,
                    brier_score=report.brier_score,
                    accuracy=report.accuracy,
                    confidence_mean=report.confidence_mean,
                    confidence_stddev=report.confidence_stddev,
                    category_stats=report.category_stats,
                    reason_stats=report.reason_type_stats,
                )
                session.add(snapshot)
                await session.commit()
                return report
        report = await self.calibration.compute(session, self.agent_id)
        snapshot = CalibrationSnapshot(
            agent_id=self.agent_id,
            total_decisions=report.total_decisions,
            reviewed_decisions=report.reviewed_decisions,
            brier_score=report.brier_score,
            accuracy=report.accuracy,
            confidence_mean=report.confidence_mean,
            confidence_stddev=report.confidence_stddev,
            category_stats=report.category_stats,
            reason_stats=report.reason_type_stats,
        )
        session.add(snapshot)
        await session.flush()
        return report

    # ------------------------------------------------------------------
    # get_episode_for_decision()
    # ------------------------------------------------------------------

    async def get_episode_for_decision(
        self, decision_id: UUID, session: AsyncSession | None = None,
    ):
        """Get the episode linked to a decision via episode_decisions table."""
        if session is None:
            async with self.db.session() as session:
                return await self._get_episode_for_decision(decision_id, session)
        return await self._get_episode_for_decision(decision_id, session)

    async def _get_episode_for_decision(self, decision_id: UUID, session: AsyncSession):
        from nous.storage.models import EpisodeDecision, Episode
        stmt = (
            select(Episode)
            .join(EpisodeDecision, Episode.id == EpisodeDecision.episode_id)
            .where(EpisodeDecision.decision_id == decision_id)
            .limit(1)
        )
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

    # ------------------------------------------------------------------
    # get_calibration()
    # ------------------------------------------------------------------

    async def get_calibration(self, session: AsyncSession | None = None) -> CalibrationReport:
        """Compute full calibration report."""
        if session is None:
            async with self.db.session() as session:
                return await self.calibration.compute(session, self.agent_id)
        return await self.calibration.compute(session, self.agent_id)

    # ------------------------------------------------------------------
    # link()
    # ------------------------------------------------------------------

    async def link(
        self,
        source_id: UUID,
        target_id: UUID,
        relation: str,
        weight: float = 1.0,
        source_type: str = "decision",
        target_type: str = "decision",
        session: AsyncSession | None = None,
    ) -> GraphEdgeInfo:
        """Create a graph edge between two nodes."""
        if session is None:
            async with self.db.session() as session:
                result = await self._link(source_id, target_id, relation, weight, False, source_type, target_type, session)
                await session.commit()
                return result
        return await self._link(source_id, target_id, relation, weight, False, source_type, target_type, session)

    async def _link(
        self,
        source_id: UUID,
        target_id: UUID,
        relation: str,
        weight: float,
        auto_linked: bool,
        source_type: str = "decision",
        target_type: str = "decision",
        session: AsyncSession | None = None,
    ) -> GraphEdgeInfo:
        edge = GraphEdge(
            source_id=source_id,
            target_id=target_id,
            source_type=source_type,
            target_type=target_type,
            agent_id=self.agent_id,
            relation=relation,
            weight=weight,
            auto_linked=auto_linked,
        )
        session.add(edge)
        await session.flush()

        # Emit event (P2-9)
        await self._emit_event(
            session,
            "decisions_linked",
            {
                "source_id": str(source_id),
                "target_id": str(target_id),
                "relation": relation,
            },
        )

        return GraphEdgeInfo(
            source_id=source_id,
            target_id=target_id,
            source_type=source_type,
            target_type=target_type,
            relation=relation,
            weight=weight,
            auto_linked=auto_linked,
        )

    # ------------------------------------------------------------------
    # neighbors()
    # ------------------------------------------------------------------

    async def neighbors(
        self,
        node_id: UUID,
        node_type: str = "decision",
        relation: str | None = None,
        limit: int = 10,
        session: AsyncSession | None = None,
    ) -> list[NeighborResult]:
        """Get nodes connected to the given node via graph edges."""
        if session is None:
            async with self.db.session() as session:
                return await self._neighbors(node_id, node_type, relation, limit, session)
        return await self._neighbors(node_id, node_type, relation, limit, session)

    async def _neighbors(
        self,
        node_id: UUID,
        node_type: str,
        relation: str | None,
        limit: int,
        session: AsyncSession,
    ) -> list[NeighborResult]:
        # Find edges where this node is source or target, matching node type
        source_q = select(
            GraphEdge.target_id.label("neighbor_id"),
            GraphEdge.target_type.label("neighbor_type"),
            GraphEdge.relation.label("edge_relation"),
            GraphEdge.weight.label("edge_weight"),
        ).where(
            GraphEdge.source_id == node_id,
            GraphEdge.source_type == node_type,
            GraphEdge.agent_id == self.agent_id,
        )

        target_q = select(
            GraphEdge.source_id.label("neighbor_id"),
            GraphEdge.source_type.label("neighbor_type"),
            GraphEdge.relation.label("edge_relation"),
            GraphEdge.weight.label("edge_weight"),
        ).where(
            GraphEdge.target_id == node_id,
            GraphEdge.target_type == node_type,
            GraphEdge.agent_id == self.agent_id,
        )

        if relation:
            source_q = source_q.where(GraphEdge.relation == relation)
            target_q = target_q.where(GraphEdge.relation == relation)

        union_q = source_q.union_all(target_q).limit(limit)
        result = await session.execute(union_q)
        rows = result.all()

        if not rows:
            return []

        # Build edge metadata map
        edge_map: dict[UUID, tuple[str, str, float]] = {}
        for r in rows:
            edge_map[r.neighbor_id] = (r.neighbor_type, r.edge_relation, r.edge_weight or 1.0)

        # Resolve decision neighbors
        decision_ids = [r.neighbor_id for r in rows if r.neighbor_type == "decision"]
        descriptions: dict[UUID, tuple[str, datetime]] = {}
        if decision_ids:
            dec_result = await session.execute(
                select(Decision.id, Decision.description, Decision.created_at)
                .where(Decision.id.in_(decision_ids))
            )
            for d in dec_result.all():
                descriptions[d.id] = (d.description, d.created_at)

        # Build results
        results = []
        for r in rows:
            ntype, rel, weight = edge_map[r.neighbor_id]
            if ntype == "decision" and r.neighbor_id in descriptions:
                desc, created = descriptions[r.neighbor_id]
            else:
                desc = f"[{ntype}] {r.neighbor_id}"
                created = datetime.now(UTC)
            results.append(NeighborResult(
                id=r.neighbor_id,
                node_type=ntype,
                description=desc,
                edge_relation=rel,
                edge_weight=weight,
                created_at=created,
            ))

        return results

    # ------------------------------------------------------------------
    # auto_link()
    # ------------------------------------------------------------------

    async def auto_link(
        self,
        decision_id: UUID,
        threshold: float | None = None,
        max_links: int | None = None,
        session: AsyncSession | None = None,
    ) -> list[GraphEdgeInfo]:
        """Find and link similar decisions automatically."""
        if session is None:
            async with self.db.session() as session:
                result = await self._auto_link(decision_id, session, threshold, max_links)
                await session.commit()
                return result
        return await self._auto_link(decision_id, session, threshold, max_links)

    async def _auto_link(
        self,
        decision_id: UUID,
        session: AsyncSession,
        threshold: float | None = None,
        max_links: int | None = None,
    ) -> list[GraphEdgeInfo]:
        """Internal auto_link — finds similar decisions by cosine similarity.

        P2-19: Normalizes edge direction (lower UUID as source_id).
        P2-20: Uses ON CONFLICT DO NOTHING for concurrent inserts.
        """
        if threshold is None:
            threshold = self.settings.auto_link_threshold
        if max_links is None:
            max_links = self.settings.auto_link_max

        # Get the decision's embedding
        decision = await session.get(Decision, decision_id)
        if decision is None or decision.embedding is None:
            return []

        # Format embedding as pgvector string: [0.1,0.2,...] without spaces
        embedding_str = "[" + ",".join(str(float(v)) for v in decision.embedding) + "]"

        # Find similar decisions by cosine similarity
        sql = text("""
            SELECT id, 1 - (embedding <=> CAST(:embedding AS vector)) AS similarity
            FROM brain.decisions
            WHERE agent_id = :agent_id
              AND id != :decision_id
              AND embedding IS NOT NULL
              AND 1 - (embedding <=> CAST(:embedding AS vector)) >= :threshold
            ORDER BY embedding <=> CAST(:embedding AS vector)
            LIMIT :max_links
        """)
        result = await session.execute(
            sql,
            {
                "embedding": embedding_str,
                "agent_id": self.agent_id,
                "decision_id": decision_id,
                "threshold": threshold,
                "max_links": max_links,
            },
        )
        similar = result.all()

        edges = []
        for row in similar:
            # P2-19: Normalize direction — lower UUID as source
            src, tgt = decision_id, row.id
            if str(src) > str(tgt):
                src, tgt = tgt, src

            # P2-20: ON CONFLICT DO NOTHING for concurrent inserts
            stmt = (
                pg_insert(GraphEdge)
                .values(
                    source_id=src,
                    target_id=tgt,
                    source_type="decision",
                    target_type="decision",
                    agent_id=self.agent_id,
                    relation="related_to",
                    weight=float(row.similarity),
                    auto_linked=True,
                )
                .on_conflict_do_nothing(constraint="uq_edges_src_tgt_rel")
            )
            await session.execute(stmt)

            edges.append(
                GraphEdgeInfo(
                    source_id=src,
                    target_id=tgt,
                    source_type="decision",
                    target_type="decision",
                    relation="related_to",
                    weight=float(row.similarity),
                    auto_linked=True,
                )
            )

        return edges

    # ------------------------------------------------------------------
    # emit_event()
    # ------------------------------------------------------------------

    async def emit_event(
        self,
        event_type: str,
        data: dict,
        session: AsyncSession | None = None,
        session_id: str | None = None,
    ) -> None:
        """Log a cognitive event to nous_system.events."""
        if session is None:
            async with self.db.session() as session:
                await self._emit_event(session, event_type, data, session_id=session_id)
                await session.commit()
        else:
            await self._emit_event(session, event_type, data, session_id=session_id)

    async def _emit_event(
        self,
        session: AsyncSession,
        event_type: str,
        data: dict,
        session_id: str | None = None,
    ) -> None:
        """Internal emit_event — inserts in same session (P2-9, 007.4)."""
        event = Event(
            agent_id=self.agent_id,
            event_type=event_type,
            data=data,
            session_id=session_id,
        )
        session.add(event)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _get_decision_orm(self, decision_id: UUID, session: AsyncSession) -> Decision | None:
        """Fetch a Decision ORM object with all relationships eagerly loaded.

        Scoped by agent_id to enforce multi-agent data isolation.
        """
        result = await session.execute(
            select(Decision)
            .options(
                selectinload(Decision.tags),
                selectinload(Decision.reasons),
                selectinload(Decision.bridge),
                selectinload(Decision.thoughts),
            )
            .where(Decision.id == decision_id)
            .where(Decision.agent_id == self.agent_id)
        )
        return result.scalars().first()

    def _decision_to_detail(self, decision: Decision) -> DecisionDetail:
        """Convert an ORM Decision to a DecisionDetail Pydantic model."""
        bridge = None
        if decision.bridge:
            bridge = BridgeInfo(
                structure=decision.bridge.structure,
                function=decision.bridge.function,
            )

        return DecisionDetail(
            id=decision.id,
            agent_id=decision.agent_id,
            description=decision.description,
            context=decision.context,
            pattern=decision.pattern,
            confidence=decision.confidence,
            category=decision.category,
            stakes=decision.stakes,
            quality_score=decision.quality_score,
            outcome=decision.outcome or "pending",
            outcome_result=decision.outcome_result,
            reviewed_at=decision.reviewed_at,
            reviewer=decision.reviewer,
            created_at=decision.created_at,
            updated_at=decision.updated_at,
            tags=[t.tag for t in decision.tags],
            reasons=[ReasonInput(type=r.type, text=r.text) for r in decision.reasons],
            bridge=bridge,
            thoughts=[ThoughtInfo(id=t.id, text=t.text, created_at=t.created_at) for t in decision.thoughts],
        )

    def _decision_to_summary(self, decision: Decision) -> DecisionSummary:
        """Convert an ORM Decision to a DecisionSummary Pydantic model."""
        return DecisionSummary(
            id=decision.id,
            description=decision.description,
            confidence=decision.confidence,
            category=decision.category,
            stakes=decision.stakes,
            outcome=decision.outcome or "pending",
            pattern=decision.pattern,
            tags=[],
            reviewed_at=decision.reviewed_at,
            created_at=decision.created_at,
        )
