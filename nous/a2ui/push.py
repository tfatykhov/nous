"""F097 §6: FCM push for the A2UI companion.

A second notification leg beside the existing Telegram ping. It carries a
POINTER to a surface that already exists and is already durable, so a lost
push loses the pointer and never the card — which is why every failure here
is a warning and nothing is ever retried.

Messages are **data-only**: the Android app owns display and dismissal, so a
notification can be cancelled when its card is resolved. A notification-type
message would be posted by the system and could never be taken back.

Three things in here are deliberate and easy to get wrong:

- **Token minting blocks.** `google.auth` ships a synchronous transport, so
  the refresh runs in a worker thread, single-flight under a lock (a fan-out
  would otherwise mint one token per recipient), and is cached until shortly
  before expiry. The loop-stall watchdog exists because of this class of call.
- **`google-auth` is an optional extra.** It is in the production image
  (`Dockerfile` installs `.[runtime,agent,rerank]`) but not in a bare
  `pip install .`. The import is lazy so a lean environment degrades to
  "push not configured" instead of failing to boot.
- **A dead token is cleared, not deleted.** The row survives with its
  `last_error`, so an operator can see that an install went away.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import select, update

from nous.storage.models import A2uiPushInstallation

logger = logging.getLogger(__name__)

_FCM_SCOPE = "https://www.googleapis.com/auth/firebase.messaging"
_FCM_ENDPOINT = "https://fcm.googleapis.com/v1/projects/{project_id}/messages:send"
# A Google API key is a fixed shape; a truncated paste is the likeliest way to
# get a config that looks present and fails at Firebase Installations instead.
_API_KEY_RE = re.compile(r"^A[\w-]{38}$")
# FCM's documented data-payload limit. Titles and bodies are truncated long
# before this; the check catches a pathological surface id or link.
_MAX_PAYLOAD_BYTES = 4096
_TITLE_MAX = 100
_BODY_MAX = 240
_NAME_MAX = 40
_APP_VERSION_MAX = 32
# Refresh a little before the token actually dies, so a send never races it.
_TOKEN_SKEW_SECONDS = 120
_DISMISS_TTL_SECONDS = 28 * 24 * 3600
_SURFACE_TTL_MAX_SECONDS = 24 * 3600
_SURFACE_TTL_MIN_SECONDS = 60
_TEST_TTL_SECONDS = 3600


class PushNotConfigured(Exception):
    """Raised by the test endpoint when push cannot send; never by the send path."""


@dataclass(frozen=True)
class FirebaseClientConfig:
    """The four values an Android app needs to initialise Firebase itself.

    Served to the app instead of shipping a `google-services.json` in a public
    repo's APK. See F097 §6.2.
    """

    project_id: str
    application_id: str
    api_key: str
    sender_id: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "enabled": True,
            "project_id": self.project_id,
            "application_id": self.application_id,
            "api_key": self.api_key,
            "sender_id": self.sender_id,
        }


def _clean(value: Any, limit: int) -> str:
    """Trim to `limit` CODE POINTS with control characters removed.

    Slicing a `str` in Python is already code-point safe; the danger is
    elsewhere — `_MAX_PAYLOAD_BYTES` is measured on the encoded payload.
    """
    text = "" if value is None else str(value)
    text = "".join(ch for ch in text if ch == " " or not (ord(ch) < 32 or ord(ch) == 127))
    return text.strip()[:limit]


def parse_google_services(raw: dict[str, Any], package_name: str) -> FirebaseClientConfig:
    """Select this package's client out of a console `google-services.json`.

    Raises `ValueError` naming what is wrong. The caller turns that into
    `{enabled: false, reason}` rather than a 500: a misconfigured push leg
    must not take down the companion.
    """
    info = raw.get("project_info") or {}
    project_id = str(info.get("project_id") or "")
    sender_id = str(info.get("project_number") or "")
    clients = raw.get("client") or []
    if not isinstance(clients, list):
        raise ValueError("google-services.json: `client` is not a list")

    for client in clients:
        if not isinstance(client, dict):
            continue
        client_info = client.get("client_info") or {}
        android = client_info.get("android_client_info") or {}
        if android.get("package_name") != package_name:
            continue
        application_id = str(client_info.get("mobilesdk_app_id") or "")
        keys = client.get("api_key") or []
        api_key = ""
        if isinstance(keys, list) and keys and isinstance(keys[0], dict):
            api_key = str(keys[0].get("current_key") or "")

        if not project_id:
            raise ValueError("google-services.json: empty project_info.project_id")
        if ":" not in application_id:
            raise ValueError(
                f"google-services.json: mobilesdk_app_id {application_id!r} is not an app id"
            )
        if not _API_KEY_RE.match(api_key):
            raise ValueError("google-services.json: api_key does not look like a Google API key")
        if not sender_id.isdigit():
            raise ValueError(
                f"google-services.json: project_number {sender_id!r} is not numeric"
            )
        return FirebaseClientConfig(project_id, application_id, api_key, sender_id)

    raise ValueError(f"google-services.json has no android client for package {package_name!r}")


class PushService:
    """Mints FCM tokens, keeps the installation table, and sends.

    Constructed unconditionally when A2UI is on; `configured` reports whether
    it can actually send. Everything else degrades quietly around that.
    """

    def __init__(self, database: Any, settings: Any, http: httpx.AsyncClient | None = None):
        self._db = database
        self._settings = settings
        self._http = http
        self._token: str | None = None
        self._token_expiry: float = 0.0
        self._token_lock = asyncio.Lock()
        self._client_config: FirebaseClientConfig | None = None
        self._config_error: str | None = None
        self._load_client_config()

    # ---------------------------------------------------------------- config

    def _load_client_config(self) -> None:
        path = (getattr(self._settings, "a2ui_fcm_google_services_file", "") or "").strip()
        if not path:
            self._config_error = "no google-services.json configured"
            return
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
        except OSError as exc:
            self._config_error = f"cannot read {path}: {exc}"
            return
        except json.JSONDecodeError as exc:
            self._config_error = f"{path} is not valid JSON: {exc}"
            return
        try:
            self._client_config = parse_google_services(
                raw, self._settings.a2ui_android_package
            )
        except ValueError as exc:
            self._config_error = str(exc)
            return
        self._config_error = None

    @property
    def configured(self) -> bool:
        """True when a send could actually be attempted."""
        return bool(
            getattr(self._settings, "a2ui_push_enabled", False)
            and self._client_config is not None
            and (getattr(self._settings, "a2ui_fcm_service_account_file", "") or "").strip()
        )

    def client_config_payload(self) -> dict[str, Any]:
        """`GET /a2ui/push/config` — what the app needs, or why it gets nothing."""
        if not getattr(self._settings, "a2ui_push_enabled", False):
            return {"enabled": False, "reason": "push disabled"}
        if self._client_config is None:
            return {"enabled": False, "reason": self._config_error or "not configured"}
        if not (getattr(self._settings, "a2ui_fcm_service_account_file", "") or "").strip():
            return {"enabled": False, "reason": "no service account configured"}
        return self._client_config.as_dict()

    # ----------------------------------------------------------------- token

    def _mint_token_sync(self) -> tuple[str, float]:
        """Blocking OAuth mint. Runs in a worker thread via asyncio.to_thread.

        Imported lazily: `google-auth` is an optional extra, present in the
        production image but not in a bare install.
        """
        from google.auth.transport.requests import Request  # noqa: PLC0415
        from google.oauth2 import service_account  # noqa: PLC0415

        creds = service_account.Credentials.from_service_account_file(
            self._settings.a2ui_fcm_service_account_file, scopes=[_FCM_SCOPE]
        )
        creds.refresh(Request())
        expiry = creds.expiry.replace(tzinfo=UTC).timestamp() if creds.expiry else time.time() + 3000
        return creds.token, expiry

    async def _access_token(self) -> str:
        """Cached OAuth token, minted at most once at a time.

        Single-flight matters: a fan-out to N phones would otherwise mint N
        tokens through a blocking transport, N threads at a time.
        """
        async with self._token_lock:
            if self._token and time.time() < self._token_expiry - _TOKEN_SKEW_SECONDS:
                return self._token
            token, expiry = await asyncio.to_thread(self._mint_token_sync)
            self._token, self._token_expiry = token, expiry
            return token

    # --------------------------------------------------------- installations

    async def register(
        self,
        installation_id: str,
        *,
        fcm_token: str,
        name: str = "",
        app_version: str = "",
        notifications_enabled: bool = True,
        platform: str = "android",
    ) -> tuple[int, dict[str, Any]]:
        """Upsert one installation. Returns `(http_status, payload)`."""
        installation_id = (installation_id or "").strip()
        if not installation_id:
            return 400, {"error": "installation_id is required"}
        if not (fcm_token or "").strip():
            return 400, {"error": "fcm_token is required"}

        agent_id = self._settings.agent_id
        name = _clean(name, _NAME_MAX)
        app_version = _clean(app_version, _APP_VERSION_MAX)
        async with self._db.session() as session:
            existing = (
                await session.execute(
                    select(A2uiPushInstallation).where(
                        A2uiPushInstallation.agent_id == agent_id,
                        A2uiPushInstallation.installation_id == installation_id,
                    )
                )
            ).scalar_one_or_none()

            if existing is None:
                total = len(
                    (
                        await session.execute(
                            select(A2uiPushInstallation.installation_id).where(
                                A2uiPushInstallation.agent_id == agent_id
                            )
                        )
                    ).all()
                )
                if total >= self._settings.a2ui_push_max_installations:
                    return 409, {
                        "error": (
                            f"installation cap reached "
                            f"({self._settings.a2ui_push_max_installations}); "
                            "delete an old installation first"
                        )
                    }
                session.add(
                    A2uiPushInstallation(
                        agent_id=agent_id,
                        installation_id=installation_id,
                        name=name,
                        platform=platform,
                        fcm_token=fcm_token,
                        app_version=app_version,
                        notifications_enabled=notifications_enabled,
                    )
                )
                await session.commit()
                # A tripwire, not a gate: the tailnet is the access control,
                # but a registration the operator did not make should be seen.
                await self._notify_new_installation(name or installation_id[:8])
                return 200, {"ok": True, "created": True}

            existing.fcm_token = fcm_token
            existing.name = name or existing.name
            existing.app_version = app_version
            existing.notifications_enabled = notifications_enabled
            existing.platform = platform
            existing.last_error = None
            existing.updated_at = datetime.now(UTC)
            await session.commit()
            return 200, {"ok": True, "created": False}

    async def deregister(self, installation_id: str) -> tuple[int, dict[str, Any]]:
        agent_id = self._settings.agent_id
        async with self._db.session() as session:
            row = (
                await session.execute(
                    select(A2uiPushInstallation).where(
                        A2uiPushInstallation.agent_id == agent_id,
                        A2uiPushInstallation.installation_id == installation_id,
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                return 404, {"error": "unknown installation"}
            await session.delete(row)
            await session.commit()
        return 200, {"ok": True}

    async def list_installations(self) -> dict[str, Any]:
        """Operator view. Never includes the token."""
        agent_id = self._settings.agent_id
        async with self._db.session() as session:
            rows = (
                (
                    await session.execute(
                        select(A2uiPushInstallation)
                        .where(A2uiPushInstallation.agent_id == agent_id)
                        .order_by(A2uiPushInstallation.created_at)
                    )
                )
                .scalars()
                .all()
            )
        return {
            "configured": self.configured,
            "installations": [
                {
                    "installation_id": r.installation_id,
                    "name": r.name,
                    "platform": r.platform,
                    "app_version": r.app_version,
                    "notifications_enabled": r.notifications_enabled,
                    "has_token": r.fcm_token is not None,
                    "last_error": r.last_error,
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                    "updated_at": r.updated_at.isoformat() if r.updated_at else None,
                }
                for r in rows
            ],
        }

    async def _recipients(self) -> list[tuple[str, str]]:
        """`(installation_id, token)` for every install that can receive."""
        agent_id = self._settings.agent_id
        async with self._db.session() as session:
            rows = (
                (
                    await session.execute(
                        select(A2uiPushInstallation).where(
                            A2uiPushInstallation.agent_id == agent_id,
                            A2uiPushInstallation.fcm_token.is_not(None),
                            A2uiPushInstallation.notifications_enabled.is_(True),
                        )
                    )
                )
                .scalars()
                .all()
            )
        return [(r.installation_id, r.fcm_token) for r in rows if r.fcm_token]

    async def _clear_token(self, installation_id: str, reason: str) -> None:
        """A dead token is cleared, never deleted — the install stays visible."""
        async with self._db.session() as session:
            await session.execute(
                update(A2uiPushInstallation)
                .where(
                    A2uiPushInstallation.agent_id == self._settings.agent_id,
                    A2uiPushInstallation.installation_id == installation_id,
                )
                .values(fcm_token=None, last_error=reason[:500], updated_at=datetime.now(UTC))
            )
            await session.commit()

    # ------------------------------------------------------------------ send

    async def notify_surface(
        self,
        surface_id: str,
        *,
        title: str,
        body: str = "",
        priority: int = 1,
        kind: str = "",
        expires_at: datetime | None = None,
    ) -> None:
        ttl = _SURFACE_TTL_MAX_SECONDS
        if expires_at is not None:
            remaining = (expires_at - datetime.now(UTC)).total_seconds()
            ttl = int(max(_SURFACE_TTL_MIN_SECONDS, min(remaining, _SURFACE_TTL_MAX_SECONDS)))
        data = {
            "v": "1",
            "type": "surface",
            "surface_id": surface_id,
            "title": _clean(title, _TITLE_MAX),
            "body": _clean(body, _BODY_MAX),
            "priority": str(priority),
            "kind": kind,
        }
        if expires_at is not None:
            data["expires_at"] = expires_at.isoformat()
        await self._send(data, ttl_seconds=ttl, high_priority=True)

    async def notify_dismiss(self, surface_id: str) -> None:
        """Cancel a notification whose card is no longer live.

        Harmless on a phone that never received the original: the app simply
        cancels a tag it does not have, and records a tombstone.
        """
        await self._send(
            {"v": "1", "type": "dismiss", "surface_id": surface_id},
            ttl_seconds=_DISMISS_TTL_SECONDS,
            high_priority=False,
        )

    async def send_test(self, installation_id: str) -> tuple[int, dict[str, Any]]:
        """`POST /a2ui/push/test`. Unlike the send path, this reports failure."""
        if not self.configured:
            payload = self.client_config_payload()
            return 503, {"error": payload.get("reason", "push not configured")}
        async with self._db.session() as session:
            row = (
                await session.execute(
                    select(A2uiPushInstallation).where(
                        A2uiPushInstallation.agent_id == self._settings.agent_id,
                        A2uiPushInstallation.installation_id == installation_id,
                    )
                )
            ).scalar_one_or_none()
        if row is None:
            return 404, {"error": "unknown installation"}
        if not row.fcm_token:
            return 409, {"error": "installation has no token", "last_error": row.last_error}

        data = {
            "v": "1",
            "type": "test",
            "title": "Nous companion",
            "body": "Push is working.",
        }
        try:
            token = await self._access_token()
        except Exception as exc:
            logger.warning("F097: FCM token mint failed for test send", exc_info=True)
            return 502, {"error": f"could not mint an FCM token: {exc}"}
        ok, detail = await self._send_one(
            token, installation_id, row.fcm_token, data, _TEST_TTL_SECONDS, True
        )
        return (200, {"ok": True}) if ok else (502, {"error": detail})

    async def _send(self, data: dict[str, str], *, ttl_seconds: int, high_priority: bool) -> None:
        """Fan out to every recipient. Best-effort: nothing here is retried."""
        if not self.configured:
            return
        recipients = await self._recipients()
        if not recipients:
            return
        try:
            token = await self._access_token()
        except Exception:
            logger.warning("F097: FCM token mint failed; push skipped", exc_info=True)
            return

        timeout = self._settings.a2ui_push_timeout_seconds
        try:
            async with asyncio.timeout(timeout):
                await asyncio.gather(
                    *(
                        self._send_one(token, iid, tok, data, ttl_seconds, high_priority)
                        for iid, tok in recipients
                    ),
                    return_exceptions=True,
                )
        except TimeoutError:
            logger.warning("F097: FCM fan-out exceeded %ss", timeout)

    async def _send_one(
        self,
        access_token: str,
        installation_id: str,
        fcm_token: str,
        data: dict[str, str],
        ttl_seconds: int,
        high_priority: bool,
    ) -> tuple[bool, str]:
        """One FCM v1 send. "Sent" means ACCEPTED BY FCM — delivery is unobservable."""
        assert self._client_config is not None  # `configured` was checked by the caller
        message = {
            "message": {
                "token": fcm_token,
                "data": data,
                "android": {
                    "priority": "HIGH" if high_priority else "NORMAL",
                    "ttl": f"{ttl_seconds}s",
                    "restricted_package_name": self._settings.a2ui_android_package,
                },
            }
        }
        encoded = json.dumps(message)
        if len(json.dumps(data).encode("utf-8")) > _MAX_PAYLOAD_BYTES:
            logger.warning("F097: FCM payload over %d bytes; not sent", _MAX_PAYLOAD_BYTES)
            return False, "payload too large"

        url = _FCM_ENDPOINT.format(project_id=self._client_config.project_id)
        client = self._http or httpx.AsyncClient()
        try:
            response = await client.post(
                url,
                content=encoded,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json",
                },
                timeout=10,
            )
            if response.status_code < 400:
                return True, ""
            detail = _fcm_error_detail(response)
            if _token_is_dead(response, detail):
                await self._clear_token(installation_id, detail)
                logger.info("F097: cleared dead FCM token for %s (%s)", installation_id, detail)
            else:
                logger.warning("F097: FCM send failed (%s): %s", response.status_code, detail)
            return False, detail
        except Exception as exc:
            logger.warning("F097: FCM send failed for %s", installation_id, exc_info=True)
            return False, str(exc)
        finally:
            if self._http is None:
                await client.aclose()

    async def _notify_new_installation(self, name: str) -> None:
        token = self._settings.telegram_bot_token
        chat_id = self._settings.telegram_chat_id
        if not token or not chat_id:
            return
        client = self._http or httpx.AsyncClient()
        try:
            await client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": f"New companion push installation: {name}"},
                timeout=10,
            )
        except Exception:
            logger.warning("F097: new-installation tripwire failed")
        finally:
            if self._http is None:
                await client.aclose()


def _fcm_error_detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except Exception:
        return f"HTTP {response.status_code}"
    error = payload.get("error") or {}
    status = error.get("status") or ""
    message = error.get("message") or ""
    return f"{status or response.status_code}: {message}"[:500]


def _token_is_dead(response: httpx.Response, detail: str) -> bool:
    """Is this error the token's fault, or the moment's?

    UNREGISTERED and SENDER_ID_MISMATCH are unambiguous. INVALID_ARGUMENT is
    not — it also covers a malformed payload, which is OUR bug, and clearing
    the token for it would silently unregister every phone. So it counts only
    when the structured `fieldViolations` names `message.token`.
    """
    try:
        payload = response.json()
    except Exception:
        return False
    error = payload.get("error") or {}
    status = error.get("status") or ""
    if status in {"UNREGISTERED", "NOT_FOUND"}:
        return True
    for item in error.get("details") or []:
        if not isinstance(item, dict):
            continue
        if item.get("errorCode") in {"UNREGISTERED", "SENDER_ID_MISMATCH"}:
            return True
        for violation in item.get("fieldViolations") or []:
            if isinstance(violation, dict) and violation.get("field") == "message.token":
                return True
    return False
