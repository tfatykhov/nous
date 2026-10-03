"""No strategy card reaches the prompt with a similarity to the turn below the floor,
whichever rung of the procedure selection offered it.

The graph rung serves the card of a decision recalled this turn. Its order comes
from the graph (edge weight times the decision's recall score); whether the card
may be shown at all is decided by its cosine to the query, held to
``procedure_score_floor`` as the cosine probe already holds its own cards. A card
whose closeness is not known is not shown.

The builds run the production read path of the earlier card tests (real
ProcedureManager rows on a fresh agent_id, a Brain double for the recalled
decision and its graph neighbours).
"""

from __future__ import annotations

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete
from test_fix_e_strategy_card_gate import _add, _build, _card_probes, _heart, _seeded_brain
from test_fix_e_strategy_card_gate import _engine as _gate_engine

from nous.observability.retrieval_trace import RetrievalTrace
from nous.storage.models import Procedure

_QUERY = "rotate the api keys"


@pytest_asyncio.fixture
async def card_heart(db):
    """No embedder: no closeness can be computed (both lanes)."""
    h = _heart(db)
    yield h
    await h.close()


@pytest_asyncio.fixture
async def vector_heart(db, mock_embeddings):
    """With the mock embedder: cosine 1.0 for the same text, about 0.93 for
    embed_near(noise=0.01), about 0 for an unrelated text."""
    h = _heart(db, mock_embeddings)
    yield h
    await h.close()


# ---------------------------------------------------------------------------
# The closeness read itself
# ---------------------------------------------------------------------------


@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_the_closeness_of_given_procedures_is_the_cosine_the_probe_scores_with(
    vector_heart, mock_embeddings, session
):
    """For the given rows of this agent, the same cosine the cards-only probe
    returns; a row with no embedding and a row of another agent are absent."""
    near = await _add(
        session, vector_heart, "card-near", kind="strategy", embedding=await mock_embeddings.embed_near(_QUERY, 0.01)
    )
    far = await _add(
        session, vector_heart, "card-far", kind="strategy", embedding=await mock_embeddings.embed("tax filing")
    )
    bare = await _add(session, vector_heart, "card-bare", kind="strategy")
    other = _heart(vector_heart.db)  # another agent_id, same database
    foreign = await _add(session, other, "card-foreign", kind="strategy", embedding=await mock_embeddings.embed(_QUERY))

    got = await vector_heart.procedure_similarities(_QUERY, [near.id, far.id, bare.id, foreign.id], session=session)
    probe = {
        s.id: s.score
        for s in await vector_heart.find_similar_procedures(_QUERY, limit=5, session=session, cards_only=True)
    }

    assert set(got) == {near.id, far.id}
    assert got[near.id] == pytest.approx(probe[near.id], abs=1e-4)
    assert got[far.id] == pytest.approx(probe[far.id], abs=1e-4)
    await other.close()


@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_the_closeness_read_opens_its_own_session_when_given_none(vector_heart, mock_embeddings, db):
    """Called without a session, as a turn calls it, the read opens its own: the
    rows are committed under this test's agent_id and removed again."""
    async with db.session() as s:
        card = await _add(
            s, vector_heart, "card-near", kind="strategy", embedding=await mock_embeddings.embed_near(_QUERY, 0.01)
        )
        await s.commit()
    try:
        got = await vector_heart.procedure_similarities(_QUERY, [card.id])
    finally:
        async with db.session() as s:
            await s.execute(delete(Procedure).where(Procedure.agent_id == vector_heart.agent_id))
            await s.commit()

    assert got[card.id] == pytest.approx(0.93, abs=0.02)


class _EmbedderThatFails:
    async def embed(self, text: str) -> list[float]:
        raise RuntimeError("the embedding service is down")

    async def close(self) -> None:
        pass


@pytest.mark.asyncio
async def test_a_closeness_read_whose_embed_fails_knows_no_cosine(db, session):
    """The query cannot be embedded: the read returns no cosine instead of
    raising."""
    heart = _heart(db, _EmbedderThatFails())
    try:
        card = await _add(session, heart, "card-0", kind="strategy")

        assert await heart.procedure_similarities(_QUERY, [card.id], session=session) == {}
    finally:
        await heart.close()


