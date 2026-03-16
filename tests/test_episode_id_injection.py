"""Tests for F022 source_episode_id auto-injection into learn_fact.

Verifies that CognitiveLayer.get_active_episode_id() exposes the active
episode for a session, enabling the runner to inject it into learn_fact
calls without the model needing to know or pass the UUID explicitly.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest


# ---------------------------------------------------------------------------
# CognitiveLayer.get_active_episode_id()
# ---------------------------------------------------------------------------

def _make_layer():
    """Build a minimal CognitiveLayer with mocked dependencies."""
    from nous.cognitive.layer import CognitiveLayer

    layer = CognitiveLayer.__new__(CognitiveLayer)
    layer._active_episodes = {}
    return layer


def test_get_active_episode_id_returns_none_for_unknown_session():
    layer = _make_layer()
    assert layer.get_active_episode_id("unknown-session") is None


def test_get_active_episode_id_returns_episode_when_active():
    layer = _make_layer()
    ep_id = str(uuid4())
    layer._active_episodes["sess-1"] = ep_id
    assert layer.get_active_episode_id("sess-1") == ep_id


def test_get_active_episode_id_isolates_sessions():
    layer = _make_layer()
    ep_a = str(uuid4())
    ep_b = str(uuid4())
    layer._active_episodes["sess-a"] = ep_a
    layer._active_episodes["sess-b"] = ep_b
    assert layer.get_active_episode_id("sess-a") == ep_a
    assert layer.get_active_episode_id("sess-b") == ep_b
    assert layer.get_active_episode_id("sess-c") is None


# ---------------------------------------------------------------------------
# Injection logic (unit-level, no runner instantiation needed)
# ---------------------------------------------------------------------------

def _apply_injection(tool_name: str, tool_input: dict, session_id: str, layer) -> dict:
    """Replicate the injection logic from runner.py for testing."""
    if tool_name == "learn_fact" and "source_episode_id" not in tool_input:
        active_ep = layer.get_active_episode_id(session_id) if session_id else None
        if active_ep:
            return {**tool_input, "source_episode_id": active_ep}
    return tool_input


def test_injection_adds_episode_id_to_learn_fact():
    layer = _make_layer()
    ep_id = str(uuid4())
    layer._active_episodes["sess-1"] = ep_id

    result = _apply_injection("learn_fact", {"content": "fact text", "category": "technical"}, "sess-1", layer)
    assert result["source_episode_id"] == ep_id
    assert result["content"] == "fact text"


def test_injection_does_not_override_explicit_episode_id():
    layer = _make_layer()
    layer._active_episodes["sess-1"] = str(uuid4())
    explicit_id = str(uuid4())

    result = _apply_injection("learn_fact", {"content": "fact", "source_episode_id": explicit_id}, "sess-1", layer)
    assert result["source_episode_id"] == explicit_id  # not overwritten


def test_injection_skips_non_learn_fact_tools():
    layer = _make_layer()
    ep_id = str(uuid4())
    layer._active_episodes["sess-1"] = ep_id

    original = {"query": "some query"}
    result = _apply_injection("recall_deep", original, "sess-1", layer)
    assert result is original  # unchanged


def test_injection_skips_when_no_active_episode():
    layer = _make_layer()  # no active episodes

    original = {"content": "fact", "category": "technical"}
    result = _apply_injection("learn_fact", original, "sess-1", layer)
    assert "source_episode_id" not in result


def test_injection_skips_when_no_session_id():
    layer = _make_layer()
    layer._active_episodes["sess-1"] = str(uuid4())

    original = {"content": "fact"}
    result = _apply_injection("learn_fact", original, None, layer)
    assert "source_episode_id" not in result
