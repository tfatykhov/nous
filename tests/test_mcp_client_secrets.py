"""F112 MCP client PR 2c: the sealed credentials (secrets.py), the header rules, migration 110 and the registry's
credential rows (SQLite by default, Postgres in CI). Every credential-looking value here is made up."""

from __future__ import annotations

import base64
import logging
import re
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from nous.mcp_client.config import ServerConfig, ToolConfig, validate_headers
from nous.mcp_client.registry import McpRegistry
from nous.mcp_client.secrets import (
    NO_KEY,
    OTHER_KEY,
    UNREADABLE,
    KeyFile,
    SecretBox,
    SecretUnavailable,
    StoredSecret,
    aad_fields,
    open_headers,
    seal_headers,
)
from nous.storage.migrator import _split_sql_statements
from nous.storage.models import McpServerSecret

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "sql" / "migrations" / "110_mcp_client_secrets.sql"
KEY = bytes(range(32))
OTHER = bytes(range(1, 33))
VALUE = "Bearer test-key-0000000000000000"
URL = "https://alpha.example.com/mcp"
SCHEMA = {"type": "object", "properties": {"q": {"type": "string"}}}


def _stored(sealed, secret_id: uuid.UUID | None = None) -> StoredSecret:
    return StoredSecret(
        id=secret_id or uuid.uuid4(),
        kind=sealed.kind,
        key_id=sealed.key_id,
        nonce=sealed.nonce,
        ciphertext=sealed.ciphertext,
        header_names=sealed.header_names,
        updated_at=None,
    )


# --- the box ---------------------------------------------------------------------------------------------------------


def test_a_sealed_header_map_opens_with_the_same_key_and_fields():
    box = SecretBox(KEY)
    fields = aad_fields("agent", "alpha", URL, "headers")
    sealed = seal_headers(box, fields, {"Authorization": VALUE, "X-Api-Key": "test-key-1111"})
    assert sealed.key_id == box.key_id and len(sealed.key_id) == 16 and len(sealed.nonce) == 12
    assert VALUE.encode() not in sealed.ciphertext and VALUE not in repr(sealed)
    headers, problem = open_headers(box, fields, _stored(sealed))
    assert problem is None and headers == {"Authorization": VALUE, "X-Api-Key": "test-key-1111"}
    again = seal_headers(box, fields, {"Authorization": VALUE})
    assert again.nonce != sealed.nonce  # a new nonce for every write


@pytest.mark.parametrize(
    "fields",
    [
        aad_fields("agent", "beta", URL, "headers"),  # another server's name
        aad_fields("agent", "alpha", "https://alpha.example.com/other", "headers"),  # another URL
        aad_fields("agent", "alpha", URL, "oauth"),  # another kind
        aad_fields("other-agent", "alpha", URL, "headers"),  # another agent
    ],
)
def test_a_row_opened_with_other_associated_data_cannot_be_decrypted(fields):
    box = SecretBox(KEY)
    sealed = seal_headers(box, aad_fields("agent", "alpha", URL, "headers"), {"Authorization": VALUE})
    assert open_headers(box, fields, _stored(sealed)) == (None, UNREADABLE)


def test_another_key_a_cut_ciphertext_and_no_key():
    box = SecretBox(KEY)
    fields = aad_fields("agent", "alpha", URL, "headers")
    sealed = seal_headers(box, fields, {"Authorization": VALUE})
    assert open_headers(SecretBox(OTHER), fields, _stored(sealed)) == (None, OTHER_KEY)
    cut = _stored(sealed)
    cut = StoredSecret(cut.id, cut.kind, cut.key_id, cut.nonce, cut.ciphertext[:-1], cut.header_names, None)
    assert open_headers(box, fields, cut) == (None, UNREADABLE)
    assert open_headers(None, fields, _stored(sealed)) == (None, NO_KEY)
    with pytest.raises(SecretUnavailable) as raised:
        box.open(fields, sealed.key_id, sealed.nonce, sealed.ciphertext[:-1])
    assert str(raised.value) == "" and raised.value.__cause__ is None
    with pytest.raises(ValueError):
        SecretBox(KEY[:16])


