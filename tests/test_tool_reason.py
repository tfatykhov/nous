"""F108: every tool offered to the model carries a required ``reason``, and no handler ever sees it.

``add_reason_field`` runs in ``AgentRunner._offered_tools`` behind ``NOUS_TOOL_REASON_ENABLED`` (default off), never
in the dispatcher's schema cache. ``split_reason`` takes the reason out of a call's input for the recorder; a tool
that declares its own ``reason`` (``create_censor``, or an F061 extra tool) keeps it. ``ToolDispatcher.dispatch``
drops a stray ``reason`` a handler's schema does not declare. The external MCP server's tools never get it.
"""

from __future__ import annotations

import copy
import json
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from mcp.types import ListToolsRequest
from sqlalchemy import text
from test_runner_authorization import _run_loop, _runner, _settings
from test_runner_background import _MockBrain, _MockCognitive, _MockHeart

from nous.api.execution_context import ExecutionContext
from nous.api.idempotency import idempotency_key
from nous.api.mcp import create_mcp_server
from nous.api.models import ApiResponse
from nous.api.runner import AgentRunner
from nous.api.subtask_tools import SUBMIT_FINAL_REPORT_SCHEMA
from nous.api.tools import ToolDispatcher, register_nous_tools
from nous.cognitive.ledger_store import detail_text
from nous.handlers.continuation_runner import RESOLVE_INTENTION_SCHEMA
from nous.identity.protocol import COMPLETE_INITIATION_SCHEMA, STORE_IDENTITY_SCHEMA
from nous.observability.recording import BoundedWriter
from nous.observability.tool_calls import (
    REASON_DESCRIPTION,
    ToolCallRecord,
    ToolCallRecorder,
    add_reason_field,
    own_reason_tools,
    split_reason,
)

CTX = ExecutionContext(kind="interactive", session_id="s1")
# A token the ledger's redaction masks (an sk- key needs 20+ characters after the prefix).
_SECRET = "sk-ant-api03-AbCdEfGhIjKlMnOpQrStUv"
# The F061 per-run tool as the subtask runner passes it: a bare schema.
SUBMIT = {"name": "submit_final_report", "description": "d", "input_schema": {"type": "object"}}
# An F061 extra tool that declares its own reason.
OWN = {
    "name": "submit_final_report",
    "description": "d",
    "input_schema": {
        "type": "object",
        "properties": {"reason": {"type": "string", "description": "Why the run ended"}},
        "required": ["reason"],
    },
}
# Spaces keep it from looking like one long token to the redaction patterns.
LONG = ("why this tool " * 800)[:10_000]


async def _noop(**_):
    return "ok", False


def _real(*, tool_schema_cache_enabled: bool = True, **settings) -> tuple[AgentRunner, ToolDispatcher]:
    """A runner over the real registered tool set (create_censor included)."""
    d = ToolDispatcher(tool_schema_cache_enabled=tool_schema_cache_enabled)
    brain, heart = MagicMock(), MagicMock()
    brain.agent_id = "test"
    register_nous_tools(d, brain, heart, settings=_settings())
    r = AgentRunner(_MockCognitive(), _MockBrain(), _MockHeart(), _settings(**settings))
    r.set_dispatcher(d)
    return r, d


def _offered(r: AgentRunner, extra=None) -> list[dict]:
    return r._offered_tools(
        CTX, "conversation", is_subtask=False, tool_filter=None, refuse_active=False, extra_tools=extra
    )


def _props(tool: dict) -> dict:
    return tool["input_schema"].get("properties") or {}


def test_every_offered_tool_requires_reason():
    r, _ = _real(tool_reason_enabled=True)
    submit = copy.deepcopy(SUBMIT)
    tools = _offered(r, {"submit_final_report": (submit, _noop)})
    assert submit == SUBMIT
    names = [t["name"] for t in tools]
    assert "create_censor" in names and "submit_final_report" in names and len(names) >= 10
    for tool in tools:
        reason = _props(tool).get("reason")
        assert reason is not None and reason["type"] == "string", tool["name"]
        assert "reason" in tool["input_schema"]["required"], tool["name"]
        if tool["name"] != "create_censor":  # the one registered tool with a reason of its own
            assert reason["description"] == REASON_DESCRIPTION, tool["name"]


