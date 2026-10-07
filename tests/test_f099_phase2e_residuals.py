"""F099 Phase 2e-6: the residuals that must land before the flip: the rollback with nowhere to deliver (9), the answer
window of a question (10), the approved proposal nobody started (11) and the push that must not hold the sweep (12)."""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from f099_support import (
    CONT,
    ON,
    SEND_EMAIL_ARGS,
    SEND_EMAIL_SCHEMA,
    ask_with_proposals,
    claim,
    claimed,
    commit_ask,
    env_factory,  # noqa: F401
    finish,
    inbox_rows,
    intention_of,
    make_child,
    make_root,
    make_subtask,
    proposal_row,
    record,
    register_send_email,
    runner_env,  # noqa: F401
    set_intention,
    until_a_backend_waits_on_a_lock,
)
from sqlalchemy import select, update

import nous.handlers.continuation_runner as runner_module
from nous.brain import continuation
from nous.config import Settings
from nous.handlers.continuation_runner import ContinuationRunner
from nous.heart.result_reconciler import repair_missing_results
from nous.storage.models import Intention, IntentionProposal, ResultInbox

pytestmark = pytest.mark.postgres_only


# ---- carry-over 9: the rollback with nowhere to deliver ----------------------------------------------------------


def _off(env, **over) -> Settings:
    values = {"telegram_bot_token": "", "telegram_chat_id": "", **over}
    return Settings(_env_file=None, agent_id=env.agent, **values)


async def _stuck(env, *, routed=True):
    """A continue intention with its NULL-keyed result, as a flag-on process left it."""
    st = await make_subtask(env, routed=routed)
    await finish(env, st)
    await env.pool._record_inbox(st)
    return st


async def test_a_result_with_no_owner_channel_is_stamped_so_it_is_not_left_undelivered_for_good(env_factory, caplog):  # noqa: F811
    """Ruling: the intention closes (nothing can deliver it), and its row is marked delivered with its own id, so
    the backlog of undelivered, unclaimable results stays at zero. The result stays on its work row."""
    env = await env_factory(**CONT)
    st = await _stuck(env, routed=False)  # no origin channel, and the process has no default chat
    with caplog.at_level(logging.WARNING, logger="nous.brain.continuation"):
        report = await continuation.rollback_at_startup(
            env.db, _off(env, result_inbox_enabled=True), telegram_push=None
        )
    assert (report.closed, report.rerouted_rows, report.undeliverable) == (1, 0, 1)
    assert "no owner channel" in caplog.text
    (row,) = await inbox_rows(env, st.id)
    assert (row.channel, row.session_id) == (None, None)
    assert row.delivered_at is not None and row.delivered_session_id == continuation.ROLLBACK_UNDELIVERABLE_ID
    assert (await intention_of(env, "subtask", st.id)).state == "closed"
    again = await continuation.rollback_at_startup(env.db, _off(env, result_inbox_enabled=True), telegram_push=None)
    assert (again.closed, again.undeliverable) == (0, 0)


