# Issue #220: Telegram File Delivery — Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `send_file` agent tool that delivers generated files (images, documents) to Telegram via sendPhoto/sendDocument API.

**Architecture:** New module `nous/api/telegram_tools.py` containing the `send_file` tool closure and registration function. Follows the established `register_*_tools` pattern. Tool auto-detects file type by extension (images → sendPhoto, everything else → sendDocument), validates size limits (50MB max, 10MB photo threshold), uploads via multipart/form-data, and cleans up temp files after send. Wired in `main.py` conditionally when `telegram_bot_token` is configured.

**Tech Stack:** Python 3.12+, httpx (multipart upload), Telegram Bot API, pytest + pytest-asyncio

**Scope:** Phase 1 only — explicit tool calls. No auto-attach (Phase 2) or inline media (Phase 3).

---

## File Structure

| Action | File | Responsibility |
|--------|------|----------------|
| Create | `nous/api/telegram_tools.py` | `send_file` tool closure, Telegram upload helpers, schema, `register_telegram_tools()` |
| Modify | `nous/api/runner.py:50-57` | Add `send_file` to FRAME_TOOLS for task, conversation, debug frames |
| Modify | `nous/main.py:311-355` | Wire `register_telegram_tools()` after tool registration block, gated on token |
| Modify | `nous/telegram_bot.py:209-219` | Add `send_file` to StreamingMessage.TOOL_INDICATORS |
| Create | `tests/test_telegram_tools.py` | Unit tests for send_file tool (mocked httpx + filesystem) |

---

### Task 1: Write send_file tool tests

**Files:**
- Create: `tests/test_telegram_tools.py`

- [ ] **Step 1: Write test file with all test cases**

