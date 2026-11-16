"""Every route of the app declares the check it applies, and each declared check is the one the route applies.

No database: the app is built by ``main.build_app`` with its components, the MCP server and the Telegram sender
replaced by stand-ins. Its lifespan runs only in the test that checks it adds no route; elsewhere it never runs, so no
handler reaches a component. A route group behind a setting
is in the app only when the setting is on: this fixture turns on ``mcp_enabled`` (the ``/mcp`` mount),
``mcp_client_enabled`` (the ``/integrations/mcp`` routes) and ``a2ui_enabled`` (the ``/a2ui`` routes, the companion
devices' among them). The ``/dashboard/v2`` mount and its redirects exist only when the dashboard build does, so the
test allows them and does not require them.
"""

from __future__ import annotations

import hashlib
import re

import httpx
import pytest
from starlette.applications import Starlette
from starlette.routing import Mount, Route

from nous import main as M
from nous.api.companion_assets import OverlayStaticFiles
from nous.api.companion_devices import NOT_VALID
from nous.config import Settings

OWNER_VALUE = "owner-only-test-value"
PROXY_VALUE = "proxy-only-test-value"
MCP_VALUE = "mcp-only-test-value"
OWNER_DIGEST = hashlib.sha256(OWNER_VALUE.encode()).hexdigest()
PROXY_DIGEST = hashlib.sha256(PROXY_VALUE.encode()).hexdigest()
MCP_DIGEST = hashlib.sha256(MCP_VALUE.encode()).hexdigest()

# The names of the checks.
OWNER = "owner"  # OwnerGuard.route: the owner's or the proxy's secret
ENTRY = "entry"  # EntryGate.route: the entry gate, under NOUS_OWNER_ENTRY_MODE
OWNER_SECRET = "owner secret"  # the route's own check of the owner's secret
PROXY_SECRET = "proxy secret"  # the route's own check of the proxy's secret
PER_VERB = (
    "per verb: a verb registered with owner_proof needs the owner's or the proxy's secret or a paired device's token"
)
PER_FUNCTION = "per function, against the surface's nonce"
INSTALLATION = "keyed by the installation id in the request"
PAIRING_CODE = "a pairing code, valid once, under the redeem limiter"  # nous/api/companion_devices.py
DEVICE_TOKEN = "the calling device's own token"  # nous/api/companion_devices.py
RULES = {OWNER, ENTRY, OWNER_SECRET, PROXY_SECRET, PER_VERB, PER_FUNCTION, INSTALLATION, PAIRING_CODE, DEVICE_TOKEN}
# The rules the behavioural test checks by sending a call that carries neither secret and expecting 403.
RULES_CHECKED_BY_A_403 = {OWNER, ENTRY, OWNER_SECRET, PROXY_SECRET, DEVICE_TOKEN}

