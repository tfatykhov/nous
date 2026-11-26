"""Build the /health payload (spec C4). No secrets, no model loading."""

from __future__ import annotations

import time
from typing import Any

from stt import SttEngine
from tts import TtsEngine
from vram import VramInfo

VERSION = "0.1.0"


def build_health(
    stt: SttEngine,
    tts: TtsEngine,
    vram: VramInfo | None,
    ollama_loaded: list[str] | None,
    active_sessions: int,
    started_at: float,
    load_error: str | None = None,
) -> dict[str, Any]:
    if load_error:
        status = "down"
    elif stt.degraded:
        status = "degraded"
    else:
        status = "ok"
    return {
        "status": status,
        "version": VERSION,
        "stt": {"model": stt.model_name, "device": stt.device},
        "tts": {"engine": tts.engine, "provider": tts.provider},
        "vram_free_mb": vram.free_mb if vram else None,
        "vram_total_mb": vram.total_mb if vram else None,
        "ollama_loaded": ollama_loaded if ollama_loaded is not None else [],
        "active_sessions": active_sessions,
        "uptime_s": int(time.monotonic() - started_at),
    }
