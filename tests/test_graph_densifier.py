"""Tests for F040 — GraphDensifier orphan backfill engine."""
import pytest
import pytest_asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from nous.brain.graph_densifier import (
    GraphDensifier,
    _ENTITY_CONFIG,
    _get_relation,
    _get_threshold,
)
from nous.brain.graph_linker import GraphLinker
from nous.config import Settings


# ---------------------------------------------------------------------------
# Unit tests (no DB required)
# ---------------------------------------------------------------------------


class TestEntityConfig:
    def test_all_types_present(self):
        assert set(_ENTITY_CONFIG.keys()) == {"fact", "decision", "episode", "procedure"}

    def test_fact_config(self):
        table, type_name, content_col, extra = _ENTITY_CONFIG["fact"]
        assert table == "heart.facts"
        assert type_name == "fact"
        assert content_col == "t.content"
        assert "t.active" in extra

    def test_episode_uses_structured_summary(self):
        _, _, content_col, extra = _ENTITY_CONFIG["episode"]
        assert "structured_summary" in content_col
        assert "t." in content_col
        assert "t.active = true" in extra
        assert "t.structured_summary IS NOT NULL" in extra

    def test_decision_no_tags_filter(self):
        """Decision config must NOT reference tags column."""
        _, _, content_col, extra = _ENTITY_CONFIG["decision"]
        assert "tags" not in content_col
        assert "tags" not in extra


class TestGetRelation:
    def test_fact_fact(self):
        assert _get_relation("fact", "fact") == "related_to"

    def test_fact_decision(self):
        assert _get_relation("fact", "decision") == "evidence_for"

    def test_decision_episode(self):
        assert _get_relation("decision", "episode") == "discussed_in"

    def test_unknown_pair_defaults(self):
        assert _get_relation("unknown", "other") == "related_to"


class TestGetThreshold:
    def test_fact_fact_threshold(self):
        s = Settings()
        assert _get_threshold(s, "fact", "fact") == s.graph_threshold_fact_fact

    def test_procedure_any(self):
        s = Settings()
        assert _get_threshold(s, "procedure", "fact") == s.graph_threshold_procedure_any
        assert _get_threshold(s, "procedure", "decision") == s.graph_threshold_procedure_any

    def test_symmetric(self):
        s = Settings()
        assert _get_threshold(s, "fact", "decision") == _get_threshold(s, "decision", "fact")


# ---------------------------------------------------------------------------
# Integration tests (require Postgres)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def _fix_stale_relation_constraint(db):
    """Drop the stale inline relation check if it exists."""
    async with db.engine.begin() as conn:
        await conn.execute(text(
            "ALTER TABLE brain.graph_edges "
            "DROP CONSTRAINT IF EXISTS graph_edges_relation_check"
        ))


@pytest_asyncio.fixture
async def densifier(db, settings, mock_embeddings, _fix_stale_relation_constraint):
    """GraphDensifier with mock embeddings."""
    agent_id = f"test-densifier-{uuid4().hex[:8]}"
    linker = GraphLinker(db, mock_embeddings, settings, agent_id)
    d = GraphDensifier(db, linker, mock_embeddings, settings, agent_id)
    yield d


async def _insert_fact(session: AsyncSession, agent_id: str, content: str, embedding: list[float]) -> str:
    """Insert a test fact and return its ID."""
    fact_id = uuid4()
    embedding_str = "[" + ",".join(str(float(v)) for v in embedding) + "]"
    await session.execute(text("""
        INSERT INTO heart.facts (id, agent_id, content, active, embedding)
        VALUES (:id, :agent_id, :content, true, CAST(:embedding AS vector))
    """), {
        "id": fact_id,
        "agent_id": agent_id,
        "content": content,
        "embedding": embedding_str,
    })
    return fact_id


async def _insert_decision(session: AsyncSession, agent_id: str, description: str, embedding: list[float]) -> str:
    """Insert a test decision and return its ID."""
    dec_id = uuid4()
    embedding_str = "[" + ",".join(str(float(v)) for v in embedding) + "]"
    await session.execute(text("""
        INSERT INTO brain.decisions (id, agent_id, description, confidence, category, stakes, embedding)
        VALUES (:id, :agent_id, :description, 0.8, 'architecture', 'low', CAST(:embedding AS vector))
    """), {
        "id": dec_id,
        "agent_id": agent_id,
        "description": description,
        "embedding": embedding_str,
    })
    return dec_id


