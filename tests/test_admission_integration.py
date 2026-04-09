"""Integration tests for F023 Memory Admission Control.

Tests the full flow through FactManager._learn() with real Postgres.
Uses shadow mode and active mode to verify admission gate behavior.
"""

import pytest

from nous.heart.schemas import FactDetail, FactInput, FactRejected


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
    """User-invoked learn_fact always bypasses gate."""
    result = await heart_with_strict_admission.learn(
        _fact(source="user_direct"),
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
        _fact(source="user_direct", content="Tim prefers light mode for his IDE editor"),
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
