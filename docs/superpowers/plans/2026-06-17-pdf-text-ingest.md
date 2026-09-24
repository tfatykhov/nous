# F024.1 — PDF Text Ingestion Implementation Plan

> Implement task-by-task; steps use `- [ ]`. Branch: `feat/F024.1-pdf-text-ingest` (off main @ 989f435).

**Goal:** When a user sends a PDF (already an F024 inbound attachment), extract its text and chunk-ingest it into `heart.episode_chunks` so its *contents* are recall-searchable — closing the image/PDF-vs-text asymmetry.

**Approach (hybrid, on by default):** `pypdf` extracts text deterministically (fast, complete, handles long PDFs). If `pypdf` yields too little text (scanned/image-only PDF), fall back to a **Claude transcription** call (cheap model). The `pypdf` path ingests inline (like text files); the slow model-fallback path runs **fire-and-forget** (`asyncio.create_task`) so it never blocks the turn. Reuses `ingest_document_text`.

**Wiring (verified):** runner has `self._api` (AnthropicClient) + `self._settings` + `self._heart` at the persist block (run_turn ~460, stream_chat ~1088, right after `maybe_ingest_text_file`). `ingest_document_text(heart, settings, *, content, source_ref, session_id, episode_id)` is the chunk→embed→episode_chunks helper. Config uses **bare annotations** next to the other `attachments_*` fields.

---

## Task 1 — deps + config + `maybe_ingest_pdf` + unit tests

**Files:** `pyproject.toml`, `nous/config.py`, `nous/api/attachment_store.py`, `tests/test_pdf_ingest.py` (new)

