"""F099 Phase 2c-1: failed attempts, the lease, and the fence between a sweep and a commit (spec 4.5)."""

from __future__ import annotations

import asyncio
import dataclasses
import uuid
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from f099_support import (
    CHAN,
    CONT,
    RESULT,
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
from sqlalchemy import select, text

from nous.brain import continuation
from nous.brain.continuation import Resolution
from nous.storage.models import Intention, IntentionArrival

pytestmark = pytest.mark.postgres_only  # FOR NO KEY UPDATE, savepoints, two transactions


async def _claimed(env, root=None):
    root = root or await make_root(env)
    await record(env, root)
    return root, await claim(env, root.id)


async def _fail(env, got, *, max_attempts=3):
    async with env.db.session() as s:
        outcome = await continuation.fail_attempt(s, env.agent, got, max_attempts=max_attempts, settings=env.settings)
        await s.commit()
    return outcome


async def _sweep(env, *, lease=900, max_attempts=3):
    async with env.db.session() as s:
        released = await continuation.release_stale_claims(
            s, env.agent, lease_s=lease, max_attempts=max_attempts, settings=env.settings
        )
        await s.commit()
    return released


async def _stale(env, *intention_ids, seconds=1000):
    when = datetime.now(UTC) - timedelta(seconds=seconds)
    for intention_id in intention_ids:
        await set_intention(env, intention_id, claimed_at=when)


async def _arrivals(env, root_id):
    async with env.db.session() as s:
        query = select(IntentionArrival).where(
            IntentionArrival.agent_id == env.agent, IntentionArrival.root_id == root_id
        )
        return list((await s.execute(query.order_by(IntentionArrival.n))).scalars().all())


async def _owner_rows(env):
    return [r for r in await inbox_rows(env) if r.source_kind == "intention_report"]


async def test_a_failed_attempt_returns_the_intention_for_a_retry(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    root, got = await _claimed(env)
    assert await _fail(env, got) == "retry"
    fresh = await intention_of(env, "subtask", root.source_id)
    assert (fresh.state, fresh.attempts, fresh.claim_token, fresh.claimed_at) == ("result_ready", 1, None, None)
    (row,) = await inbox_rows(env, UUID(root.source_id))
    assert row.delivered_at is None  # nothing was consumed
    assert await _arrivals(env, root.id) == []  # a failed attempt is not an arrival


async def test_a_retry_waits_for_the_debounce_again(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    root, got = await _claimed(env)
    await _fail(env, got)
    assert await claim(env, root.id, debounce=60, max_wait=600) is None  # result_at is now: no hot retry loop
    assert await claim(env, root.id, debounce=0, max_wait=0) is not None


async def test_three_failures_report_the_raw_results(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    root = await make_root(env)
    await record(env, root, body="40 cm overnight on the upper mountain")
    outcomes = []
    for _ in range(3):
        got = await claim(env, root.id)
        outcomes.append(await _fail(env, got))
    assert outcomes == ["retry", "retry", "failed_report"]
    fresh = await intention_of(env, "subtask", root.source_id)
    assert (fresh.state, fresh.close_reason, fresh.attempts, fresh.claim_token) == ("closed", "failed_report", 3, None)
    (arrival,) = await _arrivals(env, root.id)
    assert (arrival.outcome, arrival.decision, arrival.progress, arrival.n) == ("failed_report", "report", None, 1)
    (report,) = await _owner_rows(env)
    assert report.msg_type == "REPORT" and report.channel == CHAN and report.intention_id == root.id
    assert "40 cm overnight" in report.body and "3 attempts" in report.body
    (row,) = await inbox_rows(env, UUID(root.source_id))
    assert row.delivered_at is not None and list(arrival.inbox_ids) == [row.id]  # consumed by the report
    assert list(arrival.report_ids) == [report.source_id]


async def test_a_stale_token_cannot_fail_an_attempt(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    root, got = await _claimed(env)
    assert await _fail(env, dataclasses.replace(got, claim_token=uuid.uuid4())) == "lost"
    fresh = await intention_of(env, "subtask", root.source_id)
    assert (fresh.state, fresh.attempts, fresh.claim_token) == ("deciding", 0, got.claim_token)


async def test_release_claim_frees_the_rows_without_charging_an_attempt(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    root, got = await _claimed(env)
    async with env.db.session() as s:
        assert await continuation.release_claim(s, env.agent, got) == 1
        await s.commit()
    fresh = await intention_of(env, "subtask", root.source_id)
    assert (fresh.state, fresh.attempts, fresh.claim_token, fresh.claimed_at) == ("result_ready", 0, None, None)
    async with env.db.session() as s:  # a second release is a no-op: the fence no longer matches
        assert await continuation.release_claim(s, env.agent, got) == 0


async def test_a_stale_claim_is_released_with_an_attempt_and_a_fresh_one_is_not(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    old, old_claim = await _claimed(env)
    fresh_root, fresh_claim = await _claimed(env)
    await _stale(env, old.id)
    assert await _sweep(env) == [old.id]
    released = await intention_of(env, "subtask", old.source_id)
    assert (released.state, released.attempts, released.claim_token) == ("result_ready", 1, None)
    live = await intention_of(env, "subtask", fresh_root.source_id)
    assert (live.state, live.attempts, live.claim_token) == ("deciding", 0, fresh_claim.claim_token)
    assert old_claim.claim_token != fresh_claim.claim_token


async def test_the_lease_release_applies_the_cap_too(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    root, got = await _claimed(env)
    await set_intention(env, root.id, attempts=2)  # two failed attempts already
    await _stale(env, root.id)
    assert await _sweep(env) == [root.id]
    fresh = await intention_of(env, "subtask", root.source_id)
    assert (fresh.state, fresh.close_reason) == ("closed", "failed_report")
    (report,) = await _owner_rows(env)
    assert RESULT in report.body


async def test_a_stale_token_cannot_commit_after_its_lease_was_released(env_factory):  # noqa: F811
    """Review Focus 1: a turn that outlived its lease writes nothing."""
    env = await env_factory(**CONT)
    root, first = await _claimed(env)
    await _stale(env, root.id)
    assert await _sweep(env) == [root.id]
    second = await claim(env, root.id)  # a new claim, a new token
    assert second is not None and second.claim_token != first.claim_token
    async with env.db.session() as s:
        late = await continuation.commit_arrival(
            s,
            env.agent,
            first,
            resolution=Resolution("report", "Late news.", True, 0.9),
            outcome="resolved",
            settings=env.settings,
        )
        await s.commit()
    assert late is None
    assert await _arrivals(env, root.id) == [] and await _owner_rows(env) == []
    (row,) = await inbox_rows(env, UUID(root.source_id))
    assert row.delivered_at is None
    live = await intention_of(env, "subtask", root.source_id)
    assert (live.state, live.claim_token) == ("deciding", second.claim_token)


async def test_a_crash_after_the_claim_is_recovered_by_the_lease(env_factory):  # noqa: F811
    """Review Focus 5: the process dies between the claim and the commit. Nothing was stamped, so the
    next claim finds the result exactly as it was, and consumes it normally."""
    env = await env_factory(**CONT)
    root, _lost_claim = await _claimed(env)  # ... and the process is gone: nobody commits or fails it
    (row,) = await inbox_rows(env, UUID(root.source_id))
    assert row.delivered_at is None
    await _stale(env, root.id)
    assert await _sweep(env) == [root.id]
    again = await claim(env, root.id)
    assert again is not None and [r.id for r in again.inbox_rows] == [row.id]
    async with env.db.session() as s:
        done = await continuation.commit_arrival(
            s,
            env.agent,
            again,
            resolution=Resolution("drop", "Not needed any more.", False, 0.9),
            outcome="resolved",
            settings=env.settings,
        )
        await s.commit()
    assert done is not None
    (after,) = await inbox_rows(env, UUID(root.source_id))
    assert after.delivered_at is not None
    assert (await intention_of(env, "subtask", root.source_id)).attempts == 0  # the commit reset the count


async def test_a_sweep_that_races_a_commit_loses_to_it(env_factory):  # noqa: F811
    """Review Focus 1 and 6, deterministically: a second transaction holds the claimed row (a commit
    that has not committed) while the sweep runs. The sweep must wait for it, and then find the row
    no longer `deciding`: it releases nothing and the arrival stays decided."""
    env = await env_factory(**CONT)
    root, got = await _claimed(env)
    await _stale(env, root.id)
    async with env.db.session() as holder:
        done = await continuation.commit_arrival(
            holder,
            env.agent,
            got,
            resolution=Resolution("report", "Decided.", True, 0.9),
            outcome="resolved",
            settings=env.settings,
        )
        assert done is not None  # decided, not committed: its row locks are held
        sweep = asyncio.create_task(_sweep(env))
        try:
            await asyncio.wait_for(until_a_backend_waits_on_a_lock(env), timeout=10)
        finally:
            await holder.commit()  # always release the rows
    assert await asyncio.wait_for(sweep, timeout=30) == []
    fresh = await intention_of(env, "subtask", root.source_id)
    assert (fresh.state, fresh.close_reason, fresh.attempts) == ("closed", "resolved", 0)
    assert len(await _arrivals(env, root.id)) == 1


async def test_a_commit_that_races_a_sweep_loses_to_it(env_factory):  # noqa: F811
    """The other order: the sweep has released the claim and not committed; the commit waits, then
    finds its token gone and writes nothing."""
    env = await env_factory(**CONT)
    root, got = await _claimed(env)
    await _stale(env, root.id)

    async def late_commit():
        async with env.db.session() as s:
            done = await continuation.commit_arrival(
                s,
                env.agent,
                got,
                resolution=Resolution("report", "Too late.", True, 0.9),
                outcome="resolved",
                settings=env.settings,
            )
            await s.commit()
        return done

    async with env.db.session() as holder:
        released = await continuation.release_stale_claims(
            holder, env.agent, lease_s=900, max_attempts=3, settings=env.settings
        )
        assert released == [root.id]
        commit = asyncio.create_task(late_commit())
        try:
            await asyncio.wait_for(until_a_backend_waits_on_a_lock(env), timeout=10)
        finally:
            await holder.commit()
    assert await asyncio.wait_for(commit, timeout=30) is None
    fresh = await intention_of(env, "subtask", root.source_id)
    assert (fresh.state, fresh.attempts) == ("result_ready", 1)
    assert await _arrivals(env, root.id) == [] and await _owner_rows(env) == []


async def test_a_sweep_at_the_cap_takes_the_root_before_the_claimed_row(env_factory):  # noqa: F811
    """Review Focus 6 for the sweep: the cap's report needs the root row, so the sweep locks the root
    before the claimed row, as the commit and the expiry do. Only a child is claimed here, so the root
    is not itself a claimed row. A holder takes the root, waits until the sweep waits on it, then takes
    the claimed child: under the one lock order the sweep holds nothing yet and the holder goes through.
    A sweep that locked the child first would close a cycle that Postgres breaks with an error."""
    env = await env_factory(**CONT)
    root = await make_root(env)  # its own subtask is still pending: it is not claimed
    child = await make_child(env, root)
    await record(env, child)
    got = await claim(env, root.id)
    assert got is not None and [i.id for i in got.intentions] == [child.id]
    await set_intention(env, child.id, attempts=2)  # the next failure reaches the cap
    await _stale(env, child.id)
    lock = select(Intention.id).where(Intention.agent_id == env.agent).with_for_update(key_share=True)
    async with env.db.session() as holder:
        await holder.execute(text("SET LOCAL lock_timeout = '5s'"))  # a bounded holder
        await holder.execute(lock.where(Intention.id == root.id))
        sweep = asyncio.create_task(_sweep(env))
        try:
            await asyncio.wait_for(until_a_backend_waits_on_a_lock(env), timeout=10)
            await holder.execute(lock.where(Intention.id == child.id))  # the expiry's second lock
        finally:
            await holder.commit()
    assert await asyncio.wait_for(sweep, timeout=30) == [child.id]
    fresh = await intention_of(env, "subtask", child.source_id)
    assert (fresh.state, fresh.close_reason, fresh.attempts) == ("closed", "failed_report", 3)
    (report,) = await _owner_rows(env)
    assert report.intention_id == child.id and RESULT in report.body


async def test_a_late_row_after_a_failed_report_keeps_the_count(env_factory):  # noqa: F811
    """Lead ruling (2c1-5): a row that arrived while the capped claim ran sends its intention back to
    ``result_ready`` with ``attempts`` kept, as a retry below the cap keeps it. The late row gets one
    more attempt, not a fresh three: a lineage whose turns keep failing reports its next result raw
    after one more failure. A successful commit still resets the count (the crash test)."""
    env = await env_factory(**CONT)
    root, got = await _claimed(env)
    await set_intention(env, root.id, attempts=2)  # two failed attempts already
    await record(env, root, generation=1, body="a later result")  # held while deciding: this claim never saw it
    assert await _fail(env, got) == "failed_report"
    fresh = await intention_of(env, "subtask", root.source_id)
    assert (fresh.state, fresh.attempts, fresh.claim_token, fresh.close_reason) == ("result_ready", 3, None, None)
    rows = {r.body: r for r in await inbox_rows(env, UUID(root.source_id))}
    assert rows[RESULT].delivered_at is not None and rows["a later result"].delivered_at is None
    (report,) = await _owner_rows(env)
    assert RESULT in report.body and "a later result" not in report.body
    again = await claim(env, root.id)
    assert again is not None and [r.body for r in again.inbox_rows] == ["a later result"]
    assert await _fail(env, again) == "failed_report"  # one more failure, not three
    fresh = await intention_of(env, "subtask", root.source_id)
    assert (fresh.state, fresh.close_reason, fresh.attempts) == ("closed", "failed_report", 4)
    assert len(await _owner_rows(env)) == 2
