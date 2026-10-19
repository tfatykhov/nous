"""Task scoring: the pure part.

No database and no clock here (the caller passes ``now``). ``task_score_gatherer`` collects a
``TaskSignals`` record per finished task, ``score_leaf`` and ``score_rollup`` turn it into the row
stored in ``scoring.task_scores``. Every constant that moves a score lives in this file, and
``SCORER_VERSION`` must be bumped in the same commit as any change to one of them or to a mapping,
so old rows stay comparable with the code that wrote them.

The unit that is scored is the subtask (the leaf). A DAG run and an intention root are roll-ups:
they carry counts and no score, so no penalty is counted twice. See ``task_score_gatherer`` for the
rule that decides which unit a piece of evidence belongs to.

Trust tiers. A report that a task is done is Nous's own claim, so ``done_self`` earns nothing.
``done_verified`` needs a confirmation from a source the model cannot control: the owner's success
grade on a decision linked to the task, or (v4) a pull request Nous opened that a login other than
its own merged, read from GitHub by the scorer itself. Nothing else confirms. A check Nous wrote, run
by the harness, can only take a "done" away.

v2: a subtask's own DAG node check can only take a "done" away. Nous writes every ``completion_check``
itself, so one that passes confirms nothing and earns nothing (it is kept in the evidence, under
``node_check_passed``, for audits). One that the orchestrator ran and that said no is the harness
contradicting the model's report: a definite failure (exit 1) makes the subtask ``failed``, and a check
that never said yes before its timeout or attempt limit makes it ``partial``. So does being an earlier
attempt of a node that was retried (a node is retried only after it failed). The owner's success grade
outranks all of these.

v3: a reset archives the attempt it discards on the node (``dag_nodes.attempt_history``, migration 089),
so an earlier attempt is judged by how its own check ended, as the current attempt is. ``superseded``
remains for an attempt the archive does not show ending through its check.

v4: a DAG check node no longer verifies the DAG's subtasks: Nous writes its ``completion_check`` too, so
it is evidence (``dag_check_passed``) and nothing more. A node whose check is the PR check
(``python -m nous.dag.typed_checks pr ...``) passes once the PR exists; ``github_verify`` then reads the
PR and sets ``v2_verified`` when its author is the agent, it was opened after the DAG, and a non-agent
login merged it. While that cannot be decided yet (the PR is still open, or GitHub is unreachable) the
row is held provisional, for at most ``V2_HOLD_EXTRA_DAYS`` past the usual settle age.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta

SCORER_VERSION = 4

LEAF_KIND = "subtask"
ROLLUP_KINDS: tuple[str, ...] = ("dag", "intention")
TASK_KINDS: tuple[str, ...] = (LEAF_KIND, *ROLLUP_KINDS)
OUTCOMES: tuple[str, ...] = ("done_self", "done_verified", "partial", "failed", "not_scored")

# Base credit by outcome. Only a confirmed success earns anything; a failure the harness decided
# (timed out, errored, failed validation, no terminal report, a failed DAG, a failed report, an
# expired chain) costs.
OUTCOME_BASE: dict[str, float] = {"done_verified": 1.0, "done_self": 0.0, "partial": 0.0, "failed": -0.5}
OVERRIDE_PENALTY = 1.0  # per owner override: one cancels a full verified success
UNVERIFIED_CLAIM_PENALTY = 0.5  # per unverified claim
HARNESS_ERROR_PENALTY = 0.1  # per harness error ...
HARNESS_ERROR_CAP = 0.5  # ... at most this much in total
REFUSAL_PENALTY = 0.25  # per tool-policy refusal ...
REFUSAL_CAP = 0.5  # ... at most this much in total
SCORE_FLOOR = -1.0

SETTLE_DAYS = 14  # a score is settled once its task is older than this
# A pass re-scores one day more than that, so every row is rewritten once with settled=true.
SCORING_WINDOW_DAYS = SETTLE_DAYS + 1

# Owner grades, worst first. A unit keeps the worst grade among its decisions.
GRADE_ORDER: tuple[str, ...] = ("failure", "partial", "success")
EVIDENCE_IDS_CAP = 20  # ids kept per evidence list; the integer columns keep the full counts

# v2: how a DAG node's own completion_check ended, read from the error the orchestrator wrote when it failed the
# node (dag/orchestrator.py, _poll_awaiting_checks). "failed" is exit code 1; "unconfirmed" is a check that kept
# answering "still running" until the node's timeout or max_check_attempts. Any other node error is not a check.
NODE_CHECK_FAILED_PREFIX = "Completion check failed"
NODE_CHECK_UNCONFIRMED_PREFIXES: tuple[str, ...] = (
    "Completion check timed out after",
    "Completion check exceeded max attempts",
)
# "superseded": the subtask is an earlier attempt of a node that was retried, and the node's attempt_history does not
# show it ending through its check. A node is retried only after it failed, so the attempt's "done" was never
# confirmed (the gatherer decides this, not the node row).
NODE_CHECK_STATES: tuple[str, ...] = ("passed", "failed", "unconfirmed", "superseded")

# v4: the PR check, exactly as the CLI takes it (nous/dag/typed_checks.py). Only a completion_check that is this and
# nothing else counts: a pipe, an "|| true" or any extra argument makes it an ordinary Nous-written check.
PR_CHECK_RE = re.compile(
    r"python -m nous\.dag\.typed_checks pr ([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+) "
    r"(?:--pr ([1-9][0-9]{0,8})|--head ([A-Za-z0-9._/-]{1,200}))"
)
# A row whose PR cannot be decided yet (still open, GitHub unreachable) stays provisional this many days past
# SETTLE_DAYS, then settles with what it has.
V2_HOLD_EXTRA_DAYS = 7

_EXIT_CODE = re.compile(r"(?:^|; )exit code (-?\d+)")


@dataclass(frozen=True)
class PrCheck:
    """A node's PR check: the repository and either the PR number or the head branch that names it."""

    repo: str
    number: int | None = None
    head: str | None = None