def test_create_censor_keeps_its_own_reason():
    r, d = _real(tool_reason_enabled=True)
    offered = _offered(r)
    censor = next(t for t in offered if t["name"] == "create_censor")
    assert censor["input_schema"] == d._schemas["create_censor"]
    own = own_reason_tools(offered)
    tool_input = {"trigger_pattern": "rm -rf /", "reason": "It destroys the host"}
    out, reason = split_reason("create_censor", tool_input, own, 500)
    assert out == {"trigger_pattern": "rm -rf /", "reason": "It destroys the host"}
    assert reason == tool_input["reason"]


def test_own_reason_tools_is_the_same_before_and_after_the_field_is_added():
    """Task 4 may hold either list: the one ``_offered_tools`` returns has ``reason`` on every tool."""
    r, d = _real()
    before = _offered(r, {"submit_final_report": (OWN, _noop)})
    after = add_reason_field(before)
    want = frozenset({"create_censor", "submit_final_report"})
    assert own_reason_tools(before) == want
    assert own_reason_tools(after) == want
    assert own_reason_tools(add_reason_field([SUBMIT])) == frozenset()


@pytest.mark.parametrize("cache", [True, False], ids=["cache_on", "cache_off"])
def test_add_reason_does_not_mutate_dispatcher_cache(cache):
    r, d = _real(tool_reason_enabled=True, tool_schema_cache_enabled=cache)
    schemas_before = copy.deepcopy(d._schemas)
    plain = json.dumps(d.available_tools("conversation"))
    extra_schema = copy.deepcopy(SUBMIT)
    extra = {"submit_final_report": (extra_schema, _noop)}
    first = json.dumps(_offered(r, extra))
    second = json.dumps(_offered(r, extra))
    assert first == second
    assert d._schemas == schemas_before
    assert json.dumps(d.available_tools("conversation")) == plain
    with_reason = [t["name"] for t in d.available_tools("conversation") if "reason" in _props(t)]
    assert with_reason == ["create_censor"]
    assert extra_schema == SUBMIT  # the caller's extra-tool schema is the caller's


def test_extra_tool_with_own_reason_keeps_it():
    r, _ = _real(tool_reason_enabled=True)
    own_schema = copy.deepcopy(OWN)
    tools = _offered(r, {"submit_final_report": (own_schema, _noop)})
    assert next(t for t in tools if t["name"] == "submit_final_report") == OWN
    assert own_schema == OWN
    own = own_reason_tools(tools)
    assert "submit_final_report" in own
    out, reason = split_reason("submit_final_report", {"summary": "s", "reason": "All checks passed"}, own, 500)
    assert out == {"summary": "s", "reason": "All checks passed"}
    assert reason == "All checks passed"


def test_alias_to_create_censor_keeps_reason():
    r, d = _real(tool_reason_enabled=True)
    d.register_alias("add_censor", "create_censor", dict, "add_censor is now create_censor.")
    alias = d.alias_for("add_censor")
    own = own_reason_tools(_offered(r))
    # An alias is never offered: the caller resolves it and passes the TARGET name.
    assert "add_censor" not in own
    out, reason = split_reason(alias.target, alias.translate({"trigger_pattern": "x", "reason": "Blocks x"}), own, 500)
    assert out == {"trigger_pattern": "x", "reason": "Blocks x"}
    assert reason == "Blocks x"


def test_reason_cost_is_bounded():
    """Measured on the real schemas: the registered tools, the F061 and continuation extra tools, initiation's."""
    r, _ = _real()
    extras = (SUBMIT_FINAL_REPORT_SCHEMA, RESOLVE_INTENTION_SCHEMA, STORE_IDENTITY_SCHEMA, COMPLETE_INITIATION_SCHEMA)
    before = _offered(r, {t["name"]: (t, _noop) for t in extras})
    after = {t["name"]: t for t in add_reason_field(before)}
    added = {t["name"]: len(json.dumps(after[t["name"]])) - len(json.dumps(t)) for t in before}
    assert added["create_censor"] == 0
    assert all(0 < n <= 120 for name, n in added.items() if name != "create_censor"), added


