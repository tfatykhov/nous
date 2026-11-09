"""F108 Phase 2: the Work view's read-only endpoints (nous/api/work_view_dashboard.py).

The endpoint tests run against real Postgres: scratch rows go straight into the Phase 1 tables with SQL, under an
agent id of the test's own, and the ``cleanup`` fixture deletes them afterwards. The parsing tests need no database.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from nous.api import work_view_dashboard as wv
from nous.api.rest import create_app
from nous.config import Settings
from nous.observability.model_call_text import TextStore
from nous.observability.recording import BoundedWriter

SESSIONS = "/dashboard/work/sessions"
ZERO_COUNTS = {k: {"sessions": 0, "turns": 0} for k in ("conversation", "task", "background")}


def _agent() -> str:
    return f"test-awv-wv-{uuid.uuid4().hex[:12]}"


def _settings(agent_id: str, **over) -> Settings:
    return Settings(agent_id=agent_id, telegram_bot_token=None, telegram_chat_id=None, **over)


def make_app(db, settings: Settings):
    """The REST app with mocked organs: the Work endpoints use only the database and the settings."""
    return create_app(MagicMock(), MagicMock(), MagicMock(), MagicMock(), db, settings)


@pytest.fixture
def agent_id() -> str:
    return _agent()


@pytest.fixture
def work_settings(agent_id: str) -> Settings:
    return _settings(agent_id, tool_call_log_enabled=True)


@pytest_asyncio.fixture
async def client(db, work_settings):
    async with AsyncClient(transport=ASGITransport(app=make_app(db, work_settings)), base_url="http://test") as c:
        yield c


async def delete_agent_rows(db, agent_id: str) -> None:
    """Every scratch row of ``agent_id`` these tests write."""
    async with db.engine.begin() as conn:
        for table in (
            "nous_system.tool_call_log",
            "nous_system.agent_turn_log",
            "nous_system.model_call_log",  # its attempts and text refs go with it (ON DELETE CASCADE)
            "nous_system.model_text_block",  # after the refs: they reference the blocks
            "nous_system.execution_ledger",
            "heart.episodes",
            "heart.subtasks",
        ):
            await conn.execute(text(f"DELETE FROM {table} WHERE agent_id = :a"), {"a": agent_id})


@pytest_asyncio.fixture
async def cleanup(db, agent_id):
    yield
    await delete_agent_rows(db, agent_id)


async def insert_turn(
    db,
    agent_id: str,
    session_id: str | None,
    *,
    work_kind: str = "conversation",
    started_at: datetime | None = None,
    turn_number: int | None = 1,
    trigger: str | None = "hi",
    tool_call_count: int | None = 0,
    turn_id: uuid.UUID | None = None,
    ended_at: datetime | None = None,
    parent_session_id: str | None = None,
    context_kind: str | None = None,
    frame_id: str | None = None,
    memory: dict | None = None,
    critic: dict | None = None,
    claim_check: dict | None = None,
    reply: str | None = None,
    error: str | None = None,
) -> uuid.UUID:
    """One ``agent_turn_log`` row; returns its id (the turn id)."""
    turn_id = turn_id or uuid.uuid4()
    async with db.engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO nous_system.agent_turn_log (id, agent_id, session_id, parent_session_id, turn_number, "
                "work_kind, context_kind, trigger_text, tool_call_count, started_at, ended_at, frame_id, "
                "memory_context, critic, claim_check, reply_text, error) "
                "VALUES (:id, :a, :s, :p, :n, :wk, :ck, :trig, :tcc, :start, :end, :frame, CAST(:memory AS jsonb), "
                "CAST(:critic AS jsonb), CAST(:claim AS jsonb), :reply, :error)"
            ),
            {
                "frame": frame_id,
                "memory": None if memory is None else json.dumps(memory),
                "critic": None if critic is None else json.dumps(critic),
                "claim": None if claim_check is None else json.dumps(claim_check),
                "reply": reply,
                "error": error,
                "id": turn_id,
                "a": agent_id,
                "s": session_id,
                "p": parent_session_id,
                "n": turn_number,
                "wk": work_kind,
                "ck": context_kind,
                "trig": trigger,
                "tcc": tool_call_count,
                "start": started_at or datetime.now(UTC),
                "end": ended_at,
            },
        )
    return turn_id


async def insert_call(
    db,
    agent_id: str,
    turn_id: uuid.UUID | None,
    call_index: int,
    *,
    session_id: str | None,
    tool: str = "bash",
    input_text: str = "",
    reason: str | None = None,
    is_error: bool = False,
    outcome: str = "ran",
    ledger_entry_id: uuid.UUID | None = None,
    started_at: datetime | None = None,
    subtask_id: uuid.UUID | None = None,
    work_kind: str = "conversation",
    tool_called: str | None = None,
    result: str = "",
    error_text: str | None = None,
    refused_by: str | None = None,
    duration_ms: int | None = None,
    parent_session_id: str | None = None,
) -> uuid.UUID:
    """One ``tool_call_log`` row; returns its id. ``tool_called`` (what the model sent) defaults to ``tool``."""
    async with db.engine.begin() as conn:
        return (
            await conn.execute(
                text(
                    "INSERT INTO nous_system.tool_call_log (agent_id, session_id, turn_id, call_index, work_kind, "
                    "subtask_id, tool_name, tool_name_called, reason, input_text, outcome, is_error, "
                    "ledger_entry_id, started_at, result_preview, error_text, refused_by, duration_ms, "
                    "parent_session_id) "
                    "VALUES (:a, :s, :t, :i, :wk, :sub, :tool, :called, :reason, :input, :outcome, :err, :ledger, "
                    ":start, :result, :error_text, :refused_by, :duration, :parent) RETURNING id"
                ),
                {
                    "a": agent_id,
                    "s": session_id,
                    "t": turn_id,
                    "i": call_index,
                    "wk": work_kind,
                    "sub": subtask_id,
                    "tool": tool,
                    "called": tool_called or tool,
                    "result": result,
                    "error_text": error_text,
                    "refused_by": refused_by,
                    "duration": duration_ms,
                    "parent": parent_session_id,
                    "reason": reason,
                    "input": input_text,
                    "outcome": outcome,
                    "err": is_error,
                    "ledger": ledger_entry_id,
                    "start": started_at or datetime.now(UTC),
                },
            )
        ).scalar_one()


async def insert_episode(
    db, agent_id: str, session_id: str, title: str | None, *, started_at: datetime, active: bool = True
) -> None:
    async with db.engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO heart.episodes (agent_id, title, summary, session_id, started_at, active) "
                "VALUES (:a, :title, 'scratch', :s, :start, :active)"
            ),
            {"a": agent_id, "title": title, "s": session_id, "start": started_at, "active": active},
        )


async def insert_subtask(db, agent_id: str, task: str) -> uuid.UUID:
    subtask_id = uuid.uuid4()
    async with db.engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO heart.subtasks (id, agent_id, task) VALUES (:id, :a, :task)"),
            {"id": subtask_id, "a": agent_id, "task": task},
        )
    return subtask_id


async def insert_ledger(
    db,
    agent_id: str,
    *,
    status: str = "success",
    side_effect_type: str = "write",
    tool: str = "write_file",
    external_ref: str | None = None,
    idempotency_key: str | None = None,
    key_args: dict | None = None,
) -> uuid.UUID:
    """One ``execution_ledger`` row; returns its id."""
    async with db.engine.begin() as conn:
        return (
            await conn.execute(
                text(
                    "INSERT INTO nous_system.execution_ledger (agent_id, context_kind, tool_name, side_effect_type, "
                    "status, external_ref, idempotency_key, key_args) "
                    "VALUES (:a, 'interactive', :tool, :effect, :status, :ref, :key, CAST(:args AS jsonb)) "
                    "RETURNING id"
                ),
                {
                    "a": agent_id,
                    "tool": tool,
                    "effect": side_effect_type,
                    "status": status,
                    "ref": external_ref,
                    "key": idempotency_key,
                    "args": json.dumps(key_args if key_args is not None else {"path": "x"}),
                },
            )
        ).scalar_one()


async def insert_model_call(
    db,
    agent_id: str,
    turn_id: uuid.UUID | None = None,
    *,
    session_id: str | None = None,
    purpose: str = "main_loop",
    model: str | None = "claude-test",
    status: str = "ok",
    input_uncached: int | None = 100,
    cache_read: int | None = 50,
    output_tokens: int | None = 20,
    started_at: datetime | None = None,
    cache_write: int | None = None,
    label_source: str = "explicit",
    call_kind: str = "call",
    call_count: int = 1,
    attempts: int = 1,
    http_status: int | None = None,
    is_fallback: bool = False,
    error_text: str | None = None,
    stop_reason: str | None = None,
    duration_ms: int | None = None,
    turn_number: int | None = None,
    call_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """One ``model_call_log`` row; returns its id. ``input_total`` is the table's generated column."""
    async with db.engine.begin() as conn:
        return (
            await conn.execute(
                text(
                    "INSERT INTO nous_system.model_call_log (id, agent_id, purpose, label_source, call_kind, model, "
                    "session_id, turn_id, status, input_uncached, cache_read, cache_write, output_tokens, "
                    "started_at, call_count, attempts, http_status, is_fallback, error_text, stop_reason, "
                    "duration_ms, turn_number) "
                    "VALUES (:id, :a, :purpose, :label, :kind, :model, :s, :t, :status, :uncached, :read, :write, "
                    ":out, :start, :count, :attempts, :http, :fallback, :error_text, :stop, :duration, :turn_number) "
                    "RETURNING id"
                ),
                {
                    "id": call_id or uuid.uuid4(),
                    "a": agent_id,
                    "purpose": purpose,
                    "label": label_source,
                    "kind": call_kind,
                    "model": model,
                    "s": session_id,
                    "t": turn_id,
                    "status": status,
                    "uncached": input_uncached,
                    "read": cache_read,
                    "write": cache_write,
                    "out": output_tokens,
                    "start": started_at or datetime.now(UTC),
                    "count": call_count,
                    "attempts": attempts,
                    "http": http_status,
                    "fallback": is_fallback,
                    "error_text": error_text,
                    "stop": stop_reason,
                    "duration": duration_ms,
                    "turn_number": turn_number,
                },
            )
        ).scalar_one()


