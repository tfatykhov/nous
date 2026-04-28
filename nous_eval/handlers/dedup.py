"""F056 PR #2: dedup eval CLI — measures both legs of fact dedup.

Two distinct dedup legs that both ship in production and both have
shipped bugs in the last quarter (F051.5 PR #364 over-dedup; #354 sibling
class):

- **Leg 1 (hybrid-search pre-check):** `nous/handlers/fact_extractor.py:243-248`
  — `_dedup_via_search` flag pre-checks `Heart.search_facts(content)` against
  `fact_dedup_threshold` before calling `Heart.learn`.
- **Leg 2 (native cosine):** `nous/heart/facts.py::FactManager._learn` lines
  329-334 — cosine `>0.95` near-duplicate detection inside `Heart.learn`.

The eval measures BOTH separately so a regression in either is attributable.
Both legs route through `FactExtractor.extract_and_store(candidate_facts=[...])`
which short-circuits LLM extraction when `candidate_facts` is non-empty
(`fact_extractor.py:127-130`); legs differ only in the `dedup_via_search`
constructor flag.

Per F056 spec §B:
- Per-handler agent_id `nous-eval-handler-dedup`.
- Both content fields must be >= 30 chars (Heart.learn `facts.py:312`
  rejects shorter — F038-1.2). Schema enforces this at fixture-load.
- Admission control DISABLED for the eval — otherwise a paraphrase that
  would have dedup'd might get admission-rejected first, masking the
  dedup signal. Spec §B "isolation" rationale.
"""
from __future__ import annotations

import argparse
import logging
from pathlib import Path

from nous.config import Settings
from nous.handlers.fact_extractor import FactExtractor
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
from nous_eval.handlers._models import DedupPair
from nous_eval.retrieval_runner import _build_heart_for_eval, _settings_for_eval_db

logger = logging.getLogger(__name__)


_AGENT_ID = "nous-eval-handler-dedup"
_HANDLER_NAME = "dedup"
_DEFAULT_FIXTURE = Path("tests/fixtures/handlers/dedup_paraphrases.jsonl")


def _settings_with_dedup_overrides(base: Settings) -> Settings:
    """Apply F056 §B required overrides.

    `admission_control_enabled=False` is critical: with admission on, a
    paraphrase that should dedup might get admission-rejected first
    (F023 admission gate at `facts.py:336-362` runs AFTER cosine dedup,
    so admission can't mask Leg 2 — but Leg 1's `search_facts` pre-check
    runs INSIDE FactExtractor BEFORE Heart.learn, so admission rejection
    would corrupt the per-leg attribution if a paraphrase reaches
    Heart.learn at all). Easiest correct stance: admission off for dedup
    eval, since we're measuring dedup in isolation.
    """
    update: dict = {
        "admission_control_enabled": False,
        "agent_id": _AGENT_ID,
    }
    return base.model_copy(update=update)


def filter_pairs(pairs: list[DedupPair], *, include_unreviewed: bool) -> list[DedupPair]:
    """Apply the reviewed_by gate. Mirrors qrels_loader.py:80-85 pattern."""
    if include_unreviewed:
        return pairs
    return [p for p in pairs if p.reviewed_by]


def compute_f1(tp: int, fp: int, fn: int) -> tuple[float, float, float]:
    """Return (precision, recall, F1). Mirrors handlers.admission.compute_f1.

    Duplicated rather than imported to keep handler modules independent
    and individually-testable. If a third copy lands in a future handler,
    extract to `_metrics.py`.
    """
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    if precision + recall == 0:
        return 0.0, 0.0, 0.0
    f1 = 2 * precision * recall / (precision + recall)
    return precision, recall, f1