@pytest.mark.asyncio
async def test_a_closeness_read_with_no_embedder_or_no_rows_does_no_work(card_heart, db, session, caplog):
    """No embedding provider: no cosine, and no warning on every turn. No rows
    asked for: no embed call."""
    card = await _add(session, card_heart, "card-0", kind="strategy")
    embedder = AsyncMock()
    counting = _heart(db, embedder)
    try:
        with caplog.at_level("WARNING", logger="nous.heart.procedures"):
            assert await card_heart.procedure_similarities(_QUERY, [card.id], session=session) == {}
        assert await counting.procedure_similarities(_QUERY, [], session=session) == {}
    finally:
        await counting.close()

    assert caplog.text == ""
    embedder.embed.assert_not_awaited()


class _ZeroEmbedder:
    async def embed(self, text: str) -> list[float]:
        return [0.0] * 1536

    async def close(self) -> None:
        pass


@pytest.mark.asyncio
async def test_a_zero_length_vector_has_no_cosine(db, mock_embeddings, session):
    """A zero-length vector has no direction, so it has no cosine (pgvector's is
    NaN, and the probe serves no card on one): a row stored with one is absent,
    and a query embedded as one gets no cosine at all."""
    heart = _heart(db, mock_embeddings)
    zero_query = _heart(db, _ZeroEmbedder())
    try:
        zero = await _add(session, heart, "card-zero", kind="strategy", embedding=[0.0] * 1536)
        near = await _add(
            session, heart, "card-near", kind="strategy", embedding=await mock_embeddings.embed_near(_QUERY, 0.01)
        )
        card = await _add(session, zero_query, "card-0", kind="strategy", embedding=[1.0] + [0.0] * 1535)

        assert set(await heart.procedure_similarities(_QUERY, [zero.id, near.id], session=session)) == {near.id}
        assert await zero_query.procedure_similarities(_QUERY, [card.id], session=session) == {}
    finally:
        await heart.close()
        await zero_query.close()


def _engine(heart, brain=None, **flags):
    """The earlier tests' engine, with the real closeness of ``heart``. Those tests
    count every card the graph rung offers as close; these are about which are.
    (On the code before the floor the method does not exist and is never called.)"""
    engine = _gate_engine(heart, brain, proc_catalog_enabled=False, strategy_cards_retrieval_enabled=True, **flags)
    engine._heart.procedure_similarities = getattr(heart, "procedure_similarities", None)
    return engine


async def _recommended_ids(engine, heart, session) -> list[str]:
    result = await _build(engine, heart, session, text=_QUERY)
    return result.recalled_ids["procedure"]


# ---------------------------------------------------------------------------
# The graph rung's card is held to the floor (the mock vectors are stored
# rows, read back by the probe's pgvector SQL: Postgres lane)
# ---------------------------------------------------------------------------


@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_a_graph_rung_card_far_from_the_turn_is_not_served(vector_heart, mock_embeddings, session):
    """The card of the recalled decision is about something else: cosine about 0
    to the query. The decision's graph link offers it; the floor keeps it out of
    the prompt. The how-to procedure is recommended as before."""
    howto = await _add(session, vector_heart, "howto-rotate-keys", embedding=await mock_embeddings.embed(_QUERY))
    card = await _add(
        session,
        vector_heart,
        "card-far",
        kind="strategy",
        embedding=await mock_embeddings.embed("an unrelated lesson about tax filing"),
    )
    engine = _engine(vector_heart, _seeded_brain((card, 0.9), (howto, 0.5)))

    assert await _recommended_ids(engine, vector_heart, session) == [str(howto.id)]


