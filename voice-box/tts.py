"""Text-to-speech: Kokoro via kokoro-onnx, lazy-loaded behind an interface."""

from __future__ import annotations

import io
import logging
import threading
import wave
from collections.abc import Callable
from typing import Any, Protocol

log = logging.getLogger("voice-box.tts")

FORMATS = ("wav", "pcm")  # wav = 16-bit mono WAV; pcm = raw 16-bit LE mono
MEDIA_TYPES = {"wav": "audio/wav", "pcm": "application/octet-stream"}


class TtsEngine(Protocol):
    engine: str
    provider: str
    default_voice: str

    def load(self) -> None: ...
    def synthesize(self, text: str, voice: str | None, fmt: str) -> bytes: ...


def _to_pcm16(samples: Any) -> bytes:
    import numpy as np

    arr = np.clip(np.asarray(samples, dtype=np.float32), -1.0, 1.0)
    return (arr * 32767).astype("<i2").tobytes()


def _wav(pcm: bytes, rate: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


class KokoroTts:
    """kokoro-onnx. ``Kokoro(model_path, voices_path).create(text, voice, speed, lang)``
    returns ``(samples float32, sample_rate)`` [verified: spec, Context7 kokoro-onnx
    documents create_stream; ``create`` is the non-streaming sibling — [assumption]
    confirm on the box]. Audio stays in memory.
    """

    engine = "kokoro"

    def __init__(
        self,
        model_path: str,
        voices_path: str,
        default_voice: str = "af_nicole",
        factory: Callable[..., Any] | None = None,
    ) -> None:
        self._paths = (model_path, voices_path)
        self.default_voice = default_voice
        self.provider = "unloaded"
        self._factory = factory
        self._kokoro: Any = None
        self._lock = threading.Lock()

    def load(self) -> None:
        with self._lock:
            if self._kokoro is not None:
                return
            factory = self._factory
            if factory is None:
                from kokoro_onnx import Kokoro

                factory = Kokoro
            self._kokoro = factory(*self._paths)
            # [assumption] the onnxruntime session is exposed as ``.sess``
            sess = getattr(self._kokoro, "sess", None)
            providers = sess.get_providers() if sess is not None else []
            self.provider = providers[0] if providers else "unknown"

    def synthesize(self, text: str, voice: str | None, fmt: str) -> bytes:
        if fmt not in FORMATS:
            raise ValueError(f"unsupported format {fmt!r}")
        self.load()
        samples, rate = self._kokoro.create(text, voice=voice or self.default_voice, speed=1.0, lang="en-us")
        pcm = _to_pcm16(samples)
        return _wav(pcm, rate) if fmt == "wav" else pcm
