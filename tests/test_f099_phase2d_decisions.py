"""F099 Phase 2d-3: the owner's decisions on proposals, in the store (spec 4.4 items 3 to 6)."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from f099_support import (
    CONT,
    SEND_EMAIL_ARGS,
    ask_with_proposals,
    claim,
    claimed,
    commit_ask,
    env_factory,  # noqa: F401
    inbox_rows,
    intention_of,
    make_child,
    make_root,
    proposal_row,
    record,
    set_intention,
    stage,
    until_a_backend_waits_on_a_lock,
)
from sqlalchemy import select, update

from nous.brain import continuation
from nous.storage.models import Intention, IntentionProposal

pytestmark = pytest.mark.postgres_only  # FOR NO KEY UPDATE, savepoints, = ANY(array)


async def _decide(env, proposal_id, *, approve, actor="owner-test", now=None):
    async with env.db.session() as s:
        out = await continuation.decide_proposal(
            s, env.agent, proposal_id, approve=approve, actor=actor, settings=env.settings, now=now
        )
        await s.commit()
    return out


async def _claim_execution(env, proposal_id):
    async with env.db.session() as s:
        row = await continuation.claim_execution(s, env.agent, proposal_id)
        await s.commit()
    return row


async def _finish(env, proposal_id, **kwargs):
    async with env.db.session() as s:
        out = await continuation.finish_execution(s, env.agent, proposal_id, settings=env.settings, **kwargs)
        await s.commit()
    return out


async def _end_unrunnable(env, proposal_id):
    async with env.db.session() as s:
        out = await continuation.end_unrunnable(s, env.agent, proposal_id, settings=env.settings)
        await s.commit()
    return out


async def _expire_proposals(env, *, now=None):
    async with env.db.session() as s:
        moved = await continuation.expire_proposals(s, env.agent, settings=env.settings, now=now)
        await s.commit()
    return moved


async def _set_proposal(env, proposal_id, **values):
    async with env.db.session() as s:
        await s.execute(update(IntentionProposal).where(IntentionProposal.id == proposal_id).values(**values))
        await s.commit()


async def _results(env, intention_id):
    """The rows only the continuation reads: keyed by the intention alone, from an owner action."""
    return [
        row
        for row in await inbox_rows(env)
        if row.intention_id == intention_id and row.channel is None and row.source_kind == "intention_report"
    ]


async def _state(env, intention):
    return (await intention_of(env, "subtask", intention.source_id)).state


# ---- decide_proposal -----------------------------------------------------------------------------------------


async def test_approving_moves_a_pending_proposal_to_approved_and_wakes_nothing(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    asked = await ask_with_proposals(env)
    (pid,) = asked.ids
    out = await _decide(env, pid, approve=True, actor="telegram:42")
    assert (out.state, out.changed, out.refusal, out.woke_arrival, out.result) == ("approved", True, None, False, None)
    row = await proposal_row(env, pid)
    assert row.state == "approved" and row.decided_by == "telegram:42" and row.decided_at is not None
    assert await _state(env, asked.root) == "awaiting_owner"  # the call has not run: nothing to tell the model yet
    assert await _results(env, asked.root.id) == []


async def test_rejecting_writes_the_outcome_to_the_intention_and_wakes_it(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    asked = await ask_with_proposals(env)
    (pid,) = asked.ids
    out = await _decide(env, pid, approve=False)
    assert (out.state, out.changed, out.refusal, out.woke_arrival) == ("rejected", True, None, True)
    assert await _state(env, asked.root) == "result_ready"
    (row,) = await _results(env, asked.root.id)
    assert (row.msg_type, row.arrival_id, row.delivered_at) == ("INFORM", asked.done.arrival_id, None)
    assert "rejected" in row.body and pid.hex[:8] in row.body and row.title == f"Proposal {pid.hex[:8]}: rejected"


async def test_a_batch_wakes_only_when_every_proposal_is_terminal(env_factory):  # noqa: F811
    """Spec 7: with two proposals in one ask, deciding one does not wake the batch; both rows are then one claim."""
    env = await env_factory(**CONT)
    asked = await ask_with_proposals(env, count=2)
    first, second = asked.ids
    async with env.db.session() as s:
        assert await continuation.arrival_is_terminal(s, env.agent, asked.done.arrival_id) is False
    out = await _decide(env, first, approve=False)
    assert out.woke_arrival is False
    assert await _state(env, asked.root) == "awaiting_owner" and len(await _results(env, asked.root.id)) == 1  # held
    async with env.db.session() as s:
        assert await continuation.arrival_is_terminal(s, env.agent, asked.done.arrival_id) is False
    out = await _decide(env, second, approve=False)
    assert out.woke_arrival is True and await _state(env, asked.root) == "result_ready"
    async with env.db.session() as s:
        assert await continuation.arrival_is_terminal(s, env.agent, asked.done.arrival_id) is True
    got = await claim(env, asked.root.id)
    assert {r.title for r in got.inbox_rows} == {
        f"Proposal {first.hex[:8]}: rejected",
        f"Proposal {second.hex[:8]}: rejected",
    }


async def test_a_decision_is_the_next_result_of_every_intention_of_the_arrival(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    root = await make_root(env)
    child = await make_child(env, root)
    await record(env, root)
    await record(env, child)
    got = await claim(env, root.id)
    assert {i.id for i in got.intentions} == {root.id, child.id}
    pid = await stage(env, got)  # staged under the deepest member
    await commit_ask(env, got)
    out = await _decide(env, pid, approve=False)
    assert out.woke_arrival is True
    for intention in (root, child):
        assert await _state(env, intention) == "result_ready"
        (row,) = await _results(env, intention.id)
        assert "rejected" in row.body
    again = await claim(env, root.id)
    assert {i.id for i in again.intentions} == {root.id, child.id}  # the next claim takes them together


async def test_a_repeated_decision_is_idempotent_and_a_contradictory_one_is_refused(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    approved = (await ask_with_proposals(env)).ids[0]
    assert (await _decide(env, approved, approve=True)).changed is True
    again = await _decide(env, approved, approve=True)
    assert (again.state, again.changed, again.refusal) == ("approved", False, None)  # conflict C3: no error
    flipped = await _decide(env, approved, approve=False)
    assert (flipped.state, flipped.changed, flipped.refusal) == ("approved", False, "not_pending")
    rejected = (await ask_with_proposals(env)).ids[0]
    await _decide(env, rejected, approve=False)
    assert (await _decide(env, rejected, approve=False)).refusal is None
    flipped = await _decide(env, rejected, approve=True)
    assert (flipped.state, flipped.changed, flipped.refusal) == ("rejected", False, "not_pending")


async def test_an_unknown_proposal_is_not_found_and_a_staged_one_is_not_decidable(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    with pytest.raises(continuation.ProposalNotFound):
        await _decide(env, uuid.uuid4(), approve=True)
    _root, got = await claimed(env)
    staged = await stage(env, got)  # never published: the owner cannot have seen it
    out = await _decide(env, staged, approve=True)
    assert (out.state, out.changed, out.refusal) == ("staged", False, "not_pending")
    assert (await proposal_row(env, staged)).state == "staged"


async def test_an_approve_after_the_deadline_is_refused_and_expires_the_proposal(env_factory):  # noqa: F811
    """Carry-over 9: the sweep may not have run; the decision itself refuses, expires, writes the outcome and wakes."""
    env = await env_factory(**CONT)
    asked = await ask_with_proposals(env)
    (pid,) = asked.ids
    await _set_proposal(env, pid, deadline=datetime.now(UTC) - timedelta(minutes=1))
    out = await _decide(env, pid, approve=True)
    assert (out.state, out.changed, out.refusal, out.woke_arrival) == ("expired", True, "expired", True)
    (row,) = await _results(env, asked.root.id)
    assert "expired" in row.body and await _state(env, asked.root) == "result_ready"
    again = await _decide(env, pid, approve=True)
    assert (again.state, again.changed, again.refusal) == ("expired", False, "expired")
    assert len(await _results(env, asked.root.id)) == 1  # written once


@pytest.mark.parametrize(("marker", "state"), [("root_cancelled_at", "cancelled"), ("root_expired_at", "expired")])
async def test_a_decision_on_work_that_ended_is_refused_and_writes_no_row(env_factory, marker, state):  # noqa: F811
    """R8: the root ended, so there is nobody to tell: no INFORM, and above all no raw REPORT."""
    env = await env_factory(**CONT)
    asked = await ask_with_proposals(env)
    (pid,) = asked.ids
    await set_intention(env, asked.root.id, **{marker: datetime.now(UTC)})
    before = len(await inbox_rows(env))
    out = await _decide(env, pid, approve=True)
    assert (out.state, out.changed, out.refusal) == (state, True, "ended")
    assert len(await inbox_rows(env)) == before


# ---- claim_execution and finish_execution --------------------------------------------------------------------


async def test_claim_execution_is_once_and_has_the_root_open_predicate(env_factory):  # noqa: F811
    """Review Focus 2, and the cancel seam of spec 4.4 item 5: the same statement that claims the call requires the
    root to be open, so a cancel that committed first wins."""
    env = await env_factory(**CONT)
    first = await ask_with_proposals(env)
    (pid,) = first.ids
    assert await _claim_execution(env, pid) is None  # pending is not claimable
    await _decide(env, pid, approve=True)
    row = await _claim_execution(env, pid)
    assert row is not None and row.state == "executing" and row.arguments == {**SEND_EMAIL_ARGS, "subject": "Snow 0"}
    assert await _claim_execution(env, pid) is None  # exactly once

    second = await ask_with_proposals(env)
    (pid2,) = second.ids
    await _decide(env, pid2, approve=True)
    await set_intention(env, second.root.id, root_cancelled_at=datetime.now(UTC))  # 2e's cancel committed first
    assert await _claim_execution(env, pid2) is None
    assert (await proposal_row(env, pid2)).state == "approved"  # still approved, never executing
    out = await _end_unrunnable(env, pid2)
    assert (out.state, out.changed, out.refusal) == ("cancelled", True, "ended")
    assert (await _end_unrunnable(env, pid2)).changed is False


async def test_finish_execution_records_the_result_tells_the_intention_and_wakes(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    asked = await ask_with_proposals(env)
    (pid,) = asked.ids
    await _decide(env, pid, approve=True)
    await _claim_execution(env, pid)
    out = await _finish(env, pid, ok=True, result="x" * 5000, ledger_key="proposal:abc:0123")
    assert (out.state, out.changed, out.woke_arrival) == ("executed", True, True)
    row = await proposal_row(env, pid)
    assert row.executed_at is not None and row.ledger_key == "proposal:abc:0123" and row.error is None
    assert len(row.result) <= continuation.PROPOSAL_RESULT_MAX_CHARS and row.result.endswith("[truncated]")
    (inform,) = await _results(env, asked.root.id)
    assert "it ran" in inform.body and await _state(env, asked.root) == "result_ready"
    late = await _finish(env, pid, ok=False, error="a late duplicate")
    assert (late.state, late.changed) == ("executed", False)  # the first finish decided it


async def test_a_failed_call_is_recorded_as_failed_and_reported_to_the_intention(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    asked = await ask_with_proposals(env)
    (pid,) = asked.ids
    await _decide(env, pid, approve=True)
    await _claim_execution(env, pid)
    out = await _finish(env, pid, ok=False, error="SMTP refused the recipient")
    assert (out.state, out.error, out.woke_arrival) == ("failed", "SMTP refused the recipient", True)
    (inform,) = await _results(env, asked.root.id)
    assert "failed" in inform.body and "SMTP refused the recipient" in inform.body


# ---- expire_proposals ----------------------------------------------------------------------------------------


async def test_a_pending_proposal_past_its_deadline_expires_as_a_rejection_and_wakes(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    asked = await ask_with_proposals(env)
    (pid,) = asked.ids
    assert await _expire_proposals(env) == []  # not due
    await _set_proposal(env, pid, deadline=datetime.now(UTC) - timedelta(minutes=1))
    assert await _expire_proposals(env) == [(pid, "expired")]
    assert (await proposal_row(env, pid)).state == "expired"
    (row,) = await _results(env, asked.root.id)
    assert "expired" in row.body and await _state(env, asked.root) == "result_ready"  # terminal: the arrival woke
    assert await _expire_proposals(env) == []  # once


async def test_an_expired_proposal_is_terminal_and_wakes_a_batch_only_with_its_sibling(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    asked = await ask_with_proposals(env, count=2)
    first, second = asked.ids
    await _set_proposal(env, first, deadline=datetime.now(UTC) - timedelta(minutes=1))
    assert await _expire_proposals(env) == [(first, "expired")]
    assert await _state(env, asked.root) == "awaiting_owner"  # the sibling is still pending
    out = await _decide(env, second, approve=False)
    assert out.woke_arrival is True and len(await _results(env, asked.root.id)) == 2


@pytest.mark.parametrize(("marker", "state"), [("root_cancelled_at", "cancelled"), ("root_expired_at", "expired")])
async def test_a_pending_proposal_of_an_ended_root_is_closed_without_a_row(env_factory, marker, state):  # noqa: F811
    env = await env_factory(**CONT)
    asked = await ask_with_proposals(env)
    (pid,) = asked.ids
    await set_intention(env, asked.root.id, **{marker: datetime.now(UTC)})
    before = len(await inbox_rows(env))
    assert await _expire_proposals(env) == [(pid, state)]
    assert len(await inbox_rows(env)) == before


async def test_an_orphan_staged_row_expires_after_two_leases_and_a_fresh_one_does_not(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    _root, got = await claimed(env)
    old = await stage(env, got, arguments={**SEND_EMAIL_ARGS, "subject": "old"})
    fresh = await stage(env, got, arguments={**SEND_EMAIL_ARGS, "subject": "fresh"})
    await _set_proposal(
        env, old, created_at=datetime.now(UTC) - timedelta(hours=1)
    )  # lease 900 s: two leases is 30 min
    assert await _expire_proposals(env) == [(old, "expired")]
    assert (await proposal_row(env, fresh)).state == "staged"


async def test_a_call_left_executing_is_failed_in_doubt_after_the_bound_and_never_rerun(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    asked = await ask_with_proposals(env)
    (pid,) = asked.ids
    await _decide(env, pid, approve=True)
    await _claim_execution(env, pid)
    await _set_proposal(env, pid, updated_at=datetime.now(UTC) - timedelta(minutes=10))  # under max(lease, 2 x timeout)
    assert await _expire_proposals(env) == []
    await _set_proposal(env, pid, updated_at=datetime.now(UTC) - timedelta(hours=1))
    assert await _expire_proposals(env) == [(pid, "failed")]
    row = await proposal_row(env, pid)
    assert row.state == "failed" and row.error == continuation.IN_DOUBT_TEXT
    assert await _claim_execution(env, pid) is None  # nothing can run it again
    (inform,) = await _results(env, asked.root.id)
    assert "NOT run again" in inform.body


async def test_one_failing_proposal_does_not_stop_the_others_expiring(env_factory, monkeypatch, caplog):  # noqa: F811
    env = await env_factory(**CONT)
    first = (await ask_with_proposals(env)).ids[0]
    second = (await ask_with_proposals(env)).ids[0]
    for pid in (first, second):
        await _set_proposal(env, pid, deadline=datetime.now(UTC) - timedelta(minutes=1))
    real, calls = continuation._settle_proposal, []

    async def flaky(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("the inbox is down")
        return await real(*args, **kwargs)

    monkeypatch.setattr(continuation, "_settle_proposal", flaky)
    caplog.set_level(logging.WARNING, logger=continuation.__name__)
    assert len(await _expire_proposals(env)) == 1
    states = sorted([(await proposal_row(env, first)).state, (await proposal_row(env, second)).state])
    assert states == ["expired", "pending"] and "could not expire proposal" in caplog.text


async def test_the_wake_sweep_is_the_backstop_for_a_terminal_proposal_arrival(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    asked = await ask_with_proposals(env, count=2)
    for pid in asked.ids:  # terminal by hand: nothing woke the arrival
        await _set_proposal(env, pid, state="rejected")
    async with env.db.session() as s:
        woken = await continuation.wake_terminal_arrivals(s, env.agent, settings=env.settings)
        await s.commit()
    assert woken == [asked.root.id] and await _state(env, asked.root) == "result_ready"


async def test_an_ask_with_pending_proposals_and_no_question_is_not_woken(env_factory):  # noqa: F811
    """2d-2 review I1: since C4 an ask with proposals writes no QUESTION, so its proposals alone must hold the
    arrival. Before the proposal half of the wake rule, the next sweep woke it with the proposal still pending."""
    env = await env_factory(**CONT)
    asked = await ask_with_proposals(env)
    (pid,) = asked.ids
    assert not [r for r in await inbox_rows(env) if r.msg_type == "QUESTION"]
    arrival_id = asked.done.arrival_id
    async with env.db.session() as s:
        terminal, answered, questions = await continuation._question_state(
            s, env.agent, arrival_id, settings=env.settings, now=datetime.now(UTC)
        )
        assert (terminal, answered, questions) == (False, True, [])
        assert await continuation.arrival_is_terminal(s, env.agent, arrival_id, settings=env.settings) is False
        assert await continuation.wake_terminal_arrivals(s, env.agent, settings=env.settings) == []
        await s.commit()
    assert await _state(env, asked.root) == "awaiting_owner" and (await proposal_row(env, pid)).state == "pending"
    await _set_proposal(env, pid, state="rejected")  # terminal by hand: now the sweep wakes it, once
    async with env.db.session() as s:
        assert await continuation.wake_terminal_arrivals(s, env.agent, settings=env.settings) == [asked.root.id]
        assert await continuation.wake_terminal_arrivals(s, env.agent, settings=env.settings) == []
        await s.commit()


# ---- ids and views -------------------------------------------------------------------------------------------


def test_normalize_id_accepts_only_hex_prefixes_of_a_usable_length():
    assert continuation.normalize_id(" AB12CD34 ") == "ab12cd34"
    assert continuation.normalize_id(str(uuid.UUID(int=255))) == uuid.UUID(int=255).hex
    for bad in ("short", "ab12cd3", "../chat", "ab12cd34/../x", "zz12cd34", "", None, 12345678, "a" * 33):
        assert continuation.normalize_id(bad) is None


async def test_lookups_find_a_unique_prefix_never_a_staged_row_and_refuse_an_ambiguous_one(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    a = (await ask_with_proposals(env)).ids[0]
    _root, got = await claimed(env)
    hidden = await stage(env, got)
    async with env.db.session() as s:
        assert await continuation.find_proposal_id(s, env.agent, a.hex[:8]) == a
        assert await continuation.find_proposal_id(s, env.agent, str(a)) == a  # the dashed form
        assert await continuation.find_proposal_id(s, env.agent, hidden.hex[:8]) is None
        assert await continuation.find_proposal_id(s, env.agent, "not-hex!") is None
    b = (await ask_with_proposals(env)).ids[0]
    # Two ids that share their first 8 characters (fresh tails: the rows outlive the test).
    twin_a, twin_b = (uuid.UUID("abcdef01" + uuid.uuid4().hex[8:]) for _ in range(2))
    await _set_proposal(env, a, id=twin_a)
    await _set_proposal(env, b, id=twin_b)
    async with env.db.session() as s:
        with pytest.raises(continuation.AmbiguousId):
            await continuation.find_proposal_id(s, env.agent, "abcdef01")
        assert await continuation.find_proposal_id(s, env.agent, str(twin_b)) == twin_b


async def test_the_view_and_the_list_are_json_and_hide_staged_proposals(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    asked = await ask_with_proposals(env)
    (pid,) = asked.ids
    _root, got = await claimed(env)
    await stage(env, got)
    async with env.db.session() as s:
        pending = await continuation.list_proposals(s, env.agent, state="pending", limit=20)
        everything = await continuation.list_proposals(s, env.agent, state="all", limit=20)
        openish = await continuation.list_proposals(s, env.agent, state="open", limit=20)
    assert [v["id"] for v in pending] == [str(pid)] == [v["id"] for v in everything] == [v["id"] for v in openish]
    view = pending[0]
    expected = (
        "id short_id root_id intention_id arrival_id tool arguments rationale state deadline decided_at decided_by "
        "result"
    ).split()
    assert set(view) == set(expected)
    assert (view["short_id"], view["tool"], view["state"], view["decided_at"]) == (
        pid.hex[:8],
        "send_email",
        "pending",
        None,
    )
    assert view["arguments"] == {**SEND_EMAIL_ARGS, "subject": "Snow 0"} and view["arrival_id"] == str(
        asked.done.arrival_id
    )
    json.dumps(view)  # serialisable as it is


# ---- the races -----------------------------------------------------------------------------------------------


async def _hold_root(session, root_id):
    await session.execute(select(Intention.id).where(Intention.id == root_id).with_for_update(key_share=True))


async def test_an_approve_that_waited_for_the_expiry_sweep_finds_the_proposal_expired(env_factory):  # noqa: F811
    """Approve racing expiry, the sweep first: both lock the root first, so the approve waits and then reads the
    sweep's result instead of approving a proposal whose outcome was already written."""
    env = await env_factory(**CONT)
    asked = await ask_with_proposals(env)
    (pid,) = asked.ids
    async with env.db.session() as holder:
        await _hold_root(holder, asked.root.id)
        approve = asyncio.create_task(_decide(env, pid, approve=True))
        try:
            await asyncio.wait_for(until_a_backend_waits_on_a_lock(env), timeout=10)
            moved = await continuation.expire_proposals(
                holder, env.agent, settings=env.settings, now=datetime.now(UTC) + timedelta(hours=25)
            )
        finally:
            await holder.commit()
    out = await asyncio.wait_for(approve, timeout=30)
    assert moved == [(pid, "expired")]
    assert (out.state, out.changed, out.refusal) == ("expired", False, "expired")  # the sweep's work, not ours


async def test_an_expiry_sweep_after_an_approve_leaves_the_approved_proposal_alone(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    asked = await ask_with_proposals(env)
    (pid,) = asked.ids
    await _decide(env, pid, approve=True)
    assert await _expire_proposals(env, now=datetime.now(UTC) + timedelta(hours=25)) == []
    assert (await proposal_row(env, pid)).state == "approved"


async def test_two_concurrent_approves_change_the_proposal_once(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    asked = await ask_with_proposals(env)
    (pid,) = asked.ids
    async with env.db.session() as holder:
        await _hold_root(holder, asked.root.id)
        one = asyncio.create_task(_decide(env, pid, approve=True))
        two = asyncio.create_task(_decide(env, pid, approve=True))
        try:
            await asyncio.wait_for(until_a_backend_waits_on_a_lock(env, at_least=2), timeout=10)
        finally:
            await holder.commit()
    outs = [await asyncio.wait_for(task, timeout=30) for task in (one, two)]
    assert sorted(out.changed for out in outs) == [False, True]
    assert {out.state for out in outs} == {"approved"} and all(out.refusal is None for out in outs)
