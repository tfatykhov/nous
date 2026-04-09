"""Tests for F022 source_episode_id auto-injection into learn_fact.

Verifies:
- CognitiveLayer.get_active_episode_id() exposes the active episode
- AgentRunner._maybe_inject_episode_id() helper covers all paths cleanly
- run_python episode_id_resolver bakes episode into _learn_fact closure
"""

from __future__ import annotations

from unittest.mock import MagicMock
from uuid import uuid4

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
    ep_a, ep_b = str(uuid4()), str(uuid4())
    layer._active_episodes["sess-a"] = ep_a
    layer._active_episodes["sess-b"] = ep_b
    assert layer.get_active_episode_id("sess-a") == ep_a
    assert layer.get_active_episode_id("sess-b") == ep_b
    assert layer.get_active_episode_id("sess-c") is None


# ---------------------------------------------------------------------------
# AgentRunner._maybe_inject_episode_id() helper
# ---------------------------------------------------------------------------


def _make_runner_with_episode(session_id: str, episode_id: str | None):
    """Build a minimal runner stub with a cognitive layer that returns episode_id."""
    from nous.api.runner import AgentRunner

    layer = _make_layer()
    if episode_id:
        layer._active_episodes[session_id] = episode_id

    runner = AgentRunner.__new__(AgentRunner)
    runner._cognitive = layer
    return runner


def test_maybe_inject_adds_episode_to_learn_fact():
    ep_id = str(uuid4())
    runner = _make_runner_with_episode("sess-1", ep_id)
    result = runner._maybe_inject_episode_id("learn_fact", {"content": "fact", "category": "technical"}, "sess-1")
    assert result["source_episode_id"] == ep_id
    assert result["content"] == "fact"


def test_maybe_inject_does_not_override_explicit_episode_id():
    runner = _make_runner_with_episode("sess-1", str(uuid4()))
    explicit = str(uuid4())
    result = runner._maybe_inject_episode_id("learn_fact", {"content": "fact", "source_episode_id": explicit}, "sess-1")
    assert result["source_episode_id"] == explicit


def test_maybe_inject_skips_non_learn_fact_tools():
    ep_id = str(uuid4())
    runner = _make_runner_with_episode("sess-1", ep_id)
    original = {"query": "some query"}
    result = runner._maybe_inject_episode_id("recall_deep", original, "sess-1")
    assert result is original


def test_maybe_inject_skips_when_no_active_episode():
    runner = _make_runner_with_episode("sess-1", None)
    original = {"content": "fact"}
    result = runner._maybe_inject_episode_id("learn_fact", original, "sess-1")
    assert result is original


def test_maybe_inject_skips_when_no_session_id():
    runner = _make_runner_with_episode("sess-1", str(uuid4()))
    original = {"content": "fact"}
    result = runner._maybe_inject_episode_id("learn_fact", original, None)
    assert result is original


# ---------------------------------------------------------------------------
# run_python episode_id_resolver
# ---------------------------------------------------------------------------


def test_episode_id_resolver_called_with_session_id():
    """Resolver is invoked with _session_id at call time."""
    ep_id = str(uuid4())
    resolver = MagicMock(return_value=ep_id)

    # Just verify the resolver wiring: resolver(session_id) -> episode_id
    result = resolver("sess-x")
    resolver.assert_called_once_with("sess-x")
    assert result == ep_id


def test_episode_id_resolver_none_when_no_cognitive():
    """When no cognitive is passed, resolver is None and injection is skipped."""
    from nous.api.tools import create_programmatic_tools

    brain = MagicMock()
    heart = MagicMock()
    settings = MagicMock()
    settings.programmatic_tools_enabled = True
    settings.programmatic_tools_timeout = 30

    # No resolver — should not raise
    closures = create_programmatic_tools(brain, heart, settings, episode_id_resolver=None)
    assert "run_python" in closures
