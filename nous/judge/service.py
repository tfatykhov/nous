"""F118: the outside judge's queue and worker.

``submit_decision`` is called by ``resolve_decisions`` after its reply is built: it fixes the claim (grader, grade,
note, time) and puts it on an in-memory queue, without waiting and without raising. It drops a subject (and counts
it) when the queue is full, the service is closed, or the subject is already queued or being judged. Jobs waiting
when the process stops are not kept.

One worker judges one subject at a time. For each it waits until ``SETTLE_SECONDS`` after the submit (the tool-call
recorder writes a turn's rows in the background), skips a subject whose row at this judge's name and version is final,
builds the evidence (in a thread), and then:

- no evidence, or a record that cannot be rendered: a ``skipped`` row (``cannot_tell``, a label says why), no call;
- otherwise, within the daily limit: one call of the judge under ``asyncio.wait_for`` and one row, ``ok``, ``error``
  or ``timeout``, with the number of model requests it sent. The limit is ``NOUS_OUTSIDE_JUDGE_DAILY_CALLS`` requests
  a UTC day, counted from the table before every call; at the daily limit no further decision is judged that day, and
  when the count cannot be read none is judged either (counted here, logged once a day).

An ``error`` or ``timeout`` row is not final: the next submit of that decision tries again, at most ``MAX_ATTEMPTS``
times in all (``store``). Everything in the worker is caught: one WARNING with the exception's class name, and an
``error`` row when one can be written. A cancellation of one judging that does not come from ``close`` ends that
judging, writes nothing and leaves the worker running; a worker that ended anyway is started again by the next
submit. ``close`` stops taking work, waits ``CLOSE_TIMEOUT_SECONDS`` for the subject in flight, then cancels the
rest. The worker reads the decision and the session's records and writes only ``nous_system.judge_verdicts``.
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
MAX_ATTEMPTS = store.MAX_ATTEMPTS
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
    if kind != DECISION or output.verdict not in DECISION_VERDICTS:
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
    ) -> None:
        self._database = database
        self._agent_id = settings.agent_id
        self._daily_calls = settings.outside_judge_daily_calls
        self.judge = judge
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
    def idle(self) -> bool:
        """Nothing queued and nothing being judged."""
        return not self._pending

    @property
    def busy(self) -> bool:
        """A subject is being judged now."""
        return self._inflight is not None and not self._inflight.done()

    # ---- lifecycle -------------------------------------------------------------------------------------------------

    def start(self) -> None:
        """Start the worker (in the running event loop). A second call does nothing."""
        if self._worker is None and not self._closed:
            self._worker = asyncio.get_running_loop().create_task(self._run(), name="outside-judge")

    async def close(self, timeout: float = CLOSE_TIMEOUT_SECONDS) -> None:
        """Stop taking work, wait up to ``timeout`` for the subject being judged, then cancel the rest (a cancelled
        subject writes nothing). Subjects still queued are dropped."""
        self._closed = True
        if self._inflight is not None and not self._inflight.done():
            await asyncio.wait({self._inflight}, timeout=timeout)
        for task in (self._inflight, self._worker):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(*(t for t in (self._inflight, self._worker) if t is not None), return_exceptions=True)

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
        name, version = self.judge.name, self.judge.version
        row: dict[str, Any] = {
            "agent_id": self._agent_id,
            "subject_kind": item.kind,
            "subject_id": item.subject_id,
            "judge_name": name,
            "judge_version": version,
            "against_grader": item.claim.grader,
            "against_grade": item.claim.grade,
            "evidence": {},
            "latency_ms": None,
            "calls": 0,
        }
        try:
            async with self._database.session() as session:
                if await store.already_judged(session, self._agent_id, item.kind, item.subject_id, name, version):
                    return
                found = await evidence.build_decision_evidence(
                    session, self._agent_id, item.subject_id, item.claim, item.resolved_at
                )
            if found is None:
                logger.debug("F118: decision %s is gone; not judged", item.subject_id)
                return
            row["evidence"] = found.counts
            if found.record is None:
                skipped = JudgeOutput(status="skipped", verdict="cannot_tell", labels=(found.skip_label,))
                await self._write(row, skipped)
                return
            if not await self._within_cap():
                return
            started = time.monotonic()
            row["calls"] = 1  # from here a request may have been sent; the judge's own count replaces this
            # The judge bounds its own call; this outer bound, a little later, stops one that does not.
            outer = self._timeout + min(OUTER_GRACE_SECONDS, self._timeout)
            try:
                output = await asyncio.wait_for(self.judge.grade(found.record, timeout=self._timeout), timeout=outer)
            except TimeoutError:
                model = getattr(self.judge, "model", None)
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

    async def _within_cap(self) -> bool:
        """True when a call may be made now. Fails closed: a count that cannot be read allows none."""
        now = datetime.now(UTC)
        try:
            async with self._database.session() as session:
                used = await store.calls_today(session, self._agent_id, now)
        except Exception as exc:
            logger.warning("F118: today's judge calls could not be counted (%s); no call", type(exc).__name__)
            used = None
        if used is not None and used < self._daily_calls:
            return True
        self._over_cap += 1
        if used is not None and self._cap_logged_on != now.date():
            self._cap_logged_on = now.date()
            logger.info(
                "F118: the judge reached its daily limit of %d calls; no more verdicts today", self._daily_calls
            )
        return False

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
