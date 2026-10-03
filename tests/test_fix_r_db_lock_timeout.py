"""A lock wait longer than NOUS_DB_LOCK_TIMEOUT_SECONDS fails instead of waiting for ever.

Real connections to the test database throughout. Only the lock that makes a
statement wait is staged, by a second session.
"""

from __future__ import annotations

import asyncio
import time

import pytest
from pydantic import ValidationError
from sqlalchemy import event, text
from sqlalchemy.exc import DBAPIError

from nous.config import Settings
from nous.storage.database import Database

_LOCK_TIMEOUT = text("SELECT setting, source FROM pg_settings WHERE name = 'lock_timeout'")


def _settings(**overrides) -> Settings:
    """Hermetic settings: never inherit the developer's .env."""
    return Settings(_env_file=None, **overrides)


async def _lock_timeout_of(database: Database) -> tuple[str, str]:
    async with database.engine.connect() as conn:
        return tuple((await conn.execute(_LOCK_TIMEOUT)).one())


async def _hold(conn, statement, params: dict | None = None) -> None:
    """Take a lock from another session for a test. Its own wait is bounded, so
    a lock another test left behind fails this one instead of hanging the run."""
    await conn.execute(text("SET LOCAL lock_timeout = '10s'"))
    await conn.execute(statement, params or {})


# ---------------------------------------------------------------------------
# The setting, and the connections that carry it
# ---------------------------------------------------------------------------


def test_the_default_is_no_timeout(monkeypatch):
    monkeypatch.delenv("NOUS_DB_LOCK_TIMEOUT_SECONDS", raising=False)
    assert _settings().db_lock_timeout_seconds == 0


def test_the_setting_is_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("NOUS_DB_LOCK_TIMEOUT_SECONDS", "30")
    assert _settings().db_lock_timeout_seconds == 30


@pytest.mark.parametrize("value", [-1, 2_147_484, "1.5", "30s"])
def test_a_value_the_server_would_refuse_stops_the_start(value):
    """Postgres takes 0 to 2147483647 ms. Seconds are whole, so a fraction
    cannot round down to 0 ms, which Postgres reads as no timeout at all."""
    with pytest.raises(ValidationError, match="db_lock_timeout_seconds"):
        _settings(db_lock_timeout_seconds=value)


@pytest.mark.postgres_only
async def test_the_service_database_carries_the_setting(monkeypatch):
    """The service builds its database in create_components; the test stops
    it at the first connect and asks the server what the engine sends."""
    import nous.main

    built: list[Database] = []

    class _Stop(Exception):
        pass

    async def stop_at_connect(self):
        built.append(self)
        raise _Stop

    monkeypatch.setattr(Database, "connect", stop_at_connect)
    with pytest.raises(_Stop):
        await nous.main.create_components(_settings(db_lock_timeout_seconds=7))
    try:
        assert await _lock_timeout_of(built[0]) == ("7000", "client")
    finally:
        await built[0].disconnect()


@pytest.mark.postgres_only
async def test_a_database_built_without_the_keyword_keeps_the_server_default():
    """Scripts and tests build Database(settings) from the service's
    environment. Only the service passes the setting on."""
    database = Database(_settings(db_lock_timeout_seconds=5))
    try:
        assert await _lock_timeout_of(database) == ("0", "default")
    finally:
        await database.disconnect()


@pytest.mark.postgres_only
async def test_at_zero_the_driver_gets_only_what_the_url_says():
    database = Database(_settings(), lock_timeout_seconds=0)
    sent: list[dict] = []
    event.listen(database.engine.sync_engine, "do_connect", lambda _d, _r, _a, cparams: sent.append(dict(cparams)))
    try:
        assert await _lock_timeout_of(database) == ("0", "default")
        from_url = database.engine.dialect.create_connect_args(database.engine.url)[1]
        same = sent == [from_url]  # compared, never printed: it holds the password
        assert same
    finally:
        await database.disconnect()


@pytest.mark.postgres_only
async def test_every_pooled_connection_carries_the_timeout():
    database = Database(_settings(db_pool_size=3, db_max_overflow=2), lock_timeout_seconds=2)
    backend = text("SELECT pg_backend_pid()")

    async def hold_one() -> tuple[int, tuple[str, str]]:
        async with database.engine.connect() as conn:
            pid = (await conn.execute(backend)).scalar()
            row = tuple((await conn.execute(_LOCK_TIMEOUT)).one())
            await asyncio.sleep(0.3)  # held, so the others need connections of their own
            return pid, row

    try:
        held = await asyncio.gather(*(hold_one() for _ in range(5)))
        assert len({pid for pid, _ in held}) == 5
        assert [row for _, row in held] == [("2000", "client")] * 5
        async with database.engine.connect() as conn:
            old = (await conn.execute(backend)).scalar()
            await conn.invalidate()  # the pool replaces it with a new connection
        async with database.engine.connect() as conn:
            assert (await conn.execute(backend)).scalar() != old
            assert tuple((await conn.execute(_LOCK_TIMEOUT)).one()) == ("2000", "client")
    finally:
        await database.disconnect()


@pytest.mark.postgres_only
async def test_a_statement_that_waits_longer_than_the_timeout_fails_with_55p03(db):
    database = Database(_settings(), lock_timeout_seconds=1)
    lock = text("SELECT pg_advisory_xact_lock(hashtextextended('test-fix-r', 0))")
    try:
        async with db.engine.connect() as holder:
            await _hold(holder, lock)
            async with database.engine.connect() as conn:
                started = time.monotonic()
                with pytest.raises(DBAPIError) as caught:
                    await asyncio.wait_for(conn.execute(lock), timeout=10)
                waited = time.monotonic() - started
            await holder.rollback()
        assert caught.value.orig.sqlstate == "55P03"
        assert 0.9 < waited < 5
    finally:
        await database.disconnect()
