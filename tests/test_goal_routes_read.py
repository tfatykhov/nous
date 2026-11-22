"""F114 goal routes: the reads (the list with its spend, the options, one goal in full), on Postgres."""

from __future__ import annotations

import re
import uuid
from dataclasses import asdict
from datetime import timedelta
from pathlib import Path

import pytest
from goal_routes_support import (
    CHAN,
    PACING_ON,
    active,
    add_session,
    db_now,
    draft,
    harness_row,
    make_env,
    paused,
    set_values,
)
from sqlalchemy import func, select

from nous.api.goal_routes import JOURNAL_MAX, masked_channel
from nous.goals.envelope import _RANGES, PRESETS
from nous.goals.seed import SPEND_NOTE
from nous.goals.spend import goal_spent
from nous.goals.store import SESSION_NOTE_LABEL, format_short_id
from nous.storage.models import Goal, GoalJournal, GoalSession

pytestmark = pytest.mark.postgres_only

# The dashboard's fixture of a goal's detail: the screen's tests read answers of this shape.
FIXTURES = Path(__file__).resolve().parents[1] / "dashboard-app" / "src" / "lib" / "testing" / "goalFixtures.ts"

ROW_KEYS = {
    "id", "ref", "title", "state", "pause_reason", "preset", "proposed_by", "total_budget", "day_budget",
    "spent_total", "spent_today", "deadline_at", "next_wake_at", "open_session", "pending_request", "mode",
    "updated_at",
}
DETAIL_KEYS = ROW_KEYS | {
    "enabled", "objective", "done_when", "origin_channel", "deadline_days", "sessions_per_day",
    "min_interval_seconds", "jobs_per_day", "quiet_hours_ok", "stall_limit", "mode_until", "mode_hours", "commands",
    "approved_at", "finished_at", "created_at", "plan", "sessions", "sessions_total", "journal", "pause_sentence",
    "actions", "resume_blocked", "spend_note",
}


async def _bulk_journal(env, goal, count: int) -> None:
    async with env.store._db.session() as s:
        for i in range(count):
            agent = env.store.agent_id
            s.add(GoalJournal(agent_id=agent, goal_id=goal.id, kind="progress", actor="chain", text=f"row {i}"))
        await s.commit()


async def test_list_returns_rows_with_spend_by_the_databases_day(db) -> None:
    env = make_env(db)
    first, second = await active(env.store), await active(env.store, title="Second")
    async with db.session() as s:
        day_start = (await s.execute(select(func.date_trunc("day", func.clock_timestamp(), "UTC")))).scalar_one()
    await add_session(env.store, first, n=1, spent=700, started_at=day_start - timedelta(hours=1))  # yesterday
    await add_session(env.store, first, n=2, spent=50, started_at=day_start + timedelta(seconds=1))
    await add_session(env.store, first, n=3, spent=5, open_=True)
    await add_session(env.store, second, n=1, spent=11, started_at=day_start - timedelta(days=3))
    response = await env.call("GET", "/goals")
    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is True
    rows = {row["id"]: row for row in body["goals"]}
    assert set(rows) == {str(first.id), str(second.id)}
    assert all(set(row) == ROW_KEYS for row in rows.values())
    one, two = rows[str(first.id)], rows[str(second.id)]
    assert (one["spent_total"], one["spent_today"], one["open_session"]) == (755, 55, 3)
    assert (two["spent_total"], two["spent_today"], two["open_session"]) == (11, 0, None)
    async with db.session() as s:  # one way of counting: the driver's
        for goal, row in ((first, one), (second, two)):
            assert (row["spent_total"], row["spent_today"]) == await goal_spent(s, env.store.agent_id, goal.id)
    assert one["ref"] == format_short_id(first.short_id) and one["state"] == "active" and one["mode"] == "contained"
    assert [row["id"] for row in body["goals"]] == [str(first.id), str(second.id)]  # by short id, newest last


