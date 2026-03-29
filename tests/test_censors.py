"""Tests for CensorManager — things NOT to do.

All tests use real Postgres via the SAVEPOINT fixture from conftest.py.
Heart methods receive the test session via the session parameter (P1-1).

Key MockEmbeddingProvider behavior:
- Identical text = cosine 1.0 (matches > 0.7 threshold)
- Different text = cosine ~0.0 (no match)
"""

from sqlalchemy import select

from nous.heart import (
    CensorDetail,
    CensorInput,
)
from nous.storage.models import Censor

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _censor_input(**overrides) -> CensorInput:
    """Build a CensorInput with sensible defaults."""
    defaults = dict(
        trigger_pattern="never deploy on Friday",
        reason="Deployments on Friday risk weekend outages",
        action="warn",
        domain="operations",
    )
    defaults.update(overrides)
    return CensorInput(**defaults)


# ---------------------------------------------------------------------------
# 1. test_add_censor
# ---------------------------------------------------------------------------


async def test_add_censor(heart, session):
    """Creates with correct fields."""
    inp = _censor_input()
    detail = await heart.add_censor(inp, session=session)

    assert isinstance(detail, CensorDetail)
    assert detail.trigger_pattern == "never deploy on Friday"
    assert detail.reason == "Deployments on Friday risk weekend outages"
    assert detail.action == "warn"
    assert detail.domain == "operations"
    assert detail.activation_count == 0
    assert detail.false_positive_count == 0
    assert detail.active is True
    assert detail.escalation_threshold == 3


# ---------------------------------------------------------------------------
# 2. test_check_matches
# ---------------------------------------------------------------------------


async def test_check_matches(heart, session):
    """Censor with similar trigger fires (use IDENTICAL text for mock embeddings)."""
    inp = _censor_input(
        trigger_pattern="never use eval in production",
        reason="eval is a security risk",
    )
    await heart.add_censor(inp, session=session)

    # Use IDENTICAL text — mock embeddings produce cosine 1.0 > 0.7 threshold
    matches = await heart.check_censors(
        "never use eval in production eval is a security risk",
        session=session,
    )
    assert len(matches) >= 1
    assert any(m.trigger_pattern == "never use eval in production" for m in matches)


# ---------------------------------------------------------------------------
# 3. test_check_no_match
# ---------------------------------------------------------------------------


async def test_check_no_match(heart, session):
    """Unrelated text doesn't trigger."""
    await heart.add_censor(
        _censor_input(
            trigger_pattern="avoid recursive imports",
            reason="causes circular dependency",
        ),
        session=session,
    )

    # Completely different text — cosine ~0.0 < 0.7 threshold
    matches = await heart.check_censors(
        "the weather is sunny today and I like ice cream",
        session=session,
    )
    # Should not match
    matched_triggers = [m.trigger_pattern for m in matches]
    assert "avoid recursive imports" not in matched_triggers


# ---------------------------------------------------------------------------
# 4. test_activation_count_incremented
# ---------------------------------------------------------------------------


async def test_activation_count_incremented(heart, session):
    """Counter goes up on match."""
    censor = await heart.add_censor(
        _censor_input(
            trigger_pattern="count test censor trigger",
            reason="count test reason",
        ),
        session=session,
    )
    assert censor.activation_count == 0

    # Trigger it with identical text
    await heart.check_censors(
        "count test censor trigger count test reason",
        session=session,
    )

    # Re-read to check counter
    updated = await session.execute(select(Censor).where(Censor.id == censor.id))
    c = updated.scalar_one()
    assert (c.activation_count or 0) >= 1


# ---------------------------------------------------------------------------
# 5. test_auto_escalation
# ---------------------------------------------------------------------------


async def test_auto_escalation(heart, session):
    """After threshold triggers, warn -> block."""
    censor = await heart.add_censor(
        _censor_input(
            trigger_pattern="escalation test censor trigger",
            reason="escalation test reason",
        ),
        session=session,
    )
    assert censor.action == "warn"

    # Trigger enough times to cross threshold (default=3)
    for _ in range(3):
        matches = await heart.check_censors(
            "escalation test censor trigger escalation test reason",
            session=session,
        )

    # After 3 triggers, should auto-escalate from warn to block
    assert len(matches) >= 1
    assert matches[0].action == "block"


# ---------------------------------------------------------------------------
# 6. test_false_positive_tracking
# ---------------------------------------------------------------------------


async def test_false_positive_tracking(heart, session):
    """Count increments."""
    censor = await heart.add_censor(
        _censor_input(
            trigger_pattern="false positive test censor",
            reason="fp test reason",
        ),
        session=session,
    )

    updated = await heart.record_false_positive(censor.id, session=session)
    assert updated.false_positive_count == 1

    updated2 = await heart.record_false_positive(censor.id, session=session)
    assert updated2.false_positive_count == 2


