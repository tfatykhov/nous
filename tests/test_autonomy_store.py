"""F115: the store of the owner's autonomy settings (real Postgres): each change is written with its record in one
transaction and then applied in place; at start the stored values are checked again and applied in one pass."""

from __future__ import annotations

import asyncio
import logging
import uuid
from contextlib import asynccontextmanager

import pytest
import pytest_asyncio
from sqlalchemy import delete, select, text

from nous.autonomy import store as store_module
from nous.autonomy import tool_guard
from nous.autonomy.registry import LIMITS
from nous.autonomy.store import AutonomyRefused, AutonomyStore, tool_key
from nous.config import Settings
from nous.storage.models import AutonomySetting, AutonomySettingLog, ToolGuardRow

pytestmark = pytest.mark.postgres_only


@pytest.fixture
def agent_id() -> str:
    return f"test-autonomy-{uuid.uuid4().hex[:12]}"


def _settings(agent_id: str, **over) -> Settings:
    values = {"autonomy_settings_enabled": True, "tool_guard_enabled": True, **over}
    return Settings(_env_file=None, agent_id=agent_id, **values)


@pytest_asyncio.fixture
async def cleanup(db, agent_id: str):
    yield
    async with db.session() as session:
        for model in (AutonomySetting, AutonomySettingLog, ToolGuardRow):
            await session.execute(delete(model).where(model.agent_id == agent_id))
        await session.commit()


async def _insert(db, agent_id: str, key: str, value) -> None:
    async with db.session() as session:
        session.add(AutonomySetting(agent_id=agent_id, key=key, value=value, updated_by="owner"))
        await session.commit()


async def _rows(db, agent_id: str) -> dict:
    async with db.session() as session:
        rows = (await session.execute(select(AutonomySetting).where(AutonomySetting.agent_id == agent_id))).scalars()
        return {row.key: row.value for row in rows}


async def _log(db, agent_id: str) -> list[tuple]:
    async with db.session() as session:
        rows = (
            await session.execute(
                select(AutonomySettingLog)
                .where(AutonomySettingLog.agent_id == agent_id)
                .order_by(AutonomySettingLog.id)
            )
        ).scalars()
        return [(row.key, row.old_value, row.new_value, row.actor) for row in rows]


class _NoDatabase:
    def session(self):
        raise AssertionError("the store opened a session")


# ---- start ----------------------------------------------------------------------------------------------------


async def test_with_the_flag_off_load_reads_nothing_and_changes_nothing(agent_id):
    settings = _settings(agent_id, autonomy_settings_enabled=False, autonomy_ceilings="max_turns=5")
    before = settings.model_dump()
    store = AutonomyStore(_NoDatabase(), settings)
    await store.load()
    assert settings.model_dump() == before
    assert tool_guard.current() is None


async def test_with_no_row_load_leaves_every_limit_the_hosts(db, agent_id, cleanup):
    settings = _settings(agent_id, max_turns=600, subtask_tool_call_limit=50)
    before = settings.model_dump()
    store = AutonomyStore(db, settings)
    await store.load()
    assert settings.model_dump() == before
    assert {limit.key: store.source(limit.key) for limit in LIMITS} == {limit.key: "host" for limit in LIMITS}
    guard = tool_guard.current()
    assert guard is not None and guard.denied == frozenset() and guard.locked == frozenset()


async def test_load_applies_the_stored_values_and_skips_each_bad_one(db, agent_id, cleanup, caplog):
    caplog.set_level(logging.WARNING)
    for key, value in [
        ("subtask_tool_call_limit", 80),  # good
        ("intention_root_ttl_hours", 24),  # good, a float field
        ("max_turns", 4),  # below the range
        ("continuation_max_depth", "3"),  # not a number
        ("continuation_stall_limit", 2.0),  # not a whole number
        ("continuation_max_turns_per_root", True),  # a boolean
        ("goals_queue_headroom", 1),  # not a limit the owner may change
        ("continuation_max_spawns_per_root", 25),  # above the ceiling below
    ]:
        await _insert(db, agent_id, key, value)
    settings = _settings(agent_id, autonomy_ceilings="continuation_max_spawns_per_root=20")
    store = AutonomyStore(db, settings)
    await store.load()
    assert settings.subtask_tool_call_limit == 80
    assert settings.intention_root_ttl_hours == 24.0 and isinstance(settings.intention_root_ttl_hours, float)
    assert (settings.max_turns, settings.continuation_max_depth, settings.continuation_stall_limit) == (10, 3, 2)
    assert (settings.continuation_max_turns_per_root, settings.continuation_max_spawns_per_root) == (8, 12)
    assert store.owner == {"subtask_tool_call_limit": 80, "intention_root_ttl_hours": 24}
    skipped = [r.getMessage() for r in caplog.records if r.name == store_module.__name__]
    assert len(skipped) == 6
    for key in (
        "max_turns",
        "continuation_max_depth",
        "continuation_stall_limit",
        "continuation_max_turns_per_root",
        "continuation_max_spawns_per_root",
        "goals_queue_headroom",
    ):
        assert any(key in message for message in skipped), key


