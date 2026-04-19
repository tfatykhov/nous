"""F047: Unit tests for the actionability classifier.

No DB required — pure classification logic.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from nous.heart.actionability import ActionabilityClassifier


# ---------------------------------------------------------------------------
# Tier 0 — hard filters
# ---------------------------------------------------------------------------


class TestHardFilter:
    @pytest.mark.asyncio
    async def test_person_category_returns_false(self):
        c = ActionabilityClassifier()
        actionable, conf, tier = await c.classify("Tim's email", category="person")
        assert actionable is False
        assert conf == 1.0
        assert tier == "hard_filter"

    @pytest.mark.asyncio
    async def test_preference_category_returns_false(self):
        c = ActionabilityClassifier()
        actionable, _, tier = await c.classify("prefers dark mode", category="preference")
        assert actionable is False
        assert tier == "hard_filter"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("tag", ["resolved", "Resolved", "RESOLVED", "identity", "IDENTITY"])
    async def test_hard_no_tags_case_insensitive(self, tag):
        c = ActionabilityClassifier()
        actionable, _, tier = await c.classify("some content", tags=[tag])
        assert actionable is False
        assert tier == "hard_filter"

    @pytest.mark.asyncio
    async def test_non_hard_tag_passes_through(self):
        c = ActionabilityClassifier()
        actionable, _, tier = await c.classify(
            "TODO fix the CI pipeline",
            tags=["nonblocking"],
        )
        # Action pattern fires in tier 1 despite non-hard tag
        assert actionable is True
        assert tier == "heuristic_action"


# ---------------------------------------------------------------------------
# Tier 1 — heuristics (positive-wins-over-negative)
# ---------------------------------------------------------------------------


class TestHeuristicTier:
    """Each case here is a false-negative Codex/review agents identified
    against PR #335. These MUST classify as actionable — they are the
    regression guard for the whole F047 effort.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize("content", [
        "I need to add idempotency and side-effect tests before merge",
        "TODO: handle the timeout branch, not an edge case anymore",
        "I need to update the worker so it should treat timeouts as retryable",
        "I need to review PR #231 before Monday",
        "Need to rebase branch feat/F040-densification before CI runs",
        "I need to draft the three-tier fix for the truncation bug",
        "I need to fix the awaiting_check status, which is required but never set",
    ])
    async def test_action_wins_over_observation_substring(self, content):
        """PR #335 false-negatives must classify actionable despite observation substrings."""
        c = ActionabilityClassifier()
        actionable, conf, tier = await c.classify(content)
        assert actionable is True, f"{content!r} should be actionable"
        assert conf >= 0.8
        assert tier == "heuristic_action"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("content", [
        "Recurring false alarm from Tuesday's heartbeat run",
        "This fact is resolved — admission guardrails now block stale facts",
        "Task completion signals encoded as censors",
        "Tim's email address is tim@example.com",
        "These facts are stale and should no longer surface",
    ])
    async def test_observation_without_action_classified_false(self, content):
        """Content matching a canonical observation pattern must classify False."""
        c = ActionabilityClassifier()
        actionable, conf, tier = await c.classify(content)
        assert actionable is False
        assert tier == "heuristic_observation"
        assert conf >= 0.8

    @pytest.mark.asyncio
    @pytest.mark.parametrize("content", [
        "The architecture should treat timeouts as a fundamental design constraint",
        "Check-type nodes never get that command executed because only subtask nodes transition",
    ])
    async def test_broad_phrasing_routes_to_default_no_llm(self, content):
        """F047 intentionally does NOT suppress these broad phrases via substrings
        — they'd create false negatives when combined with action language.
        Without an LLM, these hit the default tier (actionable=False, low confidence).
        With an LLM enabled, they route to tier 2 for a real verdict.
        """
        c = ActionabilityClassifier(llm=None, default_when_unknown=False)
        actionable, conf, tier = await c.classify(content)
        assert actionable is False
        assert tier == "default"
        assert conf == 0.3


# ---------------------------------------------------------------------------
# Tier 2 — LLM disambiguation
# ---------------------------------------------------------------------------


