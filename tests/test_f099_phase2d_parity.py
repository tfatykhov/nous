"""F099 Phase 2d: on prod's exact flags (inbox, intentions and result memory ON, continuation OFF) nothing of 2d
exists or runs. The routes find nothing, the bot's handlers are inert, no migration or setting was added."""

from __future__ import annotations

import inspect
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from f099_support import env_factory, runner_env  # noqa: F401
from sqlalchemy import func, select
from starlette.applications import Starlette
from test_f099_phase2c_parity import PROD, NoDatabase, Untouchable

import nous.main as main
from nous.api.intention_routes import build_intention_routes
from nous.api.rest import create_app
from nous.brain import continuation
from nous.config import Settings
from nous.handlers.continuation_runner import ContinuationRunner
from nous.storage.models import IntentionProposal, ResultInbox
from nous.telegram_bot import NousTelegramBot

HEX = "a" * 32
GONE = "That proposal is no longer available."


def _prod_routes(env, runner=None) -> Starlette:
    return Starlette(routes=build_intention_routes(database=env.db, settings=env.settings, continuation_runner=runner))


async def _call(app, method, path, **kwargs) -> httpx.Response:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://nous") as client:
        return await client.request(method, path, **kwargs)


async def _counts(env) -> tuple[int, int]:
    async with env.db.session() as s:
        proposals = (
            await s.execute(
                select(func.count()).select_from(IntentionProposal).where(IntentionProposal.agent_id == env.agent)
            )
        ).scalar_one()
        inbox = (
            await s.execute(select(func.count()).select_from(ResultInbox).where(ResultInbox.agent_id == env.agent))
        ).scalar_one()
    return proposals, inbox


# ---- the wiring ----------------------------------------------------------------------------------------------


def test_build_app_hands_create_app_the_lazy_runner_proxy():
    assert 'continuation_runner=_lazy_component(components, "continuation_runner")' in inspect.getsource(main.build_app)
    assert "continuation_runner" in inspect.signature(create_app).parameters
    assert not main._lazy_component({"continuation_runner": None}, "continuation_runner")  # prod: falsy, so 503/404


def test_create_app_registers_the_four_owner_routes_and_the_old_ones_stay():
    settings = Settings(_env_file=None, **PROD)
    app = create_app(MagicMock(), MagicMock(), MagicMock(), MagicMock(), MagicMock(), settings)
    paths = {getattr(route, "path", None) for route in app.routes}
    assert {
        "/intentions/proposals",
        "/intentions/proposals/{id}/decide",
        "/intentions/questions/answer",
        "/intentions/questions/{id}/answer",
    } <= paths
    assert {"/chat", "/status", "/decisions", "/subtasks/{id}", "/schedules"} <= paths  # PIN: nothing was displaced


def test_create_components_gives_the_continuation_runner_the_agent_runners_dispatcher():  # PIN
    """2d-5 review m5: what ``validate_call`` approves and what ``execute_single_call`` dispatches come from ONE
    registry. ``create_components`` builds one ``ToolDispatcher`` and hands that same name to both."""
    source = inspect.getsource(main.create_components)
    assert source.count("ToolDispatcher(") == 1
    assert "runner.set_dispatcher(dispatcher)" in source
    call = source[source.index("await _build_continuation_runner(") :]
    call = call[: call.index(")\n")]
    assert "dispatcher=dispatcher" in call


@pytest.mark.postgres_only
async def test_the_built_continuation_runner_holds_the_same_dispatcher_instance(runner_env, monkeypatch):  # noqa: F811
    """The identity, not equality: the runner ``_build_continuation_runner`` returns holds the very dispatcher the
    AgentRunner dispatches through (2d-5 review m5)."""
    monkeypatch.setattr(continuation, "CONTINUATION_RUNNER_READY", True)
    env = await runner_env()
    built = await main._build_continuation_runner(
        env.settings,
        database=env.db,
        runner=env.runner,
        heart=env.heart,
        brain=env.brain,
        bus=None,
        dispatcher=env.dispatcher,
    )
    try:
        assert built._dispatcher is env.dispatcher
        assert env.runner._dispatcher is env.dispatcher
    finally:
        await built.stop()


# ---- the routes under prod's flags ---------------------------------------------------------------------------


