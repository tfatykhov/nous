"""F099 Phase 2d-7: the REST routes are thin: they resolve an id, call one runner function, and map the result."""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest
from f099_support import (
    CONT,
    ON,
    SEND_EMAIL_SCHEMA,
    ask_with_proposals,
    claimed,
    commit_ask,
    env_factory,  # noqa: F401
    proposal_row,
    register_send_email,
    runner_env,  # noqa: F401
    set_intention,
    stage,
)
from sqlalchemy import select, update
from starlette.applications import Starlette

from nous.api.intention_routes import build_intention_routes
from nous.api.rest import create_app
from nous.brain import continuation
from nous.handlers.continuation_runner import ContinuationRunner
from nous.storage.models import IntentionProposal, ResultInbox

pytestmark = pytest.mark.postgres_only


def _app(env, runner) -> Starlette:
    return Starlette(routes=build_intention_routes(database=env.db, settings=env.settings, continuation_runner=runner))


def _runner(env) -> ContinuationRunner:
    return ContinuationRunner(
        database=env.db,
        settings=env.settings,
        runner=env.runner,
        heart=env.heart,
        brain=env.brain,
        bus=env.bus,
        dispatcher=env.dispatcher,
    )


async def _call(app, method, path, **kwargs) -> httpx.Response:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://nous") as client:
        return await client.request(method, path, **kwargs)


async def _question(env, note="Shall I book it?"):
    _root, got = await claimed(env)
    done = await commit_ask(env, got, note)
    async with env.db.session() as s:
        qid = (
            await s.execute(
                select(ResultInbox.source_id).where(
                    ResultInbox.agent_id == env.agent, ResultInbox.arrival_id == done.arrival_id
                )
            )
        ).scalar_one()
    return qid


async def _set(env, model, row_id, **values):
    async with env.db.session() as s:
        await s.execute(update(model).where(model.id == row_id).values(**values))
        await s.commit()


# ---- decide --------------------------------------------------------------------------------------------------


async def test_approving_over_rest_runs_the_call_and_answers_with_its_state(runner_env):  # noqa: F811
    env = await runner_env()
    sent = register_send_email(env, text="Message sent.")
    (pid,) = (await ask_with_proposals(env)).ids
    app = _app(env, _runner(env))
    response = await _call(
        app,
        "POST",
        f"/intentions/proposals/{pid.hex[:8]}/decide",
        json={"decision": "approve", "actor": "telegram:42"},
    )
    assert response.status_code == 200
    assert response.json() == {
        "proposal_id": str(pid),
        "short_id": pid.hex[:8],
        "state": "executed",
        "result": "Message sent.",
        "error": None,
        "changed": True,
        "woke": True,
    }
    assert len(sent) == 1 and (await proposal_row(env, pid)).decided_by == "telegram:42"


async def test_rejecting_over_rest_runs_nothing(runner_env):  # noqa: F811
    env = await runner_env()
    sent = register_send_email(env)
    (pid,) = (await ask_with_proposals(env)).ids
    response = await _call(
        _app(env, _runner(env)), "POST", f"/intentions/proposals/{pid}/decide", json={"decision": "reject"}
    )
    assert response.status_code == 200 and response.json()["state"] == "rejected" and sent == []


async def test_a_repeated_approve_is_200_and_a_contradictory_one_is_409(runner_env):  # noqa: F811
    env = await runner_env()
    sent = register_send_email(env)
    (pid,) = (await ask_with_proposals(env)).ids
    app = _app(env, _runner(env))
    path = f"/intentions/proposals/{pid.hex[:8]}/decide"
    await _call(app, "POST", path, json={"decision": "approve"})
    again = await _call(app, "POST", path, json={"decision": "approve"})
    assert again.status_code == 200 and again.json()["changed"] is False and again.json()["state"] == "executed"
    flipped = await _call(app, "POST", path, json={"decision": "reject"})
    assert flipped.status_code == 409
    assert flipped.json() == {
        "error": "That proposal was already decided the other way.",
        "state": "executed",
        "refusal": "not_pending",
    }
    assert len(sent) == 1


