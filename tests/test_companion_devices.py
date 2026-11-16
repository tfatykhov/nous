"""Paired companion devices (``nous/api/companion_devices.py``): the name rules, the store, the pairing desk and the
device routes. The store and the routes run against real Postgres."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import re
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import delete, select, update
from starlette.applications import Starlette

from nous.api import companion_devices
from nous.api.companion_devices import (
    CODE_ALPHABET,
    CODE_TTL_SECONDS,
    DEVICE_EVENT,
    MAX_LIVE_DEVICES,
    NOT_VALID,
    Device,
    DeviceStore,
    PairingDesk,
    build_device_routes,
    clean_name,
)
from nous.api.owner_guard import OwnerGuard
from nous.storage.models import A2uiDevice

TOKEN_RE = re.compile(r"nd1_[A-Za-z0-9_-]{43}")
OWNER_VALUE = "owner-only-test-value"
PROXY_VALUE = "proxy-only-test-value"
OWNER = {"X-Nous-Owner-Secret": OWNER_VALUE}
PROXY = {"X-Nous-Proxy-Secret": PROXY_VALUE, "content-type": "application/json"}
BOT_TOKEN = "bot-token-test-value"


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


# ---- the pairing desk (no database) -----------------------------------------------------------------------------


class _Clock:
    def __init__(self) -> None:
        self.now = 5000.0

    def __call__(self) -> float:
        return self.now


def _bare(code: str) -> str:
    return code.replace("-", "")


def _wrong(code: str, n: int = 0) -> str:
    """A well-formed code that is not ``code``."""
    candidates = [c for c in CODE_ALPHABET if c != _bare(code)[0]]
    return candidates[n % len(candidates)] + _bare(code)[1:]


def test_a_code_is_eight_symbols_of_the_alphabet_shown_in_two_groups() -> None:
    desk = PairingDesk()
    codes = {desk.open(None) for _ in range(50)}
    assert len(codes) == 50
    for code in codes:
        assert re.fullmatch(r"[23456789ABCDEFGHJKMNPQRSTVWXYZ]{4}-[23456789ABCDEFGHJKMNPQRSTVWXYZ]{4}", code), code
    assert len(set(CODE_ALPHABET)) == 30 and not set("01ILOU") & set(CODE_ALPHABET)


def test_a_code_is_normalised_before_it_is_compared() -> None:
    for form in (lambda c: c, _bare, str.lower, lambda c: " ".join(_bare(c)), lambda c: f"  {c.lower()}\n"):
        desk = PairingDesk()
        code = desk.open("Desk phone")
        assert desk.take(form(code)) == (True, "Desk phone"), form(code)


def test_the_desk_holds_the_code_only_as_a_digest() -> None:
    desk = PairingDesk()
    code = desk.open(None)
    held = repr(vars(desk)) + repr(vars(desk._pairing))
    assert code not in held and _bare(code) not in held
    assert desk._pairing is not None and desk._pairing.digest == hashlib.sha256(_bare(code).encode()).digest()


def test_a_restart_forgets_a_live_code() -> None:
    code = PairingDesk().open(None)
    fresh = PairingDesk()  # a new process
    assert fresh.take(code) == (False, None)
    assert fresh.status() is None


# ---- the routes (real Postgres) ---------------------------------------------------------------------------------


class _Bus:
    def __init__(self, fail: bool = False) -> None:
        self.events: list[Any] = []
        self.fail = fail

    async def emit(self, event: Any) -> None:
        if self.fail:
            raise RuntimeError("the bus is down")
        self.events.append(event)


class _Telegram:
    """A Telegram stand-in behind an httpx transport: records each request, or fails it."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.fail = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.fail:
            raise httpx.ConnectError("unreachable")
        return httpx.Response(200, json={"ok": True})


class _Rig(SimpleNamespace):
    app: Starlette
    store: DeviceStore
    desk: PairingDesk
    clock: _Clock
    bus: _Bus
    telegram: _Telegram


