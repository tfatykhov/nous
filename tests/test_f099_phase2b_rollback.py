"""F099 Phase 2b: the startup rollback, with the continuation flag off (both flags off included)."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

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
    make_subtask,
    set_intention,
)
from sqlalchemy import select

from nous.brain import continuation
from nous.brain.intentions import IntentionSpec
from nous.config import Settings
from nous.storage.models import IntentionProposal

pytestmark = pytest.mark.postgres_only  # the sweep half uses a CAST(text AS uuid) join


def _off(env, **over) -> Settings:
    """The settings of the process that restarts with the flags off (the same agent)."""
    values = {"telegram_bot_token": "", "telegram_chat_id": "", **over}
    return Settings(_env_file=None, agent_id=env.agent, **values)


async def _stuck(env, *, state: str = "result_ready", routed: bool = True):
    """A continue intention with its NULL-keyed result, as a flag-on process left it."""
    st = await make_subtask(env, routed=routed)
    await finish(env, st)
    await env.pool._record_inbox(st)
    it = await intention_of(env, "subtask", st.id)
    if state != "result_ready":
        await set_intention(env, it.id, state=state, claim_token=uuid.uuid4(), claimed_at=datetime.now(UTC))
    return st, await intention_of(env, "subtask", st.id)


async def _proposal(env, it, state: str) -> uuid.UUID:
    async with env.db.session() as s:
        row = IntentionProposal(
            agent_id=env.agent,
            intention_id=it.id,
            root_id=it.root_id,
            tool="send_email",
            arguments={},
            rationale="r",
            claim_token=uuid.uuid4(),
            state=state,
        )
        s.add(row)
        await s.commit()
        return row.id


async def _proposal_state(env, pid) -> str:
    async with env.db.session() as s:
        return (await s.execute(select(IntentionProposal.state).where(IntentionProposal.id == pid))).scalar_one()


@pytest.mark.parametrize("state", ["result_ready", "deciding", "awaiting_owner"])
async def test_the_rollback_reroutes_expires_and_closes_with_both_flags_off(env_factory, state):  # noqa: F811
    env = await env_factory(**CONT)
    st, it = await _stuck(env, state=state)
    staged = await _proposal(env, it, "staged")
    pending = await _proposal(env, it, "pending")
    executed = await _proposal(env, it, "executed")
    off = _off(env, result_inbox_enabled=True)  # intentions and continuation both off
    report = await continuation.rollback_at_startup(env.db, off, telegram_push=None)
    assert (report.closed, report.rerouted_rows, report.expired_proposals, report.pushed_raw) == (1, 1, 2, 0)
    after = await intention_of(env, "subtask", st.id)
    assert (after.state, after.close_reason, after.claim_token, after.claimed_at) == ("closed", "legacy", None, None)
    assert [await _proposal_state(env, p) for p in (staged, pending, executed)] == ["expired", "expired", "executed"]
    (row,) = await inbox_rows(env, st.id)
    assert (row.channel, row.reply_to, row.session_id) == (CHAN, CHAN, None)
    rows, _ = await env.heart.result_inbox.claim(channel=CHAN, session_id="S9", max_age_hours=72, max_items=10)
    assert [r.id for r in rows] == [row.id]  # the next chat turn sees the result F098 style


async def test_a_second_rollback_changes_nothing(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    await _stuck(env)
    off = _off(env, result_inbox_enabled=True)
    await continuation.rollback_at_startup(env.db, off, telegram_push=None)
    again = await continuation.rollback_at_startup(env.db, off, telegram_push=None)
    assert (again.closed, again.rerouted_rows, again.expired_proposals, again.pushed_raw) == (0, 0, 0, 0)


async def test_a_row_with_no_origin_channel_goes_to_the_default_chat(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    st, _ = await _stuck(env, routed=False)
    off = _off(env, result_inbox_enabled=True, telegram_chat_id="4242")
    await continuation.rollback_at_startup(env.db, off, telegram_push=None)
    (row,) = await inbox_rows(env, st.id)
    assert row.channel == "telegram:4242"


async def test_a_row_with_nowhere_to_go_is_left_and_the_intention_still_closes(env_factory, caplog):  # noqa: F811
    env = await env_factory(**CONT)
    st, _ = await _stuck(env, routed=False)
    off = _off(env, result_inbox_enabled=True)  # no default chat either
    with caplog.at_level(logging.WARNING, logger="nous.brain.continuation"):
        report = await continuation.rollback_at_startup(env.db, off, telegram_push=None)
    assert (report.closed, report.rerouted_rows) == (1, 0)
    assert "no owner channel" in caplog.text
    assert (await intention_of(env, "subtask", st.id)).state == "closed"


async def test_an_owner_facing_row_keeps_its_channel_and_a_held_row_is_rerouted(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    st, it = await _stuck(env, state="awaiting_owner")
    async with env.db.session() as s:
        await continuation.insert_report(
            s,
            env.agent,
            kind="QUESTION",
            title="q",
            body="which?",
            channel="telegram:7",
            intention_id=it.id,
            root_id=it.root_id,
        )
        await s.commit()
    await continuation.rollback_at_startup(env.db, _off(env, result_inbox_enabled=True), telegram_push=None)
    by_kind = {r.source_kind: r for r in await inbox_rows(env)}
    assert by_kind["intention_report"].channel == "telegram:7"
    assert by_kind["subtask"].channel == CHAN


async def test_a_pending_intention_whose_source_already_finished_closes_as_legacy(env_factory):  # noqa: F811
    """Task-1.9 carry-over 2: work that finished while the flags were off left its intention pending."""
    env = await env_factory(**ON)
    finished = await make_subtask(env, policy="remember", routed=False)
    await finish(env, finished)  # no writer, no reconciler: the flags went off
    running = await make_subtask(env, policy="remember", routed=False)
    container = await env.heart.schedules.create(
        task="t",
        schedule_type="recurring",
        interval_seconds=1800,
        intention=IntentionSpec(intent="x", origin_kind="interactive", container=True),
    )
    report = await continuation.rollback_at_startup(env.db, _off(env), telegram_push=None)
    assert report.closed == 1
    assert (await intention_of(env, "subtask", finished.id)).close_reason == "legacy"
    assert (await intention_of(env, "subtask", running.id)).state == "pending"
    assert (await intention_of(env, "schedule", container.id)).state == "pending"


async def test_a_flag_on_process_never_rolls_back(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    st, _ = await _stuck(env)
    report = await continuation.rollback_at_startup(env.db, env.settings, telegram_push=None)
    assert (report.closed, report.rerouted_rows, report.expired_proposals, report.pushed_raw) == (0, 0, 0, 0)
    assert (await intention_of(env, "subtask", st.id)).state == "result_ready"
    assert (await inbox_rows(env, st.id))[0].channel is None


# ---- the inbox is off too: a raw Telegram send -------------------------------------------------------------


class _Push:
    def __init__(self, ok: bool = True) -> None:
        self.ok, self.sent = ok, []

    async def __call__(self, text: str) -> bool:
        self.sent.append(text)
        return self.ok


async def test_with_the_inbox_off_the_raw_result_is_sent_by_telegram(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    st, _ = await _stuck(env)
    push = _Push()
    report = await continuation.rollback_at_startup(env.db, _off(env), telegram_push=push)  # inbox off too
    assert (report.closed, report.pushed_raw, report.rerouted_rows) == (1, 1, 0)
    assert len(push.sent) == 1 and RESULT in push.sent[0]
    (row,) = await inbox_rows(env, st.id)
    assert row.delivered_at is not None and row.delivered_session_id == "rollback" and row.channel is None
    assert (await intention_of(env, "subtask", st.id)).state == "closed"


async def test_a_failed_raw_push_keeps_the_intention_open(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    st, _ = await _stuck(env)
    report = await continuation.rollback_at_startup(env.db, _off(env), telegram_push=_Push(ok=False))
    assert (report.closed, report.pushed_raw) == (0, 0)
    assert (await intention_of(env, "subtask", st.id)).state == "result_ready"
    assert (await inbox_rows(env, st.id))[0].delivered_at is None
    retry = await continuation.rollback_at_startup(env.db, _off(env), telegram_push=_Push())  # the next start
    assert (retry.closed, retry.pushed_raw) == (1, 1)


async def test_with_the_inbox_off_and_no_telegram_the_intention_still_closes(env_factory, caplog):  # noqa: F811
    env = await env_factory(**CONT)
    st, _ = await _stuck(env)
    with caplog.at_level(logging.WARNING, logger="nous.brain.continuation"):
        report = await continuation.rollback_at_startup(env.db, _off(env), telegram_push=None)
    assert (report.closed, report.pushed_raw) == (1, 0)
    assert "cannot be delivered" in caplog.text
