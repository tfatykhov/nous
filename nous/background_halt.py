"""Goals PR 0c: the halt switch for background work.

One process-wide ``BackgroundHalt`` holds an in-memory flag, an epoch and a transition sequence. Every place that
claims or starts background work reads the flag; every background turn captures the epoch when it starts, and the
runner refuses the tool calls and ends the model loop of a background turn that is halted or older than the current
epoch (``refuses``). So a turn that was running when a halt landed never acts again, even after an immediate resume.

The flag is set in memory first and then stored (``nous_system.background_halt``, with an audit row in the same
transaction); a failed write is retried in the background and ``persisted`` says so. A resume writes first and clears
the flag only when its write landed and no halt arrived since it was called. ``NOUS_BACKGROUND_HALT`` is read once, at
construction: set, the process is halted whatever the stored row says and a resume is refused.

Chat with the owner is never refused (``interactive`` and ``mcp`` turns outside a background session), and neither is
an ``approved_action`` call (one call the owner approved, with no model).
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from nous.api.execution_context import FOREGROUND_KINDS, is_background_session

logger = logging.getLogger(__name__)

# The epoch a background turn captured when it started. The model loops put it here around the synchronous
# ``_authorize_tool_call``; the worker, the heartbeat and the continuation runner set it around the work they claimed,
# so a turn inside that work keeps the earlier (outer) value. None: not captured, and only the flag counts.
TURN_EPOCH: ContextVar[int | None] = ContextVar("background_halt_turn_epoch", default=None)

# What a refused tool call of a background turn returns (ledger refusal code ``background_halt``).
HALTED_TOOL_TEXT = (
    "Tool error: halted by the owner. Background work is stopped; do nothing further and end your answer."
)
# What spawn_task, spawn_sync, dag_create and dag_manage retry_node return from a chat turn while halted.
HALTED_SPAWN_TEXT = (
    "Tool error: background work is halted by the owner, so nothing can be started. "
    "Tell the owner it will not run until they resume it."
)
# The final text of a background turn the halt ended. Every consumer discards it by an explicit branch.
HALT_TURN_TEXT = "Halted by the owner."
# The reason a refused creation and a halted hardened run carry.
HALT_REASON = "background work is halted by the owner; resume it to run this"
# Under every live chat reply while halted; never stored in the conversation.
HALT_LINE = "Background work is halted. Nothing runs on its own until you resume it."
HALT_LINE_HOST = (
    "Background work is halted. Nothing runs on its own until you resume it; it is held by the host setting."
)
# The system prompt note of every turn while halted (the dynamic tier).
PROMPT_NOTE = (
    "[Background work]\n"
    "Background work is halted by the owner. Do not promise to start tasks, schedules or checks that run now; they "
    "will not run until the owner resumes. Chat, reading and answering still work."
)
REFUSAL_CODE = "background_halt"

SURFACES = ("dashboard", "telegram", "agent", "host")
# A halt from these surfaces has nobody waiting on its answer, so its report goes to the owner's chat (unless the
# caller passes announce=False).
ANNOUNCED_SURFACES = frozenset({"agent", "host"})
_ACTOR_MAX = 100
_VIA_MAX = 40
_REASON_MAX = 200
_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")
_NEVER_REFUSED = frozenset({"approved_action"})


def clean(value: object, limit: int) -> str | None:
    """A short caller text as it is stored and shown: control characters become spaces, runs of spaces one space,
    cut to ``limit``. None for anything that is not a non-blank string."""
    if not isinstance(value, str):
        return None
    text = " ".join(_CONTROL.sub(" ", value).split())
    return text[:limit] or None


def clean_surface(value: object) -> str:
    return value if isinstance(value, str) and value in SURFACES else "unknown"


@dataclass(frozen=True)
class HaltRow:
    """The stored state of one agent's halt (``nous_system.background_halt``)."""

    halted: bool
    halted_at: datetime | None = None
    halted_by: str | None = None
    halted_via: str | None = None
    halted_verified: bool = False
    reason: str | None = None
    resumed_at: datetime | None = None
    resumed_by: str | None = None


