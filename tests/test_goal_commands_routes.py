"""A goal that may run any command, over the owner's routes: the create form's part only with the flag on, each refusal
in the store's own words, strict bodies, the guard before anything else, the return to contained (which reads the
runner's view again), the banner's read, the goal page's state of the mode, and the tools form's sentence of what can
never be allowed, as a function of the flag."""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from goal_routes_support import CHAN, PROXY, make_env, set_values
from sqlalchemy import select
from starlette.applications import Starlette

from nous.api.goal_routes import build_goal_routes
from nous.api.owner_guard import OwnerGuard
from nous.autonomy import tool_guard
from nous.autonomy.tool_guard import ToolGuard
from nous.goals import commands as C
from nous.goals.grants import NEVER_SENTENCE, NEVER_SENTENCE_WITH_COMMANDS, GoalToolGrants
from nous.storage.models import GoalJournal, GoalSession

pytestmark = pytest.mark.postgres_only

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
    "goal_commands_enabled": True,
}
NEW = {"title": "Ship the fix", "objective": "Push the fix to a branch", "done_when": "Pushed", "origin_channel": CHAN}
JSON = {"content-type": "application/json"}


def _env(db, **over):
    env = make_env(db, **{**ON, **over})
    assert env.settings.goal_commands_enabled is over.get("goal_commands_enabled", True)
    return env


def _ask(hours: int = 5, confirm: str | None = None) -> dict:
    return {"hours": hours, "confirm": C.confirm_sentence(hours) if confirm is None else confirm}


async def _create(env, *, headers=None, **commands) -> object:
    return await env.call("POST", "/goals", json={**NEW, "commands": _ask(**commands)}, headers=headers)


async def _rows(env, goal_id) -> list[GoalJournal]:
    async with env.store._db.session() as s:
        query = select(GoalJournal).where(GoalJournal.goal_id == goal_id).order_by(GoalJournal.seq)
        return list((await s.execute(query)).scalars())


# ---- the create form --------------------------------------------------------------------------------------------


async def test_options_carry_the_forms_words_only_with_the_flag_on(db):
    on = (await _env(db).call("GET", "/goals/options")).json()
    assert on["commands"] == {
        "max_hours": 24,
        "sentence": "allow this goal to run any command for {hours}",
        "hour_one": "{n} hour",
        "hours_many": "{n} hours",
        "words": dict(C.WORDS),
        "refusals": {"hours_range": C.HOURS_RANGE, "confirm": C.CONFIRM},
    }
    for hours in (1, 24):
        unit = on["commands"]["hour_one" if hours == 1 else "hours_many"].format(n=hours)
        assert on["commands"]["sentence"].format(hours=unit) == C.confirm_sentence(hours)
    off = (await _env(db, goal_commands_enabled=False).call("GET", "/goals/options")).json()
    assert "commands" not in off
    assert set(on) - {"commands"} == set(off)


async def test_the_owner_creates_a_goal_that_may_run_any_command(db):
    env = _env(db)
    response = await _create(env, headers={**PROXY, **JSON}, hours=5)
    assert response.status_code == 201
    body = response.json()
    assert (body["mode"], body["mode_hours"], body["mode_until"]) == ("commands", 5, None)
    assert body["commands"] == {
        "state": "set",
        "says": "Set: 5 hours from approval.",
        "usable": False,
        "not_usable_why": C.NOT_USABLE["draft"],
        "approve": "Approving starts its time: this goal's tasks may then run any command for 5 hours.",
    }
    armed = (await _rows(env, uuid.UUID(body["id"])))[-1]
    assert armed.evidence == ["mode:armed", "via:proxy"]
    assert "contained" in body["actions"] and "approve" in body["actions"]


