"""F099 Phase 2b: the writers route by wake policy, and close as 'delivered' with the flag on."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from f099_support import (  # noqa: F401
    CHAN,
    CONT,
    ON,
    RESULT,
    dag_kwargs,
    env_factory,
    finish,
    inbox_rows,
    intention_of,
    make_dag,
    make_subtask,
    set_intention,
)

from nous.brain import continuation
from nous.heart.result_inbox import ResultInboxDagListener, record_dag_result, record_subtask_result


async def _hook(env, st, how: str = "complete"):
    """The worker's terminal hook, after the subtask finished."""
    await finish(env, st, how)
    await env.pool._record_inbox(st)


# ---- continue: keyed by the intention alone -------------------------------------------------------


@pytest.mark.parametrize("routed", [True, False], ids=["routed", "unrouted"])
async def test_a_continue_result_is_keyed_by_the_intention_alone(env_factory, routed):  # noqa: F811
    """Spec 4.3 item 1: channel and session NULL whatever the work row says, so a chat claim cannot take it."""
    env = await env_factory(**CONT)
    st = await make_subtask(env, routed=routed)
    await _hook(env, st)
    it = await intention_of(env, "subtask", st.id)
    (row,) = await inbox_rows(env, st.id)
    assert (row.channel, row.session_id, row.intention_id) == (None, None, it.id)
    assert (it.state, it.close_reason) == ("result_ready", None) and it.result_at is not None
    rows, older = await env.heart.result_inbox.claim(channel=CHAN, session_id="S1", max_age_hours=72, max_items=10)
    assert (rows, older) == ([], 0)  # a chat turn on the origin channel and session takes nothing
    assert (await inbox_rows(env, st.id))[0].delivered_at is None


