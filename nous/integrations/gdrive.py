"""
Google Drive integration for Nous.

Supports two authentication modes:
  1. **OAuth2 (preferred for uploads)** — authenticates as Tim's account.
     Set env vars: GDRIVE_CLIENT_ID, GDRIVE_CLIENT_SECRET, GDRIVE_REFRESH_TOKEN
  2. **Service Account (read-only fallback)** — limited (no upload quota).
     Set env vars: GOOGLE_SERVICE_ACCOUNT_JSON (base64) or GOOGLE_SERVICE_ACCOUNT_PATH

OAuth credentials auto-refresh access tokens using the refresh token.
If the app is published (not in Testing mode), the refresh token is permanent.

Usage:
    from nous.integrations.gdrive import GDrive

    drive = GDrive()                     # auto-detects best auth method
    drive = GDrive(auth="oauth")         # force OAuth
    drive = GDrive(auth="service")       # force service account

    files = drive.list_files(folder_id="...")
    drive.download_file(file_id="...", destination="/tmp/report.pdf")
    drive.upload_file("/tmp/chart.png", folder_id="...", name="chart.png")
"""

from __future__ import annotations

import base64
import io
import json
import logging
import mimetypes
import os
import threading
import time
from pathlib import Path
from typing import Any

from google.oauth2.credentials import Credentials as OAuthCredentials
from google.oauth2.service_account import Credentials as ServiceAccountCredentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/drive"]
TOKEN_URI = "https://oauth2.googleapis.com/token"

# Common Google Workspace export MIME mappings
EXPORT_MIME_MAP = {
    "application/vnd.google-apps.document": (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".docx",
    ),
    "application/vnd.google-apps.spreadsheet": (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".xlsx",
    ),
    "application/vnd.google-apps.presentation": (
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        ".pptx",
    ),
    "application/vnd.google-apps.drawing": ("image/png", ".png"),
}

# Well-known folder IDs
NOUSDRIVE_FOLDER_ID = "1cm0yhpDm9etJShf1X9ndKYaKQOAEC1L6"


def _load_oauth_credentials() -> OAuthCredentials | None:
    """
    Load OAuth2 credentials from environment variables.
    Returns None if not configured.
    """
    client_id = os.environ.get("GDRIVE_CLIENT_ID")
    client_secret = os.environ.get("GDRIVE_CLIENT_SECRET")
    refresh_token = os.environ.get("GDRIVE_REFRESH_TOKEN")

    if not all([client_id, client_secret, refresh_token]):
        return None

    creds = OAuthCredentials(
        token=None,  # will be refreshed on first use
        refresh_token=refresh_token,
        client_id=client_id,
        client_secret=client_secret,
        token_uri=TOKEN_URI,
        scopes=SCOPES,
    )
    logger.info("OAuth credentials loaded (client_id=%s...)", client_id[:20])
    return creds


def _load_service_account_credentials() -> ServiceAccountCredentials | None:
    """
    Load service account credentials from env var or file path.
    Returns None if not configured.
    """
    # Option 1: base64-encoded JSON in env var
    b64_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if b64_json:
        info = json.loads(base64.b64decode(b64_json))
        return ServiceAccountCredentials.from_service_account_info(info, scopes=SCOPES)

    # Option 2: file path
    key_path = os.environ.get("GOOGLE_SERVICE_ACCOUNT_PATH")
    if key_path and Path(key_path).exists():
        return ServiceAccountCredentials.from_service_account_file(key_path, scopes=SCOPES)

    return None


