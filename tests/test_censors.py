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


# ---------------------------------------------------------------------------
# 13. test_keyword_match_pipe_separated_pattern (Issue #199)
# ---------------------------------------------------------------------------


async def test_keyword_match_pipe_separated_pattern(heart, session):
    """Pipe-separated patterns match individual keywords via regex."""
    from sqlalchemy import update

    censor = await heart.add_censor(
        _censor_input(
            trigger_pattern="api_key|token|secret|password",
            reason="Block credential exposure",
            action="block",
        ),
        session=session,
    )

    # Null out embedding so only keyword matching is used
    await session.execute(
        update(Censor).where(Censor.id == censor.id).values(embedding=None)
    )
    await session.flush()

    # Should match "token" from the pipe-separated pattern
    matches = await heart.check_censors(
        "here is my auth token for the service",
        session=session,
    )
    assert len(matches) >= 1
    assert any(m.trigger_pattern == "api_key|token|secret|password" for m in matches)


# ---------------------------------------------------------------------------
# 14. test_keyword_match_regex_wildcard_pattern (Issue #199)
# ---------------------------------------------------------------------------


async def test_keyword_match_regex_wildcard_pattern(heart, session):
    """Regex patterns with .* wildcards work correctly."""
    from sqlalchemy import update

    censor = await heart.add_censor(
        _censor_input(
            trigger_pattern="send.*email|smtp|mail.*to",
            reason="Email protection",
            action="warn",
        ),
        session=session,
    )

    await session.execute(
        update(Censor).where(Censor.id == censor.id).values(embedding=None)
    )
    await session.flush()

    matches = await heart.check_censors(
        "I want to send an email to the team",
        session=session,
    )
    assert len(matches) >= 1
    assert any(m.trigger_pattern == "send.*email|smtp|mail.*to" for m in matches)


# ---------------------------------------------------------------------------
# 15. test_keyword_match_invalid_regex_skipped (Issue #199)
# ---------------------------------------------------------------------------


async def test_keyword_match_invalid_regex_skipped(heart, session):
    """Invalid regex patterns are skipped without breaking other censors."""
    from sqlalchemy import update

    # Create a censor with invalid regex
    bad_censor = await heart.add_censor(
        _censor_input(
            trigger_pattern="[invalid(regex",
            reason="Bad pattern",
            action="warn",
        ),
        session=session,
    )

    # Create a valid censor that should still match
    good_censor = await heart.add_censor(
        _censor_input(
            trigger_pattern="password|secret",
            reason="Credential protection",
            action="block",
        ),
        session=session,
    )

    # Null out embeddings so only keyword matching is used
    await session.execute(
        update(Censor).where(Censor.id.in_([bad_censor.id, good_censor.id])).values(embedding=None)
    )
    await session.flush()

    # The bad regex should be skipped, but "password" should still match
    matches = await heart.check_censors(
        "my password is hunter2",
        session=session,
    )
    assert any(m.trigger_pattern == "password|secret" for m in matches)
    # The bad censor should NOT be in matches
    assert not any(m.id == bad_censor.id for m in matches)


# ---------------------------------------------------------------------------
# 16. test_keyword_match_no_false_positive (Issue #199)
# ---------------------------------------------------------------------------


async def test_keyword_match_no_false_positive(heart, session):
    """Pipe-separated pattern does NOT match unrelated text."""
    from sqlalchemy import update

    censor = await heart.add_censor(
        _censor_input(
            trigger_pattern="api_key|token|secret|password",
            reason="Block credential exposure",
            action="block",
        ),
        session=session,
    )

    await session.execute(
        update(Censor).where(Censor.id == censor.id).values(embedding=None)
    )
    await session.flush()

    matches = await heart.check_censors(
        "the weather is sunny today and I like ice cream",
        session=session,
    )
    assert not any(m.id == censor.id for m in matches)


