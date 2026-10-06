"""F099 section 4.1 Authority: a child is never wider than the turn that spawned it.

Narrowing the offered set (Tasks 2a.3 and 2a.4) is not enough if a continuation
could spawn an owner-authority child: the child's own turns would be wide. These
tests drive the real dispatcher, the spawn handlers and the stores.
"""

from __future__ import annotations

import dataclasses
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from test_f099_lineage import _recording_dispatcher

from nous.api.execution_context import ExecutionContext
from nous.api.tools import ToolDispatcher, register_dag_tools, register_subtask_tools
from nous.brain import intentions
from nous.config import Settings
from nous.dag.store import DAGStore
from nous.storage.models import Intention, Subtask

ON = {"result_inbox_enabled": True, "intentions_enabled": True}
NODES = [{"name": "n", "type": "subtask", "instructions": "x"}]
IID, RID = (uuid.UUID(f"00000000-0000-0000-0000-00000000000{n}") for n in (1, 2))


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
async def authority_env(db, mock_embeddings):
    from nous.heart import Heart

    hearts = []

    async def build(**over):
        agent = f"f099-auth-{uuid.uuid4().hex[:8]}"
        settings = Settings(
            _env_file=None,
            agent_id=agent,
            subtask_max_attempts=1,
            telegram_bot_token="",
            telegram_chat_id="",
            **{**ON, **over},
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


async def _dispatch(env, name, args, ctx):
    return await env.d.dispatch(name, args, session_id=ctx.session_id, context=ctx)


async def _all(env, model):
    async with env.db.session() as s:
        return list((await s.execute(select(model).where(model.agent_id == env.agent))).scalars().all())


async def _root(env, *, wake_policy="continue", authority="owner") -> Intention:
    rid = uuid.uuid4()
    row = Intention(
        id=rid,
        agent_id=env.agent,
        root_id=rid,
        depth=0,
        source_kind="subtask",
        source_id=str(uuid.uuid4()),
        intent="the turn's own intention",
        origin_kind="interactive",
        wake_policy=wake_policy,
        authority=authority,
        state="pending",
    )
    async with env.db.session() as s:
        s.add(row)
        await s.commit()
    return row


def _cont(root: Intention, **over) -> ExecutionContext:
    base = {
        "kind": "continuation",
        "session_id": f"intent-{root.root_id}",
        "authority": "internal_only",
        "intention_id": root.id,
        "root_intention_id": root.root_id,
    }
    return ExecutionContext(**{**base, **over})


def _children(rows, root) -> list[Intention]:
    return [i for i in rows if i.id != root.id]


# -- min(context authority, parent row authority) ---------------------------


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("spawn_task", {"task": "t", "intent": "why"}),
        ("dag_create", {"name": "d", "nodes": NODES, "intent": "why"}),
    ],
    ids=["spawn_task", "dag_create"],
)
async def test_a_continuation_spawn_under_an_owner_root_is_internal_only(authority_env, tool, args):
    env = await authority_env()
    root = await _root(env)  # an owner root row: the continuation's own turn is what is narrow
    text, is_error = await _dispatch(env, tool, dict(args), _cont(root))
    assert not is_error, text
    (child,) = _children(await _all(env, Intention), root)
    assert (child.parent_id, child.root_id, child.depth) == (root.id, root.id, 1)
    assert (child.authority, child.wake_policy, child.origin_kind) == ("internal_only", "continue", "continuation")


async def test_the_spawned_subtask_carries_the_narrowed_stamp(authority_env):
    env = await authority_env()
    root = await _root(env)
    await _dispatch(env, "spawn_task", {"task": "t", "intent": "why"}, _cont(root))
    (child,) = _children(await _all(env, Intention), root)
    (subtask,) = await _all(env, Subtask)
    assert subtask.metadata_["intention"] == {
        "id": str(child.id),
        "root_id": str(root.id),
        "authority": "internal_only",
    }


async def test_a_context_narrower_than_its_parent_row_wins(authority_env):
    env = await authority_env()
    root = await _root(env, authority="owner")
    ctx = ExecutionContext(
        kind="subtask",
        session_id="subtask-1",
        authority="internal_only",
        intention_id=root.id,
        root_intention_id=root.id,
    )
    await _dispatch(env, "spawn_task", {"task": "t", "intent": "why"}, ctx)
    (child,) = _children(await _all(env, Intention), root)
    assert child.authority == "internal_only"


@pytest.mark.parametrize(
    ("row_authority", "ctx_authority", "expected"),
    [
        ("internal_only", "owner", "internal_only"),
        ("owner", "owner", "owner"),
    ],  # PIN: never widened, never over-narrowed
)
async def test_a_child_is_not_widened_and_an_owner_lineage_stays_owner(
    authority_env, row_authority, ctx_authority, expected
):
    env = await authority_env()
    root = await _root(env, authority=row_authority)
    ctx = ExecutionContext(
        kind="subtask", session_id="subtask-1", authority=ctx_authority, intention_id=root.id, root_intention_id=root.id
    )
    await _dispatch(env, "spawn_task", {"task": "t", "intent": "why"}, ctx)
    (child,) = _children(await _all(env, Intention), root)
    assert child.authority == expected


async def test_a_lineage_spawn_that_asks_for_none_still_goes_to_the_continuation(authority_env):
    env = await authority_env()
    root = await _root(env, wake_policy="remember")
    text, is_error = await _dispatch(
        env, "spawn_task", {"task": "t", "intent": "why", "wake_policy": "none"}, _cont(root)
    )
    assert not is_error, text
    (child,) = _children(await _all(env, Intention), root)
    assert child.wake_policy == "continue"
    assert text.endswith("(wake_policy 'none' is not available from a continuation turn; recorded 'continue')")