# --- the key file ----------------------------------------------------------------------------------------------------


def test_the_key_file_states(tmp_path, caplog):
    path = tmp_path / "mcp-client.key"
    caplog.set_level(logging.DEBUG)
    assert KeyFile("").box() is None and not caplog.records  # not configured: silent
    keys = KeyFile(str(path))
    assert keys.box() is None and keys.box() is None  # missing: one warning, not one per use
    path.write_bytes(b"not base64 at all!")
    assert keys.box() is None
    path.write_bytes(base64.b64encode(KEY[:16]) + b"\n")
    assert keys.box() is None
    messages = [r.getMessage() for r in caplog.records]
    assert messages == [
        "MCP client: NOUS_MCP_CLIENT_KEY_FILE: the file does not exist, so stored credentials cannot be used",
        "MCP client: NOUS_MCP_CLIENT_KEY_FILE: the file does not hold base64, so stored credentials cannot be used",
        "MCP client: NOUS_MCP_CLIENT_KEY_FILE: the key is not 32 bytes, so stored credentials cannot be used",
    ]
    path.write_bytes(base64.b64encode(KEY) + b"\n")  # put in place later: picked up without a restart
    box = keys.box()
    assert box is not None and box.key_id == SecretBox(KEY).key_id
    path.write_bytes(base64.b64encode(OTHER))
    assert keys.box() is box  # read once: a loaded key stays
    assert all(base64.b64encode(KEY[:16]).decode() not in m for m in messages)


def test_an_unreadable_key_file_is_no_key(tmp_path, caplog):
    caplog.set_level(logging.WARNING)
    assert KeyFile(str(tmp_path)).box() is None  # a directory
    assert "NOUS_MCP_CLIENT_KEY_FILE" in caplog.records[0].getMessage()


# --- what may be stored ----------------------------------------------------------------------------------------------


def test_valid_headers_are_kept_as_given():
    headers = {"Authorization": VALUE, "X-API-Key": "test-key-2222", "x!#$%&'*+.^_`|~-y": "ab cd ef gh"}
    assert validate_headers(headers) == headers


# Review fix 8: HTTP's own headers, in any case.
RESERVED_NEW = ("Content-Encoding", "expect", "TRAILER", "Keep-Alive", "Proxy-Connection", "Origin", "User-Agent")


@pytest.mark.parametrize(
    "headers,message",
    [
        ([], "headers must be an object of header name to value"),
        ({}, "give 1 to 8 headers"),
        ({f"X-H{i}": "v" for i in range(9)}, "give 1 to 8 headers"),
        ({"Bad Name": "v"}, "header 1: a name is 1 to 64 letters"),
        ({"X-Ok": "test-key-dddd", "": "v"}, "header 2: a name is 1 to 64 letters"),
        ({"x" * 65: "v"}, "header 1: a name is 1 to 64 letters"),
        ({"Héader": "v"}, "header 1: a name is 1 to 64 letters"),
        ({"Host": "v"}, "header Host: the client sets this header itself"),
        ({"Mcp-Session-Id": "v"}, "header Mcp-Session-Id: the client sets this header itself"),
        ({"X-Nous-Proxy-Secret": "v"}, "header X-Nous-Proxy-Secret: the client sets this header itself"),
        ({"x-nous-anything": "v"}, "header x-nous-anything: the client sets this header itself"),
        ({"Proxy-Authorization": "v"}, "header Proxy-Authorization: the client sets this header itself"),
        ({"X-Key": "test-key-aaaa", "x-key": "test-key-bbbb"}, "header x-key: named twice"),
        *(({name: "test-key-cccc"}, f"header {name}: the client sets this header itself") for name in RESERVED_NEW),
        ({"X-Key": ""}, "header X-Key: a value is 8 to 4096 characters"),
        ({"X-Key": "abc1234"}, "header X-Key: a value is 8 to 4096 characters"),  # 7: it could never be withheld
        ({"X-Key": "v" * 4097}, "header X-Key: a value is 8 to 4096 characters"),
        ({"X-Key": 5}, "header X-Key: a value is 8 to 4096 characters"),
        ({"X-Key": "test\nkey"}, "header X-Key: a value holds printable ASCII characters only"),
        ({"X-Key": "test-kéy"}, "header X-Key: a value holds printable ASCII characters only"),
        ({"X-Key": 'test-"key"'}, "header X-Key: a value holds printable ASCII characters only"),
        ({"X-Key": "test\\key"}, "header X-Key: a value holds printable ASCII characters only"),
        ({"X-Key": " test-key"}, "header X-Key: a value may not start or end with a space"),
        ({"X-Key": "test-key "}, "header X-Key: a value may not start or end with a space"),
        ({"X-Key": "-_.~+/=!-_.~+/=!"}, "header X-Key: a value holds at least 8 letters or digits"),
        ({"X-Key": "--ab--cd--ef--g--"}, "header X-Key: a value holds at least 8 letters or digits"),  # 7 in all
    ],
)
def test_headers_the_rules_refuse_name_the_header_and_the_rule_never_the_value(headers, message):
    with pytest.raises(ValueError) as refused:
        validate_headers(headers)
    assert str(refused.value).startswith(message)
    if isinstance(headers, dict):
        for value in headers.values():
            if isinstance(value, str) and len(value) > 1:
                assert value.strip() not in str(refused.value)


