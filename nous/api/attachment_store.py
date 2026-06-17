"""Side-effecting persistence for inbound attachments (F024).

Saves originals under <workspace>/attachments/<session>/, records a Heart fact
memory-reference (including the analysis summary), and chunk-ingests text/code
bodies into episode_chunks. Base64 never reaches the DB — only the on-disk path
and metadata are persisted.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from nous.api.attachments import sanitize_filename

if TYPE_CHECKING:
    from nous.api.models import Attachment
    from nous.config import Settings
    from nous.heart.heart import Heart

logger = logging.getLogger(__name__)

ATTACHMENT_PATH_PREFIX = os.path.join("attachments", "")  # ".../attachments/"


def _write_bytes(path: str, raw: bytes) -> None:
    with open(path, "wb") as f:
        f.write(raw)


async def persist_attachment(att: "Attachment", *, session_id: str,
                             settings: "Settings") -> str:
    """Write original bytes to <attachments_root>/<session>/<uuid>__<file>.

    Returns the absolute path (also set on att.workspace_path), or "" if
    persistence is disabled, there is no data, or the write fails (degrade to
    text-only — a persistence failure must never break the turn).
    """
    if not settings.attachments_persist or not att.data_base64:
        return ""
    safe_session = sanitize_filename(session_id)
    if safe_session in ("", ".", ".."):
        safe_session = "session"
    target_dir = os.path.join(settings.attachments_root, safe_session)
    # Containment assert (security boundary, runs regardless of flags).
    root = Path(settings.attachments_root).resolve()
    if not Path(target_dir).resolve().is_relative_to(root):
        logger.warning("attachment path escaped root; pinning to root")
        target_dir = str(root)
    fname = f"{uuid.uuid4().hex}__{sanitize_filename(att.filename)}"
    path = os.path.join(target_dir, fname)
    try:
        raw = base64.b64decode(att.data_base64)
        os.makedirs(target_dir, exist_ok=True)
        await asyncio.to_thread(_write_bytes, path, raw)
    except Exception as e:
        logger.warning("persist failed for %s: %s; degrading to text-only",
                       att.filename, e)
        return ""
    att.workspace_path = path
    logger.info("Persisted attachment %s (%s, %d bytes) -> %s",
                att.filename, att.content_type, att.size_bytes, path)
    return path


async def record_attachment_fact(heart: "Heart", att: "Attachment", *,
                                 agent_id: str, source_episode_id: str | None,
                                 analysis: str = "", session=None) -> None:
    """Store a Heart fact so the saved file + its summary are discoverable via recall."""
    from uuid import UUID
    from nous.heart.schemas import FactInput

    where = f" Saved at {att.workspace_path}." if att.workspace_path else ""
    summary = (analysis or "").strip()
    snippet = (summary[:400] + "…") if len(summary) > 400 else summary
    content = (f"User shared a {att.content_type} '{att.filename}'.{where}"
               + (f" Summary: {snippet}" if snippet else ""))
    try:
        await heart.learn(
            FactInput(
                content=content, category="attachment", subject=att.filename,
                source=f"{att.source}-attachment",
                source_text=(summary or None),
                source_episode_id=UUID(source_episode_id) if source_episode_id else None,
                tags=["attachment", att.content_type],
            ),
            session=session,
        )
    except Exception as e:  # a memory write must never break the turn
        logger.warning("attachment fact failed for %s: %s (saved at %s)",
                       att.filename, e, att.workspace_path)


async def maybe_ingest_text_file(heart: "Heart", settings: "Settings",
                                 att: "Attachment", *, session_id: str | None,
                                 episode_id: str | None) -> None:
    """Chunk + embed a text/code file body into episode_chunks for recall."""
    if not settings.attachments_ingest_text_files or att.content_type != "text_file":
        return
    try:
        body = base64.b64decode(att.data_base64).decode("utf-8", errors="replace")
    except Exception:
        return
    from nous.api.tools import ingest_document_text
    try:
        await ingest_document_text(heart, settings, content=body,
                                   source_ref=att.workspace_path or att.filename,
                                   session_id=session_id, episode_id=episode_id)
    except Exception as e:
        logger.warning("text-file ingest failed for %s: %s", att.filename, e)
