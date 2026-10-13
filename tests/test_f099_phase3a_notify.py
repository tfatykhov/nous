"""F099 Phase 3a: the notify matrix of push_built, with and without telegram_ping (contract section 4.7)."""

from __future__ import annotations

import ast
import asyncio
import uuid
from pathlib import Path

import pytest
from sqlalchemy import delete
from test_a2ui_push import RecordingPush

from nous.a2ui import service as service_module
from nous.a2ui.builders import approval_gate
from nous.a2ui.service import SurfaceService
from nous.storage.models import A2uiAction, A2uiSurface

# (a2ui_telegram_notify_enabled, FCM configured, Telegram pings with telegram_ping=None: today's rule)
CELLS = [(True, True, 1), (True, False, 1), (False, True, 0), (False, False, 1)]


def _card(title="Restart?"):
    return approval_gate(
        {
            "title": title,
            "summary": "s",
            "risk": "r",
            "options": [{"id": "go", "label": "Go"}, {"id": "w", "label": "W"}],
        }
    )


@pytest.fixture
async def build(db, settings):
    made: list[str] = []

    async def make(*, switch: bool, fcm: bool):
        agent = f"test-3a-notify-{uuid.uuid4().hex[:8]}"
        made.append(agent)
        config = settings.model_copy(
            update={
                "agent_id": agent,
                "telegram_bot_token": None,
                "telegram_chat_id": None,
                "a2ui_telegram_notify_enabled": switch,
            }
        )
        push = RecordingPush(configured=fcm)
        service = SurfaceService(db, config, push=push)
        pings: list[str] = []

        async def record(title, surface_id, text=None):
            pings.append(surface_id)

        service._notify_telegram = record  # the instance attribute wins over the method
        return service, push, pings

    yield make
    async with db.session() as session:
        for agent in made:
            await session.execute(delete(A2uiAction).where(A2uiAction.agent_id == agent))
            await session.execute(delete(A2uiSurface).where(A2uiSurface.agent_id == agent))
        await session.commit()


async def _drain(service):
    await asyncio.sleep(0)
    await asyncio.gather(*list(service._pending_tasks))


@pytest.mark.postgres_only
@pytest.mark.parametrize(("switch", "fcm", "expected"), CELLS)
async def test_push_built_without_telegram_ping_keeps_the_pre_3a_notify_matrix(build, switch, fcm, expected):  # PIN
    """Mutation: change ``_telegram_ping_wanted`` (or make the new kwarg default to False) and a cell changes."""
    service, push, pings = await build(switch=switch, fcm=fcm)
    await service.push_built(_card(), dedup_key="matrix:none", notify=True)
    await _drain(service)
    assert len(pings) == expected
    assert len(push.surfaces) == (1 if fcm else 0)


@pytest.mark.postgres_only
@pytest.mark.parametrize(("switch", "fcm", "_expected"), CELLS)
async def test_telegram_ping_false_never_pings_and_leaves_the_phone_alert_alone(build, switch, fcm, _expected):
    service, push, pings = await build(switch=switch, fcm=fcm)
    await service.push_built(_card(), dedup_key="matrix:false", notify=True, telegram_ping=False)
    await _drain(service)
    assert pings == []
    assert len(push.surfaces) == (1 if fcm else 0)


@pytest.mark.postgres_only
async def test_telegram_ping_true_pings_even_with_the_switch_off(build):
    service, push, pings = await build(switch=False, fcm=True)
    await service.push_built(_card(), dedup_key="matrix:true", notify=True, telegram_ping=True)
    await _drain(service)
    assert len(pings) == 1 and len(push.surfaces) == 1


@pytest.mark.postgres_only
async def test_a_replacement_never_notifies(build):
    service, push, pings = await build(switch=True, fcm=True)
    first = await service.push_built(_card(), dedup_key="matrix:rep", notify=True, telegram_ping=False)
    second = await service.push_built(_card("Again?"), dedup_key="matrix:rep", notify=True, telegram_ping=False)
    await _drain(service)
    assert first == second and push.surfaces == [first] and pings == []


def test_every_hop_forwards_telegram_ping():
    """push_built re-enters itself on the dedup race and on IntegrityError, and passes the ping decision down to
    the post-commit send. A hop that drops the kwarg is a NameError after the commit, or a ping the caller forbade."""
    tree = ast.parse(Path(service_module.__file__).read_text(encoding="utf-8"))

    def calls(*names):
        return [
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr in names
            and isinstance(n.func.value, ast.Name)
            and n.func.value.id == "self"
        ]

    hops = calls("push_built", "_push_transaction", "_push_transaction_inner")
    assert len(hops) >= 5  # two re-entries into push_built, two into _push_transaction, one into the inner
    for call in hops:
        assert "telegram_ping" in {k.arg for k in call.keywords}, ast.dump(call)[:80]
