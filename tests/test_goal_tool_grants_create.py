"""Per-goal tool grants at a goal's creation: ``POST /goals`` takes the tools the new goal may use without asking,
checks each as ``PUT /goals/{ref}/tools/{tool}`` does, and writes the goal and its grants in one transaction (one
refused tool: nothing is created). ``GET /goals/options`` says what a new goal may be allowed. With the grants off a
``tools`` field is refused; without one the create is as before."""

from __future__ import annotations

import re
import uuid
from pathlib import Path

import pytest
from goal_routes_support import CHAN, OWNER, PROXY
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from test_goal_tool_grants_routes import MAIL, NOTES, OFF, ON, Env, mcp_registry  # noqa: F401

from nous.api.goal_routes import GRANTS_OFF
from nous.api.tool_classes import McpIdentity
from nous.autonomy import tool_guard
from nous.autonomy.tool_guard import ToolGuard
from nous.goals.grants import CREATE_CONFIRM
from nous.goals.store import GoalRefused
from nous.storage.models import Goal, GoalJournal, GoalToolGrant

pytestmark = pytest.mark.postgres_only

ROOT = Path(__file__).resolve().parents[1]
NEW = {"title": "Send the notes", "objective": "Mail the weekly summary", "done_when": "Sent", "origin_channel": CHAN}


@pytest.fixture
def env(db, mcp_registry):  # noqa: F811
    tool_guard.install(ToolGuard())
    return Env(db)


async def _goals(env) -> list[Goal]:
    async with env.db.session() as s:
        return list((await s.execute(select(Goal).where(Goal.agent_id == env.store.agent_id))).scalars())


async def _grant_rows(env) -> list[GoalToolGrant]:
    async with env.db.session() as s:
        query = select(GoalToolGrant).where(GoalToolGrant.agent_id == env.store.agent_id)
        return list((await s.execute(query.order_by(GoalToolGrant.tool))).scalars())


async def _journal(env, goal_id) -> list[GoalJournal]:
    async with env.db.session() as s:
        query = select(GoalJournal).where(GoalJournal.goal_id == uuid.UUID(goal_id)).order_by(GoalJournal.seq)
        return list((await s.execute(query)).scalars())


async def test_a_goal_created_with_two_tools_has_both_grants_and_their_journal_rows(env):
    tools = [{"tool": "send_email", "confirm": True}, {"tool": ON, "confirm": True, "server_id": str(NOTES.server_id)}]
    response = await env.call("POST", "/goals", json={**NEW, "tools": tools}, headers=PROXY)
    assert response.status_code == 201, response.json()
    goal = response.json()
    assert goal["state"] == "draft"
    rows = await _grant_rows(env)
    assert [(row.tool, row.granted_by, row.revoked_at) for row in rows] == [
        (ON, "proxy", None),
        ("send_email", "proxy", None),
    ]
    assert (rows[0].server_id, rows[0].url_sha256, rows[0].remote_tool) == (NOTES.server_id, NOTES.url_sha256, "search")
    assert [(row.actor, row.text) for row in await _journal(env, goal["id"])] == [
        ("owner", "Created as a draft (small limits)."),
        ("owner", "Allowed to use send_email without asking, from now on."),
        ("owner", f"Allowed to use {ON} (server notes) without asking, from now on."),
    ]
    read = (await env.call("GET", f"/goals/{goal['id']}/tools")).json()
    assert {row["tool"] for row in read["grants"]} == {"send_email", ON}


@pytest.mark.parametrize(
    ("second", "status", "sentence"),
    [
        (
            {"tool": "send_file", "confirm": True},
            409,
            "send_file has no Telegram chat set, so it could send to any chat a call names. Set the chat first.",
        ),
        ({"tool": "bash", "confirm": True}, 400, "bash can never be given to a goal."),
        ({"tool": "browser_task"}, 400, "Allowing a tool needs your confirmation."),
        ({"tool": "send_email", "confirm": True}, 400, "A tool is listed twice."),
        (
            {"tool": OFF, "confirm": True, "server_id": str(MAIL.server_id)},
            409,
            "mail is not available to background work. Turn that on for the server on the Integrations screen first.",
        ),
        (
            {"tool": ON, "confirm": True, "server_id": str(uuid.uuid4())},
            409,
            "That server changed since you looked. Look again.",
        ),
        ({"tool": ON, "confirm": True}, 400, "Not accepted: tools.server_id."),
        ({"tool": "mcp__gone__x", "confirm": True}, 400, "mcp__gone__x is not a tool Nous has now."),
    ],
)
async def test_one_refused_tool_creates_nothing_and_names_the_tool(db, mcp_registry, second, status, sentence):  # noqa: F811
    tool_guard.install(ToolGuard())
    env = Env(db, telegram_chat_id="")
    tools = [{"tool": "send_email", "confirm": True}, second]
    response = await env.call("POST", "/goals", json={**NEW, "tools": tools})
    assert (response.status_code, response.json()) == (status, {"error": sentence, "tool": second["tool"]})
    assert await _goals(env) == [] and await _grant_rows(env) == []


