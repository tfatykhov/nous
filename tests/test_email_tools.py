"""Tests for the guarded send_email tool (F078.1).

Nothing is ever sent for real: we monkeypatch the blocking SMTP send
(asyncio.to_thread / smtplib.SMTP) so the network is never touched.
"""

from __future__ import annotations

import asyncio
import time

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
        email_content_gate="strict",
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
    # Gate off: this test checks MIME structure, not content completeness.
    tool = create_send_email_tool(_make_settings(email_content_gate="off"))
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
    # Gate off: this test checks MIME structure, not content completeness.
    tool = create_send_email_tool(_make_settings(email_content_gate="off"))
    resp = asyncio.run(tool(to="tim@example.com", subject="s", body="b", attachments=[str(f1), str(f2)]))
    assert "2 attachment" in _text(resp)
    assert len(no_real_send) == 1


def test_attachment_missing_rejects_no_send(no_real_send, tmp_path):
    # Gate off: this test checks attachment-not-found rejection, not content gate.
    tool = create_send_email_tool(_make_settings(email_content_gate="off"))
    resp = asyncio.run(tool(to="tim@example.com", subject="s", body="b", attachments=str(tmp_path / "nope.docx")))
    assert "not found" in _text(resp).lower()
    assert len(no_real_send) == 0


def test_attachment_oversize_rejects_no_send(no_real_send, tmp_path):
    f = tmp_path / "big.bin"; f.write_bytes(b"x" * 10)
    # Gate off: this test checks oversize rejection, not content gate.
    tool = create_send_email_tool(_make_settings(email_max_attachment_mb=0, email_content_gate="off"))
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
    # Gate off: this test checks MIME structure, not content completeness.
    tool = create_send_email_tool(_make_settings(email_content_gate="off"))
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
    # Gate off: this test checks MIME structure, not content completeness.
    tool = create_send_email_tool(_make_settings(email_content_gate="off"))
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


def test_html_body_entity_encoded_attribute_secret_rejected(no_real_send):
    """#484 codex P1: an entity-encoded secret INSIDE a tag attribute must be
    caught — raw view sees it encoded, tag-stripping removes it before
    decoding, so a decode-without-strip view is required."""
    tool = create_send_email_tool(_make_settings())
    resp = asyncio.run(
        tool(
            to="tim@example.com",
            subject="newsletter",
            body="plain text is clean",
            html_body='<a href="https://x.example?key=sk-&#97;bcdEFGH1234567890zz">report</a>',
        )
    )
    assert "secret" in _text(resp).lower()
    assert len(no_real_send) == 0


def test_html_body_malformed_html_fast_and_scanned(no_real_send):
    """#484 codex P2: a flood of '<' without '>' must not go quadratic, and
    a secret after an unclosed '<' is still caught via the raw view."""
    tool = create_send_email_tool(_make_settings())
    flood = "<" * 80_000
    start = time.monotonic()
    resp = asyncio.run(
        tool(
            to="tim@example.com",
            subject="newsletter",
            body="plain text is clean",
            html_body=f"{flood}\nunclosed sk-abcdEFGH1234567890zz",
        )
    )
    elapsed = time.monotonic() - start
    assert "secret" in _text(resp).lower()
    assert len(no_real_send) == 0
    assert elapsed < 2.0  # linear scan; the old regex took seconds at 80 KB


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


# ---------------------------------------------------------------------------
# Content-completeness gate tests
# ---------------------------------------------------------------------------

# A minimal valid HTML email body that passes all structural checks.
# Must be ≥600 chars so the length gate passes; all structural markers present.
_GOOD_HTML = (
    "<html><body>"
    "<table><tr><td>"
    "<h1>Monthly Report — Q3 2026</h1>"
    "<p>Here is a detailed summary of this month's activity covering all the key "
    "metrics, trends, and observations that you need to review before the next "
    "board meeting scheduled for early next month. The data has been carefully "
    "compiled from all available sources and verified for accuracy.</p>"
    "<p>Key highlights: revenue grew 12% quarter-over-quarter, new user "
    "registrations are up 8%, and customer satisfaction scores remain at an "
    "all-time high of 94%. Infrastructure costs decreased by 3% due to "
    "optimization work completed in July.</p>"
    "<p>Please let us know if you have any questions or need additional data. "
    "We are available for a follow-up call at your convenience.</p>"
    "</td></tr></table>"
    '<div class="footer">Sent by Nous · <a href="https://example.com">unsubscribe</a></div>'
    "</body></html>"
)


def _gate_tool(no_real_send, **overrides):
    """Return a tool pre-wired with a full stub body and strict gate."""
    defaults = dict(email_content_gate="strict")
    defaults.update(overrides)
    return create_send_email_tool(_make_settings(**defaults))


# --- Placeholder markers ---

