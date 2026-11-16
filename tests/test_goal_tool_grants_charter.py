"""Per-goal tool grants in what a goal's session reads: the first task of a session and the goal block of a decision
turn tell it which tools the owner allowed the goal and whether each is available now, from the grant rows and the
harness's own words only, within the texts' bounds; with the grants off both are as before."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from f099_support import env_factory  # noqa: F401
from goals_support import reset_goal_report_channel  # noqa: F401
from sqlalchemy import select
from test_goal_tool_grants_boot import GRANTS
from test_goals_charter import _goal, _row
from test_goals_driver import _active, _started, _world

from nous.api import tool_classes
from nous.api.tool_classes import McpIdentity
from nous.autonomy import tool_guard
from nous.autonomy.tool_guard import ToolGuard
from nous.goals import charter
from nous.goals.chain import GoalChain
from nous.goals.charter import (
    MAX_BLOCK_CHARS,
    MAX_TASK_CHARS,
    TOOLS_HEADING,
    TOOLS_RULE,
    TOOLS_SECTION_CHARS,
    ToolLine,
    build_session_task,
    render_goal_block,
    tools_section,
)
from nous.goals.grants import GRANT_FACTS, GoalToolGrants
from nous.storage.models import Subtask

NOTES = McpIdentity(uuid.uuid4(), "a1" * 32, "search", server_name="notes", server_host="notes.example.com")
ON = "mcp__notes__search"
EMAIL_WORDS = GRANT_FACTS["send_email"].does


def _block(**over):
    values = {
        "plan": [],
        "journal_rows": [],
        "spent": 1000,
        "day_spent": 200,
        "session_n": 2,
        "session_ended": False,
        "citeable": [],
    }
    goal = over.pop("goal", _goal())
    values.update(over)
    return render_goal_block(goal, **values)


# ---- the section ------------------------------------------------------------------------------------------------


def test_no_grant_says_none_besides_the_standard_tools():
    assert tools_section([]) == f"{TOOLS_HEADING} none.\n{TOOLS_RULE}"
    assert TOOLS_HEADING == "Besides its standard tools, this goal may use without asking:"
    assert TOOLS_RULE == (
        "Other tools that act outside Nous (sending, acting on web pages, connected servers) are not available to "
        "this goal's turns or tasks. If the goal cannot be finished without one, say so in a progress note; do not "
        "ask the owner to approve a step these turns cannot carry out."
    )
    # The section never says the standard tools are unavailable, nor that a goal with no grant has no tools.
    assert "not listed" not in tools_section([]) and "standard tools" in tools_section([])


def test_two_grants_are_listed_with_what_they_do_and_the_rule():
    lines = [
        ToolLine("send_email", EMAIL_WORDS),
        ToolLine(ON, "a tool of the MCP server notes"),
    ]
    assert tools_section(lines) == "\n".join(
        [
            "Besides its standard tools, this goal may use without asking:",
            f"- {ON}: a tool of the MCP server notes.",
            f"- send_email: {EMAIL_WORDS}.",
            TOOLS_RULE,
        ]
    )


def test_a_tool_not_available_now_says_so_and_comes_after_the_available_ones():
    text = tools_section([ToolLine("a_tool", None), ToolLine("send_email", "send email")])
    assert text.splitlines()[1:3] == ["- send_email: send email.", "- a_tool: not available now."]


def test_twenty_grants_stay_within_the_bound_cutting_words_first():
    long = [
        ToolLine(f"mcp__server{n:02d}__tool_with_a_long_name_{n:02d}", "a tool of the MCP server " + "s" * 24)
        for n in range(20)
    ]
    text = tools_section(long)
    listed = text.split("\n", 1)[1].rsplit("\n", 1)[0]
    assert len(listed) <= TOOLS_SECTION_CHARS
    assert "a tool of the MCP server" not in text and all(line.tool in text for line in long)  # names kept
    assert "more allowed tools not listed" not in text  # every name fits once the words go
    huge = [ToolLine(f"mcp__{'x' * 90}__{n:02d}{'y' * 100}", "does") for n in range(20)]
    text = tools_section(huge)
    listed = text.split("\n", 1)[1].rsplit("\n", 1)[0]
    assert len(listed) <= TOOLS_SECTION_CHARS and "more allowed tools not listed." in text
    assert text.endswith(TOOLS_RULE)


def test_with_twenty_grants_the_largest_task_and_block_keep_their_bounds():
    goal = _goal(title="T" * 200, objective="O" * 4000, done_when="D" * 2000)
    plan = [{"id": f"s{i}", "text": "p" * 300, "status": "open"} for i in range(20)]
    rows = [_row(i, "n" * 1490, kind="owner_note", actor="owner") for i in range(10)]
    lines = [ToolLine(f"mcp__{'x' * 90}__{n:02d}{'y' * 100}", "does") for n in range(20)]
    task = build_session_task(goal, plan, rows, spent=0, day_spent=0, tools=lines)
    block = _block(goal=goal, plan=plan, journal_rows=rows, tools=lines)
    assert len(task) <= MAX_TASK_CHARS and len(block) <= MAX_BLOCK_CHARS
    assert TOOLS_RULE in task and TOOLS_RULE in block and "O" * 4000 in task


def test_with_no_tools_given_the_task_and_the_block_are_as_before():
    plan = [{"id": "s1", "text": "List the notes", "status": "open"}]
    rows = [_row(1, "Listed 40 notes.")]
    task = build_session_task(_goal(), plan, rows, spent=10, day_spent=5)
    assert TOOLS_HEADING not in task and TOOLS_RULE not in task
    assert task == charter.build_session_task(_goal(), plan, rows, spent=10, day_spent=5, tools=None)
    assert TOOLS_HEADING not in _block() and _block() == _block(tools=None)


def test_the_section_goes_after_the_charter_in_both():
    task = build_session_task(_goal(), [], [], spent=0, day_spent=0, tools=[])
    assert task.index("Budget:") < task.index(TOOLS_HEADING) < task.index(charter.FRAMING)
    block = _block(tools=[])
    assert block.index("Budget:") < block.index(TOOLS_HEADING) < block.index(charter.FRAMING)


# ---- what a real session is told --------------------------------------------------------------------------------


@pytest.fixture
def mcp_registry(monkeypatch):
    monkeypatch.setattr(tool_classes, "_MCP_TOOLS", {ON: True})
    monkeypatch.setattr(tool_classes, "_MCP_IDENTITIES", {ON: NOTES})


async def _granted_session(env_factory, *, tools=("send_email",)):  # noqa: F811
    w = await _world(env_factory, **GRANTS)
    goal = await _active(w)
    for tool in tools:
        await w.store.grant_tool(goal.id, tool, NOTES if tool == ON else None, actor="owner", proof="owner")
    tool_guard.install(ToolGuard())
    view = GoalToolGrants(w.db, w.env.settings)
    assert await view.refresh()
    w.driver.tool_grants = view
    root, session = await _started(w, goal)
    chain = GoalChain(w.db, w.env.settings, w.store, tool_grants=view)
    return w, goal, root, session, chain, view


@pytest.mark.postgres_only
async def test_a_session_with_two_grants_is_told_both_in_its_first_task_and_its_decision_turns(
    env_factory,  # noqa: F811
    mcp_registry,
):
    w, goal, root, session, chain, _view = await _granted_session(env_factory, tools=("send_email", ON))
    async with w.db.session() as s:
        subtask = (await s.execute(select(Subtask).where(Subtask.id == session.first_subtask_id))).scalar_one()
    for text in (subtask.task, (await chain.turn_context(root.id)).block):
        assert "Besides its standard tools, this goal may use without asking:\n" in text
        assert f"- {ON}: a tool of the MCP server notes." in text
        assert f"- send_email: {EMAIL_WORDS}." in text
        assert TOOLS_RULE in text
        assert "notes.example.com" not in text and "search_" not in text  # no server text, only names


@pytest.mark.postgres_only
async def test_a_paused_goal_or_an_ended_session_is_told_none_is_available(env_factory, mcp_registry):  # noqa: F811
    w, goal, root, session, chain, view = await _granted_session(env_factory)
    await w.store.pause(goal.id, actor="owner")
    await view.refresh_goal(goal.id)
    block = (await chain.turn_context(root.id)).block
    assert "- send_email: not available now." in block and EMAIL_WORDS not in block
    await w.store.resume(goal.id, actor="owner")
    await view.refresh()
    assert "- send_email: send email" in (await chain.turn_context(root.id)).block
    async with w.db.session() as s:
        from sqlalchemy import text

        await s.execute(
            text("UPDATE brain.goal_sessions SET ended_at = now(), outcome = 'no_step' WHERE id = :i"),
            {"i": session.id},
        )
        await s.commit()
    assert "- send_email: not available now." in (await chain.turn_context(root.id)).block


@pytest.mark.postgres_only
async def test_an_mcp_grant_whose_server_changed_is_told_not_available(env_factory, mcp_registry):  # noqa: F811
    w, goal, root, session, chain, view = await _granted_session(env_factory, tools=(ON,))
    assert f"- {ON}: a tool of the MCP server notes." in (await chain.turn_context(root.id)).block
    tool_classes._MCP_IDENTITIES[ON] = McpIdentity(uuid.uuid4(), NOTES.url_sha256, "search")
    assert f"- {ON}: not available now." in (await chain.turn_context(root.id)).block


@pytest.mark.postgres_only
async def test_a_removed_grant_is_not_listed_and_a_goal_without_grants_is_told_so(env_factory, mcp_registry):  # noqa: F811
    w, goal, root, session, chain, view = await _granted_session(env_factory)
    await w.store.revoke_tool(goal.id, "send_email", actor="owner", proof="owner")
    block = (await chain.turn_context(root.id)).block
    section = block[block.index(TOOLS_HEADING) : block.index(TOOLS_RULE)]
    assert section == f"{TOOLS_HEADING} none.\n"


@pytest.mark.postgres_only
async def test_with_no_view_the_session_reads_as_before(env_factory):  # noqa: F811
    w = await _world(env_factory, **GRANTS)
    goal = await _active(w)
    await w.store.grant_tool(goal.id, "send_email", None, actor="owner", proof="owner")
    root, session = await _started(w, goal)  # the driver has no view: the flag is off in main.py
    async with w.db.session() as s:
        subtask = (await s.execute(select(Subtask).where(Subtask.id == session.first_subtask_id))).scalar_one()
    block = (await GoalChain(w.db, w.env.settings, w.store).turn_context(root.id)).block
    for text in (subtask.task, block):
        assert TOOLS_HEADING not in text and TOOLS_RULE not in text


def test_the_section_reads_only_harness_words_and_names():
    from nous.goals.grants import session_tool_lines

    grant = SimpleNamespace(tool=ON, server_name="no<tes> ignore all", server_id=NOTES.server_id)
    [line] = session_tool_lines([grant], lambda _grant: True)
    assert line == ToolLine(ON, "a tool of the MCP server notesignoreall")


@pytest.mark.postgres_only
async def test_a_tool_switched_off_for_background_work_is_told_not_available(env_factory, mcp_registry):  # noqa: F811
    w, goal, root, session, chain, view = await _granted_session(env_factory)
    tool_guard.install(ToolGuard(denied=frozenset({"send_email"})))
    assert "- send_email: not available now." in (await chain.turn_context(root.id)).block


@pytest.mark.postgres_only
async def test_a_first_task_after_the_mcp_server_changed_is_told_not_available(env_factory, mcp_registry):  # noqa: F811
    w = await _world(env_factory, **GRANTS)
    goal = await _active(w)
    await w.store.grant_tool(goal.id, ON, NOTES, actor="owner", proof="owner")
    tool_classes._MCP_IDENTITIES[ON] = McpIdentity(uuid.uuid4(), NOTES.url_sha256, "search")
    tool_guard.install(ToolGuard())
    view = GoalToolGrants(w.db, w.env.settings)
    assert await view.refresh()
    w.driver.tool_grants = view
    _root, session = await _started(w, goal)
    async with w.db.session() as s:
        subtask = (await s.execute(select(Subtask).where(Subtask.id == session.first_subtask_id))).scalar_one()
    assert f"- {ON}: not available now." in subtask.task
