"""F099 Phase 2c-1: repair_missing_results (the four carry-over residuals) and ruling R1."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from f099_support import (
    CHAN,
    CONT,
    ON,
    claim,
    env_factory,  # noqa: F401
    finish,
    inbox_rows,
    intention_of,
    make_dag,
    make_root,
    make_subtask,
    record,
    set_intention,
)
from sqlalchemy import select, update

from nous.brain import continuation
from nous.config import Settings
from nous.heart import result_reconciler
from nous.heart.result_inbox import record_subtask_result, route_result
from nous.heart.result_reconciler import repair_missing_results
from nous.storage.models import ExecutionDAG, Intention, Subtask

pytestmark = pytest.mark.postgres_only  # CAST(text AS uuid) joins


async def _repair(env, settings=None):
    return await repair_missing_results(env.db, env.heart.result_inbox, settings or env.settings, limit=50)


async def _owner_rows(env):
    return [r for r in await inbox_rows(env) if r.source_kind == "intention_report"]


async def _age_subtask(env, subtask_id, hours):
    async with env.db.session() as s:
        await s.execute(
            update(Subtask)
            .where(Subtask.id == subtask_id)
            .values(completed_at=datetime.now(UTC) - timedelta(hours=hours))
        )
        await s.commit()


# ---- (a) a result nobody wrote ---------------------------------------------------------------------------


async def test_an_unrouted_report_whose_hook_raised_is_written_to_the_owner_channel(env_factory):  # noqa: F811
    env = await env_factory(**CONT, telegram_chat_id="8080")
    st = await make_subtask(env, policy="report", routed=False)
    await finish(env, st)  # the worker hook raised: no row, and the close pass leaves a report alone
    assert (await intention_of(env, "subtask", st.id)).state == "pending"
    assert await _repair(env) == 1
    (row,) = await inbox_rows(env, st.id)
    assert (row.channel, row.msg_type) == (CHAN, "INFORM") and "Powder" in row.body  # the default chat
    fresh = await intention_of(env, "subtask", st.id)
    assert (fresh.state, fresh.close_reason) == ("closed", "delivered")
    assert await _repair(env) == 0  # idempotent


async def test_an_unrouted_report_with_nowhere_to_go_closes_legacy_with_no_row(env_factory):  # noqa: F811
    env = await env_factory(**CONT)  # no origin channel and no default chat
    st = await make_subtask(env, policy="report", routed=False)
    await finish(env, st)
    assert await _repair(env) == 0  # closed, but nothing was written: not counted as a repair
    assert await inbox_rows(env, st.id) == []
    fresh = await intention_of(env, "subtask", st.id)
    assert (fresh.state, fresh.close_reason) == ("closed", "legacy")  # not 'delivered': nothing was


async def test_a_continue_result_whose_write_was_lost_is_written_and_woken(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    st = await make_subtask(env, policy="continue")
    await finish(env, st)
    assert await _repair(env) == 1
    (row,) = await inbox_rows(env, st.id)
    assert (row.channel, row.session_id, row.intention_id) == (
        None,
        None,
        (await intention_of(env, "subtask", st.id)).id,
    )
    assert (await intention_of(env, "subtask", st.id)).state == "result_ready"


async def test_a_failed_continue_subtask_is_repaired_with_its_failure(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    st = await make_subtask(env, policy="continue")
    await finish(env, st, "fail")
    await _repair(env)
    (row,) = await inbox_rows(env, st.id)
    assert row.msg_type == "FAILURE" and (await intention_of(env, "subtask", st.id)).state == "result_ready"


async def test_a_none_or_remember_intention_is_not_the_repairs_business(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    quiet = await make_subtask(env, policy="none")
    remembered = await make_subtask(env, policy="remember")
    await finish(env, quiet)
    await finish(env, remembered)
    assert await _repair(env) == 0
    assert (await intention_of(env, "subtask", quiet.id)).state == "pending"  # the close pass owns these


# ---- (b) a retried DAG whose Phase 2 write was lost ------------------------------------------------------


async def _retried_dag(env):
    """A continue DAG that Phase 1 closed as legacy and that was then retried (generation 1, delivered by F087)."""
    dag, _store = await make_dag(env, policy="continue", status="completed")
    root = await intention_of(env, "dag", dag.id)
    await set_intention(env, root.id, state="closed", close_reason="legacy")
    async with env.db.session() as s:
        now = datetime.now(UTC)
        await s.execute(
            update(ExecutionDAG)
            .where(ExecutionDAG.id == dag.id)
            .values(delivery_generation=1, delivered_at=now, completed_at=now)
        )
        await s.commit()
    return dag, root


async def test_a_retried_dag_whose_phase_2_write_was_lost_is_reported(env_factory):  # noqa: F811
    env = await env_factory(**CONT, telegram_chat_id="8080")
    dag, root = await _retried_dag(env)
    assert await _repair(env) == 1
    rows = await inbox_rows(env, dag.id)
    assert [(r.source_generation, r.channel, r.delivered_at is not None) for r in rows] == [
        (1, None, True)
    ]  # the settled twin
    (report,) = await _owner_rows(env)
    assert (report.msg_type, report.channel) == ("REPORT", CHAN)
    fresh = await intention_of(env, "dag", dag.id)
    assert (fresh.state, fresh.close_reason) == ("closed", "legacy")  # a legacy close is never reopened
    assert await _repair(env) == 0


async def test_a_dag_at_generation_zero_closed_legacy_is_left_alone(env_factory):  # noqa: F811
    env = await env_factory(**CONT, telegram_chat_id="8080")
    dag, root = await _retried_dag(env)
    async with env.db.session() as s:
        await s.execute(update(ExecutionDAG).where(ExecutionDAG.id == dag.id).values(delivery_generation=0))
        await s.commit()
    assert await _repair(env) == 0  # Phase 1 delivered (or chose not to) the first run: never re-delivered


# ---- (c) a result that already has its row ---------------------------------------------------------------


async def test_a_pending_intention_whose_source_already_has_its_row_closes_delivered(env_factory):  # noqa: F811
    """Flip-time (Review Focus 3): Phase 1's close failed, F098 delivered the row. Close, never re-deliver."""
    env = await env_factory(**CONT)
    st = await make_subtask(env, policy="continue")
    await finish(env, st)
    await env.heart.result_inbox.insert(
        source_kind="subtask", source_id=st.id, msg_type="INFORM", title="t", body="b", channel=CHAN, session_id="S1"
    )
    assert await _repair(env) == 1
    assert len(await inbox_rows(env, st.id)) == 1  # nothing was written
    fresh = await intention_of(env, "subtask", st.id)
    assert (fresh.state, fresh.close_reason) == ("closed", "delivered")