# ---------------------------------------------------------------------------
# 17. test_censor_input_with_trigger_action (F031)
# ---------------------------------------------------------------------------


async def test_censor_input_with_trigger_action(heart, session):
    """CensorInput accepts trigger_action and action_instruction."""
    inp = CensorInput(
        trigger_pattern="citing.*source",
        reason="Verify citations",
        action="warn",
        trigger_action={"tool": "recall", "args": {"query": "citations", "limit": 5}},
        action_instruction="Verify all citations against recalled sources.",
    )
    detail = await heart.add_censor(inp, session=session)
    assert detail.trigger_action == {"tool": "recall", "args": {"query": "citations", "limit": 5}}
    assert detail.action_instruction == "Verify all citations against recalled sources."


# ---------------------------------------------------------------------------
# 18. test_censor_input_without_trigger_action (F031)
# ---------------------------------------------------------------------------


async def test_censor_input_without_trigger_action(heart, session):
    """Existing censors without trigger_action still work (backward compat)."""
    inp = CensorInput(
        trigger_pattern="never deploy on Friday",
        reason="Weekend risk",
    )
    detail = await heart.add_censor(inp, session=session)
    assert detail.trigger_action is None
    assert detail.action_instruction is None
    assert detail.unblock_pattern is None


# ---------------------------------------------------------------------------
# 19. test_censor_match_includes_action_fields (F031)
# ---------------------------------------------------------------------------


def test_censor_match_includes_action_fields():
    """CensorMatch carries trigger_action and action_instruction."""
    from uuid import uuid4

    from nous.heart.schemas import CensorMatch

    match = CensorMatch(
        id=uuid4(),
        trigger_pattern="test",
        action="warn",
        reason="test reason",
        domain=None,
        trigger_action={"tool": "recall", "args": {"query": "test"}},
        action_instruction="Check memory first.",
        unblock_pattern=r"admin@example\.com",
    )
    assert match.trigger_action["tool"] == "recall"
    assert match.action_instruction == "Check memory first."
    assert match.unblock_pattern == r"admin@example\.com"


# ---------------------------------------------------------------------------
# 20. test_allowed_tools_are_read_only (F031)
# ---------------------------------------------------------------------------


def test_allowed_tools_are_read_only():
    """Only read-only tools are in the allow list."""
    from nous.heart.censor_actions import ALLOWED_TOOLS

    assert "recall" in ALLOWED_TOOLS
    assert "recall_recent" in ALLOWED_TOOLS
    assert "search_facts" in ALLOWED_TOOLS
    assert "search_episodes" in ALLOWED_TOOLS
    assert "search_procedures" in ALLOWED_TOOLS
    assert "list_tasks" in ALLOWED_TOOLS
    # Write tools must NOT be in the list
    assert "learn_fact" not in ALLOWED_TOOLS
    assert "add_censor" not in ALLOWED_TOOLS
    assert "write_file" not in ALLOWED_TOOLS


# ---------------------------------------------------------------------------
# 21. test_execute_rejects_unknown_tool (F031)
# ---------------------------------------------------------------------------


async def test_execute_rejects_unknown_tool(heart, session):
    """Unknown tools are rejected, returning None."""
    from nous.heart.censor_actions import CensorActionExecutor

    executor = CensorActionExecutor(heart)
    result = await executor.execute(
        trigger_action={"tool": "write_file", "args": {"path": "/etc/passwd"}},
        session=session,
    )
    assert result is None


# ---------------------------------------------------------------------------
# 22. test_execute_rejects_malformed_action (F031)
# ---------------------------------------------------------------------------


async def test_execute_rejects_malformed_action(heart, session):
    """Malformed trigger_action (missing tool key) returns None."""
    from nous.heart.censor_actions import CensorActionExecutor

    executor = CensorActionExecutor(heart)
    result = await executor.execute(
        trigger_action={"args": {"query": "test"}},
        session=session,
    )
    assert result is None
