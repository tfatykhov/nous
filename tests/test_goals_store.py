"""F114 goals: the envelope presets and the goal store (each transition, each refusal)."""

from __future__ import annotations

import inspect
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text, update

from nous.config import Settings
from nous.goals import envelope
from nous.goals.envelope import PRESETS, Envelope, EnvelopeError
from nous.goals.store import JOURNAL_MAX_CHARS, GoalRefused, GoalStore, format_short_id, parse_ref
from nous.storage.models import Goal, GoalJournal, GoalSession

pytestmark = pytest.mark.postgres_only

CHAN = "telegram:4242"


def _store(db, **over) -> GoalStore:
    agent = f"goals-store-{uuid.uuid4().hex[:8]}"
    settings = Settings(_env_file=None, agent_id=agent, **{"telegram_chat_id": "", **over})
    return GoalStore(db, agent, settings)


async def _draft(store: GoalStore, **over) -> Goal:
    values = {
        "title": "Tidy the notes",
        "objective": "Merge the duplicate notes in the notes folder",
        "done_when": "No two notes say the same thing",
        "proposed_by": "host",
        "origin_channel": CHAN,
        **over,
    }
    return await store.create(**values)


async def _active(store: GoalStore, **over) -> Goal:
    goal = await _draft(store, **over)
    return await store.approve(goal.id, actor="host")


async def _open_session(store: GoalStore, goal: Goal, *, n: int = 1, spent: int = 0) -> GoalSession:
    async with store._db.session() as s:
        row = GoalSession(
            agent_id=store.agent_id, goal_id=goal.id, n=n, trigger="timer", first_subtask_id=uuid.uuid4(), spent=spent
        )
        s.add(row)
        await s.commit()
        return row


async def _set(store: GoalStore, goal: Goal, **values) -> None:
    async with store._db.session() as s:
        await s.execute(update(Goal).where(Goal.id == goal.id).values(**values))
        await s.commit()


async def _journal(store: GoalStore, goal: Goal) -> list[GoalJournal]:
    async with store._db.session() as s:
        rows = await s.execute(select(GoalJournal).where(GoalJournal.goal_id == goal.id).order_by(GoalJournal.seq))
        return list(rows.scalars())


# ---- the envelope ---------------------------------------------------------------------------------------------


def test_the_presets_are_the_specs():
    assert PRESETS["small"] == Envelope(2_000_000, 500_000, 3, 2, 7_200, 1, False)
    assert PRESETS["medium"] == Envelope(10_000_000, 2_000_000, 10, 4, 3_600, 2, False)
    assert PRESETS["large"] == Envelope(40_000_000, 6_000_000, 30, 8, 1_800, 4, True)
    for preset in PRESETS.values():
        envelope.validate(preset)


def test_overrides_make_a_custom_envelope_and_a_bad_one_is_refused_in_words():
    name, env = envelope.envelope_for("small", {"total_budget": 3_000_000})
    assert name == "custom" and env.total_budget == 3_000_000 and env.day_budget == 500_000
    assert envelope.envelope_for("medium", None) == ("medium", PRESETS["medium"])
    with pytest.raises(EnvelopeError, match="day budget"):
        envelope.envelope_for("small", {"day_budget": 3_000_000})
    with pytest.raises(EnvelopeError, match="mode"):
        envelope.envelope_for("small", {"mode": "full_autonomy"})
    with pytest.raises(EnvelopeError, match="preset"):
        envelope.envelope_for("huge", None)
    with pytest.raises(EnvelopeError, match="deadline"):
        envelope.envelope_for("small", {"deadline_days": 91})


# ---- create, approve ------------------------------------------------------------------------------------------


async def test_create_makes_a_draft_with_the_preset_envelope(db):
    store = _store(db)
    goal = await _draft(store, preset="medium")
    assert goal.state == "draft" and goal.pause_reason is None and goal.mode == "contained"
    assert goal.preset == "medium" and goal.total_budget == 10_000_000 and goal.day_budget == 2_000_000
    assert goal.deadline_days == 10 and goal.sessions_per_day == 4 and goal.min_interval_seconds == 3_600
    assert goal.jobs_per_day == 2 and goal.quiet_hours_ok is False
    assert goal.deadline_at is None and goal.next_wake_at is None and goal.approved_at is None
    assert isinstance(goal.short_id, int) and goal.short_id >= 1
    (row,) = await _journal(store, goal)
    assert (row.kind, row.actor) == ("state", "host")


