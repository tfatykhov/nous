"""F099 Phase 3a: the settle sweep (R1, R2, S11), the double-surface races in both orders, the latest-arrival
rule on cards, a card approve that does not wait, and the card/route parity (contract section 5: 1, 2, 3, 7, 10)."""

from __future__ import annotations

import asyncio

import httpx
import pytest
from f099_cards_support import (
    age_question,
    answer,
    ask,
    card_row,
    cards_env,  # noqa: F401
    later,
    live_cards,
    question_id_of,
    tap,
    wake,
)
from f099_support import (
    SEND_EMAIL_SCHEMA,
    ask_with_proposals,
    claim,
    commit_ask,
    env_factory,  # noqa: F401
    inbox_rows,
    proposal_row,
    register_send_email,
    runner_env,  # noqa: F401
)
from sqlalchemy import delete, update
from starlette.applications import Starlette

from nous.a2ui.builders import approval_gate
from nous.a2ui.intention_keys import ACTIVE_KEY, proposal_key, question_key
from nous.api.intention_routes import build_intention_routes
from nous.brain import continuation
from nous.handlers import continuation_runner
from nous.owner_actions import ANSWER_REFUSALS, DECISION_REFUSALS
from nous.storage.models import IntentionProposal, ResultInbox

pytestmark = pytest.mark.postgres_only


async def _set_proposal(env, pid, **values):
    async with env.db.session() as s:
        await s.execute(update(IntentionProposal).where(IntentionProposal.id == pid).values(**values))
        await s.commit()


async def _until_state(env, pid, state):
    async with asyncio.timeout(10):  # a state that never comes fails the test, it does not hang it
        while (await proposal_row(env, pid)).state != state:
            await asyncio.sleep(0.05)


# ---- the table of 4.8, exactly -------------------------------------------------------------------------------

PROPOSAL_CASES = [
    ("pending", None),
    ("approved", "resolved"),
    ("executing", "resolved"),
    ("executed", "resolved"),
    ("failed", "resolved"),
    ("rejected", "resolved"),
    ("expired", "expired"),
    ("cancelled", "expired"),
]


@pytest.mark.parametrize(("state", "status"), PROPOSAL_CASES)
async def test_a_proposal_card_closes_exactly_when_the_proposal_is_not_pending(cards_env, state, status):  # noqa: F811
    """R1: not 'until the execution ends' but 'as soon as the owner has nothing left to decide'."""
    env = await cards_env()
    (pid,) = (await ask_with_proposals(env)).ids
    await env.publisher.push_due(now=later())
    await _set_proposal(env, pid, state=state)
    closed = await env.cont._settle_cards()
    card = await card_row(env, proposal_key(pid))
    if status is None:
        assert (closed, card.status) == (0, "live")
    else:
        assert (closed, card.status) == (1, status)
    assert await env.cont._settle_cards() == 0  # idempotent


async def test_a_proposal_card_whose_row_vanished_is_closed(cards_env):  # noqa: F811
    env = await cards_env()
    (pid,) = (await ask_with_proposals(env)).ids
    await env.publisher.push_due(now=later())
    async with env.db.session() as s:
        await s.execute(delete(IntentionProposal).where(IntentionProposal.id == pid))
        await s.commit()
    assert await env.cont._settle_cards() == 1
    assert (await card_row(env, proposal_key(pid))).status == "expired"


async def test_cancel_moves_pending_and_approved_proposals_and_the_sweep_closes_their_cards(cards_env):  # noqa: F811
    """S11 (the store's rule), seen from the card: both go to ``cancelled``; an ``executing`` one is left alone."""
    env = await cards_env()
    asked = await ask_with_proposals(env, count=3)
    a, b, c = asked.ids
    await env.publisher.push_due(now=later())
    await _set_proposal(env, b, state="approved")
    await _set_proposal(env, c, state="executing")
    async with env.db.session() as s:
        await continuation.cancel_root(s, env.agent, asked.root.id, reason="t", actor="t")
        await s.commit()
    states = {pid: (await proposal_row(env, pid)).state for pid in asked.ids}
    assert states == {a: "cancelled", b: "cancelled", c: "executing"}
    assert await env.cont._settle_cards() == 3
    statuses = {pid: (await card_row(env, proposal_key(pid))).status for pid in asked.ids}
    assert statuses == {a: "expired", b: "expired", c: "resolved"}  # executing: not pending, so nothing to decide


async def test_a_question_card_closes_when_answered_elsewhere(cards_env):  # noqa: F811
    env = await cards_env()
    _root, qid, _done = await ask(env)
    await env.publisher.push_due(now=later())
    assert await env.cont._settle_cards() == 0 and (await card_row(env, question_key(qid))).status == "live"
    await answer(env, qid)
    assert await env.cont._settle_cards() == 1
    assert (await card_row(env, question_key(qid))).status == "resolved"


