"""F092: the /a2ui/* and /companion routes.

Runs on either backend. The stream and index routes are exercised against a
recording fake service rather than a database: what is under test here is the
route's own logic — resume-point precedence, the 503 contract, actor
extraction — and a real SurfaceService would only add setup between the
assertion and the thing it asserts. The service itself is covered against
real Postgres in test_a2ui_service.py.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from nous.api.rest import create_app
from nous.brain.brain import Brain
from nous.cognitive.layer import CognitiveLayer

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class MockAgentRunner:
    def __init__(self) -> None:
        self._conversations: dict = {}
        self._ledgers: dict = {}
        self._pending_corrections: dict = {}

    async def start(self) -> None:
        pass

    async def close(self) -> None:
        pass


class FakeSurfaceService:
    """Records what the route asked for; ends the stream immediately.

    ``replay`` returning None is the real "gap too large" signal, which makes
    stream_events emit one control:resync frame and return — so the SSE
    response completes without needing a timeout or a cancel.
    """

    def __init__(self, *, latest: int = 77) -> None:
        self.replay_calls: list[int] = []
        self.latest_seq_calls = 0
        self.subscribed = 0
        self.unsubscribed = 0
        self._latest = latest

    def subscribe(self) -> asyncio.Queue:
        self.subscribed += 1
        return asyncio.Queue(maxsize=8)

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self.unsubscribed += 1

    async def replay(self, since: int) -> None:
        self.replay_calls.append(since)
        return None

    async def latest_seq(self) -> int:
        self.latest_seq_calls += 1
        return self._latest

    async def live_index(self) -> dict:
        return {
            "latest_seq": self._latest,
            "surfaces": [
                {
                    "surface_id": "nous:escalation:approval_gate:abc123",
                    "kind": "approval_gate",
                    "origin": "escalation",
                    "title": "Cancel the backfill?",
                    "priority": 2,
                    "created_at": "2026-08-29T10:00:00+00:00",
                    "updated_at": "2026-08-29T10:00:00+00:00",
                }
            ],
        }

    async def snapshot(self, surface_id: str) -> tuple[dict, int] | None:
        if surface_id != "nous:escalation:approval_gate:abc123":
            return None
        envelope = {
            "version": "v1.0",
            "createSurface": {"surfaceId": surface_id, "catalogId": "x", "components": []},
        }
        return envelope, 7


class FakeActionRouter:
    """Records the arguments the route extracted from the request."""

    def __init__(self, *, status: int = 200) -> None:
        self.calls: list[dict[str, Any]] = []
        self._status = status

    async def handle(
        self, body: dict, *, content_type: str, actor: str = "unattributed"
    ) -> tuple[int, dict]:
        self.calls.append({"body": body, "content_type": content_type, "actor": actor})
        return self._status, {"ok": True, "message": "", "resolved": False}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def brain(db, settings):
    b = Brain(database=db, settings=settings)
    yield b
    await b.close()


@pytest_asyncio.fixture
async def cognitive(brain, heart, settings):
    return CognitiveLayer(brain, heart, settings, identity_prompt="You are Nous.")


@pytest.fixture
def fake_service() -> FakeSurfaceService:
    return FakeSurfaceService()


@pytest.fixture
def fake_router() -> FakeActionRouter:
    return FakeActionRouter()


@pytest.fixture
def app_without_services(brain, heart, cognitive, db, settings):
    """create_app with the A2UI kwargs omitted — the pre-lifespan/test shape."""
    return create_app(MockAgentRunner(), brain, heart, cognitive, db, settings)


@pytest.fixture
def app(brain, heart, cognitive, db, settings, fake_service, fake_router):
    return create_app(
        MockAgentRunner(),
        brain,
        heart,
        cognitive,
        db,
        settings,
        surface_service=fake_service,
        action_router=fake_router,
    )


@pytest_asyncio.fixture
async def client(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest_asyncio.fixture
async def client_without_services(app_without_services):
    async with AsyncClient(
        transport=ASGITransport(app=app_without_services), base_url="http://test"
    ) as c:
        yield c


# ---------------------------------------------------------------------------
# Unavailable-services contract
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/a2ui/stream"),
        ("GET", "/a2ui/surfaces"),
        ("GET", "/a2ui/surfaces/nous:x:y:1"),
        ("POST", "/a2ui/action"),
    ],
)
async def test_routes_return_503_without_services(
    client_without_services: AsyncClient, method: str, path: str
) -> None:
    """Registration always happens; behavior is defined when wiring is absent.

    Matches the heartbeat routes' pattern — a missing component must produce
    a stated 503, never a 404 that reads like the feature does not exist or a
    500 from dereferencing None.
    """
    response = await client_without_services.request(method, path, json={})

    assert response.status_code == 503
    assert response.json() == {"error": "A2UI not available"}


async def test_catalog_is_served_without_any_services(
    client_without_services: AsyncClient,
) -> None:
    """The catalog route is stateless, so it works before anything is wired.

    The renderer fetches it to resolve component schemas; gating it on the
    service would make an unwired server serve a blank companion instead of
    a working one with no surfaces.
    """
    response = await client_without_services.get("/a2ui/catalog/basic")

    assert response.status_code == 200
    body = response.json()
    assert "components" in body
    assert body["$id"].endswith("/basic/catalog.json")


async def test_nous_core_catalog_is_served(client_without_services: AsyncClient) -> None:
    response = await client_without_services.get("/a2ui/catalog/nous-core")

    assert response.status_code == 200
    assert "ApprovalPanel" in response.json()["components"]


async def test_unknown_catalog_is_404(client_without_services: AsyncClient) -> None:
    response = await client_without_services.get("/a2ui/catalog/bogus")

    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Stream resume point
# ---------------------------------------------------------------------------


async def test_last_event_id_header_wins_over_the_since_query(
    client: AsyncClient, fake_service: FakeSurfaceService
) -> None:
    """A browser's automatic EventSource reconnect reuses the ORIGINAL URL,

    so ?since= is frozen at whatever the first connect used while
    Last-Event-ID carries the truth. If the query won, every reconnect would
    replay from the stale point and re-deliver envelopes already applied.
    """
    response = await client.get(
        "/a2ui/stream", params={"since": "7"}, headers={"Last-Event-ID": "42"}
    )

    assert response.status_code == 200
    assert fake_service.replay_calls == [42]
    assert fake_service.latest_seq_calls == 0


async def test_since_query_is_used_when_no_header_is_sent(
    client: AsyncClient, fake_service: FakeSurfaceService
) -> None:
    response = await client.get("/a2ui/stream", params={"since": "7"})

    assert response.status_code == 200
    assert fake_service.replay_calls == [7]


async def test_stream_without_a_resume_point_starts_at_the_latest_seq(
    client: AsyncClient, fake_service: FakeSurfaceService
) -> None:
    """A cold connect must not replay history: the client hydrates from the

    live index and snapshots first, so replaying from 0 would re-apply
    everything it already has.
    """
    response = await client.get("/a2ui/stream")

    assert response.status_code == 200
    assert fake_service.latest_seq_calls == 1
    assert fake_service.replay_calls == [77]


async def test_stream_rejects_a_non_integer_resume_point(client: AsyncClient) -> None:
    response = await client.get("/a2ui/stream", params={"since": "abc"})

    assert response.status_code == 400
    assert "integer" in response.json()["error"]


async def test_stream_sets_streaming_headers_and_emits_the_resync_frame(
    client: AsyncClient, fake_service: FakeSurfaceService
) -> None:
    """Too large a gap ends in control:resync, and the generator always

    unsubscribes — a leaked subscriber queue would grow the hub forever.
    """
    response = await client.get("/a2ui/stream", params={"since": "1"})

    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["x-accel-buffering"] == "no"

    assert "event: control" in response.text
    payload = response.text.split("data: ", 1)[1].strip()
    assert json.loads(payload) == {"type": "resync"}

    assert fake_service.subscribed == 1
    assert fake_service.unsubscribed == 1


# ---------------------------------------------------------------------------
# Surfaces + action routes
# ---------------------------------------------------------------------------


async def test_surfaces_index_is_served(client: AsyncClient) -> None:
    response = await client.get("/a2ui/surfaces")

    assert response.status_code == 200
    body = response.json()
    assert body["latest_seq"] == 77
    assert body["surfaces"][0]["kind"] == "approval_gate"
    assert "nonce" not in body["surfaces"][0]


async def test_snapshot_is_served(client: AsyncClient) -> None:
    response = await client.get("/a2ui/surfaces/nous:escalation:approval_gate:abc123")

    assert response.status_code == 200
    assert response.json()["createSurface"]["surfaceId"].endswith("abc123")


async def test_snapshot_of_an_unknown_surface_is_404(client: AsyncClient) -> None:
    response = await client.get("/a2ui/surfaces/nous:nope:nope:000000")

    assert response.status_code == 404


async def test_action_route_forwards_body_and_content_type(
    client: AsyncClient, fake_router: FakeActionRouter
) -> None:
    """The router decides on Content-Type, so the route must pass it through

    rather than trusting httpx/Starlette to have validated anything.
    """
    payload = {"version": "v1.0", "action": {"name": "approval.defer", "surfaceId": "s1"}}

    response = await client.post("/a2ui/action", json=payload)

    assert response.status_code == 200
    assert fake_router.calls[0]["body"] == payload
    assert fake_router.calls[0]["content_type"].startswith("application/json")


async def test_action_route_reports_the_routers_status(
    brain, heart, cognitive, db, settings, fake_service
) -> None:
    rejecting = FakeActionRouter(status=403)
    app = create_app(
        MockAgentRunner(),
        brain,
        heart,
        cognitive,
        db,
        settings,
        surface_service=fake_service,
        action_router=rejecting,
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/a2ui/action", json={"action": {}})

    assert response.status_code == 403


async def test_malformed_json_body_is_a_400(client: AsyncClient, fake_router) -> None:
    response = await client.post(
        "/a2ui/action", content=b"{not json", headers={"content-type": "application/json"}
    )

    assert response.status_code == 400
    assert fake_router.calls == []


@pytest.mark.parametrize("header", ["x-forwarded-user", "x-forwarded-email"])
async def test_forwarded_identity_headers_are_IGNORED_by_default(
    client: AsyncClient, fake_router: FakeActionRouter, header: str
) -> None:
    """Default posture (codex P2): on a directly reachable port ANY caller

    can set forwarding headers, and a forged actor in the audit is worse
    than 'unattributed'. Headers count only behind a configured proxy.
    """
    await client.post("/a2ui/action", json={"action": {}}, headers={header: "mallory@evil"})

    assert fake_router.calls[0]["actor"] == "unattributed"


@pytest.mark.parametrize("header", ["x-forwarded-user", "x-forwarded-email"])
async def test_actor_is_taken_from_the_proxy_header_when_trusted(
    brain, heart, cognitive, db, settings, fake_service, fake_router, header: str
) -> None:
    """With NOUS_A2UI_TRUST_FORWARDED_IDENTITY=true (an authenticating proxy

    fronts the port and strips client-supplied headers), the identity header
    is the evidence of who acted and is recorded verbatim.
    """
    trusting = settings.model_copy(update={"a2ui_trust_forwarded_identity": True})
    app = create_app(
        MockAgentRunner(),
        brain,
        heart,
        cognitive,
        db,
        trusting,
        surface_service=fake_service,
        action_router=fake_router,
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as trusted_client:
        await trusted_client.post(
            "/a2ui/action", json={"action": {}}, headers={header: "tim@example.com"}
        )

    assert fake_router.calls[0]["actor"] == "tim@example.com"


async def test_actor_is_unattributed_without_a_proxy_header(
    client: AsyncClient, fake_router: FakeActionRouter
) -> None:
    """No header means the request did not come through the proxy — the audit

    row must say so rather than imply a human consented.
    """
    await client.post("/a2ui/action", json={"action": {}})

    assert fake_router.calls[0]["actor"] == "unattributed"


# ---------------------------------------------------------------------------
# /companion entry
# ---------------------------------------------------------------------------


@pytest.fixture
def dashboard_dist_present(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pretend the Svelte build exists.

    The /companion routes are registered only when static/dashboard-v2/dist
    is on disk, and that directory is gitignored — so in CI it is absent and
    these routes would silently not exist to test. Both the registration
    branch and StaticFiles(check_dir=True) consult os.path.isdir, so the
    patch must be in place before create_app runs.
    """
    real_isdir = os.path.isdir

    def _isdir(path: str) -> bool:
        if str(path).replace("\\", "/").endswith("static/dashboard-v2/dist"):
            return True
        return real_isdir(path)

    monkeypatch.setattr(os.path, "isdir", _isdir)


