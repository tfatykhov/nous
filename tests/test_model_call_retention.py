"""F108 Phase 1 (recording), PR 2, the last part: the model-call retention sweep, the wiring in create_components, the
shutdown and the observability counters.

The sweep and boot tests run against real Postgres under their own agent ids and delete their rows afterwards. The
boots never start the heartbeat loop and their embedder fails before any request, so nothing reaches the network. The
rest need no database.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from sqlalchemy import text

import nous.main as main
from nous.brain.embeddings import EmbeddingProvider
from nous.browser import loop as browser_loop
from nous.heart import reranker
from nous.observability import model_calls, recording
from nous.observability.model_calls import ModelCallRecorder
from nous.observability.recording import BoundedWriter
from nous.observability.tool_calls import ToolCallRecorder
from nous.observability.work_view_retention import sweep_model_calls
from tests.test_work_view_retention import _observability, _run


def _agent() -> str:
    return f"test-awv-mret-{uuid.uuid4().hex[:12]}"


# ---------------------------------------------------------------------------------------------------------------------
# The sweep
# ---------------------------------------------------------------------------------------------------------------------


async def _insert_call(db, agent: str, days_ago: int, attempts: int, call_kind: str = "call") -> uuid.UUID:
    """One model-call row of ``agent`` that started ``days_ago`` days ago, with ``attempts`` attempt rows."""
    call_id = uuid.uuid4()
    async with db.engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO nous_system.model_call_log "
                "(id, agent_id, purpose, label_source, call_kind, status, started_at) "
                "VALUES (:id, :a, 'chat', 'explicit', :k, 'ok', now() - make_interval(days => :d))"
            ),
            {"id": call_id, "a": agent, "k": call_kind, "d": days_ago},
        )
        for n in range(1, attempts + 1):
            await conn.execute(
                text(
                    "INSERT INTO nous_system.model_call_attempt (call_id, agent_id, attempt_no, http_status) "
                    "VALUES (:c, :a, :n, 200)"
                ),
                {"c": call_id, "a": agent, "n": n},
            )
    return call_id


async def _call_ages(db, agent: str) -> list[int]:
    """The whole-day ages of ``agent``'s model-call rows, oldest first."""
    async with db.engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT extract(day FROM now() - started_at)::int FROM nous_system.model_call_log "
                "WHERE agent_id = :a ORDER BY started_at"
            ),
            {"a": agent},
        )
        return [row[0] for row in result]


async def _attempt_counts(db, *call_ids: uuid.UUID) -> list[int]:
    async with db.engine.connect() as conn:
        counts = []
        for call_id in call_ids:
            result = await conn.execute(
                text("SELECT count(*) FROM nous_system.model_call_attempt WHERE call_id = :c"), {"c": call_id}
            )
            counts.append(result.scalar_one())
        return counts


async def _delete_calls(db, *agents: str) -> None:
    async with db.engine.begin() as conn:
        # The attempts go with their calls (ON DELETE CASCADE).
        await conn.execute(text("DELETE FROM nous_system.model_call_log WHERE agent_id = ANY(:a)"), {"a": list(agents)})


@pytest.mark.postgres_only
async def test_sweep_deletes_old_calls_and_cascades_attempts(db):
    mine, other = _agent(), _agent()
    try:
        old = await _insert_call(db, mine, 31, attempts=2)
        new = await _insert_call(db, mine, 29, attempts=1)
        old_bucket = await _insert_call(db, mine, 31, attempts=0, call_kind="embedding")
        theirs = await _insert_call(db, other, 31, attempts=1)

        deleted = await sweep_model_calls(db, mine, 30)

        # The old call and the old minute bucket go; the call's attempts go with it.
        assert deleted == {"model_call_log": 2}
        assert await _call_ages(db, mine) == [29]
        assert await _attempt_counts(db, old, new, old_bucket) == [0, 1, 0]
        # Another agent's old rows survive: its own process sweeps them, by its own retention.
        assert await _call_ages(db, other) == [31]
        assert await _attempt_counts(db, theirs) == [1]
    finally:
        await _delete_calls(db, mine, other)


