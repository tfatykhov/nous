"""Tests for F049 Mechanism A — WorkingMemoryManager.cleanup_stale().

Safety-net sweep that deletes stale heart.working_memory rows using a
batched DELETE (ctid IN (SELECT ... LIMIT N)) under a
pg_try_advisory_xact_lock keyed on agent_id.

All tests require PostgreSQL because they exercise:
- pg_try_advisory_xact_lock / pg_advisory_xact_lock (PG-only)
- ctid (PG-only physical row identifier)

Run with: uv run pytest tests/heart/test_working_memory_cleanup.py -v
(requires NOUS_TEST_DB=postgres).
"""

from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text

from nous.heart.working_memory import WorkingMemoryManager

pytestmark = [pytest.mark.postgres_only, pytest.mark.asyncio]


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def wm_manager(db):
    """WorkingMemoryManager bound to a unique agent_id per test, cleaned up after."""
    agent_id = f"test-wm-cleanup-{uuid4().hex[:8]}"
    mgr = WorkingMemoryManager(db, agent_id)
    yield mgr
    # Teardown: drop any rows the test left behind.
    async with db.session() as session:
        await session.execute(
            text("DELETE FROM heart.working_memory WHERE agent_id = :a"),
            {"a": agent_id},
        )
        await session.commit()


async def _insert_wm_row(
    db,
    agent_id: str,
    session_id: str,
    age_hours: float,
) -> None:
    """Insert a working_memory row with explicit updated_at = now - age_hours."""
    stale_ts = datetime.now(UTC) - timedelta(hours=age_hours)
    async with db.session() as session:
        # Use raw SQL so we can override the server_default updated_at
        await session.execute(
            text(
                """
                INSERT INTO heart.working_memory
                    (agent_id, session_id, items, open_threads, updated_at, created_at)
                VALUES
                    (:agent_id, :session_id, '[]'::jsonb, '[]'::jsonb, :ts, :ts)
                """
            ),
            {"agent_id": agent_id, "session_id": session_id, "ts": stale_ts},
        )
        await session.commit()


async def _count_wm_rows(db, agent_id: str) -> int:
    async with db.session() as session:
        result = await session.execute(
            text("SELECT COUNT(*) FROM heart.working_memory WHERE agent_id = :a"),
            {"a": agent_id},
        )
        return result.scalar() or 0


# ---------------------------------------------------------------------------
# 1. test_cleanup_stale_deletes_old_rows
# ---------------------------------------------------------------------------


async def test_cleanup_stale_deletes_old_rows(wm_manager, db):
    """A row with updated_at = now - 25h must be deleted by cleanup_stale(24)."""
    await _insert_wm_row(db, wm_manager.agent_id, "stale-session-a", age_hours=25)
    assert await _count_wm_rows(db, wm_manager.agent_id) == 1

    deleted = await wm_manager.cleanup_stale(max_age_hours=24)

    assert deleted == 1
    assert await _count_wm_rows(db, wm_manager.agent_id) == 0


# ---------------------------------------------------------------------------
# 2. test_cleanup_stale_preserves_fresh_rows
# ---------------------------------------------------------------------------


async def test_cleanup_stale_preserves_fresh_rows(wm_manager, db):
    """A row with updated_at = now - 12h must be preserved by cleanup_stale(24)."""
    await _insert_wm_row(db, wm_manager.agent_id, "fresh-session-a", age_hours=12)
    assert await _count_wm_rows(db, wm_manager.agent_id) == 1

    deleted = await wm_manager.cleanup_stale(max_age_hours=24)

    assert deleted == 0
    assert await _count_wm_rows(db, wm_manager.agent_id) == 1


# ---------------------------------------------------------------------------
# 3. test_cleanup_stale_agent_id_isolation
# ---------------------------------------------------------------------------


async def test_cleanup_stale_agent_id_isolation(db):
    """Cleanup must only touch rows for the manager's agent_id."""
    agent_a = f"test-wm-iso-a-{uuid4().hex[:8]}"
    agent_b = f"test-wm-iso-b-{uuid4().hex[:8]}"
    mgr_a = WorkingMemoryManager(db, agent_a)

    try:
        await _insert_wm_row(db, agent_a, "a-stale", age_hours=25)
        await _insert_wm_row(db, agent_b, "b-stale", age_hours=25)
        assert await _count_wm_rows(db, agent_a) == 1
        assert await _count_wm_rows(db, agent_b) == 1

        deleted = await mgr_a.cleanup_stale(max_age_hours=24)

        assert deleted == 1
        assert await _count_wm_rows(db, agent_a) == 0
        # agent_b's stale row untouched — isolation holds.
        assert await _count_wm_rows(db, agent_b) == 1
    finally:
        async with db.session() as session:
            await session.execute(
                text("DELETE FROM heart.working_memory WHERE agent_id IN (:a, :b)"),
                {"a": agent_a, "b": agent_b},
            )
            await session.commit()


