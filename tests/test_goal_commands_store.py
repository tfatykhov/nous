"""A goal that may run any command, in the goal store and the driver: it is chosen only when the owner creates the
goal (each refusal in its own words, the row and its journal row in one transaction), its end is set at approval to
the least of its hours, its deadline and the host's ceiling, one goal at a time (two approvals at once leave one),
the owner returns it to contained, and a driver pause, Stop now, a cancel and the owner's confirmation end it in the
same transaction, while the owner's own pause keeps it. No other method writes the mode."""

from __future__ import annotations

import asyncio
import inspect
import re
import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select, text, update

from nous.config import Settings
from nous.goals import commands as C
from nous.goals import envelope
from nous.goals.commands import CommandsRequest, confirm_sentence
from nous.goals.driver import GoalDriver
from nous.goals.envelope import EnvelopeError
from nous.goals.store import GoalRefused, GoalStore
from nous.storage.models import Goal, GoalJournal, GoalSession

pytestmark = pytest.mark.postgres_only

CHAN = "telegram:4242"
ON = {
    "goals_enabled": True,
    "continuation_enabled": True,
    "intentions_enabled": True,
    "result_inbox_enabled": True,
    "subtask_enabled": True,
    "autonomy_settings_enabled": True,
    "tool_guard_enabled": True,
    "goal_tool_grants_enabled": True,
    "owner_entry_mode": "enforce",
    "owner_route_secret_sha256": "ab" * 32,
    "mcp_client_ui_secret_sha256": "cd" * 32,
    "goal_commands_enabled": True,
}


def _store(db, **over) -> GoalStore:
    agent = f"goal-commands-{uuid.uuid4().hex[:8]}"
    settings = Settings(_env_file=None, agent_id=agent, telegram_chat_id="", **{**ON, **over})
    return GoalStore(db, agent, settings)


def _ask(hours: int = 5, confirm: str | None = None) -> CommandsRequest:
    return CommandsRequest(hours, confirm_sentence(hours) if confirm is None else confirm)


async def _draft(store: GoalStore, *, commands: CommandsRequest | None = None, proof: str | None = "proxy", **over):
    values = {
        "title": "Ship the fix",
        "objective": "Push the fix to a branch",
        "done_when": "The branch is pushed",
        "proposed_by": "owner",
        "origin_channel": CHAN,
        **over,
    }
    if commands is not None:
        values.update(commands=commands, proof=proof)
    return await store.create(**values)


async def _rows(store: GoalStore, goal_id) -> list[GoalJournal]:
    async with store._db.session() as s:
        query = select(GoalJournal).where(GoalJournal.goal_id == goal_id).order_by(GoalJournal.seq)
        return list((await s.execute(query)).scalars())


async def _mode_rows(store: GoalStore, goal_id) -> list[GoalJournal]:
    return [row for row in await _rows(store, goal_id) if any(str(ref).startswith("mode:") for ref in row.evidence)]


async def _db_now(store: GoalStore):
    async with store._db.session() as s:
        return (await s.execute(text("SELECT clock_timestamp()"))).scalar_one()


# ---- creation ---------------------------------------------------------------------------------------------------


async def test_a_plain_create_writes_what_it_wrote(db):
    store = _store(db)
    goal = await _draft(store)
    assert (goal.mode, goal.mode_hours, goal.mode_until) == ("contained", None, None)
    assert [row.text for row in await _rows(store, goal.id)] == ["Created as a draft (small limits)."]


async def test_the_owner_arms_a_draft_with_its_row_in_the_same_transaction(db):
    store = _store(db)
    goal = await _draft(store, commands=_ask(5))
    assert (goal.mode, goal.mode_hours, goal.mode_until, goal.state) == ("commands", 5, None, "draft")
    created, armed = await _rows(store, goal.id)
    assert created.text == "Created as a draft (small limits)."
    assert armed.text == "This goal may run any command for 5 hours, starting when it is approved."
    assert (armed.kind, armed.actor, armed.evidence) == ("state", "owner", ["mode:armed", "via:proxy"])
    one = await _draft(store, commands=_ask(1), proof="owner")
    assert (await _rows(store, one.id))[-1].text.startswith("This goal may run any command for 1 hour,")
    assert (await _rows(store, one.id))[-1].evidence == ["mode:armed", "via:owner"]


@pytest.mark.parametrize("proposer", ["host", "agent"])
async def test_only_the_owner_can_arm_a_goal(db, proposer):
    store = _store(db)
    with pytest.raises(GoalRefused) as exc:
        await _draft(store, commands=_ask(), proposed_by=proposer)
    assert str(exc.value) == C.PROPOSER


