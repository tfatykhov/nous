"""F111 PR B1: the overdue watcher (plan §4, §5, §9). The store tests run on Postgres (brain.commitments, migration
107); Telegram and the clock are injected."""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from sqlalchemy import text

from nous.brain.commitments import CommitmentStore
from nous.heartbeat.commitment_watcher import UNKNOWN_SEND_CAP, CommitmentWatcher
from nous.heartbeat.send_result import SendResult

GRACE = 900


def _settings(**over: Any) -> SimpleNamespace:
    base = {
        "promise_watcher_enabled": True,
        "promise_watcher_interval_seconds": 300,
        "promise_watcher_since": None,
        "promise_watcher_max_items": 10,
        "promise_gate_classifier_enabled": False,
        "telegram_bot_token": "123:abc",
        "telegram_chat_id": "42",
        "heartbeat_quiet_start": 0,
        "heartbeat_quiet_end": 0,  # start == end: never quiet
        "tool_call_log_enabled": True,
    }
    return SimpleNamespace(**{**base, **over})


class Send:
    """The injected Telegram send: records each message and answers with the scripted results (then SENT)."""

    def __init__(self, *results: SendResult) -> None:
        self.results = list(results)
        self.messages: list[str] = []

    async def __call__(self, message: str) -> SendResult:
        self.messages.append(message)
        return self.results.pop(0) if self.results else SendResult.SENT


@pytest.fixture
async def store(db):
    agent = f"pg-watch-{uuid.uuid4().hex[:8]}"
    yield CommitmentStore(db, agent)
    async with db.session() as s:
        await s.execute(text("DELETE FROM brain.commitments WHERE agent_id = :a"), {"a": agent})
        await s.execute(text("DELETE FROM nous_system.tool_call_log WHERE agent_id = :a"), {"a": agent})
        await s.commit()


@pytest.fixture
def t0() -> datetime:
    return datetime.now(UTC)


async def _row(store: CommitmentStore, t0: datetime, name: str, *, due_in: int = 60, **over: Any):
    """A gate row due ``due_in`` seconds after ``t0`` (validated against ``t0``)."""
    spec = dict(
        source_kind="chat_stream",
        detected_by="gate",
        promise_text=f"I'll relaunch the {name} chain at 18:15 UTC.",
        what=f"I'll relaunch the {name} chain at 18:15 UTC.",
        due_at=t0 + timedelta(seconds=due_in),
        session_id="s1",
        turn_id=f"turn-{name}",
        grace_seconds=GRACE,
        evidence={"due_parsed": True},
        now=t0,
    )
    spec.update(over)
    return await store.create_idempotent(**spec)


def _watcher(store, t0, send=None, classifier=None, **over) -> CommitmentWatcher:
    over.setdefault("promise_watcher_since", (t0 - timedelta(minutes=5)).isoformat())
    w = CommitmentWatcher(store, _settings(**over), send or Send(), classifier=classifier, started_at=t0)
    assert w.start_checks()
    return w


def _past(t0: datetime, seconds: int = 60) -> datetime:
    """Just after due (60 s) + grace."""
    return t0 + timedelta(seconds=seconds + GRACE + 1)


async def _evidence(store: CommitmentStore, short_id: int) -> dict:
    row = await store.get(short_id)
    return row.evidence or {}


async def _notify_count(db, row_id) -> int:
    async with db.session() as s:
        return (
            await s.execute(text("SELECT notify_count FROM brain.commitments WHERE id = :i"), {"i": row_id})
        ).scalar()


# ---- 1, 3, 4. selection ----------------------------------------------------------------------------------------


@pytest.mark.postgres_only
async def test_selection_reads_live_unbacked_overdue_rows_only(store, t0):
    late = await _row(store, t0, "late")
    linked = await _row(store, t0, "linked")
    await store.link_artifact(linked.short_id, "dag", "5c3cedb0")
    send = Send()
    w = _watcher(store, t0, send)
    await w.run_once(_past(t0))
    assert len(send.messages) == 1 and late.label in send.messages[0] and linked.label not in send.messages[0]


@pytest.mark.postgres_only
async def test_the_aug3_statuses_are_never_selected_claimed_or_changed(store, t0):
    rows = {
        "deferred": await _row(store, t0, "deferred"),
        "cancelled": await _row(store, t0, "cancelled"),
        "superseded": await _row(store, t0, "superseded"),
        "goal": await _row(store, t0, "goal", kind="goal"),
        "awaiting": await _row(store, t0, "awaiting", kind="awaiting_user"),
    }
    for status in ("deferred", "cancelled", "superseded"):
        await store.mark(rows[status].short_id, status)
    before = {k: await store.get(r.short_id) for k, r in rows.items()}
    send = Send()
    w = _watcher(store, t0, send)
    for i in range(3):
        await w.run_once(_past(t0, 60 + i * 400))
    assert send.messages == []
    for k, r in rows.items():
        assert await store.get(r.short_id) == before[k], k