class TestLLMTier:
    @pytest.mark.asyncio
    async def test_ambiguous_content_routes_to_llm(self, monkeypatch):
        """Both action and observation patterns match → LLM decides."""
        async def fake_call(**kwargs):
            return {"actionable": True, "confidence": 0.7, "reason": "explicit todo"}

        import nous.heart.actionability as mod
        monkeypatch.setattr(
            "nous.handlers.call_background_llm_structured",
            fake_call,
        )

        fake_llm = MagicMock()
        c = ActionabilityClassifier(llm=fake_llm)
        # "todo" (action) AND "is resolved" (observation) both match
        actionable, conf, tier = await c.classify("TODO — this is resolved by tomorrow")
        assert tier == "llm"
        assert conf == 0.7
        assert actionable is True

    @pytest.mark.asyncio
    async def test_llm_malformed_response_defaults(self, monkeypatch):
        """If LLM returns dict without 'actionable' key, fall through to default.

        This guards against silent-False from .get('actionable', False) bug.
        """
        async def bad_call(**kwargs):
            return {"confidence": 0.9, "reason": "no verdict"}  # missing 'actionable'

        monkeypatch.setattr(
            "nous.handlers.call_background_llm_structured",
            bad_call,
        )

        fake_llm = MagicMock()
        c = ActionabilityClassifier(llm=fake_llm, default_when_unknown=False)
        actionable, conf, tier = await c.classify("TODO is resolved")
        assert tier == "default"
        assert actionable is False
        assert conf == 0.3

    @pytest.mark.asyncio
    async def test_llm_actionable_explicit_none_defaults(self, monkeypatch):
        """LLM returning {'actionable': None} must NOT silently coerce to False.

        Without the explicit None guard, bool(None) → False and the fact
        gets silently suppressed with 'llm' tier + whatever confidence the
        LLM returned — masking a broken LLM response as a real verdict.
        """
        async def bad_call(**kwargs):
            return {"actionable": None, "confidence": 0.95, "reason": "unsure"}

        monkeypatch.setattr(
            "nous.handlers.call_background_llm_structured",
            bad_call,
        )

        fake_llm = MagicMock()
        c = ActionabilityClassifier(llm=fake_llm)
        _, conf, tier = await c.classify("TODO is resolved")
        assert tier == "default"
        assert conf == 0.3

    @pytest.mark.asyncio
    async def test_llm_raises_falls_to_default(self, monkeypatch):
        async def explode(**kwargs):
            raise RuntimeError("LLM down")

        monkeypatch.setattr(
            "nous.handlers.call_background_llm_structured",
            explode,
        )

        fake_llm = MagicMock()
        c = ActionabilityClassifier(llm=fake_llm)
        actionable, _, tier = await c.classify("TODO is resolved")
        assert tier == "default"

    @pytest.mark.asyncio
    async def test_llm_success_emits_info_log(self, monkeypatch, caplog):
        """Successful LLM classifications must log at INFO so operators have
        symmetric visibility with the default-path log, not silent success."""
        import logging as _logging

        async def fake_call(**kwargs):
            return {"actionable": True, "confidence": 0.82, "reason": "explicit ask"}

        monkeypatch.setattr(
            "nous.handlers.call_background_llm_structured",
            fake_call,
        )

        fake_llm = MagicMock()
        c = ActionabilityClassifier(llm=fake_llm)

        with caplog.at_level(_logging.INFO, logger="nous.heart.actionability"):
            actionable, conf, tier = await c.classify("TODO is resolved")

        assert tier == "llm" and actionable is True and conf == 0.82
        success_logs = [
            r for r in caplog.records
            if r.levelno == _logging.INFO and "LLM classified" in r.getMessage()
        ]
        assert success_logs, (
            "expected an INFO log on successful LLM classification; "
            f"got records: {[(r.levelname, r.getMessage()) for r in caplog.records]}"
        )
        msg = success_logs[0].getMessage()
        assert "actionable=True" in msg
        assert "0.82" in msg

    @pytest.mark.asyncio
    async def test_no_llm_no_heuristic_returns_default(self):
        c = ActionabilityClassifier(llm=None, default_when_unknown=False)
        # "banana" matches nothing
        actionable, conf, tier = await c.classify("banana smoothie recipe")
        assert actionable is False
        assert tier == "default"
        assert conf == 0.3

    @pytest.mark.asyncio
    async def test_default_when_unknown_true_reverses(self):
        c = ActionabilityClassifier(llm=None, default_when_unknown=True)
        actionable, _, tier = await c.classify("banana smoothie recipe")
        assert actionable is True
        assert tier == "default"


# ---------------------------------------------------------------------------
# Budget gating
# ---------------------------------------------------------------------------


class TestBudgetGate:
    @pytest.mark.asyncio
    async def test_budget_exhausted_skips_llm(self, monkeypatch):
        """When budget_check() returns False, LLM is not called."""
        called = {"n": 0}

        async def fake_call(**kwargs):
            called["n"] += 1
            return {"actionable": True, "confidence": 0.9, "reason": "x"}

        monkeypatch.setattr("nous.handlers.call_background_llm_structured", fake_call)

        fake_llm = MagicMock()
        c = ActionabilityClassifier(llm=fake_llm, budget_check=lambda: False)
        _, _, tier = await c.classify("TODO is resolved")  # ambiguous
        assert tier == "default"
        assert called["n"] == 0

    @pytest.mark.asyncio
    async def test_budget_ok_allows_llm(self, monkeypatch):
        async def fake_call(**kwargs):
            return {"actionable": False, "confidence": 0.8, "reason": "obs"}

        monkeypatch.setattr("nous.handlers.call_background_llm_structured", fake_call)

        fake_llm = MagicMock()
        c = ActionabilityClassifier(llm=fake_llm, budget_check=lambda: True)
        _, _, tier = await c.classify("TODO is resolved")
        assert tier == "llm"
