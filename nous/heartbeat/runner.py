"""Heartbeat runner — background tick loop with triage (F034 + F034.1).

Follows the TaskScheduler start/stop pattern: creates an asyncio.Task
that runs a periodic loop, checking due checks and triaging findings.

F034.1 adds FindingStore integration for dedup, escalation, daily digest,
and outcome tracking.
"""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from collections.abc import Callable
from datetime import UTC, date, datetime
from uuid import uuid4

import httpx

from nous.api.anthropic_client import AnthropicClient
from nous.api.execution_context import ExecutionContext
from nous.api.runner import AgentRunner
from nous.brain import Brain
from nous.config import Settings
from nous.events import Event, EventBus
from nous.heart import Heart
from nous.heartbeat.dynamic import (
    CALLBACK_RETRY_DELAY_SECONDS,
    RUN_OUTCOME,
    DynamicCheck,
    DynamicCheckCancelled,
    DynamicCheckLoader,
)
from nous.heartbeat.finding_store import FindingStore
from nous.heartbeat.registry import BaseCheck, CheckRegistry
from nous.heartbeat.schemas import CheckResult, Finding, FindingAction, HeartbeatResult
from nous.heartbeat.tuner import HeartbeatTuner

logger = logging.getLogger(__name__)

# Audit HB-9: a single escalation step bumps urgency one level, never skipping
# straight to "high". _should_escalate gates the timing per current urgency.
_ESCALATION_LADDER: dict[str, str] = {"low": "normal", "normal": "high", "high": "high"}

# A DAG tick still running after this many dag_tick_timeout periods is treated
# as hung and reported (HeartbeatRunner._escalate_dag_stall). It is a reporting
# threshold, not proof: a tick is as slow as the slowest thing it awaits.
_DAG_STALL_TIMEOUTS = 3

# Telegram rejects a text over 4096 characters (nous/dag/delivery.py cuts at the same length).
_TELEGRAM_MAX_CHARS = 3900


def _cancelled_by_sibling_run(exc: BaseException) -> bool:
    """Whether ``exc`` is a run cancelled because a SIBLING run disabled the check."""
    return isinstance(exc, DynamicCheckCancelled) and exc.by_sibling_run


def _is_final_run(check: BaseCheck, sibling_cancelled: bool, outcome: dict[str, bool]) -> bool:
    """Whether this run is its check's self-disabling final run.

    A run cancelled because another run of the same check disabled it is not
    the final run: the disabling run owns the outcome (codex P2, PR #656).
    A DynamicCheck records the answer itself in ``outcome`` when its run
    ends; the live flag is only a fallback for runs that recorded nothing
    (codex P1, PR #656 round 7).
    """
    if "final_run" in outcome:
        return outcome["final_run"]
    return getattr(check, "_self_disabled", False) is True and not sibling_cancelled


def _await_chain(task: asyncio.Task) -> str:
    """Where a suspended task is waiting, outermost coroutine first.

    Thread stacks cannot show this — a task waiting on an await is on no
    thread — and ``Task.get_stack()`` stops at the outermost coroutine.
    """
    hops: list[str] = []
    awaitable = task.get_coro()
    while awaitable is not None:
        frame = getattr(awaitable, "cr_frame", None) or getattr(awaitable, "gi_frame", None)
        if frame is None:
            break
        hops.append(f"{frame.f_code.co_name} ({frame.f_code.co_filename}:{frame.f_lineno})")
        awaitable = getattr(awaitable, "cr_await", None) or getattr(awaitable, "gi_yieldfrom", None)
    return " > ".join(hops) or "unknown"


def _cancel_requested() -> bool:
    """Whether the running task itself is being cancelled (stop(), event-loop
    teardown). False when a CancelledError only came out of something the
    task awaited: that is the awaited thing's failure, not a request to stop.
    """
    task = asyncio.current_task()
    return task is None or task.cancelling() > 0


