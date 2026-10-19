"""GET /dashboard/autonomy: the autonomy scoreboard, read-only.

Agent-scoped like the harness dashboard, typed ORM
selects, aggregation in Python so SQLite (tests) and Postgres (prod) agree. It answers
four questions: where does the owner's attention go (taps, approval cards, decision
grades), how are tasks scoring (coverage and distribution), what does the agent do
without asking (the autonomy inventory), and which tool-policy rules would fire (warn classes).
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import ColumnElement, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from nous.dag.approval import as_utc
from nous.observability.autonomy_inventory import INVENTORY_DAYS, get_autonomy_inventory, get_censor_activity
from nous.observability.task_score_gatherer import audits_without_action_row
from nous.observability.task_scoring import LEAF_KIND, OUTCOMES, ROLLUP_KINDS, SCORER_VERSION
from nous.storage.models import (
    A2uiAction,
    A2uiSurface,
    Decision,
    Event,
    ScorerHeartbeat,
    Subtask,
    TaskScore,
)

DAYS_DEFAULT = 7
DAYS_MAX = 90
# a2ui_actions.actor is "unattributed" for a person's tap, or the forwarded username or email when
# NOUS_A2UI_TRUST_FORWARDED_IDENTITY is on and the proxy sends one. A row the server writes itself (an expiry, a
# sweep, a restart) has a "system:<reason>" actor. So every actor without that prefix is the owner's tap.
SYSTEM_ACTOR_PREFIX = "system:"
TRIAGE_PREFIXES = ("heartbeat.", "decision.")
APPROVAL_SURFACE_KIND = "approval_gate"
POLICY_EVENT = "harness_context_policy_violation"  # runner._authorize_tool_call; data.mode is warn or enforce
WARN_CLASS_CAP = 50


def parse_days(raw: str | None) -> int:
    """The ``days`` query parameter: a whole number from 1 to 90, 7 when absent. Raises ValueError otherwise."""
    if raw is None or raw == "":
        return DAYS_DEFAULT
    if not (raw.isascii() and raw.isdigit()) or not 1 <= int(raw) <= DAYS_MAX:
        raise ValueError(f"days must be a whole number from 1 to {DAYS_MAX}")
    return int(raw)


def percentile(sorted_values: list[float], q: float) -> float | None:
    """The q-th percentile (0 to 1) of an ascending list, by linear interpolation; None when it is empty."""
    if not sorted_values:
        return None
    position = q * (len(sorted_values) - 1)
    low = int(position)
    high = min(low + 1, len(sorted_values) - 1)
    return sorted_values[low] + (sorted_values[high] - sorted_values[low]) * (position - low)


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 4)


def is_owner_tap() -> ColumnElement[bool]:
    """An a2ui_actions row the owner made: its actor is not a ``system:`` one, whether it is named or not."""
    return A2uiAction.actor.not_like(f"{SYSTEM_ACTOR_PREFIX}%")


async def _taps(session: AsyncSession, agent_id: str, since: datetime) -> tuple[dict[str, Any], dict[str, int]]:
    """The owner's taps by action, and the triage share of them; both read the same ``is_owner_tap`` rows."""
    rows = (
        await session.execute(
            select(A2uiAction.action_name, func.count().label("n"))
            .where(A2uiAction.agent_id == agent_id, is_owner_tap(), A2uiAction.created_at >= since)
            .group_by(A2uiAction.action_name)
        )
    ).all()
    by_action = sorted(
        ({"action_name": r.action_name, "count": r.n} for r in rows), key=lambda r: (-r["count"], r["action_name"])
    )
    taps = {"total": sum(r["count"] for r in by_action), "by_action": by_action}
    triage = {
        prefix.rstrip("."): sum(r["count"] for r in by_action if r["action_name"].startswith(prefix))
        for prefix in TRIAGE_PREFIXES
    }
    return taps, triage


async def _decision_grades(session: AsyncSession, agent_id: str, since: datetime) -> list[dict[str, Any]]:
    rows = (
        await session.execute(
            select(Decision.reviewer, Decision.outcome, func.count().label("n"))
            .where(Decision.agent_id == agent_id, Decision.reviewed_at >= since, Decision.reviewer.is_not(None))
            .group_by(Decision.reviewer, Decision.outcome)
        )
    ).all()
    return sorted(
        ({"reviewer": r.reviewer, "outcome": r.outcome, "count": r.n} for r in rows),
        key=lambda r: (r["reviewer"], r["outcome"] or ""),
    )