```python
"""Tests for Telegram file delivery tool (Issue #220 Phase 1)."""

from __future__ import annotations

import os
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture
def tmp_png(tmp_path):
    """Create a temporary PNG file."""
    f = tmp_path / "chart.png"
    f.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)
    return str(f)


@pytest.fixture
def tmp_docx(tmp_path):
    """Create a temporary DOCX file."""
    f = tmp_path / "report.docx"
    f.write_bytes(b"PK" + b"\x00" * 100)
    return str(f)


@pytest.fixture
def tmp_svg(tmp_path):
    """Create a temporary SVG file."""
    f = tmp_path / "diagram.svg"
    f.write_text("<svg></svg>")
    return str(f)


@pytest.fixture
def mock_settings():
    """Create mock settings with Telegram config."""
    from unittest.mock import MagicMock
    s = MagicMock()
    s.telegram_bot_token = "test-bot-token"
    s.telegram_chat_id = "12345"
    return s


@pytest.fixture
def mock_http():
    """Create mock httpx client."""
    return AsyncMock()


def _ok_response():
    """Telegram API success response (httpx.Response.json() is sync)."""
    mock = MagicMock()
    mock.json.return_value = {"ok": True, "result": {"message_id": 42}}
    mock.status_code = 200
    return mock


# --- File type detection ---

def test_detect_photo_extensions():
    from nous.api.telegram_tools import _detect_send_method
    for ext in ("png", "jpg", "jpeg", "gif", "webp"):
        assert _detect_send_method(f"file.{ext}") == "sendPhoto"


def test_detect_document_extensions():
    from nous.api.telegram_tools import _detect_send_method
    for ext in ("docx", "pdf", "xlsx", "pptx", "csv", "txt", "svg", "zip"):
        assert _detect_send_method(f"file.{ext}") == "sendDocument"


def test_detect_no_extension_is_document():
    from nous.api.telegram_tools import _detect_send_method
    assert _detect_send_method("Makefile") == "sendDocument"


# --- Size validation ---

def test_validate_size_ok(tmp_png):
    from nous.api.telegram_tools import _validate_file_size
    ok, msg = _validate_file_size(tmp_png)
    assert ok is True
    assert msg == ""


def test_validate_size_too_large(tmp_path):
    from nous.api.telegram_tools import _validate_file_size
    big = tmp_path / "huge.bin"
    big.write_bytes(b"\x00" * (50 * 1024 * 1024 + 1))
    ok, msg = _validate_file_size(str(big))
    assert ok is False
    assert "50MB" in msg


def test_validate_size_photo_downgrade(tmp_path):
    from nous.api.telegram_tools import _validate_file_size
    big_img = tmp_path / "big.png"
    big_img.write_bytes(b"\x00" * (10 * 1024 * 1024 + 1))
    ok, msg = _validate_file_size(str(big_img))
    assert ok is True
    assert "document" in msg.lower()


# --- send_file tool ---

@pytest.mark.asyncio
async def test_send_photo(tmp_png, mock_settings, mock_http):
    from nous.api.telegram_tools import create_send_file_tool
    mock_http.post = AsyncMock(return_value=_ok_response())

    send_file = create_send_file_tool(mock_settings, mock_http)
    result = await send_file(file_path=tmp_png)

    assert "sent successfully" in result["content"][0]["text"].lower()
    mock_http.post.assert_called_once()
    call_args = mock_http.post.call_args
    assert "sendPhoto" in call_args[0][0]


@pytest.mark.asyncio
async def test_send_document(tmp_docx, mock_settings, mock_http):
    from nous.api.telegram_tools import create_send_file_tool
    mock_http.post = AsyncMock(return_value=_ok_response())

    send_file = create_send_file_tool(mock_settings, mock_http)
    result = await send_file(file_path=tmp_docx)

    assert "sent successfully" in result["content"][0]["text"].lower()
    call_args = mock_http.post.call_args
    assert "sendDocument" in call_args[0][0]


@pytest.mark.asyncio
async def test_send_with_caption(tmp_png, mock_settings, mock_http):
    from nous.api.telegram_tools import create_send_file_tool
    mock_http.post = AsyncMock(return_value=_ok_response())

    send_file = create_send_file_tool(mock_settings, mock_http)
    result = await send_file(file_path=tmp_png, caption="Weekly chart")

    call_args = mock_http.post.call_args
    assert call_args[1]["data"]["caption"] == "Weekly chart"


@pytest.mark.asyncio
async def test_send_custom_chat_id(tmp_png, mock_settings, mock_http):
    from nous.api.telegram_tools import create_send_file_tool
    mock_http.post = AsyncMock(return_value=_ok_response())

    send_file = create_send_file_tool(mock_settings, mock_http)
    result = await send_file(file_path=tmp_png, chat_id="99999")

    call_args = mock_http.post.call_args
    assert call_args[1]["data"]["chat_id"] == "99999"


@pytest.mark.asyncio
async def test_send_file_not_found(mock_settings, mock_http):
    from nous.api.telegram_tools import create_send_file_tool
    send_file = create_send_file_tool(mock_settings, mock_http)
    result = await send_file(file_path="/nonexistent/file.png")

    assert "not found" in result["content"][0]["text"].lower()


@pytest.mark.asyncio
async def test_send_file_no_token(mock_http):
    from unittest.mock import MagicMock
    from nous.api.telegram_tools import create_send_file_tool
    s = MagicMock()
    s.telegram_bot_token = None
    s.telegram_chat_id = "12345"

    send_file = create_send_file_tool(s, mock_http)
    result = await send_file(file_path="/tmp/file.png")

    assert "not configured" in result["content"][0]["text"].lower()


@pytest.mark.asyncio
async def test_send_file_no_chat_id(mock_settings, mock_http, tmp_png):
    from nous.api.telegram_tools import create_send_file_tool
    mock_settings.telegram_chat_id = None

    send_file = create_send_file_tool(mock_settings, mock_http)
    result = await send_file(file_path=tmp_png)

    assert "chat_id" in result["content"][0]["text"].lower()


@pytest.mark.asyncio
async def test_send_svg_as_document(tmp_svg, mock_settings, mock_http):
    from nous.api.telegram_tools import create_send_file_tool
    mock_http.post = AsyncMock(return_value=_ok_response())

    send_file = create_send_file_tool(mock_settings, mock_http)
    result = await send_file(file_path=tmp_svg)

    call_args = mock_http.post.call_args
    assert "sendDocument" in call_args[0][0]


@pytest.mark.asyncio
async def test_send_file_cleanup(tmp_png, mock_settings, mock_http):
    """File is deleted after successful send when cleanup=True."""
    from nous.api.telegram_tools import create_send_file_tool
    mock_http.post = AsyncMock(return_value=_ok_response())

    send_file = create_send_file_tool(mock_settings, mock_http)
    result = await send_file(file_path=tmp_png, cleanup=True)

    assert "sent successfully" in result["content"][0]["text"].lower()
    assert not os.path.exists(tmp_png)


@pytest.mark.asyncio
async def test_send_file_no_cleanup_by_default(tmp_png, mock_settings, mock_http):
    """File is NOT deleted by default."""
    from nous.api.telegram_tools import create_send_file_tool
    mock_http.post = AsyncMock(return_value=_ok_response())

    send_file = create_send_file_tool(mock_settings, mock_http)
    result = await send_file(file_path=tmp_png)

    assert os.path.exists(tmp_png)


@pytest.mark.asyncio
async def test_send_file_telegram_error(tmp_png, mock_settings, mock_http):
    """Telegram API error is reported in tool result."""
    from nous.api.telegram_tools import create_send_file_tool
    err_resp = MagicMock()
    err_resp.json.return_value = {"ok": False, "description": "Bad Request: file too big"}
    err_resp.status_code = 400
    mock_http.post = AsyncMock(return_value=err_resp)

    send_file = create_send_file_tool(mock_settings, mock_http)
    result = await send_file(file_path=tmp_png)

    assert "error" in result["content"][0]["text"].lower()
    assert "file too big" in result["content"][0]["text"].lower()


# --- Registration ---

def test_register_telegram_tools(mock_settings, mock_http):
    from nous.api.telegram_tools import register_telegram_tools
    from nous.api.tools import ToolDispatcher

    dispatcher = ToolDispatcher()
    register_telegram_tools(dispatcher, mock_settings, mock_http)

    names = [t["name"] for t in dispatcher.tool_definitions()]
    assert "send_file" in names
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_telegram_tools.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'nous.api.telegram_tools'`