async def test_a_late_decision_is_409_with_a_fixed_message(runner_env):  # noqa: F811
    env = await runner_env()
    sent = register_send_email(env)
    (pid,) = (await ask_with_proposals(env)).ids
    await _set(env, IntentionProposal, pid, deadline=datetime.now(UTC) - timedelta(minutes=1))
    response = await _call(
        _app(env, _runner(env)), "POST", f"/intentions/proposals/{pid.hex[:8]}/decide", json={"decision": "approve"}
    )
    assert response.status_code == 409 and response.json()["refusal"] == "expired" and sent == []
    assert "expired" in response.json()["error"]


@pytest.mark.parametrize(
    ("path_id", "payload", "status"),
    [
        ("ab12cd3", {"decision": "approve"}, 400),  # too short
        ("zz12cd34", {"decision": "approve"}, 400),
        ("ffffffff", {"decision": "maybe"}, 400),
        ("ffffffff", {"decision": "approve"}, 404),  # well-formed, no such proposal
        ("ffffffff", ["approve"], 400),
        ("ffffffff", {}, 400),
    ],
)
async def test_a_malformed_or_unknown_request_is_refused_before_anything_runs(runner_env, path_id, payload, status):  # noqa: F811
    env = await runner_env()
    sent = register_send_email(env)
    await ask_with_proposals(env)
    response = await _call(_app(env, _runner(env)), "POST", f"/intentions/proposals/{path_id}/decide", json=payload)
    assert response.status_code == status and sent == []


async def test_a_body_that_is_not_json_is_400(runner_env):  # noqa: F811
    env = await runner_env()
    response = await _call(
        _app(env, _runner(env)), "POST", "/intentions/proposals/ffffffff/decide", content=b"not json"
    )
    assert response.status_code == 400


async def test_a_staged_proposal_cannot_be_decided_over_rest(runner_env):  # noqa: F811
    env = await runner_env()
    _root, got = await claimed(env)
    staged = await stage(env, got)  # the owner has never seen it
    response = await _call(
        _app(env, _runner(env)), "POST", f"/intentions/proposals/{staged.hex[:8]}/decide", json={"decision": "approve"}
    )
    assert response.status_code == 404 and (await proposal_row(env, staged)).state == "staged"


async def test_an_ambiguous_prefix_is_400(runner_env):  # noqa: F811
    env = await runner_env()
    first = (await ask_with_proposals(env)).ids[0]
    second = (await ask_with_proposals(env)).ids[0]
    for row_id in (first, second):  # two ids that share their first 8 characters (fresh tails: rows outlive the test)
        await _set(env, IntentionProposal, row_id, id=uuid.UUID("abcdef01" + uuid.uuid4().hex[8:]))
    response = await _call(
        _app(env, _runner(env)), "POST", "/intentions/proposals/abcdef01/decide", json={"decision": "reject"}
    )
    assert response.status_code == 400 and "more than one" in response.json()["error"]


async def test_the_actor_is_clean_text_with_a_default(runner_env):  # noqa: F811
    env = await runner_env()
    first = (await ask_with_proposals(env)).ids[0]
    second = (await ask_with_proposals(env)).ids[0]
    app = _app(env, _runner(env))
    dirty = "o\x00w\nn" + "x" * 300
    await _call(app, "POST", f"/intentions/proposals/{first}/decide", json={"decision": "reject", "actor": dirty})
    await _call(app, "POST", f"/intentions/proposals/{second}/decide", json={"decision": "reject", "actor": "   "})
    assert (await proposal_row(env, first)).decided_by == ("own" + "x" * 300)[:100]  # printable only, clipped
    assert (await proposal_row(env, second)).decided_by == "rest"


# ---- the list ------------------------------------------------------------------------------------------------


