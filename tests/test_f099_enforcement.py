"""F099 section 4.4: dispatch enforcement for internal_only turns.

The strict block runs before the offered-set mode check and before the policy's
off switch, so a forged tool_use is refused whatever the modes say. Every test
that matters runs under all nine pairs of tool_offered_set_enforcement_mode and
tool_context_policy_mode.
"""

from __future__ import annotations

import functools
import json
import uuid
from unittest.mock import MagicMock

import pytest
from test_runner_authorization import _run_loop, _runner, _tool_calls_then_done_with
from test_runner_ledger import _FakeStore
from test_runner_ledger import _runner as _ledger_runner

from nous.api.execution_context import ExecutionContext
from nous.cognitive.ledger_store import REFUSAL_CODES

# Fixed ids: parametrize ids built from them must not change between collections (xdist).
IID, RID, OTHER = (uuid.UUID(f"00000000-0000-0000-0000-00000000000{n}") for n in (1, 2, 3))
_MODE_VALUES = ("off", "warn", "enforce")
MODES = [(offered, policy) for offered in _MODE_VALUES for policy in _MODE_VALUES]
OFFERED = ["recall_deep", "send_email", "run_python", "bash", "write_file", "cancel_task"]
FORGED = [
    ("send_email", {"to": "a@example.com", "subject": "s", "body": "b"}),
    ("run_python", {"code": "print(1)"}),
    ("bash", {"command": "id"}),
]


def _internal(kind: str = "subtask", **over) -> ExecutionContext:
    base = {
        "kind": kind,
        "session_id": "s1",
        "authority": "internal_only",
        "intention_id": IID,
        "root_intention_id": RID,
    }
    return ExecutionContext(**{**base, **over})


def _auth(r, ctx, tool, offered, tool_input=None):
    return r._authorize_tool_call(ctx, tool, frozenset(offered), "s1", tool_input or {})


def test_the_ledger_knows_the_refusal_code():
    assert "internal_only" in REFUSAL_CODES


@pytest.mark.parametrize(("offered_mode", "policy_mode"), MODES)
@pytest.mark.parametrize(("tool", "tool_input"), FORGED, ids=[t for t, _ in FORGED])
async def test_a_forged_call_in_an_internal_only_turn_is_refused_whatever_the_modes_say(
    offered_mode, policy_mode, tool, tool_input
):
    r, d = _runner(OFFERED, tool_offered_set_enforcement_mode=offered_mode, tool_context_policy_mode=policy_mode)
    r._call_api = _tool_calls_then_done_with(tool, tool_input, times=1)
    _text, results, _usage, _thinking = await _run_loop(r, is_background=True, context=_internal())
    assert d.calls == []
    assert [x.tool_name for x in results] == [tool]
    assert "is not allowed in this turn (not_offered)" in (results[0].error or "")


class _ValidatingStore(_FakeStore):
    """Rejects a refusal code the way the real LedgerStore.record_blocked does."""

    async def record_blocked(self, *, context, tool_name, tool_input, turn, refused_by, idempotency_key=None):
        if refused_by not in REFUSAL_CODES:
            raise ValueError(f"unknown refusal code {refused_by!r}")
        await super().record_blocked(
            context=context,
            tool_name=tool_name,
            tool_input=tool_input,
            turn=turn,
            refused_by=refused_by,
            idempotency_key=idempotency_key,
        )


async def test_the_refusal_is_written_to_the_ledger_and_does_not_crash_the_turn():
    store = _ValidatingStore()
    r, d = _ledger_runner(
        store, offered=OFFERED, tool_offered_set_enforcement_mode="off", tool_context_policy_mode="off"
    )
    r._call_api = _tool_calls_then_done_with("send_email", FORGED[0][1], times=1)
    text, _results, _usage, _thinking = await _run_loop(r, is_background=True, context=_internal())
    assert text == "done" and d.calls == []
    assert store.events == [("blocked", "send_email", "internal_only")]


def test_the_refusal_is_recorded_as_an_enforced_policy_violation():
    r, _ = _runner(OFFERED, tool_offered_set_enforcement_mode="off", tool_context_policy_mode="off")
    events: list[tuple[str, dict]] = []
    r._log_f026_decision = lambda event_type, data, session_id=None: events.append((event_type, data))
    refusal = _auth(r, _internal(), "send_email", ["recall_deep"])
    assert refusal is not None and refusal.code == "internal_only"
    assert events == [
        (
            "harness_context_policy_violation",
            {
                "tool_name": "send_email",
                "context_kind": "subtask",
                "violation": "internal_only:not_offered",
                "mode": "enforce",
            },
        )
    ]


