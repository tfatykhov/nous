"""F115: the autonomy routes (real Postgres for the store): the answers with the flags off, the owner's proof as the
actor, the sentences of a refused change, changes at once, and a change seen by the next use."""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import delete, select
from starlette.applications import Starlette

from nous.api import tool_classes
from nous.api.autonomy_routes import build_autonomy_routes, summary, tool_group
from nous.api.execution_context import ExecutionContext
from nous.api.owner_guard import OwnerGuard
from nous.api.tools import ToolDispatcher
from nous.autonomy import store as store_module
from nous.autonomy import tool_guard
from nous.autonomy.registry import by_key
from nous.autonomy.store import (
    ASK_NOT_AVAILABLE,
    GUARD_NOT_AVAILABLE,
    SCOPE_NOT_AVAILABLE,
    AutonomyStore,
)
from nous.config import Settings
from nous.storage.models import AutonomySetting, AutonomySettingLog, ToolCallLog, ToolGuardRow

OWNER_VALUE = "owner-only-test-value"
PROXY_VALUE = "proxy-only-test-value"
OWNER = {"X-Nous-Owner-Secret": OWNER_VALUE}
PROXY = {"X-Nous-Proxy-Secret": PROXY_VALUE, "content-type": "application/json"}
MCP = "mcp__notes__search"
TOOLS = {
    "web_search": "Search the web.\nMore lines.",
    "read_file": "Read a file.",
    "spawn_task": "Start a background task.",
    "bash": "Run a shell command.",
    "send_email": "Send an email.",
    "store_identity": "Initiation only.",
    "submit_final_report": "Hand in the report.",  # the harness's own: never listed
    MCP: "Search the notes server.",
}


def _settings(agent_id: str, **over) -> Settings:
    values = {
        "owner_route_secret_sha256": hashlib.sha256(OWNER_VALUE.encode()).hexdigest(),
        "mcp_client_ui_secret_sha256": hashlib.sha256(PROXY_VALUE.encode()).hexdigest(),
        "autonomy_settings_enabled": True,
        "tool_guard_enabled": True,
        "ANTHROPIC_API_KEY": "test-key",
        **over,
    }
    return Settings(_env_file=None, agent_id=agent_id, **values)


def _dispatcher() -> ToolDispatcher:
    dispatcher = ToolDispatcher()
    for name, description in TOOLS.items():
        dispatcher.register(name, MagicMock(), {"type": "object", "description": description, "properties": {}})
    return dispatcher


def _app(database, settings, store, dispatcher=None) -> Starlette:
    routes = build_autonomy_routes(
        database=database,
        settings=settings,
        store=store,
        dispatcher=dispatcher if dispatcher is not None else _dispatcher(),
        guard=OwnerGuard(settings),
    )
    return Starlette(routes=routes)


async def _send(app, method, path, **kwargs) -> httpx.Response:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://nous") as client:
        return await client.request(method, path, **kwargs)


# ---- the flags off (no database) ----------------------------------------------------------------------------------


class _NoDatabase:
    def session(self):
        raise AssertionError("a route opened a session")


READS = ["/autonomy/limits", "/autonomy/log", "/autonomy/tools"]
WRITES = [
    ("PUT", "/autonomy/limits/max_turns", {"value": 20}),
    ("DELETE", "/autonomy/limits/max_turns", None),
    ("PUT", "/autonomy/tools/web_search", {"setting": "denied"}),
    ("DELETE", "/autonomy/tools/web_search", None),
]


async def test_with_the_settings_off_the_reads_say_so_and_the_changes_are_503():
    settings = _settings("off", autonomy_settings_enabled=False, tool_guard_enabled=False)
    app = _app(_NoDatabase(), settings, None)
    for path in READS:
        response = await _send(app, "GET", path, headers=OWNER)
        assert (response.status_code, response.json()) == (200, {"enabled": False}), path
    for method, path, body in WRITES:
        response = await _send(app, method, path, headers=OWNER, json=body)
        assert response.status_code == 503, path
        assert response.json() == {"error": "Autonomy settings are off on this server.", "enabled": False}


async def test_a_store_with_the_flag_off_is_not_used():
    """A store handed over while the setting is off is never read (main.py builds none: belt and braces)."""
    settings = _settings("off", autonomy_settings_enabled=False, tool_guard_enabled=False)
    store = AutonomyStore(_NoDatabase(), _settings("off"))
    response = await _send(_app(_NoDatabase(), settings, store), "GET", "/autonomy/limits", headers=OWNER)
    assert response.json() == {"enabled": False}


