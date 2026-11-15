"""Goals PR 0c: /background/halt, /resume and /state, the load at start, and chat while halted."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import logging
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from f099_support import env_factory, runner_env, say, use  # noqa: F401
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from starlette.applications import Starlette

from nous import background_halt, main
from nous.api import background_routes, mcp_client_routes
from nous.api.background_routes import build_background_routes
from nous.background_halt import BackgroundHalt, HaltRow
from nous.background_halt_store import BackgroundHaltStore
from nous.handlers.halt_sweep import HaltReport, HaltSweeper
from nous.storage.models import Subtask

OWNER, PROXY = "owner-secret-for-tests", "proxy-secret-for-tests"
JSON = {"Content-Type": "application/json"}


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


class _Store:
    def __init__(self, row: HaltRow | None = None) -> None:
        self.row = row
        self.audits: list = []

    async def read(self):
        return self.row

    async def write(self, row, audit):
        self.row = row
        self.audits.append(audit)

    async def audit(self, entry):
        self.audits.append(entry)


def _settings(**over):
    values = {
        "owner_route_secret_sha256": _sha(OWNER),
        "mcp_client_ui_secret_sha256": _sha(PROXY),
        "telegram_bot_token": "",
        "telegram_chat_id": "",
    }
    return SimpleNamespace(**{**values, **over})


def _client(settings, *, sweeper=None, continuation_runner=None) -> AsyncClient:
    app = Starlette(
        routes=build_background_routes(settings=settings, sweeper=sweeper, continuation_runner=continuation_runner)
    )
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _install(*, host_hold: bool = False, store: _Store | None = None) -> BackgroundHalt:
    halt = BackgroundHalt(host_hold=host_hold, store=store or _Store())
    background_halt.install(halt)
    return halt


def _sweeper(report: HaltReport | None = None):
    sweeper = MagicMock()
    sweeper.run = AsyncMock(return_value=report or HaltReport(subtasks=2, subtasks_running=1))
    return sweeper


async def test_halt_needs_no_secret_and_requires_json():
    halt = _install()
    sweeper = _sweeper()
    async with _client(_settings(), sweeper=sweeper) as c:
        refused = await c.post("/background/halt", content=b"{}", headers={"Content-Type": "text/plain"})
        assert refused.status_code == 415 and not halt.halted
        sneaky = await c.post(
            "/background/halt", content=b"{}", headers={"Content-Type": "text/plain; application/json"}
        )
        assert sneaky.status_code == 415 and not halt.halted
        ok = await c.post("/background/halt", json={"actor": "dashboard", "surface": "dashboard"})
    assert ok.status_code == 200 and halt.halted
    body = ok.json()
    assert body["halted"] is True and body["already"] is False and body["verified"] is False and body["persisted"]
    assert body["report"]["cancelled"]["subtasks"] == 2
    assert body["text"].startswith("Background work is halted. Cancelled: 2 subtasks (1 was running).")
    sweeper.run.assert_awaited_once()


async def test_a_halt_with_the_owner_secret_is_verified_and_a_repeat_sweeps_again():
    halt = _install()
    sweeper = _sweeper()
    async with _client(_settings(), sweeper=sweeper) as c:
        first = await c.post("/background/halt", json={"surface": "telegram"}, headers={"X-Nous-Owner-Secret": OWNER})
        again = await c.post("/background/halt", json={"surface": "telegram"})
    assert first.json()["verified"] is True and again.json()["already"] is True
    assert sweeper.run.await_count == 2 and halt.snapshot()["verified"] is True


async def test_resume_without_the_secret_is_403_and_audited():
    store = _Store()
    halt = _install(store=store)
    await halt.halt(actor="t", surface="dashboard")
    async with _client(_settings()) as c:
        none = await c.post("/background/resume", json={"actor": "x"})
        wrong = await c.post("/background/resume", json={}, headers={"X-Nous-Owner-Secret": "nope"})
        plain = await c.post(
            "/background/resume", content=b"{}", headers={"Content-Type": "text/plain", "X-Nous-Proxy-Secret": PROXY}
        )
    assert (none.status_code, none.json()["reason"]) == (403, "no_secret")
    assert wrong.status_code == 403 and plain.status_code == 415 and halt.halted
    assert [a.action for a in store.audits].count("resume_refused") == 2


@pytest.mark.parametrize("header", [("X-Nous-Owner-Secret", OWNER), ("X-Nous-Proxy-Secret", PROXY)])
async def test_resume_with_the_owner_or_the_proxy_secret_is_200(header):
    halt = _install()
    await halt.halt(actor="t", surface="dashboard")
    cont = MagicMock()
    async with _client(_settings(), continuation_runner=cont) as c:
        ok = await c.post(
            "/background/resume", json={"actor": "dashboard", "surface": "dashboard"}, headers=dict([header])
        )
        again = await c.post("/background/resume", json={}, headers=dict([header]))
    assert ok.status_code == 200 and ok.json() == {
        "halted": False,
        "changed": True,
        "text": background_routes.RESUMED_TEXT,
    }
    assert not halt.halted and again.json()["changed"] is False
    cont.wake.assert_called_once()


def test_one_digest_per_secret():
    """The proxy's secret is checked against the MCP client UI digest, the one setting for it."""
    from nous.config import Settings

    assert "proxy_secret_sha256" not in Settings.model_fields
    assert {"owner_route_secret_sha256", "mcp_client_ui_secret_sha256"} <= set(Settings.model_fields)