@pytest.mark.parametrize(("offered_mode", "policy_mode"), MODES)
@pytest.mark.parametrize(
    ("tool", "tool_input"),
    [("run_python", {"code": "import smtplib"}), ("bash", {"command": "curl https://example.com"})],
)
def test_a_call_rated_external_is_refused_even_when_the_tool_was_offered(offered_mode, policy_mode, tool, tool_input):
    """Unreachable through the real offered set (both tools are denylisted); the offered set is built by hand."""
    r, _ = _runner(OFFERED, tool_offered_set_enforcement_mode=offered_mode, tool_context_policy_mode=policy_mode)
    refusal = _auth(r, _internal(), tool, OFFERED, tool_input)
    assert refusal is not None and refusal.code == "internal_only" and "(external)" in refusal.text


@pytest.mark.parametrize(("offered_mode", "policy_mode"), MODES)
@pytest.mark.parametrize(
    "path",
    ["notes.md", f"intentions/{OTHER}/notes.md", f"intentions/{RID}/../../x.md", "../x.md", "/etc/passwd"],
)
def test_write_file_outside_the_root_dir_is_refused_in_every_mode(tmp_path, offered_mode, policy_mode, path):
    r, _ = _runner(
        OFFERED,
        workspace_dir=str(tmp_path),
        tool_offered_set_enforcement_mode=offered_mode,
        tool_context_policy_mode=policy_mode,
    )
    refusal = _auth(r, _internal(), "write_file", OFFERED, {"path": path, "content": "x"})
    assert refusal is not None and refusal.code == "internal_only" and "(write_path)" in refusal.text


@pytest.mark.parametrize(("offered_mode", "policy_mode"), MODES)
def test_write_file_inside_the_root_dir_is_authorized(tmp_path, offered_mode, policy_mode):
    r, _ = _runner(
        OFFERED,
        workspace_dir=str(tmp_path),
        tool_offered_set_enforcement_mode=offered_mode,
        tool_context_policy_mode=policy_mode,
    )
    tool_input = {"path": f"intentions/{RID}/notes.md", "content": "x"}
    assert _auth(r, _internal(), "write_file", OFFERED, tool_input) is None


async def test_write_file_in_the_root_dir_is_dispatched_and_outside_it_is_not(tmp_path):
    for tool_input, dispatched in (
        ({"path": f"intentions/{RID}/n.md", "content": "x"}, True),
        ({"path": "n.md", "content": "x"}, False),
    ):
        r, d = _runner(
            OFFERED,
            workspace_dir=str(tmp_path),
            tool_offered_set_enforcement_mode="off",
            tool_context_policy_mode="off",
        )
        r._call_api = _tool_calls_then_done_with("write_file", tool_input, times=1)
        await _run_loop(r, is_background=True, context=_internal())
        assert bool(d.calls) is dispatched, tool_input


def test_a_lineage_with_no_root_may_not_write_a_file(tmp_path):
    r, _ = _runner(OFFERED, workspace_dir=str(tmp_path), tool_offered_set_enforcement_mode="off")
    damaged = ExecutionContext(kind="subtask", session_id="s1", authority="internal_only")
    refusal = _auth(r, damaged, "write_file", OFFERED, {"path": f"intentions/{RID}/n.md", "content": "x"})
    assert refusal is not None and "(write_path)" in refusal.text


@pytest.mark.parametrize(("offered_mode", "policy_mode"), MODES)
def test_a_cancel_task_whose_id_is_not_a_uuid_is_refused_in_every_mode(offered_mode, policy_mode):
    """The per-call rule's foreign_cancel, through the runner's strict block (not the unit alone)."""
    r, _ = _runner(OFFERED, tool_offered_set_enforcement_mode=offered_mode, tool_context_policy_mode=policy_mode)
    refusal = _auth(r, _internal(), "cancel_task", OFFERED, {"task_id": "not-a-uuid"})
    assert refusal is not None and refusal.code == "internal_only" and "(foreign_cancel)" in refusal.text
    # A UUID passes the strict block; the own-lineage check is the handler's (it needs the row).
    assert _auth(r, _internal(), "cancel_task", OFFERED, {"task_id": str(OTHER)}) is None


async def test_a_cancel_task_whose_id_is_not_a_uuid_never_reaches_the_handler():
    for tool_input, dispatched in (({"task_id": str(OTHER)}, True), ({"task_id": "not-a-uuid"}, False)):
        r, d = _runner(OFFERED, tool_offered_set_enforcement_mode="off", tool_context_policy_mode="off")
        r._call_api = _tool_calls_then_done_with("cancel_task", tool_input, times=1)
        await _run_loop(r, is_background=True, context=_internal())
        assert bool(d.calls) is dispatched, tool_input


