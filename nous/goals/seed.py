"""F114 goals: create, approve and control a goal. Run by the owner, on the host:

    docker exec <nous container> python -m nous.goals.seed create --title "..." --objective "..." --done-when "..."
    docker exec <nous container> python -m nous.goals.seed approve G-0007

Sub-commands: ``create``, ``list``, ``show``, ``approve``, ``pause``, ``resume``, ``run-now``, ``stop``, ``cancel``,
``confirm``, ``note``, ``raise-budget`` and ``extend``. Each change is a row write, journalled with actor ``host``;
this command never runs a session itself. The goal driver, in the Nous process, acts on ``run-now``, ``stop`` and
``cancel`` within one tick (``NOUS_GOALS_TICK_SECONDS``), with ``NOUS_GOALS_ENABLED`` on. With that flag off (or a
flag it needs off) the changing sub-commands refuse and change nothing; ``list`` and ``show`` still work.

Exit status 0, or 2 on a refusal, with the reason on stderr.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from typing import Any

from sqlalchemy.exc import SQLAlchemyError

from nous.config import Settings
from nous.goals.envelope import PRESETS
from nous.goals.store import GoalRefused, GoalStore, clip, describe
from nous.storage.database import Database
from nous.storage.models import Goal

ACTOR = "host"
SHOW_JOURNAL_ROWS = 10
SHOW_TEXT_CHARS = 300
SPEND_NOTE = (
    "Spend counts the uncached input and output tokens of each session's chain. Cache reads and writes, jobs "
    "launched from the chain, embeddings and helper calls are not counted."
)
FLAG_OFF_LINE = "refused: goals are off (NOUS_GOALS_ENABLED, or a flag it needs, is off); nothing was changed"
DB_ERROR_LINE = "error: the database refused the command ({kind}); nothing was changed"
_READ_ONLY = frozenset({"list", "show"})
# create's limit options and the envelope field each sets
_LIMIT_OPTIONS = (
    ("total", "total_budget"),
    ("day", "day_budget"),
    ("days", "deadline_days"),
    ("sessions_per_day", "sessions_per_day"),
    ("interval", "min_interval_seconds"),
    ("jobs_per_day", "jobs_per_day"),
    ("quiet_hours_ok", "quiet_hours_ok"),
)


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m nous.goals.seed", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    create = sub.add_parser("create", help="a new draft goal")
    create.add_argument("--title", required=True)
    create.add_argument("--objective", required=True)
    create.add_argument("--done-when", required=True, help="what done looks like")
    create.add_argument("--preset", default="small", choices=[*PRESETS, "custom"])
    create.add_argument("--total", type=int, help="total budget in tokens")
    create.add_argument("--day", type=int, help="budget per UTC day in tokens")
    create.add_argument("--days", type=int, help="deadline in days from approval")
    create.add_argument("--sessions-per-day", type=int)
    create.add_argument("--interval", type=int, help="seconds between sessions")
    create.add_argument("--jobs-per-day", type=int)
    create.add_argument(
        "--quiet-hours-ok",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="whether sessions may start in quiet hours (default: the preset's)",
    )
    create.add_argument("--origin-channel", help="where the goal asks and tells the owner (e.g. telegram:<chat id>)")

    sub.add_parser("list", help="every goal, one line each")
    for name, text in (
        ("show", "the charter, plan, newest journal rows and sessions"),
        ("approve", "draft to active"),
        ("pause", "no new session"),
        ("resume", "paused to active"),
        ("run-now", "ask for a session now"),
        ("stop", "pause, and stop the open session"),
        ("cancel", "cancel the goal and its open session"),
        ("confirm", "confirm a goal waiting for the owner is done"),
    ):
        sub.add_parser(name, help=text).add_argument("goal", help="G-0007, 7 or the goal's id")
    note = sub.add_parser("note", help="a note the next session reads")
    note.add_argument("goal")
    note.add_argument("text")
    budget = sub.add_parser("raise-budget", help="a new total (and day) budget")
    budget.add_argument("goal")
    budget.add_argument("--total", type=int, required=True)
    budget.add_argument("--day", type=int)
    extend = sub.add_parser("extend", help="more days before the deadline")
    extend.add_argument("goal")
    extend.add_argument("--days", type=int, required=True)
    return parser.parse_args(argv)


def _printable(text: str) -> str:
    """Text fit for a terminal: control characters (an escape sequence's ESC among them) become spaces."""
    return "".join(c if c == "\n" or c.isprintable() else " " for c in text)


def _line(goal: Goal) -> str:
    parts = [f"{describe(goal)}: {_printable(goal.title)}"]
    if goal.pending_request:
        parts.append(f"request {goal.pending_request} pending")
    if goal.next_wake_at is not None:
        parts.append(f"next wake {goal.next_wake_at:%Y-%m-%d %H:%M} UTC")
    if goal.deadline_at is not None:
        parts.append(f"deadline {goal.deadline_at:%Y-%m-%d %H:%M} UTC")
    return "; ".join(parts)


async def _show(store: GoalStore, goal: Goal) -> None:
    print(_line(goal))
    print(f"Objective: {_printable(goal.objective)}")
    print(f"Done when: {_printable(goal.done_when)}")
    print(
        f"Limits ({goal.preset}): {goal.total_budget} tokens in all, {goal.day_budget} a day; {goal.deadline_days} "
        f"days; {goal.sessions_per_day} sessions a day, {goal.min_interval_seconds} s apart; {goal.jobs_per_day} "
        f"jobs a day; quiet hours {'yes' if goal.quiet_hours_ok else 'no'}"
    )
    print(f"Spent: {await store.spent(goal.id)} of {goal.total_budget} tokens. {SPEND_NOTE}")
    print("Plan:" if goal.plan else "Plan: none yet")
    for step in goal.plan:
        evidence = ", ".join(step.get("evidence") or [])
        print(
            f"  {step.get('id')} [{step.get('status')}] {_printable(str(step.get('text', '')))}"
            + (f" (evidence: {_printable(evidence)})" if evidence else "")
        )
    sessions = await store.sessions(goal.id)
    print("Sessions:" if sessions else "Sessions: none yet")
    for row in sessions:
        end = f"ended {row.outcome}" if row.ended_at is not None else "open"
        print(f"  {row.n} ({row.trigger}) started {row.started_at:%Y-%m-%d %H:%M} UTC, {end}, {row.spent} tokens")
    rows = await store.journal(goal.id, limit=SHOW_JOURNAL_ROWS)
    print(f"Journal (newest {SHOW_JOURNAL_ROWS}):")
    for row in rows:
        session = f" session {row.session_n}" if row.session_n is not None else ""
        text = _printable(clip(row.text, SHOW_TEXT_CHARS))
        print(f"  #{row.seq} {row.created_at:%Y-%m-%d %H:%M} {row.kind} by {row.actor}{session}: {text}")


async def run(args: argparse.Namespace, settings: Any, store: GoalStore) -> int:
    """Run one sub-command against ``store``. Returns the exit status."""
    command = args.command
    if command not in _READ_ONLY and not getattr(settings, "goals_enabled", False):
        print(FLAG_OFF_LINE, file=sys.stderr)
        return 2
    try:
        if command == "list":
            goals = await store.list()
            for goal in goals:
                print(_line(goal))
            if not goals:
                print("No goals.")
            return 0
        if command == "create":
            overrides = {
                field: getattr(args, option) for option, field in _LIMIT_OPTIONS if getattr(args, option) is not None
            }
            goal = await store.create(
                title=args.title,
                objective=args.objective,
                done_when=args.done_when,
                preset=args.preset,
                overrides=overrides or None,
                proposed_by=ACTOR,
                origin_channel=args.origin_channel,
            )
        elif command == "show":
            goal = await store.get(args.goal)
            if goal is None:
                raise GoalRefused(f"no goal {args.goal}")
            await _show(store, goal)
            return 0
        elif command == "note":
            goal = await store.add_note(args.goal, args.text, actor=ACTOR)
        elif command == "raise-budget":
            goal = await store.raise_budget(args.goal, total=args.total, day=args.day, actor=ACTOR)
        elif command == "extend":
            goal = await store.extend_deadline(args.goal, days=args.days, actor=ACTOR)
        else:
            change = {
                "approve": store.approve,
                "pause": store.pause,
                "resume": store.resume,
                "run-now": store.request_run_now,
                "stop": store.stop,
                "cancel": store.cancel,
                "confirm": store.confirm_done,
            }[command]
            goal = await change(args.goal, actor=ACTOR)
    except GoalRefused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    except SQLAlchemyError as exc:  # its text carries the statement's parameters (the objective, a note): not shown
        print(DB_ERROR_LINE.format(kind=type(exc).__name__), file=sys.stderr)
        return 1
    print(_line(goal))
    return 0


async def seed(args: argparse.Namespace, settings: Settings) -> int:
    """Connect, run one sub-command, disconnect. Returns the exit status."""
    database = Database(settings)
    try:
        await database.connect()
        return await run(args, settings, GoalStore(database, settings.agent_id, settings))
    finally:
        await database.disconnect()


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point of ``python -m nous.goals.seed``."""
    return asyncio.run(seed(_parse_args(argv), Settings()))


if __name__ == "__main__":
    sys.exit(main())
