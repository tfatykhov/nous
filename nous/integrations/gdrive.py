"""
Google Drive integration for Nous.

Provides file listing, download, upload, and search via a service account.
The service account JSON key is loaded from the GOOGLE_SERVICE_ACCOUNT_JSON
environment variable (base64-encoded) or a file path via GOOGLE_SERVICE_ACCOUNT_PATH.

Usage:
    from nous.integrations.gdrive import GDrive

    drive = GDrive()
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
from pathlib import Path
from typing import Any

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/drive"]

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


def _load_credentials() -> Credentials:
    """Load service account credentials from env var or file path."""
    # Option 1: base64-encoded JSON in env var
    b64_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if b64_json:
        info = json.loads(base64.b64decode(b64_json))
        return Credentials.from_service_account_info(info, scopes=SCOPES)

    # Option 2: file path
    key_path = os.environ.get("GOOGLE_SERVICE_ACCOUNT_PATH")
    if key_path and Path(key_path).exists():
        return Credentials.from_service_account_file(key_path, scopes=SCOPES)

    raise RuntimeError(
        "Google Drive credentials not found. Set GOOGLE_SERVICE_ACCOUNT_JSON "
        "(base64-encoded) or GOOGLE_SERVICE_ACCOUNT_PATH in environment."
    )


class GDrive:
    """Thin wrapper around the Google Drive API v3."""

    def __init__(self) -> None:
        self._creds = _load_credentials()
        self._service = build("drive", "v3", credentials=self._creds, cache_discovery=False)
        logger.info("GDrive initialized with service account: %s", self._creds.service_account_email)

    @property
    def service_account_email(self) -> str:
        """Return the service account email (for sharing folders)."""
        return self._creds.service_account_email

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
        destination = Path(destination)

        # Get file metadata to check if it's a Google Workspace type
        meta = self._service.files().get(fileId=file_id, fields="name, mimeType").execute()
        mime_type = meta.get("mimeType", "")

        if mime_type in EXPORT_MIME_MAP:
            export_mime, ext = EXPORT_MIME_MAP[mime_type]
            # Adjust destination extension if needed
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
        """
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

    # ── Delete ───────────────────────────────────────────────────────

    def delete_file(self, file_id: str) -> None:
        """Move a file to trash."""
        self._service.files().update(fileId=file_id, body={"trashed": True}).execute()
        logger.info("Trashed file %s", file_id)

    # ── Folder creation ──────────────────────────────────────────────

    def create_folder(self, name: str, parent_id: str | None = None) -> dict[str, Any]:
        """Create a folder in Drive. Returns metadata dict."""
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
        return (
            self._service.files()
            .get(
                fileId=file_id,
                fields="id, name, mimeType, size, modifiedTime, createdTime, webViewLink, parents, description",
            )
            .execute()
        )
