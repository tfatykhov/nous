"""Tests for F090.3: dag_manage action=recent (finished DAG discoverability)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import update

from nous.api.tools import ToolDispatcher, register_dag_tools
from nous.config import Settings
from nous.dag.orchestrator import DAGOrchestrator
from nous.dag.schemas import DAGCreateRequest, DAGNodeSpec, DAGNodeType
from nous.dag.store import DAGStore
from nous.storage.models import ExecutionDAG


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

    @pytest.mark.asyncio
    async def test_recent_surfaces_a_finished_dag_pushed_out_by_newer_creates(
        self, tools, dag_store, db
    ):
        """codex P2: get_recent_dags(limit=20) orders by created_at and
        applies LIMIT before any status filter, so a DAG that finishes AFTER
        `limit` newer DAGs were CREATED drops off the created_at-ordered
        top-20 entirely — the Python-side finished-status filter never even
        sees it. Long-running DAGs are exactly the ones likely to have newer
        DAGs created while they're still running, so this hits `recent`'s
        primary use case. Must fail against the pre-fix implementation.

        `created_at`/`completed_at` are set directly via a raw UPDATE rather
        than relied on from real elapsed wall-clock time: the SQLite test
        backend's DateTime columns round-trip at whole-second precision (the
        Python-side `now()` shim is stamped per-object but truncated to the
        second on storage), so 21 DAGs created back-to-back land in only 3-4
        distinct one-second buckets and created_at-DESC ordering degenerates
        into a tie-break coin flip instead of reliably reproducing the bug —
        confirmed empirically: the wall-clock version of this test was
        flaky, sometimes passing against the UNFIXED code because `target`
        happened to tie-break into the top 20.
        """
        base = datetime.now(UTC) - timedelta(hours=1)

        target = await dag_store.create(_one("late-finisher"))
        await dag_store.update_dag_status(target.id, "completed",
                                          result_summary="finally done")

        newer_ids = []
        for i in range(20):
            newer = await dag_store.create(_one(f"newer-{i}"))
            await dag_store.update_dag_status(newer.id, "completed",
                                              result_summary="also done")
            newer_ids.append(newer.id)

        async with db.session() as session:
            # `target`: oldest created_at of all 21, but the MOST RECENT
            # completed_at (it finishes long after every "newer" DAG was
            # even created) — exactly the shape the finding describes.
            await session.execute(
                update(ExecutionDAG).where(ExecutionDAG.id == target.id).values(
                    created_at=base,
                    completed_at=base + timedelta(hours=2),
                )
            )
            for i, nid in enumerate(newer_ids):
                await session.execute(
                    update(ExecutionDAG).where(ExecutionDAG.id == nid).values(
                        created_at=base + timedelta(minutes=i + 1),
                        completed_at=base + timedelta(minutes=i + 1),
                    )
                )
            await session.commit()

        text = (await tools._handlers["dag_manage"](action="recent"))["content"][0]["text"]

        assert "late-finisher" in text
        assert str(target.id)[:8] in text