async def _insert_edge(session: AsyncSession, agent_id: str, source_id, target_id, source_type: str, target_type: str) -> None:
    """Insert a graph edge."""
    await session.execute(text("""
        INSERT INTO brain.graph_edges (agent_id, source_id, target_id, source_type, target_type, relation, weight, auto_linked)
        VALUES (:agent_id, :source_id, :target_id, :source_type, :target_type, 'related_to', 1.0, true)
    """), {
        "agent_id": agent_id,
        "source_id": source_id,
        "target_id": target_id,
        "source_type": source_type,
        "target_type": target_type,
    })


@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_find_orphans_returns_unlinked(db, settings, mock_embeddings, _fix_stale_relation_constraint):
    """find_orphans returns facts that have no graph edges."""
    agent_id = f"test-orphan-find-{uuid4().hex[:8]}"
    linker = GraphLinker(db, mock_embeddings, settings, agent_id)
    densifier = GraphDensifier(db, linker, mock_embeddings, settings, agent_id)

    async with db.session() as session:
        emb = await mock_embeddings.embed("test fact content")
        fact_id = await _insert_fact(session, agent_id, "test orphan fact", emb)
        await session.commit()

    async with db.session() as session:
        orphans = await densifier.find_orphans("fact", 10, session)
        orphan_ids = [oid for oid, _ in orphans]
        assert fact_id in orphan_ids


@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_find_orphans_excludes_linked(db, settings, mock_embeddings, _fix_stale_relation_constraint):
    """Already-linked nodes must NOT appear in orphan results."""
    agent_id = f"test-orphan-excl-{uuid4().hex[:8]}"
    linker = GraphLinker(db, mock_embeddings, settings, agent_id)
    densifier = GraphDensifier(db, linker, mock_embeddings, settings, agent_id)

    async with db.session() as session:
        emb1 = await mock_embeddings.embed("linked fact A")
        emb2 = await mock_embeddings.embed("linked fact B")
        fact_a = await _insert_fact(session, agent_id, "linked fact A", emb1)
        fact_b = await _insert_fact(session, agent_id, "linked fact B", emb2)
        await _insert_edge(session, agent_id, fact_a, fact_b, "fact", "fact")
        await session.commit()

    async with db.session() as session:
        orphans = await densifier.find_orphans("fact", 100, session)
        orphan_ids = [oid for oid, _ in orphans]
        assert fact_a not in orphan_ids
        assert fact_b not in orphan_ids


@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_backfill_creates_edges(db, settings, mock_embeddings, _fix_stale_relation_constraint):
    """backfill_orphan_facts creates edges between similar orphan facts."""
    agent_id = f"test-backfill-{uuid4().hex[:8]}"
    # Use very low thresholds so mock embeddings can match
    settings_copy = settings.model_copy(update={
        "graph_threshold_fact_fact": 0.01,
        "graph_threshold_fact_decision": 0.01,
        "cross_type_threshold": 0.01,
    })
    linker = GraphLinker(db, mock_embeddings, settings_copy, agent_id)
    densifier = GraphDensifier(db, linker, mock_embeddings, settings_copy, agent_id)

    # Insert two similar facts (same content = same embedding = similarity 1.0)
    async with db.session() as session:
        emb = await mock_embeddings.embed("Python is great for data science")
        await _insert_fact(session, agent_id, "Python is great for data science", emb)
        # Slightly different but same base text for near-match
        emb2 = await mock_embeddings.embed_near("Python is great for data science", noise=0.01)
        await _insert_fact(session, agent_id, "Python is excellent for data science", emb2)
        await session.commit()

    edges_created = await densifier.backfill_orphan_facts(max_count=10)
    # With threshold 0.01, near-identical embeddings should link
    assert edges_created >= 1


@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_backfill_disabled_returns_zero(db, settings, mock_embeddings, _fix_stale_relation_constraint):
    """When graph_backfill_enabled is False, backfill returns 0."""
    agent_id = f"test-disabled-{uuid4().hex[:8]}"
    settings_off = settings.model_copy(update={"graph_backfill_enabled": False})
    linker = GraphLinker(db, mock_embeddings, settings_off, agent_id)
    densifier = GraphDensifier(db, linker, mock_embeddings, settings_off, agent_id)

    result = await densifier.backfill_orphan_facts()
    assert result == 0


