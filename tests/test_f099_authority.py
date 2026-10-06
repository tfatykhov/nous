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


async def test_a_failed_read_adds_no_note_and_does_not_fail_the_spawn(authority_env):  # PIN
    env = await authority_env()
    env.heart.intentions.wake_policy_for_source = AsyncMock(side_effect=RuntimeError("db down"))
    bg = ExecutionContext(kind="background", session_id="bg-4")
    text, is_error = await _dispatch(env, "spawn_task", {"task": "t", "intent": "why", "wake_policy": "continue"}, bg)
    assert not is_error and "Subtask spawned." in text and "not available" not in text
    assert len(await _all(env, Subtask)) == 1


# ---------------------------------------------------------------------------
# Task 2a.7: cancel_task only on the turn's own lineage; lineage web calls are logged
# ---------------------------------------------------------------------------

import logging  # noqa: E402
import re  # noqa: E402

from nous.storage.models import Schedule  # noqa: E402


async def _spawn(env, ctx, task="work") -> uuid.UUID:
    text, is_error = await _dispatch(env, "spawn_task", {"task": task, "intent": "why"}, ctx)
    assert not is_error, text
    return uuid.UUID(re.search(r"ID: ([0-9a-f-]{36})", text).group(1))


async def _status(env, task_id: uuid.UUID) -> str:
    async with env.db.session() as s:
        return (await s.execute(select(Subtask.status).where(Subtask.id == task_id))).scalar_one()


async def _cancel(env, task_id, ctx, **extra):
    return await _dispatch(env, "cancel_task", {"task_id": str(task_id), **extra}, ctx)


async def test_a_continuation_may_cancel_work_of_its_own_lineage(authority_env):
    env = await authority_env()
    root = await _root(env)
    cont = _cont(root)
    child = await _spawn(env, cont)
    text, is_error = await _cancel(env, child, cont)
    assert not is_error and "cancelled" in text, text
    assert await _status(env, child) == "cancelled"


async def test_a_continuation_may_not_cancel_foreign_work(authority_env):
    env = await authority_env()
    root = await _root(env)
    cont = _cont(root)
    chat = ExecutionContext(kind="interactive", session_id="S1", channel="telegram:1")
    foreign = await _spawn(env, chat)  # the owner's own background task, a root of its own
    other_root = await _root(env)
    siblings_lineage = await _spawn(env, _cont(other_root))
    legacy = (await env.heart.subtasks.create(task="work from before F099")).id  # no intention row
    for target in (foreign, siblings_lineage, legacy):
        text, is_error = await _cancel(env, target, cont)
        assert is_error and "is not part of this lineage" in text, text
        assert await _status(env, target) == "pending"


async def test_another_agents_intention_row_never_puts_a_task_in_this_lineage(authority_env):
    """The lineage lookup is agent-scoped. Another agent's row names this agent's subtask
    and root R; only the agent_id filter of get_for_source keeps R's continuation from it."""
    env = await authority_env()
    root = await _root(env)
    target = (await env.heart.subtasks.create(task="this agent's work, no intention row")).id
    async with env.db.session() as s:
        s.add(
            Intention(
                id=uuid.uuid4(),
                agent_id=f"f099-auth-other-{uuid.uuid4().hex[:8]}",
                root_id=root.id,
                parent_id=root.id,
                depth=1,
                source_kind="subtask",
                source_id=str(target),
                intent="another agent's row",
                origin_kind="interactive",
                wake_policy="continue",
                authority="internal_only",
                state="pending",
            )
        )
        await s.commit()
    text, is_error = await _cancel(env, target, _cont(root))
    assert is_error and "is not part of this lineage" in text, text
    assert await _status(env, target) == "pending"


async def test_a_model_cannot_forge_the_authority_or_the_root_the_handler_checks(authority_env):
    env = await authority_env()
    root = await _root(env)
    foreign = await _spawn(env, ExecutionContext(kind="interactive", session_id="S1", channel="telegram:1"))
    forged = {"_authority": "owner", "_root_intention_id": str(foreign)}
    text, is_error = await _cancel(env, foreign, _cont(root), **forged)
    assert is_error and "is not part of this lineage" in text
    assert await _status(env, foreign) == "pending"


async def test_a_lineage_with_a_damaged_stamp_cancels_nothing(authority_env):
    """Fail closed (C8): internal_only with no root id cannot prove any target is its own."""
    env = await authority_env()
    root = await _root(env)
    child = await _spawn(env, _cont(root))
    damaged = ExecutionContext(kind="subtask", session_id="subtask-1", authority="internal_only")
    text, is_error = await _cancel(env, child, damaged)
    assert is_error and "is not part of this lineage" in text
    assert await _status(env, child) == "pending"


async def test_an_owner_turn_still_cancels_any_task(authority_env):  # PIN
    env = await authority_env()
    foreign = await _spawn(env, ExecutionContext(kind="interactive", session_id="S1", channel="telegram:1"))
    text, is_error = await _cancel(env, foreign, ExecutionContext(kind="interactive", session_id="S2"))
    assert not is_error and "cancelled" in text, text
    assert await _status(env, foreign) == "cancelled"


async def test_a_cancel_task_dispatch_from_an_owner_turn_injects_nothing():  # PIN
    d, seen = _recording_dispatcher({"cancel_task": False})
    owner = ExecutionContext(kind="interactive", session_id="S1")
    await d.dispatch("cancel_task", {"task_id": "x"}, session_id="S1", context=owner)
    assert not [k for k in seen["cancel_task"] if k.startswith("_")]


