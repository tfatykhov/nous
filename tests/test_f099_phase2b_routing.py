"""F099 Phase 2b: chat never claims a continue row, and the reconciler selects continuation work."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from f099_support import (  # noqa: F401
    CHAN,
    CONT,
    ON,
    RESULT,
    env_factory,
    finish,
    inbox_rows,
    intention_of,
    make_dag,
    make_subtask,
    set_intention,
)
from sqlalchemy import update

from nous.brain import continuation, intentions
from nous.dag.delivery import DAGResultDelivery
from nous.heart.result_reconciler import InboxDagPass, InboxSubtaskPass, IntentionClosePass, build_reconciler
from nous.storage.models import ExecutionDAG


@pytest.fixture
async def make_layer(env_factory):  # noqa: F811
    from nous.brain.brain import Brain
    from nous.cognitive.layer import CognitiveLayer

    brains = []

    async def build(**over):
        env = await env_factory(**{"result_inbox_enabled": True, **over})
        brain = Brain(database=env.db, settings=env.settings)
        brains.append(brain)
        env.layer = CognitiveLayer(brain, env.heart, env.settings, identity_prompt="You are Nous.")
        return env

    yield build
    for brain in brains:
        await brain.close()


def _prompt(ctx) -> str:
    return ctx.system_prompt if isinstance(ctx.system_prompt, str) else str(ctx.system_prompt)


async def _row(env, **over):
    import uuid

    values = dict(
        source_kind="subtask", source_id=uuid.uuid4(), msg_type="INFORM", title="t", body="forged body", channel=CHAN
    )
    values.update(over)
    await env.heart.result_inbox.insert(**values)


# ---- pre_turn -----------------------------------------------------------------------------------------


async def test_pre_turn_skips_the_inbox_for_an_intent_session(make_layer):
    env = await make_layer(**CONT)
    await _row(env, channel=None, session_id="intent-abc")  # forged into the continuation's own session
    ctx = await env.layer.pre_turn(env.agent, "intent-abc", "hi")
    assert "forged body" not in _prompt(ctx)
    assert (await inbox_rows(env))[0].delivered_at is None


async def test_pre_turn_skips_the_inbox_for_the_continuation_context(make_layer):
    env = await make_layer(**CONT)
    await _row(env)
    ctx = await env.layer.pre_turn(env.agent, "S2", "hi", channel=CHAN, context_kind="continuation")
    assert "forged body" not in _prompt(ctx)
    assert (await inbox_rows(env))[0].delivered_at is None
    assert await env.heart.result_inbox.get_channel_session(CHAN) is None  # it did not even touch the channel


async def test_the_chat_claim_still_takes_an_owner_row(make_layer):  # PIN
    env = await make_layer(**CONT)
    await _row(env, source_kind="intention_report", msg_type="REPORT", body="owner-facing report")
    ctx = await env.layer.pre_turn(env.agent, "S2", "hi", channel=CHAN)
    assert "owner-facing report" in _prompt(ctx)
    assert '<result_message type="REPORT" source="intention_report"' in _prompt(ctx)
    assert (await inbox_rows(env))[0].delivered_at is not None


async def test_a_continue_result_is_never_injected_into_a_chat_turn(make_layer):  # PIN
    env = await make_layer(**CONT)
    st = await make_subtask(env)
    await finish(env, st)
    await env.pool._record_inbox(st)
    ctx = await env.layer.pre_turn(env.agent, "S1", "hi", channel=CHAN)  # the very channel and session it came from
    assert RESULT not in _prompt(ctx)
    assert (await inbox_rows(env, st.id))[0].delivered_at is None
    assert (await intention_of(env, "subtask", st.id)).state == "result_ready"


async def test_a_continuation_turn_starts_no_deliberation(make_layer):
    env = await make_layer(**CONT)
    env.layer._deliberation.should_deliberate = AsyncMock(return_value=True)
    env.layer._deliberation.start = AsyncMock(return_value=None)
    await env.layer.pre_turn(env.agent, "S2", "hi", context_kind="continuation")
    env.layer._deliberation.start.assert_not_awaited()
    await env.layer.pre_turn(env.agent, "S3", "hi")  # control: an ordinary turn does
    env.layer._deliberation.start.assert_awaited_once()


# ---- IntentionClosePass ---------------------------------------------------------------------------------


@pytest.mark.postgres_only  # CAST(text AS uuid) join
async def test_the_close_pass_leaves_continue_and_report_alone(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    done = {}
    for policy in ("continue", "report", "remember", "none"):
        st = await make_subtask(env, policy=policy, routed=False)
        await finish(env, st)  # no writer ran
        done[policy] = st.id
    assert await IntentionClosePass(env.db, env.settings).run(limit=50) == 2
    states = {p: (await intention_of(env, "subtask", i)) for p, i in done.items()}
    assert (states["continue"].state, states["report"].state) == ("pending", "pending")
    assert [(states[p].state, states[p].close_reason) for p in ("remember", "none")] == [
        ("closed", "delivered"),
        ("closed", "delivered"),
    ]


@pytest.mark.postgres_only  # CAST(text AS uuid) join
async def test_with_continuation_off_the_close_pass_closes_every_policy_as_legacy(env_factory):  # noqa: F811  # PIN
    env = await env_factory(**ON)
    ids = []
    for policy in ("continue", "report", "remember"):
        st = await make_subtask(env, policy=policy, routed=False)
        await finish(env, st)
        ids.append(st.id)
    assert await IntentionClosePass(env.db, env.settings).run(limit=50) == 3
    for i in ids:
        assert (await intention_of(env, "subtask", i)).close_reason == "legacy"


@pytest.mark.postgres_only  # CAST(text AS uuid) join
async def test_close_finished_sources_honours_exclude_policies(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    st = await make_subtask(env, policy="remember", routed=False)
    await finish(env, st)
    async with env.db.session() as s:
        assert await intentions.close_finished_sources(s, env.agent, limit=10, exclude_policies=("remember",)) == []
        assert len(await intentions.close_finished_sources(s, env.agent, limit=10)) == 1
        await s.commit()


# ---- InboxDagPass ----------------------------------------------------------------------------------------


async def _dag_orchestrator(env, dags):
    from nous.dag.orchestrator import DAGOrchestrator

    delivery = DAGResultDelivery(
        env.settings,
        agent_id=env.agent,
        http=env.http,
        inbox=env.heart.result_inbox,
        intentions=env.heart.intentions,
    )
    loader = AsyncMock()
    loader._registry = MagicMock()
    return DAGOrchestrator(
        store=dags, subtask_mgr=AsyncMock(), dynamic_loader=loader, settings=env.settings, delivery=delivery
    )


@pytest.mark.postgres_only  # CAST(text AS uuid) in the pass filter
async def test_a_continue_dag_is_never_delivered_without_its_row(env_factory, monkeypatch):  # noqa: F811
    """Spec 7 'Routing'. The F087 delivery marks the DAG delivered (no required leg is left once the
    push stands down) even though its row failed to land. InboxDagPass must select it: a continuation
    DAG has no origin, and without the extra condition its lost row would never be repaired."""
    env = await env_factory(
        **CONT, telegram_bot_token="test-token", telegram_chat_id="77", dag_delivery_telegram_enabled=True
    )
    store = env.heart.result_inbox
    await store.ensure_enabled_at()
    dag, dags = await make_dag(env, policy="continue")  # no origin channel, no origin session
    orch = await _dag_orchestrator(env, dags)

    real = continuation.record_result
    calls = {"n": 0}

    async def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionError("db blip")
        return await real(*args, **kwargs)

    monkeypatch.setattr(continuation, "record_result", flaky)
    await orch._deliver_terminal_dags()
    delivered = await dags.get_dag(dag.id)
    assert delivered.delivered_at is not None  # marked delivered...
    assert await inbox_rows(env, dag.id) == []  # ...with no row
    env.http.post.assert_not_awaited()  # and the push stood down

    assert await InboxDagPass(env.db, store, env.settings).run(limit=10) == 1
    (row,) = await inbox_rows(env, dag.id)
    assert (row.channel, row.session_id, row.source_generation) == (None, None, delivered.delivery_generation)
    assert (await intention_of(env, "dag", dag.id)).state == "result_ready"
    assert await InboxDagPass(env.db, store, env.settings).run(limit=10) == 0  # idempotent
    await orch._deliver_terminal_dags()
    env.http.post.assert_not_awaited()


async def _delivered_dag(env, dag_id):
    """Mark a DAG delivered by hand, as F087's delivery sweep does."""
    async with env.db.session() as s:
        await s.execute(update(ExecutionDAG).where(ExecutionDAG.id == dag_id).values(delivered_at=datetime.now(UTC)))
        await s.commit()


