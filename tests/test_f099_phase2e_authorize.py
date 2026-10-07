"""F099 Phase 2e-3: the owner's cancel reaches every tool call (the view of cancelled roots in
``_authorize_tool_call``), and a continuation turn starts with no leftover session (carry-over 3)."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

import pytest
from f099_support import (
    CONT,
    ON,
    ScriptedModel,
    build_runner,
    env_factory,  # noqa: F401
    inbox_rows,
    make_root,
    record,
    runner_env,  # noqa: F401
    say,
    use,
)
from sqlalchemy import select

from nous.api import runner as runner_module
from nous.api.execution_context import ExecutionContext
from nous.api.models import Conversation, Message
from nous.cognitive.ledger_store import REFUSAL_CODES
from nous.handlers.continuation_runner import ContinuationRunner
from nous.storage.models import IntentionArrival

ROOT = uuid.uuid4()
MODES_OFF = {"tool_offered_set_enforcement_mode": "off", "tool_context_policy_mode": "off"}


def _ctx(kind: str = "subtask", *, authority: str = "owner", root=ROOT, **extra) -> ExecutionContext:
    if kind == "continuation":
        extra = {"intention_id": ROOT, **extra}
    if kind == "approved_action":
        extra = {"proposal_id": uuid.uuid4(), "declared_tools": ("web_search",), **extra}
    return ExecutionContext(kind=kind, session_id="s", authority=authority, root_intention_id=root, **extra)


class CountingView:
    """A view of cancelled roots that counts its questions."""

    def __init__(self, cancelled=()) -> None:
        self.cancelled, self.asked = set(cancelled), []

    def __call__(self, root_id) -> bool:
        self.asked.append(root_id)
        return root_id in self.cancelled


async def _runner(env_factory, **settings):  # noqa: F811
    env = await env_factory(**CONT, ANTHROPIC_API_KEY="test-key", **settings)
    runner, _cognitive, _dispatcher = build_runner(env, ScriptedModel())
    return env, runner


@pytest.mark.parametrize(
    ("kind", "authority"),
    [
        ("subtask", "owner"),  # a subtask of an owner root that was already running when the owner cancelled
        ("dag_node", "owner"),
        ("heartbeat_check", "owner"),
        ("scheduled", "owner"),
        ("subtask", "internal_only"),
        ("continuation", "internal_only"),
        ("approved_action", "owner"),
    ],
)
async def test_a_call_on_behalf_of_a_cancelled_root_is_refused_whatever_the_modes_say(env_factory, kind, authority):  # noqa: F811
    """Security: the view is checked at dispatch, for every kind and authority, with both enforcement modes off."""
    env, runner = await _runner(env_factory, **MODES_OFF)
    runner.set_cancelled_roots(CountingView({ROOT}))
    refusal = runner._authorize_tool_call(
        _ctx(kind, authority=authority), "web_search", frozenset({"web_search"}), "s", {"query": "snow"}
    )
    assert refusal is not None and refusal.code == "root_cancelled"
    assert "cancelled by the owner" in refusal.text


async def test_the_refusal_is_checked_before_the_strict_rule_and_a_cancelled_root_is_not_offered_anything(env_factory):  # noqa: F811
    env, runner = await _runner(env_factory)
    runner.set_cancelled_roots(CountingView({ROOT}))
    # An internal_only call that the strict rule would refuse as not offered is refused as cancelled: the owner's
    # stop is the answer the model reads.
    refusal = runner._authorize_tool_call(
        _ctx("continuation", authority="internal_only"), "send_email", frozenset(), "s", {}
    )
    assert refusal.code == "root_cancelled"


async def test_a_root_that_is_not_cancelled_is_untouched_by_the_view(env_factory):  # noqa: F811
    env, runner = await _runner(env_factory, **MODES_OFF)
    view = CountingView({uuid.uuid4()})
    runner.set_cancelled_roots(view)
    assert runner._authorize_tool_call(_ctx(), "web_search", frozenset({"web_search"}), "s", {}) is None
    assert view.asked == [ROOT]


async def test_a_fork_made_before_the_view_is_installed_refuses_a_cancelled_roots_call(env_factory):  # noqa: F811
    """The heartbeat's dedicated runner is a fork made at its start, and every lineage check and callback runs on it:
    a view installed later must reach it, as the snapshot store does."""
    env, runner = await _runner(env_factory, **MODES_OFF)
    forked = runner.fork(AsyncMock())
    runner.set_cancelled_roots(CountingView({ROOT}))
    refusal = forked._authorize_tool_call(
        _ctx("heartbeat_check"), "web_search", frozenset({"web_search"}), "s", {"query": "snow"}
    )
    assert refusal is not None and refusal.code == "root_cancelled"


async def test_a_fork_made_after_the_view_is_installed_refuses_a_cancelled_roots_call(env_factory):  # noqa: F811
    env, runner = await _runner(env_factory, **MODES_OFF)
    runner.set_cancelled_roots(CountingView({ROOT}))
    forked = runner.fork(AsyncMock())
    refusal = forked._authorize_tool_call(
        _ctx("heartbeat_check"), "web_search", frozenset({"web_search"}), "s", {"query": "snow"}
    )
    assert refusal is not None and refusal.code == "root_cancelled"


async def test_a_fork_of_a_runner_with_no_view_has_no_view(env_factory):  # noqa: F811  # PIN
    """Prod parity (continuation off): nothing installs a view, so a fork copies the default, which reads nothing."""
    env, runner = await _runner(env_factory)
    forked = runner.fork(AsyncMock())
    assert forked._root_cancelled is runner_module._no_cancelled_roots
    assert (
        forked._authorize_tool_call(_ctx("heartbeat_check"), "web_search", frozenset({"web_search"}), "s", {}) is None
    )


async def test_a_context_that_names_no_root_never_asks_the_view(env_factory):  # noqa: F811  # PIN
    """Prod parity: a chat turn, a heartbeat triage and every pre-F099 background context name no root, so for them
    the check is one `is not None` and the view is never called."""
    env, runner = await _runner(env_factory)
    view = CountingView({ROOT})
    runner.set_cancelled_roots(view)
    for kind in ("interactive", "mcp", "heartbeat_triage", "background", "subtask"):
        assert (
            runner._authorize_tool_call(_ctx(kind, root=None), "web_search", frozenset({"web_search"}), "s", {}) is None
        )
    assert view.asked == []


async def test_with_no_view_installed_nothing_is_cancelled_and_nothing_is_read(env_factory):  # noqa: F811  # PIN
    """Prod parity (continuation off): no runner installs a view, so the default answers False without a lookup, for
    every context, and the ordinary rules decide as they did before 2e."""
    env, runner = await _runner(env_factory, tool_offered_set_enforcement_mode="enforce")
    assert runner._root_cancelled is runner_module._no_cancelled_roots
    assert runner._root_cancelled(ROOT) is False
    assert runner._authorize_tool_call(_ctx(), "web_search", frozenset({"web_search"}), "s", {}) is None
    unoffered = runner._authorize_tool_call(_ctx(), "bash", frozenset({"web_search"}), "s", {})
    assert unoffered is not None and unoffered.code == "offered_set"  # the existing rule, unchanged


async def test_on_prods_flags_a_call_that_names_a_root_is_decided_as_one_that_names_none(env_factory):  # noqa: F811  # PIN
    """Prod parity under prod's exact flags (intentions, inbox and result memory on, continuation off, both modes at
    their default): nothing installs a view, and a lineage call (a stamped subtask, DAG node or check) gets the same
    answer as the same call with no root, for every kind, offered or not."""
    env = await env_factory(**ON, result_memory_enabled=True, ANTHROPIC_API_KEY="test-key")
    runner, _cognitive, _dispatcher = build_runner(env, ScriptedModel())
    assert env.settings.continuation_enabled is False
    assert runner._root_cancelled is runner_module._no_cancelled_roots

    def code(kind, tool, root):
        refusal = runner._authorize_tool_call(_ctx(kind, root=root), tool, frozenset({"web_search"}), "s", {})
        return None if refusal is None else refusal.code

    for kind in ("subtask", "dag_node", "heartbeat_check", "scheduled"):
        for tool in ("web_search", "bash"):
            assert code(kind, tool, ROOT) == code(kind, tool, None) != "root_cancelled"


def test_the_refusal_code_is_a_ledger_code():
    """`_ledger_blocked` writes the code into the durable row, and the ledger rejects a code it does not know."""
    assert "root_cancelled" in REFUSAL_CODES


async def test_a_cancelled_roots_tool_call_does_not_run_through_a_real_turn(runner_env):  # noqa: F811
    """End to end through the tool loop: the handler is never called, the model reads the refusal, and the blocked
    call is written to the ledger with the new code (a ledger that rejected the code would fail the turn)."""
    env = await runner_env(
        [use("web_search", query="snow")],
        [say("I stop.")],
        ANTHROPIC_API_KEY="test-key",
    )
    calls = []

    async def web_search(**kwargs):
        calls.append(kwargs)
        return {"content": [{"type": "text", "text": "40 cm"}]}

    env.dispatcher.register(
        "web_search", web_search, {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}
    )
    env.runner.set_cancelled_roots(CountingView({ROOT}))
    blocked = []

    class Ledger:
        async def record_blocked(self, **kwargs):
            if kwargs["refused_by"] not in REFUSAL_CODES:  # as the real store does
                raise ValueError(f"unknown refusal code {kwargs['refused_by']!r}")
            blocked.append(kwargs["refused_by"])

    env.runner.set_ledger_store(Ledger())
    ctx = ExecutionContext(kind="subtask", session_id="sub-1", root_intention_id=ROOT, intention_id=ROOT)
    await env.runner.run_turn("sub-1", "look", is_background=True, is_subtask=True, context=ctx)
    assert calls == [] and blocked == ["root_cancelled"]
    result_messages = [m for m in env.model.calls[-1]["messages"] if "cancelled by the owner" in str(m)]
    assert result_messages


# ---- carry-over 3: a turn starts with no leftover session ------------------------------------------------------


async def test_discard_conversation_forgets_the_whole_in_memory_session(env_factory):  # noqa: F811
    env, runner = await _runner(env_factory)
    sid = "intent-x"
    runner._conversations[sid] = Conversation(session_id=sid, messages=[Message(role="user", content="old")])
    runner._get_or_create_ledger(sid).record("learn_fact", {}, "ok", "success")
    runner._pending_corrections[sid] = ["nudge"]
    runner._compaction_locks[sid] = object()
    runner.discard_conversation(sid)
    assert (sid in runner._conversations, sid in runner._ledgers) == (False, False)
    assert (sid in runner._pending_corrections, sid in runner._compaction_locks) == (False, False)
    assert runner.executed_tools(sid) == []
    runner.discard_conversation(sid)  # nothing to forget is not an error


async def test_a_continuation_thread_is_never_restored_from_the_database(env_factory):  # noqa: F811
    env, runner = await _runner(env_factory)
    env.heart.load_conversation_state = AsyncMock(
        return_value={"messages": [{"role": "user", "content": "from a crashed arrival"}], "summary": None}
    )
    assert await runner._restore_conversation("intent-" + str(ROOT)) is None
    env.heart.load_conversation_state.assert_not_awaited()
    ordinary = await runner._restore_conversation("S1")  # every other session restores as before
    assert ordinary is not None and ordinary.messages[0].content == "from a crashed arrival"


async def test_a_leftover_session_does_not_reach_the_next_arrival(runner_env):  # noqa: F811
    """The previous arrival's end_conversation failed: its messages and its `learn_fact` stayed in memory. The next
    arrival must not see the messages, and must not verify a false `progress` with the leftover ledger."""
    env = await runner_env([use("resolve_intention", decision="report", note="Done.", progress=True, confidence=0.6)])
    cont = ContinuationRunner(
        database=env.db, settings=env.settings, runner=env.runner, heart=env.heart, brain=env.brain, bus=env.bus
    )
    root = await make_root(env)
    await record(env, root)
    sid = f"intent-{root.id}"
    env.runner._conversations[sid] = Conversation(
        session_id=sid,
        messages=[Message(role="user", content="LEFTOVER ask"), Message(role="assistant", content="LEFTOVER reply")],
    )
    env.runner._get_or_create_ledger(sid).record("learn_fact", {"fact": "x"}, "stored", "success")

    done = await cont.run_arrival(root.id)

    assert done is not None
    (call,) = env.model.calls
    assert "LEFTOVER" not in str(call["messages"])
    async with env.db.session() as s:
        (arrival,) = (await s.execute(select(IntentionArrival).where(IntentionArrival.root_id == root.id))).scalars()
    # Claimed true, and nothing this arrival did backs it (no spawn, no revise, no memory write): stored false.
    assert (arrival.progress_claimed, arrival.progress) == (True, False)
    assert [r for r in await inbox_rows(env) if r.source_kind == "intention_report"]  # the report was written
