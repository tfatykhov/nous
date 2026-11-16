"""Per-goal tool grants, the view and the rule (``nous.goals.grants``): which turns a grant reaches (``decide``), what
the in-process view holds (open sessions of active goals only), how it stays fresh and fails closed, how a revoke wins
over a refresh in flight, the one journal row of a session's first use, and the screen's words tied to the code's own
tables."""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select, text

from nous.api import tool_classes, tool_policy
from nous.api.execution_context import CONTEXT_KINDS, ExecutionContext
from nous.api.tool_classes import TOOL_CLASSES, McpIdentity
from nous.autonomy import tool_guard
from nous.autonomy.tool_guard import ToolGuard
from nous.config import Settings
from nous.goals import grants as G
from nous.goals.grants import Decision, GoalToolGrants, decide
from nous.goals.store import GoalStore
from nous.storage.models import Goal, GoalJournal, GoalSession

pytestmark = pytest.mark.postgres_only

NOTES = McpIdentity(uuid.uuid4(), "ab" * 32, "search", server_name="notes", server_host="notes.example.com")


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _settings(agent: str, **over) -> Settings:
    return Settings(_env_file=None, agent_id=agent, telegram_chat_id="", goals_tick_seconds=60, **over)


async def _world(db, **over):
    agent = f"grant-view-{uuid.uuid4().hex[:8]}"
    settings = _settings(agent, **over)
    store = GoalStore(db, agent, settings)
    clock = Clock()
    view = GoalToolGrants(db, settings, clock=clock)
    tool_guard.install(ToolGuard())
    return store, view, clock


async def _goal(store: GoalStore, *, state: str = "active") -> Goal:
    goal = await store.create(
        title="Tidy the notes",
        objective="Merge the duplicate notes",
        done_when="None are left",
        proposed_by="owner",
        origin_channel="telegram:4242",
    )
    if state == "draft":
        return goal
    goal = await store.approve(goal.id, actor="owner")
    if state == "paused":
        goal = await store.pause(goal.id, actor="owner")
    if state == "cancelled":
        goal = await store.cancel(goal.id, actor="owner")
    return goal


async def _session(db, store: GoalStore, goal: Goal, n: int = 1, *, ended: bool = False) -> uuid.UUID:
    root = uuid.uuid4()
    async with db.session() as s:
        s.add(
            GoalSession(
                agent_id=store.agent_id,
                goal_id=goal.id,
                n=n,
                trigger="timer",
                first_subtask_id=uuid.uuid4(),
                root_id=root,
                ended_at=datetime.now(UTC) if ended else None,
                outcome="no_step" if ended else None,
            )
        )
        await s.commit()
    return root


async def _end(db, root: uuid.UUID) -> None:
    async with db.session() as s:
        await s.execute(
            text("UPDATE brain.goal_sessions SET ended_at = now(), outcome = 'no_step' WHERE root_id = :r"), {"r": root}
        )
        await s.commit()


def _ctx(root: uuid.UUID | None, kind: str = "continuation", authority: str = "internal_only", **extra):
    if kind == "continuation":
        extra = {"intention_id": uuid.uuid4(), **extra}
    if kind == "approved_action":
        extra = {"proposal_id": uuid.uuid4(), "declared_tools": ("send_email",), **extra}
    return ExecutionContext(kind=kind, authority=authority, root_intention_id=root, **extra)


@pytest.fixture
def mcp_registry(monkeypatch):
    monkeypatch.setattr(tool_classes, "_MCP_TOOLS", {})
    monkeypatch.setattr(tool_classes, "_MCP_IDENTITIES", {})


# ---- what the view holds ----------------------------------------------------------------------------------------


async def test_the_view_holds_the_open_session_of_an_active_goal(db):
    store, view, _clock = await _world(db)
    goal = await _goal(store)
    root = await _session(db, store, goal)
    await store.grant_tool(goal.id, "send_email", None, actor="owner", proof="owner")
    assert await view.refresh() is True
    grant = view.grant(root, "send_email")
    assert (grant.goal_id, grant.session_n, grant.tool, grant.identity) == (goal.id, 1, "send_email", None)
    assert view.grant(root, "send_file") is None
    assert view.fresh()


