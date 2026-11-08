"""F108 Phase 1 (recording), the last part: the retention sweep, the wiring in create_components, the shutdown drain,
the observability counters and the docs.

The sweep and boot tests run against real Postgres under their own agent ids and delete their rows afterwards. The
rest need no database.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from sqlalchemy import text

import nous.main as main
from nous.observability.recording import BoundedWriter
from nous.observability.tool_calls import ToolCallRecorder
from nous.observability.work_view_retention import sweep_tool_calls

ROOT = Path(__file__).resolve().parents[1]
_SETTINGS = SimpleNamespace(tool_input_max_chars=2000, tool_result_preview_chars=500, tool_reason_max_chars=500)
DB_QUERY_EXCEPTION = (
    "dashboards read these tables, the agent never does, except `tool_call_log`, which `db_query` may read"
)


def _agent() -> str:
    return f"test-awv-ret-{uuid.uuid4().hex[:12]}"


# ---------------------------------------------------------------------------------------------------------------------
# The sweep
# ---------------------------------------------------------------------------------------------------------------------


async def _insert_rows(db, agent: str, days_ago: int) -> None:
    """One tool-call row and one turn row of ``agent`` that started ``days_ago`` days ago."""
    async with db.engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO nous_system.agent_turn_log (id, agent_id, work_kind, started_at) "
                "VALUES (:id, :a, 'conversation', now() - make_interval(days => :d))"
            ),
            {"id": uuid.uuid4(), "a": agent, "d": days_ago},
        )
        await conn.execute(
            text(
                "INSERT INTO nous_system.tool_call_log "
                "(agent_id, call_index, work_kind, tool_name, tool_name_called, outcome, started_at) "
                "VALUES (:a, 0, 'conversation', 'bash', 'bash', 'ran', now() - make_interval(days => :d))"
            ),
            {"a": agent, "d": days_ago},
        )


async def _ages(db, agent: str) -> dict[str, list[int]]:
    """Per table, the whole-day ages of ``agent``'s rows, oldest first."""
    out: dict[str, list[int]] = {}
    async with db.engine.connect() as conn:
        for table in ("tool_call_log", "agent_turn_log"):
            result = await conn.execute(
                text(
                    f"SELECT extract(day FROM now() - started_at)::int FROM nous_system.{table} "
                    "WHERE agent_id = :a ORDER BY started_at"
                ),
                {"a": agent},
            )
            out[table] = [row[0] for row in result]
    return out


async def _delete_rows(db, *agents: str) -> None:
    async with db.engine.begin() as conn:
        for table in ("tool_call_log", "agent_turn_log"):
            await conn.execute(text(f"DELETE FROM nous_system.{table} WHERE agent_id = ANY(:a)"), {"a": list(agents)})


@pytest.mark.postgres_only
async def test_sweep_deletes_only_old_rows_for_this_agent(db):
    mine, other = _agent(), _agent()
    try:
        await _insert_rows(db, mine, 31)
        await _insert_rows(db, mine, 29)
        await _insert_rows(db, other, 31)

        deleted = await sweep_tool_calls(db, mine, 30)

        assert deleted == {"tool_call_log": 1, "agent_turn_log": 1}
        assert await _ages(db, mine) == {"tool_call_log": [29], "agent_turn_log": [29]}
        # Another agent's old rows survive: its own process sweeps them, by its own retention.
        assert await _ages(db, other) == {"tool_call_log": [31], "agent_turn_log": [31]}
    finally:
        await _delete_rows(db, mine, other)


@pytest.mark.parametrize("days", [0, -1])
async def test_sweep_refuses_a_retention_below_one_day(days):
    """``make_interval(days => 0)`` would delete every row of the agent."""
    database = MagicMock()
    with pytest.raises(ValueError):
        await sweep_tool_calls(database, "a", days)
    database.session.assert_not_called()


# ---------------------------------------------------------------------------------------------------------------------
# The daily loop
# ---------------------------------------------------------------------------------------------------------------------


def _run(scenario: Callable[[], Awaitable[None]], seconds: float = 20.0) -> None:
    """Run a scenario on an event loop and a thread of its own, so a loop that ignores its cancellation fails one
    test instead of hanging the session (as tests/test_fix_z_maintenance_loops.py does)."""
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


