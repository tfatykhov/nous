"""Integration tests for Brain public API.

All tests use real Postgres via the SAVEPOINT fixture from conftest.py.
Brain methods receive the test session via the session parameter (P1-2).
"""

import uuid

import pytest
import pytest_asyncio
from sqlalchemy import select, text

from nous.brain.brain import Brain
from nous.brain.schemas import (
    CalibrationReport,
    DecisionDetail,
    DecisionSummary,
    GraphEdgeInfo,
    GuardrailResult,
    ReasonInput,
    RecordInput,
    ThoughtInfo,
)
from nous.storage.models import Event, GraphEdge

# Must match conftest.GUARDRAIL_TEST_AGENT
GUARDRAIL_TEST_AGENT = "test-guardrail-agent"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def brain(db, settings):
    """Brain without embeddings (keyword-only mode)."""
    brain = Brain(database=db, settings=settings)
    yield brain
    await brain.close()


@pytest_asyncio.fixture
async def brain_with_embeddings(db, settings, mock_embeddings):
    """Brain with mock embedding provider for vector tests."""
    brain = Brain(database=db, settings=settings, embedding_provider=mock_embeddings)
    yield brain
    await brain.close()


@pytest_asyncio.fixture
async def brain_guardrail(db, settings):
    """Brain with agent_id matching seed_guardrails fixture."""
    # Override agent_id to match the test guardrails
    settings_copy = settings.model_copy(update={"agent_id": GUARDRAIL_TEST_AGENT})
    brain = Brain(database=db, settings=settings_copy)
    yield brain
    await brain.close()


def _record_input(**overrides) -> RecordInput:
    """Build a RecordInput with sensible defaults, overridable."""
    defaults = dict(
        description="Use PostgreSQL for storage",
        confidence=0.85,
        category="architecture",
        stakes="medium",
        context="Evaluating database options for the project",
        pattern="Prefer mature, well-supported databases",
        tags=["postgres", "database"],
        reasons=[
            ReasonInput(type="analysis", text="Analyzed the trade-offs"),
            ReasonInput(type="pattern", text="Follows established patterns"),
        ],
    )
    defaults.update(overrides)
    return RecordInput(**defaults)


# ---------------------------------------------------------------------------
# 1. test_record_decision
# ---------------------------------------------------------------------------


async def test_record_decision(brain, session):
    """Record with all fields, verify stored correctly.

    F058: stored `confidence` is calibrated (raw * factor); raw value
    survives in `confidence_raw`.
    """
    inp = _record_input()
    detail = await brain.record(inp, session=session)

    assert isinstance(detail, DecisionDetail)
    assert detail.description == inp.description
    # F058: confidence is calibrated; verify it equals raw * factor.
    expected = inp.confidence * brain.settings.confidence_calibration_factor
    assert abs(detail.confidence - expected) < 1e-9
    assert detail.category == inp.category
    assert detail.stakes == inp.stakes
    assert detail.context == inp.context
    assert detail.pattern == inp.pattern
    assert detail.outcome == "pending"
    assert detail.agent_id == brain.agent_id
    assert detail.created_at is not None
    assert detail.updated_at is not None


# ---------------------------------------------------------------------------
# 2. test_record_with_tags_and_reasons
# ---------------------------------------------------------------------------


async def test_record_with_tags_and_reasons(brain, session):
    """Verify tags and reasons cascade-inserted."""
    inp = _record_input()
    detail = await brain.record(inp, session=session)

    assert set(detail.tags) == {"postgres", "database"}
    assert len(detail.reasons) == 2
    reason_types = {r.type for r in detail.reasons}
    assert reason_types == {"analysis", "pattern"}


# ---------------------------------------------------------------------------
# 3. test_record_computes_quality_score
# ---------------------------------------------------------------------------


async def test_record_computes_quality_score(brain, session):
    """Quality score > 0.5 with tags+reasons+pattern."""
    inp = _record_input()
    detail = await brain.record(inp, session=session)

    assert detail.quality_score is not None
    assert detail.quality_score > 0.5
    # tags(0.25) + reasons(0.25) + pattern(0.25) + context(0.15) + diversity(0.10) = 1.0
    assert detail.quality_score == 1.0


