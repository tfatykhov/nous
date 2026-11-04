"""K-13: facts the agent inferred or merged render apart from stated facts.

A sleep reflection or a session lesson is the agent's own conclusion, and a
merged fact is the agent's rewrite of several facts. Without a marker they read
exactly like something the user said. ``FactSummary`` now carries ``source``,
the fact results carry ``metadata["source"]``, and behind
``fact_source_marker_enabled`` (default off) recall_deep and the pre-turn
context end such a fact's line with ``[inferred]`` or ``[merged]``.
"""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import delete
from test_retrieval_pipeline import _make_brain, _make_heart, _make_settings  # noqa: E402

from nous.api.retrieval_pipeline import (
    PipelineResult,
    PipelineStats,
    _heart_results_to_pipeline,
    _keyed_r2_to_pipeline,
    _keyed_to_pipeline,
    failure_note,
    run_recall_pipeline,
)
from nous.api.tools import _format_pipeline_text, create_nous_tools
from nous.config import Settings
from nous.heart.facts import FactManager
from nous.heart.heart import Heart
from nous.heart.schemas import FactSummary, RecallResult, fact_source_marker
from nous.storage.models import Fact, FactEntityKey

ROOT = Path(__file__).resolve().parents[1]

# (source, the marker the line must end with)
CASES = [
    ("sleep_reflection", " [inferred]"),
    ("reflection", " [inferred]"),
    ("cluster_consolidation", " [merged]"),
    ("contradiction_resolution", " [merged]"),
    ("user_direct", ""),
    ("conversation", ""),
    ("enumerative_extractor", ""),
    (None, ""),
]


# ---------------------------------------------------------------------------
# The field and the shared marker map
# ---------------------------------------------------------------------------


def test_fact_summary_carries_the_persisted_source():
    assert FactSummary.model_fields["source"].default is None
    assert "source" in Fact.__table__.c
    assert "source" in FactManager.LEGACY_SUMMARY_FIELDS


@pytest.mark.parametrize(("source", "marker"), CASES)
def test_the_marker_map_is_an_allowlist(source, marker):
    assert fact_source_marker(source) == marker


def test_an_unhashable_or_odd_source_marks_nothing():
    assert fact_source_marker(["sleep_reflection"]) == ""
    assert fact_source_marker(42) == ""


def test_every_fact_summary_builder_copies_source():
    """A builder added later without ``source=`` fails here instead of reading None."""
    tree = ast.parse((ROOT / "nous" / "heart" / "facts.py").read_text(encoding="utf-8"))
    builders = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "FactSummary"
    ]
    assert len(builders) == 5
    for call in builders:
        assert "source" in {kw.arg for kw in call.keywords}, f"line {call.lineno}"


def test_fact_recall_results_carry_source_into_the_pipeline():
    fact = FactSummary(
        id=uuid4(), content="c", category=None, subject=None, confidence=1.0, active=True, source="sleep_reflection"
    )
    rr = Heart._to_recall_result(None, "fact", fact, 0.5)
    assert rr.metadata["source"] == "sleep_reflection"
    (piped,) = _heart_results_to_pipeline([rr])
    assert piped.metadata["source"] == "sleep_reflection"


@pytest.mark.parametrize("builder", [_keyed_to_pipeline, _keyed_r2_to_pipeline])
def test_keyed_rows_carry_source(builder):
    row = SimpleNamespace(
        id=uuid4(),
        content="c",
        matched=1,
        subject=None,
        event_date=None,
        learned_at=None,
        source_episode_id=None,
        source="cluster_consolidation",
    )
    (out,), _ = builder([row], SimpleNamespace(), set())
    assert out.metadata["source"] == "cluster_consolidation"


# ---------------------------------------------------------------------------
# Real Postgres: the reads return the column
# ---------------------------------------------------------------------------


@pytest.mark.postgres_only
async def test_fetch_by_entity_keys_returns_source(db):
    agent = f"test-k13-keyed-{uuid4()}"
    fm = FactManager(db, None, agent)
    try:
        async with db.session() as s:
            fact = Fact(agent_id=agent, content="Larch lessons", source="sleep_reflection")
            s.add(fact)
            await s.flush()
            s.add(FactEntityKey(fact_id=fact.id, entity_key="larch", agent_id=agent))
            await s.commit()

        (row,) = await fm.fetch_by_entity_keys(["larch"], track=False)

        assert row.source == "sleep_reflection"
    finally:
        async with db.session() as s:
            await s.execute(delete(Fact).where(Fact.agent_id == agent))
            await s.commit()


