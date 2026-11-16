"""The owner routes require the owner's or the proxy's secret: which routes, in what order, and what a call with each
secret gets. No database: every call either is refused before any lookup or fails the handler's own check first."""

from __future__ import annotations

import hashlib
import logging
from types import SimpleNamespace

import httpx
import pytest
from starlette.applications import Starlette

from nous.api.intention_routes import build_intention_routes

OWNER_VALUE = "owner-only-test-value"
PROXY_VALUE = "proxy-only-test-value"
OWNER_DIGEST = hashlib.sha256(OWNER_VALUE.encode()).hexdigest()
PROXY_DIGEST = hashlib.sha256(PROXY_VALUE.encode()).hexdigest()
OWNER = {"X-Nous-Owner-Secret": OWNER_VALUE}
PROXY = {"X-Nous-Proxy-Secret": PROXY_VALUE}
FORBIDDEN = {"error": "forbidden"}
NOT_JSON = {"error": "Content-Type must be application/json"}


def _settings(owner: bool = True, proxy: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        agent_id="guard-test",
        continuation_enabled=False,
        owner_route_secret_sha256=OWNER_DIGEST if owner else "",
        mcp_client_ui_secret_sha256=PROXY_DIGEST if proxy else "",
    )


def _intention_app(settings: SimpleNamespace) -> Starlette:
    return Starlette(routes=build_intention_routes(database=None, settings=settings, continuation_runner=None))


async def _send(app: Starlette, method: str, path: str, **kwargs: object) -> httpx.Response:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://nous") as client:
        return await client.request(method, path, **kwargs)


# Each intention POST, with a body its handler refuses before it needs the database: the answer proves the handler
# ran (and the guard let the call through).
INTENTION_POSTS = {
    "/intentions/proposals/ffffffff/decide": (b"{}", {"error": "decision must be 'approve' or 'reject'"}),
    "/intentions/questions/ffffffff/answer": (b"{}", None),  # "text is required (at most N characters)"
    "/intentions/questions/answer": (b"{}", {"error": "chat_id and message_id (integers) and text are required"}),
    "/intentions/ffffffff/cancel": (b"[1]", {"error": "the body must be a JSON object"}),
}
POSTS = list(INTENTION_POSTS)


def _handler_ran(path: str, response: httpx.Response) -> None:
    _probe, expected = INTENTION_POSTS[path]
    assert response.status_code == 400, response.text
    if expected is None:
        assert response.json()["error"].startswith("text is required")
    else:
        assert response.json() == expected


@pytest.mark.parametrize("path", POSTS)
async def test_an_intention_post_without_a_secret_is_403(path: str) -> None:
    response = await _send(_intention_app(_settings()), "POST", path, json={})
    assert response.status_code == 403
    assert response.json() == FORBIDDEN


@pytest.mark.parametrize("path", POSTS)
async def test_the_refusal_comes_before_the_body_and_the_lookup(path: str) -> None:
    # Not JSON, and no database: anything but the guard would answer 400 or raise on the None database.
    response = await _send(_intention_app(_settings()), "POST", path, content=b"not json")
    assert response.status_code == 403
    assert response.json() == FORBIDDEN


@pytest.mark.parametrize("path", POSTS)
@pytest.mark.parametrize(
    ("headers", "allowed"),
    [
        ({}, False),
        (OWNER, True),
        ({**PROXY, "content-type": "application/json"}, True),
        ({**PROXY, "content-type": "application/json; charset=utf-8"}, True),
        ({**PROXY, "content-type": "text/plain"}, False),
        ({**PROXY, "content-type": "text/plain; application/json"}, False),
        ({**OWNER, "content-type": "text/plain"}, True),
    ],
    ids=["none", "owner", "proxy-json", "proxy-json-charset", "proxy-text", "proxy-smuggled-json", "owner-text"],
)
async def test_each_intention_post_against_each_proof(path: str, headers: dict[str, str], allowed: bool) -> None:
    probe, _expected = INTENTION_POSTS[path]
    sent = {"content-type": "application/json", **headers}
    response = await _send(_intention_app(_settings()), "POST", path, content=probe, headers=sent)
    if allowed:
        _handler_ran(path, response)
    elif "X-Nous-Proxy-Secret" in headers:
        assert response.status_code == 415
        assert response.json() == NOT_JSON
    else:
        assert response.status_code == 403
        assert response.json() == FORBIDDEN


