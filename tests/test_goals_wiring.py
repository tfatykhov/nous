"""F114 goals (PR 1c): main.py builds one goal driver, binds the chain's two hooks and runs the driver's loop, only
with ``NOUS_GOALS_ENABLED`` on; with it off nothing of the chain is built, bound, imported, queried or scheduled.

``GET /goals/options`` says whether the driver's loop runs, from the app's component, not from a constant.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import subprocess
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from f099_support import CONT, env_factory, runner_env  # noqa: F401
from goal_routes_support import OWNER, send, settings_for
from goals_support import GOALS, reset_goal_report_channel  # noqa: F401
from sqlalchemy import text
from starlette.applications import Starlette
from test_f099_phase2c_parity import NoDatabase, Untouchable
from test_work_view_retention import _run

import nous.main as main
from nous.api.goal_routes import build_goal_routes
from nous.api.owner_guard import OwnerGuard
from nous.brain import continuation
from nous.brain.embeddings import EmbeddingProvider
from nous.config import Settings
from nous.goals import driver as driver_module
from nous.goals.chain import GoalChain, report_channel
from nous.goals.driver import GoalDriver
from nous.goals.store import GoalStore
from nous.handlers.continuation_runner import ContinuationRunner
from nous.heartbeat.runner import HeartbeatRunner
from nous.storage.database import Database

ROOT = Path(__file__).resolve().parents[1]
CHAIN_MODULES = ("nous.goals.chain", "nous.goals.tools", "nous.goals.evidence")


def _forbid_driver(monkeypatch) -> None:
    def refuse(*args, **kwargs):
        raise AssertionError("a GoalDriver was built")

    monkeypatch.setattr(driver_module.GoalDriver, "__init__", refuse)


# ---- the build ---------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "flags",
    [
        {},  # the default: off
        {**CONT, "subtask_enabled": True},  # everything a goal needs, but not the flag
        {"goals_enabled": True},  # the flag without what it needs: the validator turns it off
    ],
    ids=["default", "dependencies_only", "flag_without_dependencies"],
)
async def test_with_goals_off_nothing_is_built_bound_or_read(monkeypatch, flags):
    _forbid_driver(monkeypatch)
    settings = Settings(_env_file=None, **flags)
    assert settings.goals_enabled is False
    db = NoDatabase()
    runner = ContinuationRunner(
        database=db, settings=settings, runner=Untouchable(), heart=Untouchable(), brain=Untouchable()
    )
    built = main._build_goals(settings, database=db, heart=Untouchable(), continuation_runner=runner)
    assert built is None
    assert continuation._goal_report_channel is None and runner._goal_turns is None and runner._goals is None
    assert db.sessions == 0


async def test_with_goals_on_and_no_continuation_runner_nothing_is_built(monkeypatch):
    _forbid_driver(monkeypatch)
    settings = Settings(_env_file=None, **GOALS)
    db = NoDatabase()
    assert main._build_goals(settings, database=db, heart=Untouchable(), continuation_runner=None) is None
    assert continuation._goal_report_channel is None and db.sessions == 0


@pytest.mark.postgres_only
async def test_with_goals_on_one_driver_is_built_and_both_hooks_are_bound(runner_env):  # noqa: F811
    env = await runner_env(**GOALS)
    runner = ContinuationRunner(
        database=env.db, settings=env.settings, runner=env.runner, heart=env.heart, brain=env.brain
    )
    built = main._build_goals(env.settings, database=env.db, heart=env.heart, continuation_runner=runner)
    assert isinstance(built, GoalDriver) and not built.running  # its loop starts after the runner's
    assert built._runner is runner and built._heart is env.heart
    assert continuation._goal_report_channel is report_channel
    assert runner._goal_turns.__func__ is GoalChain.turn_context and runner._goal_turns.__self__._store.agent_id == (
        env.agent
    )
    assert runner._goals is built  # the owner's priority in _launch asks this driver which roots are goals'


def test_create_components_binds_the_hooks_before_anything_runs_and_starts_the_loop_last():  # PIN
    """The hooks are bound right after the runner is built, before the reconciler and the worker pool can write a
    goal chain's report and before the runner's loop runs a turn; the driver's loop starts after the runner's."""
    source = inspect.getsource(main.create_components)
    built = source.index("continuation_runner = await _build_continuation_runner(")
    goals = source.index("goal_driver = _build_goals(")
    assert built < goals < source.index("build_reconciler(") < source.index("SubtaskWorkerPool(")
    runner_start = source.index("await continuation_runner.start()")
    assert goals < runner_start < source.index("await goal_driver.start()")
    assert '"goal_driver": goal_driver' in source


# ---- the loop and the shutdown -----------------------------------------------------------------------------------


@pytest.mark.postgres_only
async def test_running_is_true_only_while_the_loop_runs(runner_env):  # noqa: F811
    env = await runner_env(**{**GOALS, "goals_tick_seconds": 3600})
    store = GoalStore(env.db, env.agent, env.settings)
    driver = GoalDriver(env.db, env.settings, env.heart, store, SimpleNamespace(wake=lambda: None))
    assert driver.running is False
    await driver.start()
    assert driver.running is True
    assert [t for t in asyncio.all_tasks() if t.get_name() == "goal-driver"]
    await driver.stop()
    assert driver.running is False
    assert not [t for t in asyncio.all_tasks() if t.get_name() == "goal-driver" and not t.done()]


async def test_the_driver_stops_before_the_runner_and_every_hook_is_cleared():
    order: list[str] = []

    class Stopping:
        def __init__(self, name: str) -> None:
            self.name = name
            self.goals, self.turns = object(), object()

        async def stop(self) -> None:
            order.append(self.name)

        def set_goals(self, goals) -> None:
            self.goals = goals

        def set_goal_turns(self, turns) -> None:
            self.turns = turns

    continuation.set_goal_report_channel(report_channel)
    runner = Stopping("continuation runner")
    await main.shutdown_components({"goal_driver": Stopping("goal driver"), "continuation_runner": runner})
    assert order == ["goal driver", "continuation runner"]
    assert continuation._goal_report_channel is None and runner.goals is None and runner.turns is None


@pytest.mark.postgres_only
def test_create_components_with_goals_off_builds_no_driver_and_schedules_no_goal_task(monkeypatch):
    """A full boot on the default flags: no driver, no hook, no goal task, and the options say no driver runs."""
    from tests.test_model_call_retention import _boot

    _forbid_driver(monkeypatch)
    seen: dict = {}

    async def checks(components: dict, settings) -> None:
        try:
            seen["driver"] = components["goal_driver"]
            seen["hook"] = continuation._goal_report_channel
            seen["runner"] = components["continuation_runner"]
            seen["tasks"] = sorted(t.get_name() for t in asyncio.all_tasks() if not t.done())
        finally:
            await main.shutdown_components(components)

    _boot(monkeypatch, checks)
    assert seen["driver"] is None and seen["hook"] is None and seen["runner"] is None  # continuation is off too
    assert "goal-driver" not in seen["tasks"]


# ---- imports -----------------------------------------------------------------------------------------------------


def test_starting_the_app_imports_none_of_the_chain():
    """Importing ``nous.main`` (and with it the routes) loads no module of the chain: those are imported only inside
    the goals block of ``create_components``. A fresh interpreter, so no other test's import counts."""
    probe = "import sys, nous.main; print(sorted(m for m in sys.modules if m.startswith('nous.goals')))"
    out = subprocess.run(
        [sys.executable, "-I", "-c", f"import sys; sys.path.insert(0, {str(ROOT)!r}); {probe}"],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=ROOT,
    )
    assert out.returncode == 0, out.stderr[-2000:]
    loaded = ast.literal_eval(out.stdout.strip().splitlines()[-1])
    assert not set(CHAIN_MODULES) & set(loaded)