@pytest_asyncio.fixture
async def make_rig(db):
    """Build a device app: the five routes over a real store, a desk on a test clock, a recording bus and Telegram."""
    built: list[tuple[str, httpx.AsyncClient]] = []

    def make(*, base_url: str = "", store: Any = None, bus: _Bus | None = None) -> _Rig:
        agent_id = f"test-devices-{uuid.uuid4().hex[:12]}"
        settings = SimpleNamespace(
            agent_id=agent_id,
            owner_route_secret_sha256=hashlib.sha256(OWNER_VALUE.encode()).hexdigest(),
            mcp_client_ui_secret_sha256=hashlib.sha256(PROXY_VALUE.encode()).hexdigest(),
            a2ui_public_base_url=base_url,
            telegram_bot_token=BOT_TOKEN,
            telegram_chat_id="42",
        )
        real = DeviceStore(db, agent_id)
        clock, telegram = _Clock(), _Telegram()
        http = httpx.AsyncClient(transport=httpx.MockTransport(telegram))
        built.append((agent_id, http))
        desk = PairingDesk(clock=clock)
        bus = bus or _Bus()
        guard = OwnerGuard(settings, devices=store or real)
        routes = build_device_routes(store=store or real, desk=desk, guard=guard, settings=settings, bus=bus, http=http)
        return _Rig(
            app=Starlette(routes=routes),
            store=real,
            desk=desk,
            clock=clock,
            bus=bus,
            telegram=telegram,
            agent_id=agent_id,
        )

    yield make
    for agent_id, http in built:
        await http.aclose()
        async with db.session() as session:
            await session.execute(delete(A2uiDevice).where(A2uiDevice.agent_id == agent_id))
            await session.commit()


async def _call(app: Starlette, method: str, path: str, **kwargs: Any) -> httpx.Response:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://nous") as client:
        return await client.request(method, path, **kwargs)


async def _open(rig: _Rig, name: str | None = None) -> str:
    response = await _call(
        rig.app, "POST", "/a2ui/devices/pairings", json={"name": name} if name else {}, headers=OWNER
    )
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["expires_in"] == CODE_TTL_SECONDS
    return response.json()["code"]


async def _redeem(rig: _Rig, code: object, installation_id: str = "inst-1", **extra: Any) -> httpx.Response:
    rig.clock.now += 7  # at most nine redeems in any minute: the redeem limit stays out of tests that are not about it
    # Encoded here, ASCII-escaped: a code with a lone surrogate must reach the server as JSON can carry it.
    body = json.dumps({"code": code, "installation_id": installation_id, **extra}).encode()
    return await _call(
        rig.app, "POST", "/a2ui/devices/redeem", content=body, headers={"content-type": "application/json"}
    )


async def _rows(db, agent_id: str) -> list[A2uiDevice]:
    async with db.session() as session:
        rows = (await session.execute(select(A2uiDevice).where(A2uiDevice.agent_id == agent_id))).scalars().all()
        for row in rows:
            session.expunge(row)
        return list(rows)


def _failure(response: httpx.Response) -> tuple[int, str, str, bytes]:
    return (
        response.status_code,
        response.headers["content-type"],
        response.headers.get("cache-control", ""),
        response.content,
    )


@pg
async def test_redeem_succeeds_once_and_the_token_verifies(make_rig) -> None:
    rig = make_rig()
    code = await _open(rig)
    response = await _redeem(rig, code, name="Pixel 7")
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert set(body) == {"device_id", "name", "token", "public_base_url"}
    assert TOKEN_RE.fullmatch(body["token"])
    device = await rig.store.verify(body["token"])
    assert device is not None and str(device.id) == body["device_id"] and device.name == body["name"] == "Pixel 7"
    again = await _redeem(rig, code)
    assert (again.status_code, again.json()) == (404, NOT_VALID)


@pg
async def test_two_redeems_of_one_code_at_once_one_wins(make_rig, db) -> None:
    rig = make_rig()
    code = await _open(rig)
    body = {"code": code, "installation_id": "inst-a"}
    other = {"code": code, "installation_id": "inst-b"}
    first, second = await asyncio.gather(
        _call(rig.app, "POST", "/a2ui/devices/redeem", json=body),
        _call(rig.app, "POST", "/a2ui/devices/redeem", json=other),
    )
    assert sorted([first.status_code, second.status_code]) == [200, 404]
    assert len(await _rows(db, rig.agent_id)) == 1


@pg
async def test_five_wrong_codes_close_the_pairing_and_the_right_code_is_then_refused(make_rig, db) -> None:
    rig = make_rig()
    code = await _open(rig)
    misses = [await _redeem(rig, _wrong(code, n)) for n in range(5)]
    assert {_failure(r) for r in misses} == {_failure(misses[0])}
    assert misses[0].status_code == 404 and misses[0].json() == NOT_VALID
    assert rig.desk.status() == {"state": "closed_tries"}
    right = await _redeem(rig, code)
    assert _failure(right) == _failure(misses[0])
    assert await _rows(db, rig.agent_id) == []


