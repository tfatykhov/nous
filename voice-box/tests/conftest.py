import pytest
import server
from fastapi.testclient import TestClient
from stt import Transcript
from vram import VramInfo

TOKEN = "t0ken-for-tests"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


class FakeStt:
    model_name = "turbo"
    device = "cuda"
    degraded = False

    def __init__(self):
        self.loaded = 0
        self.calls = []

    def load(self):
        self.loaded += 1

    def transcribe(self, audio):
        self.calls.append(audio)
        return Transcript(text="hello world", language="en", duration_s=1.5, ms=42)


class FakeTts:
    engine = "kokoro"
    provider = "CUDAExecutionProvider"
    default_voice = "af_nicole"

    def __init__(self):
        self.loaded = 0
        self.calls = []

    def load(self):
        self.loaded += 1

    def synthesize(self, text, voice, fmt):
        self.calls.append((text, voice, fmt))
        return b"RIFFfake" if fmt == "wav" else b"\x00\x01"


@pytest.fixture
def engines():
    return FakeStt(), FakeTts()


@pytest.fixture
def client(engines):
    stt, tts = engines
    settings = server.Settings(token=TOKEN, preload=False, ollama_url="http://ollama.invalid")
    app = server.create_app(settings, stt, tts, vram_probe=lambda: VramInfo(9800, 16376))
    with TestClient(app) as c:
        yield c