@pytest.mark.postgres_only
async def test_get_sources_maps_fact_ids_to_their_source(heart, session):
    a = heart.agent_id
    rows = [
        Fact(agent_id=a, content="lesson", source="reflection"),
        Fact(agent_id=a, content="told", source=None),
        Fact(agent_id=f"{a}-other", content="not mine", source="sleep_reflection"),
    ]
    session.add_all(rows)
    await session.flush()

    out = await heart.facts.get_sources([r.id for r in rows], session=session)

    assert out == {rows[0].id: "reflection", rows[1].id: None}
    assert await heart.facts.get_sources([]) == {}


# ---------------------------------------------------------------------------
# Pipeline: graph-reached facts get their source too (flag on only)
# ---------------------------------------------------------------------------


def _settings(enabled: bool | None):
    s = _make_settings(graph_recall_enabled=False)
    if enabled is not None:
        s.fact_source_marker_enabled = enabled
    return s


def _brain():
    return _make_brain(neighbors_by_node={}, contradictions=[], decision_results=[])


def _recall(source_by_summary: dict[str, str | None]) -> list[RecallResult]:
    out = []
    for i, (summary, source) in enumerate(source_by_summary.items()):
        out.append(
            RecallResult(type="fact", id=uuid4(), summary=summary, score=0.9 - i * 0.1, metadata={"source": source})
        )
    return out


class TestPipelineFill:
    async def test_flag_off_never_looks_up(self):
        heart = _make_heart(recall_results=[RecallResult(type="fact", id=uuid4(), summary="s", score=0.9)])
        heart.facts.get_sources = AsyncMock(return_value={})

        for settings in (_settings(None), _settings(False)):
            await run_recall_pipeline(query="q", heart=heart, brain=_brain(), settings=settings, limit=10)

        heart.facts.get_sources.assert_not_awaited()

    async def test_only_rows_without_a_source_are_looked_up(self):
        carried = RecallResult(type="fact", id=uuid4(), summary="carried", score=0.9, metadata={"source": None})
        bare = RecallResult(type="fact", id=uuid4(), summary="graph-reached", score=0.8)
        heart = _make_heart(recall_results=[carried, bare])
        heart.facts.get_sources = AsyncMock(return_value={bare.id: "sleep_reflection"})

        results, _ = await run_recall_pipeline(query="q", heart=heart, brain=_brain(), settings=_settings(True))

        heart.facts.get_sources.assert_awaited_once()
        assert list(heart.facts.get_sources.await_args.args[0]) == [bare.id]
        by_id = {r.id: r for r in results}
        assert by_id[bare.id].metadata["source"] == "sleep_reflection"
        assert by_id[carried.id].metadata["source"] is None
        assert [(r.id, r.score) for r in results] == [(carried.id, 0.9), (bare.id, 0.8)]

    async def test_nothing_to_fill_means_no_query(self):
        heart = _make_heart(recall_results=_recall({"a": "reflection"}))
        heart.facts.get_sources = AsyncMock(return_value={})

        await run_recall_pipeline(query="q", heart=heart, brain=_brain(), settings=_settings(True))

        heart.facts.get_sources.assert_not_awaited()

    async def test_lookup_failure_keeps_results_and_is_traced_but_not_noted(self):
        bare = RecallResult(type="fact", id=uuid4(), summary="graph-reached", score=0.8)
        heart = _make_heart(recall_results=[bare])
        heart.facts.get_sources = AsyncMock(side_effect=RuntimeError("db gone"))

        results, stats = await run_recall_pipeline(query="q", heart=heart, brain=_brain(), settings=_settings(True))

        assert [r.id for r in results] == [bare.id]
        assert "source" not in results[0].metadata
        assert stats.n_stage_errors.get("fact_sources") == 1
        assert failure_note(stats) is None


# ---------------------------------------------------------------------------
# recall_deep text
# ---------------------------------------------------------------------------


async def _recall_deep_text(heart, settings) -> str:
    tools = create_nous_tools(brain=_brain(), heart=heart, settings=settings)
    result = await tools["recall_deep"](query="larch", limit=10)
    return result["content"][0]["text"]