@pg
async def test_four_wrong_codes_leave_the_pairing_open(make_rig) -> None:
    rig = make_rig()
    code = await _open(rig)
    for n in range(4):
        await _redeem(rig, _wrong(code, n))
    assert rig.desk.status()["tries_left"] == 1
    assert (await _redeem(rig, code)).status_code == 200


@pg
async def test_a_new_code_works_after_a_closed_one(make_rig) -> None:
    rig = make_rig()
    code = await _open(rig)
    for n in range(5):
        await _redeem(rig, _wrong(code, n))
    fresh = await _open(rig)
    assert (await _redeem(rig, fresh)).status_code == 200


@pg
async def test_a_malformed_code_is_not_a_miss(make_rig) -> None:
    rig = make_rig()
    code = await _open(rig)
    wrong_bytes = None
    malformed = [
        "",
        "   ",
        "ABC",
        _bare(code)[:7],
        _bare(code) + "2",
        "0000-0000",
        "IIIIIIII",
        _bare(code)[:7] + "!",
        "\ud800" * 8,
        "A" * 5000,
    ]
    for value in malformed:
        response = await _redeem(rig, value)
        assert (response.status_code, response.json()) == (404, NOT_VALID), value
        wrong_bytes = wrong_bytes or _failure(response)
        assert _failure(response) == wrong_bytes
    assert rig.desk.status()["tries_left"] == 5
    assert (await _redeem(rig, code)).status_code == 200


@pg
async def test_every_failure_has_the_same_status_and_body(make_rig) -> None:
    answers: dict[str, httpx.Response] = {}
    none_open = make_rig()
    answers["none open"] = await _redeem(none_open, "K7QM-2XRD")

    rig = make_rig()
    code = await _open(rig)
    answers["wrong"] = await _redeem(rig, _wrong(code))
    answers["malformed"] = await _redeem(rig, "K7QM-2XR")
    assert (await _redeem(rig, code)).status_code == 200
    answers["used"] = await _redeem(rig, code)

    expired = make_rig()
    old = await _open(expired)
    expired.clock.now += CODE_TTL_SECONDS
    answers["expired"] = await _redeem(expired, old)

    closed = make_rig()
    shut = await _open(closed)
    for n in range(5):
        await _redeem(closed, _wrong(shut, n))
    answers["closed"] = await _redeem(closed, shut)

    shapes = {name: _failure(response) for name, response in answers.items()}
    assert len(set(shapes.values())) == 1, shapes
    assert answers["wrong"].status_code == 404 and answers["wrong"].json() == NOT_VALID


@pg
async def test_redeem_hashes_and_compares_even_when_no_pairing_is_open(make_rig, monkeypatch) -> None:
    calls: list[int] = []
    real = companion_devices.hmac.compare_digest

    def spy(a: Any, b: Any) -> bool:
        calls.append(1)
        return real(a, b)

    monkeypatch.setattr(companion_devices.hmac, "compare_digest", spy)

    async def compares(rig: _Rig, value: str) -> int:
        calls.clear()
        await _redeem(rig, value)
        return len(calls)

    rig = make_rig()
    assert await compares(rig, "K7QM-2XRD") == 1  # none open
    code = await _open(rig)
    assert await compares(rig, "K7QM-2XR") == 1  # malformed
    assert await compares(rig, _wrong(code)) == 1  # wrong
    assert await compares(rig, code) == 1  # right
    assert await compares(rig, code) == 1  # used
    expired = make_rig()
    old = await _open(expired)
    expired.clock.now += CODE_TTL_SECONDS
    assert await compares(expired, old) == 1


@pg
async def test_the_global_limit_answers_429_before_looking_at_the_code(make_rig, db) -> None:
    rig = make_rig()
    code = await _open(rig)
    for n in range(10):
        response = await _call(
            rig.app, "POST", "/a2ui/devices/redeem", json={"code": f"bad-{n}", "installation_id": "inst-1"}
        )
        assert response.status_code == 404
    limited = await _call(rig.app, "POST", "/a2ui/devices/redeem", json={"code": code, "installation_id": "inst-1"})
    assert limited.status_code == 429
    assert limited.headers["retry-after"] == "60"
    assert code not in limited.text
    assert rig.desk.status() == {"state": "open", "expires_in": CODE_TTL_SECONDS, "tries_left": 5}  # not consumed
    rig.clock.now += 61
    assert (
        await _call(rig.app, "POST", "/a2ui/devices/redeem", json={"code": code, "installation_id": "i"})
    ).status_code == 200
    assert len(await _rows(db, rig.agent_id)) == 1