async def test_more_than_twenty_tools_creates_nothing(env):
    tools = [{"tool": f"mcp__s{n}__t", "confirm": True} for n in range(21)]
    response = await env.call("POST", "/goals", json={**NEW, "tools": tools})
    assert (response.status_code, response.json()) == (
        409,
        {"error": "A goal can have at most 20 tools. Remove one first."},
    )
    assert await _goals(env) == []


async def test_a_tools_item_with_an_unknown_field_is_not_accepted(env):
    response = await env.call(
        "POST", "/goals", json={**NEW, "tools": [{"tool": "send_email", "confirm": True, "x": 1}]}
    )
    assert response.status_code == 400 and "tools.0.x" in response.json()["error"]
    assert await _goals(env) == []


@pytest.mark.parametrize("tools", [[{"tool": "send_email", "confirm": True}], []])
async def test_with_the_grants_off_a_tools_field_is_refused_and_nothing_is_created(db, mcp_registry, tools):  # noqa: F811
    tool_guard.install(ToolGuard())
    env = Env(db, goal_tool_grants_enabled=False)
    response = await env.call("POST", "/goals", json={**NEW, "tools": tools})
    assert (response.status_code, response.json()) == (503, GRANTS_OFF)
    assert await _goals(env) == []


async def test_without_tools_the_create_is_as_before(db, mcp_registry):  # noqa: F811
    tool_guard.install(ToolGuard())
    answers = []
    for on in (True, False):
        env = Env(db) if on else Env(db, grants=False, goal_tool_grants_enabled=False)
        response = await env.call("POST", "/goals", json=NEW, headers=OWNER)
        assert response.status_code == 201
        body = response.json()
        journal = [(row.kind, row.actor, row.text) for row in await _journal(env, body["id"])]
        answers.append((sorted(body), journal, await _grant_rows(env)))
    assert answers[0] == answers[1]
    assert answers[0][2] == []


async def test_the_options_say_what_a_new_goal_may_be_allowed_only_with_the_grants_on(db, mcp_registry):  # noqa: F811
    tool_guard.install(ToolGuard(denied=frozenset({"browser_task"})))
    on = (await Env(db).call("GET", "/goals/options")).json()
    tools = on["tools"]
    assert (tools["limit"], tools["confirm"]) == (20, CREATE_CONFIRM)
    rows = {row["tool"]: row for row in tools["available"]}
    assert set(rows) == {"send_email", "send_file", "browser_task", ON, OFF}
    assert rows["send_email"]["confirm"].startswith("Allow this goal to send email")
    assert rows["browser_task"]["blocked"]["code"] == "switched_off"
    off = (await Env(db, goal_tool_grants_enabled=False).call("GET", "/goals/options")).json()
    assert "tools" not in off
    assert set(on) - {"tools"} == set(off)


async def test_the_store_writes_a_goal_and_its_grants_together(env):
    bad = McpIdentity(uuid.uuid4(), "not-a-digest", "t", server_name="s", server_host="s.example.com")
    with pytest.raises(IntegrityError):
        await env.store.create(
            **{key: value for key, value in NEW.items()},
            proposed_by="owner",
            tools=[("send_email", None), ("mcp__s__t", bad)],
            proof="owner",
        )
    assert await _goals(env) == [] and await _grant_rows(env) == []


async def test_only_the_owner_creates_a_goal_with_tools(env):
    with pytest.raises(GoalRefused, match="only the owner"):
        await env.store.create(**NEW, proposed_by="agent", tools=[("send_email", None)], proof="owner")
    assert await _goals(env) == []


def test_the_rest_reference_says_a_goal_can_be_created_with_its_tools():
    doc = (ROOT / "docs" / "reference" / "rest-api.md").read_text(encoding="utf-8")
    [row] = [line for line in doc.splitlines() if line.startswith("| POST | `/goals` |")]
    assert "`tools`" in row
    assert re.search(r"one transaction|nothing is created", row)
