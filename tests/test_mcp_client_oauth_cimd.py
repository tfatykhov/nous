"""F112 MCP client: signing in with a client ID metadata document. With ``NOUS_MCP_CLIENT_ID_METADATA_URL`` naming the
document's address, a provider that says it takes one gets that address as the client id and nothing is registered;
otherwise the client is chosen as before (the owner's, the reused one, dynamic registration). A pending or stored
client that is another document than the one in use is not used again. The document's route is the one route with no
proxy secret, and holds the client name and the redirect address only.

The app, the fake provider and the fixtures come from test_mcp_client_oauth.py (the same SDK floor skips them).
"""

from __future__ import annotations

import dataclasses
import hashlib
import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from starlette.applications import Starlette

from nous.api.mcp_client_routes import PROXY_SECRET_HEADER, SIGNED_IN_PAGE, build_mcp_client_routes
from nous.mcp_client import oauth, secrets
from tests import test_mcp_client_oauth as base
from tests.test_mcp_client_oauth import ALPHA, BASE, PROXY, REDIRECT, _add, _callback, _signed_in, _start

pytestmark = base.pytestmark

# The fixtures of the OAuth route tests, by name, so pytest finds them in this module.
make_env = base.make_env
_dns = base._dns
_restore_mcp_tools = base._restore_mcp_tools

PATH = "/integrations/mcp/oauth/client-metadata.json"
DOCUMENT = f"{BASE}{PATH}"
ELSEWHERE = "https://static.example.org/nous/client.json"
SUPPORTED = "client_id_metadata_document_supported"


def _client_id(started: httpx.Response) -> str:
    return parse_qs(urlsplit(started.json()["authorization_url"]).query)["client_id"][0]


def _registered(env: Any) -> int:
    return sum(1 for _m, where in env.world.hits if where.endswith("/register"))


async def _get(app: Any, path: str = PATH, **headers: str) -> httpx.Response:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://nous") as client:
        return await client.get(path, headers=headers)


# --- the sign-in -----------------------------------------------------------------------------------------------------


async def test_a_provider_that_takes_only_a_metadata_document_signs_in_with_its_address_as_the_client_id(make_env):
    env = await make_env(document=DOCUMENT)
    env.world.as_meta[SUPPORTED] = True
    del env.world.as_meta["registration_endpoint"]  # no dynamic registration on offer
    started = await _start(env)
    assert started.status_code == 200, started.text
    assert started.json()["registration"] == "metadata"
    query = parse_qs(urlsplit(started.json()["authorization_url"]).query)
    assert query["client_id"] == [DOCUMENT] and query["redirect_uri"] == [REDIRECT]
    flow_id = started.json()["flow_id"]
    assert (await _callback(env, env.world.authorize(started.json()["authorization_url"]))).content == SIGNED_IN_PAGE
    exchange = env.world.forms[-1]
    assert exchange["client_id"] == DOCUMENT and "client_secret" not in exchange  # a public client
    assert _registered(env) == 0

    assert (await _add(env, flow_id)).status_code == 201
    record = env.manager.stored_oauth("alpha", ALPHA, (await env.registry.list_secrets())["alpha"])
    assert (record.client_id, record.client_secret, record.token_auth_method) == (DOCUMENT, None, "none")
    assert (await env.dispatcher.dispatch("mcp__alpha__search", {"q": "x"}))[1] is False
    again = await _start(env)  # the stored client is the document in use
    assert again.json()["registration"] == "reused" and _client_id(again) == DOCUMENT


@pytest.mark.parametrize("document", [ELSEWHERE, f"{BASE}/static/nous-client.json"])
async def test_a_document_hosted_elsewhere_is_the_client_id_as_given(make_env, document):
    env = await make_env(document=document)
    env.world.as_meta[SUPPORTED] = True
    started = await _start(env)
    assert started.json()["registration"] == "metadata" and _client_id(started) == document
    assert _registered(env) == 0  # before dynamic registration, which this provider offers too


@pytest.mark.parametrize(
    "document,supported",
    [
        ("", True),  # the host names no document
        (DOCUMENT, None),  # the provider does not say it takes one
        (DOCUMENT, False),
        (DOCUMENT, "true"),  # only the JSON true counts
        ("http://nous.example.com/client.json", True),  # not an address a document can have
    ],
)
async def test_without_a_usable_document_or_a_provider_that_takes_one_the_client_is_registered(
    make_env, document, supported
):
    env = await make_env(document=document)
    if supported is not None:
        env.world.as_meta[SUPPORTED] = supported
    started = await _start(env)
    assert started.status_code == 200, started.text
    assert started.json()["registration"] == "dynamic" and _client_id(started) == "test-client-dynamic-0001"


