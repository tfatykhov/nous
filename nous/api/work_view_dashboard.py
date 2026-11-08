"""F108 Phase 2: the Work view's read-only endpoints (GET /dashboard/work/...).

Pure functions of ``(session, agent_id, params)``: parameter parsing, SQL and shaping. ``nous/api/rest.py`` only
routes to them. They read the Phase 1 tables (``nous_system.agent_turn_log`` and ``nous_system.tool_call_log``,
migration 100) through the app's own session, agent-scoped, each statement under a 10-second timeout. The text they
return was redacted when it was stored; it is returned as stored and never logged.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

DAYS_DEFAULT = 7
DAYS_MAX = 30
SESSIONS_LIMIT_DEFAULT = 50
SESSIONS_LIMIT_MAX = 100
TOOLS_PER_SESSION = 12
TITLE_TASK_CHARS = 100
TITLE_TRIGGER_CHARS = 80
MANY_CALLS = 10
NO_TITLE = "(no title)"

KINDS: tuple[str, ...] = ("conversation", "task", "background")
# ``all`` is what the list shows by default: conversations and tasks, background work hidden.
KIND_FILTERS: dict[str, tuple[str, ...]] = {
    "all": ("conversation", "task"),
    "conversation": ("conversation",),
    "task": ("task",),
    "background": ("background",),
}
# A session's flags, in the order they are listed.
FLAGS: tuple[str, ...] = ("error", "repeat", "many", "asked", "changed")
# The only detectable "Nous asked you": a resolve_intention call that decided to ask.
ASK_PATTERN = r'"decision"\s*:\s*"ask"'

# A turn's work_kind to the list's kind of work. Any other work_kind is background.
WORK_KIND_GROUP: dict[str, str] = {
    "conversation": "conversation",
    "subtask": "task",
    "workflow_step": "task",
    "intention": "task",
    "heartbeat": "background",
    "background_check": "background",
}

# A tool to what it touches (the mockups' map). Any other tool is lane ``other``. The colours live in the UI.
TOOL_LANES: dict[str, str] = {
    **dict.fromkeys(("recall_deep", "learn_fact", "record_decision", "resolve_decisions", "get_procedure"), "memory"),
    **dict.fromkeys(("db_query", "list_decisions", "list_tasks", "dag_manage"), "live"),
    **dict.fromkeys(("bash", "read_file", "write_file", "run_python"), "workspace"),
    **dict.fromkeys(
        (
            "send_email",
            "spawn_task",
            "schedule_task",
            "resolve_intention",
            "submit_final_report",
            "web_fetch",
            "web_search",
            "dag_create",
        ),
        "acts",
    ),
}


def lane_of(tool: str) -> str:
    """The lane of a canonical tool name: memory, live, workspace, acts, or other."""
    return TOOL_LANES.get(tool, "other")


# ---------------------------------------------------------------------------------------------------------------------
# Parameters
# ---------------------------------------------------------------------------------------------------------------------


def _bounded_int(raw: str | None, name: str, default: int, maximum: int) -> int:
    if raw is None or raw == "":
        return default
    if not (raw.isascii() and raw.isdigit()) or not 1 <= int(raw) <= maximum:
        raise ValueError(f"{name} must be a whole number from 1 to {maximum}")
    return int(raw)


def parse_days(raw: str | None, default: int, maximum: int) -> int:
    """The ``days`` query parameter: 1 to ``maximum``, ``default`` when absent. Raises ValueError otherwise."""
    return _bounded_int(raw, "days", default, maximum)


def parse_limit(raw: str | None, default: int, maximum: int) -> int:
    """The ``limit`` query parameter: 1 to ``maximum``, ``default`` when absent. Raises ValueError otherwise."""
    return _bounded_int(raw, "limit", default, maximum)


def format_cursor(ts: datetime, key: str) -> str:
    """A page cursor: ``<ISO timestamp>|<key>``, microseconds kept so the database value round-trips exactly."""
    return f"{ts.isoformat()}|{key}"


def parse_cursor(raw: str | None) -> tuple[datetime, str] | None:
    """A cursor from :func:`format_cursor`, split on the first ``|`` only; None when absent. Raises ValueError unless
    it holds a timezone-aware ISO timestamp and a non-empty key."""
    if raw is None or raw == "":
        return None
    ts_part, sep, key = raw.partition("|")
    if not sep or not key:
        raise ValueError("cursor must be '<timestamp>|<key>'")
    try:
        ts = datetime.fromisoformat(ts_part)
    except ValueError as exc:
        raise ValueError("cursor timestamp must be an ISO timestamp") from exc
    if ts.tzinfo is None or ts.utcoffset() is None:
        raise ValueError("cursor timestamp must carry a timezone")
    return ts, key


def parse_sessions_query(params: Mapping[str, str]) -> dict[str, Any]:
    """The keyword arguments of :func:`get_sessions` from the request's query. Raises ValueError on a bad value."""
    kind = params.get("kind") or "all"
    if kind not in KIND_FILTERS:
        raise ValueError(f"kind must be one of {', '.join(KIND_FILTERS)}")
    lens = params.get("lens") or None
    if lens is not None and lens not in FLAGS:
        raise ValueError(f"lens must be one of {', '.join(FLAGS)}")
    return {
        "kind": kind,
        "days": parse_days(params.get("days"), DAYS_DEFAULT, DAYS_MAX),
        "tool": params.get("tool") or None,
        "q": params.get("q") or None,
        "lens": lens,
        "limit": parse_limit(params.get("limit"), SESSIONS_LIMIT_DEFAULT, SESSIONS_LIMIT_MAX),
        "before": parse_cursor(params.get("before")),
    }


