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
    assert row.context == "Two feeds exist."  # set by the call: the leaked "ctx" does not override it
    assert reason == "It is primary."


async def _fact(db, fid):
    async with db.session() as s:
        return (
            await s.execute(text("SELECT content, category, subject FROM heart.facts WHERE id = :i"), {"i": fid})
        ).one()


@pytest.mark.asyncio
async def test_atom_closing_tags_are_stored_whole(env):
    """B1: the field's own close followed by another closing tag is prose, not markup."""
    tools, db, _ = env
    content = "Atom closing tags: </content> </entry>"
    result = await tools["learn_fact"](content=content, category="technical", subject="atom feeds")
    assert "markup" not in result["content"][0]["text"]
    assert (await _fact(db, _id(result))).content == content


@pytest.mark.asyncio
async def test_a_leaked_category_fills_the_unset_argument(env):
    tools, db, _ = env
    result = await tools["learn_fact"](content=LEAKED_FACT, subject="coastal zone")
    row = await _fact(db, _id(result))
    assert row.content == "The coastal zone forecast is issued twice a day."
    assert row.category == "technical"


@pytest.mark.asyncio
async def test_a_leaked_category_does_not_override_the_call(env):
    tools, db, _ = env
    result = await tools["learn_fact"](content=LEAKED_FACT, category="concept", subject="coastal zone")
    assert (await _fact(db, _id(result))).category == "concept"


@pytest.mark.asyncio
async def test_an_argument_of_the_next_call_is_not_used(env):
    tools, db, _ = env
    content = (
        "The coastal zone forecast is issued twice a day.</content>\n</invoke>\n"
        '<invoke name="learn_fact">\n<parameter name="category">technical'
    )
    result = await tools["learn_fact"](content=content, subject="coastal zone")
    row = await _fact(db, _id(result))
    assert row.content == "The coastal zone forecast is issued twice a day."
    assert row.category is None


@pytest.mark.asyncio
async def test_record_decision_fills_unset_context_and_pattern(env):
    tools, db, _ = env
    result = await tools["record_decision"](
        description=(
            'Use the coastal feed.</description>\n<parameter name="context">Two feeds exist.</parameter>\n'
            "<pattern>prefer the primary feed</pattern>"
        ),
        confidence=0.8,
        category="tooling",
        stakes="low",
    )
    async with db.session() as s:
        row = (
            await s.execute(
                text("SELECT description, context, pattern FROM brain.decisions WHERE id = :i"), {"i": _id(result)}
            )
        ).one()
    assert (row.description, row.context, row.pattern) == (
        "Use the coastal feed.",
        "Two feeds exist.",
        "prefer the primary feed",
    )


async def _decision(db, did):
    async with db.session() as s:
        return (
            await s.execute(text("SELECT description, context, pattern FROM brain.decisions WHERE id = :i"), {"i": did})
        ).one()


@pytest.mark.asyncio
async def test_a_recovered_value_keeps_a_literal_less_than(env):
    """The value runs to its matching closer, not to the first '<'."""
    tools, db, _ = env
    result = await tools["record_decision"](
        description=(
            "Gate the coastal feed on load.</description>"
            '<parameter name="context">Only enable when load < 5; otherwise reject.</parameter></invoke>'
        ),
        confidence=0.8,
        category="tooling",
        stakes="low",
    )
    row = await _decision(db, _id(result))
    assert row.description == "Gate the coastal feed on load."
    assert row.context == "Only enable when load < 5; otherwise reject."


@pytest.mark.asyncio
async def test_a_value_whose_end_cannot_be_told_is_not_recovered(env, caplog):
    tools, db, _ = env
    with caplog.at_level("WARNING", logger="nous.api.tools"):
        result = await tools["record_decision"](
            description='Gate the coastal feed on load.</description>\n<parameter name="context">load <b>high',
            confidence=0.8,
            category="tooling",
            stakes="low",
        )
    row = await _decision(db, _id(result))
    assert row.description == "Gate the coastal feed on load."
    assert row.context is None
    assert "not recovered" in caplog.text and "context" in caplog.text


@pytest.mark.asyncio
async def test_resolve_decisions_stores_a_clean_note(env):
    tools, db, _ = env
    decision = await tools["record_decision"](
        description="Use the coastal feed for the forecast", confidence=0.8, category="tooling", stakes="low"
    )
    await tools["resolve_decisions"](
        resolutions=[
            {
                "decision_id": str(_id(decision)),
                "outcome": "success",
                "resolution_note": 'The feed held up.</resolution_note>\n<parameter name="superseded_by">x',
            }
        ]
    )
    async with db.session() as s:
        note = await s.scalar(text("SELECT outcome_result FROM brain.decisions WHERE id = :i"), {"i": _id(decision)})
    assert note == "The feed held up."


@pytest.mark.asyncio
async def test_clean_text_is_stored_unchanged(env):
    tools, db, _ = env
    content = "Atom entries wrap their text in <content>...</content>"
    result = await tools["learn_fact"](content=content, category="technical", subject="atom feeds")
    assert "markup" not in result["content"][0]["text"]
    async with db.session() as s:
        assert await s.scalar(text("SELECT content FROM heart.facts WHERE id = :i"), {"i": _id(result)}) == content
