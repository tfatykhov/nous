"""F115: the autonomy settings through the real ``create_components``: when the saved settings cannot be read at start,
background work is held from every tool until a read in the background succeeds, and a tool the owner switched off is
never offered in between (real Postgres)."""

from __future__ import annotations

import asyncio
import threading
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from sqlalchemy import text

import nous.main as main
from nous.api.execution_context import ExecutionContext
from nous.autonomy import store as store_module
from nous.autonomy import tool_guard
from nous.autonomy.tool_guard import HARNESS_TOOLS

pytestmark = pytest.mark.postgres_only

DENIED = "read_file"


def _run(scenario: Callable[[], Awaitable[None]], seconds: float = 120.0) -> None:
    """Run a scenario on an event loop and a thread of its own, so a loop that ignores its cancellation fails one
    test instead of hanging the session."""
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


def _settings(agent_id: str) -> Any:
    from nous.config import Settings

    return Settings(
        _env_file=None,
        ANTHROPIC_API_KEY="test-key",
        agent_id=agent_id,
        heartbeat_enabled=False,
        subtask_enabled=False,
        schedule_enabled=False,
        dag_enabled=False,
        mcp_enabled=False,
        autonomy_settings_enabled=True,
        tool_guard_enabled=True,
        autonomy_ceilings="subtask_tool_call_limit=40",
        subtask_tool_call_limit=50,
    )


async def _sql(settings: Any, *statements: tuple[str, dict]) -> None:
    from nous.storage.database import Database

    database = Database(settings)
    await database.connect()
    try:
        async with database.session() as session:
            for statement, params in statements:
                await session.execute(text(statement), params)
            await session.commit()
    finally:
        await database.disconnect()


def _offered(components: dict) -> set[str]:
    runner = components["runner"]
    ctx = ExecutionContext(kind="scheduled", session_id="schedule-boot")
    return {
        t["name"] for t in runner._offered_tools(ctx, "task", is_subtask=True, tool_filter=None, refuse_active=False)
    }


async def _until_loaded(components: dict, seen: list[set[str]], seconds: float = 20.0) -> None:
    """Wait for the store to read its settings, recording the background turn's tools at each look."""
    store = components["autonomy"]
    deadline = asyncio.get_running_loop().time() + seconds
    while not store.loaded:
        seen.append(_offered(components))
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("the saved settings were not read again")
        await asyncio.sleep(0.02)
    seen.append(_offered(components))


def _boot_scenario(monkeypatch: pytest.MonkeyPatch, make_unreadable: str) -> None:
    agent_id = f"test-autonomy-boot-{uuid.uuid4().hex[:8]}"
    settings = _settings(agent_id)

    async def already_migrated(engine: Any) -> None:
        async with engine.begin() as connection:
            await connection.execute(
                text("INSERT INTO nous_system.agents (id, name, config) VALUES (:id, 'x', '{}')"), {"id": agent_id}
            )

    monkeypatch.setattr(main, "run_migrations", already_migrated)
    monkeypatch.setattr(store_module, "RETRY_FIRST_SECONDS", 0.2)
    calls = {"reads": 0}
    real_read = store_module.AutonomyStore._read

    async def first_read_fails(self: Any) -> Any:
        calls["reads"] += 1
        if calls["reads"] == 1:
            raise ConnectionError("the first read fails")
        return await real_read(self)

    if make_unreadable == "transient":
        monkeypatch.setattr(store_module.AutonomyStore, "_read", first_read_fails)

    async def scenario() -> None:
        await _sql(
            settings,
            (
                "INSERT INTO nous_system.tool_guard (agent_id, scope, tool, setting, updated_by) "
                "VALUES (:a, 'background', :t, 'denied', 'owner')",
                {"a": agent_id, "t": DENIED},
            ),
            (
                "INSERT INTO nous_system.autonomy_settings (agent_id, key, value, updated_by) "
                "VALUES (:a, 'max_turns', '40', 'owner')",
                {"a": agent_id},
            ),
        )
        moved = False
        components: dict | None = None
        try:
            if make_unreadable == "table":
                await _sql(settings, ("ALTER TABLE nous_system.tool_guard RENAME TO tool_guard_moved_aside", {}))
                moved = True
            components = await main.create_components(settings)
            store = components["autonomy"]
            assert store.loaded is False
            guard = tool_guard.current()
            assert guard is not None and guard.hold_all
            # Every limit is the host's, lowered to its ceiling: the stored max_turns waits for the read.
            assert (settings.subtask_tool_call_limit, settings.max_turns) == (40, 10)
            seen: list[set[str]] = [_offered(components)]
            assert seen[0] <= HARNESS_TOOLS  # every tool held for background work
            if moved:
                await asyncio.sleep(0.5)  # a read in the background fails while the table is away
                assert not store.loaded
                await _sql(settings, ("ALTER TABLE nous_system.tool_guard_moved_aside RENAME TO tool_guard", {}))
                moved = False
            await _until_loaded(components, seen)
            assert store.loaded and not tool_guard.current().hold_all
            assert tool_guard.current().denied == frozenset({DENIED})
            assert settings.max_turns == 40
            assert all(DENIED not in offered for offered in seen)
            assert len(seen[-1]) > 5 and DENIED not in seen[-1]
        finally:
            if moved:
                await _sql(settings, ("ALTER TABLE nous_system.tool_guard_moved_aside RENAME TO tool_guard", {}))
            if components is not None:
                await main.shutdown_components(components)
            await _sql(
                settings,
                ("DELETE FROM nous_system.tool_guard WHERE agent_id = :a", {"a": agent_id}),
                ("DELETE FROM nous_system.autonomy_settings WHERE agent_id = :a", {"a": agent_id}),
                ("DELETE FROM heart.rubric_versions WHERE agent_id = :a", {"a": agent_id}),
                ("DELETE FROM nous_system.agents WHERE id = :a", {"a": agent_id}),
            )
            tool_guard.install(None)

    _run(scenario)


def test_a_table_missing_at_start_holds_every_tool_until_it_is_read(monkeypatch):
    _boot_scenario(monkeypatch, "table")


def test_a_first_read_that_fails_holds_every_tool_until_the_next_read(monkeypatch):
    _boot_scenario(monkeypatch, "transient")
