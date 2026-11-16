"""Paired companion devices through ``create_app``: the revoke hooks as wired, and what a device token proves on each
route and verb. Real Postgres: a real ``DeviceStore``, ``ActionRouter``, ``SurfaceService`` and ``PushService``."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import uuid
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import delete, select, text, update
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Mount

from nous.a2ui.actions import ActionRouter
from nous.a2ui.builders import approval_gate
from nous.a2ui.push import PushService
from nous.a2ui.service import SurfaceService
from nous.api.companion_devices import DeviceStore
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


class _GatedPush(PushService):
    """A push service whose ``deregister`` waits at a gate the test opens: a revoke held between its check that no
    live device holds the installation and its deletion of the push row."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.reached = asyncio.Event()
        self.gate = asyncio.Event()

    async def deregister(self, installation_id: str) -> tuple[int, dict[str, Any]]:
        self.reached.set()
        await self.gate.wait()
        return await super().deregister(installation_id)


async def _lock_waiters(db) -> int:
    async with db.engine.connect() as conn:
        return (
            await conn.execute(
                text(
                    "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                    "AND wait_event_type = 'Lock' AND wait_event = 'advisory' "
                    "AND query LIKE '%pg_advisory_xact_lock%'"
                )
            )
        ).scalar_one()


@pytest.mark.parametrize("how", ["owner-revoke", "unpair"])
async def test_a_revoke_cannot_drop_the_push_row_of_a_device_paired_meanwhile(
    db, owned, service: SurfaceService, agent_id: str, how: str
) -> None:
    """The old device is revoked and its push cleanup is held after the check that no live device holds the
    installation; meanwhile the same phone pairs again and registers its push token at once. Afterwards the push row
    exists, with the new token, and the live device holds the installation: the cleanup did not delete it."""
    push = _GatedPush(db, owned)
    app = _app(db, owned, service, push)
    old = await _pair(app, "inst-phone")
    await push.register("inst-phone", fcm_token="fcm-old")
    if how == "owner-revoke":
        revoking = _call(app, "DELETE", f"/a2ui/devices/{old['device_id']}", headers=OWNER)
    else:
        revoking = _call(app, "POST", "/a2ui/devices/unpair", headers={"X-Nous-Device-Token": old["token"]})
    revoke = asyncio.create_task(revoking)
    await asyncio.wait_for(push.reached.wait(), 10)
    code = (await _call(app, "POST", "/a2ui/devices/pairings", json={}, headers=OWNER)).json()["code"]

    async def pair_and_register() -> httpx.Response:
        body = {"code": code, "installation_id": "inst-phone"}
        response = await _call(app, "POST", "/a2ui/devices/redeem", json=body)
        await push.register("inst-phone", fcm_token="fcm-new")  # the phone registers as soon as it is paired
        return response

    repair = asyncio.create_task(pair_and_register())
    for _ in range(200):  # the pairing either finishes or waits for the device lock the cleanup holds
        if repair.done() or await _lock_waiters(db) >= 1:
            break
        await asyncio.sleep(0.05)
    push.gate.set()
    revoked, repaired = await asyncio.gather(revoke, repair)
    assert (revoked.status_code, repaired.status_code) == (200, 200)
    async with db.session() as session:
        rows = (
            await session.execute(
                select(A2uiPushInstallation.installation_id, A2uiPushInstallation.fcm_token).where(
                    A2uiPushInstallation.agent_id == agent_id
                )
            )
        ).all()
    assert [tuple(row) for row in rows] == [("inst-phone", "fcm-new")]
    assert await DeviceStore(db, agent_id).installation_bound("inst-phone")


# ---- the matrix: every proof against every verb that needs one, every owner route, chat and /mcp ----------------