@pytest.mark.parametrize("field,value", [
    ("subject", "PLACEHOLDER subject"),
    ("body",    "body with PLACEHOLDER text"),
    ("subject", "Report TODO: fill this in"),
    ("body",    "TBD details here"),
    ("body",    "lorem ipsum dolor sit amet"),
    ("body",    "Use {{user_name}} here"),
    ("body",    "Please <insert your content> here."),
    ("body",    "Review the XXX section"),
    ("body",    "FIXME: write actual content"),
    ("body",    "see attached rendering for details"),
    ("body",    "see attachment for the report"),
    ("body",    "content in the attached file"),
])
def test_placeholder_marker_caught(no_real_send, field, value):
    """Each placeholder/stub marker class is rejected by the gate."""
    tool = create_send_email_tool(_make_settings())
    kwargs = {"to": "tim@example.com", "subject": "hi", "body": "clean body"}
    kwargs[field] = value
    resp = asyncio.run(tool(**kwargs))
    assert "completeness" in _text(resp).lower() or "placeholder" in _text(resp).lower()
    assert len(no_real_send) == 0


def test_no_false_positive_extbd_xxxi(no_real_send):
    """TBD and XXX must not fire on ordinary substrings ('extbd', 'xxxi')."""
    tool = create_send_email_tool(_make_settings())
    resp = asyncio.run(
        tool(
            to="tim@example.com",
            subject="Chapter xxxii update",
            body="The extbd index grew by 3.2 points. The exxtbd metric is stable. "
                 "Section xxxiv shows improvement across all measured dimensions and "
                 "we have verified the results carefully.",
        )
    )
    assert "Email sent" in _text(resp)
    assert len(no_real_send) == 1


# --- Stub-body-with-attachment ---

def test_stub_body_with_attachment_caught(no_real_send, tmp_path):
    """Attachment + visible body < 200 chars is the documented failure shape."""
    f = tmp_path / "report.pdf"
    f.write_bytes(b"%PDF-1.4 fake")
    tool = create_send_email_tool(_make_settings())
    resp = asyncio.run(
        tool(
            to="tim@example.com",
            subject="report",
            body="Short body.",
            attachments=str(f),
        )
    )
    assert "stub" in _text(resp).lower() or "200" in _text(resp)
    assert len(no_real_send) == 0


def test_long_body_with_attachment_passes(no_real_send, tmp_path):
    """A body of ≥200 chars with an attachment is allowed."""
    f = tmp_path / "report.pdf"
    f.write_bytes(b"%PDF-1.4 fake")
    tool = create_send_email_tool(_make_settings())
    long_body = (
        "This report covers the quarterly financial results and includes all the "
        "relevant data and analysis for Q3. Please review the attached PDF for "
        "the full breakdown with charts, tables, and detailed commentary included "
        "across multiple sections as described in the executive summary on page 1."
    )
    assert len(long_body) >= 200
    resp = asyncio.run(
        tool(to="tim@example.com", subject="Q3 report", body=long_body, attachments=str(f))
    )
    assert "Email sent" in _text(resp)
    assert len(no_real_send) == 1


# --- HTML structural checks ---

def test_html_too_short_caught(no_real_send):
    """html_body shorter than 600 chars is rejected."""
    tool = create_send_email_tool(_make_settings())
    resp = asyncio.run(
        tool(
            to="tim@example.com",
            subject="hi",
            body="plain fallback",
            html_body="<h1>Short</h1><table><tr><td>x</td></tr></table>"
                      '<div class="footer">Sent by Nous</div>',
        )
    )
    assert "600" in _text(resp) or "placeholder" in _text(resp).lower()
    assert len(no_real_send) == 0


def test_html_style_block_caught(no_real_send):
    """<style> block is rejected (Gmail strips it)."""
    tool = create_send_email_tool(_make_settings())
    resp = asyncio.run(
        tool(to="tim@example.com", subject="hi", body="plain", html_body=_GOOD_HTML + "<style>body{}</style>")
    )
    assert "<style>" in _text(resp) or "style" in _text(resp).lower()
    assert len(no_real_send) == 0


def test_html_missing_h1_caught(no_real_send):
    """Missing <h1> is caught."""
    tool = create_send_email_tool(_make_settings())
    html_no_h1 = _GOOD_HTML.replace("<h1>", "<h2>").replace("</h1>", "</h2>")
    assert "<h1" not in html_no_h1  # confirm the replacement worked
    resp = asyncio.run(tool(to="tim@example.com", subject="hi", body="plain", html_body=html_no_h1))
    assert "h1" in _text(resp).lower()
    assert len(no_real_send) == 0


def test_html_missing_table_caught(no_real_send):
    """Missing <table> is caught."""
    tool = create_send_email_tool(_make_settings())
    html_no_table = _GOOD_HTML.replace("<table>", "<div>").replace("</table>", "</div>").replace(
        "<tr><td>", "<p>"
    ).replace("</td></tr>", "</p>")
    resp = asyncio.run(tool(to="tim@example.com", subject="hi", body="plain", html_body=html_no_table))
    assert "table" in _text(resp).lower()
    assert len(no_real_send) == 0


def test_html_missing_footer_caught(no_real_send):
    """Missing footer text is caught."""
    tool = create_send_email_tool(_make_settings())
    html_no_footer = _GOOD_HTML.replace(
        '<div class="footer">Sent by Nous · <a href="https://example.com">unsubscribe</a></div>',
        "<p>End of message.</p>",
    )
    assert "footer" not in html_no_footer and "sent by" not in html_no_footer.lower()
    resp = asyncio.run(tool(to="tim@example.com", subject="hi", body="plain", html_body=html_no_footer))
    assert "footer" in _text(resp).lower()
    assert len(no_real_send) == 0


