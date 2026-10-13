"""F099 Phase 3d: the arrival exclusion on the Brain's three readers (contract 4.10.1, R6)."""

from __future__ import annotations

import uuid

import pytest
from f099_calibration_support import arrival, brain_of, make_decision
from f099_support import CONT, env_factory, make_root  # noqa: F401

from nous.storage.models import IntentionArrival

pytestmark = pytest.mark.postgres_only


async def _world(env):
    """One arrival-linked decision (reviewed none), one ordinary decision, and a record_decision row made
    INSIDE the intent- session that is not an arrival's."""
    root = await make_root(env)
    linked = await arrival(env, root, 1)
    ordinary = await make_decision(env, session_id="chat-1")
    in_session = await make_decision(env, session_id=f"intent-{root.id}", description="Chose the Friday slot")
    return root, linked.decision_id, ordinary, in_session


async def test_get_unreviewed_omits_exactly_the_linked_decision(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    _root, linked, ordinary, in_session = await _world(env)
    brain = brain_of(env)
    plain = {d.id for d in await brain.get_unreviewed()}
    excluded = {d.id for d in await brain.get_unreviewed(exclude_intention_arrivals=True)}
    assert plain == {linked, ordinary, in_session}  # PIN: the default is today's
    assert excluded == {ordinary, in_session}


async def test_get_unreviewed_limit_is_applied_after_the_exclusion(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    _root, _linked, ordinary, in_session = await _world(env)
    rows = await brain_of(env).get_unreviewed(exclude_intention_arrivals=True, limit=2)
    assert {d.id for d in rows} == {ordinary, in_session}  # a linked row never takes a slot of the limit


async def test_get_session_decisions_filters_the_session_the_same_way(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    root, linked, _ordinary, in_session = await _world(env)
    brain = brain_of(env)
    sid = f"intent-{root.id}"
    assert {d.id for d in await brain.get_session_decisions(sid)} == {linked, in_session}  # PIN
    assert {d.id for d in await brain.get_session_decisions(sid, exclude_intention_arrivals=True)} == {in_session}


async def test_list_decisions_filters_rows_and_total(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    _root, linked, ordinary, in_session = await _world(env)
    brain = brain_of(env)
    rows, total = await brain.list_decisions(agent_id=env.agent, outcome="pending")
    assert (total, {d.id for d in rows}) == (3, {linked, ordinary, in_session})  # PIN
    rows, total = await brain.list_decisions(agent_id=env.agent, outcome="pending", exclude_intention_arrivals=True)
    assert (total, {d.id for d in rows}) == (2, {ordinary, in_session})


async def test_a_reviewed_linked_decision_is_excluded_from_the_graded_window_too(env_factory):  # noqa: F811
    """The procedure learner reads success rows: with the keyword, an arrival-linked `success` is not one."""
    env = await env_factory(**CONT)
    root = await make_root(env)
    graded = await arrival(env, root, 1, decision_kw={"outcome": "success", "reviewer": "auto"})
    ordinary = await make_decision(env, session_id="chat-2", outcome="success", reviewer="agent")
    brain = brain_of(env)
    rows, _ = await brain.list_decisions(agent_id=env.agent, outcome="success", reviewed=True)
    assert {d.id for d in rows} == {graded.decision_id, ordinary}  # PIN
    rows, _ = await brain.list_decisions(
        agent_id=env.agent, outcome="success", reviewed=True, exclude_intention_arrivals=True
    )
    assert {d.id for d in rows} == {ordinary}


async def test_another_agents_arrival_does_not_hide_this_agents_decision(env_factory):  # noqa: F811
    """The NOT EXISTS joins on agent_id as well as on the decision id."""
    env = await env_factory(**CONT)
    other = await env_factory(**CONT)
    other_root = await make_root(other)
    mine = await make_decision(env, session_id="chat-3")
    # The OTHER agent's arrival points at MY decision id: only the agent_id term keeps it from hiding mine.
    # Mutation: drop `IntentionArrival.agent_id == Decision.agent_id` from `_arrival_linked` and this fails.
    async with other.db.session() as s:
        s.add(
            IntentionArrival(
                agent_id=other.agent,
                root_id=other_root.id,
                n=1,
                intention_ids=[other_root.id],
                claim_token=uuid.uuid4(),
                decision="continue",
                outcome="resolved",
                decision_record_id=mine,
            )
        )
        await s.commit()
    rows = await brain_of(env).get_unreviewed(exclude_intention_arrivals=True)
    assert [d.id for d in rows] == [mine]
