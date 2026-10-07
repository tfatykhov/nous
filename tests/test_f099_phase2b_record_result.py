"""F099 Phase 2b: record_result — the same-transaction move, held rows, re-arrivals, reports."""

from __future__ import annotations

import asyncio
import dataclasses
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from f099_support import (  # noqa: F401
    CHAN,
    CONT,
    RESULT,
    dag_kwargs,
    env_factory,
    inbox_rows,
    intention_of,
    make_dag,
    make_subtask,
    set_intention,
)
from sqlalchemy import select, text, update

from nous.brain import continuation
from nous.heart.result_inbox import Envelope, record_dag_result
from nous.storage.models import Intention


async def _record(env, st, *, generation: int = 0, body: str = RESULT, **over):
    it = await intention_of(env, "subtask", st.id)
    async with env.db.session() as s:
        recorded = await continuation.record_result(
            s,
            env.agent,
            intention_id=it.id,
            source_kind="subtask",
            source_id=st.id,
            msg_type="INFORM",
            title="Check the snow report",
            body=body,
            source_generation=generation,
            settings=env.settings,
            **over,
        )
        await s.commit()
    return recorded


def test_result_recorded_keeps_the_contract_field_order():  # PIN
    """record_result builds ResultRecorded positionally: its fields are contract section 4.7's, in order."""
    assert [f.name for f in dataclasses.fields(continuation.ResultRecorded)] == [
        "inbox_id",
        "inserted",
        "state_after",
        "reopened",
        "reported",
        "intention_id",
        "root_id",
    ]
    recorded = continuation.ResultRecorded(None, False, "pending", False, False, uuid.uuid4(), uuid.uuid4())
    with pytest.raises(dataclasses.FrozenInstanceError):
        recorded.inserted = True  # type: ignore[misc]


