"""Paired companion devices (``nous/api/companion_devices.py``): the store, against real Postgres."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import re
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import delete, select, update

from nous.api.companion_devices import MAX_LIVE_DEVICES, Device, DeviceStore, clean_name
from nous.storage.models import A2uiDevice

TOKEN_RE = re.compile(r"nd1_[A-Za-z0-9_-]{43}")


# ---- the name rules (no database) ---------------------------------------------------------------------------


def test_a_name_keeps_letters_digits_space_and_a_few_marks_and_nothing_else() -> None:
    assert clean_name("Pixel 7 Pro") == "Pixel 7 Pro"
    assert clean_name("  Sam's   phone (work)-2_b ") == "Sam's phone (work)-2_b"
    cyrillic = "\u041f\u0438\u043a\u0441\u0435\u043b\u044c 7"
    assert clean_name(cyrillic) == cyrillic  # letters of any script
    assert clean_name("<b>Pixel</b>") == "bPixelb"
    assert clean_name("Pixel\n7\t\x00\x07\u202e\u200b") == "Pixel 7"  # control, format and zero-width dropped
    assert clean_name("Pixel \U0001f600 7") == "Pixel 7"  # emoji dropped
    assert clean_name("x" * 80) == "x" * 60
    for empty in ("", "   ", "<>&", "\U0001f600", None, 5, b"Pixel", ["Pixel"]):
        assert clean_name(empty) is None, empty


@pytest.mark.parametrize(
    "raw",
    [
        "evil.com",
        "t.me/somebody",
        "https://example.org/x",
        "mail@example.org",
        "@somebody",
        "#tag",
        "$CASH",
        "/start",
        "[link](http://x.y)",
        "<a href='x'>y</a>",
        "+1 555 0100",
        "1.2.3.4",
        "\uff45\uff56\uff49\uff4c\uff0e\uff43\uff4f\uff4d",
        "evil\u3002com",
        "Call 5550100123",
        "15550100123",
    ],
)
def test_a_name_cannot_form_a_link_a_mention_or_a_command_in_a_telegram_line(raw: str) -> None:
    """A Telegram client links a bare domain (``evil.com``), an address, a mention, a hashtag, a cashtag, a command
    and a phone number in plain text. None of the characters they need survives, and no run of digits is longer than
    six."""
    name = clean_name(raw) or ""
    for mark in '.:/@#$+<>&[]*`~|\\=%?!;,"':
        assert mark not in name, (raw, name)
    assert all(char.isalnum() or char in " _'()-" for char in name), (raw, name)
    assert not re.search(r"\d{7}", name), (raw, name)


def test_a_run_of_digits_is_cut_at_six() -> None:
    assert clean_name("Phone 5550100123") == "Phone 555010"
    assert clean_name("123456") == "123456"
    assert clean_name("1234567") == "123456"
    assert clean_name("12 34567890 1") == "12 345678 1"  # each run on its own
    assert clean_name("555\x00\x070100123") == "555010"  # a dropped character does not split a run
    arabic_indic = "\u0661\u0662\u0663\u0664\u0665\u0666\u0667\u0668"
    assert clean_name(arabic_indic) == arabic_indic[:6]  # digits of any script
    assert clean_name("Pixel 7 Pro (2024)") == "Pixel 7 Pro (2024)"


# ---- the store (real Postgres) ------------------------------------------------------------------------------

pg = pytest.mark.postgres_only


@pytest.fixture
def agent_id() -> str:
    return f"test-devices-{uuid.uuid4().hex[:12]}"


@pytest_asyncio.fixture
async def store(db, agent_id: str):
    store = DeviceStore(db, agent_id)
    yield store
    async with db.session() as session:
        await session.execute(delete(A2uiDevice).where(A2uiDevice.agent_id == agent_id))
        await session.commit()


async def _row(db, agent_id: str, device_id: uuid.UUID) -> A2uiDevice:
    async with db.session() as session:
        row = (
            await session.execute(select(A2uiDevice).where(A2uiDevice.agent_id == agent_id, A2uiDevice.id == device_id))
        ).scalar_one()
        session.expunge(row)
        return row


class _NoDatabase:
    """A database that fails the test if a session is opened."""

    def session(self):
        raise AssertionError("the store opened a session")


@pg
async def test_create_returns_a_token_once_and_the_token_verifies(store: DeviceStore, db, agent_id: str) -> None:
    device, token = await store.create("Pixel 7", "inst-1")
    assert TOKEN_RE.fullmatch(token), token
    assert len(token) == 47
    assert isinstance(device, Device) and device
    assert (device.name, device.installation_id) == ("Pixel 7", "inst-1")
    assert await store.verify(token) == device
    other, other_token = await store.create("Pixel 8", "inst-2")
    assert other_token != token and other.id != device.id


@pg
async def test_the_token_is_not_recoverable_from_the_database(store: DeviceStore, db, agent_id: str) -> None:
    device, token = await store.create("Pixel 7", "inst-1")
    row = await _row(db, agent_id, device.id)
    digest = hashlib.sha256(token.encode()).digest()
    assert row.token_sha256 == digest.hex()
    forms = {
        token,
        token[4:],
        digest.decode("latin-1"),
        base64.b64encode(digest).decode(),
        base64.urlsafe_b64encode(digest).decode().rstrip("="),
    }
    for column in A2uiDevice.__table__.columns:
        value = str(getattr(row, column.key))
        for form in forms:
            assert form not in value, column.key


@pg
async def test_a_wrong_or_malformed_token_does_not_verify(store: DeviceStore) -> None:
    _device, token = await store.create("Pixel 7", "inst-1")
    flipped = token[:-1] + ("A" if token[-1] != "A" else "B")
    assert await store.verify(flipped) is None
    no_lookup = DeviceStore(_NoDatabase(), "anyone")
    for value in (None, "", "K7QM-2XRD", "K7QM2XRD", token + "x", token[:-1], "nd2_" + token[4:], " " + token, 5):
        assert await no_lookup.verify(value) is None, value


@pg
async def test_another_agents_token_does_not_verify(db, store: DeviceStore) -> None:
    other = DeviceStore(db, f"test-devices-{uuid.uuid4().hex[:12]}")
    try:
        _device, token = await other.create("Pixel 7", "inst-1")
        assert await store.verify(token) is None
        assert await other.verify(token) is not None
    finally:
        async with db.session() as session:
            await session.execute(delete(A2uiDevice).where(A2uiDevice.agent_id == other._agent_id))
            await session.commit()


@pg
async def test_revoke_is_once_and_takes_effect_on_the_next_verify(store: DeviceStore, db, agent_id: str) -> None:
    device, token = await store.create("Pixel 7", "inst-1")
    seen: list[Device] = []

    async def hook(revoked: Device) -> None:
        seen.append(revoked)

    store.revoke_hooks.append(hook)
    assert await store.revoke(device.id) is True
    assert await store.verify(token) is None
    assert await store.revoke(str(device.id)) is False
    assert await store.revoke(uuid.uuid4()) is None
    assert await store.revoke("not-a-uuid") is None
    assert seen == [device]  # the hooks run once, for the change
    assert (await _row(db, agent_id, device.id)).revoked_at is not None


@pg
async def test_a_failing_hook_does_not_undo_the_revoke_and_logs_its_class_only(
    store: DeviceStore, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    device, token = await store.create("Pixel 7", "inst-1")
    ran: list[str] = []

    async def failing(revoked: Device) -> None:
        raise RuntimeError(f"boom {token}")

    async def after(revoked: Device) -> None:
        ran.append("after")

    store.revoke_hooks.extend([failing, after])
    assert await store.revoke(device.id) is True
    assert ran == ["after"]
    assert await store.verify(token) is None
    assert "RuntimeError" in caplog.text
    for record in caplog.records:
        assert token not in record.getMessage() + repr(record.args)
        assert record.exc_info is None


@pg
async def test_pairing_the_same_installation_supersedes_the_old_device(store: DeviceStore, db, agent_id: str) -> None:
    seen: list[Device] = []

    async def hook(revoked: Device) -> None:
        seen.append(revoked)

    store.revoke_hooks.append(hook)
    old, old_token = await store.create("Pixel 7", "inst-1")
    new, new_token = await store.create("Pixel 7 again", "inst-1")
    assert await store.verify(old_token) is None
    assert await store.verify(new_token) == new
    assert (await _row(db, agent_id, old.id)).revoked_at is not None
    assert seen == [old]
    assert await store.installation_bound("inst-1") is True  # the new device holds it
    assert await store.live_count() == 1


@pg
async def test_last_seen_is_written_once_a_minute(store: DeviceStore, db, agent_id: str) -> None:
    device, token = await store.create("Pixel 7", "inst-1")
    assert (await _row(db, agent_id, device.id)).last_seen_at is None
    await store.verify(token)
    first = (await _row(db, agent_id, device.id)).last_seen_at
    assert first is not None
    await asyncio.sleep(0.01)
    await store.verify(token)
    assert (await _row(db, agent_id, device.id)).last_seen_at == first  # not again within the minute
    async with db.session() as session:
        await session.execute(
            update(A2uiDevice)
            .where(A2uiDevice.agent_id == agent_id, A2uiDevice.id == device.id)
            .values(last_seen_at=datetime.now(UTC) - timedelta(minutes=2))
        )
        await session.commit()
    await store.verify(token)
    assert (await _row(db, agent_id, device.id)).last_seen_at > first


@pg
async def test_the_list_has_live_devices_and_the_twenty_latest_revoked_and_no_secret(
    store: DeviceStore, db, agent_id: str
) -> None:
    live, live_token = await store.create("Live one", "inst-live")
    revoked: list[Device] = []
    for n in range(22):
        device, _ = await store.create(f"Old {n}", f"inst-{n}")
        await store.revoke(device.id)
        revoked.append(device)
    listed = await store.list_devices()
    assert [entry["name"] for entry in listed if entry["revoked_at"] is None] == ["Live one"]
    gone = [entry for entry in listed if entry["revoked_at"] is not None]
    assert len(gone) == 20
    assert {entry["name"] for entry in gone} == {d.name for d in revoked[2:]}
    for entry in listed:
        assert set(entry) == {"id", "name", "created_at", "last_seen_at", "revoked_at"}
    text = repr(listed)
    assert live_token not in text and hashlib.sha256(live_token.encode()).hexdigest() not in text
    assert "inst-" not in text


@pg
async def test_live_count_counts_live_devices_only(store: DeviceStore) -> None:
    assert MAX_LIVE_DEVICES == 10
    assert await store.live_count() == 0
    first, _ = await store.create("A", "inst-a")
    await store.create("B", "inst-b")
    assert await store.live_count() == 2
    await store.revoke(first.id)
    assert await store.live_count() == 1
    assert await store.installation_bound("inst-a") is False