async def get_json(client, path: str = SESSIONS, **params) -> dict:
    resp = await client.get(path, params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _ids(body: dict) -> list[str]:
    return [s["session_id"] for s in body["sessions"]]


def _by_id(body: dict) -> dict[str, dict]:
    return {s["session_id"]: s for s in body["sessions"]}


def _hour_ago() -> datetime:
    return datetime.now(UTC) - timedelta(hours=1)


# ---------------------------------------------------------------------------------------------------------------------
# Parsing (no database)
# ---------------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("raw", ["0", "31", "abc", "-1", "7.5", " 7", "٣"])
def test_parse_days_refuses_out_of_range_and_non_digits(raw):
    with pytest.raises(ValueError):
        wv.parse_days(raw, 7, 30)


def test_parse_days_default_and_bounds():
    assert wv.parse_days(None, 7, 30) == wv.parse_days("", 7, 30) == 7
    assert wv.parse_days("1", 7, 30) == 1 and wv.parse_days("30", 7, 30) == 30


def test_cursor_round_trips_and_splits_on_the_first_bar_only():
    ts = datetime(2026, 11, 8, 10, 0, 0, 123456, tzinfo=UTC)
    assert wv.format_cursor(ts, "intent-x:y") == "2026-11-08T10:00:00.123456+00:00|intent-x:y"
    assert wv.parse_cursor(wv.format_cursor(ts, "intent-x:y")) == (ts, "intent-x:y")
    assert wv.parse_cursor(f"{ts.isoformat()}|a|b") == (ts, "a|b")
    assert wv.parse_cursor(None) is None and wv.parse_cursor("") is None


@pytest.mark.parametrize(
    "raw", ["garbage", "2026-11-08T10:00:00+00:00", "2026-11-08T10:00:00+00:00|", "2026-11-08T10:00:00|x", "x|y"]
)
def test_parse_cursor_refuses_malformed(raw):
    with pytest.raises(ValueError):
        wv.parse_cursor(raw)


def test_lanes_are_the_mockup_map():
    assert wv.TOOL_LANES == {
        "recall_deep": "memory",
        "learn_fact": "memory",
        "record_decision": "memory",
        "resolve_decisions": "memory",
        "get_procedure": "memory",
        "db_query": "live",
        "list_decisions": "live",
        "list_tasks": "live",
        "dag_manage": "live",
        "bash": "workspace",
        "read_file": "workspace",
        "write_file": "workspace",
        "run_python": "workspace",
        "send_email": "acts",
        "spawn_task": "acts",
        "schedule_task": "acts",
        "resolve_intention": "acts",
        "submit_final_report": "acts",
        "web_fetch": "acts",
        "web_search": "acts",
        "dag_create": "acts",
    }
    assert wv.lane_of("bash") == "workspace" and wv.lane_of("no_such_tool") == "other"


def test_work_kind_groups():
    assert wv.WORK_KIND_GROUP == {
        "conversation": "conversation",
        "subtask": "task",
        "workflow_step": "task",
        "intention": "task",
        "heartbeat": "background",
        "background_check": "background",
    }


def test_disabled_sessions_has_the_page_keys_and_nothing_in_them():
    assert wv.disabled_sessions(7) == {
        "enabled": False,
        "window_days": 7,
        "counts": ZERO_COUNTS,
        "tool_names": [],
        "sessions": [],
        "next_before": None,
    }


# ---------------------------------------------------------------------------------------------------------------------
# GET /dashboard/work/sessions
# ---------------------------------------------------------------------------------------------------------------------


@pytest.mark.postgres_only
async def test_sessions_flag_off_is_empty_and_opens_no_session(db, agent_id, monkeypatch):
    opened = 0
    real_session = db.session

    def counting_session(*args, **kwargs):
        nonlocal opened
        opened += 1
        return real_session(*args, **kwargs)

    monkeypatch.setattr(db, "session", counting_session)
    off = make_app(db, _settings(agent_id, tool_call_log_enabled=False))
    async with AsyncClient(transport=ASGITransport(app=off), base_url="http://test") as c:
        body = await get_json(c)
    assert body["enabled"] is False and body["sessions"] == [] and body["counts"] == ZERO_COUNTS
    assert body == wv.disabled_sessions(7)
    assert opened == 0

    # The counter does see the handler's session when the log is on (so the zero above means something).
    on = make_app(db, _settings(agent_id, tool_call_log_enabled=True))
    async with AsyncClient(transport=ASGITransport(app=on), base_url="http://test") as c:
        body = await get_json(c)
    assert body["enabled"] is True and opened == 1


@pytest.mark.postgres_only
async def test_sessions_groups_turns_by_session_and_maps_kinds(client, db, agent_id, cleanup):
    base = _hour_ago()
    for i in range(3):
        await insert_turn(db, agent_id, "sess-A", started_at=base + timedelta(minutes=i), turn_number=i + 1)
    await insert_turn(db, agent_id, "sess-B", work_kind="subtask", started_at=base + timedelta(minutes=5))
    await insert_turn(db, agent_id, "sess-C", work_kind="heartbeat", started_at=base + timedelta(minutes=6))
    counts = {
        "conversation": {"sessions": 1, "turns": 3},
        "task": {"sessions": 1, "turns": 1},
        "background": {"sessions": 1, "turns": 1},
    }

    body = await get_json(client)
    assert body["enabled"] is True and body["window_days"] == 7
    assert _ids(body) == ["sess-B", "sess-A"]  # newest first; background hidden
    by_id = _by_id(body)
    assert (by_id["sess-A"]["kind"], by_id["sess-A"]["work_kind"], by_id["sess-A"]["turns"]) == (
        "conversation",
        "conversation",
        3,
    )
    assert (by_id["sess-B"]["kind"], by_id["sess-B"]["work_kind"]) == ("task", "subtask")
    assert body["counts"] == counts and body["next_before"] is None

    for kind, expected in (("background", ["sess-C"]), ("conversation", ["sess-A"]), ("task", ["sess-B"])):
        body = await get_json(client, kind=kind)
        assert _ids(body) == expected
        assert body["counts"] == counts  # counts ignore the filters
    assert _ids(await get_json(client, kind="all")) == ["sess-B", "sess-A"]
    assert _by_id(await get_json(client, kind="background"))["sess-C"]["kind"] == "background"


@pytest.mark.postgres_only
async def test_sessions_kind_comes_from_the_first_turn_and_unknown_kinds_are_background(client, db, agent_id, cleanup):
    base = _hour_ago()
    await insert_turn(
        db,
        agent_id,
        "sess-mixed",
        work_kind="intention",
        started_at=base,
        parent_session_id="parent-1",
        context_kind="intention_run",
    )
    await insert_turn(db, agent_id, "sess-mixed", work_kind="conversation", started_at=base + timedelta(minutes=1))
    await insert_turn(db, agent_id, "sess-odd", work_kind="something_new", started_at=base)

    body = await get_json(client, kind="task")
    mixed = _by_id(body)["sess-mixed"]
    assert (mixed["kind"], mixed["work_kind"], mixed["context_kind"], mixed["parent_session_id"]) == (
        "task",
        "intention",
        "intention_run",
        "parent-1",
    )
    assert _ids(await get_json(client, kind="background")) == ["sess-odd"]


@pytest.mark.postgres_only
async def test_sessions_tools_errors_and_flags(client, db, agent_id, cleanup):
    base = _hour_ago()
    t1 = await insert_turn(db, agent_id, "sess-S", started_at=base, tool_call_count=10)
    await insert_call(db, agent_id, t1, 0, session_id="sess-S", tool="bash", input_text="ls")
    await insert_call(db, agent_id, t1, 1, session_id="sess-S", tool="bash", input_text="ls")
    await insert_call(
        db, agent_id, t1, 2, session_id="sess-S", tool="read_file", input_text="x", is_error=True, outcome="error"
    )
    t2 = await insert_turn(db, agent_id, "sess-S", started_at=base + timedelta(minutes=1), turn_number=2)
    await insert_call(
        db,
        agent_id,
        t2,
        0,
        session_id="sess-S",
        tool="resolve_intention",
        input_text='{"decision": "ask", "id": "i-1"}',
    )
    await insert_call(
        db, agent_id, t2, 1, session_id="sess-S", tool="write_file", input_text="y", ledger_entry_id=uuid.uuid4()
    )

    # A quiet session: the same call in two turns is no repeat, an approval is no ask, an ask outside
    # resolve_intention is no ask, 9 calls is not many.
    q1 = await insert_turn(db, agent_id, "sess-T", started_at=base + timedelta(minutes=2), tool_call_count=9)
    await insert_call(db, agent_id, q1, 0, session_id="sess-T", tool="bash", input_text="pwd")
    await insert_call(db, agent_id, q1, 1, session_id="sess-T", tool="write_file", input_text='{"decision": "ask"}')
    q2 = await insert_turn(db, agent_id, "sess-T", started_at=base + timedelta(minutes=3), turn_number=2)
    await insert_call(db, agent_id, q2, 0, session_id="sess-T", tool="bash", input_text="pwd")
    await insert_call(
        db, agent_id, q2, 1, session_id="sess-T", tool="resolve_intention", input_text='{"decision":"approve"}'
    )

    body = await get_json(client)
    s, t = _by_id(body)["sess-S"], _by_id(body)["sess-T"]
    assert s["flags"] == ["error", "repeat", "many", "asked", "changed"]
    assert (s["turns"], s["tool_calls"], s["errors"]) == (2, 5, 1)
    assert s["tools"] == [
        {"tool": "bash", "calls": 2, "lane": "workspace"},
        {"tool": "read_file", "calls": 1, "lane": "workspace"},
        {"tool": "resolve_intention", "calls": 1, "lane": "acts"},
        {"tool": "write_file", "calls": 1, "lane": "workspace"},
    ]
    assert t["flags"] == [] and (t["tool_calls"], t["errors"]) == (4, 0)
    assert body["tool_names"] == ["bash", "read_file", "resolve_intention", "write_file"]

    for lens in ("error", "repeat", "many", "asked", "changed"):
        assert _ids(await get_json(client, lens=lens)) == ["sess-S"], lens


@pytest.mark.postgres_only
async def test_sessions_tools_are_capped_at_12(client, db, agent_id, cleanup):
    turn = await insert_turn(db, agent_id, "sess-many-tools", started_at=_hour_ago())
    for i in range(14):
        await insert_call(db, agent_id, turn, i, session_id="sess-many-tools", tool=f"tool_{i:02d}")
    await insert_call(db, agent_id, turn, 14, session_id="sess-many-tools", tool="tool_13", input_text="again")

    tools = _by_id(await get_json(client))["sess-many-tools"]["tools"]
    assert len(tools) == 12
    assert tools[0] == {"tool": "tool_13", "calls": 2, "lane": "other"}
    assert [t["tool"] for t in tools[1:]] == [f"tool_{i:02d}" for i in range(11)]


@pytest.mark.postgres_only
async def test_sessions_title_prefers_episode_then_subtask_then_trigger(client, db, agent_id, cleanup):
    base = _hour_ago()
    long_task = "Write the quarterly report " + "x" * 120
    long_trigger = "Please look into this " + "y" * 120
    first_sub = await insert_subtask(db, agent_id, long_task)
    later_sub = await insert_subtask(db, agent_id, "A later subtask")

    # Episode: the latest active titled episode wins over the subtask and the trigger.
    e = await insert_turn(db, agent_id, "sess-E", started_at=base, trigger="trigger E")
    await insert_call(db, agent_id, e, 0, session_id="sess-E", subtask_id=first_sub)
    await insert_episode(db, agent_id, "sess-E", "Old episode", started_at=base - timedelta(hours=2))
    await insert_episode(db, agent_id, "sess-E", "Current episode", started_at=base - timedelta(hours=1))
    await insert_episode(db, agent_id, "sess-E", "Inactive episode", started_at=base, active=False)
    other = _agent()
    try:
        await insert_episode(db, other, "sess-E", "Another agent's episode", started_at=base + timedelta(minutes=1))
        await insert_episode(db, other, "sess-T", "Another agent's episode", started_at=base)

        # Subtask: the first call (in turn order) that names one.
        s1 = await insert_turn(db, agent_id, "sess-S", started_at=base + timedelta(minutes=1), trigger="trigger S")
        await insert_call(db, agent_id, s1, 0, session_id="sess-S")
        await insert_call(db, agent_id, s1, 1, session_id="sess-S", subtask_id=first_sub)
        s2 = await insert_turn(db, agent_id, "sess-S", started_at=base + timedelta(minutes=2), turn_number=2)
        await insert_call(db, agent_id, s2, 0, session_id="sess-S", subtask_id=later_sub)

        # Trigger: the first turn's, cut to 80 characters.
        await insert_turn(db, agent_id, "sess-T", started_at=base + timedelta(minutes=3), trigger=long_trigger)
        await insert_turn(db, agent_id, "sess-T", started_at=base + timedelta(minutes=4), trigger="second")

        # Another agent's subtask never titles a session.
        their_sub = await insert_subtask(db, other, "Another agent's subtask")
        x = await insert_turn(db, agent_id, "sess-X", started_at=base + timedelta(minutes=5), trigger="trigger X")
        await insert_call(db, agent_id, x, 0, session_id="sess-X", subtask_id=their_sub)

        by_id = _by_id(await get_json(client))
    finally:
        await delete_agent_rows(db, other)

    assert by_id["sess-E"]["title"] == "Current episode"
    assert by_id["sess-E"]["first_message"] == "trigger E"
    assert by_id["sess-S"]["title"] == long_task[:100]
    assert by_id["sess-T"]["title"] == long_trigger[:80]
    assert by_id["sess-T"]["first_message"] == long_trigger
    assert by_id["sess-X"]["title"] == "trigger X"


@pytest.mark.postgres_only
async def test_sessions_null_session_turn_and_turnless_calls_are_ignored(client, db, agent_id, cleanup):
    base = _hour_ago()
    a = await insert_turn(db, agent_id, "sess-A", started_at=base)
    await insert_call(db, agent_id, a, 0, session_id="sess-A", tool="bash", input_text="ls")
    before = await get_json(client)

    # A turn with no session, with calls; a call of sess-A whose turn has no row; a call of a session with no turns.
    n = await insert_turn(db, agent_id, None, started_at=base + timedelta(minutes=1), tool_call_count=12)
    await insert_call(db, agent_id, n, 0, session_id=None, tool="web_fetch", is_error=True)
    await insert_call(
        db,
        agent_id,
        uuid.uuid4(),
        0,
        session_id="sess-A",
        tool="send_email",
        is_error=True,
        ledger_entry_id=uuid.uuid4(),
    )
    await insert_call(db, agent_id, uuid.uuid4(), 0, session_id="sess-ghost", tool="spawn_task")
    await insert_call(db, agent_id, None, 0, session_id="sess-A", tool="dag_create")
    after = await get_json(client)

    assert after["sessions"] == before["sessions"]
    assert after["counts"] == before["counts"] == {**ZERO_COUNTS, "conversation": {"sessions": 1, "turns": 1}}
    assert after["tool_names"] == before["tool_names"] == ["bash"]
    assert _by_id(after)["sess-A"]["flags"] == [] and _by_id(after)["sess-A"]["tool_calls"] == 1


@pytest.mark.postgres_only
async def test_sessions_other_agent_rows_never_appear(client, db, agent_id, cleanup):
    base = _hour_ago()
    mine = await insert_turn(db, agent_id, "sess-mine", started_at=base, trigger="mine")
    await insert_call(db, agent_id, mine, 0, session_id="sess-mine", tool="bash")
    other = _agent()
    try:
        theirs = await insert_turn(db, other, "sess-theirs", started_at=base, tool_call_count=20)
        await insert_call(db, other, theirs, 0, session_id="sess-theirs", tool="web_search", is_error=True)
        # The other agent's rows on my session id, and a call of theirs under my turn's id.
        await insert_turn(db, other, "sess-mine", started_at=base - timedelta(minutes=5), work_kind="heartbeat")
        await insert_call(
            db, other, mine, 1, session_id="sess-mine", tool="send_email", is_error=True, ledger_entry_id=uuid.uuid4()
        )
        await insert_episode(db, other, "sess-mine", "Their title", started_at=base)

        body = await get_json(client)
    finally:
        await delete_agent_rows(db, other)

    assert _ids(body) == ["sess-mine"]
    s = body["sessions"][0]
    assert (s["kind"], s["title"], s["turns"], s["tool_calls"], s["errors"], s["flags"]) == (
        "conversation",
        "mine",
        1,
        1,
        0,
        [],
    )
    assert datetime.fromisoformat(s["started_at"]) == base
    assert body["counts"] == {**ZERO_COUNTS, "conversation": {"sessions": 1, "turns": 1}}
    assert body["tool_names"] == ["bash"]


async def _all_pages(client, **params) -> tuple[list[str], int]:
    """Every session id across the pages (following ``next_before``), and the number of pages."""
    seen: list[str] = []
    cursor = None
    for pages in range(1, 21):  # bounded: a cursor bug must fail, not loop
        body = await get_json(client, **params, **({"before": cursor} if cursor else {}))
        seen += _ids(body)
        cursor = body["next_before"]
        if cursor is None:
            return seen, pages
    raise AssertionError(f"next_before never ended: {seen}")


@pytest.mark.postgres_only
async def test_sessions_pagination_ties(client, db, agent_id, cleanup):
    tied = _hour_ago().replace(microsecond=123456)
    ids = [f"sess-tie-{i}" for i in range(5)]
    for sid in ids:
        await insert_turn(db, agent_id, sid, started_at=tied)

    seen, pages = await _all_pages(client, limit="2")
    assert seen == sorted(ids, reverse=True) and pages == 3

    seen, pages = await _all_pages(client, limit="5")
    assert sorted(seen) == ids and pages == 1

    first = await get_json(client, limit="2")
    assert first["next_before"] == wv.format_cursor(tied, "sess-tie-3")


@pytest.mark.postgres_only
@pytest.mark.parametrize(
    "params",
    [
        {"days": "0"},
        {"days": "31"},
        {"days": "abc"},
        {"limit": "0"},
        {"limit": "101"},
        {"kind": "x"},
        {"lens": "x"},
        {"before": "garbage"},
        {"before": "2026-11-08T10:00:00|sess-x"},
        {"before": "2026-11-08T10:00:00+00:00|"},
    ],
)
async def test_sessions_bad_params(client, params):
    resp = await client.get(SESSIONS, params=params)
    assert resp.status_code == 400 and set(resp.json()) == {"error"}


@pytest.mark.postgres_only
async def test_sessions_q_matches_percent_and_underscore_literally(client, db, agent_id, cleanup):
    base = _hour_ago()
    await insert_turn(db, agent_id, "sess-pct", started_at=base, trigger="100% done")
    await insert_turn(db, agent_id, "sess-nopct", started_at=base, trigger="1000 done")
    await insert_turn(db, agent_id, "sess-us", started_at=base, trigger="a_b here")
    await insert_turn(db, agent_id, "sess-nous", started_at=base, trigger="axb here")
    await insert_turn(db, agent_id, "sess-bs", started_at=base, trigger=r"C:\temp path")

    assert _ids(await get_json(client, q="100%")) == ["sess-pct"]
    assert _ids(await get_json(client, q="a_b")) == ["sess-us"]
    assert _ids(await get_json(client, q="\\t")) == ["sess-bs"]


@pytest.mark.postgres_only
async def test_sessions_tool_filter_and_q(client, db, agent_id, cleanup):
    base = _hour_ago()
    a = await insert_turn(db, agent_id, "sess-A", started_at=base, trigger="Say HELLO there")
    await insert_call(db, agent_id, a, 0, session_id="sess-A", tool="bash")
    b = await insert_turn(db, agent_id, "sess-B", started_at=base + timedelta(minutes=1), trigger="other")
    await insert_call(db, agent_id, b, 0, session_id="sess-B", tool="read_file")
    await insert_turn(db, agent_id, "sess-C", started_at=base + timedelta(minutes=2), trigger="x")
    await insert_episode(db, agent_id, "sess-C", "Quarterly Report", started_at=base)

    body = await get_json(client, tool="bash")
    assert _ids(body) == ["sess-A"]
    assert body["tool_names"] == ["bash", "read_file"]
    assert _ids(await get_json(client, tool="read_file")) == ["sess-B"]
    assert _ids(await get_json(client, tool="web_fetch")) == []
    assert _ids(await get_json(client, q="hello")) == ["sess-A"]  # first message, case-insensitive
    assert _ids(await get_json(client, q="QUARTERLY")) == ["sess-C"]  # title
    assert _ids(await get_json(client, q="hello", tool="read_file")) == []


@pytest.mark.postgres_only
async def test_sessions_cursor_with_colon_and_dash_ids(client, db, agent_id, cleanup):
    tied = _hour_ago().replace(microsecond=654321)
    ids = ["subtask-ab12cd34", "intent-x:y", "mcp-session"]
    for sid in ids:
        await insert_turn(db, agent_id, sid, started_at=tied)
    seen, pages = await _all_pages(client, limit="1")
    assert seen == sorted(ids, reverse=True) and pages == 3

    # The default window is 7 days.
    await insert_turn(db, agent_id, "sess-old", started_at=datetime.now(UTC) - timedelta(days=8))
    assert "sess-old" not in _ids(await get_json(client))
    body = await get_json(client, days="30")
    assert "sess-old" in _ids(body) and body["window_days"] == 30


@pytest.mark.postgres_only
async def test_sessions_ended_at_and_title_fallback(client, db, agent_id, cleanup):
    base = _hour_ago().replace(microsecond=0)
    # Late end on the first turn: 10:30 beats the open second turn's start (10:10).
    await insert_turn(db, agent_id, "sess-long", started_at=base, ended_at=base + timedelta(minutes=30), trigger=None)
    await insert_turn(
        db, agent_id, "sess-long", started_at=base + timedelta(minutes=10), trigger="later", turn_number=2
    )
    # Open last turn: its start is the end.
    await insert_turn(db, agent_id, "sess-open", started_at=base, ended_at=base + timedelta(minutes=5))
    await insert_turn(db, agent_id, "sess-open", started_at=base + timedelta(minutes=10), turn_number=2)

    by_id = _by_id(await get_json(client))
    long, open_ = by_id["sess-long"], by_id["sess-open"]
    assert datetime.fromisoformat(long["started_at"]) == base
    assert datetime.fromisoformat(long["ended_at"]) == base + timedelta(minutes=30)
    assert datetime.fromisoformat(open_["ended_at"]) == base + timedelta(minutes=10)
    assert (long["title"], long["first_message"]) == ("(no title)", None)
    assert long["tools"] == [] and long["tool_calls"] == 0 and long["flags"] == []


@pytest.mark.postgres_only
async def test_sessions_run_under_a_10s_statement_timeout(db, agent_id):
    async with db.session() as session:
        await wv.get_sessions(
            session, agent_id, kind="all", days=7, tool=None, q=None, lens=None, limit=50, before=None
        )
        assert (await session.execute(text("SHOW statement_timeout"))).scalar_one() == "10s"


@pytest.mark.postgres_only
async def test_sessions_query_failure_is_a_plain_500(client, monkeypatch, caplog):
    async def boom(*args, **kwargs):
        raise RuntimeError("secret detail from the database")

    monkeypatch.setattr(wv, "get_sessions", boom)
    resp = await client.get(SESSIONS)
    assert resp.status_code == 500 and resp.json() == {"error": "work view query failed"}
    assert any(r.levelname == "WARNING" and r.exc_info for r in caplog.records)


# ---------------------------------------------------------------------------------------------------------------------
# GET /dashboard/work/sessions/{session_id}
# ---------------------------------------------------------------------------------------------------------------------


def trace_path(session_id: str) -> str:
    return f"{SESSIONS}/{session_id}"


def _turn_ids(body: dict) -> list[str]:
    return [t["turn_id"] for t in body["turns"]]


async def _all_trace_pages(client, session_id: str, **params) -> tuple[list[str], int]:
    """Every turn id of a trace across the pages (following ``next_after``), and the number of pages."""
    seen: list[str] = []
    cursor = None
    for pages in range(1, 21):  # bounded: a cursor bug must fail, not loop
        body = await get_json(client, trace_path(session_id), **params, **({"after": cursor} if cursor else {}))
        seen += _turn_ids(body)
        cursor = body["next_after"]
        if cursor is None:
            return seen, pages
    raise AssertionError(f"next_after never ended: {seen}")


def test_parse_trace_query():
    assert wv.parse_trace_query({}) == {"limit": 100, "after": None}
    key = uuid.uuid4()
    ts = datetime(2026, 11, 8, 10, 0, 0, 1, tzinfo=UTC)
    assert wv.parse_trace_query({"limit": "200", "after": wv.format_cursor(ts, str(key))}) == {
        "limit": 200,
        "after": (ts, str(key)),
    }


@pytest.mark.parametrize(
    "params",
    [
        {"limit": "0"},
        {"limit": "201"},
        {"limit": "x"},
        {"after": "garbage"},
        {"after": "2026-11-08T10:00:00+00:00|not-a-uuid"},
        {"after": f"2026-11-08T10:00:00|{uuid.UUID(int=1)}"},
    ],
)
def test_parse_trace_query_refuses_bad_values(params):
    with pytest.raises(ValueError):
        wv.parse_trace_query(params)


def test_disabled_trace_has_the_page_keys_and_nothing_in_them():
    assert wv.disabled_trace() == {"enabled": False, "session": None, "turns": [], "next_after": None}


@pytest.mark.postgres_only
async def test_trace_orders_turns_by_started_at_and_calls_by_index(client, db, agent_id, cleanup):
    base = _hour_ago()
    # Two turns with the same turn_number (compaction restarts the count), the later one inserted first and given
    # the smaller id, so neither insertion order nor id order is the right order.
    small_id, large_id = sorted([uuid.uuid4(), uuid.uuid4()])
    late = await insert_turn(
        db, agent_id, "sess-R", started_at=base + timedelta(minutes=10), turn_number=1, turn_id=small_id
    )
    early = await insert_turn(db, agent_id, "sess-R", started_at=base, turn_number=1, turn_id=large_id)
    # Calls inserted out of order, each with a start time that runs against its index.
    for idx in (2, 0, 1):
        await insert_call(
            db,
            agent_id,
            early,
            idx,
            session_id="sess-R",
            input_text=f"e{idx}",
            started_at=base - timedelta(seconds=idx),
        )
    for idx in (1, 0):
        await insert_call(
            db,
            agent_id,
            late,
            idx,
            session_id="sess-R",
            input_text=f"l{idx}",
            started_at=base + timedelta(minutes=10, seconds=-idx),
        )

    body = await get_json(client, trace_path("sess-R"))
    assert body["enabled"] is True and body["next_after"] is None
    assert _turn_ids(body) == [str(early), str(late)]
    assert [t["turn_number"] for t in body["turns"]] == [1, 1]
    assert [t["has_turn_row"] for t in body["turns"]] == [True, True]
    assert [c["input"] for c in body["turns"][0]["calls"]] == ["e0", "e1", "e2"]
    assert [c["call_index"] for c in body["turns"][0]["calls"]] == [0, 1, 2]
    assert [c["input"] for c in body["turns"][1]["calls"]] == ["l0", "l1"]
    assert (body["session"]["turns"], body["session"]["tool_calls"], body["session"]["errors"]) == (2, 5, 0)


@pytest.mark.postgres_only
async def test_trace_call_fields(client, db, agent_id, cleanup):
    base = _hour_ago().replace(microsecond=250000)
    turn = await insert_turn(db, agent_id, "sess-F", started_at=base)
    sent = await insert_ledger(
        db, agent_id, status="unknown", side_effect_type="external", tool="send_email", external_ref="msg-1"
    )
    kept = await insert_ledger(db, agent_id, status="success", idempotency_key="k-1", key_args={})
    c0 = await insert_call(
        db,
        agent_id,
        turn,
        0,
        session_id="sess-F",
        tool="send_email",
        tool_called="email_send",
        reason="tell the owner",
        input_text='{"to": "owner"}',
        result="sent",
        ledger_entry_id=sent,
        duration_ms=120,
        started_at=base,
    )
    c1 = await insert_call(
        db,
        agent_id,
        turn,
        1,
        session_id="sess-F",
        tool="bash",
        input_text="rm -rf /tmp/x",
        outcome="refused",
        refused_by="action_gate",
        is_error=True,
        error_text="refused by the gate",
        started_at=base + timedelta(seconds=1),
    )
    await insert_call(db, agent_id, turn, 2, session_id="sess-F", tool="write_file", ledger_entry_id=uuid.uuid4())
    other = _agent()
    try:
        theirs = await insert_ledger(db, other, status="unknown")
        await insert_call(db, agent_id, turn, 3, session_id="sess-F", tool="write_file", ledger_entry_id=theirs)
        await insert_call(db, agent_id, turn, 4, session_id="sess-F", tool="write_file", ledger_entry_id=kept)
        # Another agent's call under my turn id is not mine.
        await insert_call(db, other, turn, 5, session_id="sess-F", tool="web_search")
        body = await get_json(client, trace_path("sess-F"))
        calls = body["turns"][0]["calls"]
    finally:
        await delete_agent_rows(db, other)

    assert [c["call_index"] for c in calls] == [0, 1, 2, 3, 4]
    assert (body["session"]["tool_calls"], body["session"]["errors"]) == (5, 1)
    assert calls[0] == {
        "id": str(c0),
        "call_index": 0,
        "tool": "send_email",
        "tool_called": "email_send",
        "lane": "acts",
        "reason": "tell the owner",
        "input": '{"to": "owner"}',
        "result": "sent",
        "outcome": "ran",
        "is_error": False,
        "error": None,
        "refused_by": None,
        "duration_ms": 120,
        "started_at": base.isoformat(),
        "ledger": {
            "id": str(sent),
            "status": "unknown",
            "side_effect_type": "external",
            "external_ref": "msg-1",
            "tombstone": False,
        },
    }
    assert calls[1] == {
        "id": str(c1),
        "call_index": 1,
        "tool": "bash",
        "tool_called": None,
        "lane": "workspace",
        "reason": None,
        "input": "rm -rf /tmp/x",
        "result": "",
        "outcome": "refused",
        "is_error": True,
        "error": "refused by the gate",
        "refused_by": "action_gate",
        "duration_ms": None,
        "started_at": (base + timedelta(seconds=1)).isoformat(),
        "ledger": None,
    }
    assert calls[2]["ledger"] is None  # the ledger row is missing
    assert calls[3]["ledger"] is None  # another agent's ledger row
    assert calls[4]["ledger"] == {
        "id": str(kept),
        "status": "success",
        "side_effect_type": "write",
        "external_ref": None,
        "tombstone": True,
    }


@pytest.mark.postgres_only
async def test_trace_turn_without_turn_row_is_synthetic(client, db, agent_id, cleanup):
    base = _hour_ago()
    ghost = uuid.uuid4()
    await insert_call(
        db, agent_id, ghost, 1, session_id="sess-G", tool="send_email", started_at=base + timedelta(seconds=5)
    )
    await insert_call(
        db,
        agent_id,
        ghost,
        0,
        session_id="sess-G",
        tool="bash",
        started_at=base + timedelta(seconds=2),
        work_kind="subtask",
        parent_session_id="parent-G",
    )
    # A call with no turn id, and another agent's turnless call on this session id: neither is a turn.
    await insert_call(db, agent_id, None, 0, session_id="sess-G", tool="dag_create")
    other = _agent()
    try:
        await insert_call(db, other, uuid.uuid4(), 0, session_id="sess-G", tool="spawn_task", started_at=base)
        body = await get_json(client, trace_path("sess-G"))
    finally:
        await delete_agent_rows(db, other)

    assert _turn_ids(body) == [str(ghost)]
    turn = body["turns"][0]
    assert {k: v for k, v in turn.items() if k != "calls"} == {
        "turn_id": str(ghost),
        "turn_number": None,
        "has_turn_row": False,
        "started_at": (base + timedelta(seconds=2)).isoformat(),
        "ended_at": None,
        "frame_id": None,
        "trigger": None,
        "memory": None,
        "critic": None,
        "claim_check": None,
        "reply": None,
        "error": None,
        "tool_call_count": 2,
        "model_calls": [],
    }
    assert [(c["call_index"], c["tool"]) for c in turn["calls"]] == [(0, "bash"), (1, "send_email")]
    # With no turn row at all, the header takes its kind and parent from the first call.
    assert body["session"] == {
        "session_id": "sess-G",
        "parent_session_id": "parent-G",
        "kind": "task",
        "work_kind": "subtask",
        "title": "(no title)",
        "started_at": (base + timedelta(seconds=2)).isoformat(),
        "ended_at": (base + timedelta(seconds=5)).isoformat(),  # the synthetic turn ends at its last call
        "turns": 1,
        "tool_calls": 2,
        "errors": 0,
    }


@pytest.mark.postgres_only
async def test_trace_memory_critic_claim_check_pass_through(client, db, agent_id, cleanup):
    memory = {"decision": ["d-1"], "fact": ["f-1", "f-2"], "procedure": [], "episode": ["e-1"]}
    critic = {
        "heuristic_frame": "task",
        "critic_frame": "question",
        "agreed": False,
        "rationale": "the user asked a question",
        "extra": {"nested": [1, 2.5, None, True, "ü"]},
    }
    claim_check = {"verified": True, "claim_count": 3, "violation_count": 0}
    base = _hour_ago()
    full = await insert_turn(
        db,
        agent_id,
        "sess-M",
        started_at=base,
        frame_id="question",
        memory=memory,
        critic=critic,
        claim_check=claim_check,
        reply="Here it is.",
        error="late tool error",
        tool_call_count=3,
    )
    bare = await insert_turn(db, agent_id, "sess-M", started_at=base + timedelta(minutes=1), tool_call_count=None)

    turns = (await get_json(client, trace_path("sess-M")))["turns"]
    assert [t["turn_id"] for t in turns] == [str(full), str(bare)]
    first, second = turns
    assert (first["memory"], first["critic"], first["claim_check"]) == (memory, critic, claim_check)
    assert (first["frame_id"], first["trigger"], first["reply"], first["error"]) == (
        "question",
        "hi",
        "Here it is.",
        "late tool error",
    )
    assert first["tool_call_count"] == 3  # the runner's count, even with no call rows
    assert first["calls"] == [] and first["has_turn_row"] is True
    assert (second["memory"], second["critic"], second["claim_check"], second["reply"]) == (None, None, None, None)
    assert second["tool_call_count"] == 0  # no stored count: the calls listed


@pytest.mark.postgres_only
async def test_trace_model_calls_attached_only_when_model_log_on(db, agent_id, cleanup):
    base = _hour_ago()
    turn = await insert_turn(db, agent_id, "sess-MC", started_at=base)
    busy = await insert_turn(db, agent_id, "sess-MC", started_at=base + timedelta(minutes=1), turn_number=2)
    first = await insert_model_call(db, agent_id, turn, session_id="sess-MC", started_at=base)
    failed = await insert_model_call(
        db,
        agent_id,
        turn,
        session_id="sess-MC",
        model=None,
        status="error",
        input_uncached=None,
        cache_read=None,
        output_tokens=None,
        started_at=base + timedelta(seconds=1),
    )
    # A background call of the session carries no turn id.
    await insert_model_call(db, agent_id, None, session_id="sess-MC", purpose="critic")
    busy_ids = [
        await insert_model_call(db, agent_id, busy, session_id="sess-MC", started_at=base + timedelta(seconds=30 - i))
        for i in range(22)
    ]
    other = _agent()
    try:
        # Another agent's call under my turn id.
        await insert_model_call(db, other, turn, session_id="sess-MC", purpose="theirs")
        on = make_app(db, _settings(agent_id, tool_call_log_enabled=True, model_call_log_enabled=True))
        async with AsyncClient(transport=ASGITransport(app=on), base_url="http://test") as c:
            body_on = await get_json(c, trace_path("sess-MC"))
        off = make_app(db, _settings(agent_id, tool_call_log_enabled=True, model_call_log_enabled=False))
        async with AsyncClient(transport=ASGITransport(app=off), base_url="http://test") as c:
            body_off = await get_json(c, trace_path("sess-MC"))
    finally:
        await delete_agent_rows(db, other)

    assert body_on["turns"][0]["model_calls"] == [
        {
            "id": str(first),
            "purpose": "main_loop",
            "model": "claude-test",
            "status": "ok",
            "input_total": 150,
            "output_tokens": 20,
        },
        {
            "id": str(failed),
            "purpose": "main_loop",
            "model": None,
            "status": "error",
            "input_total": None,  # no usage recorded: null, not the generated column's 0
            "output_tokens": None,
        },
    ]
    # At most 20 per turn, the earliest first.
    assert [m["id"] for m in body_on["turns"][1]["model_calls"]] == [str(i) for i in reversed(busy_ids)][:20]
    assert [t["model_calls"] for t in body_off["turns"]] == [[], []]


@pytest.mark.postgres_only
async def test_trace_model_call_input_total_is_null_only_without_usage(db, agent_id, cleanup):
    # input_total is a generated coalesce sum (0 for a call with no usage). The trace returns null when all three
    # parts are null, whatever the status, and the sum as soon as any part is recorded, a zero included.
    base = _hour_ago()
    turn = await insert_turn(db, agent_id, "sess-IT", started_at=base)
    no_usage = {"input_uncached": None, "cache_read": None}  # cache_write defaults to None
    cases = [
        ("no usage, failed", {**no_usage, "status": "error"}, None),
        ("no usage, ok", {**no_usage, "status": "ok"}, None),
        ("cache write only, failed", {**no_usage, "status": "error", "cache_write": 7}, 7),
        ("cache read only", {"input_uncached": None, "cache_read": 30}, 30),
        ("a recorded zero", {"input_uncached": 0, "cache_read": None}, 0),
        ("all three", {"input_uncached": 100, "cache_read": 50, "cache_write": 5}, 155),
    ]
    for i, (_, parts, _) in enumerate(cases):
        at = base + timedelta(seconds=i)
        await insert_model_call(db, agent_id, turn, session_id="sess-IT", started_at=at, **parts)
    on = make_app(db, _settings(agent_id, tool_call_log_enabled=True, model_call_log_enabled=True))
    async with AsyncClient(transport=ASGITransport(app=on), base_url="http://test") as c:
        body = await get_json(c, trace_path("sess-IT"))
    got = [m["input_total"] for m in body["turns"][0]["model_calls"]]
    assert got == [expected for _, _, expected in cases], list(zip([name for name, _, _ in cases], got, strict=True))


@pytest.mark.postgres_only
async def test_trace_synthetic_turn_sits_in_started_at_position(client, db, agent_id, cleanup):
    base = _hour_ago()
    # Ids in the reverse of time order, so a page taken in id order cannot pass.
    b_id, ghost, a_id = sorted(uuid.uuid4() for _ in range(3))
    a = await insert_turn(db, agent_id, "sess-P", started_at=base, tool_call_count=1, turn_id=a_id)
    await insert_call(db, agent_id, a, 0, session_id="sess-P", started_at=base)
    await insert_call(
        db,
        agent_id,
        ghost,
        0,
        session_id="sess-P",
        is_error=True,
        outcome="error",
        started_at=base + timedelta(minutes=5),
    )
    await insert_call(db, agent_id, ghost, 1, session_id="sess-P", started_at=base + timedelta(minutes=6))
    b = await insert_turn(db, agent_id, "sess-P", started_at=base + timedelta(minutes=10), turn_number=2, turn_id=b_id)
    await insert_call(db, agent_id, b, 0, session_id="sess-P", started_at=base + timedelta(minutes=10))

    body = await get_json(client, trace_path("sess-P"))
    assert _turn_ids(body) == [str(a), str(ghost), str(b)]
    assert [t["has_turn_row"] for t in body["turns"]] == [True, False, True]
    assert (body["session"]["turns"], body["session"]["tool_calls"], body["session"]["errors"]) == (3, 4, 1)

    seen, pages = await _all_trace_pages(client, "sess-P", limit="1")
    assert seen == [str(a), str(ghost), str(b)] and pages == 3


@pytest.mark.postgres_only
async def test_trace_unknown_or_other_agent_session_is_404(client, db, agent_id, cleanup):
    resp = await client.get(trace_path("sess-nothing"))
    assert resp.status_code == 404 and resp.json() == {"error": "not found"}

    other = _agent()
    try:
        theirs = await insert_turn(db, other, "sess-theirs", started_at=_hour_ago())
        await insert_call(db, other, theirs, 0, session_id="sess-theirs")
        await insert_call(db, other, uuid.uuid4(), 0, session_id="sess-theirs")
        resp = await client.get(trace_path("sess-theirs"))
    finally:
        await delete_agent_rows(db, other)
    assert resp.status_code == 404 and resp.json() == {"error": "not found"}

    # A session whose only call has no turn id is known (the EXISTS over each table), with nothing to trace.
    await insert_call(db, agent_id, None, 0, session_id="sess-loose")
    body = await get_json(client, trace_path("sess-loose"))
    assert body["turns"] == [] and body["next_after"] is None
    assert (body["session"]["turns"], body["session"]["tool_calls"]) == (0, 0)


@pytest.mark.postgres_only
async def test_trace_flag_off_is_empty(db, agent_id, cleanup, monkeypatch):
    turn = await insert_turn(db, agent_id, "sess-off", started_at=_hour_ago())
    await insert_call(db, agent_id, turn, 0, session_id="sess-off")
    opened = 0
    real_session = db.session

    def counting_session(*args, **kwargs):
        nonlocal opened
        opened += 1
        return real_session(*args, **kwargs)

    monkeypatch.setattr(db, "session", counting_session)
    off = make_app(db, _settings(agent_id, tool_call_log_enabled=False, model_call_log_enabled=True))
    async with AsyncClient(transport=ASGITransport(app=off), base_url="http://test") as c:
        assert await get_json(c, trace_path("sess-off")) == wv.disabled_trace()
        assert await get_json(c, trace_path("sess-nothing")) == wv.disabled_trace()  # no 404 either
    assert opened == 0

    # The counter does see the handler's session when the log is on (so the zero above means something).
    on = make_app(db, _settings(agent_id, tool_call_log_enabled=True))
    async with AsyncClient(transport=ASGITransport(app=on), base_url="http://test") as c:
        body = await get_json(c, trace_path("sess-off"))
    assert body["enabled"] is True and _turn_ids(body) == [str(turn)] and opened == 1


@pytest.mark.postgres_only
async def test_trace_pagination(client, db, agent_id, cleanup):
    tied = _hour_ago().replace(microsecond=987654)
    ids = [await insert_turn(db, agent_id, "intent-x:y", started_at=tied, turn_number=i) for i in range(2)]
    last = await insert_turn(db, agent_id, "intent-x:y", started_at=tied + timedelta(seconds=1), turn_number=3)
    expected = [str(i) for i in sorted(ids)] + [str(last)]

    seen, pages = await _all_trace_pages(client, "intent-x:y", limit="1")
    assert seen == expected and pages == 3
    seen, pages = await _all_trace_pages(client, "intent-x:y", limit="2")
    assert seen == expected and pages == 2

    first = await get_json(client, trace_path("intent-x:y"), limit="1")
    assert first["next_after"] == wv.format_cursor(tied, expected[0])
    assert first["session"]["turns"] == 3  # the header counts the whole session, not the page
    assert (await get_json(client, trace_path("intent-x:y"), limit="3"))["next_after"] is None


@pytest.mark.postgres_only
@pytest.mark.parametrize(
    "params",
    [
        {"limit": "0"},
        {"limit": "201"},
        {"after": "garbage"},
        {"after": "2026-11-08T10:00:00+00:00|not-a-uuid"},
        {"after": f"2026-11-08T10:00:00|{uuid.UUID(int=1)}"},
    ],
)
async def test_trace_bad_params(client, params):
    resp = await client.get(trace_path("sess-x"), params=params)
    assert resp.status_code == 400 and set(resp.json()) == {"error"}


@pytest.mark.postgres_only
async def test_trace_hostile_text_is_returned_verbatim(client, db, agent_id, cleanup):
    hostile = "<img src=x onerror=alert(1)>"
    script = "</script><script>alert('x')</script> & \"quotes\""
    turn = await insert_turn(db, agent_id, "sess-H", started_at=_hour_ago(), trigger=hostile, reply=script)
    await insert_call(
        db, agent_id, turn, 0, session_id="sess-H", reason=hostile, input_text=hostile + script, result=script
    )
    body = await get_json(client, trace_path("sess-H"))
    t = body["turns"][0]
    assert (t["trigger"], t["reply"]) == (hostile, script)
    call = t["calls"][0]
    assert (call["reason"], call["input"], call["result"]) == (hostile, hostile + script, script)
    assert body["session"]["title"] == hostile


@pytest.mark.postgres_only
async def test_trace_session_header(client, db, agent_id, cleanup):
    base = _hour_ago().replace(microsecond=0)
    # A turnless call before the first turn row: the header's kind still comes from the first turn row.
    await insert_call(db, agent_id, uuid.uuid4(), 0, session_id="sess-HD", started_at=base - timedelta(minutes=1))
    # The first turn gets the larger id, so "first" by id would pick the wrong one.
    small_id, large_id = sorted([uuid.uuid4(), uuid.uuid4()])
    await insert_turn(
        db,
        agent_id,
        "sess-HD",
        work_kind="intention",
        started_at=base,
        ended_at=base + timedelta(minutes=30),
        parent_session_id="parent-HD",
        trigger="first message",
        turn_id=large_id,
    )
    await insert_turn(
        db, agent_id, "sess-HD", work_kind="conversation", started_at=base + timedelta(minutes=5), turn_id=small_id
    )
    await insert_episode(db, agent_id, "sess-HD", "The episode title", started_at=base)
    await insert_episode(
        db, agent_id, "sess-HD", "Inactive episode", started_at=base + timedelta(minutes=1), active=False
    )
    # No episode: the subtask of the session's first call that names one (the list's rule).
    first_sub = await insert_subtask(db, agent_id, "Write the report " + "x" * 120)
    st = await insert_turn(db, agent_id, "sess-HS", started_at=base, trigger="trigger HS")
    await insert_call(db, agent_id, st, 0, session_id="sess-HS")
    await insert_call(db, agent_id, st, 1, session_id="sess-HS", subtask_id=first_sub)
    other = _agent()
    try:
        await insert_episode(db, other, "sess-HD", "Another agent's episode", started_at=base + timedelta(minutes=2))
        session = (await get_json(client, trace_path("sess-HD")))["session"]
        assert (await get_json(client, trace_path("sess-HS")))["session"]["title"] == ("Write the report " + "x" * 120)[
            :100
        ]
    finally:
        await delete_agent_rows(db, other)
    assert session == {
        "session_id": "sess-HD",
        "parent_session_id": "parent-HD",
        "kind": "task",
        "work_kind": "intention",
        "title": "The episode title",
        "started_at": (base - timedelta(minutes=1)).isoformat(),
        "ended_at": (base + timedelta(minutes=30)).isoformat(),
        "turns": 3,
        "tool_calls": 1,
        "errors": 0,
    }


@pytest.mark.postgres_only
async def test_trace_synthetic_turn_of_two_calls_ends_at_its_last_call(client, db, agent_id, cleanup):
    base = _hour_ago()
    await insert_turn(db, agent_id, "sess-G2", started_at=base, ended_at=base + timedelta(seconds=30))
    ghost = uuid.uuid4()  # calls under a turn id with no turn row
    first, last = base + timedelta(minutes=5), base + timedelta(minutes=10)
    await insert_call(db, agent_id, ghost, 0, session_id="sess-G2", input_text="a", started_at=first)
    await insert_call(db, agent_id, ghost, 1, session_id="sess-G2", input_text="b", started_at=last)

    body = await get_json(client, trace_path("sess-G2"))
    synthetic = body["turns"][1]
    assert (synthetic["turn_id"], synthetic["has_turn_row"], synthetic["started_at"]) == (
        str(ghost),
        False,
        first.isoformat(),
    )
    assert [c["input"] for c in synthetic["calls"]] == ["a", "b"]
    assert (body["session"]["started_at"], body["session"]["ended_at"]) == (base.isoformat(), last.isoformat())


@pytest.mark.postgres_only
async def test_trace_runs_under_a_10s_statement_timeout(db, agent_id):
    async with db.session() as session:
        trace = await wv.get_trace(session, agent_id, "sess-none", limit=10, after=None, model_calls_enabled=True)
        assert trace is None
        assert (await session.execute(text("SHOW statement_timeout"))).scalar_one() == "10s"


@pytest.mark.postgres_only
async def test_trace_query_failure_is_a_plain_500(client, monkeypatch, caplog):
    async def boom(*args, **kwargs):
        raise RuntimeError("secret detail from the database")

    monkeypatch.setattr(wv, "get_trace", boom)
    resp = await client.get(trace_path("sess-x"))
    assert resp.status_code == 500 and resp.json() == {"error": "work view query failed"}
    assert any(r.levelname == "WARNING" and r.exc_info for r in caplog.records)


@pytest.mark.postgres_only
async def test_trace_unrenderable_payload_is_the_plain_500(client, monkeypatch, caplog):
    # jsonb takes a number like 1e400, which decodes to inf, and Starlette renders with allow_nan=False.
    async def unrenderable(*args, **kwargs):
        return {"enabled": True, "critic": {"score": float("inf")}}

    monkeypatch.setattr(wv, "get_trace", unrenderable)
    resp = await client.get(trace_path("sess-x"))
    assert resp.status_code == 500 and resp.json() == {"error": "work view query failed"}
    assert any(r.levelname == "WARNING" and r.exc_info for r in caplog.records)


# ---------------------------------------------------------------------------------------------------------------------
# GET /dashboard/work/patterns
# ---------------------------------------------------------------------------------------------------------------------

PATTERNS = "/dashboard/work/patterns"
NO_WASTE = {"repeated_calls": 0, "retried_and_failed_again": 0}
NO_KIND = {"turns": 0, "calls": 0}


async def insert_calls(
    db,
    agent_id: str,
    tool: str,
    n: int,
    *,
    errors: int = 0,
    turn_id: uuid.UUID | None = None,
    session_id: str | None = "sess-bulk",
    work_kind: str = "conversation",
    started_at: datetime | None = None,
) -> None:
    """``n`` calls of ``tool`` in one statement, the first ``errors`` of them failing, each with an input of its own
    (so no two of them repeat). Each call gets a turn id of its own unless ``turn_id`` is given."""
    async with db.engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO nous_system.tool_call_log (agent_id, session_id, turn_id, call_index, work_kind, "
                "tool_name, tool_name_called, input_text, outcome, is_error, started_at) "
                "SELECT :a, :s, coalesce(CAST(:t AS uuid), gen_random_uuid()), g, :wk, :tool, :tool, 'in-' || g, "
                "CASE WHEN g < :errors THEN 'error' ELSE 'ran' END, g < :errors, :start "
                "FROM generate_series(0, :n - 1) g"
            ),
            {
                "a": agent_id,
                "s": session_id,
                "t": turn_id,
                "wk": work_kind,
                "tool": tool,
                "errors": errors,
                "n": n,
                "start": started_at or datetime.now(UTC),
            },
        )