@pytest.mark.parametrize("how", ["window passed", "root cancelled", "row vanished"])
async def test_a_question_card_closes_expired_when_nothing_waits_for_the_answer(cards_env, how):  # noqa: F811
    env = await cards_env()
    root, qid, done = await ask(env)
    await env.publisher.push_due(now=later())
    if how == "window passed":
        await age_question(env, done.arrival_id)
    elif how == "root cancelled":
        async with env.db.session() as s:
            await continuation.cancel_root(s, env.agent, root.id, reason="t", actor="t")
            await s.commit()
    else:
        async with env.db.session() as s:
            await s.execute(delete(ResultInbox).where(ResultInbox.source_id == qid, ResultInbox.msg_type == "QUESTION"))
            await s.commit()
    assert await env.cont._settle_cards() == 1
    assert (await card_row(env, question_key(qid))).status == "expired"


async def test_an_old_question_card_closes_as_stale_while_the_new_one_stays(cards_env):  # noqa: F811
    """Invariant 3 on the card side (the #745 scenario): ask, expire, wake, claim, ask again. The first card closes
    although its window is open again; the second stays."""
    env = await cards_env()
    root, first_qid, first_done = await ask(env, note="First question?")
    await env.publisher.push_due(now=later())  # card 1, while it is the only question
    await age_question(env, first_done.arrival_id)
    assert await wake(env) == [root.id]
    second_done = await commit_ask(env, await claim(env, root.id), "Second question?")
    second_qid = await question_id_of(env, second_done.arrival_id)
    await env.publisher.push_due(now=later())  # card 2
    env.settings.intention_proposal_ttl_hours = 48.0  # the first question's answer window is open again
    assert await env.cont._settle_cards() == 1
    assert (await card_row(env, question_key(first_qid))).status == "expired"
    assert (await card_row(env, question_key(second_qid))).status == "live"


async def test_the_sweep_leaves_every_other_card_alone_and_closes_a_malformed_reserved_one(cards_env):  # noqa: F811
    env = await cards_env()
    card = approval_gate(
        {"title": "t", "summary": "s", "risk": "r", "options": [{"id": "a", "label": "A"}, {"id": "b", "label": "B"}]}
    )
    dag_key = "dag-approval:00000000-0000-0000-0000-000000000001"
    await env.service.push_built(card, dedup_key="plain:1", notify=False)
    await env.service.push_built(card, dedup_key=dag_key, reserved_key_ok=True, notify=False)
    await env.service.push_built(card, dedup_key=ACTIVE_KEY, reserved_key_ok=True, notify=False)  # 3b's card
    await env.service.push_built(card, dedup_key="intention:proposal:garbage", reserved_key_ok=True, notify=False)
    assert await env.cont._settle_cards() == 1
    assert (await card_row(env, "intention:proposal:garbage")).status == "expired"
    for key in ("plain:1", dag_key, ACTIVE_KEY):
        assert (await card_row(env, key)).status == "live", key


async def test_the_sweep_closes_at_most_a_batch_and_an_error_does_not_stop_the_rest(cards_env, monkeypatch):  # noqa: F811
    env = await cards_env()
    asked = await ask_with_proposals(env, count=3)
    await env.publisher.push_due(now=later())
    for pid in asked.ids:
        await _set_proposal(env, pid, state="rejected")
    monkeypatch.setattr(continuation_runner, "SETTLE_CLOSE_BATCH", 2)
    assert await env.cont._settle_cards() == 2
    assert await env.cont._settle_cards() == 1
    assert await env.cont._settle_cards() == 0
    # an error on one card: logged, the others still close, the failed one is tried again next pass
    env2 = await cards_env()
    asked2 = await ask_with_proposals(env2, count=3)
    await env2.publisher.push_due(now=later())
    for pid in asked2.ids:
        await _set_proposal(env2, pid, state="rejected")
    real, seen = env2.service.close, []

    async def flaky(surface_id, status="expired"):
        seen.append(surface_id)
        if len(seen) == 1:
            raise RuntimeError("boom")
        await real(surface_id, status)

    monkeypatch.setattr(env2.service, "close", flaky)
    assert await env2.cont._settle_cards() == 2
    monkeypatch.setattr(env2.service, "close", real)
    assert await env2.cont._settle_cards() == 1


async def test_with_no_service_bound_the_sweep_is_a_no_op(cards_env):  # noqa: F811
    env = await cards_env()
    (pid,) = (await ask_with_proposals(env)).ids
    await env.publisher.push_due(now=later())
    await _set_proposal(env, pid, state="rejected")
    env.cont.set_surface_service(None)
    assert await env.cont._settle_cards() == 0
    assert (await card_row(env, proposal_key(pid))).status == "live"


