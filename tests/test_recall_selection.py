"""Recall selection fixes.

Each test pins one defect: rows served twice or dropped by the graph legs'
dedup pools, contradiction edges never fetched for facts, the recency
resolver ordering by event date instead of statement time, F055 recording
rows the formatter never rendered, unlabelled score spaces, access tracking
for rows the merge cut, and stores that act on rows outside their agent or
target set.

Mock tests use a MagicMock heart/brain (no DB). Tests that write rows are
``postgres_only`` and use an agent id of their own.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest
from sqlalchemy import delete, text

from nous.api.retrieval_pipeline import (
    PipelineResult,
    PipelineStats,
    _keyed_r2_to_pipeline,
    _keyed_to_pipeline,
    _resolve_recency_conflicts,
    run_recall_pipeline,
)
from nous.api.tools import _format_pipeline_text
from nous.brain.schemas import DecisionSummary, NeighborResult
from nous.heart.schemas import FactSummary, RecallResult
from nous.storage.models import Censor, Decision, Episode, Fact, GraphEdge

FACT_ID = UUID("11111111-1111-1111-1111-111111111111")
DECISION_ID = UUID("44444444-4444-4444-4444-444444444444")
K1_ID = UUID("aaaaaaaa-0000-0000-0000-000000000001")  # round-1 keyed hit
R2A_ID = UUID("aaaaaaaa-0000-0000-0000-000000000002")  # round-2, inside K2
R2B_ID = UUID("aaaaaaaa-0000-0000-0000-000000000003")  # round-2, beyond K2
QUERY = "what happened at alpha station"


# ---------------------------------------------------------------------------
# Mock heart / brain
# ---------------------------------------------------------------------------


class _FakeSession:
    def __init__(self, edges):
        self._edges = edges

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None

    async def execute(self, _stmt):
        edges = self._edges
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: list(edges)))


def _brain(*, neighbors_by_node, decisions=(), edges=()):
    brain = MagicMock()
    brain.agent_id = "recall-selection-mock"
    brain.query = AsyncMock(return_value=list(decisions))

    async def _neighbors(node_id, node_type="decision", limit=10, *, neighbor_type=None):
        rows = neighbors_by_node.get(node_id, [])
        if neighbor_type:
            rows = [r for r in rows if r.node_type == neighbor_type]
        return rows[:limit]

    brain.neighbors = AsyncMock(side_effect=_neighbors)
    brain.db = MagicMock()
    brain.db.session = MagicMock(return_value=_FakeSession(list(edges)))
    return brain


def _keyed_row(fid: UUID, content: str, *, attribute_key: str | None = None, learned_at=None):
    return SimpleNamespace(
        id=fid,
        content=content,
        subject=None,
        event_date=None,
        source_episode_id=None,
        matched=1,
        learned_at=learned_at or datetime(2026, 1, 1, tzinfo=UTC),
        attribute_key=attribute_key,
    )


def _keyed_heart():
    """Stage 1 returns FACT_ID; keyed round 1 returns K1, round 2 returns R2A + R2B.

    R2A ranks first (its attribute key overlaps the query), so with K2=1 it is
    the only round-2 row that renders. R2B ranks second and never renders as
    keyed_r2.
    """
    heart = MagicMock()
    heart.agent_id = "recall-selection-mock"
    heart.recall = AsyncMock(return_value=[RecallResult(type="fact", id=FACT_ID, summary="stage one fact", score=0.9)])
    heart.facts.entity_key_vocabulary = AsyncMock(return_value=frozenset({"alpha station", "bravo hub"}))
    heart.facts.fetch_by_entity_keys = AsyncMock(
        side_effect=[
            [_keyed_row(K1_ID, "plain round one words")],
            [
                _keyed_row(R2B_ID, "plain round two words"),
                _keyed_row(R2A_ID, "plain round two words", attribute_key="alpha station"),
            ],
        ]
    )
    heart.facts.entity_keys_for_facts = AsyncMock(return_value={K1_ID: ["bravo hub"]})
    heart.facts.track_access = AsyncMock()
    return heart


def _settings(**overrides):
    base = dict(
        graph_recall_enabled=True,
        cross_type_linking_enabled=True,
        spreading_activation_enabled="false",
        spreading_activation_density_threshold=3.0,
        contradiction_detection=True,
        graph_recall_decay=0.7,
        graph_recall_max_expand=5,
        graph_recall_max_neighbors=3,
        heart_graph_all_types_enabled=False,
        heart_graph_neighbors_per_seed=3,
        episode_chunks_enabled=False,
        episode_chunk_recall_limit=10,
        coherent_ranking_enabled=False,
        graph_inferred_edge_penalty=0.5,
        keyed_fact_leg_enabled=True,
        keyed_fact_leg_rounds=2,
        keyed_fact_leg_k2=1,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _fact_neighbor(fid: UUID, relation: str = "related_to") -> NeighborResult:
    return NeighborResult(
        id=fid,
        node_type="fact",
        description=f"neighbor {fid}",
        edge_relation=relation,
        edge_weight=0.8,
        created_at=datetime.now(UTC),
    )


def _rows(results, rid):
    return [r for r in results if r.id == rid]


# ---------------------------------------------------------------------------
# Keyed ids join the graph legs' dedup pool
# ---------------------------------------------------------------------------


class TestKeyedIdsInGraphDedupPool:
    async def test_path_a_does_not_reserve_keyed_rows(self):
        heart = _keyed_heart()
        brain = _brain(
            neighbors_by_node={FACT_ID: [_fact_neighbor(K1_ID), _fact_neighbor(R2A_ID), _fact_neighbor(R2B_ID)]},
        )
        settings = _settings(heart_graph_all_types_enabled=True)

        results, stats = await run_recall_pipeline(QUERY, heart, brain, settings)

        k1 = _rows(results, K1_ID)
        assert len(k1) == 1 and k1[0].metadata.get("retrieval_leg") == "keyed"
        r2a = _rows(results, R2A_ID)
        assert len(r2a) == 1 and r2a[0].metadata.get("retrieval_leg") == "keyed_r2"
        # R2B is past the K2 cut: it never renders as keyed_r2, so the graph
        # leg must still be allowed to serve it.
        r2b = _rows(results, R2B_ID)
        assert len(r2b) == 1 and r2b[0].metadata.get("stage_origin") == "heart_graph_memory"
        assert stats.n_keyed_dup == 0

    async def test_spreading_excludes_keyed_rows_inside_the_k2_cut(self):
        heart = _keyed_heart()
        brain = _brain(neighbors_by_node={})
        settings = _settings(spreading_activation_enabled="true")
        search = AsyncMock(return_value=[])

        with patch("nous.brain.spreading_activation.spreading_activation_search", new=search):
            await run_recall_pipeline(QUERY, heart, brain, settings)

        excluded = search.await_args.kwargs["exclude_ids"]
        assert K1_ID in excluded
        assert R2A_ID in excluded
        assert R2B_ID not in excluded


# ---------------------------------------------------------------------------
# The 1-hop fallback does not re-serve earlier legs' rows
# ---------------------------------------------------------------------------


class TestOneHopFallbackSeenIds:
    async def test_one_hop_neighbor_already_served_by_heart_is_not_appended(self):
        heart = MagicMock()
        heart.recall = AsyncMock(
            return_value=[RecallResult(type="fact", id=FACT_ID, summary="stage one fact", score=0.9)]
        )
        decision = DecisionSummary(
            id=DECISION_ID,
            description="a decision",
            confidence=0.8,
            category="architecture",
            stakes="low",
            outcome="pending",
            score=0.7,
            created_at=datetime.now(UTC),
        )
        brain = _brain(neighbors_by_node={DECISION_ID: [_fact_neighbor(FACT_ID)]}, decisions=[decision])
        settings = _settings(keyed_fact_leg_enabled=False)

        results, _ = await run_recall_pipeline("anything", heart, brain, settings)

        assert len(_rows(results, FACT_ID)) == 1


# ---------------------------------------------------------------------------
# The recency resolver orders by statement time
# ---------------------------------------------------------------------------


def _dated_fact(description: str, *, learned_at: str | None, event_date: str, contradicts=()):
    meta = {"subject": "dentist", "event_date": event_date}
    if learned_at is not None:
        meta["learned_at"] = learned_at
    return PipelineResult(
        id=uuid.uuid4(),
        type="fact",
        description=description,
        score=0.9,
        source="heart",
        contradicts=list(contradicts),
        metadata=meta,
    )


class TestRecencyByStatementTime:
    def test_newer_correction_with_earlier_event_date_wins(self):
        settings = SimpleNamespace(recency_resolver_similarity_floor=0.55)
        original = _dated_fact(
            "Dentist appointment booked at the downtown clinic for the 25th",
            learned_at="2026-10-01T09:00:00+00:00",
            event_date="2026-11-25",
        )
        correction = _dated_fact(
            "Moved it: the tooth checkup is now on the 20th instead",
            learned_at="2026-10-20T09:00:00+00:00",
            event_date="2026-11-20",
            contradicts=[original.id],
        )

        out = {r.id: r for r in _resolve_recency_conflicts([original, correction], settings)}

        assert out[correction.id].metadata["recency_status"] == "current"
        assert out[original.id].metadata["recency_status"] == "superseded"
        assert out[original.id].score < out[correction.id].score

    def test_same_statement_time_falls_back_to_event_date(self):
        settings = SimpleNamespace(recency_resolver_similarity_floor=0.55)
        stamp = "2026-10-01T09:00:00+00:00"
        earlier = _dated_fact("Dentist on the 20th", learned_at=stamp, event_date="2026-11-20")
        later = _dated_fact("Dentist on the 25th", learned_at=stamp, event_date="2026-11-25", contradicts=[earlier.id])

        out = {r.id: r for r in _resolve_recency_conflicts([earlier, later], settings)}

        assert out[later.id].metadata["recency_status"] == "current"
        assert out[earlier.id].metadata["recency_status"] == "superseded"

    def test_learned_at_reaches_fact_metadata(self):
        from nous.heart.heart import Heart

        stated = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)
        summary = FactSummary(
            id=uuid.uuid4(),
            content="c",
            category=None,
            subject="dentist",
            confidence=1.0,
            active=True,
            learned_at=stated,
        )
        result = Heart._to_recall_result(MagicMock(), "fact", summary, 0.5)
        assert result.metadata["learned_at"] == stated.isoformat()

    def test_learned_at_reaches_keyed_metadata(self):
        stated = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)
        row = _keyed_row(K1_ID, "c", learned_at=stated)
        settings = SimpleNamespace(keyed_fact_leg_score=0.55, keyed_fact_leg_k=8)
        r1, _ = _keyed_to_pipeline([row], settings, set())
        r2, _ = _keyed_r2_to_pipeline([row], settings, set())
        assert r1[0].metadata["learned_at"] == stated.isoformat()
        assert r2[0].metadata["learned_at"] == stated.isoformat()


def _summary(content: str, *, learned_at: datetime, event_date: date) -> FactSummary:
    return FactSummary(
        id=uuid.uuid4(),
        content=content,
        category=None,
        subject="dentist",
        confidence=1.0,
        active=True,
        score=0.9,
        learned_at=learned_at,
        event_date=event_date,
    )


class TestPreTurnRecencyByStatementTime:
    """The pre-turn copy of the resolver (ContextEngine._resolve_recency) follows the same rule."""

    @staticmethod
    def _engine():
        from nous.cognitive.context import ContextEngine

        engine = ContextEngine.__new__(ContextEngine)
        engine._settings = SimpleNamespace(recency_resolver_enabled=True, recency_resolver_similarity_floor=0.55)
        return engine

    def test_newer_correction_with_earlier_event_date_wins(self):
        original = _summary(
            "Dentist appointment is on the 25th",
            learned_at=datetime(2026, 10, 1, 9, 0, tzinfo=UTC),
            event_date=date(2026, 11, 25),
        )
        correction = _summary(
            "Dentist appointment is on the 20th",
            learned_at=datetime(2026, 10, 20, 9, 0, tzinfo=UTC),
            event_date=date(2026, 11, 20),
        )

        self._engine()._resolve_recency([original, correction])

        assert correction.recency_status == "current"
        assert original.recency_status == "superseded"
        assert original.score < correction.score

    def test_same_statement_time_falls_back_to_event_date(self):
        stamp = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
        earlier = _summary("Dentist appointment is on the 20th", learned_at=stamp, event_date=date(2026, 11, 20))
        later = _summary("Dentist appointment is on the 25th", learned_at=stamp, event_date=date(2026, 11, 25))

        self._engine()._resolve_recency([earlier, later])

        assert later.recency_status == "current"
        assert earlier.recency_status == "superseded"


# ---------------------------------------------------------------------------
# F055 records only the rows the formatter rendered
# ---------------------------------------------------------------------------


class TestRecordSurfacedRenderedOnly:
    async def test_rows_the_formatter_dropped_are_not_recorded(self):
        from nous.api.tools import create_nous_tools

        heart = MagicMock()
        heart.recall = AsyncMock(return_value=[])
        activator = MagicMock()
        activator.current_turn = AsyncMock(return_value=0)
        activator.compute_activations = AsyncMock(return_value={})
        activator.record_surfaced = AsyncMock()
        heart._residual_activator = activator
        brain = _brain(neighbors_by_node={})
        settings = _settings(residual_activation_enabled=True)
        fact = PipelineResult(id=FACT_ID, type="fact", description="a fact", score=0.9, source="heart")
        # A brain decision is rendered only when decisions are in scope.
        dec = PipelineResult(
            id=DECISION_ID,
            type="decision",
            description="a decision",
            score=0.7,
            source="brain",
            metadata={"raw_score": 0.7, "category": "tooling", "stakes": "low", "confidence": 0.5},
        )
        tools = create_nous_tools(brain=brain, heart=heart, settings=settings)

        with patch(
            "nous.api.retrieval_pipeline.run_recall_pipeline",
            new=AsyncMock(return_value=([fact, dec], PipelineStats())),
        ):
            await tools["recall_deep"](query="anything", memory_types=["fact"], _session_id="s1")
        await asyncio.sleep(0)

        surfaced = activator.record_surfaced.await_args.kwargs["surfaced"]
        assert [(s[0], s[1]) for s in surfaced] == [(FACT_ID, "fact")]


# ---------------------------------------------------------------------------
# Every model-facing score names its space
# ---------------------------------------------------------------------------


class TestScoreSpaceLabels:
    def test_each_row_kind_carries_its_space(self):
        rows = [
            PipelineResult(id=uuid.uuid4(), type="fact", description="hybrid fact", score=0.9, source="heart"),
            PipelineResult(id=uuid.uuid4(), type="censor", description="a censor", score=0.8, source="heart"),
            PipelineResult(
                id=uuid.uuid4(),
                type="censor",
                description="a pattern censor",
                score=0.7,
                source="heart",
                metadata={"matched_by": "regex"},
            ),
            PipelineResult(id=uuid.uuid4(), type="chunk", description="a chunk", score=0.7, source="heart"),
            PipelineResult(
                id=uuid.uuid4(),
                type="fact",
                description="keyed fact",
                score=0.55,
                source="heart",
                metadata={"retrieval_leg": "keyed"},
            ),
            PipelineResult(
                id=uuid.uuid4(),
                type="fact",
                description="path a fact",
                score=0.4,
                source="graph_expanded",
                edge_relation="related_to",
                metadata={"stage_origin": "heart_graph_memory"},
            ),
            PipelineResult(
                id=uuid.uuid4(),
                type="fact",
                description="spread fact",
                score=0.3,
                source="spreading_activation",
                edge_relation="spreading_activation",
                metadata={"stage_origin": "heart_graph_memory"},
            ),
            PipelineResult(
                id=uuid.uuid4(),
                type="decision",
                description="graph decision",
                score=0.5,
                source="graph_expanded",
                edge_relation="supports",
                metadata={"stage_origin": "heart_graph"},
            ),
            PipelineResult(
                id=uuid.uuid4(),
                type="decision",
                description="brain decision",
                score=0.6,
                source="brain",
                metadata={"raw_score": 0.6, "category": "tooling", "stakes": "low", "confidence": 0.5},
            ),
            PipelineResult(
                id=uuid.uuid4(),
                type="decision",
                description="spread decision",
                score=0.2,
                source="spreading_activation",
                edge_relation="spreading_activation",
                metadata={"stage_origin": "brain_graph"},
            ),
        ]
        text_out = _format_pipeline_text(rows, PipelineStats(), ["all"])

        def line_for(desc: str) -> str:
            return next(line for line in text_out.splitlines() if desc in line)

        assert "score: 0.900 [rrf])" in line_for("hybrid fact")
        assert "score: 0.800 [cosine])" in line_for("a censor")
        assert "score: 0.700 [regex])" in line_for("a pattern censor")
        assert "score: 0.700 [chunk])" in line_for("a chunk")
        assert "score: 0.550 [keyed band])" in line_for("keyed fact")
        assert "score: 0.400 [graph])" in line_for("path a fact")
        assert "score: 0.300 [activation])" in line_for("spread fact")
        assert "score: 0.500 [graph])" in line_for("graph decision")
        assert "score: 0.600 [rrf])" in line_for("brain decision")
        assert "score: 0.200 [activation])" in line_for("spread decision")


class TestContradictionWarnings:
    def test_only_edges_between_two_shown_rows_are_printed(self):
        shown_a, shown_b, unseen = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        rows = [
            PipelineResult(id=shown_a, type="fact", description="fact a", score=0.9, source="heart"),
            PipelineResult(id=shown_b, type="fact", description="fact b", score=0.8, source="heart"),
            # Not rendered: decisions are out of scope for a fact-only recall.
            PipelineResult(
                id=unseen,
                type="decision",
                description="a decision",
                score=0.7,
                source="brain",
                metadata={"raw_score": 0.7, "category": "tooling", "stakes": "low", "confidence": 0.5},
            ),
        ]
        stats = PipelineStats(
            contradiction_edges=[
                (shown_a, "fact", shown_b, "fact"),
                (shown_a, "fact", unseen, "decision"),
            ]
        )

        text_out = _format_pipeline_text(rows, stats, ["fact"])

        warnings = [line for line in text_out.splitlines() if line.startswith("Warning: Contradiction")]
        assert warnings == [
            f"Warning: Contradiction detected between fact({str(shown_a)[:8]}) and fact({str(shown_b)[:8]})"
        ]


# ---------------------------------------------------------------------------
# DB-backed tests
# ---------------------------------------------------------------------------


def _agent(tag: str) -> str:
    return f"recall-sel-{tag}-{uuid.uuid4().hex[:8]}"


async def _purge(db, agent: str) -> None:
    async with db.session() as s:
        await s.execute(delete(GraphEdge).where(GraphEdge.agent_id == agent))
        await s.execute(delete(Fact).where(Fact.agent_id == agent))
        await s.execute(delete(Censor).where(Censor.agent_id == agent))
        await s.execute(text("UPDATE brain.decisions SET superseded_by = NULL WHERE agent_id = :a"), {"a": agent})
        await s.execute(delete(Decision).where(Decision.agent_id == agent))
        await s.execute(delete(Episode).where(Episode.agent_id == agent))
        await s.commit()


def _db_settings(agent: str, **overrides):
    from nous.config import Settings

    base = dict(
        graph_recall_enabled=True,
        contradiction_detection=True,
        recency_resolver_enabled=True,
        spreading_activation_enabled="false",
        keyed_fact_leg_enabled=False,
        heart_graph_all_types_enabled=False,
        date_leg_enabled=False,
        query_expansion_enabled=False,
    )
    base.update(overrides)
    return Settings(agent_id=agent).model_copy(update=base)


async def _two_dated_facts(db, agent, vec, *, old, new, edge_agent=None):
    """Store two same-subject facts and a ``contradicts`` edge from the NEW one to the old one.

    ``old``/``new`` are (content, learned_at, event_date). The contents are
    difflib-dissimilar (ratio < 0.55), so only the edge can pair them. The edge
    row carries ``edge_agent`` (default: the facts' agent).
    """
    old_id, new_id = uuid.uuid4(), uuid.uuid4()
    async with db.session() as s:
        for fid, (content, learned, event) in ((old_id, old), (new_id, new)):
            s.add(
                Fact(
                    id=fid,
                    agent_id=agent,
                    content=content,
                    subject="trip plan",
                    active=True,
                    embedding=vec,
                    learned_at=learned,
                    event_date=event,
                )
            )
        await s.flush()
        s.add(
            GraphEdge(
                source_id=new_id,
                source_type="fact",
                target_id=old_id,
                target_type="fact",
                agent_id=edge_agent or agent,
                relation="contradicts",
                weight=1.0,
            )
        )
        await s.commit()
    return old_id, new_id


@pytest.mark.postgres_only
class TestFactContradictionEdgesReachTheResolver:
    """Stage 5 fetches fact-to-fact ``contradicts`` edges on every scope."""

    @pytest.mark.parametrize("memory_types", [["fact"], None])
    async def test_edge_pairs_two_dissimilar_facts(self, db, mock_embeddings, memory_types):
        from nous.brain.brain import Brain
        from nous.heart import Heart

        agent = _agent("edge-pairs")
        settings = _db_settings(agent)
        heart = Heart(db, settings, embedding_provider=mock_embeddings)
        brain = Brain(database=db, settings=settings)
        query = "zanzibar ferry schedule"
        try:
            # Stated earlier AND dated earlier: both orderings agree, so this
            # test depends only on the edge being fetched.
            old_id, new_id = await _two_dated_facts(
                db,
                agent,
                await mock_embeddings.embed(query),
                old=(
                    "Zanzibar ferry trip planned with the north pier operator for the spring holiday",
                    datetime(2026, 3, 1, tzinfo=UTC),
                    date(2026, 4, 1),
                ),
                new=(
                    "Boat crossing to the island now set for an earlier morning slot",
                    datetime(2026, 3, 20, tzinfo=UTC),
                    date(2026, 4, 10),
                ),
            )

            results, stats = await run_recall_pipeline(query, heart, brain, settings, memory_types=memory_types)

            by_id = {r.id: r for r in results}
            assert stats.contradiction_checks_ran is True
            assert old_id in by_id[new_id].contradicts
            assert by_id[new_id].metadata.get("recency_status") == "current"
            assert by_id[old_id].metadata.get("recency_status") == "superseded"
        finally:
            await _purge(db, agent)
            await heart.close()
            await brain.close()

    async def test_another_agents_edge_is_not_fetched(self, db, mock_embeddings):
        """Stage 5 reads only this agent's edges, even between this agent's ids."""
        from nous.brain.brain import Brain
        from nous.heart import Heart

        agent, other = _agent("edge-own"), _agent("edge-other")
        settings = _db_settings(agent)
        heart = Heart(db, settings, embedding_provider=mock_embeddings)
        brain = Brain(database=db, settings=settings)
        query = "zanzibar ferry schedule"
        try:
            old_id, new_id = await _two_dated_facts(
                db,
                agent,
                await mock_embeddings.embed(query),
                old=(
                    "Zanzibar ferry trip planned with the north pier operator for the spring holiday",
                    datetime(2026, 3, 1, tzinfo=UTC),
                    date(2026, 4, 1),
                ),
                new=(
                    "Boat crossing to the island now set for an earlier morning slot",
                    datetime(2026, 3, 20, tzinfo=UTC),
                    date(2026, 4, 10),
                ),
                edge_agent=other,
            )

            results, stats = await run_recall_pipeline(query, heart, brain, settings, memory_types=["fact"])

            by_id = {r.id: r for r in results}
            assert stats.contradiction_checks_ran is True
            assert by_id[new_id].contradicts == []
            assert stats.contradiction_edges == []
        finally:
            await _purge(db, agent)
            await _purge(db, other)
            await heart.close()
            await brain.close()

    async def test_graph_served_rows_pair_with_a_stage_one_fact(self, db, mock_embeddings):
        """A Path A neighbour and a Stage 2 decision are served rows too, so their edges are read."""
        from nous.brain.brain import Brain
        from nous.heart import Heart

        agent = _agent("stage5-graph")
        settings = _db_settings(
            agent,
            heart_graph_all_types_enabled=True,
            cross_type_linking_enabled=True,
            graph_associative_query_gate_enabled=False,
        )
        heart = Heart(db, settings, embedding_provider=mock_embeddings)
        brain = Brain(database=db, settings=settings)
        query = "zanzibar ferry schedule"
        seed_id, neighbour_id = uuid.uuid4(), uuid.uuid4()
        try:
            async with db.session() as s:
                s.add(
                    Fact(
                        id=seed_id,
                        agent_id=agent,
                        content="Zanzibar ferry schedule for the spring holiday",
                        active=True,
                        embedding=await mock_embeddings.embed(query),
                    )
                )
                # Reachable only through the graph: distant vector, no shared words.
                s.add(
                    Fact(
                        id=neighbour_id,
                        agent_id=agent,
                        content="Unrelated archival footnote filed under miscellany",
                        active=True,
                        embedding=await mock_embeddings.embed("gardening tips for tomatoes"),
                    )
                )
                decision_id = await _decision(s, agent, description="Book the earlier crossing")
                for target_id, target_type in ((neighbour_id, "fact"), (decision_id, "decision")):
                    for relation in ("related_to", "contradicts"):
                        s.add(
                            GraphEdge(
                                source_id=seed_id,
                                source_type="fact",
                                target_id=target_id,
                                target_type=target_type,
                                agent_id=agent,
                                relation=relation,
                                weight=0.9,
                            )
                        )
                await s.commit()

            # limit=1: Stage 1 returns the seed only, so the neighbour can come only from Path A.
            results, _ = await run_recall_pipeline(query, heart, brain, settings, limit=1, memory_types=["fact"])

            by_id = {r.id: r for r in results}
            assert by_id[neighbour_id].metadata.get("stage_origin") == "heart_graph_memory"
            assert by_id[decision_id].metadata.get("stage_origin") == "heart_graph"
            assert set(by_id[seed_id].contradicts) == {neighbour_id, decision_id}
        finally:
            await _purge(db, agent)
            await heart.close()
            await brain.close()

    async def test_correction_stated_later_wins_over_a_later_event_date(self, db, mock_embeddings):
        """End to end: learned_at travels from the row to the resolver."""
        from nous.brain.brain import Brain
        from nous.heart import Heart

        agent = _agent("stated-later")
        settings = _db_settings(agent)
        heart = Heart(db, settings, embedding_provider=mock_embeddings)
        brain = Brain(database=db, settings=settings)
        query = "dentist appointment date"
        try:
            old_id, new_id = await _two_dated_facts(
                db,
                agent,
                await mock_embeddings.embed(query),
                old=(
                    "Dentist appointment booked at the downtown clinic for the 25th",
                    datetime(2026, 10, 1, tzinfo=UTC),
                    date(2026, 11, 25),
                ),
                new=(
                    "Moved it: the tooth checkup is now on the 20th instead",
                    datetime(2026, 10, 20, tzinfo=UTC),
                    date(2026, 11, 20),
                ),
            )

            results, _ = await run_recall_pipeline(query, heart, brain, settings, memory_types=["fact"])

            by_id = {r.id: r for r in results}
            assert by_id[new_id].metadata.get("recency_status") == "current"
            assert by_id[old_id].metadata.get("recency_status") == "superseded"
        finally:
            await _purge(db, agent)
            await heart.close()
            await brain.close()


@pytest.mark.postgres_only
async def test_recall_tracks_access_only_for_the_rows_it_returns(db, mock_embeddings):
    """The fact leg fetches limit*2; only the merge's survivors are tracked."""
    from nous.heart import Heart

    agent = _agent("track-access")
    heart = Heart(db, _db_settings(agent), embedding_provider=mock_embeddings)
    query = "quarterly zebra inventory"
    vec = await mock_embeddings.embed(query)
    try:
        async with db.session() as s:
            for i in range(3):
                s.add(Fact(agent_id=agent, content=f"zebra inventory note {i}", active=True, embedding=vec))
            await s.commit()
        tracked: list[UUID] = []
        heart.facts._fire_track_access = lambda fact_ids: tracked.extend(fact_ids)

        out = await heart.recall(query, limit=1, types=["fact"])

        assert len(out) == 1
        assert tracked == [out[0].id]
    finally:
        await _purge(db, agent)
        await heart.close()


@pytest.mark.postgres_only
async def test_source_text_ignores_another_agents_episode(db, mock_embeddings):
    """Grounding text comes only from this agent's episodes."""
    from nous.heart import Heart
    from nous.heart.schemas import FactInput

    owner, other = _agent("source-own"), _agent("source-other")
    heart = Heart(db, _db_settings(owner), embedding_provider=mock_embeddings)
    episode_id = uuid.uuid4()
    try:
        async with db.session() as s:
            s.add(Episode(id=episode_id, agent_id=other, summary="theirs", transcript="their private transcript"))
            await s.commit()
        async with db.session() as s:
            got = await heart.facts._get_source_text(FactInput(content="x", source_episode_id=episode_id), s)
        assert got is None
    finally:
        await _purge(db, other)
        await heart.close()


async def _decision(s, agent, *, description, confidence=0.8, outcome="pending", superseded_by=None):
    did = uuid.uuid4()
    s.add(
        Decision(
            id=did,
            agent_id=agent,
            description=description,
            confidence=confidence,
            category="tooling",
            stakes="low",
            outcome=outcome,
            superseded_by=superseded_by,
        )
    )
    await s.flush()
    return did


@pytest.mark.postgres_only
async def test_pending_zero_confidence_decision_is_searchable(db):
    """Outcome NULL with confidence 0.0 is not an abandoned decision."""
    from nous.brain.brain import Brain

    agent = _agent("pending-zero")
    brain = Brain(database=db, settings=_db_settings(agent))
    try:
        async with db.session() as s:
            null_id = await _decision(s, agent, description="quokkasaurus rollout plan", confidence=0.0)
            failed_id = await _decision(
                s, agent, description="quokkasaurus abandoned plan", confidence=0.0, outcome="failure"
            )
            await s.execute(text("UPDATE brain.decisions SET outcome = NULL WHERE id = :id"), {"id": null_id})
            await s.commit()

        found = {d.id for d in await brain.query("quokkasaurus", limit=10)}

        assert null_id in found
        assert failed_id not in found
    finally:
        await _purge(db, agent)
        await brain.close()


@pytest.mark.postgres_only
async def test_graph_resolver_keeps_a_pending_zero_confidence_decision(db):
    """The graph-node resolver mirrors brain.query's abandoned-decision predicate, NULL-safe too."""
    from nous.brain.brain import Brain

    agent = _agent("pending-zero-graph")
    brain = Brain(database=db, settings=_db_settings(agent))
    try:
        async with db.session() as s:
            null_id = await _decision(s, agent, description="pending plan", confidence=0.0)
            failed_id = await _decision(s, agent, description="abandoned plan", confidence=0.0, outcome="failure")
            await s.execute(text("UPDATE brain.decisions SET outcome = NULL WHERE id = :id"), {"id": null_id})
            await s.commit()

        async with db.session() as s:
            resolved = await brain._resolve_node_descriptions(s, {"decision": [null_id, failed_id]})

        assert null_id in resolved
        assert failed_id not in resolved
    finally:
        await _purge(db, agent)
        await brain.close()


@pytest.mark.postgres_only
class TestDecisionDelete:
    """Delete acts only on this agent's decision and clears superseded_by referrers."""

    async def test_another_agent_cannot_delete_or_detach(self, db):
        from nous.brain.brain import Brain

        owner, other = _agent("delete-own"), _agent("delete-other")
        brain = Brain(database=db, settings=_db_settings(other))
        fact_id = uuid.uuid4()
        try:
            async with db.session() as s:
                did = await _decision(s, owner, description="owner decision")
                s.add(Fact(id=fact_id, agent_id=owner, content="owner fact", source_decision_id=did))
                await s.commit()

            await brain.delete(did)

            async with db.session() as s:
                assert await s.get(Decision, did) is not None
                assert (await s.get(Fact, fact_id)).source_decision_id == did
        finally:
            await _purge(db, owner)
            await brain.close()

    async def test_delete_clears_superseded_by_referrers(self, db):
        from nous.brain.brain import Brain

        agent = _agent("delete-superseded")
        brain = Brain(database=db, settings=_db_settings(agent))
        try:
            async with db.session() as s:
                new_id = await _decision(s, agent, description="replacement decision")
                old_id = await _decision(s, agent, description="old decision", superseded_by=new_id)
                await s.commit()

            await brain.delete(new_id)

            async with db.session() as s:
                assert await s.get(Decision, new_id) is None
                assert (await s.get(Decision, old_id)).superseded_by is None
        finally:
            await _purge(db, agent)
            await brain.close()


@pytest.mark.postgres_only
class TestCensorSearchAndUpdate:
    async def test_regex_match_is_returned_when_the_embedding_is_distant(self, db, mock_embeddings):
        """The regex leg runs next to the semantic leg, not only when embedding fails.

        A regex-only hit scores the semantic floor (0.7); a censor both legs
        find keeps its cosine.
        """
        from nous.heart import Heart

        agent = _agent("censor-legs")
        heart = Heart(db, _db_settings(agent), embedding_provider=mock_embeddings)
        query = "please run rm -rf /tmp/build now"
        try:
            async with db.session() as s:
                distant = Censor(
                    agent_id=agent,
                    trigger_pattern=r"rm\s+-rf",
                    reason="never wipe directories",
                    embedding=await mock_embeddings.embed("gardening tips for tomatoes"),
                )
                both = Censor(
                    agent_id=agent,
                    trigger_pattern=r"/tmp/build",
                    reason="build dir is shared",
                    embedding=await mock_embeddings.embed(query),
                )
                s.add_all([distant, both])
                await s.commit()
                distant_id, both_id = distant.id, both.id

            matches = {m.id: m for m in await heart.censors.search(query)}

            assert set(matches) == {distant_id, both_id}
            assert (matches[distant_id].matched_by, matches[distant_id].score) == ("regex", 0.7)
            assert matches[both_id].matched_by == "semantic"
            assert matches[both_id].score == pytest.approx(1.0, abs=1e-4)
        finally:
            await _purge(db, agent)
            await heart.close()

    async def test_a_regex_hit_does_not_outrank_a_relevant_fact(self, db, mock_embeddings):
        """In a cross-type recall a pattern hit sits at the semantic floor, below a close fact."""
        from nous.heart import Heart

        agent = _agent("censor-rank")
        heart = Heart(db, _db_settings(agent), embedding_provider=mock_embeddings)
        query = "please run rm -rf on the build directory"
        try:
            async with db.session() as s:
                fact = Fact(
                    agent_id=agent,
                    content="The build directory is cleaned by the nightly job",
                    active=True,
                    embedding=await mock_embeddings.embed(query),
                )
                censor = Censor(
                    agent_id=agent,
                    trigger_pattern=r"rm\s+-rf",
                    reason="never wipe directories",
                    embedding=await mock_embeddings.embed("gardening tips for tomatoes"),
                )
                s.add_all([fact, censor])
                await s.commit()
                fact_id, censor_id = fact.id, censor.id

            out = await heart.recall(query, limit=5, types=["fact", "censor"])

            assert [r.id for r in out] == [fact_id, censor_id]
            assert out[1].score == 0.7
            assert out[1].metadata["matched_by"] == "regex"
        finally:
            await _purge(db, agent)
            await heart.close()

    async def test_reason_update_refreshes_the_embedding(self, db, mock_embeddings):
        """The embedded text is trigger_pattern + reason; a new reason re-embeds."""
        from nous.heart import Heart
        from nous.heart.schemas import CensorInput

        agent = _agent("censor-reembed")
        heart = Heart(db, _db_settings(agent), embedding_provider=mock_embeddings)
        try:
            detail = await heart.censors.add(CensorInput(trigger_pattern="drop table", reason="old reason"))

            await heart.censors.update(detail.id, reason="new reason")

            async with db.session() as s:
                stored = list((await s.get(Censor, detail.id)).embedding)
            expected = await mock_embeddings.embed("drop table new reason")
            assert max(abs(a - b) for a, b in zip(stored, expected, strict=True)) < 1e-5
        finally:
            await _purge(db, agent)
            await heart.close()
