"""The default goal prompt's row: ``IdentityManager``'s goal prompt methods, ``default_prompt.load``, and the
identity's own reads, writes and tools, which never see that row."""

from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from f099_support import OWNER_DIGEST, OWNER_HEADERS
from goal_routes_support import until_blocked_by
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text

from nous.brain.brain import Brain
from nous.cognitive import CognitiveLayer
from nous.goals import default_prompt
from nous.goals.default_prompt import DEFAULT_GOAL_PROMPT
from nous.identity.manager import (
    GOAL_PROMPT_MAX_CHARS,
    GOAL_PROMPT_SECTION,
    SECTIONS,
    VALID_SECTIONS,
    GoalPromptChanged,
    IdentityManager,
)
from nous.identity.tools import register_identity_tools
from nous.storage.models import AgentIdentity

pytestmark = pytest.mark.postgres_only

NOUS = Path(__file__).resolve().parents[1] / "nous"


async def add_agent(db, agent_id: str) -> None:
    """The agent row ``agent_identity``'s foreign key needs."""
    async with db.session() as s:
        await s.execute(
            text("INSERT INTO nous_system.agents (id, name, config) VALUES (:id, 'x', '{}')"), {"id": agent_id}
        )
        await s.commit()


@pytest_asyncio.fixture
async def manager(db) -> IdentityManager:
    agent_id = f"goal-prompt-{uuid.uuid4().hex[:8]}"
    await add_agent(db, agent_id)
    return IdentityManager(db, agent_id)


async def _rows(manager: IdentityManager) -> list[AgentIdentity]:
    async with manager.db.session() as s:
        found = await s.execute(
            select(AgentIdentity)
            .where(AgentIdentity.agent_id == manager.agent_id, AgentIdentity.section == GOAL_PROMPT_SECTION)
            .order_by(AgentIdentity.version)
        )
        return list(found.scalars())


class _Dispatcher:
    """The dispatcher as ``register_identity_tools`` uses it: the tools it registers, by name."""

    def __init__(self) -> None:
        self.tools: dict = {}

    def register(self, name, fn, schema) -> None:
        self.tools[name] = fn


# ---- the methods ---------------------------------------------------------------------------------------------------


async def test_with_nothing_saved_the_built_in_text_is_used(manager, db):
    assert await manager.get_goal_prompt() is None
    assert await default_prompt.load(db, manager.agent_id) == DEFAULT_GOAL_PROMPT


async def test_each_save_is_a_new_version_and_a_reset_keeps_them(manager, db):
    await manager.set_goal_prompt("Work in small steps.", "owner")
    saved = await manager.get_goal_prompt()
    assert (saved.text, saved.updated_by) == ("Work in small steps.", "owner") and saved.updated_at is not None
    await manager.set_goal_prompt("Work in smaller steps.", "proxy")
    rows = await _rows(manager)
    assert [(r.version, r.is_current, r.updated_by) for r in rows] == [(1, False, "owner"), (2, True, "proxy")]
    assert rows[1].previous_version_id == rows[0].id
    assert await default_prompt.load(db, manager.agent_id) == "Work in smaller steps."
    assert await manager.clear_goal_prompt() is True
    assert await manager.clear_goal_prompt() is False
    assert await manager.get_goal_prompt() is None
    assert len(await _rows(manager)) == 2  # the versions stay, none current
    assert await default_prompt.load(db, manager.agent_id) == DEFAULT_GOAL_PROMPT


@pytest.mark.parametrize(
    "value", ["", "   ", "\r\n\n", "\u200b", "\u00ad\u200d\ufeff", "\x01", "a" * (GOAL_PROMPT_MAX_CHARS + 1)]
)
async def test_a_text_that_shows_nothing_or_is_too_long_is_refused(manager, value):
    with pytest.raises(ValueError):
        await manager.set_goal_prompt(value, "owner")
    assert await _rows(manager) == []


