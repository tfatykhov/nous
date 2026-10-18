"""Playwright session for browser_task: one headless Chromium, its tabs, and the domain allowlist."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from types import TracebackType
from typing import Any
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

# 1280x800 fits the model's image limits as is, so screenshots need no rescaling
# and the model's coordinates map 1:1 to viewport pixels.
VIEWPORT = {"width": 1280, "height": 800}
# /dev/shm is 64M in the container; Chromium crashes on big pages without this.
CHROMIUM_ARGS = ["--disable-dev-shm-usage"]

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f  ]")


def clean_text(value: str, limit: int) -> str:
    """Strip control characters (the API rejects them in browser_state) and cap the length."""
    return _CONTROL_CHARS.sub(" ", value or "")[:limit]


class DomainAllowlist:
    """Hosts the browser may reach: each entry and its subdomains, over http or https only."""

    def __init__(self, domains: list[str]) -> None:
        hosts = {h for h in (self._normalize(d) for d in domains) if h}
        if not hosts:
            raise ValueError("allowed_domains must name at least one host")
        self.hosts = frozenset(hosts)

    @staticmethod
    def _normalize(entry: str) -> str:
        entry = (entry or "").strip().lower()
        if "://" in entry:
            entry = urlsplit(entry).hostname or ""
        entry = entry.split("/")[0].removeprefix("*.").strip(".")
        return entry.split(":")[0] if entry.count(":") == 1 else entry

    def allows(self, url: str) -> bool:
        try:
            parts = urlsplit(url)
            host = (parts.hostname or "").lower().rstrip(".")
        except ValueError:
            return False
        if parts.scheme not in ("http", "https") or not host:
            return False
        return any(host == h or host.endswith("." + h) for h in self.hosts)


class BrowserSession:
    """Headless Chromium with a fresh profile, a tab registry and allowlist routing.

    Every request the context makes goes through `_route`: anything off the
    allowlist is aborted. Playwright does not route the redirects of a request
    it already let through, so callers re-check page URLs after each action
    (`off_allowlist_tabs`). Service workers (which bypass routing) and
    WebSockets are blocked; downloads are refused.
    """

    def __init__(self, allowlist: DomainAllowlist, artifacts_dir: Path) -> None:
        self.allowlist = allowlist
        self.artifacts_dir = artifacts_dir
        self.blocked_requests = 0
        self.tabs: dict[str, Any] = {}  # tab_id -> playwright Page, in opening order
        self.active_tab: str | None = None
        self.opened_during_call: list[str] = []
        self._next_tab = 1
        self._playwright: Any = None
        self._browser: Any = None
        self._context: Any = None

    async def __aenter__(self) -> BrowserSession:
        from playwright.async_api import async_playwright

        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        self._playwright = await async_playwright().start()
        try:
            self._browser = await self._playwright.chromium.launch(headless=True, args=CHROMIUM_ARGS)
            self._context = await self._browser.new_context(
                viewport=VIEWPORT,
                service_workers="block",
                accept_downloads=False,
            )
            await self._context.route("**/*", self._route)
            await self._context.route_web_socket("**/*", self._block_websocket)
            self._context.on("page", self._on_page)
            await self._context.new_page()  # tab-1, registered by _on_page
            self.opened_during_call = []  # the first tab is not a side effect of any call
        except BaseException:
            await self.__aexit__(None, None, None)
            raise
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        for closer in (self._context, self._browser):
            if closer is not None:
                try:
                    await closer.close()
                except Exception:
                    logger.debug("browser_task: close failed", exc_info=True)
        if self._playwright is not None:
            await self._playwright.stop()
        self._context = self._browser = self._playwright = None

    async def _route(self, route: Any) -> None:
        if self.allowlist.allows(route.request.url):
            await route.continue_()
            return
        self.blocked_requests += 1
        logger.info("browser_task: blocked request to %s", urlsplit(route.request.url).hostname)
        await route.abort("blockedbyclient")

    async def _block_websocket(self, ws: Any) -> None:
        self.blocked_requests += 1
        await ws.close()

    def _on_page(self, page: Any) -> None:
        tab_id = f"tab-{self._next_tab}"
        self._next_tab += 1
        self.tabs[tab_id] = page
        self.opened_during_call.append(tab_id)
        if self.active_tab is None:
            self.active_tab = tab_id
        page.on("close", lambda _page, tid=tab_id: self._forget(tid))

    def _forget(self, tab_id: str) -> None:
        self.tabs.pop(tab_id, None)
        if tab_id in self.opened_during_call:
            self.opened_during_call.remove(tab_id)
        if self.active_tab == tab_id:
            self.active_tab = next(reversed(self.tabs), None)

    @property
    def active_page(self) -> Any:
        if self.active_tab is None:
            raise RuntimeError("no open tab")
        return self.tabs[self.active_tab]

    def page(self, tab_id: str | None) -> Any:
        if tab_id is None:
            return self.active_page
        if tab_id not in self.tabs:
            raise KeyError(tab_id)
        return self.tabs[tab_id]

    async def new_tab(self) -> str:
        page = await self._context.new_page()
        tab_id = next(t for t, p in self.tabs.items() if p is page)
        self.active_tab = tab_id
        return tab_id

    def off_allowlist_tabs(self) -> list[str]:
        return [t for t, p in self.tabs.items() if p.url != "about:blank" and not self.allowlist.allows(p.url)]

    async def browser_state(self, *, with_changes: bool = True) -> dict[str, Any]:
        """The full tab inventory as a browser_state block; page-supplied text is cleaned."""
        tabs = []
        for tab_id, page in self.tabs.items():
            try:
                title = await page.title()
            except Exception:
                title = ""
            entry: dict[str, Any] = {
                "tab_id": tab_id,
                "title": clean_text(title, 200),
                "url": clean_text(_strip_userinfo(page.url), 2000),
            }
            if tab_id == self.active_tab:
                entry["active"] = True
            tabs.append(entry)
        block: dict[str, Any] = {"type": "browser_state", "tabs": tabs}
        if with_changes and self.opened_during_call:
            block["state_changes"] = [{"type": "tab_opened", "tab_id": t} for t in self.opened_during_call]
        self.opened_during_call = []
        return block


def _strip_userinfo(url: str) -> str:
    parts = urlsplit(url)
    if "@" not in parts.netloc:
        return url
    return parts._replace(netloc=parts.netloc.rsplit("@", 1)[1]).geturl()
