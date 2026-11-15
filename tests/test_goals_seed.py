"""F114 goals: the host command (python -m nous.goals.seed), run against a test database (no network)."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from nous.config import Settings
from nous.goals import seed
from nous.goals.store import GoalStore
from nous.storage.models import GoalJournal, GoalSession

pytestmark = pytest.mark.postgres_only

ON = {"result_inbox_enabled": True, "intentions_enabled": True, "continuation_enabled": True, "goals_enabled": True}
CREATE = ["create", "--title", "Tidy the notes", "--objective", "Merge duplicate notes", "--done-when", "None left"]


def _env(db, **over) -> tuple[Settings, GoalStore]:
    agent = f"goals-seed-{uuid.uuid4().hex[:8]}"
    settings = Settings(_env_file=None, agent_id=agent, **{"telegram_chat_id": "", **ON, **over})
    return settings, GoalStore(db, agent, settings)


async def _run(settings: Settings, store: GoalStore, *argv: str) -> int:
    return await seed.run(seed._parse_args(list(argv)), settings, store)


async def _created(settings, store, capsys, *extra: str) -> str:
    assert await _run(settings, store, *CREATE, "--origin-channel", "telegram:4242", *extra) == 0
    (goal,) = await store.list()
    capsys.readouterr()
    return f"G-{goal.short_id:04d}"


async def test_create_then_approve_then_show(db, capsys):
    settings, store = _env(db)
    assert await _run(settings, store, *CREATE, "--preset", "medium", "--origin-channel", "telegram:4242") == 0
    (goal,) = await store.list()
    label = f"G-{goal.short_id:04d}"
    assert f"{label} draft" in capsys.readouterr().out
    assert await _run(settings, store, "approve", label) == 0
    assert f"{label} active" in capsys.readouterr().out
    assert await _run(settings, store, "show", label) == 0
    out = capsys.readouterr().out
    assert "Merge duplicate notes" in out and "None left" in out
    assert "medium" in out and "10000000" in out
    assert "Journal" in out and "Approved" in out


async def test_a_refusal_exits_2_and_prints_the_reason(db, capsys):
    settings, store = _env(db)
    assert await _run(settings, store, *CREATE) == 0  # no origin channel, and no default chat
    (goal,) = await store.list()
    capsys.readouterr()
    assert await _run(settings, store, "approve", f"G-{goal.short_id:04d}") == 2
    assert "nowhere to ask the owner" in capsys.readouterr().err
    assert (await store.get(goal.id)).state == "draft"
    assert await _run(settings, store, "pause", "G-99999999") == 2
    assert "no goal" in capsys.readouterr().err


async def test_a_bad_envelope_exits_2(db, capsys):
    settings, store = _env(db)
    assert await _run(settings, store, *CREATE, "--total", "1000", "--day", "2000") == 2
    assert "day budget" in capsys.readouterr().err
    assert await store.list() == []


async def test_stop_sets_the_request_and_pauses(db, capsys):
    settings, store = _env(db)
    label = await _created(settings, store, capsys)
    assert await _run(settings, store, "approve", label) == 0
    goal = await store.get(label)
    async with db.session() as s:
        s.add(
            GoalSession(agent_id=store.agent_id, goal_id=goal.id, n=1, trigger="timer", first_subtask_id=uuid.uuid4())
        )
        await s.commit()
    assert await _run(settings, store, "stop", label) == 0
    after = await store.get(label)
    assert (after.state, after.pause_reason, after.pending_request) == ("paused", "owner", "stop")
    assert "stop" in capsys.readouterr().out


async def test_every_change_is_journalled_with_actor_host(db, capsys):
    settings, store = _env(db)
    label = await _created(settings, store, capsys)
    for argv in (
        ["approve", label],
        ["run-now", label],
        ["pause", label],
        ["resume", label],
        ["note", label, "Look in the archive too"],
        ["raise-budget", label, "--total", "3000000", "--day", "600000"],
        ["extend", label, "--days", "2"],
        ["cancel", label],
    ):
        assert await _run(settings, store, *argv) == 0, argv
    goal = await store.get(label)
    assert (goal.state, goal.total_budget, goal.day_budget, goal.deadline_days) == ("cancelled", 3_000_000, 600_000, 5)
    async with db.session() as s:
        rows = list((await s.execute(select(GoalJournal).where(GoalJournal.goal_id == goal.id))).scalars())
    assert len(rows) == 9  # created, then one per change
    assert {row.actor for row in rows} == {"host"}


async def test_show_says_what_spend_leaves_out(db, capsys):
    settings, store = _env(db)
    label = await _created(settings, store, capsys)
    assert await _run(settings, store, "show", label) == 0
    out = capsys.readouterr().out
    assert "not counted" in out and "cache" in out and "jobs" in out


async def test_with_the_flag_off_a_change_is_refused_and_list_and_show_still_work(db, capsys):
    settings, store = _env(db)
    label = await _created(settings, store, capsys)
    off = Settings(_env_file=None, agent_id=store.agent_id, telegram_chat_id="")
    assert off.goals_enabled is False
    goal = await store.get(label)
    journal_before = await store.journal(goal.id, limit=100)
    assert await _run(off, store, "approve", label) == 2
    captured = capsys.readouterr()
    assert captured.err == seed.FLAG_OFF_LINE + "\n" and "goals are off" in captured.err
    assert captured.out == ""
    assert (await store.get(label)).state == "draft"
    assert await _run(off, store, *CREATE) == 2
    assert capsys.readouterr().err == seed.FLAG_OFF_LINE + "\n"
    assert len(await store.list()) == 1
    assert len(await store.journal(goal.id, limit=100)) == len(journal_before)  # nothing written
    assert await _run(off, store, "list") == 0
    assert label in capsys.readouterr().out
    assert await _run(off, store, "show", label) == 0
    assert label in capsys.readouterr().out
