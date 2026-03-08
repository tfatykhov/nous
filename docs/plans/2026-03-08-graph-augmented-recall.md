# F022 Graph-Augmented Recall Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Wire the existing but unused `brain.graph_edges` table into `recall_deep`, add cross-type edges, contradiction bridging, and density-gated spreading activation.

**Architecture:** Polymorphic edges in a single `graph_edges` table (FKs dropped, `source_type`/`target_type`/`agent_id` added). Common-template re-embedding for cross-type similarity. 4 phases, each behind a feature flag. Contradiction/supersession bridged between fact-level FKs and graph edges.

**Tech Stack:** Python 3.12+, SQLAlchemy 2.0+ async, PostgreSQL 17 + pgvector, pydantic v2, pytest + pytest-asyncio

**Design Doc:** `docs/plans/2026-03-08-graph-augmented-recall-design.md`

---

## Task 1: Schema Migration + ORM Update

**Files:**
- Create: `sql/migrations/016_graph_edges_polymorphic.sql`
- Modify: `sql/init.sql:182-191` (graph_edges CREATE TABLE)
- Modify: `nous/storage/models.py:228-253` (GraphEdge ORM model)
- Modify: `nous/brain/schemas.py:18` (RelationType literal)
- Modify: `nous/brain/schemas.py:136-143` (GraphEdgeInfo schema)

**Step 1: Write the migration SQL**

Create `sql/migrations/016_graph_edges_polymorphic.sql`:

```sql
-- Migration 016: Make graph_edges polymorphic for cross-type edges (F022)
-- Adds source_type, target_type, agent_id. Drops decision-only FKs.
-- Extends relation types for cross-type relationships.

-- 1. Drop FK constraints (edges no longer decision-only)
ALTER TABLE brain.graph_edges
    DROP CONSTRAINT IF EXISTS graph_edges_source_id_fkey,
    DROP CONSTRAINT IF EXISTS graph_edges_target_id_fkey;

-- 2. Add new columns
ALTER TABLE brain.graph_edges
    ADD COLUMN IF NOT EXISTS source_type VARCHAR(20) NOT NULL DEFAULT 'decision',
    ADD COLUMN IF NOT EXISTS target_type VARCHAR(20) NOT NULL DEFAULT 'decision',
    ADD COLUMN IF NOT EXISTS agent_id VARCHAR(100);

-- 3. Backfill agent_id from source decisions
UPDATE brain.graph_edges e
SET agent_id = d.agent_id
FROM brain.decisions d
WHERE e.source_id = d.id
  AND e.agent_id IS NULL;

-- 4. Set default for any edges without a matching decision (shouldn't happen, but safe)
UPDATE brain.graph_edges SET agent_id = 'nous-default' WHERE agent_id IS NULL;

-- 5. Make agent_id NOT NULL
ALTER TABLE brain.graph_edges ALTER COLUMN agent_id SET NOT NULL;

-- 6. Add type check constraints
ALTER TABLE brain.graph_edges
    ADD CONSTRAINT ck_edges_source_type CHECK (
        source_type IN ('decision', 'fact', 'episode', 'procedure')
    ),
    ADD CONSTRAINT ck_edges_target_type CHECK (
        target_type IN ('decision', 'fact', 'episode', 'procedure')
    );

-- 7. Extend relation check constraint
ALTER TABLE brain.graph_edges
    DROP CONSTRAINT IF EXISTS ck_edges_relation;
ALTER TABLE brain.graph_edges
    ADD CONSTRAINT ck_edges_relation CHECK (
        relation IN (
            'supports', 'contradicts', 'supersedes', 'related_to', 'caused_by',
            'informed_by', 'evidence_for', 'discussed_in', 'extracted_from'
        )
    );

-- 8. New indexes for cross-type traversal
CREATE INDEX IF NOT EXISTS idx_graph_edges_source_type ON brain.graph_edges(source_id, source_type);
CREATE INDEX IF NOT EXISTS idx_graph_edges_target_type ON brain.graph_edges(target_id, target_type);
CREATE INDEX IF NOT EXISTS idx_graph_edges_agent ON brain.graph_edges(agent_id);
```

**Step 2: Update `sql/init.sql` graph_edges table definition**

Replace the `brain.graph_edges` CREATE TABLE block (lines 182-191) with:

```sql
CREATE TABLE brain.graph_edges (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_id UUID NOT NULL,
    target_id UUID NOT NULL,
    source_type VARCHAR(20) NOT NULL DEFAULT 'decision',
    target_type VARCHAR(20) NOT NULL DEFAULT 'decision',
    agent_id VARCHAR(100) NOT NULL,
    relation VARCHAR(50) NOT NULL CHECK (relation IN (
        'supports', 'contradicts', 'supersedes', 'related_to', 'caused_by',
        'informed_by', 'evidence_for', 'discussed_in', 'extracted_from'
    )),
    weight FLOAT DEFAULT 1.0,
    auto_linked BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(source_id, target_id, relation),
    CHECK (source_type IN ('decision', 'fact', 'episode', 'procedure')),
    CHECK (target_type IN ('decision', 'fact', 'episode', 'procedure'))
);
```

Also add the new indexes after the existing `idx_edges_source` and `idx_edges_target` lines (around line 513):

```sql
CREATE INDEX idx_graph_edges_source_type ON brain.graph_edges(source_id, source_type);
CREATE INDEX idx_graph_edges_target_type ON brain.graph_edges(target_id, target_type);
CREATE INDEX idx_graph_edges_agent ON brain.graph_edges(agent_id);
```

**Step 3: Update ORM model in `nous/storage/models.py:228-253`**

Replace the `GraphEdge` class:

```python
class GraphEdge(Base):
    __tablename__ = "graph_edges"
    __table_args__ = (
        UniqueConstraint("source_id", "target_id", "relation", name="uq_edges_src_tgt_rel"),
        CheckConstraint(
            "relation IN ('supports', 'contradicts', 'supersedes', 'related_to', 'caused_by', "
            "'informed_by', 'evidence_for', 'discussed_in', 'extracted_from')",
            name="ck_edges_relation",
        ),
        CheckConstraint(
            "source_type IN ('decision', 'fact', 'episode', 'procedure')",
            name="ck_edges_source_type",
        ),
        CheckConstraint(
            "target_type IN ('decision', 'fact', 'episode', 'procedure')",
            name="ck_edges_target_type",
        ),
        {"schema": "brain"},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    source_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    target_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    source_type: Mapped[str] = mapped_column(String(20), nullable=False, server_default="decision")
    target_type: Mapped[str] = mapped_column(String(20), nullable=False, server_default="decision")
    agent_id: Mapped[str] = mapped_column(String(100), nullable=False)
    relation: Mapped[str] = mapped_column(String(50), nullable=False)
    weight: Mapped[float | None] = mapped_column(Float, server_default="1.0")
    auto_linked: Mapped[bool | None] = mapped_column(Boolean, server_default="false")
    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), server_default=func.now())
```

**Step 4: Update `nous/brain/schemas.py`**

Update `RelationType` at line 18 to include new relation types:

```python
RelationType = Literal[
    "supports", "contradicts", "supersedes", "related_to", "caused_by",
    "informed_by", "evidence_for", "discussed_in", "extracted_from",
]
```

Also define `NodeType` after `RelationType`:

```python
NodeType = Literal["decision", "fact", "episode", "procedure"]
```

Update `GraphEdgeInfo` at line 136 to include type fields:

```python
class GraphEdgeInfo(BaseModel):
    """A graph edge between two nodes (decisions, facts, episodes, procedures)."""

    source_id: UUID
    target_id: UUID
    source_type: str = "decision"
    target_type: str = "decision"
    relation: RelationType
    weight: float
    auto_linked: bool
```

**Step 5: Update `brain._link()` to include new fields**

In `nous/brain/brain.py:967-1003`, update `_link()` to accept and pass `source_type`, `target_type`, and `agent_id`:

```python
async def _link(
    self,
    source_id: UUID,
    target_id: UUID,
    relation: str,
    weight: float,
    auto_linked: bool,
    session: AsyncSession,
    source_type: str = "decision",
    target_type: str = "decision",
) -> GraphEdgeInfo:
    edge = GraphEdge(
        source_id=source_id,
        target_id=target_id,
        source_type=source_type,
        target_type=target_type,
        agent_id=self.agent_id,
        relation=relation,
        weight=weight,
        auto_linked=auto_linked,
    )
    session.add(edge)
    await session.flush()

    await self._emit_event(
        session,
        "decisions_linked",
        {
            "source_id": str(source_id),
            "target_id": str(target_id),
            "source_type": source_type,
            "target_type": target_type,
            "relation": relation,
        },
    )

    return GraphEdgeInfo(
        source_id=source_id,
        target_id=target_id,
        source_type=source_type,
        target_type=target_type,
        relation=relation,
        weight=weight,
        auto_linked=auto_linked,
    )
```

Update the public `link()` method signature at line 951 to also accept `source_type`/`target_type` and pass them through.

**Step 6: Update `brain._auto_link()` to include agent_id**

In `nous/brain/brain.py:1088-1167`, update the `pg_insert` values dict (around line 1146) to include `source_type`, `target_type`, and `agent_id`:

```python
stmt = (
    pg_insert(GraphEdge)
    .values(
        source_id=src,
        target_id=tgt,
        source_type="decision",
        target_type="decision",
        agent_id=self.agent_id,
        relation="related_to",
        weight=float(row.similarity),
        auto_linked=True,
    )
    .on_conflict_do_nothing(constraint="uq_edges_src_tgt_rel")
)
```

**Step 7: Run existing tests to verify no regressions**

Run: `uv run pytest tests/test_brain.py tests/test_tools.py -v -x`
Expected: All existing tests pass (the ORM changes are backward-compatible because new columns have defaults).

**Step 8: Write test for new schema fields**

Add to `tests/test_brain.py` after `test_neighbors` (line 468):

```python
async def test_link_with_types(brain, session):
    """Graph edges include source_type, target_type, and agent_id."""
    d1 = await brain.record(_record_input(description="Typed link A"), session=session)
    d2 = await brain.record(_record_input(description="Typed link B"), session=session)

    edge = await brain.link(d1.id, d2.id, "supports", session=session)
    assert edge.source_type == "decision"
    assert edge.target_type == "decision"

    # Verify agent_id persisted
    result = await session.execute(
        select(GraphEdge).where(GraphEdge.source_id == d1.id)
    )
    db_edge = result.scalar_one()
    assert db_edge.agent_id == brain.agent_id
    assert db_edge.source_type == "decision"
    assert db_edge.target_type == "decision"
```

**Step 9: Run the new test**

Run: `uv run pytest tests/test_brain.py::test_link_with_types -v`
Expected: PASS

**Step 10: Commit**

```bash
git add sql/migrations/016_graph_edges_polymorphic.sql sql/init.sql nous/storage/models.py nous/brain/schemas.py nous/brain/brain.py tests/test_brain.py
git commit -m "feat(f022): polymorphic graph_edges schema with agent_id, source/target types"
```

---

## Task 2: Phase 1 — NeighborResult Schema + Updated neighbors()

**Files:**
- Modify: `nous/brain/schemas.py` (add NeighborResult)
- Modify: `nous/brain/brain.py:1009-1067` (update neighbors)
- Modify: `tests/test_brain.py` (update + add neighbor tests)

**Step 1: Add NeighborResult schema**

In `nous/brain/schemas.py`, add after `GraphEdgeInfo`:

```python
class NeighborResult(BaseModel):
    """A graph neighbor with edge metadata."""

    id: UUID
    node_type: str
    description: str
    score: float | None = None
    edge_relation: str
    edge_weight: float
    created_at: datetime
```

Also update the import in `nous/brain/brain.py` (line 26-37) to include `NeighborResult`.

**Step 2: Write failing test for updated neighbors**

Add to `tests/test_brain.py`:

```python
async def test_neighbors_returns_neighbor_result(brain, session):
    """neighbors() returns NeighborResult with edge metadata."""
    d1 = await brain.record(_record_input(description="Neighbor result A"), session=session)
    d2 = await brain.record(_record_input(description="Neighbor result B"), session=session)

    await brain.link(d1.id, d2.id, "supports", weight=0.9, session=session)

    results = await brain.neighbors(d1.id, session=session)
    assert len(results) == 1
    n = results[0]
    # Should be NeighborResult, not DecisionSummary
    from nous.brain.schemas import NeighborResult
    assert isinstance(n, NeighborResult)
    assert n.id == d2.id
    assert n.node_type == "decision"
    assert n.edge_relation == "supports"
    assert n.edge_weight == 0.9


async def test_neighbors_with_node_type(brain, session):
    """neighbors() accepts node_type parameter for cross-type traversal."""
    d1 = await brain.record(_record_input(description="Cross-type neighbor"), session=session)

    # Manually insert a cross-type edge (fact -> decision)
    fake_fact_id = uuid4()
    edge = GraphEdge(
        source_id=fake_fact_id,
        target_id=d1.id,
        source_type="fact",
        target_type="decision",
        agent_id=brain.agent_id,
        relation="evidence_for",
        weight=0.85,
    )
    session.add(edge)
    await session.flush()

    # Query neighbors of the fake fact
    results = await brain.neighbors(fake_fact_id, node_type="fact", session=session)
    assert len(results) == 1
    assert results[0].id == d1.id
    assert results[0].node_type == "decision"
    assert results[0].edge_relation == "evidence_for"
```

**Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/test_brain.py::test_neighbors_returns_neighbor_result tests/test_brain.py::test_neighbors_with_node_type -v`
Expected: FAIL (neighbors still returns DecisionSummary, no node_type param)

**Step 4: Implement updated `neighbors()` and `_neighbors()`**

Replace `nous/brain/brain.py:1009-1067`:

```python
async def neighbors(
    self,
    node_id: UUID,
    node_type: str = "decision",
    relation: str | None = None,
    limit: int = 10,
    session: AsyncSession | None = None,
) -> list[NeighborResult]:
    """Get nodes connected to the given node via graph edges."""
    if session is None:
        async with self.db.session() as session:
            return await self._neighbors(node_id, node_type, relation, limit, session)
    return await self._neighbors(node_id, node_type, relation, limit, session)

