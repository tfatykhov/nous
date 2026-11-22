"""F114 goals: ``GET /goals/{ref}/activity``, what a goal's session is doing now, and the options key that turns the
panel on. Real Postgres; the rows are written as in ``tests/test_goals_activity.py``."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from goal_activity_support import RUNNER, SECRET, World, at, bash_input, intent_sid
from goal_routes_support import FORBIDDEN, PROXY, active, make_env, send
from test_goal_routes_guard import NoDatabase

from nous.goals import activity
from nous.goals.store import format_short_id

pytestmark = pytest.mark.postgres_only

FIXTURES = Path(__file__).resolve().parents[1] / "dashboard-app" / "src" / "lib" / "testing" / "goalFixtures.ts"
FAILED = {"error": "the request could not be processed"}


async def running_task(db, **over):
    """A goal with an open session whose chain runs a task: ``(env, goal, world, session)``."""
    env = make_env(db, goal_activity_enabled=True, **over)
    goal = await active(env.store)
    w = World(db, env.settings.agent_id, goal.id)
    s = await w.session(first_status="completed")
    await w.task(s.root_id, intent=f"Sort the photos {SECRET}", started=at(5))
    return env, goal, w, s


async def test_the_answer_has_every_key_in_the_order_the_dashboards_fixture_has(db):
    env, goal, w, s = await running_task(db, tool_call_log_enabled=True)
    await w.call(at(6), session_id=intent_sid(s.root_id))
    response = await env.call("GET", f"/goals/{format_short_id(goal.short_id)}/activity")
    assert response.status_code == 200
    body = response.json()
    keys = re.search(r"export const ACTIVITY_KEYS = \[(.*?)\];", FIXTURES.read_text(encoding="utf-8"), re.S)
    assert keys is not None and re.findall(r"'(\w+)'", keys.group(1)) == list(body)
    assert (body["enabled"], body["activity_enabled"], body["log_enabled"]) == (True, True, True)
    assert body["phase"] == "task" and body["current"]["name"].startswith("Sort the photos ")
    assert SECRET not in response.text
    assert body["calls"] == {"total": 1, "errors": 0, "capped": False}


async def test_no_stored_text_of_a_call_leaves_through_the_route(db):
    env, goal, w, s = await running_task(db, tool_call_log_enabled=True)
    await w.call(
        at(6),
        session_id=intent_sid(s.root_id),
        input_text=bash_input(f"{RUNNER} launch nous --prompt-file /workspace/notes/INPUT-MARK.md"),
        reason="REASON-MARK",
        result_preview="PREVIEW-MARK",
        result_text="RESULT-MARK",
        error_text="ERROR-MARK",
    )
    response = await env.call("GET", f"/goals/{goal.id}/activity")
    assert response.status_code == 200 and response.json()["jobs"]["launched"] == 1
    for mark in ("INPUT-MARK", "REASON-MARK", "PREVIEW-MARK", "RESULT-MARK", "ERROR-MARK", "prompt-file"):
        assert mark not in response.text


async def test_with_the_call_log_off_the_answer_says_so(db):
    env, goal, _w, _s = await running_task(db)
    body = (await env.call("GET", f"/goals/{goal.id}/activity")).json()
    assert body["log_enabled"] is False and body["calls"] is None and body["phase"] == "task"


async def test_an_unknown_goal_is_not_found(db):
    env = make_env(db, goal_activity_enabled=True)
    response = await env.call("GET", "/goals/G-0999/activity")
    assert (response.status_code, response.json()) == (404, {"error": "no goal G-0999"})


async def test_with_the_panel_off_the_answer_says_so_and_no_table_is_read():
    database = NoDatabase()
    env = make_env(database)
    response = await env.call("GET", "/goals/G-0001/activity")
    assert (response.status_code, response.json()) == (200, {"enabled": True, "activity_enabled": False})
    assert database.opened == 0


async def test_with_goals_off_the_answer_is_enabled_false():
    database = NoDatabase()
    env = make_env(database, enabled=False, goal_activity_enabled=True)
    response = await env.call("GET", "/goals/G-0001/activity")
    assert (response.status_code, response.json()) == (200, {"enabled": False})
    assert database.opened == 0


async def test_a_read_that_fails_is_a_500_naming_nothing(db, monkeypatch):
    env, goal, _w, _s = await running_task(db)

    async def broken(*_args, **_kwargs):
        raise RuntimeError("the database said something private")

    monkeypatch.setattr(activity, "read_activity", broken)
    response = await env.call("GET", f"/goals/{goal.id}/activity")
    assert (response.status_code, response.json()) == (500, FAILED)
    assert "private" not in response.text


async def test_the_proxys_secret_is_let_in_and_no_secret_is_refused(db):
    env, goal, _w, _s = await running_task(db)
    assert (await send(env.app, "GET", f"/goals/{goal.id}/activity", headers=PROXY)).status_code == 200
    refused = await send(env.app, "GET", f"/goals/{goal.id}/activity")
    assert (refused.status_code, refused.json()) == (403, FORBIDDEN)


async def test_the_options_say_activity_only_with_the_panel_on_and_nothing_else_changes(db):
    off = (await make_env(db).call("GET", "/goals/options")).json()
    on = (await make_env(db, goal_activity_enabled=True).call("GET", "/goals/options")).json()
    assert "activity" not in off and on["activity"] is True
    assert {k: v for k, v in on.items() if k != "activity"} == off
    assert json.dumps(list(on)) == json.dumps([*off, "activity"])
