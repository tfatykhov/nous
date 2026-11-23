"""The default goal prompt: how a goal's work runs, in the harness's words, for the first task of each goal session
(``charter.build_session_task``) and every follow-up turn of its root (``charter.render_goal_block``).

``DEFAULT_GOAL_PROMPT`` is the built-in text. The owner may replace it from the dashboard's Identity screen (``PUT
/goals/prompt``, at most ``MAX_CHARS`` characters) or go back to it (``DELETE /goals/prompt``); the saved text is a
row of ``nous_system.agent_identity`` outside the identity's sections (``IdentityManager.set_goal_prompt``). It is
read at each session start and each follow-up turn, uncached, so a change applies from the next one, in every process.
Each sentence of the built-in text rests on a rule of the harness that ``tests/test_goal_prompt_rules.py`` pins.
"""

from __future__ import annotations

import logging
from typing import Any

from nous.goals.quote import neutralize
from nous.identity.manager import GOAL_PROMPT_MAX_CHARS, IdentityManager, normalize_goal_prompt

logger = logging.getLogger(__name__)

HEADING = "How a goal's work runs"
MAX_CHARS = GOAL_PROMPT_MAX_CHARS
DEFAULT_GOAL_PROMPT = "\n".join(
    [
        "- A goal session is a chain: a task runs, then a follow-up turn reads its result and decides what runs next.",
        "- A task cannot start other tasks or workflows. Only a follow-up turn can: spawn_task for a task, dag_create "
        "for a workflow.",
        "- In a task: when a plan step is shown, do that one step well. If there is no plan yet, put one in your final "
        "result and say which steps do not depend on each other. End with what you did, what you found and what "
        "should run next.",
        "- In a follow-up turn: a plan step is closed by citing one task or one workflow, and each citation closes one "
        "step only. When several open steps do not depend on each other, start one task for each (spawn_task) in this "
        "same turn. They run side by side as far as the task workers allow and the rest wait in the queue; their "
        "results may be read in one follow-up turn or in several. Use a workflow (dag_create) when the parts of one "
        "step can run side by side: it is one piece of evidence and closes one step, so make those parts one step "
        "first (goal_plan). Use a single task for a step that needs the previous result.",
        "- The Limits of a follow-up turn show what is left of spawned work, follow-up turns and tokens, and whether "
        "more work can be started. Aim to close at least one plan step each session, and leave a note for the next "
        "session (goal_note) before the limits run out.",
    ]
)


def effective(stored: str | None) -> tuple[str, bool]:
    """``(text, is_default)``: the text the builders use for ``stored`` (the saved text, or None). Saved text that is
    not blank and at most ``MAX_CHARS`` once normalized is used; anything else gives the built-in text. ``GET
    /goals/prompt`` answers with the same, so the screen never shows a text the builders would not use."""
    if stored is not None:
        text = normalize_goal_prompt(stored)
        if text and len(text) <= MAX_CHARS:
            return text, False
    return DEFAULT_GOAL_PROMPT, True


async def load(database: Any, agent_id: str) -> str:
    """The goal prompt to use now (``effective`` of the saved text). A failed read is logged (its class) and gives
    the built-in text: a session or a follow-up turn never fails on it."""
    try:
        row = await IdentityManager(database, agent_id).get_goal_prompt()
    except Exception as exc:
        logger.warning("The saved goal prompt could not be read (%s); the built-in text is used", type(exc).__name__)
        return DEFAULT_GOAL_PROMPT
    return effective(row.text if row is not None else None)[0]


def section(text: str, level: str, room: int) -> str:
    """``text`` under the heading ``level HEADING`` (``##`` in a task, ``###`` in a follow-up turn's goal block), if
    the whole section is at most ``room`` characters; else the built-in text under it, if that fits; else ``""``.
    ``neutralize`` is applied as it is to a goal's own title and objective: it removes format and control characters
    other than newline and tab and escapes the data tags' names, so the rendered text can differ slightly from the
    text the screen shows."""
    for body in dict.fromkeys([neutralize(text), DEFAULT_GOAL_PROMPT]):
        candidate = f"{level} {HEADING}\n{body}"
        if len(candidate) <= room:
            return candidate
    return ""
