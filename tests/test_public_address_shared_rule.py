"""The browser egress proxy, the MCP client and web_fetch decide "is this a public address" by one rule.

Each form is checked with the ipaddress property answers overridden, so only the
explicit rule in ``nous.api.safe_fetch.is_public_address`` can refuse it.
"""

from __future__ import annotations

import contextlib
import ipaddress
from collections.abc import Iterator
from unittest.mock import patch

import pytest

from nous.api import safe_fetch as sf
from nous.browser import egress
from nous.mcp_client.transport import is_public_address as mcp_is_public_address


@contextlib.contextmanager
def _properties_overridden() -> Iterator[None]:
    """The ipaddress properties answer "global" and "not private" for IPv6 and "global" for IPv4."""
    with (
        patch.object(ipaddress.IPv6Address, "is_private", property(lambda self: False)),
        patch.object(ipaddress.IPv6Address, "is_reserved", property(lambda self: False)),
        patch.object(ipaddress.IPv6Address, "is_global", property(lambda self: True)),
        patch.object(ipaddress.IPv4Address, "is_global", property(lambda self: True)),
    ):
        yield


NOT_PUBLIC_BY_THE_RULE = [
    "::a00:1",  # IPv4-compatible form of 10.0.0.1
    "::7f00:1",  # IPv4-compatible form of 127.0.0.1
    "::ffff:10.0.0.1",  # IPv4-mapped form of 10.0.0.1
    "100.64.0.1",  # shared address space
]


@pytest.mark.parametrize("literal", NOT_PUBLIC_BY_THE_RULE)
def test_the_browser_egress_rule_refuses_the_form(literal: str) -> None:
    with _properties_overridden():
        assert not egress.address_permitted("x.example", ipaddress.ip_address(literal))


@pytest.mark.parametrize("literal", NOT_PUBLIC_BY_THE_RULE)
def test_the_mcp_client_rule_refuses_the_form(literal: str) -> None:
    with _properties_overridden():
        assert not mcp_is_public_address(literal)


@pytest.mark.parametrize("literal", NOT_PUBLIC_BY_THE_RULE)
def test_the_web_fetch_rule_refuses_the_form(literal: str) -> None:
    with _properties_overridden():
        assert not sf.is_public_address(ipaddress.ip_address(literal))


@pytest.mark.parametrize("literal", ["8.8.8.8", "2606:4700:4700::1111", "::ffff:8.8.8.8"])
def test_the_three_callers_accept_a_public_address(literal: str) -> None:
    address = ipaddress.ip_address(literal)
    assert sf.is_public_address(address)
    assert egress.address_permitted("x.example", address)
    assert mcp_is_public_address(literal)


def test_the_browser_egress_rule_still_refuses_every_nat64_address() -> None:
    assert sf.is_public_address(ipaddress.ip_address("64:ff9b::808:808"))
    assert not egress.address_permitted("x.example", ipaddress.ip_address("64:ff9b::808:808"))