async def test_with_the_tool_guard_off_the_tool_routes_say_so():
    settings = _settings("guard-off", tool_guard_enabled=False)
    app = _app(_NoDatabase(), settings, AutonomyStore(_NoDatabase(), settings))
    response = await _send(app, "GET", "/autonomy/tools", headers=OWNER)
    assert response.json() == {"enabled": False}
    for method, path, body in WRITES[2:]:
        response = await _send(app, method, path, headers=OWNER, json=body)
        assert (response.status_code, response.json()) == (
            503,
            {"error": "The tool guard is off on this server.", "enabled": False},
        )


@pytest.mark.parametrize(("method", "path", "body"), [("GET", path, None) for path in READS] + WRITES)
async def test_without_a_secret_every_route_is_403_before_anything_is_read(method, path, body):
    app = _app(None, _settings("guard"), None, dispatcher=None)
    response = await _send(app, method, path, content=b"not json")
    assert (response.status_code, response.json()) == (403, {"error": "forbidden"})


@pytest.mark.parametrize(("method", "path", "body"), WRITES)
async def test_a_proxy_change_that_is_not_json_is_415(method, path, body):
    app = _app(None, _settings("guard"), None)
    response = await _send(app, method, path, content=b"{}", headers={"X-Nous-Proxy-Secret": PROXY_VALUE})
    assert response.status_code == 415


def test_the_groups_and_the_summary():
    assert [tool_group(name) for name in ("web_search", "read_file", "spawn_task", "send_email", "bash", MCP)] == [
        "reading",
        "reading",
        "starting_work",
        "reaching_outside",
        "writing",
        "other_servers",
    ]
    assert summary("First line.\nSecond.") == "First line."
    assert summary("") == "" and len(summary("x" * 500)) == 160


# ---- with the store (real Postgres) -------------------------------------------------------------------------------


@pytest.fixture
def agent_id() -> str:
    return f"test-autonomy-routes-{uuid.uuid4().hex[:12]}"


@pytest_asyncio.fixture
async def rig(db, agent_id, monkeypatch):
    monkeypatch.setattr(tool_classes, "_MCP_TOOLS", {MCP: True})
    built = []

    async def build(**over):
        settings = _settings(agent_id, **over)
        store = AutonomyStore(db, settings)
        await store.load()
        built.append(store)
        return SimpleNamespace(settings=settings, store=store, app=_app(db, settings, store))

    yield build
    async with db.session() as session:
        for model in (AutonomySetting, AutonomySettingLog, ToolGuardRow, ToolCallLog):
            await session.execute(delete(model).where(model.agent_id == agent_id))
        await session.commit()


async def _log(db, agent_id) -> list[tuple]:
    async with db.session() as session:
        rows = (
            await session.execute(
                select(AutonomySettingLog)
                .where(AutonomySettingLog.agent_id == agent_id)
                .order_by(AutonomySettingLog.id)
            )
        ).scalars()
        return [(row.key, row.old_value, row.new_value, row.actor) for row in rows]


@pytest.mark.postgres_only
async def test_the_limits_list_each_limit_with_its_host_value_and_source(rig):
    r = await rig(subtask_tool_call_limit=50, autonomy_ceilings="subtask_tool_call_limit=40")
    body = (await _send(r.app, "GET", "/autonomy/limits", headers=OWNER)).json()
    assert body["enabled"] is True and body["loaded"] is True
    assert body["tool_guard"] is True and body["ceilings_set"] is True
    assert len(body["limits"]) == 11
    row = next(row for row in body["limits"] if row["key"] == "subtask_tool_call_limit")
    assert row == {
        "key": "subtask_tool_call_limit",
        "label": "Tool calls per background task",
        "unit": "tool calls",
        "low": 1,
        "high": 200,
        "value": 40,
        "host_value": 50,
        "baseline": 40,
        "source": "clamped",
        "ceiling": 40,
        "description": by_key("subtask_tool_call_limit").description,
        "applies": "From the next task that starts.",
        "last_change": None,
    }