@pytest.mark.parametrize("state", ["draft", "paused", "cancelled"])
async def test_a_goal_that_is_not_active_has_nothing_in_the_view(db, state):
    store, view, _clock = await _world(db)
    goal = await _goal(store, state=state)
    root = await _session(db, store, goal)
    if state != "cancelled":
        await store.grant_tool(goal.id, "send_email", None, actor="owner", proof="owner")
    else:
        async with db.session() as s:  # granted while it was live
            await s.execute(
                text(
                    "INSERT INTO brain.goal_tool_grants (agent_id, goal_id, tool, granted_by) "
                    "VALUES (:a, :g, 'send_email', 'owner')"
                ),
                {"a": store.agent_id, "g": goal.id},
            )
            await s.commit()
    await view.refresh()
    assert view.grant(root, "send_email") is None


async def test_an_ended_session_leaves_the_view_through_the_query(db):
    store, view, _clock = await _world(db)
    goal = await _goal(store)
    root = await _session(db, store, goal)
    await store.grant_tool(goal.id, "send_email", None, actor="owner", proof="owner")
    await view.refresh()
    assert view.grant(root, "send_email") is not None
    await _end(db, root)
    await view.refresh()
    assert view.grant(root, "send_email") is None
    ended = await _session(db, store, goal, 2, ended=True)
    await view.refresh_root(ended)
    assert view.grant(ended, "send_email") is None


async def test_a_session_with_no_root_yet_is_not_in_the_view(db):
    store, view, _clock = await _world(db)
    goal = await _goal(store)
    async with db.session() as s:
        s.add(
            GoalSession(agent_id=store.agent_id, goal_id=goal.id, n=1, trigger="timer", first_subtask_id=uuid.uuid4())
        )
        await s.commit()
    await store.grant_tool(goal.id, "send_email", None, actor="owner", proof="owner")
    await view.refresh()
    assert view._by_root == {}


async def test_two_goals_each_get_their_own_grants_and_a_revoked_grant_is_gone(db):
    store, view, _clock = await _world(db)
    one, two = await _goal(store), await _goal(store)
    root_one, root_two = await _session(db, store, one), await _session(db, store, two)
    await store.grant_tool(one.id, "send_email", None, actor="owner", proof="owner")
    await store.grant_tool(two.id, "send_file", None, actor="owner", proof="owner")
    await store.grant_tool(two.id, "browser_task", None, actor="owner", proof="owner")
    await store.revoke_tool(two.id, "browser_task", actor="owner", proof="owner")
    await view.refresh()
    assert view.grant(root_one, "send_email") is not None and view.grant(root_one, "send_file") is None
    assert view.grant(root_two, "send_file") is not None and view.grant(root_two, "send_email") is None
    assert view.grant(root_two, "browser_task") is None


async def test_the_view_is_scoped_to_its_agent(db):
    store, view, _clock = await _world(db)
    other_store, _other_view, _ = await _world(db)
    goal = await _goal(other_store)
    root = await _session(db, other_store, goal)
    await other_store.grant_tool(goal.id, "send_email", None, actor="owner", proof="owner")
    await view.refresh()
    assert view.grant(root, "send_email") is None


async def test_refresh_root_and_refresh_goal_replace_only_their_part(db):
    store, view, _clock = await _world(db)
    one, two = await _goal(store), await _goal(store)
    root_one, root_two = await _session(db, store, one), await _session(db, store, two)
    await store.grant_tool(one.id, "send_email", None, actor="owner", proof="owner")
    await store.grant_tool(two.id, "send_email", None, actor="owner", proof="owner")
    await view.refresh()
    await store.revoke_tool(one.id, "send_email", actor="owner", proof="owner")
    await store.grant_tool(one.id, "send_file", None, actor="owner", proof="owner")
    await store.revoke_tool(two.id, "send_email", actor="owner", proof="owner")
    assert await view.refresh_root(root_one) is True
    assert view.grant(root_one, "send_email") is None and view.grant(root_one, "send_file") is not None
    assert view.grant(root_two, "send_email") is not None  # not read again yet
    assert await view.refresh_goal(two.id) is True
    assert view.grant(root_two, "send_email") is None
    assert view.grant(root_one, "send_file") is not None


