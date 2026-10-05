"""F099 I2: the four spawn tools record the intention behind each spawn.

With NOUS_INTENTIONS_ENABLED on, each tool takes a one-line intent (refused
when missing in a foreground turn, generated in a background one) and writes
one brain.intentions row in its store's transaction. With it off, the
schemas are byte-identical (tests/test_f099_tool_schemas.py) and nothing is
written.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from nous.api.execution_context import ExecutionContext
from nous.api.tools import ToolDispatcher, register_dag_tools, register_subtask_tools
from nous.brain import intentions
from nous.config import Settings
from nous.dag.store import DAGStore
from nous.storage.models import ExecutionDAG, Intention, Schedule, Subtask

CHAN = "telegram:5150"
SESSION = "S-cap"
PLAN = str(uuid.uuid4())
ON = {"result_inbox_enabled": True, "intentions_enabled": True}
NODES = [{"name": "n", "type": "subtask", "instructions": "x"}]
INTERACTIVE = ExecutionContext(kind="interactive", session_id=SESSION, channel=CHAN, decision_id=PLAN)


class _Turn:
    async def run_turn(self, **_):
        return "done", None, {"input_tokens": 1, "output_tokens": 1}

    async def end_conversation(self, *a, **k):
        return True


class _Orchestrator:
    clock_wired = True
    approvals_wired = False

    def __init__(self, settings):
        self._settings = settings
        self.start_dag = AsyncMock()


@pytest.fixture
async def capture_env(db, mock_embeddings):
    from nous.heart import Heart

    hearts = []

    async def build(**over):
        agent = f"f099-cap-{uuid.uuid4().hex[:8]}"
        settings = Settings(
            _env_file=None,
            agent_id=agent,
            subtask_payload_schema_enabled=True,
            subtask_max_attempts=1,
            telegram_bot_token="",
            telegram_chat_id="",
            **over,
        )
        heart = Heart(db, settings, embedding_provider=mock_embeddings)
        hearts.append(heart)
        d = ToolDispatcher()
        register_subtask_tools(d, heart, settings, runner=_Turn())
        register_dag_tools(d, DAGStore(db, agent, settings), _Orchestrator(settings), settings=settings)
        return SimpleNamespace(agent=agent, settings=settings, heart=heart, d=d, db=db)

    yield build
    for heart in hearts:
        await heart.close()


async def _dispatch(env, name, args, ctx=INTERACTIVE):
    return await env.d.dispatch(name, args, session_id=ctx.session_id, context=ctx)


async def _all(env, model):
    async with env.db.session() as s:
        return list((await s.execute(select(model).where(model.agent_id == env.agent))).scalars().all())


async def _root(env, *, wake_policy="remember", cancelled=False, authority="owner") -> Intention:
    rid = uuid.uuid4()
    row = Intention(
        id=rid,
        agent_id=env.agent,
        root_id=rid,
        depth=0,
        source_kind="subtask",
        source_id=str(uuid.uuid4()),
        intent="the turn's own intention",
        origin_kind="scheduler",
        wake_policy=wake_policy,
        authority=authority,
        state="pending",
        root_cancelled_at=datetime.now(UTC) if cancelled else None,
    )
    async with env.db.session() as s:
        s.add(row)
        await s.commit()
    return row


SPAWNS = [
    ("spawn_task", {"task": "Check the snow report"}),
    ("schedule_task", {"task": "Check the snow report", "every": "30 minutes"}),
    ("spawn_sync", {"task": "Check the snow report"}),
    ("dag_create", {"name": "snow", "nodes": NODES}),
]


async def test_with_the_flag_on_every_spawn_schema_requires_an_intent(capture_env):
    env = await capture_env(**ON, subtask_hardening_enabled=True)
    defs = {d["name"]: d["input_schema"] for d in env.d.tool_definitions()}
    for name, _ in SPAWNS:
        schema = defs[name]
        assert "intent" in schema["required"], name
        assert schema["properties"]["intent"]["description"] == intentions.INTENT_HELP
        assert schema["properties"]["wake_policy"]["enum"] == list(intentions.MODEL_WAKE_POLICIES)
        assert name in env.d._origin_aware


@pytest.mark.parametrize(("tool", "args"), SPAWNS, ids=[s[0] for s in SPAWNS])
@pytest.mark.parametrize("intent", [None, "  \n "], ids=["missing", "blank"])
@pytest.mark.parametrize("kind", ["interactive", "mcp"])
async def test_a_foreground_spawn_without_an_intent_is_refused_and_says_what_to_write(
    capture_env, tool, args, intent, kind
):
    env = await capture_env(**ON, subtask_hardening_enabled=(tool == "spawn_sync"))
    call = dict(args) if intent is None else {**args, "intent": intent}
    text, is_error = await _dispatch(env, tool, call, ExecutionContext(kind=kind, session_id=SESSION))
    assert is_error, text
    assert intentions.INTENT_HELP in text
    if tool == "spawn_sync":
        # Review S4: spawn_sync keeps its SubtaskResult JSON contract.
        assert intentions.INTENT_HELP in json.loads(text)["validator_reason"]
    for model in (Subtask, Schedule, ExecutionDAG, Intention):
        assert await _all(env, model) == [], model


DESC = "Find the leak\nin stages"


@pytest.mark.parametrize(
    ("tool", "args", "generated"),
    [
        (
            "spawn_task",
            {"task": "Email the user the DAG result\nwith the attachment"},
            "dag_summary: Email the user the DAG result",
        ),
        (
            "schedule_task",
            {"task": "Check the snow report", "every": "30 minutes"},
            "dag_summary: Check the snow report",
        ),
        ("spawn_sync", {"task": "Check the snow report"}, "dag_summary: Check the snow report"),
        ("dag_create", {"name": "snow", "description": DESC, "nodes": NODES}, "dag_summary: Find the leak"),
        ("dag_create", {"name": "snow", "nodes": NODES}, "dag_summary: snow"),
        ("dag_create", {"name": "snow", "description": "   ", "nodes": NODES}, "dag_summary: snow"),
    ],
    ids=[
        "spawn_task",
        "schedule_task",
        "spawn_sync",
        "dag_create",
        "dag_create-by-name",
        "dag_create-blank-description",
    ],
)
async def test_a_background_spawn_without_an_intent_goes_ahead_with_a_generated_one(capture_env, tool, args, generated):
    """I2: background prompts predate intent. The F087 summary turn's email
    subtask must still be spawned, not refused."""
    env = await capture_env(**ON, subtask_hardening_enabled=(tool == "spawn_sync"))
    ctx = ExecutionContext(kind="dag_summary", session_id="dag-summary-1")
    await _dispatch(env, tool, dict(args), ctx)
    (it,) = await _all(env, Intention)
    assert (it.intent, it.origin_kind) == (generated, "dag_summary")
    if tool == "dag_create":
        # A generated intent is not the model's reason: original_request keeps
        # the Phase 0a value, the stripped description (D1).
        (dag,) = await _all(env, ExecutionDAG)
        assert dag.original_request == ((args.get("description") or "").strip() or None)


async def test_the_intent_exemption_leaves_every_other_required_argument_enforced(capture_env):
    """Review S6(b): D5 lifts the dispatcher's check for intent only."""
    env = await capture_env(**ON)
    ctx = ExecutionContext(kind="dag_summary", session_id="dag-summary-2")
    text, is_error = await _dispatch(env, "dag_create", {"name": "d", "intent": "x"}, ctx)
    assert is_error and "missing required argument(s): nodes" in text
    assert await _all(env, ExecutionDAG) == []