async def test_with_the_flag_off_no_goal_is_armed(db):
    store = _store(db, goal_commands_enabled=False)
    with pytest.raises(GoalRefused) as exc:
        await _draft(store, commands=_ask())
    assert str(exc.value) == C.NOT_ALLOWED


@pytest.mark.parametrize("hours", [0, -1, 25, True])
async def test_the_hours_are_a_whole_number_within_the_ceiling(db, hours):
    store = _store(db)
    with pytest.raises(GoalRefused) as exc:
        await _draft(store, commands=CommandsRequest(hours, confirm_sentence(int(hours))))
    assert str(exc.value) == "Hours must be a whole number from 1 to 24."


async def test_the_hours_are_within_the_goals_deadline(db):
    store = _store(db, goal_commands_max_hours=2160)
    with pytest.raises(GoalRefused) as exc:
        await _draft(store, commands=_ask(73), overrides={"deadline_days": 3})
    assert str(exc.value) == "Hours must be a whole number from 1 to 72."
    goal = await _draft(store, commands=_ask(72), overrides={"deadline_days": 3})
    assert goal.mode_hours == 72


@pytest.mark.parametrize(
    "typed",
    [
        "Allow this goal to run any command for 5 hours",
        "allow this goal to run any command for 6 hours",
        "allow this goal to run any command",
        "",
    ],
)
async def test_the_sentence_is_typed_exactly(db, typed):
    store = _store(db)
    with pytest.raises(GoalRefused) as exc:
        await _draft(store, commands=_ask(5, typed))
    assert str(exc.value) == C.CONFIRM
    goal = await _draft(store, commands=_ask(5, f"  {confirm_sentence(5)} "))
    assert goal.mode == "commands"


async def test_arming_needs_the_routes_proof(db):
    store = _store(db)
    with pytest.raises(ValueError):
        await _draft(store, commands=_ask(), proof=None)


async def test_a_failed_journal_row_leaves_no_goal(db, monkeypatch):
    store = _store(db)
    real = GoalStore.add_journal_row

    def failing(self, s, goal_id, kind, body, **kw):
        if body.startswith("This goal may run any command"):
            raise RuntimeError("the journal insert failed")
        return real(self, s, goal_id, kind, body, **kw)

    monkeypatch.setattr(GoalStore, "add_journal_row", failing)
    with pytest.raises(RuntimeError):
        await _draft(store, commands=_ask())
    assert await store.list() == []


async def test_a_mode_is_still_never_a_limit(db):
    with pytest.raises(EnvelopeError, match="mode"):
        envelope.envelope_for("small", {"mode": "commands"})
    store = _store(db)
    with pytest.raises(GoalRefused, match="mode"):
        await _draft(store, overrides={"mode": "commands"})


# ---- approval ---------------------------------------------------------------------------------------------------


async def test_approval_starts_the_clock_for_the_chosen_hours(db):
    store = _store(db)
    goal = await store.approve((await _draft(store, commands=_ask(5))).id, actor="owner", proof="proxy")
    assert goal.mode == "commands" and goal.mode_until == goal.approved_at + timedelta(hours=5)
    on = (await _rows(store, goal.id))[-1]
    assert on.text == f"May run any command until {goal.mode_until:%Y-%m-%d %H:%M} UTC."
    assert (on.actor, on.evidence) == ("owner", ["mode:on", "via:proxy"])
    assert (await _rows(store, goal.id))[-2].text.startswith("Approved: active until")


async def test_approval_holds_the_end_to_the_hosts_ceiling_and_the_deadline(db):
    store = _store(db, goal_commands_max_hours=48)
    goal = await _draft(store, commands=_ask(48), overrides={"deadline_days": 2})
    object.__setattr__(store._settings, "goal_commands_max_hours", 10)  # lowered since the goal was created
    goal = await store.approve(goal.id, actor="owner")
    assert goal.mode_until == goal.approved_at + timedelta(hours=10)
    assert (await _rows(store, goal.id))[-1].evidence == ["mode:on", "via:owner"]
    object.__setattr__(store._settings, "goal_commands_max_hours", 2160)
    other = await _draft(store, commands=_ask(48), overrides={"deadline_days": 2})
    await store.return_to_contained(goal.id, actor="owner", proof="owner")
    other = await store.approve(other.id, actor="host")
    assert other.mode_until == other.deadline_at == other.approved_at + timedelta(days=2)