def _like_pattern(q: str) -> str:
    """A case-insensitive substring pattern for ``ILIKE ... ESCAPE '\\'`` that matches ``%``, ``_`` and ``\\``
    literally."""
    escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _iso(ts: datetime | None) -> str | None:
    return ts.isoformat() if ts is not None else None


async def _set_timeout(session: AsyncSession) -> None:
    await session.execute(text("SET LOCAL statement_timeout = '10s'"))


# ---------------------------------------------------------------------------------------------------------------------
# GET /dashboard/work/sessions
# ---------------------------------------------------------------------------------------------------------------------


def disabled_sessions(days: int) -> dict[str, Any]:
    """The sessions list with the tool-call log off: the same keys as with it on, every list empty."""
    return {
        "enabled": False,
        "window_days": days,
        "counts": {k: {"sessions": 0, "turns": 0} for k in KINDS},
        "tool_names": [],
        "sessions": [],
        "next_before": None,
    }


# A session is the window's turns of one non-NULL session_id. Its calls are the tool_call_log rows of those turns
# (joined on turn_id), so a call with no turn row and a turn with no session are never counted. Its kind, first
# message and parent come from its first turn (started_at, then id). Unreferenced CTEs are not run.
_SESSIONS_CTE = """
WITH turns AS (
    SELECT t.id, t.session_id, t.parent_session_id, t.work_kind, t.context_kind, t.trigger_text,
           t.tool_call_count, t.started_at, coalesce(t.ended_at, t.started_at) AS last_at
    FROM nous_system.agent_turn_log t
    WHERE t.agent_id = :agent AND t.session_id IS NOT NULL
      AND t.started_at >= now() - make_interval(days => :days)
),
first_turn AS (
    SELECT DISTINCT ON (session_id) session_id, parent_session_id, work_kind, context_kind, trigger_text
    FROM turns
    ORDER BY session_id, started_at, id
),
calls AS (
    SELECT c.turn_id, c.call_index, c.tool_name, c.input_text, c.is_error, c.ledger_entry_id, c.subtask_id,
           t.session_id, t.started_at AS turn_started_at
    FROM nous_system.tool_call_log c
    JOIN turns t ON t.id = c.turn_id
    WHERE c.agent_id = :agent
),
call_stats AS (
    SELECT session_id, count(*) AS tool_calls, count(*) FILTER (WHERE is_error) AS errors,
           bool_or(is_error) AS f_error,
           bool_or(tool_name = 'resolve_intention' AND input_text ~ :ask_pattern) AS f_asked,
           bool_or(ledger_entry_id IS NOT NULL) AS f_changed
    FROM calls
    GROUP BY session_id
),
repeats AS (
    SELECT DISTINCT session_id
    FROM calls
    GROUP BY session_id, turn_id, tool_name, input_text
    HAVING count(*) >= 2
),
grouped AS (
    SELECT t.session_id, min(t.started_at) AS started_at, max(t.last_at) AS ended_at, count(*) AS turns,
           coalesce(bool_or(t.tool_call_count >= :many), false) AS f_many
    FROM turns t
    GROUP BY t.session_id
),
listed AS (
    SELECT g.session_id, g.started_at, g.ended_at, g.turns, g.f_many,
           f.parent_session_id, f.work_kind, f.context_kind, f.trigger_text AS first_message,
           CASE WHEN f.work_kind = ANY(CAST(:conversation_kinds AS text[])) THEN 'conversation'
                WHEN f.work_kind = ANY(CAST(:task_kinds AS text[])) THEN 'task'
                ELSE 'background' END AS kind,
           coalesce(cs.tool_calls, 0) AS tool_calls, coalesce(cs.errors, 0) AS errors,
           coalesce(cs.f_error, false) AS f_error, (r.session_id IS NOT NULL) AS f_repeat,
           coalesce(cs.f_asked, false) AS f_asked, coalesce(cs.f_changed, false) AS f_changed
    FROM grouped g
    JOIN first_turn f ON f.session_id = g.session_id
    LEFT JOIN call_stats cs ON cs.session_id = g.session_id
    LEFT JOIN repeats r ON r.session_id = g.session_id
),
titled AS (
    SELECT l.*, coalesce(
        (SELECT e.title FROM heart.episodes e
         WHERE e.agent_id = :agent AND e.session_id = l.session_id AND e.active AND coalesce(e.title, '') <> ''
         ORDER BY e.started_at DESC LIMIT 1),
        (SELECT left(st.task, :task_chars) FROM heart.subtasks st
         WHERE st.agent_id = :agent AND st.id = (
             SELECT c.subtask_id FROM calls c
             WHERE c.session_id = l.session_id AND c.subtask_id IS NOT NULL
             ORDER BY c.turn_started_at, c.turn_id, c.call_index LIMIT 1)),
        left(nullif(l.first_message, ''), :trigger_chars),
        :no_title) AS title
    FROM listed l
)
"""

