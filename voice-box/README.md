# voice-box

GPU-box voice service for the Nous voice POC (PR 1 of the plan; spec: `research/voice-poc-2026-11-13/spec.md`, sections 0, 1, C). It serves `/health`, `/stt` and `/tts` on port 7860. The Pipecat pipeline (`/api/offer`, `bot.py`) arrives in PR 7. This directory is self-contained: its dependencies are **not** in the main Nous `pyproject.toml`, and `pytest tests/` at the repo root does not collect it.

Statements below tagged **[assumption]** are not yet verified on real hardware.

## Endpoints

| Route | Auth | Contract |
|---|---|---|
| `GET /health` | none | `{status: ok\|degraded\|down, version, stt{model,device}, tts{engine,provider}, vram_free_mb, vram_total_mb, ollama_loaded[], active_sessions, uptime_s}`. Never contains a secret. `degraded` = STT fell back to CPU int8 (spec C3 rule 5); `down` = model preload failed. |
| `POST /stt` | Bearer | multipart field `file` (audio, max `VOICE_BOX_MAX_UPLOAD_MB`) → `{text, language, duration_s, ms}`. 400 empty, 413 too large, 422 undecodable. |
| `POST /tts` | Bearer | JSON `{text, voice?, format?}` (`format` = `wav` default, or `pcm` raw 16-bit LE mono) → audio bytes. |

`Authorization: Bearer $VOICE_BOX_TOKEN`, compared in constant time. The server **refuses to start with an empty token**. Audio and text are never logged and audio is never written to disk (decoded from memory).

## Run on the box (Tim)

Follow `setup.ps1` (copy-paste steps of spec C5; idempotent; do not run it anywhere else). Then:

```powershell
cd C:\nous-src\voice-box
$env:VOICE_BOX_ENV_FILE = "C:\voice\.env"
C:\voice\.venv\Scripts\python.exe server.py
curl.exe http://<box-ip>:7860/health
```

**Network:** binds `0.0.0.0:7860` by default; the Windows Firewall rules from `setup.ps1` limit TCP 7860 (and UDP for the Python exe, needed from PR 7) to `192.168.1.0/24` and Tailscale `100.64.0.0/10`. Set `VOICE_BOX_HOST=127.0.0.1` for local-only testing. Do not port-forward it.

## Ollama coexistence (spec C3)

- Models load at startup and are never unloaded (`VOICE_BOX_PRELOAD=1`). Start voice before Ollama after a reboot.
- On the box set `OLLAMA_MAX_LOADED_MODELS=1` and `OLLAMA_KEEP_ALIVE=2m` **[assumption: names current]**.
- `vram.free_vram_for_session()` implements rule 4: if free VRAM is below `VOICE_BOX_MIN_FREE_VRAM_MB` (5000) it unloads every model listed by Ollama `GET /api/ps` via `{"model": ..., "keep_alive": 0}`. The `/api/ps` shape is an **[assumption]**. PR 7 calls it at voice-session start; it is unit-tested here with a stubbed Ollama.
- While voice is active, route local jobs to gemma4, not qwen3.6: check `/health` (`active_sessions > 0` or `vram_free_mb < 10000`).
- If the CUDA load fails, STT degrades to CPU `int8` with the `small` model and `/health` says `degraded`.

## VRAM budget

Spec estimates (**[assumption]**, ~4–5 GB total: Whisper turbo 2–3 GB, Kokoro 0.5–1 GB, contexts 1–1.5 GB). `/health` reports live free/total from `nvidia-smi`; measure after startup and replace these numbers (acceptance C-2).

## Tests

```bash
pip install -r requirements-dev.txt   # or just: fastapi httpx numpy pytest pytest-asyncio python-multipart
cd voice-box && timeout 900 python -m pytest tests -q
```

Tests use fake model objects only: no GPU, no downloads.

## Only Tim can verify (on the box)

C-1 (`/health` from the Nous container shows `device: cuda` and a CUDA provider), C-2 (cold start ≤ 90 s, VRAM within ±1 GB of the table), C-4 against the real service, and gate G0 (`/stt` on a 10 s clip ≤ 1.5 s). The Kokoro `.create()` call signature, `.sess` attribute and model-file URL are **[assumption]** until then.

Path B (Docker Compose, spec C6) is not shipped in this PR.