async def test_run_once_runs_the_card_settle_step_after_the_question_wake(cards_env):  # noqa: F811
    env = await cards_env()
    names, real = [], env.cont._step

    async def spy(name, step, default=None):
        names.append(name)
        return await real(name, step, default)

    env.cont._step = spy
    await env.cont.run_once()
    assert names.index("question wake") < names.index("card settle") < names.index("approved resume")


# ---- the double surface, both orders (invariant 2) -------------------------------------------------------------

PAIRS = [("approve", "approve"), ("approve", "reject"), ("reject", "approve"), ("reject", "reject")]


@pytest.mark.parametrize("order", ["telegram_first", "card_first"])
@pytest.mark.parametrize(("first", "second"), PAIRS)
async def test_the_two_surfaces_settle_one_proposal_once(cards_env, order, first, second):  # noqa: F811
    env = await cards_env()
    sent = register_send_email(env)
    (pid,) = (await ask_with_proposals(env)).ids
    await env.publisher.push_due(now=later())
    card = await card_row(env, proposal_key(pid))

    async def via_card(action):
        return await tap(env, card.surface_id, f"intention.{action}", {"proposalId": str(pid)}, actor="alice")

    async def via_telegram(action):  # what the REST route and the bot call: wait=True
        return await env.cont.decide_proposal(pid, approve=action == "approve", actor="telegram:42")

    step_one, step_two = (via_telegram, via_card) if order == "telegram_first" else (via_card, via_telegram)
    await step_one(first)
    if first == "approve":
        await _until_state(env, pid, "executed")
    outcome = await step_two(second)

    row = await proposal_row(env, pid)  # exactly one transition, by the first surface
    assert row.state == ("executed" if first == "approve" else "rejected")
    assert row.decided_by == ("telegram:42" if order == "telegram_first" else "companion:alice")
    assert len(sent) == (1 if first == "approve" else 0)
    if order == "telegram_first":  # the second surface is the card
        status, body = outcome
        if first == second:
            assert (status, body["resolved"]) == (200, True)
        else:
            assert status == 422 and body["error"]["message"] == DECISION_REFUSALS["not_pending"]
    else:  # the second surface is Telegram
        assert outcome.changed is False
        assert outcome.refusal == (None if first == second else "not_pending")
    await env.cont._settle_cards()  # at most one pass later the card is gone
    assert (await card_row(env, proposal_key(pid))).status == "resolved"
    assert await live_cards(env) == []


@pytest.mark.parametrize("order", ["telegram_first", "card_first"])
async def test_the_two_surfaces_answer_a_question_once(cards_env, order):  # noqa: F811
    env = await cards_env()
    _root, qid, _done = await ask(env)
    await env.publisher.push_due(now=later())
    card = await card_row(env, question_key(qid))
    model = {
        **card.data_model,
        "answer": "From the card.",
    }  # still shipped, as the renderer does: exercises the shape check

    async def via_card():
        return await tap(
            env, card.surface_id, "intention.answer", {"questionId": str(qid), "text": "From the card."}, model=model
        )

    async def via_telegram():
        try:
            await env.cont.answer_question(qid, text="From Telegram.", actor="telegram:42")
        except continuation.AnswerRefused as refused:
            return refused.reason
        return "recorded"

    if order == "telegram_first":
        assert await via_telegram() == "recorded"
        status, body = await via_card()
        assert status == 422 and body["error"]["message"] == ANSWER_REFUSALS["answered"]
        expected = "From Telegram."
    else:
        status, body = await via_card()
        assert (status, body["resolved"]) == (200, True)
        assert await via_telegram() == "answered"
        expected = "From the card."
    informs = [r for r in await inbox_rows(env) if r.msg_type == "INFORM" and r.title == "Owner's answer"]
    assert [r.body for r in informs] == [expected]  # exactly one stored answer
    await env.cont._settle_cards()
    assert (await card_row(env, question_key(qid))).status == "resolved"


# ---- a card approve does not wait (invariant 7) ----------------------------------------------------------------