@pytest.mark.parametrize("days", [0, -1])
async def test_sweep_refuses_a_retention_below_one_day(days):
    """``make_interval(days => 0)`` would delete every row of the agent."""
    database = SimpleNamespace(session=None)
    with pytest.raises(ValueError):
        await sweep_model_calls(database, "a", days)


# ---------------------------------------------------------------------------------------------------------------------
# The retention loop: one loop, each log swept by its own switch and its own retention
# ---------------------------------------------------------------------------------------------------------------------


class _Database:
    """Records each DELETE's table and days; fails the statements on the tables named in ``fail_on``."""

    def __init__(self, fail_on: tuple[str, ...] = ()) -> None:
        self.deleted: list[tuple[str, int]] = []
        self.commits = 0
        self.fail_on = fail_on
        self.passes = asyncio.Event()

    @contextlib.asynccontextmanager
    async def session(self):
        yield self

    async def execute(self, statement: Any, params: dict) -> SimpleNamespace:
        table = str(statement).split("FROM nous_system.", 1)[1].split()[0]
        if table in self.fail_on:
            self.passes.set()
            raise ConnectionError(f"{table} is down in this test")
        self.deleted.append((table, params["d"]))
        return SimpleNamespace(rowcount=0)

    async def commit(self) -> None:
        self.commits += 1
        self.passes.set()


def _loop_settings(**overrides: Any) -> SimpleNamespace:
    values: dict[str, Any] = {
        "agent_id": "test",
        "tool_call_log_enabled": True,
        "tool_call_retention_days": 30,
        "model_call_log_enabled": True,
        "model_call_retention_days": 7,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


async def _one_startup_pass(settings: SimpleNamespace, database: _Database) -> None:
    """Start the loop, let its startup pass finish, and stop it (its next pass is an hour away)."""
    task = asyncio.create_task(main._work_view_retention_loop(settings, database))
    try:
        await asyncio.wait_for(database.passes.wait(), 5)
        await asyncio.sleep(0.05)  # let the rest of the pass run
    finally:
        await main.shutdown_components({"work_view_retention_task": task})
    assert task.done()


_TOOL_TABLES = [("tool_call_log", 30), ("agent_turn_log", 30)]


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({}, [*_TOOL_TABLES, ("model_call_log", 7)]),
        ({"model_call_log_enabled": False}, _TOOL_TABLES),
        ({"tool_call_log_enabled": False}, [("model_call_log", 7)]),
        ({"tool_call_retention_days": 0}, [("model_call_log", 7)]),
        ({"model_call_retention_days": 0}, _TOOL_TABLES),
    ],
    ids=["both logs", "tool log only", "model log only", "tool retention 0", "model retention 0"],
)
def test_the_loop_sweeps_each_log_by_its_own_switch_and_retention(monkeypatch, caplog, overrides, expected):
    monkeypatch.setattr(main, "_RETENTION_SWEEP_INTERVAL_SECONDS", 3600)
    database = _Database()

    with caplog.at_level(logging.DEBUG, logger="nous.main"):
        _run(lambda: _one_startup_pass(_loop_settings(**overrides), database))

    assert database.deleted == expected
    # A log that is off, or kept 0 days, is not tried at all: no sweep refused for 0 days and logged every day.
    assert not [r for r in caplog.records if "retention sweep failed" in r.getMessage()]


