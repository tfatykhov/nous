"""Task scoring v2: the pure scorer. No database."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import nous.dag.orchestrator as orchestrator
from nous.observability import task_scoring as ts
from nous.observability.task_scoring import TaskSignals, score_leaf, score_rollup

NOW = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)


def sig(kind="subtask", status="completed", detail=None, finished=None, **kw) -> TaskSignals:
    return TaskSignals(
        task_kind=kind,
        task_id="t1",
        finished_at=finished or NOW - timedelta(hours=1),
        status=status,
        detail=detail,
        **kw,
    )


def test_the_scorer_version_and_every_constant_are_pinned():
    # Bump SCORER_VERSION in the same commit as any constant or mapping below, then update this pin.
    assert ts.SCORER_VERSION == 3  # v3: an earlier attempt is judged by the node's archive of how it ended
    assert ts.OUTCOME_BASE == {"done_verified": 1.0, "done_self": 0.0, "partial": 0.0, "failed": -0.5}
    assert ts.NODE_CHECK_FAILED_PREFIX == "Completion check failed"
    assert ts.NODE_CHECK_UNCONFIRMED_PREFIXES == (
        "Completion check timed out after",
        "Completion check exceeded max attempts",
    )
    assert ts.NODE_CHECK_STATES == ("passed", "failed", "unconfirmed", "superseded")
    assert (ts.OVERRIDE_PENALTY, ts.UNVERIFIED_CLAIM_PENALTY) == (1.0, 0.5)
    assert (ts.HARNESS_ERROR_PENALTY, ts.HARNESS_ERROR_CAP) == (0.1, 0.5)
    assert (ts.REFUSAL_PENALTY, ts.REFUSAL_CAP, ts.SCORE_FLOOR) == (0.25, 0.5, -1.0)
    assert (ts.SETTLE_DAYS, ts.SCORING_WINDOW_DAYS) == (14, 15)
    assert ts.OUTCOMES == ("done_self", "done_verified", "partial", "failed", "not_scored")
    assert score_leaf(sig(), NOW).scorer_version == ts.SCORER_VERSION


@pytest.mark.parametrize(
    ("check", "attempts", "polled", "status", "error", "state"),
    [
        ("test -f done", 2, True, "completed", None, "passed"),
        ("test -f done", 1, True, "failed", "Completion check failed: no such file", "failed"),
        ("test -f done", 1, True, "failed", "Completion check failed (exit code 1)", "failed"),
        ("test -f done", 5, True, "failed", "Completion check timed out after 600s (5 attempts)", "unconfirmed"),
        ("test -f done", 40, True, "failed", "Completion check exceeded max attempts (40)", "unconfirmed"),
        # A fix node's skip_and_continue keeps the error: the failure must not disappear with it.
        ("test -f done", 1, True, "skipped", "Completion check failed: no such file", "failed"),
        ("test -f done", 5, True, "skipped", "Completion check timed out after 600s (5 attempts)", "unconfirmed"),
        ("test -f done", 1, True, "skipped", None, None),
        ("test -f done", 1, True, "failed", "Check disabled itself but its final run failed", None),  # not the check
        ("test -f done", 1, True, "failed", None, None),
        ("test -f done", 1, True, "cancelled", None, None),
        ("test -f done", 1, True, "awaiting_check", None, None),  # not finished
        ("test -f done", 0, False, "failed", "Completion check timed out after 600s (0 attempts)", None),  # never ran
        ("test -f done", 1, False, "completed", None, None),
        ("test -f done", 0, True, "completed", None, None),
        (None, 0, False, "completed", None, None),  # no check: the subtask completed the node
        ("   ", 3, True, "completed", None, None),
    ],
)
def test_node_check_state(check, attempts, polled, status, error, state):
    assert ts.node_check_state(check, attempts, NOW if polled else None, status, error) == state


def test_the_orchestrator_still_writes_the_node_errors_the_scorer_reads():
    # node_check_state parses these prefixes out of the error the orchestrator fails a node with. A reworded
    # message would silently turn every failed own check back into a self-reported "done".
    source = Path(orchestrator.__file__).read_text(encoding="utf-8")
    assert f'f"{ts.NODE_CHECK_FAILED_PREFIX}: ' in source  # exit 1 with stderr
    assert f'"{ts.NODE_CHECK_FAILED_PREFIX} (exit code 1)"' in source  # exit 1 without
    for prefix in ts.NODE_CHECK_UNCONFIRMED_PREFIXES:
        assert f'error=f"{prefix} ' in source


@pytest.mark.parametrize(
    ("kind", "status", "detail", "raw"),
    [
        ("subtask", "completed", "completed", "done"),
        ("subtask", "completed", "incomplete_blocked", "partial"),
        ("subtask", "completed", "incomplete_no_terminal", "failed"),
        ("subtask", "failed", "validation_failed", "failed"),
        ("subtask", "failed", "timed_out", "failed"),
        ("subtask", "failed", "errored", "failed"),
        ("subtask", "completed", None, "done"),  # a row from before the hardened executor
        ("subtask", "failed", None, "failed"),
        ("subtask", "cancelled", "cancelled", "cancelled"),
        ("subtask", "cancelled", None, "cancelled"),
        ("subtask", "completed", "cancelled", "cancelled"),
        ("subtask", "running", None, None),
        ("dag", "completed", None, "done"),
        ("dag", "partial", None, "partial"),
        ("dag", "failed", None, "failed"),
        ("dag", "cancelled", None, "cancelled"),
        ("dag", "running", None, None),
        ("intention", "resolved", None, "done"),
        ("intention", "delivered", None, "done"),
        ("intention", "fallback_report", None, "partial"),
        ("intention", "failed_report", None, "failed"),
        ("intention", "expired", None, "failed"),
        ("intention", "cancelled", None, "cancelled"),
        ("intention", "legacy", None, "legacy"),
        ("nonsense", "completed", None, None),
    ],
)
def test_raw_outcome_mapping(kind, status, detail, raw):
    assert ts.raw_outcome(kind, status, detail) == raw


@pytest.mark.parametrize(
    ("raw", "verified", "expected"),
    [
        ("done", False, ("done_self", None)),
        ("done", True, ("done_verified", None)),
        ("partial", True, ("partial", None)),  # a confirmation does not turn a partial into a success
        ("failed", True, ("failed", None)),
        ("cancelled", True, ("not_scored", "cancelled")),
        ("legacy", False, ("not_scored", "legacy")),
    ],
)
def test_final_outcome(raw, verified, expected):
    assert ts.final_outcome(raw, verified=verified) == expected


@pytest.mark.parametrize(
    ("signals", "outcome", "score"),
    [
        (sig(), "done_self", 0.0),  # Nous says it is done: that earns nothing
        (sig(owner_grade="success"), "done_verified", 1.0),  # the owner graded the decision a success
        (sig(dag_checked=True), "done_verified", 1.0),  # a completed check node in a completed DAG
        (sig(node_check="passed"), "done_self", 0.0),  # Nous wrote its own node's check: a pass confirms nothing
        (sig(node_check="failed"), "failed", -0.5),  # the orchestrator ran that check and it exited 1
        (sig(node_check="unconfirmed"), "partial", 0.0),  # the check never said yes before the node's deadline
        (sig(node_check="failed", owner_grade="success"), "done_verified", 1.0),  # the owner's grade outranks it
        (sig(node_check="unconfirmed", owner_grade="success"), "done_verified", 1.0),
        (sig(node_check="failed", dag_checked=True), "failed", -0.5),  # its own failed check beats a DAG check node
        (sig(node_check="unconfirmed", dag_checked=True), "partial", 0.0),
        (sig(node_check="superseded"), "partial", 0.0),  # an earlier attempt of a node that failed and was retried
        (sig(node_check="superseded", dag_checked=True), "partial", 0.0),  # the retry's DAG check does not credit it
        (sig(node_check="superseded", owner_grade="success"), "done_verified", 1.0),
        (sig(detail="incomplete_blocked", node_check="failed"), "partial", 0.0),  # only a "done" is taken away
        (sig(status="failed", detail="errored", node_check="unconfirmed"), "failed", -0.5),
        (sig(owner_grade="partial"), "done_self", 0.0),  # only a success grade confirms
        (sig(owner_grade="failure", owner_overrides=1), "done_self", -1.0),  # graded failure is an override
        (sig(detail="incomplete_blocked"), "partial", 0.0),
        (sig(status="failed", detail="timed_out"), "failed", -0.5),
        (sig(status="failed", detail="errored"), "failed", -0.5),
        (sig(status="failed", detail="validation_failed"), "failed", -0.5),
        (sig(status="failed", detail="errored", owner_grade="success"), "failed", -0.5),
        (sig(dag_checked=True, owner_overrides=1), "done_verified", 0.0),  # one override cancels a verified success
        (sig(owner_grade="success", owner_overrides=2), "done_verified", -1.0),  # floor
        (sig(dag_checked=True, unverified_claims=1), "done_verified", 0.5),
        (sig(dag_checked=True, unverified_claims=3), "done_verified", -0.5),
        (sig(dag_checked=True, harness_errors=1), "done_verified", 0.9),
        (sig(dag_checked=True, harness_errors=5), "done_verified", 0.5),  # at the cap
        (sig(dag_checked=True, harness_errors=50), "done_verified", 0.5),  # capped: -0.5 at most
        (sig(dag_checked=True, refusals=1), "done_verified", 0.75),
        (sig(dag_checked=True, refusals=2), "done_verified", 0.5),
        (sig(dag_checked=True, refusals=40), "done_verified", 0.5),  # capped: -0.5 at most
        (sig(dag_checked=True, harness_errors=50, refusals=40), "done_verified", 0.0),  # both caps stack
        (sig(status="failed", detail="errored", unverified_claims=1), "failed", -1.0),  # -0.5 - 0.5
        (sig(status="failed", detail="errored", owner_overrides=5, unverified_claims=5), "failed", -1.0),  # floor
        (sig(detail="incomplete_blocked", harness_errors=3, refusals=1), "partial", -0.55),
    ],
)
def test_leaf_composite(signals, outcome, score):
    row = score_leaf(signals, NOW)
    assert (row.outcome, row.score) == (outcome, pytest.approx(score))
    assert row.not_scored_reason is None
    assert row.children_scored is None and row.children_mean_score is None


def test_a_float_sum_is_rounded_not_left_as_noise():
    assert score_leaf(sig(owner_grade="success", harness_errors=3), NOW).score == 0.7  # 1 - 0.1*3 is 0.7000000000000001


@pytest.mark.parametrize("kind", ["subtask", "dag", "intention"])
def test_a_cancel_is_never_scored_whoever_cancelled_it(kind):
    status = {"subtask": "cancelled", "dag": "cancelled", "intention": "cancelled"}[kind]
    signals = sig(kind=kind, status=status, owner_overrides=1, harness_errors=2, unverified_claims=1)
    row = score_leaf(signals, NOW) if kind == "subtask" else score_rollup(signals, {}, NOW)
    assert (row.outcome, row.not_scored_reason, row.score) == ("not_scored", "cancelled", None)
    # the evidence counts are kept on the row, so a cancel with a real override is still visible
    assert (row.owner_overrides, row.harness_errors, row.unverified_claims) == (1, 2, 1)


def test_a_legacy_close_is_not_scored_and_a_running_task_has_no_row():
    row = score_rollup(sig(kind="intention", status="legacy"), {}, NOW)
    assert (row.outcome, row.not_scored_reason) == ("not_scored", "legacy")
    assert score_leaf(sig(status="running"), NOW) is None
    assert score_rollup(sig(kind="dag", status="running"), {}, NOW) is None


def test_a_rollup_has_counts_and_never_a_score():
    signals = sig(
        kind="dag",
        status="completed",
        dag_checked=True,
        harness_errors=2,
        refusals=1,
        owner_overrides=1,
        child_ids=("a", "b", "c", "gone"),
    )
    row = score_rollup(signals, {"a": 1.0, "b": -0.5, "c": None}, NOW)  # c is not scored, gone was not in this pass
    assert row.score is None
    assert (row.outcome, row.children_scored, row.children_mean_score) == ("done_verified", 2, 0.25)
    assert (row.harness_errors, row.refusals, row.owner_overrides) == (2, 1, 1)


def test_a_rollup_with_no_scored_children_has_no_mean():
    row = score_rollup(sig(kind="intention", status="delivered", child_ids=("a",)), {}, NOW)
    assert (row.children_scored, row.children_mean_score, row.outcome) == (0, None, "done_self")


def test_settled_means_older_than_fourteen_days():
    assert score_leaf(sig(finished=NOW - timedelta(days=14, minutes=1)), NOW).settled is True
    assert score_leaf(sig(finished=NOW - timedelta(days=13, hours=23)), NOW).settled is False


def test_the_raw_dimensions_pass_through_and_evidence_is_capped():
    row = score_leaf(
        sig(
            owner_grade="success",
            owner_overrides=1,
            unverified_claims=2,
            harness_errors=3,
            refusals=4,
            tokens=1234,
            evidence={"refusals": [f"id{i}" for i in range(30)]},
        ),
        NOW,
    )
    assert (row.owner_grade, row.owner_overrides, row.unverified_claims) == ("success", 1, 2)
    assert (row.harness_errors, row.refusals, row.tokens) == (3, 4, 1234)
    assert len(row.evidence["refusals"]) == ts.EVIDENCE_IDS_CAP


@pytest.mark.parametrize(
    ("tool", "status", "summary", "expected"),
    [
        ("bash", "success", "exit code 1; 12 chars of output, not stored", "bash_exit"),
        ("bash", "success", "exit code 127", "bash_exit"),
        ("bash", "success", "exit code -9; 3 chars of output, not stored", "bash_exit"),
        ("bash", "success", "exit code 0; 12 chars of output, not stored", None),
        ("bash", "success", "12 chars of output, not stored", None),
        ("bash", "success", "timed out after 30s; 5 chars of output", None),
        ("bash", "success", None, None),
        ("send_email", "success", "exit code 1", None),  # only bash carries an exit-code trailer
        ("bash", "error", "x", "ledger_error"),
        ("write_file", "unknown", None, "ledger_error"),
        ("send_email", "blocked", "refused by internal_only", "refusal"),
        ("bash", "pending", None, None),
    ],
)
def test_ledger_row_classes(tool, status, summary, expected):
    # a recorded bash call that exits non-zero is a harness error under its own name, whatever it ran
    assert ts.classify_ledger_row(tool, status, summary) == expected


@pytest.mark.parametrize(
    ("decided_by", "expected"),
    [
        ("telegram:42", True),
        ("companion:owner", True),
        ("rest", True),
        ("system", False),
        ("system:expiry", False),
        ("", False),
        (None, False),
    ],
)
def test_owner_decider(decided_by, expected):
    assert ts.is_owner_decider(decided_by) is expected


def test_worst_grade():
    assert ts.worst_grade(["success", "partial", "failure"]) == "failure"
    assert ts.worst_grade(["success", "partial"]) == "partial"
    assert ts.worst_grade(["success", "noise", "superseded", "pending"]) == "success"
    assert ts.worst_grade(["noise", "pending"]) is None
    assert ts.worst_grade([]) is None
