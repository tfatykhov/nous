"""Undo journal: passive before-state snapshots and owner-run restore.

Records the state an artifact had BEFORE Nous changed it, for four kinds:

- ``file``: a workspace file written by ``write_file`` (its prior bytes,
  captured with ``compensation.snapshot_for_write_file``);
- ``schedule``: a ``heart.schedules`` row on create and deactivate;
- ``heartbeat_check``: a ``nous_system.dynamic_checks`` row on create,
  enable, disable, update and delete;
- ``config``: the heartbeat settings ``PUT /heartbeat/config`` changes at runtime.

PASSIVE by design. This is not the Phase 2.8 compensation layer
(``nous/api/compensation.py``): nothing here marks a call "compensable" or
"undoable", gates a dispatch, publishes a card, or acts on its own. A failed
snapshot is logged and the mutation proceeds. Restore runs only when the owner
asks for it (``POST /undo/snapshots/{id}/restore``), and it snapshots the
current state first, so a restore can itself be restored.

Storage: one JSON file per snapshot under ``<workspace_dir>/.nous-undo``
(``NOUS_UNDO_JOURNAL_DIR``), written to a temp file and renamed. Bounded:
file snapshots keep at most 1 MiB of prior content (a larger file is recorded
as not restorable), and after each write the oldest snapshots are pruned
beyond ``NOUS_UNDO_JOURNAL_MAX_ENTRIES`` / ``NOUS_UNDO_JOURNAL_MAX_BYTES``.
``write_file`` refuses targets inside the journal directory.

Undoing a create never hard-deletes a row: a created schedule is deactivated
and a created check disabled. A file the write created is deleted (its
content is in the pre-restore snapshot). A deactivated schedule whose F099
container intention has been closed is restored inactive, so restore never
re-arms work against a closed intention.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import logging
import os
import threading
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

logger = logging.getLogger(__name__)

KIND_FILE = "file"
KIND_SCHEDULE = "schedule"
KIND_CHECK = "heartbeat_check"
KIND_CONFIG = "config"

SOURCE_MUTATION = "mutation"
SOURCE_PRE_RESTORE = "pre_restore"

# Same cap as the compensation layer's file snapshots (and read_file).
FILE_SNAPSHOT_MAX_BYTES = 1 * 1024 * 1024

# How long a file restore waits for the write_file path lock.
_RESTORE_LOCK_WAIT_SECONDS = 5.0

_SUFFIX = ".json"


class UndoJournal:
    """A bounded directory of before-state snapshots, one JSON file each."""

    def __init__(self, root: str, *, max_entries: int = 500, max_bytes: int = 50 * 1024 * 1024) -> None:
        self._root = Path(root)
        self._max_entries = max_entries
        self._max_bytes = max_bytes
        # Held around every write and its prune (and mark_restored's rewrite).
        # A threading lock: writes run in asyncio.to_thread workers. Unlocked,
        # two writers near a bound each prune protecting only their own file
        # and can delete each other's snapshot.
        self._lock = threading.Lock()

    @property
    def root(self) -> Path:
        return self._root

    def contains(self, path: str | os.PathLike[str]) -> bool:
        """Whether ``path`` resolves inside the journal directory."""
        root = os.path.realpath(self._root)
        target = os.path.realpath(path)
        return target == root or target.startswith(root + os.sep)

    async def record(
        self,
        kind: str,
        action: str,
        target: str,
        before: Any,
        *,
        label: str | None = None,
        restorable: bool = True,
        note: str | None = None,
        source: str = SOURCE_MUTATION,
        restores: str | None = None,
    ) -> str:
        """Persist one snapshot and return its id. Raises on failure: callers
        that must never block use ``record_safe``."""
        snapshot_id = uuid4().hex
        entry = {
            "id": snapshot_id,
            "kind": kind,
            "action": action,
            "target": target,
            "label": label,
            "created_at": datetime.now(UTC).isoformat(),
            "source": source,
            "restorable": restorable,
            "note": note,
            "restores": restores,
            "before": before,
        }
        await asyncio.to_thread(self._write, entry)
        return snapshot_id

    def _write(self, entry: dict[str, Any]) -> None:
        data = _dump(entry)
        if len(data) > self._max_bytes:
            # Pruning could not make room: it would delete every older
            # snapshot and still leave the journal over its bound.
            raise ValueError(f"snapshot of {len(data)} bytes exceeds the journal's {self._max_bytes}-byte bound")
        self._root.mkdir(mode=0o700, parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
        name = f"{stamp}-{entry['id']}{_SUFFIX}"
        with self._lock:
            self._write_file(name, entry, data)
            self._prune(keep=name)

    def _write_file(self, name: str, entry: dict[str, Any], data: bytes | None = None) -> None:
        data = _dump(entry) if data is None else data
        tmp = self._root / f".{name}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self._root / name)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise

    def _entries(self) -> list[Path]:
        """Snapshot files, oldest first (names start with a UTC timestamp)."""
        if not self._root.is_dir():
            return []
        return sorted(p for p in self._root.iterdir() if p.name.endswith(_SUFFIX) and not p.name.startswith("."))

    def _prune(self, *, keep: str) -> None:
        entries = self._entries()
        sizes = {p: p.stat().st_size for p in entries}
        total = sum(sizes.values())
        count = len(entries)
        for path in entries:
            if count <= self._max_entries and total <= self._max_bytes:
                break
            if path.name == keep:
                continue
            path.unlink(missing_ok=True)
            count -= 1
            total -= sizes[path]

    def _find(self, snapshot_id: str) -> Path | None:
        if not snapshot_id or not all(c in "0123456789abcdef" for c in snapshot_id):
            return None
        matches = list(self._root.glob(f"*-{snapshot_id}{_SUFFIX}")) if self._root.is_dir() else []
        return matches[0] if matches else None

    async def get(self, snapshot_id: str) -> dict[str, Any] | None:
        def _read() -> dict[str, Any] | None:
            path = self._find(snapshot_id)
            return json.loads(path.read_bytes()) if path is not None else None

        return await asyncio.to_thread(_read)

    async def list(self, limit: int = 50) -> list[dict[str, Any]]:
        """The newest ``limit`` snapshots, newest first, without their payloads."""

        def _read() -> list[dict[str, Any]]:
            out = []
            for path in reversed(self._entries()[-limit:] if limit > 0 else []):
                try:
                    entry = json.loads(path.read_bytes())
                except (OSError, ValueError):
                    continue
                entry.pop("before", None)
                out.append(entry)
            return out

        return await asyncio.to_thread(_read)

    async def mark_restored(self, snapshot_id: str, message: str) -> None:
        """Stamp a snapshot with the restore that used it."""

        def _mark() -> None:
            # Locked: a concurrent prune must not delete the file between the
            # read and the rewrite (the rewrite would bring it back).
            with self._lock:
                path = self._find(snapshot_id)
                if path is None:
                    return
                entry = json.loads(path.read_bytes())
                entry["restored_at"] = datetime.now(UTC).isoformat()
                entry["restore_result"] = message
                self._write_file(path.name, entry)

        await asyncio.to_thread(_mark)


def _dump(entry: dict[str, Any]) -> bytes:
    return json.dumps(entry, separators=(",", ":")).encode("utf-8")


_journal: UndoJournal | None = None


def configure(settings: Any) -> UndoJournal | None:
    """Install the process-wide journal from settings (None when disabled)."""
    if getattr(settings, "undo_journal_enabled", False) is not True:  # a mocked Settings counts as off
        set_journal(None)
        return None
    root = settings.undo_journal_dir or os.path.join(settings.workspace_dir, ".nous-undo")
    journal = UndoJournal(
        root,
        max_entries=settings.undo_journal_max_entries,
        max_bytes=settings.undo_journal_max_bytes,
    )
    set_journal(journal)
    return journal


def set_journal(journal: UndoJournal | None) -> None:
    global _journal
    _journal = journal


def get_journal() -> UndoJournal | None:
    return _journal


async def record_safe(kind: str, action: str, target: str, before: Any, **kwargs: Any) -> str | None:
    """Record a snapshot; never raises. None when no journal is installed or
    the write failed (logged): the mutation it precedes goes ahead regardless."""
    journal = _journal
    if journal is None:
        return None
    try:
        return await journal.record(kind, action, target, before, **kwargs)
    except Exception:
        logger.warning("Undo journal: %s snapshot of %s failed; the %s proceeds", kind, target, action, exc_info=True)
        return None


async def record_model_safe(kind: str, action: str, model: Any, *, label: str | None = None) -> str | None:
    """``record_safe`` for an ORM row as it is now; never raises."""
    if _journal is None:
        return None
    try:
        target, before = str(model.id), encode_model(model)
    except Exception:
        logger.warning("Undo journal: %s snapshot could not be encoded; the %s proceeds", kind, action, exc_info=True)
        return None
    return await record_safe(kind, action, target, before, label=label)


async def record_file_write(path: str, workspace_dir: str) -> str | None:
    """Snapshot a file ``write_file`` is about to overwrite; never raises."""
    journal = _journal
    if journal is None:
        return None
    try:
        from nous.api.compensation import snapshot_for_write_file

        snap = await snapshot_for_write_file(path, workspace_dir, max_bytes=FILE_SNAPSHOT_MAX_BYTES)
        if snap.get("invalid_path"):
            return None  # write_file refuses this path itself
        note = None
        if snap.get("oversized"):
            note = f"prior content exceeds {FILE_SNAPSHOT_MAX_BYTES} bytes; not captured"
        elif snap.get("capture_error"):
            note = f"prior content could not be read: {snap['capture_error']}"
        return await journal.record(
            KIND_FILE, "write_file", snap["full_path"], snap, label=path, restorable=note is None, note=note
        )
    except Exception:
        logger.warning("Undo journal: file snapshot of %r failed; the write proceeds", path, exc_info=True)
        return None


# ---------------------------------------------------------------------------
# Row codec: database rows as JSON, round-tripped to the same Python values
# ---------------------------------------------------------------------------


def _encode(value: Any) -> Any:
    if isinstance(value, datetime):
        return {"$datetime": value.isoformat()}
    if isinstance(value, date):
        return {"$date": value.isoformat()}
    if isinstance(value, UUID):
        return {"$uuid": str(value)}
    if isinstance(value, Decimal):
        return {"$decimal": str(value)}
    return value


def _decode(value: Any) -> Any:
    if isinstance(value, dict) and len(value) == 1:
        ((tag, raw),) = value.items()
        if tag == "$datetime":
            return datetime.fromisoformat(raw)
        if tag == "$date":
            return date.fromisoformat(raw)
        if tag == "$uuid":
            return UUID(raw)
        if tag == "$decimal":
            return Decimal(raw)
    return value


def encode_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """A row mapping (column name -> value) as JSON-safe data."""
    return {name: _encode(value) for name, value in row.items()}


def decode_row(data: Mapping[str, Any]) -> dict[str, Any]:
    """The inverse of ``encode_row``."""
    return {name: _decode(value) for name, value in data.items()}


def encode_model(model: Any) -> dict[str, Any]:
    """An ORM instance's columns, keyed by column name, as JSON-safe data."""
    from sqlalchemy import inspect as sa_inspect

    mapper = sa_inspect(type(model))
    return encode_row(
        {col.name: getattr(model, mapper.get_property_by_column(col).key) for col in model.__table__.columns}
    )


