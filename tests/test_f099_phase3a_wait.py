"""F099 Phase 3a: a card's Approve does not wait for the tool (owner answer 3, contract section 4.2.2)."""

from __future__ import annotations

import asyncio

import pytest
from f099_support import (
    SEND_EMAIL_ARGS,
    SEND_EMAIL_SCHEMA,
    ask_with_proposals,
    env_factory,  # noqa: F401
    proposal_row,
    register_send_email,
    runner_env,  # noqa: F401
)

from nous.handlers.continuation_runner import ContinuationRunner

pytestmark = pytest.mark.postgres_only
STAGED = {**SEND_EMAIL_ARGS, "subject": "Snow 0"}  # what ask_with_proposals stages as proposal 0


def _cont(env) -> ContinuationRunner:
    return ContinuationRunner(
        database=env.db,
        settings=env.settings,
        runner=env.runner,
        heart=env.heart,
        brain=env.brain,
        bus=env.bus,
        dispatcher=env.dispatcher,
    )


async def test_wait_false_returns_approved_at_once_and_tracks_the_execution(runner_env):  # noqa: F811
    env = await runner_env()
    started, release, calls = asyncio.Event(), asyncio.Event(), []

    async def send_email(**kwargs):
        calls.append(kwargs)
        started.set()
        await release.wait()
        return {"content": [{"type": "text", "text": "sent"}]}

    env.dispatcher.register("send_email", send_email, SEND_EMAIL_SCHEMA)
    (pid,) = (await ask_with_proposals(env)).ids
    cont = _cont(env)
    outcome = await asyncio.wait_for(cont.decide_proposal(pid, approve=True, actor="t", wait=False), timeout=5)
    assert (outcome.state, outcome.refusal, outcome.changed) == ("approved", None, True)
    task = cont._executing_ids[pid]  # tracked: stop() waits for it, _resume_approved never starts it twice
    assert task in cont._executing and not task.done()
    await asyncio.wait_for(started.wait(), timeout=10)
    # a repeat while it runs starts nothing (the claim is the at-most-once fence, the dict the second guard)
    again = await cont.decide_proposal(pid, approve=True, actor="t2", wait=False)
    assert (again.changed, again.refusal) == (False, None)
    release.set()
    await asyncio.wait_for(task, timeout=10)
    await asyncio.sleep(0)
    assert (await proposal_row(env, pid)).state == "executed" and calls == [STAGED]
    assert pid not in cont._executing_ids and not cont._executing


async def test_wait_false_on_a_reject_runs_nothing(runner_env):  # noqa: F811
    env = await runner_env()
    sent = register_send_email(env)
    (pid,) = (await ask_with_proposals(env)).ids
    cont = _cont(env)
    outcome = await cont.decide_proposal(pid, approve=False, actor="t", wait=False)
    assert outcome.state == "rejected" and sent == [] and not cont._executing_ids


async def test_decide_proposal_default_still_awaits_the_execution(runner_env):  # noqa: F811  # PIN
    """The REST route and the bot keep waiting. Mutation: make the default ``wait=False`` and this returns
    ``approved`` with no call made yet."""
    env = await runner_env()
    sent = register_send_email(env, text="Message sent.")
    (pid,) = (await ask_with_proposals(env)).ids
    outcome = await _cont(env).decide_proposal(pid, approve=True, actor="t")
    assert (outcome.state, outcome.result) == ("executed", "Message sent.") and sent == [STAGED]
