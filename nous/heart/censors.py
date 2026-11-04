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
from nous.heart.search import set_local_ef_search
from nous.storage.database import Database
from nous.storage.models import Censor, Event

logger = logging.getLogger(__name__)

# F078: manual escalation path: steer -> refuse -> abort. No downgrade.
# Used ONLY by escalate() (a deliberate operator action). Auto-escalation removed.
_ESCALATION_ORDER = {"steer": "refuse", "refuse": "abort", "abort": "abort"}

# F078: provenance -> max tier a censor may be created/updated at.
# auto (monitor/F039) can never reach a halting tier; agent (create_censor tool)
# caps at refuse; human (operator/migration) may reach abort.
_TIER_RANK = {"steer": 0, "refuse": 1, "abort": 2}
_PROVENANCE_MAX_TIER = {"auto": "steer", "agent": "refuse", "human": "abort"}
_VALID_ACTIONS = ("steer", "refuse", "abort")

# Maximum length of text input to evaluate against regex patterns (ReDoS guard)
_MAX_REGEX_INPUT_LEN = 10_000

# search(): the cosine a semantic match must exceed. A regex-only match scores
# exactly this, so it ranks with the weakest semantic match instead of above
# every scored row of a cross-type recall.
_SEARCH_SIMILARITY_FLOOR = 0.7


