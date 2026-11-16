"""Per-goal tool grants end to end: the real app's lifespan builds the view, reads it before the driver starts, binds it
to the runner and hands it and the dispatcher to the goal routes; a session the real driver starts has its first task
offered the goal's granted tool through the context the worker derives from the stored subtask row, and so has its
decision turn; a real turn of each kind a grant reaches calls the tool and records it in the durable ledger; and the
first-use journal row leaves the chain's own note limit as it was."""

from __future__ import annotations

import asyncio
import uuid
from unittest.mock import MagicMock

import httpx
import pytest
from f099_support import (  # noqa: F401
    SEND_EMAIL_ARGS,
    env_factory,
    register_send_email,
    runner_env,
    say,
    use,
)
from goal_routes_support import OWNER, settings_for
from goals_support import GOALS, goal_tools, reset_goal_report_channel  # noqa: F401
from sqlalchemy import select, text
from test_goals_driver import _active, _started, _world
from test_work_view_retention import _run

import nous.main as main
from nous.api import runner as runner_module
from nous.api.execution_context import ExecutionContext
from nous.api.runner import AgentRunner
from nous.api.tools import ToolDispatcher
from nous.autonomy import tool_guard
from nous.autonomy.tool_guard import ToolGuard
from nous.brain import continuation
from nous.brain.embeddings import EmbeddingProvider
from nous.cognitive.ledger_store import LedgerStore
from nous.config import Settings
from nous.goals.driver import GoalDriver
from nous.goals.grants import GoalToolGrants
from nous.goals.store import GoalStore
from nous.goals.tools import NOTES_PER_SESSION
from nous.heartbeat.runner import HeartbeatRunner
from nous.storage.database import Database
from nous.storage.models import ExecutionLedgerEntry, GoalJournal, GoalSession, Subtask

pytestmark = pytest.mark.postgres_only

GRANTS = {**GOALS, "autonomy_settings_enabled": True, "tool_guard_enabled": True, "goal_tool_grants_enabled": True}


# ---- the app ----------------------------------------------------------------------------------------------------


def test_a_boot_with_the_flag_on_builds_reads_and_binds_the_view_and_hands_it_to_the_routes(monkeypatch):
    agent_id = f"test-grants-boot-{uuid.uuid4().hex[:8]}"

    async def already_migrated(engine) -> None:
        async with engine.begin() as connection:
            await connection.execute(
                text("INSERT INTO nous_system.agents (id, name, config) VALUES (:id, 'x', '{}')"), {"id": agent_id}
            )

    async def never_start(self) -> None:
        return None

    async def no_network(self, payload, *, batch=False):
        raise httpx.ConnectError("no network in this test")

    driver_starts: list[bool] = []

    async def driver_not_started(self) -> None:  # so the first read is create_components' own
        driver_starts.append(True)

    monkeypatch.setattr(main, "run_migrations", already_migrated)
    monkeypatch.setattr(HeartbeatRunner, "start", never_start)
    monkeypatch.setattr(GoalDriver, "start", driver_not_started)
    monkeypatch.setattr(EmbeddingProvider, "_post_with_retry", no_network)
    settings = Settings(
        _env_file=None,
        ANTHROPIC_API_KEY="test-key",
        OPENAI_API_KEY="test-key",
        agent_id=agent_id,
        schedule_enabled=False,
        dag_enabled=False,
        mcp_enabled=False,
        telegram_bot_token="",
        telegram_chat_id="",
        email_user="nous@example.com",
        email_allowlist="friend@example.com",
        owner_route_secret_sha256=settings_for().owner_route_secret_sha256,
        goals_tick_seconds=3600,
        **GRANTS,
    )
    assert settings.goal_tool_grants_enabled is True
    seen: dict = {}

    async def scenario() -> None:
        app = main.build_app(settings)
        try:
            async with app.router.lifespan_context(app):
                components = app.state.components
                view = components["goal_tool_grants"]
                runner = components["runner"]
                seen.update(
                    view=view,
                    fresh=view.fresh(),
                    driver_view=components["goal_driver"].tool_grants,
                    lift=runner._goal_lift,
                    note=runner._goal_note_use,
                    started=list(driver_starts),
                )
                store = GoalStore(components["database"], agent_id, settings)
                goal = await store.create(
                    title="Send the notes",
                    objective="Mail the summary",
                    done_when="Sent",
                    proposed_by="owner",
                    origin_channel="telegram:4242",
                )
                transport = httpx.ASGITransport(app=app)
                async with httpx.AsyncClient(transport=transport, base_url="http://nous") as client:
                    path = f"/goals/{goal.id}/tools"
                    seen["read"] = (await client.get(path, headers=OWNER)).json()
                    put = await client.put(f"{path}/send_email", json={"confirm": True}, headers=OWNER)
                    seen["put"] = (put.status_code, put.json())
            seen["after"] = (runner._goal_lift, runner._goal_note_use)
        finally:
            continuation.set_goal_report_channel(None)
            database = Database(settings)
            try:
                async with database.session() as session:
                    await session.execute(text("DELETE FROM nous_system.agents WHERE id = :a"), {"a": agent_id})
                    await session.commit()
            except Exception:
                pass  # rows of a test agent may stay; nothing reads them
            finally:
                await database.disconnect()

    _run(scenario, seconds=120)
    view = seen["view"]
    assert isinstance(view, GoalToolGrants) and seen["driver_view"] is view
    assert seen["fresh"] is True and seen["started"] == [True]  # read before the driver's start
    assert seen["lift"] == view.lifts and seen["note"] == view.note_used
    assert seen["read"]["grants_enabled"] is True
    assert "send_email" in {row["tool"] for row in seen["read"]["available"]}  # the dispatcher reached the routes
    status, body = seen["put"]
    assert status == 200 and body["changed"] is True and "reload" not in body
    assert seen["after"] == (runner_module._no_goal_lift, runner_module._no_goal_note_use)  # unbound at shutdown


