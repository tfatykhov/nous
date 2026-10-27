"""F099: the stall budget does not count an ask, and owner input shown to an arrival resets the run.

An arrival whose batch (``inbox_ids``) carried an owner-written inbox row (an answer, or the outcome of a proposal
the owner decided) resets the trailing run of no-progress arrivals. ``budget_turns``, ``budget_tokens`` and the
depth and spawn limits are untouched, so a loop of asks stays bounded by them.
"""

from __future__ import annotations

import uuid

import pytest
from f099_dashboard_support import put_inbox, put_intention
from f099_support import (
    CONT,
    add_arrival,
    claim,
    env_factory,  # noqa: F401
    make_root,
    record,
)
from sqlalchemy import func, select, update

from nous.brain import continuation
from nous.storage.models import IntentionArrival

pytestmark = pytest.mark.postgres_only  # = ANY(array), FILTER aggregates

ANSWER = "owner-answer:telegram:1"
DECISION = "owner-decision:rest"


async def _limits(env, root_id):
    async with env.db.session() as s:
        return await continuation.root_limits(s, env.agent, root_id, settings=env.settings)


async def _owner_row(env, root, *, correlation_id, source_kind="intention_report", msg_type="INFORM", **over):
    return (
        await put_inbox(env, root, source_kind=source_kind, msg_type=msg_type, correlation_id=correlation_id, **over)
    ).id


async def _arrive(env, root, specs: list[tuple]) -> list[uuid.UUID]:
    """One arrival per spec ``(progress, decision, inbox_ids)``, numbered after the root's last one."""
    async with env.db.session() as s:
        last = (
            await s.execute(
                select(func.max(IntentionArrival.n)).where(
                    IntentionArrival.agent_id == env.agent, IntentionArrival.root_id == root.id
                )
            )
        ).scalar_one() or 0
    ids = []
    for offset, (progress, decision, inbox_ids) in enumerate(specs, start=1):
        ids.append(
            await add_arrival(env, root.id, last + offset, progress=progress, decision=decision, inbox_ids=inbox_ids)
        )
    return ids


F = (False, "continue", None)
ASK = (False, "ask", None)


def _with(spec: tuple, inbox_ids: list[uuid.UUID]) -> tuple:
    return (spec[0], spec[1], inbox_ids)


async def test_an_ask_arrival_is_not_a_stall(env_factory):  # noqa: F811
    env = await env_factory(**CONT)  # continuation_stall_limit 2, the default
    root = await put_intention(env)
    await _arrive(env, root, [F, ASK, F])
    limits = await _limits(env, root.id)
    assert (limits.stalls, limits.escalate) == (2, "budget_stall")
    other = await put_intention(env)
    await _arrive(env, other, [F, ASK, ASK, ASK])  # asks neither count nor take a place in the window
    limits = await _limits(env, other.id)
    assert (limits.stalls, limits.escalate) == (1, None)


async def test_asks_do_not_break_a_real_stall_run(env_factory):  # noqa: F811
    """An ask the verifier credited (it wrote memory or spawned) used to break the run; now it is skipped, so three
    real no-progress turns around it still stop the chain."""
    env = await env_factory(**CONT, continuation_stall_limit=3)
    root = await put_intention(env)
    credited_ask = (True, "ask", None)
    await _arrive(env, root, [F, credited_ask, F, credited_ask, F])
    limits = await _limits(env, root.id)
    assert (limits.stalls, limits.escalate) == (3, "budget_stall")


async def test_three_plain_no_progress_turns_still_stop_the_chain(env_factory):  # noqa: F811
    env = await env_factory(**CONT, continuation_stall_limit=3)
    root = await make_root(env)
    await record(env, root)
    got = await claim(env, root.id)
    await _arrive(env, root, [F, F])
    async with env.db.session() as s:
        assert await continuation.gate(s, env.agent, got, settings=env.settings, plan_outcome_of=_no_plan) is None
    await _arrive(env, root, [F])
    async with env.db.session() as s:
        gate = await continuation.gate(s, env.agent, got, settings=env.settings, plan_outcome_of=_no_plan)
    assert gate == "budget_stall"


async def _no_plan(_decision_id):
    return None


