"""Goals PR 0c: the shell commands background work started, so a halt can signal them.

Each tracked command was started with ``start_new_session=True``, so it leads a process group of its own whose id is
its pid. ``kill_all`` only signals: one ``SIGKILL`` per group, each in its own ``try``. It never reads a command's
pipes (the tool call that started the command is still waiting on them in ``communicate()``, and collects the exit
itself). A command is kept after its shell exits, until its group is empty: a child it detached (``nohup ... &``)
stays in the group. The kernel does not give the group's number to a new process while a member lives; once the group
is empty an entry is dropped, and a new command with the same pid replaces a stale entry. A process that left its
group (``setsid``) is out of reach.
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


@dataclass
class _Entry:
    pgid: int
    proc: Any
    exited_at: float | None = None


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
    _entries[pid] = _Entry(pid, proc)  # a stale entry with this number is replaced
    return pid


def release(pgid: int | None) -> None:
    """The command's shell has exited (its call returned, timed out or was cancelled). The entry stays while its
    process group has members, and is dropped once it is empty."""
    if pgid is None:
        return
    entry = _entries.get(pgid)
    if entry is not None and entry.exited_at is None:
        entry.exited_at = time.monotonic()
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
