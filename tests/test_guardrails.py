"""Tests for nous/brain/guardrails.py — GuardrailEngine and helpers.

Uses unittest.mock for AsyncSession — no real DB required.
Tests cover _to_cel_value, _sanitize_context, _build_activation,
_get_expression, _jsonb_to_cel, _evaluate (incl. timeout), and check().
"""

from __future__ import annotations

import concurrent.futures
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from celpy import celtypes

from nous.brain.guardrails import (
    GuardrailEngine,
    _build_activation,
    _sanitize_context,
    _to_cel_value,
)
from nous.brain.schemas import GuardrailResult
from nous.storage.models import Guardrail


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_guardrail(
    name: str = "test-guard",
    condition=None,
    severity: str = "block",
    priority: int = 100,
) -> MagicMock:
    """Return a mock Guardrail ORM object."""
    g = MagicMock(spec=Guardrail)
    g.id = uuid.uuid4()
    g.name = name
    g.condition = condition if condition is not None else "false"
    g.severity = severity
    g.priority = priority
    g.active = True
    return g


def make_session(guardrails: list) -> AsyncMock:
    """Return a minimal AsyncSession mock that yields *guardrails* from execute()."""
    session = AsyncMock()
    scalars_mock = MagicMock()
    scalars_mock.all.return_value = guardrails
    result_mock = MagicMock()
    result_mock.scalars.return_value = scalars_mock
    session.execute.return_value = result_mock
    session.add = MagicMock()
    return session


def default_activation(stakes: str = "high", confidence: float = 0.3) -> dict:
    return _build_activation("test decision", stakes, confidence)


# ---------------------------------------------------------------------------
# _to_cel_value
# ---------------------------------------------------------------------------


class TestToCelValue:
    def test_bool_true(self):
        v = _to_cel_value(True)
        assert isinstance(v, celtypes.BoolType)
        assert v == celtypes.BoolType(True)

    def test_bool_false(self):
        v = _to_cel_value(False)
        assert isinstance(v, celtypes.BoolType)
        assert v == celtypes.BoolType(False)

    def test_int(self):
        v = _to_cel_value(42)
        assert isinstance(v, celtypes.IntType)
        assert v == celtypes.IntType(42)

    def test_float(self):
        v = _to_cel_value(0.75)
        assert isinstance(v, celtypes.DoubleType)
        assert v == celtypes.DoubleType(0.75)

    def test_str(self):
        v = _to_cel_value("hello")
        assert isinstance(v, celtypes.StringType)
        assert v == celtypes.StringType("hello")

    def test_list(self):
        v = _to_cel_value([1, "a"])
        assert isinstance(v, celtypes.ListType)
        assert v[0] == celtypes.IntType(1)
        assert v[1] == celtypes.StringType("a")

    def test_dict(self):
        v = _to_cel_value({"key": "val"})
        assert isinstance(v, celtypes.MapType)
        assert v[celtypes.StringType("key")] == celtypes.StringType("val")

    def test_none_becomes_false(self):
        v = _to_cel_value(None)
        assert isinstance(v, celtypes.BoolType)
        assert v == celtypes.BoolType(False)

    def test_unknown_type_becomes_string(self):
        v = _to_cel_value(object())
        assert isinstance(v, celtypes.StringType)

    def test_nested_list(self):
        v = _to_cel_value([[1, 2], [3]])
        assert isinstance(v, celtypes.ListType)
        assert isinstance(v[0], celtypes.ListType)

    def test_nested_dict(self):
        v = _to_cel_value({"outer": {"inner": 99}})
        outer = v[celtypes.StringType("outer")]
        assert isinstance(outer, celtypes.MapType)
        assert outer[celtypes.StringType("inner")] == celtypes.IntType(99)

    def test_empty_list(self):
        v = _to_cel_value([])
        assert isinstance(v, celtypes.ListType)
        assert len(v) == 0

    def test_empty_dict(self):
        v = _to_cel_value({})
        assert isinstance(v, celtypes.MapType)