async def test_a_provider_whose_token_endpoint_takes_no_client_without_a_secret_is_not_given_the_document(make_env):
    env = await make_env(document=DOCUMENT)
    env.world.as_meta[SUPPORTED] = True
    env.world.as_meta["token_endpoint_auth_methods_supported"] = ["private_key_jwt", "client_secret_post"]
    started = await _start(env)
    assert started.json()["registration"] == "dynamic" and _client_id(started) == "test-client-dynamic-0001"
    del env.world.as_meta["token_endpoint_auth_methods_supported"]  # it lists none: the document is tried
    assert (await _start(env, name="beta")).json()["registration"] == "metadata"


async def test_the_owners_client_id_comes_before_the_document(make_env):
    env = await make_env(document=DOCUMENT)
    env.world.as_meta[SUPPORTED] = True
    started = await _start(env, client_id="test-client-owner-0001")
    assert started.json()["registration"] == "provided" and _client_id(started) == "test-client-owner-0001"


async def test_a_provider_that_takes_only_a_document_names_the_setting_when_the_host_has_none(make_env):
    env = await make_env()
    env.world.as_meta[SUPPORTED] = True
    del env.world.as_meta["registration_endpoint"]
    started = await _start(env)
    assert started.status_code == 409
    assert "NOUS_MCP_CLIENT_ID_METADATA_URL" in started.json()["error"] and REDIRECT in started.json()["error"]
    # A provider that could not be given one is not told about a setting that would not help: its token endpoint
    # takes no client without a secret, or it takes no document at all.
    env.world.as_meta["token_endpoint_auth_methods_supported"] = ["client_secret_post"]
    secret_only = await _start(env)
    assert secret_only.status_code == 409 and "NOUS_MCP_CLIENT_ID_METADATA_URL" not in secret_only.json()["error"]
    env.world.as_meta["token_endpoint_auth_methods_supported"] = ["none"]
    del env.world.as_meta[SUPPORTED]
    plain = await _start(env)
    assert plain.status_code == 409 and "NOUS_MCP_CLIENT_ID_METADATA_URL" not in plain.json()["error"]


# --- a client from before the setting changed -------------------------------------------------------------------------


async def test_a_registered_client_stays_in_use_when_a_document_becomes_available(make_env):
    env = await make_env()
    assert (await _add(env, await _signed_in(env))).status_code == 201  # registered dynamically
    env.settings.mcp_client_id_metadata_url = DOCUMENT
    env.world.as_meta[SUPPORTED] = True
    again = await _start(env)
    assert again.json()["registration"] == "reused" and _client_id(again) == "test-client-dynamic-0001"


@pytest.mark.parametrize(
    "now,registration,client_id",
    [
        ("", "dynamic", "test-client-dynamic-0001"),  # the setting was removed
        (ELSEWHERE, "metadata", ELSEWHERE),  # the document moved
    ],
)
async def test_a_stored_client_that_is_another_document_is_not_used_again(make_env, now, registration, client_id):
    env = await make_env(document=DOCUMENT)
    env.world.as_meta[SUPPORTED] = True
    assert (await _add(env, await _signed_in(env))).status_code == 201
    env.settings.mcp_client_id_metadata_url = now
    again = await _start(env)
    assert again.status_code == 200, again.text
    assert again.json()["registration"] == registration and _client_id(again) == client_id


async def test_a_stored_document_client_is_not_used_again_once_the_provider_takes_none(make_env):
    env = await make_env(document=DOCUMENT)
    env.world.as_meta[SUPPORTED] = True
    assert (await _add(env, await _signed_in(env))).status_code == 201
    del env.world.as_meta[SUPPORTED]
    again = await _start(env)
    assert again.json()["registration"] == "dynamic" and _client_id(again) == "test-client-dynamic-0001"


