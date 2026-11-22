"""F114 goal routes: a goal's ``actions`` are exactly the changes the real store accepts, and ``resume_blocked`` is
exactly why it refuses a resume.

Each case puts a fresh goal in one condition (its state, pause reason, an open session, a pending request, its spend,
its deadline, its channel); for each action a goal of its own is put in that condition, its detail is read over HTTP,
and the store method of that action is called on it with the owner as the actor. The action is listed exactly when the
store accepts it."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any

import pytest
from goal_routes_support import (
    PACING_ON,
    Env,
    active,
    add_session,
    deadline_in,
    draft,
    journal_rows,
    make_env,
    paused,
    reread,
    set_values,
)

from nous.api.goal_routes import ACTIONS
from nous.goals import pacing
from nous.goals.store import GoalRefused, GoalStore
from nous.storage.models import Goal

pytestmark = pytest.mark.postgres_only

REASONS = ("owner", "budget", "stalled", "waiting_for_owner", "error", "deadline")

async def _contained(store: GoalStore, goal: Goal) -> Any:
    """The return to contained, which answers a goal that is contained already as it is: accepted only when it
    changed the goal."""
    if goal.mode == "contained":
        raise GoalRefused("the goal is contained already")
    return await store.return_to_contained(goal.id, actor="owner")


# One canonical call per action, with the owner as the actor.
CALLS: dict[str, Callable[[GoalStore, Goal], Awaitable[Any]]] = {
    "approve": lambda s, g: s.approve(g.id, actor="owner"),
    "run-now": lambda s, g: s.request_run_now(g.id, actor="owner"),
    "resume": lambda s, g: s.resume(g.id, actor="owner"),
    "confirm-done": lambda s, g: s.confirm_done(g.id, actor="owner"),
    "pause": lambda s, g: s.pause(g.id, actor="owner"),
    "stop": lambda s, g: s.stop(g.id, actor="owner"),
    "budget": lambda s, g: s.raise_budget(g.id, total=g.total_budget + 1, actor="owner"),
    "sessions": lambda s, g: s.change_sessions(g.id, changes={"stall_limit": g.stall_limit % 10 + 1}, actor="owner"),
    "deadline": lambda s, g: s.extend_deadline(g.id, days=1, actor="owner"),
    "note": lambda s, g: s.add_note(g.id, "A note.", actor="owner"),
    "cancel": lambda s, g: s.cancel(g.id, actor="owner"),
    "contained": _contained,
}

# (state, options) per case. Options: reason, session (an open session), request (a pending one), spent ("all": the
# total budget), deadline (days from now), channel (False: none), default_chat, deadline_days (a draft's length).
CASES: dict[str, dict[str, Any]] = {
    "draft": {"state": "draft"},
    "draft-no-channel": {"state": "draft", "channel": False},
    "draft-no-channel-default-chat": {"state": "draft", "channel": False, "default_chat": True},
    "draft-at-the-deadline-cap": {"state": "draft", "deadline_days": 90},
    "active": {"state": "active"},
    "active-session-open": {"state": "active", "session": True},
    "active-run-now-pending": {"state": "active", "request": "run_now"},
    "active-session-open-stop-pending": {"state": "active", "session": True, "request": "stop"},
    "active-budget-spent": {"state": "active", "spent": "all"},
    "active-deadline-near-the-cap": {"state": "active", "deadline": 89.5},
    **{f"paused-{reason}": {"state": "paused", "reason": reason} for reason in REASONS},
    **{f"paused-{reason}-session-open": {"state": "paused", "reason": reason, "session": True} for reason in REASONS},
    "paused-budget-spent": {"state": "paused", "reason": "budget", "spent": "all"},
    "paused-budget-spent-session-open": {"state": "paused", "reason": "budget", "spent": "all", "session": True},
    "paused-deadline-passed": {"state": "paused", "reason": "deadline", "deadline": -0.05},
    "paused-owner-budget-spent": {"state": "paused", "reason": "owner", "spent": "all"},
    "paused-owner-deadline-passed": {"state": "paused", "reason": "owner", "deadline": -0.05},
    "paused-waiting-stop-pending": {
        "state": "paused", "reason": "waiting_for_owner", "session": True, "request": "stop",
    },
    "done": {"state": "done"},
    "failed": {"state": "failed"},
    "cancelled": {"state": "cancelled"},
    "cancelled-session-open": {"state": "cancelled", "session": True},
    # A goal that may run any command (a draft's choice, or an end set at approval)
    "draft-armed": {"state": "draft", "commands": True},
    "active-commands": {"state": "active", "commands": True},
    "active-commands-session-open": {"state": "active", "commands": True, "session": True},
    "paused-owner-commands": {"state": "paused", "reason": "owner", "commands": True},
}


def _env(db, case: dict[str, Any], *, pacing_on: bool = False) -> Env:
    return make_env(
        db,
        **({"telegram_chat_id": "5550001"} if case.get("default_chat") else {}),
        **(PACING_ON if pacing_on else {}),
    )


async def _goal(env: Env, case: dict[str, Any]) -> Goal:
    """A fresh goal in the case's condition."""
    store = env.store
    assert store is not None
    over: dict[str, Any] = {}
    if case.get("channel") is False:
        over["origin_channel"] = None
    if "deadline_days" in case:
        over["overrides"] = {"deadline_days": case["deadline_days"]}
    state = case["state"]
    if state == "draft":
        goal = await draft(store, **over)
    elif state == "paused":
        goal = await paused(store, case["reason"], **over)
    else:
        goal = await active(store, **over)
    if case.get("spent") == "all":
        await add_session(store, goal, n=1, spent=goal.total_budget)
    if case.get("session"):
        await add_session(store, goal, n=2, open_=True)
    if "deadline" in case:
        await deadline_in(store, goal, case["deadline"])
    if case.get("request"):
        await set_values(store, goal, pending_request=case["request"], pending_request_at=goal.updated_at)
    if case.get("commands"):
        until = goal.approved_at + timedelta(hours=5) if goal.approved_at is not None else None
        await set_values(store, goal, mode="commands", mode_hours=5, mode_until=until)
    if state in ("done", "failed"):
        await set_values(store, goal, state=state, next_wake_at=None, finished_at=goal.updated_at)
    if state == "cancelled":
        await set_values(
            store, goal, state="cancelled", next_wake_at=None, finished_at=goal.updated_at, pending_request=None,
            pending_request_at=None,
        )
    return await reread(store, goal)