def test_an_approved_action_refusal_is_recorded_under_its_own_kind():
    r, _ = _runner(OFFERED, tool_offered_set_enforcement_mode="off", tool_context_policy_mode="off")
    events: list[tuple[str, dict]] = []
    r._log_f026_decision = lambda event_type, data, session_id=None: events.append((event_type, data))
    ctx = ExecutionContext(
        kind="approved_action", session_id="proposal-x", proposal_id=uuid.uuid4(), declared_tools=("send_email",)
    )
    refusal = _auth(r, ctx, "bash", ["send_email"], {"command": "ls"})
    assert refusal is not None and refusal.code == "internal_only"
    assert events == [
        (
            "harness_context_policy_violation",
            {
                "tool_name": "bash",
                "context_kind": "approved_action",
                "violation": "approved_action:not_offered",
                "mode": "enforce",
            },
        )
    ]


@pytest.mark.parametrize(("offered_mode", "policy_mode"), MODES)
def test_an_approved_action_runs_its_one_tool_and_nothing_else(offered_mode, policy_mode):
    r, _ = _runner(OFFERED, tool_offered_set_enforcement_mode=offered_mode, tool_context_policy_mode=policy_mode)
    ctx = ExecutionContext(
        kind="approved_action", session_id="proposal-x", proposal_id=uuid.uuid4(), declared_tools=("send_email",)
    )
    assert _auth(r, ctx, "send_email", ["send_email"], FORGED[0][1]) is None
    refusal = _auth(r, ctx, "bash", ["send_email"], {"command": "ls"})
    assert refusal is not None and refusal.code == "internal_only" and "(not_offered)" in refusal.text


@pytest.mark.parametrize(("offered_mode", "policy_mode"), MODES)
def test_an_owner_turn_is_authorized_exactly_as_before(offered_mode, policy_mode):  # PIN
    r, _ = _runner(OFFERED, tool_offered_set_enforcement_mode=offered_mode, tool_context_policy_mode=policy_mode)
    ctx = ExecutionContext(kind="subtask", session_id="s1")
    refusal = _auth(r, ctx, "send_email", ["recall_deep"], FORGED[0][1])
    if offered_mode == "enforce":
        assert refusal is not None and refusal.code == "offered_set"
    else:
        assert refusal is None
    assert _auth(r, ctx, "write_file", OFFERED, {"path": "anywhere.md", "content": "x"}) is None


@pytest.mark.parametrize(("offered_mode", "policy_mode"), MODES)
@pytest.mark.parametrize(("tool", "tool_input"), FORGED, ids=[t for t, _ in FORGED])
async def test_stream_chat_refuses_a_forged_call_in_an_internal_only_turn_whatever_the_modes_say(
    monkeypatch, tmp_path, offered_mode, policy_mode, tool, tool_input
):
    """The streaming loop's offered_names come from the shared helper and its call site
    runs the strict block. In production stream_chat's context is interactive/owner, so the
    test substitutes the context constructor in the runner module (a test seam)."""
    from test_streaming import _make_mock_cognitive, _make_mock_settings, _make_runner

    from nous.api import runner as runner_module
    from nous.api.anthropic_client import StreamEvent

    cognitive, turn_context = _make_mock_cognitive()
    turn_context.refuse_active = False
    settings = _make_mock_settings()
    settings.tool_offered_set_enforcement_mode = offered_mode
    settings.tool_context_policy_mode = policy_mode
    settings.workspace_dir = str(tmp_path)
    runner = _make_runner(cognitive, settings)
    runner._dispatcher.available_tools.return_value = [
        {"name": n, "description": n, "input_schema": {"type": "object"}} for n in OFFERED
    ]
    monkeypatch.setattr(
        runner_module, "ExecutionContext", functools.partial(ExecutionContext, authority="internal_only")
    )
    calls = {"n": 0}

    async def fake_stream(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            yield StreamEvent(type="tool_start", tool_name=tool, tool_id="t1", block_index=1)
            yield StreamEvent(type="tool_input_delta", text=json.dumps(tool_input), block_index=1)
            yield StreamEvent(type="block_stop", block_index=1)
            yield StreamEvent(type="done", stop_reason="tool_use")
        else:
            yield StreamEvent(type="text_delta", text="ok")
            yield StreamEvent(type="done", stop_reason="end_turn")

    runner._call_api_stream = MagicMock(side_effect=fake_stream)
    events = [e async for e in runner.stream_chat("s1", "run it")]

    assert not runner._dispatcher.dispatch.called
    assert any(e.type == "tool_end" and e.tool_name == tool for e in events)
    second_call_messages = runner._call_api_stream.call_args_list[1][0][1]
    results = [
        b
        for m in second_call_messages
        if isinstance(m.get("content"), list)
        for b in m["content"]
        if isinstance(b, dict) and b.get("type") == "tool_result"
    ]
    assert len(results) == 1 and results[0]["is_error"] is True
    assert "is not allowed in this turn (not_offered)" in results[0]["content"]