async def test_a_second_goal_cannot_be_approved_into_the_mode(db):
    store = _store(db)
    first = await store.approve((await _draft(store, commands=_ask())).id, actor="owner")
    second = await _draft(store, commands=_ask())
    with pytest.raises(GoalRefused) as exc:
        await store.approve(second.id, actor="owner")
    assert str(exc.value) == C.ONE_AT_A_TIME
    assert (await store.get(second.id)).state == "draft"
    await store.pause(first.id, actor="owner")  # a paused goal still holds the place
    with pytest.raises(GoalRefused):
        await store.approve(second.id, actor="owner")
    await store.return_to_contained(first.id, actor="owner")
    assert (await store.approve(second.id, actor="owner")).mode == "commands"
    contained = await store.approve((await _draft(store)).id, actor="owner")  # a contained goal is not counted
    assert contained.mode == "contained"


async def test_an_ended_goal_does_not_hold_the_place(db):
    store = _store(db)
    first = await store.approve((await _draft(store, commands=_ask())).id, actor="owner")
    async with db.session() as s:
        await s.execute(
            update(Goal).where(Goal.id == first.id).values(mode_until=Goal.approved_at - timedelta(seconds=1))
        )
        await s.commit()
    assert (await store.approve((await _draft(store, commands=_ask())).id, actor="owner")).mode == "commands"


async def test_two_approvals_at_once_leave_one_goal_in_the_mode(db, monkeypatch):
    """The first approval holds the agent's lock and waits before its commit; the second waits for that lock (seen
    in pg_stat_activity), then counts the first and is refused."""
    store = _store(db)
    one, two = await _draft(store, commands=_ask()), await _draft(store, commands=_ask())
    real = GoalStore._one_at_a_time
    holding, release = asyncio.Event(), asyncio.Event()

    async def held(self, s, goal, now):
        await real(self, s, goal, now)
        if goal.id == one.id:
            holding.set()
            await release.wait()

    monkeypatch.setattr(GoalStore, "_one_at_a_time", held)
    first = asyncio.create_task(store.approve(one.id, actor="owner"))
    await holding.wait()
    second = asyncio.create_task(store.approve(two.id, actor="owner"))
    waiting = 0
    for _ in range(100):
        async with db.session() as s:
            waiting = await s.scalar(
                text(
                    "SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock' "
                    "AND wait_event = 'advisory' AND datname = current_database()"
                )
            )
        if waiting:
            break
        await asyncio.sleep(0.02)
    assert waiting == 1
    release.set()
    assert (await first).mode == "commands"
    with pytest.raises(GoalRefused) as exc:
        await second
    assert str(exc.value) == C.ONE_AT_A_TIME
    async with db.session() as s:
        count = await s.scalar(
            select(text("count(*)"))
            .select_from(Goal)
            .where(Goal.agent_id == store.agent_id, Goal.mode_until.is_not(None))
        )
    assert count == 1


async def test_a_return_to_contained_during_an_approval_waits_for_it(db, monkeypatch):
    """The approval holds the goal's row; the owner's return waits for it, then clears the end it set."""
    store = _store(db)
    goal = await _draft(store, commands=_ask())
    real = GoalStore._one_at_a_time
    holding, release = asyncio.Event(), asyncio.Event()

    async def held(self, s, goal_row, now):
        await real(self, s, goal_row, now)
        holding.set()
        await release.wait()

    monkeypatch.setattr(GoalStore, "_one_at_a_time", held)
    approval = asyncio.create_task(store.approve(goal.id, actor="owner"))
    await holding.wait()
    returned = asyncio.create_task(store.return_to_contained(goal.id, actor="owner", proof="owner"))
    waiting = 0
    for _ in range(100):
        async with db.session() as s:
            waiting = await s.scalar(
                text(
                    "SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock' "
                    "AND datname = current_database()"
                )
            )
        if waiting:
            break
        await asyncio.sleep(0.02)
    assert waiting == 1 and not returned.done()
    release.set()
    assert (await approval).mode == "commands"
    final = await returned
    assert (final.state, final.mode, final.mode_until) == ("active", "contained", None)


async def test_an_armed_draft_approved_after_the_flag_went_off_is_approved_contained(db):
    store = _store(db)
    goal = await _draft(store, commands=_ask())
    object.__setattr__(store._settings, "goal_commands_enabled", False)
    goal = await store.approve(goal.id, actor="owner")
    assert (goal.state, goal.mode, goal.mode_hours, goal.mode_until) == ("active", "contained", None, None)
    off = (await _rows(store, goal.id))[-1]
    assert (off.actor, off.evidence, off.text) == ("harness", ["mode:off", "why:flag_off"], C.ENDED["flag_off"])


