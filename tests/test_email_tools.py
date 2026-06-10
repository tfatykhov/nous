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


# --- F078.1.1: hot-reloadable allowlist file (no restart) ---

def test_allowlist_file_hot_reload(no_real_send, tmp_path):
    """Appending an address to the allowlist FILE takes effect on the next send
    using the SAME tool instance — no recreation, no restart."""
    import os
    import time as _t

    f = tmp_path / "allowlist.txt"
    f.write_text("bob@example.com\n# a comment line\n", encoding="utf-8")
    # Empty env base so the file is the only source.
    tool = create_send_email_tool(
        _make_settings(email_allowlist="", email_allowlist_file=str(f))
    )

    # In the file -> allowed.
    assert "Email sent" in _text(asyncio.run(tool(to="bob@example.com", subject="s", body="b")))
    # Not yet in the file -> rejected.
    assert "not on the email allowlist" in _text(
        asyncio.run(tool(to="carol@example.com", subject="s", body="b"))
    )

    # Live edit: append carol, force a distinct mtime so the cache re-reads.
    f.write_text("bob@example.com\ncarol@example.com\n", encoding="utf-8")
    os.utime(f, (_t.time() + 5, _t.time() + 5))

    # Same tool instance (no restart) now allows carol.
    assert "Email sent" in _text(asyncio.run(tool(to="carol@example.com", subject="s", body="b")))


def test_allowlist_file_union_with_env(no_real_send, tmp_path):
    """Effective allowlist = env CSV UNION file contents."""
    f = tmp_path / "al.txt"
    f.write_text("filey@example.com\n", encoding="utf-8")
    tool = create_send_email_tool(
        _make_settings(email_allowlist="envy@example.com", email_allowlist_file=str(f))
    )
    assert "Email sent" in _text(asyncio.run(tool(to="envy@example.com", subject="s", body="b")))   # env base
    assert "Email sent" in _text(asyncio.run(tool(to="filey@example.com", subject="s", body="b")))  # file


def test_allowlist_file_missing_falls_back_to_env(no_real_send, tmp_path):
    """A missing/unreadable file does not widen the allowlist (fail-closed)."""
    tool = create_send_email_tool(
        _make_settings(email_allowlist="tim@example.com", email_allowlist_file=str(tmp_path / "nope.txt"))
    )
    assert "Email sent" in _text(asyncio.run(tool(to="tim@example.com", subject="s", body="b")))     # env still works
    assert "not on the email allowlist" in _text(
        asyncio.run(tool(to="stranger@evil.com", subject="s", body="b"))
    )


# --- F078.1.2: attachments ---

def test_attachment_sends_multipart(no_real_send, tmp_path):
    f = tmp_path / "report.docx"
    f.write_bytes(b"PK\x03\x04 fake docx bytes")
    tool = create_send_email_tool(_make_settings())
    resp = asyncio.run(tool(to="tim@example.com", subject="report", body="see attached", attachments=str(f)))
    assert "Email sent" in _text(resp)
    assert "1 attachment" in _text(resp)
    assert len(no_real_send) == 1
    msg = no_real_send[0][1][2]   # (settings, recipients, msg)
    assert msg.is_multipart()
    names = [p.get_filename() for p in msg.walk() if p.get_filename()]
    assert "report.docx" in names


def test_attachment_list(no_real_send, tmp_path):
    f1 = tmp_path / "a.pdf"; f1.write_bytes(b"%PDF-1.4 x")
    f2 = tmp_path / "b.csv"; f2.write_text("a,b\n1,2\n")
    tool = create_send_email_tool(_make_settings())
    resp = asyncio.run(tool(to="tim@example.com", subject="s", body="b", attachments=[str(f1), str(f2)]))
    assert "2 attachment" in _text(resp)
    assert len(no_real_send) == 1


def test_attachment_missing_rejects_no_send(no_real_send, tmp_path):
    tool = create_send_email_tool(_make_settings())
    resp = asyncio.run(tool(to="tim@example.com", subject="s", body="b", attachments=str(tmp_path / "nope.docx")))
    assert "not found" in _text(resp).lower()
    assert len(no_real_send) == 0


def test_attachment_oversize_rejects_no_send(no_real_send, tmp_path):
    f = tmp_path / "big.bin"; f.write_bytes(b"x" * 10)
    tool = create_send_email_tool(_make_settings(email_max_attachment_mb=0))  # cap 0 => any file too big
    resp = asyncio.run(tool(to="tim@example.com", subject="s", body="b", attachments=str(f)))
    assert "exceed" in _text(resp).lower()
    assert len(no_real_send) == 0