# ---------------------------------------------------------------------------
# 4. test_record_generates_bridge
# ---------------------------------------------------------------------------


async def test_record_generates_bridge(brain, session):
    """Bridge auto-extracted from description/pattern."""
    inp = _record_input()
    detail = await brain.record(inp, session=session)

    assert detail.bridge is not None
    assert detail.bridge.structure is not None
    assert len(detail.bridge.structure) > 0
    # Function should be pattern when pattern is set
    assert detail.bridge.function == inp.pattern


# ---------------------------------------------------------------------------
# 5. test_record_auto_links
# ---------------------------------------------------------------------------


async def test_record_auto_links(brain_with_embeddings, session):
    """Record two similar decisions, verify graph edge created."""
    inp1 = _record_input(description="Use PostgreSQL for the database layer")
    detail1 = await brain_with_embeddings.record(inp1, session=session)

    inp2 = _record_input(description="Use PostgreSQL for persistent storage")
    detail2 = await brain_with_embeddings.record(inp2, session=session)

    # Check if any edges were created between the two decisions
    result = await session.execute(
        text(
            "SELECT * FROM brain.graph_edges "
            "WHERE (source_id = :id1 AND target_id = :id2) "
            "   OR (source_id = :id2 AND target_id = :id1)"
        ),
        {"id1": str(detail1.id), "id2": str(detail2.id)},
    )
    edges = result.fetchall()
    # auto_link might or might not create an edge depending on threshold
    # but we verify the mechanism doesn't error
    assert isinstance(edges, list)


@pytest.mark.postgres_only
async def test_auto_link_uses_existing_constraint_not_bogus_name(
    brain, session,
):
    """Regression: Brain._auto_link used to reference
    ``constraint="uq_edges_src_tgt_rel"`` which doesn't exist. Every call
    raised ``UndefinedObject`` and was silently swallowed by record()'s
    ``except Exception``. The prior test_record_auto_links assertion
    (``isinstance(edges, list)``) was too lenient — it passed even when
    auto_link 100% no-op'd. This test directly invokes ``auto_link``
    with two near-identical embeddings and asserts that an edge IS
    inserted, which proves the ON CONFLICT clause references a real
    constraint.
    """
    from uuid import uuid4

    from nous.storage.models import Decision

    # Two decisions with identical embeddings — cosine similarity = 1.0,
    # easily clears auto_link_threshold (default 0.85).
    emb = [1.0] + [0.0] * 1535
    emb_str = "[" + ",".join(str(float(v)) for v in emb) + "]"
    d1_id = uuid4()
    d2_id = uuid4()
    for did in (d1_id, d2_id):
        session.add(Decision(
            id=did, agent_id=brain.agent_id,
            description=f"decision {did}",
            confidence=0.8, category="architecture", stakes="low",
        ))
    await session.flush()
    # Backfill embeddings via raw SQL (model field is JSON in some test
    # configs; this guarantees the pgvector column is set).
    await session.execute(text(
        "UPDATE brain.decisions SET embedding = CAST(:e AS vector) "
        "WHERE id IN (:a, :b)"
    ), {"e": emb_str, "a": d1_id, "b": d2_id})
    await session.flush()

    # Call auto_link directly — if the constraint name is wrong this
    # raises UndefinedObject and the test fails.
    edges = await brain.auto_link(d1_id, threshold=0.5, session=session)

    # And verify the edge actually persisted.
    rows = (await session.execute(text(
        "SELECT source_id::text, target_id::text, relation "
        "FROM brain.graph_edges "
        "WHERE agent_id = :a AND relation = 'related_to' "
        "  AND ((source_id = :d1 AND target_id = :d2) "
        "    OR (source_id = :d2 AND target_id = :d1))"
    ), {"a": brain.agent_id, "d1": d1_id, "d2": d2_id})).all()
    assert len(rows) >= 1, (
        "auto_link must persist at least one edge between two cosine=1.0 "
        f"decisions; got {rows}. Returned edges from auto_link: {edges}"
    )


# ---------------------------------------------------------------------------
# 6. test_think
# ---------------------------------------------------------------------------


