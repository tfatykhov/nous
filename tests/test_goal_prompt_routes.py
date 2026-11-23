"""``GET``, ``PUT`` and ``DELETE /goals/prompt``: the owner's default goal prompt, and what the goal driver's first task
and a goal session's follow-up turn read, as the owner changes it."""

from __future__ import annotations

import asyncio
import json
import uuid

import pytest
from f099_support import OWNER_HEADERS, env_factory, runner_env  # noqa: F401
from goal_routes_support import FORBIDDEN, NOT_JSON, OWNER, PROXY, make_env, send
from goals_support import active, reset_goal_report_channel, started, world  # noqa: F401
from sqlalchemy import select, text
from starlette.applications import Starlette

from nous.api.goal_routes import GOALS_OFF, PROMPT_CHANGED, PROMPT_EMPTY, build_goal_routes
from nous.api.owner_guard import OwnerGuard
from nous.goals.default_prompt import DEFAULT_GOAL_PROMPT, HEADING, MAX_CHARS
from nous.heart.schemas import CensorInput
from nous.identity.manager import GOAL_PROMPT_SECTION, IdentityManager
from nous.storage.models import AgentIdentity, GoalJournal, Subtask

pytestmark = pytest.mark.postgres_only

PATH = "/goals/prompt"
JSON = {"Content-Type": "application/json"}
SAVED = "- Merge one folder at a time, and say which folders are left."


async def add_agent(db, agent_id: str) -> None:
    async with db.session() as s:
        await s.execute(
            text("INSERT INTO nous_system.agents (id, name, config) VALUES (:id, 'x', '{}')"), {"id": agent_id}
        )
        await s.commit()


async def _env(db, **over):
    env = make_env(db, **over)
    await add_agent(db, env.settings.agent_id)
    return env


async def _current(db, agent_id: str) -> list[AgentIdentity]:
    async with db.session() as s:
        rows = await s.execute(
            select(AgentIdentity).where(
                AgentIdentity.agent_id == agent_id,
                AgentIdentity.section == GOAL_PROMPT_SECTION,
                AgentIdentity.is_current.is_(True),
            )
        )
        return list(rows.scalars())


# ---- the routes ----------------------------------------------------------------------------------------------------


async def test_with_nothing_saved_the_read_shows_the_built_in_text(db):
    env = await _env(db)
    answer = await env.call("GET", PATH)
    assert answer.status_code == 200
    assert answer.json() == {
        "enabled": True,
        "text": DEFAULT_GOAL_PROMPT,
        "is_default": True,
        "default": DEFAULT_GOAL_PROMPT,
        "max_chars": MAX_CHARS,
        "updated_at": None,
        "updated_by": None,
    }


async def test_a_save_is_read_back_with_the_proof_the_guard_accepted(db):
    env = await _env(db)
    saved = await env.call("PUT", PATH, json={"text": SAVED})
    assert saved.status_code == 200
    body = saved.json()
    assert (body["text"], body["is_default"], body["updated_by"]) == (SAVED, False, "owner")
    assert body["updated_at"] is not None and body["default"] == DEFAULT_GOAL_PROMPT
    assert (await env.call("GET", PATH)).json() == body
    by_proxy = await env.call("PUT", PATH, headers={**PROXY, **JSON}, content=json.dumps({"text": "Work small."}))
    assert by_proxy.status_code == 200 and by_proxy.json()["updated_by"] == "proxy"


async def test_a_body_never_names_who_saved_it(db):
    env = await _env(db)
    answer = await env.call("PUT", PATH, json={"text": SAVED, "updated_by": "chain"})
    assert answer.status_code == 400 and "updated_by" in answer.json()["error"]
    assert await _current(db, env.settings.agent_id) == []


async def test_a_reset_goes_back_to_the_built_in_text_and_may_be_repeated(db):
    env = await _env(db)
    await env.call("PUT", PATH, json={"text": SAVED})
    for _ in range(2):
        answer = await env.call("DELETE", PATH)
        assert answer.status_code == 200
        body = answer.json()
        assert (body["text"], body["is_default"], body["updated_by"]) == (DEFAULT_GOAL_PROMPT, True, None)
    assert (await env.call("DELETE", PATH, json={"text": SAVED})).status_code == 400  # a reset takes no field


@pytest.mark.parametrize("value", ["", "   ", "\n\r\n\t"])
async def test_a_blank_text_is_refused_with_a_sentence(db, value):
    env = await _env(db)
    answer = await env.call("PUT", PATH, json={"text": value})
    assert answer.status_code == 400 and answer.json() == {"error": PROMPT_EMPTY}


async def test_a_text_over_the_limit_is_refused_with_its_length(db):
    env = await _env(db)
    answer = await env.call("PUT", PATH, json={"text": "a" * (MAX_CHARS + 1)})
    assert answer.status_code == 400
    assert answer.json() == {"error": f"The text is {MAX_CHARS + 1} characters; at most {MAX_CHARS} are allowed."}
    assert await _current(db, env.settings.agent_id) == []


