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


class TestSalvageIsReportedToTheModel:
    @pytest.mark.asyncio
    async def test_repaired_call_tells_the_model_it_was_repaired(self):
        """A salvaged call succeeds -- but a silent success teaches the model
        nothing, so it emits the same broken shape next turn. The result must
        say which key was recovered and what the correct form is."""
        dispatcher, received = _make_decision_dispatcher()
        result_text, is_error = await dispatcher.dispatch(
            "record_decision",
            {
                "description": _OBSERVED_DESCRIPTION + _OBSERVED_TAIL,
                "category": "architecture",
                "stakes": "high",
            },
        )
        assert is_error is False
        assert received["confidence"] == 0.55  # still repaired
        assert "[input repaired]" in result_text
        assert "confidence" in result_text
        assert "top-level JSON key" in result_text
        assert result_text.endswith("ok")  # handler's own output preserved

    @pytest.mark.asyncio
    async def test_clean_call_gets_no_notice(self):
        dispatcher, _ = _make_decision_dispatcher()
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


_TAGGED_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "description": {"type": "string"},
        "confidence": {"type": "number"},
        "tags": {"type": "array", "items": {"type": "string"}},
        "reasons": {"type": "array", "items": {"type": "object"}},
        "meta": {"type": "object"},
        "anything": {},
    },
    "required": ["description"],
}


def _make_tagged_dispatcher() -> tuple[ToolDispatcher, dict]:
    dispatcher = ToolDispatcher()
    received: dict = {}

    async def record_decision(
        description: str,
        confidence: float = 0.5,
        tags: list | None = None,
        reasons: list | None = None,
        meta: dict | None = None,
        anything=None,
    ) -> dict:
        received.update(
            description=description, confidence=confidence, tags=tags,
            reasons=reasons, meta=meta, anything=anything,
        )
        return {"content": [{"type": "text", "text": "ok"}]}

    dispatcher.register("record_decision", record_decision, _TAGGED_SCHEMA)
    return dispatcher, received


class TestSchemaTypeValidation:
    """Observed 2026-08-22: the model sent tags as one comma-joined string,
    which reached the model back as a raw pydantic ValidationError with a
    docs URL. A wrong-shaped argument should be named, explained, and the
    correct form shown -- not coerced behind the model's back."""

    @pytest.mark.asyncio
    async def test_delimited_string_for_array_is_rejected_with_a_usable_message(self):
        dispatcher, received = _make_tagged_dispatcher()
        result_text, is_error = await dispatcher.dispatch(
            "record_decision",
            {
                "description": "d",
                "tags": "fannie-mae, cpm, condo, selling-guide",
            },
        )
        assert is_error is True
        assert "tags" in result_text
        assert "must be array" in result_text
        assert "got string" in result_text
        assert '["a", "b"]' in result_text  # shows the correct shape
        assert not received  # handler never ran

    @pytest.mark.asyncio
    async def test_scalar_for_scalar_is_left_alone(self):
        """pydantic's lax mode turns "0.9" into 0.9. Rejecting it here would
        fail calls that succeed today -- the check must stay fail-open."""
        dispatcher, received = _make_tagged_dispatcher()
        _, is_error = await dispatcher.dispatch(
            "record_decision", {"description": "d", "confidence": "0.9"},
        )
        assert is_error is False
        assert received["confidence"] == "0.9"  # passed through untouched

    @pytest.mark.asyncio
    async def test_container_where_scalar_belongs_is_rejected(self):
        dispatcher, received = _make_tagged_dispatcher()
        result_text, is_error = await dispatcher.dispatch(
            "record_decision", {"description": ["a", "b"]},
        )
        assert is_error is True
        assert "description must be string, got array" in result_text
        assert not received

    @pytest.mark.asyncio
    async def test_string_for_object_is_rejected_without_the_array_hint(self):
        dispatcher, _ = _make_tagged_dispatcher()
        result_text, is_error = await dispatcher.dispatch(
            "record_decision", {"description": "d", "meta": "a=1"},
        )
        assert is_error is True
        assert "meta must be object, got string" in result_text
        assert "JSON array" not in result_text

    @pytest.mark.asyncio
    async def test_untyped_property_is_never_rejected(self):
        """No declared type means no opinion -- fail open."""
        dispatcher, received = _make_tagged_dispatcher()
        _, is_error = await dispatcher.dispatch(
            "record_decision", {"description": "d", "anything": "whatever"},
        )
        assert is_error is False
        assert received["anything"] == "whatever"

    @pytest.mark.asyncio
    async def test_null_and_unknown_keys_are_skipped(self):
        dispatcher, received = _make_tagged_dispatcher()
        _, is_error = await dispatcher.dispatch(
            "record_decision", {"description": "d", "tags": None},
        )
        assert is_error is False
        assert received["tags"] is None

    @pytest.mark.asyncio
    async def test_well_formed_containers_pass(self):
        dispatcher, received = _make_tagged_dispatcher()
        _, is_error = await dispatcher.dispatch(
            "record_decision",
            {
                "description": "d",
                "tags": ["a", "b"],
                "reasons": [{"type": "analysis", "text": "t"}],
                "meta": {"k": "v"},
            },
        )
        assert is_error is False
        assert received["tags"] == ["a", "b"]

    @pytest.mark.asyncio
    async def test_missing_required_is_reported_before_type_errors(self):
        """A call that is both incomplete and mis-typed should surface the
        missing key first -- that is the one blocking dispatch outright."""
        dispatcher, _ = _make_tagged_dispatcher()
        result_text, is_error = await dispatcher.dispatch(
            "record_decision", {"tags": "a, b"},
        )
        assert is_error is True
        assert "missing required argument" in result_text