def _classify_dedup_outcome(
    expected: str, returned_uuids: list, anchor_uuid,
) -> str:
    """Pure helper: given the FactExtractor return + anchor UUID, return
    "dedup" if dedup fired (anchor_uuid present in returned list), else
    "distinct".

    Production behavior: `_store_candidate_facts` (`fact_extractor.py:243-248`
    Leg 1; `:259` Leg 2 via Heart.learn → `_confirm`) appends the EXISTING
    fact's UUID when dedup fires. So `anchor_uuid in returned_uuids` is the
    dedup signal regardless of which leg fired.

    `expected` is unused here but kept in the signature so the helper can
    later return a bool comparison if needed. Currently the caller does
    the comparison.
    """
    _ = expected  # intentionally unused (see docstring)
    if anchor_uuid in returned_uuids:
        return "dedup"
    return "distinct"


def _confusion_increment(cm: dict[str, int], expected: str, outcome: str) -> None:
    """Increment confusion counter. tp = correct dedup, tn = correct distinct."""
    if expected == "dedup" and outcome == "dedup":
        cm["tp"] += 1
    elif expected == "distinct" and outcome == "distinct":
        cm["tn"] += 1
    elif expected == "distinct" and outcome == "dedup":
        cm["fp"] += 1
    else:  # expected == "dedup" and outcome == "distinct"
        cm["fn"] += 1


async def _run_one_leg(
    pairs: list[DedupPair], heart, settings: Settings, *, dedup_via_search: bool,
) -> dict[str, int]:
    """Run one leg (Leg 1 if dedup_via_search=True, else Leg 2).

    For each pair: insert anchor → call FactExtractor.extract_and_store
    with the paraphrase as a single candidate → check whether the returned
    UUID list contains the anchor's UUID (dedup signal).
    """
    extractor = FactExtractor(
        heart=heart,
        settings=settings,
        bus=None,
        llm_client=None,
        dedup_via_search=dedup_via_search,
    )
    cm = {"tp": 0, "fp": 0, "tn": 0, "fn": 0}

    for pair in pairs:
        # Insert anchor
        anchor_result = await heart.learn(FactInput(
            content=pair.anchor, source="dedup_eval",
        ))
        if isinstance(anchor_result, FactRejected):
            logger.warning(
                "dedup eval: anchor rejected (skipping pair %s): %s",
                pair.row_id, anchor_result.explanation,
            )
            continue
        anchor_uuid = anchor_result.id

        # Submit paraphrase via FactExtractor (short-circuits LLM extraction
        # when candidate_facts is non-empty — see fact_extractor.py:127-130).
        returned_uuids = await extractor.extract_and_store(
            summary={},
            episode_id=f"dedup-eval-{pair.row_id}",
            candidate_facts=[{
                "content": pair.paraphrase,
                "subject": "dedup-eval",
                "category": "technical",
            }],
        )
        outcome = _classify_dedup_outcome(pair.expected, returned_uuids, anchor_uuid)
        _confusion_increment(cm, pair.expected, outcome)

        # Clean up the anchor + any new fact so the next pair starts fresh
        # (otherwise pair N+1's paraphrase might dedup against pair N's
        # anchor by accident).
        try:
            from sqlalchemy import text
            async with heart.db.session() as session:
                await session.execute(
                    text("DELETE FROM heart.facts WHERE agent_id = :aid").bindparams(
                        aid=settings.agent_id,
                    )
                )
                await session.commit()
        except Exception:
            logger.exception("dedup eval: per-pair cleanup failed for %s", pair.row_id)

    return cm