async def test_think(brain, session):
    """Attach thought to decision, verify retrieval."""
    inp = _record_input()
    detail = await brain.record(inp, session=session)

    thought = await brain.think(detail.id, "Considered alternative approaches", session=session)
    assert isinstance(thought, ThoughtInfo)
    assert thought.text == "Considered alternative approaches"
    assert thought.created_at is not None


# ---------------------------------------------------------------------------
# 7. test_get_decision
# ---------------------------------------------------------------------------


async def test_get_decision(brain, session):
    """Fetch with all relations populated."""
    inp = _record_input()
    detail = await brain.record(inp, session=session)
    await brain.think(detail.id, "A thought", session=session)

    # Expire the cached decision so get() re-fetches with eager loading
    await session.flush()
    session.expire_all()

    fetched = await brain.get(detail.id, session=session)
    assert fetched is not None
    assert fetched.id == detail.id
    assert fetched.description == inp.description
    assert set(fetched.tags) == {"postgres", "database"}
    assert len(fetched.reasons) == 2
    assert fetched.bridge is not None
    assert len(fetched.thoughts) == 1
    assert fetched.thoughts[0].text == "A thought"


# ---------------------------------------------------------------------------
# 8. test_get_nonexistent
# ---------------------------------------------------------------------------


async def test_get_nonexistent(brain, session):
    """Returns None for nonexistent decision."""
    result = await brain.get(uuid.uuid4(), session=session)
    assert result is None


# ---------------------------------------------------------------------------
# 9. test_update_decision
# ---------------------------------------------------------------------------


async def test_update_decision(brain, session):
    """Update description, verify re-scored."""
    inp = _record_input()
    detail = await brain.record(inp, session=session)
    original_id = detail.id

    updated = await brain.update(
        detail.id,
        description="Updated: Use PostgreSQL 17 for storage",
        session=session,
    )
    assert updated.id == original_id
    assert "Updated" in updated.description


# ---------------------------------------------------------------------------
# 10. test_query_keyword_only
# ---------------------------------------------------------------------------


async def test_query_keyword_only(brain, session):
    """Search without embeddings (keyword fallback)."""
    await brain.record(
        _record_input(description="Use PostgreSQL for persistent storage"),
        session=session,
    )
    await brain.record(
        _record_input(description="Implement Redis caching layer", pattern="Cache hot data"),
        session=session,
    )

    results = await brain.query("PostgreSQL", session=session)
    assert isinstance(results, list)
    # Should find the PostgreSQL decision via keyword search
    if results:
        assert any("PostgreSQL" in r.description for r in results)


# ---------------------------------------------------------------------------
# 11. test_query_hybrid
# ---------------------------------------------------------------------------


async def test_query_hybrid(brain_with_embeddings, session):
    """Search with mock embeddings (both vector + keyword)."""
    await brain_with_embeddings.record(
        _record_input(description="Use PostgreSQL for persistent storage"),
        session=session,
    )
    await brain_with_embeddings.record(
        _record_input(description="Implement Redis caching layer", pattern="Cache hot data"),
        session=session,
    )

    results = await brain_with_embeddings.query("PostgreSQL storage", session=session)
    assert isinstance(results, list)
    # Hybrid search should return results
    for r in results:
        assert isinstance(r, DecisionSummary)
        assert r.score is not None


# ---------------------------------------------------------------------------
# 12. test_query_with_filters
# ---------------------------------------------------------------------------


async def test_query_with_filters(brain, session):
    """Filter by category, stakes, outcome."""
    await brain.record(
        _record_input(
            description="Architecture decision about storage",
            category="architecture",
            stakes="high",
        ),
        session=session,
    )
    await brain.record(
        _record_input(
            description="Tooling decision about linting",
            category="tooling",
            stakes="low",
        ),
        session=session,
    )

    # Filter by category
    results = await brain.query("decision", category="architecture", session=session)
    assert isinstance(results, list)
    for r in results:
        assert r.category == "architecture"


# ---------------------------------------------------------------------------
# 13. test_check_guardrails_allowed
# ---------------------------------------------------------------------------