async def test_a_pending_dag_intention_whose_current_generation_has_its_row_closes_delivered(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    dag, _ = await make_dag(env, policy="report", status="completed")
    async with env.db.session() as s:
        await s.execute(update(ExecutionDAG).where(ExecutionDAG.id == dag.id).values(delivered_at=datetime.now(UTC)))
        await s.commit()
    await env.heart.result_inbox.insert(
        source_kind="dag", source_id=dag.id, msg_type="INFORM", title="t", body="b", channel=CHAN, source_generation=0
    )
    assert await _repair(env) == 1
    assert (await intention_of(env, "dag", dag.id)).close_reason == "delivered"
    assert len(await inbox_rows(env, dag.id)) == 1


# ---- (d) a cancelled subtask -----------------------------------------------------------------------------


@pytest.mark.parametrize("policy", ["continue", "report"])
async def test_a_cancelled_subtask_closes_legacy_with_no_report(env_factory, policy):  # noqa: F811
    env = await env_factory(**CONT)
    st = await make_subtask(env, policy=policy)
    await env.heart.subtasks.cancel(st.id)
    assert await _repair(env) == 1
    assert await inbox_rows(env, st.id) == [] and await _owner_rows(env) == []
    fresh = await intention_of(env, "subtask", st.id)
    assert (fresh.state, fresh.close_reason) == ("closed", "legacy")


async def test_a_cancelled_subtask_of_a_cancelled_root_closes_cancelled(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    root = await make_root(env, policy="remember")
    st = await make_subtask(env, policy="continue")
    child = await intention_of(env, "subtask", st.id)
    await set_intention(env, child.id, root_id=root.id, parent_id=root.id, depth=1)
    await set_intention(env, root.id, root_cancelled_at=datetime.now(UTC))
    await env.heart.subtasks.cancel(st.id)
    assert await _repair(env) == 1
    fresh = await intention_of(env, "subtask", st.id)
    assert (fresh.state, fresh.close_reason) == ("cancelled", "cancelled")


# ---- R1 --------------------------------------------------------------------------------------------------


async def test_a_report_with_no_content_closes_legacy_not_delivered(env_factory):  # noqa: F811  # R1
    env = await env_factory(**CONT)
    st = await make_subtask(env, policy="report")
    await finish(env, st, "empty")
    written = await route_result(
        env.heart.result_inbox,
        env.settings,
        source_kind="subtask",
        source_id=st.id,
        generation=0,
        env=None,
        channel=CHAN,
        session_id="S1",
    )
    assert written is False and await inbox_rows(env, st.id) == []
    fresh = await intention_of(env, "subtask", st.id)
    assert (fresh.state, fresh.close_reason) == ("closed", "legacy")


async def test_a_report_with_content_still_closes_delivered_with_its_row(env_factory):  # noqa: F811  # PIN
    env = await env_factory(**CONT)
    st = await make_subtask(env, policy="report")
    await finish(env, st)
    await record_subtask_result(env.heart.result_inbox, await env.heart.subtasks.get(st.id), env.settings)
    assert (await intention_of(env, "subtask", st.id)).close_reason == "delivered"
    assert len(await inbox_rows(env, st.id)) == 1


# ---- bounds, flag off, flip-time ---------------------------------------------------------------------------


async def test_old_results_beyond_the_age_window_are_left_alone(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    st = await make_subtask(env, policy="continue")
    await finish(env, st)
    await _age_subtask(env, st.id, hours=100)  # result_inbox_max_age_hours defaults to 72
    assert await _repair(env) == 0
    assert (await intention_of(env, "subtask", st.id)).state == "pending"
    assert await inbox_rows(env, st.id) == []


async def test_with_continuation_off_the_repair_does_nothing(env_factory):  # noqa: F811  # PIN (prod's flags)
    env = await env_factory(**ON)
    st = await make_subtask(env, policy="report", routed=False)
    await finish(env, st)
    assert await _repair(env) == 0
    assert (await intention_of(env, "subtask", st.id)).state == "pending"


async def test_with_prods_flags_the_repair_never_opens_a_session(env_factory):  # noqa: F811  # PIN (prod's flags)
    """Prod: intentions and the inbox on, continuation off, scheduled DAG routing on, a default chat. The repair
    returns before its first query: a database that raises on use is never touched."""
    env = await env_factory(**ON, result_inbox_dag_scheduled=True, telegram_chat_id="8080")

    def no_session():
        raise AssertionError("the repair opened a session with continuation off")

    assert (
        await repair_missing_results(
            SimpleNamespace(session=no_session), env.heart.result_inbox, env.settings, limit=50
        )
        == 0
    )


async def test_a_phase_1_world_produces_nothing_at_the_first_sweep(env_factory):  # noqa: F811
    """Review Focus 3. What Phase 1 left behind (every close `legacy`, F098 rows delivered, a DAG closed
    `legacy` at generation 0) is not touched when the flag first turns on."""
    env = await env_factory(**ON)  # the Phase 1 writers run: close legacy, F098 routing
    done = await make_subtask(env, policy="continue")
    await finish(env, done)
    await record_subtask_result(env.heart.result_inbox, await env.heart.subtasks.get(done.id), env.settings)
    dag, _ = await make_dag(env, policy="continue", status="completed")
    async with env.db.session() as s:
        await s.execute(update(ExecutionDAG).where(ExecutionDAG.id == dag.id).values(delivered_at=datetime.now(UTC)))
        await s.commit()
    await set_intention(env, (await intention_of(env, "dag", dag.id)).id, state="closed", close_reason="legacy")
    before = (
        [
            (i.state, i.close_reason)
            for i in (await intention_of(env, "subtask", done.id), await intention_of(env, "dag", dag.id))
        ],
        len(await inbox_rows(env)),
    )
    flipped = Settings(_env_file=None, agent_id=env.agent, **CONT)  # the same agent, the flag now on
    assert await _repair(env, flipped) == 0
    after = (
        [
            (i.state, i.close_reason)
            for i in (await intention_of(env, "subtask", done.id), await intention_of(env, "dag", dag.id))
        ],
        len(await inbox_rows(env)),
    )
    assert after == before == ([("closed", "legacy"), ("closed", "legacy")], 1)


# ---- idempotence -------------------------------------------------------------------------------------------


async def _snapshot(env):
    async with env.db.session() as s:
        states = sorted(
            (str(i.id), i.state, i.close_reason)
            for i in (await s.execute(select(Intention).where(Intention.agent_id == env.agent))).scalars().all()
        )
    rows = sorted(
        (r.source_kind, str(r.source_id), r.source_generation, r.channel, r.delivered_at) for r in await inbox_rows(env)
    )
    return states, rows


async def test_a_second_repair_changes_nothing(env_factory):  # noqa: F811
    """One candidate of every residual in one sweep; the next sweep writes and closes nothing."""
    env = await env_factory(**CONT, telegram_chat_id="8080")
    unrouted = await make_subtask(env, policy="report", routed=False)  # (a) report
    await finish(env, unrouted)
    lost = await make_subtask(env, policy="continue")  # (a) continue
    await finish(env, lost)
    settled = await make_subtask(env, policy="continue")  # (c)
    await finish(env, settled)
    await env.heart.result_inbox.insert(
        source_kind="subtask",
        source_id=settled.id,
        msg_type="INFORM",
        title="t",
        body="b",
        channel=CHAN,
        session_id="S1",
    )
    cancelled = await make_subtask(env, policy="report")  # (d)
    await env.heart.subtasks.cancel(cancelled.id)
    await _retried_dag(env)  # (b)
    assert await _repair(env) == 5
    first = await _snapshot(env)
    assert await _repair(env) == 0
    assert await _snapshot(env) == first


# ---- lead notes: an expiry that ran first, and rows held on a gate-closed intention -----------------------


async def _expire(env, ttl=72.0):
    async with env.db.session() as s:
        expired = await continuation.expire_roots(s, env.agent, ttl_hours=ttl, settings=env.settings)
        await s.commit()
    return expired


async def _expire_by_age(env, source_kind, source_id):
    """The TTL sweep closes the (NULL-deadline) root of ``source_id`` 72 h after it was created."""
    it = await intention_of(env, source_kind, source_id)
    await set_intention(env, it.id, created_at=datetime.now(UTC) - timedelta(hours=100))
    assert await _expire(env) == [it.id]
    fresh = await intention_of(env, source_kind, source_id)
    assert (fresh.state, fresh.close_reason) == ("expired", "expired")


@pytest.mark.parametrize("policy", ["report", "continue"])
async def test_a_root_the_ttl_sweep_expired_before_the_repair_still_reaches_the_owner(env_factory, policy):  # noqa: F811
    """Lead note (2c1-6 review, Minor 6). A root pending past its TTL whose hook raised is closed `expired` by
    the sweep before the repair sees it. Its result is still written, once, and the expiry is never undone."""
    env = await env_factory(**CONT, telegram_chat_id="8080")
    st = await make_subtask(env, policy=policy, routed=False)
    await finish(env, st)
    await _expire_by_age(env, "subtask", st.id)
    assert await _owner_rows(env) == []  # a NULL-deadline root with nothing unread expires without a report
    assert await _repair(env) == 1
    (seen,) = [r for r in await inbox_rows(env) if r.channel == CHAN]
    assert "Powder" in seen.body
    if policy == "continue":  # record_result on an expired intention: a raw REPORT, and the settled twin
        assert seen.msg_type == "REPORT"
        assert [(r.channel, r.delivered_at is not None) for r in await inbox_rows(env, st.id)] == [(None, True)]
    else:  # route_result's report branch: the F098-keyed row on the default chat
        assert (seen.source_id, seen.msg_type) == (st.id, "INFORM")
    fresh = await intention_of(env, "subtask", st.id)
    assert (fresh.state, fresh.close_reason) == ("expired", "expired")
    assert await _repair(env) == 0


async def test_an_expired_dag_intention_whose_write_was_lost_is_repaired(env_factory):  # noqa: F811
    env = await env_factory(**CONT, telegram_chat_id="8080")
    dag, _ = await make_dag(env, policy="report", status="completed")
    async with env.db.session() as s:
        await s.execute(update(ExecutionDAG).where(ExecutionDAG.id == dag.id).values(delivered_at=datetime.now(UTC)))
        await s.commit()
    await _expire_by_age(env, "dag", dag.id)
    assert await _repair(env) == 1
    (row,) = await inbox_rows(env, dag.id)
    assert (row.channel, row.source_generation) == (CHAN, 0)
    assert (await intention_of(env, "dag", dag.id)).state == "expired"
    assert await _repair(env) == 0


@pytest.mark.parametrize("why", ["no owner channel", "no content"])
async def test_an_expired_report_with_nothing_to_deliver_is_settled_once(env_factory, monkeypatch, why):  # noqa: F811
    """An expired intention is never closed again, so a result the repair cannot deliver gets a settled
    NULL-keyed row (record_result's twin), or the same candidate would be selected at every sweep."""
    env = await env_factory(**CONT, telegram_chat_id="" if why == "no owner channel" else "8080")
    st = await make_subtask(env, policy="report", routed=False)
    await finish(env, st, "complete" if why == "no owner channel" else "empty")
    await _expire_by_age(env, "subtask", st.id)
    calls = []
    real = result_reconciler.route_result

    async def counted(*args, **kwargs):
        calls.append(kwargs["source_id"])
        return await real(*args, **kwargs)

    monkeypatch.setattr(result_reconciler, "route_result", counted)
    assert await _repair(env) == 0  # nothing reached the owner
    assert [(r.channel, r.session_id, r.delivered_at is not None) for r in await inbox_rows(env, st.id)] == [
        (None, None, True)
    ]
    assert await _owner_rows(env) == []
    assert await _repair(env) == 0
    assert calls == [st.id]  # selected once, never again
    assert (await intention_of(env, "subtask", st.id)).state == "expired"


@pytest.mark.parametrize("reason", ["cancelled", "expired"])
async def test_the_repair_leaves_a_row_held_on_a_gate_closed_intention_to_the_sweep(env_factory, reason):  # noqa: F811
    """Lead note (2c1-4). A row that landed while a gate arrival closed its intention is settled by the TTL
    sweep's stranded-row pass (2c1-6). The repair selects only sources with no row, so it neither touches the
    row nor reports it a second time."""
    env = await env_factory(**CONT)
    root = await make_root(env)
    await record(env, root)
    got = await claim(env, root.id)
    await record(env, root, generation=1, body="landed while the gate ran")
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
    assert done is not None
    await env.heart.subtasks.complete(uuid.UUID(root.source_id), "done", final_outcome="completed")
    assert await _repair(env) == 0
    assert await _expire(env) == []
    assert len(await _owner_rows(env)) == 1  # the sweep's report of the held row
    assert await _repair(env) == 0
    assert len(await _owner_rows(env)) == 1
