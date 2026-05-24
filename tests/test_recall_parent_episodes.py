"""F067 Phase 2: tests for the parent-episode injection in _format_pipeline_text.

The formatter is byte-identical to legacy output when parent_episodes is
None/empty (backwards-compat invariant). When provided, it appends a
`=== Parent Episode Context ===` section.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass

from nous.api.tools import _format_pipeline_text


@dataclass
class _FakeStats:
    contradiction_edges: list = None

    def __post_init__(self):
        if self.contradiction_edges is None:
            self.contradiction_edges = []


@dataclass
class _FakeResult:
    id: uuid.UUID
    type: str
    description: str
    score: float
    source: str = "heart"
    edge_relation: str | None = None
    contradicts: list = None
    metadata: dict = None

    def __post_init__(self):
        if self.contradicts is None:
            self.contradicts = []
        if self.metadata is None:
            self.metadata = {}


def _make_result(type_="fact", desc="some content"):
    return _FakeResult(
        id=uuid.uuid4(), type=type_, description=desc, score=0.85,
    )


def test_no_parent_episodes_byte_identical_to_legacy():
    """When parent_episodes is None, output matches the pre-F067 format."""
    results = [_make_result("fact", "user prefers Memrise for language learning")]
    stats = _FakeStats()
    out_none = _format_pipeline_text(results, stats, ["all"], parent_episodes=None)
    out_default = _format_pipeline_text(results, stats, ["all"])
    assert out_none == out_default
    assert "Parent Episode Context" not in out_none


def test_empty_parent_episodes_byte_identical():
    """Empty list MUST also produce legacy output (not a header with no items)."""
    results = [_make_result("fact", "x")]
    stats = _FakeStats()
    out_empty = _format_pipeline_text(results, stats, ["all"], parent_episodes=[])
    out_default = _format_pipeline_text(results, stats, ["all"])
    assert out_empty == out_default
    assert "Parent Episode Context" not in out_empty


def test_parent_episodes_appended():
    results = [_make_result("fact", "a fact")]
    stats = _FakeStats()
    eps = [("episode-id-1", "Summary of the parent episode")]
    out = _format_pipeline_text(results, stats, ["all"], parent_episodes=eps)
    assert "=== Parent Episode Context ===" in out
    assert "Summary of the parent episode" in out
    # ID is truncated to 8 chars for display
    assert "(episode" in out


def test_parent_episodes_multiple():
    results = [_make_result("fact", "a fact")]
    stats = _FakeStats()
    eps = [
        ("abc12345-aaaa-bbbb", "First parent"),
        ("def67890-cccc-dddd", "Second parent"),
    ]
    out = _format_pipeline_text(results, stats, ["all"], parent_episodes=eps)
    assert "First parent" in out
    assert "Second parent" in out
    # Each entry on its own line under the section header
    section_text = out.split("=== Parent Episode Context ===")[1]
    assert section_text.count("- (") == 2


def test_parent_episodes_appear_after_main_content():
    results = [_make_result("fact", "main fact content xyz")]
    stats = _FakeStats()
    eps = [("episode-id-1", "parent summary text")]
    out = _format_pipeline_text(results, stats, ["all"], parent_episodes=eps)
    # main fact comes before parent section
    main_pos = out.index("main fact content xyz")
    parent_pos = out.index("Parent Episode Context")
    assert main_pos < parent_pos