@pytest.mark.postgres_only
async def test_the_boundary_is_due_plus_grace(store, t0):
    row = await _row(store, t0, "edge")
    edge = t0 + timedelta(seconds=60 + GRACE)
    send = Send()
    w = _watcher(store, t0, send)
    await w.run_once(edge - timedelta(seconds=1))
    await w.run_once(edge)  # due + grace < now is strict
    assert send.messages == []
    await w.run_once(edge + timedelta(seconds=1))
    assert len(send.messages) == 1 and row.label in send.messages[0]


@pytest.mark.postgres_only
async def test_rows_created_before_the_cutoff_are_not_watched(store, t0):
    await _row(store, t0, "shadow-era")
    send = Send()
    w = _watcher(store, t0, send, promise_watcher_since=(t0 + timedelta(minutes=1)).isoformat())
    await w.run_once(_past(t0))
    assert send.messages == []


# ---- 2. start-up checks ----------------------------------------------------------------------------------------


@pytest.mark.parametrize("since", [None, "", "   ", "next tuesday", "2026-11-20T00:00:00"])
def test_a_missing_or_bad_cutoff_stops_the_watcher_with_one_warning(since, caplog):
    w = CommitmentWatcher(object(), _settings(promise_watcher_since=since), Send())
    with caplog.at_level(logging.WARNING):
        assert w.start_checks() is False
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and "NOUS_PROMISE_WATCHER_SINCE" in warnings[0].getMessage()
    assert w.dead_man_line(datetime.now(UTC) + timedelta(days=1)) is None  # not running: no stall to report


@pytest.mark.parametrize("missing", ["telegram_bot_token", "telegram_chat_id"])
def test_telegram_not_configured_stops_the_watcher_with_one_warning(missing, caplog):
    settings = _settings(promise_watcher_since="2026-11-20T00:00:00+00:00", **{missing: None})
    w = CommitmentWatcher(object(), settings, Send())
    with caplog.at_level(logging.WARNING):
        assert w.start_checks() is False
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and "Telegram" in warnings[0].getMessage()


def test_classifier_off_runs_with_the_d_warning_once(caplog):
    w = CommitmentWatcher(object(), _settings(promise_watcher_since="2026-11-20T00:00:00+00:00"), Send())
    with caplog.at_level(logging.WARNING):
        assert w.start_checks() is True
    [warning] = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert "without stage-2 classifier" in warning.getMessage()
    assert "false positives included" in warning.getMessage()


# ---- 5, 6, 7. one message, one claim ---------------------------------------------------------------------------


@pytest.mark.postgres_only
async def test_three_rows_one_message_each_pinged_once_over_five_runs(store, t0):
    rows = [await _row(store, t0, f"r{i}", due_in=60 + i) for i in range(3)]
    send = Send()
    w = _watcher(store, t0, send)
    for i in range(5):
        await w.run_once(_past(t0, 70 + i * 300))
    assert len(send.messages) == 1
    for row in rows:
        assert row.label in send.messages[0]
        ping = (await _evidence(store, row.short_id))["ping"]
        assert ping["state"] == "sent" and "sent_at" in ping


@pytest.mark.postgres_only
async def test_two_concurrent_runs_send_one_message(store, t0):
    for i in range(3):
        await _row(store, t0, f"c{i}")
    send = Send()
    w1, w2 = _watcher(store, t0, send), _watcher(store, t0, send)
    await asyncio.gather(w1.run_once(_past(t0)), w2.run_once(_past(t0)))
    assert len(send.messages) == 1


@pytest.mark.postgres_only
async def test_the_claim_only_returns_unclaimed_rows(store, t0):
    a, b = await _row(store, t0, "a"), await _row(store, t0, "b")
    first = await store.claim_for_ping([a.id], "run-1", t0)
    second = await store.claim_for_ping([a.id, b.id], "run-2", t0)
    assert [r.id for r in first] == [a.id] and [r.id for r in second] == [b.id]


def test_the_claim_sql_sets_and_never_increments():
    import inspect

    from nous.brain import commitments

    source = inspect.getsource(commitments.CommitmentStore)
    assert "notify_count + 1" not in source.replace("  ", " ")
    assert "notify_count = notify_count" not in source
    assert "notify_count = 1, notified_at" in source


@pytest.mark.postgres_only
async def test_a_sent_ping_whose_finalize_fails_is_never_sent_again(store, t0, monkeypatch, caplog):
    row = await _row(store, t0, "fin")
    send = Send()
    w = _watcher(store, t0, send)

    async def broken(*a, **k):
        raise RuntimeError("database went away")

    monkeypatch.setattr(store, "finalize_ping", broken)
    with caplog.at_level(logging.WARNING):
        await w.run_once(_past(t0))
    monkeypatch.undo()
    await w.run_once(_past(t0, 400))
    assert len(send.messages) == 1 and row.label in send.messages[0]
    assert (await _evidence(store, row.short_id))["ping"]["state"] == "claimed"


