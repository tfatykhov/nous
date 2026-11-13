"""Hostile-input regression tests for the F112 OAuth callback page (GET /integrations/mcp/oauth/callback).

The page is a fixed document that reflects nothing of the request: whatever the query string holds, the body is the same
bytes, no header repeats a value of the query, the response forbids script and caches nothing, and no log record
repeats a value either. Every input below is an obviously synthetic test string.

The app, the fake provider and the fixtures come from test_mcp_client_oauth.py (the same SDK floor skips them).
"""

from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import parse_qsl, quote, unquote, unquote_plus, urlencode

import httpx
import pytest

from nous.api.mcp_client_routes import NOT_SIGNED_IN_PAGE, PROXY_SECRET_HEADER, SIGNED_IN_PAGE
from tests import test_mcp_client_oauth as base

pytestmark = base.pytestmark

# The fixtures of the OAuth route tests, by name, so pytest finds them in this module.
make_env = base.make_env
_dns = base._dns
_restore_mcp_tools = base._restore_mcp_tools

PATH = "/integrations/mcp/oauth/callback"
LONG = "test-long-" + "A" * (100 * 1024)


def _enc(pairs: list[tuple[str, str]]) -> str:
    """A query string with every key and value percent-encoded (repeated keys stay repeated)."""
    return urlencode(pairs, quote_via=quote)


# Each case: a readable id and the raw query string, as it goes on the wire.
HOSTILE: list[tuple[str, str]] = [
    ("no-parameters", ""),
    ("empty-query-mark-only", ""),
    ("well-formed-code-and-state", _enc([("code", "test-code-0000"), ("state", "test-state-0000")])),
    (
        "code-state-and-iss",
        _enc([("code", "test-code-0000"), ("state", "test-state-0000"), ("iss", "https://issuer.test.invalid")]),
    ),
    (
        "provider-error",
        _enc([("error", "access_denied"), ("error_description", "test-denied-description"), ("state", "test-state-1")]),
    ),
    ("error-only", _enc([("error", "server_error")])),
    (
        "unknown-parameters",
        _enc([("test_unknown_alpha", "test-value-alpha"), ("test_unknown_beta", "test-value-beta")]),
    ),
    (
        "repeated-parameters",
        _enc([("code", "test-code-first"), ("code", "test-code-second"), ("state", "test-state-first")])
        + "&"
        + _enc([("state", "test-state-second"), ("error", "test-error-first"), ("error", "test-error-second")]),
    ),
    ("repeated-empty-and-blank", "code=&code=&state&=test-no-key-0000&&&"),
    ("long-code-100kb", _enc([("code", LONG), ("state", "test-state-0000")])),
    ("long-state-100kb", _enc([("state", LONG)])),
    ("long-unknown-key-and-value", _enc([("test_key_" + "K" * 4096, LONG)])),
    ("script-tag", _enc([("code", "<script>test_alert_marker(1)</script>"), ("state", "test-state-0000")])),
    ("script-src-tag", _enc([("error", '<script src="//test.invalid/test-marker.js"></script>')])),
    ("img-onerror-handler", _enc([("error_description", '<img src=x onerror="test_alert_marker(1)">')])),
    ("attribute-breakout-handler", _enc([("state", '" onmouseover="test_alert_marker(1)" x="')])),
    ("svg-onload", _enc([("code", "<svg/onload=test_alert_marker(1)>")])),
    ("javascript-url", _enc([("code", "javascript:test_alert_marker(1)"), ("iss", "javascript:test_alert_marker(2)")])),
    ("data-url", _enc([("error", "data:text/html,<b>test-marker-data</b>")])),
    ("close-title-breakout", _enc([("error_description", "</title><h1>test-marker-title</h1>")])),
    ("close-script-breakout", _enc([("code", "</script><h1>test-marker-script</h1>")])),
    ("close-body-and-html", _enc([("state", "</body></html><p>test-marker-after</p>")])),
    ("html-comment-opener", _enc([("state", "<!-- test-marker-comment")])),
    ("html-entities-named", _enc([("error", "&lt;script&gt;test_marker_entity&lt;/script&gt;&amp;quot;")])),
    ("html-entities-numeric", _enc([("error", "&#60;script&#62;test_marker_numeric&#x3c;/script&#x3e;")])),
    ("percent-encoded-markup-raw", "error=%3Cscript%3Etest_marker_pct%3C/script%3E&state=test-state-0000"),
    ("double-encoded-markup", "error=%253Cscript%253Etest_marker_double%253C%252Fscript%253E"),
    ("triple-encoded-markup", "error=%25253Cscript%25253Etest_marker_triple%25253C%25252Fscript%25253E"),
    ("overlong-utf8-lt", "error=%C0%BCscript%C0%BEtest_marker_overlong"),
    ("invalid-percent-escapes", "error=%zz%2%&state=%E0%A4%A&code=test-marker-badpct"),
    ("null-bytes", _enc([("code", "test-nul-\x00-marker"), ("state", "test-nul-\x00\x00-state")])),
    ("null-byte-in-key", _enc([("test-key-\x00-marker", "test-value-nul")])),
    ("cr-lf-header-injection", _enc([("state", "test-crlf\r\nSet-Cookie: test_injected=1\r\nX-Test-Injected: yes")])),
    ("cr-lf-in-code", _enc([("code", "test-line-one\r\n\r\n<html>test-marker-split</html>")])),
    ("lf-only-and-tab", _enc([("error", "test-lf\ntest-tab\tmarker")])),
    ("unicode-rtl-override", _enc([("error_description", "test-‮marker-rtl‬-⁦isolate⁩")])),
    ("unicode-zero-width-and-bom", _enc([("code", "test-​‍﻿-marker-zw")])),
    ("unicode-non-ascii-text", _enc([("error_description", "test-é中文\U0001f600-marker-utf8")])),
    ("unicode-raw-non-ascii-bytes", "error=test-é中文-marker-raw-utf8"),
    ("utf7-style-payload", _enc([("error_description", "+ADw-script+AD4-test_marker_utf7+ADw-/script+AD4-")])),
    (
        "utf7-style-with-charset-hint",
        _enc([("test_charset_hint", "utf-7"), ("code", "+ADw-script+AD4-alert(1)+ADw-/script+AD4-")]),
    ),
    ("template-and-format-markers", _enc([("state", "{0} {{test-marker-fmt}} ${test_marker_tpl} %s %(test)s")])),
    ("sql-and-path-looking", _enc([("code", "' OR 1=1 --"), ("state", "../../test-marker-path/..\\..")])),
    (
        "crlf-injection-in-redirect-like-params",
        _enc([("redirect", "//test.invalid/test-marker-redirect"), ("next", "/\\test.invalid")]),
    ),
    ("fragment-and-hash-chars", _enc([("code", "test-frag#marker-fragment"), ("state", "a#b?c&d=e")])),
]


