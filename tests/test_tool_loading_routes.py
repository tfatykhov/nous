"""F120 tool loading, watch only: the routes of the card (nous/api/tool_loading_routes.py), over real Postgres.

The answers with the setting off (no session), the setting and its changes (merged, validated, stored under this
agent's key with the owner's proof, set on the running process), the per-kind numbers from ``model_call_log`` and
``tool_call_log`` and the tool table, each read for this agent only. Every test uses its own agent id and deletes its
rows and its key. The owner's proof itself is tested with the screen's other routes in test_owner_guard_routes.py.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
from sqlalchemy import text
from starlette.applications import Starlette

from nous.api.owner_guard import OwnerGuard
from nous.api.tool_loading import PINNED, ToolLoadingConfig, config_key, index_tokens, tokens_est
from nous.api.tool_loading_routes import BODY_MAX_BYTES, build_tool_loading_routes, kind_numbers
from nous.api.tools import ToolDispatcher
from nous.config import Settings
from nous.runtime_config import RuntimeConfig

OWNER_VALUE = "owner-only-test-value"
PROXY_VALUE = "proxy-only-test-value"
OWNER = {"X-Nous-Owner-Secret": OWNER_VALUE}
PROXY = {"X-Nous-Proxy-Secret": PROXY_VALUE, "content-type": "application/json"}
PATH = "/dashboard/tool-loading"
TOOLS = {
    "bash": "Run a shell command.",
    "recall_deep": "Search memory.",
    "db_query": "Read the database.",
    "send_email": "Send an email.",
    "dag_create": "Create a workflow.",
    "store_identity": "Initiation only.",  # never listed: the widest offered set leaves it out
    "mcp__ha__HassTurnOn": "[ha] Turn on a device.",
}


@pytest.fixture(autouse=True)
def _fresh_runtime_config():
    RuntimeConfig.reset()
    yield
    RuntimeConfig.reset()


def _agent() -> str:
    return f"test-tool-loading-{uuid.uuid4().hex[:12]}"


def _settings(agent_id: str, **over: Any) -> Settings:
    values = {
        "owner_route_secret_sha256": hashlib.sha256(OWNER_VALUE.encode()).hexdigest(),
        "mcp_client_ui_secret_sha256": hashlib.sha256(PROXY_VALUE.encode()).hexdigest(),
        "tool_loading_enabled": True,
        "model_call_log_enabled": True,
        "tool_call_log_enabled": True,
        **over,
    }
    return Settings(_env_file=None, agent_id=agent_id, **values)


def _dispatcher() -> ToolDispatcher:
    dispatcher = ToolDispatcher()
    for name, description in TOOLS.items():
        dispatcher.register(name, MagicMock(), {"type": "object", "description": description, "properties": {}})
    return dispatcher


def _app(database, settings, dispatcher=None) -> Starlette:
    return Starlette(
        routes=build_tool_loading_routes(
            database=database,
            settings=settings,
            dispatcher=dispatcher if dispatcher is not None else _dispatcher(),
            guard=OwnerGuard(settings),
        )
    )


async def _send(app, method, path, **kwargs) -> httpx.Response:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://nous") as client:
        return await client.request(method, path, **kwargs)


async def _stored(db, agent: str) -> Any:
    async with db.engine.connect() as conn:
        value = (
            await conn.execute(text("SELECT value FROM nous_system.config WHERE key = :k"), {"k": config_key(agent)})
        ).scalar()
    return json.loads(value) if isinstance(value, str) else value


async def _cleanup(db, *agents: str) -> None:
    async with db.engine.begin() as conn:
        for agent in agents:
            await conn.execute(text("DELETE FROM nous_system.config WHERE key = :k"), {"k": config_key(agent)})
            await conn.execute(text("DELETE FROM nous_system.model_call_log WHERE agent_id = :a"), {"a": agent})
            await conn.execute(text("DELETE FROM nous_system.tool_call_log WHERE agent_id = :a"), {"a": agent})


class _NoDatabase:
    def session(self):
        raise AssertionError("a route opened a session")


# ---- the setting off ------------------------------------------------------------------------------------------------


async def test_with_the_setting_off_the_read_says_so_and_the_change_is_409_with_no_session():
    app = _app(_NoDatabase(), _settings("off", tool_loading_enabled=False))
    read = await _send(app, "GET", PATH, headers=OWNER)
    assert (read.status_code, read.json()) == (200, {"enabled": False})
    change = await _send(app, "PUT", PATH, headers=OWNER, content=b"not even json")
    assert (change.status_code, change.json()) == (
        409,
        {"error": "Tool loading is off on this server.", "enabled": False},
    )


# ---- the read -------------------------------------------------------------------------------------------------------


async def test_the_read_with_nothing_stored_and_nothing_recorded():
    agent = _agent()
    settings = _settings(agent, model_call_log_enabled=False, tool_call_log_enabled=False)
    body = (await _send(_app(_NoDatabase(), settings), "GET", PATH, headers=OWNER)).json()
    assert body["enabled"] is True and body["mode"] == "off" and body["days"] == 7
    assert [kind["key"] for kind in body["kinds"]] == [
        "chat",
        "subtask",
        "workflow_step",
        "heartbeat",
        "other_background",
    ]
    assert not any(kind["on"] for kind in body["kinds"])
    assert body["core_default"] is True and body["core"] == ["db_query"]  # the default core set, less the pinned
    assert {item["name"] for item in body["pinned"]} == set(PINNED)
    assert body["recorded"] == {"model_calls": False, "tool_calls": False}
    assert body["numbers"] is None and body["problem"] is None
    rows = {row["name"]: row for row in body["tools"]}
    assert set(rows) == set(TOOLS) - {"store_identity"}
    assert rows["bash"]["state"] == "pinned" and rows["bash"]["why"] == PINNED["bash"]
    assert rows["db_query"]["state"] == "core" and rows["send_email"]["state"] == "left_out"
    assert rows["mcp__ha__HassTurnOn"]["server"] == "ha" and rows["bash"]["server"] is None
    assert rows["send_email"]["calls_30d"] is None and rows["send_email"]["sessions_30d"] is None
    defs = {t["name"]: t for t in _dispatcher().available_tools("task")}
    assert rows["send_email"]["tokens_est"] == tokens_est(defs["send_email"])
    left_out = [defs[n] for n in ("dag_create", "mcp__ha__HassTurnOn", "send_email")]
    assert body["totals"]["left_out"] == 3 and body["totals"]["kept"] == 3
    assert body["totals"]["left_out_tokens_est"] == sum(tokens_est(t) for t in left_out)
    assert body["totals"]["index_tokens_est"] == index_tokens(sorted(left_out, key=lambda t: t["name"]))


@pytest.mark.parametrize("days", ["0", "2", "abc", "-7", "300"])
async def test_a_window_other_than_1_7_or_30_days_is_refused(days):
    app = _app(_NoDatabase(), _settings(_agent(), model_call_log_enabled=False, tool_call_log_enabled=False))
    response = await _send(app, "GET", f"{PATH}?days={days}", headers=OWNER)
    assert (response.status_code, response.json()) == (400, {"error": "The window must be 1, 7 or 30 days."})


def _record(kind: str, deferred: int, deferred_tokens: int, index: int, cache: str | None, kept: list[str]) -> str:
    return json.dumps(
        {
            "mode": "watch",
            "kind": kind,
            "sent": deferred + len(kept),
            "deferred": deferred,
            "deferred_tokens_est": deferred_tokens,
            "index_tokens_est": index,
            "kept": kept,
            "cache": cache,
        }
    )


async def _call(conn, agent: str, turn: uuid.UUID, record: str | None, names: list[str], when: datetime) -> None:
    await conn.execute(
        text(
            "INSERT INTO nous_system.model_call_log "
            "(agent_id, purpose, label_source, call_kind, status, turn_id, tool_names, started_at, tool_loading) "
            "VALUES (:a, 'chat', 'explicit', 'call', 'ok', :t, CAST(:n AS text[]), :w, CAST(:r AS jsonb))"
        ),
        {"a": agent, "t": turn, "n": names, "w": when, "r": record},
    )


async def _tool_call(conn, agent: str, turn: uuid.UUID, name: str, when: datetime, kind: str, session: str) -> None:
    await conn.execute(
        text(
            "INSERT INTO nous_system.tool_call_log (agent_id, session_id, turn_id, call_index, work_kind, "
            "context_kind, tool_name, tool_name_called, outcome, started_at) "
            "VALUES (:a, :s, :t, 0, 'chat', :k, :n, :n, 'success', :w)"
        ),
        {"a": agent, "s": session, "t": turn, "k": kind, "n": name, "w": when},
    )


@pytest.mark.postgres_only
async def test_the_numbers_per_kind_weigh_each_call_by_its_cache_state(db):
    agent, other = _agent(), _agent()
    now = datetime.now(UTC)
    t1, t2, t3, old = uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    names = ["bash", "db_query", "send_email", "dag_create"]
    kept = ["bash", "db_query"]
    try:
        async with db.engine.begin() as conn:
            # chat: turn 1 read the tools, then wrote them; turn 2 read; one call with no usage is not weighed
            await _call(conn, agent, t1, _record("chat", 2, 1000, 200, "read", kept), names, now)
            await _call(conn, agent, t1, _record("chat", 2, 1000, 200, "written", kept), names, now)
            await _call(conn, agent, t2, _record("chat", 2, 1000, 200, "read", kept), names, now)
            await _call(conn, agent, t2, _record("chat", 2, 1000, 200, None, kept), names, now)
            # heartbeat: one call, read
            await _call(conn, agent, t3, _record("heartbeat", 1, 400, 100, "read", kept), names, now)
            # not counted: a call before the window, a call with no record, another agent's call
            await _call(conn, agent, old, _record("chat", 2, 1000, 200, "read", kept), names, now - timedelta(days=9))
            await _call(conn, agent, t2, None, names, now)
            await _call(conn, other, t1, _record("chat", 2, 1000, 200, "read", kept), names, now)
            # turn 1 then called send_email (left out) and bash (kept); turn 2 only bash; another agent's turn 2 call
            await _tool_call(conn, agent, t1, "send_email", now, "interactive", "s1")
            await _tool_call(conn, agent, t1, "bash", now, "interactive", "s1")
            await _tool_call(conn, agent, t2, "bash", now, "interactive", "s2")
            await _tool_call(conn, other, t2, "send_email", now, "interactive", "s2")
        RuntimeConfig.get().set_tool_loading(agent, ToolLoadingConfig(mode="watch", kinds=frozenset({"chat"})))
        body = (await _send(_app(db, _settings(agent)), "GET", PATH, headers=OWNER)).json()
        numbers = {row["kind"]: row for row in body["numbers"]}
        chat = numbers["chat"]
        assert (chat["on"], chat["calls"], chat["read_calls"], chat["written_calls"]) == (True, 4, 2, 1)
        assert (chat["deferred_avg"], chat["deferred_tokens_avg"], chat["index_tokens_avg"]) == (2.0, 1000, 200)
        # two read calls at 0.1 and one written at 1.25, of 800 net tokens each, over the three weighed calls
        assert chat["saved_per_call"] == round((2 * 800 * 0.1 + 800 * 1.25) / 3)
        assert (chat["turns"], chat["turns_used_left_out"], chat["calls_used_left_out"]) == (2, 1, 1)
        heartbeat = numbers["heartbeat"]
        assert (heartbeat["on"], heartbeat["calls"], heartbeat["saved_per_call"]) == (False, 1, 30)
        assert (heartbeat["turns_used_left_out"], heartbeat["calls_used_left_out"]) == (0, 0)
        assert numbers["subtask"]["calls"] == 0 and numbers["subtask"]["saved_per_call"] is None
        # a 30-day window takes the older call in
        wide = (await _send(_app(db, _settings(agent)), "GET", f"{PATH}?days=30", headers=OWNER)).json()
        assert {row["kind"]: row["calls"] for row in wide["numbers"]}["chat"] == 5
        # the tool table: calls and sessions in 30 days over every kind of call, this agent's only
        rows = {row["name"]: row for row in body["tools"]}
        assert (rows["bash"]["calls_30d"], rows["bash"]["sessions_30d"]) == (2, 2)
        assert (rows["send_email"]["calls_30d"], rows["send_email"]["sessions_30d"]) == (1, 1)
        assert rows["dag_create"]["calls_30d"] == 0
    finally:
        await _cleanup(db, agent, other)


@pytest.mark.postgres_only
async def test_without_the_tool_call_log_the_usage_numbers_are_null(db):
    agent = _agent()
    try:
        async with db.engine.begin() as conn:
            await _call(
                conn, agent, uuid.uuid4(), _record("chat", 2, 1000, 200, "read", ["bash"]), ["bash"], datetime.now(UTC)
            )
        body = (await _send(_app(db, _settings(agent, tool_call_log_enabled=False)), "GET", PATH, headers=OWNER)).json()
        chat = next(row for row in body["numbers"] if row["kind"] == "chat")
        assert chat["calls"] == 1 and chat["turns_used_left_out"] is None and chat["calls_used_left_out"] is None
        assert body["recorded"] == {"model_calls": True, "tool_calls": False}
        assert all(row["calls_30d"] is None for row in body["tools"])
    finally:
        await _cleanup(db, agent)


def test_a_kind_with_no_weighed_call_has_no_saving():
    row = {
        "calls": 2,
        "read_calls": 0,
        "written_calls": 0,
        "deferred": 4,
        "deferred_tokens": 10,
        "index_tokens": 2,
        "read_net": 0,
        "written_net": 0,
        "turns": 1,
    }
    assert kind_numbers(row, None)["saved_per_call"] is None


# ---- the change -----------------------------------------------------------------------------------------------------


@pytest.mark.postgres_only
async def test_a_change_is_merged_stored_under_this_agents_key_and_set_at_once(db):
    agent, other = _agent(), _agent()
    others = ToolLoadingConfig(mode="watch", kinds=frozenset({"chat"})).to_json()
    try:
        async with db.engine.begin() as conn:
            await conn.execute(
                text("INSERT INTO nous_system.config (key, value) VALUES (:k, CAST(:v AS jsonb))"),
                {"k": config_key(other), "v": json.dumps(others)},
            )
        app = _app(db, _settings(agent))
        first = await _send(app, "PUT", PATH, headers=PROXY, json={"mode": "watch"})
        assert first.status_code == 200, first.text
        assert first.json()["changed"] is True and first.json()["mode"] == "watch"
        second = await _send(app, "PUT", PATH, headers=OWNER, json={"kinds": {"chat": True, "heartbeat": True}})
        third = await _send(app, "PUT", PATH, headers=OWNER, json={"kinds": {"heartbeat": False}})
        assert second.status_code == third.status_code == 200
        live = RuntimeConfig.get().get_tool_loading(agent)
        assert (live.mode, live.kinds, live.core, live.updated_by) == ("watch", frozenset({"chat"}), None, "owner")
        stored = await _stored(db, agent)
        assert stored["mode"] == "watch" and stored["kinds"] == ["chat"] and stored["core"] is None
        assert stored["updated_by"] == "owner" and stored["updated_at"] == live.updated_at
        assert await _stored(db, other) == others  # another agent's key is untouched
        assert RuntimeConfig.get().get_tool_loading(other).mode == "off"  # and not read
    finally:
        await _cleanup(db, agent, other)


@pytest.mark.postgres_only
async def test_a_change_that_changes_nothing_is_not_written(db):
    agent = _agent()
    try:
        app = _app(db, _settings(agent))
        await _send(app, "PUT", PATH, headers=OWNER, json={"mode": "watch"})
        before = await _stored(db, agent)
        again = await _send(app, "PUT", PATH, headers=PROXY, json={"mode": "watch", "kinds": {"chat": False}})
        assert again.status_code == 200 and again.json()["changed"] is False
        assert await _stored(db, agent) == before
    finally:
        await _cleanup(db, agent)


@pytest.mark.postgres_only
async def test_the_core_set_adds_only_registered_names_keeps_stored_ones_and_cannot_drop_a_pinned_tool(db):
    agent = _agent()
    try:
        RuntimeConfig.get().set_tool_loading(agent, ToolLoadingConfig(core=("db_query", "mcp__gone__lookup")))
        app = _app(db, _settings(agent))
        refused = await _send(app, "PUT", PATH, headers=OWNER, json={"core": ["db_query", "no_such_tool", "x" * 60]})
        assert refused.status_code == 400
        assert refused.json() == {"error": f"Not registered now: no_such_tool, {'x' * 40}."}
        assert RuntimeConfig.get().get_tool_loading(agent).core == ("db_query", "mcp__gone__lookup")
        # a stored name that is not registered now is kept; a registered one is added; pinned names are not stored
        kept = await _send(
            app, "PUT", PATH, headers=OWNER, json={"core": ["mcp__gone__lookup", "send_email", "bash", "send_email"]}
        )
        assert kept.status_code == 200, kept.text
        assert RuntimeConfig.get().get_tool_loading(agent).core == ("mcp__gone__lookup", "send_email")
        body = (await _send(app, "GET", PATH, headers=OWNER)).json()
        rows = {row["name"]: row for row in body["tools"]}
        assert rows["mcp__gone__lookup"]["registered"] is False and rows["mcp__gone__lookup"]["state"] == "core"
        assert rows["send_email"]["state"] == "core" and rows["db_query"]["state"] == "left_out"
        # an empty core set leaves every pinned tool in full
        await _send(app, "PUT", PATH, headers=OWNER, json={"core": []})
        body = (await _send(app, "GET", PATH, headers=OWNER)).json()
        rows = {row["name"]: row for row in body["tools"]}
        assert rows["bash"]["state"] == rows["recall_deep"]["state"] == "pinned"
        assert body["core"] == [] and body["core_default"] is False
        assert {"bash", "recall_deep"} <= RuntimeConfig.get().get_tool_loading(agent).kept()
        # null is the default core set again
        await _send(app, "PUT", PATH, headers=OWNER, json={"core": None})
        assert RuntimeConfig.get().get_tool_loading(agent).core is None
    finally:
        await _cleanup(db, agent)


@pytest.mark.parametrize(
    ("body", "error"),
    [
        (b'{"mode": "on"}', "The mode must be off or watch."),
        (b'{"mode": 1}', "Not accepted: mode."),
        (b'{"kinds": {"goal_session": true}}', "Not accepted: a kind of call this server does not know."),
        (b'{"kinds": {"chat": "yes"}}', "Not accepted: kinds."),
        (b'{"core": ["has space"]}', "Not accepted: a core name that is not a tool name."),
        (b'{"core": "bash"}', "Not accepted: core."),
        (b'{"updated_by": "owner"}', "Not accepted: an unknown field."),
        (b"[1]", "The body must be a JSON object."),
        (b"not json", "The body must be JSON."),
        pytest.param(
            b'{"core": [' + b'"a",' * BODY_MAX_BYTES + b'"a"]}',
            f"The body must be at most {BODY_MAX_BYTES} bytes.",
            id="too-large",
        ),
    ],
)
async def test_a_bad_body_is_refused_with_its_sentence_and_nothing_is_stored(body, error):
    agent = _agent()
    response = await _send(_app(_NoDatabase(), _settings(agent)), "PUT", PATH, headers=OWNER, content=body)
    assert (response.status_code, response.json()) == (400, {"error": error})
    assert RuntimeConfig.get().get_tool_loading(agent).mode == "off"


async def test_a_failed_store_sets_nothing():
    class _Broken:
        def session(self):
            raise RuntimeError("database down")

    agent = _agent()
    response = await _send(_app(_Broken(), _settings(agent)), "PUT", PATH, headers=OWNER, json={"mode": "watch"})
    assert (response.status_code, response.json()) == (500, {"error": "The request could not be processed."})
    assert RuntimeConfig.get().get_tool_loading(agent).mode == "off"
