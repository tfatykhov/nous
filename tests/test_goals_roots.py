"""F114 goals: the root of a goal session is an internal continuation chain (the helper and its pins)."""

from __future__ import annotations

import ast
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from f099_support import (
    CHAN,
    CONT,
    env_factory,  # noqa: F401
)

from nous.api.execution_context import ExecutionContext
from nous.brain import intentions
from nous.goals import roots

pytestmark = pytest.mark.postgres_only


def _goal(**over) -> SimpleNamespace:
    values = {"id": uuid.uuid4(), "short_id": 7, "title": "Tidy the notes", "origin_channel": CHAN}
    return SimpleNamespace(**{**values, **over})


async def _root(env, *, subtask_id: uuid.UUID | None = None, session_n: int = 1):
    subtask = await roots.create_session_root(
        env.heart,
        env.settings,
        goal=_goal(),
        session_n=session_n,
        subtask_id=subtask_id or uuid.uuid4(),
        task="Take the next step of the plan.",
    )
    intention = await env.heart.intentions.get_for_source(intentions.SOURCE_SUBTASK, subtask.id)
    return subtask, intention


async def test_a_session_root_is_continue_and_internal_only(env_factory):  # noqa: F811
    # PIN: a goal session relies on resolve_wake_policy giving `continue` for an internal-authority root (its
    # results go back to the continuation, never to the chat) and on prepare_intention making the row
    # internal_only. A change there that stops either fails here, not in a running goal.
    env = await env_factory(**CONT)
    _, intention = await _root(env)
    assert intention.wake_policy == intentions.WAKE_CONTINUE
    assert intention.authority == intentions.AUTHORITY_INTERNAL
    assert intention.origin_kind == intentions.ORIGIN_GOAL == "goal"
    assert intention.depth == 0
    assert intention.root_id == intention.id
    assert intention.deadline is not None
    assert intention.state == intentions.STATE_PENDING
    assert intention.intent == "Goal G-0007, session 1: Tidy the notes"
    assert intention.origin_channel == CHAN


async def test_the_first_subtask_carries_the_lineage_stamp_and_a_real_timeout(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    subtask, intention = await _root(env, session_n=3)
    stamp = subtask.metadata_["intention"]
    assert stamp["authority"] == intentions.AUTHORITY_INTERNAL
    assert stamp["root_id"] == str(intention.id)
    assert subtask.metadata_["goal_session"] == 3
    expected = min(env.settings.subtask_default_timeout, env.settings.subtask_max_timeout)
    assert subtask.timeout_seconds == expected != 120  # not SubtaskManager.create's default
    assert subtask.notify is False
    assert subtask.parent_session_id is None
    assert subtask.model == env.settings.background_model
    ctx = ExecutionContext.for_subtask(subtask, "s")
    assert ctx.kind == "subtask"
    assert ctx.authority == intentions.AUTHORITY_INTERNAL
    assert ctx.root_intention_id == intention.id


async def test_a_session_root_is_refused_with_continuation_off(env_factory):  # noqa: F811
    env = await env_factory(result_inbox_enabled=True, intentions_enabled=True)  # continuation off
    with pytest.raises(roots.GoalsUnavailable):
        await _root(env)
    assert await env.heart.subtasks.list() == []


async def test_a_pre_generated_subtask_id_is_the_subtask_id(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    wanted = uuid.uuid4()
    subtask, intention = await _root(env, subtask_id=wanted)
    assert subtask.id == wanted
    assert intention.source_id == str(wanted)


def _imports_goals(node: ast.AST) -> bool:
    if isinstance(node, ast.Import):
        return any(alias.name == "nous.goals" or alias.name.startswith("nous.goals.") for alias in node.names)
    if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
        if node.module == "nous":
            return any(alias.name == "goals" for alias in node.names)
        return node.module == "nous.goals" or node.module.startswith("nous.goals.")
    return False


def test_only_the_goal_routes_import_the_goal_package():
    # PIN (flag-off parity of PR 1a, widened by PR 2 and PR 1c): outside the package only the goal routes, their
    # mount and main.py's goals block import it. With NOUS_GOALS_ENABLED off the routes answer fixed sentences and
    # read no table (tests/test_goal_routes_guard.py), and main.py imports the chain only inside the flag's block
    # (tests/test_goals_wiring.py), so no existing path runs a goal query, builds a goal prompt or offers a goal tool.
    nous_dir = Path(__file__).resolve().parents[1] / "nous"
    goals_dir = nous_dir / "goals"
    importers = [
        path.relative_to(nous_dir).as_posix()
        for path in sorted(nous_dir.rglob("*.py"))
        if goals_dir not in path.parents
        and any(_imports_goals(node) for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))))
    ]
    assert importers == ["api/goal_routes.py", "api/rest.py", "main.py"]
    assert not (goals_dir / "__main__.py").exists()