def _tokens(query: str) -> set[str]:
    """Every value that would count as the query reflected: each key and value of the query, raw (still percent-encoded)
    and decoded once and twice, plus the whole query string. Short tokens are dropped (they would match by chance)."""
    found = {query}
    for part in query.split("&"):
        for piece in part.split("=", 1):
            raw = piece
            found.update({raw, unquote(raw), unquote_plus(raw), unquote(unquote(raw)), unquote_plus(unquote_plus(raw))})
    for key, value in parse_qsl(query, keep_blank_values=True, max_num_fields=None):
        found.update({key, value})
    return {token for token in found if len(token) >= 6}


async def _get(env: Any, query: str, *, method: str = "GET", secret: bool = True, body: bytes | None = None):
    """One request straight to the ASGI app with the query string as raw bytes (an HTTP client would refuse a URL of
    100 KB, and would normalise what it was given)."""
    headers = [(b"host", b"nous")]
    if secret:
        headers.append((PROXY_SECRET_HEADER.lower().encode(), base.PROXY.encode()))
    if body is not None:
        headers += [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": PATH,
        "raw_path": PATH.encode(),
        "root_path": "",
        "query_string": query.encode("utf-8"),
        "headers": headers,
        "client": ("127.0.0.1", 5000),
        "server": ("nous", 80),
    }
    sent_request = False
    started: dict[str, Any] = {}
    chunks: list[bytes] = []

    async def receive() -> dict[str, Any]:
        nonlocal sent_request
        if sent_request:
            return {"type": "http.disconnect"}
        sent_request = True
        return {"type": "http.request", "body": body or b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            started.update(message)
        elif message["type"] == "http.response.body":
            chunks.append(message.get("body", b""))

    await env.app(scope, receive, send)
    return httpx.Response(started["status"], headers=started["headers"], content=b"".join(chunks))


def _assert_nothing_reflected(response: httpx.Response, query: str) -> None:
    tokens = _tokens(query)
    body = response.content.decode("utf-8", "replace")
    for token in tokens:
        assert token not in body, "a value of the query is in the body"
    # Every header, every name and every value, whichever header it is (Location, Set-Cookie, Refresh, Link, ...).
    for name, value in response.headers.multi_items():
        for token in tokens:
            assert token not in name and token not in value, f"a value of the query is in the {name} header"


# --- invariant 1 and 2: the same bytes, nothing reflected ----------------------------


@pytest.mark.parametrize("query", [q for _, q in HOSTILE], ids=[i for i, _ in HOSTILE])
async def test_the_body_is_the_same_fixed_bytes_and_reflects_nothing(make_env, query):
    env = await make_env()
    baseline = await _get(env, "")
    response = await _get(env, query)
    assert baseline.content == NOT_SIGNED_IN_PAGE
    assert response.status_code == baseline.status_code == 400
    assert response.content == baseline.content  # byte-identical to the answer for no query at all
    assert response.content == NOT_SIGNED_IN_PAGE
    _assert_nothing_reflected(response, query)
    # No header the query could have caused: it adds none, and drops none, of the fixed set.
    assert response.headers.multi_items() == baseline.headers.multi_items()
    assert "set-cookie" not in response.headers
    assert "location" not in response.headers


@pytest.mark.parametrize("query", [q for _, q in HOSTILE], ids=[i for i, _ in HOSTILE])
async def test_the_headers_are_the_fixed_set_for_every_query(make_env, query):
    env = await make_env()
    response = await _get(env, query)
    content_type = response.headers["content-type"].replace(" ", "").lower()
    assert content_type == "text/html;charset=utf-8"
    csp = response.headers["content-security-policy"]
    assert "default-src 'none'" in csp
    assert "script-src" not in csp and "unsafe-inline" not in csp and "unsafe-eval" not in csp
    assert "*" not in csp and "http:" not in csp and "data:" not in csp
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-length"] == str(len(NOT_SIGNED_IN_PAGE))


# --- invariant 4: no oracle on the state ----------------------------


async def test_a_known_and_an_unknown_state_get_the_same_status_and_body_when_the_answer_is_an_error(make_env):
    env = await make_env()
    unknown = await _get(env, _enc([("error", "access_denied"), ("state", "test-state-unknown")]))
    started = (await base._start(env)).json()
    flow = env.manager.flows.get(started["flow_id"])
    state = flow.state  # read before the answer: a failed flow keeps no state
    known = await _get(env, _enc([("error", "access_denied"), ("state", state)]))
    again = await _get(env, _enc([("error", "access_denied"), ("state", state)]))  # the state is single use
    assert env.manager.flows.status(flow) == "failed"  # the known state was taken: the flow did end
    assert flow.state == ""
    for response in (unknown, known, again):
        assert response.status_code == 400
        assert response.content == NOT_SIGNED_IN_PAGE
    assert unknown.headers.multi_items() == known.headers.multi_items() == again.headers.multi_items()
    for response, query in ((known, state),):
        assert state.encode() not in response.content and query not in str(response.headers)


async def test_a_known_state_with_a_code_the_provider_refuses_is_the_same_page_as_an_unknown_state(make_env):
    env = await make_env()
    unknown = await _get(env, _enc([("code", "test-code-0000"), ("state", "test-state-unknown")]))
    started = (await base._start(env)).json()
    flow = env.manager.flows.get(started["flow_id"])
    known = await _get(env, _enc([("code", "test-code-0000"), ("state", flow.state)]))  # the provider never issued it
    assert known.status_code == unknown.status_code == 400
    assert known.content == unknown.content == NOT_SIGNED_IN_PAGE
    assert known.headers.multi_items() == unknown.headers.multi_items()


async def test_only_a_completed_sign_in_gets_another_page_and_that_page_is_fixed_too(make_env):
    env = await make_env()
    started = (await base._start(env)).json()
    params = env.world.authorize(started["authorization_url"])
    extra = _enc([*params.items(), ("test_unknown_gamma", "<script>test_marker_ok</script>")])
    ok = await _get(env, extra)
    assert ok.status_code == 200
    assert ok.content == SIGNED_IN_PAGE
    _assert_nothing_reflected(ok, extra)
    assert ok.headers["content-security-policy"] == "default-src 'none'"
    assert ok.headers["cache-control"] == "no-store"


# --- invariant 5: no log record holds a query value ----------------------------


def _own_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    """The records Nous wrote: not the test client's own request lines (httpx prints the URL it was asked for)."""
    return [r for r in caplog.records if r.name.split(".")[0] not in {"httpx", "httpcore", "asyncio"}]


@pytest.mark.parametrize("query", [q for _, q in HOSTILE], ids=[i for i, _ in HOSTILE])
async def test_no_log_record_holds_a_value_of_the_query(make_env, caplog, query):
    env = await make_env()
    caplog.set_level(logging.DEBUG)
    caplog.clear()
    await _get(env, query)
    tokens = _tokens(query)
    for record in _own_records(caplog):
        text = logging.Formatter().format(record)  # the message with its arguments, and any traceback
        assert record.getMessage() in text
        for token in tokens:
            assert token not in text, f"{record.name}: a value of the query is in a log record"
            assert token not in repr(record.args)


async def test_no_log_record_holds_the_state_or_code_of_a_known_flow_that_fails(make_env, caplog):
    env = await make_env()
    caplog.set_level(logging.DEBUG)
    started = (await base._start(env)).json()
    flow = env.manager.flows.get(started["flow_id"])
    secrets = [flow.state, flow.verifier, "test-code-never-issued-0001"]
    caplog.clear()
    await _get(env, _enc([("code", secrets[2]), ("state", flow.state), ("error_description", "test-reflected-text-1")]))
    for record in _own_records(caplog):
        text = logging.Formatter().format(record)
        for value in [*secrets, "test-reflected-text-1"]:
            assert value not in text


# --- invariant 6: the document has nothing active in it ----------------------------


@pytest.mark.parametrize("page", [SIGNED_IN_PAGE, NOT_SIGNED_IN_PAGE], ids=["signed-in", "not-signed-in"])
def test_the_pages_hold_no_script_handler_url_or_form(page):
    text = page.decode("utf-8")
    lowered = text.lower()
    assert "<script" not in lowered
    assert not re.search(r"\bon[a-z]+\s*=", lowered), "an inline event handler attribute"
    assert "javascript:" not in lowered
    for needle in ("http://", "https://", "//", "ftp:", "data:"):
        assert needle not in lowered, needle
    for tag in (
        "<form",
        "<input",
        "<button",
        "<iframe",
        "<object",
        "<embed",
        "<link",
        "<img",
        "<a ",
        "<style",
        "<base",
    ):
        assert tag not in lowered, tag
    assert not re.search(r"\b(src|href|action|formaction|srcdoc|style)\s*=", lowered)
    assert "&" not in text  # no entity, so nothing for a reflected entity to be confused with


async def test_the_served_body_has_nothing_active_for_the_hostile_queries_either(make_env):
    env = await make_env()
    for _, query in HOSTILE:
        text = (await _get(env, query)).text.lower()
        assert "<script" not in text and "<form" not in text
        assert not re.search(r"\bon[a-z]+\s*=", text)
        assert "http://" not in text and "https://" not in text and "//" not in text


# --- the guard in front, and other methods ----------------------------


_GUARDED = [(i, q) for i, q in HOSTILE if i in {"script-tag", "cr-lf-header-injection", "long-code-100kb"}]


@pytest.mark.parametrize("query", [q for _, q in _GUARDED], ids=[i for i, _ in _GUARDED])
async def test_without_the_proxy_secret_the_answer_is_forbidden_and_reflects_nothing(make_env, query):
    env = await make_env()
    response = await _get(env, query, secret=False)
    assert response.status_code == 403
    assert response.json() == {"error": "forbidden"}
    _assert_nothing_reflected(response, query)


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
@pytest.mark.parametrize("with_secret", [True, False], ids=["with-secret", "without-secret"])
async def test_other_methods_are_refused_and_reflect_nothing(make_env, method, with_secret):
    env = await make_env()
    query = _enc([("code", "<script>test_marker_method</script>"), ("state", "test-state-method-0000")])
    response = await _get(env, query, method=method, secret=with_secret, body=b'{"code":"test-body-marker-0000"}')
    assert response.status_code == 405
    assert set(response.headers["allow"].replace(" ", "").split(",")) == {"GET", "HEAD"}
    assert response.content not in (SIGNED_IN_PAGE, NOT_SIGNED_IN_PAGE)
    _assert_nothing_reflected(response, query)
    assert b"test-body-marker-0000" not in response.content
    assert "test-body-marker-0000" not in str(response.headers)


async def test_head_answers_with_the_same_headers_and_no_body(make_env):
    env = await make_env()
    query = _enc([("code", "<script>test_marker_head</script>"), ("state", "test-state-head-0000")])
    head = await _get(env, query, method="HEAD")
    get = await _get(env, query)
    assert head.status_code == get.status_code == 400
    assert head.content in (b"", NOT_SIGNED_IN_PAGE)  # Starlette sends the body, the server drops it
    for name in (
        "content-type",
        "content-security-policy",
        "cache-control",
        "referrer-policy",
        "x-content-type-options",
    ):
        assert head.headers[name] == get.headers[name]
    _assert_nothing_reflected(head, query)
