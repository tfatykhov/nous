"""F108 Phase 1 (recording), PR 3, the last part: the seven-day model-call text sweep, its wiring in create_components,
and the reader's boundary.

``sweep_model_call_text`` deletes this agent's ``model_call_block_ref`` rows older than
``NOUS_MODEL_CALL_TEXT_RETENTION_DAYS``, then its ``model_text_block`` rows that no call references and that no store
has touched for an hour. The row tests run against real Postgres under their own agent ids and delete their rows
afterwards; the concurrency tests hold real row locks on two connections and bound every wait. The loop tests need no
database.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import uuid
from datetime import timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from sqlalchemy import text

import nous.main as main
from nous.observability import model_call_text
from nous.observability.model_call_text import TextStore
from nous.observability.recording import BoundedWriter
from nous.observability.work_view_retention import sweep_model_call_text
from tests.test_model_call_retention import (
    _attempt_counts,
    _boot,
    _call_ages,
    _Database,
    _insert_call,
    _loop_settings,
    _one_startup_pass,
    _run,
)
from tests.test_work_view_retention import ROOT

_SYSTEM_PROMPT = "You are Nous. Keep what you learn."
_TEXT_TABLES = ("model_text_block", "model_call_block_ref")


def _agent() -> str:
    return f"test-awv-tret-{uuid.uuid4().hex[:12]}"


def _store(database: Any, agent: str) -> TextStore:
    """A real text store on ``database`` (its writer is never used: the tests call ``store`` directly)."""
    settings = SimpleNamespace(
        model_call_log_enabled=True, model_call_text_enabled=True, model_call_text_block_max_chars=200_000
    )
    return TextStore(BoundedWriter("test"), database, agent, settings)


async def _hashes(db, agent: str) -> set[str]:
    async with db.engine.connect() as conn:
        result = await conn.execute(
            text("SELECT hash FROM nous_system.model_text_block WHERE agent_id = :a"), {"a": agent}
        )
        return {row[0] for row in result}


async def _block_ages_in_hours(db, agent: str) -> dict[str, int]:
    """Each block of ``agent`` and the whole hours since a store last used it."""
    async with db.engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT hash, (extract(epoch FROM now() - created_at) / 3600)::int "
                "FROM nous_system.model_text_block WHERE agent_id = :a"
            ),
            {"a": agent},
        )
        return {row[0]: row[1] for row in result}


async def _refs(db, call_id: uuid.UUID) -> list[tuple[str, int, str]]:
    async with db.engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT part, ord, block_hash FROM nous_system.model_call_block_ref WHERE call_id = :c "
                "ORDER BY part, ord"
            ),
            {"c": call_id},
        )
        return [tuple(row) for row in result]


async def _age(
    db, agent: str, *, refs_of: uuid.UUID | None = None, refs: timedelta | None = None, blocks: timedelta | None = None
) -> None:
    """Move the refs of call ``refs_of`` back by ``refs``, and every block of ``agent`` back by ``blocks``."""
    async with db.engine.begin() as conn:
        if refs:
            await conn.execute(
                text(
                    "UPDATE nous_system.model_call_block_ref SET created_at = now() - CAST(:age AS interval) "
                    "WHERE call_id = :c"
                ),
                {"age": refs, "c": refs_of},
            )
        if blocks:
            await conn.execute(
                text(
                    "UPDATE nous_system.model_text_block SET created_at = now() - CAST(:age AS interval) "
                    "WHERE agent_id = :a"
                ),
                {"age": blocks, "a": agent},
            )


async def _insert_block(db, agent: str, body: str, age: timedelta, digest: str | None = None) -> str:
    """One block of ``agent`` last used ``age`` ago, with no ref; returns its hash."""
    digest = digest or hashlib.sha256(body.encode()).hexdigest()
    async with db.engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO nous_system.model_text_block (agent_id, hash, body, size_chars, created_at) "
                "VALUES (:a, :h, :b, :n, now() - CAST(:age AS interval))"
            ),
            {"a": agent, "h": digest, "b": body, "n": len(body), "age": age},
        )
    return digest


async def _delete(db, *agents: str) -> None:
    async with db.engine.begin() as conn:
        # the refs go with their calls (ON DELETE CASCADE), then nothing references the blocks
        await conn.execute(text("DELETE FROM nous_system.model_call_log WHERE agent_id = ANY(:a)"), {"a": list(agents)})
        await conn.execute(
            text("DELETE FROM nous_system.model_text_block WHERE agent_id = ANY(:a)"), {"a": list(agents)}
        )


# ---------------------------------------------------------------------------------------------------------------------
# The sweep
# ---------------------------------------------------------------------------------------------------------------------


@pytest.mark.postgres_only
async def test_block_referenced_by_a_recent_call_survives(db):
    agent = _agent()
    try:
        old_call = await _insert_call(db, agent, 10, attempts=0)
        new_call = await _insert_call(db, agent, 1, attempts=0)
        store = _store(db, agent)
        await store.store(old_call, [("system", 0, _SYSTEM_PROMPT), ("messages", 0, "the old question")])
        await store.store(new_call, [("system", 0, _SYSTEM_PROMPT), ("messages", 0, "the new question")])
        new_question, system = (row[2] for row in await _refs(db, new_call))  # ordered by part: messages, system
        # The old call's refs are 10 days old, and no block was used in the last hour: only a ref keeps a block.
        await _age(db, agent, refs_of=old_call, refs=timedelta(days=10), blocks=timedelta(hours=2))

        deleted = await sweep_model_call_text(db, agent, 7)

        assert deleted == {"model_call_block_ref": 2, "model_text_block": 1}  # the old question's block
        assert await _hashes(db, agent) == {system, new_question}
        assert await _refs(db, old_call) == []
        assert await _refs(db, new_call) == [("messages", 0, new_question), ("system", 0, system)]
    finally:
        await _delete(db, agent)


@pytest.mark.postgres_only
async def test_unreferenced_old_block_is_deleted(db):
    mine, other = _agent(), _agent()
    try:
        old = await _insert_block(db, mine, "a block no call sends any more", timedelta(hours=2))
        fresh = await _insert_block(db, mine, "a block a store has just written", timedelta(minutes=10))
        # Another agent: an old call whose ref is past the retention, and an old block no call references.
        their_call = await _insert_call(db, other, 10, attempts=0)
        await _store(db, other).store(their_call, [("system", 0, _SYSTEM_PROMPT)])
        await _age(db, other, refs_of=their_call, refs=timedelta(days=10), blocks=timedelta(hours=2))
        [their_system] = await _hashes(db, other)
        await _insert_block(db, other, "a block no call sends any more", timedelta(hours=2))

        deleted = await sweep_model_call_text(db, mine, 7)

        assert deleted == {"model_call_block_ref": 0, "model_text_block": 1}
        # A block no store has used for an hour goes; a block used within the hour stays.
        assert await _hashes(db, mine) == {fresh}
        # Another agent's old ref and old blocks survive: its own process sweeps them, by its own retention.
        assert await _refs(db, their_call) == [("system", 0, their_system)]
        assert await _hashes(db, other) == {old, their_system}
    finally:
        await _delete(db, mine, other)


@pytest.mark.postgres_only
async def test_numbers_row_survives_text_sweep(db):
    agent = _agent()
    try:
        call = await _insert_call(db, agent, 10, attempts=1)
        await _store(db, agent).store(call, [("system", 0, _SYSTEM_PROMPT), ("response", 0, "an answer")])
        await _age(db, agent, refs_of=call, refs=timedelta(days=10), blocks=timedelta(days=10))

        deleted = await sweep_model_call_text(db, agent, 7)

        assert deleted == {"model_call_block_ref": 2, "model_text_block": 2}
        assert await _refs(db, call) == [] and await _hashes(db, agent) == set()
        # The numbers are kept 30 days by their own sweep: the call row and its attempt are untouched.
        assert await _call_ages(db, agent) == [10]
        assert await _attempt_counts(db, call) == [1]
    finally:
        await _delete(db, agent)


@pytest.mark.parametrize("days", [0, -1])
async def test_sweep_refuses_a_retention_below_one_day(days):
    """``make_interval(days => 0)`` would delete every ref of the agent."""
    database = MagicMock()
    with pytest.raises(ValueError):
        await sweep_model_call_text(database, "a", days)
    database.session.assert_not_called()


# ---------------------------------------------------------------------------------------------------------------------
# A sweep and a store at once
# ---------------------------------------------------------------------------------------------------------------------


class _ParkedCommit:
    """The test database, but a session waits before its commit until ``release`` is set: the transaction has run all
    its statements and holds their row locks while it waits. ``parked`` is set when it gets there."""

    def __init__(self, db: Any) -> None:
        self._db = db
        self.parked = asyncio.Event()
        self.release = asyncio.Event()

    @contextlib.asynccontextmanager
    async def session(self):
        async with self._db.session() as session:
            yield _ParkedSession(session, self)


class _ParkedSession:
    def __init__(self, session: Any, gate: _ParkedCommit) -> None:
        self._session = session
        self._gate = gate

    async def execute(self, *args: Any, **kwargs: Any) -> Any:
        return await self._session.execute(*args, **kwargs)

    async def commit(self) -> None:
        self._gate.parked.set()
        await asyncio.wait_for(self._gate.release.wait(), 10)
        await self._session.commit()


class _ScanningDatabase:
    """The test database with index scans off in each session, so a statement reads the blocks in their physical
    order (a sweep that did not sort them would lock them in that order)."""

    def __init__(self, db: Any) -> None:
        self._db = db

    @contextlib.asynccontextmanager
    async def session(self):
        async with self._db.session() as session:
            for setting in ("enable_indexscan", "enable_bitmapscan", "enable_indexonlyscan"):
                await session.execute(text(f"SET LOCAL {setting} = off"))
            yield session


async def _a_statement_waits_for_a_block_lock(db) -> None:
    """Return once a statement on the text blocks is waiting for a lock another transaction holds (5 s at most)."""

    async def poll() -> None:
        while True:
            async with db.engine.connect() as conn:
                waiting = (
                    await conn.execute(
                        text(
                            "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                            "AND wait_event_type = 'Lock' AND query LIKE '%model_text_block%'"
                        )
                    )
                ).scalar_one()
            if waiting:
                return
            await asyncio.sleep(0.02)

    await asyncio.wait_for(poll(), 5)


async def _settle(gate: _ParkedCommit, tasks: list[asyncio.Task]) -> None:
    """Let a parked transaction go and stop whatever is still running, so a failed test never leaves a lock held."""
    gate.release.set()
    for task in tasks:
        if not task.done():
            task.cancel()
    await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 15)


@pytest.mark.postgres_only
@pytest.mark.parametrize("first", ["store", "sweep"], ids=["store holds the block", "sweep holds the block"])
async def test_sweep_is_safe_against_a_concurrent_store(db, first):
    """A store sends an old block no call references any more, while the sweep deletes it.

    Store first: the store's upsert touches the block and holds its row lock until it commits, so the sweep waits, then
    finds the block used now and keeps it (without the touch the sweep would delete it under the store's new ref, a
    foreign-key error). Sweep first: the store's upsert waits for the sweep's delete to commit, then writes the block
    again. Either way no foreign-key error and the new call's ref is kept.
    """
    agent = _agent()
    gate = _ParkedCommit(db)
    tasks: list[asyncio.Task] = []
    try:
        old_call = await _insert_call(db, agent, 10, attempts=0)
        new_call = await _insert_call(db, agent, 0, attempts=0)
        await _store(db, agent).store(old_call, [("system", 0, _SYSTEM_PROMPT)])
        await _age(db, agent, refs_of=old_call, refs=timedelta(days=10), blocks=timedelta(days=10))
        [digest] = await _hashes(db, agent)
        parts = [("system", 0, _SYSTEM_PROMPT)]

        if first == "store":
            store_task = asyncio.create_task(_store(gate, agent).store(new_call, parts))
            tasks.append(store_task)
            await asyncio.wait_for(gate.parked.wait(), 5)  # the block touched and the ref inserted, not committed
            sweep_task = asyncio.create_task(sweep_model_call_text(db, agent, 7))
            tasks.append(sweep_task)
            waiting = sweep_task
        else:
            sweep_task = asyncio.create_task(sweep_model_call_text(gate, agent, 7))
            tasks.append(sweep_task)
            await asyncio.wait_for(gate.parked.wait(), 5)  # the old ref and the block deleted, not committed
            store_task = asyncio.create_task(_store(db, agent).store(new_call, parts))
            tasks.append(store_task)
            waiting = store_task
        # The other transaction is really waiting for the parked one's row lock: not a run that merely came second.
        await _a_statement_waits_for_a_block_lock(db)
        assert not waiting.done()

        gate.release.set()
        swept = await asyncio.wait_for(sweep_task, 10)
        await asyncio.wait_for(store_task, 10)

        # The old call's ref goes either way; the block goes only when the sweep got it first, and the store then
        # writes it again.
        assert swept == {"model_call_block_ref": 1, "model_text_block": 1 if first == "sweep" else 0}
        assert await _refs(db, old_call) == []
        assert await _refs(db, new_call) == [("system", 0, digest)]
        assert await _block_ages_in_hours(db, agent) == {digest: 0}
    finally:
        await _settle(gate, tasks)
        await _delete(db, agent)


@pytest.mark.postgres_only
async def test_sweep_locks_blocks_in_the_stores_order(db):
    """The store upserts a call's blocks in hash order, and the sweep locks the blocks it deletes in hash order too,
    so a store and a sweep that share two blocks wait for each other and never deadlock.

    Here the sweep scans the table, the larger hash first. The store (its own statement, one block at a time) holds the
    smaller hash's row: the sweep waits for it without holding the larger one, so the store can touch the larger one
    and commit. A sweep that locked in scan order would hold the larger one, and the two would deadlock.
    """
    agent = _agent()
    high, low = "f" * 64, "0" * 64
    tasks: list[asyncio.Task] = []
    gate = _ParkedCommit(db)  # never parked: only for _settle
    try:
        async with db.engine.begin() as conn:
            for digest in (high, low):
                await conn.execute(
                    text(
                        "INSERT INTO nous_system.model_text_block (agent_id, hash, body, size_chars, created_at) "
                        "VALUES (:a, :h, 'old', 3, now() - interval '2 days')"
                    ),
                    {"a": agent, "h": digest},
                )
        async with db.engine.connect() as conn:
            physical = await conn.execute(
                text("SELECT hash FROM nous_system.model_text_block WHERE agent_id = :a ORDER BY ctid"), {"a": agent}
            )
            assert [row[0] for row in physical] == [high, low], "the scan must meet the larger hash first"

        def upsert(digest: str) -> dict[str, Any]:
            return {"agent_id": agent, "hash": digest, "body": "old", "size_chars": 3}

        async with db.engine.connect() as store:
            async with store.begin():
                await store.execute(model_call_text._UPSERT_BLOCK, upsert(low))
                sweep_task = asyncio.create_task(sweep_model_call_text(_ScanningDatabase(db), agent, 7))
                tasks.append(sweep_task)
                await _a_statement_waits_for_a_block_lock(db)
                await asyncio.wait_for(store.execute(model_call_text._UPSERT_BLOCK, upsert(high)), 5)
            swept = await asyncio.wait_for(sweep_task, 10)

        # Both blocks were touched by the store before the sweep could take them: both are kept.
        assert swept == {"model_call_block_ref": 0, "model_text_block": 0}
        assert await _block_ages_in_hours(db, agent) == {high: 0, low: 0}
    finally:
        await _settle(gate, tasks)
        await _delete(db, agent)


# ---------------------------------------------------------------------------------------------------------------------
# The reader's boundary
# ---------------------------------------------------------------------------------------------------------------------


@pytest.mark.postgres_only
async def test_reader_cannot_select_text_tables(db, reader_role, reader_settings):
    """The ``db_query`` reader never reads the text (it holds the prompts and responses). The role exists only where
    scripts/ops/db_roles.sql has run (the "DB roles" CI job); elsewhere the fixtures skip this test."""
    from nous.api.db_query import run_db_query

    async with db.engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT c.relname, has_any_column_privilege(:r, c.oid, 'SELECT') FROM pg_class c "
                "WHERE c.relnamespace = 'nous_system'::regnamespace AND c.relname = ANY(:t)"
            ),
            {"r": reader_role, "t": [*_TEXT_TABLES, "model_call_log"]},
        )
        readable = dict(result.all())
    # model_call_log is the positive control: the reader does read the numbers of the same calls.
    assert readable == {"model_text_block": False, "model_call_block_ref": False, "model_call_log": True}

    for table in _TEXT_TABLES:
        out = await run_db_query(reader_settings, f"select count(*) from nous_system.{table}")
        assert out.startswith("Error: 42501") and "permission denied" in out, (table, out)
    out = await run_db_query(reader_settings, "select count(*) as n from nous_system.model_call_log")
    assert out.splitlines()[0] == "n", out


# ---------------------------------------------------------------------------------------------------------------------
# The retention loop
# ---------------------------------------------------------------------------------------------------------------------


_TOOL_TABLES = [("tool_call_log", 30), ("agent_turn_log", 30)]
_TEXT_SWEEP = [("model_call_block_ref", 3), ("model_text_block", None)]  # the blocks go by the hour, not by days


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"model_call_text_enabled": True}, [*_TOOL_TABLES, ("model_call_log", 7), *_TEXT_SWEEP]),
        ({"model_call_text_enabled": True, "model_call_retention_days": 0}, [*_TOOL_TABLES, *_TEXT_SWEEP]),
        (
            {"model_call_text_enabled": True, "model_call_text_retention_days": 0},
            [*_TOOL_TABLES, ("model_call_log", 7)],
        ),
        ({"model_call_text_enabled": True, "model_call_log_enabled": False}, _TOOL_TABLES),
        ({}, [*_TOOL_TABLES, ("model_call_log", 7)]),
    ],
    ids=["text on", "numbers kept 0 days", "text kept 0 days", "text on, numbers off", "text off"],
)
def test_the_loop_sweeps_the_text_by_its_own_switch_and_retention(monkeypatch, caplog, overrides, expected):
    monkeypatch.setattr(main, "_RETENTION_SWEEP_INTERVAL_SECONDS", 3600)
    database = _Database()
    settings = _loop_settings(**{"model_call_text_retention_days": 3, **overrides})

    with caplog.at_level(logging.DEBUG, logger="nous.main"):
        _run(lambda: _one_startup_pass(settings, database))

    assert database.deleted == expected
    assert not [r for r in caplog.records if "retention sweep failed" in r.getMessage()]


@pytest.mark.parametrize(
    ("down", "message", "still_swept"),
    [
        (
            "model_call_block_ref",
            "F108: model-call text retention sweep failed",
            [*_TOOL_TABLES, ("model_call_log", 7)],
        ),
        ("model_call_log", "F108: model-call retention sweep failed", [*_TOOL_TABLES, *_TEXT_SWEEP]),
    ],
    ids=["text sweep fails", "numbers sweep fails"],
)
def test_a_failed_text_sweep_logs_a_warning_and_the_others_still_run(monkeypatch, caplog, down, message, still_swept):
    monkeypatch.setattr(main, "_RETENTION_SWEEP_INTERVAL_SECONDS", 3600)
    database = _Database(fail_on=(down,))
    settings = _loop_settings(model_call_text_enabled=True, model_call_text_retention_days=3)

    with caplog.at_level(logging.DEBUG, logger="nous.main"):
        _run(lambda: _one_startup_pass(settings, database))

    assert database.deleted == still_swept
    failed = [r for r in caplog.records if r.name == "nous.main" and message in r.getMessage()]
    assert failed, [r.getMessage() for r in caplog.records]
    # WARNING, never DEBUG: a sweep that keeps failing lets the text grow without bound.
    assert {r.levelno for r in failed} == {logging.WARNING}
    assert all(r.exc_info for r in failed)


# ---------------------------------------------------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------------------------------------------------


def _text_warnings(caplog) -> list[logging.LogRecord]:
    return [
        r for r in caplog.records if r.levelno == logging.WARNING and "NOUS_MODEL_CALL_TEXT_ENABLED" in r.getMessage()
    ]


def _spy_text_stores(monkeypatch) -> list[TextStore]:
    """Record every TextStore create_components builds (main imports it at call time)."""
    built: list[TextStore] = []

    class _Store(TextStore):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            built.append(self)

    monkeypatch.setattr(model_call_text, "TextStore", _Store)
    return built


@pytest.mark.postgres_only
def test_text_store_is_wired_on_the_shared_writer_when_both_flags_are_on(monkeypatch, caplog):
    built = _spy_text_stores(monkeypatch)
    seen: dict[str, Any] = {}

    async def checks(components: dict, settings: Any) -> None:
        try:
            writer = components["work_view_writer"]
            recorder = components["model_call_recorder"]
            assert len(built) == 1, "one text store"
            store = built[0]
            assert recorder._text is store and store.enabled
            assert store._writer is writer and store._database is components["database"]
            assert store._agent_id == settings.agent_id
            task = components["work_view_retention_task"]
            assert isinstance(task, asyncio.Task) and not task.done()
            seen["task"] = task
        finally:
            await main.shutdown_components(components)

    with caplog.at_level(logging.INFO):
        _boot(monkeypatch, checks, model_call_log_enabled=True, model_call_text_enabled=True)

    assert seen["task"].done()
    assert _text_warnings(caplog) == []


@pytest.mark.postgres_only
def test_text_on_without_the_numbers_warns_once_and_wires_nothing(monkeypatch, caplog):
    built = _spy_text_stores(monkeypatch)
    seen: dict[str, Any] = {}

    async def checks(components: dict, settings: Any) -> None:
        try:
            seen["writer"] = components["work_view_writer"]
            seen["recorder"] = components["model_call_recorder"]
            seen["task"] = components["work_view_retention_task"]
        finally:
            await main.shutdown_components(components)

    with caplog.at_level(logging.INFO):
        _boot(monkeypatch, checks, model_call_text_enabled=True)

    assert built == [] and seen == {"writer": None, "recorder": None, "task": None}
    # Exactly one WARNING at startup, though no recorder and no store is built.
    warned = _text_warnings(caplog)
    assert len(warned) == 1, [r.getMessage() for r in warned]
    assert warned[0].name == "nous.main" and "NOUS_MODEL_CALL_LOG_ENABLED" in warned[0].getMessage()


@pytest.mark.postgres_only
def test_numbers_on_without_the_text_builds_no_text_store(monkeypatch, caplog):
    built = _spy_text_stores(monkeypatch)
    seen: dict[str, Any] = {}

    async def checks(components: dict, settings: Any) -> None:
        try:
            seen["text"] = components["model_call_recorder"]._text
        finally:
            await main.shutdown_components(components)

    with caplog.at_level(logging.INFO):
        _boot(monkeypatch, checks, model_call_log_enabled=True)

    assert built == [] and seen == {"text": None}
    assert _text_warnings(caplog) == []


# ---------------------------------------------------------------------------------------------------------------------
# Docs
# ---------------------------------------------------------------------------------------------------------------------


def test_the_text_setting_rows_name_the_wiring_and_the_sweep():
    doc = (ROOT / "docs" / "reference" / "environment-variables.md").read_text(encoding="utf-8")
    rows = {
        name: next(line for line in doc.splitlines() if line.startswith(f"| `{name}` |"))
        for name in ("NOUS_MODEL_CALL_TEXT_ENABLED", "NOUS_MODEL_CALL_TEXT_RETENTION_DAYS")
    }
    assert "come with the rest of F108 PR 3" not in rows["NOUS_MODEL_CALL_TEXT_ENABLED"]
    for name, row in rows.items():
        assert "sweep_model_call_text" in row and "main.py" in row, name
