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

MIN_PDF_TEXT_CHARS = 100  # below this, treat as scanned/image PDF and fall back to transcription


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
        result = await heart.learn(
            FactInput(
                content=content, category="attachment", subject=att.filename,
                source=f"{att.source}-attachment",
                source_text=(summary or None),
                source_episode_id=UUID(source_episode_id) if source_episode_id else None,
                tags=["attachment", att.content_type],
            ),
            session=session,
        )
        from nous.heart.schemas import FactRejected
        if isinstance(result, FactRejected):
            logger.info("attachment fact rejected by admission for %s: %s",
                        att.filename, getattr(result, "explanation", "n/a"))
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
        result = await ingest_document_text(heart, settings, content=body,
                                            source_ref=att.workspace_path or att.filename,
                                            session_id=session_id, episode_id=episode_id)
        if isinstance(result, dict) and result.get("error"):
            code = result.get("code")
            level = logging.WARNING if code in ("embed_failed", "vector_mismatch", "no_episode") else logging.INFO
            logger.log(level, "text-file ingest no-op for %s: code=%s (%s)",
                       att.filename, code, result.get("error"))
    except Exception as e:
        logger.warning("text-file ingest failed for %s: %s", att.filename, e)


def _extract_pdf_text(raw: bytes) -> str:
    """Extract text from PDF bytes via pypdf. Returns "" if pypdf is unavailable
    or the PDF has no extractable text (e.g. scanned/image-only)."""
    try:
        from pypdf import PdfReader
    except ImportError:
        return ""
    try:
        import io
        reader = PdfReader(io.BytesIO(raw))
        return "\n".join((page.extract_text() or "") for page in reader.pages).strip()
    except Exception as e:
        logger.warning("pypdf extraction failed: %s", e)
        return ""


def _parse_transcription_response(resp) -> tuple[str, str | None]:
    """Extract (text, stop_reason) from an AnthropicClient.call() response.

    Mirrors runner._extract_text / call_background_llm: the response is an
    ApiResponse dataclass with `.content` (list of dict content blocks, each
    text block carries type=="text" and a "text" field) and `.stop_reason`.
    """
    blocks = getattr(resp, "content", None) or []
    text_parts = [
        b.get("text", "") for b in blocks
        if isinstance(b, dict) and b.get("type") == "text"
    ]
    return "\n".join(text_parts), getattr(resp, "stop_reason", None)


async def maybe_ingest_pdf(heart: "Heart", settings: "Settings", att: "Attachment", *,
                           session_id: str | None, episode_id: str | None,
                           llm_client=None) -> None:
    """Chunk+ingest a PDF's text for recall. pypdf first; for scanned PDFs (no
    extractable text), fire-and-forget a Claude transcription fallback."""
    if (not settings.attachments_ingest_pdfs or att.content_type != "document"
            or att.media_type != "application/pdf" or not att.data_base64):
        return
    try:
        raw = base64.b64decode(att.data_base64)
    except Exception as e:
        logger.warning("PDF base64 decode failed for %s: %s", att.filename, e)
        return
    text = await asyncio.to_thread(_extract_pdf_text, raw)
    source_ref = att.workspace_path or att.filename
    if len(text.strip()) >= MIN_PDF_TEXT_CHARS:
        from nous.api.tools import ingest_document_text
        try:
            result = await ingest_document_text(heart, settings, content=text,
                                                source_ref=source_ref,
                                                session_id=session_id, episode_id=episode_id)
            if isinstance(result, dict) and result.get("error"):
                code = result.get("code")
                level = logging.WARNING if code in ("embed_failed", "vector_mismatch", "no_episode") else logging.INFO
                logger.log(level, "PDF ingest no-op for %s: code=%s (%s)", att.filename, code, result.get("error"))
        except Exception as e:
            logger.warning("PDF ingest failed for %s: %s", att.filename, e)
        return
    # Scanned/image PDF: no extractable text. Fall back to model transcription (non-blocking).
    if llm_client is not None:
        asyncio.create_task(_transcribe_and_ingest_pdf(
            raw, heart, settings, llm_client, source_ref=source_ref, episode_id=episode_id,
            model=settings.attachments_pdf_transcription_model,
            max_tokens=settings.attachments_pdf_max_transcription_tokens, filename=att.filename))
    else:
        logger.info("PDF %s has no extractable text and no llm_client; skipping ingest", att.filename)


async def _transcribe_and_ingest_pdf(raw: bytes, heart: "Heart", settings: "Settings",
                                     llm_client, *, source_ref: str, episode_id: str | None,
                                     model: str, max_tokens: int, filename: str) -> None:
    """Fire-and-forget: transcribe a scanned PDF via Claude, then chunk-ingest the text.
    Best-effort — must never raise (it's an un-awaited task)."""
    try:
        b64 = base64.b64encode(raw).decode()
        payload = {"model": model, "max_tokens": max_tokens, "messages": [{"role": "user", "content": [
            {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": b64}},
            {"type": "text", "text": "Transcribe the full text content of this document verbatim. Output only the document's text, with no commentary."},
        ]}]}
        resp = await llm_client.call(payload)
        text, stop_reason = _parse_transcription_response(resp)
        if stop_reason == "max_tokens":
            logger.warning("PDF transcription truncated for %s (raise max_tokens or add paging)", filename)
        if text and text.strip():
            from nous.api.tools import ingest_document_text
            result = await ingest_document_text(heart, settings, content=text.strip(),
                                                source_ref=source_ref, session_id=None, episode_id=episode_id)
            if isinstance(result, dict) and result.get("error"):
                logger.info("PDF transcription ingest no-op for %s: %s", filename, result.get("error"))
        else:
            logger.info("PDF transcription returned no text for %s", filename)
    except Exception as e:
        logger.warning("PDF transcription/ingest failed for %s: %s", filename, e)
