"""Bi-temporal facts, PR3: the read path and rendering.

- ``temporal_resolver_intervals_enabled``: both recency resolvers use the interval relation when a span or state is
  involved (coexisting facts get no tag), keep the B-1 statement-time rule for two point facts, and read statement
  time as ``COALESCE(mentioned_at, learned_at)``.
- ``temporal_read_intervals_enabled`` (R): the date leg matches a fact whose interval overlaps the window.
- ``temporal_render_enabled`` (D): facts carry a date label and dated facts render in date order, in recall_deep and
  in the pre-turn Relevant Facts section.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from nous.api.retrieval_pipeline import PipelineResult, PipelineStats, _resolve_recency_conflicts
from nous.api.tools import _format_pipeline_text
from nous.cognitive.context import ContextEngine
from nous.cognitive.schemas import FrameSelection
from nous.config import Settings
from nous.heart.schemas import FactSummary

D = date


# ---------------------------------------------------------------------------
# Resolver: recall_deep copy (_resolve_recency_conflicts)
# ---------------------------------------------------------------------------


def _rsettings(intervals: bool):
    return SimpleNamespace(
        recency_resolver_enabled=True,
        recency_resolver_similarity_floor=0.55,
        temporal_resolver_intervals_enabled=intervals,
    )


def _pfact(desc: str, *, valid_from=None, valid_to=None, event_date=None, learned_at=None, mentioned_at=None):
    meta: dict = {"subject": "alice"}
    for key, value in (
        ("valid_from", valid_from),
        ("valid_to", valid_to),
        ("event_date", event_date),
        ("learned_at", learned_at),
        ("mentioned_at", mentioned_at),
    ):
        if value is not None:
            meta[key] = value.isoformat()
    return PipelineResult(id=uuid4(), type="fact", description=desc, score=1.0, source="heart", metadata=meta)


def _tags(results):
    return {r.description: (r.metadata.get("recency_status"), r.metadata.get("recency_date"), r.score) for r in results}


@pytest.mark.parametrize("intervals", [False, True])
def test_pipeline_coexisting_spans_get_no_tag(intervals):
    a = _pfact("Alice lived in Paris in winter", valid_from=D(2024, 1, 1), valid_to=D(2024, 2, 1))
    b = _pfact("Alice lived in Porto in summer", valid_from=D(2024, 6, 1), valid_to=D(2024, 8, 1))
    out = _resolve_recency_conflicts([a, b], _rsettings(intervals))
    assert all(r.metadata.get("recency_status") is None for r in out)


def test_pipeline_a_later_state_is_current_only_with_the_flag():
    old = _pfact("Alice lives in Paris", valid_from=D(2024, 1, 1))
    new = _pfact("Alice lives in Berlin", valid_from=D(2024, 6, 1))
    off = _resolve_recency_conflicts([old, new], _rsettings(False))
    assert all(r.metadata.get("recency_status") is None for r in off)
    tags = _tags(_resolve_recency_conflicts([old, new], _rsettings(True)))
    assert tags["Alice lives in Berlin"] == ("current", "2024-06", 1.0)
    assert tags["Alice lives in Paris"] == ("superseded", "2024-01", pytest.approx(0.3))


def test_pipeline_two_points_keep_the_statement_time_rule_read_as_mentioned_at_first():
    # Points on different days are a correction: the one stated later is current (B-1). Statement time is
    # mentioned_at when set, so a fact ingested first but said later wins once the flag is on.
    said_later = _pfact(
        "Alice's flight is at 5pm",
        valid_from=D(2024, 3, 5),
        valid_to=D(2024, 3, 5),
        event_date=D(2024, 3, 5),
        learned_at=datetime(2024, 3, 1, tzinfo=UTC),
        mentioned_at=datetime(2024, 3, 4, tzinfo=UTC),
    )
    said_first = _pfact(
        "Alice's flight is at 3pm",
        valid_from=D(2024, 3, 3),
        valid_to=D(2024, 3, 3),
        event_date=D(2024, 3, 3),
        learned_at=datetime(2024, 3, 2, tzinfo=UTC),
        mentioned_at=datetime(2024, 3, 2, tzinfo=UTC),
    )
    off = _tags(_resolve_recency_conflicts([said_later, said_first], _rsettings(False)))
    assert off["Alice's flight is at 3pm"][0] == "current"  # learned_at only
    on = _tags(_resolve_recency_conflicts([said_later, said_first], _rsettings(True)))
    assert on["Alice's flight is at 5pm"][0] == "current" and on["Alice's flight is at 3pm"][0] == "superseded"


# ---------------------------------------------------------------------------
# Resolver: pre-turn copy (ContextEngine._resolve_recency)
# ---------------------------------------------------------------------------


def _engine(**flags) -> ContextEngine:
    brain = AsyncMock()
    brain.embeddings = MagicMock()
    settings = Settings(_env_file=None, recency_resolver_enabled=True, **flags)
    return ContextEngine(brain, AsyncMock(), settings, identity_prompt="")


def _sfact(content: str, *, subject="alice", score=0.9, **dates) -> FactSummary:
    return FactSummary(
        id=uuid4(),
        content=content,
        category="concept",
        subject=subject,
        confidence=1.0,
        active=True,
        score=score,
        **dates,
    )


@pytest.mark.parametrize("intervals", [False, True])
def test_preturn_coexisting_spans_get_no_tag(intervals):
    a = _sfact("Alice lived in Paris in winter", valid_from=D(2024, 1, 1), valid_to=D(2024, 2, 1))
    b = _sfact("Alice lived in Porto in summer", valid_from=D(2024, 6, 1), valid_to=D(2024, 8, 1))
    out = _engine(temporal_resolver_intervals_enabled=intervals)._resolve_recency([a, b])
    assert [f.recency_status for f in out] == [None, None]


def test_preturn_a_later_state_is_current_only_with_the_flag():
    def pair():
        return (
            _sfact("Alice lives in Paris", valid_from=D(2024, 1, 1)),
            _sfact("Alice lives in Berlin", valid_from=D(2024, 6, 1)),
        )

    old, new = _engine()._resolve_recency(list(pair()))
    assert (old.recency_status, new.recency_status) == (None, None)
    old, new = _engine(temporal_resolver_intervals_enabled=True)._resolve_recency(list(pair()))
    assert (new.recency_status, new.recency_date) == ("current", "2024-06")
    assert (old.recency_status, old.recency_date) == ("superseded", "2024-01")
    assert old.score == pytest.approx(0.27)


def test_preturn_two_points_read_statement_time_as_mentioned_at_first():
    def pair():
        return (
            _sfact(
                "Alice's flight is at 5pm",
                valid_from=D(2024, 3, 5),
                valid_to=D(2024, 3, 5),
                event_date=D(2024, 3, 5),
                learned_at=datetime(2024, 3, 1, tzinfo=UTC),
                mentioned_at=datetime(2024, 3, 4, tzinfo=UTC),
            ),
            _sfact(
                "Alice's flight is at 3pm",
                valid_from=D(2024, 3, 3),
                valid_to=D(2024, 3, 3),
                event_date=D(2024, 3, 3),
                learned_at=datetime(2024, 3, 2, tzinfo=UTC),
                mentioned_at=datetime(2024, 3, 2, tzinfo=UTC),
            ),
        )

    later, first = _engine()._resolve_recency(list(pair()))
    assert first.recency_status == "current"
    later, first = _engine(temporal_resolver_intervals_enabled=True)._resolve_recency(list(pair()))
    assert (later.recency_status, first.recency_status) == ("current", "superseded")


def test_one_day_intervals_without_an_event_date_are_points_in_both_copies():
    stated = {"first": datetime(2024, 3, 1, tzinfo=UTC), "later": datetime(2024, 3, 2, tzinfo=UTC)}
    days = {"first": D(2024, 3, 5), "later": D(2024, 3, 3)}
    texts = {"first": "Alice's dentist visit is on the 5th", "later": "Alice's dentist visit is on the 3rd"}

    pipe = [_pfact(texts[k], valid_from=days[k], valid_to=days[k], learned_at=stated[k]) for k in ("first", "later")]
    assert all(r.metadata.get("recency_status") is None for r in _resolve_recency_conflicts(pipe, _rsettings(False)))
    tags = _tags(_resolve_recency_conflicts(pipe, _rsettings(True)))
    assert (tags[texts["later"]][:2], tags[texts["first"]][:2]) == (("current", "2024-03"), ("superseded", "2024-03"))

    def pre():
        return [
            _sfact(texts[k], valid_from=days[k], valid_to=days[k], learned_at=stated[k]) for k in ("first", "later")
        ]

    first, later = _engine()._resolve_recency(pre())
    assert (first.recency_status, later.recency_status) == (None, None)
    first, later = _engine(temporal_resolver_intervals_enabled=True)._resolve_recency(pre())
    assert (later.recency_status, first.recency_status) == ("current", "superseded")


# ---------------------------------------------------------------------------
# Date leg (R)
# ---------------------------------------------------------------------------


@pytest.mark.postgres_only
@pytest.mark.parametrize("intervals", [False, True])
async def test_the_date_leg_matches_a_span_only_with_r_and_points_either_way(db, session, mock_embeddings, intervals):
    from nous.heart import Heart
    from nous.heart.date_window import DateWindow
    from nous.storage.models import Fact

    agent = f"test-t3-leg-{uuid4().hex[:8]}"
    settings = Settings(_env_file=None, agent_id=agent, temporal_read_intervals_enabled=intervals)
    heart = Heart(db, settings, embedding_provider=mock_embeddings)
    try:
        rows = {
            "point": dict(event_date=D(2024, 2, 10), valid_from=D(2024, 2, 10), valid_to=D(2024, 2, 10)),
            "span": dict(valid_from=D(2024, 1, 1), valid_to=D(2024, 3, 31)),
            "open": dict(valid_from=D(2023, 6, 1)),
            "before": dict(valid_from=D(2023, 1, 1), valid_to=D(2023, 2, 1)),
            "after": dict(event_date=D(2024, 5, 1), valid_from=D(2024, 5, 1), valid_to=D(2024, 5, 1)),
        }
        ids = {}
        for name, dates in rows.items():
            fact = Fact(
                id=uuid4(),
                agent_id=agent,
                content=f"{name} trip to Lisbon",
                category="concept",
                embedding=await mock_embeddings.embed(f"{name} trip to Lisbon"),
                **dates,
            )
            session.add(fact)
            ids[fact.id] = name
        await session.flush()
        window = DateWindow(start=D(2024, 2, 1), end=D(2024, 2, 29))
        hits = await heart.facts._date_window_leg(session, await mock_embeddings.embed("trip"), window, 15)
        found = {ids[i] for i, _ in hits if i in ids}
        assert found == ({"point", "span", "open"} if intervals else {"point"})
    finally:
        await heart.close()


# ---------------------------------------------------------------------------
# Rendering (D): recall_deep
# ---------------------------------------------------------------------------


def _row(desc, *, score, valid_from=None, valid_to=None, source_episode_id=None, rtype="fact"):
    meta: dict = {}
    if valid_from is not None:
        meta["valid_from"] = valid_from.isoformat()
    if valid_to is not None:
        meta["valid_to"] = valid_to.isoformat()
    if source_episode_id is not None:
        meta["source_episode_id"] = source_episode_id
    return PipelineResult(id=uuid4(), type=rtype, description=desc, score=score, source="heart", metadata=meta)


def _lines(text: str) -> list[str]:
    return [ln for ln in text.splitlines() if ln[:1].isdigit()]


def _flat_rows():
    return [
        _row("March event", score=0.9, valid_from=D(2024, 3, 1), valid_to=D(2024, 3, 1)),
        _row("undated fact", score=0.8),
        _row("January state", score=0.7, valid_from=D(2024, 1, 5)),
        _row("an episode", score=0.6, rtype="episode"),
        _row("February month", score=0.5, valid_from=D(2024, 2, 1), valid_to=D(2024, 2, 29)),
    ]


def test_recall_deep_with_d_off_is_unchanged_by_dates():
    rows = _flat_rows()
    plain = [
        PipelineResult(id=r.id, type=r.type, description=r.description, score=r.score, source="heart", metadata={})
        for r in rows
    ]
    on_dates = _format_pipeline_text(rows, PipelineStats(), ["all"])
    assert on_dates == _format_pipeline_text(plain, PipelineStats(), ["all"])
    assert on_dates == _format_pipeline_text(rows, PipelineStats(), ["all"], temporal_render=False)


def test_recall_deep_flat_list_sorts_dated_facts_and_keeps_the_rest_in_rank_order():
    lines = _lines(_format_pipeline_text(_flat_rows(), PipelineStats(), ["all"], temporal_render=True))
    assert [ln.split("(id:")[0] for ln in lines] == [
        "1. [fact] January state [since 2024-01-05] ",
        "2. [fact] undated fact ",
        "3. [fact] February month [2024-02] ",
        "4. [episode] an episode ",
        "5. [fact] March event [2024-03-01] ",
    ]
    assert "score: 0.700 [rrf])" in lines[0] and "score: 0.900 [rrf])" in lines[4]


def test_recall_deep_sorts_inside_each_session_bucket_and_keeps_the_bucket_order():
    s1, s2 = str(uuid4()), str(uuid4())
    rows = [
        _row("s1 June", score=0.9, valid_from=D(2024, 6, 1), valid_to=D(2024, 6, 1), source_episode_id=s1),
        _row("s2 May", score=0.8, valid_from=D(2024, 5, 1), valid_to=D(2024, 5, 1), source_episode_id=s2),
        _row("s1 April", score=0.7, valid_from=D(2024, 4, 1), valid_to=D(2024, 4, 1), source_episode_id=s1),
        _row("other September", score=0.6, valid_from=D(2024, 9, 1), valid_to=D(2024, 9, 1)),
        _row("other undated", score=0.5),
        _row("other August", score=0.4, valid_from=D(2024, 8, 1), valid_to=D(2024, 8, 1)),
    ]
    text = _format_pipeline_text(rows, PipelineStats(), ["all"], session_group_heart=True, temporal_render=True)
    order = [ln.split(". [fact] ")[1].split(" (id:")[0].split(" [")[0] for ln in _lines(text)]
    assert order == ["s1 April", "s1 June", "s2 May", "other August", "other undated", "other September"]
    assert text.index(f"-- Session {s1[:8]} --") < text.index(f"-- Session {s2[:8]} --") < text.index("-- Other --")
    assert [ln.split(".")[0] for ln in _lines(text)] == ["1", "2", "3", "4", "5", "6"]


def test_recall_deep_date_label_sits_before_the_recency_tag():
    row = PipelineResult(
        id=uuid4(),
        type="fact",
        description="Alice lives in Berlin",
        score=0.5,
        source="heart",
        metadata={"valid_from": "2024-06-01", "recency_status": "current", "recency_date": "2024-06"},
    )
    text = _format_pipeline_text([row], PipelineStats(), ["all"], temporal_render=True)
    assert "Alice lives in Berlin [since 2024-06-01][current 2024-06] (id:" in text


# ---------------------------------------------------------------------------
# Rendering (D): pre-turn
# ---------------------------------------------------------------------------


def test_preturn_format_truncates_by_rank_then_orders_by_date():
    engine = _engine(temporal_render_enabled=True, fact_format_max_chars=50)
    long = "a long fact about the March trip, with enough words to run past fifty characters"
    cut = long.replace("March", "January")[:50].rsplit(" ", 1)[0] + "..."
    facts = [
        _sfact(long, subject=None, valid_from=D(2024, 3, 1), valid_to=D(2024, 3, 1)),  # rank 1: full
        _sfact("undated note", subject=None),
        _sfact(long.replace("March", "January"), subject=None, valid_from=D(2024, 1, 1)),  # rank 3: cut
    ]
    lines = engine._format_facts(facts, full_top_n=1, dated=True).splitlines()
    assert lines[0] == f"- {cut} [since 2024-01-01] [confidence: 1.00]"
    assert lines[1].startswith("- undated note [confidence")
    assert lines[2] == f"- {long} [2024-03-01] [confidence: 1.00]"
    plain = engine._format_facts(facts, full_top_n=1).splitlines()
    assert plain[0] == f"- {long} [confidence: 1.00]" and "[2024" not in "".join(plain)


def test_preturn_budget_drops_the_lowest_ranked_line_before_the_date_order():
    engine = _engine(temporal_render_enabled=True)
    facts = [
        _sfact("rank one, the latest", subject=None, valid_from=D(2024, 9, 1), valid_to=D(2024, 9, 1)),
        _sfact("rank two", subject=None, valid_from=D(2024, 5, 1), valid_to=D(2024, 5, 1)),
        _sfact("rank three, the earliest", subject=None, valid_from=D(2024, 1, 1), valid_to=D(2024, 1, 1)),
    ]
    two_lines = len(engine._format_facts(facts[:2], dated=True))
    text = engine._format_facts(facts, dated=True, budget_tokens=two_lines // 4 + 1)
    assert [ln.split(" [")[0] for ln in text.splitlines()] == ["- rank two", "- rank one, the latest"]


def test_preturn_date_label_sits_before_the_recency_tag():
    engine = _engine(temporal_render_enabled=True)
    fact = _sfact("Alice lives in Berlin", valid_from=D(2024, 6, 1))
    fact.recency_status, fact.recency_date = "current", "2024-06"
    assert engine._format_facts([fact], dated=True) == (
        "- [alice] Alice lives in Berlin [since 2024-06-01] [current 2024-06] [confidence: 1.00]"
    )


def _frame():
    return FrameSelection(
        frame_id="question", frame_name="Question", description="test", confidence=0.9, match_method="pattern"
    )


@pytest.mark.parametrize("render", [False, True])
async def test_build_dates_relevant_facts_with_d_and_leaves_the_user_profile_alone(render):
    engine = _engine(temporal_render_enabled=render)
    heart = engine._heart
    dated = [
        _sfact("visited Porto", subject="trip", score=0.9, valid_from=D(2024, 5, 1), valid_to=D(2024, 5, 1)),
        _sfact("visited Faro", subject="trip2", score=0.8, valid_from=D(2024, 2, 1), valid_to=D(2024, 2, 1)),
    ]
    profile = [_sfact("prefers tea", subject="user", valid_from=D(2024, 1, 1), valid_to=D(2024, 1, 1))]
    heart.search_facts.return_value = dated
    heart.list_censors.return_value = []
    heart.list_facts_by_category.return_value = profile
    heart.get_working_memory.return_value = None
    heart.search_episodes.return_value = []
    heart.list_episodes.return_value = []
    heart.list_procedures.return_value = ([], 0)
    engine._brain.query.return_value = []
    result = await engine.build(agent_id="a", session_id="s", input_text="where did I travel?", frame=_frame())
    facts = next(s for s in result.sections if s.label == "Relevant Facts").content
    user = next(s for s in result.sections if s.label == "User Profile").content
    if render:
        assert facts.index("Faro [2024-02-01]") < facts.index("Porto [2024-05-01]")
    else:
        assert facts.index("Porto") < facts.index("Faro") and "[2024" not in facts
    assert "[2024" not in user


def test_keyed_rows_carry_their_interval_and_statement_time():
    from nous.api.retrieval_pipeline import _keyed_r2_to_pipeline, _keyed_to_pipeline

    row = SimpleNamespace(
        id=uuid4(),
        matched=2,
        subject="alice",
        content="Alice lives in Berlin",
        event_date=None,
        learned_at=datetime(2024, 6, 3, tzinfo=UTC),
        source_episode_id=None,
        valid_from=D(2024, 6, 1),
        valid_to=None,
        mentioned_at=datetime(2024, 6, 2, tzinfo=UTC),
    )
    for convert in (_keyed_to_pipeline, _keyed_r2_to_pipeline):
        (result,), _ = convert([row], SimpleNamespace(), set())
        assert result.metadata["valid_from"] == "2024-06-01"
        assert result.metadata["mentioned_at"] == "2024-06-02T00:00:00+00:00"
        assert "valid_to" not in result.metadata and "event_date" not in result.metadata


def test_keyed_rows_without_dates_keep_today_s_metadata_exactly():
    from nous.api.retrieval_pipeline import _keyed_r2_to_pipeline, _keyed_to_pipeline

    row = SimpleNamespace(
        id=uuid4(),
        matched=1,
        subject="alice",
        content="Alice likes tea",
        event_date=None,
        learned_at=datetime(2024, 6, 3, tzinfo=UTC),
        source_episode_id="ep-1",
        valid_from=None,
        valid_to=None,
        mentioned_at=None,
    )
    for convert, leg in ((_keyed_to_pipeline, "keyed"), (_keyed_r2_to_pipeline, "keyed_r2")):
        (result,), _ = convert([row], SimpleNamespace(), set())
        assert result.metadata == {
            "retrieval_leg": leg,
            "matched_keys": 1,
            "subject": "alice",
            "learned_at": "2024-06-03T00:00:00+00:00",
            "source_episode_id": "ep-1",
        }
        assert list(result.metadata) == ["retrieval_leg", "matched_keys", "subject", "learned_at", "source_episode_id"]


_FLAGS = ("temporal_read_intervals_enabled", "temporal_resolver_intervals_enabled", "temporal_render_enabled")


def test_the_three_flags_default_off_and_have_a_compose_line_and_a_doc_row(monkeypatch):
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    assert not any(getattr(Settings(_env_file=None), f) for f in _FLAGS)
    for flag in _FLAGS:
        monkeypatch.setenv(f"NOUS_{flag.upper()}", "true")
    assert all(getattr(Settings(_env_file=None), f) for f in _FLAGS)
    compose = (root / "docker-compose.yml").read_text(encoding="utf-8")
    doc = (root / "docs" / "reference" / "environment-variables.md").read_text(encoding="utf-8")
    for flag in _FLAGS:
        name = f"NOUS_{flag.upper()}"
        assert f"- {name}=${{{name}:-false}}" in compose, name
        assert f"| `{name}` | `false` |" in doc, name


def test_supersession_needs_rendering_and_stays_off_without_it(caplog):
    with caplog.at_level("WARNING"):
        alone = Settings(_env_file=None, temporal_supersession_enabled=True)
    assert alone.temporal_supersession_enabled is False
    assert "NOUS_TEMPORAL_SUPERSESSION_ENABLED=true needs NOUS_TEMPORAL_RENDER_ENABLED=true" in caplog.text
    both = Settings(_env_file=None, temporal_supersession_enabled=True, temporal_render_enabled=True)
    assert (both.temporal_supersession_enabled, both.temporal_render_enabled) == (True, True)
    assert Settings(_env_file=None, temporal_render_enabled=True).temporal_supersession_enabled is False