async def test_the_list_filters_by_state_validates_its_limit_and_never_shows_a_staged_proposal(runner_env):  # noqa: F811
    env = await runner_env()
    pending = (await ask_with_proposals(env)).ids[0]
    rejected = (await ask_with_proposals(env)).ids[0]
    _root, got = await claimed(env)
    await stage(env, got)
    app = _app(env, _runner(env))
    await _call(app, "POST", f"/intentions/proposals/{rejected}/decide", json={"decision": "reject"})
    default = (await _call(app, "GET", "/intentions/proposals")).json()["proposals"]
    assert [p["id"] for p in default] == [str(pending)] and default[0]["arguments"]["subject"] == "Snow 0"
    everything = (await _call(app, "GET", "/intentions/proposals", params={"state": "all"})).json()["proposals"]
    assert {p["id"] for p in everything} == {str(pending), str(rejected)}
    one = (await _call(app, "GET", "/intentions/proposals", params={"state": "all", "limit": 1})).json()["proposals"]
    assert len(one) == 1
    bad_limits = ({"limit": "0"}, {"limit": "x"}, {"limit": "101"}, {"limit": chr(0xB2)})  # a superscript two
    for params in ({"state": "staged"}, {"state": "bogus"}, *bad_limits):
        assert (await _call(app, "GET", "/intentions/proposals", params=params)).status_code == 400


# ---- answers -------------------------------------------------------------------------------------------------


async def test_an_answer_over_rest_is_recorded_and_wakes_the_arrival(runner_env):  # noqa: F811
    env = await runner_env()
    qid = await _question(env)
    app = _app(env, _runner(env))
    path = f"/intentions/questions/{qid.hex[:8]}/answer"
    response = await _call(app, "POST", path, json={"text": "Yes, book it.", "actor": "telegram:42"})
    assert response.status_code == 200
    body = response.json()
    assert body["question_id"] == str(qid) and body["woke"] is True and uuid.UUID(body["arrival_id"])
    again = await _call(app, "POST", path, json={"text": "No."})
    assert again.status_code == 409 and again.json()["reason"] == "answered"
    assert again.json()["error"] == "That question was already answered."


@pytest.mark.parametrize("payload", [{"text": "   "}, {"text": 5}, {}, {"text": "x" * 8001}, ["Yes"]])
async def test_a_blank_or_oversize_answer_is_400(runner_env, payload):  # noqa: F811
    env = await runner_env()
    qid = await _question(env)
    path = f"/intentions/questions/{qid.hex[:8]}/answer"
    assert (await _call(_app(env, _runner(env)), "POST", path, json=payload)).status_code == 400


@pytest.mark.parametrize("text", ["a" + chr(0) + "b", chr(0xD800) + " yes"])
async def test_a_nul_or_a_lone_surrogate_in_an_answer_is_400_and_records_nothing(runner_env, text):  # noqa: F811
    """2d-7 review m1: Postgres refuses a NUL and the UTF-8 encode a lone surrogate, so either would reach the store
    and 500. The raw JSON escapes it (``ensure_ascii``): httpx's own encoder would refuse the surrogate client-side."""
    env = await runner_env()
    qid = await _question(env)
    app = _app(env, _runner(env))
    path = f"/intentions/questions/{qid.hex[:8]}/answer"
    headers = {"content-type": "application/json"}
    response = await _call(app, "POST", path, content=json.dumps({"text": text}), headers=headers)
    assert response.status_code == 400
    assert (await _call(app, "POST", path, json={"text": "Yes"})).status_code == 200  # still unanswered


async def test_an_answer_to_an_unknown_question_or_a_proposal_is_404(runner_env):  # noqa: F811
    env = await runner_env()
    (pid,) = (await ask_with_proposals(env)).ids
    app = _app(env, _runner(env))
    for ident in ("ffffffff", pid.hex[:8]):  # a proposal's id is not a question's
        response = await _call(app, "POST", f"/intentions/questions/{ident}/answer", json={"text": "Yes"})
        assert response.status_code == 404


async def test_an_answer_after_the_question_expired_is_409_expired(runner_env):  # noqa: F811
    env = await runner_env()
    qid = await _question(env)
    aged = datetime.now(UTC) - timedelta(hours=25)
    await _set(env, ResultInbox, await _row_id(env, qid), created_at=aged, push_after=None)
    path = f"/intentions/questions/{qid.hex[:8]}/answer"
    response = await _call(_app(env, _runner(env)), "POST", path, json={"text": "Yes"})
    assert response.status_code == 409 and response.json()["reason"] == "expired"


