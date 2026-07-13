"""Tests for pre-turn fact-injection fixes (render depth, pin, lineage, backstop).

The build()-level golden below was captured on unmodified HEAD and guards the
Global Constraint that all new flags default to byte-identical output.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from nous.cognitive.context import ContextEngine
from nous.cognitive.schemas import FrameSelection
from nous.config import Settings


class FakeFact:
    def __init__(self, content="", subject=None, confidence=1.0, score=None,
                 id=None, superseded_by=None, category=None, source=None):
        self.content = content
        self.subject = subject
        self.confidence = confidence
        self.score = score
        self.id = id or ""
        self.superseded_by = superseded_by
        self.category = category
        self.source = source
        self.recency_status = None
        self.recency_date = None


def _make_engine(**settings_kwargs) -> ContextEngine:
    brain = AsyncMock()
    brain.embeddings = MagicMock()
    heart = AsyncMock()
    settings = Settings(_env_file=None, **settings_kwargs)
    return ContextEngine(brain, heart, settings, identity_prompt="")


def _frame(frame_id="question"):
    return FrameSelection(
        frame_id=frame_id, frame_name=frame_id.title(),
        description="test", confidence=0.9, match_method="pattern",
    )


def _stub_heart_for_build(engine, facts):
    heart = engine._heart
    heart.search_facts.return_value = facts
    heart.list_censors.return_value = []
    heart.list_facts_by_category.return_value = []
    heart.get_working_memory.return_value = None
    heart.search_episodes.return_value = []
    heart.list_episodes.return_value = []
    heart.list_procedures.return_value = ([], 0)
    engine._brain.query.return_value = []


GOLDEN_FACTS = [
    FakeFact(content="A" * 150 + " midpoint marker " + "B" * 150,
             subject="long-one", confidence=1.0, score=0.9, id="f1"),
    FakeFact(content="short fact two", subject="short-two", confidence=0.8,
             score=0.5, id="f2"),
    FakeFact(content="short fact three", subject=None, confidence=0.7,
             score=0.4, id="f3"),
]

# Captured on unmodified HEAD (see Step 2). Reconstructed via formula to avoid
# manual A-count errors; equivalent to the repr() output from the capture run.
GOLDEN_RELEVANT_FACTS = (
    "- [long-one] " + "A" * 150 + " midpoint marker... [confidence: 1.00]\n"
    "- [short-two] short fact two [confidence: 0.80]\n"
    "- short fact three [confidence: 0.70]"
)


async def test_build_relevant_facts_golden_default_settings():
    """Byte-identity oracle: default Settings must reproduce HEAD's exact
    Relevant Facts section content for a fixed fact set."""
    engine = _make_engine()
    _stub_heart_for_build(engine, list(GOLDEN_FACTS))
    result = await engine.build(
        agent_id="a", session_id="s",
        input_text="what do we know about the long one?", frame=_frame(),
    )
    section = next(s for s in result.sections if s.label == "Relevant Facts")
    assert section.content == GOLDEN_RELEVANT_FACTS


LONG = "A" * 150 + " midpoint marker " + "B" * 150  # 317 chars


class TestFactRenderDepth:
    def test_default_truncates_at_200_word_boundary(self):
        engine = _make_engine()
        out = engine._format_facts([FakeFact(content=LONG)])
        assert "..." in out
        assert "midpoint marker" in out          # 200 chars reaches past the As
        assert "B" * 100 not in out              # tail cut

    def test_max_chars_setting_raises_cap(self):
        engine = _make_engine(fact_format_max_chars=1000)
        out = engine._format_facts([FakeFact(content=LONG)])
        assert "B" * 150 in out                  # full content survives
        assert "..." not in out

    def test_full_top_n_renders_head_untruncated(self):
        engine = _make_engine()  # max_chars stays 200
        facts = [FakeFact(content=LONG, subject="first"),
                 FakeFact(content=LONG, subject="second")]
        out = engine._format_facts(facts, full_top_n=1)
        first_line, second_line = out.splitlines()
        assert "B" * 150 in first_line           # rank 1: full
        assert "..." in second_line              # rank 2: default cap

    def test_default_output_byte_identical_to_legacy(self):
        engine = _make_engine()
        f = FakeFact(content="short fact", subject="subj", confidence=0.93)
        assert engine._format_facts([f]) == "- [subj] short fact [confidence: 0.93]"