@pytest.mark.parametrize(
    ("over", "commands", "sentence"),
    [
        ({"goal_commands_enabled": False}, {}, C.NOT_ALLOWED),
        ({}, {"hours": 25}, "Hours must be a whole number from 1 to 24."),
        ({}, {"hours": 0}, "Hours must be a whole number from 1 to 24."),
        ({}, {"hours": 5, "confirm": "allow this goal to run any command for 6 hours"}, C.CONFIRM),
    ],
)
async def test_each_refusal_is_the_stores_sentence_and_nothing_is_created(db, over, commands, sentence):
    env = _env(db, **over)
    response = await _create(env, **commands)
    assert (response.status_code, response.json()) == (409, {"error": sentence})
    assert (await env.call("GET", "/goals?state=all")).json()["goals"] == []


@pytest.mark.parametrize(
    "commands",
    [{"hours": "5", "confirm": "x"}, {"hours": 5}, {"hours": 5, "confirm": "x", "until": "later"}, {"hours": 5.0}],
)
async def test_the_commands_body_is_strict(db, commands):
    env = _env(db)
    response = await env.call("POST", "/goals", json={**NEW, "commands": commands})
    assert response.status_code == 400 and "commands" in response.json()["error"]


async def test_no_secret_is_refused_before_the_body_is_read(db):
    env = _env(db)
    for method, path in (("POST", "/goals"), ("POST", "/goals/G-0001/contained"), ("GET", "/goals/commands")):
        response = await env.call(method, path, headers={}, content=b"not json")
        assert response.status_code == 403, path


async def test_with_goals_off_the_return_answers_503_and_the_banner_read_nothing(db):
    env = make_env(db, enabled=False)
    response = await env.call("POST", "/goals/G-0001/contained")
    assert response.status_code == 503
    assert (await env.call("GET", "/goals/commands")).json() == {"enabled": False, "allowed": False, "active": []}


# ---- approval, the banner, the return ---------------------------------------------------------------------------


async def test_approval_starts_the_time_and_the_banner_lists_the_goal(db):
    env = _env(db)
    created = (await _create(env, hours=5)).json()
    approved = await env.call("POST", f"/goals/{created['ref']}/approve", headers={**PROXY, **JSON})
    body = approved.json()
    assert approved.status_code == 200 and body["commands"]["state"] == "on" and body["mode_until"] is not None
    assert (await _rows(env, uuid.UUID(body["id"])))[-1].evidence == ["mode:on", "via:proxy"]
    tool_guard.install(ToolGuard())
    detail = (await env.call("GET", f"/goals/{created['ref']}")).json()
    assert detail["commands"]["usable"] is True and detail["commands"]["not_usable_why"] is None
    banner = (await env.call("GET", "/goals/commands")).json()
    assert banner["enabled"] is True and banner["allowed"] is True
    [one] = banner["active"]
    assert one["ref"] == created["ref"] and one["title"] == "Ship the fix" and one["until"] == body["mode_until"]
    assert one["says"].startswith(f"{created['ref']} may run any command until ") and one["says"].endswith(" UTC.")


async def test_a_second_approval_into_the_mode_is_refused_in_the_stores_words(db):
    env = _env(db)
    first, second = (await _create(env)).json(), (await _create(env)).json()
    assert (await env.call("POST", f"/goals/{first['ref']}/approve")).status_code == 200
    response = await env.call("POST", f"/goals/{second['ref']}/approve")
    assert (response.status_code, response.json()) == (409, {"error": C.ONE_AT_A_TIME})