# ---- 8. send results -------------------------------------------------------------------------------------------


@pytest.mark.postgres_only
async def test_an_outage_only_delays_the_ping(store, t0, db):
    row = await _row(store, t0, "outage")
    send = Send(*[SendResult.NOT_SENT] * 5)
    w = _watcher(store, t0, send)
    for i in range(5):
        await w.run_once(_past(t0, 60 + i * 300))
        ping = (await _evidence(store, row.short_id))["ping"]
        assert ping["state"] == "released" and ping["unknown_sends"] == 0 and ping["last_result"] == "not_sent"
        assert await _notify_count(db, row.id) == 0
    await w.run_once(_past(t0, 2000))
    assert len(send.messages) == 6
    assert (await _evidence(store, row.short_id))["ping"]["state"] == "sent"
    await w.run_once(_past(t0, 2400))
    assert len(send.messages) == 6


@pytest.mark.postgres_only
async def test_unknown_sends_are_counted_and_stop_at_the_cap(store, t0, db, caplog):
    row = await _row(store, t0, "unknown")
    send = Send(*[SendResult.UNKNOWN] * 5)
    w = _watcher(store, t0, send)
    for i in range(UNKNOWN_SEND_CAP - 1):
        await w.run_once(_past(t0, 60 + i * 300))
        ping = (await _evidence(store, row.short_id))["ping"]
        assert ping["state"] == "released" and ping["unknown_sends"] == i + 1 and ping["last_result"] == "unknown"
    with caplog.at_level(logging.WARNING):
        await w.run_once(_past(t0, 1000))
    ping = (await _evidence(store, row.short_id))["ping"]
    assert ping["state"] == "failed" and ping["unknown_sends"] == UNKNOWN_SEND_CAP
    assert await _notify_count(db, row.id) == 1
    assert any(row.label in r.getMessage() for r in caplog.records if r.levelno == logging.WARNING)
    for i in range(3):
        await w.run_once(_past(t0, 1400 + i * 300))
    assert len(send.messages) == UNKNOWN_SEND_CAP


class _Client:
    """An httpx-like client: answers with ``status`` or raises ``exc``."""

    def __init__(self, status: int | None = None, exc: Exception | None = None) -> None:
        self.status, self.exc, self.posts = status, exc, 0

    async def post(self, url, json=None, timeout=None):
        self.posts += 1
        if self.exc is not None:
            raise self.exc
        return SimpleNamespace(status_code=self.status)


def _hb(client, **settings):
    from nous.heartbeat.runner import HeartbeatRunner

    hb = HeartbeatRunner.__new__(HeartbeatRunner)
    hb._settings = SimpleNamespace(**{"telegram_bot_token": "123:abc", "telegram_chat_id": "42", **settings})
    hb._http = client
    return hb


_REQ = httpx.Request("POST", "https://api.telegram.org/x")


@pytest.mark.parametrize(
    ("client", "expected"),
    [
        (_Client(200), SendResult.SENT),
        (_Client(429), SendResult.NOT_SENT),
        (_Client(500), SendResult.NOT_SENT),
        (_Client(exc=httpx.ConnectError("refused", request=_REQ)), SendResult.NOT_SENT),
        (_Client(exc=httpx.ConnectTimeout("slow", request=_REQ)), SendResult.NOT_SENT),
        (_Client(exc=httpx.PoolTimeout("pool", request=_REQ)), SendResult.NOT_SENT),
        (_Client(exc=httpx.InvalidURL("bad token character")), SendResult.NOT_SENT),
        (_Client(exc=httpx.UnsupportedProtocol("ftp", request=_REQ)), SendResult.NOT_SENT),
        (_Client(exc=httpx.ProxyError("proxy refused", request=_REQ)), SendResult.NOT_SENT),
        (_Client(exc=httpx.ReadTimeout("read", request=_REQ)), SendResult.UNKNOWN),
        (_Client(exc=httpx.WriteTimeout("write", request=_REQ)), SendResult.UNKNOWN),
        (_Client(exc=httpx.ReadError("reset", request=_REQ)), SendResult.UNKNOWN),
        (_Client(exc=httpx.RemoteProtocolError("eof", request=_REQ)), SendResult.UNKNOWN),
        (_Client(exc=RuntimeError("odd")), SendResult.UNKNOWN),
    ],
    ids=lambda v: getattr(v, "name", None) or type(getattr(v, "exc", None) or v).__name__,
)
async def test_send_telegram_maps_each_outcome(client, expected):
    assert await _hb(client)._send_telegram("hello") is expected