@pg
async def test_a_bad_body_is_400_and_counts_nothing(make_rig) -> None:
    rig = make_rig()
    code = await _open(rig)
    bad = [
        {"content": b"not json", "headers": {"content-type": "application/json"}},
        {"json": [code]},
        {"json": {"code": code}},
        {"json": {"installation_id": "inst-1"}},
        {"json": {"code": 12345678, "installation_id": "inst-1"}},
        {"json": {"code": code, "installation_id": ""}},
        {"json": {"code": code, "installation_id": "   "}},
        {"json": {"code": code, "installation_id": "x" * 101}},
        {"json": {"code": code, "installation_id": "inst\x00-1"}},
        {"json": {"code": code, "installation_id": 7}},
    ]
    for kwargs in bad * 2:  # more than the redeem limit: none of them takes a slot
        response = await _call(rig.app, "POST", "/a2ui/devices/redeem", **kwargs)
        assert response.status_code == 400, (kwargs, response.text)
        assert code not in response.text and _bare(code) not in response.text
        assert response.json() == {"error": "the body must be a JSON object with a string code and installation_id"}
    text = await _call(rig.app, "POST", "/a2ui/devices/redeem", content=b"{}", headers={"content-type": "text/plain"})
    assert text.status_code == 415
    assert rig.desk.limited() is False
    assert rig.desk.status()["tries_left"] == 5
    # surrounding whitespace of the installation id is stripped; the code was never consumed
    padded = await _call(rig.app, "POST", "/a2ui/devices/redeem", json={"code": code, "installation_id": " inst-1\n"})
    assert padded.status_code == 200
    assert (await rig.store.verify(padded.json()["token"])).installation_id == "inst-1"


@pg
async def test_the_owners_name_wins_over_the_devices(make_rig) -> None:
    rig = make_rig()
    for owner, reported, expected in [
        ("Desk phone", "Pixel 7", "Desk phone"),
        (None, "<b>Pixel</b> 7", "bPixelb 7"),
        (None, None, "Android"),
        (None, "\U0001f600", "Android"),
        (None, 42, "Android"),
    ]:
        code = await _open(rig, owner)
        extra = {"name": reported} if reported is not None else {}
        response = await _redeem(rig, code, installation_id=f"inst-{uuid.uuid4().hex[:6]}", **extra)
        assert response.status_code == 200, response.text
        assert response.json()["name"] == expected
        assert (await rig.store.verify(response.json()["token"])).name == expected


@pg
async def test_an_owner_name_that_cleans_to_nothing_is_refused(make_rig) -> None:
    rig = make_rig()
    for name in ("<>", "\U0001f600"):
        response = await _call(rig.app, "POST", "/a2ui/devices/pairings", json={"name": name}, headers=OWNER)
        assert response.status_code == 400, name
    for body in ({"name": ""}, {"name": "   "}, {}, None):
        kwargs: dict[str, Any] = {"json": body} if body is not None else {}
        response = await _call(rig.app, "POST", "/a2ui/devices/pairings", headers=OWNER, **kwargs)
        assert response.status_code == 200, body
    assert (await _call(rig.app, "POST", "/a2ui/devices/pairings", json={"name": 5}, headers=OWNER)).status_code == 400


@pg
async def test_redeem_returns_the_public_base_url_or_null(make_rig) -> None:
    for configured, expected in [("https://nous.example.org", "https://nous.example.org"), ("", None), ("  ", None)]:
        rig = make_rig(base_url=configured)
        response = await _redeem(rig, await _open(rig))
        assert response.json()["public_base_url"] == expected


@pg
async def test_a_new_pairing_replaces_the_open_one(make_rig) -> None:
    rig = make_rig()
    first = await _open(rig)
    second = await _open(rig)
    assert (await _redeem(rig, first)).status_code == 404
    assert (await _redeem(rig, second)).status_code == 200


@pg
async def test_at_most_ten_live_devices(make_rig) -> None:
    rig = make_rig()
    made = [await rig.store.create(f"Phone {n}", f"inst-{n}") for n in range(MAX_LIVE_DEVICES)]
    refused = await _call(rig.app, "POST", "/a2ui/devices/pairings", json={}, headers=OWNER)
    assert (refused.status_code, refused.json()) == (409, {"error": "revoke a device first"})
    await rig.store.revoke(made[0][0].id)
    await _open(rig)