# ---- freshness and failure -------------------------------------------------------------------------------------


async def test_fresh_follows_the_injected_monotonic_clock_and_only_a_full_refresh_renews_it(db, monkeypatch):
    store, view, clock = await _world(db)
    assert not view.fresh()  # never loaded
    await view.refresh()
    assert view.fresh()
    max_age = max(3 * 60, G.MIN_FRESH_SECONDS)
    clock.now += max_age
    assert view.fresh()
    clock.now += 1
    assert not view.fresh()
    goal = await _goal(store)
    await view.refresh_goal(goal.id)
    assert not view.fresh()
    await view.refresh()
    assert view.fresh()
    # The wall clock moves; nothing changes.
    monkeypatch.setattr(time, "time", lambda: 10**12)
    assert view.fresh()


async def test_the_default_clock_is_monotonic():
    view = GoalToolGrants(None, _settings("x"))
    assert view._clock is time.monotonic


async def test_a_refresh_that_fails_clears_the_whole_view(db, caplog):
    store, view, _clock = await _world(db)
    goal = await _goal(store)
    root = await _session(db, store, goal)
    await store.grant_tool(goal.id, "send_email", None, actor="owner", proof="owner")
    await view.refresh()
    assert view.grant(root, "send_email") is not None

    class Broken:
        def session(self):
            raise ConnectionError("db at 10.0.0.1 is down")

    good, view._db = view._db, Broken()
    for refresh in (view.refresh, lambda: view.refresh_root(root), lambda: view.refresh_goal(goal.id)):
        assert await refresh() is False
        assert view.grant(root, "send_email") is None and not view.fresh()
    assert "10.0.0.1" not in caplog.text and "ConnectionError" in caplog.text
    view._db = good
    assert await view.refresh() is True
    assert view.grant(root, "send_email") is not None


async def test_a_revoke_during_a_suspended_refresh_ends_with_no_lift(db):
    """A refresh reads the grant, then is held between its read and its apply; the revoke commits and refreshes the
    goal. The revoke's refresh waits for the held one, so the grant read before the revoke is never the last word."""
    store, view, _clock = await _world(db)
    goal = await _goal(store)
    root = await _session(db, store, goal)
    await store.grant_tool(goal.id, "send_email", None, actor="owner", proof="owner")
    await view.refresh()
    read_done, release = asyncio.Event(), asyncio.Event()
    real = view._db
    held = {"first": True}

    class Holding:
        @contextlib.asynccontextmanager
        async def session(self):
            async with real.session() as s:
                yield s
            if held["first"]:
                held["first"] = False
                read_done.set()
                await release.wait()

    view._db = Holding()
    suspended = asyncio.create_task(view.refresh_root(root))
    await read_done.wait()
    await store.revoke_tool(goal.id, "send_email", actor="owner", proof="owner")
    after_revoke = asyncio.create_task(view.refresh_goal(goal.id))
    await asyncio.sleep(0.05)
    release.set()
    assert await suspended is True and await after_revoke is True
    assert view.grant(root, "send_email") is None
    assert decide(view, _ctx(root), "send_email") == Decision(False, "not_granted")


# ---- decide: every why ------------------------------------------------------------------------------------------


async def _granted_world(db, **over):
    store, view, clock = await _world(db, **over)
    goal = await _goal(store)
    root = await _session(db, store, goal)
    await store.grant_tool(goal.id, "send_email", None, actor="owner", proof="owner")
    await view.refresh()
    return store, view, clock, goal, root


async def test_a_turn_of_the_goals_lineage_is_lifted(db):
    _store, view, _clock, _goal_row, root = await _granted_world(db)
    for kind in tool_policy.GRANT_KINDS:
        assert decide(view, _ctx(root, kind), "send_email") == Decision(True, "granted"), kind
    assert view.lifts(_ctx(root), "send_email") is True