ANOTHER_AGENT = "test-devmatrix-another-agent"
PROOF_NAMES = [
    "none",
    "owner",
    "proxy-json",
    "proxy-text",
    "device",
    "revoked-device",
    "another-agents-device",
    "wrong-prefix",
    "wrong-length",
    "pairing-code",
    "wrong-owner-and-device",
    "wrong-proxy-and-device",
    "oversize",
]
# What a verb registered with owner_proof=True answers. "ACTION_FAILED": the proof held and the verb reached its
# handler, which refuses it on its own terms here (an option nobody offered, no continuation runner, no agent).
VERB_OUTCOME = {
    "none": (403, "OWNER_PROOF_REQUIRED"),
    "owner": (422, "ACTION_FAILED"),
    "proxy-json": (422, "ACTION_FAILED"),
    "proxy-text": (415, "UNSUPPORTED_MEDIA_TYPE"),
    "device": (422, "ACTION_FAILED"),
    "revoked-device": (403, "DEVICE_TOKEN_REJECTED"),
    "another-agents-device": (403, "DEVICE_TOKEN_REJECTED"),
    "wrong-prefix": (403, "DEVICE_TOKEN_REJECTED"),
    "wrong-length": (403, "DEVICE_TOKEN_REJECTED"),
    "pairing-code": (403, "DEVICE_TOKEN_REJECTED"),
    "wrong-owner-and-device": (403, "OWNER_PROOF_REQUIRED"),
    "wrong-proxy-and-device": (403, "OWNER_PROOF_REQUIRED"),
    "oversize": (403, "OWNER_PROOF_REQUIRED"),
}
OWNER_PROVEN = {"owner", "proxy-json"}


@pytest_asyncio.fixture
async def another_agents_token(db):
    _device, token = await DeviceStore(db, ANOTHER_AGENT).create("Elsewhere", f"inst-{uuid.uuid4().hex[:8]}")
    yield token
    async with db.session() as session:
        await session.execute(delete(A2uiDevice).where(A2uiDevice.agent_id == ANOTHER_AGENT))
        await session.commit()


async def _proofs(app: Any, another: str) -> dict[str, dict[str, str]]:
    """Each proof as the headers of a request; the device rows pair a live and a revoked device on ``app``."""
    live = await _pair(app, "inst-live")
    gone = await _pair(app, "inst-gone")
    assert (await _call(app, "DELETE", f"/a2ui/devices/{gone['device_id']}", headers=OWNER)).status_code == 200

    def token(value: str) -> dict[str, str]:
        return {"X-Nous-Device-Token": value, "content-type": JSON}

    proofs = {
        "none": {"content-type": JSON},
        "owner": OWNER,
        "proxy-json": PROXY,
        "proxy-text": {"X-Nous-Proxy-Secret": PROXY_VALUE, "content-type": "text/plain"},
        "device": token(live["token"]),
        "revoked-device": token(gone["token"]),
        "another-agents-device": token(another),
        "wrong-prefix": token("nd2_" + live["token"][4:]),
        "wrong-length": token(live["token"][:-1]),
        "pairing-code": token("K7QM-2XRD"),
        "wrong-owner-and-device": {**token(live["token"]), "X-Nous-Owner-Secret": "a-wrong-value"},
        "wrong-proxy-and-device": {**token(live["token"]), "X-Nous-Proxy-Secret": "a-wrong-value"},
        "oversize": token("nd1_" + "A" * 197),
    }
    assert list(proofs) == PROOF_NAMES
    return proofs


async def _card(db, service: SurfaceService, verbs: list[str]) -> tuple[str, str]:
    """A live approval card that offers ``verbs``: ``(surface_id, nonce)``."""
    surface_id = await service.push_built(approval_gate(APPROVAL_PARAMS))
    async with db.session() as session:
        await session.execute(
            update(A2uiSurface).where(A2uiSurface.surface_id == surface_id).values(allowed_actions=verbs)
        )
        await session.commit()
        nonce = (await session.execute(select(A2uiSurface.nonce).where(A2uiSurface.surface_id == surface_id))).scalar()
    return surface_id, nonce


def _action(verb: str, surface_id: str, nonce: str, **context: Any) -> bytes:
    body = {
        "version": "v1.0",
        "action": {
            "name": verb,
            "surfaceId": surface_id,
            "context": {"optionId": "never-offered", **context},
            "metadata": {"extensions": {"com_nous_nonce": nonce}},
        },
    }
    return json.dumps(body).encode()


APPROVAL_PARAMS = {
    "title": "Cancel the running backfill?",
    "summary": "It has processed 40% of rows.",
    "risk": "Partial state; resumable from the watermark.",
    "options": [{"id": "cancel", "label": "Cancel"}, {"id": "let_it_run", "label": "Let it run"}],
}


