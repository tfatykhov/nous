"""Goals that may run any command, at the real app's start: with the flag on, the lifespan starts the goals' view's
own loop after the goal driver and stops it at shutdown; with the flag off the view has no loop."""

from __future__ import annotations

import uuid

import httpx
import pytest
from goal_routes_support import settings_for
from goals_support import reset_goal_report_channel  # noqa: F401
from sqlalchemy import text
from test_goal_tool_grants_boot import GRANTS
from test_work_view_retention import _run

import nous.main as main
from nous.brain import continuation
from nous.brain.embeddings import EmbeddingProvider
from nous.config import Settings
from nous.goals.driver import GoalDriver
from nous.heartbeat.runner import HeartbeatRunner
from nous.storage.database import Database

pytestmark = pytest.mark.postgres_only

COMMANDS = {
    **GRANTS,
    "owner_entry_mode": "enforce",
    "mcp_client_ui_secret_sha256": "cd" * 32,
    "goal_commands_enabled": True,
}


@pytest.mark.parametrize("on", [True, False], ids=["flag_on", "flag_off"])
def test_the_start_runs_the_views_loop_only_with_the_flag_and_the_shutdown_stops_it(monkeypatch, on):
    agent_id = f"test-commands-boot-{uuid.uuid4().hex[:8]}"

    async def already_migrated(engine) -> None:
        async with engine.begin() as connection:
            await connection.execute(
                text("INSERT INTO nous_system.agents (id, name, config) VALUES (:id, 'x', '{}')"), {"id": agent_id}
            )

    async def never_start(self) -> None:
        return None

    async def no_network(self, payload, *, batch=False):
        raise httpx.ConnectError("no network in this test")

    order: list[str] = []

    async def driver_started(self) -> None:
        order.append("driver")

    monkeypatch.setattr(main, "run_migrations", already_migrated)
    monkeypatch.setattr(HeartbeatRunner, "start", never_start)
    monkeypatch.setattr(GoalDriver, "start", driver_started)
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
        owner_route_secret_sha256=settings_for().owner_route_secret_sha256,
        goals_tick_seconds=3600,
        **{**COMMANDS, "goal_commands_enabled": on},
    )
    assert settings.goal_commands_enabled is on
    seen: dict = {}

    async def scenario() -> None:
        app = main.build_app(settings)
        try:
            async with app.router.lifespan_context(app):
                view = app.state.components["goal_tool_grants"]
                task = view._loop_task
                seen.update(
                    view=view,
                    commands_on=view.commands_on,
                    order=list(order),
                    task=None if task is None else (task.get_name(), task.done()),
                )
            seen["after"] = seen["view"]._loop_task
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
    assert seen["commands_on"] is on and seen["order"] == ["driver"]
    assert seen["task"] == (("goal-tool-grants-view", False) if on else None)
    assert seen["after"] is None
