# Inbound Multimodal Attachments Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let users send images, PDFs, and text/code files to the Nous agent via Telegram and the REST API; Claude analyzes them in-turn, the originals are saved to the workspace with a retrievable memory reference, and no base64 ever reaches the database.

**Architecture:** A new pure-function module (`attachments.py`) classifies/validates/builds Claude content blocks. `Message.content` becomes `str | list[dict]` (the API payload builder already passes lists through). The runner accepts an `attachments` param, builds blocks, and **after** the API call swaps the heavy blocks for a lightweight on-disk reference string and clears base64. A persistence helper saves originals under `workspace_dir/attachments/<session>/` (already sandboxed), records a Heart fact pointing at the path, and chunk-ingests text-file bodies into `episode_chunks`. REST gains an `attachments[]` field; the Telegram bot (raw httpx) downloads via `getFile` and threads attachments through its existing `_chat_streaming` payload. Everything is gated by `NOUS_ATTACHMENTS_ENABLED` (default off — land dark).

**Tech Stack:** Python 3.12, Starlette REST, raw httpx Telegram polling, async SQLAlchemy + pgvector, Anthropic Messages API (base64 inline content blocks), pytest + pytest-asyncio.

---

## Spec reconciliation (read first)

This plan revives `docs/implementation/011.2-multimodal-file-support.md` (Feature F024) but **overrides it** on three verified points:

1. **`_build_api_payload` already handles `list` content** (`nous/api/runner.py:688-704`). Multimodal blocks flow to Claude with no payload-builder change. The spec's "_format_messages needs no changes" claim is correct for the same reason.
2. **The Telegram layer in the spec is the wrong library.** The real bot is raw httpx + `self._tg()` long-polling (`nous/telegram_bot.py:484-557`). Task 8 rewrites it to the `getFile` → file-download-URL pattern.
3. **Persistence is "save + reference," not "strip + forget."** Originals are written to `workspace_dir/attachments/` and a Heart fact records the path (Task 5). Base64 is still removed from conversation history post-turn (Task 6).

**Deliberate v1 boundaries (surface for review):**
- **Native types only:** images (`image/jpeg|png|gif|webp`) + PDF (`application/pdf`) go to Claude as `image`/`document` blocks. Text/code files are decoded and injected as `text` blocks. `.docx/.xlsx/.pptx`, audio, video, archives are **rejected with a helpful message** (Claude has no native support; no new deps).
- **PDF body is NOT chunk-ingested** (would need a `pypdf` dependency). PDFs are analyzed in-turn + saved to disk + fact-referenced. **Text/code file bodies ARE chunk-ingested** (we already hold the decoded text). The on-disk PDF remains retrievable, and a follow-up can add server-side PDF extraction later.
- **No media-group batching, no per-user rate limiting** (noted in spec Known Limitations; out of scope for v1).

---

## v2 Revisions — BINDING (post team-review, 2026-06-17)

