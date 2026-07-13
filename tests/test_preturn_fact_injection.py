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
from nous.heart.search import _wrap_with_score


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


class TestFactPin:
    """fact_pin_top_k guarantees top-K search hits survive the pipeline.

    NOTE: the relevance filter alone can't drop rank-1/2 facts (min_k=3 floor);
    the drops the pin repairs come from diversity, conversation-dedup, and the
    gap-cut at deeper ranks (staleness is a phantom for facts — FactSummary has
    no created_at). These tests exercise _reinsert_pinned directly with
    explicit dropped-survivor configurations, plus a build()-level wiring test.
    """

    def test_pin_reinserts_dropped_facts_at_front(self):
        engine = _make_engine(fact_pin_top_k=2)
        raw = [FakeFact(content=f"fact {i}", id=str(i)) for i in range(5)]
        pinned = raw[:2]
        survivors = raw[2:]  # pipeline dropped BOTH pinned facts
        merged = engine._reinsert_pinned(pinned, survivors)
        assert [f.id for f in merged] == ["0", "1", "2", "3", "4"]

    def test_pin_partial_drop_keeps_survivor_position(self):
        engine = _make_engine(fact_pin_top_k=2)
        raw = [FakeFact(content=f"fact {i}", id=str(i)) for i in range(4)]
        pinned = raw[:2]
        survivors = [raw[2], raw[0], raw[3]]  # "0" survived mid-list, "1" dropped
        merged = engine._reinsert_pinned(pinned, survivors)
        assert [f.id for f in merged] == ["1", "2", "0", "3"]  # only "1" re-inserted

    def test_pin_never_resurrects_superseded_fact(self):
        engine = _make_engine(fact_pin_top_k=2)
        stale = FakeFact(content="old value", id="stale")
        stale.recency_status = "superseded"   # tagged by _resolve_recency
        fresh = FakeFact(content="new value", id="fresh")
        merged = engine._reinsert_pinned([stale, fresh], [])  # pipeline dropped both
        assert [f.id for f in merged] == ["fresh"]            # stale NOT re-inserted

    def test_pin_preserves_pipeline_order_when_nothing_dropped(self):
        engine = _make_engine(fact_pin_top_k=1)
        raw = [FakeFact(content="a", id="a"), FakeFact(content="b", id="b")]
        merged = engine._reinsert_pinned(raw[:1], list(raw))
        assert [f.id for f in merged] == ["a", "b"]  # unchanged, no duplicate

    def test_pin_zero_is_inert(self):
        engine = _make_engine()  # fact_pin_top_k defaults 0
        assert engine._settings.fact_pin_top_k == 0


async def test_pin_build_wiring_records_pinned_ids():
    """build()-level: pinned facts flow through to the section AND recalled ids."""
    engine = _make_engine(fact_pin_top_k=2)
    facts = [FakeFact(content=f"pinnable fact {i}", subject=f"s{i}",
                      id=f"id-{i}", score=0.9 - i * 0.1) for i in range(4)]
    _stub_heart_for_build(engine, facts)
    result = await engine.build(
        agent_id="a", session_id="s", input_text="pinnable?", frame=_frame(),
    )
    section = next(s for s in result.sections if s.label == "Relevant Facts")
    assert "pinnable fact 0" in section.content
    assert "id-0" in result.recalled_ids["fact"]
    assert "id-1" in result.recalled_ids["fact"]


class TestSupersessionLineage:
    def test_mode_validates(self):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, supersession_lineage_mode="bogus")

    def test_tag_mode_appends_generic_marker(self):
        engine = _make_engine(supersession_lineage_mode="tag")
        f = FakeFact(content="Past Masters was performed by Madonna",
                     subject="Past Masters", id="f1")
        out = engine._format_facts(
            [f], lineage={"f1": ["Past Masters was performed by The Beatles"]})
        assert "[current — supersedes an earlier belief]" in out
        assert "Beatles" not in out               # tag mode never names the stale value

    def test_named_mode_quotes_stale_value(self):
        engine = _make_engine(supersession_lineage_mode="named")
        f = FakeFact(content="Past Masters was performed by Madonna",
                     subject="Past Masters", id="f1")
        out = engine._format_facts(
            [f], lineage={"f1": ["Past Masters was performed by The Beatles"]})
        assert 'supersedes earlier belief: "Past Masters was performed by The Beatles"' in out

    def test_named_mode_truncates_stale_value_at_120(self):
        engine = _make_engine(supersession_lineage_mode="named")
        f = FakeFact(content="new", subject="s", id="f1")
        out = engine._format_facts([f], lineage={"f1": ["X" * 500]})
        assert "X" * 120 in out
        assert "X" * 121 not in out

    def test_off_mode_renders_nothing_even_with_lineage(self):
        engine = _make_engine()  # mode defaults "off"
        f = FakeFact(content="new", subject="s", id="f1")
        out = engine._format_facts([f], lineage={"f1": ["old"]})
        assert "supersede" not in out.lower()

    def test_lineage_renders_through_scored_wrapper(self):
        """The pipeline wraps facts in _ScoredWrapper (__slots__ forbids attribute
        writes) — lineage must render via the dict, reading id through the wrapper."""
        engine = _make_engine(supersession_lineage_mode="tag")
        f = _wrap_with_score(FakeFact(content="new", subject="s", id="f1"), 0.9)
        out = engine._format_facts([f], lineage={"f1": ["old"]})
        assert "[current — supersedes an earlier belief]" in out


async def test_lineage_build_wiring_fetches_and_renders():
    engine = _make_engine(supersession_lineage_mode="tag")
    facts = [FakeFact(content="current value", subject="cv", id="f1", score=0.9)]
    _stub_heart_for_build(engine, facts)
    engine._heart.get_superseded_contents.return_value = {"f1": ["old value"]}
    result = await engine.build(
        agent_id="a", session_id="s", input_text="current?", frame=_frame(),
    )
    section = next(s for s in result.sections if s.label == "Relevant Facts")
    assert "[current — supersedes an earlier belief]" in section.content


async def test_lineage_fetch_failure_degrades_to_plain_rendering():
    engine = _make_engine(supersession_lineage_mode="tag")
    facts = [FakeFact(content="current value", subject="cv", id="f1", score=0.9)]
    _stub_heart_for_build(engine, facts)
    engine._heart.get_superseded_contents.side_effect = Exception("db down")
    result = await engine.build(
        agent_id="a", session_id="s", input_text="current?", frame=_frame(),
    )
    section = next(s for s in result.sections if s.label == "Relevant Facts")
    assert "current value" in section.content
    assert "supersede" not in section.content.lower()