def _tools(body: dict) -> dict[str, dict]:
    return {t["tool"]: t for t in body["tools"]}


def test_disabled_patterns_has_the_page_keys_and_nothing_in_them():
    assert wv.disabled_patterns(9) == {
        "enabled": False,
        "window_days": 9,
        "reason_coverage": {"calls": 0, "with_reason": 0},
        "tools": [],
        "failing": [],
        "wasted": NO_WASTE,
    }


@pytest.mark.postgres_only
async def test_patterns_flag_off(db, agent_id, cleanup, monkeypatch):
    await insert_call(db, agent_id, uuid.uuid4(), 0, session_id="sess-P", reason="why")
    opened = 0
    real_session = db.session

    def counting_session(*args, **kwargs):
        nonlocal opened
        opened += 1
        return real_session(*args, **kwargs)

    monkeypatch.setattr(db, "session", counting_session)
    off = make_app(db, _settings(agent_id, tool_call_log_enabled=False))
    async with AsyncClient(transport=ASGITransport(app=off), base_url="http://test") as c:
        assert await get_json(c, PATTERNS) == wv.disabled_patterns(7)
        assert await get_json(c, PATTERNS, days="3") == wv.disabled_patterns(3)
    assert opened == 0

    # The counter does see the handler's session when the log is on (so the zero above means something).
    on = make_app(db, _settings(agent_id, tool_call_log_enabled=True))
    async with AsyncClient(transport=ASGITransport(app=on), base_url="http://test") as c:
        body = await get_json(c, PATTERNS)
    assert body["enabled"] is True and opened == 1
    assert body["reason_coverage"] == {"calls": 1, "with_reason": 1}