_COUNTS_SQL = (
    _SESSIONS_CTE + "SELECT kind, count(*)::int AS sessions, sum(turns)::int AS turns FROM listed GROUP BY kind"
)
_TOOL_NAMES_SQL = _SESSIONS_CTE + "SELECT DISTINCT tool_name FROM calls ORDER BY tool_name"
# Fixed column per lens (the lens is validated against FLAGS before it gets here).
_LENS_COLUMN = {flag: f"f_{flag}" for flag in FLAGS}


def _page_sql(*, tool: bool, q: bool, lens: str | None, before: bool) -> str:
    where = ["kind = ANY(CAST(:kinds AS text[]))"]
    if tool:
        where.append("EXISTS (SELECT 1 FROM calls c WHERE c.session_id = titled.session_id AND c.tool_name = :tool)")
    if q:
        where.append("(title ILIKE :q ESCAPE '\\' OR first_message ILIKE :q ESCAPE '\\')")
    if lens is not None:
        where.append(_LENS_COLUMN[lens])
    if before:
        where.append("(started_at, session_id) < (CAST(:before_ts AS timestamptz), CAST(:before_key AS text))")
    return (
        _SESSIONS_CTE
        + """
SELECT p.*, tl.tool_list, tl.tool_counts
FROM (
    SELECT * FROM titled
    WHERE """
        + "\n      AND ".join(where)
        + """
    ORDER BY started_at DESC, session_id DESC
    LIMIT :fetch
) p
LEFT JOIN LATERAL (
    SELECT array_agg(x.tool_name ORDER BY x.calls DESC, x.tool_name) AS tool_list,
           array_agg(x.calls ORDER BY x.calls DESC, x.tool_name) AS tool_counts
    FROM (SELECT c.tool_name, count(*)::int AS calls FROM calls c WHERE c.session_id = p.session_id
          GROUP BY c.tool_name ORDER BY calls DESC, c.tool_name LIMIT :tools_cap) x
) tl ON true
ORDER BY p.started_at DESC, p.session_id DESC
"""
    )