@pytest.mark.parametrize(
    ("down", "message", "still_swept"),
    [
        ("model_call_log", "F108: model-call retention sweep failed", _TOOL_TABLES),
        ("tool_call_log", "F108: tool-call retention sweep failed", [("model_call_log", 7)]),
    ],
    ids=["model sweep fails", "tool sweep fails"],
)
def test_a_failed_sweep_logs_a_warning_and_the_other_still_runs(monkeypatch, caplog, down, message, still_swept):
    monkeypatch.setattr(main, "_RETENTION_SWEEP_INTERVAL_SECONDS", 3600)
    database = _Database(fail_on=(down,))

    with caplog.at_level(logging.DEBUG, logger="nous.main"):
        _run(lambda: _one_startup_pass(_loop_settings(), database))

    assert database.deleted == still_swept
    failed = [r for r in caplog.records if r.name == "nous.main" and message in r.getMessage()]
    assert failed, [r.getMessage() for r in caplog.records]
    # WARNING, never DEBUG: a sweep that keeps failing lets its tables grow without bound.
    assert {r.levelno for r in failed} == {logging.WARNING}
    assert all(r.exc_info for r in failed)


# ---------------------------------------------------------------------------------------------------------------------
# Wiring and shutdown
# ---------------------------------------------------------------------------------------------------------------------


def _spy_constructions(monkeypatch) -> dict[str, list[Any]]:
    """Record every BoundedWriter and ModelCallRecorder create_components builds (it imports them at call time)."""
    built: dict[str, list[Any]] = {"writers": [], "model_recorders": []}

    class _Writer(BoundedWriter):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            built["writers"].append(self)

    class _Recorder(ModelCallRecorder):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            built["model_recorders"].append(self)

    monkeypatch.setattr(recording, "BoundedWriter", _Writer)
    monkeypatch.setattr(model_calls, "ModelCallRecorder", _Recorder)
    return built


def _boot(monkeypatch, scenario_checks, **flags: Any) -> None:
    """Boot create_components with the heartbeat client built but its loop never started (no tick, no model call),
    an embedding provider, and ``flags``; run ``scenario_checks(components, settings)``, which shuts down, then delete
    the boot's rows."""
    from nous.config import Settings
    from nous.heartbeat.runner import HeartbeatRunner

    agent_id = f"test-awv-mboot-{uuid.uuid4().hex[:8]}"

    async def already_migrated(engine: Any) -> None:
        """The test database is migrated before the run. The boot writes rows: they go to this test's own agent."""
        async with engine.begin() as connection:
            await connection.execute(
                text("INSERT INTO nous_system.agents (id, name, config) VALUES (:id, 'x', '{}')"), {"id": agent_id}
            )

    async def never_start(self: HeartbeatRunner) -> None:
        return None

    async def no_network(self: EmbeddingProvider, payload: dict, *, batch: bool = False) -> Any:
        """The boot's identity seed searches facts, which embeds: fail it here instead of at api.openai.com."""
        raise httpx.ConnectError("no network in this test")

    monkeypatch.setattr(main, "run_migrations", already_migrated)
    monkeypatch.setattr(HeartbeatRunner, "start", never_start)
    monkeypatch.setattr(EmbeddingProvider, "_post_with_retry", no_network)
    settings = Settings(
        _env_file=None,
        ANTHROPIC_API_KEY="test-key",
        OPENAI_API_KEY="test-key",
        agent_id=agent_id,
        heartbeat_enabled=True,
        subtask_enabled=False,
        schedule_enabled=False,
        dag_enabled=False,
        mcp_enabled=False,
        **flags,
    )

    async def scenario() -> None:
        try:
            components = await main.create_components(settings)
            await scenario_checks(components, settings)
        finally:
            # The browser loop's and the reranker's hooks are process-wide: a boot that failed before its shutdown
            # must not leave them set for the tests that run after it.
            browser_loop.set_recorder(None)
            reranker.set_recorder(None)
            from nous.storage.database import Database

            database = Database(settings)
            try:
                async with database.session() as session:
                    for statement in (
                        "DELETE FROM nous_system.model_call_log WHERE agent_id = :a",
                        "DELETE FROM heart.rubric_versions WHERE agent_id = :a",
                        "DELETE FROM nous_system.agents WHERE id = :a",
                    ):
                        await session.execute(text(statement), {"a": agent_id})
                    await session.commit()
            finally:
                await database.disconnect()

    _run(scenario, seconds=90)


