"""Fix PR E (post-merge review of #651) — where a strategy card may reach a prompt.

A strategy card is a ``heart.procedures`` row with ``kind='strategy'``: a lesson
distilled from a decision, not a how-to procedure. These tests pin that it stays
out of every how-to surface. Each one runs the production read path
(``ContextEngine.build``, ``CriticAgent._build_skill_catalog``) against a real
``ProcedureManager`` on its own ``agent_id``. The doubles are the Brain (decision
recall and graph seeds) and the non-procedure heart searches, which use pgvector
SQL that the SQLite lane cannot run.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete

from nous.cognitive.context import ContextEngine
from nous.cognitive.critic import CriticAgent
from nous.cognitive.schemas import FrameSelection
from nous.config import Settings
from nous.heart import Heart
from nous.storage.models import Procedure


def _heart(db, embeddings=None) -> Heart:
    """A real Heart on a fresh agent_id, so seeded rows never reach a catalog."""
    settings = Settings().model_copy(update={"agent_id": f"fix-e-{uuid4().hex[:8]}"})
    return Heart(db, settings, embedding_provider=embeddings)


@pytest_asyncio.fixture
async def card_heart(db):
    """No embedder: the cosine reads return [] before any SQL (both lanes)."""
    h = _heart(db)
    yield h
    await h.close()


@pytest_asyncio.fixture
async def vector_heart(db, mock_embeddings):
    """With the mock embedder, for the pgvector reads (Postgres lane only)."""
    h = _heart(db, mock_embeddings)
    yield h
    await h.close()


async def _add(session, heart, name, *, kind=None, age_minutes=0, embedding=None, body=None) -> Procedure:
    """Insert one active procedure row. ``age_minutes`` orders newest-first reads."""
    row = Procedure(
        agent_id=heart.agent_id,
        name=name,
        domain="strategy" if kind else "ops",
        description=f"about {name}",
        implementation_notes=[body or f"body of {name}"],
        kind=kind,
        active=True,
        embedding=embedding,
        created_at=datetime.now(UTC) - timedelta(minutes=age_minutes),
    )
    session.add(row)
    await session.flush()
    return row


def _engine(heart, brain=None, **flags) -> ContextEngine:
    """ContextEngine over a stand-in heart whose PROCEDURE reads are the real ones."""
    h = MagicMock()
    for m in ("search_facts", "search_episodes", "list_facts_by_category", "list_censors", "list_episodes"):
        setattr(h, m, AsyncMock(return_value=[]))
    for m in (
        "list_procedures",
        "get_procedure",
        "get_procedure_by_name",
        "find_similar_procedures",
        "search_procedures",
    ):
        setattr(h, m, getattr(heart, m))
    if brain is None:
        brain = MagicMock()
        brain.embeddings = None
        brain.query = AsyncMock(return_value=[])
        brain.neighbors = AsyncMock(return_value=[])
    settings = Settings(_env_file=None, relevance_floor_enabled=False, **flags)
    return ContextEngine(brain, h, settings, identity_prompt="Test")


async def _build(engine, heart, session, text="do a task"):
    frame = FrameSelection(frame_id="task", frame_name="Task", confidence=0.9, match_method="test")
    return await engine.build(
        agent_id=heart.agent_id,
        session_id="s1",
        input_text=text,
        frame=frame,
        session=session,
    )


def _section(result, label) -> str | None:
    return next((s.content for s in result.sections if s.label == label), None)


# ---------------------------------------------------------------------------
# Invariant 2 — the how-to catalog never lists a card, flag on or off
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("retrieval", [False, True])
async def test_catalog_never_lists_cards_and_a_new_card_leaves_its_bytes_alone(
    card_heart,
    session,
    retrieval,
):
    """Seven cards newer than both how-to procedures fill the whole catalog fetch
    window (proc_catalog_max=2 -> 6 rows). The catalog must still be exactly the
    two how-to rows, byte-identical to the catalog before any card existed."""
    await _add(session, card_heart, "howto-a", age_minutes=60)
    await _add(session, card_heart, "howto-b", age_minutes=61)
    engine = _engine(
        card_heart,
        proc_catalog_max=2,
        strategy_cards_retrieval_enabled=retrieval,
    )
    before = _section(await _build(engine, card_heart, session), "Procedure Catalog")

    for i in range(7):
        await _add(session, card_heart, f"card-{i}", kind="strategy", age_minutes=i)
    after = _section(await _build(engine, card_heart, session), "Procedure Catalog")

    assert "howto-a" in before and "howto-b" in before
    assert "card-" not in after
    assert after == before


# ---------------------------------------------------------------------------
# Invariant 1 — the Critic's skill menu never offers a card
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_critic_skill_menu_never_offers_cards(card_heart, db):
    """51 cards newer than the one skill fill the Critic's 50-row window. The menu
    must still be the skill alone. CriticAgent reads through its own session, so
    the rows are committed (and removed again) under this test's agent_id."""
    now = datetime.now(UTC)
    async with db.session() as s:
        s.add(
            Procedure(
                agent_id=card_heart.agent_id,
                name="deploy-skill",
                description="how to deploy",
                active=True,
                created_at=now - timedelta(hours=1),
            )
        )
        s.add_all(
            [
                Procedure(
                    agent_id=card_heart.agent_id,
                    name=f"card-{i}",
                    description="a lesson",
                    kind="strategy",
                    active=True,
                    created_at=now - timedelta(seconds=i),
                )
                for i in range(51)
            ]
        )
        await s.commit()
    try:
        critic = CriticAgent(Settings(_env_file=None), procedure_manager=card_heart.procedures)
        menu, names = await critic._build_skill_catalog()
    finally:
        async with db.session() as s:
            await s.execute(delete(Procedure).where(Procedure.agent_id == card_heart.agent_id))
            await s.commit()

    assert names == {"deploy-skill"}
    assert "card-" not in menu