@pg
async def test_redeeming_with_the_same_installation_supersedes_the_old_device(make_rig) -> None:
    rig = make_rig()
    old = (await _redeem(rig, await _open(rig), installation_id="inst-same")).json()["token"]
    new = (await _redeem(rig, await _open(rig), installation_id="inst-same")).json()["token"]
    assert await rig.store.verify(old) is None
    assert await rig.store.verify(new) is not None


@pg
async def test_pairing_cannot_be_opened_listed_or_revoked_with_a_device_token_or_no_secret(make_rig) -> None:
    rig = make_rig()
    paired = (await _redeem(rig, await _open(rig))).json()
    device = {"X-Nous-Device-Token": paired["token"], "content-type": "application/json"}
    own = f"/a2ui/devices/{paired['device_id']}"
    for method, path in [("POST", "/a2ui/devices/pairings"), ("GET", "/a2ui/devices"), ("DELETE", own)]:
        for headers in ({}, device, {"content-type": "application/json"}):
            response = await _call(rig.app, method, path, headers=headers)
            assert (response.status_code, response.json()) == (403, {"error": "forbidden"}), (method, path, headers)
        plain = await _call(rig.app, method, path, headers={**PROXY, "content-type": "text/plain"})
        expected = 200 if method == "GET" else 415
        assert plain.status_code == expected, (method, path)
    assert await rig.store.verify(paired["token"]) is not None  # its own id was not revoked


@pg
async def test_the_list_reports_the_pairing_state_and_the_redeem_limit(make_rig) -> None:
    rig = make_rig()

    async def listed() -> dict[str, Any]:
        response = await _call(rig.app, "GET", "/a2ui/devices", headers=OWNER)
        assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
        return response.json()

    assert await listed() == {"devices": [], "pairing": None, "redeem": {"limited": False}}
    code = await _open(rig, "Desk phone")
    assert (await listed())["pairing"] == {"state": "open", "expires_in": CODE_TTL_SECONDS, "tries_left": 5}
    await _redeem(rig, _wrong(code))
    assert (await listed())["pairing"]["tries_left"] == 4
    await _redeem(rig, code)
    body = await listed()
    assert body["pairing"] == {"state": "used"}
    assert [d["name"] for d in body["devices"]] == ["Desk phone"]
    assert set(body["devices"][0]) == {"id", "name", "created_at", "last_seen_at", "revoked_at"}

    shut = await _open(rig)
    for n in range(5):
        await _redeem(rig, _wrong(shut, n))
    assert (await listed())["pairing"] == {"state": "closed_tries"}

    old = await _open(rig)
    rig.clock.now += CODE_TTL_SECONDS
    assert (await listed())["pairing"] == {"state": "expired"}
    assert old not in json.dumps(await listed())

    rig.clock.now += 61
    for n in range(10):
        await _call(rig.app, "POST", "/a2ui/devices/redeem", json={"code": f"bad-{n}", "installation_id": "inst-1"})
    assert (await listed())["redeem"] == {"limited": True}
    rig.clock.now += 61
    assert (await listed())["redeem"] == {"limited": False}


@pg
async def test_the_code_is_in_no_table_and_no_response_but_the_open_call(make_rig, db) -> None:
    rig = make_rig()
    code = await _open(rig, "Desk phone")
    forms = {code, _bare(code), hashlib.sha256(_bare(code).encode()).hexdigest()}
    wrong = await _redeem(rig, _wrong(code))
    right = await _redeem(rig, code)
    listing = await _call(rig.app, "GET", "/a2ui/devices", headers=OWNER)
    revoke = await _call(rig.app, "DELETE", f"/a2ui/devices/{right.json()['device_id']}", headers=OWNER)
    for response in (wrong, right, listing, revoke):
        for form in forms:
            assert form not in response.text
    for row in await _rows(db, rig.agent_id):
        for column in A2uiDevice.__table__.columns:
            for form in forms:
                assert form not in str(getattr(row, column.key))
    for event in rig.bus.events:
        for form in forms:
            assert form not in json.dumps(event.data)