def _hooks(components: dict) -> dict[str, Any]:
    """The recorder each model-call hook holds."""
    return {
        "api_client": components["api_client"]._recorder,
        "heartbeat_client": components["heartbeat_runner"]._api_client._recorder,
        "embedder": components["embedding_provider"]._recorder,
        "browser_loop": browser_loop._recorder,
        "reranker": reranker._recorder,
    }


@pytest.mark.postgres_only
def test_wiring_off_by_default(monkeypatch):
    built = _spy_constructions(monkeypatch)
    seen: dict[str, Any] = {}

    async def checks(components: dict, settings: Any) -> None:
        try:
            seen.update(_hooks(components))
            seen["writer"] = components["work_view_writer"]
            seen["model_call_recorder"] = components["model_call_recorder"]
            seen["retention_task"] = components["work_view_retention_task"]
        finally:
            await main.shutdown_components(components)

    _boot(monkeypatch, checks)

    assert built == {"writers": [], "model_recorders": []}
    assert seen == {
        "api_client": None,
        "heartbeat_client": None,
        "embedder": None,
        "browser_loop": None,
        "reranker": None,
        "writer": None,
        "model_call_recorder": None,
        "retention_task": None,
    }


@pytest.mark.postgres_only
def test_wiring_with_only_the_tool_log_builds_the_writer_and_no_model_recorder(monkeypatch):
    built = _spy_constructions(monkeypatch)
    seen: dict[str, Any] = {}

    async def checks(components: dict, settings: Any) -> None:
        try:
            seen.update(_hooks(components))
            seen["model_call_recorder"] = components["model_call_recorder"]
            writer = components["work_view_writer"]
            assert built["writers"] == [writer]
            assert components["tool_call_recorder"]._writer is writer
        finally:
            await main.shutdown_components(components)

    _boot(monkeypatch, checks, tool_call_log_enabled=True)

    assert built["model_recorders"] == []
    assert seen == dict.fromkeys(
        ("api_client", "heartbeat_client", "embedder", "browser_loop", "reranker", "model_call_recorder")
    )


@pytest.mark.postgres_only
@pytest.mark.parametrize("tool_log", [False, True], ids=["model log only", "both logs"])
def test_wiring_on_sets_recorder_on_both_clients(monkeypatch, tool_log):
    built = _spy_constructions(monkeypatch)
    seen: dict[str, Any] = {}

    async def checks(components: dict, settings: Any) -> None:
        try:
            writer = components["work_view_writer"]
            recorder = components["model_call_recorder"]
            seen["recorder"] = recorder
            assert built["writers"] == [writer], "one shared writer, never two"
            assert writer.name == "work_view" and (writer._timeout_s, writer._max_pending) == (30.0, 500)
            assert built["model_recorders"] == [recorder]
            assert recorder._writer is writer and recorder._database is components["database"]
            assert (recorder._agent_id, recorder._settings) == (settings.agent_id, settings)
            # Every hook holds the one recorder: both Anthropic clients, the embedder, the browser loop, the reranker.
            assert _hooks(components) == dict.fromkeys(
                ("api_client", "heartbeat_client", "embedder", "browser_loop", "reranker"), recorder
            )
            tool_recorder = components["tool_call_recorder"]
            if tool_log:
                assert isinstance(tool_recorder, ToolCallRecorder) and tool_recorder._writer is writer
            else:
                assert tool_recorder is None and components["runner"]._tool_call_recorder is None
            task = components["work_view_retention_task"]
            assert isinstance(task, asyncio.Task) and not task.done()
            seen["task"] = task
            seen["api_client"] = components["api_client"]
            seen["embedder"] = components["embedding_provider"]
        finally:
            await main.shutdown_components(components)
            seen["module_hooks_after_shutdown"] = (browser_loop._recorder, reranker._recorder)

    _boot(monkeypatch, checks, model_call_log_enabled=True, tool_call_log_enabled=tool_log)

    assert seen["task"].done()
    # Shutdown clears the hooks before the writer drains, so no recorder outlives its writer.
    assert seen["module_hooks_after_shutdown"] == (None, None)
    assert (seen["api_client"]._recorder, seen["embedder"]._recorder) == (None, None)


