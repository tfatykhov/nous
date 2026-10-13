"""F099 Phase 3c-1: root_limits_many equals root_limits for every root (contract 5.11, ruling R4)."""

from __future__ import annotations

import uuid
from dataclasses import asdict

import pytest
from f099_dashboard_support import put_arrivals, put_intention
from f099_support import (
    CONT,
    env_factory,  # noqa: F401
    make_child,
    make_dag,
    make_root,
    set_intention,
)
from sqlalchemy import update

from nous.brain import continuation
from nous.storage.models import ExecutionDAG, Subtask

pytestmark = pytest.mark.postgres_only

SETTINGS = dict(
    continuation_max_turns_per_root=4,
    continuation_max_tokens_per_root=1000,
    continuation_stall_limit=2,
    continuation_max_depth=2,
    continuation_max_spawns_per_root=2,
)


async def _world(env) -> dict[str, uuid.UUID]:
    """One root per branch of the ladder, plus the shapes that tripped the single-root version before. Rows are
    written directly (put_intention, put_arrivals): the budgets read only Intention and IntentionArrival rows here,
    and make_root would hit the 5-pending-subtasks cap (SubtaskQueueFull, subtasks.py _MAX_PENDING)."""
    ids: dict[str, uuid.UUID] = {}
    ids["quiet"] = (await put_intention(env)).id
    turns = await put_intention(env)
    await put_arrivals(env, turns, [{"progress": True}] * 4)
    ids["turns"] = turns.id
    tokens = await put_intention(env)
    await put_arrivals(env, tokens, [{"progress": True, "tokens": (600, 500)}])
    ids["tokens"] = tokens.id
    stall = await put_intention(env)
    await put_arrivals(env, stall, [{"progress": True}, {"progress": False}, {"progress": False}])
    ids["stall"] = stall.id
    broken = await put_intention(env)  # False, True, False: the trailing run is 1, not 2
    await put_arrivals(env, broken, [{"progress": False}, {"progress": True}, {"progress": False}])
    ids["stall_broken"] = broken.id
    deep = await put_intention(env)
    await put_intention(env, parent=await put_intention(env, parent=deep))  # depth 2 = continuation_max_depth
    ids["depth"] = deep.id
    wide = await put_intention(env)
    await put_intention(env, parent=wide)
    await put_intention(env, parent=wide)  # 2 spawns = continuation_max_spawns_per_root, depth 1 < 2
    ids["spawns"] = wide.id
    one = await put_intention(env)
    await put_intention(env, parent=one)
    ids["one_child"] = one.id
    container = await put_intention(env, wake_policy="container")
    ids["container"] = container.id
    failed = await put_intention(env, failed_tokens=999)  # 999 < 1000: still under the token budget
    ids["failed_tokens"] = failed.id
    return ids


async def _compare(env, ids: list[uuid.UUID]):
    async with env.db.session() as s:
        many = await continuation.root_limits_many(s, env.agent, ids, settings=env.settings)
        single = {rid: await continuation.root_limits(s, env.agent, rid, settings=env.settings) for rid in ids}
    return many, single


async def test_root_limits_many_equals_root_limits_for_every_root(env_factory):  # noqa: F811
    env = await env_factory(**CONT, **SETTINGS)
    ids = await _world(env)
    many, single = await _compare(env, list(ids.values()))
    assert many == single
    # every ladder branch is exercised, so equality is not vacuous
    assert {many[i].escalate for i in ids.values()} >= {
        None,
        "budget_turns",
        "budget_tokens",
        "budget_stall",
        "limit_depth",
        "limit_spawns",
    }
    assert many[ids["stall_broken"]].stalls == 1 and many[ids["stall"]].stalls == 2


async def test_root_limits_many_counts_failed_tokens_subtasks_and_dags_like_root_limits(env_factory):  # noqa: F811
    env = await env_factory(**CONT, **SETTINGS)
    root = await make_root(env)
    child = await make_child(env, root)
    await set_intention(env, root.id, failed_tokens=300)
    async with env.db.session() as s:
        await s.execute(
            update(Subtask).where(Subtask.id == uuid.UUID(child.source_id)).values(tokens_in=100, tokens_out=50)
        )
        await s.commit()
    dag, _store = await make_dag(env, parent=root)
    async with env.db.session() as s:
        await s.execute(update(ExecutionDAG).where(ExecutionDAG.id == dag.id).values(tokens_consumed=70))
        await s.commit()
    await put_arrivals(env, root, [{"tokens": (20, 10)}])
    many, single = await _compare(env, [root.id])
    assert many == single
    assert many[root.id].tokens == 300 + 150 + 70 + 30
    async with env.db.session() as s:
        parts = (await continuation.root_token_parts(s, env.agent, [root.id]))[root.id]
    assert (parts.arrivals_in, parts.arrivals_out, parts.failed, parts.subtasks, parts.dags) == (20, 10, 300, 150, 70)
    assert parts.total == many[root.id].tokens


async def test_root_limits_many_answers_an_unknown_root_like_root_limits_and_an_empty_list_with_nothing(
    env_factory,  # noqa: F811
):
    env = await env_factory(**CONT)
    ghost = uuid.uuid4()
    many, single = await _compare(env, [ghost])
    assert many == single and asdict(many[ghost])["tokens"] == 0
    async with env.db.session() as s:
        assert await continuation.root_limits_many(s, env.agent, [], settings=env.settings) == {}
