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
# A parameter left open to the end, with no closing tag before it.
OPEN_PARAM_CLEAN = "The marine forecast covers the outer waters."
OPEN_PARAM = OPEN_PARAM_CLEAN + '\n<parameter name="category">technical'


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
        "open_param": await _fact(db, embedder, agent_x, OPEN_PARAM),
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
    assert planned["facts"] == {ids["dirty"], ids["call"], ids["open_param"]}  # not the legit XML, not agent Y's
    assert planned["decisions"] == {ids["decision"]}
    assert planned["episodes"] == {ids["episode"]}
    assert report.unchanged["facts"] == 1  # the legit XML matched the prefilter and was left
    assert [c.calls for c in report.planned["facts"] if c.id == ids["call"]] == [["learn_fact"]]
    assert [c.params for c in report.planned["facts"] if c.id == ids["dirty"]] == [["category"]]
    assert report.cleaned == {}
    assert await _content(db, ids["dirty"]) == DIRTY


@pytest.mark.asyncio
async def test_apply_cleans_reembeds_and_is_idempotent(env):
    db, settings, embedder, ids = env
    tables = list(clean_tool_markup.TABLES)
    report = await clean_tool_markup.run(db, settings, embedder, tables=tables, apply=True)
    assert report.cleaned == {"facts": 2, "decisions": 1, "procedures": 0, "censors": 0, "episodes": 1}
    assert report.skipped_calls["facts"] == 1
    assert await _content(db, ids["dirty"]) == CLEAN
    assert await _content(db, ids["open_param"]) == OPEN_PARAM_CLEAN
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


def _floats(vec: str) -> list[float]:
    return [float(x) for x in vec.strip("[]").split(",")]


@pytest.mark.asyncio
async def test_episode_embed_text_is_the_write_paths(env):
    """An ended episode is embedded from title, summary, outcome and lessons, an ongoing one from title and
    summary: the script re-embeds from the same text, compared here with the vector the write path stored."""
    db, settings, embedder, ids = env
    heart = Heart(db, settings, embedding_provider=embedder)
    try:
        ended = await heart.start_episode(EpisodeInput(title="Harbour check", summary="Checked the harbour gauge"))
        await heart.end_episode(ended.id, outcome="success", lessons_learned=["gauge reads high", "recalibrate"])
    finally:
        await heart.close()
    async with db.session() as s:
        rows = (
            await s.execute(
                text(
                    "SELECT id, title, summary, outcome, lessons_learned, ended_at, embedding::text AS embedding "
                    "FROM heart.episodes WHERE id = ANY(:ids)"
                ),
                {"ids": [ended.id, ids["episode"]]},
            )
        ).mappings()
        rows = [dict(r) for r in rows]
    assert {r["ended_at"] is None for r in rows} == {True, False}  # one ongoing, one ended
    for row in rows:
        built = clean_tool_markup.TABLES["episodes"].embed_text(row, settings)
        expected = await embedder.embed(built)
        assert max(abs(a - b) for a, b in zip(_floats(row["embedding"]), expected, strict=True)) < 1e-5


@pytest.mark.asyncio
async def test_apply_skips_a_row_whose_embed_inputs_changed_since_read(env):
    """The guard covers every column the new embedding is built from, not only the cleaned ones."""
    db, settings, embedder, ids = env
    async with db.session() as s:
        planned = await clean_tool_markup.scan(s, settings, ["decisions"])
        await s.rollback()
    (change,) = planned["decisions"]
    async with db.session() as s:  # the app edits the clean context meanwhile
        await s.execute(
            text("UPDATE brain.decisions SET context = 'Newer context.' WHERE id = :i"), {"i": ids["decision"]}
        )
        await s.commit()
    async with db.session() as s:
        assert not await clean_tool_markup._write(
            s, change, settings.agent_id, await embedder.embed(change.new_embed_text)
        )
        await s.rollback()
        desc = await s.scalar(text("SELECT description FROM brain.decisions WHERE id = :i"), {"i": ids["decision"]})
    assert desc.startswith("Use the coastal feed.</description>")


@pytest.mark.asyncio
async def test_apply_without_a_provider_refuses(env):
    db, settings, _, ids = env
    with pytest.raises(ValueError):
        await clean_tool_markup.run(db, settings, None, tables=["facts"], apply=True)
    assert await _content(db, ids["dirty"]) == DIRTY