# ---------------------------------------------------------------------------
# _sanitize_context
# ---------------------------------------------------------------------------


class TestSanitizeContext:
    def test_none_returns_empty(self):
        assert _sanitize_context(None) == {}

    def test_empty_dict(self):
        assert _sanitize_context({}) == {}

    def test_all_scalar_types_pass_through(self):
        ctx = {"s": "str", "i": 1, "f": 1.5, "b": True, "n": None}
        assert _sanitize_context(ctx) == ctx

    def test_list_of_scalars_passes(self):
        ctx = {"tags": ["a", "b", 1]}
        assert _sanitize_context(ctx) == ctx

    def test_list_drops_non_scalars(self):
        ctx = {"items": ["keep", object(), 42]}
        result = _sanitize_context(ctx)
        assert result["items"] == ["keep", 42]

    def test_nested_dict_recursion(self):
        ctx = {"outer": {"inner": "value"}}
        result = _sanitize_context(ctx)
        assert result["outer"]["inner"] == "value"

    def test_non_serializable_value_dropped(self):
        ctx = {"bad": object(), "good": "yes"}
        result = _sanitize_context(ctx)
        assert "bad" not in result
        assert result["good"] == "yes"


# ---------------------------------------------------------------------------
# _build_activation
# ---------------------------------------------------------------------------


class TestBuildActivation:
    _K = celtypes.StringType

    def test_decision_key_present(self):
        activation = _build_activation("desc", "high", 0.9)
        assert "decision" in activation

    def test_description_and_stakes(self):
        activation = _build_activation("my desc", "critical", 0.8)
        d = activation["decision"]
        assert d[self._K("description")] == celtypes.StringType("my desc")
        assert d[self._K("stakes")] == celtypes.StringType("critical")

    def test_confidence_is_double(self):
        activation = _build_activation("desc", "low", 0.5)
        d = activation["decision"]
        assert isinstance(d[self._K("confidence")], celtypes.DoubleType)

    def test_has_pattern_true(self):
        activation = _build_activation("desc", "low", 0.5, pattern="p")
        d = activation["decision"]
        assert d[self._K("has_pattern")] == celtypes.BoolType(True)

    def test_has_pattern_false_when_none(self):
        activation = _build_activation("desc", "low", 0.5, pattern=None)
        d = activation["decision"]
        assert d[self._K("has_pattern")] == celtypes.BoolType(False)

    def test_has_tags_true(self):
        activation = _build_activation("desc", "low", 0.5, tags=["x"])
        d = activation["decision"]
        assert d[self._K("has_tags")] == celtypes.BoolType(True)

    def test_has_tags_false_when_empty(self):
        activation = _build_activation("desc", "low", 0.5, tags=[])
        d = activation["decision"]
        assert d[self._K("has_tags")] == celtypes.BoolType(False)

    def test_reason_count(self):
        reasons = [{"type": "analysis", "text": "r1"}, {"type": "pattern", "text": "r2"}]
        activation = _build_activation("desc", "low", 0.5, reasons=reasons)
        d = activation["decision"]
        assert d[self._K("reason_count")] == celtypes.IntType(2)

    def test_quality_score_defaults_to_zero(self):
        activation = _build_activation("desc", "low", 0.5)
        d = activation["decision"]
        assert d[self._K("quality_score")] == celtypes.DoubleType(0.0)

    def test_quality_score_passed_through(self):
        activation = _build_activation("desc", "low", 0.5, quality_score=0.7)
        d = activation["decision"]
        assert d[self._K("quality_score")] == celtypes.DoubleType(0.7)


# ---------------------------------------------------------------------------
# GuardrailEngine — expression compilation + validation
# ---------------------------------------------------------------------------


