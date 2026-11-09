"""F108 Phase 2: the Work view's and the model-call view's read-only endpoints (GET /dashboard/work/... and
GET /dashboard/model-calls).

Pure functions of ``(session, agent_id, params)``: parameter parsing, SQL and shaping. ``nous/api/rest.py`` only
routes to them. They read the Phase 1 tables (``nous_system.agent_turn_log`` and ``nous_system.tool_call_log``,
migration 100; ``nous_system.model_call_log`` and ``nous_system.model_call_attempt``, migration 101) and, for the
trace and the titles, ``nous_system.execution_ledger``, ``heart.episodes`` and ``heart.subtasks``, through the app's own
session, agent-scoped, each statement under a 10-second timeout. A model call's stored text (migration 102) is read
only through its ``TextStore``: the detail lists its blocks without their bodies, and one block's body is read alone.
The text they return was redacted when it was stored; it is returned as stored and never logged.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from types import SimpleNamespace
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from nous.api.harness_dashboard import _is_tombstone
from nous.observability.model_call_text import BLOCK_PARTS

DAYS_DEFAULT = 7
DAYS_MAX = 30
SESSIONS_LIMIT_DEFAULT = 50
SESSIONS_LIMIT_MAX = 100
TRACE_LIMIT_DEFAULT = 100
TRACE_LIMIT_MAX = 200
MODEL_CALLS_PER_TURN = 20
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
# WORK_KIND_GROUP as the bound lists _kind_case_sql reads.
_KIND_PARAMS: dict[str, list[str]] = {
    "conversation_kinds": [k for k, g in WORK_KIND_GROUP.items() if g == "conversation"],
    "task_kinds": [k for k, g in WORK_KIND_GROUP.items() if g == "task"],
}


def _kind_case_sql(col: str) -> str:
    """The kind of work of the ``work_kind`` column ``col`` as SQL: WORK_KIND_GROUP through the bound lists of
    ``_KIND_PARAMS``, anything else background. The sessions list and the patterns both use it."""
    return (
        f"CASE WHEN {col} = ANY(CAST(:conversation_kinds AS text[])) THEN 'conversation' "
        f"WHEN {col} = ANY(CAST(:task_kinds AS text[])) THEN 'task' ELSE 'background' END"
    )


# A repeated call: two calls of one turn with the same tool and the same input. The sessions list's ``repeat`` flag
# and the patterns' wasted steps both group on this key, so the two cannot drift apart.
_REPEAT_KEY = "turn_id, tool_name, input_text"

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


def _parse_uuid_cursor(raw: str | None, what: str) -> tuple[datetime, str] | None:
    """A :func:`parse_cursor` cursor whose key is ``what`` (a UUID), the key returned in its canonical form."""
    cursor = parse_cursor(raw)
    if cursor is None:
        return None
    try:
        return cursor[0], str(UUID(cursor[1]))
    except ValueError as exc:
        raise ValueError(f"cursor key must be a {what} (a UUID)") from exc


def parse_trace_query(params: Mapping[str, str]) -> dict[str, Any]:
    """The ``limit`` and ``after`` keyword arguments of :func:`get_trace` from the request's query. The cursor's key
    is a turn id, so it must be a UUID (returned in its canonical form). Raises ValueError on a bad value."""
    after = _parse_uuid_cursor(params.get("after"), "turn id")
    return {"limit": parse_limit(params.get("limit"), TRACE_LIMIT_DEFAULT, TRACE_LIMIT_MAX), "after": after}


def _like_pattern(q: str) -> str:
    """A case-insensitive substring pattern for ``ILIKE ... ESCAPE '\\'`` that matches ``%``, ``_`` and ``\\``
    literally."""
    escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _iso(ts: datetime | None) -> str | None:
    return ts.isoformat() if ts is not None else None


def input_total_sql(alias: str) -> str:
    """A model call's input tokens as a SQL expression over ``model_call_log`` row ``alias``: NULL when the call
    recorded no usage (``input_uncached``, ``cache_read`` and ``cache_write`` all NULL), else ``input_total``. The
    table's ``input_total`` is a generated coalesce sum, so it reads 0, never NULL, for a call that failed before any
    usage came back."""
    return (
        f"CASE WHEN num_nonnulls({alias}.input_uncached, {alias}.cache_read, {alias}.cache_write) > 0 "
        f"THEN {alias}.input_total END"
    )


async def _set_timeout(session: AsyncSession) -> None:
    # SET LOCAL binds the transaction the session autobegan; it holds only while the caller runs every statement in
    # that one transaction, so never commit (or open a second session) between the statements of a handler.
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
# message and parent come from its first turn (started_at, then id). Unreferenced CTEs are not run. tool_counts is
# MATERIALIZED so the page's per-row tools lookup reads the aggregate once instead of re-aggregating every call.
_SESSIONS_CTE = f"""
WITH turns AS (
    SELECT t.id, t.session_id, t.parent_session_id, t.work_kind, t.context_kind, t.trigger_text,
           t.tool_call_count, t.started_at, coalesce(t.ended_at, t.started_at) AS last_at
    FROM nous_system.agent_turn_log t
    WHERE t.agent_id = :agent AND t.session_id IS NOT NULL
      AND t.started_at >= now() - make_interval(days => :days)
),
first_turn AS (
    SELECT DISTINCT ON (session_id) session_id, parent_session_id, work_kind, context_kind, trigger_text,
           {_kind_case_sql("work_kind")} AS kind
    FROM turns
    ORDER BY session_id, started_at, id
),
calls AS (
    SELECT c.turn_id, c.call_index, c.tool_name, c.input_text, c.is_error, c.ledger_entry_id, c.subtask_id,
           t.session_id, t.started_at AS turn_started_at
    FROM nous_system.tool_call_log c
    JOIN turns t ON t.id = c.turn_id
    WHERE c.agent_id = :agent
      -- Redundant, so it can only narrow the scan. The runner stamps a turn's start before any of its calls start
      -- (both datetime.now), and a call written without a start gets now() when written, later still, so every
      -- call of a window turn is itself in the window. Only a wall-clock step back between the two, at the very
      -- edge of the window, could drop one.
      AND c.started_at >= now() - make_interval(days => :days)
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
    GROUP BY session_id, {_REPEAT_KEY}
    HAVING count(*) >= 2
),
tool_counts AS MATERIALIZED (
    SELECT session_id, tool_name, count(*)::int AS calls
    FROM calls
    GROUP BY session_id, tool_name
),
first_subtask AS (
    SELECT DISTINCT ON (session_id) session_id, subtask_id
    FROM calls
    WHERE subtask_id IS NOT NULL
    ORDER BY session_id, turn_started_at, turn_id, call_index
),
grouped AS (
    SELECT t.session_id, min(t.started_at) AS started_at, max(t.last_at) AS ended_at, count(*) AS turns,
           coalesce(bool_or(t.tool_call_count >= :many), false) AS f_many
    FROM turns t
    GROUP BY t.session_id
),
listed AS (
    SELECT g.session_id, g.started_at, g.ended_at, g.turns, g.f_many,
           f.parent_session_id, f.work_kind, f.context_kind, f.trigger_text AS first_message, f.kind,
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
        left(st.task, :task_chars),
        left(nullif(l.first_message, ''), :trigger_chars),
        :no_title) AS title
    FROM listed l
    LEFT JOIN first_subtask fs ON fs.session_id = l.session_id
    LEFT JOIN heart.subtasks st ON st.id = fs.subtask_id AND st.agent_id = :agent
)
"""

# Counts need only the turns: no call is read.
_COUNTS_SQL = (
    _SESSIONS_CTE
    + "SELECT f.kind, count(*)::int AS sessions, sum(g.turns)::int AS turns "
    + "FROM grouped g JOIN first_turn f ON f.session_id = g.session_id GROUP BY f.kind"
)
_TOOL_NAMES_SQL = _SESSIONS_CTE + "SELECT DISTINCT tool_name FROM calls ORDER BY tool_name"
# Fixed column per lens (the lens is validated against FLAGS before it gets here).
_LENS_COLUMN = {flag: f"f_{flag}" for flag in FLAGS}


def _page_sql(*, tool: bool, q: bool, lens: str | None, before: bool) -> str:
    where = ["kind = ANY(CAST(:kinds AS text[]))"]
    if tool:
        where.append(
            "EXISTS (SELECT 1 FROM tool_counts tc WHERE tc.session_id = titled.session_id AND tc.tool_name = :tool)"
        )
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
    FROM (SELECT tc.tool_name, tc.calls FROM tool_counts tc WHERE tc.session_id = p.session_id
          ORDER BY tc.calls DESC, tc.tool_name LIMIT :tools_cap) x
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
        **_KIND_PARAMS,
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


# ---------------------------------------------------------------------------------------------------------------------
# GET /dashboard/work/sessions/{session_id}
# ---------------------------------------------------------------------------------------------------------------------


def disabled_trace() -> dict[str, Any]:
    """The trace with the tool-call log off: the same keys as with it on, every list empty."""
    return {"enabled": False, "session": None, "turns": [], "next_after": None}


# A trace's turns: the session's turn rows, and, as synthetic turns, the turn ids of the session's calls that have no
# turn row (execute_single_call writes its calls under a turn id of their own and never a turn row). The two parts
# are disjoint (NOT EXISTS), hence UNION ALL. The header and the page both read this union, so the header's counts
# agree with the body. A synthetic turn starts at its first call and ends at its last.
_TRACE_TURNS_CTE = """
WITH trace_turns AS (
    SELECT t.id, t.started_at, coalesce(t.ended_at, t.started_at) AS last_at, true AS has_turn_row
    FROM nous_system.agent_turn_log t
    WHERE t.agent_id = :agent AND t.session_id = :sid
    UNION ALL
    SELECT c.turn_id, min(c.started_at), max(c.started_at), false
    FROM nous_system.tool_call_log c
    WHERE c.agent_id = :agent AND c.session_id = :sid AND c.turn_id IS NOT NULL
      AND NOT EXISTS (SELECT 1 FROM nous_system.agent_turn_log t WHERE t.id = c.turn_id AND t.agent_id = :agent)
    GROUP BY c.turn_id
)
"""

# The session's header. Its calls are the calls of the union's turns (joined on turn_id), as in the list. kind,
# work_kind and parent come from the first turn row (as in the list), or from the first call when the session has no
# turn row. The title follows the list's rule.
_TRACE_HEADER_SQL = (
    _TRACE_TURNS_CTE
    + """,
