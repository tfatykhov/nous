"""F099 Phase 3a (R2): ONE predicate says whether a question card still has something to answer."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from f099_cards_support import CARDS, age_question, answer, ask, question_id_of, wake
from f099_support import claim, commit_ask, env_factory, set_intention  # noqa: F401

from nous.brain import continuation

pytestmark = pytest.mark.postgres_only


async def _state(env, qid, **kw):
    async with env.db.session() as s:
        return await continuation.question_card_state(s, env.agent, qid, settings=env.settings, **kw)


async def _awaited(env, qid, **kw):
    async with env.db.session() as s:
        return await continuation.question_awaited(s, env.agent, qid, settings=env.settings, **kw)


async def test_an_open_question_is_awaited(env_factory):  # noqa: F811
    env = await env_factory(**CARDS)
    _root, qid, _done = await ask(env)
    assert await _state(env, qid) == "awaited" and await _awaited(env, qid) is True


async def test_an_unknown_question_is_closed(env_factory):  # noqa: F811
    env = await env_factory(**CARDS)
    assert await _state(env, uuid.uuid4()) == "closed" and await _awaited(env, uuid.uuid4()) is False


async def test_an_answered_question_is_answered_not_awaited(env_factory):  # noqa: F811
    env = await env_factory(**CARDS)
    _root, qid, _done = await ask(env)
    await answer(env, qid)
    assert await _state(env, qid) == "answered" and await _awaited(env, qid) is False


async def test_a_question_past_its_window_is_closed_and_without_settings_the_window_is_not_tested(env_factory):  # noqa: F811
    env = await env_factory(**CARDS)
    _root, qid, done = await ask(env)
    await age_question(env, done.arrival_id)
    assert await _state(env, qid) == "closed"
    async with env.db.session() as s:
        assert await continuation.question_card_state(s, env.agent, qid) == "awaited"  # no settings: no window


@pytest.mark.parametrize("first", ["answered", "expired"])
async def test_an_old_question_whose_intention_asked_again_is_not_awaited(env_factory, first):  # noqa: F811
    """The #745 scenario: ask, settle the ask, wake, claim, ask again. Only the second question is awaited, even
    when the first one's answer window is open again (the store would hold a late answer, but nothing is waiting)."""
    env = await env_factory(**CARDS)
    root, first_qid, first_done = await ask(env, note="First question?")
    if first == "answered":
        await answer(env, first_qid)
    else:
        await age_question(env, first_done.arrival_id)
    # an answer wakes the arrival at once (record_answer); an expired question is woken by the sweep
    assert await wake(env) == ([] if first == "answered" else [root.id])
    second_done = await commit_ask(env, await claim(env, root.id), "Second question?")
    second_qid = await question_id_of(env, second_done.arrival_id)
    env.settings.intention_proposal_ttl_hours = 48.0  # the first question's window is open again
    assert await _awaited(env, first_qid) is False
    assert await _awaited(env, second_qid) is True


async def test_a_cancelled_root_closes_its_questions(env_factory):  # noqa: F811
    env = await env_factory(**CARDS)
    root, qid, _done = await ask(env)
    async with env.db.session() as s:
        await continuation.cancel_root(s, env.agent, root.id, reason="test", actor="t")
        await s.commit()
    assert await _state(env, qid) == "closed"


async def test_an_expired_root_closes_its_questions(env_factory):  # noqa: F811
    env = await env_factory(**CARDS)
    root, qid, _done = await ask(env)
    await set_intention(env, root.id, root_expired_at=datetime.now(UTC))
    assert await _state(env, qid) == "closed"


async def test_an_intention_that_is_no_longer_awaiting_the_owner_closes_the_question(env_factory):  # noqa: F811
    env = await env_factory(**CARDS)
    root, qid, _done = await ask(env)
    await set_intention(env, root.id, state="closed")
    assert await _state(env, qid) == "closed"
