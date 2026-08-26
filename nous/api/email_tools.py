"""Guarded send_email tool (F078.1).

Provides a `send_email` tool that validates the recipient against an
allowlist before sending, scans the message for obvious secrets, and
rate-limits sends. This is the *safe chokepoint* alongside the existing
agent-authored bash+smtplib path (which stays untouched for BC).

Mirrors the structure of telegram_tools.py: closure factory +
``_SEND_EMAIL_SCHEMA`` + ``register_email_tools(dispatcher, settings)``.

Security note (lesson from decision 73ed8f2d): smtplib/login errors can
embed credentials or the SMTP host. We log the full error server-side but
return only a generic ``email send failed: <ExceptionType>`` to the model.
"""

from __future__ import annotations

import asyncio
import html
import logging
import os
import re
import smtplib
import time
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html.parser import HTMLParser
from typing import Any

from nous.config import Settings

logger = logging.getLogger(__name__)

# Secret patterns scanned across subject + body. A hit rejects the send.
_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"sk-[A-Za-z0-9]{16,}"),
    re.compile(r"AKIA[0-9A-Z]{12,}"),
    re.compile(r"password\s*[:=]", re.IGNORECASE),
    re.compile(r"Bearer [A-Za-z0-9._-]{20,}"),
)


def _ok(text: str) -> dict[str, Any]:
    """MCP-compliant success response."""
    return {"content": [{"type": "text", "text": text}]}


def _error(text: str) -> dict[str, Any]:
    """MCP-compliant error response."""
    return {"content": [{"type": "text", "text": f"Error: {text}"}]}


def _normalize_recipients(value: Any) -> list[str]:
    """Coerce a recipient field (str or list) into a list of stripped addresses."""
    if value is None:
        return []
    if isinstance(value, str):
        items = value.split(",")
    elif isinstance(value, (list, tuple)):
        items = []
        for v in value:
            items.extend(str(v).split(","))
    else:
        items = [str(value)]
    return [a.strip() for a in items if a and a.strip()]


def _parse_allowlist(raw: str) -> set[str]:
    """Parse the CSV allowlist into a set of lowercased addresses."""
    return {a.strip().lower() for a in (raw or "").split(",") if a.strip()}


def _read_allowlist_file(path: str, cache: dict[str, Any]) -> set[str]:
    """Read the hot-reloadable allowlist file, mtime-cached.

    One address per line or CSV; '#' starts a comment. Re-reads only when the
    file's mtime changes, so a live edit takes effect on the NEXT send with no
    restart. On any read error, returns the last-known set (fail-closed — an
    error never widens the allowlist).
    """
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return cache.get("addrs", set())
    if cache.get("mtime") == mtime:
        return cache["addrs"]
    try:
        with open(path, encoding="utf-8") as f:
            raw = f.read()
    except OSError:
        return cache.get("addrs", set())
    addrs: set[str] = set()
    for line in raw.splitlines():
        line = line.split("#", 1)[0]
        for a in line.split(","):
            a = a.strip().lower()
            if a:
                addrs.add(a)
    cache["mtime"] = mtime
    cache["addrs"] = addrs
    return addrs


def _scan_secrets(text: str) -> bool:
    """Return True if the text matches any known secret pattern."""
    return any(p.search(text) for p in _SECRET_PATTERNS)


def _strip_tags(s: str) -> str:
    """Single-pass, linear-time tag removal (codex P2 on #484).

    The naive ``<[^>]+>`` regex rescans to end-of-string from every
    unclosed ``<``, going quadratic on malformed HTML — and this runs
    synchronously inside the async handler on an unbounded body. Tags
    become a space. Text after an unclosed trailing ``<`` is dropped from
    THIS view only; the raw and unescape-only views still scan it.
    """
    out: list[str] = []
    in_tag = False
    for ch in s:
        if in_tag:
            if ch == ">":
                in_tag = False
        elif ch == "<":
            in_tag = True
            out.append(" ")
        else:
            out.append(ch)
    return "".join(out)


