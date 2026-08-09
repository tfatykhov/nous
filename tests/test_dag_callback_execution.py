from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

from nous.config import Settings
from nous.dag import orchestrator as orch_mod
from nous.dag.orchestrator import DAGOrchestrator
from nous.dag.schemas import DAGCreateRequest, DAGEdgeSpec, DAGNodeSpec, DAGNodeType
from nous.dag.store import DAGStore


def test_subtask_backed_covers_subtask_and_callback():
    """The seven gates that used to hardcode 'subtask' now share one set.

    F087 review found the same fix applied to some sites and not others,
    repeatedly. A single constant makes the set impossible to drift.
    """
    assert orch_mod._SUBTASK_BACKED == frozenset({"subtask", "callback"})


def test_no_bare_subtask_type_comparisons_remain():
    """Guard against a future site re-hardcoding the literal.

    Six of the seven original gates share _SUBTASK_BACKED directly. The
    seventh — the F064.2 concurrency-cap gate in _dispatch_ready_nodes —
    used to keep a bare 'node_type != "subtask"' literal instead, because
    folding callbacks into _SUBTASK_BACKED there was a real behaviour change
    (cap-gating callbacks that at the time always completed instantly for
    free), not the no-op it was everywhere else.

    F090.1 made that behaviour change real: callback nodes can now execute
    as subtasks (dag_callback_execution_enabled), and once they do, they
    consume a worker the cap is meant to bound. The seventh site now reads
    _SUBTASK_BACKED plus the flag instead of the bare literal — see the
    'cap_exempt' comment block at that call site. This asserts the bare
    literal is gone everywhere, and that the conditional exemption made it
    into the right function.
    """
    import inspect

    src = inspect.getsource(orch_mod)
    total = src.count('node_type == "subtask"') + src.count('node_type != "subtask"')
    assert total == 0, (
        f"expected zero bare node_type/'subtask' comparisons, found {total}"
    )

    dispatch_src = inspect.getsource(orch_mod.DAGOrchestrator._dispatch_ready_nodes)
    assert "_SUBTASK_BACKED" in dispatch_src
    assert "dag_callback_execution_enabled" in dispatch_src


def _settings(**overrides) -> Settings:
    base = dict(_env_file=None, dag_callback_execution_enabled=True)
    base.update(overrides)
    return Settings(**base)


@pytest_asyncio.fixture
async def store(db):
    return DAGStore(db, f"test-cb-{uuid.uuid4().hex[:8]}", _settings())


@pytest.fixture
def subtask_mgr():
    mgr = AsyncMock()
    mgr.create.return_value = SimpleNamespace(id=uuid.uuid4(), status="pending")
    return mgr


@pytest.fixture
def dynamic_loader():
    loader = AsyncMock()
    loader._registry = MagicMock()
    loader._registry.get_check.return_value = None
    return loader


def _orch(store, subtask_mgr, dynamic_loader, settings=None):
    o = DAGOrchestrator(
        store=store, subtask_mgr=subtask_mgr,
        dynamic_loader=dynamic_loader, settings=settings or _settings(),
    )
    o.clock_wired = True
    return o


def _callback_after_work() -> DAGCreateRequest:
    return DAGCreateRequest(
        name="cb-dag",
        nodes=[
            DAGNodeSpec(name="work", type=DAGNodeType.subtask,
                        instructions="do the work", timeout_seconds=120),
            DAGNodeSpec(name="handle", type=DAGNodeType.callback,
                        instructions="Review the result and act on it",
                        tools=["bash"], timeout_seconds=120),
        ],
        edges=[DAGEdgeSpec(from_node="work", to_node="handle",
                           edge_type="context_flow")],
    )


