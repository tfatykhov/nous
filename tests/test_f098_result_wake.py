"""F098 Phase B — bot-initiated wake turn (option C)."""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

from nous.config import Settings
from nous.heart.result_inbox import ResultInboxStore
from nous.heart.result_wake import WAKE_NOTE, WakeGate, will_wake

CHAN = "telegram:4242"
NOON = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)  # outside the default 23-8 quiet hours


def _agent() -> str:
    return f"f098b-{uuid.uuid4().hex[:8]}"


def _settings(**over) -> Settings:
    base = {"result_inbox_enabled": True, "result_wake_enabled": True, "telegram_chat_id": None}
    base.update(over)
    return Settings(_env_file=None, **base)


async def _row(store: ResultInboxStore, *, age_s: float = 60, now: datetime = NOON, **over) -> uuid.UUID:
    sid = over.pop("source_id", uuid.uuid4())
    kw = dict(
        source_kind="subtask", source_id=sid, msg_type="INFORM", title="F098 build", body="done",
        channel=CHAN, session_id="s1", created_at=now - timedelta(seconds=age_s),
    )
    kw.update(over)
    await store.insert(**kw)
    return sid


@pytest.fixture
def env(db):
    agent = _agent()
    s = _settings(agent_id=agent)
    return SimpleNamespace(settings=s, store=ResultInboxStore(db, agent), gate=WakeGate(db, agent, s), db=db)


def test_flag_defaults_off():
    s = Settings(_env_file=None)
    assert s.result_wake_enabled is False
    assert s.result_wake_max_per_hour == 6 and s.result_wake_debounce_seconds == 20


def test_will_wake_needs_both_flags_and_a_telegram_channel():
    assert will_wake(_settings(), CHAN)
    assert not will_wake(_settings(result_inbox_enabled=False), CHAN)
    assert not will_wake(_settings(result_wake_enabled=False), CHAN)
    assert not will_wake(_settings(), "api:tim")
    assert not will_wake(_settings(), None)


# ---------------------------------------------------------------------------
# 1. The /inbox/wake decision matrix
# ---------------------------------------------------------------------------


class TestGate:
    async def test_empty(self, env):
        d = await env.gate.decide(CHAN, now=NOON)
        assert (d.wake, d.reason) == (False, "empty")
        assert env.gate.metrics() == {"fired": 0, "suppressed": {}}

    async def test_ready(self, env):
        await _row(env.store)
        d = await env.gate.decide(CHAN, now=NOON)
        assert (d.wake, d.reason, d.count, d.titles) == (True, "ready", 1, ("F098 build",))

    async def test_quiet_hours(self, env):
        await _row(env.store, now=NOON.replace(hour=2))
        d = await env.gate.decide(CHAN, now=NOON.replace(hour=2))
        assert (d.wake, d.reason) == (False, "quiet_hours")
        assert env.gate.metrics()["suppressed"] == {"quiet_hours": 1}

    async def test_debounce(self, env):
        await _row(env.store, age_s=5)
        d = await env.gate.decide(CHAN, now=NOON)
        assert (d.wake, d.reason) == (False, "debounce")

    async def test_rate_limit_counts_distinct_wakes_per_hour(self, env):
        for i in range(6):
            sid = await _row(env.store, age_s=600)
            stamp = NOON - timedelta(minutes=50 - i)
            await _stamp(env, sid, stamp)
        await _row(env.store)
        d = await env.gate.decide(CHAN, now=NOON)
        assert (d.wake, d.reason) == (False, "rate_limited")
        assert env.gate.metrics()["suppressed"] == {"rate_limited": 1}
        # An hour after the oldest wake, a slot frees up.
        d = await env.gate.decide(CHAN, now=NOON + timedelta(minutes=11))
        assert d.wake

    async def test_other_channels_and_handled_rows_do_not_count(self, env):
        await _row(env.store, channel="telegram:999")
        await _row(env.store, channel="api:tim")
        await _row(env.store, channel=None)
        sid = await _row(env.store)
        await _stamp(env, sid, NOON - timedelta(minutes=5))  # already woken for
        await env.store.claim(channel="telegram:999", session_id=None, max_age_hours=72)  # delivered
        assert (await env.gate.decide(CHAN, now=NOON)).reason == "empty"
        assert await env.gate.pending_channels(now=NOON) == []

    async def test_rows_past_max_age_never_wake(self, env):
        await _row(env.store, age_s=73 * 3600)
        assert (await env.gate.decide(CHAN, now=NOON)).reason == "empty"

    async def test_origin_filter_for_dags(self, env):
        from nous.dag.schemas import DAGCreateRequest, DAGNodeSpec, DAGNodeType
        from nous.dag.store import DAGStore

        dags = DAGStore(env.db, env.store._agent_id, env.settings)

        async def _dag(origin):
            return await dags.create(DAGCreateRequest(
                name="d", nodes=[DAGNodeSpec(name="n", type=DAGNodeType.callback, instructions="x")],
                origin_channel=origin,
            ))

        scheduled = await _dag(None)  # routed to the default chat by result_inbox_dag_scheduled
        await _row(env.store, source_kind="dag", source_id=scheduled.id)
        assert (await env.gate.decide(CHAN, now=NOON)).reason == "empty"

        conversational = await _dag(CHAN)
        await _row(env.store, source_kind="dag", source_id=conversational.id)
        d = await env.gate.decide(CHAN, now=NOON)
        assert d.wake and d.count == 1

    async def test_pending_channels_lists_telegram_channels_only(self, env):
        await _row(env.store)
        await _row(env.store, channel="telegram:7")
        await _row(env.store, channel="api:tim")
        assert await env.gate.pending_channels(now=NOON) == [CHAN, "telegram:7"]