def _expected_resume_blocked(case: dict[str, Any]) -> str | None:
    if case["state"] != "paused":
        return None
    if case["reason"] == "budget" and case.get("spent") == "all":
        return "budget"
    if case["reason"] == "deadline" and case.get("deadline", 1) < 0:
        return "deadline"
    return None


@pytest.mark.parametrize("pacing_on", [False, True], ids=["pacing_off", "pacing_on"])
@pytest.mark.parametrize("name", list(CASES))
async def test_actions_equal_what_the_real_store_accepts(db, name: str, pacing_on: bool) -> None:
    case = CASES[name]
    env = _env(db, case, pacing_on=pacing_on)
    accepted: list[str] = []
    listed: list[str] | None = None
    for action in ACTIONS:
        goal = await _goal(env, case)
        detail = (await env.call("GET", f"/goals/{goal.id}")).json()
        assert detail["resume_blocked"] == _expected_resume_blocked(case)
        listed = detail["actions"] if listed is None else listed
        assert detail["actions"] == listed  # every goal of the case is in the same condition
        try:
            await CALLS[action](env.store, goal)
        except GoalRefused as refusal:
            assert action not in listed, f"{name}: {action} is listed, and the store said: {refusal}"
        else:
            accepted.append(action)
            assert action in listed, f"{name}: the store accepts {action}, and it is not listed"
    assert listed == accepted  # in ACTIONS order, nothing else listed
    assert ("sessions" in listed) is (pacing_on and case["state"] in ("draft", "active", "paused"))


def test_the_cases_reach_every_state_and_pause_reason_and_every_action_has_a_call() -> None:
    assert {case["state"] for case in CASES.values()} == {"draft", "active", "paused", "done", "failed", "cancelled"}
    assert {case["reason"] for case in CASES.values() if "reason" in case} == set(REASONS)
    assert list(CALLS) == list(ACTIONS)


