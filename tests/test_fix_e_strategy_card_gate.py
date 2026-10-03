"""Post-merge review of #651 — where a strategy card may reach a prompt.

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
from unittest.mock import AsyncMock, MagicMock, patch
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


async def _add(
    session, heart, name, *, kind=None, age_minutes=0, embedding=None, body=None, description=None
) -> Procedure:
    """Insert one active procedure row. ``age_minutes`` orders newest-first reads."""
    row = Procedure(
        agent_id=heart.agent_id,
        name=name,
        domain="strategy" if kind else "ops",
        description=description or f"about {name}",
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
# The how-to catalog never lists a card, flag on or off
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
# The Critic's skill menu never offers a card
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
# The search-driven reads return how-to procedures only
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


# ---------------------------------------------------------------------------
# Recommended Procedures: gate, cap, and slot accounting
# ---------------------------------------------------------------------------


def _seeded_brain(*neighbors) -> MagicMock:
    """Brain double: one recalled decision and its procedure neighbours, given as
    (row, edge_weight) pairs. A call that asks for cards only gets the cards among
    them; every other call gets all of them, cards included. A real Brain returns
    no card on that other call, so these tests pin what ``_select_procedures``
    itself does with a card that reaches it."""
    from nous.brain.schemas import DecisionSummary, NeighborResult

    now = datetime.now(UTC)
    decision = DecisionSummary(
        id=uuid4(),
        description="chose blue-green deploys",
        confidence=0.8,
        category="process",
        stakes="medium",
        outcome="success",
        score=0.9,
        created_at=now,
    )
    rows = [
        (
            NeighborResult(
                id=row.id,
                node_type="procedure",
                description=row.name,
                edge_relation="extracted_from",
                edge_weight=weight,
                created_at=now,
            ),
            row.kind == "strategy",
        )
        for row, weight in neighbors
    ]

    async def _neighbors(*_args, cards_only=False, **_kwargs):
        return [n for n, is_card in rows if is_card or not cards_only]

    brain = MagicMock()
    brain.embeddings = None
    brain.query = AsyncMock(return_value=[decision])
    brain.neighbors = AsyncMock(side_effect=_neighbors)
    return brain


@pytest.mark.asyncio
async def test_retrieval_off_puts_no_card_anywhere_in_the_prompt(card_heart, session):
    """Distillation alone only accumulates: with retrieval off, the card of a
    recalled decision is not recommended, and no card is in the prompt at all."""
    howto = await _add(session, card_heart, "howto-deploy", age_minutes=60)
    card = await _add(session, card_heart, "card-0", kind="strategy")
    engine = _engine(card_heart, _seeded_brain((card, 0.9), (howto, 0.5)))

    result = await _build(engine, card_heart, session)

    assert "body of howto-deploy" in _section(result, "Recommended Procedures")
    assert "card-" not in result.system_prompt


@pytest.mark.asyncio
async def test_retrieval_on_serves_one_card_after_all_five_how_to_procedures(card_heart, session):
    """Two cards outrank five how-to neighbors on the graph rung. All five how-to
    procedures keep their slots; exactly one card (the stronger) follows them."""
    howtos = [await _add(session, card_heart, f"howto-{i}", age_minutes=60 + i) for i in range(5)]
    cards = [await _add(session, card_heart, f"card-{i}", kind="strategy") for i in range(2)]
    brain = _seeded_brain(
        (cards[0], 0.95),
        (cards[1], 0.9),
        *[(h, 0.8 - 0.1 * i) for i, h in enumerate(howtos)],
    )
    engine = _engine(card_heart, brain, strategy_cards_retrieval_enabled=True)

    result = await _build(engine, card_heart, session)
    recommended = _section(result, "Recommended Procedures")

    for i in range(5):
        assert f"body of howto-{i}" in recommended
    assert "body of card-0" in recommended
    assert "card-1" not in result.system_prompt
    assert recommended.index("card-0") > recommended.index("body of howto-4")
    assert "card-" not in _section(result, "Procedure Catalog")


@pytest.mark.asyncio
async def test_a_card_that_still_fits_is_shown_when_the_budget_cuts_a_how_to_body(card_heart, session):
    """A procedure budget of 400 tokens. The first how-to body takes about 210 of
    them and the second (about 1,500) is cut. The card comes after the how-to
    procedures and is small (about 35): it still fits in what the first one left,
    so it is shown. The how-to body that did not fit stays cut, and only what is
    shown is recorded as recalled."""
    fits = await _add(session, card_heart, "howto-fits", age_minutes=60, body="s" * 800)
    cut = await _add(session, card_heart, "howto-cut", age_minutes=61, body="s" * 6000)
    card = await _add(session, card_heart, "card-0", kind="strategy")
    engine = _engine(
        card_heart,
        _seeded_brain((fits, 0.9), (cut, 0.8), (card, 0.7)),
        strategy_cards_retrieval_enabled=True,
        proc_catalog_enabled=False,
        context_budget_overrides={"procedures": 400},
        budget_scale_enabled=False,
        proc_recommended_body_max_chars=8000,
    )

    result = await _build(engine, card_heart, session)
    recommended = _section(result, "Recommended Procedures")

    assert "### howto-fits (ops)" in recommended
    assert "### howto-cut (ops)" not in recommended
    assert "### card-0 (strategy)" in recommended
    assert recommended.index("### card-0") > recommended.index("### howto-fits")
    assert result.recalled_ids["procedure"] == [str(fits.id), str(card.id)]


@pytest.mark.asyncio
async def test_a_card_that_does_not_fit_in_what_the_how_to_procedures_left_is_not_shown(card_heart, session):
    """The other half: the budget still bounds the card, and the card takes nothing
    from a how-to procedure. This card (about 280 tokens) would fit in the whole
    budget of 400, but not in the 190 the first how-to body left of it, so it is
    not shown and not recorded."""
    fits = await _add(session, card_heart, "howto-fits", age_minutes=60, body="s" * 800)
    cut = await _add(session, card_heart, "howto-cut", age_minutes=61, body="s" * 6000)
    card = await _add(session, card_heart, "card-0", kind="strategy", body="c" * 1000)
    engine = _engine(
        card_heart,
        _seeded_brain((fits, 0.9), (cut, 0.8), (card, 0.7)),
        strategy_cards_retrieval_enabled=True,
        proc_catalog_enabled=False,
        context_budget_overrides={"procedures": 400},
        budget_scale_enabled=False,
        proc_recommended_body_max_chars=8000,
    )

    result = await _build(engine, card_heart, session)
    recommended = _section(result, "Recommended Procedures")

    assert "### howto-fits (ops)" in recommended
    assert "card-0" not in recommended
    assert result.recalled_ids["procedure"] == [str(fits.id)]


_TIGHT = dict(
    proc_catalog_enabled=False,
    context_budget_overrides={"procedures": 400},
    budget_scale_enabled=False,
    proc_recommended_body_max_chars=8000,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("retrieval", [False, True])
async def test_a_how_to_body_after_the_budget_cut_is_still_not_shown(card_heart, session, retrieval):
    """What the budget loop does on 236c110: it stops at the first how-to body that does
    not fit, and a later, smaller one is not shown. The pass that lets a card through
    must leave that alone. Mutation: let the second pass admit any procedure."""
    fits = await _add(session, card_heart, "howto-fits", age_minutes=60, body="s" * 800)
    cut = await _add(session, card_heart, "howto-cut", age_minutes=61, body="s" * 6000)
    small = await _add(session, card_heart, "howto-small", age_minutes=62)
    engine = _engine(
        card_heart,
        _seeded_brain((fits, 0.9), (cut, 0.8), (small, 0.7)),
        strategy_cards_retrieval_enabled=retrieval,
        **_TIGHT,
    )

    result = await _build(engine, card_heart, session)

    assert "### howto-fits (ops)" in _section(result, "Recommended Procedures")
    assert "howto-small" not in result.system_prompt
    assert result.recalled_ids["procedure"] == [str(fits.id)]


@pytest.mark.asyncio
async def test_two_cards_share_what_the_how_to_procedures_left(card_heart, session):
    """Allowance two. The first how-to body takes about 210 of 400 tokens and the second
    is cut; each card is about 160. One card fits in what is left, two do not.
    Mutation: the second pass does not count what a card it showed used."""
    fits = await _add(session, card_heart, "howto-fits", age_minutes=60, body="s" * 800)
    cut = await _add(session, card_heart, "howto-cut", age_minutes=61, body="s" * 6000)
    cards = [await _add(session, card_heart, f"card-{i}", kind="strategy", body="c" * 500) for i in range(2)]
    engine = _engine(
        card_heart,
        _seeded_brain((fits, 0.9), (cut, 0.8), (cards[0], 0.7), (cards[1], 0.6)),
        strategy_cards_retrieval_enabled=True,
        strategy_cards_max_per_turn=2,
        **_TIGHT,
    )

    result = await _build(engine, card_heart, session)
    recommended = _section(result, "Recommended Procedures")

    assert "### card-0 (strategy)" in recommended
    assert "card-1" not in recommended
    assert result.recalled_ids["procedure"] == [str(fits.id), str(cards[0].id)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("budget", "shown", "traced_as"),
    [
        (500, False, ("budget_truncated", "procedure_budget_accumulation")),
        (1000, True, ("rendered", "final")),
    ],
    ids=["budget-below-the-card", "budget-above-the-card"],
)
async def test_a_card_that_is_the_only_block_is_shown_only_if_it_fits_the_budget(
    card_heart, session, budget, shown, traced_as
):
    """No how-to procedure is selected, so the card is the first block of the
    section. The first HOW-TO block is shown whatever its size; a card is not.
    This one is as large as the distiller stores them (name 80, description 1000,
    lesson 2000 characters: about 800 tokens). In a procedure budget of 500 tokens
    it is left out and attributed as cut by the budget; in a budget of 1000 it is
    shown."""
    from nous.observability.retrieval_logger import RetrievalLogger

    card = await _add(session, card_heart, "n" * 80, kind="strategy", description="d" * 1000, body="l" * 2000)
    engine = _engine(
        card_heart,
        _seeded_brain((card, 0.9)),
        strategy_cards_retrieval_enabled=True,
        proc_catalog_enabled=False,
        context_budget_overrides={"procedures": budget},
        budget_scale_enabled=False,
    )

    tracing = RetrievalLogger(candidate_sample_rate=1.0)
    with patch("nous.cognitive.context.get_active_retrieval_logger", return_value=tracing):
        result = await _build(engine, card_heart, session)

    assert ("l" * 2000 in result.system_prompt) is shown
    assert result.recalled_ids["procedure"] == ([str(card.id)] if shown else [])
    traced = next(c for c in result.retrieval_trace.to_dict()["candidates"] if c["id"] == str(card.id))
    assert (traced["disposition"], traced["disposition_stage"]) == traced_as


@pytest.mark.asyncio
@pytest.mark.parametrize("retrieval", [False, True])
async def test_a_how_to_body_that_is_the_first_block_is_shown_whatever_the_budget(card_heart, session, retrieval):
    """The exemption a card does not get: a how-to body that is the first block is
    shown although it is larger than the procedure budget (the per-item cap is what
    bounds it), as on 236c110. Mutation: hold the first block to the budget whatever
    it is."""
    big = await _add(session, card_heart, "howto-big", age_minutes=60, body="s" * 6000)
    engine = _engine(
        card_heart,
        _seeded_brain((big, 0.9)),
        strategy_cards_retrieval_enabled=retrieval,
        proc_catalog_enabled=False,
        context_budget_overrides={"procedures": 400},
        budget_scale_enabled=False,
        proc_recommended_body_max_chars=8000,
    )

    result = await _build(engine, card_heart, session)

    assert "### howto-big (ops)" in _section(result, "Recommended Procedures")
    assert result.recalled_ids["procedure"] == [str(big.id)]


@pytest.mark.asyncio
async def test_max_per_turn_zero_serves_no_card(card_heart, session):
    """0 means none (it used to mean unlimited)."""
    howto = await _add(session, card_heart, "howto-deploy", age_minutes=60)
    card = await _add(session, card_heart, "card-0", kind="strategy")
    engine = _engine(
        card_heart,
        _seeded_brain((card, 0.9), (howto, 0.5)),
        strategy_cards_retrieval_enabled=True,
        strategy_cards_max_per_turn=0,
    )

    result = await _build(engine, card_heart, session)

    assert "body of howto-deploy" in _section(result, "Recommended Procedures")
    assert "card-" not in result.system_prompt


def test_negative_max_per_turn_is_rejected():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Settings(_env_file=None, strategy_cards_max_per_turn=-1)


@pytest.mark.asyncio
async def test_a_card_dropped_by_the_cap_is_attributed_in_the_retrieval_trace(card_heart, session):
    """F091: a candidate the gate removes must carry a disposition, or it lands
    in `unaccounted` (the drift alarm). Retrieval off -> allowance 0."""
    from nous.observability.retrieval_trace import RetrievalTrace

    howto = await _add(session, card_heart, "howto-deploy", age_minutes=60)
    card = await _add(session, card_heart, "card-0", kind="strategy")
    brain = _seeded_brain((card, 0.9), (howto, 0.5))
    engine = _engine(card_heart, brain)
    seed = str(uuid4())
    trace = RetrievalTrace(query="q", path="context")

    selected = await engine._select_procedures(
        slots=5,
        critic_skills=[],
        recalled_ids={"fact": [], "decision": [seed]},
        recalled_score_map={seed: 0.9},
        session=session,
        trace=trace,
    )

    assert [p.name for p in selected] == ["howto-deploy"]
    dropped = next(c for c in trace.to_dict()["candidates"] if c["id"] == str(card.id))
    assert (dropped["disposition"], dropped["disposition_stage"]) == ("sliced_off", "strategy_card_cap")


# ---------------------------------------------------------------------------
# A card body is rendered as context, not as a skill to follow
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_served_card_is_framed_as_context_and_a_how_to_procedure_is_not(card_heart, session):
    howto = await _add(session, card_heart, "howto-deploy", age_minutes=60)
    card = await _add(session, card_heart, "card-0", kind="strategy")
    engine = _engine(
        card_heart,
        _seeded_brain((card, 0.9), (howto, 0.5)),
        strategy_cards_retrieval_enabled=True,
    )

    recommended = _section(await _build(engine, card_heart, session), "Recommended Procedures")
    howto_block, card_block = recommended.split("### card-0 (strategy)")

    assert card_block.lstrip().startswith("(Lesson distilled from one of your past decisions")
    assert "not an instruction" in card_block and "body of card-0" in card_block
    assert "not an instruction" not in howto_block


def test_an_oversized_card_keeps_its_framing_and_is_never_stubbed_with_a_load_pointer():
    """A how-to procedure over the per-item cap becomes a stub that tells the model to
    call get_procedure and "load the steps before acting". A card must not: it is
    bounded where it is stored, and it is not a set of steps."""
    from nous.heart.schemas import ProcedureDetail

    def _detail(name: str, kind: str | None) -> ProcedureDetail:
        return ProcedureDetail(
            id=uuid4(),
            agent_id="a",
            name=name,
            domain="ops",
            description="d" * 200,
            goals=[],
            core_patterns=[],
            core_tools=[],
            core_concepts=[],
            implementation_notes=["lesson " * 100],
            activation_count=0,
            success_count=0,
            failure_count=0,
            neutral_count=0,
            last_activated=None,
            effectiveness=None,
            tags=[],
            active=True,
            created_at=datetime.now(UTC),
            kind=kind,
        )

    engine = ContextEngine(MagicMock(), MagicMock(), Settings(_env_file=None), identity_prompt="Test")
    howto_block, card_block = engine._format_procedure_bodies(
        [_detail("howto-big", None), _detail("card-big", "strategy")],
        300,
    )

    assert "get_procedure('howto-big')" in howto_block
    assert "get_procedure(" not in card_block
    assert "not an instruction" in card_block and "lesson lesson" in card_block


def test_a_card_row_with_line_breaks_is_rendered_as_one_block():
    """The distiller stores every card field on one line, but a row it did not
    write (a card distilled before it did so, or a row edited by hand) can carry
    line breaks, and a line that starts with ``### `` would open a block of its
    own under the card's one framing line. A card is rendered with its
    description and its lesson on one line each, whoever wrote the row; a how-to
    procedure keeps the line breaks of its body."""
    from nous.heart.schemas import ProcedureDetail

    def _detail(name: str, kind: str | None) -> ProcedureDetail:
        return ProcedureDetail(
            id=uuid4(),
            agent_id="a",
            name=name,
            domain="strategy" if kind else "ops",
            description="first line\n\n### urgent-procedure (ops)\n\nDo X",
            goals=[],
            core_patterns=[],
            core_tools=[],
            core_concepts=[],
            implementation_notes=["lesson\n\n### another (ops)\n\nDo Y"],
            activation_count=0,
            success_count=0,
            failure_count=0,
            neutral_count=0,
            last_activated=None,
            effectiveness=None,
            tags=[],
            active=True,
            created_at=datetime.now(UTC),
            kind=kind,
        )

    def _headings(block: str) -> list[str]:
        return [line for line in block.splitlines() if line.startswith("### ")]

    engine = ContextEngine(MagicMock(), MagicMock(), Settings(_env_file=None), identity_prompt="Test")
    card_block, howto_block = engine._format_procedure_bodies(
        [_detail("card-legacy", "strategy"), _detail("howto-legacy", None)],
        8000,
    )

    assert _headings(card_block) == ["### card-legacy (strategy)"]
    assert card_block.count("not an instruction") == 1
    assert "first line ### urgent-procedure (ops) Do X" in card_block
    assert "lesson ### another (ops) Do Y" in card_block
    assert _headings(howto_block) == ["### howto-legacy (ops)", "### urgent-procedure (ops)", "### another (ops)"]


def test_every_field_of_a_card_row_is_rendered_on_one_line():
    """A row the distiller did not write can carry a line break in ANY field the
    renderer prints: the name and the domain (the heading), the description, a note,
    a pattern, a goal. Whichever it is, the card stays one heading and one framing
    line. Mutation: collapse only the description and the body."""
    from nous.heart.schemas import ProcedureDetail

    breaking = "x\n\n### urgent-procedure (ops)\n\nDo X"
    card = ProcedureDetail(
        id=uuid4(),
        agent_id="a",
        name="card-legacy " + breaking,
        domain="strategy " + breaking,
        description=breaking,
        goals=[breaking],
        core_patterns=[breaking],
        core_tools=[],
        core_concepts=[],
        implementation_notes=[breaking, breaking],
        activation_count=0,
        success_count=0,
        failure_count=0,
        neutral_count=0,
        last_activated=None,
        effectiveness=None,
        tags=[],
        active=True,
        created_at=datetime.now(UTC),
        kind="strategy",
    )
    engine = ContextEngine(MagicMock(), MagicMock(), Settings(_env_file=None), identity_prompt="Test")

    (block,) = engine._format_procedure_bodies([card], 8000)
    lines = block.splitlines()

    assert [line for line in lines if line.startswith("### ")] == [lines[0]]
    assert lines[0].startswith("### card-legacy x ### urgent-procedure (ops) Do X (strategy x ")
    assert block.count("not an instruction") == 1


# ---------------------------------------------------------------------------
# The cosine probe reads one population: how-to procedures, or cards when asked
# (pgvector SQL, so these run on the Postgres lane)
# ---------------------------------------------------------------------------


@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_the_cosine_probe_returns_how_to_procedures_or_cards_never_both(vector_heart, mock_embeddings, session):
    """Three how-to procedures and three cards sit at cosine 1.0 to the query, one
    more of each at about 0.93. Asked for four rows, the default probe returns the
    four how-to procedures and the cards-only probe the four cards, nearest first:
    neither population uses a row of the other's LIMIT window. The default is
    how-to procedures on the Heart method and on the ProcedureManager method that
    other callers use directly."""
    query = "rotate the api keys"
    exact = await mock_embeddings.embed(query)
    near = await mock_embeddings.embed_near(query, noise=0.01)
    for i in range(3):
        await _add(session, vector_heart, f"howto-exact-{i}", embedding=exact)
        await _add(session, vector_heart, f"card-exact-{i}", kind="strategy", embedding=exact)
    await _add(session, vector_heart, "howto-near", embedding=near)
    await _add(session, vector_heart, "card-near", kind="strategy", embedding=near)

    howtos = await vector_heart.find_similar_procedures(query, limit=4, session=session)
    direct = await vector_heart.procedures.find_similar_for_selection(query, limit=4, session=session)
    cards = await vector_heart.find_similar_procedures(query, limit=4, session=session, cards_only=True)

    assert sorted(p.name for p in howtos[:3]) == ["howto-exact-0", "howto-exact-1", "howto-exact-2"]
    assert [p.name for p in howtos[3:]] == ["howto-near"]
    assert sorted(p.name for p in direct) == sorted(p.name for p in howtos)
    assert sorted(p.name for p in cards[:3]) == ["card-exact-0", "card-exact-1", "card-exact-2"]
    assert [p.name for p in cards[3:]] == ["card-near"]
    assert cards[3].score == pytest.approx(0.93, abs=0.02)


@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_the_cards_only_probe_is_agent_scoped_and_returns_active_cards_only(
    vector_heart, mock_embeddings, session
):
    """A retired card of this agent and an active card of ANOTHER agent sit at
    cosine 1.0 to the query, the agent's own active card at about 0.93. Asked for
    one card, the probe returns the agent's own active one: the other two are left
    out before the LIMIT."""
    query = "rotate the api keys"
    exact = await mock_embeddings.embed(query)
    own = await _add(
        session,
        vector_heart,
        "card-own",
        kind="strategy",
        embedding=await mock_embeddings.embed_near(query, noise=0.01),
    )
    session.add_all(
        [
            Procedure(
                agent_id=vector_heart.agent_id,
                name="card-retired",
                domain="strategy",
                kind="strategy",
                active=False,
                embedding=exact,
            ),
            Procedure(
                agent_id=f"{vector_heart.agent_id}-other",
                name="card-foreign",
                domain="strategy",
                kind="strategy",
                active=True,
                embedding=exact,
            ),
        ]
    )
    await session.flush()

    found = await vector_heart.find_similar_procedures(query, limit=1, session=session, cards_only=True)

    assert [p.id for p in found] == [own.id]


@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_the_cards_only_probe_passes_the_flag_on_without_a_session(vector_heart, mock_embeddings, db):
    """Called without a session the probe opens its own, so the rows are committed
    under this test's agent_id and removed again. The how-to procedure is nearer
    the query than the card; asked for one card, the probe returns the card."""
    query = "rotate the api keys"
    async with db.session() as s:
        await _add(s, vector_heart, "howto-rotate-keys", embedding=await mock_embeddings.embed(query))
        card = await _add(
            s, vector_heart, "card-near", kind="strategy", embedding=await mock_embeddings.embed_near(query, noise=0.01)
        )
        await s.commit()
    try:
        found = await vector_heart.find_similar_procedures(query, limit=1, cards_only=True)
    finally:
        async with db.session() as s:
            await s.execute(delete(Procedure).where(Procedure.agent_id == vector_heart.agent_id))
            await s.commit()

    assert [p.id for p in found] == [card.id]


# ---------------------------------------------------------------------------
# The card allowance is topped up by the cards nearest the query (cosine probe)
# ---------------------------------------------------------------------------


@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_card_allowance_is_topped_up_by_the_card_nearest_the_query(
    vector_heart,
    mock_embeddings,
    session,
):
    """No recalled decision, so the graph rung offers no card. The cards-only
    cosine probe serves the card nearest the query (never one under the score
    floor), after the how-to procedure (pgvector SQL — Postgres lane only)."""
    query = "rotate the api keys"
    await _add(
        session, vector_heart, "howto-rotate-keys", embedding=await mock_embeddings.embed_near(query, noise=0.01)
    )
    await _add(session, vector_heart, "card-near", kind="strategy", embedding=await mock_embeddings.embed(query))
    await _add(
        session,
        vector_heart,
        "card-far",
        kind="strategy",
        embedding=await mock_embeddings.embed("an unrelated lesson about tax filing"),
    )
    engine = _engine(
        vector_heart,
        proc_catalog_enabled=False,
        strategy_cards_retrieval_enabled=True,
        strategy_cards_max_per_turn=2,
    )

    recommended = _section(await _build(engine, vector_heart, session, text=query), "Recommended Procedures")

    assert "body of card-near" in recommended
    assert "card-far" not in recommended
    assert recommended.index("card-near") > recommended.index("body of howto-rotate-keys")


def _card_probes(engine) -> list[dict]:
    """Record the cards-only cosine probes this engine sends. The how-to cosine
    rung calls the same method without ``cards_only``; those are not recorded."""
    real = engine._heart.find_similar_procedures
    calls: list[dict] = []

    async def _recording(*args, **kwargs):
        if kwargs.get("cards_only"):
            calls.append(kwargs)
        return await real(*args, **kwargs)

    engine._heart.find_similar_procedures = _recording
    return calls


@pytest.mark.postgres_only
@pytest.mark.asyncio
@pytest.mark.parametrize(("floor", "served"), [(0.40, True), (0.95, False)], ids=["floor-below", "floor-above"])
async def test_the_probe_serves_a_card_only_at_or_above_the_procedure_score_floor(
    vector_heart, mock_embeddings, session, floor, served
):
    """The floor is the one the how-to cosine rung already uses,
    ``procedure_score_floor``. A card at cosine about 0.93 to the query is served
    with the default floor of 0.40 and is not served with a floor of 0.95."""
    query = "rotate the api keys"
    await _add(
        session,
        vector_heart,
        "card-near",
        kind="strategy",
        embedding=await mock_embeddings.embed_near(query, noise=0.01),
    )
    engine = _engine(
        vector_heart,
        proc_catalog_enabled=False,
        strategy_cards_retrieval_enabled=True,
        procedure_score_floor=floor,
    )

    result = await _build(engine, vector_heart, session, text=query)

    assert ("### card-near (strategy)" in result.system_prompt) is served


@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_the_graph_rung_and_the_probe_share_one_allowance(vector_heart, mock_embeddings, session):
    """An allowance of three. The card of the recalled decision comes from the
    graph rung, and the probe finds three more cards near the query. Three cards
    are served in all: the graph rung's first, then the two nearest the query.
    The fourth is not served."""
    query = "rotate the api keys"
    graph_card = await _add(session, vector_heart, "card-graph", kind="strategy")
    near = [
        await _add(
            session,
            vector_heart,
            f"card-near-{i}",
            kind="strategy",
            embedding=await mock_embeddings.embed_near(query, noise=0.01 + i / 1000),
        )
        for i in range(3)
    ]
    engine = _engine(
        vector_heart,
        _seeded_brain((graph_card, 0.9)),
        proc_catalog_enabled=False,
        strategy_cards_retrieval_enabled=True,
        strategy_cards_max_per_turn=3,
    )

    result = await _build(engine, vector_heart, session, text=query)

    assert result.recalled_ids["procedure"] == [str(graph_card.id), str(near[0].id), str(near[1].id)]
    assert "card-near-2" not in result.system_prompt


@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_a_card_the_graph_rung_served_is_not_served_again_by_the_probe(vector_heart, mock_embeddings, session):
    """The card of the recalled decision is also the card nearest the query. It is
    in the prompt once; the room left in an allowance of two goes to the next
    nearest card."""
    query = "rotate the api keys"
    graph_card = await _add(
        session, vector_heart, "card-graph", kind="strategy", embedding=await mock_embeddings.embed(query)
    )
    near = await _add(
        session,
        vector_heart,
        "card-near",
        kind="strategy",
        embedding=await mock_embeddings.embed_near(query, noise=0.01),
    )
    engine = _engine(
        vector_heart,
        _seeded_brain((graph_card, 0.9)),
        proc_catalog_enabled=False,
        strategy_cards_retrieval_enabled=True,
        strategy_cards_max_per_turn=2,
    )

    result = await _build(engine, vector_heart, session, text=query)
    recommended = _section(result, "Recommended Procedures")

    assert [recommended.count(f"### {name} (strategy)") for name in ("card-graph", "card-near")] == [1, 1]
    assert result.recalled_ids["procedure"] == [str(graph_card.id), str(near.id)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("flags", "card_on_the_graph_rung", "probes"),
    [
        ({}, False, 0),
        ({"strategy_cards_retrieval_enabled": True, "strategy_cards_max_per_turn": 0}, False, 0),
        ({"strategy_cards_retrieval_enabled": True}, True, 0),
        ({"strategy_cards_retrieval_enabled": True}, False, 1),
        ({"strategy_cards_retrieval_enabled": True, "strategy_cards_max_per_turn": 3}, True, 1),
    ],
    ids=[
        "retrieval-off",
        "allowance-zero",
        "allowance-used-by-the-graph-rung",
        "room-for-one",
        "room-for-two-of-three",
    ],
)
async def test_the_probe_is_sent_at_most_once_a_turn_and_only_while_the_allowance_has_room(
    card_heart, session, flags, card_on_the_graph_rung, probes
):
    """What the probe costs: one cards-only query a turn at most, and none when
    retrieval is off, when the allowance is 0, or when the graph rung already used
    the allowance. It asks for as many rows as the allowance, no more."""
    howto = await _add(session, card_heart, "howto-deploy", age_minutes=60)
    card = await _add(session, card_heart, "card-0", kind="strategy")
    neighbours = [(howto, 0.5)] + ([(card, 0.9)] if card_on_the_graph_rung else [])
    engine = _engine(card_heart, _seeded_brain(*neighbours), proc_catalog_enabled=False, **flags)
    calls = _card_probes(engine)

    await _build(engine, card_heart, session)

    assert len(calls) == probes
    assert [call["limit"] for call in calls] == [engine._settings.strategy_cards_max_per_turn] * probes


@pytest.mark.asyncio
async def test_the_probe_is_not_sent_without_a_query(card_heart, session):
    """Like the how-to cosine rung, the probe needs a query to compare cards with.
    A selection made with an empty query sends none, although the allowance has
    room."""
    await _add(session, card_heart, "card-0", kind="strategy")
    engine = _engine(card_heart, proc_catalog_enabled=False, strategy_cards_retrieval_enabled=True)
    calls = _card_probes(engine)

    selected = await engine._select_procedures(
        slots=5,
        critic_skills=[],
        recalled_ids={"fact": [], "decision": []},
        recalled_score_map={},
        session=session,
        card_slots=1,
    )

    assert selected == []
    assert calls == []


@pytest.mark.postgres_only
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("budget", "shown"),
    [(500, False), (1000, True)],
    ids=["budget-below-the-card", "budget-above-the-card"],
)
async def test_a_card_from_the_probe_is_framed_and_shown_only_if_it_fits_the_budget(
    vector_heart, mock_embeddings, session, budget, shown
):
    """No how-to procedure and no recalled decision: the one block is a card the
    probe found, as large as the distiller stores them (about 800 tokens). It is
    rendered like a card from the graph rung, one heading and the framing line
    under it, and only if it fits the procedure budget."""
    query = "rotate the api keys"
    card = await _add(
        session,
        vector_heart,
        "n" * 80,
        kind="strategy",
        description="d" * 1000,
        body="l" * 2000,
        embedding=await mock_embeddings.embed(query),
    )
    engine = _engine(
        vector_heart,
        proc_catalog_enabled=False,
        strategy_cards_retrieval_enabled=True,
        context_budget_overrides={"procedures": budget},
        budget_scale_enabled=False,
    )

    result = await _build(engine, vector_heart, session, text=query)
    recommended = _section(result, "Recommended Procedures") or ""

    assert result.recalled_ids["procedure"] == ([str(card.id)] if shown else [])
    assert recommended.count("### ") == int(shown)
    assert recommended.count("(Lesson distilled from one of your past decisions") == int(shown)


@pytest.mark.asyncio
async def test_a_failing_probe_keeps_the_how_to_procedures_and_the_graph_rung_cards(card_heart, session):
    """The probe is a query of its own. When it fails, what was selected before it
    is still recommended: the how-to procedure and the card of the recalled
    decision."""
    howto = await _add(session, card_heart, "howto-deploy", age_minutes=60)
    card = await _add(session, card_heart, "card-graph", kind="strategy")
    engine = _engine(
        card_heart,
        _seeded_brain((howto, 0.5), (card, 0.9)),
        proc_catalog_enabled=False,
        strategy_cards_retrieval_enabled=True,
        strategy_cards_max_per_turn=2,
    )
    real = engine._heart.find_similar_procedures
    failed: list[dict] = []

    async def _probe_fails(*args, **kwargs):
        if kwargs.get("cards_only"):
            failed.append(kwargs)
            raise RuntimeError("the cards-only probe failed")
        return await real(*args, **kwargs)

    engine._heart.find_similar_procedures = _probe_fails

    result = await _build(engine, card_heart, session)

    assert len(failed) == 1
    assert result.recalled_ids["procedure"] == [str(howto.id), str(card.id)]


def _probe_finds(engine, *found) -> None:
    """Stand in for the cards-only probe: it returns ``found``, given nearest
    first as (id, score) pairs. The how-to cosine rung, the same method without
    ``cards_only``, gets nothing."""
    from nous.heart.schemas import ProcedureSummary

    summaries = [
        ProcedureSummary(id=pid, name="a card", domain="strategy", activation_count=0, effectiveness=None, score=score)
        for pid, score in found
    ]

    async def _probe(*_args, **kwargs):
        return summaries if kwargs.get("cards_only") else []

    engine._heart.find_similar_procedures = _probe


@pytest.mark.asyncio
async def test_a_card_at_the_score_floor_is_served_and_a_card_under_it_is_not(card_heart, session):
    """The floor is compared with the score the probe returned for the card. A
    card exactly at the floor is served, as on the how-to cosine rung. A card
    under it is not, although the allowance has room for both."""
    at = await _add(session, card_heart, "card-at-the-floor", kind="strategy")
    under = await _add(session, card_heart, "card-under-the-floor", kind="strategy")
    engine = _engine(
        card_heart,
        proc_catalog_enabled=False,
        strategy_cards_retrieval_enabled=True,
        strategy_cards_max_per_turn=2,
        procedure_score_floor=0.5,
    )
    _probe_finds(engine, (at.id, 0.5), (under.id, 0.49))

    result = await _build(engine, card_heart, session)

    assert result.recalled_ids["procedure"] == [str(at.id)]


@pytest.mark.asyncio
async def test_a_score_floor_of_zero_serves_a_card_with_a_low_score(card_heart, session):
    """``procedure_score_floor = 0`` means no floor above zero, as on the how-to
    cosine rung: a card the probe returns with a cosine of 0.05 is served.
    Mutation: a floor of 0 falls back to the default of 0.40."""
    low = await _add(session, card_heart, "card-low", kind="strategy")
    engine = _engine(
        card_heart,
        proc_catalog_enabled=False,
        strategy_cards_retrieval_enabled=True,
        procedure_score_floor=0.0,
    )
    _probe_finds(engine, (low.id, 0.05))

    result = await _build(engine, card_heart, session)

    assert result.recalled_ids["procedure"] == [str(low.id)]


@pytest.mark.asyncio
async def test_the_ladder_serves_a_probe_card_when_it_is_given_no_trace(card_heart, session):
    """``_select_procedures`` takes ``trace=None`` by default and every rung guards
    its trace calls. build() always passes one, so only a direct caller gets here.
    Mutation: the probe's trace call is made without the guard."""
    card = await _add(session, card_heart, "card-0", kind="strategy")
    engine = _engine(card_heart, proc_catalog_enabled=False, strategy_cards_retrieval_enabled=True)
    _probe_finds(engine, (card.id, 0.9))

    selected = await engine._select_procedures(
        slots=5,
        critic_skills=[],
        recalled_ids={"fact": [], "decision": []},
        recalled_score_map={},
        session=session,
        query="rotate the api keys",
        card_slots=1,
    )

    assert [p.id for p in selected] == [card.id]


