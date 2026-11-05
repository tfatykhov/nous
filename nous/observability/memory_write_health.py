"""Memory write health (precision plan P4): a daily rollup of the memory write paths, rules over it, and a hard-delete
detector.

One row per agent per UTC day in ``nous_system.memory_write_health_daily`` (migration 099), written by the hourly
``memory_write_health`` heartbeat check (``nous/heartbeat/memory_write_health_check.py``). A row's ``metrics`` holds:

- the day's counts, recomputable later from permanent tables (facts by source bucket, tier-1 inflow, deactivations
  by reason, admission rejects, episodes, sleep-prune counts); the first run fills ``BACKFILL_DAYS`` of them;
- on a day the check ran, ``live``: point-in-time values from its last run that day (row totals, ledger ids with no
  row, dangling edges, open episodes, the profile card), which cannot be recomputed later;
- on the same days, ``counters``: running totals of every rise in the hard-delete signals since the first
  observation, and ``rules``: the state of each rule at that run.

Rules (``RULES``): H1 hard delete, F1 source flood, S1 tier-1 stale deactivation, P1 person+preference inflow
collapse, R1 rule inflow collapse, A1 admission reject spike. Episode balance, merge rate and card churn are
metrics only. The findings go to the dashboard only (the check sets ``chat_delivery = False``) and never ask the
agent to act (``needs_action=False``): a triage turn that judges a spike to be junk can delete rows, so the owner
decides.

H1 is latched. Its five counters only grow; it trips while any counter is above the baseline, which is the counters
at the last acknowledgement (or zero, at the first observation). An acknowledgement is a ``nous_system.events`` row
of type ``ACK_EVENT`` (``acknowledge``); that table is never pruned. For the windowed rules an acknowledgement of
day D drops days up to D from the rule's trip window; the collapse rules then wait for a full window after D.

All days are UTC days (``AT TIME ZONE 'UTC'``), whatever the server's TimeZone setting.
"""

from __future__ import annotations

import json
import logging
import statistics
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from sqlalchemy import text

from nous.heart.facts import _TIER1_CATEGORIES
from nous.owner_actions import clean_actor
from nous.storage.models import Event

logger = logging.getLogger(__name__)

METRICS_VERSION = 1
ACK_EVENT = "memory_write_health_ack"
SOURCE = "memory_write_health"
BACKFILL_DAYS = 90  # covers P1's five 14-day windows with room to spare
NOTE_MAX_CHARS = 500
ACK_MAX_AGE_DAYS = 90

# heart.facts.source is free text. The seven labels with real volume keep their name; everything else, NULL
# included, is "other", which F1 watches as a whole.
SOURCE_LABELS = (
    "enumerative_extractor",
    "user_direct",
    "reflection",
    "sleep_reflection",
    "contradiction_resolution",
    "cluster_consolidation",
    "episode_summarizer",
)
OTHER = "other"
ENUMERATIVE = "enumerative_extractor"
TIER1 = tuple(_TIER1_CATEGORIES)
DEACTIVATION_REASONS = ("stale", "superseded", "merged", "removed", "user", "rollback", "unknown")
H1_COUNTERS = ("vanished_fact_ids", "vanished_episode_ids", "rows_dropped", "dangling_edges", "sleep_missing_endpoint")

RULES = ("H1", "F1", "S1", "P1", "R1", "A1")
RULE_NAMES = {
    "H1": "hard_delete",
    "F1": "source_flood",
    "S1": "tier1_stale",
    "P1": "profile_collapse",
    "R1": "rule_collapse",
    "A1": "admission_spike",
}
URGENCY = {"H1": "high", "F1": "high", "S1": "high", "P1": "normal", "R1": "normal", "A1": "normal"}
THRESHOLDS: dict[str, dict[str, float]] = {
    # F1: a label's count on one of the last two days >= min_count and >= ratio x max(median of its previous
    # median_days daily counts, median_floor).
    "F1": {"min_count": 100, "ratio": 3.0, "median_floor": 10, "median_days": 14, "days": 2},
    # S1: tier-1 facts the stale scan deactivated, today or yesterday.
    "S1": {"min": 1, "days": 2},
    # P1/R1: non-enumerative inflow over the last window_days <= ratio x the mean of the prior_windows windows
    # before it, with that mean >= min_mean; or zero for zero_days while the prior windows had some inflow.
    "P1": {"window_days": 14, "prior_windows": 4, "ratio": 0.4, "min_mean": 8, "zero_days": 21},
    "R1": {"window_days": 14, "prior_windows": 4, "ratio": 0.4, "min_mean": 8, "zero_days": 21},
    # A1: rejects / (rejects + admitted) over the last window_days >= min_rate with >= min_attempts attempts.
    "A1": {"window_days": 3, "min_rate": 0.75, "min_attempts": 100},
}
COLLAPSE_CATEGORIES = {"P1": ("person", "preference"), "R1": ("rule",)}