def parse_pr_check(completion_check: str | None) -> PrCheck | None:
    """The PR check a ``completion_check`` is, when it is exactly ``PR_CHECK_RE`` (surrounding blanks aside)."""
    match = PR_CHECK_RE.fullmatch((completion_check or "").strip())
    if match is None:
        return None
    repo, number, head = match.groups()
    if any(part in (".", "..") for part in repo.split("/")):  # a path segment, not a repository
        return None
    return PrCheck(repo=repo, number=int(number) if number else None, head=head)


@dataclass(frozen=True)
class TaskSignals:
    """Everything the scorer reads about one finished task. ``status`` and ``detail`` are kind-specific.

    subtask: status = heart.subtasks.status, detail = final_outcome.
    dag: status = execution_dags.status.
    intention: status = brain.intentions.close_reason of the root.
    """

    task_kind: str
    task_id: str
    finished_at: datetime
    status: str | None
    detail: str | None = None
    dag_checked: bool = False  # its DAG completed and has a completed check node (v4: evidence, never verification)
    node_check: str | None = None  # v2: how its own DAG node's completion_check ended (NODE_CHECK_STATES), or None
    pr_check: PrCheck | None = None  # v4: its node's check is the PR check and it passed (current attempt)
    node_id: str | None = None  # v4: its DAG node, and the DAG's creation time: the order "one PR, one leaf" uses
    dag_created_at: datetime | None = None
    v2_verified: bool = False  # v4: github_verify found the PR merged by a non-agent login
    v2_hold: bool = False  # v4: the PR cannot be decided yet; keep the row provisional (bounded)
    owner_grade: str | None = None  # the worst a2ui grade among the decisions that belong to this unit
    owner_overrides: int = 0  # rejected proposals, undone ledger rows, decisions the owner graded failure
    unverified_claims: int = 0
    harness_errors: int = 0
    refusals: int = 0
    tokens: int | None = None
    child_ids: tuple[str, ...] = ()  # roll-ups: the subtasks under this unit
    evidence: dict[str, list[str]] = field(default_factory=dict)


@dataclass(frozen=True)
class TaskScore:
    """One row of scoring.task_scores, minus id, agent_id, first_scored_at and scored_at."""

    task_kind: str
    task_id: str
    finished_at: datetime
    scorer_version: int
    outcome: str
    not_scored_reason: str | None
    owner_grade: str | None
    owner_overrides: int
    unverified_claims: int
    harness_errors: int
    refusals: int
    tokens: int | None
    score: float | None
    children_scored: int | None
    children_mean_score: float | None
    settled: bool
    evidence: dict[str, list[str]]