async def _stamp(env, source_id, when):
    from sqlalchemy import update

    from nous.storage.models import ResultInbox

    async with env.db.session() as session:
        await session.execute(
            update(ResultInbox).where(ResultInbox.source_id == source_id).values(wake_attempted_at=when)
        )
        await session.commit()


# ---------------------------------------------------------------------------
# 3. Three results within the debounce window -> one wake, injected once
# ---------------------------------------------------------------------------


async def test_three_results_batch_into_one_wake_and_inject_once(env):
    for age in (18, 12, 6):
        await _row(env.store, age_s=age)
    assert (await env.gate.decide(CHAN, now=NOON)).reason == "debounce"

    later = NOON + timedelta(seconds=20)
    begun = await env.gate.begin(CHAN, now=later)
    assert (begun.wake, begun.count) == (True, 3)
    # The stamp is the gate: a second poller (or a retry) finds nothing.
    again = await env.gate.begin(CHAN, now=later)
    assert (again.wake, again.reason) == (False, "empty")
    assert env.gate.metrics()["fired"] == 1

    # The wake turn's pre_turn claims all three through the Phase A reader.
    claimed = await env.store.claim(channel=CHAN, session_id="wake-session", max_age_hours=72)
    assert len(claimed) == 3
    assert await env.store.claim(channel=CHAN, session_id="wake-session", max_age_hours=72) == []


async def test_begin_after_a_user_turn_claimed_the_rows_runs_no_turn(env):
    await _row(env.store)
    assert (await env.gate.decide(CHAN, now=NOON)).wake
    await env.store.claim(channel=CHAN, session_id="user-turn", max_age_hours=72)
    d = await env.gate.begin(CHAN, now=NOON)
    assert (d.wake, d.reason) == (False, "empty")


# ---------------------------------------------------------------------------
# REST: GET /inbox/wake and POST /chat/stream {wake: true}
# ---------------------------------------------------------------------------


class _StreamRunner:
    def __init__(self):
        self.calls: list[tuple] = []

    async def stream_chat(self, session_id, message, **kwargs):
        from nous.api.runner import StreamEvent

        self.calls.append((session_id, message, kwargs))
        yield StreamEvent(type="text_delta", text="2 results arrived")
        yield StreamEvent(type="done", stop_reason="end_turn")


async def _client(env, runner):
    from nous.api.rest import create_app

    app = create_app(runner, MagicMock(), MagicMock(), MagicMock(), env.db, env.settings)
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


