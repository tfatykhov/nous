"""F118 PR 3: the manager of the second judge's settings applies each change at once, commit then apply, in one task.

Real Postgres for the row; the first judge is a stand-in that records every call, and after each test it must be the
same object, never started, closed or replaced, and still taking decisions. Most tests run the second judge as a
stand-in service (``_make_service``) whose close can be held; a few build the real one with a fake transport. No
request leaves the process.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text

from nous.cognitive.ledger_store import redact_output
from nous.config import Settings
from nous.judge import second_settings
from nous.judge.jev_judge import JevJudge
from nous.judge.second_settings import (
    CHECK_LIMIT,
    MODE_OFF,
    NO_KEY_FILE_TEXT,
    NO_KEY_TO_CHECK,
    NOT_LOADED,
    SHUTTING_DOWN,
    SecondJudgeManager,
    seal_key,
)
from nous.judge.service import JudgeSet, OutsideJudge
from nous.mcp_client.secrets import SecretBox

pytestmark = pytest.mark.postgres_only

KEY = "ts-manager-" + "M3" * 12
KEY_2 = "ts-manager-" + "N8" * 12
ENV_KEY = "ts-env-key-" + "V5" * 10
BOX = SecretBox(b"\x33" * 32)
MODEL = "jev-1.14.0"


@pytest.fixture(autouse=True)
def _no_request_leaves_the_process(monkeypatch):
    async def refuse(self: Any, request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"a real request was attempted to {request.url.host}")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", refuse)


class FixedKeyFile:
    """The key file as the manager reads it: a box, or None (no key file)."""

    def __init__(self, box: SecretBox | None) -> None:
        self._box = box

    def box(self) -> SecretBox | None:
        return self._box


class Primary:
    """The first judge, a stand-in that records every call made on it."""

    def __init__(self) -> None:
        self.judge = SimpleNamespace(name="background_model", version="dj-2", model="claude-test")
        self.calls: list[str] = []
        self.submitted: list[Any] = []
        self.daily_calls = 100

    def submit_decision(self, decision_id: Any, **claim: Any) -> bool:
        self.submitted.append(decision_id)
        return True

    def runtime(self) -> dict[str, Any]:
        return {"queued": 0, "dropped": 0, "over_cap": 0, "worker_alive": True}

    def start(self) -> None:  # pragma: no cover - must never be called
        self.calls.append("start")

    async def close(self, timeout: float = 5.0) -> None:  # pragma: no cover - must never be called
        self.calls.append("close")


class StandIn:
    """The second judge's service: records what it was built for, what it was given and when it closed. Its close
    waits for ``release``."""

    def __init__(self, config: Any, events: list[str]) -> None:
        self.config = config
        self.judge = SimpleNamespace(name="jev", version=f"jq-2:{config.model}", model=config.model)
        self.daily_calls = config.daily_calls
        self.events = events
        self.started = False
        self.closed = False
        self.submitted: list[Any] = []
        self.release = asyncio.Event()
        self.release.set()
        self.closing = asyncio.Event()

    def start(self) -> None:
        self.started = True

    def submit_decision(self, decision_id: Any, **claim: Any) -> bool:
        if self.closed:
            return False
        self.submitted.append(decision_id)
        return True

    def runtime(self) -> dict[str, Any]:
        return {"queued": 0, "dropped": 0, "over_cap": 0, "worker_alive": not self.closed}

    async def close(self, timeout: float = 5.0) -> None:
        self.closing.set()
        await self.release.wait()
        self.closed = True
        self.events.append(f"closed {self.config.key}")


class FlakyDatabase:
    """The real database, with failures on demand: reads of the settings row, the write before its commit, or the
    commit (before or after it landed)."""

    def __init__(self, real: Any) -> None:
        self.real = real
        self.failing_reads = 0
        self.fail_write = False
        self.fail_commit: str | None = None  # "before" | "after"

    @asynccontextmanager
    async def session(self):
        async with self.real.session() as session:
            yield _FlakySession(session, self)


class _FlakySession:
    def __init__(self, session: Any, database: FlakyDatabase) -> None:
        self._session = session
        self._database = database

    async def execute(self, statement: Any, *args: Any, **kwargs: Any) -> Any:
        sql = str(statement)
        if "judge_second_settings" in sql and sql.lstrip().startswith("SELECT") and self._database.failing_reads:
            self._database.failing_reads -= 1
            raise ConnectionError(f"read refused {KEY}")
        if "judge_second_settings" in sql and sql.lstrip().startswith("INSERT") and self._database.fail_write:
            raise ConnectionError(f"write refused {KEY}")
        return await self._session.execute(statement, *args, **kwargs)

    async def commit(self) -> None:
        if self._database.fail_commit == "after":
            await self._session.commit()
            raise ConnectionError(f"commit lost {KEY}")
        if self._database.fail_commit == "before":
            raise ConnectionError(f"commit refused {KEY}")
        await self._session.commit()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._session, name)


class Env:
    """One test's manager, its judge set, the first judge and the second judge's services."""

    def __init__(self, manager: SecondJudgeManager, judges: JudgeSet, primary: Primary, database: FlakyDatabase):
        self.manager = manager
        self.judges = judges
        self.primary = primary
        self.database = database
        self.events: list[str] = []
        self.built: list[StandIn] = []

    @property
    def second(self) -> Any:
        return self.judges.second

    async def row(self) -> Any:
        async with self.database.real.session() as session:
            return await second_settings.read_row(session, self.manager._agent_id)


@pytest_asyncio.fixture
async def make(db, monkeypatch):
    envs: list[Env] = []
    agents: list[str] = []

    async def build(
        *,
        stand_ins: bool = True,
        mode: str = "shadow",
        key_file: SecretBox | None = BOX,
        row: dict[str, Any] | None = None,
        key: str | None = None,
        load: bool = True,
        transport: Any = None,
        clock: Any = None,
        monotonic: Any = None,
        **over: Any,
    ) -> Env:
        agent = f"test-f118-mgr-{uuid.uuid4().hex[:8]}"
        agents.append(agent)
        settings = Settings(
            _env_file=None,
            agent_id=agent,
            outside_judge_mode=mode,
            outside_judge_settings_enabled=True,
            **over,
        )
        if key is not None:
            row = {**(row or {}), **_sealed(agent, key)}
        if row:
            async with db.session() as session:
                await second_settings.upsert_row(session, agent, row, "owner")
                await session.commit()
        primary = Primary()
        judges = JudgeSet(primary, None) if mode == "shadow" else None
        database = FlakyDatabase(db)
        manager = SecondJudgeManager(
            database,
            settings,
            judges,
            transport=transport or httpx.MockTransport(_no_answer),
            key_file=FixedKeyFile(key_file),
            clock=clock,
            monotonic=monotonic,
        )
        env = Env(manager, judges, primary, database)
        if stand_ins:

            def make_service(config: Any) -> StandIn:
                service = StandIn(config, env.events)
                env.built.append(service)
                return service

            monkeypatch.setattr(manager, "_make_service", make_service)
        if load:
            await manager.load()
        envs.append(env)
        return env

    yield build
    for env in envs:
        await env.manager.close()
        if env.judges is not None:
            for service in env.built:
                service.release.set()
            second = env.judges.replace_second(None)
            if second is not None:
                await second.close()
            # The first judge: the same object, never started, closed or replaced, still taking decisions.
            assert env.judges.primary is env.primary
            assert env.primary.calls == []
            assert env.judges.submit_decision(
                uuid.uuid4(), grade="success", note=None, grader="agent", resolved_at=datetime.now(UTC)
            )
    async with db.session() as session:
        for agent in agents:
            await session.execute(
                text("DELETE FROM nous_system.judge_second_settings WHERE agent_id = :a"), {"a": agent}
            )
            await session.execute(text("DELETE FROM nous_system.judge_verdicts WHERE agent_id = :a"), {"a": agent})
        await session.commit()


def _no_answer(request: httpx.Request) -> httpx.Response:
    raise AssertionError("the second judge was given no decision in this test")


def _sealed(agent: str, key: str = KEY) -> dict[str, Any]:
    return {**seal_key(BOX, agent, key), "key_set_at": datetime(2026, 11, 20, tzinfo=UTC)}


async def _keyed(make: Any, key: str = KEY, **over: Any) -> Env:
    """A manager whose row holds ``key`` sealed (and ``over``'s stored values)."""
    return await make(key=key, row=over)


# ---- loading ---------------------------------------------------------------------------------------------------------


async def test_with_nothing_stored_and_no_server_values_no_second_judge_runs(make):
    env = await make()
    assert env.manager.loaded and env.second is None and env.built == []
    state = env.manager.state()
    assert state["on"] == {"value": False, "source": "server"}
    assert state["key"] == {"source": "none", "set_at": None, "problem": None, "server_has_key": False}
    assert state["running"] is False and state["locked"] is None and state["can_store_key"] is True


async def test_with_the_server_values_alone_the_same_second_judge_as_the_environments_runs(make):
    env = await make(stand_ins=False, outside_judge_second="jev", TYPESAFE_API_KEY=ENV_KEY, outside_judge_daily_calls=9)
    second = env.second
    assert isinstance(second, OutsideJudge) and isinstance(second.judge, JevJudge)
    assert (second.judge._key, second.judge.model, second._timeout, second.daily_calls) == (ENV_KEY, MODEL, 10.0, 9)
    assert second.runtime()["worker_alive"] is True


async def test_a_stored_switch_and_key_run_the_second_judge_with_the_key_masked(make):
    env = await _keyed(make, enabled=True, daily_calls=5, model="jev-1.15.0")
    [service] = env.built
    assert env.second is service and service.started
    assert (service.config.key, service.config.model, service.config.daily_calls) == (KEY, "jev-1.15.0", 5)
    assert KEY not in redact_output(f"the key {KEY}")
    state = env.manager.state()
    assert state["key"]["source"] == "stored" and state["key"]["set_at"].startswith("2026-11-20")
    assert KEY not in repr(state) and KEY[-6:] not in repr(state)


async def test_a_stored_key_that_does_not_open_is_reported_and_not_replaced_by_the_servers(make):
    env = await make(load=False, TYPESAFE_API_KEY=ENV_KEY)
    other = SecretBox(b"\x44" * 32)
    async with env.database.real.session() as session:
        sealed = {**seal_key(other, env.manager._agent_id, KEY), "key_set_at": datetime.now(UTC), "enabled": True}
        await second_settings.upsert_row(session, env.manager._agent_id, sealed, "owner")
        await session.commit()
    await env.manager.load()
    assert env.second is None
    key = env.manager.state()["key"]
    assert (key["source"], key["problem"], key["server_has_key"]) == ("stored", "stored with another key", True)


async def test_a_load_that_fails_leaves_the_second_off_and_the_first_untouched_and_the_retry_applies(make, monkeypatch):
    waits: list[float] = []
    release = asyncio.Event()

    async def sleep(seconds: float) -> None:
        waits.append(seconds)
        await release.wait()

    monkeypatch.setattr(second_settings, "_sleep", sleep)
    env = await make(key=KEY, row={"enabled": True}, load=False)
    env.database.failing_reads = 2  # the load's read and the first read again fail
    await env.manager.load()
    assert env.manager.loaded is False and env.second is None and env.built == []
    assert env.manager.state()["loaded"] is False
    assert (await env.manager.patch({"on": False}, confirm=False, actor="owner")).status == 503
    assert (await env.manager.put_key(KEY, confirm=True, actor="owner")).body["error"] == NOT_LOADED
    assert (await env.manager.remove_key(confirm=True, actor="owner")).status == 503
    release.set()
    await asyncio.wait_for(env.manager._retry, 5)
    assert waits == [30.0, 60.0]
    assert env.manager.loaded is True
    assert env.second is not None and env.second.config.key == KEY


# ---- the confirmation ----------------------------------------------------------------------------------------------


async def _starts(env: Env, call: Any) -> None:
    """``call(confirm)`` starts the sending: refused without the confirmation, nothing written, then accepted."""
    before = await env.row()
    refused = await call(False)
    assert refused.status == 409 and refused.body["needs_confirm"] is True
    assert refused.body["error"].startswith("Send a copy of each decision Nous resolves to TypeSafe")
    assert await env.row() == before and env.second is None
    accepted = await call(True)
    assert accepted.status == 200 and accepted.body["changed"] is True and env.second is not None


async def test_switching_on_with_a_key_needs_the_confirmation(make):
    env = await _keyed(make, enabled=False)
    await _starts(env, lambda confirm: env.manager.patch({"on": True}, confirm=confirm, actor="owner"))


async def test_saving_a_key_while_on_needs_the_confirmation(make):
    env = await make(row={"enabled": True})
    await _starts(env, lambda confirm: env.manager.put_key(KEY, confirm=confirm, actor="owner"))


async def test_raising_the_limit_from_0_needs_the_confirmation(make):
    env = await _keyed(make, enabled=True, daily_calls=0)
    await _starts(env, lambda confirm: env.manager.patch({"daily_calls": 3}, confirm=confirm, actor="owner"))
    assert "At most 3 copies a day." in env.manager.state()["confirm_text"]


async def test_returning_the_limit_to_the_servers_from_0_needs_the_confirmation(make):
    env = await _keyed(make, enabled=True, daily_calls=0)
    await _starts(env, lambda confirm: env.manager.patch({"daily_calls": None}, confirm=confirm, actor="owner"))


async def test_removing_a_key_that_does_not_open_hands_over_to_the_servers_key_and_needs_the_confirmation(make):
    env = await make(load=False, TYPESAFE_API_KEY=ENV_KEY)
    async with env.database.real.session() as session:
        sealed = {**seal_key(SecretBox(b"\x55" * 32), env.manager._agent_id, KEY), "key_set_at": datetime.now(UTC)}
        await second_settings.upsert_row(session, env.manager._agent_id, {**sealed, "enabled": True}, "owner")
        await session.commit()
    await env.manager.load()
    await _starts(env, lambda confirm: env.manager.remove_key(confirm=confirm, actor="owner"))
    assert env.second.config.key == ENV_KEY
    assert env.manager.state()["key"]["source"] == "server"


async def test_the_sentence_names_the_limit_after_the_change(make):
    env = await _keyed(make, enabled=False, daily_calls=4)
    refused = await env.manager.patch({"on": True, "daily_calls": 12}, confirm=False, actor="owner")
    assert "At most 12 copies a day." in refused.body["error"]


@pytest.mark.parametrize(
    ("row", "change"),
    [
        ({"enabled": True}, {"on": False}),  # switching off
        ({"enabled": True}, {"model": "jev-1.15.0"}),  # a model change while sending
        ({"enabled": True}, {"daily_calls": 50}),  # a limit change while sending
        ({"enabled": True}, {"daily_calls": 0}),  # stopping by the limit
        ({"enabled": False}, {"model": "jev-1.15.0"}),  # a change while off
        ({"enabled": False}, {"daily_calls": 9}),
    ],
)
async def test_a_change_that_does_not_start_the_sending_needs_no_confirmation(make, row, change):
    env = await _keyed(make, **row)
    answer = await env.manager.patch(change, confirm=False, actor="owner")
    assert answer.status == 200 and answer.body["changed"] is True


async def test_saving_a_key_while_off_or_removing_a_key_that_stops_the_sending_needs_no_confirmation(make):
    env = await make(row={"enabled": False})
    assert (await env.manager.put_key(KEY, confirm=False, actor="owner")).status == 200
    on = await env.manager.patch({"on": True}, confirm=True, actor="owner")
    assert on.status == 200 and env.second is not None
    removed = await env.manager.remove_key(confirm=False, actor="owner")
    assert removed.status == 200 and env.second is None
    assert removed.body["key"]["source"] == "none"


async def test_switching_on_with_no_key_is_saved_and_runs_nothing(make):
    env = await make()
    answer = await env.manager.patch({"on": True}, confirm=False, actor="owner")
    assert answer.status == 200 and env.second is None
    assert answer.body["on"] == {"value": True, "source": "stored"}


# ---- the values ------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "change",
    [
        {},
        {"model": "jev-latest"},
        {"model": "jev-1.14.0\n"},
        {"model": "jev-1.14"},
        {"model": "jev-" + "1" * 40 + ".0.0"},
        {"model": 14},
        {"daily_calls": -1},
        {"daily_calls": 101},
        {"daily_calls": True},
        {"daily_calls": 50.0},
        {"daily_calls": "5"},
        {"on": None},
        {"on": "yes"},
        {"other": 1},
    ],
)
async def test_a_value_outside_its_rule_is_refused_and_nothing_is_written(make, change):
    env = await make(outside_judge_daily_calls=100)
    answer = await env.manager.patch(change, confirm=True, actor="owner")
    assert answer.status == 400
    assert await env.row() is None