async def test_the_return_to_contained_reads_the_runners_view_again(db):
    tool_guard.install(ToolGuard())
    env = _env(db)
    view = GoalToolGrants(db, env.settings, commands=True)
    env.app = Starlette(
        routes=build_goal_routes(
            database=db, settings=env.settings, goals=env.store, guard=OwnerGuard(env.settings), grants=view
        )
    )
    created = (await _create(env)).json()
    goal_id = uuid.UUID(created["id"])
    await env.call("POST", f"/goals/{created['ref']}/approve")
    root = uuid.uuid4()
    async with db.session() as s:
        s.add(
            GoalSession(
                agent_id=env.store.agent_id,
                goal_id=goal_id,
                n=1,
                trigger="timer",
                first_subtask_id=uuid.uuid4(),
                root_id=root,
            )
        )
        await s.commit()
    assert await view.refresh() and view.commands_entry(root) is not None
    response = await env.call("POST", f"/goals/{created['ref']}/contained", headers={**PROXY, **JSON})
    body = response.json()
    assert response.status_code == 200 and (body["mode"], body["mode_hours"], body["mode_until"]) == (
        "contained",
        None,
        None,
    )
    assert view.commands_entry(root) is None
    assert body["commands"] == {
        "state": "ended",
        "says": "Ended: Returned to contained.",
        "usable": False,
        "not_usable_why": None,
        "approve": None,
    }
    assert "contained" not in body["actions"]
    assert (await _rows(env, goal_id))[-1].evidence == ["mode:off", "why:returned", "via:proxy"]
    count = len(await _rows(env, goal_id))
    again = await env.call("POST", f"/goals/{created['ref']}/contained")
    assert again.status_code == 200 and len(await _rows(env, goal_id)) == count
    assert (await env.call("GET", "/goals/commands")).json()["active"] == []


async def test_the_return_works_with_the_flag_off(db):
    env = _env(db)
    created = (await _create(env)).json()
    off = make_env(db, **{**ON, "goal_commands_enabled": False}, agent=env.settings.agent_id)
    response = await off.call("POST", f"/goals/{created['ref']}/contained")
    assert response.status_code == 200 and response.json()["mode"] == "contained"


# ---- the goal page ----------------------------------------------------------------------------------------------


async def test_a_contained_goal_says_off_and_an_ended_one_says_why(db):
    env = _env(db)
    plain = (await env.call("POST", "/goals", json=NEW)).json()
    assert plain["commands"] == {
        "state": "off",
        "says": "Off",
        "usable": False,
        "not_usable_why": None,
        "approve": None,
    }
    created = (await _create(env)).json()
    await env.call("POST", f"/goals/{created['ref']}/approve")
    stopped = (await env.call("POST", f"/goals/{created['ref']}/cancel")).json()
    assert stopped["commands"]["state"] == "ended"
    assert stopped["commands"]["says"] == f"Ended: {C.ENDED['cancelled']}"


@pytest.mark.parametrize(
    ("guard", "why"),
    [
        (None, C.NOT_USABLE["no_guard"]),
        (ToolGuard(locked=frozenset({"bash"})), C.NOT_USABLE[tool_guard.LOCK]),
        (ToolGuard(hold_all=True), C.NOT_USABLE[tool_guard.NOT_READ]),
        (ToolGuard(denied=frozenset({"bash"})), C.NOT_USABLE[tool_guard.OWNER]),
    ],
)
async def test_the_page_says_why_the_goals_tasks_do_not_run_any_command_now(db, guard, why):
    env = _env(db)
    created = (await _create(env)).json()
    await env.call("POST", f"/goals/{created['ref']}/approve")
    tool_guard.install(guard)
    detail = (await env.call("GET", f"/goals/{created['ref']}")).json()
    assert (detail["commands"]["usable"], detail["commands"]["not_usable_why"]) == (False, why)


async def test_usable_reads_the_guard_for_a_task_and_for_a_workflow_step(db, monkeypatch):
    env = _env(db)
    created = (await _create(env)).json()
    await env.call("POST", f"/goals/{created['ref']}/approve")
    seen: list[str] = []

    class Recording(ToolGuard):
        def reason(self, kind, session_id, tool):
            seen.append(kind)
            return tool_guard.LOCK if kind == "dag_node" else None

    tool_guard.install(Recording())
    detail = (await env.call("GET", f"/goals/{created['ref']}")).json()
    assert set(seen) == {"subtask", "dag_node"}
    assert detail["commands"]["not_usable_why"] == C.NOT_USABLE[tool_guard.LOCK]