@pytest.mark.postgres_only
async def test_patterns_empty_window_is_zeros_not_nulls(client, agent_id):
    assert await get_json(client, PATTERNS) == {
        "enabled": True,
        "window_days": 7,
        "reason_coverage": {"calls": 0, "with_reason": 0},
        "tools": [],
        "failing": [],
        "wasted": NO_WASTE,
    }


@pytest.mark.postgres_only
async def test_patterns_counts_by_kind_of_work(client, db, agent_id, cleanup):
    conv, sub, beat = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await insert_call(db, agent_id, conv, 0, session_id="sess-A", tool="bash", input_text="ls")
    await insert_call(db, agent_id, conv, 1, session_id="sess-A", tool="bash", input_text="pwd", is_error=True)
    await insert_call(db, agent_id, conv, 2, session_id="sess-A", tool="read_file", input_text="x")
    await insert_call(db, agent_id, sub, 0, session_id="sess-B", tool="bash", input_text="ls", work_kind="subtask")
    await insert_call(db, agent_id, beat, 0, session_id="sess-C", tool="bash", input_text="ls", work_kind="heartbeat")
    # A turnless call of an unknown kind of work: background, a call but no turn.
    await insert_call(db, agent_id, None, 0, session_id="sess-C", tool="bash", input_text="df", work_kind="new_kind")

    body = await get_json(client, PATTERNS)
    assert [t["tool"] for t in body["tools"]] == ["bash", "read_file"]  # most calls first
    bash, read = _tools(body)["bash"], _tools(body)["read_file"]
    assert (bash["lane"], bash["calls"], bash["errors"], bash["sessions"]) == ("workspace", 5, 1, 3)
    assert bash["by_kind"] == {
        "conversation": {"turns": 1, "calls": 2},
        "task": {"turns": 1, "calls": 1},
        "background": {"turns": 1, "calls": 2},
    }
    # Every kind is listed, zero when the tool was not used in it.
    assert read["by_kind"] == {"conversation": {"turns": 1, "calls": 1}, "task": NO_KIND, "background": NO_KIND}
    assert (read["calls"], read["errors"], read["sessions"], read["reasons"]) == (1, 0, 1, [])


