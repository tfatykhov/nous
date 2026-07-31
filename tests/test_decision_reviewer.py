"""Unit tests for 008.5 Decision Review Loop.

Tests cover Tasks 2-18: ORM model updates, schema extensions,
new Brain methods, config additions, signals, handler, and REST endpoints.
All tests use mocks (no real DB) to verify the public contract.

15 test classes, 34 tests total.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest

from nous.brain.schemas import (
    CalibrationReport,
    DecisionDetail,
    DecisionSummary,
    RecordInput,
    ReviewInput,
)
from nous.config import Settings
from nous.handlers.decision_reviewer import (
    CONFIDENCE_THRESHOLD,
    DecisionReviewer,
    ErrorSignal,
    FileExistsSignal,
    GitHubSignal,
    ReviewResult,
)
from nous.storage.models import Decision


# ---------------------------------------------------------------------------
# Task 2: ORM Model — Decision accepts session_id and reviewer
# ---------------------------------------------------------------------------


class TestDecisionModel:
    """Verify the Decision ORM model accepts the new columns."""

    def test_decision_accepts_session_id(self):
        """Decision constructor should accept session_id."""
        d = Decision(
            agent_id="test",
            description="test decision",
            confidence=0.8,
            category="architecture",
            stakes="medium",
            session_id="sess-123",
        )
        assert d.session_id == "sess-123"

    def test_decision_accepts_reviewer(self):
        """Decision constructor should accept reviewer."""
        d = Decision(
            agent_id="test",
            description="test decision",
            confidence=0.8,
            category="architecture",
            stakes="medium",
            reviewer="auto-reviewer",
        )
        assert d.reviewer == "auto-reviewer"


# ---------------------------------------------------------------------------
# Task 3: ReviewInput accepts reviewer
# ---------------------------------------------------------------------------


class TestBrainReviewExtension:
    """Verify ReviewInput schema accepts the reviewer field."""

    def test_review_input_accepts_reviewer(self):
        """ReviewInput should accept an optional reviewer field."""
        ri = ReviewInput(outcome="success", result="it worked", reviewer="auto")
        assert ri.reviewer == "auto"
        assert ri.outcome == "success"

    def test_review_input_reviewer_optional(self):
        """ReviewInput reviewer should default to None."""
        ri = ReviewInput(outcome="failure")
        assert ri.reviewer is None


# ---------------------------------------------------------------------------
# Task 3b: DecisionDetail includes reviewer
# ---------------------------------------------------------------------------


class TestDecisionDetailReviewer:
    """Verify DecisionDetail schema includes reviewer."""

    def test_decision_detail_includes_reviewer(self):
        """DecisionDetail should have a reviewer field."""
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        dd = DecisionDetail(
            id="00000000-0000-0000-0000-000000000001",
            agent_id="test",
            description="test",
            confidence=0.8,
            category="architecture",
            stakes="medium",
            outcome="success",
            reviewed_at=now,
            reviewer="auto-reviewer",
            created_at=now,
            updated_at=now,
        )
        assert dd.reviewer == "auto-reviewer"


# ---------------------------------------------------------------------------
# Task 7: RecordInput accepts session_id
# ---------------------------------------------------------------------------


class TestRecordSessionId:
    """Verify RecordInput schema accepts session_id."""

    def test_record_input_accepts_session_id(self):
        """RecordInput should accept an optional session_id field."""
        ri = RecordInput(
            description="Use Postgres",
            confidence=0.9,
            category="architecture",
            stakes="high",
            session_id="sess-abc",
        )
        assert ri.session_id == "sess-abc"

    def test_record_input_session_id_optional(self):
        """RecordInput session_id should default to None."""
        ri = RecordInput(
            description="Use Postgres",
            confidence=0.9,
            category="architecture",
            stakes="high",
        )
        assert ri.session_id is None


# ---------------------------------------------------------------------------
# Task 4 + 5: get_session_decisions / get_unreviewed exist on Brain
# ---------------------------------------------------------------------------


class TestBrainNewMethods:
    """Verify new methods exist on the Brain class."""

    def test_get_session_decisions_exists(self):
        """Brain should have a get_session_decisions method."""
        from nous.brain.brain import Brain

        assert hasattr(Brain, "get_session_decisions")
        assert callable(getattr(Brain, "get_session_decisions"))

    def test_get_unreviewed_exists(self):
        """Brain should have a get_unreviewed method."""
        from nous.brain.brain import Brain

        assert hasattr(Brain, "get_unreviewed")
        assert callable(getattr(Brain, "get_unreviewed"))


# ---------------------------------------------------------------------------
# Task 6: generate_calibration_snapshot exists on Brain
# ---------------------------------------------------------------------------


class TestCalibrationSnapshot:
    """Verify generate_calibration_snapshot method exists."""

    def test_generate_calibration_snapshot_exists(self):
        """Brain should have a generate_calibration_snapshot method."""
        from nous.brain.brain import Brain

        assert hasattr(Brain, "generate_calibration_snapshot")
        assert callable(getattr(Brain, "generate_calibration_snapshot"))


# ---------------------------------------------------------------------------
# Task 8: Config additions
# ---------------------------------------------------------------------------


class TestConfigAdditions:
    """Verify new config fields exist."""

    def test_decision_review_enabled_default(self):
        """Settings should have decision_review_enabled defaulting to True."""
        s = Settings(ANTHROPIC_API_KEY="test")
        assert s.decision_review_enabled is True

    def test_github_token_default(self):
        """Settings should have github_token defaulting to empty string."""
        s = Settings(ANTHROPIC_API_KEY="test")
        assert s.github_token == ""


# ===========================================================================
# Helpers for Tasks 9-15
# ===========================================================================

_NOW = datetime.now(timezone.utc)


def _make_decision(
    *,
    confidence: float = 0.8,
    confidence_raw: float | None = None,
    description: str = "Use Postgres for storage",
    outcome: str = "pending",
    reviewed_at=None,
    id: UUID | None = None,
) -> DecisionSummary:
    """Build a minimal DecisionSummary for signal tests."""
    return DecisionSummary(
        id=id or uuid4(),
        description=description,
        confidence=confidence,
        confidence_raw=confidence_raw,
        category="architecture",
        stakes="medium",
        outcome=outcome,
        reviewed_at=reviewed_at,
        created_at=_NOW,
    )


# ---------------------------------------------------------------------------
# Task 10: ErrorSignal
# ---------------------------------------------------------------------------


class TestErrorSignal:
    """Verify ErrorSignal auto-fails only genuinely low-confidence decisions."""

    @pytest.mark.asyncio
    async def test_low_confidence_returns_failure(self):
        """Decisions with confidence < 0.4 should be auto-failed."""
        signal = ErrorSignal()
        result = await signal.check(_make_decision(confidence=0.3))
        assert result is not None
        assert result.result == "failure"
        assert result.signal_type == "error"
        assert "0.30" in result.explanation

    @pytest.mark.asyncio
    async def test_error_keyword_in_description_does_not_fail(self):
        """Audit HD-4: a decision *about* a bug/error (keyword in the plan text)
        must NOT be auto-failed. The description is the intended action, not the
        outcome — the keyword branch was removed."""
        signal = ErrorSignal()
        result = await signal.check(
            _make_decision(confidence=0.7, description="Fix the login error in auth.py")
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_normal_decision_returns_none(self):
        """Clean decisions should return None."""
        signal = ErrorSignal()
        result = await signal.check(
            _make_decision(confidence=0.8, description="Use Postgres for storage")
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_f058_calibrated_dip_below_threshold_does_not_fail(self):
        """An author-stated 0.5 must NOT be auto-failed just because F058 scaled it.

        The measured prod case: recorded at raw 0.5, stored calibrated as
        0.38 (x0.7627), auto-failed. The 0.4 threshold is a claim about what
        the author asserted, so it must be compared against the raw value.
        """
        signal = ErrorSignal()
        result = await signal.check(_make_decision(confidence=0.38, confidence_raw=0.5))
        assert result is None

    @pytest.mark.asyncio
    async def test_genuinely_low_raw_confidence_still_fails(self):
        """A raw confidence under 0.4 is still auto-failed, and reports the raw value."""
        signal = ErrorSignal()
        result = await signal.check(_make_decision(confidence=0.229, confidence_raw=0.3))
        assert result is not None
        assert result.result == "failure"
        assert "0.30" in result.explanation

    @pytest.mark.asyncio
    async def test_legacy_row_without_raw_uses_calibrated(self):
        """Pre-F058 rows have confidence_raw IS NULL — behaviour is unchanged."""
        signal = ErrorSignal()
        failed = await signal.check(_make_decision(confidence=0.3, confidence_raw=None))
        assert failed is not None
        assert failed.result == "failure"
        assert "0.30" in failed.explanation
        assert await signal.check(_make_decision(confidence=0.8, confidence_raw=None)) is None


class TestConfidenceRawPropagation:
    """The raw confidence must survive the ORM -> DecisionSummary hop.

    ErrorSignal only ever sees a DecisionSummary, so if the summary drops
    confidence_raw the F058 fix above is silently inert in production.
    """

    def test_decision_to_summary_propagates_confidence_raw(self):
        """Brain._decision_to_summary feeds both reviewer paths."""
        from nous.brain.brain import Brain

        d = Decision(
            agent_id="test",
            description="Recorded with a humble 0.5",
            confidence=0.38,
            confidence_raw=0.5,
            category="architecture",
            stakes="medium",
        )
        d.id = uuid4()
        d.created_at = _NOW
        # _decision_to_summary uses no instance state — call it unbound so the
        # assertion needs no database.
        summary = Brain._decision_to_summary(None, d)
        assert summary.confidence_raw == 0.5
        assert summary.confidence == 0.38

    def test_summary_confidence_raw_defaults_to_none(self):
        """Legacy/partial constructions must not be forced to supply it."""
        assert _make_decision(confidence=0.8).confidence_raw is None


# ---------------------------------------------------------------------------
# Task 11: EpisodeSignal — REMOVED 2026-07-28
# ---------------------------------------------------------------------------
# TestEpisodeSignal was deleted with the class it covered. EpisodeSignal had
# been unwired since audit HD-4 (2026-06-09) for mapping the hardcoded-"success"
# episode outcome onto every decision in a session; these tests only ever
# asserted that broken mapping against mocks.


# ---------------------------------------------------------------------------
# Task 12: FileExistsSignal
# ---------------------------------------------------------------------------


class TestFileExistsSignal:
    """Verify FileExistsSignal checks file existence on disk."""

    @pytest.mark.asyncio
    async def test_existing_file_returns_success(self):
        """When a referenced file exists, should return success."""
        signal = FileExistsSignal()
        decision = _make_decision(
            description="Created docs/features/INDEX.md for tracking"
        )
        with patch("nous.handlers.decision_reviewer.Path") as mock_path:
            mock_path.return_value.exists.return_value = True
            # The signal uses Path(path_str).exists(), so we need
            # the class call to return our mock instance
            result = await signal.check(decision)

        assert result is not None
        assert result.result == "success"
        assert result.signal_type == "file_exists"

    @pytest.mark.asyncio
    async def test_missing_file_returns_none(self):
        """When referenced files don't exist, should return None."""
        signal = FileExistsSignal()
        decision = _make_decision(
            description="Created docs/features/INDEX.md for tracking"
        )
        with patch("nous.handlers.decision_reviewer.Path") as mock_path:
            mock_path.return_value.exists.return_value = False
            result = await signal.check(decision)

        assert result is None

    @pytest.mark.asyncio
    async def test_no_file_path_returns_none(self):
        """Decisions without file paths should return None."""
        signal = FileExistsSignal()
        decision = _make_decision(description="Use Postgres for storage")
        result = await signal.check(decision)
        assert result is None


