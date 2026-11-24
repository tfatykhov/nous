"""F114 goals: what a goal's sessions wait on the owner for, in the Goals screen's reads: ``awaiting_count`` in each
row of ``GET /goals``, ``awaiting`` in ``GET /goals/{ref}``, and the total of the dashboard's Goals badge
(``nous.goals.awaiting``). Real Postgres; the approval requests and the questions are written by the continuation's
own functions (tests/f099_support.py) and tied to a goal's session by its ``root_id``, as the goal driver ties them."""

from __future__ import annotations

import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from f099_support import (
    CONT,
    SEND_EMAIL_ARGS,
    ask_with_proposals,
    claimed,
    commit_ask,
    env_factory,  # noqa: F401
    inbox_rows,
    make_root,
    proposal_row,
    stage,
)
from goal_routes_support import active, draft, make_env
from sqlalchemy import func, update

from nous.brain import continuation
from nous.goals import awaiting
from nous.storage.models import GoalSession, IntentionProposal, ResultInbox
from nous.telegram_bot import describe_answer, describe_decision

pytestmark = pytest.mark.postgres_only

# The Goals screen's words for these answers (it keeps its own copy of the bot's sentences).
GOALS_TS = Path(__file__).resolve().parents[1] / "dashboard-app" / "src" / "lib" / "goals.ts"

PROPOSAL_KEYS = {"id", "short_id", "tool", "arguments", "why", "expires_at"}
QUESTION_KEYS = {"id", "short_id", "text", "expires_at"}
QUESTION = "Which folder holds the newest notes?"


async def world(env_factory, **over):  # noqa: F811
    """A continuation environment and the goal routes of the same agent, with one active goal: ``(env, routes,
    goal)``."""
    env = await env_factory(**CONT, **over)
    routes = make_env(env.db, agent=env.agent, **CONT, **over)
    return env, routes, await active(routes.store)


async def attach(env, goal, root_id, *, n: int, open_: bool = False, started_ago: timedelta | None = None) -> None:
    """A session of ``goal`` whose chain is ``root_id``, as the goal driver writes it: closed unless ``open_`` (a goal
    has at most one open session)."""
    async with env.db.session() as s:
        s.add(
            GoalSession(
                agent_id=env.agent,
                goal_id=goal.id,
                n=n,
                trigger="timer",
                first_subtask_id=uuid.uuid4(),
                root_id=root_id,
                **({"started_at": func.clock_timestamp() - started_ago} if started_ago is not None else {}),
                **({} if open_ else {"ended_at": func.clock_timestamp(), "outcome": "no_step"}),
            )
        )
        await s.commit()


async def ask_question(env, got) -> ResultInbox:
    """The chain of ``got`` asks the owner ``QUESTION`` (an ask with no approval request): its QUESTION row."""
    await commit_ask(env, got, QUESTION)
    (row,) = [
        r for r in await inbox_rows(env) if r.msg_type == continuation.MSG_QUESTION and r.intention_id == got.deepest.id
    ]
    return row


async def read(routes, goal) -> dict:
    response = await routes.call("GET", f"/goals/{goal.id}")
    assert response.status_code == 200
    return response.json()


async def counts(routes, query: str = "") -> dict[str, int | None]:
    response = await routes.call("GET", f"/goals{query}")
    assert response.status_code == 200
    return {row["id"]: row["awaiting_count"] for row in response.json()["goals"]}


async def test_a_goal_lists_its_approval_request_and_its_question_and_the_list_counts_them(env_factory):  # noqa: F811
    env, routes, goal = await world(env_factory)
    other = await active(routes.store, title="Sort the photos")
    asked = await ask_with_proposals(env)
    _root, got = await claimed(env)
    question = await ask_question(env, got)
    await attach(env, goal, asked.root.id, n=1)
    await attach(env, goal, got.root_id, n=2, open_=True)
    body = await read(routes, goal)
    stored = await proposal_row(env, asked.ids[0])
    ttl = timedelta(hours=routes.settings.intention_proposal_ttl_hours)
    assert body["awaiting"] == {
        "proposals": [
            {
                "id": str(stored.id),
                "short_id": stored.id.hex[:8],
                "tool": "send_email",
                "arguments": continuation.render_arguments(stored.arguments),
                "why": continuation.render_text(stored.rationale),
                "expires_at": stored.deadline.isoformat(),
            }
        ],
        "questions": [
            {
                "id": str(question.source_id),
                "short_id": question.source_id.hex[:8],
                "text": QUESTION,
                "expires_at": (continuation.question_window_start(question) + ttl).isoformat(),
            }
        ],
    }
    assert body["awaiting_count"] == 2
    assert await counts(routes) == {str(goal.id): 2, str(other.id): 0}
    assert (await read(routes, other))["awaiting"] == {"proposals": [], "questions": []}