A 3-agent review (architecture wiring + security/silent-failure + devil's-advocate) against HEAD found **4 P0** and several **P1** issues. The fixes below **override** the original task bodies wherever they conflict. Implement these — they are not optional.

### F1 (P0) — Sanitize multimodal content at the SERIALIZATION boundary, not by post-turn timing

`_save_conversation` (runner.py:2533) does `[{"role": m.role, "content": m.content} ...]` and is called **inside the start-of-turn history-compaction gate** (runner.py:478 / :1045) — i.e. **before** the API call. So a long-history attachment turn persists raw base64 to `heart.conversation_state` regardless of any post-turn compaction. **Fix structurally:** add a serialization sanitizer and apply it in `_save_conversation` so the DB never receives `image`/`document` `source.data` or verbatim text-file bodies.

Add to `nous/api/attachments.py`:

```python
def sanitize_blocks_for_storage(content, attachments=None):
    """Return DB-safe content: strip base64 from image/doc blocks and replace
    text-file body blocks with a reference label. Pure; safe to call repeatedly."""
    if isinstance(content, str):
        return content
    atts = attachments or []
    media = [a for a in atts if a.content_type in ("image", "document")]
    text_files = [a for a in atts if a.content_type == "text_file"]
    mi = ti = 0
    parts = []
    for block in content:
        bt = block.get("type")
        if bt in ("image", "document"):
            att = media[mi] if mi < len(media) else None
            mi += 1
            parts.append({"type": "text", "text": _ref_label(att) if att else f"[Attached {bt} analyzed]"})
        elif bt == "text" and block.get("text", "").startswith("--- File: "):
            att = text_files[ti] if ti < len(text_files) else None
            ti += 1
            parts.append({"type": "text", "text": _ref_label(att) if att else "[Attached file ingested]"})
        elif bt == "text":
            parts.append(block)
    if len(parts) == 1 and parts[0].get("type") == "text":
        return parts[0]["text"]
    return parts
```

In `_save_conversation` (runner.py:2533-2536), map each message through it (the Conversation no longer carries `attachments` per stored message, so sanitize without them — base64 is still stripped; ref labels degrade to generic but DB-safe):

```python
        from nous.api.attachments import sanitize_blocks_for_storage
        messages = [
            {"role": m.role, "content": sanitize_blocks_for_storage(m.content)}
            for m in conversation.messages
        ]
```

This makes DB-safety independent of when `_save_conversation` runs. **`compact_message_for_history` (Task 2) is still needed** for the live in-memory object (so tool-loop re-sends, dedup, and subsequent turns are clean), but it is no longer the security boundary.

### F2 (P0) — Compact on ALL post-API paths (run_turn + stream_chat)

The original Task 6 placed compaction only before the success-path assistant append. **run_turn** also appends-and-returns on the censor-block path (runner.py:407-413) and the exception path (528); **stream_chat** on censor-block (967-971) and exception (1374).

- **Build attachments AFTER the censor check.** Move the Task 6 Step 4 build/persist block to run *after* the `if turn_context.censor_blocked:` early-return (runner.py:413). The censor path never calls the API, so it must not hold a multimodal message at all — keeping it text-only there sidesteps the leak.
- **Compact in a `finally`.** Wrap the `_tool_loop`/API call so compaction of the live user message runs on success AND exception:

```python
            try:
                response_text, ... = await self._tool_loop(...)
            finally:
                if valid_attachments:
                    for i in range(len(conversation.messages) - 1, -1, -1):
                        if conversation.messages[i].role == "user":
                            conversation.messages[i] = compact_message_for_history(
                                conversation.messages[i])
                            break
```

- For **stream_chat**, put the same compaction inside the existing `finally` at runner.py:1386, before `post_turn`.
- The memory-record calls (F4 below) stay on the **success path only** (they need `response_text`).

### F3 (P0) — `compact_message_for_history` must also strip text-file bodies + map media by identity

The original kept `--- File: …` text blocks verbatim (1MB body persists). Replace Task 2's `compact_message_for_history` body so it routes through the same logic as F1, mapping by attachment identity (not blind positional index across mixed types):

```python
def compact_message_for_history(message):
    from nous.api.models import Message
    if isinstance(message.content, str):
        return message
    content = sanitize_blocks_for_storage(message.content, message.attachments)
    for att in (message.attachments or []):
        att.data_base64 = ""  # original is on disk
    return Message(role=message.role, content=content,
                   attachments=message.attachments, text_content=message.text_content)
```

Delete the old positional-index implementation and the unused `by_name`/`_ = by_name` lines. The `test_compact_*` unit tests stay valid.

### F4 (P1) — Actually "keep a summary in memory" (fold Claude's analysis); unifies image/PDF/text

`record_attachment_fact` stored only *"a file exists."* The user asked to keep a **summary**. On the **success path**, after the turn, store the assistant analysis as the durable summary. Change `record_attachment_fact` to accept the analysis and include a snippet; additionally chunk-ingest the analysis so images AND PDFs become recall-searchable (closes the PDF-asymmetry gap with no new deps):

```python
async def record_attachment_fact(heart, att, *, agent_id, source_episode_id,
                                 analysis="", session=None):
    from uuid import UUID
    from nous.heart.schemas import FactInput
    where = f" Saved at {att.workspace_path}." if att.workspace_path else ""
    summary = (analysis or "").strip()
    snippet = (summary[:400] + "…") if len(summary) > 400 else summary
    content = (f"User shared a {att.content_type} '{att.filename}'"
               f"{where}" + (f" Summary: {snippet}" if snippet else ""))
    try:
        await heart.learn(FactInput(
            content=content, category="attachment", subject=att.filename,
            source=f"{att.source}-attachment", source_text=att.workspace_path or None,
            source_episode_id=UUID(source_episode_id) if source_episode_id else None,
            tags=["attachment", att.content_type]), session=session)
    except Exception as e:
        logger.warning("attachment fact failed for %s: %s (saved at %s)",
                       att.filename, e, att.workspace_path)
```

Runner success-path call (replaces Task 6 Step 5's record loop):

```python
                if valid_attachments:
                    for att in valid_attachments:
                        await attachment_store.record_attachment_fact(
                            self._heart, att, agent_id=_agent_id,
                            source_episode_id=None,  # see F5
                            analysis=response_text)
                        await attachment_store.maybe_ingest_text_file(
                            self._heart, self._settings, att,
                            session_id=session_id, episode_id=None)
```

`maybe_ingest_text_file` still handles verbatim text-file bodies; the analysis-summary in the fact covers images/PDFs. (Optional stretch: chunk-ingest `response_text` for image/document types via `ingest_document_text` — only if a future eval shows the fact snippet is insufficient.)

### F5 (P1) — `TurnContext` has no `episode_id`

Confirmed: `nous/cognitive/schemas.py` TurnContext has no `episode_id`. For v1, pass `source_episode_id=None` (the `UUID(x) if x else None` guard already handles it — see F4). Do **not** reference `turn_context.episode_id` (AttributeError). (Adding the field + populating it in `pre_turn` is a clean follow-up, out of scope for v1.)

### F6 (P1) — Make the consumer migration non-optional + regression-test it

The `recent_messages` list-content bug silently disables dedup and breaks working-memory focus for the whole session (dicts hit `.lower()`/`sha256(text)` → caught → no-op). Apply the Task 6 Step 6 migration at **both** sites (runner.py:387, 950) and in `_format_history_text` (runner.py:2521 — it does `f"{role}: {msg.content}"`). Add a regression test asserting every entry passed to `pre_turn(conversation_messages=…)` is a `str`.

### F7 (P1) — `persist_attachment` must degrade, not kill the turn; + path containment

- Wrap the I/O so a write failure returns `""` + logs WARNING and the turn proceeds text-only (it runs *before* the API call, so an unhandled raise = generic 500 for a turn that could have succeeded).
- **Containment assert** (security boundary, runs regardless of any flag): after computing `target_dir`/`path`, assert it resolves under `attachments_root`; reject otherwise. `session_id` is client-controlled and `sanitize_filename("..") == ".."` escapes one level.
- Harden `sanitize_filename`: collapse a pure-dots result (`""`, `"."`, `".."`, …) to `"unnamed_file"`.

```python
    safe_session = sanitize_filename(session_id)
    if safe_session in ("", ".", ".."):
        safe_session = "session"
    target_dir = os.path.join(settings.attachments_root, safe_session)
    from pathlib import Path
    root = Path(settings.attachments_root).resolve()
    if not Path(target_dir).resolve().is_relative_to(root):
        logger.warning("attachment path escaped root; using root"); target_dir = str(root)
    try:
        os.makedirs(target_dir, exist_ok=True)
        await asyncio.to_thread(_write_bytes, path, raw)
    except Exception as e:
        logger.warning("persist failed for %s: %s; degrading to text-only", att.filename, e)
        return ""
```

Add the matching `test_sanitize_filename` cases (`".."`, `"foo/.."`) and a containment test (`session_id=".."` stays under root).

### F8 (P1) — REST request-body cap (OOM/DoS)

`validate_base64_size` runs after `request.json()` buffers the whole body. Add a `Content-Length` guard at the top of `chat`/`chat_stream` **before** `request.json()`:

```python
        max_body = int(settings.attachments_max_per_message * 32 * 1024 * 1024 * 1.4) + 1_000_000
        clen = request.headers.get("content-length")
        if clen and clen.isdigit() and int(clen) > max_body:
            return JSONResponse({"error": "Request too large"}, status_code=413)
```

### F9 (P2) — Validate base64 decodes for image/document (avoid opaque 400)

In `validate_attachment`, for `image`/`document`, attempt `base64.b64decode(att.data_base64, validate=True)` and return a user-facing "that file appears corrupted, please resend" on failure (the `text_file` path already guards decode in `build_content_blocks`).

### F10 (P2) — `Attachment.data_base64` must not appear in `repr`

In Task 3, declare the field with `field(repr=False)` (import `field` from dataclasses) so a stray `logger.…(att)` / traceback-locals / `exc_info` never dumps base64:

```python
    data_base64: str = field(repr=False)  # cleared after compaction; never logged
```

(Reorder so non-default fields stay before defaulted ones, or give it a default and keep ordering valid.)

### F11 (P2) — `build_content_blocks` guard: last block is always text

When `text==""` and exactly one image is sent, `cache_control` would attach to the image block (huge, cache-busting). Ensure the final block is always `text`: if no text and the last appended block is media, append `{"type": "text", "text": "(no caption)"}`. (REST substitutes `attachments_default_prompt`, but guard the pure function too.)

### F12 (P2) — Telegram wiring corrections

- Use `self.bot_token` (not `self.token`) in `_download_telegram_file`.
- The bot is a **separate process** with no `Settings`. Thread the flag in: in `telegram_bot.py:main()` construct `Settings()` (or read `NOUS_ATTACHMENTS_ENABLED` from env) and pass `attachments_enabled: bool` + `attachments_default_prompt` into `NousTelegramBot.__init__`; gate `_handle_update` on that instance attribute. Make this a hard step, not "verify if needed."

### F13 (P2) — `ingest_document_text` return shape

The current `ingest_document` tool returns MCP format `{"content": [...]}`. Make the extracted `ingest_document_text(...)` return a plain `{"inserted": N, "source_ref": ..., "episode_id": ...}`; the thin tool wrapper adapts it into the MCP content-block shape so the tool's external contract is unchanged.

### F14 (P2) — Fix Task 9 recall test + task ordering

- `heart.recall(...)` returns `list[RecallResult]` with `.summary`/`.type` (not `.facts`/`.content`). Rewrite the assertion:
  `hits = await heart.recall("attachment dot.png", limit=5)` → `assert any("dot.png" in (r.summary or "") for r in hits if r.type == "fact")`.
- **Merge Task 2 + Task 3** (or do Task 3 first): the pure-function tests can't pass until `Attachment`/`Message` exist. Implement the dataclasses first, then the functions, then run the unit suite once.

**Known-limitation wording (D-P2-3):** the album note must say the *caption is divorced from the other photos* (Telegram puts the caption on the first album item; each photo becomes its own turn). **Vision-model note (D-P1-3):** document that `NOUS_ATTACHMENTS_ENABLED=true` requires a vision-capable `NOUS_MODEL`; a non-vision model returns an opaque error on image turns.

**Confirmed SAFE by review (do not add guards):** `knowledge_extractor._serialize_messages` already skips image/doc blocks; the episode transcript is built from the `user_input` string, not `m.content` — so no base64 reaches episodes/fact-extraction. The `str | list[dict]` union is backward-compatible (positional `Message(role=, content=)` still works).

---

## File Structure

| File | Responsibility | Action |
|------|----------------|--------|
| `nous/api/attachments.py` | Pure functions: sanitize, classify, validate, build blocks, compact-for-history. No I/O. | **Create** |
| `nous/api/attachment_store.py` | Side-effecting: persist bytes to workspace, record Heart fact ref, chunk-ingest text bodies. | **Create** |
| `nous/api/models.py` | `Attachment` dataclass; `Message` gains `content: str \| list[dict]`, `attachments`, `text_content`. | Modify |
| `nous/api/tools.py` | Extract `ingest_document` core into a reusable `ingest_document_text(...)` helper; tool calls it. | Modify |
| `nous/api/compaction.py` | `TokenEstimator` becomes block-aware (`estimate_image_tokens`, `estimate_message`). | Modify |
| `nous/api/runner.py` | `run_turn`/`stream_chat` accept `attachments`; build blocks; post-turn compaction; consumer migration. | Modify |
| `nous/api/rest.py` | `/chat` + `/chat/stream` accept `attachments[]`; relax empty-message guard; validate size from base64. | Modify |
| `nous/telegram_bot.py` | Download photo/document/sticker via `getFile`; thread attachments through `_chat_streaming`. | Modify |
| `nous/config.py` | `NOUS_ATTACHMENTS_*` settings. | Modify |
| `tests/test_attachments.py` | Unit tests for pure functions. | **Create** |
| `tests/test_attachment_store.py` | Tests for persistence + ingest helper. | **Create** |
| `tests/test_attachments_integration.py` | run_turn / REST / compaction-lifecycle integration. | **Create** |

**Conventions to follow** (verified in-repo):
- Config fields use bare annotations with `NOUS_` env prefix — no `validation_alias` (decision 16588dc7 / b3479bd4).
- New DB writes go through existing patterns (`Heart.learn(FactInput(...))`, `chunk_document` + `embed_batch` + `episode_chunks` insert).
- Tests use real Postgres via docker-compose, not mocks, for anything touching Heart/DB. Pure-function tests need no DB.

---

## Task 1: Config flags

**Files:**
- Modify: `nous/config.py` (near `workspace_dir`, line ~430)

- [ ] **Step 1: Add settings**

Add after `workspace_dir`:

```python
    # 011.2 / F024 — Inbound multimodal attachments
    attachments_enabled: bool = False  # master switch; land dark
    attachments_dir: str = ""  # empty => computed as f"{workspace_dir}/attachments"
    attachments_max_per_message: int = 5
    attachments_persist: bool = True  # save originals to disk + record fact reference
    attachments_ingest_text_files: bool = True  # chunk text/code bodies into episode_chunks
    attachments_default_prompt: str = "What can you tell me about this?"
```

- [ ] **Step 2: Add a resolved-dir property**

Add a property on `Settings` (match how `dag_workspace_root` is resolved, config.py ~1014):

```python
    @property
    def attachments_root(self) -> str:
        """Resolved attachments directory (defaults under workspace_dir)."""
        import os
        return self.attachments_dir or os.path.join(self.workspace_dir, "attachments")
```

- [ ] **Step 3: Verify import**

Run: `uv run python -c "from nous.config import Settings; s=Settings(); print(s.attachments_enabled, s.attachments_root)"`
Expected: `False /tmp/nous-workspace/attachments`

- [ ] **Step 4: Commit**

```bash
git add nous/config.py
git commit -m "feat(F024): add NOUS_ATTACHMENTS_* config flags (land dark)"
```

---

## Task 2: Pure attachment functions — `nous/api/attachments.py`

**Files:**
- Create: `nous/api/attachments.py`
- Test: `tests/test_attachments.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_attachments.py
import base64
import pytest

from nous.api.attachments import (
    sanitize_filename, classify_attachment, validate_base64_size,
    validate_attachment, build_content_blocks, compact_message_for_history,
    MAX_IMAGE_SIZE,
)
from nous.api.models import Attachment, Message


def _att(**kw):
    base = dict(filename="f.png", media_type="image/png", data_base64="", size_bytes=0)
    base.update(kw)
    a = Attachment(**{k: v for k, v in base.items() if k in {
        "filename", "media_type", "data_base64", "size_bytes", "source"}})
    a.content_type = classify_attachment(a.filename, a.media_type)
    return a


def test_sanitize_filename_strips_path_and_unsafe():
    assert sanitize_filename("../../etc/passwd") == "passwd"
    assert sanitize_filename("a/b/c.png") == "c.png"
    assert "\x00" not in sanitize_filename("x\x00y.txt")
    assert sanitize_filename("") == "unnamed_file"
    assert len(sanitize_filename("a" * 500 + ".png")) <= 255


def test_classify_attachment():
    assert classify_attachment("x.png", "image/png") == "image"
    assert classify_attachment("x.pdf", "application/pdf") == "document"
    assert classify_attachment("x.py", "text/x-python") == "text_file"
    assert classify_attachment("x.json", "application/octet-stream") == "text_file"
    assert classify_attachment("x.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document") == "unsupported"
    assert classify_attachment("x.mp3", "audio/mpeg") == "unsupported"


def test_validate_base64_size_matches_actual():
    raw = b"hello world" * 1000
    b64 = base64.b64encode(raw).decode()
    assert validate_base64_size(b64) == len(raw)


def test_validate_attachment_rejects_unsupported_and_oversize():
    assert validate_attachment(_att(filename="a.mp3", media_type="audio/mpeg")) is not None
    big = _att(filename="a.png", media_type="image/png", size_bytes=MAX_IMAGE_SIZE + 1)
    assert "too large" in validate_attachment(big)
    ok = _att(filename="a.png", media_type="image/png", size_bytes=1024)
    assert validate_attachment(ok) is None


def test_build_content_blocks_orders_media_then_text():
    img = _att(filename="a.png", media_type="image/png",
               data_base64=base64.b64encode(b"\x89PNG").decode(), size_bytes=4)
    blocks = build_content_blocks("describe this", [img])
    assert blocks[0]["type"] == "image"
    assert blocks[0]["source"]["media_type"] == "image/png"
    assert blocks[-1] == {"type": "text", "text": "describe this"}


def test_build_content_blocks_text_file_decoded_with_header():
    code = base64.b64encode(b"print('hi')").decode()
    tf = _att(filename="s.py", media_type="text/x-python", data_base64=code, size_bytes=11)
    blocks = build_content_blocks("", [tf])
    assert blocks[0]["type"] == "text"
    assert "--- File: s.py ---" in blocks[0]["text"]
    assert "print('hi')" in blocks[0]["text"]


def test_compact_message_replaces_blobs_and_clears_base64():
    img = _att(filename="a.png", media_type="image/png",
               data_base64="QUJD", size_bytes=3)
    img.workspace_path = "/tmp/nous-workspace/attachments/s/abc__a.png"
    msg = Message(role="user", content=build_content_blocks("hi", [img]),
                  attachments=[img], text_content="hi")
    out = compact_message_for_history(msg)
    flat = out.content if isinstance(out.content, str) else " ".join(
        b.get("text", "") for b in out.content)
    assert "QUJD" not in str(out.content)
    assert "abc__a.png" in flat  # actionable ref, not "[Image was sent]"
    assert img.data_base64 == ""  # cleared for GC


def test_compact_message_string_passthrough():
    msg = Message(role="user", content="plain", text_content="plain")
    assert compact_message_for_history(msg).content == "plain"
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_attachments.py -v`
Expected: FAIL — `ModuleNotFoundError: nous.api.attachments` / `Attachment` not in models.

(Task 3 adds `Attachment`/`Message` fields; if running Task 2 first, these fail on import — that's expected. Implement Task 2 and Task 3 together, then run.)

- [ ] **Step 3: Implement the module**

```python
# nous/api/attachments.py
"""Pure functions for inbound multimodal attachment support (F024 / 011.2).

No I/O, no DB. Classification, validation, Claude content-block construction,
and history compaction. Side-effecting persistence lives in attachment_store.py.
"""

from __future__ import annotations

import base64
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


def sanitize_filename(filename: str) -> str:
    """Strip path components, null bytes, unsafe chars; truncate to 255."""
    filename = os.path.basename(filename or "")
    filename = filename.replace("\x00", "")
    filename = _FILENAME_SAFE.sub("_", filename)
    filename = filename[:MAX_FILENAME_LENGTH]
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
        return (
            f"\U0001F4CE I can't process {ext} files yet. I support images "
            f"(JPEG, PNG, GIF, WebP), PDFs, and text/code files."
        )
    limits = {
        "image": MAX_IMAGE_SIZE,
        "document": MAX_DOCUMENT_SIZE,
        "text_file": MAX_TEXT_FILE_SIZE,
    }
    limit = limits.get(attachment.content_type, 0)
    if attachment.size_bytes > limit:
        return (
            f"\U0001F4CE {attachment.filename} is too large "
            f"({attachment.size_bytes / 1024 / 1024:.1f} MB). Max for "
            f"{attachment.content_type} is {limit / 1024 / 1024:.0f} MB."
        )
    return None


def build_content_blocks(text: str, attachments: list["Attachment"]) -> list[dict]:
    """Build Claude content blocks: media first, then user text last."""
    blocks: list[dict] = []
    for att in attachments:
        if att.content_type == "image":
            blocks.append({
                "type": "image",
                "source": {"type": "base64", "media_type": att.media_type,
                           "data": att.data_base64},
            })
        elif att.content_type == "document":
            blocks.append({
                "type": "document",
                "source": {"type": "base64", "media_type": att.media_type,
                           "data": att.data_base64},
            })
        elif att.content_type == "text_file":
            try:
                body = base64.b64decode(att.data_base64).decode("utf-8", errors="replace")
                blocks.append({"type": "text",
                               "text": f"--- File: {att.filename} ---\n{body}"})
            except Exception:
                blocks.append({"type": "text",
                               "text": f"[Could not decode file: {att.filename}]"})
    if text:
        blocks.append({"type": "text", "text": text})
    return blocks


def _ref_label(att: "Attachment") -> str:
    where = f" — saved at {att.workspace_path}" if att.workspace_path else ""
    if att.content_type == "image":
        return f"[Attached image: {att.filename}{where}]"
    if att.content_type == "document":
        return f"[Attached document: {att.filename}{where}]"
    return f"[Attached file: {att.filename}{where}]"


def compact_message_for_history(message: "Message") -> "Message":
    """Swap heavy blocks for an actionable on-disk reference; clear base64.

    MUST be called immediately after the Claude API response, BEFORE
    _save_conversation(). The original file stays on disk (see attachment_store).
    """
    from nous.api.models import Message  # local import avoids cycle

    if isinstance(message.content, str):
        return message

    by_name = {a.filename: a for a in (message.attachments or [])}
    parts: list[dict] = []
    media_idx = 0
    media_atts = [a for a in (message.attachments or [])
                  if a.content_type in ("image", "document")]
    for block in message.content:
        btype = block.get("type")
        if btype == "text":
            parts.append(block)
        elif btype in ("image", "document"):
            att = media_atts[media_idx] if media_idx < len(media_atts) else None
            media_idx += 1
            label = _ref_label(att) if att else f"[Attached {btype} was analyzed]"
            parts.append({"type": "text", "text": label})

    content: str | list[dict]
    if len(parts) == 1 and parts[0].get("type") == "text":
        content = parts[0]["text"]
    else:
        content = parts

    for att in (message.attachments or []):
        att.data_base64 = ""  # allow GC; original is on disk

    _ = by_name  # reserved for future per-name mapping
    return Message(role=message.role, content=content,
                   attachments=message.attachments,
                   text_content=message.text_content)
```

- [ ] **Step 4: Run (after Task 3) to verify pass**

Run: `uv run pytest tests/test_attachments.py -v`
Expected: PASS (all 8 tests).

- [ ] **Step 5: Commit**

```bash
git add nous/api/attachments.py tests/test_attachments.py
git commit -m "feat(F024): pure attachment functions (classify/validate/build/compact)"
```

---

## Task 3: Data model — `Attachment` + `Message` migration

**Files:**
- Modify: `nous/api/models.py:14-19`

- [ ] **Step 1: Replace the `Message` dataclass and add `Attachment`**

```python
@dataclass
class Attachment:
    """A file attachment accompanying a message (F024)."""

    filename: str
    media_type: str
    data_base64: str          # cleared after compaction
    size_bytes: int
    source: str = "upload"    # "telegram" | "rest" | "url"
    content_type: str = "unknown"  # image | document | text_file | unsupported
    workspace_path: str = ""  # on-disk location after persistence (Task 5)


@dataclass
class Message:
    """A single message in a conversation."""

    role: str  # "user" or "assistant"
    content: str | list[dict[str, Any]]  # str for text-only, list for multimodal
    attachments: list[Attachment] | None = None  # metadata only (no base64 post-compaction)
    text_content: str = ""  # plain-text portion (episodes/search/cognitive layer)
```

- [ ] **Step 2: Verify import + backward compat**

Run: `uv run python -c "from nous.api.models import Message, Attachment; m=Message(role='user', content='hi'); print(m.content, m.text_content, m.attachments)"`
Expected: `hi  None` (existing positional `Message(role=..., content=...)` still works).

- [ ] **Step 3: Run the attachments unit tests (now importable)**

Run: `uv run pytest tests/test_attachments.py -v`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add nous/api/models.py
git commit -m "feat(F024): Message.content union + Attachment dataclass"
```

---

## Task 4: Block-aware `TokenEstimator`

**Files:**
- Modify: `nous/api/compaction.py:441-449`
- Test: `tests/test_attachments_integration.py` (token section)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_attachments_integration.py (token-estimation section)
from nous.api.compaction import TokenEstimator


def test_estimate_message_does_not_inflate_on_image():
    est = TokenEstimator()
    big_b64 = "A" * 200_000  # ~150KB base64 blob
    img_msg = {"role": "user", "content": [
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": big_b64}},
        {"type": "text", "text": "what is this?"},
    ]}
    # naive stringification would be ~50K tokens; block-aware must be small
    assert est.estimate_message(img_msg) < 3000


def test_estimate_message_text_passthrough():
    est = TokenEstimator()
    assert est.estimate_message({"role": "user", "content": "hello"}) >= 1
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_attachments_integration.py -k estimate_message -v`
Expected: FAIL — `AttributeError: 'TokenEstimator' object has no attribute 'estimate_message'`.

- [ ] **Step 3: Implement**

Add to `TokenEstimator` (after `estimate`, compaction.py:445):

```python
    def estimate_image_tokens(self, width: int = 0, height: int = 0) -> int:
        """Claude's image token formula: (w*h)/750; conservative default if unknown."""
        if width > 0 and height > 0:
            return max(1, (width * height) // 750)
        return 1600

    def estimate_message(self, message: dict[str, Any]) -> int:
        """Per-message estimate; block-aware for multimodal content."""
        content = message.get("content", "")
        if isinstance(content, str):
            return self.estimate(content) + 4
        total = 4
        for block in content:
            btype = block.get("type") if isinstance(block, dict) else None
            if btype == "text":
                total += self.estimate(block.get("text", ""))
            elif btype == "image":
                total += self.estimate_image_tokens()
            elif btype == "document":
                total += 7500  # ~5 pages @ ~1500 tok/page
        return total
```

Update `estimate_messages` to delegate (replaces the str-only sum at compaction.py:447-449):

```python
    def estimate_messages(self, messages: list[dict[str, Any]]) -> int:
        """Estimate total tokens for a message list (multimodal-aware)."""
        return sum(self.estimate_message(m) for m in messages)
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_attachments_integration.py -k estimate_message -v`
Expected: PASS.

- [ ] **Step 5: Regression — existing compaction tests still pass**

Run: `uv run pytest tests/test_compaction.py -v`
Expected: PASS (no behavior change for str content).

- [ ] **Step 6: Commit**

```bash
git add nous/api/compaction.py tests/test_attachments_integration.py
git commit -m "feat(F024): block-aware TokenEstimator (no false compaction on images)"
```

---

## Task 5: Persistence + memory reference — `nous/api/attachment_store.py`

**Files:**
- Create: `nous/api/attachment_store.py`
- Modify: `nous/api/tools.py` (extract `ingest_document_text` core)
- Test: `tests/test_attachment_store.py`

- [ ] **Step 1: Extract a reusable doc-ingest helper in `tools.py`**

Find the body of `ingest_document` (tools.py ~1230-1370: chunk → embed_batch → advisory-lock → insert into `episode_chunks`). Lift the chunk/embed/insert core into a module-level coroutine and have the tool closure call it. Signature:

```python
# nous/api/tools.py — module level (near other helpers)
async def ingest_document_text(
    heart: "Heart",
    settings: "Settings",
    *,
    content: str,
    source_ref: str,
    session_id: str | None = None,
    episode_id: str | None = None,
) -> dict[str, Any]:
    """Chunk + embed + persist document text to heart.episode_chunks.

    Extracted from the ingest_document tool so the attachment pipeline can call it
    directly. Returns {"inserted": N, "source_ref": ..., "episode_id": ...} or
    {"error": ...}. Honors settings.document_ingest_enabled / chunk sizes.
    """
    # (body relocated verbatim from the existing tool, with `content`/`source_ref`/
    #  `episode_id`/`session_id` as params instead of closure-captured tool args)
```

Then the existing `ingest_document` tool becomes a thin wrapper:

```python
    async def ingest_document(content: str, source_ref: str,
                              episode_id: str | None = None,
                              _session_id: str | None = None) -> dict[str, Any]:
        return await ingest_document_text(
            heart, settings, content=content, source_ref=source_ref,
            session_id=_session_id, episode_id=episode_id)
```

- [ ] **Step 2: Run existing ingest tests to confirm the refactor is byte-equivalent**

Run: `uv run pytest tests/ -k ingest_document -v`
Expected: PASS (behavior unchanged; pure extraction).

- [ ] **Step 3: Write the failing persistence tests**

```python
# tests/test_attachment_store.py
import base64
import os
import pytest

from nous.api.attachments import classify_attachment
from nous.api.attachment_store import persist_attachment, ATTACHMENT_PATH_PREFIX
from nous.api.models import Attachment


def _img(tmp_b64=b"\x89PNG\r\n\x1a\n"):
    a = Attachment(filename="shot.png", media_type="image/png",
                   data_base64=base64.b64encode(tmp_b64).decode(),
                   size_bytes=len(tmp_b64), source="telegram")
    a.content_type = classify_attachment(a.filename, a.media_type)
    return a


@pytest.mark.asyncio
async def test_persist_writes_file_under_attachments_root(tmp_path, monkeypatch):
    from nous.config import settings as global_settings
    monkeypatch.setattr(global_settings, "workspace_dir", str(tmp_path))
    att = _img()
    path = await persist_attachment(att, session_id="sess-1", settings=global_settings)
    assert os.path.isfile(path)
    assert ATTACHMENT_PATH_PREFIX in path  # under <workspace>/attachments/
    assert att.workspace_path == path
    with open(path, "rb") as f:
        assert f.read() == base64.b64decode(att.data_base64)


@pytest.mark.asyncio
async def test_persist_disabled_returns_empty(tmp_path, monkeypatch):
    from nous.config import settings as global_settings
    monkeypatch.setattr(global_settings, "workspace_dir", str(tmp_path))
    monkeypatch.setattr(global_settings, "attachments_persist", False)
    att = _img()
    path = await persist_attachment(att, session_id="s", settings=global_settings)
    assert path == ""
    assert att.workspace_path == ""
```

(A Heart-backed test for `record_attachment_fact` + `maybe_ingest_text_file` belongs in the integration suite, Task 9, since it needs a live DB.)

- [ ] **Step 4: Run to verify failure**

Run: `uv run pytest tests/test_attachment_store.py -v`
Expected: FAIL — `ModuleNotFoundError: nous.api.attachment_store`.

- [ ] **Step 5: Implement the store**

```python
# nous/api/attachment_store.py
"""Side-effecting persistence for inbound attachments (F024).

Saves originals under <workspace>/attachments/<session>/, records a Heart fact
memory-reference, and chunk-ingests text/code bodies into episode_chunks. Base64
never reaches the DB — only the on-disk path and metadata are persisted.
"""

from __future__ import annotations

import base64
import logging
import os
import uuid
from typing import TYPE_CHECKING

from nous.api.attachments import sanitize_filename

if TYPE_CHECKING:
    from nous.api.models import Attachment
    from nous.config import Settings
    from nous.heart.heart import Heart

logger = logging.getLogger(__name__)

ATTACHMENT_PATH_PREFIX = os.path.join("attachments", "")  # ".../attachments/"


async def persist_attachment(att: "Attachment", *, session_id: str,
                             settings: "Settings") -> str:
    """Write the original bytes to <attachments_root>/<session>/<uuid>__<file>.

    Returns the absolute path (also stored on att.workspace_path), or "" if
    persistence is disabled or the attachment carries no data.
    """
    if not settings.attachments_persist or not att.data_base64:
        return ""
    safe_session = sanitize_filename(session_id) or "session"
    target_dir = os.path.join(settings.attachments_root, safe_session)
    os.makedirs(target_dir, exist_ok=True)
    fname = f"{uuid.uuid4().hex}__{sanitize_filename(att.filename)}"
    path = os.path.join(target_dir, fname)
    raw = base64.b64decode(att.data_base64)

    import asyncio
    await asyncio.to_thread(_write_bytes, path, raw)
    att.workspace_path = path
    logger.info("Persisted attachment %s (%s, %d bytes) -> %s",
                att.filename, att.content_type, att.size_bytes, path)
    return path


def _write_bytes(path: str, raw: bytes) -> None:
    with open(path, "wb") as f:
        f.write(raw)


async def record_attachment_fact(heart: "Heart", att: "Attachment", *,
                                 agent_id: str, source_episode_id: str | None,
                                 session: object | None = None) -> None:
    """Store a Heart fact so the saved file is discoverable via recall."""
    from uuid import UUID
    from nous.heart.schemas import FactInput

    where = f" saved at {att.workspace_path}" if att.workspace_path else ""
    content = (f"User shared a {att.content_type} file '{att.filename}' "
               f"({att.size_bytes // 1024} KB) via {att.source}.{where}")
    try:
        await heart.learn(
            FactInput(
                content=content,
                category="attachment",
                subject=att.filename,
                source=f"{att.source}-attachment",
                source_text=att.workspace_path or None,
                source_episode_id=UUID(source_episode_id) if source_episode_id else None,
                tags=["attachment", att.content_type],
            ),
            session=session,
        )
    except Exception as e:  # never let a memory write break the turn
        logger.warning("Failed to record attachment fact for %s: %s", att.filename, e)


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
        await ingest_document_text(
            heart, settings, content=body,
            source_ref=att.workspace_path or att.filename,
            session_id=session_id, episode_id=episode_id)
    except Exception as e:
        logger.warning("Text-file ingest failed for %s: %s", att.filename, e)
```

- [ ] **Step 6: Run to verify pass**

Run: `uv run pytest tests/test_attachment_store.py -v`
Expected: PASS (2 tests).

- [ ] **Step 7: Commit**

```bash
git add nous/api/attachment_store.py tests/test_attachment_store.py nous/api/tools.py
git commit -m "feat(F024): attachment persistence + fact reference + text-file ingest helper"
```

---

## Task 6: Runner wiring — `run_turn` / `stream_chat`

**Files:**
- Modify: `nous/api/runner.py` (run_turn ~312-401, stream_chat ~911-961, consumers at 386-388 & 949)
- Test: `tests/test_attachments_integration.py`

- [ ] **Step 1: Write the failing integration test (needs DB + a fake API)**

```python
# tests/test_attachments_integration.py (runner section)
import base64
import pytest

from nous.api.models import Attachment
from nous.api.attachments import classify_attachment


def _png_att():
    raw = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR4nGNgYAAAAAMAASsJTYQAAAAASUVORK5CYII=")
    a = Attachment(filename="dot.png", media_type="image/png",
                   data_base64=base64.b64encode(raw).decode(), size_bytes=len(raw),
                   source="rest")
    a.content_type = classify_attachment(a.filename, a.media_type)
    return a


@pytest.mark.asyncio
async def test_run_turn_with_image_sends_blocks_and_compacts(runner_with_fake_api):
    """run_turn builds image+text blocks for the API call, then the stored
    user message contains NO base64 and an actionable reference instead."""
    runner, captured = runner_with_fake_api
    resp, ctx, usage = await runner.run_turn(
        "sess-img", "what is this?", attachments=[_png_att()])
    # 1. The API saw a list with an image block
    sent_user = [m for m in captured["messages"] if m["role"] == "user"][-1]
    assert isinstance(sent_user["content"], list)
    assert any(b.get("type") == "image" for b in sent_user["content"])
    # 2. History is compacted: no base64, reference present
    conv = await runner._get_or_create_conversation("sess-img")
    stored = [m for m in conv.messages if m.role == "user"][-1]
    assert "iVBOR" not in str(stored.content)
    assert "dot.png" in (stored.content if isinstance(stored.content, str)
                          else str(stored.content))
```

> The `runner_with_fake_api` fixture stubs `runner._api.call(...)`/`call_streaming_aggregated` to capture the payload and return a canned assistant text. Add it to `tests/conftest.py` mirroring existing runner fixtures (see `tests/test_runner*.py` for the established Heart/Brain/DB wiring). The fixture must enable `settings.attachments_enabled = True`.

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_attachments_integration.py -k run_turn_with_image -v`
Expected: FAIL — `run_turn() got an unexpected keyword argument 'attachments'`.

- [ ] **Step 3: Add `attachments` param to `run_turn`**

At the `run_turn` signature (runner.py:312), add after `user_message`:

```python
        attachments: list[Attachment] | None = None,
```

Import at top of runner.py: `from nous.api.models import Attachment` (extend the existing models import), and:

```python
from nous.api.attachments import (
    build_content_blocks, compact_message_for_history, validate_attachment,
)
from nous.api import attachment_store
```

- [ ] **Step 4: Build the user message with attachments (replace runner.py:400-401)**

```python
            # 3. Append user message (F024: multimodal when attachments present)
            valid_attachments: list[Attachment] = []
            if attachments and self._settings.attachments_enabled:
                warnings: list[str] = []
                for att in attachments[: self._settings.attachments_max_per_message]:
                    err = validate_attachment(att)
                    (warnings.append(err) if err else valid_attachments.append(att))
                # Persist + record references BEFORE the API call (full-fidelity capture)
                for att in valid_attachments:
                    await attachment_store.persist_attachment(
                        att, session_id=session_id, settings=self._settings)
                if valid_attachments:
                    user_msg = Message(
                        role="user",
                        content=build_content_blocks(user_message, valid_attachments),
                        attachments=valid_attachments,
                        text_content=user_message,
                    )
                else:
                    user_msg = Message(role="user", content=user_message,
                                       text_content=user_message)
                if warnings:
                    note = "\n".join(warnings)
                    user_message = f"{note}\n\n{user_message}".strip()
            else:
                user_msg = Message(role="user", content=user_message,
                                   text_content=user_message)
            conversation.messages.append(user_msg)
```

- [ ] **Step 5: Compact + record memory AFTER the API response**

Locate where the assistant response is appended on the success path (runner.py:522/528). Immediately BEFORE appending the assistant message (so the user message is the last element), insert:

```python
                # F024: strip base64 from history (file is on disk), record memory
                if valid_attachments:
                    idx = len(conversation.messages) - 1  # the user msg we appended
                    conversation.messages[idx] = compact_message_for_history(
                        conversation.messages[idx])
                    for att in valid_attachments:
                        await attachment_store.record_attachment_fact(
                            self._heart, att, agent_id=_agent_id,
                            source_episode_id=turn_context.episode_id)
                        await attachment_store.maybe_ingest_text_file(
                            self._heart, self._settings, att,
                            session_id=session_id,
                            episode_id=turn_context.episode_id)
```

> Verify `turn_context.episode_id` is the attribute name (grep `episode_id` on `TurnContext` in `nous/cognitive/schemas.py`). Verify `self._heart` is the runner's Heart handle (grep `self._heart` in runner.py). Adjust names if they differ — do not invent.

- [ ] **Step 6: Migrate `Message.content` consumers**

`recent_messages` (runner.py:386-388 and the stream_chat copy ~949):

```python
            recent_messages = [
                (m.text_content or (m.content if isinstance(m.content, str) else ""))
                for m in conversation.messages if m.role == "user"
            ][-8:]
```

`_format_history_text` (grep its definition ~1321) — make it tolerate list content:

```python
        text = (m.text_content if getattr(m, "text_content", "") else
                (m.content if isinstance(m.content, str) else "[multimodal message]"))
```

- [ ] **Step 7: Mirror Steps 3-6 into `stream_chat`**

Add the same `attachments` param (runner.py:911), the same build/persist block (before the user append at ~961), and the same compact+record step before the assistant append (runner.py:1368/1374). The `_build_api_payload`/`_call_api_stream` path already passes list content through (verified runner.py:699-704) — no streaming-specific change.

- [ ] **Step 8: Run to verify pass**

Run: `uv run pytest tests/test_attachments_integration.py -k "run_turn_with_image or estimate_message" -v`
Expected: PASS.

- [ ] **Step 9: Full runner regression (text-only path unchanged)**

Run: `uv run pytest tests/test_runner.py tests/test_runner_streaming.py -v`
Expected: PASS (existing positional `Message(role=, content=)` and text turns behave identically).

- [ ] **Step 10: Commit**

```bash
git add nous/api/runner.py tests/test_attachments_integration.py tests/conftest.py
git commit -m "feat(F024): runner builds multimodal blocks, persists + compacts post-turn"
```

---

## Task 7: REST endpoints

**Files:**
- Modify: `nous/api/rest.py` (`chat` 82-104, `chat_stream` 134-155)
- Test: `tests/test_attachments_integration.py` (REST section)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_attachments_integration.py (REST section)
import base64
from nous.api.attachments import validate_base64_size


def test_validate_base64_size_used_not_client_size():
    raw = b"x" * 5000
    declared_wrong = 999_999
    assert validate_base64_size(base64.b64encode(raw).decode()) == 5000 != declared_wrong
```

> A full Starlette TestClient round-trip belongs here too if the suite already has REST fixtures (grep `TestClient` in tests/). If so, assert: POST `/chat` with an `attachments` array and empty `message` returns 200 and the runner received one classified attachment.

- [ ] **Step 2: Implement — shared parse helper at top of `create_app` scope in rest.py**

```python
    def _parse_attachments(body: dict) -> list[Attachment]:
        from nous.api.attachments import (
            sanitize_filename, classify_attachment, validate_base64_size)
        out: list[Attachment] = []
        for a in (body.get("attachments") or [])[: settings.attachments_max_per_message]:
            data_b64 = a.get("data_base64", "")
            filename = sanitize_filename(a.get("filename", "unnamed"))
            media_type = a.get("media_type", "application/octet-stream")
            att = Attachment(
                filename=filename, media_type=media_type, data_base64=data_b64,
                size_bytes=validate_base64_size(data_b64),  # actual, not client-declared
                source="rest",
                content_type=classify_attachment(filename, media_type),
            )
            out.append(att)
        return out
```

Add `from nous.api.models import Attachment` to rest.py imports.

- [ ] **Step 3: Relax the empty-message guard + pass attachments (`chat`, rest.py:89-104)**

```python
        message = body.get("message")
        attachments = _parse_attachments(body) if settings.attachments_enabled else []
        if not message and not attachments:
            return JSONResponse({"error": "Missing required field: message"}, status_code=400)
        if not message:
            message = settings.attachments_default_prompt
        ...
            response_text, turn_context, usage = await runner.run_turn(
                session_id, message, attachments=attachments or None,
                platform=platform, user_id=user_id,
                user_display_name=user_display_name,
            )
```

- [ ] **Step 4: Same for `chat_stream` (rest.py:141-155)**

```python
        message = body.get("message")
        attachments = _parse_attachments(body) if settings.attachments_enabled else []
        if not message and not attachments:
            return JSONResponse({"error": "Missing required field: message"}, status_code=400)
        if not message:
            message = settings.attachments_default_prompt
        ...
            stream = runner.stream_chat(
                session_id, message, attachments=attachments or None,
                platform=platform, user_id=user_id,
                user_display_name=user_display_name,
            )
```

- [ ] **Step 5: Run**

Run: `uv run pytest tests/test_attachments_integration.py -k "base64_size or rest" -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add nous/api/rest.py tests/test_attachments_integration.py
git commit -m "feat(F024): REST /chat[/stream] accept attachments[] (server-validated size)"
```

---

## Task 8: Telegram bot — download + thread attachments (raw httpx)

**Files:**
- Modify: `nous/telegram_bot.py` (`_handle_update` 507-557, `_chat_streaming` 673-696, add download helpers, imports)

- [ ] **Step 1: Add imports + a file-download helper**

At the top of telegram_bot.py add `import base64`, `import mimetypes` (if absent), and `from nous.api.models import Attachment`. Add a method on the bot class:

```python
    async def _download_telegram_file(self, file_id: str) -> bytes:
        """Resolve file_id via getFile, then GET the file-download URL.

        Telegram Bot API: files are downloadable for ~1h, max 20 MB.
        """
        info = await self._tg("getFile", params={"file_id": file_id})
        file_path = info.get("file_path")
        if not file_path:
            raise ValueError(f"getFile returned no file_path for {file_id}")
        url = f"https://api.telegram.org/file/bot{self.token}/{file_path}"
        resp = await self._http.get(url, timeout=60)
        resp.raise_for_status()
        data = resp.content
        if not data:
            raise ValueError(f"Empty download for file_id={file_id}")
        return data
```

> Verify the bot's token attribute name (grep `self.token`/`bot_token` in telegram_bot.py) and that `self._tg` returns the parsed `result` dict. Adjust if the attribute differs.

- [ ] **Step 2: Extract attachments in `_handle_update`**

Replace the text-only short-circuit (telegram_bot.py:515-518) so photos/documents/stickers are handled. After the access-control check and command handlers, before the final `_chat_streaming` call (line 555-557):

```python
        text = (message.get("text") or message.get("caption") or "").strip()
        attachments: list[Attachment] = []

        if self.settings.attachments_enabled:
            try:
                photos = message.get("photo") or []
                if photos:
                    p = photos[-1]  # largest size
                    raw = await self._download_telegram_file(p["file_id"])
                    att = Attachment(
                        filename=f"photo_{p['file_unique_id']}.jpg",
                        media_type="image/jpeg",
                        data_base64=base64.b64encode(raw).decode(),
                        size_bytes=len(raw), source="telegram",
                        content_type="image")
                    attachments.append(att)

                doc = message.get("document")
                if doc:
                    raw = await self._download_telegram_file(doc["file_id"])
                    mime = (doc.get("mime_type")
                            or mimetypes.guess_type(doc.get("file_name", ""))[0]
                            or "application/octet-stream")
                    from nous.api.attachments import classify_attachment, sanitize_filename
                    fname = sanitize_filename(doc.get("file_name") or "document")
                    att = Attachment(
                        filename=fname, media_type=mime,
                        data_base64=base64.b64encode(raw).decode(),
                        size_bytes=len(raw), source="telegram",
                        content_type=classify_attachment(fname, mime))
                    attachments.append(att)

                sticker = message.get("sticker")
                if sticker and not sticker.get("is_animated") and not sticker.get("is_video"):
                    raw = await self._download_telegram_file(sticker["file_id"])
                    attachments.append(Attachment(
                        filename=f"sticker_{sticker['file_unique_id']}.webp",
                        media_type="image/webp",
                        data_base64=base64.b64encode(raw).decode(),
                        size_bytes=len(raw), source="telegram",
                        content_type="image"))

                if (message.get("voice") or message.get("audio")) and not attachments:
                    await self._send(chat_id, "\U0001F3A4 I can't process audio yet. "
                                              "Send text, images, PDFs, or code files.")
                    if not text:
                        return
            except Exception as e:
                logger.error("Attachment download failed: %s", e, exc_info=True)
                if not text:
                    await self._send(chat_id, "⚠️ I couldn't download that file. "
                                              "Please try again.")
                    return
                attachments = []  # continue text-only

        if not text and not attachments:
            return

        user_display_name = message.get("from", {}).get("first_name")
        await self._chat_streaming(
            chat_id, text, user_id=str(user_id) if user_id else None,
            user_display_name=user_display_name, attachments=attachments or None)
```

> Verify the bot holds a `self.settings` handle (grep `self.settings`/`Settings` in telegram_bot.py). If not, thread it in via `__init__` (small change) — the bot is constructed in `main.py`.

- [ ] **Step 3: Thread attachments through `_chat_streaming` (telegram_bot.py:673-696)**

Add the param and payload field:

```python
    async def _chat_streaming(
        self, chat_id: int, text: str,
        user_id: str | None = None, user_display_name: str | None = None,
        attachments: list[Attachment] | None = None,
    ) -> None:
        ...
        payload: dict[str, Any] = {"message": text, "session_id": session_id, "platform": "telegram"}
        if user_id:
            payload["user_id"] = user_id
        if user_display_name:
            payload["user_display_name"] = user_display_name
        if attachments:
            payload["attachments"] = [
                {"filename": a.filename, "media_type": a.media_type,
                 "data_base64": a.data_base64, "size_bytes": a.size_bytes}
                for a in attachments
            ]
```

> `_chat_streaming` currently early-returns if `text` is empty? It does not (it always builds a payload). But the REST default-prompt (Task 7) covers the no-caption case server-side — the bot may send `text=""` and the server substitutes `attachments_default_prompt`. Keep `text` as-is.

- [ ] **Step 4: Manual smoke (documented, not automated)**

The bot path has no unit-test harness in-repo (it's an out-of-process poller). Validate manually per Task 9's checklist.

- [ ] **Step 5: Commit**

```bash
git add nous/telegram_bot.py
git commit -m "feat(F024): Telegram bot downloads photos/docs/stickers, threads to /chat/stream"
```

---

## Task 9: End-to-end validation + docs

**Files:**
- Test: `tests/test_attachments_integration.py` (DB-backed cases)
- Modify: `CLAUDE.md` (env-var table + shipped row), `docs/features/INDEX.md`

- [ ] **Step 1: DB-backed integration tests**

```python
@pytest.mark.asyncio
async def test_attachment_fact_recorded(runner_with_fake_api, heart):
    """After an image turn, a discoverable 'attachment' fact references the path."""
    runner, _ = runner_with_fake_api
    await runner.run_turn("sess-fact", "what's this?", attachments=[_png_att()])
    hits = await heart.recall("attachment dot.png", limit=5)
    assert any("dot.png" in (f.content or "") for f in hits.facts)


@pytest.mark.asyncio
async def test_text_file_ingested_to_chunks(runner_with_fake_api, db_session):
    """A .py attachment's body is chunk-ingested with source_kind='document'."""
    runner, _ = runner_with_fake_api
    code = base64.b64encode(b"def f():\n    return 42\n" * 50).decode()
    att = Attachment(filename="m.py", media_type="text/x-python",
                     data_base64=code, size_bytes=len(code), source="rest",
                     content_type="text_file")
    await runner.run_turn("sess-code", "review", attachments=[att])
    # at least one document chunk exists for this source
    from sqlalchemy import text as sql
    rows = await db_session.execute(sql(
        "SELECT count(*) FROM heart.episode_chunks WHERE source_kind='document'"))
    assert rows.scalar() >= 1


@pytest.mark.asyncio
async def test_history_has_no_base64_after_turn(runner_with_fake_api):
    runner, _ = runner_with_fake_api
    await runner.run_turn("sess-clean", "hi", attachments=[_png_att()])
    conv = await runner._get_or_create_conversation("sess-clean")
    assert "iVBOR" not in str([m.content for m in conv.messages])
```

> Reuse the existing `heart` / `db_session` fixtures (grep tests/conftest.py). Adjust `heart.recall(...)` to the real signature (grep `async def recall` in `nous/heart/heart.py`).

- [ ] **Step 2: Run the full new suite**

Run: `uv run pytest tests/test_attachments.py tests/test_attachment_store.py tests/test_attachments_integration.py -v`
Expected: PASS.

- [ ] **Step 3: Manual Telegram validation (with `NOUS_ATTACHMENTS_ENABLED=true`)**

- Send a photo → Nous describes it.
- Send a photo with caption → uses both.
- Send a PDF → Nous summarizes it; file appears under `/tmp/nous-workspace/attachments/<session>/`.
- Send a `.py` file → Nous reviews it; a document chunk is searchable via `recall_deep`.
- Send an `.mp3` → helpful "can't process audio" reply.
- Follow-up "what was in that file?" in the same session → recall surfaces the attachment fact / saved path.
- Confirm logs never contain base64 (grep the log for `data_base64`/long base64 runs).

- [ ] **Step 4: Docs — CLAUDE.md**

Add a shipped-table row and the env-var rows:

```markdown
| F024 | Inbound Multimodal Attachments (Telegram + REST accept images/PDFs/text files → Claude content blocks; originals saved to workspace_dir/attachments with a Heart fact memory-reference; text-file bodies chunk-ingested to episode_chunks; base64 stripped from history post-turn; block-aware token estimation; gated by NOUS_ATTACHMENTS_ENABLED default OFF) | — |
```

| Variable | Default | Description |
|----------|---------|-------------|
| `NOUS_ATTACHMENTS_ENABLED` | `false` | F024 master switch for inbound image/PDF/text-file attachments. |
| `NOUS_ATTACHMENTS_DIR` | *(empty → `<workspace>/attachments`)* | F024 root for saved originals. |
| `NOUS_ATTACHMENTS_MAX_PER_MESSAGE` | `5` | F024 cap on attachments per message. |
| `NOUS_ATTACHMENTS_PERSIST` | `true` | F024 save originals to disk + record a Heart fact reference. |
| `NOUS_ATTACHMENTS_INGEST_TEXT_FILES` | `true` | F024 chunk text/code bodies into episode_chunks. |
| `NOUS_ATTACHMENTS_DEFAULT_PROMPT` | `What can you tell me about this?` | F024 prompt used when an attachment arrives with no caption. |

- [ ] **Step 5: Docs — INDEX.md**

Flip the F024 / 011.2 row from `📋 Draft` to shipped with the PR number once merged.

- [ ] **Step 6: Commit**

```bash
git add tests/test_attachments_integration.py CLAUDE.md docs/features/INDEX.md
git commit -m "test(F024): DB-backed integration + docs for inbound attachments"
```

---

## Self-Review (completed during authoring)

**Spec coverage:** images ✅ (Task 2/6), PDF ✅ (analyze in-turn; body-ingest deliberately deferred — flagged), text/code ✅ (Task 2 + Task 5 ingest), filename sanitization ✅ (Task 2), server-side size validation ✅ (Task 2/7), blob lifecycle ✅ (Task 6 post-turn compaction), token estimation ✅ (Task 4), Telegram ✅ (Task 8, rewritten to raw httpx), REST ✅ (Task 7), backward compat ✅ (Task 3 + Task 6 Step 9). **Added beyond spec:** workspace persistence + fact reference + text-file recall ingest (Task 5), feature flag (Task 1).

**Placeholders:** none — every code step shows real code. Items marked "verify" are signature-confirmation steps, not missing content; they name the exact grep to run.

**Type consistency:** `Attachment` fields (`content_type`, `workspace_path`) are used identically across attachments.py, attachment_store.py, runner.py, rest.py, telegram_bot.py. `Message(role, content, attachments, text_content)` consistent. `ingest_document_text(heart, settings, *, content, source_ref, session_id, episode_id)` called identically in tools.py wrapper and attachment_store.

**Open verification points for the implementer (do not invent — grep and confirm):**
1. `TurnContext.episode_id` attribute name (`nous/cognitive/schemas.py`).
2. Runner's Heart handle (`self._heart` vs other) and Settings handle (`self._settings`).
3. Telegram bot's token + settings attribute names; `self._tg` return shape.
4. `Heart.recall(...)` return type/fields for the integration assertions.
5. Existing runner/Heart/DB test fixtures in `tests/conftest.py` to model `runner_with_fake_api`.
