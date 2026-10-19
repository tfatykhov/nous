"""Task scoring v2: the pure scorer. No database."""

from __future__ import annotations

from dataclasses import replace
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
    assert ts.SCORER_VERSION == 5  # v5: sampled owner audits; a trusted check kind earns done_checked
    assert ts.V2_HOLD_EXTRA_DAYS == 7
    assert ts.PR_CHECK_RE.pattern == (
        r"python -m nous\.dag\.typed_checks pr ([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+) "
        r"(?:--pr ([1-9][0-9]{0,8})|--head ([A-Za-z0-9._/-]{1,200}))"
    )
    assert ts.OUTCOME_BASE == {
        "done_verified": 1.0,
        "done_checked": 0.25,
        "done_self": 0.0,
        "partial": 0.0,
        "failed": -0.5,
    }
    assert (ts.CHECK_KIND_PR, ts.CHECK_KIND_OTHER) == ("pr", "other")
    assert [(kind, pattern.pattern) for kind, pattern in ts.CHECK_KIND_PATTERNS] == [
        ("job_status", r"claude-jobs|JOB_?ID"),
        ("pr_state", r"\bgh pr\b|check_pr"),
        ("file_exists", r"\btest -[efs]\b|\[ -[efs] |ALL_DONE"),
        ("script", r"^\s*(bash|sh|python3?)\s+\S+\.(sh|py)\b"),
    ]
    assert ts.CHECK_KINDS == ("pr", "job_status", "pr_state", "file_exists", "script", "other")
    assert ts.AUDIT_VERDICTS == ("right", "wrong")
    assert ts.AUDIT_TRUST_WINDOW == 5
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
    assert ts.OUTCOMES == ("done_self", "done_verified", "done_checked", "partial", "failed", "not_scored")
    assert set(ts.OUTCOME_BASE) == set(ts.OUTCOMES) - {"not_scored"}
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
    ("raw", "verified", "checked", "expected"),
    [
        ("done", False, False, ("done_self", None)),
        ("done", True, False, ("done_verified", None)),
        ("done", False, True, ("done_checked", None)),  # v5
        ("done", True, True, ("done_verified", None)),  # verified outranks checked
        ("partial", True, True, ("partial", None)),  # a confirmation does not turn a partial into a success
        ("failed", True, True, ("failed", None)),
        ("cancelled", True, True, ("not_scored", "cancelled")),
        ("legacy", False, False, ("not_scored", "legacy")),
    ],
)
def test_final_outcome(raw, verified, checked, expected):
    assert ts.final_outcome(raw, verified=verified, checked=checked) == expected


