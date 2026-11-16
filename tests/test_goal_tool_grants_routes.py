"""Per-goal tool grants over REST: ``GET /goals/{ref}/tools``, ``PUT`` and ``DELETE /goals/{ref}/tools/{tool}``,
behind the owner guard, with the real store, the real tool guard and a stand-in MCP client registry: every refusal
with its sentence, the flags off, strict bodies, idempotent changes, the proof stored with a grant, the answer's
words, and the runner's view read again after a change and after a goal action that can change whether the goal is
active."""

from __future__ import annotations

import uuid
from types import MappingProxyType
from typing import Any
from unittest.mock import MagicMock

import pytest
from goal_routes_support import CHAN, FORBIDDEN, OWNER, PROXY, active, draft, send, settings_for
from sqlalchemy import select
from starlette.applications import Starlette

from nous.api import tool_classes
from nous.api.execution_context import ExecutionContext
from nous.api.goal_routes import GOALS_OFF, GRANTS_OFF, SERVER_CHANGED_SENTENCE, build_goal_routes
from nous.api.owner_guard import OwnerGuard
from nous.api.tool_classes import TOOL_CLASSES, McpIdentity, ToolClass
from nous.api.tools import ToolDispatcher
from nous.autonomy import tool_guard
from nous.autonomy.tool_guard import ToolGuard
from nous.goals.grants import NEVER_SENTENCE, GoalToolGrants, decide, identity_token, lead_sentence
from nous.goals.store import GoalStore
from nous.storage.models import GoalJournal, GoalSession, GoalToolGrant

pytestmark = pytest.mark.postgres_only

ON = "mcp__notes__search"
OFF = "mcp__mail__send"
NOTES = McpIdentity(uuid.uuid4(), "a1" * 32, "search", server_name="notes", server_host="notes.example.com")
MAIL = McpIdentity(uuid.uuid4(), "b2" * 32, "send", server_name="mail", server_host="mail.example.com")
FLAGS = {
    "goals_enabled": True,
    "continuation_enabled": True,
    "intentions_enabled": True,
    "result_inbox_enabled": True,
    "subtask_enabled": True,
    "autonomy_settings_enabled": True,
    "tool_guard_enabled": True,
    "goal_tool_grants_enabled": True,
    "email_allowlist": "friend@example.com",
    "telegram_chat_id": "4242",
}
REGISTERED = ("send_email", "send_file", "browser_task", "web_search", "bash", "resolve_intention", ON, OFF)


@pytest.fixture
def mcp_registry(monkeypatch):
    monkeypatch.setattr(tool_classes, "_MCP_TOOLS", {ON: True, OFF: False})
    monkeypatch.setattr(tool_classes, "_MCP_IDENTITIES", {ON: NOTES, OFF: MAIL})


class Env:
    def __init__(self, db, *, goals: bool = True, grants: bool = True, **over: Any) -> None:
        self.settings = settings_for(**{**FLAGS, **over})
        self.store = GoalStore(db, self.settings.agent_id, self.settings) if goals else None
        self.dispatcher = ToolDispatcher()
        for name in REGISTERED:
            self.dispatcher.register(name, MagicMock(), {"type": "object", "properties": {}})
        self.view = GoalToolGrants(db, self.settings) if grants else None
        self.db = db
        routes = build_goal_routes(
            database=db,
            settings=self.settings,
            goals=self.store,
            guard=OwnerGuard(self.settings),
            dispatcher=self.dispatcher,
            grants=self.view,
        )
        self.app = Starlette(routes=routes)

    async def call(self, method: str, path: str, *, headers: dict | None = None, **kw: Any):
        return await send(self.app, method, path, headers=OWNER if headers is None else headers, **kw)

    async def allow(self, ref: Any, tool: str, body: dict | None = None, **kw: Any):
        return await self.call("PUT", f"/goals/{ref}/tools/{tool}", json={"confirm": True} if body is None else body)

    async def remove(self, ref: Any, tool: str, **kw: Any):
        return await self.call("DELETE", f"/goals/{ref}/tools/{tool}", json={}, **kw)