class TestVariadicHandlerValidation:
    """codex P2: a (**kwargs) handler reports zero named required params, so
    every schema-required key was excluded from hard_missing and validation
    silently no-opped. heartbeat_check_create / heartbeat_check_manage have
    exactly this shape and index kwargs["name"] / kwargs["action"] directly."""

    @staticmethod
    def _variadic_dispatcher() -> tuple[ToolDispatcher, dict]:
        dispatcher = ToolDispatcher()
        received: dict = {}

        async def heartbeat_check_create(**kwargs) -> dict:
            # Mirrors the real handler: direct indexing, KeyError if absent.
            received.update(name=kwargs["name"], prompt=kwargs["prompt"])
            return {"content": [{"type": "text", "text": "created"}]}

        dispatcher.register(
            "heartbeat_check_create",
            heartbeat_check_create,
            {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "prompt": {"type": "string"},
                },
                "required": ["name", "prompt"],
            },
        )
        return dispatcher, received

    @pytest.mark.asyncio
    async def test_missing_key_on_variadic_handler_is_caught(self):
        dispatcher, received = self._variadic_dispatcher()
        result_text, is_error = await dispatcher.dispatch(
            "heartbeat_check_create", {"name": "watch-ci"},
        )
        assert is_error is True
        assert "missing required argument" in result_text
        assert "prompt" in result_text
        assert not received  # KeyError never reached the handler

    @pytest.mark.asyncio
    async def test_complete_call_on_variadic_handler_still_works(self):
        dispatcher, received = self._variadic_dispatcher()
        result_text, is_error = await dispatcher.dispatch(
            "heartbeat_check_create", {"name": "watch-ci", "prompt": "p"},
        )
        assert is_error is False
        assert result_text == "created"
        assert received["name"] == "watch-ci"


