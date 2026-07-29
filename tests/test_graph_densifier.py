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


class TestEpisodeOrphanEligibility:
    """2026-07-12: closed episodes (active=false, ended_at set) must be
    orphan-eligible so F040 can heal the layer F053 wrongly pruned;
    trivial discards and abandoned episodes must stay excluded."""

    def test_entity_config_episode_uses_liveness_predicate(self):
        _, _, _, extra_where = _ENTITY_CONFIG["episode"]
        assert "ended_at IS NOT NULL" in extra_where
        assert "IS DISTINCT FROM 'abandoned'" in extra_where
        assert extra_where.count("t.active = true") == 1

    def test_entity_config_fact_and_procedure_unchanged(self):
        assert _ENTITY_CONFIG["fact"][3] == "t.active = true"
        assert _ENTITY_CONFIG["procedure"][3] == "t.active = true"


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
    episode_id, chunk_index: int, content: str,
    embedding: list[float] | None,
) -> str:
    """F070 test helper: insert an episode_chunks row and return its ID.

    Pass ``embedding=None`` to model the real-world case where embed
    generation failed (column is nullable).
    """
    chunk_id = uuid4()
    if embedding is None:
        await session.execute(text("""
            INSERT INTO heart.episode_chunks
                (id, agent_id, episode_id, chunk_index, content, embedding)
            VALUES
                (:id, :agent_id, :ep_id, :idx, :content, NULL)
        """), {
            "id": chunk_id, "agent_id": agent_id, "ep_id": episode_id,
            "idx": chunk_index, "content": content,
        })
    else:
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


async def _insert_dated_fact(
    session: AsyncSession, agent_id: str, content: str,
    embedding: list[float], event_date, source_episode_id,
) -> str:
    """Insert an active fact with embedding + event_date + source_episode_id."""
    fact_id = uuid4()
    embedding_str = "[" + ",".join(str(float(v)) for v in embedding) + "]"
    await session.execute(text("""
        INSERT INTO heart.facts
            (id, agent_id, content, active, embedding, event_date, source_episode_id)
        VALUES (:id, :agent_id, :content, true, CAST(:emb AS vector), :ed, :ep_id)
    """), {
        "id": fact_id, "agent_id": agent_id, "content": content,
        "emb": embedding_str, "ed": event_date, "ep_id": source_episode_id,
    })
    return fact_id


async def _insert_lifecycle_episode(
    session: AsyncSession, agent_id: str, summary: str,
    *, active: bool, ended_at, outcome: str | None,
    embedding: list[float] | None = None,
    session_id: str | None = None,
    started_at=None,
) -> str:
    """2026-07-12 F053 helper: insert an episode with explicit lifecycle
    state (active/ended_at/outcome) and optional embedding.

    ``session_id``/``started_at`` (2026-07-28) let a caller pin the
    episode→decision correlation window that replaced heart.episode_decisions;
    ``started_at`` defaults to NOW() as before.
    """
    ep_id = uuid4()
    emb_sql = "CAST(:emb AS vector)" if embedding is not None else "NULL"
    started_sql = ":st" if started_at is not None else "NOW()"
    params = {
        "id": ep_id, "agent_id": agent_id, "summary": summary,
        "act": active, "en": ended_at, "oc": outcome, "sid": session_id,
    }
    if started_at is not None:
        params["st"] = started_at
    if embedding is not None:
        params["emb"] = "[" + ",".join(str(float(v)) for v in embedding) + "]"
    await session.execute(text(f"""
        INSERT INTO heart.episodes
            (id, agent_id, summary, active, started_at, ended_at, outcome,
             embedding, session_id)
        VALUES (:id, :agent_id, :summary, :act, {started_sql}, :en, :oc,
                {emb_sql}, :sid)
    """), params)
    return ep_id


def _vec(*head: float) -> list[float]:
    """1536-dim test embedding: the given head, zero-padded."""
    return list(head) + [0.0] * (1536 - len(head))


@pytest.mark.postgres_only
async def test_happened_before_relatedness_gate(densifier, db, settings):
    """F075 (2026-06-13): a happened_before edge links two same-episode dated
    facts only when they are semantically related — date order alone must not
    chain an unrelated co-episode event (the 0.27-precision residual)."""
    from datetime import date

    settings.happened_before_relatedness_threshold = 0.45
    agent = densifier._agent_id
    async with db.session() as s:
        ep_rel = await _insert_episode(s, agent, "related events")
        ep_unrel = await _insert_episode(s, agent, "unrelated events")
        # related pair: near-identical embeddings (cosine ~1.0 >= 0.45 -> link)
        await _insert_dated_fact(s, agent, "A", _vec(1.0), date(2024, 1, 1), ep_rel)
        await _insert_dated_fact(s, agent, "B", _vec(1.0, 0.01), date(2024, 1, 2), ep_rel)
        # unrelated pair: orthogonal embeddings (cosine ~0 < 0.45 -> no link)
        await _insert_dated_fact(s, agent, "X", _vec(1.0), date(2024, 1, 1), ep_unrel)
        await _insert_dated_fact(s, agent, "Y", _vec(0.0, 1.0), date(2024, 1, 2), ep_unrel)
        await s.commit()

    n = await densifier._build_happened_before_edges()
    assert n == 1, "only the related same-episode pair should link"
    async with db.session() as s:
        rows = (await s.execute(text(
            "SELECT 1 FROM brain.graph_edges WHERE agent_id = :a "
            "AND relation = 'happened_before'"
        ), {"a": agent})).all()
    assert len(rows) == 1


@pytest.mark.postgres_only
async def test_happened_before_gate_disabled_at_zero(densifier, db, settings):
    """threshold 0 disables the gate: unrelated dated facts still chain
    (pre-fix date-order-only behaviour preserved)."""
    from datetime import date

    settings.happened_before_relatedness_threshold = 0.0
    agent = densifier._agent_id
    async with db.session() as s:
        ep = await _insert_episode(s, agent, "unrelated but dated")
        await _insert_dated_fact(s, agent, "X", _vec(1.0), date(2024, 1, 1), ep)
        await _insert_dated_fact(s, agent, "Y", _vec(0.0, 1.0), date(2024, 1, 2), ep)
        await s.commit()

    n = await densifier._build_happened_before_edges()
    assert n == 1, "gate disabled -> unrelated pair still links on date order"