class GDrive:
    """
    Thin wrapper around the Google Drive API v3.

    Supports OAuth2 (user account) and Service Account authentication.
    OAuth tokens are auto-refreshed before each API call.
    """

    def __init__(self, auth: str | None = None) -> None:
        """
        Initialize the Drive client.

        Args:
            auth: Force auth mode — "oauth" or "service". If None, auto-detects
                  (prefers OAuth if configured, falls back to service account).
        """
        self._creds = None
        self._auth_mode = None
        self._lock = threading.Lock()  # thread-safe token refresh

        if auth == "oauth" or auth is None:
            self._creds = _load_oauth_credentials()
            if self._creds:
                self._auth_mode = "oauth"
                # Force initial token refresh
                self._ensure_valid_token()
                logger.info("GDrive initialized with OAuth (user account)")

        if self._creds is None and (auth == "service" or auth is None):
            self._creds = _load_service_account_credentials()
            if self._creds:
                self._auth_mode = "service"
                logger.info(
                    "GDrive initialized with service account: %s",
                    self._creds.service_account_email,
                )

        if self._creds is None:
            raise RuntimeError(
                "Google Drive credentials not found. Set either:\n"
                "  OAuth: GDRIVE_CLIENT_ID, GDRIVE_CLIENT_SECRET, GDRIVE_REFRESH_TOKEN\n"
                "  Service Account: GOOGLE_SERVICE_ACCOUNT_JSON or GOOGLE_SERVICE_ACCOUNT_PATH"
            )

        self._service = build("drive", "v3", credentials=self._creds, cache_discovery=False)

    def _ensure_valid_token(self) -> None:
        """
        Ensure the access token is valid. For OAuth, refreshes if expired
        or not yet obtained. Thread-safe.
        """
        if self._auth_mode != "oauth":
            return

        with self._lock:
            if not self._creds.valid:
                logger.info("Access token expired or missing — refreshing...")
                try:
                    self._creds.refresh(Request())
                    logger.info(
                        "Access token refreshed successfully (expires: %s)",
                        self._creds.expiry,
                    )
                except Exception as e:
                    logger.error("Failed to refresh access token: %s", e)
                    raise RuntimeError(
                        f"OAuth token refresh failed: {e}\n"
                        "The refresh token may have expired. Ask Tim to re-authorize."
                    ) from e

    def _call(self, method, *args, **kwargs):
        """
        Execute a Drive API call with automatic token refresh.
        Wraps every API call to ensure fresh credentials.
        """
        self._ensure_valid_token()
        return method(*args, **kwargs).execute()

    @property
    def auth_mode(self) -> str:
        """Return current auth mode: 'oauth' or 'service'."""
        return self._auth_mode

    @property
    def is_oauth(self) -> bool:
        """True if using OAuth (can upload without quota issues)."""
        return self._auth_mode == "oauth"

    @property
    def token_expiry(self) -> str | None:
        """Return the current access token expiry time (OAuth only)."""
        if self._auth_mode == "oauth" and self._creds.expiry:
            return self._creds.expiry.isoformat()
        return None

    # ── List ─────────────────────────────────────────────────────────

    def list_files(
        self,
        folder_id: str | None = None,
        page_size: int = 50,
        query: str | None = None,
        include_trashed: bool = False,
    ) -> list[dict[str, Any]]:
        """
        List files. If folder_id is given, lists that folder's contents.
        Returns list of dicts with id, name, mimeType, size, modifiedTime.
        """
        self._ensure_valid_token()

        q_parts: list[str] = []
        if folder_id:
            q_parts.append(f"'{folder_id}' in parents")
        if not include_trashed:
            q_parts.append("trashed = false")
        if query:
            q_parts.append(query)

        q = " and ".join(q_parts) if q_parts else None

        results: list[dict[str, Any]] = []
        page_token = None

        while True:
            resp = (
                self._service.files()
                .list(
                    q=q,
                    pageSize=page_size,
                    fields="nextPageToken, files(id, name, mimeType, size, modifiedTime, parents)",
                    pageToken=page_token,
                )
                .execute()
            )
            results.extend(resp.get("files", []))
            page_token = resp.get("nextPageToken")
            if not page_token:
                break

        logger.info("Listed %d files (folder=%s)", len(results), folder_id)
        return results

    # ── Search ───────────────────────────────────────────────────────

    def search_files(self, name_contains: str, folder_id: str | None = None) -> list[dict[str, Any]]:
        """Search for files by name substring."""
        escaped = name_contains.replace("'", "\\'")
        query = f"name contains '{escaped}'"
        return self.list_files(folder_id=folder_id, query=query)

    # ── Download ─────────────────────────────────────────────────────

    def download_file(self, file_id: str, destination: str | Path) -> Path:
        """
        Download a file by ID to a local path.
        Handles Google Workspace files (Docs, Sheets, Slides) by exporting them.
        Returns the final Path.
        """
        self._ensure_valid_token()
        destination = Path(destination)

        # Get file metadata to check if it's a Google Workspace type
        meta = self._service.files().get(fileId=file_id, fields="name, mimeType").execute()
        mime_type = meta.get("mimeType", "")

        if mime_type in EXPORT_MIME_MAP:
            export_mime, ext = EXPORT_MIME_MAP[mime_type]
            if destination.suffix != ext:
                destination = destination.with_suffix(ext)
            request = self._service.files().export_media(fileId=file_id, mimeType=export_mime)
        else:
            request = self._service.files().get_media(fileId=file_id)

        destination.parent.mkdir(parents=True, exist_ok=True)

        with open(destination, "wb") as f:
            downloader = MediaIoBaseDownload(f, request)
            done = False
            while not done:
                status, done = downloader.next_chunk()
                if status:
                    logger.debug("Download %s: %d%%", file_id, int(status.progress() * 100))

        logger.info("Downloaded %s → %s", file_id, destination)
        return destination

    # ── Upload ───────────────────────────────────────────────────────

    def upload_file(
        self,
        local_path: str | Path,
        folder_id: str | None = None,
        name: str | None = None,
        description: str | None = None,
    ) -> dict[str, Any]:
        """
        Upload a local file to Google Drive.
        Returns the file metadata dict (id, name, mimeType, webViewLink).

        Note: Service account uploads will fail with storageQuotaExceeded.
        Use OAuth mode for uploads.
        """
        self._ensure_valid_token()

        if self._auth_mode == "service":
            logger.warning(
                "Uploading with service account — this may fail with "
                "storageQuotaExceeded. Consider using OAuth mode."
            )

        local_path = Path(local_path)
        if not local_path.exists():
            raise FileNotFoundError(f"File not found: {local_path}")

        file_name = name or local_path.name
        mime_type = mimetypes.guess_type(str(local_path))[0] or "application/octet-stream"

        file_metadata: dict[str, Any] = {"name": file_name}
        if folder_id:
            file_metadata["parents"] = [folder_id]
        if description:
            file_metadata["description"] = description

        media = MediaFileUpload(str(local_path), mimetype=mime_type, resumable=True)

        result = (
            self._service.files()
            .create(
                body=file_metadata,
                media_body=media,
                fields="id, name, mimeType, webViewLink, size",
            )
            .execute()
        )

        logger.info("Uploaded %s → %s (id=%s)", local_path, result["name"], result["id"])
        return result

    # ── Update (overwrite existing file) ─────────────────────────────

    def update_file(
        self,
        file_id: str,
        local_path: str | Path,
        name: str | None = None,
    ) -> dict[str, Any]:
        """
        Update (overwrite) an existing file's content.
        Optionally rename it too.
        """
        self._ensure_valid_token()

        local_path = Path(local_path)
        if not local_path.exists():
            raise FileNotFoundError(f"File not found: {local_path}")

        mime_type = mimetypes.guess_type(str(local_path))[0] or "application/octet-stream"
        media = MediaFileUpload(str(local_path), mimetype=mime_type, resumable=True)

        body: dict[str, Any] = {}
        if name:
            body["name"] = name

        result = (
            self._service.files()
            .update(
                fileId=file_id,
                body=body if body else None,
                media_body=media,
                fields="id, name, mimeType, webViewLink, size",
            )
            .execute()
        )

        logger.info("Updated %s → %s (id=%s)", local_path, result["name"], result["id"])
        return result

    # ── Delete ───────────────────────────────────────────────────────

    def delete_file(self, file_id: str) -> None:
        """Move a file to trash."""
        self._ensure_valid_token()
        self._service.files().update(fileId=file_id, body={"trashed": True}).execute()
        logger.info("Trashed file %s", file_id)

    # ── Folder creation ──────────────────────────────────────────────

    def create_folder(self, name: str, parent_id: str | None = None) -> dict[str, Any]:
        """Create a folder in Drive. Returns metadata dict."""
        self._ensure_valid_token()

        metadata: dict[str, Any] = {
            "name": name,
            "mimeType": "application/vnd.google-apps.folder",
        }
        if parent_id:
            metadata["parents"] = [parent_id]

        result = (
            self._service.files()
            .create(body=metadata, fields="id, name, webViewLink")
            .execute()
        )
        logger.info("Created folder '%s' (id=%s)", name, result["id"])
        return result

    # ── File info ────────────────────────────────────────────────────

    def get_file_info(self, file_id: str) -> dict[str, Any]:
        """Get detailed metadata for a file."""
        self._ensure_valid_token()
        return (
            self._service.files()
            .get(
                fileId=file_id,
                fields="id, name, mimeType, size, modifiedTime, createdTime, webViewLink, parents, description",
            )
            .execute()
        )

    # ── Convenience: upload to NousDrive ─────────────────────────────

    def upload_to_nousdrive(
        self,
        local_path: str | Path,
        subfolder: str | None = None,
        name: str | None = None,
        description: str | None = None,
    ) -> dict[str, Any]:
        """
        Upload a file to NousDrive, optionally into a subfolder.
        If subfolder doesn't exist, creates it.

        Args:
            local_path: Path to the local file.
            subfolder: Optional subfolder name within NousDrive (e.g. "Architecture").
            name: Optional file name override.
            description: Optional file description.

        Returns:
            File metadata dict from Google Drive API.
        """
        target_folder = NOUSDRIVE_FOLDER_ID

        if subfolder:
            # Check if subfolder already exists
            existing = self.list_files(
                folder_id=NOUSDRIVE_FOLDER_ID,
                query=f"name = '{subfolder}' and mimeType = 'application/vnd.google-apps.folder'",
            )
            if existing:
                target_folder = existing[0]["id"]
                logger.info("Using existing subfolder '%s' (id=%s)", subfolder, target_folder)
            else:
                folder = self.create_folder(subfolder, parent_id=NOUSDRIVE_FOLDER_ID)
                target_folder = folder["id"]
                logger.info("Created subfolder '%s' (id=%s)", subfolder, target_folder)

        return self.upload_file(
            local_path=local_path,
            folder_id=target_folder,
            name=name,
            description=description,
        )

    # ── Health check ─────────────────────────────────────────────────

    def health_check(self) -> dict[str, Any]:
        """
        Run a health check: verify credentials, list NousDrive contents.
        Returns a status dict.
        """
        try:
            self._ensure_valid_token()
            about = self._service.about().get(fields="user(displayName, emailAddress)").execute()
            files = self.list_files(folder_id=NOUSDRIVE_FOLDER_ID)

            return {
                "status": "healthy",
                "auth_mode": self._auth_mode,
                "user": about.get("user", {}),
                "token_expiry": self.token_expiry,
                "nousdrive_files": len(files),
                "can_upload": self._auth_mode == "oauth",
            }
        except Exception as e:
            return {
                "status": "error",
                "auth_mode": self._auth_mode,
                "error": str(e),
            }