@pytest.mark.postgres_only
async def test_patterns_tools_are_capped_at_30_by_calls(client, db, agent_id, cleanup):
    # 30 tools with one call, then two with two calls whose names sort last: a cap by name would drop them.
    for i in range(30):
        await insert_calls(db, agent_id, f"t-{i:02d}", 1)
    await insert_calls(db, agent_id, "zz-a", 2)
    await insert_calls(db, agent_id, "zz-b", 2)

    body = await get_json(client, PATTERNS)
    assert [t["tool"] for t in body["tools"]] == ["zz-a", "zz-b"] + [f"t-{i:02d}" for i in range(28)]
    assert {t["lane"] for t in body["tools"]} == {"other"}
    assert body["reason_coverage"] == {"calls": 34, "with_reason": 0}  # every call, not only the listed tools


@pytest.mark.postgres_only
async def test_patterns_common_reasons(client, db, agent_id, cleanup):
    base = _hour_ago()
    turn = uuid.uuid4()
    # Inserted out of time order: the text shown is the first seen (by started_at), not the first inserted and not
    # min() (the database's collation sorts "check x " before "Check X").
    bash = [
        ("check x ", 1),
        ("Check X", 0),
        ("Read log", 2),
        (None, 3),
        ("   ", 4),  # blank: no reason
    ]
    for i, (reason, second) in enumerate(bash):
        await insert_call(
            db,
            agent_id,
            turn,
            i,
            session_id="sess-R",
            tool="bash",
            input_text=f"cmd-{i}",
            reason=reason,
            started_at=base + timedelta(seconds=second),
        )
    # Top 3 only; ties go to the reason seen first.
    read = ["a", "a", "a", "b", "b", "c", "d"]
    for i, reason in enumerate(read):
        await insert_call(
            db,
            agent_id,
            turn,
            10 + i,
            session_id="sess-R",
            tool="read_file",
            input_text=f"f-{i}",
            reason=reason,
            started_at=base + timedelta(seconds=10 + i),
        )

    body = await get_json(client, PATTERNS)
    tools = _tools(body)
    assert tools["bash"]["reasons"] == [{"text": "Check X", "count": 2}, {"text": "Read log", "count": 1}]
    assert tools["read_file"]["reasons"] == [
        {"text": "a", "count": 3},
        {"text": "b", "count": 2},
        {"text": "c", "count": 1},
    ]
    assert body["reason_coverage"] == {"calls": 12, "with_reason": 10}


@pytest.mark.postgres_only
async def test_patterns_failing_needs_20_calls(client, db, agent_id, cleanup):
    await insert_calls(db, agent_id, "few", 5, errors=5)  # every call failed, but too few calls
    await insert_calls(db, agent_id, "never", 20, errors=0)  # enough calls, none failed
    await insert_calls(db, agent_id, "half", 20, errors=10)
    for tool, calls, errors in (
        ("f-a", 20, 9),
        ("f-b", 20, 8),
        ("f-c", 40, 16),  # the same rate as f-b with more calls: listed first
        ("f-d", 20, 7),
        ("f-e", 20, 6),
        ("f-f", 20, 5),
        ("f-g", 20, 4),
        ("f-h", 20, 3),  # ninth: cut
    ):
        await insert_calls(db, agent_id, tool, calls, errors=errors)

    failing = (await get_json(client, PATTERNS))["failing"]
    assert failing == [
        {"tool": "half", "calls": 20, "errors": 10, "error_rate": 0.5},
        {"tool": "f-a", "calls": 20, "errors": 9, "error_rate": 0.45},
        {"tool": "f-c", "calls": 40, "errors": 16, "error_rate": 0.4},
        {"tool": "f-b", "calls": 20, "errors": 8, "error_rate": 0.4},
        {"tool": "f-d", "calls": 20, "errors": 7, "error_rate": 0.35},
        {"tool": "f-e", "calls": 20, "errors": 6, "error_rate": 0.3},
        {"tool": "f-f", "calls": 20, "errors": 5, "error_rate": 0.25},
        {"tool": "f-g", "calls": 20, "errors": 4, "error_rate": 0.2},
    ]


@pytest.mark.postgres_only
async def test_patterns_failing_reads_every_tool_not_only_the_listed_30(client, db, agent_id, cleanup):
    for i in range(30):
        await insert_calls(db, agent_id, f"busy-{i:02d}", 25)
    await insert_calls(db, agent_id, "flaky", 20, errors=10)

    body = await get_json(client, PATTERNS)
    assert "flaky" not in _tools(body) and len(body["tools"]) == 30
    assert body["failing"] == [{"tool": "flaky", "calls": 20, "errors": 10, "error_rate": 0.5}]


@pytest.mark.postgres_only
async def test_patterns_wasted(client, db, agent_id, cleanup):
    base = _hour_ago()

    async def turn_calls(calls: list[tuple[str, str, bool]], *, seconds: list[int] | None = None) -> None:
        turn = uuid.uuid4()
        for i, (tool, input_text, is_error) in enumerate(calls):
            await insert_call(
                db,
                agent_id,
                turn,
                i,
                session_id="sess-W",
                tool=tool,
                input_text=input_text,
                is_error=is_error,
                started_at=base + timedelta(seconds=seconds[i] if seconds else i),
            )

    await turn_calls([("bash", "ls", False), ("bash", "ls", False)])  # repeated 1
    await turn_calls([("read_file", "x", True), ("read_file", "x", True)])  # repeated 1, retried and failed 1
    await turn_calls([("bash", "pwd", False)] * 3)  # repeated 2: each call after the first
    await turn_calls([("web_fetch", "u", True), ("web_fetch", "u", False)])  # repeated 1; the retry worked
    await turn_calls([("db_query", "q", True), ("bash", "q", True), ("db_query", "q2", True)])  # no repeat
    # In call order: error, ok, error (repeated 2, no failed retry); in time order it would read error, error, ok.
    await turn_calls(
        [("write_file", "w", True), ("write_file", "w", False), ("write_file", "w", True)], seconds=[0, 2, 1]
    )
    # The same call in two turns, and two turnless calls: no repeat.
    await turn_calls([("bash", "same", True)])
    await turn_calls([("bash", "same", True)])
    for _ in range(2):
        await insert_call(db, agent_id, None, 0, session_id="sess-W", tool="bash", input_text="solo", is_error=True)

    assert (await get_json(client, PATTERNS))["wasted"] == {"repeated_calls": 7, "retried_and_failed_again": 1}


@pytest.mark.postgres_only
async def test_patterns_window_and_agent_scope(client, db, agent_id, cleanup):
    now = datetime.now(UTC)
    old, new = now - timedelta(days=8), now - timedelta(hours=1)
    await insert_call(db, agent_id, uuid.uuid4(), 0, session_id="sess-old", reason="old", started_at=old)
    await insert_call(db, agent_id, uuid.uuid4(), 0, session_id="sess-new", reason="new", started_at=new)
    other = _agent()
    try:
        # Another agent: failing calls, reasons and a repeat under my session id, none of them mine.
        await insert_calls(db, other, "theirs", 25, errors=25, session_id="sess-new")
        shared_turn = uuid.uuid4()
        for i in range(2):
            await insert_call(
                db, other, shared_turn, i, session_id="sess-new", input_text="ls", reason="theirs", is_error=True
            )

        body = await get_json(client, PATTERNS)  # default: 7 days
        assert body["window_days"] == 7
        assert [(t["tool"], t["calls"], t["sessions"]) for t in body["tools"]] == [("bash", 1, 1)]
        assert _tools(body)["bash"]["reasons"] == [{"text": "new", "count": 1}]
        assert body["reason_coverage"] == {"calls": 1, "with_reason": 1}
        assert (body["failing"], body["wasted"]) == ([], NO_WASTE)

        wide = await get_json(client, PATTERNS, days="9")
        assert wide["window_days"] == 9
        assert [(t["tool"], t["calls"], t["sessions"]) for t in wide["tools"]] == [("bash", 2, 2)]
        assert wide["reason_coverage"] == {"calls": 2, "with_reason": 2}
    finally:
        await delete_agent_rows(db, other)


@pytest.mark.postgres_only
@pytest.mark.parametrize("days", ["0", "31", "abc", "-1", "7.5"])
async def test_patterns_days_bounds_400(client, days):
    resp = await client.get(PATTERNS, params={"days": days})
    assert resp.status_code == 400 and set(resp.json()) == {"error"}


@pytest.mark.postgres_only
async def test_patterns_days_bounds_accepted(client):
    assert (await get_json(client, PATTERNS, days="1"))["window_days"] == 1
    assert (await get_json(client, PATTERNS, days="30"))["window_days"] == 30


@pytest.mark.postgres_only
async def test_patterns_run_under_a_10s_statement_timeout(db, agent_id):
    async with db.session() as session:
        await wv.get_patterns(session, agent_id, days=7)
        assert (await session.execute(text("SHOW statement_timeout"))).scalar_one() == "10s"


@pytest.mark.postgres_only
async def test_patterns_query_failure_is_a_plain_500(client, monkeypatch, caplog):
    async def boom(*args, **kwargs):
        raise RuntimeError("secret detail from the database")

    monkeypatch.setattr(wv, "get_patterns", boom)
    resp = await client.get(PATTERNS)
    assert resp.status_code == 500 and resp.json() == {"error": "work view query failed"}
    assert any(r.levelname == "WARNING" and r.exc_info for r in caplog.records)


# ---------------------------------------------------------------------------------------------------------------------
# GET /dashboard/model-calls
# ---------------------------------------------------------------------------------------------------------------------

MODEL_CALLS = "/dashboard/model-calls"
# The brief's numbers: a cached main-loop call.
BRIEF_TOKENS = {"input_uncached": 2, "cache_read": 46077, "cache_write": 8453, "output_tokens": 612}
NO_TOKENS = {"input_uncached": None, "cache_read": None, "cache_write": None, "output_tokens": None}


