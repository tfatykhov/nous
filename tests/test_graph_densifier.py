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
        # F070 (2026-05-25): 'chunk' added for chunk-aware sleep consolidation.
        assert set(_ENTITY_CONFIG.keys()) == {"fact", "decision", "episode", "procedure", "chunk"}

    def test_fact_config(self):
        table, type_name, content_col, extra = _ENTITY_CONFIG["fact"]
        assert table == "heart.facts"
        assert type_name == "fact"
        assert content_col == "t.content"
        assert "t.active" in extra

    def test_episode_uses_structured_summary_with_fallback(self):
        """F058: F040 was filtering out 100% of stuck-open episodes
        because structured_summary was NULL (set only by
        episode_summarizer on episode_ended). The COALESCE fallback
        unblocks them; the IS NOT NULL filter is dropped so episodes
        with only the plain `summary` field are now F040-eligible."""
        _, _, content_col, extra = _ENTITY_CONFIG["episode"]
        # Both structured_summary AND plain summary must appear in the
        # content extractor (COALESCE fallback)
        assert "structured_summary" in content_col
        assert "COALESCE" in content_col
        assert "t.summary" in content_col
        assert "t." in content_col
        # Active filter still required
        assert "t.active = true" in extra
        # The IS NOT NULL filter MUST be gone (that was the bug)
        assert "structured_summary IS NOT NULL" not in extra, (
            "F058 dropped the structured_summary filter; if it's back, "
            "F040 will silently exclude stuck-open episodes again "
            "(76/76 prod orphans had this problem pre-F058)."
        )

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


async def _insert_episode(session: AsyncSession, agent_id: str, summary: str) -> str:
    """F070 test helper: insert an episode and return its ID."""
    ep_id = uuid4()
    await session.execute(text("""
        INSERT INTO heart.episodes (id, agent_id, summary, active, started_at)
        VALUES (:id, :agent_id, :summary, true, NOW())
    """), {
        "id": ep_id, "agent_id": agent_id, "summary": summary,
    })
    return ep_id


async def _insert_chunk(
    session: AsyncSession, agent_id: str,
    episode_id, chunk_index: int, content: str, embedding: list[float],
) -> str:
    """F070 test helper: insert an episode_chunks row and return its ID."""
    chunk_id = uuid4()
    embedding_str = "[" + ",".join(str(float(v)) for v in embedding) + "]"
    await session.execute(text("""
        INSERT INTO heart.episode_chunks
            (id, agent_id, episode_id, chunk_index, content, embedding)
        VALUES
            (:id, :agent_id, :ep_id, :idx, :content, CAST(:embedding AS vector))
    """), {
        "id": chunk_id, "agent_id": agent_id, "ep_id": episode_id,
        "idx": chunk_index, "content": content, "embedding": embedding_str,
    })
    return chunk_id