async def test_check_guardrails_allowed(brain_guardrail, session, seed_guardrails):
    """Low stakes, high confidence -> allowed."""
    result = await brain_guardrail.check(
        description="Simple tooling change",
        stakes="low",
        confidence=0.9,
        reasons=[{"type": "analysis", "text": "Straightforward change"}],
        quality_score=0.8,
        session=session,
    )
    assert isinstance(result, GuardrailResult)
    assert result.allowed


# ---------------------------------------------------------------------------
# 14. test_check_guardrails_blocked
# ---------------------------------------------------------------------------


async def test_check_guardrails_blocked(brain_guardrail, session, seed_guardrails):
    """High stakes, low confidence -> blocked by seed guardrail."""
    result = await brain_guardrail.check(
        description="Major production change",
        stakes="high",
        confidence=0.3,
        reasons=[{"type": "intuition", "text": "Gut feeling"}],
        quality_score=0.8,
        session=session,
    )
    assert not result.allowed
    assert "no-high-stakes-low-confidence" in result.blocked_by


# ---------------------------------------------------------------------------
# 15. test_review_decision
# ---------------------------------------------------------------------------


async def test_review_decision(brain, session):
    """Set outcome, verify reviewed_at set."""
    inp = _record_input()
    detail = await brain.record(inp, session=session)

    reviewed = await brain.review(detail.id, "success", result="Worked perfectly", session=session)
    assert reviewed.outcome == "success"
    assert reviewed.outcome_result == "Worked perfectly"
    assert reviewed.reviewed_at is not None


# ---------------------------------------------------------------------------
# 16. test_calibration_report
# ---------------------------------------------------------------------------


async def test_calibration_report(brain, session):
    """Record 5 decisions, review 3, verify Brier score."""
    # Record 5 decisions
    decisions = []
    for i in range(5):
        d = await brain.record(
            _record_input(
                description=f"Calibration test decision {i}",
                confidence=0.8,
            ),
            session=session,
        )
        decisions.append(d)

    # Review 3 of them
    await brain.review(decisions[0].id, "success", session=session)
    await brain.review(decisions[1].id, "success", session=session)
    await brain.review(decisions[2].id, "failure", session=session)

    report = await brain.get_calibration(session=session)
    assert isinstance(report, CalibrationReport)
    assert report.total_decisions >= 5
    assert report.reviewed_decisions >= 3
    assert report.brier_score is not None
    assert report.accuracy is not None


# ---------------------------------------------------------------------------
# 17. test_link_decisions
# ---------------------------------------------------------------------------


async def test_link_decisions(brain, session):
    """Manual link, verify edge created."""
    d1 = await brain.record(
        _record_input(description="First decision"),
        session=session,
    )
    d2 = await brain.record(
        _record_input(description="Second decision"),
        session=session,
    )

    edge = await brain.link(d1.id, d2.id, "supports", weight=0.9, session=session)
    assert isinstance(edge, GraphEdgeInfo)
    assert edge.relation == "supports"
    assert edge.weight == 0.9
    assert edge.auto_linked is False


# ---------------------------------------------------------------------------
# 18. test_neighbors
# ---------------------------------------------------------------------------


async def test_neighbors(brain, session):
    """Link 3 decisions, query neighbors of middle one."""
    d1 = await brain.record(
        _record_input(description="Decision A"),
        session=session,
    )
    d2 = await brain.record(
        _record_input(description="Decision B"),
        session=session,
    )
    d3 = await brain.record(
        _record_input(description="Decision C"),
        session=session,
    )

    await brain.link(d1.id, d2.id, "supports", session=session)
    await brain.link(d2.id, d3.id, "related_to", session=session)

    neighbors = await brain.neighbors(d2.id, session=session)
    assert isinstance(neighbors, list)
    assert len(neighbors) >= 2
    neighbor_ids = {n.id for n in neighbors}
    assert d1.id in neighbor_ids
    assert d3.id in neighbor_ids
    for n in neighbors:
        assert n.node_type == "decision"
        assert n.edge_relation in ("supports", "related_to")


# ---------------------------------------------------------------------------
# 18a. test_neighbors_neighbor_type_filter
# ---------------------------------------------------------------------------


