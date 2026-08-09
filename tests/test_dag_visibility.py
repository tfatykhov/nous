"""Tests for F090.3: dag_manage action=recent (finished DAG discoverability)."""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from unittest.mock import AsyncMock

from nous.api.tools import ToolDispatcher, register_dag_tools
from nous.config import Settings
from nous.dag.orchestrator import DAGOrchestrator
from nous.dag.schemas import DAGCreateRequest, DAGNodeSpec, DAGNodeType
from nous.dag.store import DAGStore


@pytest_asyncio.fixture
async def dag_store(db):
    return DAGStore(db, f"test-vis-{uuid.uuid4().hex[:8]}",
                    Settings(_env_file=None))


@pytest_asyncio.fixture
async def tools(dag_store):
    orch = DAGOrchestrator(
        store=dag_store, subtask_mgr=AsyncMock(), dynamic_loader=AsyncMock(),
        settings=Settings(_env_file=None),
    )
    orch.clock_wired = True
    d = ToolDispatcher()
    register_dag_tools(d, dag_store, orch)
    return d


def _one(name: str) -> DAGCreateRequest:
    return DAGCreateRequest(
        name=name,
        nodes=[DAGNodeSpec(name="n", type=DAGNodeType.subtask,
                           instructions="x", timeout_seconds=120)],
    )


class TestFinishedDagsAreDiscoverable:
    @pytest.mark.asyncio
    async def test_list_still_shows_only_active(self, tools, dag_store):
        finished = await dag_store.create(_one("finished-one"))
        await dag_store.update_dag_status(finished.id, "completed",
                                          result_summary="done")
        await dag_store.create(_one("still-going"))

        text = (await tools._handlers["dag_manage"](action="list"))["content"][0]["text"]

        assert "still-going" in text
        assert "finished-one" not in text

    @pytest.mark.asyncio
    async def test_recent_shows_finished_dags(self, tools, dag_store):
        """Before F090.3 a finished DAG could only be reached by status if you
        already knew its id prefix — there was no way to discover one."""
        finished = await dag_store.create(_one("finished-one"))
        await dag_store.update_dag_status(finished.id, "completed",
                                          result_summary="all good")

        text = (await tools._handlers["dag_manage"](action="recent"))["content"][0]["text"]

        assert "finished-one" in text
        assert str(finished.id)[:8] in text
        assert "completed" in text

    @pytest.mark.asyncio
    async def test_recent_is_empty_message_not_error(self, tools):
        text = (await tools._handlers["dag_manage"](action="recent"))["content"][0]["text"]
        assert "Error" not in text