async def test_the_text_is_counted_and_stored_as_normalized(manager):
    await manager.set_goal_prompt("a" * GOAL_PROMPT_MAX_CHARS, "owner")
    await manager.set_goal_prompt("a" * GOAL_PROMPT_MAX_CHARS + "\n", "owner")
    sent = "a" * 700 + "\r\n" + "b" * (GOAL_PROMPT_MAX_CHARS - 701)  # one character over as sent
    await manager.set_goal_prompt(sent, "owner")
    assert (await manager.get_goal_prompt()).text == "a" * 700 + "\n" + "b" * (GOAL_PROMPT_MAX_CHARS - 701)
    await manager.set_goal_prompt("one\rtwo", "owner")  # a lone carriage return is a line break too
    assert (await manager.get_goal_prompt()).text == "one\ntwo"
    await manager.set_goal_prompt(" \t\u00a0Work small.\ufeff\n", "owner")  # only spaces, tabs, newlines go
    assert (await manager.get_goal_prompt()).text == "\u00a0Work small.\ufeff"


async def test_a_save_that_meets_another_save_committing_is_refused_and_writes_nothing(manager, db):
    async with db.session() as first:
        await manager.set_goal_prompt("The first text.", "owner", session=first)  # written, not committed
        holder = (await first.execute(text("SELECT pg_backend_pid()"))).scalar_one()
        second = asyncio.create_task(manager.set_goal_prompt("The second text.", "owner"))
        await asyncio.wait_for(until_blocked_by(db, holder), timeout=10)
        await first.commit()
    with pytest.raises(GoalPromptChanged):
        await asyncio.wait_for(second, timeout=10)
    assert (await manager.get_goal_prompt()).text == "The first text."
    assert len(await _rows(manager)) == 1


@pytest.mark.parametrize("writer", ["agent", "initiation", "user", ""])
async def test_only_the_owners_proofs_write_the_text(manager, writer):
    with pytest.raises(ValueError):
        await manager.set_goal_prompt("Work in small steps.", writer)
    assert await _rows(manager) == []


async def test_a_saved_row_over_the_limit_gives_the_built_in_text(manager, db):
    async with db.session() as s:
        s.add(
            AgentIdentity(
                agent_id=manager.agent_id,
                section=GOAL_PROMPT_SECTION,
                content="a" * (GOAL_PROMPT_MAX_CHARS + 1),
                version=1,
                is_current=True,
                updated_by="owner",
            )
        )
        await s.commit()
    assert await default_prompt.load(db, manager.agent_id) == DEFAULT_GOAL_PROMPT
    assert default_prompt.effective((await manager.get_goal_prompt()).text) == (DEFAULT_GOAL_PROMPT, True)


async def test_a_failed_read_gives_the_built_in_text_and_is_logged(caplog):
    class Unreachable:
        def session(self):
            raise ConnectionError("the database is not reachable")

    with caplog.at_level(logging.WARNING, logger="nous.goals.default_prompt"):
        assert await default_prompt.load(Unreachable(), "any-agent") == DEFAULT_GOAL_PROMPT
    assert "ConnectionError" in caplog.text


# ---- the identity never sees it ------------------------------------------------------------------------------------


def test_the_goal_prompt_is_not_an_identity_section():
    assert GOAL_PROMPT_SECTION not in SECTIONS and GOAL_PROMPT_SECTION not in VALID_SECTIONS


async def test_the_identitys_reads_leave_the_goal_prompt_out(manager):
    await manager.update_section("character", "I am Nous.", updated_by="test")
    initiated = await manager.is_initiated()
    await manager.set_goal_prompt("Work in small steps.", "owner")
    manager._invalidate_cache()
    current = await manager.get_current()
    assert current == {"character": "I am Nous."}
    assert "Work in small steps." not in manager.assemble_prompt(current)
    assert await manager.is_initiated() == initiated


async def test_a_reset_of_the_identity_keeps_the_goal_prompt(manager):
    for name in SECTIONS:
        await manager.update_section(name, f"{name} text", updated_by="test")
    await manager.set_goal_prompt("Work in small steps.", "owner")
    await manager.reset_identity()
    assert await manager.get_current() == {}
    assert (await manager.get_goal_prompt()).text == "Work in small steps."