def test_html_insecure_href_caught(no_real_send):
    """http:// href is rejected."""
    tool = create_send_email_tool(_make_settings())
    html_http = _GOOD_HTML.replace('href="https://example.com"', 'href="http://example.com"')
    resp = asyncio.run(tool(to="tim@example.com", subject="hi", body="plain", html_body=html_http))
    assert "http://" in _text(resp) or "insecure" in _text(resp).lower()
    assert len(no_real_send) == 0


def test_html_leaked_escaped_tag_caught(no_real_send):
    """HTML-escaped markup (e.g. &lt;a&gt;) in html_body is caught."""
    tool = create_send_email_tool(_make_settings())
    resp = asyncio.run(
        tool(
            to="tim@example.com",
            subject="hi",
            body="plain",
            html_body=_GOOD_HTML + " &lt;a href='x'&gt;click&lt;/a&gt;",
        )
    )
    assert "escaped" in _text(resp).lower() or "&lt;" in _text(resp)
    assert len(no_real_send) == 0


def test_html_leaked_double_encoded_entity_caught(no_real_send):
    """Double-encoded entity (e.g. &amp;mdash;) in html_body is caught."""
    tool = create_send_email_tool(_make_settings())
    resp = asyncio.run(
        tool(
            to="tim@example.com",
            subject="hi",
            body="plain",
            html_body=_GOOD_HTML + " This is a good email &amp;mdash; really.",
        )
    )
    assert "entity" in _text(resp).lower() or "&amp;" in _text(resp)
    assert len(no_real_send) == 0


def test_good_html_email_passes(no_real_send):
    """A well-formed, complete HTML email passes all gate checks."""
    tool = create_send_email_tool(_make_settings())
    resp = asyncio.run(
        tool(to="tim@example.com", subject="Monthly Report", body="See HTML version.", html_body=_GOOD_HTML)
    )
    assert "Email sent" in _text(resp)
    assert len(no_real_send) == 1


# --- Plain-text (no html_body, no attachments) ---

def test_short_plain_text_no_attachments_passes(no_real_send):
    """A short plain-text email with no attachments passes (no HTML/stub checks)."""
    tool = create_send_email_tool(_make_settings())
    resp = asyncio.run(
        tool(to="tim@example.com", subject="Quick note", body="Your package shipped!")
    )
    assert "Email sent" in _text(resp)
    assert len(no_real_send) == 1


def test_empty_body_no_html_caught(no_real_send):
    """Empty plain-text body with no html_body is rejected."""
    tool = create_send_email_tool(_make_settings())
    resp = asyncio.run(tool(to="tim@example.com", subject="hi", body="   "))
    assert "empty" in _text(resp).lower()
    assert len(no_real_send) == 0


# --- Mode: warn ---

def test_gate_warn_logs_but_sends(no_real_send, caplog):
    """email_content_gate=warn logs a warning but does not block the send."""
    import logging as _logging

    tool = create_send_email_tool(_make_settings(email_content_gate="warn"))
    with caplog.at_level(_logging.WARNING, logger="nous.api.email_tools"):
        resp = asyncio.run(
            tool(to="tim@example.com", subject="Report TODO: fill in", body="hello")
        )
    assert "Email sent" in _text(resp)
    assert len(no_real_send) == 1
    assert any("content gate" in r.message.lower() for r in caplog.records)


# --- Mode: off ---

def test_gate_off_skips_entirely(no_real_send):
    """email_content_gate=off lets a clearly stub email through."""
    tool = create_send_email_tool(_make_settings(email_content_gate="off"))
    resp = asyncio.run(
        tool(to="tim@example.com", subject="PLACEHOLDER", body="   ")
    )
    assert "Email sent" in _text(resp)
    assert len(no_real_send) == 1


# --- Gate runs before rate limiter ---

def test_gate_refused_send_does_not_consume_rate_limit(no_real_send):
    """A gate-refused send must not increment the rate-limit counter."""
    # max_per_hour=2: one slot for the valid send, one for a second valid send.
    tool = create_send_email_tool(_make_settings(email_max_per_hour=2))

    async def run():
        # Send 1: valid → succeeds (consumes slot 1).
        r1 = await tool(to="tim@example.com", subject="hi", body="hello")
        # Send 2: gate refuses (PLACEHOLDER body) → must NOT consume slot 2.
        r2 = await tool(to="tim@example.com", subject="PLACEHOLDER", body="test")
        # Send 3: valid → should succeed (slot 2 still available).
        r3 = await tool(to="tim@example.com", subject="hi", body="hello again")
        return r1, r2, r3

    r1, r2, r3 = asyncio.run(run())
    assert "Email sent" in _text(r1)
    assert "completeness" in _text(r2).lower() or "placeholder" in _text(r2).lower()
    assert "Email sent" in _text(r3)  # not rate-limited — gate refusal didn't count
    assert len(no_real_send) == 2  # only the two successful sends touched SMTP