@pytest.mark.postgres_only
async def test_find_orphans_episode_liveness(db, settings, mock_embeddings):
    """2026-07-12: a closed edge-less episode IS an orphan; a trivial
    discard and an abandoned episode (prod shape: ended_at SET per F060.2)
    are NOT — they're genuinely deleted."""
    from datetime import UTC, datetime

    agent_id = f"f040-ep-{uuid4().hex[:8]}"
    now = datetime.now(UTC)
    linker = GraphLinker(db, mock_embeddings, settings, agent_id)
    densifier = GraphDensifier(db, linker, mock_embeddings, settings, agent_id)

    async with db.session() as s:
        ep_closed = await _insert_lifecycle_episode(
            s, agent_id, "closed episode", active=False, ended_at=now,
            outcome="success",
        )
        ep_trivial = await _insert_lifecycle_episode(
            s, agent_id, "trivial discard", active=False, ended_at=None,
            outcome=None,
        )
        ep_abandoned = await _insert_lifecycle_episode(
            s, agent_id, "abandoned mark", active=False, ended_at=now,
            outcome="abandoned",
        )
        await s.commit()

    async with db.session() as s:
        orphans = await densifier.find_orphans(
            "episode", 50, s, require_embedding=False,
        )
    orphan_ids = {oid for oid, _ in orphans}
    assert ep_closed in orphan_ids
    assert ep_trivial not in orphan_ids
    assert ep_abandoned not in orphan_ids


@pytest.mark.postgres_only
async def test_same_type_backfill_links_orphan_to_closed_episode(
    db, settings, mock_embeddings,
):
    """2026-07-12 Task 4: two closed episodes with identical stored
    embeddings and no edges — backfilling must link them episode↔episode
    even though both have active=false (closed lifecycle state). Before
    the carve-out, hybrid_search's `AND t.active = true` excluded the
    closed candidate (the orphan itself is found thanks to the Task-3
    liveness extra_where)."""
    from datetime import UTC, datetime

    agent_id = f"f040-tgt-{uuid4().hex[:8]}"
    now = datetime.now(UTC)
    settings.ce_backfill_enabled = False
    linker = GraphLinker(db, mock_embeddings, settings, agent_id)
    densifier = GraphDensifier(db, linker, mock_embeddings, settings, agent_id)

    emb = _vec(1.0, 0.5, 0.25)
    async with db.session() as s:
        for _ in range(2):
            await _insert_lifecycle_episode(
                s, agent_id, "deploying the nous agent to production",
                active=False, ended_at=now, outcome="success", embedding=emb,
            )
        await s.commit()

    created = await densifier.backfill_orphan_episodes(max_count=5)
    assert created >= 1, (
        "orphan closed episode must link to the other closed episode"
    )


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
async def test_find_orphans_ignores_comention_edges(db, settings, mock_embeddings, _fix_stale_relation_constraint):
    """F076 (codex P2-C): a fact whose ONLY edge is co_mention must still be an orphan.

    co_mention links fact<->fact on a shared entity but gives no cross-type
    (fact->decision/episode) connectivity; counting it would permanently skip the
    fact from later F040 backfill cycles. A NON-co_mention edge still masks (control)."""
    agent_id = f"test-orphan-cm-{uuid4().hex[:8]}"
    linker = GraphLinker(db, mock_embeddings, settings, agent_id)
    densifier = GraphDensifier(db, linker, mock_embeddings, settings, agent_id)

    async with db.session() as session:
        emb = await mock_embeddings.embed("cm")
        cm_a = await _insert_fact(session, agent_id, "co_mention only fact A", emb)
        cm_b = await _insert_fact(session, agent_id, "co_mention only fact B", emb)
        await session.execute(text(
            "INSERT INTO brain.graph_edges (agent_id, source_id, target_id, source_type, "
            "target_type, relation, weight, auto_linked, extraction_method) "
            "VALUES (:a, :s, :t, 'fact', 'fact', 'related_to', 0.9, true, 'co_mention')"
        ), {"a": agent_id, "s": cm_a, "t": cm_b})
        # control: a fact linked by a non-co_mention (NULL extraction_method) edge
        cos_a = await _insert_fact(session, agent_id, "cosine-linked fact A", emb)
        cos_b = await _insert_fact(session, agent_id, "cosine-linked fact B", emb)
        await _insert_edge(session, agent_id, cos_a, cos_b, "fact", "fact")
        await session.commit()

    async with db.session() as session:
        orphan_ids = [oid for oid, _ in await densifier.find_orphans("fact", 100, session)]
    assert cm_a in orphan_ids and cm_b in orphan_ids   # co_mention does NOT mask
    assert cos_a not in orphan_ids and cos_b not in orphan_ids  # control still masks


async def test_find_orphans_ignores_supersedes_edges(db, settings, mock_embeddings, _fix_stale_relation_constraint):
    """2026-06-13 audit: a replacement fact whose ONLY edge is supersedes must
    still be an orphan. supersedes is lineage, not traversable connectivity
    (_neighbors + spreading both skip it), so if it masked orphan status the
    F040 backfill would never densify the replacement and it would stay
    graph-isolated. A non-supersedes edge still masks (control)."""
    agent_id = f"test-orphan-sup-{uuid4().hex[:8]}"
    linker = GraphLinker(db, mock_embeddings, settings, agent_id)
    densifier = GraphDensifier(db, linker, mock_embeddings, settings, agent_id)

    async with db.session() as session:
        emb = await mock_embeddings.embed("sup")
        # new (replacement, active) supersedes old (superseded) — new's only edge.
        new_fact = await _insert_fact(session, agent_id, "replacement fact new", emb)
        old_fact = await _insert_fact(session, agent_id, "superseded fact old", emb)
        await session.execute(text(
            "INSERT INTO brain.graph_edges (agent_id, source_id, target_id, source_type, "
            "target_type, relation, weight, auto_linked, extraction_method) "
            "VALUES (:a, :s, :t, 'fact', 'fact', 'supersedes', 1.0, true, 'deterministic')"
        ), {"a": agent_id, "s": new_fact, "t": old_fact})
        # control: a fact linked by a non-supersedes edge still masks.
        cos_a = await _insert_fact(session, agent_id, "cosine-linked fact A2", emb)
        cos_b = await _insert_fact(session, agent_id, "cosine-linked fact B2", emb)
        await _insert_edge(session, agent_id, cos_a, cos_b, "fact", "fact")
        await session.commit()

    async with db.session() as session:
        orphan_ids = [oid for oid, _ in await densifier.find_orphans("fact", 100, session)]
    assert new_fact in orphan_ids   # supersedes does NOT mask the replacement
    assert cos_a not in orphan_ids and cos_b not in orphan_ids  # control still masks


