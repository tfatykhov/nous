"""Persistent, agent-hosted assets for the companion and dashboard.

The `/dashboard/v2` mount serves `static/dashboard-v2/dist`, which is baked
into the Docker image. An agent that wanted to show its own images (map
tiles, photos mirrored for a trip app) had exactly one served directory to
put them in — that one — and everything written there lives in the
container's writable layer, so it vanished on every rebuild. Observed on
prod 2026-09-28: the Italy app's photos were gone after a restart while the
originals sat untouched in the workspace volume.

This adds a second directory to the same mount, inside the workspace volume,
so an asset keeps the URL it already has and survives a rebuild.

Two rules, both deliberate:

- **The image's own `dist` is searched first.** An overlay file can never
  shadow the application bundle.
- **The overlay serves an allowlist of media types, nothing else.** It is a
  persistent, agent-writable directory served on the dashboard's own origin,
  and what an agent writes can originate from a fetched web page. HTML or
  JavaScript served from there would run with the same origin as
  `/a2ui/action`. So: raster images, and SVG behind a sandboxing CSP (an SVG
  in an `<img>` cannot run script; one opened directly can).
"""

from __future__ import annotations

import os
import stat
from typing import Any

from starlette.responses import FileResponse, Response
from starlette.staticfiles import StaticFiles
from starlette.types import Scope

# An allowlist, not a denylist: a new executable type must not become
# servable by default.
ALLOWED_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif", ".avif", ".svg"})

_SVG_CSP = "sandbox; default-src 'none'; style-src 'unsafe-inline'"


class OverlayStaticFiles(StaticFiles):
    """`StaticFiles` over a primary directory plus a persistent overlay."""

    def __init__(self, *, directory: Any, overlay_dir: str | None = None, **kwargs: Any) -> None:
        super().__init__(directory=directory, **kwargs)
        self._overlay_dir = overlay_dir or None
        if self._overlay_dir:
            # Appended, so the primary directory always wins a name clash.
            # It need not exist yet: lookup_path skips a missing directory.
            self.all_directories = [*self.all_directories, self._overlay_dir]

    def _from_overlay(self, full_path: str) -> bool:
        if not self._overlay_dir or not full_path:
            return False
        try:
            overlay = os.path.realpath(self._overlay_dir)
            return os.path.commonpath([os.path.realpath(full_path), overlay]) == overlay
        except ValueError:
            # Different drives (Windows): certainly not inside the overlay.
            return False

    def lookup_path(self, path: str) -> tuple[str, os.stat_result | None]:
        full_path, stat_result = super().lookup_path(path)
        if stat_result is None or not self._from_overlay(full_path):
            return full_path, stat_result
        extension = os.path.splitext(full_path)[1].lower()
        if not stat.S_ISREG(stat_result.st_mode) or extension not in ALLOWED_EXTENSIONS:
            # A directory (no index pages from the overlay) or a type that is
            # not on the allowlist: to the client it does not exist.
            return "", None
        return full_path, stat_result

    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        if isinstance(response, FileResponse) and self._from_overlay(str(response.path)):
            response.headers["X-Content-Type-Options"] = "nosniff"
            if str(response.path).lower().endswith(".svg"):
                response.headers["Content-Security-Policy"] = _SVG_CSP
        return response
