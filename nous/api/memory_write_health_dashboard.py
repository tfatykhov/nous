"""GET /dashboard/memory-write-health and its acknowledge action (precision plan P4).

The page reads the stored rollup (``nous_system.memory_write_health_daily``) and evaluates the rules on it with the
acknowledgements recorded so far, so an acknowledgement shows at once, without waiting for the hourly check. It never
returns memory text: the rollup holds counts, and the rule summaries are fixed text with numbers and source labels.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

from nous.api.autonomy_dashboard import parse_days
from nous.observability import memory_write_health as mwh

DAYS_DEFAULT = 30
ACKS_SHOWN = 20


def parse_window(raw: str | None) -> int:
    """The ``days`` query parameter: 1 to 90, ``DAYS_DEFAULT`` when absent. Raises ValueError otherwise."""
    return DAYS_DEFAULT if raw is None or raw == "" else parse_days(raw)


def disabled_payload(days: int, *, now: datetime | None = None) -> dict[str, Any]:
    """The page with the check off: the same keys as with it on, every list empty."""
    return {
        "enabled": False,
        "window_days": days,
        "as_of": (now or datetime.now(UTC)).isoformat(),
        "days": [],
        "live": None,
        "checks": [],
        "rules": [],
        "acknowledgements": [],
    }


def _ack_out(ack: dict[str, Any]) -> dict[str, Any]:
    at = ack.get("at")
    return {
        "rule": ack["rule"],
        "day": ack["day"].isoformat(),
        "note": ack.get("note") or "",
        "actor": ack.get("actor") or "",
        "at": at.isoformat() if isinstance(at, datetime) else at,
    }


async def get_memory_write_health_data(
    session: Any, agent_id: str, days: int, *, now: datetime | None = None
) -> dict[str, Any]:
    """The rollup for the last ``days`` days (oldest first), the latest live observation, every rule evaluated now
    and the latest acknowledgements (newest first)."""
    now = now or datetime.now(UTC)
    today = mwh.utc_day(now)
    rows = await mwh.load_rows(session, agent_id, today - timedelta(days=mwh.BACKFILL_DAYS - 1))
    acks = await mwh.load_acks(session, agent_id)
    latest = await mwh.latest_observation(session, agent_id)
    first_shown = today - timedelta(days=days - 1)
    return {
        "enabled": True,
        "window_days": days,
        "as_of": now.isoformat(),
        "days": [{"day": d.isoformat(), "metrics": m} for d, m in sorted(rows.items()) if d >= first_shown],
        "live": (
            {"day": latest[0].isoformat(), **latest[1].get("live", {}), "counters": latest[1].get("counters", {})}
            if latest is not None
            else None
        ),
        "checks": [s.to_dict() for s in mwh.evaluate(rows, acks, today)] if rows else [],
        "rules": [{"id": r, "name": mwh.RULE_NAMES[r], "urgency": mwh.URGENCY[r]} for r in mwh.RULES],
        "acknowledgements": [_ack_out(a) for a in reversed(acks[-ACKS_SHOWN:])],
    }


def parse_acknowledgement(body: Any, *, today: date) -> tuple[str, date, str]:
    """``{rule, day?, note?}`` from a request body; ``day`` defaults to today (UTC). Raises ValueError."""
    if not isinstance(body, dict):
        raise ValueError("body must be a JSON object")
    rule = body.get("rule")
    if not isinstance(rule, str) or rule not in mwh.RULES:
        raise ValueError(f"rule must be one of {list(mwh.RULES)}")
    raw_day = body.get("day")
    if raw_day is None or raw_day == "":
        day = today
    elif isinstance(raw_day, str):
        try:
            day = date.fromisoformat(raw_day)
        except ValueError as exc:
            raise ValueError("day must be an ISO date (YYYY-MM-DD)") from exc
    else:
        raise ValueError("day must be an ISO date (YYYY-MM-DD)")
    note = body.get("note", "")
    if not isinstance(note, str):
        raise ValueError("note must be a string")
    return rule, day, note