async def test_the_limit_may_be_set_up_to_the_servers_number(make):
    env = await make(outside_judge_daily_calls=40)
    assert (await env.manager.patch({"daily_calls": 40}, confirm=True, actor="owner")).status == 200
    refused = await env.manager.patch({"daily_calls": 41}, confirm=True, actor="owner")
    assert refused.status == 400 and refused.body["error"].startswith("daily_calls must be a whole number from 0 to 40")


@pytest.mark.parametrize(
    "key", ["short-key-15chr", "x" * 201, "has a space in it ok", "line\nbreak-key-123456", "ünïcödé-key-1234567", ""]
)
async def test_a_key_outside_its_rule_is_refused_without_the_value(make, key):
    env = await make()
    answer = await env.manager.put_key(key, confirm=True, actor="owner")
    assert (answer.status, answer.body) == (400, {"error": second_settings.KEY_RULE})
    assert await env.row() is None


async def test_a_key_is_saved_with_the_spaces_around_it_removed(make):
    env = await make()
    assert (await env.manager.put_key(f"  {KEY}\n", confirm=True, actor="owner")).status == 200
    assert second_settings.open_key(BOX, env.manager._agent_id, await env.row()) == (KEY, None)


async def test_without_a_key_file_a_key_cannot_be_saved_but_the_other_values_can(make):
    env = await make(key_file=None)
    answer = await env.manager.put_key(KEY, confirm=True, actor="owner")
    assert (answer.status, answer.body["error"]) == (409, NO_KEY_FILE_TEXT)
    assert (await env.manager.patch({"model": "jev-1.15.0"}, confirm=False, actor="owner")).status == 200
    assert env.manager.state()["can_store_key"] is False