# --- Codex #609 review: measure rendered content, not raw markup ---

def _shell(inner: str) -> str:
    """Canonical-shaped shell: inline-styled like real renderer output (~1.4KB raw)
    so the raw-length floor is cleared and only the check under test can fire."""
    td = ('padding:12px 16px;font-family:Helvetica,Arial,sans-serif;font-size:15px;'
          'color:#111827;line-height:1.6;border-bottom:1px solid #e5e7eb;')
    return (
        '<html><body style="margin:0;background-color:#f4f4f7;">'
        '<h1 style="color:#ffffff;margin:0;font-size:22px;font-weight:600;">Report</h1>'
        '<table role="presentation" cellpadding="0" cellspacing="0" '
        'style="width:100%;max-width:640px;border-collapse:collapse;background:#ffffff;">'
        f'<tr><td style="{td}">{inner}</td></tr></table>'
        '<div class="footer" style="font-size:12px;color:#6b7280;text-align:center;">'
        'Sent by Nous \u00b7 cognition-engines.ai</div></body></html>'
    )


def test_whitespace_inflated_body_with_attachment_refused(no_real_send, tmp_path):
    """P1: pretty-print indentation is not message content (codex, PR #609).

    `.strip()` only trims the ends, so a template whose tags are separated by
    newlines/indentation previously cleared the attachment threshold with a
    near-empty message.
    """
    f = tmp_path / "report.pdf"
    f.write_bytes(b"%PDF-1.4 fake")
    padded = "<html>\n  <body>\n    <h1>Q3</h1>\n    <table>\n" + \
             "      <tr>\n        <td>\n        </td>\n      </tr>\n" * 20 + \
             '    </table>\n    <div class="footer">Sent by Nous</div>\n  </body>\n</html>'
    tool = create_send_email_tool(_make_settings())
    resp = asyncio.run(
        tool(to="tim@example.com", subject="Q3", body="", html_body=padded, attachments=str(f))
    )
    assert "visible body" in _text(resp).lower()
    assert len(no_real_send) == 0


def test_whitespace_only_plain_body_with_attachment_refused(no_real_send, tmp_path):
    """Sibling of the above on the plain-text branch: newlines are not content."""
    f = tmp_path / "report.pdf"
    f.write_bytes(b"%PDF-1.4 fake")
    tool = create_send_email_tool(_make_settings())
    resp = asyncio.run(
        tool(to="tim@example.com", subject="report",
             body="Hi\n" + "\n" * 250 + "\nT", attachments=str(f))
    )
    assert "visible body" in _text(resp).lower()
    assert len(no_real_send) == 0


def test_verbose_empty_template_refused(no_real_send):
    """P1: raw length counts tags + inline styles, so an empty styled template
    cleared the 600-char floor with zero rendered content (codex, PR #609)."""
    cell = ('<td style="padding:12px 16px;font-family:Helvetica,Arial,sans-serif;'
            'font-size:15px;color:#111827;line-height:1.6;border-bottom:1px solid #e5e7eb;"></td>')
    empty = ('<html><body><h1 style="font-family:Helvetica;font-size:24px;color:#111;'
             'margin:0 0 16px;"></h1><table role="presentation" cellpadding="0" '
             f'cellspacing="0" style="width:100%;border-collapse:collapse;">{cell * 3}</table>'
             '<div class="footer" style="font-size:12px;color:#6b7280;">x</div></body></html>')
    assert len(empty) > 600  # clears the raw floor — that was the bypass
    tool = create_send_email_tool(_make_settings())
    resp = asyncio.run(
        tool(to="tim@example.com", subject="Q3 Report", body="Q3 Report", html_body=empty)
    )
    assert "visible text" in _text(resp).lower()
    assert len(no_real_send) == 0


@pytest.mark.parametrize(
    "anchor",
    [
        '<a href = "http://evil.example.com">x</a>',
        "<a href= 'http://evil.example.com'>x</a>",
        "<a href =\t'http://evil.example.com'>x</a>",
        "<a href=http://evil.example.com>x</a>",
        '<a href="http://evil.example.com">x</a>',
    ],
)
def test_insecure_href_detected_across_attribute_spacing(no_real_send, anchor):
    """P2: HTML permits whitespace around '=' and unquoted values, so the
    HTTPS-only rule was trivially evaded (codex, PR #609)."""
    doc = _shell("Real content. " * 30 + anchor)
    tool = create_send_email_tool(_make_settings())
    resp = asyncio.run(
        tool(to="tim@example.com", subject="hi", body="plain body", html_body=doc)
    )
    assert "insecure" in _text(resp).lower()
    assert len(no_real_send) == 0


def test_real_short_canonical_email_still_passes(no_real_send):
    """Guard against over-tightening: a genuine short email must not be refused.

    Canonical renderer output measures ~158-210 chars of rendered text, so the
    thresholds are calibrated below that floor, not above it.
    """
    doc = _shell(
        "Deploy finished. All 56 tests pass and CI is green on the merge commit. "
        "No action needed from you; the rollback path is still armed if anything "
        "regresses overnight."
    )
    tool = create_send_email_tool(_make_settings())
    resp = asyncio.run(
        tool(to="tim@example.com", subject="Deploy status", body="Deploy finished.", html_body=doc)
    )
    assert "Email sent" in _text(resp)
    assert len(no_real_send) == 1