async def _insert_fact_with_episode(
    session: AsyncSession, agent_id: str,
    content: str, embedding: list[float], source_episode_id,
    *, active: bool = True,
) -> str:
    """F070 test helper: insert a fact with source_episode_id set."""
    fact_id = uuid4()
    embedding_str = "[" + ",".join(str(float(v)) for v in embedding) + "]"
    await session.execute(text("""
        INSERT INTO heart.facts
            (id, agent_id, content, active, embedding, source_episode_id)
        VALUES (:id, :agent_id, :content, :active, CAST(:emb AS vector), :ep_id)
    """), {
        "id": fact_id, "agent_id": agent_id, "content": content,
        "active": active,
        "emb": embedding_str, "ep_id": source_episode_id,
    })
    return fact_id


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
async def test_find_orphans_episode_with_only_plain_summary_is_returned(
    db, settings, mock_embeddings, _fix_stale_relation_constraint,
):
    """F058 regression: episodes with NULL structured_summary but a
    populated `summary` field MUST be returned by find_orphans.

    Pre-F058 the entity-config filter was
    ``t.active = true AND t.structured_summary IS NOT NULL`` which
    silently excluded 100% of stuck-open prod episodes (76/76 in the
    eval-scratch snapshot, identical shape to prod nous-default).
    The fix dropped the structured_summary filter and added a
    COALESCE(structured_summary->>'summary', summary) content fallback
    so F040 can densify these orphans.
    """
    from datetime import UTC, datetime
    agent_id = f"test-f058-{uuid4().hex[:8]}"
    linker = GraphLinker(db, mock_embeddings, settings, agent_id)
    densifier = GraphDensifier(db, linker, mock_embeddings, settings, agent_id)

    plain_only_ep = uuid4()
    structured_ep = uuid4()
    emb1 = await mock_embeddings.embed("plain summary only")
    emb2 = await mock_embeddings.embed("with structured summary")
    emb1_str = "[" + ",".join(str(float(v)) for v in emb1) + "]"
    emb2_str = "[" + ",".join(str(float(v)) for v in emb2) + "]"

    async with db.session() as session:
        # Episode with plain summary only — pre-F058 was excluded
        await session.execute(text("""
            INSERT INTO heart.episodes
              (id, agent_id, summary, structured_summary, started_at,
               active, tags, embedding)
            VALUES (:id, :aid, :s, NULL, :t, true, '{}',
                    CAST(:emb AS vector))
        """), {
            "id": plain_only_ep, "aid": agent_id,
            "s": "stuck-open episode with no structured_summary",
            "t": datetime.now(UTC), "emb": emb1_str,
        })
        # Episode with structured_summary populated (pre-F058 also
        # included; ensure F058 still includes it)
        await session.execute(text("""
            INSERT INTO heart.episodes
              (id, agent_id, summary, structured_summary, started_at,
               active, tags, embedding)
            VALUES (:id, :aid, :s, CAST(:ss AS jsonb), :t, true, '{}',
                    CAST(:emb AS vector))
        """), {
            "id": structured_ep, "aid": agent_id,
            "s": "fallback summary text", "ss": '{"summary": "structured-version"}',
            "t": datetime.now(UTC), "emb": emb2_str,
        })
        await session.commit()

    try:
        async with db.session() as session:
            orphans = await densifier.find_orphans("episode", 50, session)
            ids_to_content = {oid: content for oid, content in orphans}

        # Both episodes must be returned — F058 fix
        assert plain_only_ep in ids_to_content, (
            "plain-summary-only episode missing from find_orphans → "
            "F058 COALESCE/filter regression"
        )
        assert structured_ep in ids_to_content

        # Content extractor must return the right text per episode:
        # - plain_only_ep: COALESCE picks `summary` (structured is NULL)
        # - structured_ep: COALESCE picks `structured_summary->>'summary'`
        assert ids_to_content[plain_only_ep] == \
            "stuck-open episode with no structured_summary"
        assert ids_to_content[structured_ep] == "structured-version"
    finally:
        async with db.session() as cs:
            await cs.execute(text(
                "DELETE FROM heart.episodes WHERE agent_id=:aid"
            ), {"aid": agent_id})
            await cs.commit()


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
        # F045: fixture uses short synthetic content ("candidate 0"...) that
        # would be dropped by the default 80-char guard. Disable it here so
        # the F043 test semantics are preserved.
        "ce_backfill_min_content_chars": 0,
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
    # max_count=1: process exactly one orphan so ce_stats reflects a single CE call.
    # With max_count>1, every near-duplicate becomes its own orphan and ce_stats
    # accumulates across all of them (5 orphans × 2 survivors = 10, not 2).
    edges = await densifier.backfill_orphan_facts(max_count=1, ce_stats=ce_stats)
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


# ---------------------------------------------------------------------------
# F045: CE-aware threshold dispatch
# ---------------------------------------------------------------------------


def test_get_threshold_ce_mode(settings):
    """When ce_backfill_enabled=True, _get_threshold returns the relaxed CE-mode values."""
    from nous.brain.graph_densifier import _get_threshold

    s = settings.model_copy(update={"ce_backfill_enabled": True})

    cases = [
        (("fact", "fact"), s.ce_backfill_threshold_fact_fact),
        (("fact", "decision"), s.ce_backfill_threshold_fact_decision),
        (("decision", "fact"), s.ce_backfill_threshold_fact_decision),  # order-agnostic
        (("fact", "episode"), s.ce_backfill_threshold_fact_episode),
        (("decision", "decision"), s.ce_backfill_threshold_decision_decision),
        (("episode", "episode"), s.ce_backfill_threshold_episode_episode),
        (("fact", "procedure"), s.ce_backfill_threshold_procedure_any),
        (("procedure", "procedure"), s.ce_backfill_threshold_procedure_any),
    ]
    for (a, b), expected in cases:
        got = _get_threshold(s, a, b)
        assert got == expected, (
            f"CE-mode threshold for ({a},{b}) should be {expected}, got {got}"
        )