class AcknowledgeError(ValueError):
    """An acknowledgement the caller must fix (unknown rule, bad day, nothing observed yet)."""


@dataclass
class RuleState:
    """One rule evaluated at one moment. ``state`` is ok, tripped, acknowledged or no_data."""

    id: str
    state: str
    value: float | int | None = None
    threshold: float | int | None = None
    summary: str = ""
    detail: dict[str, Any] = field(default_factory=dict)
    acknowledged: dict[str, Any] | None = None

    @property
    def name(self) -> str:
        return RULE_NAMES[self.id]

    @property
    def urgency(self) -> str:
        return URGENCY[self.id]

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["name"] = self.name
        out["urgency"] = self.urgency
        return out


# ---------------------------------------------------------------------------------------------------------------------
# Day boundaries
# ---------------------------------------------------------------------------------------------------------------------


def utc_day(moment: datetime) -> date:
    return moment.astimezone(UTC).date()


def _day_start(day: date) -> datetime:
    return datetime.combine(day, time.min, tzinfo=UTC)


def _days(start: date, end: date) -> list[date]:
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def _day(col: str) -> str:
    return f"(({col}) AT TIME ZONE 'UTC')::date"


# ---------------------------------------------------------------------------------------------------------------------
# Recomputable day keys
# ---------------------------------------------------------------------------------------------------------------------


def empty_day() -> dict[str, Any]:
    """The recomputable keys of a day with no activity."""
    return {
        "facts_by_source": {},
        "max_facts_one_episode": 0,
        "tier1_inflow": {c: {"all": 0, "non_enum": 0} for c in TIER1},
        "deactivated": {r: 0 for r in DEACTIVATION_REASONS},
        "deactivated_tier1_stale": 0,
        "admission_rejected": 0,
        "episodes_started": 0,
        "episodes_ended": 0,
        "sleep_missing_endpoint": 0,
        "profile_card_rewrites": 0,
    }


_INT_OF = "CASE WHEN ({x}) ~ '^[0-9]+$' THEN ({x})::bigint ELSE 0 END"
_UUID_RE = "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"


def _as_uuid(expr: str) -> str:
    """``expr`` cast to uuid, or NULL when it is not one: the planner may cast before it filters."""
    return f"CASE WHEN ({expr}) ~* '{_UUID_RE}' THEN ({expr})::uuid END"