# ---------------------------------------------------------------------------
# Task 13: GitHubSignal
# ---------------------------------------------------------------------------


class TestGitHubSignal:
    """Verify GitHubSignal checks PR status on GitHub."""

    @pytest.mark.asyncio
    async def test_merged_pr_returns_success(self):
        """Merged PR should return success."""
        http = AsyncMock()
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"state": "closed", "merged": True}
        http.get = AsyncMock(return_value=resp)

        signal = GitHubSignal(http, "ghp_test_token")
        decision = _make_decision(description="Implement storage layer PR #5")
        result = await signal.check(decision)

        assert result is not None
        assert result.result == "success"
        assert result.signal_type == "github"
        assert "#5" in result.explanation

    @pytest.mark.asyncio
    async def test_closed_unmerged_returns_failure(self):
        """Closed-without-merge PR should return failure."""
        http = AsyncMock()
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"state": "closed", "merged": False}
        http.get = AsyncMock(return_value=resp)

        signal = GitHubSignal(http, "ghp_test_token")
        decision = _make_decision(description="Implement storage layer PR #5")
        result = await signal.check(decision)

        assert result is not None
        assert result.result == "failure"
        assert result.signal_type == "github"

    @pytest.mark.asyncio
    async def test_open_pr_returns_none(self):
        """Open PR (not merged, not closed) should return None."""
        http = AsyncMock()
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"state": "open", "merged": False}
        http.get = AsyncMock(return_value=resp)

        signal = GitHubSignal(http, "ghp_test_token")
        decision = _make_decision(description="Implement storage layer PR #5")
        result = await signal.check(decision)

        assert result is None

    @pytest.mark.asyncio
    async def test_no_token_returns_none(self):
        """Empty token should skip GitHub check."""
        http = AsyncMock()
        signal = GitHubSignal(http, "")
        decision = _make_decision(description="Implement storage layer PR #5")
        result = await signal.check(decision)
        assert result is None


