"""F099 Phase 2d-4: one approved call runs through the shared ledger bracket, under approved_action."""

from __future__ import annotations

import asyncio
import inspect
import uuid

import pytest
from test_runner_authorization import _MockBrain, _MockCognitive, _MockHeart, _RecordingDispatcher, _settings
from test_runner_ledger import _FakeSnapStore, _FakeStore, _held, _runner

from nous.api import compensation
from nous.api.execution_context import ExecutionContext
from nous.api.idempotency import idempotency_key
from nous.api.runner import AgentRunner, Dispatched, SingleCall
from nous.api.tools import ToolDispatcher, _origin_args
from nous.brain import intentions

SEND = {"to": "friend@example.com", "subject": "Snow", "body": "40 cm overnight."}


def _ctx(proposal_id=None, tool="send_email", **over) -> ExecutionContext:
    proposal_id = proposal_id or uuid.uuid4()
    values = {
        "kind": "approved_action",
        "session_id": f"proposal-{proposal_id}",
        "proposal_id": proposal_id,
        "declared_tools": (tool,),
        "root_intention_id": uuid.uuid4(),
        "intention_id": uuid.uuid4(),
        **over,
    }
    return ExecutionContext(**values)


# ---- the scope -----------------------------------------------------------------------------------------------


def test_the_idempotency_scope_of_an_approved_send_is_its_proposal():
    proposal_id = uuid.uuid4()
    key = idempotency_key(_ctx(proposal_id), "send_email", SEND)
    assert key is not None and key.startswith(f"proposal:{proposal_id}:")
    assert idempotency_key(_ctx(uuid.uuid4()), "send_email", SEND) != key  # another proposal, another send
    # Conflict C11, stated as a test: only the keyed sends have a key; every other tool has the state fence alone.
    assert idempotency_key(_ctx(proposal_id, tool="bash"), "bash", {"command": "ls"}) is None


def test_the_other_scopes_are_unchanged():  # PIN
    subtask_id = uuid.uuid4()
    ctx = ExecutionContext(kind="subtask", session_id="s", subtask_id=subtask_id)
    assert idempotency_key(ctx, "send_email", SEND).startswith(f"subtask:{subtask_id}:")
    assert idempotency_key(ExecutionContext(kind="interactive", session_id="s"), "send_email", SEND) is None


# ---- execute_single_call -------------------------------------------------------------------------------------


async def test_the_one_call_runs_through_the_ledger_bracket_under_its_context():
    store = _FakeStore()
    runner, dispatcher = _runner(store, offered=("send_email",))
    proposal_id = uuid.uuid4()
    ctx = _ctx(proposal_id)
    out = await runner.execute_single_call(ctx, "send_email", dict(SEND))
    assert isinstance(out, SingleCall) and (out.text, out.is_error) == ("send_email ran", False)
    assert out.send_key.startswith(f"proposal:{proposal_id}:")
    assert store.events == [
        ("open", "send_email", "approved_action"),
        ("dispatch", "send_email"),
        ("close", "id-send_email", "success"),
    ]
    assert store.keys == [out.send_key] and store.claimed == ["id-send_email"]  # a keyed send is claimed first
    ((name, seen, background),) = dispatcher.calls
    assert (name, seen is ctx, background) == ("send_email", True, True)
    assert seen.authority == "owner"  # the owner approved this one call


async def test_a_second_run_of_the_same_proposal_is_suppressed_by_its_key():
    """The ledger key is the second fence behind claim_execution, for a keyed send."""
    store = _FakeStore(duplicate=_held("success"))
    runner, dispatcher = _runner(store, offered=("send_email",))
    out = await runner.execute_single_call(_ctx(), "send_email", dict(SEND))
    assert dispatcher.calls == []
    assert out.is_error is False and "Already sent" in out.text  # never sent twice
    assert ("blocked", "send_email", "duplicate") in store.events


async def test_a_call_to_any_other_tool_is_refused_and_recorded():
    store = _FakeStore()
    runner, dispatcher = _runner(store, offered=("send_email", "bash"))
    out = await runner.execute_single_call(_ctx(tool="send_email"), "bash", {"command": "ls"})
    assert out.is_error and "undeclared" in out.text and out.send_key is None
    assert dispatcher.calls == [] and ("blocked", "bash", "internal_only") in store.events