async def test_send_telegram_not_configured_is_not_sent():
    client = _Client(200)
    assert await _hb(client, telegram_bot_token=None)._send_telegram("hello") is SendResult.NOT_SENT
    assert await _hb(client, telegram_chat_id="")._send_telegram("hello") is SendResult.NOT_SENT
    assert client.posts == 0


async def test_a_send_refused_every_run_warns_once_then_once_an_hour(caplog):
    """Review M4: a send refused the same way for ever (a wrong token answers 401 every interval) logged a WARNING
    every run. Now: the first, then at most one per hour with the count left out, and again at once after a ping
    went out."""

    class Settles:
        async def release_ping(self, counts, run_id, reason):
            return None

        async def finalize_ping(self, ids, run_id, now):
            return None

    t0 = datetime(2026, 11, 12, 9, 0, tzinfo=UTC)
    w = CommitmentWatcher(Settles(), _settings(), Send())
    row = SimpleNamespace(id=uuid.uuid4(), label="C-0001", evidence={})

    def released() -> list[str]:
        return [r.getMessage() for r in caplog.records if "released" in r.getMessage()]

    with caplog.at_level(logging.WARNING):
        for run in range(13):  # one hour of 5-minute runs, and the first run of the next
            await w._settle([row], f"run{run}", SendResult.NOT_SENT, t0 + timedelta(seconds=300 * run))
        assert len(released()) == 2
        assert "(11 more like it since the last line)" in released()[1]
        await w._settle([row], "unknown", SendResult.UNKNOWN, t0 + timedelta(seconds=300 * 12 + 1))
        assert len(released()) == 3 and "was unknown" in released()[2]  # each outcome has its own hour
        await w._settle([row], "ok", SendResult.SENT, t0 + timedelta(seconds=300 * 13))
        await w._settle([row], "again", SendResult.NOT_SENT, t0 + timedelta(seconds=300 * 14))
    assert len(released()) == 4 and "more like it" not in released()[3]


# ---- 9. a crash between claim and send -------------------------------------------------------------------------


@pytest.mark.postgres_only
async def test_a_claim_left_by_a_crash_is_never_sent_and_is_orphaned(store, t0, caplog):
    row = await _row(store, t0, "crash")
    await store.claim_for_ping([row.id], "dead-run", _past(t0))  # the process died here
    send = Send()
    w = _watcher(store, t0, send)
    await w.run_once(_past(t0, 400))
    assert (await _evidence(store, row.short_id))["ping"]["state"] == "claimed"
    with caplog.at_level(logging.WARNING):
        await w.run_once(_past(t0) + timedelta(seconds=2 * 300 + 1))
    assert (await _evidence(store, row.short_id))["ping"]["state"] == "orphaned"
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len([m for m in warnings if "orphaned" in m]) == 1 and any(row.label in m for m in warnings)
    assert send.messages == []


# ---- 10. quiet hours -------------------------------------------------------------------------------------------


@pytest.mark.postgres_only
async def test_nothing_is_claimed_in_quiet_hours_and_rows_go_out_after(store, t0):
    row = await _row(store, t0, "quiet")
    run_at = _past(t0)
    quiet = {"heartbeat_quiet_start": run_at.hour, "heartbeat_quiet_end": (run_at.hour + 1) % 24}
    send = Send()
    w = _watcher(store, t0, send, **quiet)
    await w.run_once(run_at)
    assert send.messages == [] and w.last_tick_at == run_at
    assert "ping" not in await _evidence(store, row.short_id)
    after = run_at.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1, minutes=1)
    await w.run_once(after)
    assert len(send.messages) == 1 and row.label in send.messages[0] and w.last_tick_at == after


# ---- 11. wording -----------------------------------------------------------------------------------------------


@pytest.mark.postgres_only
async def test_the_message_wording(store, t0):
    long_text = "I'll relaunch the chain " + "and check every shard " * 12 + "at 18:15 UTC."
    parsed = await _row(store, t0, "parsed", due_in=59)  # first in the message (rows go by due_at, then id)
    default = await _row(store, t0, "default", evidence={"due_parsed": False}, promise_text=long_text)
    send = Send()
    w = _watcher(store, t0, send)
    await w.run_once(_past(t0))
    [message] = send.messages
    assert message.startswith("Possibly unbacked commitments:")
    assert f"{parsed.label} '{parsed.promise_text}' (due {parsed.due_at:%a %d %b %H:%M} UTC)" in message
    assert f"{default.label} '{long_text[:119]}…'" in message and long_text not in message
    line = message[message.index(default.label) :]
    assert "(no time stated)" in line.split("\n")[0]
    assert "(not checked)" in line.split("\n")[0]  # the classifier is off: a gate row is not checked
    assert f"/done {parsed.label}" in message and f"/drop {parsed.label}" in message
    assert "more will follow" not in message