async def test_a_forged_intention_id_from_the_model_is_ignored(capture_env):
    """Review S2: a chat turn's model cannot attach its spawn to a lineage."""
    env = await capture_env(**ON)
    lineage = await _root(env, wake_policy="continue", authority="internal_only")
    text, is_error = await _dispatch(env, "spawn_task", {"task": "t", "intent": "x", "_intention_id": str(lineage.id)})
    assert not is_error, text
    (it,) = [i for i in await _all(env, Intention) if i.id != lineage.id]
    assert (it.root_id, it.parent_id, it.authority) == (it.id, None, "owner")


async def test_a_chat_spawn_records_a_continue_root_with_its_origin(capture_env):
    env = await capture_env(**ON)
    text, is_error = await _dispatch(
        env, "spawn_task", {"task": "Check the snow report", "intent": "  Tell the user\nabout the snow "}
    )
    assert not is_error, text
    (row,) = await _all(env, Subtask)
    (it,) = await _all(env, Intention)
    assert (it.source_kind, it.source_id) == ("subtask", str(row.id))
    assert (it.wake_policy, it.authority, it.root_id, it.depth) == ("continue", "owner", it.id, 0)
    assert (it.origin_kind, it.origin_session_id, it.origin_channel) == ("interactive", SESSION, CHAN)
    assert str(it.origin_decision_id) == PLAN and it.intent == "Tell the user about the snow"
    assert row.metadata_["intention"] == {"id": str(it.id), "root_id": str(it.id), "authority": "owner"}
    assert (row.parent_session_id, row.parent_channel) == (SESSION, CHAN)  # I5: routing as before