# ---------------------------------------------------------------------------
# 7. test_manual_escalation
# ---------------------------------------------------------------------------


async def test_manual_escalation(heart, session):
    """warn -> block -> absolute."""
    censor = await heart.add_censor(
        _censor_input(
            trigger_pattern="manual escalation test",
            reason="test reason",
            action="warn",
        ),
        session=session,
    )
    assert censor.action == "warn"

    escalated1 = await heart.escalate_censor(censor.id, session=session)
    assert escalated1.action == "block"

    escalated2 = await heart.escalate_censor(censor.id, session=session)
    assert escalated2.action == "absolute"


# ---------------------------------------------------------------------------
# 8. test_escalation_no_downgrade
# ---------------------------------------------------------------------------


async def test_escalation_no_downgrade(heart, session):
    """block cannot go back to warn."""
    censor = await heart.add_censor(
        _censor_input(
            trigger_pattern="no downgrade test",
            reason="test reason",
            action="block",
        ),
        session=session,
    )
    assert censor.action == "block"

    # Escalate — should go to absolute, never back to warn
    escalated = await heart.escalate_censor(censor.id, session=session)
    assert escalated.action == "absolute"

    # Escalate again — should stay absolute
    escalated2 = await heart.escalate_censor(censor.id, session=session)
    assert escalated2.action == "absolute"


# ---------------------------------------------------------------------------
# 9. test_inactive_censor_skipped
# ---------------------------------------------------------------------------


async def test_inactive_censor_skipped(heart, session):
    """Deactivated censors don't match."""
    censor = await heart.add_censor(
        _censor_input(
            trigger_pattern="inactive censor test trigger",
            reason="inactive censor test reason",
        ),
        session=session,
    )

    # Deactivate
    await heart.deactivate_censor(censor.id, session=session)

    # Should not match even with identical text
    matches = await heart.check_censors(
        "inactive censor test trigger inactive censor test reason",
        session=session,
    )
    matched_ids = [m.id for m in matches]
    assert censor.id not in matched_ids


# ---------------------------------------------------------------------------
# 10. test_domain_filter
# ---------------------------------------------------------------------------


async def test_domain_filter(heart, session):
    """Only censors in matching domain trigger."""
    await heart.add_censor(
        _censor_input(
            trigger_pattern="domain filter censor ops",
            reason="domain filter reason ops",
            domain="operations",
        ),
        session=session,
    )
    await heart.add_censor(
        _censor_input(
            trigger_pattern="domain filter censor dev",
            reason="domain filter reason dev",
            domain="development",
        ),
        session=session,
    )

    # Check with domain=development — should only match dev censor
    matches = await heart.check_censors(
        "domain filter censor dev domain filter reason dev",
        domain="development",
        session=session,
    )
    for m in matches:
        # Only development or NULL domain censors should match
        assert m.domain in ("development", None)


# ---------------------------------------------------------------------------
# 11. test_search_read_only (P1-5)
# ---------------------------------------------------------------------------


async def test_search_read_only(heart, session):
    """search() does NOT increment activation_count."""
    censor = await heart.add_censor(
        _censor_input(
            trigger_pattern="read only search test censor",
            reason="read only search test reason",
        ),
        session=session,
    )
    initial_count = censor.activation_count

    # Use heart.censors.search() directly (read-only)
    await heart.censors.search(
        "read only search test censor read only search test reason",
        session=session,
    )

    # Re-read to check counter unchanged
    updated = await session.execute(select(Censor).where(Censor.id == censor.id))
    c = updated.scalar_one()
    assert (c.activation_count or 0) == initial_count


# ---------------------------------------------------------------------------
# 12. test_keyword_fallback_for_null_embeddings
# ---------------------------------------------------------------------------


async def test_keyword_fallback_for_null_embeddings(heart, session):
    """Censors without embeddings still match via keyword (ILIKE) fallback."""
    # Create censor normally (gets embedding from mock provider)
    censor = await heart.add_censor(
        _censor_input(
            trigger_pattern="never rebase main",
            reason="rebasing main breaks shared history",
        ),
        session=session,
    )

    # Null out the embedding to simulate a censor created without embeddings
    await session.execute(
        select(Censor).where(Censor.id == censor.id)
    )
    from sqlalchemy import update
    await session.execute(
        update(Censor).where(Censor.id == censor.id).values(embedding=None)
    )
    await session.flush()

    # Semantic match will skip this censor (embedding IS NULL),
    # but keyword matching should find "never rebase main" as a substring
    matches = await heart.check_censors(
        "I think we should never rebase main because it causes issues",
        session=session,
    )
    assert len(matches) >= 1
    assert any(m.trigger_pattern == "never rebase main" for m in matches)