# ---------------------------------------------------------------------------
# Invariants 1 and 3 — the search-driven reads return how-to procedures only
# ---------------------------------------------------------------------------


@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_cosine_rung_recommends_the_how_to_procedure_not_the_nearer_cards(
    vector_heart,
    mock_embeddings,
    session,
):
    """Twelve cards sit at cosine 1.0 to the query and one how-to procedure at
    about 0.93. The cosine rung fetches 10 rows: the cards must not use up that
    window (pgvector SQL, so the test is skipped on the SQLite lane)."""
    query = "rotate the api keys"
    await _add(
        session, vector_heart, "howto-rotate-keys", embedding=await mock_embeddings.embed_near(query, noise=0.01)
    )
    exact = await mock_embeddings.embed(query)
    for i in range(12):
        await _add(session, vector_heart, f"card-{i}", kind="strategy", embedding=exact)

    engine = _engine(vector_heart, proc_catalog_enabled=False)
    recommended = _section(await _build(engine, vector_heart, session, text=query), "Recommended Procedures")

    assert recommended is not None and "howto-rotate-keys" in recommended
    assert "card-" not in recommended


@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_passive_track_b_lists_the_how_to_procedure_not_the_nearer_cards(
    vector_heart,
    mock_embeddings,
    session,
):
    """Legacy passive path (graph-primary off): the Track B hybrid search has five
    slots. Six cards nearer the query than the how-to procedure must not take them
    (hybrid_search SQL: the SQLite lane's stand-in ignores the filter)."""
    query = "rotate the api keys"
    await _add(
        session, vector_heart, "howto-rotate-keys", embedding=await mock_embeddings.embed_near(query, noise=0.01)
    )
    exact = await mock_embeddings.embed(query)
    for i in range(6):
        await _add(session, vector_heart, f"card-{i}", kind="strategy", embedding=exact)

    engine = _engine(
        vector_heart,
        proc_selection_graph_primary=False,
        proc_catalog_enabled=False,
        proc_passive_injection_enabled=True,
    )
    known = _section(await _build(engine, vector_heart, session, text=query), "Known Procedures")

    assert known is not None and "howto-rotate-keys" in known
    assert "card-" not in known


# ---------------------------------------------------------------------------
# Parity pin — the dashboard browse is the one reader that still lists cards
# ---------------------------------------------------------------------------


class _NoRunner:
    _conversations: dict = {}


@pytest.mark.asyncio
async def test_dashboard_procedure_list_still_shows_cards(card_heart, db):
    """GET /procedures is how an operator inspects accumulated cards, so it keeps
    listing them next to how-to procedures. Green on 236c110 and after the fix;
    red only if rest.py stops asking for cards once list_all hides them by default."""
    from httpx import ASGITransport, AsyncClient

    from nous.api.rest import create_app
    from nous.brain.brain import Brain
    from nous.cognitive.layer import CognitiveLayer

    settings = card_heart.settings
    brain = Brain(database=db, settings=settings)
    cognitive = CognitiveLayer(brain, card_heart, settings, identity_prompt="Test")
    app = create_app(_NoRunner(), brain, card_heart, cognitive, db, settings)
    async with db.session() as s:
        s.add(Procedure(agent_id=card_heart.agent_id, name="deploy-skill", active=True))
        s.add(Procedure(agent_id=card_heart.agent_id, name="card-0", kind="strategy", active=True))
        await s.commit()
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/procedures?limit=50&offset=0")
    finally:
        async with db.session() as s:
            await s.execute(delete(Procedure).where(Procedure.agent_id == card_heart.agent_id))
            await s.commit()
        await brain.close()

    assert resp.status_code == 200
    data = resp.json()
    assert {p["name"] for p in data["procedures"]} == {"deploy-skill", "card-0"}
    assert data["total"] == 2
