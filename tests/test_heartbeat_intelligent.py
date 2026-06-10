"""Tests for F034.2 Intelligent Checks — embedding search, LLM email, drive context.

20 test cases across 6 test classes:
- TestHealthCheckTunableParams (3): params initialized, get_param returns TunableParam, set_param respects bounds
- TestSelfInitiatedEmbedding (4): with embeddings uses cosine, without embeddings falls back, prototype caching, max_pending_items limit
- TestSelfInitiatedPromiseTracking (2): finds ongoing episodes, skips completed
- TestEmailLLMClassification (4): LLM available classifies correctly, LLM unavailable falls back, sender reputation bypasses LLM, budget check gates LLM
- TestEmailSenderReputation (3): builds reputation over time, reputation decays after 30 days, unknown senders get full classification
- TestDriveSignificance (3): new file=high, own edit=normal, folder mapping enriches summary
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nous.heartbeat.checks import DriveCheck, EmailCheck, HealthCheck, SelfInitiatedCheck
from nous.heartbeat.registry import BaseCheck
from nous.heartbeat.schemas import CheckResult, Finding, TunableParam


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_settings(**overrides) -> MagicMock:
    """MagicMock Settings to avoid pydantic validation."""
    s = MagicMock()
    s.agent_id = "test-agent"
    s.heartbeat_enabled = True
    s.heartbeat_tick_interval = 30
    s.heartbeat_quiet_start = 23
    s.heartbeat_quiet_end = 8
    s.heartbeat_daily_token_budget = 50_000
    s.heartbeat_email_enabled = False
    s.heartbeat_email_interval = 180
    s.heartbeat_email_imap_host = "imap.gmail.com"
    s.email_user = ""
    s.email_password = ""
    s.heartbeat_health_interval = 3600
    s.heartbeat_self_initiated_interval = 1800
    s.heartbeat_drive_enabled = True
    s.heartbeat_drive_interval = 600
    s.telegram_bot_token = "test-token"
    s.telegram_chat_id = "12345"
    # F034.1 fields
    s.heartbeat_escalation_low_to_normal_hours = 72
    s.heartbeat_escalation_normal_to_high_hours = 24
    s.heartbeat_escalation_high_realert_hours = 12
    s.heartbeat_escalation_accumulation_threshold = 5
    s.heartbeat_digest_hour_utc = 9
    s.heartbeat_suppression_ttl_hours = 24
    # F034.3 fields
    s.heartbeat_tuning_enabled = False
    s.heartbeat_tuning_interval_hours = 168
    s.heartbeat_tuning_min_samples = 10
    s.heartbeat_tuning_learning_rate = 0.1
    s.heartbeat_tuning_rollback_threshold = 0.2
    for k, v in overrides.items():
        setattr(s, k, v)
    return s


# ===========================================================================
# TestHealthCheckTunableParams — 3 tests
# ===========================================================================


class TestHealthCheckTunableParams:
    """Tests for HealthCheck tunable parameter system."""

    def test_params_initialized(self):
        """HealthCheck initializes with 4 tunable params."""
        heart = MagicMock()
        brain = MagicMock()
        settings = _mock_settings()
        check = HealthCheck(heart, brain, settings)
        params = check.tunable_params()
        assert "stale_decision_days" in params
        assert "stale_fact_days" in params
        assert "low_effectiveness_threshold" in params
        assert "max_findings_per_run" in params

    def test_get_param_returns_tunable_param(self):
        """get_param returns a TunableParam with correct bounds."""
        heart = MagicMock()
        brain = MagicMock()
        settings = _mock_settings()
        check = HealthCheck(heart, brain, settings)
        p = check.get_param("stale_decision_days")
        assert p is not None
        assert isinstance(p, TunableParam)
        assert p.value == 7
        assert p.min_val == 3
        assert p.max_val == 30

    def test_set_param_respects_bounds(self):
        """set_param clamps value within min/max bounds."""
        heart = MagicMock()
        brain = MagicMock()
        settings = _mock_settings()
        check = HealthCheck(heart, brain, settings)

        # Set above max
        check.set_param("stale_decision_days", 100)
        assert check.get_param_value("stale_decision_days") == 30  # clamped to max

        # Set below min
        check.set_param("stale_decision_days", 1)
        assert check.get_param_value("stale_decision_days") == 3  # clamped to min


# ===========================================================================
# TestSelfInitiatedEmbedding — 4 tests
# ===========================================================================


class TestSelfInitiatedEmbedding:
    """Tests for SelfInitiatedCheck embedding-based search."""

    @pytest.mark.asyncio
    async def test_with_embeddings_uses_embedding_search(self):
        """When embeddings are provided, embedding_search is called."""
        heart = MagicMock()
        brain = MagicMock()
        settings = _mock_settings()

        embeddings = MagicMock()
        embeddings.embed_batch = AsyncMock(return_value=[[0.1] * 10] * 5)

        # Mock heart.facts.search to return a mock fact
        mock_fact = MagicMock()
        mock_fact.id = "fact-1"
        mock_fact.content = "TODO: follow-up on this pending action"
        mock_fact.score = 0.8
        heart.facts.search = AsyncMock(return_value=[mock_fact])
        heart.search_episodes = AsyncMock(return_value=[])
        heart.schedules.get_due = AsyncMock(return_value=[])

        check = SelfInitiatedCheck(heart, brain, settings, embeddings=embeddings)
        result = await check.run()

        # Should have called embed_batch for prototypes
        embeddings.embed_batch.assert_called_once()
        assert result.has_updates is True

    @pytest.mark.asyncio
    async def test_without_embeddings_falls_back_to_keywords(self):
        """When embeddings=None, falls back to keyword search only."""
        heart = MagicMock()
        brain = MagicMock()
        settings = _mock_settings()

        mock_fact = MagicMock()
        mock_fact.id = "fact-1"
        mock_fact.content = "TODO: need to review the deployment issue"
        mock_fact.score = 0.6
        heart.facts.search = AsyncMock(return_value=[mock_fact])
        heart.search_episodes = AsyncMock(return_value=[])
        heart.schedules.get_due = AsyncMock(return_value=[])

        check = SelfInitiatedCheck(heart, brain, settings, embeddings=None)
        result = await check.run()

        # Should still find via keyword fallback
        assert result.has_updates is True
        keyword_findings = [f for f in result.findings if f.raw_data.get("detection") == "keyword"]
        assert len(keyword_findings) >= 1

    @pytest.mark.asyncio
    async def test_prototype_caching(self):
        """Prototype embeddings are cached after first call."""
        heart = MagicMock()
        brain = MagicMock()
        settings = _mock_settings()

        embeddings = MagicMock()
        embeddings.embed_batch = AsyncMock(return_value=[[0.1] * 10] * 5)

        heart.facts.search = AsyncMock(return_value=[])
        heart.search_episodes = AsyncMock(return_value=[])
        heart.schedules.get_due = AsyncMock(return_value=[])

        check = SelfInitiatedCheck(heart, brain, settings, embeddings=embeddings)

        # First call should embed prototypes
        await check.run()
        assert embeddings.embed_batch.call_count == 1

        # Second call should use cached prototypes
        await check.run()
        assert embeddings.embed_batch.call_count == 1  # still 1

    @pytest.mark.asyncio
    async def test_max_pending_items_limit(self):
        """Findings are capped at max_pending_items."""
        heart = MagicMock()
        brain = MagicMock()
        settings = _mock_settings()

        # Return many facts
        facts = []
        for i in range(20):
            f = MagicMock()
            f.id = f"fact-{i}"
            f.content = f"TODO: pending action number {i}"
            f.score = 0.9
            facts.append(f)

        heart.facts.search = AsyncMock(return_value=facts)
        heart.search_episodes = AsyncMock(return_value=[])
        heart.schedules.get_due = AsyncMock(return_value=[])

        check = SelfInitiatedCheck(heart, brain, settings, embeddings=None)
        result = await check.run()

        max_items = int(check.get_param_value("max_pending_items"))
        assert len(result.findings) <= max_items


# ===========================================================================
# TestSelfInitiatedPromiseTracking — 2 tests
# ===========================================================================


class TestSelfInitiatedPromiseTracking:
    """Tests for SelfInitiatedCheck promise tracking via episodes."""

    @pytest.mark.asyncio
    async def test_finds_ongoing_episodes(self):
        """Promise scan finds ongoing episodes."""
        heart = MagicMock()
        brain = MagicMock()
        settings = _mock_settings()

        mock_episode = MagicMock()
        mock_episode.id = "ep-1"
        mock_episode.outcome = "ongoing"
        mock_episode.started_at = datetime.now(UTC) - timedelta(hours=72)
        mock_episode.summary = "Looking into the deployment issue"

        heart.facts.search = AsyncMock(return_value=[])
        heart.search_episodes = AsyncMock(return_value=[mock_episode])
        heart.schedules.get_due = AsyncMock(return_value=[])

        check = SelfInitiatedCheck(heart, brain, settings, embeddings=None)
        result = await check.run()

        promise_findings = [
            f for f in result.findings if f.raw_data.get("detection") == "promise_scan"
        ]
        assert len(promise_findings) >= 1
        assert "commitment" in promise_findings[0].summary.lower()

    @pytest.mark.asyncio
    async def test_skips_completed_episodes(self):
        """Promise scan skips completed episodes."""
        heart = MagicMock()
        brain = MagicMock()
        settings = _mock_settings()

        mock_episode = MagicMock()
        mock_episode.id = "ep-1"
        mock_episode.outcome = "completed"
        mock_episode.started_at = datetime.now(UTC) - timedelta(hours=1)  # recent
        mock_episode.summary = "All done"

        heart.facts.search = AsyncMock(return_value=[])
        heart.search_episodes = AsyncMock(return_value=[mock_episode])
        heart.schedules.get_due = AsyncMock(return_value=[])

        check = SelfInitiatedCheck(heart, brain, settings, embeddings=None)
        result = await check.run()

        promise_findings = [
            f for f in result.findings if f.raw_data.get("detection") == "promise_scan"
        ]
        assert len(promise_findings) == 0

    @pytest.mark.asyncio
    async def test_question_form_summary_not_flagged(self):
        """#369: a question/conversational opener is not a commitment, even on
        an otherwise-flaggable (ongoing) episode."""
        heart = MagicMock()
        brain = MagicMock()
        settings = _mock_settings()

        mock_episode = MagicMock()
        mock_episode.id = "ep-q"
        mock_episode.outcome = "ongoing"
        mock_episode.started_at = datetime.now(UTC) - timedelta(hours=72)
        mock_episode.summary = "ok what outstanding items we do have?"

        heart.facts.search = AsyncMock(return_value=[])
        heart.search_episodes = AsyncMock(return_value=[mock_episode])
        heart.schedules.get_due = AsyncMock(return_value=[])

        check = SelfInitiatedCheck(heart, brain, settings, embeddings=None)
        result = await check.run()

        promise_findings = [
            f for f in result.findings if f.raw_data.get("detection") == "promise_scan"
        ]
        assert len(promise_findings) == 0

    @pytest.mark.asyncio
    async def test_commitment_with_filler_or_modal_start_still_flagged(self):
        """#369 codex P2 regression: filler/modal openers on COMMITMENTS must
        not be suppressed — only real questions are."""
        heart = MagicMock()
        brain = MagicMock()
        settings = _mock_settings()

        episodes = []
        for i, summary in enumerate([
            "Okay, I'll follow up with Tim",
            "Should update the deployment docs",
            "Can finish the migration tomorrow",
        ]):
            ep = MagicMock()
            ep.id = f"ep-c{i}"
            ep.outcome = "ongoing"
            ep.started_at = datetime.now(UTC) - timedelta(hours=72)
            ep.summary = summary
            episodes.append(ep)

        heart.facts.search = AsyncMock(return_value=[])
        heart.search_episodes = AsyncMock(return_value=episodes)
        heart.schedules.get_due = AsyncMock(return_value=[])

        check = SelfInitiatedCheck(heart, brain, settings, embeddings=None)
        result = await check.run()

        promise_findings = [
            f for f in result.findings if f.raw_data.get("detection") == "promise_scan"
        ]
        assert len(promise_findings) == 3

    @pytest.mark.asyncio
    async def test_stale_episode_beyond_age_cap_not_flagged(self):
        """#369: the age-based heuristic has an upper bound — an episode older
        than max_stale_age_hours (default 14 days) is too old to be actionable."""
        heart = MagicMock()
        brain = MagicMock()
        settings = _mock_settings()

        mock_episode = MagicMock()
        mock_episode.id = "ep-old"
        mock_episode.outcome = "partial"  # not ongoing — age path only
        mock_episode.started_at = datetime.now(UTC) - timedelta(days=20)
        mock_episode.summary = "I'll draft the migration plan"

        heart.facts.search = AsyncMock(return_value=[])
        heart.search_episodes = AsyncMock(return_value=[mock_episode])
        heart.schedules.get_due = AsyncMock(return_value=[])

        check = SelfInitiatedCheck(heart, brain, settings, embeddings=None)
        result = await check.run()

        promise_findings = [
            f for f in result.findings if f.raw_data.get("detection") == "promise_scan"
        ]
        assert len(promise_findings) == 0

    @pytest.mark.asyncio
    async def test_stale_episode_within_age_cap_flagged(self):
        """#369 regression guard: a statement-form episode in the 48h..14d
        window is still flagged by the age-based path."""
        heart = MagicMock()
        brain = MagicMock()
        settings = _mock_settings()

        mock_episode = MagicMock()
        mock_episode.id = "ep-mid"
        mock_episode.outcome = "partial"  # not ongoing — age path only
        mock_episode.started_at = datetime.now(UTC) - timedelta(hours=72)
        mock_episode.summary = "I'll draft the migration plan"

        heart.facts.search = AsyncMock(return_value=[])
        heart.search_episodes = AsyncMock(return_value=[mock_episode])
        heart.schedules.get_due = AsyncMock(return_value=[])

        check = SelfInitiatedCheck(heart, brain, settings, embeddings=None)
        result = await check.run()

        promise_findings = [
            f for f in result.findings if f.raw_data.get("detection") == "promise_scan"
        ]
        assert len(promise_findings) >= 1

    @pytest.mark.asyncio
    async def test_old_ongoing_episode_still_flagged(self):
        """#369: outcome=ongoing is an explicit state, not a heuristic — the
        age cap does NOT suppress it."""
        heart = MagicMock()
        brain = MagicMock()
        settings = _mock_settings()

        mock_episode = MagicMock()
        mock_episode.id = "ep-old-ongoing"
        mock_episode.outcome = "ongoing"
        mock_episode.started_at = datetime.now(UTC) - timedelta(days=30)
        mock_episode.summary = "Researching the embedding migration"

        heart.facts.search = AsyncMock(return_value=[])
        heart.search_episodes = AsyncMock(return_value=[mock_episode])
        heart.schedules.get_due = AsyncMock(return_value=[])

        check = SelfInitiatedCheck(heart, brain, settings, embeddings=None)
        result = await check.run()

        promise_findings = [
            f for f in result.findings if f.raw_data.get("detection") == "promise_scan"
        ]
        assert len(promise_findings) >= 1


# ===========================================================================
# TestObservationPatternSuppression — coverage for the 10 patterns added in
# PR #323 (identity/contact facts, resolved/encoded notes, false-positive
# meta-docs). Guards against accidental pattern removal.
# ===========================================================================


class TestObservationPatternSuppression:
    """Regression guard for PR #323 identity/resolved _OBSERVATION_PATTERNS."""

    @pytest.mark.parametrize("content", [
        # Contact info / identity facts
        "Tim's email address is tim@example.com",
        "Profile: linkedin.com/in/tfatykhov",
        "He has two email addresses for different accounts",
        "His profile url is example.com/tim",
        # Resolved / encoded patterns
        "Resolved — admission guardrails now block stale facts",
        "Task completion signals encoded as censors",
        "Long-running failure modes encoded in the heart module",
        "These facts are stale and should no longer surface",
        "That flag is a false positive from last week",
        "Recurring false alarm from Tuesday's heartbeat run",
    ])
    def test_pattern_triggers_is_observation(self, content: str):
        """Each PR #323 pattern makes _is_observation return True."""
        assert SelfInitiatedCheck._is_observation(content)


