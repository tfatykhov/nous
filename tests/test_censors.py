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
    await session.execute(select(Censor).where(Censor.id == censor.id))
    from sqlalchemy import update

    await session.execute(update(Censor).where(Censor.id == censor.id).values(embedding=None))
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
    await session.execute(update(Censor).where(Censor.id == censor.id).values(embedding=None))
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

    await session.execute(update(Censor).where(Censor.id == censor.id).values(embedding=None))
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
    await session.execute(update(Censor).where(Censor.id.in_([bad_censor.id, good_censor.id])).values(embedding=None))
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

    await session.execute(update(Censor).where(Censor.id == censor.id).values(embedding=None))
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


# ---------------------------------------------------------------------------
# F031 Task 4: TurnContext schema tests
# ---------------------------------------------------------------------------


def test_turn_context_has_censor_injected_context():
    """TurnContext schema includes censor_injected_context field."""
    from nous.cognitive.schemas import FrameSelection, TurnContext

    ctx = TurnContext(
        system_prompt="test",
        frame=FrameSelection(
            frame_id="conversation",
            frame_name="Conversation",
            description="test",
            confidence=1.0,
            match_method="pattern",
        ),
        censor_injected_context={"censor-id-1": "[Censor recall: 3 results]..."},
    )
    assert ctx.censor_injected_context == {"censor-id-1": "[Censor recall: 3 results]..."}


def test_turn_context_censor_injected_context_default_empty():
    """censor_injected_context defaults to empty dict."""
    from nous.cognitive.schemas import FrameSelection, TurnContext

    ctx = TurnContext(
        system_prompt="test",
        frame=FrameSelection(
            frame_id="conversation",
            frame_name="Conversation",
            description="test",
            confidence=1.0,
            match_method="pattern",
        ),
    )
    assert ctx.censor_injected_context == {}


# ---------------------------------------------------------------------------
# F031 Task 5: Post-turn compliance check tests
# ---------------------------------------------------------------------------


def test_check_censor_compliance_used():
    """Compliance check detects when agent referenced injected context."""
    from nous.cognitive.layer import _check_censor_compliance

    injected = {"censor-1": "[Censor recall for 'citations': 2 results]\n  1. [fact] Source Alpha from research paper"}
    response = "Based on Source Alpha from the research paper, the data shows significant improvement."
    result = _check_censor_compliance(injected, response)
    assert result["censor-1"] is True


def test_check_censor_compliance_not_used():
    """Compliance check detects when agent did NOT reference injected context."""
    from nous.cognitive.layer import _check_censor_compliance

    injected = {"censor-1": "[Censor recall for 'citations': 2 results]\n  1. [fact] Source Alpha from research paper"}
    response = "I think the answer is 42."
    result = _check_censor_compliance(injected, response)
    assert result["censor-1"] is False


def test_check_censor_compliance_empty_injected():
    """Empty injected context returns empty results."""
    from nous.cognitive.layer import _check_censor_compliance

    result = _check_censor_compliance({}, "Some response")
    assert result == {}


# ---------------------------------------------------------------------------
# F031 Task 6: Integration tests
# ---------------------------------------------------------------------------


async def test_censor_action_end_to_end(heart, session):
    """Full flow: create censor with action -> check -> execute -> get results."""
    from nous.heart.schemas import FactInput

    await heart.learn(
        FactInput(content="Paris is the capital of France", category="geography", subject="France"),
        session=session,
    )

    inp = CensorInput(
        trigger_pattern="capital.*country|what.*capital",
        reason="Verify geographic claims",
        action="warn",
        trigger_action={"tool": "recall", "args": {"query": "capital country geography", "limit": 3}},
        action_instruction="Verify geographic claims against recalled facts.",
    )
    detail = await heart.add_censor(inp, session=session)
    assert detail.trigger_action is not None

    matches = await heart.check_censors("What is the capital of France?", session=session)
    assert len(matches) >= 1
    warn_match = [m for m in matches if m.action == "warn"]
    assert len(warn_match) >= 1
    assert warn_match[0].trigger_action is not None

    from nous.heart.censor_actions import CensorActionExecutor

    executor = CensorActionExecutor(heart)
    result = await executor.execute(warn_match[0].trigger_action, session=session)
    assert result is not None
    assert "capital" in result.lower() or "france" in result.lower() or "paris" in result.lower()


