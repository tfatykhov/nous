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


class TestResolveDagPrefix:
    """codex P2 FINDING 3: `_resolve_dag`'s fallback had the same shape as
    FINDING 1 — `get_recent_dags(limit=20)` then a Python-side filter — for
    the `status`/`cancel`/`retry_node` prefix lookup. A finished DAG outside
    that created_at-ordered top-20 was unresolvable by prefix, which is
    exactly the DAG a user is likely to ask about right after an F087
    delivery notification announces it.
    """

    @pytest.mark.asyncio
    async def test_prefix_resolves_a_finished_dag_pushed_out_by_newer_creates(
        self, tools, dag_store, db
    ):
        """Must fail against the pre-fix implementation — see FINDING 1's
        sibling test for why timestamps are set directly rather than via
        real elapsed wall-clock time (whole-second truncation on the
        SQLite test backend makes wall-clock ordering a tie-break coin
        flip).
        """
        base = datetime.now(UTC) - timedelta(hours=1)

        target = await dag_store.create(_one("late-finisher-2"))
        await dag_store.update_dag_status(target.id, "completed",
                                          result_summary="finally done")

        newer_ids = []
        for i in range(20):
            newer = await dag_store.create(_one(f"newer2-{i}"))
            await dag_store.update_dag_status(newer.id, "completed",
                                              result_summary="also done")
            newer_ids.append(newer.id)

        async with db.session() as session:
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

        prefix = str(target.id)[:8]
        text = (await tools._handlers["dag_manage"](
            action="status", dag_id=prefix))["content"][0]["text"]

        assert "not found" not in text
        assert "late-finisher-2" in text

    @pytest.mark.asyncio
    async def test_ambiguous_prefix_raises_naming_both_candidates(
        self, tools, dag_store, db
    ):
        """Two DAGs sharing an 8-char prefix must report BOTH ids, not
        silently pick one. Ids are chosen explicitly (bypassing
        DAGStore.create()'s random gen_random_uuid()/uuid4() assignment)
        since forcing a real collision would mean mutating an
        already-inserted DAG's primary key, which the dag_nodes.dag_id
        FK (ON DELETE CASCADE, no ON UPDATE) does not tolerate.
        """
        shared = "deadbeef"
        id_a = uuid.UUID(f"{shared}-0000-4000-8000-000000000001")
        id_b = uuid.UUID(f"{shared}-0000-4000-8000-000000000002")

        async with db.session() as session:
            session.add_all([
                ExecutionDAG(id=id_a, agent_id=dag_store._agent_id, name="dag-a"),
                ExecutionDAG(id=id_b, agent_id=dag_store._agent_id, name="dag-b"),
            ])
            await session.commit()

        text = (await tools._handlers["dag_manage"](
            action="status", dag_id=shared))["content"][0]["text"]

        assert "ambiguous" in text
        assert str(id_a)[:8] in text or shared in text
        assert "deadbeef" in text

    @pytest.mark.asyncio
    async def test_unknown_prefix_returns_not_found(self, tools):
        text = (await tools._handlers["dag_manage"](
            action="status", dag_id="ffffffff"))["content"][0]["text"]
        assert "not found" in text

    @pytest.mark.parametrize("bad_char", ["_", "%"])
    @pytest.mark.asyncio
    async def test_wildcard_metacharacters_do_not_match_unrelated_dags(
        self, tools, dag_store, bad_char
    ):
        """A prefix with a LIKE metacharacter substituted for one real
        character must NOT wildcard-match the DAG it was derived from —
        proving `%`/`_` are rejected rather than passed through to SQL
        LIKE, where `_` matches any single char and `%` matches any run.
        """
        target = await dag_store.create(_one("wildcard-target"))
        real_prefix = str(target.id)[:8]
        corrupted = bad_char + real_prefix[1:]

        text = (await tools._handlers["dag_manage"](
            action="status", dag_id=corrupted))["content"][0]["text"]

        assert "not found" in text
        assert "wildcard-target" not in text

    @pytest.mark.asyncio
    async def test_prefix_lookup_is_agent_scoped(self, tools, db):
        """A prefix belonging to another agent's DAG must not resolve —
        matches DAGStore's agent-scoping discipline everywhere else.
        """
        other_store = DAGStore(db, f"test-vis-other-{uuid.uuid4().hex[:8]}",
                               Settings(_env_file=None))
        other_dag = await other_store.create(_one("someone-elses-dag"))
        prefix = str(other_dag.id)[:8]

        text = (await tools._handlers["dag_manage"](
            action="status", dag_id=prefix))["content"][0]["text"]

        assert "not found" in text