async def test_the_call_and_the_reason_are_the_text_the_approval_message_shows(env_factory):  # noqa: F811
    env, routes, goal = await world(env_factory)
    root, got = await claimed(env)
    arguments = {
        **SEND_EMAIL_ARGS,
        "subject": 'Notes <b>bold</b> & "quoted"',
        "body": "line one\nline two ‮owt",
    }
    proposal_id = await stage(env, got, arguments=arguments, rationale="The owner asked for <i>this</i> list.")
    await commit_ask(env, got)
    await attach(env, goal, root.id, n=1)
    (item,) = (await read(routes, goal))["awaiting"]["proposals"]
    assert set(item) == PROPOSAL_KEYS
    assert item["arguments"] == continuation.render_arguments((await proposal_row(env, proposal_id)).arguments)
    assert '"subject": "Notes <b>bold</b> & \\"quoted\\""' in item["arguments"]
    assert '"body": "line one\\nline two \\u202eowt"' in item["arguments"]
    assert "‮" not in item["arguments"]  # the direction mark is shown as its escape, as in the approval message
    assert item["why"] == "The owner asked for <i>this</i> list."


async def test_a_decided_or_late_request_and_a_chain_that_asks_nothing_are_not_listed(env_factory):  # noqa: F811
    env, routes, goal = await world(env_factory)
    rejected, approved, late = [await ask_with_proposals(env) for _ in range(3)]
    working = await make_root(env)  # a chain that waits on nothing from the owner
    for n, root_id in enumerate([rejected.root.id, approved.root.id, late.root.id, working.id], start=1):
        await attach(env, goal, root_id, n=n)
    assert (await read(routes, goal))["awaiting_count"] == 3
    async with env.db.session() as s:
        for proposal_id, approve in ((rejected.ids[0], False), (approved.ids[0], True)):
            await continuation.decide_proposal(
                s, env.agent, proposal_id, approve=approve, actor="test", settings=env.settings
            )
        await s.execute(
            update(IntentionProposal)
            .where(IntentionProposal.id == late.ids[0])
            .values(deadline=func.clock_timestamp() - timedelta(minutes=1))
        )
        await s.commit()
    body = await read(routes, goal)
    assert (body["awaiting"], body["awaiting_count"]) == ({"proposals": [], "questions": []}, 0)
    assert await counts(routes) == {str(goal.id): 0}


async def test_an_answered_or_late_question_is_not_listed(env_factory):  # noqa: F811
    env, routes, goal = await world(env_factory)
    _root, answered_got = await claimed(env)
    answered = await ask_question(env, answered_got)
    _root, old_got = await claimed(env)
    old = await ask_question(env, old_got)
    await attach(env, goal, answered_got.root_id, n=1)
    await attach(env, goal, old_got.root_id, n=2)
    assert (await read(routes, goal))["awaiting_count"] == 2
    async with env.db.session() as s:
        await continuation.record_answer(
            s, env.agent, answered.source_id, text="The 2024 folder.", actor="test", settings=env.settings
        )
        await s.execute(  # its window (24 hours from when it was written) ended an hour ago
            update(ResultInbox)
            .where(ResultInbox.id == old.id)
            .values(created_at=func.clock_timestamp() - timedelta(hours=25), push_after=None)
        )
        await s.commit()
    body = await read(routes, goal)
    assert (body["awaiting"], body["awaiting_count"]) == ({"proposals": [], "questions": []}, 0)
    assert await counts(routes) == {str(goal.id): 0}


async def test_a_closed_or_old_session_whose_chain_still_waits_is_listed(env_factory):  # noqa: F811
    env, routes, goal = await world(env_factory)
    asked = await ask_with_proposals(env)
    _root, got = await claimed(env)
    question = await ask_question(env, got)
    await attach(env, goal, asked.root.id, n=1, started_ago=timedelta(days=10))
    await attach(env, goal, got.root_id, n=2)
    awaiting_now = (await read(routes, goal))["awaiting"]
    assert [p["id"] for p in awaiting_now["proposals"]] == [str(asked.ids[0])]
    assert [q["id"] for q in awaiting_now["questions"]] == [str(question.source_id)]