@pytest.mark.postgres_only  # CAST(text AS uuid) in the pass filter
async def test_the_dag_pass_still_skips_an_unroutable_non_continue_dag(env_factory):  # noqa: F811  # PIN
    env = await env_factory(**CONT)
    store = env.heart.result_inbox
    await store.ensure_enabled_at()
    dag, _ = await make_dag(env, policy="remember")
    await _delivered_dag(env, dag.id)
    assert await InboxDagPass(env.db, store, env.settings).run(limit=10) == 0
    assert await inbox_rows(env, dag.id) == []


@pytest.mark.postgres_only  # CAST(text AS uuid) in the pass filter
@pytest.mark.parametrize(
    ("reason", "selected", "after"), [("legacy", 0, ("closed", "legacy")), ("resolved", 1, ("result_ready", None))]
)
async def test_the_dag_pass_never_selects_a_continue_dag_closed_as_legacy(env_factory, reason, selected, after):  # noqa: F811
    """Lead addendum (2b-7 review). F098 already delivered the result of a continue DAG that Phase 1 (or the
    startup rollback) closed as 'legacy': with continuation on, InboxDagPass must not select it and reopen
    it. One the continuation closed itself ('resolved') is still selected: a retried DAG reopens it."""
    env = await env_factory(**CONT)
    store = env.heart.result_inbox
    await store.ensure_enabled_at()
    dag, _ = await make_dag(env, policy="continue")  # no origin: selectable by its intention alone
    it = await intention_of(env, "dag", dag.id)
    await set_intention(env, it.id, state="closed", close_reason=reason, closed_at=datetime.now(UTC))
    await _delivered_dag(env, dag.id)
    assert await InboxDagPass(env.db, store, env.settings).run(limit=10) == selected
    assert len(await inbox_rows(env, dag.id)) == selected  # not even a settled twin for the legacy one
    found = await intention_of(env, "dag", dag.id)
    assert (found.state, found.close_reason) == after


