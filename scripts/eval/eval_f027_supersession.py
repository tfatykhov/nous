"""F027 supersession-classifier evaluation.

Synthesizes labeled fact pairs from prod data and runs F027's classifier
to measure per-category accuracy and the confusion matrix.

Method:
1. Sample N active facts from heart.facts (agent_id=nous-prod-snapshot).
2. For each fact, ask Haiku to generate 4 synthetic counterparts, one per
   ground-truth category: CONTRADICTION, UPDATE, REFINEMENT, UNRELATED.
3. Run F027's _classify_fact_pair on each (original, synthetic) pair.
4. Score: per-category accuracy + confusion matrix + confidence stats.

Caveat: generator and classifier are both Haiku — this is a
self-consistency test, not a strict precision test. Useful for surfacing
calibration drift between generation and classification, not for
absolute precision claims. Use --judge-model sonnet to break the loop.

Usage:
    NOUS_EVAL_DB_NAME=nous_eval_scratch \
      uv run python scripts/eval/eval_f027_supersession.py
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from collections import defaultdict
from pathlib import Path

import asyncpg

from nous.api.anthropic_client import create_client
from nous.config import Settings
from nous.handlers import call_background_llm_structured
from nous.heart.facts import _SUPERSESSION_CLASSIFIER_PROMPT_TEMPLATE


_AGENT_ID = "nous-prod-snapshot"
_HAIKU_MODEL = "claude-haiku-4-5-20251001"

_CATEGORIES = ["CONTRADICTION", "UPDATE", "REFINEMENT", "UNRELATED"]


_GEN_PROMPTS = {
    "CONTRADICTION": (
        "Write a fact about the same subject that DIRECTLY CONTRADICTS the "
        "original. Pick one specific claim in the original and reverse it or "
        "assert the opposite. Same subject, contradictory content."
    ),
    "UPDATE": (
        "Write a fact about the same subject that REPLACES the original with "
        "newer information. Same subject, but the state has changed (a value "
        "moved, a status flipped, a number was updated). The new fact should "
        "be the current truth; the old one is now stale."
    ),
    "REFINEMENT": (
        "Write a fact about the same subject that ADDS DETAIL to the original "
        "without contradicting it. Same subject, compatible content, just more "
        "specific or with extra context."
    ),
    "UNRELATED": (
        "Write a fact whose surface wording is similar to the original (same "
        "key terms or phrasing) but talks about a DIFFERENT subject or "
        "different aspect, so it should not be classified as relating to the "
        "original at all."
    ),
}


_GEN_TEMPLATE = """You are generating a labeled test pair for a fact-classification eval.

Given the ORIGINAL fact below and a target category, write ONE alternative fact.

ORIGINAL fact: {content}

Target category: {category}
Generation rule: {rule}

Output requirements:
- Write only the fact text (1-3 sentences, 30-300 chars).
- Do not mention the category or use meta-language.
- Do not quote the original verbatim.