# --- migration 110 ---------------------------------------------------------------------------------------------------


def _sql() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def _code() -> str:
    return "\n".join(line for line in _sql().splitlines() if not line.lstrip().startswith("--"))


def test_the_migration_is_one_create_table():
    (table,) = _split_sql_statements(_sql())
    assert table.startswith("CREATE TABLE IF NOT EXISTS nous_system.mcp_server_secrets ")
    assert "REFERENCES nous_system.mcp_servers (id) ON DELETE CASCADE" in table
    assert "UNIQUE (server_id)" in table and "CHECK (kind IN ('headers', 'oauth'))" in table


def test_the_migration_creates_only_the_table():
    code = _code()
    for word in ("GRANT", "REVOKE", "OWNER", "ROLE", "ALTER", "DROP", "AUTHORIZATION"):
        assert not re.search(rf"\b{word}\b", code.upper()), word
    assert not re.search(r"scoring\.|brain\.|heart\.|public\.", code, re.I)


def test_the_migration_has_nothing_the_splitter_or_text_would_mangle():
    sql = _sql()
    assert "/*" not in sql and "$$" not in sql and "%" not in _code()
    assert not re.search(r"(?<!:):[A-Za-z_]", _code())
    assert all(";" not in line for line in sql.splitlines() if line.lstrip().startswith("--"))


def test_the_columns_match_the_orm_model():
    (statement,) = _split_sql_statements(_sql())
    columns = set(re.findall(r"^\s{4}([a-z_]+)\s+(?:UUID|TEXT|BYTEA|TIMESTAMPTZ)", statement, re.M))
    assert columns == {c.name for c in McpServerSecret.__table__.columns}
    assert McpServerSecret.__table__.fullname == "nous_system.mcp_server_secrets"


@pytest.mark.postgres_only
async def test_the_checks_hold_on_postgres(session):
    agent = _agent()
    server = text("INSERT INTO nous_system.mcp_servers (agent_id, name, url) VALUES (:a, 'alpha', :u) RETURNING id")
    server_id = (await session.execute(server, {"a": agent, "u": URL})).scalar_one()
    secret = text(
        "INSERT INTO nous_system.mcp_server_secrets (agent_id, server_id, kind, key_id, nonce, ciphertext) "
        "VALUES (:a, :s, :k, 'k', CAST('n' AS bytea), CAST('c' AS bytea))"
    )
    await session.execute(secret, {"a": agent, "s": server_id, "k": "headers"})
    for kind in ("oauth", "basic"):  # one credential per server; an unknown kind
        with pytest.raises(IntegrityError):
            async with session.begin_nested():
                await session.execute(secret, {"a": agent, "s": server_id, "k": kind})


