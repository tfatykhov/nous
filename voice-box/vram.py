"""VRAM probe and the Ollama coexistence rule (spec C3)."""

from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass

import httpx

log = logging.getLogger("voice-box.vram")


@dataclass(frozen=True)
class VramInfo:
    free_mb: int
    total_mb: int


def probe_nvidia_smi() -> VramInfo | None:
    """Read free/total VRAM of GPU 0 via nvidia-smi; None if unavailable."""
    exe = shutil.which("nvidia-smi")
    if exe is None:
        return None
    try:
        out = subprocess.run(
            [exe, "--query-gpu=memory.free,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        ).stdout.splitlines()[0]
        free, total = (int(x.strip()) for x in out.split(","))
    except (subprocess.SubprocessError, OSError, ValueError, IndexError):
        log.warning("nvidia-smi probe failed", exc_info=True)
        return None
    return VramInfo(free_mb=free, total_mb=total)


VramProbe = Callable[[], VramInfo | None]


async def ollama_loaded(ollama_url: str, client: httpx.AsyncClient) -> list[str] | None:
    """Names of models Ollama has resident (GET /api/ps).

    [assumption] /api/ps response shape is {"models": [{"name": ...}]}.
    Returns None when Ollama is unreachable or answers unexpectedly.
    """
    try:
        resp = await client.get(f"{ollama_url.rstrip('/')}/api/ps", timeout=3)
        resp.raise_for_status()
        return [m["name"] for m in resp.json().get("models", [])]
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        log.warning("ollama /api/ps unavailable")
        return None


async def free_vram_for_session(
    ollama_url: str,
    probe: VramProbe,
    min_free_mb: int = 5000,
    client: httpx.AsyncClient | None = None,
) -> list[str]:
    """Spec C3 rule 4: at voice session start, if free VRAM < min_free_mb,
    unload every model listed by Ollama /api/ps with keep_alive=0.

    Returns the names asked to unload (empty when nothing was needed or done).
    Never raises: a failure to talk to Ollama must not block a voice session.
    """
    info = await asyncio.to_thread(probe)
    if info is None or info.free_mb >= min_free_mb:
        return []
    owns = client is None
    client = client or httpx.AsyncClient()
    unloaded: list[str] = []
    try:
        for name in await ollama_loaded(ollama_url, client) or []:
            try:
                r = await client.post(
                    f"{ollama_url.rstrip('/')}/api/generate",
                    json={"model": name, "keep_alive": 0},
                    timeout=10,
                )
                r.raise_for_status()
                unloaded.append(name)
            except httpx.HTTPError:
                log.warning("could not unload ollama model %s", name)
    finally:
        if owns:
            await client.aclose()
    return unloaded