@pytest.mark.asyncio
async def test_the_probe_serves_only_a_card_that_is_still_there_and_still_active(card_heart, session):
    """What the probe returned is checked again when the body is fetched. A card
    retired since, and one that is gone, are passed over, and the next card is
    served."""
    retired = await _add(session, card_heart, "card-retired", kind="strategy")
    retired.active = False
    served = await _add(session, card_heart, "card-served", kind="strategy")
    await session.flush()
    engine = _engine(
        card_heart,
        proc_catalog_enabled=False,
        strategy_cards_retrieval_enabled=True,
        strategy_cards_max_per_turn=3,
    )
    _probe_finds(engine, (retired.id, 0.9), (uuid4(), 0.9), (served.id, 0.9))

    result = await _build(engine, card_heart, session)

    assert result.recalled_ids["procedure"] == [str(served.id)]


@pytest.mark.asyncio
async def test_a_card_whose_body_cannot_be_fetched_is_passed_over(card_heart, session):
    """Fetching the body of one card fails. That card is passed over: the how-to
    procedure selected before it and the next card are still recommended."""
    howto = await _add(session, card_heart, "howto-deploy", age_minutes=60)
    unreadable = await _add(session, card_heart, "card-unreadable", kind="strategy")
    served = await _add(session, card_heart, "card-served", kind="strategy")
    engine = _engine(
        card_heart,
        _seeded_brain((howto, 0.5)),
        proc_catalog_enabled=False,
        strategy_cards_retrieval_enabled=True,
        strategy_cards_max_per_turn=2,
    )
    _probe_finds(engine, (unreadable.id, 0.9), (served.id, 0.8))
    real = engine._heart.get_procedure

    async def _get_procedure(procedure_id, *args, **kwargs):
        if procedure_id == unreadable.id:
            raise RuntimeError("the body fetch failed")
        return await real(procedure_id, *args, **kwargs)

    engine._heart.get_procedure = _get_procedure

    result = await _build(engine, card_heart, session)

    assert result.recalled_ids["procedure"] == [str(howto.id), str(served.id)]


