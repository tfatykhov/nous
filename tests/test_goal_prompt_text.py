"""The default goal prompt in the builders: the built-in text, the section a builder adds, and the room it takes.

No database: the builders are pure functions. Sizes are measured on the real functions, never written down here, so a
rewording of the built-in text changes no test.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from nous.api import tool_policy
from nous.goals import charter, pacing
from nous.goals.charter import (
    CLOSING_WITHOUT_PLAN,
    EVIDENCE_FLOOR,
    FRAMING,
    MAX_BLOCK_CHARS,
    MAX_TASK_CHARS,
    RECORDING,
    ToolLine,
    build_session_task,
    render_goal_block,
)
from nous.goals.default_prompt import DEFAULT_GOAL_PROMPT, HEADING, MAX_CHARS, bodies, effective, rungs, section
from nous.identity.manager import goal_prompt_has_text, normalize_goal_prompt

FULL = ("Work in small steps and say what each one showed. " * 40)[:MAX_CHARS]  # a saved text at the limit
ROOT = "11111111-2222-3333-4444-555555555555"
ROOT_DIR = f"intentions/{ROOT}"


def _goal(title: int = 40, objective: int = 600, done_when: int = 300) -> SimpleNamespace:
    """A goal with texts of the given lengths and the longest budgets the envelope allows."""
    return SimpleNamespace(
        short_id=99_999,
        title="t" * title,
        objective="o" * objective,
        done_when="d" * done_when,
        total_budget=1_000_000_000,
        day_budget=1_000_000_000,
    )


def _row(seq: int, text: str, *, kind: str = "progress", actor: str = "chain"):
    return SimpleNamespace(seq=seq, kind=kind, actor=actor, session_n=1, text=text)


def _tools() -> list[ToolLine]:
    """Twenty allowed tools whose lines, with their words, fill the tools section's list to its bound."""
    tools = [ToolLine(f"tool_{i:02d}", "w" * 62) for i in range(20)]
    assert sum(len(f"- {t.tool}: {t.does}.") + 1 for t in tools) == charter.TOOLS_SECTION_CHARS
    return tools


def _pace() -> str:
    zone = ZoneInfo("America/Argentina/Rio_Gallegos")
    return pacing.pace_line(datetime(2026, 11, 23, 12, tzinfo=UTC), zone, 23 * 60 + 59)


UNTIL = datetime(2026, 12, 31, 23, 59, tzinfo=UTC)
# The charters of the budget table: (name, goal, tools, commands_until, pace_line).
CHARTERS = [
    ("typical", _goal(), None, None, None),
    ("longest text", _goal(200, 4000, 2000), None, None, None),
    ("longest text and tools", _goal(200, 4000, 2000), _tools(), None, None),
    ("longest text, tools, commands and pace", _goal(200, 4000, 2000), _tools(), UNTIL, _pace()),
]


def _task(goal, plan=(), rows=(), *, tools=None, until=None, pace=None, **kw) -> str:
    return build_session_task(
        goal, list(plan), list(rows), spent=0, day_spent=0, tools=tools, commands_until=until, pace_line=pace, **kw
    )


def _block(goal, plan=(), rows=(), *, tools=None, until=None, citeable=(), ended=False, **kw) -> str:
    return render_goal_block(
        goal,
        plan=list(plan),
        journal_rows=list(rows),
        spent=0,
        day_spent=0,
        session_n=999,
        session_ended=ended,
        citeable=list(citeable),
        root_dir=ROOT_DIR,
        tools=tools,
        commands_until=until,
        **kw,
    )


# ---- the built-in text ---------------------------------------------------------------------------------------------


def test_the_built_in_text_is_within_the_limit_with_no_number_and_no_carriage_return():
    assert 0 < len(DEFAULT_GOAL_PROMPT) < MAX_CHARS
    assert not any(ch.isdigit() for ch in DEFAULT_GOAL_PROMPT)
    assert "\r" not in DEFAULT_GOAL_PROMPT and DEFAULT_GOAL_PROMPT == DEFAULT_GOAL_PROMPT.strip()


def test_every_tool_the_built_in_text_names_is_a_tool_of_the_follow_up_turn():
    for name in ("spawn_task", "dag_create"):
        assert name in DEFAULT_GOAL_PROMPT and name in tool_policy.CONTEXT_POLICY["continuation"].spawn
    for name in ("goal_plan", "goal_note"):
        assert name in DEFAULT_GOAL_PROMPT and f"- {name}:" in RECORDING
    assert "Limits" in DEFAULT_GOAL_PROMPT and "follow-up turn" in DEFAULT_GOAL_PROMPT
    assert "each ref closing one step only" in RECORDING