# Every route not listed in READS, by (method, path), mapped to the name of the check it applies.
ROUTE_RULE: dict[tuple[str, str], str] = {
    # Chat
    ("POST", "/chat"): ENTRY,
    ("POST", "/chat/stream"): ENTRY,
    ("DELETE", "/chat/{session_id}"): ENTRY,
    # Decisions, memory, identity
    ("POST", "/decisions/{id}/review"): OWNER,
    ("POST", "/facts/{fact_id}/core"): OWNER,
    ("DELETE", "/profile-card/entries/{entry_id}"): OWNER,
    ("POST", "/profile-card/rebuild"): OWNER,
    ("PUT", "/facts/{fact_id}"): OWNER,
    ("DELETE", "/facts/{fact_id}"): OWNER,
    ("PUT", "/censors/{id}"): OWNER,
    ("PUT", "/identity/{section}"): OWNER,
    ("POST", "/reinitiate"): OWNER,
    # Subtasks and schedules
    ("DELETE", "/subtasks/{id}"): OWNER,
    ("POST", "/schedules"): OWNER,
    ("DELETE", "/schedules/{id}"): OWNER,
    ("POST", "/schedules/{id}/run"): OWNER,
    # Companion
    ("POST", "/a2ui/action"): PER_VERB,
    ("POST", "/a2ui/call"): PER_FUNCTION,
    ("GET", "/a2ui/push/installations"): OWNER,
    ("POST", "/a2ui/push/test"): INSTALLATION,
    ("DELETE", "/a2ui/push/tokens/{installation_id}"): INSTALLATION,
    ("PUT", "/a2ui/push/tokens"): INSTALLATION,
    ("POST", "/a2ui/devices/pairings"): OWNER,
    ("GET", "/a2ui/devices"): OWNER,
    ("DELETE", "/a2ui/devices/{id}"): OWNER,
    ("POST", "/a2ui/devices/redeem"): PAIRING_CODE,
    ("POST", "/a2ui/devices/unpair"): DEVICE_TOKEN,
    # Heartbeat, undo and sleep
    ("POST", "/heartbeat/trigger"): OWNER,
    ("PUT", "/heartbeat/config"): OWNER,
    ("POST", "/undo/snapshots/{id}/restore"): OWNER,
    ("POST", "/heartbeat/findings/{fingerprint}/acknowledge"): OWNER,
    ("POST", "/heartbeat/findings/{fingerprint}/resolve"): OWNER,
    ("POST", "/heartbeat/findings/{fingerprint}/dismiss"): OWNER,
    ("PUT", "/heartbeat/escalation-policy"): OWNER,
    ("POST", "/heartbeat/tune"): OWNER,
    ("POST", "/heartbeat/check/{name}/trigger"): OWNER,
    ("POST", "/heartbeat/check/{name}/reset"): OWNER,
    ("POST", "/heartbeat/checks/dynamic/{name}/trigger"): OWNER,
    ("PATCH", "/heartbeat/checks/dynamic/{name}"): OWNER,
    ("DELETE", "/heartbeat/checks/dynamic/{name}"): OWNER,
    ("POST", "/heartbeat/checks/dynamic"): OWNER,
    ("POST", "/sleep/trigger"): OWNER,
    # Admin and rubric
    ("POST", "/admin/search-weights"): OWNER,
    ("POST", "/rubric/propose-dimension"): OWNER,
    ("POST", "/rubric/proposals/{id}/approve"): OWNER,
    ("POST", "/rubric/rollback"): OWNER,
    ("POST", "/rubric/evolve"): OWNER,
    # Dashboard
    ("POST", "/dashboard/memory-write-health/acknowledge"): OWNER,
    # The stored prompt, response, message and tool-result reads
    ("GET", "/dashboard/work/sessions"): OWNER,
    ("GET", "/dashboard/work/sessions/{session_id}"): OWNER,
    ("GET", "/dashboard/work/calls/{call_id}/result"): OWNER,
    ("GET", "/dashboard/model-calls/{call_id}/blocks/{part}/{ord}"): OWNER,
    ("GET", "/dashboard/execution/{entry_id}"): OWNER,
    ("GET", "/context/log/{id}/payload"): OWNER,
    ("GET", "/context/log/{id}/sections"): OWNER,
    # Intentions and commitments
    ("POST", "/intentions/proposals/{id}/decide"): OWNER,
    ("POST", "/intentions/questions/answer"): OWNER,
    ("POST", "/intentions/questions/{id}/answer"): OWNER,
    ("POST", "/intentions/{root_id}/cancel"): OWNER,
    ("POST", "/commitments/{short_id}/{verb}"): OWNER_SECRET,
    # Goals (always mounted; with NOUS_GOALS_ENABLED off they answer that goals are off)
    ("GET", "/goals"): OWNER,
    ("GET", "/goals/options"): OWNER,
    ("GET", "/goals/{ref}"): OWNER,
    ("POST", "/goals"): OWNER,
    ("POST", "/goals/{ref}/approve"): OWNER,
    ("POST", "/goals/{ref}/pause"): OWNER,
    ("POST", "/goals/{ref}/resume"): OWNER,
    ("POST", "/goals/{ref}/confirm-done"): OWNER,
    ("POST", "/goals/{ref}/stop"): OWNER,
    ("POST", "/goals/{ref}/cancel"): OWNER,
    ("POST", "/goals/{ref}/run-now"): OWNER,
    ("POST", "/goals/{ref}/budget"): OWNER,
    ("POST", "/goals/{ref}/deadline"): OWNER,
    ("POST", "/goals/{ref}/note"): OWNER,
    # MCP client (mcp_client_enabled)
    ("GET", "/integrations/mcp/servers"): PROXY_SECRET,
    ("POST", "/integrations/mcp/servers"): PROXY_SECRET,
    ("POST", "/integrations/mcp/probe"): PROXY_SECRET,
    ("POST", "/integrations/mcp/servers/{name}/refresh"): PROXY_SECRET,
    ("PATCH", "/integrations/mcp/servers/{name}"): PROXY_SECRET,
    ("DELETE", "/integrations/mcp/servers/{name}"): PROXY_SECRET,
    ("POST", "/integrations/mcp/servers/{name}/sign-out"): PROXY_SECRET,
    ("POST", "/integrations/mcp/oauth/start"): PROXY_SECRET,
    ("GET", "/integrations/mcp/oauth/callback"): PROXY_SECRET,
    ("POST", "/integrations/mcp/oauth/complete"): PROXY_SECRET,
    ("GET", "/integrations/mcp/oauth/flows/{flow_id}"): PROXY_SECRET,
}

