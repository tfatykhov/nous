"""F115: a limit the owner changes is what the code that uses it resolves on its next use, with no restart. Each test
changes a limit through the store and asserts at the reader: the loop, the subtask turn, the chain's limits, a new
chain's end, a question's window, a new subtask's time limit and the ledger's orphan threshold."""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from f099_support import (
    CONT,
    add_arrival,
    claim,
    commit_ask,
    env_factory,  # noqa: F401
    intention_of,
    make_child,
    make_root,
    record,
    runner_env,  # noqa: F401
    say,
    use,
)
from sqlalchemy import delete, select, update

from nous.autonomy.registry import LIMITS
from nous.autonomy.store import AutonomyStore
from nous.brain import continuation, intentions
from nous.brain.intentions import IntentionSpec
from nous.cognitive.ledger_store import effective_orphan_threshold
from nous.storage.models import AutonomySetting, AutonomySettingLog, ResultInbox

pytestmark = pytest.mark.postgres_only
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
async def store_for(db):
    agents: list[str] = []

    def build(env) -> AutonomyStore:
        agents.append(env.agent)
        return AutonomyStore(env.db, env.settings)

    yield build
    async with db.session() as session:
        for model in (AutonomySetting, AutonomySettingLog):
            await session.execute(delete(model).where(model.agent_id.in_(agents)))
        await session.commit()


async def test_the_tool_loop_stops_at_the_new_number_of_iterations(runner_env, store_for):  # noqa: F811
    # Five tool calls and the closing call: a loop still at the host's 10 would call the model again (an overrun).
    env = await runner_env(*[[use("read_file", path="notes.txt")] for _ in range(5)], [say("Done.")])
    await store_for(env).set_limit("max_turns", 5, "owner")
    await env.runner.run_turn("subtask-loop", "read", is_background=True, is_subtask=True)
    assert len(env.model.calls) == 6
    assert env.model.calls[-1]["tools"] is None  # the closing call after the last iteration


async def test_a_background_turn_is_given_the_new_tool_call_limit(env_factory, store_for):  # noqa: F811
    from nous.handlers.subtask_worker import SubtaskWorkerPool

    env = await env_factory()
    runner = AsyncMock()
    runner.run_turn = AsyncMock(return_value=("done", MagicMock(), {"input_tokens": 1, "output_tokens": 1}))
    heart = MagicMock()
    heart.subtasks.complete = AsyncMock()
    heart.subtasks.fail = AsyncMock()
    heart.check_censors = AsyncMock(return_value=[])
    pool = SubtaskWorkerPool(runner=runner, heart=heart, settings=env.settings)
    await store_for(env).set_limit("subtask_tool_call_limit", 7, "owner")
    subtask = SimpleNamespace(
        id=uuid.uuid4(),
        task="look",
        frame_type=None,
        model=None,
        notify=False,
        output_format=None,
        success_criteria=None,
    )
    await pool._execute_subtask(subtask)
    assert runner.run_turn.await_args.kwargs["max_tool_calls"] == 7


async def test_a_running_chain_meets_the_new_depth_and_spawn_limits(env_factory, store_for):  # noqa: F811
    env = await env_factory(**CONT)
    root = await make_root(env)
    await make_child(env, root)  # depth 1, one spawn
    store = store_for(env)

    async def limits():
        async with env.db.session() as s:
            one = await continuation.root_limits(s, env.agent, root.id, settings=env.settings)
            many = await continuation.root_limits_many(s, env.agent, [root.id], settings=env.settings)
        assert many[root.id] == one
        return one

    assert (await limits()).escalate is None
    await store.set_limit("continuation_max_depth", 1, "owner")
    assert (await limits()).escalate == "limit_depth"
    await store.clear_limit("continuation_max_depth", "owner")
    await store.set_limit("continuation_max_spawns_per_root", 1, "owner")
    got = await limits()
    assert got.escalate == "limit_spawns" and got.spawn_blocked


