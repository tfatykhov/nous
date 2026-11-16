"""Goals PR 0c: the one-shot cancel sweep a halt runs, and its report.

Refusing background tool calls is what holds a halt (``AgentRunner._authorize_tool_call``); the sweep saves work and
cost, best effort. Each step is isolated and bounded: one that raises or times out is named in the report and the
next still runs, and nothing it does can clear the halt. In order:

1. Subtasks, in one statement, before anything else, so a turn whose model call returns later finds its row terminal.
2. Chains: every open root that is not a schedule container (cancelling one would end its schedule for good), through
   the continuation runner's ``cancel_root``, eight at a time.
3. DAGs: every active one, through the orchestrator, recorded as cancelled by the harness.
4. Shell commands of background turns: their process groups are signalled (``background_processes.kill_all``).
5. Claude Code jobs: each job directory in the workspace whose ``runner.sh status`` says running is cancelled.

The report is counts and fixed words only, never a row's text.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import exists, select
from sqlalchemy.orm import aliased

from nous import background_processes, cancel_provenance
from nous.background_halt import AuditEntry, BackgroundHalt
from nous.brain import continuation, intentions
from nous.cancellation import cancel_requested
from nous.storage.models import Intention

logger = logging.getLogger(__name__)

STEP_TIMEOUT_SECONDS = 30.0
ROOTS_TIMEOUT_SECONDS = 180.0
JOBS_TIMEOUT_SECONDS = 120.0
RUNNER_CALL_SECONDS = 30.0
ROOTS_MAX = 200
ROOTS_BATCH = 8
# The jobs step starts no new job after this many seconds, so a status and a cancel call still fit in its timeout.
JOBS_SCAN_SECONDS = JOBS_TIMEOUT_SECONDS - 2 * RUNNER_CALL_SECONDS
HALT_REASON_TEXT = "halted by the owner"
# The words claude-jobs/runner.sh writes to a job's status file when the job has ended, and "gone" (a running job
# whose worker is known to be gone, nous.dag.typed_checks.job_state_for). Any other word, or none, is asked.
FINAL_JOB_WORDS = frozenset({"completed", "failed", "cancelled", "gone"})

_STEP_WORDS = {
    "subtasks": "subtasks",
    "chains": "chains",
    "dags": "DAGs",
    "commands": "shell commands",
    "jobs": "Claude Code jobs",
}
_SCOPE_TEXT = "Nothing new can start, and every tool call from background work is refused."


def _count(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


@dataclass
class HaltReport:
    """What one sweep cancelled, and which steps failed."""

    chains: int = 0
    subtasks: int = 0
    subtasks_running: int = 0
    dags: int = 0
    commands: int = 0
    jobs: int = 0
    jobs_checked: bool = True
    # Job directories whose status file shows no final word and whose job was not asked to the end: its status
    # could not be read from the runner, or the deadline came first.
    jobs_not_checked: int = 0
    jobs_not_cancelled: int = 0  # running jobs whose cancel failed or timed out
    chains_truncated: bool = False
    failed: list[str] = field(default_factory=list)
    # The halt this sweep belongs to ended (a resume) or a newer halt replaced it: the steps after that point did not
    # run (the newer halt's own sweep does them).
    stopped: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "cancelled": {
                "chains": self.chains,
                "subtasks": self.subtasks,
                "subtasks_running": self.subtasks_running,
                "dags": self.dags,
                "commands": self.commands,
                "jobs": self.jobs,
            },
            "failed": list(self.failed),
            "jobs_checked": self.jobs_checked,
            "jobs_not_checked": self.jobs_not_checked,
            "jobs_not_cancelled": self.jobs_not_cancelled,
            "chains_truncated": self.chains_truncated,
            "stopped": self.stopped,
        }

    def describe(self) -> str:
        """The one message of a halt: fixed vocabulary and counts."""
        parts = []
        if self.chains:
            parts.append(_count(self.chains, "chain", "chains"))
        if self.subtasks:
            text = _count(self.subtasks, "subtask", "subtasks")
            if self.subtasks_running:
                text += f" ({self.subtasks_running} {'was' if self.subtasks_running == 1 else 'were'} running)"
            parts.append(text)
        if self.dags:
            parts.append(_count(self.dags, "DAG", "DAGs"))
        if self.commands:
            parts.append(_count(self.commands, "shell command", "shell commands"))
        if self.jobs:
            parts.append(_count(self.jobs, "Claude Code job", "Claude Code jobs"))
        sentences = [
            "Background work is halted.",
            f"Cancelled: {', '.join(parts)}." if parts else "Nothing was cancelled.",
        ]
        if self.stopped:
            sentences.append("The sweep stopped early: background work was resumed or halted again.")
        if self.failed:
            words = ", ".join(_STEP_WORDS.get(step, step) for step in self.failed)
            sentences.append(f"Could not stop: {words}. Press Halt again to retry.")
        if self.chains_truncated:
            sentences.append("Some chains were not reached. Press Halt again to reach them.")
        if self.jobs_not_cancelled:
            sentences.append(
                f"{_count(self.jobs_not_cancelled, 'Claude Code job', 'Claude Code jobs')} could not be cancelled."
            )
        if self.jobs_not_checked:
            sentences.append(
                f"{_count(self.jobs_not_checked, 'Claude Code job', 'Claude Code jobs')} could not be checked."
            )
        elif not self.jobs_checked and "jobs" not in self.failed:
            sentences.append("Claude Code jobs were not checked.")
        sentences.append(_SCOPE_TEXT)
        return " ".join(sentences)


async def open_root_ids(session: Any, agent_id: str, *, limit: int = ROOTS_MAX) -> list[UUID]:
    """The ids of the open roots that are not schedule containers, newest first: the roots
    ``continuation.list_roots(state="open", exclude_containers=True)`` lists, ids only."""
    lineage_row = aliased(Intention)
    query = (
        select(Intention.id)
        .where(
            Intention.agent_id == agent_id,
            Intention.id == Intention.root_id,
            Intention.wake_policy != intentions.WAKE_CONTAINER,
            Intention.root_cancelled_at.is_(None),
            Intention.root_expired_at.is_(None),
            exists().where(
                lineage_row.agent_id == agent_id,
                lineage_row.root_id == Intention.id,
                lineage_row.state.in_(continuation.OPEN_STATES),
            ),
        )
        .order_by(Intention.created_at.desc(), Intention.id)
        .limit(limit)
    )
    return list((await session.execute(query)).scalars())


def _jobs_to_ask(jobs_dir: Path, workspace: str) -> list[str]:
    """The job directories (names that are job ids, real directories) whose status file does not show a final word
    (``FINAL_JOB_WORDS``): those with a status word, newest first, then those whose status file could not be read,
    newest first. Reads files: run it in a thread. The status file is trusted: a job whose file shows a final word
    is not asked, even if something else is still running under it."""
    from nous.api.job_marks import JOB_ID_RE
    from nous.dag.typed_checks import job_state_for

    names = sorted(
        (
            entry.name
            for entry in os.scandir(jobs_dir)
            if JOB_ID_RE.fullmatch(entry.name) and entry.is_dir(follow_symlinks=False)
        ),
        reverse=True,
    )
    worded: list[str] = []
    unread: list[str] = []
    for job_id in names:
        recorded = job_state_for(job_id, workspace)
        if recorded is None:
            unread.append(job_id)
        elif recorded[0] not in FINAL_JOB_WORDS:
            worded.append(job_id)
    return worded + unread


class HaltSweeper:
    """The cancel sweep. Every component is optional: a missing one's step is skipped."""

    def __init__(
        self,
        *,
        settings: Any,
        heart: Any,
        halt: BackgroundHalt,
        database: Any = None,
        continuation_runner: Any = None,
        dag_orchestrator: Any = None,
        run_runner: Any = None,
    ) -> None:
        self._settings = settings
        self._heart = heart
        self._halt = halt
        self._db = database
        self._continuation = continuation_runner
        self._dags = dag_orchestrator
        self._run_runner = run_runner

    async def run(self, *, actor: str | None, epoch: int | None = None) -> HaltReport:
        """The whole sweep, then one ``sweep`` audit row with the counts. ``epoch``: the halt this sweep belongs to
        (``HaltOutcome.epoch``; default the current one). Before each step, and between the batches of a long one, the
        sweep stops when that halt is no longer on: a resume landed, or a newer halt, whose own sweep goes on."""
        epoch = self._halt.epoch if epoch is None else epoch
        report = HaltReport()
        steps: list[tuple[str, Callable[[], Awaitable[Any]], float]] = [
            ("subtasks", lambda: self._subtasks(report), STEP_TIMEOUT_SECONDS)
        ]
        if self._continuation is not None and self._db is not None:
            steps.append(("chains", lambda: self._chains(report, actor, epoch), ROOTS_TIMEOUT_SECONDS))
        if self._dags is not None:
            steps.append(("dags", lambda: self._cancel_dags(report), STEP_TIMEOUT_SECONDS))
        steps.append(("commands", lambda: self._commands(report), STEP_TIMEOUT_SECONDS))
        steps.append(("jobs", lambda: self._jobs(report, epoch), JOBS_TIMEOUT_SECONDS))
        for name, work, timeout in steps:
            if not self._still_halted(epoch):
                report.stopped = True
                break
            await self._step(report, name, work(), timeout)
        report.stopped = report.stopped or not self._still_halted(epoch)
        await self._halt.record(AuditEntry("sweep", actor, None, False, None, report.as_dict()))
        logger.warning("Background halt: sweep done %s", report.as_dict())
        return report

    async def cancel_new_work(self) -> None:
        """What a resume runs before it clears the flag: the subtask and DAG steps again, so nothing created or left
        since the halt's sweep starts when the flag clears."""
        report = HaltReport()
        await self._step(report, "subtasks", self._subtasks(report), STEP_TIMEOUT_SECONDS)
        if self._dags is not None:
            await self._step(report, "dags", self._cancel_dags(report), STEP_TIMEOUT_SECONDS)
        if report.subtasks or report.dags or report.failed:
            logger.warning("Background halt: the resume sweep %s", report.as_dict())

    def _still_halted(self, epoch: int) -> bool:
        """The halt this sweep belongs to is still the one on."""
        return self._halt.halted and self._halt.epoch == epoch

    async def _step(self, report: HaltReport, name: str, work: Awaitable[Any], timeout: float) -> None:
        try:
            await asyncio.wait_for(work, timeout=timeout)
        except asyncio.CancelledError:
            if cancel_requested():
                raise
            logger.error("Background halt: the %s step was cancelled", name)
            report.failed.append(name)
        except Exception:
            logger.error("Background halt: the %s step failed", name, exc_info=True)
            report.failed.append(name)

    async def _subtasks(self, report: HaltReport) -> None:
        rows = await self._heart.subtasks.cancel_all_active()
        report.subtasks += len(rows)
        report.subtasks_running += sum(1 for _id, status in rows if status == "running")

    async def _chains(self, report: HaltReport, actor: str | None, epoch: int) -> None:
        async with self._db.session() as session:
            roots = await open_root_ids(session, self._settings.agent_id)
        failures = 0
        by_harness = cancel_provenance.halt_record()
        for start in range(0, len(roots), ROOTS_BATCH):
            if not self._still_halted(epoch):
                report.stopped = True
                return
            batch = roots[start : start + ROOTS_BATCH]
            outcomes = await asyncio.gather(
                *(
                    self._continuation.cancel_root(
                        root, reason=HALT_REASON_TEXT, actor=actor or "halt", cancelled_by=by_harness
                    )
                    for root in batch
                ),
                return_exceptions=True,
            )
            for outcome in outcomes:
                if isinstance(outcome, (continuation.CancelRefused, continuation.RootNotFound)):
                    continue  # nothing running under it, or gone: skipped, not failed
                if isinstance(outcome, BaseException):  # a cancelled one included: counted, never re-raised
                    failures += 1
                    logger.warning("Background halt: a chain could not be cancelled", exc_info=outcome)
                    continue
                report.chains += 1
                report.chains_truncated = report.chains_truncated or bool(getattr(outcome, "truncated", False))
        if failures:
            raise RuntimeError(f"{failures} chain(s) could not be cancelled")

    async def _cancel_dags(self, report: HaltReport) -> None:
        report.dags += await self._dags.cancel_all_active(cancel_provenance.halt_record())

    async def _commands(self, report: HaltReport) -> None:
        report.commands += background_processes.kill_all()

    async def _jobs(self, report: HaltReport, epoch: int | None = None) -> None:
        """Claude Code jobs: the job directories whose status file shows no final word (``_jobs_to_ask``, read off
        the event loop) are asked ``runner.sh status``, those with a status word first, newest first, then those
        whose status file could not be read, so a halt pressed again reaches further when some of them are slow to
        answer. One that says running is cancelled and marked as cancelled by the harness. A cancel that fails is
        counted and the scan goes on. A job whose status the runner did not give, and the jobs the deadline
        (``JOBS_SCAN_SECONDS``) leaves unasked, are counted as not checked. Either count marks the step failed, so
        the owner is offered the retry."""
        workspace = getattr(self._settings, "workspace_dir", "")
        jobs_dir = Path(workspace) / "claude-jobs" if workspace else None
        if jobs_dir is None or not jobs_dir.is_dir():
            return
        from nous.cognitive.execution_ledger import observed_status

        loop = asyncio.get_running_loop()
        deadline = loop.time() + JOBS_SCAN_SECONDS
        names = await asyncio.to_thread(_jobs_to_ask, jobs_dir, workspace)
        if not names:
            return
        run_runner = self._run_runner
        if run_runner is None:
            from nous.api.builtin_tools import run_runner
        unread = failed = 0
        for index, job_id in enumerate(names):
            if epoch is not None and not self._still_halted(epoch):
                report.stopped = True
                return
            if loop.time() > deadline:
                unread += len(names) - index
                break
            code, out = await run_runner(workspace, ["status", job_id], RUNNER_CALL_SECONDS)
            status = observed_status(out) if code == 0 else None
            if status is None:
                unread += 1
                continue
            if status != "running":
                continue
            code, _out = await run_runner(workspace, ["cancel", job_id], RUNNER_CALL_SECONDS)
            if code != 0:
                failed += 1
                logger.warning("Background halt: the cancel of job %s failed (exit %s)", job_id, code)
                continue
            report.jobs += 1
            if not cancel_provenance.write_job_cancelled_by(workspace, job_id, cancel_provenance.halt_record()):
                logger.warning("Background halt: could not mark job %s as cancelled by the harness", job_id)
        report.jobs_not_checked += unread
        report.jobs_not_cancelled += failed
        if unread:
            report.jobs_checked = False
        if failed or unread:
            report.failed.append("jobs")
