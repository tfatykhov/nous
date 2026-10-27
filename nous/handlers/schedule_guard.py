"""Run-level guard for scheduled script jobs.

A recurring schedule whose run mostly executes a script and reports "nothing" can carry a guard in
its own ``metadata``, which names an entry of the owner's allowlist ``NOUS_SCHEDULE_GUARD_COMMANDS``::

    {"guard": {"name": "ledger_sweep", "mode": "skip", "skip_when": "exit0_quiet", "timeout_seconds": 60}}

The command itself comes only from the allowlist (env), never from the row: the agent can write
schedule rows through its shell and database access, so the row may only choose among commands
the owner approved. A name that is not in the allowlist means no guard. The subtask worker runs
the command before the run's model turn. When the command certifies there is nothing to do
(``exit0_quiet``: exit 0 and whitespace-only stdout and stderr) the run completes with no model
call. Any other result (non-zero exit, output on either stream, timeout, an unknown ``skip_when``,
an error starting the command) fails open: the model turn runs as before, with ``receipt()``
appended to its task text so the model does not need to run the command again.
``NOUS_SCHEDULE_GUARDS_ENABLED=false`` ignores guards.

``mode`` (phase 2) is ``shadow`` when absent: the command runs and is recorded, but the model turn always
runs too, and after it the worker records whether the model acted (``acted_signals``). Only ``skip`` lets
the guard complete a run, and only the owner writes it. Any other mode falls through, like an unknown
``skip_when``.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from nous.cognitive.execution_ledger import redact_error_text
from nous.cognitive.ledger_store import durable_key_args
from nous.utils import kill_process_group

SKIP_WHEN_EXIT0_QUIET = "exit0_quiet"
MODE_SHADOW = "shadow"
MODE_SKIP = "skip"
MODES = (MODE_SHADOW, MODE_SKIP)
# Why a guard run did not certify a no-op; ``not_allowlisted`` is a guard that names no allowlisted command.
REASONS = ("output", "stderr", "exit", "timeout", "error", "not_allowlisted")
# The kinds of action acted_signals reports, in its order.
SIGNALS = ("report", "finding", "inbox", "push", "ledger")
# The ledger statuses of a call that did not take effect (a blocked call was refused before dispatch).
_NOT_ACTING_STATUSES = frozenset({"blocked"})
DEFAULT_TIMEOUT_SECONDS = 60
# The guard runs after the row is marked running and before the turn's own timeout starts, so a
# guard that takes long eats into F101's grace (find_active_run counts a running row only until
# started_at + timeout + 300 s; past that a second run could start). Capped well below it: at most
# 120 s plus kill_process_group's 5 s bound.
MAX_TIMEOUT_SECONDS = 120
OUTPUT_MAX_BYTES = 2048
STDERR_HEAD_BYTES = 512  # kept in the durable record of a run


@dataclass(frozen=True, slots=True)
class GuardSpec:
    name: str
    command: str
    skip_when: str
    timeout_seconds: int
    mode: str = MODE_SHADOW  # kept as written when unknown, so the worker can see it and fall through


@dataclass(frozen=True, slots=True)
class GuardResult:
    exit_code: int | None  # None when the command timed out or could not start
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool = False
    error: str | None = None
    stdout_bytes: int = 0
    stderr_bytes: int = 0


def parse_guard(raw: Any, commands: Mapping[str, str]) -> GuardSpec | None:
    """The guard a schedule's ``metadata.guard`` describes, or None when there is no usable one.

    ``raw["name"]`` must name an entry of ``commands`` (the owner's allowlist); any ``command``
    key in ``raw`` is ignored.
    """
    if not isinstance(raw, dict):
        return None
    name = raw.get("name")
    command = commands.get(name) if isinstance(name, str) else None
    if not isinstance(command, str) or not command.strip():
        return None
    timeout = raw.get("timeout_seconds")
    if not isinstance(timeout, int) or isinstance(timeout, bool):
        timeout = DEFAULT_TIMEOUT_SECONDS
    return GuardSpec(
        name=name,
        command=command,
        skip_when=str(raw.get("skip_when") or SKIP_WHEN_EXIT0_QUIET),
        timeout_seconds=min(max(timeout, 1), MAX_TIMEOUT_SECONDS),
        mode=str(raw["mode"]) if "mode" in raw else MODE_SHADOW,
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


def fall_through_reason(spec: GuardSpec, result: GuardResult) -> str | None:
    """Why this guard run does not end the run's work, or None when it certified a no-op in a known mode.

    An unknown ``mode`` or ``skip_when`` on an otherwise quiet run is a configuration error: ``error``.
    """
    if result.error is not None:
        return "error"
    if result.timed_out:
        return "timeout"
    if result.exit_code != 0:
        return "exit"
    if result.stdout.strip():
        return "output"
    if result.stderr.strip():
        return "stderr"
    if spec.mode not in MODES or not should_skip(spec, result):
        return "error"
    return None


def command_digest(command: str) -> str:
    """How the execution ledger keeps a bash command (``key_args.command_sha256``)."""
    return durable_key_args("bash", {"command": command})["command_sha256"]


def acted_signals(
    row: Any,
    *,
    inbox_written: bool,
    pushed: bool,
    ledger: Sequence[tuple[str, str, Mapping[str, Any]]],
    guard_command: str,
) -> list[str]:
    """THE definition of "the model acted" in a guarded run (F107 phase 2): the kinds of action it took.

    ``row`` is the run's subtask row after the turn, ``inbox_written`` whether the inbox writer wrote a row
    for it, ``pushed`` whether its Telegram push was sent, and ``ledger`` the ``(tool_name, status, key_args)``
    of its execution-ledger entries. Kinds, in this order:

    - ``report``: the report has a non-empty summary;
    - ``finding``: the report has findings or next actions, or says the work is incomplete or blocked;
    - ``inbox`` / ``push``: an inbox row was written / a push was sent, for a run that completed (on a
      failed run they carry the system's failure notice, not something the model did);
    - ``ledger``: a ledger entry that was not blocked, other than a bash run of the guard's own command.
      The durable ledger holds only side-effecting calls (write, external, irreversible), never reads.

    The model acted when the list is not empty.
    """
    report = getattr(row, "report_jsonb", None)
    report = report if isinstance(report, dict) else {}
    completed = getattr(row, "status", None) == "completed"
    own = command_digest(guard_command)
    signals = []
    if str(report.get("summary") or "").strip():
        signals.append("report")
    if report.get("findings") or report.get("next_actions") or report.get("incomplete") or report.get("blocked_reason"):
        signals.append("finding")
    if inbox_written and completed:
        signals.append("inbox")
    if pushed and completed:
        signals.append("push")
    if any(
        status not in _NOT_ACTING_STATUSES and not (tool == "bash" and (key_args or {}).get("command_sha256") == own)
        for tool, status, key_args in ledger
    ):
        signals.append("ledger")
    return signals


def _clip(text: str, limit: int = OUTPUT_MAX_BYTES) -> str:
    return text.encode("utf-8")[:limit].decode("utf-8", errors="ignore")


async def run_guard(spec: GuardSpec) -> GuardResult:
    """Run the guard command with a hard timeout. Never raises, except for cancellation.

    On timeout or cancellation the command's whole process group is killed, even when the
    shell itself has already exited and only a background child of it holds the pipes
    (``even_if_exited``), and the wait for it is bounded (``kill_process_group``), so a child
    that left the group and still holds the pipes cannot keep the worker waiting either.
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
        await kill_process_group(proc, even_if_exited=True)
        return GuardResult(None, "", "", elapsed(), timed_out=True)
    except Exception as exc:
        await kill_process_group(proc, even_if_exited=True)
        return GuardResult(None, "", "", elapsed(), error=f"{type(exc).__name__}: {exc}")
    except BaseException:
        await kill_process_group(proc, even_if_exited=True)
        raise
    return GuardResult(
        proc.returncode,
        stdout.decode("utf-8", errors="replace"),
        stderr.decode("utf-8", errors="replace"),
        elapsed(),
        stdout_bytes=len(stdout),
        stderr_bytes=len(stderr),
    )


def record(spec: GuardSpec, result: GuardResult, *, skipped: bool, reason: str | None = None) -> dict[str, Any]:
    """The durable record of one run of the guard (JSON-ready).

    ``would_skip`` is whether the command certified a no-op, whatever the mode; ``reason`` is
    ``fall_through_reason`` unless the caller gives one (a skip whose row write failed is an ``error``).

    ``stderr_head`` is the first ``STDERR_HEAD_BYTES`` of stderr; it and ``command`` pass through
    the ledger's error redaction since they are stored as written.
    """
    return {
        "name": spec.name,
        "command": redact_error_text(spec.command),
        "skip_when": spec.skip_when,
        "timeout_seconds": spec.timeout_seconds,
        "exit_code": result.exit_code,
        "timed_out": result.timed_out,
        "error": result.error,
        "duration_ms": result.duration_ms,
        "stdout_bytes": result.stdout_bytes,
        "stderr_bytes": result.stderr_bytes,
        "stderr_head": redact_error_text(_clip(result.stderr, STDERR_HEAD_BYTES)),
        "skipped": skipped,
        "mode": spec.mode,
        "would_skip": should_skip(spec, result),
        "reason": reason or fall_through_reason(spec, result),
    }


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
    if spec.mode not in MODES:
        lines.append(f"note: unknown mode {spec.mode!r}; the guard never skips with it")
    for name, text in (("stdout", result.stdout), ("stderr", result.stderr)):
        if text.strip():
            lines += [f"{name} (command output, first {OUTPUT_MAX_BYTES} bytes):", "```", _clip(text).rstrip(), "```"]
    return "\n".join(lines)
