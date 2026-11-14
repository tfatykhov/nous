import pytest
import server
from conftest import AUTH, TOKEN
from fastapi.testclient import TestClient

BAD = {"Authorization": "Bearer nope"}


def test_health_shape_unauthenticated(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {
        "status",
        "version",
        "stt",
        "tts",
        "vram_free_mb",
        "vram_total_mb",
        "ollama_loaded",
        "active_sessions",
        "uptime_s",
    }
    assert body["status"] == "ok"
    assert body["stt"] == {"model": "turbo", "device": "cuda"}
    assert body["tts"] == {"engine": "kokoro", "provider": "CUDAExecutionProvider"}
    assert body["vram_free_mb"] == 9800 and body["vram_total_mb"] == 16376
    assert body["ollama_loaded"] == [] and body["active_sessions"] == 0
    assert TOKEN not in r.text


def test_health_degraded_and_down(engines):
    stt, tts = engines
    stt.degraded = True
    settings = server.Settings(token=TOKEN, preload=False, ollama_url="http://ollama.invalid")
    with TestClient(server.create_app(settings, stt, tts, lambda: None)) as c:
        body = c.get("/health").json()
    assert body["status"] == "degraded"
    assert body["vram_free_mb"] is None


def test_preload_failure_reports_down(engines):
    stt, tts = engines
    stt.load = lambda: (_ for _ in ()).throw(RuntimeError("secret detail"))
    settings = server.Settings(token=TOKEN, preload=True, ollama_url="http://ollama.invalid")
    with TestClient(server.create_app(settings, stt, tts, lambda: None)) as c:
        r = c.get("/health")
    assert r.json()["status"] == "down"
    assert "secret detail" not in r.text


def test_preload_loads_models(engines):
    stt, tts = engines
    settings = server.Settings(token=TOKEN, preload=True, ollama_url="http://ollama.invalid")
    with TestClient(server.create_app(settings, stt, tts, lambda: None)):
        pass
    assert stt.loaded == 1 and tts.loaded == 1


@pytest.mark.parametrize("headers", [{}, BAD, {"Authorization": "Basic abc"}])
def test_stt_rejects_bad_auth(client, engines, headers):
    r = client.post("/stt", files={"file": ("a.ogg", b"data")}, headers=headers)
    assert r.status_code == 401
    assert engines[0].calls == []


@pytest.mark.parametrize("headers", [{}, BAD])
def test_tts_rejects_bad_auth(client, engines, headers):
    r = client.post("/tts", json={"text": "hi"}, headers=headers)
    assert r.status_code == 401
    assert engines[1].calls == []


def test_stt_contract(client, engines):
    r = client.post("/stt", files={"file": ("a.ogg", b"opus-bytes")}, headers=AUTH)
    assert r.status_code == 200
    assert r.json() == {"text": "hello world", "language": "en", "duration_s": 1.5, "ms": 42}
    assert engines[0].calls == [b"opus-bytes"]


def test_stt_empty_and_oversize(engines):
    stt, tts = engines
    settings = server.Settings(token=TOKEN, preload=False, max_upload_mb=1)
    with TestClient(server.create_app(settings, stt, tts, lambda: None)) as c:
        assert c.post("/stt", files={"file": ("a", b"")}, headers=AUTH).status_code == 400
        big = b"x" * (1024 * 1024 + 1)
        assert c.post("/stt", files={"file": ("a", big)}, headers=AUTH).status_code == 413


def test_stt_failure_is_422_without_leak(client, engines):
    def boom(_):
        raise RuntimeError("cuda exploded /secret/path")

    engines[0].transcribe = boom
    r = client.post("/stt", files={"file": ("a", b"x")}, headers=AUTH)
    assert r.status_code == 422 and "secret" not in r.text


def test_tts_contract(client, engines):
    r = client.post("/tts", json={"text": "hi", "voice": "af_bella", "format": "wav"}, headers=AUTH)
    assert r.status_code == 200
    assert r.headers["content-type"] == "audio/wav" and r.content == b"RIFFfake"
    assert engines[1].calls == [("hi", "af_bella", "wav")]
    r = client.post("/tts", json={"text": "hi", "format": "pcm"}, headers=AUTH)
    assert r.status_code == 200 and r.content == b"\x00\x01"


def test_tts_validation(client):
    assert client.post("/tts", json={"text": ""}, headers=AUTH).status_code == 422
    assert client.post("/tts", json={"text": "x", "format": "mp3"}, headers=AUTH).status_code == 422


@pytest.mark.parametrize("tok", ["", "   "])
def test_empty_token_refuses_to_start(tok):
    with pytest.raises(RuntimeError, match="refusing to start"):
        server.Settings(token=tok)


def test_from_env_empty_token_refuses(monkeypatch):
    monkeypatch.delenv("VOICE_BOX_TOKEN", raising=False)
    with pytest.raises(RuntimeError):
        server.Settings.from_env()


def test_load_env_file(tmp_path, monkeypatch):
    f = tmp_path / ".env"
    f.write_text("# c\nVB_TEST_A=1\nVB_TEST_B=x=y\n")
    monkeypatch.setenv("VB_TEST_A", "keep")
    monkeypatch.delenv("VB_TEST_B", raising=False)
    server.load_env_file(str(f))
    import os

    assert os.environ["VB_TEST_A"] == "keep" and os.environ["VB_TEST_B"] == "x=y"
    monkeypatch.delenv("VB_TEST_B")
    server.load_env_file(str(tmp_path / "missing"))  # no raise


def test_body_gate_runs_before_parsing(engines):
    stt, tts = engines
    settings = server.Settings(token=TOKEN, preload=False, max_upload_mb=1)
    with TestClient(server.create_app(settings, stt, tts, lambda: None)) as c:
        big = b"x" * (3 * 1024 * 1024)
        # no token: 401 even for a huge body (never reaches the form parser)
        assert c.post("/stt", files={"file": ("a", big)}).status_code == 401
        assert c.post("/stt", files={"file": ("a", big)}, headers=AUTH).status_code == 413
        # no Content-Length (chunked) is refused
        r = c.post("/stt", content=iter([b"x"]), headers=AUTH)
        assert r.status_code == 411