async def test_create_refuses_empty_or_long_text_in_words(db):
    store = _store(db)
    with pytest.raises(GoalRefused, match="title"):
        await _draft(store, title="  ")
    with pytest.raises(GoalRefused, match="objective"):
        await _draft(store, objective="x" * 4001)
    assert await store.list() == []


async def test_approve_sets_deadline_and_first_wake(db):
    store = _store(db)
    before = datetime.now(UTC)
    goal = await _active(store)
    assert goal.state == "active"
    assert before <= goal.approved_at <= datetime.now(UTC)
    assert goal.deadline_at == goal.approved_at + timedelta(days=goal.deadline_days)
    assert goal.next_wake_at == goal.approved_at


async def test_approve_twice_is_refused_and_changes_nothing(db):
    store = _store(db)
    goal = await _active(store)
    rows_before = await _journal(store, goal)
    with pytest.raises(GoalRefused, match="only a draft"):
        await store.approve(goal.id, actor="host")
    again = await store.get(goal.id)
    assert (again.approved_at, again.deadline_at, again.state) == (goal.approved_at, goal.deadline_at, "active")
    assert len(await _journal(store, goal)) == len(rows_before)


async def test_approve_is_refused_without_an_owner_channel(db):
    store = _store(db)  # telegram_chat_id is empty
    goal = await _draft(store, origin_channel=None)
    with pytest.raises(GoalRefused, match="nowhere to ask the owner"):
        await store.approve(goal.id, actor="host")
    assert (await store.get(goal.id)).state == "draft"
    with_chat = _store(db, telegram_chat_id="77")
    other = await _draft(with_chat, origin_channel=None)
    assert (await with_chat.approve(other.id, actor="host")).state == "active"  # the default chat is enough


# ---- pause, resume, confirm, cancel ---------------------------------------------------------------------------


async def test_pause_resume_confirm_cancel_walk(db):
    store = _store(db)
    goal = await _active(store)
    count = len(await _journal(store, goal))

    paused = await store.pause(goal.id, actor="owner")
    assert (paused.state, paused.pause_reason, paused.next_wake_at) == ("paused", "owner", None)
    resumed = await store.resume(goal.id, actor="host")
    assert (resumed.state, resumed.pause_reason) == ("active", None) and resumed.next_wake_at is not None
    await _set(store, goal, state="paused", pause_reason="waiting_for_owner", next_wake_at=None)
    done = await store.confirm_done(goal.id, actor="owner")
    assert (done.state, done.pause_reason, done.next_wake_at) == ("done", None, None) and done.finished_at
    with pytest.raises(GoalRefused):
        await store.cancel(goal.id, actor="host")  # done is final

    other = await _draft(store)
    cancelled = await store.cancel(other.id, actor="host")
    assert (cancelled.state, cancelled.pending_request) == ("cancelled", None) and cancelled.finished_at

    rows = (await _journal(store, goal))[count:]
    assert [(row.kind, row.actor) for row in rows] == [("state", "owner"), ("state", "host"), ("state", "owner")]


async def test_resume_after_budget_pause_is_refused_until_the_budget_is_raised(db):
    store = _store(db)
    goal = await _active(store)
    session = await _open_session(store, goal, spent=goal.total_budget)
    async with store._db.session() as s:
        await s.execute(
            update(GoalSession)
            .where(GoalSession.id == session.id)
            .values(ended_at=datetime.now(UTC), outcome="no_step")
        )
        await s.commit()
    await _set(store, goal, state="paused", pause_reason="budget", next_wake_at=None)
    with pytest.raises(GoalRefused, match="total budget is spent"):
        await store.resume(goal.id, actor="host")
    raised = await store.raise_budget(goal.id, total=goal.total_budget * 2, actor="host")
    assert raised.total_budget == goal.total_budget * 2 and raised.state == "paused"
    assert (await store.resume(goal.id, actor="host")).state == "active"


async def test_resume_after_a_deadline_pause_is_refused_until_the_deadline_is_extended(db):
    store = _store(db)
    goal = await _active(store)
    past = datetime.now(UTC) - timedelta(minutes=1)
    await _set(store, goal, state="paused", pause_reason="deadline", next_wake_at=None, deadline_at=past)
    with pytest.raises(GoalRefused, match="deadline has passed"):
        await store.resume(goal.id, actor="host")
    before = datetime.now(UTC)
    extended = await store.extend_deadline(goal.id, days=2, actor="host")
    # counted from now, not from the passed deadline (which would leave only a day and 23 hours)
    assert before + timedelta(days=2) <= extended.deadline_at <= datetime.now(UTC) + timedelta(days=2)
    assert extended.deadline_days == goal.deadline_days  # the length it was approved with
    assert (await store.resume(goal.id, actor="host")).state == "active"
    with pytest.raises(GoalRefused, match="90"):
        await store.extend_deadline(goal.id, days=89, actor="host")  # more than 90 days from now
    assert (await store.get(goal.id)).deadline_at == extended.deadline_at


