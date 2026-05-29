"""Tests for §2 — Haiku-Layered Three-Way Epistemic Gate.

Two scopes:
  - EpistemicClassifier.classify control flow: fail-open paths (flag off, no
    LLM, timeout, error, budget exhausted, malformed output) all return None;
    happy paths return the class; CancelledError propagates. The LLM is mocked
    (call_background_llm_structured is patched) — no real Haiku.
  - ContextEngine._epistemic_instruction prose mapping + the gated section
    (present flag-ON, absent flag-OFF, sibling to Context Safety).

These assert FAIL-OPEN paths + the class->prose mapping, NOT abstention
BEHAVIOR (behavior is only the prod probe in plan §7).
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nous.cognitive.epistemic import (
    _VALID_CLASSES,
    EpistemicClassifier,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_settings(
    *,
    epistemic_gate_enabled: bool = True,
    epistemic_gate_timeout_seconds: float = 2.0,
    epistemic_gate_max_per_hour: int = 500,
    epistemic_gate_model: str = "claude-haiku-4-5-20251001",
):
    return SimpleNamespace(
        epistemic_gate_enabled=epistemic_gate_enabled,
        epistemic_gate_timeout_seconds=epistemic_gate_timeout_seconds,
        epistemic_gate_max_per_hour=epistemic_gate_max_per_hour,
        epistemic_gate_model=epistemic_gate_model,
    )


def _make_classifier(llm=None, settings=None) -> EpistemicClassifier:
    return EpistemicClassifier(
        llm=llm if llm is not None else MagicMock(),
        settings=settings or _make_settings(),
    )


# classify() lazy-imports from nous.handlers, so patch at the source module.
_PATCH_TARGET = "nous.handlers.call_background_llm_structured"


# ---------------------------------------------------------------------------
# classify() — fail-open paths (all return None, never raise)
# ---------------------------------------------------------------------------


class TestClassifyFailOpen:
    @pytest.mark.asyncio
    async def test_flag_off_returns_none(self):
        c = _make_classifier(settings=_make_settings(epistemic_gate_enabled=False))
        assert await c.classify("What is a B-tree?") is None

    @pytest.mark.asyncio
    async def test_llm_none_returns_none(self):
        c = EpistemicClassifier(llm=None, settings=_make_settings())
        assert await c.classify("What is a B-tree?") is None

    @pytest.mark.asyncio
    async def test_empty_or_nonstring_returns_none(self):
        c = _make_classifier()
        assert await c.classify("") is None
        assert await c.classify("   ") is None
        assert await c.classify(None) is None  # type: ignore[arg-type]
        assert await c.classify(123) is None  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_llm_raises_returns_none(self):
        c = _make_classifier()
        with patch(_PATCH_TARGET, new=AsyncMock(side_effect=RuntimeError("boom"))):
            assert await c.classify("decent length query here") is None

    @pytest.mark.asyncio
    async def test_timeout_returns_none(self):
        c = _make_classifier()
        with patch(
            _PATCH_TARGET, new=AsyncMock(side_effect=asyncio.TimeoutError())
        ):
            assert await c.classify("decent length query here") is None

    @pytest.mark.asyncio
    async def test_malformed_non_dict_returns_none(self):
        c = _make_classifier()
        with patch(_PATCH_TARGET, new=AsyncMock(return_value="not a dict")):
            assert await c.classify("decent length query here") is None

    @pytest.mark.asyncio
    async def test_none_result_returns_none(self):
        c = _make_classifier()
        with patch(_PATCH_TARGET, new=AsyncMock(return_value=None)):
            assert await c.classify("decent length query here") is None

    @pytest.mark.asyncio
    async def test_invalid_class_value_returns_none(self):
        c = _make_classifier()
        with patch(
            _PATCH_TARGET,
            new=AsyncMock(return_value={"epistemic_class": "nonsense"}),
        ):
            assert await c.classify("decent length query here") is None

    @pytest.mark.asyncio
    async def test_budget_exhausted_returns_none(self):
        c = _make_classifier(settings=_make_settings(epistemic_gate_max_per_hour=2))
        ok = {"epistemic_class": "world_knowledge"}
        with patch(_PATCH_TARGET, new=AsyncMock(return_value=ok)):
            assert await c.classify("decent length query here") == "world_knowledge"
            assert await c.classify("decent length query here") == "world_knowledge"
            # Third call exceeds the 2/hr budget -> fail-open None.
            assert await c.classify("decent length query here") is None


# ---------------------------------------------------------------------------
# classify() — happy paths + CancelledError
# ---------------------------------------------------------------------------


class TestClassifyHappyPath:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("cls", ["grounded", "world_knowledge", "abstain"])
    async def test_returns_valid_class(self, cls):
        c = _make_classifier()
        with patch(
            _PATCH_TARGET, new=AsyncMock(return_value={"epistemic_class": cls})
        ):
            assert await c.classify("decent length query here") == cls

    @pytest.mark.asyncio
    async def test_cancelled_error_propagates(self):
        c = _make_classifier()
        with patch(
            _PATCH_TARGET, new=AsyncMock(side_effect=asyncio.CancelledError())
        ):
            with pytest.raises(asyncio.CancelledError):
                await c.classify("decent length query here")

    def test_valid_classes_constant(self):
        assert _VALID_CLASSES == {"grounded", "world_knowledge", "abstain"}


# ---------------------------------------------------------------------------
# _budget_consume unit
# ---------------------------------------------------------------------------


class TestBudgetConsume:
    @pytest.mark.asyncio
    async def test_budget_blocks_after_cap(self):
        c = _make_classifier(settings=_make_settings(epistemic_gate_max_per_hour=3))
        assert await c._budget_consume() is True
        assert await c._budget_consume() is True
        assert await c._budget_consume() is True
        assert await c._budget_consume() is False  # 4th over cap

    @pytest.mark.asyncio
    async def test_budget_disabled_when_zero(self):
        c = _make_classifier(settings=_make_settings(epistemic_gate_max_per_hour=0))
        for _ in range(10):
            assert await c._budget_consume() is True


# ---------------------------------------------------------------------------
# ContextEngine._epistemic_instruction prose mapping
# ---------------------------------------------------------------------------


def _make_context_engine(*, epistemic_gate_enabled: bool):
    from nous.cognitive.context import ContextEngine

    settings = SimpleNamespace(
        epistemic_gate_enabled=epistemic_gate_enabled,
        anti_hallucination_prompt=True,
    )
    brain = MagicMock()
    brain.embeddings = None
    heart = MagicMock()
    # ContextEngine only needs these on __init__; method-under-test is pure.
    return ContextEngine(brain, heart, settings, identity_prompt="")


class TestEpistemicInstruction:
    def test_grounded_prose(self):
        eng = _make_context_engine(epistemic_gate_enabled=True)
        text = eng._epistemic_instruction("grounded")
        assert "cite" in text.lower()
        assert "retrieved memory" in text.lower()

    def test_world_knowledge_permits_base(self):
        eng = _make_context_engine(epistemic_gate_enabled=True)
        text = eng._epistemic_instruction("world_knowledge")
        assert "you may" in text.lower()
        assert "broad knowledge" in text.lower()
        assert "do not refuse" in text.lower()

    def test_abstain_memory_only(self):
        eng = _make_context_engine(epistemic_gate_enabled=True)
        text = eng._epistemic_instruction("abstain")
        assert "answer only" in text.lower()
        assert "don't have that information" in text.lower()

    def test_none_returns_softened_prose(self):
        eng = _make_context_engine(epistemic_gate_enabled=True)
        text = eng._epistemic_instruction(None)
        # Softened prose must PERMIT base knowledge on general turns.
        assert "you may answer from your own broad knowledge" in text.lower()
        assert "do not refuse a general question" in text.lower()

    def test_unknown_returns_softened_prose(self):
        eng = _make_context_engine(epistemic_gate_enabled=True)
        assert eng._epistemic_instruction("garbage") == eng._epistemic_instruction(None)


# ---------------------------------------------------------------------------
# ContextEngine.build — gated section presence
# ---------------------------------------------------------------------------


class TestBuildSectionWiring:
    """§2 build() wiring. NOTE: a full ContextEngine.build() runs pgvector SQL,
    which the sqlite test DB cannot execute — so the section's actual APPEND
    inside build() is NOT unit-verifiable here (validated via the prod probe in
    plan §7). These tests cover the genuinely-real, sqlite-free pieces: the
    SECTION_TIERS registration and the prose the gate block would inject."""

    def test_section_tier_registered(self):
        from nous.cognitive.context import SECTION_TIERS

        assert SECTION_TIERS.get("Epistemic Routing") == "dynamic"

    def test_instruction_nonempty_for_all_routing_inputs(self):
        # The gate block only appends when _epistemic_instruction(...) is
        # truthy. Confirm it is non-empty for every input the classifier can
        # produce (incl. the fail-open None and an unknown value).
        eng = _make_context_engine(epistemic_gate_enabled=True)
        for cls in ("grounded", "world_knowledge", "abstain", None, "garbage"):
            assert eng._epistemic_instruction(cls).strip()
