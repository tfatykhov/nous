"""Voice box HTTP service (spec C4): /health, /stt, /tts on port 7860.

PR 1 scope only: no Pipecat pipeline, no /api/offer (PR 7).
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import os
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass

import httpx
from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from health import build_health
from pydantic import BaseModel, Field
from starlette.formparsers import MultiPartParser
from stt import SttEngine, WhisperStt
from tts import FORMATS, MEDIA_TYPES, KokoroTts, TtsEngine
from vram import VramProbe, ollama_loaded, probe_nvidia_smi

log = logging.getLogger("voice-box")


@dataclass(frozen=True)
class Settings:
    token: str
    host: str = "0.0.0.0"
    port: int = 7860
    ollama_url: str = "http://localhost:11434"
    preload: bool = True
    max_upload_mb: int = 25

    def __post_init__(self) -> None:
        if not self.token.strip():
            raise RuntimeError("VOICE_BOX_TOKEN is empty; refusing to start")

    @classmethod
    def from_env(cls) -> Settings:
        e = os.environ
        return cls(
            token=e.get("VOICE_BOX_TOKEN", ""),
            host=e.get("VOICE_BOX_HOST", "0.0.0.0"),
            port=int(e.get("VOICE_BOX_PORT", "7860")),
            ollama_url=e.get("OLLAMA_URL", "http://localhost:11434"),
            preload=e.get("VOICE_BOX_PRELOAD", "1") != "0",
            max_upload_mb=int(e.get("VOICE_BOX_MAX_UPLOAD_MB", "25")),
        )


class TtsRequest(BaseModel):
    text: str = Field(min_length=1, max_length=5000)
    voice: str | None = None
    format: str = "wav"


def create_app(
    settings: Settings,
    stt: SttEngine,
    tts: TtsEngine,
    vram_probe: VramProbe = probe_nvidia_smi,
) -> FastAPI:
    state = {"load_error": None, "active_sessions": 0}
    started = time.monotonic()
    bearer = HTTPBearer(auto_error=False)
    expected = settings.token.encode()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if settings.preload:  # hold VRAM from startup (spec C3 rule 1)
            try:
                await asyncio.to_thread(stt.load)
                await asyncio.to_thread(tts.load)
            except Exception as exc:
                log.exception("model preload failed")
                state["load_error"] = type(exc).__name__  # class only, no details
        yield

    app = FastAPI(title="nous-voice-box", lifespan=lifespan)

    # Starlette spools multipart parts above 1 MB to a temp file. Keep them in
    # memory up to the upload cap (enforced below before any body is read).
    MultiPartParser.spool_max_size = settings.max_upload_mb * 1024 * 1024 + 1024 * 1024
    body_limit = MultiPartParser.spool_max_size

    @app.middleware("http")
    async def gate_protected_bodies(request: Request, call_next):
        """Authenticate and size-check /stt and /tts BEFORE FastAPI parses the body."""
        if request.url.path in ("/stt", "/tts"):
            auth = request.headers.get("authorization", "")
            scheme, _, given = auth.partition(" ")
            ok = scheme.lower() == "bearer" and hmac.compare_digest(given.encode(), expected)
            if not ok:
                return JSONResponse({"detail": "unauthorized"}, 401, headers={"WWW-Authenticate": "Bearer"})
            length = request.headers.get("content-length")
            if length is None or not length.isdigit():
                return JSONResponse({"detail": "content-length required"}, 411)
            if int(length) > body_limit:
                return JSONResponse({"detail": "body too large"}, 413)
        return await call_next(request)

    def require_token(
        creds: HTTPAuthorizationCredentials | None = Depends(bearer),
    ) -> None:
        given = creds.credentials.encode() if creds else b""
        # compare_digest on bytes; evaluated even when the header is missing
        if not hmac.compare_digest(given, expected) or creds is None:
            raise HTTPException(401, "unauthorized", headers={"WWW-Authenticate": "Bearer"})

    @app.get("/health")
    async def health() -> JSONResponse:
        async with httpx.AsyncClient() as client:
            loaded = await ollama_loaded(settings.ollama_url, client)
        payload = build_health(stt, tts, vram_probe(), loaded, state["active_sessions"], started, state["load_error"])
        return JSONResponse(payload)

    @app.post("/stt", dependencies=[Depends(require_token)])
    async def stt_route(file: UploadFile = File(...)) -> dict:
        limit = settings.max_upload_mb * 1024 * 1024
        audio = await file.read(limit + 1)
        if not audio:
            raise HTTPException(400, "empty audio")
        if len(audio) > limit:
            raise HTTPException(413, "audio too large")
        try:
            t = await asyncio.to_thread(stt.transcribe, audio)
        except Exception:
            log.exception("transcription failed")  # never log audio or text
            raise HTTPException(422, "could not transcribe audio") from None
        return {"text": t.text, "language": t.language, "duration_s": t.duration_s, "ms": t.ms}

    @app.post("/tts", dependencies=[Depends(require_token)])
    async def tts_route(req: TtsRequest) -> Response:
        if req.format not in FORMATS:
            raise HTTPException(422, f"format must be one of {list(FORMATS)}")
        try:
            audio = await asyncio.to_thread(tts.synthesize, req.text, req.voice, req.format)
        except Exception:
            log.exception("synthesis failed")
            raise HTTPException(422, "could not synthesize") from None
        return Response(audio, media_type=MEDIA_TYPES[req.format])

    return app


def load_env_file(path: str) -> None:
    """Minimal KEY=VALUE loader (no dependency). Real env vars win. Missing file is fine."""
    try:
        lines = open(path, encoding="utf-8").read().splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def main() -> None:
    import uvicorn

    load_env_file(os.environ.get("VOICE_BOX_ENV_FILE", ".env"))
    logging.basicConfig(level=logging.INFO)
    settings = Settings.from_env()  # raises on empty token
    model_dir = os.environ.get("VOICE_BOX_MODEL_DIR", ".")
    stt = WhisperStt(
        os.environ.get("VOICE_BOX_STT_MODEL", "turbo"),
        os.environ.get("VOICE_BOX_STT_DEVICE", "cuda"),
        os.environ.get("VOICE_BOX_STT_COMPUTE", "float16"),
    )
    tts = KokoroTts(
        os.path.join(model_dir, "kokoro-v1.0.onnx"),
        os.path.join(model_dir, "voices-v1.0.bin"),
        os.environ.get("VOICE_BOX_TTS_VOICE", "af_nicole"),
    )
    uvicorn.run(create_app(settings, stt, tts), host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