async def test_extend_counts_from_a_future_deadline_and_caps_at_90_days_from_now(db):
    store = _store(db)
    goal = await _active(store, overrides={"deadline_days": 60})
    tomorrow = datetime.now(UTC) + timedelta(days=1)  # 59 of its 60 days have passed
    await _set(store, goal, deadline_at=tomorrow)
    extended = await store.extend_deadline(goal.id, days=80, actor="host")  # 60 + 80 > 90, but 81 days from now
    assert extended.deadline_at == tomorrow + timedelta(days=80)
    with pytest.raises(GoalRefused, match="90"):
        await store.extend_deadline(goal.id, days=10, actor="host")  # 91 days from now
    assert (await store.get(goal.id)).deadline_at == extended.deadline_at
    draft = await _draft(store)
    assert (await store.extend_deadline(draft.id, days=4, actor="host")).deadline_days == 7  # applied at approval
    with pytest.raises(GoalRefused, match="90"):
        await store.extend_deadline(draft.id, days=84, actor="host")


async def test_confirm_done_only_from_waiting_for_owner(db):
    store = _store(db)
    goal = await _active(store)
    with pytest.raises(GoalRefused, match="waiting for the owner"):
        await store.confirm_done(goal.id, actor="host")
    await store.pause(goal.id, actor="host")
    with pytest.raises(GoalRefused, match="waiting for the owner"):
        await store.confirm_done(goal.id, actor="host")
    assert (await store.get(goal.id)).state == "paused"


# ---- stop, cancel and run now with and without an open session ------------------------------------------------


async def test_stop_with_no_open_session_is_a_plain_pause(db):
    store = _store(db)
    goal = await _active(store)
    stopped = await store.stop(goal.id, actor="host")
    assert (stopped.state, stopped.pause_reason, stopped.pending_request) == ("paused", "owner", None)


async def test_pause_and_a_plain_stop_clear_a_pending_run_now(db):
    store = _store(db)
    for change in (store.pause, store.stop):
        goal = await _active(store)
        assert (await store.request_run_now(goal.id, actor="host")).pending_request == "run_now"
        after = await change(goal.id, actor="host")
        assert (after.state, after.pending_request, after.pending_request_at) == ("paused", None, None)


async def test_stop_on_a_paused_goal_stops_its_open_session_and_keeps_the_pause(db):
    store = _store(db)
    goal = await _active(store)
    await store.pause(goal.id, actor="owner")
    with pytest.raises(GoalRefused, match="no session is open"):
        await store.stop(goal.id, actor="host")  # paused with nothing running: nothing to stop
    await _set(store, goal, pause_reason="budget")
    await _open_session(store, goal, n=1)
    count = len(await _journal(store, goal))
    stopped = await store.stop(goal.id, actor="host")
    assert (stopped.state, stopped.pause_reason, stopped.pending_request) == ("paused", "budget", "stop")
    rows = await _journal(store, goal)
    assert len(rows) == count + 1 and (rows[-1].kind, rows[-1].session_n) == ("state", 1)


async def test_stop_with_an_open_session_asks_the_driver_and_names_the_session(db):
    store = _store(db)
    goal = await _active(store)
    await _open_session(store, goal, n=1)
    stopped = await store.stop(goal.id, actor="host")
    assert (stopped.state, stopped.pause_reason, stopped.pending_request) == ("paused", "owner", "stop")
    assert stopped.pending_request_at is not None
    assert (await _journal(store, goal))[-1].session_n == 1


async def test_cancel_with_an_open_session_asks_the_driver_to_cancel_it(db):
    store = _store(db)
    goal = await _active(store)
    await _open_session(store, goal, n=1)
    cancelled = await store.cancel(goal.id, actor="host")
    assert (cancelled.state, cancelled.pause_reason, cancelled.pending_request) == ("cancelled", None, "cancel")


