"""F111 PR B1: the overdue watcher (reconciler v1-min).

Every ``NOUS_PROMISE_WATCHER_INTERVAL_SECONDS`` the heartbeat runner calls ``run_once``. It reads this agent's open
rows past ``due_at + grace`` (created at or after ``NOUS_PROMISE_WATCHER_SINCE``), leaves out the ones a later backing
call in the same session already covers, and pings the owner on Telegram once, in one message, about at most
``NOUS_PROMISE_WATCHER_MAX_ITEMS`` of them. Each row is pinged at most once.

Exactly-once is built from conditional sets, never increments (``CommitmentStore.claim_for_ping`` and friends): a
row is claimed (``notify_count = 1``) before the send; a send that went out finalizes it, one that certainly did not
(``SendResult.NOT_SENT``, e.g. a Telegram outage) releases it for the next run, and one whose outcome is unknown
releases it with ``unknown_sends`` + 1 until ``UNKNOWN_SEND_CAP``, when it is marked ``failed``. A claim a crash left
behind is never re-sent: a lost ping is preferred over a repeated one; it is marked ``orphaned`` after 2 intervals.

The watcher also keeps ``last_tick_at`` in memory for the dead-man line the chat replies carry when it stalls
(``dead_man_line``). What it never does: close or change a row's status, send anything but the one ping, or act on
the row's text.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

from nous.brain.commitments import Commitment, format_short_id
from nous.heartbeat.quiet_hours import in_quiet_hours
from nous.heartbeat.send_result import SendResult

logger = logging.getLogger(__name__)

UNKNOWN_SEND_CAP = 3  # sends whose outcome was unknown, per row, before it is marked failed
TEXT_MAX = 120  # characters of the promise shown in the ping
# Rows read per run. Rows stage 2 called not_promise are left out by the query, so the rest are pinged or suppressed
# within a run or two and the overdue set stays small.
CANDIDATE_LIMIT = 500
STALL_INTERVALS = 2  # a claim left behind is marked orphaned after this many intervals
# The dead-man line waits one interval more: a run that fails is no tick, so with 2 one failed run between two
# good ones would already show it (review M1 on PR #906).
DEAD_MAN_INTERVALS = 3
MAX_STAGE2_ATTEMPTS = 2
# Stage-2 retries per run, per row listed: a backlog of unclassified rows is worked off over several runs, so one run
# never takes longer than the dead-man line allows.
RETRIES_PER_ITEM = 2
# A send refused the same way every run (a wrong token: 401 every interval) logs its WARNING once, then at most once
# per this period, until a ping goes out (review M4 on PR #906).
RELEASE_WARNING_EVERY = timedelta(hours=1)


def parse_since(value: str | None) -> datetime | None:
    """The cutoff as an aware datetime, or None when it is unset or not ISO 8601 with an offset."""
    if not value or not value.strip():
        return None
    try:
        since = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    return since if since.tzinfo is not None else None


def _unknown_sends(row: Commitment) -> int:
    ping = (row.evidence or {}).get("ping")
    value = ping.get("unknown_sends") if isinstance(ping, dict) else None
    return value if isinstance(value, int) else 0


class CommitmentWatcher:
    """One agent's overdue watcher. ``send`` posts one message and says what is known about it (``SendResult``);
    ``classifier`` (a ``ClassifierScheduler``) retries stage 2 on rows that have no verdict."""

    def __init__(
        self,
        store: Any,
        settings: Any,
        send: Callable[[str], Awaitable[SendResult]],
        classifier: Any = None,
        started_at: datetime | None = None,
    ) -> None:
        self._store = store
        self._settings = settings
        self._send = send
        self._classifier = classifier
        self._since: datetime | None = None
        self.active = False  # passed its start-up checks: it runs, and a stall is reported
        self.last_tick_at = started_at or datetime.now(UTC)
        self._stall_shown_for: datetime | None = None  # the last_tick_at of the stall the line already reported
        # Per send outcome: when its "released" WARNING was last logged, and how many were left out since.
        self._release_warned_at: dict[SendResult, datetime] = {}
        self._release_unlogged: dict[SendResult, int] = {}

    @property
    def interval_seconds(self) -> int:
        return self._settings.promise_watcher_interval_seconds

    @property
    def _classifier_on(self) -> bool:
        return self._classifier is not None and bool(self._settings.promise_gate_classifier_enabled)

    def start_checks(self) -> bool:
        """The start-up checks, each with one WARNING: no usable cutoff or no Telegram, and the watcher does not
        run; the classifier off, and it runs but says it will ping every gate row. True when it may run."""
        ok = True
        self._since = parse_since(self._settings.promise_watcher_since)
        if self._since is None:
            logger.warning(
                "F111 promise watcher enabled but NOUS_PROMISE_WATCHER_SINCE is unset or not ISO 8601 with an "
                "offset (e.g. 2026-11-20T00:00:00+00:00); the watcher does not run"
            )
            ok = False
        if not self._settings.telegram_bot_token or not self._settings.telegram_chat_id:
            logger.warning(
                "F111 promise watcher enabled but Telegram is not configured (bot token and owner chat id); "
                "the watcher does not run"
            )
            ok = False
        if ok and not self._classifier_on:
            logger.warning(
                "promise watcher enabled without stage-2 classifier: every gate-detected row past due+grace with no "
                "stored not_promise verdict will be pinged, false positives included"
            )
        self.active = ok
        return ok

    def note_tick(self, now: datetime) -> None:
        """A run finished (quiet-hours runs and runs that pinged nothing included)."""
        self.last_tick_at = now

    def dead_man_line(self, now: datetime | None = None) -> str | None:
        """The warning a live chat reply carries when the watcher is running but no run finished for 3 intervals;
        at most once per stall (shown again only after a new tick and a new stall)."""
        if not self.active:
            return None
        now = now or datetime.now(UTC)
        if now - self.last_tick_at <= timedelta(seconds=DEAD_MAN_INTERVALS * self.interval_seconds):
            return None
        if self._stall_shown_for == self.last_tick_at:
            return None
        self._stall_shown_for = self.last_tick_at
        return (
            f"⚠ Commitment watcher has not run since {self.last_tick_at.astimezone(UTC):%H:%M} UTC; "
            "overdue promises are not being checked."
        )

    async def run_once(self, now: datetime | None = None) -> None:
        """One run at ``now`` (default: the current time). Never raises (a cancellation still propagates): a failure
        is one WARNING, and the run does not count as a tick. A completed run ticks at its end."""
        clock = now or datetime.now(UTC)
        try:
            await self._run(clock)
        except Exception:
            logger.warning("F111 promise watcher run failed", exc_info=True)
            return
        self.note_tick(now or datetime.now(UTC))

    async def _run(self, now: datetime) -> None:
        stale_before = now - timedelta(seconds=STALL_INTERVALS * self.interval_seconds)
        orphaned = await self._store.expire_stale_claims(stale_before)
        if orphaned:
            logger.warning(
                "F111 promise watcher: claims left by a crash marked orphaned, never re-sent: %s",
                ", ".join(format_short_id(s) for s in orphaned),
            )
        candidates = await self._store.list_watchable(now=now, since=self._since, limit=CANDIDATE_LIMIT)
        if not candidates:
            return
        eligible = await self._unsuppressed(candidates, now)
        picked, remaining = await self._pick(eligible)
        if not picked or in_quiet_hours(self._settings, now):
            return  # quiet hours: nothing is claimed; the rows go out in the first run after
        run_id = uuid.uuid4().hex
        claimed = await self._store.claim_for_ping([r.id for r in picked], run_id, now)
        if not claimed:
            return  # another run or process claimed them
        result = await self._deliver(self.render(claimed, remaining))
        # The send is over: the bookkeeping below runs to its end even if the run is cancelled now, so a sent ping
        # is not left as a claim.
        await asyncio.shield(self._settle(claimed, run_id, result, now))

    async def _unsuppressed(self, rows: list[Commitment], now: datetime) -> list[Commitment]:
        """The rows no later same-session backing call covers; the covered ones are marked and stay open. With the
        tool-call log off, or its query failing, every row is kept (pinged)."""
        if not self._settings.tool_call_log_enabled:
            return rows
        try:
            suppressors = await self._store.find_suppressors(rows)
        except Exception:
            logger.warning("F111 promise watcher: the suppression check failed; the rows are pinged", exc_info=True)
            return rows
        for row_id, call_id in suppressors.items():
            try:
                await self._store.mark_suppressed(row_id, call_id, now)
            except Exception:
                # Still covered, so still not pinged; the mark is tried again next run.
                logger.warning("F111 promise watcher: a suppression was not recorded", exc_info=True)
        return [r for r in rows if r.id not in suppressors]

    async def _pick(self, rows: list[Commitment]) -> tuple[list[Commitment], int]:
        """The first ``MAX_ITEMS`` rows to ping, in order, and how many eligible rows are left for later runs. A gate
        row stage 2 has not settled is classified once more here (with the classifier on); one it then calls
        ``not_promise`` is skipped. At most ``RETRIES_PER_ITEM`` x ``MAX_ITEMS`` retries per run; the rows past them
        wait for the next run."""
        limit = self._settings.promise_watcher_max_items
        retries_left = RETRIES_PER_ITEM * limit
        picked: list[Commitment] = []
        for index, row in enumerate(rows):
            if len(picked) >= limit:
                return picked, len(rows) - index
            if self._classifier_on and row.detected_by == "gate" and self._needs_retry(row):
                if retries_left <= 0:
                    return picked, len(rows) - index
                retries_left -= 1
                doc = await self._classifier.classify_now(row)
                row = replace(row, evidence={**(row.evidence or {}), "stage2": doc})
                if doc.get("verdict") == "not_promise":
                    continue
            picked.append(row)
        return picked, 0

    @staticmethod
    def _needs_retry(row: Commitment) -> bool:
        stage2 = row.stage2
        verdict = stage2.get("verdict")
        if verdict is None:
            return True
        attempts = stage2.get("attempts")
        return verdict == "error" and (not isinstance(attempts, int) or attempts < MAX_STAGE2_ATTEMPTS)

    def render(self, rows: list[Commitment], remaining: int) -> str:
        """One message: each row's id, its sentence (cut to ``TEXT_MAX``) and its deadline, then how to answer."""
        lines = ["Possibly unbacked commitments:"]
        for row in rows:
            text = " ".join(row.promise_text.split())
            if len(text) > TEXT_MAX:
                text = text[: TEXT_MAX - 1] + "…"
            line = f"{row.label} '{text}' (due {row.due_at.astimezone(UTC):%a %d %b %H:%M} UTC)"
            if (row.evidence or {}).get("due_parsed") is False:
                line += " (no time stated)"
            if row.detected_by == "gate" and not (self._classifier_on and row.stage2.get("verdict") == "promise"):
                line += " (not checked)"
            lines.append(line)
        lines.append(f"Reply /done {rows[0].label} or /drop {rows[0].label}.")
        if remaining > 0:
            lines.append(f"+{remaining} more will follow.")
        return "\n".join(lines)

    async def _deliver(self, message: str) -> SendResult:
        try:
            return await self._send(message)
        except Exception as exc:
            # The class only, no traceback or text: an httpx one can carry the request URL, which holds the bot token.
            logger.warning("F111 promise watcher: the send raised %s; its outcome is unknown", type(exc).__name__)
            return SendResult.UNKNOWN

    def _warn_released(self, labels: str, result: SendResult, now: datetime) -> None:
        """The WARNING for a ping that did not go out: the first of an outcome, then at most one per
        ``RELEASE_WARNING_EVERY``, counting the ones left out, until a ping is sent."""
        last = self._release_warned_at.get(result)
        if last is not None and now - last < RELEASE_WARNING_EVERY:
            self._release_unlogged[result] = self._release_unlogged.get(result, 0) + 1
            return
        self._release_warned_at[result] = now
        left_out = self._release_unlogged.pop(result, 0)
        logger.warning(
            "F111 promise watcher: the ping for %s was %s; released%s",
            labels,
            result.value,
            f" ({left_out} more like it since the last line)" if left_out else "",
        )

    async def _settle(self, claimed: list[Commitment], run_id: str, result: SendResult, now: datetime) -> None:
        """Finalize, release or fail the claimed rows by the send's result. Never raises: a write that fails is one
        WARNING naming the rows (they keep their claim, and are marked orphaned after 2 intervals)."""
        labels = ", ".join(r.label for r in claimed)
        try:
            if result is SendResult.SENT:
                self._release_warned_at.clear()
                self._release_unlogged.clear()
                await self._store.finalize_ping([r.id for r in claimed], run_id, now)
                return
            self._warn_released(labels, result, now)
            if result is SendResult.NOT_SENT:
                await self._store.release_ping({r.id: _unknown_sends(r) for r in claimed}, run_id, "not_sent")
                return
            counts = {r.id: _unknown_sends(r) + 1 for r in claimed}
            failed = {i: n for i, n in counts.items() if n >= UNKNOWN_SEND_CAP}
            released = {i: n for i, n in counts.items() if n < UNKNOWN_SEND_CAP}
            await self._store.release_ping(released, run_id, "unknown")
            if failed:
                await self._store.fail_ping(failed, run_id)
                logger.warning(
                    "F111 promise watcher: %s marked failed after %d sends with an unknown outcome; not sent again",
                    ", ".join(r.label for r in claimed if r.id in failed),
                    UNKNOWN_SEND_CAP,
                )
        except Exception:
            # A SENT ping keeps notify_count = 1, so it is never repeated; only its record is incomplete.
            logger.warning(
                "F111 promise watcher: run %s, ping %s for %s not recorded; the rows keep their claim and will be "
                "marked orphaned",
                run_id,
                result.value,
                labels,
                exc_info=True,
            )