# --- Codex re-review of 9d72871: hidden nodes are not visible content ---

@pytest.mark.parametrize(
    "hidden_node",
    [
        '<script>var x = "{pad}";</script>',
        "<head><title>{pad}</title></head>",
    ],
)
def test_non_rendered_node_text_does_not_satisfy_content_floor(no_real_send, hidden_node):
    """P1: text inside script/head is never shown, so it must not count."""
    pad = "Padding text the recipient never sees. " * 4
    doc = _shell("").replace("<h1", hidden_node.format(pad=pad) + "<h1", 1)
    tool = create_send_email_tool(_make_settings())
    resp = asyncio.run(
        tool(to="tim@example.com", subject="Q3", body="Q3", html_body=doc)
    )
    assert "visible text" in _text(resp).lower()
    assert len(no_real_send) == 0


@pytest.mark.parametrize(
    "style", ["display:none", "visibility: hidden", "font-size:0", "opacity:0"]
)
def test_inline_hidden_content_refused(no_real_send, style):
    """P1: hidden padding could clear the content floors invisibly.

    Regex cannot match balanced nesting to find a hidden node's extent, so the
    gate refuses the markers outright — the canonical renderer never emits them.
    """
    pad = "Padding text the recipient never sees. " * 4
    doc = _shell(f'<div style="{style}">{pad}</div>')
    tool = create_send_email_tool(_make_settings())
    resp = asyncio.run(
        tool(to="tim@example.com", subject="Q3", body="Q3", html_body=doc)
    )
    assert "hidden content" in _text(resp).lower()
    assert len(no_real_send) == 0


@pytest.mark.parametrize("style", ["opacity:0.9", "opacity: 0.85", "font-size:0.9em"])
def test_near_zero_css_values_are_not_treated_as_hidden(no_real_send, style):
    """Guard the hidden-marker regex against its own false-positive shape.

    A bare ``0`` prefix would also match ordinary ``opacity:0.9``, refusing
    legitimate mail — the failure mode this gate must not have.
    """
    doc = _shell(
        f'<span style="{style}">Real, fully visible content that the recipient '
        f"reads in the message body.</span> " + "More real content. " * 6
    )
    tool = create_send_email_tool(_make_settings())
    resp = asyncio.run(
        tool(to="tim@example.com", subject="Status", body="Status update", html_body=doc)
    )
    assert "Email sent" in _text(resp)
    assert len(no_real_send) == 1


def test_unterminated_css_comment_linear_time(no_real_send):
    """P1 regression: _CSS_COMMENT_RE must be O(n), not O(n²).

    A style attribute with 10 000 unclosed /* openers previously caused
    catastrophic backtracking (~8.5 s for 60 KB).  The non-backtracking
    pattern completes in well under 1 second for any input.
    """
    # 10 000 unclosed openers — the pathological input for the old lazy .*? regex.
    bad_style = "/*a" * 10_000
    doc = _shell(f'<p style="{bad_style}">Real visible content here. ' + "words " * 30 + "</p>")
    tool = create_send_email_tool(_make_settings())
    t0 = time.monotonic()
    # Run synchronously through asyncio to exercise the full parse path.
    asyncio.run(
        tool(to="tim@example.com", subject="hi", body="plain", html_body=doc)
    )
    elapsed = time.monotonic() - t0
    assert elapsed < 1.0, f"CSS comment stripping took {elapsed:.2f}s — likely O(n²) regression"


# --- Codex re-review of b2bcee2 ---

@pytest.mark.parametrize("node", ["<div hidden>{pad}</div>", '<span hidden="">{pad}</span>'])
def test_hidden_attribute_content_refused(no_real_send, node):
    """P1: the HTML `hidden` boolean attribute hides content with no CSS at all."""
    pad = "Padding the recipient never sees. " * 4
    tool = create_send_email_tool(_make_settings())
    resp = asyncio.run(
        tool(to="tim@example.com", subject="Q3", body="Q3",
             html_body=_shell(node.format(pad=pad)))
    )
    assert "hidden content" in _text(resp).lower()
    assert len(no_real_send) == 0


def test_data_hidden_attribute_is_not_hidden_content(no_real_send):
    """False-positive guard: `data-hidden` hides nothing."""
    doc = _shell('<div data-hidden="1">Fully visible content. </div>' + "Real content. " * 12)
    tool = create_send_email_tool(_make_settings())
    resp = asyncio.run(
        tool(to="tim@example.com", subject="Status", body="Status", html_body=doc)
    )
    assert "Email sent" in _text(resp)
    assert len(no_real_send) == 1