@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_a_card_from_the_probe_has_a_leg_of_its_own_in_the_retrieval_trace(
    vector_heart, mock_embeddings, session
):
    """F091: a card the probe served is registered under the probe's own leg, with
    its cosine to the query as the entry score, so it is counted neither as a
    how-to cosine pick nor under the ladder's catch-all leg."""
    from nous.observability.retrieval_logger import RetrievalLogger

    query = "rotate the api keys"
    card = await _add(
        session,
        vector_heart,
        "card-near",
        kind="strategy",
        embedding=await mock_embeddings.embed_near(query, noise=0.01),
    )
    engine = _engine(vector_heart, proc_catalog_enabled=False, strategy_cards_retrieval_enabled=True)

    tracing = RetrievalLogger(candidate_sample_rate=1.0)
    with patch("nous.cognitive.context.get_active_retrieval_logger", return_value=tracing):
        result = await _build(engine, vector_heart, session, text=query)

    traced = next((c for c in result.retrieval_trace.to_dict()["candidates"] if c["id"] == str(card.id)), {})
    assert traced.get("entry_leg") == "context_strategy_cards_cosine"
    assert traced["entry_score"] == pytest.approx(0.93, abs=0.02)
    assert (traced["disposition"], traced["disposition_stage"]) == ("rendered", "final")


