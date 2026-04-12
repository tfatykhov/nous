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

from nous.brain.embeddings import EmbeddingProvider
from nous.brain.graph_linker import GraphLinker, common_template_text, edge_confidence
from nous.config import Settings
from nous.storage.database import Database

logger = logging.getLogger(__name__)

# Entity configuration: (table, type_name, content_column, extra_where)
# content_column uses `t.` alias for the main table.
_ENTITY_CONFIG: dict[str, tuple[str, str, str, str]] = {
    "fact": ("heart.facts", "fact", "t.content", "t.active = true"),
    "decision": ("brain.decisions", "decision", "t.description", "1=1"),
    "episode": (
        "heart.episodes",
        "episode",
        "t.structured_summary->>'summary'",
        "t.active = true AND t.structured_summary IS NOT NULL",
    ),
    "procedure": ("heart.procedures", "procedure", "t.description", "t.active = true"),
}

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


def _get_threshold(settings: Settings, source_type: str, target_type: str) -> float:
    """Get the similarity threshold for a given type pair."""
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
        session: AsyncSession,
    ) -> int:
        """Link an orphan to similar nodes of the same type using stored embeddings."""
        config = _ENTITY_CONFIG[entity_type]
        table, type_name, content_col, extra_where = config
        threshold = _get_threshold(self._settings, entity_type, entity_type)

        sql = text(f"""
            SELECT t.id, {content_col} AS content,
                   1 - (t.embedding <=> (SELECT embedding FROM {table} WHERE id = :orphan_id)) AS similarity
            FROM {table} t
            WHERE t.agent_id = :agent_id
              AND {extra_where}
              AND t.id != :orphan_id
              AND t.embedding IS NOT NULL
              AND 1 - (t.embedding <=> (SELECT embedding FROM {table} WHERE id = :orphan_id)) >= :pre_threshold
            ORDER BY t.embedding <=> (SELECT embedding FROM {table} WHERE id = :orphan_id)
            LIMIT 5
        """)
        result = await session.execute(sql, {
            "agent_id": self._agent_id,
            "orphan_id": orphan_id,
            "pre_threshold": threshold * 0.9,
        })
        candidates = result.all()

        edges_created = 0
        relation = _get_relation(entity_type, entity_type)

        for row in candidates:
            if row.similarity >= threshold:
                edge = await self._linker.create_edge(
                    source_id=orphan_id,
                    target_id=row.id,
                    source_type=type_name,
                    target_type=type_name,
                    relation=relation,
                    weight=float(row.similarity),
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
    ) -> int:
        """Link an orphan to nodes of a different type using common-template re-embedding."""
        if not self._embedder:
            return 0

        config = _ENTITY_CONFIG[target_type]
        target_table, target_type_name, target_content_col, target_where = config
        threshold = _get_threshold(self._settings, source_type, target_type)

        # Embed source with common template for fair comparison
        source_template = common_template_text(source_type, orphan_content)
        try:
            source_embedding = await self._embedder.embed(source_template)
        except Exception:
            logger.warning("Failed to embed %s %s for cross-type backfill", source_type, orphan_id)
            return 0

        embedding_str = "[" + ",".join(str(float(v)) for v in source_embedding) + "]"

        sql = text(f"""
            SELECT t.id, {target_content_col} AS content,
                   1 - (t.embedding <=> CAST(:embedding AS vector)) AS similarity
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
        candidates = result.all()

        edges_created = 0
        relation = _get_relation(source_type, target_type)

        for row in candidates:
            if not row.content:
                continue
            # Re-embed target with common template for fair similarity
            target_template = common_template_text(target_type, row.content)
            try:
                target_embedding = await self._embedder.embed(target_template)
            except Exception:
                continue

            similarity = GraphLinker._cosine_similarity(source_embedding, target_embedding)

            if similarity >= threshold:
                # Use edge_confidence for final scoring
                confidence = edge_confidence(
                    similarity=similarity,
                    shared_tags=0,  # Tags not compared in batch backfill
                    shared_subject=False,
                    temporal_proximity_days=0.0,
                )

                edge = await self._linker.create_edge(
                    source_id=orphan_id,
                    target_id=row.id,
                    source_type=source_type,
                    target_type=target_type_name,
                    relation=relation,
                    weight=confidence,
                    session=session,
                )
                if edge:
                    edges_created += 1

        return edges_created

    async def backfill_orphan_facts(self, max_count: int | None = None) -> int:
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
                total += await self._backfill_same_type("fact", orphan_id, session)
                total += await self._backfill_cross_type("fact", orphan_id, content, "decision", session)
            await session.commit()

        logger.info("F040: backfill_orphan_facts created %d edges from %d orphans", total, len(orphans))
        return total

    async def backfill_orphan_decisions(self, max_count: int | None = None) -> int:
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
                total += await self._backfill_same_type("decision", orphan_id, session)
                total += await self._backfill_cross_type("decision", orphan_id, content, "fact", session)
            await session.commit()

        logger.info("F040: backfill_orphan_decisions created %d edges from %d orphans", total, len(orphans))
        return total

    async def backfill_orphan_episodes(self, max_count: int | None = None) -> int:
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
                total += await self._backfill_same_type("episode", orphan_id, session)
                total += await self._backfill_cross_type("episode", orphan_id, content, "fact", session)
            await session.commit()

        logger.info("F040: backfill_orphan_episodes created %d edges from %d orphans", total, len(orphans))
        return total

    async def backfill_orphan_procedures(self, max_count: int | None = None) -> int:
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
                total += await self._backfill_same_type("procedure", orphan_id, session)
                total += await self._backfill_cross_type("procedure", orphan_id, content, "fact", session)
                total += await self._backfill_cross_type("procedure", orphan_id, content, "decision", session)
            await session.commit()

        logger.info("F040: backfill_orphan_procedures created %d edges from %d orphans", total, len(orphans))
        return total

    async def run_backfill_cycle(self) -> dict[str, int]:
        """Orchestrate a full backfill cycle across all entity types.

        Returns a dict mapping entity type to number of edges created.
        """
        self._interrupted = False
        results: dict[str, int] = {}

        results["facts"] = await self.backfill_orphan_facts()
        if self._interrupted:
            return results

        results["decisions"] = await self.backfill_orphan_decisions()
        if self._interrupted:
            return results

        results["episodes"] = await self.backfill_orphan_episodes()
        if self._interrupted:
            return results

        results["procedures"] = await self.backfill_orphan_procedures()

        total = sum(results.values())
        logger.info("F040: backfill cycle complete — %d total edges created (%s)", total, results)
        return results

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