def _html_to_text(s: str) -> str:
    """Strip HTML tags and decode entities for secret scanning (#484).

    Mail clients render entity-encoded content (``sk-&#97;bcd...`` displays
    as ``sk-abcd...``), so the scan must also see the decoded text. Tags are
    replaced with a space; entities are decoded after tag-stripping so a
    decoded ``&lt;`` cannot fabricate a tag. The raw HTML is still scanned
    separately — this is an additional view, not a replacement.
    """
    return html.unescape(_strip_tags(s))


# ---------------------------------------------------------------------------
# Content-completeness gate helpers
# ---------------------------------------------------------------------------

# Placeholder / stub markers: (label, pattern) pairs.
# Applied to subject, body, and raw html_body.
# TBD / XXX are case-sensitive acronyms to avoid false positives like "tbdx"
# or "xxxl"; all others are case-insensitive.
_PLACEHOLDER_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("PLACEHOLDER", re.compile(r"\bPLACEHOLDER\b", re.IGNORECASE)),
    ("TODO:", re.compile(r"\bTODO\s*:", re.IGNORECASE)),
    ("TBD", re.compile(r"\bTBD\b")),
    ("lorem ipsum", re.compile(r"\blorem\s+ipsum\b", re.IGNORECASE)),
    ("{{...}} template braces", re.compile(r"\{\{")),
    ("<insert", re.compile(r"<insert\s", re.IGNORECASE)),
    ("XXX", re.compile(r"\bXXX\b")),
    ("FIXME", re.compile(r"\bFIXME\b", re.IGNORECASE)),
    ("see attached rendering", re.compile(r"\bsee\s+attached\s+rendering\b", re.IGNORECASE)),
    ("see attachment for", re.compile(r"\bsee\s+attachment\s+for\b", re.IGNORECASE)),
    ("content in the attached", re.compile(r"\bcontent\s+in\s+the\s+attached\b", re.IGNORECASE)),
)

# Tags whose body text is never shown to the recipient — excluded from
# visible text, hidden detection, and structural tag tracking.
_SKIP_TAGS = frozenset({"script", "style", "head", "title", "template"})

# CSS patterns for hidden content — applied ONLY to element style attribute
# values, never to text content. This scoping fixes the false-positive where
# prose mentioning "display:none" (e.g. a status email about the gate itself)
# would trip the hidden-content check (codex P2 on this PR).
_CSS_HIDDEN_RE = re.compile(
    # The zero-value lookaheads prevent ``opacity:0.9`` / ``font-size:0.9em``
    # from matching — those are perfectly ordinary values in real emails.
    r"(display\s*:\s*none"
    r"|visibility\s*:\s*hidden"
    r"|font-size\s*:\s*0(?![.\d])"
    r"|opacity\s*:\s*0(?!\.\d*[1-9]))",
    re.IGNORECASE,
)
# CSS comments inside a property declaration (e.g. ``display/**/:none``) are
# stripped before applying ``_CSS_HIDDEN_RE`` so both spellings are caught.
def _strip_css_comments(s: str) -> str:
    """Remove CSS block comments from *s* in O(n).

    A regex with lazy quantifiers (``/\\*.*?\\*/`` + DOTALL) backtracks
    catastrophically on unterminated ``/*`` openers, taking O(n²) time.
    This single-pass scanner never backtracks.
    """
    result: list[str] = []
    i = 0
    n = len(s)
    while i < n:
        if s[i] == "/" and i + 1 < n and s[i + 1] == "*":
            # Skip everything until the matching */ (or end of string).
            i += 2
            while i < n:
                if i + 1 < n and s[i] == "*" and s[i + 1] == "/":
                    i += 2
                    break
                i += 1
        else:
            result.append(s[i])
            i += 1
    return "".join(result)



