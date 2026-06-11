"""Tests for Telegram file delivery tool (Issue #220 Phase 1)."""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock

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
    assert call_args.kwargs["data"]["caption"] == "Weekly chart"


@pytest.mark.asyncio
async def test_send_explicit_matching_chat_id(tmp_png, mock_settings, mock_http):
    # AS-3: an explicit chat_id equal to the configured chat is allowed.
    from nous.api.telegram_tools import create_send_file_tool
    mock_http.post = AsyncMock(return_value=_ok_response())

    send_file = create_send_file_tool(mock_settings, mock_http)
    result = await send_file(file_path=tmp_png, chat_id="12345")

    call_args = mock_http.post.call_args
    assert call_args.kwargs["data"]["chat_id"] == "12345"
    assert "sent successfully" in result["content"][0]["text"].lower()


@pytest.mark.asyncio
async def test_send_mismatched_chat_id_rejected(tmp_png, mock_settings, mock_http):
    # AS-3: a chat_id that differs from the configured chat is rejected
    # (exfiltration guard); no HTTP call is made.
    from nous.api.telegram_tools import create_send_file_tool
    mock_http.post = AsyncMock(return_value=_ok_response())

    send_file = create_send_file_tool(mock_settings, mock_http)
    result = await send_file(file_path=tmp_png, chat_id="99999")

    assert "not permitted" in result["content"][0]["text"].lower()
    mock_http.post.assert_not_called()


@pytest.mark.asyncio
async def test_send_uploads_file_bytes(tmp_png, mock_settings, mock_http):
    # AS-6: the file is read (off-loop) and its bytes are uploaded.
    from nous.api.telegram_tools import create_send_file_tool
    mock_http.post = AsyncMock(return_value=_ok_response())

    send_file = create_send_file_tool(mock_settings, mock_http)
    await send_file(file_path=tmp_png)

    files = mock_http.post.call_args.kwargs["files"]
    _name, content = files["photo"]
    assert isinstance(content, bytes)
    assert content == open(tmp_png, "rb").read()


@pytest.mark.asyncio
async def test_send_file_not_found(mock_settings, mock_http):
    from nous.api.telegram_tools import create_send_file_tool
    send_file = create_send_file_tool(mock_settings, mock_http)
    result = await send_file(file_path="/nonexistent/file.png")

    assert "not found" in result["content"][0]["text"].lower()


@pytest.mark.asyncio
async def test_send_file_no_token(mock_http):
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


# --- Integration tests ---

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


def test_send_file_in_frame_tools():
    """send_file is available in conversation and debug frames."""
    from nous.api.runner import FRAME_TOOLS
    assert "send_file" in FRAME_TOOLS["conversation"]
    assert "send_file" in FRAME_TOOLS["debug"]
    # task uses "*" wildcard, so it implicitly includes all tools