async def test_list_filters_by_state_and_rejects_an_unknown_state(db) -> None:
    env = make_env(db)
    a_draft = await draft(env.store)
    an_active = await active(env.store)
    a_paused = await paused(env.store, "owner")
    cancelled = await draft(env.store)
    await env.store.cancel(cancelled.id, actor="owner")

    async def ids(query: str) -> set[str]:
        response = await env.call("GET", f"/goals{query}")
        assert response.status_code == 200
        return {row["id"] for row in response.json()["goals"]}

    assert await ids("?state=open") == {str(a_draft.id), str(an_active.id), str(a_paused.id)}
    assert await ids("") == await ids("?state=open")  # a poll never re-reads finished goals
    assert await ids("?state=cancelled") == {str(cancelled.id)}
    assert await ids("?state=finished") == {str(cancelled.id)}
    assert await ids("?state=all") == {str(a_draft.id), str(an_active.id), str(a_paused.id), str(cancelled.id)}
    bogus = await env.call("GET", "/goals?state=bogus")
    assert bogus.status_code == 400
    assert bogus.json() == {
        "error": "state must be one of draft, active, paused, done, failed, cancelled, open, finished or all"
    }


async def test_the_list_takes_the_newest_goals_up_to_its_limit_and_says_how_many_there_are(db) -> None:
    env = make_env(db)
    made = [await draft(env.store, title=f"Goal {i}") for i in range(3)]
    response = await env.call("GET", "/goals?state=all&limit=2")
    body = response.json()
    assert [row["id"] for row in body["goals"]] == [str(made[1].id), str(made[2].id)]  # the newest two, newest last
    assert body["total"] == 3
    assert (await env.call("GET", "/goals")).json()["total"] == 3
    await env.store.cancel(made[0].id, actor="owner")
    assert (await env.call("GET", "/goals")).json()["total"] == 2  # total follows the filter
    assert (await env.call("GET", "/goals?state=finished")).json()["total"] == 1
    assert (await env.call("GET", "/goals?state=all")).json()["total"] == 3
    for bad in ("0", "x", "-1", "1" * 10, "9" * 5000):
        refused = await env.call("GET", f"/goals?limit={bad}")
        assert refused.status_code == 400, bad
        assert refused.json() == {"error": "limit must be a whole number from 1 (at most 200 are sent)"}


async def test_the_list_sends_at_most_200_goals_however_many_are_asked_for(db) -> None:
    env = make_env(db)
    async with db.session() as s:
        for i in range(205):
            s.add(
                Goal(
                    agent_id=env.store.agent_id, title=f"Goal {i}", objective="o", done_when="d", preset="small",
                    total_budget=10, day_budget=1, deadline_days=3, sessions_per_day=2, min_interval_seconds=7200,
                    jobs_per_day=1, proposed_by="owner",
                )
            )
        await s.commit()
    body = (await env.call("GET", "/goals?limit=500")).json()
    assert (len(body["goals"]), body["total"]) == (200, 205)
    assert body["goals"][-1]["title"] == "Goal 204"  # the newest, last


async def test_detail_has_charter_limits_plan_sessions_journal_and_the_spend_note(db) -> None:
    env = make_env(db)
    goal = await active(env.store, preset="medium")
    await add_session(env.store, goal, n=1, spent=40)
    await add_session(env.store, goal, n=2, spent=2, open_=True)
    await _bulk_journal(env, goal, 60)
    response = await env.call("GET", f"/goals/{goal.id}")
    assert response.status_code == 200
    body = response.json()
    assert set(body) == DETAIL_KEYS
    fixture = FIXTURES.read_text(encoding="utf-8")
    keys = re.search(r"export const DETAIL_KEYS = \[(.*?)\];", fixture, re.S)
    assert keys is not None and re.findall(r"'(\w+)'", keys.group(1)) == list(body)
    assert f'"{SPEND_NOTE}"' in fixture  # the fixture's SPEND_NOTE is the server's sentence
    assert body["objective"] == goal.objective and body["done_when"] == goal.done_when
    assert body["origin_channel"] == "telegram:****" and body["proposed_by"] == "owner"
    assert (body["total_budget"], body["day_budget"], body["deadline_days"]) == (10_000_000, 2_000_000, 10)
    assert (body["sessions_per_day"], body["min_interval_seconds"], body["jobs_per_day"]) == (4, 3600, 2)
    assert body["quiet_hours_ok"] is False and body["stall_limit"] == 3
    assert [s["n"] for s in body["sessions"]] == [1, 2]
    assert body["sessions"][0]["outcome"] == "no_step" and body["sessions"][1]["ended_at"] is None
    assert body["spent_total"] == 42 and body["open_session"] == 2
    assert body["spend_note"] == SPEND_NOTE
    journal = body["journal"]
    assert len(journal) == 50
    assert [row["seq"] for row in journal] == sorted((row["seq"] for row in journal), reverse=True)
    assert journal[0]["text"] == "row 59"
    assert set(journal[0]) == {"seq", "session_n", "kind", "actor", "text", "evidence", "created_at"}
    five = await env.call("GET", f"/goals/{goal.id}?journal=5")
    assert len(five.json()["journal"]) == 5
    await _bulk_journal(env, goal, 200)
    clamped = await env.call("GET", f"/goals/{goal.id}?journal=999")
    assert len(clamped.json()["journal"]) == JOURNAL_MAX
    for bad in ("0", "x", "-1", "1.5", "1" * 10, "9" * 5000):
        assert (await env.call("GET", f"/goals/{goal.id}?journal={bad}")).status_code == 400, bad