async def _run_dedup_eval(
    args: argparse.Namespace,
    eval_settings: EvalSettings,
    main_settings: Settings,
) -> HandlerResult:
    fixture_path = args.fixture_path or _DEFAULT_FIXTURE
    pairs = load_jsonl(fixture_path, DedupPair)
    pairs = filter_pairs(pairs, include_unreviewed=args.include_unreviewed)
    pairs.sort(key=lambda p: p.row_id)

    if not pairs:
        logger.error("dedup eval: zero pairs after reviewed_by filter")
        return HandlerResult(
            metrics={"dedup_f1": 0.0, "dedup_f1_leg1": 0.0, "dedup_f1_leg2": 0.0},
            extras={"confusion_matrix_leg1": {"tp": 0, "fp": 0, "tn": 0, "fn": 0},
                    "confusion_matrix_leg2": {"tp": 0, "fp": 0, "tn": 0, "fn": 0}},
            report_lines=["No pairs passed the reviewed_by filter."],
            primary_metric="dedup_f1",
            fixture_size=0,
        )

    overridden = _settings_with_dedup_overrides(main_settings)
    # See admission.py for the agent_id clobber-restore rationale — same
    # pattern: _settings_for_eval_db picks up eval_settings.agent_id, which
    # would otherwise route writes to "nous-eval-corpus".
    eval_scoped = _settings_for_eval_db(eval_settings, overridden)
    eval_scoped = eval_scoped.model_copy(update={"agent_id": _AGENT_ID})

    eval_db = Database(eval_scoped)
    try:
        await eval_db.connect()

        # Lifecycle step 6: clean slate before seed under advisory lock.
        await clear_handler_state(
            eval_db, name=_HANDLER_NAME, agent_id=_AGENT_ID,
            deletes=[_DeleteSpec(schema_table="heart.facts", agent_id=_AGENT_ID)],
        )

        async with _build_heart_for_eval(eval_db, eval_scoped) as heart:
            cm_leg1 = await _run_one_leg(pairs, heart, eval_scoped, dedup_via_search=True)
            cm_leg2 = await _run_one_leg(pairs, heart, eval_scoped, dedup_via_search=False)
    finally:
        await eval_db.disconnect()

    p1, r1, f1_1 = compute_f1(cm_leg1["tp"], cm_leg1["fp"], cm_leg1["fn"])
    p2, r2, f1_2 = compute_f1(cm_leg2["tp"], cm_leg2["fp"], cm_leg2["fn"])
    f1_mean = (f1_1 + f1_2) / 2.0

    n_unreviewed = sum(1 for p in pairs if not p.reviewed_by)
    report_lines = [
        f"- fixture pairs: {len(pairs)} ({n_unreviewed} unreviewed)",
        "",
        "### Leg 1 (hybrid-search pre-check)",
        f"- TP/FP/TN/FN: {cm_leg1['tp']}/{cm_leg1['fp']}/{cm_leg1['tn']}/{cm_leg1['fn']}",
        f"- precision: {p1:.3f}, recall: {r1:.3f}, F1: {f1_1:.3f}",
        "",
        "### Leg 2 (native cosine in Heart.learn)",
        f"- TP/FP/TN/FN: {cm_leg2['tp']}/{cm_leg2['fp']}/{cm_leg2['tn']}/{cm_leg2['fn']}",
        f"- precision: {p2:.3f}, recall: {r2:.3f}, F1: {f1_2:.3f}",
        "",
        f"### Combined dedup_f1 = mean(leg1, leg2) = {f1_mean:.3f}",
    ]

    return HandlerResult(
        metrics={
            "dedup_f1": f1_mean,
            "dedup_f1_leg1": f1_1,
            "dedup_f1_leg2": f1_2,
        },
        extras={
            "confusion_matrix_leg1": cm_leg1,
            "confusion_matrix_leg2": cm_leg2,
            "leg1_precision": p1,
            "leg1_recall": r1,
            "leg2_precision": p2,
            "leg2_recall": r2,
        },
        report_lines=report_lines,
        primary_metric="dedup_f1",
        fixture_size=len(pairs),
        handler_specific_notes=(
            f"admission_off, fact_dedup_threshold={eval_scoped.fact_dedup_threshold}, "
            f"include_unreviewed={args.include_unreviewed}"
        ),
    )


def main(argv: list[str] | None = None) -> int:
    return run_handler_eval(
        _HANDLER_NAME,
        _run_dedup_eval,
        default_threshold=0.05,
        argv=argv,
    )


if __name__ == "__main__":
    raise SystemExit(main())