# ---------------------------------------------------------------------------
# What the probe leaves as it was: a prompt built with retrieval off, a prompt
# built when there is no card to serve, and the how-to selection
# (pgvector SQL, so these run on the Postgres lane)
# ---------------------------------------------------------------------------


def _prompt(result) -> list[tuple[str, str]]:
    """Every prompt section but the clock, as (label, content)."""
    return [(s.label, s.content) for s in result.sections if s.label != "Current Date/Time"]


@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_with_retrieval_off_cards_near_the_query_change_no_section_and_no_probe_is_sent(
    vector_heart, mock_embeddings, session
):
    """Retrieval off. The prompt is built before any card exists, and again after
    three cards are stored at cosine 1.0 to the query, nearer than every how-to
    procedure. Every section is byte-identical, the recalled ids are the same, and
    the cards-only probe is not sent."""
    query = "rotate the api keys"
    for i in range(3):
        await _add(
            session,
            vector_heart,
            f"howto-{i}",
            embedding=await mock_embeddings.embed_near(query, noise=0.01 + i / 500),
        )
    engine = _engine(vector_heart)
    calls = _card_probes(engine)
    before = await _build(engine, vector_heart, session, text=query)

    exact = await mock_embeddings.embed(query)
    for i in range(3):
        await _add(session, vector_heart, f"card-{i}", kind="strategy", embedding=exact)
    after = await _build(engine, vector_heart, session, text=query)

    assert "body of howto-0" in _section(before, "Recommended Procedures")
    assert _prompt(after) == _prompt(before)
    assert after.recalled_ids == before.recalled_ids
    assert calls == []