async def test_neighbors_neighbor_type_filter(brain, session):
    """F070 fix: ``neighbor_type`` pushes the filter into SQL so a small
    ``limit`` returns rows of the requested type, not rows of arbitrary
    types that a downstream Python filter would discard.

    Pre-fix: querying a fact with 5 chunk neighbors + 1 decision neighbor
    at ``limit=2`` could return 2 chunks → caller's Python ``decision``
    filter discarded both → 0 decisions surfaced even though one exists.
    Post-fix: ``neighbor_type="decision"`` returns the 1 decision.
    """
    # Build a fact with mixed-type neighbors.
    # We insert raw GraphEdge rows so we don't need Heart/chunks fixtures.
    from uuid import uuid4

    from nous.storage.models import GraphEdge

    fact_id = uuid4()
    decision = await brain.record(_record_input(description="real decision"), session=session)

    # 5 chunk neighbors via summarized_by (the F070 case that crowds decisions).
    chunk_ids = [uuid4() for _ in range(5)]
    for cid in chunk_ids:
        session.add(GraphEdge(
            source_id=cid, target_id=fact_id,
            source_type="chunk", target_type="fact",
            agent_id=brain.agent_id, relation="summarized_by",
            weight=0.8, auto_linked=True,
            extraction_method="inferred",
        ))
    # 1 decision neighbor via informed_by.
    session.add(GraphEdge(
        source_id=fact_id, target_id=decision.id,
        source_type="fact", target_type="decision",
        agent_id=brain.agent_id, relation="informed_by",
        weight=0.9, auto_linked=True,
        extraction_method="heuristic",
    ))
    await session.flush()

    # Without filter, limit=2 over 6 edges can return any mix.
    # WITH filter, limit=2 must return only decisions (we have 1).
    neighbors = await brain.neighbors(
        fact_id, node_type="fact", limit=2, session=session,
        neighbor_type="decision",
    )
    assert len(neighbors) == 1, (
        f"neighbor_type='decision' must return only the decision row, "
        f"got {[(n.node_type, n.id) for n in neighbors]}"
    )
    assert neighbors[0].node_type == "decision"
    assert neighbors[0].id == decision.id


async def test_neighbors_dedupes_multi_edge_neighbor(brain, session):
    """F080 (codex P1): a neighbor connected via multiple edges (e.g. informed_by
    from the linker + related_to from the densifier) must collapse to ONE row
    (max weight) BEFORE the fan-out cap, so duplicate edges don't consume slots
    and crowd out other distinct neighbors.
    """
    from uuid import uuid4

    from nous.storage.models import GraphEdge

    fact_id = uuid4()
    d = await brain.record(_record_input(description="decision D"), session=session)
    e = await brain.record(_record_input(description="decision E"), session=session)

    # Two edges fact->D (same neighbor, different relations) + one edge fact->E.
    session.add(GraphEdge(
        source_id=fact_id, target_id=d.id, source_type="fact", target_type="decision",
        agent_id=brain.agent_id, relation="informed_by", weight=0.9,
        auto_linked=True, extraction_method="heuristic",
    ))
    session.add(GraphEdge(
        source_id=fact_id, target_id=d.id, source_type="fact", target_type="decision",
        agent_id=brain.agent_id, relation="related_to", weight=0.7,
        auto_linked=True, extraction_method="inferred",
    ))
    session.add(GraphEdge(
        source_id=fact_id, target_id=e.id, source_type="fact", target_type="decision",
        agent_id=brain.agent_id, relation="informed_by", weight=0.8,
        auto_linked=True, extraction_method="heuristic",
    ))
    await session.flush()

    neighbors = await brain.neighbors(
        fact_id, node_type="fact", limit=2, session=session, neighbor_type="decision",
    )
    ids = [n.id for n in neighbors]
    assert d.id in ids and e.id in ids  # both distinct neighbors survive the cap
    assert ids.count(d.id) == 1  # D collapsed to one row (the 0.9 edge)
    assert len(neighbors) == 2