async def test_start_again_does_not_reuse_a_pending_client_that_is_another_document(make_env):
    env = await make_env(document=DOCUMENT)
    env.world.as_meta[SUPPORTED] = True
    assert (await _start(env)).json()["registration"] == "metadata"
    same = await _start(env)  # "Start again" with the setting unchanged
    assert same.json()["registration"] == "reused" and _client_id(same) == DOCUMENT
    env.settings.mcp_client_id_metadata_url = ELSEWHERE
    moved = await _start(env)
    assert moved.json()["registration"] == "metadata" and _client_id(moved) == ELSEWHERE


@pytest.mark.parametrize("secret", [None, base.CLIENT_SECRET])
@pytest.mark.parametrize("document", ["", DOCUMENT])
async def test_a_client_the_owner_typed_is_never_taken_for_a_document_whatever_its_id_looks_like(
    make_env, secret, document
):
    env = await make_env(document=document)
    typed = "https://apps.example.org/clients/nous"  # the owner's own client, whose id is an address
    extra = {"client_id": typed} if secret is None else {"client_id": typed, "client_secret": secret}
    started = await _start(env, **extra)
    assert started.json()["registration"] == "provided"
    pending = await _start(env)  # "Start again" with nothing typed: the pending client
    assert pending.json()["registration"] == "reused" and _client_id(pending) == typed
    assert (await _callback(env, env.world.authorize(pending.json()["authorization_url"]))).content == SIGNED_IN_PAGE
    assert (await _add(env, pending.json()["flow_id"])).status_code == 201
    record = env.manager.stored_oauth("alpha", ALPHA, (await env.registry.list_secrets())["alpha"])
    assert record.client_document is False
    again = await _start(env)  # "Sign in again" on the card: the stored client
    assert again.json()["registration"] == "reused" and _client_id(again) == typed


@pytest.mark.parametrize("document", ["", ELSEWHERE])  # the host names none, or the very address the owner types
async def test_the_owner_typing_the_documents_address_is_the_owners_client(make_env, document):
    env = await make_env(document=document)
    started = await _start(env, client_id=ELSEWHERE)
    assert started.json()["registration"] == "provided"
    assert (await _callback(env, env.world.authorize(started.json()["authorization_url"]))).content == SIGNED_IN_PAGE
    assert (await _add(env, started.json()["flow_id"])).status_code == 201
    record = env.manager.stored_oauth("alpha", ALPHA, (await env.registry.list_secrets())["alpha"])
    assert record.client_document is False
    env.settings.mcp_client_id_metadata_url = ""
    again = await _start(env)
    assert again.json()["registration"] == "reused" and _client_id(again) == ELSEWHERE


async def test_retyping_a_stored_document_clients_address_keeps_it_the_document(make_env):
    env = await make_env(document=DOCUMENT)
    env.world.as_meta[SUPPORTED] = True
    assert (await _add(env, await _signed_in(env))).status_code == 201
    typed = await _start(env, client_id=DOCUMENT)  # "Sign in again" with the address typed: the same client
    assert typed.json()["registration"] == "provided"
    assert (await _callback(env, env.world.authorize(typed.json()["authorization_url"]))).content == SIGNED_IN_PAGE
    record = env.manager.stored_oauth("alpha", ALPHA, (await env.registry.list_secrets())["alpha"])
    assert record.client_document is True  # stored at once, still marked
    env.settings.mcp_client_id_metadata_url = ""  # so once the setting is removed it is not used again
    again = await _start(env)
    assert again.json()["registration"] == "dynamic" and _client_id(again) == "test-client-dynamic-0001"


# --- the stored record ------------------------------------------------------------------------------------------------


async def test_the_record_says_its_client_was_the_document_and_keeps_saying_so_across_a_refresh(make_env):
    env = await make_env(document=DOCUMENT)
    env.world.as_meta[SUPPORTED] = True
    assert (await _add(env, await _signed_in(env))).status_code == 201
    record = env.manager.stored_oauth("alpha", ALPHA, (await env.registry.list_secrets())["alpha"])
    assert record.client_document is True
    fresh = await oauth.refresh_tokens(record, env.manager.oauth_http, env.manager.allow_private_hosts)
    assert fresh.client_document is True and fresh.access_token != record.access_token
    assert env.world.forms[-1]["client_id"] == DOCUMENT  # a refresh sends the stored client id


