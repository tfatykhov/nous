"""Shared utility functions for Nous."""

from __future__ import annotations

import asyncio
import logging
import os
import signal

logger = logging.getLogger(__name__)

# How long kill_process_group waits for the shell it killed. Killing the whole
# group closes the command's pipes and the wait ends in milliseconds; the
# bound is for a process outside the group that still holds them, or for a
# kill the kernel refused.
_KILL_WAIT_SECONDS = 5.0


def text_overlap(a: str, b: str) -> float:
    """Word overlap ratio for deduplication.

    Filters words shorter than 3 characters to avoid false positives
    from stop words (the, is, a, in, etc.).

    Returns 0.0-1.0 representing what fraction of the smaller
    text's words appear in the larger text.
    """
    words_a = set(w for w in a.lower().split() if len(w) >= 3)
    words_b = set(w for w in b.lower().split() if len(w) >= 3)
    if not words_a or not words_b:
        return 0.0
    overlap = len(words_a & words_b)
    smaller = min(len(words_a), len(words_b))
    return overlap / smaller


async def _discard(stream: asyncio.StreamReader | None) -> None:
    """Read a stream to its end and throw the data away."""
    while stream is not None and await stream.read(65536):
        pass


async def kill_process_group(proc: asyncio.subprocess.Process) -> None:
    """Kill a shell command that is still running, as a whole, and wait for its shell, but not forever.

    ``proc`` must have been started with ``start_new_session=True``, which
    makes its pid its process group. ``proc.kill()`` alone signals only the
    shell: what the shell started runs on and keeps the stdout/stderr pipes
    open, and a ``proc.wait()`` entered before the shell's exit is known
    returns only once those pipes are closed, so the caller would wait for
    the whole command. That holds up to Python 3.12; 3.13 and 3.14 report
    the shell's exit at once in their current releases (3.13.16, 3.14.8;
    CPython gh-119710).

    A shell whose exit is already known (``returncode`` is set) is left
    alone, and so is a job it left running: ``proc.wait()`` returns at once
    for it, so nothing holds the caller, and its pid is no longer ours to
    signal.
    """
    if proc.returncode is not None:
        return
    try:
        if hasattr(os, "killpg"):
            os.killpg(proc.pid, signal.SIGKILL)
        else:  # Windows has no process groups: the shell only
            proc.kill()
    except (ProcessLookupError, PermissionError):
        pass  # nothing is left in the group, or nothing in it may be signalled
    try:
        # Read the pipes to their end while waiting: once communicate() has
        # been cancelled nobody reads them, and asyncio stops watching a pipe
        # that has more than 128 KiB unread, so it would never see it close.
        await asyncio.wait_for(
            asyncio.gather(_discard(proc.stdout), _discard(proc.stderr), proc.wait()),
            timeout=_KILL_WAIT_SECONDS,
        )
    except TimeoutError:
        logger.warning(
            "Shell command (pid %d) had not ended %.1fs after the attempt to kill it: "
            "something it started may still be running and holding its output",
            proc.pid,
            _KILL_WAIT_SECONDS,
        )