def raw_outcome(task_kind: str, status: str | None, detail: str | None = None) -> str | None:
    """done | partial | failed | cancelled | legacy, or None for a task that has not finished (never scored)."""
    if task_kind == "subtask":
        if status == "cancelled":
            return "cancelled"
        if status not in ("completed", "failed"):
            return None
        if detail is not None:  # final_outcome, written by the hardened executor
            if detail == "completed":
                return "done"
            if detail == "incomplete_blocked":
                return "partial"
            return "cancelled" if detail == "cancelled" else "failed"
        return "done" if status == "completed" else "failed"
    if task_kind == "dag":
        return {"completed": "done", "partial": "partial", "failed": "failed", "cancelled": "cancelled"}.get(
            status or ""
        )
    if task_kind == "intention":
        return {
            "resolved": "done",
            "delivered": "done",
            "fallback_report": "partial",
            "failed_report": "failed",
            "expired": "failed",
            "cancelled": "cancelled",
            "legacy": "legacy",
        }.get(status or "")
    return None


def final_outcome(raw: str, *, verified: bool) -> tuple[str, str | None]:
    """(outcome, not_scored_reason). A cancel is never scored, whoever cancelled: "plans changed" cannot be told
    apart from "wrong", and nothing but result_summary text could attribute it, and that is not evidence."""
    if raw in ("cancelled", "legacy"):
        return "not_scored", raw
    if raw == "done":
        return ("done_verified" if verified else "done_self"), None
    return raw, None


def is_owner_decider(decided_by: str | None) -> bool:
    """A proposal's ``decided_by`` that is not the system ("telegram:<id>", "companion:<who>", "rest")."""
    return bool(decided_by) and not str(decided_by).strip().lower().startswith("system")


def classify_ledger_row(tool_name: str, status: str, result_summary: str | None) -> str | None:
    """``refusal`` for a blocked row, ``ledger_error`` for error or unknown, ``bash_exit`` for a bash call that exited
    non-zero, else None. The last two are both harness errors, kept apart so a later scorer can weigh them differently.

    The ledger stores a failing bash call as status ``success`` and keeps the exit code only in ``result_summary``
    ("exit code 1; 12 chars of output, not stored"), so that is read here. The ledger holds a bash call only when the
    whole command classified as a write (``ledger_store`` skips ``none``, and the classifier resolves every doubt to
    write), so a ``grep`` that finds nothing never gets here. A recorded write that exits non-zero counts, whatever it
    ran: v1 does not try to tell a failing test run from a failing write.
    """
    if status == "blocked":
        return "refusal"
    if status in ("error", "unknown"):
        return "ledger_error"
    if tool_name == "bash" and status == "success" and result_summary:
        match = _EXIT_CODE.search(result_summary)
        if match and int(match.group(1)) != 0:
            return "bash_exit"
    return None


def worst_grade(outcomes: Iterable[str]) -> str | None:
    """The worst of the owner's decision grades (failure, partial, success); other outcomes are not grades."""
    seen = {o for o in outcomes if o in GRADE_ORDER}
    return next((g for g in GRADE_ORDER if g in seen), None)


def _settled(finished_at: datetime, now: datetime) -> bool:
    return finished_at <= now - timedelta(days=SETTLE_DAYS)


def _held(signals: TaskSignals, now: datetime) -> bool:
    """A row whose PR cannot be decided yet stays provisional, until V2_HOLD_EXTRA_DAYS past the settle age."""
    return signals.v2_hold and now < signals.finished_at + timedelta(days=SETTLE_DAYS + V2_HOLD_EXTRA_DAYS)


def _row(signals: TaskSignals, outcome: str, reason: str | None, now: datetime, **over: object) -> TaskScore:
    fields: dict[str, object] = dict(
        task_kind=signals.task_kind,
        task_id=signals.task_id,
        finished_at=signals.finished_at,
        scorer_version=SCORER_VERSION,
        outcome=outcome,
        not_scored_reason=reason,
        owner_grade=signals.owner_grade,
        owner_overrides=signals.owner_overrides,
        unverified_claims=signals.unverified_claims,
        harness_errors=signals.harness_errors,
        refusals=signals.refusals,
        tokens=signals.tokens,
        score=None,
        children_scored=None,
        children_mean_score=None,
        settled=_settled(signals.finished_at, now) and not _held(signals, now),
        evidence={key: list(ids)[:EVIDENCE_IDS_CAP] for key, ids in signals.evidence.items()},
    )
    fields.update(over)
    return TaskScore(**fields)  # type: ignore[arg-type]