async def compute_days(session: Any, agent_id: str, start: date, end: date) -> dict[date, dict[str, Any]]:
    """The recomputable keys of every UTC day from ``start`` to ``end``, inclusive, one query per group."""
    out = {d: empty_day() for d in _days(start, end)}
    p = {"aid": agent_id, "t0": _day_start(start), "t1": _day_start(end + timedelta(days=1))}

    async def rows(sql: str, **extra: Any) -> list[Any]:
        return list((await session.execute(text(sql), {**p, **extra})).all())

    for day, label, n in await rows(
        f"""SELECT {_day("learned_at")}, CASE WHEN source = ANY(:labels) THEN source ELSE '{OTHER}' END, count(*)
              FROM heart.facts WHERE agent_id = :aid AND learned_at >= :t0 AND learned_at < :t1 GROUP BY 1, 2""",
        labels=list(SOURCE_LABELS),
    ):
        out[day]["facts_by_source"][label] = int(n)
    for day, n in await rows(
        f"""SELECT d, max(n) FROM (
                SELECT {_day("learned_at")} AS d, source_episode_id, count(*) AS n FROM heart.facts
                 WHERE agent_id = :aid AND learned_at >= :t0 AND learned_at < :t1 AND source_episode_id IS NOT NULL
                 GROUP BY 1, 2) x GROUP BY d"""
    ):
        out[day]["max_facts_one_episode"] = int(n)
    for day, category, n_all, n_non_enum in await rows(
        f"""SELECT {_day("learned_at")}, category, count(*),
                   count(*) FILTER (WHERE source IS DISTINCT FROM '{ENUMERATIVE}')
              FROM heart.facts WHERE agent_id = :aid AND learned_at >= :t0 AND learned_at < :t1
               AND category = ANY(:tier1) GROUP BY 1, 2""",
        tier1=list(TIER1),
    ):
        out[day]["tier1_inflow"][category] = {"all": int(n_all), "non_enum": int(n_non_enum)}
    for day, reason, n, n_tier1 in await rows(
        f"""SELECT {_day("deactivated_at")}, COALESCE(deactivation_reason, 'unknown'), count(*),
                   count(*) FILTER (WHERE category = ANY(:tier1))
              FROM heart.facts WHERE agent_id = :aid AND deactivated_at >= :t0 AND deactivated_at < :t1
             GROUP BY 1, 2""",
        tier1=list(TIER1),
    ):
        out[day]["deactivated"][reason] = int(n)
        if reason == "stale":
            out[day]["deactivated_tier1_stale"] = int(n_tier1)
    for day, n in await rows(
        f"""SELECT {_day("started_at")}, count(*) FROM heart.episodes
             WHERE agent_id = :aid AND started_at >= :t0 AND started_at < :t1 GROUP BY 1"""
    ):
        out[day]["episodes_started"] = int(n)
    for day, n in await rows(
        f"""SELECT {_day("ended_at")}, count(*) FROM heart.episodes
             WHERE agent_id = :aid AND ended_at >= :t0 AND ended_at < :t1 GROUP BY 1"""
    ):
        out[day]["episodes_ended"] = int(n)
    missing = _INT_OF.format(x="data->>'dead_edges_missing_endpoint'")
    for day, event_type, n, n_missing, n_rewrites in await rows(
        f"""SELECT {_day("created_at")}, event_type, count(*), sum({missing}),
                   count(*) FILTER (WHERE data->'profile_card'->>'outcome' = 'written')
              FROM nous_system.events
             WHERE agent_id = :aid AND event_type IN ('fact_rejected', 'sleep_completed')
               AND created_at >= :t0 AND created_at < :t1 GROUP BY 1, 2"""
    ):
        if event_type == "fact_rejected":
            out[day]["admission_rejected"] = int(n)
        else:
            out[day]["sleep_missing_endpoint"] = int(n_missing or 0)
            out[day]["profile_card_rewrites"] = int(n_rewrites)
    return out


# ---------------------------------------------------------------------------------------------------------------------
# Point-in-time values and the hard-delete counters
# ---------------------------------------------------------------------------------------------------------------------


