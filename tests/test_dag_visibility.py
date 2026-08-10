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

    @pytest.mark.parametrize("case", ["upper", "mixed"])
    @pytest.mark.asyncio
    async def test_prefix_resolves_regardless_of_case(
        self, tools, dag_store, case
    ):
        """codex P2 FINDING 7: Postgres renders `id::text` lowercase and
        `LIKE` is case-sensitive, but `_DAG_ID_PREFIX_RE` legitimately
        admits `A`-`F` (case-insensitively-typed hex is still valid UUID
        content) -- an uppercase or mixed-case prefix validated and then
        silently matched nothing. Not a regression: the pre-FINDING-3
        `str(d.id).startswith(dag_id_str)` had the identical failure,
        since Python's `str(UUID)` is lowercase too; the validator now
        just accepts input it can't serve, which is worse than not
        documenting it. Must fail against the pre-fix implementation.
        """
        target = await dag_store.create(_one("case-target"))
        real_prefix = str(target.id)[:8]
        corrupted = (
            real_prefix.upper() if case == "upper"
            else real_prefix[:4].upper() + real_prefix[4:]
        )

        text = (await tools._handlers["dag_manage"](
            action="status", dag_id=corrupted))["content"][0]["text"]

        assert "not found" not in text
        assert "case-target" in text

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


class TestFinishedDagOutputIsRecoverable:
    """codex P2 FINDING 5: `recent` and `status` could tell an agent THAT a
    DAG finished but not WHAT it produced. `result_summary` is the generic
    constant `_check_dag_completion` writes ("All nodes completed
    successfully") — never the real outcome. `status` rendered every node
    field except `node.result`. Both matter most exactly when an F087
    delivery was missed, since that was this PR's whole justification for
    `recent` existing at all.
    """

    @pytest.mark.asyncio
    async def test_status_surfaces_node_results(self, tools, dag_store):
        """Must fail against the pre-fix implementation — `status` listed
        every node's name/type/wave/status/error but never its result.
        """
        dag = await dag_store.create(_one("result-bearing"))
        node = dag.nodes[0]
        await dag_store.update_node(
            node.id, status="completed",
            result="distinctive-output-xyz-the-actual-payload",
        )

        text = (await tools._handlers["dag_manage"](
            action="status", dag_id=str(dag.id)))["content"][0]["text"]

        assert "distinctive-output-xyz-the-actual-payload" in text

    @pytest.mark.asyncio
    async def test_recent_prefers_delivery_summary_over_generic_result_summary(
        self, tools, dag_store, db
    ):
        """Must fail against the pre-fix implementation — `recent` only
        ever read `result_summary`, the generic constant, never
        `delivery_summary`, the real agent-authored outcome F087 caches
        for exactly this purpose (delivery.py, ahead of retries).
        """
        dag = await dag_store.create(_one("summarized-dag"))
        await dag_store.update_dag_status(
            dag.id, "completed",
            result_summary="All nodes completed successfully",
        )
        async with db.session() as session:
            await session.execute(
                update(ExecutionDAG).where(ExecutionDAG.id == dag.id).values(
                    delivery_summary=(
                        "Deployed the new pricing page and verified it "
                        "renders correctly in production."
                    ),
                )
            )
            await session.commit()

        text = (await tools._handlers["dag_manage"](action="recent"))["content"][0]["text"]

        assert "Deployed the new pricing page" in text

    @pytest.mark.asyncio
    async def test_status_truncates_long_results_and_names_the_recovery_command(
        self, tools, dag_store
    ):
        """codex P2 round 5 FINDING 9: [:80] (the `error` convention) is
        right for a classified error string and useless for an LLM's
        actual output — a subtask result is routinely hundreds to
        thousands of characters. `status`'s preview must be a MEANINGFUL
        length, and truncation must never be silent: the line must say
        how much was cut and name the exact command that recovers the
        rest. Must fail against the pre-fix implementation, whose [:80]
        slice gives no indication anything was cut at all.
        """
        dag = await dag_store.create(_one("long-result-dag"))
        node = dag.nodes[0]
        long_result = "word " * 200  # 1000 chars, past any reasonable preview bound
        await dag_store.update_node(node.id, status="completed", result=long_result)

        text = (await tools._handlers["dag_manage"](
            action="status", dag_id=str(dag.id)))["content"][0]["text"]

        assert long_result not in text
        assert "truncated" in text
        assert "node_result" in text
        assert node.name in text

    @pytest.mark.asyncio
    async def test_status_does_not_mark_short_results_truncated(
        self, tools, dag_store
    ):
        """Guard against a truncation notice that always fires regardless
        of length.
        """
        dag = await dag_store.create(_one("short-result-dag"))
        node = dag.nodes[0]
        short_result = "all good, done."
        await dag_store.update_node(node.id, status="completed", result=short_result)

        text = (await tools._handlers["dag_manage"](
            action="status", dag_id=str(dag.id)))["content"][0]["text"]

        assert short_result in text
        assert "truncated" not in text

    @pytest.mark.asyncio
    async def test_node_result_returns_the_complete_result(self, tools, dag_store):
        """codex P2 round 5 FINDING 9: `status` stays a bounded overview —
        16 nodes rendered in full would flood the context, which is the
        actual reason for bounding it — so recovery is a lossless PATH,
        not an unbounded overview. Must fail against the pre-fix
        implementation: `node_result` does not exist as an action yet.
        """
        dag = await dag_store.create(_one("full-result-dag"))
        node = dag.nodes[0]
        long_result = "word " * 500  # 2500 chars
        await dag_store.update_node(node.id, status="completed", result=long_result)

        text = (await tools._handlers["dag_manage"](
            action="node_result", dag_id=str(dag.id), node_name=node.name
        ))["content"][0]["text"]

        assert text == long_result

    @pytest.mark.asyncio
    async def test_node_result_unknown_node_name_lists_available_nodes(
        self, tools, dag_store
    ):
        """Unknown node_name must give a clear, helpful error — not an
        empty response.
        """
        dag = await dag_store.create(_one("known-nodes-dag"))
        node = dag.nodes[0]

        text = (await tools._handlers["dag_manage"](
            action="node_result", dag_id=str(dag.id), node_name="does-not-exist"
        ))["content"][0]["text"]

        assert "Error" in text
        assert node.name in text
        assert "All nodes completed successfully" not in text