@pytest.mark.postgres_only
@pytest.mark.parametrize(("headers", "actor"), [(OWNER, "owner"), (PROXY, "proxy")], ids=["owner", "proxy"])
async def test_a_change_names_the_proof_as_its_actor(rig, db, agent_id, headers, actor):
    r = await rig()
    response = await _send(r.app, "PUT", "/autonomy/limits/max_turns", headers=headers, json={"value": 40})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["changed"] is True and body["limit"]["value"] == 40 and body["limit"]["source"] == "owner"
    assert body["limit"]["last_change"]["actor"] == actor
    assert r.settings.max_turns == 40
    response = await _send(r.app, "DELETE", "/autonomy/limits/max_turns", headers=headers)
    assert response.json()["changed"] is True and r.settings.max_turns == 10
    assert await _log(db, agent_id) == [("max_turns", None, 40, actor), ("max_turns", 40, None, actor)]
    changes = (await _send(r.app, "GET", "/autonomy/log", headers=headers)).json()["changes"]
    assert [(c["key"], c["new"], c["actor"], c["label"]) for c in changes] == [
        ("max_turns", None, actor, "Rounds of tool use per turn"),
        ("max_turns", 40, actor, "Rounds of tool use per turn"),
    ]


@pytest.mark.postgres_only
@pytest.mark.parametrize(
    ("path", "body", "status", "words"),
    [
        ("/autonomy/limits/max_turns", {"value": 4}, 400, "a whole number from 5 to 1,000"),
        ("/autonomy/limits/max_turns", {"value": "40"}, 400, "a whole number from 5 to 1,000"),
        ("/autonomy/limits/max_turns", {"value": 40.5}, 400, "a whole number"),
        ("/autonomy/limits/max_turns", {"value": 40, "actor": "agent"}, 400, "actor"),
        ("/autonomy/limits/max_turns", {}, 400, "value"),
        ("/autonomy/limits/max_turns", [40], 400, "JSON object"),
        ("/autonomy/limits/goals_queue_headroom", {"value": 1}, 404, "no limit"),
        ("/autonomy/limits/subtask_tool_call_limit", {"value": 41}, 409, "at most 40"),
        ("/autonomy/limits/subtask_default_timeout", {"value": 4000}, 409, "cannot be above"),
        ("/autonomy/tools/web_search", {"setting": "ask"}, 400, ASK_NOT_AVAILABLE),
        ("/autonomy/tools/web_search", {"setting": "denied", "scope": "goal_session"}, 400, SCOPE_NOT_AVAILABLE),
        ("/autonomy/tools/web_search", {"setting": "denied", "scope": 7}, 400, SCOPE_NOT_AVAILABLE),
        ("/autonomy/tools/web_search", {"setting": "denied", "destructive_guard": "off"}, 400, GUARD_NOT_AVAILABLE),
        ("/autonomy/tools/web_search", {"setting": "denied", "actor": "agent"}, 400, "actor"),
        ("/autonomy/tools/nope", {"setting": "denied"}, 400, "no tool named"),
        ("/autonomy/tools/resolve_intention", {"setting": "denied"}, 400, "no tool named"),
        ("/autonomy/tools/bash", {"setting": "allowed"}, 409, "locked by the server"),
    ],
)
async def test_a_refused_change_says_why_and_changes_nothing(rig, db, agent_id, path, body, status, words):
    r = await rig(autonomy_ceilings="subtask_tool_call_limit=40", autonomy_locked_tools="bash")
    before = r.settings.model_dump()
    response = await _send(r.app, "PUT", path, headers=OWNER, json=body)
    assert response.status_code == status, response.text
    assert words in response.json()["error"]
    assert r.settings.model_dump() == before and await _log(db, agent_id) == []
    assert tool_guard.current().denied == frozenset()


@pytest.mark.postgres_only
async def test_returning_a_tool_in_another_scope_is_not_available_yet(rig, db, agent_id):
    r = await rig()
    for body in ({"scope": "goal_session"}, {"scope": None}):
        response = await _send(r.app, "DELETE", "/autonomy/tools/web_search", headers=OWNER, json=body)
        if body["scope"] is None:
            assert (response.status_code, response.json()["changed"]) == (200, False)
        else:
            assert (response.status_code, response.json()) == (400, {"error": SCOPE_NOT_AVAILABLE})
    assert await _log(db, agent_id) == []


@pytest.mark.postgres_only
async def test_changes_at_once_all_land(rig, db, agent_id):
    r = await rig()
    responses = await asyncio.gather(
        _send(r.app, "PUT", "/autonomy/limits/max_turns", headers=OWNER, json={"value": 100}),
        _send(r.app, "PUT", "/autonomy/limits/max_turns", headers=PROXY, json={"value": 200}),
        _send(r.app, "PUT", "/autonomy/limits/continuation_max_depth", headers=OWNER, json={"value": 4}),
        _send(r.app, "PUT", "/autonomy/tools/web_search", headers=OWNER, json={"setting": "denied"}),
    )
    assert [response.status_code for response in responses] == [200, 200, 200, 200]
    log = await _log(db, agent_id)
    turns = [row for row in log if row[0] == "max_turns"]
    assert len(turns) == 2 and turns[0][1] is None and turns[1][1] == turns[0][2]
    assert r.settings.max_turns == turns[1][2] and r.settings.continuation_max_depth == 4
    assert tool_guard.current().denied == frozenset({"web_search"})


