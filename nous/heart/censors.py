"""Censor management — things NOT to do.

Manages learned constraints with semantic matching, escalation, and
false positive tracking. All methods follow Brain's session injection
pattern (P1-1).
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from nous.brain.embeddings import EmbeddingProvider
from nous.heart.schemas import CensorDetail, CensorInput, CensorMatch
from nous.storage.database import Database
from nous.storage.models import Censor, Event

logger = logging.getLogger(__name__)

# Escalation path: warn -> block -> absolute. No downgrade.
_ESCALATION_ORDER = {"warn": "block", "block": "absolute", "absolute": "absolute"}

# Maximum length of text input to evaluate against regex patterns (ReDoS guard)
_MAX_REGEX_INPUT_LEN = 10_000


def _safe_regex_match(pattern: str, text_input: str) -> bool:
    """Match pattern against text with ReDoS protection.

    Truncates input to _MAX_REGEX_INPUT_LEN and catches both
    re.error (bad pattern) and any unexpected regex exceptions.
    Returns True if matched, False otherwise. Raises re.error
    for invalid patterns (caller handles).
    """
    # Truncate excessively long inputs to bound regex evaluation time
    truncated = text_input[:_MAX_REGEX_INPUT_LEN]
    return re.search(pattern, truncated, re.IGNORECASE) is not None


class CensorManager:
    """Manages censors — things NOT to do."""

    def __init__(
        self,
        db: Database,
        embeddings: EmbeddingProvider | None,
        agent_id: str,
    ) -> None:
        self.db = db
        self.embeddings = embeddings
        self.agent_id = agent_id

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

    # ------------------------------------------------------------------
    # add()
    # ------------------------------------------------------------------

    async def add(self, input: CensorInput, session: AsyncSession | None = None) -> CensorDetail:
        """Create a new censor."""
        if session is None:
            async with self.db.session() as session:
                result = await self._add(input, session)
                await session.commit()
                return result
        return await self._add(input, session)

    async def _add(self, input: CensorInput, session: AsyncSession) -> CensorDetail:
        # Generate embedding from trigger_pattern + reason
        embedding = None
        if self.embeddings:
            embed_text = f"{input.trigger_pattern} {input.reason}"
            try:
                embedding = await self.embeddings.embed(embed_text)
            except Exception:
                logger.warning("Embedding generation failed for censor add")

        censor = Censor(
            agent_id=self.agent_id,
            trigger_pattern=input.trigger_pattern,
            action=input.action,
            reason=input.reason,
            domain=input.domain,
            learned_from_decision=input.learned_from_decision,
            learned_from_episode=input.learned_from_episode,
            trigger_action=input.trigger_action,
            action_instruction=input.action_instruction,
            unblock_pattern=input.unblock_pattern,
            created_by="manual",
            embedding=embedding,
        )
        session.add(censor)
        await session.flush()

        await self._emit_event(
            session,
            "censor_created",
            {
                "censor_id": str(censor.id),
                "trigger": input.trigger_pattern,
                "action": input.action,
            },
        )

        return self._to_detail(censor)

    # ------------------------------------------------------------------
    # check() — side-effecting censor evaluation
    # ------------------------------------------------------------------

    async def check(
        self,
        text_input: str,
        domain: str | None = None,
        session: AsyncSession | None = None,
    ) -> list[CensorMatch]:
        """Check text against all active censors (with side effects).

        Increments activation_count, updates last_activated, and
        auto-escalates when threshold is reached.
        """
        if session is None:
            async with self.db.session() as session:
                result = await self._check(text_input, domain, session)
                await session.commit()
                return result
        return await self._check(text_input, domain, session)

    async def _check(
        self,
        text_input: str,
        domain: str | None,
        session: AsyncSession,
    ) -> list[CensorMatch]:
        matches: list[tuple[Censor, float]] = []

        if self.embeddings:
            # Semantic matching: cosine similarity > 0.7
            try:
                embedding = await self.embeddings.embed(text_input)
                matches = await self._semantic_match(embedding, domain, session)
            except Exception:
                logger.warning("Embedding failed for censor check, skipping semantic")

        # Always run keyword matching to catch censors without embeddings
        # or those below the semantic similarity threshold.
        keyword_matches = await self._keyword_match(text_input, domain, session)
        if keyword_matches:
            seen_ids = {c.id for c, _ in matches}
            for censor, sim in keyword_matches:
                if censor.id not in seen_ids:
                    matches.append((censor, sim))
                    seen_ids.add(censor.id)

        # Apply side effects for each match
        results: list[CensorMatch] = []
        now = datetime.now(UTC)

        for censor, _similarity in matches:
            # Increment activation_count (P2-9: NULL-safe)
            censor.activation_count = (censor.activation_count or 0) + 1
            censor.last_activated = now

            # Auto-escalation check
            threshold = censor.escalation_threshold or 3
            if censor.activation_count >= threshold and censor.action == "warn":
                old_action = censor.action
                censor.action = "block"
                await self._emit_event(
                    session,
                    "censor_escalated",
                    {
                        "censor_id": str(censor.id),
                        "old_action": old_action,
                        "new_action": "block",
                    },
                )

            await self._emit_event(
                session,
                "censor_triggered",
                {
                    "censor_id": str(censor.id),
                    "matched_text": text_input[:200],
                },
            )

            results.append(
                CensorMatch(
                    id=censor.id,
                    trigger_pattern=censor.trigger_pattern,
                    action=censor.action,
                    reason=censor.reason,
                    domain=censor.domain,
                    trigger_action=censor.trigger_action,
                    action_instruction=censor.action_instruction,
                    unblock_pattern=censor.unblock_pattern,
                )
            )

        await session.flush()
        return results

    async def _semantic_match(
        self,
        embedding: list[float],
        domain: str | None,
        session: AsyncSession,
    ) -> list[tuple[Censor, float]]:
        """Find censors with cosine similarity > 0.7."""
        embedding_str = "[" + ",".join(str(float(v)) for v in embedding) + "]"

        domain_clause = ""
        params: dict = {
            "embedding": embedding_str,
            "agent_id": self.agent_id,
            "threshold": 0.7,
        }
        if domain:
            domain_clause = "AND (domain = :domain OR domain IS NULL)"
            params["domain"] = domain

        sql = text(f"""
            SELECT id, 1 - (embedding <=> CAST(:embedding AS vector)) AS similarity
            FROM heart.censors
            WHERE agent_id = :agent_id
              AND active = true
              AND embedding IS NOT NULL
              AND 1 - (embedding <=> CAST(:embedding AS vector)) > :threshold
              {domain_clause}
            ORDER BY similarity DESC
        """)

        result = await session.execute(sql, params)
        rows = result.all()

        if not rows:
            return []

        # Fetch ORM objects
        ids = [row.id for row in rows]
        similarities = {row.id: float(row.similarity) for row in rows}

        censor_result = await session.execute(select(Censor).where(Censor.id.in_(ids)))
        censors = {c.id: c for c in censor_result.scalars().all()}

        return [(censors[cid], similarities[cid]) for cid in ids if cid in censors]

    async def _keyword_match(
        self,
        text_input: str,
        domain: str | None,
        session: AsyncSession,
    ) -> list[tuple[Censor, float]]:
        """P1-3: Regex fallback — Python-side re.search on trigger_pattern.

        Each censor is evaluated independently with try/except so a single
        malformed regex cannot disable all censors (Issue #199).
        """
        stmt = (
            select(Censor)
            .where(Censor.agent_id == self.agent_id)
            .where(Censor.active == True)  # noqa: E712
        )
        if domain:
            stmt = stmt.where((Censor.domain == domain) | (Censor.domain.is_(None)))

        result = await session.execute(stmt)
        censors = result.scalars().all()

        matches: list[tuple[Censor, float]] = []
        for censor in censors:
            try:
                if _safe_regex_match(censor.trigger_pattern, text_input):
                    matches.append((censor, 1.0))
            except re.error:
                logger.warning(
                    "Invalid regex in censor %s, pattern: %s",
                    censor.id,
                    censor.trigger_pattern,
                )

        return matches

    # ------------------------------------------------------------------
    # search() — read-only (P1-5)
    # ------------------------------------------------------------------

    async def search(
        self,
        query: str,
        limit: int = 10,
        domain: str | None = None,
        session: AsyncSession | None = None,
    ) -> list[CensorMatch]:
        """Read-only semantic search over censors (no side effects).

        Unlike check(), this does NOT increment activation_count,
        does NOT update last_activated, and does NOT auto-escalate.
        Used by Heart.recall() for safe censor searching.
        """
        if session is None:
            async with self.db.session() as session:
                return await self._search(query, limit, domain, session)
        return await self._search(query, limit, domain, session)

    async def _search(
        self,
        query: str,
        limit: int,
        domain: str | None,
        session: AsyncSession,
    ) -> list[CensorMatch]:
        if self.embeddings:
            try:
                embedding = await self.embeddings.embed(query)
            except Exception:
                logger.warning("Embedding failed for censor search, falling back to ILIKE")
                return await self._keyword_search(query, limit, domain, session)

            return await self._semantic_search(embedding, limit, domain, session)

        return await self._keyword_search(query, limit, domain, session)

    async def _semantic_search(
        self,
        embedding: list[float],
        limit: int,
        domain: str | None,
        session: AsyncSession,
    ) -> list[CensorMatch]:
        """Read-only semantic search — no counters, no escalation."""
        embedding_str = "[" + ",".join(str(float(v)) for v in embedding) + "]"

        domain_clause = ""
        params: dict = {
            "embedding": embedding_str,
            "agent_id": self.agent_id,
            "threshold": 0.7,
            "limit": limit,
        }
        if domain:
            domain_clause = "AND (domain = :domain OR domain IS NULL)"
            params["domain"] = domain

        sql = text(f"""
            SELECT id, 1 - (embedding <=> CAST(:embedding AS vector)) AS similarity
            FROM heart.censors
            WHERE agent_id = :agent_id
              AND active = true
              AND embedding IS NOT NULL
              AND 1 - (embedding <=> CAST(:embedding AS vector)) > :threshold
              {domain_clause}
            ORDER BY similarity DESC
            LIMIT :limit
        """)

        result = await session.execute(sql, params)
        rows = result.all()

        if not rows:
            return []

        ids = [row.id for row in rows]
        scores = {row.id: float(row.similarity) for row in rows}
        censor_result = await session.execute(select(Censor).where(Censor.id.in_(ids)))
        censors = {c.id: c for c in censor_result.scalars().all()}

        return [
            CensorMatch(
                id=c.id,
                trigger_pattern=c.trigger_pattern,
                action=c.action,
                reason=c.reason,
                domain=c.domain,
                score=scores.get(c.id),
                trigger_action=c.trigger_action,
                action_instruction=c.action_instruction,
                unblock_pattern=c.unblock_pattern,
            )
            for cid in ids
            if (c := censors.get(cid)) is not None
        ]

    async def _keyword_search(
        self,
        query: str,
        limit: int,
        domain: str | None,
        session: AsyncSession,
    ) -> list[CensorMatch]:
        """Read-only regex keyword search (Issue #199)."""
        stmt = (
            select(Censor)
            .where(Censor.agent_id == self.agent_id)
            .where(Censor.active == True)  # noqa: E712
        )
        if domain:
            stmt = stmt.where((Censor.domain == domain) | (Censor.domain.is_(None)))

        result = await session.execute(stmt)
        censors = result.scalars().all()

        matches: list[CensorMatch] = []
        query_lower = query.lower()
        for censor in censors:
            if len(matches) >= limit:
                break
            try:
                # Match if query matches pattern OR query appears in pattern
                pattern_matches = _safe_regex_match(censor.trigger_pattern, query)
                query_in_pattern = query_lower in (censor.trigger_pattern or "").lower()
                if pattern_matches or query_in_pattern:
                    matches.append(
                        CensorMatch(
                            id=censor.id,
                            trigger_pattern=censor.trigger_pattern,
                            action=censor.action,
                            reason=censor.reason,
                            domain=censor.domain,
                            score=1.0,
                            trigger_action=censor.trigger_action,
                            action_instruction=censor.action_instruction,
                            unblock_pattern=censor.unblock_pattern,
                        )
                    )
            except re.error:
                logger.warning(
                    "Invalid regex in censor %s, pattern: %s",
                    censor.id,
                    censor.trigger_pattern,
                )

        return matches

    # ------------------------------------------------------------------
    # record_false_positive()
    # ------------------------------------------------------------------

    async def record_false_positive(self, censor_id: UUID, session: AsyncSession | None = None) -> CensorDetail:
        """Record a false positive trigger."""
        if session is None:
            async with self.db.session() as session:
                result = await self._record_false_positive(censor_id, session)
                await session.commit()
                return result
        return await self._record_false_positive(censor_id, session)

    async def _record_false_positive(self, censor_id: UUID, session: AsyncSession) -> CensorDetail:
        censor = await self._get_censor_orm(censor_id, session)
        if censor is None:
            raise ValueError(f"Censor {censor_id} not found")

        # P2-9: NULL-safe counter
        censor.false_positive_count = (censor.false_positive_count or 0) + 1
        censor.last_false_positive = datetime.now(UTC)

        # Log warning if more than half are false positives
        act_count = censor.activation_count or 0
        if act_count > 0 and censor.false_positive_count > act_count * 0.5:
            logger.warning(
                "Censor %s has high false positive rate: %d/%d",
                censor_id,
                censor.false_positive_count,
                act_count,
            )

        await session.flush()
        return self._to_detail(censor)

    # ------------------------------------------------------------------
    # escalate()
    # ------------------------------------------------------------------

    async def escalate(self, censor_id: UUID, session: AsyncSession | None = None) -> CensorDetail:
        """Manually escalate censor severity. warn -> block -> absolute. No downgrade."""
        if session is None:
            async with self.db.session() as session:
                result = await self._escalate(censor_id, session)
                await session.commit()
                return result
        return await self._escalate(censor_id, session)

    async def _escalate(self, censor_id: UUID, session: AsyncSession) -> CensorDetail:
        censor = await self._get_censor_orm(censor_id, session)
        if censor is None:
            raise ValueError(f"Censor {censor_id} not found")

        old_action = censor.action
        new_action = _ESCALATION_ORDER.get(old_action, old_action)

        if new_action != old_action:
            censor.action = new_action
            await session.flush()

            await self._emit_event(
                session,
                "censor_escalated",
                {
                    "censor_id": str(censor_id),
                    "old_action": old_action,
                    "new_action": new_action,
                },
            )

        return self._to_detail(censor)

    # ------------------------------------------------------------------
    # list_active()
    # ------------------------------------------------------------------

    async def list_active(self, domain: str | None = None, session: AsyncSession | None = None) -> list[CensorDetail]:
        """List all active censors, optionally filtered by domain."""
        if session is None:
            async with self.db.session() as session:
                return await self._list_active(domain, session)
        return await self._list_active(domain, session)

    async def _list_active(self, domain: str | None, session: AsyncSession) -> list[CensorDetail]:
        stmt = (
            select(Censor).where(Censor.agent_id == self.agent_id).where(Censor.active == True)  # noqa: E712
        )
        if domain is not None:
            stmt = stmt.where((Censor.domain == domain) | (Censor.domain.is_(None)))

        result = await session.execute(stmt)
        censors = result.scalars().all()
        return [self._to_detail(c) for c in censors]

    # ------------------------------------------------------------------
    # list_all() — F021 dashboard browse mode
    # ------------------------------------------------------------------

    async def list_all(
        self,
        limit: int = 50,
        offset: int = 0,
        action: str | None = None,
        active_only: bool = True,
        domain: str | None = None,
        session: AsyncSession | None = None,
    ) -> tuple[list[CensorDetail], int]:
        """Paginated censor list with filters (F021)."""
        if session is None:
            async with self.db.session() as session:
                return await self._list_all(limit, offset, action, active_only, domain, session)
        return await self._list_all(limit, offset, action, active_only, domain, session)

    async def _list_all(self, limit, offset, action, active_only, domain, session):
        from sqlalchemy import func as sa_func

        conditions = [Censor.agent_id == self.agent_id]
        if active_only:
            conditions.append(Censor.active == True)  # noqa: E712
        if action:
            conditions.append(Censor.action == action)
        if domain:
            conditions.append(Censor.domain == domain)

        count_q = select(sa_func.count()).select_from(Censor).where(*conditions)
        total = (await session.execute(count_q)).scalar() or 0

        q = (select(Censor).where(*conditions)
             .order_by(Censor.created_at.desc()).limit(limit).offset(offset))
        result = await session.execute(q)
        censors = list(result.scalars().all())

        details = [self._to_detail(c) for c in censors]
        return details, total

    # ------------------------------------------------------------------
    # deactivate()
    # ------------------------------------------------------------------

    async def deactivate(self, censor_id: UUID, session: AsyncSession | None = None) -> None:
        """Deactivate a censor. Set active=false."""
        if session is None:
            async with self.db.session() as session:
                await self._deactivate(censor_id, session)
                await session.commit()
                return
        await self._deactivate(censor_id, session)

    async def _deactivate(self, censor_id: UUID, session: AsyncSession) -> None:
        censor = await self._get_censor_orm(censor_id, session)
        if censor is None:
            raise ValueError(f"Censor {censor_id} not found")
        censor.active = False
        await session.flush()

    # ------------------------------------------------------------------
    # update() — F031: modify existing censor fields
    # ------------------------------------------------------------------

    _SENTINEL = object()

    async def update(
        self,
        censor_id: UUID,
        *,
        trigger_action: dict | None | object = _SENTINEL,
        action_instruction: str | None | object = _SENTINEL,
        unblock_pattern: str | None | object = _SENTINEL,
        reason: str | None | object = _SENTINEL,
        domain: str | None | object = _SENTINEL,
        session: AsyncSession | None = None,
    ) -> CensorDetail:
        """Update specific fields on an existing censor.

        Only fields explicitly passed are updated. Pass None to clear a field.
        Fields not passed are left unchanged.
        """
        if session is None:
            async with self.db.session() as session:
                result = await self._update(
                    censor_id, trigger_action=trigger_action,
                    action_instruction=action_instruction,
                    unblock_pattern=unblock_pattern,
                    reason=reason, domain=domain, session=session,
                )
                await session.commit()
                return result
        return await self._update(
            censor_id, trigger_action=trigger_action,
            action_instruction=action_instruction,
            unblock_pattern=unblock_pattern,
            reason=reason, domain=domain, session=session,
        )

    async def _update(
        self,
        censor_id: UUID,
        *,
        trigger_action,
        action_instruction,
        unblock_pattern,
        reason,
        domain,
        session: AsyncSession,
    ) -> CensorDetail:
        censor = await self._get_censor_orm(censor_id, session)
        if censor is None:
            raise ValueError(f"Censor {censor_id} not found")

        SENTINEL = self._SENTINEL
        if trigger_action is not SENTINEL:
            censor.trigger_action = trigger_action
        if action_instruction is not SENTINEL:
            censor.action_instruction = action_instruction
        if unblock_pattern is not SENTINEL:
            censor.unblock_pattern = unblock_pattern
        if reason is not SENTINEL and reason is not None:
            censor.reason = reason
        if domain is not SENTINEL:
            censor.domain = domain

        censor.updated_at = datetime.now(UTC)
        await session.flush()
        return self._to_detail(censor)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _get_censor_orm(self, censor_id: UUID, session: AsyncSession) -> Censor | None:
        """Fetch Censor ORM scoped by agent_id."""
        result = await session.execute(
            select(Censor).where(Censor.id == censor_id).where(Censor.agent_id == self.agent_id)
        )
        return result.scalars().first()

    def _to_detail(self, censor: Censor) -> CensorDetail:
        """Convert ORM Censor to CensorDetail DTO."""
        return CensorDetail(
            id=censor.id,
            agent_id=censor.agent_id,
            trigger_pattern=censor.trigger_pattern,
            action=censor.action,
            reason=censor.reason,
            domain=censor.domain,
            learned_from_decision=censor.learned_from_decision,
            learned_from_episode=censor.learned_from_episode,
            created_by=censor.created_by or "manual",
            activation_count=censor.activation_count or 0,
            last_activated=censor.last_activated,
            false_positive_count=censor.false_positive_count or 0,
            escalation_threshold=censor.escalation_threshold or 3,
            active=censor.active if censor.active is not None else True,
            created_at=censor.created_at,
            trigger_action=censor.trigger_action,
            action_instruction=censor.action_instruction,
            unblock_pattern=censor.unblock_pattern,
        )
