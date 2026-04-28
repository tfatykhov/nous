"""F056 PR #1: admission control eval CLI.

Exercises `nous/heart/admission.py::AdmissionController` (line 135) via
`nous/heart/heart.py::Heart.learn` (line 282) against a labeled fixture
of 50 candidate facts (25 admit + 25 reject). Computes F1 of the
admit/reject classification.

Per F056 spec §A:
- `admission_shadow_mode=False` (defaults to True in production —
  without override the eval admits everything → useless gate).
- `admission_control_enabled=True`.
- Outcome derived from `isinstance(result, FactRejected)` — NOT
  `active=true`, which is overloaded with supersession.
- Per-handler agent_id `nous-eval-handler-admission`.
- Truncate happens BEFORE seed under advisory lock (clean slate).

Usage:
    python -m nous_eval.handlers.admission
    python -m nous_eval.handlers.admission --include-unreviewed
    python -m nous_eval.handlers.admission --fixture-path my-fixture.jsonl
"""
from __future__ import annotations

import argparse
import logging
from pathlib import Path

from sqlalchemy import text

from nous.config import Settings
from nous.heart.schemas import FactInput, FactRejected
from nous.storage.database import Database
from nous_eval.config import EvalSettings
from nous_eval.handlers._cli_base import (
    HandlerResult,
    _DeleteSpec,
    clear_handler_state,
    run_handler_eval,
)
from nous_eval.handlers._jsonl import load_jsonl
from nous_eval.handlers._models import AdmissionRow
from nous_eval.retrieval_runner import _build_heart_for_eval, _settings_for_eval_db

logger = logging.getLogger(__name__)


_AGENT_ID = "nous-eval-handler-admission"
_HANDLER_NAME = "admission"
_DEFAULT_FIXTURE = Path("tests/fixtures/handlers/admission_labeled.jsonl")


def _settings_with_admission_overrides(base: Settings) -> Settings:
    """Apply the F056 §A required overrides.

    `admission_shadow_mode` defaults to True in production
    (`nous/config.py:413`); without `False` the eval admits every fact and
    F1 collapses to whatever the label balance is — useless as a gate.
    """
    update: dict = {
        "admission_control_enabled": True,
        "admission_shadow_mode": False,
        "agent_id": _AGENT_ID,
    }
    return base.model_copy(update=update)


def filter_rows(rows: list[AdmissionRow], *, include_unreviewed: bool) -> list[AdmissionRow]:
    """Apply the reviewed_by gate. Mirrors qrels_loader.py:80-85 pattern."""
    if include_unreviewed:
        return rows
    return [r for r in rows if r.reviewed_by]


def compute_f1(tp: int, fp: int, fn: int) -> tuple[float, float, float]:
    """Return (precision, recall, F1) given confusion-matrix counts.

    Returns 0.0 for any metric when its denominator is 0 (matches sklearn).
    """
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    if precision + recall == 0:
        return 0.0, 0.0, 0.0
    f1 = 2 * precision * recall / (precision + recall)
    return precision, recall, f1


def _classify_outcome(label: str, learn_result) -> str:
    """Pure helper — given a label and Heart.learn return, return "admit" or "reject".

    Extracted from `_seed_and_score` so the outcome-derivation logic
    (F056 spec §A: `isinstance(result, FactRejected)` only, NOT
    `active=true`) can be unit-tested without a DB.
    """
    return "reject" if isinstance(learn_result, FactRejected) else "admit"


def _confusion_increment(cm: dict[str, int], label: str, outcome: str) -> None:
    """Mutate `cm` to increment the right tp/fp/tn/fn counter.

    `cm` keys: tp (correctly admitted), tn (correctly rejected),
    fp (incorrectly admitted), fn (incorrectly rejected).
    """
    if label == "admit" and outcome == "admit":
        cm["tp"] += 1
    elif label == "reject" and outcome == "reject":
        cm["tn"] += 1
    elif label == "reject" and outcome == "admit":
        cm["fp"] += 1
    else:  # label == "admit" and outcome == "reject"
        cm["fn"] += 1