@pytest.mark.parametrize("enc", ["http&#58;//evil.example.com", "http&#x3A;//evil.example.com"])
def test_entity_encoded_insecure_href_detected(no_real_send, enc):
    """P2: clients resolve `href="http&#58;//x"` as http://, so the raw-source
    regex alone missed it — the decoded view is scanned too (#484 precedent)."""
    doc = _shell("Real content. " * 12 + f'<a href="{enc}">x</a>')
    tool = create_send_email_tool(_make_settings())
    resp = asyncio.run(
        tool(to="tim@example.com", subject="hi", body="plain body", html_body=doc)
    )
    assert "insecure" in _text(resp).lower()
    assert len(no_real_send) == 0


def test_template_element_text_does_not_satisfy_content_floor(no_real_send):
    """`<template>` content is inert and never rendered (codex, 318bc56)."""
    pad = "Padding the recipient never sees. " * 4
    tool = create_send_email_tool(_make_settings())
    resp = asyncio.run(
        tool(to="tim@example.com", subject="Q3", body="Q3",
             html_body=_shell(f"<template>{pad}</template>"))
    )
    assert "visible text" in _text(resp).lower()
    assert len(no_real_send) == 0


def test_malformed_unclosed_tags_do_not_stall_the_handler(no_real_send):
    """Non-rendered stripping must be linear.

    The lazy-regex form was quadratic: 10k unclosed `<script>` starts in 86KB
    took 10.2s, stalling the event loop inside the async send handler before the
    message was even rejected (codex P2 on 318bc56).
    """
    import time

    payload = "<script>" * 10000 + "x" * 8000
    tool = create_send_email_tool(_make_settings())
    start = time.monotonic()
    resp = asyncio.run(
        tool(to="tim@example.com", subject="Q3", body="Q3", html_body=payload)
    )
    elapsed = time.monotonic() - start
    assert elapsed < 1.0, f"content gate took {elapsed:.2f}s on malformed input"
    assert len(no_real_send) == 0  # refused on completeness, not hung
    assert "Error" in _text(resp)


def test_scriptural_prose_is_not_stripped_as_a_script_tag(no_real_send):
    """Boundary guard: a tag-name prefix inside prose must survive stripping."""
    doc = _shell("Scriptural analysis of the headings. " * 8)
    tool = create_send_email_tool(_make_settings())
    resp = asyncio.run(
        tool(to="tim@example.com", subject="Notes", body="Notes", html_body=doc)
    )
    assert "Email sent" in _text(resp)
    assert len(no_real_send) == 1


# ---------------------------------------------------------------------------
# Regression tests for DOM-parser refactor (codex P1/P2 on this PR)
# ---------------------------------------------------------------------------

def test_comment_hidden_h1_and_table_refused(no_real_send):
    """P1: structural tags inside HTML comments render nothing — must be refused.

    The old regex matched ``<!-- <h1></h1><table></table> -->`` as if the tags
    were real.  The DOM parser drops comments before tracking rendered_tags.
    """
    # Body >= 600 chars (raw-length gate) + >= 80 chars visible prose (content
    # gate), but ALL h1/table are inside a comment.
    visible_prose = "This is real visible prose content for the recipient. " * 3
    doc = (
        "<html><body>"
        "<!-- <h1>Title hidden in comment</h1>"
        "<table><tr><td>Table hidden in comment</td></tr></table> -->"
        f"<p>{visible_prose}</p>"
        '<div class="footer">Sent by Nous</div>'
        "</body></html>"
        + "<!-- padding " + "x" * 700 + " -->"
    )
    assert len(doc) >= 600
    tool = create_send_email_tool(_make_settings())
    resp = asyncio.run(
        tool(to="tim@example.com", subject="hi", body="plain", html_body=doc)
    )
    text = _text(resp)
    assert "h1" in text.lower() or "table" in text.lower(), (
        "expected h1/table missing error but got: " + text
    )
    assert len(no_real_send) == 0


def test_prose_mentioning_display_none_is_allowed(no_real_send):
    """P2 false-positive fix: visible prose mentioning display:none must NOT refuse.

    The old regex scanned raw source, so an email *explaining* the gate
    (e.g. a status report about display:none checks) was refused in strict mode.
    The DOM parser checks only style ATTRIBUTE VALUES, not text nodes.
    """
    doc = _shell(
        "The content gate checks element style attributes for display:none and "
        "visibility:hidden declarations to prevent invisible padding. "
        "It also detects opacity:0 and font-size:0 as hidden-content markers. "
        "These checks are scoped to CSS attribute values only, not body text. "
        + "More real content. " * 8
    )
    tool = create_send_email_tool(_make_settings())
    resp = asyncio.run(
        tool(to="tim@example.com", subject="Gate status", body="Status update.", html_body=doc)
    )
    assert "Email sent" in _text(resp), "prose mentioning display:none was wrongly refused"
    assert len(no_real_send) == 1


@pytest.mark.parametrize("style", [
    "display:none",
    "display/**/:none",
    "display: none",
])
def test_style_attr_hidden_css_refused(no_real_send, style):
    """P2 regression guard: CSS hiding in a style *attribute* still refused.

    Also covers the CSS-comment bypass (display/**/:none) which the DOM parser
    strips before checking.
    """
    pad = "Content the recipient never sees. " * 4
    doc = _shell(f'<div style="{style}">{pad}</div>')
    tool = create_send_email_tool(_make_settings())
    resp = asyncio.run(
        tool(to="tim@example.com", subject="Q3", body="Q3", html_body=doc)
    )
    assert "hidden content" in _text(resp).lower()
    assert len(no_real_send) == 0


