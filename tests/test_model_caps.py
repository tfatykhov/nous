"""Model capabilities by version: legacy Haiku, Haiku 5+ thinking by default, and the default thinking field.

Measured over OAuth on 2026-10-16/18: Haiku 4.5 answers 400 to adaptive
thinking and to the effort parameter; Haiku 5.5 accepts both and thinks when a
request has no ``thinking`` field, and it accepts ``{"type": "disabled"}``.
Sonnet 5.5 and Opus 5.5 answer 400 to ``{"type": "disabled"}``.
"""

from __future__ import annotations

import pytest

from nous.api.model_caps import disables_thinking_by_default, is_legacy_haiku, with_default_thinking

_LEGACY = ["claude-haiku-4-5", "claude-haiku-4-5-20251001", "claude-3-5-haiku-20241022", "claude-3-haiku-20240307"]
_HAIKU_5 = ["claude-haiku-5-5", "claude-haiku-5-5-20261001", "CLAUDE-HAIKU-5-5", "claude-haiku-6-0"]
_NOT_HAIKU = ["claude-sonnet-5-5", "claude-opus-5-5", "claude-sonnet-4-6", "claude-opus-4-6", "fable-5-1", ""]


@pytest.mark.parametrize("model", _LEGACY)
def test_a_haiku_below_5_is_legacy_and_keeps_its_default(model):
    assert is_legacy_haiku(model) is True
    assert disables_thinking_by_default(model) is False


@pytest.mark.parametrize("model", _HAIKU_5)
def test_a_haiku_5_or_later_is_not_legacy_and_gets_thinking_disabled_by_default(model):
    assert is_legacy_haiku(model) is False
    assert disables_thinking_by_default(model) is True


@pytest.mark.parametrize("model", _NOT_HAIKU)
def test_other_families_are_neither(model):
    assert is_legacy_haiku(model) is False
    assert disables_thinking_by_default(model) is False


def test_a_haiku_naming_no_version_is_treated_as_legacy():
    """Nothing is added that the model might reject, and the runner sends it no thinking, as before Haiku 5."""
    assert is_legacy_haiku("claude-haiku-latest") is True
    assert disables_thinking_by_default("claude-haiku-latest") is False


def test_a_haiku_5_payload_without_thinking_gets_it_disabled_in_a_copy():
    payload = {"model": "claude-haiku-5-5", "max_tokens": 512, "messages": []}

    result = with_default_thinking(payload)

    assert result == {**payload, "thinking": {"type": "disabled"}}
    assert "thinking" not in payload, "the caller's payload was mutated"


@pytest.mark.parametrize(
    "thinking", [{"type": "adaptive"}, {"type": "enabled", "budget_tokens": 2048}, {"type": "disabled"}]
)
def test_an_explicit_thinking_is_never_overridden(thinking):
    payload = {"model": "claude-haiku-5-5", "max_tokens": 512, "thinking": thinking}

    assert with_default_thinking(payload) is payload


@pytest.mark.parametrize("model", [*_LEGACY, *_NOT_HAIKU, "claude-haiku-latest"])
def test_every_other_payload_is_returned_as_is(model):
    payload = {"model": model, "max_tokens": 512}

    assert with_default_thinking(payload) is payload


def test_a_payload_without_a_model_is_returned_as_is():
    payload = {"max_tokens": 512}

    assert with_default_thinking(payload) is payload
