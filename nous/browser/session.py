"""Playwright session for browser_task: one headless Chromium, its tabs, the domain allowlist and its egress proxy."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import re
import secrets
from collections.abc import Iterable
from pathlib import Path
from types import TracebackType
from typing import Any
from urllib.parse import urlsplit

from nous.browser import egress

logger = logging.getLogger(__name__)

# 1280x800 fits the model's image limits as is, so screenshots need no rescaling
# and the model's coordinates map 1:1 to viewport pixels.
VIEWPORT = {"width": 1280, "height": 800}
CHROMIUM_ARGS = [
    # /dev/shm is 64M in the container; Chromium crashes on big pages without this.
    "--disable-dev-shm-usage",
    # Loopback hosts go through the egress proxy too. Playwright adds this itself unless an environment
    # variable turns that off; ours comes later on the command line, so it wins.
    "--proxy-bypass-list=<-loopback>",
    # WebRTC (STUN, TURN, data channels) would otherwise send UDP around the proxy.
    "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
    # With an HTTP proxy Chromium needs no DNS of its own: every host name goes to the proxy. Failing every
    # lookup but the proxy's address stops DNS prefetch from leaking names (Chromium 156 has no switch
    # that turns prefetch off).
    "--host-resolver-rules=MAP * ~NOTFOUND , EXCLUDE 127.0.0.1",
]
# Chromium runs as this user when Nous runs as root (the container), through the setpriv wrapper.
BROWSER_USER = "nous-browser"
CHROMIUM_WRAPPER = Path(__file__).with_name("chromium-as-user.sh")
SETPRIV = "/usr/bin/setpriv"
# Each teardown step gets this long before Chromium is killed.
CLOSE_STEP_TIMEOUT = 5.0
# Resolving the allowlist at task start; the egress proxy checks every connection again.
START_RESOLVE_TIMEOUT = 5.0

_CONTROL_CHARS = re.compile("[\x00-\x1f\x7f  ]")
_LABEL = re.compile(r"[a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9_])?")
# The last label starts with a letter: a browser reads a name whose last label is a number (decimal, octal
# or 0x-hex, as in 0xa9.0xfe.0xa9.0xfe, 2852039166 or 127.1) as an IPv4 address.
_TOP_LABEL = re.compile(r"[a-z][a-z0-9-]*")
_LOCAL_NAMES = ("localhost", "internal", "local", "lan", "home.arpa")
_root_fallback_warned = False


def clean_text(value: str, limit: int) -> str:
    """Strip control characters (the API rejects them in browser_state) and cap the length."""
    return _CONTROL_CHARS.sub(" ", value or "")[:limit]


def host_problem(host: str) -> str | None:
    """Why the browser may never be pointed at `host`, or None. A host is a public IP address in canonical
    form, or a public DNS name. What a name resolves to is checked separately (`check_resolution` at task
    start, the egress proxy on every connection)."""
    address = _ip(host)
    if address is not None:
        return None if egress.address_permitted(host, address) else "is not a public address"
    labels = host.split(".")
    if len(labels) < 2:
        return "is a single label (a bare suffix or a local name), not a public host"
    if not all(_LABEL.fullmatch(label) for label in labels):
        return "is not a valid host name"
    if not _TOP_LABEL.fullmatch(labels[-1]):
        return "does not end in an alphabetic label, so a browser reads it as an IP address"
    if any(host == name or host.endswith("." + name) for name in _LOCAL_NAMES):
        return "names a local or internal network"
    return None


async def check_resolution(hosts: Iterable[str]) -> None:
    """Raise ValueError for a host name that does not resolve, or resolves to any non-public address.
    Runs once at task start, before the browser launches."""
    names = sorted({h for h in hosts if _ip(h) is None})
    problems = await asyncio.gather(*(_resolution_problem(name) for name in names))
    refused = [f"{name!r} {why}" for name, why in zip(names, problems, strict=True) if why]
    if refused:
        raise ValueError(f"allowed_domains refused: {'; '.join(refused)}.")


async def _resolution_problem(host: str) -> str | None:
    try:
        addresses = await asyncio.wait_for(egress.resolve(host), START_RESOLVE_TIMEOUT)
    except (OSError, TimeoutError, ValueError):
        addresses = []
    if not addresses:
        return "does not resolve"
    private = [str(a) for a in addresses if not egress.address_permitted(host, a)]
    if private:
        logger.info("browser_task: %s resolves to non-public %s", host, ", ".join(private))
        return "resolves to a non-public address"
    return None


class DomainAllowlist:
    """Hosts the browser may reach: each entry and its subdomains, over http or https only."""

    def __init__(self, domains: list[str]) -> None:
        hosts = {h for h in (self._normalize(d) for d in domains) if h}
        if not hosts:
            raise ValueError("allowed_domains must name at least one host")
        refused = [f"{h!r} {why}" for h in sorted(hosts) if (why := host_problem(h))]
        if refused:
            raise ValueError(
                f"allowed_domains refused: {'; '.join(refused)}. "
                "Entries must be public host names or public IP addresses."
            )
        self.hosts = frozenset(hosts)

    @staticmethod
    def _normalize(entry: str) -> str:
        entry = (entry or "").strip().lower()
        if "://" in entry:
            try:
                entry = urlsplit(entry).hostname or ""
            except ValueError:
                return entry  # refused by host_problem
        entry = entry.split("/")[0].removeprefix("*.").strip(".")
        if entry.startswith("["):
            entry = entry[1:].split("]")[0]
        elif entry.count(":") == 1:
            entry = entry.split(":")[0]
        address = _ip(entry)
        return str(address) if address is not None else entry  # IPv6 as browsers print it

    def allows(self, url: str) -> bool:
        try:
            parts = urlsplit(url)
            host = (parts.hostname or "").lower().rstrip(".")
        except ValueError:
            return False
        if parts.scheme not in ("http", "https") or not host:
            return False
        return any(host == h or host.endswith("." + h) for h in self.hosts)


def _ip(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


def _browser_user_launch(playwright: Any) -> dict[str, Any]:
    """Launch options that run Chromium as BROWSER_USER through the setpriv wrapper. Empty when that is not
    possible (not root, as in local development and tests, or no such user): Chromium then runs as the
    current user, with a WARNING logged once."""
    global _root_fallback_warned
    reason = _browser_user_unavailable()
    if reason is None:
        return {
            "executable_path": str(CHROMIUM_WRAPPER),
            # Replaces the environment: the wrapper needs only these, and setpriv clears Chromium's.
            "env": {"NOUS_BROWSER_CHROMIUM": _chromium_binary(playwright), "PATH": "/usr/sbin:/usr/bin:/sbin:/bin"},
        }
    if not _root_fallback_warned:
        _root_fallback_warned = True
        logger.warning("browser_task: Chromium runs as the current user, not %s: %s", BROWSER_USER, reason)
    return {}


def _browser_user_unavailable() -> str | None:
    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        return "Nous is not running as root"
    import pwd  # POSIX only, and only reached as root

    try:
        pwd.getpwnam(BROWSER_USER)
    except KeyError:
        return f"there is no {BROWSER_USER} user"
    if not os.access(CHROMIUM_WRAPPER, os.X_OK) or not os.access(SETPRIV, os.X_OK):
        return f"{CHROMIUM_WRAPPER} or {SETPRIV} is not executable"
    return None


def _chromium_binary(playwright: Any) -> str:
    """The binary `launch(headless=True)` runs on its own: Playwright's headless shell of the same
    revision as its Chromium, or that Chromium when the shell is not installed."""
    full = Path(playwright.chromium.executable_path)  # <browsers>/chromium-<rev>/chrome-linux64/chrome
    revision = full.parent.parent.name.removeprefix("chromium-")
    shells = sorted(
        full.parent.parent.parent.glob(
            f"chromium_headless_shell-{revision}/chrome-headless-shell-*/chrome-headless-shell"
        )
    )
    return str(shells[0]) if shells else str(full)


def _kill_chromium(marker: str) -> None:
    """Last resort when teardown hangs: SIGKILL the process group of the Chromium launched with `marker` on
    its command line. Linux only (it reads /proc); elsewhere Playwright's own cleanup is all there is."""
    import signal

    proc = Path("/proc")
    if not proc.is_dir():
        return
    needle = marker.encode()
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            if needle not in (entry / "cmdline").read_bytes().split(b"\0"):
                continue
            pid = int(entry.name)
            # Playwright starts Chromium as the leader of its own process group; the renderers are in it.
            if os.getpgid(pid) == pid:
                os.killpg(pid, signal.SIGKILL)
            else:
                os.kill(pid, signal.SIGKILL)
        except OSError:
            continue
        logger.warning("browser_task: killed Chromium (pid %d) after a teardown step hung", pid)


