"""Tests for ProcedureManager — procedural memory (how to do things).

All tests use real Postgres via the SAVEPOINT fixture from conftest.py.
Heart methods receive the test session via the session parameter (P1-1).
"""

import pytest

from nous.heart import (


    ProcedureDetail,
    ProcedureInput,
    ProcedureSummary,
)

pytestmark = pytest.mark.integration

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _procedure_input(**overrides) -> ProcedureInput:
    """Build a ProcedureInput with sensible defaults."""
    defaults = dict(
        name="Git commit workflow",
        domain="development",
        description="Standard git commit workflow with conventional commits",
        goals=["Maintain clean git history"],
        core_patterns=["conventional commits", "atomic changes"],
        core_tools=["git"],
        core_concepts=["version control"],
        implementation_notes=["Always run tests before committing"],
        tags=["git", "workflow"],
    )
    defaults.update(overrides)
    return ProcedureInput(**defaults)


# ---------------------------------------------------------------------------
# 1. test_store_procedure
# ---------------------------------------------------------------------------


async def test_store_procedure(heart, session):
    """Basic creation with all level-band fields."""
    inp = _procedure_input()
    detail = await heart.store_procedure(inp, session=session)

    assert isinstance(detail, ProcedureDetail)
    assert detail.name == "Git commit workflow"
    assert detail.domain == "development"
    assert detail.description is not None
    assert detail.goals == ["Maintain clean git history"]
    assert detail.core_patterns == ["conventional commits", "atomic changes"]
    assert detail.core_tools == ["git"]
    assert detail.core_concepts == ["version control"]
    assert detail.implementation_notes == ["Always run tests before committing"]
    assert detail.activation_count == 0
    assert detail.success_count == 0
    assert detail.failure_count == 0
    assert detail.active is True


# ---------------------------------------------------------------------------
# 2. test_search_procedures
# ---------------------------------------------------------------------------


async def test_search_procedures(heart, session):
    """Search by name/description."""
    await heart.store_procedure(
        _procedure_input(
            name="Database migration procedure",
            description="How to run database migrations safely",
            core_patterns=["database migration"],
        ),
        session=session,
    )
    await heart.store_procedure(
        _procedure_input(
            name="API testing procedure",
            description="How to test REST APIs",
            core_patterns=["api testing"],
        ),
        session=session,
    )

    # Search using identical text to procedure name+desc for mock embeddings match
    results = await heart.search_procedures(
        "Database migration procedure How to run database migrations safely database migration",
        session=session,
    )
    assert isinstance(results, list)
    if results:
        assert isinstance(results[0], ProcedureSummary)


# ---------------------------------------------------------------------------
# 3. test_activate_procedure
# ---------------------------------------------------------------------------


async def test_activate_procedure(heart, session):
    """activation_count increments."""
    detail = await heart.store_procedure(
        _procedure_input(name="Activate test procedure"),
        session=session,
    )
    assert detail.activation_count == 0

    activated = await heart.activate_procedure(detail.id, session=session)
    assert activated.activation_count == 1

    activated2 = await heart.activate_procedure(detail.id, session=session)
    assert activated2.activation_count == 2


# ---------------------------------------------------------------------------
# 4. test_record_outcome_effectiveness
# ---------------------------------------------------------------------------


async def test_record_outcome_effectiveness(heart, session):
    """Laplace smoothing: (success+1)/(success+failure+2)."""
    detail = await heart.store_procedure(
        _procedure_input(name="Effectiveness test procedure"),
        session=session,
    )

    # Record 2 successes and 1 failure
    await heart.record_procedure_outcome(detail.id, "success", session=session)
    await heart.record_procedure_outcome(detail.id, "success", session=session)
    result = await heart.record_procedure_outcome(detail.id, "failure", session=session)

    assert result.success_count == 2
    assert result.failure_count == 1
    # Laplace: (2+1)/(2+1+2) = 3/5 = 0.6
    assert result.effectiveness == pytest.approx(0.6, abs=0.01)


# ---------------------------------------------------------------------------
# 5. test_retire_procedure
# ---------------------------------------------------------------------------


async def test_retire_procedure(heart, session):
    """Set active=false."""
    detail = await heart.store_procedure(
        _procedure_input(name="Retire test procedure"),
        session=session,
    )
    assert detail.active is True

    await heart.retire_procedure(detail.id, session=session)

    # Re-read to check
    fetched = await heart.get_procedure(detail.id, session=session)
    assert fetched.active is False