@pytest.mark.postgres_only
async def test_the_routes_answer_404_or_empty_for_everything_under_prods_flags(env_factory):  # noqa: F811
    """R11: no rows exist and no runner is built, so every id is a 404 and the list is empty, and nothing is written."""
    env = await env_factory(**PROD, telegram_bot_token="test-token", telegram_chat_id="8080")
    before = await _counts(env)
    proxy = main._lazy_component({"continuation_runner": None}, "continuation_runner")  # what main passes
    for runner in (None, proxy):
        app = _prod_routes(env, runner)
        assert (await _call(app, "GET", "/intentions/proposals")).json() == {"proposals": []}
        for state in ("open", "all", "executed"):
            listed = await _call(app, "GET", "/intentions/proposals", params={"state": state})
            assert listed.json() == {"proposals": []}
        decide = await _call(app, "POST", f"/intentions/proposals/{HEX}/decide", json={"decision": "approve"})
        reject = await _call(app, "POST", f"/intentions/proposals/{HEX[:8]}/decide", json={"decision": "reject"})
        answer = await _call(app, "POST", f"/intentions/questions/{HEX}/answer", json={"text": "yes"})
        reply = await _call(
            app, "POST", "/intentions/questions/answer", json={"chat_id": 8080, "message_id": 1, "text": "yes"}
        )
        assert [r.status_code for r in (decide, reject, answer, reply)] == [404, 404, 404, 404]
    assert await _counts(env) == before


@pytest.mark.postgres_only
async def test_the_real_bot_against_the_real_routes_is_inert_under_prods_flags(env_factory):  # noqa: F811
    """A stale or forged tap, a typed /approve and /answer, a reply and a malformed id, from the owner chat, end to
    end (C18, strict parity): the tap, which cannot occur in prod, is told the proposal is gone; every message falls
    through to chat unchanged; no row is written. The bot is built WITH an owner chat (the stronger case: the bot
    service is passed ``NOUS_TELEGRAM_CHAT_ID`` from 2d-9 on); without one it makes no REST call at all."""
    env = await env_factory(**PROD, telegram_bot_token="test-token", telegram_chat_id="8080")
    before = await _counts(env)
    bot = NousTelegramBot("test-token", "http://nous.test", allowed_users={42}, owner_chat_id=42)
    await bot._http.aclose()
    bot._http = httpx.AsyncClient(transport=httpx.ASGITransport(app=_prod_routes(env)))  # the real routes
    sent = []

    async def fake_tg(method, params=None):
        if method == "sendMessage":
            sent.append(params["text"])
        return {}

    bot._tg = fake_tg
    bot._chat_streaming = AsyncMock()
    user = {"id": 42, "first_name": "Owner"}
    button = {"id": "c", "from": user, "data": f"f099:p:{HEX}:a", "message": {"message_id": 1, "chat": {"id": 42}}}
    tap = {"callback_query": button}
    typed = {"message": {"message_id": 2, "from": user, "chat": {"id": 42}, "text": f"/approve {HEX[:8]}"}}
    reply = {
        "message": {
            "message_id": 3,
            "from": user,
            "chat": {"id": 42},
            "text": "Thanks!",
            "reply_to_message": {"message_id": 9, "from": {"id": 1, "is_bot": True}},
        }
    }
    answered = {"message": {"message_id": 4, "from": user, "chat": {"id": 42}, "text": f"/answer {HEX[:8]} yes"}}
    malformed = {"message": {"message_id": 5, "from": user, "chat": {"id": 42}, "text": "/approve xyz"}}
    await bot._handle_update(tap)
    assert sent == [GONE]  # a tap has no chat to fall through to, and none can occur in prod
    bot._chat_streaming.assert_not_called()
    messages = (typed, answered, reply, malformed)
    for count, update in enumerate(messages, start=1):
        await bot._handle_update(update)
        assert bot._chat_streaming.await_count == count  # the ordinary chat path, as before 2d
    assert sent == [GONE]  # the owner was told nothing else
    texts = [call.args[1] for call in bot._chat_streaming.await_args_list]
    assert texts == [f"/approve {HEX[:8]}", f"/answer {HEX[:8]} yes", "Thanks!", "/approve xyz"]
    assert await _counts(env) == before
    await bot._http.aclose()


# ---- nothing else was added ----------------------------------------------------------------------------------


def test_2d_added_no_migration_and_no_setting():  # PIN: changes when a later PR adds one on purpose
    migrations = Path(__file__).resolve().parents[1] / "sql" / "migrations"
    assert sorted(migrations.glob("*.sql"))[-1].name.startswith("084_")
    named = {name for name in Settings.model_fields if "proposal" in name or "owner_action" in name}
    assert named == {"intention_proposal_ttl_hours"}


async def test_the_owner_actions_of_the_runner_are_inert_with_the_flag_off_and_touch_no_collaborator():
    """The runner is never built on prod's flags; if one were, its sweep still does nothing and touches nothing."""
    settings = Settings(_env_file=None, telegram_bot_token="test-token", **PROD)
    db = NoDatabase()
    runner = ContinuationRunner(
        database=db, settings=settings, runner=Untouchable(), heart=Untouchable(), brain=Untouchable()
    )
    report = await runner.run_once()
    assert db.sessions == 0 and report.expired_proposals == 0 and report.launched == ()
    assert await runner._push() == 0 and db.sessions == 0  # no publisher: nothing to push
