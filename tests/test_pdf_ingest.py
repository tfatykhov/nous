import base64
from unittest.mock import AsyncMock

import pytest

import nous.api.attachment_store as store
from nous.api.attachment_store import (
    _extract_pdf_text,
    _transcribe_and_ingest_pdf,
    maybe_ingest_pdf,
)
from nous.api.models import Attachment, ApiResponse


def _settings():
    from nous.config import Settings
    return Settings()


def _pdf_att():
    raw = b"%PDF-1.4 dummy bytes"
    a = Attachment(filename="doc.pdf", media_type="application/pdf",
                   data_base64=base64.b64encode(raw).decode(),
                   size_bytes=len(raw), source="telegram")
    a.content_type = "document"
    return a


@pytest.mark.asyncio
async def test_maybe_ingest_pdf_pypdf_path_ingests_inline(monkeypatch):
    gs = _settings()
    long_text = "Extracted body. " * 20  # well over MIN_PDF_TEXT_CHARS
    monkeypatch.setattr(store, "_extract_pdf_text", lambda raw: long_text)
    ingest = AsyncMock(return_value={"inserted": 1})
    monkeypatch.setattr("nous.api.tools.ingest_document_text", ingest)

    await maybe_ingest_pdf(object(), gs, _pdf_att(),
                           session_id="s1", episode_id="e1", llm_client=None)

    ingest.assert_awaited_once()
    assert ingest.await_args.kwargs["content"] == long_text


@pytest.mark.asyncio
async def test_maybe_ingest_pdf_scanned_fallback_transcribes(monkeypatch):
    gs = _settings()
    # No extractable text -> scanned path.
    monkeypatch.setattr(store, "_extract_pdf_text", lambda raw: "")
    ingest = AsyncMock(return_value={"inserted": 2})
    monkeypatch.setattr("nous.api.tools.ingest_document_text", ingest)

    fake_resp = ApiResponse(
        content=[{"type": "text", "text": "transcribed body of the scanned pdf"}],
        stop_reason="end_turn",
    )
    llm = type("LLM", (), {"call": AsyncMock(return_value=fake_resp)})()

    # Call the fallback coroutine directly to avoid create_task timing flakiness.
    await _transcribe_and_ingest_pdf(
        b"%PDF raw", object(), gs, llm,
        source_ref="doc.pdf", episode_id="e1",
        model=gs.attachments_pdf_transcription_model,
        max_tokens=gs.attachments_pdf_max_transcription_tokens,
        filename="doc.pdf",
    )

    llm.call.assert_awaited_once()
    # Regression: the payload MUST carry a non-empty "system" — the default sdk
    # backend's _payload_to_kwargs indexes payload["system"] and would KeyError
    # (silently killing the fallback) if it were absent.
    sent_payload = llm.call.await_args.args[0]
    assert sent_payload.get("system")
    ingest.assert_awaited_once()
    assert ingest.await_args.kwargs["content"] == "transcribed body of the scanned pdf"


@pytest.mark.asyncio
async def test_maybe_ingest_pdf_scanned_schedules_task(monkeypatch):
    gs = _settings()
    monkeypatch.setattr(store, "_extract_pdf_text", lambda raw: "")

    captured = {}

    def fake_create_task(coro):
        captured["coro"] = coro
        return None  # we'll await the coro ourselves

    monkeypatch.setattr(store.asyncio, "create_task", fake_create_task)

    transcribe = AsyncMock()
    monkeypatch.setattr(store, "_transcribe_and_ingest_pdf", transcribe)

    llm = type("LLM", (), {"call": AsyncMock()})()
    await maybe_ingest_pdf(object(), gs, _pdf_att(),
                           session_id="s1", episode_id="e1", llm_client=llm)

    # A task was scheduled with the transcription coroutine.
    assert "coro" in captured
    await captured["coro"]  # drive it so the inner AsyncMock records the call
    transcribe.assert_awaited_once()


@pytest.mark.asyncio
async def test_maybe_ingest_pdf_disabled_skips(monkeypatch):
    gs = _settings()
    gs.attachments_ingest_pdfs = False

    def _boom(raw):
        raise AssertionError("_extract_pdf_text must not be called when disabled")

    monkeypatch.setattr(store, "_extract_pdf_text", _boom)

    await maybe_ingest_pdf(object(), gs, _pdf_att(),
                           session_id="s1", episode_id="e1", llm_client=None)


@pytest.mark.asyncio
async def test_maybe_ingest_pdf_non_pdf_skips(monkeypatch):
    gs = _settings()

    def _boom(raw):
        raise AssertionError("_extract_pdf_text must not be called for non-PDF")

    monkeypatch.setattr(store, "_extract_pdf_text", _boom)

    att = _pdf_att()
    att.content_type = "image"
    att.media_type = "image/png"
    await maybe_ingest_pdf(object(), gs, att,
                           session_id="s1", episode_id="e1", llm_client=None)


def test_extract_pdf_text_handles_garbage_without_raising():
    # Not a valid PDF; pypdf raises internally -> helper must return "" not raise.
    assert _extract_pdf_text(b"not a pdf at all") == ""
