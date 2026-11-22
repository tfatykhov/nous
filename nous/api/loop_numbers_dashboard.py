"""GET /dashboard/loop-numbers: what a finished unit of work costs, and how many finished units nobody outside the
agent checked. Read-only, computed on read from existing columns: typed ORM selects of named columns, aggregation in
Python (as ``autonomy_dashboard``). No table of its own and no roll-up job.

Three units, shown side by side and never added up:

- ``tasks``: ``heart.subtasks`` rows with no DAG node (plain spawns, scheduled runs, a goal session's tasks);
- ``workflow_steps``: rows with a DAG node, one per attempt of a step;
- ``goal_sessions``: ended ``brain.goal_sessions`` rows. A session's ``spent`` includes its tasks and workflows, so the
  same tokens appear in more than one unit.

Tokens are ``tokens_in + tokens_out`` as the runner counts them (``usage.input_tokens`` plus ``output_tokens``): input
read from or written to the prompt cache is not in them, the way goal budgets count. A task is done or not by the
scorer's own rule (``task_scoring.raw_outcome``); a cancelled one is left out, as the scorer leaves it out.

The second number reads ``scoring.task_scores`` (written only by the scoring job): of the finished tasks and workflow
steps, how many no one outside the agent confirmed (``done_self``), against those the owner or a merged pull request
confirmed (``done_verified``) or whose kind of check the owner's spot checks trust (``done_checked``). A goal session
has no such number. Only numbers and labels leave this module: no task text, no result text, no ids.
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from nous.api.autonomy_dashboard import percentile
from nous.dag.approval import as_utc
from nous.observability.task_scoring import LEAF_KIND, SCORER_VERSION, raw_outcome
from nous.storage.models import GoalSession, ScorerHeartbeat, Subtask, TaskScore

# Each read is capped; a window that hits the cap says so (``truncated``) and keeps the newest rows.
MAX_ROWS = 20_000
# A score row's finished_at is its subtask's completed_at; the score read is widened by this much on each side.
SCORE_MARGIN = timedelta(days=1)
CONFIRMED = frozenset({"done_verified", "done_checked"})
UNCONFIRMED = "done_self"
TAKEN_BACK = frozenset({"partial", "failed"})  # a check or a retry took the "done" away
NOTES = ("tokens_leave_out_cached_input", "goal_session_tokens_include_tasks_and_workflows")


def _ratio(numerator: float, denominator: float, places: int) -> float | None:
    """``numerator / denominator`` rounded, or None (never 0) when there is nothing to divide by."""
    return round(numerator / denominator, places) if denominator else None


async def _subtasks(session: AsyncSession, agent_id: str, since: datetime, until: datetime | None) -> tuple[list, bool]:
    query = select(
        Subtask.id,
        Subtask.status,
        Subtask.final_outcome,
        Subtask.tokens_in,
        Subtask.tokens_out,
        Subtask.dag_node_id,
    ).where(
        Subtask.agent_id == agent_id,
        Subtask.status.in_(("completed", "failed")),
        Subtask.completed_at >= since,
    )
    if until is not None:
        query = query.where(Subtask.completed_at < until)
    rows = (await session.execute(query.order_by(Subtask.completed_at.desc()).limit(MAX_ROWS + 1))).all()
    return rows[:MAX_ROWS], len(rows) > MAX_ROWS


async def _scores(session: AsyncSession, agent_id: str, since: datetime, until: datetime | None) -> tuple[dict, bool]:
    """The leaf score rows of this scorer version near the window, by task id (a subtask's id as text)."""
    query = select(TaskScore.task_id, TaskScore.outcome, TaskScore.unverified_claims, TaskScore.settled).where(
        TaskScore.agent_id == agent_id,
        TaskScore.scorer_version == SCORER_VERSION,
        TaskScore.task_kind == LEAF_KIND,
        TaskScore.finished_at >= since - SCORE_MARGIN,
    )
    if until is not None:
        query = query.where(TaskScore.finished_at < until + SCORE_MARGIN)
    rows = (await session.execute(query.order_by(TaskScore.finished_at.desc()).limit(MAX_ROWS + 1))).all()
    return {r.task_id: r for r in rows[:MAX_ROWS]}, len(rows) > MAX_ROWS


async def _goal_sessions(
    session: AsyncSession, agent_id: str, since: datetime, until: datetime | None
) -> tuple[list, bool]:
    """Ended goal sessions that started in the window (the day ``goals/spend.py`` assigns a session's spend to)."""
    query = select(GoalSession.spent, GoalSession.steps_closed).where(
        GoalSession.agent_id == agent_id,
        GoalSession.started_at >= since,
        GoalSession.ended_at.is_not(None),
    )
    if until is not None:
        query = query.where(GoalSession.started_at < until)
    rows = (await session.execute(query.order_by(GoalSession.started_at.desc()).limit(MAX_ROWS + 1))).all()
    return rows[:MAX_ROWS], len(rows) > MAX_ROWS


def _outside_check(done: list, scores: dict, *, claims_recorded: bool) -> dict[str, Any]:
    """Of the done units: confirmed outside the agent, not confirmed (its own word, checked or not), taken back by
    the scorer (a check said no, or an earlier attempt), or with no score row yet. A score row of another outcome
    (``not_scored``) counts as unscored, so the four always add up to the done units."""
    confirmed = unconfirmed = taken_back = unscored = provisional = flagged = 0
    for row in done:
        sc = scores.get(str(row.id))
        if sc is None:
            unscored += 1
            continue
        if sc.unverified_claims > 0:
            flagged += 1
        if sc.outcome in CONFIRMED:
            confirmed += 1
        elif sc.outcome == UNCONFIRMED:
            unconfirmed += 1
        elif sc.outcome in TAKEN_BACK:
            taken_back += 1
            continue
        else:
            unscored += 1
            continue
        if not sc.settled:
            provisional += 1
    return {
        "confirmed": confirmed,
        "unconfirmed": unconfirmed,
        "taken_back": taken_back,
        "unscored": unscored,
        "unconfirmed_share": _ratio(unconfirmed, confirmed + unconfirmed, 4),
        "provisional": provisional,
        # The per-turn claim check's finding is recorded only with event persistence on: off, nothing was counted.
        "flagged": flagged if claims_recorded else None,
    }


def _unit(rows: list, scores: dict, *, claims_recorded: bool) -> dict[str, Any]:
    """Number 1 and number 2 for one unit. Tokens of every unit that ended (done or not) over the done count: work that
    did not finish is cost the finished work carried."""
    done, not_done = [], []
    outcomes: Counter[str] = Counter()
    for row in rows:
        raw = raw_outcome("subtask", row.status, row.final_outcome)
        if raw == "done":
            done.append(row)
        elif raw in ("partial", "failed"):
            not_done.append(row)
        else:  # cancelled: left out, as the scorer leaves it out
            continue
        outcomes[row.final_outcome or "none"] += 1
    tokens_in = {"done": sum(r.tokens_in for r in done), "not_done": sum(r.tokens_in for r in not_done)}
    tokens_out = {"done": sum(r.tokens_out for r in done), "not_done": sum(r.tokens_out for r in not_done)}
    per_done = sorted(float(r.tokens_in + r.tokens_out) for r in done)
    p50 = percentile(per_done, 0.5)
    return {
        "done": len(done),
        "not_done": len(not_done),
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "tokens_per_finished": _ratio(sum(tokens_in.values()) + sum(tokens_out.values()), len(done), 1),
        # Output only: per-call input added by the harness does not move it.
        "output_per_finished": _ratio(sum(tokens_out.values()), len(done), 1),
        "done_tokens": {
            "mean": _ratio(sum(per_done), len(per_done), 1),
            "p50": None if p50 is None else round(p50, 1),
        },
        "by_final_outcome": dict(sorted(outcomes.items())),
        "outside_check": _outside_check(done, scores, claims_recorded=claims_recorded),
    }


def _goal_unit(rows: list) -> dict[str, Any]:
    spent = sum(r.spent for r in rows)
    steps = sum(r.steps_closed for r in rows)
    return {
        "sessions": len(rows),
        "sessions_without_step": sum(1 for r in rows if r.steps_closed == 0),
        "steps_closed": steps,
        "spent": spent,
        "spent_without_step": sum(r.spent for r in rows if r.steps_closed == 0),
        "tokens_per_finished": _ratio(spent, steps, 1),
        # Nothing existing says who confirmed a closed step: not measured.
        "outside_check": None,
    }


async def _window(
    session: AsyncSession,
    agent_id: str,
    since: datetime,
    until: datetime | None,
    *,
    to: datetime,
    claims_recorded: bool,
) -> dict[str, Any]:
    subtasks, cut_subtasks = await _subtasks(session, agent_id, since, until)
    scores, cut_scores = await _scores(session, agent_id, since, until)
    goals, cut_goals = await _goal_sessions(session, agent_id, since, until)
    return {
        "from": since.isoformat(),
        "to": to.isoformat(),
        "truncated": cut_subtasks or cut_scores or cut_goals,
        "tasks": _unit([r for r in subtasks if r.dag_node_id is None], scores, claims_recorded=claims_recorded),
        "workflow_steps": _unit(
            [r for r in subtasks if r.dag_node_id is not None], scores, claims_recorded=claims_recorded
        ),
        "goal_sessions": _goal_unit(goals),
    }


async def get_loop_numbers(
    session: AsyncSession,
    agent_id: str,
    *,
    days: int,
    claims_recorded: bool,
    now: datetime | None = None,
) -> dict[str, Any]:
    """GET /dashboard/loop-numbers: the last ``days`` days (``current``) and the window of the same length before it
    (``previous``). ``claims_recorded`` is ``f026_persistence_enabled``: off, ``flagged`` is null."""
    current = as_utc(now) if now is not None else datetime.now(UTC)
    since = current - timedelta(days=days)
    before = since - timedelta(days=days)
    beat = (
        await session.execute(select(ScorerHeartbeat.agent_id).where(ScorerHeartbeat.agent_id == agent_id))
    ).scalar_one_or_none()
    return {
        "enabled": True,
        "window_days": days,
        "counts_cached_input": False,
        "scorer_ran": beat is not None,
        "claims_recorded": claims_recorded,
        "notes": list(NOTES),
        "current": await _window(session, agent_id, since, None, to=current, claims_recorded=claims_recorded),
        "previous": await _window(session, agent_id, before, since, to=since, claims_recorded=claims_recorded),
    }