async def test_run_now_only_for_an_active_goal_with_no_open_session(db):
    store = _store(db)
    goal = await _active(store)
    asked = await store.request_run_now(goal.id, actor="host")
    assert asked.pending_request == "run_now" and asked.pending_request_at is not None
    await _open_session(store, goal)
    with pytest.raises(GoalRefused, match="session 1 is already running"):
        await store.request_run_now(goal.id, actor="host")
    draft = await _draft(store)
    with pytest.raises(GoalRefused, match="only an active goal"):
        await store.request_run_now(draft.id, actor="host")


async def test_resume_and_approve_clear_a_pending_request(db):
    store = _store(db)
    goal = await _active(store)
    await _open_session(store, goal)
    assert (await store.stop(goal.id, actor="host")).pending_request == "stop"
    resumed = await store.resume(goal.id, actor="host")
    assert (resumed.pending_request, resumed.pending_request_at) == (None, None)

    draft = await _draft(store)
    await _set(store, draft, pending_request="run_now", pending_request_at=datetime.now(UTC))  # a stale row
    approved = await store.approve(draft.id, actor="host")
    assert (approved.pending_request, approved.pending_request_at) == (None, None)


# ---- notes, wakes and the journal ----------------------------------------------------------------------------


async def test_a_note_wakes_an_active_goal_with_no_session_and_never_resumes_a_paused_one(db):
    store = _store(db)
    goal = await _active(store)
    later = datetime.now(UTC) + timedelta(hours=5)
    await _set(store, goal, next_wake_at=later)
    noted = await store.add_note(goal.id, "Look in the archive folder too", actor="owner")
    assert noted.next_wake_at < later
    assert (await _journal(store, goal))[-1].kind == "owner_note"

    await _open_session(store, goal)
    await _set(store, goal, next_wake_at=later)
    assert (await store.add_note(goal.id, "and the drafts", actor="owner")).next_wake_at == later  # a session runs

    other = await _active(store)
    await store.pause(other.id, actor="owner")
    still = await store.add_note(other.id, "keep going", actor="owner")
    assert (still.state, still.pause_reason, still.next_wake_at) == ("paused", "owner", None)


async def test_delay_wake_moves_only_the_wake_its_caller_saw(db):
    store = _store(db)
    goal = await _active(store)
    seen = goal.next_wake_at  # the driver read the goal due
    assert await store.delay_wake(goal.id, seconds=300, seen=seen) is True
    delayed = (await store.get(goal.id)).next_wake_at
    assert delayed > seen
    await store.add_note(goal.id, "look at the drafts too", actor="owner")  # the owner wakes it now
    woken = (await store.get(goal.id)).next_wake_at
    assert woken < delayed
    assert await store.delay_wake(goal.id, seconds=300, seen=delayed) is False  # a stale read moves nothing
    assert (await store.get(goal.id)).next_wake_at == woken
    with pytest.raises(GoalRefused, match="delayed"):
        await store.delay_wake(goal.id, seconds=-1, seen=woken)
    await store.pause(goal.id, actor="owner")
    assert await store.delay_wake(goal.id, seconds=60, seen=None) is False  # paused: nothing moves
    after = await store.get(goal.id)
    assert (after.state, after.pause_reason, after.next_wake_at) == ("paused", "owner", None)


async def test_journal_text_is_clipped_to_the_bound(db):
    store = _store(db)
    goal = await _draft(store)
    row = await store.append_journal(
        goal.id, "progress", "y" * 5000, actor="chain", session_n=2, evidence=[f"ref-{i}" * 80 for i in range(12)]
    )
    assert len(row.text) == JOURNAL_MAX_CHARS and row.text.endswith("[clipped]")
    assert len(row.evidence) == 10 and all(len(ref) <= 300 for ref in row.evidence)
    assert row.session_n == 2 and row.seq >= 1
    short = await store.append_journal(goal.id, "progress", "short", actor="chain")
    assert short.text == "short" and short.seq > row.seq


async def test_the_journal_tail_is_bounded_newest_first_and_keeps_owner_notes(db):
    store = _store(db)
    goal = await _draft(store)
    await store.add_note(goal.id, "the owner's first note", actor="owner")
    for i in range(10):
        await store.append_journal(goal.id, "progress", f"step {i} " + "z" * 1990, actor="chain")
    tail = await store.journal_tail(goal.id, chars=6000)
    assert tail[0].text.startswith("step 9")
    assert [row.kind for row in tail].count("owner_note") == 1
    assert all(len(row.text) <= 1500 for row in tail)
    assert sum(len(row.text) for row in tail) <= 6000 + len("the owner's first note")
    assert len([row for row in tail if row.kind == "progress"]) < 10