@pytest.mark.postgres_only
async def test_a_tool_switched_off_is_refused_on_the_very_next_call(rig, env_factory_runner):
    r = await rig()
    runner = env_factory_runner(r.settings)
    ctx = ExecutionContext(kind="scheduled", session_id="schedule-1")
    assert runner._authorize_tool_call(ctx, "web_search", frozenset({"web_search"}), "schedule-1", {}) is None
    response = await _send(r.app, "PUT", "/autonomy/tools/web_search", headers=PROXY, json={"setting": "denied"})
    assert response.json()["tool"]["setting"] == "denied" and response.json()["tool"]["source"] == "owner"
    refusal = runner._authorize_tool_call(ctx, "web_search", frozenset({"web_search"}), "schedule-1", {})
    assert refusal is not None and refusal.code == "tool_guard"
    response = await _send(r.app, "DELETE", "/autonomy/tools/web_search", headers=PROXY)
    assert response.json()["changed"] is True and response.json()["tool"]["source"] == "default"
    assert runner._authorize_tool_call(ctx, "web_search", frozenset({"web_search"}), "schedule-1", {}) is None


@pytest.fixture
def env_factory_runner():
    from nous.api.runner import AgentRunner

    def build(settings):
        runner = AgentRunner(MagicMock(), None, MagicMock(), settings)
        runner.set_dispatcher(_dispatcher())
        return runner

    return build


@pytest.mark.postgres_only
async def test_the_tools_list(rig, db, agent_id):
    r = await rig(autonomy_locked_tools="bash")
    await r.store.set_tool("web_search", "denied", "owner")
    body = (await _send(r.app, "GET", "/autonomy/tools", headers=OWNER)).json()
    assert body["enabled"] is True and body["recorded"] is False and body["locks_set"] is True
    assert [place["key"] for place in body["places"]][:3] == ["chain_decisions", "chain_work", "tasks"]
    rows = {row["name"]: row for row in body["tools"]}
    assert "store_identity" not in rows  # an initiation tool is never offered to background work
    assert set(rows) == set(TOOLS) - {"store_identity", "submit_final_report"}
    assert rows["web_search"] | {"not_offered": None} == {
        "name": "web_search",
        "summary": "Search the web.",
        "registered": True,
        "group": "reading",
        "classes": ["Reads only", "Brings in outside text"],
        "setting": "denied",
        "source": "owner",
        "locked": False,
        "not_offered": None,
        "calls_30d": None,
        "last_call": None,
    }
    assert (rows["bash"]["setting"], rows["bash"]["source"], rows["bash"]["locked"]) == ("denied", "server", True)
    assert (rows["read_file"]["setting"], rows["read_file"]["source"]) == ("allowed", "default")
    places = {item["place"]: item["why"] for item in rows["spawn_task"]["not_offered"]}
    assert set(places) >= {"tasks", "workflow_steps", "schedules", "checks", "triage"}
    assert places["tasks"].startswith("Work started in the background")
    assert {item["place"] for item in rows["send_email"]["not_offered"]} == {"chain_decisions", "chain_work"}


@pytest.mark.postgres_only
async def test_the_tools_list_counts_background_calls_when_the_log_is_on(rig, db, agent_id):
    r = await rig(tool_call_log_enabled=True)
    now = datetime.now(UTC)
    async with db.session() as session:
        for name, kind, when in [
            ("web_search", "scheduled", now - timedelta(days=1)),
            ("web_search", "subtask", now - timedelta(hours=1)),
            ("web_search", "interactive", now),  # chat: not a background call
            ("web_search", "scheduled", now - timedelta(days=40)),  # too old
            ("read_file", "dag_node", now - timedelta(days=2)),
        ]:
            session.add(
                ToolCallLog(
                    agent_id=agent_id,
                    call_index=0,
                    work_kind="background",
                    context_kind=kind,
                    tool_name=name,
                    tool_name_called=name,
                    outcome="success",
                    started_at=when,
                )
            )
        await session.commit()
    body = (await _send(r.app, "GET", "/autonomy/tools", headers=OWNER)).json()
    rows = {row["name"]: row for row in body["tools"]}
    assert body["recorded"] is True
    assert rows["web_search"]["calls_30d"] == 2
    assert rows["web_search"]["last_call"].startswith((now - timedelta(hours=1)).isoformat()[:13])
    assert (rows["read_file"]["calls_30d"], rows["bash"]["calls_30d"], rows["bash"]["last_call"]) == (1, 0, None)