@pytest.fixture
def env(db, mcp_registry):
    tool_guard.install(ToolGuard())
    return Env(db)


async def _rows(db, goal) -> list[GoalToolGrant]:
    async with db.session() as s:
        query = select(GoalToolGrant).where(GoalToolGrant.goal_id == goal.id).order_by(GoalToolGrant.granted_at)
        return list((await s.execute(query)).scalars())


async def _state_rows(db, goal) -> list[GoalJournal]:
    async with db.session() as s:
        query = select(GoalJournal).where(GoalJournal.goal_id == goal.id, GoalJournal.kind == "state")
        return list((await s.execute(query.order_by(GoalJournal.seq))).scalars())


def _ref(goal) -> str:
    return f"G-{goal.short_id:04d}"


# ---- the guard and the flags ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path"),
    [("GET", "/goals/G-0001/tools"), ("PUT", "/goals/G-0001/tools/send_email"), ("DELETE", "/goals/G-0001/tools/x")],
)
async def test_every_tool_route_needs_the_owners_or_the_proxys_secret(db, method, path):
    env = Env(db, goals=False, grants=False)
    response = await send(env.app, method, path, json={"confirm": True})
    assert (response.status_code, response.json()) == (403, FORBIDDEN)
    response = await send(env.app, method, path, json={"confirm": True}, headers=PROXY)
    assert response.status_code != 403


async def test_with_goals_off_the_read_says_so_and_the_changes_are_503(db):
    env = Env(db, goals=False, grants=False)
    response = await env.call("GET", "/goals/G-0001/tools")
    assert (response.status_code, response.json()) == (200, {"enabled": False})
    for response in (await env.allow("G-0001", "send_email"), await env.remove("G-0001", "send_email")):
        assert (response.status_code, response.json()) == (503, GOALS_OFF)


@pytest.mark.parametrize("why", ["flag", "view"])
async def test_with_the_grants_off_the_read_says_so_and_the_changes_are_503(db, why):
    env = Env(db, goal_tool_grants_enabled=False) if why == "flag" else Env(db, grants=False)
    goal = await active(env.store, origin_channel=CHAN)
    response = await env.call("GET", f"/goals/{goal.id}/tools")
    assert (response.status_code, response.json()) == (200, {"enabled": True, "grants_enabled": False})
    for response in (await env.allow(goal.id, "send_email"), await env.remove(goal.id, "send_email")):
        assert (response.status_code, response.json()) == (503, GRANTS_OFF)
    assert GRANTS_OFF == {"error": "Tool grants are off on this server."}


# ---- bodies -----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "sentence"),
    [
        ({"confirm": True, "actor": "owner"}, "Not accepted: actor."),
        ({"confirm": "yes"}, "Not accepted: confirm."),
        ({"confirm": 1}, "Not accepted: confirm."),
        ({"confirm": True, "identity": 7}, "Not accepted: identity."),
        ({"confirm": True, "server_id": "x"}, "Not accepted: server_id."),
        ({}, "Allowing a tool needs your confirmation."),
        ({"confirm": False}, "Allowing a tool needs your confirmation."),
        ({"confirm": None}, "Allowing a tool needs your confirmation."),
    ],
)
async def test_a_put_body_is_strict_and_needs_the_confirmation(env, body, sentence):
    goal = await active(env.store, origin_channel=CHAN)
    response = await env.allow(goal.id, "send_email", body)
    assert (response.status_code, response.json()) == (400, {"error": sentence})
    assert await _rows(env.db, goal) == []


async def test_a_body_that_is_not_an_object_is_refused(env):
    goal = await active(env.store, origin_channel=CHAN)
    for content in (b"[1]", b"not json"):
        response = await send(
            env.app,
            "PUT",
            f"/goals/{goal.id}/tools/send_email",
            content=content,
            headers={**OWNER, "content-type": "application/json"},
        )
        assert (response.status_code, response.json()) == (400, {"error": "Not accepted: body."})
    response = await env.call("DELETE", f"/goals/{goal.id}/tools/send_email", json={"actor": "owner"})
    assert (response.status_code, response.json()) == (400, {"error": "Not accepted: actor."})