@pytest.mark.parametrize("marker", [ANSWER, DECISION])
async def test_owner_input_in_the_batch_resets_the_run(env_factory, marker):  # noqa: F811
    env = await env_factory(**CONT)  # limit 2
    root = await put_intention(env)
    owner = await _owner_row(env, root, correlation_id=marker)
    await _arrive(env, root, [F, F, _with(F, [owner])])
    limits = await _limits(env, root.id)
    assert (limits.stalls, limits.escalate) == (0, None)
    await _arrive(env, root, [F])
    limits = await _limits(env, root.id)
    assert (limits.stalls, limits.escalate) == (1, None)
    await _arrive(env, root, [F])
    limits = await _limits(env, root.id)
    assert (limits.stalls, limits.escalate) == (2, "budget_stall")


async def test_an_ask_that_carried_owner_input_still_resets(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    root = await put_intention(env)
    owner = await _owner_row(env, root, correlation_id=ANSWER)
    await _arrive(env, root, [F, F, _with(ASK, [owner])])
    limits = await _limits(env, root.id)
    assert (limits.stalls, limits.escalate) == (0, None)


async def test_an_answer_resets_the_arrival_that_was_shown_it_not_the_one_that_asked(env_factory):  # noqa: F811
    """An answer to the question of arrival 2 is shown in arrival 3's batch: the reset is keyed on the batch
    (``inbox_ids``), never on the row's ``arrival_id``."""
    env = await env_factory(**CONT)  # limit 2
    root = await put_intention(env)
    _first, asked = await _arrive(env, root, [F, F])
    answer = await _owner_row(env, root, correlation_id=ANSWER, arrival_id=asked)
    await _arrive(env, root, [_with(F, [answer])])
    assert (await _limits(env, root.id)).stalls == 0

    env = await env_factory(**CONT, continuation_stall_limit=3)
    root = await put_intention(env)
    first, _second, latest = await _arrive(env, root, [F, F, F])
    shown_early = await _owner_row(env, root, correlation_id=ANSWER, arrival_id=latest)
    async with env.db.session() as s:
        await s.execute(update(IntentionArrival).where(IntentionArrival.id == first).values(inbox_ids=[shown_early]))
        await s.commit()
    limits = await _limits(env, root.id)
    assert (limits.stalls, limits.escalate) == (2, None)  # the two newer arrivals count; the oldest one resets


@pytest.mark.parametrize(
    "look_alike",
    [
        {"source_kind": "dag", "correlation_id": ANSWER},
        {"source_kind": "subtask", "correlation_id": DECISION},
        {"msg_type": "REPORT", "correlation_id": ANSWER},
        {"msg_type": "QUESTION", "correlation_id": DECISION},
        {"correlation_id": "not-owner-answer:x"},
        {"correlation_id": "owner-answers"},
    ],
)
async def test_look_alike_rows_never_reset(env_factory, look_alike):  # noqa: F811
    env = await env_factory(**CONT, continuation_stall_limit=3)
    root = await put_intention(env)
    row = await _owner_row(env, root, **look_alike)
    await _arrive(env, root, [F, F, _with(F, [row])])
    limits = await _limits(env, root.id)
    assert (limits.stalls, limits.escalate) == (3, "budget_stall")


async def test_an_owner_row_not_shown_to_any_arrival_never_resets(env_factory):  # noqa: F811
    env = await env_factory(**CONT, continuation_stall_limit=3)
    root = await put_intention(env)
    *_, latest = await _arrive(env, root, [F, F, F])
    await _owner_row(env, root, correlation_id=ANSWER, arrival_id=latest)  # keyed to the arrival, not in its batch
    limits = await _limits(env, root.id)
    assert (limits.stalls, limits.escalate) == (3, "budget_stall")


async def test_a_system_ended_proposal_outcome_does_not_reset(env_factory):  # noqa: F811
    """An expired proposal's outcome row carries no marker, so an expiry loop still stalls."""
    env = await env_factory(**CONT, continuation_stall_limit=3)
    root = await put_intention(env)
    expired = await _owner_row(env, root, correlation_id=None)
    await _arrive(env, root, [F, F, _with(F, [expired])])
    limits = await _limits(env, root.id)
    assert (limits.stalls, limits.escalate) == (3, "budget_stall")


async def test_another_agents_owner_row_never_resets(env_factory):  # noqa: F811
    env = await env_factory(**CONT, continuation_stall_limit=3)
    other = await env_factory(**CONT)
    root = await put_intention(env)
    foreign = await _owner_row(other, await put_intention(other), correlation_id=ANSWER)
    await _arrive(env, root, [F, F, _with(F, [foreign])])
    assert (await _limits(env, root.id)).escalate == "budget_stall"
