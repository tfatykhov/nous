"""F047: heartbeat integration — _embedding_search respects persisted actionable.

Tests that SelfInitiatedCheck surfaces/suppresses facts based on the
persisted `actionable` column, with a positive-wins fallback for NULL
(unclassified) rows.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from nous.heartbeat.checks import SelfInitiatedCheck


def _mock_settings() -> MagicMock:
    s = MagicMock()
    s.agent_id = "test-agent"
    s.heartbeat_self_initiated_interval = 1800
    return s


def _make_fact(*, content, actionable, score=0.9, fid="fact-1"):
    """Build a FactSummary-shaped mock with the fields _embedding_search reads."""
    fact = MagicMock()
    fact.id = fid
    fact.content = content
    fact.score = score
    fact.actionable = actionable
    fact.actionable_confidence = 0.85 if actionable is not None else None
    fact.category = None
    fact.tags = None
    return fact


class TestActionablePersistedVerdict:
    @pytest.mark.asyncio
    async def test_actionable_true_surfaces_finding(self):
        """Persisted actionable=True → always surfaces, regardless of content."""
        heart = MagicMock()
        brain = MagicMock()
        settings = _mock_settings()

        embeddings = MagicMock()
        embeddings.embed_batch = AsyncMock(return_value=[[0.1] * 10] * 5)

        # Fact whose content looks like a pure observation, but classifier said True
        fact = _make_fact(
            content="This fact is resolved — but classifier flagged it as actionable",
            actionable=True,
        )
        heart.facts.search = AsyncMock(return_value=[fact])
        heart.search_episodes = AsyncMock(return_value=[])
        heart.schedules.get_due = AsyncMock(return_value=[])

        check = SelfInitiatedCheck(heart, brain, settings, embeddings=embeddings)
        result = await check.run()

        assert any(
            f.source == "facts" and f.raw_data.get("detection") == "embedding"
            for f in result.findings
        ), "actionable=True fact should surface as pending"

    @pytest.mark.asyncio
    async def test_actionable_false_suppresses_even_if_content_screams_action(self):
        """Persisted actionable=False → suppressed on ALL fact-detection paths.

        Reverse of the PR #335 bug: classifier gets the final say.
        Both the embedding path AND the keyword fallback must respect the verdict.
        """
        heart = MagicMock()
        brain = MagicMock()
        settings = _mock_settings()

        embeddings = MagicMock()
        embeddings.embed_batch = AsyncMock(return_value=[[0.1] * 10] * 5)

        fact = _make_fact(
            content="I need to follow up on this action needed TODO item",
            actionable=False,
        )
        heart.facts.search = AsyncMock(return_value=[fact])
        heart.search_episodes = AsyncMock(return_value=[])
        heart.schedules.get_due = AsyncMock(return_value=[])

        check = SelfInitiatedCheck(heart, brain, settings, embeddings=embeddings)
        result = await check.run()

        # Check BOTH embedding and keyword detection paths.
        fact_findings = [
            f for f in result.findings
            if f.source == "facts"
            and f.raw_data.get("detection") in ("embedding", "keyword")
        ]
        assert not fact_findings, (
            f"actionable=False fact must not surface via any path, got: {fact_findings}"
        )


class TestSqliteIntBoolCompat:
    """F047: actionable column comes back as 1/0 from SQLite, not True/False."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("raw_value,expected_surface", [
        (1, True),      # SQLite integer → should surface (== True)
        (True, True),   # Python bool True → should surface
        (0, False),     # SQLite integer 0 → suppressed
        (False, False), # Python bool False → suppressed
    ])
    async def test_int_and_bool_both_work(self, raw_value, expected_surface):
        """Code uses `== True/False` (not `is`) so both int and bool route correctly."""
        heart = MagicMock()
        brain = MagicMock()
        settings = _mock_settings()

        embeddings = MagicMock()
        embeddings.embed_batch = AsyncMock(return_value=[[0.1] * 10] * 5)

        fact = _make_fact(content="any content", actionable=raw_value)
        heart.facts.search = AsyncMock(return_value=[fact])
        heart.search_episodes = AsyncMock(return_value=[])
        heart.schedules.get_due = AsyncMock(return_value=[])

        check = SelfInitiatedCheck(heart, brain, settings, embeddings=embeddings)
        result = await check.run()

        embedding_findings = [
            f for f in result.findings if f.raw_data.get("detection") == "embedding"
        ]
        if expected_surface:
            assert embedding_findings, f"actionable={raw_value!r} should surface"
        else:
            assert not embedding_findings, f"actionable={raw_value!r} should suppress"