async def test_a_repeated_save_writes_and_rebuilds_nothing(make):
    env = await _keyed(make, enabled=True, daily_calls=5)
    [service] = env.built
    before = await env.row()
    for answer in (
        await env.manager.patch({"on": True, "daily_calls": 5}, confirm=False, actor="owner"),
        await env.manager.put_key(KEY, confirm=False, actor="owner"),
    ):
        assert answer.status == 200 and answer.body["changed"] is False
    assert env.built == [service] and env.second is service and not service.closed
    assert await env.row() == before


async def test_a_patch_keeps_the_stored_key(make):
    env = await _keyed(make, enabled=True)
    await env.manager.patch({"model": "jev-1.16.0"}, confirm=False, actor="proxy")
    row = await env.row()
    assert second_settings.open_key(BOX, env.manager._agent_id, row) == (KEY, None)
    assert (row.model, row.updated_by) == ("jev-1.16.0", "proxy")
    assert env.second.config.key == KEY and env.second.config.model == "jev-1.16.0"


async def test_a_stored_limit_above_the_server_is_used_lowered(make):
    env = await make(load=False, outside_judge_daily_calls=10)
    async with env.database.real.session() as session:
        await second_settings.upsert_row(session, env.manager._agent_id, {"daily_calls": 300}, "owner")
        await session.commit()
    await env.manager.load()
    assert env.manager.state()["daily_calls"] == {"value": 10, "most": 10, "source": "lowered"}