async def _row_id(env, source_id):
    async with env.db.session() as s:
        return (await s.execute(select(ResultInbox.id).where(ResultInbox.source_id == source_id))).scalar_one()


async def test_a_telegram_reply_is_resolved_by_the_message_it_replies_to(runner_env):  # noqa: F811
    env = await runner_env()
    qid = await _question(env)  # asked on the root's channel, telegram:8080
    await _set(env, ResultInbox, await _row_id(env, qid), push_message_id=777, pushed_at=datetime.now(UTC))
    app = _app(env, _runner(env))
    path = "/intentions/questions/answer"
    wrong_chat = await _call(app, "POST", path, json={"chat_id": 9999, "message_id": 777, "text": "Yes"})
    other_message = await _call(app, "POST", path, json={"chat_id": 8080, "message_id": 778, "text": "Yes"})
    assert (wrong_chat.status_code, other_message.status_code) == (404, 404)  # a reply to something else
    malformed = [
        {"chat_id": "8080", "message_id": 777, "text": "Yes"},
        {"chat_id": True, "message_id": 777, "text": "Yes"},
        {"chat_id": 8080, "message_id": 777, "text": " "},
        {"chat_id": 8080, "message_id": 2**63, "text": "Yes"},  # past BIGINT: asyncpg would refuse the bind
        {"chat_id": -(2**63) - 1, "message_id": 777, "text": "Yes"},
    ]
    for bad in malformed:
        assert (await _call(app, "POST", path, json=bad)).status_code == 400
    good = {"chat_id": 8080, "message_id": 777, "text": "Yes", "actor": "telegram:42"}
    ok = await _call(app, "POST", path, json=good)
    assert ok.status_code == 200 and ok.json()["question_id"] == str(qid) and ok.json()["woke"] is True


# ---- no runner -----------------------------------------------------------------------------------------------


async def test_without_a_runner_a_row_answers_503_and_an_unknown_id_still_404(env_factory):  # noqa: F811
    """Conflict C2: the lookup comes first. With no runner a real row is 503 (nothing can act on it); an id that
    names nothing is 404, which is all that prod's empty tables ever answer."""
    env = await env_factory(**CONT)
    (pid,) = (await ask_with_proposals(env)).ids
    app = _app(env, None)
    real = await _call(app, "POST", f"/intentions/proposals/{pid.hex[:8]}/decide", json={"decision": "approve"})
    assert (real.status_code, real.json()) == (503, {"error": "continuation is not running"})
    nothing = await _call(app, "POST", "/intentions/proposals/ffffffff/decide", json={"decision": "approve"})
    assert nothing.status_code == 404
    listed = (await _call(app, "GET", "/intentions/proposals")).json()["proposals"]
    assert [p["id"] for p in listed] == [str(pid)]  # reads need no runner
    assert (await proposal_row(env, pid)).state == "pending"  # nothing was touched


class _RaisingRunner:
    """A runner whose owner actions fail the way a store error would."""

    async def decide_proposal(self, *args, **kwargs):
        raise RuntimeError("secret internal detail")

    async def answer_question(self, *args, **kwargs):
        raise RuntimeError("secret internal detail")


async def test_a_runner_that_raises_is_a_fixed_500_that_leaks_nothing(runner_env):  # noqa: F811  # PIN
    """2d-7 review m4: the bot branches on these codes. The exception's text stays in the log."""
    env = await runner_env()
    (pid,) = (await ask_with_proposals(env)).ids
    qid = await _question(env)
    app = _app(env, _RaisingRunner())
    decided = await _call(app, "POST", f"/intentions/proposals/{pid.hex[:8]}/decide", json={"decision": "approve"})
    assert (decided.status_code, decided.json()) == (500, {"error": "the decision could not be processed"})
    answer_path = f"/intentions/questions/{qid.hex[:8]}/answer"
    answered = await _call(app, "POST", answer_path, json={"text": "Yes"})
    assert (answered.status_code, answered.json()) == (500, {"error": "the answer could not be processed"})
    assert "secret" not in decided.text and "secret" not in answered.text
    assert (await proposal_row(env, pid)).state == "pending"
    real = _app(env, _runner(env))
    assert (await _call(real, "POST", answer_path, json={"text": "Yes"})).status_code == 200  # was not answered


