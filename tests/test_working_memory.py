"""Tests for WorkingMemoryManager — current session state.

All tests use real Postgres via the SAVEPOINT fixture from conftest.py.
Heart methods receive the test session via the session parameter (P1-1).
"""

import uuid
from datetime import UTC, datetime

from nous.heart import (
    OpenThread,
    WorkingMemoryItem,
    WorkingMemoryState,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_item(type_: str = "fact", relevance: float = 0.5, **overrides) -> WorkingMemoryItem:
    """Build a WorkingMemoryItem with sensible defaults."""
    defaults = dict(
        type=type_,
        ref_id=uuid.uuid4(),
        summary="Test item",
        relevance=relevance,
        loaded_at=datetime.now(UTC),
    )
    defaults.update(overrides)
    return WorkingMemoryItem(**defaults)


# ---------------------------------------------------------------------------
# 1. test_get_or_create
# ---------------------------------------------------------------------------


async def test_get_or_create(heart, session):
    """Creates new if missing, returns existing if present."""
    sid = f"test-session-{uuid.uuid4().hex[:8]}"

    # First call creates
    state1 = await heart.get_or_create_working_memory(sid, session=session)
    assert isinstance(state1, WorkingMemoryState)
    assert state1.session_id == sid
    assert state1.items == []

    # Second call returns existing
    state2 = await heart.get_or_create_working_memory(sid, session=session)
    assert state2.session_id == sid


# ---------------------------------------------------------------------------
# 2. test_focus_sets_task
# ---------------------------------------------------------------------------


async def test_focus_sets_task(heart, session):
    """current_task and current_frame updated."""
    sid = f"test-session-{uuid.uuid4().hex[:8]}"
    await heart.get_or_create_working_memory(sid, session=session)

    state = await heart.focus(
        sid,
        task="Implement login page",
        frame="web-development",
        session=session,
    )

    assert state.current_task == "Implement login page"
    assert state.current_frame == "web-development"


# ---------------------------------------------------------------------------
# 3. test_load_item
# ---------------------------------------------------------------------------


async def test_load_item(heart, session):
    """Item added to items array."""
    sid = f"test-session-{uuid.uuid4().hex[:8]}"
    await heart.get_or_create_working_memory(sid, session=session)

    item = _make_item(summary="Important fact")
    state = await heart.load_to_working_memory(sid, item, session=session)

    assert state.item_count == 1
    assert state.items[0].summary == "Important fact"


# ---------------------------------------------------------------------------
# 4. test_capacity_eviction
# ---------------------------------------------------------------------------


async def test_capacity_eviction(heart, session):
    """At max_items, lowest relevance evicted before new add."""
    sid = f"test-session-{uuid.uuid4().hex[:8]}"
    await heart.get_or_create_working_memory(sid, session=session)

    # Working memory default max_items is 20.
    # Load 20 items with varying relevance.
    for i in range(20):
        item = _make_item(
            summary=f"Item {i}",
            relevance=0.5,
            ref_id=uuid.uuid4(),
        )
        await heart.load_to_working_memory(sid, item, session=session)

    # Load one more item with low relevance to add —
    # should evict the lowest relevance item first, then add new one
    low_rel_item = _make_item(
        summary="Low relevance to be evicted",
        relevance=0.1,
        ref_id=uuid.uuid4(),
    )
    await heart.load_to_working_memory(sid, low_rel_item, session=session)

    # Now add another — the 0.1 relevance item should be evicted
    new_item = _make_item(
        summary="New high relevance item",
        relevance=0.9,
        ref_id=uuid.uuid4(),
    )
    state = await heart.load_to_working_memory(sid, new_item, session=session)

    # Should still be at max (20) — evicted the lowest
    assert state.item_count <= 20
    # The new high relevance item should be present
    summaries = [item.summary for item in state.items]
    assert "New high relevance item" in summaries


# ---------------------------------------------------------------------------
# 5. test_evict_specific
# ---------------------------------------------------------------------------


async def test_evict_specific(heart, session):
    """Remove by ref_id."""
    sid = f"test-session-{uuid.uuid4().hex[:8]}"
    await heart.get_or_create_working_memory(sid, session=session)

    target_ref = uuid.uuid4()
    item1 = _make_item(summary="Keep this", ref_id=uuid.uuid4())
    item2 = _make_item(summary="Remove this", ref_id=target_ref)

    await heart.load_to_working_memory(sid, item1, session=session)
    await heart.load_to_working_memory(sid, item2, session=session)

    state = await heart.evict_from_working_memory(sid, ref_id=target_ref, session=session)

    assert state.item_count == 1
    assert state.items[0].summary == "Keep this"


# ---------------------------------------------------------------------------
# 6. test_add_thread
# ---------------------------------------------------------------------------


async def test_add_thread(heart, session):
    """Thread added to open_threads via Heart delegation."""
    sid = f"test-session-{uuid.uuid4().hex[:8]}"
    await heart.get_or_create_working_memory(sid, session=session)

    thread = OpenThread(
        description="Investigate memory leak",
        priority="high",
        created_at=datetime.now(UTC),
    )
    state = await heart.add_thread(sid, thread, session=session)

    assert len(state.open_threads) == 1
    assert state.open_threads[0].description == "Investigate memory leak"
    assert state.open_threads[0].priority == "high"


# ---------------------------------------------------------------------------
# 7. test_resolve_thread
# ---------------------------------------------------------------------------


async def test_resolve_thread(heart, session):
    """Thread removed by description match via Heart delegation."""
    sid = f"test-session-{uuid.uuid4().hex[:8]}"
    await heart.get_or_create_working_memory(sid, session=session)

    thread1 = OpenThread(
        description="Fix the login bug",
        priority="high",
        created_at=datetime.now(UTC),
    )
    thread2 = OpenThread(
        description="Review PR #42",
        priority="medium",
        created_at=datetime.now(UTC),
    )

    await heart.add_thread(sid, thread1, session=session)
    await heart.add_thread(sid, thread2, session=session)

    # Resolve by matching description (case-insensitive contains)
    state = await heart.resolve_thread(sid, "login bug", session=session)

    assert len(state.open_threads) == 1
    assert state.open_threads[0].description == "Review PR #42"


# ---------------------------------------------------------------------------
# 8. test_clear
# ---------------------------------------------------------------------------


async def test_clear(heart, session):
    """Row deleted."""
    sid = f"test-session-{uuid.uuid4().hex[:8]}"
    await heart.get_or_create_working_memory(sid, session=session)

    await heart.clear_working_memory(sid, session=session)

    state = await heart.get_working_memory(sid, session=session)
    assert state is None


# ---------------------------------------------------------------------------
# Regression: F055 residual rows with loaded_at=None must not 500
# ---------------------------------------------------------------------------


async def test_to_state_tolerates_null_loaded_at_residual_row(
    heart, session, caplog,
):
    """F055's record_surfaced used to write items with ``loaded_at=None``,
    which caused _to_state's pydantic parse to raise ValidationError,
    500'ing /status?dashboard=true and pre_turn WM init.

    Verify _to_state now coerces the None to a real timestamp + emits a
    WARN, instead of raising. (Writer fixed at
    residual_activation.py:248; this is the read-side defense.)"""
    import logging

    from sqlalchemy import select
    from nous.storage.models import WorkingMemory

    sid = f"test-residual-null-{uuid.uuid4().hex[:8]}"
    await heart.get_or_create_working_memory(sid, session=session)

    # Inject a bad row that mimics the historical F055 writer.
    bad_item = {
        "type": "fact",
        "ref_id": str(uuid.uuid4()),
        "summary": "residual fact",
        "relevance": 0.5,
        "loaded_at": None,  # ← the historical bug
        "activation": 0.5,
        "last_surfaced_turn": 7,
    }
    result = await session.execute(
        select(WorkingMemory)
        .where(WorkingMemory.session_id == sid)
        .where(WorkingMemory.agent_id == heart.agent_id)
    )
    wm = result.scalars().one()
    wm.items = [bad_item]
    await session.flush()

    # Now reading must not raise.
    with caplog.at_level(logging.WARNING, logger="nous.heart.working_memory"):
        state = await heart.get_working_memory(sid, session=session)

    assert state is not None
    assert len(state.items) == 1
    assert state.items[0].loaded_at is not None
    assert state.items[0].relevance == 0.5  # other fields preserved
    warn_msgs = [r.getMessage() for r in caplog.records
                 if r.levelname == "WARNING"]
    assert any("loaded_at=None" in m for m in warn_msgs), (
        f"expected WARN about loaded_at=None, got {warn_msgs}"
    )