# ===========================================================================
# TestTagCasingSkip — tag membership must be case-insensitive.
# Fact schema does not enforce tag casing; comparison must lowercase.
# ===========================================================================


class TestTagCasingSkip:
    """Embedding-search tag skip ignores case for 'resolved' and 'identity'."""

    @staticmethod
    def _make_fact(tags: list[str], category: str | None = None) -> MagicMock:
        f = MagicMock()
        f.id = "fact-tag"
        # Actionable-looking content so only the tag skip can suppress it.
        f.content = "TODO: need to follow up on this pending action"
        f.score = 0.9
        f.category = category
        f.tags = tags
        return f

    @staticmethod
    def _wire_heart(heart: MagicMock, fact: MagicMock) -> None:
        heart.facts.search = AsyncMock(return_value=[fact])
        heart.search_episodes = AsyncMock(return_value=[])
        heart.schedules.get_due = AsyncMock(return_value=[])

    @pytest.mark.asyncio
    @pytest.mark.parametrize("tag_variant", ["resolved", "Resolved", "RESOLVED"])
    async def test_resolved_tag_skipped_case_insensitively(self, tag_variant: str):
        heart, brain = MagicMock(), MagicMock()
        self._wire_heart(heart, self._make_fact(tags=[tag_variant]))
        embeddings = MagicMock()
        embeddings.embed_batch = AsyncMock(return_value=[[0.1] * 10] * 5)

        check = SelfInitiatedCheck(heart, brain, _mock_settings(), embeddings=embeddings)
        result = await check.run()

        assert [f for f in result.findings if f.raw_data.get("detection") == "embedding"] == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("tag_variant", ["identity", "Identity", "IDENTITY"])
    async def test_identity_tag_skipped_case_insensitively(self, tag_variant: str):
        heart, brain = MagicMock(), MagicMock()
        self._wire_heart(heart, self._make_fact(tags=[tag_variant]))
        embeddings = MagicMock()
        embeddings.embed_batch = AsyncMock(return_value=[[0.1] * 10] * 5)

        check = SelfInitiatedCheck(heart, brain, _mock_settings(), embeddings=embeddings)
        result = await check.run()

        assert [f for f in result.findings if f.raw_data.get("detection") == "embedding"] == []

    @pytest.mark.asyncio
    async def test_person_category_still_skipped(self):
        """Regression guard: category='person' skip survives the tag-casing refactor."""
        heart, brain = MagicMock(), MagicMock()
        self._wire_heart(heart, self._make_fact(tags=[], category="person"))
        embeddings = MagicMock()
        embeddings.embed_batch = AsyncMock(return_value=[[0.1] * 10] * 5)

        check = SelfInitiatedCheck(heart, brain, _mock_settings(), embeddings=embeddings)
        result = await check.run()

        assert [f for f in result.findings if f.raw_data.get("detection") == "embedding"] == []