trace_calls AS (
    SELECT c.turn_id, c.call_index, c.is_error, c.subtask_id, c.work_kind, c.parent_session_id,
           tt.started_at AS turn_started_at
    FROM nous_system.tool_call_log c
    JOIN trace_turns tt ON tt.id = c.turn_id
    WHERE c.agent_id = :agent
      -- Redundant, so it can only narrow the scan to the session's calls. The runner writes a turn's calls with
      -- the turn's own session id (one turn-local value), and a synthetic turn is made of this session's calls.
      AND c.session_id = :sid
),
first_turn AS (
    SELECT t.parent_session_id, t.work_kind, t.trigger_text
    FROM nous_system.agent_turn_log t
    WHERE t.agent_id = :agent AND t.session_id = :sid
    ORDER BY t.started_at, t.id
    LIMIT 1
),
first_call AS (
    SELECT parent_session_id, work_kind FROM trace_calls ORDER BY turn_started_at, turn_id, call_index LIMIT 1
),
first_subtask AS (
    SELECT subtask_id FROM trace_calls WHERE subtask_id IS NOT NULL
    ORDER BY turn_started_at, turn_id, call_index LIMIT 1
)
SELECT
    (EXISTS (SELECT 1 FROM nous_system.agent_turn_log WHERE agent_id = :agent AND session_id = :sid)
     OR EXISTS (SELECT 1 FROM nous_system.tool_call_log WHERE agent_id = :agent AND session_id = :sid)) AS known,
    tt.turns, tt.started_at, tt.ended_at, tc.tool_calls, tc.errors,
    coalesce(ft.work_kind, fc.work_kind) AS work_kind,
    CASE WHEN ft.work_kind IS NOT NULL THEN ft.parent_session_id ELSE fc.parent_session_id END AS parent_session_id,
    coalesce(
        (SELECT e.title FROM heart.episodes e
         WHERE e.agent_id = :agent AND e.session_id = :sid AND e.active AND coalesce(e.title, '') <> ''
         ORDER BY e.started_at DESC LIMIT 1),
        left(st.task, :task_chars),
        left(nullif(ft.trigger_text, ''), :trigger_chars),
        :no_title) AS title