class TestRest:
    async def test_poll_disabled(self, env):
        env.settings = _settings(agent_id=env.store._agent_id, result_wake_enabled=False)
        async with await _client(env, _StreamRunner()) as c:
            resp = await c.get("/inbox/wake")
        assert resp.json() == {"enabled": False, "channels": []}

    async def test_poll_lists_ready_channels(self, env):
        await _row(env.store, now=datetime.now(UTC))
        env.settings = _settings(agent_id=env.store._agent_id, heartbeat_quiet_start=0, heartbeat_quiet_end=0)
        async with await _client(env, _StreamRunner()) as c:
            resp = await c.get("/inbox/wake")
        body = resp.json()
        assert body["enabled"] is True
        assert [(d["channel"], d["wake"], d["count"]) for d in body["channels"]] == [(CHAN, True, 1)]

    async def test_wake_stream_runs_a_result_wake_turn(self, env):
        await _row(env.store, now=datetime.now(UTC))
        env.settings = _settings(agent_id=env.store._agent_id, heartbeat_quiet_start=0, heartbeat_quiet_end=0)
        runner = _StreamRunner()
        async with await _client(env, runner) as c:
            resp = await c.post("/chat/stream", json={
                "message": "ignored", "session_id": "S1", "platform": "telegram", "chat_id": 4242, "wake": True,
            })
        assert resp.status_code == 200 and "2 results arrived" in resp.text
        ((session_id, message, kwargs),) = runner.calls
        assert (session_id, message) == ("S1", WAKE_NOTE)
        assert kwargs["wake"] is True and kwargs["channel"] == CHAN

    async def test_wake_stream_with_nothing_to_report_is_409(self, env):
        env.settings = _settings(agent_id=env.store._agent_id, heartbeat_quiet_start=0, heartbeat_quiet_end=0)
        runner = _StreamRunner()
        async with await _client(env, runner) as c:
            resp = await c.post("/chat/stream", json={
                "message": "x", "session_id": "S1", "platform": "telegram", "chat_id": 4242, "wake": True,
            })
        assert resp.status_code == 409 and resp.json()["reason"] == "empty"
        assert runner.calls == []

    async def test_normal_stream_is_unchanged(self, env):
        runner = _StreamRunner()
        async with await _client(env, runner) as c:
            resp = await c.post("/chat/stream", json={
                "message": "hi", "session_id": "S1", "platform": "telegram", "chat_id": 4242,
            })
        assert resp.status_code == 200
        assert "wake" not in runner.calls[0][2]


# ---------------------------------------------------------------------------
# 4. The wake turn runs as result_wake; side-effect tools are flagged (warn)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tool", ["send_email", "spawn_task", "dag_create", "write_file"])
def test_policy_denies_side_effects(tool):
    from nous.api.execution_context import ExecutionContext
    from nous.api.tool_policy import evaluate

    assert evaluate(ExecutionContext(kind="result_wake"), tool, {"path": "x"}) is not None
    assert evaluate(ExecutionContext(kind="result_wake"), "recall_deep", {"query": "x"}) is None


async def test_wake_turn_side_effect_is_logged_not_blocked_in_warn_mode():
    from test_runner_authorization import _one_tool_call_then_done, _run_loop, _runner

    from nous.api.execution_context import ExecutionContext

    r, d = _runner(["send_email"], tool_context_policy_mode="warn")
    events = []
    r._log_f026_decision = lambda kind, data, session_id: events.append((kind, data))
    r._call_api = _one_tool_call_then_done("send_email")
    await _run_loop(r, is_background=True, context=ExecutionContext(kind="result_wake"))
    assert [c[0] for c in d.calls] == ["send_email"]
    assert (
        "harness_context_policy_violation",
        {"tool_name": "send_email", "context_kind": "result_wake", "violation": "level:external", "mode": "warn"},
    ) in events


