"""Fetching a URL a model supplied: only public internet addresses, and only from the address that was checked.

``_check_url`` parses a URL, normalises its host, and resolves and checks every address it names.
``fetch_checked`` sends each request (the first and every redirect hop) to an address that passed the check,
with the original Host header and TLS server name, so the connection never depends on a second resolution.
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
import ssl
from typing import Any
from urllib.parse import unquote, urljoin, urlparse

import httpx

# Names that are refused without resolving the host (the machine itself and the compose service names).
_BLOCKED_HOSTNAMES = {"localhost", "postgres", "nous", "redis", "0.0.0.0"}

_NAT64_PREFIX = ipaddress.ip_network("64:ff9b::/96")
_CGNAT_NETWORK = ipaddress.ip_network("100.64.0.0/10")
_IPV4_COMPATIBLE = ipaddress.ip_network("::/96")
_FETCH_SCHEMES = ("http", "https")
_DEFAULT_PORTS = {"http": 80, "https": 443}
_REFUSED_ADDRESS = "the host does not resolve to a public internet address"
_HOSTNAME = re.compile(r"[a-z0-9]([a-z0-9_-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9_-]*[a-z0-9])?)*")
_BAD_LOCATION = "Blocked redirect to unsafe URL: the Location header is not a URL."
# At most this many of a host's checked addresses are tried, in order, when a connection fails.
_MAX_ADDRESS_ATTEMPTS = 4
# web_fetch reads at most this many bytes of a response.
WEB_FETCH_MAX_BYTES = 10 * 1024 * 1024
# One fetch (every redirect hop included) ends after this many seconds; the limit on a single read is separate.
FETCH_DEADLINE_SECONDS = 60.0
# Resolving a host name gets this many seconds.
RESOLVE_TIMEOUT_SECONDS = 10.0


def is_public_address(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Return True only for a globally routable unicast address.

    Refuses loopback, private, link-local, unspecified, multicast, reserved, site-local, shared (CGNAT)
    and every other address the ``ipaddress`` module does not call global. An IPv6 address that embeds
    an IPv4 address (IPv4-mapped, IPv4-compatible, 6to4, NAT64, Teredo) is accepted only when the
    embedded IPv4 address is accepted too.
    """
    if isinstance(ip, ipaddress.IPv6Address):
        low32 = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
        if ip.ipv4_mapped is not None:
            return is_public_address(ip.ipv4_mapped)
        if ip in _NAT64_PREFIX:
            return is_public_address(low32)
        embedded: list[ipaddress.IPv4Address] = []
        if ip.sixtofour is not None:
            embedded.append(ip.sixtofour)
        if ip in _IPV4_COMPATIBLE:
            embedded.append(low32)
        if ip.teredo is not None:
            embedded.extend(ip.teredo)
        if not all(is_public_address(v4) for v4 in embedded):
            return False
        if ip.is_site_local:
            return False
    elif ip in _CGNAT_NETWORK:
        return False
    return not (
        ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_unspecified
        or ip.is_multicast
        or ip.is_reserved
        or not ip.is_global
    )


def _parse_legacy_int(part: str) -> int | None:
    """Parse one dotted part the way ``inet_aton`` does: 0x hex, leading-0 octal, else decimal."""
    try:
        if part[:2].lower() == "0x":
            return int(part[2:] or "0", 16)
        if len(part) > 1 and part[0] == "0":
            return int(part, 8)
        return int(part, 10) if part.isascii() and part.isdigit() else None
    except ValueError:
        return None


def _parse_ip_host(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """Parse ``host`` as an IP address in any spelling a resolver accepts, or return None.

    Covers IPv6, dotted decimal, and the short, octal and hexadecimal IPv4 forms (``2130706433``,
    ``0x7f.1``, ``017700000001``). Raises ValueError for a host whose final label looks numeric but
    is not a valid address, so it is never handed to a resolver.
    """
    if ":" in host:
        if "%" in host:
            raise ValueError("an address with a zone is not fetched")
        return ipaddress.ip_address(host)
    parts = host.split(".")
    last = parts[-1]
    if not (last.isascii() and last.isdigit()) and last[:2].lower() != "0x":
        return None
    values = [_parse_legacy_int(p) for p in parts]
    nums = [v for v in values if v is not None]
    if len(parts) > 4 or len(nums) != len(parts):
        raise ValueError("numeric host is not an address")
    if any(n > 255 for n in nums[:-1]) or nums[-1] >= 256 ** (5 - len(nums)):
        raise ValueError("numeric host is out of range")
    packed = 0
    for n in nums[:-1]:
        packed = (packed << 8) | n
    packed = (packed << (8 * (5 - len(nums)))) | nums[-1]
    return ipaddress.IPv4Address(packed)


def _resolve_host(host: str) -> list[str]:
    """Resolve ``host`` to every address (A and AAAA) the system resolver returns."""
    return [str(info[4][0]) for info in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)]


