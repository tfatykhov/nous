"""F040 — Graph Densifier: orphan backfill engine + cluster discovery.

Runs during sleep cycles to find orphan nodes (no graph edges) and
connect them to similar nodes via embedding similarity.  Also discovers
disconnected clusters and proposes bridge edges between similar hubs.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from nous.brain._entity_config import _ENTITY_CONFIG
from nous.brain.backfill_rerank import (
    ce_rerank_backfill_candidates,
    fetch_candidate_content,
)
from nous.brain.embeddings import EmbeddingProvider
from nous.brain.graph_linker import GraphLinker, common_template_text, edge_confidence
from nous.config import Settings
from nous.heart.search import hybrid_search, hybrid_search_multi  # F052
from nous.storage.database import Database

if TYPE_CHECKING:
    # F052 — forward-only to avoid the heart->brain.embeddings circular import.
    from nous.heart.heart import Heart

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
        heart: "Heart | None" = None,  # F052 — for expand_query_pairs in same-type backfill
    ) -> None:
        self.db = db
        self._linker = graph_linker
        self._embedder = embedder
        self._settings = settings
        self._agent_id = agent_id
        self._heart = heart  # F052 — None keeps pre-F052 behavior (single-query path)
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
    ) -> list[tuple[UUID, str]]:
        """Find nodes with no graph edges (orphans).

        Returns list of (id, content_text) tuples for orphan nodes.
        """
        config = _ENTITY_CONFIG.get(entity_type)
        if not config:
            return []

        table, type_name, content_col, extra_where = config

        sql = text(f"""
            SELECT t.id, {content_col} AS content
            FROM {table} t
            WHERE t.agent_id = :agent_id
              AND {extra_where}
              AND t.embedding IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1 FROM brain.graph_edges e
                  WHERE e.agent_id = :agent_id
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
        # brain.decisions has no `active` column — disable active filter for it
        has_active = entity_type != "decision"

        # F052: When enabled + Heart wired + non-empty orphan content, expand the
        # orphan into N (text, embedding) variants via Heart.expand_query_pairs and
        # route through hybrid_search_multi. The helper guarantees a non-empty
        # list and never raises (single-pair fallback on any internal failure).
        # When the helper returns the single-pair fallback `[(content, None)]`,
        # we substitute the orphan's stored embedding so the short-circuit at
        # search.py:319-332 is byte-identical to today's hybrid_search call.
        # When the feature flag is off OR heart is None OR orphan_content is
        # empty, queries_pairs falls back to a single (content, orphan_embedding)
        # pair that triggers the same short-circuit. Cosine verification at
        # :244-256 still uses orphan_embedding (NOT variant embeddings) — variants
        # only widen candidate generation. _backfill_cross_type is intentionally
        # NOT wedged in Phase 1.
        if (
            self._settings.graph_backfill_multi_embedding_enabled
            and self._heart is not None
            and orphan_content
        ):
            queries_pairs = await self._heart.expand_query_pairs(orphan_content[:500])
            # Helper contract: never None, never empty. Single-pair-with-None-
            # embedding means expansion was unavailable (disabled / no expander /
            # Haiku failed / embed_batch failed) — substitute orphan_embedding
            # so we still get vector signal.  Note: orphan_embedding may itself
            # be None for rows without an embedding — hybrid_search_multi's
            # short-circuit then routes to hybrid_search(embedding=None, ...),
            # which is the existing keyword-only path; the weight branch at
            # :259-260 already handles that via the RRF score proxy.
            if len(queries_pairs) == 1 and queries_pairs[0][1] is None:
                queries_pairs = [(orphan_content[:500], orphan_embedding)]
        else:
            queries_pairs = [
                (orphan_content[:500] if orphan_content else "", orphan_embedding)
            ]

        candidates = await hybrid_search_multi(
            session=session,
            table=table,
            queries=queries_pairs,
            agent_id=self._agent_id,
            extra_where=f"AND t.id != :orphan_id",
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
            keyword_hits = await hybrid_search(
                session=session,
                table=target_table,
                embedding=None,  # keyword-only
                query_text=orphan_content[:500],
                agent_id=self._agent_id,
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
        return _log_and_return(aborted=False)

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
            # 1. Fetch all edges
            result = await session.execute(text(
                "SELECT source_id, target_id, source_type, target_type "
                "FROM brain.graph_edges WHERE agent_id = :agent_id"
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
