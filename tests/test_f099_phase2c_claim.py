"""F099 Phase 2c-1: the claim (spec 4.5.2): the per-root lock, debounce, max-wait, the batch."""

from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta

import pytest
from f099_support import (
    CHAN,
    CONT,
    age,
    claim,
    eligible,
    env_factory,  # noqa: F401
    intention_of,
    make_child,
    make_root,
    record,
    set_intention,
    until_a_backend_waits_on_a_lock,
)
from sqlalchemy import select, update

from nous.brain import continuation
from nous.storage.models import Intention, ResultInbox

pytestmark = pytest.mark.postgres_only  # FOR NO KEY UPDATE, make_interval, per-root locking


async def test_a_result_ready_intention_is_claimed_with_its_unconsumed_rows(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    root = await make_root(env)
    await record(env, root)
    got = await claim(env, root.id)
    assert got is not None and got.root_id == root.id and got.deepest.id == root.id
    assert [i.state for i in got.intentions] == ["deciding"]
    (row,) = got.inbox_rows
    assert (row.intention_id, row.channel, row.session_id, row.delivered_at) == (root.id, None, None, None)
    fresh = await intention_of(env, "subtask", root.source_id)
    assert (fresh.state, fresh.claim_token) == ("deciding", got.claim_token) and fresh.claimed_at is not None


async def test_a_claim_stamps_nothing_delivered(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    root = await make_root(env)
    await record(env, root)
    await claim(env, root.id)
    async with env.db.session() as s:
        rows = (await s.execute(select(ResultInbox).where(ResultInbox.agent_id == env.agent))).scalars().all()
    assert [r.delivered_at for r in rows] == [None]  # only the fenced commit stamps delivery


async def test_the_debounce_holds_a_fresh_result(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    root = await make_root(env)
    await record(env, root)  # result_at = now
    assert await claim(env, root.id, debounce=60, max_wait=600) is None
    assert (await intention_of(env, "subtask", root.source_id)).state == "result_ready"
    await age(env, root.id, seconds=61)
    assert await claim(env, root.id, debounce=60, max_wait=600) is not None


async def test_max_wait_claims_a_batch_whose_newest_member_is_still_fresh(env_factory):  # noqa: F811
    """Anti-starvation: results keep arriving, so the newest never ages past the debounce, but the
    oldest has waited the cap: the whole root is claimed."""
    env = await env_factory(**CONT)
    root = await make_root(env)
    child = await make_child(env, root)
    await record(env, root)
    await record(env, child)
    await age(env, root.id, seconds=700)  # the child's result is fresh
    got = await claim(env, root.id, debounce=60, max_wait=600)
    assert got is not None and {i.id for i in got.intentions} == {root.id, child.id}


async def test_a_fan_out_is_one_batch_and_its_deepest_member_is_the_parent(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    root = await make_root(env)
    first = await make_child(env, root)
    second = await make_child(env, first)  # depth 2
    for it in (root, first, second):
        await record(env, it)
    got = await claim(env, root.id)
    assert {i.id for i in got.intentions} == {root.id, first.id, second.id}
    assert got.deepest.id == second.id and got.deepest.depth == 2
    assert len(got.inbox_rows) == 3


async def test_the_deepest_tie_goes_to_the_earliest_created(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    root = await make_root(env)
    early = await make_child(env, root)
    late = await make_child(env, root)
    await record(env, early)
    await record(env, late)
    got = await claim(env, root.id)
    assert got.deepest.id == early.id


async def test_a_report_intention_is_never_claimed(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    root = await make_root(env, policy="report")
    await set_intention(env, root.id, state="result_ready", result_at=root.created_at)  # forced: never reached by code
    assert await claim(env, root.id) is None


async def test_the_claim_reads_only_the_undelivered_intention_keyed_rows(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    root = await make_root(env)
    await record(env, root, generation=0, body="first")
    async with env.db.session() as s:  # generation 0 was consumed by an earlier arrival
        await s.execute(
            update(ResultInbox).where(ResultInbox.intention_id == root.id).values(delivered_at=root.created_at)
        )
        await continuation.insert_report(  # an owner-facing row of the same intention is chat's
            s,
            env.agent,
            kind=continuation.MSG_REPORT,
            title="r",
            body="b",
            channel=CHAN,
            intention_id=root.id,
            root_id=root.id,
        )
        await s.commit()
    await record(env, root, generation=1, body="second")  # root is result_ready already: inserted and held
    got = await claim(env, root.id)
    assert [r.body for r in got.inbox_rows] == ["second"]


async def test_a_claim_waits_for_the_root_lock_and_sees_the_claim_that_committed_first(env_factory):  # noqa: F811
    """The per-root mutex, deterministically. Claimer A has updated the root but not committed; a
    second intention of the root becomes ready in its own transaction; claimer B must wait for A,
    and then see A's `deciding` row. Without the lock B's UPDATE would not block, its snapshot would
    not contain A's uncommitted row, and it would claim the second intention too."""
    env = await env_factory(**CONT)
    root = await make_root(env)
    second = await make_child(env, root)
    await record(env, root)
    async with env.db.session() as a:
        claim_a = await continuation.claim_root(a, env.agent, root.id, token=uuid.uuid4(), debounce_s=0, max_wait_s=0)
        assert claim_a is not None and [i.id for i in claim_a.intentions] == [root.id]
        await record(env, second)  # committed on its own: ready, and invisible to A's UPDATE
        rival = asyncio.create_task(claim(env, root.id))
        try:
            await asyncio.wait_for(until_a_backend_waits_on_a_lock(env), timeout=10)
        finally:
            await a.commit()  # always release the root
    assert await asyncio.wait_for(rival, timeout=30) is None
    assert (await intention_of(env, "subtask", second.source_id)).state == "result_ready"
    assert (await intention_of(env, "subtask", root.source_id)).state == "deciding"


async def test_two_claimers_blocked_on_one_root_make_exactly_one_claim(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    root = await make_root(env)
    await record(env, root)
    async with env.db.session() as holder:
        await holder.execute(select(Intention.id).where(Intention.id == root.id).with_for_update(key_share=True))
        first = asyncio.create_task(claim(env, root.id))
        second = asyncio.create_task(claim(env, root.id))
        try:
            await asyncio.wait_for(until_a_backend_waits_on_a_lock(env, at_least=2), timeout=10)
        finally:
            await holder.commit()
    results = await asyncio.wait_for(asyncio.gather(first, second), timeout=30)
    assert sorted(r is None for r in results) == [False, True]  # whichever order: exactly one claim


async def test_a_missing_root_claims_nothing(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    assert await claim(env, uuid.uuid4()) is None


async def test_eligible_roots_say_when_each_root_becomes_claimable(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    a = await make_root(env)
    b = await make_root(env)
    await record(env, a)
    await record(env, b)
    await age(env, b.id, seconds=100)
    due = await eligible(env, debounce=60, max_wait=600)
    assert [root for root, _ in due] == [b.id, a.id]  # b was due 40 s ago, a is due in about 60 s
    a_result = (await intention_of(env, "subtask", a.source_id)).result_at
    assert abs((dict(due)[a.id] - (a_result + timedelta(seconds=60))).total_seconds()) < 1


async def test_eligible_roots_leave_out_a_root_that_is_deciding(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    root = await make_root(env)
    second = await make_child(env, root)
    await record(env, root)
    assert (await claim(env, root.id)) is not None
    await record(env, second)  # ready, but its root is deciding
    assert await eligible(env) == []


async def test_eligible_roots_keep_the_earliest_due_under_the_limit(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    roots = [await make_root(env) for _ in range(3)]
    for root, seconds in zip(roots, (10, 300, 100), strict=True):
        await record(env, root)
        await age(env, root.id, seconds=seconds)
    due = await eligible(env, debounce=60, max_wait=600, limit=2)
    assert [root for root, _ in due] == [roots[1].id, roots[2].id]  # due 240 s and 40 s ago; roots[0] in 50 s


async def test_a_claim_without_a_due_result_is_cheap_and_leaves_no_trace(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    root = await make_root(env)  # pending: nothing arrived
    assert await claim(env, root.id) is None
    fresh = await intention_of(env, "subtask", root.source_id)
    assert (fresh.state, fresh.claim_token, fresh.claimed_at) == ("pending", None, None)