class _FailingDatabase:
    """A database that is down. ``tried`` is set once a sweep asked it for a session."""

    def __init__(self) -> None:
        self.tried: asyncio.Event | None = None

    @contextlib.asynccontextmanager
    async def session(self):
        if self.tried is not None:
            self.tried.set()
        raise ConnectionError("no database in this test")
        yield


def test_sweep_failure_logs_warning(monkeypatch, caplog):
    monkeypatch.setattr(main, "_RETENTION_SWEEP_INTERVAL_SECONDS", 0.01)
    settings = SimpleNamespace(agent_id="test", tool_call_retention_days=30)
    database = _FailingDatabase()

    async def scenario() -> None:
        database.tried = asyncio.Event()
        task = asyncio.create_task(main._tool_call_retention_loop(settings, database))
        try:
            await asyncio.wait_for(database.tried.wait(), 5)
            await asyncio.sleep(0.05)  # let the failed pass log
        finally:
            await main.shutdown_components({"tool_call_retention_task": task})
        assert task.done()

    with caplog.at_level(logging.DEBUG, logger="nous.main"):
        _run(scenario)

    failed = [r for r in caplog.records if r.name == "nous.main" and "tool-call retention sweep failed" in r.message]
    assert failed, [r.getMessage() for r in caplog.records]
    # WARNING, never DEBUG: a sweep that keeps failing lets the tables grow without bound.
    assert {r.levelno for r in failed} == {logging.WARNING}
    assert all(r.exc_info for r in failed)


# ---------------------------------------------------------------------------------------------------------------------
# Wiring and shutdown
# ---------------------------------------------------------------------------------------------------------------------


def test_shutdown_drains_the_writer_before_the_database_disconnects():
    order: list[str] = []

    class _Writer:
        async def drain(self, timeout_s: float) -> None:
            assert 0 < timeout_s <= 10
            order.append("drain")

    class _Database:
        async def disconnect(self) -> None:
            order.append("disconnect")

    async def scenario() -> None:
        await main.shutdown_components({"work_view_writer": _Writer(), "database": _Database()})

    _run(scenario)

    assert order == ["drain", "disconnect"]


@pytest.mark.postgres_only
@pytest.mark.parametrize("enabled", [True, False], ids=["log on", "log off"])
def test_create_components_wires_one_writer_and_the_recorder_only_when_the_log_is_on(monkeypatch, enabled):
    from nous.config import Settings

    agent_id = f"test-awv-boot-{uuid.uuid4().hex[:8]}"

    async def already_migrated(engine: Any) -> None:
        """The test database is migrated before the run. The boot writes rows: they go to this test's own agent."""
        async with engine.begin() as connection:
            await connection.execute(
                text("INSERT INTO nous_system.agents (id, name, config) VALUES (:id, 'x', '{}')"), {"id": agent_id}
            )

    monkeypatch.setattr(main, "run_migrations", already_migrated)
    settings = Settings(
        _env_file=None,
        ANTHROPIC_API_KEY="test-key",
        agent_id=agent_id,
        heartbeat_enabled=False,
        subtask_enabled=False,
        schedule_enabled=False,
        dag_enabled=False,
        mcp_enabled=False,
        tool_call_log_enabled=enabled,
    )

    async def boot() -> None:
        # With the log on, the retention loop sweeps at startup: a real DELETE, scoped to this test's own agent.
        components = await main.create_components(settings)
        try:
            writer = components["work_view_writer"]
            recorder = components["tool_call_recorder"]
            task = components["tool_call_retention_task"]
            if enabled:
                assert isinstance(writer, BoundedWriter) and writer.name == "work_view"
                assert (writer._timeout_s, writer._max_pending) == (30.0, 500)
                assert isinstance(recorder, ToolCallRecorder) and recorder._writer is writer
                assert components["runner"]._tool_call_recorder is recorder
                assert isinstance(task, asyncio.Task) and not task.done()
            else:
                assert (writer, recorder, task) == (None, None, None)
                assert components["runner"]._tool_call_recorder is None
        finally:
            await main.shutdown_components(components)
        if enabled:
            assert task.done()

    async def scenario() -> None:
        try:
            await boot()
        finally:
            # The two rows the boot wrote: its agent and that agent's rubric version.
            from nous.storage.database import Database

            database = Database(settings)
            try:
                async with database.session() as session:
                    await session.execute(
                        text("DELETE FROM heart.rubric_versions WHERE agent_id = :a"), {"a": agent_id}
                    )
                    await session.execute(text("DELETE FROM nous_system.agents WHERE id = :a"), {"a": agent_id})
                    await session.commit()
            finally:
                await database.disconnect()

    _run(scenario, seconds=90)


