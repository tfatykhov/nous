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
    claimed,
    commit_ask,
    env_factory,  # noqa: F401
    finish,
    inbox_rows,
    intention_of,
    make_root,
    make_subtask,
    proposal_row,
    record,
    register_send_email,
    runner_env,  # noqa: F401
    set_intention,
)
from sqlalchemy import update

import nous.handlers.continuation_runner as runner_module
from nous.brain import continuation
from nous.config import Settings
from nous.handlers.continuation_runner import ContinuationRunner
from nous.storage.models import IntentionProposal, ResultInbox

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
    for proposal_id in (approved, pending):
        row = await proposal_row(env, proposal_id)
        assert (row.state, row.decided_by) == ("expired", "system")
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
