"""F108 Phase 2: the Work view's read-only endpoints (nous/api/work_view_dashboard.py).

The endpoint tests run against real Postgres: scratch rows go straight into the Phase 1 tables with SQL, under an
agent id of the test's own, and the ``cleanup`` fixture deletes them afterwards. The parsing tests need no database.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from nous.api import work_view_dashboard as wv
from nous.api.rest import create_app
from nous.config import Settings

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
            "nous_system.model_call_log",
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
) -> uuid.UUID:
    """One ``agent_turn_log`` row; returns its id (the turn id)."""
    turn_id = turn_id or uuid.uuid4()
    async with db.engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO nous_system.agent_turn_log (id, agent_id, session_id, parent_session_id, turn_number, "
                "work_kind, context_kind, trigger_text, tool_call_count, started_at, ended_at) "
                "VALUES (:id, :a, :s, :p, :n, :wk, :ck, :trig, :tcc, :start, :end)"
            ),
            {
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
) -> uuid.UUID:
    """One ``tool_call_log`` row; returns its id."""
    async with db.engine.begin() as conn:
        return (
            await conn.execute(
                text(
                    "INSERT INTO nous_system.tool_call_log (agent_id, session_id, turn_id, call_index, work_kind, "
                    "subtask_id, tool_name, tool_name_called, reason, input_text, outcome, is_error, "
                    "ledger_entry_id, started_at) "
                    "VALUES (:a, :s, :t, :i, :wk, :sub, :tool, :tool, :reason, :input, :outcome, :err, :ledger, "
                    ":start) RETURNING id"
                ),
                {
                    "a": agent_id,
                    "s": session_id,
                    "t": turn_id,
                    "i": call_index,
                    "wk": work_kind,
                    "sub": subtask_id,
                    "tool": tool,
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

    # A quiet session: the same call in two turns is no repeat, an approval is no ask, 9 calls is not many.
    q1 = await insert_turn(db, agent_id, "sess-T", started_at=base + timedelta(minutes=2), tool_call_count=9)
    await insert_call(db, agent_id, q1, 0, session_id="sess-T", tool="bash", input_text="pwd")
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
    assert t["flags"] == [] and (t["tool_calls"], t["errors"]) == (3, 0)
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

        by_id = _by_id(await get_json(client))
    finally:
        await delete_agent_rows(db, other)

    assert by_id["sess-E"]["title"] == "Current episode"
    assert by_id["sess-E"]["first_message"] == "trigger E"
    assert by_id["sess-S"]["title"] == long_task[:100]
    assert by_id["sess-T"]["title"] == long_trigger[:80]
    assert by_id["sess-T"]["first_message"] == long_trigger


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
async def test_sessions_query_failure_is_a_plain_500(client, monkeypatch, caplog):
    async def boom(*args, **kwargs):
        raise RuntimeError("secret detail from the database")

    monkeypatch.setattr(wv, "get_sessions", boom)
    resp = await client.get(SESSIONS)
    assert resp.status_code == 500 and resp.json() == {"error": "work view query failed"}
    assert any(r.levelname == "WARNING" and r.exc_info for r in caplog.records)