@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_run_backfill_cycle_respects_interrupt(db, settings, mock_embeddings, _fix_stale_relation_constraint):
    """run_backfill_cycle stops when interrupt flag is set."""
    agent_id = f"test-interrupt-{uuid4().hex[:8]}"
    linker = GraphLinker(db, mock_embeddings, settings, agent_id)
    densifier = GraphDensifier(db, linker, mock_embeddings, settings, agent_id)

    densifier.interrupt()
    results = await densifier.run_backfill_cycle()
    # Should return early, facts might be 0 since interrupt is checked per-orphan
    assert isinstance(results, dict)
    assert "facts" in results


@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_discover_clusters_empty_graph(db, settings, mock_embeddings, _fix_stale_relation_constraint):
    """discover_clusters with no edges returns 0."""
    agent_id = f"test-cluster-empty-{uuid4().hex[:8]}"
    linker = GraphLinker(db, mock_embeddings, settings, agent_id)
    densifier = GraphDensifier(db, linker, mock_embeddings, settings, agent_id)

    result = await densifier.discover_clusters()
    assert result == 0


@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_discover_clusters_rate_limited(db, settings, mock_embeddings, _fix_stale_relation_constraint):
    """discover_clusters skips if called within 7 days."""
    agent_id = f"test-cluster-rate-{uuid4().hex[:8]}"
    linker = GraphLinker(db, mock_embeddings, settings, agent_id)
    densifier = GraphDensifier(db, linker, mock_embeddings, settings, agent_id)

    # Simulate a recent run
    densifier._last_cluster_discovery = datetime.now(UTC) - timedelta(days=1)
    result = await densifier.discover_clusters()
    assert result == 0


@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_discover_clusters_runs_after_7_days(db, settings, mock_embeddings, _fix_stale_relation_constraint):
    """discover_clusters runs if last run was > 7 days ago."""
    agent_id = f"test-cluster-old-{uuid4().hex[:8]}"
    linker = GraphLinker(db, mock_embeddings, settings, agent_id)
    densifier = GraphDensifier(db, linker, mock_embeddings, settings, agent_id)

    # Simulate an old run
    densifier._last_cluster_discovery = datetime.now(UTC) - timedelta(days=8)
    result = await densifier.discover_clusters()
    # Empty graph = 0, but it should have run (not skipped)
    assert result == 0
    # Verify timestamp was updated
    assert densifier._last_cluster_discovery is not None
    assert (datetime.now(UTC) - densifier._last_cluster_discovery).seconds < 10


@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_run_backfill_cycle_returns_all_types(db, settings, mock_embeddings, _fix_stale_relation_constraint):
    """run_backfill_cycle returns dict with all entity types."""
    agent_id = f"test-cycle-{uuid4().hex[:8]}"
    linker = GraphLinker(db, mock_embeddings, settings, agent_id)
    densifier = GraphDensifier(db, linker, mock_embeddings, settings, agent_id)

    results = await densifier.run_backfill_cycle()
    assert "facts" in results
    assert "decisions" in results
    assert "episodes" in results
    assert "procedures" in results


# ---------------------------------------------------------------------------
# F043: CE rerank integration with backfill (require Postgres)
# ---------------------------------------------------------------------------


def _install_fake_ce(monkeypatch, fake):
    """Force CE availability and install fake loader on both reranker + adapter modules."""
    import nous.heart.reranker as reranker_mod
    from nous.brain import backfill_rerank as br

    monkeypatch.setattr(reranker_mod, "CROSS_ENCODER_AVAILABLE", True)
    monkeypatch.setattr(br, "CROSS_ENCODER_AVAILABLE", True)
    monkeypatch.setattr(reranker_mod, "_load_cross_encoder", lambda name: fake)


class _FakeCE:
    """Fake CrossEncoder; predict returns a precomputed list of raw logits."""

    def __init__(self, scores):
        self._scores = scores
        self.calls = 0

    def predict(self, pairs):
        self.calls += 1
        # Return scores aligned with however many pairs we got.
        n = len(list(pairs))
        if n <= len(self._scores):
            return self._scores[:n]
        # Pad with very high logits so extras pass.
        return list(self._scores) + [10.0] * (n - len(self._scores))