@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_find_orphans_ignores_co_occurred_and_contradicts(db, settings, mock_embeddings, _fix_stale_relation_constraint):
    """2b: co_occurred / contradicts / happened_before are not associative
    connectivity for densification — a fact whose only edge is one of them must
    still be an orphan (else F040 never densifies it)."""
    agent_id = f"test-orphan-2b-{uuid4().hex[:8]}"
    linker = GraphLinker(db, mock_embeddings, settings, agent_id)
    densifier = GraphDensifier(db, linker, mock_embeddings, settings, agent_id)

    async with db.session() as session:
        emb = await mock_embeddings.embed("co")
        f_cooc = await _insert_fact(session, agent_id, "co-occurred only fact here", emb)
        f_other = await _insert_fact(session, agent_id, "co-occurred partner fact here", emb)
        await session.execute(text(
            "INSERT INTO brain.graph_edges (agent_id, source_id, target_id, source_type, "
            "target_type, relation, weight, auto_linked, extraction_method) "
            "VALUES (:a, :s, :t, 'fact', 'fact', 'co_occurred', 1.0, true, 'co_occurrence')"
        ), {"a": agent_id, "s": f_cooc, "t": f_other})
        # control
        cos_a = await _insert_fact(session, agent_id, "cosine masked fact A3", emb)
        cos_b = await _insert_fact(session, agent_id, "cosine masked fact B3", emb)
        await _insert_edge(session, agent_id, cos_a, cos_b, "fact", "fact")
        await session.commit()

    async with db.session() as session:
        orphan_ids = [oid for oid, _ in await densifier.find_orphans("fact", 100, session)]
    assert f_cooc in orphan_ids   # co_occurred does NOT mask
    assert cos_a not in orphan_ids  # control still masks


@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_comention_skips_pair_with_contradicts_edge(db, settings, mock_embeddings, _fix_stale_relation_constraint):
    """F076 (codex P2-D): build_comention_edges must NOT add a related_to edge over a
    pair that already has a contradicts edge — adjacency-boost/spreading filter only
    `relation != 'contradicts'`, so that would let contradictory facts reinforce."""
    agent_id = f"test-cm-contra-{uuid4().hex[:8]}"
    s = settings.model_copy(update={"comention_linking_enabled": True})
    linker = GraphLinker(db, mock_embeddings, s, agent_id)
    densifier = GraphDensifier(db, linker, mock_embeddings, s, agent_id)

    async with db.session() as session:
        emb = await mock_embeddings.embed("x")
        a = await _insert_fact(session, agent_id, "Dara Velen leads Project Helios.", emb)
        b = await _insert_fact(session, agent_id, "Dara Velen never touched Project Helios.", emb)
        await session.execute(text(
            "INSERT INTO brain.graph_edges (agent_id, source_id, target_id, source_type, "
            "target_type, relation, weight, auto_linked) "
            "VALUES (:a, :s, :t, 'fact', 'fact', 'contradicts', 1.0, true)"
        ), {"a": agent_id, "s": a, "t": b})
        await session.commit()

    n = await densifier.build_comention_edges()

    async with db.session() as session:
        cm = (await session.execute(text(
            "SELECT count(*) FROM brain.graph_edges WHERE agent_id=:a AND extraction_method='co_mention'"
        ), {"a": agent_id})).scalar()
    assert n == 0 and cm == 0, "co_mention must skip a pair already joined by contradicts"


@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_comention_links_clean_shared_entity_pair(db, settings, mock_embeddings, _fix_stale_relation_constraint):
    """Positive control: the P2-D 'skip any prior fact-fact edge' change must NOT break
    the happy path — a shared-entity pair with no prior edge still gets exactly 1 edge."""
    agent_id = f"test-cm-clean-{uuid4().hex[:8]}"
    s = settings.model_copy(update={"comention_linking_enabled": True})
    linker = GraphLinker(db, mock_embeddings, s, agent_id)
    densifier = GraphDensifier(db, linker, mock_embeddings, s, agent_id)

    async with db.session() as session:
        emb = await mock_embeddings.embed("x")
        await _insert_fact(session, agent_id, "Dara Velen leads Project Helios.", emb)
        await _insert_fact(session, agent_id, "Dara Velen studied at the Halvorsen Institute.", emb)
        await session.commit()

    n = await densifier.build_comention_edges()
    async with db.session() as session:
        cm = (await session.execute(text(
            "SELECT count(*) FROM brain.graph_edges WHERE agent_id=:a AND extraction_method='co_mention'"
        ), {"a": agent_id})).scalar()
    assert n == 1 and cm == 1


@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_build_comention_dry_run_previews_without_writing(db, settings, mock_embeddings, _fix_stale_relation_constraint):
    """F076 backfill: dry_run returns the would-build count and writes nothing; a real
    run then inserts exactly that many."""
    agent_id = f"test-cm-dry-{uuid4().hex[:8]}"
    s = settings.model_copy(update={"comention_linking_enabled": True})
    linker = GraphLinker(db, mock_embeddings, s, agent_id)
    densifier = GraphDensifier(db, linker, mock_embeddings, s, agent_id)

    async with db.session() as session:
        emb = await mock_embeddings.embed("x")
        await _insert_fact(session, agent_id, "Dara Velen leads Project Helios.", emb)
        await _insert_fact(session, agent_id, "Dara Velen studied at the Halvorsen Institute.", emb)
        await session.commit()

    would = await densifier.build_comention_edges(dry_run=True)
    async with db.session() as session:
        after_dry = (await session.execute(text(
            "SELECT count(*) FROM brain.graph_edges WHERE agent_id=:a AND extraction_method='co_mention'"
        ), {"a": agent_id})).scalar()
    assert would == 1 and after_dry == 0  # previewed, nothing written

    built = await densifier.build_comention_edges()
    assert built == would


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
async def test_discover_clusters_ignores_supersedes_edges(db, settings, mock_embeddings, _fix_stale_relation_constraint):
    """2026-06-13 audit: a supersedes edge must not union the replacement with
    its inactive predecessor into a connected component. Two 2-node components
    bridged ONLY by a supersedes edge must stay separate (no 3+ component =>
    no cluster). If supersedes counted, all 4 would merge into one cluster."""
    agent_id = f"test-cluster-sup-{uuid4().hex[:8]}"
    linker = GraphLinker(db, mock_embeddings, settings, agent_id)
    densifier = GraphDensifier(db, linker, mock_embeddings, settings, agent_id)
    densifier._last_cluster_discovery = datetime.now(UTC) - timedelta(days=8)

    a, b, c, d = uuid4(), uuid4(), uuid4(), uuid4()
    async with db.session() as session:
        await _insert_edge(session, agent_id, a, b, "decision", "decision")  # related_to
        await _insert_edge(session, agent_id, c, d, "decision", "decision")  # related_to
        # bridge the two 2-node components with ONLY a supersedes edge
        await session.execute(text(
            "INSERT INTO brain.graph_edges (agent_id, source_id, target_id, source_type,"
            " target_type, relation, weight, auto_linked, extraction_method) "
            "VALUES (:a, :s, :t, 'decision', 'decision', 'supersedes', 1.0, true, 'deterministic')"
        ), {"a": agent_id, "s": str(b), "t": str(c)})
        await session.commit()

    result = await densifier.discover_clusters()
    # supersedes excluded => {a,b} and {c,d} stay separate, both < 3 => no cluster
    assert result == 0


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
async def test_adjacent_chunk_links_even_when_sibling_embedding_null(
    db, settings, mock_embeddings, _fix_stale_relation_constraint,
):
    """F070 (codex P2): adjacency is structural, not embedding-derived.
    When an adjacent sibling chunk has NULL embedding (e.g. embed call
    failed), the guaranteed sequential edge must still be created."""
    agent_id = f"test-chunk-null-emb-{uuid4().hex[:8]}"
    settings_local = settings.model_copy(update={
        "chunk_consolidation_enabled": True,
        "graph_backfill_enabled": True,
        "graph_threshold_chunk_chunk_intra": 0.5,
    })
    linker = GraphLinker(db, mock_embeddings, settings_local, agent_id)
    d = GraphDensifier(db, linker, mock_embeddings, settings_local, agent_id)

    async with db.session() as s:
        ep = await _insert_episode(s, agent_id, "test-null-emb")
        emb = await mock_embeddings.embed("apple")
        # c0 has embedding (orphan that find_orphans will pick up),
        # c1 is its adjacent sibling but has NULL embedding (embed failed).
        c0 = await _insert_chunk(s, agent_id, ep, 0, "apple chunk", emb)
        c1 = await _insert_chunk(s, agent_id, ep, 1, "zebra chunk", None)
        await s.commit()

    await d.backfill_orphan_chunks()

    async with db.session() as s:
        rows = (await s.execute(text(
            "SELECT source_id::text, target_id::text "
            "FROM brain.graph_edges "
            "WHERE agent_id = :a AND source_type='chunk' AND target_type='chunk'"
        ), {"a": agent_id})).all()
    pair = {(str(c0), str(c1)), (str(c1), str(c0))}
    found = {(r.source_id, r.target_id) for r in rows}
    assert pair & found, (
        f"adjacent chunks must link even when sibling embedding is NULL, "
        f"got {found}"
    )