# ---------------------------------------------------------------------------
# Restore
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RestoreResult:
    success: bool
    message: str
    pre_restore_id: str | None = None


@dataclass
class RestoreDeps:
    """What a restore of each kind needs; a missing one fails that kind."""

    workspace_dir: str | None = None  # file restores must land inside it
    schedules: Any = None  # heart.schedules (ScheduleManager)
    check_loader: Any = None  # DynamicCheckLoader
    read_config: Callable[[list[str]], dict[str, Any]] | None = None
    apply_config: Callable[[dict[str, Any]], list[str]] | None = None
    config_lock: asyncio.Lock | None = None  # the lock config writers hold around read + record + apply


async def restore(snapshot_id: str, deps: RestoreDeps, journal: UndoJournal | None = None) -> RestoreResult:
    """Put an artifact back into the state a snapshot recorded.

    The current state is snapshotted first (``source="pre_restore"``); when
    that fails, nothing is changed. Owner-initiated only: nothing in Nous
    calls this on its own.
    """
    journal = journal or _journal
    if journal is None:
        return RestoreResult(False, "undo journal is not enabled")
    entry = await journal.get(snapshot_id)
    if entry is None:
        return RestoreResult(False, f"snapshot {snapshot_id!r} not found")
    if not entry.get("restorable"):
        return RestoreResult(False, f"snapshot {snapshot_id} is not restorable: {entry.get('note')}")

    pre_ids: list[str] = []

    async def record_current(before: Any) -> None:
        # Strict: a restore whose current state was not saved does not run.
        pre_ids.append(
            await journal.record(
                entry["kind"],
                "pre_restore",
                entry["target"],
                before,
                label=entry.get("label"),
                source=SOURCE_PRE_RESTORE,
                restores=snapshot_id,
            )
        )

    kind = entry["kind"]
    try:
        if kind == KIND_FILE:
            result = await _restore_file(entry["before"], deps.workspace_dir, record_current)
        elif kind == KIND_SCHEDULE:
            result = await _restore_row(deps.schedules, "schedules", entry, record_current)
        elif kind == KIND_CHECK:
            result = await _restore_row(deps.check_loader, "dynamic checks", entry, record_current)
        elif kind == KIND_CONFIG:
            result = await _restore_config(entry["before"], deps, record_current)
        else:
            result = RestoreResult(False, f"unknown snapshot kind {kind!r}")
    except Exception as exc:
        logger.warning("Undo journal: restore of %s failed", snapshot_id, exc_info=True)
        result = RestoreResult(False, f"restore failed: {type(exc).__name__}: {exc}")
    result = RestoreResult(result.success, result.message, pre_ids[0] if pre_ids else None)
    if result.success:
        try:
            await journal.mark_restored(snapshot_id, result.message)
        except Exception:
            logger.warning("Undo journal: could not mark %s restored", snapshot_id, exc_info=True)
    return result