class TestSalvageRespectsSchemaConstraints:
    """codex P2: a leaked confidence=2.0 coerces to a float and passes the type
    gate, then fails RecordInput's le=1.0 inside the handler -- nothing stored,
    but the call reported success and got an [input repaired] prefix."""

    @pytest.mark.asyncio
    async def test_out_of_range_number_is_not_treated_as_salvaged(self):
        dispatcher, received = _make_decision_dispatcher()
        result_text, is_error = await dispatcher.dispatch(
            "record_decision",
            {
                "description": (
                    _OBSERVED_DESCRIPTION
                    + '</description>\n<parameter name="confidence">2.0'
                ),
                "category": "architecture",
                "stakes": "high",
            },
        )
        assert is_error is True
        assert "missing required argument" in result_text
        assert "confidence" in result_text
        assert "[input repaired]" not in result_text
        assert not received

    @pytest.mark.asyncio
    async def test_in_range_number_still_salvages(self):
        dispatcher, received = _make_decision_dispatcher()
        _, is_error = await dispatcher.dispatch(
            "record_decision",
            {
                "description": _OBSERVED_DESCRIPTION + _OBSERVED_TAIL,
                "category": "architecture",
                "stakes": "high",
            },
        )
        assert is_error is False
        assert received["confidence"] == 0.55

    @pytest.mark.asyncio
    async def test_value_outside_enum_is_not_salvaged(self):
        dispatcher = ToolDispatcher()
        received: dict = {}

        async def tool(description: str, stakes: str) -> dict:
            received.update(description=description, stakes=stakes)
            return {"content": [{"type": "text", "text": "ok"}]}

        dispatcher.register("tool", tool, {
            "type": "object",
            "properties": {
                "description": {"type": "string"},
                "stakes": {"type": "string", "enum": ["low", "medium", "high"]},
            },
            "required": ["description", "stakes"],
        })
        result_text, is_error = await dispatcher.dispatch(
            "tool", {"description": 'd<parameter name="stakes">catastrophic'},
        )
        assert is_error is True
        assert "stakes" in result_text
        assert not received


class TestSalvageRequiresEvidenceOfASyntaxTransition:
    """codex P2: a trailing XML *example* is not a leak. A description that
    legitimately ends by quoting the format -- e.g. a decision written ABOUT
    this bug -- would otherwise be truncated and have a value invented from
    the quotation. Ambiguous evidence must fall through to the error."""

    @pytest.mark.asyncio
    async def test_well_formed_trailing_example_is_not_salvaged(self):
        dispatcher, received = _make_decision_dispatcher()
        description = (
            "The model leaked tool syntax into the JSON string, like "
            '<parameter name="confidence">0.9</parameter>'
        )
        result_text, is_error = await dispatcher.dispatch(
            "record_decision",
            {"description": description, "category": "process", "stakes": "low"},
        )
        assert is_error is True
        assert "missing required argument" in result_text
        assert not received  # nothing invented, nothing truncated

    @pytest.mark.asyncio
    async def test_unterminated_trailing_tag_is_still_salvaged(self):
        """The real prod shape: the model stopped mid-emission, so the final
        tag is never closed. That is the transition evidence."""
        dispatcher, received = _make_decision_dispatcher()
        _, is_error = await dispatcher.dispatch(
            "record_decision",
            {
                "description": _OBSERVED_DESCRIPTION + _OBSERVED_TAIL,
                "category": "architecture",
                "stakes": "high",
            },
        )
        assert is_error is False
        assert received["confidence"] == 0.55
        assert received["description"] == _OBSERVED_DESCRIPTION

    @pytest.mark.asyncio
    async def test_nan_is_not_accepted_as_a_salvaged_number(self):
        """Every comparison against NaN is False, so a bare min/max check
        would wave it through as in-range."""
        dispatcher, received = _make_decision_dispatcher()
        result_text, is_error = await dispatcher.dispatch(
            "record_decision",
            {
                "description": (
                    _OBSERVED_DESCRIPTION
                    + '</description>\n<parameter name="confidence">NaN'
                ),
                "category": "architecture",
                "stakes": "high",
            },
        )
        assert is_error is True
        assert "confidence" in result_text
        assert "[input repaired]" not in result_text
        assert not received

    @pytest.mark.asyncio
    async def test_infinity_is_not_accepted_either(self):
        dispatcher, received = _make_decision_dispatcher()
        _, is_error = await dispatcher.dispatch(
            "record_decision",
            {
                "description": (
                    _OBSERVED_DESCRIPTION
                    + '</description>\n<parameter name="confidence">Infinity'
                ),
                "category": "architecture",
                "stakes": "high",
            },
        )
        assert is_error is True
        assert not received


class TestSettingsFlag:
    def test_salvage_flag_default_on(self):
        from nous.config import Settings

        assert Settings().tool_arg_salvage_enabled is True