@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_null_embedding_chunk_still_gets_part_of_edge(
    db, settings, mock_embeddings, _fix_stale_relation_constraint,
):
    """F070 (codex P2): chunks whose embed call failed (embedding IS NULL)
    must still receive the structural chunk→episode part_of edge — that's
    the whole point of v1 being 'edges only, embedding-tolerant'."""
    agent_id = f"test-null-emb-orphan-{uuid4().hex[:8]}"
    settings_local = settings.model_copy(update={
        "chunk_consolidation_enabled": True,
        "graph_backfill_enabled": True,
    })
    linker = GraphLinker(db, mock_embeddings, settings_local, agent_id)
    d = GraphDensifier(db, linker, mock_embeddings, settings_local, agent_id)

    async with db.session() as s:
        ep = await _insert_episode(s, agent_id, "ep")
        # Chunk with NULL embedding (failed embed call scenario).
        chunk = await _insert_chunk(s, agent_id, ep, 0, "content here", None)
        await s.commit()

    await d.backfill_orphan_chunks()

    async with db.session() as s:
        rows = (await s.execute(text(
            "SELECT target_id::text, relation FROM brain.graph_edges "
            "WHERE agent_id = :a AND source_id = :c "
            "  AND source_type = 'chunk'"
        ), {"a": agent_id, "c": chunk})).all()
    relations = {(r.target_id, r.relation) for r in rows}
    assert (str(ep), "part_of") in relations, (
        f"NULL-embedded chunk must still receive chunk→episode part_of edge, "
        f"got {relations}"
    )


@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_sequential_chunk_edge_persisted_at_weight_1_0(
    db, settings, mock_embeddings, _fix_stale_relation_constraint,
):
    """F070 (codex P2): adjacent chunk→chunk edges use the related_to
    relation (multiplier 0.8) but the documented structural weight is 1.0.
    Verify the multiplier override on create_edge keeps the persisted
    weight at 1.0 for sequential pairs."""
    agent_id = f"test-seq-weight-{uuid4().hex[:8]}"
    settings_local = settings.model_copy(update={
        "chunk_consolidation_enabled": True,
        "graph_backfill_enabled": True,
        # High cosine threshold so only the adjacency branch fires.
        "graph_threshold_chunk_chunk_intra": 0.99,
    })
    linker = GraphLinker(db, mock_embeddings, settings_local, agent_id)
    d = GraphDensifier(db, linker, mock_embeddings, settings_local, agent_id)

    async with db.session() as s:
        ep = await _insert_episode(s, agent_id, "ep")
        emb_a = await mock_embeddings.embed("apple")
        emb_b = await mock_embeddings.embed("zebra")
        c0 = await _insert_chunk(s, agent_id, ep, 0, "apple chunk", emb_a)
        c1 = await _insert_chunk(s, agent_id, ep, 1, "zebra chunk", emb_b)
        await s.commit()

    await d.backfill_orphan_chunks()

    async with db.session() as s:
        rows = (await s.execute(text(
            "SELECT weight FROM brain.graph_edges "
            "WHERE agent_id = :a AND source_type = 'chunk' "
            "  AND target_type = 'chunk' "
            "  AND ((source_id = :c0 AND target_id = :c1) "
            "    OR (source_id = :c1 AND target_id = :c0))"
        ), {"a": agent_id, "c0": c0, "c1": c1})).all()
    assert rows, "sequential edge should exist"
    weights = [float(r.weight) for r in rows]
    assert all(abs(w - 1.0) < 1e-6 for w in weights), (
        f"sequential chunk edges must persist at structural weight 1.0, "
        f"got {weights}"
    )


@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_structural_chunk_edges_tagged_deterministic(
    db, settings, mock_embeddings, _fix_stale_relation_constraint,
):
    """F070 (codex P2): structural chunk edges (chunk→episode part_of and
    adjacent chunk→chunk) must persist with extraction_method='deterministic'
    so `graph_inferred_edge_penalty` does not down-weight them at recall time.
    Cosine-derived chunk→fact summarized_by stays 'inferred' (auto_linker)."""
    agent_id = f"test-prov-{uuid4().hex[:8]}"
    settings_local = settings.model_copy(update={
        "chunk_consolidation_enabled": True,
        "graph_backfill_enabled": True,
        "graph_threshold_chunk_fact": 0.5,
        "graph_threshold_chunk_chunk_intra": 0.99,  # only adjacency fires
    })
    linker = GraphLinker(db, mock_embeddings, settings_local, agent_id)
    d = GraphDensifier(db, linker, mock_embeddings, settings_local, agent_id)

    async with db.session() as s:
        ep = await _insert_episode(s, agent_id, "ep")
        ident_emb = await mock_embeddings.embed("user likes dark roast coffee")
        c0 = await _insert_chunk(s, agent_id, ep, 0, "coffee chunk", ident_emb)
        c1 = await _insert_chunk(s, agent_id, ep, 1, "next chunk", ident_emb)
        fact = await _insert_fact_with_episode(
            s, agent_id, "user likes dark roast coffee", ident_emb, ep,
        )
        await s.commit()

    await d.backfill_orphan_chunks()

    async with db.session() as s:
        rows = (await s.execute(text(
            "SELECT target_id::text, target_type, relation, extraction_method "
            "FROM brain.graph_edges "
            "WHERE agent_id = :a AND source_id = :c0 AND source_type = 'chunk'"
        ), {"a": agent_id, "c0": c0})).all()
    by_target = {(r.target_id, r.relation): r.extraction_method for r in rows}
    assert by_target.get((str(ep), "part_of")) == "deterministic", (
        f"chunk→episode part_of must be deterministic, got {by_target}"
    )
    assert by_target.get((str(c1), "related_to")) == "deterministic", (
        f"adjacent chunk→chunk must be deterministic, got {by_target}"
    )
    assert by_target.get((str(fact), "summarized_by")) == "inferred", (
        f"cosine-derived chunk→fact summarized_by must remain inferred, "
        f"got {by_target}"
    )


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


