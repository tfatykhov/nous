"""The default goal prompt: how a goal's work runs, in the harness's words, for the first task of each goal session
(``charter.build_session_task``) and every follow-up turn of its root (``charter.render_goal_block``).

``DEFAULT_GOAL_PROMPT`` is the built-in text. The owner may replace it from the dashboard's Identity screen (``PUT
/goals/prompt``, at most ``MAX_CHARS`` characters) or go back to it (``DELETE /goals/prompt``); the saved text is a
row of ``nous_system.agent_identity`` outside the identity's sections (``IdentityManager.set_goal_prompt``). It is
read at each session start and each follow-up turn, uncached, so a change applies from the next one, in every process.
Each sentence of the built-in text rests on a rule of the harness that ``tests/test_goal_prompt_rules.py`` pins.

Every reader tries the same texts in the same order (``rungs``): the text loaded, the built-in text, none. A builder
moves down when the text does not fit (``charter``) and says which it carries (``charter.Rendered``); a reader decides
from that, never from the text it asked for. The first task's start moves down when the censor gate refuses
the task and the pattern of a censor that refuses matches the text (``GoalDriver._gated_task``); a follow-up turn
moves down when such a pattern matches the text (``GoalChain.turn_context``, read only: ``refusing_patterns`` and
``refused``). This is the censor check's keyword match on the text alone: a refusal of the whole task or turn for
anything else (the semantic match, or a pattern that spans other text) is the task's or the turn's own.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from nous.goals.quote import neutralize
from nous.identity.manager import GOAL_PROMPT_MAX_CHARS, IdentityManager, goal_prompt_has_text, normalize_goal_prompt

logger = logging.getLogger(__name__)

HEADING = "How a goal's work runs"
MAX_CHARS = GOAL_PROMPT_MAX_CHARS
REFUSING_ACTIONS = frozenset({"abort", "refuse"})  # the censor tiers that stop a turn or a task
DEFAULT_GOAL_PROMPT = "\n".join(
    [
        "- A goal session is a chain: a task runs, then a follow-up turn reads its result and decides what runs next.",
        "- A task cannot start other tasks or workflows. Only a follow-up turn can: spawn_task for a task, dag_create "
        "for a workflow.",
        "- In a task: when a plan step is shown, do that one step well. If there is no plan yet, put one in your final "
        "result and say which steps do not depend on each other. End with what you did, what you found and what "
        "should run next.",
        "- In a follow-up turn: a plan step is closed by citing a completed task or workflow of this session, and each "
        "citation closes one step only. When several open steps do not depend on each other, start one task for each "
        "(spawn_task) in this same turn. They run side by side as far as the task workers allow and the rest wait in "
        "the queue; their results may be read in one follow-up turn or in several. Use a workflow (dag_create) when "
        "the parts of one step can run side by side: it is one piece of evidence and closes one step, so make those "
        "parts one step first (goal_plan). Use a single task for a step that needs the previous result.",
        "- The Limits of a follow-up turn show what has been used of the limits on spawned work, follow-up turns and "
        "tokens, and whether more work can be started. Aim to close at least one plan step each session, and leave a "
        "note for the next session (goal_note) before the limits run out.",
    ]
)


def effective(stored: str | None) -> tuple[str, bool]:
    """``(text, is_default)``: the text the builders use for ``stored`` (the saved text, or None). Saved text that,
    once normalized, shows something (``goal_prompt_has_text``, the rule a save is held to) and is at most
    ``MAX_CHARS`` is used; anything else gives the built-in text. ``GET /goals/prompt`` answers with the same, so the
    screen never shows a text the builders would not use."""
    if stored is not None:
        text = normalize_goal_prompt(stored)
        if goal_prompt_has_text(text) and len(text) <= MAX_CHARS:
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


def rungs(text: str) -> list[str | None]:
    """The goal prompts a reader tries, in order: ``text``, the built-in text (when ``text`` is another), none."""
    return [*dict.fromkeys([text, DEFAULT_GOAL_PROMPT]), None]


def bodies(text: str) -> list[tuple[str, str]]:
    """What a builder tries, in order, as ``(goal prompt, what goes under the heading)``: ``text`` as it is shown,
    then the built-in text, each only when it shows something (and the built-in text once). ``neutralize`` is applied
    as it is to a goal's own title and objective: it removes format and control characters other than newline and
    tab and escapes the data tags' names, so the rendered text can differ slightly from the text the screen shows."""
    tried: list[tuple[str, str]] = []
    for rung, body in ((text, neutralize(text)), (DEFAULT_GOAL_PROMPT, DEFAULT_GOAL_PROMPT)):
        if goal_prompt_has_text(body) and all(body != seen for _rung, seen in tried):
            tried.append((rung, body))
    return tried


def section(body: str, level: str) -> str:
    """``body`` under the heading ``level HEADING`` (``##`` in a task, ``###`` in a follow-up turn's goal block)."""
    return f"{level} {HEADING}\n{body}"


async def refusing_patterns(heart: Any) -> list[str]:
    """The patterns of the agent's active censors that refuse or abort, read without side effects (no activation is
    counted, no event is written). A failed read is logged and gives none: the reader then keeps its text, and the
    check every task and turn goes through still runs."""
    try:
        censors = await heart.list_censors()
    except Exception as exc:
        logger.warning("The censors could not be read for the goal prompt (%s)", type(exc).__name__)
        return []
    return [censor.trigger_pattern for censor in censors if censor.action in REFUSING_ACTIONS]


def refused(text: str | None, patterns: list[str]) -> bool:
    """Whether one of ``patterns`` matches ``text`` as a builder shows it, the way the censor check's keyword match
    does (a regular expression, any case). None, no text, is never refused."""
    if text is None:
        return False
    shown = neutralize(text)
    for pattern in patterns:
        try:
            if re.search(pattern, shown, re.IGNORECASE):
                return True
        except re.error:
            continue
    return False