# ---- the one later change ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("state", ["draft", "active", "paused"])
async def test_the_owner_returns_a_goal_to_contained_in_any_live_state(db, state):
    store = _store(db)
    goal = await _draft(store, commands=_ask())
    if state != "draft":
        goal = await store.approve(goal.id, actor="owner")
    if state == "paused":
        goal = await store.pause(goal.id, actor="owner")
    object.__setattr__(store._settings, "goal_commands_enabled", False)  # whatever the flag says now
    before = await store.get(goal.id)
    returned = await store.return_to_contained(goal.id, actor="owner", proof="proxy")
    assert (returned.mode, returned.mode_hours, returned.mode_until, returned.state) == (
        "contained",
        None,
        None,
        state,
    )
    assert (returned.next_wake_at, returned.pending_request) == (before.next_wake_at, before.pending_request)
    row = (await _rows(store, goal.id))[-1]
    assert (row.text, row.actor, row.evidence) == (
        "Returned to contained.",
        "owner",
        ["mode:off", "why:returned", "via:proxy"],
    )
    count = len(await _rows(store, goal.id))
    again = await store.return_to_contained(goal.id, actor="owner", proof="proxy")
    assert again.mode == "contained" and len(await _rows(store, goal.id)) == count  # a no-op writes nothing


async def test_only_the_owner_returns_a_goal_to_contained(db):
    store = _store(db)
    goal = await _draft(store, commands=_ask())
    for actor in ("host", "chain", "harness"):
        with pytest.raises(GoalRefused):
            await store.return_to_contained(goal.id, actor=actor)
    assert (await store.get(goal.id)).mode == "commands"


# ---- the ends ---------------------------------------------------------------------------------------------------


async def _in_mode(store: GoalStore) -> Goal:
    return await store.approve((await _draft(store, commands=_ask())).id, actor="owner")


async def _session(store: GoalStore, goal: Goal) -> GoalSession:
    async with store._db.session() as s:
        row = GoalSession(agent_id=store.agent_id, goal_id=goal.id, n=1, trigger="timer", first_subtask_id=uuid.uuid4())
        s.add(row)
        await s.commit()
        return row


def _ended(goal: Goal) -> bool:
    return (goal.mode, goal.mode_hours, goal.mode_until) == ("contained", None, None)


async def test_the_owners_pause_keeps_the_mode(db):
    store = _store(db)
    goal = await store.pause((await _in_mode(store)).id, actor="owner")
    assert goal.mode == "commands" and goal.mode_until is not None
    assert (await _mode_rows(store, goal.id))[-1].evidence[0] == "mode:on"


@pytest.mark.parametrize("paused_first", [False, True])
async def test_stop_now_ends_the_mode(db, paused_first):
    store = _store(db)
    goal = await _in_mode(store)
    await _session(store, goal)
    if paused_first:
        await store.pause(goal.id, actor="owner")
    goal = await store.stop(goal.id, actor="owner")
    assert _ended(goal) and goal.pending_request == "stop"
    row = (await _rows(store, goal.id))[-1]
    assert (row.actor, row.evidence, row.text) == ("owner", ["mode:off", "why:stopped"], C.ENDED["stopped"])


async def test_a_cancel_ends_the_mode(db):
    store = _store(db)
    goal = await store.cancel((await _in_mode(store)).id, actor="owner")
    assert goal.state == "cancelled" and _ended(goal)
    assert (await _rows(store, goal.id))[-1].evidence == ["mode:off", "why:cancelled"]
    armed = await store.cancel((await _draft(store, commands=_ask())).id, actor="owner")
    assert _ended(armed)


async def test_the_owners_confirmation_ends_the_mode(db):
    store = _store(db)
    goal = await _in_mode(store)
    async with db.session() as s:
        await s.execute(update(Goal).where(Goal.id == goal.id).values(state="paused", pause_reason="waiting_for_owner"))
        await s.commit()
    goal = await store.confirm_done(goal.id, actor="host")
    assert goal.state == "done" and _ended(goal)
    assert (await _rows(store, goal.id))[-1].evidence == ["mode:off", "why:done"]


async def test_a_contained_goals_changes_write_no_mode_row(db):
    store = _store(db)
    goal = await store.approve((await _draft(store)).id, actor="owner")
    await _session(store, goal)
    await store.stop(goal.id, actor="owner")
    await store.cancel(goal.id, actor="owner")
    assert await _mode_rows(store, goal.id) == []