async def test_the_items_of_several_sessions_are_listed_oldest_first(env_factory):  # noqa: F811
    env, routes, goal = await world(env_factory)
    first = await ask_with_proposals(env, count=2)
    second = await ask_with_proposals(env)
    await attach(env, goal, second.root.id, n=1)
    await attach(env, goal, first.root.id, n=2)
    proposals = (await read(routes, goal))["awaiting"]["proposals"]
    assert [p["id"] for p in proposals] == [str(i) for i in [*first.ids, *second.ids]]


async def test_each_goal_lists_only_its_own_sessions_and_a_finished_goal_none(env_factory):  # noqa: F811
    env, routes, goal = await world(env_factory)
    other = await active(routes.store, title="Sort the photos")
    finished = await active(routes.store, title="Old report")
    mine, theirs, ended = [await ask_with_proposals(env) for _ in range(3)]
    await attach(env, goal, mine.root.id, n=1)
    await attach(env, other, theirs.root.id, n=1)
    await attach(env, finished, ended.root.id, n=1)
    await routes.store.cancel(finished.id, actor="owner")
    assert [p["id"] for p in (await read(routes, goal))["awaiting"]["proposals"]] == [str(mine.ids[0])]
    assert [p["id"] for p in (await read(routes, other))["awaiting"]["proposals"]] == [str(theirs.ids[0])]
    done = await read(routes, finished)
    assert (done["awaiting"], done["awaiting_count"]) == ({"proposals": [], "questions": []}, 0)
    assert await counts(routes, "?state=all") == {str(goal.id): 1, str(other.id): 1, str(finished.id): 0}
    async with env.db.session() as s:
        assert await awaiting.waiting_total(s, env.agent, settings=env.settings) == 2


async def test_an_item_whose_message_is_not_sent_yet_is_listed(env_factory):  # noqa: F811
    env, routes, goal = await world(env_factory)
    asked = await ask_with_proposals(env)
    _root, got = await claimed(env)
    await ask_question(env, got)
    await attach(env, goal, asked.root.id, n=1)
    await attach(env, goal, got.root_id, n=2)
    async with env.db.session() as s:  # held, as in quiet hours: due in three hours, not sent
        await s.execute(
            update(ResultInbox)
            .where(ResultInbox.agent_id == env.agent)
            .values(push_after=func.clock_timestamp() + timedelta(hours=3))
        )
        await s.commit()
    assert all(row.pushed_at is None for row in await inbox_rows(env))
    body = await read(routes, goal)
    assert (len(body["awaiting"]["proposals"]), len(body["awaiting"]["questions"])) == (1, 1)


async def test_when_the_waiting_read_fails_the_goal_is_sent_with_null_in_its_place(db, monkeypatch):
    routes = make_env(db, **CONT)
    goal = await active(routes.store)
    finished = await draft(routes.store)
    await routes.store.cancel(finished.id, actor="owner")

    async def broken(*_args, **_kwargs):
        raise RuntimeError("the database said something private")

    monkeypatch.setattr(awaiting, "waiting_ids", broken)
    response = await routes.call("GET", f"/goals/{goal.id}")
    assert response.status_code == 200
    body = response.json()
    assert (body["awaiting"], body["awaiting_count"], body["title"]) == (None, None, goal.title)
    assert "private" not in response.text
    assert await counts(routes, "?state=all") == {str(goal.id): None, str(finished.id): 0}


def test_the_goals_screen_says_what_the_telegram_bot_says_for_the_same_answer():
    source = GOALS_TS.read_text(encoding="utf-8")
    for state in ("executed", "failed", "executing", "approved", "rejected", "something new"):
        text, _final = describe_decision(200, {"state": state}, True, "${shortId}")
        assert f"`{text}`" in source, state
    assert f"'{describe_decision(409, {}, True, 'cfd2afea')[0]}'" in source
    assert f"'{describe_answer(200, {})}'" in source
    assert f"'{describe_answer(409, {})}'" in source
    assert f"'{describe_answer(503, {})}'" in source
    assert f"export const ANSWER_MAX = {continuation.ANSWER_MAX_CHARS};" in source
