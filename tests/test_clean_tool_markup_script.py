"""scripts/ops/clean_tool_markup.py: the dry run writes nothing, --apply cleans and re-embeds, both agent-scoped."""

from __future__ import annotations

import json
from uuid import uuid4

import pytest
import pytest_asyncio
from conftest import MockEmbeddingProvider
from sqlalchemy import text

from nous.brain.brain import Brain
from nous.brain.schemas import RecordInput
from nous.config import Settings
from nous.heart import EpisodeInput, Heart
from scripts.ops import clean_tool_markup

pytestmark = pytest.mark.postgres_only

CLEAN = "The coastal zone forecast is issued twice a day."
DIRTY = CLEAN + '</content>\n<parameter name="category">technical'
WITH_CALL = (
    CLEAN + "</content>\n<category>technical</category>\n</invoke>\n"
    '<invoke name="learn_fact">\n<parameter name="content">Another fact.</content>\n</invoke>'
)
LEGIT = "Atom entries wrap their text in <content>...</content>"


def _vec(v: list[float]) -> str:
    return "[" + ",".join(f"{x:.6f}" for x in v) + "]"


async def _fact(db, embedder, agent: str, content: str) -> str:
    async with db.session() as s:
        fid = await s.scalar(
            text(
                "INSERT INTO heart.facts (agent_id, content, category, source, embedding) "
                "VALUES (:a, :c, 'technical', 'user_direct', CAST(:v AS vector)) RETURNING id"
            ),
            {"a": agent, "c": content, "v": _vec(await embedder.embed(content))},
        )
        await s.commit()
    return fid


async def _content(db, fid) -> str:
    async with db.session() as s:
        return await s.scalar(text("SELECT content FROM heart.facts WHERE id = :i"), {"i": fid})


@pytest_asyncio.fixture
async def env(db):
    embedder = MockEmbeddingProvider()
    agent_x, agent_y = f"test-clean-x-{uuid4().hex[:8]}", f"test-clean-y-{uuid4().hex[:8]}"
    settings = Settings().model_copy(update={"agent_id": agent_x})
    ids = {
        "dirty": await _fact(db, embedder, agent_x, DIRTY),
        "call": await _fact(db, embedder, agent_x, WITH_CALL),
        "legit": await _fact(db, embedder, agent_x, LEGIT),
        "other_agent": await _fact(db, embedder, agent_y, DIRTY),
    }
    brain = Brain(database=db, settings=settings, embedding_provider=embedder)
    decision = await brain.record(
        RecordInput(
            description='Use the coastal feed.</description>\n<parameter name="context">ctx',
            confidence=0.8,
            category="tooling",
            stakes="low",
        )
    )
    ids["decision"] = decision.id
    heart = Heart(db, settings, embedding_provider=embedder)
    episode = await heart.start_episode(EpisodeInput(title="Coastal forecast", summary="Coastal forecast chat"))
    ids["episode"] = episode.id
    async with db.session() as s:
        await s.execute(
            text("UPDATE heart.episodes SET structured_summary = CAST(:j AS jsonb) WHERE id = :i"),
            {"i": episode.id, "j": json.dumps({"title": "Coastal</title>\n</invoke>", "summary": "kept"})},
        )
        await s.commit()
    try:
        yield db, settings, embedder, ids
    finally:
        async with db.session() as s:
            for agent in (agent_x, agent_y):
                for table in ("heart.facts", "brain.decisions", "heart.episodes"):
                    await s.execute(text(f"DELETE FROM {table} WHERE agent_id = :a"), {"a": agent})
            await s.commit()
        await heart.close()
        await brain.close()


@pytest.mark.asyncio
async def test_dry_run_counts_this_agent_and_writes_nothing(env):
    db, settings, embedder, ids = env
    report = await clean_tool_markup.run(db, settings, embedder, tables=list(clean_tool_markup.TABLES), apply=False)
    planned = {k: {c.id for c in v} for k, v in report.planned.items()}
    assert planned["facts"] == {ids["dirty"], ids["call"]}  # not the legit XML, not agent Y's
    assert planned["decisions"] == {ids["decision"]}
    assert planned["episodes"] == {ids["episode"]}
    assert [c.calls for c in report.planned["facts"] if c.id == ids["call"]] == [["learn_fact"]]
    assert [c.params for c in report.planned["facts"] if c.id == ids["dirty"]] == [["category"]]
    assert report.cleaned == {}
    assert await _content(db, ids["dirty"]) == DIRTY


@pytest.mark.asyncio
async def test_apply_cleans_reembeds_and_is_idempotent(env):
    db, settings, embedder, ids = env
    tables = list(clean_tool_markup.TABLES)
    report = await clean_tool_markup.run(db, settings, embedder, tables=tables, apply=True)
    assert report.cleaned == {"facts": 1, "decisions": 1, "procedures": 0, "censors": 0, "episodes": 1}
    assert report.skipped_calls["facts"] == 1
    assert await _content(db, ids["dirty"]) == CLEAN
    assert await _content(db, ids["call"]) == WITH_CALL  # left alone without --include-calls
    assert await _content(db, ids["legit"]) == LEGIT
    assert await _content(db, ids["other_agent"]) == DIRTY
    async with db.session() as s:
        vec = await s.scalar(text("SELECT embedding::text FROM heart.facts WHERE id = :i"), {"i": ids["dirty"]})
        desc = await s.scalar(text("SELECT description FROM brain.decisions WHERE id = :i"), {"i": ids["decision"]})
        ss = await s.scalar(
            text("SELECT structured_summary::text FROM heart.episodes WHERE id = :i"), {"i": ids["episode"]}
        )
    stored = [float(x) for x in vec.strip("[]").split(",")]
    assert max(abs(a - b) for a, b in zip(stored, await embedder.embed(CLEAN), strict=True)) < 1e-5
    assert desc == "Use the coastal feed."
    assert json.loads(ss) == {"title": "Coastal", "summary": "kept"}

    report = await clean_tool_markup.run(db, settings, embedder, tables=tables, apply=True, include_calls=True)
    assert report.cleaned["facts"] == 1
    assert await _content(db, ids["call"]) == CLEAN

    again = await clean_tool_markup.run(db, settings, embedder, tables=tables, apply=False)
    assert all(not v for v in again.planned.values())


@pytest.mark.asyncio
async def test_apply_without_a_provider_refuses(env):
    db, settings, _, ids = env
    with pytest.raises(ValueError):
        await clean_tool_markup.run(db, settings, None, tables=["facts"], apply=True)
    assert await _content(db, ids["dirty"]) == DIRTY
