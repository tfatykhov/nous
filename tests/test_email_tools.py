"""Tests for the guarded send_email tool (F078.1).

Nothing is ever sent for real: we monkeypatch the blocking SMTP send
(asyncio.to_thread / smtplib.SMTP) so the network is never touched.
"""

from __future__ import annotations

import asyncio

import pytest

from nous.api.email_tools import create_send_email_tool
from nous.config import Settings


def _make_settings(**overrides) -> Settings:
    """Build a Settings with the email fields populated for the tool."""
    defaults = dict(
        email="nous@example.com",
        email_user="nous@example.com",
        email_password="app-password",
        email_allowlist="tim@example.com, alice@example.com",
        email_tool_enabled=True,
        email_max_per_hour=5,
        email_smtp_host="smtp.example.com",
        email_smtp_port=587,
    )
    defaults.update(overrides)
    return Settings(**defaults)


def _text(resp: dict) -> str:
    return resp["content"][0]["text"]


@pytest.fixture
def no_real_send(monkeypatch):
    """Replace asyncio.to_thread with a no-op so no SMTP call happens.

    Returns a list that records the (func, args) tuples it was called with,
    so tests can assert whether a send was attempted.
    """
    calls: list[tuple] = []

    async def fake_to_thread(func, *args, **kwargs):
        calls.append((func, args, kwargs))
        return None

    monkeypatch.setattr("nous.api.email_tools.asyncio.to_thread", fake_to_thread)
    return calls


# --- Allowlist ---

def test_in_allowlist_sends(no_real_send):
    tool = create_send_email_tool(_make_settings())
    resp = asyncio.run(tool(to="tim@example.com", subject="hi", body="hello"))
    assert "Email sent" in _text(resp)
    assert len(no_real_send) == 1  # send was attempted


def test_in_allowlist_case_insensitive(no_real_send):
    tool = create_send_email_tool(_make_settings())
    resp = asyncio.run(tool(to="TIM@Example.com", subject="hi", body="hello"))
    assert "Email sent" in _text(resp)
    assert len(no_real_send) == 1


def test_off_allowlist_rejects_no_send(no_real_send):
    tool = create_send_email_tool(_make_settings())
    resp = asyncio.run(tool(to="stranger@evil.com", subject="hi", body="hello"))
    assert "not on the email allowlist" in _text(resp)
    assert "stranger@evil.com" in _text(resp)
    assert len(no_real_send) == 0  # nothing sent


def test_bad_address_in_cc_list_rejects(no_real_send):
    tool = create_send_email_tool(_make_settings())
    resp = asyncio.run(
        tool(
            to="tim@example.com",
            subject="hi",
            body="hello",
            cc=["alice@example.com", "stranger@evil.com"],
        )
    )
    assert "not on the email allowlist" in _text(resp)
    assert "stranger@evil.com" in _text(resp)
    assert len(no_real_send) == 0


def test_empty_allowlist_rejects_all(no_real_send):
    tool = create_send_email_tool(_make_settings(email_allowlist=""))
    resp = asyncio.run(tool(to="tim@example.com", subject="hi", body="hello"))
    assert "not on the email allowlist" in _text(resp)
    assert len(no_real_send) == 0


# --- Secret scan ---

def test_secret_scan_rejects_sk_key(no_real_send):
    tool = create_send_email_tool(_make_settings())
    resp = asyncio.run(
        tool(
            to="tim@example.com",
            subject="report",
            body="here is the key sk-abcdEFGH1234567890zz",
        )
    )
    assert "secret" in _text(resp).lower()
    assert len(no_real_send) == 0


def test_secret_scan_rejects_password(no_real_send):
    tool = create_send_email_tool(_make_settings())
    resp = asyncio.run(
        tool(to="tim@example.com", subject="creds", body="password: hunter2")
    )
    assert "secret" in _text(resp).lower()
    assert len(no_real_send) == 0


# --- Rate limit ---

def test_rate_limit_rejects_over_window(no_real_send):
    tool = create_send_email_tool(_make_settings(email_max_per_hour=5))

    async def run():
        results = []
        for _ in range(6):
            results.append(await tool(to="tim@example.com", subject="x", body="y"))
        return results

    results = asyncio.run(run())
    # First 5 succeed, 6th is rate-limited.
    for r in results[:5]:
        assert "Email sent" in _text(r)
    assert "rate limit" in _text(results[5]).lower()
    assert len(no_real_send) == 5  # only the 5 successful sends touched SMTP


# --- Creds / enabled gating ---

def test_creds_absent_clear_error(no_real_send):
    tool = create_send_email_tool(_make_settings(email_user="", email_password=""))
    resp = asyncio.run(tool(to="tim@example.com", subject="hi", body="hello"))
    assert "credentials not configured" in _text(resp).lower()
    assert len(no_real_send) == 0


def test_tool_disabled_clear_error(no_real_send):
    tool = create_send_email_tool(_make_settings(email_tool_enabled=False))
    resp = asyncio.run(tool(to="tim@example.com", subject="hi", body="hello"))
    assert "disabled" in _text(resp).lower()
    assert len(no_real_send) == 0


# --- Exception sanitization ---

def test_exception_sanitized_generic_message(monkeypatch):
    """A raised smtplib error must surface a GENERIC message, not the raw text."""
    import smtplib as _smtplib

    secret_leak = "535 auth failed for user nous@example.com password app-password"

    class BoomSMTP:
        def __init__(self, *a, **k):
            raise _smtplib.SMTPAuthenticationError(535, secret_leak)

    # Patch the SMTP class used inside the sync helper; run to_thread for real.
    monkeypatch.setattr("nous.api.email_tools.smtplib.SMTP", BoomSMTP)

    tool = create_send_email_tool(_make_settings())
    resp = asyncio.run(tool(to="tim@example.com", subject="hi", body="hello"))
    text = _text(resp)
    assert "email send failed" in text.lower()
    assert "SMTPAuthenticationError" in text  # exception type is allowed
    # The raw smtplib message (with creds/host) must NOT leak to the model.
    assert "app-password" not in text
    assert "nous@example.com" not in text
    assert secret_leak not in text
