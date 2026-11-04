"""The write tools store model text without leaked tool-call markup, and say so in their result."""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from conftest import MockEmbeddingProvider
from sqlalchemy import text

from nous.api.tools import create_nous_tools
from nous.brain.brain import Brain
from nous.config import Settings
from nous.heart import Heart

pytestmark = pytest.mark.postgres_only

LEAKED_FACT = 'The coastal zone forecast is issued twice a day.</content>\n<parameter name="category">technical'
LEAKED_CALL = (
    "The first fact about the coastal zone forecast.</content>\n<category>technical</category>\n</invoke>\n"
    '<invoke name="learn_fact">\n<parameter name="content">Second fact.</content>\n</invoke>'
)


@pytest_asyncio.fixture
async def env(db):
    agent = f"test-markup-{uuid4().hex[:8]}"
    settings = Settings().model_copy(update={"agent_id": agent})
    embedder = MockEmbeddingProvider()
    heart = Heart(db, settings, embedding_provider=embedder)
    brain = Brain(database=db, settings=settings, embedding_provider=embedder)
    tools = create_nous_tools(brain, heart, settings)
    try:
        yield tools, db, embedder
    finally:
        async with db.session() as s:
            await s.execute(text("DELETE FROM heart.facts WHERE agent_id = :a"), {"a": agent})
            await s.execute(text("DELETE FROM brain.decisions WHERE agent_id = :a"), {"a": agent})
            await s.commit()
        await heart.close()
        await brain.close()


def _id(result: dict) -> UUID:
    line = next(ln for ln in result["content"][0]["text"].splitlines() if ln.startswith("ID: "))
    return UUID(line.removeprefix("ID: "))


@pytest.mark.asyncio
async def test_learn_fact_stores_and_embeds_the_clean_text(env):
    tools, db, embedder = env
    result = await tools["learn_fact"](content=LEAKED_FACT, category="technical", subject="coastal zone")
    assert "Fact learned successfully" in result["content"][0]["text"]
    assert "markup" in result["content"][0]["text"]
    async with db.session() as s:
        row = (
            await s.execute(text("SELECT content, embedding::text FROM heart.facts WHERE id = :i"), {"i": _id(result)})
        ).one()
    assert row.content == "The coastal zone forecast is issued twice a day."
    expected = await embedder.embed("The coastal zone forecast is issued twice a day.")
    stored = [float(x) for x in row.embedding.strip("[]").split(",")]
    assert max(abs(a - b) for a, b in zip(stored, expected, strict=True)) < 1e-5


@pytest.mark.asyncio
async def test_learn_fact_names_a_call_that_never_ran(env):
    tools, db, _ = env
    result = await tools["learn_fact"](content=LEAKED_CALL, category="technical", subject="coastal zone")
    out = result["content"][0]["text"]
    assert "did not run (learn_fact)" in out
    async with db.session() as s:
        content = await s.scalar(text("SELECT content FROM heart.facts WHERE id = :i"), {"i": _id(result)})
    assert content == "The first fact about the coastal zone forecast."


@pytest.mark.asyncio
async def test_record_decision_stores_clean_text(env):
    tools, db, _ = env
    result = await tools["record_decision"](
        description='Use the coastal feed.</description>\n<parameter name="context">ctx',
        confidence=0.8,
        category="tooling",
        stakes="low",
        context='Two feeds exist.</context>\n<parameter name="pattern">prefer primary',
        reasons=[{"type": "analysis", "text": 'It is primary.</parameter>\n<parameter name="type">analysis'}],
    )
    async with db.session() as s:
        row = (
            await s.execute(text("SELECT description, context FROM brain.decisions WHERE id = :i"), {"i": _id(result)})
        ).one()
        reason = await s.scalar(
            text("SELECT text FROM brain.decision_reasons WHERE decision_id = :i"), {"i": _id(result)}
        )
    assert row.description == "Use the coastal feed."
    assert row.context == "Two feeds exist."
    assert reason == "It is primary."


@pytest.mark.asyncio
async def test_clean_text_is_stored_unchanged(env):
    tools, db, _ = env
    content = "Atom entries wrap their text in <content>...</content>"
    result = await tools["learn_fact"](content=content, category="technical", subject="atom feeds")
    assert "markup" not in result["content"][0]["text"]
    async with db.session() as s:
        assert await s.scalar(text("SELECT content FROM heart.facts WHERE id = :i"), {"i": _id(result)}) == content