Return ONLY the fact text. No preamble, no explanation, no JSON wrapper."""


_CLASSIFIER_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "relation": {
            "type": "string",
            "enum": ["UPDATE", "CONTRADICTION", "REFINEMENT", "UNRELATED"],
        },
        "current_fact": {"type": "string", "enum": ["new", "old"]},
        "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
    },
    "required": ["relation", "current_fact", "confidence"],
}


async def _generate_pair(llm, content: str, category: str) -> str | None:
    payload = {
        "model": _HAIKU_MODEL,
        "max_tokens": 150,
        "temperature": 0,
        "system": "",
        "messages": [{
            "role": "user",
            "content": _GEN_TEMPLATE.format(
                content=content[:1200],
                category=category,
                rule=_GEN_PROMPTS[category],
            ),
        }],
    }
    try:
        response = await llm.call(payload)
    except Exception:
        logging.exception("gen failed for %s", category)
        return None
    text = ""
    for block in response.content:
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text", "").strip()
            break
    text = text.strip("\"'`")
    if not text or len(text) < 20 or len(text) > 600:
        return None
    return text


async def _classify(llm, model: str, old: str, new: str) -> dict | None:
    """Mirror nous/heart/facts.py::FactManager._classify_fact_pair.

    Imports the prod prompt template directly so the eval can never drift
    from the prompt actually used in production.
    """
    prompt = _SUPERSESSION_CLASSIFIER_PROMPT_TEMPLATE.format(
        old=old[:500],
        new=new[:500],
    )
    try:
        return await call_background_llm_structured(
            client=llm,
            model=model,
            system_prompt="You are a memory management classifier. Analyze fact relationships precisely.",
            user_message=prompt,
            tool_name="classify_facts",
            tool_description="Classify the relationship between two facts.",
            output_schema=_CLASSIFIER_SCHEMA,
            max_tokens=300,
        )
    except Exception:
        logging.exception("classify failed")
        return None


async def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--n-facts", type=int, default=30)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--judge-model", default=_HAIKU_MODEL,
                   help="Model for the F027 classifier (default Haiku — same as prod).")
    p.add_argument("--out", type=Path, default=Path("reports/f027_supersession_eval.md"))
    p.add_argument("--out-json", type=Path, default=Path("reports/f027_supersession_eval.json"))
    p.add_argument("--eval-host", default="127.0.0.1")
    p.add_argument("--eval-port", type=int, default=5433)
    p.add_argument("--eval-user", default="nous")
    p.add_argument("--eval-password", default="nous_eval")
    p.add_argument("--eval-db", default="nous_eval_scratch")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    logger = logging.getLogger(__name__)

    main_settings = Settings()
    if not (main_settings.anthropic_api_key or main_settings.anthropic_auth_token):
        print("ERROR: ANTHROPIC creds required.", file=sys.stderr)
        return 2

    conn = await asyncpg.connect(
        host=args.eval_host, port=args.eval_port,
        user=args.eval_user, password=args.eval_password,
        database=args.eval_db,
    )
    # Honor --seed so two runs with the same CLI parameters sample identical
    # facts. Postgres setseed takes [-1.0, 1.0]; modulo + scale keeps it
    # deterministic and in range for any positive int seed.
    pg_seed = (args.seed % 10000) / 10000.0
    await conn.execute("SELECT setseed($1)", pg_seed)
    logger.info("Postgres random() seeded with %.4f (from --seed=%d)",
                pg_seed, args.seed)

    llm = create_client(main_settings)
    await llm.start()

    # Per-category counters
    truth_pred_matrix: dict[tuple[str, str], int] = defaultdict(int)
    confidences: dict[str, list[float]] = defaultdict(list)
    pairs_for_report: list[dict] = []

    try:
        rows = await conn.fetch(
            """
            SELECT id, content, subject
            FROM heart.facts
            WHERE agent_id = $1 AND active = true
              AND length(content) >= 80 AND length(content) <= 600
              AND subject IS NOT NULL
            ORDER BY random()
            LIMIT $2
            """,
            _AGENT_ID, args.n_facts,
        )
        facts = list(rows)
        logger.info("Sampled %d facts; generating + classifying %d pairs",
                    len(facts), len(facts) * 4)

        for idx, fact in enumerate(facts, 1):
            for category in _CATEGORIES:
                synthetic = await _generate_pair(llm, fact["content"], category)
                if synthetic is None:
                    truth_pred_matrix[(category, "GEN_FAIL")] += 1
                    continue
                result = await _classify(llm, args.judge_model,
                                         fact["content"], synthetic)
                if result is None:
                    truth_pred_matrix[(category, "CLF_FAIL")] += 1
                    continue
                predicted = result.get("relation", "UNKNOWN")
                confidence = float(result.get("confidence", 0))
                truth_pred_matrix[(category, predicted)] += 1
                confidences[category].append(confidence)
                pairs_for_report.append({
                    "fact_id": str(fact["id"]),
                    "subject": fact["subject"],
                    "original": fact["content"][:200],
                    "synthetic": synthetic[:200],
                    "truth": category,
                    "predicted": predicted,
                    "current_fact": result.get("current_fact"),
                    "confidence": confidence,
                })
            if idx % 5 == 0:
                logger.info("  facts processed: %d/%d", idx, len(facts))
    finally:
        await llm.close()
        await conn.close()

    # ---- Score ----
    print()
    print("=" * 70)
    print(f"F027 SUPERSESSION CLASSIFIER EVAL")
    print(f"  facts={len(facts)} pairs={len(pairs_for_report)} judge={args.judge_model}")
    print("=" * 70)
    print()

    all_predictions = [pred for _, pred in {(t, p) for (t, p) in truth_pred_matrix}]
    pred_labels = sorted({p for (t, p) in truth_pred_matrix.keys()})

    # Per-category accuracy
    print("Per-category accuracy:")
    print(f"  {'truth':16s} {'n':>4s} {'correct':>8s} {'acc':>6s} {'avg_conf':>9s}")
    overall_correct = 0
    overall_n = 0
    for truth in _CATEGORIES:
        n_total = sum(c for (t, _), c in truth_pred_matrix.items() if t == truth)
        n_correct = truth_pred_matrix.get((truth, truth), 0)
        acc = n_correct / n_total if n_total else 0
        avg_conf = (sum(confidences[truth]) / len(confidences[truth])) if confidences[truth] else 0
        print(f"  {truth:16s} {n_total:>4d} {n_correct:>8d} {acc:>6.2%} {avg_conf:>9.2f}")
        overall_correct += n_correct
        overall_n += n_total
    overall_acc = overall_correct / overall_n if overall_n else 0
    print(f"  {'OVERALL':16s} {overall_n:>4d} {overall_correct:>8d} {overall_acc:>6.2%}")
    print()

    # Confusion matrix
    print(f"Confusion matrix (rows=truth, cols=predicted):")
    header = f"  {'':16s}" + "".join(f"{p:>14s}" for p in pred_labels)
    print(header)
    for truth in _CATEGORIES:
        row = f"  {truth:16s}"
        for pred in pred_labels:
            row += f"{truth_pred_matrix.get((truth, pred), 0):>14d}"
        print(row)
    print()

    # Most common error pattern per truth category
    print("Top confusions:")
    for truth in _CATEGORIES:
        errors = [(p, c) for (t, p), c in truth_pred_matrix.items()
                  if t == truth and p != truth and p not in {"GEN_FAIL", "CLF_FAIL"}]
        errors.sort(key=lambda x: -x[1])
        if errors:
            top = errors[0]
            print(f"  {truth:16s} most-confused-with: {top[0]} ({top[1]}x)")
    print("=" * 70)

    # Persist report
    args.out.parent.mkdir(parents=True, exist_ok=True)
    md_lines = [
        "# F027 Supersession Classifier Eval",
        "",
        f"- judge_model: `{args.judge_model}`",
        f"- facts sampled: {len(facts)}",
        f"- pairs scored: {len(pairs_for_report)}",
        f"- overall accuracy: **{overall_acc:.2%}**",
        "",
        "## Per-category accuracy",
        "",
        "| truth | n | correct | accuracy | avg_confidence |",
        "|---|---:|---:|---:|---:|",
    ]
    for truth in _CATEGORIES:
        n_total = sum(c for (t, _), c in truth_pred_matrix.items() if t == truth)
        n_correct = truth_pred_matrix.get((truth, truth), 0)
        acc = n_correct / n_total if n_total else 0
        avg_conf = (sum(confidences[truth]) / len(confidences[truth])) if confidences[truth] else 0
        md_lines.append(f"| {truth} | {n_total} | {n_correct} | {acc:.2%} | {avg_conf:.2f} |")
    md_lines.extend([
        "",
        "## Confusion matrix",
        "",
        "rows=truth, cols=predicted",
        "",
    ])
    md_lines.append("| truth \\ pred | " + " | ".join(pred_labels) + " |")
    md_lines.append("|---" * (len(pred_labels) + 1) + "|")
    for truth in _CATEGORIES:
        row = f"| {truth} | " + " | ".join(
            str(truth_pred_matrix.get((truth, p), 0)) for p in pred_labels
        ) + " |"
        md_lines.append(row)
    md_lines.extend([
        "",
        "## Caveat",
        "",
        "Generator and classifier are both `claude-haiku-4-5`. Agreement here "
        "is a self-consistency signal, not a strict precision test. Re-run with "
        "`--judge-model claude-sonnet-4-6` to use a stronger judge.",
        "",
    ])
    args.out.write_text("\n".join(md_lines), encoding="utf-8")
    args.out_json.write_text(json.dumps({
        "judge_model": args.judge_model,
        "n_facts": len(facts),
        "n_pairs": len(pairs_for_report),
        "overall_accuracy": overall_acc,
        "per_category": {
            t: {
                "n": sum(c for (tt, _), c in truth_pred_matrix.items() if tt == t),
                "correct": truth_pred_matrix.get((t, t), 0),
                "accuracy": (truth_pred_matrix.get((t, t), 0) /
                             max(1, sum(c for (tt, _), c in truth_pred_matrix.items() if tt == t))),
                "avg_confidence": (sum(confidences[t]) / len(confidences[t])) if confidences[t] else 0,
            }
            for t in _CATEGORIES
        },
        "confusion_matrix": {
            f"{t}->{p}": c for (t, p), c in truth_pred_matrix.items()
        },
        "pairs": pairs_for_report,
    }, indent=2), encoding="utf-8")
    print(f"\nWrote: {args.out}")
    print(f"Wrote: {args.out_json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
