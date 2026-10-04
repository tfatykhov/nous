"""F098 Phase B: the wake turn gate (option C — the Telegram bot pulls).

The bot polls ``GET /inbox/wake`` and, when a chat has results waiting and no
live turn, runs a normal streaming turn in its own session for that chat. The
whole decision — origin filter, quiet hours, rate limit, debounce — lives in
:class:`WakeGate`, so the bot only has to know "is this chat busy".

Only conversation-originated rows wake: a subtask row carries a channel only
when ``spawn_task`` ran in a conversation, and a DAG row only wakes when the
DAG itself recorded an ``origin_channel`` (a scheduled DAG routed to the
default chat by ``NOUS_RESULT_INBOX_DAG_SCHEDULED`` never does).

The wake does not claim rows: the turn's ``pre_turn`` claims them through the
Phase A reader, so a row is still injected exactly once. Starting a wake
stamps ``wake_attempted_at`` on the rows it is for; that stamp is both the
"never wake twice for one row" guard (a failed wake falls back to the next
user turn) and the durable rate-limit counter.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import and_, exists, func, or_, select, update

from nous.storage.database import Database
from nous.storage.models import ExecutionDAG, ResultInbox

if TYPE_CHECKING:  # pragma: no cover - typing only
    from nous.config import Settings

logger = logging.getLogger(__name__)

TELEGRAM_PREFIX = "telegram:"
# The user-side text of a wake turn. Tagged so a reader of the transcript can
# tell it apart from something the user typed; the results themselves arrive
# through the inbox reader's <result_message> envelope.
WAKE_NOTE = "[system:wake] Background results arrived; report them briefly."
_MAX_TITLES = 5


def wake_enabled(settings: Settings) -> bool:
    return bool(settings.result_wake_enabled and settings.result_inbox_enabled)


def will_wake(settings: Settings, channel: str | None) -> bool:
    """Whether a result routed to ``channel`` is reported by a wake turn.

    The one predicate the two older Telegram pushes (subtask notify, F087
    DAG leg) consult to stand down, so the user is not pinged twice.
    """
    return wake_enabled(settings) and bool(channel) and str(channel).startswith(TELEGRAM_PREFIX)


@dataclass(frozen=True, slots=True)
class WakeDecision:
    channel: str
    wake: bool
    reason: str  # "ready" | "empty" | "quiet_hours" | "rate_limited" | "debounce"
    count: int = 0
    titles: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, Any]:
        return {
            "channel": self.channel, "wake": self.wake, "reason": self.reason,
            "count": self.count, "titles": list(self.titles),
        }


def _aware(ts: datetime) -> datetime:
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=UTC)


class WakeGate:
    """Decides whether a channel gets a wake turn now. One per server process."""

    def __init__(self, database: Database, agent_id: str, settings: Settings) -> None:
        self._db = database
        self._agent_id = agent_id
        self._settings = settings
        # Metrics (in-process): wakes fired, and polls that found rows waiting
        # but held back, by reason.
        self.fired = 0
        self.suppressed: Counter[str] = Counter()

    def _eligible(self, now: datetime, channel: str | None):
        cutoff = now - timedelta(hours=self._settings.result_inbox_max_age_hours)
        dag_has_origin = exists(
            select(ExecutionDAG.id).where(
                ExecutionDAG.id == ResultInbox.source_id, ExecutionDAG.origin_channel.is_not(None),
            )
        )
        return and_(
            ResultInbox.agent_id == self._agent_id,
            ResultInbox.channel == channel if channel else ResultInbox.channel.like(f"{TELEGRAM_PREFIX}%"),
            ResultInbox.delivered_at.is_(None),
            ResultInbox.wake_attempted_at.is_(None),
            ResultInbox.created_at > cutoff,
            or_(ResultInbox.source_kind == "subtask", and_(ResultInbox.source_kind == "dag", dag_has_origin)),
        )

    async def pending_channels(self, now: datetime | None = None) -> list[str]:
        """Telegram channels with at least one row that could wake."""
        now = now or datetime.now(UTC)
        async with self._db.session() as session:
            rows = await session.execute(
                select(ResultInbox.channel).where(self._eligible(now, None)).distinct()
            )
            return sorted(c for c in rows.scalars().all() if c)

    async def decide(self, channel: str, *, now: datetime | None = None, record: bool = True) -> WakeDecision:
        """Whether ``channel`` should be woken now. Read-only: claims nothing."""
        from nous.heartbeat.runner import in_quiet_hours  # heavy module; keep import lazy

        now = now or datetime.now(UTC)
        async with self._db.session() as session:
            rows = (
                await session.execute(
                    select(ResultInbox.title, ResultInbox.created_at)
                    .where(self._eligible(now, channel))
                    .order_by(ResultInbox.created_at)
                )
            ).all()
            if not rows:
                return WakeDecision(channel, False, "empty")
            recent = (
                await session.execute(
                    select(func.count(func.distinct(ResultInbox.wake_attempted_at))).where(
                        ResultInbox.agent_id == self._agent_id,
                        ResultInbox.channel == channel,
                        ResultInbox.wake_attempted_at > now - timedelta(hours=1),
                    )
                )
            ).scalar_one()
        titles = tuple(r[0] for r in rows[-_MAX_TITLES:])
        newest = max(_aware(r[1]) for r in rows)
        if in_quiet_hours(self._settings, now):
            reason = "quiet_hours"
        elif recent >= self._settings.result_wake_max_per_hour:
            reason = "rate_limited"
        elif (now - newest).total_seconds() < self._settings.result_wake_debounce_seconds:
            # Results that finish together are reported together.
            reason = "debounce"
        else:
            return WakeDecision(channel, True, "ready", len(rows), titles)
        if record:
            self.suppressed[reason] += 1
        return WakeDecision(channel, False, reason, len(rows), titles)

    async def begin(self, channel: str, *, now: datetime | None = None) -> WakeDecision:
        """Re-decide and, if still ready, stamp the rows this wake is for.

        The stamp is the gate: it only flips rows that are still eligible, so
        a user turn that claimed them first (or a second poller) leaves
        nothing to stamp and no empty wake turn is run.
        """
        now = now or datetime.now(UTC)
        decision = await self.decide(channel, now=now, record=False)
        if not decision.wake:
            return decision
        async with self._db.session() as session:
            ids = (
                await session.execute(select(ResultInbox.id).where(self._eligible(now, channel)))
            ).scalars().all()
            stamped = []
            if ids:
                stamped = (
                    await session.execute(
                        update(ResultInbox)
                        .where(
                            ResultInbox.id.in_(ids),
                            ResultInbox.delivered_at.is_(None),
                            ResultInbox.wake_attempted_at.is_(None),
                        )
                        # One value for the whole batch: the rate limit counts
                        # distinct stamps, i.e. wakes.
                        .values(wake_attempted_at=now)
                        .returning(ResultInbox.id)
                    )
                ).scalars().all()
            await session.commit()
        if not stamped:
            return WakeDecision(channel, False, "empty")
        self.fired += 1
        logger.info("F098: wake turn for %s (%d results)", channel, len(stamped))
        return WakeDecision(channel, True, "ready", len(stamped), decision.titles)

    def metrics(self) -> dict[str, Any]:
        return {"fired": self.fired, "suppressed": dict(self.suppressed)}