# ===========================================================================
# TestEmailLLMClassification — 4 tests
# ===========================================================================


class TestEmailLLMClassification:
    """Tests for EmailCheck LLM-based email classification."""

    @pytest.mark.asyncio
    async def test_llm_classifies_correctly(self):
        """LLM classification maps 'urgent' to 'high'."""
        settings = _mock_settings(email_user="test@example.com", email_password="pass")
        llm_callable = AsyncMock(return_value="urgent")
        check = EmailCheck(settings, llm_callable=llm_callable)

        result = await check._classify_email("URGENT: Server down", "ops@company.com")
        assert result == "high"
        llm_callable.assert_called_once()

    @pytest.mark.asyncio
    async def test_llm_unavailable_falls_back_to_keywords(self):
        """When LLM callable is None, falls back to keyword classification."""
        settings = _mock_settings(email_user="test@example.com", email_password="pass")
        check = EmailCheck(settings, llm_callable=None)

        result = await check._classify_email("URGENT: Server down", "ops@company.com")
        assert result == "high"  # keyword match

    @pytest.mark.asyncio
    async def test_sender_reputation_bypasses_llm(self):
        """When sender has sufficient reputation, LLM is not called."""
        settings = _mock_settings(email_user="test@example.com", email_password="pass")
        llm_callable = AsyncMock(return_value="actionable")
        check = EmailCheck(settings, llm_callable=llm_callable)

        # Build up reputation (5+ consistent entries needed)
        sender = "alerts@monitoring.com"
        now = datetime.now(UTC)
        check._sender_reputation[sender] = [
            ("high", now - timedelta(hours=i)) for i in range(6)
        ]

        result = await check._classify_email("Alert triggered", sender)
        assert result == "high"
        llm_callable.assert_not_called()

    @pytest.mark.asyncio
    async def test_budget_check_gates_llm(self):
        """When budget_check returns False, LLM is not called."""
        settings = _mock_settings(email_user="test@example.com", email_password="pass")
        llm_callable = AsyncMock(return_value="actionable")
        budget_check = lambda: False
        check = EmailCheck(settings, llm_callable=llm_callable, budget_check=budget_check)

        result = await check._classify_email("Some email", "someone@example.com")
        llm_callable.assert_not_called()
        assert result in ("high", "normal", "low")  # fell back to keywords