@pytest.mark.parametrize(
    ("signals", "outcome", "score"),
    [
        (sig(), "done_self", 0.0),  # Nous says it is done: that earns nothing
        (sig(owner_grade="success"), "done_verified", 1.0),  # the owner graded the decision a success
        (sig(dag_checked=True), "done_self", 0.0),  # v4: a DAG check node is Nous-written, so evidence only
        (sig(v2_verified=True), "done_verified", 1.0),  # v4: a PR Nous opened, merged by another login
        (sig(v2_verified=True, node_check="failed"), "done_verified", 1.0),  # outranks its own check, as a grade
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
        (sig(v2_verified=True, owner_overrides=1), "done_verified", 0.0),  # one override cancels a verified success
        (sig(owner_grade="success", owner_overrides=2), "done_verified", -1.0),  # floor
        (sig(v2_verified=True, unverified_claims=1), "done_verified", 0.5),
        (sig(v2_verified=True, unverified_claims=3), "done_verified", -0.5),
        (sig(v2_verified=True, harness_errors=1), "done_verified", 0.9),
        (sig(v2_verified=True, harness_errors=5), "done_verified", 0.5),  # at the cap
        (sig(v2_verified=True, harness_errors=50), "done_verified", 0.5),  # capped: -0.5 at most
        (sig(v2_verified=True, refusals=1), "done_verified", 0.75),
        (sig(v2_verified=True, refusals=2), "done_verified", 0.5),
        (sig(v2_verified=True, refusals=40), "done_verified", 0.5),  # capped: -0.5 at most
        (sig(v2_verified=True, harness_errors=50, refusals=40), "done_verified", 0.0),  # both caps stack
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
        dag_checked=True,  # v4: evidence only, so the roll-up is done_self
        harness_errors=2,
        refusals=1,
        owner_overrides=1,
        child_ids=("a", "b", "c", "gone"),
    )
    row = score_rollup(signals, {"a": 1.0, "b": -0.5, "c": None}, NOW)  # c is not scored, gone was not in this pass
    assert row.score is None
    assert (row.outcome, row.children_scored, row.children_mean_score) == ("done_self", 2, 0.25)
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


def test_a_rollup_stays_provisional_while_a_child_it_counts_is():
    # A DAG old enough to settle, whose child is held (its PR still open): the roll-up's mean would go stale.
    old = sig(kind="dag", status="completed", finished=NOW - timedelta(days=ts.SETTLE_DAYS + 1), child_ids=("a", "b"))
    assert score_rollup(old, {"a": 0.0, "b": 1.0}, NOW).settled is True
    assert score_rollup(old, {"a": 0.0, "b": 1.0}, NOW, frozenset({"b"})).settled is False
    assert score_rollup(old, {"a": 0.0, "b": 1.0}, NOW, frozenset({"other"})).settled is True


# v5: check kinds, the owner's audits, done_checked


@pytest.mark.parametrize(
    ("command", "kind"),
    [
        ("python -m nous.dag.typed_checks pr tfatykhov/nous --pr 12", "pr"),
        ("  python -m nous.dag.typed_checks pr tfatykhov/nous --head fix/x  ", "pr"),  # surrounding blanks aside
        ("python -m nous.dag.typed_checks pr tfatykhov/nous --pr 12 || true", "other"),  # not the PR check any more
        ("cat /tmp/claude-jobs/abc/status | grep done", "job_status"),
        ('test -n "$JOB_ID"', "job_status"),
        ("echo $JOBID", "job_status"),
        ("gh pr view 12 --json state", "pr_state"),
        ("python check_pr.py 12", "pr_state"),  # check_pr comes before the script pattern
        ("test -f /tmp/report.md", "file_exists"),
        ("test -s out.json", "file_exists"),
        ("[ -e /tmp/done ] && echo ok", "file_exists"),
        ("grep -q ALL_DONE log.txt", "file_exists"),
        ("bash scripts/verify.sh", "script"),
        ("  python3 tools/check.py --strict", "script"),
        ("sh run.sh", "script"),
        ("test -f /tmp/claude-jobs/x/done", "job_status"),  # first match wins: job_status before file_exists
        ("gh pr checks 12 && test -f done", "pr_state"),  # pr_state before file_exists
        ("bash check_pr.sh", "pr_state"),  # pr_state before script
        ("pytest -q tests/test_x.py", "other"),
        ("testing -f x", "other"),  # \btest -f\b only
        ("", "other"),
        (None, "other"),
    ],
)
def test_check_kind(command, kind):
    assert ts.check_kind(command) == kind
    assert kind in ts.CHECK_KINDS


def _answers(kind: str, verdicts: str, start: int = 0) -> list[tuple[str, str, datetime, str]]:
    """``verdicts`` oldest first, one letter each (r right, w wrong), an hour apart."""
    names = {"r": "right", "w": "wrong"}
    return [
        (kind, names[v], NOW - timedelta(days=30) + timedelta(hours=start + i), f"{kind}-{start + i:03d}")
        for i, v in enumerate(verdicts)
    ]


@pytest.mark.parametrize(
    ("verdicts", "trusted"),
    [
        ("rrrrr", True),  # exactly five, all right
        ("rrrr", False),  # four is not enough
        ("", False),  # nobody answered: absence is not a verdict
        ("wrrrrr", True),  # the wrong is older than the latest five
        ("rrrrrw", False),  # one wrong among the latest five takes the trust away
        ("rrwrrrr", False),  # still among the latest five
        ("rrwrrrrr", True),  # five consecutive rights restore it
        ("rrrrrrrrrrrr", True),
    ],
)
def test_a_kind_is_trusted_only_when_its_latest_five_answers_are_all_right(verdicts, trusted):
    assert (ts.trusted_kinds(_answers("file_exists", verdicts)) == {"file_exists"}) is trusted


def test_trust_is_by_answered_at_per_kind_not_by_input_order():
    rows = _answers("script", "wrrrrr") + _answers("other", "rrrrrw")
    rows.reverse()  # newest first in, and kinds interleaved: the order the rows arrive in does not matter
    assert ts.trusted_kinds(rows) == {"script"}
    # the same answered_at: the audit id breaks the tie, so the result does not depend on the database's row order
    tied = [("job_status", v, NOW, f"id-{i}") for i, v in enumerate(["wrong", *["right"] * 5])]
    assert ts.trusted_kinds(tied) == {"job_status"}
    assert ts.trusted_kinds(list(reversed(tied))) == {"job_status"}


CHECKED = dict(node_check="passed", check_kind="file_exists", kind_trusted=True)


@pytest.mark.parametrize(
    ("signals", "outcome", "score"),
    [
        (sig(**CHECKED), "done_checked", 0.25),  # raw done, its own check passed, the kind is trusted
        (sig(**{**CHECKED, "kind_trusted": False}), "done_self", 0.0),  # an untrusted kind earns nothing
        (sig(**{**CHECKED, "check_kind": "pr"}), "done_self", 0.0),  # a PR check is V2: never done_checked
        (sig(**{**CHECKED, "check_kind": None}), "done_self", 0.0),
        (sig(**{**CHECKED, "node_check": None}), "done_self", 0.0),  # no check ran
        (sig(**{**CHECKED, "node_check": "failed"}), "failed", -0.5),  # its check said no
        (sig(**{**CHECKED, "node_check": "superseded"}), "partial", 0.0),
        (sig(**CHECKED, detail="incomplete_blocked"), "partial", 0.0),  # only a "done" is credited
        (sig(**CHECKED, status="failed", detail="errored"), "failed", -0.5),
        (sig(**CHECKED, owner_grade="success"), "done_verified", 1.0),  # verified outranks checked
        (sig(**CHECKED, harness_errors=1), "done_checked", 0.15),  # the usual penalties apply
        # the owner's "right" on this leaf's audit is a success grade: verified, over a failed check of its own too
        (sig(audit_verdict="right"), "done_verified", 1.0),
        (sig(audit_verdict="right", node_check="failed"), "done_verified", 1.0),
        (sig(audit_verdict="right", node_check="unconfirmed"), "done_verified", 1.0),
        (sig(**CHECKED, audit_verdict="right"), "done_verified", 1.0),
        # "wrong" is a failure grade: the gatherer counts it as an override, and it is never done_checked
        (sig(**CHECKED, audit_verdict="wrong", owner_overrides=1), "done_self", -1.0),
        (sig(audit_verdict="wrong", owner_overrides=1), "done_self", -1.0),
        (sig(audit_verdict="wrong", owner_overrides=1, node_check="failed"), "failed", -1.0),  # floor
        (sig(**CHECKED, audit_pending=True), "done_checked", 0.25),  # unanswered: no verdict yet
    ],
)
def test_v5_leaf_outcomes(signals, outcome, score):
    row = score_leaf(signals, NOW)
    assert (row.outcome, row.score) == (outcome, pytest.approx(score))


def test_a_pending_audit_holds_the_row_provisional_within_the_bound_and_then_it_settles():
    bound = timedelta(days=ts.SETTLE_DAYS + ts.V2_HOLD_EXTRA_DAYS)
    held = sig(audit_pending=True, finished=NOW - bound + timedelta(minutes=1))
    assert score_leaf(held, NOW).settled is False  # older than SETTLE_DAYS, still waiting for the owner
    assert score_leaf(replace(held, audit_pending=False), NOW).settled is True
    late = sig(audit_pending=True, finished=NOW - bound)
    assert score_leaf(late, NOW).settled is True  # the bound passed: it settles with what it has
    young = sig(audit_pending=True, finished=NOW - timedelta(days=1))
    assert score_leaf(young, NOW).settled is False
    # one bound for both waits: an undecided PR is held exactly as long
    assert score_leaf(replace(held, audit_pending=False, v2_hold=True), NOW).settled is False


def test_a_rollup_mean_counts_the_done_checked_leaves():
    leaf = score_leaf(sig(**CHECKED), NOW)
    row = score_rollup(sig(kind="dag", status="completed", child_ids=("a", "b")), {"a": leaf.score, "b": 0.0}, NOW)
    assert (row.children_scored, row.children_mean_score) == (2, 0.125)
