"""Speech-to-text: faster-whisper on CUDA, lazy-loaded behind an interface."""

from __future__ import annotations

import io
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

log = logging.getLogger("voice-box.stt")


@dataclass(frozen=True)
class Transcript:
    text: str
    language: str
    duration_s: float
    ms: int


class SttEngine(Protocol):
    model_name: str
    device: str
    degraded: bool

    def load(self) -> None: ...
    def transcribe(self, audio: bytes) -> Transcript: ...


class WhisperStt:
    """faster-whisper behind a lazy loader.

    Spec C3 rule 5: if the CUDA load fails, fall back to CPU int8 with a
    smaller model and report ``degraded``. Audio is decoded from memory
    (BytesIO); nothing is written to disk.
    """

    FALLBACK_MODEL = "small"  # [assumption] adequate CPU fallback size

    def __init__(
        self,
        model: str = "turbo",
        device: str = "cuda",
        compute_type: str = "float16",
        factory: Callable[..., Any] | None = None,
    ) -> None:
        self.model_name = model
        self.device = device
        self.compute_type = compute_type
        self.degraded = False
        self._factory = factory
        self._model: Any = None
        self._lock = threading.Lock()

    def _make(self, *args: Any, **kwargs: Any) -> Any:
        factory = self._factory
        if factory is None:
            from faster_whisper import WhisperModel  # heavy; imported lazily

            factory = WhisperModel
        return factory(*args, **kwargs)

    def load(self) -> None:
        with self._lock:
            if self._model is not None:
                return
            try:
                self._model = self._make(self.model_name, device=self.device, compute_type=self.compute_type)
            except Exception:  # CUDA OOM / missing libs surface as various types
                log.exception("STT load on %s failed; falling back to CPU int8", self.device)
                self.model_name, self.device, self.compute_type = self.FALLBACK_MODEL, "cpu", "int8"
                self.degraded = True
                self._model = self._make(self.model_name, device=self.device, compute_type=self.compute_type)

    def transcribe(self, audio: bytes) -> Transcript:
        self.load()
        t0 = time.perf_counter()
        segments, info = self._model.transcribe(io.BytesIO(audio), beam_size=1)
        text = " ".join(s.text.strip() for s in segments).strip()
        return Transcript(
            text=text,
            language=getattr(info, "language", "") or "",
            duration_s=round(float(getattr(info, "duration", 0.0)), 3),
            ms=int((time.perf_counter() - t0) * 1000),
        )
