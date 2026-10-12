"""F099 Phase 3a: the publisher's card leg (contract sections 4.4, 4.5, 4.7; owner answer 2; S7, S10)."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import pytest
from f099_cards_support import (  # noqa: F401
    age_question,
    answer,
    ask,
    card_row,
    cards_env,
    drain,
    fake_http,
    later,
    live_cards,
)
from f099_support import (
    ask_with_proposals,
    env_factory,  # noqa: F401
    proposal_row,
    runner_env,  # noqa: F401
)
from sqlalchemy import select, update

from nous.a2ui.intention_keys import proposal_key, question_key
from nous.brain import continuation
from nous.storage.models import A2uiSurface, ResultInbox

pytestmark = pytest.mark.postgres_only


async def _stored(env, msg_type):
    async with env.db.session() as s:
        return (
            await s.execute(
                select(ResultInbox).where(ResultInbox.agent_id == env.agent, ResultInbox.msg_type == msg_type)
            )
        ).scalar_one()


async def test_a_due_proposal_gets_a_card_beside_its_telegram_message(cards_env):  # noqa: F811
    env = await cards_env(fcm=True)
    (pid,) = (await ask_with_proposals(env)).ids
    assert await env.publisher.push_due(now=later()) == 1
    await drain(env.service)
    card = await card_row(env, proposal_key(pid))
    proposal = await proposal_row(env, pid)
    assert (card.status, card.kind, card.origin, card.priority) == ("live", "intention_proposal", "continuation", 2)
    assert card.allowed_actions == ["intention.approve", "intention.reject"]
    assert card.title == f"Approval needed {pid.hex[:8]}" and card.data_model["proposal_id"] == str(pid)
    # the card expires with the proposal: `deadline - now` on the real clock (push_due's `now=` is 10 h ahead)
    expected = datetime.now(UTC) + (proposal.deadline - later())
    assert abs((card.expires_at - expected).total_seconds()) < 5
    assert card.trace_id is None
    assert env.http.post.await_count == 1  # the Telegram message with its buttons is still sent (owner answer 2)
    assert "reply_markup" in env.http.post.call_args.kwargs["json"]
    assert env.push.surfaces == [card.surface_id] and env.pings == []  # one phone alert, no [companion] ping


async def test_a_due_question_gets_a_card_beside_its_telegram_message(cards_env):  # noqa: F811
    env = await cards_env(fcm=True)
    _root, qid, _done = await ask(env)
    assert await env.publisher.push_due(now=later()) == 1
    await drain(env.service)
    card = await card_row(env, question_key(qid))
    assert (card.status, card.kind, card.title) == ("live", "intention_question", "Question from Nous")
    assert card.allowed_actions == ["intention.answer"] and card.data_model["answer"] == ""
    assert env.http.post.await_count == 1 and env.push.surfaces == [card.surface_id] and env.pings == []


@pytest.mark.parametrize("switch", [True, False])
@pytest.mark.parametrize("fcm", [True, False])
async def test_the_notification_matrix_is_one_alert_and_one_telegram_message(cards_env, fcm, switch):  # noqa: F811
    """Owner answer 2 / contract 4.7: FCM configured = 1 FCM alert + 1 publisher Telegram message; unconfigured = 1
    Telegram message; the switch makes no difference; never a ``[companion]`` ping."""
    env = await cards_env(fcm=fcm, a2ui_telegram_notify_enabled=switch)
    await ask_with_proposals(env)
    await ask(env)
    assert await env.publisher.push_due(now=later()) == 2
    await drain(env.service)
    assert len(env.push.surfaces) == (2 if fcm else 0)
    assert env.pings == []
    assert env.http.post.await_count == 2  # one Telegram message per row


async def test_a_transient_telegram_failure_then_a_retry_replaces_the_card_and_alerts_once(cards_env):  # noqa: F811
    env = await cards_env(fcm=True, http_status=503)
    (pid,) = (await ask_with_proposals(env)).ids
    assert await env.publisher.push_due(now=later()) == 0  # transient: nothing counted, the row stays due
    first = await card_row(env, proposal_key(pid))
    assert (await _stored(env, "PROPOSAL")).pushed_at is None
    env.publisher._http = fake_http(status=200)
    assert await env.publisher.push_due(now=later()) == 1
    await drain(env.service)
    second = await card_row(env, proposal_key(pid))
    assert second.surface_id == first.surface_id and second.nonce != first.nonce  # replaced in place
    assert env.push.surfaces == [first.surface_id] and env.pings == []  # a replacement never notifies
    assert (await _stored(env, "PROPOSAL")).pushed_at is not None
    assert len(await live_cards(env)) == 1


async def test_a_replacement_resets_a_half_typed_answer(cards_env):  # noqa: F811
    """S10, accepted: the card is rebuilt whole on a replacement push, so text typed in the field is lost."""
    env = await cards_env(fcm=True, http_status=503)
    _root, qid, _done = await ask(env)
    await env.publisher.push_due(now=later())
    env.publisher._http = fake_http(status=200)
    await env.publisher.push_due(now=later())
    assert (await card_row(env, question_key(qid))).data_model["answer"] == ""


async def test_quiet_hours_defer_both_legs(cards_env):  # noqa: F811
    env = await cards_env(fcm=True)
    await ask_with_proposals(env)
    morning = datetime.now(UTC) + timedelta(hours=2)
    async with env.db.session() as s:
        await s.execute(
            update(ResultInbox)
            .where(ResultInbox.agent_id == env.agent, ResultInbox.msg_type == "PROPOSAL")
            .values(push_after=morning)
        )
        await s.commit()
    assert await env.publisher.push_due(now=morning - timedelta(hours=1)) == 0
    await drain(env.service)
    assert env.http.post.await_count == 0 and await live_cards(env) == [] and env.push.surfaces == []
    assert await env.publisher.push_due(now=morning) == 1
    assert len(await live_cards(env)) == 1


async def test_a_proposal_that_is_no_longer_pending_gets_no_card(cards_env):  # noqa: F811
    """S7: the card leg skips exactly what ``_payload`` skips."""
    env = await cards_env(fcm=True)
    (pid,) = (await ask_with_proposals(env)).ids
    async with env.db.session() as s:
        await continuation.decide_proposal(s, env.agent, pid, approve=False, actor="t", settings=env.settings)
        await s.commit()
    assert await env.publisher.push_due(now=later()) == 0
    assert await card_row(env, proposal_key(pid)) is None and env.http.post.await_count == 0


@pytest.mark.parametrize("how", ["answered", "window passed", "root cancelled"])
async def test_a_question_that_nothing_waits_for_gets_no_card(cards_env, how):  # noqa: F811
    env = await cards_env(fcm=True)
    root, qid, done = await ask(env)
    if how == "answered":
        await answer(env, qid)
    elif how == "window passed":
        await age_question(env, done.arrival_id)
    else:
        async with env.db.session() as s:
            await continuation.cancel_root(s, env.agent, root.id, reason="t", actor="t")
            await s.commit()
    await env.publisher.push_due(now=later())
    await drain(env.service)
    assert await card_row(env, question_key(qid)) is None and env.push.surfaces == []
    if how != "root cancelled":  # C10: the Telegram message is unchanged (a cancel closes the unsent rows themselves)
        assert env.http.post.await_count == 1


async def test_a_failing_card_push_never_blocks_the_telegram_message(cards_env, caplog, monkeypatch):  # noqa: F811
    env = await cards_env(fcm=True)
    (pid,) = (await ask_with_proposals(env)).ids

    async def boom(*args, **kwargs):
        raise RuntimeError("model text that must not reach the log")

    monkeypatch.setattr(env.service, "push_built", boom)
    with caplog.at_level(logging.WARNING, logger="nous.handlers.continuation_publisher"):
        assert await env.publisher.push_due(now=later()) == 1
    assert env.http.post.await_count == 1 and (await _stored(env, "PROPOSAL")).pushed_at is not None
    assert "was not pushed (RuntimeError)" in caplog.text and "model text" not in caplog.text


async def test_a_call_too_long_to_show_whole_gets_no_card_and_still_reaches_telegram(
    cards_env,  # noqa: F811
    monkeypatch,
    caplog,
):
    env = await cards_env(fcm=True)
    await ask_with_proposals(env)
    from nous.a2ui.builders import intention_cards

    monkeypatch.setattr(intention_cards, "ARGUMENTS_CHARS", 10)  # the builder refuses what it cannot show whole
    with caplog.at_level(logging.WARNING, logger="nous.handlers.continuation_publisher"):
        assert await env.publisher.push_due(now=later()) == 1
    assert await live_cards(env) == [] and env.http.post.await_count == 1
    assert "was not pushed (call)" in caplog.text  # the fixed reason, never the call


async def test_flag_off_creates_no_card_and_calls_no_surface_service(cards_env):  # noqa: F811  # PIN
    """With ``NOUS_INTENTION_CARDS_ENABLED`` off, a publisher pass and a full sweep touch the service not at all.
    Counters, not raising stubs: the card leg and ``_step`` both swallow exceptions, so a raise would pass.
    Mutation: drop the ``cards_enabled`` check from ``_push_card`` and ``push_built`` is called."""
    env = await cards_env(fcm=True, intention_cards_enabled=False)
    seen: list[str] = []

    def spy(name):
        async def counting(*args, **kwargs):
            seen.append(name)
            return [] if name == "live_cards_by_prefix" else None

        return counting

    for name in ("push_built", "close", "live_cards_by_prefix"):
        setattr(env.service, name, spy(name))
    await ask_with_proposals(env)
    await ask(env)
    assert await env.publisher.push_due(now=later()) == 2  # the Telegram side is exactly as before
    await env.cont.run_once()
    assert seen == [] and env.http.post.await_count == 2
    async with env.db.session() as s:
        assert (await s.execute(select(A2uiSurface).where(A2uiSurface.agent_id == env.agent))).first() is None


async def test_an_unbound_service_is_a_no_op_even_with_the_flag_on(cards_env):  # noqa: F811
    env = await cards_env(fcm=True)
    env.cont.set_surface_service(None)  # a deployment without the companion never binds one
    await ask_with_proposals(env)
    assert await env.publisher.push_due(now=later()) == 1
    assert await live_cards(env) == [] and env.http.post.await_count == 1  # Task 12 adds: and the sweep returns 0