@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_a_graph_rung_card_close_to_the_turn_is_still_served(vector_heart, mock_embeddings, session):
    """The control: the card of the recalled decision at cosine about 0.93 to the
    query is served by the graph rung after the how-to procedure, as before. It
    fills the allowance, so the cards-only probe is not sent."""
    howto = await _add(session, vector_heart, "howto-rotate-keys", embedding=await mock_embeddings.embed(_QUERY))
    card = await _add(
        session,
        vector_heart,
        "card-close",
        kind="strategy",
        embedding=await mock_embeddings.embed_near(_QUERY, noise=0.01),
    )
    engine = _engine(vector_heart, _seeded_brain((card, 0.9), (howto, 0.5)))
    probes = _card_probes(engine)

    assert await _recommended_ids(engine, vector_heart, session) == [str(howto.id), str(card.id)]
    assert probes == []


@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_a_far_graph_rung_card_leaves_its_slot_to_the_card_nearest_the_turn(
    vector_heart, mock_embeddings, session
):
    """An allowance of one. The graph rung offers a far card; a card that is not
    linked to the recalled decision is near the query. The far card does not take
    the slot, so the probe serves the near one."""
    far = await _add(
        session,
        vector_heart,
        "card-far",
        kind="strategy",
        embedding=await mock_embeddings.embed("an unrelated lesson about tax filing"),
    )
    near = await _add(
        session,
        vector_heart,
        "card-near",
        kind="strategy",
        embedding=await mock_embeddings.embed_near(_QUERY, noise=0.01),
    )
    engine = _engine(vector_heart, _seeded_brain((far, 0.9)))

    assert await _recommended_ids(engine, vector_heart, session) == [str(near.id)]


# ---------------------------------------------------------------------------
# The floor, and a closeness that is not known (both lanes: the closeness is
# given by a stand-in, or there is none)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("cosine", "served"),
    [(0.5, True), (0.49, False), (float("nan"), False)],
    ids=["at-the-floor", "under-it", "not-a-number"],
)
async def test_a_graph_rung_card_is_served_only_at_or_above_the_floor(card_heart, session, cosine, served):
    """The floor is ``procedure_score_floor`` and it is inclusive, as for the
    probe's cards: a card at it is served, one just under it is not, and neither is
    one whose cosine is not a number."""
    card = await _add(session, card_heart, "card-0", kind="strategy")
    engine = _engine(card_heart, _seeded_brain((card, 0.9)), procedure_score_floor=0.5)
    engine._heart.procedure_similarities = AsyncMock(return_value={card.id: cosine})

    assert await _recommended_ids(engine, card_heart, session) == ([str(card.id)] if served else [])


@pytest.mark.asyncio
@pytest.mark.parametrize("why", ["no-embedder", "closeness-fails"])
async def test_a_graph_rung_card_whose_closeness_is_not_known_is_not_served(card_heart, session, why):
    """Without an embedding provider no cosine can be computed, and a failing
    closeness read gives none either. Such a card is not shown; the how-to
    procedure of the same decision still is."""
    howto = await _add(session, card_heart, "howto-deploy", age_minutes=60)
    card = await _add(session, card_heart, "card-0", kind="strategy")
    engine = _engine(card_heart, _seeded_brain((card, 0.9), (howto, 0.5)))
    if why == "closeness-fails":
        engine._heart.procedure_similarities = AsyncMock(side_effect=RuntimeError("the closeness read failed"))

    assert await _recommended_ids(engine, card_heart, session) == [str(howto.id)]