def test_attachment_off_allowlist_rejected_before_read(no_real_send, tmp_path):
    f = tmp_path / "r.docx"; f.write_bytes(b"x")
    tool = create_send_email_tool(_make_settings())
    resp = asyncio.run(tool(to="stranger@evil.com", subject="s", body="b", attachments=str(f)))
    assert "not on the email allowlist" in _text(resp)
    assert len(no_real_send) == 0


# --- F078.1.3: html_body ---

def test_html_body_none_plain_text(no_real_send):
    """html_body=None → message is plain MIMEText (BC preserved)."""
    from email.mime.text import MIMEText as _MIMEText

    tool = create_send_email_tool(_make_settings())
    asyncio.run(tool(to="tim@example.com", subject="hi", body="hello"))
    assert len(no_real_send) == 1
    msg = no_real_send[0][1][2]  # (settings, recipients, msg)
    assert isinstance(msg, _MIMEText)
    assert msg.get_content_type() == "text/plain"


def test_html_body_set_no_attachments(no_real_send):
    """html_body set, no attachments → top-level multipart/alternative with two
    parts: text/plain first, text/html second."""
    tool = create_send_email_tool(_make_settings())
    asyncio.run(
        tool(
            to="tim@example.com",
            subject="newsletter",
            body="plain fallback",
            html_body="<h1>Rich content</h1>",
        )
    )
    assert len(no_real_send) == 1
    msg = no_real_send[0][1][2]
    assert msg.get_content_type() == "multipart/alternative"
    parts = msg.get_payload()
    assert len(parts) == 2
    assert parts[0].get_content_type() == "text/plain"
    assert parts[1].get_content_type() == "text/html"


def test_html_body_set_with_attachment(no_real_send, tmp_path):
    """html_body + attachment → top-level multipart/mixed whose first part is
    multipart/alternative, plus the attachment part."""
    f = tmp_path / "report.pdf"
    f.write_bytes(b"%PDF-1.4 fake")
    tool = create_send_email_tool(_make_settings())
    asyncio.run(
        tool(
            to="tim@example.com",
            subject="report",
            body="see attached",
            html_body="<p>See attached</p>",
            attachments=str(f),
        )
    )
    assert len(no_real_send) == 1
    msg = no_real_send[0][1][2]
    assert msg.get_content_type() == "multipart/mixed"
    parts = msg.get_payload()
    assert parts[0].get_content_type() == "multipart/alternative"
    alt_parts = parts[0].get_payload()
    assert alt_parts[0].get_content_type() == "text/plain"
    assert alt_parts[1].get_content_type() == "text/html"
    assert parts[1].get_filename() == "report.pdf"


def test_html_body_secret_scan_rejected(no_real_send):
    """html_body containing a secret pattern is rejected."""
    tool = create_send_email_tool(_make_settings())
    resp = asyncio.run(
        tool(
            to="tim@example.com",
            subject="newsletter",
            body="plain text is clean",
            html_body="<p>key: sk-abcdEFGH1234567890zz</p>",
        )
    )
    assert "secret" in _text(resp).lower()
    assert len(no_real_send) == 0


def test_html_body_entity_encoded_secret_rejected(no_real_send):
    """#484: an HTML-entity-encoded secret in html_body is rejected.

    Mail clients render `sk-&#97;bcd...` as `sk-abcd...`, so the scan must
    run on the decoded text, not just the raw HTML source.
    """
    tool = create_send_email_tool(_make_settings())
    resp = asyncio.run(
        tool(
            to="tim@example.com",
            subject="newsletter",
            body="plain text is clean",
            html_body="<p>key: sk-&#97;bcdEFGH1234567890zz</p>",
        )
    )
    assert "secret" in _text(resp).lower()
    assert len(no_real_send) == 0


def test_html_body_attribute_secret_still_rejected(no_real_send):
    """#484 regression guard: a secret inside a tag attribute (stripped from
    the rendered text) must still be caught via the raw-HTML scan."""
    tool = create_send_email_tool(_make_settings())
    resp = asyncio.run(
        tool(
            to="tim@example.com",
            subject="newsletter",
            body="plain text is clean",
            html_body='<a href="https://x.example?key=sk-abcdEFGH1234567890zz">report</a>',
        )
    )
    assert "secret" in _text(resp).lower()
    assert len(no_real_send) == 0
