"""A lock wait longer than NOUS_DB_LOCK_TIMEOUT_SECONDS fails instead of waiting for ever.

Real connections to the test database throughout. Only the lock that makes a
statement wait is staged, by a second session.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import pytest_asyncio
from pydantic import ValidationError
from sqlalchemy import event, select, text
from sqlalchemy.exc import DBAPIError

from nous.config import Settings
from nous.events import Event
from nous.handlers.fact_extractor import FactExtractor
from nous.heart import Heart
from nous.heart.schemas import FactInput
from nous.heartbeat.checks import BehaviorDriftCheck
from nous.storage.database import Database
from nous.storage.models import Fact

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


# ---------------------------------------------------------------------------
# A fact write waits for another write of the same content, as before
# ---------------------------------------------------------------------------


def _fact(what: str) -> FactInput:
    return FactInput(content=f"{what} ({uuid.uuid4().hex[:8]})", category="technical", subject="fix-r")


def _own_agent() -> Settings:
    """Settings for an agent of its own: the facts a test stores never show up
    in another test's searches."""
    return _settings(agent_id=f"test-fixr-{uuid.uuid4().hex[:8]}")


@pytest_asyncio.fixture
async def own_heart(db, mock_embeddings):
    """A Heart on the test database, which has no lock timeout."""
    heart = Heart(db, _own_agent(), embedding_provider=mock_embeddings)
    yield heart
    await heart.close()


@pytest_asyncio.fixture
async def timed(mock_embeddings):
    """The service's database with a 1 s lock timeout, and a Heart on it."""
    database = Database(_settings(), lock_timeout_seconds=1)
    heart = Heart(database, _own_agent(), embedding_provider=mock_embeddings)
    yield SimpleNamespace(database=database, heart=heart)
    await heart.close()
    await database.disconnect()