# ---- the shape of the module ---------------------------------------------------------------------------------


def test_the_routes_touch_only_the_runners_owner_actions():
    """Surface neutrality: the cards of Phase 3 call the same two functions; the module reaches the runner through
    nothing else, and no model-facing object."""
    source = (Path(__file__).resolve().parents[1] / "nous" / "api" / "intention_routes.py").read_text(encoding="utf-8")
    assert set(re.findall(r"continuation_runner\.(\w+)", source)) == {"decide_proposal", "answer_question"}
    assert "dispatcher" not in source and "AgentRunner" not in source


# ---- lead addendum 1: a refusal of any kind is a 409, whichever caller comes first -----------------------------

# The bot's sentences: since the final review's m6 the routes and the bot share one map (nous/owner_actions.py).
ENDED_TEXT = "That work has already ended, so nothing ran."
EXPIRED_TEXT = "That proposal expired before it was decided, so it did not run."
# The root marker, the state the proposal ends in, and what a later caller is refused with (decide_proposal's map).
ENDED_ROOTS = [("root_expired_at", "expired", "expired"), ("root_cancelled_at", "cancelled", "ended")]


def _refused(state: str, refusal: str) -> dict:
    return {"error": ENDED_TEXT if refusal == "ended" else EXPIRED_TEXT, "state": state, "refusal": refusal}


@pytest.mark.parametrize(("marker", "state", "later"), ENDED_ROOTS)
@pytest.mark.parametrize(("first", "second"), [("approve", "reject"), ("reject", "approve")])
async def test_a_decision_on_ended_work_is_409_for_the_first_caller_and_for_a_later_one(
    runner_env,  # noqa: F811
    marker,
    state,
    later,
    first,
    second,
):
    """The first decision on a pending proposal whose work ended ends it (``REFUSE_ENDED``); a later one finds it
    ended (``REFUSE_EXPIRED`` for an expired root). Both are 409 with the current state: a refusal is never a 200."""
    env = await runner_env()
    sent = register_send_email(env)
    asked = await ask_with_proposals(env)
    (pid,) = asked.ids
    await set_intention(env, asked.root.id, **{marker: datetime.now(UTC)})
    app = _app(env, _runner(env))
    path = f"/intentions/proposals/{pid.hex[:8]}/decide"
    one = await _call(app, "POST", path, json={"decision": first})
    two = await _call(app, "POST", path, json={"decision": second})
    assert (one.status_code, one.json()) == (409, _refused(state, "ended"))
    assert (two.status_code, two.json()) == (409, _refused(state, later))
    assert sent == [] and (await proposal_row(env, pid)).state == state


@pytest.mark.parametrize(("marker", "state", "later"), ENDED_ROOTS)
@pytest.mark.parametrize("second", ["approve", "reject"])
async def test_an_approved_call_whose_work_ended_before_it_ran_is_409_not_200(runner_env, marker, state, later, second):  # noqa: F811
    """The end_unrunnable path (2d-6 addendum m3): approved, the process stopped before the claim, then the root
    ended. The store calls a second approve a repeat, the runner's claim fails, and the row ends there: the route
    must answer 409 ``ended``, not a 200 with ``changed``. A later caller is refused by the ended row."""
    env = await runner_env()
    sent = register_send_email(env)
    asked = await ask_with_proposals(env)
    (pid,) = asked.ids
    async with env.db.session() as s:
        await continuation.decide_proposal(s, env.agent, pid, approve=True, actor="t", settings=env.settings)
        await s.commit()
    await set_intention(env, asked.root.id, **{marker: datetime.now(UTC)})
    app = _app(env, _runner(env))
    path = f"/intentions/proposals/{pid.hex[:8]}/decide"
    one = await _call(app, "POST", path, json={"decision": "approve"})
    two = await _call(app, "POST", path, json={"decision": second})
    assert (one.status_code, one.json()) == (409, _refused(state, "ended"))
    assert (two.status_code, two.json()) == (409, _refused(state, later))
    row = await proposal_row(env, pid)
    assert (row.state, row.executed_at) == (state, None) and sent == []