async def test_detail_accepts_the_short_id_forms_and_404s_an_unknown_goal(db) -> None:
    env = make_env(db)
    goal = await draft(env.store)
    for ref in (format_short_id(goal.short_id), f"g{goal.short_id}", str(goal.short_id), str(goal.id)):
        response = await env.call("GET", f"/goals/{ref}")
        assert response.status_code == 200, ref
        assert response.json()["id"] == str(goal.id)
    for ref in (str(uuid.uuid4()), "not-a-goal", "0"):
        response = await env.call("GET", f"/goals/{ref}")
        assert (response.status_code, response.json()) == (404, {"error": f"no goal {ref}"})


async def test_another_agents_goal_is_not_visible(db) -> None:
    mine, theirs = make_env(db), make_env(db)
    their_goal = await active(theirs.store)
    listed = await mine.call("GET", "/goals")
    assert listed.json()["goals"] == []
    for ref in (format_short_id(their_goal.short_id), str(their_goal.id)):
        assert (await mine.call("GET", f"/goals/{ref}")).status_code == 404
        assert (await mine.call("POST", f"/goals/{ref}/pause", json={})).status_code == 404
    assert (await theirs.store.get(their_goal.id)).state == "active"


async def test_the_origin_channel_is_masked_in_the_detail(db) -> None:
    env = make_env(db)
    goal = await draft(env.store, origin_channel="telegram:-1009876543210")
    body = (await env.call("GET", f"/goals/{goal.id}")).json()
    assert body["origin_channel"] == "telegram:****3210"
    assert "9876543" not in str(body)
    assert masked_channel(None) is None and masked_channel(CHAN) == "telegram:****"


async def test_options_lists_the_presets_and_never_the_chat_id(db) -> None:
    with_chat = make_env(db, telegram_chat_id="5550001234567")
    response = await with_chat.call("GET", "/goals/options")
    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is True and body["driver_running"] is False
    assert body["presets"] == {name: asdict(limits) for name, limits in PRESETS.items()}
    assert body["ranges"] == [{"field": f, "low": lo, "high": hi, "words": w} for f, lo, hi, w in _RANGES]
    assert body["has_default_channel"] is True and body["channel_kinds"] == ["telegram"]
    assert body["spend_note"] == SPEND_NOTE
    assert "5550001234567" not in response.text and "1234567" not in response.text
    without = await make_env(db).call("GET", "/goals/options")
    assert without.json()["has_default_channel"] is False