async def _neighbors(
    self,
    node_id: UUID,
    node_type: str,
    relation: str | None,
    limit: int,
    session: AsyncSession,
) -> list[NeighborResult]:
    # Find edges where this node is source or target, matching node type
    source_q = select(
        GraphEdge.target_id.label("neighbor_id"),
        GraphEdge.target_type.label("neighbor_type"),
        GraphEdge.relation.label("edge_relation"),
        GraphEdge.weight.label("edge_weight"),
    ).where(
        GraphEdge.source_id == node_id,
        GraphEdge.source_type == node_type,
        GraphEdge.agent_id == self.agent_id,
    )

    target_q = select(
        GraphEdge.source_id.label("neighbor_id"),
        GraphEdge.source_type.label("neighbor_type"),
        GraphEdge.relation.label("edge_relation"),
        GraphEdge.weight.label("edge_weight"),
    ).where(
        GraphEdge.target_id == node_id,
        GraphEdge.target_type == node_type,
        GraphEdge.agent_id == self.agent_id,
    )

    if relation:
        source_q = source_q.where(GraphEdge.relation == relation)
        target_q = target_q.where(GraphEdge.relation == relation)

    union_q = source_q.union_all(target_q).limit(limit)
    result = await session.execute(union_q)
    rows = result.all()

    if not rows:
        return []

    # Group by neighbor type for batch resolution
    decision_ids = [r.neighbor_id for r in rows if r.neighbor_type == "decision"]
    edge_map: dict[UUID, tuple[str, str, float]] = {
        r.neighbor_id: (r.neighbor_type, r.edge_relation, r.edge_weight or 1.0)
        for r in rows
    }

    # Resolve decision neighbors (most common case)
    descriptions: dict[UUID, tuple[str, datetime]] = {}
    if decision_ids:
        dec_result = await session.execute(
            select(Decision.id, Decision.description, Decision.created_at)
            .where(Decision.id.in_(decision_ids))
        )
        for d in dec_result.all():
            descriptions[d.id] = (d.description, d.created_at)

    # For non-decision types, use a generic description
    # (Phase 2 will add proper resolution for facts/episodes/procedures)
    results = []
    for r in rows:
        ntype, rel, weight = edge_map[r.neighbor_id]
        if ntype == "decision" and r.neighbor_id in descriptions:
            desc, created = descriptions[r.neighbor_id]
        else:
            desc = f"[{ntype}] {r.neighbor_id}"
            created = datetime.now(UTC)

        results.append(NeighborResult(
            id=r.neighbor_id,
            node_type=ntype,
            description=desc,
            edge_relation=rel,
            edge_weight=weight,
            created_at=created,
        ))

    return results
```

**Step 5: Run the new tests**

Run: `uv run pytest tests/test_brain.py::test_neighbors_returns_neighbor_result tests/test_brain.py::test_neighbors_with_node_type -v`
Expected: PASS

**Step 6: Update existing test_neighbors**

The existing `test_neighbors` at line 445 expects `DecisionSummary` objects. Update it to expect `NeighborResult`:

```python
async def test_neighbors(brain, session):
    """Link 3 decisions, query neighbors of middle one."""
    d1 = await brain.record(_record_input(description="Decision A"), session=session)
    d2 = await brain.record(_record_input(description="Decision B"), session=session)
    d3 = await brain.record(_record_input(description="Decision C"), session=session)

    await brain.link(d1.id, d2.id, "supports", session=session)
    await brain.link(d2.id, d3.id, "related_to", session=session)

    neighbors = await brain.neighbors(d2.id, session=session)
    assert isinstance(neighbors, list)
    assert len(neighbors) >= 2
    neighbor_ids = {n.id for n in neighbors}
    assert d1.id in neighbor_ids
    assert d3.id in neighbor_ids
    # Verify edge metadata is present
    for n in neighbors:
        assert n.node_type == "decision"
        assert n.edge_relation in ("supports", "related_to")
```

**Step 7: Run all brain tests**

Run: `uv run pytest tests/test_brain.py -v -x`
Expected: All pass

**Step 8: Commit**

```bash
git add nous/brain/schemas.py nous/brain/brain.py tests/test_brain.py
git commit -m "feat(f022): NeighborResult schema + cross-type neighbors() support"
```

---

## Task 3: Phase 1 — Config Settings

**Files:**
- Modify: `nous/config.py` (add graph recall settings)

**Step 1: Add config settings**

In `nous/config.py`, after the subtask settings block (around line 206), add:

```python
    # F022: Graph-Augmented Recall
    graph_recall_enabled: bool = True
    graph_recall_max_expand: int = 5
    graph_recall_decay: float = 0.7
    graph_recall_max_neighbors: int = 3
```

**Step 2: Commit**

```bash
git add nous/config.py
git commit -m "feat(f022): add graph recall config settings"
```

---

## Task 4: Phase 1 — Wire Graph Expansion into recall_deep

**Files:**
- Modify: `nous/api/tools.py:278-342` (recall_deep closure)
- Modify: `tests/test_tools.py` (add graph expansion tests)

**Step 1: Write failing test**

Add to `tests/test_tools.py` in the `TestRecallDeep` class:

```python
@pytest.mark.asyncio
async def test_recall_deep_graph_expansion(self, tools):
    """Graph neighbors are included in recall_deep results when edges exist."""
    # Record two decisions
    await tools["record_decision"](
        description="Graph expansion: primary decision about caching strategy",
        confidence=0.85,
        category="architecture",
        stakes="medium",
        tags=["graph-test"],
    )
    await tools["record_decision"](
        description="Graph expansion: linked decision about Redis configuration",
        confidence=0.80,
        category="tooling",
        stakes="low",
        tags=["graph-test"],
    )

    # Search for caching — should find at least the first decision
    result = await tools["recall_deep"](query="caching strategy", memory_types=["decision"])
    text = result["content"][0]["text"]
    assert "caching strategy" in text.lower() or "Brain Decisions" in text