def _censor_embed_text(trigger_pattern: str, reason: str, action_instruction: str | None, mode: str) -> str:
    """The text a censor is embedded from (Settings.censor_embed_text_mode).

    "legacy": the pattern and the reason. "reason": the reason and the action instruction, without
    the pattern, which is a regex and matched as one by the keyword leg.
    """
    if mode == "reason":
        return f"{reason} {action_instruction or ''}".strip()
    if mode == "legacy":
        return f"{trigger_pattern} {reason}"
    raise ValueError(f"Unknown censor embed text mode: {mode!r}")


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
        *,
        embed_text_mode: str = "legacy",
    ) -> None:
        self.db = db
        self.embeddings = embeddings
        self.agent_id = agent_id
        self._embed_text_mode = embed_text_mode

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
        # F078: validate trigger_pattern compiles (reject at the boundary, not
        # silently dead at check time). Same for unblock_pattern if present.
        try:
            re.compile(input.trigger_pattern)
        except re.error as exc:
            raise ValueError(f"Invalid trigger_pattern regex: {exc}") from exc
        if input.unblock_pattern is not None:
            try:
                re.compile(input.unblock_pattern)
            except re.error as exc:
                raise ValueError(f"Invalid unblock_pattern regex: {exc}") from exc

        # F078: validate trigger_action.tool against the read-only allowlist.
        # Local import avoids a circular import (censor_actions imports Heart).
        if input.trigger_action is not None:
            from nous.heart.censor_actions import ALLOWED_TOOLS

            tool = input.trigger_action.get("tool") if isinstance(input.trigger_action, dict) else None
            if tool is not None and tool not in ALLOWED_TOOLS:
                raise ValueError(f"Censor trigger_action.tool not allowed: {tool}")

        # F078: provenance -> max-tier cap. Clamp DOWN to the cap (do not raise)
        # so an over-eager auto/agent caller can never create a turn-killer.
        action = input.action
        cap = _PROVENANCE_MAX_TIER.get(input.provenance, "steer")
        if _TIER_RANK.get(action, 0) > _TIER_RANK[cap]:
            logger.warning(
                "Censor provenance=%s caps tier at %s; clamping requested action %s down",
                input.provenance, cap, action,
            )
            action = cap

        embedding = None
        if self.embeddings:
            embed_text = _censor_embed_text(
                input.trigger_pattern, input.reason, input.action_instruction, self._embed_text_mode,
            )
            try:
                embedding = await self.embeddings.embed(embed_text)
            except Exception:
                logger.warning("Embedding generation failed for censor add")

        censor = Censor(
            agent_id=self.agent_id,
            trigger_pattern=input.trigger_pattern,
            action=action,
            reason=input.reason,
            domain=input.domain,
            learned_from_decision=input.learned_from_decision,
            learned_from_episode=input.learned_from_episode,
            trigger_action=input.trigger_action,
            action_instruction=input.action_instruction,
            unblock_pattern=input.unblock_pattern,
            provenance=input.provenance,
            refuse_keep_tools=input.refuse_keep_tools,
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
                "action": action,
                "provenance": input.provenance,
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

        Increments activation_count and updates last_activated. F078 removed
        the silent auto-escalation — promotion to a harder tier is now a
        deliberate operator action only (see escalate()).
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
        # False when the semantic leg did not run (no provider, embed or SQL failure): then a
        # "regex" match only says the regex matched, not that the cosine missed.
        semantic_checked = False

        if self.embeddings:
            # Semantic matching: cosine similarity > 0.7
            try:
                # fail_fast: this runs every turn; during an outage the keyword
                # match below still enforces, without waiting out the budget.
                embedding = await self.embeddings.embed(text_input, fail_fast=True)
                # A savepoint (I-5): a SQL error here rolls back only this read,
                # so the keyword match below still runs on a usable session.
                async with session.begin_nested():
                    matches = await self._semantic_match(embedding, domain, session)
                semantic_checked = True
            except Exception:
                logger.warning("Semantic censor check failed, skipping semantic", exc_info=True)

        # Always run keyword matching to catch censors without embeddings
        # or those below the semantic similarity threshold.
        keyword_matches = await self._keyword_match(text_input, domain, session)
        semantic_ids = {c.id for c, _ in matches}
        keyword_ids = {c.id for c, _ in keyword_matches}
        if keyword_matches:
            seen_ids = set(semantic_ids)
            for censor, sim in keyword_matches:
                if censor.id not in seen_ids:
                    matches.append((censor, sim))
                    seen_ids.add(censor.id)

        # Apply side effects for each match
        results: list[CensorMatch] = []
        now = datetime.now(UTC)

        for censor, similarity in matches:
            # Which leg found it: a semantic hit keeps its cosine, a regex-only hit scores 1.0.
            if censor.id not in semantic_ids:
                matched_by = "regex"
            elif censor.id in keyword_ids:
                matched_by = "both"
            else:
                matched_by = "semantic"
            # Increment activation_count (P2-9: NULL-safe)
            # F078: auto-escalation removed — a loose auto-created rule can no
            # longer promote itself into a turn-killer. Promotion is human-only.
            censor.activation_count = (censor.activation_count or 0) + 1
            censor.last_activated = now

            await self._emit_event(
                session,
                "censor_triggered",
                {
                    "censor_id": str(censor.id),
                    "matched_text": text_input[:200],
                    "matched_by": matched_by,
                    "score": similarity,
                    "semantic_checked": semantic_checked,
                },
            )

            results.append(
                CensorMatch(
                    id=censor.id,
                    trigger_pattern=censor.trigger_pattern,
                    action=censor.action,
                    reason=censor.reason,
                    domain=censor.domain,
                    score=similarity,
                    matched_by=matched_by,
                    trigger_action=censor.trigger_action,
                    action_instruction=censor.action_instruction,
                    unblock_pattern=censor.unblock_pattern,
                    refuse_keep_tools=bool(censor.refuse_keep_tools),
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

        # DELIBERATELY EXACT (codex P1, 2026-06-09): this is the censor
        # ENFORCEMENT path — completeness is the point. A filtered HNSW scan
        # is approximate (pgvector applies agent_id/active/threshold AFTER
        # the candidate walk), so a true match can be silently missing and
        # an unenforced steer/refuse/abort has no recovery. The censor table
        # is tiny (~50 rows/agent), so the exact scan the audit's D9 flagged
        # as a perf concern costs microseconds here; the read-only
        # _semantic_search below keeps the index-served form instead.
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
        keyword = await self._keyword_search(query, limit, domain, session)
        if not self.embeddings:
            return keyword
        try:
            embedding = await self.embeddings.embed(query, fail_fast=True)
        except Exception:
            logger.warning("Embedding failed for censor search, using the regex match only")
            return keyword
        # Both legs run, not the regex only when embedding fails: a censor whose
        # pattern matches but whose embedding is distant is still a match. One
        # row per censor; a censor both legs found keeps its semantic row.
        by_id: dict[UUID, CensorMatch] = {m.id: m for m in keyword}
        for match in await self._semantic_search(embedding, limit, domain, session):
            by_id[match.id] = match
        return sorted(by_id.values(), key=lambda m: m.score or 0.0, reverse=True)[:limit]

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
            "threshold": _SEARCH_SIMILARITY_FLOOR,
            "limit": limit,
        }
        if domain:
            domain_clause = "AND (domain = :domain OR domain IS NULL)"
            params["domain"] = domain

        # Audit D9: distance-ordered so the HNSW index serves the query
        # (ordering identical — distance ASC == similarity DESC). This is
        # the read-only SEARCH surface — approximate top-k is acceptable
        # here, unlike the enforcement path above which stays exact.
        # ef_search raised above any plausible :limit so post-filters can't
        # evict true matches from the approximate candidate set (review P2).
        await set_local_ef_search(session, 120)
        sql = text(f"""
            SELECT id, 1 - (embedding <=> CAST(:embedding AS vector)) AS similarity
            FROM heart.censors
            WHERE agent_id = :agent_id
              AND active = true
              AND embedding IS NOT NULL
              AND 1 - (embedding <=> CAST(:embedding AS vector)) > :threshold
              {domain_clause}
            ORDER BY embedding <=> CAST(:embedding AS vector)
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
                matched_by="semantic",
                trigger_action=c.trigger_action,
                action_instruction=c.action_instruction,
                unblock_pattern=c.unblock_pattern,
                refuse_keep_tools=bool(c.refuse_keep_tools),
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
                            score=_SEARCH_SIMILARITY_FLOOR,
                            matched_by="regex",
                            trigger_action=censor.trigger_action,
                            action_instruction=censor.action_instruction,
                            unblock_pattern=censor.unblock_pattern,
                            refuse_keep_tools=bool(censor.refuse_keep_tools),
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
    # reembed_all()
    # ------------------------------------------------------------------

    async def reembed_all(self, session: AsyncSession | None = None) -> int:
        """Re-embed every censor of this agent, active or not, from the current embed-text mode.

        Run after changing the mode (scripts/ops/reembed_write_quality.py). A failed embed keeps
        the row's vector and is not counted. Returns the number re-embedded.
        """
        if session is None:
            async with self.db.session() as session:
                count = await self._reembed_all(session)
                await session.commit()
                return count
        return await self._reembed_all(session)

    async def _reembed_all(self, session: AsyncSession) -> int:
        if not self.embeddings:
            return 0
        result = await session.execute(select(Censor).where(Censor.agent_id == self.agent_id))
        count = 0
        for censor in result.scalars().all():
            embed_text = _censor_embed_text(
                censor.trigger_pattern, censor.reason, censor.action_instruction, self._embed_text_mode,
            )
            try:
                censor.embedding = await self.embeddings.embed(embed_text)
                count += 1
            except Exception:
                logger.warning("Re-embed failed for censor %s", censor.id)
        await session.flush()
        return count

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
        """Manually escalate censor severity. steer -> refuse -> abort. No downgrade.

        F078: deliberate operator action only — there is no automatic caller.
        """
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
        action: str | object = _SENTINEL,
        active: bool | object = _SENTINEL,
        session: AsyncSession | None = None,
    ) -> CensorDetail:
        """Update specific fields on an existing censor.

        Only fields explicitly passed are updated. Pass None to clear a field.
        Fields not passed are left unchanged.

        F078: ``action`` and ``active`` are the UI severity-control path. The
        operator is provenance=human, so ANY valid tier (incl. abort) is allowed
        on update — no cap. ``action`` must be one of steer/refuse/abort.
        """
        if session is None:
            async with self.db.session() as session:
                result = await self._update(
                    censor_id, trigger_action=trigger_action,
                    action_instruction=action_instruction,
                    unblock_pattern=unblock_pattern,
                    reason=reason, domain=domain,
                    action=action, active=active, session=session,
                )
                await session.commit()
                return result
        return await self._update(
            censor_id, trigger_action=trigger_action,
            action_instruction=action_instruction,
            unblock_pattern=unblock_pattern,
            reason=reason, domain=domain,
            action=action, active=active, session=session,
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
        action=_SENTINEL,
        active=_SENTINEL,
        session: AsyncSession,
    ) -> CensorDetail:
        censor = await self._get_censor_orm(censor_id, session)
        if censor is None:
            raise ValueError(f"Censor {censor_id} not found")

        SENTINEL = self._SENTINEL
        old_text = _censor_embed_text(
            censor.trigger_pattern, censor.reason, censor.action_instruction, self._embed_text_mode,
        )
        if trigger_action is not SENTINEL:
            censor.trigger_action = trigger_action
        if action_instruction is not SENTINEL:
            censor.action_instruction = action_instruction
        if unblock_pattern is not SENTINEL:
            censor.unblock_pattern = unblock_pattern
        if reason is not SENTINEL and reason is not None and reason != censor.reason:
            censor.reason = reason
        # A change to the embedded text (the reason, and in "reason" mode the action
        # instruction) re-embeds it. On failure the old vector stays rather than none.
        new_text = _censor_embed_text(
            censor.trigger_pattern, censor.reason, censor.action_instruction, self._embed_text_mode,
        )
        if self.embeddings and new_text != old_text:
            try:
                censor.embedding = await self.embeddings.embed(new_text)
            except Exception:
                logger.warning("Embedding refresh failed for censor %s update", censor_id)
        if domain is not SENTINEL:
            censor.domain = domain

        # F078: UI severity-control path. Operator may set any valid tier.
        if action is not SENTINEL and action is not None:
            if action not in _VALID_ACTIONS:
                raise ValueError(f"Invalid censor action: {action!r}")
            old_action = censor.action
            if action != old_action:
                censor.action = action
                await self._emit_event(
                    session,
                    "censor_updated",
                    {
                        "censor_id": str(censor_id),
                        "old_action": old_action,
                        "new_action": action,
                    },
                )

        if active is not SENTINEL and active is not None:
            if not isinstance(active, bool):
                raise ValueError(f"Invalid censor active flag: {active!r}")
            censor.active = active

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
            provenance=censor.provenance or "human",
            refuse_keep_tools=bool(censor.refuse_keep_tools),
        )