async def test_block_censor_with_action_enriches_reason(heart, session):
    """Block censor with trigger_action enriches the block reason with evidence."""
    from nous.heart.schemas import FactInput

    await heart.learn(
        FactInput(
            content="Production database was accidentally deleted on 2025-12-01",
            category="incident",
            subject="production",
        ),
        session=session,
    )

    inp = CensorInput(
        trigger_pattern="delete.*production|drop.*production",
        reason="Destructive production operations are prohibited",
        action="block",
        trigger_action={"tool": "recall", "args": {"query": "production deletion incident", "limit": 3}},
        action_instruction="Contact the infrastructure team for production changes.",
    )
    detail = await heart.add_censor(inp, session=session)
    assert detail.action == "block"
    assert detail.trigger_action is not None

    matches = await heart.check_censors("Let's delete the production database", session=session)
    block_matches = [m for m in matches if m.action == "block"]
    assert len(block_matches) >= 1
    assert block_matches[0].trigger_action is not None
    assert block_matches[0].action_instruction == "Contact the infrastructure team for production changes."

    from nous.heart.censor_actions import CensorActionExecutor

    executor = CensorActionExecutor(heart)
    result = await executor.execute(block_matches[0].trigger_action, session=session)
    assert result is not None


async def test_block_censor_conditional_unblock(heart, session):
    """Block censor with unblock_pattern downgrades to warn when pattern matches action results."""
    from nous.heart.schemas import FactInput

    await heart.learn(
        FactInput(content="Allowed admin: admin@company.com, ops@company.com", category="access", subject="admin-list"),
        session=session,
    )

    inp = CensorInput(
        trigger_pattern="delete.*production",
        reason="Production deletion requires admin access",
        action="block",
        trigger_action={"tool": "search_facts", "args": {"query": "allowed admin access", "limit": 5}},
        unblock_pattern=r"admin@company\.com",
        action_instruction="Contact infrastructure team if you need access.",
    )
    detail = await heart.add_censor(inp, session=session)
    assert detail.unblock_pattern is not None

    import re

    from nous.heart.censor_actions import CensorActionExecutor

    executor = CensorActionExecutor(heart)
    matches = await heart.check_censors("delete production database", session=session)
    block_match = [m for m in matches if m.trigger_pattern == "delete.*production"][0]

    result = await executor.execute(block_match.trigger_action, session=session)
    assert result is not None
    assert re.search(block_match.unblock_pattern, result, re.IGNORECASE)


async def test_block_censor_no_unblock_when_pattern_missing(heart, session):
    """Block censor without unblock_pattern always blocks (no downgrade)."""
    inp = CensorInput(
        trigger_pattern="drop.*table",
        reason="No dropping tables",
        action="block",
        trigger_action={"tool": "recall", "args": {"query": "table drops", "limit": 3}},
    )
    detail = await heart.add_censor(inp, session=session)
    assert detail.unblock_pattern is None

    matches = await heart.check_censors("drop table users", session=session)
    block_matches = [m for m in matches if m.action == "block"]
    assert len(block_matches) >= 1
    assert block_matches[0].unblock_pattern is None


async def test_multiple_censor_actions_all_injected(heart, session):
    """When multiple warn censors with trigger_action fire, all results are collected."""
    from nous.heart.schemas import FactInput

    await heart.learn(
        FactInput(content="Python is a programming language", category="tech", subject="Python"),
        session=session,
    )
    await heart.learn(
        FactInput(content="Security best practices include input validation", category="security", subject="security"),
        session=session,
    )

    inp1 = CensorInput(
        trigger_pattern="python.*code",
        reason="Check coding standards",
        action="warn",
        trigger_action={"tool": "recall", "args": {"query": "python programming", "limit": 3}},
    )
    inp2 = CensorInput(
        trigger_pattern="python.*code",
        reason="Check security",
        action="warn",
        trigger_action={"tool": "search_facts", "args": {"query": "security", "limit": 3}},
    )
    await heart.add_censor(inp1, session=session)
    await heart.add_censor(inp2, session=session)

    matches = await heart.check_censors("Write python code for login", session=session)
    warn_matches = [m for m in matches if m.action == "warn" and m.trigger_action]
    assert len(warn_matches) >= 2

    from nous.heart.censor_actions import CensorActionExecutor

    executor = CensorActionExecutor(heart)
    results = {}
    for m in warn_matches:
        result = await executor.execute(m.trigger_action, session=session)
        if result:
            results[str(m.id)] = result
    assert len(results) >= 2


async def test_backward_compat_censor_no_action(heart, session):
    """Censors without trigger_action work exactly as before."""
    inp = CensorInput(
        trigger_pattern="deploy.*friday",
        reason="No Friday deploys",
        action="warn",
    )
    detail = await heart.add_censor(inp, session=session)
    assert detail.trigger_action is None
    assert detail.action_instruction is None
    assert detail.unblock_pattern is None

    matches = await heart.check_censors("Let's deploy on Friday", session=session)
    assert len(matches) >= 1
    assert matches[0].trigger_action is None
    assert matches[0].action_instruction is None
    assert matches[0].unblock_pattern is None