- [ ] **Step 3: Commit test file**

```bash
git add tests/test_telegram_tools.py
git commit -m "test: add send_file tool tests for Issue #220 Phase 1"
```

---

### Task 2: Implement telegram_tools.py

**Files:**
- Create: `nous/api/telegram_tools.py`

- [ ] **Step 1: Create the module**

```python
"""Telegram file delivery tool (Issue #220 Phase 1).

Provides send_file tool for delivering generated files to Telegram
via sendPhoto (images) and sendDocument (everything else).
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

from nous.config import Settings

logger = logging.getLogger(__name__)

# Telegram Bot API base URL
_TG_API = "https://api.telegram.org/bot{token}/{method}"

# Extensions that should use sendPhoto
_PHOTO_EXTENSIONS = frozenset({"png", "jpg", "jpeg", "gif", "webp"})

# Size limits (bytes)
_MAX_FILE_SIZE = 50 * 1024 * 1024  # 50MB — Telegram absolute max
_MAX_PHOTO_SIZE = 10 * 1024 * 1024  # 10MB — sendPhoto limit


def _detect_send_method(file_path: str) -> str:
    """Determine Telegram API method based on file extension.

    Images (png, jpg, jpeg, gif, webp) use sendPhoto.
    Everything else (docx, pdf, svg, etc.) uses sendDocument.
    """
    ext = os.path.splitext(file_path)[1].lstrip(".").lower()
    if ext in _PHOTO_EXTENSIONS:
        return "sendPhoto"
    return "sendDocument"


def _validate_file_size(file_path: str) -> tuple[bool, str]:
    """Validate file size against Telegram limits.

    Returns (ok, message). If ok=True but message is non-empty,
    it's a warning (e.g., photo will be sent as document).
    """
    size = os.path.getsize(file_path)
    if size > _MAX_FILE_SIZE:
        return False, f"File too large ({size / 1024 / 1024:.1f}MB). Telegram limit is 50MB."
    if size > _MAX_PHOTO_SIZE and _detect_send_method(file_path) == "sendPhoto":
        return True, f"Photo is {size / 1024 / 1024:.1f}MB (>10MB). Will send as document instead."
    return True, ""


def create_send_file_tool(settings: Settings, http_client: httpx.AsyncClient):
    """Create the send_file tool closure.

    Args:
        settings: App settings (telegram_bot_token, telegram_chat_id).
        http_client: httpx client for Telegram API calls.

    Returns:
        Async callable matching ToolDispatcher signature.
    """

    async def send_file(
        file_path: str,
        caption: str | None = None,
        chat_id: str | None = None,
        cleanup: bool = False,
    ) -> dict[str, Any]:
        """Send a file to Telegram via sendPhoto or sendDocument.

        Args:
            file_path: Path to the file to send.
            caption: Optional caption for the file.
            chat_id: Telegram chat ID. Defaults to NOUS_TELEGRAM_CHAT_ID.
            cleanup: If True, delete the file after successful send.

        Returns:
            MCP-compliant response with send confirmation or error.
        """
        # Validate configuration
        token = settings.telegram_bot_token
        if not token:
            return _error("Telegram bot token not configured. Set NOUS_TELEGRAM_BOT_TOKEN.")

        target_chat = chat_id or settings.telegram_chat_id
        if not target_chat:
            return _error("No chat_id provided and NOUS_TELEGRAM_CHAT_ID not configured.")

        # Validate file exists
        if not os.path.exists(file_path):
            return _error(f"File not found: {file_path}")

        # Validate size
        ok, size_msg = _validate_file_size(file_path)
        if not ok:
            return _error(size_msg)

        # Determine send method
        method = _detect_send_method(file_path)
        if size_msg:
            # Photo too large — downgrade to document
            method = "sendDocument"

        # Build multipart upload
        file_key = "photo" if method == "sendPhoto" else "document"
        url = _TG_API.format(token=token, method=method)

        try:
            with open(file_path, "rb") as f:
                files = {file_key: (os.path.basename(file_path), f)}
                data: dict[str, str] = {"chat_id": target_chat}
                if caption:
                    data["caption"] = caption

                response = await http_client.post(url, files=files, data=data)

            result = response.json()
            if not result.get("ok"):
                desc = result.get("description", "Unknown Telegram error")
                return _error(f"Telegram API error: {desc}")

            # Success — optionally clean up
            if cleanup:
                try:
                    os.remove(file_path)
                except OSError:
                    logger.warning("Failed to clean up file: %s", file_path)

            filename = os.path.basename(file_path)
            warning = f" (Note: {size_msg})" if size_msg else ""
            return _ok(
                f"File sent successfully: {filename} via {method} to chat {target_chat}.{warning}"
            )

        except Exception as e:
            logger.exception("Failed to send file to Telegram: %s", file_path)
            return _error(f"Failed to send file: {e}")

    return send_file


def _ok(text: str) -> dict[str, Any]:
    """MCP-compliant success response."""
    return {"content": [{"type": "text", "text": text}]}


def _error(text: str) -> dict[str, Any]:
    """MCP-compliant error response."""
    return {"content": [{"type": "text", "text": f"Error: {text}"}]}


# ---------------------------------------------------------------------------
# Tool schema
# ---------------------------------------------------------------------------

_SEND_FILE_SCHEMA = {
    "description": (
        "Send a file to Telegram. Images (png, jpg, gif, webp) are sent as photos; "
        "all other files (docx, pdf, xlsx, csv, svg, etc.) as documents. "
        "Use after generating a file with write_file, bash, or run_python."
    ),
    "type": "object",
    "properties": {
        "file_path": {
            "type": "string",
            "description": "Absolute path to the file to send.",
        },
        "caption": {
            "type": "string",
            "description": "Optional caption displayed with the file.",
        },
        "chat_id": {
            "type": "string",
            "description": "Telegram chat ID. Defaults to configured NOUS_TELEGRAM_CHAT_ID.",
        },
        "cleanup": {
            "type": "boolean",
            "description": "If true, delete the file after successful send. Default: false.",
        },
    },
    "required": ["file_path"],
}


def register_telegram_tools(
    dispatcher, settings: Settings, http_client: httpx.AsyncClient,
) -> None:
    """Register Telegram tools with the dispatcher."""
    handler = create_send_file_tool(settings, http_client)
    dispatcher.register("send_file", handler, _SEND_FILE_SCHEMA)
```