async def test_load_skips_both_time_limits_when_they_break_the_cross_rule(db, agent_id, cleanup):
    await _insert(db, agent_id, "subtask_default_timeout", 3000)
    await _insert(db, agent_id, "subtask_max_timeout", 2000)
    settings = _settings(agent_id)
    store = AutonomyStore(db, settings)
    await store.load()
    assert (settings.subtask_default_timeout, settings.subtask_max_timeout) == (600, 3600)
    assert store.owner == {}


async def test_rows_are_the_agents_own(db, agent_id, cleanup):
    other = f"{agent_id}-other"
    await _insert(db, other, "subtask_tool_call_limit", 99)
    try:
        settings = _settings(agent_id)
        await AutonomyStore(db, settings).load()
        assert settings.subtask_tool_call_limit == 20
    finally:
        async with db.session() as session:
            await session.execute(delete(AutonomySetting).where(AutonomySetting.agent_id == other))
            await session.commit()


async def test_an_env_value_above_its_ceiling_is_lowered_to_it_with_one_warning(db, agent_id, cleanup, caplog):
    caplog.set_level(logging.WARNING)
    settings = _settings(agent_id, subtask_tool_call_limit=80, autonomy_ceilings="subtask_tool_call_limit=60")
    store = AutonomyStore(db, settings)
    assert settings.subtask_tool_call_limit == 80  # nothing is applied before load
    await store.load()
    assert settings.subtask_tool_call_limit == 60
    assert store.source("subtask_tool_call_limit") == "clamped"
    assert (store.host["subtask_tool_call_limit"], store.baseline["subtask_tool_call_limit"]) == (80, 60)
    warnings = [r for r in caplog.records if "subtask_tool_call_limit" in r.getMessage()]
    assert len(warnings) == 1
    with pytest.raises(AutonomyRefused) as refused:
        await store.set_limit("subtask_tool_call_limit", 61, "owner")
    assert refused.value.status == 409 and "60" in str(refused.value)
    await store.set_limit("subtask_tool_call_limit", 40, "owner")
    assert await store.clear_limit("subtask_tool_call_limit", "owner") is True
    assert settings.subtask_tool_call_limit == 60  # back to the host's value as the ceiling allows it


# ---- changes ----------------------------------------------------------------------------------------------------


async def test_a_change_is_written_with_its_record_and_applied(db, agent_id, cleanup):
    settings = _settings(agent_id)
    store = AutonomyStore(db, settings)
    await store.load()
    await store.set_limit("subtask_tool_call_limit", 50, "proxy")
    await store.set_limit("subtask_tool_call_limit", 70, "owner")
    assert settings.subtask_tool_call_limit == 70 and store.source("subtask_tool_call_limit") == "owner"
    assert await _rows(db, agent_id) == {"subtask_tool_call_limit": 70}
    assert await _log(db, agent_id) == [
        ("subtask_tool_call_limit", None, 50, "proxy"),
        ("subtask_tool_call_limit", 50, 70, "owner"),
    ]
    assert await store.clear_limit("subtask_tool_call_limit", "owner") is True
    assert settings.subtask_tool_call_limit == 20 and store.source("subtask_tool_call_limit") == "host"
    assert await _rows(db, agent_id) == {}
    assert (await _log(db, agent_id))[-1] == ("subtask_tool_call_limit", 70, None, "owner")
    # Clearing what is not set writes nothing.
    assert await store.clear_limit("subtask_tool_call_limit", "owner") is False
    assert len(await _log(db, agent_id)) == 3
    changes = await store.changes()
    assert [c.new for c in changes] == [None, 70, 50]
    last = await store.last_changes(["subtask_tool_call_limit", "max_turns"])
    assert set(last) == {"subtask_tool_call_limit"} and last["subtask_tool_call_limit"].old == 70


async def test_a_failed_record_leaves_no_row_and_no_change(db, agent_id, cleanup, monkeypatch):
    settings = _settings(agent_id)
    store = AutonomyStore(db, settings)
    await store.load()

    def broken_record(key, old, new, actor):
        # A record the table's CHECK refuses when the transaction commits, after the setting row was written.
        return AutonomySettingLog(agent_id=agent_id, key="k" * 301, old_value=old, new_value=new, actor=actor)

    monkeypatch.setattr(store, "_record", broken_record)
    with pytest.raises(Exception, match="check constraint"):  # the database's own error
        await store.set_limit("subtask_tool_call_limit", 50, "owner")
    assert settings.subtask_tool_call_limit == 20 and store.owner == {}
    assert await _rows(db, agent_id) == {}
    with pytest.raises(Exception, match="check constraint"):
        await store.set_tool("web_search", "denied", "owner")
    assert tool_guard.current().denied == frozenset()
    async with db.session() as session:
        assert (await session.execute(select(ToolGuardRow).where(ToolGuardRow.agent_id == agent_id))).first() is None


