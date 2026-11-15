"""F114 goals: the root of a goal session is an internal continuation chain (the helper and its pins)."""

from __future__ import annotations

import uuid
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