@pytest.mark.parametrize("headers", [OWNER, PROXY], ids=["owner", "proxy"])
async def test_with_no_digest_an_intention_post_is_403_even_with_a_header(headers: dict[str, str]) -> None:
    app = _intention_app(_settings(owner=False, proxy=False))
    response = await _send(app, "POST", "/intentions/proposals/ffffffff/decide", json={}, headers=headers)
    assert response.status_code == 403


async def test_the_intention_reads_need_no_secret() -> None:
    response = await _send(_intention_app(_settings()), "GET", "/intentions")
    assert response.status_code == 200
    assert response.json() == {"roots": [], "continuation": False}


def test_exactly_the_intention_posts_are_guarded() -> None:
    routes = build_intention_routes(database=None, settings=_settings(), continuation_runner=None)
    guarded = {route.path for route in routes if getattr(route.endpoint, "owner_guarded", False)}
    assert guarded == {
        "/intentions/proposals/{id}/decide",
        "/intentions/questions/answer",
        "/intentions/questions/{id}/answer",
        "/intentions/{root_id}/cancel",
    }
    for route in routes:
        if route.path in guarded:
            assert route.methods == {"POST"}


async def test_no_log_record_carries_a_secret_on_a_refused_or_an_accepted_call(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    app = _intention_app(_settings())
    path = "/intentions/proposals/ffffffff/decide"
    wrong = {"X-Nous-Owner-Secret": "a-wrong-value", "X-Nous-Proxy-Secret": "another-wrong-value"}
    refused = await _send(app, "POST", path, json={}, headers=wrong)
    by_owner = await _send(app, "POST", path, json={}, headers=OWNER)
    by_proxy = await _send(app, "POST", path, json={}, headers=PROXY)
    assert [refused.status_code, by_owner.status_code, by_proxy.status_code] == [403, 400, 400]
    assert caplog.records  # the refusal was logged
    # The server's records (the test client logs its own request line): the template, never the caller's path.
    messages = [record.getMessage() for record in caplog.records if record.name.startswith("nous")]
    assert any("POST /intentions/proposals/{id}/decide" in message for message in messages)
    assert not any("ffffffff" in message for message in messages)
    for record in caplog.records:
        text = record.getMessage() + repr(record.args)
        for value in (OWNER_VALUE, PROXY_VALUE, "a-wrong-value", "another-wrong-value", OWNER_DIGEST, PROXY_DIGEST):
            assert value not in text


# ---- the owner setting routes in rest.py ----------------------------------------------------------------------

# Each owner setting route, with a body, and what its handler answers with the stand-ins below (proof it ran).
OWNER_ROUTES = {
    ("PUT", "/heartbeat/config"): (b"{}", 503, {"error": "Heartbeat not enabled"}),
    ("PUT", "/heartbeat/escalation-policy"): (b"{}", 503, {"error": "Finding store not available"}),
    ("POST", "/admin/search-weights"): (b'{"vector_weight": "x"}', 400, {"error": "vector_weight must be a number"}),
    ("POST", "/undo/snapshots/abc/restore"): (b"{}", 503, {"error": "Undo journal not enabled"}),
    # F114: the goal changes, with goals off (the default)
    **{
        ("POST", path): (b"{}", 503, {"error": "goals are off", "goals_enabled": False})
        for path in (
            "/goals",
            *(
                f"/goals/G-0001/{action}"
                for action in (
                    "approve", "pause", "resume", "confirm-done", "stop", "cancel", "run-now", "budget", "deadline",
                    "note",
                )
            ),
        )
    },
}
# F114: the goal reads require a secret too; a read carries no body, so any media type passes with the proxy's.
GOAL_READS = {
    "/goals": {"enabled": False, "goals": []},
    "/goals/options": {"enabled": False},
    "/goals/G-0001": {"enabled": False},
}
GOAL_ROUTE_SET = {
    ("GET", "/goals"),
    ("GET", "/goals/options"),
    ("GET", "/goals/{ref}"),
    *{(method, path.replace("G-0001", "{ref}")) for method, path in OWNER_ROUTES if path.startswith("/goals")},
}
INTENTION_ROUTE_SET = {
    ("POST", "/intentions/proposals/{id}/decide"),
    ("POST", "/intentions/questions/answer"),
    ("POST", "/intentions/questions/{id}/answer"),
    ("POST", "/intentions/{root_id}/cancel"),
}
REST_ROUTE_SET = {
    ("PUT", "/heartbeat/config"),
    ("PUT", "/heartbeat/escalation-policy"),
    ("POST", "/admin/search-weights"),
    ("POST", "/undo/snapshots/{id}/restore"),
    ("POST", "/schedules"),
}


def _rest_app(tmp_path, *, owner: bool = True, proxy: bool = True, **kw: object) -> Starlette:
    from nous.api.rest import create_app
    from nous.config import Settings

    settings = Settings(
        _env_file=None,
        workspace_dir=str(tmp_path),
        owner_route_secret_sha256=OWNER_DIGEST if owner else "",
        mcp_client_ui_secret_sha256=PROXY_DIGEST if proxy else "",
    )
    return create_app(
        SimpleNamespace(),
        SimpleNamespace(),
        SimpleNamespace(schedules=None),
        SimpleNamespace(),
        SimpleNamespace(),
        settings,
        heartbeat_runner=SimpleNamespace(finding_store=None),
        **kw,
    )


@pytest.mark.parametrize(("method", "path"), list(OWNER_ROUTES))
async def test_an_owner_setting_route_without_a_secret_is_403(tmp_path, method: str, path: str) -> None:
    app = _rest_app(tmp_path)
    as_json = await _send(app, method, path, json={})
    not_json = await _send(app, method, path, content=b"not json")
    assert (as_json.status_code, not_json.status_code) == (403, 403)
    assert as_json.json() == not_json.json() == FORBIDDEN


@pytest.mark.parametrize(("method", "path"), list(OWNER_ROUTES))
@pytest.mark.parametrize(
    ("headers", "allowed"),
    [
        ({}, False),
        (OWNER, True),
        ({**PROXY, "content-type": "application/json"}, True),
        ({**PROXY, "content-type": "text/plain"}, False),
        ({**PROXY, "content-type": "text/plain; application/json"}, False),
        ({**OWNER, "content-type": "text/plain"}, True),
    ],
    ids=["none", "owner", "proxy-json", "proxy-text", "proxy-smuggled-json", "owner-text"],
)
async def test_each_owner_setting_route_against_each_proof(
    tmp_path, method: str, path: str, headers: dict[str, str], allowed: bool
) -> None:
    probe, status, body = OWNER_ROUTES[(method, path)]
    sent = {"content-type": "application/json", **headers}
    response = await _send(_rest_app(tmp_path), method, path, content=probe, headers=sent)
    if allowed:
        assert (response.status_code, response.json()) == (status, body)
    elif "X-Nous-Proxy-Secret" in headers:
        assert (response.status_code, response.json()) == (415, NOT_JSON)
    else:
        assert (response.status_code, response.json()) == (403, FORBIDDEN)


async def test_a_proxy_call_with_no_body_still_needs_the_json_media_type(tmp_path) -> None:
    """A body-less restore or cancel with only the proxy's secret: 415 without the JSON media type; with it the
    restore reaches its handler (503, no journal here)."""
    rest, intentions = _rest_app(tmp_path), _intention_app(_settings())
    restore = "/undo/snapshots/abc/restore"
    bare_restore = await _send(rest, "POST", restore, headers=PROXY)
    bare_cancel = await _send(intentions, "POST", "/intentions/ffffffff/cancel", headers=PROXY)
    assert (bare_restore.status_code, bare_restore.json()) == (415, NOT_JSON)
    assert (bare_cancel.status_code, bare_cancel.json()) == (415, NOT_JSON)
    typed = await _send(rest, "POST", restore, headers={**PROXY, "content-type": "application/json"})
    assert (typed.status_code, typed.json()) == (503, {"error": "Undo journal not enabled"})


async def test_the_reads_beside_them_need_no_secret(tmp_path) -> None:
    app = _rest_app(tmp_path)
    for path in ("/undo/snapshots", "/admin/search-weights"):
        response = await _send(app, "GET", path)
        assert response.status_code != 403, path


def test_exactly_the_owner_routes_are_guarded(tmp_path) -> None:
    app = _rest_app(tmp_path)
    guarded = {
        (method, route.path)
        for route in app.routes
        if getattr(route, "methods", None) and getattr(route.endpoint, "owner_guarded", False)
        for method in route.methods - {"HEAD"}
    }
    assert guarded == REST_ROUTE_SET | INTENTION_ROUTE_SET | GOAL_ROUTE_SET
    assert len(GOAL_ROUTE_SET) == 14


@pytest.mark.parametrize("path", list(GOAL_READS))
@pytest.mark.parametrize(
    ("headers", "allowed"),
    [({}, False), (OWNER, True), (PROXY, True), ({**PROXY, "content-type": "text/plain"}, True)],
    ids=["none", "owner", "proxy", "proxy-text"],
)
async def test_each_goal_read_against_each_proof(tmp_path, path: str, headers: dict[str, str], allowed: bool) -> None:
    response = await _send(_rest_app(tmp_path), "GET", path, headers=headers)
    expected = (200, GOAL_READS[path]) if allowed else (403, FORBIDDEN)
    assert (response.status_code, response.json()) == expected


async def test_the_goal_routes_answer_from_their_store_when_goals_are_on(tmp_path) -> None:
    """With the flag (and the flags it needs) on, ``/goals/options`` is the options route, not a goal named
    "options": no table is read for it (the database here is a stand-in)."""
    from nous.api.rest import create_app
    from nous.config import Settings

    settings = Settings(
        _env_file=None,
        workspace_dir=str(tmp_path),
        owner_route_secret_sha256=OWNER_DIGEST,
        goals_enabled=True,
        result_inbox_enabled=True,
        intentions_enabled=True,
        continuation_enabled=True,
        subtask_enabled=True,
    )
    assert settings.goals_enabled is True
    app = create_app(
        SimpleNamespace(), SimpleNamespace(), SimpleNamespace(schedules=None), SimpleNamespace(), SimpleNamespace(),
        settings, heartbeat_runner=SimpleNamespace(finding_store=None),
    )
    response = await _send(app, "GET", "/goals/options", headers=OWNER)
    assert response.status_code == 200
    assert response.json()["enabled"] is True and set(response.json()["presets"]) == {"small", "medium", "large"}
    paths = [getattr(route, "path", None) for route in app.routes]
    assert paths.index("/goals/options") < paths.index("/goals/{ref}")


def _unconfigured_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [
        record
        for record in caplog.records
        if "NOUS_OWNER_ROUTE_SECRET_SHA256" in record.getMessage()
        and "NOUS_MCP_CLIENT_UI_SECRET_SHA256" in record.getMessage()
    ]


async def test_an_unconfigured_guard_says_so_once_at_start(tmp_path, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING)
    _rest_app(tmp_path, owner=False, proxy=False)
    assert len(_unconfigured_records(caplog)) == 1
    caplog.clear()
    _rest_app(tmp_path, owner=True, proxy=False)
    _rest_app(tmp_path, owner=False, proxy=True)
    assert _unconfigured_records(caplog) == []


async def test_no_log_record_carries_a_secret_on_an_owner_setting_route(
    tmp_path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    app = _rest_app(tmp_path)
    wrong = {"X-Nous-Owner-Secret": "a-wrong-value", "X-Nous-Proxy-Secret": "another-wrong-value"}
    statuses = [
        (await _send(app, "PUT", "/heartbeat/config", json={}, headers=headers)).status_code
        for headers in (wrong, OWNER, PROXY)
    ]
    assert statuses == [403, 503, 503]
    assert caplog.records
    for record in caplog.records:
        text = record.getMessage() + repr(record.args)
        for value in (OWNER_VALUE, PROXY_VALUE, "a-wrong-value", "another-wrong-value", OWNER_DIGEST, PROXY_DIGEST):
            assert value not in text