class _Target:
    """A URL whose host has been checked: the addresses it may be fetched from, in order."""

    def __init__(self, scheme: str, host: str, is_ip: bool, port: int, path: str, addresses: list[str]) -> None:
        self.scheme = scheme
        self.host = host
        self.is_ip = is_ip
        self.port = port
        self.path = path
        self.addresses = addresses

    @property
    def host_header(self) -> str:
        """The Host header value: the hostname, with the port only when it is not the default."""
        host = f"[{self.host}]" if ":" in self.host else self.host
        return host if self.port == _DEFAULT_PORTS[self.scheme] else f"{host}:{self.port}"

    def pinned_url(self, address: str) -> str:
        """The URL that connects to ``address`` (same path and query as the original)."""
        literal = f"[{address}]" if ":" in address else address
        return f"{self.scheme}://{literal}:{self.port}{self.path}"


def _check_url(url: str) -> tuple[_Target | None, str]:
    """Parse ``url``, normalise its host, and resolve and check every address it names.

    Returns ``(target, "")`` when every address is public, otherwise ``(None, message)``.
    """
    try:
        url = url.strip()
        if not url or any(ord(c) < 33 or ord(c) == 127 or c == "\\" for c in url):
            return None, "Could not parse URL"
        parsed = urlparse(url)
        scheme = parsed.scheme.lower()
        if scheme not in _FETCH_SCHEMES:
            return None, "URL must start with http:// or https://"
        raw_host = parsed.hostname
        if not raw_host:
            return None, "Could not parse hostname from URL"
        if parsed.port == 0:
            return None, "Could not parse URL"
        port = parsed.port or _DEFAULT_PORTS[scheme]
        host = unquote(raw_host).rstrip(".").lower()
        if ":" not in host:
            host = host.encode("idna").decode("ascii").rstrip(".").lower()
        if not host:
            return None, "Could not parse hostname from URL"

        ip = _parse_ip_host(host)
        if ip is not None:
            if not is_public_address(ip):
                return None, _REFUSED_ADDRESS
            addresses, host, is_ip = [str(ip)], str(ip), True
        else:
            if not _HOSTNAME.fullmatch(host):
                return None, "Could not parse hostname from URL"
            if host in _BLOCKED_HOSTNAMES:
                return None, f"Blocked hostname: {host}"
            try:
                addresses = list(dict.fromkeys(_resolve_host(host)))
            except OSError:
                return None, _REFUSED_ADDRESS
            if not addresses:
                return None, _REFUSED_ADDRESS
            is_ip = False
            for address in addresses:
                if not is_public_address(ipaddress.ip_address(address.split("%", 1)[0])):
                    return None, _REFUSED_ADDRESS
        path = parsed.path or "/"
        if parsed.params:
            path += f";{parsed.params}"
        if parsed.query:
            path += f"?{parsed.query}"
        unscoped = list(dict.fromkeys(a.split("%", 1)[0] for a in addresses))
        return _Target(scheme, host, is_ip, port, path, unscoped), ""
    except Exception:
        return None, "URL validation error"


async def check_url(url: str) -> tuple[_Target | None, str]:
    """``_check_url`` off the event loop, with a time limit on the resolver."""
    try:
        return await asyncio.wait_for(asyncio.to_thread(_check_url, url), RESOLVE_TIMEOUT_SECONDS)
    except TimeoutError:
        return None, _REFUSED_ADDRESS


def _is_url_safe(url: str) -> tuple[bool, str]:
    """Check whether ``url`` names only public internet addresses.

    Returns (is_safe, error_message); the message never contains a resolved address.
    """
    target, error = _check_url(url)
    return target is not None, error


class FetchRefused(Exception):
    """A fetch that was not made or not completed; the message is one plain sentence for the model."""


_SSL_CONTEXT: ssl.SSLContext | None = None


def _ssl_context() -> ssl.SSLContext:
    """The TLS context every fetch client verifies with, built on first use (the CA bundle is read once)."""
    global _SSL_CONTEXT
    if _SSL_CONTEXT is None:
        _SSL_CONTEXT = httpx.create_ssl_context()
    return _SSL_CONTEXT


def make_fetch_client(**kwargs: Any) -> httpx.AsyncClient:
    """Build a client for one request to a model-supplied URL.

    It ignores the proxy environment variables (HTTP_PROXY, HTTPS_PROXY, ALL_PROXY, NO_PROXY), because a
    request sent to a proxy would be resolved again there instead of connecting to the address that was checked.
    ``fetch_checked`` builds one per request and closes it afterwards, so no connection or cookie is shared
    between requests.
    """
    kwargs["trust_env"] = False
    kwargs.setdefault("verify", _ssl_context())
    return httpx.AsyncClient(**kwargs)