# ---- S1: the route keeps the runner's shield --------------------------------------------------------------------


async def test_a_client_that_goes_away_does_not_cancel_the_approved_call_over_rest(runner_env):  # noqa: F811
    """The decide route awaits the runner's shielded execution and catches no cancellation: a client that drops
    the request mid-call leaves the call running to its end, never stuck in ``executing``."""
    env = await runner_env()
    started, release, calls = asyncio.Event(), asyncio.Event(), []

    async def send_email(**kwargs):
        calls.append(kwargs)
        started.set()
        await release.wait()
        return {"content": [{"type": "text", "text": "sent"}]}

    env.dispatcher.register("send_email", send_email, SEND_EMAIL_SCHEMA)
    (pid,) = (await ask_with_proposals(env)).ids
    runner = _runner(env)
    path = f"/intentions/proposals/{pid.hex[:8]}/decide"
    request = asyncio.create_task(_call(_app(env, runner), "POST", path, json={"decision": "approve"}))
    await asyncio.wait_for(started.wait(), timeout=10)
    request.cancel()
    await asyncio.wait_for(asyncio.gather(request, return_exceptions=True), timeout=10)
    executing = list(runner._executing)
    assert len(executing) == 1 and (await proposal_row(env, pid)).state == "executing"  # still running
    release.set()
    await asyncio.wait_for(asyncio.gather(*executing), timeout=10)
    assert (await proposal_row(env, pid)).state == "executed" and len(calls) == 1


# ---- prod parity -------------------------------------------------------------------------------------------------


async def test_under_prods_flags_the_mounted_routes_find_nothing(env_factory):  # noqa: F811
    """R11 and C2: prod runs intentions, the inbox and result memory with continuation off, so create_app gets no
    runner and no proposal or question row exists. The routes are mounted and answer 404 for every id and an
    empty list; nothing is written."""
    env = await env_factory(**ON, result_memory_enabled=True)
    assert env.settings.continuation_enabled is False
    app = create_app(MagicMock(), MagicMock(), env.heart, MagicMock(), env.db, env.settings)
    listed = await _call(app, "GET", "/intentions/proposals")
    assert (listed.status_code, listed.json()) == (200, {"proposals": []})
    requests = [
        ("/intentions/proposals/ffffffff/decide", {"decision": "approve"}),
        (f"/intentions/proposals/{uuid.uuid4()}/decide", {"decision": "reject"}),
        ("/intentions/questions/ffffffff/answer", {"text": "Yes"}),
        ("/intentions/questions/answer", {"chat_id": 8080, "message_id": 777, "text": "Yes"}),
    ]
    for path, payload in requests:
        assert (await _call(app, "POST", path, json=payload)).status_code == 404, path


async def test_under_prods_flags_malformed_input_is_400_never_500(env_factory):  # noqa: F811
    """2d-7 review I1: the routes are mounted in prod, so malformed input there must be refused, not crash. A
    Unicode digit passes ``str.isdigit`` and fails ``int()``; an id past int64 fails asyncpg's BIGINT bind."""
    env = await env_factory(**ON, result_memory_enabled=True)
    assert env.settings.continuation_enabled is False
    app = create_app(MagicMock(), MagicMock(), env.heart, MagicMock(), env.db, env.settings)
    for limit in (chr(0xB2), chr(0x663)):  # a superscript two; an Arabic-Indic three
        listed = await _call(app, "GET", "/intentions/proposals", params={"limit": limit})
        assert listed.status_code == 400, limit
    for ids in ({"chat_id": 8080, "message_id": 2**63}, {"chat_id": 10**30, "message_id": 10**30}):
        answered = await _call(app, "POST", "/intentions/questions/answer", json={**ids, "text": "Yes"})
        assert answered.status_code == 400, ids