def _session_out(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "session_id": row["session_id"],
        "parent_session_id": row["parent_session_id"],
        "kind": row["kind"],
        "work_kind": row["work_kind"],
        "context_kind": row["context_kind"],
        "title": row["title"],
        "first_message": row["first_message"],
        "started_at": _iso(row["started_at"]),
        "ended_at": _iso(row["ended_at"]),
        "turns": row["turns"],
        "tool_calls": row["tool_calls"],
        "errors": row["errors"],
        "tools": [
            {"tool": tool, "calls": calls, "lane": lane_of(tool)}
            for tool, calls in zip(row["tool_list"] or [], row["tool_counts"] or [], strict=True)
        ],
        "flags": [flag for flag in FLAGS if row[_LENS_COLUMN[flag]]],
    }


async def get_sessions(
    session: AsyncSession,
    agent_id: str,
    *,
    kind: str,
    days: int,
    tool: str | None,
    q: str | None,
    lens: str | None,
    limit: int,
    before: tuple[datetime, str] | None,
) -> dict[str, Any]:
    """The Work list: the sessions of the last ``days`` days, newest first, ``limit`` per page after ``before``.

    ``counts`` and ``tool_names`` cover the whole window and ignore ``kind``, ``tool``, ``q`` and ``lens``.
    """
    base = {
        "agent": agent_id,
        "days": days,
        "ask_pattern": ASK_PATTERN,
        "many": MANY_CALLS,
        "conversation_kinds": [k for k, g in WORK_KIND_GROUP.items() if g == "conversation"],
        "task_kinds": [k for k, g in WORK_KIND_GROUP.items() if g == "task"],
        "task_chars": TITLE_TASK_CHARS,
        "trigger_chars": TITLE_TRIGGER_CHARS,
        "no_title": NO_TITLE,
    }
    await _set_timeout(session)

    counts = {k: {"sessions": 0, "turns": 0} for k in KINDS}
    for row in (await session.execute(text(_COUNTS_SQL), base)).mappings():
        counts[row["kind"]] = {"sessions": row["sessions"], "turns": row["turns"]}

    tool_names = list((await session.execute(text(_TOOL_NAMES_SQL), base)).scalars())

    params: dict[str, Any] = {
        **base,
        "kinds": list(KIND_FILTERS[kind]),
        "tools_cap": TOOLS_PER_SESSION,
        "fetch": limit + 1,
    }
    if tool is not None:
        params["tool"] = tool
    if q is not None:
        params["q"] = _like_pattern(q)
    if before is not None:
        params["before_ts"], params["before_key"] = before
    sql = _page_sql(tool=tool is not None, q=q is not None, lens=lens, before=before is not None)
    rows = list((await session.execute(text(sql), params)).mappings())

    next_before = None
    if len(rows) > limit:
        rows = rows[:limit]
        next_before = format_cursor(rows[-1]["started_at"], rows[-1]["session_id"])
    return {
        "enabled": True,
        "window_days": days,
        "counts": counts,
        "tool_names": tool_names,
        "sessions": [_session_out(row) for row in rows],
        "next_before": next_before,
    }