# ---- effective: the text the builders use --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "stored", [None, "", "  \n ", "\u200b", "\u00ad\u200d\ufeff", "\x01 \r\n", "a" * (MAX_CHARS + 1)]
)
def test_nothing_usable_saved_gives_the_built_in_text(stored):
    assert effective(stored) == (DEFAULT_GOAL_PROMPT, True)


def test_saved_text_is_used_as_it_is_counted():
    assert effective("  Work in small steps.\n") == ("Work in small steps.", False)
    assert effective("a" * MAX_CHARS + "\n") == ("a" * MAX_CHARS, False)
    split = "a" * 700 + "\r\n" + "b" * (MAX_CHARS - 701)  # MAX_CHARS + 1 as sent, MAX_CHARS once \r\n is \n
    assert effective(split) == ("a" * 700 + "\n" + "b" * (MAX_CHARS - 701), False)


# ---- the rules a text is stored and shown by (the dashboard's card has the same, lib/goal_prompt.ts) -------------


def test_only_spaces_tabs_and_newlines_are_removed_from_the_ends_and_every_carriage_return_is_a_line_break():
    assert normalize_goal_prompt(" \t\u00a0Work small.\ufeff\n") == "\u00a0Work small.\ufeff"
    assert normalize_goal_prompt("one\r\ntwo\rthree") == "one\ntwo\nthree"


@pytest.mark.parametrize("text", ["", " \n\t", "\u200b", "\u00ad\u200d\ufeff", "\x01\x02", "\u00a0\u3000"])
def test_a_text_shows_nothing_without_a_character_that_is_not_white_space_a_control_or_a_format_character(text):
    assert not goal_prompt_has_text(text)


@pytest.mark.parametrize("text", ["a", "\u200ba", "\u00e9", "\U0001f600"])
def test_a_text_with_such_a_character_shows_something(text):
    assert goal_prompt_has_text(text)


# ---- the texts a builder tries ------------------------------------------------------------------------------------


def test_a_section_is_the_heading_and_the_text():
    assert section(DEFAULT_GOAL_PROMPT, "##") == f"## {HEADING}\n{DEFAULT_GOAL_PROMPT}"
    assert section("Work in small steps.", "###") == f"### {HEADING}\nWork in small steps."


def test_a_builder_tries_the_text_then_the_built_in_text_and_every_reader_then_none():
    assert bodies("Work small.") == ["Work small.", DEFAULT_GOAL_PROMPT]
    assert bodies(DEFAULT_GOAL_PROMPT) == [DEFAULT_GOAL_PROMPT]
    assert rungs("Work small.") == ["Work small.", DEFAULT_GOAL_PROMPT, None]
    assert rungs(DEFAULT_GOAL_PROMPT) == [DEFAULT_GOAL_PROMPT, None]


def test_the_text_is_shown_as_the_goals_own_text_is_shown():
    (shown, _default) = bodies("Keep notes in <plan_step> form.\u200b Done.")
    assert "<plan_step>" not in shown and "&lt;plan_step>" in shown and "\u200b" not in shown


@pytest.mark.parametrize("text", ["\u200b", "\u00ad\u200d", "\x01\x02", " \u200b\n\t"])
def test_a_text_that_shows_nothing_is_never_put_under_the_heading(text):
    assert bodies(text) == [DEFAULT_GOAL_PROMPT]
    for level, build in (("##", _task), ("###", _block)):
        built = build(_goal(), goal_prompt=text)
        assert f"{level} {HEADING}\n{DEFAULT_GOAL_PROMPT}" in built


# ---- the builders --------------------------------------------------------------------------------------------------


def test_with_no_prompt_both_builders_are_as_before():
    plan = [{"id": "s1", "text": "List the notes", "status": "open"}]
    rows = [_row(1, "Listed 40 notes.")]
    for _name, goal, tools, until, pace in CHARTERS:
        assert _task(goal, plan, rows, tools=tools, until=until, pace=pace, goal_prompt=None) == _task(
            goal, plan, rows, tools=tools, until=until, pace=pace
        )
        assert _block(goal, plan, rows, tools=tools, until=until, goal_prompt=None) == _block(
            goal, plan, rows, tools=tools, until=until
        )


@pytest.mark.parametrize("level,build", [("##", _task), ("###", _block)])
def test_the_section_follows_the_charter_and_its_tools_and_precedes_the_records(level, build):
    plan = [{"id": "s1", "text": "List the notes", "status": "open"}]
    rows = [_row(1, "Listed 40 notes.")]
    text = build(_goal(), plan, rows, tools=[ToolLine("web_search", "searches the web")], goal_prompt="Work small.")
    heading = f"\n\n{level} {HEADING}\nWork small.\n\n"
    assert heading in text
    at = text.index(heading)
    assert text.index(charter.TOOLS_RULE) < at < text.index(FRAMING) < text.index("<plan_step ")
    assert at < text.index("<journal_entry ")