async def observe_live(session: Any, agent_id: str, now: datetime) -> dict[str, Any]:
    """The point-in-time values: row totals, the id ledger, dangling edges, open episodes, the profile card.

    The ledger: every fact and episode the app creates emits a ``fact_learned`` / ``episode_started`` event in the
    same transaction, and events are never pruned, so an id in the ledger with no row is a row someone deleted.
    ``fact_learned`` is emitted twice for most facts (the DB session and the bus), hence DISTINCT.
    """
    p = {"aid": agent_id, "day_ago": now - timedelta(hours=24)}
    row = (
        await session.execute(
            text(
                f"""SELECT
                (SELECT count(*) FROM heart.facts WHERE agent_id = :aid),
                (SELECT count(*) FROM heart.episodes WHERE agent_id = :aid),
                (SELECT count(DISTINCT ev.data->>'fact_id') FROM nous_system.events ev
                  WHERE ev.agent_id = :aid AND ev.event_type = 'fact_learned'
                    AND ev.data->>'fact_id' ~* '{_UUID_RE}'
                    AND NOT EXISTS (SELECT 1 FROM heart.facts f WHERE f.id = {_as_uuid("ev.data->>'fact_id'")})),
                (SELECT count(DISTINCT ev.data->>'episode_id') FROM nous_system.events ev
                  WHERE ev.agent_id = :aid AND ev.event_type = 'episode_started'
                    AND ev.data->>'episode_id' ~* '{_UUID_RE}'
                    AND NOT EXISTS (SELECT 1 FROM heart.episodes e WHERE e.id = {_as_uuid("ev.data->>'episode_id'")})),
                (SELECT count(*) FROM brain.graph_edges g WHERE g.agent_id = :aid AND (
                    (g.source_type = 'fact' AND NOT EXISTS (SELECT 1 FROM heart.facts f WHERE f.id = g.source_id))
                 OR (g.target_type = 'fact' AND NOT EXISTS (SELECT 1 FROM heart.facts f WHERE f.id = g.target_id))
                 OR (g.source_type = 'episode'
                     AND NOT EXISTS (SELECT 1 FROM heart.episodes e WHERE e.id = g.source_id))
                 OR (g.target_type = 'episode'
                     AND NOT EXISTS (SELECT 1 FROM heart.episodes e WHERE e.id = g.target_id)))),
                (SELECT COALESCE(sum({_INT_OF.format(x="data->>'dead_edges_missing_endpoint'")}), 0)
                   FROM nous_system.events WHERE agent_id = :aid AND event_type = 'sleep_completed'),
                (SELECT count(*) FROM heart.episodes WHERE agent_id = :aid AND active AND ended_at IS NULL
                    AND outcome IS DISTINCT FROM 'abandoned'),
                (SELECT count(*) FROM heart.episodes WHERE agent_id = :aid AND active AND ended_at IS NULL
                    AND outcome IS DISTINCT FROM 'abandoned' AND started_at < :day_ago),
                (SELECT min(created_at) FROM nous_system.events
                  WHERE agent_id = :aid AND event_type = 'fact_learned')"""
            ),
            p,
        )
    ).one()
    card = (
        await session.execute(
            text(
                """SELECT jsonb_array_length(entries), tokens,
                          (SELECT count(*) FROM jsonb_array_elements(entries) e
                            WHERE NOT EXISTS (SELECT 1 FROM jsonb_array_elements(previous_entries) pe
                                               WHERE pe->>'text' = e->>'text'))
                     FROM nous_system.profile_card WHERE agent_id = :aid"""
            ),
            {"aid": agent_id},
        )
    ).one_or_none()
    return {
        "observed_at": now.isoformat(),
        "facts_total": int(row[0]),
        "episodes_total": int(row[1]),
        "vanished_fact_ids": int(row[2]),
        "vanished_episode_ids": int(row[3]),
        "dangling_edges": int(row[4]),
        "sleep_missing_endpoint_total": int(row[5]),
        "open_episodes": int(row[6]),
        "open_episodes_over_24h": int(row[7]),
        "ledger_oldest": row[8].isoformat() if row[8] is not None else None,
        "profile_card": (
            {
                "entries": int(card[0]),
                "tokens": int(card[1]),
                "changed_frac": round(int(card[2]) / max(int(card[0]), 1), 3),
            }
            if card is not None
            else None
        ),
    }


def advance_counters(
    prev_live: Mapping[str, Any] | None, prev_counters: Mapping[str, Any] | None, live: Mapping[str, Any]
) -> dict[str, int]:
    """The running hard-delete counters after this observation.

    Each counter adds this run's rise over the previous observation (a fall adds nothing, so a restore never hides a
    later delete): the two ledger counts, dangling edges, the sleep prune's missing-endpoint total, and the fall of
    the fact plus episode row totals (rows only grow; a deactivation changes ``active``, not the count). With no
    previous observation every counter starts at zero: what is already gone is the baseline.
    """
    counters = {k: int((prev_counters or {}).get(k, 0)) for k in H1_COUNTERS}
    if not prev_live:
        return {k: 0 for k in H1_COUNTERS}

    def rise(key: str) -> int:
        return max(0, int(live[key]) - int(prev_live.get(key, live[key])))

    counters["vanished_fact_ids"] += rise("vanished_fact_ids")
    counters["vanished_episode_ids"] += rise("vanished_episode_ids")
    counters["dangling_edges"] += rise("dangling_edges")
    counters["sleep_missing_endpoint"] += rise("sleep_missing_endpoint_total")
    for total in ("facts_total", "episodes_total"):
        counters["rows_dropped"] += max(0, int(prev_live.get(total, live[total])) - int(live[total]))
    return counters