async def test_the_store_has_no_way_to_edit_or_delete_a_journal_row():
    public = {name for name, _ in inspect.getmembers(GoalStore, inspect.isfunction) if not name.startswith("_")}
    assert not [name for name in public if "delete" in name or "remove" in name or "update" in name]
    # the journal's only writer appends; the other two read
    assert {name for name in public if "journal" in name} == {"append_journal", "journal", "journal_tail"}


async def test_short_id_forms_resolve(db):
    store = _store(db)
    goal = await _draft(store)
    label = format_short_id(goal.short_id)
    assert label == f"G-{goal.short_id:04d}"
    for ref in (goal.id, str(goal.id), goal.short_id, str(goal.short_id), label, label.lower()):
        assert (await store.get(ref)).id == goal.id, ref
    assert parse_ref("G-0007") == 7 and parse_ref("g7") == 7 and parse_ref("nonsense") is None
    assert await store.get("G-999999999") is None
    other = _store(db)
    assert await other.get(goal.id) is None  # agent-scoped
    with pytest.raises(GoalRefused, match="no goal"):
        await other.pause(goal.id, actor="host")


async def test_the_store_cannot_set_a_mode(db):
    for name, fn in inspect.getmembers(GoalStore, inspect.isfunction):
        assert "mode" not in inspect.signature(fn).parameters, name
    store = _store(db)
    with pytest.raises(GoalRefused, match="mode"):
        await _draft(store, overrides={"mode": "full_autonomy"})
    goal = await _draft(store, overrides={"jobs_per_day": 3})
    assert (goal.mode, goal.mode_until, goal.preset) == ("contained", None, "custom")
    async with store._db.session() as s:
        assert (
            await s.execute(
                text("SELECT count(*) FROM brain.goals WHERE agent_id = :a AND mode <> 'contained'"),
                {"a": store.agent_id},
            )
        ).scalar_one() == 0


# ---- who may change what, and whose goal ----------------------------------------------------------------------


async def test_owner_only_changes_refuse_any_other_actor(db):
    store = _store(db)
    draft = await _draft(store)
    with pytest.raises(GoalRefused, match="only the owner"):
        await store.approve(draft.id, actor="chain")
    goal = await _active(store)
    await store.pause(goal.id, actor="host")
    for change in (
        lambda: store.resume(goal.id, actor="chain"),
        lambda: store.raise_budget(goal.id, total=goal.total_budget * 2, actor="chain"),
        lambda: store.extend_deadline(goal.id, days=1, actor="harness"),
        lambda: store.confirm_done(goal.id, actor="chain"),
    ):
        with pytest.raises(GoalRefused, match="only the owner"):
            await change()
    after = await store.get(goal.id)
    assert (after.state, after.total_budget, after.deadline_at) == ("paused", goal.total_budget, goal.deadline_at)
    assert (await store.get(draft.id)).state == "draft"


async def test_another_agents_store_cannot_write_to_this_agents_goal(db):
    store = _store(db)
    goal = await _active(store)
    other = _store(db)
    count = len(await _journal(store, goal))
    with pytest.raises(GoalRefused, match="no goal"):
        await other.append_journal(goal.id, "progress", "not mine", actor="chain")
    assert await other.delay_wake(goal.id, seconds=60, seen=goal.next_wake_at) is False
    assert len(await _journal(store, goal)) == count
    assert (await store.get(goal.id)).next_wake_at == goal.next_wake_at


async def test_raise_budget_refuses_a_total_below_what_is_spent(db):
    store = _store(db)
    goal = await _active(store)
    await _open_session(store, goal, spent=700_000)
    with pytest.raises(GoalRefused, match="already spent"):
        await store.raise_budget(goal.id, total=600_000, day=100_000, actor="host")
    assert (await store.raise_budget(goal.id, total=700_000, day=100_000, actor="host")).total_budget == 700_000


async def test_a_note_on_a_finished_goal_is_refused(db):
    store = _store(db)
    goal = await _draft(store)
    await store.cancel(goal.id, actor="host")
    count = len(await _journal(store, goal))
    with pytest.raises(GoalRefused, match="cancelled"):
        await store.add_note(goal.id, "too late", actor="owner")
    done = await _active(store)
    await _set(store, done, state="done", next_wake_at=None)
    with pytest.raises(GoalRefused, match="done"):
        await store.add_note(done.id, "too late", actor="owner")
    assert len(await _journal(store, goal)) == count