# ---------------------------------------------------------------------------------------------------------------------
# /dashboard/observability
# ---------------------------------------------------------------------------------------------------------------------


class _NoDatabase:
    """A database that is down: the endpoint's DB sections take their fallback."""

    @contextlib.asynccontextmanager
    async def session(self):
        raise ConnectionError("no database in this test")
        yield


def _observability(**kwargs: Any) -> dict:
    from starlette.testclient import TestClient

    from nous.api.rest import create_app
    from nous.config import Settings

    app = create_app(
        runner=MagicMock(),
        brain=MagicMock(),
        heart=MagicMock(),
        cognitive=MagicMock(),
        database=_NoDatabase(),
        settings=Settings(_env_file=None),
        **kwargs,
    )
    response = TestClient(app).get("/dashboard/observability")
    assert response.status_code == 200, response.text
    return response.json()


def _recorder() -> ToolCallRecorder:
    recorder = ToolCallRecorder(BoundedWriter("work_view"), None, "a", _SETTINGS)
    recorder.stats.written, recorder.stats.dropped_timeout, recorder.stats.dropped_backpressure = 7, 1, 2
    return recorder


COUNTERS = {"written": 7, "dropped_timeout": 1, "dropped_error": 0, "dropped_backpressure": 2, "unlabelled": 0}


def test_observability_endpoint_reports_recording_counters():
    data = _observability(tool_call_recorder=_recorder())

    assert data["recording"] == {"tool_calls": COUNTERS}
    # Additive: the existing keys are all still there.
    assert set(data) == {
        "event_bus",
        "recent_traces",
        "recent_modifications",
        "drift",
        "drift_trends",
        "context_log",
        "recording",
    }


def test_observability_endpoint_reports_null_when_recording_is_off():
    assert _observability()["recording"] == {"tool_calls": None}


def test_observability_endpoint_reads_the_recorder_through_the_apps_lazy_proxy():
    """main.py hands create_app a lazy proxy, which is never None: with the log off its component is None, and the
    endpoint must not touch it (a RuntimeError there would fail the whole endpoint)."""
    components: dict[str, Any] = {"tool_call_recorder": None}
    proxy = main._lazy_component(components, "tool_call_recorder")

    assert _observability(tool_call_recorder=proxy)["recording"] == {"tool_calls": None}

    components["tool_call_recorder"] = _recorder()
    assert _observability(tool_call_recorder=proxy)["recording"] == {"tool_calls": COUNTERS}


# ---------------------------------------------------------------------------------------------------------------------
# Docs
# ---------------------------------------------------------------------------------------------------------------------


def test_claude_md_states_the_db_query_exception():
    for path in (ROOT / "CLAUDE.md", ROOT / "docs" / "reference" / "project-structure.md"):
        assert DB_QUERY_EXCEPTION in path.read_text(encoding="utf-8"), path


ENV_ROWS = {
    "NOUS_TOOL_CALL_LOG_ENABLED": "false",
    "NOUS_TOOL_REASON_ENABLED": "false",
    "NOUS_TOOL_CALL_RETENTION_DAYS": "30",
    "NOUS_TOOL_INPUT_MAX_CHARS": "2000",
    "NOUS_TOOL_RESULT_PREVIEW_CHARS": "500",
    "NOUS_TOOL_REASON_MAX_CHARS": "500",
}


def test_every_work_view_setting_has_its_row_with_the_code_default():
    from nous.config import Settings

    settings = Settings(_env_file=None)
    doc = (ROOT / "docs" / "reference" / "environment-variables.md").read_text(encoding="utf-8")
    for name, value in ENV_ROWS.items():
        assert str(getattr(settings, name.removeprefix("NOUS_").lower())).lower() == value, name
        assert f"| `{name}` | `{value}` |" in doc, name
    reason_row = next(line for line in doc.splitlines() if line.startswith("| `NOUS_TOOL_REASON_ENABLED` |"))
    assert "cache" in reason_row