@pytest.mark.asyncio
async def test_a_card_the_floor_keeps_out_is_attributed_in_the_retrieval_trace(card_heart, session):
    """F091: a candidate the gate removes carries a disposition of its own, so it
    is neither counted as rendered nor left unaccounted."""
    card = await _add(session, card_heart, "card-0", kind="strategy")
    engine = _engine(card_heart, _seeded_brain((card, 0.9)))
    engine._heart.procedure_similarities = AsyncMock(return_value={card.id: 0.1})
    seed = str(uuid4())
    trace = RetrievalTrace(query=_QUERY, path="context")

    selected = await engine._select_procedures(
        slots=5,
        critic_skills=[],
        recalled_ids={"fact": [], "decision": [seed]},
        recalled_score_map={seed: 0.9},
        session=session,
        query=_QUERY,
        trace=trace,
        card_slots=1,
    )

    assert selected == []
    dropped = next((c for c in trace.to_dict()["candidates"] if c["id"] == str(card.id)), {})
    assert (dropped.get("disposition"), dropped.get("disposition_stage")) == ("filter_dropped", "strategy_card_floor")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("retrieval", "card_on_the_graph_rung", "asked"),
    [(False, True, 0), (True, False, 0), (True, True, 1)],
    ids=["retrieval-off", "no-card-on-the-graph-rung", "a-card-on-the-graph-rung"],
)
async def test_the_closeness_is_read_once_and_only_for_the_cards_the_graph_rung_met(
    card_heart, session, retrieval, card_on_the_graph_rung, asked
):
    """What the floor costs: one closeness read a turn, for the cards the graph
    rung met, and none when retrieval is off or the rung met no card."""
    howto = await _add(session, card_heart, "howto-deploy", age_minutes=60)
    card = await _add(session, card_heart, "card-0", kind="strategy")
    neighbours = [(howto, 0.5)] + ([(card, 0.9)] if card_on_the_graph_rung else [])
    engine = _gate_engine(
        card_heart,
        _seeded_brain(*neighbours),
        proc_catalog_enabled=False,
        strategy_cards_retrieval_enabled=retrieval,
    )
    engine._heart.procedure_similarities = AsyncMock(return_value={card.id: 1.0})

    await _build(engine, card_heart, session, text=_QUERY)

    calls = engine._heart.procedure_similarities.await_args_list
    assert len(calls) == asked
    assert [list(call.args[1]) for call in calls] == [[card.id]] * asked


@pytest.mark.asyncio
async def test_without_a_query_no_closeness_is_read_and_no_graph_rung_card_is_served(card_heart, session):
    """A selection made with an empty query has nothing to measure a card against:
    no closeness read, and the card of the recalled decision is not served (the
    probe needs a query as well)."""
    card = await _add(session, card_heart, "card-0", kind="strategy")
    engine = _engine(card_heart, _seeded_brain((card, 0.9)))
    engine._heart.procedure_similarities = AsyncMock(return_value={card.id: 1.0})
    seed = str(uuid4())

    selected = await engine._select_procedures(
        slots=5,
        critic_skills=[],
        recalled_ids={"fact": [], "decision": [seed]},
        recalled_score_map={seed: 0.9},
        session=session,
        card_slots=1,
    )

    assert selected == []
    engine._heart.procedure_similarities.assert_not_awaited()


@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_a_graph_rung_card_with_a_zero_length_vector_is_not_served_at_a_floor_of_zero(vector_heart, session):
    """A floor of 0 lets every known cosine through, but a zero-length vector has
    none. The probe refuses such a card (its pgvector cosine is NaN), and so does
    the graph rung (the probe's SQL reads the stored vector: Postgres lane)."""
    card = await _add(session, vector_heart, "card-zero", kind="strategy", embedding=[0.0] * 1536)
    engine = _engine(vector_heart, _seeded_brain((card, 0.9)), procedure_score_floor=0.0)

    assert await _recommended_ids(engine, vector_heart, session) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("cosine", [0.1, None, float("nan")], ids=["under-it", "not-known", "not-a-number"])
async def test_a_graph_rung_card_the_floor_keeps_out_is_dropped_before_its_body_is_fetched(card_heart, session, cosine):
    """The rung's cards have their cosine before it fetches anything, so a card the
    floor keeps out (under it, without a cosine, or with one that is not a number)
    costs no body read."""
    card = await _add(session, card_heart, "card-0", kind="strategy")
    engine = _engine(card_heart, _seeded_brain((card, 0.9)))
    engine._heart.procedure_similarities = AsyncMock(return_value={} if cosine is None else {card.id: cosine})
    real = engine._heart.get_procedure
    fetched: list = []

    async def _get_procedure(procedure_id, *args, **kwargs):
        fetched.append(procedure_id)
        return await real(procedure_id, *args, **kwargs)

    engine._heart.get_procedure = _get_procedure

    assert await _recommended_ids(engine, card_heart, session) == []
    assert card.id not in fetched