# ---- applying ------------------------------------------------------------------------------------------------------


async def test_the_old_second_judge_has_closed_before_the_change_returns(make):
    env = await _keyed(make, enabled=True)
    [old] = env.built
    answer = await env.manager.patch({"on": False}, confirm=False, actor="owner")
    assert answer.status == 200 and answer.body["running"] is False
    assert old.closed and env.second is None


async def test_a_decision_submitted_during_a_swap_lands_on_exactly_one_second_judge(make):
    env = await _keyed(make, enabled=True, model="jev-1.14.0")
    [old] = env.built
    old.release.clear()  # the old judge's close waits

    def submit() -> uuid.UUID:
        decision = uuid.uuid4()
        env.judges.submit_decision(decision, grade="success", note=None, grader="agent", resolved_at=datetime.now(UTC))
        return decision

    change = asyncio.create_task(env.manager.patch({"model": "jev-1.15.0"}, confirm=False, actor="owner"))
    before = []
    while not old.closing.is_set():  # until the swap is done and the old judge is closing
        before.append(submit())
        await asyncio.sleep(0.001)
    after = [submit() for _ in range(5)]
    new = env.second
    assert new is not old and new.config.model == "jev-1.15.0" and not old.closed
    old.release.set()
    assert (await change).status == 200
    assert old.submitted == before and new.submitted == after
    assert env.primary.submitted == before + after