# ---------------------------------------------------------------------------
# 6. test_search_domain_filter
# ---------------------------------------------------------------------------


async def test_search_domain_filter(heart, session):
    """Only matching domain returned."""
    await heart.store_procedure(
        _procedure_input(
            name="Dev domain procedure",
            domain="development",
            core_patterns=["domain filter test"],
        ),
        session=session,
    )
    await heart.store_procedure(
        _procedure_input(
            name="Trading domain procedure",
            domain="trading",
            core_patterns=["domain filter test"],
        ),
        session=session,
    )

    results = await heart.search_procedures(
        "domain filter test",
        domain="development",
        session=session,
    )
    for r in results:
        assert r.domain == "development"


# ---------------------------------------------------------------------------
# 7. test_search_by_implementation_notes (issue #197)
# ---------------------------------------------------------------------------


async def test_search_by_implementation_notes(heart, session):
    """Issue #197: search should match on implementation_notes body text."""
    await heart.store_procedure(
        _procedure_input(
            name="Deploy checklist",
            description="Production deployment steps",
            core_patterns=["deployment"],
            implementation_notes=[
                "Run database migrations before deploying new code",
                "Verify health checks pass after rollout",
                "Monitor error rates for 15 minutes post-deploy",
            ],
        ),
        session=session,
    )

    # Search for content that only exists in implementation_notes
    results = await heart.search_procedures("health checks rollout", session=session)
    assert len(results) >= 1
    assert results[0].name == "Deploy checklist"


# ---------------------------------------------------------------------------
# 8. test_embed_text_includes_body_fields (issue #197)
# ---------------------------------------------------------------------------


async def test_embed_text_includes_body_fields(heart, session):
    """Issue #197: embedding should be generated from all fields, not just name+desc+patterns."""
    await heart.store_procedure(
        _procedure_input(
            name="Generic procedure",
            description="A generic procedure",
            core_patterns=["generic"],
            implementation_notes=["kubernetes pod autoscaling configuration"],
            goals=["scale infrastructure"],
            core_tools=["kubectl", "helm"],
            core_concepts=["container orchestration"],
        ),
        session=session,
    )
    await heart.store_procedure(
        _procedure_input(
            name="Another procedure",
            description="Another generic procedure",
            core_patterns=["generic"],
            implementation_notes=["watercolor painting techniques for landscapes"],
            goals=["create art"],
            core_tools=["brushes", "palette"],
            core_concepts=["color theory"],
        ),
        session=session,
    )

    # Search for kubernetes — should rank first procedure higher due to body content
    results = await heart.search_procedures("kubernetes autoscaling", session=session)
    assert len(results) >= 1
    assert results[0].name == "Generic procedure"


# ---------------------------------------------------------------------------
# 9. test_reembed_all (issue #197)
# ---------------------------------------------------------------------------


async def test_reembed_all(heart, session):
    """reembed_all() recomputes embeddings for all active procedures."""
    await heart.store_procedure(
        _procedure_input(
            name="Reembed test",
            description="Test procedure",
            implementation_notes=["specialized quantum computing algorithms"],
        ),
        session=session,
    )

    count = await heart.reembed_procedures(session=session)
    assert count >= 1


# ---------------------------------------------------------------------------
# 10. test_build_embed_text_helper (issue #197)
# ---------------------------------------------------------------------------


def test_build_embed_text_helper():
    """_build_embed_text includes all fields and handles None gracefully."""
    from nous.heart.procedures import _build_embed_text

    result = _build_embed_text(
        name="Test",
        description="A test",
        core_patterns=["pattern1"],
        goals=["goal1"],
        core_tools=["tool1"],
        core_concepts=["concept1"],
        implementation_notes=["note1", "note2"],
    )
    assert "Test" in result
    assert "A test" in result
    assert "pattern1" in result
    assert "goal1" in result
    assert "tool1" in result
    assert "concept1" in result
    assert "note1" in result
    assert "note2" in result


def test_build_embed_text_none_fields():
    """_build_embed_text handles None fields without error."""
    from nous.heart.procedures import _build_embed_text


    result = _build_embed_text(
        name="Minimal",
        description=None,
        core_patterns=None,
        goals=None,
        core_tools=None,
        core_concepts=None,
        implementation_notes=None,
    )
    assert result == "Minimal"
