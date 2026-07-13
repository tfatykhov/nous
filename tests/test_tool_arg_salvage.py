"""Tests for ToolDispatcher required-arg validation + XML <parameter> leak salvage.

Root cause (2026-07-13, prod): the model can leak Claude-internal XML tool
syntax inside a JSON string value — the record_decision `description` string
ended with '</description>\n<parameter name="confidence">0.55' and the model
then resumed valid JSON for the remaining keys. The parsed input therefore
lacked the top-level `confidence` key and handler(**args) raised an opaque
TypeError that leaked closure internals back to the model, which doom-looped.

Two dispatch-layer defenses under test:
1. Required-key validation: actionable tool error (missing + provided keys)
   instead of a raw TypeError — only when the handler signature would
   actually fail, preserving lenient handlers with defaults.
2. Trailing XML <parameter> leak salvage (NOUS_TOOL_ARG_SALVAGE_ENABLED):
   recover leaked values from string args, type-coerce per schema, strip the
   leaked tail from the host string.
"""

import pytest

from nous.api.tools import ToolDispatcher

_DECISION_SCHEMA: dict = {
    "type": "object",
    "description": "Test decision tool",
    "properties": {
        "description": {"type": "string"},
        "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
        "category": {"type": "string"},
        "stakes": {"type": "string"},
    },
    "required": ["description", "confidence", "category", "stakes"],
}

_OBSERVED_TAIL = '</description>\n<parameter name="confidence">0.55'
_OBSERVED_DESCRIPTION = (
    "Supersede F041's frozen .h5-file falsification gate: nest-gpu is now the "
    "SNN backend of record. No nest-gpu bridge yet exists in the Nous codebase."
)


def _make_decision_dispatcher(**dispatcher_kwargs) -> tuple[ToolDispatcher, dict]:
    """Dispatcher with a strict-signature tool mirroring record_decision."""
    dispatcher = ToolDispatcher(**dispatcher_kwargs)
    received: dict = {}

    async def record_decision(
        description: str,
        confidence: float,
        category: str,
        stakes: str,
    ) -> dict:
        received.update(
            description=description,
            confidence=confidence,
            category=category,
            stakes=stakes,
        )
        return {"content": [{"type": "text", "text": "ok"}]}

    dispatcher.register("record_decision", record_decision, _DECISION_SCHEMA)
    return dispatcher, received


class TestRequiredArgValidation:
    @pytest.mark.asyncio
    async def test_normal_call_unaffected(self):
        dispatcher, received = _make_decision_dispatcher()
        result_text, is_error = await dispatcher.dispatch(
            "record_decision",
            {
                "description": "d",
                "confidence": 0.8,
                "category": "process",
                "stakes": "low",
            },
        )
        assert is_error is False
        assert result_text == "ok"
        assert received["confidence"] == 0.8

    @pytest.mark.asyncio
    async def test_missing_required_returns_actionable_error(self):
        """No leak present -> clean error naming missing + provided keys."""
        dispatcher, received = _make_decision_dispatcher()
        result_text, is_error = await dispatcher.dispatch(
            "record_decision",
            {"description": "d", "category": "process", "stakes": "low"},
        )
        assert is_error is True
        assert "record_decision" in result_text
        assert "confidence" in result_text
        # Provided keys are echoed back so the model can see what landed
        assert "category" in result_text
        # No raw TypeError / closure internals leak
        assert "create_nous_tools" not in result_text
        assert "positional" not in result_text
        assert not received

    @pytest.mark.asyncio
    async def test_handler_default_leniency_preserved(self):
        """Schema-required key with a handler default still succeeds —
        validation only fires when handler(**args) would actually raise."""
        dispatcher = ToolDispatcher()
        received: dict = {}

        async def lenient(content: str, confidence: float = 1.0) -> dict:
            received.update(content=content, confidence=confidence)
            return {"content": [{"type": "text", "text": "stored"}]}

        schema = {
            "type": "object",
            "properties": {
                "content": {"type": "string"},
                "confidence": {"type": "number"},
            },
            "required": ["content", "confidence"],
        }
        dispatcher.register("lenient", lenient, schema)

        result_text, is_error = await dispatcher.dispatch(
            "lenient", {"content": "some fact"}
        )
        assert is_error is False
        assert received["confidence"] == 1.0


