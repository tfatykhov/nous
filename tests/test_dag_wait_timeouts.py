"""Typed waits for lineage DAGs: a check or wait node's timeout covers its polls, a pr wait's poll floor, and a wait
node as the store writes it."""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock

import pytest
from sqlalchemy import delete

from nous.config import Settings
from nous.dag.schemas import DAGCreateRequest, DAGEdgeSpec, DAGNodeSpec, DAGNodeType
from nous.dag.store import DAGStore
from nous.storage.models import ExecutionDAG

JOB = "job-20261027-101500-ab12cd34"


def _store(**over) -> DAGStore:
    base = dict(_env_file=None, dag_node_default_timeout=120, dag_node_max_timeout=3600)
    base.update(over)
    return DAGStore(MagicMock(), "a", Settings(**base))


def _check(**over) -> DAGNodeSpec:
    return DAGNodeSpec(name="c", type=DAGNodeType.check, instructions="watch", **over)


def _wait(wait_for, **over) -> DAGNodeSpec:
    return DAGNodeSpec(name="w", type=DAGNodeType.wait, wait_for=wait_for, **over)


def _one(store: DAGStore, spec: DAGNodeSpec):
    resolved, problems, notes = store.resolve_node_timeouts([spec])
    return resolved[0], problems, notes


def test_a_check_nodes_timeout_is_raised_to_cover_its_polls():
    (timeout, _), problems, notes = _one(_store(), _check(completion_check_interval=60, max_check_attempts=10))
    assert timeout == 660  # 60 x 10 + 60
    assert problems == []
    assert len(notes) == 1
    assert "node 'c'" in notes[0] and "120" in notes[0] and "660" in notes[0]


@pytest.mark.parametrize("given", [900, 660])
def test_a_timeout_that_already_covers_the_polls_is_kept(given):
    (timeout, _), problems, notes = _one(
        _store(), _check(timeout_seconds=given, completion_check_interval=60, max_check_attempts=10)
    )
    assert (timeout, problems, notes) == (given, [], [])


def test_polls_that_need_more_than_the_max_timeout_are_refused():
    (timeout, _), problems, notes = _one(_store(), _check(completion_check_interval=600, max_check_attempts=10))
    assert len(problems) == 1
    assert "6600" in problems[0] and "3600" in problems[0] and "NOUS_DAG_NODE_MAX_TIMEOUT" in problems[0]
    assert timeout == 120 and notes == []


@pytest.mark.parametrize("over", [{"completion_check_interval": 60}, {"max_check_attempts": 10}])
def test_a_check_with_an_interval_or_attempts_alone_is_untouched(over):
    (timeout, _), problems, notes = _one(_store(), _check(**over))
    assert (timeout, problems, notes) == (120, [], [])


def test_a_file_wait_is_covered_too():
    (timeout, _), problems, notes = _one(
        _store(), _wait({"kind": "file", "path": "x"}, completion_check_interval=30, max_check_attempts=20)
    )
    assert (timeout, problems, len(notes)) == (630, [], 1)


def test_a_wait_without_attempts_or_interval_keeps_the_default():
    (timeout, stall), problems, notes = _one(_store(), _wait({"kind": "job", "job_id": JOB}))
    assert (timeout, stall, problems, notes) == (120, None, [], [])


def test_a_pr_wait_polls_at_most_once_a_minute_and_its_timeout_uses_that():
    (timeout, _), problems, notes = _one(
        _store(),
        _wait({"kind": "pr", "number": 7, "until": "merged"}, completion_check_interval=10, max_check_attempts=5),
    )
    assert timeout == 360  # 60 x 5 + 60
    assert problems == []
    assert any("poll interval raised from 10 to 60" in n for n in notes)
    assert any("360" in n and "timeout" in n for n in notes)
    assert len(notes) == 2


def test_a_pr_wait_with_no_interval_uses_the_floor_without_a_note_about_it():
    (timeout, _), problems, notes = _one(
        _store(), _wait({"kind": "pr", "number": 7, "until": "merged"}, max_check_attempts=3)
    )
    assert timeout == 240  # 60 x 3 + 60
    assert problems == []
    assert len(notes) == 1 and "poll interval" not in notes[0]


def test_a_subtask_with_a_completion_check_is_not_touched():
    spec = DAGNodeSpec(
        name="s",
        type=DAGNodeType.subtask,
        instructions="go",
        completion_check="test -f x",
        completion_check_interval=60,
        max_check_attempts=10,
    )
    (timeout, _), problems, notes = _one(_store(), spec)
    assert (timeout, problems, notes) == (120, [], [])


def test_a_wait_node_has_no_stall_timeout():
    store = _store(dag_stall_detection_enabled=True, dag_node_default_stall_timeout=60)
    (_, stall), _, _ = _one(store, _wait({"kind": "file", "path": "x"}))
    assert stall is None


def test_a_pr_wait_with_no_repo_and_no_default_is_refused_at_create():
    settings = Settings(_env_file=None, dag_node_default_timeout=120, dag_node_max_timeout=3600)
    store = DAGStore(MagicMock(), "a", settings.model_copy(update={"github_default_repo": ""}))
    _, problems, _ = _one(store, _wait({"kind": "pr", "number": 7, "until": "merged"}))
    assert problems == ["node 'w': pr wait needs repo (no NOUS_GITHUB_DEFAULT_REPO is set)"]
    # A repo in wait_for, or the default, is enough.
    _, problems, _ = _one(store, _wait({"kind": "pr", "number": 7, "until": "merged", "repo": "o/r"}))
    assert problems == []
    _, problems, _ = _one(_store(), _wait({"kind": "pr", "number": 7, "until": "merged"}))
    assert problems == []


def test_the_default_repo_setting_falls_back_on_a_bad_value():
    assert Settings(_env_file=None).github_default_repo == "tfatykhov/nous"
    assert Settings(_env_file=None, github_default_repo="me/mine").github_default_repo == "me/mine"
    assert Settings(_env_file=None, github_default_repo="../x").github_default_repo == "tfatykhov/nous"
    assert Settings(_env_file=None, github_default_repo="").github_default_repo == "tfatykhov/nous"


@pytest.mark.postgres_only
async def test_the_store_writes_a_wait_node(db):
    agent = f"test-wait-store-{uuid.uuid4().hex[:8]}"
    store = DAGStore(db, agent, Settings(_env_file=None, dag_node_default_timeout=120, dag_node_max_timeout=3600))
    try:
        dag = await store.create(
            DAGCreateRequest(
                name="d",
                nodes=[
                    _wait({"kind": "pr", "number": 7, "until": "merged"}, completion_check_interval=10),
                    DAGNodeSpec(name="s", type=DAGNodeType.subtask, instructions="go", completion_check_interval=10),
                ],
                edges=[DAGEdgeSpec(from_node="w", to_node="s")],
            )
        )
        fetched = await store.get_dag(dag.id)
        nodes = {n.name: n for n in fetched.nodes}
        wait = nodes["w"]
        assert wait.node_type == "wait"
        assert wait.wait_for == {"kind": "pr", "number": 7, "until": "merged"}
        assert wait.completion_check_interval == 60
        assert wait.status == "ready"
        assert wait.instructions is None
        # Any other node keeps the interval it asked for.
        assert nodes["s"].completion_check_interval == 10
        assert nodes["s"].wait_for is None
    finally:
        async with db.session() as s:
            await s.execute(delete(ExecutionDAG).where(ExecutionDAG.agent_id == agent))
            await s.commit()
