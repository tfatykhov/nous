"""Shared builders for the F099 Phase 3a card tests. Fixtures are imported by name into a test module."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from f099_support import claim, commit_ask, make_root, record
from sqlalchemy import delete, select, update
from test_a2ui_push import RecordingPush

from nous.a2ui.actions import ActionRouter
from nous.a2ui.service import SurfaceService
from nous.brain import continuation
from nous.handlers.continuation_publisher import OwnerPublisher
from nous.handlers.continuation_runner import ContinuationRunner
from nous.storage.models import A2uiAction, A2uiSurface, ResultInbox

CARDS = {
    "result_inbox_enabled": True,
    "intentions_enabled": True,
    "continuation_enabled": True,
    "intention_cards_enabled": True,
}


async def question_id_of(env, arrival_id):
    """The id the owner surfaces use for the QUESTION row of an arrival (its ``source_id``)."""
    async with env.db.session() as s:
        return (
            await s.execute(
                select(ResultInbox.source_id).where(
                    ResultInbox.agent_id == env.agent,
                    ResultInbox.arrival_id == arrival_id,
                    ResultInbox.msg_type == "QUESTION",
                )
            )
        ).scalar_one()


async def ask(env, root=None, note="May I book the Friday slot?"):
    """A root with a result, claimed and asked about: ``(root, question id, commit)``."""
    root = root or await make_root(env)
    await record(env, root)
    done = await commit_ask(env, await claim(env, root.id), note)
    return root, await question_id_of(env, done.arrival_id), done


async def answer(env, question_id, text="Yes, book it.", actor="telegram:42"):
    """``continuation.record_answer`` in its own committed session (what ``answer_question`` does)."""
    async with env.db.session() as s:
        recorded = await continuation.record_answer(
            s, env.agent, question_id, text=text, actor=actor, settings=env.settings
        )
        await s.commit()
    return recorded


async def age_question(env, arrival_id, hours=25):
    """Move a QUESTION row's window into the past (the window opens at ``created_at``/``push_after``). ``push_after``
    moves too, never to NULL: ``push_due`` selects ``push_after IS NOT NULL``, and a row it cannot select would make the
    publisher tests pass for the wrong reason."""
    past = datetime.now(UTC) - timedelta(hours=hours)
    async with env.db.session() as s:
        await s.execute(
            update(ResultInbox)
            .where(ResultInbox.arrival_id == arrival_id, ResultInbox.msg_type == "QUESTION")
            .values(created_at=past, push_after=past)
        )
        await s.commit()


async def wake(env):
    """The sweep's wake of answered or expired questions, committed. Returns the woken intention ids."""
    async with env.db.session() as s:
        woken = await continuation.wake_terminal_arrivals(s, env.agent, settings=env.settings)
        await s.commit()
    return woken


def later() -> datetime:
    """Past any quiet-hours end and inside the 24 h proposal window (the publisher tests' ``_later``)."""
    return datetime.now(UTC) + timedelta(hours=10)


def fake_http(*, status=200, message_id=555):
    http = MagicMock()
    http.post = AsyncMock(
        return_value=SimpleNamespace(
            status_code=status, json=lambda: {"ok": True, "result": {"message_id": message_id}}
        )
    )
    return http


async def drain(service) -> None:
    """Let the background FCM/Telegram-ping tasks of a push land."""
    await asyncio.sleep(0)
    await asyncio.gather(*list(service._pending_tasks))


@pytest.fixture
async def cards_env(runner_env, db):
    """``await cards_env(fcm=False, http_status=200, **settings)``: a ``runner_env`` with the cards flag on, a real
    ``SurfaceService`` (a recording push leg; every Telegram ping recorded in ``env.pings``), a publisher with a
    fake Telegram HTTP client (``env.http``), a ``ContinuationRunner`` wired as main.py wires it, and an
    ``ActionRouter``. Teardown deletes the agent's surfaces and audit rows."""
    agents: list[str] = []

    async def build(*, fcm: bool = False, http_status: int = 200, **settings):
        values = {**CARDS, "telegram_bot_token": "test-token", "telegram_chat_id": "8080", **settings}
        env = await runner_env(**values)
        env.http = fake_http(status=http_status)
        env.push = RecordingPush(configured=fcm)
        env.pings = []
        env.service = SurfaceService(env.db, env.settings, heart=env.heart, push=env.push)

        async def ping(title, surface_id, text=None):
            env.pings.append(surface_id)

        env.service._notify_telegram = ping  # the instance attribute wins over the method
        env.publisher = OwnerPublisher(database=env.db, settings=env.settings, http_client=env.http)
        env.cont = ContinuationRunner(
            database=env.db,
            settings=env.settings,
            runner=env.runner,
            heart=env.heart,
            brain=env.brain,
            bus=env.bus,
            dispatcher=env.dispatcher,
            publisher=env.publisher,
        )
        env.cont.set_surface_service(env.service)
        env.router = ActionRouter(env.db, env.settings, env.service, heart=env.heart, continuation_runner=env.cont)
        agents.append(env.agent)
        return env

    yield build
    async with db.session() as session:
        for agent in agents:
            await session.execute(delete(A2uiAction).where(A2uiAction.agent_id == agent))
            await session.execute(delete(A2uiSurface).where(A2uiSurface.agent_id == agent))
        await session.commit()


async def card_row(env, dedup_key):
    """The newest surface row with this dedup key, live or not, or None."""
    async with env.db.session() as s:
        return (
            (
                await s.execute(
                    select(A2uiSurface)
                    .where(A2uiSurface.agent_id == env.agent, A2uiSurface.dedup_key == dedup_key)
                    .order_by(A2uiSurface.created_at.desc())
                )
            )
            .scalars()
            .first()
        )


async def live_cards(env):
    async with env.db.session() as s:
        return list(
            (
                await s.execute(
                    select(A2uiSurface).where(
                        A2uiSurface.agent_id == env.agent,
                        A2uiSurface.status == "live",
                        A2uiSurface.dedup_key.like("intention:%"),
                    )
                )
            ).scalars()
        )


async def tap(env, surface_id, name, context, *, model=None, actor="alice"):
    """A tap through the REAL ActionRouter gate (live surface, allowed_actions, shape, nonce, handler, audit):
    ``(http status, body)``."""
    async with env.db.session() as s:
        nonce = (await s.execute(select(A2uiSurface.nonce).where(A2uiSurface.surface_id == surface_id))).scalar_one()
    body = {
        "action": {
            "name": name,
            "surfaceId": surface_id,
            "context": context,
            "metadata": {"extensions": {"com_nous_nonce": nonce}},
        }
    }
    if model is not None:  # the renderer ships its local data model with the action (transport.ts)
        body["a2uiRendererDataModel"] = {"surfaces": {surface_id: model}}
    return await env.router.handle(body, content_type="application/json", actor=actor)
