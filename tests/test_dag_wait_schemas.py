"""Typed waits for lineage DAGs: the wait node's schema (wait_for kinds and what a wait node may carry)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from nous.dag.schemas import (
    PR_WAIT_POLL_FLOOR_SECONDS,
    DAGCreateRequest,
    DAGEdgeSpec,
    DAGNodeSpec,
    DAGNodeType,
    FileWait,
    JobWait,
    PrWait,
    effective_poll_interval,
    parse_wait_for,
)

JOB = "job-20261027-101500-ab12cd34"


def _wait(wait_for, **over) -> DAGNodeSpec:
    return DAGNodeSpec(name="w", type=DAGNodeType.wait, wait_for=wait_for, **over)


def test_a_job_wait_builds_and_defaults_to_finished():
    spec = _wait({"kind": "job", "job_id": JOB})
    assert isinstance(spec.wait_for, JobWait)
    assert spec.wait_for.until == "finished"


def test_a_pr_wait_builds():
    spec = _wait({"kind": "pr", "number": 7, "until": "merged"})
    assert isinstance(spec.wait_for, PrWait)
    assert (spec.wait_for.number, spec.wait_for.repo, spec.wait_for.head) == (7, None, None)


def test_a_file_wait_builds():
    spec = _wait({"kind": "file", "path": "out/report.md", "nonempty": True})
    assert isinstance(spec.wait_for, FileWait)
    assert spec.wait_for.nonempty is True


@pytest.mark.parametrize(
    "wait_for",
    [
        {"kind": "pr", "number": 7, "until": "head_changed", "head": "abc1234"},
        {"kind": "pr", "number": 7, "until": "head_changed", "head": "A" * 40},
        {"kind": "pr", "number": 7, "until": "checks_done", "head": "abc1234", "repo": "o/r"},
        {"kind": "pr", "number": 7, "until": "review_on_head", "reviewer": "tfatykhov"},
        {"kind": "pr", "number": 7, "until": "closed", "repo": "tfatykhov/nous"},
        {"kind": "job", "job_id": JOB, "until": "completed"},
        {"kind": "file", "path": "/tmp/nous-workspace/research/out.md"},
    ],
)
def test_valid_waits(wait_for):
    assert _wait(wait_for).wait_for is not None


@pytest.mark.parametrize(
    ("wait_for", "fragment"),
    [
        ({"kind": "job", "job_id": "job-1"}, "job_id"),
        ({"kind": "job", "job_id": "job-20261027-101500-ab12cd3"}, "job_id"),  # 7 hex
        ({"kind": "job", "job_id": "job-20261027-101500-AB12CD34"}, "job_id"),  # upper-case hex
        ({"kind": "job", "job_id": JOB + "x"}, "job_id"),  # trailing junk
        ({"kind": "job", "job_id": JOB + "\n"}, "job_id"),  # trailing newline
        ({"kind": "job", "job_id": JOB, "until": "running"}, "until"),
        ({"kind": "pr", "number": 7, "until": "head_changed"}, "head"),
        ({"kind": "pr", "number": 7, "until": "merged", "reviewer": "bob"}, "reviewer"),
        ({"kind": "pr", "number": 7, "until": "head_changed", "head": "zzz"}, "head"),
        ({"kind": "pr", "number": 7, "until": "head_changed", "head": "abc1234\n"}, "head"),
        ({"kind": "pr", "number": 7, "until": "head_changed", "head": "a" * 41}, "head"),
        ({"kind": "pr", "number": 7, "until": "merged", "repo": "a/../b"}, "repo"),
        ({"kind": "pr", "number": 7, "until": "merged", "repo": "a/.."}, "repo"),
        ({"kind": "pr", "number": 7, "until": "merged", "repo": "owner"}, "repo"),
        ({"kind": "pr", "number": 7, "until": "merged", "repo": "o/r\n"}, "repo"),
        ({"kind": "pr", "number": 7, "until": "review_on_head", "reviewer": "bob\n"}, "reviewer"),
        ({"kind": "pr", "number": 7, "until": "review_on_head", "reviewer": "-bob"}, "reviewer"),
        ({"kind": "pr", "number": 0, "until": "merged"}, "number"),
        ({"kind": "pr", "number": 7, "until": "open"}, "until"),
        ({"kind": "job", "job_id": JOB, "cmd": "x"}, "cmd"),  # extra keys are refused
        ({"kind": "shell", "cmd": "x"}, "kind"),
        ({"kind": "file", "path": "../x"}, "path"),
        ({"kind": "file", "path": "a/../b"}, "path"),
        ({"kind": "file", "path": ".git/config"}, "path"),
        ({"kind": "file", "path": "a/.hidden"}, "path"),
        ({"kind": "file", "path": "/ws/.x/y"}, "path"),
        ({"kind": "file", "path": "a\x00b"}, "path"),
        ({"kind": "file", "path": "a\nb"}, "path"),
        ({"kind": "file", "path": ""}, "path"),
        ({"kind": "file", "path": "x" * 301}, "path"),
    ],
)
def test_invalid_waits_are_refused(wait_for, fragment):
    with pytest.raises(ValidationError) as exc:
        _wait(wait_for)
    assert fragment in str(exc.value)


def test_a_wait_node_needs_wait_for():
    with pytest.raises(ValidationError, match="needs wait_for"):
        DAGNodeSpec(name="w", type=DAGNodeType.wait)


def test_wait_for_is_only_for_wait_nodes():
    with pytest.raises(ValidationError, match="wait_for is allowed only on wait nodes"):
        DAGNodeSpec(name="s", type=DAGNodeType.subtask, instructions="x", wait_for={"kind": "job", "job_id": JOB})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("instructions", "do x"),
        ("tools", ["bash"]),
        ("model", "m"),
        ("frame_type", "f"),
        ("completion_check", "true"),
        ("completion_condition", "c"),
        ("stall_timeout_seconds", 30),
        ("parent_node", "p"),
        ("fix_actions", ["retry_as_is"]),
        ("undoable", True),
        ("options", [{"id": "a", "label": "A", "outcome": "proceed"}, {"id": "b", "label": "B", "outcome": "stop"}]),
    ],
)
def test_a_wait_node_runs_no_model(field, value):
    with pytest.raises(ValidationError) as exc:
        _wait({"kind": "file", "path": "x"}, **{field: value})
    assert field in str(exc.value)


def test_a_wait_node_takes_empty_values_and_its_own_fields():
    spec = _wait(
        {"kind": "file", "path": "x"},
        instructions="",
        tools=[],
        description="wait for the report",
        timeout_seconds=600,
        completion_check_interval=30,
        max_check_attempts=5,
    )
    assert (spec.timeout_seconds, spec.completion_check_interval, spec.max_check_attempts) == (600, 30, 5)


def test_parse_wait_for_returns_the_kind_model():
    assert isinstance(parse_wait_for({"kind": "pr", "number": 3, "until": "closed"}), PrWait)
    with pytest.raises(ValidationError):
        parse_wait_for({"kind": "job"})
    with pytest.raises(ValidationError):
        parse_wait_for("junk")


@pytest.mark.parametrize(
    ("kind", "interval", "expected"),
    [("pr", None, 60), ("pr", 10, 60), ("pr", 300, 300), ("job", 5, 5), ("file", None, None), (None, 30, 30)],
)
def test_effective_poll_interval(kind, interval, expected):
    assert PR_WAIT_POLL_FLOOR_SECONDS == 60
    assert effective_poll_interval(kind, interval) == expected


def test_a_wait_feeds_a_subtask_in_the_next_wave():
    request = DAGCreateRequest(
        name="d",
        nodes=[
            _wait({"kind": "job", "job_id": JOB}),
            DAGNodeSpec(name="s", type=DAGNodeType.subtask, instructions="go"),
        ],
        edges=[DAGEdgeSpec(from_node="w", to_node="s")],
    )
    assert request.compute_waves() == {"w": 0, "s": 1}


def _below_a_proceed_default(node: DAGNodeSpec) -> DAGCreateRequest:
    return DAGCreateRequest(
        name="d",
        nodes=[
            DAGNodeSpec(
                name="a",
                type=DAGNodeType.approval,
                instructions="Go?",
                options=[
                    {"id": "go", "label": "Go", "outcome": "proceed"},
                    {"id": "no", "label": "No", "outcome": "stop"},
                ],
                default_option="go",
            ),
            node,
        ],
        edges=[DAGEdgeSpec(from_node="a", to_node=node.name, edge_type="context_flow")],
    )


def test_a_wait_below_a_proceed_default_approval_need_not_be_undoable(monkeypatch):
    # The validator builds a real Settings(), so the flag and its prerequisites come from the environment.
    for name in (
        "NOUS_DAG_APPROVAL_PROCEED_DEFAULT_ENABLED",
        "NOUS_COMPENSATION_ENABLED",
        "NOUS_COMPENSATION_AUTO_REVIEW_ENABLED",
        "NOUS_EXECUTION_LEDGER_PERSIST_ENABLED",
        "NOUS_A2UI_ENABLED",
    ):
        monkeypatch.setenv(name, "true")
    # Armed: an acting node in the same place is refused.
    with pytest.raises(ValidationError, match="not declared undoable"):
        _below_a_proceed_default(DAGNodeSpec(name="w", type=DAGNodeType.subtask, instructions="send"))
    assert _below_a_proceed_default(_wait({"kind": "file", "path": "x"})).compute_waves() == {"a": 0, "w": 1}