def node_check_state(
    completion_check: str | None,
    check_attempts: int | None,
    last_check_at: datetime | str | None,  # an archived attempt carries it as an ISO string
    node_status: str | None,
    node_error: str | None,
) -> str | None:
    """How a DAG node's own completion_check ended: ``passed``, ``failed``, ``unconfirmed``, or None.

    None unless the node carries a non-empty check that the orchestrator ran at least once, so a node whose check
    never got a poll (the worker never ran) says nothing either way. ``passed`` is a node that completed through its
    check; ``failed`` and ``unconfirmed`` come from the error it was failed with (``NODE_CHECK_FAILED_PREFIX``,
    ``NODE_CHECK_UNCONFIRMED_PREFIXES``), also once a fix node has skipped it: the skip keeps the error. Any other
    status or error is None. The ``failed`` text goes on with the check's own stderr, so nothing after the prefix is
    trusted to tell one kind of failure from another.
    """
    if not (completion_check or "").strip() or (check_attempts or 0) < 1 or last_check_at is None:
        return None
    if node_status == "completed":
        return "passed"
    if node_status not in ("failed", "skipped"):
        return None
    error = node_error or ""
    if error.startswith(NODE_CHECK_FAILED_PREFIX):
        return "failed"
    if error.startswith(NODE_CHECK_UNCONFIRMED_PREFIXES):
        return "unconfirmed"
    return None


def _outcome_of(signals: TaskSignals) -> tuple[str, str | None] | None:
    raw = raw_outcome(signals.task_kind, signals.status, signals.detail)
    if raw is None:
        return None
    # v4: only sources the model cannot control confirm. A DAG check node (dag_checked) is Nous-written: evidence only.
    confirmed = signals.owner_grade == "success" or signals.v2_verified
    if raw == "done" and not confirmed:
        # The orchestrator ran the subtask's own check and it did not say yes: the model's "done" does not stand.
        if signals.node_check == "failed":
            raw = "failed"
        elif signals.node_check in ("unconfirmed", "superseded"):
            raw = "partial"
    return final_outcome(raw, verified=confirmed)


def score_leaf(signals: TaskSignals, now: datetime) -> TaskScore | None:
    """The composite for a subtask, or None for a task that has not finished.

    A cancelled subtask gets a ``not_scored`` row (score NULL), so coverage can tell it from a missed one.
    """
    outcome = _outcome_of(signals)
    if outcome is None:
        return None
    name, reason = outcome
    if name == "not_scored":
        return _row(signals, name, reason, now)
    raw = (
        OUTCOME_BASE[name]
        - OVERRIDE_PENALTY * signals.owner_overrides
        - UNVERIFIED_CLAIM_PENALTY * signals.unverified_claims
        - min(HARNESS_ERROR_CAP, HARNESS_ERROR_PENALTY * signals.harness_errors)
        - min(REFUSAL_CAP, REFUSAL_PENALTY * signals.refusals)
    )
    return _row(signals, name, reason, now, score=round(max(SCORE_FLOOR, raw), 4))


def score_rollup(signals: TaskSignals, leaf_scores: Mapping[str, float | None], now: datetime) -> TaskScore | None:
    """The roll-up row of a DAG run or an intention root: counts only, score NULL.

    ``signals`` holds the penalties that belong to the unit itself (a DAG-only ledger row, an arrival note,
    a rejected proposal). The children's own penalties stay on the children. ``leaf_scores`` maps subtask id
    to its score: this pass's leaves, plus the stored rows of children that finished before the window (the job
    reads those). A child absent from both (never scored) or not scored is not counted.
    """
    outcome = _outcome_of(signals)
    if outcome is None:
        return None
    scores = [leaf_scores[c] for c in signals.child_ids if leaf_scores.get(c) is not None]
    mean = round(sum(scores) / len(scores), 4) if scores else None
    return _row(signals, outcome[0], outcome[1], now, children_scored=len(scores), children_mean_score=mean)