@pytest.mark.postgres_only
@pytest.mark.asyncio
@pytest.mark.parametrize("cards_that_may_not_be_served", [False, True], ids=["no-card-row", "retired-and-foreign"])
async def test_with_no_card_to_serve_retrieval_on_builds_the_prompt_of_retrieval_off(
    vector_heart, mock_embeddings, session, cards_that_may_not_be_served
):
    """The agent has how-to procedures near the query and no card that may be
    served: either no card row at all, or a retired card of its own and an active
    card of ANOTHER agent, both at cosine 1.0 to the query. With retrieval on the
    probe is sent once and nothing comes of it: every section and the recalled ids
    are what they are with retrieval off."""
    query = "rotate the api keys"
    for i in range(3):
        await _add(
            session,
            vector_heart,
            f"howto-{i}",
            embedding=await mock_embeddings.embed_near(query, noise=0.01 + i / 500),
        )
    if cards_that_may_not_be_served:
        exact = await mock_embeddings.embed(query)
        retired = await _add(session, vector_heart, "card-retired", kind="strategy", embedding=exact)
        retired.active = False
        session.add(
            Procedure(
                agent_id=f"{vector_heart.agent_id}-other",
                name="card-foreign",
                domain="strategy",
                kind="strategy",
                active=True,
                embedding=exact,
            )
        )
        await session.flush()
    engine = _engine(vector_heart, strategy_cards_retrieval_enabled=True, strategy_cards_max_per_turn=2)
    calls = _card_probes(engine)

    off = await _build(_engine(vector_heart), vector_heart, session, text=query)
    on = await _build(engine, vector_heart, session, text=query)

    assert "body of howto-0" in _section(off, "Recommended Procedures")
    assert _prompt(on) == _prompt(off)
    assert on.recalled_ids == off.recalled_ids
    assert len(calls) == 1