def test_a_record_reads_the_same_on_a_build_from_before_the_field_unless_its_client_was_the_document():
    record = secrets._oauth_of(
        b'{"v":1,"issuer":"i","token_endpoint":"t","client_id":"c","client_secret":null,"token_auth_method":"none",'
        b'"resource":"r","scope":null,"access_token":"a","refresh_token":null,"expires_at":null}'
    )
    assert record.client_document is False and record.lifetime is None  # sealed before either field existed
    assert b"client_document" not in secrets.oauth_plaintext(record)  # written only when true
    document = dataclasses.replace(record, client_document=True)
    plain = secrets.oauth_plaintext(document)
    assert b'"client_document":true' in plain and secrets._oauth_of(plain) == document
    with pytest.raises(secrets.SecretUnavailable):
        secrets._oauth_of(plain.replace(b'"client_document":true', b'"client_document":"yes"'))


# --- the setting ------------------------------------------------------------------------------------------------------


def test_the_address_of_a_document_follows_the_client_identifier_rules():
    for good in (DOCUMENT, ELSEWHERE, "https://nous.example.com:8443/c", f"  {DOCUMENT}\n", f"{BASE}/a%2Fb.json"):
        assert oauth.client_metadata_url(good) == good.strip(), good
    for bad in (
        "",
        None,
        MagicMock(),
        "http://nous.example.com/client.json",
        "https://nous.example.com",
        "https://nous.example.com/",
        "https://nous.example.com//",
        "https://nous.example.com/a/../client.json",
        "https://nous.example.com/./client.json",
        "https://nous.example.com/a/%2e%2e/client.json",  # a dot segment, percent-encoded
        "https://nous.example.com/%2E/client.json",
        "https://nous.example.com/.%2e/client.json",
        "HTTPS://nous.example.com/client.json",  # compared as plain text: the scheme in lower case
        "https://user@nous.example.com/client.json",
        "https://nous.example.com/client.json?v=1",
        "https://nous.example.com/client.json?",
        "https://nous.example.com/client.json#top",
        "https://nous.example.com/client.json#",
        "https://nous.example.com/cli ent.json",
        "https:///client.json",
        "https://nous.example.com:port/client.json",
        "https://nous.example.com/" + "a" * 600,
    ):
        assert oauth.client_metadata_url(bad) is None, bad


def test_the_document_is_what_dynamic_registration_sends_with_its_own_address_as_the_client_id():
    assert oauth.client_metadata_document(DOCUMENT, REDIRECT) == {
        "client_id": DOCUMENT,
        "client_name": "Nous",
        "redirect_uris": [REDIRECT],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }


# --- the document's route ---------------------------------------------------------------------------------------------


def _app(document: Any, *, base_url: Any = BASE, digest: str = "", manager: Any = None) -> Starlette:
    settings = SimpleNamespace(
        agent_id="a",
        mcp_client_ui_secret_sha256=digest,
        a2ui_public_base_url=base_url,
        mcp_client_id_metadata_url=document,
    )
    return Starlette(routes=build_mcp_client_routes(database=MagicMock(), settings=settings, manager=manager, bus=None))


async def test_the_document_is_served_with_no_proxy_secret_no_client_running_and_nothing_of_the_request():
    app = _app(DOCUMENT)  # no digest and no client: every other route answers 503
    plain = await _get(app)
    assert plain.status_code == 200 and plain.headers["content-type"] == "application/json"
    assert plain.headers["cache-control"] == "public, max-age=3600"
    assert plain.json() == oauth.client_metadata_document(DOCUMENT, REDIRECT)
    assert plain.json()["client_id"] == DOCUMENT and plain.json()["redirect_uris"] == [REDIRECT]
    noisy = await _get(app, PATH + "?client_id=test-injected&x=%3Cb%3E", **{"X-Forwarded-Host": "other.example.com"})
    assert noisy.content == plain.content
    assert (await _get(app, "/integrations/mcp/servers")).status_code == 503
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://nous") as client:
        assert (await client.post(PATH, json={})).status_code == 405


async def test_a_base_address_with_a_path_serves_the_document_under_it():
    under = "https://example.com/nous"
    served = await _get(_app(f"{under}{PATH}", base_url=under))
    assert served.status_code == 200 and served.json()["client_id"] == f"{under}{PATH}"
    assert served.json()["redirect_uris"] == [f"{under}/integrations/mcp/oauth/callback"]
    assert (await _get(_app(f"https://example.com{PATH}", base_url=under))).status_code == 404


