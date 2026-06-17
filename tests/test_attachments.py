import base64
import pytest

from nous.api.attachments import (
    sanitize_filename, classify_attachment, validate_base64_size,
    validate_attachment, build_content_blocks, compact_message_for_history,
    sanitize_blocks_for_storage, MAX_IMAGE_SIZE,
)
from nous.api.models import Attachment, Message


def _att(**kw):
    base = dict(filename="f.png", media_type="image/png", data_base64="", size_bytes=0)
    base.update(kw)
    a = Attachment(filename=base["filename"], media_type=base["media_type"],
                   data_base64=base["data_base64"], size_bytes=base["size_bytes"],
                   source=base.get("source", "upload"))
    a.content_type = classify_attachment(a.filename, a.media_type)
    return a


def test_sanitize_filename_strips_path_and_unsafe():
    assert sanitize_filename("../../etc/passwd") == "passwd"
    assert sanitize_filename("a/b/c.png") == "c.png"
    assert "\x00" not in sanitize_filename("x\x00y.txt")
    assert sanitize_filename("") == "unnamed_file"
    assert len(sanitize_filename("a" * 500 + ".png")) <= 255


def test_sanitize_filename_pure_dots_collapse():
    # F7: pure-dot results must not be usable as path-escape directory names
    assert sanitize_filename("..") == "unnamed_file"
    assert sanitize_filename(".") == "unnamed_file"
    assert sanitize_filename("foo/..") == "unnamed_file"


def test_classify_attachment():
    assert classify_attachment("x.png", "image/png") == "image"
    assert classify_attachment("x.pdf", "application/pdf") == "document"
    assert classify_attachment("x.py", "text/x-python") == "text_file"
    assert classify_attachment("x.json", "application/octet-stream") == "text_file"
    assert classify_attachment("x.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document") == "unsupported"
    assert classify_attachment("x.mp3", "audio/mpeg") == "unsupported"


def test_validate_base64_size_matches_actual():
    raw = b"hello world" * 1000
    assert validate_base64_size(base64.b64encode(raw).decode()) == len(raw)


def test_validate_attachment_rejects_unsupported_and_oversize():
    assert validate_attachment(_att(filename="a.mp3", media_type="audio/mpeg")) is not None
    # Non-empty base64 that decodes past the image limit (empty data is rejected
    # first by the empty-data guard, so it must carry real oversized bytes here).
    oversize_raw = b"\x89PNG\r\n\x1a\n" * (MAX_IMAGE_SIZE // 4)
    big = _att(filename="a.png", media_type="image/png",
               data_base64=base64.b64encode(oversize_raw).decode(),
               size_bytes=MAX_IMAGE_SIZE + 1)
    assert "too large" in validate_attachment(big)
    ok_raw = b"\x89PNG\r\n\x1a\n" * 4
    ok = _att(filename="a.png", media_type="image/png",
              data_base64=base64.b64encode(ok_raw).decode(), size_bytes=len(ok_raw))
    assert validate_attachment(ok) is None


def test_validate_attachment_rejects_empty_data():
    a = Attachment(filename="x.png", media_type="image/png", data_base64="", size_bytes=0)
    a.content_type = classify_attachment(a.filename, a.media_type)
    assert validate_attachment(a) is not None


def test_validate_attachment_derives_size_from_base64():
    # Untrusted size_bytes=0 but the actual base64 decodes to > MAX_IMAGE_SIZE.
    raw = b"\x89PNG\r\n\x1a\n" * (MAX_IMAGE_SIZE // 4)
    huge = _att(filename="a.png", media_type="image/png",
                data_base64=base64.b64encode(raw).decode(), size_bytes=0)
    assert "too large" in validate_attachment(huge)


def test_validate_attachment_rejects_corrupt_base64():
    # F9: malformed base64 for image/document must be caught here, not at the API
    bad = _att(filename="a.png", media_type="image/png",
               data_base64="!!!not base64!!!", size_bytes=10)
    assert validate_attachment(bad) is not None


def test_build_content_blocks_orders_media_then_text():
    img = _att(filename="a.png", media_type="image/png",
               data_base64=base64.b64encode(b"\x89PNG").decode(), size_bytes=4)
    blocks = build_content_blocks("describe this", [img])
    assert blocks[0]["type"] == "image"
    assert blocks[0]["source"]["media_type"] == "image/png"
    assert blocks[-1] == {"type": "text", "text": "describe this"}


def test_build_content_blocks_last_block_is_text_when_no_caption():
    # F11: with no caption + single image, the LAST block must be text
    # (else cache_control lands on the base64 image block).
    img = _att(filename="a.png", media_type="image/png",
               data_base64=base64.b64encode(b"\x89PNG").decode(), size_bytes=4)
    blocks = build_content_blocks("", [img])
    assert blocks[-1]["type"] == "text"


def test_build_content_blocks_text_file_decoded_with_header():
    code = base64.b64encode(b"print('hi')").decode()
    tf = _att(filename="s.py", media_type="text/x-python", data_base64=code, size_bytes=11)
    blocks = build_content_blocks("", [tf])
    assert blocks[0]["type"] == "text"
    assert "--- File: s.py ---" in blocks[0]["text"]
    assert "print('hi')" in blocks[0]["text"]


def test_compact_message_replaces_blobs_and_clears_base64():
    img = _att(filename="a.png", media_type="image/png", data_base64="QUJD", size_bytes=3)
    img.workspace_path = "/tmp/nous-workspace/attachments/s/abc__a.png"
    msg = Message(role="user", content=build_content_blocks("hi", [img]),
                  attachments=[img], text_content="hi")
    out = compact_message_for_history(msg)
    assert "QUJD" not in str(out.content)
    assert "abc__a.png" in str(out.content)  # actionable on-disk reference
    assert img.data_base64 == ""  # cleared for GC


def test_compact_message_strips_text_file_body():
    # F3/P0: text-file BODY must not persist in history
    code = base64.b64encode(b"SECRET_TOKEN=abc123" * 100).decode()
    tf = _att(filename="s.env", media_type="text/plain", data_base64=code, size_bytes=1900)
    tf.workspace_path = "/tmp/nous-workspace/attachments/s/zzz__s.env"
    msg = Message(role="user", content=build_content_blocks("check", [tf]),
                  attachments=[tf], text_content="check")
    out = compact_message_for_history(msg)
    assert "SECRET_TOKEN" not in str(out.content)
    assert "s.env" in str(out.content)


def test_compact_message_string_passthrough():
    msg = Message(role="user", content="plain", text_content="plain")
    assert compact_message_for_history(msg).content == "plain"


def test_sanitize_blocks_for_storage_strips_base64_without_attachments():
    # F1: called from _save_conversation WITHOUT attachment metadata; must still strip base64
    content = [
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "BIGB64DATA"}},
        {"type": "text", "text": "what is this?"},
    ]
    out = sanitize_blocks_for_storage(content)
    assert "BIGB64DATA" not in str(out)
    assert "what is this?" in str(out)


def test_sanitize_blocks_string_passthrough():
    assert sanitize_blocks_for_storage("plain text") == "plain text"
