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
