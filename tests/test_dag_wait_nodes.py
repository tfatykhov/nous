"""Typed waits for lineage DAGs: the orchestrator launches, polls and finishes wait nodes.

A wait node runs no model and no shell: it parks in awaiting_check and the tick evaluates its wait_for in-process.
postgres_only: the real DAGStore and DAGOrchestrator (conditional writes, JSONB wait_for).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import delete

from nous.config import Settings
from nous.dag import orchestrator as orchestrator_module
from nous.dag.orchestrator import DAGOrchestrator, _is_working
from nous.dag.schemas import DAGCreateRequest, DAGEdgeSpec, DAGNodeSpec, DAGNodeType
from nous.dag.store import DAGStore
from nous.observability.github_verify import GitHubReader
from nous.storage.models import ExecutionDAG

pytestmark = pytest.mark.postgres_only

JOB = "job-20261027-101500-ab12cd34"


@pytest_asyncio.fixture
async def world(db, tmp_path):
    agent = f"test-wait-node-{uuid.uuid4().hex[:10]}"
    cfg = Settings(
        _env_file=None,
        agent_id=agent,
        workspace_dir=str(tmp_path),
        github_default_repo="o/r",
        dag_node_default_timeout=600,
        dag_node_max_timeout=7200,
    ).model_copy(update={"github_token": ""})  # read from GITHUB_TOKEN, not a NOUS_ name
    store = DAGStore(db, agent, cfg)
    subtask_mgr, loader = AsyncMock(), AsyncMock()
    orch = DAGOrchestrator(store=store, subtask_mgr=subtask_mgr, dynamic_loader=loader, settings=cfg)
    orch.clock_wired = True
    yield SimpleNamespace(
        db=db, store=store, orch=orch, cfg=cfg, subtask_mgr=subtask_mgr, loader=loader, workspace=tmp_path
    )
    async with db.session() as session:
        await session.execute(delete(ExecutionDAG).where(ExecutionDAG.agent_id == agent))
        await session.commit()


async def _wait_dag(world, wait_for, *, interval=None, attempts=None, timeout=None, start=True):
    """wait -> after (a gate, which auto-passes once the wait completes)."""
    dag = await world.store.create(
        DAGCreateRequest(
            name="waiting",
            nodes=[
                DAGNodeSpec(
                    name="w",
                    type=DAGNodeType.wait,
                    wait_for=wait_for,
                    completion_check_interval=interval,
                    max_check_attempts=attempts,
                    timeout_seconds=timeout,
                ),
                DAGNodeSpec(name="after", type=DAGNodeType.gate),
            ],
            edges=[DAGEdgeSpec(from_node="w", to_node="after")],
        )
    )
    if start:
        await world.orch.start_dag(dag.id)
    return dag.id


async def _nodes(world, dag_id):
    dag = await world.store.get_dag(dag_id)
    return dag, {n.name: n for n in dag.nodes}


def _job(world, status: str) -> None:
    job_dir = world.workspace / "claude-jobs" / JOB
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "status").write_text(status + "\n")


async def test_a_file_wait_parks_then_completes_and_releases_its_successor(world):
    dag_id = await _wait_dag(world, {"kind": "file", "path": "out/r.md"})
    _, nodes = await _nodes(world, dag_id)
    w = nodes["w"]
    assert w.status == "awaiting_check" and w.started_at and w.awaiting_check_at
    assert w.check_attempts == 0
    world.subtask_mgr.create.assert_not_called()
    world.loader.create_check.assert_not_called()

    await world.orch.tick()
    _, nodes = await _nodes(world, dag_id)
    assert (nodes["w"].status, nodes["w"].check_attempts) == ("awaiting_check", 1)
    assert nodes["after"].status == "pending"

    (world.workspace / "out").mkdir()
    (world.workspace / "out" / "r.md").write_text("done")
    await world.orch.tick()
    dag, nodes = await _nodes(world, dag_id)
    assert (nodes["w"].status, nodes["w"].result) == ("completed", "file out/r.md exists")
    assert nodes["w"].completed_at is not None
    assert nodes["after"].status == "completed"
    assert dag.status == "completed"
    world.subtask_mgr.create.assert_not_called()
    world.loader.create_check.assert_not_called()


async def test_a_job_wait_completes_when_the_job_finishes(world):
    _job(world, "running")
    dag_id = await _wait_dag(world, {"kind": "job", "job_id": JOB})
    await world.orch.tick()
    assert (await _nodes(world, dag_id))[1]["w"].status == "awaiting_check"
    _job(world, "completed")
    await world.orch.tick()
    _, nodes = await _nodes(world, dag_id)
    assert (nodes["w"].status, nodes["w"].result) == ("completed", f"job {JOB} finished: completed")


async def test_a_job_wait_fails_for_a_failed_job_when_until_completed(world):
    _job(world, "failed")
    dag_id = await _wait_dag(world, {"kind": "job", "job_id": JOB, "until": "completed"})
    await world.orch.tick()
    dag, nodes = await _nodes(world, dag_id)
    assert nodes["w"].status == "failed"
    assert nodes["w"].error == f"job {JOB} finished: failed"
    assert nodes["after"].status == "blocked"
    assert dag.status == "failed"


def _reader_for(world, handler, readers: list):
    def factory() -> GitHubReader:
        reader = GitHubReader(transport=httpx.MockTransport(handler))
        close = reader.aclose
        reader.aclose = AsyncMock(side_effect=close)
        readers.append(reader)
        return reader

    world.orch._github_reader_factory = factory


def _pull(state="open", merged=False) -> dict:
    return {
        "number": 7,
        "state": state,
        "merged_at": "2026-10-27T00:00:00Z" if merged else None,
        "head": {"sha": "a" * 40},
    }


async def _due(world, node_id) -> None:
    """Make a polled node due again: its last poll a little over a minute ago."""
    await world.store.update_node(node_id, last_check_at=datetime.now(UTC) - timedelta(seconds=61))


async def test_a_pr_wait_uses_the_github_reader(world):
    answers = {"next": httpx.Response(200, json=_pull())}
    seen: list[httpx.Request] = []
    readers: list[GitHubReader] = []

    def handler(request):
        seen.append(request)
        return answers["next"]

    _reader_for(world, handler, readers)
    dag_id = await _wait_dag(world, {"kind": "pr", "number": 7, "until": "merged"})
    w_id = (await _nodes(world, dag_id))[1]["w"].id

    await world.orch.tick()  # open: still waiting
    _, nodes = await _nodes(world, dag_id)
    assert (nodes["w"].status, nodes["w"].error) == ("awaiting_check", None)
    assert seen[-1].url.path == "/repos/o/r/pulls/7"  # NOUS_GITHUB_DEFAULT_REPO

    answers["next"] = httpx.Response(500, json={})
    await _due(world, w_id)
    await world.orch.tick()  # GitHub failing: still waiting, never failed
    _, nodes = await _nodes(world, dag_id)
    assert (nodes["w"].status, nodes["w"].error, nodes["w"].check_attempts) == ("awaiting_check", None, 2)

    answers["next"] = httpx.Response(200, json=_pull("closed", merged=True))
    await _due(world, w_id)
    await world.orch.tick()
    _, nodes = await _nodes(world, dag_id)
    assert (nodes["w"].status, nodes["w"].result) == ("completed", "PR #7 merged")
    assert len(readers) == 3 and all(r.aclose.await_count == 1 for r in readers)


async def test_a_pr_wait_with_no_repo_is_impossible(world):
    dag_id = await _wait_dag(world, {"kind": "pr", "number": 7, "until": "merged"})
    world.orch._settings = world.cfg.model_copy(update={"github_default_repo": ""})
    await world.orch.tick()
    _, nodes = await _nodes(world, dag_id)
    assert nodes["w"].status == "failed"
    assert nodes["w"].error.startswith("pr wait needs a repo")


async def test_a_pr_wait_polls_at_most_once_a_minute(world):
    seen: list[httpx.Request] = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=_pull())

    _reader_for(world, handler, [])
    dag_id = await _wait_dag(world, {"kind": "pr", "number": 7, "until": "merged"})
    w = (await _nodes(world, dag_id))[1]["w"]
    assert w.completion_check_interval == 60
    await world.store.update_node(w.id, last_check_at=datetime.now(UTC) - timedelta(seconds=10))
    await world.orch.tick()
    assert seen == []
    await _due(world, w.id)
    await world.orch.tick()
    assert len(seen) == 1
    # A row written without the floor is still held to it.
    await world.store.update_node(
        w.id, completion_check_interval=5, last_check_at=datetime.now(UTC) - timedelta(seconds=10)
    )
    await world.orch.tick()
    assert len(seen) == 1


async def test_a_file_wait_with_no_interval_is_polled_every_tick(world):
    dag_id = await _wait_dag(world, {"kind": "file", "path": "x"})
    for expected in (1, 2, 3):
        await world.orch.tick()
        assert (await _nodes(world, dag_id))[1]["w"].check_attempts == expected


async def test_a_wait_times_out_from_its_park(world):
    dag_id = await _wait_dag(world, {"kind": "file", "path": "x"}, timeout=600)
    w = (await _nodes(world, dag_id))[1]["w"]
    await world.store.update_node(w.id, awaiting_check_at=datetime.now(UTC) - timedelta(seconds=500))
    await world.orch.tick()
    assert (await _nodes(world, dag_id))[1]["w"].status == "awaiting_check"  # inside its window
    await world.store.update_node(w.id, awaiting_check_at=datetime.now(UTC) - timedelta(seconds=7200))
    await world.orch.tick()
    w = (await _nodes(world, dag_id))[1]["w"]
    assert w.status == "failed"
    assert w.error.startswith("Wait timed out after 600s")


async def test_a_wait_fails_after_its_max_attempts(world):
    dag_id = await _wait_dag(world, {"kind": "file", "path": "x"}, attempts=2)
    await world.orch.tick()
    await world.orch.tick()
    w = (await _nodes(world, dag_id))[1]["w"]
    assert (w.status, w.check_attempts) == ("awaiting_check", 2)
    await world.orch.tick()
    w = (await _nodes(world, dag_id))[1]["w"]
    assert (w.status, w.error) == ("failed", "Wait exceeded max attempts (2)")


async def test_a_condition_already_true_completes_on_the_first_tick(world):
    (world.workspace / "ready.md").write_text("x")
    dag_id = await _wait_dag(world, {"kind": "file", "path": "ready.md"})
    await world.orch.tick()
    assert (await _nodes(world, dag_id))[1]["w"].status == "completed"


async def test_the_completion_check_poll_leaves_a_wait_alone(world):
    """_poll_awaiting_checks completes any awaiting_check node with no completion_check, which a wait never has."""
    dag_id = await _wait_dag(world, {"kind": "file", "path": "absent.md"})
    dag, _ = await _nodes(world, dag_id)
    await world.orch._poll_awaiting_checks(dag)
    w = (await _nodes(world, dag_id))[1]["w"]
    assert (w.status, w.completed_at) == ("awaiting_check", None)


async def test_a_wait_takes_no_slot_and_is_never_held(world):
    dag = await world.store.create(
        DAGCreateRequest(
            name="held",
            nodes=[
                DAGNodeSpec(name="g", type=DAGNodeType.gate),
                DAGNodeSpec(name="w", type=DAGNodeType.wait, wait_for={"kind": "file", "path": "x"}),
            ],
            edges=[DAGEdgeSpec(from_node="g", to_node="w")],
        )
    )
    await world.orch.start_dag(dag.id)  # the gate completes; the wait is next
    loaded = await world.store.get_dag(dag.id)
    assert world.orch._costs_nothing(next(n for n in loaded.nodes if n.name == "w"))
    await world.orch._advance_dag(loaded, may_start=False)
    assert dag.id not in world.orch._held_this_tick
    _, nodes = await _nodes(world, dag.id)
    assert nodes["w"].status == "awaiting_check"
    assert not _is_working(await world.store.get_dag(dag.id))


async def test_cancel_and_retry(world):
    dag_id = await _wait_dag(world, {"kind": "file", "path": "x"}, timeout=600)
    w = (await _nodes(world, dag_id))[1]["w"]
    await world.store.update_node(w.id, awaiting_check_at=datetime.now(UTC) - timedelta(seconds=7200))
    await world.orch.tick()
    dag, nodes = await _nodes(world, dag_id)
    assert (nodes["w"].status, dag.status) == ("failed", "failed")

    await world.orch.retry_node(dag_id, "w")
    w = (await _nodes(world, dag_id))[1]["w"]
    assert (w.status, w.awaiting_check_at, w.check_attempts) == ("pending", None, 0)
    await world.orch.tick()
    w = (await _nodes(world, dag_id))[1]["w"]
    assert w.status == "awaiting_check" and w.awaiting_check_at is not None

    await world.orch.cancel_dag(dag_id)
    dag, nodes = await _nodes(world, dag_id)
    assert (nodes["w"].status, dag.status) == ("cancelled", "cancelled")


async def test_the_park_resets_the_attempt(world):
    dag_id = await _wait_dag(world, {"kind": "file", "path": "x"})
    w = (await _nodes(world, dag_id))[1]["w"]
    # As the fix stage's retry_as_is leaves a node: pending again, with the old attempt's counters.
    await world.store.update_node(
        w.id, status="pending", check_attempts=5, last_check_at=datetime.now(UTC), error="old", result="old"
    )
    dag, _ = await _nodes(world, dag_id)
    await world.orch._advance_dag(dag)
    w = (await _nodes(world, dag_id))[1]["w"]
    assert w.status == "awaiting_check"
    assert (w.check_attempts, w.error, w.result) == (0, None, None)


async def test_a_cancel_that_lands_during_the_poll_keeps_its_status(world, monkeypatch):
    (world.workspace / "x").write_text("x")
    dag_id = await _wait_dag(world, {"kind": "file", "path": "x"})
    w = (await _nodes(world, dag_id))[1]["w"]
    original = world.store.transition_node

    async def cancel_first(node_id, **kwargs):
        if node_id == w.id and kwargs.get("status") == "completed":
            await world.store.update_node(w.id, status="cancelled")
        return await original(node_id, **kwargs)

    monkeypatch.setattr(world.store, "transition_node", cancel_first)
    dag, _ = await _nodes(world, dag_id)
    await world.orch._poll_wait_nodes(dag)
    assert next(n for n in dag.nodes if n.name == "w").status == "awaiting_check"  # the lost write is not mirrored
    w = (await _nodes(world, dag_id))[1]["w"]
    assert (w.status, w.result) == ("cancelled", None)


async def test_stall_detection_and_the_reaper_leave_a_wait_alone(world):
    world.orch._settings = world.cfg.model_copy(
        update={"dag_stall_detection_enabled": True, "dag_node_reaper_enabled": True}
    )
    dag_id = await _wait_dag(world, {"kind": "file", "path": "x"}, timeout=60)
    w = (await _nodes(world, dag_id))[1]["w"]
    long_ago = datetime.now(UTC) - timedelta(hours=5)
    await world.store.update_node(w.id, started_at=long_ago, awaiting_check_at=long_ago, last_activity_at=long_ago)
    dag, _ = await _nodes(world, dag_id)
    await world.orch._check_stalled_nodes(dag)
    await world.orch._reap_overrun_nodes(dag)
    assert (await _nodes(world, dag_id))[1]["w"].status == "awaiting_check"


async def test_a_stored_wait_for_that_is_not_valid_fails_the_launch(world):
    dag = await world.store.create(
        DAGCreateRequest(
            name="bad",
            nodes=[
                DAGNodeSpec(name="g", type=DAGNodeType.gate),
                DAGNodeSpec(name="w", type=DAGNodeType.wait, wait_for={"kind": "file", "path": "x"}),
            ],
            edges=[DAGEdgeSpec(from_node="g", to_node="w")],
        )
    )
    w = next(n for n in dag.nodes if n.name == "w")
    await world.store.update_node(w.id, wait_for={"kind": "shell", "cmd": "true"})
    await world.orch.start_dag(dag.id)
    await world.orch.tick()
    w = (await _nodes(world, dag.id))[1]["w"]
    assert w.status == "failed"
    assert w.error.startswith("wait_for is not valid")


async def test_a_rate_limit_backs_off_every_pr_wait_without_spending_attempts(world):
    seen: list[httpx.Request] = []

    def handler(request):
        seen.append(request)
        return httpx.Response(403, json={})

    _reader_for(world, handler, [])
    dag_id = await _wait_dag(world, {"kind": "pr", "number": 7, "until": "merged"})
    w_id = (await _nodes(world, dag_id))[1]["w"].id
    await world.orch.tick()
    w = (await _nodes(world, dag_id))[1]["w"]
    assert (w.status, w.check_attempts, len(seen)) == ("awaiting_check", 1, 1)
    until = world.orch._github_backoff_until
    assert until is not None and until > datetime.now(UTC) + timedelta(minutes=14)

    await _due(world, w_id)
    stamped = (await _nodes(world, dag_id))[1]["w"].last_check_at
    await world.orch.tick()
    w = (await _nodes(world, dag_id))[1]["w"]
    assert (w.check_attempts, w.last_check_at, len(seen)) == (1, stamped, 1)  # not asked, nothing spent

    world.orch._github_backoff_until = datetime.now(UTC) - timedelta(seconds=1)
    await world.orch.tick()
    assert len(seen) == 2


async def test_the_default_reader_is_short_and_named(world):
    world.orch._settings = world.cfg.model_copy(update={"github_token": "t0ken"})
    reader = world.orch._new_github_reader()
    try:
        assert reader._client.timeout.read == 5.0
        assert reader._client.headers["user-agent"] == "nous-dag-wait"
        assert reader._client.headers["authorization"] == "Bearer t0ken"
    finally:
        await reader.aclose()


async def test_an_evaluator_that_raises_still_spends_an_attempt(world, monkeypatch):
    async def boom(*args, **kwargs):
        raise RuntimeError("bug")

    monkeypatch.setattr(orchestrator_module, "evaluate_wait", boom)
    dag_id = await _wait_dag(world, {"kind": "file", "path": "x"})
    await world.orch.tick()
    w = (await _nodes(world, dag_id))[1]["w"]
    assert (w.status, w.check_attempts) == ("awaiting_check", 1)
    assert w.last_check_at is not None
