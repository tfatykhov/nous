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

    async def grade(self, record: Any, *, timeout: float) -> Any:  # pragma: no cover - never called here
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


def _boot_settings(agent_id: str, mode: str) -> Settings:
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