async def test_a_real_second_judge_drops_its_queued_decisions_when_it_is_replaced(make):
    env = await make(stand_ins=False, key=KEY, row={"enabled": True})
    old = env.second
    assert isinstance(old, OutsideJudge)
    queued = env.judges.submit_decision(
        uuid.uuid4(), grade="success", note=None, grader="agent", resolved_at=datetime.now(UTC)
    )
    assert queued and old.idle is False
    answer = await env.manager.patch({"model": "jev-1.15.0"}, confirm=False, actor="owner")
    assert answer.status == 200
    assert old.runtime()["worker_alive"] is False
    assert env.second is not old and env.second.idle is True and env.second.judge.model == "jev-1.15.0"
    async with env.database.real.session() as session:
        rows = await session.scalar(
            text("SELECT count(*) FROM nous_system.judge_verdicts WHERE agent_id = :a"), {"a": env.manager._agent_id}
        )
    assert rows == 0


async def test_a_second_judge_that_fails_to_start_leaves_it_off_with_the_row_saved_and_a_later_write_tries_again(
    make, monkeypatch, caplog
):
    env = await _keyed(make, enabled=False)

    def broken(config: Any) -> Any:
        raise RuntimeError(f"cannot build {config.key}")

    good = env.manager._make_service
    monkeypatch.setattr(env.manager, "_make_service", broken)
    with caplog.at_level(logging.DEBUG):
        answer = await env.manager.patch({"on": True}, confirm=True, actor="owner")
    assert answer.status == 200 and answer.body["running"] is False and env.second is None
    assert (await env.row()).enabled is True
    assert any("RuntimeError" in record.getMessage() for record in caplog.records)
    assert KEY not in caplog.text
    monkeypatch.setattr(env.manager, "_make_service", good)
    again = await env.manager.patch({"model": "jev-1.15.0"}, confirm=False, actor="owner")
    assert again.status == 200 and again.body["running"] is True


