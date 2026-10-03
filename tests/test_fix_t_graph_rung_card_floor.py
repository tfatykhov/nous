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

import pytest
import pytest_asyncio
from sqlalchemy import delete
from test_fix_e_strategy_card_gate import _add, _heart

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