@pytest.mark.parametrize(
    ("key", "value", "status"),
    [
        ("max_turns", 4, 400),
        ("max_turns", 1001, 400),
        ("max_turns", 50.0, 400),
        ("max_turns", True, 400),
        ("max_turns", "50", 400),
        ("nope", 5, 404),
        ("goals_queue_headroom", 1, 404),
    ],
)
async def test_a_refused_change_writes_nothing(db, agent_id, cleanup, key, value, status):
    settings = _settings(agent_id)
    store = AutonomyStore(db, settings)
    await store.load()
    with pytest.raises(AutonomyRefused) as refused:
        await store.set_limit(key, value, "owner")
    assert refused.value.status == status
    assert await _rows(db, agent_id) == {} and await _log(db, agent_id) == []
    assert settings.max_turns == 10


async def test_the_cross_rule_refuses_a_default_above_the_largest(db, agent_id, cleanup):
    settings = _settings(agent_id)
    store = AutonomyStore(db, settings)
    await store.load()
    with pytest.raises(AutonomyRefused) as refused:
        await store.set_limit("subtask_default_timeout", 4000, "owner")  # the largest is 3,600
    assert refused.value.status == 409 and "3,600" in str(refused.value)
    await store.set_limit("subtask_max_timeout", 5000, "owner")
    await store.set_limit("subtask_default_timeout", 4000, "owner")
    # Returning the largest to the host's 3,600 would put the default above it.
    with pytest.raises(AutonomyRefused) as refused:
        await store.clear_limit("subtask_max_timeout", "owner")
    assert refused.value.status == 409
    assert (settings.subtask_default_timeout, settings.subtask_max_timeout) == (4000, 5000)


async def test_a_host_whose_own_time_limits_disagree_can_still_change_another_limit(db, agent_id, cleanup, caplog):
    caplog.set_level(logging.WARNING)
    settings = _settings(agent_id, subtask_default_timeout=4000, subtask_max_timeout=3600)
    store = AutonomyStore(db, settings)
    await store.load()
    assert not [r for r in caplog.records if "cross rule" in r.getMessage()]  # no stored value: nothing skipped
    await store.set_limit("max_turns", 50, "owner")
    with pytest.raises(AutonomyRefused) as refused:
        await store.set_limit("subtask_max_timeout", 3700, "owner")  # still below the host's default of 4,000
    assert refused.value.status == 409
    await store.set_limit("subtask_default_timeout", 3000, "owner")
    assert (settings.max_turns, settings.subtask_default_timeout) == (50, 3000)


async def test_two_changes_of_one_limit_at_once_both_land_in_order(db, agent_id, cleanup):
    settings = _settings(agent_id)
    store = AutonomyStore(db, settings)
    await store.load()
    await asyncio.gather(
        store.set_limit("max_turns", 100, "owner"),
        store.set_limit("max_turns", 200, "proxy"),
    )
    log = await _log(db, agent_id)
    assert len(log) == 2
    first, second = log
    assert first[1] is None and second[1] == first[2]  # each record's old value is the one before it
    assert settings.max_turns == second[2] == (await _rows(db, agent_id))["max_turns"]


async def test_two_changes_of_different_limits_at_once_both_land(db, agent_id, cleanup):
    settings = _settings(agent_id)
    store = AutonomyStore(db, settings)
    await store.load()
    await asyncio.gather(
        store.set_limit("max_turns", 100, "owner"),
        store.set_limit("continuation_max_depth", 4, "owner"),
    )
    assert (settings.max_turns, settings.continuation_max_depth) == (100, 4)
    assert await _rows(db, agent_id) == {"max_turns": 100, "continuation_max_depth": 4}


async def test_a_reader_sees_the_old_value_until_the_change_is_committed(db, agent_id, cleanup):
    settings = _settings(agent_id)
    gate, waiting = asyncio.Event(), asyncio.Event()

    class GatedDatabase:
        @asynccontextmanager
        async def session(self):
            async with db.session() as session:
                commit = session.commit

                async def held_commit():
                    waiting.set()
                    await gate.wait()
                    await commit()

                session.commit = held_commit
                yield session

    store = AutonomyStore(GatedDatabase(), settings)
    change = asyncio.create_task(store.set_limit("subtask_tool_call_limit", 120, "owner"))
    await asyncio.wait_for(waiting.wait(), 10)
    assert settings.subtask_tool_call_limit == 20  # a turn resolving the value now reads the host's
    gate.set()
    await change
    assert settings.subtask_tool_call_limit == 120