# ---------------------------------------------------------------------------
# 4. test_cleanup_stale_zero_hours_is_noop
# ---------------------------------------------------------------------------


async def test_cleanup_stale_zero_hours_is_noop(wm_manager, db):
    """max_age_hours=0 must return 0 without issuing a DELETE."""
    await _insert_wm_row(db, wm_manager.agent_id, "any-session", age_hours=100)
    assert await _count_wm_rows(db, wm_manager.agent_id) == 1

    deleted = await wm_manager.cleanup_stale(max_age_hours=0)

    assert deleted == 0
    # Row preserved: sweep was disabled, no DELETE issued.
    assert await _count_wm_rows(db, wm_manager.agent_id) == 1


# ---------------------------------------------------------------------------
# 5. test_cleanup_stale_batch_limit
# ---------------------------------------------------------------------------


async def test_cleanup_stale_batch_limit(wm_manager, db):
    """Batching path must correctly iterate when rows > batch_size.

    NOTE: the spec-level acceptance criterion says 12 000 rows across 3 batches
    of 5 000, but inserting 12 000 rows is CI-hostile (slow + bloats the
    heart.working_memory table for other tests). We exercise the exact same
    code path with batch_size=5 and 12 rows → 3 batches (5 + 5 + 2), which
    proves the while-loop termination, the LIMIT, and the cumulative counter
    without the wall-clock cost.
    """
    n_rows = 12
    batch = 5
    for i in range(n_rows):
        await _insert_wm_row(db, wm_manager.agent_id, f"batch-session-{i}", age_hours=48)
    assert await _count_wm_rows(db, wm_manager.agent_id) == n_rows

    deleted = await wm_manager.cleanup_stale(max_age_hours=24, batch_size=batch)

    assert deleted == n_rows
    assert await _count_wm_rows(db, wm_manager.agent_id) == 0


# ---------------------------------------------------------------------------
# 6. test_cleanup_stale_advisory_lock_prevents_concurrent
# ---------------------------------------------------------------------------


async def test_cleanup_stale_advisory_lock_prevents_concurrent(wm_manager, db, caplog):
    """When a peer holds the advisory lock, cleanup_stale returns 0 without deleting.

    Opens a second DB session, acquires pg_advisory_xact_lock with the same
    key the impl will compute (SHA-256 → 31-bit int), then invokes
    cleanup_stale. Assert return == 0, stale row is preserved, and the
    DEBUG "another replica holds" log was emitted. Release by committing.
    """
    # Seed a stale row that WOULD be deleted if the lock weren't held.
    await _insert_wm_row(db, wm_manager.agent_id, "lock-contention", age_hours=48)
    assert await _count_wm_rows(db, wm_manager.agent_id) == 1

    # Compute the same lock key cleanup_stale will use. Must match impl
    # (SHA-256 of agent_id, first 4 bytes big-endian, mod 2**31) — builtin
    # hash() is randomized per-process and cannot be used here.
    digest = hashlib.sha256(wm_manager.agent_id.encode("utf-8")).digest()
    lock_key = int.from_bytes(digest[:4], "big") % (2**31)

    # Hold the lock in a second session, then call cleanup_stale.
    async with db.session() as holder_session:
        # Begin an explicit transaction so pg_advisory_xact_lock sticks.
        await holder_session.execute(
            text("SELECT pg_advisory_xact_lock(:k)").bindparams(k=lock_key)
        )

        with caplog.at_level(logging.DEBUG, logger="nous.heart.working_memory"):
            deleted = await wm_manager.cleanup_stale(max_age_hours=24)

        # Lock was held by holder_session → cleanup_stale must bail out.
        assert deleted == 0
        # Row preserved since the DELETE never ran.
        assert await _count_wm_rows(db, wm_manager.agent_id) == 1
        # DEBUG log confirming the bail-out path.
        assert any(
            "another replica holds" in rec.getMessage()
            for rec in caplog.records
        ), f"Expected DEBUG 'another replica holds' log; got: {[r.getMessage() for r in caplog.records]}"

        # Release by committing the holder session (xact_lock auto-releases).
        await holder_session.commit()

    # After release, a second cleanup should now succeed.
    deleted_after = await wm_manager.cleanup_stale(max_age_hours=24)
    assert deleted_after == 1
    assert await _count_wm_rows(db, wm_manager.agent_id) == 0