@pytest.mark.asyncio
async def test_a_card_that_reached_the_rung_outside_the_cards_only_window_is_not_served(card_heart, session):
    """A real Brain returns a card only on the cards-only call, the one whose cards
    the rung measures. A card that reached the rung any other way has no cosine:
    it is not shown, and the trace says the floor kept it out."""
    card = await _add(session, card_heart, "card-0", kind="strategy")
    brain = _seeded_brain((card, 0.9))
    neighbours = brain.neighbors.side_effect

    async def _default_window_only(*args, cards_only=False, **kwargs):
        return [] if cards_only else await neighbours(*args, **kwargs)

    brain.neighbors = AsyncMock(side_effect=_default_window_only)
    engine = _engine(card_heart, brain)
    engine._heart.procedure_similarities = AsyncMock(return_value={card.id: 1.0})
    seed = str(uuid4())
    trace = RetrievalTrace(query=_QUERY, path="context")

    selected = await engine._select_procedures(
        slots=5,
        critic_skills=[],
        recalled_ids={"fact": [], "decision": [seed]},
        recalled_score_map={seed: 0.9},
        session=session,
        query=_QUERY,
        trace=trace,
        card_slots=1,
    )

    assert selected == []
    dropped = next((c for c in trace.to_dict()["candidates"] if c["id"] == str(card.id)), {})
    assert (dropped.get("disposition"), dropped.get("disposition_stage")) == ("filter_dropped", "strategy_card_floor")


@pytest.mark.asyncio
async def test_a_floor_of_zero_lets_any_known_cosine_through_on_the_graph_rung(card_heart, session):
    """A floor of 0 is a legal setting: a graph rung card with a low but known
    cosine is served, as the probe would serve it."""
    card = await _add(session, card_heart, "card-0", kind="strategy")
    engine = _engine(card_heart, _seeded_brain((card, 0.9)), procedure_score_floor=0.0)
    engine._heart.procedure_similarities = AsyncMock(return_value={card.id: 0.1})

    assert await _recommended_ids(engine, card_heart, session) == [str(card.id)]


@pytest.mark.asyncio
async def test_a_card_past_the_allowance_is_cut_by_the_cap_however_far_it_is(card_heart, session):
    """The allowance is checked before the floor: a card the rung can no longer
    serve is reported as cut by the cap, not as kept out by the floor, so the
    floor's count in the trace holds only the cards it actually kept out."""
    near = await _add(session, card_heart, "card-near", kind="strategy")
    far = await _add(session, card_heart, "card-far", kind="strategy")
    engine = _engine(card_heart, _seeded_brain((near, 0.9), (far, 0.8)))
    engine._heart.procedure_similarities = AsyncMock(return_value={near.id: 0.9, far.id: 0.1})
    seed = str(uuid4())
    trace = RetrievalTrace(query=_QUERY, path="context")

    selected = await engine._select_procedures(
        slots=5,
        critic_skills=[],
        recalled_ids={"fact": [], "decision": [seed]},
        recalled_score_map={seed: 0.9},
        session=session,
        query=_QUERY,
        trace=trace,
        card_slots=1,
    )

    assert [d.id for d in selected] == [near.id]
    cut = next((c for c in trace.to_dict()["candidates"] if c["id"] == str(far.id)), {})
    assert (cut.get("disposition"), cut.get("disposition_stage")) == ("sliced_off", "strategy_card_cap")


@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_the_closeness_read_measures_only_the_rows_asked_for(vector_heart, mock_embeddings, session):
    """Only the given ids are read: another card of the same agent, with an
    embedding, is not."""
    asked = await _add(
        session, vector_heart, "card-asked", kind="strategy", embedding=await mock_embeddings.embed_near(_QUERY, 0.01)
    )
    await _add(
        session,
        vector_heart,
        "card-not-asked",
        kind="strategy",
        embedding=await mock_embeddings.embed_near(_QUERY, 0.02),
    )

    assert set(await vector_heart.procedure_similarities(_QUERY, [asked.id], session=session)) == {asked.id}