@pytest.mark.postgres_only  # CAST(text AS uuid) in the pass filter
async def test_with_continuation_off_the_dag_pass_skips_a_continue_dag_without_origin(env_factory):  # noqa: F811  # PIN
    env = await env_factory(**ON)
    store = env.heart.result_inbox
    await store.ensure_enabled_at()
    dag, _ = await make_dag(env, policy="continue")
    await _delivered_dag(env, dag.id)
    assert await InboxDagPass(env.db, store, env.settings).run(limit=10) == 0


# ---- InboxSubtaskPass ------------------------------------------------------------------------------------


@pytest.mark.postgres_only  # CAST(text AS uuid) in the pass filter
async def test_the_subtask_pass_selects_an_unrouted_continue_subtask_and_not_a_remember_one(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    store = env.heart.result_inbox
    await store.ensure_enabled_at()
    cont = await make_subtask(env, policy="continue", routed=False)  # e.g. a heartbeat check's spawn
    other = await make_subtask(env, policy="remember", routed=False)
    await finish(env, cont)
    await finish(env, other)  # neither hook ran
    assert await InboxSubtaskPass(env.db, store, env.settings).run(limit=10) == 1
    (row,) = await inbox_rows(env)
    assert (row.source_id, row.channel, row.session_id) == (cont.id, None, None)


# No postgres_only marker: with the flag off the pass never builds the CAST(text AS uuid) condition.
async def test_with_continuation_off_the_subtask_pass_skips_an_unrouted_continue_subtask(env_factory):  # noqa: F811  # PIN
    env = await env_factory(**ON)
    store = env.heart.result_inbox
    await store.ensure_enabled_at()
    st = await make_subtask(env, policy="continue", routed=False)
    await finish(env, st)
    assert await InboxSubtaskPass(env.db, store, env.settings).run(limit=10) == 0
    assert await inbox_rows(env, st.id) == []
    assert (await intention_of(env, "subtask", st.id)).state == "pending"  # the intentions pass closes it


@pytest.mark.postgres_only  # CAST(text AS uuid) in the pass filter
async def test_a_reported_continue_result_is_not_reselected_by_the_subtask_pass(env_factory):  # noqa: F811  # PIN
    """MF-1. A continue result that became a report (its root expired after the work finished) must
    leave a source-keyed row behind. Otherwise the pass re-selects it on every tick and, with a small
    batch, starves the source behind it."""
    env = await env_factory(**CONT)
    store = env.heart.result_inbox
    await store.ensure_enabled_at()
    stuck = await make_subtask(env)
    await finish(env, stuck)
    it = await intention_of(env, "subtask", stuck.id)
    await set_intention(env, it.id, state="closed", close_reason="resolved", root_expired_at=datetime.now(UTC))
    healthy = await make_subtask(env)
    await finish(env, healthy)  # its hook's write was lost
    pass_ = InboxSubtaskPass(env.db, store, env.settings)
    assert await pass_.run(limit=1) == 1  # the oldest: the stuck one becomes a report
    assert await pass_.run(limit=1) == 1  # the next slot goes to the healthy one, not to the stuck one again
    assert (await intention_of(env, "subtask", healthy.id)).state == "result_ready"
    assert await pass_.run(limit=1) == 0
    assert [r.source_kind for r in await inbox_rows(env, stuck.id)] == ["subtask"]  # the settled twin


@pytest.mark.postgres_only  # CAST(text AS uuid) in the pass filter
async def test_a_reported_continue_dag_is_settled_for_the_dag_pass(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    store = env.heart.result_inbox
    await store.ensure_enabled_at()
    dag, _ = await make_dag(env, policy="continue")  # unrouted: selected by its intention alone
    it = await intention_of(env, "dag", dag.id)
    await set_intention(env, it.id, state="closed", close_reason="resolved", root_cancelled_at=datetime.now(UTC))
    await _delivered_dag(env, dag.id)
    pass_ = InboxDagPass(env.db, store, env.settings)
    await pass_.run(limit=1)
    (twin,) = await inbox_rows(env, dag.id)  # without the settled twin the next tick selects it again
    assert (twin.channel, twin.session_id) == (None, None) and twin.delivered_at is not None
    assert await pass_.run(limit=1) == 0
    assert len(await inbox_rows(env, dag.id)) == 1


# ---- end to end: a lost hook write is repaired, never closed (lead addendum) --------------------------------


async def _write_fails(*args, **kwargs):
    raise ConnectionError("db blip")


@pytest.mark.postgres_only  # CAST(text AS uuid) in the pass filters and the close join
async def test_a_lost_unrouted_continue_subtask_result_is_repaired_not_closed(env_factory, monkeypatch):  # noqa: F811
    """An unrouted continue subtask (a heartbeat check's spawn) whose worker-hook write raised. F098's
    filter never selects it, so on the base the intentions pass closes it as 'legacy' and the result is
    lost for good. A tick whose inbox pass fails too must not close it; the next tick writes its row."""
    env = await env_factory(**CONT)
    store = env.heart.result_inbox
    await store.ensure_enabled_at()
    st = await make_subtask(env, routed=False)
    await finish(env, st)
    real = continuation.record_result
    monkeypatch.setattr(continuation, "record_result", _write_fails)
    await env.pool._record_inbox(st)  # the hook's write raises, and is swallowed
    assert await inbox_rows(env, st.id) == []

    reconciler = build_reconciler(env.db, store, env.settings)
    await reconciler.run_once()  # the inbox pass raises again; the intentions pass runs after it
    assert (await intention_of(env, "subtask", st.id)).state == "pending"

    monkeypatch.setattr(continuation, "record_result", real)
    await reconciler.run_once()
    await reconciler.run_once()  # idempotent
    it = await intention_of(env, "subtask", st.id)
    assert (it.state, it.close_reason) == ("result_ready", None)
    (row,) = await inbox_rows(env, st.id)
    assert (row.intention_id, row.channel, row.session_id) == (it.id, None, None)


@pytest.mark.postgres_only  # CAST(text AS uuid) in the pass filters and the close join
async def test_a_delivered_continue_dag_without_its_row_is_repaired_not_closed(env_factory, monkeypatch):  # noqa: F811
    """A no-origin continue DAG whose inbox write raised. F087 still marks it delivered (the superseded
    Telegram leg is not required, and record_dag_result swallows the failure), so the repair starts from
    a delivered DAG with an open continue intention and no row. The intentions pass must not close it,
    and InboxDagPass must select it although it is delivered."""
    env = await env_factory(
        **CONT, telegram_bot_token="test-token", telegram_chat_id="77", dag_delivery_telegram_enabled=True
    )
    store = env.heart.result_inbox
    await store.ensure_enabled_at()
    dag, dags = await make_dag(env, policy="continue")  # no origin channel, no origin session
    real = continuation.record_result
    monkeypatch.setattr(continuation, "record_result", _write_fails)
    await (await _dag_orchestrator(env, dags))._deliver_terminal_dags()
    delivered = await dags.get_dag(dag.id)
    assert delivered.delivered_at is not None
    assert await inbox_rows(env, dag.id) == []
    assert (await intention_of(env, "dag", dag.id)).state == "pending"

    reconciler = build_reconciler(env.db, store, env.settings)
    await reconciler.run_once()  # the DAG pass's write fails again; the intentions pass runs after it
    assert (await intention_of(env, "dag", dag.id)).state == "pending"

    monkeypatch.setattr(continuation, "record_result", real)
    await reconciler.run_once()
    await reconciler.run_once()  # idempotent
    it = await intention_of(env, "dag", dag.id)
    assert (it.state, it.close_reason) == ("result_ready", None)
    (row,) = await inbox_rows(env, dag.id)
    assert (row.intention_id, row.channel, row.session_id) == (it.id, None, None)
    assert row.source_generation == delivered.delivery_generation
    env.http.post.assert_not_awaited()
