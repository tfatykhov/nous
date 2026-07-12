"""F040 — Graph Densifier: orphan backfill engine + cluster discovery.

Runs during sleep cycles to find orphan nodes (no graph edges) and
connect them to similar nodes via embedding similarity.  Also discovers
disconnected clusters and proposes bridge edges between similar hubs.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from nous.brain._entity_config import _ENTITY_CONFIG
from nous.brain.graph_constants import autobehavior_exclusion_sql, episode_live_sql
from nous.brain.backfill_rerank import (
    ce_rerank_backfill_candidates,
    fetch_candidate_content,
)
from nous.brain.embeddings import EmbeddingProvider
from nous.brain.graph_linker import GraphLinker, common_template_text, edge_confidence
from nous.config import Settings
from nous.heart.search import hybrid_search
from nous.storage.database import Database

logger = logging.getLogger(__name__)

# Relation types for different type pairs
_RELATION_MAP: dict[tuple[str, str], str] = {
    ("fact", "fact"): "related_to",
    ("fact", "decision"): "evidence_for",
    ("fact", "episode"): "extracted_from",
    ("fact", "procedure"): "related_to",
    ("decision", "decision"): "related_to",
    ("decision", "episode"): "discussed_in",
    ("decision", "procedure"): "informed_by",
    ("episode", "episode"): "related_to",
    ("episode", "procedure"): "related_to",
    ("procedure", "procedure"): "related_to",
    # F070 (2026-05-25): chunk relations
    ("chunk", "fact"): "summarized_by",       # chunk text → fact extracted from same episode
    ("chunk", "episode"): "part_of",          # chunk → its source episode
    ("chunk", "chunk"): "related_to",         # intra/cross-episode chunk similarity
}


def _get_strict_threshold(settings: Settings, source_type: str, target_type: str) -> float:
    """Strict per-relation cosine thresholds — used when CE backfill is disabled."""
    key = tuple(sorted([source_type, target_type]))
    thresholds = {
        ("fact", "fact"): settings.graph_threshold_fact_fact,
        ("decision", "fact"): settings.graph_threshold_fact_decision,
        ("episode", "fact"): settings.graph_threshold_fact_episode,
        ("decision", "decision"): settings.graph_threshold_decision_decision,
        ("episode", "episode"): settings.graph_threshold_episode_episode,
    }
    if "procedure" in key:
        return settings.graph_threshold_procedure_any
    return thresholds.get(key, 0.75)


def _get_ce_mode_threshold(settings: Settings, source_type: str, target_type: str) -> float:
    """Relaxed thresholds for when F043 CE backfill is upstream and has already
    pruned candidates. Defaults calibrated from the 2026-04-14 A/B experiment
    where fact-fact=0.65 achieved 80% LLM-judged precision.
    """
    key = tuple(sorted([source_type, target_type]))
    thresholds = {
        ("fact", "fact"): settings.ce_backfill_threshold_fact_fact,
        ("decision", "fact"): settings.ce_backfill_threshold_fact_decision,
        ("episode", "fact"): settings.ce_backfill_threshold_fact_episode,
        ("decision", "decision"): settings.ce_backfill_threshold_decision_decision,
        ("episode", "episode"): settings.ce_backfill_threshold_episode_episode,
    }
    if "procedure" in key:
        return settings.ce_backfill_threshold_procedure_any
    return thresholds.get(key, 0.60)


def _get_threshold(settings: Settings, source_type: str, target_type: str) -> float:
    """Resolve per-relation cosine threshold.

    Routes to CE-mode (relaxed) thresholds when ``ce_backfill_enabled`` is set,
    otherwise returns the strict defaults. F045.
    """
    if getattr(settings, "ce_backfill_enabled", False):
        return _get_ce_mode_threshold(settings, source_type, target_type)
    return _get_strict_threshold(settings, source_type, target_type)


def _get_relation(source_type: str, target_type: str) -> str:
    """Get the relation type for a given type pair."""
    return _RELATION_MAP.get((source_type, target_type),
                             _RELATION_MAP.get((target_type, source_type), "related_to"))


class GraphDensifier:
    """Orphan backfill engine for graph densification.

    Finds nodes with zero graph edges (orphans) and connects them to
    similar nodes using embedding similarity with multi-signal scoring.
    """

    def __init__(
        self,
        db: Database,
        graph_linker: GraphLinker,
        embedder: EmbeddingProvider | None,
        settings: Settings,
        agent_id: str,
    ) -> None:
        self.db = db
        self._linker = graph_linker
        self._embedder = embedder
        self._settings = settings
        self._agent_id = agent_id
        self._interrupted = False
        self._last_cluster_discovery: datetime | None = None

    def interrupt(self) -> None:
        """Signal the densifier to stop at the next safe point."""
        self._interrupted = True

    async def find_orphans(
        self,
        entity_type: str,
        limit: int,
        session: AsyncSession,
        *,
        require_embedding: bool = True,
    ) -> list[tuple[UUID, str]]:
        """Find nodes with no graph edges (orphans).

        Returns list of (id, content_text) tuples for orphan nodes.

        ``require_embedding=False`` allows F070 chunks whose embed call
        failed to still receive structural edges (e.g. ``part_of``) that
        don't depend on embeddings. Default keeps existing semantics for
        all other entity types (cosine-driven backfill requires embeddings).
        """
        config = _ENTITY_CONFIG.get(entity_type)
        if not config:
            return []

        table, type_name, content_col, extra_where = config
        embedding_clause = "AND t.embedding IS NOT NULL" if require_embedding else ""
        orphan_excl = autobehavior_exclusion_sql("e.")  # 2b

        sql = text(f"""
            SELECT t.id, {content_col} AS content
            FROM {table} t
            WHERE t.agent_id = :agent_id
              AND {extra_where}
              {embedding_clause}
              AND NOT EXISTS (
                  SELECT 1 FROM brain.graph_edges e
                  WHERE e.agent_id = :agent_id
                    -- 2b: a non-associative edge (co_mention/co_occurred builder,
                    -- supersedes lineage, contradicts negative, happened_before
                    -- temporal) must NOT make a fact look non-orphan, or the F040
                    -- backfill permanently skips it and leaves it graph-isolated.
                    -- Single source of truth: graph_constants.
                    AND {orphan_excl}
                    AND (
                        (e.source_id = t.id AND e.source_type = :type_name)
                        OR (e.target_id = t.id AND e.target_type = :type_name)
                    )
              )
            ORDER BY t.created_at DESC NULLS LAST
            LIMIT :lim
        """)
        result = await session.execute(sql, {
            "agent_id": self._agent_id,
            "type_name": type_name,
            "lim": limit,
        })
        return [(row.id, row.content or "") for row in result.all()]

    async def _backfill_same_type(
        self,
        entity_type: str,
        orphan_id: UUID,
        orphan_content: str,
        session: AsyncSession,
        ce_stats: dict[str, int] | None = None,
    ) -> int:
        """Link an orphan to similar nodes of the same type using hybrid search.

        Uses RRF-fused vector + keyword search to find candidates, catching
        relationships that pure vector search misses (shared terms in
        different embedding neighborhoods).
        """
        config = _ENTITY_CONFIG[entity_type]
        table, type_name, content_col, extra_where = config
        threshold = _get_threshold(self._settings, entity_type, entity_type)

        # Fetch the orphan's stored embedding for vector search
        emb_sql = text(f"SELECT embedding::text FROM {table} WHERE id = :orphan_id")
        emb_result = await session.execute(emb_sql, {"orphan_id": orphan_id})
        emb_row = emb_result.first()

        orphan_embedding: list[float] | None = None
        if emb_row and emb_row.embedding:
            import json
            raw = emb_row.embedding
            orphan_embedding = json.loads(raw) if isinstance(raw, str) else raw

        # Hybrid search: vector + keyword via RRF
        # brain.decisions has no `active` column — disable active filter for
        # it. Episodes: `active=false` is the closed lifecycle state, not
        # deletion (HT-1) — filter by the liveness predicate instead of the
        # raw flag, or closed episodes can never be link targets.
        extra_where = "AND t.id != :orphan_id"
        has_active = entity_type != "decision"
        if entity_type == "episode":
            has_active = False
            extra_where += f" AND {episode_live_sql('t.')}"
        candidates = await hybrid_search(
            session=session,
            table=table,
            embedding=orphan_embedding,
            query_text=orphan_content[:500] if orphan_content else "",
            agent_id=self._agent_id,
            extra_where=extra_where,
            extra_params={"orphan_id": orphan_id},
            limit=10,
            vector_weight=0.6,  # 60% vector, 40% keyword — gives FTS more weight than default
            active_filter=has_active,
        )

        if not candidates:
            return 0

        # F043: CE rerank before cosine verification (precision pre-filter).
        if self._settings.ce_backfill_enabled:
            content_map = await fetch_candidate_content(
                session,
                self._agent_id,
                entity_type,
                [c[0] for c in candidates],
                settings=self._settings,  # F054: enables decision-content guard
            )
            before = len(candidates)
            candidates = await ce_rerank_backfill_candidates(
                query_text=orphan_content,
                candidate_rows=candidates,
                content_map=content_map,
                settings=self._settings,
                log_context=f"{entity_type}-same:{orphan_id}",
            )
            after = len(candidates)
            if ce_stats is not None:
                ce_stats["survived"] += after
                ce_stats["pruned"] += max(before - after, 0)
            if not candidates:
                return 0

        # For each candidate, verify actual cosine similarity meets threshold
        # (RRF scores are rank-based, not directly comparable to similarity thresholds)
        edges_created = 0
        relation = _get_relation(entity_type, entity_type)

        for cand_id, rrf_score in candidates:
            if cand_id == orphan_id:
                continue

            # Check actual embedding similarity if we have the orphan embedding
            if orphan_embedding:
                sim_sql = text(f"""
                    SELECT 1 - (embedding <=> CAST(:emb AS vector)) AS similarity
                    FROM {table} WHERE id = :cand_id AND embedding IS NOT NULL
                """)
                emb_str = "[" + ",".join(str(float(v)) for v in orphan_embedding) + "]"
                sim_result = await session.execute(sim_sql, {
                    "emb": emb_str,
                    "cand_id": cand_id,
                })
                sim_row = sim_result.first()
                if not sim_row or sim_row.similarity < threshold:
                    continue
                weight = float(sim_row.similarity)
            else:
                # Keyword-only match — use RRF score as proxy weight
                weight = min(rrf_score, 1.0)

            edge = await self._linker.create_edge(
                source_id=orphan_id,
                target_id=cand_id,
                source_type=type_name,
                target_type=type_name,
                relation=relation,
                weight=weight,
                session=session,
            )
            if edge:
                edges_created += 1

        return edges_created

    async def _backfill_cross_type(
        self,
        source_type: str,
        orphan_id: UUID,
        orphan_content: str,
        target_type: str,
        session: AsyncSession,
        ce_stats: dict[str, int] | None = None,
    ) -> int:
        """Link an orphan to nodes of a different type.

        Uses two candidate sources merged via dedup:
        1. Vector search with common-template re-embedding (existing approach)
        2. Keyword-only hybrid search (catches term matches embeddings miss)
        """
        config = _ENTITY_CONFIG[target_type]
        target_table, target_type_name, target_content_col, target_where = config
        threshold = _get_threshold(self._settings, source_type, target_type)

        # Candidate set (deduped by ID)
        candidate_ids: set[UUID] = set()

        # Source 1: Vector search with common-template re-embedding
        source_embedding: list[float] | None = None
        if self._embedder:
            source_template = common_template_text(source_type, orphan_content)
            try:
                source_embedding = await self._embedder.embed(source_template)
            except Exception:
                logger.warning("Failed to embed %s %s for cross-type backfill", source_type, orphan_id)

        if source_embedding:
            embedding_str = "[" + ",".join(str(float(v)) for v in source_embedding) + "]"
            sql = text(f"""
                SELECT t.id
                FROM {target_table} t
                WHERE t.agent_id = :agent_id
                  AND {target_where}
                  AND t.embedding IS NOT NULL
                  AND 1 - (t.embedding <=> CAST(:embedding AS vector)) >= :pre_threshold
                ORDER BY t.embedding <=> CAST(:embedding AS vector)
                LIMIT 5
            """)
            result = await session.execute(sql, {
                "agent_id": self._agent_id,
                "embedding": embedding_str,
                "pre_threshold": threshold * 0.9,
            })
            for row in result:
                candidate_ids.add(row.id)

        # Source 2: Keyword search via hybrid_search (keyword-only, no embedding)
        if orphan_content:
            has_active = target_type != "decision"
            kw_extra_where = ""
            if target_type == "episode":
                # Same liveness carve-out as _backfill_same_type. No live
                # caller passes target_type="episode" today — this is
                # consistency-hardening so a future caller doesn't silently
                # re-inherit the active=true filter (2026-07-12 review F9).
                has_active = False
                kw_extra_where = f"AND {episode_live_sql('t.')}"
            keyword_hits = await hybrid_search(
                session=session,
                table=target_table,
                embedding=None,  # keyword-only
                query_text=orphan_content[:500],
                agent_id=self._agent_id,
                extra_where=kw_extra_where,
                limit=5,
                active_filter=has_active,
            )
            for cand_id, _ in keyword_hits:
                candidate_ids.add(cand_id)

        if not candidate_ids:
            return 0

        # Fetch content for all candidates in one query
        placeholders = ", ".join(f":id_{i}" for i in range(len(candidate_ids)))
        content_sql = text(f"""
            SELECT t.id, {target_content_col} AS content
            FROM {target_table} t
            WHERE t.id IN ({placeholders})
        """)
        params = {f"id_{i}": cid for i, cid in enumerate(candidate_ids)}
        result = await session.execute(content_sql, params)
        candidate_content: dict[UUID, str] = {
            row.id: row.content for row in result if row.content
        }

        # F043: CE rerank cross-type survivors before the re-embed loop.
        if self._settings.ce_backfill_enabled and candidate_content:
            sorted_ids = sorted(candidate_content.keys())  # deterministic tie-break
            synthetic_rows = [(cid, 0.0) for cid in sorted_ids]
            before = len(synthetic_rows)
            ranked = await ce_rerank_backfill_candidates(
                query_text=orphan_content,
                candidate_rows=synthetic_rows,
                content_map=candidate_content,
                settings=self._settings,
                log_context=f"{source_type}->{target_type}:{orphan_id}",
            )
            surviving = {cid for cid, _ in ranked}
            candidate_content = {
                cid: txt
                for cid, txt in candidate_content.items()
                if cid in surviving
            }
            after = len(candidate_content)
            if ce_stats is not None:
                ce_stats["survived"] += after
                ce_stats["pruned"] += max(before - after, 0)

        edges_created = 0
        relation = _get_relation(source_type, target_type)

        for cand_id, content in candidate_content.items():
            # Re-embed target with common template for fair similarity
            if not self._embedder:
                continue
            target_template = common_template_text(target_type, content)
            try:
                target_embedding = await self._embedder.embed(target_template)
            except Exception:
                continue

            if source_embedding:
                similarity = GraphLinker._cosine_similarity(source_embedding, target_embedding)
            else:
                # No source embedding — skip this candidate
                continue

            if similarity >= threshold:
                confidence = edge_confidence(
                    similarity=similarity,
                    shared_tags=0,
                    shared_subject=False,
                    temporal_proximity_days=0.0,
                )

                edge = await self._linker.create_edge(
                    source_id=orphan_id,
                    target_id=cand_id,
                    source_type=source_type,
                    target_type=target_type_name,
                    relation=relation,
                    weight=confidence,
                    session=session,
                )
                if edge:
                    edges_created += 1

        return edges_created

    async def backfill_orphan_facts(
        self,
        max_count: int | None = None,
        ce_stats: dict[str, int] | None = None,
    ) -> int:
        """Find orphan facts and connect them to similar facts and decisions."""
        if not self._settings.graph_backfill_enabled:
            return 0
        limit = max_count or self._settings.graph_backfill_max_facts
        total = 0

        async with self.db.session() as session:
            orphans = await self.find_orphans("fact", limit, session)
            for orphan_id, content in orphans:
                if self._interrupted:
                    break
                total += await self._backfill_same_type(
                    "fact", orphan_id, content, session, ce_stats=ce_stats
                )
                total += await self._backfill_cross_type(
                    "fact", orphan_id, content, "decision", session, ce_stats=ce_stats
                )
            await session.commit()

        logger.info("F040: backfill_orphan_facts created %d edges from %d orphans", total, len(orphans))
        return total

    async def backfill_orphan_decisions(
        self,
        max_count: int | None = None,
        ce_stats: dict[str, int] | None = None,
    ) -> int:
        """Find orphan decisions and connect them to similar decisions and facts."""
        if not self._settings.graph_backfill_enabled:
            return 0
        limit = max_count or self._settings.graph_backfill_max_decisions
        total = 0

        async with self.db.session() as session:
            orphans = await self.find_orphans("decision", limit, session)
            for orphan_id, content in orphans:
                if self._interrupted:
                    break
                total += await self._backfill_same_type(
                    "decision", orphan_id, content, session, ce_stats=ce_stats
                )
                total += await self._backfill_cross_type(
                    "decision", orphan_id, content, "fact", session, ce_stats=ce_stats
                )
            await session.commit()

        logger.info("F040: backfill_orphan_decisions created %d edges from %d orphans", total, len(orphans))
        return total

    async def backfill_orphan_episodes(
        self,
        max_count: int | None = None,
        ce_stats: dict[str, int] | None = None,
    ) -> int:
        """Find orphan episodes and connect them to similar episodes and facts."""
        if not self._settings.graph_backfill_enabled:
            return 0
        limit = max_count or self._settings.graph_backfill_max_episodes
        total = 0

        async with self.db.session() as session:
            orphans = await self.find_orphans("episode", limit, session)
            for orphan_id, content in orphans:
                if self._interrupted:
                    break
                total += await self._backfill_same_type(
                    "episode", orphan_id, content, session, ce_stats=ce_stats
                )
                total += await self._backfill_cross_type(
                    "episode", orphan_id, content, "fact", session, ce_stats=ce_stats
                )
            await session.commit()

        logger.info("F040: backfill_orphan_episodes created %d edges from %d orphans", total, len(orphans))
        return total

    async def backfill_orphan_chunks(
        self,
        max_count: int | None = None,
    ) -> int:
        """F070: find orphan chunks and build their graph edges.

        Chunks live in ``heart.episode_chunks`` and are tightly coupled to a
        source episode (``episode_id`` FK). Unlike facts/decisions/episodes
        which use hybrid search to find candidates, chunk-backfill is
        structurally local: edges target same-episode facts and same-episode
        chunks. v1 builds:

          - chunk → episode (always, weight=1.0)
          - chunk → fact same-episode (cosine ≥ threshold_chunk_fact)
          - chunk ↔ chunk intra-episode (sequential at weight=1.0 +
            non-adjacent at cosine ≥ threshold_chunk_chunk_intra)

        v1 does NOT build cross-episode chunk↔chunk dedup edges yet —
        that requires hybrid search across the full chunks table and is
        deferred to F070.1 along with the persistent-dedup column.
        """
        if not self._settings.chunk_consolidation_enabled:
            return 0
        if not self._settings.graph_backfill_enabled:
            return 0

        limit = max_count or self._settings.graph_backfill_max_chunks
        thresh_chunk_fact = self._settings.graph_threshold_chunk_fact
        thresh_chunk_chunk_intra = self._settings.graph_threshold_chunk_chunk_intra
        total = 0

        async with self.db.session() as session:
            # F070: chunks must always get the structural ``part_of`` edge,
            # even when the embed call failed (heart.episode_chunks.embedding
            # is nullable). Cosine-gated sub-stages return zero rows naturally
            # when the source embedding is NULL.
            orphans = await self.find_orphans(
                "chunk", limit, session, require_embedding=False,
            )
            for orphan_id, _content in orphans:
                if self._interrupted:
                    break

                # 1. chunk → episode (always; FK guarantees the episode exists)
                ep_row = (await session.execute(
                    text(
                        "SELECT episode_id FROM heart.episode_chunks "
                        "WHERE id = :i AND agent_id = :a"
                    ),
                    {"i": orphan_id, "a": self._agent_id},
                )).first()
                if not ep_row or not ep_row.episode_id:
                    continue
                episode_id: UUID = ep_row.episode_id
                edge = await self._linker.create_edge(
                    source_id=orphan_id,
                    target_id=episode_id,
                    source_type="chunk",
                    target_type="episode",
                    relation="part_of",
                    weight=1.0,
                    session=session,
                    # FK-derived anchor — deterministic, not cosine-inferred.
                    provenance_source="structural",
                )
                if edge is not None:
                    total += 1

                # 2. chunk → fact same-episode (cosine-ranked)
                fact_edges = await self._link_chunk_to_same_episode_facts(
                    orphan_id, episode_id, thresh_chunk_fact, session,
                )
                total += fact_edges

                # 3. chunk ↔ chunk intra-episode (sequential + non-adjacent)
                chunk_edges = await self._link_chunk_to_intra_episode_chunks(
                    orphan_id, episode_id, thresh_chunk_chunk_intra, session,
                )
                total += chunk_edges

            await session.commit()

        logger.info(
            "F070: backfill_orphan_chunks created %d edges from %d orphans",
            total, len(orphans),
        )
        return total

    async def restore_episode_anchor_edges(
        self, *, dry_run: bool = False,
    ) -> dict[str, int]:
        """One-shot remediation for the F053 episode-prune bug (2026-07-12).

        The old prune predicate treated every closed episode as a dead node
        and deleted its incident edges; the orphan gate (chunks keep their
        chunk↔chunk edges → never re-orphan) made the loss permanent. This
        restores the three DETERMINISTIC edge classes directly from their FK
        ground truth — no embeddings, no LLM:

          - chunk   → episode  ``part_of``        (episode_chunks.episode_id;
            mirrors backfill_orphan_chunks step 1: weight 1.0, structural)
          - fact    → episode  ``extracted_from`` (facts.source_episode_id,
            active facts only; mirrors GraphLinker.link_episode_deterministic)
          - episode → decision ``discussed_in``   (heart.episode_decisions;
            mirrors GraphLinker.link_episode_deterministic — F053 destroyed
            these too, and no other mechanism rebuilds them)

        Cosine-inferred classes (episode↔episode related_to, episode→fact)
        are NOT restored here — they heal via
        ``scripts/backfill_f053_episode_edges.py --densify``, which MUST run
        BEFORE this method for the historical population: these anchors
        de-orphan every episode they touch, and F040's orphan gate then
        skips them forever (the same ratchet this fix diagnoses).

        Idempotent (ON CONFLICT DO NOTHING); only targets LIVE episodes.
        Returns inserted counts per relation (would-insert counts when
        ``dry_run``).
        """
        live = episode_live_sql("ep.")
        # ep.agent_id scoping is defense-in-depth (FKs cannot cross agents),
        # per the repo rule: agent-scope every side of every new query.
        selects = {
            "part_of": f"""
                SELECT c.id AS source_id, c.episode_id AS target_id,
                       'chunk' AS source_type, 'episode' AS target_type,
                       c.agent_id, 'part_of' AS relation
                FROM heart.episode_chunks c
                JOIN heart.episodes ep
                  ON ep.id = c.episode_id AND ep.agent_id = :agent_id
                WHERE c.agent_id = :agent_id AND {live}
            """,
            "extracted_from": f"""
                SELECT f.id AS source_id, f.source_episode_id AS target_id,
                       'fact' AS source_type, 'episode' AS target_type,
                       f.agent_id, 'extracted_from' AS relation
                FROM heart.facts f
                JOIN heart.episodes ep
                  ON ep.id = f.source_episode_id AND ep.agent_id = :agent_id
                WHERE f.agent_id = :agent_id AND f.active = TRUE AND {live}
            """,
            "discussed_in": f"""
                SELECT ed.episode_id AS source_id, ed.decision_id AS target_id,
                       'episode' AS source_type, 'decision' AS target_type,
                       ep.agent_id, 'discussed_in' AS relation
                FROM heart.episode_decisions ed
                JOIN heart.episodes ep
                  ON ep.id = ed.episode_id AND ep.agent_id = :agent_id
                -- Codex PR #557 P2: episode_decisions has NO agent_id column,
                -- so the decision side must be verified explicitly or a
                -- cross-agent join-table row materializes a cross-agent edge.
                JOIN brain.decisions d
                  ON d.id = ed.decision_id AND d.agent_id = :agent_id
                WHERE {live}
            """,
        }
        results: dict[str, int] = {}
        async with self.db.session() as session:
            for relation, select_sql in selects.items():
                if dry_run:
                    count_sql = text(f"""
                        SELECT count(*) FROM ({select_sql}) cand
                        WHERE NOT EXISTS (
                            SELECT 1 FROM brain.graph_edges e
                            WHERE e.agent_id = :agent_id
                              AND e.source_id = cand.source_id
                              AND e.target_id = cand.target_id
                              AND e.relation = cand.relation
                        )
                    """)
                    row = await session.execute(
                        count_sql, {"agent_id": self._agent_id},
                    )
                    results[relation] = int(row.scalar() or 0)
                    continue
                insert_sql = text(f"""
                    INSERT INTO brain.graph_edges
                        (source_id, target_id, source_type, target_type,
                         agent_id, relation, weight, auto_linked,
                         extraction_method)
                    SELECT source_id, target_id, source_type, target_type,
                           agent_id, relation, 1.0, TRUE, 'deterministic'
                    FROM ({select_sql}) cand
                    ON CONFLICT (source_id, target_id, relation) DO NOTHING
                """)
                result = await session.execute(
                    insert_sql, {"agent_id": self._agent_id},
                )
                results[relation] = result.rowcount or 0
            if not dry_run:
                await session.commit()
        if not dry_run and any(results.values()):
            logger.info(
                "F053 restore: %d part_of + %d extracted_from + "
                "%d discussed_in edges re-anchored for agent_id=%s",
                results["part_of"], results["extracted_from"],
                results["discussed_in"], self._agent_id,
            )
        return results

    async def _link_chunk_to_same_episode_facts(
        self,
        chunk_id: UUID,
        episode_id: UUID,
        threshold: float,
        session: AsyncSession,
    ) -> int:
        """Link a chunk to facts extracted from the same episode.

        Cosine similarity between chunk embedding and fact embedding; one
        edge per fact above ``threshold``. Returns count of edges created.
        """
        rows = (await session.execute(
            text(
                "SELECT f.id, "
                "  1 - (c.embedding <=> f.embedding) AS sim "
                "FROM heart.episode_chunks c, heart.facts f "
                "WHERE c.id = :chunk_id "
                "  AND c.agent_id = :a "
                "  AND f.agent_id = :a "
                "  AND f.active = true "
                "  AND f.source_episode_id = :ep_id "
                "  AND f.embedding IS NOT NULL "
                "  AND c.embedding IS NOT NULL "
                "  AND (1 - (c.embedding <=> f.embedding)) >= :thresh "
                "ORDER BY sim DESC"
            ),
            {
                "chunk_id": chunk_id, "ep_id": episode_id,
                "a": self._agent_id, "thresh": threshold,
            },
        )).all()
        created = 0
        for row in rows:
            edge = await self._linker.create_edge(
                source_id=chunk_id,
                target_id=row.id,
                source_type="chunk",
                target_type="fact",
                relation="summarized_by",
                weight=float(row.sim),
                session=session,
            )
            if edge is not None:
                created += 1
        return created

    async def _link_chunk_to_intra_episode_chunks(
        self,
        chunk_id: UUID,
        episode_id: UUID,
        cosine_threshold: float,
        session: AsyncSession,
    ) -> int:
        """Link a chunk to other chunks in the same episode.

        Two edge types:
        - Adjacent chunks (chunk_index ± 1, SAME source_kind): always
          linked, weight=1.0. The source_kind guard (codex P2, PR #495):
          dialogue chunks are MAX+1-allocated after document chunks, so the
          first dialogue chunk is numerically adjacent to the last document
          chunk — index adjacency across source kinds is an allocation
          artifact, not a structural relationship.
        - Non-adjacent (or cross-kind): cosine ≥ ``cosine_threshold``

        Returns count of edges created.
        """
        # Fetch this chunk's index for sequential check
        self_row = (await session.execute(
            text(
                "SELECT chunk_index, source_kind, source_ref "
                "FROM heart.episode_chunks "
                "WHERE id = :i AND agent_id = :a"
            ),
            {"i": chunk_id, "a": self._agent_id},
        )).first()
        if not self_row:
            return 0
        self_idx = self_row.chunk_index
        self_kind = self_row.source_kind
        self_ref = self_row.source_ref

        # Fetch sibling chunks (same episode, different chunk_index).
        # Don't filter by embedding presence: adjacent siblings must link
        # even when their embedding is NULL (sequential adjacency is
        # structural, not embedding-derived). For non-adjacent siblings
        # without an embedding, the cosine yields NULL → sim=0 → blocked
        # by the threshold gate below.
        siblings = (await session.execute(
            text(
                "SELECT id, chunk_index, source_kind, source_ref, "
                "  1 - (embedding <=> ("
                "    SELECT embedding FROM heart.episode_chunks "
                "    WHERE id = :i AND agent_id = :a"
                "  )) AS sim "
                "FROM heart.episode_chunks "
                "WHERE episode_id = :ep_id "
                "  AND agent_id = :a "
                "  AND id != :i"
            ),
            {"i": chunk_id, "ep_id": episode_id, "a": self._agent_id},
        )).all()

        created = 0
        for row in siblings:
            sibling_idx = row.chunk_index
            sim = float(row.sim or 0.0)
            # codex P2 (round 4½): same source_ref required too — two
            # documents ingested under one episode share source_kind and
            # consecutive indexes, but the last chunk of doc A and the
            # first of doc B are unrelated streams. Dialogue chunks carry
            # source_ref NULL, so None == None keeps them adjacent.
            is_adjacent = (
                abs(sibling_idx - self_idx) == 1
                and row.source_kind == self_kind
                and row.source_ref == self_ref
            )
            if is_adjacent:
                # Sequential link — always create, structural weight=1.0.
                # Override the related_to multiplier (0.8) so the persisted
                # weight matches the documented structural anchor.
                weight = 1.0
                multiplier_override: float | None = 1.0
                # chunk_index ± 1 is structural, not cosine-inferred.
                provenance: str = "structural"
            elif sim >= cosine_threshold:
                # Non-adjacent but similar enough — cosine drives weight;
                # let the global related_to multiplier (0.8) discount it.
                weight = sim
                multiplier_override = None
                provenance = "auto_linker"
            else:
                continue
            edge = await self._linker.create_edge(
                source_id=chunk_id,
                target_id=row.id,
                source_type="chunk",
                target_type="chunk",
                relation="related_to",
                weight=weight,
                session=session,
                weight_multiplier_override=multiplier_override,
                provenance_source=provenance,
            )
            if edge is not None:
                created += 1
        return created

    # ------------------------------------------------------------------
    # F070.1 — Cross-episode chunk graph edges
    # ------------------------------------------------------------------

    async def find_chunks_lacking_cross_episode_edges(
        self,
        limit: int,
        session: AsyncSession,
        exclude_ids: set[UUID] | frozenset[UUID] | None = None,
    ) -> list[tuple[UUID, str, UUID]]:
        """F070.1: chunks that have at least one edge but no cross-episode one.

        Returns ``(chunk_id, content, episode_id)`` tuples.

        Cross-episode detection covers THREE paths (codex round-1 P1):
        chunk may be source OR target of a chunk↔chunk cross-edge, plus
        chunk→fact. Missing any path would re-process already-linked chunks
        on every run, defeating idempotency.

        Codex round-2 P2: also filters ``c.embedding IS NOT NULL`` because
        both cross-episode link queries require embeddings — NULL-embedded
        chunks could never produce cross-episode edges and would otherwise
        occupy the LIMIT window forever, blocking progress.

        Codex round-3 P1: ``exclude_ids`` is the correct abstraction for
        paginating past hard-negative chunks. The earlier ``offset``
        approach skipped real candidates when a batch successfully linked
        some rows — those linked rows shifted out of the result set, and
        an offset advance jumped past their (still-unlinked) neighbors.
        Caller tracks the ``attempted`` set across batches and passes it
        here; every chunk is visited at most once per run, regardless of
        whether linking succeeded or failed.

        Implementation note: three separate ``NOT EXISTS`` clauses
        correlated on ``c.id``. This lets the Postgres planner short-
        circuit per row — as soon as ANY one cross-episode path matches
        for a given chunk, the row is excluded. Earlier attempts (UNION
        + NOT IN, big NOT EXISTS with multi-table LEFT JOIN) materialized
        intermediate sets and stalled on real corpus sizes.
        """
        params: dict[str, object] = {"a": self._agent_id, "lim": limit}
        exclude_clause = ""
        if exclude_ids:
            # Cast each UUID to text for a TEXT[] parameter so asyncpg
            # binds it as a single array — avoids the "expanding IN list
            # via SQL string interpolation" footgun.
            params["excl"] = [str(uid) for uid in exclude_ids]
            exclude_clause = (
                "AND c.id::text != ALL(CAST(:excl AS text[]))"
            )
        sql = text(
            f"""
            SELECT c.id, c.content, c.episode_id
            FROM heart.episode_chunks c
            WHERE c.agent_id = :a
              AND c.embedding IS NOT NULL
              {exclude_clause}
              AND EXISTS (
                  SELECT 1 FROM brain.graph_edges e
                  WHERE e.agent_id = :a
                    AND ((e.source_id = c.id AND e.source_type = 'chunk')
                         OR (e.target_id = c.id AND e.target_type = 'chunk'))
              )
              -- Path 1: chunk → fact in another episode
              AND NOT EXISTS (
                  SELECT 1 FROM brain.graph_edges e
                  JOIN heart.facts f
                      ON e.target_id = f.id AND f.agent_id = :a
                  WHERE e.agent_id = :a
                    AND e.source_id = c.id
                    AND e.source_type = 'chunk'
                    AND e.target_type = 'fact'
                    AND f.source_episode_id IS NOT NULL
                    AND f.source_episode_id != c.episode_id
              )
              -- Path 2: chunk → other-chunk where current chunk is SOURCE
              AND NOT EXISTS (
                  SELECT 1 FROM brain.graph_edges e
                  JOIN heart.episode_chunks other
                      ON e.target_id = other.id AND other.agent_id = :a
                  WHERE e.agent_id = :a
                    AND e.source_id = c.id
                    AND e.source_type = 'chunk'
                    AND e.target_type = 'chunk'
                    AND other.episode_id != c.episode_id
              )
              -- Path 3: chunk → other-chunk where current chunk is TARGET
              AND NOT EXISTS (
                  SELECT 1 FROM brain.graph_edges e
                  JOIN heart.episode_chunks other
                      ON e.source_id = other.id AND other.agent_id = :a
                  WHERE e.agent_id = :a
                    AND e.target_id = c.id
                    AND e.target_type = 'chunk'
                    AND e.source_type = 'chunk'
                    AND other.episode_id != c.episode_id
              )
            ORDER BY c.created_at DESC NULLS LAST
            LIMIT :lim
            """
        )
        result = await session.execute(sql, params)
        return [(row.id, row.content or "", row.episode_id) for row in result.all()]

    async def _link_chunk_to_cross_episode_facts(
        self,
        chunk_id: UUID,
        source_episode_id: UUID,
        threshold: float,
        top_k: int,
        session: AsyncSession,
    ) -> int:
        """F070.1: chunk → fact summarized_by ACROSS episodes.

        HNSW-bounded scan: take the top_k facts (excluding the source's own
        episode) by cosine, then threshold-gate. Returns count of edges created.
        Inferred provenance — cosine-derived, not structural.
        """
        rows = (await session.execute(
            text(
                "SELECT f.id, "
                "  1 - (c.embedding <=> f.embedding) AS sim "
                "FROM heart.episode_chunks c, heart.facts f "
                "WHERE c.id = :chunk_id "
                "  AND c.agent_id = :a "
                "  AND f.agent_id = :a "
                "  AND f.active = true "
                "  AND f.source_episode_id IS NOT NULL "
                "  AND f.source_episode_id != :ep_id "
                "  AND f.embedding IS NOT NULL "
                "  AND c.embedding IS NOT NULL "
                "ORDER BY c.embedding <=> f.embedding "
                "LIMIT :top_k"
            ),
            {
                "chunk_id": chunk_id, "ep_id": source_episode_id,
                "a": self._agent_id, "top_k": top_k,
            },
        )).all()
        created = 0
        for row in rows:
            sim = float(row.sim or 0.0)
            if sim < threshold:
                continue
            edge = await self._linker.create_edge(
                source_id=chunk_id,
                target_id=row.id,
                source_type="chunk",
                target_type="fact",
                relation="summarized_by",
                weight=sim,
                session=session,
                # Cosine-derived → 'inferred' tier (matches v1 same-episode).
                provenance_source="auto_linker",
            )
            if edge is not None:
                created += 1
        return created

    async def _link_chunk_to_cross_episode_chunks(
        self,
        chunk_id: UUID,
        source_episode_id: UUID,
        threshold: float,
        top_k: int,
        session: AsyncSession,
    ) -> int:
        """F070.1: chunk ↔ chunk related_to ACROSS episodes (dedup).

        Same HNSW pattern. Excludes the source chunk's own id (so a chunk
        doesn't link to itself) and same-episode siblings (handled by v1
        intra-episode method). Cosine-derived → inferred provenance.
        """
        rows = (await session.execute(
            text(
                "SELECT other.id, other.episode_id, "
                "  1 - (c.embedding <=> other.embedding) AS sim "
                "FROM heart.episode_chunks c, heart.episode_chunks other "
                "WHERE c.id = :chunk_id "
                "  AND c.agent_id = :a "
                "  AND other.agent_id = :a "
                "  AND other.id != :chunk_id "
                "  AND other.episode_id != :ep_id "
                "  AND other.embedding IS NOT NULL "
                "  AND c.embedding IS NOT NULL "
                "ORDER BY c.embedding <=> other.embedding "
                "LIMIT :top_k"
            ),
            {
                "chunk_id": chunk_id, "ep_id": source_episode_id,
                "a": self._agent_id, "top_k": top_k,
            },
        )).all()
        created = 0
        for row in rows:
            sim = float(row.sim or 0.0)
            if sim < threshold:
                continue
            edge = await self._linker.create_edge(
                source_id=chunk_id,
                target_id=row.id,
                source_type="chunk",
                target_type="chunk",
                relation="related_to",
                weight=sim,
                session=session,
                # Cross-episode chunk↔chunk is cosine-only → inferred.
                provenance_source="auto_linker",
            )
            if edge is not None:
                created += 1
        return created

    async def backfill_orphan_chunks_cross_episode(
        self,
        max_count: int | None = None,
        exclude_ids: set[UUID] | frozenset[UUID] | None = None,
    ) -> tuple[int, list[UUID]]:
        """F070.1: add cross-episode summarized_by + related_to edges.

        Returns ``(edges_created, attempted_chunk_ids)``. The caller is
        expected to extend its ``attempted`` set with the returned IDs
        and pass that set back as ``exclude_ids`` on the next call —
        ensures every chunk is visited at most once per run regardless
        of per-batch link success/failure (codex round-3 P1; the prior
        offset-based pagination skipped chunks when batches succeeded).

        Idempotent across runs: ``find_chunks_lacking_cross_episode_edges``
        skips any chunk that already has cross-episode edges; ``create_edge``
        uses ON CONFLICT DO NOTHING under the hood. Re-running after a
        partial run picks up where the last run left off.

        Returns ``(0, [])`` when ``chunk_consolidation_enabled`` or
        ``graph_backfill_enabled`` is False.
        """
        if not self._settings.chunk_consolidation_enabled:
            return 0, []
        if not self._settings.graph_backfill_enabled:
            return 0, []

        limit = max_count or self._settings.graph_backfill_max_chunks_cross_episode
        thresh_fact = self._settings.graph_threshold_chunk_fact_cross
        thresh_chunk = self._settings.graph_threshold_chunk_chunk_cross
        top_k = self._settings.chunk_cross_episode_top_k
        total = 0
        attempted: list[UUID] = []

        async with self.db.session() as session:
            candidates = await self.find_chunks_lacking_cross_episode_edges(
                limit, session, exclude_ids=exclude_ids,
            )
            for chunk_id, _content, episode_id in candidates:
                if self._interrupted:
                    break
                # Track attempted regardless of skip/fail/success so the
                # caller can exclude these chunks from the next batch
                # (codex round-3 P1 — every chunk visited at most once).
                attempted.append(chunk_id)
                if episode_id is None:
                    continue  # chunk with no parent episode — skip

                fact_edges = await self._link_chunk_to_cross_episode_facts(
                    chunk_id, episode_id, thresh_fact, top_k, session,
                )
                total += fact_edges

                chunk_edges = await self._link_chunk_to_cross_episode_chunks(
                    chunk_id, episode_id, thresh_chunk, top_k, session,
                )
                total += chunk_edges

            await session.commit()

        logger.info(
            "F070.1: backfill_orphan_chunks_cross_episode created %d edges "
            "from %d candidates",
            total, len(candidates),
        )
        return total, attempted

    async def backfill_orphan_procedures(
        self,
        max_count: int | None = None,
        ce_stats: dict[str, int] | None = None,
    ) -> int:
        """Find orphan procedures and connect them to similar nodes."""
        if not self._settings.graph_backfill_enabled:
            return 0
        limit = max_count or self._settings.graph_backfill_max_procedures
        total = 0

        async with self.db.session() as session:
            orphans = await self.find_orphans("procedure", limit, session)
            for orphan_id, content in orphans:
                if self._interrupted:
                    break
                total += await self._backfill_same_type(
                    "procedure", orphan_id, content, session, ce_stats=ce_stats
                )
                total += await self._backfill_cross_type(
                    "procedure", orphan_id, content, "fact", session, ce_stats=ce_stats
                )
                total += await self._backfill_cross_type(
                    "procedure", orphan_id, content, "decision", session, ce_stats=ce_stats
                )
            await session.commit()

        logger.info("F040: backfill_orphan_procedures created %d edges from %d orphans", total, len(orphans))
        return total

    async def run_backfill_cycle(self) -> dict:
        """Orchestrate a full backfill cycle across all entity types.

        Returns a dict mapping entity type to number of edges created plus
        a ``_ce_stats`` key carrying F043 reranker survival counters. The
        leading underscore on ``_ce_stats`` signals "not a per-type edge
        count — do not sum me" to downstream consumers that aggregate
        ``result.values()``.
        """
        self._interrupted = False
        ce_stats: dict[str, int] = {"survived": 0, "pruned": 0}
        results: dict = {}

        def _log_and_return(aborted: bool) -> dict:
            # Filter `_`-prefixed keys from the edge-total sum so CE counters
            # never inflate the per-type totals (F043 P1 regression guard).
            edge_total = sum(v for k, v in results.items() if not k.startswith("_"))
            results["_ce_stats"] = ce_stats
            per_type = {k: v for k, v in results.items() if not k.startswith("_")}
            if aborted:
                logger.info(
                    "F040: backfill cycle aborted (interrupt) — %d edges so far "
                    "(per_type=%s ce=%s)",
                    edge_total, per_type, ce_stats,
                )
            else:
                logger.info(
                    "F040: backfill cycle complete — %d total edges "
                    "(per_type=%s ce=%s)",
                    edge_total, per_type, ce_stats,
                )
            return results

        results["facts"] = await self.backfill_orphan_facts(ce_stats=ce_stats)
        if self._interrupted:
            return _log_and_return(aborted=True)

        results["decisions"] = await self.backfill_orphan_decisions(ce_stats=ce_stats)
        if self._interrupted:
            return _log_and_return(aborted=True)

        results["episodes"] = await self.backfill_orphan_episodes(ce_stats=ce_stats)
        if self._interrupted:
            return _log_and_return(aborted=True)

        results["procedures"] = await self.backfill_orphan_procedures(ce_stats=ce_stats)
        if self._interrupted:
            return _log_and_return(aborted=True)

        # F070 (2026-05-25): chunk consolidation. Gated by separate flag
        # (chunk_consolidation_enabled) so it can ship independently of the
        # main F040 backfill master switch.
        results["chunks"] = await self.backfill_orphan_chunks()
        if self._interrupted:
            return _log_and_return(aborted=True)

        # F075 (2026-05-28): chain temporal events within episode boundaries.
        # Builds `happened_before` edges from each dated fact to its
        # next-distinct-date successor in the same episode. Inert unless
        # NOUS_GRAPH_ADJACENCY_BOOST_ENABLED=true (consumer at
        # retrieval_pipeline.py:243-247) — spec line 443 documents that
        # operators must flip the boost flag for Layer 2 to take effect.
        #
        # Leading underscore on `_happened_before` keeps this temporal-chain
        # count out of the per-entity "orphan_edges_created" aggregation in
        # sleep_handler._phase_graph_densification (same convention as
        # `_ce_stats`). Codex PR #461 P2 fix.
        results["_happened_before"] = await self._build_happened_before_edges()
        if self._interrupted:
            return _log_and_return(aborted=True)

        # F076 (2026-05-30): co-mention / shared-entity associative edges.
        # Leading underscore keeps the count out of the orphan-backfill sum in
        # sleep_handler._phase_graph_densification (same convention as
        # `_ce_stats` / `_happened_before`). Default ON (comention_linking_enabled).
        # Wrapped: this default-ON, last-in-cycle pass must not mask sibling
        # backfill stats or skip downstream phases if it throws (its own edges
        # commit independently; siblings already committed). Silent-failure review.
        try:
            results["_co_mention"] = await self.build_comention_edges()
        except Exception:
            logger.warning(
                "F076: build_comention_edges failed for agent_id=%s "
                "(non-fatal; sibling backfill edges already committed)",
                self._agent_id, exc_info=True,
            )
            results["_co_mention"] = 0
        # Gap-1 formation: experiential co-occurrence edges (default OFF; same wrapping
        # discipline — own edges commit independently, must not mask siblings on throw).
        try:
            results["_co_occurrence"] = await self.build_cooccurrence_edges()
        except Exception:
            logger.warning(
                "Gap-1: build_cooccurrence_edges failed for agent_id=%s "
                "(non-fatal; sibling backfill edges already committed)",
                self._agent_id, exc_info=True,
            )
            results["_co_occurrence"] = 0
        return _log_and_return(aborted=self._interrupted)

    async def _build_happened_before_edges(self) -> int:
        """F075 Layer 2: chain temporally-adjacent dated facts within episodes.

        Each dated, active fact gets at most ONE outgoing ``happened_before``
        edge pointing to the earliest later-dated active fact in the same
        episode that is also semantically related (cosine >=
        ``happened_before_relatedness_threshold``). The relatedness gate stops
        date-order alone from chaining unrelated co-episode events (edge audit
        2026-06-13: 0.27 precision; the gate cleanly separated related
        sequences from unrelated co-episode facts on the prod sample). When the
        threshold is 0 the gate is disabled (pre-fix date-order-only behaviour,
        embedding-less facts still chain). LATERAL with ``LIMIT 1`` prevents
        quadratic explosion when multiple facts share the same ``event_date``.
        Same-date facts deliberately do not link to each other (we don't claim
        ordering on concurrent events).

        Returns the number of edges newly inserted (excluding ON CONFLICT
        DO NOTHING hits — those are no-ops on re-run).
        """
        threshold = float(
            getattr(self._settings, "happened_before_relatedness_threshold", 0.0) or 0.0
        )
        params: dict = {"agent_id": self._agent_id}
        outer_emb = ""
        lateral_rel = ""
        if threshold > 0:
            params["rel_threshold"] = threshold
            outer_emb = "AND a.embedding IS NOT NULL"
            lateral_rel = (
                "AND b.embedding IS NOT NULL "
                "AND (1 - (a.embedding <=> b.embedding)) >= :rel_threshold"
            )
        async with self.db.session() as session:
            result = await session.execute(
                text(
                    f"""
                    INSERT INTO brain.graph_edges
                        (source_id, source_type, target_id, target_type,
                         agent_id, relation, weight, auto_linked,
                         extraction_method)
                    SELECT a.id, 'fact', b.id, 'fact',
                           a.agent_id, 'happened_before', 1.0, TRUE,
                           'deterministic'
                    FROM heart.facts a
                    JOIN LATERAL (
                        SELECT b.id
                        FROM heart.facts b
                        WHERE b.agent_id = a.agent_id
                          AND b.source_episode_id = a.source_episode_id
                          AND b.event_date IS NOT NULL
                          AND b.event_date > a.event_date
                          AND b.active = TRUE
                          {lateral_rel}
                        ORDER BY b.event_date ASC, b.id ASC
                        LIMIT 1
                    ) b ON TRUE
                    WHERE a.agent_id = :agent_id
                      AND a.event_date IS NOT NULL
                      AND a.active = TRUE
                      {outer_emb}
                    ON CONFLICT (source_id, target_id, relation) DO NOTHING
                    """
                ),
                params,
            )
            await session.commit()
            count = result.rowcount or 0
            if count:
                logger.info(
                    "F075: built %d happened_before edges for agent_id=%s",
                    count, self._agent_id,
                )
            return count

    async def build_cooccurrence_edges(self, *, dry_run: bool = False) -> int:
        """Gap-1 formation: link facts learned from the SAME source episode.

        Two facts mentioned together in one conversation/occasion co-occurred — an
        experiential association the cosine-only graph misses when they share no words and
        aren't semantically near (the no-handle case). Distinct from F076 co-mention
        (shared entity): the signal is shared ``source_episode_id``, not a shared entity.
        Edges are relation='co_occurred' (carries the occasion semantics so the agent can
        contextualise, unlike generic related_to) + extraction_method='co_occurrence'.

        Noise gate: skip episodes that produced more than ``cooccurrence_max_episode_facts``
        facts — a focused chat co-mentions a few related things; a rambling one touches many
        unrelated topics, where linking all pairs is noise, not association. Pre-existing-edge
        guard (any relation, either direction) keeps it idempotent and never links a
        contradicting pair. dry_run returns the count that WOULD be inserted.
        """
        from itertools import combinations

        s = self._settings
        if not getattr(s, "cooccurrence_linking_enabled", False):
            return 0
        max_facts = int(s.cooccurrence_max_episode_facts)
        max_eps = int(s.cooccurrence_max_episodes_per_cycle)
        weight = float(s.cooccurrence_weight)

        # episode -> its active facts; focused episodes only (2..max_facts), recent first.
        async with self.db.session() as session:
            rows = (await session.execute(
                text(
                    "SELECT array_agg(id::text) AS fids FROM heart.facts "
                    "WHERE agent_id = :a AND active = TRUE AND source_episode_id IS NOT NULL "
                    "GROUP BY source_episode_id "
                    "HAVING count(*) BETWEEN 2 AND :maxf "
                    "ORDER BY max(learned_at) DESC LIMIT :lim"
                ),
                {"a": self._agent_id, "maxf": max_facts, "lim": max_eps},
            )).all()
        if not rows:
            return 0

        candidates: set[tuple[str, str]] = set()
        for (fids,) in rows:
            for a, b in combinations(sorted(fids), 2):
                candidates.add((a, b))
        if not candidates:
            return 0

        # pre-existing fact-fact edges (any relation, either direction) among the candidate
        # facts -> hard skip (no dup; never overlay a contradicts/supersedes pair, which the
        # adjacency/spreading consumers would let reinforce each other).
        all_ids = sorted({x for pair in candidates for x in pair})
        async with self.db.session() as session:
            ex = (await session.execute(
                text(
                    "SELECT source_id, target_id FROM brain.graph_edges "
                    "WHERE agent_id = :a AND source_type='fact' AND target_type='fact' "
                    "AND source_id = ANY(CAST(:ids AS uuid[])) "
                    "AND target_id = ANY(CAST(:ids AS uuid[]))"
                ),
                {"a": self._agent_id, "ids": all_ids},
            )).all()
        existing: set[tuple[str, str]] = set()
        for src, tgt in ex:
            x, y = str(src), str(tgt)
            existing.add((x, y) if x < y else (y, x))

        to_insert = [p for p in candidates if p not in existing]
        if dry_run or not to_insert:
            return len(to_insert)

        inserted = 0
        async with self.db.session() as session:
            for a, b in to_insert:
                if self._interrupted:
                    break
                await session.execute(
                    text(
                        "INSERT INTO brain.graph_edges "
                        "(source_id, target_id, source_type, target_type, agent_id, "
                        " relation, weight, auto_linked, extraction_method) "
                        "VALUES (CAST(:s AS uuid), CAST(:t AS uuid), 'fact', 'fact', :a, "
                        " 'co_occurred', :w, TRUE, 'co_occurrence') "
                        "ON CONFLICT DO NOTHING"
                    ),
                    {"s": a, "t": b, "a": self._agent_id, "w": weight},
                )
                inserted += 1
            await session.commit()
        return inserted

    async def build_comention_edges(self, *, dry_run: bool = False) -> int:
        """F076: link facts that NAME the same entity, independent of cosine.

        FACT-only by design. chunk<->chunk co-mention was considered and dropped: it
        builds a noisy, redundant edge web over overlapping raw transcript slices with
        little marginal retrieval value. The right associative node for a document is a
        distilled connector FACT (see the document-consolidation feature, F077), which
        joins THIS fact graph — not a tangle of chunk edges. Chunks stay for verbatim
        recall; consolidation gives documents a semantic identity in the fact graph.

        dry_run=True computes the candidate pairs (same caps, hub/fan-out limits, and
        prior-edge skip) and returns how many edges WOULD be inserted, without writing —
        used by scripts/backfill_comention_edges.py to preview yield.

        The associative edge the cosine-only graph misses: two facts that both mention
        "Steve Hillage" but embed below the 0.82 fact-fact threshold stay orphans. We
        extract entities from each active fact's content and link facts that share one —
        no cosine gate. Edges are relation='related_to', extraction_method='co_mention'
        (own provenance tier, escapes the F065 inferred penalty; weight written directly
        via raw INSERT).

        Conservative for a default-on builder: hub-entity degree cap, per-fact fan-out
        cap, rarer-entity-first fill (rarer = stronger signal), and a pre-existing-edge
        guard (any relation, incl. contradicts) so we never duplicate a link or join a
        contradicting pair. Idempotent across sleep cycles.

        Returns the number of new co-mention edges inserted.
        """
        from itertools import combinations

        from nous.brain.entity_extraction import extract_entities

        s = self._settings
        if not getattr(s, "comention_linking_enabled", False):
            return 0
        max_degree = int(s.comention_max_degree)
        max_per_node = int(s.comention_max_edges_per_node)
        weight = float(s.comention_weight)
        min_chars = int(s.comention_min_entity_chars)
        max_facts = int(s.comention_max_facts_per_cycle)

        async with self.db.session() as session:
            rows = (await session.execute(
                text(
                    "SELECT id, content FROM heart.facts "
                    "WHERE agent_id = :a AND active = TRUE "
                    "ORDER BY learned_at DESC, id LIMIT :lim"
                ),
                {"a": self._agent_id, "lim": max_facts},
            )).all()
        if not rows:
            return 0

        # entity -> ordered unique fact ids (deterministic).
        ent_to_facts: dict[str, list[str]] = {}
        for fid, content in rows:
            for ent in extract_entities(content or "", min_chars=min_chars):
                bucket = ent_to_facts.setdefault(ent, [])
                fid_s = str(fid)
                if fid_s not in bucket:
                    bucket.append(fid_s)

        # Pre-existing fact-fact edges of ANY relation (either direction) — skip to
        # avoid (a) duplicate undirected related_to links + churn AND (b) adding a
        # related_to edge OVER a contradicts/supersedes pair. adjacency-boost and
        # spreading-activation filter only `relation != 'contradicts'`, so a co_mention
        # related_to edge would let mutually-inconsistent facts reinforce each other.
        # Loading every relation makes any prior edge a hard skip.
        # P2-F: scope to the SCANNED facts — candidate pairs only come from `rows`, so an
        # edge can only block a pair if BOTH endpoints are in the scanned set; this bounds
        # the lookup by the per-cycle scan, not the agent's total historical edge count.
        fact_ids = [str(fid) for fid, _ in rows]
        async with self.db.session() as session:
            existing_rows = (await session.execute(
                text(
                    "SELECT source_id, target_id FROM brain.graph_edges "
                    "WHERE agent_id = :a "
                    "AND source_type = 'fact' AND target_type = 'fact' "
                    "AND source_id = ANY(CAST(:ids AS uuid[])) "
                    "AND target_id = ANY(CAST(:ids AS uuid[]))"
                ),
                {"a": self._agent_id, "ids": fact_ids},
            )).all()
        existing: set[tuple[str, str]] = set()
        for src, tgt in existing_rows:
            a, b = str(src), str(tgt)
            existing.add((a, b) if a < b else (b, a))

        # Candidate pairs: rarer entities first, skip hubs, cap per-node fan-out,
        # canonical (a < b), interrupt-aware.
        deg: dict[str, int] = {}
        to_insert: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for ent in sorted(ent_to_facts, key=lambda k: (len(ent_to_facts[k]), k)):
            fids = ent_to_facts[ent]
            if not (2 <= len(fids) <= max_degree):
                continue
            for a, b in combinations(sorted(fids), 2):
                if deg.get(a, 0) >= max_per_node or deg.get(b, 0) >= max_per_node:
                    continue
                key = (a, b)
                if key in seen or key in existing:
                    continue
                seen.add(key)
                deg[a] = deg.get(a, 0) + 1
                deg[b] = deg.get(b, 0) + 1
                to_insert.append(key)
            if self._interrupted:
                break

        if not to_insert:
            return 0
        if dry_run:
            return len(to_insert)

        insert_sql = text(
            "INSERT INTO brain.graph_edges "
            "(source_id, target_id, source_type, target_type, agent_id, "
            " relation, weight, auto_linked, extraction_method) "
            "VALUES (:s, :t, 'fact', 'fact', :a, 'related_to', :w, TRUE, 'co_mention') "
            "ON CONFLICT (source_id, target_id, relation) DO NOTHING"
        )
        inserted = 0
        BATCH = 500
        async with self.db.session() as session:
            for i in range(0, len(to_insert), BATCH):
                batch = to_insert[i:i + BATCH]
                await session.execute(
                    insert_sql,
                    [{"s": a, "t": b, "a": self._agent_id, "w": weight} for a, b in batch],
                )
                inserted += len(batch)
            await session.commit()

        if inserted:
            logger.info(
                "F076: built %d co_mention edges for agent_id=%s "
                "(%d shared entities over %d facts)",
                inserted, self._agent_id,
                sum(1 for v in ent_to_facts.values() if 2 <= len(v) <= max_degree),
                len(rows),
            )
        return inserted

    async def discover_clusters(self, max_bridges: int = 20) -> int:
        """Discover disconnected graph components and create bridge edges.

        Uses Python-side union-find to build connected components, then
        tries to bridge large components via hub-to-hub similarity.
        Rate-limited to once per 7 days.
        """
        if not self._embedder:
            return 0

        # Rate limit: skip if last run < 7 days ago
        if self._last_cluster_discovery and (
            datetime.now(UTC) - self._last_cluster_discovery
        ).days < 7:
            logger.debug("F040: skipping cluster discovery (last run < 7 days ago)")
            return 0

        async with self.db.session() as session:
            # 1. Fetch all edges. Exclude supersedes lineage (2026-06-13 audit):
            # it is not real connectivity (retrieval refuses to traverse it), so
            # unioning a replacement with its inactive predecessor would merge
            # otherwise-separate components and distort cluster discovery.
            result = await session.execute(text(
                "SELECT source_id, target_id, source_type, target_type "
                "FROM brain.graph_edges WHERE agent_id = :agent_id "
                # 2b: exclude non-associative edges so cluster discovery doesn't
                # union a replacement with its lineage/co-occurrence partner.
                f"AND {autobehavior_exclusion_sql()}"
            ), {"agent_id": self._agent_id})
            edges = result.all()

            if not edges:
                self._last_cluster_discovery = datetime.now(UTC)
                return 0

            # 2. Python union-find
            parent: dict[UUID, UUID] = {}

            def find(x: UUID) -> UUID:
                while parent.get(x, x) != x:
                    parent[x] = parent.get(parent[x], parent[x])
                    x = parent[x]
                return x

            def union(a: UUID, b: UUID) -> None:
                ra, rb = find(a), find(b)
                if ra != rb:
                    parent[ra] = rb

            node_types: dict[UUID, str] = {}
            for e in edges:
                node_types[e.source_id] = e.source_type
                node_types[e.target_id] = e.target_type
                union(e.source_id, e.target_id)

            # 3. Group by component
            components: dict[UUID, list[UUID]] = defaultdict(list)
            for node_id in node_types:
                components[find(node_id)].append(node_id)

            # 4. Filter components with 3+ nodes
            large_components = [
                nodes for nodes in components.values() if len(nodes) >= 3
            ]

            if len(large_components) < 2:
                self._last_cluster_discovery = datetime.now(UTC)
                return 0

            # 5. Find hub of each component (node with most edges)
            edge_counts: dict[UUID, int] = defaultdict(int)
            for e in edges:
                edge_counts[e.source_id] += 1
                edge_counts[e.target_id] += 1

            hubs: list[tuple[UUID, str]] = []
            for comp_nodes in large_components:
                hub_id = max(comp_nodes, key=lambda n: edge_counts.get(n, 0))
                hub_type = node_types[hub_id]
                hubs.append((hub_id, hub_type))

            # 6. Get hub content for embedding
            hub_contents: dict[UUID, str] = {}
            for hub_id, hub_type in hubs:
                config = _ENTITY_CONFIG.get(hub_type)
                if not config:
                    continue
                table, _, content_col, _ = config
                try:
                    res = await session.execute(text(
                        f"SELECT {content_col} AS content FROM {table} t WHERE t.id = :hub_id"
                    ), {"hub_id": hub_id})
                    row = res.first()
                    if row and row.content:
                        hub_contents[hub_id] = row.content
                except Exception:
                    continue

            # 7. Bridge similar hubs from different components
            bridges_created = 0
            hub_embeddings: dict[UUID, list[float]] = {}

            for hub_id, hub_type in hubs:
                if hub_id not in hub_contents:
                    continue
                template = common_template_text(hub_type, hub_contents[hub_id])
                try:
                    hub_embeddings[hub_id] = await self._embedder.embed(template)
                except Exception:
                    continue

            hub_list = [(hid, htype) for hid, htype in hubs if hid in hub_embeddings]

            for i, (hub_a, type_a) in enumerate(hub_list):
                if bridges_created >= max_bridges:
                    break
                for hub_b, type_b in hub_list[i + 1:]:
                    if bridges_created >= max_bridges:
                        break
                    # Only bridge if they're in different components
                    if find(hub_a) == find(hub_b):
                        continue

                    sim = GraphLinker._cosine_similarity(
                        hub_embeddings[hub_a], hub_embeddings[hub_b]
                    )
                    threshold = _get_threshold(self._settings, type_a, type_b)
                    if sim >= threshold:
                        relation = _get_relation(type_a, type_b)
                        edge = await self._linker.create_edge(
                            source_id=hub_a,
                            target_id=hub_b,
                            source_type=type_a,
                            target_type=type_b,
                            relation=relation,
                            weight=sim,
                            session=session,
                        )
                        if edge:
                            bridges_created += 1
                            union(hub_a, hub_b)

            await session.commit()

        self._last_cluster_discovery = datetime.now(UTC)
        logger.info("F040: cluster discovery created %d bridge edges", bridges_created)
        return bridges_created