async def test_the_plan_and_journal_text_come_back_unchanged_and_unmarked(db) -> None:
    env = make_env(db)
    goal = await active(env.store)
    text = "<script>x</script> & <b>bold</b> \"quoted\""
    await env.store.append_journal(goal.id, "report", text, actor="chain", evidence=["<i>ref</i>"])
    plan = [
        "a string",
        None,
        [1, 2],
        {"text": "<b>step</b>", "status": "open", "evidence": ["ref-1", 3, None], "closed_session": 2, "extra": "x"},
        {"text": 5, "status": None, "evidence": "not a list", "closed_session": True},
        {"text": "y" * 5000, "status": "done"},
    ]
    await set_values(env.store, goal, plan=plan)
    body = (await env.call("GET", f"/goals/{goal.id}")).json()
    assert body["journal"][0]["text"] == text
    assert body["journal"][0]["evidence"] == ["<i>ref</i>"]
    steps = body["plan"]
    assert steps[0] == {"text": "<b>step</b>", "status": "open", "evidence": ["ref-1"], "closed_session": 2}
    assert steps[1] == {}
    assert len(steps[2]["text"]) == 4000 and steps[2]["status"] == "done"
    assert len(steps) == 3


async def test_the_detail_sends_the_newest_sessions_and_how_many_there_are(db) -> None:
    env = make_env(db)
    goal = await active(env.store)
    async with db.session() as s:
        for n in range(1, 56):
            s.add(
                GoalSession(
                    agent_id=env.store.agent_id, goal_id=goal.id, n=n, trigger="timer", first_subtask_id=uuid.uuid4(),
                    ended_at=func.clock_timestamp(), outcome="no_step",
                )
            )
        await s.commit()
    body = (await env.call("GET", f"/goals/{goal.id}")).json()
    assert [row["n"] for row in body["sessions"]] == list(range(6, 56))
    assert body["sessions_total"] == 55


async def test_the_pause_sentence_is_the_harness_row_with_the_pauses_evidence_ref(db) -> None:
    env = make_env(db)
    goal = await paused(env.store, "error")
    await harness_row(env.store, goal, "Paused: 3 sessions in a row ended in an error.", evidence=("stop:error",))
    await env.store.raise_budget(goal.id, total=goal.total_budget * 2, actor="owner")  # a newer state row, the owner's
    # Harness rows written after the pause, as the driver writes them: a session's close, with and without the
    # session's note, a chain being stopped, and a pause row for another reason. None of them is this pause's row.
    closed = "Session 3 ended: no_step, 10 tokens."
    await harness_row(env.store, goal, closed, session_n=3, evidence=("arrivals:4",))
    await harness_row(env.store, goal, f"A row of the session's.\n{SESSION_NOTE_LABEL} It was hard.", session_n=3)
    stopping = "Session 3 is being stopped: a task of its chain ran past its time limit."
    await harness_row(env.store, goal, stopping, session_n=3, evidence=(f"ending:{uuid.uuid4()}",))
    await harness_row(env.store, goal, "Paused: the deadline has passed.", evidence=("stop:deadline",))
    for i in range(55):  # newer rows beyond the journal the detail sends
        await env.store.append_journal(goal.id, "progress", f"progress {i}", actor="chain")
    body = (await env.call("GET", f"/goals/{goal.id}")).json()
    assert body["journal"][0]["actor"] == "chain"
    assert body["pause_sentence"] == "Paused: 3 sessions in a row ended in an error."
    long = await paused(env.store, "error")
    await harness_row(env.store, long, "w" * 1000, evidence=("stop:error",))
    clipped = (await env.call("GET", f"/goals/{long.id}")).json()["pause_sentence"]
    assert len(clipped) == 300 and clipped.endswith("[clipped]")
    no_row = await paused(env.store, "stalled")
    await harness_row(env.store, no_row, "Session 2 is being stopped: the owner asked to stop it.")
    assert (await env.call("GET", f"/goals/{no_row.id}")).json()["pause_sentence"] is None
    by_owner = await paused(env.store, "owner")
    await harness_row(env.store, by_owner, "An older harness row.", evidence=("stop:error",))
    assert (await env.call("GET", f"/goals/{by_owner.id}")).json()["pause_sentence"] is None
    running = await active(env.store)
    assert (await env.call("GET", f"/goals/{running.id}")).json()["pause_sentence"] is None