FROM (SELECT count(*)::int AS turns, min(started_at) AS started_at, max(last_at) AS ended_at FROM trace_turns) tt
CROSS JOIN (SELECT count(*)::int AS tool_calls, (count(*) FILTER (WHERE is_error))::int AS errors
            FROM trace_calls) tc
LEFT JOIN first_turn ft ON true
LEFT JOIN first_call fc ON true
LEFT JOIN first_subtask fs ON true
LEFT JOIN heart.subtasks st ON st.id = fs.subtask_id AND st.agent_id = :agent
"""
)


def _trace_page_sql(*, after: bool) -> str:
    """The page's turns: keyset and LIMIT on the union, then the turn row's columns by primary key (NULL for a
    synthetic turn)."""
    where = "WHERE (started_at, id) > (CAST(:after_ts AS timestamptz), CAST(:after_id AS uuid))" if after else ""
    return (
        _TRACE_TURNS_CTE
        + """
SELECT p.id, p.started_at, p.has_turn_row, t.turn_number, t.ended_at, t.frame_id, t.trigger_text, t.memory_context,
       t.critic, t.claim_check, t.reply_text, t.error, t.tool_call_count
FROM (SELECT id, started_at, has_turn_row FROM trace_turns
      """
        + where
        + """
      ORDER BY started_at, id
      LIMIT :fetch) p
LEFT JOIN nous_system.agent_turn_log t ON t.id = p.id AND t.agent_id = :agent
ORDER BY p.started_at, p.id
"""
    )


# The page's calls in (turn_id, call_index) order, each with its execution_ledger row when this agent has one.
_TRACE_CALLS_SQL = """
SELECT c.id, c.turn_id, c.call_index, c.tool_name, c.tool_name_called, c.reason, c.input_text, c.result_preview,
       c.outcome, c.is_error, c.error_text, c.refused_by, c.duration_ms, c.started_at,
       l.id AS ledger_id, l.status AS ledger_status, l.side_effect_type, l.external_ref, l.idempotency_key, l.key_args
