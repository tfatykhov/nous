"""F066.1 Phase 1.5: LLM-based fix-node dispatch.

Covers:
- choose_action_llm: happy path, action constraint enforcement,
  retry_with_amended_prompt requires amended_prompt, timeout falls
  back, no tool_use block falls back, unknown action falls back.
- orchestrator integration: flag-on + LLM success applies LLM action;
  flag-on + LLM failure falls back to rule-based; flag-off uses rule-based.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

from nous.config import Settings
from nous.dag.fix_executor import (
    FixActionResult,
    choose_action,
    choose_action_llm,
)
from nous.dag.orchestrator import DAGOrchestrator
from nous.dag.schemas import (
    DAGCreateRequest,
    DAGEdgeSpec,
    DAGNodeSpec,
    DAGNodeType,
)
from nous.dag.store import DAGStore


def _llm_response(action: str, *, amended_prompt: str | None = None, rationale: str = "rationale"):
    """Construct a mock ApiResponse with a tool_use block."""
    tool_input: dict = {"action": action, "rationale": rationale}
    if amended_prompt is not None:
        tool_input["amended_prompt"] = amended_prompt
    return SimpleNamespace(
        content=[
            {
                "type": "tool_use",
                "name": "choose_fix_action",
                "input": tool_input,
                "id": "toolu_test",
            }
        ],
        stop_reason="tool_use",
        usage={"input_tokens": 100, "output_tokens": 50},
    )


def _llm_client_returning(payload_response):
    client = MagicMock()
    client.call = AsyncMock(return_value=payload_response)
    return client


# ---------------------------------------------------------------------------
# choose_action_llm direct tests
# ---------------------------------------------------------------------------


class TestChooseActionLLM:
    @pytest.mark.asyncio
    async def test_happy_path_skip_and_continue(self):
        client = _llm_client_returning(
            _llm_response("skip_and_continue", rationale="test failed and is non-critical")
        )
        result = await choose_action_llm(
            parent_name="fact-check",
            parent_instructions="verify the claims",
            parent_error="unverifiable_claim",
            parent_result=None,
            fix_instructions="diagnose and recover",
            fix_actions=["retry_as_is", "skip_and_continue"],
            llm_client=client,
            model="claude-haiku-4-5-20251001",
            timeout_seconds=5.0,
        )
        assert result.action == "skip_and_continue"
        assert "LLM dispatch" in (result.rationale or "")
        client.call.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_action_outside_fix_actions_raises(self):
        client = _llm_client_returning(_llm_response("mark_unrecoverable"))
        with pytest.raises(ValueError, match="not in fix_actions"):
            await choose_action_llm(
                parent_name="p",
                parent_instructions=None,
                parent_error=None,
                parent_result=None,
                fix_instructions=None,
                fix_actions=["retry_as_is"],  # mark_unrecoverable NOT allowed
                llm_client=client,
                model="x",
                timeout_seconds=5.0,
            )

    @pytest.mark.asyncio
    async def test_retry_with_amended_prompt_requires_amended_prompt(self):
        client = _llm_client_returning(
            _llm_response("retry_with_amended_prompt", amended_prompt=None)
        )
        with pytest.raises(ValueError, match="amended_prompt"):
            await choose_action_llm(
                parent_name="p",
                parent_instructions=None,
                parent_error=None,
                parent_result=None,
                fix_instructions=None,
                fix_actions=["retry_with_amended_prompt"],
                llm_client=client,
                model="x",
                timeout_seconds=5.0,
            )

    @pytest.mark.asyncio
    async def test_retry_with_amended_prompt_happy_path(self):
        client = _llm_client_returning(
            _llm_response(
                "retry_with_amended_prompt",
                amended_prompt="Try again but only check claim #1 and #3",
                rationale="claim #2 was unverifiable",
            )
        )
        result = await choose_action_llm(
            parent_name="p",
            parent_instructions="verify all claims",
            parent_error="unverifiable_claim",
            parent_result=None,
            fix_instructions=None,
            fix_actions=["retry_with_amended_prompt", "skip_and_continue"],
            llm_client=client,
            model="x",
            timeout_seconds=5.0,
        )
        assert result.action == "retry_with_amended_prompt"
        assert result.amended_prompt == "Try again but only check claim #1 and #3"

    @pytest.mark.asyncio
    async def test_no_tool_use_block_raises(self):
        empty_resp = SimpleNamespace(content=[], stop_reason="end_turn", usage={})
        client = _llm_client_returning(empty_resp)
        with pytest.raises(ValueError, match="no choose_fix_action"):
            await choose_action_llm(
                parent_name="p",
                parent_instructions=None,
                parent_error=None,
                parent_result=None,
                fix_instructions=None,
                fix_actions=["retry_as_is"],
                llm_client=client,
                model="x",
                timeout_seconds=5.0,
            )

    @pytest.mark.asyncio
    async def test_payload_includes_system_key_for_sdk_backend(self):
        """Codex P1 (2026-05-22): SdkAnthropicClient._payload_to_kwargs indexes
        payload["system"] unconditionally. Without this key, the SDK backend
        raises KeyError before reaching Anthropic and every LLM dispatch
        silently falls back to rule-based. Guard the payload shape so the
        regression can't reappear.
        """
        captured: dict = {}

        async def _capture(payload):
            captured.update(payload)
            return _llm_response("retry_as_is")

        client = MagicMock()
        client.call = _capture

        await choose_action_llm(
            parent_name="p",
            parent_instructions=None,
            parent_error="incomplete_no_terminal",
            parent_result=None,
            fix_instructions=None,
            fix_actions=["retry_as_is"],
            llm_client=client,
            model="x",
            timeout_seconds=5.0,
        )
        assert "system" in captured, "payload must include 'system' for SDK backend"

    @pytest.mark.asyncio
    async def test_empty_fix_actions_raises(self):
        client = _llm_client_returning(_llm_response("retry_as_is"))
        with pytest.raises(ValueError, match="non-empty"):
            await choose_action_llm(
                parent_name="p",
                parent_instructions=None,
                parent_error=None,
                parent_result=None,
                fix_instructions=None,
                fix_actions=[],
                llm_client=client,
                model="x",
                timeout_seconds=5.0,
            )

    @pytest.mark.asyncio
    async def test_timeout_propagates(self):
        import asyncio

        async def _slow(_payload):
            await asyncio.sleep(0.5)
            return _llm_response("retry_as_is")

        client = MagicMock()
        client.call = _slow

        with pytest.raises(asyncio.TimeoutError):
            await choose_action_llm(
                parent_name="p",
                parent_instructions=None,
                parent_error=None,
                parent_result=None,
                fix_instructions=None,
                fix_actions=["retry_as_is"],
                llm_client=client,
                model="x",
                timeout_seconds=0.05,
            )


# ---------------------------------------------------------------------------
# Orchestrator integration tests
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def store(db):
    agent_id = f"f066-1-llm-{uuid.uuid4().hex[:8]}"
    return DAGStore(db, agent_id, Settings())


@pytest.fixture
def subtask_mgr():
    mgr = AsyncMock()
    mgr.create.return_value = SimpleNamespace(id=uuid.uuid4(), status="pending")
    return mgr


@pytest.fixture
def dynamic_loader():
    loader = AsyncMock()
    loader.create_check = AsyncMock(return_value={"name": "t"})
    loader._registry = MagicMock()
    loader._registry.get_check.return_value = None
    return loader


def _parent_with_fix_request(fix_actions: list[str]) -> DAGCreateRequest:
    return DAGCreateRequest(
        name="parent-with-fix",
        nodes=[
            DAGNodeSpec(name="parent", type=DAGNodeType.subtask, instructions="original prompt"),
            DAGNodeSpec(
                name="fix-parent",
                type=DAGNodeType.fix,
                parent_node="parent",
                fix_actions=fix_actions,
                instructions="recover from failure",
            ),
        ],
        edges=[DAGEdgeSpec(from_node="parent", to_node="fix-parent", edge_type="on_failure")],
    )


class TestOrchestratorLLMDispatch:
    @pytest.mark.asyncio
    async def test_llm_dispatch_applied_when_flag_on(
        self, store, subtask_mgr, dynamic_loader
    ):
        """Flag on + working LLM → orchestrator uses LLM-chosen action."""
        llm_client = _llm_client_returning(
            _llm_response("skip_and_continue", rationale="LLM said skip")
        )
        settings = Settings().model_copy(update={
            "dag_fix_llm_dispatch_enabled": True,
            "dag_fix_llm_timeout_seconds": 5.0,
        })
        orch = DAGOrchestrator(
            store=store,
            subtask_mgr=subtask_mgr,
            dynamic_loader=dynamic_loader,
            settings=settings,
            llm_client=llm_client,
        )

        dag = await store.create(_parent_with_fix_request(["retry_as_is", "skip_and_continue"]))
        await orch.start_dag(dag.id)

        # Fail the parent so the fix fires.
        fetched = await store.get_dag(dag.id)
        parent = next(n for n in fetched.nodes if n.name == "parent")
        await store.update_node(parent.id, status="failed", error="validation_failed")

        await orch.tick()

        fetched = await store.get_dag(dag.id)
        parent = next(n for n in fetched.nodes if n.name == "parent")
        # LLM said skip — even though rule-based would have said retry_as_is
        # (because parent.error contains 'validation_failed').
        assert parent.status == "skipped"
        llm_client.call.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_llm_failure_falls_back_to_rule_based(
        self, store, subtask_mgr, dynamic_loader
    ):
        """LLM raises → orchestrator falls back to choose_action."""
        llm_client = MagicMock()
        llm_client.call = AsyncMock(side_effect=RuntimeError("LLM unavailable"))

        settings = Settings().model_copy(update={
            "dag_fix_llm_dispatch_enabled": True,
        })
        orch = DAGOrchestrator(
            store=store,
            subtask_mgr=subtask_mgr,
            dynamic_loader=dynamic_loader,
            settings=settings,
            llm_client=llm_client,
        )

        dag = await store.create(_parent_with_fix_request(["retry_as_is"]))
        await orch.start_dag(dag.id)

        fetched = await store.get_dag(dag.id)
        parent = next(n for n in fetched.nodes if n.name == "parent")
        await store.update_node(parent.id, status="failed", error="validation_failed")

        await orch.tick()

        fetched = await store.get_dag(dag.id)
        parent = next(n for n in fetched.nodes if n.name == "parent")
        # Rule-based: validation_failed → retry_as_is → parent set to pending.
        assert parent.status in ("pending", "ready", "running")
        llm_client.call.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_flag_off_uses_rule_based(
        self, store, subtask_mgr, dynamic_loader
    ):
        """Flag off → LLM not called even if client is wired."""
        llm_client = MagicMock()
        llm_client.call = AsyncMock(return_value=_llm_response("skip_and_continue"))

        # Flag explicitly OFF: the repo .env sets
        # NOUS_DAG_FIX_LLM_DISPATCH_ENABLED=true (and a process-level
        # export would survive _env_file=None — codex P2), which would
        # turn this flag-OFF test into a flag-ON run (the mock's
        # skip_and_continue then marks the parent 'skipped').
        orch = DAGOrchestrator(
            store=store,
            subtask_mgr=subtask_mgr,
            dynamic_loader=dynamic_loader,
            settings=Settings(_env_file=None, dag_fix_llm_dispatch_enabled=False),
            llm_client=llm_client,
        )

        dag = await store.create(_parent_with_fix_request(["retry_as_is", "skip_and_continue"]))
        await orch.start_dag(dag.id)

        fetched = await store.get_dag(dag.id)
        parent = next(n for n in fetched.nodes if n.name == "parent")
        await store.update_node(parent.id, status="failed", error="validation_failed")

        await orch.tick()

        # Rule-based path: chose retry_as_is for validation_failed.
        fetched = await store.get_dag(dag.id)
        parent = next(n for n in fetched.nodes if n.name == "parent")
        assert parent.status in ("pending", "ready", "running")
        # The LLM client must NOT have been called.
        llm_client.call.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_amended_prompt_applied_to_parent_instructions(
        self, store, subtask_mgr, dynamic_loader
    ):
        """LLM returns retry_with_amended_prompt + new prompt → parent's
        instructions is overwritten."""
        llm_client = _llm_client_returning(
            _llm_response(
                "retry_with_amended_prompt",
                amended_prompt="Revised: only check items A and C",
                rationale="item B is unverifiable",
            )
        )
        settings = Settings().model_copy(update={
            "dag_fix_llm_dispatch_enabled": True,
        })
        orch = DAGOrchestrator(
            store=store,
            subtask_mgr=subtask_mgr,
            dynamic_loader=dynamic_loader,
            settings=settings,
            llm_client=llm_client,
        )

        dag = await store.create(
            _parent_with_fix_request(["retry_with_amended_prompt", "mark_unrecoverable"])
        )
        await orch.start_dag(dag.id)

        fetched = await store.get_dag(dag.id)
        parent = next(n for n in fetched.nodes if n.name == "parent")
        await store.update_node(parent.id, status="failed", error="unverifiable_claim")

        await orch.tick()

        fetched = await store.get_dag(dag.id)
        parent = next(n for n in fetched.nodes if n.name == "parent")
        # Parent re-enqueued AND instructions amended.
        assert parent.status in ("pending", "ready", "running")
        assert parent.instructions == "Revised: only check items A and C"