def _stats(rows: list[Any]) -> dict[str, Any]:
    """count, mean, p10, p50, p90 of the scores of ``rows`` and the overrides they carry."""
    scores = sorted(r.score for r in rows if r.score is not None)
    return {
        "count": len(scores),
        "mean": _round(sum(scores) / len(scores)) if scores else None,
        "p10": _round(percentile(scores, 0.10)),
        "p50": _round(percentile(scores, 0.50)),
        "p90": _round(percentile(scores, 0.90)),
        "overrides": sum(r.owner_overrides for r in rows if r.score is not None),
    }


def _verified_share(rows: list[Any]) -> dict[str, Any]:
    """Of the leaves that were scored (not cancelled, not legacy), how many are ``done_verified``."""
    scored = [r for r in rows if r.score is not None]
    verified = sum(1 for r in scored if r.outcome == "done_verified")
    return {"scored": len(scored), "verified": verified, "share": _round(verified / len(scored)) if scored else None}


def _scorer(beat: ScorerHeartbeat | None) -> dict[str, Any] | None:
    """The scoring command's heartbeat row, or None before its first pass."""
    if beat is None:
        return None
    return {
        "scorer_version": beat.scorer_version,
        "last_run_at": as_utc(beat.last_run_at).isoformat(),
        "last_status": beat.last_status,
        "last_error": beat.last_error,
        "last_ok_at": as_utc(beat.last_ok_at).isoformat() if beat.last_ok_at is not None else None,
        "rows_submitted": beat.rows_submitted,
    }


async def _task_scores(session: AsyncSession, agent_id: str, since: datetime) -> dict[str, Any]:
    """Subtasks are the scored unit. DAG runs and intention roots are roll-ups: counted apart, never in a score sum.

    Coverage is measured only from the scorer's first run (``first_scored_at``): tasks that finished before it
    were never going to have a row. ``scorer`` is the heartbeat the scoring command writes after every pass, the
    liveness signal: a ``last_run_at`` much older than the scorer's interval means the scorer stopped or cannot write
    to ``scoring``, and ``last_status`` says how the latest pass ended. It is None until the first pass.
    ``last_scored_at`` stays the time of the latest score write, which does not move while no task finished in the
    last 14 days.
    """
    T = TaskScore
    first, last = (
        await session.execute(
            select(func.min(T.first_scored_at), func.max(T.scored_at)).where(
                T.agent_id == agent_id, T.scorer_version == SCORER_VERSION
            )
        )
    ).one()
    rows = (
        await session.execute(
            select(
                T.task_kind,
                T.outcome,
                T.score,
                T.owner_overrides,
                T.unverified_claims,
                T.harness_errors,
                T.refusals,
                T.settled,
                T.finished_at,
            ).where(T.agent_id == agent_id, T.scorer_version == SCORER_VERSION, T.finished_at >= since)
        )
    ).all()
    leaves = [r for r in rows if r.task_kind == LEAF_KIND]
    floor = max(since, as_utc(first)) if first is not None else None
    finished = covered = None
    if floor is not None:
        finished = (
            await session.execute(
                select(func.count()).where(
                    Subtask.agent_id == agent_id,
                    Subtask.status.in_(("completed", "failed", "cancelled")),
                    Subtask.completed_at >= floor,
                )
            )
        ).scalar_one()
        covered = sum(1 for r in leaves if as_utc(r.finished_at) >= floor)
    outcomes = Counter(r.outcome for r in leaves)
    beat = (
        await session.execute(select(ScorerHeartbeat).where(ScorerHeartbeat.agent_id == agent_id))
    ).scalar_one_or_none()
    return {
        "scorer_version": SCORER_VERSION,
        "first_scored_at": as_utc(first).isoformat() if first is not None else None,
        "last_scored_at": as_utc(last).isoformat() if last is not None else None,
        "scorer": _scorer(beat),
        # v5 tripwire: answered check audits with no completed audit.verdict action for that exact audit. Normally 0.
        "audits_without_action_row": await audits_without_action_row(session, agent_id),
        # The three numbers to read first, side by side. A subtask's score alone does not see the owner's overrides:
        # a rejected proposal, an undone chain call, an unverified-claim note and a DAG-level error all sit on
        # roll-ups, which are in no score sum. So the overrides and the penalties are counted over ALL units.
        "headline": {
            "verified_share": {
                "provisional": _verified_share([r for r in leaves if not r.settled]),
                "settled": _verified_share([r for r in leaves if r.settled]),
            },
            "owner_overrides": {
                "total": sum(r.owner_overrides for r in rows),
                "subtasks": sum(r.owner_overrides for r in leaves),
                "rollups": sum(r.owner_overrides for r in rows if r.task_kind != LEAF_KIND),
            },
            "harness_penalties": {
                "harness_errors": sum(r.harness_errors for r in rows),
                "refusals": sum(r.refusals for r in rows),
                "unverified_claims": sum(r.unverified_claims for r in rows),
            },
        },
        "subtasks": {
            "finished": finished,
            "covered": covered,
            "coverage": _round(covered / finished) if finished else None,
            "by_outcome": {o: outcomes.get(o, 0) for o in OUTCOMES},
            "provisional": _stats([r for r in leaves if not r.settled]),
            "settled": _stats([r for r in leaves if r.settled]),
        },
        "rollups": {
            kind: {
                "count": sum(1 for r in rows if r.task_kind == kind),
                "by_outcome": {o: sum(1 for r in rows if r.task_kind == kind and r.outcome == o) for o in OUTCOMES},
                # penalties that belong to the unit itself; the subtasks under it keep their own
                "own_penalties": {
                    "harness_errors": sum(r.harness_errors for r in rows if r.task_kind == kind),
                    "refusals": sum(r.refusals for r in rows if r.task_kind == kind),
                    "unverified_claims": sum(r.unverified_claims for r in rows if r.task_kind == kind),
                    "owner_overrides": sum(r.owner_overrides for r in rows if r.task_kind == kind),
                },
            }
            for kind in ROLLUP_KINDS
        },
    }


