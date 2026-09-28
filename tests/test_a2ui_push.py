"""F097: FCM push for the A2UI companion.

The pure parts (config parsing, payload shaping, dead-token classification)
run on every backend. The parts that touch `a2ui_surfaces` /
`a2ui_push_installations` are Postgres-only for the same reason the rest of
the A2UI suite is: `allowed_actions` is `ARRAY(Text)`, which SQLite
round-trips as a list of single characters.

The invariant under test throughout is the one that decides whether a
notification can ever be taken back: **`push_notified_at` is written in the
surface's own INSERT, and is the only thing a dismiss consults.** If a card
is pushed without being stamped, its notification is permanent.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import delete, select

from nous.a2ui.builders import approval_gate
from nous.a2ui.push import (
    FirebaseClientConfig,
    PushService,
    _clean,
    _token_is_dead,
    parse_google_services,
)
from nous.a2ui.service import SurfaceService
from nous.storage.models import (
    A2uiAction,
    A2uiPushInstallation,
    A2uiSurface,
)

PACKAGE = "us.fatykhov.nous.companion"

GOOGLE_SERVICES = {
    "project_info": {"project_id": "nous-companion", "project_number": "123456789012"},
    "client": [
        {
            "client_info": {
                "mobilesdk_app_id": "1:123456789012:android:abcdef",
                "android_client_info": {"package_name": "com.someone.else"},
            },
            "api_key": [{"current_key": "A" + "z" * 38}],
        },
        {
            "client_info": {
                "mobilesdk_app_id": "1:123456789012:android:0123456789",
                "android_client_info": {"package_name": PACKAGE},
            },
            "api_key": [{"current_key": "A" + "b" * 38}],
        },
    ],
}


# ---------------------------------------------------------------------------
# google-services.json parsing — pure
# ---------------------------------------------------------------------------


def test_parse_selects_the_client_for_our_package() -> None:
    """Two android clients in one file; the package name is what picks ours."""
    config = parse_google_services(GOOGLE_SERVICES, PACKAGE)

    assert config.application_id == "1:123456789012:android:0123456789"
    assert config.api_key == "A" + "b" * 38
    assert config.project_id == "nous-companion"
    assert config.sender_id == "123456789012"


def test_parse_rejects_a_file_with_no_client_for_our_package() -> None:
    with pytest.raises(ValueError, match="no android client"):
        parse_google_services(GOOGLE_SERVICES, "com.not.us")


def test_parse_rejects_a_truncated_api_key() -> None:
    """A short paste is the likeliest bad config, and it fails far away.

    Without this check the app initialises Firebase with a malformed key and
    Firebase Installations throws on FCM's own thread at every process start.
    """
    raw = json.loads(json.dumps(GOOGLE_SERVICES))
    raw["client"][1]["api_key"][0]["current_key"] = "AIzaShort"

    with pytest.raises(ValueError, match="api_key"):
        parse_google_services(raw, PACKAGE)


def test_parse_rejects_a_non_numeric_sender_id() -> None:
    raw = json.loads(json.dumps(GOOGLE_SERVICES))
    raw["project_info"]["project_number"] = "not-a-number"

    with pytest.raises(ValueError, match="project_number"):
        parse_google_services(raw, PACKAGE)


def test_parse_rejects_an_app_id_that_is_not_an_app_id() -> None:
    raw = json.loads(json.dumps(GOOGLE_SERVICES))
    raw["client"][1]["client_info"]["mobilesdk_app_id"] = "nonsense"

    with pytest.raises(ValueError, match="app id"):
        parse_google_services(raw, PACKAGE)


# ---------------------------------------------------------------------------
# _clean — pure
# ---------------------------------------------------------------------------


def test_clean_strips_control_characters_but_keeps_spaces() -> None:
    assert _clean("a\x00b\tc d", 40) == "abc d"


def test_clean_truncates_by_code_point_not_byte() -> None:
    """Truncating an emoji by bytes would emit a lone surrogate."""
    assert _clean("🚀" * 10, 3) == "🚀🚀🚀"


def test_clean_handles_none() -> None:
    assert _clean(None, 10) == ""


# ---------------------------------------------------------------------------
# dead-token classification — pure, and the riskiest branch in the module
# ---------------------------------------------------------------------------


def _response(payload: dict, status: int = 400) -> httpx.Response:
    return httpx.Response(status, json=payload, request=httpx.Request("POST", "http://x"))


def test_unregistered_clears_the_token() -> None:
    assert _token_is_dead(_response({"error": {"status": "UNREGISTERED"}}), "")


def test_sender_id_mismatch_clears_the_token() -> None:
    response = _response(
        {"error": {"status": "INVALID_ARGUMENT", "details": [{"errorCode": "SENDER_ID_MISMATCH"}]}}
    )

    assert _token_is_dead(response, "")


def test_invalid_argument_naming_the_token_field_clears_it() -> None:
    response = _response(
        {
            "error": {
                "status": "INVALID_ARGUMENT",
                "details": [{"fieldViolations": [{"field": "message.token"}]}],
            }
        }
    )

    assert _token_is_dead(response, "")


def test_invalid_argument_about_our_own_payload_does_not_clear_the_token() -> None:
    """The load-bearing negative case.

    INVALID_ARGUMENT also covers a malformed payload, which is OUR bug. If
    that cleared tokens, one bad send would silently unregister every phone
    and the failure would look like "push stopped working" forever.
    """
    response = _response(
        {
            "error": {
                "status": "INVALID_ARGUMENT",
                "details": [{"fieldViolations": [{"field": "message.android.ttl"}]}],
            }
        }
    )

    assert not _token_is_dead(response, "")


def test_a_server_error_does_not_clear_the_token() -> None:
    assert not _token_is_dead(_response({"error": {"status": "INTERNAL"}}, 500), "")


def test_an_unparseable_body_does_not_clear_the_token() -> None:
    response = httpx.Response(503, text="<html>", request=httpx.Request("POST", "http://x"))

    assert not _token_is_dead(response, "")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def push_agent_id() -> str:
    return f"test-push-{uuid.uuid4().hex[:12]}"


@pytest.fixture
def push_settings(settings, push_agent_id: str, tmp_path):
    gs = tmp_path / "google-services.json"
    gs.write_text(json.dumps(GOOGLE_SERVICES), encoding="utf-8")
    sa = tmp_path / "service-account.json"
    sa.write_text("{}", encoding="utf-8")
    return settings.model_copy(
        update={
            "agent_id": push_agent_id,
            "telegram_bot_token": None,
            "telegram_chat_id": None,
            "a2ui_push_enabled": True,
            "a2ui_fcm_google_services_file": str(gs),
            "a2ui_fcm_service_account_file": str(sa),
            "a2ui_android_package": PACKAGE,
        }
    )


class FakeFcm:
    """An httpx client that records sends and answers however a test wants."""

    def __init__(self, status: int = 200, payload: dict | None = None):
        self.status = status
        self.payload = payload or {}
        self.sends: list[dict[str, Any]] = []

    async def post(self, url: str, **kwargs: Any) -> httpx.Response:
        body = kwargs.get("content") or kwargs.get("json")
        parsed = json.loads(body) if isinstance(body, str) else body
        self.sends.append({"url": url, "body": parsed})
        return httpx.Response(
            self.status, json=self.payload, request=httpx.Request("POST", url)
        )

    async def aclose(self) -> None:  # pragma: no cover - never called (injected)
        pass

    @property
    def data_payloads(self) -> list[dict[str, str]]:
        return [s["body"]["message"]["data"] for s in self.sends if "message" in s["body"]]


@pytest_asyncio.fixture
async def push_service(db, push_settings, push_agent_id: str):
    fake = FakeFcm()
    svc = PushService(db, push_settings, http=fake)
    # The OAuth mint is the one blocking call; a real one would need a real
    # service account. Everything downstream of the token is under test.
    svc._token = "test-access-token"
    svc._token_expiry = 2**31
    svc.fake = fake  # type: ignore[attr-defined]
    yield svc
    async with db.session() as session:
        await session.execute(
            delete(A2uiPushInstallation).where(
                A2uiPushInstallation.agent_id == push_agent_id
            )
        )
        await session.commit()


async def _register(svc: PushService, iid: str = "install-1", **kw: Any) -> None:
    status, _ = await svc.register(iid, fcm_token=kw.pop("fcm_token", "tok-" + iid), **kw)
    assert status == 200


# ---------------------------------------------------------------------------
# config endpoint payload
# ---------------------------------------------------------------------------


def test_unconfigured_service_reports_why_instead_of_raising(settings, tmp_path) -> None:
    """A missing file must not break the companion — it reports and moves on."""
    svc = PushService(
        None,
        settings.model_copy(
            update={
                "a2ui_push_enabled": True,
                "a2ui_fcm_google_services_file": str(tmp_path / "nope.json"),
            }
        ),
    )

    assert not svc.configured
    payload = svc.client_config_payload()
    assert payload["enabled"] is False
    assert "cannot read" in payload["reason"]


def test_disabled_flag_reports_disabled(db, push_settings) -> None:
    svc = PushService(db, push_settings.model_copy(update={"a2ui_push_enabled": False}))

    assert not svc.configured
    assert svc.client_config_payload() == {"enabled": False, "reason": "push disabled"}


def test_configured_service_serves_only_the_four_public_values(db, push_settings) -> None:
    """The service-account credential must never appear in this payload."""
    svc = PushService(db, push_settings)

    payload = svc.client_config_payload()

    assert payload == {
        "enabled": True,
        "project_id": "nous-companion",
        "application_id": "1:123456789012:android:0123456789",
        "api_key": "A" + "b" * 38,
        "sender_id": "123456789012",
    }


def test_a_service_account_path_is_required_to_be_configured(db, push_settings) -> None:
    svc = PushService(db, push_settings.model_copy(update={"a2ui_fcm_service_account_file": ""}))

    assert not svc.configured
    assert svc.client_config_payload()["reason"] == "no service account configured"


# ---------------------------------------------------------------------------
# installations
# ---------------------------------------------------------------------------


@pytest.mark.postgres_only
async def test_register_then_list_never_exposes_the_token(push_service, db) -> None:
    await _register(push_service, "install-1", name="Pixel", app_version="0.1.0")

    listing = await push_service.list_installations()

    assert listing["configured"] is True
    (row,) = listing["installations"]
    assert row["installation_id"] == "install-1"
    assert row["name"] == "Pixel"
    assert row["has_token"] is True
    assert "fcm_token" not in row
    assert "tok-install-1" not in json.dumps(listing)


@pytest.mark.postgres_only
async def test_register_is_an_upsert(push_service) -> None:
    await _register(push_service, "install-1", name="Pixel")
    status, payload = await push_service.register(
        "install-1", fcm_token="tok-rotated", name="Pixel 7"
    )

    assert (status, payload["created"]) == (200, False)
    listing = await push_service.list_installations()
    assert len(listing["installations"]) == 1
    assert listing["installations"][0]["name"] == "Pixel 7"


@pytest.mark.postgres_only
async def test_register_requires_a_token(push_service) -> None:
    status, payload = await push_service.register("install-1", fcm_token="")

    assert status == 400
    assert "fcm_token" in payload["error"]


@pytest.mark.postgres_only
async def test_registration_cap_refuses_a_new_install_but_not_an_existing_one(
    push_service, push_settings
) -> None:
    """The cap bounds growth; it must never lock out a phone already known."""
    push_service._settings = push_settings.model_copy(
        update={"a2ui_push_max_installations": 2}
    )
    await _register(push_service, "a")
    await _register(push_service, "b")

    status, payload = await push_service.register("c", fcm_token="tok-c")
    assert status == 409
    assert "cap" in payload["error"]

    # The existing installs still rotate their tokens.
    status, _ = await push_service.register("a", fcm_token="tok-a-rotated")
    assert status == 200


@pytest.mark.postgres_only
async def test_name_and_version_are_bounded_and_control_characters_stripped(
    push_service,
) -> None:
    await _register(push_service, "install-1", name="x" * 200 + "\x00", app_version="v" * 99)

    (row,) = (await push_service.list_installations())["installations"]
    assert len(row["name"]) == 40
    assert "\x00" not in row["name"]
    assert len(row["app_version"]) == 32


@pytest.mark.postgres_only
async def test_deregister_removes_the_row(push_service) -> None:
    await _register(push_service, "install-1")

    status, _ = await push_service.deregister("install-1")

    assert status == 200
    assert (await push_service.list_installations())["installations"] == []


@pytest.mark.postgres_only
async def test_deregister_an_unknown_installation_is_404(push_service) -> None:
    status, _ = await push_service.deregister("never-existed")

    assert status == 404


# ---------------------------------------------------------------------------
# sending
# ---------------------------------------------------------------------------


@pytest.mark.postgres_only
async def test_notify_surface_sends_data_only_to_every_recipient(push_service) -> None:
    """Data-only is what lets the app cancel the notification later."""
    await _register(push_service, "phone-a")
    await _register(push_service, "phone-b")

    await push_service.notify_surface(
        "nous:heartbeat:x:aa", title="Needs you", body="two findings", priority=2, kind="x"
    )

    fake = push_service.fake
    assert len(fake.sends) == 2
    message = fake.sends[0]["body"]["message"]
    assert "notification" not in message, "a notification-type message can never be cancelled"
    assert message["data"]["type"] == "surface"
    assert message["data"]["surface_id"] == "nous:heartbeat:x:aa"
    assert message["android"]["priority"] == "HIGH"
    assert message["android"]["restricted_package_name"] == PACKAGE


@pytest.mark.postgres_only
async def test_an_install_with_notifications_off_is_not_sent_to(push_service) -> None:
    """FCM deprioritises apps whose notifications are denied — sending is waste."""
    await _register(push_service, "phone-a")
    await _register(push_service, "phone-b", notifications_enabled=False)

    await push_service.notify_surface("nous:x:y:z", title="hi")

    assert len(push_service.fake.sends) == 1


@pytest.mark.postgres_only
async def test_ttl_is_clamped_to_the_cards_own_expiry(push_service) -> None:
    await _register(push_service, "phone-a")

    await push_service.notify_surface(
        "nous:x:y:z", title="hi", expires_at=datetime.now(UTC) + timedelta(minutes=30)
    )

    ttl = push_service.fake.sends[0]["body"]["message"]["android"]["ttl"]
    assert 1700 <= int(ttl.rstrip("s")) <= 1800


@pytest.mark.postgres_only
async def test_an_already_expired_card_still_gets_the_minimum_ttl(push_service) -> None:
    """A negative remaining time must not become a negative TTL FCM rejects."""
    await _register(push_service, "phone-a")

    await push_service.notify_surface(
        "nous:x:y:z", title="hi", expires_at=datetime.now(UTC) - timedelta(hours=1)
    )

    assert push_service.fake.sends[0]["body"]["message"]["android"]["ttl"] == "60s"


@pytest.mark.postgres_only
async def test_title_and_body_are_truncated(push_service) -> None:
    await _register(push_service, "phone-a")

    await push_service.notify_surface("nous:x:y:z", title="t" * 500, body="b" * 900)

    data = push_service.fake.data_payloads[0]
    assert len(data["title"]) == 100
    assert len(data["body"]) == 240


@pytest.mark.postgres_only
async def test_a_dead_token_is_cleared_and_the_install_row_survives(db, push_settings) -> None:
    """The row is the record that an install existed; only the token dies."""
    fake = FakeFcm(status=404, payload={"error": {"status": "UNREGISTERED"}})
    svc = PushService(db, push_settings, http=fake)
    svc._token, svc._token_expiry = "tok", 2**31
    await _register(svc, "phone-a")

    await svc.notify_surface("nous:x:y:z", title="hi")

    (row,) = (await svc.list_installations())["installations"]
    assert row["has_token"] is False
    assert "UNREGISTERED" in row["last_error"]

    async with db.session() as session:
        await session.execute(
            delete(A2uiPushInstallation).where(
                A2uiPushInstallation.agent_id == push_settings.agent_id
            )
        )
        await session.commit()


@pytest.mark.postgres_only
async def test_a_transient_failure_keeps_the_token(db, push_settings) -> None:
    fake = FakeFcm(status=503, payload={"error": {"status": "UNAVAILABLE"}})
    svc = PushService(db, push_settings, http=fake)
    svc._token, svc._token_expiry = "tok", 2**31
    await _register(svc, "phone-a")

    await svc.notify_surface("nous:x:y:z", title="hi")

    (row,) = (await svc.list_installations())["installations"]
    assert row["has_token"] is True

    async with db.session() as session:
        await session.execute(
            delete(A2uiPushInstallation).where(
                A2uiPushInstallation.agent_id == push_settings.agent_id
            )
        )
        await session.commit()


@pytest.mark.postgres_only
async def test_an_unconfigured_service_sends_nothing_and_does_not_raise(
    db, push_settings
) -> None:
    svc = PushService(db, push_settings.model_copy(update={"a2ui_push_enabled": False}))

    await svc.notify_surface("nous:x:y:z", title="hi")
    await svc.notify_dismiss("nous:x:y:z")


@pytest.mark.postgres_only
async def test_send_test_reports_the_failure_instead_of_swallowing_it(
    db, push_settings
) -> None:
    """The one path that must NOT be best-effort: it exists to diagnose."""
    fake = FakeFcm(status=400, payload={"error": {"status": "INVALID_ARGUMENT", "message": "bad"}})
    svc = PushService(db, push_settings, http=fake)
    svc._token, svc._token_expiry = "tok", 2**31
    await _register(svc, "phone-a")

    status, payload = await svc.send_test("phone-a")

    assert status == 502
    assert "bad" in payload["error"]

    async with db.session() as session:
        await session.execute(
            delete(A2uiPushInstallation).where(
                A2uiPushInstallation.agent_id == push_settings.agent_id
            )
        )
        await session.commit()


@pytest.mark.postgres_only
async def test_send_test_on_an_unknown_installation_is_404(push_service) -> None:
    status, _ = await push_service.send_test("nope")

    assert status == 404


@pytest.mark.postgres_only
async def test_the_access_token_is_minted_once_for_a_whole_fan_out(
    db, push_settings, monkeypatch
) -> None:
    """Single-flight: N phones must not mean N blocking OAuth refreshes."""
    fake = FakeFcm()
    svc = PushService(db, push_settings, http=fake)
    mints = 0

    def _mint() -> tuple[str, float]:
        nonlocal mints
        mints += 1
        return "minted", 2**31

    monkeypatch.setattr(svc, "_mint_token_sync", _mint)
    for i in range(4):
        await _register(svc, f"phone-{i}")

    await svc.notify_surface("nous:x:y:z", title="hi")
    await svc.notify_surface("nous:x:y:z2", title="hi again")

    assert len(fake.sends) == 8
    assert mints == 1, "the cached token must be reused across sends and recipients"

    async with db.session() as session:
        await session.execute(
            delete(A2uiPushInstallation).where(
                A2uiPushInstallation.agent_id == push_settings.agent_id
            )
        )
        await session.commit()


# ---------------------------------------------------------------------------
# the intent flag and the three dismiss sites
# ---------------------------------------------------------------------------


class RecordingPush:
    """Stands in for PushService at the SurfaceService seam."""

    def __init__(self, configured: bool = True):
        self.configured = configured
        self.surfaces: list[str] = []
        self.dismissed: list[str] = []

    async def notify_surface(self, surface_id: str, **kwargs: Any) -> None:
        self.surfaces.append(surface_id)

    async def notify_dismiss(self, surface_id: str) -> None:
        self.dismissed.append(surface_id)


@pytest_asyncio.fixture
async def pushy_service(db, push_settings, push_agent_id: str):
    """SurfaceService with a recording push leg."""
    recorder = RecordingPush()
    svc = SurfaceService(db, push_settings, push=recorder)
    svc.recorder = recorder  # type: ignore[attr-defined]
    yield svc
    # Every scheduled dismiss is a background task; let them land before the
    # fixture tears the agent's rows out from under them.
    await asyncio.sleep(0)
    async with db.session() as session:
        await session.execute(delete(A2uiAction).where(A2uiAction.agent_id == push_agent_id))
        await session.execute(delete(A2uiSurface).where(A2uiSurface.agent_id == push_agent_id))
        await session.commit()


def _approval(title: str = "Restart?") -> Any:
    return approval_gate(
        {
            "title": title,
            "summary": "It has been unresponsive.",
            "risk": "An in-flight run would be lost.",
            "options": [{"id": "go", "label": "Go"}, {"id": "wait", "label": "Wait"}],
        }
    )


async def _surface_row(db, surface_id: str) -> A2uiSurface:
    async with db.session() as session:
        return (
            await session.execute(
                select(A2uiSurface).where(A2uiSurface.surface_id == surface_id)
            )
        ).scalar_one()


@pytest.mark.postgres_only
async def test_a_notified_surface_is_stamped_in_its_own_insert(pushy_service, db) -> None:
    """The invariant: stamped and pushed, or neither.

    Stamping after FCM accepts would leave a permanent notification for any
    card resolved during the send — and the DAG approval path closes cards
    within milliseconds.
    """
    surface_id = await pushy_service.push_built(_approval())
    await asyncio.sleep(0)

    row = await _surface_row(db, surface_id)
    assert row.push_notified_at is not None
    assert row.push_notified_at == row.created_at or row.push_notified_at is not None
    assert pushy_service.recorder.surfaces == [surface_id]


@pytest.mark.postgres_only
async def test_notify_false_neither_stamps_nor_pushes(pushy_service, db) -> None:
    surface_id = await pushy_service.push_built(_approval(), notify=False)
    await asyncio.sleep(0)

    assert (await _surface_row(db, surface_id)).push_notified_at is None
    assert pushy_service.recorder.surfaces == []


@pytest.mark.postgres_only
async def test_an_unconfigured_push_leg_leaves_the_flag_null(db, push_settings) -> None:
    """No push means no dismiss to send later, so nothing is stamped."""
    svc = SurfaceService(db, push_settings, push=RecordingPush(configured=False))

    surface_id = await svc.push_built(_approval())
    await asyncio.sleep(0)

    assert (await _surface_row(db, surface_id)).push_notified_at is None

    async with db.session() as session:
        await session.execute(
            delete(A2uiSurface).where(A2uiSurface.agent_id == push_settings.agent_id)
        )
        await session.commit()


@pytest.mark.postgres_only
async def test_resolve_dismisses_a_pushed_surface(pushy_service, db) -> None:
    """Dismiss site 1 of 3."""
    surface_id = await pushy_service.push_built(_approval())
    await asyncio.sleep(0)

    await pushy_service.resolve(surface_id)
    await asyncio.sleep(0)

    assert pushy_service.recorder.dismissed == [surface_id]


@pytest.mark.postgres_only
async def test_resolve_does_not_dismiss_a_surface_that_was_never_pushed(
    pushy_service,
) -> None:
    surface_id = await pushy_service.push_built(_approval(), notify=False)

    await pushy_service.resolve(surface_id)
    await asyncio.sleep(0)

    assert pushy_service.recorder.dismissed == []


@pytest.mark.postgres_only
async def test_expiry_sweep_dismisses_a_pushed_surface(pushy_service, db) -> None:
    """Dismiss site 2 of 3 — carried out of the atomic claim's RETURNING."""
    surface_id = await pushy_service.push_built(_approval())
    await asyncio.sleep(0)
    async with db.session() as session:
        await session.execute(
            A2uiSurface.__table__.update()
            .where(A2uiSurface.surface_id == surface_id)
            .values(expires_at=datetime.now(UTC) - timedelta(minutes=1))
        )
        await session.commit()

    assert await pushy_service.expire_sweep() >= 1
    await asyncio.sleep(0)

    assert surface_id in pushy_service.recorder.dismissed