def test_get_threshold_strict_mode(settings):
    """When ce_backfill_enabled=False, _get_threshold returns the existing strict values.

    Regression guard: the F045 split must not change any pre-existing strict threshold.
    """
    from nous.brain.graph_densifier import _get_threshold

    s = settings.model_copy(update={"ce_backfill_enabled": False})

    cases = [
        (("fact", "fact"), s.graph_threshold_fact_fact),
        (("fact", "decision"), s.graph_threshold_fact_decision),
        (("decision", "fact"), s.graph_threshold_fact_decision),  # order-agnostic
        (("fact", "episode"), s.graph_threshold_fact_episode),
        (("decision", "decision"), s.graph_threshold_decision_decision),
        (("episode", "episode"), s.graph_threshold_episode_episode),
        (("fact", "procedure"), s.graph_threshold_procedure_any),
        (("procedure", "procedure"), s.graph_threshold_procedure_any),
    ]
    for (a, b), expected in cases:
        got = _get_threshold(s, a, b)
        assert got == expected, (
            f"strict threshold for ({a},{b}) should be {expected}, got {got}"
        )


def test_get_threshold_default_flag_off(settings):
    """Out-of-box settings have ce_backfill_enabled=False → strict thresholds."""
    from nous.brain.graph_densifier import _get_threshold

    # Untouched settings — default must route to strict mode.
    assert _get_threshold(settings, "fact", "fact") == settings.graph_threshold_fact_fact


@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_backfill_uses_ce_mode_threshold_end_to_end(
    db, settings, mock_embeddings, _fix_stale_relation_constraint, monkeypatch
):
    """F045 P2-3: end-to-end proof that CE-mode dispatch reaches _backfill_same_type.

    We set the strict fact-fact threshold to 0.99 (impossible) and the CE-mode
    fact-fact threshold to 0.01 (always passes). With ``ce_backfill_enabled=True``,
    ``_get_threshold`` must route to the CE-mode value — which is the ONLY way
    edges can form. If a future refactor ever bypasses the helper and reads the
    strict setting directly, this test produces 0 edges and fails loudly.
    """
    agent_id = f"test-f045-dispatch-{uuid4().hex[:8]}"
    s = settings.model_copy(update={
        # Strict defaults turned WAY up — unreachable if dispatch is broken.
        "graph_threshold_fact_fact": 0.99,
        "graph_threshold_fact_decision": 0.99,
        # CE-mode defaults turned WAY down — always passes.
        "ce_backfill_threshold_fact_fact": 0.01,
        "ce_backfill_threshold_fact_decision": 0.01,
        "ce_backfill_enabled": True,
        "ce_backfill_top_k": 10,
        "ce_backfill_min_score": 0.1,
        # Disable content guard so short test-fact content flows through.
        "ce_backfill_min_content_chars": 0,
    })

    # Fake CE returns high raw logits for all candidates → sigmoid ≈ 0.99.
    fake = _FakeCE(scores=[5.0, 5.0, 5.0, 5.0])
    _install_fake_ce(monkeypatch, fake)

    linker = GraphLinker(db, mock_embeddings, s, agent_id)
    densifier = GraphDensifier(db, linker, mock_embeddings, s, agent_id)

    async with db.session() as session:
        base_emb = await mock_embeddings.embed("F045 wiring seed")
        await _insert_fact(session, agent_id, "F045 wiring seed fact text", base_emb)
        for i in range(2):
            near = await mock_embeddings.embed_near("F045 wiring seed", noise=0.005)
            await _insert_fact(
                session, agent_id, f"F045 wiring candidate {i}", near,
            )
        await session.commit()

    ce_stats = {"survived": 0, "pruned": 0}
    edges = await densifier.backfill_orphan_facts(max_count=1, ce_stats=ce_stats)

    # CE survived (sigmoid(5.0) > 0.1 floor) and dispatched to the 0.01 CE-mode
    # gate, so at least one edge should have formed. If dispatch were broken,
    # the strict 0.99 floor would have blocked everything.
    assert ce_stats["survived"] >= 1, (
        f"F045: CE should have kept >=1 candidate (fake scores 5.0, "
        f"sigmoid~0.99, min_score=0.1) — got ce_stats={ce_stats}"
    )
    assert edges >= 1, (
        f"F045: CE-mode threshold dispatch is broken. With "
        f"ce_backfill_enabled=True, strict fact_fact=0.99 is unreachable and "
        f"CE-mode fact_fact=0.01 should always pass. Got {edges} edges, "
        f"ce_stats={ce_stats}."
    )


# ============================================================================
# F070 — Chunk-aware sleep consolidation
# ============================================================================


