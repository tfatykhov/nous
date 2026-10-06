"""F099 section 4.4: one helper builds the offered set for both loops.

An internal_only turn is offered no external tool, no denylisted tool and, unless
it is a continuation with room to spawn, no spawn tool. An owner turn is offered
exactly what it was before F099.
"""

from __future__ import annotations

import itertools
import json
import uuid
from unittest.mock import MagicMock

import pytest
from test_f099_tool_policy import LINEAGE_ALLOWED
from test_runner_authorization import _run_loop, _runner

from nous.api.execution_context import CONTEXT_KINDS, ExecutionContext
from nous.api.models import ApiResponse
from nous.api.tool_classes import TOOL_CLASSES, refuse_denylist
from nous.api.tool_policy import INTERNAL_ONLY_SPAWN_TOOLS
from nous.heartbeat.dynamic import ALLOWED_TOOLS as CHECK_TOOLS

IID, RID = (uuid.UUID(f"00000000-0000-0000-0000-00000000000{n}") for n in (1, 2))
OWNER_KINDS = [k for k in CONTEXT_KINDS if k not in ("continuation", "approved_action")]
ALL_TOOLS = list(TOOL_CLASSES)
SUBMIT = {"name": "submit_final_report", "description": "d", "input_schema": {"type": "object"}}
MODES = ("off", "warn", "enforce")


async def _noop(**_):
    return "ok", False


def _internal(kind: str = "subtask", **over) -> ExecutionContext:
    base = {
        "kind": kind,
        "session_id": "s1",
        "authority": "internal_only",
        "intention_id": IID,
        "root_intention_id": RID,
    }
    return ExecutionContext(**{**base, **over})


def _names(tools) -> set[str]:
    return {t["name"] for t in tools}


def _legacy_offered(dispatcher, frame_id, *, is_subtask, tool_filter, refuse_active, extra_tools):
    """The offered set as _tool_loop built it before F099, kept here as the reference."""
    tools = dispatcher.available_tools(frame_id)
    if is_subtask:
        tools = [t for t in tools if t["name"] not in {"spawn_task", "schedule_task", "spawn_sync"}]
    if tool_filter is not None:
        tools = [t for t in tools if t["name"] in tool_filter]
    if refuse_active:
        denylist = refuse_denylist()
        tools = [t for t in tools if t["name"] not in denylist]
    out = list(tools)
    if extra_tools:
        for _name, (schema, _executor) in extra_tools.items():
            out.append(schema)
    return out


def test_owner_contexts_are_offered_exactly_what_they_were_before_f099():  # PIN (also fails on the base: no helper)
    r, d = _runner(ALL_TOOLS)
    checked = 0
    for kind in OWNER_KINDS:
        ctx = ExecutionContext(kind=kind, session_id="s1")
        for is_subtask, tool_filter, refuse_active, extra in itertools.product(
            (False, True),
            (None, ["web_search", "bash", "recall_deep", "heartbeat_check_create"]),
            (False, True),
            (None, {"submit_final_report": (SUBMIT, _noop)}),
        ):
            kwargs = {
                "is_subtask": is_subtask,
                "tool_filter": tool_filter,
                "refuse_active": refuse_active,
                "extra_tools": extra,
            }
            got = r._offered_tools(ctx, "conversation", **kwargs)
            assert json.dumps(got) == json.dumps(_legacy_offered(d, "conversation", **kwargs)), (kind, kwargs)
            checked += 1
    assert checked == len(OWNER_KINDS) * 16


@pytest.mark.parametrize("kind", OWNER_KINDS)
@pytest.mark.parametrize("is_subtask", [False, True])
def test_a_lineage_turn_that_is_not_a_continuation_is_offered_no_spawn_external_or_denylisted_tool(kind, is_subtask):
    r, _ = _runner(ALL_TOOLS)
    tools = r._offered_tools(
        _internal(kind), "conversation", is_subtask=is_subtask, tool_filter=None, refuse_active=False
    )
    assert _names(tools) == LINEAGE_ALLOWED


def test_a_continuation_is_offered_the_spawn_tools_until_its_root_is_at_a_limit():
    r, _ = _runner(ALL_TOOLS)

    def offered(**over):
        return _names(
            r._offered_tools(
                _internal("continuation", **over),
                "conversation",
                is_subtask=False,
                tool_filter=None,
                refuse_active=False,
            )
        )

    assert offered() == LINEAGE_ALLOWED | INTERNAL_ONLY_SPAWN_TOOLS
    assert offered(spawn_blocked=True) == LINEAGE_ALLOWED


def test_the_narrowing_composes_with_refuse_and_the_subtask_exclusion():
    r, _ = _runner(ALL_TOOLS)
    tools = r._offered_tools(
        _internal("continuation"), "conversation", is_subtask=False, tool_filter=None, refuse_active=True
    )
    assert _names(tools) <= LINEAGE_ALLOWED and not _names(tools) & refuse_denylist()
    # A continuation run as a subtask would lose spawn_task to the 012.2 rule: is_subtask must stay False for it.
    tools = r._offered_tools(
        _internal("continuation"), "conversation", is_subtask=True, tool_filter=None, refuse_active=False
    )
    assert "spawn_task" not in _names(tools)