# ---------------------------------------------------------------------------------------------------------------------
# Acknowledgements
# ---------------------------------------------------------------------------------------------------------------------


async def load_acks(session: Any, agent_id: str) -> list[dict[str, Any]]:
    """Every acknowledgement of this agent, oldest first: ``{rule, day, note, actor, at, counters?}``."""
    result = await session.execute(
        text(
            """SELECT data, created_at FROM nous_system.events
                WHERE agent_id = :aid AND event_type = :t ORDER BY created_at, id"""
        ),
        {"aid": agent_id, "t": ACK_EVENT},
    )
    acks = []
    for data, created_at in result.all():
        if not isinstance(data, dict) or data.get("rule") not in RULES:
            continue
        try:
            day = date.fromisoformat(str(data.get("day")))
        except ValueError:
            continue
        acks.append({**data, "day": day, "at": created_at})
    return acks


def _ack_index(acks: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """Per rule: the latest acknowledgement, with ``cut_day`` = the latest day any acknowledgement of it named."""
    out: dict[str, dict[str, Any]] = {}
    for ack in acks:
        rule = ack["rule"]
        cut = max(ack["day"], out[rule]["cut_day"]) if rule in out else ack["day"]
        out[rule] = {**ack, "cut_day": cut}
    return out


async def acknowledge(
    session: Any,
    agent_id: str,
    rule: str,
    day: date,
    note: str,
    actor: str,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Record that the owner accepts rule ``rule``'s evidence up to ``day`` (a deliberate restore or delete).

    For H1 the baseline becomes the running counters of the latest observation, so a delete after that observation
    still trips. The caller commits. Raises ``AcknowledgeError`` for an unknown rule, a day in the future or older
    than ``ACK_MAX_AGE_DAYS``, or an H1 acknowledgement before the first observation.
    """
    now = now or datetime.now(UTC)
    if rule not in RULES:
        raise AcknowledgeError(f"rule must be one of {list(RULES)}")
    today = utc_day(now)
    if not isinstance(day, date) or day > today or day < today - timedelta(days=ACK_MAX_AGE_DAYS):
        raise AcknowledgeError(f"day must be within the last {ACK_MAX_AGE_DAYS} days, not in the future")
    data: dict[str, Any] = {
        "rule": rule,
        "day": day.isoformat(),
        "note": "".join(ch for ch in str(note or "") if ch.isprintable())[:NOTE_MAX_CHARS].strip(),
        "actor": clean_actor(actor, default="unattributed"),
    }
    if rule == "H1":
        latest = await latest_observation(session, agent_id)
        if latest is None:
            raise AcknowledgeError("nothing observed yet: the check has not run")
        data["counters"] = {k: int(latest[1]["counters"].get(k, 0)) for k in H1_COUNTERS}
    session.add(Event(agent_id=agent_id, event_type=ACK_EVENT, data=data))
    await session.flush()
    return data


# ---------------------------------------------------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------------------------------------------------


def _get(rows: Mapping[date, Mapping[str, Any]], day: date, *path: str) -> Any:
    node: Any = rows.get(day)
    for key in path:
        if not isinstance(node, Mapping):
            return None
        node = node.get(key)
    return node


def _latest_with(rows: Mapping[date, Mapping[str, Any]], key: str) -> tuple[date, Mapping[str, Any]] | None:
    for day in sorted(rows, reverse=True):
        if isinstance(rows[day].get(key), Mapping):
            return day, rows[day]
    return None


def _ack_meta(ack: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if ack is None:
        return None
    at = ack.get("at")
    return {
        "day": ack["cut_day"].isoformat(),
        "at": at.isoformat() if isinstance(at, datetime) else at,
        "actor": ack.get("actor"),
        "note": ack.get("note"),
    }


def _h1(rows: Mapping[date, Mapping[str, Any]], ack: Mapping[str, Any] | None) -> RuleState:
    latest = _latest_with(rows, "counters")
    if latest is None:
        return RuleState("H1", "no_data")
    counters = latest[1]["counters"]
    base = (ack or {}).get("counters") or {}
    delta = {k: int(counters.get(k, 0)) - int(base.get(k, 0)) for k in H1_COUNTERS}
    if not any(v > 0 for v in delta.values()):
        return RuleState(
            "H1", "ok", value=0, threshold=1, detail={"since_baseline": delta}, acknowledged=_ack_meta(ack)
        )
    summary = (
        f"memory rows vanished since the baseline: {max(delta['vanished_fact_ids'], 0)} fact ids, "
        f"{max(delta['vanished_episode_ids'], 0)} episode ids, {max(delta['rows_dropped'], 0)} rows dropped, "
        f"{max(delta['dangling_edges'], 0)} dangling edges, {max(delta['sleep_missing_endpoint'], 0)} edges pruned "
        "for a missing row (owner decides; acknowledge on the dashboard if deliberate)"
    )
    return RuleState(
        "H1",
        "tripped",
        value=sum(max(v, 0) for v in delta.values()),
        threshold=1,
        summary=summary,
        detail={"since_baseline": delta},
        acknowledged=_ack_meta(ack),
    )


def _f1(rows: Mapping[date, Mapping[str, Any]], today: date, ack: Mapping[str, Any] | None) -> list[RuleState]:
    t = THRESHOLDS["F1"]
    cut = ack["cut_day"] if ack else None
    days = [today - timedelta(days=i) for i in range(int(t["days"]))]
    if not any(d in rows for d in days):
        return [RuleState("F1", "no_data")]
    states: list[RuleState] = []
    best: RuleState | None = None
    for label in (*SOURCE_LABELS, OTHER):
        for day in days:
            if day not in rows:
                continue
            count = int(_get(rows, day, "facts_by_source", label) or 0)
            prior = [
                int(_get(rows, d, "facts_by_source", label) or 0)
                for d in (day - timedelta(days=i) for i in range(1, int(t["median_days"]) + 1))
                if d in rows
            ]
            median = statistics.median(prior) if prior else 0
            threshold = max(t["min_count"], t["ratio"] * max(median, t["median_floor"]))
            state = RuleState(
                "F1",
                "ok",
                value=count,
                threshold=threshold,
                detail={"source": label, "day": day.isoformat(), "median": median},
            )
            if count >= threshold:
                if cut is not None and day <= cut:
                    state.state = "acknowledged"
                    state.acknowledged = _ack_meta(ack)
                else:
                    state.state = "tripped"
                    state.summary = (
                        f"fact flood from source {label}: {count} facts in a day against a median of {median:g} "
                        "(owner decides)"
                    )
                    states.append(state)
                    break
            if best is None or (count / threshold) > ((best.value or 0) / (best.threshold or 1)):
                best = state
    return states or ([best] if best is not None else [RuleState("F1", "no_data")])


def _s1(rows: Mapping[date, Mapping[str, Any]], today: date, ack: Mapping[str, Any] | None) -> RuleState:
    t = THRESHOLDS["S1"]
    cut = ack["cut_day"] if ack else None
    days = [today - timedelta(days=i) for i in range(int(t["days"]))]
    if not any(d in rows for d in days):
        return RuleState("S1", "no_data")
    raw = sum(int(_get(rows, d, "deactivated_tier1_stale") or 0) for d in days)
    count = sum(int(_get(rows, d, "deactivated_tier1_stale") or 0) for d in days if cut is None or d > cut)
    state = RuleState("S1", "ok", value=count, threshold=t["min"], acknowledged=_ack_meta(ack) if raw > count else None)
    if count >= t["min"]:
        state.state = "tripped"
        state.summary = f"stale scan deactivated {count} tier-1 facts today or yesterday (owner decides)"
    elif raw >= t["min"]:
        state.state = "acknowledged"
    return state


def _window_sum(rows: Mapping[date, Mapping[str, Any]], end: date, n: int, categories: Iterable[str]) -> int | None:
    """The non-enumerative inflow of ``categories`` over the ``n`` days ending ``end``; None if a day has no row."""
    total = 0
    for d in (end - timedelta(days=i) for i in range(n)):
        if d not in rows:
            return None
        inflow = _get(rows, d, "tier1_inflow") or {}
        total += sum(int((inflow.get(c) or {}).get("non_enum", 0)) for c in categories)
    return total


def _collapse(
    rule: str, rows: Mapping[date, Mapping[str, Any]], today: date, ack: Mapping[str, Any] | None
) -> RuleState:
    t = THRESHOLDS[rule]
    cats = COLLAPSE_CATEGORIES[rule]
    w = int(t["window_days"])
    current = _window_sum(rows, today, w, cats)
    priors = [_window_sum(rows, today - timedelta(days=w * k), w, cats) for k in range(1, int(t["prior_windows"]) + 1)]
    if current is None or any(p is None for p in priors):
        return RuleState(rule, "no_data")
    mean = sum(priors) / len(priors)  # type: ignore[arg-type]
    threshold = round(t["ratio"] * mean, 2)
    zero_days = int(t["zero_days"])
    zero_run = _window_sum(rows, today, zero_days, cats)
    detail = {"window_sum": current, "prior_mean": round(mean, 2), "prior_sums": priors, "categories": list(cats)}
    tripped_ratio = mean >= t["min_mean"] and current <= threshold
    tripped_zero = zero_run == 0 and sum(priors) > 0  # type: ignore[arg-type]
    state = RuleState(rule, "ok", value=current, threshold=threshold, detail=detail)
    if not (tripped_ratio or tripped_zero):
        return state
    cut = ack["cut_day"] if ack else None
    if cut is not None and today - timedelta(days=w - 1) <= cut:
        # The trip window still holds acknowledged days: wait for a full window after them.
        state.state = "acknowledged"
        state.acknowledged = _ack_meta(ack)
        return state
    what = "profile learning (person, preference)" if rule == "P1" else "rule learning"
    state.state = "tripped"
    state.summary = (
        f"{what} dropped: {current} facts in the last {w} days against a mean of {mean:.0f} before (owner decides)"
        if tripped_ratio
        else f"{what} stopped: no facts in {zero_days} days (owner decides)"
    )
    return state


def _a1(rows: Mapping[date, Mapping[str, Any]], today: date, ack: Mapping[str, Any] | None) -> RuleState:
    t = THRESHOLDS["A1"]
    cut = ack["cut_day"] if ack else None
    days = [today - timedelta(days=i) for i in range(int(t["window_days"]))]
    if not any(d in rows for d in days):
        return RuleState("A1", "no_data")
    kept = [d for d in days if d in rows and (cut is None or d > cut)]
    rejected = sum(int(_get(rows, d, "admission_rejected") or 0) for d in kept)
    admitted = sum(sum((_get(rows, d, "facts_by_source") or {}).values()) for d in kept)
    attempts = rejected + admitted
    rate = round(rejected / attempts, 3) if attempts else 0.0
    state = RuleState(
        "A1",
        "ok",
        value=rate,
        threshold=t["min_rate"],
        detail={"rejected": rejected, "admitted": admitted, "days": len(kept)},
        acknowledged=_ack_meta(ack) if len(kept) < len([d for d in days if d in rows]) else None,
    )
    if attempts >= t["min_attempts"] and rate >= t["min_rate"]:
        state.state = "tripped"
        state.summary = f"admission rejecting most facts: {rejected} of {attempts} in {len(kept)} days (owner decides)"
    return state


def evaluate(rows: Mapping[date, Mapping[str, Any]], acks: Iterable[Mapping[str, Any]], today: date) -> list[RuleState]:
    """Every rule over the stored day ``rows`` (``{day: metrics}``) and the acknowledgements. Pure."""
    index = _ack_index(acks)
    return [
        _h1(rows, index.get("H1")),
        *_f1(rows, today, index.get("F1")),
        _s1(rows, today, index.get("S1")),
        _collapse("P1", rows, today, index.get("P1")),
        _collapse("R1", rows, today, index.get("R1")),
        _a1(rows, today, index.get("A1")),
    ]


# ---------------------------------------------------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------------------------------------------------


async def load_rows(session: Any, agent_id: str, since: date) -> dict[date, dict[str, Any]]:
    result = await session.execute(
        text(
            """SELECT day, metrics FROM nous_system.memory_write_health_daily
                WHERE agent_id = :aid AND day >= :since ORDER BY day"""
        ),
        {"aid": agent_id, "since": since},
    )
    return {day: dict(metrics) for day, metrics in result.all()}


async def latest_observation(session: Any, agent_id: str) -> tuple[date, dict[str, Any]] | None:
    """The latest stored row the check observed live (its ``live`` and ``counters``), however old."""
    row = (
        await session.execute(
            text(
                """SELECT day, metrics FROM nous_system.memory_write_health_daily
                    WHERE agent_id = :aid AND jsonb_typeof(metrics->'counters') = 'object'
                    ORDER BY day DESC LIMIT 1"""
            ),
            {"aid": agent_id},
        )
    ).one_or_none()
    return (row[0], dict(row[1])) if row is not None else None


async def _upsert(session: Any, agent_id: str, day: date, metrics: Mapping[str, Any], now: datetime) -> None:
    await session.execute(
        text(
            """INSERT INTO nous_system.memory_write_health_daily (agent_id, day, metrics, metrics_version, computed_at)
               VALUES (:aid, :day, CAST(:metrics AS jsonb), :v, :now)
               ON CONFLICT (agent_id, day) DO UPDATE
                  SET metrics = EXCLUDED.metrics, metrics_version = EXCLUDED.metrics_version,
                      computed_at = EXCLUDED.computed_at"""
        ),
        {"aid": agent_id, "day": day, "metrics": json.dumps(metrics, sort_keys=True), "v": METRICS_VERSION, "now": now},
    )


# ---------------------------------------------------------------------------------------------------------------------
# One run
# ---------------------------------------------------------------------------------------------------------------------


async def run_once(database: Any, agent_id: str, *, now: datetime | None = None) -> list[RuleState]:
    """One hourly run, in one transaction: recompute today, yesterday and any missing day of the last
    ``BACKFILL_DAYS``; observe the live values; advance the counters; evaluate; store. A failure raises and writes
    nothing. The first observation (no earlier ``counters``) records a system baseline acknowledgement for H1, so the
    ids already gone before the monitor started never trip it."""
    now = now or datetime.now(UTC)
    today = utc_day(now)
    start = today - timedelta(days=BACKFILL_DAYS - 1)
    async with database.session() as session:
        # One run at a time per agent: each run adds its rise over the previous observation.
        await session.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": f"{SOURCE}:{agent_id}"})
        stored = await load_rows(session, agent_id, start)
        prev = await latest_observation(session, agent_id)
        missing = [d for d in _days(start, today) if d not in stored]
        recompute_from = min([*missing, today - timedelta(days=1)])
        computed = await compute_days(session, agent_id, recompute_from, today)
        live = await observe_live(session, agent_id, now)
        prev_metrics = prev[1] if prev is not None else None
        counters = advance_counters((prev_metrics or {}).get("live"), (prev_metrics or {}).get("counters"), live)
        rows: dict[date, dict[str, Any]] = {d: dict(m) for d, m in stored.items()}
        for d, day_keys in computed.items():
            rows[d] = {**rows.get(d, {}), **day_keys}
        rows[today]["live"] = live
        rows[today]["counters"] = counters
        acks = await load_acks(session, agent_id)
        if prev is None:
            baseline = await _seed_baseline(session, agent_id, today, counters, live)
            acks.append({**baseline, "day": today, "at": now})
        states = evaluate(rows, acks, today)
        # F1 yields either its tripped labels or its one closest label, so one state per rule id here.
        rows[today]["rules"] = {s.id: s.state for s in states}
        for d in computed:
            await _upsert(session, agent_id, d, rows[d], now)
        await session.commit()
    return states


async def _seed_baseline(
    session: Any, agent_id: str, today: date, counters: Mapping[str, int], live: Mapping[str, Any]
) -> dict[str, Any]:
    data = {
        "rule": "H1",
        "day": today.isoformat(),
        "note": "baseline at the first observation",
        "actor": "system",
        "counters": dict(counters),
        # What was already gone when the monitor started (counts only), for the record.
        "observed": {k: live[k] for k in ("vanished_fact_ids", "vanished_episode_ids", "dangling_edges")},
    }
    session.add(Event(agent_id=agent_id, event_type=ACK_EVENT, data=data))
    await session.flush()
    return data