class TestCallbackExecutes:
    @pytest.mark.asyncio
    async def test_callback_creates_a_subtask_with_predecessor_context(
        self, store, subtask_mgr, dynamic_loader
    ):
        """The whole point: a callback must READ its predecessor and act.

        Before F090.1 this node completed instantly with its own instruction
        text as the result — 103 callback nodes in the dev DB, all 'completed',
        none having executed anything.
        """
        dag = await store.create(_callback_after_work())
        await store.update_dag_status(dag.id, "running")
        dag = await store.get_dag(dag.id)
        work = next(n for n in dag.nodes if n.name == "work")
        await store.update_node(
            work.id, status="completed", result="BUILD OK: 0 errors",
        )
        subtask_mgr.get.return_value = SimpleNamespace(
            id=uuid.uuid4(), status="running", result=None, error=None,
            final_outcome=None, tokens_in=0, tokens_out=0, started_at=None,
        )

        await _orch(store, subtask_mgr, dynamic_loader).tick()

        assert subtask_mgr.create.await_count == 1
        task_text = subtask_mgr.create.await_args.kwargs["task"]
        assert "BUILD OK: 0 errors" in task_text
        assert "Review the result and act on it" in task_text
        handle = next(
            n for n in (await store.get_dag(dag.id)).nodes if n.name == "handle"
        )
        assert handle.status == "running"
        assert handle.subtask_id is not None

    @pytest.mark.asyncio
    async def test_callback_forwards_timeout_and_dag_node_id(
        self, store, subtask_mgr, dynamic_loader
    ):
        """NOT a claim that tools are forwarded — they aren't, for any
        node type. SubtaskManager.create has no tools parameter at all;
        DAGNodeSpec.tools is silently dropped for both subtask and callback
        nodes today (pre-existing, out of scope for F090.1)."""
        dag = await store.create(_callback_after_work())
        await store.update_dag_status(dag.id, "running")
        dag = await store.get_dag(dag.id)
        work = next(n for n in dag.nodes if n.name == "work")
        await store.update_node(work.id, status="completed", result="done")
        subtask_mgr.get.return_value = SimpleNamespace(
            id=uuid.uuid4(), status="running", result=None, error=None,
            final_outcome=None, tokens_in=0, tokens_out=0, started_at=None,
        )

        await _orch(store, subtask_mgr, dynamic_loader).tick()

        kwargs = subtask_mgr.create.await_args.kwargs
        assert kwargs["timeout"] == 120
        assert kwargs["dag_node_id"] is not None

    @pytest.mark.asyncio
    async def test_flag_off_keeps_the_legacy_instant_completion(
        self, store, subtask_mgr, dynamic_loader
    ):
        """Default is OFF: 83 existing DAGs must not start paying for LLM turns
        the moment this deploys."""
        dag = await store.create(_callback_after_work())
        await store.update_dag_status(dag.id, "running")
        dag = await store.get_dag(dag.id)
        work = next(n for n in dag.nodes if n.name == "work")
        handle_instructions = next(
            n for n in dag.nodes if n.name == "handle"
        ).instructions
        await store.update_node(work.id, status="completed", result="done")

        orch = _orch(store, subtask_mgr, dynamic_loader,
                     _settings(dag_callback_execution_enabled=False))
        await orch.tick()

        handle = next(
            n for n in (await store.get_dag(dag.id)).nodes if n.name == "handle"
        )
        assert handle.status == "completed"
        assert handle.subtask_id is None
        assert subtask_mgr.create.await_count == 0
        # These two are the fields whose drift would actually change the 83
        # existing DAG shapes on deploy: result feeds downstream
        # _build_predecessor_context calls, and started_at/completed_at are
        # what a dashboard or a wall-clock check would key off of. Byte-for-
        # byte match to the pre-F090 stub, not just "some completion".
        assert handle.result == handle_instructions
        assert handle.started_at is not None
        assert handle.completed_at is not None

    @pytest.mark.asyncio
    async def test_executing_callback_inherits_the_wall_clock_reaper(
        self, store, subtask_mgr, dynamic_loader
    ):
        """It gets F087's backstops for free by having a subtask_id."""
        from datetime import UTC, datetime, timedelta

        dag = await store.create(_callback_after_work())
        await store.update_dag_status(dag.id, "running")
        dag = await store.get_dag(dag.id)
        handle = next(n for n in dag.nodes if n.name == "handle")
        sid = uuid.uuid4()
        long_ago = datetime.now(UTC) - timedelta(seconds=5000)
        await store.update_node(
            handle.id, status="running", subtask_id=sid, started_at=long_ago,
        )
        state = {"status": "running"}

        async def _get(_s):
            return SimpleNamespace(
                id=sid, status=state["status"], result=None, error=None,
                final_outcome=None, tokens_in=0, tokens_out=0,
                started_at=long_ago,
            )

        async def _cancel(_s):
            state["status"] = "cancelled"
            return True

        subtask_mgr.get = AsyncMock(side_effect=_get)
        subtask_mgr.cancel = AsyncMock(side_effect=_cancel)

        await _orch(store, subtask_mgr, dynamic_loader).tick()

        reaped = next(
            n for n in (await store.get_dag(dag.id)).nodes if n.name == "handle"
        )
        assert reaped.status == "failed"
        assert "exceeded wall-clock budget" in reaped.error


def _work_and_callback_same_frame(frame: str) -> DAGCreateRequest:
    """Two independent (no context_flow edge) wave-0 nodes on one frame.

    No edge between them so both are 'ready' in the same tick — isolates the
    F064.2 cap-dispatch predicate (test-2's job) from callback-execution
    itself (already covered above), instead of needing two ticks to observe
    a deferral.
    """
    return DAGCreateRequest(
        name="cap-vs-callback-dag",
        nodes=[
            DAGNodeSpec(name="sub-1", type=DAGNodeType.subtask,
                        instructions="work", frame_type=frame),
            DAGNodeSpec(name="cb-1", type=DAGNodeType.callback,
                        instructions="handle it", frame_type=frame),
        ],
        max_concurrent_by_frame_type={frame: 1},
    )