@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_chunk_consolidation_disabled_skips(
    db, settings, mock_embeddings, _fix_stale_relation_constraint,
):
    """F070: master flag off → backfill returns 0, no edges created."""
    agent_id = f"test-chunk-disabled-{uuid4().hex[:8]}"
    settings_local = settings.model_copy(update={
        "chunk_consolidation_enabled": False,
        "graph_backfill_enabled": True,
    })
    linker = GraphLinker(db, mock_embeddings, settings_local, agent_id)
    d = GraphDensifier(db, linker, mock_embeddings, settings_local, agent_id)

    async with db.session() as s:
        ep = await _insert_episode(s, agent_id, "test")
        emb = await mock_embeddings.embed("chunk content")
        await _insert_chunk(s, agent_id, ep, 0, "chunk content", emb)
        await s.commit()

    edges = await d.backfill_orphan_chunks()
    assert edges == 0


@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_chunk_to_episode_edge_created(
    db, settings, mock_embeddings, _fix_stale_relation_constraint,
):
    """F070: orphan chunk gets a chunk→episode 'part_of' edge."""
    agent_id = f"test-chunk-ep-{uuid4().hex[:8]}"
    settings_local = settings.model_copy(update={
        "chunk_consolidation_enabled": True,
        "graph_backfill_enabled": True,
        "graph_backfill_max_chunks": 10,
    })
    linker = GraphLinker(db, mock_embeddings, settings_local, agent_id)
    d = GraphDensifier(db, linker, mock_embeddings, settings_local, agent_id)

    async with db.session() as s:
        ep = await _insert_episode(s, agent_id, "test")
        emb = await mock_embeddings.embed("chunk content here is long enough to be real")
        chunk_id = await _insert_chunk(s, agent_id, ep, 0, "chunk content here is long enough to be real", emb)
        await s.commit()

    edges_created = await d.backfill_orphan_chunks()
    assert edges_created >= 1

    async with db.session() as s:
        rows = (await s.execute(text(
            "SELECT source_id::text, target_id::text, source_type, target_type, relation "
            "FROM brain.graph_edges WHERE agent_id = :a"
        ), {"a": agent_id})).all()
    # Should have a chunk→episode edge
    chunk_ep = [r for r in rows
                if r.source_type == "chunk" and r.target_type == "episode"]
    assert len(chunk_ep) == 1
    assert chunk_ep[0].source_id == str(chunk_id)
    assert chunk_ep[0].target_id == str(ep)
    assert chunk_ep[0].relation == "part_of"


@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_chunk_to_fact_same_episode_links(
    db, settings, mock_embeddings, _fix_stale_relation_constraint,
):
    """F070: chunk gets chunk→fact edge to facts with same source_episode_id
    where cosine ≥ threshold. Cross-episode facts must NOT be linked."""
    agent_id = f"test-chunk-fact-{uuid4().hex[:8]}"
    settings_local = settings.model_copy(update={
        "chunk_consolidation_enabled": True,
        "graph_backfill_enabled": True,
        "graph_threshold_chunk_fact": 0.5,
    })
    linker = GraphLinker(db, mock_embeddings, settings_local, agent_id)
    d = GraphDensifier(db, linker, mock_embeddings, settings_local, agent_id)

    async with db.session() as s:
        ep_a = await _insert_episode(s, agent_id, "ep A")
        ep_b = await _insert_episode(s, agent_id, "ep B")
        # Same embedding for chunk and same-episode fact (high cosine)
        ident_emb = await mock_embeddings.embed("user likes dark roast coffee")
        chunk_a = await _insert_chunk(
            s, agent_id, ep_a, 0,
            "user likes dark roast coffee in this snippet",
            ident_emb,
        )
        # Same-episode fact (should link)
        same_ep_fact = await _insert_fact_with_episode(
            s, agent_id, "user likes dark roast coffee", ident_emb, ep_a,
        )
        # Cross-episode fact (must NOT link even with same embedding)
        cross_ep_fact = await _insert_fact_with_episode(
            s, agent_id, "user likes dark roast coffee", ident_emb, ep_b,
        )
        await s.commit()

    await d.backfill_orphan_chunks()

    async with db.session() as s:
        rows = (await s.execute(text(
            "SELECT target_id::text FROM brain.graph_edges "
            "WHERE agent_id = :a AND source_type = 'chunk' "
            "  AND target_type = 'fact' AND source_id = :c "
        ), {"a": agent_id, "c": chunk_a})).all()
    fact_targets = {r.target_id for r in rows}
    assert str(same_ep_fact) in fact_targets, "same-episode fact should link"
    assert str(cross_ep_fact) not in fact_targets, "cross-episode fact must NOT link"


