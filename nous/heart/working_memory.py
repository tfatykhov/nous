"""Working memory management — current session state.

Manages session focus, item loading/eviction, and open threads via
JSONB read-modify-write with SELECT FOR UPDATE (P2-8).
All methods follow Brain's session injection pattern (P1-1).

No embeddings needed — working memory is structured, not searched.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from nous.heart.schemas import OpenThread, WorkingMemoryItem, WorkingMemoryState
from nous.storage.database import Database
from nous.storage.models import WorkingMemory

logger = logging.getLogger(__name__)


class WorkingMemoryManager:
    """Manages working memory — current session focus."""

    def __init__(self, db: Database, agent_id: str) -> None:
        self.db = db
        self.agent_id = agent_id

    # ------------------------------------------------------------------
    # get_or_create()
    # ------------------------------------------------------------------

    async def get_or_create(self, session_id: str, session: AsyncSession | None = None) -> WorkingMemoryState:
        """Get existing working memory for session, or create new one."""
        if session is None:
            async with self.db.session() as session:
                result = await self._get_or_create(session_id, session)
                await session.commit()
                return result
        return await self._get_or_create(session_id, session)

    async def _get_or_create(self, session_id: str, session: AsyncSession) -> WorkingMemoryState:
        # UPSERT: INSERT ... ON CONFLICT DO NOTHING
        stmt = (
            pg_insert(WorkingMemory)
            .values(
                agent_id=self.agent_id,
                session_id=session_id,
                items=[],
                open_threads=[],
            )
            .on_conflict_do_nothing(constraint="working_memory_agent_id_session_id_key")
        )
        await session.execute(stmt)
        await session.flush()

        # Fetch the row (either newly created or existing)
        wm = await self._get_wm_orm(session_id, session)
        return self._to_state(wm)

    # ------------------------------------------------------------------
    # focus()
    # ------------------------------------------------------------------

    async def focus(
        self,
        session_id: str,
        task: str,
        frame: str | None = None,
        session: AsyncSession | None = None,
    ) -> WorkingMemoryState:
        """Set the current task and frame."""
        if session is None:
            async with self.db.session() as session:
                result = await self._focus(session_id, task, frame, session)
                await session.commit()
                return result
        return await self._focus(session_id, task, frame, session)

    async def _focus(
        self,
        session_id: str,
        task: str,
        frame: str | None,
        session: AsyncSession,
    ) -> WorkingMemoryState:
        wm = await self._get_wm_orm_for_update(session_id, session)
        if wm is None:
            raise ValueError(f"Working memory for session {session_id} not found")

        wm.current_task = task
        if frame is not None:
            wm.current_frame = frame
        await session.flush()

        return self._to_state(wm)

    # ------------------------------------------------------------------
    # load_item()
    # ------------------------------------------------------------------

    async def load_item(
        self,
        session_id: str,
        item: WorkingMemoryItem,
        session: AsyncSession | None = None,
    ) -> WorkingMemoryState:
        """Add an item to working memory, evicting if at capacity."""
        if session is None:
            async with self.db.session() as session:
                result = await self._load_item(session_id, item, session)
                await session.commit()
                return result
        return await self._load_item(session_id, item, session)

    async def _load_item(
        self,
        session_id: str,
        item: WorkingMemoryItem,
        session: AsyncSession,
    ) -> WorkingMemoryState:
        # P2-8: SELECT FOR UPDATE to prevent concurrent modification
        wm = await self._get_wm_orm_for_update(session_id, session)
        if wm is None:
            raise ValueError(f"Working memory for session {session_id} not found")

        items = list(wm.items or [])
        max_items = wm.max_items or 20

        # Evict lowest relevance if at capacity
        if len(items) >= max_items:
            items = self._evict_lowest(items)

        # Add new item
        items.append(item.model_dump(mode="json"))
        wm.items = items
        await session.flush()

        return self._to_state(wm)

    # ------------------------------------------------------------------
    # evict()
    # ------------------------------------------------------------------

    async def evict(
        self,
        session_id: str,
        ref_id: UUID | None = None,
        session: AsyncSession | None = None,
    ) -> WorkingMemoryState:
        """Evict an item from working memory."""
        if session is None:
            async with self.db.session() as session:
                result = await self._evict(session_id, ref_id, session)
                await session.commit()
                return result
        return await self._evict(session_id, ref_id, session)

    async def _evict(
        self,
        session_id: str,
        ref_id: UUID | None,
        session: AsyncSession,
    ) -> WorkingMemoryState:
        wm = await self._get_wm_orm_for_update(session_id, session)
        if wm is None:
            raise ValueError(f"Working memory for session {session_id} not found")

        items = list(wm.items or [])

        if ref_id is not None:
            # Remove specific item by ref_id
            items = [i for i in items if str(i.get("ref_id")) != str(ref_id)]
        else:
            # Remove item with lowest relevance (P3-6: tie-break by earliest loaded_at)
            items = self._evict_lowest(items)

        wm.items = items
        await session.flush()
        return self._to_state(wm)

    def _evict_lowest(self, items: list[dict]) -> list[dict]:
        """Remove the item with lowest relevance. Tie-break by earliest loaded_at (P3-6)."""
        if not items:
            return items

        # Sort by (relevance ASC, loaded_at ASC) to find eviction candidate
        min_item = min(
            items,
            key=lambda i: (i.get("relevance", 0), i.get("loaded_at", "")),
        )
        items.remove(min_item)
        return items

    # ------------------------------------------------------------------
    # add_thread()
    # ------------------------------------------------------------------

    async def add_thread(
        self,
        session_id: str,
        thread: OpenThread,
        session: AsyncSession | None = None,
    ) -> WorkingMemoryState:
        """Add an open thread (pending item)."""
        if session is None:
            async with self.db.session() as session:
                result = await self._add_thread(session_id, thread, session)
                await session.commit()
                return result
        return await self._add_thread(session_id, thread, session)

    async def _add_thread(
        self,
        session_id: str,
        thread: OpenThread,
        session: AsyncSession,
    ) -> WorkingMemoryState:
        wm = await self._get_wm_orm_for_update(session_id, session)
        if wm is None:
            raise ValueError(f"Working memory for session {session_id} not found")

        threads = list(wm.open_threads or [])
        threads.append(thread.model_dump(mode="json"))
        wm.open_threads = threads
        await session.flush()

        return self._to_state(wm)

    # ------------------------------------------------------------------
    # resolve_thread()
    # ------------------------------------------------------------------

    async def resolve_thread(
        self,
        session_id: str,
        description: str,
        session: AsyncSession | None = None,
    ) -> WorkingMemoryState:
        """Remove a thread by matching description (case-insensitive contains)."""
        if session is None:
            async with self.db.session() as session:
                result = await self._resolve_thread(session_id, description, session)
                await session.commit()
                return result
        return await self._resolve_thread(session_id, description, session)

    async def _resolve_thread(
        self,
        session_id: str,
        description: str,
        session: AsyncSession,
    ) -> WorkingMemoryState:
        wm = await self._get_wm_orm_for_update(session_id, session)
        if wm is None:
            raise ValueError(f"Working memory for session {session_id} not found")

        threads = list(wm.open_threads or [])
        desc_lower = description.lower()

        # Case-insensitive contains match — remove first match
        wm.open_threads = [t for t in threads if desc_lower not in (t.get("description", "")).lower()]
        await session.flush()

        return self._to_state(wm)

    # ------------------------------------------------------------------
    # get()
    # ------------------------------------------------------------------

    async def get(self, session_id: str, session: AsyncSession | None = None) -> WorkingMemoryState | None:
        """Get current working memory state. Returns None if no session exists."""
        if session is None:
            async with self.db.session() as session:
                return await self._get(session_id, session)
        return await self._get(session_id, session)

    async def _get(self, session_id: str, session: AsyncSession) -> WorkingMemoryState | None:
        wm = await self._get_wm_orm(session_id, session)
        if wm is None:
            return None
        return self._to_state(wm)

    # ------------------------------------------------------------------
    # clear()
    # ------------------------------------------------------------------

    async def clear(self, session_id: str, session: AsyncSession | None = None) -> None:
        """Clear working memory for session. DELETE the row."""
        if session is None:
            async with self.db.session() as session:
                await self._clear(session_id, session)
                await session.commit()
                return
        await self._clear(session_id, session)

    async def _clear(self, session_id: str, session: AsyncSession) -> None:
        wm = await self._get_wm_orm(session_id, session)
        if wm is not None:
            await session.delete(wm)
            await session.flush()

    # ------------------------------------------------------------------
    # cleanup_stale() — F049 Mechanism A: TTL safety-net sweep
    # ------------------------------------------------------------------

    async def cleanup_stale(
        self,
        max_age_hours: int = 24,
        batch_size: int = 5000,
    ) -> int:
        """Delete stale working_memory rows for this agent.

        Safety net for session paths that never called end_conversation
        (primary cause: subtask sessions prior to F049 Mechanism B).

        Uses a PostgreSQL transaction-scoped advisory lock keyed on the
        agent_id hash so two replicas cannot race on the same DELETE.
        Deletes in LIMIT-batched chunks via ``ctid IN (SELECT ... LIMIT N)``
        so a single invocation cannot hold a long exclusive lock on the
        table at scale.

        Args:
            max_age_hours: Age threshold. Rows whose ``updated_at`` is older
                than ``now() - max_age_hours`` are deleted. ``<= 0`` disables
                the sweep (returns 0 without issuing any SQL).
            batch_size: Maximum rows deleted per DELETE batch.

        Returns:
            Total rows deleted across all batches. ``0`` when TTL disabled
            or when another replica holds the advisory lock.
        """
        if max_age_hours <= 0:
            return 0

        cutoff = datetime.now(UTC) - timedelta(hours=max_age_hours)
        # Cross-process-stable 31-bit key for pg_try_advisory_xact_lock.
        # Python's builtin hash() has a randomized per-process seed, so two
        # replicas would compute different keys and the lock would not
        # serialize them. SHA-256 → int → mod keeps the key deterministic.
        digest = hashlib.sha256(self.agent_id.encode("utf-8")).digest()
        lock_key = int.from_bytes(digest[:4], "big") % (2**31)
        total_deleted = 0

        async with self.db.session() as session:
            acquired = (
                await session.execute(
                    text("SELECT pg_try_advisory_xact_lock(:k)").bindparams(k=lock_key)
                )
            ).scalar()
            if not acquired:
                logger.debug(
                    "WM sweep skipped — another replica holds the advisory lock for agent %s",
                    self.agent_id,
                )
                return 0

            while True:
                result = await session.execute(
                    text(
                        """
                        DELETE FROM heart.working_memory
                        WHERE ctid IN (
                            SELECT ctid FROM heart.working_memory
                            WHERE agent_id = :agent_id AND updated_at < :cutoff
                            LIMIT :batch_size
                        )
                        RETURNING session_id
                        """
                    ).bindparams(
                        agent_id=self.agent_id,
                        cutoff=cutoff,
                        batch_size=int(batch_size),
                    )
                )
                deleted = result.scalars().all()
                total_deleted += len(deleted)
                if len(deleted) < batch_size:
                    break
            await session.commit()

        if total_deleted:
            logger.info(
                "WM sweep deleted %d rows for agent %s (threshold=%dh)",
                total_deleted,
                self.agent_id,
                max_age_hours,
            )
        else:
            logger.debug("WM sweep: no stale rows for agent %s", self.agent_id)
        return total_deleted

    # ------------------------------------------------------------------
    # F055 — Cross-Turn Residual Activation helpers
    # ------------------------------------------------------------------

    async def list_raw_items(self, agent_id: str, session_id: str) -> list[dict]:
        """F055: return raw JSONB ``items`` (pre-pydantic-parse).

        ResidualActivator needs access to the F055-extension keys
        (``activation``, ``last_surfaced_turn``) which are NOT in
        ``WorkingMemoryItem`` Pydantic v1. This bypasses the parse so
        those extra keys remain accessible. Returns ``[]`` on missing row.
        """
        async with self.db.session() as session:
            result = await session.execute(
                select(WorkingMemory.items)
                .where(WorkingMemory.agent_id == agent_id)
                .where(WorkingMemory.session_id == session_id)
            )
            row = result.scalar_one_or_none()
            return list(row) if row else []

    @staticmethod
    def _is_residual_item(item: object) -> bool:
        """F055 residual entries carry the extra JSONB keys record_surfaced
        writes; items loaded via load_item never have them."""
        return (
            isinstance(item, dict)
            and "activation" in item
            and "last_surfaced_turn" in item
        )

    async def upsert_residual_items(
        self,
        agent_id: str,
        session_id: str,
        items: list[dict],
        max_residual_items: int | None = None,
        current_turn: int | None = None,
        decay_fn=None,
    ) -> None:
        """F055: merge residual-activation entries into WorkingMemory.items.

        Audit E2 (2026-06-09): MERGE, not wholesale replace. The old
        wholesale assignment clobbered curated items loaded via load_item
        (replacing real summaries with placeholder stubs in the next turn's
        prompt) and reduced F055's decay model to a single-recall window.
        Merge semantics:
        - non-residual items are preserved untouched;
        - residual entries are unioned by ref_id — a re-surfaced item is
          refreshed by the new entry; carried entries not in this recall's
          surfaced set are KEPT, because decay is applied read-side by
          load_activations (turn-distance decay + floor governs lifetime);
        - residual entries are ranked by turn-DECAYED activation (codex P2:
          ranking by stored activation let stale turn-1 entries at 1.0
          permanently starve fresh surfaces out of the cap) and capped at
          ``max_residual_items`` when provided. The decay model is the
          caller's (``decay_fn(turns_elapsed) -> float`` — the activator
          owns it; duplicating it here would drift). Without
          ``current_turn``/``decay_fn`` the stored activation ranks as-is.

        Uses an isolated DB session so it can be safely fired as
        ``asyncio.create_task`` from recall_deep. Creates the WorkingMemory
        row if missing (matches F049 lifecycle).
        """
        async with self.db.session() as session:
            wm = await session.execute(
                select(WorkingMemory)
                .where(WorkingMemory.agent_id == agent_id)
                .where(WorkingMemory.session_id == session_id)
                .with_for_update()
            )
            existing = wm.scalars().first()
            existing_items = list(existing.items or []) if existing is not None else []

            non_residual = [d for d in existing_items if not self._is_residual_item(d)]
            # codex P2 (round 4): an item already present as a CURATED entry
            # must not gain a residual twin — both copies rendered into the
            # prompt. Curated wins (it carries the real load_item summary);
            # the residual activation signal is redundant for an item that
            # is already deliberately loaded.
            curated_refs = {
                str(d.get("ref_id"))
                for d in non_residual
                if isinstance(d, dict) and d.get("ref_id") is not None
            }
            merged: dict[str, dict] = {}
            for d in existing_items:
                if (
                    self._is_residual_item(d)
                    and d.get("ref_id") is not None
                    and str(d["ref_id"]) not in curated_refs
                ):
                    merged[str(d["ref_id"])] = d
            for d in items:
                if (
                    isinstance(d, dict)
                    and d.get("ref_id") is not None
                    and str(d["ref_id"]) not in curated_refs
                ):
                    merged[str(d["ref_id"])] = d
            def _activation(d: dict) -> float:
                # One corrupt JSONB value must not kill residual persistence
                # for the whole session (review P3) — coerce defensively.
                try:
                    act = float(d.get("activation", 0.0) or 0.0)
                except (TypeError, ValueError):
                    return 0.0
                if current_turn is not None and decay_fn is not None:
                    try:
                        age = max(0, int(current_turn) - int(d.get("last_surfaced_turn", current_turn)))
                        act *= float(decay_fn(age))
                    except (TypeError, ValueError):
                        pass  # rank undecayed rather than drop the write
                return act

            residual = sorted(merged.values(), key=_activation, reverse=True)
            # codex P2 (round 2): the COMBINED list must respect the row's
            # max_items capacity — capping only the residual portion let
            # curated(20) + residual(20) = 40 escape the contract, and
            # load_item's evict-one-then-append never recovers the
            # oversize. Curated items keep priority (they're deliberate
            # loads); residuals fill the remaining space.
            row_cap = int(getattr(existing, "max_items", None) or 20)
            remaining = max(0, row_cap - len(non_residual))
            cap = (
                remaining if max_residual_items is None
                else min(remaining, max(0, max_residual_items))
            )
            residual = residual[:cap]
            new_items = non_residual + residual

            if existing is None:
                # Create row — F055's record_surfaced may fire before any
                # other WM write (cold session start).
                from datetime import UTC, datetime
                new_wm = WorkingMemory(
                    agent_id=agent_id,
                    session_id=session_id,
                    items=new_items,
                    open_threads=[],
                    current_task=None,
                    current_frame=None,
                    created_at=datetime.now(tz=UTC),
                    updated_at=datetime.now(tz=UTC),
                )
                session.add(new_wm)
            else:
                existing.items = new_items
            await session.commit()

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _get_wm_orm(self, session_id: str, session: AsyncSession) -> WorkingMemory | None:
        """Fetch WorkingMemory ORM without locking."""
        result = await session.execute(
            select(WorkingMemory)
            .where(WorkingMemory.agent_id == self.agent_id)
            .where(WorkingMemory.session_id == session_id)
        )
        return result.scalars().first()

    async def _get_wm_orm_for_update(self, session_id: str, session: AsyncSession) -> WorkingMemory | None:
        """P2-8: Fetch WorkingMemory ORM with FOR UPDATE lock."""
        result = await session.execute(
            select(WorkingMemory)
            .where(WorkingMemory.agent_id == self.agent_id)
            .where(WorkingMemory.session_id == session_id)
            .with_for_update()
        )
        return result.scalars().first()

    def _to_state(self, wm: WorkingMemory) -> WorkingMemoryState:
        """Convert ORM WorkingMemory to WorkingMemoryState DTO."""
        raw_items = wm.items or []
        raw_threads = wm.open_threads or []

        # Defensive coercion: ``loaded_at`` is a required datetime on
        # WorkingMemoryItem, but F055's ``record_surfaced`` historically
        # wrote ``None``, causing a pydantic ValidationError that 500'd
        # /status?dashboard=true and the pre_turn WM init. Patch
        # in-memory so existing bad rows self-heal on read; the writer
        # bug is fixed at residual_activation.py:248 in the same change.
        # F049 sweep eventually evicts the underlying bad JSONB rows.
        now = datetime.now(UTC)
        cleaned_items: list[dict] = []
        for i in raw_items:
            if i.get("loaded_at") is None:
                logger.warning(
                    "WorkingMemoryItem with loaded_at=None in session=%s "
                    "agent=%s ref_id=%s — coercing to now() (F055 residual "
                    "writer historically wrote None; bad row will be "
                    "evicted by F049 sweep)",
                    wm.session_id, wm.agent_id, i.get("ref_id"),
                )
                i = {**i, "loaded_at": now.isoformat()}
            cleaned_items.append(i)
        items = [WorkingMemoryItem(**i) for i in cleaned_items]
        threads = [OpenThread(**t) for t in raw_threads]

        return WorkingMemoryState(
            agent_id=wm.agent_id,
            session_id=wm.session_id,
            current_task=wm.current_task,
            current_frame=wm.current_frame,
            items=items,
            open_threads=threads,
            max_items=wm.max_items or 20,
            item_count=len(items),
        )