def _line(text: str, summary: str) -> str:
    return next(line for line in text.splitlines() if f"] {summary}" in line)


class TestRecallDeepRender:
    async def test_flag_off_text_is_byte_identical(self):
        recall = _recall({f"fact from {s}": s for s, _ in CASES})

        before = await _recall_deep_text(_make_heart(recall_results=recall), _settings(None))
        off = await _recall_deep_text(_make_heart(recall_results=recall), _settings(False))

        assert off == before
        assert "[inferred]" not in before and "[merged]" not in before

    async def test_flag_on_ends_each_line_with_its_marker(self):
        sources = {f"fact from {s}": s for s, _ in CASES}

        text = await _recall_deep_text(_make_heart(recall_results=_recall(sources)), _settings(True))

        for source, marker in CASES:
            line = _line(text, f"fact from {source}")
            assert f"fact from {source}{marker} (id: " in line, line

    def _rows(self) -> list[PipelineResult]:
        return [
            PipelineResult(
                id=uuid4(),
                type="fact",
                description="in a session",
                score=0.9,
                metadata={"source_episode_id": str(uuid4()), "source": "sleep_reflection"},
            ),
            PipelineResult(
                id=uuid4(),
                type="fact",
                description="graph neighbour",
                score=0.8,
                source="graph_expanded",
                edge_relation="co_occurs",
                metadata={"stage_origin": "heart_graph_memory", "source": "contradiction_resolution"},
            ),
            PipelineResult(
                id=uuid4(),
                type="fact",
                description="dated",
                score=0.7,
                metadata={"source": "reflection", "recency_status": "current", "recency_date": "2026-03"},
            ),
        ]

    @pytest.mark.parametrize("grouped", [False, True])
    def test_every_heart_memory_site_marks_and_off_marks_none(self, grouped):
        rows = self._rows()

        on = _format_pipeline_text(rows, PipelineStats(), ["all"], session_group_heart=grouped, source_marker=True)
        off = _format_pipeline_text(rows, PipelineStats(), ["all"], session_group_heart=grouped)

        assert "in a session [inferred] (id: " in on
        assert "[via co_occurs] graph neighbour [merged] (id: " in on
        # After the recency tag, which carries no leading space of its own.
        assert "dated[current 2026-03] [inferred] (id: " in on
        assert "[inferred]" not in off and "[merged]" not in off


# ---------------------------------------------------------------------------
# Pre-turn context
# ---------------------------------------------------------------------------


def _engine(**kwargs):
    from unittest.mock import MagicMock

    from nous.cognitive.context import ContextEngine

    return ContextEngine(MagicMock(), MagicMock(), Settings(_env_file=None, **kwargs), identity_prompt="")


def _facts() -> list[SimpleNamespace]:
    return [
        SimpleNamespace(id=uuid4(), content=f"fact from {s}", confidence=0.7, subject="larch", source=s)
        for s, _ in CASES
    ]


class TestPreTurnRender:
    def test_flag_defaults_off_and_off_is_byte_identical(self):
        assert Settings.model_fields["fact_source_marker_enabled"].default is False
        facts = _facts()
        without_source = [SimpleNamespace(**{k: v for k, v in vars(f).items() if k != "source"}) for f in facts]

        assert _engine()._format_facts(facts) == _engine()._format_facts(without_source)

    def test_flag_on_marks_before_the_confidence(self):
        out = _engine(fact_source_marker_enabled=True)._format_facts(_facts())

        for source, marker in CASES:
            assert f"- [larch] fact from {source}{marker} [confidence: 0.70]" in out.splitlines()

    def test_the_marker_follows_the_recency_and_lineage_tags(self):
        fact = SimpleNamespace(
            id=uuid4(),
            content="lesson",
            confidence=0.7,
            subject=None,
            source="sleep_reflection",
            recency_status="current",
            recency_date="2026-03",
        )
        engine = _engine(fact_source_marker_enabled=True, supersession_lineage_mode="tag")

        out = engine._format_facts([fact], lineage={str(fact.id): ["older"]})

        assert out == (
            "- lesson [current 2026-03] [current — supersedes an earlier belief] [inferred] [confidence: 0.70]"
        )