async def test_the_identity_is_still_seeded_from_facts_beside_a_goal_prompt(manager, db):
    await manager.set_goal_prompt("Work in small steps.", "owner")

    async def search_facts(query, *, category, limit, session):
        return [SimpleNamespace(content="Prefers short answers.")] if category == "preference" else []

    async with db.session() as s:
        assert await manager.auto_seed_from_facts(SimpleNamespace(search_facts=search_facts), s) is True
        await s.commit()
    manager._invalidate_cache()
    assert "Prefers short answers." in (await manager.get_current())["preferences"]


async def test_initiation_cannot_complete_with_only_a_goal_prompt(manager):
    await manager.set_goal_prompt("Work in small steps.", "owner")
    tools = _Dispatcher()
    register_identity_tools(tools, manager)
    said = await tools.tools["complete_initiation"]()
    assert said.get("is_error") and "character, preferences" in said["content"][0]["text"]


async def test_the_identity_writes_refuse_the_goal_prompts_section(manager):
    with pytest.raises(ValueError):
        await manager.update_section(GOAL_PROMPT_SECTION, "Work in small steps.", updated_by="owner")
    tools = _Dispatcher()
    register_identity_tools(tools, manager)
    said = await tools.tools["store_identity"](section=GOAL_PROMPT_SECTION, content="Work in small steps.")
    assert said.get("is_error") and "Invalid section" in said["content"][0]["text"]
    assert await _rows(manager) == []


def test_only_the_owners_routes_write_the_goal_prompt():
    """``set_goal_prompt`` and ``clear_goal_prompt`` are called from the goal routes only, and no tool names the
    section."""
    calls = (".set_goal_prompt(", ".clear_goal_prompt(")
    found = {
        path.relative_to(NOUS).as_posix()
        for path in NOUS.rglob("*.py")
        if any(call in path.read_text(encoding="utf-8") for call in calls)
    }
    assert found == {"api/goal_routes.py"}
    inner = {
        path.relative_to(NOUS).as_posix()
        for path in NOUS.rglob("*.py")
        if any(name in path.read_text(encoding="utf-8") for name in ("GOAL_PROMPT_SECTION", "_write_version"))
    }
    assert inner == {"identity/manager.py"}
    named = {
        path.relative_to(NOUS).as_posix()
        for path in NOUS.rglob("*.py")
        if "goal_prompt" in path.read_text(encoding="utf-8")
    }
    assert named <= {
        "identity/manager.py",
        "api/goal_routes.py",
        "goals/default_prompt.py",
        "goals/charter.py",
        "goals/driver.py",
        "goals/chain.py",
    }


# ---- the identity routes -------------------------------------------------------------------------------------------


@pytest_asyncio.fixture
async def identity_client(db, settings, heart, manager):
    brain = Brain(database=db, settings=settings)
    cognitive = CognitiveLayer(brain, heart, settings, identity_prompt="You are Nous.")
    from nous.api.rest import create_app
    from nous.api.runner import AgentRunner

    owner_settings = settings.model_copy(update={"owner_route_secret_sha256": OWNER_DIGEST})
    app = create_app(AsyncMock(spec=AgentRunner), brain, heart, cognitive, db, owner_settings, identity_manager=manager)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=OWNER_HEADERS) as client:
        yield client
    await brain.close()


async def test_get_identity_lists_no_goal_prompt(identity_client, manager):
    await manager.update_section("character", "I am Nous.", updated_by="test")
    await manager.set_goal_prompt("Work in small steps.", "owner")
    manager._invalidate_cache()
    answer = (await identity_client.get("/identity")).json()
    assert answer["sections"] == {"character": "I am Nous."}


async def test_put_identity_refuses_the_goal_prompts_section(identity_client, manager):
    answer = await identity_client.put(f"/identity/{GOAL_PROMPT_SECTION}", json={"content": "Work in small steps."})
    assert answer.status_code == 400 and "Invalid section" in answer.json()["error"]
    assert await _rows(manager) == []