# ---------------------------------------------------------------------------
# F070.1 — Cross-episode chunk graph edges
# ---------------------------------------------------------------------------


@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_cross_episode_links_chunk_to_fact_in_other_episode(
    db, settings, mock_embeddings, _fix_stale_relation_constraint,
):
    """F070.1: chunk → fact summarized_by must fire for facts in
    OTHER episodes when cosine ≥ threshold. Same-episode facts handled
    by F070 v1 path; cross-episode is the new code path."""
    agent_id = f"test-f070-1-cef-{uuid4().hex[:8]}"
    settings_local = settings.model_copy(update={
        "chunk_consolidation_enabled": True,
        "graph_backfill_enabled": True,
        "graph_threshold_chunk_fact_cross": 0.4,
        "chunk_cross_episode_top_k": 10,
    })
    linker = GraphLinker(db, mock_embeddings, settings_local, agent_id)
    d = GraphDensifier(db, linker, mock_embeddings, settings_local, agent_id)

    async with db.session() as s:
        ep_a = await _insert_episode(s, agent_id, "episode A")
        ep_b = await _insert_episode(s, agent_id, "episode B")
        ident_emb = await mock_embeddings.embed("coffee preferences")
        chunk_a = await _insert_chunk(
            s, agent_id, ep_a, 0, "user likes coffee strong", ident_emb,
        )
        # Fact lives in DIFFERENT episode (ep_b) — must still link.
        other_ep_fact = await _insert_fact_with_episode(
            s, agent_id, "user prefers dark roast coffee", ident_emb, ep_b,
        )
        # Give chunk_a a part_of edge so it's NOT classified as same-ep
        # orphan (avoids it being picked up by the wrong path).
        await _insert_edge(s, agent_id, chunk_a, ep_a, "chunk", "episode")
        await s.commit()

    created, _attempted = await d.backfill_orphan_chunks_cross_episode()
    assert created >= 1, "expected at least 1 cross-episode chunk→fact edge"

    async with db.session() as s:
        rows = (await s.execute(text(
            "SELECT target_id::text, relation, extraction_method "
            "FROM brain.graph_edges "
            "WHERE agent_id = :a AND source_id = :c "
            "  AND source_type = 'chunk' AND target_type = 'fact'"
        ), {"a": agent_id, "c": chunk_a})).all()
    edges = {(r.target_id, r.relation, r.extraction_method) for r in rows}
    assert (str(other_ep_fact), "summarized_by", "inferred") in edges, (
        f"cross-episode chunk→fact missing or wrong tier: {edges}"
    )


@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_cross_episode_links_chunk_to_chunk_in_other_episode(
    db, settings, mock_embeddings, _fix_stale_relation_constraint,
):
    """F070.1: chunk ↔ chunk related_to ACROSS episodes when cosine ≥
    threshold. Same-episode siblings handled by F070 v1 intra path."""
    agent_id = f"test-f070-1-cec-{uuid4().hex[:8]}"
    settings_local = settings.model_copy(update={
        "chunk_consolidation_enabled": True,
        "graph_backfill_enabled": True,
        "graph_threshold_chunk_chunk_cross": 0.4,
        "chunk_cross_episode_top_k": 10,
    })
    linker = GraphLinker(db, mock_embeddings, settings_local, agent_id)
    d = GraphDensifier(db, linker, mock_embeddings, settings_local, agent_id)

    async with db.session() as s:
        ep_a = await _insert_episode(s, agent_id, "episode A")
        ep_b = await _insert_episode(s, agent_id, "episode B")
        emb = await mock_embeddings.embed("shared topic")
        c_a = await _insert_chunk(s, agent_id, ep_a, 0, "topic content A", emb)
        c_b = await _insert_chunk(s, agent_id, ep_b, 0, "topic content B", emb)
        # Both already have a part_of edge → cross-episode orphans, not v1 orphans.
        await _insert_edge(s, agent_id, c_a, ep_a, "chunk", "episode")
        await _insert_edge(s, agent_id, c_b, ep_b, "chunk", "episode")
        await s.commit()

    created, _attempted = await d.backfill_orphan_chunks_cross_episode()
    assert created >= 1

    async with db.session() as s:
        rows = (await s.execute(text(
            "SELECT source_id::text, target_id::text "
            "FROM brain.graph_edges "
            "WHERE agent_id = :a AND source_type = 'chunk' "
            "  AND target_type = 'chunk' AND relation = 'related_to'"
        ), {"a": agent_id})).all()
    pair = {(str(c_a), str(c_b)), (str(c_b), str(c_a))}
    found = {(r.source_id, r.target_id) for r in rows}
    assert pair & found, (
        f"cross-episode chunk↔chunk missing, got {found}"
    )


@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_cross_episode_skips_below_threshold(
    db, settings, mock_embeddings, _fix_stale_relation_constraint,
):
    """F070.1: candidates below the cosine threshold must NOT be linked
    even when they're in the top-K HNSW scan."""
    agent_id = f"test-f070-1-thr-{uuid4().hex[:8]}"
    settings_local = settings.model_copy(update={
        "chunk_consolidation_enabled": True,
        "graph_backfill_enabled": True,
        "graph_threshold_chunk_fact_cross": 0.99,  # very high — nothing should pass
        "graph_threshold_chunk_chunk_cross": 0.99,
        "chunk_cross_episode_top_k": 10,
    })
    linker = GraphLinker(db, mock_embeddings, settings_local, agent_id)
    d = GraphDensifier(db, linker, mock_embeddings, settings_local, agent_id)

    async with db.session() as s:
        ep_a = await _insert_episode(s, agent_id, "ep A")
        ep_b = await _insert_episode(s, agent_id, "ep B")
        emb_a = await mock_embeddings.embed("apple")
        emb_b = await mock_embeddings.embed("zebra")  # different embedding
        c_a = await _insert_chunk(s, agent_id, ep_a, 0, "apple", emb_a)
        f_b = await _insert_fact_with_episode(
            s, agent_id, "zebra story", emb_b, ep_b,
        )
        c_b = await _insert_chunk(s, agent_id, ep_b, 0, "zebra", emb_b)
        await _insert_edge(s, agent_id, c_a, ep_a, "chunk", "episode")
        await _insert_edge(s, agent_id, c_b, ep_b, "chunk", "episode")
        await s.commit()

    created, _attempted = await d.backfill_orphan_chunks_cross_episode()

    async with db.session() as s:
        rows = (await s.execute(text(
            "SELECT COUNT(*) AS n FROM brain.graph_edges "
            "WHERE agent_id = :a AND source_type = 'chunk' "
            "  AND (target_type = 'fact' OR target_type = 'chunk') "
            "  AND relation IN ('summarized_by', 'related_to')"
        ), {"a": agent_id})).all()
    assert rows[0].n == 0, (
        f"expected 0 cross-ep edges with 0.99 threshold and dissimilar "
        f"embeddings, got {rows[0].n} (created counter said {created})"
    )