@pytest.mark.parametrize("path", ["/companion", "/companion/"])
async def test_companion_redirects_to_the_built_entry(
    dashboard_dist_present: None, brain, heart, cognitive, db, settings, path: str
) -> None:
    """Both slash variants are registered as exact-match Routes.

    The Location carries NO fragment, so a deep link /companion#/s/{id} keeps
    its fragment across the redirect and the companion router reads it on
    mount.
    """
    app = create_app(MockAgentRunner(), brain, heart, cognitive, db, settings)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(path)

    assert response.status_code == 307
    assert response.headers["location"] == "/dashboard/v2/companion.html"


async def test_companion_per_app_deep_link_redirects_into_the_hash_router(
    dashboard_dist_present: None, brain, heart, cognitive, db, settings
) -> None:
    """F092.1 §7: /companion/a/{surface_id} is a PATH-form deep link
    (shareable where fragments get stripped) that lands on the entry with
    the id moved into the hash; #/a/ and #/s/ are client-side aliases."""
    app = create_app(MockAgentRunner(), brain, heart, cognitive, db, settings)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/companion/a/nous:chat:micro_app:abc123")

    assert response.status_code == 307
    assert (
        response.headers["location"]
        == "/dashboard/v2/companion.html#/a/nous%3Achat%3Amicro_app%3Aabc123"
    )


async def test_companion_routes_absent_without_a_build(
    brain, heart, cognitive, db, settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without the build, /companion must not redirect into a 404.

    Same rationale as the /dashboard pair: a redirect to a page that cannot
    be served is worse than an honest 404 at the entry point.
    """
    real_isdir = os.path.isdir
    monkeypatch.setattr(
        os.path,
        "isdir",
        lambda p: False
        if str(p).replace("\\", "/").endswith("static/dashboard-v2/dist")
        else real_isdir(p),
    )
    app = create_app(MockAgentRunner(), brain, heart, cognitive, db, settings)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/companion")

    assert response.status_code == 404
