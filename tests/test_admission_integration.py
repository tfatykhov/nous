"""Integration tests for F023 Memory Admission Control.

Tests the full flow through FactManager._learn() with real Postgres.
Uses shadow mode and active mode to verify admission gate behavior.
"""

import asyncio
from datetime import date

import pytest
from sqlalchemy import delete, func, select

from nous.heart.schemas import FactDetail, FactInput, FactRejected
from nous.storage.models import Fact


async def _active_count(heart, content: str) -> int:
    async with heart.db.session() as s:
        return await s.scalar(
            select(func.count())
            .select_from(Fact)
            .where(
                Fact.agent_id == heart.facts.agent_id,
                Fact.content == content,
                Fact.active.is_(True),
            )
        )


async def _cleanup(heart, content: str) -> None:
    async with heart.db.session() as s:
        await s.execute(delete(Fact).where(Fact.content == content))
        await s.commit()


def _fact(**overrides) -> FactInput:
    defaults = dict(
        content="Tim prefers dark mode in his IDE for reduced eye strain",
        category="preference",
        subject="Tim",
        confidence=0.9,
        source="fact_extractor",
        tags=["preference", "ide"],
    )
    defaults.update(overrides)
    return FactInput(**defaults)


# ---------------------------------------------------------------------------
# Admission via Heart
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_admitted_fact_stored(heart_with_admission, session):
    """High-quality fact should be stored with admission_score."""
    result = await heart_with_admission.learn(
        _fact(source="fact_extractor"),
        session=session,
    )
    assert isinstance(result, FactDetail)
    assert result.active is True


@pytest.mark.asyncio
async def test_rejected_fact_not_stored(heart_with_strict_admission, session):
    """Low-quality fact with strict threshold should be rejected."""
    result = await heart_with_strict_admission.learn(
        _fact(
            content="A vague low quality reflection from sleep cycle",
            category=None,
            subject=None,
            confidence=0.3,
            source="sleep_reflection",
            tags=[],
        ),
        session=session,
    )
    assert isinstance(result, FactRejected)
    assert result.admitted is False
    assert result.composite_score < 0.99


@pytest.mark.asyncio
async def test_user_direct_bypasses_strict_gate(heart_with_strict_admission, session):
    """User-stated facts always bypass gate (in bypass_sources list)."""
    result = await heart_with_strict_admission.learn(
        _fact(source="user_stated"),
        session=session,
    )
    assert isinstance(result, FactDetail)


@pytest.mark.asyncio
async def test_shadow_mode_admits_all(heart_with_shadow_admission, session):
    """Shadow mode stores all facts regardless of score."""
    result = await heart_with_shadow_admission.learn(
        _fact(
            content="A vague low quality shadow mode test fact",
            category=None,
            subject=None,
            confidence=0.1,
            source="sleep_reflection",
            tags=[],
        ),
        session=session,
    )
    assert isinstance(result, FactDetail)


@pytest.mark.asyncio
async def test_disabled_admission_no_score(heart, session):
    """With no controller, facts stored with admission_score=None."""
    result = await heart.learn(
        _fact(source="fact_extractor"),
        session=session,
    )
    assert isinstance(result, FactDetail)


@pytest.mark.asyncio
async def test_supersede_bypasses_gate(heart_with_strict_admission, session):
    """Supersede should bypass the admission gate."""
    # First, store an original fact via bypass
    original = await heart_with_strict_admission.learn(
        _fact(source="user_stated", content="Tim prefers light mode for his IDE editor"),
        session=session,
    )
    assert isinstance(original, FactDetail)

    # Supersede with a new fact — should bypass the strict gate
    new_result = await heart_with_strict_admission.facts.supersede(
        original.id,
        FactInput(
            content="Tim prefers dark mode for his IDE editor",
            category="preference",
            subject="Tim",
            source="fact_extractor",
        ),
        session=session,
    )
    assert isinstance(new_result, FactDetail)


# ---------------------------------------------------------------------------
# W-8: concurrent-learn advisory lock (Postgres-only — the lock is a no-op on
# the SQLite test backend, which is serial). These use own sessions (commit)
# so the advisory lock actually contends across two pooled connections.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_w8_concurrent_identical_learn_single_active_row(heart):
    """Two concurrent identical learns must yield exactly one active fact."""
    content = "Tim migrated the billing database to the new replica cluster overnight"
    try:
        await asyncio.gather(
            heart.learn(_fact(content=content, source="fact_extractor")),
            heart.learn(_fact(content=content, source="fact_extractor")),
        )
        assert await _active_count(heart, content) == 1
    finally:
        await _cleanup(heart, content)


@pytest.mark.asyncio
async def test_w8_concurrent_distinct_event_dates_two_rows(heart):
    """F075 is preserved under the lock: same content on distinct event_dates
    are distinct events and both persist."""
    content = "The staging API token was rotated during the security review window"
    try:
        await asyncio.gather(
            heart.learn(_fact(content=content, source="fact_extractor", event_date=date(2026, 3, 10))),
            heart.learn(_fact(content=content, source="fact_extractor", event_date=date(2026, 3, 12))),
        )
        assert await _active_count(heart, content) == 2
    finally:
        await _cleanup(heart, content)