# ===========================================================================
# TestEmailSenderReputation — 3 tests
# ===========================================================================


class TestEmailSenderReputation:
    """Tests for EmailCheck sender reputation tracking."""

    def test_builds_reputation(self):
        """Reputation builds as classifications accumulate."""
        settings = _mock_settings(email_user="test@example.com", email_password="pass")
        check = EmailCheck(settings)
        sender = "alerts@company.com"

        # Add 5 consistent entries
        for _ in range(5):
            check._update_reputation(sender, "high")

        # Now reputation should return "high"
        assert check._get_sender_reputation(sender) == "high"

    def test_reputation_decays_after_30_days(self):
        """Old reputation entries are pruned on update."""
        settings = _mock_settings(email_user="test@example.com", email_password="pass")
        check = EmailCheck(settings)
        sender = "old@company.com"

        # Add old entries (>30 days)
        old_time = datetime.now(UTC) - timedelta(days=35)
        check._sender_reputation[sender] = [("high", old_time)] * 6

        # Update with new classification — old entries should be pruned
        check._update_reputation(sender, "low")

        entries = check._sender_reputation[sender]
        assert len(entries) == 1  # only the new one
        assert entries[0][0] == "low"

    def test_unknown_senders_get_full_classification(self):
        """Senders with no reputation return None (needs full classification)."""
        settings = _mock_settings(email_user="test@example.com", email_password="pass")
        check = EmailCheck(settings)

        assert check._get_sender_reputation("unknown@example.com") is None