def test_attachment_error_surfaces_before_content_gate(no_real_send, tmp_path):
    """P2 ordering fix: a missing attachment error must precede the content gate.

    Previously _build_message ran AFTER the content gate, so a missing/oversized
    attachment combined with a short body surfaced the content-gate error
    ("pad your content") instead of the real problem.
    """
    tool = create_send_email_tool(_make_settings())
    resp = asyncio.run(
        tool(
            to="tim@example.com",
            subject="report",
            body="Short.",
            attachments=str(tmp_path / "nonexistent_file_xyz.pdf"),
        )
    )
    text = _text(resp)
    assert "not found" in text.lower(), (
        "expected attachment-not-found error but got: " + text
    )
    # Must NOT surface the content-gate short-body problem instead.
    assert "visible body" not in text.lower()
    assert len(no_real_send) == 0


def test_malformed_html_does_not_raise_in_content_gate(no_real_send):
    """Parse failure policy: malformed HTML must not raise; gate stays lenient.

    HTMLParser is generally robust, but truly broken input (e.g. bare angle
    brackets with no matching close) should never propagate an exception out
    to the async send handler.  The documented policy is partial-parse + leniency.
    """
    malformed = "<<<<" + "x>" * 50 + "<not-a-tag attr='>"
    tool = create_send_email_tool(_make_settings())
    # Should not raise — any response (Error or Email sent) is acceptable.
    try:
        resp = asyncio.run(
            tool(to="tim@example.com", subject="hi", body="plain", html_body=malformed)
        )
        # Got a response — no exception propagated.
        assert "content" in _text(resp).lower() or "Error" in _text(resp)
    except Exception as exc:
        pytest.fail(f"content gate raised on malformed HTML: {exc}")

# ---------------------------------------------------------------------------
# Regression tests for codex review findings on commit 9d96157 (PR #609)
# ---------------------------------------------------------------------------

def test_chrome_inflated_stub_body_with_attachment_refused(no_real_send, tmp_path):
    """F1: verbose h1 + footer chrome must not substitute for content.

    A descriptive h1 title (66 chars) + standard footer (76 chars) = 142 rendered
    chars — enough to exceed the 120-char stub threshold when chrome is naively
    counted toward it.  The fix measures the content region (excluding chrome).
    """
    f = tmp_path / "report.pdf"
    f.write_bytes(b"%PDF-1.4 fake")
    h1_text = "Q3 2026 Monthly Financial Performance and Analytics Summary Report"
    footer_text = (
        "Sent by Nous Cognitive Engine · Company Name Inc "
        "· Unsubscribe · View Online"
    )
    # Sanity: chrome alone exceeds the stub threshold.
    assert len(h1_text) + len(footer_text) > 120
    long_style = (
        "padding:12px 16px;font-family:Helvetica,Arial,sans-serif;"
        "font-size:15px;color:#111827;line-height:1.6;"
        "border-bottom:1px solid #e5e7eb;"
    )
    # Shell with verbose chrome but an EMPTY content cell.
    shell = (
        '<html><body style="margin:0;background-color:#f4f4f7;">'
        f'<h1 style="color:#111;margin:0 0 16px;font-size:22px;">{h1_text}</h1>'
        '<table role="presentation" cellpadding="0" cellspacing="0" '
        'style="width:100%;max-width:640px;border-collapse:collapse;background:#ffffff;">'
        f'<tr><td style="{long_style}"></td></tr></table>'
        f'<div class="footer" style="font-size:12px;color:#6b7280;">{footer_text}</div>'
        '</body></html>'
    )
    assert len(shell) >= 600  # raw-length floor must be cleared
    tool = create_send_email_tool(_make_settings())
    resp = asyncio.run(
        tool(to="tim@example.com", subject="Q3 Report", body="see attached",
             html_body=shell, attachments=str(f))
    )
    assert "visible body" in _text(resp).lower()
    assert len(no_real_send) == 0


def test_whitespace_prefixed_insecure_href_detected(no_real_send):
    """F2: leading whitespace in an href value does not bypass the http:// check.

    Browsers strip leading whitespace from href values before navigating;
    the old startswith() check on the raw value let '  http://evil' through.
    """
    doc = _shell("Real content. " * 30 + '<a href="  http://evil.example.com">click</a>')
    tool = create_send_email_tool(_make_settings())
    resp = asyncio.run(
        tool(to="tim@example.com", subject="hi", body="plain body", html_body=doc)
    )
    assert "insecure" in _text(resp).lower()
    assert len(no_real_send) == 0