@pytest.mark.parametrize(
    "value",
    [
        "a" * MAX_CHARS,
        "a" * MAX_CHARS + "\n",
        "a" * 700 + "\r\n" + "b" * (MAX_CHARS - 701),
        "\r\n" + "c" * MAX_CHARS + "\r\n",
    ],
)
async def test_a_text_at_the_limit_once_counted_is_saved(db, value):
    env = await _env(db)
    answer = await env.call("PUT", PATH, json={"text": value})
    assert answer.status_code == 200
    saved = answer.json()["text"]
    assert len(saved) == MAX_CHARS and "\r" not in saved


@pytest.mark.parametrize(
    "body",
    [
        b'{"text": 5}',
        b'{"text": null}',
        b'{"text": ["a"]}',
        b'{"text": "a\\u0000b"}',
        b'{"text": "a\\ud800b"}',
        b'{"text": "a", "extra": 1}',
        b"{}",
        b"[]",
        b"not json",
        json.dumps({"text": "a" * 20_001}).encode(),
    ],
)
async def test_a_body_that_is_not_one_text_is_refused(db, body):
    env = await _env(db)
    answer = await env.call("PUT", PATH, content=body, headers={**OWNER, **JSON})
    assert answer.status_code == 400, answer.text
    assert await _current(db, env.settings.agent_id) == []


async def test_two_saves_at_once_leave_one_current_text(db):
    env = await _env(db)
    answers = await asyncio.gather(*(env.call("PUT", PATH, json={"text": f"Work small, take {n}."}) for n in "ab"))
    codes = sorted(answer.status_code for answer in answers)
    assert codes in ([200, 200], [200, 409]), codes
    for answer in answers:
        if answer.status_code == 409:
            assert answer.json() == {"error": PROMPT_CHANGED}
    assert len(await _current(db, env.settings.agent_id)) == 1


async def test_each_route_needs_the_owners_or_the_proxys_secret_before_the_body_is_read(db):
    env = await _env(db)
    for method in ("GET", "PUT", "DELETE"):
        answer = await env.call(method, PATH, headers={}, content=b"not json")
        assert (answer.status_code, answer.json()) == (403, FORBIDDEN), method


@pytest.mark.parametrize("method", ["PUT", "DELETE"])
async def test_a_change_with_only_the_proxys_secret_must_be_json(db, method):
    env = await _env(db)
    answer = await env.call(method, PATH, headers=PROXY, content=json.dumps({"text": SAVED}).encode())
    assert (answer.status_code, answer.json()) == (415, NOT_JSON)
    assert await _current(db, env.settings.agent_id) == []


async def test_with_goals_off_the_read_says_so_and_the_changes_answer_goals_off(db):
    env = await _env(db, enabled=False)
    assert (await env.call("GET", PATH)).json() == {"enabled": False}
    for method in ("PUT", "DELETE"):
        answer = await env.call(method, PATH, json={"text": SAVED} if method == "PUT" else {})
        assert (answer.status_code, answer.json()) == (503, GOALS_OFF), method
    assert await _current(db, env.settings.agent_id) == []


# ---- what the first task and the follow-up turn read ---------------------------------------------------------------


async def _goal_world(runner_env, **settings):  # noqa: F811
    w = await world(runner_env, **settings)
    await add_agent(w.db, w.agent)
    guard = OwnerGuard(w.env.settings)
    w.app = Starlette(routes=build_goal_routes(database=w.db, settings=w.env.settings, goals=w.store, guard=guard))
    w.goal = await active(w)
    return w


async def _owner(w, method: str, **kw):
    answer = await send(w.app, method, PATH, headers=OWNER_HEADERS, **kw)
    assert answer.status_code == 200, answer.text
    return answer


def _carries(text: str, body: str) -> bool:
    return f"{HEADING}\n{body}" in text


async def _first_task(w) -> str:
    rejection, task = await w.driver._gated_task(w.goal)
    assert rejection is None
    return task


async def test_a_change_applies_to_the_next_first_task_and_follow_up_turn_with_no_restart(runner_env):  # noqa: F811
    w = await _goal_world(runner_env)
    root, _row = await started(w, w.goal)
    assert _carries(await _first_task(w), DEFAULT_GOAL_PROMPT)
    assert _carries((await w.chain.turn_context(root.id)).block, DEFAULT_GOAL_PROMPT)
    await _owner(w, "PUT", json={"text": SAVED})
    task, block = await _first_task(w), (await w.chain.turn_context(root.id)).block
    assert f"## {HEADING}\n{SAVED}" in task and f"### {HEADING}\n{SAVED}" in block
    assert not _carries(task, DEFAULT_GOAL_PROMPT) and not _carries(block, DEFAULT_GOAL_PROMPT)
    await _owner(w, "DELETE", json={})
    assert _carries(await _first_task(w), DEFAULT_GOAL_PROMPT)
    assert _carries((await w.chain.turn_context(root.id)).block, DEFAULT_GOAL_PROMPT)