@pytest.mark.postgres_only
async def test_a_commit_row_is_never_marked_not_checked(store, t0):
    row = await _row(store, t0, "tool", detected_by="tool")
    send = Send()
    await _watcher(store, t0, send).run_once(_past(t0))
    [message] = send.messages
    assert row.label in message and "(not checked)" not in message


# ---- 12. the cap -----------------------------------------------------------------------------------------------


@pytest.mark.postgres_only
async def test_the_cap_claims_only_the_listed_rows(store, t0, db):
    rows = [await _row(store, t0, f"cap{i}", due_in=60 + i) for i in range(5)]
    send = Send()
    w = _watcher(store, t0, send, promise_watcher_max_items=2)
    await w.run_once(_past(t0, 70))
    assert "+3 more will follow." in send.messages[0]
    counts = [await _notify_count(db, r.id) for r in rows]
    assert counts == [1, 1, 0, 0, 0]
    await w.run_once(_past(t0, 370))
    await w.run_once(_past(t0, 670))
    assert len(send.messages) == 3
    assert "+1 more will follow." in send.messages[1] and "more will follow" not in send.messages[2]
    for row in rows:
        assert sum(row.label in m for m in send.messages) == 1, row.label


# ---- 13. same-chat suppression ---------------------------------------------------------------------------------


async def _call(db, store, *, session="s1", turn=None, tool="schedule_task", outcome="ran", at=None) -> uuid.UUID:
    call_id = uuid.uuid4()
    async with db.session() as s:
        await s.execute(
            text(
                "INSERT INTO nous_system.tool_call_log (id, agent_id, session_id, turn_id, call_index, work_kind, "
                "tool_name, tool_name_called, outcome, is_error, started_at) VALUES (:id, :a, :s, :t, 0, 'chat', "
                ":tool, :tool, :outcome, :err, :at)"
            ),
            {
                "id": call_id,
                "a": store._agent_id,
                "s": session,
                "t": turn or uuid.uuid4(),
                "tool": tool,
                "outcome": outcome,
                "err": outcome != "ran",
                "at": at or datetime.now(UTC) + timedelta(seconds=1),
            },
        )
        await s.commit()
    return call_id


@pytest.mark.postgres_only
async def test_a_later_backing_call_in_the_same_session_suppresses(store, t0, db):
    row = await _row(store, t0, "sup")
    call_id = await _call(db, store)
    send = Send()
    w = _watcher(store, t0, send)
    await w.run_once(_past(t0))
    assert send.messages == []
    watch = (await _evidence(store, row.short_id))["watch"]
    assert watch["suppressed_by"] == str(call_id)
    assert (await store.get(row.short_id)).status == "open"
    await w.run_once(_past(t0, 400))
    assert send.messages == []


@pytest.mark.postgres_only
@pytest.mark.parametrize("case", ["earlier", "same_turn", "other_session", "failed", "other_tool"])
async def test_what_does_not_suppress(store, t0, db, case):
    row = await _row(store, t0, f"nosup-{case}", turn_id=str(uuid.uuid4()))
    if case == "earlier":
        await _call(db, store, at=row.created_at - timedelta(seconds=5))
    elif case == "same_turn":
        await _call(db, store, turn=uuid.UUID(row.turn_id))
    elif case == "other_session":
        await _call(db, store, session="s2")
    elif case == "failed":
        await _call(db, store, outcome="error")
    else:
        await _call(db, store, tool="record_decision")
    send = Send()
    await _watcher(store, t0, send).run_once(_past(t0))
    assert len(send.messages) == 1 and row.label in send.messages[0]


@pytest.mark.postgres_only
async def test_with_the_tool_call_log_off_the_row_is_pinged(store, t0, db):
    row = await _row(store, t0, "logoff")
    await _call(db, store)
    send = Send()
    await _watcher(store, t0, send, tool_call_log_enabled=False).run_once(_past(t0))
    assert len(send.messages) == 1 and row.label in send.messages[0]


@pytest.mark.postgres_only
async def test_a_failing_suppression_query_pings(store, t0, monkeypatch, caplog):
    row = await _row(store, t0, "qfail")

    async def broken(rows):
        raise RuntimeError("no such table")

    monkeypatch.setattr(store, "find_suppressors", broken)
    send = Send()
    with caplog.at_level(logging.WARNING):
        await _watcher(store, t0, send).run_once(_past(t0))
    assert len(send.messages) == 1 and row.label in send.messages[0]


# ---- 14. stage-2 rules -----------------------------------------------------------------------------------------


class Classifier:
    """``classify_now`` with scripted verdicts, storing them like the real scheduler."""

    def __init__(self, store, verdicts: dict[str, str] | None = None) -> None:
        self.store, self.verdicts, self.calls = store, verdicts or {}, []

    async def classify_now(self, row):
        self.calls.append(row.short_id)
        prev = ((row.evidence or {}).get("stage2") or {}).get("attempts", 0)
        doc = {"verdict": self.verdicts.get(row.what, "error"), "attempts": prev + 1}
        await self.store.set_stage2(row.id, doc)
        return doc