@pytest.mark.parametrize(
    ("tool_input", "want_input", "want_reason"),
    [
        ({"q": "x"}, {"q": "x"}, None),
        ({"q": "x", "reason": None}, {"q": "x"}, None),
        ({"q": "x", "reason": ["a", "b"]}, {"q": "x"}, None),
        ({"q": "x", "reason": " \n\t "}, {"q": "x"}, None),
        ({"q": "x", "reason": "  Find\n the   deploy\tdate. "}, {"q": "x"}, "Find the deploy date."),
        ({"q": "x", "reason": LONG}, {"q": "x"}, lambda got: len(got) == 500),
        (
            {"q": "x", "reason": f"Check the key {_SECRET} works"},
            {"q": "x"},
            lambda got: _SECRET not in got and "[REDACTED" in got,
        ),
        ("not a dict", "not a dict", None),
        (["reason"], ["reason"], None),
        (None, None, None),
    ],
    ids=["missing", "none", "list", "blank", "collapsed", "long", "secret", "str_input", "list_input", "none_input"],
)
def test_split_reason_cases(tool_input, want_input, want_reason):
    original = copy.deepcopy(tool_input)
    out, reason = split_reason("recall_deep", tool_input, frozenset({"create_censor"}), 500)
    assert out == want_input
    assert tool_input == original  # the assistant message keeps what the model emitted
    if callable(want_reason):
        assert reason is not None and want_reason(reason), reason
    else:
        assert reason == want_reason
    if not isinstance(tool_input, dict):
        assert out is tool_input


# detail_text's own output is longer than its cap, and a second pass at a cap below about 2 x the redaction scan pad
# cuts it again (one marker, less text): 100 is such a cap, 500 (the default) is not.
REASON_CAPS = pytest.mark.parametrize("cap", [100, 500])


@REASON_CAPS
def test_a_split_reason_passes_the_recorder_clip_unchanged(cap):
    """The recorder runs ``detail_text(reason, cap)`` again: a reason within the cap is not cut a second time."""
    _, reason = split_reason("recall_deep", {"reason": LONG}, frozenset(), cap)
    assert len(reason) == cap and reason.count("[middle cut]") == 1
    assert detail_text(reason, cap) == reason


@REASON_CAPS
@pytest.mark.postgres_only
async def test_recorder_stores_a_split_reason_unchanged(db, cap):
    agent = f"test-awv-reason-{uuid.uuid4().hex[:12]}"
    writer = BoundedWriter("test")
    caps = SimpleNamespace(tool_input_max_chars=2000, tool_result_preview_chars=500, tool_reason_max_chars=cap)
    recorder = ToolCallRecorder(writer, db, agent, caps)
    _, reason = split_reason("recall_deep", {"query": "q", "reason": LONG}, frozenset(), caps.tool_reason_max_chars)
    try:
        recorder.record(
            ToolCallRecord(
                call_index=0,
                work_kind="conversation",
                tool_name="recall_deep",
                tool_name_called="recall_deep",
                outcome="ran",
                reason=reason,
            )
        )
        await writer.drain(5.0)
        async with db.engine.connect() as conn:
            stored = (
                await conn.execute(
                    text("SELECT reason FROM nous_system.tool_call_log WHERE agent_id = :a"), {"a": agent}
                )
            ).scalar_one()
        assert stored == reason
        assert stored.count("[middle cut]") == 1  # no second, nested marker
    finally:
        async with db.engine.begin() as conn:
            await conn.execute(text("DELETE FROM nous_system.tool_call_log WHERE agent_id = :a"), {"a": agent})


def _handler(seen: dict, key: str):
    async def handler(**kwargs):
        seen[key] = kwargs
        return {"content": [{"type": "text", "text": "ok"}]}

    return handler