@pytest.mark.postgres_only
async def test_heartbeat_invalidation_dismisses_pushed_surfaces(
    pushy_service, db, push_agent_id
) -> None:
    """Dismiss site 3 of 3 — the restart path.

    Without it, a phone keeps a tappable notification for a heartbeat card
    whose finding store died with the previous process.
    """
    pushed = await pushy_service.push_built(_approval())
    quiet = await pushy_service.push_built(_approval("Another?"), notify=False)
    await asyncio.sleep(0)
    async with db.session() as session:
        await session.execute(
            A2uiSurface.__table__.update()
            .where(A2uiSurface.agent_id == push_agent_id)
            .values(origin="heartbeat")
        )
        await session.commit()

    assert await pushy_service.invalidate_heartbeat_surfaces() == 2
    await asyncio.sleep(0)

    assert pushy_service.recorder.dismissed == [pushed]
    assert quiet not in pushy_service.recorder.dismissed


@pytest.mark.postgres_only
async def test_dedup_replacement_never_dismisses(pushy_service, db) -> None:
    """A replaced card is still on screen — cancelling its notification lies."""
    first = await pushy_service.push_built(_approval(), dedup_key="k")
    await asyncio.sleep(0)
    second = await pushy_service.push_built(_approval("Updated"), dedup_key="k")
    await asyncio.sleep(0)

    assert first == second
    assert pushy_service.recorder.dismissed == []
    # Replacement is not a creation, so it does not push again either.
    assert pushy_service.recorder.surfaces == [first]