@pytest.mark.postgres_only
async def test_stage2_rules(store, t0):
    promise = await _row(store, t0, "p")
    not_promise = await _row(store, t0, "np")
    errored = await _row(store, t0, "err")
    unclassified = await _row(store, t0, "none")
    tool = await _row(store, t0, "tool", detected_by="tool")
    await store.set_stage2(promise.id, {"verdict": "promise", "attempts": 1})
    await store.set_stage2(not_promise.id, {"verdict": "not_promise", "attempts": 1})
    await store.set_stage2(errored.id, {"verdict": "error", "attempts": 1})
    classifier = Classifier(store, {unclassified.what: "promise"})
    send = Send()
    w = _watcher(store, t0, send, classifier=classifier, promise_gate_classifier_enabled=True)
    await w.run_once(_past(t0))
    [message] = send.messages
    lines = {r.label: next(ln for ln in message.split("\n") if r.label in ln) for r in (promise, errored, unclassified)}
    assert not_promise.label not in message and tool.label in message
    assert "(not checked)" not in lines[promise.label] and "(not checked)" not in lines[unclassified.label]
    assert "(not checked)" in lines[errored.label]  # retried once, still error
    assert sorted(classifier.calls) == sorted([errored.short_id, unclassified.short_id])  # never the commit row
    assert (await _evidence(store, errored.short_id))["stage2"]["attempts"] == 2
    assert (await store.get(not_promise.short_id)).status == "open"
    await w.run_once(_past(t0, 400))
    assert len(send.messages) == 1 and len(classifier.calls) == 2  # nothing retried or sent again


@pytest.mark.postgres_only
async def test_with_the_classifier_off_every_gate_row_without_a_not_promise_verdict_is_pinged(store, t0):
    """Owner ruling I3 on PR #906 (a change to plan D): a stored not_promise verdict keeps a row out of the ping
    whatever the classifier flag says; the flag decides only what happens to rows with no verdict."""
    row = await _row(store, t0, "off")
    rejected = await _row(store, t0, "off-np")
    await store.set_stage2(rejected.id, {"verdict": "not_promise", "attempts": 1})
    send = Send()
    await _watcher(store, t0, send).run_once(_past(t0))
    [message] = send.messages
    assert row.label in message and rejected.label not in message
    assert (await store.get(rejected.short_id)).status == "open"


@pytest.mark.postgres_only
async def test_not_promise_rows_do_not_fill_the_selection(store, t0):
    for i in range(3):
        r = await _row(store, t0, f"np{i}", due_in=60 + i)
        await store.set_stage2(r.id, {"verdict": "not_promise", "attempts": 1})
    real = await _row(store, t0, "real", due_in=90)
    await store.set_stage2(real.id, {"verdict": "promise", "attempts": 1})
    watchable = await store.list_watchable(now=_past(t0, 100), limit=1)
    assert [r.id for r in watchable] == [real.id]


# ---- 15. the dead-man line (the watcher's part; the runner's part is in test_promise_gate_runner) ---------------


def test_the_dead_man_line_shows_once_per_stall():
    t0 = datetime(2026, 11, 12, 9, 0, tzinfo=UTC)
    w = CommitmentWatcher(object(), _settings(promise_watcher_since=t0.isoformat()), Send(), started_at=t0)
    line = w.dead_man_line(t0 + timedelta(seconds=901))
    assert line is None  # start-up checks not passed: the watcher is not running
    assert w.start_checks()
    assert w.dead_man_line(t0 + timedelta(seconds=900)) is None  # 3 intervals is not yet a stall
    line = w.dead_man_line(t0 + timedelta(seconds=901))
    assert line == ("⚠ Commitment watcher has not run since 09:00 UTC; overdue promises are not being checked.")
    assert w.dead_man_line(t0 + timedelta(seconds=1200)) is None  # once per stall
    w.note_tick(t0 + timedelta(seconds=1300))
    assert w.dead_man_line(t0 + timedelta(seconds=1400)) is None  # a fresh tick
    again = w.dead_man_line(t0 + timedelta(seconds=1300 + 901))
    assert again is not None and "09:21 UTC" in again  # a new stall