@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_the_how_to_selection_is_the_same_with_and_without_the_probe(vector_heart, mock_embeddings, session):
    """The ladder fills its five slots: one how-to procedure from the graph rung
    and the four nearest of twelve from the cosine rung, whose window of ten rows
    is full. Three cards are nearer the query than any of the twelve. With
    retrieval on and an allowance of two, the probe adds the two nearest cards
    after the how-to procedures. The how-to part of Recommended Procedures is
    byte-identical to the section built with retrieval off, the how-to ids are
    recalled in the same order, and no other section differs."""
    query = "rotate the api keys"
    from_the_graph = await _add(session, vector_heart, "howto-graph", age_minutes=60)
    near = [
        await _add(
            session,
            vector_heart,
            f"howto-{i}",
            embedding=await mock_embeddings.embed_near(query, noise=0.01 + i / 500),
        )
        for i in range(12)
    ]
    cards = [
        await _add(
            session,
            vector_heart,
            f"card-{i}",
            kind="strategy",
            embedding=await mock_embeddings.embed_near(query, noise=i / 500),
        )
        for i in range(3)
    ]
    brain = _seeded_brain((from_the_graph, 0.5))

    off = await _build(_engine(vector_heart, brain), vector_heart, session, text=query)
    on = await _build(
        _engine(vector_heart, brain, strategy_cards_retrieval_enabled=True, strategy_cards_max_per_turn=2),
        vector_heart,
        session,
        text=query,
    )

    how_to_ids = [str(p.id) for p in (from_the_graph, *near[:4])]
    assert off.recalled_ids["procedure"] == how_to_ids
    assert on.recalled_ids["procedure"] == how_to_ids + [str(cards[0].id), str(cards[1].id)]
    recommended = _section(off, "Recommended Procedures")
    assert _section(on, "Recommended Procedures").startswith(recommended + "\n\n### card-0 (strategy)")
    assert [s for s in _prompt(on) if s[0] != "Recommended Procedures"] == [
        s for s in _prompt(off) if s[0] != "Recommended Procedures"
    ]