@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_cross_episode_finder_covers_both_chunk_chunk_directions(
    db, settings, mock_embeddings, _fix_stale_relation_constraint,
):
    """F070.1 (codex P1): find_chunks_lacking_cross_episode_edges must
    detect cross-episode connectivity whether the chunk is on the SOURCE
    or TARGET side of a chunk→chunk edge. Otherwise idempotency breaks
    and the script re-processes the same chunk forever."""
    agent_id = f"test-f070-1-dir-{uuid4().hex[:8]}"
    settings_local = settings.model_copy(update={
        "chunk_consolidation_enabled": True,
        "graph_backfill_enabled": True,
    })
    linker = GraphLinker(db, mock_embeddings, settings_local, agent_id)
    d = GraphDensifier(db, linker, mock_embeddings, settings_local, agent_id)

    async with db.session() as s:
        ep_a = await _insert_episode(s, agent_id, "ep A")
        ep_b = await _insert_episode(s, agent_id, "ep B")
        emb = await mock_embeddings.embed("x")
        # c_a has cross-episode edge where c_a is SOURCE (c_a → c_b).
        c_a = await _insert_chunk(s, agent_id, ep_a, 0, "A", emb)
        # c_b has cross-episode edge where c_b is TARGET (c_a → c_b).
        c_b = await _insert_chunk(s, agent_id, ep_b, 0, "B", emb)
        # Both have at-least-one-edge (the cross-episode edge itself
        # satisfies the EXISTS clause).
        await _insert_edge(s, agent_id, c_a, c_b, "chunk", "chunk")
        await s.commit()

    async with db.session() as s:
        candidates = await d.find_chunks_lacking_cross_episode_edges(
            limit=100, session=s,
        )
    candidate_ids = {str(cid) for cid, _content, _ep in candidates}
    assert str(c_a) not in candidate_ids, (
        f"c_a (source of cross-ep edge) wrongly classified as lacking "
        f"cross-ep edges: {candidate_ids}"
    )
    assert str(c_b) not in candidate_ids, (
        f"c_b (target of cross-ep edge) wrongly classified as lacking "
        f"cross-ep edges: {candidate_ids}"
    )


@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_cross_episode_idempotent_rerun(
    db, settings, mock_embeddings, _fix_stale_relation_constraint,
):
    """F070.1: re-running backfill_orphan_chunks_cross_episode must NOT
    create duplicate edges (ON CONFLICT DO NOTHING) and the second call
    must return 0 since find_chunks_lacking_cross_episode_edges now
    excludes already-linked chunks."""
    agent_id = f"test-f070-1-idem-{uuid4().hex[:8]}"
    settings_local = settings.model_copy(update={
        "chunk_consolidation_enabled": True,
        "graph_backfill_enabled": True,
        "graph_threshold_chunk_fact_cross": 0.4,
        "graph_threshold_chunk_chunk_cross": 0.4,
        "chunk_cross_episode_top_k": 10,
    })
    linker = GraphLinker(db, mock_embeddings, settings_local, agent_id)
    d = GraphDensifier(db, linker, mock_embeddings, settings_local, agent_id)

    async with db.session() as s:
        ep_a = await _insert_episode(s, agent_id, "ep A")
        ep_b = await _insert_episode(s, agent_id, "ep B")
        emb = await mock_embeddings.embed("shared")
        c_a = await _insert_chunk(s, agent_id, ep_a, 0, "shared content A", emb)
        f_b = await _insert_fact_with_episode(
            s, agent_id, "shared content B", emb, ep_b,
        )
        # Pre-existing part_of edges (post-F070 v1 backfill state).
        await _insert_edge(s, agent_id, c_a, ep_a, "chunk", "episode")
        await s.commit()

    first_created, _first_attempted = await d.backfill_orphan_chunks_cross_episode()
    second_created, second_attempted = await d.backfill_orphan_chunks_cross_episode()
    assert first_created >= 1, "first run must create edges"
    assert second_created == 0, (
        f"second run must be a no-op (idempotent), created {second_created}"
    )
    assert second_attempted == [], (
        f"second run must report no attempted chunks (all already linked), "
        f"got {second_attempted}"
    )


@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_cross_episode_finder_excludes_null_embedding_chunks(
    db, settings, mock_embeddings, _fix_stale_relation_constraint,
):
    """F070.1 (codex round-2 P2): chunks without embeddings can never
    produce cross-episode edges (both link queries require embedding
    NOT NULL). The finder must exclude them so they don't occupy the
    LIMIT window forever."""
    agent_id = f"test-f070-1-nullemb-{uuid4().hex[:8]}"
    settings_local = settings.model_copy(update={
        "chunk_consolidation_enabled": True,
        "graph_backfill_enabled": True,
    })
    linker = GraphLinker(db, mock_embeddings, settings_local, agent_id)
    d = GraphDensifier(db, linker, mock_embeddings, settings_local, agent_id)

    async with db.session() as s:
        ep_a = await _insert_episode(s, agent_id, "ep A")
        ep_b = await _insert_episode(s, agent_id, "ep B")
        emb = await mock_embeddings.embed("topic")
        null_emb_chunk = await _insert_chunk(
            s, agent_id, ep_a, 0, "missing embedding", None,
        )
        ok_chunk = await _insert_chunk(s, agent_id, ep_b, 0, "ok content", emb)
        await _insert_edge(s, agent_id, null_emb_chunk, ep_a, "chunk", "episode")
        await _insert_edge(s, agent_id, ok_chunk, ep_b, "chunk", "episode")
        await s.commit()

    async with db.session() as s:
        candidates = await d.find_chunks_lacking_cross_episode_edges(
            limit=100, session=s,
        )
    candidate_ids = {str(cid) for cid, _content, _ep in candidates}
    assert str(null_emb_chunk) not in candidate_ids, (
        f"NULL-embedded chunk must NOT be returned by finder; got "
        f"{candidate_ids}"
    )
    assert str(ok_chunk) in candidate_ids, (
        f"embedded chunk must be returned by finder; got {candidate_ids}"
    )