class BrowserSession:
    """Headless Chromium with a fresh profile, a tab registry, an egress proxy and allowlist routing.

    The egress proxy (`egress.py`) is the network boundary: every request Chromium makes goes through it,
    and it refuses hosts off the allowlist and any host that resolves to a non-public address, including
    redirect hops and rebinding. `_route` is a second layer inside the browser: anything off the allowlist
    is aborted there too. Callers also check page and frame URLs before and after each action (`blocks`).
    Service workers (which bypass routing) and WebSockets are blocked; downloads are refused.
    """

    def __init__(self, allowlist: DomainAllowlist, artifacts_dir: Path) -> None:
        self.allowlist = allowlist
        self.artifacts_dir = artifacts_dir
        self.blocked_requests = 0
        self.tabs: dict[str, Any] = {}  # tab_id -> playwright Page, in opening order
        self.active_tab: str | None = None
        self.opened_during_call: list[str] = []
        self._next_tab = 1
        self._proxy: egress.EgressProxy | None = None
        self._playwright: Any = None
        self._browser: Any = None
        self._context: Any = None
        # An inert switch that tags this session's Chromium, so a hung teardown can find and kill it.
        self._marker = f"--nous-browser-session={secrets.token_hex(8)}"

    async def __aenter__(self) -> BrowserSession:
        from playwright.async_api import async_playwright

        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        try:
            self._proxy = egress.EgressProxy(self.allowlist, on_refuse=self._count_refusal)
            port = await self._proxy.start()
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(
                headless=True,
                args=[*CHROMIUM_ARGS, self._marker],
                proxy={"server": f"http://127.0.0.1:{port}"},
                **_browser_user_launch(self._playwright),
            )
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
        """Close the context, the browser and Playwright, then the proxy. Each step runs even if an earlier
        one failed, and each is bounded: a step that hangs or is cancelled (a second cancel) gets Chromium
        killed, so it is never left running. A cancel is re-raised once everything is closed."""
        context, browser, playwright, proxy = self._context, self._browser, self._playwright, self._proxy
        self._context = self._browser = self._playwright = self._proxy = None
        cancelled: asyncio.CancelledError | None = None
        steps = [s for s in (context and context.close, browser and browser.close, playwright and playwright.stop) if s]
        for step in steps:
            try:
                await asyncio.wait_for(step(), CLOSE_STEP_TIMEOUT)
            except (TimeoutError, asyncio.CancelledError) as e:
                timed_out = isinstance(e, TimeoutError)
                if not timed_out:
                    cancelled = e
                logger.warning(
                    "browser_task: teardown step %s %s; killing Chromium",
                    getattr(step, "__qualname__", step),
                    "timed out" if timed_out else "was cancelled",
                )
                _kill_chromium(self._marker)
            except Exception:
                logger.warning("browser_task: browser teardown step failed", exc_info=True)
        if proxy is not None:
            try:
                await asyncio.wait_for(proxy.close(), CLOSE_STEP_TIMEOUT)
            except asyncio.CancelledError as e:
                cancelled = e
            except Exception:
                logger.warning("browser_task: closing the egress proxy failed", exc_info=True)
        if cancelled is not None:
            raise cancelled

    def _count_refusal(self) -> None:
        self.blocked_requests += 1

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
        return self.active_page if tab_id is None else self.tabs[tab_id]

    async def new_tab(self) -> str:
        page = await self._context.new_page()
        tab_id = next(t for t, p in self.tabs.items() if p is page)
        self.active_tab = tab_id
        return tab_id

    def blocks(self, url: str) -> bool:
        """A web URL off the allowlist. about:, data: and error pages are not web documents."""
        return urlsplit(url).scheme in ("http", "https") and not self.allowlist.allows(url)

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