class TestValidateExpression:
    def setup_method(self):
        self.engine = GuardrailEngine()

    def test_valid_expression(self):
        valid, err = self.engine.validate_expression("decision.stakes == 'high'")
        assert valid is True
        assert err is None

    def test_invalid_expression(self):
        valid, err = self.engine.validate_expression("!!! not CEL @@@")
        assert valid is False
        assert err is not None

    def test_compile_is_cached(self):
        expr = "decision.confidence > 0.5"
        prog1 = self.engine._compile_program(expr)
        prog2 = self.engine._compile_program(expr)
        assert prog1 is prog2  # LRU cache returns same object


# ---------------------------------------------------------------------------
# GuardrailEngine._evaluate
# ---------------------------------------------------------------------------


class TestEvaluate:
    def setup_method(self):
        self.engine = GuardrailEngine()

    def test_matching_returns_true(self):
        activation = default_activation(stakes="high")
        assert self.engine._evaluate("decision.stakes == 'high'", activation) is True

    def test_non_matching_returns_false(self):
        activation = default_activation(stakes="low")
        assert self.engine._evaluate("decision.stakes == 'high'", activation) is False

    def test_compound_true(self):
        activation = default_activation(stakes="high", confidence=0.3)
        assert self.engine._evaluate(
            "decision.stakes == 'high' && decision.confidence < 0.5", activation
        ) is True

    def test_compound_false_when_confidence_high(self):
        activation = default_activation(stakes="high", confidence=0.8)
        assert self.engine._evaluate(
            "decision.stakes == 'high' && decision.confidence < 0.5", activation
        ) is False

    def test_timeout_block_fails_closed(self):
        activation = default_activation()
        with patch.object(
            concurrent.futures.Future, "result", side_effect=concurrent.futures.TimeoutError
        ):
            result = self.engine._evaluate("true", activation, severity="block")
        assert result is True

    def test_timeout_absolute_fails_closed(self):
        activation = default_activation()
        with patch.object(
            concurrent.futures.Future, "result", side_effect=concurrent.futures.TimeoutError
        ):
            result = self.engine._evaluate("true", activation, severity="absolute")
        assert result is True

    def test_timeout_warn_fails_open(self):
        activation = default_activation()
        with patch.object(
            concurrent.futures.Future, "result", side_effect=concurrent.futures.TimeoutError
        ):
            result = self.engine._evaluate("true", activation, severity="warn")
        assert result is False

    def test_error_block_fails_closed(self):
        activation = default_activation()
        with patch.object(
            concurrent.futures.Future, "result", side_effect=RuntimeError("boom")
        ):
            result = self.engine._evaluate("true", activation, severity="block")
        assert result is True

    def test_error_warn_fails_open(self):
        activation = default_activation()
        with patch.object(
            concurrent.futures.Future, "result", side_effect=RuntimeError("boom")
        ):
            result = self.engine._evaluate("true", activation, severity="warn")
        assert result is False


# ---------------------------------------------------------------------------
# GuardrailEngine._get_expression
# ---------------------------------------------------------------------------


class TestGetExpression:
    def setup_method(self):
        self.engine = GuardrailEngine()

    def test_string_condition_passthrough(self):
        expr = self.engine._get_expression("decision.stakes == 'high'")
        assert expr == "decision.stakes == 'high'"

    def test_dict_cel_key(self):
        expr = self.engine._get_expression({"cel": "decision.confidence < 0.5"})
        assert expr == "decision.confidence < 0.5"

    def test_dict_cel_non_string_returns_none(self):
        expr = self.engine._get_expression({"cel": 123})
        assert expr is None

    def test_legacy_jsonb_converted(self):
        expr = self.engine._get_expression({"stakes": "high"})
        assert "decision.stakes == 'high'" in expr

    def test_invalid_type_returns_none(self):
        expr = self.engine._get_expression(42)  # type: ignore
        assert expr is None

    def test_empty_dict_returns_false(self):
        expr = self.engine._get_expression({})
        assert expr == "false"


