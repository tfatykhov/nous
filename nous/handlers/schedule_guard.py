"""Run-level guard for scheduled script jobs.

A recurring schedule whose run mostly executes a script and reports "nothing" can carry a guard in
its own ``metadata`` (written by the owner, never by the agent or the task text)::

    {"guard": {"command": "bash /path/to/sweep.sh", "skip_when": "exit0_quiet", "timeout_seconds": 60}}

The subtask worker runs the command before the run's model turn. When the command certifies there is
nothing to do (``exit0_quiet``: exit 0 and whitespace-only stdout and stderr) the run completes with no model
call. Any other result (non-zero exit, output on either stream, timeout, an unknown ``skip_when``, an error starting the
command) fails open: the model turn runs as before, with ``receipt()`` appended to its task text so the
model does not need to run the command again. ``NOUS_SCHEDULE_GUARDS_ENABLED=false`` ignores guards.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import time
from dataclasses import dataclass
from typing import Any

SKIP_WHEN_EXIT0_QUIET = "exit0_quiet"
DEFAULT_TIMEOUT_SECONDS = 60
MAX_TIMEOUT_SECONDS = 300
OUTPUT_MAX_BYTES = 2048


@dataclass(frozen=True, slots=True)
class GuardSpec:
    command: str
    skip_when: str
    timeout_seconds: int


@dataclass(frozen=True, slots=True)
class GuardResult:
    exit_code: int | None  # None when the command timed out or could not start
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool = False
    error: str | None = None


def parse_guard(raw: Any) -> GuardSpec | None:
    """The guard a schedule's ``metadata.guard`` describes, or None when there is no usable one."""
    if not isinstance(raw, dict):
        return None
    command = raw.get("command")
    if not isinstance(command, str) or not command.strip():
        return None
    timeout = raw.get("timeout_seconds")
    if not isinstance(timeout, int) or isinstance(timeout, bool):
        timeout = DEFAULT_TIMEOUT_SECONDS
    return GuardSpec(
        command=command,
        skip_when=str(raw.get("skip_when") or SKIP_WHEN_EXIT0_QUIET),
        timeout_seconds=min(max(timeout, 1), MAX_TIMEOUT_SECONDS),
    )


def should_skip(spec: GuardSpec, result: GuardResult) -> bool:
    """True only when the guard certifies the run has nothing to do.

    stderr counts: a script ending in ``; true`` reports a failure only there, so a skip
    that ignored it would hide a broken script behind "nothing to do". Known chatter
    belongs in the script (``2>/dev/null``), not in the guard.
    """
    return (
        spec.skip_when == SKIP_WHEN_EXIT0_QUIET
        and result.exit_code == 0
        and not result.stdout.strip()
        and not result.stderr.strip()
    )


def _clip(text: str) -> str:
    return text.encode("utf-8")[:OUTPUT_MAX_BYTES].decode("utf-8", errors="ignore")


def _kill_group(proc: asyncio.subprocess.Process) -> None:
    # The command runs in its own session, so its children die with it.
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGKILL)


async def run_guard(spec: GuardSpec) -> GuardResult:
    """Run the guard command with a hard timeout. Never raises, except for cancellation.

    On timeout or cancellation the command's whole process group is killed.
    """
    start = time.monotonic()

    def elapsed() -> int:
        return int((time.monotonic() - start) * 1000)

    try:
        proc = await asyncio.create_subprocess_shell(
            spec.command,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
    except Exception as exc:
        return GuardResult(None, "", "", elapsed(), error=f"{type(exc).__name__}: {exc}")
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=spec.timeout_seconds)
    except TimeoutError:
        _kill_group(proc)
        await proc.wait()
        return GuardResult(None, "", "", elapsed(), timed_out=True)
    except Exception as exc:
        _kill_group(proc)
        await proc.wait()
        return GuardResult(None, "", "", elapsed(), error=f"{type(exc).__name__}: {exc}")
    except BaseException:
        _kill_group(proc)
        raise
    return GuardResult(
        proc.returncode, stdout.decode("utf-8", errors="replace"), stderr.decode("utf-8", errors="replace"), elapsed()
    )


def skip_text(spec: GuardSpec, result: GuardResult) -> str:
    """The result text of a run the guard completed."""
    return f"skipped_by_guard: {spec.command} exit=0 stdout=empty stderr=empty ({result.duration_ms} ms)"


def receipt(spec: GuardSpec, result: GuardResult) -> str:
    """What the model is told about a guard that did not certify the run as a no-op."""
    if result.error is not None:
        outcome = f"error: {result.error}"
    elif result.timed_out:
        outcome = f"timed out after {spec.timeout_seconds}s (killed)"
    else:
        outcome = f"exit: {result.exit_code}"
    lines = [
        "",
        "",
        "[Run guard receipt] The system already ran this command before this turn; "
        "use its result below instead of running it again just to see the output.",
        f"command: {spec.command}",
        f"{outcome} ({result.duration_ms} ms)",
    ]
    if spec.skip_when != SKIP_WHEN_EXIT0_QUIET:
        lines.append(f"note: unknown skip_when {spec.skip_when!r}; the guard never skips with it")
    for name, text in (("stdout", result.stdout), ("stderr", result.stderr)):
        if text.strip():
            lines += [f"{name} (command output, first {OUTPUT_MAX_BYTES} bytes):", "```", _clip(text).rstrip(), "```"]
    return "\n".join(lines)
