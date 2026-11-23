"""F118: main.py builds, starts and closes the outside judge only in shadow; off, nothing of it is built or imported.

The judge is never a tool: with the judge in shadow the dispatcher's tools, their schemas and the tools a turn is
offered are the same as with it off. Only ``main.py`` (the build), ``nous.api.tools`` (the accessor, for the
submit) and ``nous.api.rest`` (the read route) refer to the package.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import logging
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text

import nous.main as main
from nous import judge as judge_slot
from nous.config import Settings
from nous.judge import slot
from nous.judge.model_judge import BackgroundModelJudge
from nous.judge.service import OutsideJudge

ROOT = Path(__file__).resolve().parents[1]


def _imports_judge(path: Path) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.ImportFrom)
            and node.module
            and (
                node.module == "nous.judge"
                or node.module.startswith("nous.judge.")
                or (node.module == "nous" and any(alias.name == "judge" for alias in node.names))
            )
        ):
            return True
        if isinstance(node, ast.Import) and any(a.name.startswith("nous.judge") for a in node.names):
            return True
    return False


def test_only_main_tools_and_rest_refer_to_the_judge():
    users = {
        str(path.relative_to(ROOT)).replace("\\", "/")
        for path in (ROOT / "nous").rglob("*.py")
        if "judge" not in path.parts and _imports_judge(path)
    }
    assert users == {"nous/main.py", "nous/api/tools.py", "nous/api/rest.py"}


def _modules_after(code: str) -> list[str]:
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, cwd=ROOT, timeout=120, check=True
    )
    return sorted(line for line in result.stdout.split() if line)


def test_the_tools_module_loads_only_the_judges_accessor():
    loaded = _modules_after(
        "import sys, nous.api.tools\nprint(' '.join(m for m in sys.modules if m.startswith('nous.judge')))"
    )
    assert loaded == ["nous.judge"]


def test_with_the_judge_off_the_build_returns_none_and_imports_nothing_of_it():
    loaded = _modules_after(
        "import sys\nimport nous.main as m\nfrom nous.config import Settings\n"
        "assert m._build_outside_judge(Settings(_env_file=None), database=None, api_client=None) is None\n"
        "print(' '.join(x for x in sys.modules if x.startswith('nous.judge')))"
    )
    assert loaded == ["nous.judge"]


def test_in_shadow_the_build_registers_the_built_in_provider_and_builds_a_service(monkeypatch):
    monkeypatch.setattr(slot, "PROVIDERS", {})
    settings = Settings(_env_file=None, outside_judge_mode="shadow", outside_judge_model="claude-haiku-5-5")
    client = object()
    built = main._build_outside_judge(settings, database=object(), api_client=client)
    assert isinstance(built, OutsideJudge) and isinstance(built.judge, BackgroundModelJudge)
    assert (built.judge.name, built.judge.model, built.judge._client) == (
        "background_model",
        "claude-haiku-5-5",
        client,
    )
    assert set(slot.PROVIDERS) == {"background_model"}
    assert judge_slot.current() is None  # create_components installs it once it has started


def test_create_components_wires_the_judge_outside_the_bus_block_and_closes_it_before_the_client():  # PIN
    build = inspect.getsource(main.create_components)
    assert build.count("_start_outside_judge(") == 1 and "_build_outside_judge(" not in build
    at = build.index("_start_outside_judge(")
    # Not inside `if bus is not None:`: the line's indentation is the function body's.
    line = build[build.rindex("\n", 0, at) + 1 : at]
    assert line.strip() == "outside_judge = await" and len(line) - len(line.lstrip()) == 4
    stop = inspect.getsource(main.shutdown_components)
    assert stop.index('components.get("outside_judge")') < stop.index('components.get("api_client")')
    assert stop.index('components.get("outside_judge")') < stop.index('components.get("brain")')


# ---- start-up fails open ------------------------------------------------------------------------------------------


async def test_a_build_that_raises_leaves_the_process_without_a_judge(monkeypatch, caplog):
    def broken(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("no judge today")

    monkeypatch.setattr(main, "_build_outside_judge", broken)
    settings = Settings(_env_file=None, outside_judge_mode="shadow")
    with caplog.at_level(logging.WARNING, logger="nous.main"):
        assert await main._start_outside_judge(settings, database=object(), api_client=object()) is None
    assert judge_slot.current() is None
    [line] = [r.getMessage() for r in caplog.records if "outside judge" in r.getMessage()]
    assert "RuntimeError" in line and "no judge today" not in line


class _NoModelJudge:
    name, version = "no_model", "nm-1"

    async def grade(  # pragma: no cover - never called here
        self, record: Any, *, timeout: float, max_calls: int | None = None
    ) -> Any:
        raise AssertionError


async def test_a_judge_without_a_model_still_starts(monkeypatch, caplog):
    settings = Settings(_env_file=None, outside_judge_mode="shadow")
    service = OutsideJudge(object(), settings, _NoModelJudge())
    monkeypatch.setattr(main, "_build_outside_judge", lambda *args, **kwargs: service)
    with caplog.at_level(logging.INFO, logger="nous.main"):
        started = await main._start_outside_judge(settings, database=object(), api_client=object())
    try:
        assert started is service and judge_slot.current() is service
        assert service.runtime()["worker_alive"] is True
        assert [r for r in caplog.records if "runs in shadow" in r.getMessage()]
    finally:
        await service.close()


async def test_a_start_that_raises_closes_what_it_built_and_installs_nothing(monkeypatch, caplog):
    settings = Settings(_env_file=None, outside_judge_mode="shadow")
    service = OutsideJudge(object(), settings, _NoModelJudge())
    monkeypatch.setattr(main, "_build_outside_judge", lambda *args, **kwargs: service)

    def install_fails(value: Any) -> None:
        raise ValueError("cannot install")

    monkeypatch.setattr(judge_slot, "set_current", install_fails)
    with caplog.at_level(logging.WARNING, logger="nous.main"):
        assert await main._start_outside_judge(settings, database=object(), api_client=object()) is None
    assert judge_slot.current() is None
    assert service.runtime()["worker_alive"] is False  # closed: no worker left behind
    assert any("ValueError" in r.getMessage() for r in caplog.records)


async def test_with_the_judge_off_start_builds_nothing():
    assert await main._start_outside_judge(Settings(_env_file=None), database=None, api_client=None) is None
    assert judge_slot.current() is None


# ---- the real build ---------------------------------------------------------------------------------------------


def _run(scenario: Any, seconds: float = 180.0) -> None:
    import threading

    raised: list[BaseException] = []

    def target() -> None:
        try:
            asyncio.run(scenario())
        except BaseException as exc:  # noqa: BLE001 - raised again below, in the test's own thread
            raised.append(exc)

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(seconds)
    if thread.is_alive():
        pytest.fail(f"the scenario had not finished after {seconds:.0f}s")
    if raised:
        raise raised[0]


def _boot_settings(agent_id: str, mode: str, **over: Any) -> Settings:
    return Settings(
        _env_file=None,
        ANTHROPIC_API_KEY="test-key",
        agent_id=agent_id,
        heartbeat_enabled=False,
        subtask_enabled=False,
        schedule_enabled=False,
        dag_enabled=False,
        mcp_enabled=False,
        outside_judge_mode=mode,
        **over,
    )


@pytest.mark.postgres_only
def test_in_shadow_the_tools_are_those_of_the_judge_off_and_the_service_runs_until_shutdown(monkeypatch):
    from nous.api.execution_context import ExecutionContext

    agents = {mode: f"test-f118-boot-{mode}-{uuid.uuid4().hex[:6]}" for mode in ("off", "shadow")}

    async def already_migrated(engine: Any) -> None:
        async with engine.begin() as connection:
            for agent_id in agents.values():
                await connection.execute(
                    text(
                        "INSERT INTO nous_system.agents (id, name, config) VALUES (:id, 'x', '{}') "
                        "ON CONFLICT (id) DO NOTHING"
                    ),
                    {"id": agent_id},
                )

    monkeypatch.setattr(main, "run_migrations", already_migrated)
    seen: dict[str, Any] = {}

    async def scenario() -> None:
        for mode, agent_id in agents.items():
            components = await main.create_components(_boot_settings(agent_id, mode))
            try:
                dispatcher, runner = components["dispatcher"], components["runner"]
                ctx = ExecutionContext(kind="interactive", session_id=f"boot-{mode}")
                seen[mode] = {
                    "definitions": dispatcher.tool_definitions(),
                    "offered": runner._offered_tools(
                        ctx, "task", is_subtask=False, tool_filter=None, refuse_active=False
                    ),
                    "current": judge_slot.current(),
                    "component": components.get("outside_judge"),
                }
            finally:
                await main.shutdown_components(components)
            seen[mode]["after"] = judge_slot.current()

    try:
        _run(scenario)
    finally:

        async def cleanup() -> None:
            from nous.storage.database import Database

            database = Database(_boot_settings(agents["off"], "off"))
            await database.connect()
            try:
                async with database.session() as session:
                    for agent_id in agents.values():
                        await session.execute(
                            text("DELETE FROM heart.rubric_versions WHERE agent_id = :a"), {"a": agent_id}
                        )
                        await session.execute(text("DELETE FROM nous_system.agents WHERE id = :a"), {"a": agent_id})
                    await session.commit()
            finally:
                await database.disconnect()

        asyncio.run(cleanup())

    off, shadow = seen["off"], seen["shadow"]
    assert off["current"] is None and off["component"] is None
    assert isinstance(shadow["current"], OutsideJudge) and shadow["component"] is shadow["current"]
    assert shadow["after"] is None and off["after"] is None
    # Closed at shutdown: it takes no more work.
    assert (
        shadow["component"].submit_decision(
            uuid.uuid4(), grade="success", note=None, grader="agent", resolved_at=datetime.now(UTC)
        )
        is False
    )
    assert shadow["definitions"] == off["definitions"]
    assert shadow["offered"] == off["offered"]
    assert not [d for d in shadow["definitions"] if "judge" in d["name"]]


# ---- the second judge -----------------------------------------------------------------------------------------------

JEV_KEY = "ts-wiring-" + "W3" * 16


@pytest.fixture(autouse=True)
def _no_request_leaves_the_process(monkeypatch):
    """The judges built here are never given a decision; a request through a real transport fails the test."""
    import httpx

    async def refuse(self: Any, request: Any) -> Any:
        raise AssertionError(f"a real request was attempted to {request.url.host}")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", refuse)


def _shadow(**over: Any) -> Settings:
    return Settings(_env_file=None, outside_judge_mode="shadow", outside_judge_model="claude-haiku-5-5", **over)


def test_with_no_second_judge_the_running_judge_is_pr_1s_and_nothing_of_the_second_is_imported():
    loaded = _modules_after(
        "import asyncio, sys\nimport nous.main as m\nfrom nous import judge\nfrom nous.config import Settings\n"
        "from nous.judge.service import OutsideJudge\n"
        "async def run():\n"
        "    s = Settings(_env_file=None, outside_judge_mode='shadow', outside_judge_model='claude-haiku-5-5')\n"
        "    service = await m._start_outside_judge(s, database=object(), api_client=object())\n"
        "    assert type(service) is OutsideJudge and judge.current() is service\n"
        "    assert set(service.runtime()) == {'queued', 'dropped', 'over_cap', 'worker_alive'}\n"
        "    await service.close()\n"
        "    off = Settings(_env_file=None, outside_judge_second='jev', TYPESAFE_API_KEY='k-off')\n"
        "    assert await m._start_outside_judge(off, database=object(), api_client=object()) is None\n"
        "asyncio.run(run())\n"
        "print(' '.join(x for x in sys.modules if x.startswith('nous.judge')))"
    )
    assert "nous.judge.jev_judge" not in loaded and "nous.judge.service" in loaded


async def test_with_the_second_judge_chosen_but_no_key_one_warning_and_the_first_judge_alone(caplog):
    with caplog.at_level(logging.WARNING, logger="nous.main"):
        service = await main._start_outside_judge(
            _shadow(outside_judge_second="jev"), database=object(), api_client=object()
        )
    try:
        assert type(service) is OutsideJudge and judge_slot.current() is service
        assert service.runtime()["worker_alive"] is True
        [line] = [r.getMessage() for r in caplog.records if r.name == "nous.main"]
        assert "TYPESAFE_API_KEY" in line
    finally:
        await service.close()
        judge_slot.set_current(None)


async def test_a_second_judge_that_fails_to_build_leaves_the_first_judge_running(monkeypatch, caplog):
    def broken(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("no second judge today")

    monkeypatch.setattr(main, "_build_second_judge", broken)
    settings = _shadow(outside_judge_second="jev", TYPESAFE_API_KEY=JEV_KEY)
    with caplog.at_level(logging.WARNING, logger="nous.main"):
        service = await main._start_outside_judge(settings, database=object(), api_client=object())
    try:
        assert type(service) is OutsideJudge and judge_slot.current() is service
        assert service.runtime()["worker_alive"] is True
        [line] = [r.getMessage() for r in caplog.records if r.name == "nous.main"]
        assert "RuntimeError" in line and "no second judge today" not in line
    finally:
        await service.close()
        judge_slot.set_current(None)


class _FailsToStart(OutsideJudge):
    def start(self) -> None:
        raise ValueError("cannot start")


async def test_a_second_judge_that_fails_to_start_is_closed_and_the_first_judge_runs(monkeypatch, caplog):
    built: list[OutsideJudge] = []

    def fails_to_start(settings: Settings, *, database: Any) -> OutsideJudge:
        built.append(_FailsToStart(database, settings, _NoModelJudge()))
        return built[-1]

    monkeypatch.setattr(main, "_build_second_judge", fails_to_start)
    settings = _shadow(outside_judge_second="jev", TYPESAFE_API_KEY=JEV_KEY)
    with caplog.at_level(logging.WARNING, logger="nous.main"):
        service = await main._start_outside_judge(settings, database=object(), api_client=object())
    try:
        assert type(service) is OutsideJudge and judge_slot.current() is service
        assert service.runtime()["worker_alive"] is True
        [second] = built
        closed = second.submit_decision(
            uuid.uuid4(), grade="success", note=None, grader="agent", resolved_at=datetime.now(UTC)
        )
        assert closed is False
        assert any("ValueError" in r.getMessage() for r in caplog.records if r.name == "nous.main")
    finally:
        await service.close()
        judge_slot.set_current(None)


async def test_with_a_key_both_judges_run_side_by_side(caplog):
    from nous.judge.jev_judge import JevJudge
    from nous.judge.service import JudgeSet

    settings = _shadow(outside_judge_second="jev", TYPESAFE_API_KEY=JEV_KEY, outside_judge_daily_calls=9)
    assert settings.typesafe_api_key == JEV_KEY
    with caplog.at_level(logging.INFO, logger="nous.main"):
        service = await main._start_outside_judge(settings, database=object(), api_client=object())
    try:
        assert isinstance(service, JudgeSet) and judge_slot.current() is service
        assert isinstance(service.judge, BackgroundModelJudge)
        assert isinstance(service.second.judge, JevJudge) and service.second.judge.model == "jev-1.14.0"
        assert service.second.judge._transport is None  # the real transport: never given a decision here
        assert service.second._timeout == 10.0
        assert service.second.daily_calls == service.primary.daily_calls == 9
        runtime = service.runtime()
        assert runtime["worker_alive"] is True and runtime["second"]["worker_alive"] is True
        lines = [r.getMessage() for r in caplog.records if r.name == "nous.main"]
        assert any("second judge" in line and "jev-1.14.0" in line for line in lines)
        assert JEV_KEY not in "\n".join(lines)
    finally:
        await service.close()
        judge_slot.set_current(None)
    assert service.runtime()["worker_alive"] is False and service.runtime()["second"]["worker_alive"] is False


@pytest.mark.postgres_only
def test_with_two_judges_the_tools_are_those_of_the_judge_off_and_both_run_until_shutdown(monkeypatch):
    from nous.api.execution_context import ExecutionContext
    from nous.judge.service import JudgeSet

    agents = {mode: f"test-f118-boot2-{mode}-{uuid.uuid4().hex[:6]}" for mode in ("off", "two")}

    async def already_migrated(engine: Any) -> None:
        async with engine.begin() as connection:
            for agent_id in agents.values():
                await connection.execute(
                    text(
                        "INSERT INTO nous_system.agents (id, name, config) VALUES (:id, 'x', '{}') "
                        "ON CONFLICT (id) DO NOTHING"
                    ),
                    {"id": agent_id},
                )

    monkeypatch.setattr(main, "run_migrations", already_migrated)
    seen: dict[str, Any] = {}
    boots = {
        "off": _boot_settings(agents["off"], "off"),
        "two": _boot_settings(agents["two"], "shadow", outside_judge_second="jev", TYPESAFE_API_KEY=JEV_KEY),
    }

    async def scenario() -> None:
        for mode, settings in boots.items():
            components = await main.create_components(settings)
            try:
                dispatcher, runner = components["dispatcher"], components["runner"]
                ctx = ExecutionContext(kind="interactive", session_id=f"boot2-{mode}")
                current = judge_slot.current()
                seen[mode] = {
                    "definitions": dispatcher.tool_definitions(),
                    "offered": runner._offered_tools(
                        ctx, "task", is_subtask=False, tool_filter=None, refuse_active=False
                    ),
                    "current": current,
                    "component": components.get("outside_judge"),
                    "runtime": current.runtime() if current is not None else None,
                }
            finally:
                await main.shutdown_components(components)
            seen[mode]["after"] = judge_slot.current()

    try:
        _run(scenario)
    finally:

        async def cleanup() -> None:
            from nous.storage.database import Database

            database = Database(boots["off"])
            await database.connect()
            try:
                async with database.session() as session:
                    for agent_id in agents.values():
                        await session.execute(
                            text("DELETE FROM heart.rubric_versions WHERE agent_id = :a"), {"a": agent_id}
                        )
                        await session.execute(text("DELETE FROM nous_system.agents WHERE id = :a"), {"a": agent_id})
                    await session.commit()
            finally:
                await database.disconnect()

        asyncio.run(cleanup())

    off, two = seen["off"], seen["two"]
    assert isinstance(two["current"], JudgeSet) and two["component"] is two["current"]
    assert two["runtime"]["worker_alive"] is True and two["runtime"]["second"]["worker_alive"] is True
    assert two["after"] is None
    # Closed at shutdown: neither judge takes more work.
    for service in (two["component"].primary, two["component"].second):
        assert (
            service.submit_decision(
                uuid.uuid4(), grade="success", note=None, grader="agent", resolved_at=datetime.now(UTC)
            )
            is False
        )
    assert two["definitions"] == off["definitions"]
    assert two["offered"] == off["offered"]


# ---- the second judge's dashboard settings (PR 3) -----------------------------------------------------------------


def test_with_the_settings_off_the_build_is_the_environments_and_nothing_of_the_settings_is_imported():
    loaded = _modules_after(
        "import asyncio, sys\nimport nous.main as m\nfrom nous import judge\nfrom nous.config import Settings\n"
        "from nous.judge.service import JudgeSet, OutsideJudge\n"
        "async def run():\n"
        "    s = Settings(_env_file=None, outside_judge_mode='shadow', outside_judge_model='claude-haiku-5-5',\n"
        "                 outside_judge_second='jev', TYPESAFE_API_KEY='ts-sub-" + "Z9" * 10 + "')\n"
        "    service = await m._start_outside_judge(s, database=object(), api_client=object())\n"
        "    assert type(service) is JudgeSet and type(service.second) is OutsideJudge\n"
        "    assert await m._start_second_settings(s, service, database=object()) is None\n"
        "    await service.close()\n"
        "asyncio.run(run())\n"
        "print(' '.join(x for x in sys.modules if x.startswith(('nous.judge', 'nous.mcp_client'))))"
    )
    assert "nous.judge.second_settings" not in loaded
    assert not [name for name in loaded if name.startswith("nous.mcp_client")]


def _settings_on(agent_id: str = "test-f118-wiring", **over: Any) -> Settings:
    return _shadow(agent_id=agent_id, outside_judge_settings_enabled=True, **over)


@pytest_asyncio.fixture
async def settings_agent(db):
    agent = f"test-f118-wiring-{uuid.uuid4().hex[:8]}"
    yield agent
    async with db.session() as session:
        await session.execute(text("DELETE FROM nous_system.judge_second_settings WHERE agent_id = :a"), {"a": agent})
        await session.commit()


@pytest.mark.postgres_only
async def test_with_the_settings_on_and_nothing_stored_the_first_judge_runs_in_a_set_with_no_second(db, settings_agent):
    from nous.judge.second_settings import SecondJudgeManager
    from nous.judge.service import JudgeSet

    settings = _settings_on(settings_agent)
    service = await main._start_outside_judge(settings, database=db, api_client=object())
    manager = await main._start_second_settings(settings, service, database=db)
    try:
        assert isinstance(service, JudgeSet) and service.second is None and judge_slot.current() is service
        assert isinstance(manager, SecondJudgeManager) and manager.loaded is True
        assert isinstance(service.judge, BackgroundModelJudge) and service.primary.runtime()["worker_alive"] is True
    finally:
        await manager.close()
        await service.close()
        judge_slot.set_current(None)


@pytest.mark.postgres_only
async def test_with_the_settings_on_and_the_servers_values_the_same_second_judge_as_the_environments_runs(
    db, settings_agent
):
    from nous.judge.jev_judge import JevJudge
    from nous.judge.service import JudgeSet

    on = _settings_on(settings_agent, outside_judge_second="jev", TYPESAFE_API_KEY=JEV_KEY, outside_judge_daily_calls=9)
    off = _shadow(outside_judge_second="jev", TYPESAFE_API_KEY=JEV_KEY, outside_judge_daily_calls=9)
    service = await main._start_outside_judge(on, database=db, api_client=object())
    manager = await main._start_second_settings(on, service, database=db)
    environments = await main._start_outside_judge(off, database=db, api_client=object())
    try:
        assert isinstance(service, JudgeSet) and isinstance(environments, JudgeSet)
        mine, theirs = service.second, environments.second
        assert type(mine) is type(theirs) and isinstance(mine.judge, JevJudge)
        assert (mine.judge._key, mine.judge.model, mine.judge.version, mine._timeout, mine.daily_calls) == (
            theirs.judge._key,
            theirs.judge.model,
            theirs.judge.version,
            theirs._timeout,
            theirs.daily_calls,
        )
        assert mine.runtime()["worker_alive"] is True
    finally:
        await manager.close()
        await service.close()
        await environments.close()
        judge_slot.set_current(None)


@pytest.mark.postgres_only
async def test_with_the_settings_on_and_the_mode_off_no_judge_runs_and_the_settings_allow_the_removal_only(
    db, settings_agent, monkeypatch
):
    from nous.judge import second_settings

    opened: list[Any] = []
    monkeypatch.setattr(second_settings, "open_key", lambda *args: opened.append(args) or (None, None))
    settings = Settings(_env_file=None, agent_id=settings_agent, outside_judge_settings_enabled=True)
    assert await main._start_outside_judge(settings, database=db, api_client=object()) is None
    manager = await main._start_second_settings(settings, None, database=db)
    try:
        assert manager.state()["locked"] == "off" and manager.state()["running"] is False
        assert (await manager.patch({"on": True}, confirm=True, actor="owner")).status == 409
        assert (await manager.remove_key(confirm=False, actor="owner")).status == 200
        assert opened == []
    finally:
        await manager.close()


async def test_with_the_settings_on_and_the_outside_judge_not_running_there_is_no_manager(caplog):
    with caplog.at_level(logging.WARNING, logger="nous.main"):
        assert await main._start_second_settings(_settings_on(), None, database=object()) is None
    assert any("not available" in record.getMessage() for record in caplog.records)


async def test_a_manager_that_raises_at_start_leaves_the_first_judge_running(monkeypatch, caplog):
    from nous.judge import second_settings
    from nous.judge.service import JudgeSet

    async def broken(self: Any) -> None:
        raise RuntimeError(f"cannot load {JEV_KEY}")

    monkeypatch.setattr(second_settings.SecondJudgeManager, "load", broken)
    settings = _settings_on()
    service = await main._start_outside_judge(settings, database=object(), api_client=object())
    try:
        with caplog.at_level(logging.WARNING, logger="nous.main"):
            assert await main._start_second_settings(settings, service, database=object()) is None
        assert isinstance(service, JudgeSet) and service.second is None and judge_slot.current() is service
        assert service.primary.runtime()["worker_alive"] is True
        [line] = [r.getMessage() for r in caplog.records if r.name == "nous.main"]
        assert "RuntimeError" in line and JEV_KEY not in line
    finally:
        await service.close()
        judge_slot.set_current(None)


def test_create_components_starts_the_settings_after_the_judge_and_closes_them_before_it():  # PIN
    build = inspect.getsource(main.create_components)
    assert build.count("_start_second_settings(") == 1
    assert build.index("_start_outside_judge(") < build.index("_start_second_settings(")
    assert '"judge_second": judge_second' in build
    stop = inspect.getsource(main.shutdown_components)
    assert stop.index('components.get("judge_second")') < stop.index('components.get("outside_judge")')
    assert 'judge_second=_lazy_component(components, "judge_second")' in inspect.getsource(main.build_app)