@dataclass(frozen=True)
class AuditEntry:
    """One ``nous_system.background_halt_log`` row."""

    action: Literal["halt", "resume", "resume_refused", "sweep"]
    actor: str | None = None
    via: str | None = None
    verified: bool = False
    reason: str | None = None
    detail: dict[str, Any] | None = None


class HaltStore(Protocol):
    async def read(self) -> HaltRow | None: ...

    async def write(self, row: HaltRow, audit: AuditEntry) -> None: ...

    async def audit(self, entry: AuditEntry) -> None: ...


@dataclass(frozen=True)
class HaltOutcome:
    """What one ``halt()`` call did. ``persisted``: whether its first write landed (a failed one is retried).
    ``announce``: whether the caller sends the halt's report to the owner's chat."""

    already: bool
    persisted: bool
    announce: bool = False
    epoch: int = 0  # the epoch this halt set: its sweep runs while the halt on is still this one


@dataclass(frozen=True)
class ResumeOutcome:
    status: Literal["resumed", "not_halted", "host_hold", "raced", "failed"]

    @property
    def changed(self) -> bool:
        return self.status == "resumed"


@dataclass
class _Shown:
    since: datetime | None = None
    by: str | None = None
    surface: str | None = None
    verified: bool = False
    reason: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


class BackgroundHalt:
    """The halt state of this process. See the module docstring."""

    def __init__(
        self,
        settings: Any = None,
        *,
        store: HaltStore | None = None,
        host_hold: bool | None = None,
        retry_seconds: float = 5.0,
    ) -> None:
        self._host_hold = bool(getattr(settings, "background_halt", False) is True) if host_hold is None else host_hold
        self._store = store
        self._stored = False
        self._unreadable = False
        self._epoch = 0
        self._seq = 0
        self._shown = _Shown()
        self._persisted = True
        self._lock = asyncio.Lock()
        self._retry_seconds = retry_seconds
        self._retry_task: asyncio.Task | None = None
        self._resume_sweep: Callable[[], Awaitable[Any]] | None = None

    # -- reading ------------------------------------------------------------------------------------------------

    @property
    def halted(self) -> bool:
        return self._host_hold or self._stored

    @property
    def host_hold(self) -> bool:
        return self._host_hold

    @property
    def epoch(self) -> int:
        return self._epoch

    @property
    def persisted(self) -> bool:
        return self._persisted

    @property
    def source(self) -> str:
        if self._host_hold:
            return "host"
        if self._stored:
            return "unreadable" if self._unreadable else "stored"
        return "none"

    def refuses(self, kind: str, session_id: str | None, epoch: int | None) -> bool:
        """Whether a turn of ``kind`` in ``session_id`` that captured ``epoch`` at its start may not act: it is
        background work, and the halt is on or the turn started before the current epoch. ``approved_action`` is
        never refused; ``interactive`` and ``mcp`` only in a background session (a job reporting over chat)."""
        if kind in _NEVER_REFUSED:
            return False
        if kind in FOREGROUND_KINDS and not is_background_session(session_id):
            return False
        if self._host_hold or self._stored:
            return True
        return epoch is not None and epoch < self._epoch

    def snapshot(self, *, full: bool = True) -> dict[str, Any]:
        """What ``GET /background/state`` answers: fixed vocabulary plus the cleaned, cut texts. Not ``full`` (a caller
        with no valid secret): ``by`` is the surface kind only (``dashboard``, ``telegram``, ``agent``, ``host``,
        ``unknown``) and there is no ``reason``."""
        shown = self._shown
        by = shown.by if full else (shown.surface or ("host" if self._host_hold else None))
        return {
            "halted": self.halted,
            "source": self.source,
            "since": shown.since.isoformat() if shown.since else None,
            "by": by,
            "surface": shown.surface,
            "verified": shown.verified,
            "reason": shown.reason if full else None,
            "persisted": self._persisted,
        }

    def set_resume_sweep(self, sweep: Callable[[], Awaitable[Any]] | None) -> None:
        """What a resume runs, under the transition lock, before it writes: the cancel of any subtask or DAG left or
        created since the halt's sweep, so nothing of it starts at the resume. Best effort. It runs under the
        transition lock, so it must not call ``halt`` or ``resume`` itself."""
        self._resume_sweep = sweep

    # -- start ------------------------------------------------------------------------------------------------

    async def load(self) -> None:
        """Read the stored state once, at start. No row: not halted. A read that fails: halted (``unreadable``)
        until a resume. The host hold is kept either way."""
        if self._store is None:
            return
        try:
            row = await self._store.read()
        except Exception:
            logger.error("Background halt: the stored state could not be read; starting halted", exc_info=True)
            self._stored = True
            self._unreadable = True
            return
        if row is not None and row.halted:
            self._stored = True
            self._shown = _Shown(
                since=row.halted_at,
                by=row.halted_by,
                surface=row.halted_via,
                verified=row.halted_verified,
                reason=row.reason,
            )

    # -- transitions ------------------------------------------------------------------------------------------

    async def halt(
        self,
        *,
        actor: str | None,
        surface: str,
        reason: str | None = None,
        verified: bool = False,
        announce: bool = True,
    ) -> HaltOutcome:
        """Halt: the flag and the epoch change at once, before any await; then the state and an audit row are
        written in one transaction. A failed write leaves the flag on and is retried every ``retry_seconds`` until
        it lands or a newer transition supersedes it; ``persisted`` (here and on the outcome) says at once whether
        the first write landed.

        A repeat while halted writes one more audit row with its own actor and reason, and keeps the first halt's
        ``since``, ``by`` and ``reason`` in the state. ``announce=False``: recorded and enforced the same, and the
        outcome tells the caller to send no owner message."""
        already = self.halted
        self._seq += 1
        seq = self._seq
        self._epoch += 1
        epoch = self._epoch
        actor_text, surface_text = clean(actor, _ACTOR_MAX), clean_surface(surface)
        reason_text = clean(reason, _REASON_MAX)
        if not self._stored or self._unreadable:
            self._shown = _Shown(
                since=datetime.now(UTC), by=actor_text, surface=surface_text, verified=verified, reason=reason_text
            )
        self._stored = True
        self._unreadable = False
        self._persisted = False
        audit = AuditEntry("halt", actor_text, surface_text, verified, reason_text)
        landed = await self._write_halt(seq, audit, first=True)
        if not landed and self._seq == seq:
            self._start_retry(seq, audit)
        return HaltOutcome(
            already=already,
            persisted=self._persisted,
            announce=announce and surface_text in ANNOUNCED_SURFACES,
            epoch=epoch,
        )

    def _row(self) -> HaltRow:
        shown = self._shown
        return HaltRow(
            halted=True,
            halted_at=shown.since,
            halted_by=shown.by,
            halted_via=shown.surface,
            halted_verified=shown.verified,
            reason=shown.reason,
        )

    async def _write_halt(self, seq: int, audit: AuditEntry, *, first: bool) -> bool:
        """One attempt to store the halt. The first attempt always writes (it queued at the lock before any later
        transition could); a retry stops when a newer transition has taken over. True when nothing is left to do."""
        if self._store is None:
            if self._seq == seq:
                self._persisted = True
            return True
        async with self._lock:
            if not first and self._seq != seq:
                return True
            try:
                await self._store.write(self._row(), audit)
            except Exception:
                logger.error("Background halt: the halt could not be stored; retrying", exc_info=True)
                return False
            if self._seq == seq:
                self._persisted = True
            return True

    def _start_retry(self, seq: int, audit: AuditEntry) -> None:
        if self._retry_task is not None and not self._retry_task.done():
            self._retry_task.cancel()
        self._retry_task = asyncio.create_task(self._retry(seq, audit), name="background-halt-persist")

    async def _retry(self, seq: int, audit: AuditEntry) -> None:
        while self._seq == seq:
            await asyncio.sleep(self._retry_seconds)
            if await self._write_halt(seq, audit, first=False):
                return

    async def resume(self, *, actor: str | None, via: str, verified: bool = True) -> ResumeOutcome:
        """Resume: refused under the host hold. Otherwise, under the lock, re-run the resume sweep, write
        ``halted=false`` and an audit row, and clear the flag only when the write landed and no halt arrived since
        this call (``raced``: the flag stays on, and the halt's own write lands after this one)."""
        seq0 = self._seq
        actor_text, via_text = clean(actor, _ACTOR_MAX), clean(via, _VIA_MAX)
        if self._host_hold:
            await self.record(AuditEntry("resume_refused", actor_text, via_text, verified, detail={"why": "host_hold"}))
            return ResumeOutcome("host_hold")
        if not self._stored:
            return ResumeOutcome("not_halted")
        async with self._lock:
            if self._seq != seq0:
                return ResumeOutcome("raced")
            if self._resume_sweep is not None:
                try:
                    await self._resume_sweep()
                except Exception:
                    logger.error("Background halt: the resume sweep failed; resuming anyway", exc_info=True)
            if self._seq != seq0:
                return ResumeOutcome("raced")
            shown = self._shown
            row = HaltRow(
                halted=False,
                halted_at=shown.since,
                halted_by=shown.by,
                halted_via=shown.surface,
                halted_verified=shown.verified,
                reason=shown.reason,
                resumed_at=datetime.now(UTC),
                resumed_by=actor_text,
            )
            if self._store is not None:
                try:
                    await self._store.write(row, AuditEntry("resume", actor_text, via_text, verified))
                except Exception:
                    logger.error("Background halt: the resume could not be stored; still halted", exc_info=True)
                    return ResumeOutcome("failed")
            if self._seq != seq0:
                return ResumeOutcome("raced")
            self._stored = False
            self._unreadable = False
            self._seq += 1
            self._persisted = True
            self._shown = _Shown()
            return ResumeOutcome("resumed")

    async def record(self, entry: AuditEntry) -> None:
        """One audit row on its own (a refused resume, a sweep's counts). Never raises."""
        if self._store is None:
            return
        try:
            await self._store.audit(entry)
        except Exception:
            logger.warning("Background halt: could not write a %s audit row", entry.action, exc_info=True)

    async def close(self) -> None:
        """Stop a pending retry (shutdown)."""
        task, self._retry_task = self._retry_task, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass


class _NeverHalted(BackgroundHalt):
    """What ``current()`` returns before ``install``: never halted, and it cannot be."""

    async def halt(self, **_kwargs: Any) -> HaltOutcome:
        raise RuntimeError("no background halt is installed")


_DEFAULT = _NeverHalted(host_hold=False)
_installed: BackgroundHalt | None = None


def install(halt: BackgroundHalt | None) -> None:
    """Make ``halt`` the process's halt (main.py, once, before any loop starts). None restores the default."""
    global _installed
    _installed = halt


def current() -> BackgroundHalt:
    """The installed halt, or a never-halted default."""
    return _installed if _installed is not None else _DEFAULT


def background_halted() -> bool:
    """Whether background work is halted now. The goals driver calls it before every session start."""
    return current().halted


# The tool call running now, as the dispatcher sets it around the handler: (kind, session id, epoch). A handler
# that awaits before its own action (a connection, a browser start, a step of an inner loop) checks ``call_refused``
# right before that action.
CALL_SCOPE: ContextVar[tuple[str, str | None, int | None] | None] = ContextVar(
    "background_halt_call_scope", default=None
)


def call_refused() -> bool:
    """Whether the halt refuses the tool call running now. Outside a tool call: False."""
    scope = CALL_SCOPE.get()
    return scope is not None and current().refuses(*scope)


def turn_epoch() -> int:
    """The epoch a turn starting now captures: the one already in ``TURN_EPOCH`` (set by the work that claimed this
    turn, so the earliest start wins), else the one of the tool call running now (``CALL_SCOPE``: the dispatcher is
    handed it explicitly, which the streaming loop needs), else the current one."""
    outer = TURN_EPOCH.get()
    if outer is None:
        scope = CALL_SCOPE.get()
        outer = scope[2] if scope is not None else None
    return outer if outer is not None else current().epoch


@contextmanager
def epoch_scope(epoch: int | None) -> Iterator[None]:
    """``TURN_EPOCH`` set to ``epoch`` for the block. Use it within one task and never across a ``yield`` of an
    async generator (a later resume of the generator runs in another context, where the reset would raise)."""
    token = TURN_EPOCH.set(epoch)
    try:
        yield
    finally:
        TURN_EPOCH.reset(token)