# ---------------------------------------------------------------------------
# Task 14: DecisionReviewer handler
# ---------------------------------------------------------------------------


class TestDecisionReviewer:
    """Verify DecisionReviewer handler wiring and behaviour."""

    def test_handler_registers_on_session_ended(self):
        """DecisionReviewer should register on 'session_ended'."""
        bus = MagicMock()
        bus.on = MagicMock()
        settings = MagicMock()
        settings.github_token = ""

        DecisionReviewer(brain=AsyncMock(), settings=settings, bus=bus)
        bus.on.assert_called_once()
        assert bus.on.call_args[0][0] == "session_ended"

    @pytest.mark.asyncio
    async def test_handler_reviews_session_decisions(self):
        """Handler should auto-review unreviewed low-confidence decisions."""
        bus = MagicMock()
        bus.on = MagicMock()
        brain = AsyncMock()
        settings = MagicMock()
        settings.github_token = ""

        # Low-confidence decision triggers ErrorSignal
        decision = _make_decision(confidence=0.3, reviewed_at=None)
        brain.get_session_decisions = AsyncMock(return_value=[decision])
        brain.get_unreviewed = AsyncMock(return_value=[])
        brain.review = AsyncMock()

        reviewer = DecisionReviewer(brain=brain, settings=settings, bus=bus)

        # Simulate session_ended event
        event = MagicMock()
        event.data = {"session_id": "sess-1"}
        event.session_id = "sess-1"
        await reviewer.handle(event)

        brain.review.assert_called()
        call_kwargs = brain.review.call_args
        assert call_kwargs[1]["reviewer"] == "auto" or call_kwargs.kwargs.get("reviewer") == "auto"

    @pytest.mark.asyncio
    async def test_handler_skips_already_reviewed(self):
        """Handler should skip decisions that already have reviewed_at set."""
        bus = MagicMock()
        bus.on = MagicMock()
        brain = AsyncMock()
        settings = MagicMock()
        settings.github_token = ""

        # Already-reviewed decision
        decision = _make_decision(confidence=0.3, reviewed_at=_NOW)
        brain.get_session_decisions = AsyncMock(return_value=[decision])
        brain.get_unreviewed = AsyncMock(return_value=[])
        brain.review = AsyncMock()

        reviewer = DecisionReviewer(brain=brain, settings=settings, bus=bus)

        event = MagicMock()
        event.data = {"session_id": "sess-1"}
        event.session_id = "sess-1"
        await reviewer.handle(event)

        brain.review.assert_not_called()

    @pytest.mark.asyncio
    async def test_sweep_reviews_old_unreviewed(self):
        """sweep() should review old unreviewed decisions and return results."""
        bus = MagicMock()
        bus.on = MagicMock()
        brain = AsyncMock()
        settings = MagicMock()
        settings.github_token = ""

        # Low-confidence unreviewed decision
        decision = _make_decision(confidence=0.2, reviewed_at=None)
        brain.get_unreviewed = AsyncMock(return_value=[decision])
        brain.review = AsyncMock()

        reviewer = DecisionReviewer(brain=brain, settings=settings, bus=bus)
        results = await reviewer.sweep()

        assert len(results) == 1
        assert results[0].result == "failure"
        brain.review.assert_called_once()