async def test_neighbors_excludes_inactive_fact(brain, session):
    """2026-06-13 audit: a supersedes (or any) edge to an inactive/superseded
    fact must not surface it as a fact neighbor — otherwise the supersedes-edge
    backfill resurfaces obsolete facts via Path A expansion. Mirrors the F080
    procedure filter."""
    from uuid import uuid4

    from nous.storage.models import Fact, GraphEdge

    seed_id, active_id, inactive_id = uuid4(), uuid4(), uuid4()
    session.add(Fact(id=seed_id, agent_id=brain.agent_id, content="seed fact", active=True))
    session.add(Fact(id=active_id, agent_id=brain.agent_id, content="active neighbor", active=True))
    session.add(Fact(id=inactive_id, agent_id=brain.agent_id,
                     content="superseded neighbor", active=False, superseded_by=active_id))
    # seed --related_to--> active ; seed --supersedes--> inactive
    session.add(GraphEdge(
        source_id=seed_id, target_id=active_id, source_type="fact", target_type="fact",
        agent_id=brain.agent_id, relation="related_to", weight=0.8,
        auto_linked=True, extraction_method="heuristic",
    ))
    session.add(GraphEdge(
        source_id=seed_id, target_id=inactive_id, source_type="fact", target_type="fact",
        agent_id=brain.agent_id, relation="supersedes", weight=1.0,
        auto_linked=True, extraction_method="deterministic",
    ))
    await session.flush()

    neighbors = await brain.neighbors(
        seed_id, node_type="fact", limit=10, session=session, neighbor_type="fact",
    )
    ids = [n.id for n in neighbors]
    assert active_id in ids
    assert inactive_id not in ids  # superseded/inactive fact filtered out


async def test_neighbors_excludes_lineage_and_negative_relations(brain, session):
    """2b: the unfiltered neighbor fan-out must never surface supersedes (lineage)
    or contradicts (negative) edges as connectivity; co_occurred IS legitimate
    associative connectivity for retrieval and is kept. An explicit relation=
    request is still honoured verbatim (not exercised here)."""
    from uuid import uuid4

    from nous.storage.models import Fact, GraphEdge

    seed, related, cooc, contra = uuid4(), uuid4(), uuid4(), uuid4()
    for fid, name in [(seed, "seed"), (related, "related nb"), (cooc, "cooc nb"), (contra, "contra nb")]:
        session.add(Fact(id=fid, agent_id=brain.agent_id, content=f"{name} fact", active=True))
    def _edge(t, rel, method):
        session.add(GraphEdge(
            source_id=seed, target_id=t, source_type="fact", target_type="fact",
            agent_id=brain.agent_id, relation=rel, weight=1.0, auto_linked=True,
            extraction_method=method,
        ))
    _edge(related, "related_to", "heuristic")
    _edge(cooc, "co_occurred", "co_occurrence")
    _edge(contra, "contradicts", "inferred")
    await session.flush()

    ids = [n.id for n in await brain.neighbors(
        seed, node_type="fact", limit=10, session=session, neighbor_type="fact")]
    assert related in ids
    assert cooc in ids               # co_occurred kept (associative for retrieval)
    assert contra not in ids         # contradicts excluded


# ---------------------------------------------------------------------------
# 18a-Path-A. test_neighbors_resolves_content_for_all_node_types
# ---------------------------------------------------------------------------