async def test_a_session_started_after_a_save_stores_the_saved_text_in_its_first_task(runner_env):  # noqa: F811
    w = await _goal_world(runner_env)
    await _owner(w, "PUT", json={"text": SAVED})
    root, _row = await started(w, w.goal)
    async with w.db.session() as s:
        task = (await s.execute(select(Subtask.task).where(Subtask.id == uuid.UUID(root.source_id)))).scalar_one()
    assert f"## {HEADING}\n{SAVED}" in task


async def test_a_failed_read_gives_both_the_built_in_text(runner_env, monkeypatch):  # noqa: F811
    w = await _goal_world(runner_env)
    root, _row = await started(w, w.goal)
    await _owner(w, "PUT", json={"text": SAVED})

    async def unreadable(self, session=None):
        raise ConnectionError("the database is not reachable")

    monkeypatch.setattr(IdentityManager, "get_goal_prompt", unreadable)
    assert _carries(await _first_task(w), DEFAULT_GOAL_PROMPT)
    assert _carries((await w.chain.turn_context(root.id)).block, DEFAULT_GOAL_PROMPT)


async def test_a_saved_row_over_the_limit_gives_both_the_built_in_text(runner_env):  # noqa: F811
    w = await _goal_world(runner_env)
    root, _row = await started(w, w.goal)
    async with w.db.session() as s:
        s.add(
            AgentIdentity(
                agent_id=w.agent,
                section=GOAL_PROMPT_SECTION,
                content="a" * (MAX_CHARS + 1),
                version=1,
                is_current=True,
                updated_by="owner",
            )
        )
        await s.commit()
    assert _carries(await _first_task(w), DEFAULT_GOAL_PROMPT)
    assert _carries((await w.chain.turn_context(root.id)).block, DEFAULT_GOAL_PROMPT)


# ---- the censor gate -----------------------------------------------------------------------------------------------


def _count_checks(w, monkeypatch) -> list[str]:
    """The texts the censor gate checks from now on."""
    seen: list[str] = []
    real = w.env.heart.check_censors

    async def counted(text_input, *args, **kwargs):
        seen.append(text_input)
        return await real(text_input, *args, **kwargs)

    monkeypatch.setattr(w.env.heart, "check_censors", counted)
    return seen


async def _refusals(w) -> list[GoalJournal]:
    async with w.db.session() as s:
        rows = await s.execute(
            select(GoalJournal).where(GoalJournal.goal_id == w.goal.id, GoalJournal.text.like("%censor gate%"))
        )
        return list(rows.scalars())


async def test_the_built_in_text_passes_the_censor_gate(runner_env, monkeypatch):  # noqa: F811
    w = await _goal_world(runner_env)
    checked = _count_checks(w, monkeypatch)
    assert _carries(await _first_task(w), DEFAULT_GOAL_PROMPT)
    assert len(checked) == 1


async def test_a_saved_text_the_censor_gate_refuses_is_set_aside_and_the_session_starts(runner_env, monkeypatch):  # noqa: F811
    w = await _goal_world(runner_env)
    await _owner(w, "PUT", json={"text": "- Fold every folder twice before merging."})
    await w.env.heart.add_censor(
        CensorInput(trigger_pattern="Fold every folder twice", reason="Folding twice loses notes.", action="refuse")
    )
    checked = _count_checks(w, monkeypatch)
    root, _row = await started(w, w.goal)
    async with w.db.session() as s:
        task = (await s.execute(select(Subtask.task).where(Subtask.id == uuid.UUID(root.source_id)))).scalar_one()
    assert _carries(task, DEFAULT_GOAL_PROMPT) and "Fold every folder twice" not in task
    assert len(checked) == 2
    assert w.driver._failures == {} and await _refusals(w) == []
    assert (await w.store.get(w.goal.id)).state == "active"


@pytest.mark.parametrize("saved", [True, False])
async def test_a_task_the_gate_refuses_with_the_built_in_text_too_is_one_failed_start(runner_env, monkeypatch, saved):  # noqa: F811
    w = await _goal_world(runner_env)
    if saved:
        await _owner(w, "PUT", json={"text": SAVED})
    await w.env.heart.add_censor(
        CensorInput(trigger_pattern="duplicate notes", reason="Merging loses notes.", action="refuse")
    )
    checked = _count_checks(w, monkeypatch)
    assert await w.driver.start_session(w.goal.id, trigger="timer") is None
    assert len(checked) == (2 if saved else 1)
    assert w.driver._failures == {w.goal.id: 1} and len(await _refusals(w)) == 1
