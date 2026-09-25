"""Harness Phase 3 §3.5: approval.choose / approval.defer on a DAG card.

Handler-level (no database): the router's gate is unchanged; what changes is
that a card carrying the dag-approval: key answers its node and never takes
the generic path.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

from nous.a2ui.actions import ActionContext, ActionRouter
from nous.config import Settings
from nous.dag.approval import AnswerResult, approval_dedup_key

RISK = "If nobody answers by 2026-09-26 12:00 UTC, 'Hold' applies."


def _router(orchestrator):
    return ActionRouter(None, Settings(_env_file=None), None, dag_orchestrator=orchestrator)


def _ctx(router, *, name="approval.choose", option="send", dag_card=True, context=None, data=None):
    surface = SimpleNamespace(
        surface_id="card-1",
        dedup_key=approval_dedup_key(uuid.UUID(int=7)) if dag_card else None,
        data_model=data or {
            "options": [{"id": "send", "label": "Send"}, {"id": "hold", "label": "Hold"}],
            "risk": RISK,
            "summary": "Send it?",
        },
    )
    return ActionContext(
        surface=surface, name=name, context=context if context is not None else {"optionId": option},
        data_model=None, services=router, actor="alice@example.com",
    )


async def _run(router, ctx):
    return await router._handlers[ctx.name].fn(ctx)


async def test_a_recorded_answer_retires_the_card():
    orch = SimpleNamespace(answer_node=AsyncMock(return_value=AnswerResult(outcome="recorded")))
    router = _router(orch)

    result = await _run(router, _ctx(router))

    assert result.ok and result.resolve_surface and result.data_patches == []
    orch.answer_node.assert_awaited_once_with(
        uuid.UUID(int=7), "send", source="companion", actor="alice@example.com", surface_id="card-1"
    )


async def test_a_refused_answer_leaves_the_card_up_saying_why():
    closed = AnswerResult(outcome="closed", option_label="Send", answer_source="companion")
    router = _router(SimpleNamespace(answer_node=AsyncMock(return_value=closed)))

    result = await _run(router, _ctx(router, option="hold"))

    assert not result.ok and not result.resolve_surface
    assert result.message.startswith("already answered 'Send'")


async def test_an_unwired_orchestrator_keeps_the_card():
    router = _router(None)
    result = await _run(router, _ctx(router))
    assert not result.ok and not result.resolve_surface
    assert "not running" in result.message


async def test_an_agent_pushed_card_behaves_as_before():
    orch = SimpleNamespace(answer_node=AsyncMock())
    router = _router(orch)

    result = await _run(router, _ctx(router, dag_card=False))

    assert result.ok and result.resolve_surface
    assert result.data_patches == [("/summary", "Decided: send.")]
    orch.answer_node.assert_not_awaited()


async def test_defer_on_a_dag_card_keeps_the_draft_and_restates_the_default():
    router = _router(SimpleNamespace())
    result = await _run(router, _ctx(router, name="approval.defer", context={}))
    assert result.data_patches == [("/risk", f"Deferred. {RISK}")]


async def test_companion_retry_may_re_ask_a_declined_question():
    orch = SimpleNamespace(retry_node=AsyncMock())
    router = _router(orch)
    dag_id = uuid.uuid4()
    ctx = _ctx(
        router, name="dag.retry", dag_card=False, context={"node": "approve"},
        data={"dag_id": str(dag_id), "nodes": [{"name": "approve"}]},
    )

    result = await _run(router, ctx)

    assert result.ok
    orch.retry_node.assert_awaited_once_with(dag_id, "approve", allow_declined=True)