# ---------------------------------------------------------------------------
# Task 15: main.py wiring
# ---------------------------------------------------------------------------


class TestMainWiring:
    """Verify DecisionReviewer is importable and wirable from main."""

    def test_decision_reviewer_importable(self):
        """DecisionReviewer should be importable from handlers module."""
        from nous.handlers.decision_reviewer import DecisionReviewer

        assert DecisionReviewer is not None

    def test_brain_no_longer_has_get_episode_for_decision(self):
        """get_episode_for_decision was removed with EpisodeSignal (2026-07-28).

        Inverted rather than deleted: this pins the removal so the method
        cannot be reintroduced without a deliberate test change.
        """
        from nous.brain.brain import Brain

        assert not hasattr(Brain, "get_episode_for_decision")


# ---------------------------------------------------------------------------
# Task 16: POST /decisions/{id}/review endpoint
# ---------------------------------------------------------------------------


class TestReviewEndpoint:
    """REST endpoint tests for POST /decisions/{id}/review."""

    def test_review_input_validates_outcome(self):
        """ReviewInput rejects invalid outcomes."""
        from nous.brain.schemas import ReviewInput
        import pytest

        with pytest.raises(Exception):
            ReviewInput(outcome="invalid_value")

    def test_review_input_accepts_valid_outcomes(self):
        """ReviewInput accepts success, partial, failure."""
        from nous.brain.schemas import ReviewInput

        for outcome in ["success", "partial", "failure"]:
            ri = ReviewInput(outcome=outcome, reviewer="emerson")
            assert ri.outcome == outcome