# ---- refusals ---------------------------------------------------------------------------------------------------


async def test_an_unknown_goal_is_404_and_a_finished_one_409(env):
    response = await env.allow("G-9999", "send_email")
    assert (response.status_code, response.json()) == (404, {"error": "no goal G-9999"})
    assert (await env.remove("G-9999", "send_email")).status_code == 404
    assert (await env.call("GET", "/goals/G-9999/tools")).status_code == 404
    goal = await active(env.store, origin_channel=CHAN)
    await env.allow(goal.id, "send_email")
    await env.store.cancel(goal.id, actor="owner")
    sentence = f"{_ref(goal)} is finished; its tools can no longer be changed."
    for response in (await env.allow(goal.id, "send_file"), await env.remove(goal.id, "send_email")):
        assert (response.status_code, response.json()) == (409, {"error": sentence})


@pytest.mark.parametrize(
    ("tool", "sentence"),
    [
        ("bash", "bash can never be given to a goal."),
        ("resolve_intention", "resolve_intention can never be given to a goal."),
        ("web_search", "web_search needs no permission: a goal can use it already, or never can."),
        ("not_a_tool", "not_a_tool is not a tool Nous has now."),
        ("mcp__gone__thing", "mcp__gone__thing is not a tool Nous has now."),
        ("dag_create", "dag_create can never be given to a goal."),
    ],
)
async def test_a_tool_a_goal_cannot_be_given_is_400(env, tool, sentence):
    goal = await active(env.store, origin_channel=CHAN)
    response = await env.allow(goal.id, tool)
    assert (response.status_code, response.json()) == (400, {"error": sentence})


async def test_a_tool_that_starts_work_is_400(env, monkeypatch):
    table = dict(TOOL_CLASSES)
    table["reach_and_spawn"] = ToolClass("external", spawns=True)
    monkeypatch.setattr(tool_classes, "TOOL_CLASSES", MappingProxyType(table))
    env.dispatcher.register("reach_and_spawn", MagicMock(), {"type": "object", "properties": {}})
    goal = await active(env.store, origin_channel=CHAN)
    response = await env.allow(goal.id, "reach_and_spawn")
    assert response.json() == {"error": "reach_and_spawn starts other work and can never be given to a goal."}


async def test_a_grantable_tool_nous_does_not_have_now_is_400(env):
    env.dispatcher.unregister("browser_task")
    goal = await active(env.store, origin_channel=CHAN)
    response = await env.allow(goal.id, "browser_task")
    assert (response.status_code, response.json()) == (400, {"error": "browser_task is not a tool Nous has now."})


@pytest.mark.parametrize(
    ("guard", "status", "sentence"),
    [
        (
            ToolGuard(denied=frozenset({"send_email"})),
            409,
            "send_email is switched off for background work. Set it to Allowed under Limits and tools first.",
        ),
        (
            ToolGuard(locked=frozenset({"send_email"})),
            409,
            "send_email is locked by the server and cannot be given to a goal.",
        ),
        (
            ToolGuard(hold_all=True),
            503,
            "The saved tool settings could not be read, so nothing can be changed until they can.",
        ),
        (None, 503, "The saved tool settings could not be read, so nothing can be changed until they can."),
    ],
    ids=["owner", "lock", "not read", "no guard"],
)
async def test_the_tool_guard_refuses_a_grant(env, guard, status, sentence):
    tool_guard.install(guard)
    goal = await active(env.store, origin_channel=CHAN)
    response = await env.allow(goal.id, "send_email")
    assert (response.status_code, response.json()) == (status, {"error": sentence})
    assert await _rows(env.db, goal) == []


async def test_an_mcp_tool_of_a_server_not_available_to_background_work_is_409(env):
    goal = await active(env.store, origin_channel=CHAN)
    response = await env.allow(goal.id, OFF, {"confirm": True, "identity": identity_token(MAIL)})
    sentence = "mail is not available to background work. Turn that on for the server on the Integrations screen first."
    assert (response.status_code, response.json()) == (409, {"error": sentence})


