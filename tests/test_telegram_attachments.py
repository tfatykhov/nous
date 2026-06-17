"""Telegram inbound-attachment tests for F024.

The bot previously had zero coverage. These tests instantiate NousTelegramBot
with attachments enabled and mock its instance-level collaborators (_tg, _http,
_chat_streaming, _send) so we can exercise _download_telegram_file and the
_handle_update attachment block without any real network I/O.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from nous.telegram_bot import NousTelegramBot


class Resp:
    """Minimal stand-in for an httpx.Response."""

    def __init__(self, status_code: int, content: bytes = b""):
        self.status_code = status_code
        self.content = content


def _make_bot() -> NousTelegramBot:
    bot = NousTelegramBot(
        bot_token="TESTTOKEN",
        nous_url="http://x",
        attachments_enabled=True,
    )
    # allowed_users=None => everyone allowed.
    bot._tg = AsyncMock()
    bot._http = MagicMock()
    bot._http.get = AsyncMock()
    bot._chat_streaming = AsyncMock()
    bot._send = AsyncMock()
    return bot


@pytest.mark.asyncio
async def test_download_url_and_token_not_in_error():
    """A failed download must raise WITHOUT leaking the bot token; the happy
    path returns the downloaded bytes."""
    bot = _make_bot()
    bot._tg = AsyncMock(return_value={"file_path": "photos/f.jpg"})
    bot._http.get = AsyncMock(return_value=Resp(status_code=404, content=b""))

    with pytest.raises(ValueError) as exc:
        await bot._download_telegram_file("fid")
    assert "TESTTOKEN" not in str(exc.value)

    # 200 path returns the bytes.
    bot._http.get = AsyncMock(return_value=Resp(status_code=200, content=b"\x89PNG\x00"))
    data = await bot._download_telegram_file("fid")
    assert data == b"\x89PNG\x00"


@pytest.mark.asyncio
async def test_handle_update_photo_extracts_attachment():
    bot = _make_bot()
    bot._tg = AsyncMock(return_value={"file_path": "photos/f.jpg"})
    bot._http.get = AsyncMock(return_value=Resp(status_code=200, content=b"imgbytes"))

    update = {"message": {
        "chat": {"id": 1}, "from": {"id": 1},
        "photo": [{"file_id": "f", "file_unique_id": "u", "width": 1, "height": 1}],
    }}
    await bot._handle_update(update)

    bot._chat_streaming.assert_awaited_once()
    kwargs = bot._chat_streaming.await_args.kwargs
    atts = kwargs["attachments"]
    assert atts is not None and len(atts) == 1
    assert atts[0].content_type == "image"
    assert atts[0].media_type == "image/jpeg"


@pytest.mark.asyncio
async def test_handle_update_document_mime_and_classify():
    bot = _make_bot()
    bot._tg = AsyncMock(return_value={"file_path": "docs/m.py"})
    bot._http.get = AsyncMock(return_value=Resp(status_code=200, content=b"print('hi')"))

    update = {"message": {
        "chat": {"id": 1}, "from": {"id": 1},
        "document": {"file_id": "d", "mime_type": "text/x-python", "file_name": "m.py"},
    }}
    await bot._handle_update(update)

    bot._chat_streaming.assert_awaited_once()
    atts = bot._chat_streaming.await_args.kwargs["attachments"]
    assert atts is not None and len(atts) == 1
    assert atts[0].content_type == "text_file"
    assert atts[0].filename == "m.py"


@pytest.mark.asyncio
async def test_handle_update_voice_rejected():
    bot = _make_bot()

    update = {"message": {
        "chat": {"id": 1}, "from": {"id": 1},
        "voice": {"file_id": "v", "duration": 3},
    }}
    await bot._handle_update(update)

    bot._chat_streaming.assert_not_awaited()
    bot._send.assert_awaited()
    sent_text = " ".join(str(c.args[1]) for c in bot._send.await_args_list)
    assert "audio" in sent_text.lower()


@pytest.mark.asyncio
async def test_handle_update_partial_download_failure_keeps_others():
    """Photo succeeds, document download raises -> photo still forwarded AND a
    'couldn't download' warning is sent."""
    bot = _make_bot()
    bot._tg = AsyncMock(return_value={"file_path": "x"})

    async def _dl(file_id):
        if file_id == "photo_fid":
            return b"imgbytes"
        raise ValueError("boom")

    bot._download_telegram_file = AsyncMock(side_effect=_dl)

    update = {"message": {
        "chat": {"id": 1}, "from": {"id": 1},
        "photo": [{"file_id": "photo_fid", "file_unique_id": "u", "width": 1, "height": 1}],
        "document": {"file_id": "doc_fid", "mime_type": "application/pdf", "file_name": "a.pdf"},
    }}
    await bot._handle_update(update)

    bot._chat_streaming.assert_awaited_once()
    atts = bot._chat_streaming.await_args.kwargs["attachments"]
    assert atts is not None and len(atts) == 1
    assert atts[0].content_type == "image"

    warned = " ".join(str(c.args[1]) for c in bot._send.await_args_list)
    assert "couldn't download" in warned.lower()


@pytest.mark.asyncio
async def test_handle_update_animated_sticker_skipped():
    bot = _make_bot()
    bot._download_telegram_file = AsyncMock(side_effect=AssertionError("must not download"))

    update = {"message": {
        "chat": {"id": 1}, "from": {"id": 1},
        "sticker": {"file_id": "s", "file_unique_id": "su", "is_animated": True},
    }}
    await bot._handle_update(update)

    bot._chat_streaming.assert_not_awaited()