async def _get_pinned(target: _Target, *, timeout: float, max_bytes: int) -> httpx.Response:
    """GET ``target`` from an address that passed the check, never from a second resolution.

    The request goes to the address itself; the Host header and the TLS server name stay the original
    hostname. The checked addresses (at most ``_MAX_ADDRESS_ATTEMPTS``) are tried in order until one accepts
    the connection. The body is read raw as it arrives, with no content encoding accepted, and the fetch fails
    once it is longer than ``max_bytes``.
    """
    headers = {"User-Agent": "Nous/0.1 (cognitive agent)", "Host": target.host_header, "Accept-Encoding": "identity"}
    extensions = {"sni_hostname": target.host} if target.scheme == "https" and not target.is_ip else {}
    last_error: httpx.TransportError | None = None
    for address in target.addresses[:_MAX_ADDRESS_ATTEMPTS]:
        client = make_fetch_client(timeout=timeout)
        try:
            async with client.stream(
                "GET", target.pinned_url(address), headers=headers, extensions=extensions, follow_redirects=False
            ) as response:
                encoding = response.headers.get("content-encoding", "").strip().lower()
                if encoding not in ("", "identity"):
                    raise FetchRefused("The response uses a content encoding that is not supported.")
                body = bytearray()
                async for chunk in response.aiter_raw():
                    body.extend(chunk)
                    if len(body) > max_bytes:
                        raise FetchRefused(f"The response is longer than {max_bytes} bytes.")
                kept = [
                    (k, v)
                    for k, v in response.headers.multi_items()
                    if k.lower() not in ("content-encoding", "content-length", "transfer-encoding")
                ]
                return httpx.Response(response.status_code, headers=kept, content=bytes(body), request=response.request)
        except (httpx.ConnectError, httpx.ConnectTimeout) as e:
            last_error = e
        except (httpx.InvalidURL, ValueError):  # httpx cannot read the Location header of a redirect response
            raise FetchRefused(_BAD_LOCATION) from None
        except httpx.RemoteProtocolError as e:
            if "location header" in str(e).lower():
                raise FetchRefused(_BAD_LOCATION) from None
            raise
        finally:
            await client.aclose()
    raise last_error if last_error is not None else httpx.ConnectError("no address to connect to")


async def fetch_checked(
    url: str,
    *,
    max_redirects: int = 5,
    timeout: float = 15,
    max_bytes: int | None = None,
    deadline: float | None = None,
) -> tuple[httpx.Response, str]:
    """GET ``url`` and follow redirects by hand; return the final response and its URL.

    The URL and every redirect target go through ``check_url``, and each request is pinned to an address
    that passed, on a client of its own. A redirect from https to http is refused. ``timeout`` is the limit on
    each read, ``deadline`` the limit on the whole fetch. Raises ``FetchRefused`` with a plain sentence when a
    URL is refused, a limit is exceeded, or the host cannot be reached. ``max_bytes`` defaults to
    ``WEB_FETCH_MAX_BYTES`` and ``deadline`` to ``FETCH_DEADLINE_SECONDS``.
    """
    max_bytes = WEB_FETCH_MAX_BYTES if max_bytes is None else max_bytes
    deadline = FETCH_DEADLINE_SECONDS if deadline is None else deadline
    try:
        async with asyncio.timeout(deadline):
            return await _fetch_hops(url, max_redirects, timeout, max_bytes)
    except TimeoutError:
        raise FetchRefused(f"The fetch took longer than {deadline:g} seconds.") from None
    except httpx.TimeoutException:
        raise FetchRefused(f"Fetch timed out for: {url}") from None
    except httpx.TransportError:
        raise FetchRefused(f"Could not connect to {url}") from None


async def _fetch_hops(url: str, max_redirects: int, timeout: float, max_bytes: int) -> tuple[httpx.Response, str]:
    target, error = await check_url(url)
    if target is None:
        raise FetchRefused(error if error.startswith("URL must") else f"Blocked: {error}")
    current_url = url
    for _ in range(max_redirects + 1):
        response = await _get_pinned(target, timeout=timeout, max_bytes=max_bytes)
        if response.status_code not in (301, 302, 303, 307, 308):
            return response, current_url
        location = response.headers.get("location", "")
        if not location:
            return response, current_url
        try:
            next_url = urljoin(current_url, location)
        except ValueError:
            raise FetchRefused(_BAD_LOCATION) from None
        next_target, error = await check_url(next_url)
        if next_target is None:
            raise FetchRefused(f"Blocked redirect to unsafe URL: {error}")
        if target.scheme == "https" and next_target.scheme == "http":
            raise FetchRefused("Blocked redirect to unsafe URL: the redirect leads from https to http.")
        current_url, target = next_url, next_target
    raise FetchRefused(f"Too many redirects (max {max_redirects})")