async def test_a_card_approve_does_not_wait_for_the_tool_and_the_card_is_already_closed(cards_env):  # noqa: F811
    env = await cards_env()
    started, release, calls = asyncio.Event(), asyncio.Event(), []

    async def send_email(**kwargs):
        calls.append(kwargs)
        started.set()
        await release.wait()
        return {"content": [{"type": "text", "text": "sent"}]}

    env.dispatcher.register("send_email", send_email, SEND_EMAIL_SCHEMA)
    (pid,) = (await ask_with_proposals(env)).ids
    await env.publisher.push_due(now=later())
    card = await card_row(env, proposal_key(pid))
    status, body = await asyncio.wait_for(
        tap(env, card.surface_id, "intention.approve", {"proposalId": str(pid)}), timeout=5
    )
    assert (status, body["message"], body["resolved"]) == (200, "Approved; running", True)
    assert (await card_row(env, proposal_key(pid))).status == "resolved"  # R1: closed at the tap
    await asyncio.wait_for(started.wait(), timeout=10)
    assert (await proposal_row(env, pid)).state == "executing" and pid in env.cont._executing_ids
    assert await env.cont._settle_cards() == 0  # nothing left to close
    release.set()
    await asyncio.wait_for(_until_state(env, pid, "executed"), timeout=10)
    assert len(calls) == 1


# ---- the card and the route leave the same rows (invariant 1) ---------------------------------------------------


async def _route(env, method, path, **kwargs) -> httpx.Response:
    app = Starlette(routes=build_intention_routes(database=env.db, settings=env.settings, continuation_runner=env.cont))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://nous") as client:
        return await client.request(method, path, **kwargs)


async def _informs(env, arrival_id, proposal_id=None):
    """The INFORM rows of an arrival as (title, body); a proposal's short id is masked: it differs per row."""
    rows = [r for r in await inbox_rows(env) if r.msg_type == "INFORM" and r.arrival_id == arrival_id]
    mask = (lambda text: text.replace(proposal_id.hex[:8], "<id>")) if proposal_id is not None else (lambda text: text)
    return sorted((mask(r.title), mask(r.body)) for r in rows)


@pytest.mark.parametrize("decision", ["approve", "reject"])
async def test_a_decision_through_the_card_and_through_the_route_leave_the_same_rows(cards_env, decision):  # noqa: F811
    env = await cards_env()
    sent = register_send_email(env, text="Message sent.")
    via_card, via_route = await ask_with_proposals(env), await ask_with_proposals(env)
    (card_pid,), (route_pid,) = via_card.ids, via_route.ids
    await env.publisher.push_due(now=later())
    card = await card_row(env, proposal_key(card_pid))
    status, _body = await tap(env, card.surface_id, f"intention.{decision}", {"proposalId": str(card_pid)})
    assert status == 200
    response = await _route(
        env, "POST", f"/intentions/proposals/{route_pid}/decide", json={"decision": decision, "actor": "alice"}
    )
    assert response.status_code == 200
    final = "executed" if decision == "approve" else "rejected"
    await asyncio.wait_for(_until_state(env, card_pid, final), timeout=10)
    a, b = await proposal_row(env, card_pid), await proposal_row(env, route_pid)
    assert (a.state, a.error) == (b.state, b.error) == (final, None)
    assert a.result == b.result
    assert await _informs(env, via_card.done.arrival_id, card_pid) == await _informs(
        env, via_route.done.arrival_id, route_pid
    )
    assert len(sent) == (2 if decision == "approve" else 0)
    # the REST route refuses a contradiction in the store's fixed words (the handler says the same sentence: pinned
    # at handler level in test_f099_phase3a_handlers); the card is already closed at the first tap (R1), so a second
    # tap on it is a 404 on a dead surface
    other = "reject" if decision == "approve" else "approve"
    refused = await _route(env, "POST", f"/intentions/proposals/{route_pid}/decide", json={"decision": other})
    assert refused.status_code == 409 and refused.json()["error"] == DECISION_REFUSALS["not_pending"]
    status, _body = await tap(env, card.surface_id, f"intention.{other}", {"proposalId": str(card_pid)})
    assert status == 404


async def test_an_answer_through_the_card_and_through_the_route_leave_the_same_rows(cards_env):  # noqa: F811
    env = await cards_env()
    _r1, card_qid, card_done = await ask(env)
    _r2, route_qid, route_done = await ask(env)
    await env.publisher.push_due(now=later())
    card = await card_row(env, question_key(card_qid))
    model = {**card.data_model, "answer": "Yes, book it."}
    status, _ = await tap(
        env, card.surface_id, "intention.answer", {"questionId": str(card_qid), "text": "Yes, book it."}, model=model
    )
    response = await _route(
        env, "POST", f"/intentions/questions/{route_qid}/answer", json={"text": "Yes, book it.", "actor": "alice"}
    )
    assert (status, response.status_code) == (200, 200)
    assert (
        await _informs(env, card_done.arrival_id)
        == await _informs(env, route_done.arrival_id)
        == [("Owner's answer", "Yes, book it.")]
    )
    again = await _route(env, "POST", f"/intentions/questions/{route_qid}/answer", json={"text": "No."})
    assert again.status_code == 409 and again.json()["error"] == ANSWER_REFUSALS["answered"]
