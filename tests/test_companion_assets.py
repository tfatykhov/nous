"""Persistent companion assets: the overlay directory behind /dashboard/v2.

Tested against `OverlayStaticFiles` directly, over temporary directories,
rather than through `create_app`: the production mount exists only when the
dashboard build is present, and a test that depends on a Node build being on
disk passes or fails for reasons unrelated to the code under test.

The failure this exists for: on prod, 2026-09-28, the Italy app's photos
disappeared at a rebuild because the agent had stored them in the image's own
static directory — the only served directory it had.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from starlette.applications import Starlette
from starlette.routing import Mount

from nous.api.companion_assets import ALLOWED_EXTENSIONS, OverlayStaticFiles

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


@pytest.fixture
def dirs(tmp_path: Path) -> tuple[Path, Path]:
    dist = tmp_path / "dist"
    overlay = tmp_path / "workspace" / "companion-assets"
    dist.mkdir()
    overlay.mkdir(parents=True)
    (dist / "index.html").write_text("<html>app</html>", encoding="utf-8")
    (dist / "favicon.svg").write_text("<svg/>", encoding="utf-8")
    return dist, overlay


def _client(dist: Path, overlay: Path | None) -> AsyncClient:
    static = OverlayStaticFiles(
        directory=str(dist), overlay_dir=str(overlay) if overlay else None, html=True
    )
    app = Starlette(routes=[Mount("/dashboard/v2", app=static)])
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_an_overlay_image_is_served_at_the_url_surfaces_already_use(dirs) -> None:
    """The point of the feature: same URL, different (persistent) directory."""
    dist, overlay = dirs
    (overlay / "italy-maps").mkdir()
    (overlay / "italy-maps" / "photo_dobbiaco.jpg").write_bytes(PNG)

    async with _client(dist, overlay) as client:
        response = await client.get("/dashboard/v2/italy-maps/photo_dobbiaco.jpg")

    assert response.status_code == 200
    assert response.content == PNG
    assert response.headers["x-content-type-options"] == "nosniff"


async def test_the_application_bundle_always_wins_a_name_clash(dirs) -> None:
    """An overlay file must never be able to replace part of the app."""
    dist, overlay = dirs
    (overlay / "favicon.svg").write_text("<svg>agent</svg>", encoding="utf-8")

    async with _client(dist, overlay) as client:
        response = await client.get("/dashboard/v2/favicon.svg")

    assert response.text == "<svg/>"
    assert "content-security-policy" not in response.headers, "dist files are not sandboxed"


@pytest.mark.parametrize("name", ["evil.html", "evil.js", "evil.mjs", "evil.css", "data.json", "noext"])
async def test_the_overlay_serves_nothing_outside_the_allowlist(dirs, name: str) -> None:
    """Same-origin HTML or script from an agent-writable directory is the risk.

    What an agent writes can originate from a fetched web page, and this
    origin also serves /a2ui/action.
    """
    dist, overlay = dirs
    (overlay / name).write_text("alert(1)", encoding="utf-8")

    async with _client(dist, overlay) as client:
        response = await client.get(f"/dashboard/v2/{name}")

    assert response.status_code == 404


async def test_an_overlay_directory_never_serves_an_index_page(dirs) -> None:
    """`html=True` would otherwise turn overlay/x/index.html into a page."""
    dist, overlay = dirs
    (overlay / "trip").mkdir()
    (overlay / "trip" / "index.html").write_text("<script>1</script>", encoding="utf-8")

    async with _client(dist, overlay) as client:
        for path in ("/dashboard/v2/trip", "/dashboard/v2/trip/", "/dashboard/v2/trip/index.html"):
            response = await client.get(path, follow_redirects=True)
            assert response.status_code == 404, path


async def test_an_overlay_svg_is_sandboxed(dirs) -> None:
    """In an <img> an SVG cannot run script; opened directly it can."""
    dist, overlay = dirs
    (overlay / "map.svg").write_text("<svg><script>1</script></svg>", encoding="utf-8")

    async with _client(dist, overlay) as client:
        response = await client.get("/dashboard/v2/map.svg")

    assert response.status_code == 200
    assert "sandbox" in response.headers["content-security-policy"]


async def test_a_path_cannot_climb_out_of_the_overlay(dirs, tmp_path: Path) -> None:
    dist, overlay = dirs
    (tmp_path / "workspace" / "secret.png").write_bytes(PNG)

    async with _client(dist, overlay) as client:
        response = await client.get("/dashboard/v2/%2e%2e/secret.png")

    assert response.status_code == 404


async def test_a_missing_overlay_directory_is_not_an_error(dirs, tmp_path: Path) -> None:
    """The agent may not have created it yet; the app must still serve."""
    dist, _ = dirs

    async with _client(dist, tmp_path / "does-not-exist") as client:
        assert (await client.get("/dashboard/v2/index.html")).status_code == 200
        assert (await client.get("/dashboard/v2/italy-maps/x.jpg")).status_code == 404


async def test_no_overlay_configured_behaves_like_plain_static_files(dirs) -> None:
    dist, _ = dirs

    async with _client(dist, None) as client:
        assert (await client.get("/dashboard/v2/index.html")).status_code == 200


async def test_an_asset_added_after_startup_is_served(dirs) -> None:
    """Lookup is per request, so a new mirror needs no restart."""
    dist, overlay = dirs

    async with _client(dist, overlay) as client:
        assert (await client.get("/dashboard/v2/late.png")).status_code == 404
        (overlay / "late.png").write_bytes(PNG)
        assert (await client.get("/dashboard/v2/late.png")).status_code == 200


def test_the_allowlist_holds_no_executable_type() -> None:
    assert not ALLOWED_EXTENSIONS & {".html", ".htm", ".js", ".mjs", ".css", ".json", ".xml", ".wasm"}