async def test_an_internal_only_context_is_refused_an_external_send():
    """Carry-over 4: the approved context must carry owner authority. With internal_only the strict path refuses
    the very call the owner approved, so execute_approved_proposal builds the context with the default authority."""
    store = _FakeStore()
    runner, dispatcher = _runner(store, offered=("send_email",))
    out = await runner.execute_single_call(_ctx(authority="internal_only"), "send_email", dict(SEND))
    assert out.is_error and "external" in out.text and dispatcher.calls == []


async def test_only_an_approved_action_context_may_use_it():
    runner, _dispatcher = _runner(_FakeStore(), offered=("send_email",))
    with pytest.raises(ValueError, match="approved_action"):
        await runner.execute_single_call(ExecutionContext(kind="subtask", session_id="s"), "send_email", dict(SEND))


class _Raising(_RecordingDispatcher):
    def __init__(self, offered, store, exc):
        super().__init__(offered, store)
        self._exc = exc

    async def dispatch(self, name, inp, **kwargs):
        raise self._exc


async def test_a_dispatch_that_raises_closes_its_ledger_row_and_re_raises():
    store = _FakeStore()
    runner, _ = _runner(store, offered=("send_email",))
    runner.set_dispatcher(_Raising(["send_email"], store, RuntimeError("smtp is down")))
    with pytest.raises(RuntimeError):
        await runner.execute_single_call(_ctx(), "send_email", dict(SEND))
    assert store.closes[-1][0] == "error"  # the row is not left pending


async def test_a_cancelled_dispatch_closes_the_row_unknown_and_re_raises():
    """A shutdown mid-send: the message may or may not have gone. The row says so, and nothing re-runs it."""
    store = _FakeStore()
    runner, _ = _runner(store, offered=("send_email",))
    runner.set_dispatcher(_Raising(["send_email"], store, asyncio.CancelledError()))
    with pytest.raises(asyncio.CancelledError):
        await runner.execute_single_call(_ctx(), "send_email", dict(SEND))
    assert store.closes[-1][0] == "unknown"


# ---- the origin stamp (C12) ----------------------------------------------------------------------------------


def test_an_approved_action_stamps_internal_only_so_what_it_starts_cannot_widen():
    """A root intention's own authority is owner, so the stamp is what narrows the child of an approved spawn."""
    ctx = _ctx()
    stamp = _origin_args(ctx)
    assert (stamp["_origin_kind"], stamp["_origin_authority"]) == ("approved_action", "internal_only")
    assert stamp["_intention_id"] == str(ctx.intention_id)  # it still joins the lineage it was proposed in


def test_every_other_kind_stamps_its_own_authority():  # PIN
    internal = {"authority": "internal_only", "intention_id": uuid.uuid4(), "root_intention_id": uuid.uuid4()}
    continuation_ctx = ExecutionContext(kind="continuation", session_id="intent-x", **internal)
    subtask_ctx = ExecutionContext(kind="subtask", session_id="s", **internal)
    assert _origin_args(continuation_ctx)["_origin_authority"] == "internal_only"
    assert _origin_args(subtask_ctx)["_origin_authority"] == "internal_only"
    for kind in ("interactive", "subtask", "scheduled"):
        assert _origin_args(ExecutionContext(kind=kind, session_id="s"))["_origin_authority"] == "owner"


# ---- the move ------------------------------------------------------------------------------------------------


def test_the_loop_reaches_the_bracket_only_through_the_shared_helper():
    """One definition of the ledger invariants for the non-streaming path: the loop no longer opens rows itself."""
    source = inspect.getsource(AgentRunner._tool_loop)
    assert "_dispatch_with_ledger(" in source and "_open_for_call(" not in source
    helper = inspect.getsource(AgentRunner._dispatch_with_ledger)
    assert "_open_for_call(" in helper and "_ledger_close(" in helper and "_after_compensable_call(" in helper
    assert {"text", "is_error", "suppressed", "send_key"} == set(Dispatched._fields)


# ---- lead addendum 1 (2d-1 review m5): the handler's own required parameters ----------------------------------


