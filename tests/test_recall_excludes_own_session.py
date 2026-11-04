"""recall_deep does not hand back the conversation it is called from.

The calling session's ongoing episode and its dialogue chunks are left out of
every leg, and out of the parent-episode leg a fact of this session would pull
in. A call from another session still finds them. Facts of the session, and a
document stored on the session's episode, are still returned.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
import pytest_asyncio
from conftest import MockEmbeddingProvider
from sqlalchemy import text

from nous.api.tools import create_nous_tools
from nous.brain.brain import Brain
from nous.config import Settings
from nous.heart import EpisodeInput, FactInput, Heart

pytestmark = pytest.mark.postgres_only


@pytest_asyncio.fixture
async def setup(db):
    agent = f"test-own-session-{uuid4().hex[:8]}"
    settings = Settings().model_copy(
        update={
            "agent_id": agent,
            "episode_chunks_enabled": True,
            # Keyword leg on, so the chunks are found without a real embedder.
            "chunk_hybrid_search_enabled": True,
            "recall_include_parent_episodes": True,
        }
    )
    embedder = MockEmbeddingProvider()
    heart = Heart(db, settings, embedding_provider=embedder)
    brain = Brain(database=db, settings=settings)
    tag = uuid4().hex[:8]
    session_s = f"sess-s-{tag}"
    session_t = f"sess-t-{tag}"
    ep = await heart.start_episode(
        EpisodeInput(
            title="zanzibar quokka planning",
            summary=f"zanzibar quokka planning conversation EPMARK{tag}",
            session_id=session_s,
        )
    )
    async with db.session() as s:
        for idx, (kind, marker) in enumerate((("dialogue", "DIALOGUEMARK"), ("document", "DOCMARK"))):
            content = f"zanzibar quokka chunk {marker}{tag}"
            vec = await embedder.embed(content)
            await s.execute(
                text(
                    "INSERT INTO heart.episode_chunks "
                    "(agent_id, episode_id, chunk_index, content, embedding, source_kind) "
                    "VALUES (:a, :e, :i, :c, CAST(:v AS vector), :k)"
                ),
                {
                    "a": agent,
                    "e": str(ep.id),
                    "i": idx,
                    "c": content,
                    "v": "[" + ",".join(f"{x:.6f}" for x in vec) + "]",
                    "k": kind,
                },
            )
        await s.commit()
    await heart.learn(
        FactInput(
            content=f"The zanzibar quokka trip is planned for March FACTMARK{tag}",
            category="technical",
            subject="zanzibar quokka trip",
            source="user_direct",
            source_episode_id=ep.id,
        )
    )
    tools = create_nous_tools(brain=brain, heart=heart, settings=settings)
    try:
        yield tools, tag, session_s, session_t
    finally:
        async with db.session() as s:
            for table in ("heart.episode_chunks", "heart.facts", "heart.episodes"):
                await s.execute(text(f"DELETE FROM {table} WHERE agent_id = :a"), {"a": agent})
            await s.commit()
        await heart.close()
        await brain.close()


async def _recall(tools, session_id: str) -> str:
    out = await tools["recall_deep"](query="zanzibar quokka", limit=20, _session_id=session_id)
    return out["content"][0]["text"]


@pytest.mark.asyncio
async def test_call_from_the_session_leaves_out_its_episode_and_dialogue_chunks(setup):
    tools, tag, session_s, _ = setup
    text_out = await _recall(tools, session_s)
    assert f"EPMARK{tag}" not in text_out  # neither the episode leg nor the parent-episode leg
    assert f"DIALOGUEMARK{tag}" not in text_out
    # Kept: a fact learned in the session, and a document stored on its episode.
    assert f"FACTMARK{tag}" in text_out
    assert f"DOCMARK{tag}" in text_out


@pytest.mark.asyncio
async def test_call_from_another_session_still_finds_them(setup):
    tools, tag, _, session_t = setup
    text_out = await _recall(tools, session_t)
    assert f"EPMARK{tag}" in text_out
    assert f"DIALOGUEMARK{tag}" in text_out
    assert f"FACTMARK{tag}" in text_out


@pytest.mark.asyncio
async def test_an_ended_episode_of_the_same_session_is_a_past_conversation(setup, db):
    """A session id can be reused after the conversation closed (REST clients, intent threads):
    the closed episode is no longer in the model's context, so it is recallable again."""
    tools, tag, session_s, _ = setup
    async with db.session() as s:
        await s.execute(
            text("UPDATE heart.episodes SET active = false, ended_at = now() WHERE session_id = :s"),
            {"s": session_s},
        )
        await s.commit()
    text_out = await _recall(tools, session_s)
    assert f"EPMARK{tag}" in text_out
    assert f"DIALOGUEMARK{tag}" in text_out