async def test_one_failed_run_does_not_show_the_dead_man_line(caplog):
    """Review M1: a failed run is no tick, so with a threshold of 2 intervals one failed run between two good ones
    showed the line to a chat reply while the next run was still under way."""

    class FailsOnce:
        def __init__(self) -> None:
            self.runs = 0

        async def expire_stale_claims(self, older_than):
            self.runs += 1
            if self.runs == 2:
                raise RuntimeError("one bad run")
            return []

        async def list_watchable(self, **kwargs):
            return []

    t0 = datetime(2026, 11, 12, 9, 0, tzinfo=UTC)
    w = CommitmentWatcher(FailsOnce(), _settings(promise_watcher_since=t0.isoformat()), Send(), started_at=t0)
    assert w.start_checks()
    await w.run_once(t0 + timedelta(seconds=300))  # ticks at 09:05
    with caplog.at_level(logging.WARNING):
        await w.run_once(t0 + timedelta(seconds=600))  # fails: no tick
    assert w.last_tick_at == t0 + timedelta(seconds=300)
    # The next run starts at 09:15 and takes a few seconds; a chat reply meanwhile sees no line.
    assert w.dead_man_line(t0 + timedelta(seconds=300 + 600 + 30)) is None
    await w.run_once(t0 + timedelta(seconds=960))
    assert w.dead_man_line(t0 + timedelta(seconds=1000)) is None


# ---- 16. error isolation ---------------------------------------------------------------------------------------


async def test_an_exception_in_a_run_is_caught_with_a_warning(caplog):
    class Broken:
        async def expire_stale_claims(self, older_than):
            raise RuntimeError("boom")

    t0 = datetime.now(UTC)
    w = CommitmentWatcher(Broken(), _settings(promise_watcher_since=t0.isoformat()), Send(), started_at=t0)
    assert w.start_checks()
    with caplog.at_level(logging.WARNING):
        await w.run_once(t0 + timedelta(seconds=300))
    assert any("watcher run failed" in r.getMessage() for r in caplog.records if r.levelno == logging.WARNING)
    assert w.last_tick_at == t0  # a failed run is no tick: the dead-man line can still report it


async def test_the_heartbeat_watcher_loop_survives_a_raising_run(monkeypatch):
    from nous.heartbeat.runner import HeartbeatRunner

    runs: list[int] = []

    class W:
        interval_seconds = 0

        async def run_once(self, now=None):
            runs.append(1)
            if len(runs) == 1:
                raise RuntimeError("unexpected")

    hb = HeartbeatRunner.__new__(HeartbeatRunner)
    hb._running = True
    hb._commitment_watcher = W()
    task = asyncio.create_task(hb._watcher_loop())
    while len(runs) < 3:
        await asyncio.sleep(0.01)
    hb._running = False
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert len(runs) >= 3


# ---- review round: start-up gate, retry limits, quiet-hours work, message size, a failing settle ---------------------


async def test_the_heartbeat_starts_the_watcher_only_when_its_checks_pass(monkeypatch):
    from unittest.mock import MagicMock

    from nous.heartbeat.runner import HeartbeatRunner

    async def idle(self):
        await asyncio.sleep(3600)

    async def nothing(self):
        return None

    monkeypatch.setattr(HeartbeatRunner, "_loop", idle)
    monkeypatch.setattr(HeartbeatRunner, "_dag_loop", idle)
    monkeypatch.setattr(HeartbeatRunner, "_detect_missed_checks", nothing)
    hb_settings = SimpleNamespace(
        heartbeat_tick_interval=60,
        heartbeat_quiet_start=0,
        heartbeat_quiet_end=0,
        heartbeat_daily_token_budget=1,
        dag_tick_interval=60,
        dag_tick_timeout=1,
    )
    for since, started in ((None, False), ("2026-11-20T00:00:00+00:00", True)):
        watcher = CommitmentWatcher(object(), _settings(promise_watcher_since=since), Send())
        hb = HeartbeatRunner(
            hb_settings, MagicMock(), MagicMock(), MagicMock(), MagicMock(), None, None, commitment_watcher=watcher
        )
        await hb.start()
        try:
            assert (hb._watcher_task is not None) is started and watcher.active is started
        finally:
            await hb.stop()
        assert hb._watcher_task is None


@pytest.mark.postgres_only
async def test_a_row_that_used_its_stage2_attempts_is_not_retried_and_is_not_checked(store, t0):
    row = await _row(store, t0, "spent")
    await store.set_stage2(row.id, {"verdict": "error", "attempts": 2})
    classifier = Classifier(store)
    send = Send()
    await _watcher(store, t0, send, classifier=classifier, promise_gate_classifier_enabled=True).run_once(_past(t0))
    assert classifier.calls == []
    [message] = send.messages
    assert "(not checked)" in next(ln for ln in message.split("\n") if row.label in ln)


@pytest.mark.postgres_only
async def test_a_retry_that_says_not_promise_is_never_pinged_and_stays_open(store, t0, db):
    row = await _row(store, t0, "retry-np")
    classifier = Classifier(store, {row.what: "not_promise"})
    send = Send()
    w = _watcher(store, t0, send, classifier=classifier, promise_gate_classifier_enabled=True)
    await w.run_once(_past(t0))
    assert send.messages == [] and classifier.calls == [row.short_id]
    assert await _notify_count(db, row.id) == 0 and (await store.get(row.short_id)).status == "open"
    await w.run_once(_past(t0, 400))
    assert send.messages == [] and classifier.calls == [row.short_id]  # left out of the read from now on