async def test_a_failed_continue_subtask_writes_a_failure_row(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    st = await make_subtask(env)
    await _hook(env, st, "fail")
    (row,) = await inbox_rows(env, st.id)
    assert row.msg_type == "FAILURE" and (row.channel, row.session_id) == (None, None)


async def test_a_continue_subtask_with_no_output_still_wakes_its_intention(env_factory):  # noqa: F811
    """Contract C12: without a row the intention would stay pending forever."""
    env = await env_factory(**CONT)
    st = await make_subtask(env)
    await _hook(env, st, "empty")
    (row,) = await inbox_rows(env, st.id)
    assert row.msg_type == "INFORM" and "returned no output" in row.body
    assert (await intention_of(env, "subtask", st.id)).state == "result_ready"


async def test_the_worker_hook_announces_a_continue_result_on_the_bus(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    env.heart.result_inbox.set_bus(env.bus)
    st = await make_subtask(env)
    await _hook(env, st)
    it = await intention_of(env, "subtask", st.id)
    assert [(e.type, e.data["intention_id"]) for e in env.bus.events] == [("intention.result_ready", str(it.id))]


async def test_a_fault_in_the_move_leaves_the_result_undelivered_and_the_intention_pending(env_factory, monkeypatch):  # noqa: F811
    env = await env_factory(**CONT)
    st = await make_subtask(env)

    async def boom(*args, **kwargs):
        raise RuntimeError("fault")

    monkeypatch.setattr(continuation, "_set_result_ready", boom)
    await _hook(env, st)  # the hook swallows it
    assert await inbox_rows(env, st.id) == []
    assert (await intention_of(env, "subtask", st.id)).state == "pending"


# ---- report: I4, closed as delivered in the insert's transaction ----------------------------------


async def test_a_report_intention_closes_as_delivered_with_its_row(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    st = await make_subtask(env, policy="report")
    await _hook(env, st)
    it = await intention_of(env, "subtask", st.id)
    (row,) = await inbox_rows(env, st.id)
    assert (it.state, it.close_reason) == ("closed", "delivered") and it.result_at is not None
    assert (row.channel, row.session_id, row.intention_id) == (CHAN, "S1", it.id)  # routed as F098 A: chat consumes it


async def test_a_failed_close_rolls_the_report_row_back_and_a_retry_lands_both(env_factory, monkeypatch):  # noqa: F811
    env = await env_factory(**CONT)
    st = await make_subtask(env, policy="report")
    await finish(env, st)
    real = continuation.close_delivered

    async def boom(*args, **kwargs):
        raise RuntimeError("fault after the insert")

    monkeypatch.setattr(continuation, "close_delivered", boom)
    await env.pool._record_inbox(st)
    assert await inbox_rows(env, st.id) == []  # one transaction: neither the row nor the close
    assert (await intention_of(env, "subtask", st.id)).state == "pending"
    monkeypatch.setattr(continuation, "close_delivered", real)
    await env.pool._record_inbox(st)  # what the reconciler's pass does
    assert len(await inbox_rows(env, st.id)) == 1
    assert (await intention_of(env, "subtask", st.id)).close_reason == "delivered"


async def test_an_unrouted_report_falls_back_to_the_default_chat(env_factory):  # noqa: F811
    """Spec 4.1 (lead ruling): a report with no routing key goes to the default chat, closed as
    delivered in the insert's transaction. A DAG takes it with scheduled routing off too."""
    env = await env_factory(**CONT, telegram_chat_id="4242")
    st = await make_subtask(env, policy="report", routed=False)
    await _hook(env, st)
    it = await intention_of(env, "subtask", st.id)
    (row,) = await inbox_rows(env, st.id)
    assert (row.channel, row.session_id, row.intention_id) == ("telegram:4242", None, it.id)
    assert (it.state, it.close_reason) == ("closed", "delivered")
    dag, _ = await make_dag(env, policy="report")
    assert await record_dag_result(env.heart.result_inbox, env.settings, **dag_kwargs(dag)) is True
    assert [r.channel for r in await inbox_rows(env, dag.id)] == ["telegram:4242"]
    assert (await intention_of(env, "dag", dag.id)).close_reason == "delivered"


async def test_an_unrouted_report_with_no_chat_writes_nothing_and_closes_as_legacy(env_factory):  # noqa: F811
    """Lead ruling: nothing was delivered, so the close is not 'delivered'."""
    env = await env_factory(**CONT)  # no origin channel, no default chat
    st = await make_subtask(env, policy="report", routed=False)
    await _hook(env, st)
    it = await intention_of(env, "subtask", st.id)
    assert (it.state, it.close_reason) == ("closed", "legacy") and await inbox_rows(env, st.id) == []


async def test_with_continuation_off_an_unrouted_report_still_writes_nothing(env_factory):  # noqa: F811  # PIN (Phase 1 behaviour)
    env = await env_factory(**ON, telegram_chat_id="4242")
    st = await make_subtask(env, policy="report", routed=False)
    await _hook(env, st)
    it = await intention_of(env, "subtask", st.id)
    assert (it.state, it.close_reason) == ("closed", "legacy") and await inbox_rows(env) == []


# ---- none and remember -----------------------------------------------------------------------------


@pytest.mark.parametrize("policy", ["none", "remember"])
async def test_none_and_remember_close_as_delivered_and_route_as_f098(env_factory, policy):  # noqa: F811
    env = await env_factory(**CONT)
    st = await make_subtask(env, policy=policy)
    await _hook(env, st)
    it = await intention_of(env, "subtask", st.id)
    (row,) = await inbox_rows(env, st.id)
    assert (it.state, it.close_reason) == ("closed", "delivered")
    assert (row.channel, row.session_id, row.intention_id) == (CHAN, "S1", it.id)


# ---- the three flag states --------------------------------------------------------------------------

STATES = {
    # The first two rows are Phase 1, byte for byte.
    "off-off": ({}, "pending", None, (CHAN, "S1"), False),
    "on-off": (ON, "closed", "legacy", (CHAN, "S1"), True),
    "on-on": (CONT, "result_ready", None, (None, None), True),
}


@pytest.mark.parametrize("flags", list(STATES))
async def test_the_three_flag_states_route_a_continue_result_as_specified(env_factory, flags):  # noqa: F811  # PIN: off-off, on-off
    over, state, reason, keys, names_it = STATES[flags]
    env = await env_factory(**{"result_inbox_enabled": True, **over})
    st = await make_subtask(env)  # as if spawned while the intentions flag was on
    await _hook(env, st)
    it = await intention_of(env, "subtask", st.id)
    (row,) = await inbox_rows(env, st.id)
    assert (it.state, it.close_reason) == (state, reason)
    assert (row.channel, row.session_id) == keys
    assert (row.intention_id == it.id) is names_it


# ---- DAGs -------------------------------------------------------------------------------------------


async def test_a_continue_dag_never_takes_the_default_chat(env_factory):  # noqa: F811
    """Contract risk 6: result_inbox_dag_scheduled would route a continue DAG to the chat."""
    env = await env_factory(**CONT, result_inbox_dag_scheduled=True, telegram_chat_id="4242")
    dag, _ = await make_dag(env, policy="continue")
    assert await record_dag_result(env.heart.result_inbox, env.settings, **dag_kwargs(dag)) is True
    it = await intention_of(env, "dag", dag.id)
    (row,) = await inbox_rows(env, dag.id)
    assert (row.channel, row.session_id, row.intention_id) == (None, None, it.id)
    assert it.state == "result_ready"
    remembered, _ = await make_dag(env, policy="remember")  # the non-continue branch keeps its substitution
    await record_dag_result(env.heart.result_inbox, env.settings, **dag_kwargs(remembered))
    (other,) = await inbox_rows(env, remembered.id)
    it2 = await intention_of(env, "dag", remembered.id)
    assert (other.channel, it2.close_reason) == ("telegram:4242", "delivered")


async def test_the_bus_listener_and_the_delivery_path_collapse_to_one_row(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    dag, _ = await make_dag(env)
    store = env.heart.result_inbox
    assert await record_dag_result(store, env.settings, **dag_kwargs(dag)) is True
    assert await record_dag_result(store, env.settings, **dag_kwargs(dag)) is False
    event = SimpleNamespace(
        data={
            "dag_id": str(dag.id),
            "name": "snow-dag",
            "status": "completed",
            "summary": "ok",
            "delivery_generation": dag.delivery_generation,
        }
    )
    await ResultInboxDagListener(store, env.settings).handle(event)
    assert len(await inbox_rows(env, dag.id)) == 1
    assert (await intention_of(env, "dag", dag.id)).state == "result_ready"


async def test_a_retried_dag_reopens_its_closed_continue_intention(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    dag, _ = await make_dag(env)
    store = env.heart.result_inbox
    await record_dag_result(store, env.settings, **dag_kwargs(dag))
    it = await intention_of(env, "dag", dag.id)
    await set_intention(env, it.id, state="closed", close_reason="resolved", closed_at=datetime.now(UTC))
    await record_dag_result(store, env.settings, **{**dag_kwargs(dag), "generation": 1})
    after = await intention_of(env, "dag", dag.id)
    assert (after.state, after.close_reason) == ("result_ready", None)
    assert [r.source_generation for r in await inbox_rows(env, dag.id)] == [0, 1]


async def test_a_retried_dag_on_an_expired_root_reports_the_raw_result(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    dag, _ = await make_dag(env, origin_channel=CHAN)
    store = env.heart.result_inbox
    await record_dag_result(store, env.settings, **dag_kwargs(dag, origin_channel=CHAN))
    it = await intention_of(env, "dag", dag.id)
    await set_intention(env, it.id, state="closed", close_reason="resolved", root_expired_at=datetime.now(UTC))
    await record_dag_result(store, env.settings, **{**dag_kwargs(dag, origin_channel=CHAN), "generation": 1})
    rows = await inbox_rows(env)
    reports = [r for r in rows if r.source_kind == "intention_report"]
    assert len(reports) == 1 and (reports[0].channel, reports[0].msg_type) == (CHAN, "REPORT")
    assert (await intention_of(env, "dag", dag.id)).state == "closed"


@pytest.mark.parametrize("flags", [CONT, ON], ids=["continuation-on", "continuation-off"])
async def test_a_non_terminal_dag_status_writes_and_closes_nothing(env_factory, flags):  # noqa: F811
    env = await env_factory(**flags)
    dag, _ = await make_dag(env, status="running")
    assert await record_dag_result(env.heart.result_inbox, env.settings, **dag_kwargs(dag, status="running")) is False
    assert await inbox_rows(env, dag.id) == []
    assert (await intention_of(env, "dag", dag.id)).state == "pending"


async def test_a_report_dag_closes_as_delivered_with_its_row_and_an_unrouted_one_as_legacy(env_factory):  # noqa: F811
    env = await env_factory(**CONT)  # no default chat
    routed, _ = await make_dag(env, policy="report", origin_channel=CHAN)
    bare, _ = await make_dag(env, policy="report")
    store = env.heart.result_inbox
    assert await record_dag_result(store, env.settings, **dag_kwargs(routed, origin_channel=CHAN)) is True
    assert await record_dag_result(store, env.settings, **dag_kwargs(bare)) is False
    assert (await intention_of(env, "dag", routed.id)).close_reason == "delivered"
    assert (await intention_of(env, "dag", bare.id)).close_reason == "legacy"  # nothing was delivered
    assert [r.channel for r in await inbox_rows(env, routed.id)] == [CHAN]
    assert await inbox_rows(env, bare.id) == []


# ---- the reconciler's subtask pass ------------------------------------------------------------------


async def test_the_inbox_pass_routes_a_lost_continue_result_by_the_intention(env_factory):  # noqa: F811
    from nous.heart.result_reconciler import InboxSubtaskPass

    env = await env_factory(**CONT)
    store = env.heart.result_inbox
    await store.ensure_enabled_at()  # repairs cover results finished after this
    st = await make_subtask(env)
    await finish(env, st)  # the hook's write was lost
    assert await InboxSubtaskPass(env.db, store, env.settings).run(limit=10) == 1
    (row,) = await inbox_rows(env, st.id)
    assert (row.channel, row.session_id) == (None, None)
    assert (await intention_of(env, "subtask", st.id)).state == "result_ready"
    assert await InboxSubtaskPass(env.db, store, env.settings).run(limit=10) == 0  # idempotent


async def test_the_inbox_pass_settles_a_non_continue_subtask_with_nothing_to_say(env_factory):  # noqa: F811
    from nous.heart.result_reconciler import InboxSubtaskPass

    env = await env_factory(**CONT)
    store = env.heart.result_inbox
    await store.ensure_enabled_at()
    st = await make_subtask(env, policy="remember")
    await finish(env, st, "empty")
    assert await InboxSubtaskPass(env.db, store, env.settings).run(limit=10) == 0
    assert (await env.heart.subtasks.get(st.id)).delivered is True
    assert (await intention_of(env, "subtask", st.id)).close_reason == "delivered"


async def test_record_subtask_result_skips_dag_node_subtasks_with_the_flag_on(env_factory):  # noqa: F811  # PIN (Phase 1 behaviour)
    env = await env_factory(**CONT)
    st = await make_subtask(env)
    row = await finish(env, st)
    row.metadata_ = {"dag_id": "d"}
    assert await record_subtask_result(env.heart.result_inbox, row, env.settings) is False