class TestKeywordPathRespectsActionable:
    """F047: the keyword fallback path honours persisted actionable verdict."""

    @pytest.mark.asyncio
    async def test_keyword_path_surfaces_actionable_true(self):
        heart = MagicMock()
        brain = MagicMock()
        settings = _mock_settings()

        # No embeddings → embedding path skipped; falls through to keyword search
        fact = _make_fact(
            content="Boring content that matches no action patterns",
            actionable=True,
        )
        heart.facts.search = AsyncMock(return_value=[fact])
        heart.search_episodes = AsyncMock(return_value=[])
        heart.schedules.get_due = AsyncMock(return_value=[])

        check = SelfInitiatedCheck(heart, brain, settings, embeddings=None)
        result = await check.run()

        keyword_findings = [
            f for f in result.findings if f.raw_data.get("detection") == "keyword"
        ]
        assert keyword_findings, (
            "actionable=True must surface via keyword path, regardless of content"
        )


class TestNullFallbackPositiveWins:
    """NULL (unclassified) rows go through legacy heuristic, but with
    the F047-fixed positive-wins ordering — this is the fix for the
    PR #335 short-circuit bug carried forward to unclassified rows.
    """

    @pytest.mark.asyncio
    async def test_null_action_phrasing_with_observation_substring_surfaces(self):
        """Action wins over observation substring in fallback path."""
        heart = MagicMock()
        brain = MagicMock()
        settings = _mock_settings()

        embeddings = MagicMock()
        embeddings.embed_batch = AsyncMock(return_value=[[0.1] * 10] * 5)

        # Contains "idempotency and side-effect" observation-like phrasing
        # AND "I need to" action phrasing. Pre-F047 behavior suppressed.
        fact = _make_fact(
            content="I need to add idempotency and side-effect tests before merge",
            actionable=None,  # unclassified → fallback path
        )
        heart.facts.search = AsyncMock(return_value=[fact])
        heart.search_episodes = AsyncMock(return_value=[])
        heart.schedules.get_due = AsyncMock(return_value=[])

        check = SelfInitiatedCheck(heart, brain, settings, embeddings=embeddings)
        result = await check.run()

        embedding_findings = [
            f for f in result.findings if f.raw_data.get("detection") == "embedding"
        ]
        assert embedding_findings, (
            "action-phrased NULL fact must surface even if an observation substring matches"
        )

    @pytest.mark.asyncio
    async def test_null_pure_observation_suppressed(self):
        """Pure observation (no action phrasing) still suppressed in fallback."""
        heart = MagicMock()
        brain = MagicMock()
        settings = _mock_settings()

        embeddings = MagicMock()
        embeddings.embed_batch = AsyncMock(return_value=[[0.1] * 10] * 5)

        fact = _make_fact(
            content="The task completion signals are encoded as censors",
            actionable=None,
        )
        heart.facts.search = AsyncMock(return_value=[fact])
        heart.search_episodes = AsyncMock(return_value=[])
        heart.schedules.get_due = AsyncMock(return_value=[])

        check = SelfInitiatedCheck(heart, brain, settings, embeddings=embeddings)
        result = await check.run()

        embedding_findings = [
            f for f in result.findings if f.raw_data.get("detection") == "embedding"
        ]
        assert not embedding_findings