@pg
async def test_the_owner_revokes_a_device_once(make_rig) -> None:
    rig = make_rig()
    paired = (await _redeem(rig, await _open(rig))).json()
    path = f"/a2ui/devices/{paired['device_id']}"
    first = await _call(rig.app, "DELETE", path, headers=OWNER)
    assert (first.status_code, first.json()) == (200, {"revoked": True})
    assert await rig.store.verify(paired["token"]) is None
    again = await _call(rig.app, "DELETE", path, headers=PROXY)
    assert (again.status_code, again.json()) == (200, {"revoked": False})
    for unknown in (str(uuid.uuid4()), "not-a-uuid"):
        response = await _call(rig.app, "DELETE", f"/a2ui/devices/{unknown}", headers=OWNER)
        assert (response.status_code, response.json()) == (404, {"error": "no such device"})


@pg
async def test_unpair_revokes_the_calling_device_by_its_own_token_only(make_rig) -> None:
    rig = make_rig()
    paired = (await _redeem(rig, await _open(rig))).json()
    token = {"X-Nous-Device-Token": paired["token"]}
    rejected = {"error": {"code": "DEVICE_TOKEN_REJECTED", "message": companion_devices.DEVICE_TOKEN_TEXT}}

    for headers in ({}, {**OWNER, **token}, {**PROXY, **token}, OWNER, PROXY):
        response = await _call(rig.app, "POST", "/a2ui/devices/unpair", headers=headers)
        assert (response.status_code, response.json()) == (403, {"error": "forbidden"}), headers
    malformed = await _call(rig.app, "POST", "/a2ui/devices/unpair", headers={"X-Nous-Device-Token": "K7QM-2XRD"})
    assert (malformed.status_code, malformed.json()) == (403, rejected)
    assert await rig.store.verify(paired["token"]) is not None

    unpaired = await _call(rig.app, "POST", "/a2ui/devices/unpair", headers=token)
    assert (unpaired.status_code, unpaired.json()) == (200, {"revoked": True})
    assert await rig.store.verify(paired["token"]) is None
    again = await _call(rig.app, "POST", "/a2ui/devices/unpair", headers=token)
    assert (again.status_code, again.json()) == (403, rejected)


@pg
async def test_the_telegram_line_names_the_device_and_nothing_secret(make_rig) -> None:
    rig = make_rig()
    code = await _open(rig)
    response = await _redeem(rig, code, name="Pixel 7")
    token = response.json()["token"]
    assert len(rig.telegram.requests) == 1
    sent = rig.telegram.requests[0]
    assert sent.url.path == f"/bot{BOT_TOKEN}/sendMessage"
    payload = json.loads(sent.content)
    assert payload == {
        "chat_id": "42",
        "text": "Companion device paired: Pixel 7. If you did not just do this, open Devices in the dashboard and "
        "revoke it.",
    }
    for secret in (token, code, _bare(code), hashlib.sha256(token.encode()).hexdigest()):
        assert secret not in sent.content.decode()
    [event] = rig.bus.events
    assert event.type == DEVICE_EVENT and event.agent_id == rig.agent_id
    # Action and id only: the name is the caller's choice and stays out of the events table.
    assert event.data == {"action": "paired", "device_id": response.json()["device_id"]}


@pg
async def test_a_failed_telegram_send_or_event_does_not_fail_the_pairing(make_rig, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    rig = make_rig(bus=_Bus(fail=True))
    rig.telegram.fail = True
    response = await _redeem(rig, await _open(rig))
    assert response.status_code == 200
    assert await rig.store.verify(response.json()["token"]) is not None
    assert len(rig.telegram.requests) == 1
    assert "ConnectError" in caplog.text and "RuntimeError" in caplog.text
    for record in caplog.records:
        assert BOT_TOKEN not in record.getMessage() + repr(record.args)


class _FailingStore(DeviceStore):
    async def create(self, name: str, installation_id: str) -> tuple[Device, str]:
        raise RuntimeError("insert failed for nd1_" + "A" * 43)


@pg
async def test_a_store_failure_after_the_code_is_taken_burns_the_code(make_rig, db, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    probe = make_rig()
    rig = make_rig(store=_FailingStore(db, probe.agent_id))
    code = await _open(rig)
    failed = await _redeem(rig, code)
    assert (failed.status_code, failed.json()) == (500, {"error": "the request could not be processed"})
    assert "RuntimeError" in caplog.text
    for record in caplog.records:
        assert "nd1_" not in record.getMessage() + repr(record.args)
        assert record.exc_info is None
    assert (await _redeem(rig, code)).status_code == 404
    assert rig.telegram.requests == []
