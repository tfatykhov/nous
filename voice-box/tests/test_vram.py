import json

import httpx
from vram import VramInfo, free_vram_for_session, ollama_loaded

URL = "http://ollama.test"


def make_client(loaded, posts):
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/api/ps":
            if loaded is None:
                return httpx.Response(500)
            return httpx.Response(200, json={"models": [{"name": n} for n in loaded]})
        posts.append(json.loads(req.content))
        return httpx.Response(200, json={})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_unloads_all_when_vram_low():
    posts = []
    async with make_client(["qwen3.6:latest", "gemma4:latest"], posts) as c:
        out = await free_vram_for_session(URL, lambda: VramInfo(3000, 16376), 5000, c)
    assert out == ["qwen3.6:latest", "gemma4:latest"]
    assert posts == [{"model": "qwen3.6:latest", "keep_alive": 0}, {"model": "gemma4:latest", "keep_alive": 0}]


async def test_no_unload_when_enough_free():
    posts = []
    async with make_client(["gemma4:latest"], posts) as c:
        out = await free_vram_for_session(URL, lambda: VramInfo(5000, 16376), 5000, c)
    assert out == [] and posts == []


async def test_no_probe_means_no_action():
    posts = []
    async with make_client(["x"], posts) as c:
        assert await free_vram_for_session(URL, lambda: None, 5000, c) == []
    assert posts == []


async def test_ollama_down_does_not_raise():
    posts = []
    async with make_client(None, posts) as c:
        assert await free_vram_for_session(URL, lambda: VramInfo(1, 2), 5000, c) == []
        assert await ollama_loaded(URL, c) is None
