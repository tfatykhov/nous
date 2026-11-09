"""F108 Phase 3: the "Tool notes" procedure, rendered from the notes table at read time (real Postgres)."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import pytest_asyncio
from job_support import lesson_heart  # noqa: F401
from sqlalchemy import select
from sqlalchemy import text as sa_text

from nous.api.tools import ToolDispatcher, create_nous_tools, register_nous_tools
from nous.config import Settings
from nous.handlers import tool_notes as tool_notes_module
from nous.handlers.tool_notes import (
    TOOL_NOTES_PROCEDURE,
    ToolNotesReview,
    ToolNoteStore,
    is_tool_notes_procedure,
    render_notes,
    sync_procedure,
)
from nous.heart.procedures import LEARNED_PARAMS, LEARNED_WHERE
from nous.heart.schemas import ProcedureInput
from nous.observability.tool_patterns import Pattern
from nous.storage.models import Procedure
from tests.test_work_view_dashboard import insert_call

NOW = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)
DESCRIPTION = (
    "Notes on past tool mistakes. Read before a tool you have not used lately, "
    "after a tool fails, or when choosing a tool."
)


def pat(tool="bash", key="boom", hits=6, calls=30, kind="error_repeat") -> Pattern:
    return Pattern(kind=kind, tool_name=tool, key=key, hits=hits, calls=calls, guard=None, example=key)


@pytest_asyncio.fixture
async def env(lesson_heart, db):  # noqa: F811
    store = ToolNoteStore(db, lesson_heart.agent_id)
    yield SimpleNamespace(heart=lesson_heart, db=db, store=store, agent_id=lesson_heart.agent_id)
    async with db.session() as s:
        await s.execute(sa_text("DELETE FROM nous_system.tool_note WHERE agent_id = :a"), {"a": lesson_heart.agent_id})
        await s.commit()
    async with db.engine.begin() as conn:
        for t in ("tool_call_log", "agent_turn_log"):
            delete = sa_text(f"DELETE FROM nous_system.{t} WHERE agent_id = :a")
            await conn.execute(delete, {"a": lesson_heart.agent_id})


def tools_for(env, *, enabled=True, log=True):
    settings = Settings(_env_file=None).model_copy(update={"tool_notes_enabled": enabled, "tool_call_log_enabled": log})
    return create_nous_tools(MagicMock(), env.heart, settings)


async def text_of(tools, ident) -> str:
    return (await tools["get_procedure"](procedure_id=ident))["content"][0]["text"]


async def procedure_rows(env) -> list[Procedure]:
    async with env.db.session() as s:
        res = await s.execute(
            select(Procedure).where(Procedure.agent_id == env.agent_id, Procedure.name == TOOL_NOTES_PROCEDURE)
        )
        return list(res.scalars().all())


async def add_note(env, tool, text_, key="boom"):
    return await env.store.add(pat(tool=tool, key=key), text_, now=NOW)


@pytest.mark.asyncio
async def test_get_procedure_renders_current_notes(env):
    n1 = await add_note(env, "bash", "check the path first", "a")
    await add_note(env, "write_file", "give an absolute path", "b")
    await sync_procedure(env.heart, env.store, True)
    tools = tools_for(env)
    out = await text_of(tools, TOOL_NOTES_PROCEDURE)
    assert "bash: check the path first" in out
    assert "write_file: give an absolute path" in out
    assert out.index("bash:") < out.index("write_file:")  # grouped by tool, tools sorted
    await env.store.retire(n1, {"reason": "no_improvement"}, now=NOW)
    out = await text_of(tools, TOOL_NOTES_PROCEDURE)  # rendered at read time, not stored
    assert "check the path first" not in out and "give an absolute path" in out
    assert (await procedure_rows(env))[0].implementation_notes in (None, [])


@pytest.mark.asyncio
async def test_notes_are_newest_first_within_a_tool(env):
    await env.store.add(pat(key="a"), "older note", now=datetime(2026, 11, 8, tzinfo=UTC))
    await env.store.add(pat(key="b"), "newer note", now=NOW)
    await sync_procedure(env.heart, env.store, True)
    out = await text_of(tools_for(env), TOOL_NOTES_PROCEDURE)
    assert out.index("newer note") < out.index("older note")


@pytest.mark.asyncio
async def test_sync_creates_activates_and_retires(env):
    await sync_procedure(env.heart, env.store, True)
    assert await procedure_rows(env) == []  # no notes, nothing created
    n = await add_note(env, "bash", "check the path first")
    await sync_procedure(env.heart, env.store, True)
    (row,) = await procedure_rows(env)
    assert row.active and row.description == DESCRIPTION and row.tags == ["tool_notes"]
    assert row.domain == "tools" and not row.implementation_notes
    await env.store.retire(n, {"reason": "unused"}, now=NOW)
    await sync_procedure(env.heart, env.store, True)
    (row,) = await procedure_rows(env)
    assert not row.active
    await add_note(env, "bash", "check the path first", "again")
    await sync_procedure(env.heart, env.store, True)
    rows = await procedure_rows(env)
    assert len(rows) == 1 and rows[0].id == row.id and rows[0].active  # the same row, no duplicate name
    await sync_procedure(env.heart, env.store, False)  # flag off retires it even with notes
    assert not (await procedure_rows(env))[0].active
    await sync_procedure(env.heart, env.store, False)  # idempotent
    await sync_procedure(env.heart, env.store, True)
    await sync_procedure(env.heart, env.store, True)
    assert len(await procedure_rows(env)) == 1


@pytest.mark.asyncio
async def test_no_notes_means_no_catalog_entry_and_no_body(env):
    n = await add_note(env, "bash", "check the path first")
    await sync_procedure(env.heart, env.store, True)
    names, _ = await env.heart.list_procedures()
    assert TOOL_NOTES_PROCEDURE in {s.name for s in names}
    await env.store.retire(n, {"reason": "unused"}, now=NOW)
    await sync_procedure(env.heart, env.store, True)
    names, _ = await env.heart.list_procedures()
    assert TOOL_NOTES_PROCEDURE not in {s.name for s in names}
    assert await text_of(tools_for(env), TOOL_NOTES_PROCEDURE) == f"No procedure found for '{TOOL_NOTES_PROCEDURE}'."


@pytest.mark.asyncio
async def test_flag_off_after_notes_exist_serves_nothing(env):
    await add_note(env, "bash", "check the path first")
    await sync_procedure(env.heart, env.store, True)
    (row,) = await procedure_rows(env)
    on = tools_for(env, enabled=True)
    assert "check the path first" in await text_of(on, TOOL_NOTES_PROCEDURE)
    assert "check the path first" in await text_of(on, str(row.id))
    await sync_procedure(env.heart, env.store, False)
    for enabled in (False, True):  # inactive: nothing, whatever the flag says
        t = tools_for(env, enabled=enabled)
        assert (await text_of(t, TOOL_NOTES_PROCEDURE)).startswith("No procedure found")
        assert (await text_of(t, str(row.id))).startswith("No procedure found")
    names, _ = await env.heart.list_procedures()
    assert TOOL_NOTES_PROCEDURE not in {s.name for s in names}
    # the row is active but the flag is off (the flag flipped without a sync): nothing is served either
    await env.heart.reactivate_procedure(row.id)
    off = tools_for(env, enabled=False)
    assert (await text_of(off, TOOL_NOTES_PROCEDURE)).startswith("No procedure found")
    assert (await text_of(off, str(row.id))).startswith("No procedure found")
    await env.heart.retire_procedure(row.id)
    await sync_procedure(env.heart, env.store, True)  # flag on again: back, same notes, same row
    again = tools_for(env, enabled=True)
    assert "check the path first" in await text_of(again, TOOL_NOTES_PROCEDURE)
    assert "check the path first" in await text_of(again, str(row.id))


def test_description_fits_the_catalog():
    s = Settings(_env_file=None)
    assert len(DESCRIPTION) == 118 and len(DESCRIPTION) <= s.proc_catalog_desc_chars
    assert tool_notes_module.TOOL_NOTES_DESCRIPTION == DESCRIPTION


@pytest.mark.asyncio
async def test_get_procedure_other_procedures_unchanged(env):
    created = await env.heart.store_procedure(
        ProcedureInput(
            name="Run the report by hand",
            domain="ops",
            description="steps",
            goals=["report"],
            core_tools=["bash"],
            core_patterns=["gather metrics"],
            implementation_notes=["then summarise"],
            tags=["auto:howto"],
        )
    )
    out = await text_of(tools_for(env), created.name)
    assert out == "\n".join(
        [
            "**Run the report by hand** (ops)",
            "Description: steps",
            "Triggers: report",
            "Tools: bash",
            "",
            "Core patterns:",
            "- gather metrics",
            "",
            "Implementation notes:",
            "then summarise",
            "\nActivated: 0x | Status: active",
        ]
    )
    # a user procedure that merely shares the name is not the notes procedure and keeps its own body
    lookalike = await env.heart.store_procedure(
        ProcedureInput(name="Tool notes", implementation_notes=["mine"], tags=["other"])
    )
    assert not is_tool_notes_procedure(lookalike)
    assert "mine" in await text_of(tools_for(env, enabled=False), lookalike.name)


def test_render_notes_shape():
    assert render_notes([]) == []
    n = lambda tool, text_, day: SimpleNamespace(  # noqa: E731
        tool_name=tool, note_text=text_, created_at=datetime(2026, 11, day, tzinfo=UTC)
    )
    assert render_notes([n("write_file", "w", 1), n("bash", "old", 1), n("bash", "new", 5)]) == [
        "bash: new",
        "bash: old",
        "write_file: w",
    ]


@pytest.mark.asyncio
async def test_tool_notes_adds_no_agent_tool():
    def names(enabled: bool) -> set[str]:
        d = ToolDispatcher()
        settings = Settings(_env_file=None).model_copy(update={"tool_notes_enabled": enabled})
        register_nous_tools(d, MagicMock(), MagicMock(), settings)
        return set(d._handlers)

    assert names(True) == names(False)
    source = Path(tool_notes_module.__file__).read_text(encoding="utf-8")
    assert not re.search(r"^\s*(from|import)\s+nous\.api\.tools", source, re.M)
    assert "register(" not in source


@pytest.mark.asyncio
async def test_procedure_is_not_a_learned_procedure(env):
    await add_note(env, "bash", "check the path first")
    await sync_procedure(env.heart, env.store, True)
    (row,) = await procedure_rows(env)
    assert not any(t.startswith("auto:") for t in row.tags)
    async with env.db.session() as s:
        found = (
            await s.execute(
                sa_text(f"SELECT id FROM heart.procedures WHERE agent_id = :agent AND {LEARNED_WHERE}"),
                {"agent": env.agent_id, **LEARNED_PARAMS},
            )
        ).all()
    assert row.id not in {r.id for r in found}


@pytest.mark.asyncio
async def test_is_tool_notes_procedure_needs_name_and_tag():
    d = SimpleNamespace
    assert is_tool_notes_procedure(d(name="Tool notes", tags=["tool_notes"]))
    assert is_tool_notes_procedure(d(name="tool NOTES", tags=["tool_notes"]))
    assert not is_tool_notes_procedure(d(name="Tool notes", tags=["x"]))
    assert not is_tool_notes_procedure(d(name="Other", tags=["tool_notes"]))
    assert not is_tool_notes_procedure(d(name="Tool notes", tags=None))
    assert not is_tool_notes_procedure(MagicMock())


class _Client:
    supports_call_context = True

    def __init__(self, answer):
        self.answer = answer

    async def call(self, payload, **kwargs):
        r = MagicMock()
        r.content = [{"type": "text", "text": json.dumps(self.answer)}]
        return r


@pytest.mark.asyncio
async def test_run_sleep_syncs_the_procedure(env):
    for i in range(10):
        await insert_call(
            env.db,
            env.agent_id,
            None,
            i,
            session_id=None,
            tool="bash",
            input_text=f"in-{i}",
            is_error=i < 6,
            outcome="error" if i < 6 else "ran",
            error_text="boom" if i < 6 else None,
            started_at=datetime(2026, 11, 7, 12, tzinfo=UTC),
        )
    settings = SimpleNamespace(
        tool_notes_enabled=True, tool_call_log_enabled=True, background_model="m", agent_id=env.agent_id
    )
    pid = Pattern(kind="error_repeat", tool_name="bash", key="boom", hits=6, calls=10, guard=None, example="boom").id
    client = _Client({"notes": [{"pattern": pid, "action": "write", "text": "check the path first", "replaces": None}]})
    review = ToolNotesReview(env.db, env.heart, settings, client, clock=lambda: NOW)
    out = await review.run_sleep()
    assert out["written"] == 1
    (row,) = await procedure_rows(env)
    assert row.active
    (note,) = await env.store.active()
    await env.store.retire(note.id, {"reason": "no_improvement"}, now=NOW)
    await review.run_sleep()  # nothing new to offer (the pattern is blocked), the last note is gone
    assert not (await procedure_rows(env))[0].active


@pytest.mark.asyncio
async def test_run_sleep_with_the_tool_call_log_off_retires_the_procedure(env):
    await add_note(env, "bash", "check the path first")
    await sync_procedure(env.heart, env.store, True)
    assert (await procedure_rows(env))[0].active
    settings = SimpleNamespace(
        tool_notes_enabled=True, tool_call_log_enabled=False, background_model="m", agent_id=env.agent_id
    )
    review = ToolNotesReview(env.db, env.heart, settings, _Client({"notes": []}), clock=lambda: NOW)
    await review.run_sleep()
    assert not (await procedure_rows(env))[0].active
    settings.tool_call_log_enabled = True  # both on: active again
    await review.run_sleep()
    assert (await procedure_rows(env))[0].active


@pytest.mark.asyncio
async def test_get_procedure_serves_nothing_with_the_tool_call_log_off(env):
    await add_note(env, "bash", "check the path first")
    await sync_procedure(env.heart, env.store, True)  # the row is active, the log flag is off
    (row,) = await procedure_rows(env)
    off = tools_for(env, enabled=True, log=False)
    assert (await text_of(off, TOOL_NOTES_PROCEDURE)).startswith("No procedure found")
    assert (await text_of(off, str(row.id))).startswith("No procedure found")
    assert "check the path first" in await text_of(tools_for(env, enabled=True, log=True), TOOL_NOTES_PROCEDURE)


def test_the_startup_sync_needs_both_flags():
    source = (Path(__file__).parent.parent / "nous" / "main.py").read_text(encoding="utf-8")
    assert "settings.tool_notes_enabled and settings.tool_call_log_enabled" in source


@pytest.mark.asyncio
async def test_retired_note_leaves_the_procedure_body(env):
    created = NOW - timedelta(days=10)
    n = await env.store.add(pat(tool="bash", key="boom", hits=4, calls=10), "check the path first", now=created)
    await sync_procedure(env.heart, env.store, True)
    assert (await procedure_rows(env))[0].active
    for i in range(20):  # the mistake got no rarer
        await insert_call(
            env.db, env.agent_id, None, i, session_id=None, tool="bash", input_text=f"in-{i}",
            is_error=i < 10, outcome="error" if i < 10 else "ran", error_text="boom" if i < 10 else None,
            started_at=NOW - timedelta(days=1),
        )  # fmt: skip
    settings = SimpleNamespace(
        tool_notes_enabled=True, tool_call_log_enabled=True, background_model="m", agent_id=env.agent_id
    )
    out = await ToolNotesReview(env.db, env.heart, settings, None, clock=lambda: NOW).run_sleep()
    assert out["retired"] == 1
    assert not (await procedure_rows(env))[0].active
    assert n not in [x.id for x in await env.store.active()]
