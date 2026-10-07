"""F099 Phase 2e-1: cancel in the store (spec 4.6, T13): the cascade and the proposals under the root lock."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from f099_support import (
    CONT,
    ask_with_proposals,
    claimed,
    commit_ask,
    env_factory,  # noqa: F401
    finish,
    inbox_rows,
    intention_of,
    make_child,
    make_dag,
    make_root,
    proposal_row,
    record,
    set_intention,
    stage,
    until_a_backend_waits_on_a_lock,
)
from sqlalchemy import select, update

from nous.brain import continuation, intentions
from nous.brain.continuation import Resolution
from nous.brain.intentions import IntentionSpec
from nous.storage.models import Intention, IntentionArrival, IntentionProposal, ResultInbox, Subtask

pytestmark = pytest.mark.postgres_only  # FOR NO KEY UPDATE, savepoints, = ANY(array)


async def _cancel(env, root_id, *, reason="test", actor="owner-test"):
    async with env.db.session() as s:
        out = await continuation.cancel_root(s, env.agent, root_id, reason=reason, actor=actor)
        await s.commit()
    return out


async def _row(env, intention_id) -> Intention:
    async with env.db.session() as s:
        return (
            await s.execute(
                select(Intention).where(Intention.id == intention_id).execution_options(populate_existing=True)
            )
        ).scalar_one()


async def _set_proposal(env, proposal_id, **values):
    async with env.db.session() as s:
        await s.execute(update(IntentionProposal).where(IntentionProposal.id == proposal_id).values(**values))
        await s.commit()


async def _decide(env, proposal_id, *, approve=True):
    async with env.db.session() as s:
        out = await continuation.decide_proposal(
            s, env.agent, proposal_id, approve=approve, actor="owner-test", settings=env.settings
        )
        await s.commit()
    return out


async def _claim_execution(env, proposal_id):
    async with env.db.session() as s:
        row = await continuation.claim_execution(s, env.agent, proposal_id)
        await s.commit()
    return row


async def _schedule_container(env):
    """A live recurring schedule and its container intention (a root with the ``container`` policy)."""
    schedule = await env.heart.schedules.create(
        task="watch the snow",
        schedule_type="recurring",
        interval_seconds=1800,
        intention=IntentionSpec(intent="Watch the snow", origin_kind="interactive", container=True),
    )
    container = await intention_of(env, "schedule", schedule.id)
    return schedule, container


def _fire_spec(schedule) -> IntentionSpec:
    return IntentionSpec(
        intent="Snow check",
        origin_kind="scheduler",
        parent_source=("schedule", str(schedule.id)),
        wake_policy="remember",
    )


async def _fire(env, schedule):
    """One schedule fire: a new root under the container, with a pending subtask."""
    st = await env.heart.subtasks.create(task="snow check", intention=_fire_spec(schedule))
    return st, await intention_of(env, "subtask", st.id)


# ---- the cascade ---------------------------------------------------------------------------------------------


async def test_a_cancel_moves_the_whole_lineage_and_stops_its_work(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    root = await make_root(env)
    child = await make_child(env, root)
    other = await make_root(env)  # another lineage: untouched
    async with env.db.session() as s:  # the usual case for an owner cancel: the work is running (2e-1 review m3)
        await s.execute(update(Subtask).where(Subtask.id == uuid.UUID(child.source_id)).values(status="running"))
        await s.commit()

    out = await _cancel(env, root.id)

    assert (out.already_cancelled, out.cancelled_intentions, out.cancelled_subtasks) == (False, 2, 2)
    assert out.root_ids == (root.id,) and out.dag_ids == () and out.proposal_ids == ()
    for fresh in (await _row(env, root.id), await _row(env, child.id)):
        assert (fresh.state, fresh.close_reason, fresh.claim_token) == ("cancelled", "cancelled", None)
        assert fresh.closed_at is not None
    assert (await _row(env, root.id)).root_cancelled_at is not None
    assert (await _row(env, child.id)).root_cancelled_at is None  # the marker lives on the root row only
    for source_id in (root.source_id, child.source_id):
        stopped = await env.heart.subtasks.get(uuid.UUID(source_id))
        assert (stopped.status, stopped.final_outcome) == ("cancelled", "cancelled") and stopped.completed_at
    assert (await _row(env, other.id)).state == "pending"
    assert (await env.heart.subtasks.get(uuid.UUID(other.source_id))).status == "pending"


async def test_a_cancel_leaves_a_finished_subtask_and_its_result_as_they_were(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    root = await make_root(env)
    child = await make_child(env, root)
    done = await env.heart.subtasks.get(uuid.UUID(child.source_id))
    await env.heart.subtasks.complete(done.id, "40 cm", final_outcome="completed")
    out = await _cancel(env, root.id)
    assert out.cancelled_subtasks == 1  # the root's own subtask, which was still pending
    assert (await env.heart.subtasks.get(done.id)).status == "completed"


async def test_a_cancel_clears_a_live_claim_so_the_turns_commit_loses_its_fence(env_factory):  # noqa: F811
    """Review Focus 3: the claim token is cleared in the cancel, and everything a fenced write needs goes with it."""
    env = await env_factory(**CONT)
    root, got = await claimed(env)
    pid = await stage(env, got)
    live = await intention_of(env, "subtask", root.source_id)
    assert live.state == "deciding" and live.claim_token is not None

    out = await _cancel(env, root.id)

    assert out.cancelled_intentions == 1 and out.cancelled_proposals == 1
    assert (await _row(env, root.id)).claim_token is None  # the token is cleared (2e-1 review m4)
    assert (await proposal_row(env, pid)).state == "cancelled"
    async with env.db.session() as s:
        done = await continuation.commit_arrival(
            s,
            env.agent,
            got,
            resolution=Resolution("drop", "gone", False, 0.5),
            outcome="resolved",
            settings=env.settings,
        )
        await s.commit()
    assert done is None  # the fence: nothing of the turn is written
    async with env.db.session() as s:
        arrivals = (await s.execute(select(IntentionArrival).where(IntentionArrival.root_id == root.id))).all()
    assert arrivals == []


async def test_a_cancel_stamps_the_unread_results_and_reports_nothing(env_factory):  # noqa: F811
    """The unified late-result rule, the cancel half: a cancelled root says nothing it has not said."""
    env = await env_factory(**CONT)
    root = await make_root(env)
    await record(env, root)
    (unread,) = [r for r in await inbox_rows(env) if r.intention_id == root.id]
    assert unread.delivered_at is None

    await _cancel(env, root.id)

    rows = await inbox_rows(env)
    (stamped,) = [r for r in rows if r.intention_id == root.id]
    assert stamped.delivered_at is not None and stamped.delivered_session_id == f"intent-{root.id}"
    assert [r for r in rows if r.source_kind == "intention_report"] == []


async def test_a_cancel_cancels_every_proposal_that_could_still_start(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    asked = await ask_with_proposals(env, count=4)
    pending, approved, staged, executing = asked.ids
    await _decide(env, approved, approve=True)
    await _set_proposal(env, staged, state="staged")
    await _set_proposal(env, executing, state="executing")

    out = await _cancel(env, asked.root.id)

    assert out.cancelled_proposals == 3 and set(out.proposal_ids) == {pending, approved}  # staged was never shown
    assert [(await proposal_row(env, p)).state for p in (pending, approved, staged, executing)] == [
        "cancelled",
        "cancelled",
        "cancelled",
        "executing",  # the call has started: a cancel cannot take it back
    ]
    assert (await proposal_row(env, approved)).decided_by == "system"


async def test_a_cancel_reports_the_running_dags_and_not_the_finished_ones(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    root = await make_root(env)
    running, _store = await make_dag(env, status="running", parent=root)
    done, _store = await make_dag(env, status="completed", parent=root)
    out = await _cancel(env, root.id)
    assert out.dag_ids == (running.id,) and done.id not in out.dag_ids
    assert (await intention_of(env, "dag", running.id)).state == "cancelled"


async def test_a_finished_root_is_refused_and_nothing_is_written(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    root = await make_root(env)
    await finish(env, await env.heart.subtasks.get(uuid.UUID(root.source_id)))
    await set_intention(env, root.id, state="closed", close_reason="resolved", closed_at=datetime.now(UTC))
    with pytest.raises(continuation.CancelRefused) as refused:
        await _cancel(env, root.id)
    assert refused.value.reason == continuation.REFUSE_FINISHED
    assert (await _row(env, root.id)).root_cancelled_at is None  # no marker on a finished root


async def test_a_repeated_cancel_is_allowed_and_says_so(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    root = await make_root(env)
    first = await _cancel(env, root.id)
    again = await _cancel(env, root.id)
    assert (first.already_cancelled, again.already_cancelled) == (False, True)
    assert (again.cancelled_intentions, again.cancelled_subtasks) == (0, 0)
    stamp = (await _row(env, root.id)).root_cancelled_at
    await _cancel(env, root.id)
    assert (await _row(env, root.id)).root_cancelled_at == stamp  # the marker is written once


async def test_only_a_root_can_be_cancelled(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    root = await make_root(env)
    child = await make_child(env, root)
    for bad in (child.id, uuid.uuid4()):
        with pytest.raises(continuation.RootNotFound):
            await _cancel(env, bad)
    assert (await _row(env, child.id)).state == "pending"


async def test_a_child_id_is_refused_before_any_lock_is_taken(env_factory):  # noqa: F811
    """2e-1 review m2: a child's lock without its root's is the order the one lock order forbids, so the refusal
    takes none (the caller's transaction is still open here, and another session can lock the child at once)."""
    env = await env_factory(**CONT)
    root = await make_root(env)
    child = await make_child(env, root)
    async with env.db.session() as caller:
        with pytest.raises(continuation.RootNotFound):
            await continuation.cancel_root(caller, env.agent, child.id, reason="t", actor="t")
        async with env.db.session() as other:
            locked = await other.execute(
                select(Intention.id).where(Intention.id == child.id).with_for_update(key_share=True, nowait=True)
            )
            assert locked.scalar_one() == child.id
            await other.rollback()
        await caller.rollback()


async def test_the_view_of_cancelled_roots_and_the_root_lookup(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    one, two, kept = await make_root(env), await make_root(env), await make_root(env)
    child = await make_child(env, one)
    await _cancel(env, one.id)
    await _cancel(env, two.id)
    async with env.db.session() as s:
        every = await continuation.cancelled_root_ids(s, env.agent)
        recent = await continuation.cancelled_root_ids(s, env.agent, since=datetime.now(UTC) + timedelta(seconds=5))
        found = await continuation.find_root_id(s, env.agent, one.id.hex[:10])
        missing_child = await continuation.find_root_id(s, env.agent, child.id.hex[:12])
        nonsense = await continuation.find_root_id(s, env.agent, "not hex")
    assert set(every) == {one.id, two.id} and kept.id not in every and recent == []
    assert (found, missing_child, nonsense) == (one.id, None, None)


async def test_a_cancel_closes_the_lineages_unsent_owner_rows_so_nothing_is_pushed_or_claimed(env_factory):  # noqa: F811
    """M1 of the plan review. An owner-facing row (REPORT, QUESTION, PROPOSAL) is keyed to a channel, so the stamp of
    the intention-keyed rows does not reach it: a push that quiet hours deferred would go out for cancelled work, and
    F098's chat claim would read it into the next chat turn."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    from f099_support import CHAN, commit_ask

    from nous.handlers.continuation_publisher import OwnerPublisher

    env = await env_factory(**CONT, telegram_bot_token="test-token")
    root, got = await claimed(env)
    await commit_ask(env, got)
    (question,) = [r for r in await inbox_rows(env) if r.msg_type == "QUESTION"]
    later = datetime.now(UTC) + timedelta(hours=1)
    async with env.db.session() as s:
        await s.execute(update(ResultInbox).where(ResultInbox.id == question.id).values(push_after=later))
        report_id = await continuation.insert_report(
            s,
            env.agent,
            kind="REPORT",
            title="Update",
            body="already on Telegram",
            channel=CHAN,
            intention_id=root.id,
            root_id=root.id,
            push_after=datetime.now(UTC) - timedelta(minutes=5),
        )
        await s.execute(
            update(ResultInbox)
            .where(ResultInbox.source_id == report_id)
            .values(pushed_at=datetime.now(UTC), push_message_id=7)
        )
        await s.commit()

    pushed_before = await _stored_pushed_at(env, report_id)
    await _cancel(env, root.id)

    rows = {r.msg_type: r for r in await inbox_rows(env) if r.source_kind == "intention_report"}
    assert rows["QUESTION"].delivered_session_id == continuation.SILENT_SESSION_ID and rows["QUESTION"].pushed_at
    assert rows["QUESTION"].push_message_id is None  # a reply to a message that was never sent resolves to nothing
    assert rows["REPORT"].delivered_session_id == continuation.SILENT_SESSION_ID
    assert rows["REPORT"].pushed_at == pushed_before  # a push that happened keeps its time
    http = MagicMock()
    http.post = AsyncMock(return_value=SimpleNamespace(status_code=200, json=lambda: {"result": {"message_id": 9}}))
    publisher = OwnerPublisher(database=env.db, settings=env.settings, http_client=http)
    assert await publisher.push_due(now=later + timedelta(seconds=1)) == 0
    http.post.assert_not_called()  # the deferred question is never pushed
    claimed_rows, _ = await env.heart.result_inbox.claim(channel=CHAN, session_id="S9", max_age_hours=72, max_items=10)
    assert claimed_rows == []  # and no chat turn reads it either


async def _stored_pushed_at(env, source_id):
    async with env.db.session() as s:
        return (await s.execute(select(ResultInbox.pushed_at).where(ResultInbox.source_id == source_id))).scalar_one()


# ---- containers and fires ------------------------------------------------------------------------------------


async def test_a_cancelled_container_deactivates_its_schedule_and_cancels_its_fires(env_factory):  # noqa: F811
    """Each fire is its own root, so the root cascade alone would not reach it (spec 4.6)."""
    env = await env_factory(**CONT)
    schedule, container = await _schedule_container(env)
    st, fire = await _fire(env, schedule)
    assert fire.root_id == fire.id and fire.parent_id == container.id
    _closed_st, closed_fire = await _fire(env, schedule)
    await set_intention(env, closed_fire.id, state="closed", close_reason="delivered", closed_at=datetime.now(UTC))

    out = await _cancel(env, container.id)

    assert out.deactivated_schedules == 1 and set(out.root_ids) == {container.id, fire.id}
    assert (await env.heart.schedules.get(schedule.id)).active is False
    assert (await _row(env, container.id)).state == "cancelled"
    cancelled = await _row(env, fire.id)
    assert (cancelled.state, cancelled.root_cancelled_at is not None) == ("cancelled", True)
    assert (await env.heart.subtasks.get(st.id)).status == "cancelled"
    assert (await _row(env, closed_fire.id)).root_cancelled_at is None  # a fire that was done is left alone


async def test_a_fire_in_flight_is_part_of_the_cancel(env_factory):  # noqa: F811
    """Lock order container, schedule: a fire holds both FOR SHARE, so the cancel waits and then finds its root."""
    env = await env_factory(**CONT)
    schedule, container = await _schedule_container(env)
    async with env.db.session() as firing:
        prepared = await intentions.prepare_intention(firing, env.agent, _fire_spec(schedule))
        await intentions.insert_prepared(firing, env.agent, prepared, source_kind="subtask", source_id=uuid.uuid4())
        cancel = asyncio.create_task(_cancel(env, container.id))
        try:
            await asyncio.wait_for(until_a_backend_waits_on_a_lock(env), timeout=10)
        finally:
            await firing.commit()
    out = await asyncio.wait_for(cancel, timeout=30)
    assert prepared.id in out.root_ids
    assert (await _row(env, prepared.id)).root_cancelled_at is not None


async def test_a_cancel_in_flight_refuses_a_fire(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    schedule, container = await _schedule_container(env)
    async with env.db.session() as canceller:
        await continuation.cancel_root(canceller, env.agent, container.id, reason="t", actor="t")
        fire = asyncio.create_task(_fire(env, schedule))
        try:
            await asyncio.wait_for(until_a_backend_waits_on_a_lock(env), timeout=10)
        finally:
            await canceller.commit()
    with pytest.raises(intentions.IntentionRootClosed):
        await asyncio.wait_for(fire, timeout=30)
    assert (await env.heart.schedules.get(schedule.id)).active is False


async def test_a_container_inside_a_lineage_is_cancelled_with_it(env_factory):  # noqa: F811
    """A lineage that scheduled something: the container is a child row of the root, and its fires are other roots."""
    env = await env_factory(**CONT)
    root = await make_root(env)
    schedule = await env.heart.schedules.create(
        task="inner",
        schedule_type="recurring",
        interval_seconds=1800,
        intention=IntentionSpec(
            intent="Inner schedule",
            origin_kind="continuation",
            container=True,
            parent_id=root.id,
            origin_authority="internal_only",
        ),
    )
    _st, fire = await _fire(env, schedule)
    out = await _cancel(env, root.id)
    assert out.deactivated_schedules == 1 and set(out.root_ids) == {root.id, fire.id}
    assert (await env.heart.schedules.get(schedule.id)).active is False


async def _lock_intention(session, intention_id):
    await session.execute(select(Intention.id).where(Intention.id == intention_id).with_for_update(key_share=True))


async def test_a_cancel_takes_the_roots_of_nested_fires_in_the_one_order_so_a_sweep_cannot_deadlock_it(env_factory):  # noqa: F811
    """2e-1 review I1. Container C has fires F1 and F2; F1's lineage scheduled an inner container whose fire is G,
    made between them. The one cross-root order is (created_at, id): F1, G, F2. A sweep holds G and then wants F2;
    a cancel that took F2 before G (level by level) would hold F2 and wait on G, and Postgres would abort one side."""
    env = await env_factory(**CONT)
    schedule, container = await _schedule_container(env)
    _st1, f1 = await _fire(env, schedule)
    inner = await env.heart.schedules.create(
        task="inner",
        schedule_type="recurring",
        interval_seconds=1800,
        intention=IntentionSpec(
            intent="Inner schedule",
            origin_kind="continuation",
            container=True,
            parent_id=f1.id,
            origin_authority="internal_only",
        ),
    )
    _stg, g = await _fire(env, inner)
    _st2, f2 = await _fire(env, schedule)
    assert (f1.created_at, f1.id) < (g.created_at, g.id) < (f2.created_at, f2.id)
    outcomes: dict[str, str] = {}

    async def cancel():
        try:
            await _cancel(env, container.id)
            outcomes["cancel"] = "ok"
        except Exception as exc:  # the probe records what Postgres did to each side
            outcomes["cancel"] = str(exc)

    async with env.db.session() as sweep:
        await _lock_intention(sweep, g.id)
        task = asyncio.create_task(cancel())
        try:
            await asyncio.wait_for(until_a_backend_waits_on_a_lock(env), timeout=10)  # the cancel queues on G
            try:
                await asyncio.wait_for(_lock_intention(sweep, f2.id), timeout=30)
                outcomes["sweep"] = "ok"
            except Exception as exc:
                outcomes["sweep"] = str(exc)
        finally:
            await sweep.rollback()
    await asyncio.wait_for(task, timeout=30)
    assert outcomes == {"sweep": "ok", "cancel": "ok"}
    for fire in (f1, g, f2):
        assert (await _row(env, fire.id)).root_cancelled_at is not None


async def test_a_fire_that_ended_while_the_cancel_waited_for_it_gets_no_marker(env_factory):  # noqa: F811
    """2e-1 review m1 (E16). The cancel reads the open fires before it locks them. A fire the TTL sweep expired in
    between has nothing left to cancel, so it gets no marker: a marker would turn its later result silent, and an
    expired root's late result is reported raw."""
    env = await env_factory(**CONT)
    schedule, container = await _schedule_container(env)
    st, fire = await _fire(env, schedule)
    await env.heart.subtasks.complete(st.id, "done", final_outcome="completed")  # nothing of it runs any more
    now = datetime.now(UTC)
    async with env.db.session() as sweep:  # the expiry, not yet committed: the cancel still reads the fire as open
        await sweep.execute(
            update(Intention)
            .where(Intention.id == fire.id)
            .values(state="expired", close_reason="expired", closed_at=now, root_expired_at=now)
        )
        expiry_report = await continuation.insert_report(  # what the expiry tells the owner
            sweep,
            env.agent,
            kind="REPORT",
            title="Expired",
            body="the snow check expired",
            channel="telegram:8080",
            intention_id=fire.id,
            root_id=fire.id,
            push_after=now,
        )
        task = asyncio.create_task(_cancel(env, container.id))
        try:
            await asyncio.wait_for(until_a_backend_waits_on_a_lock(env), timeout=10)
        finally:
            await sweep.commit()
    out = await asyncio.wait_for(task, timeout=30)
    assert out.root_ids == (container.id,)
    fresh = await _row(env, fire.id)
    assert (fresh.state, fresh.root_cancelled_at) == ("expired", None)
    (report,) = await inbox_rows(env, expiry_report)
    assert report.delivered_at is None  # the cancel does not silence what the expiry says


# ---- the races ------------------------------------------------------------------------------------------------


async def test_a_cancel_not_yet_committed_stops_an_approved_call_from_starting(env_factory):  # noqa: F811
    """2d-3 review m1, a hard requirement. claim_execution's root-open predicate sees only a COMMITTED marker, so the
    cancel must move the proposal in its own transaction: the claim then waits on the row and fails its re-check."""
    env = await env_factory(**CONT)
    asked = await ask_with_proposals(env)
    (pid,) = asked.ids
    await _decide(env, pid, approve=True)
    async with env.db.session() as holder:
        await continuation.cancel_root(holder, env.agent, asked.root.id, reason="t", actor="t")  # uncommitted
        claim = asyncio.create_task(_claim_execution(env, pid))
        try:
            await asyncio.wait_for(until_a_backend_waits_on_a_lock(env), timeout=10)
        finally:
            await holder.commit()
    assert await asyncio.wait_for(claim, timeout=30) is None
    assert (await proposal_row(env, pid)).state == "cancelled"


async def test_an_expiry_not_yet_committed_stops_an_approved_call_from_starting(env_factory):  # noqa: F811
    """The same window in _expire_root: its marker and its proposals move in one transaction under the root lock."""
    env = await env_factory(**CONT)
    asked = await ask_with_proposals(env, count=2)
    approved, pending = asked.ids
    await _decide(env, approved, approve=True)
    later = datetime.now(UTC) + timedelta(hours=100)
    async with env.db.session() as holder:
        expired = await continuation.expire_roots(
            holder, env.agent, ttl_hours=env.settings.intention_root_ttl_hours, settings=env.settings, now=later
        )
        assert expired == [asked.root.id]
        claim = asyncio.create_task(_claim_execution(env, approved))
        try:
            await asyncio.wait_for(until_a_backend_waits_on_a_lock(env), timeout=10)
        finally:
            await holder.commit()
    assert await asyncio.wait_for(claim, timeout=30) is None
    assert [(await proposal_row(env, p)).state for p in (approved, pending)] == ["expired", "expired"]
    for proposal_id in (approved, pending):  # S3: the proposals sweep used to write these; the expiry does now
        row = await proposal_row(env, proposal_id)
        assert (row.decided_by, row.decided_at is not None) == ("system", True)


async def test_the_expiry_names_the_proposals_it_ended_for_the_bus_and_not_the_ones_never_shown(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    asked = await ask_with_proposals(env, count=2)
    shown, never_shown = asked.ids
    await _set_proposal(env, never_shown, state="staged")
    ended: list = []
    async with env.db.session() as s:
        await continuation.expire_roots(
            s,
            env.agent,
            ttl_hours=env.settings.intention_root_ttl_hours,
            settings=env.settings,
            now=datetime.now(UTC) + timedelta(hours=100),
            proposals_out=ended,
        )
        await s.commit()
    assert ended == [(shown, "expired")]
    assert (await proposal_row(env, never_shown)).state == "expired"  # ended, but the owner never saw it


async def test_a_cancel_holding_the_root_does_not_deadlock_the_stale_staged_sweep(env_factory, caplog):  # noqa: F811
    """2e-1 review m5. The cancel (and ``_expire_root``) hold the root and then move the lineage's ``staged`` rows. The
    proposals sweep's stale-staged step used to move ``staged`` rows holding no root and then lock roots for its
    ``pending`` arm: hold-staged-want-root against hold-root-want-staged. It now takes each stale row's root first, so
    it waits for the cancel and finds the row already cancelled."""
    env = await env_factory(**CONT)
    asked = await ask_with_proposals(env, count=2)
    stale, due = asked.ids
    await _set_proposal(env, stale, state="staged", created_at=datetime.now(UTC) - timedelta(hours=1))
    await _set_proposal(env, due, deadline=datetime.now(UTC) - timedelta(minutes=1))  # the pending arm wants the root

    async def sweep():
        async with env.db.session() as s:
            moved = await continuation.expire_proposals(s, env.agent, settings=env.settings)
            await s.commit()
        return moved

    async with env.db.session() as canceller:
        await _lock_intention(canceller, asked.root.id)  # the cancel's first statement
        task = asyncio.create_task(sweep())
        try:
            await asyncio.wait_for(until_a_backend_waits_on_a_lock(env), timeout=10)  # the sweep queues on the root
            await asyncio.wait_for(
                continuation.cancel_root(canceller, env.agent, asked.root.id, reason="t", actor="t"), timeout=30
            )
        finally:
            await canceller.commit()
    assert await asyncio.wait_for(task, timeout=30) == []  # the cancel ended both, the sweep found nothing left
    assert [(await proposal_row(env, p)).state for p in (stale, due)] == ["cancelled", "cancelled"]
    assert "deadlock" not in caplog.text


async def test_a_spawn_in_flight_makes_the_cancel_wait_and_is_cancelled_with_the_lineage(env_factory):  # noqa: F811
    """I1: a spawn reads the root FOR SHARE, which conflicts with the cancel's FOR NO KEY UPDATE. The cancel waits,
    then reads the lineage and finds the new child: nothing a spawn in flight made escapes it."""
    env = await env_factory(**CONT)
    root = await make_root(env)
    spec = IntentionSpec(
        intent="next step", origin_kind="continuation", parent_id=root.id, origin_authority="internal_only"
    )
    async with env.db.session() as spawning:
        prepared = await intentions.prepare_intention(spawning, env.agent, spec)
        await intentions.insert_prepared(spawning, env.agent, prepared, source_kind="subtask", source_id=uuid.uuid4())
        cancel = asyncio.create_task(_cancel(env, root.id))
        try:
            await asyncio.wait_for(until_a_backend_waits_on_a_lock(env), timeout=10)
        finally:
            await spawning.commit()
    out = await asyncio.wait_for(cancel, timeout=30)
    assert out.cancelled_intentions == 2
    assert (await _row(env, prepared.id)).state == "cancelled"


async def test_a_cancel_in_flight_refuses_a_spawn(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    root = await make_root(env)
    async with env.db.session() as canceller:
        await continuation.cancel_root(canceller, env.agent, root.id, reason="t", actor="t")
        spawn = asyncio.create_task(make_child(env, root))
        try:
            await asyncio.wait_for(until_a_backend_waits_on_a_lock(env), timeout=10)
        finally:
            await canceller.commit()
    with pytest.raises(intentions.IntentionRootClosed):
        await asyncio.wait_for(spawn, timeout=30)


async def test_a_decision_after_a_cancel_is_refused_as_ended(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    asked = await ask_with_proposals(env)
    (pid,) = asked.ids
    await _cancel(env, asked.root.id)
    out = await _decide(env, pid, approve=True)
    assert (out.state, out.changed, out.refusal) == ("cancelled", False, "ended")
    assert await _claim_execution(env, pid) is None


async def test_an_answer_after_a_cancel_is_refused_and_writes_nothing(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    root, got = await claimed(env)
    await commit_ask(env, got)
    (question,) = [r for r in await inbox_rows(env) if r.msg_type == "QUESTION"]
    await _cancel(env, root.id)
    before = len(await inbox_rows(env))
    async with env.db.session() as s:
        with pytest.raises(continuation.AnswerRefused) as refused:
            await continuation.record_answer(
                s, env.agent, question.source_id, text="yes", actor="t", settings=env.settings
            )
    assert refused.value.reason == "ended" and len(await inbox_rows(env)) == before


async def test_a_claim_after_a_cancel_finds_nothing_to_claim(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    root = await make_root(env)
    await record(env, root)
    await _cancel(env, root.id)
    from f099_support import claim

    assert await claim(env, root.id) is None


async def test_cancel_and_ask_leave_a_woken_arrival_nothing_to_wake(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    root, got = await claimed(env)
    await commit_ask(env, got)  # a question: awaiting_owner
    assert (await intention_of(env, "subtask", root.source_id)).state == "awaiting_owner"
    await _cancel(env, root.id)
    async with env.db.session() as s:
        woken = await continuation.wake_terminal_arrivals(s, env.agent, settings=env.settings)
        await s.commit()
    assert woken == []
    assert (await intention_of(env, "subtask", root.source_id)).state == "cancelled"