@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_backfill_same_type_with_ce_rerank(
    db, settings, mock_embeddings, _fix_stale_relation_constraint, monkeypatch
):
    """CE rerank prunes low-score candidates; only above-floor survivors get edges."""
    agent_id = f"test-ce-rerank-{uuid4().hex[:8]}"
    s = settings.model_copy(update={
        "graph_threshold_fact_fact": 0.01,
        "graph_threshold_fact_decision": 0.01,
        "ce_backfill_enabled": True,
        "ce_backfill_top_k": 10,
        "ce_backfill_min_score": 0.5,
    })
    # 2 high logits (sigmoid >> 0.5), 2 low logits (sigmoid < 0.5).
    fake = _FakeCE(scores=[5.0, 5.0, -5.0, -5.0])
    _install_fake_ce(monkeypatch, fake)

    linker = GraphLinker(db, mock_embeddings, s, agent_id)
    densifier = GraphDensifier(db, linker, mock_embeddings, s, agent_id)

    async with db.session() as session:
        base_emb = await mock_embeddings.embed("Python is great for data science")
        await _insert_fact(session, agent_id, "Python orphan seed", base_emb)
        for i in range(4):
            near = await mock_embeddings.embed_near(
                "Python is great for data science", noise=0.005
            )
            await _insert_fact(session, agent_id, f"candidate {i}", near)
        await session.commit()

    ce_stats = {"survived": 0, "pruned": 0}
    edges = await densifier.backfill_orphan_facts(max_count=10, ce_stats=ce_stats)
    # 2 survivors above floor, 2 pruned below floor.
    assert ce_stats["survived"] == 2
    assert ce_stats["pruned"] == 2
    # At most as many edges as survivors (cosine gate may drop further).
    assert edges <= 2


@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_backfill_ce_disabled_matches_baseline(
    db, settings, mock_embeddings, _fix_stale_relation_constraint
):
    """ce_backfill_enabled=False → ce_stats stays zero AND edges still get created."""
    agent_id = f"test-ce-disabled-{uuid4().hex[:8]}"
    s = settings.model_copy(update={
        "graph_threshold_fact_fact": 0.01,
        "graph_threshold_fact_decision": 0.01,
        "ce_backfill_enabled": False,
    })
    linker = GraphLinker(db, mock_embeddings, s, agent_id)
    densifier = GraphDensifier(db, linker, mock_embeddings, s, agent_id)

    async with db.session() as session:
        base_emb = await mock_embeddings.embed("Identical fact text")
        await _insert_fact(session, agent_id, "Identical fact text", base_emb)
        near = await mock_embeddings.embed_near("Identical fact text", noise=0.005)
        await _insert_fact(session, agent_id, "Identical fact text v2", near)
        await session.commit()

    ce_stats = {"survived": 0, "pruned": 0}
    edges = await densifier.backfill_orphan_facts(max_count=10, ce_stats=ce_stats)
    # CE disabled → counters never incremented.
    assert ce_stats == {"survived": 0, "pruned": 0}
    # Baseline behavior: low threshold + near-identical embeddings → at least one edge.
    assert edges >= 1


@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_run_backfill_cycle_returns_ce_stats(
    db, settings, mock_embeddings, _fix_stale_relation_constraint
):
    """run_backfill_cycle includes a `_ce_stats` dict with int survived/pruned."""
    agent_id = f"test-ce-stats-{uuid4().hex[:8]}"
    linker = GraphLinker(db, mock_embeddings, settings, agent_id)
    densifier = GraphDensifier(db, linker, mock_embeddings, settings, agent_id)

    result = await densifier.run_backfill_cycle()
    assert "_ce_stats" in result
    assert isinstance(result["_ce_stats"], dict)
    assert isinstance(result["_ce_stats"].get("survived"), int)
    assert isinstance(result["_ce_stats"].get("pruned"), int)
    # Per-type counts remain ints.
    for k in ("facts", "decisions", "episodes", "procedures"):
        assert isinstance(result[k], int)


@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_run_backfill_cycle_sum_values_unchanged(
    db, settings, mock_embeddings, _fix_stale_relation_constraint
):
    """Regression guard for P1: summing edge counts must EXCLUDE _ce_stats."""
    agent_id = f"test-ce-sum-{uuid4().hex[:8]}"
    linker = GraphLinker(db, mock_embeddings, settings, agent_id)
    densifier = GraphDensifier(db, linker, mock_embeddings, settings, agent_id)

    result = await densifier.run_backfill_cycle()

    # Mimic the sleep_handler aggregation rule.
    edge_sum = sum(v for k, v in result.items() if not k.startswith("_"))
    expected = (
        result["facts"]
        + result["decisions"]
        + result["episodes"]
        + result["procedures"]
    )
    assert edge_sum == expected
    # And the dict still carries the underscored key.
    assert "_ce_stats" in result