async def test_two_writes_run_one_after_the_other(make):
    env = await _keyed(make, enabled=True)
    [old] = env.built
    old.release.clear()
    first = asyncio.create_task(env.manager.patch({"model": "jev-1.15.0"}, confirm=False, actor="owner"))
    second = asyncio.create_task(env.manager.patch({"daily_calls": 3}, confirm=False, actor="owner"))
    await asyncio.wait_for(old.closing.wait(), 5)
    for _ in range(10):
        await asyncio.sleep(0)
    assert not first.done() and not second.done()
    old.release.set()
    answers = await asyncio.gather(first, second)
    assert [answer.status for answer in answers] == [200, 200]
    assert answers[1].body["model"]["value"] == "jev-1.15.0"  # the second saw the first's result
    running = env.second
    assert (running.config.model, running.config.daily_calls) == ("jev-1.15.0", 3)


async def test_a_caller_that_goes_away_does_not_stop_the_change(make):
    env = await _keyed(make, enabled=False)
    caller = asyncio.create_task(env.manager.patch({"on": True}, confirm=True, actor="owner"))
    await asyncio.sleep(0)
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    await asyncio.gather(*list(env.manager._tasks))
    assert (await env.row()).enabled is True and env.second is not None


async def test_close_waits_for_the_change_in_flight_and_later_writes_are_refused(make):
    env = await _keyed(make, enabled=True)
    [old] = env.built
    old.release.clear()
    change = asyncio.create_task(env.manager.patch({"on": False}, confirm=False, actor="owner"))
    await asyncio.wait_for(old.closing.wait(), 5)
    closing = asyncio.create_task(env.manager.close())
    for _ in range(10):
        await asyncio.sleep(0)
    assert not closing.done()
    old.release.set()
    await closing
    assert (await change).status == 200 and old.closed
    for answer in (
        await env.manager.patch({"on": True}, confirm=True, actor="owner"),
        await env.manager.put_key(KEY_2, confirm=True, actor="owner"),
        await env.manager.remove_key(confirm=True, actor="owner"),
        await env.manager.check(),
    ):
        assert (answer.status, answer.body["error"]) == (503, SHUTTING_DOWN)


async def test_no_change_touches_the_first_judge(make):
    env = await _keyed(make, enabled=False)
    first = env.judges.primary
    for answer in (
        await env.manager.patch({"on": True}, confirm=True, actor="owner"),
        await env.manager.patch({"model": "jev-1.15.0", "daily_calls": 3}, confirm=False, actor="owner"),
        await env.manager.put_key(KEY_2, confirm=False, actor="owner"),
        await env.manager.patch({"on": False}, confirm=False, actor="owner"),
        await env.manager.remove_key(confirm=False, actor="owner"),
    ):
        assert answer.status == 200
        assert env.judges.primary is first and first.calls == []
        assert env.judges.judge is first.judge
    decision = uuid.uuid4()
    assert env.judges.submit_decision(
        decision, grade="success", note=None, grader="agent", resolved_at=datetime.now(UTC)
    )
    assert first.submitted[-1] == decision


# ---- the key and the redaction ------------------------------------------------------------------------------------


async def test_a_new_key_is_registered_before_it_is_written_and_the_old_one_forgotten_after_the_old_judge_closed(
    make, monkeypatch
):
    env = await _keyed(make, enabled=True)
    events = env.events
    real_register, real_forget, real_upsert = (
        second_settings.register_own_secret,
        second_settings.forget_own_secret,
        second_settings.upsert_row,
    )

    def register(value: str) -> None:
        events.append(f"register {value}")
        real_register(value)

    def forget(value: str) -> None:
        events.append(f"forget {value}")
        real_forget(value)

    async def upsert(*args: Any, **kwargs: Any) -> Any:
        events.append("write")
        return await real_upsert(*args, **kwargs)

    monkeypatch.setattr(second_settings, "register_own_secret", register)
    monkeypatch.setattr(second_settings, "forget_own_secret", forget)
    monkeypatch.setattr(second_settings, "upsert_row", upsert)
    answer = await env.manager.put_key(KEY_2, confirm=False, actor="owner")
    assert answer.status == 200
    assert events == [f"register {KEY_2}", "write", f"closed {KEY}", f"forget {KEY}"]
    assert KEY_2 not in redact_output(KEY_2) and redact_output(KEY) == KEY


async def test_switching_off_keeps_the_saved_key_masked_and_removing_it_forgets_it(make):
    env = await _keyed(make, enabled=True)
    await env.manager.patch({"on": False}, confirm=False, actor="owner")
    assert KEY not in redact_output(KEY)  # still held by the settings (the check uses it)
    await env.manager.remove_key(confirm=False, actor="owner")
    assert redact_output(KEY) == KEY