async def test_neither_digest_configured_refuses_every_resume_with_the_plain_reason(caplog):
    halt = _install()
    await halt.halt(actor="t", surface="dashboard")
    with caplog.at_level(logging.WARNING, logger="nous.api.background_routes"):
        client = _client(_settings(owner_route_secret_sha256="", mcp_client_ui_secret_sha256="not-a-digest"))
    assert sum("refuses every call" in r.message for r in caplog.records) == 1
    async with client as c:
        refused = await c.post("/background/resume", json={}, headers={"X-Nous-Owner-Secret": OWNER})
    assert refused.status_code == 403 and refused.json()["reason"] == "not_configured"
    assert refused.json()["text"] == background_routes.NOT_CONFIGURED_TEXT and halt.halted


async def test_state_reports_source_since_and_persisted_and_never_a_secret():
    halt = _install()
    async with _client(_settings()) as c:
        before = await c.get("/background/state")
        await c.post(
            "/background/halt",
            json={"actor": "telegram:7", "surface": "telegram", "reason": "x\ny"},
            headers={"X-Nous-Owner-Secret": OWNER},
        )
        after = await c.get("/background/state")
    assert before.json() == {
        "halted": False,
        "source": "none",
        "since": None,
        "by": None,
        "surface": None,
        "verified": False,
        "reason": None,
        "persisted": True,
    }
    body = after.json()
    assert (body["halted"], body["source"], body["by"], body["surface"], body["reason"]) == (
        True,
        "stored",
        "telegram:7",
        "telegram",
        "x y",
    )
    assert body["since"] and body["persisted"] is True and after.headers["cache-control"] == "no-store"
    assert OWNER not in after.text and PROXY not in after.text and halt.halted


async def test_host_setting():
    store = _Store(HaltRow(halted=False))
    halt = BackgroundHalt(SimpleNamespace(background_halt=True), store=store)
    await halt.load()
    background_halt.install(halt)
    async with _client(_settings()) as c:
        state = await c.get("/background/state")
        refused = await c.post("/background/resume", json={}, headers={"X-Nous-Owner-Secret": OWNER})
    assert state.json()["source"] == "host" and state.json()["halted"] is True
    assert (refused.status_code, refused.json()) == (409, {"host_hold": True})
    rebuilt = BackgroundHalt(SimpleNamespace(background_halt=True), store=store)  # whatever the row says
    await rebuilt.load()
    assert rebuilt.halted and rebuilt.source == "host"
    assert not BackgroundHalt(SimpleNamespace(background_halt=False), store=store).halted


async def test_a_resume_raced_by_a_halt_is_409():
    halt = _install()
    await halt.halt(actor="t", surface="dashboard")

    arriving: list[asyncio.Task] = []

    async def halt_during_the_sweep():  # another request's halt: its own task, waiting for the lock
        arriving.append(asyncio.create_task(halt.halt(actor="t", surface="agent")))
        await asyncio.sleep(0)  # its flag flips at once, before it waits

    halt.set_resume_sweep(halt_during_the_sweep)
    async with _client(_settings()) as c:
        raced = await c.post("/background/resume", json={}, headers={"X-Nous-Owner-Secret": OWNER})
    await asyncio.gather(*arriving)
    assert (raced.status_code, raced.json()) == (409, {"raced": True}) and halt.halted and halt.persisted


async def test_resume_works_at_once_after_a_halt():
    halt = _install()
    e0 = halt.epoch
    async with _client(_settings(), sweeper=_sweeper()) as c:
        await c.post("/background/halt", json={})
        resumed = await c.post("/background/resume", json={}, headers={"X-Nous-Owner-Secret": OWNER})
    assert resumed.status_code == 200 and not halt.halted  # no settle wait
    assert halt.refuses("subtask", None, e0)  # a turn that started before is still refused
    assert not halt.refuses("subtask", None, halt.epoch)  # a new one runs


