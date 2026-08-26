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