# GET routes that return data.
READS: set[str] = {
    "/status",
    "/decisions",
    "/decisions/unreviewed",
    "/decisions/{id}",
    "/episodes",
    "/profile/facts",
    "/profile-card",
    "/facts",
    "/chunks",
    "/censors",
    "/procedures",
    "/frames",
    "/calibration",
    "/identity",
    "/subtasks",
    "/subtasks/{id}",
    "/schedules",
    "/health",
    "/a2ui/stream",
    "/a2ui/surfaces/{surface_id}",
    "/a2ui/surfaces",
    "/a2ui/push/config",
    "/a2ui/catalog/{name}",
    "/events/stats",
    "/events/recent",
    "/events/trace/{trace_id}",
    "/events/recent-traces",
    "/events/modifications",
    "/heartbeat/status",
    "/undo/snapshots",
    "/heartbeat/findings",
    "/heartbeat/tuning-report",
    "/heartbeat/checks/dynamic",
    "/admin/search-weights",
    "/rubric/proposals",
    "/rubric/history",
    "/rubric/signals",
    "/rubric",
    "/dashboard/graph",
    "/dashboard/graph/node/{node_id}",
    "/dashboard/calibration",
    "/dashboard/activity",
    "/dashboard/health",
    "/dashboard/rubric",
    "/dashboard/admission/rejected",
    "/dashboard/admission",
    "/dashboard/ledger",
    "/dashboard/heartbeat",
    "/dashboard/observability",
    "/dashboard/retrieval",
    "/dashboard/retrieval/{entry_id}",
    "/dashboard/cache",
    "/dashboard/work/patterns",
    "/dashboard/work/today",
    "/dashboard/model-calls",
    "/dashboard/model-calls/{call_id}",
    "/dashboard/dag",
    "/dashboard/execution",
    "/dashboard/harness",
    "/dashboard/intentions",
    "/dashboard/attention",
    "/dashboard/autonomy",
    "/dashboard/memory-write-health",
    "/dashboard/density",
    "/dashboard/consolidation/{cycle_id}",
    "/dashboard/consolidation",
    "/dashboard/subtasks",
    "/context/log",
    "/context/log/{id}",
    "/context/diff",
    "/behavior/snapshot/latest",
    "/behavior/trends",
    "/behavior/anomalies",
    "/behavior/drift-report",
    "/intentions/proposals",
    "/intentions",
    "/integrations/mcp/oauth/client-metadata.json",
}
# Redirects registered only when the dashboard build exists (static/dashboard-v2/dist).
BUILD_ONLY_READS = {"/dashboard", "/dashboard/", "/", "/companion", "/companion/", "/companion/a/{surface_id}"}
READS |= BUILD_ONLY_READS

# Mounts, by path: the check each applies. ``/dashboard/v2`` serves the dashboard build's static files.
MOUNT_RULE = {"/mcp": ENTRY, "/dashboard/v2": "static files"}
BUILD_ONLY_MOUNTS = {"/dashboard/v2"}


@pytest.fixture
def stand_ins(monkeypatch: pytest.MonkeyPatch) -> None:
    """Components, the MCP server and the Telegram sender replaced, as in ``test_entry_gate_mcp``."""

    async def fake_create_components(settings):
        return {}

    async def fake_shutdown(components):
        return None

    def fake_create_mcp_server(**_kw):
        raise AssertionError("no request reaches the MCP server in these tests")

    monkeypatch.setattr(M, "create_components", fake_create_components)
    monkeypatch.setattr(M, "shutdown_components", fake_shutdown)
    monkeypatch.setattr(M, "start_event_loop_watchdog", lambda s: None)
    monkeypatch.setattr("nous.api.mcp.create_mcp_server", fake_create_mcp_server)
    monkeypatch.setattr(M, "_telegram_text_push", lambda settings, **kw: None)


