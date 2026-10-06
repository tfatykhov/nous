"""F099 Phase 2b: the raw pushes stand down for a continue source; no summary turn for a lineage DAG."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from f099_support import (  # noqa: F401
    CONT,
    ON,
    RESULT,
    env_factory,
    inbox_rows,
    intention_of,
    make_dag,
    make_subtask,
    set_intention,
)

from nous.dag.delivery import DAGResultDelivery

TG = {"telegram_bot_token": "test-token", "telegram_chat_id": "4242"}


def _leg(outcome, name):
    return next(leg for leg in outcome.legs if leg.name == name)


# ---- the subtask worker ------------------------------------------------------------------------------


async def test_no_raw_push_for_a_continue_subtask(env_factory):  # noqa: F811
    env = await env_factory(**CONT, **TG)
    st = await make_subtask(env, notify=True)
    await env.pool._notify_telegram(st, result="done")
    env.http.post.assert_not_awaited()


async def test_the_raw_push_still_goes_out_with_continuation_off(env_factory):  # noqa: F811  # PIN
    env = await env_factory(**ON, **TG)
    st = await make_subtask(env, notify=True)
    await env.pool._notify_telegram(st, result="done")
    env.http.post.assert_awaited_once()


@pytest.mark.parametrize("policy", ["remember", "none", "report"])
async def test_the_raw_push_still_goes_out_for_every_other_policy(env_factory, policy):  # noqa: F811
    env = await env_factory(**CONT, **TG)
    st = await make_subtask(env, policy=policy, notify=True)
    await env.pool._notify_telegram(st, error="boom")
    env.http.post.assert_awaited_once()


async def test_a_failed_intention_lookup_never_costs_the_push(env_factory, monkeypatch):  # noqa: F811
    env = await env_factory(**CONT, **TG)
    st = await make_subtask(env, notify=True)

    async def boom(*args, **kwargs):
        raise RuntimeError("intentions down")

    monkeypatch.setattr(env.heart.intentions, "get_for_source", boom)
    await env.pool._notify_telegram(st, result="done")
    env.http.post.assert_awaited_once()


async def test_the_worker_path_end_to_end_sends_no_push_and_writes_the_intention_keyed_row(env_factory):  # noqa: F811
    """The real _process_subtask: its Telegram call and its terminal hook. Fails when either hook is removed."""
    from nous.handlers.subtask_worker import SubtaskWorkerPool

    class _WorkerTurn:
        async def run_turn(self, **kwargs):
            return RESULT, None, {"input_tokens": 1, "output_tokens": 1}

        async def end_conversation(self, *a, **k):
            return None

    env = await env_factory(**CONT, **TG)
    await make_subtask(env, notify=True)
    pool = SubtaskWorkerPool(_WorkerTurn(), env.heart, env.settings, http_client=env.http)
    await pool._process_subtask(await env.heart.subtasks.dequeue("worker-0"))
    env.http.post.assert_not_awaited()
    (row,) = await inbox_rows(env)
    assert (row.channel, row.session_id, row.body.startswith("Powder")) == (None, None, True)
    assert (await intention_of(env, "subtask", row.source_id)).state == "result_ready"


# ---- the F087 delivery -------------------------------------------------------------------------------


def _delivery(env, runner=None) -> DAGResultDelivery:
    return DAGResultDelivery(
        env.settings,
        agent_id=env.agent,
        http=env.http,
        runner=runner,
        inbox=env.heart.result_inbox,
        intentions=env.heart.intentions,
    )


async def test_the_telegram_leg_stands_down_for_a_continue_dag(env_factory):  # noqa: F811
    env = await env_factory(**CONT, **TG, dag_delivery_telegram_enabled=True)
    dag, _ = await make_dag(env, policy="continue")
    outcome = await _delivery(env).deliver(dag)
    leg = _leg(outcome, "telegram")
    assert (leg.ok, leg.required, leg.detail) == (False, False, "superseded_by_continuation")
    assert outcome.delivered is True  # no required leg: the reconciler guarantees the row (Task 2b-7)
    env.http.post.assert_not_awaited()
    (row,) = await inbox_rows(env, dag.id)
    assert (row.channel, row.session_id) == (None, None)


async def test_the_telegram_leg_still_pushes_with_continuation_off(env_factory):  # noqa: F811  # PIN
    env = await env_factory(**ON, **TG, dag_delivery_telegram_enabled=True)
    dag, _ = await make_dag(env, policy="continue")
    outcome = await _delivery(env).deliver(dag)
    assert _leg(outcome, "telegram").ok is True
    env.http.post.assert_awaited_once()


async def test_the_telegram_leg_still_pushes_for_a_remember_dag(env_factory):  # noqa: F811
    env = await env_factory(**CONT, **TG, dag_delivery_telegram_enabled=True)
    dag, _ = await make_dag(env, policy="remember")
    outcome = await _delivery(env).deliver(dag)
    assert _leg(outcome, "telegram").required is True
    env.http.post.assert_awaited_once()


def _summary_runner() -> AsyncMock:
    runner = AsyncMock()
    runner.run_turn.return_value = ("An authored summary.", None, {})
    return runner


async def test_an_owner_dag_still_gets_its_summary_turn(env_factory):  # noqa: F811  # PIN
    env = await env_factory(**ON, **TG, dag_delivery_agent_summary_enabled=True)
    dag, _ = await make_dag(env, policy="remember")
    runner = _summary_runner()
    outcome = await _delivery(env, runner).deliver(dag)
    runner.run_turn.assert_awaited_once()
    assert _leg(outcome, "summary").ok is True and outcome.summary == "An authored summary."


@pytest.mark.parametrize("flags", [CONT, ON], ids=["continuation-on", "continuation-off"])
async def test_an_internal_only_dag_gets_no_summary_turn_whatever_the_flag(env_factory, flags):  # noqa: F811
    """Contract C6: a lineage DAG that finishes after the flag went off must not run the
    summary turn with outward tools."""
    env = await env_factory(**flags, **TG, dag_delivery_agent_summary_enabled=True)
    dag, _ = await make_dag(env, policy="continue")
    it = await intention_of(env, "dag", dag.id)
    await set_intention(env, it.id, authority="internal_only")
    runner = _summary_runner()
    outcome = await _delivery(env, runner).deliver(dag)
    runner.run_turn.assert_not_awaited()
    leg = _leg(outcome, "summary")
    assert (leg.ok, leg.required, leg.detail) == (True, False, "internal_only")
    assert outcome.summary != "An authored summary."


async def test_a_failed_dag_intention_lookup_follows_the_continuation_flag(env_factory, monkeypatch):  # noqa: F811
    """MF-2. With continuation on, an unreadable lineage fails closed: no summary turn (the template is
    used) and the Telegram push is kept. With continuation off (intentions on), a failed lookup changes
    nothing: the summary turn runs exactly as in Phase 1."""

    async def boom(*args, **kwargs):
        raise RuntimeError("intentions down")

    results = {}
    for name, flags in (("continuation-on", CONT), ("continuation-off", ON)):
        env = await env_factory(
            **flags, **TG, dag_delivery_agent_summary_enabled=True, dag_delivery_telegram_enabled=True
        )
        dag, _ = await make_dag(env, policy="remember")
        monkeypatch.setattr(env.heart.intentions, "get_for_source", boom)
        runner = _summary_runner()
        outcome = await _delivery(env, runner).deliver(dag)
        results[name] = (runner.run_turn.await_count, _leg(outcome, "summary").detail, _leg(outcome, "telegram").ok)
    assert results["continuation-on"] == (0, "internal_only", True)
    assert results["continuation-off"][0] == 1 and results["continuation-off"][2] is True  # PIN: Phase 1


async def test_with_intentions_off_the_delivery_reads_no_intention(env_factory, monkeypatch):  # noqa: F811  # PIN
    env = await env_factory(result_inbox_enabled=True, **TG, dag_delivery_agent_summary_enabled=True)
    dag, _ = await make_dag(env, policy="remember")
    reads = []

    async def boom(*args, **kwargs):
        reads.append(args)
        raise AssertionError("an intention was read with the flag off")

    monkeypatch.setattr(env.heart.intentions, "get_for_source", boom)
    runner = _summary_runner()
    await _delivery(env, runner).deliver(dag)
    runner.run_turn.assert_awaited_once()
    assert reads == []  # deliver swallows a failed read, so the raise alone would pass unseen