- [ ] **Step 2: Run tests to verify they pass**

Run: `uv run pytest tests/test_telegram_tools.py -v`
Expected: All 15 tests PASS

- [ ] **Step 3: Commit implementation**

```bash
git add nous/api/telegram_tools.py
git commit -m "feat: add send_file tool for Telegram file delivery (Issue #220)"
```

---

### Task 3: Wire into FRAME_TOOLS and main.py

**Files:**
- Modify: `nous/api/runner.py:50-57` (FRAME_TOOLS)
- Modify: `nous/main.py:311-331` (tool registration)
- Modify: `nous/telegram_bot.py:209-219` (tool indicator)

- [ ] **Step 1: Add send_file to FRAME_TOOLS in runner.py**

In `nous/api/runner.py`, add `"send_file"` to the `conversation`, `task`, and `debug` frame tool lists:

```python
# In FRAME_TOOLS dict:
# "conversation": [..., "run_python", "send_file"],
# "task": ["*"],  # already includes all
# "debug": [..., "run_python", "send_file"],
```

Specifically append `"send_file"` to the end of the `conversation` and `debug` lists.

- [ ] **Step 2: Wire register_telegram_tools in main.py**

In `nous/main.py`, after the `register_web_tools` block (around line 331), add:

```python
    # Issue #220: Register Telegram file delivery tool (gated on bot token)
    if settings.telegram_bot_token:
        from nous.api.telegram_tools import register_telegram_tools
        register_telegram_tools(dispatcher, settings, web_http)
        logger.info("Telegram file delivery tool registered (send_file)")
```

