"""Tests for cache-stabilizing tool superset (NOUS_STABLE_TOOL_SET_ENABLED).

When enabled (default), all non-initiation frames resolve to one byte-identical
tool superset so the Anthropic prompt-cache prefix (tools sit at its front) is
not busted when the cognitive frame changes between turns. The `initiation`
frame keeps its distinct minimal set, and initiation-only protocol tools never
leak into the conversational superset.
"""

from __future__ import annotations

import pytest

from nous.api.tools import ToolDispatcher, _INITIATION_ONLY_TOOLS


async def _dummy(**kwargs):
    return {"content": [{"type": "text", "text": "ok"}]}


def _register(dispatcher: ToolDispatcher, names: list[str]) -> None:
    for name in names:
        dispatcher.register(
            name, _dummy, {"description": f"{name} tool", "type": "object", "properties": {}}
        )


# A realistic-ish mix: normal tools + the two initiation-only protocol tools.
_NORMAL = ["record_decision", "recall_deep", "learn_fact", "bash", "write_file", "web_search"]
_INIT = ["store_identity", "complete_initiation"]

# FRAME_TOOLS stub mirroring the real shape: conversational frames are subsets,
# task is wildcard, initiation is the two protocol tools.
_FRAME_TOOLS = {
    "conversation": ["record_decision", "learn_fact", "recall_deep", "bash", "write_file", "web_search"],
    "question": ["recall_deep", "web_search"],
    "decision": ["record_decision", "recall_deep"],
    "creative": ["learn_fact", "recall_deep"],
    "task": ["*"],
    "debug": ["record_decision", "recall_deep", "bash"],
    "initiation": ["store_identity", "complete_initiation"],
}

_CONVERSATIONAL = ["conversation", "question", "decision", "creative", "task", "debug"]


@pytest.fixture
def dispatcher(monkeypatch):
    d = ToolDispatcher(stable_tool_set_enabled=True)
    _register(d, _NORMAL + _INIT)
    monkeypatch.setattr("nous.api.runner.FRAME_TOOLS", _FRAME_TOOLS)
    return d


def test_all_conversational_frames_return_identical_list(dispatcher):
    """Every non-initiation frame returns a byte-identical tool array, so the
    cached prefix is stable across frame changes."""
    lists = {f: dispatcher.available_tools(f) for f in _CONVERSATIONAL}
    baseline = lists["task"]
    for frame, tools in lists.items():
        assert tools == baseline, f"{frame} diverged from the stable superset"


def test_superset_excludes_initiation_only_tools(dispatcher):
    """store_identity / complete_initiation must NOT leak into the superset."""
    names = {t["name"] for t in dispatcher.available_tools("conversation")}
    assert names == set(_NORMAL)
    assert not (_INITIATION_ONLY_TOOLS & names)


def test_initiation_frame_keeps_distinct_minimal_set(dispatcher):
    """initiation is never collapsed — it returns exactly its protocol tools."""
    names = {t["name"] for t in dispatcher.available_tools("initiation")}
    assert names == set(_INIT)


def test_superset_is_cached_under_single_key(dispatcher):
    """All conversational frames share one cache entry (keyed 'task'), so the
    cache is not bypassed per-frame."""
    for f in _CONVERSATIONAL:
        dispatcher.available_tools(f)
    # Only the effective 'task' key (plus nothing per-frame) should be present.
    assert set(dispatcher._tool_schema_cache.keys()) == {"task"}


def test_deep_copy_isolation_holds_for_superset(dispatcher):
    """Mutating a returned superset must not corrupt the shared cache entry."""
    first = dispatcher.available_tools("question")
    first.append({"name": "injected"})
    first[0]["description"] = "CORRUPTED"
    second = dispatcher.available_tools("decision")
    assert all(t.get("name") != "injected" for t in second)
    assert all(t["description"] != "CORRUPTED" for t in second)


def test_flag_off_restores_per_frame_gating(monkeypatch):
    """With the flag disabled, frames filter to their FRAME_TOOLS subset."""
    d = ToolDispatcher(stable_tool_set_enabled=False)
    _register(d, _NORMAL + _INIT)
    monkeypatch.setattr("nous.api.runner.FRAME_TOOLS", _FRAME_TOOLS)

    q = {t["name"] for t in d.available_tools("question")}
    assert q == {"recall_deep", "web_search"}
    dec = {t["name"] for t in d.available_tools("decision")}
    assert dec == {"record_decision", "recall_deep"}
    # Distinct per-frame cache entries under legacy behavior.
    assert {"question", "decision"} <= set(d._tool_schema_cache.keys())