@pytest.mark.parametrize(
    ("tool", "over", "sentence"),
    [
        (
            "send_file",
            {"telegram_chat_id": ""},
            "send_file has no Telegram chat set, so it could send to any chat a call names. Set the chat first.",
        ),
        ("send_email", {"email_allowlist": ""}, "The email allowlist is empty, so send_email could send nothing."),
    ],
)
async def test_a_tool_the_server_has_not_set_up_is_409(db, mcp_registry, tool, over, sentence):
    tool_guard.install(ToolGuard())
    env = Env(db, **over)
    goal = await draft(env.store, origin_channel=CHAN)
    response = await env.allow(goal.id, tool)
    assert (response.status_code, response.json()) == (409, {"error": sentence})


async def test_at_most_twenty_tools(env):
    goal = await active(env.store, origin_channel=CHAN)
    for n in range(20):
        identity = McpIdentity(uuid.uuid4(), "ab" * 32, "t", server_name=f"s{n}", server_host=f"s{n}.example.com")
        await env.store.grant_tool(goal.id, f"mcp__s{n}__t", identity, actor="owner", proof="owner")
    response = await env.allow(goal.id, "send_email")
    assert (response.status_code, response.json()) == (
        409,
        {"error": "A goal can have at most 20 tools. Remove one first."},
    )
    listed = (await env.call("GET", f"/goals/{goal.id}/tools")).json()
    blocked = {row["tool"]: row["blocked"] for row in listed["available"]}
    assert blocked["send_email"] == {"code": "cap", "sentence": "A goal can have at most 20 tools. Remove one first."}


async def test_an_mcp_grant_names_the_identity_the_screen_showed(env):
    goal = await active(env.store, origin_channel=CHAN)
    response = await env.allow(goal.id, ON)
    assert (response.status_code, response.json()) == (400, {"error": "Not accepted: identity."})
    response = await env.allow(goal.id, ON, {"confirm": True, "identity": identity_token(MAIL)})
    assert (response.status_code, response.json()) == (
        409,
        {"error": "That server changed since you looked. Look again."},
    )
    response = await env.allow(goal.id, "send_email", {"confirm": True, "identity": identity_token(NOTES)})
    assert (response.status_code, response.json()) == (400, {"error": "Not accepted: identity."})
    response = await env.allow(goal.id, ON, {"confirm": True, "identity": identity_token(NOTES)})
    assert response.status_code == 200 and response.json()["changed"] is True
    [row] = await _rows(env.db, goal)
    assert (row.server_name, row.server_host, row.server_id, row.url_sha256, row.remote_tool) == (
        "notes",
        "notes.example.com",
        NOTES.server_id,
        NOTES.url_sha256,
        "search",
    )


async def _journal_count(db, goal) -> int:
    async with db.session() as s:
        return len(list((await s.execute(select(GoalJournal.seq).where(GoalJournal.goal_id == goal.id))).scalars()))


def _repointed(identity: McpIdentity, *, url: str | None = None, remote: str | None = None) -> McpIdentity:
    """The same server row, pointed elsewhere (as ``McpRegistry._upsert_server`` keeps a row's id when its URL
    changes), or with another remote tool behind the name."""
    return McpIdentity(
        identity.server_id,
        url or "c3" * 32,
        remote or identity.remote_tool,
        server_name=identity.server_name,
        server_host="elsewhere.example.com",
    )