# ---- tools --------------------------------------------------------------------------------------------------------


async def test_a_tool_switched_off_is_held_at_once_and_back_on_cleared(db, agent_id, cleanup):
    settings = _settings(agent_id)
    store = AutonomyStore(db, settings)
    await store.load()
    await store.set_tool("web_search", "denied", "proxy")
    assert tool_guard.current().denies("scheduled", "schedule-1", "web_search")
    await store.set_tool("web_search", "allowed", "owner")
    assert not tool_guard.current().denies("scheduled", "schedule-1", "web_search")
    await store.set_tool("web_search", "denied", "owner")
    assert await store.clear_tool("web_search", "owner") is True
    assert not tool_guard.current().denies("scheduled", "schedule-1", "web_search")
    assert await store.clear_tool("web_search", "owner") is False
    key = tool_key("web_search")
    assert key == "tool_guard:background:web_search"
    assert await _log(db, agent_id) == [
        (key, None, "denied", "proxy"),
        (key, "denied", "allowed", "owner"),
        (key, "allowed", "denied", "owner"),
        (key, "denied", None, "owner"),
    ]


@pytest.mark.parametrize(
    ("tool", "setting", "status", "words"),
    [
        ("web_search", "ask", 400, "not available yet"),
        ("web_search", "maybe", 400, "not available yet"),
        ("resolve_intention", "denied", 400, "cannot be switched off"),
        ("submit_final_report", "denied", 400, "cannot be switched off"),
        ("bash", "denied", 409, "locked by the server"),
        ("bash", "allowed", 409, "locked by the server"),
    ],
)
async def test_a_refused_tool_setting_writes_nothing(db, agent_id, cleanup, tool, setting, status, words):
    settings = _settings(agent_id, autonomy_locked_tools="bash")
    store = AutonomyStore(db, settings)
    await store.load()
    with pytest.raises(AutonomyRefused) as refused:
        await store.set_tool(tool, setting, "owner")
    assert refused.value.status == status and words in str(refused.value)
    assert await _log(db, agent_id) == []
    if tool == "bash":
        with pytest.raises(AutonomyRefused):
            await store.clear_tool(tool, "owner")


async def test_load_reads_the_background_settings_and_the_host_locks(db, agent_id, cleanup):
    async with db.session() as session:
        for scope, tool, setting in [
            ("background", "web_search", "denied"),
            ("background", "web_fetch", "allowed"),
            ("background", "send_email", "ask"),  # not a word 3a reads
            ("goal_session", "read_file", "denied"),  # not a scope 3a reads
            ("background", "resolve_intention", "denied"),  # the harness's own tool
        ]:
            session.add(ToolGuardRow(agent_id=agent_id, scope=scope, tool=tool, setting=setting, updated_by="owner"))
        await session.commit()
    settings = _settings(agent_id, autonomy_locked_tools="bash")
    store = AutonomyStore(db, settings)
    await store.load()
    guard = tool_guard.current()
    assert guard.denied == frozenset({"web_search"}) and guard.locked == frozenset({"bash"})
    assert store.tools == {"web_search": "denied", "web_fetch": "allowed", "resolve_intention": "denied"}


async def test_with_the_tool_guard_off_no_guard_is_installed(db, agent_id, cleanup):
    async with db.session() as session:
        session.add(
            ToolGuardRow(agent_id=agent_id, scope="background", tool="web_search", setting="denied", updated_by="owner")
        )
        await session.commit()
    settings = _settings(agent_id, tool_guard_enabled=False, autonomy_locked_tools="bash")
    store = AutonomyStore(db, settings)
    await store.load()
    assert tool_guard.current() is None and store.locked == frozenset()


async def test_the_checks_refuse_another_scope_or_setting_and_a_tool_may_have_two_scopes(db, agent_id, cleanup):
    async def insert(scope, setting, guard="common", tool="web_search"):
        async with db.session() as session:
            await session.execute(
                text(
                    "INSERT INTO nous_system.tool_guard "
                    "(agent_id, scope, tool, setting, destructive_guard, updated_by) "
                    "VALUES (:a, :scope, :tool, :setting, :guard, 'owner')"
                ),
                {"a": agent_id, "scope": scope, "setting": setting, "guard": guard, "tool": tool},
            )
            await session.commit()

    await insert("background", "denied")
    await insert("goal_session", "ask")  # the same tool in another scope
    for scope, setting, guard in [
        ("chat", "denied", "common"),
        ("background", "maybe", "common"),
        ("background", "denied", "never"),
    ]:
        with pytest.raises(Exception, match="check"):
            await insert(scope, setting, guard, tool="web_fetch")