async def _seed_and_score(
    rows: list[AdmissionRow], heart, eval_scoped: Settings,
) -> tuple[dict[str, int], list[tuple[str, str, str]]]:
    """Run admission for each row; return (confusion_matrix, per_row_results).

    `confusion_matrix` keys: tp, fp, tn, fn (tp = correctly admitted).
    `per_row_results` is a list of (row_id, label, outcome) for the report.
    """
    cm = {"tp": 0, "fp": 0, "tn": 0, "fn": 0}
    per_row: list[tuple[str, str, str]] = []

    for row in rows:
        try:
            result = await heart.learn(FactInput(
                content=row.content,
                subject=row.subject,
                category=row.category,
                source="admission_eval",
                source_text=row.source_text,
            ))
        except Exception:
            logger.exception("admission eval: Heart.learn raised on row_id=%s", row.row_id)
            continue

        outcome = _classify_outcome(row.label, result)
        _confusion_increment(cm, row.label, outcome)
        per_row.append((row.row_id, row.label, outcome))

    return cm, per_row


async def _run_admission_eval(
    args: argparse.Namespace,
    eval_settings: EvalSettings,
    main_settings: Settings,
) -> HandlerResult:
    fixture_path = args.fixture_path or _DEFAULT_FIXTURE
    rows = load_jsonl(fixture_path, AdmissionRow)
    rows = filter_rows(rows, include_unreviewed=args.include_unreviewed)
    rows.sort(key=lambda r: r.row_id)  # deterministic ordering

    if not rows:
        logger.error("admission eval: zero rows after reviewed_by filter")
        return HandlerResult(
            metrics={"admission_f1": 0.0, "admission_precision": 0.0, "admission_recall": 0.0},
            extras={"confusion_matrix": {"tp": 0, "fp": 0, "tn": 0, "fn": 0}},
            report_lines=["No rows passed the reviewed_by filter."],
            primary_metric="admission_f1",
            fixture_size=0,
        )

    overridden = _settings_with_admission_overrides(main_settings)
    # CAREFUL: _settings_for_eval_db's `update` dict picks up
    # `eval_settings.agent_id` (default "nous-eval-corpus") and would clobber
    # our handler-scoped agent_id from `_settings_with_admission_overrides`.
    # The third model_copy below restores it. If a future refactor drops
    # the third copy, writes silently land under "nous-eval-corpus" and
    # pollute the retrieval corpus — keep the comment + the restore.
    eval_scoped = _settings_for_eval_db(eval_settings, overridden)
    eval_scoped = eval_scoped.model_copy(update={"agent_id": _AGENT_ID})

    eval_db = Database(eval_scoped)
    try:
        await eval_db.connect()

        # Step 6 of lifecycle: TRUNCATE handler-scoped rows BEFORE seed.
        # Inside its own session under advisory lock; runs to completion +
        # commits before _build_heart_for_eval opens its own connections.
        # Admission rejection is runtime-only (no separate log table — see
        # sql/migrations/017_memory_admission_control.sql) so we only need
        # to clear admitted facts under our handler's agent_id.
        await clear_handler_state(
            eval_db, name=_HANDLER_NAME, agent_id=_AGENT_ID,
            deletes=[_DeleteSpec(schema_table="heart.facts", agent_id=_AGENT_ID)],
        )

        async with _build_heart_for_eval(eval_db, eval_scoped) as heart:
            cm, per_row = await _seed_and_score(rows, heart, eval_scoped)
    finally:
        await eval_db.disconnect()

    precision, recall, f1 = compute_f1(cm["tp"], cm["fp"], cm["fn"])

    n_unreviewed = sum(1 for r in rows if not r.reviewed_by)
    report_lines = [
        f"- fixture rows: {len(rows)} ({n_unreviewed} unreviewed)",
        f"- TP/FP/TN/FN: {cm['tp']}/{cm['fp']}/{cm['tn']}/{cm['fn']}",
        "",
        "### Per-row outcomes",
        "",
        "| row_id | label | outcome | match |",
        "|---|---|---|---|",
    ]
    for row_id, label, outcome in per_row:
        match = "ok" if label == outcome else "MISS"
        report_lines.append(f"| {row_id} | {label} | {outcome} | {match} |")

    return HandlerResult(
        metrics={
            "admission_f1": f1,
            "admission_precision": precision,
            "admission_recall": recall,
        },
        extras={"confusion_matrix": cm},
        report_lines=report_lines,
        primary_metric="admission_f1",
        fixture_size=len(rows),
        handler_specific_notes=(
            f"shadow_mode=False, threshold={args.threshold}, "
            f"include_unreviewed={args.include_unreviewed}"
        ),
    )


def main(argv: list[str] | None = None) -> int:
    return run_handler_eval(
        _HANDLER_NAME,
        _run_admission_eval,
        default_threshold=0.05,
        argv=argv,
    )


if __name__ == "__main__":
    raise SystemExit(main())