@pytest.mark.parametrize("change", [{"url": "c3" * 32}, {"remote": "search_all"}])
async def test_a_server_repointed_after_the_page_was_read_is_refused_and_nothing_is_written(env, change):
    goal = await active(env.store, origin_channel=CHAN)
    shown = {row["tool"]: row for row in (await env.call("GET", f"/goals/{goal.id}/tools")).json()["available"]}
    token = shown[ON]["identity"]
    assert token == identity_token(NOTES) and "notes.example.com" not in token
    moved = _repointed(NOTES, **change)
    tool_classes._MCP_IDENTITIES[ON] = moved
    journal_before = await _journal_count(env.db, goal)
    response = await env.allow(goal.id, ON, {"confirm": True, "identity": token})
    assert (response.status_code, response.json()) == (409, {"error": SERVER_CHANGED_SENTENCE})
    assert await _rows(env.db, goal) == [] and await _journal_count(env.db, goal) == journal_before
    # Read again, the page shows the new identity, and that one is accepted.
    shown = {row["tool"]: row for row in (await env.call("GET", f"/goals/{goal.id}/tools")).json()["available"]}
    assert shown[ON]["identity"] == identity_token(moved) != token
    response = await env.allow(goal.id, ON, {"confirm": True, "identity": shown[ON]["identity"]})
    assert response.status_code == 200 and response.json()["changed"] is True
    [row] = await _rows(env.db, goal)
    assert (row.url_sha256, row.remote_tool) == (moved.url_sha256, moved.remote_tool)


async def test_a_stale_identity_does_not_replace_a_grant_that_stopped_working(env):
    goal = await active(env.store, origin_channel=CHAN)
    await env.allow(goal.id, ON, {"confirm": True, "identity": identity_token(NOTES)})
    tool_classes._MCP_IDENTITIES[ON] = _repointed(NOTES)
    response = await env.allow(goal.id, ON, {"confirm": True, "identity": identity_token(NOTES)})
    assert response.status_code == 409
    [row] = await _rows(env.db, goal)
    assert (row.url_sha256, row.revoked_at) == (NOTES.url_sha256, None)  # the old grant, untouched


# ---- changes ----------------------------------------------------------------------------------------------------


async def test_allow_and_remove_are_idempotent_and_store_the_proof(env):
    goal = await draft(env.store, origin_channel=CHAN)  # a draft accepts grants
    response = await env.call("PUT", f"/goals/{goal.id}/tools/send_email", json={"confirm": True}, headers=PROXY)
    assert response.status_code == 200 and response.json()["changed"] is True
    again = await env.allow(goal.id, "send_email")
    assert again.status_code == 200 and again.json()["changed"] is False
    [row] = await _rows(env.db, goal)
    assert row.granted_by == "proxy"
    journal = (await _state_rows(env.db, goal))[-1]
    assert (journal.actor, journal.text) == ("owner", "Allowed to use send_email without asking, from now on.")
    removed = await env.remove(goal.id, "send_email", headers=PROXY)
    assert removed.status_code == 200 and removed.json()["changed"] is True
    assert (await env.remove(goal.id, "send_email")).json()["changed"] is False
    [row] = await _rows(env.db, goal)
    assert row.revoked_by == "proxy"


async def test_a_tool_no_longer_registered_can_still_be_removed(env):
    goal = await active(env.store, origin_channel=CHAN)
    await env.allow(goal.id, "browser_task")
    env.dispatcher.unregister("browser_task")
    response = await env.remove(goal.id, "browser_task")
    assert response.status_code == 200 and response.json()["changed"] is True


async def test_a_change_reaches_the_runners_view(env):
    goal = await active(env.store, origin_channel=CHAN)
    root = uuid.uuid4()
    async with env.db.session() as s:
        s.add(
            GoalSession(
                agent_id=env.store.agent_id,
                goal_id=goal.id,
                n=1,
                trigger="timer",
                first_subtask_id=uuid.uuid4(),
                root_id=root,
            )
        )
        await s.commit()
    await env.view.refresh()
    ctx = ExecutionContext(kind="continuation", authority="internal_only", intention_id=root, root_intention_id=root)
    assert not decide(env.view, ctx, "send_email").lifted
    await env.allow(goal.id, "send_email")
    assert decide(env.view, ctx, "send_email").lifted
    await env.call("POST", f"/goals/{goal.id}/pause", json={})
    assert not decide(env.view, ctx, "send_email").lifted
    await env.call("POST", f"/goals/{goal.id}/resume", json={})
    assert decide(env.view, ctx, "send_email").lifted
    await env.remove(goal.id, "send_email")
    assert not decide(env.view, ctx, "send_email").lifted