# ---- a session the real driver starts ---------------------------------------------------------------------------


def _runner_over(names: tuple[str, ...]) -> AgentRunner:
    dispatcher = ToolDispatcher()
    for name in names:
        dispatcher.register(name, MagicMock(), {"type": "object", "properties": {}})
    runner = AgentRunner(MagicMock(), None, MagicMock(), Settings(_env_file=None, ANTHROPIC_API_KEY="test-key"))
    runner.set_dispatcher(dispatcher)
    return runner


def _offered(runner: AgentRunner, ctx: ExecutionContext) -> set[str]:
    is_subtask = ctx.kind in ("subtask", "dag_node")
    tools = runner._offered_tools(ctx, "task", is_subtask=is_subtask, tool_filter=None, refuse_active=False)
    return {tool["name"] for tool in tools}


async def test_the_first_task_and_the_decision_turn_of_a_started_session_are_offered_the_grant(env_factory):  # noqa: F811
    w = await _world(env_factory, **GRANTS)
    goal = await _active(w)
    await w.store.grant_tool(goal.id, "send_email", None, actor="owner", proof="owner")
    tool_guard.install(ToolGuard())
    view = GoalToolGrants(w.db, w.env.settings)
    assert await view.refresh()
    w.driver.tool_grants = view
    runner = _runner_over(("send_email", "send_file", "web_search"))
    runner.set_goal_lift(view.lifts, view.note_used)

    root, session = await _started(w, goal)  # the start reads the new root; no tick has run
    async with w.db.session() as s:
        subtask = (await s.execute(select(Subtask).where(Subtask.id == session.first_subtask_id))).scalar_one()
    first_task = ExecutionContext.for_subtask(subtask, f"subtask-{subtask.id}")
    assert (first_task.kind, first_task.authority, first_task.root_intention_id) == (
        "subtask",
        "internal_only",
        root.id,
    )
    assert "send_email" in _offered(runner, first_task) and "send_file" not in _offered(runner, first_task)
    assert runner._authorize_tool_call(first_task, "send_email", frozenset({"send_email"}), "s", {}) is None

    decision = ExecutionContext(
        kind="continuation", authority="internal_only", intention_id=root.id, root_intention_id=root.id
    )
    assert "send_email" in _offered(runner, decision)

    # The first use's harness row is not one of the chain's notes: the session can still write all of its own.
    await asyncio.gather(*view._tasks)
    async with w.db.session() as s:
        rows = list(
            (
                await s.execute(
                    select(GoalJournal).where(GoalJournal.goal_id == goal.id, GoalJournal.kind == "progress")
                )
            ).scalars()
        )
    assert [(row.actor, row.evidence) for row in rows] == [("harness", ["grant-used:send_email"])]
    goal_note, _goal_plan = goal_tools(w, goal, session)
    for n in range(NOTES_PER_SESSION):
        answer, is_error = await goal_note(text=f"note {n}", kind="progress")
        assert is_error is False, answer


# ---- a real turn of each kind a grant reaches -------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["subtask", "continuation", "dag_node"])
async def test_a_real_turn_of_each_kind_calls_the_granted_tool_and_the_ledger_records_it(runner_env, kind):  # noqa: F811
    env = await runner_env([use("send_email", **SEND_EMAIL_ARGS)], [say("Sent.")], telegram_chat_id="")
    calls = register_send_email(env)
    env.runner.set_ledger_store(LedgerStore(env.db, env.agent))
    store = GoalStore(env.db, env.agent, env.settings)
    goal = await store.approve(
        (
            await store.create(
                title="Send the notes",
                objective="Mail the summary",
                done_when="Sent",
                proposed_by="owner",
                origin_channel="telegram:4242",
            )
        ).id,
        actor="owner",
    )
    root = uuid.uuid4()
    async with env.db.session() as s:
        s.add(
            GoalSession(
                agent_id=env.agent, goal_id=goal.id, n=1, trigger="timer", first_subtask_id=uuid.uuid4(), root_id=root
            )
        )
        await s.commit()
    await store.grant_tool(goal.id, "send_email", None, actor="owner", proof="owner")
    tool_guard.install(ToolGuard())
    view = GoalToolGrants(env.db, env.settings)
    await view.refresh()
    env.runner.set_goal_lift(view.lifts, view.note_used)
    session_id = {"subtask": "subtask-81", "continuation": "intent-81", "dag_node": "subtask-82"}[kind]
    extra = {"dag_id": uuid.uuid4(), "dag_node_id": uuid.uuid4()} if kind == "dag_node" else {}
    ctx = ExecutionContext(
        kind=kind,
        authority="internal_only",
        session_id=session_id,
        intention_id=uuid.uuid4(),
        root_intention_id=root,
        **extra,
    )
    await env.runner.run_turn(
        session_id, "send the note", is_background=True, is_subtask=kind != "continuation", context=ctx
    )
    assert calls == [SEND_EMAIL_ARGS]
    await asyncio.gather(*env.runner._ledger_pending_tasks, return_exceptions=True)
    async with env.db.session() as s:
        rows = list(
            (
                await s.execute(
                    select(ExecutionLedgerEntry).where(
                        ExecutionLedgerEntry.agent_id == env.agent, ExecutionLedgerEntry.tool_name == "send_email"
                    )
                )
            ).scalars()
        )
    assert [(row.context_kind, row.status) for row in rows] == [(kind, "success")]
    await asyncio.gather(*view._tasks)