def test_shutdown_clears_the_recorders_before_the_writer_drains():
    order: list[str] = []
    recorder = ModelCallRecorder(BoundedWriter("work_view"), None, "a", SimpleNamespace())

    class _Client:
        def __init__(self) -> None:
            self._recorder = recorder

        def set_recorder(self, value: Any) -> None:
            self._recorder = value

        async def close(self) -> None:
            order.append("close")

    class _Writer:
        async def drain(self, timeout_s: float) -> None:
            order.append(f"drain, hooks {browser_loop._recorder}, {reranker._recorder}")

    class _Database:
        async def disconnect(self) -> None:
            order.append("disconnect")

    client, embedder = _Client(), _Client()

    async def scenario() -> None:
        browser_loop.set_recorder(recorder)
        reranker.set_recorder(recorder)
        try:
            await main.shutdown_components(
                {
                    "api_client": client,
                    "embedding_provider": embedder,
                    "model_call_recorder": recorder,
                    "work_view_writer": _Writer(),
                    "database": _Database(),
                }
            )
        finally:
            browser_loop.set_recorder(None)
            reranker.set_recorder(None)

    _run(scenario)

    assert order == ["close", "drain, hooks None, None", "disconnect"]
    assert (client._recorder, embedder._recorder) == (None, None)


# ---------------------------------------------------------------------------------------------------------------------
# /dashboard/observability
# ---------------------------------------------------------------------------------------------------------------------


def _model_recorder() -> ModelCallRecorder:
    recorder = ModelCallRecorder(BoundedWriter("work_view"), None, "a", SimpleNamespace())
    stats = recorder.stats
    stats.written, stats.dropped_error, stats.unlabelled, stats.dropped_late = 9, 1, 3, 2
    return recorder


MODEL_COUNTERS = {
    "written": 9,
    "dropped_timeout": 0,
    "dropped_error": 1,
    "dropped_backpressure": 0,
    "unlabelled": 3,
    "dropped_late": 2,
}


def test_unlabelled_counter_in_observability_endpoint():
    data = _observability(model_call_recorder=_model_recorder())

    assert data["recording"] == {"tool_calls": None, "model_calls": MODEL_COUNTERS}


def test_observability_endpoint_reports_null_model_calls_when_the_model_log_is_off():
    assert _observability()["recording"] == {"tool_calls": None, "model_calls": None}


def test_observability_endpoint_reads_the_model_recorder_through_the_apps_lazy_proxy():
    """main.py hands create_app a lazy proxy, which is never None: with the log off its component is None."""
    components: dict[str, Any] = {"model_call_recorder": None}
    proxy = main._lazy_component(components, "model_call_recorder")

    assert _observability(model_call_recorder=proxy)["recording"]["model_calls"] is None

    components["model_call_recorder"] = _model_recorder()
    assert _observability(model_call_recorder=proxy)["recording"]["model_calls"] == MODEL_COUNTERS


# ---------------------------------------------------------------------------------------------------------------------
# Docs
# ---------------------------------------------------------------------------------------------------------------------


def test_the_model_call_setting_rows_say_what_reads_them():
    from tests.test_work_view_retention import ROOT

    doc = (ROOT / "docs" / "reference" / "environment-variables.md").read_text(encoding="utf-8")
    for name in ("NOUS_MODEL_CALL_LOG_ENABLED", "NOUS_MODEL_CALL_RETENTION_DAYS"):
        row = next(line for line in doc.splitlines() if line.startswith(f"| `{name}` |"))
        assert "Nothing reads" not in row, name
        assert "sweep_model_calls" in row or "set_recorder" in row, name