async def test_a_cancel_task_dispatch_from_an_internal_only_turn_injects_the_authority_and_root():
    d, seen = _recording_dispatcher({"cancel_task": False})
    lineage = ExecutionContext(
        kind="subtask", session_id="s", authority="internal_only", intention_id=IID, root_intention_id=RID
    )
    damaged = ExecutionContext(kind="subtask", session_id="s", authority="internal_only")
    await d.dispatch("cancel_task", {"task_id": "x"}, session_id="s", context=lineage)
    assert (seen["cancel_task"]["_authority"], seen["cancel_task"]["_root_intention_id"]) == ("internal_only", str(RID))
    await d.dispatch("cancel_task", {"task_id": "x"}, session_id="s", context=damaged)
    assert (seen["cancel_task"]["_authority"], seen["cancel_task"]["_root_intention_id"]) == ("internal_only", "")


async def test_the_dispatcher_sets_the_cancel_authority_and_root_and_the_model_cannot():
    """Forged values for both hidden arguments: the turn's own replace them, and an owner
    turn passes neither on (a forged ``_authority`` cannot narrow or widen it)."""
    d, seen = _recording_dispatcher({"cancel_task": False})
    forged = {"task_id": "x", "_authority": "owner", "_root_intention_id": str(IID)}
    lineage = ExecutionContext(
        kind="subtask", session_id="s", authority="internal_only", intention_id=IID, root_intention_id=RID
    )
    await d.dispatch("cancel_task", dict(forged), session_id="s", context=lineage)
    assert (seen["cancel_task"]["_authority"], seen["cancel_task"]["_root_intention_id"]) == ("internal_only", str(RID))
    damaged = ExecutionContext(kind="subtask", session_id="s", authority="internal_only")
    await d.dispatch("cancel_task", dict(forged), session_id="s", context=damaged)
    assert (seen["cancel_task"]["_authority"], seen["cancel_task"]["_root_intention_id"]) == ("internal_only", "")
    owner = ExecutionContext(kind="interactive", session_id="S1")
    await d.dispatch("cancel_task", {**forged, "_authority": "internal_only"}, session_id="S1", context=owner)
    assert seen["cancel_task"] == {"task_id": "x"}


async def test_a_forged_root_naming_the_targets_own_lineage_cancels_nothing(authority_env):
    """The forged root is the foreign target's REAL root: it would pass the handler's check
    if the model's value reached it."""
    env = await authority_env()
    root = await _root(env)
    foreign = await _spawn(env, ExecutionContext(kind="interactive", session_id="S1", channel="telegram:1"))
    its_root = (await env.heart.intentions.get_for_source("subtask", foreign)).root_id
    for ctx in (_cont(root), ExecutionContext(kind="subtask", session_id="subtask-1", authority="internal_only")):
        text, is_error = await _cancel(env, foreign, ctx, _root_intention_id=str(its_root))
        assert is_error and "is not part of this lineage" in text, text
        assert await _status(env, foreign) == "pending"


async def _schedule(env, ctx) -> uuid.UUID:
    args = {"task": "t", "every": "30 minutes", "intent": "why"}
    text, is_error = await _dispatch(env, "schedule_task", args, ctx)
    assert not is_error, text
    return uuid.UUID(re.search(r"ID: ([0-9a-f-]{36})", text).group(1))


async def test_a_schedule_is_cancelled_only_from_the_lineage_that_created_it(authority_env):
    """A schedule's container joins the lineage of the owner turn that created it (a lineage
    cannot call schedule_task itself); one from the owner's chat is a root of its own."""
    env = await authority_env()
    root = await _root(env)
    in_lineage = ExecutionContext(
        kind="subtask", session_id="subtask-1", intention_id=root.id, root_intention_id=root.id
    )
    own = await _schedule(env, in_lineage)
    foreign = await _schedule(env, ExecutionContext(kind="interactive", session_id="S1", channel="telegram:1"))
    text, is_error = await _cancel(env, foreign, _cont(root))
    assert is_error and "is not part of this lineage" in text, text
    text, is_error = await _cancel(env, own, _cont(root))
    assert not is_error and "deactivated" in text, text
    active = {s.id: s.active for s in await _all(env, Schedule)}
    assert active == {own: False, foreign: True}


async def test_web_calls_from_a_lineage_are_logged_with_their_root(caplog):
    d, _ = _recording_dispatcher({"web_fetch": False, "web_search": False, "recall_deep": False})
    lineage = ExecutionContext(
        kind="subtask", session_id="subtask-1", authority="internal_only", intention_id=IID, root_intention_id=RID
    )
    owner = ExecutionContext(kind="subtask", session_id="subtask-2")
    with caplog.at_level(logging.INFO, logger="nous.api.tools"):
        await d.dispatch("web_fetch", {}, session_id="subtask-1", context=lineage)
        await d.dispatch("web_search", {}, session_id="subtask-1", context=lineage)
        await d.dispatch("recall_deep", {}, session_id="subtask-1", context=lineage)
        await d.dispatch("web_fetch", {}, session_id="subtask-2", context=owner)
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("F099: web_")]
    assert lines == [
        f"F099: web_fetch from lineage root {RID} (session subtask-1)",
        f"F099: web_search from lineage root {RID} (session subtask-1)",
    ]