@pytest.mark.parametrize(
    ("tool", "args", "kind", "policy"),
    [
        ("spawn_task", {"task": "t", "await_result": True}, "subtask", "none"),
        ("spawn_sync", {"task": "t"}, "subtask", "none"),
        ("schedule_task", {"task": "t", "every": "30 minutes", "wake_policy": "report"}, "schedule", "container"),
        ("dag_create", {"name": "d", "nodes": NODES}, "dag", "continue"),
        ("spawn_task", {"task": "t", "wake_policy": "report"}, "subtask", "report"),
    ],
    ids=["inline", "spawn_sync", "schedule_task", "dag_create", "chat-override"],
)
async def test_each_spawn_tool_records_one_intention(capture_env, tool, args, kind, policy):
    env = await capture_env(**ON, subtask_hardening_enabled=(tool == "spawn_sync"))
    await _dispatch(env, tool, {**args, "intent": "Tell the user about the snow"})
    (it,) = await _all(env, Intention)
    assert (it.source_kind, it.wake_policy) == (kind, policy)


async def test_spawn_sync_records_the_session_on_the_intention_only(capture_env):
    env = await capture_env(**ON, subtask_hardening_enabled=True)
    await _dispatch(env, "spawn_sync", {"task": "t", "intent": "Tell the user about the snow"})
    (row,) = await _all(env, Subtask)
    (it,) = await _all(env, Intention)
    assert it.origin_session_id == SESSION
    assert row.parent_session_id is None  # I5: still no routing key


async def test_dag_create_stores_the_intent_as_its_original_request(capture_env):
    env = await capture_env(**ON)
    await _dispatch(env, "dag_create", {"name": "d", "description": "desc", "nodes": NODES, "intent": "Find the leak"})
    (dag,) = await _all(env, ExecutionDAG)
    assert dag.original_request == "Find the leak"


async def test_a_background_spawn_joins_its_turns_intention(capture_env):
    env = await capture_env(**ON)
    parent = await _root(env, wake_policy="remember")
    ctx = ExecutionContext(
        kind="scheduled", session_id="subtask-x", intention_id=parent.id, root_intention_id=parent.id
    )
    text, is_error = await _dispatch(
        env, "dag_create", {"name": "d", "nodes": NODES, "intent": "Launch the stages"}, ctx
    )
    assert not is_error, text
    (child,) = [i for i in await _all(env, Intention) if i.id != parent.id]
    assert (child.root_id, child.parent_id, child.depth) == (parent.id, parent.id, 1)
    assert (child.wake_policy, child.origin_kind) == ("remember", "scheduled")


async def test_a_background_spawn_with_no_intention_is_none_and_cannot_widen(capture_env):
    env = await capture_env(**ON)
    ctx = ExecutionContext(kind="subtask", session_id="subtask-y")
    await _dispatch(env, "dag_create", {"name": "d", "nodes": NODES, "intent": "x", "wake_policy": "continue"}, ctx)
    (it,) = await _all(env, Intention)
    assert (it.wake_policy, it.root_id) == ("none", it.id)


async def test_a_spawn_under_a_cancelled_root_is_refused(capture_env):
    env = await capture_env(**ON)
    root = await _root(env, cancelled=True)
    ctx = ExecutionContext(kind="scheduled", session_id="subtask-z", intention_id=root.id, root_intention_id=root.id)
    text, is_error = await _dispatch(env, "dag_create", {"name": "d", "nodes": NODES, "intent": "x"}, ctx)
    assert is_error and "cancelled or expired" in text
    assert await _all(env, ExecutionDAG) == []


async def test_a_spawn_naming_a_missing_intention_is_refused(capture_env):
    env = await capture_env(**ON)
    ctx = ExecutionContext(
        kind="subtask", session_id="subtask-m", intention_id=uuid.uuid4(), root_intention_id=uuid.uuid4()
    )
    text, is_error = await _dispatch(env, "spawn_task", {"task": "t", "intent": "x"}, ctx)
    assert is_error and "does not exist" in text
    assert await _all(env, Subtask) == []


WORK_ROW = {"spawn_task": Subtask, "schedule_task": Schedule, "spawn_sync": Subtask, "dag_create": ExecutionDAG}


@pytest.mark.parametrize(("tool", "args"), SPAWNS, ids=[s[0] for s in SPAWNS])
async def test_with_the_flag_off_nothing_is_recorded_and_no_intent_is_needed(capture_env, tool, args):
    env = await capture_env(result_inbox_enabled=True, subtask_hardening_enabled=(tool == "spawn_sync"))
    text, is_error = await _dispatch(env, tool, dict(args))
    # The spawn itself still works (review S6a): a new keyword reaching a
    # handler with the flag off would fail here, not pass silently.
    assert len(await _all(env, WORK_ROW[tool])) == 1, text
    if tool != "spawn_sync":  # the stub runner never submits a report, so spawn_sync's outcome is an error
        assert not is_error, text
    assert await _all(env, Intention) == []
    assert env.d._origin_aware == set()