FROM nous_system.tool_call_log c
LEFT JOIN nous_system.execution_ledger l ON l.id = c.ledger_entry_id AND l.agent_id = :agent
WHERE c.agent_id = :agent AND c.turn_id = ANY(CAST(:turn_ids AS uuid[]))
ORDER BY c.turn_id, c.call_index, c.id
"""

# The model calls that carry one of the page's turn ids (main-loop calls: background calls carry no turn id), the
# first few of each turn. input_total is null for a call with no usage (input_total_sql).
_TRACE_MODEL_CALLS_SQL = f"""
SELECT id, turn_id, purpose, model, status, input_total, output_tokens
FROM (
    SELECT m.id, m.turn_id, m.purpose, m.model, m.status, {input_total_sql("m")} AS input_total, m.output_tokens,
           m.started_at, row_number() OVER (PARTITION BY m.turn_id ORDER BY m.started_at, m.id) AS n
    FROM nous_system.model_call_log m
    WHERE m.agent_id = :agent AND m.turn_id = ANY(CAST(:turn_ids AS uuid[]))
) x
WHERE n <= :per_turn
ORDER BY turn_id, started_at, id
"""


def _ledger_out(row: Mapping[str, Any]) -> dict[str, Any] | None:
    if row["ledger_id"] is None:
        return None
    return {
        "id": str(row["ledger_id"]),
        "status": row["ledger_status"],
        "side_effect_type": row["side_effect_type"],
        "external_ref": row["external_ref"],
        "tombstone": _is_tombstone(
            SimpleNamespace(
                idempotency_key=row["idempotency_key"], status=row["ledger_status"], key_args=row["key_args"]
            )
        ),
    }


def _call_out(row: Mapping[str, Any]) -> dict[str, Any]:
    tool = row["tool_name"]
    return {
        "id": str(row["id"]),
        "call_index": row["call_index"],
        "tool": tool,
        "tool_called": row["tool_name_called"] if row["tool_name_called"] != tool else None,
        "lane": lane_of(tool),
        "reason": row["reason"],
        "input": row["input_text"],
        "result": row["result_preview"],
        "outcome": row["outcome"],
        "is_error": row["is_error"],
        "error": row["error_text"],
        "refused_by": row["refused_by"],
        "duration_ms": row["duration_ms"],
        "started_at": _iso(row["started_at"]),
        "ledger": _ledger_out(row),
    }


def _turn_out(row: Mapping[str, Any], calls: list[dict[str, Any]], model_calls: list[dict[str, Any]]) -> dict[str, Any]:
    count = row["tool_call_count"]
    return {
        "turn_id": str(row["id"]),
        "turn_number": row["turn_number"],
        "has_turn_row": row["has_turn_row"],
        "started_at": _iso(row["started_at"]),
        "ended_at": _iso(row["ended_at"]),
        "frame_id": row["frame_id"],
        "trigger": row["trigger_text"],
        "memory": row["memory_context"],
        "critic": row["critic"],
        "claim_check": row["claim_check"],
        "reply": row["reply_text"],
        "error": row["error"],
        # The runner's count when the turn row has one, else the calls listed (a synthetic turn has no count).
        "tool_call_count": count if count is not None else len(calls),
        "model_calls": model_calls,
        "calls": calls,
    }


async def get_trace(
    session: AsyncSession,
    agent_id: str,
    session_id: str,
    *,
    limit: int,
    after: tuple[datetime, str] | None,
    model_calls_enabled: bool,
) -> dict[str, Any] | None:
    """One session's trace: its header and ``limit`` turns after ``after``, oldest first, each with its calls and
    (when the model-call log is on) its main-loop model calls. None when this agent has no turn row and no call of
    the session."""
    await _set_timeout(session)
    header = (
        (
            await session.execute(
                text(_TRACE_HEADER_SQL),
                {
                    "agent": agent_id,
                    "sid": session_id,
                    "task_chars": TITLE_TASK_CHARS,
                    "trigger_chars": TITLE_TRIGGER_CHARS,
                    "no_title": NO_TITLE,
                },
            )
        )
        .mappings()
        .one()
    )
    if not header["known"]:
        return None

    params: dict[str, Any] = {"agent": agent_id, "sid": session_id, "fetch": limit + 1}
    if after is not None:
        params["after_ts"], params["after_id"] = after
    rows = list((await session.execute(text(_trace_page_sql(after=after is not None)), params)).mappings())
    next_after = None
    if len(rows) > limit:
        rows = rows[:limit]
        next_after = format_cursor(rows[-1]["started_at"], str(rows[-1]["id"]))

    turn_ids = [row["id"] for row in rows]
    calls: dict[str, list[dict[str, Any]]] = {str(t): [] for t in turn_ids}
    model_calls: dict[str, list[dict[str, Any]]] = {str(t): [] for t in turn_ids}
    if turn_ids:
        ids = {"agent": agent_id, "turn_ids": turn_ids}
        for row in (await session.execute(text(_TRACE_CALLS_SQL), ids)).mappings():
            calls[str(row["turn_id"])].append(_call_out(row))
        if model_calls_enabled:
            per_turn = {**ids, "per_turn": MODEL_CALLS_PER_TURN}
            for row in (await session.execute(text(_TRACE_MODEL_CALLS_SQL), per_turn)).mappings():
                model_calls[str(row["turn_id"])].append(
                    {
                        "id": str(row["id"]),
                        "purpose": row["purpose"],
                        "model": row["model"],
                        "status": row["status"],
                        "input_total": row["input_total"],
                        "output_tokens": row["output_tokens"],
                    }
                )

    work_kind = header["work_kind"]
    return {
        "enabled": True,
        "session": {
            "session_id": session_id,
            "parent_session_id": header["parent_session_id"],
            "kind": WORK_KIND_GROUP.get(work_kind or "", "background"),
            "work_kind": work_kind,
            "title": header["title"],
            "started_at": _iso(header["started_at"]),
            "ended_at": _iso(header["ended_at"]),
            "turns": header["turns"],
            "tool_calls": header["tool_calls"],
            "errors": header["errors"],
        },
        "turns": [_turn_out(row, calls[str(row["id"])], model_calls[str(row["id"])]) for row in rows],
        "next_after": next_after,
    }


# ---------------------------------------------------------------------------------------------------------------------
# GET /dashboard/work/patterns
# ---------------------------------------------------------------------------------------------------------------------

PATTERN_TOOLS = 30
PATTERN_REASONS = 3
FAILING_MIN_CALLS = 20
FAILING_TOOLS = 8


def disabled_patterns(days: int) -> dict[str, Any]:
    """The patterns with the tool-call log off: the same keys as with it on, every list empty, every count zero."""
    return {
        "enabled": False,
        "window_days": days,
        "reason_coverage": {"calls": 0, "with_reason": 0},
        "tools": [],
        "failing": [],
        "wasted": {"repeated_calls": 0, "retried_and_failed_again": 0},
    }


# The window's calls of this agent. Every patterns statement reads only this, so each is a range scan of
# idx_tool_call_log_agent_started (agent_id, started_at DESC), never the whole table. A call's kind of work is its
# own work_kind through WORK_KIND_GROUP; a blank reason is no reason.
_PATTERNS_WINDOW_CTE = f"""
WITH win AS (
    SELECT c.id, c.turn_id, c.call_index, c.session_id, c.tool_name, c.input_text, c.is_error, c.started_at,
           nullif(btrim(c.reason), '') AS reason, {_kind_case_sql("c.work_kind")} AS kind
    FROM nous_system.tool_call_log c
    WHERE c.agent_id = :agent AND c.started_at >= now() - make_interval(days => :days)
)
"""

# The most used tools, with their calls and turns per kind of work (a turnless call counts as a call, not a turn).
_PATTERNS_TOOLS_SQL = (
    _PATTERNS_WINDOW_CTE
    + "SELECT tool_name, count(*)::int AS calls, (count(*) FILTER (WHERE is_error))::int AS errors, "
    + "count(DISTINCT session_id)::int AS sessions, "
    + ", ".join(
        f"(count(*) FILTER (WHERE kind = '{k}'))::int AS {k}_calls, "
        f"(count(DISTINCT turn_id) FILTER (WHERE kind = '{k}'))::int AS {k}_turns"
        for k in KINDS
    )
    + " FROM win GROUP BY tool_name ORDER BY calls DESC, tool_name LIMIT :tools_cap"
)

# The listed tools' most common reasons: grouped by the trimmed, lowercased text, shown as the first seen (by
# started_at, not min(), which follows the database's collation), most frequent first, ties to the first seen.
_PATTERNS_REASONS_SQL = (
    _PATTERNS_WINDOW_CTE
    + """