class HeartbeatRunner:
    """Background heartbeat loop with check execution and triage.

    Runs due checks on each tick, collects findings, and either
    sends Telegram notifications (high urgency) or opens a cognitive
    session (normal urgency) for the agent to process.
    """

    def __init__(
        self,
        settings: Settings,
        registry: CheckRegistry,
        runner: AgentRunner,
        brain: Brain,
        heart: Heart,
        bus: EventBus | None,
        http_client: httpx.AsyncClient | None,
        finding_store: FindingStore | None = None,
        api_client: AnthropicClient | None = None,
        dynamic_loader: DynamicCheckLoader | None = None,
    ) -> None:
        self._settings = settings
        self._registry = registry
        self._runner = runner
        self._brain = brain
        self._heart = heart
        self._bus = bus
        self._http = http_client
        self._finding_store = finding_store
        self._api_client = api_client
        self._dynamic_loader = dynamic_loader
        self._dedicated_runner: AgentRunner | None = None

        self.dag_orchestrator: object | None = None  # F038: injected by main.py

        self._task: asyncio.Task | None = None
        self._dag_task: asyncio.Task | None = None  # fix/dag-tick-own-loop
        self._dag_tick_lock: asyncio.Lock = asyncio.Lock()
        # Tracks a tick that timed out but is still running in background.
        # The next iteration checks this to maintain single-flight even after
        # the lock has been released by the timeout path.
        self._dag_pending_task: asyncio.Task | None = None
        # When the tick in _dag_pending_task started. Never cleared: readers
        # go through dag_tick_pending_since, which is None once it finishes.
        self._dag_pending_since: datetime | None = None
        # The same start on the event loop's monotonic clock — the stall
        # clock, so a wall-clock step can never look like a hung tick — and
        # whether that tick has been escalated already.
        self._dag_pending_started: float = 0.0
        self._dag_stall_escalated = False
        # What to do about a DAG tick treated as hung, besides reporting it.
        # Injected from outside, like dag_orchestrator above; None = report only.
        self.dag_stall_action: Callable[[str], None] | None = None
        # Absolute loop.time() deadline set when _dag_loop begins draining an
        # in-flight tick during shutdown.  stop() reads this so the two drain
        # windows share one total budget instead of each taking dag_tick_timeout.
        self._dag_shutdown_drain_deadline: float | None = None
        self._running = False
        self._tick_count: int = 0
        self._tokens_used_today: int = 0
        self._budget_date: date = date.today()
        self._last_tick: datetime | None = None
        self._last_dag_tick: datetime | None = None  # fix/dag-tick-own-loop
        self._last_digest_date: date | None = None
        self._last_prune: datetime | None = None
        self._tuner: HeartbeatTuner = HeartbeatTuner(
            min_samples=getattr(settings, "heartbeat_tuning_min_samples", None),
        )
        self._last_tune: datetime | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the heartbeat loop."""
        self._running = True

        # Create dedicated runner with isolated API client for triage
        if self._api_client is not None:
            self._dedicated_runner = self._runner.fork(self._api_client)
            logger.info("F034: Heartbeat using dedicated API client (isolated connection pool)")

        # F034.5: Wire dynamic check loader to dedicated runner and do initial sync
        if self._dynamic_loader is not None:
            runner_for_checks = self._dedicated_runner or self._runner
            self._dynamic_loader.set_runner(runner_for_checks)
            try:
                count = await self._dynamic_loader.sync()
                logger.info("F034.5: Initial dynamic check sync loaded %d checks", count)
            except Exception:
                logger.exception("F034.5: Initial dynamic check sync failed")

        await self._detect_missed_checks()
        self._task = asyncio.create_task(self._loop(), name="heartbeat-runner")
        self._dag_task = asyncio.create_task(self._dag_loop(), name="dag-tick-loop")
        logger.info(
            "F034: Heartbeat started (tick=%ds, quiet=%d-%d, budget=%d tokens/day)",
            self._settings.heartbeat_tick_interval,
            self._settings.heartbeat_quiet_start,
            self._settings.heartbeat_quiet_end,
            self._settings.heartbeat_daily_token_budget,
        )
        logger.info(
            "F038: DAG tick loop started (interval=%ds, timeout=%ds)",
            self._settings.dag_tick_interval,
            self._settings.dag_tick_timeout,
        )

    async def stop(self) -> None:
        """Stop the heartbeat loop and DAG tick loop."""
        self._running = False
        # Fix 4 (Codex P2): cancel the heartbeat check loop FIRST so it cannot
        # wake from sleep and launch new tool-using checks (with external side
        # effects) while DAG work is still draining.
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self._dag_task:
            self._dag_task.cancel()
            try:
                await self._dag_task
            except asyncio.CancelledError:
                pass
            self._dag_task = None
        # Drain any tick that timed out and is still running in background.
        # stop() must return only after this task is finished because
        # shutdown_components() closes the DB and subtask pool immediately
        # after — an in-flight tick would otherwise write to a closed pool.
        # Always clear the reference so stop() never leaves it pointing at a
        # finished (or live) task.
        # Fix 2 (Codex P1): use asyncio.wait (not asyncio.wait_for) so the
        # pending task is never cancelled on timeout.  Cancelling a shielded
        # tick reintroduces the unsafe CancelledError window between primitive
        # creation and the node's running transition.
        _pending = self._dag_pending_task
        self._dag_pending_task = None
        if _pending is not None:
            if _pending.done():
                # Fix 6 (Codex P2 round-5): task completed while _dag_loop
                # slept between iterations — harvest its result so
                # last_dag_tick reflects a successful tick and any exception
                # is routed through the structured failure log rather than
                # silently dropped as an unhandled task exception.
                self._record_dag_tick(_pending, "F038: DAG pending tick raised (already completed at shutdown)")
            else:
                # Fix 8 (Codex P1 round-6): honour any budget already spent
                # by _dag_loop's own shutdown drain.  If _dag_loop set a
                # deadline before timing out, use the remaining seconds so the
                # combined wait never exceeds one dag_tick_timeout.
                loop = asyncio.get_running_loop()
                if self._dag_shutdown_drain_deadline is not None:
                    drain_timeout = max(0.0, self._dag_shutdown_drain_deadline - loop.time())
                else:
                    drain_timeout = float(self._settings.dag_tick_timeout)
                logger.warning(
                    "F038: Draining timed-out DAG tick during shutdown (waiting up to %.1fs)",
                    drain_timeout,
                )
                done, _ = await asyncio.wait(
                    {_pending},
                    timeout=drain_timeout,
                )
                if not done:
                    # Fix 7 (Codex P1 round-5): cancel the task so it cannot
                    # access the DB or subtask pool after shutdown_components()
                    # closes them.  We have already waited a full dag_tick_timeout
                    # for a graceful finish; cancellation is the lesser evil
                    # compared to writing to a closed pool.  The round-3
                    # "do-not-cancel" constraint applied only to the _dag_loop
                    # CancelledError path where the tick had just started and
                    # the primitive/node-transition window was live.
                    logger.error(
                        "F038: Timed-out DAG tick did not complete within %ds "
                        "during shutdown — cancelling to prevent DB access "
                        "after resource teardown",
                        self._settings.dag_tick_timeout,
                    )
                    _pending.cancel()
                    try:
                        await _pending
                    except (asyncio.CancelledError, Exception):
                        pass
                else:
                    self._record_dag_tick(_pending, "F038: Timed-out DAG tick raised during shutdown drain")

        # Clean up dedicated runner and its API client
        if self._dedicated_runner is not None:
            try:
                await self._dedicated_runner.close()
            except Exception:
                logger.warning("F034: Error closing dedicated runner", exc_info=True)
            finally:
                self._dedicated_runner = None
        if self._api_client is not None:
            try:
                await self._api_client.close()
            except Exception:
                logger.warning("F034: Error closing heartbeat API client", exc_info=True)
            finally:
                self._api_client = None

        logger.info("F034: Heartbeat stopped")

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def _loop(self) -> None:
        """Main tick loop — runs until cancelled."""
        while self._running:
            try:
                await asyncio.sleep(self._settings.heartbeat_tick_interval)
                self._maybe_reset_budget()

                if self._in_quiet_hours():
                    # Still run urgent-override checks during quiet hours
                    await self._tick(urgent_only=True)
                else:
                    await self._tick()

                # F034.5: Periodic dynamic check sync
                if (
                    self._dynamic_loader is not None
                    and self._settings.heartbeat_dynamic_sync_ticks > 0
                    and self._tick_count % self._settings.heartbeat_dynamic_sync_ticks == 0
                ):
                    try:
                        await self._dynamic_loader.sync()
                    except Exception:
                        logger.exception("F034.5: Dynamic check sync failed")

                # F034.1: Daily digest at UTC hour 9
                await self._maybe_send_digest()

                # F034.1: Periodic prune + sweep (every 24h)
                await self._maybe_prune_and_sweep()

                # F034.3 / Audit HB-3: scheduled self-tuning pass
                await self._maybe_tune()

            except asyncio.CancelledError:
                if _cancel_requested():
                    break
                logger.exception("Heartbeat tick was cancelled from within — the loop continues")
            except Exception:
                logger.exception("Heartbeat tick failed")

    async def _dag_loop(self) -> None:
        """Independent DAG orchestrator tick loop — runs until cancelled.

        Decoupled from the heartbeat check loop (fix/dag-tick-own-loop) so
        slow or hung heartbeat checks cannot block DAG progress (completion
        polling, wave launching, result delivery). Runs during quiet hours
        because DAG work is not subject to the heartbeat token budget.

        Single-flight: if the previous tick is still running when the next
        interval fires, the new tick is SKIPPED with a WARNING. A per-tick
        soft deadline logs when a tick exceeds the budget; the tick itself
        is shielded from cancellation so that CancelledError cannot land
        between a primitive-creation commit and the node's running
        transition (Codex P1: untracked subtask / duplicate launch).

        A tick still pending after its deadline is reported once it has been
        pending for _DAG_STALL_TIMEOUTS x dag_tick_timeout: on the way through
        the sleep between two passes (_sleep_one_dag_interval), or by the pass
        on which that moment falls. The report never brings a pass forward.
        """
        while self._running:
            try:
                await self._sleep_one_dag_interval()
                if self.dag_orchestrator is None:
                    continue

                if self._dag_tick_lock.locked():
                    logger.warning(
                        "F038: DAG tick skipped — previous tick still running (interval=%ds, timeout=%ds)",
                        self._settings.dag_tick_interval,
                        self._settings.dag_tick_timeout,
                    )
                    continue

                # A timed-out tick keeps running in background after its
                # wait_for deadline fires. Single-flight is maintained by
                # checking the pending-task reference (the lock was released
                # when the timeout path exited `async with`).
                if self._dag_pending_task is not None and not self._dag_pending_task.done():
                    pending_for = asyncio.get_running_loop().time() - self._dag_pending_started
                    if (
                        not self._dag_stall_escalated
                        and pending_for >= _DAG_STALL_TIMEOUTS * self._settings.dag_tick_timeout
                    ):
                        self._dag_stall_escalated = True
                        await self._escalate_dag_stall(pending_for)
                    # Look again before saying so: the tick can return while
                    # its report is being sent. The next pass harvests it.
                    if not self._dag_pending_task.done():
                        logger.warning(
                            "F038: DAG tick skipped — previous tick timed out and is still running in background",
                        )
                    continue
                # Fix 3 (Codex P2): harvest the result of a completed pending
                # task so that (a) last_dag_tick reflects its success and
                # (b) any exception is logged via the structured failure path
                # rather than emitted as an unhandled task exception.
                if self._dag_pending_task is not None:  # done — consume it
                    self._record_dag_tick(self._dag_pending_task, "F038: DAG tick raised after timing out")
                self._dag_pending_task = None  # clear any completed reference

                async with self._dag_tick_lock:
                    inner_task: asyncio.Task = asyncio.create_task(self.dag_orchestrator.tick())
                    # Track for post-timeout single-flight (see check above).
                    self._dag_pending_task = inner_task
                    self._dag_pending_since = datetime.now(UTC)
                    self._dag_pending_started = asyncio.get_running_loop().time()
                    self._dag_stall_escalated = False
                    try:
                        # Enforce the configured deadline. asyncio.wait_for
                        # cancels only the shield wrapper on timeout — the
                        # inner_task itself is NOT cancelled (shield protects
                        # it), so in-flight DB writes and subtask launches
                        # complete safely. The outer CancelledError path
                        # (shutdown) drains inner_task before propagating.
                        await asyncio.wait_for(
                            asyncio.shield(inner_task),
                            timeout=self._settings.dag_tick_timeout,
                        )
                    except TimeoutError:
                        if inner_task.done():
                            # Not the deadline. wait_for passes through what
                            # the tick itself raised, and a builtin
                            # TimeoutError from inside it (a connect timeout,
                            # socket.timeout) has the deadline's type.
                            self._record_dag_tick(inner_task, "F038: DAG orchestrator tick failed")
                            self._dag_pending_task = None
                            continue
                        logger.error(
                            "F038: DAG orchestrator tick timed out after %ds — "
                            "tick is still running in background; "
                            "subsequent ticks will skip until it completes",
                            self._settings.dag_tick_timeout,
                        )
                        # inner_task continues; _dag_pending_task keeps the
                        # reference so the single-flight check above prevents
                        # a new tick from starting while it is still running.
                    except asyncio.CancelledError:
                        if inner_task.cancelled() and not _cancel_requested():
                            # The tick itself finished cancelled (something
                            # it awaited was cancelled elsewhere). Nobody
                            # asked this loop to stop: a failed tick.
                            logger.error("F038: DAG orchestrator tick was cancelled from within — a failed tick")
                            self._dag_pending_task = None
                            continue
                        # Outer task was cancelled. inner_task is STILL
                        # RUNNING — drain it with a bounded deadline so
                        # shutdown cannot hang indefinitely on a hung DB or
                        # network call (Fix 1, Codex P1 round-3).
                        #
                        # Fix 8 (Codex P1 round-6): record an absolute
                        # deadline so stop()'s second drain uses the
                        # REMAINING budget rather than a fresh full timeout.
                        # Without this the two drain windows stack and the
                        # total wait can reach 2 × dag_tick_timeout.
                        loop = asyncio.get_running_loop()
                        self._dag_shutdown_drain_deadline = loop.time() + self._settings.dag_tick_timeout
                        _shutdown_drain_timed_out = False
                        try:
                            await asyncio.wait_for(
                                asyncio.shield(inner_task),
                                timeout=self._settings.dag_tick_timeout,
                            )
                            self._last_dag_tick = datetime.now(UTC)
                        except TimeoutError:
                            if inner_task.done():
                                # The tick finished: the TimeoutError is its
                                # own, as in the deadline handler above.
                                self._record_dag_tick(
                                    inner_task, "F038: DAG orchestrator tick failed during shutdown drain"
                                )
                            else:
                                logger.warning(
                                    "F038: In-flight DAG tick did not finish within "
                                    "%ds during shutdown drain — stop() will drain it",
                                    self._settings.dag_tick_timeout,
                                )
                                # Fix 5 (Codex P1 round-4): do NOT clear
                                # _dag_pending_task when the drain times out.
                                # inner_task is still running; stop() reads the
                                # reference and drains it via asyncio.wait before
                                # shutdown_components() closes the DB pool.
                                _shutdown_drain_timed_out = True
                        except Exception:
                            logger.exception("F038: DAG orchestrator tick failed during shutdown drain")
                        if not _shutdown_drain_timed_out:
                            self._dag_pending_task = None
                        raise
                    except Exception:
                        logger.exception("F038: DAG orchestrator tick failed")
                        self._dag_pending_task = None
                    else:
                        self._last_dag_tick = datetime.now(UTC)
                        self._dag_pending_task = None

            except asyncio.CancelledError:
                if _cancel_requested():
                    break
                logger.exception("F038: DAG tick loop iteration was cancelled from within — the loop continues")
            except Exception:
                logger.exception("F038: DAG tick loop iteration failed")

    async def _sleep_one_dag_interval(self) -> None:
        """Sleep one tick interval. On the way, report a pending tick that reaches its stall threshold.

        The sleep is split in two only for a pending tick, not reported yet,
        that reaches _DAG_STALL_TIMEOUTS x dag_tick_timeout before the interval
        is over. Both parts together are one interval, so the next pass, and
        with it the next tick, is never brought forward by a report.
        """
        interval = self._settings.dag_tick_interval
        pending = self._dag_pending_task
        if pending is not None and not pending.done() and not self._dag_stall_escalated:
            loop = asyncio.get_running_loop()
            report_at = self._dag_pending_started + _DAG_STALL_TIMEOUTS * self._settings.dag_tick_timeout
            until_report = max(report_at - loop.time(), 0.0)
            if until_report < interval:
                await asyncio.sleep(until_report)
                # The clock is not read again: a timer may fire a clock tick
                # early, and the tick is due now unless it has returned.
                if not pending.done():
                    self._dag_stall_escalated = True
                    await self._escalate_dag_stall(loop.time() - self._dag_pending_started)
                interval -= until_report
        await asyncio.sleep(interval)

    async def _escalate_dag_stall(self, pending_for: float) -> None:
        """The pending DAG tick is treated as hung: say so, tell a person, then run the stall action, if any.

        Every later tick is skipped behind it, and nothing but stop() ever
        cancels it (cancelling a shielded tick mid-launch is unsafe), so no
        DAG advances again until it returns or the process restarts. The log
        line carries the tick's await chain because nothing else can show
        where it is waiting.
        """
        try:
            waiting_at = _await_chain(self._dag_pending_task)
        except Exception:  # it reads other libraries' coroutine objects
            logger.exception("F038: could not read where the pending DAG tick is waiting")
            waiting_at = "unknown"
        reason = (
            f"F038: DAG tick has not returned after {pending_for:.0f}s "
            f"({_DAG_STALL_TIMEOUTS} x dag_tick_timeout={self._settings.dag_tick_timeout}s); "
            f"every tick since it started at {self._dag_pending_since} was skipped. "
            f"Waiting at: {waiting_at}"
        )
        logger.critical("%s", reason)
        # Nothing reads a CRITICAL line, so a person is told as well. The
        # sender returns at once when Telegram is not configured, and it only
        # logs a send that fails or that Telegram rejects; the line above has
        # the text in full.
        await self._send_telegram(f"[Heartbeat] {reason}"[:_TELEGRAM_MAX_CHARS])
        if self.dag_stall_action is not None:
            self.dag_stall_action(reason)

    def _record_dag_tick(self, task: asyncio.Task, failure_message: str) -> None:
        """Record a finished DAG tick: a success advances last_dag_tick,
        anything else is logged. Never raises for a finished task —
        ``task.result()`` would re-raise a cancelled tick's CancelledError
        into the caller, past every ``except Exception``.
        """
        failure = asyncio.CancelledError() if task.cancelled() else task.exception()
        if failure is None:
            self._last_dag_tick = datetime.now(UTC)
        else:
            logger.error(failure_message, exc_info=failure)

    async def _record_run_stats(
        self,
        check: BaseCheck,
        *,
        success: bool,
        error_msg: str | None = None,
    ) -> None:
        """Single choke point for persisting a check's run outcome to the DB.

        Issue #590: DynamicCheckLoader.update_run_stats's caller-side
        try/except was duplicated across five call sites (two _tick
        branches, trigger_check's two branches) with inconsistent
        handling — two of the five (_tick's timeout and exception
        branches) were a bare `pass`, dropping a FAILED run record with
        no log line at all. A dropped failure record is worse than a
        dropped success record: it inflates apparent success rates,
        which is exactly what F034.3's self-tuning consumes. Root cause
        of the family (this method exists to close): a durable record of
        check execution was written best-effort, and its loss was
        invisible to every caller.

        Never raises into the caller — a stats-write failure must not
        break check execution, abort the tick loop, or (for
        trigger_check) change the caller's own exception contract.

        NO RETRY, deliberately (codex P2 on the original version of this
        fix, which retried once). update_run_stats writes a RELATIVE
        increment (`run_count = run_count + 1`), not an absolute value —
        if the UPDATE commits on the server but the client never learns
        that (e.g. the connection drops after commit, before the
        acknowledgement arrives), we cannot tell "the write never
        landed" from "the write landed and we just didn't hear back". A
        retry in the second case double-counts a single execution,
        silently corrupting the exact two consumers this fix protects:
        F034.3's self-tuner and #589's `run_count - error_count` gate.
        That is a worse failure than the one #590 filed — #590's ask is
        visibility, and one ERROR-logged attempt already delivers it.
        Considered and rejected: retrying only on
        sqlalchemy.exc.TimeoutError (pool-checkout timeout — provably
        never reached the server, so provably safe) is real, but covers
        only pool exhaustion. The likelier failure for a live connection
        mid-UPDATE — a dropped connection — is exactly the ambiguous
        case with no safe classification, so a retry that skips the
        common case in exchange for a "which exceptions are safe" list
        that breaks quietly on a driver upgrade is a bad trade. Making
        the write itself idempotent (an execution-scoped dedup key) would
        close this properly, but needs a schema change — proportionate
        only if a real double-count is ever observed, not for a defect
        whose filed symptom is a missing log line.

        Escalates to ERROR with exc_info on any failure, naming the
        check and which record was lost. The point of #590 is that a
        silent drop leaves no consumer able to distinguish "did not run"
        from "ran, and we failed to record it" — F034.3's self-tuning,
        /heartbeat/status, the dashboard, and #589's completion gate all
        read this table and need that distinction.
        """
        if not isinstance(check, DynamicCheck) or self._dynamic_loader is None:
            return
        try:
            await self._dynamic_loader.update_run_stats(
                check.check_id,
                success=success,
                error_msg=error_msg,
            )
        except Exception as exc:
            outcome = "success" if success else "failure"
            logger.error(
                "F034.5/#590: failed to record %s run stats for check "
                "'%s' — this run's record is LOST (downstream consumers "
                "cannot distinguish this from the check never having run)",
                outcome,
                check.name,
                exc_info=exc,
            )

    async def _tick(self, urgent_only: bool = False) -> list[Finding]:
        """Run due checks and triage findings."""
        self._tick_count += 1
        now = datetime.now(UTC)
        due_checks = self._registry.get_due_checks(now)

        if urgent_only:
            due_checks = [c for c in due_checks if c.urgent_override]

        if not due_checks:
            return []

        logger.info(
            "Heartbeat tick: running %d check(s) — %s",
            len(due_checks),
            ", ".join(c.name for c in due_checks),
        )

        all_findings: list[Finding] = []
        successful_checks: set[str] = set()
        current_fingerprints: dict[str, set[str]] = {}

        # #273: Fire on_complete callbacks for self-disabled dynamic checks
        callback_candidates: list[DynamicCheck] = []

        for check in due_checks:
            # Codex P1: a DAG task can reap/cancel a DAG-managed dynamic
            # check between the snapshot (get_due_checks) and here.  Re-
            # verify the check is still registered and active before
            # starting an LLM turn that would run its tools on a cancelled
            # node.
            if isinstance(check, DynamicCheck):
                live = self._registry.get_check(check.name)
                if (
                    live is None
                    or not live.active
                    or live._self_disabled
                    or self._registry.is_fenced(check.name)
                ):
                    logger.info(
                        "Heartbeat check '%s' was unregistered or disabled after snapshot — skipping",
                        check.name,
                    )
                    continue

            # Bracket the run so the DAG loop cannot read a mid-run
            # self-disable (manage_check unregisters the check before run()
            # returns) as node completion. end_run fires in the finally,
            # after the run's stats are recorded. No await separates this
            # from the live re-check above. The outcome defaults to FAILED so
            # an exit path that records nothing (a cancellation from stop(),
            # any BaseException) can never read as success to the DAG loop;
            # only a recorded success or an explicit skip overrides it.
            self._registry.begin_run(check.name)
            run_succeeded: bool | None = False
            sibling_cancelled = False
            outcome: dict[str, bool] = {}
            outcome_token = RUN_OUTCOME.set(outcome)
            try:
                result: CheckResult = await asyncio.wait_for(
                    check.run(),
                    timeout=check.timeout,
                )
                # A skipped result means run() returned early because the
                # check was disabled at the execution boundary (race between
                # _tick's snapshot and the first await in run()). Do not
                # record stats or findings — no LLM turn ran.
                if result.skipped:
                    logger.debug(
                        "Heartbeat check '%s' skipped at execution boundary (disabled concurrently)",
                        check.name,
                    )
                    run_succeeded = None
                    continue
                check.mark_success()
                successful_checks.add(check.name)
                run_succeeded = True

                # F034.5: Track token usage from dynamic checks
                if result.tokens_used:
                    self._tokens_used_today += result.tokens_used

                # F034.5: Update run stats in DB for dynamic checks
                await self._record_run_stats(check, success=True)

                # #273: Collect self-disabled checks with callbacks
                if isinstance(check, DynamicCheck) and result.self_disabled and check.on_complete_prompt:
                    callback_candidates.append(check)

                if result.has_updates:
                    for f in result.findings:
                        f.check_name = check.name
                    all_findings.extend(result.findings)
                    for f in result.findings:
                        current_fingerprints.setdefault(check.name, set()).add(f.fingerprint())

            except TimeoutError:
                check.mark_failure()
                logger.warning("Heartbeat check '%s' timed out", check.name)
                # F034.5: Record timeout as error for dynamic checks
                await self._record_run_stats(check, success=False, error_msg="timeout")
                run_succeeded = False
            except asyncio.CancelledError:
                if _cancel_requested():
                    raise
                # Something awaited for this check was cancelled elsewhere.
                # Nobody asked this loop to stop: a failed run of this check.
                check.mark_failure()
                # The arm also covers the stats write that follows a successful run.
                successful_checks.discard(check.name)
                if run_succeeded:
                    # It was that write. Whether its record landed is unknown,
                    # and the write is a relative increment: one run gets no
                    # second write (see NO RETRY in _record_run_stats).
                    logger.error(
                        "Heartbeat check '%s': the write of its success stats was cancelled from within — "
                        "the record may or may not have landed and is not written again",
                        check.name,
                    )
                else:
                    logger.error("Heartbeat check '%s' was cancelled from within — a failed run", check.name)
                    await self._record_run_stats(check, success=False, error_msg="cancelled")
                run_succeeded = False
            except Exception as exc:
                check.mark_failure()
                logger.exception("Heartbeat check '%s' failed", check.name)
                sibling_cancelled = _cancelled_by_sibling_run(exc)
                # F034.5: Record error for dynamic checks
                await self._record_run_stats(check, success=False, error_msg=str(exc)[:200])
                run_succeeded = False
            finally:
                RUN_OUTCOME.reset(outcome_token)
                self._registry.end_run(
                    check.name,
                    run_succeeded,
                    self_disabled=_is_final_run(check, sibling_cancelled, outcome),
                )

        # #273: Fire callbacks as background tasks (non-blocking)
        for cb_check in callback_candidates:
            if self._has_budget():
                asyncio.create_task(
                    self._execute_callback(cb_check),
                    name=f"callback-{cb_check.name}",
                )
            else:
                logger.warning(
                    "#273: Skipping callback for '%s' — token budget exhausted",
                    cb_check.name,
                )

        self._last_tick = now

        # Auto-resolve findings no longer reported by successful checks
        self._auto_resolve_absent_findings(successful_checks, current_fingerprints)

        if all_findings:
            logger.info(
                "Heartbeat found %d finding(s): %s",
                len(all_findings),
                "; ".join(f"[{f.urgency}] {f.summary[:60]}" for f in all_findings),
            )
            try:
                logger.info("Heartbeat triage starting for %d findings", len(all_findings))
                await self._triage(all_findings)
                logger.info("Heartbeat triage completed")
            except Exception:
                logger.exception("Heartbeat triage crashed")

            # Emit event for audit trail
            if self._bus:
                tick_event = Event(
                    type="heartbeat_tick",
                    agent_id=self._settings.agent_id,
                    data={
                        "findings_count": len(all_findings),
                        "checks_run": len(due_checks),
                        "tokens_used_today": self._tokens_used_today,
                        "findings": [
                            {
                                "source": f.source,
                                "summary": f.summary,
                                "urgency": f.urgency,
                                "check_name": f.check_name,
                            }
                            for f in all_findings
                        ],
                        "by_source": dict(Counter(f.source for f in all_findings)),
                        "by_urgency": dict(Counter(f.urgency for f in all_findings)),
                    },
                )
                tick_event.trace_id = tick_event.event_id  # Root event
                self._current_tick_event = tick_event
                await self._bus.emit(tick_event)

        return all_findings

    # ------------------------------------------------------------------
    # Auto-resolve absent findings
    # ------------------------------------------------------------------

    def _auto_resolve_absent_findings(
        self,
        successful_checks: set[str],
        current_fingerprints: dict[str, set[str]],
        threshold: int = 2,
    ) -> None:
        """Auto-resolve ACKNOWLEDGED findings no longer reported by successful checks.

        For each check that ran successfully this tick, compare its current
        findings against tracked findings. Mark absent findings, then resolve
        any ACKNOWLEDGED findings absent for >= threshold consecutive ticks.
        """
        if self._finding_store is None:
            return

        # Phase 1: Mark absent findings for all successful checks
        for check_name in successful_checks:
            active_fps = self._finding_store.get_active_by_check(check_name)
            current_fps = current_fingerprints.get(check_name, set())
            absent_fps = active_fps - current_fps

            for fp in absent_fps:
                self._finding_store.mark_absent_tick(fp)

        # Phase 2: Single pass — resolve ACKNOWLEDGED findings past threshold
        resolvable = self._finding_store.get_auto_resolvable(threshold=threshold)
        for fp in resolvable:
            # Only resolve if the finding's check ran successfully this tick
            tracked = self._finding_store.get_tracked(fp)
            if tracked and tracked.finding.check_name in successful_checks:
                self._finding_store.resolve(fp)
                logger.info("Auto-resolved finding %s (absent for %d+ ticks)", fp, threshold)

    # ------------------------------------------------------------------
    # Triage
    # ------------------------------------------------------------------

    async def _triage(self, findings: list[Finding]) -> None:
        """Sort findings by urgency and dispatch appropriately.

        F034.1: When FindingStore is present, each finding is routed through
        the store's state machine first. SUPPRESS -> skip, ESCALATE -> upgrade
        urgency to high, TRIAGE -> proceed normally.
        """
        # F034.1: Route through FindingStore if available
        if self._finding_store is not None:
            routed_findings: list[Finding] = []
            time_escalated_checks: set[str] = set()

            for f in findings:
                action = self._finding_store.ingest(f)
                fp = f.fingerprint()

                if action == FindingAction.SUPPRESS:
                    logger.debug("F034.1: Suppressed finding %s: %s", fp, f.summary[:60])
                    continue
                elif action == FindingAction.ESCALATE:
                    # Audit HB-9 (2026-06-09): bump ONE ladder step
                    # (low->normal->high), not straight to "high". _should_escalate
                    # already gates the timing per current urgency (low waits
                    # low_to_normal_hours, normal waits normal_to_high_hours), so
                    # forcing "high" made aged low findings page as urgent,
                    # collapsing the documented ladder.
                    new_urgency = _ESCALATION_LADDER.get(f.urgency, "high")
                    logger.info(
                        "F034.1: Escalating finding %s (%s -> %s): %s",
                        fp,
                        f.urgency,
                        new_urgency,
                        f.summary[:60],
                    )
                    f.urgency = new_urgency
                    time_escalated_checks.add(f.check_name)
                    routed_findings.append(f)
                else:  # TRIAGE
                    routed_findings.append(f)

                # Acknowledge after routing to triage/escalate
                self._finding_store.acknowledge(fp)

            # F034.1: Accumulation escalation per check_name
            # (mutually exclusive with time-based escalation within a tick)
            check_names_seen = {f.check_name for f in findings}
            for check_name in check_names_seen:
                if check_name in time_escalated_checks:
                    continue  # already time-escalated, skip accumulation
                if self._finding_store.check_accumulation_escalation(check_name):
                    logger.info(
                        "F034.1: Accumulation escalation for check '%s'",
                        check_name,
                    )
                    # Send accumulation alert via Telegram
                    ack_items = [
                        t for t in self._finding_store.get_digest_items() if t.finding.check_name == check_name
                    ]
                    if ack_items:
                        lines = [f"[Heartbeat] Accumulation alert: {check_name} ({len(ack_items)} findings)"]
                        for item in ack_items[:10]:  # cap at 10 in message
                            lines.append(f"- {item.finding.summary[:80]}")
                        await self._send_telegram("\n".join(lines))

            findings = routed_findings

        if not findings:
            logger.debug("Heartbeat triage: all findings suppressed by FindingStore")
            return

        # Sort: high first, then normal, then low
        urgency_order = {"high": 0, "normal": 1, "low": 2}
        findings.sort(key=lambda f: urgency_order.get(f.urgency, 1))

        high_findings = [f for f in findings if f.urgency == "high"]

        # High urgency: immediate Telegram notification
        if high_findings:
            lines = ["[Heartbeat] Urgent findings:"]
            for f in high_findings:
                lines.append(f"- [{f.source}] {f.summary}")
            await self._send_telegram("\n".join(lines))

        # Normal+ findings: cognitive triage if budget allows
        actionable = [f for f in findings if f.needs_action]
        logger.info(
            "Heartbeat triage: %d routed, %d actionable, budget=%s",
            len(findings),
            len(actionable),
            "ok" if self._has_budget() else "exhausted",
        )
        if actionable and self._has_budget():
            await self._cognitive_triage(actionable)
        elif actionable and not self._has_budget():
            logger.warning(
                "Heartbeat budget exhausted (%d/%d tokens) — %d actionable finding(s) not triaged",
                self._tokens_used_today,
                self._settings.heartbeat_daily_token_budget,
                len(actionable),
            )

    def _get_triage_runner(self) -> AgentRunner:
        """Return the runner to use for cognitive triage.

        If a dedicated api_client was provided and the dedicated runner was
        initialized in start(), returns it. Otherwise falls back to the
        shared runner with a warning if api_client was set but start()
        didn't complete.
        """
        if self._api_client is not None:
            if self._dedicated_runner is not None:
                return self._dedicated_runner
            logger.warning(
                "F034: api_client provided but dedicated runner not initialized "
                "(was start() called?); falling back to shared runner"
            )
        return self._runner

    async def _cognitive_triage(self, findings: list[Finding]) -> HeartbeatResult:
        """Open a cognitive session to process findings."""
        result = HeartbeatResult()

        # Build a message summarizing findings
        lines = ["[Heartbeat] The following items need attention:"]
        for f in findings:
            lines.append(f"- [{f.source}] {f.summary}")
        lines.append("\nPlease review these findings and take any needed actions.")
        message = "\n".join(lines)

        session_id = f"heartbeat-{uuid4().hex[:8]}"
        triage_runner = self._get_triage_runner()

        try:
            heartbeat_model = self._settings.heartbeat_model or self._settings.background_model
            response_text, _context, usage = await triage_runner.run_turn(
                session_id,
                message,
                platform="heartbeat",
                skip_episode=True,
                is_subtask=True,
                model_override=heartbeat_model,
                is_background=True,
                context=ExecutionContext(kind="heartbeat_triage", session_id=session_id),
            )
            result.response = response_text or ""
            result.tokens_used = (usage or {}).get("input_tokens", 0) + (usage or {}).get("output_tokens", 0)
            self._tokens_used_today += result.tokens_used

            logger.info(
                "Heartbeat cognitive triage used %d tokens (daily: %d/%d)",
                result.tokens_used,
                self._tokens_used_today,
                self._settings.heartbeat_daily_token_budget,
            )

            if self._bus:
                _parent = getattr(self, "_current_tick_event", None)
                await self._bus.emit(
                    Event(
                        type="heartbeat_triage",
                        agent_id=self._settings.agent_id,
                        data={
                            "session_id": session_id,
                            "findings_count": len(findings),
                            "tokens_used": result.tokens_used,
                            "response_summary": result.response[:200],
                        },
                        trace_id=_parent.trace_id if _parent else None,
                        caused_by=_parent.event_id if _parent else None,
                    )
                )
        except Exception:
            logger.exception("Heartbeat cognitive triage failed")

        # End the session
        try:
            await triage_runner.end_conversation(session_id)
        except Exception:
            pass

        return result

    # ------------------------------------------------------------------
    # #273: on_complete callback execution
    # ------------------------------------------------------------------

    async def _execute_callback(self, check: DynamicCheck) -> None:
        """#273: Execute on_complete callback for a self-disabled dynamic check.

        3-layer failure handling:
        1. Run callback prompt; on failure, retry once after delay
        2. On second failure, send Telegram notification
        3. Create warning Finding and persist callback context
        """
        session_id = f"dynamic-callback-{check.name}-{uuid4().hex[:8]}"
        triage_runner = self._get_triage_runner()

        instruction = (
            f"[Dynamic Check Callback: {check.name}]\n"
            f"The check '{check.name}' has completed and self-disabled. "
            f"Execute the following callback task.\n\n"
            f"Instructions: {check.on_complete_prompt}\n\n"
            f"IMPORTANT: You may NOT re-enable the check '{check.name}' that triggered this callback."
        )

        tool_filter = check.on_complete_tools if check.on_complete_tools else None
        heartbeat_model = self._settings.heartbeat_model or self._settings.background_model
        # Harness Phase 2b: one run id across the retry below -- the retry gets a
        # fresh session but the same idempotency scope, so it cannot re-send.
        run_id = uuid4().hex

        for attempt in range(2):
            if not self._has_budget():
                logger.warning(
                    "#273: Skipping callback for '%s' — budget exhausted (attempt %d)",
                    check.name,
                    attempt + 1,
                )
                break
            if attempt == 1:
                # Layer 1: Retry after delay
                await asyncio.sleep(CALLBACK_RETRY_DELAY_SECONDS)

            try:
                response_text, _ctx, usage = await triage_runner.run_turn(
                    session_id,
                    instruction,
                    platform="heartbeat",
                    skip_episode=True,
                    is_subtask=True,
                    tool_filter=tool_filter,
                    model_override=heartbeat_model,
                    is_background=True,
                    context=ExecutionContext(
                        kind="heartbeat_callback",
                        session_id=session_id,
                        declared_tools=tuple(check.on_complete_tools or ()) or None,
                        check_name=check.name,
                        run_id=run_id,
                    ),
                )
                tokens = (usage or {}).get("input_tokens", 0) + (usage or {}).get("output_tokens", 0)
                self._tokens_used_today += tokens
                logger.info(
                    "#273: Callback for '%s' completed (tokens=%d, attempt=%d)",
                    check.name,
                    tokens,
                    attempt + 1,
                )
                # Success — clean up and return
                try:
                    await triage_runner.end_conversation(session_id)
                except Exception:
                    pass
                return
            except Exception:
                logger.exception(
                    "#273: Callback for '%s' failed (attempt %d/2)",
                    check.name,
                    attempt + 1,
                )
                # Clean up session before retry
                try:
                    await triage_runner.end_conversation(session_id)
                except Exception:
                    pass
                # Generate new session_id for retry
                session_id = f"dynamic-callback-{check.name}-{uuid4().hex[:8]}"

        # Layer 2: Both attempts failed — send Telegram notification
        await self._send_telegram(f"[Heartbeat] Callback failed for check '{check.name}' — manual follow-up needed")

        # Layer 3: Create warning Finding
        failure_finding = Finding(
            source=f"dynamic-callback:{check.name}",
            summary=f"on_complete callback failed after 2 attempts for check '{check.name}'",
            urgency="normal",
            needs_action=True,
            raw_data={
                "check_id": check.check_id,
                "check_name": check.name,
                "on_complete_prompt": check.on_complete_prompt,
                "dynamic": True,
            },
            check_name=check.name,
        )
        # Route through finding store if available
        if self._finding_store is not None:
            self._finding_store.ingest(failure_finding)
        logger.warning(
            "#273: Callback for '%s' failed after 2 attempts — Finding created",
            check.name,
        )

    # ------------------------------------------------------------------
    # F034.1: Daily digest + maintenance
    # ------------------------------------------------------------------

    async def _maybe_send_digest(self) -> None:
        """Send daily digest at UTC hour 9 if FindingStore has acknowledged items."""
        if self._finding_store is None:
            return

        now = datetime.now(UTC)
        today = now.date()

        if now.hour == 9 and self._last_digest_date != today:
            self._last_digest_date = today
            await self._daily_digest()

    async def _daily_digest(self) -> None:
        """Collect acknowledged findings and send grouped Telegram digest."""
        if self._finding_store is None:
            return

        items = self._finding_store.get_digest_items()
        if not items:
            return

        # Group by check_name
        by_check: dict[str, list] = {}
        for item in items:
            by_check.setdefault(item.finding.check_name, []).append(item)

        lines = [f"[Heartbeat] Daily digest ({len(items)} tracked findings):"]
        for check_name, check_items in sorted(by_check.items()):
            lines.append(f"\n{check_name} ({len(check_items)}):")
            for item in check_items[:5]:  # cap per check
                # Mark items near escalation with arrow
                near_escalation = ""
                if item.first_seen is not None:
                    age_h = (datetime.now(UTC) - item.first_seen).total_seconds() / 3600
                    urgency = item.finding.urgency
                    threshold = {"low": 72, "normal": 24, "high": 12}.get(urgency, 24)
                    if age_h >= threshold * 0.75:
                        near_escalation = " \u2b06\ufe0f"
                lines.append(
                    f"  - [{item.finding.urgency}] {item.finding.summary[:60]} (x{item.seen_count}){near_escalation}"
                )
            if len(check_items) > 5:
                lines.append(f"  ... and {len(check_items) - 5} more")

        await self._send_telegram("\n".join(lines))
        logger.info("F034.1: Sent daily digest with %d findings", len(items))

    async def _maybe_prune_and_sweep(self) -> None:
        """Run prune + sweep every 24 hours."""
        if self._finding_store is None:
            return

        now = datetime.now(UTC)
        if self._last_prune is not None and (now - self._last_prune).total_seconds() < 86400:
            return

        self._last_prune = now
        pruned = self._finding_store.prune()
        swept = self._finding_store.sweep_weak_negatives()
        if pruned or swept:
            logger.info("F034.1: Pruned %d resolved, swept %d weak_negative findings", pruned, swept)

    # ------------------------------------------------------------------
    # Telegram notifications
    # ------------------------------------------------------------------

    async def _send_telegram(self, text: str) -> None:
        """Send Telegram notification if configured (direct httpx POST)."""
        token = self._settings.telegram_bot_token
        chat_id = self._settings.telegram_chat_id
        if not token or not chat_id:
            return

        url = f"https://api.telegram.org/bot{token}/sendMessage"
        try:
            client = self._http or httpx.AsyncClient()
            try:
                response = await client.post(
                    url,
                    json={"chat_id": chat_id, "text": text},
                    timeout=10,
                )
            finally:
                if self._http is None:
                    await client.aclose()
            # Telegram answers a wrong token, a rate limit or a bad request
            # with an HTTP error, not an exception. Only the status is
            # logged: the URL holds the bot token, so neither it, the request
            # nor an httpx error text may reach a log line. (The isinstance:
            # a mocked client answers with no integer status.)
            status = getattr(response, "status_code", None)
            if isinstance(status, int) and status >= 400:
                logger.warning("Heartbeat Telegram notification rejected: HTTP %s", status)
        except Exception:
            logger.warning("Heartbeat Telegram notification failed")

    # ------------------------------------------------------------------
    # Budget + quiet hours
    # ------------------------------------------------------------------

    def _in_quiet_hours(self) -> bool:
        """Check if current hour falls in quiet range."""
        hour = datetime.now(UTC).hour
        start = self._settings.heartbeat_quiet_start
        end = self._settings.heartbeat_quiet_end

        if start <= end:
            # Simple range: e.g. 9-17
            return start <= hour < end
        else:
            # Wraps midnight: e.g. 23-8
            return hour >= start or hour < end

    def _has_budget(self) -> bool:
        """Check if daily token budget is not exhausted."""
        return self._tokens_used_today < self._settings.heartbeat_daily_token_budget

    def _maybe_reset_budget(self) -> None:
        """Reset daily budget on date change."""
        today = date.today()
        if today != self._budget_date:
            self._tokens_used_today = 0
            self._budget_date = today
            logger.debug("Heartbeat daily token budget reset")

    # ------------------------------------------------------------------
    # Missed check detection
    # ------------------------------------------------------------------

    async def _detect_missed_checks(self) -> None:
        """Detect if heartbeat was down and log it."""
        if self._bus is None:
            return

        try:
            # Query last heartbeat event from DB
            from sqlalchemy import select

            from nous.storage.models import Event as EventModel

            async with self._heart.db.session() as session:
                result = await session.execute(
                    select(EventModel.created_at)
                    .where(EventModel.agent_id == self._settings.agent_id)
                    .where(EventModel.event_type == "heartbeat_tick")
                    .order_by(EventModel.created_at.desc())
                    .limit(1)
                )
                row = result.scalar_one_or_none()
                if row is not None:
                    gap = (datetime.now(UTC) - row).total_seconds()
                    if gap > self._settings.heartbeat_tick_interval * 10:
                        logger.warning(
                            "Heartbeat was offline for %.0f seconds (last tick: %s)",
                            gap,
                            row.isoformat(),
                        )
        except Exception:
            logger.debug("Could not detect missed heartbeat checks (non-fatal)")

    # ------------------------------------------------------------------
    # Public API (for REST endpoints)
    # ------------------------------------------------------------------

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def is_quiet(self) -> bool:
        return self._in_quiet_hours()

    @property
    def registry(self) -> CheckRegistry:
        return self._registry

    @property
    def finding_store(self) -> FindingStore | None:
        return self._finding_store

    async def _maybe_tune(self) -> None:
        """Audit HB-3 (2026-06-09): run the self-tuning pass on a schedule.

        Previously `tuner.tune` was reachable only via the manual REST endpoint,
        so the four `NOUS_HEARTBEAT_TUNING_*` settings were inert even with
        `NOUS_HEARTBEAT_TUNING_ENABLED=true` in prod. The tuner internally
        enforces MIN_SAMPLES per check and snapshots/rolls back, so a pass with
        no outcome data is a safe no-op. First pass is delayed one full interval
        after startup to avoid tuning on every restart.
        """
        if not self._settings.heartbeat_tuning_enabled or self._finding_store is None:
            return
        now = datetime.now(UTC)
        if self._last_tune is None:
            # Anchor the interval at startup; don't tune immediately on boot.
            self._last_tune = now
            return
        interval_hours = getattr(self._settings, "heartbeat_tuning_interval_hours", 168)
        if (now - self._last_tune).total_seconds() / 3600 < interval_hours:
            return
        self._last_tune = now
        try:
            report = await self._tuner.tune(self._finding_store, self._registry)
            logger.info(
                "F034.3/HB-3: scheduled tuning pass — %d adjustment(s), %d skipped",
                len(report.adjustments),
                len(report.skipped_checks),
            )
        except Exception:
            logger.exception("F034.3/HB-3: scheduled tuning pass failed")

    @property
    def tuner(self) -> HeartbeatTuner:
        return self._tuner

    @property
    def dynamic_loader(self) -> DynamicCheckLoader | None:
        return self._dynamic_loader

    @property
    def tokens_used_today(self) -> int:
        return self._tokens_used_today

    @property
    def last_tick(self) -> datetime | None:
        return self._last_tick

    @property
    def last_dag_tick(self) -> datetime | None:
        return self._last_dag_tick

    @property
    def dag_tick_pending_since(self) -> datetime | None:
        """When the in-flight DAG tick started, or None when none is in flight.

        Read next to last_dag_tick: a tick that never returns leaves
        last_dag_tick stale and this value old.
        """
        task = self._dag_pending_task
        if task is None or task.done():
            return None
        return self._dag_pending_since

    def get_stats(self) -> dict:
        """F035.1: Return heartbeat runner statistics."""
        return {
            "total_ticks": self._tick_count,
            "last_tick_at": self._last_tick.isoformat() if self._last_tick else None,
            "currently_running": self._running,
            "tokens_used_today": self._tokens_used_today,
            "budget_remaining": max(0, self._settings.heartbeat_daily_token_budget - self._tokens_used_today),
        }

    async def trigger_tick(self) -> list[Finding]:
        """Force an immediate tick (for REST endpoint)."""
        self._maybe_reset_budget()
        return await self._tick()

    async def trigger_check(self, name: str) -> CheckResult | None:
        """Force a specific check to run (for REST endpoint)."""
        check = self._registry.get_check(name)
        if check is None:
            return None
        # A check whose DAG node is being terminalized starts no new run.
        if self._registry.is_fenced(name):
            return CheckResult(skipped=True)
        # Same run bracket as _tick (outcome defaults to failed): see the
        # comment there.
        self._registry.begin_run(check.name)
        run_succeeded: bool | None = False
        sibling_cancelled = False
        outcome: dict[str, bool] = {}
        outcome_token = RUN_OUTCOME.set(outcome)
        try:
            result = await asyncio.wait_for(check.run(), timeout=check.timeout)
            # A skipped result means run() returned early because the check
            # was disabled at the execution boundary — no LLM turn ran.
            if result.skipped:
                run_succeeded = None
            else:
                check.mark_success()
                run_succeeded = True
                # F034.5: Update DB stats for dynamic checks
                await self._record_run_stats(check, success=True)
            if result.tokens_used:
                self._tokens_used_today += result.tokens_used
            # #273: Fire callback if check self-disabled
            if isinstance(check, DynamicCheck) and result.self_disabled and check.on_complete_prompt:
                if self._has_budget():
                    asyncio.create_task(
                        self._execute_callback(check),
                        name=f"callback-{check.name}",
                    )
                else:
                    logger.warning(
                        "#273: Skipping callback for '%s' — budget exhausted",
                        check.name,
                    )
            return result
        except Exception as e:
            check.mark_failure()
            sibling_cancelled = _cancelled_by_sibling_run(e)
            # F034.5: Update DB stats for dynamic checks on failure
            await self._record_run_stats(check, success=False, error_msg=str(e)[:200])
            raise
        finally:
            RUN_OUTCOME.reset(outcome_token)
            self._registry.end_run(
                check.name,
                run_succeeded,
                self_disabled=_is_final_run(check, sibling_cancelled, outcome),
            )