@pytest_asyncio.fixture
async def mc_client(db, agent_id):
    """The REST app with the model-call log on (the tool-call log stays off: the model-call endpoints need only
    their own flag)."""
    app = make_app(db, _settings(agent_id, model_call_log_enabled=True))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


def _call_ids(body: dict) -> list[str]:
    return [c["id"] for c in body["calls"]]


def _purposes(body: dict) -> dict[str, dict]:
    return {p["purpose"]: p for p in body["purposes"]}


def test_parse_model_calls_query():
    assert wv.parse_model_calls_query({}) == {
        "days": 7,
        "purpose": None,
        "model": None,
        "session_id": None,
        "turn_id": None,
        "status": None,
        "limit": 50,
        "before": None,
    }
    turn, key = uuid.uuid4(), uuid.uuid4()
    ts = datetime(2026, 11, 8, 10, 0, 0, 1, tzinfo=UTC)
    assert wv.parse_model_calls_query(
        {
            "days": "30",
            "purpose": "chat",
            "model": "claude-x",
            "session_id": "intent-x:y",
            "turn_id": str(turn).upper(),
            "status": "problem",
            "limit": "100",
            "before": wv.format_cursor(ts, str(key).upper()),
        }
    ) == {
        "days": 30,
        "purpose": "chat",
        "model": "claude-x",
        "session_id": "intent-x:y",
        "turn_id": turn,
        "status": "problem",
        "limit": 100,
        "before": (ts, str(key)),  # the canonical form of the id
    }


@pytest.mark.parametrize(
    "params",
    [
        {"days": "0"},
        {"days": "31"},
        {"limit": "0"},
        {"limit": "101"},
        {"status": "x"},
        {"status": "OK"},
        {"turn_id": "not-a-uuid"},
        {"before": "garbage"},
        {"before": "2026-11-08T10:00:00+00:00|not-a-uuid"},
        {"before": f"2026-11-08T10:00:00|{uuid.UUID(int=1)}"},
    ],
)
def test_parse_model_calls_query_refuses_bad_values(params):
    with pytest.raises(ValueError):
        wv.parse_model_calls_query(params)


def test_disabled_model_calls_has_the_page_keys_and_nothing_in_them():
    assert wv.disabled_model_calls(7, False) == {
        "enabled": False,
        "text_enabled": False,
        "window_days": 7,
        "purposes": [],
        "calls": [],
        "next_before": None,
    }
    assert wv.disabled_model_calls(30, True)["text_enabled"] is True


@pytest.mark.postgres_only
async def test_model_calls_flag_off(db, agent_id, cleanup, monkeypatch):
    await insert_model_call(db, agent_id, purpose="chat", started_at=_hour_ago())
    opened = 0
    real_session = db.session

    def counting_session(*args, **kwargs):
        nonlocal opened
        opened += 1
        return real_session(*args, **kwargs)

    monkeypatch.setattr(db, "session", counting_session)
    # The tool-call log on does not open the model-call endpoint.
    off = make_app(db, _settings(agent_id, model_call_log_enabled=False, tool_call_log_enabled=True))
    async with AsyncClient(transport=ASGITransport(app=off), base_url="http://test") as c:
        assert await get_json(c, MODEL_CALLS) == wv.disabled_model_calls(7, False)
        assert await get_json(c, MODEL_CALLS, days="30") == wv.disabled_model_calls(30, False)
        assert (await c.get(MODEL_CALLS, params={"status": "x"})).status_code == 400  # parsing still applies
    assert opened == 0

    # The counter does see the handler's session when the log is on (so the zero above means something).
    on = make_app(db, _settings(agent_id, model_call_log_enabled=True))
    async with AsyncClient(transport=ASGITransport(app=on), base_url="http://test") as c:
        body = await get_json(c, MODEL_CALLS)
    assert body["enabled"] is True and len(body["calls"]) == 1 and opened == 1


@pytest.mark.postgres_only
async def test_model_calls_text_enabled_needs_the_setting_and_a_store(db, agent_id, cleanup, monkeypatch):
    def app(log: bool, text_on: bool, store):
        settings = _settings(agent_id, model_call_log_enabled=log, model_call_text_enabled=text_on)
        mocks = (MagicMock(), MagicMock(), MagicMock(), MagicMock())
        return create_app(*mocks, db, settings, model_call_text_store=store)

    # A lazy proxy whose component is None is falsy: the handler tests truthiness, not ``is not None``.
    empty_proxy = MagicMock()
    empty_proxy.__bool__.return_value = False
    cases = [
        (True, True, MagicMock(), True),
        (True, True, None, False),
        (True, True, empty_proxy, False),
        (True, False, MagicMock(), False),
        (False, True, MagicMock(), True),  # the flag-off payload still says whether text is on
        (False, True, None, False),
    ]
    for log, text_on, store, expected in cases:
        async with AsyncClient(transport=ASGITransport(app=app(log, text_on, store)), base_url="http://test") as c:
            body = await get_json(c, MODEL_CALLS)
        assert (body["enabled"], body["text_enabled"]) == (log, expected), (log, text_on, store)


@pytest.mark.postgres_only
async def test_model_calls_numbers_and_input_total(mc_client, db, agent_id, cleanup):
    base = _hour_ago()
    turn = uuid.uuid4()
    ok = await insert_model_call(
        db,
        agent_id,
        turn_id=turn,
        session_id="sess-N",
        turn_number=4,
        purpose="chat",
        model="claude-opus-test",
        stop_reason="end_turn",
        duration_ms=4321,
        started_at=base,
        **BRIEF_TOKENS,
    )
    failed = await insert_model_call(
        db,
        agent_id,
        purpose="critic",
        model=None,
        status="error",
        http_status=529,
        error_text="overloaded",
        started_at=base + timedelta(seconds=1),
        **NO_TOKENS,
    )
    body = await get_json(mc_client, MODEL_CALLS)
    assert body["enabled"] is True and body["window_days"] == 7 and body["next_before"] is None
    assert body["calls"] == [
        {
            "id": str(failed),
            "started_at": (base + timedelta(seconds=1)).isoformat(),
            "purpose": "critic",
            "label_source": "explicit",
            "call_kind": "call",
            "model": None,
            "status": "error",
            "http_status": 529,
            "stop_reason": None,
            "input_uncached": None,
            "cache_read": None,
            "cache_write": None,
            "input_total": None,  # no usage recorded: null, never the generated column's 0
            "output_tokens": None,
            "duration_ms": None,
            "attempts": 1,
            "call_count": 1,
            "session_id": None,
            "turn_id": None,
            "turn_number": None,
            "error": "overloaded",
            "problem": "Failed (HTTP 529)",
        },
        {
            "id": str(ok),
            "started_at": base.isoformat(),
            "purpose": "chat",
            "label_source": "explicit",
            "call_kind": "call",
            "model": "claude-opus-test",
            "status": "ok",
            "http_status": None,
            "stop_reason": "end_turn",
            "input_uncached": 2,
            "cache_read": 46077,
            "cache_write": 8453,
            "input_total": 54532,
            "output_tokens": 612,
            "duration_ms": 4321,
            "attempts": 1,
            "call_count": 1,
            "session_id": "sess-N",
            "turn_id": str(turn),
            "turn_number": 4,
            "error": None,
            "problem": None,
        },
    ]
    # The chips count the failed call's tokens as zero, never null.
    assert _purposes(body)["critic"] == {
        "purpose": "critic",
        "calls": 1,
        "input_total": 0,
        "output_tokens": 0,
        "errors": 1,
        "bucketed": False,
    }
    assert _purposes(body)["chat"] == {
        "purpose": "chat",
        "calls": 1,
        "input_total": 54532,
        "output_tokens": 612,
        "errors": 0,
        "bucketed": False,
    }


@pytest.mark.postgres_only
async def test_model_calls_filters(mc_client, db, agent_id, cleanup):
    base = _hour_ago()
    turn = uuid.uuid4()
    at = iter(base + timedelta(seconds=i) for i in range(20))
    chat = await insert_model_call(
        db, agent_id, turn_id=turn, session_id="sess-F", purpose="chat", model="m-a", started_at=next(at)
    )
    other_turn = await insert_model_call(
        db, agent_id, turn_id=uuid.uuid4(), session_id="sess-F", purpose="chat", model="m-b", started_at=next(at)
    )
    critic = await insert_model_call(db, agent_id, session_id="sess-G", purpose="critic", started_at=next(at))
    errored = await insert_model_call(db, agent_id, purpose="critic", status="error", started_at=next(at))
    cancelled = await insert_model_call(db, agent_id, purpose="chat", status="cancelled", started_at=next(at))
    resent = await insert_model_call(db, agent_id, purpose="chat", attempts=2, started_at=next(at))
    fallback = await insert_model_call(db, agent_id, purpose="chat", is_fallback=True, started_at=next(at))
    unlabelled = await insert_model_call(db, agent_id, purpose="unlabelled", label_source="none", started_at=next(at))

    async def ids(**params) -> set[str]:
        return set(_call_ids(await get_json(mc_client, MODEL_CALLS, **params)))

    every = {chat, other_turn, critic, errored, cancelled, resent, fallback, unlabelled}
    assert await ids() == {str(i) for i in every}
    assert await ids(purpose="critic") == {str(critic), str(errored)}
    assert await ids(model="m-b") == {str(other_turn)}
    assert await ids(session_id="sess-F") == {str(chat), str(other_turn)}
    assert await ids(turn_id=str(turn)) == {str(chat)}
    assert await ids(status="ok") == {str(i) for i in every - {errored, cancelled}}
    assert await ids(status="error") == {str(errored)}
    assert await ids(status="cancelled") == {str(cancelled)}
    # problem: a failure, a re-send, a fallback, or no purpose recorded (a cancelled call is not a problem).
    assert await ids(status="problem") == {str(errored), str(resent), str(fallback), str(unlabelled)}
    # Filters combine.
    assert await ids(purpose="chat", status="problem") == {str(resent), str(fallback)}
    assert await ids(session_id="sess-F", model="m-a", turn_id=str(turn), purpose="chat") == {str(chat)}
    assert await ids(session_id="sess-nothing") == set()


@pytest.mark.postgres_only
async def test_model_calls_problem_text(mc_client, db, agent_id, cleanup):
    base = _hour_ago()
    # Each case also carries every later reason, so only "first match wins" gives the expected text.
    later = {"attempts": 3, "is_fallback": True, "label_source": "none"}
    cases = [
        ("failed with a status", {"status": "error", "http_status": 529, **later}, "Failed (HTTP 529)"),
        ("failed with no status", {"status": "error", **later}, "Failed"),
        ("sent three times", later, "Sent 3 times"),
        ("fallback", {"is_fallback": True, "label_source": "none"}, "Sent again on a fallback path"),
        ("no purpose", {"label_source": "none"}, "No purpose recorded"),
        ("fine", {}, None),
        ("cancelled", {"status": "cancelled"}, None),
        ("ok with an old status", {"http_status": 200}, None),
    ]
    ids = []
    for i, (_, fields, _) in enumerate(cases):
        ids.append(
            str(await insert_model_call(db, agent_id, purpose="chat", started_at=base + timedelta(seconds=i), **fields))
        )
    body = await get_json(mc_client, MODEL_CALLS)
    problem = {c["id"]: c["problem"] for c in body["calls"]}
    got = [problem[i] for i in ids]
    assert got == [expected for _, _, expected in cases], list(zip([n for n, _, _ in cases], got, strict=True))
    # The status=problem filter and the problem text agree: a call is listed exactly when it has a problem.
    listed = set(_call_ids(await get_json(mc_client, MODEL_CALLS, status="problem")))
    assert listed == {i for i in ids if problem[i] is not None}


@pytest.mark.postgres_only
async def test_model_calls_purposes_chips_ignore_filters_and_bucket_rows(mc_client, db, agent_id, cleanup):
    base = _hour_ago()
    chat = await insert_model_call(db, agent_id, purpose="chat", session_id="sess-C", started_at=base, **BRIEF_TOKENS)
    await insert_model_call(
        db, agent_id, purpose="chat", status="error", started_at=base + timedelta(seconds=1), **NO_TOKENS
    )
    # Minute buckets: one row counts call_count calls, their tokens summed in input_uncached, no output tokens.
    embed_ok = await insert_model_call(
        db,
        agent_id,
        purpose="embedding",
        call_kind="embedding",
        call_count=30,
        model="text-embedding-3-small",
        input_uncached=1200,
        cache_read=None,
        output_tokens=None,
        started_at=base.replace(second=0, microsecond=0),
    )
    embed_err = await insert_model_call(
        db,
        agent_id,
        purpose="embedding",
        call_kind="embedding",
        call_count=2,
        status="error",
        model="text-embedding-3-small",
        **NO_TOKENS,
        started_at=base.replace(second=0, microsecond=0),
    )
    rerank = await insert_model_call(
        db,
        agent_id,
        purpose="rerank",
        call_kind="rerank",
        call_count=5,
        model=None,
        **NO_TOKENS,
        started_at=base.replace(second=0, microsecond=0),
    )
    # One purpose with both kinds of row: an embedding whose purpose resolved to unlabelled, and an unlabelled call.
    # Its chip is bucketed (some of its calls are counted per minute); the list still shows the call.
    unlabelled_call = await insert_model_call(
        db, agent_id, purpose="unlabelled", label_source="none", started_at=base + timedelta(seconds=2)
    )
    unlabelled_bucket = await insert_model_call(
        db,
        agent_id,
        purpose="unlabelled",
        call_kind="embedding",
        call_count=3,
        input_uncached=30,
        cache_read=None,
        output_tokens=None,
        started_at=base.replace(second=0, microsecond=0),
    )
    expected_purposes = [
        {"purpose": "embedding", "calls": 32, "input_total": 1200, "output_tokens": 0, "errors": 2, "bucketed": True},
        {"purpose": "rerank", "calls": 5, "input_total": 0, "output_tokens": 0, "errors": 0, "bucketed": True},
        {"purpose": "unlabelled", "calls": 4, "input_total": 180, "output_tokens": 20, "errors": 0, "bucketed": True},
        {"purpose": "chat", "calls": 2, "input_total": 54532, "output_tokens": 612, "errors": 1, "bucketed": False},
    ]

    body = await get_json(mc_client, MODEL_CALLS)
    assert body["purposes"] == expected_purposes  # most calls first
    assert str(embed_ok) not in _call_ids(body) and str(rerank) not in _call_ids(body)
    assert str(unlabelled_bucket) not in _call_ids(body) and str(unlabelled_call) in _call_ids(body)
    assert len(body["calls"]) == 3
    assert set(_call_ids(await get_json(mc_client, MODEL_CALLS, purpose="unlabelled"))) == {
        str(unlabelled_call),
        str(unlabelled_bucket),
    }

    # The chips ignore every filter.
    for params in (
        {"purpose": "chat"},
        {"status": "error"},
        {"session_id": "sess-C"},
        {"model": "text-embedding-3-small"},
        {"turn_id": str(uuid.uuid4())},
        {"limit": "1"},
    ):
        assert (await get_json(mc_client, MODEL_CALLS, **params))["purposes"] == expected_purposes, params

    # A purpose that names the buckets lists them; a model filter alone does not.
    assert set(_call_ids(await get_json(mc_client, MODEL_CALLS, purpose="embedding"))) == {
        str(embed_ok),
        str(embed_err),
    }
    assert _call_ids(await get_json(mc_client, MODEL_CALLS, purpose="rerank")) == [str(rerank)]
    assert _call_ids(await get_json(mc_client, MODEL_CALLS, model="text-embedding-3-small")) == []
    bucket = next(
        c for c in (await get_json(mc_client, MODEL_CALLS, purpose="embedding"))["calls"] if c["id"] == str(embed_ok)
    )
    assert (bucket["call_kind"], bucket["input_total"], bucket["output_tokens"]) == ("embedding", 1200, None)
    # A bucket row shows how many calls it counts.
    counts = {c["id"]: c["call_count"] for c in (await get_json(mc_client, MODEL_CALLS, purpose="embedding"))["calls"]}
    assert counts == {str(embed_ok): 30, str(embed_err): 2}
    assert [c["call_count"] for c in (await get_json(mc_client, MODEL_CALLS, purpose="rerank"))["calls"]] == [5]
    assert str(chat) in _call_ids(await get_json(mc_client, MODEL_CALLS, purpose="chat"))