- [ ] **Dep:** add `"pypdf>=5.0,<6.0",` to the `[project.optional-dependencies] agent` group (alongside python-docx/openpyxl/Pillow). Then `uv sync --extra agent` (or the project's equivalent) so pypdf is importable in dev.
- [ ] **Config** (bare annotations, immediately after `attachments_ingest_text_files`):
  ```python
      attachments_ingest_pdfs: bool = True  # extract + chunk-ingest PDF text for recall
      attachments_pdf_transcription_model: str = "claude-haiku-4-5-20251001"  # scanned-PDF fallback
      attachments_pdf_max_transcription_tokens: int = 8000  # output cap for the fallback call
  ```
- [ ] **`attachment_store.py`** — add module constants + functions:
  - `MIN_PDF_TEXT_CHARS = 100` (below this, treat pypdf result as "scanned" → fallback).
  - `_extract_pdf_text(raw: bytes) -> str` — guarded `from pypdf import PdfReader`; if import fails return `""`; else read pages, join `page.extract_text() or ""`. Pure/sync (called via `asyncio.to_thread`).
  - `async def maybe_ingest_pdf(heart, settings, att, *, session_id, episode_id, llm_client=None) -> None`:
    - Guard: `if not settings.attachments_ingest_pdfs or att.content_type != "document" or att.media_type != "application/pdf" or not att.data_base64: return`.
    - `raw = base64.b64decode(att.data_base64)` (wrap in try; on failure log + return).
    - `text = await asyncio.to_thread(_extract_pdf_text, raw)`.
    - If `len(text.strip()) >= MIN_PDF_TEXT_CHARS`: call `ingest_document_text(heart, settings, content=text, source_ref=att.workspace_path or att.filename, session_id=session_id, episode_id=episode_id)`, inspect `{error,code}` return + log (mirror `maybe_ingest_text_file`). Return.
    - Else (insufficient text): if `llm_client is not None`: `asyncio.create_task(_transcribe_and_ingest_pdf(raw, heart, settings, llm_client, source_ref=att.workspace_path or att.filename, episode_id=episode_id, model=settings.attachments_pdf_transcription_model, max_tokens=settings.attachments_pdf_max_transcription_tokens, filename=att.filename))`. Else log `info` "no pypdf text and no llm client; skipping PDF ingest for %s".
  - `async def _transcribe_and_ingest_pdf(raw, heart, settings, llm_client, *, source_ref, episode_id, model, max_tokens, filename) -> None`:
    - Wrap the WHOLE body in try/except → `logger.warning(...)` (fire-and-forget must never raise).
    - Build payload:
      ```python
      b64 = base64.b64encode(raw).decode()
      payload = {"model": model, "max_tokens": max_tokens, "messages": [{"role": "user", "content": [
          {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": b64}},
          {"type": "text", "text": "Transcribe the full text content of this document verbatim. Output only the document's text, no commentary."},
      ]}]}
      resp = await llm_client.call(payload)
      ```
    - Parse text + stop_reason from `resp` — READ how `call_background_llm` (nous/handlers/__init__.py) and `_call_api` parse `client.call(...)` output and mirror it exactly (it returns content blocks + stop_reason). If `stop_reason == "max_tokens"`, `logger.warning("PDF transcription truncated for %s (raise max_tokens or add paging)", filename)`.
    - If text non-empty: `await ingest_document_text(heart, settings, content=text, source_ref=source_ref, session_id=None, episode_id=episode_id)` + log the `{error,code}` outcome.

- [ ] **Tests `tests/test_pdf_ingest.py`** (`pytest.mark.asyncio`; no DB needed if `ingest_document_text` is monkeypatched/mocked):
  - `test_extract_pdf_text_from_real_pdf` — a tiny embedded text-PDF byte literal (or build one with pypdf if a writer is available; else monkeypatch `_extract_pdf_text`); assert non-empty extraction.
  - `test_maybe_ingest_pdf_pypdf_path_ingests_inline` — monkeypatch `_extract_pdf_text` to return a long string; monkeypatch `ingest_document_text` (AsyncMock) and assert it's awaited with that content, and NO `create_task` fallback fired.
  - `test_maybe_ingest_pdf_scanned_fallback_spawns_transcription` — monkeypatch `_extract_pdf_text` to return `""`; pass a mock `llm_client` whose `.call` returns a fake response with transcribed text + `stop_reason="end_turn"`; assert the transcription path runs and `ingest_document_text` is awaited with the transcribed text. (Await the spawned task — e.g. capture it, or call `_transcribe_and_ingest_pdf` directly to avoid create_task timing.)
  - `test_maybe_ingest_pdf_disabled_or_non_pdf_skips` — `attachments_ingest_pdfs=False`, and a non-PDF attachment: assert no extraction/ingest.
  - `test_extract_pdf_text_missing_pypdf_returns_empty` — simulate pypdf import failure (monkeypatch the import or the guarded flag); assert `_extract_pdf_text` returns `""` and `maybe_ingest_pdf` with no llm_client logs+skips without raising.
- [ ] Run `uv run pytest tests/test_pdf_ingest.py -v` → PASS. Commit.

## Task 2 — runner wiring + integration test + docs

**Files:** `nous/api/runner.py`, `tests/test_attachments_integration.py`, docs.

- [ ] In `run_turn` persist block (after the `maybe_ingest_text_file` call, ~line 460) add:
  ```python
                    await attachment_store.maybe_ingest_pdf(
                        self._heart, self._settings, att,
                        session_id=session_id, episode_id=episode_id,
                        llm_client=self._api)
  ```
- [ ] Same in `stream_chat` persist block (~line 1088).
- [ ] Integration test: a `run_turn` with a PDF attachment (monkeypatch `_extract_pdf_text` to return text; monkeypatch `ingest_document_text` AsyncMock) asserts ingest is invoked from the runner path. Keep it light if the fixture makes a full PDF turn heavy.
- [ ] Run `uv run pytest tests/test_pdf_ingest.py tests/test_attachments_integration.py tests/test_runner.py -q` → PASS.
- [ ] **Docs:** update `docs/implementation/011.2-multimodal-file-support.md` (the PDF body-ingest is no longer deferred — note the hybrid pypdf+model approach + the long-scanned-PDF truncation caveat); add CLAUDE.md env rows (`NOUS_ATTACHMENTS_INGEST_PDFS`=true, `NOUS_ATTACHMENTS_PDF_TRANSCRIPTION_MODEL`, `NOUS_ATTACHMENTS_PDF_MAX_TRANSCRIPTION_TOKENS`) and update the F024 shipped-row to mention PDF text ingest. Commit.

## Known limitations (document)
- Long **scanned** PDFs truncate at the transcription output cap (paging deferred). Text PDFs via pypdf are complete.
- Fire-and-forget fallback is best-effort: lost if the process exits before it completes.
- `pypdf` lives in the `agent` extra; a core install without it falls back to model transcription (or skips if no llm_client).