SELECT tool_name, reason_text, n
FROM (
    SELECT tool_name, reason_text, n,
           row_number() OVER (PARTITION BY tool_name ORDER BY n DESC, first_at, reason_key) AS rank
    FROM (
        SELECT tool_name, lower(reason) AS reason_key, count(*)::int AS n, min(started_at) AS first_at,
               (array_agg(reason ORDER BY started_at, id))[1] AS reason_text
        FROM win
        WHERE reason IS NOT NULL AND tool_name = ANY(CAST(:tools AS text[]))
        GROUP BY tool_name, lower(reason)
    ) g
) r
WHERE rank <= :reasons_cap
ORDER BY tool_name, rank
"""
)

# Every tool of the window (not only the listed ones) with enough calls that failed at all, worst rate first.
_PATTERNS_FAILING_SQL = (
    _PATTERNS_WINDOW_CTE
    + """
SELECT tool_name, count(*)::int AS calls, (count(*) FILTER (WHERE is_error))::int AS errors
FROM win
GROUP BY tool_name
HAVING count(*) >= :failing_min AND count(*) FILTER (WHERE is_error) > 0
ORDER BY (count(*) FILTER (WHERE is_error))::float8 / count(*) DESC, count(*) DESC, tool_name
LIMIT :failing_cap
"""
)

# Reason coverage over every call of the window, and the wasted steps within a turn: prev_error is the is_error of
# the turn's previous identical call (same tool, same input, by call_index), NULL for the first. A call that has one
# is a repeat; an error after an identical error is a retry that failed again. Turnless calls have no turn to repeat
# in.
_PATTERNS_TOTALS_SQL = (
    _PATTERNS_WINDOW_CTE
    + f"""