class TestCallbackCapExemption:
    """F090.1 correction (B): the F064.2 cap-exemption predicate in
    _dispatch_ready_nodes must track dag_callback_execution_enabled — a
    callback is cap-exempt only while it still completes for free. Once it
    executes, it consumes a worker slot exactly like a subtask node.
    """

    @pytest.mark.asyncio
    async def test_flag_off_callback_still_bypasses_the_cap(
        self, store, subtask_mgr, dynamic_loader
    ):
        """Same contract test_check_nodes_bypass_frame_cap_enforcement
        already pins in tests/test_dag_concurrency_caps.py — repeated here,
        scoped to this module, as the flag-OFF half of the pair.

        Fix round 1: the original version of this test used two independent
        ready nodes ("sub-1" + "cb-1") against an EMPTY running_by_frame seed
        (0 running subtasks). `0 >= cap(1)` is false whether or not the
        callback is exempt, so an accidentally cap-gated callback still
        launched, still completed once its (mocked) subtask resolved, and
        still produced identical `by_status`/`create.await_count` — the test
        could not distinguish the correct predicate from its inversion (the
        reviewer confirmed this by mutation). Fixed by pre-exhausting the cap
        via a stubbed `count_running_subtasks_by_frame_type` BEFORE dispatch,
        so an exempt callback (correct) completes while a cap-gated callback
        (broken) would be deferred to "pending" instead — an observable
        split. Verified against both the correct predicate and its inversion;
        see task-2-report.md for the mutation-proof transcript.
        """
        settings = _settings(
            dag_callback_execution_enabled=False,
            dag_frame_concurrency_enabled=True,
            dag_global_max_concurrent_by_frame={},
        )
        local_store = DAGStore(store._db, store._agent_id, settings)
        dag = await local_store.create(DAGCreateRequest(
            name="cap-exhausted-dag",
            nodes=[
                DAGNodeSpec(name="cb-1", type=DAGNodeType.callback,
                            instructions="handle it", frame_type="debug"),
            ],
            max_concurrent_by_frame_type={"debug": 1},
        ))
        # Pre-exhaust the frame's only slot. Stubbed rather than seeded via a
        # second real subtask row: the count query joins dag_nodes scoped to
        # THIS dag_id (@codex P1 on aa3c739), so a genuinely independent
        # "already running elsewhere" subtask isn't reachable without a
        # second DAG — the stub is the direct, honest way to force the
        # pre-dispatch state this test needs.
        local_store.count_running_subtasks_by_frame_type = AsyncMock(
            return_value={"debug": 1}
        )
        orch = _orch(local_store, subtask_mgr, dynamic_loader, settings)

        await orch.start_dag(dag.id)

        fetched = await local_store.get_dag(dag.id)
        cb = next(n for n in fetched.nodes if n.name == "cb-1")
        # Exempt (correct): completes instantly, cap never consulted for it.
        # Cap-gated (broken): would be deferred to "pending" instead, since
        # running_by_frame["debug"]=1 >= cap=1.
        assert cb.status == "completed"
        assert subtask_mgr.create.await_count == 0

    @pytest.mark.asyncio
    async def test_flag_on_callback_consumes_a_cap_slot(
        self, store, subtask_mgr, dynamic_loader
    ):
        """With the flag on, 'cb-1' now launches a real subtask and must
        compete for the same frame's cap slot instead of running alongside
        'sub-1' for free.

        Asserted on status counts, not node identity, mirroring
        test_in_memory_accumulator_prevents_overdispatch_within_tick in
        tests/test_dag_concurrency_caps.py — NOT because dispatch order is
        uncontracted (fix round 1 correction: it is. ExecutionDAG.nodes is
        `order_by="DAGNode.wave, DAGNode.name"` — nous/storage/models.py:1075
        — and both start_dag and _find_ready_nodes preserve that relationship
        order, so within wave 0 "cb-1" deterministically dispatches before
        "sub-1" and wins the single slot every time here). The status-count
        assertion is used because this test's claim is "exactly one of the
        two gets the slot, not both" — which node doesn't matter to that
        claim. A test that instead wants to pin a *specific* winner should
        name nodes so the intended winner sorts first and assert node
        identity directly, not rely on order being unspecified."""
        settings = _settings(
            dag_callback_execution_enabled=True,
            dag_frame_concurrency_enabled=True,
            dag_global_max_concurrent_by_frame={},
        )
        local_store = DAGStore(store._db, store._agent_id, settings)
        dag = await local_store.create(_work_and_callback_same_frame("debug"))
        orch = _orch(local_store, subtask_mgr, dynamic_loader, settings)

        await orch.start_dag(dag.id)

        fetched = await local_store.get_dag(dag.id)
        statuses = sorted(n.status for n in fetched.nodes)
        # Both nodes are now cap-consuming, so cap=1 admits exactly one and
        # defers the other — unlike the flag-OFF test above, where "cb-1"
        # is unconditionally exempt and both nodes always settle.
        assert statuses == ["pending", "running"]
        assert subtask_mgr.create.await_count == 1  # only the admitted node
