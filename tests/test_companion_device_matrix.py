"""Paired companion devices through ``create_app``: the revoke hooks as wired, and what a device token proves on each
route and verb. Real Postgres: a real ``DeviceStore``, ``ActionRouter``, ``SurfaceService`` and ``PushService``."""

from __future__ import annotations

import hashlib
import uuid
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import delete, select

from nous.a2ui.push import PushService
from nous.a2ui.service import SurfaceService
from nous.api.rest import create_app
from nous.storage.models import A2uiAction, A2uiDevice, A2uiPushInstallation, A2uiSurface

pytestmark = pytest.mark.postgres_only

OWNER_VALUE = "owner-only-test-value"
PROXY_VALUE = "proxy-only-test-value"
JSON = "application/json"
OWNER = {"X-Nous-Owner-Secret": OWNER_VALUE, "content-type": JSON}
PROXY = {"X-Nous-Proxy-Secret": PROXY_VALUE, "content-type": JSON}


@pytest.fixture
def agent_id() -> str:
    return f"test-devmatrix-{uuid.uuid4().hex[:12]}"


@pytest.fixture
def owned(settings, agent_id: str):
    return settings.model_copy(
        update={
            "agent_id": agent_id,
            "a2ui_enabled": True,
            "telegram_bot_token": None,
            "telegram_chat_id": None,
            "owner_route_secret_sha256": hashlib.sha256(OWNER_VALUE.encode()).hexdigest(),
            "mcp_client_ui_secret_sha256": hashlib.sha256(PROXY_VALUE.encode()).hexdigest(),
        }
    )


@pytest_asyncio.fixture
async def service(db, owned, agent_id: str):
    svc = SurfaceService(db, owned)
    yield svc
    async with db.session() as session:
        for model in (A2uiAction, A2uiSurface, A2uiDevice, A2uiPushInstallation):
            await session.execute(delete(model).where(model.agent_id == agent_id))
        await session.commit()


@pytest.fixture
def push(db, owned) -> PushService:
    return PushService(db, owned)


def _app(db, owned, service: SurfaceService, push: PushService, router: Any = None) -> Any:
    stand_in = SimpleNamespace()
    return create_app(
        stand_in,
        stand_in,
        SimpleNamespace(schedules=None),
        stand_in,
        db,
        owned,
        surface_service=service,
        action_router=router,
        push_service=push,
    )


async def _call(app: Any, method: str, path: str, **kwargs: Any) -> httpx.Response:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://nous") as client:
        return await client.request(method, path, **kwargs)


async def _pair(app: Any, installation_id: str = "inst-1", name: str | None = None) -> dict[str, Any]:
    opened = await _call(app, "POST", "/a2ui/devices/pairings", json={"name": name} if name else {}, headers=OWNER)
    assert opened.status_code == 200, opened.text
    body = {"code": opened.json()["code"], "installation_id": installation_id}
    redeemed = await _call(app, "POST", "/a2ui/devices/redeem", json=body)
    assert redeemed.status_code == 200, redeemed.text
    return redeemed.json()


async def _installations(db, agent_id: str) -> set[str]:
    async with db.session() as session:
        rows = await session.execute(
            select(A2uiPushInstallation.installation_id).where(A2uiPushInstallation.agent_id == agent_id)
        )
        return {row[0] for row in rows}


# ---- the revoke hooks, as create_app wires them ------------------------------------------------------------------


async def test_revoking_or_unpairing_a_device_drops_its_push_installation(
    db, owned, service: SurfaceService, push: PushService, agent_id: str
) -> None:
    app = _app(db, owned, service, push)
    first = await _pair(app, "inst-1")
    second = await _pair(app, "inst-2")
    for installation in ("inst-1", "inst-2", "inst-other"):
        status, _ = await push.register(installation, fcm_token=f"fcm-{installation}")
        assert status == 200
    revoked = await _call(app, "DELETE", f"/a2ui/devices/{first['device_id']}", headers=OWNER)
    assert revoked.json() == {"revoked": True}
    assert await _installations(db, agent_id) == {"inst-2", "inst-other"}
    unpaired = await _call(app, "POST", "/a2ui/devices/unpair", headers={"X-Nous-Device-Token": second["token"]})
    assert unpaired.json() == {"revoked": True}
    assert await _installations(db, agent_id) == {"inst-other"}


async def test_pairing_again_keeps_the_push_installation_the_new_device_holds(
    db, owned, service: SurfaceService, push: PushService, agent_id: str
) -> None:
    app = _app(db, owned, service, push)
    old = await _pair(app, "inst-1")
    await push.register("inst-1", fcm_token="fcm-1")
    new = await _pair(app, "inst-1")
    listed = (await _call(app, "GET", "/a2ui/devices", headers=OWNER)).json()["devices"]
    assert {(d["id"], d["revoked_at"] is None) for d in listed} == {(old["device_id"], False), (new["device_id"], True)}
    assert await _installations(db, agent_id) == {"inst-1"}  # superseded, and still the new device's
