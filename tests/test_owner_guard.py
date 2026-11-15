"""The check that a request carries the owner's or the proxy's secret (``nous/api/owner_guard.py``). No database."""

from __future__ import annotations

import hashlib
import logging
from types import SimpleNamespace

import httpx
import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse

from nous.api.owner_guard import (
    CARD_DECISION,
    DEVICE_TOKEN_HEADER,
    GRANTS,
    OWNER,
    OWNER_SECRET_HEADER,
    PROXY_SECRET_HEADER,
    OwnerGuard,
)

OWNER_VALUE = "owner-only-test-value"
PROXY_VALUE = "proxy-only-test-value"
OWNER_DIGEST = hashlib.sha256(OWNER_VALUE.encode()).hexdigest()
PROXY_DIGEST = hashlib.sha256(PROXY_VALUE.encode()).hexdigest()


def _settings(owner: object = OWNER_DIGEST, proxy: object = PROXY_DIGEST) -> SimpleNamespace:
    return SimpleNamespace(owner_route_secret_sha256=owner, mcp_client_ui_secret_sha256=proxy)


class _Counter:
    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, request: Request) -> JSONResponse:
        self.calls += 1
        return JSONResponse({"ok": True})


def _app(guard: OwnerGuard, handler: _Counter) -> Starlette:
    return Starlette(routes=[guard.route("/x", handler, methods=["GET", "POST"])])


async def _post(app: Starlette, **kwargs: object) -> httpx.Response:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://nous") as client:
        return await client.post("/x", **kwargs)


async def _get(app: Starlette, **kwargs: object) -> httpx.Response:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://nous") as client:
        return await client.get("/x", **kwargs)


def _request(method: str = "POST", headers: dict[str, str] | None = None) -> Request:
    raw = [(name.lower().encode("latin-1"), value.encode("latin-1")) for name, value in (headers or {}).items()]
    return Request({"type": "http", "method": method, "path": "/a2ui/action", "headers": raw, "query_string": b""})


class _Devices:
    """A device store stand-in: ``verify`` answers ``result`` (or raises ``error``) and counts its calls."""

    def __init__(self, result: bool = True, error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.calls = 0

    async def verify(self, token: str | None) -> bool:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.result


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {OWNER_SECRET_HEADER: "wrong-owner-value"},
        {PROXY_SECRET_HEADER: "wrong-proxy-value"},
        {PROXY_SECRET_HEADER: OWNER_VALUE},
        {OWNER_SECRET_HEADER: PROXY_VALUE},
    ],
    ids=["none", "wrong-owner", "wrong-proxy", "owner-value-in-proxy-header", "proxy-value-in-owner-header"],
)
async def test_a_call_with_neither_secret_is_403_and_the_handler_never_runs(headers: dict[str, str]) -> None:
    handler = _Counter()
    response = await _post(_app(OwnerGuard(_settings()), handler), json={}, headers=headers)
    assert response.status_code == 403
    assert response.json() == {"error": "forbidden"}
    assert handler.calls == 0


async def test_either_secret_lets_the_call_through() -> None:
    handler = _Counter()
    app = _app(OwnerGuard(_settings()), handler)
    owner = await _post(app, json={}, headers={OWNER_SECRET_HEADER: OWNER_VALUE})
    proxy = await _post(app, json={}, headers={PROXY_SECRET_HEADER: PROXY_VALUE})
    assert (owner.status_code, proxy.status_code) == (200, 200)
    assert handler.calls == 2


@pytest.mark.parametrize("settings", [SimpleNamespace(), _settings(owner="", proxy="")], ids=["no-attrs", "empty"])
async def test_with_no_digest_set_every_call_is_refused(settings: SimpleNamespace) -> None:
    guard = OwnerGuard(settings)
    assert guard.configured is False
    handler = _Counter()
    app = _app(guard, handler)
    for headers in ({OWNER_SECRET_HEADER: OWNER_VALUE}, {PROXY_SECRET_HEADER: PROXY_VALUE}, {OWNER_SECRET_HEADER: ""}):
        response = await _post(app, json={}, headers=headers)
        assert response.status_code == 403
    assert handler.calls == 0


@pytest.mark.parametrize(
    "raw", ["abc", "z" * 64, "é" * 64, 5, None], ids=["short", "not-hex", "non-ascii", "int", "none"]
)
async def test_a_value_that_is_not_a_digest_counts_as_unset(raw: object) -> None:
    guard = OwnerGuard(_settings(owner=raw, proxy=raw))
    assert guard.configured is False
    handler = _Counter()
    app = _app(guard, handler)
    for value in (OWNER_VALUE, PROXY_VALUE, "abc", "z" * 64):
        response = await _post(app, json={}, headers={OWNER_SECRET_HEADER: value})
        assert response.status_code == 403
        response = await _post(app, json={}, headers={PROXY_SECRET_HEADER: value})
        assert response.status_code == 403
    assert handler.calls == 0