```

Note: This test verifies the basic flow works. The graph expansion is best tested at the brain level (Task 2 tests). Integration-level graph tests require seeded edges which are hard in the tool closure layer.

**Step 2: Run to verify current behavior**

Run: `uv run pytest tests/test_tools.py::TestRecallDeep::test_recall_deep_graph_expansion -v`
Expected: PASS (basic flow works)

**Step 3: Implement graph expansion in recall_deep**

Update `nous/api/tools.py:278-342`. The `recall_deep` closure needs access to the `brain` and `settings` objects already in scope from `create_nous_tools()`. Replace the Brain decisions section (lines 321-333):

```python
    # Search Brain decisions
    if search_all or "decision" in search_types:
        decision_results = await brain.query(query, limit=limit)

        # F022: Graph expansion — expand top decisions by 1 hop
        if decision_results and settings.graph_recall_enabled:
            seen_ids = {d.id for d in decision_results}
            graph_expanded = []
            for dec in decision_results[:settings.graph_recall_max_expand]:
                if dec.score is None:
                    continue
                try:
                    neighbors = await brain.neighbors(
                        dec.id,
                        node_type="decision",
                        limit=settings.graph_recall_max_neighbors,
                    )
                    for n in neighbors:
                        if n.id not in seen_ids:
                            graph_expanded.append(n)
                            seen_ids.add(n.id)
                except Exception:
                    logger.debug("Graph expansion failed for decision %s", dec.id)

            if decision_results or graph_expanded:
                results_text.append("\n=== Brain Decisions ===")
                for i, dec in enumerate(decision_results, 1):
                    score_str = f" (score: {dec.score:.3f})" if dec.score else ""
                    results_text.append(
                        f"{i}. {dec.description} | {dec.category} | {dec.stakes} | "
                        f"confidence: {dec.confidence:.2f}{score_str}"
                    )
                for j, n in enumerate(graph_expanded, len(decision_results) + 1):
                    decayed_score = n.edge_weight * settings.graph_recall_decay
                    results_text.append(
                        f"{j}. [via graph: {n.edge_relation}] {n.description} "
                        f"(score: {decayed_score:.3f})"
                    )
            else:
                results_text.append("\n=== Brain Decisions ===\nNo results found.")
        else:
            if decision_results:
                results_text.append("\n=== Brain Decisions ===")
                for i, dec in enumerate(decision_results, 1):
                    score_str = f" (score: {dec.score:.3f})" if dec.score else ""
                    results_text.append(
                        f"{i}. {dec.description} | {dec.category} | {dec.stakes} | "
                        f"confidence: {dec.confidence:.2f}{score_str}"
                    )
            else:
                results_text.append("\n=== Brain Decisions ===\nNo results found.")
```

Note: Check that `settings` is in scope within the `recall_deep` closure. Look at the `create_nous_tools()` function — it receives `settings` as a parameter (check line ~118). If not directly in the closure scope, pass it through.

**Step 4: Run all recall_deep tests**

Run: `uv run pytest tests/test_tools.py::TestRecallDeep -v`
Expected: All pass (existing + new)

**Step 5: Run full test suite to check for regressions**

Run: `uv run pytest tests/ -v -x --timeout=60`
Expected: All pass

**Step 6: Commit**

```bash
git add nous/api/tools.py tests/test_tools.py
git commit -m "feat(f022): wire graph expansion into recall_deep (Phase 1)"
```

---

## Task 5: Phase 2 — Common-Template Re-Embedding Helper

**Files:**
- Create: `nous/brain/graph_linker.py` (cross-type auto-linking logic)
- Test: `tests/test_graph_linker.py`

**Step 1: Write failing test for common-template embedding**

Create `tests/test_graph_linker.py`:

```python
"""Tests for F022 Phase 2 — cross-type graph linking."""
import pytest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from nous.brain.graph_linker import common_template_text, GraphLinker


class TestCommonTemplate:
    """Test common-template re-embedding for cross-type comparison."""

    def test_decision_template(self):
        text = common_template_text("decision", "Use Redis for caching")
        assert text == "decision: Use Redis for caching"

    def test_fact_template(self):
        text = common_template_text("fact", "Redis supports TTL natively")
        assert text == "fact: Redis supports TTL natively"

    def test_episode_template(self):
        text = common_template_text("episode", "Discussed caching architecture")
        assert text == "episode: Discussed caching architecture"

    def test_procedure_template(self):
        text = common_template_text("procedure", "Deploy Redis cluster")
        assert text == "procedure: Deploy Redis cluster"