# --- the registry's credential rows ----------------------------------------------------------------------------------


def _agent() -> str:
    return f"mcp-secrets-{uuid.uuid4().hex[:8]}"


def _sealed(name: str = "alpha", url: str = URL, agent: str = "a", value: str = VALUE):
    return seal_headers(SecretBox(KEY), aad_fields(agent, name, url, "headers"), {"Authorization": value})


async def test_set_replace_and_remove_make_a_new_row_each_time(db):
    registry = McpRegistry(db, _agent())
    tools = [ToolConfig(server="alpha", tool="search", input_schema=SCHEMA, permission="allow")]
    await registry.replace_server(ServerConfig(name="alpha", url=URL), tools, secret=_sealed())
    first = (await registry.list_secrets())["alpha"]
    assert (first.kind, first.header_names) == ("headers", ("Authorization",))
    await registry.update_server("alpha", secret=_sealed(value="Bearer test-key-1111111111111111"))
    second = (await registry.list_secrets())["alpha"]
    assert second.id != first.id and second.ciphertext != first.ciphertext  # a new version
    await registry.replace_server(ServerConfig(name="alpha", url=URL), tools)  # no secret, same URL: kept
    assert (await registry.list_secrets())["alpha"].id == second.id
    assert await registry.update_server("alpha", remove_secret=True) is True
    assert await registry.list_secrets() == {}
    assert await registry.update_server("zeta", remove_secret=True) is False


async def test_a_new_url_deletes_the_credential_whoever_stores_it(db):
    registry = McpRegistry(db, _agent())
    await registry.replace_server(ServerConfig(name="alpha", url=URL), [], secret=_sealed())
    await registry.upsert_server(ServerConfig(name="alpha", url=URL))  # the seed's path, same URL: kept
    assert "alpha" in await registry.list_secrets()
    await registry.upsert_server(ServerConfig(name="alpha", url="https://alpha.example.com/v2"))  # the seed's path
    assert await registry.list_secrets() == {}
    await registry.replace_server(ServerConfig(name="alpha", url=URL), [], secret=_sealed())
    await registry.replace_server(ServerConfig(name="alpha", url="https://moved.example.com/mcp"), [])
    assert await registry.list_secrets() == {}


async def test_deleting_the_server_deletes_its_credential(db):
    agent = _agent()
    registry = McpRegistry(db, agent)
    await registry.replace_server(ServerConfig(name="alpha", url=URL), [], secret=_sealed())
    assert await registry.delete_server("alpha") is True
    async with db.session() as session:
        rows = (
            await session.execute(
                text("SELECT count(*) FROM nous_system.mcp_server_secrets WHERE agent_id = :a"), {"a": agent}
            )
        ).scalar_one()
    assert rows == 0


async def test_an_update_in_place_keeps_the_id_and_misses_a_replaced_row(db):
    registry = McpRegistry(db, _agent())
    await registry.replace_server(ServerConfig(name="alpha", url=URL), [], secret=_sealed())
    before = (await registry.list_secrets())["alpha"]
    assert await registry.update_secret_in_place(before.id, _sealed(value="Bearer test-key-2222222222222222"))
    after = (await registry.list_secrets())["alpha"]
    assert after.id == before.id and after.ciphertext != before.ciphertext
    await registry.update_server("alpha", secret=_sealed())  # replaced meanwhile: a new id
    assert await registry.update_secret_in_place(before.id, _sealed()) is False  # discarded, never resurrected
    await registry.update_server("alpha", remove_secret=True)
    assert await registry.update_secret_in_place(after.id, _sealed()) is False
    assert await registry.list_secrets() == {}


async def test_credential_rows_are_per_agent(db):
    a, b = McpRegistry(db, _agent()), McpRegistry(db, _agent())
    await a.replace_server(ServerConfig(name="alpha", url=URL), [], secret=_sealed())
    await b.replace_server(ServerConfig(name="alpha", url=URL), [])
    assert list(await a.list_secrets()) == ["alpha"] and await b.list_secrets() == {}