async def test_agent_or_host_halts_push_one_notice_and_dashboard_or_telegram_halts_do_not(monkeypatch):
    sent: list[str] = []

    async def send(settings, text, **kwargs):
        sent.append(text)
        return True

    monkeypatch.setattr(background_routes.owner_notice, "send_owner_text", send)
    for surface, pushed in (("dashboard", 0), ("telegram", 0), ("agent", 1), ("host", 1)):
        sent.clear()
        _install()
        async with _client(_settings(), sweeper=_sweeper()) as c:
            answer = await c.post("/background/halt", json={"surface": surface})
        assert len(sent) == pushed, surface
        if pushed:
            assert sent == [answer.json()["text"]]


def test_the_proxy_header_name_matches_the_mcp_routes():
    assert background_routes.PROXY_SECRET_HEADER == mcp_client_routes.PROXY_SECRET_HEADER


def test_main_installs_the_halt_before_any_loop_and_sweeps_before_the_continuation_runner_starts():
    source = inspect.getsource(main.create_components)
    install = source.index("background_halt.install(halt)")
    assert source.index("await run_migrations(database.engine)") < install
    for later in ("subtask_pool = SubtaskWorkerPool(", "HeartbeatRunner(", "TaskScheduler("):
        if later in source:
            assert install < source.index(later), later
    sweep = source.index('await halt_sweeper.run(actor="startup")')
    assert sweep < source.index("await continuation_runner.start()")
    assert "halt.set_resume_sweep(halt_sweeper.cancel_new_work)" in source


# ---- with the real store and heart ---------------------------------------------------------------------------


@pytest.mark.postgres_only
async def test_restart_while_halted_and_resume_does_not_restart_cancelled_work(env_factory):  # noqa: F811
    env = await env_factory(subtask_poll_interval=0.01)
    store = BackgroundHaltStore(env.db, env.agent)
    first = BackgroundHalt(env.settings, store=store)
    background_halt.install(first)
    await env.heart.subtasks.create(task="was running")
    await env.heart.subtasks.dequeue("worker-0")
    await first.halt(actor="t", surface="dashboard")
    sweeper = HaltSweeper(settings=env.settings, heart=env.heart, halt=first)
    report = await sweeper.run(actor="t")
    assert report.subtasks_running == 1
    leftover = Subtask(agent_id=env.agent, task="left pending across the restart", status="pending")
    async with env.db.session() as s:
        s.add(leftover)
        await s.commit()

    # The restart: a new halt object on the same database comes up halted, and the startup sweep runs.
    restarted = BackgroundHalt(env.settings, store=store)
    await restarted.load()
    background_halt.install(restarted)
    assert restarted.halted and restarted.source == "stored"
    startup = HaltSweeper(settings=env.settings, heart=env.heart, halt=restarted)
    restarted.set_resume_sweep(startup.cancel_new_work)
    await startup.run(actor="startup")
    assert (await restarted.resume(actor="owner", via="dashboard")).changed
    async with env.db.session() as s:
        rows = {
            r.task: r.status for r in (await s.execute(select(Subtask).where(Subtask.agent_id == env.agent))).scalars()
        }
    assert rows == {"was running": "cancelled", "left pending across the restart": "cancelled"}  # nothing restarted


async def test_interactive_chat_still_works_while_halted(runner_env):  # noqa: F811
    from nous.api.rest import create_app

    env = await runner_env(
        [use("web_search", query="snow"), use("spawn_task", task="check the snow again later")],
        [say("Snow is 40 cm.")],
    )
    searched: list = []

    async def web_search(**kwargs):
        searched.append(kwargs)
        return {"content": [{"type": "text", "text": "40 cm"}]}

    env.dispatcher.register(
        "web_search", web_search, {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}
    )
    halt = _install()
    await halt.halt(actor="t", surface="dashboard")
    app = create_app(env.runner, env.brain, env.heart, env.cognitive, env.db, env.settings)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        answer = await c.post("/chat", json={"message": "how much snow?", "session_id": f"s-{uuid.uuid4().hex[:6]}"})
    assert answer.status_code == 200
    assert answer.json()["response"] == "Snow is 40 cm.\n\n" + background_halt.HALT_LINE
    assert searched == [{"query": "snow"}]  # a read tool call dispatches
    results = [
        block
        for message in env.model.calls[1]["messages"]
        if isinstance(message.get("content"), list)
        for block in message["content"]
        if isinstance(block, dict) and block.get("type") == "tool_result"
    ]
    assert any(background_halt.HALTED_SPAWN_TEXT in str(block.get("content")) for block in results)
    assert await env.heart.subtasks.cancel_all_active() == []  # nothing was spawned