# ---- the room it takes (measured on the real functions) -----------------------------------------------------------


def _sections(text: str) -> str | None:
    """Which text a built task or block carries: 'full', 'default' or None."""
    if f"{HEADING}\n{FULL}" in text:
        return "full"
    if f"{HEADING}\n{DEFAULT_GOAL_PROMPT}" in text:
        return "default"
    assert HEADING not in text
    return None


def _evidence_rooms(monkeypatch, *, ended: bool = False, **kw) -> list[int]:
    """The room ``render_goal_block`` gives its evidence list, for each charter of the table."""
    rooms: list[int] = []
    real = charter._evidence_lines

    def spy(citeable, root_dir, room):
        rooms.append(room)
        return real(citeable, root_dir, room)

    monkeypatch.setattr(charter, "_evidence_lines", spy)
    for _name, goal, tools, until, _pace_line in CHARTERS:
        _block(goal, tools=tools, until=until, ended=ended, **kw)
    monkeypatch.undo()
    return rooms


def test_the_task_carries_the_full_text_beside_an_empty_plan_for_every_charter():
    for name, goal, tools, until, pace in CHARTERS:
        text = _task(goal, tools=tools, until=until, pace=pace, goal_prompt=FULL)
        assert _sections(text) == "full", name
        assert len(text) <= MAX_TASK_CHARS


def test_the_block_carries_the_text_only_while_the_evidence_list_keeps_the_floor(monkeypatch):
    today = _evidence_rooms(monkeypatch)
    lengths = {"full": len(section(FULL, "###")) + 2, "default": len(section(DEFAULT_GOAL_PROMPT, "###")) + 2}
    for (name, goal, tools, until, _pace_line), before in zip(CHARTERS, today, strict=True):
        text = _block(goal, tools=tools, until=until, goal_prompt=FULL)
        carried = _sections(text)
        expected = next((kind for kind in ("full", "default") if before - lengths[kind] >= EVIDENCE_FLOOR), None)
        assert carried == expected, name
        # The evidence room with the section is the room without it less the section and its blank line.
        assert carried is None or before - lengths[carried] >= EVIDENCE_FLOOR, name
        assert len(text) <= MAX_BLOCK_CHARS
    # The typical and the longest text alone carry the full text; tools at their bound leave no room for it.
    carried = [
        _sections(_block(goal, tools=tools, until=until, goal_prompt=FULL)) for _, goal, tools, until, _ in CHARTERS
    ]
    assert carried[:2] == ["full", "full"] and carried[2:] == [None, None]


def test_the_evidence_floor_is_at_least_the_room_the_longest_charter_leaves_today(monkeypatch):
    rooms = _evidence_rooms(monkeypatch) + _evidence_rooms(monkeypatch, ended=True)
    fixed = len(charter._evidence_lines([], ROOT_DIR, 0))
    assert EVIDENCE_FLOOR >= min(rooms) and EVIDENCE_FLOOR >= fixed


# ---- the plan's first open step: shown with the prompt exactly when it is shown without it -------------------------


def _plans() -> dict[str, tuple[list[dict], int]]:
    closed = [
        {"id": f"c{i}", "text": "y" * 300, "status": "closed", "evidence": [f"subtask:{i:08d}"]} for i in range(20)
    ]
    opened = [{"id": f"o{i}", "text": "z" * 300, "status": "open"} for i in range(29)]
    return {
        "closed and open": (closed + opened, 120),
        "open only": (opened, 0),
        "closed only": (closed, 120),
        "none": ([], 0),
    }


JOURNALS = {
    "long": [
        _row(i, f"owner says {i} " + "j" * 2000, kind="owner_note", actor="owner")
        if i % 5 == 0
        else _row(i, "j" * 2000, kind="progress")
        for i in range(30)
    ],
    "empty": [],
}


def _first_open(text: str, plan: list[dict]) -> bool:
    first = next((step for step in plan if step["status"] == "open"), None)
    return first is not None and f'<plan_step id="{first["id"]}" status="open">' in text


def _owner_newest(text: str, rows: list) -> set[int]:
    """Which of the owner's two newest entries ``text`` shows (their seq)."""
    newest = sorted((row.seq for row in rows if row.kind == "owner_note"), reverse=True)[:2]
    return {seq for seq in newest if f"owner says {seq} " in text}