async def test_with_the_inbox_off_and_no_telegram_the_rows_are_stamped_too(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    st = await _stuck(env)
    report = await continuation.rollback_at_startup(env.db, _off(env), telegram_push=None)
    assert (report.closed, report.pushed_raw, report.undeliverable) == (1, 0, 1)
    (row,) = await inbox_rows(env, st.id)
    assert row.delivered_session_id == continuation.ROLLBACK_UNDELIVERABLE_ID


async def test_a_transient_push_failure_is_not_undeliverable_and_keeps_everything_open(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    st = await _stuck(env)

    async def failing(_text):
        return False

    report = await continuation.rollback_at_startup(env.db, _off(env), telegram_push=failing)
    assert (report.closed, report.undeliverable) == (0, 0)
    assert (await inbox_rows(env, st.id))[0].delivered_at is None
    assert (await intention_of(env, "subtask", st.id)).state == "result_ready"


async def test_a_deliverable_result_is_still_rerouted_and_counts_nothing_as_undeliverable(env_factory):  # noqa: F811  # PIN
    env = await env_factory(**CONT, telegram_chat_id="4242")
    st = await _stuck(env, routed=False)
    report = await continuation.rollback_at_startup(
        env.db, _off(env, result_inbox_enabled=True, telegram_chat_id="4242"), telegram_push=None
    )
    assert (report.rerouted_rows, report.undeliverable) == (1, 0)
    assert (await inbox_rows(env, st.id))[0].channel == "telegram:4242"


async def test_prods_flags_roll_back_nothing_new(env_factory):  # noqa: F811  # PIN
    """Prod runs inbox and intentions on with continuation off, with a default chat: no flag-on row exists, so the
    rollback finds no open continuation row, and the new branch has nothing to reach."""
    env = await env_factory(**ON, telegram_chat_id="4242")
    report = await continuation.rollback_at_startup(env.db, env.settings, telegram_push=None)
    assert report == continuation.RollbackReport(0, 0, 0, 0, 0)


@pytest.mark.parametrize("chat", ["4242", ""])
async def test_on_phase_1_data_under_prods_flags_the_rollback_stamps_nothing_and_ends_no_proposal(env_factory, chat):  # noqa: F811  # PIN
    """Prod's real data is Phase 1's: channel-keyed rows, routed or not, and pending intentions of finished work.
    None is an open `continue` row, so neither the stamp nor the proposal UPDATE has a row to reach, with or
    without a default chat."""
    env = await env_factory(**ON, telegram_chat_id=chat)
    for routed in (True, False):
        written = await make_subtask(env, routed=routed)
        await finish(env, written)
        await env.pool._record_inbox(written)
    lost = await make_subtask(env, routed=False)
    await finish(env, lost)  # its writer never ran: the legacy sweep closes it, as before
    before = [(r.id, r.channel, r.delivered_at, r.delivered_session_id) for r in await inbox_rows(env)]
    report = await continuation.rollback_at_startup(env.db, env.settings, telegram_push=None)
    assert (report.rerouted_rows, report.expired_proposals, report.pushed_raw, report.undeliverable) == (0, 0, 0, 0)
    assert [(r.id, r.channel, r.delivered_at, r.delivered_session_id) for r in await inbox_rows(env)] == before
    assert (await intention_of(env, "subtask", lost.id)).state == "closed"


async def test_the_flag_off_rollback_ends_an_approved_proposal_so_a_later_flag_on_never_resumes_it(env_factory):  # noqa: F811
    """S6 of the plan review. The owner approved, the process died before the call started, and the operator turned
    the flag off (the rollback closes the intention) and on again inside the proposal window: without this the sweep
    would run a call whose intention was closed, and its outcome would reach nobody."""
    env = await env_factory(**CONT)
    asked = await ask_with_proposals(env, count=2)
    approved, pending = asked.ids
    async with env.db.session() as s:
        await continuation.decide_proposal(
            s, env.agent, approved, approve=True, actor="telegram:42", settings=env.settings
        )
        await s.commit()
    report = await continuation.rollback_at_startup(env.db, _off(env, result_inbox_enabled=True), telegram_push=None)
    assert report.expired_proposals == 2
    for proposal_id, decided_by in ((approved, "telegram:42"), (pending, "system")):  # who approved is kept
        row = await proposal_row(env, proposal_id)
        assert (row.state, row.decided_by) == ("expired", decided_by)
    async with env.db.session() as s:
        resumable = await continuation.stalled_approved_ids(
            s, env.agent, settings=env.settings, now=datetime.now(UTC) + timedelta(hours=1)
        )
    assert resumable == []  # the flag is back on, and there is nothing for the sweep to resume


async def test_the_inbox_metrics_count_a_row_nobody_read_apart_from_the_delivered_ones(env_factory):  # noqa: F811
    """M2 of the plan review: the owner-facing rule of item 9 is that nothing reports the stamp as "delivered"."""
    env = await env_factory(**CONT)
    now = datetime.now(UTC)
    async with env.db.session() as s:
        for session_id in ("S1", continuation.ROLLBACK_UNDELIVERABLE_ID, continuation.SILENT_SESSION_ID, None):
            await continuation.insert_inbox_row(
                s,
                env.agent,
                source_kind="subtask",
                source_id=uuid.uuid4(),
                msg_type="INFORM",
                title="r",
                body="b",
                delivered_at=now if session_id is not None else None,
                delivered_session_id=session_id,
            )
        await s.commit()
    bucket = (await env.heart.result_inbox.metrics(1))["subtask"]
    assert (bucket["created"], bucket["delivered"]) == (4, 1)  # only the row a chat turn read
    assert (bucket["undeliverable"], bucket["closed_by_cancel"]) == (1, 1)
    assert bucket["delivery_rate"] == 0.5  # 1 of the 2 rows that could have been delivered
    assert bucket["latency_p50_s"] is not None


async def test_on_prods_rows_the_inbox_metrics_keep_their_values_and_the_new_buckets_are_empty(env_factory):  # noqa: F811  # PIN
    """The metrics are served in prod. Its rows carry neither stamp (both ids are new in 2e): a row a chat turn
    read, a 2b report twin, the 2b rollback's raw push and an unread row count exactly as they did."""
    env = await env_factory(**ON)
    now = datetime.now(UTC)
    async with env.db.session() as s:
        for session_id in ("S1", "report:0a1b2c3d", continuation.ROLLBACK_SESSION_ID, None):
            await continuation.insert_inbox_row(
                s,
                env.agent,
                source_kind="subtask",
                source_id=uuid.uuid4(),
                msg_type="INFORM",
                title="r",
                body="b",
                delivered_at=now if session_id is not None else None,
                delivered_session_id=session_id,
            )
        await s.commit()
    bucket = (await env.heart.result_inbox.metrics(1))["subtask"]
    assert (bucket["created"], bucket["delivered"], bucket["delivery_rate"]) == (4, 3, 0.75)
    assert (bucket["undeliverable"], bucket["closed_by_cancel"]) == (0, 0)


async def test_the_inbox_off_warning_names_the_rows_it_marked(env_factory, caplog):  # noqa: F811
    env = await env_factory(**CONT)
    st = await _stuck(env)
    (row,) = await inbox_rows(env, st.id)
    with caplog.at_level(logging.WARNING, logger="nous.brain.continuation"):
        await continuation.rollback_at_startup(env.db, _off(env), telegram_push=None)
    assert str(row.id) in caplog.text and str(row.intention_id) in caplog.text


# ---- carry-over 10: a question's answer window starts at its push --------------------------------------------------

T0 = datetime(2026, 10, 7, 2, 0, tzinfo=UTC)  # the small hours: a push deferred to eight


async def _question(env, *, push_after):
    root, got = await claimed(env)
    await commit_ask(env, got)
    (row,) = [r for r in await inbox_rows(env) if r.msg_type == "QUESTION"]
    async with env.db.session() as s:
        await s.execute(
            update(ResultInbox).where(ResultInbox.id == row.id).values(created_at=T0, push_after=push_after)
        )
        await s.commit()
    return root, row


async def _answer(env, question, *, now):
    async with env.db.session() as s:
        out = await continuation.record_answer(
            s, env.agent, question.source_id, text="yes", actor="t", settings=env.settings, now=now
        )
        await s.commit()
    return out


async def test_a_question_deferred_by_quiet_hours_can_be_answered_for_the_whole_window_after_its_push(env_factory):  # noqa: F811
    """The same rule as a proposal's deadline (2d review m2): the window is `max(created_at, push_after) + ttl`.
    Written at 02:00 and pushed at 08:00 with a 24 h window, it is answerable until 08:00 the next day."""
    env = await env_factory(**CONT, intention_proposal_ttl_hours=24)
    root, question = await _question(env, push_after=T0 + timedelta(hours=6))
    assert continuation.question_window_start(await _fresh(env, question)) == T0 + timedelta(hours=6)
    assert (await _answer(env, question, now=T0 + timedelta(hours=29))).woke_arrival is True  # 29 h after the write


async def test_after_that_window_the_question_has_expired(env_factory):  # noqa: F811
    env = await env_factory(**CONT, intention_proposal_ttl_hours=24)
    root, question = await _question(env, push_after=T0 + timedelta(hours=6))
    with pytest.raises(continuation.AnswerRefused) as refused:
        await _answer(env, question, now=T0 + timedelta(hours=31))  # 25 h after the push
    assert refused.value.reason == "expired"


async def test_a_question_with_no_push_time_keeps_the_window_from_its_write(env_factory):  # noqa: F811  # PIN
    env = await env_factory(**CONT, intention_proposal_ttl_hours=24)
    root, question = await _question(env, push_after=None)
    with pytest.raises(continuation.AnswerRefused):
        await _answer(env, question, now=T0 + timedelta(hours=25))


async def test_the_wake_sweep_judges_a_question_by_the_same_window(env_factory):  # noqa: F811
    env = await env_factory(**CONT, intention_proposal_ttl_hours=24)
    root, question = await _question(env, push_after=T0 + timedelta(hours=6))

    async def sweep(now):
        async with env.db.session() as s:
            woken = await continuation.wake_terminal_arrivals(s, env.agent, settings=env.settings, now=now)
            await s.commit()
        return woken

    assert await sweep(T0 + timedelta(hours=29)) == []  # inside the window from the push: still waiting
    assert await sweep(T0 + timedelta(hours=31)) == [root.id]  # past it: expired unanswered, so it wakes


async def _fresh(env, row):
    async with env.db.session() as s:
        return await s.get(ResultInbox, row.id)


# ---- carry-over 11: an approved proposal nobody started ----------------------------------------------------


def _cont(env) -> ContinuationRunner:
    env.cont = ContinuationRunner(
        database=env.db,
        settings=env.settings,
        runner=env.runner,
        heart=env.heart,
        brain=env.brain,
        bus=env.bus,
        dispatcher=env.dispatcher,
    )
    return env.cont


async def _approved_and_stalled(env, *, age_seconds=600):
    """The crash window: the owner's decision committed, and nothing ran the call."""
    asked = await ask_with_proposals(env)
    (pid,) = asked.ids
    async with env.db.session() as s:
        await continuation.decide_proposal(s, env.agent, pid, approve=True, actor="telegram:42", settings=env.settings)
        await s.commit()
    then = datetime.now(UTC) - timedelta(seconds=age_seconds)
    async with env.db.session() as s:
        await s.execute(
            update(IntentionProposal).where(IntentionProposal.id == pid).values(updated_at=then, decided_at=then)
        )
        await s.commit()
    return asked, pid


async def _settle(cont):
    tasks = list(cont._executing)
    if tasks:
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=30)


async def test_the_sweep_resumes_an_approved_proposal_once_and_the_arrival_wakes(runner_env):  # noqa: F811
    env = await runner_env()
    sent = register_send_email(env, text="Message sent.")
    asked, pid = await _approved_and_stalled(env)
    assert (await proposal_row(env, pid)).state == "approved" and sent == []  # the crash left it so
    assert (await intention_of(env, "subtask", asked.root.source_id)).state == "awaiting_owner"  # and the batch waits
    cont = _cont(env)
    await cont.run_once()
    await _settle(cont)
    assert sent == [{**SEND_EMAIL_ARGS, "subject": "Snow 0"}]
    assert (await proposal_row(env, pid)).state == "executed"
    assert (await intention_of(env, "subtask", asked.root.source_id)).state == "result_ready"
    await cont.run_once()
    await _settle(cont)
    assert len(sent) == 1  # at most once: the second sweep finds nothing approved


async def test_a_fresh_approval_is_not_taken_from_the_request_that_is_running_it(runner_env):  # noqa: F811
    env = await runner_env()
    sent = register_send_email(env)
    _asked, pid = await _approved_and_stalled(env, age_seconds=1)
    cont = _cont(env)
    await cont.run_once()
    await _settle(cont)
    assert sent == [] and (await proposal_row(env, pid)).state == "approved"


async def test_an_approval_older_than_the_proposal_window_is_not_honoured_late(runner_env):  # noqa: F811
    env = await runner_env(intention_proposal_ttl_hours=1)
    sent = register_send_email(env)
    _asked, pid = await _approved_and_stalled(env, age_seconds=2 * 3600)
    cont = _cont(env)
    await cont.run_once()
    await _settle(cont)
    assert sent == [] and (await proposal_row(env, pid)).state == "approved"  # the root's TTL ends it


@pytest.mark.parametrize("marker", ["root_cancelled_at", "root_expired_at"])
async def test_an_approval_on_work_that_ended_is_ended_not_resumed(runner_env, marker):  # noqa: F811
    env = await runner_env()
    sent = register_send_email(env)
    asked, pid = await _approved_and_stalled(env)
    await set_intention(env, asked.root.id, **{marker: datetime.now(UTC)})
    cont = _cont(env)
    await cont.run_once()
    await _settle(cont)
    assert sent == [] and (await proposal_row(env, pid)).state in ("cancelled", "expired")


async def test_a_resume_and_an_owners_retap_run_the_call_once(runner_env):  # noqa: F811
    started, release = asyncio.Event(), asyncio.Event()
    env = await runner_env()
    calls = []

    async def send_email(**kwargs):
        calls.append(kwargs)
        started.set()
        await release.wait()
        return {"content": [{"type": "text", "text": "sent"}]}

    env.dispatcher.register("send_email", send_email, SEND_EMAIL_SCHEMA)
    _asked, pid = await _approved_and_stalled(env)
    cont = _cont(env)
    await cont.run_once()  # the sweep starts it
    await asyncio.wait_for(started.wait(), timeout=10)  # the call is running: its claim is `executing`
    assert len(calls) == 1 and (await proposal_row(env, pid)).state == "executing"
    # The owner's re-tap sees a call in flight and returns at once: it starts no other (so it runs before the release).
    out = await asyncio.wait_for(cont.decide_proposal(pid, approve=True, actor="telegram:42"), timeout=30)
    assert out.state == "executing" and len(calls) == 1
    release.set()
    await _settle(cont)
    assert (await proposal_row(env, pid)).state == "executed" and cont._executing_ids == {}


# ---- carry-over 12: the push does not hold the sweep ------------------------------------------------------


class SlowPublisher:
    def __init__(self) -> None:
        self.release, self.calls = asyncio.Event(), 0

    async def push_due(self, limit=20, *, now=None):
        self.calls += 1
        await self.release.wait()
        return 3


async def test_a_slow_push_does_not_delay_a_launch_and_is_never_started_twice(runner_env, monkeypatch):  # noqa: F811
    monkeypatch.setattr(runner_module, "PUSH_WAIT_SECONDS", 0.2)
    from f099_support import use

    env = await runner_env([use("resolve_intention", decision="drop", note="n", progress=False, confidence=0.5)])
    root = await make_root(env)
    await record(env, root)
    publisher = SlowPublisher()
    cont = ContinuationRunner(
        database=env.db,
        settings=env.settings,
        runner=env.runner,
        heart=env.heart,
        brain=env.brain,
        bus=env.bus,
        publisher=publisher,
    )
    report = await asyncio.wait_for(cont.run_once(), timeout=10)  # not the 200 s a stalled Telegram could cost
    assert report.launched == (root.id,) and report.pushed == 0 and publisher.calls == 1
    await asyncio.wait_for(asyncio.gather(*cont._running.values()), timeout=30)
    again = await asyncio.wait_for(cont.run_once(), timeout=10)
    assert again.pushed == 0 and publisher.calls == 1  # one push at a time
    publisher.release.set()
    await asyncio.wait_for(cont._push_task, timeout=10)
    done = await cont.run_once()
    assert done.pushed == 3 and publisher.calls == 2  # the next sweep starts the next push


async def test_stop_lets_a_push_in_flight_finish_its_send_and_then_ends_it(runner_env, monkeypatch):  # noqa: F811
    monkeypatch.setattr(runner_module, "PUSH_WAIT_SECONDS", 0.1)
    monkeypatch.setattr(runner_module, "EXECUTION_GRACE_SECONDS", 0.3)
    env = await runner_env()
    publisher = SlowPublisher()
    cont = ContinuationRunner(
        database=env.db,
        settings=env.settings,
        runner=env.runner,
        heart=env.heart,
        brain=env.brain,
        bus=env.bus,
        publisher=publisher,
    )
    await cont.run_once()
    push = cont._push_task
    assert push is not None and not push.done()
    await cont.stop()
    assert push.cancelled() or push.done()
    assert cont._push_task is None


async def test_a_failing_push_is_logged_and_the_sweep_goes_on(runner_env, caplog):  # noqa: F811
    env = await runner_env()

    class Broken:
        async def push_due(self, limit=20, *, now=None):
            raise RuntimeError("telegram is down")

    cont = ContinuationRunner(
        database=env.db,
        settings=env.settings,
        runner=env.runner,
        heart=env.heart,
        brain=env.brain,
        bus=env.bus,
        publisher=Broken(),
    )
    report = await cont.run_once()
    assert report.pushed == 0 and "owner push failed" in caplog.text
    assert (await cont.run_once()).pushed == 0  # and the next sweep tries again


# ---- 2e-2 review m4: a `continue` commit that leaves nothing open --------------------------------------------


async def _waiting_on_a_child(env):
    """A root with a result to decide and one running child: the executor's `has_open_work` check passes."""
    root = await make_root(env)
    await record(env, root)
    child = await make_child(env, root)
    got = await claim(env, root.id)
    async with env.db.session() as s:
        assert await continuation.has_open_work(s, env.agent, got)
    return root, child, got


async def _close_child(env, child):
    """The model's own `cancel_task` mid-turn, then the repair's tick: the child closes `legacy`, no result."""
    await env.heart.subtasks.cancel(uuid.UUID(child.source_id))
    return await repair_missing_results(env.db, env.heart.result_inbox, env.settings, limit=50)


async def _commit_continue(s, env, got):
    return await continuation.commit_arrival(
        s,
        env.agent,
        got,
        resolution=continuation.Resolution("continue", "waiting on the follow-up", False, 0.6),
        outcome="resolved",
        settings=env.settings,
    )


async def _root_and_reports(env, root):
    async with env.db.session() as s:
        fresh = (await s.execute(select(Intention).where(Intention.id == root.id))).scalar_one()
    return fresh, [r for r in await inbox_rows(env) if r.source_kind == "intention_report"]


async def test_a_continue_committed_after_its_last_child_closed_ends_the_root_with_one_report(env_factory):  # noqa: F811
    """The child closes between the executor's check and the commit. Its close saw the claimed root still open, so
    it ended nothing; the commit is the last chance, and it closes the root and says so, in its own transaction."""
    env = await env_factory(**CONT, telegram_chat_id="8080")
    root, child, got = await _waiting_on_a_child(env)
    await _close_child(env, child)
    assert (await intention_of(env, "subtask", child.source_id)).close_reason == "legacy"
    assert (await _root_and_reports(env, root))[1] == []  # the close found the claimed root open
    async with env.db.session() as s:
        assert await _commit_continue(s, env, got) is not None
        await s.commit()
    fresh, reports = await _root_and_reports(env, root)
    assert fresh.root_expired_at is not None and fresh.state == "closed"
    (report,) = reports
    assert report.msg_type == "REPORT" and root.intent in report.body


async def test_a_close_that_queues_behind_the_commit_ends_the_root_and_the_commit_does_not_report_too(env_factory):  # noqa: F811
    """The other order: the commit holds the root, the child's close waits on it, then sees the `continue`
    arrival with nothing open. One REPORT, from the close."""
    env = await env_factory(**CONT, telegram_chat_id="8080")
    root, child, got = await _waiting_on_a_child(env)
    async with env.db.session() as s:
        assert await _commit_continue(s, env, got) is not None  # holds the root until the commit
        closing = asyncio.create_task(_close_child(env, child))
        await asyncio.wait_for(until_a_backend_waits_on_a_lock(env), timeout=10)
        await s.commit()
    await asyncio.wait_for(closing, timeout=30)
    fresh, reports = await _root_and_reports(env, root)
    assert fresh.root_expired_at is not None and len(reports) == 1


async def test_a_continue_with_a_child_still_running_leaves_the_root_open(env_factory):  # noqa: F811  # PIN
    env = await env_factory(**CONT, telegram_chat_id="8080")
    root, _child, got = await _waiting_on_a_child(env)
    async with env.db.session() as s:
        assert await _commit_continue(s, env, got) is not None
        await s.commit()
    fresh, reports = await _root_and_reports(env, root)
    assert fresh.root_expired_at is None and reports == []


# ---- 2e-1 review m7 and 2e-2 review m1: every silent stamp of a cancel says so ----------------------------------


async def test_a_cancelled_roots_unread_results_count_as_closed_by_cancel_not_delivered(env_factory):  # noqa: F811
    """The cancel stamps the lineage's unread rows `SILENT_SESSION_ID`, not the `intent-<root>` a turn's delivery
    writes, so the metrics' `closed_by_cancel` bucket counts them and `delivered` does not."""
    env = await env_factory(**CONT)
    root = await make_root(env)
    await record(env, root)
    async with env.db.session() as s:
        await continuation.cancel_root(s, env.agent, root.id, reason="t", actor="owner-test")
        await s.commit()
    (row,) = await inbox_rows(env, uuid.UUID(root.source_id))
    assert row.delivered_session_id == continuation.SILENT_SESSION_ID
    bucket = (await env.heart.result_inbox.metrics(1))["subtask"]
    assert (bucket["created"], bucket["delivered"], bucket["closed_by_cancel"]) == (1, 0, 1)
    assert bucket["delivery_rate"] is None  # nothing was deliverable


async def test_a_gate_that_drops_a_cancelled_roots_arrival_stamps_its_rows_closed_by_cancel(env_factory):  # noqa: F811
    """The third silent stamp: a claim taken before the marker, dropped by the gate. Nobody saw its rows; an
    expired root's rows are reported, so they keep the turn's id."""
    env = await env_factory(**CONT)
    stamps = {}
    for marker, reason in (("root_cancelled_at", "cancelled"), ("root_expired_at", "expired")):
        root = await make_root(env)
        await record(env, root)
        got = await claim(env, root.id)
        await set_intention(env, root.id, **{marker: datetime.now(UTC)})
        resolution, report_text = continuation.gate_inputs(reason, got)
        async with env.db.session() as s:
            await continuation.commit_arrival(
                s,
                env.agent,
                got,
                resolution=resolution,
                outcome="resolved",
                gate_reason=reason,
                settings=env.settings,
                report_text=report_text,
            )
            await s.commit()
        (row,) = await inbox_rows(env, uuid.UUID(root.source_id))
        stamps[reason] = (row.delivered_session_id, root.id)
    assert stamps["cancelled"][0] == continuation.SILENT_SESSION_ID
    assert stamps["expired"][0] == f"intent-{stamps['expired'][1]}"


# ---- 2e-2 review: the proposal sweep takes the roots of all its arms in the one order ----------------------------


async def _set_proposal(env, proposal_id, **values):
    async with env.db.session() as s:
        await s.execute(update(IntentionProposal).where(IntentionProposal.id == proposal_id).values(**values))
        await s.commit()


async def _lock_root(s, root_id):
    await s.execute(select(Intention.id).where(Intention.id == root_id).with_for_update(key_share=True))


async def test_the_proposal_sweep_locks_the_roots_of_every_arm_in_one_order_so_a_holder_cannot_deadlock_it(
    env_factory,  # noqa: F811
    caplog,
):
    """Each arm sorted only its own roots, inside one transaction, and a released SAVEPOINT keeps its locks: the stale
    arm took the YOUNGER root, and the pending arm then waited on the OLDER one. Against another holder taking roots
    in the one order (a nested-fire cancel, the expiry), that is a cycle. The sweep now locks every root any arm will
    touch first, in `(created_at, id)` order, so it waits on the older root holding nothing."""
    env = await env_factory(**CONT)
    older, younger = await ask_with_proposals(env), await ask_with_proposals(env)
    now = datetime.now(UTC)
    # The order is the test's, not the clock's.
    await set_intention(env, older.root.id, created_at=now - timedelta(hours=2))
    await set_intention(env, younger.root.id, created_at=now - timedelta(hours=1))
    (due,), (stale,) = older.ids, younger.ids
    await _set_proposal(env, due, deadline=now - timedelta(minutes=1))  # the pending arm: the older root
    await _set_proposal(env, stale, state="staged", created_at=now - timedelta(hours=1))  # the stale arm runs first

    async def sweep():
        async with env.db.session() as s:
            moved = await continuation.expire_proposals(s, env.agent, settings=env.settings)
            await s.commit()
        return moved

    async with env.db.session() as holder:
        await _lock_root(holder, older.root.id)  # a holder in the one order: the older root, then the younger
        task = asyncio.create_task(sweep())
        try:
            await asyncio.wait_for(until_a_backend_waits_on_a_lock(env), timeout=10)  # the sweep waits on the older
            await asyncio.wait_for(_lock_root(holder, younger.root.id), timeout=30)  # the sweep must not hold it
        finally:
            await holder.commit()
    moved = await asyncio.wait_for(task, timeout=30)
    assert sorted(moved) == sorted([(due, "expired"), (stale, "expired")])
    assert "deadlock" not in caplog.text


# ---- 2e-6 review: three unpinned guards (I1, m1, m2), and a system close keeps who approved (m3) -----------------


async def test_two_resumes_of_one_approved_proposal_start_one_execution(runner_env):  # noqa: F811  # PIN
    """I1: while the first start's claim has not committed, the proposal still reads `approved`, so a second resume
    (or a re-tap) that reads it then gets the task already running instead of starting another. The claim would fence
    a second task's call, so the call count alone cannot tell: the number of tasks does."""
    started, release = asyncio.Event(), asyncio.Event()
    env = await runner_env()
    calls = []

    async def send_email(**kwargs):
        calls.append(kwargs)
        started.set()
        await release.wait()
        return {"content": [{"type": "text", "text": "sent"}]}

    env.dispatcher.register("send_email", send_email, SEND_EMAIL_SCHEMA)
    _asked, pid = await _approved_and_stalled(env)
    cont = _cont(env)
    async with env.db.session() as holder:
        await holder.execute(select(IntentionProposal.id).where(IntentionProposal.id == pid).with_for_update())
        try:
            assert await cont._resume_approved() == 1
            await asyncio.wait_for(until_a_backend_waits_on_a_lock(env), timeout=10)  # its claim queues on the row
            assert await cont._resume_approved() == 1  # the second resume reads the same `approved` proposal
            assert len(cont._executing) == 1 and set(cont._executing_ids) == {pid}
        finally:
            await holder.commit()
    await asyncio.wait_for(started.wait(), timeout=10)
    release.set()
    await _settle(cont)
    assert len(calls) == 1 and (await proposal_row(env, pid)).state == "executed"


@pytest.mark.parametrize("marker", ["root_cancelled_at", "root_expired_at"])
async def test_the_resume_never_reads_an_approved_proposal_under_a_marked_root(env_factory, marker):  # noqa: F811  # PIN
    """m1: the resume's own predicates. Through a sweep the proposals' `unrunnable` arm ends the row first, so this
    reads `stalled_approved_ids` directly, with the row still `approved`."""
    env = await env_factory(**CONT)
    asked, pid = await _approved_and_stalled(env)
    async with env.db.session() as s:
        assert await continuation.stalled_approved_ids(s, env.agent, settings=env.settings) == [pid]
    await set_intention(env, asked.root.id, **{marker: datetime.now(UTC)})
    async with env.db.session() as s:
        assert await continuation.stalled_approved_ids(s, env.agent, settings=env.settings) == []
    assert (await proposal_row(env, pid)).state == "approved"  # nothing else moved it: the predicate alone


async def test_stop_waits_for_a_push_in_flight_to_finish_its_send(runner_env, monkeypatch):  # noqa: F811  # PIN
    """m2: `stop()` lets a push in flight finish (bounded by the grace) before it ends it."""
    monkeypatch.setattr(runner_module, "PUSH_WAIT_SECONDS", 0.1)
    monkeypatch.setattr(runner_module, "EXECUTION_GRACE_SECONDS", 10.0)
    env = await runner_env()
    publisher = SlowPublisher()
    cont = ContinuationRunner(
        database=env.db,
        settings=env.settings,
        runner=env.runner,
        heart=env.heart,
        brain=env.brain,
        bus=env.bus,
        publisher=publisher,
    )
    await cont.run_once()
    push = cont._push_task
    assert push is not None and not push.done()

    async def release_soon():
        await asyncio.sleep(0.2)
        publisher.release.set()

    releaser = asyncio.create_task(release_soon())
    await asyncio.wait_for(cont.stop(), timeout=30)
    await asyncio.wait_for(releaser, timeout=10)
    assert not push.cancelled() and push.result() == 3


async def _end_by_cancel(env, asked):
    async with env.db.session() as s:
        await continuation.cancel_root(s, env.agent, asked.root.id, reason="t", actor="owner-test")
        await s.commit()


async def _end_by_expiry(env, asked):
    later = datetime.now(UTC) + timedelta(hours=100)
    async with env.db.session() as s:
        expired = await continuation.expire_roots(
            s, env.agent, ttl_hours=env.settings.intention_root_ttl_hours, settings=env.settings, now=later
        )
        await s.commit()
    assert expired == [asked.root.id]


async def _end_by_rollback(env, asked):
    report = await continuation.rollback_at_startup(env.db, _off(env, result_inbox_enabled=True), telegram_push=None)
    assert report.expired_proposals == 3  # the staged one too


@pytest.mark.parametrize(
    "end", [_end_by_cancel, _end_by_expiry, _end_by_rollback], ids=["cancel", "expiry", "rollback"]
)
async def test_a_system_close_keeps_who_approved_and_names_the_system_on_the_rest(env_factory, end):  # noqa: F811
    """m3: `decided_by` keeps the owner who approved when the system later ends the proposal (a cancel, the TTL or
    the flag-off rollback), so the row still says the owner approved; a pending one the system ended says `system`.
    `decided_at` follows the same rule (2e-7 review): the approved row keeps when the owner decided, the pending one
    gets the close's time, and a staged one, never shown, keeps no decision at all."""
    env = await env_factory(**CONT)
    asked = await ask_with_proposals(env, count=3)
    approved, pending, staged = asked.ids
    await _set_proposal(env, staged, state="staged")
    async with env.db.session() as s:
        await continuation.decide_proposal(
            s, env.agent, approved, approve=True, actor="telegram:42", settings=env.settings
        )
        await s.commit()
    approved_at = (await proposal_row(env, approved)).decided_at
    assert approved_at is not None
    await end(env, asked)
    rows = [await proposal_row(env, p) for p in (approved, pending, staged)]
    assert all(row.state in ("cancelled", "expired") for row in rows)
    assert [row.decided_by for row in rows] == ["telegram:42", "system", None]
    assert rows[0].decided_at == approved_at and rows[1].decided_at is not None and rows[2].decided_at is None