class _WrongTypeClient:
    """An LLM client whose tool call carries a label of the wrong type (Codex on 391e7849)."""

    supports_call_context = True

    async def call(self, payload: dict, *, call_context: Any = None) -> Any:
        return SimpleNamespace(
            stop_reason="tool_use", content=[{"type": "tool_use", "input": {"verdict": [], "reason": "x"}}]
        )


@pytest.mark.postgres_only
async def test_a_retry_whose_label_has_the_wrong_type_stores_error_and_the_run_goes_on(store, t0):
    from nous.cognitive.promise_classifier import ClassifierScheduler

    row = await _row(store, t0, "wrong-type")
    settings = _settings(
        promise_gate_classifier_enabled=True,
        promise_gate_classifier_model="claude-haiku-5-5",
        promise_gate_classifier_timeout_seconds=10,
        promise_gate_classifier_max_concurrency=2,
    )
    classifier = ClassifierScheduler(store, settings, _WrongTypeClient())
    send = Send()
    await _watcher(store, t0, send, classifier=classifier, promise_gate_classifier_enabled=True).run_once(_past(t0))
    assert (await _evidence(store, row.short_id))["stage2"]["verdict"] == "error"
    [message] = send.messages
    assert "(not checked)" in next(ln for ln in message.split("\n") if row.label in ln)


@pytest.mark.postgres_only
async def test_the_retries_of_one_run_are_bounded(store, t0):
    rows = [await _row(store, t0, f"bound{i}", due_in=60 + i) for i in range(6)]
    classifier = Classifier(store, {r.what: "not_promise" for r in rows})
    w = _watcher(
        store, t0, Send(), classifier=classifier, promise_gate_classifier_enabled=True, promise_watcher_max_items=2
    )
    await w.run_once(_past(t0, 70))
    assert len(classifier.calls) == 4  # 2 per listed row; the other two wait for the next run
    await w.run_once(_past(t0, 370))
    assert len(classifier.calls) == 6


@pytest.mark.postgres_only
async def test_quiet_hours_still_expire_stale_claims_and_retry_stage2(store, t0):
    stale = await _row(store, t0, "q-stale")
    fresh = await _row(store, t0, "q-fresh")
    run_at = _past(t0) + timedelta(seconds=601)
    await store.claim_for_ping([stale.id], "dead-run", _past(t0) - timedelta(seconds=1))
    quiet = {"heartbeat_quiet_start": run_at.hour, "heartbeat_quiet_end": (run_at.hour + 1) % 24}
    classifier = Classifier(store, {fresh.what: "promise"})
    send = Send()
    w = _watcher(store, t0, send, classifier=classifier, promise_gate_classifier_enabled=True, **quiet)
    await w.run_once(run_at)
    assert send.messages == []
    assert (await _evidence(store, stale.short_id))["ping"]["state"] == "orphaned"
    assert classifier.calls == [fresh.short_id]
    assert (await _evidence(store, fresh.short_id))["stage2"]["verdict"] == "promise"


def test_the_longest_message_fits_telegram():
    from nous.config import Settings
    from nous.heartbeat.runner import _TELEGRAM_MAX_CHARS

    cap = Settings.model_fields["promise_watcher_max_items"].metadata
    assert any(getattr(m, "le", None) == 15 for m in cap)
    t0 = datetime(2026, 11, 12, 9, 0, tzinfo=UTC)
    rows = [
        SimpleNamespace(
            label=f"C-{100000 + i}",
            promise_text="x" * 500,
            due_at=t0,
            evidence={"due_parsed": False},
            detected_by="gate",
            stage2={},
        )
        for i in range(15)
    ]
    w = CommitmentWatcher(object(), _settings(promise_watcher_since=t0.isoformat()), Send())
    assert len(w.render(rows, 999)) < _TELEGRAM_MAX_CHARS


async def test_a_failing_release_is_logged_with_the_rows_and_does_not_raise(caplog):
    class Store:
        async def release_ping(self, *a, **k):
            raise RuntimeError("database went away")

    t0 = datetime.now(UTC)
    w = CommitmentWatcher(Store(), _settings(promise_watcher_since=t0.isoformat()), Send(), started_at=t0)
    row = SimpleNamespace(id=uuid.uuid4(), label="C-0077", evidence={})
    with caplog.at_level(logging.WARNING):
        await w._settle([row], "run-x", SendResult.NOT_SENT, t0)
    messages = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("C-0077" in m and "not recorded" in m and "run-x" in m for m in messages)