async def test_decide_names_why_it_does_not_lift(db, mcp_registry):
    store, view, clock, goal, root = await _granted_world(db)
    assert decide(view, _ctx(root, authority="owner", kind="subtask"), "send_email") == Decision(False, "authority")
    assert decide(view, _ctx(None, kind="subtask"), "send_email") == Decision(False, "no_root")
    assert decide(view, _ctx(root, kind="heartbeat_check"), "send_email") == Decision(False, "kind")
    assert decide(view, _ctx(root, kind="scheduled"), "send_email") == Decision(False, "kind")
    assert decide(view, _ctx(root), "bash") == Decision(False, "not_grantable")
    assert decide(view, _ctx(root), "send_file") == Decision(False, "not_granted")
    assert decide(view, _ctx(uuid.uuid4()), "send_email") == Decision(False, "not_granted")
    clock.now += 10_000
    assert decide(view, _ctx(root), "send_email") == Decision(False, "stale")
    await view.refresh()
    tool_guard.install(None)
    assert decide(view, _ctx(root), "send_email") == Decision(False, "no_guard")


@pytest.mark.parametrize("kind", CONTEXT_KINDS)
async def test_only_the_three_kinds_of_a_goals_lineage_are_lifted(db, kind):
    _store, view, _clock, _goal_row, root = await _granted_world(db)
    authority = "internal_only" if kind != "approved_action" else "owner"
    decision = decide(view, _ctx(root, kind, authority), "send_email")
    assert decision.lifted is (kind in ("continuation", "subtask", "dag_node")), kind


async def test_the_lift_keys_on_the_root_never_the_intention(db):
    _store, view, _clock, _goal_row, root = await _granted_world(db)
    # A subtask two levels down: its own intention, the goal's root.
    deep = ExecutionContext(
        kind="subtask", authority="internal_only", intention_id=uuid.uuid4(), root_intention_id=root
    )
    assert decide(view, deep, "send_email").lifted
    # A turn whose intention is the goal's root but whose root is another's.
    other = ExecutionContext(
        kind="subtask", authority="internal_only", intention_id=root, root_intention_id=uuid.uuid4()
    )
    assert not decide(view, other, "send_email").lifted


async def test_an_mcp_grant_lifts_only_while_the_tool_is_the_one_allowed(db, mcp_registry):
    store, view, _clock = await _world(db)
    goal = await _goal(store)
    root = await _session(db, store, goal)
    await store.grant_tool(goal.id, "mcp__notes__search", NOTES, actor="owner", proof="owner")
    await view.refresh()
    assert decide(view, _ctx(root), "mcp__notes__search") == Decision(False, "identity_changed")  # not registered
    tool_classes.note_mcp_tool("mcp__notes__search", background=True, identity=NOTES)
    assert decide(view, _ctx(root), "mcp__notes__search") == Decision(True, "granted")
    for moved in (
        McpIdentity(uuid.uuid4(), NOTES.url_sha256, "search"),  # deleted and added again
        McpIdentity(NOTES.server_id, "cd" * 32, "search"),  # another URL
        McpIdentity(NOTES.server_id, NOTES.url_sha256, "find"),  # another remote tool
    ):
        tool_classes.note_mcp_tool("mcp__notes__search", background=True, identity=moved)
        assert decide(view, _ctx(root), "mcp__notes__search") == Decision(False, "identity_changed")
    tool_classes.note_mcp_tool("mcp__notes__search", background=True)
    assert decide(view, _ctx(root), "mcp__notes__search") == Decision(False, "identity_changed")


async def test_a_decision_that_raises_does_not_lift(db, caplog):
    _store, view, _clock, _goal_row, root = await _granted_world(db)

    def boom(_root, _tool):
        raise RuntimeError("secret detail")

    view.grant = boom
    assert decide(view, _ctx(root), "send_email") == Decision(False, "error")
    assert "secret detail" not in caplog.text and "RuntimeError" in caplog.text


# ---- the first use of a granted tool in a session ---------------------------------------------------------------


async def _use_rows(db, goal: Goal) -> list[GoalJournal]:
    async with db.session() as s:
        query = select(GoalJournal).where(GoalJournal.goal_id == goal.id, GoalJournal.kind == "progress")
        return list((await s.execute(query.order_by(GoalJournal.seq))).scalars())