SELECT cov.calls, cov.with_reason, w.repeated_calls, w.retried_and_failed_again
FROM (SELECT count(*)::int AS calls, count(reason)::int AS with_reason FROM win) cov
CROSS JOIN (
    SELECT (count(*) FILTER (WHERE prev_error IS NOT NULL))::int AS repeated_calls,
           (count(*) FILTER (WHERE is_error AND prev_error))::int AS retried_and_failed_again
    FROM (
        SELECT is_error,
               lag(is_error) OVER (PARTITION BY {_REPEAT_KEY} ORDER BY call_index, id) AS prev_error
        FROM win
        WHERE turn_id IS NOT NULL
    ) x
) w
"""
)


async def get_patterns(session: AsyncSession, agent_id: str, *, days: int) -> dict[str, Any]:
    """Tool use over the last ``days`` days: the most used tools by kind of work with their common reasons, the
    tools that fail most, and the wasted steps."""
    base = {
        "agent": agent_id,
        "days": days,
        **_KIND_PARAMS,
    }
    await _set_timeout(session)

    tool_rows = list(
        (await session.execute(text(_PATTERNS_TOOLS_SQL), {**base, "tools_cap": PATTERN_TOOLS})).mappings()
    )
    reasons: dict[str, list[dict[str, Any]]] = {row["tool_name"]: [] for row in tool_rows}
    if tool_rows:
        params = {**base, "tools": list(reasons), "reasons_cap": PATTERN_REASONS}
        for row in (await session.execute(text(_PATTERNS_REASONS_SQL), params)).mappings():
            reasons[row["tool_name"]].append({"text": row["reason_text"], "count": row["n"]})

    params = {**base, "failing_min": FAILING_MIN_CALLS, "failing_cap": FAILING_TOOLS}
    failing = [
        {
            "tool": row["tool_name"],
            "calls": row["calls"],
            "errors": row["errors"],
            "error_rate": row["errors"] / row["calls"],
        }
        for row in (await session.execute(text(_PATTERNS_FAILING_SQL), params)).mappings()
    ]
    totals = (await session.execute(text(_PATTERNS_TOTALS_SQL), base)).mappings().one()

    return {
        "enabled": True,
        "window_days": days,
        "reason_coverage": {"calls": totals["calls"], "with_reason": totals["with_reason"]},
        "tools": [
            {
                "tool": row["tool_name"],
                "lane": lane_of(row["tool_name"]),
                "calls": row["calls"],
                "errors": row["errors"],
                "sessions": row["sessions"],
                "by_kind": {k: {"turns": row[f"{k}_turns"], "calls": row[f"{k}_calls"]} for k in KINDS},
                "reasons": reasons[row["tool_name"]],
            }
            for row in tool_rows
        ],
        "failing": failing,
        "wasted": {
            "repeated_calls": totals["repeated_calls"],
            "retried_and_failed_again": totals["retried_and_failed_again"],
        },
    }


# ---------------------------------------------------------------------------------------------------------------------
# GET /dashboard/model-calls
# ---------------------------------------------------------------------------------------------------------------------

MODEL_CALLS_LIMIT_DEFAULT = 50
MODEL_CALLS_LIMIT_MAX = 100
ERROR_PREVIEW_CHARS = 300
# The status filter: a stored status, or ``problem`` (the calls that have a ``problem`` text, see _PROBLEM_SQL).
MODEL_CALL_STATUSES: tuple[str, ...] = ("ok", "error", "cancelled", "problem")
# The call kinds the recorder counts per minute: one row per minute, purpose, model and status, holding call_count
# calls, their tokens summed in input_uncached.
BUCKET_CALL_KINDS: tuple[str, ...] = ("embedding", "rerank")


def parse_model_calls_query(params: Mapping[str, str]) -> dict[str, Any]:
    """The keyword arguments of :func:`get_model_calls`, all but ``text_enabled``, from the request's query. The
    cursor's key is a call id, so it must be a UUID (returned in its canonical form). Raises ValueError on a bad
    value."""
    status = params.get("status") or None
    if status is not None and status not in MODEL_CALL_STATUSES:
        raise ValueError(f"status must be one of {', '.join(MODEL_CALL_STATUSES)}")
    raw_turn = params.get("turn_id") or None
    try:
        turn_id = UUID(raw_turn) if raw_turn is not None else None
    except ValueError as exc:
        raise ValueError("turn_id must be a UUID") from exc
    return {
        "days": parse_days(params.get("days"), DAYS_DEFAULT, DAYS_MAX),
        "purpose": params.get("purpose") or None,
        "model": params.get("model") or None,
        "session_id": params.get("session_id") or None,
        "turn_id": turn_id,
        "status": status,
        "limit": parse_limit(params.get("limit"), MODEL_CALLS_LIMIT_DEFAULT, MODEL_CALLS_LIMIT_MAX),
        "before": _parse_uuid_cursor(params.get("before"), "call id"),
    }


def disabled_model_calls(days: int, text_enabled: bool) -> dict[str, Any]:
    """The model-call list with the model-call log off: the same keys as with it on, every list empty."""
    return {
        "enabled": False,
        "text_enabled": text_enabled,
        "window_days": days,
        "purposes": [],
        "calls": [],
        "next_before": None,
    }


# This agent's model calls of the window. Every statement carries both bounds, so it is a range scan of
# idx_model_call_log_agent_started (agent_id, started_at DESC), or, with a filter, of
# idx_model_call_log_agent_purpose_started, idx_model_call_log_agent_session_started or idx_model_call_log_turn.
_MODEL_CALLS_WINDOW = "m.agent_id = :agent AND m.started_at >= now() - make_interval(days => :days)"

# The purpose chips: every call of the window, whatever the filters. A minute bucket counts as its call_count calls
# (failed ones too); input_total sums the generated column, so a call with no usage adds 0, never null.
_MODEL_CALL_PURPOSES_SQL = f"""
SELECT m.purpose,
       sum(m.call_count) AS calls,
       sum(m.input_total) AS input_total,
       coalesce(sum(m.output_tokens), 0) AS output_tokens,
       coalesce(sum(m.call_count) FILTER (WHERE m.status = 'error'), 0) AS errors,
       bool_or(m.call_kind = ANY(CAST(:bucket_kinds AS text[]))) AS bucketed
FROM nous_system.model_call_log m
WHERE {_MODEL_CALLS_WINDOW}
GROUP BY m.purpose
ORDER BY calls DESC, m.purpose
"""

# What _model_call_out reads of a model_call_log row ``m``: a list item's columns (the detail reads them too).
_MODEL_CALL_COLUMNS = f"""m.id, m.started_at, m.purpose, m.label_source, m.call_kind, m.model, m.status, m.http_status,
       m.stop_reason, m.input_uncached, m.cache_read, m.cache_write, {input_total_sql("m")} AS input_total,
       m.output_tokens, m.duration_ms, m.attempts, m.call_count, m.session_id, m.turn_id, m.turn_number, m.is_fallback,
       left(m.error_text, :error_chars) AS error"""

# A call with a problem: it failed, was sent more than once, was sent on a fallback path, or recorded no purpose.
# _problem names the first of these; the two must list the same reasons.
_PROBLEM_SQL = "(m.status = 'error' OR m.attempts > 1 OR m.is_fallback OR m.label_source = 'none')"


def _model_calls_page_sql(
    *, purpose: bool, model: bool, session_id: bool, turn_id: bool, status: str | None, before: bool
) -> str:
    # Minute buckets are not calls of their own: the page lists them only when the purpose filter names theirs.
    where = [
        _MODEL_CALLS_WINDOW,
        "m.purpose = :purpose" if purpose else "NOT m.call_kind = ANY(CAST(:bucket_kinds AS text[]))",
    ]
    if model:
        where.append("m.model = :model")
    if session_id:
        where.append("m.session_id = :session_id")
    if turn_id:
        where.append("m.turn_id = CAST(:turn_id AS uuid)")
    if status == "problem":
        where.append(_PROBLEM_SQL)
    elif status is not None:
        where.append("m.status = :status")
    if before:
        where.append("(m.started_at, m.id) < (CAST(:before_ts AS timestamptz), CAST(:before_id AS uuid))")
    return (
        f"""
SELECT {_MODEL_CALL_COLUMNS}
FROM nous_system.model_call_log m
WHERE """
        + "\n  AND ".join(where)
        + """
