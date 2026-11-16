"""F114 goal routes: every route requires the owner's or the proxy's secret, checked before the flag, the body and the
goal; with the flag off the reads give a fixed answer and the changes a fixed refusal, and no table is read. No
database: every call is answered before one would be needed."""

from __future__ import annotations

from typing import Any

import pytest
from goal_routes_support import FORBIDDEN, NOT_JSON, OWNER, PROXY, make_env, send

from nous.api.goal_routes import GOALS_OFF

POSTS = [
    "/goals",
    "/goals/G-0001/approve",
    "/goals/G-0001/pause",
    "/goals/G-0001/resume",
    "/goals/G-0001/confirm-done",
    "/goals/G-0001/stop",
    "/goals/G-0001/cancel",
    "/goals/G-0001/run-now",
    "/goals/G-0001/budget",
    "/goals/G-0001/deadline",
    "/goals/G-0001/note",
]
READS = {
    "/goals": {"enabled": False, "goals": []},
    "/goals/options": {"enabled": False},
    "/goals/G-0001": {"enabled": False},
}
GOAL_ROUTE_SET = {
    ("GET", "/goals"),
    ("POST", "/goals"),
    ("GET", "/goals/options"),
    ("GET", "/goals/{ref}"),
    *{
        ("POST", f"/goals/{{ref}}/{name}")
        for name in (
            "approve",
            "pause",
            "resume",
            "confirm-done",
            "stop",
            "cancel",
            "run-now",
            "budget",
            "deadline",
            "note",
        )
    },
}


class NoDatabase:
    """A database that counts the sessions asked of it, and opens none."""

    def __init__(self) -> None:
        self.opened = 0

    def session(self) -> Any:
        self.opened += 1
        raise AssertionError("a goal route opened a database session")


@pytest.mark.parametrize("path", POSTS)
async def test_a_goal_post_without_a_secret_is_403_and_the_handler_never_runs(path: str) -> None:
    database = NoDatabase()
    env = make_env(database, enabled=False)
    response = await send(env.app, "POST", path, content=b"not json")
    assert (response.status_code, response.json()) == (403, FORBIDDEN)
    assert database.opened == 0


@pytest.mark.parametrize("path", list(READS))
async def test_a_goal_read_without_a_secret_is_403(path: str) -> None:
    env = make_env(NoDatabase(), enabled=False)
    response = await send(env.app, "GET", path)
    assert (response.status_code, response.json()) == (403, FORBIDDEN)


@pytest.mark.parametrize("path", POSTS)
@pytest.mark.parametrize(
    ("headers", "status", "body"),
    [
        ({}, 403, FORBIDDEN),
        (OWNER, 503, GOALS_OFF),
        ({**PROXY, "content-type": "application/json"}, 503, GOALS_OFF),
        ({**PROXY, "content-type": "application/json; charset=utf-8"}, 503, GOALS_OFF),
        ({**PROXY, "content-type": "text/plain"}, 415, NOT_JSON),
        ({**PROXY, "content-type": "text/plain; application/json"}, 415, NOT_JSON),
        ({**OWNER, "content-type": "text/plain"}, 503, GOALS_OFF),
    ],
    ids=["none", "owner", "proxy-json", "proxy-json-charset", "proxy-text", "proxy-smuggled-json", "owner-text"],
)
async def test_each_goal_post_against_each_proof(path: str, headers: dict[str, str], status: int, body: dict) -> None:
    """With the flag off the 503 is the proof the guard let the call through to the handler."""
    env = make_env(NoDatabase(), enabled=False)
    response = await send(env.app, "POST", path, content=b"{}", headers=headers)
    assert (response.status_code, response.json()) == (status, body)


@pytest.mark.parametrize("path", list(READS))
@pytest.mark.parametrize(
    ("headers", "allowed"),
    [({}, False), (OWNER, True), (PROXY, True), ({**PROXY, "content-type": "text/plain"}, True)],
    ids=["none", "owner", "proxy", "proxy-text"],
)
async def test_each_goal_read_against_each_proof(path: str, headers: dict[str, str], allowed: bool) -> None:
    """A read carries no body: the proxy's secret alone lets it through, whatever its content type."""
    env = make_env(NoDatabase(), enabled=False)
    response = await send(env.app, "GET", path, headers=headers)
    expected = (200, READS[path]) if allowed else (403, FORBIDDEN)
    assert (response.status_code, response.json()) == expected


async def test_with_goals_off_reads_answer_enabled_false_and_writes_503_and_nothing_is_read() -> None:
    database = NoDatabase()
    env = make_env(database, enabled=False)
    for path, answer in READS.items():
        response = await env.call("GET", path)
        assert (response.status_code, response.json()) == (200, answer), path
    for path in POSTS:
        response = await env.call("POST", path, json={"text": "x", "days": 1, "total": 5})
        assert (response.status_code, response.json()) == (503, GOALS_OFF), path
    assert database.opened == 0


async def test_the_guard_comes_before_the_flag_check() -> None:
    env = make_env(NoDatabase(), enabled=False)
    for path in POSTS:
        assert (await send(env.app, "POST", path, json={})).status_code == 403, path
    for path in READS:
        assert (await send(env.app, "GET", path)).status_code == 403, path


@pytest.mark.parametrize("headers", [OWNER, PROXY], ids=["owner", "proxy"])
async def test_with_no_digest_a_goal_route_is_403_even_with_a_header(headers: dict[str, str]) -> None:
    env = make_env(NoDatabase(), enabled=False, owner_route_secret_sha256="", mcp_client_ui_secret_sha256="")
    for method, path in (("GET", "/goals"), ("POST", "/goals/G-0001/pause")):
        response = await send(env.app, method, path, json={}, headers=headers)
        assert (response.status_code, response.json()) == (403, FORBIDDEN)


def test_the_set_of_guarded_goal_routes_is_exact() -> None:
    env = make_env(NoDatabase(), enabled=False)
    pairs = set()
    for route in env.app.routes:
        assert getattr(route.endpoint, "owner_guarded", False) is True, route.path
        pairs |= {(method, route.path) for method in route.methods - {"HEAD"}}
    assert pairs == GOAL_ROUTE_SET


def test_options_comes_before_the_goal_ref_route() -> None:
    paths = [route.path for route in make_env(NoDatabase(), enabled=False).app.routes]
    assert paths.index("/goals/options") < paths.index("/goals/{ref}")