class _BrokenDatabase:
    @asynccontextmanager
    async def session(self):
        raise ConnectionError("the database is down")
        yield


@pytest.mark.postgres_only
async def test_while_the_saved_settings_cannot_be_read_the_reads_say_so_and_the_changes_are_503(agent_id, monkeypatch):
    monkeypatch.setattr(store_module, "RETRY_FIRST_SECONDS", 3600.0)
    settings = _settings(agent_id)
    store = AutonomyStore(_BrokenDatabase(), settings)
    await store.load()
    app = _app(_BrokenDatabase(), settings, store)
    try:
        message = (
            "The saved settings could not be read; background work is held from tools until they can. Every limit is "
            "the server's own value until then."
        )
        for path in ("/autonomy/limits", "/autonomy/tools"):
            response = await _send(app, "GET", path, headers=OWNER)
            assert (response.status_code, response.json()) == (
                200,
                {"enabled": True, "loaded": False, "message": message},
            ), path
        for method, path, body in WRITES:
            response = await _send(app, method, path, headers=OWNER, json=body)
            assert (response.status_code, response.json()) == (503, {"error": store_module.NOT_LOADED_WRITE}), path
    finally:
        await store.close()


@pytest.mark.postgres_only
async def test_the_record_can_be_read_by_kind_and_length(rig):
    r = await rig()
    for n in range(3):
        await r.store.set_limit("max_turns", 20 + n, "owner")
    await r.store.set_tool("web_search", "denied", "proxy")
    read = lambda query: _send(r.app, "GET", f"/autonomy/log{query}", headers=OWNER)  # noqa: E731
    assert [c["new"] for c in (await read("")).json()["changes"]] == ["denied", 22, 21, 20]
    assert [c["new"] for c in (await read("?kind=limits&limit=2")).json()["changes"]] == [22, 21]
    assert [c["tool"] for c in (await read("?kind=tools")).json()["changes"]] == ["web_search"]
    for query, words in [
        ("?kind=other", "The kind must be all, limits or tools."),
        ("?limit=0", "The limit must be a whole number from 1 to 100."),
        ("?limit=101", "The limit must be a whole number from 1 to 100."),
        ("?limit=x", "The limit must be a whole number from 1 to 100."),
    ]:
        response = await read(query)
        assert (response.status_code, response.json()) == (400, {"error": words}), query


@pytest.mark.postgres_only
@pytest.mark.parametrize("body", [{"actor": "agent"}, {"value": 5}, [1]], ids=["actor", "unknown", "not-an-object"])
async def test_returning_a_limit_takes_no_field(rig, db, agent_id, body):
    r = await rig()
    await r.store.set_limit("max_turns", 40, "owner")
    response = await _send(r.app, "DELETE", "/autonomy/limits/max_turns", headers=OWNER, json=body)
    assert response.status_code == 400
    assert r.settings.max_turns == 40 and len(await _log(db, agent_id)) == 1


@pytest.mark.postgres_only
async def test_saving_what_is_already_saved_answers_unchanged_and_records_nothing(rig, db, agent_id):
    r = await rig()
    first = await _send(r.app, "PUT", "/autonomy/limits/max_turns", headers=OWNER, json={"value": 40})
    again = await _send(r.app, "PUT", "/autonomy/limits/max_turns", headers=PROXY, json={"value": 40})
    assert (first.json()["changed"], again.json()["changed"]) == (True, False)
    assert again.json()["limit"]["last_change"]["actor"] == "owner"
    tool = await _send(r.app, "PUT", "/autonomy/tools/web_search", headers=OWNER, json={"setting": "denied"})
    tool_again = await _send(r.app, "PUT", "/autonomy/tools/web_search", headers=OWNER, json={"setting": "denied"})
    assert (tool.json()["changed"], tool_again.json()["changed"]) == (True, False)
    assert len(await _log(db, agent_id)) == 2


@pytest.mark.postgres_only
async def test_the_tools_list_names_where_not_allowed_applies(rig):
    r = await rig()
    body = (await _send(r.app, "GET", "/autonomy/tools", headers=OWNER)).json()
    assert body["loaded"] is True and body["held_in"] == tool_guard.held_places()
    assert {row["name"] for row in body["tools"]} == set(TOOLS) - {"store_identity", "submit_final_report"}
    assert "chain decisions" in body["held_in"] and not any("companion" in place for place in body["held_in"])
