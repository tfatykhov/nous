"""A disable that races a revert is not lost (post-merge review of #652, finding P2-4).

postgres_only: ``SELECT ... FOR UPDATE`` is silently dropped on SQLite, and
``enable_if_unchanged`` uses JSONB operators SQLite does not have. On the
SQLite lane these tests are SKIPPED — they do not run there, so they cannot
fail there. CI (``NOUS_TEST_DB=postgres``) is the lane that proves them.

Two real sessions, real commits: the row is created under a throwaway
``agent_id`` and deleted in the fixture teardown.
"""

from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import delete, select, text
from sqlalchemy.exc import DBAPIError

from nous.heartbeat.dynamic import DynamicCheckLoader
from nous.heartbeat.registry import CheckRegistry
from nous.storage.models import DynamicCheckModel

pytestmark = pytest.mark.postgres_only

WAIT = 10.0
NAME = "racy"


class _HeldAtCommit:
    """The real database, except that once ``hold_next()`` is called the next
    session it hands out stops just before its commit until ``release`` is
    set: the window between a disable's SELECT and its commit, held open."""

    def __init__(self, db) -> None:
        self._db = db
        self._hold_next = False
        self._hold_after_read = False
        self.at_commit = asyncio.Event()
        self.at_read = asyncio.Event()
        self.release = asyncio.Event()

    def hold_next(self) -> None:
        self._hold_next = True

    def hold_next_after_read(self) -> None:
        """Stop the next session earlier instead: as soon as its first
        statement has returned. Every action has one, ``list`` included."""
        self._hold_after_read = True

    @asynccontextmanager
    async def session(self):
        async with self._db.session() as session:
            if self._hold_next:
                self._hold_next = False
                commit = session.commit

                async def held_commit() -> None:
                    self.at_commit.set()
                    await self.release.wait()
                    await commit()

                session.commit = held_commit
            if self._hold_after_read:
                self._hold_after_read = False
                execute = session.execute

                async def held_execute(*args, **kwargs):
                    result = await execute(*args, **kwargs)
                    if not self.at_read.is_set():
                        self.at_read.set()
                        await self.release.wait()
                    return result

                session.execute = held_execute
            yield session


@pytest_asyncio.fixture
async def world(db):
    agent = f"test-fixc-{uuid.uuid4().hex[:10]}"
    held = _HeldAtCommit(db)
    registry = CheckRegistry()
    loader = DynamicCheckLoader(db=held, registry=registry, runner=AsyncMock(), agent_id=agent)
    await loader.create_check(name=NAME, description="d", prompt="p", interval_seconds=300)
    # A recorded (revertible) disable: the row is now disabled and carries the
    # token a revert must match.
    capture: dict = {}
    await loader.manage_check(action="disable", name=NAME, capture=capture)
    written = capture["written"]

    async def row() -> tuple[bool, str]:
        async with db.session() as session:
            enabled, metadata = (
                await session.execute(
                    select(DynamicCheckModel.enabled, DynamicCheckModel.metadata_)
                    .where(DynamicCheckModel.agent_id == agent)
                    .where(DynamicCheckModel.name == NAME)
                )
            ).one()
        return enabled, metadata["enabled_state_token"]

    try:
        yield loader, held, registry, written["check_id"], written["enabled_state_token"], row
    finally:
        held.release.set()
        async with db.session() as session:
            await session.execute(delete(DynamicCheckModel).where(DynamicCheckModel.agent_id == agent))
            await session.commit()