async def test_a_change_whose_view_read_fails_answers_reload(env):
    goal = await active(env.store, origin_channel=CHAN)

    class Broken:
        def session(self):
            raise ConnectionError("down")

    env.view._db = Broken()
    response = await env.allow(goal.id, "send_email")
    assert response.status_code == 200 and response.json()["reload"] is True and response.json()["changed"] is True
    assert len(await _rows(env.db, goal)) == 1


# ---- the answer --------------------------------------------------------------------------------------------------


async def test_the_read_lists_the_grants_and_the_tools_that_could_be_allowed(env):
    goal = await active(env.store, origin_channel=CHAN)
    await env.allow(goal.id, "send_email")
    await env.allow(goal.id, ON, {"confirm": True, "identity": identity_token(NOTES)})
    body = (await env.call("GET", f"/goals/{_ref(goal)}/tools")).json()
    assert (body["enabled"], body["grants_enabled"], body["can_edit"], body["limit"]) == (True, True, True, 20)
    assert body["applies_in"] == lead_sentence() and body["never"] == NEVER_SENTENCE
    grants = {row["tool"]: row for row in body["grants"]}
    assert set(grants) == {"send_email", ON}
    email = grants["send_email"]
    assert email["says"] == (
        "Send email, with files this goal's work wrote, to the addresses on your email allowlist. "
        "A sent email cannot be taken back."
    )
    assert (email["usable"], email["not_usable_why"], email["server_name"]) == (True, None, None)
    notes = grants[ON]
    assert (notes["label"], notes["server_name"], notes["server_host"], notes["group"]) == (
        "search",
        "notes",
        "notes.example.com",
        "other_servers",
    )
    assert notes["says"].startswith("Use search on notes (notes.example.com).")
    available = {row["tool"]: row for row in body["available"]}
    assert set(available) == {"send_file", "browser_task", OFF}  # grantable, registered, not granted
    assert available["send_file"]["blocked"] is None
    assert available["send_file"]["confirm"] == (
        f"Allow {_ref(goal)} to send files this goal's work wrote to your Telegram chat without asking you each "
        "time? "
        "A sent file cannot be taken back."
    )
    assert available[OFF]["blocked"]["code"] == "server_off"
    assert available[OFF]["server_id"] == str(MAIL.server_id)


async def test_a_grant_shows_why_it_is_not_used_now(env):
    goal = await active(env.store, origin_channel=CHAN)
    await env.allow(goal.id, "send_email")
    await env.allow(goal.id, ON, {"confirm": True, "identity": identity_token(NOTES)})

    async def why() -> dict:
        body = (await env.call("GET", f"/goals/{goal.id}/tools")).json()
        return {row["tool"]: row["not_usable_why"] for row in body["grants"]}

    tool_guard.install(ToolGuard(denied=frozenset({"send_email"})))
    assert (await why())["send_email"] == "Switched off for background work under Limits and tools."
    tool_guard.install(ToolGuard(locked=frozenset({"send_email"})))
    assert (await why())["send_email"] == "Locked by the server."
    tool_guard.install(ToolGuard(hold_all=True))
    assert (await why())["send_email"] == "Held until the saved tool settings can be read."
    tool_guard.install(ToolGuard())
    tool_classes._MCP_TOOLS[ON] = False
    assert (await why())[ON] == "Its server is not available to background work."
    tool_classes._MCP_IDENTITIES[ON] = McpIdentity(uuid.uuid4(), NOTES.url_sha256, "search")
    assert (await why())[ON].startswith("Its server changed since you allowed it.")
    del tool_classes._MCP_TOOLS[ON]
    assert (await why())[ON] == "Its server is not connected now."


async def test_a_finished_goals_tools_are_listed_and_cannot_be_edited(env):
    goal = await active(env.store, origin_channel=CHAN)
    await env.allow(goal.id, "send_email")
    await env.store.cancel(goal.id, actor="owner")
    body = (await env.call("GET", f"/goals/{goal.id}/tools")).json()
    assert body["can_edit"] is False and [row["tool"] for row in body["grants"]] == ["send_email"]