# ---- goal pacing: POST /goals/{ref}/sessions ---------------------------------------------------------------------


async def test_sessions_changes_the_settings_and_answers_the_goals_detail(db) -> None:
    env = make_env(db, **PACING_ON)
    goal = await active(env.store)
    body = {"sessions_per_day": 1, "stall_limit": 5, "quiet_hours_ok": True, "daily": True, "daily_at": "09:30"}
    response = await env.call("POST", f"/goals/{goal.id}/sessions", json=body)
    assert response.status_code == 200, response.text
    answer = response.json()
    assert answer["id"] == str(goal.id) and "sessions" in answer["actions"]
    assert (answer["sessions_per_day"], answer["stall_limit"], answer["quiet_hours_ok"]) == (1, 5, True)
    assert (answer["daily_at"], answer["preset"], answer["state"]) == ("09:30", "custom", "active")
    # once a day changed on an active goal with no session open: its wake is now, so the driver decides again
    assert datetime.fromisoformat(answer["next_wake_at"]) >= goal.next_wake_at
    assert answer["journal"][0]["actor"] == "owner"
    assert answer["journal"][0]["text"].startswith("Session settings: sessions per day 2 -> 1; ")
    off = await env.call("POST", f"/goals/{goal.id}/sessions", json={"daily": False})
    assert off.status_code == 200 and off.json()["daily_at"] is None
    assert (await reread(env.store, goal)).daily_at_minute is None


async def test_sessions_refused_by_the_store_is_409_in_its_words(db) -> None:
    env = make_env(db, **PACING_ON)
    goal = await active(env.store)
    for body, words in (
        ({"stall_limit": 11}, "the stall limit must be a whole number from 1 to 10"),
        ({"sessions_per_day": 2}, "nothing would change"),
        ({}, "give at least one session setting to change"),
    ):
        response = await env.call("POST", f"/goals/{goal.id}/sessions", json=body)
        assert response.status_code == 409 and words in response.json()["error"], (body, response.text)
    finished = await draft(env.store)
    await env.store.cancel(finished.id, actor="owner")
    response = await env.call("POST", f"/goals/{finished.id}/sessions", json={"stall_limit": 4})
    assert response.status_code == 409 and "session settings can change" in response.json()["error"]


async def test_sessions_with_pacing_off_is_409_and_writes_nothing(db) -> None:
    env = make_env(db)
    goal = await active(env.store)
    before = await journal_rows(env.store, goal)
    response = await env.call("POST", f"/goals/{goal.id}/sessions", json={"sessions_per_day": 1})
    assert (response.status_code, response.json()) == (409, {"error": pacing.CHANGE_OFF})
    after = await reread(env.store, goal)
    assert (after.sessions_per_day, after.updated_at) == (goal.sessions_per_day, goal.updated_at)
    assert len(await journal_rows(env.store, goal)) == len(before)


@pytest.mark.parametrize(
    ("body", "field"),
    [
        ({"actor": "host", "stall_limit": 4}, "actor"),
        ({"preset": "large"}, "preset"),
        ({"day_budget": 5}, "day_budget"),
        ({"daily_at": "09:00"}, "daily_at"),
        ({"daily": False, "daily_at": "09:00"}, "daily_at"),
        ({"daily": True}, "daily_at"),
        ({"daily": True, "daily_at": "9:00"}, "daily_at"),
        ({"daily": True, "daily_at": "24:00"}, "daily_at"),
        ({"daily": True, "daily_at": 540}, "daily_at"),
        ({"daily": "yes", "daily_at": "09:00"}, "daily"),
        ({"stall_limit": "4"}, "stall_limit"),
        ({"quiet_hours_ok": 1}, "quiet_hours_ok"),
    ],
)
async def test_a_sessions_body_out_of_shape_is_400_naming_the_field(db, body: dict, field: str) -> None:
    env = make_env(db, **PACING_ON)
    goal = await active(env.store)
    response = await env.call("POST", f"/goals/{goal.id}/sessions", json=body)
    assert response.status_code == 400, response.text
    error = response.json()["error"]
    assert error.startswith(f"{field}: ") or f"; {field}: " in error, error
    assert len(await journal_rows(env.store, goal)) == 2  # created and approved: nothing more