def test_origin_authority_narrows_the_wake_policy_like_an_internal_parent():
    parent = intentions.ParentView(id=IID, root_id=IID, depth=0, authority="owner", wake_policy="remember")
    spec = intentions.IntentionSpec(
        intent="x", origin_kind="continuation", wake_policy="none", origin_authority="internal_only"
    )
    assert intentions.resolve_wake_policy(spec, parent) == "continue"
    assert intentions.resolve_wake_policy(dataclasses.replace(spec, inline=True), parent) == "none"
    # PIN: with no claim an owner parent's policy is followed as before.
    plain = intentions.IntentionSpec(intent="x", origin_kind="subtask")
    assert intentions.resolve_wake_policy(plain, parent) == "remember"


def test_an_unknown_origin_authority_fails_closed_and_none_claims_nothing():
    def spec(authority):
        return intentions.spec_from_tool_call(
            intent="x", wake_policy=None, origin_kind="subtask", origin_authority=authority
        )

    assert spec("root").origin_authority == "internal_only"
    assert spec("owner").origin_authority == "owner"
    assert spec(None).origin_authority is None


async def test_the_dispatcher_sets_origin_authority_and_the_model_cannot():
    d, seen = _recording_dispatcher({"spawn_task": True})
    owner = ExecutionContext(kind="interactive", session_id="S1")
    await d.dispatch("spawn_task", {"task": "t", "_origin_authority": "internal_only"}, session_id="S1", context=owner)
    assert seen["spawn_task"]["_origin_authority"] == "owner"
    narrow = ExecutionContext(
        kind="subtask", session_id="s", authority="internal_only", intention_id=IID, root_intention_id=RID
    )
    await d.dispatch("spawn_task", {"task": "t", "_origin_authority": "owner"}, session_id="s", context=narrow)
    assert seen["spawn_task"]["_origin_authority"] == "internal_only"


# -- no approval node from a lineage ----------------------------------------

APPROVAL = [
    {
        "name": "ask",
        "type": "approval",
        "instructions": "Proceed?",
        "options": [
            {"id": "go", "label": "Go", "outcome": "proceed"},
            {"id": "stop", "label": "Stop", "outcome": "stop"},
        ],
        "default_option": "stop",
    }
]


@pytest.mark.parametrize("approvals_on", [False, True])
async def test_an_internal_only_turn_cannot_create_an_approval_node(authority_env, approvals_on):
    env = await authority_env(dag_approval_nodes_enabled=approvals_on)
    root = await _root(env)
    text, is_error = await _dispatch(env, "dag_create", {"name": "d", "nodes": APPROVAL, "intent": "why"}, _cont(root))
    assert is_error and "an internal-only turn cannot create approval nodes" in text
    assert "resolve_intention(decision='ask')" in text
    assert _children(await _all(env, Intention), root) == []


async def test_an_owner_turn_meets_the_existing_approval_rules_unchanged(authority_env):  # PIN
    env = await authority_env()
    ctx = ExecutionContext(kind="interactive", session_id="S1")
    text, is_error = await _dispatch(env, "dag_create", {"name": "d", "nodes": APPROVAL, "intent": "why"}, ctx)
    assert is_error and "approval nodes are disabled" in text


# -- the D7 downgrade is visible --------------------------------------------

NOTE = "(wake_policy 'continue' is not available from a background turn; recorded 'none')"


async def test_a_downgraded_wake_policy_is_named_in_the_spawn_receipt(authority_env):
    env = await authority_env()
    bg = ExecutionContext(kind="background", session_id="bg-1")
    text, is_error = await _dispatch(env, "spawn_task", {"task": "t", "intent": "why", "wake_policy": "continue"}, bg)
    assert not is_error and text.endswith(NOTE), text
    (it,) = await _all(env, Intention)
    assert it.wake_policy == "none"


async def test_a_downgraded_wake_policy_is_named_in_the_dag_receipt(authority_env):
    env = await authority_env()
    bg = ExecutionContext(kind="background", session_id="bg-2")
    args = {"name": "d", "nodes": NODES, "intent": "why", "wake_policy": "continue"}
    text, is_error = await _dispatch(env, "dag_create", args, bg)
    assert not is_error and text.endswith(NOTE), text


async def test_a_wake_policy_that_was_honoured_adds_no_note(authority_env):  # PIN
    env = await authority_env()
    chat = ExecutionContext(kind="interactive", session_id="S1", channel="telegram:1")
    for policy in ("continue", "remember"):
        text, is_error = await _dispatch(env, "spawn_task", {"task": "t", "intent": "why", "wake_policy": policy}, chat)
        assert not is_error and "not available" not in text, text


async def test_no_wake_policy_argument_means_no_read(authority_env):  # PIN
    env = await authority_env()
    spy = AsyncMock(return_value="none")
    env.heart.intentions.wake_policy_for_source = spy
    bg = ExecutionContext(kind="background", session_id="bg-3")
    text, is_error = await _dispatch(env, "spawn_task", {"task": "t", "intent": "why"}, bg)
    assert not is_error and "not available" not in text
    spy.assert_not_called()


async def test_a_failed_read_adds_no_note_and_does_not_fail_the_spawn(authority_env):
    env = await authority_env()
    env.heart.intentions.wake_policy_for_source = AsyncMock(side_effect=RuntimeError("db down"))
    bg = ExecutionContext(kind="background", session_id="bg-4")
    text, is_error = await _dispatch(env, "spawn_task", {"task": "t", "intent": "why", "wake_policy": "continue"}, bg)
    assert not is_error and "Subtask spawned." in text and "not available" not in text
    assert len(await _all(env, Subtask)) == 1