@pytest.mark.parametrize("reason", ["budget", "stalled", "error", "deadline"])
async def test_every_driver_pause_ends_the_mode(db, reason):
    store = _store(db)
    goal = await _in_mode(store)
    driver = GoalDriver(db, store._settings, None, store, None)
    async with db.session() as s:
        assert await driver._lock_active(s, goal.id) is not None
        assert await driver._paused(s, goal.id, reason, f"Paused: {reason}.", evidence=[f"stop:{reason}"])
        await s.commit()
    goal = await store.get(goal.id)
    assert goal.state == "paused" and _ended(goal)
    pause, ended = (await _rows(store, goal.id))[-2:]
    assert pause.text == f"Paused: {reason}." and pause.evidence == [f"stop:{reason}"]
    assert (ended.actor, ended.evidence, ended.text) == ("harness", ["mode:off", "why:paused"], C.ENDED["paused"])


async def test_a_driver_pause_of_a_contained_goal_writes_no_mode_row(db):
    store = _store(db)
    goal = await store.approve((await _draft(store)).id, actor="owner")
    driver = GoalDriver(db, store._settings, None, store, None)
    async with db.session() as s:
        assert await driver._paused(s, goal.id, "budget", "Paused: budget.")
        await s.commit()
    assert await _mode_rows(store, goal.id) == []


async def test_the_owners_cancel_of_a_sessions_chain_ends_the_mode(db):
    store = _store(db)
    goal = await _in_mode(store)
    row = await _session(store, goal)
    driver = GoalDriver(db, store._settings, None, store, None)
    now = await _db_now(store)
    async with db.session() as s:
        goal_row = (await s.execute(select(Goal).where(Goal.id == goal.id).with_for_update())).scalar_one()
        assert await driver._end(
            s, goal_row, row, now, outcome="interrupted", spent=0, steps=0, summary=None, cancelled_by_owner=True
        )
        await s.commit()
    goal = await store.get(goal.id)
    assert goal.pause_reason == "owner" and _ended(goal)
    assert (await _rows(store, goal.id))[-1].evidence == ["mode:off", "why:stopped"]


# ---- who writes the mode ----------------------------------------------------------------------------------------

# Every public method of the store, and whether it may write a goal's mode. A new method fails the test below until
# it is listed here.
WRITES_MODE = {"create", "approve", "return_to_contained", "stop", "cancel", "confirm_done"}
NO_MODE = {
    "get",
    "list",
    "sessions",
    "spent",
    "tool_grants",
    "journal",
    "journal_tail",
    "pause",
    "resume",
    "request_run_now",
    "raise_budget",
    "extend_deadline",
    "add_note",
    "grant_tool",
    "revoke_tool",
    "delay_wake",
    "append_journal",
    "add_journal_row",
}
_WRITES = re.compile(r"CONTAINED_VALUES|_ending\(|\"mode_until\"|\"mode\":|mode_hours|MODE_COMMANDS")


def test_every_public_store_method_is_classified_and_only_the_listed_ones_write_the_mode():
    public = {name for name, _fn in inspect.getmembers(GoalStore, inspect.isfunction) if not name.startswith("_")}
    assert public == WRITES_MODE | NO_MODE
    for name in public:
        source = inspect.getsource(getattr(GoalStore, name))
        assert bool(_WRITES.search(source)) is (name in WRITES_MODE), name


def test_the_driver_writes_the_mode_only_where_it_ends_it():
    writes = {
        name
        for name, fn in inspect.getmembers(GoalDriver, inspect.isfunction)
        if "CONTAINED_VALUES" in inspect.getsource(fn) or "mode_until" in inspect.getsource(fn)
    }
    assert writes == {"_paused", "_end"}


def test_the_chains_tools_cannot_reach_the_mode():
    source = (Path(__file__).resolve().parents[1] / "nous" / "goals" / "tools.py").read_text(encoding="utf-8")
    for word in ("return_to_contained", "CONTAINED_VALUES", "mode_until", "mode_hours", "commands", ".approve("):
        assert word not in source, word


async def test_two_creates_at_once_both_arm_and_their_approvals_leave_one_goal_in_the_mode(db):
    """The rule binds at approval: two drafts may each be created to run any command, and approving both at once
    leaves exactly one with an end."""
    store = _store(db)
    one, two = await asyncio.gather(_draft(store, commands=_ask()), _draft(store, commands=_ask()))
    assert (one.mode, two.mode) == ("commands", "commands")
    results = await asyncio.gather(
        store.approve(one.id, actor="owner"), store.approve(two.id, actor="owner"), return_exceptions=True
    )
    approved = [r for r in results if not isinstance(r, BaseException)]
    refused = [r for r in results if isinstance(r, GoalRefused)]
    assert len(approved) == 1 and len(refused) == 1 and str(refused[0]) == C.ONE_AT_A_TIME
    assert approved[0].mode_until is not None