class TestXmlParamLeakSalvage:
    @pytest.mark.asyncio
    async def test_observed_leak_salvaged(self):
        """The exact prod shape: confidence leaked as a trailing XML tag
        inside the description string."""
        dispatcher, received = _make_decision_dispatcher()
        result_text, is_error = await dispatcher.dispatch(
            "record_decision",
            {
                "description": _OBSERVED_DESCRIPTION + _OBSERVED_TAIL,
                "category": "architecture",
                "stakes": "high",
            },
        )
        assert is_error is False, result_text
        assert received["confidence"] == 0.55
        # Host string cleaned: leaked run stripped, real content intact
        assert received["description"] == _OBSERVED_DESCRIPTION
        assert "<parameter" not in received["description"]
        assert "</description>" not in received["description"]

    @pytest.mark.asyncio
    async def test_multi_param_trailing_run_salvaged(self):
        dispatcher, received = _make_decision_dispatcher()
        tail = (
            '</description>\n<parameter name="confidence">0.85</parameter>\n'
            '<parameter name="category">process'
        )
        result_text, is_error = await dispatcher.dispatch(
            "record_decision",
            {"description": "Chose X over Y." + tail, "stakes": "medium"},
        )
        assert is_error is False, result_text
        assert received["confidence"] == 0.85
        assert received["category"] == "process"
        assert received["description"] == "Chose X over Y."

    @pytest.mark.asyncio
    async def test_type_coercion_integer_and_boolean(self):
        dispatcher = ToolDispatcher()
        received: dict = {}

        async def typed(note: str, count: int, enabled: bool) -> dict:
            received.update(note=note, count=count, enabled=enabled)
            return {"content": [{"type": "text", "text": "ok"}]}

        schema = {
            "type": "object",
            "properties": {
                "note": {"type": "string"},
                "count": {"type": "integer"},
                "enabled": {"type": "boolean"},
            },
            "required": ["note", "count", "enabled"],
        }
        dispatcher.register("typed", typed, schema)

        result_text, is_error = await dispatcher.dispatch(
            "typed",
            {
                "note": 'n</note>\n<parameter name="count">3</parameter>\n'
                '<parameter name="enabled">true'
            },
        )
        assert is_error is False, result_text
        assert received["count"] == 3
        assert received["enabled"] is True
        assert received["note"] == "n"

    @pytest.mark.asyncio
    async def test_uncoercible_value_falls_through_to_error(self):
        dispatcher, received = _make_decision_dispatcher()
        result_text, is_error = await dispatcher.dispatch(
            "record_decision",
            {
                "description": 'd</description>\n<parameter name="confidence">high',
                "category": "process",
                "stakes": "low",
            },
        )
        assert is_error is True
        assert "confidence" in result_text
        assert not received

    @pytest.mark.asyncio
    async def test_mid_string_leak_not_salvaged(self):
        """Leak followed by more prose is not a trailing run — leave the
        string untouched rather than risk corrupting legitimate content."""
        dispatcher, received = _make_decision_dispatcher()
        result_text, is_error = await dispatcher.dispatch(
            "record_decision",
            {
                "description": (
                    'quoting docs: <parameter name="confidence">0.9</parameter> '
                    "is the old syntax, do not use it"
                ),
                "category": "process",
                "stakes": "low",
            },
        )
        assert is_error is True
        assert "confidence" in result_text
        assert not received

    @pytest.mark.asyncio
    async def test_flag_off_no_salvage(self):
        dispatcher, received = _make_decision_dispatcher(arg_salvage_enabled=False)
        result_text, is_error = await dispatcher.dispatch(
            "record_decision",
            {
                "description": _OBSERVED_DESCRIPTION + _OBSERVED_TAIL,
                "category": "architecture",
                "stakes": "high",
            },
        )
        assert is_error is True
        assert "confidence" in result_text
        assert not received


class TestSettingsFlag:
    def test_salvage_flag_default_on(self):
        from nous.config import Settings

        assert Settings().tool_arg_salvage_enabled is True
