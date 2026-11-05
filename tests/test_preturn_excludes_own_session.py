"""The pre-turn context does not show the conversation to itself.

From turn 2 on, the episode search of ``ContextEngine.build`` can return the
calling session's ongoing episode, whose summary echoes this conversation. It is
dropped from "Past Episodes" with the trace reason ``own_session``. Same
boundary as ``own_session_exclusions`` (recall_deep): an ended episode of a
reused session id, and another session's ongoing episode, stay. "Recent
Conversations" reads only ended episodes, so it cannot show the ongoing one.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
import pytest_asyncio
from conftest import MockEmbeddingProvider
from sqlalchemy import text

from nous.cognitive.context import ContextEngine
from nous.cognitive.intent import RetrievalPlan, RetrievalQuery
from nous.cognitive.schemas import FrameSelection
from nous.config import Settings
from nous.heart import EpisodeInput, Heart
from nous.observability.retrieval_logger import RetrievalLogger

pytestmark = pytest.mark.postgres_only

QUERY = "zorbleflux quarnet"


@pytest_asyncio.fixture
async def setup(db):
    """One agent per test: X's ended episode, X's ongoing one, Y's ongoing one."""
    agent = f"test-preturn-{uuid4().hex[:8]}"
    tag = uuid4().hex[:8]
    session_x, session_y = f"sess-x-{tag}", f"sess-y-{tag}"
    heart = Heart(db, Settings(_env_file=None, agent_id=agent), embedding_provider=MockEmbeddingProvider())
    ended = await heart.start_episode(
        EpisodeInput(title="zorbleflux ENDEDX", summary="zorbleflux quarnet budget review ENDEDX", session_id=session_x)
    )
    await heart.end_episode(ended.id, outcome="success")
    ongoing_x = await heart.start_episode(
        EpisodeInput(
            title="zorbleflux ONGOINGX", summary="zorbleflux quarnet travel plans ONGOINGX", session_id=session_x
        )
    )
    ongoing_y = await heart.start_episode(
        EpisodeInput(
            title="zorbleflux ONGOINGY", summary="zorbleflux quarnet garden notes ONGOINGY", session_id=session_y
        )
    )
    ids = {"ENDEDX": str(ended.id), "ONGOINGX": str(ongoing_x.id), "ONGOINGY": str(ongoing_y.id)}
    try:
        yield db, agent, heart, session_x, session_y, ids
    finally:
        async with db.session() as s:
            for table in ("heart.episode_chunks", "heart.episodes"):
                await s.execute(text(f"DELETE FROM {table} WHERE agent_id = :a"), {"a": agent})
            await s.commit()
        await heart.close()


def _frame() -> FrameSelection:
    return FrameSelection(
        frame_id="question", frame_name="Question", description="test", confidence=0.9, match_method="pattern"
    )


async def _build(db, agent, heart, session_id, *, temporal: bool):
    brain = AsyncMock()
    brain.embeddings = MagicMock()
    brain.query.return_value = []
    settings = Settings(
        _env_file=None, agent_id=agent, temporal_context_enabled=temporal, proc_selection_graph_primary=False
    )
    engine = ContextEngine(brain, heart, settings, identity_prompt="")
    plan = RetrievalPlan(
        queries=[
            RetrievalQuery(memory_type=t, query_text=QUERY, limit=8)
            for t in ("decision", "fact", "procedure", "episode")
        ]
    )
    rl = RetrievalLogger(candidate_sample_rate=1.0)
    with patch("nous.cognitive.context.get_active_retrieval_logger", return_value=rl):
        async with db.session() as s:
            return await engine.build(
                agent_id=agent, session_id=session_id, input_text=QUERY, frame=_frame(), session=s, retrieval_plan=plan
            )


def _section(result, label: str) -> str:
    return next((s.content for s in result.sections if s.label == label), "")


def _cands(result) -> dict:
    return {c["id"]: c for c in result.retrieval_trace.to_dict()["candidates"]}


@pytest.mark.asyncio
async def test_turn_two_leaves_out_the_sessions_ongoing_episode(setup):
    """(a) X's ongoing episode is searched but not rendered, and the trace says why."""
    db, agent, heart, session_x, _, ids = setup
    result = await _build(db, agent, heart, session_x, temporal=False)
    assert "ONGOINGX" not in _section(result, "Past Episodes")
    assert ids["ONGOINGX"] not in result.recalled_ids["episode"]
    gone = _cands(result)[ids["ONGOINGX"]]
    assert (gone["disposition"], gone["disposition_stage"]) == ("filter_dropped", "own_session")


@pytest.mark.asyncio
async def test_an_ended_episode_of_the_same_session_stays(setup):
    """(b) A reused session id's ended episode is a past conversation."""
    db, agent, heart, session_x, _, ids = setup
    result = await _build(db, agent, heart, session_x, temporal=False)
    assert "ENDEDX" in _section(result, "Past Episodes")
    assert ids["ENDEDX"] in result.recalled_ids["episode"]


@pytest.mark.asyncio
async def test_another_sessions_ongoing_episode_stays(setup):
    """(c) Only the calling session's own episode is left out.

    Chat episodes are untagged and share two slots (context_episode_untagged_diversity_cap):
    before the fix, X's ongoing episode took one of them and pushed Y's out."""
    db, agent, heart, session_x, _, ids = setup
    result = await _build(db, agent, heart, session_x, temporal=False)
    assert "ONGOINGY" in _section(result, "Past Episodes")
    assert ids["ONGOINGY"] in result.recalled_ids["episode"]


@pytest.mark.asyncio
async def test_no_session_id_filters_nothing(setup):
    """(d) Without a session id the build runs and nothing is dropped as own-session."""
    db, agent, heart, _, _, ids = setup
    result = await _build(db, agent, heart, None, temporal=False)
    assert _section(result, "Past Episodes")
    assert ids["ONGOINGX"] in _cands(result)  # searched, and not dropped as own-session
    assert not [c for c in _cands(result).values() if c.get("disposition_stage") == "own_session"]


@pytest.mark.asyncio
async def test_recent_conversations_cannot_show_the_ongoing_episode(setup):
    """(e) list_recent reads ended episodes only: X's ended one is listed, its ongoing one is not."""
    db, agent, heart, session_x, _, ids = setup
    result = await _build(db, agent, heart, session_x, temporal=True)
    recent = _section(result, "Recent Conversations")
    assert "ENDEDX" in recent
    assert "ONGOINGX" not in recent and "ONGOINGY" not in recent
    assert "ONGOINGX" not in _section(result, "Past Episodes")


@pytest.mark.asyncio
async def test_search_carries_the_session_but_payloads_do_not(setup):
    """The filter keys come with the search rows; model_dump (the REST payload) leaves them out."""
    _, _, heart, session_x, _, ids = setup
    rows = {str(e.id): e for e in await heart.search_episodes(QUERY, limit=8)}
    assert (rows[ids["ONGOINGX"]].session_id, rows[ids["ONGOINGX"]].active) == (session_x, True)
    assert (rows[ids["ENDEDX"]].session_id, rows[ids["ENDEDX"]].active) == (session_x, False)
    dumped = rows[ids["ONGOINGX"]].model_dump(mode="json")
    assert "session_id" not in dumped and "active" not in dumped