async def test_validate_call_refuses_a_call_missing_a_parameter_only_the_handler_requires():
    """The stored call is re-validated before it runs (2d-5, before claim_execution) with the same function that
    staged it, so ``validate_call`` must also know what the handler cannot do without: a parameter the schema does
    not mark required but the handler's signature does would fail at dispatch, and the owner would have approved
    a call that cannot run. A parameter named with an underscore is the dispatcher's to inject, never demanded."""

    async def touch(*, path: str, mode: str = "w", _session_id: str = "") -> dict:
        return {"content": [{"type": "text", "text": f"touched {path}"}]}

    async def variadic(**kwargs) -> dict:
        return {"content": [{"type": "text", "text": "ok"}]}

    dispatcher = ToolDispatcher()
    dispatcher.register("touch", touch, {"type": "object", "properties": {"path": {"type": "string"}}})
    dispatcher.register("both", touch, {"type": "object", "required": ["path"]})
    dispatcher.register("variadic", variadic, {"type": "object"})
    assert dispatcher.validate_call("touch", {}) == ["missing required argument 'path'"]
    assert dispatcher.validate_call("touch", {"path": "a.txt"}) == []
    assert dispatcher.validate_call("both", {}) == ["missing required argument 'path'"]  # named once, not twice
    assert dispatcher.validate_call("variadic", {}) == []  # the signature says nothing: the schema decides
    # What validate_call now refuses is exactly what dispatch cannot run.
    text, is_error = await dispatcher.dispatch("touch", {})
    assert is_error and "path" in text


# ---- lead addendum (2d-4 review m1 to m3) ---------------------------------------------------------------------


def test_an_approved_context_without_an_intention_fails_closed_instead_of_making_a_root():
    """2d-4 review m1: the unreadable-lineage fallback keys on the STAMPED authority. An approved_action context
    stamps internal_only, so without an intention id its spawn is refused like a damaged stamp, never made an
    internal_only root with no continuation to return to."""
    stamp = _origin_args(_ctx(intention_id=None, root_intention_id=None))
    assert stamp["_origin_authority"] == "internal_only"
    assert stamp["_intention_id"] == intentions.UNREADABLE_LINEAGE


def test_the_unreadable_lineage_fallback_is_unchanged_for_every_other_kind():  # PIN
    for kind in ("subtask", "dag_node"):
        ctx = ExecutionContext(kind=kind, session_id="s", authority="internal_only")
        assert _origin_args(ctx)["_intention_id"] == intentions.UNREADABLE_LINEAGE
    for kind in ("interactive", "subtask", "scheduled"):
        assert "_intention_id" not in _origin_args(ExecutionContext(kind=kind, session_id="s"))


async def test_a_single_call_without_a_dispatcher_fails_like_the_loops():
    """2d-4 review m2: the same guard and the same text as _tool_loop and stream_chat."""
    runner = AgentRunner(_MockCognitive(), _MockBrain(), _MockHeart(), _settings())
    with pytest.raises(RuntimeError, match="No tool dispatcher set"):
        await runner.execute_single_call(_ctx(), "send_email", dict(SEND))


async def test_a_dispatch_that_raises_still_releases_the_write_lock(tmp_path):  # PIN
    """2d-4 review m3: the path lock a write takes is released on the error path too, not only after a success."""
    store = _FakeStore()
    runner, _ = _runner(store, offered=("write_file",), compensation_enabled=True)
    runner.set_snapshot_store(_FakeSnapStore(), str(tmp_path))
    key = compensation.write_path_key("plain.txt", str(tmp_path))
    held_at_raise: list[bool] = []

    class _RaisingWrite(_RecordingDispatcher):
        async def dispatch(self, name, inp, **kwargs):
            lock = compensation._write_path_locks.get(key)
            held_at_raise.append(lock is not None and lock.locked())
            raise RuntimeError("disk is full")

    runner.set_dispatcher(_RaisingWrite(["write_file"], store))
    with pytest.raises(RuntimeError, match="disk is full"):
        await runner.execute_single_call(_ctx(tool="write_file"), "write_file", {"path": "plain.txt", "content": "x"})
    assert held_at_raise == [True]  # the call held the lock when it raised
    assert key not in compensation._write_path_locks  # and the error path released it