def test_a_lineage_check_loses_the_tools_it_declared_that_the_narrowing_denies():
    """Phase 1 carry-over: a stamped check cannot be offered heartbeat_check_create (or bash)."""
    r, _ = _runner(ALL_TOOLS)
    declared = sorted(CHECK_TOOLS)
    ctx = _internal("heartbeat_check", declared_tools=tuple(declared))
    tools = r._offered_tools(ctx, "conversation", is_subtask=True, tool_filter=declared, refuse_active=False)
    assert _names(tools) == {"web_search", "web_fetch", "recall_deep", "recall_recent", "read_file"}
    assert not _names(tools) & {"bash", "heartbeat_check_create", "heartbeat_check_manage"}
    # PIN: the same check with no lineage is offered everything it declared.
    owner = ExecutionContext(kind="heartbeat_check", session_id="s1", declared_tools=tuple(declared))
    tools = r._offered_tools(owner, "conversation", is_subtask=True, tool_filter=declared, refuse_active=False)
    assert _names(tools) == set(CHECK_TOOLS)


def test_extra_tools_follow_the_narrowing_and_are_not_filtered():
    r, _ = _runner(ALL_TOOLS)
    schema = {"name": "resolve_intention", "description": "d", "input_schema": {"type": "object"}}
    tools = r._offered_tools(
        _internal("continuation"),
        "conversation",
        is_subtask=False,
        tool_filter=None,
        refuse_active=False,
        extra_tools={"resolve_intention": (schema, _noop)},
    )
    assert tools[-1] == schema
    assert _names(tools) == LINEAGE_ALLOWED | INTERNAL_ONLY_SPAWN_TOOLS | {"resolve_intention"}


def _capturing_api(seen: list[set[str]]):
    async def fake_call_api(
        system_prompt, messages, tools=None, skip_thinking=False, model_override=None, is_background=False, **_
    ):
        seen.append({t["name"] for t in (tools or [])})
        return ApiResponse(content=[{"type": "text", "text": "done"}], stop_reason="end_turn")

    return fake_call_api


@pytest.mark.parametrize("offered_mode", MODES)
@pytest.mark.parametrize("policy_mode", MODES)
@pytest.mark.parametrize(
    ("ctx", "is_subtask", "expected"),
    [
        (_internal("continuation"), False, LINEAGE_ALLOWED | INTERNAL_ONLY_SPAWN_TOOLS),
        (_internal("subtask"), True, LINEAGE_ALLOWED),
        (_internal("dag_node"), True, LINEAGE_ALLOWED),
    ],
    ids=["continuation", "subtask", "dag_node"],
)
async def test_the_model_is_sent_no_external_or_denylisted_tool_under_any_mode(
    offered_mode, policy_mode, ctx, is_subtask, expected
):
    r, _ = _runner(ALL_TOOLS, tool_offered_set_enforcement_mode=offered_mode, tool_context_policy_mode=policy_mode)
    seen: list[set[str]] = []
    r._call_api = _capturing_api(seen)
    await _run_loop(r, is_background=True, is_subtask=is_subtask, context=ctx)
    assert seen == [expected]


async def test_stream_chat_offers_an_internal_only_turn_the_narrowed_set(monkeypatch, tmp_path):
    """stream_chat shares the helper, so it shares the narrowing.

    In production stream_chat's context is always interactive and owner, so no
    internal_only context reaches it today. The test substitutes the context
    constructor in the runner module (a test seam) to run the real streaming
    loop with one.
    """
    import functools

    from test_streaming import _make_mock_cognitive, _make_mock_settings, _make_runner

    from nous.api import runner as runner_module
    from nous.api.anthropic_client import StreamEvent

    cognitive, turn_context = _make_mock_cognitive()
    turn_context.refuse_active = False
    settings = _make_mock_settings()
    settings.workspace_dir = str(tmp_path)
    runner = _make_runner(cognitive, settings)
    runner._dispatcher.available_tools.return_value = [
        {"name": n, "description": n, "input_schema": {"type": "object"}}
        for n in ("recall_deep", "send_email", "bash", "write_file", "web_fetch", "spawn_task")
    ]
    monkeypatch.setattr(
        runner_module, "ExecutionContext", functools.partial(ExecutionContext, authority="internal_only")
    )
    offered: list[set[str]] = []

    async def fake_stream(*args, **kwargs):
        tools = kwargs.get("tools") or next(
            (a for a in args if isinstance(a, list) and a and isinstance(a[0], dict) and "name" in a[0]), []
        )
        offered.append({t["name"] for t in tools})
        yield StreamEvent(type="text_delta", text="ok")
        yield StreamEvent(type="done", stop_reason="end_turn")

    runner._call_api_stream = MagicMock(side_effect=fake_stream)
    [e async for e in runner.stream_chat("s1", "hi")]
    assert offered == [{"recall_deep", "write_file", "web_fetch"}]
