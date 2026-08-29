"""F092: SurfaceService — surface lifecycle, outbox, and live broadcast.

Authoritative state lives on ``nous_system.a2ui_surfaces`` (components +
data_model updated on every mutation); the outbox is a delta log for
connected SSE clients only. Reconnect is hydration-first on the client, so
replay correctness is an optimization, not a durability requirement.

Concurrency notes (review findings, do not simplify away):

- Broadcast happens strictly AFTER commit — a subscriber must never see an
  envelope whose row could still roll back.
- ``BIGSERIAL`` seq values become visible at commit, not insert, so a bare
  ``seq > watermark`` scan can permanently skip a row committed late. Every
  catch-up read therefore only advances over rows older than
  ``_LAG_WINDOW_SECONDS``.
- Subscriber queues are bounded and fed with ``put_nowait``; a slow consumer
  overflows, is dropped, and its stream tells the client to resync (F087
  precedent: drop-on-full by design, never backpressure a producer).
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import delete, select, text, update
from sqlalchemy.exc import IntegrityError

from nous.storage.database import Database
from nous.storage.models import A2uiAction, A2uiOutbox, A2uiSurface

from .dsl import BuiltSurface

logger = logging.getLogger(__name__)

_LAG_WINDOW_SECONDS = 2
_SUBSCRIBER_QUEUE_SIZE = 256


class _LockEntry:
    """A per-surface lock plus the count of tasks holding or awaiting it."""

    __slots__ = ("lock", "refs")

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.refs = 0


class SurfaceService:
    def __init__(self, database: Database, settings: Any, heart: Any = None):
        self._db = database
        self._settings = settings
        self._heart = heart
        self._subscribers: set[asyncio.Queue] = set()
        self._pending_tasks: set[asyncio.Task] = set()
        # Per-surface serialization shared by the ActionRouter AND the expiry
        # sweep (codex P1): an action could pass its live check and still be
        # mid-handler when the sweep claimed the surface, recording both a
        # completed action and contradictory no_objection evidence. One lock
        # registry means expiry waits for in-flight actions and vice versa.
        self._surface_locks: dict[str, _LockEntry] = {}

    @asynccontextmanager
    async def surface_lock(self, surface_id: str) -> AsyncIterator[None]:
        """Serialize mutations of one surface (refcounted; see ActionRouter).

        The refcount mutations sit on either side of the await with no await
        between check and delete, so they are atomic under the event loop.
        """
        entry = self._surface_locks.setdefault(surface_id, _LockEntry())
        entry.refs += 1
        try:
            async with entry.lock:
                yield
        finally:
            entry.refs -= 1
            if entry.refs == 0 and self._surface_locks.get(surface_id) is entry:
                self._surface_locks.pop(surface_id, None)

    # ------------------------------------------------------------------ push

    async def push_built(
        self,
        built: BuiltSurface,
        *,
        dedup_key: str | None = None,
        session_id: str | None = None,
        notify: bool | None = None,
        _dedup_retry: bool = False,
    ) -> str:
        """Persist a built surface and broadcast it. Returns the surface_id.

        With a ``dedup_key`` matching a live surface, the existing surface is
        updated in place (components + data model replaced) instead of a new
        card being created — a hourly heartbeat check must not stack 24 cards.
        Two producers racing the same key are serialized by the partial
        UNIQUE index on (agent_id, dedup_key) WHERE live (codex P2): the
        loser's insert raises and retries once down the update path.
        """
        built.validate()
        await self._censor_gate(built)

        agent_id = self._settings.agent_id
        now = datetime.now(UTC)
        expires_at = (now + built.expires_in) if built.expires_in else None

        # A dedup REPLACEMENT must serialize with in-flight actions on the
        # existing surface (codex P1): without the lock, an action validated
        # against the old components could patch or resolve the freshly
        # committed replacement. Resolve the target id first, then re-run the
        # lookup INSIDE the same per-surface lock the ActionRouter holds.
        if dedup_key:
            async with self._db.session() as session:
                existing_id = (
                    await session.execute(
                        select(A2uiSurface.surface_id).where(
                            A2uiSurface.agent_id == agent_id,
                            A2uiSurface.dedup_key == dedup_key,
                            A2uiSurface.status == "live",
                        )
                    )
                ).scalar_one_or_none()
            if existing_id is not None:
                async with self.surface_lock(existing_id):
                    return await self._push_transaction(
                        built,
                        dedup_key=dedup_key,
                        session_id=session_id,
                        notify=notify,
                        _dedup_retry=_dedup_retry,
                        agent_id=agent_id,
                        now=now,
                        expires_at=expires_at,
                    )
        return await self._push_transaction(
            built,
            dedup_key=dedup_key,
            session_id=session_id,
            notify=notify,
            _dedup_retry=_dedup_retry,
            agent_id=agent_id,
            now=now,
            expires_at=expires_at,
        )

    async def _push_transaction(
        self,
        built: BuiltSurface,
        *,
        dedup_key: str | None,
        session_id: str | None,
        notify: bool | None,
        _dedup_retry: bool,
        agent_id: str,
        now: datetime,
        expires_at: datetime | None,
    ) -> str:
        async with self._db.session() as session:
            existing = None
            if dedup_key:
                existing = (
                    await session.execute(
                        select(A2uiSurface).where(
                            A2uiSurface.agent_id == agent_id,
                            A2uiSurface.dedup_key == dedup_key,
                            A2uiSurface.status == "live",
                        )
                    )
                ).scalar_one_or_none()

            if existing is not None:
                surface_id = existing.surface_id
                priority_changed = existing.priority != built.priority
                existing.components = built.components
                existing.data_model = built.data_model
                existing.title = built.title
                existing.priority = built.priority
                existing.allowed_actions = built.allowed_actions
                # Provenance must follow the content (codex P2): a reused
                # dedup_key describing a NEW occurrence carries a new
                # trace_id — leaving the old one would misdirect
                # course-corrections at the stale decision.
                existing.trace_id = built.trace_id
                if session_id is not None:
                    existing.session_id = session_id
                existing.updated_at = now
                existing.expires_at = expires_at
                if priority_changed:
                    # Priority lives in createSurface metadata and the client
                    # reads it only there (codex P2: plain updates left a
                    # connected companion ordering the card by the OLD
                    # priority until a full reconnect). deleteSurface then
                    # createSurface re-delivers the metadata atomically.
                    envelopes = [
                        {"version": "v1.0", "deleteSurface": {"surfaceId": surface_id}},
                        self._create_envelope(
                            surface_id,
                            built.catalog_id,
                            built.components,
                            built.data_model,
                            existing.nonce,
                            built.priority,
                        ),
                    ]
                else:
                    envelopes = [
                        {
                            "version": "v1.0",
                            "updateComponents": {
                                "surfaceId": surface_id,
                                "components": built.components,
                            },
                        },
                        {
                            "version": "v1.0",
                            "updateDataModel": {
                                "surfaceId": surface_id,
                                "value": built.data_model,
                            },
                        },
                    ]
                created = False
            else:
                surface_id = f"nous:{built.origin}:{built.kind}:{uuid.uuid4().hex[:6]}"
                nonce = secrets.token_urlsafe(16)
                session.add(
                    A2uiSurface(
                        surface_id=surface_id,
                        agent_id=agent_id,
                        origin=built.origin,
                        kind=built.kind,
                        catalog_id=built.catalog_id,
                        status="live",
                        priority=built.priority,
                        title=built.title,
                        components=built.components,
                        data_model=built.data_model,
                        allowed_actions=built.allowed_actions,
                        dedup_key=dedup_key,
                        nonce=nonce,
                        session_id=session_id,
                        trace_id=built.trace_id,
                        expires_at=expires_at,
                    )
                )
                envelopes = [
                    self._create_envelope(
                        surface_id,
                        built.catalog_id,
                        built.components,
                        built.data_model,
                        nonce,
                        built.priority,
                    )
                ]
                created = True

            # Explicit flush so the surface row exists before the outbox FK
            # references it — unit-of-work ordering alone proved unreliable
            # here (observed FK violation with add() + add_all() unflushed).
            try:
                await session.flush()
                rows = [A2uiOutbox(agent_id=agent_id, surface_id=surface_id, envelope=env) for env in envelopes]
                session.add_all(rows)
                await session.commit()
            except IntegrityError:
                # Lost the dedup race: another producer inserted the same
                # live (agent_id, dedup_key) first. Retry once — the lookup
                # now finds the winner and takes the update-in-place path.
                await session.rollback()
                if _dedup_retry or not dedup_key:
                    raise
                return await self.push_built(
                    built,
                    dedup_key=dedup_key,
                    session_id=session_id,
                    notify=notify,
                    _dedup_retry=True,
                )
            seqs = [row.seq for row in rows]

        for seq, env in zip(seqs, envelopes):
            self._broadcast(seq, env)

        should_notify = built.priority >= 1 if notify is None else notify
        if created and should_notify:
            self._schedule_bg(self._notify_telegram(built.title, surface_id))
        return surface_id

    def _create_envelope(
        self,
        surface_id: str,
        catalog_id: str,
        components: list[dict],
        data_model: dict,
        nonce: str,
        priority: int,
    ) -> dict:
        return {
            "version": "v1.0",
            "createSurface": {
                "surfaceId": surface_id,
                "catalogId": catalog_id,
                "sendDataModel": True,
                "metadata": {
                    "extensions": {
                        "com_nous_nonce": nonce,
                        "com_nous_priority": priority,
                    }
                },
                "components": components,
                "dataModel": data_model,
            },
        }

    async def _censor_gate(self, built: BuiltSurface) -> None:
        """Push-time censor check on ALL the surface's prose.

        Title + data model + component literals (codex P1: heartbeat finding
        messages live as Text component literals, not data-model values, so
        a data-model-only flatten never showed them to the censors). Action
        names stay opaque tokens; the risky text a censor was written
        against is what gets matched. abort/refuse block the push; steer
        passes (guidance belongs to the agent's turn, not the surface).
        """
        if self._heart is None:
            return
        prose = built.title + " " + _flatten_strings(built.data_model) + " " + _flatten_strings(built.components)
        blocking = await check_censors_chunked(self._heart, prose, where="push")
        if blocking is not None:
            raise PermissionError(f"surface blocked by censor: {blocking}")

    # ------------------------------------------------------------- mutations

    async def _get_own(self, session: Any, surface_id: str) -> A2uiSurface | None:
        """Agent-scoped surface lookup.

        Every read must carry the agent filter (codex P1): a bare primary-key
        get would let one agent's surface id disclose another agent's
        components, data model, and action nonce.
        """
        return (
            await session.execute(
                select(A2uiSurface).where(
                    A2uiSurface.surface_id == surface_id,
                    A2uiSurface.agent_id == self._settings.agent_id,
                )
            )
        ).scalar_one_or_none()

    async def update_data(self, surface_id: str, path: str | None, value: Any) -> None:
        """Patch the data model (authoritative row + outbox + broadcast)."""
        async with self._db.session() as session:
            surface = await self._get_own(session, surface_id)
            if surface is None or surface.status != "live":
                raise KeyError(surface_id)
            # Deep copy, not dict(): a shallow copy shares its nested objects
            # with SQLAlchemy's committed-state snapshot, so an in-place
            # _pointer_set mutates BOTH. The flush then compares new == old,
            # finds them equal, and emits no UPDATE — the outbox envelope
            # still ships, so live clients update while the authoritative row
            # silently keeps the stale value. Top-level paths happened to
            # work; every nested path (all heartbeat.* patches) was lost.
            model = deepcopy(surface.data_model)
            if path in (None, "", "/"):
                if not isinstance(value, dict):
                    raise ValueError("whole-model replace requires an object")
                model = value
            else:
                _pointer_set(model, path, value)
            surface.data_model = model
            surface.updated_at = datetime.now(UTC)
            body: dict[str, Any] = {"surfaceId": surface_id, "value": value}
            if path not in (None, "", "/"):
                body["path"] = path
            envelope = {"version": "v1.0", "updateDataModel": body}
            row = A2uiOutbox(agent_id=surface.agent_id, surface_id=surface_id, envelope=envelope)
            session.add(row)
            await session.commit()
            seq = row.seq
        self._broadcast(seq, envelope)

    async def resolve(self, surface_id: str, *, status: str = "resolved") -> None:
        """Terminal transition + teardown envelope."""
        envelope = {"version": "v1.0", "deleteSurface": {"surfaceId": surface_id}}
        async with self._db.session() as session:
            surface = await self._get_own(session, surface_id)
            if surface is None:
                raise KeyError(surface_id)
            if surface.status != "live":
                return
            surface.status = status
            surface.resolved_at = datetime.now(UTC)
            row = A2uiOutbox(agent_id=surface.agent_id, surface_id=surface_id, envelope=envelope)
            session.add(row)
            await session.commit()
            seq = row.seq
        self._broadcast(seq, envelope)

    async def expire_sweep(self) -> int:
        """Expire overdue surfaces + prune old rows. Returns surfaces expired.

        Spec 6.2 "silence counts": before a surface expires unactioned, a
        durable ``no_objection`` record is written to a2ui_actions. (Linking
        that evidence into brain.decisions rides with the escalation
        integration, not this PR — a2ui_actions is the durable audit tier.)
        """
        now = datetime.now(UTC)
        agent_id = self._settings.agent_id
        async with self._db.session() as session:
            overdue_ids = (
                (
                    await session.execute(
                        select(A2uiSurface.surface_id).where(
                            A2uiSurface.agent_id == agent_id,
                            A2uiSurface.status == "live",
                            A2uiSurface.expires_at.is_not(None),
                            A2uiSurface.expires_at <= now,
                        )
                    )
                )
                .scalars()
                .all()
            )

        expired = 0
        for surface_id in overdue_ids:
            # Per-surface lock + atomic claim (codex P1, two layers): the
            # lock serializes with an in-flight action's whole dispatch, so
            # expiry cannot record no_objection while a handler is mid-run;
            # the UPDATE..WHERE live claim keeps the evidence write in the
            # same transaction as the flip, so a completed action and the
            # silence claim can never both land.
            async with self.surface_lock(surface_id):
                async with self._db.session() as session:
                    # Deadline REVALIDATED inside the claim (codex P1): a
                    # dedup refresh that ran while we waited for the lock
                    # moves expires_at into the future — claiming on
                    # liveness alone would delete the fresh card and record
                    # false no_objection evidence.
                    claim_now = datetime.now(UTC)
                    claimed = (
                        (
                            await session.execute(
                                update(A2uiSurface)
                                .where(
                                    A2uiSurface.surface_id == surface_id,
                                    A2uiSurface.agent_id == agent_id,
                                    A2uiSurface.status == "live",
                                    A2uiSurface.expires_at.is_not(None),
                                    A2uiSurface.expires_at <= claim_now,
                                )
                                .values(status="expired", resolved_at=claim_now)
                                .returning(A2uiSurface.surface_id)
                            )
                        )
                        .scalars()
                        .all()
                    )
                    if not claimed:
                        continue
                    session.add(
                        A2uiAction(
                            agent_id=agent_id,
                            surface_id=surface_id,
                            action_name="no_objection",
                            actor="system:expiry",
                            context={"expired_at": now.isoformat()},
                            status="completed",
                            completed_at=now,
                        )
                    )
                    envelope = {"version": "v1.0", "deleteSurface": {"surfaceId": surface_id}}
                    row = A2uiOutbox(agent_id=agent_id, surface_id=surface_id, envelope=envelope)
                    session.add(row)
                    await session.commit()
                    seq = row.seq
                self._broadcast(seq, envelope)
                expired += 1

        async with self._db.session() as session:
            await session.execute(
                delete(A2uiOutbox).where(
                    A2uiOutbox.agent_id == agent_id,
                    A2uiOutbox.created_at < now - timedelta(hours=self._settings.a2ui_outbox_nonlive_retention_hours),
                    A2uiOutbox.surface_id.in_(select(A2uiSurface.surface_id).where(A2uiSurface.status != "live")),
                )
            )
            retention_days = self._settings.a2ui_surface_retention_days
            if retention_days > 0:
                await session.execute(
                    delete(A2uiSurface).where(
                        A2uiSurface.agent_id == agent_id,
                        A2uiSurface.status != "live",
                        A2uiSurface.resolved_at.is_not(None),
                        A2uiSurface.resolved_at < now - timedelta(days=retention_days),
                    )
                )
            await session.commit()
        return expired

    async def invalidate_heartbeat_surfaces(self) -> int:
        """Expire live heartbeat surfaces at process start (codex P2).

        The finding store is in-memory: after a restart every fingerprint a
        durable heartbeat surface references is gone, so each of its buttons
        would return "finding not found" for up to 72h. The surfaces are
        provably dead — expire them with an ``invalidated`` audit row (NOT
        ``no_objection``: this is process loss, not user silence).
        """
        now = datetime.now(UTC)
        agent_id = self._settings.agent_id
        async with self._db.session() as session:
            claimed = (
                (
                    await session.execute(
                        update(A2uiSurface)
                        .where(
                            A2uiSurface.agent_id == agent_id,
                            A2uiSurface.status == "live",
                            A2uiSurface.origin == "heartbeat",
                        )
                        .values(status="expired", resolved_at=now)
                        .returning(A2uiSurface.surface_id)
                    )
                )
                .scalars()
                .all()
            )
            teardowns = []
            for surface_id in claimed:
                session.add(
                    A2uiAction(
                        agent_id=agent_id,
                        surface_id=surface_id,
                        action_name="invalidated",
                        actor="system:restart",
                        context={"reason": "finding store reset by process restart"},
                        status="completed",
                        completed_at=now,
                    )
                )
                envelope = {"version": "v1.0", "deleteSurface": {"surfaceId": surface_id}}
                row = A2uiOutbox(agent_id=agent_id, surface_id=surface_id, envelope=envelope)
                session.add(row)
                teardowns.append((row, envelope))
            await session.commit()

        for row, envelope in teardowns:
            self._broadcast(row.seq, envelope)
        return len(claimed)

    # ---------------------------------------------------------------- reads

    async def live_index(self) -> dict:
        """Feed index for cold-start hydration. Never includes the nonce.

        The watermark is read BEFORE the surface list (codex P1): under READ
        COMMITTED each statement sees its own snapshot, so watermark-after
        could cover a create envelope for a surface the list query missed —
        the client would then floor past it and never learn of the surface.
        Watermark-first inverts the race into the harmless direction: a
        surface created in between appears in the list AND its envelope
        replays above the floor (idempotent re-apply).
        """
        async with self._db.session() as session:
            latest = await self._latest_visible_seq(session)
            surfaces = (
                (
                    await session.execute(
                        select(A2uiSurface)
                        .where(
                            A2uiSurface.agent_id == self._settings.agent_id,
                            A2uiSurface.status == "live",
                        )
                        .order_by(A2uiSurface.priority.desc(), A2uiSurface.created_at.desc())
                    )
                )
                .scalars()
                .all()
            )
        return {
            "latest_seq": latest,
            "surfaces": [
                {
                    "surface_id": s.surface_id,
                    "kind": s.kind,
                    "origin": s.origin,
                    "title": s.title,
                    "priority": s.priority,
                    "created_at": s.created_at.isoformat(),
                    "updated_at": s.updated_at.isoformat(),
                }
                for s in surfaces
            ],
        }

    async def snapshot(self, surface_id: str) -> dict | None:
        """Full surface as a single createSurface envelope (v1.0 inline)."""
        async with self._db.session() as session:
            surface = await self._get_own(session, surface_id)
        if surface is None or surface.status != "live":
            return None
        return self._create_envelope(
            surface.surface_id,
            surface.catalog_id,
            surface.components,
            surface.data_model,
            surface.nonce,
            surface.priority,
        )

    async def replay(self, since: int) -> list[tuple[int, dict]] | None:
        """Outbox rows after ``since``, lag-windowed.

        Content deltas replay only for live surfaces, but ``deleteSurface``
        teardowns replay REGARDLESS of surface status: a surface can resolve
        in the gap between the client's snapshot fetch and its SSE subscribe,
        and a live-only filter would drop the only event that removes the
        stale card (codex P2). Applying a teardown for an unknown surface is
        a no-op client-side.

        Returns None when the gap exceeds the replay window — the caller
        must tell the client to resync (hydration-first) instead.
        """
        cutoff = datetime.now(UTC) - timedelta(seconds=_LAG_WINDOW_SECONDS)
        async with self._db.session() as session:
            gap = (
                await session.execute(
                    text(
                        "SELECT count(*) FROM nous_system.a2ui_outbox "
                        "WHERE seq > :since AND agent_id = :agent AND created_at <= :cutoff"
                    ),
                    {"since": since, "agent": self._settings.agent_id, "cutoff": cutoff},
                )
            ).scalar_one()
            if gap > self._settings.a2ui_outbox_replay_window:
                return None
            rows = (
                await session.execute(
                    select(A2uiOutbox.seq, A2uiOutbox.envelope)
                    .join(A2uiSurface, A2uiOutbox.surface_id == A2uiSurface.surface_id)
                    .where(
                        A2uiOutbox.seq > since,
                        A2uiOutbox.agent_id == self._settings.agent_id,
                        A2uiOutbox.created_at <= cutoff,
                        (A2uiSurface.status == "live") | A2uiOutbox.envelope.has_key("deleteSurface"),
                    )
                    .order_by(A2uiOutbox.seq)
                )
            ).all()
        return [(seq, env) for seq, env in rows]

    async def latest_seq(self) -> int:
        """Snapshot-consistent watermark: the TRUE max committed seq.

        Deliberately NOT lag-windowed (codex P2): hydration snapshots read
        current committed state, so every committed envelope's effect is
        already in what the client just fetched. A lag-windowed watermark
        excluded rows younger than 2s, and when those aged in they were
        REPLAYED — a redelivered createSurface replaced the local data model
        and destroyed input typed in that window. The lag window remains
        where it belongs: inside ``replay``'s reads, guarding the poll
        against out-of-order stragglers.
        """
        async with self._db.session() as session:
            return await self._latest_visible_seq(session)

    async def _latest_visible_seq(self, session: Any) -> int:
        latest = (
            await session.execute(
                text("SELECT coalesce(max(seq), 0) FROM nous_system.a2ui_outbox WHERE agent_id = :agent"),
                {"agent": self._settings.agent_id},
            )
        ).scalar_one()
        return int(latest)

    # ------------------------------------------------------------ broadcast

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=_SUBSCRIBER_QUEUE_SIZE)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    def _broadcast(self, seq: int, envelope: dict) -> None:
        for q in list(self._subscribers):
            try:
                q.put_nowait((seq, envelope))
            except asyncio.QueueFull:
                # Slow consumer: drop it and deliver the resync sentinel. The
                # queue is FULL here, so a bare put would raise again and the
                # sentinel would never arrive (codex P2: the stream drained
                # the stale buffer and then waited forever) — evict one
                # buffered item first to guarantee room.
                self._subscribers.discard(q)
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                try:
                    q.put_nowait(None)
                except asyncio.QueueFull:
                    pass

    # ------------------------------------------------------------- plumbing

    def _schedule_bg(self, coro) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            coro.close()
            return
        task = loop.create_task(coro)
        self._pending_tasks.add(task)
        task.add_done_callback(self._pending_tasks.discard)

    async def _notify_telegram(self, title: str, surface_id: str) -> None:
        """One-line Telegram pointer with a deep link (best-effort)."""
        token = self._settings.telegram_bot_token
        chat_id = self._settings.telegram_chat_id
        if not token or not chat_id:
            return
        base = (self._settings.a2ui_public_base_url or "").rstrip("/")
        link = f"{base}/companion#/s/{surface_id}" if base else f"/companion#/s/{surface_id}"
        try:
            async with httpx.AsyncClient() as client:
                await client.post(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    json={"chat_id": chat_id, "text": f"[companion] {title}\n{link}"},
                    timeout=10,
                )
        except Exception:
            logger.warning("F092 Telegram notification failed")


_CENSOR_CHUNK_CHARS = 1800
_CENSOR_CHUNK_OVERLAP = 200
_CENSOR_MAX_CHARS = 20_000


async def check_censors_chunked(heart: Any, text_in: str, *, where: str) -> str | None:
    """Run the censor check over the FULL text in overlapping chunks.

    A single ``[:2000]`` slice let prohibited text bypass an abort censor by
    sitting past the cut (codex P1) while handlers consumed the whole thing.
    Chunks overlap so a phrase straddling a boundary still matches. Text
    beyond the hard cap FAILS CLOSED — silently skipping a tail is exactly
    the bypass this exists to prevent.

    Returns the blocking reason, or None when the text passes. Shared by the
    push gate (SurfaceService) and the action gate (ActionRouter).
    """
    text_in = text_in.strip()
    if not text_in:
        return None
    if len(text_in) > _CENSOR_MAX_CHARS:
        return f"content too large to censor-check ({len(text_in)} chars; cap {_CENSOR_MAX_CHARS})"
    step = _CENSOR_CHUNK_CHARS - _CENSOR_CHUNK_OVERLAP
    for start in range(0, len(text_in), step):
        chunk = text_in[start : start + _CENSOR_CHUNK_CHARS]
        try:
            matches = await heart.check_censors(chunk)
        except Exception:
            logger.warning("F092 censor check failed open on %s", where, exc_info=True)
            return None
        blocking = [m for m in matches if m.action in ("abort", "refuse")]
        if blocking:
            return blocking[0].reason or blocking[0].trigger_pattern
        if start + _CENSOR_CHUNK_CHARS >= len(text_in):
            break
    return None


def _flatten_strings(node: Any) -> str:
    """Collect every string in a JSON tree, bounded by SIZE, not depth.

    A depth cutoff silently dropped prose nested more than a few containers
    deep — bindable, renderable, and invisible to the censor gate (codex
    P1). Iterative walk, so arbitrarily deep valid JSON cannot recurse out;
    collection stops once the censor cap is exceeded, and the chunked
    checker then FAILS CLOSED on oversized content rather than skipping it.
    """
    parts: list[str] = []
    total = 0
    stack: list[Any] = [node]
    while stack and total <= _CENSOR_MAX_CHARS:
        current = stack.pop()
        if isinstance(current, str):
            parts.append(current)
            total += len(current) + 1
        elif isinstance(current, dict):
            # Keys too (codex P1): toDisplayString JSON-serializes bound
            # objects, so a prohibited phrase used as a KEY is rendered.
            stack.extend(k for k in current.keys() if isinstance(k, str))
            stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(current)
    return " ".join(parts)


def _pointer_set(model: dict, pointer: str, value: Any) -> None:
    """RFC 6901 upsert into ``model``; ``value=None`` deletes the key.

    Missing intermediates are created: numeric-token children become lists,
    everything else objects (mirrored by the client's pointer.ts).
    """
    tokens = [t.replace("~1", "/").replace("~0", "~") for t in pointer.lstrip("/").split("/")]
    node: Any = model
    for i, token in enumerate(tokens[:-1]):
        nxt = tokens[i + 1]
        if isinstance(node, list):
            idx = int(token)
            while len(node) <= idx:
                node.append(None)
            if node[idx] is None or not isinstance(node[idx], (dict, list)):
                node[idx] = [] if nxt.isdigit() else {}
            node = node[idx]
        else:
            if token not in node or not isinstance(node[token], (dict, list)):
                node[token] = [] if nxt.isdigit() else {}
            node = node[token]
    last = tokens[-1]
    if isinstance(node, list):
        idx = int(last)
        if value is None:
            if 0 <= idx < len(node):
                node.pop(idx)
        else:
            while len(node) <= idx:
                node.append(None)
            node[idx] = value
    else:
        if value is None:
            node.pop(last, None)
        else:
            node[last] = value