def _app(tmp_path, *, digests: bool = True) -> Starlette:
    settings = Settings(
        _env_file=None,
        workspace_dir=str(tmp_path),
        owner_entry_mode="enforce",
        owner_route_secret_sha256=OWNER_DIGEST if digests else "",
        mcp_client_ui_secret_sha256=PROXY_DIGEST if digests else "",
        mcp_server_secret_sha256=MCP_DIGEST if digests else "",
        mcp_enabled=True,
        mcp_client_enabled=True,
        a2ui_enabled=True,
    )
    return M.build_app(settings)


def _rows(app: Starlette) -> list[tuple[str, str, Route]]:
    """Every (method, path, route) of the app's ``Route`` entries, HEAD left out (Starlette adds it to a GET)."""
    return [
        (method, route.path, route)
        for route in app.routes
        if type(route) is Route and route.methods is not None
        for method in sorted(route.methods - {"HEAD"})
    ]


def _mounts(app: Starlette) -> dict[str, Mount]:
    return {route.path: route for route in app.routes if type(route) is Mount}


def _concrete(path: str) -> str:
    """The path with each ``{param}`` filled with a value that names nothing."""
    return re.sub(r"\{[^}]+\}", "x", path)


async def _send(app: Starlette, method: str, path: str, **kwargs: object) -> httpx.Response:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://nous") as client:
        return await client.request(method, path, **kwargs)


def test_every_route_has_a_declared_rule(tmp_path, stand_ins) -> None:
    app = _app(tmp_path)
    for route in app.routes:
        assert type(route) in (Route, Mount), (
            f"{route!r}: a route type this test does not read; declare how it is checked"
        )
        if type(route) is Route:
            assert route.methods is not None, f"{route.path}: a route with no methods list; give it methods=[...]"
    mounts = _mounts(app)
    assert "/mcp" in mounts  # mcp_enabled is on here
    assert set(mounts) <= set(MOUNT_RULE), (
        f"declare the check each mount applies: {sorted(set(mounts) - set(MOUNT_RULE))}"
    )
    if "/dashboard/v2" in mounts:  # the dashboard build exists
        assert isinstance(mounts["/dashboard/v2"].app, OverlayStaticFiles), "/dashboard/v2 serves static files only"
    for method, path, _route in _rows(app):
        if method != "GET":
            assert (method, path) in ROUTE_RULE, f"declare the check {method} {path} applies in ROUTE_RULE"


def _table(app: Starlette) -> list[tuple[str, str, tuple[str, ...]]]:
    return [
        (type(route).__name__, getattr(route, "path", ""), tuple(sorted(getattr(route, "methods", None) or ())))
        for route in app.routes
    ]


async def test_the_lifespan_adds_no_route(tmp_path, stand_ins) -> None:
    """The other tests read the route table as built; a route the lifespan added would escape them."""
    app = _app(tmp_path)
    before = _table(app)
    async with app.router.lifespan_context(app):
        during = _table(app)
    assert during == before == _table(app)


def test_every_get_is_declared_a_read_or_has_a_rule(tmp_path, stand_ins) -> None:
    app = _app(tmp_path)
    for method, path, _route in _rows(app):
        if method == "GET":
            declared = [path in READS, ("GET", path) in ROUTE_RULE]
            assert declared.count(True) == 1, f"declare GET {path} in READS or in ROUTE_RULE (exactly one)"


def test_every_declared_rule_and_read_names_a_route(tmp_path, stand_ins) -> None:
    app = _app(tmp_path)
    rows = {(method, path) for method, path, _route in _rows(app)}
    assert set(ROUTE_RULE) - rows == set(), "ROUTE_RULE names a route the app does not have"
    assert {("GET", path) for path in READS - BUILD_ONLY_READS} - rows == set(), "READS names a GET the app lacks"
    assert set(MOUNT_RULE) - set(_mounts(app)) <= BUILD_ONLY_MOUNTS


def test_every_rule_is_one_this_file_names_and_tests() -> None:
    assert set(ROUTE_RULE.values()) <= RULES, "a new kind of check: name it in RULES and test it in this file"