@pytest.mark.postgres_only
async def test_model_calls_window(mc_client, db, agent_id, cleanup):
    recent = await insert_model_call(db, agent_id, purpose="chat", started_at=_hour_ago())
    old = await insert_model_call(db, agent_id, purpose="critic", started_at=datetime.now(UTC) - timedelta(days=8))
    week = await get_json(mc_client, MODEL_CALLS)
    assert _call_ids(week) == [str(recent)] and set(_purposes(week)) == {"chat"}
    month = await get_json(mc_client, MODEL_CALLS, days="30")
    assert _call_ids(month) == [str(recent), str(old)] and set(_purposes(month)) == {"chat", "critic"}
    assert month["window_days"] == 30
    # A filter does not lift the window.
    assert _call_ids(await get_json(mc_client, MODEL_CALLS, purpose="critic")) == []


async def _all_model_call_pages(client, **params) -> tuple[list[str], int]:
    """Every call id across the pages (following ``next_before``), and the number of pages."""
    seen: list[str] = []
    cursor = None
    for pages in range(1, 21):  # bounded: a cursor bug must fail, not loop
        body = await get_json(client, MODEL_CALLS, **params, **({"before": cursor} if cursor else {}))
        seen += _call_ids(body)
        cursor = body["next_before"]
        if cursor is None:
            return seen, pages
    raise AssertionError(f"next_before never ended: {seen}")


@pytest.mark.postgres_only
async def test_model_calls_pagination_ties_and_agent_scope(mc_client, db, agent_id, cleanup):
    tied = _hour_ago().replace(microsecond=987654)
    tied_ids = sorted(uuid.uuid4() for _ in range(5))
    for call_id in tied_ids:
        await insert_model_call(db, agent_id, purpose="chat", started_at=tied, call_id=call_id)
    newest = await insert_model_call(db, agent_id, purpose="chat", started_at=tied + timedelta(seconds=1))
    expected = [str(newest)] + [str(i) for i in reversed(tied_ids)]  # started_at desc, then id desc

    other = _agent()
    try:
        # Another agent's calls, at the same instant and on my session, turn and purpose.
        turn = uuid.uuid4()
        for _ in range(3):
            await insert_model_call(db, other, turn_id=turn, session_id="sess-T", purpose="chat", started_at=tied)
        await insert_model_call(db, other, purpose="theirs", started_at=tied)
        seen, pages = await _all_model_call_pages(mc_client, limit="2")
        assert seen == expected and pages == 3
        seen, pages = await _all_model_call_pages(mc_client, limit="6")
        assert seen == expected and pages == 1

        first = await get_json(mc_client, MODEL_CALLS, limit="1")
        assert first["next_before"] == wv.format_cursor(tied + timedelta(seconds=1), str(newest))
        assert _purposes(first) == {
            "chat": {
                "purpose": "chat",
                "calls": 6,
                "input_total": 900,
                "output_tokens": 120,
                "errors": 0,
                "bucketed": False,
            }
        }
        assert (await get_json(mc_client, MODEL_CALLS, session_id="sess-T"))["calls"] == []
        assert (await get_json(mc_client, MODEL_CALLS, turn_id=str(turn)))["calls"] == []
    finally:
        await delete_agent_rows(db, other)


@pytest.mark.postgres_only
@pytest.mark.parametrize(
    "params",
    [
        {"turn_id": "not-a-uuid"},
        {"status": "x"},
        {"days": "31"},
        {"days": "0"},
        {"limit": "101"},
        {"before": "garbage"},
        {"before": "2026-11-08T10:00:00+00:00|not-a-uuid"},
        {"before": f"2026-11-08T10:00:00|{uuid.UUID(int=1)}"},
    ],
)
async def test_model_calls_bad_params_400(mc_client, params):
    resp = await mc_client.get(MODEL_CALLS, params=params)
    assert resp.status_code == 400 and set(resp.json()) == {"error"}


@pytest.mark.postgres_only
async def test_model_calls_error_text_cut_to_300(mc_client, db, agent_id, cleanup):
    base = _hour_ago()
    long_error = "<img src=x onerror=alert(1)>" + "e" * 2000
    long_id = await insert_model_call(
        db, agent_id, purpose="chat", status="error", error_text=long_error, started_at=base
    )
    exact_id = await insert_model_call(
        db, agent_id, purpose="chat", status="error", error_text="x" * 300, started_at=base + timedelta(seconds=1)
    )
    body = await get_json(mc_client, MODEL_CALLS)
    errors = {c["id"]: c["error"] for c in body["calls"]}
    assert errors[str(long_id)] == long_error[:300]  # returned as stored, cut, not escaped
    assert errors[str(exact_id)] == "x" * 300


@pytest.mark.postgres_only
async def test_model_calls_run_under_a_10s_statement_timeout(db, agent_id):
    async with db.session() as session:
        await wv.get_model_calls(
            session,
            agent_id,
            days=7,
            purpose=None,
            model=None,
            session_id=None,
            turn_id=None,
            status=None,
            limit=50,
            before=None,
            text_enabled=False,
        )
        assert (await session.execute(text("SHOW statement_timeout"))).scalar_one() == "10s"


@pytest.mark.postgres_only
async def test_model_calls_query_failure_is_a_plain_500(mc_client, monkeypatch, caplog):
    async def boom(*args, **kwargs):
        raise RuntimeError("secret detail from the database")

    monkeypatch.setattr(wv, "get_model_calls", boom)
    resp = await mc_client.get(MODEL_CALLS)
    assert resp.status_code == 500 and resp.json() == {"error": "work view query failed"}
    assert any(r.levelname == "WARNING" and r.exc_info for r in caplog.records)


# ---------------------------------------------------------------------------------------------------------------------
# GET /dashboard/model-calls/{call_id} and /dashboard/model-calls/{call_id}/blocks/{part}/{ord}
# ---------------------------------------------------------------------------------------------------------------------

# The detail's call fields on top of a list item's.
DETAIL_EXTRAS = {
    "group_id",
    "is_fallback",
    "parent_session_id",
    "frame_id",
    "subtask_id",
    "dag_id",
    "trace_id",
    "call_count",
    "error_text",
    "context_window_size",
    "total_tokens_est",
    "utilization_pct",
    "tools_count",
    "tool_names",
    "messages_count",
    "loaded_facts",
    "loaded_decisions",
    "loaded_procedures",
    "loaded_episodes",
    "cache_break",
    "cache_break_components",
    "cache_break_tokens_lost",
}
HOSTILE = '<script>alert("x")</script><img src=x onerror=alert(1)>'


def call_path(call_id) -> str:
    return f"{MODEL_CALLS}/{call_id}"


def block_path(call_id, part: str, ord_) -> str:
    return f"{MODEL_CALLS}/{call_id}/blocks/{part}/{ord_}"


def make_text_store(db, agent_id: str) -> TextStore:
    """A text store of ``agent_id``; the tests await its ``store`` instead of submitting through the writer."""
    flags = SimpleNamespace(
        model_call_log_enabled=True, model_call_text_enabled=True, model_call_text_block_max_chars=200_000
    )
    return TextStore(BoundedWriter("test"), db, agent_id, flags)


def text_app(db, agent_id: str, store, *, log: bool = True, text_on: bool = True):
    """The REST app with the model-call log ``log`` and its text setting ``text_on``, reading ``store``."""
    settings = _settings(agent_id, model_call_log_enabled=log, model_call_text_enabled=text_on)
    return create_app(MagicMock(), MagicMock(), MagicMock(), MagicMock(), db, settings, model_call_text_store=store)


@pytest.fixture
def text_store(db, agent_id) -> TextStore:
    return make_text_store(db, agent_id)


@pytest_asyncio.fixture
async def mct_client(db, agent_id, text_store):
    """The REST app with the model-call log and its text on, reading ``text_store``."""
    app = text_app(db, agent_id, text_store)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def update_model_call(db, call_id: uuid.UUID, **columns) -> None:
    """Set the context columns ``insert_model_call`` leaves at their defaults (column names chosen by the test)."""
    sets = ", ".join(
        f"{name} = CAST(:{name} AS jsonb)" if name == "section_tokens_est" else f"{name} = :{name}" for name in columns
    )
    params = {k: json.dumps(v) if k == "section_tokens_est" else v for k, v in columns.items()}
    async with db.engine.begin() as conn:
        await conn.execute(
            text(f"UPDATE nous_system.model_call_log SET {sets} WHERE id = :id"), {**params, "id": call_id}
        )


async def insert_attempt(
    db,
    agent_id: str,
    call_id: uuid.UUID,
    attempt_no: int,
    *,
    started_at: datetime,
    duration_ms: int | None = None,
    http_status: int | None = None,
    error_type: str | None = None,
    error_text: str | None = None,
) -> None:
    """One ``model_call_attempt`` row of call ``call_id``."""
    async with db.engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO nous_system.model_call_attempt (call_id, agent_id, attempt_no, started_at, duration_ms, "
                "http_status, error_type, error_text) VALUES (:c, :a, :n, :start, :duration, :http, :type, :error)"
            ),
            {
                "c": call_id,
                "a": agent_id,
                "n": attempt_no,
                "start": started_at,
                "duration": duration_ms,
                "http": http_status,
                "type": error_type,
                "error": error_text,
            },
        )


async def stored_body(db, agent_id: str, call_id: uuid.UUID, part: str, ord_: int) -> str:
    """The body stored for one block of a call, read straight from the tables."""
    async with db.engine.connect() as conn:
        return (
            await conn.execute(
                text(
                    "SELECT b.body FROM nous_system.model_call_block_ref r JOIN nous_system.model_text_block b "
                    "ON b.agent_id = r.agent_id AND b.hash = r.block_hash "
                    "WHERE r.call_id = :c AND r.agent_id = :a AND r.part = :p AND r.ord = :o"
                ),
                {"c": call_id, "a": agent_id, "p": part, "o": ord_},
            )
        ).scalar_one()


def _keys_anywhere(value) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {k for v in value.values() for k in _keys_anywhere(v)}
    if isinstance(value, list):
        return {k for v in value for k in _keys_anywhere(v)}
    return set()


def test_parse_call_id():
    call_id = uuid.uuid4()
    assert wv.parse_call_id(str(call_id).upper()) == call_id
    for raw in ("", "not-a-uuid", "123"):
        with pytest.raises(ValueError):
            wv.parse_call_id(raw)


def test_disabled_model_call_has_the_detail_keys_and_nothing_in_them():
    assert wv.disabled_model_call(False) == {
        "enabled": False,
        "text_enabled": False,
        "call": None,
        "attempts": [],
        "sections": [],
        "turn": None,
        "text_status": "off",
        "blocks": [],
    }
    assert wv.disabled_model_call(True)["text_enabled"] is True


@pytest.mark.postgres_only
async def test_model_call_detail_fields(mct_client, db, agent_id, cleanup):
    base = _hour_ago()
    turn, group, subtask, dag = uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    long_error = "<b>upstream</b> " + "e" * 1000
    call_id = await insert_model_call(
        db,
        agent_id,
        turn_id=turn,
        session_id="sess-D",
        turn_number=3,
        purpose="chat",
        model="claude-opus-test",
        stop_reason="end_turn",
        duration_ms=5100,
        attempts=2,
        is_fallback=True,
        error_text=long_error,
        started_at=base,
        **BRIEF_TOKENS,
    )
    await update_model_call(
        db,
        call_id,
        group_id=group,
        parent_session_id="sess-P",
        frame_id="task",
        subtask_id=subtask,
        dag_id=dag,
        trace_id="trace-1",
        context_window_size=200_000,
        total_tokens_est=54_000,
        utilization_pct=27.5,
        tools_count=2,
        tool_names=["bash", "recall_deep"],
        messages_count=5,
        loaded_facts=3,
        loaded_decisions=2,
        loaded_procedures=1,
        loaded_episodes=0,
        cache_break=True,
        cache_break_components=["system", "tools"],
        cache_break_tokens_lost=1234,
        section_tokens_est={"identity": 500, "tools": 2000, "facts": 2000, "history": 9000},
    )
    # Attempts written out of order; the first one failed.
    await insert_attempt(db, agent_id, call_id, 2, started_at=base + timedelta(seconds=2), duration_ms=3000)
    await insert_attempt(
        db,
        agent_id,
        call_id,
        1,
        started_at=base,
        duration_ms=2000,
        http_status=529,
        error_type="overloaded_error",
        error_text="Overloaded",
    )
    # The session's title: its latest active titled episode.
    await insert_episode(db, agent_id, "sess-D", "Old title", started_at=base - timedelta(hours=2))
    await insert_episode(db, agent_id, "sess-D", "Plan the trip", started_at=base - timedelta(hours=1))
    await insert_episode(db, agent_id, "sess-D", "Deleted", started_at=base, active=False)

    body = await get_json(mct_client, call_path(call_id))
    assert set(body) == {"enabled", "text_enabled", "call", "attempts", "sections", "turn", "text_status", "blocks"}
    assert body["enabled"] is True and body["text_enabled"] is True

    # Every list field, as the list shows it, plus the detail's own.
    listed = next(c for c in (await get_json(mct_client, MODEL_CALLS))["calls"] if c["id"] == str(call_id))
    assert set(body["call"]) == set(listed) | DETAIL_EXTRAS
    assert {k: body["call"][k] for k in listed} == listed
    assert listed["error"] == long_error[:300] and listed["problem"] == "Sent 2 times"
    assert {k: body["call"][k] for k in DETAIL_EXTRAS} == {
        "group_id": str(group),
        "is_fallback": True,
        "parent_session_id": "sess-P",
        "frame_id": "task",
        "subtask_id": str(subtask),
        "dag_id": str(dag),
        "trace_id": "trace-1",
        "call_count": 1,
        "error_text": long_error,  # in full, as stored
        "context_window_size": 200_000,
        "total_tokens_est": 54_000,
        "utilization_pct": 27.5,
        "tools_count": 2,
        "tool_names": ["bash", "recall_deep"],
        "messages_count": 5,
        "loaded_facts": 3,
        "loaded_decisions": 2,
        "loaded_procedures": 1,
        "loaded_episodes": 0,
        "cache_break": True,
        "cache_break_components": ["system", "tools"],
        "cache_break_tokens_lost": 1234,
    }
    assert body["attempts"] == [
        {
            "attempt_no": 1,
            "started_at": base.isoformat(),
            "duration_ms": 2000,
            "http_status": 529,
            "error_type": "overloaded_error",
            "error_text": "Overloaded",
        },
        {
            "attempt_no": 2,
            "started_at": (base + timedelta(seconds=2)).isoformat(),
            "duration_ms": 3000,
            "http_status": None,
            "error_type": None,
            "error_text": None,
        },
    ]
    # Largest first; a tie goes by name.
    assert body["sections"] == [
        {"name": "history", "tokens_est": 9000},
        {"name": "facts", "tokens_est": 2000},
        {"name": "tools", "tokens_est": 2000},
        {"name": "identity", "tokens_est": 500},
    ]
    assert body["turn"] == {"turn_id": str(turn), "session_id": "sess-D", "session_title": "Plan the trip"}
    assert (body["text_status"], body["blocks"]) == ("not_recorded", [])

    # A call with no turn has no turn; a turn whose session has no titled episode has no title; no sections: [].
    bare = await insert_model_call(db, agent_id, purpose="critic", session_id="sess-D", started_at=base)
    untitled = await insert_model_call(db, agent_id, turn_id=uuid.uuid4(), session_id="sess-U", started_at=base)
    await insert_episode(db, agent_id, "sess-U", None, started_at=base)
    sessionless = await insert_model_call(db, agent_id, turn_id=turn, started_at=base)
    detail = await get_json(mct_client, call_path(bare))
    assert (detail["turn"], detail["sections"], detail["attempts"]) == (None, [], [])
    assert detail["call"]["error_text"] is None and detail["call"]["tool_names"] is None
    assert (await get_json(mct_client, call_path(untitled)))["turn"]["session_title"] is None
    assert (await get_json(mct_client, call_path(sessionless)))["turn"] == {
        "turn_id": str(turn),
        "session_id": None,
        "session_title": None,
    }


