"""F099 Phase 2c-2: the runner's sweep and loop (spec 4.5.1, 4.5.2, 4.6)."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from f099_support import (
    ON,
    age,
    claim,
    env_factory,  # noqa: F401
    finish,
    inbox_rows,
    intention_of,
    make_dag,
    make_root,
    make_subtask,
    record,
    runner_env,  # noqa: F401
    say,
    set_intention,
    use,
)
from sqlalchemy import select, update

import nous.handlers.continuation_runner as runner_module
from nous.brain import continuation
from nous.config import Settings
from nous.events import Event
from nous.handlers.continuation_runner import ContinuationRunner
from nous.heart.result_inbox import record_subtask_result
from nous.storage.models import ExecutionDAG, IntentionArrival

pytestmark = pytest.mark.postgres_only


def _cont(env, **kw) -> ContinuationRunner:
    env.cont = ContinuationRunner(
        database=env.db, settings=env.settings, runner=env.runner, heart=env.heart, brain=env.brain, bus=env.bus, **kw
    )
    return env.cont


def resolve(decision="drop", note="Nothing more to do.", progress=False, confidence=0.7):
    return use("resolve_intention", decision=decision, note=note, progress=progress, confidence=confidence)


async def _settle(cont: ContinuationRunner) -> None:
    tasks = list(cont._running.values())
    if tasks:
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=30)


async def _arrivals(env, root_id):
    async with env.db.session() as s:
        query = select(IntentionArrival).where(
            IntentionArrival.agent_id == env.agent, IntentionArrival.root_id == root_id
        )
        return list((await s.execute(query.order_by(IntentionArrival.n))).scalars().all())


async def _ready_root(env):
    root = await make_root(env)
    await record(env, root)
    return root


async def test_a_sweep_launches_a_root_that_is_due_and_the_arrival_completes(runner_env):  # noqa: F811
    env = await runner_env([resolve("report", "The snow is deep.")])
    root = await _ready_root(env)
    cont = _cont(env)
    report = await cont.run_once()
    assert report.launched == (root.id,) and cont.running_roots == frozenset({root.id})
    await _settle(cont)
    (arrival,) = await _arrivals(env, root.id)
    assert arrival.decision == "report" and cont.running_roots == frozenset()
    assert (await cont.run_once()).launched == ()  # nothing left to do


async def test_a_sweep_leaves_a_root_inside_its_debounce_and_says_when_it_is_due(runner_env):  # noqa: F811
    env = await runner_env(continuation_debounce_seconds=60, continuation_max_wait_seconds=600)
    root = await _ready_root(env)
    result_at = (await intention_of(env, "subtask", root.source_id)).result_at
    report = await _cont(env).run_once()
    assert report.launched == () and env.model.calls == []
    assert abs((report.next_due - (result_at + timedelta(seconds=60))).total_seconds()) < 1


async def test_the_concurrency_bound_holds_and_the_next_root_goes_when_a_slot_frees(runner_env):  # noqa: F811
    started, go = asyncio.Event(), asyncio.Event()

    async def blocked(kwargs):
        started.set()
        await go.wait()
        return [resolve()]

    env = await runner_env(blocked, [resolve()], continuation_max_concurrent=1)
    first, second = await _ready_root(env), await _ready_root(env)
    await age(env, first.id, seconds=100)  # first is due earlier
    cont = _cont(env)
    assert (await cont.run_once()).launched == (first.id,)
    await asyncio.wait_for(started.wait(), timeout=10)
    busy = await cont.run_once()
    assert busy.launched == () and cont.running_roots == frozenset({first.id})  # the one slot is taken
    go.set()
    await _settle(cont)
    assert (await cont.run_once()).launched == (second.id,)
    await _settle(cont)
    assert len(await _arrivals(env, first.id)) == len(await _arrivals(env, second.id)) == 1


async def test_a_root_that_was_due_but_not_claimable_is_not_retried_at_once(runner_env, monkeypatch):  # noqa: F811
    env = await runner_env()
    root = await _ready_root(env)

    async def nothing(*args, **kwargs):
        return None  # the claim found nothing (a clock disagreement, a race lost)

    monkeypatch.setattr(continuation, "claim_root", nothing)
    cont = _cont(env)
    assert (await cont.run_once()).launched == (root.id,)
    await _settle(cont)
    again = await cont.run_once()
    assert again.launched == () and again.next_due is not None  # cooling down: the loop will not spin
    cont._cooldown[root.id] = datetime.now(UTC) - timedelta(seconds=1)
    assert (await cont.run_once()).launched == (root.id,)
    await _settle(cont)


async def test_one_root_whose_arrival_raises_does_not_stop_the_next(runner_env, caplog):  # noqa: F811
    """Lead note 2: an error outside run_arrival's own handling (here, the whole call raises) is caught per root:
    it is logged, the root cools down, and the next root of the same sweep still runs."""
    env = await runner_env([resolve("report", "The second one ran.")])
    broken, fine = await _ready_root(env), await _ready_root(env)
    await age(env, broken.id, seconds=100)  # the broken root is launched first
    cont = _cont(env)
    real = cont.run_arrival

    async def run_arrival(root_id):
        if root_id == broken.id:
            raise RuntimeError("the database went away")
        return await real(root_id)

    cont.run_arrival = run_arrival
    report = await cont.run_once()
    assert report.launched == (broken.id, fine.id)
    await _settle(cont)
    assert await _arrivals(env, broken.id) == [] and len(await _arrivals(env, fine.id)) == 1
    assert broken.id in cont._cooldown and fine.id not in cont._cooldown
    assert cont.running_roots == frozenset() and "raised" in caplog.text
    assert (await cont.run_once()).launched == ()  # the broken root cools down: not retried at once


async def test_a_crash_after_the_claim_is_recovered_by_the_first_sweep_after_the_lease(runner_env):  # noqa: F811
    """Review Focus 4: a process claimed a root and died. Its claim has no live turn. Inside the lease
    nothing happens; after it the sweep releases the claim and the same sweep decides it."""
    env = await runner_env([resolve("drop", "Not needed.")])
    root = await _ready_root(env)
    assert await claim(env, root.id) is not None  # ... and the process is gone
    cont = _cont(env)
    inside = await cont.run_once()
    assert (inside.released, inside.launched) == (0, ())  # a live-looking claim is left alone
    await set_intention(env, root.id, claimed_at=datetime.now(UTC) - timedelta(seconds=1000))
    after = await cont.run_once()
    assert (after.released, after.launched) == (1, (root.id,))
    await _settle(cont)
    (arrival,) = await _arrivals(env, root.id)
    assert arrival.decision == "drop"
    (row,) = await inbox_rows(env, UUID(root.source_id))
    assert row.delivered_at is not None  # stamped by the commit, once
    fresh = await intention_of(env, "subtask", root.source_id)
    assert (fresh.state, fresh.close_reason, fresh.attempts) == ("closed", "resolved", 0)


async def test_a_sweep_expires_a_root_past_its_ttl_and_announces_it(runner_env):  # noqa: F811
    env = await runner_env()
    root = await make_root(env)
    await set_intention(env, root.id, deadline=datetime.now(UTC) - timedelta(hours=1))
    report = await _cont(env).run_once()
    assert report.expired_roots == 1
    assert [e.data for e in env.bus.events if e.type == "intention.root_expired"] == [{"root_id": str(root.id)}]


async def test_a_sweep_wakes_an_answered_question_and_decides_the_answer(runner_env):  # noqa: F811
    env = await runner_env([resolve("drop", "Thanks, done.")])
    root = await _ready_root(env)
    got = await claim(env, root.id)
    async with env.db.session() as s:
        asked = await continuation.commit_arrival(
            s,
            env.agent,
            got,
            resolution=continuation.Resolution("ask", "Shall I book it?", True, 0.8),
            outcome="resolved",
            settings=env.settings,
        )
        await s.commit()
    async with env.db.session() as s:
        await continuation.record_result(
            s,
            env.agent,
            intention_id=root.id,
            source_kind=continuation.SOURCE_INTENTION_REPORT,
            source_id=uuid.uuid4(),
            msg_type="INFORM",
            title="Owner's answer",
            correlation_id=f"{continuation.ANSWER_CORRELATION_PREFIX}telegram:42",  # as record_answer writes it
            body="Yes, book it.",
            arrival_id=asked.arrival_id,
            settings=env.settings,
        )
        await s.commit()
    cont = _cont(env)
    report = await cont.run_once()
    assert report.launched == (root.id,)  # woken, and decided in the same sweep
    await _settle(cont)
    first, second = await _arrivals(env, root.id)
    assert (first.decision, second.decision) == ("ask", "drop") and "Yes, book it." in env.cognitive.pre_turn_calls[0][
        "user_message"
    ]


async def test_the_sweep_runs_the_publisher_and_counts_what_it_pushed(runner_env):  # noqa: F811
    class Publisher:
        calls = 0

        async def push_due(self):
            type(self).calls += 1
            return 2

    env = await runner_env()
    report = await _cont(env, publisher=Publisher()).run_once()
    assert report.pushed == 2 and Publisher.calls == 1


@pytest.mark.parametrize(
    ("step", "label"),
    [
        ("release_stale_claims", "lease release"),
        ("expire_roots", "TTL sweep"),
        ("expire_proposals", "proposal expiry"),
        ("wake_terminal_arrivals", "question wake"),
    ],
)
async def test_one_failing_step_does_not_stop_the_others(runner_env, monkeypatch, caplog, step, label):  # noqa: F811
    """Carry-over 6: each sweep step is isolated. (Repair is the reconciler pass's, isolated there: C13.)"""
    env = await runner_env([resolve()])
    root = await _ready_root(env)

    async def boom(*args, **kwargs):
        raise RuntimeError("the store is down")

    monkeypatch.setattr(continuation, step, boom)
    cont = _cont(env)
    report = await cont.run_once()
    assert report.launched == (root.id,) and label in caplog.text  # the launch still happened
    await _settle(cont)


async def test_a_failing_push_does_not_stop_the_launch(runner_env, caplog):  # noqa: F811
    class Publisher:
        async def push_due(self):
            raise RuntimeError("telegram is down")

    env = await runner_env([resolve()])
    root = await _ready_root(env)
    cont = _cont(env, publisher=Publisher())
    report = await cont.run_once()
    assert report.launched == (root.id,) and report.pushed == 0 and "owner push" in caplog.text
    await _settle(cont)


async def test_a_failing_launch_does_not_stop_the_steps_before_it(runner_env, monkeypatch, caplog):  # noqa: F811
    """The launch is a step like the others: it fails alone, and the expiry that ran before it is still counted."""
    env = await runner_env()
    root = await make_root(env)
    await set_intention(env, root.id, deadline=datetime.now(UTC) - timedelta(hours=1))

    async def boom(*args, **kwargs):
        raise RuntimeError("the eligibility scan is down")

    monkeypatch.setattr(continuation, "eligible_roots", boom)
    report = await _cont(env).run_once()
    assert report.launched == () and report.expired_roots == 1 and "launch" in caplog.text


async def test_the_first_sweep_after_the_flip_launches_nothing_for_a_phase_1_world(runner_env):  # noqa: F811
    """Review Focus 3: what Phase 1 left behind (legacy closes, delivered F098 rows, a DAG closed legacy, an
    old root with no deadline) produces no turn, no arrival, no report and no push at the first sweep. The
    TTL sweep closes the old root silently."""
    # with_bounds is applied by the tools, REST, the scheduler, the work queue and a2ui, not by the store.
    env = await runner_env()
    phase_1 = Settings(_env_file=None, agent_id=env.agent, **ON)  # the writers as they ran with the flag off
    done = await make_subtask(env, policy="continue")
    await finish(env, done)
    await record_subtask_result(env.heart.result_inbox, await env.heart.subtasks.get(done.id), phase_1)
    dag, _ = await make_dag(env, policy="continue", status="completed")
    async with env.db.session() as s:
        await s.execute(update(ExecutionDAG).where(ExecutionDAG.id == dag.id).values(delivered_at=datetime.now(UTC)))
        await s.commit()
    await set_intention(env, (await intention_of(env, "dag", dag.id)).id, state="closed", close_reason="legacy")
    stale = await make_root(env)  # still running since before the flip: no deadline, older than the TTL
    assert (await intention_of(env, "subtask", stale.source_id)).deadline is None  # the case needs it
    await set_intention(env, stale.id, created_at=datetime.now(UTC) - timedelta(hours=100))
    rows_before = len(await inbox_rows(env))
    cont = _cont(env)
    report = await cont.run_once()
    assert (report.released, report.launched, report.pushed) == (0, (), 0)
    assert report.expired_roots == 1  # the stale root, closed silently
    assert env.model.calls == [] and env.cognitive.pre_turn_calls == []
    assert [e.type for e in env.bus.events if e.type == "intention.arrival_decided"] == []
    assert len(await inbox_rows(env)) == rows_before  # no REPORT, nothing written
    assert (await intention_of(env, "subtask", done.id)).close_reason == "legacy"
    assert (await intention_of(env, "dag", dag.id)).close_reason == "legacy"
    assert (await intention_of(env, "subtask", stale.source_id)).state == "expired"


async def test_a_result_ready_hint_wakes_the_loop(runner_env):  # noqa: F811
    env = await runner_env()
    cont = _cont(env)
    cont._wake.clear()
    await cont.on_result_ready(Event(type="intention.result_ready", agent_id=env.agent, data={}))
    assert cont._wake.is_set()


async def test_the_loop_decides_a_woken_root_without_waiting_a_sweep(runner_env, monkeypatch):  # noqa: F811
    monkeypatch.setattr(runner_module, "SWEEP_INTERVAL_SECONDS", 30)  # a sweep would be far too late
    env = await runner_env([resolve("report", "Woken.")])
    cont = _cont(env)
    await cont.start()
    try:
        root = await _ready_root(env)  # a result lands while the loop sleeps
        cont.wake()  # as the bus hint does
        await _until_arrived(env, root.id)
    finally:
        await cont.stop()


async def test_the_loop_sleeps_until_the_debounce_ends_on_its_own(runner_env, monkeypatch):  # noqa: F811
    monkeypatch.setattr(runner_module, "SWEEP_INTERVAL_SECONDS", 30)
    env = await runner_env(
        [resolve("report", "Due.")], continuation_debounce_seconds=2, continuation_max_wait_seconds=10
    )
    root = await _ready_root(env)  # fresh: not due for two seconds
    cont = _cont(env)
    await cont.start()
    try:
        await _until_arrived(env, root.id, timeout=15)  # no wake: the loop computed when to look again
    finally:
        await cont.stop()


async def test_a_wake_during_the_sweep_is_not_lost(runner_env, monkeypatch):  # noqa: F811
    """The loop clears its wake BEFORE the sweep: a wake that lands while a sweep runs starts the next one at once."""
    monkeypatch.setattr(runner_module, "SWEEP_INTERVAL_SECONDS", 30)  # without the wake, the next sweep is 30 s off
    second = asyncio.Event()

    class Publisher:
        calls = 0

        async def push_due(self):
            type(self).calls += 1
            if type(self).calls == 1:
                env.cont.wake()  # mid-sweep, as an arrival that ends during it does
            else:
                second.set()
            return 0

    env = await runner_env()
    cont = _cont(env, publisher=Publisher())
    await cont.start()
    try:
        await asyncio.wait_for(second.wait(), timeout=5)
    finally:
        await cont.stop()


async def _until_arrived(env, root_id, timeout=10):
    async def arrived():
        while not await _arrivals(env, root_id):
            await asyncio.sleep(0.05)

    await asyncio.wait_for(arrived(), timeout=timeout)


async def test_a_cancellation_from_within_does_not_end_the_loop(runner_env, monkeypatch):  # noqa: F811
    """Fix-Z: a CancelledError that came out of something the loop awaited is that thing's failure."""
    monkeypatch.setattr(runner_module, "SWEEP_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(runner_module, "LOOP_RETRY_SECONDS", 0.01)
    env = await runner_env()
    cont = _cont(env)
    calls = {"n": 0}

    async def run_once():
        calls["n"] += 1
        if calls["n"] == 1:
            victim = asyncio.get_running_loop().create_future()
            victim.cancel()
            await victim  # cancelled from within: nobody cancelled the loop's task
        return continuation.SweepReport(0, 0, 0, 0, (), None)

    cont.run_once = run_once
    await cont.start()
    try:
        deadline = asyncio.get_running_loop().time() + 5
        while calls["n"] < 3:
            assert asyncio.get_running_loop().time() < deadline, "the loop ended after a cancellation from within"
            await asyncio.sleep(0.01)
        assert not cont._task.done()
    finally:
        task = cont._task
        await cont.stop()
    assert task.done()  # its own cancellation does end it


async def test_a_cancellation_from_within_an_arrival_is_a_failed_attempt_and_cools_down(runner_env, caplog):  # noqa: F811
    """Fix-Z inside an arrival (the #690/#691 class): a CancelledError nobody requested came out of something the
    arrival awaited. It is not a stop and not a cancel, so it is charged as a failed attempt and the root cools down:
    a recurring one reaches failed_report instead of coming back at once, silently, for good."""
    env = await runner_env(continuation_max_concurrent=1)
    root = await _ready_root(env)
    cont = _cont(env)
    entered = asyncio.Event()

    async def cancelled_from_within(*args, **kwargs):
        entered.set()
        victim = asyncio.get_running_loop().create_future()
        victim.cancel()
        await victim  # nobody cancelled the arrival's task

    cont._run_turns = cancelled_from_within
    await cont.start()
    try:
        await asyncio.wait_for(entered.wait(), timeout=10)
        arrival = cont._running[root.id]
        await asyncio.wait_for(asyncio.gather(arrival, return_exceptions=True), timeout=10)
        fresh = await intention_of(env, "subtask", root.source_id)
        assert (fresh.state, fresh.attempts, fresh.claim_token) == ("result_ready", 1, None)  # charged, released
        assert root.id in cont._cooldown and "cancelled from within" in caplog.text
        assert cont.running_roots == frozenset() and not cont._slots.locked()  # the one slot is free again
        assert not cont._task.done()  # the loop goes on
    finally:
        await cont.stop()


async def test_a_cancellation_from_within_ending_the_session_keeps_the_decision(runner_env, caplog):  # noqa: F811
    """Final review M1: a CancelledError nobody requested out of end_conversation comes after the model decided. The
    decision is committed (no attempt charged, no second turn); the session is left to the idle monitor."""
    env = await runner_env([resolve("report", "The snow is deep.")])
    root = await _ready_root(env)
    cont = _cont(env)
    ended = asyncio.Event()

    async def cancelled_from_within(session_id, *args, **kwargs):
        ended.set()
        victim = asyncio.get_running_loop().create_future()
        victim.cancel()
        await victim  # nobody cancelled the arrival's task

    env.runner.end_conversation = cancelled_from_within
    await cont.start()
    try:
        await asyncio.wait_for(ended.wait(), timeout=10)
        await _until_arrived(env, root.id)
        (arrival,) = await _arrivals(env, root.id)
        assert (arrival.decision, arrival.outcome) == ("report", "resolved")
        fresh = await intention_of(env, "subtask", root.source_id)
        assert (fresh.state, fresh.close_reason, fresh.attempts) == ("closed", "resolved", 0)  # no attempt charged
        assert [r.msg_type for r in await inbox_rows(env) if r.source_kind == "intention_report"] == ["REPORT"]
        assert "could not be ended" in caplog.text and root.id not in cont._cooldown
        assert len(env.model.calls) == 1 and not cont._task.done()  # one turn; the loop goes on
    finally:
        await cont.stop()


async def test_a_stop_while_ending_the_session_still_ends_the_arrival(runner_env):  # noqa: F811  # PIN
    """The other side of the guard: a stop is a real cancel, so it re-raises out of end_conversation, the claim is
    released without an attempt and nothing is committed."""
    env = await runner_env([resolve("report", "The snow is deep.")])
    root = await _ready_root(env)
    cont = _cont(env)
    ending = asyncio.Event()

    async def slow_end(session_id, *args, **kwargs):
        ending.set()
        await asyncio.sleep(30)

    env.runner.end_conversation = slow_end
    await cont.start()
    await asyncio.wait_for(ending.wait(), timeout=10)
    arrival = cont._running[root.id]
    await asyncio.wait_for(cont.stop(), timeout=15)
    assert arrival.cancelled()  # re-raised: the stop ended the arrival
    assert await _arrivals(env, root.id) == []
    fresh = await intention_of(env, "subtask", root.source_id)
    assert (fresh.state, fresh.attempts, fresh.claim_token) == ("result_ready", 0, None)


async def test_an_ended_arrival_gives_back_only_what_it_holds(runner_env):  # noqa: F811
    """The done callback unmaps its root only while the map still holds THIS task (a late callback of an older task
    cannot unmap a newer arrival, which 2e's cancel reads), and the cap is bounded: a stray release raises instead of
    quietly widening it."""
    env = await runner_env(continuation_max_concurrent=2)
    cont = _cont(env)
    root_id = uuid.uuid4()
    older, newer = asyncio.get_running_loop().create_future(), asyncio.get_running_loop().create_future()
    await cont._slots.acquire()  # the older arrival's slot
    await cont._slots.acquire()  # the newer one's
    cont._running[root_id] = newer
    cont._arrival_ended(root_id, older)
    assert cont._running[root_id] is newer and not cont._slots.locked()  # its slot back, not the newer's place
    cont._arrival_ended(root_id, newer)
    assert cont.running_roots == frozenset()
    with pytest.raises(ValueError):
        cont._slots.release()  # both slots are back: one more release is a bug, and it says so


async def test_stop_releases_a_running_arrival_without_an_attempt(runner_env):  # noqa: F811
    started = asyncio.Event()

    async def blocked(kwargs):
        started.set()
        await asyncio.sleep(30)
        return [say("never")]

    env = await runner_env(blocked)
    root = await _ready_root(env)
    cont = _cont(env)
    assert (await cont.run_once()).launched == (root.id,)
    await asyncio.wait_for(started.wait(), timeout=10)
    await asyncio.wait_for(cont.stop(), timeout=15)
    assert cont.running_roots == frozenset()
    fresh = await intention_of(env, "subtask", root.source_id)
    assert (fresh.state, fresh.attempts, fresh.claim_token) == ("result_ready", 0, None)


async def test_an_arrival_cancelled_before_it_started_gives_its_slot_back(runner_env):  # noqa: F811
    """A task cancelled before its first step never runs its body (so no ``finally`` in it either): the slot and the
    running map are given back by the task's done callback. A stop, or 2e's cancel, right after a launch does this."""
    env = await runner_env([resolve()], continuation_max_concurrent=1)
    root = await _ready_root(env)
    cont = _cont(env)
    assert (await cont.run_once()).launched == (root.id,)  # created, not yet started: run_once does not yield after it
    cont._running[root.id].cancel()
    await asyncio.wait_for(asyncio.gather(*cont._running.values(), return_exceptions=True), timeout=10)
    assert cont.running_roots == frozenset() and not cont._slots.locked()  # the one slot is free again
    assert (await cont.run_once()).launched == (root.id,)  # nothing was claimed, so the root is simply due again
    await _settle(cont)
    (arrival,) = await _arrivals(env, root.id)
    assert arrival.decision == "drop"


async def test_start_releases_old_claims_and_starting_twice_makes_one_loop(runner_env):  # noqa: F811
    env = await runner_env(continuation_debounce_seconds=3600, continuation_max_wait_seconds=7200)  # nothing is due
    root = await _ready_root(env)
    await claim(env, root.id)
    await set_intention(env, root.id, claimed_at=datetime.now(UTC) - timedelta(seconds=1000))
    cont = _cont(env)
    await cont.start()
    try:
        first = cont._task
        await cont.start()
        assert cont._task is first  # no second loop
        released = await intention_of(env, "subtask", root.source_id)
        assert (released.state, released.attempts) == ("result_ready", 1)  # the startup sweep released it
    finally:
        await cont.stop()