@pytest.mark.parametrize(
    "document,base_url",
    [
        ("", BASE),
        (MagicMock(), BASE),
        ("http://nous.example.com/client.json", BASE),
        # Any other address than this route's own is a document hosted elsewhere: never served here, where its
        # client id would not be the address it is served at.
        (ELSEWHERE, BASE),
        (f"{BASE}/another/path.json", BASE),
        (f"https://NOUS.example.com{PATH}", BASE),
        (f"https://nous.example.com:443{PATH}", BASE),
        (f"https://nous.example.com.{PATH}", BASE),
        (f"{DOCUMENT}/", BASE),
        (DOCUMENT, "http://nous.example.com"),  # no redirect address: sign-in is unavailable
        (DOCUMENT, MagicMock()),
    ],
)
async def test_no_document_is_served_unless_the_one_in_use_is_at_this_routes_own_address(document, base_url):
    answer = await _get(_app(document, base_url=base_url))
    assert answer.status_code == 404 and answer.json() == {"error": "not found"}


async def test_the_route_stops_serving_when_the_setting_is_emptied_or_moved(make_env):
    env = await make_env(document=DOCUMENT)
    assert (await _get(env.app)).json()["client_id"] == DOCUMENT
    for now in ("", ELSEWHERE):
        env.settings.mcp_client_id_metadata_url = now
        assert (await _get(env.app)).status_code == 404, now


async def test_head_answers_like_get_with_no_body_and_the_host_header_changes_nothing():
    app = _app(DOCUMENT)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://other.example.com") as c:
        head, got = await c.head(PATH), await c.get(PATH)
    assert head.status_code == 200 and head.content == b""
    assert got.json()["client_id"] == DOCUMENT


def test_startup_says_whether_a_document_is_in_use_and_whether_it_is_served_here(caplog):
    caplog.set_level(logging.INFO, logger="nous.api.mcp_client_routes")
    digest = hashlib.sha256(PROXY.encode()).hexdigest()

    def said(document: Any, base_url: Any = BASE) -> list[tuple[str, str]]:
        caplog.clear()
        _app(document, base_url=base_url, digest=digest)
        return [(r.levelname, r.getMessage()) for r in caplog.records]

    assert said("") == [] and said(MagicMock()) == []
    (served,) = said(DOCUMENT)
    assert served[0] == "INFO" and "served here" in served[1] and "not served here" not in served[1]
    for elsewhere in (ELSEWHERE, f"{BASE}/another/path.json"):  # a mistyped address looks the same
        ((level, text),) = said(elsewhere)
        assert level == "INFO" and "not served here" in text and elsewhere not in text
    for document, base_url in (("http://nous.example.com/client.json", BASE), (DOCUMENT, "http://nous.example.com")):
        ((level, text),) = said(document, base_url)
        assert level == "WARNING" and "NOUS_MCP_CLIENT_ID_METADATA_URL" in text and "NOUS_A2UI_PUBLIC_BASE_URL" in text


async def test_the_document_is_the_only_route_that_needs_no_proxy_secret(make_env):
    env = await make_env(document=DOCUMENT)
    public = []
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=env.app), base_url="http://nous") as client:
        for route in env.app.routes:
            path = route.path.replace("{name}", "alpha").replace("{flow_id}", "some-flow")
            for method in sorted(route.methods - {"HEAD"}):
                answer = await client.request(method, path, content="{}", headers={"content-type": "application/json"})
                if answer.status_code != 403:
                    public.append((method, route.path, answer.status_code))
    assert public == [("GET", PATH, 200)]
    assert env.world.hits == []


async def test_the_real_app_serves_the_document_route_and_does_not_fail_on_settings_that_are_not_text():
    from nous.api.rest import create_app

    def app_with(settings: Any) -> Any:
        return create_app(
            runner=MagicMock(),
            brain=MagicMock(),
            heart=MagicMock(),
            cognitive=MagicMock(),
            database=MagicMock(),
            settings=settings,
        )

    assert (await _get(app_with(MagicMock(agent_id="a")))).status_code == 404
    settings = MagicMock(agent_id="a", a2ui_public_base_url=BASE, mcp_client_id_metadata_url=DOCUMENT)
    served = await _get(app_with(settings))
    assert served.status_code == 200 and served.json() == oauth.client_metadata_document(DOCUMENT, REDIRECT)
    assert (await _get(app_with(settings), **{PROXY_SECRET_HEADER: "wrong"})).status_code == 200
