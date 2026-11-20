import io
import wave
from types import SimpleNamespace

import numpy as np
from stt import WhisperStt
from tts import KokoroTts


class FakeModel:
    def transcribe(self, audio, beam_size=1):
        assert isinstance(audio, io.BytesIO)  # in-memory, never a path
        segs = [SimpleNamespace(text=" hello"), SimpleNamespace(text="world ")]
        return iter(segs), SimpleNamespace(language="en", duration=2.0)


def test_whisper_lazy_and_transcribes():
    made = []
    stt = WhisperStt(factory=lambda *a, **k: made.append((a, k)) or FakeModel())
    assert made == []  # lazy
    t = stt.transcribe(b"abc")
    assert (t.text, t.language, t.duration_s) == ("hello world", "en", 2.0)
    stt.transcribe(b"abc")
    assert len(made) == 1 and made[0][1]["device"] == "cuda"


def test_whisper_cuda_failure_degrades_to_cpu():
    calls = []

    def factory(name, device, compute_type):
        calls.append((name, device, compute_type))
        if device == "cuda":
            raise RuntimeError("CUDA out of memory")
        return FakeModel()

    stt = WhisperStt(factory=factory)
    stt.load()
    assert stt.degraded and stt.device == "cpu"
    assert calls[-1] == ("small", "cpu", "int8")


class FakeKokoro:
    sess = SimpleNamespace(get_providers=lambda: ["CUDAExecutionProvider", "CPUExecutionProvider"])

    def create(self, text, voice, speed, lang):
        return np.array([0.0, 0.5, -0.5], dtype=np.float32), 24000


def test_kokoro_wav_and_pcm():
    tts = KokoroTts("m", "v", factory=lambda *a: FakeKokoro())
    assert tts.provider == "unloaded"
    wav = tts.synthesize("hi", None, "wav")
    assert tts.provider == "CUDAExecutionProvider"
    with wave.open(io.BytesIO(wav)) as w:
        assert (w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()) == (1, 2, 24000, 3)
    assert len(tts.synthesize("hi", "af_bella", "pcm")) == 6