@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_cross_episode_finder_honors_exclude_ids(
    db, settings, mock_embeddings, _fix_stale_relation_constraint,
):
    """F070.1 (codex round-3 P1): the finder accepts an ``exclude_ids``
    set so callers can paginate without skipping unprocessed candidates.
    Insert 3 candidates and verify each batch with cumulative exclusion
    returns a previously-unseen row, with the 4th call empty."""
    agent_id = f"test-f070-1-excl-{uuid4().hex[:8]}"
    settings_local = settings.model_copy(update={
        "chunk_consolidation_enabled": True,
        "graph_backfill_enabled": True,
    })
    linker = GraphLinker(db, mock_embeddings, settings_local, agent_id)
    d = GraphDensifier(db, linker, mock_embeddings, settings_local, agent_id)

    async with db.session() as s:
        ep = await _insert_episode(s, agent_id, "ep")
        emb = await mock_embeddings.embed("topic")
        chunk_ids = []
        for i in range(3):
            cid = await _insert_chunk(s, agent_id, ep, i, f"chunk {i}", emb)
            await _insert_edge(s, agent_id, cid, ep, "chunk", "episode")
            chunk_ids.append(cid)
        await s.commit()

    seen: set = set()
    async with db.session() as s:
        for _ in range(3):
            results = await d.find_chunks_lacking_cross_episode_edges(
                limit=1, session=s, exclude_ids=seen,
            )
            assert len(results) == 1, (
                f"expected 1 row with exclude_ids={seen}, got {len(results)}"
            )
            seen.add(results[0][0])
        empty = await d.find_chunks_lacking_cross_episode_edges(
            limit=1, session=s, exclude_ids=seen,
        )
        assert empty == [], f"empty result expected with all excluded, got {empty}"

    assert seen == set(chunk_ids), (
        f"three exclusion-paginated batches must expose each chunk once; "
        f"got {seen} vs expected {set(chunk_ids)}"
    )


@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_cross_episode_backfill_visits_every_chunk_even_when_some_link(
    db, settings, mock_embeddings, _fix_stale_relation_constraint,
):
    """F070.1 (codex round-3 P1): regression for the bug where
    successful batches caused the offset-based pager to skip
    still-unlinked candidates. Scenario: 3 candidates A, B, C.
    A and C have matching cross-episode neighbors (will link);
    B does NOT (hard negative — only same-episode neighbors).
    With ``effective_batch=2`` over multiple batches and
    ``exclude_ids`` tracking, all 3 chunks must be ATTEMPTED — the
    backfill must not exit while B is still un-visited.
    """
    agent_id = f"test-f070-1-visit-all-{uuid4().hex[:8]}"
    settings_local = settings.model_copy(update={
        "chunk_consolidation_enabled": True,
        "graph_backfill_enabled": True,
        "graph_threshold_chunk_fact_cross": 0.4,
        "graph_threshold_chunk_chunk_cross": 0.4,
        "chunk_cross_episode_top_k": 10,
    })
    linker = GraphLinker(db, mock_embeddings, settings_local, agent_id)
    d = GraphDensifier(db, linker, mock_embeddings, settings_local, agent_id)

    async with db.session() as s:
        ep_a = await _insert_episode(s, agent_id, "ep A")
        ep_b = await _insert_episode(s, agent_id, "ep B")
        # Three chunks in ep_a. A and C share embedding with a fact in
        # ep_b → both will link cross-episode. B has a unique embedding
        # with no cross-episode neighbor → hard negative.
        emb_shared = await mock_embeddings.embed("shared topic")
        emb_unique = await mock_embeddings.embed("zebra unique content")
        chunk_a = await _insert_chunk(s, agent_id, ep_a, 0, "shared A", emb_shared)
        chunk_b = await _insert_chunk(s, agent_id, ep_a, 1, "zebra B", emb_unique)
        chunk_c = await _insert_chunk(s, agent_id, ep_a, 2, "shared C", emb_shared)
        # Cross-episode fact in ep_b that matches A and C.
        await _insert_fact_with_episode(
            s, agent_id, "shared topic content", emb_shared, ep_b,
        )
        for cid, ep in [(chunk_a, ep_a), (chunk_b, ep_a), (chunk_c, ep_a)]:
            await _insert_edge(s, agent_id, cid, ep, "chunk", "episode")
        await s.commit()

    # Simulate the script's loop with effective_batch=2 + exclude_ids tracking.
    attempted: set = set()
    total_created = 0
    seen_attempted: list = []
    for _ in range(5):  # safety cap
        created, attempted_ids = await d.backfill_orphan_chunks_cross_episode(
            max_count=2, exclude_ids=attempted,
        )
        if not attempted_ids:
            break
        attempted.update(attempted_ids)
        seen_attempted.extend(attempted_ids)
        total_created += created

    attempted_str = {str(x) for x in attempted}
    expected = {str(chunk_a), str(chunk_b), str(chunk_c)}
    assert attempted_str == expected, (
        f"every candidate must be attempted at least once across batches; "
        f"got {attempted_str} vs expected {expected} (created={total_created})"
    )


# ---------------------------------------------------------------------------
# 2026-07-12 F053 remediation — restore_episode_anchor_edges
# ---------------------------------------------------------------------------