async def test_a_running_chain_meets_the_new_decision_token_and_stall_limits(env_factory, store_for):  # noqa: F811
    env = await env_factory(**CONT)
    root = await make_root(env)
    await add_arrival(env, root.id, 1, progress=True, tokens=(15_000, 5_000))  # one decision, 20,000 tokens
    store = store_for(env)

    async def escalate():
        async with env.db.session() as s:
            one = await continuation.root_limits(s, env.agent, root.id, settings=env.settings)
            many = await continuation.root_limits_many(s, env.agent, [root.id], settings=env.settings)
        assert many[root.id] == one
        return one.escalate

    assert await escalate() is None
    for key, value, verdict in [
        ("continuation_max_turns_per_root", 1, "budget_turns"),
        ("continuation_max_tokens_per_root", 10_000, "budget_tokens"),
    ]:
        await store.set_limit(key, value, "owner")
        assert await escalate() == verdict, key
        await store.clear_limit(key, "owner")
        assert await escalate() is None, key
    await add_arrival(env, root.id, 2, progress=False)  # one step without progress: below the host's 2
    assert await escalate() is None
    await store.set_limit("continuation_stall_limit", 1, "owner")
    assert await escalate() == "budget_stall"


async def test_a_chain_started_after_the_change_gets_the_new_lifetime(env_factory, store_for):  # noqa: F811
    env = await env_factory(**CONT)
    await store_for(env).set_limit("intention_root_ttl_hours", 5, "owner")
    # As spawn_task stamps a new root: its bounds from the settings at that moment.
    spec = intentions.with_bounds(IntentionSpec(intent="look", origin_kind="interactive"), env.settings)
    subtask = await env.heart.subtasks.create(task="look", intention=spec)
    root = await intention_of(env, "subtask", subtask.id)
    assert abs((root.deadline - root.created_at) - timedelta(hours=5)) < timedelta(minutes=1)


async def test_a_question_is_judged_by_the_new_window(env_factory, store_for):  # noqa: F811
    env = await env_factory(**CONT)
    root = await make_root(env)
    await record(env, root)
    done = await commit_ask(env, await claim(env, root.id), "May I book the Friday slot?")
    async with env.db.session() as s:
        await s.execute(
            update(ResultInbox)
            .where(ResultInbox.arrival_id == done.arrival_id, ResultInbox.msg_type == "QUESTION")
            .values(created_at=datetime.now(UTC) - timedelta(hours=25), push_after=None)
        )
        await s.commit()
        question_id = (
            await s.execute(
                select(ResultInbox.source_id).where(
                    ResultInbox.arrival_id == done.arrival_id, ResultInbox.msg_type == "QUESTION"
                )
            )
        ).scalar_one()

    async def answer():
        async with env.db.session() as s:
            got = await continuation.record_answer(
                s, env.agent, question_id, text="Yes.", actor="telegram:42", settings=env.settings
            )
            await s.commit()
        return got

    with pytest.raises(continuation.AnswerRefused):  # 25 hours old, the host's window is 24
        await answer()
    await store_for(env).set_limit("intention_proposal_ttl_hours", 48, "owner")
    assert (await answer()).question_id == question_id


async def test_a_new_subtask_gets_the_new_time_limits(env_factory, store_for):  # noqa: F811
    from nous.api.tools import create_subtask_tools

    env = await env_factory(subtask_enabled=True)
    heart = MagicMock()
    created: list[dict] = []

    async def create(**kwargs):
        created.append(kwargs)
        return SimpleNamespace(id=uuid.uuid4(), **{k: kwargs.get(k) for k in ("task", "frame_type", "model")})

    heart.subtasks.create = AsyncMock(side_effect=create)
    tools = create_subtask_tools(heart, env.settings)
    store = store_for(env)
    await store.set_limit("subtask_max_timeout", 5000, "owner")
    await store.set_limit("subtask_default_timeout", 4000, "owner")
    await tools["spawn_task"](task="look")
    await tools["spawn_task"](task="look", timeout=9000)
    assert [call["timeout"] for call in created] == [4000, 5000]
    assert effective_orphan_threshold(env.settings) >= 5000 + 600


def test_every_reader_of_a_limit_reads_it_from_settings_when_it_uses_it():
    """No module keeps a limit's value from start: every read is a ``settings`` attribute read inside a function."""
    for limit in LIMITS:
        word = re.compile(rf"\b{limit.key}\b")
        for path in (ROOT / "nous").rglob("*.py"):
            if path.name == "config.py" and path.parent.name == "nous" or path.parent.name == "autonomy":
                continue
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if not word.search(line) or line.lstrip().startswith("#"):
                    continue
                where = f"{path.relative_to(ROOT)}:{number}"
                assert line.startswith(" "), where  # not at module level
                assert not re.match(rf"\s*self\._\w+\s*[:=].*\b{limit.key}\b", line), where  # not kept on self