# ---------------------------------------------------------------------------
# F031 Task 7: Censor Update API tests
# ---------------------------------------------------------------------------


async def test_update_censor_add_action_fields(heart, session):
    """Update an existing censor to add trigger_action and related fields."""
    inp = CensorInput(
        trigger_pattern="deploy.*friday",
        reason="No Friday deploys",
        action="warn",
    )
    detail = await heart.add_censor(inp, session=session)
    assert detail.trigger_action is None

    updated = await heart.update_censor(
        detail.id,
        trigger_action={"tool": "recall", "args": {"query": "deploy incidents", "limit": 3}},
        action_instruction="Check past deploy incidents before proceeding.",
        session=session,
    )
    assert updated.trigger_action == {"tool": "recall", "args": {"query": "deploy incidents", "limit": 3}}
    assert updated.action_instruction == "Check past deploy incidents before proceeding."
    assert updated.trigger_pattern == "deploy.*friday"
    assert updated.reason == "No Friday deploys"
    assert updated.action == "warn"


async def test_update_censor_add_unblock_pattern(heart, session):
    """Upgrade a block censor with unblock_pattern."""
    inp = CensorInput(
        trigger_pattern="delete.*production",
        reason="No production deletes",
        action="block",
    )
    detail = await heart.add_censor(inp, session=session)

    updated = await heart.update_censor(
        detail.id,
        trigger_action={"tool": "search_facts", "args": {"query": "allowed admins"}},
        unblock_pattern=r"admin@company\.com",
        action_instruction="Contact infra team.",
        session=session,
    )
    assert updated.unblock_pattern == r"admin@company\.com"
    assert updated.trigger_action is not None
    assert updated.action == "block"


# ---------------------------------------------------------------------------
# F031 Subtask censor handling
# ---------------------------------------------------------------------------


def test_pre_turn_accepts_is_subtask_param():
    """pre_turn signature includes is_subtask parameter."""
    import inspect

    from nous.cognitive.layer import CognitiveLayer

    sig = inspect.signature(CognitiveLayer.pre_turn)
    assert "is_subtask" in sig.parameters
    param = sig.parameters["is_subtask"]
    assert param.default is False


async def test_spawn_task_rejects_blocked_subtask(heart, session):
    """spawn_task censor check rejects subtasks that match a block censor."""
    # Create a block censor
    inp = CensorInput(
        trigger_pattern="delete.*production",
        reason="No production deletes from subtasks",
        action="block",
    )
    await heart.add_censor(inp, session=session)

    # Simulate what spawn_task does: check censors on task text
    matches = await heart.check_censors("delete production logs", session=session)
    block_matches = [m for m in matches if m.action == "block"]
    assert len(block_matches) >= 1, "Block censor should fire on subtask text"


async def test_spawn_task_allows_unblocked_subtask(heart, session):
    """spawn_task censor check allows subtask when unblock_pattern matches."""
    from nous.heart.schemas import FactInput

    await heart.learn(
        FactInput(
            content="Subtask-authorized operations: log-analysis, report-generation",
            category="access",
            subject="subtask-perms",
        ),
        session=session,
    )

    inp = CensorInput(
        trigger_pattern="production.*logs",
        reason="Production access restricted",
        action="block",
        trigger_action={"tool": "search_facts", "args": {"query": "subtask authorized operations"}},
        unblock_pattern=r"log-analysis",
    )
    await heart.add_censor(inp, session=session)

    matches = await heart.check_censors("analyze production logs", session=session)
    block_matches = [m for m in matches if m.action == "block"]
    assert len(block_matches) >= 1

    # Verify unblock would work
    import re

    from nous.heart.censor_actions import CensorActionExecutor

    executor = CensorActionExecutor(heart)
    for match in block_matches:
        if match.trigger_action and match.unblock_pattern:
            result = await executor.execute(match.trigger_action, session=session)
            if result and re.search(match.unblock_pattern, result, re.IGNORECASE):
                # Would be unblocked — subtask should proceed
                assert True
                return
    # If we get here, unblock didn't work
    assert False, "Unblock pattern should have matched"


async def test_warn_censor_does_not_block_subtask(heart, session):
    """Warn censors on subtask creation should not block the task."""
    inp = CensorInput(
        trigger_pattern="sensitive.*data",
        reason="Handle with care",
        action="warn",
    )
    await heart.add_censor(inp, session=session)

    matches = await heart.check_censors("process sensitive data export", session=session)
    block_matches = [m for m in matches if m.action == "block"]
    warn_matches = [m for m in matches if m.action == "warn"]
    assert len(block_matches) == 0, "Warn censors should not block"
    assert len(warn_matches) >= 1, "Warn censor should fire but not block"
