"""F099 Phase 3b: the owner's view of the roots gains `awaiting` (and the newest report of each root)."""

from __future__ import annotations

import pytest
from f099_cards_support import (
    CARDS,
    age_question,
    answer,
    ask,
    question_id_of,
    report,
)
from f099_support import (
    CONT,
    ask_with_proposals,
    claim,
    commit_ask,
    env_factory,  # noqa: F401
    make_child,
    make_root,
    runner_env,  # noqa: F401
)
from sqlalchemy import update

from nous.brain import continuation
from nous.storage.models import IntentionProposal

pytestmark = pytest.mark.postgres_only

TODAYS_KEYS = {
    "id", "short_id", "intent", "state", "wake_policy", "authority", "origin_kind", "origin_channel", "created_at",
    "deadline", "root_cancelled_at", "root_expired_at", "open_rows", "limits", "lineage", "lineage_truncated",
    "arrivals", "open_proposals",
}  # fmt: skip


async def _views(env, *, state="open", limit=10):
    async with env.db.session() as s:
        return await continuation.list_roots(s, env.agent, state=state, limit=limit, settings=env.settings)


async def test_awaiting_names_the_arrival_and_its_pending_proposals(runner_env):  # noqa: F811
    env = await runner_env(**CARDS)
    asked = await ask_with_proposals(env, count=2)
    (view,) = await _views(env)
    (entry,) = view["awaiting"]
    assert entry["arrival_id"] == str(asked.done.arrival_id) and entry["n"] == 1
    assert entry["proposal_ids"] == [str(i) for i in asked.ids]
    assert entry["question_ids"] == []  # proposals are the ask: no QUESTION row


async def test_awaiting_names_the_unanswered_question_of_a_plain_ask(runner_env):  # noqa: F811
    env = await runner_env(**CARDS)
    _root, question_id, done = await ask(env)
    (view,) = await _views(env)
    (entry,) = view["awaiting"]
    assert entry["arrival_id"] == str(done.arrival_id) and entry["proposal_ids"] == []
    assert entry["question_ids"] == [str(question_id)] == [str(await question_id_of(env, done.arrival_id))]


async def test_a_root_that_waits_on_nobody_has_an_empty_awaiting_list(runner_env):  # noqa: F811
    env = await runner_env(**CARDS)
    await make_root(env)
    (view,) = await _views(env)
    assert view["awaiting"] == []


async def test_an_answered_question_leaves_nothing_awaited(runner_env):  # noqa: F811
    """The answer is terminal (no proposals), so it wakes the intention in the same transaction: no longer
    `awaiting_owner`, so no entry."""
    env = await runner_env(**CARDS)
    _root, question_id, _done = await ask(env)
    assert [e["question_ids"] for e in (await _views(env))[0]["awaiting"]] == [[str(question_id)]]
    await answer(env, question_id)
    assert (await _views(env))[0]["awaiting"] == []


async def test_awaiting_lists_only_the_latest_arrival_of_an_intention_that_asked_again(runner_env):  # noqa: F811
    """The 0511c252 scenario: ask, answer, wake, claim, ask again. The first question must not be shown as waiting."""
    env = await runner_env(**CARDS)
    root, first_q, _first = await ask(env)
    await answer(env, first_q)
    second = await commit_ask(env, await claim(env, root.id), "Second question?")
    second_q = str(await question_id_of(env, second.arrival_id))
    (view,) = await _views(env)
    assert [(e["n"], e["question_ids"]) for e in view["awaiting"]] == [(2, [second_q])]
    assert str(first_q) not in {q for e in view["awaiting"] for q in e["question_ids"]}


async def test_an_expired_window_and_a_past_deadline_are_not_awaited(runner_env):  # noqa: F811
    from datetime import UTC, datetime, timedelta

    env = await runner_env(**CARDS)
    asked = await ask_with_proposals(env, count=1)
    async with env.db.session() as s:
        await s.execute(
            update(IntentionProposal)
            .where(IntentionProposal.id == asked.ids[0])
            .values(deadline=datetime.now(UTC) - timedelta(minutes=1))
        )
        await s.commit()
    await age_question(env, asked.done.arrival_id, hours=100)
    (view,) = await _views(env)
    (entry,) = view["awaiting"]  # the intention still waits on this arrival until the sweep wakes it
    assert entry["proposal_ids"] == [] and entry["question_ids"] == []


async def test_with_cards_off_a_root_view_has_exactly_todays_keys(runner_env):  # noqa: F811  # PIN
    """Flag off: GET /intentions is byte-identical. Mutation: compute `awaiting` without the `cards_enabled` gate and
    this fails on the extra key."""
    env = await runner_env(**CONT)
    await ask_with_proposals(env, count=1)
    (view,) = await _views(env)
    assert set(view) == TODAYS_KEYS


def test_the_bot_reads_a_root_the_same_with_or_without_awaiting():  # PIN
    """The bot reads named keys only. Mutation: make `describe_intentions` read `awaiting` and this fails."""
    from nous.telegram_bot import describe_intentions

    root = {
        "id": "ab12cd34" + "0" * 24, "short_id": "ab12cd34", "intent": "Tell the user about the snow",
        "state": "awaiting_owner", "wake_policy": "continue", "open_rows": 2,
    }  # fmt: skip
    extra = [{"arrival_id": "a", "n": 1, "proposal_ids": ["p"], "question_ids": ["q"]}]
    assert describe_intentions({"roots": [{**root, "awaiting": extra}]}) == describe_intentions({"roots": [root]})


async def _newest(env, root_ids):
    async with env.db.session() as s:
        return await continuation.newest_reports(s, env.agent, root_ids)


async def test_the_newest_report_of_a_root_comes_from_anywhere_in_its_lineage(runner_env):  # noqa: F811
    env = await runner_env(**CARDS)
    a = await make_root(env)
    child = await make_child(env, a)
    b = await make_root(env)
    await report(env, a, a, "old report", hours_ago=3)
    newest = await report(env, a, child, "new report", hours_ago=1)
    other = await report(env, b, b, "the other root", hours_ago=2)
    got = await _newest(env, [a.id, b.id])
    assert got[str(a.id)]["id"] == str(newest) and got[str(a.id)]["body"] == "new report"
    assert got[str(b.id)]["id"] == str(other)


async def test_only_reports_count_and_a_root_without_one_is_absent(runner_env):  # noqa: F811
    env = await runner_env(**CARDS)
    asked = await ask_with_proposals(env, count=1)  # a PROPOSAL row, no REPORT
    bare = await make_root(env)
    assert await _newest(env, [asked.root.id, bare.id]) == {}
    assert await _newest(env, []) == {}
