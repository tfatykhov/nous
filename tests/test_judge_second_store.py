"""F118 PR 3: the second judge's key sealed and opened, and the row written by named columns.

The sealing is the MCP credentials' (``SecretBox``), with associated data whose kind no MCP row can have: a sealed key
opens for its own agent only, never as an MCP row, and an MCP row's sealed value never opens here. The database tests
write rows of their own agent and delete them.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from nous.judge import second_settings
from nous.judge.second_settings import (
    NO_KEY_FILE,
    OTHER_KEY,
    UNREADABLE,
    StoredRow,
    open_key,
    read_row,
    seal_key,
    upsert_row,
)
from nous.mcp_client.secrets import (
    SecretBox,
    StoredSecret,
    aad_fields,
    open_credential,
    seal_headers,
)

KEY = "ts-store-" + "Q4" * 12
BOX = SecretBox(b"\x11" * 32)
OTHER_BOX = SecretBox(b"\x22" * 32)
AGENT = "test-f118-store"


def _row(sealed: dict, **over) -> StoredRow:
    return StoredRow(**{**sealed, "key_set_at": datetime(2026, 11, 23, tzinfo=UTC), **over})


def test_a_sealed_key_opens_for_its_agent():
    sealed = seal_key(BOX, AGENT, KEY)
    assert set(sealed) == {"key_id", "nonce", "ciphertext"}
    assert KEY.encode() not in sealed["ciphertext"]
    assert open_key(BOX, AGENT, _row(sealed)) == (KEY, None)


def test_each_seal_has_a_new_nonce():
    first, second = seal_key(BOX, AGENT, KEY), seal_key(BOX, AGENT, KEY)
    assert first["nonce"] != second["nonce"] and first["ciphertext"] != second["ciphertext"]


def test_a_sealed_key_does_not_open_for_another_agent():
    assert open_key(BOX, f"{AGENT}-other", _row(seal_key(BOX, AGENT, KEY))) == (None, UNREADABLE)


def test_a_stored_key_and_no_key_file_or_another_key():
    sealed = _row(seal_key(BOX, AGENT, KEY))
    assert open_key(None, AGENT, sealed) == (None, NO_KEY_FILE)
    assert open_key(OTHER_BOX, AGENT, sealed) == (None, OTHER_KEY)


def test_a_damaged_stored_key_cannot_be_opened():
    sealed = seal_key(BOX, AGENT, KEY)
    cut = {**sealed, "ciphertext": sealed["ciphertext"][:-1]}
    flipped = {**sealed, "ciphertext": bytes([sealed["ciphertext"][0] ^ 1]) + sealed["ciphertext"][1:]}
    for damaged in (cut, flipped, {**sealed, "nonce": b"\x00" * 12}):
        assert open_key(BOX, AGENT, _row(damaged)) == (None, UNREADABLE)


def test_a_sealed_value_that_is_not_a_key_of_the_allowed_form_is_not_used():
    key_id, nonce, ciphertext = BOX.seal(second_settings.key_fields(AGENT), b"has a space and is long enough")
    row = _row({"key_id": key_id, "nonce": nonce, "ciphertext": ciphertext})
    assert open_key(BOX, AGENT, row) == (None, UNREADABLE)


def test_no_stored_key_is_no_key_and_no_problem():
    assert open_key(BOX, AGENT, None) == (None, None)
    assert open_key(None, AGENT, StoredRow(enabled=True)) == (None, None)


def _as_mcp_secret(sealed: dict, kind: str) -> StoredSecret:
    return StoredSecret(uuid.uuid4(), kind, sealed["key_id"], sealed["nonce"], sealed["ciphertext"], (), None)


@pytest.mark.parametrize("kind", ["headers", "oauth"])
@pytest.mark.parametrize("server", ["outside-judge:jev", "jev", ""])
def test_a_sealed_key_does_not_open_as_an_mcp_row(kind, server):
    sealed = seal_key(BOX, AGENT, KEY)
    for url in ("", "https://api.typesafe.ai/v1/systemone"):
        opened, problem = open_credential(BOX, aad_fields(AGENT, server, url, kind), _as_mcp_secret(sealed, kind))
        assert (opened, problem) == (None, "cannot be decrypted")


@pytest.mark.parametrize("server", ["outside-judge:jev", "jev", "typesafe"])
def test_an_mcp_rows_sealed_value_does_not_open_here(server):
    secret = seal_headers(BOX, aad_fields(AGENT, server, "", "headers"), {"Authorization": f"Bearer {KEY}"})
    row = _row({"key_id": secret.key_id, "nonce": secret.nonce, "ciphertext": secret.ciphertext})
    assert open_key(BOX, AGENT, row) == (None, UNREADABLE)


# ---- the row -------------------------------------------------------------------------------------------------------


@pytest.fixture
async def agent(db):
    agent_id = f"test-f118-store-{uuid.uuid4().hex[:8]}"
    yield agent_id
    async with db.session() as session:
        await session.execute(
            text("DELETE FROM nous_system.judge_second_settings WHERE agent_id = :a"), {"a": agent_id}
        )
        await session.commit()


@pytest.mark.postgres_only
async def test_a_write_names_only_its_columns_and_null_returns_a_value_to_the_server(db, agent):
    async with db.session() as session:
        assert await read_row(session, agent) is None
        first = await upsert_row(session, agent, {"enabled": True, "daily_calls": 7}, "owner")
        await session.commit()
    assert (first.enabled, first.model, first.daily_calls, first.has_key, first.updated_by) == (
        True,
        None,
        7,
        False,
        "owner",
    )
    sealed = seal_key(BOX, agent, KEY)
    set_at = datetime(2026, 11, 23, 9, 30, tzinfo=UTC)
    async with db.session() as session:
        keyed = await upsert_row(session, agent, {**sealed, "key_set_at": set_at}, "proxy")
        await session.commit()
    # The key's write kept the switch and the limit.
    assert (keyed.enabled, keyed.daily_calls, keyed.key_set_at, keyed.updated_by) == (True, 7, set_at, "proxy")
    assert open_key(BOX, agent, keyed) == (KEY, None)
    async with db.session() as session:
        reset = await upsert_row(session, agent, {"daily_calls": None, "model": "jev-1.15.0"}, "owner")
        await session.commit()
        again = await read_row(session, agent)
    assert (reset.enabled, reset.daily_calls, reset.model, reset.key_id) == (True, None, "jev-1.15.0", sealed["key_id"])
    assert again == reset
    async with db.session() as session:
        removed = await upsert_row(session, agent, dict.fromkeys(second_settings.KEY_COLUMNS), "owner")
        await session.commit()
    assert not removed.has_key and removed.nonce is None and removed.key_set_at is None
    assert (removed.enabled, removed.model) == (True, "jev-1.15.0")


@pytest.mark.postgres_only
async def test_the_key_is_in_no_column_as_text(db, agent):
    sealed = seal_key(BOX, agent, KEY)
    async with db.session() as session:
        await upsert_row(session, agent, {**sealed, "key_set_at": datetime.now(UTC)}, "owner")
        await session.commit()
        row = (
            (
                await session.execute(
                    text("SELECT * FROM nous_system.judge_second_settings WHERE agent_id = :a"), {"a": agent}
                )
            )
            .mappings()
            .one()
        )
    for name, value in row.items():
        raw = value if isinstance(value, bytes) else str(value).encode()
        assert KEY.encode() not in raw, name
        assert KEY[-8:].encode() not in raw, name


@pytest.mark.parametrize("names", [{}, {"agent_id": "x"}, {"updated_by": "owner"}, {"enabled": True, "other": 1}])
async def test_a_write_refuses_a_column_outside_the_writable_ones(names):
    with pytest.raises(ValueError):
        await upsert_row(None, AGENT, names, "owner")


def test_the_environment_holds_no_key_after_sealing_and_opening():
    before = dict(os.environ)
    open_key(BOX, AGENT, _row(seal_key(BOX, AGENT, KEY)))
    assert dict(os.environ) == before
