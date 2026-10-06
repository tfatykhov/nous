"""F099 Phase 2a: the continuation and approved_action contexts.

Neither kind is created by any caller yet (2c and 2d build them); this pins
what each may be, and what the policy table says each may do.
"""

from __future__ import annotations

import uuid

import pytest

from nous.api.execution_context import CONTEXT_KINDS, FOREGROUND_KINDS, ExecutionContext
from nous.api.tool_policy import CONTEXT_POLICY, evaluate

IID, RID, PID = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()


def _continuation(**over) -> ExecutionContext:
    base = {
        "kind": "continuation",
        "session_id": "intent-x",
        "authority": "internal_only",
        "intention_id": IID,
        "root_intention_id": RID,
    }
    return ExecutionContext(**{**base, **over})


def _approved(**over) -> ExecutionContext:
    base = {
        "kind": "approved_action",
        "session_id": "proposal-x",
        "proposal_id": PID,
        "declared_tools": ("send_email",),
    }
    return ExecutionContext(**{**base, **over})


@pytest.mark.parametrize("kind", ["continuation", "approved_action"])
def test_the_new_kinds_are_background_kinds(kind):
    assert kind in CONTEXT_KINDS
    assert kind not in FOREGROUND_KINDS
    assert (_continuation() if kind == "continuation" else _approved()).is_background is True


def test_the_new_fields_default_so_existing_constructors_are_unchanged():
    ctx = ExecutionContext(kind="subtask")
    assert (ctx.proposal_id, ctx.arrival_id, ctx.claim_token, ctx.spawn_blocked) == (None, None, None, False)


def test_a_continuation_carries_its_arrival_and_claim():
    arrival, token = uuid.uuid4(), uuid.uuid4()
    ctx = _continuation(arrival_id=arrival, claim_token=token, spawn_blocked=True)
    assert (ctx.arrival_id, ctx.claim_token, ctx.spawn_blocked) == (arrival, token, True)


@pytest.mark.parametrize(
    "over",
    [
        {"authority": "owner"},
        {"intention_id": None},
        {"root_intention_id": None},
    ],
    ids=["owner-authority", "no-intention", "no-root"],
)
def test_a_continuation_can_never_be_built_wide_or_without_its_lineage(over):
    with pytest.raises(
        ValueError, match="a continuation context is internal_only and names its intention and its root"
    ):
        _continuation(**over)


@pytest.mark.parametrize(
    "over",
    [
        {"proposal_id": None},
        {"declared_tools": None},
        {"declared_tools": ()},
        {"declared_tools": ("send_email", "bash")},
    ],
    ids=["no-proposal", "no-declared-tool", "empty-declared-tools", "two-declared-tools"],
)
def test_an_approved_action_names_its_proposal_and_exactly_one_tool(over):
    with pytest.raises(
        ValueError, match="an approved_action context needs a proposal_id and exactly one declared tool"
    ):
        _approved(**over)


def test_the_policy_rows_are_the_contracts():
    cont = CONTEXT_POLICY["continuation"]
    assert cont.levels == frozenset({"none", "write"})
    assert cont.spawn == frozenset({"spawn_task", "dag_create"})
    appr = CONTEXT_POLICY["approved_action"]
    assert appr.levels == frozenset({"none", "write", "external", "irreversible"})
    assert appr.spawn is True


def test_a_continuation_may_spawn_only_the_two_spawn_tools_and_never_send():
    ctx = _continuation()
    assert evaluate(ctx, "spawn_task", {}) is None
    assert evaluate(ctx, "dag_create", {}) is None
    assert evaluate(ctx, "recall_deep", {}) is None
    assert evaluate(ctx, "schedule_task", {}) == "spawn"
    assert evaluate(ctx, "spawn_sync", {}) == "spawn"
    assert evaluate(ctx, "send_email", {}) == "level:external"
    assert evaluate(ctx, "bash", {"command": "curl https://example.com"}) == "level:external"


def test_an_approved_action_runs_exactly_its_declared_tool():
    ctx = _approved()
    assert evaluate(ctx, "send_email", {}) is None
    assert evaluate(ctx, "bash", {"command": "ls"}) == "undeclared"
    assert evaluate(ctx, "recall_deep", {}) == "undeclared"
    # Wide levels: the proposed call is outward or a denylisted local tool by definition.
    assert evaluate(_approved(declared_tools=("bash",)), "bash", {"command": "curl https://example.com"}) is None