@pytest.mark.parametrize("proof", PROOF_NAMES)
async def test_each_verb_that_needs_proof_against_each_proof(
    db, owned, service: SurfaceService, push: PushService, another_agents_token: str, proof: str
) -> None:
    """A device token is a proof on exactly the verbs that need one, like the owner's and the proxy's secrets; a token
    no live device has is ``DEVICE_TOKEN_REJECTED``, and a failed secret is not rescued by a valid token. The verbs are
    read from the router, so a verb added with ``owner_proof=True`` is in the matrix at once."""
    router = ActionRouter(db, owned, service)
    gated = sorted(name for name, meta in router._handlers.items() if meta.owner_proof)
    assert {"approval.choose", "app.act"} <= set(gated)
    app = _app(db, owned, service, push, router)
    headers = (await _proofs(app, another_agents_token))[proof]
    surface_id, nonce = await _card(db, service, gated)

    for verb in gated:
        response = await _call(app, "POST", "/a2ui/action", content=_action(verb, surface_id, nonce), headers=headers)
        error = response.json().get("error") or {}
        assert (response.status_code, error.get("code")) == VERB_OUTCOME[proof], (verb, response.json())
    async with db.session() as session:
        status = (
            await session.execute(select(A2uiSurface.status).where(A2uiSurface.surface_id == surface_id))
        ).scalar()
    assert status == "live"


async def _owner_routes(app: Any) -> list[tuple[str, str]]:
    found = sorted(
        (method, route.path)
        for route in app.routes
        if getattr(route, "methods", None) and getattr(route.endpoint, "owner_guarded", False)
        for method in route.methods - {"HEAD"}
    )
    assert ("POST", "/a2ui/devices/pairings") in found and ("POST", "/schedules") in found
    return found


@pytest.mark.parametrize("proof", PROOF_NAMES)
async def test_a_device_token_is_refused_on_every_owner_route(
    db, owned, service: SurfaceService, push: PushService, another_agents_token: str, proof: str
) -> None:
    """Every route behind the owner guard, read from the app's route table (the device management routes among them):
    only the owner's and the proxy's secrets pass; every device row is 403 before the handler."""
    app = _app(db, owned, service, push, ActionRouter(db, owned, service))
    headers = (await _proofs(app, another_agents_token))[proof]
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://nous") as client:
        for method, path in await _owner_routes(app):
            concrete = re.sub(r"\{[^}]+\}", "ffffffff", path)
            response = await client.request(method, concrete, content=b"{}", headers=headers)
            if proof in OWNER_PROVEN:
                assert response.status_code not in (403, 415), (method, path, response.text)
            elif proof == "proxy-text":
                assert response.status_code == (415 if method != "GET" else 200), (method, path)
            else:
                assert (response.status_code, response.json()) == (403, {"error": "forbidden"}), (method, path, proof)