# ===========================================================================
# TestDriveSignificance — 3 tests
# ===========================================================================


class TestDriveSignificance:
    """Tests for DriveCheck significance scoring."""

    def test_shared_file_high_significance(self):
        """New file shared by someone else is high significance."""
        result = DriveCheck._score_significance({
            "name": "Shared Document",
            "mimeType": "application/vnd.google-apps.document",
            "sharingUser": {"displayName": "Alice"},
        })
        assert result == "high"

    def test_google_doc_edit_normal(self):
        """Google Docs edit with different created/modified is normal."""
        result = DriveCheck._score_significance({
            "name": "My Document",
            "mimeType": "application/vnd.google-apps.document",
            "createdTime": "2026-01-01T00:00:00",
            "modifiedTime": "2026-04-01T12:00:00",
        })
        assert result == "normal"

    @pytest.mark.asyncio
    async def test_contextualize_enriches_summary(self):
        """DriveCheck contextualizes files with recent conversations."""
        settings = _mock_settings()
        heart = MagicMock()

        mock_episode = MagicMock()
        mock_episode.score = 0.9
        mock_episode.summary = "We discussed the Q1 report earlier"
        heart.search_episodes = AsyncMock(return_value=[mock_episode])

        check = DriveCheck(settings, heart=heart)
        context = await check._contextualize("Q1 Report.docx")
        assert context is not None
        assert "recent conversation" in context.lower()

    @pytest.mark.asyncio
    async def test_contextualize_no_heart_returns_none(self):
        """Without heart, contextualize returns None."""
        settings = _mock_settings()
        check = DriveCheck(settings, heart=None)
        context = await check._contextualize("test.pdf")
        assert context is None