class _EmailDOMWalker(HTMLParser):
    """Single-pass DOM walker for email content-gate checks.

    Produces:
    - visible_parts: text from rendered nodes (script/style/head/title/template
      bodies and HTML comments are excluded — comments are never rendered and
      comment-hidden tags like ``<!-- <h1/> -->`` must not satisfy structural
      checks).
    - content_parts: visible text outside structural chrome (h1 and footer-class
      elements). Used by ``content_text()`` for the stub-body-with-attachment
      check so that a verbose h1 title and standard footer cannot substitute for
      the missing message body (F1, codex P1 on #609).
    - rendered_tags: tag names that appear as start tags in the rendered tree.
      Tags inside comments or _SKIP_TAGS bodies are excluded, so a
      commented-out or ``<template>``-wrapped ``<h1>`` does not count.
    - hidden_detected: True when a rendered element's style attribute declares
      display:none / visibility:hidden / zero font-size or opacity, OR when an
      element carries the HTML ``hidden`` boolean attribute. Text content is
      never scanned (fixes the P2 false positive).
    - insecure_hrefs: list of href attribute values starting with ``http://``.
      HTMLParser decodes character references in attributes, so
      ``href="http&#58;//x"`` arrives already decoded as ``http://x``. Leading
      URL whitespace is stripped before the scheme check so ``href="  http://x"``
      is caught (F2, codex P2 on #609).
    - has_footer_element: True when at least one rendered element carries a CSS
      class of ``footer`` — used to derive footer presence from the parsed DOM
      instead of the raw source so commented-out footers do not satisfy the
      check (F3, codex P2 on #609).
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._skip_tag_stack: list[str] = []
        self.visible_parts: list[str] = []
        self.content_parts: list[str] = []  # F1: non-chrome visible text
        self.rendered_tags: set[str] = set()
        self.hidden_detected: bool = False
        self.insecure_hrefs: list[str] = []
        self.has_footer_element: bool = False  # F3: a rendered element has class "footer"
        self._h1_depth: int = 0               # F1: depth inside <h1> elements
        self._footer_depth: int = 0  # F1: nesting depth inside footer region (counts ALL elements)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag_lower = tag.lower()

        if self._skip_depth == 0:
            # Rendered context — record the tag itself.
            self.rendered_tags.add(tag_lower)

            if tag_lower in _SKIP_TAGS:
                # Enter skip mode; only content (not the tag) is excluded.
                # ``<style>`` still lands in rendered_tags for the Gmail check.
                self._skip_depth += 1
                self._skip_tag_stack.append(tag_lower)
                return  # Don't inspect attributes of non-rendered elements.

            # Inspect attributes for hidden detection and href checking.
            attrs_dict: dict[str, str] = {}
            for name, value in attrs:
                # Boolean attrs like ``hidden`` have value=None from the parser.
                attrs_dict[name.lower()] = value if value is not None else ""

            # HTML ``hidden`` boolean attribute — element is not rendered.
            if "hidden" in attrs_dict:
                self.hidden_detected = True

            # CSS hiding via inline style (attribute value only, not text).
            style_val = attrs_dict.get("style", "")
            if style_val:
                stripped_css = _strip_css_comments(style_val)
                if _CSS_HIDDEN_RE.search(stripped_css):
                    self.hidden_detected = True

            # Insecure http:// href — apply WHATWG URL normalization before checking
            # the scheme: strip leading whitespace and remove embedded ASCII
            # tab/newline/CR/FF characters that browsers strip before following
            # a link, so ``href="  http://x"`` and ``href="ht&#9;tp://x"``
            # (decoded to ``ht\ttp://x``) are both caught (F2).
            href = re.sub(r"[\t\n\r\f]", "", attrs_dict.get("href", "")).lstrip(" ")
            if href and href.lower().startswith("http://"):
                self.insecure_hrefs.append(href)

            # F1/F3: track structural chrome — h1 and footer-class elements.
            # Text inside these is required frame, not the message body, so it
            # must not count toward the stub-body content threshold (F1).
            if tag_lower == "h1":
                self._h1_depth += 1
            if self._footer_depth > 0:
                # Inside a footer region: count every element to maintain the
                # correct nesting depth.  Only decrementing on the tag that
                # entered the footer would prematurely clear depth when a nested
                # element shares the same tag name (e.g. inner <div> inside
                # <div class="footer">) (F1, codex P1 re-review).
                self._footer_depth += 1
            else:
                cls = attrs_dict.get("class", "")
                if "footer" in cls.split():
                    self._footer_depth += 1
                    self.has_footer_element = True  # F3: footer found in rendered DOM

        else:
            # Inside a skip-tag body — only track nested skip tags for correct
            # depth management; don't record them in rendered_tags.
            if tag_lower in _SKIP_TAGS:
                self._skip_depth += 1
                self._skip_tag_stack.append(tag_lower)

    def handle_endtag(self, tag: str) -> None:
        tag_lower = tag.lower()
        if (
            self._skip_depth > 0
            and self._skip_tag_stack
            and self._skip_tag_stack[-1] == tag_lower
        ):
            self._skip_depth -= 1
            self._skip_tag_stack.pop()
        elif self._skip_depth == 0:
            # F1: maintain chrome-depth counters for rendered end tags.
            if tag_lower == "h1" and self._h1_depth > 0:
                self._h1_depth -= 1
            if self._footer_depth > 0:
                self._footer_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            self.visible_parts.append(data)
            # F1: accumulate only non-chrome text in content_parts so the
            # stub-body check can measure the substantive message region.
            if self._h1_depth == 0 and self._footer_depth == 0:
                self.content_parts.append(data)

    def handle_comment(self, data: str) -> None:
        # Comments are never rendered; drop them entirely.  A body that is
        # entirely ``<!-- <h1/><table/> -->`` renders nothing and must fail
        # the structural checks (fixes codex P1 on this PR).
        pass

    def visible_text(self) -> str:
        """Whitespace-collapsed visible text (what the recipient reads)."""
        return re.sub(r"\s+", " ", "".join(self.visible_parts)).strip()

    def content_text(self) -> str:
        """F1: visible text excluding structural chrome (h1 and footer elements)."""
        return re.sub(r"\s+", " ", "".join(self.content_parts)).strip()


def _parse_html_email(html_body: str) -> _EmailDOMWalker:
    """Parse html_body in one DOM walk; return the populated walker.

    Failure policy: on any parse exception return partial results without
    raising.  HTMLParser tolerates most malformed HTML, so exceptions are
    rare.  When one occurs the partial walker reflects whatever was successfully
    parsed — a truncated parse is more likely to MISS structural markers than
    to gain false ones, so the gate tends toward leniency on malformed input.
    Biasing toward leniency is deliberate: a false refusal on the send hot path
    is the failure mode this gate exists to prevent.
    """
    walker = _EmailDOMWalker()
    try:
        walker.feed(html_body)
        walker.close()
    except Exception:
        pass
    return walker


# Detects HTML-escaped markup in **visible text** after parsing.
# ``&lt;a&gt;`` in source decodes to ``<a>`` in visible text via HTMLParser.
_VISIBLE_LEAKED_TAG_RE = re.compile(
    r"</?(?:a|b|strong|em|i|br|p|ul|li|span|div|table)\b", re.IGNORECASE
)
# Detects double-encoded entities in **visible text** after parsing.
# ``&amp;mdash;`` in source decodes to ``&mdash;`` in visible text.
_VISIBLE_LEAKED_ENTITY_RE = re.compile(r"&[a-zA-Z]{2,8};")

# Minimum visible-text length when attachments are present (stub-body guard).
# Applied to WHITESPACE-COLLAPSED rendered text, not raw markup: a pretty-printed
# template's indentation is not message content (codex P1 on #609). Calibrated
# against canonical renderer output — real emails measure 158-210 collapsed chars,
# an empty styled template measures 0.
_STUB_BODY_MIN_CHARS = 120
# Minimum raw html_body length for a real HTML email (structural floor).
_HTML_MIN_CHARS = 600
# Minimum RENDERED content length for a real HTML email. The raw floor above
# counts tags and inline-style attributes, so a verbose empty template clears it
# with zero content (codex P1 on #609). Set well below the smallest observed
# canonical email (158) and far above an empty template (0).
_HTML_MIN_VISIBLE_CHARS = 80


def _check_content_completeness(
    subject: str, body: str, html_body: str | None, attachments: list[str]
) -> list[str]:
    """Return a list of human-readable problems; empty list == send is allowed."""
    problems: list[str] = []

    # --- Checks for all sends ---

    # Empty/whitespace-only body when no html_body supplied.
    if not html_body and not body.strip():
        problems.append("plain-text body is empty or whitespace-only")

    # Placeholder / stub markers in subject, body, or html_body (raw).
    for field_name, field_value in (
        ("subject", subject),
        ("body", body),
        ("html_body", html_body or ""),
    ):
        if not field_value:
            continue
        for label, pat in _PLACEHOLDER_PATTERNS:
            if pat.search(field_value):
                problems.append(
                    f"placeholder/stub marker {label!r} detected in {field_name}"
                )
                break  # one report per field — first match is enough

    # Parse HTML once; all structural checks below reuse the single walk.
    walker: _EmailDOMWalker | None = None
    rendered: str = ""
    if html_body:
        walker = _parse_html_email(html_body)
        rendered = walker.visible_text()

    # Stub-body-with-attachment: attachments present but substantive content too short.
    # For HTML emails, measure content_text() (chrome-excluded) not rendered (full
    # visible text): a descriptive h1 title and standard footer can push rendered
    # past the threshold even with an empty content region (F1, codex P1 on #609).
    if attachments:
        if html_body and walker is not None:
            content = walker.content_text()
        else:
            # Plain-text: same whitespace-collapse — newlines are not content.
            content = re.sub(r"\s+", " ", body).strip()
        if len(content) < _STUB_BODY_MIN_CHARS:
            problems.append(
                f"attachments present but visible body is only {len(content)} chars "
                f"(must be \u2265{_STUB_BODY_MIN_CHARS}) \u2014 this is the documented "
                "stub-body-with-attachment failure pattern; include the actual content "
                "in body/html_body rather than a short stub"
            )

    # --- Checks that apply only when html_body is provided ---
    if html_body and walker is not None:
        if len(html_body) < _HTML_MIN_CHARS:
            problems.append(
                f"html_body is only {len(html_body)} chars "
                f"(must be \u2265{_HTML_MIN_CHARS}) \u2014 likely a placeholder or empty send"
            )

        if len(rendered) < _HTML_MIN_VISIBLE_CHARS:
            problems.append(
                f"html_body renders to only {len(rendered)} chars of visible text "
                f"(must be \u2265{_HTML_MIN_VISIBLE_CHARS}) \u2014 the markup is present but "
                "the message is empty; put the actual content inside the template"
            )

        if walker.hidden_detected:
            problems.append(
                "hidden content detected (display:none / visibility:hidden / "
                "zero font-size or opacity / the hidden attribute) \u2014 "
                "hidden text is invisible to the "
                "recipient and cannot substitute for message content"
            )

        if "style" in walker.rendered_tags:
            problems.append(
                "<style> block present \u2014 Gmail strips <style>; use inline styles only"
            )

        if "h1" not in walker.rendered_tags:
            problems.append("no <h1> element found in html_body \u2014 email header is missing")

        if "table" not in walker.rendered_tags:
            problems.append(
                "no <table> element found in html_body \u2014 "
                "canonical table-based email layout was bypassed"
            )

        # Footer check: derive from the parsed rendered tree, not raw source (F3).
        # ``<!-- Sent by Nous -->`` or ``<!-- footer -->`` satisfies a raw-source
        # search but renders nothing — recipients see no footer.
        rendered_lower = rendered.lower()
        if not walker.has_footer_element and "sent by" not in rendered_lower:
            problems.append(
                'html_body is missing a footer: '
                'no rendered element with class "footer" and '
                '"Sent by" not found in the rendered text'
            )

        if walker.insecure_hrefs:
            problems.append(
                "insecure http:// link in an href attribute \u2014 use https:// only"
            )

        if _VISIBLE_LEAKED_TAG_RE.search(rendered):
            problems.append(
                "HTML-escaped markup detected (e.g. &lt;a&gt;) \u2014 "
                "a renderer helper was called with esc=True on HTML content; "
                "the model sees raw tag text rather than formatted elements"
            )

        if _VISIBLE_LEAKED_ENTITY_RE.search(rendered):
            problems.append(
                "double-encoded HTML entity detected (e.g. &amp;mdash;) \u2014 "
                "renders as literal &mdash; instead of \u2014 in the email client"
            )

    return problems

def _normalize_paths(value: Any) -> list[str]:
    """Coerce an attachments field (str or list) into a list of stripped paths."""
    if value is None:
        return []
    items = [value] if isinstance(value, str) else list(value)
    return [str(p).strip() for p in items if p and str(p).strip()]


def _build_message(
    settings: Settings,
    subject: str,
    body: str,
    from_addr: str,
    to_list: list[str],
    cc_list: list[str],
    attachments: list[str],
    html_body: str | None = None,
) -> tuple[Any | None, str | None]:
    """Build the MIME message. Returns (msg, None) on success or (None, error).

    With no attachments and no html_body → plain MIMEText (unchanged from v1).
    With html_body → multipart/alternative with plain text first, HTML second.
    With attachments → MIMEMultipart("mixed") wrapping the body part plus file
    parts; each path is validated (regular file, readable, within the total size
    cap) before any send. NOTE: attachment *contents* are not secret-scanned —
    the recipient allowlist (trusted recipients only) is the guard, and this is
    strictly safer than the unguarded bash+smtplib path.
    """
    if html_body:
        body_part: Any = MIMEMultipart("alternative")
        body_part.attach(MIMEText(body))
        body_part.attach(MIMEText(html_body, "html"))
    else:
        body_part = MIMEText(body)

    if not attachments:
        msg: Any = body_part
    else:
        msg = MIMEMultipart("mixed")
        msg.attach(body_part)
        cap = settings.email_max_attachment_mb * 1024 * 1024
        total = 0
        for path in attachments:
            if not os.path.isfile(path):
                return None, f"attachment not found or not a regular file: {path}"
            try:
                total += os.path.getsize(path)
                if total > cap:
                    return None, f"attachments exceed the {settings.email_max_attachment_mb}MB total limit"
                with open(path, "rb") as fh:
                    data = fh.read()
            except OSError:
                return None, f"attachment unreadable: {path}"
            name = os.path.basename(path)
            part = MIMEApplication(data, Name=name)
            part["Content-Disposition"] = f'attachment; filename="{name}"'
            msg.attach(part)
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = ", ".join(to_list)
    if cc_list:
        msg["Cc"] = ", ".join(cc_list)
    return msg, None


def _validate_attachments(settings: Settings, attachments: list[str]) -> str | None:
    """Stat-only attachment validation: check existence and total size without reading.

    Returns an error string on failure, None on success. Called before the content gate
    and rate limiter so cheap rejections surface fast and multi-MB file reads are deferred
    until after all lightweight checks pass (F4, codex P2 on #609).
    """
    cap = settings.email_max_attachment_mb * 1024 * 1024
    total = 0
    for path in attachments:
        if not os.path.isfile(path):
            return f"attachment not found or not a regular file: {path}"
        try:
            total += os.path.getsize(path)
        except OSError:
            return f"attachment unreadable: {path}"
        if total > cap:
            return f"attachments exceed the {settings.email_max_attachment_mb}MB total limit"
    return None


def _send_email_sync(
    settings: Settings,
    recipients: list[str],
    msg: Any,  # MIMEText or MIMEMultipart (with attachments)
) -> None:
    """Blocking SMTP send. Runs in a worker thread via asyncio.to_thread."""
    server = smtplib.SMTP(settings.email_smtp_host, settings.email_smtp_port)
    try:
        server.starttls()
        server.login(settings.email_user, settings.email_password)
        server.send_message(msg, to_addrs=recipients)
    finally:
        try:
            server.quit()
        except Exception:  # noqa: BLE001 — quit() errors must not mask the real result
            pass


def create_send_email_tool(settings: Settings):
    """Create the send_email tool closure.

    The rate-limit window lives in this factory scope so it persists across
    calls. Only successful sends record a timestamp.
    """
    # Monotonic timestamps of successful sends, within the current hour window.
    _send_times: list[float] = []
    # F078.1.1: mtime-cache for the hot-reloadable allowlist file.
    _file_cache: dict[str, Any] = {"mtime": None, "addrs": set()}

    async def send_email(
        to: Any,
        subject: str,
        body: str,
        cc: Any = None,
        attachments: Any = None,
        html_body: Any = None,
    ) -> dict[str, Any]:
        """Send an email to allowlisted recipient(s).

        Args:
            to: Recipient address(es) — string or list. Each must be allowlisted.
            subject: Email subject line.
            body: Plain-text email body.
            cc: Optional CC address(es) — string or list. Each must be allowlisted.
            attachments: Optional file path(s) — string or list — to attach (e.g. a
                generated .docx/.pdf report). Each must be a readable file; the total
                size must be within NOUS_EMAIL_MAX_ATTACHMENT_MB.
            html_body: Optional HTML body. When provided, the email is sent as
                multipart/alternative with the plain ``body`` as the fallback part.
                Use this for styled newsletter-format emails.

        Returns:
            MCP-compliant response confirming the send or naming the rejection reason.
        """
        # 1. Enabled + creds check.
        if not settings.email_tool_enabled:
            return _error("send_email tool is disabled (NOUS_EMAIL_TOOL_ENABLED=false).")
        if not settings.email_user or not settings.email_password:
            return _error(
                "Email credentials not configured. Set NOUS_EMAIL_USER and "
                "NOUS_EMAIL_PASSWORD."
            )

        to_list = _normalize_recipients(to)
        cc_list = _normalize_recipients(cc)
        if not to_list:
            return _error("No recipient provided in 'to'.")

        # 2. Recipient allowlist (the core guard). Empty allowlist => reject all.
        # Effective = env CSV (static base) UNION the hot-reloadable file (read at
        # send-time, mtime-cached) — adding an address to the file needs no restart.
        allowlist = _parse_allowlist(settings.email_allowlist)
        if settings.email_allowlist_file:
            allowlist |= _read_allowlist_file(settings.email_allowlist_file, _file_cache)
        for addr in to_list + cc_list:
            if addr.lower() not in allowlist:
                return _error(
                    f"Recipient '{addr}' is not on the email allowlist; refusing to "
                    "send. To allow it, add the address to NOUS_EMAIL_ALLOWLIST "
                    "(operator action)."
                )

        # 3. Secret scan (secondary guard). html_body is scanned in three
        # views (#484): raw source (secrets in tag attributes), entity-decoded
        # source (codex P1 — encoded secrets *inside* attributes, which
        # tag-stripping would remove before decoding), and tag-stripped +
        # decoded text (secrets a mail client renders from encoded content).
        html_views = (
            f"\n{html.unescape(html_body)}\n{_html_to_text(html_body)}"
            if html_body
            else ""
        )
        if _scan_secrets(f"{subject}\n{body}\n{html_body or ''}{html_views}"):
            return _error(
                "email appears to contain a secret (API key, password, or token); "
                "refusing to send."
            )

        # Normalize attachments here so both the content gate and the builder use
        # the same list without computing it twice.
        attach_list = _normalize_paths(attachments)

        # 4. Lightweight attachment validation (stat-only: existence + total size, no
        # file reads). Surfaces attachment-not-found before the content gate so the
        # real problem is reported; file contents are deferred until after all cheap
        # rejections pass (F4, codex P2 on #609).
        if attach_list:
            attach_err = _validate_attachments(settings, attach_list)
            if attach_err:
                return _error(attach_err)
        all_recipients = to_list + cc_list

        # 5. Content-completeness gate — runs BEFORE the rate limiter so a refused
        # send never consumes rate-limit budget. Set email_content_gate="warn" to
        # log problems without refusing, or "off" to skip entirely. There is no
        # per-call bypass: the model must not be able to talk its way past the gate.
        if settings.email_content_gate != "off":
            problems = _check_content_completeness(
                subject, body, html_body or None, attach_list
            )
            if problems:
                lines = ["email content failed completeness check:"]
                for p in problems:
                    lines.append(f"  • {p}")
                lines.append(
                    "Fix every issue listed above and resend. "
                    "Do not pass placeholder text or a stub body when attachments are present."
                )
                gate_msg = "\n".join(lines)
                if settings.email_content_gate == "warn":
                    logger.warning("send_email content gate: %s", gate_msg)
                else:  # "strict"
                    return _error(gate_msg)

        # 6. Rate limit — in-process sliding window over the last hour.
        now = time.monotonic()
        cutoff = now - 3600.0
        _send_times[:] = [t for t in _send_times if t > cutoff]
        if len(_send_times) >= settings.email_max_per_hour:
            return _error(
                f"email rate limit reached ({settings.email_max_per_hour}/hour); "
                "try again later."
            )

        # 7. Build the MIME message (reads attachment file contents) — deferred until
        # after the content gate and rate limiter so a cheap rejection never blocks on
        # multi-MB attachment reads (F4, codex P2 on #609).
        msg, build_err = _build_message(
            settings, subject, body, settings.email or settings.email_user,
            to_list, cc_list, attach_list, html_body=html_body or None,
        )
        if build_err:
            return _error(build_err)

        try:
            await asyncio.to_thread(_send_email_sync, settings, all_recipients, msg)
        except Exception as e:  # noqa: BLE001 — sanitize: never leak smtplib/creds detail
            # Sanitize exceptions: log full server-side, return generic to the model.
            logger.error(
                "send_email failed (to=%s): %s: %s",
                to_list,
                type(e).__name__,
                e,
            )
            return _error(f"email send failed: {type(e).__name__}")

        # 7. Record success for rate limiting and return confirmation.
        _send_times.append(now)
        cc_note = f" (cc: {', '.join(cc_list)})" if cc_list else ""
        att_note = f" with {len(attach_list)} attachment(s)" if attach_list else ""
        return _ok(f"Email sent to {', '.join(to_list)}{cc_note}{att_note}.")

    return send_email


# ---------------------------------------------------------------------------
# Tool schema
# ---------------------------------------------------------------------------

_SEND_EMAIL_SCHEMA = {
    "description": (
        "Send an email (optionally with file attachments) to an allowlisted recipient. "
        "The recipient must be on the configured allowlist or the send is refused. "
        "Prefer this tool over ad-hoc bash+smtplib for sending email — it is the safe, "
        "guarded path. The message is scanned for secrets and rate-limited."
    ),
    "type": "object",
    "properties": {
        "to": {
            "type": ["string", "array"],
            "items": {"type": "string"},
            "description": "Recipient email address(es). String or list. Must be allowlisted.",
        },
        "subject": {
            "type": "string",
            "description": "Email subject line.",
        },
        "body": {
            "type": "string",
            "description": "Plain-text email body.",
        },
        "html_body": {
            "type": "string",
            "description": (
                "Optional rich-HTML email body. When provided, the email is sent as "
                "multipart/alternative with the plain-text `body` as the fallback part "
                "— use this for styled newsletter-format emails."
            ),
        },
        "cc": {
            "type": ["string", "array"],
            "items": {"type": "string"},
            "description": "Optional CC address(es). String or list. Must be allowlisted.",
        },
        "attachments": {
            "type": ["string", "array"],
            "items": {"type": "string"},
            "description": (
                "Optional file path(s) to attach (e.g. a generated .docx/.pdf report). "
                "String or list. Each must be a readable file; total size within the cap."
            ),
        },
    },
    "required": ["to", "subject", "body"],
}


def register_email_tools(dispatcher, settings: Settings) -> None:
    """Register the guarded send_email tool with the dispatcher."""
    handler = create_send_email_tool(settings)
    dispatcher.register("send_email", handler, _SEND_EMAIL_SCHEMA)
