"""F047: Unit tests for the actionability backfill handler.

Exercises the handler against a mocked DB to verify batching, idempotency,
and the CancelledError re-raise contract. Integration with Postgres is
implicit via the production code path; we mock the session here to keep
tests fast and DB-free.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from nous.handlers.actionability_backfill import (
    ActionabilityBackfillHandler,
    run_backfill_with_supervision,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Advisory lock
# ---------------------------------------------------------------------------


class TestAdvisoryLock:
    @pytest.mark.asyncio
    async def test_advisory_lock_key_is_stable_per_agent(self):
        db = MagicMock()
        classifier = MagicMock()
        h1 = ActionabilityBackfillHandler(db, classifier, "agent-one")
        h2 = ActionabilityBackfillHandler(db, classifier, "agent-one")
        h3 = ActionabilityBackfillHandler(db, classifier, "agent-two")
        assert h1._advisory_lock_key() == h2._advisory_lock_key()
        assert h1._advisory_lock_key() != h3._advisory_lock_key()

    @pytest.mark.asyncio
    async def test_lock_held_returns_skipped(self):
        """Second process sees lock_held=False → returns skipped."""
        db = MagicMock()

        class LockSession:
            async def __aenter__(self):
                m = MagicMock()
                res = MagicMock()
                res.scalar = MagicMock(return_value=False)  # lock held
                m.execute = AsyncMock(return_value=res)
                return m

            async def __aexit__(self, *a):
                return False

        db.session = MagicMock(return_value=LockSession())

        classifier = MagicMock()
        h = ActionabilityBackfillHandler(db, classifier, "agent-x")
        result = await h.run_once()
        assert result == {"skipped": True, "reason": "lock_held"}


# ---------------------------------------------------------------------------
# Supervision wrapper
# ---------------------------------------------------------------------------


class TestSupervision:
    @pytest.mark.asyncio
    async def test_cancelled_error_reraises(self):
        handler = MagicMock()

        async def explode():
            raise asyncio.CancelledError()

        handler.run_once = explode

        with pytest.raises(asyncio.CancelledError):
            await run_backfill_with_supervision(handler)

    @pytest.mark.asyncio
    async def test_generic_exception_logged_and_swallowed(self, caplog):
        handler = MagicMock()

        async def explode():
            raise RuntimeError("boom")

        handler.run_once = explode

        # Should NOT raise — supervision swallows generic errors.
        await run_backfill_with_supervision(handler)
        assert "F047 backfill failed" in caplog.text

    @pytest.mark.asyncio
    async def test_happy_path_completes_without_exception(self):
        """Supervision wrapper returns cleanly on success."""
        handler = MagicMock()
        result_seen = {}

        async def ok():
            result_seen["called"] = True
            return {"total": 3, "classified": 3, "errors": 0, "elapsed_s": 0.5}

        handler.run_once = ok

        # Should return None (coroutine completes) without raising.
        ret = await run_backfill_with_supervision(handler)
        assert ret is None
        assert result_seen["called"] is True


class TestTokenBudget:
    """F047: backfill injects a budget gate into the classifier."""

    def test_budget_check_installed_on_init(self):
        """Handler mutates classifier._budget_check so tier-2 stops at cap."""
        db = MagicMock()

        # Plain object (not MagicMock) so attribute identity is real.
        class Stub:
            _budget_check = None

        classifier = Stub()

        handler = ActionabilityBackfillHandler(
            db, classifier, "agent-x", token_budget=500,
        )

        # Budget gate installed; 500 tokens / 250 per call = 2 max calls.
        # Bound methods don't have stable identity across accesses, so
        # verify the callable the classifier sees is bound to this handler.
        assert classifier._budget_check is not None
        assert getattr(classifier._budget_check, "__self__", None) is handler
        assert handler._max_llm_calls == 2
        assert handler._llm_calls_used == 0

    def test_budget_ok_flips_to_false_after_cap(self):
        db = MagicMock()

        class Stub:
            _budget_check = None

        classifier = Stub()
        handler = ActionabilityBackfillHandler(
            db, classifier, "agent-x", token_budget=250,  # 1 call cap
        )
        assert handler._budget_ok() is True
        handler._llm_calls_used = 1
        assert handler._budget_ok() is False

    @pytest.mark.asyncio
    async def test_llm_tier_increments_counter(self, monkeypatch):
        """Each tier=llm classification bumps the counter."""
        db = MagicMock()

        # Stub db.session for lock + updates
        class LockSession:
            async def __aenter__(self_inner):
                m = MagicMock()
                res = MagicMock(); res.scalar = MagicMock(return_value=True)
                m.execute = AsyncMock(return_value=res)
                return m
            async def __aexit__(self_inner, *a): return False

        class UpdateSession:
            async def __aenter__(self_inner):
                m = MagicMock(); m.execute = AsyncMock(); m.commit = AsyncMock()
                return m
            async def __aexit__(self_inner, *a): return False

        db.session = MagicMock(side_effect=[LockSession(), UpdateSession(), UpdateSession()])

        classifier = MagicMock()
        classifier._budget_check = None

        async def classify(content, cat, tags):
            return (True, 0.7, "llm")

        classifier.classify = AsyncMock(side_effect=classify)

        h = ActionabilityBackfillHandler(db, classifier, "agent-x", token_budget=10_000)

        batches = iter([
            [(uuid4(), "x", None, []), (uuid4(), "y", None, [])],
            [],
        ])

        async def fake_fetch():
            return next(batches)

        monkeypatch.setattr(h, "_fetch_batch", fake_fetch)

        await h.run_once()
        assert h._llm_calls_used == 2


# ---------------------------------------------------------------------------
# Classifier-error handling in _run_batches
# ---------------------------------------------------------------------------


class TestClassifierErrorHandling:
    @pytest.mark.asyncio
    async def test_classifier_raises_counts_error_continues(self, monkeypatch):
        """A single classify failure increments error counter; loop continues."""
        # Mock DB session used only for advisory lock and updates
        class LockSession:
            async def __aenter__(self_inner):
                m = MagicMock()
                res = MagicMock()
                res.scalar = MagicMock(return_value=True)
                m.execute = AsyncMock(return_value=res)
                return m

            async def __aexit__(self_inner, *a):
                return False

        class UpdateSession:
            async def __aenter__(self_inner):
                m = MagicMock()
                m.execute = AsyncMock()
                m.commit = AsyncMock()
                return m

            async def __aexit__(self_inner, *a):
                return False

        session_queue = [LockSession(), UpdateSession(), UpdateSession()]
        db = MagicMock()
        db.session = MagicMock(side_effect=session_queue)

        classifier = MagicMock()

        async def classify_side_effect(content, category, tags):
            if content == "first":
                raise RuntimeError("classifier fail")
            return (True, 0.9, "heuristic_action")

        classifier.classify = AsyncMock(side_effect=classify_side_effect)

        h = ActionabilityBackfillHandler(db, classifier, "agent-x")

        # Short-circuit _fetch_batch: one batch of 2, then empty (end of loop)
        batches = iter([
            [
                (uuid4(), "first", None, []),
                (uuid4(), "second", None, []),
            ],
            [],
        ])

        async def fake_fetch():
            return next(batches)

        monkeypatch.setattr(h, "_fetch_batch", fake_fetch)

        result = await h.run_once()

        assert result["total"] == 2
        assert result["classified"] == 1
        assert result["errors"] == 1


# ---------------------------------------------------------------------------
# Summary / observability (INFO log tier breakdown + budget-exhausted WARNING)
# ---------------------------------------------------------------------------


class TestBackfillSummary:
    @pytest.mark.asyncio
    async def test_summary_includes_tier_counts(self, monkeypatch):
        """run_once return value has per-tier counts and LLM budget usage."""
        class LockSession:
            async def __aenter__(self_inner):
                m = MagicMock()
                res = MagicMock(); res.scalar = MagicMock(return_value=True)
                m.execute = AsyncMock(return_value=res)
                return m
            async def __aexit__(self_inner, *a): return False

        class UpdateSession:
            async def __aenter__(self_inner):
                m = MagicMock(); m.execute = AsyncMock(); m.commit = AsyncMock()
                return m
            async def __aexit__(self_inner, *a): return False

        db = MagicMock()
        db.session = MagicMock(side_effect=[
            LockSession(),
            UpdateSession(), UpdateSession(), UpdateSession(),
        ])

        classifier = MagicMock()
        classifier._budget_check = None
        tiers = iter(["llm", "heuristic_action", "default"])

        async def classify(content, cat, tags):
            return (True, 0.7, next(tiers))

        classifier.classify = AsyncMock(side_effect=classify)
        h = ActionabilityBackfillHandler(db, classifier, "agent-x", token_budget=10_000)

        batches = iter([
            [(uuid4(), "a", None, []), (uuid4(), "b", None, []), (uuid4(), "c", None, [])],
            [],
        ])

        async def fake_fetch():
            return next(batches)

        monkeypatch.setattr(h, "_fetch_batch", fake_fetch)
        summary = await h.run_once()

        assert summary["total"] == 3
        assert summary["classified"] == 3
        assert summary["errors"] == 0
        assert summary["tiers"] == {"llm": 1, "heuristic_action": 1, "default": 1}
        assert summary["llm_calls_used"] == 1
        assert summary["llm_budget"] == 40

    @pytest.mark.asyncio
    async def test_budget_exhausted_emits_warning(self, monkeypatch, caplog):
        """When LLM budget hits cap AND defaults happened, operator sees a
        WARNING pointing at NOUS_ACTIONABILITY_BACKFILL_TOKEN_BUDGET."""
        import logging as _logging

        class LockSession:
            async def __aenter__(self_inner):
                m = MagicMock()
                res = MagicMock(); res.scalar = MagicMock(return_value=True)
                m.execute = AsyncMock(return_value=res)
                return m
            async def __aexit__(self_inner, *a): return False

        class UpdateSession:
            async def __aenter__(self_inner):
                m = MagicMock(); m.execute = AsyncMock(); m.commit = AsyncMock()
                return m
            async def __aexit__(self_inner, *a): return False

        db = MagicMock()
        # token_budget=250 → _max_llm_calls=1. Two facts: 1st LLM (budget
        # consumed), 2nd defaulted.
        db.session = MagicMock(side_effect=[LockSession(), UpdateSession(), UpdateSession()])
        classifier = MagicMock()
        classifier._budget_check = None
        tiers = iter(["llm", "default"])

        async def classify(content, cat, tags):
            return (False, 0.5, next(tiers))

        classifier.classify = AsyncMock(side_effect=classify)
        h = ActionabilityBackfillHandler(db, classifier, "agent-x", token_budget=250)

        batches = iter([
            [(uuid4(), "a", None, []), (uuid4(), "b", None, [])],
            [],
        ])

        async def fake_fetch():
            return next(batches)

        monkeypatch.setattr(h, "_fetch_batch", fake_fetch)

        with caplog.at_level(_logging.WARNING, logger="nous.handlers.actionability_backfill"):
            summary = await h.run_once()

        assert summary["llm_calls_used"] == 1
        assert summary["llm_budget"] == 1
        assert summary["tiers"].get("default", 0) == 1

        warns = [r for r in caplog.records if r.levelno == _logging.WARNING]
        assert any(
            "budget exhausted" in r.getMessage()
            and "NOUS_ACTIONABILITY_BACKFILL_TOKEN_BUDGET" in r.getMessage()
            for r in warns
        ), f"expected budget-exhausted WARNING; got: {[r.getMessage() for r in warns]}"

    @pytest.mark.asyncio
    async def test_budget_exhausted_without_defaults_no_warning(self, monkeypatch, caplog):
        """If budget is fully used but every fact was LLM-classified (no
        defaults), no warning — there was nothing missed."""
        import logging as _logging

        class LockSession:
            async def __aenter__(self_inner):
                m = MagicMock()
                res = MagicMock(); res.scalar = MagicMock(return_value=True)
                m.execute = AsyncMock(return_value=res)
                return m
            async def __aexit__(self_inner, *a): return False

        class UpdateSession:
            async def __aenter__(self_inner):
                m = MagicMock(); m.execute = AsyncMock(); m.commit = AsyncMock()
                return m
            async def __aexit__(self_inner, *a): return False

        db = MagicMock()
        db.session = MagicMock(side_effect=[LockSession(), UpdateSession()])
        classifier = MagicMock()
        classifier._budget_check = None

        async def classify(content, cat, tags):
            return (True, 0.9, "llm")

        classifier.classify = AsyncMock(side_effect=classify)
        h = ActionabilityBackfillHandler(db, classifier, "agent-x", token_budget=250)

        batches = iter([[(uuid4(), "a", None, [])], []])

        async def fake_fetch():
            return next(batches)

        monkeypatch.setattr(h, "_fetch_batch", fake_fetch)
        with caplog.at_level(_logging.WARNING, logger="nous.handlers.actionability_backfill"):
            await h.run_once()

        assert not any(
            "budget exhausted" in r.getMessage()
            for r in caplog.records if r.levelno == _logging.WARNING
        )
