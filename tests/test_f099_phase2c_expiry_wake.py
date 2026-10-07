"""F099 Phase 2c-1: the TTL sweep (spec 4.6) and the wake rule of an ask (spec 4.4 item 6, 4.5.6)."""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from f099_support import (
    CHAN,
    CONT,
    claim,
    env_factory,  # noqa: F401
    inbox_rows,
    intention_of,
    make_child,
    make_root,
    record,
    set_intention,
    until_a_backend_waits_on_a_lock,
)
from sqlalchemy import select, update

from nous.brain import continuation, intentions
from nous.brain.continuation import Resolution
from nous.storage.models import Intention, ResultInbox

pytestmark = pytest.mark.postgres_only  # = ANY(array column), FOR NO KEY UPDATE, savepoints


async def _expire(env, *, limit=10, ttl=72.0, now=None):
    async with env.db.session() as s:
        expired = await continuation.expire_roots(
            s, env.agent, ttl_hours=ttl, settings=env.settings, limit=limit, now=now
        )
        await s.commit()
    return expired


async def _owner_rows(env):
    return [r for r in await inbox_rows(env) if r.source_kind == "intention_report"]


async def _past(env, intention_id, hours=1):
    await set_intention(env, intention_id, deadline=datetime.now(UTC) - timedelta(hours=hours))


def _high_then_low_ids(monkeypatch):
    """The next root sorts AFTER its child: ``prepare_intention`` draws a high id, then a low one. Fresh ids
    on every run (the rows outlive the test), so only the order is fixed."""
    high, low = uuid.uuid4().hex, uuid.uuid4().hex
    ids = iter([uuid.UUID("ff" + high[2:]), uuid.UUID("00" + low[2:])])
    monkeypatch.setattr(intentions, "uuid", SimpleNamespace(uuid4=lambda: next(ids)))