# ---------------------------------------------------------------------------
# Task 17: GET /decisions/unreviewed endpoint
# ---------------------------------------------------------------------------


class TestUnreviewedEndpoint:
    """REST endpoint tests for GET /decisions/unreviewed."""

    @pytest.mark.asyncio
    async def test_get_unreviewed_mock(self):
        """get_unreviewed returns empty list when no decisions match."""
        brain = AsyncMock()
        brain.get_unreviewed = AsyncMock(return_value=[])
        result = await brain.get_unreviewed(max_age_days=14, stakes="high")
        assert result == []

    @pytest.mark.asyncio
    async def test_get_unreviewed_with_results(self):
        """get_unreviewed returns matching decisions."""
        brain = AsyncMock()
        decisions = [_make_decision(confidence=0.6), _make_decision(confidence=0.4)]
        brain.get_unreviewed = AsyncMock(return_value=decisions)
        result = await brain.get_unreviewed(max_age_days=30)
        assert len(result) == 2

    @pytest.mark.asyncio
    async def test_get_unreviewed_limit_slicing(self):
        """Limit parameter should slice the results."""
        brain = AsyncMock()
        decisions = [_make_decision() for _ in range(5)]
        brain.get_unreviewed = AsyncMock(return_value=decisions)
        result = await brain.get_unreviewed(max_age_days=30)
        # Simulating the endpoint's limit slicing
        limit = 3
        sliced = result[:limit]
        assert len(sliced) == 3


# ---------------------------------------------------------------------------
# Task 18: Route ordering verification
# ---------------------------------------------------------------------------


class TestRouteOrdering:
    """Verify that /decisions/unreviewed and /decisions/{id}/review
    are registered before /decisions/{id} in the routes list."""

    def test_unreviewed_before_parameterized(self):
        """The /decisions/unreviewed route must come before /decisions/{id}."""
        from starlette.routing import Route

        # Import and inspect the create_app function source
        import inspect
        from nous.api.rest import create_app

        source = inspect.getsource(create_app)
        unreviewed_pos = source.index("/decisions/unreviewed")
        review_pos = source.index('/decisions/{id}/review')
        parameterized_pos = source.index('Route("/decisions/{id}"', review_pos + 1)
        assert unreviewed_pos < parameterized_pos, (
            "/decisions/unreviewed must be before /decisions/{id}"
        )
        assert review_pos < parameterized_pos, (
            "/decisions/{id}/review must be before /decisions/{id}"
        )