async def test_a_write_that_fails_changes_nothing_and_forgets_the_new_key(make):
    env = await _keyed(make, enabled=True)
    [service] = env.built
    before = await env.row()
    env.database.fail_write = True
    answer = await env.manager.put_key(KEY_2, confirm=True, actor="owner")
    assert answer.status == 500 and KEY_2 not in repr(answer.body)
    assert await env.row() == before
    assert env.second is service and not service.closed
    assert redact_output(KEY_2) == KEY_2 and KEY not in redact_output(KEY)


async def test_a_commit_that_failed_before_landing_changes_nothing(make):
    env = await _keyed(make, enabled=True)
    before = await env.row()
    env.database.fail_commit = "before"
    answer = await env.manager.put_key(KEY_2, confirm=True, actor="owner")
    assert answer.status == 500
    assert await env.row() == before and env.second.config.key == KEY
    assert redact_output(KEY_2) == KEY_2


async def test_a_commit_whose_outcome_was_lost_is_read_back_and_applied(make):
    env = await _keyed(make, enabled=True)
    env.database.fail_commit = "after"
    answer = await env.manager.put_key(KEY_2, confirm=True, actor="owner")
    assert answer.status == 200 and answer.body["changed"] is True
    assert env.second.config.key == KEY_2
    assert redact_output(KEY) == KEY and KEY_2 not in redact_output(KEY_2)


async def test_a_lost_commit_that_cannot_be_read_back_stops_the_second_judge_and_reads_again(make, monkeypatch):
    release = asyncio.Event()

    async def sleep(seconds: float) -> None:
        await release.wait()

    monkeypatch.setattr(second_settings, "_sleep", sleep)
    env = await _keyed(make, enabled=True)
    [old] = env.built
    env.database.fail_commit = "after"
    env.database.failing_reads = 1
    answer = await env.manager.put_key(KEY_2, confirm=True, actor="owner")
    assert answer.status == 500
    assert env.manager.loaded is False and env.second is None and old.closed
    assert redact_output(KEY) == KEY and redact_output(KEY_2) == KEY_2
    env.database.fail_commit = None
    release.set()
    await asyncio.wait_for(env.manager._retry, 5)
    assert env.manager.loaded is True and env.second.config.key == KEY_2


async def test_no_write_puts_a_key_in_the_environment_or_a_log_record(make, caplog):
    caplog.set_level(logging.DEBUG)
    before = dict(os.environ)
    env = await make(row={"enabled": True})
    await env.manager.put_key(KEY, confirm=False, actor="owner")
    await env.manager.put_key(KEY, confirm=True, actor="owner")
    await env.manager.put_key(KEY_2, confirm=True, actor="owner")
    await env.manager.patch({"daily_calls": 2}, confirm=False, actor="owner")
    env.database.fail_write = True
    await env.manager.put_key(KEY, confirm=True, actor="owner")
    env.database.fail_write = False
    await env.manager.remove_key(confirm=False, actor="owner")
    assert dict(os.environ) == before
    for record in caplog.records:
        logged = record.getMessage() + repr(record.args) + repr(record.exc_info) + repr(record.exc_text)
        for key in (KEY, KEY_2):
            assert key not in logged and key[-8:] not in logged
    assert any("second judge settings changed (key) by owner" in record.getMessage() for record in caplog.records)


# ---- the mode off ----------------------------------------------------------------------------------------------------


async def test_with_the_mode_off_only_the_removal_of_a_stored_key_is_allowed_and_no_key_is_opened(make, monkeypatch):
    opened: list[Any] = []
    monkeypatch.setattr(second_settings, "open_key", lambda *args: opened.append(args) or (None, None))
    env = await make(mode="off", key=KEY, row={"enabled": True})
    state = env.manager.state()
    assert (state["locked"], state["running"], state["key"]["source"]) == ("off", False, "stored")
    for answer in (
        await env.manager.patch({"on": False}, confirm=True, actor="owner"),
        await env.manager.put_key(KEY_2, confirm=True, actor="owner"),
        await env.manager.check(),
    ):
        assert (answer.status, answer.body["error"]) == (409, MODE_OFF)
    removed = await env.manager.remove_key(confirm=False, actor="owner")
    assert removed.status == 200 and removed.body["key"]["source"] == "none"
    assert not (await env.row()).has_key
    assert opened == [] and redact_output(KEY) == KEY


# ---- the check -------------------------------------------------------------------------------------------------------


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 11, 23, 12, 0, tzinfo=UTC)
        self.mono = 1000.0

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)
        self.mono += seconds