@pytest.mark.parametrize("path", ["nous/handlers/continuation_runner.py", "nous/brain/continuation.py"])
def test_the_runner_and_the_store_import_nothing_of_the_goals(path):
    tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
    names = [
        node.module if isinstance(node, ast.ImportFrom) else alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import | ast.ImportFrom)
        for alias in (node.names if isinstance(node, ast.Import) else [node])
    ]
    assert not [name for name in names if name and name.startswith("nous.goals")]


# ---- the options -------------------------------------------------------------------------------------------------


class _Driver:
    def __init__(self, running: bool) -> None:
        self.running = running


@pytest.mark.parametrize(("driver", "running"), [(None, False), (_Driver(False), False), (_Driver(True), True)])
async def test_options_say_whether_the_drivers_loop_runs(driver, running):
    settings = settings_for()
    store = GoalStore(NoDatabase(), settings.agent_id, settings)
    routes = build_goal_routes(
        database=NoDatabase(), settings=settings, goals=store, guard=OwnerGuard(settings), driver=driver
    )
    response = await send(Starlette(routes=routes), "GET", "/goals/options", headers=OWNER)
    assert response.status_code == 200 and response.json()["driver_running"] is running


async def test_options_read_the_driver_when_asked_not_when_built():
    """The app builds its routes before the lifespan builds the driver: the routes hold the component's lazy proxy,
    so the answer follows the app's component."""
    components: dict = {}
    settings = settings_for()
    store = GoalStore(NoDatabase(), settings.agent_id, settings)
    proxy = main._lazy_component(components, "goal_driver")
    app = Starlette(
        routes=build_goal_routes(
            database=NoDatabase(), settings=settings, goals=store, guard=OwnerGuard(settings), driver=proxy
        )
    )
    assert (await send(app, "GET", "/goals/options", headers=OWNER)).json()["driver_running"] is False
    components["goal_driver"] = _Driver(True)
    assert (await send(app, "GET", "/goals/options", headers=OWNER)).json()["driver_running"] is True