async def test_a_root_past_its_deadline_expires_with_a_report_of_what_exists(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    root = await make_root(env)
    child = await make_child(env, root)
    await record(env, root, body="40 cm overnight")
    await _past(env, root.id)
    assert await _expire(env) == [root.id]
    fresh_root = await intention_of(env, "subtask", root.source_id)
    fresh_child = await intention_of(env, "subtask", child.source_id)
    assert (fresh_root.state, fresh_root.close_reason, fresh_root.root_expired_at is not None) == (
        "expired",
        "expired",
        True,
    )
    assert (fresh_child.state, fresh_child.close_reason) == ("expired", "expired")
    (row,) = await inbox_rows(env, uuid.UUID(root.source_id))
    assert row.delivered_at is not None and row.delivered_session_id == f"intent-{root.id}"
    (report,) = await _owner_rows(env)
    assert (report.msg_type, report.channel, report.intention_id) == ("REPORT", CHAN, root.id)
    assert "did not finish" in report.body and "40 cm overnight" in report.body and report.push_after is not None


async def test_a_root_without_a_deadline_expires_without_a_report_when_it_has_nothing_to_show(env_factory):  # noqa: F811
    """Flip-time (Review Focus 3): a Phase 1 root has a NULL deadline. Judged by created_at + ttl; with
    nothing unread it closes without a report, so the first sweep after the flag flips cannot flood the owner."""
    env = await env_factory(**CONT)
    old = await make_root(env)
    young = await make_root(env)
    await set_intention(env, old.id, created_at=datetime.now(UTC) - timedelta(hours=100))
    assert await _expire(env) == [old.id]  # the young one is inside its TTL
    assert (await intention_of(env, "subtask", old.source_id)).state == "expired"
    assert (await intention_of(env, "subtask", young.source_id)).state == "pending"
    assert await _owner_rows(env) == []


async def test_a_root_without_a_deadline_still_reports_a_result_nobody_saw(env_factory):  # noqa: F811
    """A Phase 1 root past its TTL whose result is already in the inbox: the rows are stamped delivered by the
    expiry, so they must be reported, never swallowed."""
    env = await env_factory(**CONT)
    root = await make_root(env)
    await record(env, root, body="40 cm overnight")
    await set_intention(env, root.id, created_at=datetime.now(UTC) - timedelta(hours=100))
    assert await _expire(env) == [root.id]
    (report,) = await _owner_rows(env)
    assert "40 cm overnight" in report.body and report.channel == CHAN
    (row,) = await inbox_rows(env, uuid.UUID(root.source_id))
    assert row.delivered_at is not None


async def test_a_result_committed_while_the_expiry_closes_the_lineage_is_not_orphaned(env_factory):  # noqa: F811
    """Lock, then read. A second transaction has recorded a result for a child (it holds the child's row, not
    committed) when the expiry starts. The expiry's UPDATE waits for it, then closes the child, and must read
    the unread rows AFTER that: the new row is stamped and reported, not left on an expired intention."""
    env = await env_factory(**CONT)
    root = await make_root(env)
    child = await make_child(env, root)
    await _past(env, root.id)
    async with env.db.session() as holder:
        await continuation.record_result(
            holder,
            env.agent,
            intention_id=child.id,
            source_kind="subtask",
            source_id=uuid.UUID(child.source_id),
            msg_type="INFORM",
            title="Snow",
            body="late but committed",
            settings=env.settings,
        )
        sweep = asyncio.create_task(_expire(env))
        try:
            await asyncio.wait_for(until_a_backend_waits_on_a_lock(env), timeout=10)
        finally:
            await holder.commit()
    assert await asyncio.wait_for(sweep, timeout=30) == [root.id]
    (row,) = await inbox_rows(env, uuid.UUID(child.source_id))
    assert row.delivered_at is not None  # not orphaned
    # In the expiry's own report: a row it orphaned would be picked up by the stranded-row settle at the end of
    # the same sweep, as a second report, and hide the read-before-lock order from the line above.
    (report,) = await _owner_rows(env)
    assert "did not finish" in report.body and "late but committed" in report.body


async def test_a_commit_and_an_expiry_that_meet_on_a_low_id_child_do_not_deadlock(env_factory, monkeypatch, caplog):  # noqa: F811
    """The lock order (root first, everywhere). The root's id sorts AFTER its child's, so a commit that locked
    its claimed rows in id order and then the root would hold the child while the expiry holds the root and
    wants the child: a deadlock. Both must finish, whichever is granted the root first: the expiry wins and the
    commit's fence is lost, or the commit wins and the expiry finds the root resolved and leaves it alone."""
    caplog.set_level(logging.WARNING, logger=continuation.__name__)
    _high_then_low_ids(monkeypatch)
    env = await env_factory(**CONT)
    root = await make_root(env)  # the high id
    child = await make_child(env, root)  # the low id
    assert root.id > child.id
    await record(env, root)
    await record(env, child)
    got = await claim(env, root.id)
    assert {i.id for i in got.intentions} == {root.id, child.id}
    await _past(env, root.id)

    async def commit():
        async with env.db.session() as s:
            done = await continuation.commit_arrival(
                s,
                env.agent,
                got,
                resolution=Resolution("drop", "Done.", False, 0.9),
                outcome="resolved",
                settings=env.settings,
            )
            await s.commit()
        return done

    async with env.db.session() as holder:
        await holder.execute(
            select(Intention.id).where(Intention.id == root.id).with_for_update(key_share=True)
        )  # the root is busy: whoever wants it first waits
        sweep = asyncio.create_task(_expire(env))
        await asyncio.wait_for(until_a_backend_waits_on_a_lock(env), timeout=10)
        late = asyncio.create_task(commit())
        try:
            await asyncio.wait_for(until_a_backend_waits_on_a_lock(env, at_least=2), timeout=10)
        finally:
            await holder.commit()
    expired = await asyncio.wait_for(sweep, timeout=30)
    done = await asyncio.wait_for(late, timeout=30)  # a deadlock error on the commit's side raises here
    # The order the two waiters are granted the root in is Postgres's, not the test's: either outcome, never both.
    assert (expired, done is None) in (([root.id], True), ([], False))
    # On the expiry's side a deadlock is caught per root and would read as "the commit won": the log says which.
    assert not [r for r in caplog.records if "could not expire root" in r.getMessage()]


@pytest.mark.parametrize("child_policy", [None, "remember"])
async def test_an_expiry_that_waited_for_a_turn_that_resolved_the_root_writes_nothing(
    env_factory,  # noqa: F811
    caplog,
    child_policy,
):
    """Review Important 1. The sweep read the root as due while its turn was deciding, then waited for the root
    while the turn committed a drop. Under the lock the lineage has no open continue or report intention: no
    marker, no "did not finish" report for a root that finished. Re-review new issue 1: a pending ``remember``
    child (it outlives its parent's turn by design) is open but never made the root due, so it is left alone.
    The holder IS the committing turn, so no lock-grant order is assumed."""
    caplog.set_level(logging.WARNING, logger=continuation.__name__)
    env = await env_factory(**CONT)
    root = await make_root(env)
    child = await make_child(env, root) if child_policy else None
    if child is not None:
        await set_intention(env, child.id, wake_policy=child_policy)
    await record(env, root)
    got = await claim(env, root.id)
    await _past(env, root.id)
    async with env.db.session() as holder:
        await holder.execute(select(Intention.id).where(Intention.id == root.id).with_for_update(key_share=True))
        sweep = asyncio.create_task(_expire(env))
        try:
            await asyncio.wait_for(until_a_backend_waits_on_a_lock(env), timeout=10)  # it read the root as due
            done = await continuation.commit_arrival(
                holder,
                env.agent,
                got,
                resolution=Resolution("drop", "Done.", False, 0.9),
                outcome="resolved",
                settings=env.settings,
            )
        finally:
            await holder.commit()
    assert done is not None
    assert await asyncio.wait_for(sweep, timeout=30) == []
    fresh = await intention_of(env, "subtask", root.source_id)
    assert (fresh.state, fresh.close_reason, fresh.root_expired_at) == ("closed", "resolved", None)
    if child is not None:
        assert (await intention_of(env, "subtask", child.source_id)).state == "pending"
    assert await _owner_rows(env) == []
    # An expiry that raised is caught per root and would also return []: the log says it did not.
    assert not [r for r in caplog.records if "could not expire root" in r.getMessage()]


async def _pause_the_expiry_after_its_first_root(monkeypatch):
    """The expiry expires its first root and stops before the second, holding the first root's lock (its
    transaction is open). Returns ``(paused, resume)``."""
    real, paused, resume = continuation._expire_root, asyncio.Event(), asyncio.Event()
    seen: list = []

    async def expire_one(session, agent_id, root_id, **kwargs):
        if seen:
            paused.set()
            await resume.wait()
        seen.append(root_id)
        return await real(session, agent_id, root_id, **kwargs)

    monkeypatch.setattr(continuation, "_expire_root", expire_one)
    return paused, resume


async def _release_sweep(env):
    async with env.db.session() as s:
        released = await continuation.release_stale_claims(
            s, env.agent, lease_s=900, max_attempts=3, settings=env.settings
        )
        await s.commit()
    return released


@pytest.mark.parametrize("other", ["wake", "release"])
async def test_two_sweeps_take_two_roots_in_one_order(env_factory, monkeypatch, other):  # noqa: F811
    """Review Important 2: one cross-root order, (root.created_at, root.id), in every multi-root sweep. Root A
    is older but has the higher id, and B's question was asked first, so ordering by root id (the lease sweep's
    old order) or by when the arrival was decided (the wake's old order) puts B first. The expiry holds A and
    pauses; the other sweep then takes A first and waits, instead of holding B while the expiry wants it."""
    _high_then_low_ids(monkeypatch)
    env = await env_factory(**CONT)
    a = await make_root(env)  # older, the high id
    b = await make_root(env)  # younger, the low id
    assert a.id > b.id
    for root in (b, a):  # B's arrival first
        await record(env, root)
        got = await claim(env, root.id)
        if other == "wake":
            done = await _commit(env, got, Resolution("ask", "Shall I book it?", True, 0.8))
            await _age_question(env, done.arrival_id)
        else:
            await set_intention(env, root.id, claimed_at=datetime.now(UTC) - timedelta(seconds=1000))
        await _past(env, root.id)
    paused, resume = await _pause_the_expiry_after_its_first_root(monkeypatch)
    sweep = asyncio.create_task(_expire(env))
    await asyncio.wait_for(paused.wait(), timeout=10)  # A is expired and locked; B is not asked for yet
    rival = asyncio.create_task(_wake(env) if other == "wake" else _release_sweep(env))
    try:
        await asyncio.wait_for(until_a_backend_waits_on_a_lock(env), timeout=10)  # the rival waits for a root
    finally:
        resume.set()
    assert await asyncio.wait_for(sweep, timeout=30) == [a.id, b.id]
    assert await asyncio.wait_for(rival, timeout=30) == []  # both lineages were expired before it got them


async def test_expire_roots_takes_at_most_limit_roots(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    roots = [await make_root(env) for _ in range(3)]
    for root in roots:
        await _past(env, root.id)
    assert len(await _expire(env, limit=2)) == 2
    assert len(await _expire(env, limit=2)) == 1  # the next sweep takes the rest


async def test_only_roots_with_a_continue_or_report_intention_expire(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    none_root = await make_root(env, policy="none")
    container = await make_root(env, policy="remember")
    await set_intention(env, container.id, wake_policy="container")
    cancelled = await make_root(env)
    for root in (none_root, container, cancelled):
        await _past(env, root.id)
    await set_intention(env, cancelled.id, root_cancelled_at=datetime.now(UTC))
    assert await _expire(env) == []  # a container, a none root and a cancelled root are left alone


async def test_a_root_inside_its_deadline_is_left_alone(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    root = await make_root(env)
    await set_intention(env, root.id, deadline=datetime.now(UTC) + timedelta(hours=1))
    assert await _expire(env) == []


async def test_a_live_claim_loses_its_fence_when_its_root_expires(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    root = await make_root(env)
    await record(env, root)
    got = await claim(env, root.id)
    await _past(env, root.id)
    assert await _expire(env) == [root.id]
    late = await _commit(env, got, Resolution("report", "Late.", True, 0.9))
    assert late is None
    assert len(await _owner_rows(env)) == 1  # the expiry's report, nothing from the late commit


async def test_a_late_result_of_an_expired_root_still_reaches_the_owner(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    root = await make_root(env)
    await _past(env, root.id)
    await _expire(env)
    recorded = await record(env, root, body="it finished after all")
    assert recorded.reported is True  # the root is closed: record_result writes a raw REPORT
    assert any("it finished after all" in r.body for r in await _owner_rows(env))


async def test_an_expiry_leaves_a_row_chat_will_deliver_alone(env_factory):  # noqa: F811
    """Flip-time: a Phase 1 result routed to chat (F098: keyed by its channel and session) belongs to chat. The
    expiry stamps only the rows keyed by an intention alone, so chat still delivers this one and the owner is
    not told twice."""
    env = await env_factory(**CONT)
    root = await make_root(env)
    await set_intention(env, root.id, created_at=datetime.now(UTC) - timedelta(hours=100))
    async with env.db.session() as s:
        await continuation.insert_inbox_row(
            s,
            env.agent,
            source_kind="subtask",
            source_id=uuid.UUID(root.source_id),
            msg_type="INFORM",
            title="Snow report",
            body="40 cm overnight",
            channel=CHAN,
            session_id="S1",
            intention_id=root.id,
        )
        await s.commit()
    assert await _expire(env) == [root.id]
    (row,) = await inbox_rows(env, uuid.UUID(root.source_id))
    assert row.delivered_at is None  # chat's to deliver
    assert await _owner_rows(env) == []


@pytest.mark.parametrize("reason", ["cancelled", "expired"])
async def test_a_row_held_on_an_intention_a_gate_arrival_closed_is_settled_by_the_sweep(env_factory, reason, caplog):  # noqa: F811
    """Lead note (2c1-4). A row that lands after the claim read its rows, and before the root's marker, is held.
    The gate arrival then closes its intention, so nothing can claim the row any more. The sweep stamps it, once,
    with a WARNING (review Minor 2: the settle is a backstop, so its firing is worth seeing in the log), and, by
    the unified late-result rule (2e), reports it raw when the root EXPIRED and says nothing when it was
    CANCELLED. An expired root's gate arrival escalates too, so its own rows are reported as well."""
    env = await env_factory(**CONT)
    root = await make_root(env)
    await record(env, root)
    got = await claim(env, root.id)
    await record(env, root, generation=1, body="landed while the gate ran")  # held: the intention is deciding
    await set_intention(env, root.id, **{f"root_{reason}_at": datetime.now(UTC)})
    resolution, report_text = continuation.gate_inputs(reason, got)
    async with env.db.session() as s:
        done = await continuation.commit_arrival(
            s,
            env.agent,
            got,
            resolution=resolution,
            outcome="resolved",
            gate_reason=reason,
            report_text=report_text,
            settings=env.settings,
        )
        await s.commit()
    assert done is not None and done.next_states == {root.id: reason}

    async def held():
        return [r for r in await inbox_rows(env, uuid.UUID(root.source_id)) if r.source_generation == 1]

    (row,) = await held()
    assert row.delivered_at is None  # stranded on a closed intention

    def settle_warnings():
        return [
            r
            for r in caplog.records
            if r.levelno == logging.WARNING and r.name == continuation.__name__ and "held on closed" in r.getMessage()
        ]

    with caplog.at_level(logging.WARNING, logger=continuation.__name__):
        assert await _expire(env) == []  # the root is closed: nothing expires, but the stranded row is settled
        (warning,) = settle_warnings()
        assert "settled 1 result(s)" in warning.getMessage()
        (row,) = await held()
        silent = continuation.SILENT_SESSION_ID if reason == "cancelled" else f"intent-{root.id}"
        assert row.delivered_at is not None and row.delivered_session_id == silent
        reported = [r for r in await _owner_rows(env) if "landed while the gate ran" in r.body]
        if reason == "cancelled":
            assert reported == [] and await _owner_rows(env) == []
        else:
            (report,) = reported
            assert (report.msg_type, report.channel, report.intention_id) == ("REPORT", CHAN, root.id)
        owner_rows = len(await _owner_rows(env))
        assert await _expire(env) == [] and len(await _owner_rows(env)) == owner_rows  # settled once
        assert len(settle_warnings()) == 1  # and a sweep that settles nothing says nothing


# ---- the wake rule -----------------------------------------------------------------------------------------


async def _commit(env, got, resolution):
    async with env.db.session() as s:
        done = await continuation.commit_arrival(
            s, env.agent, got, resolution=resolution, outcome="resolved", settings=env.settings
        )
        await s.commit()
    return done


async def _ask(env, intention, *, root_id=None, question="Shall I book the Friday slot?"):
    """Record a result for ``intention``, claim its root, and commit an ``ask``: the claimed intention is
    ``awaiting_owner``. Returns the commit."""
    await record(env, intention)
    got = await claim(env, root_id or intention.id)
    return await _commit(env, got, Resolution("ask", question, True, 0.8))


async def _answer(env, arrival_id, intention, text="Yes, book it."):
    """What 2d's record_answer writes: an INFORM row of the arrival, as the next result of the intention."""
    async with env.db.session() as s:
        recorded = await continuation.record_result(
            s,
            env.agent,
            intention_id=intention.id,
            source_kind=continuation.SOURCE_INTENTION_REPORT,
            source_id=uuid.uuid4(),
            msg_type="INFORM",
            title="Owner's answer",
            correlation_id=f"{continuation.ANSWER_CORRELATION_PREFIX}telegram:42",  # as record_answer writes it
            body=text,
            arrival_id=arrival_id,
            settings=env.settings,
        )
        await s.commit()
    return recorded


async def _terminal(env, arrival_id, *, with_settings=True):
    async with env.db.session() as s:
        return await continuation.arrival_is_terminal(
            s, env.agent, arrival_id, settings=env.settings if with_settings else None
        )


async def _wake(env):
    async with env.db.session() as s:
        woken = await continuation.wake_terminal_arrivals(s, env.agent, settings=env.settings)
        await s.commit()
    return woken


async def _age_question(env, arrival_id, hours=25):
    async with env.db.session() as s:
        await s.execute(
            update(ResultInbox)
            .where(ResultInbox.arrival_id == arrival_id, ResultInbox.msg_type == "QUESTION")
            .values(created_at=datetime.now(UTC) - timedelta(hours=hours), push_after=None)
        )
        await s.commit()


async def test_an_unanswered_question_is_not_terminal_and_an_answered_one_is(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    root = await make_root(env)
    done = await _ask(env, root)
    assert await _terminal(env, done.arrival_id) is False
    recorded = await _answer(env, done.arrival_id, root)
    assert recorded.inserted and recorded.state_after == "awaiting_owner"  # held: the answer does not move the chain
    assert await _terminal(env, done.arrival_id) is True


async def test_an_expired_question_is_terminal_only_with_the_ttl_to_judge_it_by(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    root = await make_root(env)
    done = await _ask(env, root)
    async with env.db.session() as s:
        await s.execute(
            update(ResultInbox)
            .where(ResultInbox.arrival_id == done.arrival_id, ResultInbox.msg_type == "QUESTION")
            .values(created_at=datetime.now(UTC) - timedelta(hours=25), push_after=None)
        )
        await s.commit()
    assert await _terminal(env, done.arrival_id, with_settings=False) is False
    assert await _terminal(env, done.arrival_id) is True  # intention_proposal_ttl_hours = 24


async def test_an_arrival_without_a_question_is_terminal(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    root = await make_root(env)
    await record(env, root)
    got = await claim(env, root.id)
    done = await _commit(env, got, Resolution("drop", "Done.", False, 0.9))
    assert await _terminal(env, done.arrival_id) is True


async def test_wake_arrival_moves_the_awaiting_intentions_and_only_those(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    root = await make_root(env)
    done = await _ask(env, root)
    async with env.db.session() as s:
        woken = await continuation.wake_arrival(s, env.agent, done.arrival_id)
        await s.commit()
        again = await continuation.wake_arrival(s, env.agent, done.arrival_id)
    assert woken == [root.id] and again == []  # the second call finds nothing awaiting
    fresh = await intention_of(env, "subtask", root.source_id)
    assert fresh.state == "result_ready" and fresh.result_at is not None


async def test_the_answer_joins_the_next_claim_as_one_batch(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    root = await make_root(env)
    done = await _ask(env, root)
    await record(env, root, generation=1, body="a result that arrived meanwhile")  # held
    await _answer(env, done.arrival_id, root, "Yes, book it.")
    assert await _wake(env) == [root.id]
    got = await claim(env, root.id)
    assert {r.body for r in got.inbox_rows} == {"a result that arrived meanwhile", "Yes, book it."}


async def test_nothing_wakes_before_the_question_is_terminal(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    root = await make_root(env)
    await _ask(env, root)
    assert await _wake(env) == []
    assert (await intention_of(env, "subtask", root.source_id)).state == "awaiting_owner"


async def test_an_expired_question_wakes_with_a_row_saying_nobody_answered(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    root = await make_root(env)
    done = await _ask(env, root)
    async with env.db.session() as s:
        await s.execute(
            update(ResultInbox)
            .where(ResultInbox.arrival_id == done.arrival_id, ResultInbox.msg_type == "QUESTION")
            .values(created_at=datetime.now(UTC) - timedelta(hours=25), push_after=None)
        )
        await s.commit()
    assert await _wake(env) == [root.id]
    assert await _wake(env) == []  # idempotent: nothing is awaiting any more
    rows = [r for r in await inbox_rows(env) if r.msg_type == "INFORM" and r.arrival_id == done.arrival_id]
    assert len(rows) == 1 and "did not answer" in rows[0].body and rows[0].channel is None  # keyed by intention alone
    got = await claim(env, root.id)
    assert [r.id for r in got.inbox_rows] == [rows[0].id]  # the woken claim has something to show


async def test_one_root_can_hold_two_awaiting_arrivals_and_each_wakes_alone(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    root = await make_root(env)
    child = await make_child(env, root)
    first = await _ask(env, root, question="First question?")
    second = await _ask(env, child, root_id=root.id, question="Second question?")  # root awaiting: claimable
    assert first.arrival_id != second.arrival_id
    await _answer(env, first.arrival_id, root)
    assert await _wake(env) == [root.id]
    assert (await intention_of(env, "subtask", root.source_id)).state == "result_ready"
    assert (await intention_of(env, "subtask", child.source_id)).state == "awaiting_owner"  # its own question is open


async def test_an_expiry_and_a_wake_that_meet_on_an_expired_question_do_not_deadlock(env_factory, monkeypatch):  # noqa: F811
    """Root first in the wake too. The arrival lists the low-id child before the high-id root, so a wake that
    locked the arrival's intentions in that order would hold the child while the expiry holds the root and
    wants the child. Whichever is granted the root first, both finish: the expiry first, and the wake finds
    nothing awaiting and writes nothing; the wake first, and its "did not answer" rows are in the expiry's
    report. Never one on an expired lineage, where record_result would make it a raw report to the owner."""
    _high_then_low_ids(monkeypatch)
    env = await env_factory(**CONT)
    root = await make_root(env)  # the high id
    child = await make_child(env, root)  # the low id
    await record(env, root)
    await record(env, child)
    got = await claim(env, root.id)
    done = await _commit(env, got, Resolution("ask", "Shall I book it?", True, 0.8))
    await _age_question(env, done.arrival_id)
    await _past(env, root.id)
    async with env.db.session() as holder:
        await holder.execute(select(Intention.id).where(Intention.id == root.id).with_for_update(key_share=True))
        sweep = asyncio.create_task(_expire(env))
        await asyncio.wait_for(until_a_backend_waits_on_a_lock(env), timeout=10)
        wake = asyncio.create_task(_wake(env))
        try:
            await asyncio.wait_for(until_a_backend_waits_on_a_lock(env, at_least=2), timeout=10)
        finally:
            await holder.commit()
    assert await asyncio.wait_for(sweep, timeout=30) == [root.id]  # the lineage expires either way
    woken = await asyncio.wait_for(wake, timeout=30)
    assert set(woken) in (set(), {root.id, child.id})  # the order of the grant is Postgres's, not the test's
    informs = [r for r in await inbox_rows(env) if r.msg_type == "INFORM" and r.arrival_id == done.arrival_id]
    assert len(informs) == (2 if woken else 0) and all(r.delivered_at is not None for r in informs)
    assert not any(r.title == "The owner did not answer" for r in await _owner_rows(env))