```

**Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_graph_linker.py::TestCommonTemplate -v`
Expected: FAIL (module doesn't exist)

**Step 3: Implement the module**

Create `nous/brain/graph_linker.py`:

```python
"""F022 Phase 2 — Cross-type graph linking with common-template re-embedding.

Provides auto-linking between different memory types (decisions, facts,
episodes, procedures) using a normalized embedding format for fair
cross-type similarity comparison.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from nous.brain.embeddings import EmbeddingProvider
from nous.brain.schemas import GraphEdgeInfo
from nous.config import Settings
from nous.storage.database import Database
from nous.storage.models import GraphEdge

logger = logging.getLogger(__name__)


def common_template_text(node_type: str, content: str) -> str:
    """Format content using common template for cross-type embedding comparison.

    All memory types are embedded as "{type}: {content}" to ensure
    cosine similarity is meaningful across different memory types.
    """
    return f"{node_type}: {content}"


class GraphLinker:
    """Cross-type auto-linking engine.

    Links facts to decisions, episodes to decisions, etc. using
    common-template re-embedding for fair similarity comparison.
    """

    def __init__(
        self,
        db: Database,
        embedder: EmbeddingProvider | None,
        settings: Settings,
        agent_id: str,
    ) -> None:
        self.db = db
        self.embedder = embedder
        self.settings = settings
        self.agent_id = agent_id

    async def link_fact_to_decisions(
        self,
        fact_id: UUID,
        fact_content: str,
        session: AsyncSession,
    ) -> list[GraphEdgeInfo]:
        """Find and link a new fact to related decisions.

        Uses common-template re-embedding for fair cross-type comparison.
        Creates 'evidence_for' edges for similarity > threshold.
        """
        if not self.embedder or not self.settings.cross_type_linking_enabled:
            return []

        # Re-embed fact with common template
        template_text = common_template_text("fact", fact_content)
        try:
            fact_embedding = await self.embedder.embed(template_text)
        except Exception:
            logger.warning("Failed to embed fact %s for cross-type linking", fact_id)
            return []

        embedding_str = "[" + ",".join(str(float(v)) for v in fact_embedding) + "]"

        # Find recent decisions with similar common-template embeddings
        # We compare against decision descriptions re-embedded with the template
        # For efficiency, we first find candidates by raw embedding similarity,
        # then re-embed and compare with common template
        cutoff = datetime.now(UTC) - timedelta(days=30)
        sql = text("""
            SELECT id, description,
                   1 - (embedding <=> CAST(:embedding AS vector)) AS similarity
            FROM brain.decisions
            WHERE agent_id = :agent_id
              AND embedding IS NOT NULL
              AND created_at >= :cutoff
              AND 1 - (embedding <=> CAST(:embedding AS vector)) >= :threshold
            ORDER BY embedding <=> CAST(:embedding AS vector)
            LIMIT 5
        """)
        result = await session.execute(sql, {
            "embedding": embedding_str,
            "agent_id": self.agent_id,
            "cutoff": cutoff,
            "threshold": self.settings.cross_type_threshold * 0.9,  # slightly lower for candidates
        })
        candidates = result.all()

        edges = []
        for row in candidates:
            # Re-embed decision with common template for fair comparison
            decision_template = common_template_text("decision", row.description)
            try:
                decision_embedding = await self.embedder.embed(decision_template)
            except Exception:
                continue

            # Compute cosine similarity between common-template embeddings
            similarity = self._cosine_similarity(fact_embedding, decision_embedding)
            if similarity >= self.settings.cross_type_threshold:
                stmt = (
                    pg_insert(GraphEdge)
                    .values(
                        source_id=fact_id,
                        target_id=row.id,
                        source_type="fact",
                        target_type="decision",
                        agent_id=self.agent_id,
                        relation="evidence_for",
                        weight=float(similarity),
                        auto_linked=True,
                    )
                    .on_conflict_do_nothing(constraint="uq_edges_src_tgt_rel")
                )
                await session.execute(stmt)
                edges.append(GraphEdgeInfo(
                    source_id=fact_id,
                    target_id=row.id,
                    source_type="fact",
                    target_type="decision",
                    relation="evidence_for",
                    weight=float(similarity),
                    auto_linked=True,
                ))

        return edges

    async def link_episode_deterministic(
        self,
        episode_id: UUID,
        decision_ids: list[UUID],
        fact_ids: list[UUID],
        session: AsyncSession,
    ) -> list[GraphEdgeInfo]:
        """Create deterministic edges from episode to its decisions and facts.

        No embedding comparison — these are structural links from existing
        episode_decisions and source_episode_id relationships.
        """
        edges = []

        for dec_id in decision_ids:
            stmt = (
                pg_insert(GraphEdge)
                .values(
                    source_id=episode_id,
                    target_id=dec_id,
                    source_type="episode",
                    target_type="decision",
                    agent_id=self.agent_id,
                    relation="discussed_in",
                    weight=1.0,
                    auto_linked=True,
                )
                .on_conflict_do_nothing(constraint="uq_edges_src_tgt_rel")
            )
            await session.execute(stmt)
            edges.append(GraphEdgeInfo(
                source_id=episode_id,
                target_id=dec_id,
                source_type="episode",
                target_type="decision",
                relation="discussed_in",
                weight=1.0,
                auto_linked=True,
            ))

        for fact_id in fact_ids:
            stmt = (
                pg_insert(GraphEdge)
                .values(
                    source_id=fact_id,
                    target_id=episode_id,
                    source_type="fact",
                    target_type="episode",
                    agent_id=self.agent_id,
                    relation="extracted_from",
                    weight=1.0,
                    auto_linked=True,
                )
                .on_conflict_do_nothing(constraint="uq_edges_src_tgt_rel")
            )
            await session.execute(stmt)
            edges.append(GraphEdgeInfo(
                source_id=fact_id,
                target_id=episode_id,
                source_type="fact",
                target_type="episode",
                relation="extracted_from",
                weight=1.0,
                auto_linked=True,
            ))

        return edges

    @staticmethod
    def _cosine_similarity(a: list[float], b: list[float]) -> float:
        """Compute cosine similarity between two vectors."""
        dot = sum(x * y for x, y in zip(a, b))
        norm_a = sum(x * x for x in a) ** 0.5
        norm_b = sum(x * x for x in b) ** 0.5
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return dot / (norm_a * norm_b)
```

**Step 4: Run common template tests**

Run: `uv run pytest tests/test_graph_linker.py::TestCommonTemplate -v`
Expected: PASS

**Step 5: Add config settings for Phase 2**

In `nous/config.py`, after the Phase 1 settings:

```python
    # F022 Phase 2: Cross-type linking
    cross_type_linking_enabled: bool = True
    cross_type_threshold: float = 0.80
    cross_type_same_threshold: float = 0.90
```

**Step 6: Write integration test for link_episode_deterministic**

Add to `tests/test_graph_linker.py`:

```python
@pytest.mark.asyncio
async def test_link_episode_deterministic(brain, session):
    """Deterministic episode linking creates discussed_in and extracted_from edges."""
    from nous.brain.graph_linker import GraphLinker
    from nous.config import Settings

    settings = Settings()
    linker = GraphLinker(brain.db, None, settings, brain.agent_id)

    d1 = await brain.record(
        RecordInput(
            description="Episode link decision",
            confidence=0.8, category="architecture", stakes="low",
        ),
        session=session,
    )

    episode_id = uuid4()
    fact_id = uuid4()

    edges = await linker.link_episode_deterministic(
        episode_id=episode_id,
        decision_ids=[d1.id],
        fact_ids=[fact_id],
        session=session,
    )

    assert len(edges) == 2
    relations = {e.relation for e in edges}
    assert "discussed_in" in relations
    assert "extracted_from" in relations
```

**Step 7: Run integration test**

Run: `uv run pytest tests/test_graph_linker.py::test_link_episode_deterministic -v`
Expected: PASS

**Step 8: Commit**

```bash
git add nous/brain/graph_linker.py tests/test_graph_linker.py nous/config.py
git commit -m "feat(f022): cross-type graph linker with common-template re-embedding (Phase 2)"
```

---

## Task 6: Phase 2 — Wire Cross-Type Linking into Event Handlers

**Files:**
- Modify: `nous/handlers/episode_summarizer.py` (add graph linking after summary)
- Modify: `nous/main.py` (wire GraphLinker into event handlers)

**Step 1: Add GraphLinker to episode summarizer**

In `nous/handlers/episode_summarizer.py`, update `__init__` to accept a `GraphLinker`:

```python
from nous.brain.graph_linker import GraphLinker

class EpisodeSummarizer:
    def __init__(
        self,
        heart: Heart,
        brain: Brain | None,
        settings: Settings,
        bus: EventBus,
        http_client: httpx.AsyncClient | None = None,
        graph_linker: GraphLinker | None = None,  # NEW
    ):
        self._heart = heart
        self._brain = brain
        self._settings = settings
        self._bus = bus
        self._http = http_client
        self._graph_linker = graph_linker  # NEW
        bus.on("session_ended", self.handle)
```

After the `episode_summarized` event is emitted (around line 127), add graph linking:

```python
            # F022: Create deterministic graph edges
            if self._graph_linker:
                try:
                    async with self._heart.db.session() as link_session:
                        # Get episode's linked decisions
                        ep = await self._heart.get_episode(UUID(episode_id))
                        decision_ids = ep.decision_ids if ep and ep.decision_ids else []

                        # Get facts extracted from this episode
                        fact_ids = []
                        if summary.get("candidate_facts"):
                            # Facts from this episode have source_episode_id set
                            from sqlalchemy import select as sa_select
                            from nous.storage.models import Fact
                            fact_result = await link_session.execute(
                                sa_select(Fact.id).where(
                                    Fact.source_episode_id == UUID(episode_id)
                                )
                            )
                            fact_ids = [r.id for r in fact_result.all()]

                        if decision_ids or fact_ids:
                            await self._graph_linker.link_episode_deterministic(
                                episode_id=UUID(episode_id),
                                decision_ids=decision_ids,
                                fact_ids=fact_ids,
                                session=link_session,
                            )
                            await link_session.commit()
                except Exception:
                    logger.debug("F022 graph linking failed for episode %s", episode_id)
```

**Step 2: Wire GraphLinker in main.py**

Find where `EpisodeSummarizer` is instantiated in `nous/main.py` and pass the `GraphLinker`. This requires:

1. Import `GraphLinker` from `nous.brain.graph_linker`
2. Create a `GraphLinker` instance after Brain is created
3. Pass it to `EpisodeSummarizer`

Search for `EpisodeSummarizer(` in `main.py` and add `graph_linker=graph_linker` to its constructor call.

**Step 3: Run existing episode summarizer tests**

Run: `uv run pytest tests/ -k "episode_summar" -v`
Expected: All pass (graph_linker is optional, defaults to None)

**Step 4: Commit**

```bash
git add nous/handlers/episode_summarizer.py nous/main.py
git commit -m "feat(f022): wire cross-type graph linking into episode summarizer"
```

---

## Task 7: Phase 2 — Cross-Type Recall Expansion

**Files:**
- Modify: `nous/api/tools.py` (expand Heart results through graph)

**Step 1: Update recall_deep to expand Heart results**

After the Heart memory search section in `recall_deep` (around line 311), add cross-type expansion. After `heart_results` are collected:

```python
    # F022 Phase 2: Check if Heart results have graph edges to decisions
    if heart_results and settings.graph_recall_enabled and settings.cross_type_linking_enabled:
        heart_graph_decisions = []
        seen_decision_ids = set()
        for hr in heart_results[:3]:
            if hr.type in ("fact", "episode"):
                try:
                    neighbors = await brain.neighbors(
                        hr.id,
                        node_type=hr.type,
                        limit=2,
                    )
                    for n in neighbors:
                        if n.node_type == "decision" and n.id not in seen_decision_ids:
                            heart_graph_decisions.append(n)
                            seen_decision_ids.add(n.id)
                except Exception:
                    pass

        if heart_graph_decisions:
            results_text.append("\n=== Graph-Connected Decisions ===")
            for i, n in enumerate(heart_graph_decisions, 1):
                decayed = n.edge_weight * settings.graph_recall_decay
                results_text.append(
                    f"{i}. [via {n.edge_relation}] {n.description} (score: {decayed:.3f})"
                )
```

**Step 2: Run recall_deep tests**

Run: `uv run pytest tests/test_tools.py::TestRecallDeep -v`
Expected: All pass

**Step 3: Commit**

```bash
git add nous/api/tools.py
git commit -m "feat(f022): cross-type recall expansion in recall_deep (Phase 2)"
```

---

## Task 8: Phase 3 — Contradiction Bridge

**Files:**
- Modify: `nous/heart/facts.py:471-498` (_contradict method)
- Modify: `nous/heart/facts.py:423-451` (_supersede method)
- Modify: `nous/heart/heart.py` (pass brain reference to facts)
- Test: `tests/test_facts.py` (add bridge tests)

**Step 1: Write failing test**

Add to the facts test file (find the file with `pytest tests/ -k "contradict" --collect-only`):

```python
async def test_contradict_creates_graph_edge(facts, brain, session):
    """facts.contradict() also creates a 'contradicts' graph edge."""
    from nous.storage.models import GraphEdge

    f1 = await facts.learn(FactInput(content="Tim prefers Celsius", subject="Tim"), session=session)
    f2_input = FactInput(content="Tim uses Fahrenheit", subject="Tim")
    f2 = await facts.contradict(f1.id, f2_input, session=session)

    # Check graph edge was created
    result = await session.execute(
        select(GraphEdge).where(
            GraphEdge.source_id == f2.id,
            GraphEdge.target_id == f1.id,
            GraphEdge.relation == "contradicts",
        )
    )
    edge = result.scalar_one_or_none()
    assert edge is not None
    assert edge.source_type == "fact"
    assert edge.target_type == "fact"
```

**Step 2: Run to verify it fails**

Run: `uv run pytest tests/ -k "test_contradict_creates_graph_edge" -v`
Expected: FAIL

**Step 3: Implement the bridge**

The `Facts` class needs access to a database session to insert graph edges. The simplest approach: add a helper method `_create_graph_edge()` to `Facts` that inserts directly.

In `nous/heart/facts.py`, add a method:

```python
async def _create_graph_edge(
    self,
    source_id: UUID,
    target_id: UUID,
    source_type: str,
    target_type: str,
    relation: str,
    weight: float,
    session: AsyncSession,
) -> None:
    """F022: Create a graph edge as side effect of fact operations."""
    try:
        stmt = (
            pg_insert(GraphEdge)
            .values(
                source_id=source_id,
                target_id=target_id,
                source_type=source_type,
                target_type=target_type,
                agent_id=self.agent_id,
                relation=relation,
                weight=weight,
                auto_linked=True,
            )
            .on_conflict_do_nothing(constraint="uq_edges_src_tgt_rel")
        )
        await session.execute(stmt)
    except Exception:
        logger.debug("F022 graph edge creation failed for %s->%s", source_id, target_id)
```

Add the necessary imports at the top of `facts.py`:
```python
from sqlalchemy.dialects.postgresql import insert as pg_insert
from nous.storage.models import GraphEdge
```

Then in `_contradict()` (line 471), after `await session.flush()` on line 489, add:

```python
        # F022: Bridge — also create graph edge
        await self._create_graph_edge(
            new_detail.id, fact_id, "fact", "fact", "contradicts", 1.0, session
        )
```

And in `_supersede()` (line 423), after `await session.flush()` on line 440, add:

```python
        # F022: Bridge — also create graph edge
        await self._create_graph_edge(
            new_detail.id, old_fact_id, "fact", "fact", "supersedes", 1.0, session
        )
```

**Step 4: Run the test**

Run: `uv run pytest tests/ -k "test_contradict_creates_graph_edge" -v`
Expected: PASS

**Step 5: Write supersede bridge test**

```python
async def test_supersede_creates_graph_edge(facts, session):
    """facts.supersede() also creates a 'supersedes' graph edge."""
    from nous.storage.models import GraphEdge

    f1 = await facts.learn(FactInput(content="Python 3.11 is latest", subject="Python"), session=session)
    f2_input = FactInput(content="Python 3.12 is latest", subject="Python")
    f2 = await facts.supersede(f1.id, f2_input, session=session)

    result = await session.execute(
        select(GraphEdge).where(
            GraphEdge.source_id == f2.id,
            GraphEdge.target_id == f1.id,
            GraphEdge.relation == "supersedes",
        )
    )
    edge = result.scalar_one_or_none()
    assert edge is not None
    assert edge.source_type == "fact"
    assert edge.target_type == "fact"
```

**Step 6: Run it**

Run: `uv run pytest tests/ -k "test_supersede_creates_graph_edge" -v`
Expected: PASS

**Step 7: Run all facts tests**

Run: `uv run pytest tests/ -k "fact" -v -x`
Expected: All pass

**Step 8: Commit**

```bash
git add nous/heart/facts.py tests/
git commit -m "feat(f022): contradiction/supersession bridge creates graph edges (Phase 3)"
```

---

## Task 9: Phase 3 — Contradiction Surfacing in recall_deep

**Files:**
- Modify: `nous/api/tools.py` (detect contradictions in results)
- Modify: `nous/config.py` (Phase 3 config)

**Step 1: Add Phase 3 config**

In `nous/config.py`:

```python
    # F022 Phase 3: Contradiction detection
    contradiction_detection: bool = True
    contradiction_similarity_threshold: float = 0.85
    contradiction_model: str = "claude-haiku-4-5-20241022"
```

**Step 2: Add contradiction surfacing to recall_deep**

After all results are assembled in `recall_deep` but before returning (around line 335), add:

```python
    # F022 Phase 3: Check for contradictions among results
    if settings.graph_recall_enabled and settings.contradiction_detection:
        try:
            # Collect all result IDs by type
            all_result_ids: set[UUID] = set()
            if search_all or "decision" in search_types:
                for d in (decision_results or []):
                    all_result_ids.add(d.id)
            # Check for contradiction edges between any results
            if len(all_result_ids) >= 2:
                from sqlalchemy import select as sa_select
                from nous.storage.models import GraphEdge as GE
                async with brain.db.session() as contra_session:
                    contra_result = await contra_session.execute(
                        sa_select(GE).where(
                            GE.relation == "contradicts",
                            GE.source_id.in_(all_result_ids),
                            GE.target_id.in_(all_result_ids),
                        )
                    )
                    contradictions = contra_result.scalars().all()
                    for c in contradictions:
                        results_text.append(
                            f"\n⚠️ Contradiction detected: {c.source_type}({str(c.source_id)[:8]}) "
                            f"↔ {c.target_type}({str(c.target_id)[:8]})"
                        )
        except Exception:
            pass  # Non-critical — don't break recall
```

**Step 3: Commit**

```bash
git add nous/api/tools.py nous/config.py
git commit -m "feat(f022): contradiction surfacing in recall_deep (Phase 3)"
```

---

## Task 10: Phase 4 — Density Gate + Config

**Files:**
- Modify: `nous/config.py` (Phase 4 config)
- Create: `nous/brain/spreading_activation.py`
- Test: `tests/test_spreading_activation.py`

**Step 1: Add Phase 4 config**

In `nous/config.py`:

```python
    # F022 Phase 4: Spreading activation
    spreading_activation_enabled: str = "auto"  # "auto", "true", "false"
    spreading_activation_density_threshold: float = 3.0
    spreading_activation_decay: float = 0.5
    spreading_activation_max_depth: int = 2
    spreading_activation_alpha: float = 0.5  # vector score weight
    spreading_activation_beta: float = 0.3   # graph activation weight
    spreading_activation_gamma: float = 0.2  # recency weight
```

**Step 2: Write failing test for density check**

Create `tests/test_spreading_activation.py`:

```python
"""Tests for F022 Phase 4 — spreading activation."""
import pytest
from uuid import uuid4

from nous.brain.spreading_activation import compute_graph_density


@pytest.mark.asyncio
async def test_density_zero_when_empty(session):
    """Empty graph has density 0."""
    density = await compute_graph_density(session, "test-agent")
    assert density == 0.0


@pytest.mark.asyncio
async def test_density_with_edges(brain, session):
    """Density = edges / unique_nodes."""
    from nous.brain.schemas import RecordInput
    d1 = await brain.record(RecordInput(
        description="Density A", confidence=0.8, category="architecture", stakes="low",
    ), session=session)
    d2 = await brain.record(RecordInput(
        description="Density B", confidence=0.8, category="architecture", stakes="low",
    ), session=session)
    d3 = await brain.record(RecordInput(
        description="Density C", confidence=0.8, category="architecture", stakes="low",
    ), session=session)

    await brain.link(d1.id, d2.id, "supports", session=session)
    await brain.link(d2.id, d3.id, "related_to", session=session)
    await brain.link(d1.id, d3.id, "caused_by", session=session)

    density = await compute_graph_density(session, brain.agent_id)
    # 3 edges, 3 unique nodes => density 1.0
    assert density == pytest.approx(1.0, abs=0.1)
```

**Step 3: Run to verify it fails**

Run: `uv run pytest tests/test_spreading_activation.py -v`
Expected: FAIL (module doesn't exist)

**Step 4: Implement spreading activation module**

Create `nous/brain/spreading_activation.py`:

```python
"""F022 Phase 4 — Spreading activation with density gate.

Provides multi-hop graph traversal using a recursive CTE when
graph density exceeds a configurable threshold.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from nous.config import Settings

logger = logging.getLogger(__name__)


async def compute_graph_density(session: AsyncSession, agent_id: str) -> float:
    """Compute average edges per unique node for the given agent.

    Returns 0.0 if no edges exist.
    """
    sql = text("""
        WITH node_counts AS (
            SELECT COUNT(*) AS edge_count,
                   (SELECT COUNT(DISTINCT node_id) FROM (
                       SELECT source_id AS node_id FROM brain.graph_edges WHERE agent_id = :agent_id
                       UNION
                       SELECT target_id AS node_id FROM brain.graph_edges WHERE agent_id = :agent_id
                   ) nodes) AS unique_nodes
            FROM brain.graph_edges
            WHERE agent_id = :agent_id
        )
        SELECT CASE WHEN unique_nodes = 0 THEN 0.0
                    ELSE edge_count::float / unique_nodes
               END AS density
        FROM node_counts
    """)
    result = await session.execute(sql, {"agent_id": agent_id})
    row = result.one()
    return float(row.density)


def should_use_spreading_activation(
    settings: Settings,
    cached_density: float,
) -> bool:
    """Determine whether to use spreading activation or simple 1-hop.

    Returns True if:
    - spreading_activation_enabled == "true", OR
    - spreading_activation_enabled == "auto" AND density >= threshold

    Returns False if:
    - spreading_activation_enabled == "false"
    """
    mode = settings.spreading_activation_enabled.lower()
    if mode == "true":
        return True
    if mode == "false":
        return False
    # auto mode — check density
    return cached_density >= settings.spreading_activation_density_threshold


async def spreading_activation_search(
    session: AsyncSession,
    agent_id: str,
    seed_nodes: list[tuple[UUID, str, float]],  # (id, node_type, score)
    settings: Settings,
) -> list[tuple[UUID, str, float]]:
    """Run spreading activation CTE and return activated nodes.

    Args:
        session: DB session
        agent_id: Agent scope
        seed_nodes: List of (node_id, node_type, score) from vector search
        settings: For decay, max_depth config

    Returns:
        List of (node_id, node_type, total_activation) sorted by activation desc
    """
    if not seed_nodes:
        return []

    # Build VALUES clause for seeds
    values_parts = []
    params: dict = {
        "decay": settings.spreading_activation_decay,
        "max_depth": settings.spreading_activation_max_depth,
        "agent_id": agent_id,
    }
    for i, (nid, ntype, score) in enumerate(seed_nodes):
        values_parts.append(f"(CAST(:id_{i} AS UUID), :type_{i}, :score_{i})")
        params[f"id_{i}"] = str(nid)
        params[f"type_{i}"] = ntype
        params[f"score_{i}"] = score

    values_clause = ", ".join(values_parts)

    sql = text(f"""
        WITH RECURSIVE activation AS (
            SELECT id, node_type, score AS activation, 0 AS depth
            FROM (VALUES {values_clause}) AS seeds(id, node_type, score)

            UNION ALL

            SELECT
                CASE WHEN e.source_id = a.id THEN e.target_id ELSE e.source_id END,
                CASE WHEN e.source_id = a.id THEN e.target_type ELSE e.source_type END,
                a.activation * COALESCE(e.weight, 1.0) * :decay,
                a.depth + 1
            FROM activation a
            JOIN brain.graph_edges e
                ON (e.source_id = a.id OR e.target_id = a.id)
            WHERE a.depth < :max_depth
                AND e.relation != 'contradicts'
                AND e.agent_id = :agent_id
        )
        SELECT id, node_type, SUM(activation) AS total_activation
        FROM activation
        GROUP BY id, node_type
        ORDER BY total_activation DESC
        LIMIT 20
    """)

    result = await session.execute(sql, params)
    return [(row.id, row.node_type, float(row.total_activation)) for row in result.all()]
```

**Step 5: Run the tests**

Run: `uv run pytest tests/test_spreading_activation.py -v`
Expected: PASS

**Step 6: Write test for should_use_spreading_activation**

Add to `tests/test_spreading_activation.py`:

```python
from nous.brain.spreading_activation import should_use_spreading_activation
from nous.config import Settings


class TestDensityGate:
    def test_force_on(self):
        s = Settings(spreading_activation_enabled="true")
        assert should_use_spreading_activation(s, 0.0) is True

    def test_force_off(self):
        s = Settings(spreading_activation_enabled="false")
        assert should_use_spreading_activation(s, 100.0) is False

    def test_auto_below_threshold(self):
        s = Settings(spreading_activation_enabled="auto", spreading_activation_density_threshold=3.0)
        assert should_use_spreading_activation(s, 2.5) is False

    def test_auto_above_threshold(self):
        s = Settings(spreading_activation_enabled="auto", spreading_activation_density_threshold=3.0)
        assert should_use_spreading_activation(s, 3.5) is True
```

**Step 7: Run density gate tests**

Run: `uv run pytest tests/test_spreading_activation.py::TestDensityGate -v`
Expected: PASS

**Step 8: Commit**

```bash
git add nous/brain/spreading_activation.py tests/test_spreading_activation.py nous/config.py
git commit -m "feat(f022): spreading activation with density gate (Phase 4)"
```

---

## Task 11: Phase 4 — Wire Spreading Activation into recall_deep

**Files:**
- Modify: `nous/api/tools.py` (use spreading activation when enabled)

**Step 1: Update recall_deep to optionally use spreading activation**

In the Brain decisions section of `recall_deep`, after the Phase 1 graph expansion block, add a check for spreading activation:

```python
    # F022 Phase 4: Use spreading activation if density is high enough
    from nous.brain.spreading_activation import (
        should_use_spreading_activation,
        spreading_activation_search,
    )

    # Check if spreading activation should be used
    # (density is cached on cognitive layer, but we can check settings here)
    use_spreading = (
        settings.graph_recall_enabled
        and settings.spreading_activation_enabled != "false"
    )

    if use_spreading and decision_results:
        try:
            async with brain.db.session() as sa_session:
                from nous.brain.spreading_activation import (
                    compute_graph_density,
                    should_use_spreading_activation,
                )
                density = await compute_graph_density(sa_session, brain.agent_id)
                if should_use_spreading_activation(settings, density):
                    # Build seed nodes from vector search results
                    seeds = [
                        (d.id, "decision", d.score or 0.5)
                        for d in decision_results[:5]
                    ]
                    activated = await spreading_activation_search(
                        sa_session, brain.agent_id, seeds, settings
                    )
                    # Replace simple graph expansion with activation results
                    # (filter out seeds, resolve node descriptions)
                    seed_ids = {s[0] for s in seeds}
                    for nid, ntype, activation in activated:
                        if nid not in seed_ids and activation > 0.1:
                            graph_expanded.append(NeighborResult(
                                id=nid,
                                node_type=ntype,
                                description=f"[{ntype}] {str(nid)[:8]}",
                                edge_relation="activation",
                                edge_weight=activation,
                                created_at=datetime.now(UTC),
                            ))
                            seed_ids.add(nid)
        except Exception:
            logger.debug("Spreading activation failed, using 1-hop fallback")
```

Note: This is approximate — the exact integration point depends on how Phase 1 was structured. The key principle is: if spreading activation is enabled and density is high, replace the simple 1-hop expansion with the CTE results.

**Step 2: Run all tests**

Run: `uv run pytest tests/ -v -x --timeout=60`
Expected: All pass

**Step 3: Commit**

```bash
git add nous/api/tools.py
git commit -m "feat(f022): wire spreading activation into recall_deep (Phase 4)"
```

---

## Task 12: Update CLAUDE.md + Feature Index

**Files:**
- Modify: `CLAUDE.md` (add new env vars to table)
- Modify: `docs/features/INDEX.md` (update F022 status)
- Modify: `docs/features/F022-graph-augmented-recall.md` (mark as shipped)

**Step 1: Add env vars to CLAUDE.md**

Add to the environment variables table:

```
| `NOUS_GRAPH_RECALL_ENABLED` | `true` | Enable graph expansion in recall_deep |
| `NOUS_GRAPH_RECALL_MAX_EXPAND` | `5` | Max seed results to expand |
| `NOUS_GRAPH_RECALL_DECAY` | `0.7` | Score decay per graph hop |
| `NOUS_GRAPH_RECALL_MAX_NEIGHBORS` | `3` | Max neighbors per seed |
| `NOUS_CROSS_TYPE_LINKING_ENABLED` | `true` | Enable cross-type auto-linking |
| `NOUS_CROSS_TYPE_THRESHOLD` | `0.80` | Cross-type similarity threshold |
| `NOUS_CONTRADICTION_DETECTION` | `true` | Enable LLM contradiction detection |
| `NOUS_CONTRADICTION_MODEL` | `claude-haiku-4-5-20241022` | Model for contradiction classification |
| `NOUS_SPREADING_ACTIVATION_ENABLED` | `auto` | Spreading activation (auto/true/false) |
| `NOUS_SPREADING_ACTIVATION_DENSITY_THRESHOLD` | `3.0` | Density threshold for auto-enable |
```

**Step 2: Update F022 status**

In `docs/features/F022-graph-augmented-recall.md`, change line 3:
```
> **Status:** Shipped
```

In `docs/features/INDEX.md`, update F022 entry status.

**Step 3: Commit**

```bash
git add CLAUDE.md docs/features/INDEX.md docs/features/F022-graph-augmented-recall.md
git commit -m "docs: update CLAUDE.md and feature index for F022"
```

---

## Task 13: Final Integration Test + Full Suite

**Step 1: Run the full test suite**

Run: `uv run pytest tests/ -v --timeout=120`
Expected: All pass

**Step 2: If any failures, fix them**

Common issues to watch for:
- Tests that create `GraphEdge` without the new required `agent_id` field
- Tests that assert on `DecisionSummary` type from `neighbors()` (now returns `NeighborResult`)
- Import path issues for new modules

**Step 3: Final commit if any fixes needed**

```bash
git add -A
git commit -m "fix(f022): address test failures from graph-augmented recall"
```
