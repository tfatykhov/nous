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

from sqlalchemy import case, delete, func, or_, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from nous.brain.bridge import BridgeExtractor
from nous.brain.calibration import CalibrationEngine
from nous.brain.graph_constants import RETRIEVAL_EXCLUDED_RELATIONS
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
    Episode,
    EpisodeChunk,
    Event,
    Fact,
    GraphEdge,
    Procedure,
    Thought,
)

logger = logging.getLogger(__name__)

# Noise indicators — short descriptions with no alternatives/reasoning signal
_NOISE_KEYWORDS = frozenset({
    "completed", "done", "finished", "success", "started",
    "status", "progress", "update", "checked", "confirmed",
})


def apply_outcome_demotion(
    scored: list[tuple[object, str | None, float | None]],
    factors: dict[str, float],
) -> list[tuple[object, float | None]]:
    """Multiply each score by its outcome's factor, then stable-sort desc.

    Returns (item, new_score) pairs. Empty ``factors`` is an exact no-op:
    no multiplication AND no re-sort, so merged order is preserved
    byte-identically (the kill switch).

    Multiplicative because _query returns TWO score spaces (normalized RRF,
    and raw ts_rank_cd on the keyword-only fallback) — a scale-free operator
    is correct in both. None scores pass through untouched and sort last.

    THE RE-SORT IS THE FEATURE: ``_query`` builds its summaries preserving
    search order and nothing downstream re-sorts (``_apply_staleness_penalty``
    returns input order; ``_enforce_diversity`` / ``_apply_relevance_filter`` /
    ``_format_decisions`` all iterate in order). A multiplier without the
    re-sort would be a no-op on the pre-turn path — and worse than nothing,
    because a low score injected mid-list desynchronizes
    ``_apply_relevance_filter``'s monotonic ``prev_score`` walk.

    Module level (not a method) so it is unit-testable without a database:
    ``Brain._query`` needs Postgres FTS and cannot run on the sqlite backend
    the test suite defaults to.
    """
    if not factors:
        return [(item, score) for item, _, score in scored]

    demoted: list[tuple[object, float | None]] = []
    for item, outcome, score in scored:
        # The column is nullable — a NULL outcome means "pending".
        factor = factors.get(outcome or "pending")
        if factor is not None and score is not None:
            score = score * factor
        demoted.append((item, score))

    # Stable sort, descending, None last. sorted() is stable, so equal scores
    # (including ties created by the multiplication) keep their merged order.
    return sorted(
        demoted,
        key=lambda pair: (pair[1] is None, -pair[1] if pair[1] is not None else 0.0),
    )


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

        # F040: Optional EventBus for decision_recorded emission.
        # Injected post-construction in main.py (same pattern as Heart._bus).
        self._bus = None

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
                reviewed_at=d.reviewed_at,
                superseded_by=d.superseded_by,
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
                superseded_by=d.superseded_by,
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
        rather than a real decision. This is a HARD filter — callers
        should raise ValueError when True.

        Checks:
        1. Very short description (<20 chars) with no reasons
        2. Description is mostly noise keywords with no reasoning
        3. Description contains error-processing phrases
        4. Description starts with a quote character
        5. Description is conversational filler (< 60 chars, starts with filler word)
        """
        desc_lower = description.lower().strip()
        desc_stripped = description.strip()

        # Very short with no reasons — almost certainly noise
        if len(desc_lower) < 20 and not reasons:
            return True

        # F038-1.1: Error processing patterns
        if "error processing your request" in desc_lower or "encountered an error" in desc_lower:
            return True

        # F038-1.1: Starts with quote character
        if desc_stripped and desc_stripped[0] in ('"', "'", '\u201c', '\u201d'):
            return True

        # F038-1.1: Conversational filler — short descriptions starting with filler words
        _FILLER_PREFIXES = ("excellent", "great", "perfect", "wonderful", "let me", "i'll", "i will")
        if len(desc_stripped) < 60 and any(desc_lower.startswith(p) for p in _FILLER_PREFIXES):
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
        # F038-1.1: Noise check — hard reject
        if self._is_noise_decision(input.description, input.reasons):
            raise ValueError(f"Noise decision rejected: {input.description[:80]}")

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

        # 4-7. Insert decision with cascade-populated relationships.
        # F058: apply temperature scaling to confidence so all downstream
        # gates (guardrails, supersession, action gating, deliberation) see
        # calibrated values. Raw agent claim is preserved in confidence_raw
        # for calibration eval.
        from nous.brain.calibration_scaling import calibrate_confidence
        calibrated = calibrate_confidence(
            input.confidence, self.settings.confidence_calibration_factor
        )
        decision = Decision(
            agent_id=self.agent_id,
            description=input.description,
            context=input.context,
            pattern=input.pattern,
            confidence=calibrated,
            confidence_raw=input.confidence,
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

        # F040: Emit on in-process EventBus for reverse graph linking.
        # The DB audit event (via _emit_event) does NOT reach the bus.
        if self._bus is not None:
            from nous.events import Event as BusEvent
            await self._bus.emit(BusEvent(
                type="decision_recorded",
                agent_id=self.agent_id,
                data={"decision_id": str(decision.id), "category": input.category},
            ))

        # 8. Auto-link (isolated in nested savepoint + try/except — P1-1)
        # Nested savepoint ensures SQL errors in auto_link don't abort the
        # parent transaction.
        try:
            async with session.begin_nested():
                await self._auto_link(decision.id, session)
        except Exception:
            # exc_info=True so the actual failure mode is visible in prod
            # logs. The prior log line gave no clue why auto-linking was
            # silently a no-op (it was the missing constraint name; see
            # _auto_link). Keep the WARN-level since auto-link is best-
            # effort and we don't want to fail the parent record() call.
            logger.warning(
                "auto_link failed for decision %s, continuing",
                decision.id, exc_info=True,
            )

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
            # F058: calibrate on update too — keeps the storage invariant
            # `confidence` = calibrated, `confidence_raw` = agent's claim.
            from nous.brain.calibration_scaling import calibrate_confidence
            decision.confidence = calibrate_confidence(
                confidence, self.settings.confidence_calibration_factor
            )
            decision.confidence_raw = confidence
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
        # Audit D1 (2026-06-09): brain.graph_edges lost its FK constraints in
        # migration 016 (polymorphic endpoints), so CASCADE no longer covers
        # it — edges must be deleted explicitly or they dangle forever,
        # feeding ghost ids to spreading activation / neighbors / adjacency
        # boost and inflating graph density. (Migration 060 cleans up edges
        # stranded by deletes that ran before this fix.)
        await session.execute(
            text(
                "DELETE FROM brain.graph_edges "
                "WHERE (source_id = :did AND source_type = 'decision') "
                "   OR (target_id = :did AND target_type = 'decision')"
            ),
            {"did": decision_id},
        )
        # Delete the decision — CASCADE handles brain.thoughts, decision_tags,
        # decision_reasons, decision_bridge (NOT graph_edges — see above)
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

        # Outcome demotion (2026-07-27) is active only when factors are
        # configured AND the caller did not ask for a specific outcome. Resolved
        # here because it widens the candidate fetch (codex #577 r1) — see the
        # _rrf_merge return_limit and the keyword-only LIMIT below.
        _factors = getattr(self.settings, "decision_outcome_score_factors", {}) or {}
        _demotion_active = bool(_factors) and not outcome

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
            # Full hybrid search using RRF (F025)
            from nous.heart.search import _resolve_vector_weight, _resolve_rrf_k, _rrf_merge

            vw = _resolve_vector_weight()
            rrf_k = _resolve_rrf_k()

            params["query_embedding"] = "[" + ",".join(str(float(v)) for v in query_embedding) + "]"

            # Vector search
            vector_sql = text(f"""
                SELECT d.id, 1 - (d.embedding <=> CAST(:query_embedding AS vector)) AS score
                FROM brain.decisions d
                {bridge_join}
                WHERE d.embedding IS NOT NULL {filter_clauses}
                ORDER BY d.embedding <=> CAST(:query_embedding AS vector)
                LIMIT :limit_expanded
            """)
            v_result = await session.execute(vector_sql, params)
            vector_results = [(row.id, float(row.score)) for row in v_result.all()]

            # Keyword search
            keyword_sql = text(f"""
                SELECT d.id,
                    ts_rank_cd(d.search_tsv, plainto_tsquery('english', :query_text))
                    / (1.0 + ts_rank_cd(d.search_tsv, plainto_tsquery('english', :query_text))) AS score
                FROM brain.decisions d
                {bridge_join}
                WHERE d.search_tsv @@ plainto_tsquery('english', :query_text)
                    {filter_clauses}
                ORDER BY score DESC
                LIMIT :limit_expanded
            """)
            k_result = await session.execute(keyword_sql, params)
            keyword_results = [(row.id, float(row.score)) for row in k_result.all()]

            # codex #577 r1/r3: when demotion is active we must re-rank the
            # COMPLETE fetched candidate set — otherwise a demoted row occupies
            # a top-`limit` slot and the better undemoted row it displaced was
            # never considered. A fixed 3x window still starves when more than
            # 3x demoted rows outrank the first undemoted one, so return
            # everything the SQL legs fetched (bounded by `limit_expanded`) and
            # truncate after the re-rank. `return_limit` widens the RETURN only:
            # `limit` still defines `penalty_rank = limit + 1`, and inflating
            # that would silently rescore every single-list doc (the #574 trap).
            merged = _rrf_merge(
                vector_results, keyword_results, rrf_k, vw, limit,
                return_limit=(
                    len(vector_results) + len(keyword_results)
                    if _demotion_active else None
                ),
            )
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
                LIMIT {':limit_expanded' if _demotion_active else ':limit'}
            """)
            result = await session.execute(sql, params)
            merged = [(row.id, float(row.score)) for row in result.all()]

        if not merged:
            return []

        decision_ids = [r[0] for r in merged]
        scores_by_id = {r[0]: r[1] for r in merged}

        # Fetch decision data
        decisions_result = await session.execute(select(Decision).where(Decision.id.in_(decision_ids)))
        decisions = {d.id: d for d in decisions_result.scalars().all()}

        # Separate tag query (P2-17)
        tag_result = await session.execute(select(DecisionTag).where(DecisionTag.decision_id.in_(decision_ids)))
        tags_by_id: dict[UUID, list[str]] = defaultdict(list)
        for tag_row in tag_result.scalars().all():
            tags_by_id[tag_row.decision_id].append(tag_row.tag)

        # Build results preserving search order
        ordered = [
            (d, d.outcome, scores_by_id.get(d.id))
            for d in (decisions.get(did) for did in decision_ids)
            if d is not None
        ]

        # Outcome demotion + re-sort (2026-07-27). Superseded/noise decisions
        # were outranking the current one in "## Related Decisions". Skipped
        # when the caller asked for a specific outcome — mirrors the abandoned
        # suppression `else` branch above: an explicit request wins.
        factors = _factors
        if _demotion_active:
            n_demoted = sum(
                1 for _, o, s in ordered if s is not None and (o or "pending") in factors
            )
            demoted = apply_outcome_demotion(ordered, factors)
            # codex #577 r1: the candidate set was widened to limit*3 above so
            # demotion can promote a row that would otherwise never have been
            # fetched — cut back to the caller's limit AFTER re-ranking.
            demoted = demoted[:limit]
            if n_demoted:
                logger.debug(
                    "brain._query: demoted %d/%d decisions by outcome (factors=%s)",
                    n_demoted, len(ordered), factors,
                )
        else:
            demoted = [(d, s) for d, _, s in ordered]

        summaries = []
        for d, score in demoted:
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
                    score=score,
                    superseded_by=d.superseded_by,
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
        superseded_by: UUID | None = None,
        session: AsyncSession | None = None,
    ) -> DecisionDetail:
        """Record outcome for a decision."""
        if session is None:
            async with self.db.session() as session:
                detail = await self._review(
                    decision_id, outcome, result, reviewer, superseded_by, session
                )
                await session.commit()
                return detail
        return await self._review(
            decision_id, outcome, result, reviewer, superseded_by, session
        )

    async def review_many(
        self,
        items: list[dict],
        reviewer: str | None = None,
        session: AsyncSession | None = None,
    ) -> list[dict]:
        """Resolve a batch of decisions in one transaction.

        Each item is a dict with keys ``decision_id`` (str|UUID, required),
        ``outcome`` (required), ``result`` (optional), ``superseded_by``
        (optional). A per-item failure (not-found / invalid outcome) is
        captured in the returned row and does not abort the batch — the
        failing item is simply not mutated. Returns one
        ``{decision_id, ok, error}`` dict per input item, in order.
        """
        if session is None:
            async with self.db.session() as session:
                results = await self._review_many(items, reviewer, session)
                await session.commit()
                return results
        return await self._review_many(items, reviewer, session)

    async def _review_many(
        self, items: list[dict], reviewer: str | None, session: AsyncSession,
    ) -> list[dict]:
        results: list[dict] = []
        for item in items:
            raw_id = item.get("decision_id")
            try:
                decision_id = raw_id if isinstance(raw_id, UUID) else UUID(str(raw_id))
                sup = item.get("superseded_by")
                sup_uuid = sup if (sup is None or isinstance(sup, UUID)) else UUID(str(sup))
                await self._review(
                    decision_id,
                    item["outcome"],
                    item.get("result"),
                    item.get("reviewer", reviewer),
                    sup_uuid,
                    session,
                )
                results.append({"decision_id": str(raw_id), "ok": True, "error": None})
            except Exception as e:  # noqa: BLE001 — surface per-item, keep batch alive
                results.append({"decision_id": str(raw_id), "ok": False, "error": str(e)})
        return results

    async def _review(
        self,
        decision_id: UUID,
        outcome: str,
        result_text: str | None,
        reviewer: str | None,
        superseded_by: UUID | None,
        session: AsyncSession,
    ) -> DecisionDetail:
        # Validate via Pydantic (P2-18)
        validated = ReviewInput(
            outcome=outcome,
            result=result_text,
            reviewer=reviewer,
            superseded_by=superseded_by,
        )

        decision = await self._get_decision_orm(decision_id, session)
        if decision is None:
            raise ValueError(f"Decision {decision_id} not found")

        decision.outcome = validated.outcome
        decision.outcome_result = validated.result
        decision.reviewed_at = datetime.now(UTC)
        decision.reviewer = validated.reviewer
        # Lineage marker only meaningful for a supersession.
        if validated.outcome == "superseded":
            decision.superseded_by = validated.superseded_by

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
        session: AsyncSession | None = None, limit: int | None = None,
    ) -> list[DecisionSummary]:
        """Fetch unreviewed decisions, optionally filtered by stakes.

        ``limit`` pushes the bound into SQL (F092.1 sources — a Python
        slice after ``.all()`` still materializes the whole window).
        """
        if session is None:
            async with self.db.session() as session:
                return await self._get_unreviewed(max_age_days, stakes, session, limit)
        return await self._get_unreviewed(max_age_days, stakes, session, limit)

    async def _get_unreviewed(
        self, max_age_days: int, stakes: str | None, session: AsyncSession,
        limit: int | None = None,
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
        if limit is not None:
            stmt = stmt.limit(limit)
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
    # get_episode_for_decision() — REMOVED
    # ------------------------------------------------------------------
    # Removed 2026-07-28 with EpisodeSignal, its only non-test caller. It
    # resolved decision -> *which* episode via heart.episode_decisions, a
    # direction nothing else consumes (and one that is genuinely ambiguous:
    # 15 prod decisions matched more than one episode).

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
        from nous.brain.edge_provenance import classify  # F065 (avoid circular)

        # F065 phase 4 follow-up (2026-05-23): auto-linked writes are
        # cosine-derived "inferred" provenance even when the relation is
        # neither contradicts nor supersedes. Without this tagging, the
        # F065 penalty multiplier had nothing to apply to in prod (0
        # contradicts rows — see nous/heart/facts.py:35 for the F027
        # classifier's CONTRADICTION bias).
        writer = "auto_linker" if auto_linked else None
        edge = GraphEdge(
            source_id=source_id,
            target_id=target_id,
            source_type=source_type,
            target_type=target_type,
            agent_id=self.agent_id,
            relation=relation,
            weight=weight,
            auto_linked=auto_linked,
            extraction_method=classify(relation, source=writer),
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
        *,
        neighbor_type: str | None = None,
    ) -> list[NeighborResult]:
        """Get nodes connected to the given node via graph edges.

        ``neighbor_type`` filters the SQL union to a single target node
        type (e.g. ``"decision"``). Required to prevent F070's chunk
        edges from crowding decisions out of small ``LIMIT`` windows in
        ``heart_graph_neighbors`` — without it, ``LIMIT 2`` over the
        full union can return zero decisions when a fact also has
        ``summarized_by`` chunk neighbors.
        """
        if session is None:
            async with self.db.session() as session:
                return await self._neighbors(
                    node_id, node_type, relation, limit, session,
                    neighbor_type=neighbor_type,
                )
        return await self._neighbors(
            node_id, node_type, relation, limit, session,
            neighbor_type=neighbor_type,
        )

    async def _neighbors(
        self,
        node_id: UUID,
        node_type: str,
        relation: str | None,
        limit: int,
        session: AsyncSession,
        *,
        neighbor_type: str | None = None,
    ) -> list[NeighborResult]:
        # Find edges where this node is source or target, matching node type.
        # F065: SELECT extraction_method so neighbors carry provenance tier
        # for downstream penalty multiplier wiring in retrieval_pipeline.
        source_q = select(
            GraphEdge.target_id.label("neighbor_id"),
            GraphEdge.target_type.label("neighbor_type"),
            GraphEdge.relation.label("edge_relation"),
            GraphEdge.weight.label("edge_weight"),
            GraphEdge.extraction_method.label("extraction_method"),
            GraphEdge.id.label("edge_id"),
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
            GraphEdge.extraction_method.label("extraction_method"),
            GraphEdge.id.label("edge_id"),
        ).where(
            GraphEdge.target_id == node_id,
            GraphEdge.target_type == node_type,
            GraphEdge.agent_id == self.agent_id,
        )

        if relation:
            source_q = source_q.where(GraphEdge.relation == relation)
            target_q = target_q.where(GraphEdge.relation == relation)
        else:
            # 2b: never surface lineage (supersedes) or negative (contradicts)
            # edges as retrieval connectivity. (co_occurred / co_mention ARE
            # legitimate associative connectivity for retrieval, so they are NOT
            # excluded here — graph_constants.RETRIEVAL_EXCLUDED_RELATIONS.) Only
            # applied to the unfiltered fan-out; an explicit `relation=` request
            # is honoured verbatim.
            source_q = source_q.where(GraphEdge.relation.notin_(RETRIEVAL_EXCLUDED_RELATIONS))
            target_q = target_q.where(GraphEdge.relation.notin_(RETRIEVAL_EXCLUDED_RELATIONS))

        # F070 fix: push the neighbor-type filter into SQL so LIMIT N
        # returns N rows of the requested type, not N rows that may all
        # be filtered out by a downstream Python check.
        if neighbor_type:
            source_q = source_q.where(GraphEdge.target_type == neighbor_type)
            target_q = target_q.where(GraphEdge.source_type == neighbor_type)
            # F080: for procedure neighbors, exclude edges pointing at inactive /
            # superseded skills BEFORE the LIMIT, so a capped fetch returns N
            # *active* procedures rather than N edges that may be filtered to
            # fewer (mirrors the F070 neighbor_type pushdown rationale above).
            if neighbor_type == "procedure":
                _active_proc = select(Procedure.id).where(Procedure.active == True)  # noqa: E712
                source_q = source_q.where(GraphEdge.target_id.in_(_active_proc))
                target_q = target_q.where(GraphEdge.source_id.in_(_active_proc))
            # Same guard for facts: a `supersedes` (or any) edge to a
            # superseded/inactive fact must not surface it as a graph neighbor
            # (Path A fact expansion). Without this, the 2026-06-13 supersedes-
            # edge backfill would make obsolete facts traversable and let them
            # displace active memories. Mirrors the F080 procedure filter.
            if neighbor_type == "fact":
                _active_fact = select(Fact.id).where(Fact.active == True)  # noqa: E712
                source_q = source_q.where(GraphEdge.target_id.in_(_active_fact))
                target_q = target_q.where(GraphEdge.source_id.in_(_active_fact))

        # codex #577 r3/r5: pushdown for demoted decision outcomes, applied
        # OUTSIDE the neighbor_type block — Stage 3's one-hop call passes no
        # neighbor_type, and the resolver's filter runs AFTER this LIMIT, so a
        # node with more higher-weight superseded/noise decision neighbors than
        # the cap returned an empty graph leg while valid lower-weight neighbors
        # sat just outside the window. The predicate is type-aware: only edges
        # POINTING AT a demoted decision are excluded, so non-decision neighbors
        # are unaffected in the untyped fan-out. Only outcomes actually demoted
        # (factor < 1.0) filter — 1.0 is a legal identity value — and an explicit
        # `relation=` overrides (documented contract, see above).
        if neighbor_type in (None, "decision") and not relation:
            _demoted_outcomes = [
                o for o, f in (
                    getattr(self.settings, "decision_outcome_score_factors", {}) or {}
                ).items() if f < 1.0
            ]
            if _demoted_outcomes:
                _ok_dec = select(Decision.id).where(
                    func.coalesce(Decision.outcome, "pending").notin_(_demoted_outcomes)
                )
                source_q = source_q.where(
                    or_(GraphEdge.target_type != "decision",
                        GraphEdge.target_id.in_(_ok_dec))
                )
                target_q = target_q.where(
                    or_(GraphEdge.source_type != "decision",
                        GraphEdge.source_id.in_(_ok_dec))
                )

        # F080: deduplicate to ONE row per neighbor (the max-weight edge) and cap,
        # all in SQL via a window function, so the dedup happens BEFORE the cap
        # (codex P1) — a node linked via multiple edges (informed_by from the graph
        # linker + related_to from the densifier) can't consume the fan-out cap with
        # duplicates. COALESCE sorts NULL weights LAST (Postgres would otherwise put
        # NULL first under DESC). ROW_NUMBER works on both Postgres and the SQLite
        # used in tests (avoids Postgres-only DISTINCT ON).
        combined = source_q.union_all(target_q).subquery()
        _w = func.coalesce(combined.c.edge_weight, -1.0)
        ranked = (
            select(
                combined.c.neighbor_id,
                combined.c.neighbor_type,
                combined.c.edge_relation,
                combined.c.edge_weight,
                combined.c.extraction_method,
                func.row_number()
                .over(
                    partition_by=combined.c.neighbor_id,
                    # Deterministic tie-break for equal-weight duplicate edges
                    # (common with the default weight 1.0): prefer NON-inferred
                    # provenance (so F065 penalties are stable), then the unique
                    # edge id. partition_by neighbor_id can't tie-break itself.
                    order_by=[
                        _w.desc(),
                        case((combined.c.extraction_method == "inferred", 1), else_=0).asc(),
                        combined.c.edge_id,
                    ],
                )
                .label("rn"),
            )
            .subquery()
        )
        # Over-fetch 3x (mirrors hybrid_search's limit_expanded and the
        # spreading-branch over-fetch): the resolver below drops rows whose
        # endpoints are inactive/foreign/dangling (codex P2 round 7,
        # PR #555), and Path A windows are small (limit=3) — without the
        # buffer a few bad high-weight edges starve the window. Results are
        # capped back to ``limit`` after resolution.
        union_q = (
            select(
                ranked.c.neighbor_id,
                ranked.c.neighbor_type,
                ranked.c.edge_relation,
                ranked.c.edge_weight,
                ranked.c.extraction_method,
            )
            .where(ranked.c.rn == 1)
            .order_by(func.coalesce(ranked.c.edge_weight, -1.0).desc(), ranked.c.neighbor_id)
            .limit(limit * 3)
        )
        result = await session.execute(union_q)
        rows = result.all()

        if not rows:
            return []

        # Build edge metadata map. F065: now carries extraction_method.
        edge_map: dict[UUID, tuple[str, str, float, str]] = {}
        for r in rows:
            method = r.extraction_method or "heuristic"  # F065: NULL → fail-open heuristic
            edge_map[r.neighbor_id] = (r.neighbor_type, r.edge_relation, r.edge_weight or 1.0, method)

        # Resolve content per node type via one batched SELECT per type.
        # Pre-Path-A, only ``decision`` was resolved and other types were
        # rendered as ``"[<ntype>] <uuid>"`` placeholders — useful for
        # decision-only consumers but useless once we let fact/episode/chunk/
        # procedure neighbors reach retrieval (heart_graph_all_types_enabled).
        ids_by_type: dict[str, list[UUID]] = defaultdict(list)
        for r in rows:
            ids_by_type[r.neighbor_type].append(r.neighbor_id)

        # codex #577 r4: an explicit `relation=` is documented as an override
        # of retrieval exclusions (see the RETRIEVAL_EXCLUDED_RELATIONS branch
        # above). neighbors(relation="supersedes") is literally asking for the
        # superseded endpoint — filtering it would make that query return
        # nothing. Mirrors _query's explicit-`outcome=`-wins rule.
        descriptions = await self._resolve_node_descriptions(
            session, ids_by_type, apply_outcome_filter=not relation,
        )

        # Build results
        # Node types where the content column is declared NOT NULL in models.py
        # (Fact.content, Episode.summary, EpisodeChunk.content). An empty
        # resolved description for these types means a data integrity issue
        # (NOT NULL bypass, or a manual insert with empty string) — log it
        # rather than silently shipping a placeholder candidate to rerank.
        _NOT_NULL_CONTENT_TYPES = {"fact", "episode", "chunk"}
        results = []
        for r in rows:
            if len(results) >= limit:
                break
            ntype, rel, weight, method = edge_map[r.neighbor_id]
            # F080 / Audit BR-1 (codex P1) + codex P2 round 3 (PR #555): an
            # id absent from the resolver map is an inactive/superseded
            # fact or procedure (active=true filters), a foreign-agent node
            # (agent_id filters — graph_edges endpoints are polymorphic and
            # not FK-protected), or a dangling edge. Drop ALL of them rather
            # than surfacing a "[type] <uuid>" placeholder that ships no
            # information yet consumes a post-LIMIT ranking slot.
            if r.neighbor_id not in descriptions:
                continue
            desc, created = descriptions[r.neighbor_id]
            # Defensive: keep placeholder if resolved description is empty
            # (DB rows with NULL/empty content shouldn't crash retrieval).
            if not desc:
                if ntype in _NOT_NULL_CONTENT_TYPES:
                    logger.warning(
                        "brain._neighbors: empty/NULL content on "
                        "%s id=%s (column is declared NOT NULL — "
                        "data integrity issue)",
                        ntype, r.neighbor_id,
                    )
                desc = f"[{ntype}] {r.neighbor_id}"
            if created is None:
                logger.warning(
                    "brain._neighbors: NULL created_at on %s id=%s "
                    "(column has server_default — possible migration bug)",
                    ntype, r.neighbor_id,
                )
                created = datetime.now(UTC)
            results.append(NeighborResult(
                id=r.neighbor_id,
                node_type=ntype,
                description=desc,
                edge_relation=rel,
                edge_weight=weight,
                created_at=created,
                extraction_method=method,  # F065
            ))

        return results

    async def _resolve_node_descriptions(
        self,
        session: AsyncSession,
        ids_by_type: dict[str, list[UUID]],
        apply_outcome_filter: bool = True,
    ) -> dict[UUID, tuple[str, datetime | None]]:
        """Resolve real content + created_at for graph node ids, batched per type.

        Shared by ``_neighbors`` and the spreading-activation branch in
        ``run_recall_pipeline`` so every graph consumer surfaces real memory
        content instead of ``"[<ntype>] <uuid>"`` placeholders.

        Inactive facts and procedures are filtered out (for those types,
        ``active=false`` is a soft-delete / supersession marker) and are simply
        ABSENT from the returned map — callers must treat a missing id as
        "drop this node".

        Every lookup is agent-scoped (codex P2 round 2, PR #555):
        ``graph_edges`` endpoints are polymorphic and not FK-protected, so a
        miswritten edge can point at another agent's node — foreign content
        must never resolve.
        """
        descriptions: dict[UUID, tuple[str, datetime | None]] = {}

        # Decision: mirrors Brain._query's default suppression of abandoned
        # decisions (outcome='failure' AND confidence=0.0 — codex P2 round 8,
        # PR #555) so graph traversal cannot reintroduce noise decisions
        # that normal brain search hides.
        #
        # Demoted outcomes (2026-07-27) are FILTERED here rather than demoted.
        # The asymmetry with _query is deliberate: this resolver returns
        # (description, created_at) tuples — there is no score to multiply, and
        # plumbing `outcome` through NeighborResult to every graph consumer is
        # disproportionate. Gated on the same setting, so `{}` restores today's
        # behavior on BOTH paths at once. NULL outcome normalizes to 'pending'
        # exactly as _query does (COALESCE keeps the predicate NULL-safe —
        # a bare NOT IN would silently drop every unreviewed decision).
        if ids_by_type.get("decision"):
            dec_stmt = (
                select(Decision.id, Decision.description, Decision.created_at)
                .where(Decision.id.in_(ids_by_type["decision"]))
                .where(Decision.agent_id == self.agent_id)
                .where(
                    ~((Decision.outcome == "failure") & (Decision.confidence == 0.0))
                )
            )
            # codex #577 r2: only outcomes actually DEMOTED (factor < 1.0)
            # belong in the exclusion set. 1.0 is a legal identity value an
            # operator uses to disable one outcome while keeping others — key
            # presence alone would exclude it here while the query path left
            # its score untouched, i.e. contradictory behavior across paths.
            demoted_outcomes = [
                o for o, f in (
                    getattr(self.settings, "decision_outcome_score_factors", {}) or {}
                ).items() if f < 1.0
            ] if apply_outcome_filter else []
            if demoted_outcomes:
                dec_stmt = dec_stmt.where(
                    func.coalesce(Decision.outcome, "pending").notin_(demoted_outcomes)
                )
            dec_result = await session.execute(dec_stmt)
            for d in dec_result.all():
                descriptions[d.id] = (d.description, d.created_at)

        # Fact: heart.facts.content
        # Audit BR-1 (2026-06-09): only ACTIVE facts. For facts, active=false
        # is a soft-delete / supersession marker (F027), so superseded and
        # contradiction-resolved facts must never resurface as graph neighbors
        # via Path A or decision 1-hop expansion — mirrors the F080 procedure
        # fix below. (Episodes/chunks are intentionally NOT filtered here:
        # episode active=false is the normal *closed* lifecycle state, and
        # episode_chunks has no soft-delete column.)
        if ids_by_type.get("fact"):
            f_result = await session.execute(
                select(Fact.id, Fact.content, Fact.created_at)
                .where(Fact.id.in_(ids_by_type["fact"]))
                .where(Fact.active == True)  # noqa: E712
                .where(Fact.agent_id == self.agent_id)
            )
            for f in f_result.all():
                descriptions[f.id] = (f.content, f.created_at)

        # Episode: heart.episodes.summary (matches _ENTITY_CONFIG fallback;
        # structured_summary preferred at densifier time but plain summary
        # is always populated). Mirrors the episode recall contract
        # (episodes.py HT-1 filter; codex P2 round 4, PR #555): ongoing
        # (active=true) OR genuinely-closed (ended_at IS NOT NULL), never
        # deactivated-noise or abandoned rows — graph edges to suppressed
        # episodes must not resurface them past normal recall's filters.
        if ids_by_type.get("episode"):
            e_result = await session.execute(
                select(Episode.id, Episode.summary, Episode.created_at)
                .where(Episode.id.in_(ids_by_type["episode"]))
                .where(Episode.agent_id == self.agent_id)
                .where(or_(
                    Episode.active == True,  # noqa: E712
                    Episode.ended_at.is_not(None),
                ))
                .where(Episode.outcome.is_distinct_from("abandoned"))
            )
            for e in e_result.all():
                descriptions[e.id] = (e.summary, e.created_at)

        # Chunk: heart.episode_chunks.content (F067 raw transcript fragment).
        if ids_by_type.get("chunk"):
            c_result = await session.execute(
                select(EpisodeChunk.id, EpisodeChunk.content, EpisodeChunk.created_at)
                .where(EpisodeChunk.id.in_(ids_by_type["chunk"]))
                .where(EpisodeChunk.agent_id == self.agent_id)
            )
            for c in c_result.all():
                descriptions[c.id] = (c.content, c.created_at)

        # Procedure: heart.procedures.description. F080: only ACTIVE procedures —
        # archived/superseded skills (active=false, set by name-dedup) must never
        # be surfaced as graph neighbors. Inactive ids are simply absent from
        # ``descriptions`` and dropped by the caller (this also fixes
        # a live Path-A resurrection of dead skills via auto_linked edges).
        if ids_by_type.get("procedure"):
            p_result = await session.execute(
                select(
                    Procedure.id,
                    Procedure.name,
                    Procedure.description,
                    Procedure.created_at,
                )
                .where(Procedure.id.in_(ids_by_type["procedure"]))
                .where(Procedure.active == True)  # noqa: E712
                .where(Procedure.agent_id == self.agent_id)
            )
            for p in p_result.all():
                # Procedure.description is nullable — fall back to the NAME
                # (NOT NULL; matches how recall formats descriptionless
                # procedures), then to a placeholder so consumers never get
                # None (codex P2, PR #555).
                desc_text = p.description or p.name or f"[procedure] {p.id}"
                descriptions[p.id] = (desc_text, p.created_at)

        return descriptions

    # ------------------------------------------------------------------
    # top_hubs() — F065 god-node surfacing
    # ------------------------------------------------------------------

    async def top_hubs(
        self,
        limit: int = 10,
        node_type: str | None = None,
        session: AsyncSession | None = None,
    ) -> list[dict]:
        """Return the highest-degree nodes in brain.graph_edges for this agent.

        Uses undirected degree (source + target appearances combined).
        Inspired by Graphify's god_nodes() — Postgres aggregation instead
        of NetworkX, keeping the zero-dependency posture from F022.

        Each row resolves to:
          {
              "node_id": str(UUID),
              "node_type": "decision" | "fact" | "episode" | "procedure",
              "label": str,
              "degree": int,
              "extraction_method_breakdown": {"deterministic": N, "heuristic": N, "inferred": N},
          }
        """
        if session is None:
            async with self.db.session() as s:
                return await self._top_hubs(limit, node_type, s)
        return await self._top_hubs(limit, node_type, session)

    async def _top_hubs(
        self,
        limit: int,
        node_type: str | None,
        session: AsyncSession,
    ) -> list[dict]:
        from sqlalchemy import bindparam

        # Step 1: aggregate degree across both edge directions.
        # CAST :node_type to TEXT so asyncpg can determine the parameter type
        # when the value is None — without the cast, asyncpg raises
        # AmbiguousParameterError.
        sql = text("""
            SELECT node_id, node_type, COUNT(*) AS degree
            FROM (
                SELECT source_id AS node_id, source_type AS node_type
                FROM brain.graph_edges WHERE agent_id = :agent_id
                UNION ALL
                SELECT target_id AS node_id, target_type AS node_type
                FROM brain.graph_edges WHERE agent_id = :agent_id
            ) combined
            WHERE (CAST(:node_type AS TEXT) IS NULL OR node_type = CAST(:node_type AS TEXT))
            GROUP BY node_id, node_type
            ORDER BY degree DESC
            LIMIT :limit
        """)
        result = await session.execute(
            sql,
            {"agent_id": self.agent_id, "node_type": node_type, "limit": limit},
        )
        hubs = result.all()
        if not hubs:
            return []

        hub_ids = [h.node_id for h in hubs]

        # Step 2: single-pass extraction_method_breakdown across both directions.
        # Uses expanding bindparam (renders as IN (?,?,...)) so the query is
        # portable across Postgres and the SQLite test backend.
        breakdown_sql = text("""
            SELECT node_id, extraction_method, COUNT(*) AS cnt
            FROM (
                SELECT source_id AS node_id, extraction_method
                FROM brain.graph_edges WHERE agent_id = :agent_id
                UNION ALL
                SELECT target_id AS node_id, extraction_method
                FROM brain.graph_edges WHERE agent_id = :agent_id
            ) combined
            WHERE node_id IN :hub_ids
            GROUP BY node_id, extraction_method
        """).bindparams(bindparam("hub_ids", expanding=True))
        breakdown_result = await session.execute(
            breakdown_sql,
            {"agent_id": self.agent_id, "hub_ids": hub_ids},
        )
        # Pivot into a dict[UUID -> dict[method -> count]].
        breakdown: dict[UUID, dict[str, int]] = {}
        for row in breakdown_result.all():
            breakdown.setdefault(row.node_id, {})[row.extraction_method] = int(row.cnt)

        # Step 3: resolve labels per node_type. Mirrors _neighbors's pattern.
        # Raw SQL returns node_ids as strings on SQLite / as UUID on asyncpg —
        # normalize to UUID objects for the ORM .in_() filters.
        def _as_uuid(v) -> UUID:
            return v if isinstance(v, UUID) else UUID(str(v))

        labels: dict[UUID, str] = {}
        decision_ids = [_as_uuid(h.node_id) for h in hubs if h.node_type == "decision"]
        if decision_ids:
            dec_result = await session.execute(
                select(Decision.id, Decision.description)
                .where(Decision.id.in_(decision_ids))
            )
            for d in dec_result.all():
                labels[d.id] = d.description

        fact_ids = [_as_uuid(h.node_id) for h in hubs if h.node_type == "fact"]
        if fact_ids:
            from nous.storage.models import Fact
            fact_result = await session.execute(
                select(Fact.id, Fact.subject).where(Fact.id.in_(fact_ids))
            )
            for f in fact_result.all():
                labels[f.id] = f.subject or f"[fact] {f.id}"

        episode_ids = [_as_uuid(h.node_id) for h in hubs if h.node_type == "episode"]
        if episode_ids:
            from nous.storage.models import Episode
            ep_result = await session.execute(
                select(Episode.id, Episode.summary).where(Episode.id.in_(episode_ids))
            )
            for e in ep_result.all():
                labels[e.id] = e.summary or f"[episode] {e.id}"

        proc_ids = [_as_uuid(h.node_id) for h in hubs if h.node_type == "procedure"]
        if proc_ids:
            from nous.storage.models import Procedure
            proc_result = await session.execute(
                select(Procedure.id, Procedure.name).where(Procedure.id.in_(proc_ids))
            )
            for p in proc_result.all():
                labels[p.id] = p.name or f"[procedure] {p.id}"

        # Step 4: assemble. Fall back to [<type>] <uuid> for orphan / soft-deleted nodes.
        results = []
        for h in hubs:
            hub_id = _as_uuid(h.node_id)
            label = labels.get(hub_id) or f"[{h.node_type}] {hub_id}"
            row_breakdown = breakdown.get(h.node_id, {})
            # Always present all three tier keys (zero-fill missing).
            full_breakdown = {
                "deterministic": int(row_breakdown.get("deterministic", 0)),
                "heuristic": int(row_breakdown.get("heuristic", 0)),
                "inferred": int(row_breakdown.get("inferred", 0)),
            }
            results.append({
                "node_id": str(hub_id),
                "node_type": h.node_type,
                "label": label,
                "degree": int(h.degree),
                "extraction_method_breakdown": full_breakdown,
            })
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
            from nous.brain.edge_provenance import classify  # F065
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
                    extraction_method=classify("related_to", source="auto_linker"),
                )
                .on_conflict_do_nothing(
                    # Match the unique constraint by columns, not by name.
                    # The actual constraint is auto-named
                    # ``graph_edges_source_id_target_id_relation_key`` (init.sql:205,
                    # ``UNIQUE(source_id, target_id, relation)``). The previous
                    # ``constraint="uq_edges_src_tgt_rel"`` referenced a name that
                    # never existed, so every ``_auto_link`` call raised
                    # ``UndefinedObject`` and was silently swallowed by the
                    # ``except Exception`` at line 420 — meaning auto-linking
                    # has been a no-op since this code was written. Mirrors the
                    # pattern used by ``GraphLinker.create_edge``.
                    index_elements=["source_id", "target_id", "relation"],
                )
            )
            result = await session.execute(stmt)

            # F044-STC-HOOK: a conflict (rowcount 0) means this decision↔decision
            # edge already existed and was re-derived → reinforce its LTP counter.
            if (
                getattr(self.settings, "tinyhippo_lite_enabled", False)
                and result.rowcount == 0
            ):
                from nous.brain.tinyhippo_lite import increment_ltp_on_rederivation
                await increment_ltp_on_rederivation(session, src, tgt, "related_to")

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
        event_id: str | None = None,
        trace_id: str | None = None,
        caused_by: str | None = None,
    ) -> None:
        """Log a cognitive event to nous_system.events."""
        if session is None:
            async with self.db.session() as session:
                await self._emit_event(
                    session, event_type, data, session_id=session_id,
                    event_id=event_id, trace_id=trace_id, caused_by=caused_by,
                )
                await session.commit()
        else:
            await self._emit_event(
                session, event_type, data, session_id=session_id,
                event_id=event_id, trace_id=trace_id, caused_by=caused_by,
            )

    async def _emit_event(
        self,
        session: AsyncSession,
        event_type: str,
        data: dict,
        session_id: str | None = None,
        event_id: str | None = None,
        trace_id: str | None = None,
        caused_by: str | None = None,
    ) -> None:
        """Internal emit_event — inserts in same session (P2-9, 007.4)."""
        event = Event(
            agent_id=self.agent_id,
            event_type=event_type,
            data=data,
            session_id=session_id,
            event_id=event_id,
            trace_id=trace_id,
            caused_by=caused_by,
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
            superseded_by=decision.superseded_by,
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
            # F058: the auto-reviewer thresholds on the agent's own claim, not
            # the calibrated value (see ErrorSignal).
            confidence_raw=decision.confidence_raw,
            category=decision.category,
            stakes=decision.stakes,
            outcome=decision.outcome or "pending",
            pattern=decision.pattern,
            tags=[],
            reviewed_at=decision.reviewed_at,
            superseded_by=decision.superseded_by,
            created_at=decision.created_at,
        )
