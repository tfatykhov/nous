"""F099 Phase 2d-2: a staged proposal becomes pending only at the fenced commit; every other path expires it."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from f099_support import (
    CHAN,
    CONT,
    SEND_EMAIL_ARGS,
    ask_with_proposals,
    claimed,
    commit_ask,
    env_factory,  # noqa: F401
    inbox_rows,
    intention_of,
    make_root,
    proposal_row,
    record,
    register_send_email,
    runner_env,  # noqa: F401
    say,
    set_intention,
    stage,
    use,
)
from sqlalchemy import select

from nous.brain import continuation
from nous.brain.continuation import Resolution
from nous.handlers.continuation_publisher import OwnerPublisher
from nous.handlers.continuation_runner import ContinuationRunner
from nous.storage.models import IntentionArrival, IntentionProposal

pytestmark = pytest.mark.postgres_only  # FOR NO KEY UPDATE, savepoints, = ANY(array)


async def _proposal_rows(env):
    return [row for row in await inbox_rows(env) if row.msg_type == "PROPOSAL"]


async def _proposals(env):
    async with env.db.session() as s:
        return list(
            (await s.execute(select(IntentionProposal).where(IntentionProposal.agent_id == env.agent))).scalars()
        )


async def _arrivals(env):
    async with env.db.session() as s:
        return list((await s.execute(select(IntentionArrival).where(IntentionArrival.agent_id == env.agent))).scalars())


# ---- the commit ----------------------------------------------------------------------------------------------


async def test_an_ask_publishes_its_staged_proposals_in_the_fenced_commit(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    asked = await ask_with_proposals(env, count=2)
    done = asked.done
    assert [pid for pid, _tool in done.proposals] == asked.ids
    assert {tool for _pid, tool in done.proposals} == {"send_email"}
    now = datetime.now(UTC)
    for proposal_id in asked.ids:
        row = await proposal_row(env, proposal_id)
        assert row.state == "pending" and row.arrival_id == done.arrival_id
        assert timedelta(hours=23, minutes=55) < row.deadline - now < timedelta(hours=24, minutes=1)  # the 24 h TTL
    pushed = await _proposal_rows(env)
    assert sorted(r.source_id for r in pushed) == sorted(asked.ids)
    for row in pushed:
        # the row's id IS the proposal's id: /approve <short id>, the button and the inbox row all name one thing
        assert (row.source_kind, row.channel, row.proposal_id) == ("intention_report", CHAN, row.source_id)
        assert row.arrival_id == done.arrival_id and row.push_after is not None and row.delivered_at is None
        assert row.title.startswith("Proposal ") and "Call, exactly as it will run" in row.body
        assert "May I email this?" in row.body  # the note rides along as context
    assert not [r for r in await inbox_rows(env) if r.msg_type == "QUESTION"]  # conflict C4
    assert (await intention_of(env, "subtask", asked.root.source_id)).state == "awaiting_owner"
    (arrival,) = await _arrivals(env)
    assert arrival.decision == "ask" and set(arrival.report_ids) == set(asked.ids)


async def test_an_ask_with_no_proposals_still_writes_its_question(env_factory):  # noqa: F811  # PIN (2c behaviour)
    env = await env_factory(**CONT)
    _root, got = await claimed(env)
    done = await commit_ask(env, got, "Shall I book the Friday slot?")
    assert done.proposals == ()
    (question,) = [r for r in await inbox_rows(env) if r.msg_type == "QUESTION"]
    assert "Friday slot" in question.body and not await _proposal_rows(env)


async def test_the_proposals_of_another_claim_are_not_published(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    _root, mine = await claimed(env)
    mine_id = await stage(env, mine)
    _other_root, theirs = await claimed(env)
    theirs_id = await stage(env, theirs)
    done = await commit_ask(env, mine)
    assert [pid for pid, _t in done.proposals] == [mine_id]
    assert (await proposal_row(env, mine_id)).state == "pending"
    assert (await proposal_row(env, theirs_id)).state == "staged"  # its own claim has not committed
    assert [r.source_id for r in await _proposal_rows(env)] == [mine_id]


async def test_a_commit_that_loses_its_fence_publishes_nothing(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    root, got = await claimed(env)
    proposal_id = await stage(env, got)
    # The lease went: the intention is back to result_ready under no token (what a sweep does), without the
    # expiry that release_claim would add, so the staged row is still there when the late commit arrives.
    await set_intention(env, root.id, state="result_ready", claim_token=None, claimed_at=None)
    assert await commit_ask(env, got) is None
    assert (await proposal_row(env, proposal_id)).state == "staged"  # never pending
    assert await _proposal_rows(env) == [] and await _arrivals(env) == []


async def test_a_resolved_decision_other_than_ask_with_staged_proposals_is_refused(env_factory):  # noqa: F811
    """The model chose to report with a proposal staged: it would be a pending proposal nobody was told about."""
    env = await env_factory(**CONT)
    root, got = await claimed(env)
    proposal_id = await stage(env, got)
    with pytest.raises(ValueError, match="must end with ask"):
        async with env.db.session() as s:
            await continuation.commit_arrival(
                s,
                env.agent,
                got,
                resolution=Resolution("report", "All done.", False, 0.5),
                outcome="resolved",
                settings=env.settings,
            )
    assert (await proposal_row(env, proposal_id)).state == "staged"
    assert (await intention_of(env, "subtask", root.source_id)).state == "deciding"  # nothing was written
    assert await _arrivals(env) == [] and await _proposal_rows(env) == []


async def test_an_ask_with_proposals_and_no_owner_channel_is_refused(env_factory):  # noqa: F811
    env = await env_factory(**CONT)  # no default chat
    root, got = await claimed(env, routed=False)  # and the root has no origin channel
    proposal_id = await stage(env, got)
    with pytest.raises(ValueError, match="nowhere to ask"):
        async with env.db.session() as s:
            await continuation.commit_arrival(
                s,
                env.agent,
                got,
                resolution=Resolution("ask", "May I?", True, 0.8),
                outcome="resolved",
                settings=env.settings,
            )
    assert (await proposal_row(env, proposal_id)).state == "staged" and await _arrivals(env) == []


# ---- every other path expires --------------------------------------------------------------------------------


async def _fallback(env, root, got):
    async with env.db.session() as s:
        done = await continuation.commit_arrival(
            s,
            env.agent,
            got,
            resolution=Resolution("report", "I could not decide.", False, 0.3),
            outcome="fallback_report",
            report_text="I could not decide.",
            settings=env.settings,
        )
        await s.commit()
    assert done is not None


async def _retry(env, root, got):
    async with env.db.session() as s:
        assert await continuation.fail_attempt(s, env.agent, got, max_attempts=3, settings=env.settings) == "retry"
        await s.commit()


async def _cap(env, root, got):
    await set_intention(env, root.id, attempts=2)
    async with env.db.session() as s:
        assert (
            await continuation.fail_attempt(s, env.agent, got, max_attempts=3, settings=env.settings)
            == continuation.CLOSE_FAILED_REPORT
        )
        await s.commit()


async def _release(env, root, got):
    async with env.db.session() as s:
        assert await continuation.release_claim(s, env.agent, got) == 1
        await s.commit()


async def _lease(env, root, got):
    await set_intention(env, root.id, claimed_at=datetime.now(UTC) - timedelta(hours=1))
    async with env.db.session() as s:
        released = await continuation.release_stale_claims(
            s, env.agent, lease_s=900, max_attempts=3, settings=env.settings
        )
        await s.commit()
    assert released == [root.id]


async def _ttl(env, root, got):
    """The TTL sweep ends the claim by clearing its token; the late commit would lose its fence (S6)."""
    await set_intention(env, root.id, deadline=datetime.now(UTC) - timedelta(hours=1))
    async with env.db.session() as s:
        assert await continuation.expire_roots(s, env.agent, ttl_hours=72.0, settings=env.settings) == [root.id]
        await s.commit()


@pytest.mark.parametrize("path", [_fallback, _retry, _cap, _release, _lease, _ttl], ids=lambda f: f.__name__.strip("_"))
async def test_every_path_that_does_not_commit_an_ask_expires_the_staged_rows(env_factory, path):  # noqa: F811
    env = await env_factory(**CONT)
    root, got = await claimed(env)
    proposal_id = await stage(env, got)
    await path(env, root, got)
    assert (await proposal_row(env, proposal_id)).state == "expired"
    assert all(row.state != "pending" for row in await _proposals(env))
    assert await _proposal_rows(env) == []


# ---- through the runner --------------------------------------------------------------------------------------


def _cont(env) -> ContinuationRunner:
    env.cont = ContinuationRunner(
        database=env.db,
        settings=env.settings,
        runner=env.runner,
        heart=env.heart,
        brain=env.brain,
        bus=env.bus,
        dispatcher=env.dispatcher,
    )
    return env.cont


def _propose(**over):
    args = {"tool": "send_email", "arguments": SEND_EMAIL_ARGS, "rationale": "The owner wants it.", **over}
    return use("propose_action", **args)


def _ask(note="May I email the report?"):
    return use("resolve_intention", decision="ask", note=note, progress=False, confidence=0.7)


def _http():
    http = MagicMock()
    http.post = AsyncMock(return_value=SimpleNamespace(status_code=200, json=lambda: {"result": {"message_id": 5}}))
    return http


async def test_a_turn_that_staged_and_then_failed_leaves_nothing_approvable(runner_env):  # noqa: F811
    """Review Focus 3: kill the turn after propose_action. No pending row exists and the publisher sends nothing."""
    env = await runner_env(
        [_propose()], RuntimeError("the model is down"), telegram_bot_token="test-token", telegram_chat_id="8080"
    )
    register_send_email(env)
    root = await make_root(env)
    await record(env, root)
    assert await _cont(env).run_arrival(root.id) is None  # a failed attempt
    (row,) = await _proposals(env)
    assert row.state == "expired"
    http = _http()
    publisher = OwnerPublisher(database=env.db, settings=env.settings, http_client=http)
    assert await publisher.push_due() == 0 and http.post.await_count == 0


async def test_a_turn_that_proposes_and_asks_publishes_and_says_so_on_the_bus(runner_env):  # noqa: F811
    env = await runner_env([_propose()], [_ask()])
    register_send_email(env)
    root = await make_root(env)
    await record(env, root)
    done = await _cont(env).run_arrival(root.id)
    (row,) = await _proposals(env)
    assert row.state == "pending" and row.arrival_id == done.arrival_id
    (pending,) = [e for e in env.bus.events if e.type == "intention.proposal_pending"]
    assert pending.data == {
        "proposal_id": str(row.id),
        "root_id": str(root.id),
        "arrival_id": str(done.arrival_id),
        "tool": "send_email",
    }
    assert [e.type for e in env.bus.events].index("intention.arrival_decided") < [e.type for e in env.bus.events].index(
        "intention.proposal_pending"
    )


async def test_a_turn_that_staged_and_never_resolved_falls_back_and_expires(runner_env):  # noqa: F811
    env = await runner_env([_propose()], [say("I proposed the email.")], [say("Still no decision.")])
    register_send_email(env)
    root = await make_root(env)
    await record(env, root)
    done = await _cont(env).run_arrival(root.id)
    assert done is not None and done.proposals == ()
    (row,) = await _proposals(env)
    assert row.state == "expired"
    (report,) = [r for r in await inbox_rows(env) if r.source_kind == "intention_report"]
    assert report.msg_type == "REPORT" and not await _proposal_rows(env)


async def test_an_ask_with_proposals_and_nowhere_to_ask_falls_back_and_expires(runner_env):  # noqa: F811
    env = await runner_env([_propose()], [_ask()])
    register_send_email(env)
    root = await make_root(env, routed=False)  # no origin channel, and the environment has no default chat
    await record(env, root)
    done = await _cont(env).run_arrival(root.id)
    assert done is not None and done.proposals == ()
    (row,) = await _proposals(env)
    assert row.state == "expired" and not await _proposal_rows(env)