# ---- one clock --------------------------------------------------------------------------------------------------


class _SlowClock(datetime):
    """A process clock an hour behind the database's."""

    @classmethod
    def now(cls, tz=None):
        return datetime.now(tz) - timedelta(hours=1)


async def test_every_stored_time_comes_from_the_database_clock(db, monkeypatch):
    import nous.goals.store as store_module

    monkeypatch.setattr(store_module, "datetime", _SlowClock)
    store = _store(db)
    goal = await _draft(store)
    async with db.session() as s:
        before = (await s.execute(text("SELECT clock_timestamp()"))).scalar_one()
    approved = await store.approve(goal.id, actor="host")
    assert approved.approved_at >= before and approved.next_wake_at >= before
    assert approved.deadline_at == approved.approved_at + timedelta(days=approved.deadline_days)
    # a session inserted under the goal's row lock (the driver's order), then a stop: the request is not older
    async with db.session() as s:
        await s.execute(select(Goal).where(Goal.id == goal.id).with_for_update())
        s.add(
            GoalSession(agent_id=store.agent_id, goal_id=goal.id, n=1, trigger="timer", first_subtask_id=uuid.uuid4())
        )
        await s.commit()
    (session,) = await store.sessions(goal.id)
    stopped = await store.stop(goal.id, actor="host")
    assert stopped.pending_request_at >= session.started_at
    cancelled = await store.cancel(goal.id, actor="host")
    assert cancelled.finished_at >= session.started_at and cancelled.pending_request_at >= session.started_at
    other = await _active(store)
    assert await store.delay_wake(other.id, seconds=60, seen=other.next_wake_at)
    assert (await store.get(other.id)).next_wake_at >= before + timedelta(seconds=60)


# ---- what create accepts ------------------------------------------------------------------------------------------


def test_the_delivered_channel_kinds_are_the_publishers():
    from pathlib import Path

    from nous.goals.store import DELIVERED_CHANNEL_KINDS

    source = (Path(__file__).resolve().parents[1] / "nous" / "handlers" / "continuation_publisher.py").read_text(
        encoding="utf-8"
    )
    assert DELIVERED_CHANNEL_KINDS == ("telegram",)
    assert all(f'ResultInbox.channel.like("{kind}:%")' in source for kind in DELIVERED_CHANNEL_KINDS)


async def test_create_refuses_an_origin_channel_the_owner_is_never_sent_to(db):
    store = _store(db)
    for channel in ("slack:42", "telegram:", "telegram", "4242", "telegram:42 43", "companion:web"):
        with pytest.raises(GoalRefused, match="origin channel"):
            await _draft(store, origin_channel=channel)
    assert await store.list() == []
    assert (await _draft(store, origin_channel=" telegram:-1001234 ")).origin_channel == "telegram:-1001234"
    assert (await _draft(store, origin_channel=None)).origin_channel is None


def test_out_of_range_limits_are_refused_in_words():
    with pytest.raises(EnvelopeError, match="interval"):
        envelope.envelope_for("small", {"min_interval_seconds": 10**12})
    with pytest.raises(EnvelopeError, match="day budget"):
        envelope.envelope_for("small", {"day_budget": 10**20, "total_budget": 10**20})
    with pytest.raises(EnvelopeError, match="day budget"):
        envelope.validate(replace(PRESETS["small"], day_budget=10**20))


async def test_a_huge_extension_is_a_refusal_not_an_overflow(db):
    store = _store(db)
    goal = await _active(store)
    for days in (91, 10**12):
        with pytest.raises(GoalRefused, match="90"):
            await store.extend_deadline(goal.id, days=days, actor="host")
    assert (await store.get(goal.id)).deadline_at == goal.deadline_at


async def test_after_an_overshoot_the_budget_may_still_move_the_right_way(db):
    store = _store(db)
    goal = await _active(store)  # total 2,000,000, day 500,000
    await _open_session(store, goal, spent=2_300_000)  # a chain overshot the total
    day_only = await store.raise_budget(goal.id, day=600_000, actor="host")  # the total is not touched
    assert (day_only.total_budget, day_only.day_budget) == (2_000_000, 600_000)
    raised = await store.raise_budget(goal.id, total=2_200_000, actor="host")  # up, though still under spent
    assert raised.total_budget == 2_200_000
    with pytest.raises(GoalRefused, match="already spent"):
        await store.raise_budget(goal.id, total=2_100_000, actor="host")  # down, below what is spent
    assert (await store.get(goal.id)).total_budget == 2_200_000