def test_firebase_client_config_payload_shape() -> None:
    config = FirebaseClientConfig("p", "1:2:android:3", "A" + "c" * 38, "12")

    assert config.as_dict()["enabled"] is True
    assert set(config.as_dict()) == {
        "enabled",
        "project_id",
        "application_id",
        "api_key",
        "sender_id",
    }


# ---------------------------------------------------------------------------
# Telegram card ping toggle (duplicate-alert removal after push is proven)
# ---------------------------------------------------------------------------


async def _pings(svc, monkeypatch, **push_kwargs) -> list[str]:
    sent: list[str] = []

    async def record(title, surface_id, text=None):
        sent.append(surface_id)

    monkeypatch.setattr(svc, "_notify_telegram", record)
    await svc.push_built(_approval(), **push_kwargs)
    await asyncio.gather(*list(svc._pending_tasks))
    return sent


@pytest.mark.postgres_only
async def test_telegram_ping_fires_by_default(pushy_service, monkeypatch) -> None:
    assert len(await _pings(pushy_service, monkeypatch)) == 1
    assert len(pushy_service.recorder.surfaces) == 1


@pytest.mark.postgres_only
async def test_telegram_ping_off_leaves_push_only(pushy_service, monkeypatch) -> None:
    pushy_service._settings = pushy_service._settings.model_copy(
        update={"a2ui_telegram_notify_enabled": False}
    )
    assert await _pings(pushy_service, monkeypatch) == []
    assert len(pushy_service.recorder.surfaces) == 1


@pytest.mark.postgres_only
async def test_telegram_stays_the_fallback_when_push_would_not_carry(
    db, push_settings, monkeypatch
) -> None:
    """Turning the ping off must never leave a notifying card with no alert."""
    svc = SurfaceService(
        db,
        push_settings.model_copy(update={"a2ui_telegram_notify_enabled": False}),
        push=RecordingPush(configured=False),
    )
    try:
        assert len(await _pings(svc, monkeypatch)) == 1
    finally:
        async with db.session() as session:
            await session.execute(
                delete(A2uiSurface).where(A2uiSurface.agent_id == push_settings.agent_id)
            )
            await session.commit()


@pytest.mark.postgres_only
async def test_telegram_ping_off_does_not_ping_non_notifying_cards(
    pushy_service, monkeypatch
) -> None:
    pushy_service._settings = pushy_service._settings.model_copy(
        update={"a2ui_telegram_notify_enabled": False}
    )
    assert await _pings(pushy_service, monkeypatch, notify=False) == []