@pytest.mark.postgres_only
async def test_a_second_learner_of_the_same_content_waits_for_the_first(db, timed):
    """The first learner holds the content's advisory lock for as long as its
    embedder and model calls take; the second waits for it, past the timeout."""
    fact = _fact("The fix-r deploy window is on Thursdays")
    key = f"fact_learn:{timed.heart.agent_id}:{fact.content}"
    async with db.engine.connect() as first:
        await _hold(first, text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"), {"k": key})

        async def first_finishes() -> None:
            await asyncio.sleep(2.5)
            await first.rollback()

        finishing = asyncio.create_task(first_finishes())
        started = time.monotonic()
        stored = await asyncio.wait_for(timed.heart.learn(fact), timeout=30)
        waited = time.monotonic() - started
        await finishing
    assert stored.content == fact.content
    assert waited > 2


@pytest.mark.postgres_only
async def test_the_rest_of_the_fact_write_keeps_the_timeout(db, timed):
    """Only the advisory lock waits without a limit: a write after it still
    gives up after the timeout."""
    async with db.engine.connect() as holder:
        await _hold(holder, text("LOCK TABLE heart.facts IN EXCLUSIVE MODE"))  # reads go on, writes wait
        with pytest.raises(DBAPIError) as caught:
            await asyncio.wait_for(timed.heart.learn(_fact("The fix-r build runs every night")), timeout=8)
        await holder.rollback()
    assert caught.value.orig.sqlstate == "55P03"


@pytest.mark.postgres_only
async def test_a_fact_write_in_a_callers_transaction_leaves_its_timeout_as_it_was(mock_embeddings):
    """With 2 s, not the 1 s of the other tests, so that only a restore to the
    connection's own value passes, not one to a fixed value."""
    database = Database(_settings(), lock_timeout_seconds=2)
    heart = Heart(database, _own_agent(), embedding_provider=mock_embeddings)
    current = text("SELECT current_setting('lock_timeout')")
    try:
        async with database.session() as session:
            await heart.learn(_fact("The fix-r staging host is in Oslo"), session=session)
            assert (await session.execute(current)).scalar() == "2s"
            async with session.begin_nested():
                await heart.learn(_fact("The fix-r staging host has 64 GB"), session=session)
            assert (await session.execute(current)).scalar() == "2s"
            await session.rollback()
    finally:
        await heart.close()
        await database.disconnect()


@pytest.mark.postgres_only
async def test_the_lift_ends_with_the_fact_write(mock_embeddings):
    """Every connection of the pool, the one the write used among them, has
    the timeout again once the write is over."""
    database = Database(_settings(db_pool_size=2, db_max_overflow=0), lock_timeout_seconds=1)
    heart = Heart(database, _own_agent(), embedding_provider=mock_embeddings)
    try:
        await heart.learn(_fact("The fix-r queue drains every minute"))
        both = await asyncio.gather(_lock_timeout_of(database), _lock_timeout_of(database))
        assert both == [("1000", "client")] * 2
    finally:
        await heart.close()
        await database.disconnect()


@pytest.mark.postgres_only
async def test_without_a_timeout_the_fact_write_sends_what_it_always_sent(db, own_heart):
    sent: list[str] = []

    def record(_conn, _cursor, statement, *_rest) -> None:
        sent.append(statement)

    event.listen(db.engine.sync_engine, "before_cursor_execute", record)
    try:
        await own_heart.learn(_fact("The fix-r cache holds 512 entries"))
    finally:
        event.remove(db.engine.sync_engine, "before_cursor_execute", record)
    assert any("pg_advisory_xact_lock" in statement for statement in sent)
    assert not any("lock_timeout" in statement for statement in sent)


# ---------------------------------------------------------------------------
# One fact that cannot be stored does not cost the rest of the episode
# ---------------------------------------------------------------------------

_EPISODE = "episode-fix-r"  # not a UUID, so the facts reference no episode row


def _episode_facts(tag: str) -> list[dict]:
    return [
        {"content": f"The fix-r rule number {n} of run {tag} is long enough to keep", "subject": "fix-r"}
        for n in range(3)
    ]


def _summarized(**data) -> Event:
    data = {"summary": {"summary": "s"}, "episode_id": _EPISODE, **data}
    return Event(type="episode_summarized", agent_id="fix-r", data=data)


async def _stored(database, tag: str) -> list[str]:
    async with database.session() as session:
        rows = await session.execute(select(Fact.content).where(Fact.content.like(f"% of run {tag} %")))
        return sorted(rows.scalars())


def _lock_timeout() -> DBAPIError:
    return DBAPIError("INSERT INTO heart.facts", {}, Exception("canceling statement due to lock timeout"))


def _first_learn_fails(heart: Heart, error: BaseException) -> None:
    real = heart.learn
    calls = []

    async def learn(fact_input, **kwargs):
        calls.append(fact_input)
        if len(calls) == 1:
            raise error
        return await real(fact_input, **kwargs)

    heart.learn = learn


@pytest.mark.postgres_only
async def test_one_fact_that_cannot_be_stored_does_not_cost_the_rest(db, own_heart, caplog):
    """Postgres only, as every test here that stores a fact: the fact write
    searches by vector."""
    tag = uuid.uuid4().hex[:8]
    facts = _episode_facts(tag)
    _first_learn_fails(own_heart, _lock_timeout())

    await FactExtractor(own_heart, _settings(), None, dedup_via_search=False).handle(_summarized(candidate_facts=facts))

    assert await _stored(db, tag) == [f["content"] for f in facts[1:]]
    failed = [r for r in caplog.records if r.levelno >= logging.WARNING and _EPISODE in r.getMessage()]
    assert [(r.levelname, r.exc_info[0]) for r in failed] == [("ERROR", DBAPIError)]


@pytest.mark.postgres_only
async def test_on_the_model_path_too_one_fact_does_not_cost_the_rest(db, own_heart):
    """Without candidate facts the extractor asks the model; the model is
    replaced here by its answer."""
    tag = uuid.uuid4().hex[:8]
    facts = _episode_facts(tag)
    extractor = FactExtractor(own_heart, _settings(), None, dedup_via_search=False)

    async def the_model_answers(_summary):
        return facts

    extractor._extract_facts = the_model_answers
    _first_learn_fails(own_heart, _lock_timeout())

    await extractor.handle(_summarized())

    assert await _stored(db, tag) == [f["content"] for f in facts[1:]]


@pytest.mark.postgres_only
async def test_the_tiebreakers_exclusions_still_reach_the_fact_write(db, own_heart):
    """The extractor's search finds a stored fact with the same words; the
    tiebreaker (the model, replaced here by its answer) calls them distinct, so
    the fact write must not fold the candidate into the stored one."""
    tag = uuid.uuid4().hex[:8]
    content = f"The fix-r rule number 9 of run {tag} is long enough to keep"
    await own_heart.learn(FactInput(content=content, subject="fix-r", category="technical"))

    async def the_model_says_distinct(_stored, _candidate):
        return True

    own_heart.facts.is_distinct_fact = the_model_says_distinct
    extractor = FactExtractor(own_heart, _settings(fact_dedup_tiebreaker_enabled=True), None)
    await extractor.handle(_summarized(candidate_facts=[{"content": content, "subject": "fix-r"}]))

    assert await _stored(db, tag) == [content, content]


@pytest.mark.postgres_only
async def test_a_real_lock_timeout_in_the_middle_of_an_episode_costs_one_fact(db, timed):
    """Another session blocks every write to heart.facts and lets go once the
    first fact's write has timed out."""
    tag = uuid.uuid4().hex[:8]
    facts = _episode_facts(tag)
    async with db.engine.connect() as holder:
        await _hold(holder, text("LOCK TABLE heart.facts IN EXCLUSIVE MODE"))
        real = timed.heart.learn

        async def release_when_it_times_out(fact_input, **kwargs):
            try:
                return await real(fact_input, **kwargs)
            except DBAPIError:
                await holder.rollback()
                raise

        timed.heart.learn = release_when_it_times_out
        extractor = FactExtractor(timed.heart, _settings(), None, dedup_via_search=False)
        await asyncio.wait_for(extractor.handle(_summarized(candidate_facts=facts)), timeout=30)

    assert await _stored(timed.database, tag) == [f["content"] for f in facts[1:]]


# ---------------------------------------------------------------------------
# The drift check says when it could not read or write its numbers
# ---------------------------------------------------------------------------


@pytest.mark.postgres_only
@pytest.mark.parametrize(
    ("table", "mode", "message"),
    [
        ("heart.facts", "ACCESS EXCLUSIVE", "Snapshot: DB query failed"),
        ("nous_system.behavior_snapshots", "ACCESS EXCLUSIVE", "Baseline load failed"),
        ("nous_system.behavior_snapshots", "EXCLUSIVE", "Snapshot store failed"),
    ],
    ids=["count-query", "baseline-load", "snapshot-store"],
)
async def test_a_drift_check_that_cannot_reach_its_table_logs_a_warning(db, timed, caplog, table, mode, message):
    check = BehaviorDriftCheck(heart=MagicMock(), brain=MagicMock(), settings=_own_agent(), db=timed.database)
    async with db.engine.connect() as holder:
        await _hold(holder, text(f"LOCK TABLE {table} IN {mode} MODE"))
        await asyncio.wait_for(check.run(), timeout=15)
        await holder.rollback()
    warned = [r for r in caplog.records if r.levelno == logging.WARNING and r.getMessage() == message]
    assert len(warned) == 1
    assert "lock timeout" in str(warned[0].exc_info[1])
