"""F114: what goal sessions spend, read from rows.

A session's spend is its root's token budget exactly as the continuation counts it (``continuation.root_limits``):
the root's arrivals (input plus output), what its failed attempts spent, its lineage's subtasks outside a DAG and
its lineage's DAGs. Not counted: cache reads and writes, jobs launched from the chain, embeddings and helper calls
such as compaction, and a model call that failed before it returned its usage.

A session's spend belongs to the UTC day it started, however late its tokens come: two of the five sources (a
failed attempt's tokens and a DAG's total) are running totals with no time of their own, so the spend cannot be
split by the day it was spent without a second way to count it.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Sequence
from datetime import datetime
from pathlib import PurePosixPath
from typing import Any
from uuid import UUID

from sqlalchemy import ColumnElement, Text, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from nous.brain import continuation, intentions
from nous.cognitive.bash_side_effect import simple_commands
from nous.storage.models import GoalSession, Intention, ToolCallLog

# The tool-call log's outcomes of a call that reached the shell (refused, gated, suppressed calls never ran).
RAN_OUTCOMES: tuple[str, ...] = ("ran", "error")
# Shells that may be given runner.sh as an operand (``bash <workspace>/claude-jobs/runner.sh launch ...``).
_SHELLS = frozenset({"sh", "bash", "dash", "zsh", "ksh"})
# A logged call that cannot be read as shell words (its JSON clipped by the log, or a command the tokeniser refuses):
# runner.sh, then only blanks, quotes, backslashes or the JSON escapes of a tab, a line break or a quote, then the
# word launch. A clipped call keeps its head, where the runner and its verb are.
_LAUNCH_TEXT = re.compile(r"runner\.sh(?:\s|\\[tnr\"\\]|['\"])+launch(?![\w.-])")


def day_start(now: datetime | None = None) -> ColumnElement[datetime]:
    """The start of the UTC day ``now`` is in (by default the database's clock)."""
    return func.date_trunc("day", func.clock_timestamp() if now is None else now, "UTC")


async def session_spent(session: AsyncSession, agent_id: str, root_ids: Iterable[UUID]) -> dict[UUID, int]:
    """Tokens each root's chain has spent (the five terms ``root_limits`` adds); 0 for a root with no rows."""
    parts = await continuation.root_token_parts(session, agent_id, root_ids)
    return {root_id: tally.total for root_id, tally in parts.items()}


async def goal_spent(
    session: AsyncSession, agent_id: str, goal_id: UUID, *, now: datetime | None = None
) -> tuple[int, int]:
    """``(total, today)``: what the goal's sessions have spent in all, and what those started since the start of
    the UTC day of ``now`` (by default the database's clock) have spent. A session counts for the day it started."""
    since = day_start(now)
    total, today = (
        await session.execute(
            select(
                func.coalesce(func.sum(GoalSession.spent), 0),
                func.coalesce(func.sum(GoalSession.spent).filter(GoalSession.started_at >= since), 0),
            ).where(GoalSession.agent_id == agent_id, GoalSession.goal_id == goal_id)
        )
    ).one()
    return int(total), int(today)


async def sessions_started_today(session: AsyncSession, agent_id: str, goal_id: UUID, *, now: datetime) -> int:
    """How many of the goal's sessions started since the start of the UTC day of ``now``."""
    return int(
        await session.scalar(
            select(func.count()).where(
                GoalSession.agent_id == agent_id,
                GoalSession.goal_id == goal_id,
                GoalSession.started_at >= day_start(now),
            )
        )
        or 0
    )


def runner_verb(words: Sequence[str]) -> str | None:
    """The verb one simple command gives runner.sh: the word after the program when the program is named
    ``runner.sh``, or after the first ``runner.sh`` operand of a shell. None when the command runs no runner.sh."""
    if not words:
        return None
    name = PurePosixPath(words[0]).name
    if name == "runner.sh":
        at = 0
    elif name in _SHELLS:
        found = [i for i, word in enumerate(words) if i > 0 and PurePosixPath(word).name == "runner.sh"]
        if not found:
            return None
        at = found[0]
    else:
        return None
    return words[at + 1] if at + 1 < len(words) else None


def launches_job(input_text: str | None) -> bool:
    """Whether a bash call, as the tool-call log stores its input (the input as JSON, redacted, clipped past its
    cap), launches a job. The command is read into shell words by the lineage shell's own tokeniser
    (``simple_commands``, as ``tool_policy.lineage_bash_allowed`` reads it), so a launch counts however its words are
    spaced or quoted, and a command that only names runner.sh (an echo, a status) does not. A text that cannot be read
    so (clipped, not a JSON object with a string ``command``, or refused by the tokeniser) counts when ``_LAUNCH_TEXT``
    finds runner.sh then launch in it: a cap then holds a start too early rather than too late."""
    text = input_text or ""
    try:
        data = json.loads(text)
    except ValueError:
        data = None
    command = data.get("command") if isinstance(data, dict) else None
    commands = simple_commands(command, globs=True) if isinstance(command, str) else None
    if commands is None:
        return _LAUNCH_TEXT.search(text) is not None
    return any(runner_verb(words) == "launch" for words, _targets, _inputs, _op in commands)


async def jobs_launched_today(
    session: AsyncSession, agent_id: str, goal_id: UUID, settings: Any, *, now: datetime
) -> int | None:
    """Job launches (``launches_job``: runner.sh run with the verb launch, in a shell call that ran) of the goal's
    lineages since the start of the UTC day of ``now``, read from the tool-call log: a call of a session's decision
    turns (their session id), of a lineage subtask or of a lineage DAG. None (unknown) when the tool-call log is off;
    it is best-effort even when on (a write dropped under load is a launch not counted).

    The count reads each logged command as shell words, the way the shell policy does, and counts a launch when the
    job runner is the program (or a shell's operand) and its verb is launch; a logged command that cannot be read as
    words is matched by its text. Every bash call of the goal's lineages that ran that day is read for it (the query
    has no text filter): one goal's calls of one UTC day, each at most ``tool_input_max_chars`` long, read when a
    start of that goal is decided."""
    if getattr(settings, "tool_call_log_enabled", False) is not True:
        return None
    goal_roots = select(GoalSession.root_id).where(
        GoalSession.agent_id == agent_id, GoalSession.goal_id == goal_id, GoalSession.root_id.is_not(None)
    )
    turn_sessions = select(func.concat(continuation.INTENT_SESSION_PREFIX, cast(GoalSession.root_id, Text))).where(
        GoalSession.agent_id == agent_id, GoalSession.goal_id == goal_id, GoalSession.root_id.is_not(None)
    )

    def lineage(kind: str):
        return select(Intention.source_id).where(
            Intention.agent_id == agent_id, Intention.root_id.in_(goal_roots), Intention.source_kind == kind
        )

    texts = await session.scalars(
        select(ToolCallLog.input_text).where(
            ToolCallLog.agent_id == agent_id,
            ToolCallLog.tool_name == "bash",
            ToolCallLog.started_at >= day_start(now),
            ToolCallLog.outcome.in_(RAN_OUTCOMES),
            or_(
                ToolCallLog.session_id.in_(turn_sessions),
                cast(ToolCallLog.subtask_id, Text).in_(lineage(intentions.SOURCE_SUBTASK)),
                cast(ToolCallLog.dag_id, Text).in_(lineage(intentions.SOURCE_DAG)),
            ),
        )
    )
    return sum(1 for text in texts if launches_job(text))
