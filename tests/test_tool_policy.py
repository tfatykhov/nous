"""Harness Phase 2a: one table decides what each execution context may do."""

import pytest

from nous.api.execution_context import CONTEXT_KINDS, ExecutionContext
from nous.api.tool_policy import CONTEXT_POLICY, evaluate


def _ctx(kind, **kw):
    return ExecutionContext(kind=kind, **kw)


def test_every_context_kind_has_a_policy():
    assert set(CONTEXT_POLICY) == set(CONTEXT_KINDS)


@pytest.mark.parametrize("kind", ["interactive", "mcp"])
def test_foreground_may_do_anything_classified(kind):
    assert evaluate(_ctx(kind), "dag_create", {}) is None
    assert evaluate(_ctx(kind), "send_email", {}) is None
    assert evaluate(_ctx(kind), "brand_new_tool", {}) is None


@pytest.mark.parametrize("kind", ["subtask", "dag_node", "scheduled", "agent_action"])
def test_background_work_may_send_but_not_spawn(kind):
    assert evaluate(_ctx(kind), "spawn_task", {}) == "spawn"
    assert evaluate(_ctx(kind), "send_email", {}) is None


def test_dag_summary_may_spawn_only_the_delivery_subtask():
    assert evaluate(_ctx("dag_summary"), "spawn_task", {}) is None
    assert evaluate(_ctx("dag_summary"), "schedule_task", {}) == "spawn"
    assert evaluate(_ctx("dag_summary"), "dag_create", {}) == "spawn"


def test_triage_may_not_send_by_any_route():
    ctx = _ctx("heartbeat_triage")
    assert evaluate(ctx, "send_email", {}) == "level:external"
    assert evaluate(ctx, "bash", {"command": "curl https://x"}) == "level:external"
    assert evaluate(ctx, "run_python", {"code": "import smtplib"}) == "level:external"
    assert evaluate(ctx, "bash", {"command": "ls"}) is None


def test_a_check_may_use_only_what_it_declared():
    ctx = _ctx("heartbeat_check", declared_tools=("web_search",))
    assert evaluate(ctx, "web_search", {}) is None
    assert evaluate(ctx, "bash", {"command": "ls"}) == "undeclared"


def test_an_undeclared_check_falls_back_to_the_level_rules():
    ctx = _ctx("heartbeat_check", declared_tools=None)
    assert evaluate(ctx, "recall_deep", {}) is None
    assert evaluate(ctx, "spawn_task", {}) == "spawn"


def test_a_declared_spawn_tool_is_the_check_pipeline():
    ctx = _ctx("heartbeat_check", declared_tools=("heartbeat_check_create",))
    assert evaluate(ctx, "heartbeat_check_create", {"name": "step-2"}) is None


def test_a_callback_may_not_re_enable_its_own_check():
    ctx = _ctx("heartbeat_callback", declared_tools=("heartbeat_check_manage",), check_name="watch-ci")
    assert evaluate(ctx, "heartbeat_check_manage", {"action": "enable", "name": "watch-ci"}) == "reenable"
    assert evaluate(ctx, "heartbeat_check_manage", {"action": "enable", "name": "other"}) is None
    assert evaluate(ctx, "heartbeat_check_manage", {"action": "disable", "name": "watch-ci"}) is None


def test_unclassified_tools_are_denied_in_background():
    assert evaluate(_ctx("subtask"), "brand_new_tool", {}) == "unclassified"


def test_submit_final_report_is_allowed_in_hardened_subtasks():
    assert evaluate(_ctx("dag_node"), "submit_final_report", {}) is None


def test_generic_background_is_local_only():
    assert evaluate(_ctx("background"), "send_file", {}) == "level:external"
    assert evaluate(_ctx("background"), "write_file", {}) is None