async def test_neighbors_resolves_content_for_all_node_types(brain, session):
    """Path A: brain._neighbors must resolve real description text for
    fact / episode / chunk / procedure / decision neighbors, not the
    pre-Path-A ``f'[{ntype}] {uuid}'`` placeholder. Without this, every
    chunk-edge consumer would receive useless placeholder strings."""
    from uuid import uuid4

    from nous.storage.models import (
        Episode, EpisodeChunk, Fact, GraphEdge, Procedure,
    )

    seed = await brain.record(
        _record_input(description="Seed decision text"), session=session,
    )

    # Build one of each non-decision node type with known content.
    fact_id = uuid4()
    session.add(Fact(
        id=fact_id, agent_id=brain.agent_id, content="FACT_CONTENT_TOKEN",
        active=True,
    ))
    ep_id = uuid4()
    session.add(Episode(
        id=ep_id, agent_id=brain.agent_id, summary="EPISODE_SUMMARY_TOKEN",
        active=True,
    ))
    chunk_id = uuid4()
    session.add(EpisodeChunk(
        id=chunk_id, agent_id=brain.agent_id, episode_id=ep_id,
        chunk_index=0, content="CHUNK_CONTENT_TOKEN",
    ))
    proc_id = uuid4()
    session.add(Procedure(
        id=proc_id, agent_id=brain.agent_id, name="proc",
        description="PROCEDURE_DESC_TOKEN", active=True,
    ))
    # Link seed decision to each via graph_edges.
    for tgt_id, tgt_type, rel in [
        (fact_id, "fact", "informed_by"),
        (ep_id, "episode", "discussed_in"),
        (chunk_id, "chunk", "summarized_by"),
        (proc_id, "procedure", "related_to"),
    ]:
        session.add(GraphEdge(
            source_id=seed.id, target_id=tgt_id,
            source_type="decision", target_type=tgt_type,
            agent_id=brain.agent_id, relation=rel,
            weight=0.8, auto_linked=True, extraction_method="heuristic",
        ))
    await session.flush()

    neighbors = await brain.neighbors(
        seed.id, node_type="decision", limit=10, session=session,
    )
    by_type = {n.node_type: n.description for n in neighbors}

    assert by_type.get("fact") == "FACT_CONTENT_TOKEN", (
        f"fact content unresolved: {by_type.get('fact')!r}"
    )
    assert by_type.get("episode") == "EPISODE_SUMMARY_TOKEN", (
        f"episode summary unresolved: {by_type.get('episode')!r}"
    )
    assert by_type.get("chunk") == "CHUNK_CONTENT_TOKEN", (
        f"chunk content unresolved: {by_type.get('chunk')!r}"
    )
    assert by_type.get("procedure") == "PROCEDURE_DESC_TOKEN", (
        f"procedure description unresolved: {by_type.get('procedure')!r}"
    )
    # Tighter assertion: every resolved description must NOT be the
    # placeholder format ``[<ntype>] <uuid>``. Pre-Path-A code emitted that
    # for all non-decision types; this regression-tests against reverting.
    for n in neighbors:
        assert not (n.description or "").startswith(f"[{n.node_type}]"), (
            f"placeholder leaked for {n.node_type}: {n.description!r}"
        )


# ---------------------------------------------------------------------------
# 18a-Path-A.2. test_neighbors_falls_back_to_placeholder_for_empty_content
# ---------------------------------------------------------------------------


async def test_neighbors_falls_back_to_placeholder_for_empty_content(
    brain, session, caplog,
):
    """Path A defensive: when a resolved row has empty/NULL content (the
    NOT-NULL bypass / data corruption case), _neighbors falls back to the
    ``[<ntype>] <uuid>`` placeholder AND logs WARNING — silent passthrough
    would let a useless candidate land in rerank with no operator signal."""
    import logging
    from uuid import uuid4

    from nous.storage.models import Episode, Fact, GraphEdge

    seed = await brain.record(
        _record_input(description="seed"), session=session,
    )
    # Fact with empty content (simulates NOT-NULL bypass via empty string).
    bad_fact_id = uuid4()
    session.add(Fact(
        id=bad_fact_id, agent_id=brain.agent_id, content="", active=True,
    ))
    # Episode with empty summary.
    bad_ep_id = uuid4()
    session.add(Episode(
        id=bad_ep_id, agent_id=brain.agent_id, summary="", active=True,
    ))
    for tgt_id, tgt_type in [(bad_fact_id, "fact"), (bad_ep_id, "episode")]:
        session.add(GraphEdge(
            source_id=seed.id, target_id=tgt_id,
            source_type="decision", target_type=tgt_type,
            agent_id=brain.agent_id, relation="related_to",
            weight=0.8, auto_linked=True, extraction_method="heuristic",
        ))
    await session.flush()

    with caplog.at_level(logging.WARNING, logger="nous.brain.brain"):
        neighbors = await brain.neighbors(
            seed.id, node_type="decision", limit=10, session=session,
        )

    by_id = {n.id: n for n in neighbors}
    # Both fall back to placeholder rather than empty string.
    assert by_id[bad_fact_id].description == f"[fact] {bad_fact_id}"
    assert by_id[bad_ep_id].description == f"[episode] {bad_ep_id}"
    # And both emit a WARN (one per offending row).
    warns = [r for r in caplog.records if r.levelname == "WARNING"]
    warn_texts = "\n".join(r.getMessage() for r in warns)
    assert "fact" in warn_texts and str(bad_fact_id) in warn_texts, (
        f"expected WARN for empty fact, got: {warn_texts!r}"
    )
    assert "episode" in warn_texts and str(bad_ep_id) in warn_texts, (
        f"expected WARN for empty episode, got: {warn_texts!r}"
    )