async def test_a_device_token_is_not_a_proof_on_chat(db, owned, service: SurfaceService, push: PushService) -> None:
    enforce = owned.model_copy(update={"owner_entry_mode": "enforce"})
    app = _app(db, enforce, service, push)
    device = await _pair(app)
    token = {"X-Nous-Device-Token": device["token"], "content-type": JSON}
    for method, path in (("POST", "/chat"), ("POST", "/chat/stream"), ("DELETE", "/chat/abc")):
        response = await _call(app, method, path, json={"message": "hi"}, headers=token)
        assert (response.status_code, response.json()) == (403, {"error": "forbidden"}), path
    warn = _app(db, owned.model_copy(update={"owner_entry_mode": "warn"}), service, push)
    warned = await _pair(warn)
    transport = httpx.ASGITransport(app=warn, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://nous") as client:
        await client.post("/chat", json={"message": "hi"}, headers={"X-Nous-Device-Token": warned["token"]})
    assert sum(warn.state.entry_gate.drain_counts().values()) == 1  # counted as a call that carried no proof


async def test_a_device_token_is_not_a_proof_on_the_mcp_server(
    db, owned, service: SurfaceService, push: PushService
) -> None:
    """The inbound MCP server is wrapped with the app's entry gate, as main.build_app wraps it."""
    for mode in ("enforce", "warn"):
        app = _app(db, owned.model_copy(update={"owner_entry_mode": mode, "mcp_enabled": True}), service, push)
        device = await _pair(app)
        reached: list[str] = []

        async def mcp(scope: Any, receive: Any, send: Any) -> None:
            reached.append(scope["path"])
            await JSONResponse({"mcp": True})(scope, receive, send)

        gated = Starlette(routes=[Mount("/mcp", app=app.state.entry_gate.asgi(mcp, name="/mcp"))])
        headers = {"X-Nous-Device-Token": device["token"], "content-type": JSON}
        response = await _call(gated, "POST", "/mcp/", json={}, headers=headers)
        if mode == "enforce":
            assert (response.status_code, reached) == (403, [])
        else:
            assert response.status_code == 200
            assert sum(app.state.entry_gate.drain_counts().values()) == 1


async def test_a_device_token_grants_nothing_on_the_mcp_client_routes(
    db, owned, service: SurfaceService, push: PushService
) -> None:
    """Every ``/integrations/mcp`` route answers a device token exactly as it answers a call with no header: 403 on
    the routes that need the proxy's secret, and the same answer on the others."""
    app = _app(db, owned.model_copy(update={"mcp_client_enabled": True}), service, push)
    device = await _pair(app)
    routes = [route for route in app.routes if getattr(route, "path", "").startswith("/integrations/mcp")]
    refused = 0
    for route in routes:
        concrete = re.sub(r"\{[^}]+\}", "x", route.path)
        for method in sorted(route.methods - {"HEAD"}):
            bare = await _call(app, method, concrete, json={})
            with_token = await _call(app, method, concrete, json={}, headers={"X-Nous-Device-Token": device["token"]})
            assert (with_token.status_code, with_token.content) == (bare.status_code, bare.content), (
                method,
                route.path,
            )
            refused += with_token.status_code == 403
    assert refused >= 5


# ---- revocation, the limiter and the logs, through the real route and router ---------------------------------


async def test_revocation_takes_effect_on_the_next_call(
    db, owned, service: SurfaceService, push: PushService, agent_id: str
) -> None:
    app = _app(db, owned, service, push, ActionRouter(db, owned, service))
    device = await _pair(app, "inst-phone")
    await push.register("inst-phone", fcm_token="fcm-phone")
    token = {"X-Nous-Device-Token": device["token"], "content-type": JSON}
    first, nonce = await _card(db, service, ["approval.choose"])
    acted = await _call(
        app, "POST", "/a2ui/action", content=_action("approval.choose", first, nonce, optionId="cancel"), headers=token
    )
    assert acted.status_code == 200, acted.text
    assert (await _call(app, "DELETE", f"/a2ui/devices/{device['device_id']}", headers=OWNER)).status_code == 200
    second, nonce = await _card(db, service, ["approval.choose"])
    refused = await _call(
        app, "POST", "/a2ui/action", content=_action("approval.choose", second, nonce, optionId="cancel"), headers=token
    )
    assert refused.status_code == 403
    assert refused.json()["error"]["code"] == "DEVICE_TOKEN_REJECTED"
    assert refused.json()["error"]["message"] == "This device is no longer paired. Pair it again in the app's settings."
    assert await _installations(db, agent_id) == set()


async def test_revoking_drops_the_devices_rate_window(db, owned, service: SurfaceService, push: PushService) -> None:
    router = ActionRouter(db, owned, service)
    app = _app(db, owned, service, push, router)
    device = await _pair(app)
    surface_id, nonce = await _card(db, service, ["approval.choose"])
    for headers in ({"X-Nous-Device-Token": device["token"], "content-type": JSON}, OWNER):
        await _call(app, "POST", "/a2ui/action", content=_action("approval.choose", surface_id, nonce), headers=headers)
    key = f"device:{device['device_id']}"
    assert {k: len(v) for k, v in router._windows.items()} == {key: 1, "owner": 1}
    await _call(app, "DELETE", f"/a2ui/devices/{device['device_id']}", headers=OWNER)
    assert set(router._windows) == {"owner"}


async def test_a_store_error_is_a_plain_refusal_not_a_rejected_token(
    db, owned, service: SurfaceService, push: PushService, monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    caplog.set_level(logging.DEBUG)
    app = _app(db, owned, service, push, ActionRouter(db, owned, service))
    device = await _pair(app)

    async def broken(self: DeviceStore, token: object) -> None:
        raise RuntimeError(f"the database is down {token}")

    monkeypatch.setattr(DeviceStore, "verify", broken)
    surface_id, nonce = await _card(db, service, ["approval.choose"])
    response = await _call(
        app,
        "POST",
        "/a2ui/action",
        content=_action("approval.choose", surface_id, nonce),
        headers={"X-Nous-Device-Token": device["token"], "content-type": JSON},
    )
    assert (response.status_code, response.json()["error"]["code"]) == (403, "OWNER_PROOF_REQUIRED")
    assert "RuntimeError" in caplog.text
    for record in caplog.records:
        assert device["token"] not in record.getMessage() + repr(record.args)


class _Bus:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def emit(self, event: Any) -> None:
        self.events.append(event)


async def test_no_secret_in_any_log_record_at_debug(
    db, owned, service: SurfaceService, push: PushService, caplog: pytest.LogCaptureFixture, agent_id: str
) -> None:
    """Open, a wrong and a right redeem, a wrong, a revoked and a valid token on an action, a read, the list, a revoke
    and an unpair: no log record, event, audit row or listing carries a token, a code or the digest of either. Header
    names may appear; values never. The "sqlalchemy" logger stays at WARNING (its own default), so statement
    parameters are not logged at all."""
    caplog.set_level(logging.DEBUG)
    bus = _Bus()
    stand_in = SimpleNamespace()
    router = ActionRouter(db, owned, service)
    app = create_app(
        stand_in,
        stand_in,
        SimpleNamespace(schedules=None),
        stand_in,
        db,
        owned,
        bus=bus,
        surface_service=service,
        action_router=router,
        push_service=push,
    )
    opened = (await _call(app, "POST", "/a2ui/devices/pairings", json={"name": "Desk phone"}, headers=OWNER)).json()
    code = opened["code"]
    wrong = "".join("2" if c != "2" else "3" for c in code.replace("-", ""))
    await _call(app, "POST", "/a2ui/devices/redeem", json={"code": wrong, "installation_id": "inst-1"})
    live = (await _call(app, "POST", "/a2ui/devices/redeem", json={"code": code, "installation_id": "inst-1"})).json()
    gone = await _pair(app, "inst-2")
    other = await _pair(app, "inst-3")
    await _call(app, "DELETE", f"/a2ui/devices/{gone['device_id']}", headers=OWNER)
    surface_id, nonce = await _card(db, service, ["approval.choose"])
    for value in (live["token"], gone["token"], live["token"][:-1]):
        await _call(
            app,
            "POST",
            "/a2ui/action",
            content=_action("approval.choose", surface_id, nonce),
            headers={"X-Nous-Device-Token": value, "content-type": JSON},
        )
    await _call(app, "GET", "/a2ui/surfaces", headers={"X-Nous-Device-Token": live["token"]})
    listing = await _call(app, "GET", "/a2ui/devices", headers=OWNER)
    await _call(app, "DELETE", f"/a2ui/devices/{live['device_id']}", headers=OWNER)
    await _call(app, "POST", "/a2ui/devices/unpair", headers={"X-Nous-Device-Token": other["token"]})

    secrets_seen = []
    for token in (live["token"], gone["token"], other["token"]):
        secrets_seen += [token, hashlib.sha256(token.encode()).hexdigest()]
    for value in (code, code.replace("-", ""), wrong):
        secrets_seen += [value, hashlib.sha256(value.encode()).hexdigest()]
    assert caplog.records
    for record in caplog.records:
        text = record.getMessage() + repr(record.args) + (record.exc_text or "")
        for secret in secrets_seen:
            assert secret not in text, record.getMessage()
    assert [event.data["action"] for event in bus.events] == ["paired", "paired", "paired"]
    for event in bus.events:
        for secret in secrets_seen:
            assert secret not in json.dumps(event.data)
    for secret in secrets_seen:
        assert secret not in listing.text
    async with db.session() as session:
        audits = (await session.execute(select(A2uiAction).where(A2uiAction.agent_id == agent_id))).scalars().all()
    assert [(row.action_name, row.status) for row in audits] == [("approval.choose", "rejected")] * 3
    for row in audits:
        text = " ".join(str(getattr(row, column.key)) for column in A2uiAction.__table__.columns)
        for secret in secrets_seen:
            assert secret not in text
