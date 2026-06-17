"""Pure functions for inbound multimodal attachment support (F024 / 011.2).

No I/O, no DB. Classification, validation, Claude content-block construction,
and history sanitization. Side-effecting persistence lives in attachment_store.py.
"""

from __future__ import annotations

import base64
import binascii
import os
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nous.api.models import Attachment, Message

IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
DOCUMENT_TYPES = {"application/pdf"}

TEXT_EXTENSIONS = {
    ".py", ".js", ".ts", ".jsx", ".tsx", ".html", ".css",
    ".c", ".cpp", ".h", ".java", ".go", ".rs", ".rb", ".php",
    ".sh", ".sql", ".yaml", ".yml", ".toml", ".json", ".csv",
    ".tsv", ".xml", ".txt", ".md", ".rst", ".log", ".env",
    ".ini", ".cfg",
}

MAX_IMAGE_SIZE = 20 * 1024 * 1024
MAX_DOCUMENT_SIZE = 32 * 1024 * 1024
MAX_TEXT_FILE_SIZE = 1 * 1024 * 1024

MAX_FILENAME_LENGTH = 255
_FILENAME_SAFE = re.compile(r"[^\w\s\-.()]", re.UNICODE)
_TEXT_FILE_HEADER = "--- File: "


def sanitize_filename(filename: str) -> str:
    """Strip path components, null bytes, unsafe chars; collapse pure-dots; truncate."""
    filename = os.path.basename(filename or "")
    filename = filename.replace("\x00", "")
    filename = _FILENAME_SAFE.sub("_", filename)
    filename = filename[:MAX_FILENAME_LENGTH]
    if filename.strip(".") == "":  # "", ".", "..", "...." -> unusable as a path segment
        return "unnamed_file"
    return filename or "unnamed_file"


def _ext(filename: str) -> str:
    return os.path.splitext(filename)[1].lower()


def classify_attachment(filename: str, media_type: str) -> str:
    """Return one of: image, document, text_file, unsupported."""
    if media_type in IMAGE_TYPES:
        return "image"
    if media_type in DOCUMENT_TYPES:
        return "document"
    if _ext(filename) in TEXT_EXTENSIONS or media_type.startswith("text/"):
        return "text_file"
    return "unsupported"


def validate_base64_size(data_base64: str) -> int:
    """Actual decoded size from base64 (never trust a client-declared size)."""
    if not data_base64:
        return 0
    padding = data_base64.count("=")
    return (len(data_base64) * 3 // 4) - padding


def validate_attachment(attachment: "Attachment") -> str | None:
    """Return a user-facing error string, or None if valid."""
    if attachment.content_type == "unsupported":
        ext = _ext(attachment.filename) or attachment.media_type
        return (f"\U0001F4CE I can't process {ext} files yet. I support images "
                f"(JPEG, PNG, GIF, WebP), PDFs, and text/code files.")
    limits = {"image": MAX_IMAGE_SIZE, "document": MAX_DOCUMENT_SIZE,
              "text_file": MAX_TEXT_FILE_SIZE}
    limit = limits.get(attachment.content_type, 0)
    effective_size = validate_base64_size(attachment.data_base64) if attachment.data_base64 else attachment.size_bytes
    if effective_size > limit:
        return (f"\U0001F4CE {attachment.filename} is too large "
                f"({effective_size / 1024 / 1024:.1f} MB). Max for "
                f"{attachment.content_type} is {limit / 1024 / 1024:.0f} MB.")
    # F9: image/document base64 must actually decode, else the API 400s opaquely.
    if attachment.content_type in ("image", "document") and attachment.data_base64:
        try:
            base64.b64decode(attachment.data_base64, validate=True)
        except (binascii.Error, ValueError):
            return (f"\U0001F4CE {attachment.filename} appears corrupted "
                    f"(unreadable data). Please resend it.")
    return None


def build_content_blocks(text: str, attachments: list["Attachment"]) -> list[dict]:
    """Build Claude content blocks: media first, then user text last.

    F11: the final block is always a text block so the prompt-cache breakpoint
    never lands on a base64 image/document block.
    """
    blocks: list[dict] = []
    for att in attachments:
        if att.content_type == "image":
            blocks.append({"type": "image", "source": {
                "type": "base64", "media_type": att.media_type, "data": att.data_base64}})
        elif att.content_type == "document":
            blocks.append({"type": "document", "source": {
                "type": "base64", "media_type": att.media_type, "data": att.data_base64}})
        elif att.content_type == "text_file":
            try:
                body = base64.b64decode(att.data_base64).decode("utf-8", errors="replace")
                blocks.append({"type": "text",
                               "text": f"{_TEXT_FILE_HEADER}{att.filename} ---\n{body}"})
            except Exception:
                blocks.append({"type": "text", "text": f"[Could not decode file: {att.filename}]"})
    if text:
        blocks.append({"type": "text", "text": text})
    elif not blocks or blocks[-1].get("type") != "text":
        blocks.append({"type": "text", "text": "(no caption)"})
    return blocks


def _ref_label(att: "Attachment | None", kind: str = "file") -> str:
    if att is None:
        return f"[Attached {kind} was analyzed]"
    where = f" — saved at {att.workspace_path}" if att.workspace_path else ""
    label = {"image": "image", "document": "document"}.get(att.content_type, "file")
    return f"[Attached {label}: {att.filename}{where}]"


def sanitize_blocks_for_storage(content: "str | list[dict]", attachments: "list[Attachment] | None" = None) -> "str | list[dict]":
    """Return DB-safe content: strip base64 from image/doc blocks and replace
    text-file body blocks with a reference label. Pure; safe to call repeatedly.

    When ``attachments`` is omitted (e.g. from _save_conversation), base64 is
    still stripped; reference labels degrade to a generic form.
    """
    if isinstance(content, str):
        return content
    atts = attachments or []
    media = [a for a in atts if a.content_type in ("image", "document")]
    text_files = [a for a in atts if a.content_type == "text_file"]
    mi = ti = 0
    parts: list[dict] = []
    for block in content:
        bt = block.get("type") if isinstance(block, dict) else None
        if bt in ("image", "document"):
            att = media[mi] if mi < len(media) else None
            mi += 1
            parts.append({"type": "text", "text": _ref_label(att, bt)})
        elif bt == "text" and str(block.get("text", "")).startswith(_TEXT_FILE_HEADER):
            att = text_files[ti] if ti < len(text_files) else None
            ti += 1
            parts.append({"type": "text",
                          "text": _ref_label(att, "file") if att else "[Attached file ingested]"})
        elif bt == "text":
            parts.append(block)
    if len(parts) == 1 and parts[0].get("type") == "text":
        return parts[0]["text"]
    return parts


def compact_message_for_history(message: "Message") -> "Message":
    """Swap heavy blocks for actionable on-disk references; clear base64.

    Called on the live in-memory message after the API interaction so tool-loop
    re-sends, dedup, and subsequent turns stay clean. The original file remains
    on disk (see attachment_store). DB-safety itself is enforced separately by
    sanitize_blocks_for_storage in _save_conversation.
    """
    from nous.api.models import Message
    if isinstance(message.content, str):
        return message
    content = sanitize_blocks_for_storage(message.content, message.attachments)
    for att in (message.attachments or []):
        att.data_base64 = ""  # allow GC; original is on disk
    return Message(role=message.role, content=content,
                   attachments=message.attachments, text_content=message.text_content)
