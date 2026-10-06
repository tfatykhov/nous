"""F099 Phase 2b: main.py's wiring — the rollback call, the bus, the delivery's intention store."""

from __future__ import annotations

import inspect
import logging
from types import SimpleNamespace

import pytest

import nous.main as main
from nous.brain import continuation
from nous.config import Settings


async def test_a_failed_rollback_never_blocks_startup(monkeypatch, caplog):
    async def boom(*args, **kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(continuation, "rollback_at_startup", boom)
    with caplog.at_level(logging.WARNING, logger="nous.main"):
        await main._rollback_continuation(Settings(_env_file=None), object())
    assert "retried at the next start" in caplog.text


async def test_the_rollback_gets_a_push_only_when_telegram_is_configured(monkeypatch):
    seen = []

    async def spy(database, settings, *, telegram_push):
        seen.append(telegram_push)
        return continuation.RollbackReport(0, 0, 0, 0)

    monkeypatch.setattr(continuation, "rollback_at_startup", spy)
    await main._rollback_continuation(Settings(_env_file=None, telegram_bot_token="", telegram_chat_id=""), object())
    await main._rollback_continuation(
        Settings(_env_file=None, telegram_bot_token="test-token", telegram_chat_id="4242"), object()
    )
    assert seen[0] is None and callable(seen[1])


class _FakeClient:
    status = 200
    boom = False
    posted: list = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None, timeout=None):
        if self.boom:
            raise ConnectionError("down")
        self.posted.append((url, json))
        return SimpleNamespace(status_code=self.status)


@pytest.mark.parametrize(("status", "boom", "expected"), [(200, False, True), (500, False, False), (200, True, False)])
async def test_the_raw_telegram_push(monkeypatch, status, boom, expected):
    client = type("C", (_FakeClient,), {"status": status, "boom": boom, "posted": []})
    monkeypatch.setattr(main.httpx, "AsyncClient", client)
    push = main._telegram_text_push(Settings(_env_file=None, telegram_bot_token="test-token", telegram_chat_id="4242"))
    assert await push("x" * 5000) is expected
    if not boom:
        ((url, payload),) = client.posted
        assert url == "https://api.telegram.org/bottest-token/sendMessage"
        assert payload["chat_id"] == "4242" and len(payload["text"]) == 3900


def test_create_components_wires_the_pieces_in_order():
    """PIN (by source): the gate before the first component, the rollback after the migrations and
    before the heart, the bus on the inbox store, the intention store on the DAG delivery."""
    src = inspect.getsource(main.create_components)
    order = [
        "_gate_continuation_flag(settings)",
        "Database(settings",
        "await run_migrations(database.engine)",
        "await _rollback_continuation(settings, database)",
        "heart = Heart(",
    ]
    assert [src.index(s) for s in order] == sorted(src.index(s) for s in order)
    assert "heart.result_inbox.set_bus(bus)" in src
    assert "intentions=heart.intentions" in src
