"""F097 §6.3 — the Android push client and the server routes must agree.

Two codebases, one contract, and nothing else checks it. The Python route
tests prove the server accepts a shape; the Kotlin tests prove the client
sends one. Neither notices when those are different shapes.

They were. The first version of this pair had `TokenWorker` POST to a
PUT-only route, and the worker treated the resulting 405 as "this server has
no push" — so registration would have failed silently, forever, while
reporting success to the user in Settings. That is the failure mode this
file exists to prevent, so it reads the Kotlin source directly and runs in
the always-on CI, like the catalog-coverage ratchet next door.

Source reading is deliberately literal. A parser that understood Kotlin
would be a second thing to maintain and get wrong; what matters here is
only that a specific verb and a specific path appear together.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ANDROID = ROOT / "android" / "app" / "src" / "main" / "kotlin" / "us" / "fatykhov" / "nous" / "companion"
TOKEN_WORKER = ANDROID / "push" / "TokenWorker.kt"
PUSH_MANAGER = ANDROID / "push" / "PushManager.kt"
MESSAGING = ANDROID / "push" / "NousMessagingService.kt"
REST = ROOT / "nous" / "api" / "rest.py"
PUSH = ROOT / "nous" / "a2ui" / "push.py"

# `Http` method -> the HTTP verb it issues (OkHttpTransport).
_VERB_OF = {"get": "GET", "postJson": "POST", "putJson": "PUT"}


def _text(path: Path) -> str:
    if not path.is_file():
        pytest.skip(f"android sources not present: {path}")
    return path.read_text(encoding="utf-8")


def _client_calls(source: str) -> set[tuple[str, str]]:
    """`(VERB, path)` for every `http.<method>("<path>"...)` in the source."""
    calls = set()
    for method, path in re.findall(r'\.(get|postJson|putJson)\(\s*"([^"]+)"', source):
        calls.add((_VERB_OF[method], path.split("?")[0]))
    return calls


def _server_routes() -> set[tuple[str, str]]:
    """`(VERB, path)` for every `/a2ui/push/*` Route in rest.py.

    A Route with no `methods=` is GET, per Starlette.
    """
    routes = set()
    source = REST.read_text(encoding="utf-8")
    for match in re.finditer(r'Route\(\s*"(/a2ui/push/[^"]*)"([^)]*)\)', source):
        path, rest = match.group(1), match.group(2)
        methods = re.findall(r'"(GET|PUT|POST|DELETE|PATCH)"', rest) or ["GET"]
        for verb in methods:
            routes.add((verb, path))
    return routes


def _normalise(path: str) -> str:
    """Collapse a path parameter so `/tokens/{id}` and `/tokens/abc` compare."""
    return re.sub(r"\{[^}]+\}", "{}", path)


def test_every_push_endpoint_the_app_calls_exists_on_the_server() -> None:
    """The verb is part of the contract, not an implementation detail.

    A right path with a wrong verb is a 405, and the client is written to
    keep going on some statuses — so this must compare (verb, path) pairs,
    never paths alone.
    """
    client = {
        (verb, path)
        for verb, path in _client_calls(_text(TOKEN_WORKER)) | _client_calls(_text(PUSH_MANAGER))
        if path.startswith("/a2ui/push/")
    }
    server = {(verb, _normalise(path)) for verb, path in _server_routes()}

    assert client, "expected the app to call at least one push endpoint"
    unmatched = {call for call in client if call not in server}
    assert not unmatched, (
        f"the app calls push endpoints the server does not serve: {sorted(unmatched)}; "
        f"server has {sorted(server)}"
    )


def test_the_token_registration_body_matches_what_the_server_reads() -> None:
    """Field names, in both directions.

    A field the server reads but the app never sends silently defaults; a
    field the app sends but the server ignores silently does nothing.
    """
    worker = _text(TOKEN_WORKER)
    sent = set(re.findall(r'put\("(\w+)"', worker))
    handler = REST.read_text(encoding="utf-8")
    block = handler[handler.index("async def a2ui_push_register") :][:2000]
    read = set(re.findall(r'body\.get\("(\w+)"', block))

    assert sent == read, f"app sends {sorted(sent)}, server reads {sorted(read)}"


def test_the_message_types_the_server_sends_are_the_ones_the_app_handles() -> None:
    """A type the app does not handle is a notification that never appears."""
    server_types = set(re.findall(r'"type": "(\w+)"', PUSH.read_text(encoding="utf-8")))
    app_types = set(re.findall(r'"(\w+)" ->', _text(MESSAGING)))

    assert server_types, "expected the server to send at least one message type"
    assert server_types <= app_types, (
        f"server sends {sorted(server_types)}, app handles {sorted(app_types)}"
    )


def test_the_surface_payload_keys_the_app_reads_are_all_sent() -> None:
    """`surface_id` especially: without it the app cannot tag or cancel."""
    server = PUSH.read_text(encoding="utf-8")
    sent = set(re.findall(r'^\s+"(\w+)": ', server, re.MULTILINE)) | set(
        re.findall(r'data\["(\w+)"\]', server)
    )
    read = set(re.findall(r'd\["(\w+)"\]', _text(MESSAGING)))

    missing = read - sent
    assert not missing, f"the app reads payload keys the server never sends: {sorted(missing)}"


def test_the_configured_package_matches_the_apps_application_id() -> None:
    """FCM's `restricted_package_name` rejects every send if these differ."""
    gradle = (ROOT / "android" / "app" / "build.gradle.kts").read_text(encoding="utf-8")
    (application_id,) = re.findall(r'applicationId\s*=\s*"([^"]+)"', gradle)
    config = (ROOT / "nous" / "config.py").read_text(encoding="utf-8")
    block = config[config.index("a2ui_android_package") :][:400]
    (default,) = re.findall(r'default="([^"]+)"', block)

    assert application_id == default