async def _revert_committed_or_blocked(db, row) -> str:
    """Wait until the revert's UPDATE has either committed (the row reads
    enabled) or is waiting on the row lock. Either is a settled state: the
    held disable can be released without racing the revert."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + WAIT
    while loop.time() < deadline:
        if (await row())[0]:
            return "committed"
        async with db.session() as session:
            waiting = (
                await session.execute(
                    text(
                        "SELECT count(*) FROM pg_stat_activity "
                        "WHERE datname = current_database() AND wait_event_type = 'Lock' "
                        "AND query ILIKE '%dynamic_checks%'"
                    )
                )
            ).scalar_one()
        if waiting:
            return "blocked"
        await asyncio.sleep(0.02)
    pytest.fail(f"the revert neither committed nor blocked (waited {WAIT:.0f}s)")


async def test_a_disable_holding_its_row_is_not_undone_by_a_concurrent_revert(db, world):
    loader, held, registry, check_id, token, row = world
    assert await row() == (False, token)

    # The DAG's own disable of the already-disabled check (no capture), held
    # between its SELECT and its commit.
    held.hold_next()
    disable = asyncio.create_task(loader.manage_check(action="disable", name=NAME))
    await asyncio.wait_for(held.at_commit.wait(), WAIT)

    # The revert of the recorded disable lands in that window.
    revert = asyncio.create_task(loader.enable_if_unchanged(NAME, check_id, token))
    how = await _revert_committed_or_blocked(db, row)

    held.release.set()
    result = await asyncio.wait_for(disable, WAIT)
    reverted = await asyncio.wait_for(revert, WAIT)

    enabled, final_token = await row()
    assert result == {"status": "disabled", "name": NAME}
    assert enabled is False, "manage_check reported 'disabled' but the row is enabled: the disable was lost"
    assert reverted is False, "the revert re-enabled a check that was disabled again after the state it recorded"
    assert how == "blocked", "the revert committed inside the disable's read-modify-write window"
    assert final_token != token
    assert registry.get_check(NAME) is None


async def test_a_revert_that_commits_first_is_seen_by_the_next_disable(world):
    """The other order, and the first test to run enable_if_unchanged's SQL."""
    loader, _held, registry, check_id, token, row = world

    assert await loader.enable_if_unchanged(NAME, check_id, token) is True
    assert (await row())[0] is True
    assert registry.get_check(NAME) is not None

    assert await loader.manage_check(action="disable", name=NAME) == {"status": "disabled", "name": NAME}

    assert (await row())[0] is False
    assert registry.get_check(NAME) is None
    # The token moved twice since the recorded disable: its revert is refused now.
    assert await loader.enable_if_unchanged(NAME, check_id, token) is False


async def _row_is_locked(db, check_id: str) -> bool:
    """Whether another transaction holds the check's row: a third session's
    ``SELECT ... FOR UPDATE NOWAIT`` fails at once if one does."""
    async with db.session() as session:
        try:
            await session.execute(
                select(DynamicCheckModel.id)
                .where(DynamicCheckModel.id == uuid.UUID(check_id))
                .with_for_update(nowait=True)
            )
        except DBAPIError as exc:
            if "could not obtain lock" not in str(exc):
                raise
            return True
        finally:
            await session.rollback()  # the probe must not keep the row itself
    return False


@pytest.mark.parametrize("action", ["enable", "disable", "delete", "update"])
async def test_every_state_changing_action_holds_the_row_from_its_read(db, world, action):
    """Each of these actions is a read-modify-write of the row, so each locks
    it at its read: with the action stopped right after that read, nothing
    written yet, a third session cannot lock the row."""
    loader, held, _registry, check_id, _token, _row = world
    updates = {"description": "changed"} if action == "update" else None

    held.hold_next_after_read()
    acting = asyncio.create_task(loader.manage_check(action=action, name=NAME, updates=updates))
    await asyncio.wait_for(held.at_read.wait(), WAIT)
    locked = await _row_is_locked(db, check_id)

    held.release.set()
    await asyncio.wait_for(acting, WAIT)

    assert locked, f"{action}: the row is not locked between the read and the commit"
    assert not await _row_is_locked(db, check_id)  # released by the commit


async def test_list_takes_no_row_lock(db, world):
    """Guard: ``list`` only reads. Stopped right after its read, it holds no row."""
    loader, held, _registry, check_id, _token, _row = world

    held.hold_next_after_read()
    listing = asyncio.create_task(loader.manage_check(action="list"))
    await asyncio.wait_for(held.at_read.wait(), WAIT)
    locked = await _row_is_locked(db, check_id)

    held.release.set()
    listed = await asyncio.wait_for(listing, WAIT)

    assert not locked
    assert [check["name"] for check in listed["checks"]] == [NAME]