async def test_the_redeem_route_refuses_a_call_without_a_valid_code(tmp_path, stand_ins) -> None:
    """The pairing code is the redeem route's check: with no pairing open, a well-formed body is refused as an invalid
    code, and a body that is not JSON before it is read. The device store is never reached (its database is a
    stand-in here, so reaching it would answer 500)."""
    app = _app(tmp_path)
    body = b'{"code": "ABCD-EFGH", "installation_id": "an-installation"}'
    no_code = await _send(
        app, "POST", "/a2ui/devices/redeem", content=body, headers={"content-type": "application/json"}
    )
    not_json = await _send(app, "POST", "/a2ui/devices/redeem", content=b"not json")
    with_owner = await _send(
        app,
        "POST",
        "/a2ui/devices/redeem",
        content=body,
        headers={"content-type": "application/json", "X-Nous-Owner-Secret": OWNER_VALUE},
    )
    assert (no_code.status_code, no_code.json()) == (404, NOT_VALID)
    assert not_json.status_code == 415
    assert (with_owner.status_code, with_owner.json()) == (404, NOT_VALID)  # a secret is not a code


async def test_the_unpair_route_takes_only_a_device_token(tmp_path, stand_ins) -> None:
    """The device's own token is the unpair route's check: neither the owner's nor the proxy's secret stands in."""
    app = _app(tmp_path)
    answers = [
        (await _send(app, "POST", "/a2ui/devices/unpair", content=b"{}", headers=headers)).status_code
        for headers in (
            {"content-type": "application/json"},
            {"content-type": "application/json", "X-Nous-Owner-Secret": OWNER_VALUE},
            {"content-type": "application/json", "X-Nous-Proxy-Secret": PROXY_VALUE},
        )
    ]
    assert answers == [403, 403, 403]


def test_each_declared_rule_is_the_marker_the_route_carries(tmp_path, stand_ins) -> None:
    app = _app(tmp_path)
    for method, path, route in _rows(app):
        rule = ROUTE_RULE.get((method, path))
        guarded = getattr(route.endpoint, "owner_guarded", False)
        gated = getattr(route.endpoint, "entry_gated", False)
        assert guarded is (rule == OWNER), f"{method} {path}: declared {rule!r}, owner_guarded is {guarded}"
        assert gated is (rule == ENTRY), f"{method} {path}: declared {rule!r}, entry_gated is {gated}"
    assert getattr(_mounts(app)["/mcp"].app, "entry_gated", False) is True


async def test_each_route_whose_rule_is_checked_by_a_403_answers_403_to_a_call_carrying_neither_secret(
    tmp_path, stand_ins
) -> None:
    """Behavioural: the call carries neither secret and a body that is not JSON. A marker set by hand cannot pass
    this; the per-verb, per-function and installation-id rules are tested where those checks live."""
    app = _app(tmp_path)
    statuses = {}
    for method, path, _route in _rows(app):
        if ROUTE_RULE.get((method, path)) in RULES_CHECKED_BY_A_403:
            response = await _send(app, method, _concrete(path), content=b"not json")
            statuses[(method, path)] = response.status_code
    mcp = await _send(app, "POST", "/mcp/", content=b"not json")
    assert {key: status for key, status in statuses.items() if status != 403} == {}
    assert mcp.status_code == 403
    assert len(statuses) == sum(1 for rule in ROUTE_RULE.values() if rule in RULES_CHECKED_BY_A_403)


async def test_with_neither_digest_set_every_owner_route_refuses_even_with_a_header(tmp_path, stand_ins) -> None:
    app = _app(tmp_path, digests=False)
    owner_rows = [(method, path) for method, path, _route in _rows(app) if ROUTE_RULE.get((method, path)) == OWNER]
    assert owner_rows
    answers = {}
    for method, path in owner_rows:
        for name, headers in (
            ("owner", {"X-Nous-Owner-Secret": OWNER_VALUE}),
            ("proxy", {"X-Nous-Proxy-Secret": PROXY_VALUE, "content-type": "application/json"}),
        ):
            response = await _send(app, method, _concrete(path), content=b"{}", headers=headers)
            answers[(method, path, name)] = response.status_code
    assert {key: status for key, status in answers.items() if status != 403} == {}