@pytest.mark.postgres_only
async def test_restore_is_complete_scoped_idempotent_and_prune_safe(
    db, settings, mock_embeddings, _fix_stale_relation_constraint,
):
    """The full F053 damage shape, plus the invariants around it:

    Agent A fixture:
      - LIVE closed episode: 2 chunks, 1 active fact, 1 inactive fact,
        1 decision recorded inside its session window, 1 fact with NULL
        source_episode_id (must contribute nothing).
      - DEAD (trivial) episode: 1 chunk, 1 active fact (all skipped).
    Agent B fixture: identical minimal live shape (1 chunk) — must be
    untouched by agent A's run and excluded from A's dry-run counts.

    Asserts: dry_run counts == real-run counts and dry_run writes
    nothing; restored rows have exact endpoints, weight 1.0,
    extraction_method='deterministic'; re-run inserts 0 (idempotent);
    and — the invariant this whole plan exists for — running
    _phase_prune_dead_edges AFTER the restore deletes none of the
    restored edges (episode_dead_sql and episode_live_sql stay
    complements)."""
    from datetime import UTC, datetime
    from unittest.mock import AsyncMock, MagicMock

    from nous.events import EventBus
    from nous.handlers.sleep_handler import SleepHandler
    from nous.heart import Heart

    agent_a = f"f053-ra-{uuid4().hex[:8]}"
    agent_b = f"f053-rb-{uuid4().hex[:8]}"
    now = datetime.now(UTC)
    started = now - timedelta(hours=1)
    in_window = now - timedelta(minutes=30)
    # session_id is NOT agent-namespaced in prod (heartbeat-<hex> /
    # subtask-<hex> are generated identically per agent), so agent A and
    # agent B deliberately share one here.
    shared_session = f"f053-session-{uuid4().hex[:8]}"
    decision_id = uuid4()

    async with db.session() as fs:
        for aid in (agent_a, agent_b):
            await fs.execute(text(
                "INSERT INTO nous_system.agents (id, name) VALUES "
                "(:aid, 'x') ON CONFLICT (id) DO NOTHING"
            ), {"aid": aid})
        await fs.commit()

    async with db.session() as fs:
        ep_live = await _insert_lifecycle_episode(
            fs, agent_a, "live closed episode", active=False, ended_at=now,
            outcome="success", session_id=shared_session, started_at=started,
        )
        ep_dead = await _insert_lifecycle_episode(
            fs, agent_a, "trivial discard", active=False, ended_at=None,
            outcome=None,
        )
        ep_b = await _insert_lifecycle_episode(
            fs, agent_b, "agent B closed episode", active=False, ended_at=now,
            outcome="success", session_id=shared_session, started_at=started,
        )
        chunk_l1 = await _insert_chunk(fs, agent_a, ep_live, 0, "chunk one", None)
        chunk_l2 = await _insert_chunk(fs, agent_a, ep_live, 1, "chunk two", None)
        await _insert_chunk(fs, agent_a, ep_dead, 0, "dead chunk", None)
        await _insert_chunk(fs, agent_b, ep_b, 0, "agent B chunk", None)

        async def _fact(aid, eid, active):
            fid = uuid4()
            await fs.execute(text(
                "INSERT INTO heart.facts "
                "(id, agent_id, content, active, source_episode_id) "
                "VALUES (:id, :aid, 'restore fact fixture', :a, :eid)"
            ), {"id": fid, "aid": aid, "a": active, "eid": eid})
            return fid

        fact_live = await _fact(agent_a, ep_live, True)
        await _fact(agent_a, ep_live, False)   # inactive — skipped
        await _fact(agent_a, ep_dead, True)    # dead parent — skipped
        await _fact(agent_a, None, True)       # NULL FK — contributes nothing
        await fs.execute(text(
            "INSERT INTO brain.decisions "
            "(id, agent_id, description, confidence, category, stakes, "
            " session_id, created_at) "
            "VALUES (:id, :aid, 'restore decision fixture', 0.8, "
            "        'process', 'low', :sid, :ts)"
        ), {
            "id": decision_id, "aid": agent_a, "sid": shared_session,
            "ts": in_window,
        })
        # Codex PR #557 P2, restated for the session_id join: session_id is
        # NOT agent-namespaced, so a decision owned by agent B in the SAME
        # session must be SKIPPED, not materialized as a cross-agent
        # discussed_in edge. Only `d.agent_id = ep.agent_id` prevents that.
        cross_agent_decision = uuid4()
        await fs.execute(text(
            "INSERT INTO brain.decisions "
            "(id, agent_id, description, confidence, category, stakes, "
            " session_id, created_at) "
            "VALUES (:id, :aid, 'agent B decision', 0.8, 'process', 'low', "
            "        :sid, :ts)"
        ), {
            "id": cross_agent_decision, "aid": agent_b,
            "sid": shared_session, "ts": in_window,
        })
        # Same session + same agent, but recorded AFTER the episode closed —
        # outside the window, so it must not produce an edge either.
        out_of_window_decision = uuid4()
        await fs.execute(text(
            "INSERT INTO brain.decisions "
            "(id, agent_id, description, confidence, category, stakes, "
            " session_id, created_at) "
            "VALUES (:id, :aid, 'after the episode closed', 0.8, 'process', "
            "        'low', :sid, :ts)"
        ), {
            "id": out_of_window_decision, "aid": agent_a,
            "sid": shared_session, "ts": now + timedelta(hours=1),
        })
        await fs.commit()

    linker = GraphLinker(db, mock_embeddings, settings, agent_a)
    densifier = GraphDensifier(db, linker, mock_embeddings, settings, agent_a)

    expected = {"part_of": 2, "extracted_from": 1, "discussed_in": 1}

    # dry_run: report without writing, agent-scoped
    dry = await densifier.restore_episode_anchor_edges(dry_run=True)
    assert dry == expected
    async with db.session() as vs:
        n = (await vs.execute(text(
            "SELECT count(*) FROM brain.graph_edges "
            "WHERE agent_id IN (:a, :b)"
        ), {"a": agent_a, "b": agent_b})).scalar()
    assert n == 0, "dry_run must not write"

    # real run
    created = await densifier.restore_episode_anchor_edges()
    assert created == expected

    async with db.session() as vs:
        rows = (await vs.execute(text(
            "SELECT source_id, target_id, relation, weight, "
            "       extraction_method, agent_id "
            "FROM brain.graph_edges "
            "WHERE agent_id IN (:a, :b)"
        ), {"a": agent_a, "b": agent_b})).all()
    assert all(r.agent_id == agent_a for r in rows), "agent B must be untouched"
    by_rel: dict = {}
    for r in rows:
        by_rel.setdefault(r.relation, []).append(r)
    assert {r.source_id for r in by_rel["part_of"]} == {chunk_l1, chunk_l2}
    assert all(r.target_id == ep_live for r in by_rel["part_of"])
    assert by_rel["extracted_from"][0].source_id == fact_live
    assert by_rel["extracted_from"][0].target_id == ep_live
    assert by_rel["discussed_in"][0].source_id == ep_live
    assert by_rel["discussed_in"][0].target_id == decision_id
    assert all(
        float(r.weight) == 1.0 and r.extraction_method == "deterministic"
        for r in rows
    )

    # idempotent re-run
    again = await densifier.restore_episode_anchor_edges()
    assert again == {"part_of": 0, "extracted_from": 0, "discussed_in": 0}

    # restore → prune interplay: the prune must NOT delete what the
    # restore just wrote (dead/live predicates stay complements).
    handler_settings = Settings()
    object.__setattr__(handler_settings, "agent_id", agent_a)
    object.__setattr__(handler_settings, "dead_edge_pruning_enabled", True)
    object.__setattr__(handler_settings, "dead_edge_pruning_max_per_cycle", 1000)
    heart = Heart(db, handler_settings, embedding_provider=mock_embeddings)
    bus = MagicMock(spec=EventBus)
    bus.on = MagicMock()
    bus.emit = AsyncMock()
    handler = SleepHandler(AsyncMock(), heart, handler_settings, bus, AsyncMock())
    assert await handler._phase_prune_dead_edges({}) is True
    async with db.session() as vs:
        n_after = (await vs.execute(text(
            "SELECT count(*) FROM brain.graph_edges WHERE agent_id = :a"
        ), {"a": agent_a})).scalar()
    assert n_after == sum(expected.values()), (
        "prune deleted restored edges — dead/live predicates drifted"
    )
    await heart.close()