async def test_an_end_that_has_passed_is_not_usable(db):
    env = _env(db)
    created = (await _create(env)).json()
    approved = (await env.call("POST", f"/goals/{created['ref']}/approve")).json()
    goal = await env.store.get(created["ref"])
    await set_values(env.store, goal, mode_until=goal.approved_at - timedelta(seconds=1))
    tool_guard.install(ToolGuard())
    detail = (await env.call("GET", f"/goals/{created['ref']}")).json()
    assert detail["commands"]["not_usable_why"] == C.NOT_USABLE["ended"] and approved["mode"] == "commands"
    assert (await env.call("GET", "/goals/commands")).json()["active"] == []


# ---- the tools form ---------------------------------------------------------------------------------------------


async def test_the_never_sentence_follows_the_flag(db):
    tool_guard.install(ToolGuard())
    on = _env(db)
    on_options = (await on.call("GET", "/goals/options")).json()
    off = _env(db, goal_commands_enabled=False)
    off_options = (await off.call("GET", "/goals/options")).json()
    # The tools form is read only with the grants view: build the routes with one.
    for env, expected in ((on, NEVER_SENTENCE_WITH_COMMANDS), (off, NEVER_SENTENCE)):
        view = GoalToolGrants(db, env.settings)
        env.app = Starlette(
            routes=build_goal_routes(
                database=db, settings=env.settings, goals=env.store, guard=OwnerGuard(env.settings), grants=view
            )
        )
        options = (await env.call("GET", "/goals/options")).json()
        assert options["tools"]["never"] == expected
        goal = (await env.call("POST", "/goals", json=NEW)).json()
        assert (await env.call("GET", f"/goals/{goal['ref']}/tools")).json()["never"] == expected
    assert "tools" not in on_options and "tools" not in off_options  # no grants view in the plain environment


def test_the_words_with_the_flag_on_drop_only_the_shell():
    from nous.goals import grants as G

    assert NEVER_SENTENCE == (
        "The shell and Python cannot be allowed here. "
        "Neither can scheduling, starting other work, or changing settings."
    )
    assert dict(G.NEVER_NAMED_WITH_COMMANDS) == {k: v for k, v in G.NEVER_NAMED.items() if k != "The shell"}
    for words in G.NEVER_NAMED_WITH_COMMANDS:
        assert words.lower() in NEVER_SENTENCE_WITH_COMMANDS.lower(), words
    assert "shell" not in NEVER_SENTENCE_WITH_COMMANDS.lower()


def test_the_words_table_has_its_keys():
    assert set(C.WORDS) == {
        "checkbox",
        "hours",
        "description",
        "heading",
        "off",
        "set",
        "on",
        "ended",
        "return",
        "return_note",
        "stops",
        "approve",
        "banner",
    }
    assert C.WORDS["description"] == (
        "This goal's tasks can run any command on the server Nous runs on, as the same user Nous runs as, and can "
        "start coding jobs. Nous does not pass its database login, model keys or chat tokens to those commands, but a "
        "command can read them from the running server, and can read and change Nous's files, settings and data. The "
        "code-host token is passed to them. Nous checks the end time before each command. A command already running "
        "at that time, anything it left running, and anything it changed stay as they are."
    )


async def test_with_the_flag_off_the_banner_read_says_not_allowed(db):
    env = _env(db, goal_commands_enabled=False)
    assert (await env.call("GET", "/goals/commands")).json() == {"enabled": True, "allowed": False, "active": []}


async def test_only_an_end_as_the_newest_change_of_mode_says_ended(db):
    from goal_routes_support import harness_row

    env = _env(db)
    plain = (await env.call("POST", "/goals", json=NEW)).json()
    goal = await env.store.get(plain["ref"])
    await harness_row(env.store, goal, "May run any command until 2026-01-01 00:00 UTC.", evidence=("mode:on",))
    assert (await env.call("GET", f"/goals/{plain['ref']}")).json()["commands"]["state"] == "off"
    await harness_row(env.store, goal, C.ENDED["expired"], evidence=("mode:off", "why:expired"))
    assert (await env.call("GET", f"/goals/{plain['ref']}")).json()["commands"]["says"] == "Ended: Its end time passed."
