"""Post-merge review of #651 — strategy cards and the decision graph.

A strategy card is linked to the decision it was distilled from by an
``extracted_from`` edge, so it is a graph neighbour of that decision. These tests
pin that graph-neighbour expansion returns a card only to the one caller that
asks for cards: the K-line rung of procedure selection, when strategy-card
retrieval is on.

``Brain.neighbors``, ``Brain._resolve_node_descriptions`` and ``Brain.top_hubs``
always run for real against real rows. The K-line tests run
``ContextEngine.build`` over that ``Brain.neighbors``; the recall tests run
``run_recall_pipeline`` and the ``recall_deep`` tool. The doubles are decision
recall (its SQL is Postgres-only) and the non-procedure heart searches.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete

from nous.api.retrieval_pipeline import run_recall_pipeline
from nous.brain.brain import Brain
from nous.brain.schemas import DecisionSummary, ReasonInput, RecordInput
from nous.cognitive.context import ContextEngine
from nous.cognitive.schemas import FrameSelection
from nous.config import Settings
from nous.heart import Heart
from nous.storage.models import Decision, Event, Fact, GraphEdge, Procedure


@pytest_asyncio.fixture
async def graph(db):
    """A real Brain and a real Heart (no embedder) on one fresh agent_id."""
    settings = Settings().model_copy(update={"agent_id": f"fix-e-{uuid4().hex[:8]}"})
    g = SimpleNamespace(
        agent_id=settings.agent_id,
        settings=settings,
        brain=Brain(database=db, settings=settings),
        heart=Heart(db, settings, embedding_provider=None),
    )
    yield g
    await g.heart.close()
    await g.brain.close()


@pytest_asyncio.fixture
async def stored(db, graph):
    """For code that opens its own sessions (the recall pipeline): the test commits
    its rows under its own agent_id and this fixture deletes them again."""
    yield graph
    async with db.session() as s:
        for model in (GraphEdge, Procedure, Decision, Fact, Event):
            await s.execute(delete(model).where(model.agent_id == graph.agent_id))
        await s.commit()


async def _link(session, graph, decision_id: UUID, name: str, weight: float, *, kind=None, active=True) -> Procedure:
    """One procedure linked to a decision the way its own linker links it: a card
    by ``extracted_from`` (the distiller), a how-to procedure by ``caused_by``
    (ProcedureGraphLinker)."""
    row = Procedure(
        id=uuid4(),
        agent_id=graph.agent_id,
        name=name,
        domain="strategy" if kind else "ops",
        description=f"about {name}",
        kind=kind,
        active=active,
    )
    session.add(row)
    session.add(
        GraphEdge(
            agent_id=graph.agent_id,
            source_id=row.id,
            source_type="procedure",
            target_id=decision_id,
            target_type="decision",
            relation="extracted_from" if kind else "caused_by",
            weight=weight,
            auto_linked=True,
            extraction_method="heuristic",
        )
    )
    await session.flush()
    return row


# ---------------------------------------------------------------------------
# Brain.neighbors / Brain._resolve_node_descriptions — a card only when asked
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_card_never_takes_a_row_of_the_procedure_window(graph, session):
    """Three retired cards and three active ones all outrank the one how-to
    procedure on a decision's edges. Asked for ONE procedure neighbour (the SQL
    fetches three rows), Brain.neighbors returns the how-to procedure: cards are
    excluded before the LIMIT, not after it. Asked for cards — with no
    ``neighbor_type`` — it returns active cards and nothing else: the strongest
    one for limit 1, although three retired cards outrank it, and exactly the
    three active ones for limit 5."""
    seed = uuid4()
    howto = await _link(session, graph, seed, "howto-deploy", 0.5)
    for i in range(3):
        await _link(session, graph, seed, f"card-retired-{i}", 0.95, kind="strategy", active=False)
    cards = [await _link(session, graph, seed, f"card-{i}", 0.9 - i / 10, kind="strategy") for i in range(3)]

    default = await graph.brain.neighbors(
        seed,
        node_type="decision",
        neighbor_type="procedure",
        limit=1,
        session=session,
    )
    assert [n.id for n in default] == [howto.id]

    strongest = await graph.brain.neighbors(seed, node_type="decision", limit=1, session=session, cards_only=True)
    assert [n.id for n in strongest] == [cards[0].id]

    asked = await graph.brain.neighbors(seed, node_type="decision", limit=5, session=session, cards_only=True)
    assert [n.id for n in asked] == [c.id for c in cards]


@pytest.mark.asyncio
async def test_the_untyped_fan_out_returns_its_other_neighbours_and_no_card(graph, session):
    """No ``neighbor_type``: the call shape of recall_deep's decision one-hop
    (``_run_stages`` in retrieval_pipeline.py) and of the companion's
    expandGraphNode (``expand_graph_node`` in a2ui/actions.py). Three cards
    outrank a fact on the decision's edges; with one row asked for, the fact
    comes back."""
    seed = uuid4()
    fact = Fact(id=uuid4(), agent_id=graph.agent_id, content="a fact about the deploy", active=True)
    session.add(fact)
    session.add(
        GraphEdge(
            agent_id=graph.agent_id,
            source_id=fact.id,
            source_type="fact",
            target_id=seed,
            target_type="decision",
            relation="evidence_for",
            weight=0.5,
            auto_linked=True,
            extraction_method="heuristic",
        )
    )
    for i in range(3):
        await _link(session, graph, seed, f"card-{i}", 0.9, kind="strategy")

    found = await graph.brain.neighbors(seed, node_type="decision", limit=1, session=session)

    assert [(n.node_type, n.id) for n in found] == [("fact", fact.id)]


@pytest.mark.asyncio
async def test_a_card_on_the_target_side_of_an_edge_is_filtered_the_same_way(graph, session):
    """The distiller writes card -> decision. Other writers (hub bridging, backfill) can
    put the card on the target side: decision -> card. Three such cards outrank the
    how-to procedure and a fact. Mutations: drop either source-side predicate of
    Brain._neighbors (the exclusion, or the cards-only restriction)."""
    seed = uuid4()
    howto = Procedure(
        id=uuid4(),
        agent_id=graph.agent_id,
        name="howto-deploy",
        domain="ops",
        description="about howto-deploy",
        active=True,
    )
    cards = [
        Procedure(
            id=uuid4(),
            agent_id=graph.agent_id,
            name=f"card-{i}",
            domain="strategy",
            description=f"about card-{i}",
            kind="strategy",
            active=True,
        )
        for i in range(3)
    ]
    other = Fact(id=uuid4(), agent_id=graph.agent_id, content="a fact about the deploy", active=True)
    session.add_all([howto, other, *cards])
    targets = [(c, "procedure", 0.9 - i / 100) for i, c in enumerate(cards)]
    targets += [(howto, "procedure", 0.5), (other, "fact", 0.4)]
    for target, ttype, weight in targets:
        session.add(
            GraphEdge(
                agent_id=graph.agent_id,
                source_id=seed,
                source_type="decision",
                target_id=target.id,
                target_type=ttype,
                relation="related_to",
                weight=weight,
                auto_linked=True,
                extraction_method="heuristic",
            )
        )
    await session.flush()

    typed = await graph.brain.neighbors(seed, node_type="decision", neighbor_type="procedure", limit=1, session=session)
    assert [n.id for n in typed] == [howto.id]
    untyped = await graph.brain.neighbors(seed, node_type="decision", limit=1, session=session)
    assert [n.id for n in untyped] == [howto.id]
    asked = await graph.brain.neighbors(seed, node_type="decision", limit=5, session=session, cards_only=True)
    assert [n.id for n in asked] == [c.id for c in cards]


@pytest.mark.asyncio
async def test_another_agents_cards_do_not_take_the_cards_only_window(graph, session):
    """Three edges of this agent point at cards that belong to ANOTHER agent, and
    they outrank the edges to its own card and to its own how-to procedure. Asked
    for one card, Brain.neighbors returns the agent's own card: a foreign card is
    left out before the LIMIT, where it would take the row and then be dropped by
    the agent-scoped resolver. The default call leaves a card of any agent out
    before the LIMIT as well, and returns the how-to procedure."""
    seed = uuid4()
    own_card = await _link(session, graph, seed, "card-own", 0.5, kind="strategy")
    own_howto = await _link(session, graph, seed, "howto-deploy", 0.4)
    for i in range(3):
        foreign = Procedure(
            id=uuid4(),
            agent_id=f"{graph.agent_id}-other",
            name=f"card-foreign-{i}",
            domain="strategy",
            description=f"about card-foreign-{i}",
            kind="strategy",
            active=True,
        )
        session.add(foreign)
        session.add(
            GraphEdge(
                agent_id=graph.agent_id,
                source_id=foreign.id,
                source_type="procedure",
                target_id=seed,
                target_type="decision",
                relation="extracted_from",
                weight=0.9,
                auto_linked=True,
                extraction_method="heuristic",
            )
        )
    await session.flush()

    asked = await graph.brain.neighbors(seed, node_type="decision", limit=1, session=session, cards_only=True)
    assert [n.id for n in asked] == [own_card.id]

    default = await graph.brain.neighbors(
        seed,
        node_type="decision",
        neighbor_type="procedure",
        limit=1,
        session=session,
    )
    assert [n.id for n in default] == [own_howto.id]


@pytest.mark.asyncio
async def test_the_resolver_leaves_a_card_absent_unless_asked(graph, session):
    """recall_deep's spreading-activation branch (in ``_run_stages``,
    retrieval_pipeline.py) resolves its hits here and drops an id that is absent
    from the map."""
    seed = uuid4()
    howto = await _link(session, graph, seed, "howto-deploy", 0.5)
    card = await _link(session, graph, seed, "card-0", 0.7, kind="strategy")
    ids = {"procedure": [howto.id, card.id]}

    default = await graph.brain._resolve_node_descriptions(session, ids)
    assert set(default) == {howto.id}

    asked = await graph.brain._resolve_node_descriptions(session, ids, include_strategy_cards=True)
    assert set(asked) == {howto.id, card.id}


@pytest.mark.asyncio
async def test_the_readers_that_ask_for_cards_get_them_with_or_without_a_session(stored, db):
    """Brain.neighbors(cards_only=True) without a session, and list_procedures(
    include_strategy_cards=True) with one. Mutations: either wrapper drops the flag
    on the branch no current caller takes."""
    seed = uuid4()
    async with db.session() as s:
        await _link(s, stored, seed, "howto-deploy", 0.9)
        card = await _link(s, stored, seed, "card-0", 0.7, kind="strategy")
        await s.commit()

    asked = await stored.brain.neighbors(seed, node_type="decision", limit=5, cards_only=True)
    assert [n.id for n in asked] == [card.id]
    async with db.session() as s:
        rows, total = await stored.heart.list_procedures(limit=10, session=s, include_strategy_cards=True)
    assert {p.name for p in rows} == {"howto-deploy", "card-0"} and total == 2


@pytest.mark.asyncio
async def test_a_hub_listing_never_names_a_card(graph, session):
    """Brain.top_hubs labels feed the recall_hubs tool and the hub-shift notice
    that pre_turn appends to the system prompt. A card that is the most connected
    procedure is listed without its name; a how-to procedure keeps its name."""
    seed = uuid4()
    card = await _link(session, graph, seed, "card-0", 0.7, kind="strategy")
    for _ in range(2):  # two more edges: the card is now the procedure with the highest degree
        session.add(
            GraphEdge(
                agent_id=graph.agent_id,
                source_id=card.id,
                source_type="procedure",
                target_id=uuid4(),
                target_type="fact",
                relation="related_to",
                weight=0.8,
                auto_linked=True,
                extraction_method="heuristic",
            )
        )
    await _link(session, graph, seed, "howto-deploy", 0.5)
    await session.flush()

    hubs = await graph.brain.top_hubs(limit=10, node_type="procedure", session=session)

    assert [h["label"] for h in hubs] == [f"[procedure] {card.id}", "howto-deploy"]


# ---------------------------------------------------------------------------
# The K-line rung (ContextEngine._select_procedures) over the real Brain.neighbors
# ---------------------------------------------------------------------------


def _engine(graph, *decision_ids: UUID, neighbors=None, **flags) -> ContextEngine:
    """ContextEngine whose graph rung calls the real Brain.neighbors (or
    ``neighbors``) and whose procedure reads are the real Heart's.
    ``decision_ids`` are the decisions recalled this turn, best-scored first:
    the seeds whose procedure neighbours the rung fetches."""
    heart = MagicMock()
    for m in ("search_facts", "search_episodes", "list_facts_by_category", "list_censors", "list_episodes"):
        setattr(heart, m, AsyncMock(return_value=[]))
    for m in (
        "list_procedures",
        "get_procedure",
        "get_procedure_by_name",
        "find_similar_procedures",
        "search_procedures",
    ):
        setattr(heart, m, getattr(graph.heart, m))
    brain = MagicMock()
    brain.embeddings = None
    brain.query = AsyncMock(
        return_value=[
            DecisionSummary(
                id=decision_id,
                description="chose blue-green deploys",
                confidence=0.8,
                category="process",
                stakes="medium",
                outcome="success",
                score=0.9 - rank / 10,
                created_at=datetime.now(UTC),
            )
            for rank, decision_id in enumerate(decision_ids)
        ]
    )
    brain.neighbors = neighbors or graph.brain.neighbors
    settings = Settings(
        _env_file=None,
        relevance_floor_enabled=False,
        proc_catalog_enabled=False,
        **flags,
    )
    return ContextEngine(brain, heart, settings, identity_prompt="Test")


async def _build(engine, graph, session):
    frame = FrameSelection(frame_id="task", frame_name="Task", confidence=0.9, match_method="test")
    return await engine.build(
        agent_id=graph.agent_id,
        session_id="s1",
        input_text="do a task",
        frame=frame,
        session=session,
    )


def _recommended(result) -> str:
    return next((s.content for s in result.sections if s.label == "Recommended Procedures"), "")


@pytest.mark.asyncio
async def test_retrieval_off_a_window_full_of_cards_still_recommends_the_how_to_procedure(graph, session):
    """Ten cards outrank the one how-to procedure on the recalled decision's edges:
    more than the rung's whole fetch (three neighbours per seed, nine rows in SQL).
    The how-to procedure is still recommended and no card is in the prompt."""
    seed = uuid4()
    await _link(session, graph, seed, "howto-deploy", 0.5)
    for i in range(10):
        await _link(session, graph, seed, f"card-{i}", 0.9, kind="strategy")

    result = await _build(_engine(graph, seed), graph, session)

    assert "### howto-deploy (ops)" in _recommended(result)
    assert "card-" not in result.system_prompt


@pytest.mark.asyncio
async def test_retrieval_on_serves_the_card_of_a_recalled_decision(graph, session):
    """The one caller that asks. Green on 236c110 and after the fix; red if
    Brain.neighbors stops returning cards and the K-line rung does not ask for them."""
    seed = uuid4()
    await _link(session, graph, seed, "howto-deploy", 0.9)
    await _link(session, graph, seed, "card-0", 0.7, kind="strategy")

    result = await _build(_engine(graph, seed, strategy_cards_retrieval_enabled=True), graph, session)

    assert "### howto-deploy (ops)" in _recommended(result)
    assert "### card-0 (strategy)" in _recommended(result)


@pytest.mark.asyncio
async def test_retrieval_on_cards_do_not_take_the_how_to_window(graph, session):
    """Retrieval on, the same ten stronger cards: the how-to procedure is still
    recommended, followed by exactly one card (the allowance), the strongest."""
    seed = uuid4()
    await _link(session, graph, seed, "howto-deploy", 0.5)
    for i in range(10):
        await _link(session, graph, seed, f"card-{i}", 0.9 - i / 100, kind="strategy")

    result = await _build(_engine(graph, seed, strategy_cards_retrieval_enabled=True), graph, session)
    recommended = _recommended(result)

    assert "### howto-deploy (ops)" in recommended
    assert "### card-0 (strategy)" in recommended
    assert recommended.count("### card-") == 1
    assert recommended.index("### card-0") > recommended.index("### howto-deploy")


@pytest.mark.asyncio
async def test_retrieval_on_how_to_procedures_do_not_take_the_card_window_or_allowance(graph, session):
    """The mirror image. Five how-to procedures outrank the card on the recalled
    decision's edges: they fill the whole per-seed fetch (raised to five here) and
    all five how-to slots. The card is still served, after them."""
    seed = uuid4()
    for i in range(5):
        await _link(session, graph, seed, f"howto-{i}", 1.0 - i / 100)
    await _link(session, graph, seed, "card-0", 0.7, kind="strategy")
    engine = _engine(
        graph,
        seed,
        strategy_cards_retrieval_enabled=True,
        proc_graph_neighbors_per_seed=5,
    )

    recommended = _recommended(await _build(engine, graph, session))

    for i in range(5):
        assert f"### howto-{i} (ops)" in recommended
    assert "### card-0 (strategy)" in recommended
    assert recommended.index("### card-0") > recommended.index("### howto-4")


@pytest.mark.asyncio
async def test_retrieval_on_a_failed_cards_call_keeps_the_how_to_neighbours_of_that_seed(graph, session):
    """The cards-only call is a second query. When it fails, the how-to neighbours
    the first query returned for that seed are still recommended."""
    seed = uuid4()
    await _link(session, graph, seed, "howto-deploy", 0.9)
    await _link(session, graph, seed, "card-0", 0.7, kind="strategy")
    real = graph.brain.neighbors

    async def _cards_call_fails(*args, **kwargs):
        if kwargs.get("cards_only"):
            raise RuntimeError("the cards-only query failed")
        return await real(*args, **kwargs)

    engine = _engine(graph, seed, neighbors=_cards_call_fails, strategy_cards_retrieval_enabled=True)

    recommended = _recommended(await _build(engine, graph, session))

    assert "### howto-deploy (ops)" in recommended
    assert "### card-" not in recommended


@pytest.mark.asyncio
async def test_retrieval_on_a_card_past_the_allowance_is_dropped_before_its_body_is_fetched(graph, session):
    """Two recalled decisions, each with its own card, and an allowance of one. The
    card of the better-scored decision is served; the other is dropped without a
    body fetch."""
    first, second = uuid4(), uuid4()
    served = await _link(session, graph, first, "card-first", 0.7, kind="strategy")
    dropped = await _link(session, graph, second, "card-second", 0.7, kind="strategy")
    fetched: list[UUID] = []
    real = graph.heart.get_procedure

    async def _get_procedure(procedure_id, **kwargs):
        fetched.append(procedure_id)
        return await real(procedure_id, **kwargs)

    graph.heart.get_procedure = _get_procedure
    engine = _engine(graph, first, second, strategy_cards_retrieval_enabled=True)

    recommended = _recommended(await _build(engine, graph, session))

    assert "### card-first (strategy)" in recommended
    assert "card-second" not in recommended
    assert served.id in fetched and dropped.id not in fetched


@pytest.mark.asyncio
async def test_a_card_dropped_before_its_body_is_fetched_is_attributed_in_the_retrieval_trace(graph, session):
    """The drop a real Brain produces: the card came from the cards-only window and the
    allowance is used. Mutation: remove that drop's trace line (the card then has no
    disposition and lands in `unaccounted`)."""
    from nous.observability.retrieval_trace import RetrievalTrace

    first, second = uuid4(), uuid4()
    await _link(session, graph, first, "card-first", 0.7, kind="strategy")
    dropped = await _link(session, graph, second, "card-second", 0.7, kind="strategy")
    engine = _engine(graph, first, second, strategy_cards_retrieval_enabled=True)
    trace = RetrievalTrace(query="q", path="context")

    selected = await engine._select_procedures(
        slots=5,
        critic_skills=[],
        recalled_ids={"fact": [], "decision": [str(first), str(second)]},
        recalled_score_map={str(first): 0.9, str(second): 0.8},
        session=session,
        trace=trace,
        card_slots=1,
    )

    assert [p.name for p in selected] == ["card-first"]
    row = next(c for c in trace.to_dict()["candidates"] if c["id"] == str(dropped.id))
    assert (row["disposition"], row["disposition_stage"]) == ("sliced_off", "strategy_card_cap")


@pytest.mark.asyncio
async def test_an_allowance_of_two_serves_both_cards_of_one_seed(graph, session):
    """Mutations: a cards-only window of one row instead of the allowance; an
    allowance that never goes above one."""
    seed = uuid4()
    await _link(session, graph, seed, "howto-deploy", 0.9)
    await _link(session, graph, seed, "card-0", 0.7, kind="strategy")
    await _link(session, graph, seed, "card-1", 0.6, kind="strategy")
    await _link(session, graph, seed, "card-2", 0.5, kind="strategy")
    engine = _engine(graph, seed, strategy_cards_retrieval_enabled=True, strategy_cards_max_per_turn=2)

    recommended = _recommended(await _build(engine, graph, session))

    assert [recommended.count(f"### card-{i} (strategy)") for i in range(3)] == [1, 1, 0]
    assert recommended.index("### howto-deploy") < recommended.index("### card-0") < recommended.index("### card-1")


@pytest.mark.asyncio
async def test_retrieval_off_makes_no_cards_only_call(graph, session):
    """With the flag off the turn costs what it cost before: one neighbour query per
    seed. Mutation: make the cards-only call whatever the allowance is."""
    seed = uuid4()
    await _link(session, graph, seed, "howto-deploy", 0.9)
    await _link(session, graph, seed, "card-0", 0.7, kind="strategy")
    real = graph.brain.neighbors
    asked_for_cards: list[bool] = []

    async def _recording(*args, **kwargs):
        asked_for_cards.append(bool(kwargs.get("cards_only")))
        return await real(*args, **kwargs)

    await _build(_engine(graph, seed, neighbors=_recording), graph, session)
    assert asked_for_cards == [False]

    asked_for_cards.clear()
    engine = _engine(graph, seed, neighbors=_recording, strategy_cards_retrieval_enabled=True)
    await _build(engine, graph, session)
    assert asked_for_cards == [False, True]


# ---------------------------------------------------------------------------
# recall_deep — the graph legs of run_recall_pipeline
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recall_pipeline_one_hop_returns_the_how_to_neighbour_and_not_the_card(stored, db):
    """run_recall_pipeline's decision one-hop over the real Brain.neighbors. The
    pipeline opens its own sessions, so the rows are committed. Decision recall
    is the double (Brain.query is Postgres-only SQL); the next test runs it for
    real on the Postgres lane."""
    seed = uuid4()
    async with db.session() as s:
        howto = await _link(s, stored, seed, "howto-deploy", 0.6)
        card = await _link(s, stored, seed, "card-0", 0.7, kind="strategy")
        await s.commit()
    recalled = DecisionSummary(
        id=seed,
        description="chose blue-green deploys",
        confidence=0.8,
        category="process",
        stakes="medium",
        outcome="success",
        score=0.9,
        created_at=datetime.now(UTC),
    )
    settings = stored.settings.model_copy(update={"spreading_activation_enabled": "false"})

    with patch.object(stored.brain, "query", AsyncMock(return_value=[recalled])):
        results, _stats = await run_recall_pipeline(
            "blue-green deploys",
            MagicMock(),
            stored.brain,
            settings,
            memory_types=["decision"],
        )

    by_id = {r.id: r for r in results}
    assert by_id[howto.id].type == "procedure"  # the graph leg ran and still returns procedures
    assert card.id not in by_id


@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_cards_do_not_use_up_the_spreading_activation_window(stored, db):
    """run_recall_pipeline's spreading-activation leg reads the 40 most activated
    nodes and returns at most 20 of them. The recalled decision is linked to 30
    decisions that are recalled as well, each with its own card, and by weaker
    edges to 25 facts. With a decay of 0.5 a card is activated at 0.9 x 0.5 x
    0.7 x 0.5 = 0.16 and a fact at 0.9 x 0.3 x 0.5 = 0.14, so the 30 cards
    outrank every fact. The leg still returns 20 facts: a card is left out of
    the 40 rows, not dropped after them. (The spreading CTE is Postgres-only
    SQL.)"""
    seed = uuid4()
    others = [uuid4() for _ in range(30)]
    async with db.session() as s:
        cards = []
        for i, other in enumerate(others):
            s.add(
                GraphEdge(
                    agent_id=stored.agent_id,
                    source_id=seed,
                    source_type="decision",
                    target_id=other,
                    target_type="decision",
                    relation="related_to",
                    weight=1.0,
                    auto_linked=True,
                    extraction_method="heuristic",
                )
            )
            cards.append(await _link(s, stored, other, f"card-{i}", 0.7, kind="strategy"))
        for i in range(25):
            fact = Fact(id=uuid4(), agent_id=stored.agent_id, content=f"a fact about the deploy {i}", active=True)
            s.add(fact)
            s.add(
                GraphEdge(
                    agent_id=stored.agent_id,
                    source_id=fact.id,
                    source_type="fact",
                    target_id=seed,
                    target_type="decision",
                    relation="evidence_for",
                    weight=0.3,
                    auto_linked=True,
                    extraction_method="heuristic",
                )
            )
        await s.commit()
    recalled = [
        DecisionSummary(
            id=decision_id,
            description="chose blue-green deploys",
            confidence=0.8,
            category="process",
            stakes="medium",
            outcome="success",
            score=0.9 - rank / 100,
            created_at=datetime.now(UTC),
        )
        for rank, decision_id in enumerate([seed, *others])
    ]
    settings = stored.settings.model_copy(
        update={
            "spreading_activation_enabled": "true",
            "spreading_activation_decay": 0.5,
            "graph_recall_max_expand": 1,  # the recalled decision is the one seed
        }
    )

    with patch.object(stored.brain, "query", AsyncMock(return_value=recalled)):
        results, stats = await run_recall_pipeline(
            "blue-green deploys",
            MagicMock(),
            stored.brain,
            settings,
            memory_types=["decision"],
        )

    spread = [r for r in results if r.source == "spreading_activation"]
    assert stats.spreading_activation_used
    assert [r.type for r in spread] == ["fact"] * 20
    assert not {c.id for c in cards} & {r.id for r in results}


@pytest.mark.postgres_only
@pytest.mark.asyncio
@pytest.mark.parametrize("spreading", ["false", "true"])
async def test_recall_deep_text_never_carries_the_card_of_a_recalled_decision(
    stored,
    db,
    mock_embeddings,
    spreading,
):
    """The real recall_deep tool, default scope, real decision recall. A how-to
    procedure and a card are linked to the decision the query recalls. The tool's
    text carries the how-to neighbour and not the card, through the one-hop leg
    (``false`` -> Brain.neighbors) and through the spreading-activation leg
    (``true`` -> Brain._resolve_node_descriptions). Postgres-only SQL (tsvector,
    pgvector, the spreading CTE): skipped on the SQLite lane."""
    from nous.api.tools import create_nous_tools

    settings = stored.settings.model_copy(update={"spreading_activation_enabled": spreading})
    brain = Brain(database=db, settings=settings, embedding_provider=mock_embeddings)
    heart = Heart(db, settings, embedding_provider=mock_embeddings)
    try:
        decision = await brain.record(
            RecordInput(
                description="Roll the billing release out behind a blue-green switch",
                confidence=0.9,
                category="process",
                stakes="medium",
                reasons=[ReasonInput(type="analysis", text="Rollback is one switch flip")],
            )
        )
        async with db.session() as s:
            await _link(s, stored, decision.id, "howto-deploy", 0.6)
            await _link(s, stored, decision.id, "card-0", 0.7, kind="strategy")
            await s.commit()

        out = await create_nous_tools(brain, heart, settings)["recall_deep"](
            query="blue-green switch for the billing release",
        )
    finally:
        await heart.close()
        await brain.close()
    text = out["content"][0]["text"]

    via = "spreading_activation" if spreading == "true" else "caused_by"
    assert f"[procedure] [via {via}] about howto-deploy" in text
    assert "about card-0" not in text
