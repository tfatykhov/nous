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
JUDGE_SECOND_OFF = {"error": "The second judge's settings are off on this server.", "enabled": False}


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
    # A handler that raises answers 500 rather than failing the test: the guard never answers 500.
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://nous") as client:
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

# Each owner route in rest.py, with a body, and what its handler answers with the stand-ins below (proof it ran).
# A body of None: only the status is compared (the stand-in database has no session, and the handler says so).
OWNER_ROUTES: dict[tuple[str, str], tuple[bytes, int, dict[str, object] | None]] = {
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
                    "approve", "pause", "resume", "confirm-done", "stop", "cancel", "run-now", "budget", "sessions",
                    "deadline", "note", "contained",
                )
            ),
        )
    },
    # F115 per-goal tools: a goal's tool changes, with goals off (the default)
    **{
        (method, "/goals/G-0001/tools/send_email"): (b"{}", 503, {"error": "goals are off", "goals_enabled": False})
        for method in ("PUT", "DELETE")
    },
    # The default goal prompt: a change, with goals off (the default)
    **{
        (method, "/goals/prompt"): (b"{}", 503, {"error": "goals are off", "goals_enabled": False})
        for method in ("PUT", "DELETE")
    },
    # F115: the autonomy changes, with the autonomy settings off (the default)
    **{
        (method, path): (b"{}", 503, {"error": "Autonomy settings are off on this server.", "enabled": False})
        for method in ("PUT", "DELETE")
        for path in ("/autonomy/limits/max_turns", "/autonomy/tools/web_search")
    },
    # Decision review, facts, the profile card, censors and identity
    ("POST", "/decisions/abc/review"): (b"{}", 400, {"error": "outcome is required"}),
    ("POST", "/facts/abc/core"): (b"{}", 400, {"error": "invalid fact id"}),
    ("DELETE", "/profile-card/entries/abc"): (b"", 500, None),
    ("POST", "/profile-card/rebuild"): (b"", 409, {"error": "the profile card writer is off"}),
    ("PUT", "/facts/abc"): (b"{}", 400, {"error": "invalid fact id"}),
    ("DELETE", "/facts/abc"): (b"", 400, {"error": "invalid fact id"}),
    ("PUT", "/censors/abc"): (b"{}", 400, {"error": "No fields to update"}),
    ("PUT", "/identity/nonsense"): (b"{}", 503, {"error": "Identity manager not initialized"}),
    ("POST", "/reinitiate"): (b"", 503, {"error": "Identity manager not initialized"}),
    # Subtasks and schedules
    ("DELETE", "/subtasks/abc"): (b"", 400, {"error": "Invalid subtask ID"}),
    ("DELETE", "/schedules/abc"): (b"", 400, {"error": "Invalid schedule ID"}),
    ("POST", "/schedules/abc/run"): (b"", 400, {"error": "Invalid schedule ID"}),
    # Heartbeat operations and sleep
    ("POST", "/heartbeat/trigger"): (b"", 503, {"error": "Heartbeat not enabled"}),
    ("POST", "/heartbeat/findings/abc/acknowledge"): (b"", 503, {"error": "Finding store not available"}),
    ("POST", "/heartbeat/findings/abc/resolve"): (b"", 503, {"error": "Finding store not available"}),
    ("POST", "/heartbeat/findings/abc/dismiss"): (b"", 503, {"error": "Finding store not available"}),
    ("POST", "/heartbeat/tune"): (b"", 400, {"error": "Tuning not enabled"}),
    ("POST", "/heartbeat/check/abc/trigger"): (b"", 503, {"error": "Heartbeat not enabled"}),
    ("POST", "/heartbeat/check/abc/reset"): (b"", 503, {"error": "Heartbeat not enabled"}),
    ("POST", "/heartbeat/checks/dynamic"): (b"{}", 503, {"error": "Heartbeat not enabled"}),
    ("PATCH", "/heartbeat/checks/dynamic/abc"): (b"{}", 503, {"error": "Heartbeat not enabled"}),
    ("DELETE", "/heartbeat/checks/dynamic/abc"): (b"", 503, {"error": "Heartbeat not enabled"}),
    ("POST", "/heartbeat/checks/dynamic/abc/trigger"): (b"", 503, {"error": "Heartbeat not enabled"}),
    ("POST", "/sleep/trigger"): (b"", 503, {"error": "Sleep handler not configured"}),
    # Rubric and the memory-write-health acknowledgement
    ("POST", "/rubric/propose-dimension"): (b"{}", 503, {"error": "Rubric system not enabled"}),
    ("POST", "/rubric/proposals/abc/approve"): (b"{}", 503, {"error": "Rubric system not enabled"}),
    ("POST", "/rubric/rollback"): (b"{}", 503, {"error": "Rubric system not enabled"}),
    ("POST", "/rubric/evolve"): (b"", 503, {"error": "Rubric evolution not enabled"}),
    ("POST", "/dashboard/memory-write-health/acknowledge"): (
        b"{}",
        400,
        {"error": "rule must be one of ['H1', 'F1', 'S1', 'P1', 'R1', 'A1']"},
    ),
    # F118 PR 3: the second judge's settings, with the setting off (the default): refused before the body is read
    **{
        (method, path): (b"{}", 409, JUDGE_SECOND_OFF)
        for method, path in (
            ("PATCH", "/dashboard/outside-judge/second"),
            ("PUT", "/dashboard/outside-judge/second/key"),
            ("DELETE", "/dashboard/outside-judge/second/key"),
            ("POST", "/dashboard/outside-judge/second/check"),
        )
    },
}
# F114: the goal reads require a secret too; a read carries no body, so any media type passes with the proxy's.
GOAL_READS = {
    "/goals": {"enabled": False, "goals": []},
    "/goals/options": {"enabled": False},
    "/goals/G-0001": {"enabled": False},
    "/goals/G-0001/tools": {"enabled": False},
    "/goals/G-0001/activity": {"enabled": False},
    "/goals/commands": {"enabled": False, "allowed": False, "active": []},
    "/goals/prompt": {"enabled": False},
}
# F115: the autonomy reads require a secret too.
AUTONOMY_READS = {
    "/autonomy/limits": {"enabled": False},
    "/autonomy/log": {"enabled": False},
    "/autonomy/tools": {"enabled": False},
}
# F118 PR 3: the second judge's settings (nous/api/judge_second_routes.py)
JUDGE_SECOND_ROUTE_SET = {
    ("GET", "/dashboard/outside-judge/second"),
    ("PATCH", "/dashboard/outside-judge/second"),
    ("PUT", "/dashboard/outside-judge/second/key"),
    ("DELETE", "/dashboard/outside-judge/second/key"),
    ("POST", "/dashboard/outside-judge/second/check"),
}
AUTONOMY_ROUTE_SET = {
    ("GET", "/autonomy/limits"),
    ("PUT", "/autonomy/limits/{key}"),
    ("DELETE", "/autonomy/limits/{key}"),
    ("GET", "/autonomy/log"),
    ("GET", "/autonomy/tools"),
    ("PUT", "/autonomy/tools/{tool}"),
    ("DELETE", "/autonomy/tools/{tool}"),
}
GOAL_ROUTE_SET = {
    ("GET", "/goals"),
    ("GET", "/goals/options"),
    ("GET", "/goals/commands"),
    ("GET", "/goals/prompt"),
    ("GET", "/goals/{ref}"),
    ("GET", "/goals/{ref}/tools"),
    ("GET", "/goals/{ref}/activity"),
    # The {tool} of a goal's tool route is not made by the G-0001 replacement below.
    ("PUT", "/goals/{ref}/tools/{tool}"),
    ("DELETE", "/goals/{ref}/tools/{tool}"),
    *{
        (method, path.replace("G-0001", "{ref}"))
        for method, path in OWNER_ROUTES
        if path.startswith("/goals") and "/tools/" not in path
    },
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
    ("POST", "/decisions/{id}/review"),
    ("POST", "/facts/{fact_id}/core"),
    ("DELETE", "/profile-card/entries/{entry_id}"),
    ("POST", "/profile-card/rebuild"),
    ("PUT", "/facts/{fact_id}"),
    ("DELETE", "/facts/{fact_id}"),
    ("PUT", "/censors/{id}"),
    ("PUT", "/identity/{section}"),
    ("POST", "/reinitiate"),
    ("DELETE", "/subtasks/{id}"),
    ("DELETE", "/schedules/{id}"),
    ("POST", "/schedules/{id}/run"),
    ("POST", "/heartbeat/trigger"),
    ("POST", "/heartbeat/findings/{fingerprint}/acknowledge"),
    ("POST", "/heartbeat/findings/{fingerprint}/resolve"),
    ("POST", "/heartbeat/findings/{fingerprint}/dismiss"),
    ("POST", "/heartbeat/tune"),
    ("POST", "/heartbeat/check/{name}/trigger"),
    ("POST", "/heartbeat/check/{name}/reset"),
    ("POST", "/heartbeat/checks/dynamic"),
    ("PATCH", "/heartbeat/checks/dynamic/{name}"),
    ("DELETE", "/heartbeat/checks/dynamic/{name}"),
    ("POST", "/heartbeat/checks/dynamic/{name}/trigger"),
    ("POST", "/sleep/trigger"),
    ("POST", "/rubric/propose-dimension"),
    ("POST", "/rubric/proposals/{id}/approve"),
    ("POST", "/rubric/rollback"),
    ("POST", "/rubric/evolve"),
    ("POST", "/dashboard/memory-write-health/acknowledge"),
    ("GET", "/dashboard/about"),
    ("GET", "/a2ui/push/installations"),
    ("GET", "/dashboard/work/sessions"),
    ("GET", "/dashboard/work/sessions/{session_id}"),
    ("GET", "/dashboard/work/calls/{call_id}/result"),
    ("GET", "/dashboard/model-calls/{call_id}/blocks/{part}/{ord}"),
    ("GET", "/dashboard/execution/{entry_id}"),
    # F118: the outside judge's numbers, with the judge's sentence on each disagreement
    ("GET", "/dashboard/outside-judge"),
    ("GET", "/context/log/{id}/payload"),
    ("GET", "/context/log/{id}/sections"),
    # Companion devices: the owner opens a pairing, lists and revokes (nous/api/companion_devices.py)
    ("POST", "/a2ui/devices/pairings"),
    ("GET", "/a2ui/devices"),
    ("DELETE", "/a2ui/devices/{id}"),
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


def _owner_route_ran(method: str, path: str, response: httpx.Response, *, owner_text: bool = False) -> None:
    """The answer the route's handler gives with the stand-ins (the guard let the call through)."""
    _probe, status, body = OWNER_ROUTES[(method, path)]
    if owner_text and (method, path) in OWN_MEDIA_CHECK:
        status, body = 415, NOT_JSON  # the handler's own media type check
    assert response.status_code == status, response.text
    if body is not None:
        assert response.json() == body


# Routes whose handler checks the media type itself: with the owner's secret and another type, the handler answers 415.
OWN_MEDIA_CHECK = {("POST", "/dashboard/memory-write-health/acknowledge")}


@pytest.mark.parametrize(("method", "path"), list(OWNER_ROUTES))
async def test_an_owner_setting_route_without_a_secret_is_403(tmp_path, method: str, path: str) -> None:
    app = _rest_app(tmp_path)
    as_json = await _send(app, method, path, json={})
    not_json = await _send(app, method, path, content=b"not json")
    assert (as_json.status_code, not_json.status_code) == (403, 403)
    assert as_json.json() == not_json.json() == FORBIDDEN


@pytest.mark.parametrize(("method", "path"), list(OWNER_ROUTES))
@pytest.mark.parametrize(
    "headers",
    [OWNER, {**PROXY, "content-type": "application/json"}],
    ids=["owner", "proxy-json"],
)
async def test_with_no_digest_each_owner_route_is_403_even_with_a_header(
    tmp_path, method: str, path: str, headers: dict[str, str]
) -> None:
    probe, _status, _body = OWNER_ROUTES[(method, path)]
    app = _rest_app(tmp_path, owner=False, proxy=False)
    response = await _send(app, method, path, content=probe, headers=headers)
    assert (response.status_code, response.json()) == (403, FORBIDDEN)


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
    probe, _status, _body = OWNER_ROUTES[(method, path)]
    sent = {"content-type": "application/json", **headers}
    response = await _send(_rest_app(tmp_path), method, path, content=probe, headers=sent)
    if allowed:
        _owner_route_ran(method, path, response, owner_text=sent["content-type"] != "application/json")
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
    # A body-less DELETE, as the dashboard sends one.
    bare_delete = await _send(rest, "DELETE", "/facts/abc", headers=PROXY)
    typed_delete = await _send(rest, "DELETE", "/facts/abc", headers={**PROXY, "content-type": "application/json"})
    assert (bare_delete.status_code, bare_delete.json()) == (415, NOT_JSON)
    assert (typed_delete.status_code, typed_delete.json()) == (400, {"error": "invalid fact id"})


# Each guarded read, and what its handler answers with the stand-ins (proof it ran). A GET with only the proxy's
# secret needs no media type.
GUARDED_READS: dict[str, tuple[int, dict[str, object] | None]] = {
    "/a2ui/push/installations": (503, {"error": "A2UI not available"}),
    # The About screen: the stand-in database cannot be read, so its values are null
    "/dashboard/about": (200, None),
    # The stored prompt, response, message and tool-result reads
    "/dashboard/work/sessions": (200, None),
    "/dashboard/work/sessions/abc": (200, {"enabled": False, "session": None, "turns": [], "next_after": None}),
    "/dashboard/work/calls/abc/result": (400, {"error": "call id must be a UUID"}),
    "/dashboard/model-calls/abc/blocks/system/0": (400, {"error": "call id must be a UUID"}),
    "/dashboard/execution/abc": (500, None),
    # With the judge off (the default) it answers without a session: the stand-in database has none.
    "/dashboard/outside-judge": (200, {"enabled": False}),
    # F118 PR 3: the second judge's settings, with the setting off (the default): no session either.
    "/dashboard/outside-judge/second": (200, {"enabled": False}),
    "/context/log/abc/payload": (404, {"error": "Not enabled"}),
    "/context/log/abc/sections": (404, {"error": "Not enabled"}),
}


@pytest.mark.parametrize("path", list(GUARDED_READS))
@pytest.mark.parametrize(
    ("headers", "allowed"),
    [
        ({}, False),
        (OWNER, True),
        (PROXY, True),
        ({**PROXY, "content-type": "text/plain"}, True),
        ({"X-Nous-Owner-Secret": "a-wrong-value"}, False),
    ],
    ids=["none", "owner", "proxy-no-type", "proxy-text", "wrong"],
)
async def test_each_guarded_read_against_each_proof(
    tmp_path, path: str, headers: dict[str, str], allowed: bool
) -> None:
    response = await _send(_rest_app(tmp_path), "GET", path, headers=headers)
    if allowed:
        status, body = GUARDED_READS[path]
        assert response.status_code == status, response.text
        if body is not None:
            assert response.json() == body
    else:
        assert (response.status_code, response.json()) == (403, FORBIDDEN)


@pytest.mark.parametrize("path", list(GUARDED_READS))
@pytest.mark.parametrize("headers", [OWNER, PROXY], ids=["owner", "proxy"])
async def test_with_no_digest_each_guarded_read_is_403_even_with_a_header(
    tmp_path, path: str, headers: dict[str, str]
) -> None:
    response = await _send(_rest_app(tmp_path, owner=False, proxy=False), "GET", path, headers=headers)
    assert (response.status_code, response.json()) == (403, FORBIDDEN)


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
    assert guarded == (
        REST_ROUTE_SET | INTENTION_ROUTE_SET | GOAL_ROUTE_SET | AUTONOMY_ROUTE_SET | JUDGE_SECOND_ROUTE_SET
    )
    assert len(GOAL_ROUTE_SET) == 24
    assert len(AUTONOMY_ROUTE_SET) == 7
    assert len(JUDGE_SECOND_ROUTE_SET) == 5


@pytest.mark.parametrize("path", [*GOAL_READS, *AUTONOMY_READS])
@pytest.mark.parametrize(
    ("headers", "allowed"),
    [({}, False), (OWNER, True), (PROXY, True), ({**PROXY, "content-type": "text/plain"}, True)],
    ids=["none", "owner", "proxy", "proxy-text"],
)
async def test_each_goal_read_against_each_proof(tmp_path, path: str, headers: dict[str, str], allowed: bool) -> None:
    response = await _send(_rest_app(tmp_path), "GET", path, headers=headers)
    expected = (200, {**GOAL_READS, **AUTONOMY_READS}[path]) if allowed else (403, FORBIDDEN)
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


def test_the_device_routes_are_mounted_only_with_the_companion_on(tmp_path) -> None:
    from nous.api.rest import create_app
    from nous.config import Settings

    stand_in = SimpleNamespace()
    off = Settings(_env_file=None, workspace_dir=str(tmp_path), a2ui_enabled=False)
    app = create_app(stand_in, stand_in, SimpleNamespace(schedules=None), stand_in, stand_in, off)
    assert not [route.path for route in app.routes if getattr(route, "path", "").startswith("/a2ui/devices")]
    on = {route.path for route in _rest_app(tmp_path).routes if getattr(route, "path", "").startswith("/a2ui/devices")}
    assert on == {
        "/a2ui/devices/pairings",
        "/a2ui/devices/redeem",
        "/a2ui/devices/unpair",
        "/a2ui/devices/{id}",
        "/a2ui/devices",
    }


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


async def test_a_refusal_logs_the_route_template_never_the_callers_path_or_a_secret(
    tmp_path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    app = _rest_app(tmp_path)
    wrong = {"X-Nous-Owner-Secret": "a-wrong-value", "X-Nous-Proxy-Secret": "another-wrong-value"}
    refused = await _send(app, "DELETE", "/facts/feedfacecafe", headers=wrong)
    accepted = await _send(app, "DELETE", "/facts/feedfacecafe", headers=OWNER)
    assert (refused.status_code, accepted.status_code) == (403, 400)
    messages = [record.getMessage() for record in caplog.records if record.name.startswith("nous")]
    assert any("DELETE /facts/{fact_id}" in message for message in messages)
    assert not any("feedfacecafe" in message for message in messages)
    for record in caplog.records:
        text = record.getMessage() + repr(record.args)
        for value in (OWNER_VALUE, PROXY_VALUE, "a-wrong-value", "another-wrong-value", OWNER_DIGEST, PROXY_DIGEST):
            assert value not in text