@pytest.mark.parametrize(
    "settings",
    [SimpleNamespace(), _settings(owner="", proxy=""), _settings(owner=OWNER_DIGEST, proxy=""), _settings(proxy="")],
    ids=["no-attrs", "both-empty", "owner-only", "proxy-empty"],
)
@pytest.mark.parametrize("header", [OWNER_SECRET_HEADER, PROXY_SECRET_HEADER])
async def test_an_empty_header_never_matches_an_unset_digest(settings: SimpleNamespace, header: str) -> None:
    """An empty value in either header proves nothing, whichever digest is unset: an empty header must never equal
    an empty digest."""
    guard = OwnerGuard(settings)
    request = _request(headers={header: "", "content-type": "application/json"})
    assert guard.proof(request) is None
    assert guard.allows(request) is False
    assert await guard.grants(request, OWNER) is False
    assert await guard.grants(request, CARD_DECISION) is False
    handler = _Counter()
    response = await _post(_app(guard, handler), json={}, headers={header: ""})
    assert response.status_code == 403
    assert handler.calls == 0


@pytest.mark.parametrize("raw", [f"{OWNER_DIGEST}  -", OWNER_DIGEST.upper()], ids=["sha256sum", "upper"])
async def test_sha256sum_output_is_accepted(raw: str) -> None:
    guard = OwnerGuard(_settings(owner=raw, proxy=""))
    assert guard.configured is True
    response = await _post(_app(guard, _Counter()), json={}, headers={OWNER_SECRET_HEADER: OWNER_VALUE})
    assert response.status_code == 200


async def test_a_settings_object_with_only_one_digest_works() -> None:
    guard = OwnerGuard(SimpleNamespace(owner_route_secret_sha256=OWNER_DIGEST))
    app = _app(guard, _Counter())
    assert (await _post(app, json={}, headers={OWNER_SECRET_HEADER: OWNER_VALUE})).status_code == 200
    assert (await _post(app, json={}, headers={PROXY_SECRET_HEADER: PROXY_VALUE})).status_code == 403


async def test_a_proxy_call_must_be_json() -> None:
    handler = _Counter()
    app = _app(OwnerGuard(_settings()), handler)
    proxy = {PROXY_SECRET_HEADER: PROXY_VALUE}
    plain = await _post(app, content=b"{}", headers={**proxy, "content-type": "text/plain"})
    smuggled = await _post(app, content=b"{}", headers={**proxy, "content-type": "text/plain; application/json"})
    assert (plain.status_code, smuggled.status_code) == (415, 415)
    assert plain.json() == {"error": "Content-Type must be application/json"}
    assert handler.calls == 0
    charset = await _post(app, content=b"{}", headers={**proxy, "content-type": "application/json; charset=utf-8"})
    assert charset.status_code == 200
    read = await _get(app, headers=proxy)
    assert read.status_code == 200


async def test_an_owner_call_is_not_held_to_the_json_rule() -> None:
    app = _app(OwnerGuard(_settings()), _Counter())
    response = await _post(app, content=b"{}", headers={OWNER_SECRET_HEADER: OWNER_VALUE, "content-type": "text/plain"})
    assert response.status_code == 200


async def test_the_refusal_logs_the_header_names_and_never_a_value(caplog: pytest.LogCaptureFixture) -> None:
    sent = "a-wrong-value-that-must-not-be-logged"
    caplog.set_level(logging.DEBUG)
    app = _app(OwnerGuard(_settings()), _Counter())
    response = await _post(app, json={}, headers={OWNER_SECRET_HEADER: sent})
    assert response.status_code == 403
    assert OWNER_SECRET_HEADER in caplog.text
    assert PROXY_SECRET_HEADER in caplog.text
    for record in caplog.records:
        assert sent not in record.getMessage()
        assert sent not in repr(record.args)