async def _warn_classes(session: AsyncSession, agent_id: str, since: datetime) -> list[dict[str, Any]]:
    classes: Counter[tuple[str, str, str, str]] = Counter()
    for ev in await session.execute(
        select(Event.data).where(
            Event.agent_id == agent_id, Event.event_type == POLICY_EVENT, Event.created_at >= since
        )
    ):
        data = ev.data if isinstance(ev.data, dict) else {}
        classes[
            (
                str(data.get("mode") or "unknown"),
                str(data.get("context_kind") or "unknown"),
                str(data.get("violation") or "unknown"),
                str(data.get("tool_name") or "unknown"),
            )
        ] += 1
    ranked = sorted(classes.items(), key=lambda item: (-item[1], item[0]))
    return [
        {"mode": mode, "context_kind": context, "violation": violation, "tool_name": tool, "count": n}
        for (mode, context, violation, tool), n in ranked[:WARN_CLASS_CAP]
    ]


async def get_autonomy_data(
    session: AsyncSession,
    agent_id: str,
    *,
    days: int = DAYS_DEFAULT,
    events_persisted: bool = True,
    now: datetime | None = None,
) -> dict[str, Any]:
    """GET /dashboard/autonomy. Read-only; every count is for the last ``days`` days except the inventory (30)."""
    since = (now or datetime.now(UTC)) - timedelta(days=days)
    taps, triage = await _taps(session, agent_id, since)
    cards = (
        await session.execute(
            select(func.count()).where(
                A2uiSurface.agent_id == agent_id,
                A2uiSurface.kind == APPROVAL_SURFACE_KIND,
                A2uiSurface.created_at >= since,
            )
        )
    ).scalar_one()
    return {
        "window_days": days,
        "taps": taps,
        "triage_taps": triage,
        "approval_cards_asked": cards,
        "decision_grades": await _decision_grades(session, agent_id, since),
        "task_scores": await _task_scores(session, agent_id, since),
        "inventory": {
            "days": INVENTORY_DAYS,
            "tools": await get_autonomy_inventory(session, agent_id, days=INVENTORY_DAYS, now=now),
            "censors": await get_censor_activity(session, agent_id, days=INVENTORY_DAYS, now=now),
        },
        "events_persisted": events_persisted,
        # With event persistence off nothing was recorded, so an empty list would read as "no warnings".
        "warn_classes": await _warn_classes(session, agent_id, since) if events_persisted else [],
    }
