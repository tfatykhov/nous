"""Episode management — what happened.

Manages episodic memory: starting, ending, linking, searching episodes.
All methods follow Brain's session injection pattern (P1-1).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from nous.utils import text_overlap

from nous.brain.embeddings import EmbeddingProvider
from nous.heart.schemas import EpisodeDetail, EpisodeInput, EpisodeSummary
from nous.heart.search import hybrid_search, hybrid_search_multi
from nous.storage.database import Database
from nous.storage.models import Episode, EpisodeDecision, EpisodeProcedure, Event

logger = logging.getLogger(__name__)


class EpisodeManager:
    """Manages episodic memory — what happened."""

    # 006.2: Dedup window and threshold
    _DEDUP_WINDOW_MINUTES = 30
    _DEDUP_THRESHOLD = 0.80

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
    # start()
    # ------------------------------------------------------------------

    async def start(self, input: EpisodeInput, session: AsyncSession | None = None) -> EpisodeDetail:
        """Start a new episode."""
        if session is None:
            async with self.db.session() as session:
                result = await self._start(input, session)
                await session.commit()
                return result
        return await self._start(input, session)

    async def _start(self, input: EpisodeInput, session: AsyncSession) -> EpisodeDetail:
        # 006.2: Dedup — check for similar ongoing episodes in recent window
        cutoff = datetime.now(UTC) - timedelta(minutes=self._DEDUP_WINDOW_MINUTES)
        recent_result = await session.execute(
            select(Episode).where(
                Episode.agent_id == self.agent_id,
                Episode.ended_at.is_(None),  # ongoing = not ended
                Episode.started_at >= cutoff,
            )
        )
        for existing_ep in recent_result.scalars().all():
            if text_overlap(existing_ep.summary or "", input.summary or "") > self._DEDUP_THRESHOLD:
                logger.debug("Reusing existing episode %s (similar summary)", existing_ep.id)
                reloaded = await self._get_episode_orm(existing_ep.id, session)
                return self._to_detail(reloaded)

        # Generate embedding from title + summary
        embedding = None
        if self.embeddings:
            embed_text = f"{input.title or ''} {input.summary}".strip()
            try:
                embedding = await self.embeddings.embed(embed_text)
            except Exception:
                logger.warning("Embedding generation failed for episode start")

        episode = Episode(
            agent_id=self.agent_id,
            title=input.title,
            summary=input.summary,
            detail=input.detail,
            frame_used=input.frame_used,
            trigger=input.trigger,
            participants=input.participants or None,
            tags=input.tags or None,
            embedding=embedding,
            user_id=input.user_id,
            user_display_name=input.user_display_name,
            session_id=input.session_id,  # F022 follow-up
        )
        session.add(episode)
        await session.flush()

        await self._emit_event(session, "episode_started", {"episode_id": str(episode.id)})

        # Re-fetch with eager loading to avoid lazy-load MissingGreenlet
        episode = await self._get_episode_orm(episode.id, session)
        return self._to_detail(episode)

    # ------------------------------------------------------------------
    # update_summary()
    # ------------------------------------------------------------------

    async def update_summary(
        self, episode_id: UUID, summary: dict, session: AsyncSession | None = None
    ) -> None:
        """Store structured summary on episode."""
        if session is None:
            async with self.db.session() as session:
                await self._update_summary(episode_id, summary, session)
                await session.commit()
                return
        await self._update_summary(episode_id, summary, session)

    async def _update_summary(
        self, episode_id: UUID, summary: dict, session: AsyncSession
    ) -> None:
        stmt = select(Episode).where(Episode.id == episode_id)
        result = await session.execute(stmt)
        episode = result.scalar_one_or_none()
        if episode:
            episode.structured_summary = summary

            # 008.3: Backfill text columns from structured summary
            if isinstance(summary, dict):
                if summary.get("title"):
                    episode.title = summary["title"]
                if summary.get("summary"):
                    episode.summary = summary["summary"]
                if summary.get("key_points"):
                    episode.lessons_learned = summary["key_points"]

            # 008.3: Refresh embedding with real content
            if self.embeddings:
                embed_text = f"{episode.title or ''} {episode.summary or ''}".strip()
                if embed_text:
                    try:
                        episode.embedding = await self.embeddings.embed(embed_text)
                    except Exception:
                        logger.debug("Failed to refresh episode embedding after summary")

            await session.flush()

    # ------------------------------------------------------------------
    # bump_compaction_count()
    # ------------------------------------------------------------------

    async def bump_compaction_count(
        self, episode_id: UUID, session: AsyncSession | None = None
    ) -> None:
        """Increment compaction counter on an active episode.

        Note: Episode embedding is NOT refreshed here — it still reflects the
        initial session summary. EpisodeSummarizer refreshes it at session end
        via update_summary(). If the session ends abnormally, the embedding
        may be stale.
        """
        if session is None:
            async with self.db.session() as session:
                await self._bump_compaction_count(episode_id, session)
                await session.commit()
                return
        await self._bump_compaction_count(episode_id, session)

    async def _bump_compaction_count(
        self, episode_id: UUID, session: AsyncSession
    ) -> None:
        stmt = select(Episode).where(Episode.id == episode_id)
        result = await session.execute(stmt)
        episode = result.scalar_one_or_none()
        if episode is None:
            raise ValueError(f"Episode {episode_id} not found")
        episode.compaction_count = (episode.compaction_count or 0) + 1
        await session.flush()

    # ------------------------------------------------------------------
    # end()
    # ------------------------------------------------------------------

    async def end(
        self,
        episode_id: UUID,
        outcome: str,
        lessons_learned: list[str] | None = None,
        surprise_level: float | None = None,
        transcript: str | None = None,  # F025 P3-C
        session: AsyncSession | None = None,
    ) -> EpisodeDetail:
        """Close an episode with outcome and lessons."""
        if session is None:
            async with self.db.session() as session:
                result = await self._end(episode_id, outcome, lessons_learned, surprise_level, transcript, session)
                await session.commit()
                return result
        return await self._end(episode_id, outcome, lessons_learned, surprise_level, transcript, session)

    async def _end(
        self,
        episode_id: UUID,
        outcome: str,
        lessons_learned: list[str] | None,
        surprise_level: float | None,
        transcript: str | None,  # F025 P3-C
        session: AsyncSession,
    ) -> EpisodeDetail:
        episode = await self._get_episode_orm(episode_id, session)
        if episode is None:
            raise ValueError(f"Episode {episode_id} not found")

        now = datetime.now(UTC)
        episode.ended_at = now
        episode.duration_seconds = int((now - episode.started_at).total_seconds())
        episode.outcome = outcome
        episode.lessons_learned = lessons_learned
        episode.surprise_level = surprise_level
        episode.active = False  # 008.3: Mark episode as inactive on close

        # F025 P3-C: Persist transcript
        if transcript:
            episode.transcript = transcript

        # P3-7: Regenerate embedding incorporating outcome + lessons
        if self.embeddings:
            embed_text = (
                f"{episode.title or ''} {episode.summary} {outcome} {' '.join(lessons_learned or [])}"
            ).strip()
            try:
                episode.embedding = await self.embeddings.embed(embed_text)
            except Exception:
                logger.warning("Embedding regeneration failed for episode end")

        await session.flush()

        await self._emit_event(
            session,
            "episode_completed",
            {
                "episode_id": str(episode_id),
                "outcome": outcome,
                "duration": episode.duration_seconds,
            },
        )

        # Re-fetch with eager loading to avoid lazy-load MissingGreenlet
        episode = await self._get_episode_orm(episode_id, session)
        return self._to_detail(episode)

    # ------------------------------------------------------------------
    # link_decision()
    # ------------------------------------------------------------------

    async def link_decision(
        self,
        episode_id: UUID,
        decision_id: UUID,
        session: AsyncSession | None = None,
    ) -> None:
        """Insert into heart.episode_decisions."""
        if session is None:
            async with self.db.session() as session:
                await self._link_decision(episode_id, decision_id, session)
                await session.commit()
                return
        await self._link_decision(episode_id, decision_id, session)

    async def _link_decision(
        self,
        episode_id: UUID,
        decision_id: UUID,
        session: AsyncSession,
    ) -> None:
        link = EpisodeDecision(episode_id=episode_id, decision_id=decision_id)
        session.add(link)
        await session.flush()

    # ------------------------------------------------------------------
    # link_procedure()
    # ------------------------------------------------------------------

    async def link_procedure(
        self,
        episode_id: UUID,
        procedure_id: UUID,
        effectiveness: str | None = None,
        session: AsyncSession | None = None,
    ) -> None:
        """Insert into heart.episode_procedures with optional effectiveness."""
        if session is None:
            async with self.db.session() as session:
                await self._link_procedure(episode_id, procedure_id, effectiveness, session)
                await session.commit()
                return
        await self._link_procedure(episode_id, procedure_id, effectiveness, session)

    async def _link_procedure(
        self,
        episode_id: UUID,
        procedure_id: UUID,
        effectiveness: str | None,
        session: AsyncSession,
    ) -> None:
        link = EpisodeProcedure(
            episode_id=episode_id,
            procedure_id=procedure_id,
            effectiveness=effectiveness,
        )
        session.add(link)
        await session.flush()

    # ------------------------------------------------------------------
    # get()
    # ------------------------------------------------------------------

    async def get(self, episode_id: UUID, session: AsyncSession | None = None) -> EpisodeDetail | None:
        """Fetch episode with linked decision_ids."""
        if session is None:
            async with self.db.session() as session:
                return await self._get(episode_id, session)
        return await self._get(episode_id, session)

    async def _get(self, episode_id: UUID, session: AsyncSession) -> EpisodeDetail | None:
        episode = await self._get_episode_orm(episode_id, session)
        if episode is None:
            return None
        return self._to_detail(episode)

    # ------------------------------------------------------------------
    # list_recent()
    # ------------------------------------------------------------------

    async def list_recent(
        self,
        limit: int = 10,
        outcome: str | None = None,
        hours: int | None = None,
        session: AsyncSession | None = None,
    ) -> list[EpisodeSummary]:
        """List recent episodes ordered by started_at DESC."""
        if session is None:
            async with self.db.session() as session:
                return await self._list_recent(limit, outcome, hours, session)
        return await self._list_recent(limit, outcome, hours, session)

    async def _list_recent(
        self,
        limit: int,
        outcome: str | None,
        hours: int | None,
        session: AsyncSession,
    ) -> list[EpisodeSummary]:
        stmt = (
            select(Episode)
            .where(Episode.agent_id == self.agent_id)
            .where(Episode.ended_at.isnot(None))
            .order_by(Episode.started_at.desc())
            .limit(limit)
        )
        if hours is not None:
            cutoff = datetime.now(UTC) - timedelta(hours=hours)
            stmt = stmt.where(Episode.started_at >= cutoff)
        if outcome is not None:
            stmt = stmt.where(Episode.outcome == outcome)
        else:
            stmt = stmt.where(Episode.outcome != 'abandoned')

        result = await session.execute(stmt)
        episodes = result.scalars().all()

        return [
            EpisodeSummary(
                id=e.id,
                title=e.title,
                summary=e.summary,
                outcome=e.outcome,
                started_at=e.started_at,
                tags=e.tags or [],
                # Without this, consumers (recall_recent) can only show the
                # legacy creation-time echo even for summarized episodes —
                # the recall_deep parent-episode path already COALESCEs to
                # structured_summary->>'summary' (tools.py, codex fix).
                structured_summary=e.structured_summary,
            )
            for e in episodes
        ]

    # ------------------------------------------------------------------
    # list_all() — F021 dashboard browse mode
    # ------------------------------------------------------------------

    async def list_all(
        self,
        limit: int = 50,
        offset: int = 0,
        outcome: str | None = None,
        frame: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        sort: str = "started_at",
        order: str = "desc",
        session: AsyncSession | None = None,
    ) -> tuple[list[EpisodeSummary], int]:
        """Paginated episode list with filters (F021)."""
        if session is None:
            async with self.db.session() as session:
                return await self._list_all(limit, offset, outcome, frame, date_from, date_to, sort, order, session)
        return await self._list_all(limit, offset, outcome, frame, date_from, date_to, sort, order, session)

    async def _list_all(
        self, limit, offset, outcome, frame, date_from, date_to, sort, order, session,
    ) -> tuple[list[EpisodeSummary], int]:
        from sqlalchemy import func as sa_func

        conditions = [Episode.agent_id == self.agent_id]
        if outcome:
            conditions.append(Episode.outcome == outcome)
        if frame:
            conditions.append(Episode.frame_used == frame)
        if date_from:
            conditions.append(Episode.started_at >= date_from)
        if date_to:
            conditions.append(Episode.started_at <= date_to)

        count_q = select(sa_func.count()).select_from(Episode).where(*conditions)
        total = (await session.execute(count_q)).scalar() or 0

        # Sort — VALIDATE against allowlist to prevent attribute injection
        ALLOWED_SORTS = {"started_at", "ended_at", "outcome", "title"}
        if sort not in ALLOWED_SORTS:
            sort = "started_at"
        if order not in ("asc", "desc"):
            order = "desc"
        sort_col = getattr(Episode, sort)
        order_clause = sort_col.desc() if order == "desc" else sort_col.asc()

        q = select(Episode).where(*conditions).order_by(order_clause).limit(limit).offset(offset)
        result = await session.execute(q)
        episodes = list(result.scalars().all())

        # NOTE: There is NO `_to_summary` method in EpisodeManager.
        # list_recent() constructs EpisodeSummary inline (episodes.py:349-358).
        # Replicate the same inline construction here, adding structured_summary
        # for browser expand view:
        summaries = [
            EpisodeSummary(
                id=e.id,
                title=e.title,
                summary=e.summary,
                outcome=e.outcome,
                started_at=e.started_at,
                tags=e.tags or [],
                structured_summary=e.structured_summary if isinstance(e.structured_summary, dict) else None,
            )
            for e in episodes
        ]
        return summaries, total

    # ------------------------------------------------------------------
    # search()
    # ------------------------------------------------------------------

    async def search(
        self,
        query: str,
        limit: int = 10,
        session: AsyncSession | None = None,
        variant_pairs: list[tuple[str, list[float] | None]] | None = None,
    ) -> list[EpisodeSummary]:
        """Hybrid search over episodes using search.py helper.

        Args:
            variant_pairs: F050 — when set with len > 1, routes through
                hybrid_search_multi for RRF fusion across query variants.
                Defaults to None (single-query path; backwards compatible).
        """
        if session is None:
            async with self.db.session() as session:
                return await self._search(query, limit, session, variant_pairs)
        return await self._search(query, limit, session, variant_pairs)

    async def _search(
        self,
        query: str,
        limit: int,
        session: AsyncSession,
        variant_pairs: list[tuple[str, list[float] | None]] | None = None,
    ) -> list[EpisodeSummary]:
        # Generate query embedding
        embedding = None
        if self.embeddings:
            try:
                embedding = await self.embeddings.embed(query)
            except Exception:
                logger.warning("Embedding generation failed for episode search")

        # Audit HT-1 (2026-06-09): episode hybrid search was structurally dead.
        # For episodes `active` is a LIFECYCLE flag — _end() sets active=False
        # on close — not a soft-delete marker, so the default active_filter=true
        # excluded every closed episode (the only ones worth recalling). And
        # `outcome != 'abandoned'` is NULL (→ excluded) for ongoing/recovered
        # episodes by SQL 3-valued logic. Together they excluded ALL episodes.
        #
        # Fix (refined per codex P1): active_filter=False, but DON'T blanket-
        # include every inactive row — the trivial-session path soft-deletes
        # noise via deactivate_episode (active=false, ended_at=NULL). Include
        # ongoing (active=true) OR genuinely-closed (ended_at IS NOT NULL) rows,
        # which excludes deactivated-but-unfinished noise; still drop only
        # literal 'abandoned' via IS DISTINCT FROM so NULL-outcome rows pass.
        _episode_where = (
            "AND (t.active = true OR t.ended_at IS NOT NULL) "
            "AND t.outcome IS DISTINCT FROM 'abandoned'"
        )
        if variant_pairs and len(variant_pairs) > 1:
            results = await hybrid_search_multi(
                session=session,
                table="heart.episodes",
                queries=variant_pairs,
                agent_id=self.agent_id,
                limit=limit,
                active_filter=False,
                extra_where=_episode_where,
            )
        else:
            results = await hybrid_search(
                session=session,
                table="heart.episodes",
                embedding=embedding,
                query_text=query,
                agent_id=self.agent_id,
                limit=limit,
                active_filter=False,
                extra_where=_episode_where,
            )

        if not results:
            return []

        # Fetch full episodes by IDs
        ids = [r[0] for r in results]
        scores = {r[0]: r[1] for r in results}

        ep_result = await session.execute(select(Episode).where(Episode.id.in_(ids)))
        episodes = {e.id: e for e in ep_result.scalars().all()}

        # Preserve search order
        return [
            EpisodeSummary(
                id=e.id,
                title=e.title,
                summary=e.summary,
                outcome=e.outcome,
                started_at=e.started_at,
                tags=e.tags or [],
                score=scores.get(e.id),
            )
            for eid in ids
            if (e := episodes.get(eid)) is not None
        ]

    # ------------------------------------------------------------------
    # deactivate()
    # ------------------------------------------------------------------

    async def deactivate(self, episode_id: UUID, session: AsyncSession | None = None) -> None:
        """Soft-delete an episode (set active=False)."""
        if session is None:
            async with self.db.session() as session:
                await self._deactivate(episode_id, session)
                await session.commit()
                return
        await self._deactivate(episode_id, session)

    async def _deactivate(self, episode_id: UUID, session: AsyncSession) -> None:
        episode = await self._get_episode_orm(episode_id, session)
        if episode:
            episode.active = False
            await session.flush()

    # ------------------------------------------------------------------
    # search_recent_by_embedding()
    # ------------------------------------------------------------------

    async def search_recent_by_embedding(
        self,
        query_embedding: list[float],
        hours: int = 48,
        limit: int = 1,
        session: AsyncSession | None = None,
    ) -> list[tuple[UUID, float]]:
        """Find recent episodes by direct cosine similarity.

        Returns list of (episode_id, cosine_similarity) tuples.
        Only searches episodes within the given time window.
        """
        if session is None:
            async with self.db.session() as session:
                return await self._search_recent_by_embedding(
                    query_embedding, hours, limit, session
                )
        return await self._search_recent_by_embedding(
            query_embedding, hours, limit, session
        )

    async def _search_recent_by_embedding(
        self,
        query_embedding: list[float],
        hours: int,
        limit: int,
        session: AsyncSession,
    ) -> list[tuple[UUID, float]]:
        from sqlalchemy import text

        sql = text("""
            SELECT id, 1 - (embedding <=> CAST(:embedding AS vector)) AS cosine_sim
            FROM heart.episodes
            WHERE agent_id = :agent_id
              AND active = true
              AND outcome != 'abandoned'
              AND embedding IS NOT NULL
              AND started_at > NOW() - make_interval(hours => :hours)
            ORDER BY embedding <=> CAST(:embedding AS vector)
            LIMIT :limit
        """)
        result = await session.execute(sql, {
            "embedding": str(query_embedding),
            "agent_id": self.agent_id,
            "hours": hours,
            "limit": limit,
        })
        return [(row[0], float(row[1])) for row in result.fetchall()]

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _get_episode_orm(self, episode_id: UUID, session: AsyncSession) -> Episode | None:
        """Fetch Episode ORM with eager-loaded relationships, scoped by agent_id."""
        result = await session.execute(
            select(Episode)
            .options(selectinload(Episode.episode_decisions))
            .where(Episode.id == episode_id)
            .where(Episode.agent_id == self.agent_id)
        )
        return result.scalars().first()

    def _to_detail(self, episode: Episode) -> EpisodeDetail:
        """Convert ORM Episode to EpisodeDetail DTO."""
        decision_ids = [ed.decision_id for ed in (episode.episode_decisions or [])]
        return EpisodeDetail(
            id=episode.id,
            agent_id=episode.agent_id,
            title=episode.title,
            summary=episode.summary,
            detail=episode.detail,
            started_at=episode.started_at,
            ended_at=episode.ended_at,
            duration_seconds=episode.duration_seconds,
            frame_used=episode.frame_used,
            trigger=episode.trigger,
            participants=episode.participants or [],
            outcome=episode.outcome,
            surprise_level=episode.surprise_level,
            lessons_learned=episode.lessons_learned or [],
            tags=episode.tags or [],
            decision_ids=decision_ids,
            active=episode.active,
            structured_summary=episode.structured_summary,
            user_id=episode.user_id,
            user_display_name=episode.user_display_name,
            created_at=episode.created_at,
            compaction_count=episode.compaction_count or 0,
        )
