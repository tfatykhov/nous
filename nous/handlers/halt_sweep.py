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
from collections.abc import Awaitable
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
JOBS_MAX = 50
HALT_REASON_TEXT = "halted by the owner"

_STEP_WORDS = {
    "subtasks": "subtasks",
    "chains": "chains",
    "dags": "DAGs",
    "commands": "shell commands",
    "jobs": "Claude Code jobs",
}
_SCOPE_TEXT = (
    "Nothing new can start, and every tool call from background work is refused. "
    "This does not stop a script that is already running or a caller that posts to chat."
)


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
    chains_truncated: bool = False
    failed: list[str] = field(default_factory=list)

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
            "chains_truncated": self.chains_truncated,
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
            f"Cancelled: {', '.join(parts)}." if parts else "Nothing was running.",
        ]
        if self.failed:
            words = ", ".join(_STEP_WORDS.get(step, step) for step in self.failed)
            sentences.append(f"Could not stop: {words}. Press Halt again to retry.")
        if self.chains_truncated:
            sentences.append("Some chains were not reached. Press Halt again to reach them.")
        if not self.jobs_checked and "jobs" not in self.failed:
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

    async def run(self, *, actor: str | None) -> HaltReport:
        """The whole sweep, then one ``sweep`` audit row with the counts."""
        report = HaltReport()
        await self._step(report, "subtasks", self._subtasks(report), STEP_TIMEOUT_SECONDS)
        if self._continuation is not None and self._db is not None:
            await self._step(report, "chains", self._chains(report, actor), ROOTS_TIMEOUT_SECONDS)
        if self._dags is not None:
            await self._step(report, "dags", self._cancel_dags(report), STEP_TIMEOUT_SECONDS)
        await self._step(report, "commands", self._commands(report), STEP_TIMEOUT_SECONDS)
        await self._step(report, "jobs", self._jobs(report), JOBS_TIMEOUT_SECONDS)
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

    async def _chains(self, report: HaltReport, actor: str | None) -> None:
        async with self._db.session() as session:
            roots = await open_root_ids(session, self._settings.agent_id)
        failures = 0
        by_harness = cancel_provenance.halt_record()
        for start in range(0, len(roots), ROOTS_BATCH):
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

    async def _jobs(self, report: HaltReport) -> None:
        """Claude Code jobs: the job directories (names that are job ids, real directories), newest first; a job
        whose ``status`` says running is cancelled and marked as cancelled by the harness. A job whose status could
        not be read leaves the report saying the jobs were not checked."""
        workspace = getattr(self._settings, "workspace_dir", "")
        jobs_dir = Path(workspace) / "claude-jobs" if workspace else None
        if jobs_dir is None or not jobs_dir.is_dir():
            return
        from nous.api.job_marks import JOB_ID_RE
        from nous.cognitive.execution_ledger import observed_status

        names = sorted(
            (
                entry.name
                for entry in os.scandir(jobs_dir)
                if JOB_ID_RE.fullmatch(entry.name) and entry.is_dir(follow_symlinks=False)
            ),
            reverse=True,
        )[:JOBS_MAX]
        if not names:
            return
        run_runner = self._run_runner
        if run_runner is None:
            from nous.api.builtin_tools import run_runner
        unread = 0
        for job_id in names:
            code, out = await run_runner(workspace, ["status", job_id], RUNNER_CALL_SECONDS)
            status = observed_status(out) if code == 0 else None
            if status is None:
                unread += 1
                continue
            if status != "running":
                continue
            code, _out = await run_runner(workspace, ["cancel", job_id], RUNNER_CALL_SECONDS)
            if code == 0:
                report.jobs += 1
                if not cancel_provenance.write_job_cancelled_by(workspace, job_id, cancel_provenance.halt_record()):
                    logger.warning("Background halt: could not mark job %s as cancelled by the harness", job_id)
        if unread:
            report.jobs_checked = False