async def test_the_refusal_logs_the_route_template_and_never_the_callers_path(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    handler = _Counter()
    app = Starlette(routes=[OwnerGuard(_settings()).route("/things/{id}", handler, methods=["POST"])])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://nous") as client:
        response = await client.post("/things/caller-chosen-segment", json={})
    assert response.status_code == 403
    assert handler.calls == 0
    refusals = [record.getMessage() for record in caplog.records if "refused" in record.getMessage()]
    assert refusals == [f"refused POST /things/{{id}}: no valid {OWNER_SECRET_HEADER} or {PROXY_SECRET_HEADER}"]
    for record in caplog.records:
        if record.name.startswith("nous"):  # the server's records; the test client logs its own request line
            assert "caller-chosen-segment" not in record.getMessage() + repr(record.args)


def test_the_wrapper_is_marked() -> None:
    async def handler(request: Request) -> JSONResponse:
        return JSONResponse({})

    wrapped = OwnerGuard(_settings()).protect(handler, "/x")
    assert wrapped.owner_guarded is True
    assert wrapped.__name__ == "handler"
    route = OwnerGuard(_settings()).route("/x", handler, methods=["POST"])
    assert (route.path, route.methods, route.endpoint.owner_guarded) == ("/x", {"POST"}, True)


def test_the_proxy_header_name_is_the_mcp_routes_name() -> None:
    from nous.api import mcp_client_routes

    assert PROXY_SECRET_HEADER == mcp_client_routes.PROXY_SECRET_HEADER


def test_the_grants_table_is_the_rule() -> None:
    assert GRANTS == {
        "owner": frozenset({OWNER, CARD_DECISION}),
        "proxy": frozenset({OWNER, CARD_DECISION}),
        "device": frozenset({CARD_DECISION}),
    }
    assert (OWNER, CARD_DECISION, DEVICE_TOKEN_HEADER) == ("owner", "card_decision", "X-Nous-Device-Token")


async def test_a_device_token_grants_the_card_decision_and_nothing_else() -> None:
    devices = _Devices(result=True)
    guard = OwnerGuard(_settings(), devices=devices)
    headers = {DEVICE_TOKEN_HEADER: "a-device-token", "content-type": "application/json"}
    assert await guard.grants(_request(headers=headers), CARD_DECISION) is True
    assert devices.calls == 1
    assert await guard.grants(_request(headers=headers), OWNER) is False
    assert devices.calls == 1  # the owner capability never looks a device up
    handler = _Counter()
    response = await _post(_app(guard, handler), json={}, headers={DEVICE_TOKEN_HEADER: "a-device-token"})
    assert response.status_code == 403
    assert handler.calls == 0
    assert devices.calls == 1


async def test_with_no_device_store_a_device_header_grants_nothing() -> None:
    guard = OwnerGuard(_settings())
    request = _request(headers={DEVICE_TOKEN_HEADER: "a-device-token", "content-type": "application/json"})
    assert await guard.grants(request, CARD_DECISION) is False
    assert await guard.decide(request, CARD_DECISION) == (False, None)


async def test_a_proxy_proof_without_json_grants_no_capability() -> None:
    guard = OwnerGuard(_settings())
    proxy = {PROXY_SECRET_HEADER: PROXY_VALUE}
    smuggled = _request(headers={**proxy, "content-type": "text/plain; application/json"})
    assert await guard.grants(smuggled, CARD_DECISION) is False
    assert await guard.grants(smuggled, OWNER) is False
    charset = _request(headers={**proxy, "content-type": "application/json; charset=utf-8"})
    assert await guard.grants(charset, CARD_DECISION) is True
    owner_plain = _request(headers={OWNER_SECRET_HEADER: OWNER_VALUE, "content-type": "text/plain"})
    assert await guard.grants(owner_plain, CARD_DECISION) is True
    assert await guard.grants(_request("GET", headers=proxy), OWNER) is True


async def test_a_store_error_is_a_refusal_and_not_a_rejected_token(caplog: pytest.LogCaptureFixture) -> None:
    token = "a-device-token-that-must-not-be-logged"
    caplog.set_level(logging.DEBUG)
    devices = _Devices(error=RuntimeError(f"boom {token}"))
    guard = OwnerGuard(_settings(), devices=devices)
    request = _request(headers={DEVICE_TOKEN_HEADER: token, "content-type": "application/json"})
    assert await guard.decide(request, CARD_DECISION) == (False, None)
    assert devices.calls == 1
    assert "RuntimeError" in caplog.text
    for record in caplog.records:
        assert token not in record.getMessage()
        assert token not in repr(record.args)
        assert record.exc_info is None


async def test_a_token_no_live_device_has_is_a_rejected_token() -> None:
    guard = OwnerGuard(_settings(), devices=_Devices(result=False))
    request = _request(headers={DEVICE_TOKEN_HEADER: "a-device-token", "content-type": "application/json"})
    assert await guard.decide(request, CARD_DECISION) == (False, "device_token_rejected")


async def test_an_oversize_device_header_is_refused_without_a_lookup() -> None:
    devices = _Devices(result=True)
    guard = OwnerGuard(_settings(), devices=devices)
    request = _request(headers={DEVICE_TOKEN_HEADER: "x" * 201, "content-type": "application/json"})
    assert await guard.decide(request, CARD_DECISION) == (False, None)
    assert devices.calls == 0