async def test_of_two_pause_rows_the_newest_is_the_pause_sentence(db) -> None:
    env = make_env(db)
    goal = await paused(env.store, "error")
    await harness_row(env.store, goal, "Paused: 3 sessions in a row ended in an error.", evidence=("stop:error",))
    failed = "Session 4 could not start: RuntimeError.\nPaused: the start failed 3 times in a row."
    await harness_row(env.store, goal, failed, evidence=("stop:error",))
    body = (await env.call("GET", f"/goals/{goal.id}")).json()
    assert body["pause_sentence"] == failed


async def test_a_paused_goals_resume_blocked_follows_its_spend_and_deadline(db) -> None:
    env = make_env(db)
    budget = await paused(env.store, "budget")
    await add_session(env.store, budget, n=1, spent=budget.total_budget)
    deadline = await paused(env.store, "deadline")
    await set_values(env.store, deadline, deadline_at=await db_now(env.store) - timedelta(hours=1))
    owner = await paused(env.store, "owner")
    await add_session(env.store, owner, n=1, spent=owner.total_budget)
    answers = {g.id: (await env.call("GET", f"/goals/{g.id}")).json() for g in (budget, deadline, owner)}
    assert answers[budget.id]["resume_blocked"] == "budget" and "resume" not in answers[budget.id]["actions"]
    assert answers[deadline.id]["resume_blocked"] == "deadline" and "resume" not in answers[deadline.id]["actions"]
    assert answers[owner.id]["resume_blocked"] is None and "resume" in answers[owner.id]["actions"]
    await env.store.raise_budget(budget.id, total=budget.total_budget * 2, actor="owner")
    after = (await env.call("GET", f"/goals/{budget.id}")).json()
    assert after["resume_blocked"] is None and "resume" in after["actions"]


# ---- goal pacing ----------------------------------------------------------------------------------------------------

OPTION_KEYS = {
    "enabled", "presets", "ranges", "has_default_channel", "channel_kinds", "bounds", "driver_running", "spend_note",
}


async def test_with_pacing_on_the_detail_has_the_time_and_the_options_the_zone(db) -> None:
    env = make_env(db, **PACING_ON)
    paced = await draft(env.store, daily_at_minute=540)
    plain = await draft(env.store)
    body = (await env.call("GET", f"/goals/{paced.id}")).json()
    assert set(body) == DETAIL_KEYS | {"daily_at"} and body["daily_at"] == "09:00"
    assert list(body).index("daily_at") == list(body).index("stall_limit") + 1
    assert (await env.call("GET", f"/goals/{plain.id}")).json()["daily_at"] is None
    options = (await env.call("GET", "/goals/options")).json()
    assert set(options) == OPTION_KEYS | {"pacing"}
    assert options["pacing"] == {
        "zone": "America/New_York",
        "stall_limit": {"low": 1, "high": 10},
        "default_at": "09:00",
    }
    listed = (await env.call("GET", "/goals")).json()["goals"]
    assert all(set(row) == ROW_KEYS for row in listed)


async def test_with_pacing_on_sessions_is_offered_for_a_draft_an_active_and_a_paused_goal(db) -> None:
    env = make_env(db, **PACING_ON)
    goals = [await draft(env.store), await active(env.store), await paused(env.store, "owner")]
    finished = await draft(env.store)
    await env.store.cancel(finished.id, actor="owner")
    for goal in goals:
        actions = (await env.call("GET", f"/goals/{goal.id}")).json()["actions"]
        assert actions.index("sessions") == actions.index("budget") + 1, actions
    assert (await env.call("GET", f"/goals/{finished.id}")).json()["actions"] == []


async def test_with_pacing_off_the_answers_are_as_before(db) -> None:
    env = make_env(db)
    goal = await active(env.store)
    await set_values(env.store, goal, daily_at_minute=540)  # a time written by hand is not shown
    for state in ("active", "paused", "draft"):
        if state == "paused":
            goal = await paused(env.store, "owner")
        if state == "draft":
            goal = await draft(env.store)
        body = (await env.call("GET", f"/goals/{goal.id}")).json()
        assert set(body) == DETAIL_KEYS and "sessions" not in body["actions"], state
    options = (await env.call("GET", "/goals/options")).json()
    assert set(options) == OPTION_KEYS