# ---------------------------------------------------------------------------
# GuardrailEngine._jsonb_to_cel
# ---------------------------------------------------------------------------


class TestJsonbToCel:
    def setup_method(self):
        self.engine = GuardrailEngine()

    def test_stakes_key(self):
        assert self.engine._jsonb_to_cel({"stakes": "high"}) == "decision.stakes == 'high'"

    def test_confidence_lt_key(self):
        assert self.engine._jsonb_to_cel({"confidence_lt": 0.5}) == "decision.confidence < 0.5"

    def test_reason_count_lt_key(self):
        assert self.engine._jsonb_to_cel({"reason_count_lt": 3}) == "decision.reason_count < 3"

    def test_quality_lt_key(self):
        assert self.engine._jsonb_to_cel({"quality_lt": 0.6}) == "decision.quality_score < 0.6"

    def test_unknown_key_skipped(self):
        expr = self.engine._jsonb_to_cel({"mystery": "val", "stakes": "low"})
        assert "mystery" not in expr
        assert "decision.stakes == 'low'" in expr

    def test_empty_dict_returns_false(self):
        assert self.engine._jsonb_to_cel({}) == "false"

    def test_multiple_conditions_joined_with_and(self):
        expr = self.engine._jsonb_to_cel({"stakes": "high", "confidence_lt": 0.3})
        assert "&&" in expr
        assert "decision.stakes == 'high'" in expr
        assert "decision.confidence < 0.3" in expr

    def test_only_unknown_keys_returns_false(self):
        assert self.engine._jsonb_to_cel({"nope": "nope"}) == "false"