@pytest.mark.postgres_only
def test_a_boot_with_goals_on_runs_one_driver_and_its_shutdown_stops_it(monkeypatch):
    """The app's own lifespan with the flag and everything it needs on: one driver is built and its loop runs, both
    hooks are bound, GET /goals/options says the driver runs; after shutdown the loop has stopped and the report hook
    is cleared."""
    agent_id = f"test-goals-boot-{uuid.uuid4().hex[:8]}"

    async def already_migrated(engine) -> None:
        async with engine.begin() as connection:
            await connection.execute(
                text("INSERT INTO nous_system.agents (id, name, config) VALUES (:id, 'x', '{}')"), {"id": agent_id}
            )

    async def never_start(self) -> None:
        return None

    async def no_network(self, payload, *, batch=False):
        raise httpx.ConnectError("no network in this test")

    monkeypatch.setattr(main, "run_migrations", already_migrated)
    monkeypatch.setattr(HeartbeatRunner, "start", never_start)
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
        **GOALS,
    )
    assert settings.goals_enabled is True
    seen: dict = {}

    def goal_tasks() -> list[str]:
        return [t.get_name() for t in asyncio.all_tasks() if t.get_name() == "goal-driver" and not t.done()]

    async def scenario() -> None:
        app = main.build_app(settings)
        try:
            async with app.router.lifespan_context(app):
                components = app.state.components
                driver = components["goal_driver"]
                seen.update(
                    driver=driver,
                    running=driver.running,
                    tasks=goal_tasks(),
                    hook=continuation._goal_report_channel,
                    turns=components["continuation_runner"]._goal_turns,
                    goals=components["continuation_runner"]._goals,
                    runner=components["continuation_runner"],
                )
                transport = httpx.ASGITransport(app=app)
                async with httpx.AsyncClient(transport=transport, base_url="http://nous") as client:
                    seen["options"] = (await client.get("/goals/options", headers=OWNER)).json()
            seen.update(
                after_running=driver.running, after_tasks=goal_tasks(), after_hook=continuation._goal_report_channel
            )
        finally:
            continuation.set_goal_report_channel(None)
            database = Database(settings)
            try:
                async with database.session() as session:
                    for statement in (
                        "DELETE FROM nous_system.model_call_log WHERE agent_id = :a",
                        "DELETE FROM nous_system.model_text_block WHERE agent_id = :a",
                        "DELETE FROM heart.rubric_versions WHERE agent_id = :a",
                        "DELETE FROM nous_system.agents WHERE id = :a",
                    ):
                        await session.execute(text(statement), {"a": agent_id})
                    await session.commit()
            except Exception:
                pass  # rows of a test agent may stay; nothing reads them
            finally:
                await database.disconnect()

    _run(scenario, seconds=120)
    assert isinstance(seen["driver"], GoalDriver) and seen["running"] is True and seen["tasks"] == ["goal-driver"]
    assert seen["hook"] is report_channel and seen["turns"].__func__ is GoalChain.turn_context
    assert seen["goals"] is seen["driver"]
    assert seen["runner"]._goals is None and seen["runner"]._goal_turns is None  # cleared at the shutdown
    assert seen["options"]["enabled"] is True and seen["options"]["driver_running"] is True
    assert seen["after_running"] is False and seen["after_tasks"] == [] and seen["after_hook"] is None
