"""Goals PR 0c: the shell commands background work started, so a halt can signal them.

Each tracked command was started with ``start_new_session=True``, so it leads a process group of its own whose id is
its pid. ``kill_all`` only signals: one ``SIGKILL`` per group, each in its own ``try``. It never reads a command's
pipes (the tool call that started the command is still waiting on them in ``communicate()``, and collects the exit
itself). A command is kept after its shell exits, until its group is empty: a child it detached (``nohup ... &``)
stays in the group. The kernel does not give the group's number to a new process while a member lives; once the group
is empty an entry is dropped, and a new command with the same pid replaces a stale entry.

Before a group is signalled it is checked to still be the command's (Linux ``/proc``): the leader's start time is
recorded at ``track``; a live process with the group's number must have that start time, and when the leader has
ended, a member of the group must have started before the leader's exit was seen. Anything else (another start time,
nothing readable) is dropped, never signalled.
"""

from __future__ import annotations

import logging
import os
import signal
import time
from dataclasses import dataclass
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


def _stat(pid: int) -> tuple[int, int] | None:
    """``(process group, start time)`` of a live process from ``/proc``; None when it cannot be read."""
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as f:
            fields = f.read().rsplit(") ", 1)[1].split()
        return int(fields[2]), int(fields[19])
    except (OSError, IndexError, ValueError):
        return None


def _now_ticks() -> int | None:
    try:
        with open("/proc/uptime", encoding="utf-8") as f:
            return int(float(f.read().split()[0]) * _CLK_TCK)
    except (OSError, IndexError, ValueError):
        return None


def _member_started_by(pgid: int, ticks: int) -> bool:
    try:
        names = os.listdir("/proc")
    except OSError:
        return False
    for name in names:
        if name.isdigit():
            found = _stat(int(name))
            if found is not None and found[0] == pgid and found[1] <= ticks:
                return True
    return False


def _still_ours(entry: _Entry) -> bool:
    """The group with this number is still the command's (see the module docstring)."""
    if entry.start is None:
        return False
    leader = _stat(entry.pgid)
    if leader is not None:
        return leader[1] == entry.start
    return entry.exited_ticks is not None and _member_started_by(entry.pgid, entry.exited_ticks)


_entries: dict[int, _Entry] = {}


def _group_alive(pgid: int) -> bool:
    if not hasattr(os, "killpg"):
        return False
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _prune() -> None:
    now = time.monotonic()
    for pgid, entry in list(_entries.items()):
        if entry.exited_at is None:
            continue
        if now - entry.exited_at > _EXITED_KEEP_SECONDS or not _group_alive(pgid):
            _entries.pop(pgid, None)


def track(proc: Any) -> int | None:
    """Track a command just started in its own session. Returns its group id (its pid), or None without one."""
    pid = getattr(proc, "pid", None)
    if not isinstance(pid, int):
        return None
    _prune()
    found = _stat(pid)
    _entries[pid] = _Entry(pid, proc, start=found[1] if found else None)  # a stale entry with this number is replaced
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