def test_footer_in_html_comment_not_accepted(no_real_send):
    """F3: 'footer'/'Sent by' inside an HTML comment must not satisfy the footer check.

    The old check searched raw HTML source, so a commented-out footer bypassed it.
    The DOM-derived check uses parsed rendered elements only — comments are dropped.
    """
    html_comment_footer = _GOOD_HTML.replace(
        '<div class="footer">Sent by Nous · <a href="https://example.com">unsubscribe</a></div>',
        "<!-- Sent by Nous footer --><p>End of message.</p>",
    )
    # The raw source still contains "footer" and "sent by" (in the comment) —
    # the old raw-search check would have passed this.
    assert "footer" in html_comment_footer.lower()
    assert "sent by" in html_comment_footer.lower()
    tool = create_send_email_tool(_make_settings())
    resp = asyncio.run(
        tool(to="tim@example.com", subject="hi", body="plain", html_body=html_comment_footer)
    )
    assert "footer" in _text(resp).lower()
    assert len(no_real_send) == 0


def test_mime_construction_deferred_past_rate_limit(no_real_send, tmp_path, monkeypatch):
    """F4: _build_message is not called when the rate limit is already exhausted.

    Previously _build_message (which reads all attachment bytes) ran before the rate
    limiter, so an exhausted rate limit still triggered multi-MB file reads.  Now MIME
    construction is deferred until both the content gate and rate limiter pass.
    """
    from nous.api.email_tools import _build_message as _real_build

    build_calls: list[bool] = []

    def tracking_build(*args, **kwargs):
        build_calls.append(True)
        return _real_build(*args, **kwargs)

    monkeypatch.setattr("nous.api.email_tools._build_message", tracking_build)

    f = tmp_path / "data.bin"
    f.write_bytes(b"x" * 65536)  # 64 KB — non-trivial if read unnecessarily
    long_body = "Real content. " * 20

    tool = create_send_email_tool(_make_settings(email_max_per_hour=1))

    async def run():
        r1 = await tool(to="tim@example.com", subject="hi", body=long_body)
        build_calls.clear()  # discard the legitimate first build
        r2 = await tool(
            to="tim@example.com", subject="hi", body=long_body, attachments=str(f)
        )
        return r1, r2

    r1, r2 = asyncio.run(run())
    assert "Email sent" in _text(r1)
    assert "rate limit" in _text(r2).lower()
    assert not build_calls, "_build_message was called despite rate limit being exhausted"


# ---------------------------------------------------------------------------
# Regression tests for codex re-review findings (PR #609 follow-up)
# ---------------------------------------------------------------------------

def test_nested_div_inside_footer_not_leaked_to_content(no_real_send, tmp_path):
    """Finding 1 regression: nested element inside a footer-class div must not
    prematurely clear _footer_depth and let footer text escape into content_parts.

    Before the fix, <div class="footer"><div>…inner…</div>more footer</div>
    caused the inner </div> to match the footer root on the _footer_tag_stack and
    decrement _footer_depth to 0, so "more footer" was added to content_parts and
    inflated the stub-body check beyond _STUB_BODY_MIN_CHARS.
    """
    f = tmp_path / "report.pdf"
    f.write_bytes(b"%PDF-1.4 fake")
    # Inner text alone exceeds _STUB_BODY_MIN_CHARS (120). If it leaks out of
    # the footer region into content_parts, the stub gate will not fire.
    inner_text = "Inner nested footer text that must stay inside the footer. " * 3  # ~177 chars
    nested_footer = (
        '<div class="footer">'
        f"<div>{inner_text}</div>"
        "Sent by Nous"
        "</div>"
    )
    assert len(inner_text) > 120  # leaking this would suppress the gate

    doc = (
        "<html><body>"
        "<h1>Monthly Report</h1>"
        "<table><tr><td> </td></tr></table>"
        + nested_footer
        # Pad raw length past _HTML_MIN_CHARS=600 using a comment so the text
        # doesn't land in content_parts (comments are dropped by handle_comment).
        + f"<!-- pad: {'x' * 700} -->"
        + "</body></html>"
    )
    tool = create_send_email_tool(_make_settings())
    resp = asyncio.run(
        tool(
            to="tim@example.com",
            subject="Report",
            body="see attached",
            html_body=doc,
            attachments=str(f),
        )
    )
    # With the bug: inner_text leaks into content_parts → len > 120 → gate passes
    # With the fix: content_parts is empty (no real body) → stub gate fires
    assert "visible body" in _text(resp).lower()
    assert len(no_real_send) == 0


def test_embedded_tab_in_href_scheme_rejected(no_real_send):
    """Finding 2 regression: `ht&#9;tp://evil.example` decodes via HTMLParser
    to `ht\\ttp://evil.example`; lstrip alone cannot remove the embedded tab
    mid-scheme, so the HTTPS gate was bypassed.

    Per the WHATWG URL standard, embedded ASCII tab/LF/CR characters must be
    stripped throughout the URL value before scheme checking.
    """
    # &#9; is TAB (U+0009); HTMLParser with convert_charrefs=True decodes this
    # to a literal '\t' in the attribute value, not as leading whitespace.
    href_with_embedded_tab = "ht&#9;tp://evil.example.com"
    doc = _shell("Real content. " * 12 + f'<a href="{href_with_embedded_tab}">click here</a>')
    tool = create_send_email_tool(_make_settings())
    resp = asyncio.run(
        tool(to="tim@example.com", subject="hi", body="plain body", html_body=doc)
    )
    assert "insecure" in _text(resp).lower()
    assert len(no_real_send) == 0