# ---------------------------------------------------------------------------
# 18b. test_link_with_types
# ---------------------------------------------------------------------------


async def test_link_with_types(brain, session):
    """Graph edges include source_type, target_type, and agent_id."""
    d1 = await brain.record(_record_input(description="Typed link A"), session=session)
    d2 = await brain.record(_record_input(description="Typed link B"), session=session)

    edge = await brain.link(d1.id, d2.id, "supports", session=session)
    assert edge.source_type == "decision"
    assert edge.target_type == "decision"

    # Verify agent_id persisted in DB
    result = await session.execute(
        select(GraphEdge).where(GraphEdge.source_id == d1.id)
    )
    db_edge = result.scalar_one()
    assert db_edge.agent_id == brain.agent_id
    assert db_edge.source_type == "decision"


# ---------------------------------------------------------------------------
# 18c. test_neighbors_returns_neighbor_result
# ---------------------------------------------------------------------------


async def test_neighbors_returns_neighbor_result(brain, session):
    """neighbors() returns NeighborResult with edge metadata."""
    d1 = await brain.record(_record_input(description="Neighbor result A"), session=session)
    d2 = await brain.record(_record_input(description="Neighbor result B"), session=session)

    await brain.link(d1.id, d2.id, "supports", weight=0.9, session=session)

    results = await brain.neighbors(d1.id, session=session)
    assert len(results) == 1
    n = results[0]
    from nous.brain.schemas import NeighborResult

    assert isinstance(n, NeighborResult)
    assert n.id == d2.id
    assert n.node_type == "decision"
    assert n.edge_relation == "supports"
    assert n.edge_weight == 0.9


# ---------------------------------------------------------------------------
# 18d. test_neighbors_with_node_type
# ---------------------------------------------------------------------------


async def test_neighbors_with_node_type(brain, session):
    """neighbors() accepts node_type parameter for cross-type traversal."""
    from uuid import uuid4

    d1 = await brain.record(_record_input(description="Cross-type neighbor"), session=session)

    fake_fact_id = uuid4()
    edge = GraphEdge(
        source_id=fake_fact_id,
        target_id=d1.id,
        source_type="fact",
        target_type="decision",
        agent_id=brain.agent_id,
        relation="supports",
        weight=0.85,
    )
    session.add(edge)
    await session.flush()

    results = await brain.neighbors(fake_fact_id, node_type="fact", session=session)
    assert len(results) == 1
    assert results[0].id == d1.id
    assert results[0].node_type == "decision"
    assert results[0].edge_relation == "supports"


# ---------------------------------------------------------------------------
# 19. test_emit_event
# ---------------------------------------------------------------------------


async def test_emit_event(brain, session):
    """Verify event written to nous_system.events."""
    await brain.emit_event(
        "test_event",
        {"key": "value"},
        session=session,
    )

    result = await session.execute(
        select(Event).where(
            Event.agent_id == brain.agent_id,
            Event.event_type == "test_event",
        )
    )
    event = result.scalar_one()
    assert event.data == {"key": "value"}


# ---------------------------------------------------------------------------
# Negative tests (P3 fix)
# ---------------------------------------------------------------------------


async def test_get_nonexistent_returns_none(brain, session):
    """get() with nonexistent UUID returns None, not an error."""
    result = await brain.get(uuid.uuid4(), session=session)
    assert result is None


async def test_review_nonexistent_decision(brain, session):
    """review() with nonexistent decision_id raises or returns gracefully."""
    with pytest.raises(Exception):
        await brain.review(uuid.uuid4(), "success", session=session)


async def test_think_nonexistent_decision(brain, session):
    """think() with nonexistent decision_id raises due to FK constraint."""
    with pytest.raises(Exception):
        await brain.think(uuid.uuid4(), "orphan thought", session=session)