Note: reuses `web_http` client (already created for web tools) — no need for a separate client.

- [ ] **Step 3: Add send_file to StreamingMessage tool indicators in telegram_bot.py**

In `nous/telegram_bot.py`, add to `TOOL_INDICATORS` dict (around line 219):

```python
        "send_file": ("\U0001f4ce", "Sending file"),
```

- [ ] **Step 4: Run full test suite to verify no regressions**

Run: `uv run pytest tests/test_telegram_tools.py tests/test_config.py -v`
Expected: All tests PASS

- [ ] **Step 5: Commit wiring changes**

```bash
git add nous/api/runner.py nous/main.py nous/telegram_bot.py
git commit -m "feat: wire send_file tool into frames, main.py, and streaming indicators"
```

---

### Task 4: Integration test and final verification

**Files:**
- Modify: `tests/test_telegram_tools.py` (add integration-style test)

- [ ] **Step 1: Add dispatcher integration test**

Add to `tests/test_telegram_tools.py`:

```python
@pytest.mark.asyncio
async def test_dispatch_send_file(tmp_png, mock_settings, mock_http):
    """Test send_file works through ToolDispatcher.dispatch()."""
    from nous.api.telegram_tools import register_telegram_tools
    from nous.api.tools import ToolDispatcher

    mock_http.post = AsyncMock(return_value=_ok_response())

    dispatcher = ToolDispatcher()
    register_telegram_tools(dispatcher, mock_settings, mock_http)

    result_text, is_error = await dispatcher.dispatch(
        "send_file", {"file_path": tmp_png, "caption": "Test"}
    )

    assert not is_error
    assert "sent successfully" in result_text.lower()
```

- [ ] **Step 2: Add photo-downgrade integration test**

Add to `tests/test_telegram_tools.py`:

```python
@pytest.mark.asyncio
async def test_send_oversized_photo_downgrades_to_document(tmp_path, mock_settings, mock_http):
    """Photos >10MB are sent via sendDocument instead of sendPhoto."""
    from nous.api.telegram_tools import create_send_file_tool
    mock_http.post = AsyncMock(return_value=_ok_response())

    big_img = tmp_path / "big.png"
    big_img.write_bytes(b"\x00" * (10 * 1024 * 1024 + 1))

    send_file = create_send_file_tool(mock_settings, mock_http)
    result = await send_file(file_path=str(big_img))

    call_args = mock_http.post.call_args
    assert "sendDocument" in call_args[0][0]
    assert "sent successfully" in result["content"][0]["text"].lower()
```

- [ ] **Step 3: Verify FRAME_TOOLS includes send_file**

Add to `tests/test_telegram_tools.py`:

```python
def test_send_file_in_frame_tools():
    """send_file is available in conversation and debug frames."""
    from nous.api.runner import FRAME_TOOLS
    assert "send_file" in FRAME_TOOLS["conversation"]
    assert "send_file" in FRAME_TOOLS["debug"]
    # task uses "*" wildcard, so it implicitly includes all tools
```

- [ ] **Step 4: Run all tests**

Run: `uv run pytest tests/test_telegram_tools.py -v`
Expected: All 20 tests PASS

- [ ] **Step 5: Run broader test suite to check for regressions**

Run: `uv run pytest tests/ -v --timeout=60 -x`
Expected: No failures

- [ ] **Step 6: Update CLAUDE.md Agent Tools table**

Add `send_file` to the Agent Tools table in `CLAUDE.md`:

```markdown
| `send_file` | task, conversation, debug | Send files to Telegram (images as photos, rest as documents) |
```

- [ ] **Step 7: Commit final tests and docs**

```bash
git add tests/test_telegram_tools.py CLAUDE.md
git commit -m "test: add integration tests for send_file dispatch and frame gating"
```