# ---- goal actions read the view again ---------------------------------------------------------------------------


class Recorder:
    """A grants view that records which reads were asked for; with ``fail`` each one reports a failure."""

    def __init__(self, fail: bool = False) -> None:
        self.calls: list[tuple] = []
        self.fail = fail

    async def refresh_goal(self, goal_id) -> bool:
        self.calls.append(("goal", goal_id))
        return not self.fail


def _app(db, grants) -> tuple[Starlette, GoalStore]:
    settings = settings_for()
    store = GoalStore(db, settings.agent_id, settings)
    routes = build_goal_routes(database=db, settings=settings, goals=store, guard=OwnerGuard(settings), grants=grants)
    return Starlette(routes=routes), store


@pytest.mark.parametrize("action", ["pause", "stop", "cancel"])
async def test_a_goal_action_that_changes_the_state_reads_the_goal_again(db, action):
    recorder = Recorder()
    app, store = _app(db, recorder)
    goal = await active(store, origin_channel=CHAN)
    response = await send(app, "POST", f"/goals/{goal.id}/{action}", headers=OWNER, json={})
    assert response.status_code == 200
    assert recorder.calls == [("goal", goal.id)]


async def test_approve_and_resume_read_the_goal_again_and_a_note_does_not(db):
    recorder = Recorder()
    app, store = _app(db, recorder)
    goal = await draft(store, origin_channel=CHAN)
    await send(app, "POST", f"/goals/{goal.id}/approve", headers=OWNER, json={})
    await send(app, "POST", f"/goals/{goal.id}/note", headers=OWNER, json={"text": "Look at the drafts too."})
    await send(app, "POST", f"/goals/{goal.id}/budget", headers=OWNER, json={"day": 400_000})
    assert recorder.calls == [("goal", goal.id)]
    await send(app, "POST", f"/goals/{goal.id}/pause", headers=OWNER, json={})
    await send(app, "POST", f"/goals/{goal.id}/resume", headers=OWNER, json={})
    assert recorder.calls == [("goal", goal.id)] * 3


async def test_a_failing_read_after_an_action_does_not_fail_the_route(db):
    app, store = _app(db, Recorder(fail=True))
    goal = await active(store, origin_channel=CHAN)
    response = await send(app, "POST", f"/goals/{goal.id}/pause", headers=OWNER, json={})
    assert response.status_code == 200 and response.json()["state"] == "paused"

    class Raising(Recorder):
        async def refresh_goal(self, goal_id) -> bool:
            raise RuntimeError("down")

    app, store = _app(db, Raising())
    goal = await active(store, origin_channel=CHAN)
    response = await send(app, "POST", f"/goals/{goal.id}/pause", headers=OWNER, json={})
    assert response.status_code == 200 and response.json()["state"] == "paused"


@pytest.mark.parametrize(
    ("state", "why"),
    [
        ("draft", "The goal is a draft: nothing runs until you approve it."),
        ("paused", "The goal is paused."),
        ("cancelled", "The goal is finished."),
    ],
)
async def test_a_grant_of_a_goal_that_is_not_active_says_it_is_not_used(env, state, why):
    goal = await draft(env.store, origin_channel=CHAN)
    await env.allow(goal.id, "send_email")
    if state == "paused":
        await env.store.pause((await env.store.approve(goal.id, actor="owner")).id, actor="owner")
    if state == "cancelled":
        await env.store.cancel(goal.id, actor="owner")
    [grant] = (await env.call("GET", f"/goals/{goal.id}/tools")).json()["grants"]
    assert (grant["usable"], grant["not_usable_why"]) == (False, why)
    if state == "paused":
        await env.store.resume(goal.id, actor="owner")
        [grant] = (await env.call("GET", f"/goals/{goal.id}/tools")).json()["grants"]
        assert (grant["usable"], grant["not_usable_why"]) == (True, None)