# ---------------------------------------------------------------------------
# GuardrailEngine.check (async, mocked session)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestCheck:
    async def test_no_guardrails_returns_allowed(self):
        engine = GuardrailEngine()
        session = make_session([])
        result = await engine.check(session, "agent-1", "do something", "low", 0.9)
        assert result.allowed is True
        assert result.blocked_by == []
        assert result.warnings == []

    async def test_returns_guardrail_result_type(self):
        engine = GuardrailEngine()
        session = make_session([])
        result = await engine.check(session, "agent-1", "desc", "low", 0.9)
        assert isinstance(result, GuardrailResult)

    async def test_block_guardrail_matches(self):
        engine = GuardrailEngine()
        g = make_guardrail(
            name="no-high-stakes",
            condition="decision.stakes == 'high'",
            severity="block",
        )
        session = make_session([g])
        result = await engine.check(session, "agent-1", "do it", "high", 0.9)
        assert result.allowed is False
        assert "no-high-stakes" in result.blocked_by

    async def test_absolute_guardrail_matches(self):
        engine = GuardrailEngine()
        g = make_guardrail(
            name="absolute-guard",
            condition="decision.stakes == 'critical'",
            severity="absolute",
        )
        session = make_session([g])
        result = await engine.check(session, "agent-1", "do it", "critical", 0.9)
        assert result.allowed is False
        assert "absolute-guard" in result.blocked_by

    async def test_warn_guardrail_allows_but_warns(self):
        engine = GuardrailEngine()
        g = make_guardrail(
            name="low-confidence-warn",
            condition="decision.confidence < 0.4",
            severity="warn",
        )
        session = make_session([g])
        result = await engine.check(session, "agent-1", "do it", "low", 0.2)
        assert result.allowed is True
        assert "low-confidence-warn" in result.warnings

    async def test_guardrail_no_match_stays_allowed(self):
        engine = GuardrailEngine()
        g = make_guardrail(
            name="high-stakes",
            condition="decision.stakes == 'high'",
            severity="block",
        )
        session = make_session([g])
        result = await engine.check(session, "agent-1", "safe action", "low", 0.9)
        assert result.allowed is True
        assert result.blocked_by == []

    async def test_multiple_guardrails_block_and_warn(self):
        engine = GuardrailEngine()
        g1 = make_guardrail(name="warn-low-conf", condition="decision.confidence < 0.9", severity="warn")
        g2 = make_guardrail(name="block-high", condition="decision.stakes == 'high'", severity="block")
        session = make_session([g1, g2])
        result = await engine.check(session, "agent-1", "do it", "high", 0.5)
        assert result.allowed is False
        assert "block-high" in result.blocked_by
        assert "warn-low-conf" in result.warnings

    async def test_multiple_blocks(self):
        engine = GuardrailEngine()
        g1 = make_guardrail(name="block-1", condition="decision.stakes == 'high'", severity="block")
        g2 = make_guardrail(name="block-2", condition="decision.confidence < 0.9", severity="block")
        session = make_session([g1, g2])
        result = await engine.check(session, "agent-1", "do it", "high", 0.1)
        assert result.allowed is False
        assert len(result.blocked_by) == 2

    async def test_activation_count_update_called_on_match(self):
        engine = GuardrailEngine()
        g = make_guardrail(name="blocker", condition="decision.stakes == 'high'", severity="block")
        session = make_session([g])
        await engine.check(session, "agent-1", "do it", "high", 0.9)
        # SELECT + UPDATE — at least 2 execute calls
        assert session.execute.call_count >= 2

    async def test_event_logged_on_block(self):
        engine = GuardrailEngine()
        g = make_guardrail(name="blocker", condition="decision.stakes == 'high'", severity="block")
        session = make_session([g])
        await engine.check(session, "agent-1", "do it", "high", 0.9)
        session.add.assert_called_once()

    async def test_event_logged_on_warn(self):
        engine = GuardrailEngine()
        g = make_guardrail(name="warner", condition="decision.confidence < 0.5", severity="warn")
        session = make_session([g])
        await engine.check(session, "agent-1", "do it", "low", 0.1)
        session.add.assert_called_once()

    async def test_no_event_when_no_match(self):
        engine = GuardrailEngine()
        g = make_guardrail(name="guard", condition="decision.stakes == 'high'", severity="block")
        session = make_session([g])
        await engine.check(session, "agent-1", "safe action", "low", 0.9)
        session.add.assert_not_called()

    async def test_context_accessible_in_cel(self):
        engine = GuardrailEngine()
        g = make_guardrail(
            name="ctx-guard",
            condition={"cel": "decision.context.risky == true"},
            severity="block",
        )
        session = make_session([g])
        result = await engine.check(
            session, "agent-1", "do it", "low", 0.9, context={"risky": True}
        )
        assert result.allowed is False

    async def test_legacy_jsonb_condition_in_check(self):
        engine = GuardrailEngine()
        g = make_guardrail(
            name="legacy-guard",
            condition={"stakes": "high", "confidence_lt": 0.5},
            severity="block",
        )
        session = make_session([g])
        result = await engine.check(session, "agent-1", "do it", "high", 0.3)
        assert result.allowed is False

    async def test_tags_accessible_in_cel(self):
        engine = GuardrailEngine()
        g = make_guardrail(
            name="require-tags",
            condition={"cel": "size(decision.tags) == 0"},
            severity="warn",
        )
        session = make_session([g])
        result_no_tags = await engine.check(session, "agent-1", "do it", "low", 0.9, tags=[])
        assert "require-tags" in result_no_tags.warnings

        result_with_tags = await engine.check(session, "agent-1", "do it", "low", 0.9, tags=["x"])
        assert "require-tags" not in result_with_tags.warnings

    async def test_has_pattern_accessible_in_cel(self):
        engine = GuardrailEngine()
        g = make_guardrail(
            name="require-pattern",
            condition={"cel": "!decision.has_pattern"},
            severity="warn",
        )
        session = make_session([g])
        result = await engine.check(session, "agent-1", "do it", "low", 0.9, pattern=None)
        assert "require-pattern" in result.warnings

        result_ok = await engine.check(session, "agent-1", "do it", "low", 0.9, pattern="some-pat")
        assert "require-pattern" not in result_ok.warnings