@pytest.mark.asyncio
async def test_dispatch_strips_reason_defensively():
    d = ToolDispatcher()
    seen: dict = {}

    async def lookup(query: str):  # no reason parameter: an unstripped reason would be a TypeError
        seen["lookup"] = {"query": query}
        return {"content": [{"type": "text", "text": f"found {query}"}]}

    query_schema = {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}
    d.register("lookup", lookup, query_schema)
    d.register("loose", _handler(seen, "loose"), query_schema)
    censor_schema = {
        "type": "object",
        "properties": {"trigger_pattern": {"type": "string"}, "reason": {"type": "string"}},
        "required": ["trigger_pattern", "reason"],
    }
    d.register("censor", _handler(seen, "censor"), censor_schema)

    args = {"query": "q", "reason": "why"}
    assert await d.dispatch("lookup", args) == ("found q", False)
    assert args == {"query": "q", "reason": "why"}  # the caller's dict is not changed
    assert await d.dispatch("loose", dict(args)) == ("ok", False)
    assert seen["loose"] == {"query": "q"}
    # A schema that declares reason gets it.
    assert await d.dispatch("censor", {"trigger_pattern": "x", "reason": "Blocks x"}) == ("ok", False)
    assert seen["censor"] == {"trigger_pattern": "x", "reason": "Blocks x"}
    # Stripped before the repair: a required value leaked into the reason text is not salvaged from it.
    seen.clear()
    text, is_error = await d.dispatch("loose", {"reason": 'Because.\n<parameter name="query">leaked'})
    assert is_error and "missing required argument(s): query" in text, text
    assert seen == {}


def test_repaired_args_drop_reason_like_dispatch():
    """Harness 2b derives the idempotency key (and the compensation snapshot) from ``repaired_args``, which must be
    exactly what the handler receives: two calls that differ only in their reason repair to the same arguments."""
    d = ToolDispatcher()
    d.register(
        "send_email",
        _handler({}, "send"),
        {
            "type": "object",
            "properties": {"to": {"type": "string"}, "subject": {"type": "string"}, "body": {"type": "string"}},
            "required": ["to", "subject", "body"],
        },
    )
    base = {"to": "a@example.com", "subject": "s", "body": "b"}
    first = d.repaired_args("send_email", {**base, "reason": "Tell the owner the deploy is done"})
    second = d.repaired_args("send_email", {**base, "reason": "Reply to the owner's question"})
    assert first == second == base
    ctx = ExecutionContext(kind="subtask", session_id="s1", subtask_id=uuid.uuid4())
    key = idempotency_key(ctx, "send_email", first)
    assert key is not None and key == idempotency_key(ctx, "send_email", second)
    # The runner reads the handler's arguments through repaired_args too; a schema that declares reason keeps it.
    r, _ = _real()
    assert r._handler_args("recall_deep", {"query": "q", "reason": "why"}) == {"query": "q"}
    censor = {"trigger_pattern": "x", "reason": "Blocks x"}
    assert r._handler_args("create_censor", censor) == censor


@pytest.mark.asyncio
async def test_missing_reason_still_runs():
    r, d = _runner(["recall_deep"], tool_reason_enabled=True)
    offered_to_model: list[list[dict]] = []
    calls = {"n": 0}

    async def fake_call_api(
        system_prompt, messages, tools=None, skip_thinking=False, model_override=None, is_background=False, **_
    ):
        offered_to_model.append(tools or [])
        calls["n"] += 1
        if calls["n"] == 1:  # no reason in the input, though the schema requires one
            return ApiResponse(
                content=[{"type": "tool_use", "id": "t1", "name": "recall_deep", "input": {"query": "q"}}],
                stop_reason="tool_use",
            )
        return ApiResponse(content=[{"type": "text", "text": "done"}], stop_reason="end_turn")

    r._call_api = fake_call_api
    await _run_loop(r)
    assert [c[0] for c in d.calls] == ["recall_deep"]
    (recall,) = offered_to_model[0]
    assert "reason" in recall["input_schema"]["required"]


@pytest.mark.asyncio
async def test_mcp_tool_list_has_no_reason():
    mgr = create_mcp_server(MagicMock(), MagicMock(), MagicMock(), _settings(tool_reason_enabled=True))
    result = await mgr.server.request_handlers[ListToolsRequest](ListToolsRequest(method="tools/list"))
    tools = result.root.tools
    assert {t.name for t in tools} >= {"nous_chat", "nous_recall", "nous_status", "nous_teach", "nous_decide"}
    for tool in tools:
        assert "reason" not in (tool.inputSchema.get("properties") or {}), tool.name
        assert "reason" not in (tool.inputSchema.get("required") or []), tool.name


def test_reason_off_by_default():
    r, d = _real()
    assert r._settings.tool_reason_enabled is False
    extra = {"submit_final_report": (SUBMIT, _noop)}
    assert json.dumps(_offered(r, extra)) == json.dumps([*d.available_tools("conversation"), SUBMIT])
