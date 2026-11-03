"""Pre-turn context selection: ranking, pinning, abandoned episodes and trace labels.

- The relevance filter's max_k cap and gap cut assume score order, so it sorts
  first; a modifier that lowers a score in place (staleness, the recency
  resolver's demotion) must not get a fresher, higher-scored item cut.
- fact_pin_top_k is bounded below both the plan's fact limit and the number of
  facts returned, so pinning never re-inserts the whole result set and the
  diversity / dedup / relevance pipeline still decides at least one fact.
- A re-inserted pin carries the score a survivor would (after frame and usage
  boost), and finalizes as rendered with stage ``pinned``.
- Score modifiers are traced as mutations when they move a score by more than
  1%, and a gap cut names the modifier that last moved the dropped item.
- Abandoned episodes are never rendered, so they are filtered before they take
  a relevance slot, count as recalled, or show in Recent Conversations.
- Each leg records the query text it actually searched with.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import nous.cognitive.context as context_mod
from nous.cognitive.context import ContextEngine
from nous.cognitive.intent import RetrievalPlan, RetrievalQuery
from nous.cognitive.schemas import FrameSelection
from nous.config import Settings
from nous.observability.retrieval_logger import RetrievalLogger
from nous.observability.retrieval_trace import RetrievalTrace

NOW = datetime.now(UTC)


class FakeFact:
    def __init__(self, id, score, subject=None, content=None, event_date=None, encoded_frame=None):
        self.id = id
        self.score = score
        self.subject = subject
        self.content = content or f"fact {id}"
        self.confidence = 0.9
        self.category = None
        self.source = None
        self.superseded_by = None
        self.recency_status = None
        self.recency_date = None
        self.event_date = event_date
        self.encoded_frame = encoded_frame


class FakeDecision:
    def __init__(self, id, score, category, created_at):
        self.id = id
        self.score = score
        self.category = category
        self.created_at = created_at
        self.description = f"decision {id}"
        self.outcome = "success"
        self.confidence = 0.8


class FakeEpisode:
    def __init__(self, id, score, outcome=None, tags=None):
        self.id = id
        self.score = score
        self.outcome = outcome
        self.tags = tags or [f"tag-{id}"]
        self.summary = f"episode {id} summary"
        self.title = f"episode {id}"
        self.started_at = NOW - timedelta(hours=1)


class FakeUsageTracker:
    def __init__(self, factors):
        self._factors = factors

    def get_boost_factor(self, memory_id):
        return self._factors.get(memory_id, 1.0)


def _engine(**settings_kwargs) -> ContextEngine:
    brain = AsyncMock()
    brain.embeddings = MagicMock()
    heart = AsyncMock()
    heart.list_censors.return_value = []
    heart.list_facts_by_category.return_value = []
    heart.get_working_memory.return_value = None
    heart.search_facts.return_value = []
    heart.search_episodes.return_value = []
    heart.list_episodes.return_value = []
    heart.list_procedures.return_value = ([], 0)
    heart.search_procedures.return_value = []
    brain.query.return_value = []
    settings_kwargs.setdefault("proc_selection_graph_primary", False)
    settings_kwargs.setdefault("temporal_context_enabled", False)
    settings = Settings(_env_file=None, **settings_kwargs)
    return ContextEngine(brain, heart, settings, identity_prompt="")


def _frame(frame_id="question"):
    return FrameSelection(
        frame_id=frame_id,
        frame_name=frame_id.title(),
        description="test",
        confidence=0.9,
        match_method="pattern",
    )


def _plan(query_text="bag words", **limits):
    return RetrievalPlan(
        queries=[
            RetrievalQuery(memory_type=t, query_text=query_text, limit=limits.get(t, 5))
            for t in ("decision", "fact", "procedure", "episode")
        ]
    )


async def _build(engine, *, traced=True, plan=None, input_text="a sentence the user typed", usage_tracker=None):
    rl = RetrievalLogger(candidate_sample_rate=1.0) if traced else None
    with patch("nous.cognitive.context.get_active_retrieval_logger", return_value=rl):
        return await engine.build(
            agent_id="a",
            session_id="s",
            input_text=input_text,
            frame=_frame(),
            retrieval_plan=plan or _plan(),
            usage_tracker=usage_tracker,
        )


def _cands(result):
    return {c["id"]: c for c in result.retrieval_trace.to_dict()["candidates"]}


def _leg(result, name):
    return next(lg for lg in result.retrieval_trace.to_dict()["legs"] if lg["name"] == name)


# ---------------------------------------------------------------------------
# Score order before the gap cut
# ---------------------------------------------------------------------------


def _stale_decisions():
    # Search order (raw score desc). d-old is 90 days old, so the penalty
    # floors it at 0.3x (0.279) while it sits at index 2 — the first index the
    # gap cut inspects (decision min_k = 2).
    return [
        FakeDecision("d-a", 0.966, "architecture", NOW),
        FakeDecision("d-b", 0.950, "process", NOW),
        FakeDecision("d-old", 0.930, "tooling", NOW - timedelta(days=90)),
        FakeDecision("d-fresh", 0.927, "integration", NOW),
    ]


def test_staleness_penalty_returns_score_order():
    engine = _engine()
    out = engine._apply_staleness_penalty(_stale_decisions())
    assert [d.id for d in out] == ["d-a", "d-b", "d-fresh", "d-old"]


async def test_fresh_decision_behind_a_penalised_one_survives_the_gap_cut():
    engine = _engine()
    engine._brain.query.return_value = _stale_decisions()
    result = await _build(engine)
    assert "d-fresh" in result.recalled_ids["decision"]
    assert "d-old" not in result.recalled_ids["decision"]


def test_relevance_filter_sorts_before_cutting_a_recency_demoted_list():
    # Staleness off, so nothing re-sorts after the recency resolver demotes
    # "old" (0.89 -> 0.267) in place at index 3, ahead of the better "good".
    engine = _engine(staleness_penalty_enabled=False, recency_resolver_enabled=True)
    facts = [
        FakeFact("a", 0.95, subject="s0"),
        FakeFact("b", 0.92, subject="s1"),
        FakeFact("c", 0.90, subject="s2"),
        FakeFact("old", 0.89, subject="price", content="the price is 10 dollars", event_date=datetime(2025, 1, 1)),
        FakeFact("good", 0.85, subject="s3"),
        FakeFact("new", 0.50, subject="price", content="the price is 12 dollars", event_date=datetime(2026, 1, 1)),
    ]
    facts = engine._apply_staleness_penalty(engine._resolve_recency(facts))
    assert [f.id for f in facts][3] == "old"  # demoted in place, list unsorted
    kept = [f.id for f in engine._apply_relevance_filter(facts, "fact")]
    assert "good" in kept


# ---------------------------------------------------------------------------
# Pinning is bounded below the fact limit and the result count
# ---------------------------------------------------------------------------


def _same_subject_facts():
    # Diversity keeps 2 per subject, so f2 is dropped by the pipeline unless
    # the pin resurrects it.
    return [FakeFact(f"f{i}", 0.9 - i * 0.1, subject="same") for i in range(3)]


async def test_pin_k_at_or_above_the_fact_limit_leaves_one_fact_to_the_pipeline(monkeypatch):
    monkeypatch.setattr(context_mod, "_PIN_CLAMP_WARNED", False)
    engine = _engine(fact_pin_top_k=5)
    engine._heart.search_facts.return_value = _same_subject_facts()
    result = await _build(engine, plan=_plan(fact=3))
    assert result.recalled_ids["fact"] == ["f0", "f1"]
    cands = _cands(result)
    assert (cands["f2"]["disposition"], cands["f2"]["disposition_stage"]) == ("filter_dropped", "diversity")


async def test_pin_clamp_warns_once_per_process(monkeypatch, caplog):
    monkeypatch.setattr(context_mod, "_PIN_CLAMP_WARNED", False)
    engine = _engine(fact_pin_top_k=5)
    engine._heart.search_facts.return_value = _same_subject_facts()
    with caplog.at_level(logging.WARNING, logger="nous.cognitive.context"):
        await _build(engine, plan=_plan(fact=3))
        await _build(engine, plan=_plan(fact=3))
    clamp = [r for r in caplog.records if "fact_pin_top_k" in r.getMessage()]
    assert len(clamp) == 1


async def test_short_result_set_is_not_pinned_whole():
    # pin_k=4 is below the limit (5), but only 3 facts came back.
    engine = _engine(fact_pin_top_k=4)
    engine._heart.search_facts.return_value = _same_subject_facts()
    result = await _build(engine, plan=_plan(fact=5))
    assert result.recalled_ids["fact"] == ["f0", "f1"]


async def test_pinned_facts_are_traced_as_pinned():
    engine = _engine(fact_pin_top_k=2)
    engine._heart.search_facts.return_value = _same_subject_facts()
    result = await _build(engine, plan=_plan(fact=5))
    cands = _cands(result)
    for fid in ("f0", "f1"):
        assert (cands[fid]["disposition"], cands[fid]["disposition_stage"]) == ("rendered", "pinned")


# ---------------------------------------------------------------------------
# Pinned scores, modifier mutations and gap-cut labels
# ---------------------------------------------------------------------------


def _usage_cut_facts():
    # p0 is the top search hit, but usage boost (0.3x) sinks it to 0.27 at the
    # tail, where the gap cut (0.27 < 0.78 * 0.5) removes it.
    return [
        FakeFact("p0", 0.90, subject="s0"),
        FakeFact("p1", 0.88, subject="s1"),
        FakeFact("a", 0.80, subject="s2"),
        FakeFact("b", 0.79, subject="s3"),
        FakeFact("c", 0.78, subject="s4"),
    ]


async def test_reinserted_pinned_fact_carries_its_boosted_score():
    engine = _engine(fact_pin_top_k=1)
    engine._heart.search_facts.return_value = _usage_cut_facts()
    result = await _build(engine, usage_tracker=FakeUsageTracker({"p0": 0.3}))
    assert result.recalled_ids["fact"][0] == "p0"  # pin re-inserts at the front
    assert result.recalled_score_map["p0"] == pytest.approx(0.27)


async def test_pin_dropped_at_diversity_carries_the_score_a_survivor_would():
    # Frame boost (1.3x) lifts f1 and f2 above the pinned f0, so diversity (2
    # per subject) drops f0 before the usage boost runs. Re-inserted, f0 must
    # still carry its usage-boosted score (0.6 * 0.5), like the survivors.
    engine = _engine(fact_pin_top_k=1)
    engine._heart.search_facts.return_value = [
        FakeFact("f0", 0.60, subject="same"),
        FakeFact("f1", 0.58, subject="same", encoded_frame="question"),
        FakeFact("f2", 0.55, subject="same", encoded_frame="question"),
    ]
    result = await _build(engine, usage_tracker=FakeUsageTracker({"f0": 0.5}))
    assert _cands(result)["f0"]["restored_from"] == "filter_dropped@diversity"
    assert result.recalled_score_map["f0"] == pytest.approx(0.30)
    assert result.recalled_score_map["f1"] == pytest.approx(0.58 * 1.3)


async def test_gap_cut_names_the_usage_boost_that_sank_the_item():
    engine = _engine()
    engine._heart.search_facts.return_value = _usage_cut_facts()
    result = await _build(engine, usage_tracker=FakeUsageTracker({"p0": 0.3}))
    p0 = _cands(result)["p0"]
    assert p0["disposition"] == "below_floor"
    assert p0["disposition_stage"] == "relevance_filter:score_gap<0.5x|after:usage_boost"
    usage = [m for m in p0["mutations"] if m["stage"] == "usage_boost"]
    assert usage and usage[0]["score_before"] == pytest.approx(0.9)
    assert usage[0]["score_after"] == pytest.approx(0.27)


async def test_staleness_is_traced_as_a_mutation_and_named_in_the_gap_cut():
    engine = _engine()
    # d-old sinks to the tail but the gap cut still removes it.
    engine._brain.query.return_value = _stale_decisions()
    result = await _build(engine)
    old = _cands(result)["d-old"]
    assert old["disposition_stage"] == "relevance_filter:score_gap<0.5x|after:staleness"
    stale = [m for m in old["mutations"] if m["stage"] == "staleness"]
    assert stale and stale[0]["score_after"] == pytest.approx(0.930 * 0.3)


async def test_gap_cut_label_is_unchanged_when_no_modifier_touched_the_item():
    engine = _engine()
    engine._heart.search_facts.return_value = [
        FakeFact("a", 0.9, subject="s0"),
        FakeFact("b", 0.88, subject="s1"),
        FakeFact("c", 0.86, subject="s2"),
        FakeFact("low", 0.2, subject="s3"),
    ]
    result = await _build(engine)
    assert _cands(result)["low"]["disposition_stage"] == "relevance_filter:score_gap<0.5x"


async def test_episode_decay_is_traced_as_a_mutation():
    engine = _engine()
    old = FakeEpisode("e-old", 0.9)
    old.started_at = NOW - timedelta(days=30)
    engine._heart.search_episodes.return_value = [FakeEpisode("e-new", 0.95), old]
    result = await _build(engine)
    cands = _cands(result)
    assert [m["stage"] for m in cands["e-old"]["mutations"]] == ["episode_recency"]
    # One hour old: recency moves the score by well under 1%, so no row.
    assert cands["e-new"]["mutations"] == []


async def test_frame_boost_is_traced_and_named_in_the_gap_cut():
    engine = _engine()
    engine._heart.search_facts.return_value = [
        FakeFact("a", 0.95, subject="s0"),
        FakeFact("b", 0.93, subject="s1"),
        FakeFact("c", 0.90, subject="s2"),
        FakeFact("low", 0.10, subject="s3", encoded_frame="question"),
    ]
    result = await _build(engine)
    low = _cands(result)["low"]
    assert low["disposition_stage"] == "relevance_filter:score_gap<0.5x|after:frame_boost"
    assert [m["stage"] for m in low["mutations"]] == ["frame_boost"]


# ---------------------------------------------------------------------------
# Abandoned episodes
# ---------------------------------------------------------------------------


async def test_abandoned_episode_is_not_recalled():
    engine = _engine()
    engine._heart.search_episodes.return_value = [
        FakeEpisode("e-gone", 0.95, outcome="abandoned"),
        FakeEpisode("e-ok", 0.9, outcome="success"),
    ]
    result = await _build(engine)
    assert result.recalled_ids["episode"] == ["e-ok"]
    gone = _cands(result)["e-gone"]
    assert (gone["disposition"], gone["disposition_stage"]) == ("filter_dropped", "abandoned_episode")


async def test_abandoned_episode_is_not_shown_in_recent_conversations():
    engine = _engine(temporal_context_enabled=True)
    engine._heart.list_episodes.return_value = [
        FakeEpisode("t-gone", None, outcome="abandoned"),
        FakeEpisode("t-ok", None, outcome="success"),
    ]
    result = await _build(engine)
    recent = next(s for s in result.sections if s.label == "Recent Conversations")
    assert "episode t-ok" in recent.content
    assert "episode t-gone" not in recent.content
    gone = _cands(result)["t-gone"]
    assert (gone["disposition"], gone["disposition_stage"]) == ("filter_dropped", "abandoned_episode")


# ---------------------------------------------------------------------------
# Per-leg query text
# ---------------------------------------------------------------------------


async def test_legs_record_the_query_they_searched_with():
    engine = _engine()
    result = await _build(
        engine, plan=_plan(query_text="deploy prod scheduler"), input_text="How do I deploy the scheduler to prod?"
    )
    for leg in ("context_decisions", "context_facts", "context_episodes"):
        assert _leg(result, leg)["query"] == "deploy prod scheduler"


async def test_ladder_leg_records_the_raw_input():
    engine = _engine(proc_selection_graph_primary=True)
    engine._select_procedures = AsyncMock(return_value=[])
    result = await _build(engine, plan=_plan(query_text="bag"), input_text="the raw sentence")
    assert _leg(result, "context_procedures_ladder")["query"] == "the raw sentence"


def test_leg_query_is_truncated_and_omitted_when_unknown():
    t = RetrievalTrace(query="q", query_chars=5)
    t.leg("with_query", query="abcdefghij")
    t.leg("without_query")
    legs = {lg["name"]: lg for lg in t.to_dict()["legs"]}
    assert legs["with_query"]["query"] == "abcde"
    assert "query" not in legs["without_query"]