@pytest.mark.postgres_only
async def test_model_call_text_status(db, agent_id, cleanup, text_store):
    eight_days_ago = datetime.now(UTC) - timedelta(days=8)
    recent = await insert_model_call(db, agent_id, purpose="chat", started_at=_hour_ago())
    stored = await insert_model_call(db, agent_id, purpose="chat", started_at=_hour_ago())
    old = await insert_model_call(db, agent_id, purpose="chat", started_at=eight_days_ago)
    old_kept = await insert_model_call(db, agent_id, purpose="chat", started_at=eight_days_ago)
    await text_store.store(stored, [("system", 0, "be brief", False), ("response", 0, '[{"type": "text"}]', True)])
    await text_store.store(old_kept, [("system", 0, "be brief", False)])  # older than the retention, not swept yet

    async def status(call_id, store=text_store, **flags) -> tuple[str, bool, list]:
        app = text_app(db, agent_id, store, **flags)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            body = await get_json(c, call_path(call_id))
        return body["text_status"], body["text_enabled"], body["blocks"]

    text_status, text_enabled, blocks = await status(stored)
    assert (text_status, text_enabled) == ("ok", True)
    assert [(b["part"], b["ord"], b["reused_by"]) for b in blocks] == [("system", 0, 2), ("response", 0, 1)]
    assert await status(old_kept) == (
        "ok",
        True,
        [{"part": "system", "ord": 0, "size_chars": 8, "tokens_est": 2, "reused_by": 2}],
    )
    assert await status(recent) == ("not_recorded", True, [])
    assert await status(old) == ("expired", True, [])  # older than the 7-day text retention, no blocks
    # The text setting off, or no store: off, even where blocks exist.
    assert await status(stored, text_on=False) == ("off", False, [])
    assert await status(stored, store=None) == ("off", False, [])
    empty_proxy = MagicMock()
    empty_proxy.__bool__.return_value = False
    assert await status(stored, store=empty_proxy) == ("off", False, [])

    # A retention of 0 days means the text is never swept: an old call with no blocks was not recorded.
    async with db.session() as session:
        never_swept = await wv.get_model_call(session, agent_id, old, text_store=text_store, text_retention_days=0)
        three_days = await wv.get_model_call(session, agent_id, old, text_store=text_store, text_retention_days=3)
        nine_days = await wv.get_model_call(session, agent_id, old, text_store=text_store, text_retention_days=9)
    assert never_swept["text_status"] == "not_recorded"
    assert three_days["text_status"] == "expired"
    assert nine_days["text_status"] == "not_recorded"


@pytest.mark.postgres_only
async def test_model_call_blocks_are_metadata_only(mct_client, db, agent_id, cleanup, text_store):
    call_id = await insert_model_call(db, agent_id, purpose="chat", started_at=_hour_ago())
    big = ("The quick brown fox jumps over the lazy dog. " * 5000)[:200_000]
    await text_store.store(
        call_id,
        [("system", 0, big, False), ("messages", 0, '{"role": "user", "content": "hi"}', True)],
    )
    resp = await mct_client.get(call_path(call_id))
    assert resp.status_code == 200
    body = resp.json()
    assert "body" not in _keys_anywhere(body)
    assert len(resp.content) < 10_000  # no block's text rides along under another key
    message_chars = len(await stored_body(db, agent_id, call_id, "messages", 0))
    assert body["blocks"] == [
        {"part": "system", "ord": 0, "size_chars": 200_000, "tokens_est": 50_000, "reused_by": 1},
        {"part": "messages", "ord": 0, "size_chars": message_chars, "tokens_est": message_chars // 4, "reused_by": 1},
    ]


@pytest.mark.postgres_only
async def test_block_body_endpoint(mct_client, db, agent_id, cleanup, text_store):
    call_id = await insert_model_call(db, agent_id, purpose="chat", started_at=_hour_ago())
    message = {"role": "user", "content": HOSTILE}
    await text_store.store(call_id, [("system", 0, HOSTILE, False), ("messages", 0, json.dumps(message), True)])

    resp = await mct_client.get(block_path(call_id, "system", 0))
    assert resp.status_code == 200 and resp.headers["content-type"].startswith("application/json")
    assert await stored_body(db, agent_id, call_id, "system", 0) == HOSTILE  # nothing in it to redact
    assert resp.json() == {"part": "system", "ord": 0, "size_chars": len(HOSTILE), "body": HOSTILE}  # verbatim
    msg = await get_json(mct_client, block_path(call_id, "messages", 0))
    assert msg["body"] == await stored_body(db, agent_id, call_id, "messages", 0)
    assert json.loads(msg["body"]) == message and msg["size_chars"] == len(msg["body"])

    # Another agent's call, with a block at the same address.
    other = _agent()
    try:
        foreign = await insert_model_call(db, other, purpose="chat", started_at=_hour_ago())
        await make_text_store(db, other).store(foreign, [("system", 0, "not yours", False)])
        for path in (
            block_path(call_id, "secrets", 0),  # not a part
            block_path(call_id, "tools", 0),  # a part this call does not have
            block_path(call_id, "system", 1),  # past the call's blocks
            block_path(call_id, "messages", 7),
            block_path(call_id, "system", 2**31),  # past the INT column: cannot exist
            block_path(call_id, "system", "x"),  # not a number: no route
            block_path(uuid.uuid4(), "system", 0),
            block_path(foreign, "system", 0),
        ):
            assert (await mct_client.get(path)).status_code == 404, path
    finally:
        await delete_agent_rows(db, other)
    resp = await mct_client.get(block_path("not-a-uuid", "system", 0))
    assert resp.status_code == 400 and set(resp.json()) == {"error"}

    # Text off (the setting, the store, or the model-call log): 404.
    for store, flags in ((text_store, {"text_on": False}), (text_store, {"log": False}), (None, {})):
        app = text_app(db, agent_id, store, **flags)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            assert (await c.get(block_path(call_id, "system", 0))).status_code == 404, (store, flags)


@pytest.mark.postgres_only
async def test_detail_unknown_call_404(mct_client, db, agent_id, cleanup):
    other = _agent()
    try:
        foreign = await insert_model_call(db, other, purpose="chat", started_at=_hour_ago())
        for call_id in (uuid.uuid4(), foreign):
            resp = await mct_client.get(call_path(call_id))
            assert resp.status_code == 404 and resp.json() == {"error": "not found"}
    finally:
        await delete_agent_rows(db, other)
    resp = await mct_client.get(call_path("not-a-uuid"))
    assert resp.status_code == 400 and set(resp.json()) == {"error"}


@pytest.mark.postgres_only
async def test_detail_flag_off(db, agent_id, cleanup, monkeypatch, text_store):
    call_id = await insert_model_call(db, agent_id, purpose="chat", started_at=_hour_ago())
    await text_store.store(call_id, [("system", 0, "be brief", False)])
    opened = 0
    real_session = db.session

    def counting_session(*args, **kwargs):
        nonlocal opened
        opened += 1
        return real_session(*args, **kwargs)

    monkeypatch.setattr(db, "session", counting_session)
    # The model-call log off (the tool-call log on does not open the model-call endpoints).
    settings = _settings(
        agent_id, model_call_log_enabled=False, model_call_text_enabled=True, tool_call_log_enabled=True
    )
    off = create_app(MagicMock(), MagicMock(), MagicMock(), MagicMock(), db, settings, model_call_text_store=text_store)
    async with AsyncClient(transport=ASGITransport(app=off), base_url="http://test") as c:
        assert await get_json(c, call_path(call_id)) == wv.disabled_model_call(True)
        assert (await c.get(block_path(call_id, "system", 0))).status_code == 404
        assert (await c.get(call_path("not-a-uuid"))).status_code == 400  # parsing still applies
    # The text off: the detail opens one session for the call's numbers, the block endpoint none.
    text_off = text_app(db, agent_id, text_store, text_on=False)
    async with AsyncClient(transport=ASGITransport(app=text_off), base_url="http://test") as c:
        assert (await c.get(block_path(call_id, "system", 0))).status_code == 404
        assert opened == 0
        assert (await get_json(c, call_path(call_id)))["text_status"] == "off"
    assert opened == 1

    # Both on: the handler's session, then the text store's (so the zeros above mean something).
    both_on = text_app(db, agent_id, text_store)
    async with AsyncClient(transport=ASGITransport(app=both_on), base_url="http://test") as c:
        assert (await get_json(c, call_path(call_id)))["text_status"] == "ok"
        assert opened == 3
        assert (await get_json(c, block_path(call_id, "system", 0)))["body"] == "be brief"
        assert opened == 4


@pytest.mark.postgres_only
async def test_model_call_detail_runs_under_a_10s_statement_timeout(db, agent_id, cleanup):
    call_id = await insert_model_call(db, agent_id, purpose="chat", started_at=_hour_ago())
    async with db.session() as session:
        detail = await wv.get_model_call(session, agent_id, call_id, text_store=None, text_retention_days=7)
        assert detail["call"]["id"] == str(call_id)
        assert (await session.execute(text("SHOW statement_timeout"))).scalar_one() == "10s"


@pytest.mark.postgres_only
async def test_model_call_detail_and_block_failures_are_a_plain_500(mct_client, monkeypatch, caplog):
    async def boom(*args, **kwargs):
        raise RuntimeError("secret detail from the database")

    monkeypatch.setattr(wv, "get_model_call", boom)
    monkeypatch.setattr(wv, "get_block", boom)
    call_id = uuid.uuid4()
    for path in (call_path(call_id), block_path(call_id, "system", 0)):
        caplog.clear()
        resp = await mct_client.get(path)
        assert resp.status_code == 500 and resp.json() == {"error": "work view query failed"}, path
        assert any(r.levelname == "WARNING" and r.exc_info for r in caplog.records), path


# ---------------------------------------------------------------------------------------------------------------------
# Across the endpoints
# ---------------------------------------------------------------------------------------------------------------------


@pytest.mark.postgres_only
async def test_sessions_repeat_flag_and_patterns_repeated_calls_agree(client, db, agent_id, cleanup):
    """The list's ``repeat`` flag and the patterns' ``repeated_calls`` share one key (turn, tool, input), so they
    agree on the same rows. Their populations differ by design: the list sees only the calls of turns that have a turn
    row in the window, the patterns every call with a turn id. That is harmless, because execute_single_call mints a
    turn id per call, so a turnless group never holds two calls."""
    turn = await insert_turn(db, agent_id, "sess-X", started_at=_hour_ago())
    await insert_call(db, agent_id, turn, 0, session_id="sess-X", tool="bash", input_text="ls")
    await insert_call(db, agent_id, turn, 1, session_id="sess-X", tool="bash", input_text="ls -a")  # input differs

    sessions, patterns = await get_json(client), await get_json(client, PATTERNS)
    assert "repeat" not in sessions["sessions"][0]["flags"]
    assert patterns["wasted"]["repeated_calls"] == 0

    await insert_call(db, agent_id, turn, 2, session_id="sess-X", tool="bash", input_text="ls")  # the first, again

    sessions, patterns = await get_json(client), await get_json(client, PATTERNS)
    assert "repeat" in sessions["sessions"][0]["flags"]
    assert patterns["wasted"]["repeated_calls"] == 1


def _assert_same_keys(disabled: dict, enabled: dict, where: str = "payload") -> None:
    """Every dict of the flag-off payload has the keys of the enabled one at the same place (a dict that is null
    when disabled, the trace's ``session``, is compared no further)."""
    assert set(disabled) == set(enabled), where
    for key, value in disabled.items():
        if isinstance(value, dict) and isinstance(enabled[key], dict):
            _assert_same_keys(value, enabled[key], f"{where}.{key}")


@pytest.mark.postgres_only
async def test_disabled_payloads_have_the_enabled_keys(client, mc_client, db, agent_id, cleanup):
    await insert_turn(db, agent_id, "sess-K", started_at=_hour_ago())
    call_id = await insert_model_call(db, agent_id, purpose="chat", started_at=_hour_ago())
    pairs = {
        "sessions": (wv.disabled_sessions(7), await get_json(client)),
        "trace": (wv.disabled_trace(), await get_json(client, trace_path("sess-K"))),
        "patterns": (wv.disabled_patterns(7), await get_json(client, PATTERNS)),
        "model_calls": (wv.disabled_model_calls(7, False), await get_json(mc_client, MODEL_CALLS)),
        "model_call": (wv.disabled_model_call(False), await get_json(mc_client, call_path(call_id))),
    }
    for name, (disabled, enabled) in pairs.items():
        assert enabled["enabled"] is True and disabled["enabled"] is False, name
        _assert_same_keys(disabled, enabled, name)