@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_chunk_to_fact_skips_inactive_facts(
    db, settings, mock_embeddings, _fix_stale_relation_constraint,
):
    """F070 (codex P2): inactive facts (active=false, e.g. superseded) must
    NOT be linked even if they meet the cosine threshold."""
    agent_id = f"test-chunk-inactive-{uuid4().hex[:8]}"
    settings_local = settings.model_copy(update={
        "chunk_consolidation_enabled": True,
        "graph_backfill_enabled": True,
        "graph_threshold_chunk_fact": 0.5,
    })
    linker = GraphLinker(db, mock_embeddings, settings_local, agent_id)
    d = GraphDensifier(db, linker, mock_embeddings, settings_local, agent_id)

    async with db.session() as s:
        ep = await _insert_episode(s, agent_id, "ep")
        ident_emb = await mock_embeddings.embed("user likes dark roast coffee")
        chunk_id = await _insert_chunk(
            s, agent_id, ep, 0,
            "user likes dark roast coffee in this snippet", ident_emb,
        )
        active_fact = await _insert_fact_with_episode(
            s, agent_id, "user likes dark roast coffee", ident_emb, ep,
            active=True,
        )
        superseded_fact = await _insert_fact_with_episode(
            s, agent_id, "user likes dark roast coffee", ident_emb, ep,
            active=False,
        )
        await s.commit()

    await d.backfill_orphan_chunks()

    async with db.session() as s:
        rows = (await s.execute(text(
            "SELECT target_id::text FROM brain.graph_edges "
            "WHERE agent_id = :a AND source_type = 'chunk' "
            "  AND target_type = 'fact' AND source_id = :c "
        ), {"a": agent_id, "c": chunk_id})).all()
    fact_targets = {r.target_id for r in rows}
    assert str(active_fact) in fact_targets, "active fact should link"
    assert str(superseded_fact) not in fact_targets, (
        "inactive (superseded) fact must NOT link"
    )


@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_chunk_intra_episode_sequential_always_linked(
    db, settings, mock_embeddings, _fix_stale_relation_constraint,
):
    """F070: adjacent chunks (chunk_index ± 1) always get linked at weight=1.0
    regardless of cosine similarity."""
    agent_id = f"test-chunk-seq-{uuid4().hex[:8]}"
    settings_local = settings.model_copy(update={
        "chunk_consolidation_enabled": True,
        "graph_backfill_enabled": True,
        # Very high threshold so cosine alone won't link adjacent chunks
        "graph_threshold_chunk_chunk_intra": 0.99,
    })
    linker = GraphLinker(db, mock_embeddings, settings_local, agent_id)
    d = GraphDensifier(db, linker, mock_embeddings, settings_local, agent_id)

    async with db.session() as s:
        ep = await _insert_episode(s, agent_id, "test")
        # Different embeddings — would NOT pass cosine threshold
        emb_a = await mock_embeddings.embed("apple")
        emb_b = await mock_embeddings.embed("zebra")
        c0 = await _insert_chunk(s, agent_id, ep, 0, "apple chunk content here", emb_a)
        c1 = await _insert_chunk(s, agent_id, ep, 1, "zebra chunk content here", emb_b)
        await s.commit()

    await d.backfill_orphan_chunks()

    async with db.session() as s:
        rows = (await s.execute(text(
            "SELECT source_id::text, target_id::text, weight "
            "FROM brain.graph_edges "
            "WHERE agent_id = :a AND source_type = 'chunk' AND target_type = 'chunk'"
        ), {"a": agent_id})).all()
    # Expect at least one chunk↔chunk edge between c0 and c1 (sequential)
    pair = {(str(c0), str(c1)), (str(c1), str(c0))}
    found = [(r.source_id, r.target_id, r.weight) for r in rows]
    has_pair = any((s_id, t_id) in pair for s_id, t_id, _ in found)
    assert has_pair, f"sequential adjacent chunks must link, got {found}"


@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_run_backfill_cycle_includes_chunks_key(
    db, settings, mock_embeddings, _fix_stale_relation_constraint,
):
    """F070: run_backfill_cycle results dict includes a 'chunks' entry."""
    agent_id = f"test-cycle-chunks-{uuid4().hex[:8]}"
    settings_local = settings.model_copy(update={
        "graph_backfill_enabled": True,
        "chunk_consolidation_enabled": True,
    })
    linker = GraphLinker(db, mock_embeddings, settings_local, agent_id)
    d = GraphDensifier(db, linker, mock_embeddings, settings_local, agent_id)

    result = await d.run_backfill_cycle()
    assert "chunks" in result
    # _ce_stats prefix-underscored — must not be confused with per-type entries
    assert "chunks" in {k for k in result if not k.startswith("_")}