def _provider(answers: list[Any]) -> tuple[httpx.MockTransport, list[httpx.Request]]:
    sent: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        answer = answers.pop(0) if answers else (200, {"model": MODEL})
        if isinstance(answer, Exception):
            raise answer
        status, body = answer
        return httpx.Response(status, json=body)

    return httpx.MockTransport(handle), sent


async def _checking(make, answers: list[Any], **over: Any) -> tuple[Env, Clock, list[httpx.Request]]:
    clock = Clock()
    transport, sent = _provider(answers)
    env = await make(
        transport=transport,
        clock=lambda: clock.now,
        monotonic=lambda: clock.mono,
        TYPESAFE_API_KEY=ENV_KEY,
        **over,
    )
    return env, clock, sent


async def test_the_check_sends_one_fixed_record_with_the_key_in_effect_and_writes_nothing(make):
    env, _clock, sent = await _checking(make, [(200, {"model": MODEL, "answers": {}})])
    answer = await env.manager.check()
    assert (answer.status, answer.body) == (200, {"result": "ok"})
    [request] = sent
    assert request.headers["authorization"] == f"Bearer {ENV_KEY}"
    body = json.loads(request.content)
    assert body == {
        "model": MODEL,
        "state": {"note": "connection check"},
        "questions": {
            "check": {
                "type": "choice",
                "instructions": "Is the state a connection check?",
                "criteria": {"yes": "The state says it is a connection check.", "no": "The state does not say so."},
            }
        },
    }
    async with env.database.real.session() as session:
        rows = await session.scalar(
            text("SELECT count(*) FROM nous_system.judge_verdicts WHERE agent_id = :a"), {"a": env.manager._agent_id}
        )
    assert rows == 0 and env.second is None


async def test_the_check_uses_a_stored_key_and_model_while_the_second_judge_is_off(make):
    clock = Clock()
    transport, sent = _provider([(200, {"model": "jev-1.15.0"})])
    env = await make(
        transport=transport, clock=lambda: clock.now, monotonic=lambda: clock.mono, key=KEY, row={"model": "jev-1.15.0"}
    )
    assert (await env.manager.check()).body == {"result": "ok"}
    assert sent[0].headers["authorization"] == f"Bearer {KEY}"


async def test_the_check_is_bounded_one_at_a_time_10_s_apart_20_a_day(make):
    env, clock, sent = await _checking(make, [])
    assert (await env.manager.check()).status == 200
    clock.advance(9.9)
    limited = await env.manager.check()
    assert (limited.status, limited.body["error"]) == (429, CHECK_LIMIT)
    for _ in range(19):
        clock.advance(10)
        assert (await env.manager.check()).status == 200
    clock.advance(10)
    assert (await env.manager.check()).status == 429  # 20 today
    assert len(sent) == 20
    clock.advance(24 * 3600)  # the next UTC day
    assert (await env.manager.check()).status == 200


async def test_a_second_check_while_one_runs_is_refused(make):
    release = asyncio.Event()

    async def slow(request: httpx.Request) -> httpx.Response:
        await release.wait()
        return httpx.Response(200, json={"model": MODEL})

    clock = Clock()
    env = await make(
        transport=httpx.MockTransport(slow),
        clock=lambda: clock.now,
        monotonic=lambda: clock.mono,
        TYPESAFE_API_KEY=ENV_KEY,
    )
    first = asyncio.create_task(env.manager.check())
    for _ in range(10):
        await asyncio.sleep(0)
    clock.advance(60)
    assert (await env.manager.check()).status == 429
    release.set()
    assert (await first).body == {"result": "ok"}


async def test_the_check_without_a_usable_key_is_refused(make):
    env = await make()
    answer = await env.manager.check()
    assert (answer.status, answer.body["error"]) == (409, NO_KEY_TO_CHECK)


async def test_the_check_answers_one_word_and_logs_no_key(make, caplog):
    caplog.set_level(logging.DEBUG)
    env, clock, _sent = await _checking(
        make,
        [
            (401, {"error": f"bad key {ENV_KEY}"}),
            (200, {"model": "jev-9.9.9", "echo": ENV_KEY}),
            (503, {"error": ENV_KEY}),
            RuntimeError(ENV_KEY),
            httpx.ConnectError(f"no route {ENV_KEY}"),
        ],
    )
    results = []
    for _ in range(5):
        answer = await env.manager.check()
        results.append(answer.body)
        clock.advance(11)
    assert results == [
        {"result": "refused"},
        {"result": "refused"},
        {"result": "unreachable"},
        {"result": "unreachable"},
        {"result": "unreachable"},
    ]
    for record in caplog.records:
        assert ENV_KEY not in record.getMessage() + repr(record.args) + repr(record.exc_info)
