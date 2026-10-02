"""Compensation registry and snapshot store (harness Phase 2.8).

A compensator undoes a successful tool call given its ledger row and a
snapshot of the state before the call. The registry is separate from
``tool_classes.py`` (which is a leaf with no ``nous`` imports) because a
compensator needs the database and domain objects at runtime.

Snapshot lifecycle:
  1. Before dispatch of a compensable tool -- in an undoable context, or in
     a background one whose review card can be published, so that something
     can revert from it -- ``SnapshotStore.capture`` persists the prior state.
     Spawning tools (schedule_task, heartbeat_check_create) are deliberately
     NOT compensable: cancelling after the first fire does not undo the work
     it already started.
  2. On ``review.revert``, the handler reads the snapshot and calls the
     compensator. ``mark_reverted`` is idempotent (double revert = no-op).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select, update

from nous.api.tool_classes import TOOL_CLASSES
from nous.storage.models import CompensationSnapshot

logger = logging.getLogger(__name__)

# Cap for file snapshots: matches the read_file tool's 1 MiB limit.
_FILE_SNAPSHOT_MAX_BYTES = 1 * 1024 * 1024

# How long a revert waits for its target's write-path lock (a write_file to
# that path is in flight) before it reports failure and leaves the card live.
_REVERT_LOCK_WAIT_SECONDS = 5.0

# One lock per resolved target path, shared by every runner fork in the
# process: a write_file's snapshot capture and its write run inside one
# critical section, so two concurrent writes to a path cannot both snapshot
# the same prior content (the second revert would then erase the first write).
_write_path_locks: dict[str, asyncio.Lock] = {}


def write_path_key(path: str, workspace_dir: str) -> str:
    """The key a write_file target is serialized on: the resolved path."""
    import os

    full_path = os.path.join(workspace_dir, path) if not os.path.isabs(path) else path
    return os.path.realpath(full_path)


def write_path_lock(path: str, workspace_dir: str) -> asyncio.Lock:
    """The process-wide lock for a write_file target."""
    key = write_path_key(path, workspace_dir)
    lock = _write_path_locks.get(key)
    if lock is None:
        lock = _write_path_locks[key] = asyncio.Lock()
    return lock


def write_path_lock_is(full_path: str, lock: asyncio.Lock) -> bool:
    """Whether ``lock`` is the lock for ``full_path`` as it resolves now."""
    return _write_path_locks.get(write_path_key(full_path, "")) is lock


def release_write_path_lock(lock: asyncio.Lock) -> None:
    """Release ``lock`` and drop idle entries so the map stays bounded."""
    lock.release()
    for key, held in list(_write_path_locks.items()):
        if held is lock and not held.locked() and not getattr(held, "_waiters", None):
            del _write_path_locks[key]


def release_write_path_lock_after(lock: asyncio.Lock, worker: asyncio.Future | None) -> None:
    """Release ``lock`` now, or -- while ``worker`` (a write_file's thread
    task) is still running -- only once it finishes. A cancelled or timed-out
    write returns before its thread does; releasing at once would let the
    next write snapshot and dispatch while the orphan can still rename over
    it. The release is deferred, never the caller: cancellation still
    propagates immediately, and the lock is freed the moment the thread ends."""
    if worker is None or worker.done():
        release_write_path_lock(lock)
        return

    def _done(task: asyncio.Future) -> None:
        if not task.cancelled():
            task.exception()  # retrieved: the cancelled caller cannot report it
        release_write_path_lock(lock)

    worker.add_done_callback(_done)


class SnapshotBlocksDispatch(Exception):
    """Raised when a required snapshot cannot be captured, preventing the dispatch.

    Only raised in undoable contexts where a missing snapshot would silently
    allow a non-revertible side effect past the undoable guarantee -- and, in
    any context, when a write_file's path lock is still held after its bounded
    wait (``AgentRunner._acquire_write_lock``): refused the same way.
    """


@dataclass(frozen=True)
class CompensationResult:
    """What a compensator did."""

    success: bool
    message: str


Compensator = Callable[[UUID, dict[str, Any], Any], Awaitable[CompensationResult]]


class CompensationRegistry:
    """Runtime registry of compensator functions, keyed by tool name."""

    def __init__(self) -> None:
        self._compensators: dict[str, Compensator] = {}

    def register(self, tool_name: str, fn: Compensator) -> None:
        tc = TOOL_CLASSES.get(tool_name)
        if tc is None or not tc.compensable:
            raise ValueError(
                f"cannot register a compensator for {tool_name!r}: it must be declared compensable in TOOL_CLASSES"
            )
        self._compensators[tool_name] = fn

    def get(self, tool_name: str) -> Compensator | None:
        return self._compensators.get(tool_name)

    def is_registered(self, tool_name: str) -> bool:
        return tool_name in self._compensators


class SnapshotStore:
    """Reads and writes ``nous_system.compensation_snapshots`` (migration 080)."""

    def __init__(self, database: Any, agent_id: str, *, timeout: float = 5.0) -> None:
        self._db = database
        self._agent_id = agent_id
        self._timeout = timeout

    async def capture(
        self,
        *,
        ledger_entry_id: UUID,
        tool_name: str,
        snapshot_data: dict[str, Any],
        card_pending: bool = False,
    ) -> UUID:
        """Persist the prior state BEFORE the call is dispatched.

        ``card_pending`` records, in the same insert, the intent to publish a
        review card: written ahead of the side effect, so a process that dies
        at any point after dispatch still leaves a marker the pending-card
        sweep finds (codex P1 on #652). The sweep and the post-call path
        clear it once the ledger shows the call cannot have changed anything,
        or once the card is published.
        """
        if card_pending:
            snapshot_data = {**snapshot_data, "_card_pending": True, "_card_tool_name": tool_name}
        snapshot_id = uuid4()
        row = CompensationSnapshot(
            id=snapshot_id,
            ledger_entry_id=ledger_entry_id,
            agent_id=self._agent_id,
            tool_name=tool_name,
            snapshot_data=snapshot_data,
        )

        async def _write() -> None:
            async with self._db.session() as s:
                s.add(row)
                await s.commit()

        await asyncio.wait_for(_write(), timeout=self._timeout)
        return snapshot_id

    async def get_by_ledger_entry(self, ledger_entry_id: UUID) -> CompensationSnapshot | None:
        async with self._db.session() as s:
            result = await s.execute(
                select(CompensationSnapshot)
                .where(CompensationSnapshot.ledger_entry_id == ledger_entry_id)
                .where(CompensationSnapshot.agent_id == self._agent_id)
                .limit(1)
            )
            return result.scalar_one_or_none()

    async def mark_reverted(
        self,
        snapshot_id: UUID,
        *,
        result_message: str,
    ) -> bool:
        """Mark a snapshot as reverted. Returns False if already reverted (idempotent)."""
        async with self._db.session() as s:
            res = await s.execute(
                update(CompensationSnapshot)
                .where(CompensationSnapshot.id == snapshot_id)
                .where(CompensationSnapshot.agent_id == self._agent_id)
                .where(CompensationSnapshot.reverted_at.is_(None))
                .values(reverted_at=datetime.now(UTC), revert_result=result_message)
            )
            await s.commit()
            return (res.rowcount or 0) == 1

    async def mark_card_published(
        self,
        ledger_entry_id: UUID,
    ) -> bool:
        """Clear the pending-card marker: the card was published, or the
        ledger shows the call changed nothing.

        Returns whether a row was updated (codex P1 on #652: durable card publication).
        """

        async def _write() -> bool:
            async with self._db.session() as s:
                row = (
                    await s.execute(
                        select(CompensationSnapshot)
                        .where(CompensationSnapshot.ledger_entry_id == ledger_entry_id)
                        .where(CompensationSnapshot.agent_id == self._agent_id)
                        .limit(1)
                        # Row-locked read-modify-write: the card marker and
                        # the written state are merged into one JSONB value
                        # by different tasks, and an unlocked merge could
                        # write back a copy missing the other's key.
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if row is None:
                    return False
                data = dict(row.snapshot_data or {})
                if not data.get("_card_pending"):
                    return True  # already cleared
                data.pop("_card_pending", None)
                data.pop("_card_tool_name", None)
                row.snapshot_data = data
                await s.commit()
                return True

        return await asyncio.wait_for(_write(), timeout=self._timeout)

    async def get_pending_cards(
        self,
        limit: int = 10,
        *,
        absent_ledger_after_seconds: float = 3600.0,
    ) -> list[tuple[UUID, str, str | None]]:
        """Snapshots whose review card is still pending, with their ledger
        status: ``(ledger_entry_id, tool_name, status)``.

        The intent is written before dispatch, so the ledger decides what a
        marker means: ``success``/``unknown`` -- publish; ``error``/``blocked``
        -- nothing changed, clear it. A ``pending`` row is a call still in
        flight (or, after a restart, one the startup sweep turns ``unknown``)
        and is excluded HERE, in SQL, so long-running calls cannot fill the
        batch and starve newer cards. A snapshot with no ledger row (the
        ledger insert fails open) is returned with status None once older
        than ``absent_ledger_after_seconds``: the call may have run, and the
        compensators' own state guards make a card for a call that never
        landed harmless. (codex P1 on #652: durable card publication.)
        """
        from datetime import timedelta

        from sqlalchemy import and_, or_

        from nous.storage.models import ExecutionLedgerEntry

        cutoff = datetime.now(UTC) - timedelta(seconds=absent_ledger_after_seconds)

        async def _read() -> list[tuple[UUID, str, str | None]]:
            async with self._db.session() as s:
                result = await s.execute(
                    select(
                        CompensationSnapshot.ledger_entry_id,
                        CompensationSnapshot.tool_name,
                        ExecutionLedgerEntry.status,
                    )
                    .outerjoin(
                        ExecutionLedgerEntry,
                        ExecutionLedgerEntry.id == CompensationSnapshot.ledger_entry_id,
                    )
                    .where(CompensationSnapshot.agent_id == self._agent_id)
                    .where(CompensationSnapshot.reverted_at.is_(None))
                    .where(CompensationSnapshot.snapshot_data["_card_pending"].astext == "true")
                    .where(
                        or_(
                            ExecutionLedgerEntry.status.in_(("success", "unknown", "error", "blocked")),
                            and_(
                                ExecutionLedgerEntry.id.is_(None),
                                CompensationSnapshot.created_at < cutoff,
                            ),
                        )
                    )
                    .order_by(CompensationSnapshot.created_at)
                    .limit(limit)
                )
                return [(row[0], row[1], row[2]) for row in result.all()]

        return await asyncio.wait_for(_read(), timeout=self._timeout)

    async def record_written_state_in(
        self,
        session: Any,
        ledger_entry_id: UUID,
        written: dict[str, Any],
        *,
        prior: dict[str, Any] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        """Merge ``written`` -- the state the call left behind -- into its
        snapshot, so the revert can refuse once anything has changed that
        state since. Runs on the MUTATION's own ``session`` and never
        commits: the snapshot update commits or rolls back together with the
        state change itself, so no crash or cancellation after that commit
        can leave a change whose revert lacks its written state (codex P1 on
        #652). ``prior``, when given, replaces the pre-dispatch prior state
        (read inside the call's own transaction, it is exact where the
        pre-dispatch read could be overtaken by a concurrent write);
        ``extra`` top-level fields replace their pre-dispatch values for the
        same reason. Raises LookupError when the snapshot row is missing, so
        the mutation rolls back rather than commit unrevertibly."""
        row = (
            await session.execute(
                select(CompensationSnapshot)
                .where(CompensationSnapshot.ledger_entry_id == ledger_entry_id)
                .where(CompensationSnapshot.agent_id == self._agent_id)
                .limit(1)
                # Row-locked read-modify-write: the card marker and the
                # written state are merged into one JSONB value by different
                # tasks, and an unlocked merge could write back a copy
                # missing the other's key.
                .with_for_update()
            )
        ).scalar_one_or_none()
        if row is None:
            raise LookupError(
                f"refused to keep the change revertible: its compensation snapshot (ledger entry "
                f"{ledger_entry_id}) is missing, so nothing was changed; retrying is safe"
            )
        merged = {**(row.snapshot_data or {}), "written": written}
        if prior is not None:
            merged["prior"] = prior
        if extra:
            merged.update(extra)
        row.snapshot_data = merged
        await session.flush()

    async def check_enabled(self, name: str) -> bool | None:
        """Whether dynamic check ``name`` is enabled right now, or None when
        no such check exists. Read before a ``heartbeat_check_manage``
        disable so its revert restores the prior state."""
        from nous.storage.models import DynamicCheckModel

        async def _read() -> bool | None:
            async with self._db.session() as s:
                result = await s.execute(
                    select(DynamicCheckModel.enabled)
                    .where(DynamicCheckModel.agent_id == self._agent_id)
                    .where(DynamicCheckModel.name == name)
                )
                return result.scalar_one_or_none()

        return await asyncio.wait_for(_read(), timeout=self._timeout)

    async def decision_state(self, decision_id: str) -> dict[str, Any] | None:
        """The review fields ``Brain.review`` overwrites, as they are now, or
        None when this agent has no such decision. Read before a
        ``resolve_decision`` so its revert restores exactly this state."""
        from nous.storage.models import Decision

        async def _read() -> dict[str, Any] | None:
            async with self._db.session() as s:
                row = (
                    await s.execute(
                        select(
                            Decision.outcome,
                            Decision.outcome_result,
                            Decision.reviewed_at,
                            Decision.reviewer,
                            Decision.superseded_by,
                        )
                        .where(Decision.id == UUID(decision_id))
                        .where(Decision.agent_id == self._agent_id)
                    )
                ).first()
            if row is None:
                return None
            return {
                "outcome": row[0],
                "outcome_result": row[1],
                "reviewed_at": row[2].isoformat() if row[2] is not None else None,
                "reviewer": row[3],
                "superseded_by": str(row[4]) if row[4] is not None else None,
            }

        return await asyncio.wait_for(_read(), timeout=self._timeout)


async def snapshot_for_write_file(
    path: str,
    workspace_dir: str,
) -> dict[str, Any]:
    """Capture the prior state of a file before write_file overwrites it.

    I/O is offloaded to a worker thread so the event loop is never stalled.
    The prior content is recorded as bytes (``prior_b64``, with its
    ``prior_sha256``). Files larger than ``_FILE_SNAPSHOT_MAX_BYTES`` are
    flagged ``oversized=True`` and an existing file whose content cannot be
    read carries ``capture_error``; neither has ``prior_b64`` captured, and callers in undoable contexts
    should raise ``SnapshotBlocksDispatch`` rather than proceed without a snapshot.

    A path ``write_file`` would refuse (outside the workspace, via ``..`` or a
    symlink) is checked with the handler's own ``_validate_path`` BEFORE any
    filesystem access and flagged ``invalid_path``: nothing outside the
    workspace is ever stat'ed or read into a snapshot.
    """
    import base64
    import hashlib
    import os
    import stat
    from pathlib import Path

    from nous.api.builtin_tools import PreconditionFailed, _parent_dir, _ParentMissing, _validate_path

    nofollow = getattr(os, "O_NOFOLLOW", 0)
    nonblock = getattr(os, "O_NONBLOCK", 0)
    try:
        target = _validate_path(path, workspace_dir)
    except ValueError as exc:
        return {"path": path, "invalid_path": str(exc)}
    # Every filesystem operation below uses the validated, resolved path --
    # never the caller's -- and runs in one worker-thread pass over one open
    # file. The final component is opened without following a symlink, the
    # size comes from that descriptor, and after the read the path is
    # validated again and must still name the very file that was read: a
    # symlink retargeted outside the workspace between the check and the
    # read can never have its target's content captured.
    full_path = str(target)
    root = Path(workspace_dir).resolve()

    def _capture() -> tuple[bool, bytes | None, bool]:
        try:
            # Walked down from the workspace root without following a symlink
            # at any level (builtin_tools._parent_dir), then the final
            # component opened O_NOFOLLOW; O_NONBLOCK: a FIFO with no writer
            # must not block the snapshot.
            with _parent_dir(target, root) as (dfd, name):
                fd = os.open(name, os.O_RDONLY | nofollow | nonblock, dir_fd=dfd)
        except (FileNotFoundError, _ParentMissing):
            return False, None, False
        except PreconditionFailed as exc:
            raise ValueError(str(exc)) from exc
        with os.fdopen(fd, "rb") as f:
            st = os.fstat(f.fileno())
            if not stat.S_ISREG(st.st_mode):
                raise ValueError(f"{path!r} is not a regular file")
            if st.st_size > _FILE_SNAPSHOT_MAX_BYTES:
                return True, None, True
            data = f.read(_FILE_SNAPSHOT_MAX_BYTES + 1)
            recheck = os.stat(_validate_path(path, workspace_dir))
            if (recheck.st_dev, recheck.st_ino) != (st.st_dev, st.st_ino):
                raise ValueError(f"{path!r} changed while it was being snapshotted")
        if len(data) > _FILE_SNAPSHOT_MAX_BYTES:
            return True, None, True
        return True, data, False

    existed = True
    prior: bytes | None = None
    oversized = False
    capture_error: str | None = None
    try:
        existed, prior, oversized = await asyncio.to_thread(_capture)
    except Exception as exc:
        # The file exists but its content could not be read safely: the
        # snapshot cannot restore it, and callers must know that (capture_error).
        prior = None
        capture_error = f"{type(exc).__name__}: {exc}"
    # The prior content is kept as BYTES (base64), never decoded: a file that
    # is not valid UTF-8 is restored byte for byte (codex P1 on #652).
    snap: dict[str, Any] = {
        "path": path,
        "full_path": full_path,
        # The revert walks down from here without following symlinks.
        "workspace_root": str(root),
        "existed": existed,
        "prior_b64": base64.b64encode(prior).decode("ascii") if prior is not None else None,
        "prior_sha256": hashlib.sha256(prior).hexdigest() if prior is not None else None,
        "oversized": oversized,
    }
    if capture_error is not None:
        snap["capture_error"] = capture_error
    return snap


async def compensate_write_file(
    entry_id: UUID,
    snapshot_data: dict[str, Any],
    deps: Any,
) -> CompensationResult:
    """Restore prior file bytes, or delete the file if the call created it.

    Every step goes through the same compare-and-replace primitive as the
    forward write (``builtin_tools.atomic_replace_bytes`` /
    ``remove_if_matches``): the target is never followed through a symlink,
    the prior bytes land via a temp file and an atomic rename (a failed
    restore leaves the file untouched and retryable), and the change is made
    only while the file still holds exactly what the call wrote
    (``written_content_hash``) -- anything else is a newer change and the
    revert is refused rather than discard it. A snapshot that does not
    record what was written cannot make that distinction and is refused.

    Idempotent: a file already back at its prior state (or, for a created
    file, already absent) reports success, so a revert whose completion was
    not recorded can simply be retried. Before looking, the revert revokes
    this call's write fence: a write orphaned by a cancelled call can then
    no longer land after the revert decided the file was already reverted.
    """
    import base64

    full_path = snapshot_data.get("full_path", "")
    existed = snapshot_data.get("existed", False)
    written_content_hash: str | None = snapshot_data.get("written_content_hash")
    written_size = snapshot_data.get("written_size")

    workspace_root = snapshot_data.get("workspace_root")
    if not full_path:
        return CompensationResult(False, "no path in snapshot")
    if written_content_hash is None or not isinstance(written_size, int):
        # Without the hash of what the call wrote there is no way to tell our
        # write from a newer one, so the revert could destroy newer content.
        return CompensationResult(False, "revert refused: snapshot does not record what was written")
    prior: bytes | None = None
    if existed:
        prior_b64 = snapshot_data.get("prior_b64")
        if not isinstance(prior_b64, str):
            return CompensationResult(False, "file existed but prior content not captured")
        prior = base64.b64decode(prior_b64)
    if not isinstance(workspace_root, str) or not workspace_root:
        # Without the root the revert cannot walk to the file without
        # following a symlink somewhere on the way.
        return CompensationResult(False, "revert refused: snapshot does not record its workspace")

    # The same per-path lock write_file holds across its snapshot and write:
    # held across the check AND the restore, no runner write can land between
    # them and be overwritten by this revert.
    lock = write_path_lock(full_path, "")
    try:
        await asyncio.wait_for(lock.acquire(), timeout=_REVERT_LOCK_WAIT_SECONDS)
    except TimeoutError:
        # Never wait forever: this runs under the card's surface lock.
        return CompensationResult(
            False, f"revert not attempted: a write to {full_path!r} is still in flight; nothing was changed, retry"
        )
    try:
        return await _check_and_restore_write_file(
            entry_id, full_path, workspace_root, existed, prior, written_size, written_content_hash
        )
    finally:
        release_write_path_lock(lock)


async def _check_and_restore_write_file(
    entry_id: UUID,
    full_path: str,
    workspace_root: str,
    existed: bool,
    prior: bytes | None,
    written_size: int,
    written_content_hash: str,
) -> CompensationResult:
    """``compensate_write_file``'s check and restore; the caller holds the
    target's write-path lock."""
    import hashlib
    from pathlib import Path

    from nous.api.builtin_tools import (
        ABSENT,
        PreconditionFailed,
        atomic_replace_bytes,
        file_digest,
        remove_if_matches,
        revoke_write_fence,
    )

    target = Path(full_path)
    root = Path(workspace_root)
    limit = max(written_size, len(prior or b""))

    def _revert() -> CompensationResult:
        revoke_write_fence(str(entry_id))
        current = file_digest(target, limit, root=root)
        if existed:
            assert prior is not None
            if current == hashlib.sha256(prior).hexdigest():
                return CompensationResult(True, f"{full_path} already holds its prior content")
            if current == ABSENT:
                # Our write left the file present: its absence now is a newer change.
                return CompensationResult(False, f"revert refused: {full_path!r} was removed after the original write")
            if current != written_content_hash:
                raise PreconditionFailed("modified")
            atomic_replace_bytes(target, prior, expected=written_content_hash, limit=limit, root=root)
            return CompensationResult(True, f"restored prior content of {full_path}")
        if current == ABSENT:
            return CompensationResult(True, "file already absent")
        if current != written_content_hash:
            raise PreconditionFailed("modified")
        remove_if_matches(target, written_content_hash, limit=limit, root=root)
        return CompensationResult(True, f"deleted {full_path} (was new)")

    try:
        return await asyncio.to_thread(_revert)
    except PreconditionFailed:
        return CompensationResult(
            False,
            f"revert refused: {full_path!r} was modified after the original write; "
            "revert would overwrite newer content",
        )
    except Exception as exc:
        return CompensationResult(False, f"revert failed: {exc}")


def snapshot_is_revertible(tool_name: str, snapshot_data: dict[str, Any]) -> bool:
    """Whether a snapshot records everything its compensator's guard needs.

    A card must not offer Revert for a snapshot its compensator would refuse:
    the DB tools need the ``written`` state (recorded inside the call's own
    transaction, so a call that never committed has none); write_file
    records what it writes before dispatch.
    """
    data = snapshot_data or {}
    if tool_name == "write_file":
        return (
            bool(data.get("written_content_hash"))
            and isinstance(data.get("written_size"), int)
            and bool(data.get("workspace_root"))
        )
    if tool_name in ("resolve_decision", "heartbeat_check_manage"):
        return isinstance(data.get("written"), dict)
    return False


async def compensate_heartbeat_check_manage(
    entry_id: UUID,
    snapshot_data: dict[str, Any],
    deps: Any,
) -> CompensationResult:
    """Restore a heartbeat check the call disabled to its prior state.

    Only ``action="disable"`` is compensable (``is_compensable_call``), so the
    runner only snapshots that action, recording ``prior_enabled``. A check
    that was already disabled before the call is left disabled (the disable
    was a no-op); a check whose prior state is unknown is NOT enabled, since
    that could start autonomous work that was inactive before the call.
    """
    check_name = snapshot_data.get("check_name")
    if not check_name:
        return CompensationResult(False, "no check_name in snapshot")
    if snapshot_data.get("action") != "disable":
        return CompensationResult(False, f"action {snapshot_data.get('action')!r} is not revertible")
    if "prior_enabled" not in snapshot_data:
        return CompensationResult(
            False, f"prior state of check {check_name!r} was not recorded (snapshot predates capture); not enabling"
        )
    prior_enabled = snapshot_data["prior_enabled"]
    if prior_enabled is False:
        return CompensationResult(True, f"check {check_name!r} was already disabled before the call; left disabled")
    if prior_enabled is not True:
        return CompensationResult(False, f"prior state of check {check_name!r} is unknown; not enabling")
    written = snapshot_data.get("written")
    if not (isinstance(written, dict) and written.get("check_id") and written.get("enabled_state_token")):
        return CompensationResult(
            False,
            f"revert refused: the state the disable of {check_name!r} wrote was not recorded, "
            "so a later enable/disable cannot be ruled out",
        )
    loader = getattr(deps, "heartbeat_loader", None)
    if loader is None:
        return CompensationResult(False, "heartbeat loader not available")
    try:
        # Conditional on the check still being exactly what this disable left:
        # a later explicit disable (or enable) replaced the token, and
        # re-enabling then would override that newer intent.
        if await loader.enable_if_unchanged(check_name, written["check_id"], written["enabled_state_token"]):
            return CompensationResult(True, f"re-enabled check {check_name!r}")
        # Idempotent: the check (the same row) is enabled again -- by an
        # earlier revert whose completion was not recorded, or by hand. That
        # is the state this revert restores, so report it done.
        if await loader.is_enabled(check_name, written["check_id"]):
            return CompensationResult(True, f"check {check_name!r} is already enabled")
        return CompensationResult(
            False,
            f"revert refused: check {check_name!r} was changed or removed after the original disable",
        )
    except Exception as exc:
        return CompensationResult(False, f"revert failed: {exc}")


async def compensate_resolve_decision(
    entry_id: UUID,
    snapshot_data: dict[str, Any],
    deps: Any,
) -> CompensationResult:
    """Restore the review fields ``Brain.review`` overwrote.

    Uses the Brain's public ``db`` / ``agent_id``. Stale guard: the update
    applies only while the decision still carries EVERY review field this
    call wrote (``written``, recorded in the call's own transaction), so a
    later re-review -- even one keeping the same outcome with a new note,
    reviewer or timestamp -- is never silently undone. A snapshot without
    the written state cannot make that distinction and is refused.
    """
    from nous.storage.models import Decision

    decision_id = snapshot_data.get("decision_id")
    prior = snapshot_data.get("prior")
    written = snapshot_data.get("written")
    if not decision_id:
        return CompensationResult(False, "no decision_id in snapshot")
    if not isinstance(prior, dict):
        return CompensationResult(False, "prior state of the decision was not recorded; not reverting")
    if not isinstance(written, dict):
        return CompensationResult(
            False, "revert refused: the state this call wrote was not recorded, so a later review cannot be ruled out"
        )
    brain = getattr(deps, "brain", None)
    if brain is None:
        return CompensationResult(False, "brain not available")

    def _ts(value: Any) -> datetime | None:
        return datetime.fromisoformat(value) if value else None

    def _uuid(value: Any) -> UUID | None:
        return UUID(value) if value else None

    try:
        stmt = (
            update(Decision)
            .where(Decision.id == UUID(decision_id))
            .where(Decision.agent_id == brain.agent_id)
            .where(Decision.outcome.is_not_distinct_from(written.get("outcome")))
            .where(Decision.outcome_result.is_not_distinct_from(written.get("outcome_result")))
            .where(Decision.reviewed_at.is_not_distinct_from(_ts(written.get("reviewed_at"))))
            .where(Decision.reviewer.is_not_distinct_from(written.get("reviewer")))
            .where(Decision.superseded_by.is_not_distinct_from(_uuid(written.get("superseded_by"))))
            .values(
                outcome=prior.get("outcome"),
                outcome_result=prior.get("outcome_result"),
                reviewed_at=_ts(prior.get("reviewed_at")),
                reviewer=prior.get("reviewer"),
                superseded_by=_uuid(prior.get("superseded_by")),
            )
        )
        async with brain.db.session() as s:
            result = await s.execute(stmt)
            await s.commit()
        if (result.rowcount or 0) > 0:
            return CompensationResult(True, f"restored decision {decision_id} to outcome={prior.get('outcome')!r}")
        # Idempotent: the decision already carries EVERY prior review field
        # (the microsecond reviewed_at included) -- an earlier revert whose
        # completion was not recorded. Report it done.
        async with brain.db.session() as s:
            row = (
                await s.execute(
                    select(
                        Decision.outcome,
                        Decision.outcome_result,
                        Decision.reviewed_at,
                        Decision.reviewer,
                        Decision.superseded_by,
                    )
                    .where(Decision.id == UUID(decision_id))
                    .where(Decision.agent_id == brain.agent_id)
                )
            ).first()
        if row is not None and tuple(row) == (
            prior.get("outcome"),
            prior.get("outcome_result"),
            _ts(prior.get("reviewed_at")),
            prior.get("reviewer"),
            _uuid(prior.get("superseded_by")),
        ):
            return CompensationResult(True, f"decision {decision_id} already carries its prior review state")
        return CompensationResult(
            False,
            f"revert refused: decision {decision_id} not found or reviewed again since the original call",
        )
    except Exception as exc:
        return CompensationResult(False, f"revert failed: {exc}")


def register_compensators(registry: CompensationRegistry) -> None:
    """Register all built-in compensators."""
    registry.register("write_file", compensate_write_file)
    registry.register("heartbeat_check_manage", compensate_heartbeat_check_manage)
    registry.register("resolve_decision", compensate_resolve_decision)
