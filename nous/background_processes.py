"""Goals PR 0c: the shell commands background work started, so a halt can signal them.

Each tracked command was started with ``start_new_session=True``, so it leads a process group of its own whose id is
its pid. ``kill_all`` only signals: one ``SIGKILL`` per group, each in its own ``try``. It never reads a command's
pipes (the tool call that started the command is still waiting on them in ``communicate()``, and collects the exit
itself). A command is kept after its shell exits, until its group is empty: a child it detached (``nohup ... &``)
stays in the group. The kernel does not give the group's number to a new process while a member lives; once the group
is empty an entry is dropped, and a new command with the same pid replaces a stale entry.

Before a group is signalled it is checked to still be the command's (Linux ``/proc``): the leader's start time is
recorded at ``track``; a live process with the group's number must have that start time, and when the leader has
ended, the group must still hold a member recorded at ``track`` (same start time) or one that started before the
leader's exit was seen, that tick included: a start time and that moment are both whole clock ticks, so a child the
command started in its last moment can carry the very tick at which the exit was seen, and leaving it out would leave
that child running. A leader already gone at ``track`` (a fast command whose shell exited and was reaped first) is
identified by the members its group has then; with none, nothing is tracked. Anything else (another start time,
nothing readable) is dropped, never signalled. A zombie is not a member: it has ended, and with no init process in
the container nothing may ever collect it. On a POSIX host without ``/proc`` nothing is tracked: no start time can be
read, so no group could be shown to be the command's.
"""

from __future__ import annotations

import logging
import os
import signal
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

_EXITED_KEEP_SECONDS = 3600.0
_CLK_TCK = os.sysconf("SC_CLK_TCK") if "SC_CLK_TCK" in getattr(os, "sysconf_names", {}) else 100


@dataclass
class _Entry:
    pgid: int
    proc: Any
    exited_at: float | None = None
    start: int | None = None  # the leader's start time, clock ticks since boot (/proc/<pid>/stat field 22)
    exited_ticks: int | None = None  # when the leader's exit was seen, on the same clock
    members: dict[int, int] = field(default_factory=dict)  # pid -> start time, when the leader was gone at track


def _stat(pid: int) -> tuple[str, int, int] | None:
    """``(state, process group, start time)`` of a process from ``/proc``; None when it cannot be read."""
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as f:
            fields = f.read().rsplit(") ", 1)[1].split()
        return fields[0], int(fields[2]), int(fields[19])
    except (OSError, IndexError, ValueError):
        return None


def _groups() -> dict[int, dict[int, int]] | None:
    """Every process that has not ended (a zombie has), by process group: pgid -> {pid: start time}. One scan of
    ``/proc``; None when it cannot be read."""
    try:
        names = os.listdir("/proc")
    except OSError:
        return None
    groups: dict[int, dict[int, int]] = {}
    for name in names:
        if name.isdigit():
            stat = _stat(int(name))
            if stat is not None and stat[0] != "Z":
                groups.setdefault(stat[1], {})[int(name)] = stat[2]
    return groups


def _members(pgid: int) -> dict[int, int]:
    """The members of process group ``pgid`` that have not ended, as pid -> start time. Empty when ``/proc`` cannot
    be read."""
    return (_groups() or {}).get(pgid, {})


def _now_ticks() -> int | None:
    try:
        with open("/proc/uptime", encoding="utf-8") as f:
            # round, not int: 4.35 * 100 is 434.99999999999994 in floating point, a tick early
            return round(float(f.read().split()[0]) * _CLK_TCK)
    except (OSError, IndexError, ValueError):
        return None


def _still_ours(entry: _Entry) -> bool:
    """The group with this number is still the command's (see the module docstring)."""
    if entry.start is not None:
        leader = _stat(entry.pgid)
        if leader is not None:
            return leader[2] == entry.start
    members = _members(entry.pgid)
    if any(entry.members.get(pid) == started for pid, started in members.items()):
        return True
    return entry.exited_ticks is not None and any(started <= entry.exited_ticks for started in members.values())


_entries: dict[int, _Entry] = {}


def _group_alive(pgid: int, groups: dict[int, dict[int, int]] | None) -> bool:
    """Whether the group still has a member that has not ended, from ``groups`` (one scan of ``/proc``). Without
    ``/proc``, whether a signal reaches it."""
    if not hasattr(os, "killpg"):
        return False
    if groups is not None:
        return bool(groups.get(pgid))
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _prune() -> None:
    """Drop the entries whose shell has exited and whose group is empty (or that were kept too long). One scan of
    ``/proc`` for all of them."""
    exited = [pgid for pgid, entry in _entries.items() if entry.exited_at is not None]
    if not exited:
        return
    now = time.monotonic()
    groups = _groups() if hasattr(os, "killpg") and os.path.isdir("/proc/self") else None
    for pgid in exited:
        entry = _entries[pgid]
        if now - entry.exited_at > _EXITED_KEEP_SECONDS or not _group_alive(pgid, groups):
            _entries.pop(pgid, None)


def track(proc: Any) -> int | None:
    """Track a command just started in its own session. Returns its group id (its pid), or None when it is not
    tracked (no pid, or on a POSIX host its group cannot be read from ``/proc``)."""
    pid = getattr(proc, "pid", None)
    if not isinstance(pid, int):
        return None
    _prune()
    if not hasattr(os, "killpg"):  # no process groups: the shell only, through its handle
        _entries[pid] = _Entry(pid, proc)
        return pid
    found = _stat(pid)
    if found is not None:
        _entries[pid] = _Entry(pid, proc, start=found[2])  # a stale entry with this number is replaced
        return pid
    # The shell has already ended: the group is known by the members it has now.
    members = _members(pid)
    if not members:
        _entries.pop(pid, None)
        return None
    _entries[pid] = _Entry(pid, proc, exited_at=time.monotonic(), exited_ticks=_now_ticks(), members=members)
    return pid


def release(pgid: int | None) -> None:
    """The command's shell has exited (its call returned, timed out or was cancelled). The entry stays while its
    process group has members, and is dropped once it is empty."""
    if pgid is None:
        return
    entry = _entries.get(pgid)
    if entry is not None and entry.exited_at is None:
        entry.exited_at = time.monotonic()
        entry.exited_ticks = _now_ticks()
    _prune()


def tracked() -> list[int]:
    """The group ids tracked now (tests and the sweep's report)."""
    _prune()
    return sorted(_entries)


def kill_all() -> int:
    """Signal every tracked command's process group with SIGKILL. Returns how many groups were signalled. Never
    raises and never waits."""
    _prune()
    signalled = 0
    for pgid, entry in list(_entries.items()):
        try:
            if hasattr(os, "killpg"):
                if not _still_ours(entry):
                    _entries.pop(pgid, None)
                    continue
                os.killpg(pgid, signal.SIGKILL)
            elif getattr(entry.proc, "returncode", 0) is None:
                entry.proc.kill()  # no process groups on Windows: the shell only
            else:
                continue
            signalled += 1
        except (ProcessLookupError, PermissionError):
            _entries.pop(pgid, None)
        except Exception:
            logger.warning("Background halt: could not signal process group %d", pgid, exc_info=True)
    return signalled


def _reset() -> None:
    """Forget every entry (tests)."""
    _entries.clear()