ORDER BY m.started_at DESC, m.id DESC
LIMIT :fetch
"""
    )


def _problem(row: Mapping[str, Any]) -> str | None:
    """What went wrong with a call, the first reason of _PROBLEM_SQL that applies, else None."""
    if row["status"] == "error":
        return f"Failed (HTTP {row['http_status']})" if row["http_status"] is not None else "Failed"
    if row["attempts"] > 1:
        return f"Sent {row['attempts']} times"
    if row["is_fallback"]:
        return "Sent again on a fallback path"
    if row["label_source"] == "none":
        return "No purpose recorded"
    return None


def _model_call_out(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "started_at": _iso(row["started_at"]),
        "purpose": row["purpose"],
        "label_source": row["label_source"],
        "call_kind": row["call_kind"],
        "model": row["model"],
        "status": row["status"],
        "http_status": row["http_status"],
        "stop_reason": row["stop_reason"],
        "input_uncached": row["input_uncached"],
        "cache_read": row["cache_read"],
        "cache_write": row["cache_write"],
        "input_total": row["input_total"],
        "output_tokens": row["output_tokens"],
        "duration_ms": row["duration_ms"],
        "attempts": row["attempts"],
        "call_count": row["call_count"],
        "session_id": row["session_id"],
        "turn_id": str(row["turn_id"]) if row["turn_id"] is not None else None,
        "turn_number": row["turn_number"],
        "error": row["error"],
        "problem": _problem(row),
    }


async def get_model_calls(
    session: AsyncSession,
    agent_id: str,
    *,
    days: int,
    purpose: str | None,
    model: str | None,
    session_id: str | None,
    turn_id: UUID | None,
    status: str | None,
    limit: int,
    before: tuple[datetime, str] | None,
    text_enabled: bool,
) -> dict[str, Any]:
    """The model-call list: this agent's model calls of the last ``days`` days, every purpose (read from
    ``model_call_log``, not only the main loop's calls), newest first, ``limit`` per page after ``before``.
    ``purposes`` covers the whole window and ignores the filters. ``text_enabled`` is passed through for the UI."""
    base = {"agent": agent_id, "days": days, "bucket_kinds": list(BUCKET_CALL_KINDS)}
    await _set_timeout(session)

    purposes = [
        {
            "purpose": row["purpose"],
            "calls": row["calls"],
            "input_total": row["input_total"],
            "output_tokens": row["output_tokens"],
            "errors": row["errors"],
            "bucketed": row["bucketed"],
        }
        for row in (await session.execute(text(_MODEL_CALL_PURPOSES_SQL), base)).mappings()
    ]

    params: dict[str, Any] = {**base, "error_chars": ERROR_PREVIEW_CHARS, "fetch": limit + 1}
    if purpose is not None:
        params["purpose"] = purpose
    if model is not None:
        params["model"] = model
    if session_id is not None:
        params["session_id"] = session_id
    if turn_id is not None:
        params["turn_id"] = turn_id
    if status is not None and status != "problem":
        params["status"] = status
    if before is not None:
        params["before_ts"], params["before_id"] = before
    sql = _model_calls_page_sql(
        purpose=purpose is not None,
        model=model is not None,
        session_id=session_id is not None,
        turn_id=turn_id is not None,
        status=status,
        before=before is not None,
    )
    rows = list((await session.execute(text(sql), params)).mappings())

    next_before = None
    if len(rows) > limit:
        rows = rows[:limit]
        next_before = format_cursor(rows[-1]["started_at"], str(rows[-1]["id"]))
    return {
        "enabled": True,
        "text_enabled": text_enabled,
        "window_days": days,
        "purposes": purposes,
        "calls": [_model_call_out(row) for row in rows],
        "next_before": next_before,
    }


# ---------------------------------------------------------------------------------------------------------------------
# GET /dashboard/model-calls/{call_id} and /dashboard/model-calls/{call_id}/blocks/{part}/{ord}
# ---------------------------------------------------------------------------------------------------------------------

# A block's ord is an INT column: a larger one cannot exist (and would not bind).
BLOCK_ORD_MAX = 2**31 - 1


def parse_call_id(raw: str) -> UUID:
    """A model call id from the path. Raises ValueError unless it is a UUID."""
    try:
        return UUID(raw)
    except ValueError as exc:
        raise ValueError("call id must be a UUID") from exc


def disabled_model_call(text_enabled: bool) -> dict[str, Any]:
    """The model-call detail with the model-call log off: the same keys as with it on, nothing in them."""
    return {
        "enabled": False,
        "text_enabled": text_enabled,
        "call": None,
        "attempts": [],
        "sections": [],
        "turn": None,
        "text_status": "off",
        "blocks": [],
    }


# One call by its primary key, agent-scoped, with the detail's columns, whether it is older than the text retention,
# and its session's title (the latest active titled episode, as the sessions list's title reads it). The call's
# session is its turn's: the runner stamps both from the same turn (Runner._call_context).
_MODEL_CALL_DETAIL_SQL = f"""
SELECT {_MODEL_CALL_COLUMNS},
       m.group_id, m.parent_session_id, m.frame_id, m.subtask_id, m.dag_id, m.trace_id, m.error_text,
       m.context_window_size, m.total_tokens_est, m.utilization_pct, m.tools_count, m.tool_names, m.messages_count,
       m.loaded_facts, m.loaded_decisions, m.loaded_procedures, m.loaded_episodes, m.cache_break,
       m.cache_break_components, m.cache_break_tokens_lost, m.section_tokens_est,
       m.started_at < now() - make_interval(days => :retention_days) AS past_text_retention,
       (SELECT e.title FROM heart.episodes e
        WHERE e.agent_id = :agent AND e.session_id = m.session_id AND e.active AND coalesce(e.title, '') <> ''
        ORDER BY e.started_at DESC LIMIT 1) AS session_title
