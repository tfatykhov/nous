"""F118: the outside judge's queue and worker.

``submit_decision`` is called by ``resolve_decisions`` after its reply is built: it fixes the claim (grader, grade,
note, time) and puts it on an in-memory queue, without waiting and without raising. It drops a subject (and counts
it) when the queue is full, the service is closed, or the subject is already queued or being judged. Jobs waiting
when the process stops are not kept.

One worker judges one subject at a time. For each it waits until ``SETTLE_SECONDS`` after the submit (the tool-call
recorder writes a turn's rows in the background), skips a subject whose row at this judge's name and version is final,
builds the evidence (in a thread), and then:

- no evidence, or a record that cannot be rendered: a ``skipped`` row (``cannot_tell``, a label says why), no call;
- otherwise, within the daily limit: one call of the judge under ``asyncio.wait_for``, told how many requests are
  left today, and one row, ``ok``, ``error`` or ``timeout``, with the number of model requests it sent. The limit is
  ``NOUS_OUTSIDE_JUDGE_DAILY_CALLS`` requests of this judge a UTC day, counted from the table before every call; at the
  daily limit no further decision is judged that day, and when the count cannot be read none is judged either
  (counted here, logged once a day).

An ``error`` or ``timeout`` row is not final: the next submit of that decision tries again, at most ``MAX_ATTEMPTS``
times in all (``store``). Everything in the worker is caught: one WARNING with the exception's class name, and an
``error`` row when one can be written. A cancellation of one judging that does not come from ``close`` ends that
judging, writes nothing and leaves the worker running; a worker that ended anyway is started again by the next
submit. ``close`` stops taking work, waits ``CLOSE_TIMEOUT_SECONDS`` for the subject in flight, then cancels the
rest; a service built with its own ``close_timeout`` (the second judge's: ``call_bound`` of its timeout) waits that
long instead, so a request it has sent ends with its row. A judging that has not sent its request when close begins
sends none and writes nothing. The worker reads the decision and the session's records and writes only
``nous_system.judge_verdicts``.

``JudgeSet`` puts two such services side by side, a second judge on the same decisions: a submit hands both the same
claim, and neither one's failure, time or limit changes the other's.

With ``NOUS_OUTSIDE_JUDGE_REPORTS_ENABLED`` the first judge's service also has a report judge: ``submit_report`` (from
the task executor, after a completed task's row is written) queues a finished task's report, which the worker judges
as it stood at the task's completion, read from the task's row (a task not finished as completed is not judged). The
report judge shares the judge's name, so the daily limit counts both; reports may use at most half of it, so
decisions always keep the other half (a report past that half is not judged now). ``JudgeSet`` hands a report to the
first judge only (``SECOND_JUDGE_GRADES_REPORTS``). That service also runs the sweep (``sweep_once``): soon after the
start and then hourly, at most ``SWEEP_BATCH`` subjects without a final row, nothing queued once its calls today reach
half the day's limit. A second judge's service runs the same sweep over decisions, at its own name, version and limit
(``sweep=True``).
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID

from nous.brain.schemas import GRADED_OUTCOMES
from nous.cognitive.ledger_store import redacted_head
from nous.judge import evidence, store
from nous.judge.instructions import REPORT_VERDICTS as _REPORT_VERDICT_NAMES
from nous.judge.slot import JUDGE_STATUSES, Claim, Judge, JudgeOutput, failed

logger = logging.getLogger(__name__)

SETTLE_SECONDS = 30.0
JUDGE_TIMEOUT_SECONDS = 60.0
QUEUE_MAX = 50
CLOSE_TIMEOUT_SECONDS = 5.0
# How much later than the judge's own timeout a judge that ignores it is stopped (at most the timeout itself).
OUTER_GRACE_SECONDS = 5.0
DECISION = "decision"
DECISION_VERDICTS = frozenset({*GRADED_OUTCOMES, "cannot_tell"})
TASK_REPORT = "task_report"
REPORT_VERDICTS = frozenset(_REPORT_VERDICT_NAMES)
VERDICTS_BY_KIND = {DECISION: DECISION_VERDICTS, TASK_REPORT: REPORT_VERDICTS}
# A finished task's report as its author grades it (the claim read from the task's row is the same).
REPORT_CLAIM = Claim(grader=evidence.REPORT_CLAIM_GRADER, grade=evidence.REPORT_CLAIM_GRADE)
# The second judge grades decisions only for now: a finished task's report goes to the first judge alone. True hands
# each report to the second judge as well (its service then needs a report judge).
SECOND_JUDGE_GRADES_REPORTS = False
MAX_ATTEMPTS = store.MAX_ATTEMPTS
# The sweep: its first pass this long after the start, then one pass an interval; at most a batch of subjects a pass.
SWEEP_FIRST_DELAY_SECONDS = 120.0
SWEEP_INTERVAL_SECONDS = 3600.0
SWEEP_BATCH = 5
REASON_MAX = 200
MAX_LABELS = 8
LABEL_MAX = 64


@dataclass(frozen=True)
class _Item:
    kind: str
    subject_id: UUID
    claim: Claim
    resolved_at: datetime
    submitted: float  # time.monotonic() at the submit

    @property
    def key(self) -> tuple[str, UUID]:
        return self.kind, self.subject_id


def call_bound(timeout_seconds: float) -> float:
    """The longest one judging's call may take: the judge's own timeout and the grace after it (``_judge_one``)."""
    return timeout_seconds + min(OUTER_GRACE_SECONDS, timeout_seconds)


def _calls_reported(output: Any) -> int:
    """The model requests a judge says it sent; one when it does not say (or says something that is not a count)."""
    calls = getattr(output, "calls", None)
    return calls if isinstance(calls, int) and not isinstance(calls, bool) and calls >= 0 else 1


def _checked(output: Any, kind: str) -> JudgeOutput:
    """A judge's output as it may be stored: a status or verdict it may not give is an error; a confidence outside 0
    to 1, labels that are not short strings and a reason are bounded or dropped."""
    model = getattr(output, "model", None)
    model = model if isinstance(model, str) else None
    if not isinstance(output, JudgeOutput) or output.status not in JUDGE_STATUSES:
        return failed("error", model)
    tokens_in = output.tokens_in if isinstance(output.tokens_in, int) else None
    tokens_out = output.tokens_out if isinstance(output.tokens_out, int) else None
    if output.status != "ok":
        return failed(output.status, model, tokens_in=tokens_in, tokens_out=tokens_out)
    if output.verdict not in VERDICTS_BY_KIND.get(kind, ()):
        return failed("error", model, tokens_in=tokens_in, tokens_out=tokens_out)
    confidence = output.confidence
    if isinstance(confidence, bool) or not isinstance(confidence, int | float) or not math.isfinite(confidence):
        confidence = None
    elif not 0.0 <= confidence <= 1.0:
        confidence = None
    labels = tuple(label for label in output.labels if isinstance(label, str) and 0 < len(label) <= LABEL_MAX)
    reason = None
    if isinstance(output.reason, str) and output.reason.strip():
        reason = redacted_head(" ".join(output.reason.split()), REASON_MAX) or None
    return JudgeOutput(
        status="ok",
        verdict=output.verdict,
        confidence=None if confidence is None else float(confidence),
        labels=labels[:MAX_LABELS],
        reason=reason,
        model=model,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
    )


def _shown_claim(claim: Claim) -> Claim | None:
    """The claim the judge reads with a decision: the agent's own grade and note. A grade another grader gave (the
    owner, a fixed signal) is what the verdict is compared with, so the judge reads the record without it; it is kept
    on the verdict row only (``against_grader``, ``against_grade``)."""
    return claim if claim.grader == store.AGENT_GRADER else None


# The sweep's order: decisions graded by the owner, then what a restart lost (decisions the agent resolved, then
# finished reports), then decisions graded through the review route, then decisions graded by a fixed signal.
_REPORT_RANK = 2


def _sweep_rank(grader: str) -> int:
    if grader in store.OWNER_GRADERS:
        return 0
    if grader == store.AGENT_GRADER:
        return 1
    return 3 if grader == store.EXTERNAL_GRADER else 4


class OutsideJudge:
    """The queue, the worker and the daily limit of one judge for one agent (one Nous process per database and
    agent, like the execution ledger)."""

    def __init__(
        self,
        database: Any,
        settings: Any,
        judge: Judge,
        *,
        settle_seconds: float = SETTLE_SECONDS,
        timeout_seconds: float = JUDGE_TIMEOUT_SECONDS,
        queue_max: int = QUEUE_MAX,
        daily_calls: int | None = None,
        report_judge: Judge | None = None,
        sweep: bool | None = None,
        close_timeout: float | None = None,
    ) -> None:
        self._database = database
        # How long close waits for the subject being judged (None: CLOSE_TIMEOUT_SECONDS).
        self._close_timeout = CLOSE_TIMEOUT_SECONDS if close_timeout is None else close_timeout
        self._agent_id = settings.agent_id
        self._daily_calls = settings.outside_judge_daily_calls if daily_calls is None else daily_calls
        self.judge = judge
        # The judge of finished task reports (None: this service takes no report), and the hourly sweep. None: the
        # sweep runs in the service that judges reports, with NOUS_OUTSIDE_JUDGE_REPORTS_ENABLED, so the first judge's.
        # A second judge's service is built with sweep=True under the same setting (main.py, second_settings.py) and
        # sweeps decisions only: it has no report judge.
        self.report_judge = report_judge
        if sweep is None:
            sweep = report_judge is not None and bool(getattr(settings, "outside_judge_reports_enabled", False))
        self._sweep_on = sweep
        self._sweeper: asyncio.Task | None = None
        self._sweep_counts: dict[str, Any] = {
            "passes": 0,
            "enqueued": {},
            "held_at_half_limit": 0,
            "failed": 0,
            "last_pass_at": None,
        }
        self._settle = settle_seconds
        self._timeout = timeout_seconds
        self._queue: asyncio.Queue[_Item] = asyncio.Queue(maxsize=queue_max)
        self._pending: set[tuple[str, UUID]] = set()  # queued or being judged
        self._closed = False
        self._worker: asyncio.Task | None = None
        self._inflight: asyncio.Task | None = None
        self._dropped = 0
        self._over_cap = 0
        self._cap_logged_on: date | None = None

    # ---- intake ----------------------------------------------------------------------------------------------------

    def submit_decision(self, decision_id: Any, *, grade: Any, note: Any, grader: Any, resolved_at: Any) -> bool:
        """Queue one decision with its claim as it stands now. True when queued. Never raises and never waits."""
        try:
            if self._closed:
                self._dropped += 1
                return False
            subject_id = decision_id if isinstance(decision_id, UUID) else UUID(str(decision_id))
            if not isinstance(grade, str) or not isinstance(grader, str) or not isinstance(resolved_at, datetime):
                raise TypeError("a claim needs a grader, a grade and a time")
            claim = Claim(grader=grader, grade=grade, note=note if isinstance(note, str) else None)
            item = _Item(DECISION, subject_id, claim, resolved_at, time.monotonic())
            if item.key in self._pending:
                self._dropped += 1
                return False
            self._queue.put_nowait(item)
            self._pending.add(item.key)
            self._restart_if_ended()
            return True
        except Exception:
            self._dropped += 1
            return False

    def submit_report(self, subtask_id: Any, *, claimed_at: Any) -> bool:
        """Queue one finished task's report, judged by the report judge as it stood at the task's completion (read
        from the task's row then; ``claimed_at`` is when it was handed in). True when queued. A service without a
        report judge takes none and counts nothing; otherwise a report is dropped and counted like a decision. Never
        raises and never waits."""
        try:
            if self.report_judge is None:
                return False
            if self._closed:
                self._dropped += 1
                return False
            subject_id = subtask_id if isinstance(subtask_id, UUID) else UUID(str(subtask_id))
            if not isinstance(claimed_at, datetime):
                raise TypeError("a report needs the time it was handed in")
            item = _Item(TASK_REPORT, subject_id, REPORT_CLAIM, claimed_at, time.monotonic())
            if item.key in self._pending:
                self._dropped += 1
                return False
            self._queue.put_nowait(item)
            self._pending.add(item.key)
            self._restart_if_ended()
            return True
        except Exception:
            self._dropped += 1
            return False

    def _restart_if_ended(self) -> None:
        """Start the worker again when it was started once and has ended without ``close``."""
        if self._worker is not None and self._worker.done() and not self._closed:
            ended = self._worker
            reason = "cancelled" if ended.cancelled() else type(ended.exception()).__name__
            logger.warning("F118: the judge worker had ended (%s); it is started again", reason)
            self._worker = None
            self.start()

    def runtime(self) -> dict[str, Any]:
        """What this process holds now: subjects queued (not yet taken), dropped at the submit, not judged because of
        the daily limit, and whether the worker runs."""
        return {
            "queued": self._queue.qsize(),
            "dropped": self._dropped,
            "over_cap": self._over_cap,
            "worker_alive": self._worker is not None and not self._worker.done(),
        }

    @property
    def daily_calls(self) -> int:
        """The most requests this judge may send a UTC day (``NOUS_OUTSIDE_JUDGE_DAILY_CALLS`` unless given)."""
        return self._daily_calls

    @property
    def idle(self) -> bool:
        """Nothing queued and nothing being judged."""
        return not self._pending

    @property
    def busy(self) -> bool:
        """A subject is being judged now."""
        return self._inflight is not None and not self._inflight.done()

    # ---- lifecycle -------------------------------------------------------------------------------------------------

    def start(self) -> None:
        """Start the worker (in the running event loop), and the sweep when it is on. A second call does nothing."""
        if self._worker is None and not self._closed:
            self._worker = asyncio.get_running_loop().create_task(self._run(), name="outside-judge")
        if self._sweep_on and self._sweeper is None and not self._closed:
            self._sweeper = asyncio.get_running_loop().create_task(self._sweep_loop(), name="outside-judge-sweep")

    async def close(self, timeout: float | None = None) -> None:
        """Stop taking work, wait up to ``timeout`` (None: this service's ``close_timeout``) for the subject being
        judged, then cancel the rest (a cancelled subject writes nothing). Subjects still queued are dropped."""
        timeout = self._close_timeout if timeout is None else timeout
        self._closed = True
        if self._sweeper is not None:
            self._sweeper.cancel()
        if self._inflight is not None and not self._inflight.done():
            await asyncio.wait({self._inflight}, timeout=timeout)
        for task in (self._inflight, self._worker):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(*(t for t in (self._inflight, self._worker) if t is not None), return_exceptions=True)
        if self._sweeper is not None:
            await asyncio.gather(self._sweeper, return_exceptions=True)

    # ---- the worker ------------------------------------------------------------------------------------------------

    async def _run(self) -> None:
        while True:
            item = await self._queue.get()
            try:
                delay = item.submitted + self._settle - time.monotonic()
                if delay > 0:
                    await asyncio.sleep(delay)
                self._inflight = asyncio.ensure_future(self._judge_one(item))
                await self._inflight
            except asyncio.CancelledError:
                worker = asyncio.current_task()
                if self._closed or (worker is not None and worker.cancelling()):
                    raise
                # The judging alone was cancelled, not by close(): it wrote nothing, and the worker goes on.
                logger.warning("F118: judging %s was cancelled; the worker goes on", item.subject_id)
            except Exception as exc:  # _judge_one catches its own: this is a last guard for the loop
                logger.warning("F118: the judge worker failed on one subject (%s)", type(exc).__name__)
            finally:
                self._inflight = None
                self._pending.discard(item.key)

    async def _judge_one(self, item: _Item) -> None:
        # A report is judged by the report judge (submit_report takes none without one), a decision by the judge.
        judge = self.report_judge if item.kind == TASK_REPORT else self.judge
        if judge is None:
            return
        name, version = judge.name, judge.version
        row: dict[str, Any] = {
            "agent_id": self._agent_id,
            "subject_kind": item.kind,
            "subject_id": item.subject_id,
            "judge_name": name,
            "judge_version": version,
            "against_grader": item.claim.grader,
            "against_grade": item.claim.grade,
            "claim_at": item.resolved_at,
            "evidence": {},
            "latency_ms": None,
            "calls": 0,
        }
        try:
            async with self._database.session() as session:
                if await store.already_judged(session, self._agent_id, item.kind, item.subject_id, name, version):
                    return
                if item.kind == TASK_REPORT:
                    found = await evidence.build_report_evidence(session, self._agent_id, item.subject_id)
                else:
                    found = await evidence.build_decision_evidence(
                        session, self._agent_id, item.subject_id, _shown_claim(item.claim), item.resolved_at
                    )
            if found is None:
                logger.debug("F118: %s %s is gone or not finished as completed; not judged", item.kind, item.subject_id)
                return
            if item.kind == TASK_REPORT:  # the claim as it stood at the task's completion, read from its row
                row.update(against_grader=found.claim.grader, against_grade=found.claim.grade, claim_at=found.claim_at)
            row["evidence"] = found.counts
            if found.record is None:
                skipped = JudgeOutput(status="skipped", verdict="cannot_tell", labels=(found.skip_label,))
                await self._write(row, skipped)
                return
            left = await self._requests_left(name, item.kind)
            if left is None:
                return
            if self._closed:  # no request is started once close has begun; one already sent has close's full wait
                return
            started = time.monotonic()
            row["calls"] = 1  # from here a request may have been sent; the judge's own count replaces this
            # The judge bounds its own call; this outer bound, a little later, stops one that does not.
            outer = call_bound(self._timeout)
            try:
                judging = judge.grade(found.record, timeout=self._timeout, max_calls=left)
                output = await asyncio.wait_for(judging, timeout=outer)
            except TimeoutError:
                model = getattr(judge, "model", None)
                output = failed("timeout", model if isinstance(model, str) else None)
            else:
                row["calls"] = _calls_reported(output)
            row["latency_ms"] = round((time.monotonic() - started) * 1000)
            await self._write(row, _checked(output, item.kind))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("F118: judging %s %s failed (%s)", item.kind, item.subject_id, type(exc).__name__)
            try:
                await self._write(row, failed("error"))
            except Exception as write_exc:
                logger.debug(
                    "F118: the error row for %s was not written (%s)", item.subject_id, type(write_exc).__name__
                )

    async def _requests_left(self, judge_name: str | None = None, kind: str = DECISION) -> int | None:
        """How many requests the judge ``judge_name`` (this service's judge when None) may still send today on a
        subject of ``kind`` (at least one), or None when it may send none. The limit is per judge name: the report
        judge shares the judge's. Reports may use at most half of it (``daily_calls // 2``), so decisions always keep
        the other half; a report past its half is not judged now (the sweep may queue it again on a later pass).
        Fails closed: a count that cannot be read allows none."""
        now = datetime.now(UTC)
        name = judge_name or self.judge.name
        reports_used = 0
        try:
            async with self._database.session() as session:
                used = await store.calls_today(session, self._agent_id, now, judge_name=name)
                if kind == TASK_REPORT:
                    reports_used = await store.calls_today(
                        session, self._agent_id, now, judge_name=name, subject_kind=TASK_REPORT
                    )
        except Exception as exc:
            logger.warning("F118: today's judge calls could not be counted (%s); no call", type(exc).__name__)
            used = None
        if used is not None and used < self._daily_calls:
            left = self._daily_calls - used
            if kind != TASK_REPORT:
                return left
            reports_left = self._daily_calls // 2 - reports_used
            if reports_left > 0:
                return min(left, reports_left)
            self._over_cap += 1  # the reports' half is spent; the rest is the decisions'
            return None
        self._over_cap += 1
        if used is not None and self._cap_logged_on != now.date():
            self._cap_logged_on = now.date()
            logger.info(
                "F118: the judge reached its daily limit of %d calls; no more verdicts today", self._daily_calls
            )
        return None

    # ---- the sweep -------------------------------------------------------------------------------------------------

    def sweep_numbers(self) -> dict[str, Any] | None:
        """What the sweep did in this process, or None when it is off: passes, subjects queued (decisions by grader,
        reports), passes that queued nothing at half the day's limit, passes that failed, and the last pass's time."""
        if not self._sweep_on:
            return None
        counts = self._sweep_counts
        last = counts["last_pass_at"]
        return {
            "passes": counts["passes"],
            "queued_decisions": {k: v for k, v in counts["enqueued"].items() if k != TASK_REPORT},
            "queued_reports": counts["enqueued"].get(TASK_REPORT, 0),
            "held_at_half_limit": counts["held_at_half_limit"],
            "failed": counts["failed"],
            "last_pass_at": None if last is None else last.isoformat(),
        }

    async def _sweep_loop(self) -> None:
        await asyncio.sleep(SWEEP_FIRST_DELAY_SECONDS)
        while not self._closed:
            await self.sweep_once()
            if self._closed:  # a pass cut by close ends the loop, also when its cancellation came back as an error
                return
            await asyncio.sleep(SWEEP_INTERVAL_SECONDS)

    async def sweep_once(self, now: datetime | None = None) -> int:
        """One pass of the sweep; how many subjects it queued. It queues at most ``SWEEP_BATCH`` subjects without a
        final row at this service's judge names and versions (``store.sweep_decisions``, ``store.sweep_reports``, the
        latter only with a report judge): decisions graded by the owner, then what a restart lost (decisions the agent
        resolved in an interactive turn, finished reports), then decisions graded through the review route, then by a
        fixed signal, newest first in each. A decision's claim is its grade as it stands now, at the time it was
        graded. Nothing is queued once today's calls of the judge reach half its daily limit, so the day's own
        subjects keep the other half; nor when that count cannot be read; a subject whose last attempt failed within
        ``store.SWEEP_RETRY_AFTER`` waits. A subject already queued is passed over. It reads ``brain.decisions`` and
        ``heart.subtasks`` and writes neither. Never raises: a failure is one WARNING with the class name, counted (a
        pass cut by ``close`` is neither)."""
        now = now or datetime.now(UTC)
        counts = self._sweep_counts
        counts["passes"] += 1
        counts["last_pass_at"] = now
        if self._closed:
            return 0
        try:
            async with self._database.session() as session:
                used = await store.calls_today(session, self._agent_id, now, judge_name=self.judge.name)
                if used * 2 >= self._daily_calls:
                    counts["held_at_half_limit"] += 1
                    return 0
                room = SWEEP_BATCH + len(self._pending)
                decisions = await store.sweep_decisions(
                    session,
                    self._agent_id,
                    judge_name=self.judge.name,
                    judge_version=self.judge.version,
                    now=now,
                    limit=room,
                )
                reports = []
                if self.report_judge is not None:
                    reports = await store.sweep_reports(
                        session,
                        self._agent_id,
                        judge_name=self.report_judge.name,
                        judge_version=self.report_judge.version,
                        now=now,
                        limit=room,
                    )
            return self._queue_swept(decisions, reports)
        except Exception as exc:
            if self._closed:  # cut by close: not a failure
                return 0
            counts["failed"] += 1
            logger.warning("F118: a sweep pass failed (%s)", type(exc).__name__)
            return 0

    def _queue_swept(self, decisions: list[dict[str, Any]], reports: list[dict[str, Any]]) -> int:
        """Queue up to ``SWEEP_BATCH`` of the subjects a pass found, in the sweep's order; how many were queued."""
        counts = self._sweep_counts
        candidates = [(_sweep_rank(d["grader"]), -d["graded_at"].timestamp(), DECISION, d) for d in decisions]
        candidates += [(_REPORT_RANK, -r["completed_at"].timestamp(), TASK_REPORT, r) for r in reports]
        queued = 0
        for _rank, _newest, kind, found in sorted(candidates, key=lambda c: (c[0], c[1], str(c[3]["id"]))):
            if queued >= SWEEP_BATCH:
                break
            if (kind, found["id"]) in self._pending:
                continue
            if kind == TASK_REPORT:
                taken = self.submit_report(found["id"], claimed_at=found["completed_at"])
                key = TASK_REPORT
            else:
                taken = self.submit_decision(
                    found["id"],
                    grade=found["grade"],
                    note=found["note"],
                    grader=found["grader"],
                    resolved_at=found["graded_at"],
                )
                key = found["grader"]
            if taken:
                queued += 1
                counts["enqueued"][key] = counts["enqueued"].get(key, 0) + 1
        return queued

    async def _write(self, row: dict[str, Any], output: JudgeOutput) -> None:
        values = {
            **row,
            "model": output.model,
            "status": output.status,
            "verdict": output.verdict,
            "confidence": output.confidence,
            "labels": list(output.labels),
            "reason": output.reason,
            "tokens_in": output.tokens_in,
            "tokens_out": output.tokens_out,
        }
        async with self._database.session() as session:
            await store.insert_verdict(session, values)


class JudgeSet:
    """The first judge and, when there is one, a second judge on the same decisions (``NOUS_OUTSIDE_JUDGE_SECOND``),
    each an ``OutsideJudge`` with its own queue, worker, timeout, daily limit and rows. It reads as the first judge
    (``judge``, and ``runtime`` with the second's under the key ``second`` when there is one); ``second`` is the second
    judge's service or None. A service is started before it is put in the set; ``replace_second`` swaps the second
    judge and leaves starting the new one and closing the old one to its caller."""

    def __init__(self, primary: Any, second: Any | None = None) -> None:
        self.primary = primary
        self.second = second

    @property
    def judge(self) -> Judge:
        """The first judge."""
        return self.primary.judge

    def replace_second(self, new: Any | None) -> Any | None:
        """Make ``new`` (a started service, or None) the second judge and return the one it replaces. Starts and
        closes nothing: the caller closes the old one."""
        old, self.second = self.second, new
        return old

    def submit_decision(self, decision_id: Any, *, grade: Any, note: Any, grader: Any, resolved_at: Any) -> bool:
        """Hand the same claim and time to each judge, each on its own (one that raises takes nothing from the
        other). The first judge's answer: True when it queued the decision. Never raises and never waits."""
        claim = {"grade": grade, "note": note, "grader": grader, "resolved_at": resolved_at}
        second = self.second  # read once, before either judge is called
        queued = _handed(self.primary, decision_id, claim)
        if second is not None:
            _handed(second, decision_id, claim)
        return queued

    def runtime(self) -> dict[str, Any]:
        """The first judge's runtime, with the second judge's under ``second`` when there is one."""
        second = self.second
        runtime = self.primary.runtime()
        return runtime if second is None else {**runtime, "second": second.runtime()}

    @property
    def report_judge(self) -> Judge | None:
        """The first judge's report judge (reports go to the first judge)."""
        return getattr(self.primary, "report_judge", None)

    def submit_report(self, subtask_id: Any, *, claimed_at: Any) -> bool:
        """Hand a finished task's report to the first judge, and to the second only with
        ``SECOND_JUDGE_GRADES_REPORTS`` (the second judge grades decisions only for now). The first judge's answer.
        Never raises and never waits."""
        second = self.second  # read once
        queued = _handed_report(self.primary, subtask_id, claimed_at)
        if SECOND_JUDGE_GRADES_REPORTS and second is not None:
            _handed_report(second, subtask_id, claimed_at)
        return queued

    def sweep_numbers(self) -> dict[str, Any] | None:
        """The first judge's sweep numbers (the second judge's are ``second_sweep_numbers``)."""
        return _sweep_numbers_of(self.primary)

    def second_sweep_numbers(self) -> dict[str, Any] | None:
        """The second judge's sweep numbers (its own hourly pass of decisions), or None when there is no second judge
        or its sweep is off."""
        second = self.second  # read once
        return None if second is None else _sweep_numbers_of(second)

    async def close(self, timeout: float | None = None) -> None:
        """Close every judge at once (``OutsideJudge.close``), each waiting ``timeout`` or, when None, its own time;
        one that raises does not keep another open."""
        services = [service for service in (self.primary, self.second) if service is not None]
        closing = await asyncio.gather(
            *(service.close() if timeout is None else service.close(timeout) for service in services),
            return_exceptions=True,
        )
        for result in closing:
            if isinstance(result, Exception):
                logger.warning("F118: a judge did not close cleanly (%s)", type(result).__name__)


def _handed(service: Any, decision_id: Any, claim: dict[str, Any]) -> bool:
    """Submit one claim to one judge's service: True when it queued it. Never raises."""
    try:
        return service.submit_decision(decision_id, **claim) is True
    except Exception as exc:
        logger.warning("F118: a judge did not take a decision (%s)", type(exc).__name__)
        return False


def _handed_report(service: Any, subtask_id: Any, claimed_at: Any) -> bool:
    """Submit one finished task's report to one judge's service: True when it queued it. Never raises."""
    try:
        return service.submit_report(subtask_id, claimed_at=claimed_at) is True
    except Exception as exc:
        logger.warning("F118: a judge did not take a report (%s)", type(exc).__name__)
        return False


def _sweep_numbers_of(service: Any) -> dict[str, Any] | None:
    try:
        return service.sweep_numbers()
    except Exception as exc:
        logger.warning("F118: a judge's sweep numbers were not read (%s)", type(exc).__name__)
        return None