async def test_stream_chat_wake_runs_as_result_wake():
    from test_streaming import _make_mock_cognitive, _make_mock_settings, _make_runner

    from nous.api.runner import StreamEvent

    cognitive, _ = _make_mock_cognitive()
    runner = _make_runner(cognitive, _make_mock_settings())
    calls = {"n": 0}

    async def fake_stream(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            yield StreamEvent(type="tool_start", tool_name="web_search", tool_id="t1", block_index=1)
            yield StreamEvent(type="tool_input_delta", text='{"query": "x"}', block_index=1)
            yield StreamEvent(type="block_stop", block_index=1)
            yield StreamEvent(type="done", stop_reason="tool_use")
        else:
            yield StreamEvent(type="text_delta", text="Done")
            yield StreamEvent(type="done", stop_reason="end_turn")

    runner._call_api_stream = MagicMock(side_effect=fake_stream)
    _ = [e async for e in runner.stream_chat("s1", WAKE_NOTE, channel=CHAN, wake=True)]
    ctx = runner._dispatcher.dispatch.call_args.kwargs["context"]
    assert (ctx.kind, ctx.channel, ctx.session_id) == ("result_wake", CHAN, "s1")


# ---------------------------------------------------------------------------
# 5. With wake on: no _notify_telegram ping, F087 Telegram leg not required
# ---------------------------------------------------------------------------


def _pool(settings):
    from nous.handlers.subtask_worker import SubtaskWorkerPool

    http = MagicMock()
    http.post = AsyncMock()
    pool = SubtaskWorkerPool(MagicMock(), MagicMock(), settings, None, http_client=http)
    return pool, http


def _subtask(**over):
    base = dict(
        id=uuid.uuid4(), task="F098 build", notify=True, parent_channel=CHAN,
        dag_node_id=None, metadata_={},
    )
    base.update(over)
    return SimpleNamespace(**base)


@pytest.mark.parametrize(
    ("wake", "subtask", "pinged"),
    [
        (True, _subtask(), False),  # the wake turn reports it
        (False, _subtask(), True),  # Phase A: unchanged
        (True, _subtask(parent_channel=None), True),  # scheduled / background notify
        (True, _subtask(parent_channel="api:tim"), True),  # no bot to wake it
    ],
)
async def test_subtask_notify_stands_down_for_wake(wake, subtask, pinged):
    pool, http = _pool(_settings(
        result_wake_enabled=wake, telegram_bot_token="t", telegram_chat_id="4242",
    ))
    await pool._notify_telegram(subtask, result="ok")
    assert http.post.called is pinged


async def _deliver(db, *, wake: bool, origin: str | None, inbox_ok: bool = True):
    from nous.dag.delivery import DAGResultDelivery
    from nous.dag.schemas import DAGCreateRequest, DAGNodeSpec, DAGNodeType
    from nous.dag.store import DAGStore

    agent = _agent()
    s = _settings(
        agent_id=agent, result_wake_enabled=wake, telegram_bot_token="t", telegram_chat_id="4242",
        dag_delivery_bus_enabled=False,
    )
    dags = DAGStore(db, agent, s)
    dag = await dags.create(DAGCreateRequest(
        name="report", nodes=[DAGNodeSpec(name="n", type=DAGNodeType.callback, instructions="x")],
        origin_channel=origin,
    ))
    await dags.update_dag_status(dag.id, "completed", result_summary="ok")
    dag = await dags.get_dag(dag.id)
    inbox = ResultInboxStore(db, agent)
    if not inbox_ok:
        inbox.insert = AsyncMock(side_effect=RuntimeError("db down"))
    http = MagicMock()
    http.post = AsyncMock(return_value=SimpleNamespace(status_code=200))
    outcome = await DAGResultDelivery(s, agent_id=agent, http=http, inbox=inbox).deliver(dag)
    (leg,) = [leg for leg in outcome.legs if leg.name == "telegram"]
    return outcome, leg, http


async def test_dag_telegram_leg_superseded_by_wake(db):
    outcome, leg, http = await _deliver(db, wake=True, origin=CHAN)
    assert (leg.ok, leg.required, leg.detail) == (False, False, "superseded_by_wake")
    assert outcome.delivered and not http.post.called


async def test_dag_telegram_leg_unchanged_without_wake_or_origin(db):
    for wake, origin in ((False, CHAN), (True, None)):
        outcome, leg, http = await _deliver(db, wake=wake, origin=origin)
        assert (leg.ok, leg.required) == (True, True) and http.post.called


async def test_dag_telegram_leg_pushes_when_the_inbox_write_failed(db):
    outcome, leg, http = await _deliver(db, wake=True, origin=CHAN, inbox_ok=False)
    assert (leg.ok, leg.required) == (True, True) and http.post.called


# ---------------------------------------------------------------------------
# Bot: 2. busy chat never woken; 6. same session; 7. flag off -> no poll task
# ---------------------------------------------------------------------------


def _bot(**kw):
    from nous.telegram_bot import NousTelegramBot

    bot = NousTelegramBot("token", "http://nous", **kw)
    bot._tg = AsyncMock(return_value={})
    return bot


def _poll_response(*channels):
    return SimpleNamespace(
        status_code=200,
        json=lambda: {"enabled": True, "channels": [
            {"channel": c, "wake": True, "reason": "ready", "count": 1, "titles": []} for c in channels
        ]},
    )


async def test_bot_wakes_ready_chats_but_not_busy_ones():
    bot = _bot(wake_enabled=True)
    bot._http.get = AsyncMock(return_value=_poll_response("telegram:1", "telegram:2", "api:x"))
    bot._chat_streaming = AsyncMock()
    with bot._busy_chat(2):
        await bot._wake_tick()
    bot._chat_streaming.assert_awaited_once_with(1, "", wake=True)
    await bot.close()


async def test_bot_marks_a_chat_busy_while_its_wake_runs():
    bot = _bot(wake_enabled=True)
    seen = []

    async def fake_stream(chat_id, text, wake=False):
        seen.append(dict(bot._busy))

    bot._http.get = AsyncMock(return_value=_poll_response("telegram:1"))
    bot._chat_streaming = fake_stream
    await bot._wake_tick()
    assert seen == [{1: 1}] and bot._busy == {}
    await bot.close()


class _FakeStream:
    def __init__(self, status: int, lines: list[str], sink: list):
        self.status_code = status
        self._lines = lines
        self._sink = sink

    def __call__(self, method, url, json=None, timeout=None):
        self._sink.append(json)
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def aread(self):
        return b'{"wake": false}'

    async def aiter_lines(self):
        for line in self._lines:
            yield line


async def test_wake_turn_and_reply_share_the_chats_session():
    bot = _bot(wake_enabled=True)
    payloads: list = []
    done = "data: " + json.dumps({"type": "done"})
    bot._http.stream = _FakeStream(200, ["data: " + json.dumps({"type": "text_delta", "text": "hi"}), done], payloads)
    await bot._chat_streaming(4242, "", wake=True)
    await bot._chat_streaming(4242, "tell me more")
    wake, reply = payloads
    assert wake["wake"] is True and "wake" not in reply
    assert wake["session_id"] == reply["session_id"] and wake["chat_id"] == 4242
    await bot.close()


async def test_bot_swallows_a_409_wake():
    bot = _bot(wake_enabled=True)
    bot._send = AsyncMock()
    bot._http.stream = _FakeStream(409, [], [])
    await bot._chat_streaming(4242, "", wake=True)
    bot._send.assert_not_awaited()
    await bot.close()


@pytest.mark.parametrize("enabled", [True, False])
async def test_poll_task_starts_only_with_the_flag(enabled):
    bot = _bot(wake_enabled=enabled)

    async def tg(method, params=None):
        if method == "getUpdates":
            raise asyncio.CancelledError
        return {}

    bot._tg = tg
    with pytest.raises(asyncio.CancelledError):
        await bot.start()
    assert (bot._wake_task is not None) is enabled
    await bot.close()


async def test_update_handling_marks_the_chat_busy():
    bot = _bot()
    seen = []

    async def handle(update):
        seen.append(dict(bot._busy))

    bot._handle_update = handle
    updates = iter([[{"update_id": 1, "message": {"chat": {"id": 9}}}]])

    async def tg(method, params=None):
        if method == "getUpdates":
            try:
                return next(updates)
            except StopIteration:
                raise asyncio.CancelledError from None
        return {}

    bot._tg = tg
    with pytest.raises(asyncio.CancelledError):
        await bot.start()
    assert seen == [{9: 1}] and bot._busy == {}
    await bot.close()