async def test_a_pending_continue_intention_moves_to_result_ready_with_its_row(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    st = await make_subtask(env)
    recorded = await _record(env, st)
    it = await intention_of(env, "subtask", st.id)
    (row,) = await inbox_rows(env, st.id)
    assert (recorded.inserted, recorded.state_after, recorded.reopened, recorded.reported) == (
        True,
        "result_ready",
        False,
        False,
    )
    assert recorded.inbox_id == row.id and recorded.intention_id == it.id and recorded.root_id == it.root_id
    assert (row.channel, row.session_id, row.intention_id, row.source_kind) == (None, None, it.id, "subtask")
    assert (it.state, it.close_reason) == ("result_ready", None) and it.result_at is not None


async def test_a_fault_after_the_insert_leaves_neither_row_nor_move(env_factory, monkeypatch):  # noqa: F811
    """The row and the move are one transaction: a fault between them leaves the
    intention pending and no row, and the next arrival succeeds."""
    env = await env_factory(**CONT)
    st = await make_subtask(env)
    real = continuation._set_result_ready

    async def boom(*args, **kwargs):
        raise RuntimeError("fault between the INSERT and the UPDATE")

    monkeypatch.setattr(continuation, "_set_result_ready", boom)
    with pytest.raises(RuntimeError, match="fault between"):
        await _record(env, st)
    assert await inbox_rows(env, st.id) == []
    assert (await intention_of(env, "subtask", st.id)).state == "pending"
    monkeypatch.setattr(continuation, "_set_result_ready", real)
    assert (await _record(env, st)).state_after == "result_ready"
    assert len(await inbox_rows(env, st.id)) == 1


async def test_a_duplicate_arrival_changes_nothing(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    st = await make_subtask(env)
    assert (await _record(env, st)).inserted is True
    it = await intention_of(env, "subtask", st.id)
    await set_intention(env, it.id, state="closed", close_reason="resolved", closed_at=datetime.now(UTC))
    again = await _record(env, st)  # the same (kind, id, generation): the listener and deliver both write
    assert (again.inserted, again.reopened, again.state_after, again.inbox_id) == (False, False, "closed", None)
    assert (await intention_of(env, "subtask", st.id)).state == "closed"
    assert len(await inbox_rows(env, st.id)) == 1


@pytest.mark.parametrize("state", ["awaiting_owner", "deciding", "result_ready"])
async def test_rows_arriving_while_awaiting_or_deciding_are_held(env_factory, state):  # noqa: F811
    """Spec section 4.3 items 2 and 3: the row is inserted and the intention is left alone."""
    env = await env_factory(**CONT)
    st = await make_subtask(env)
    it = await intention_of(env, "subtask", st.id)
    await set_intention(env, it.id, state=state)
    recorded = await _record(env, st, generation=1)
    assert (recorded.inserted, recorded.state_after, recorded.reopened, recorded.reported) == (
        True,
        state,
        False,
        False,
    )
    (row,) = await inbox_rows(env, st.id)
    assert (row.channel, row.session_id, row.intention_id) == (None, None, it.id)
    assert (await intention_of(env, "subtask", st.id)).state == state


async def test_a_closed_continue_intention_with_an_open_root_reopens(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    st = await make_subtask(env)
    await _record(env, st)
    it = await intention_of(env, "subtask", st.id)
    await set_intention(env, it.id, state="closed", close_reason="resolved", closed_at=datetime.now(UTC))
    recorded = await _record(env, st, generation=1)  # a retried DAG, a decided proposal, an answer
    after = await intention_of(env, "subtask", st.id)
    assert (recorded.inserted, recorded.reopened, recorded.state_after) == (True, True, "result_ready")
    assert (after.state, after.close_reason, after.closed_at) == ("result_ready", None, None)
    assert len(await inbox_rows(env, st.id)) == 2


async def test_a_reopen_clears_the_previous_claim_and_keeps_attempts(env_factory):  # noqa: F811
    """T6: the new arrival starts 2c's lease from scratch; ``attempts`` stays as the last arrival left it
    (2c1-5 ruling). A ``resolved`` close with a count is unreachable in code (a success resets it): this is a
    unit test of the reopen, and the real-path proof is in ``test_f099_phase2c_failure.py``."""
    env = await env_factory(**CONT)
    st = await make_subtask(env)
    await _record(env, st)
    it = await intention_of(env, "subtask", st.id)
    await set_intention(
        env,
        it.id,
        state="closed",
        close_reason="resolved",
        closed_at=datetime.now(UTC),
        claim_token=uuid.uuid4(),
        claimed_at=datetime.now(UTC),
        attempts=2,
    )
    assert (await _record(env, st, generation=1)).reopened is True
    after = await intention_of(env, "subtask", st.id)
    assert (after.state, after.claim_token, after.claimed_at, after.attempts) == ("result_ready", None, None, 2)


def _split(rows):
    """(the source-keyed rows, the intention_report rows) of an environment's inbox."""
    return (
        [r for r in rows if r.source_kind != "intention_report"],
        [r for r in rows if r.source_kind == "intention_report"],
    )


# 2e: the report is the EXPIRED half of the unified late-result rule; a cancelled root is silent (2e tests).
@pytest.mark.parametrize("marker", ["root_expired_at"])
async def test_a_re_arrival_on_a_closed_root_becomes_an_intention_report(env_factory, marker):  # noqa: F811
    env = await env_factory(**CONT)
    st = await make_subtask(env)
    it = await intention_of(env, "subtask", st.id)
    await set_intention(env, it.id, state="closed", close_reason="resolved", **{marker: datetime.now(UTC)})
    recorded = await _record(env, st, generation=1, body="the raw result")
    stamped, (row,) = _split(await inbox_rows(env))  # the report's source_id is its own, not the subtask's
    assert (recorded.inserted, recorded.reported, recorded.reopened, recorded.state_after) == (
        True,
        True,
        False,
        "closed",
    )
    assert (row.source_kind, row.msg_type, row.channel, row.session_id) == ("intention_report", "REPORT", CHAN, None)
    assert row.body == "the raw result" and row.intention_id == it.id
    assert row.source_id == continuation.arrival_report_id("subtask", st.id, 1)
    # inbox_id is the REPORT row's primary key; 2c and 2d key a report on its source_id, which differs
    assert recorded.inbox_id == row.id and recorded.inbox_id != row.source_id
    assert (await intention_of(env, "subtask", st.id)).state == "closed"  # a closed root is never reopened
    again = await _record(env, st, generation=1, body="the raw result")  # the second writer of the same outcome
    assert (again.inserted, again.reported) == (False, False)
    assert len(await inbox_rows(env)) == 2  # the report and its work row's settled twin, once each
    assert len(stamped) == 1


async def test_a_new_generation_of_a_dag_closed_as_legacy_reports_and_never_reopens(env_factory):  # noqa: F811
    """Lead addendum (2b-7 review). F098 already delivered the result of a continue intention that Phase 1
    (or the startup rollback) closed as 'legacy'. A retry_node generation, written through F087's
    record_dag_result, must not reopen it; nor may it write nothing (the MF-1 re-select loop): it becomes
    exactly one REPORT plus the settled source-keyed twin, and the intention stays closed."""
    env = await env_factory(**CONT)
    dag, _ = await make_dag(env, policy="continue", origin_channel=CHAN)
    it = await intention_of(env, "dag", dag.id)
    await set_intention(env, it.id, state="closed", close_reason="legacy", closed_at=datetime.now(UTC))
    generation = dag.delivery_generation + 1
    kwargs = {**dag_kwargs(dag, origin_channel=CHAN), "generation": generation}
    first = await record_dag_result(env.heart.result_inbox, env.settings, **kwargs)
    second = await record_dag_result(env.heart.result_inbox, env.settings, **kwargs)  # the listener and deliver
    assert (first, second) == (True, False)
    (twin,), (report,) = _split(await inbox_rows(env))
    assert (report.msg_type, report.channel, report.intention_id) == ("REPORT", CHAN, it.id)
    assert (twin.source_id, twin.source_generation, twin.channel, twin.session_id) == (dag.id, generation, None, None)
    assert twin.delivered_at is not None
    after = await intention_of(env, "dag", dag.id)
    assert (after.state, after.close_reason) == ("closed", "legacy")


async def test_a_duplicate_delivery_after_a_root_expiry_writes_no_report(env_factory):  # noqa: F811
    """The REPORT is written only with a newly written settled twin. Generation 0 landed on the continue
    path and the root expired before anything consumed it: a duplicate of generation 0 (the bus
    listener and deliver both write) changes nothing. A real re-arrival reports once, however often it comes."""
    env = await env_factory(**CONT)
    st = await make_subtask(env)
    assert (await _record(env, st)).inserted is True
    it = await intention_of(env, "subtask", st.id)
    await set_intention(env, it.id, root_expired_at=datetime.now(UTC))
    again = await _record(env, st)
    assert (again.inbox_id, again.inserted, again.reported) == (None, False, False)
    stamped, reports = _split(await inbox_rows(env))
    assert reports == [] and len(stamped) == 1 and stamped[0].delivered_at is None  # still the continue row
    first, second = await _record(env, st, generation=1), await _record(env, st, generation=1)
    assert (first.reported, second.reported) == (True, False)
    _, reports = _split(await inbox_rows(env))
    assert [r.msg_type for r in reports] == ["REPORT"]


@pytest.mark.postgres_only  # another transaction moves the intention and commits while this session is open
async def test_record_result_reads_the_locked_row_not_the_callers_identity_map(env_factory):  # noqa: F811
    """record_result locks the intention by its columns, not as an ORM entity. A SELECT ... FOR UPDATE of
    the entity returns the object the caller's session already holds without refreshing it, so it would
    decide on that stale state: here 'pending', where the committed state is 'awaiting_owner'."""
    env = await env_factory(**CONT)
    st = await make_subtask(env)
    it = await intention_of(env, "subtask", st.id)
    async with env.db.session() as s:
        held = await s.get(Intention, it.id)  # the caller's own view, loaded first
        assert held.state == "pending"
        await set_intention(env, it.id, state="awaiting_owner")  # another transaction moves it and commits
        recorded = await continuation.record_result(
            s,
            env.agent,
            intention_id=it.id,
            source_kind="subtask",
            source_id=st.id,
            msg_type="INFORM",
            title="t",
            body="b",
            settings=env.settings,
        )
        await s.commit()
    assert (recorded.inserted, recorded.state_after) == (True, "awaiting_owner")  # held, not moved
    assert (await intention_of(env, "subtask", st.id)).state == "awaiting_owner"


async def test_a_reported_result_settles_its_work_row_for_the_reconciler_passes(env_factory):  # noqa: F811
    """MF-1. The F098 passes decide 'needs repair' by a source-keyed row (has_row). A result that became a
    report must leave one, NULL-keyed and already delivered, or the passes re-select the work row on every
    tick (the pass-level pin is in Task 2b-7). It must never be claimable by a chat turn."""
    env = await env_factory(**CONT)
    st = await make_subtask(env)
    it = await intention_of(env, "subtask", st.id)
    await set_intention(env, it.id, state="closed", close_reason="resolved", root_expired_at=datetime.now(UTC))
    await _record(env, st, generation=0)
    (stamped,), _ = _split(await inbox_rows(env))
    assert (stamped.source_kind, stamped.source_id, stamped.source_generation) == ("subtask", st.id, 0)
    assert (stamped.channel, stamped.session_id, stamped.intention_id) == (None, None, it.id)
    assert stamped.delivered_at is not None and stamped.delivered_session_id.startswith("report:")
    rows, _ = await env.heart.result_inbox.claim(channel=CHAN, session_id="S1", max_age_hours=72, max_items=10)
    assert [r.source_kind for r in rows] == ["intention_report"]  # the report only, never the settled twin


async def test_a_report_of_an_old_result_gets_a_fresh_claim_window_and_its_twin_keeps_the_works_time(env_factory):  # noqa: F811
    """An owner-facing row's created_at is when the owner can see it, so F098's claim window starts then.
    The reconciler passes hand record_result the work's completed_at; here the result finished 100 h
    ago (the passes themselves select only the last 72 h, so the call is made directly, with the
    created_at they would pass). The settled twin is the work row's record and keeps that time."""
    env = await env_factory(**CONT)
    st = await make_subtask(env)
    it = await intention_of(env, "subtask", st.id)
    await set_intention(env, it.id, state="closed", close_reason="resolved", root_expired_at=datetime.now(UTC))
    finished = datetime.now(UTC) - timedelta(hours=100)
    before = datetime.now(UTC)
    assert (await _record(env, st, created_at=finished)).reported is True
    (twin,), (report,) = _split(await inbox_rows(env))
    assert twin.created_at == finished and report.created_at >= before
    rows, _ = await env.heart.result_inbox.claim(channel=CHAN, session_id="S9", max_age_hours=72, max_items=10)
    assert [r.id for r in rows] == [report.id]


async def test_an_expired_intention_reports_instead_of_waking(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    st = await make_subtask(env)
    it = await intention_of(env, "subtask", st.id)
    await set_intention(env, it.id, state="expired")
    recorded = await _record(env, st)
    assert (recorded.reported, recorded.state_after) == (True, "expired")


async def test_a_result_with_no_owner_channel_writes_only_the_settled_work_row(env_factory, caplog):  # noqa: F811
    env = await env_factory(**CONT)  # no default chat configured
    st = await make_subtask(env, routed=False)  # and no origin channel
    it = await intention_of(env, "subtask", st.id)
    await set_intention(env, it.id, root_expired_at=datetime.now(UTC))
    recorded = await _record(env, st)
    assert (recorded.inserted, recorded.reported) == (False, False)
    stamped, reports = _split(await inbox_rows(env))
    assert reports == [] and len(stamped) == 1 and stamped[0].delivered_at is not None
    assert "no owner channel" in caplog.text


async def test_a_report_falls_back_to_the_default_chat(env_factory):  # noqa: F811
    env = await env_factory(**CONT, telegram_chat_id="4242")
    st = await make_subtask(env, routed=False)
    it = await intention_of(env, "subtask", st.id)
    await set_intention(env, it.id, root_expired_at=datetime.now(UTC))
    await _record(env, st)
    _, (row,) = _split(await inbox_rows(env))
    assert row.channel == "telegram:4242"


async def test_a_non_continue_intention_reports_directly(env_factory):  # noqa: F811
    """Contract C8: the spec's 'any other policy' rule, applied by record_result itself."""
    env = await env_factory(**CONT)
    st = await make_subtask(env, policy="report")
    recorded = await _record(env, st)
    _, (row,) = _split(await inbox_rows(env))
    assert (recorded.reported, recorded.state_after) == (True, "pending")
    assert (row.source_kind, row.channel) == ("intention_report", CHAN)
    assert (await intention_of(env, "subtask", st.id)).state == "pending"


async def test_an_arrival_id_reaches_the_row(env_factory):  # noqa: F811
    """SF-2: 2d's record_answer passes the question's arrival."""
    env = await env_factory(**CONT)
    st = await make_subtask(env)
    arrival = uuid.uuid4()
    await _record(env, st, arrival_id=arrival)
    (row,) = await inbox_rows(env, st.id)
    assert row.arrival_id == arrival


async def test_an_unknown_intention_raises(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    st = await make_subtask(env)
    async with env.db.session() as s:
        with pytest.raises(LookupError):
            await continuation.record_result(
                s,
                env.agent,
                intention_id=uuid.uuid4(),
                source_kind="subtask",
                source_id=st.id,
                msg_type="INFORM",
                title="t",
                body="b",
                settings=env.settings,
            )


@pytest.mark.postgres_only  # two writers contend for one FOR UPDATE row lock
async def test_two_writers_on_one_intention_both_land_and_move_it_once(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    st = await make_subtask(env)
    first, second = await asyncio.wait_for(  # bounded: a lock that is never released fails, never hangs
        asyncio.gather(_record(env, st, generation=0), _record(env, st, generation=1)), timeout=30
    )
    assert first.inserted and second.inserted
    assert {first.state_after, second.state_after} == {"result_ready"}
    assert len(await inbox_rows(env, st.id)) == 2
    assert (await intention_of(env, "subtask", st.id)).state == "result_ready"


async def _until_a_backend_waits_on_a_lock(env) -> None:
    while True:
        async with env.db.session() as s:  # a fresh transaction each time: the stats view is snapshotted
            waiting = (
                await s.execute(
                    text(
                        "SELECT count(*) FROM pg_stat_activity "
                        "WHERE datname = current_database() AND wait_event_type = 'Lock'"
                    )
                )
            ).scalar_one()
        if waiting:
            return
        await asyncio.sleep(0.05)


@pytest.mark.postgres_only  # another transaction holds the intention's row while a writer arrives
async def test_a_writer_waits_for_a_concurrent_move_and_then_holds_its_row(env_factory):  # noqa: F811
    """The FOR UPDATE, deterministically: a writer arriving while another transaction moves the
    intention reads the state that transaction commits and holds its row; without the lock it would
    read the old state and fail its own conditional UPDATE."""
    env = await env_factory(**CONT)
    st = await make_subtask(env)
    it = await intention_of(env, "subtask", st.id)
    async with env.db.session() as holder:
        await holder.execute(text("SET LOCAL idle_in_transaction_session_timeout = '30s'"))
        await holder.execute(update(Intention).where(Intention.id == it.id).values(state="result_ready"))
        writer = asyncio.create_task(_record(env, st, generation=1))
        try:
            await asyncio.wait_for(_until_a_backend_waits_on_a_lock(env), timeout=10)
        finally:
            await holder.commit()  # always release the row
    recorded = await asyncio.wait_for(writer, timeout=10)
    assert (recorded.inserted, recorded.state_after, recorded.reopened) == (True, "result_ready", False)
    assert (await intention_of(env, "subtask", st.id)).state == "result_ready"


class _CommittedStateBus:
    """Records each event and the intention's state as a fresh session reads it at emit time."""

    def __init__(self, env) -> None:
        self.env = env
        self.events: list = []
        self.seen: list[str] = []

    async def emit(self, event) -> None:
        self.events.append(event)
        async with self.env.db.session() as s:
            intention_id = uuid.UUID(event.data["intention_id"])
            self.seen.append(
                (await s.execute(select(Intention.state).where(Intention.id == intention_id))).scalar_one()
            )


async def test_the_store_emits_result_ready_after_the_commit(env_factory):  # noqa: F811
    """The emit follows the commit: a fresh session already reads 'result_ready' when the event goes out
    (an emit inside the writer's transaction would see 'pending')."""
    env = await env_factory(**CONT)
    env.bus = _CommittedStateBus(env)
    store = env.heart.result_inbox
    store.set_bus(env.bus)
    assert store.bus is env.bus
    st = await make_subtask(env)
    it = await intention_of(env, "subtask", st.id)
    kwargs = dict(
        intention_id=it.id,
        source_kind="subtask",
        source_id=st.id,
        correlation_id=None,
        created_at=None,
        settings=env.settings,
    )
    first = await store.record_continue_result(generation=0, envelope=Envelope("INFORM", "t", "b"), **kwargs)
    assert first.state_after == "result_ready"
    (event,) = env.bus.events
    assert event.type == "intention.result_ready"
    assert event.data == {"intention_id": str(it.id), "root_id": str(it.root_id), "agent_id": env.agent}
    assert env.bus.seen == ["result_ready"]
    await store.record_continue_result(generation=0, envelope=Envelope("INFORM", "t", "b"), **kwargs)  # duplicate
    assert len(env.bus.events) == 1


async def test_a_held_row_and_a_report_emit_nothing(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    store = env.heart.result_inbox
    store.set_bus(env.bus)
    st = await make_subtask(env)
    it = await intention_of(env, "subtask", st.id)
    await set_intention(env, it.id, state="awaiting_owner")
    kwargs = dict(
        intention_id=it.id,
        source_kind="subtask",
        source_id=st.id,
        generation=1,
        correlation_id=None,
        created_at=None,
        envelope=Envelope("INFORM", "t", "b"),
        settings=env.settings,
    )
    await store.record_continue_result(**kwargs)
    await set_intention(env, it.id, state="closed", root_cancelled_at=datetime.now(UTC))
    await store.record_continue_result(**{**kwargs, "generation": 2})
    assert env.bus.events == []


async def test_a_bus_failure_never_fails_the_write(env_factory):  # noqa: F811
    class _BrokenBus:
        async def emit(self, event):
            raise RuntimeError("bus down")

    env = await env_factory(**CONT)
    store = env.heart.result_inbox
    store.set_bus(_BrokenBus())
    st = await make_subtask(env)
    it = await intention_of(env, "subtask", st.id)
    recorded = await store.record_continue_result(
        intention_id=it.id,
        source_kind="subtask",
        source_id=st.id,
        generation=0,
        correlation_id=None,
        created_at=None,
        envelope=Envelope("INFORM", "t", "b"),
        settings=env.settings,
    )
    assert recorded.inserted is True and (await intention_of(env, "subtask", st.id)).state == "result_ready"
