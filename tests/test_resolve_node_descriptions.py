"""Tests for ``Brain._resolve_node_descriptions``.

The per-type content-resolution block extracted from ``Brain._neighbors``
so the spreading-activation branch in ``run_recall_pipeline`` can share it
instead of fabricating ``[<ntype>] <uuid8>`` placeholder descriptions.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
import pytest_asyncio

from nous.brain.brain import Brain
from nous.brain.schemas import ReasonInput, RecordInput
from nous.storage.models import Episode, Fact, GraphEdge, Procedure


@pytest_asyncio.fixture
async def brain(db, settings):
    """Brain without embeddings (keyword-only mode)."""
    b = Brain(database=db, settings=settings)
    yield b
    await b.close()


@pytest.mark.asyncio
async def test_resolves_content_and_drops_inactive_fact(brain, session):
    """Active facts + decisions resolve to real content with real created_at;
    inactive (superseded) facts are absent from the map entirely."""
    active = Fact(
        id=uuid.uuid4(),
        agent_id=brain.agent_id,
        content="active fact content",
        active=True,
        created_at=datetime(2026, 1, 5, tzinfo=UTC),
    )
    inactive = Fact(
        id=uuid.uuid4(),
        agent_id=brain.agent_id,
        content="superseded fact content",
        active=False,
        created_at=datetime(2026, 1, 5, tzinfo=UTC),
    )
    session.add_all([active, inactive])
    await session.flush()

    decision = await brain.record(
        RecordInput(
            description="resolver decision",
            confidence=0.8,
            category="architecture",
            stakes="low",
            reasons=[ReasonInput(type="analysis", text="resolver test")],
        ),
        session=session,
    )

    resolved = await brain._resolve_node_descriptions(
        session,
        {"fact": [active.id, inactive.id], "decision": [decision.id]},
    )

    assert resolved[active.id][0] == "active fact content"
    assert isinstance(resolved[active.id][1], datetime)
    assert inactive.id not in resolved
    assert resolved[decision.id][0] == "resolver decision"


@pytest.mark.asyncio
async def test_descriptionless_procedure_resolves_to_name(brain, session):
    """Codex P2 (PR #555): ``Procedure.description`` is optional — a NULL
    description must fall back to the procedure NAME (matching how normal
    recall formats descriptionless procedures), not a ``[procedure] <uuid>``
    placeholder that re-introduces the non-informative content this
    resolver exists to remove."""
    proc = Procedure(
        id=uuid.uuid4(),
        agent_id=brain.agent_id,
        name="deploy-checklist",
        description=None,
        active=True,
        created_at=datetime(2026, 1, 5, tzinfo=UTC),
    )
    session.add(proc)
    await session.flush()

    resolved = await brain._resolve_node_descriptions(
        session, {"procedure": [proc.id]}
    )

    assert resolved[proc.id][0] == "deploy-checklist"


@pytest.mark.asyncio
async def test_foreign_agent_nodes_do_not_resolve(brain, session):
    """Codex P2 round 2 (PR #555): graph_edges endpoints are polymorphic and
    not FK-protected — a miswritten edge can point at ANOTHER agent's node.
    The resolver must agent-scope every lookup so foreign content is never
    surfaced (absent from the map => dropped by callers)."""
    foreign = Fact(
        id=uuid.uuid4(),
        agent_id="some-other-agent",
        content="foreign agent secret fact",
        active=True,
        created_at=datetime(2026, 1, 5, tzinfo=UTC),
    )
    session.add(foreign)
    await session.flush()

    resolved = await brain._resolve_node_descriptions(
        session, {"fact": [foreign.id]}
    )

    assert foreign.id not in resolved


@pytest.mark.asyncio
async def test_neighbors_drops_all_unresolved_node_types(brain, session):
    """Codex P2 round 3 (PR #555): after agent-scoping, an unresolved
    decision/episode/chunk must be DROPPED by ``_neighbors`` like
    facts/procedures already are — not emitted as a ``[type] <uuid>``
    placeholder that surfaces a foreign/dangling node as a recall
    candidate."""
    d1 = await brain.record(
        RecordInput(
            description="edge source decision",
            confidence=0.8,
            category="architecture",
            stakes="low",
            reasons=[ReasonInput(type="analysis", text="neighbors drop test")],
        ),
        session=session,
    )
    foreign_episode = Episode(
        id=uuid.uuid4(),
        agent_id="some-other-agent",
        title="foreign episode",
        summary="foreign agent episode summary",
        created_at=datetime(2026, 1, 5, tzinfo=UTC),
    )
    session.add(foreign_episode)
    await session.flush()
    # Current-agent edge whose endpoint points at another agent's episode
    # (polymorphic, not FK-protected — this can exist).
    session.add(GraphEdge(
        source_id=d1.id,
        target_id=foreign_episode.id,
        source_type="decision",
        target_type="episode",
        agent_id=brain.agent_id,
        relation="related_to",
        weight=0.9,
    ))
    await session.flush()

    results = await brain.neighbors(d1.id, session=session)

    assert all(r.id != foreign_episode.id for r in results), (
        "foreign-agent episode must be dropped, not surfaced as a placeholder"
    )


@pytest.mark.asyncio
async def test_empty_input_returns_empty_map(brain, session):
    resolved = await brain._resolve_node_descriptions(session, {})
    assert resolved == {}