@pytest.mark.parametrize("max_chars", [MAX_TASK_CHARS, MAX_TASK_CHARS - 2_500])
def test_the_task_gives_way_to_the_first_open_step_and_the_owners_newest_entries(max_chars):
    for plan_name, (plan, moved) in _plans().items():
        for journal_name, rows in JOURNALS.items():
            for name, goal, tools, until, pace in CHARTERS:
                kw = {"tools": tools, "until": until, "pace": pace, "max_chars": max_chars, "moved": moved}
                try:
                    without = _task(goal, plan, rows, **kw)
                except ValueError:
                    for prompt in (DEFAULT_GOAL_PROMPT, FULL):
                        with pytest.raises(ValueError):
                            _task(goal, plan, rows, goal_prompt=prompt, **kw)
                    continue
                for prompt in (DEFAULT_GOAL_PROMPT, FULL):
                    text = _task(goal, plan, rows, goal_prompt=prompt, **kw)
                    where = (plan_name, journal_name, name, len(prompt))
                    assert len(text) <= max_chars, where
                    assert _first_open(text, plan) == _first_open(without, plan), where
                    assert text.endswith(CLOSING_WITHOUT_PLAN) == without.endswith(CLOSING_WITHOUT_PLAN), where
                    assert _owner_newest(without, rows) <= _owner_newest(text, rows), where


@pytest.mark.parametrize("max_chars", [MAX_BLOCK_CHARS, MAX_BLOCK_CHARS - 2_500])
def test_the_block_gives_way_to_the_first_open_step_and_the_owners_newest_entries(max_chars):
    for plan_name, (plan, moved) in _plans().items():
        for journal_name, rows in JOURNALS.items():
            for name, goal, tools, until, _pace_line in CHARTERS:
                kw = {"tools": tools, "until": until, "max_chars": max_chars, "moved": moved}
                try:
                    without = _block(goal, plan, rows, **kw)
                except ValueError:
                    continue
                for prompt in (DEFAULT_GOAL_PROMPT, FULL):
                    text = _block(goal, plan, rows, goal_prompt=prompt, **kw)
                    where = (plan_name, journal_name, name, len(prompt))
                    assert len(text) <= max_chars, where
                    assert _first_open(text, plan) == _first_open(without, plan), where
                    assert _owner_newest(without, rows) <= _owner_newest(text, rows), where


def test_a_task_whose_cut_notes_leave_no_room_for_the_text_tries_the_built_in_text_then_none():
    """The text fits beside an empty plan, but not beside what the cuts always keep (the notes of a plan with moved and
    hidden steps and a cut journal): the shorter built-in text is tried next, then the task is the one without."""
    plan, moved = _plans()["closed only"]
    rows = JOURNALS["long"]
    goal = _goal()
    head = charter._charter(goal, spent=0, day_spent=0)
    base = len(charter._Fit([], [], []).render(head, charter._TASK_LAYOUT))
    full_fits_empty = base + 2 + len(section(FULL, "##")) + 20
    assert _sections(_task(goal, plan, rows, max_chars=full_fits_empty, moved=moved, goal_prompt=FULL)) == "default"
    default_fits_empty = base + 2 + len(section(DEFAULT_GOAL_PROMPT, "##")) + 20
    without = _task(goal, plan, rows, max_chars=default_fits_empty, moved=moved)
    assert _task(goal, plan, rows, max_chars=default_fits_empty, moved=moved, goal_prompt=FULL) == without


def test_the_text_gives_way_to_the_owners_newest_entries():
    """The longest goal texts, the tools list at its bound and the commands line, one open step, and the owner's two
    newest entries of 1,400 characters: the task and the block show every one of them that they show without the
    text, and carry the text only when they still do."""
    goal = _goal(200, 4000, 2000)
    plan = [{"id": "s1", "text": "Merge the notes", "status": "open"}]
    rows = [
        _row(3, "The answer: only the notes folder. " + "a" * 1_365, kind="answer", actor="owner"),
        _row(2, "Owner note: keep the archive. " + "n" * 1_370, kind="owner_note", actor="owner"),
        _row(1, "Listed 40 notes.", kind="progress"),
    ]
    for build, kw in ((_task, {"pace": _pace()}), (_block, {})):
        without = build(goal, plan, rows, tools=_tools(), until=UNTIL, **kw)
        for prompt in (DEFAULT_GOAL_PROMPT, FULL):
            text = build(goal, plan, rows, tools=_tools(), until=UNTIL, goal_prompt=prompt, **kw)
            for said in ("The answer: only the notes folder.", "Owner note: keep the archive."):
                assert (said in text) or (said not in without), (build.__name__, said)