FROM nous_system.model_call_log m
WHERE m.id = CAST(:call_id AS uuid) AND m.agent_id = :agent
"""
# The call's HTTP attempts, on the primary key (call_id, attempt_no).
_MODEL_CALL_ATTEMPTS_SQL = """
SELECT a.attempt_no, a.started_at, a.duration_ms, a.http_status, a.error_type, a.error_text
FROM nous_system.model_call_attempt a
WHERE a.call_id = CAST(:call_id AS uuid) AND a.agent_id = :agent
ORDER BY a.attempt_no
"""


def _uuid_str(value: UUID | None) -> str | None:
    return str(value) if value is not None else None


async def get_model_call(
    session: AsyncSession,
    agent_id: str,
    call_id: UUID,
    *,
    text_store: Any | None,
    text_retention_days: int,
) -> dict[str, Any] | None:
    """One model call of this agent: every list field and the call's context, its HTTP attempts, its context
    sections (``section_tokens_est``, largest first) and its turn. None for an unknown or another agent's call.

    ``text_status``: ``off`` without ``text_store``. With one (the text is on), the status of a call without stored
    blocks: ``expired`` when it is older than ``text_retention_days`` (0 means the text is never swept), else
    ``not_recorded``; ``blocks`` is empty. :func:`add_model_call_blocks`, awaited once this session is closed, lists
    the call's blocks and makes the status ``ok`` when it has any."""
    params = {"agent": agent_id, "call_id": call_id, "error_chars": ERROR_PREVIEW_CHARS}
    await _set_timeout(session)
    row = (
        (await session.execute(text(_MODEL_CALL_DETAIL_SQL), {**params, "retention_days": text_retention_days}))
        .mappings()
        .one_or_none()
    )
    if row is None:
        return None
    attempts = [
        {
            "attempt_no": a["attempt_no"],
            "started_at": _iso(a["started_at"]),
            "duration_ms": a["duration_ms"],
            "http_status": a["http_status"],
            "error_type": a["error_type"],
            "error_text": a["error_text"],
        }
        for a in (await session.execute(text(_MODEL_CALL_ATTEMPTS_SQL), params)).mappings()
    ]

    if not text_store:
        text_status = "off"
    elif text_retention_days > 0 and row["past_text_retention"]:
        text_status = "expired"
    else:
        text_status = "not_recorded"

    sections = sorted((row["section_tokens_est"] or {}).items(), key=lambda item: (-item[1], item[0]))
    return {
        "enabled": True,
        "text_enabled": bool(text_store),
        "call": {
            **_model_call_out(row),
            "group_id": _uuid_str(row["group_id"]),
            "is_fallback": row["is_fallback"],
            "parent_session_id": row["parent_session_id"],
            "frame_id": row["frame_id"],
            "subtask_id": _uuid_str(row["subtask_id"]),
            "dag_id": _uuid_str(row["dag_id"]),
            "trace_id": row["trace_id"],
            "error_text": row["error_text"],
            "context_window_size": row["context_window_size"],
            "total_tokens_est": row["total_tokens_est"],
            "utilization_pct": row["utilization_pct"],
            "tools_count": row["tools_count"],
            "tool_names": row["tool_names"],
            "messages_count": row["messages_count"],
            "loaded_facts": row["loaded_facts"],
            "loaded_decisions": row["loaded_decisions"],
            "loaded_procedures": row["loaded_procedures"],
            "loaded_episodes": row["loaded_episodes"],
            "cache_break": row["cache_break"],
            "cache_break_components": row["cache_break_components"],
            "cache_break_tokens_lost": row["cache_break_tokens_lost"],
        },
        "attempts": attempts,
        "sections": [{"name": name, "tokens_est": tokens} for name, tokens in sections],
        "turn": (
            None
            if row["turn_id"] is None
            else {
                "turn_id": str(row["turn_id"]),
                "session_id": row["session_id"],
                "session_title": row["session_title"],
            }
        ),
        "text_status": text_status,
        "blocks": [],
    }


async def add_model_call_blocks(detail: dict[str, Any], text_store: Any | None) -> dict[str, Any]:
    """``detail`` (from :func:`get_model_call`) with the call's stored blocks, without their bodies
    (``TextStore.describe``), and ``text_status`` ``ok`` when it has any; unchanged with the text off. The store reads
    in a session of its own, so await this after get_model_call's session is closed: a request then never holds two
    connections at once."""
    if not text_store:
        return detail
    blocks = [
        {
            "part": b.part,
            "ord": b.ord,
            "size_chars": b.size_chars,
            "tokens_est": b.size_chars // 4,
            "reused_by": b.reused_by,
        }
        for b in await text_store.describe(UUID(detail["call"]["id"]))
    ]
    if blocks:
        detail["text_status"], detail["blocks"] = "ok", blocks
    return detail


async def get_block(text_store: Any, call_id: UUID, part: str, ord_: int) -> dict[str, Any] | None:
    """One stored block of a model call, with its body (``TextStore.load_one``: a primary-key lookup of that block
    alone, agent-scoped by the store). None, before any query, for a ``part`` outside system, messages, tools and
    response or an ``ord_`` the column cannot hold; None when the call has no such block or is another agent's."""
    if part not in BLOCK_PARTS or not 0 <= ord_ <= BLOCK_ORD_MAX:
        return None
    body = await text_store.load_one(call_id, part, ord_)
    if body is None:
        return None
    return {"part": part, "ord": ord_, "size_chars": len(body), "body": body}