async def test_the_first_use_in_a_session_writes_one_harness_row(db):
    store, view, _clock, goal, root = await _granted_world(db)
    view.note_use(root, "send_email")
    view.note_use(root, "send_email")
    view.note_use(root, "send_file")  # not granted: nothing
    await asyncio.gather(*view._tasks)
    [row] = await _use_rows(db, goal)
    assert (row.actor, row.session_n, row.evidence) == ("harness", 1, ["grant-used:send_email"])
    assert row.text == "Session 1 was allowed to use send_email without asking, as you set for this goal."
    # A restart forgets the set; the row's own guard keeps it at one.
    again = GoalToolGrants(db, _settings(store.agent_id))
    await again.refresh()
    again.note_use(root, "send_email")
    await asyncio.gather(*again._tasks)
    assert len(await _use_rows(db, goal)) == 1


async def test_note_used_reads_the_turns_root(db):
    _store, view, _clock, goal, root = await _granted_world(db)
    view.note_used(_ctx(root), "send_email")
    view.note_used(_ctx(None, kind="subtask"), "send_email")
    await asyncio.gather(*view._tasks)
    assert len(await _use_rows(db, goal)) == 1


async def test_a_failed_first_use_row_is_logged_by_class(db, caplog):
    _store, view, _clock, _goal_row, root = await _granted_world(db)

    class Broken:
        def session(self):
            raise ConnectionError("db at 10.0.0.1 is down")

    view._db = Broken()
    view.note_use(root, "send_email")
    await asyncio.gather(*view._tasks, return_exceptions=True)
    await asyncio.sleep(0)
    assert "ConnectionError" in caplog.text and "10.0.0.1" not in caplog.text


# ---- the words, tied to the code's tables -----------------------------------------------------------------------


def test_the_places_are_the_grant_kinds_in_the_guards_words():
    assert set(G.GRANT_PLACES) == set(tool_policy.GRANT_KINDS)
    for kind, words in G.GRANT_PLACES.items():
        assert words.startswith(tool_guard.KINDS[kind][0]), kind
    lead = G.lead_sentence()
    for words in G.GRANT_PLACES.values():
        assert words in lead
    assert "check steps are not included" in lead


def test_the_never_sentence_names_only_tools_that_can_never_be_given():
    named = [tool for tools in G.NEVER_NAMED.values() for tool in tools]
    assert named
    for tool in named:
        assert tool in tool_policy.INTERNAL_ONLY_DENYLIST | tool_policy.INTERNAL_ONLY_SPAWN_TOOLS, tool
        assert tool_policy.grant_refusal(tool) == "never"
    for words in G.NEVER_NAMED:
        assert words.lower() in G.NEVER_SENTENCE.lower(), words


def test_every_tool_a_goal_may_be_given_has_its_words():
    grantable = {name for name in TOOL_CLASSES if tool_policy.grant_refusal(name) is None}
    assert set(G.GRANT_FACTS) == grantable
    for tool, fact in G.GRANT_FACTS.items():
        assert fact.does and fact.note.endswith("."), tool


def test_the_mcp_words_name_the_server_and_host_and_promise_nothing():
    does, note = G.words_for("mcp__notes__search", NOTES)
    assert does == "use search on notes (notes.example.com)"
    assert "cannot check" in note
    for promise in ("safe", "read-only", "only reads", "harmless"):
        assert promise not in (does + note).lower()


def test_the_readiness_of_send_email_and_send_file():
    off = Settings(_env_file=None, telegram_chat_id="", email_allowlist="", email_allowlist_file="")
    assert G.not_ready("send_email", off) == "The email allowlist is empty, so send_email could send nothing."
    assert G.not_ready("send_file", off) == (
        "send_file has no Telegram chat set, so it could send to any chat a call names. Set the chat first."
    )
    on = Settings(_env_file=None, telegram_chat_id="4242", email_allowlist="a@example.com")
    assert G.not_ready("send_email", on) is None and G.not_ready("send_file", on) is None
    assert G.not_ready("send_email", Settings(_env_file=None, email_allowlist_file="/x/allow.txt")) is None
    assert G.not_ready("browser_task", off) is None


def test_the_journal_rows_evidence_is_json():
    assert json.loads(json.dumps(G.used_ref("send_email"))) == "grant-used:send_email"