async def _restore_row(
    store: Any,
    what: str,
    entry: dict[str, Any],
    record_current: Callable[[Any], Awaitable[None]],
) -> RestoreResult:
    if store is None:
        return RestoreResult(False, f"{what} are not available")
    before = entry["before"]

    async def on_current(row: Mapping[str, Any] | None) -> None:
        await record_current(encode_row(row) if row is not None else None)

    ok, message = await store.restore_row(
        UUID(entry["target"]), decode_row(before) if before is not None else None, on_current=on_current
    )
    return RestoreResult(ok, message)


async def _restore_config(
    before: dict[str, Any],
    deps: RestoreDeps,
    record_current: Callable[[Any], Awaitable[None]],
) -> RestoreResult:
    if deps.read_config is None or deps.apply_config is None:
        return RestoreResult(False, "runtime config is not available")
    async with deps.config_lock or contextlib.nullcontext():
        await record_current(deps.read_config(list(before)))
        applied = deps.apply_config(before)
    return RestoreResult(True, f"restored config fields: {', '.join(sorted(applied)) or 'none'}")


async def _restore_file(
    before: dict[str, Any],
    workspace_dir: str | None,
    record_current: Callable[[Any], Awaitable[None]],
) -> RestoreResult:
    """Write the prior bytes back (or remove a file the write created), only
    while the file still holds what the pre-restore snapshot captured. The
    target must lie inside the CONFIGURED workspace: the journal lives in
    the workspace, so the root a snapshot records is not trusted."""
    from nous.api.builtin_tools import ABSENT, PreconditionFailed, atomic_replace_bytes, remove_if_matches
    from nous.api.compensation import release_write_path_lock, snapshot_for_write_file, write_path_lock

    if not workspace_dir:
        return RestoreResult(False, "the workspace is not available")
    full_path = before["full_path"]
    root = str(Path(workspace_dir).resolve())
    prior: bytes | None = None
    if before["existed"]:
        prior = base64.b64decode(before["prior_b64"])
        if hashlib.sha256(prior).hexdigest() != before["prior_sha256"]:
            return RestoreResult(False, "snapshot content does not match its recorded sha256; not restoring")

    lock = write_path_lock(full_path, "")
    try:
        await asyncio.wait_for(lock.acquire(), timeout=_RESTORE_LOCK_WAIT_SECONDS)
    except TimeoutError:
        return RestoreResult(False, f"a write to {full_path!r} is in flight; nothing was changed, retry")
    try:
        # The same per-file cap as write_file's snapshots: a larger current
        # file cannot be saved first, so the restore does not run.
        current = await snapshot_for_write_file(full_path, root, max_bytes=FILE_SNAPSHOT_MAX_BYTES)
        if current.get("invalid_path") or current.get("capture_error") or current.get("oversized"):
            reason = current.get("invalid_path") or current.get("capture_error") or "it is too large to snapshot"
            return RestoreResult(
                False, f"current state of {full_path!r} could not be saved first ({reason}); nothing changed"
            )
        expected = current["prior_sha256"] if current["existed"] else ABSENT
        if prior is not None and expected == before["prior_sha256"]:
            return RestoreResult(True, f"{full_path} already holds its snapshotted content")
        if prior is None and expected == ABSENT:
            return RestoreResult(True, f"{full_path} is already absent")
        await record_current(current)
        limit = max(len(prior or b""), len(base64.b64decode(current["prior_b64"] or "")))

        def _apply() -> None:
            if prior is not None:
                atomic_replace_bytes(Path(full_path), prior, expected=expected, limit=limit, root=Path(root))
            else:
                remove_if_matches(Path(full_path), expected, limit=limit, root=Path(root))

        try:
            await asyncio.to_thread(_apply)
        except PreconditionFailed:
            return RestoreResult(False, f"{full_path!r} changed while restoring; nothing was changed, retry")
        if prior is None:
            return RestoreResult(True, f"removed {full_path} (the write created it)")
        return RestoreResult(True, f"restored prior content of {full_path}")
    finally:
        release_write_path_lock(lock)
