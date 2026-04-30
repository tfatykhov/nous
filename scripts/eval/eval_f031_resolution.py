"""F031 sleep-cycle contradiction-resolution synthetic eval.

Background:
    Prod sleep cycle (2026-04-30) reported "Contradiction resolution: 10
    found, 0 resolved." That's the F031 phase in `sleep_handler.py`,
    NOT the F027 classifier in `heart/facts.py` (different prompt,
    different schema, different code path).

    Two suspect mechanisms:
    1. F031 prompt is bare (6-line action list, no decision tree, no
       examples) — LLM may return low-confidence verdicts on real cases.
    2. Hard 0.7 floor at line 668 downgrades any non-KEEP_BOTH action
       to KEEP_BOTH when confidence < 0.7. Sonnet returning 0.65 on
       a genuine SUPERSEDE silently becomes a no-op.

This script measures both:
    - Per-action accuracy on synthetic ground truth (6 categories × 5
      pairs = 30 total)
    - Confidence distribution by category
    - Downgrade rate (how many would-be resolutions got downgraded to
      KEEP_BOTH because confidence < 0.7)

Cost: ~$0.50 in Haiku/Sonnet (30 gen + 30 classify calls).

Usage:
    NOUS_EVAL_DB_NAME=nous_eval_scratch \
      uv run python scripts/eval/eval_f031_resolution.py
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from collections import defaultdict
from pathlib import Path

from nous.api.anthropic_client import create_client
from nous.config import Settings
from nous.handlers import call_background_llm_structured
from nous.handlers.sleep_handler import (
    _CONTRADICTION_RESOLUTION_PROMPT,
    _CONTRADICTION_RESOLUTION_SCHEMA,
)


_HAIKU_MODEL = "claude-haiku-4-5-20251001"
_DEFAULT_RESOLVER_MODEL = "claude-sonnet-4-6"  # matches background_model

# F031 floor — anything below gets downgraded to KEEP_BOTH at line 668.
_F031_CONFIDENCE_FLOOR = 0.7

_CATEGORIES = [
    "SUPERSEDE_A",
    "SUPERSEDE_B",
    "MERGE",
    "KEEP_BOTH",
    "REMOVE_A",
    "REMOVE_B",
]


# Generation prompts — produce a paired fact (B) given a seed (A) such that
# the labeled action is the right answer.
_GEN_PROMPTS: dict[str, str] = {
    "SUPERSEDE_A": (
        "Write a fact B about the same subject and property as A, but "
        "describing a NEWER state. The property is mutable (status, "
        "schedule, value, version, location) and changed over time. "
        "Both facts were true at different points; B is current. "
        "Do not contradict — B simply replaces A."
    ),
    "SUPERSEDE_B": (
        "Imagine that fact A was generated AFTER an earlier truth. Write "
        "fact B as the OLDER version of A (older state, older schedule, "
        "earlier value). The current correct fact is A; B is stale. "
        "Both were true at different points."
    ),
    "MERGE": (
        "Write a fact B that contains COMPLEMENTARY information about the "
        "same subject as A. A and B do not contradict — they each describe "
        "a different aspect or detail. Together they form a richer single "
        "fact when merged. Neither is complete on its own."
    ),
    "KEEP_BOTH": (
        "Write a fact B about the same subject as A but describing a "
        "totally DIFFERENT property or aspect. Both facts are simultaneously "
        "true and INDEPENDENT — they do not need to be merged. Examples: "
        "A about subject's color, B about subject's size."
    ),
    "REMOVE_A": (
        "Write a fact B about the same subject as A. B is FACTUALLY CORRECT, "
        "but A is OBJECTIVELY WRONG (incorrect at the time of writing, not "
        "merely outdated). The user/system would want A purged, not "
        "preserved as historical record."
    ),
    "REMOVE_B": (
        "Imagine A is correct. Write a fact B about the same subject that "
        "is OBJECTIVELY WRONG (factual error, not state change). The system "
        "should retire B, not just supersede it. B was incorrect at the "
        "time of writing."
    ),
}


_GEN_TEMPLATE = """You are generating a labeled test pair for a memory-resolution eval.

Given the SEED fact below, write ONE alternative fact B following the rule.

SEED fact A: {content}

Target action: {action}
Generation rule: {rule}

Output requirements:
- Write only the fact text (1-2 sentences, 50-300 chars).
- Do not mention the action or use meta-language.
- Do not quote A verbatim.

Return ONLY the fact text."""


async def _gen_fact_b(llm, content_a: str, action: str) -> str | None:
    payload = {
        "model": _HAIKU_MODEL,
        "max_tokens": 150,
        "temperature": 0,
        "system": "",
        "messages": [{
            "role": "user",
            "content": _GEN_TEMPLATE.format(
                content=content_a[:800],
                action=action,
                rule=_GEN_PROMPTS[action],
            ),
        }],
    }
    try:
        response = await llm.call(payload)
    except Exception:
        logging.exception("gen failed for %s", action)
        return None
    text = ""
    for block in response.content:
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text", "").strip()
            break
    text = text.strip("\"'`")
    if not text or len(text) < 30 or len(text) > 600:
        return None
    return text


async def _f031_resolve(
    llm, model: str, content_a: str, content_b: str,
) -> dict | None:
    """Mirror the production call in sleep_handler._phase_resolve_contradictions."""
    prompt = _CONTRADICTION_RESOLUTION_PROMPT.format(
        date_a="2026-04-15", date_b="2026-04-29",
        content_a=content_a[:500], content_b=content_b[:500],
    )
    try:
        return await call_background_llm_structured(
            client=llm,
            model=model,
            system_prompt="You are a memory management system resolving contradictory facts.",
            user_message=prompt,
            tool_name="resolve_contradiction",
            tool_description="Resolve a contradiction between two facts in memory.",
            output_schema=_CONTRADICTION_RESOLUTION_SCHEMA,
            max_tokens=300,
        )
    except Exception:
        logging.exception("F031 resolve failed")
        return None


# Hand-curated seed facts. Diverse enough to give the generator material
# across all 6 categories. We don't need perfect — Haiku generates B given A.
_SEEDS: list[str] = [
    "Tim's flight UA2408 from Denver to Washington Dulles departs at 5:45 PM on May 12.",
    "The Nous codebase uses claude-sonnet-4-6 as the default background model.",
    "Article 'Your AI Agent Has Amnesia' was published on dev.to on March 14, 2026.",
    "Tim Fatykhov is a software engineer building cognitive AI agents in Python.",
    "The Anthropic API rate limit for the OAT subscription is 50 requests per minute.",
    "Tim's Telegram bot token expires on June 15, 2026.",
    "PR #380 fixed three production findings from the F056 smoke test.",
    "The DAG orchestration system has a default node timeout of 600 seconds.",
    "FOMC meeting on March 11, 2026 priced one rate cut for September.",
    "Nous prod runs on the host 192.168.1.141 and listens on port 8383.",
]


async def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--n-per-category", type=int, default=5)
    p.add_argument("--resolver-model", default=_DEFAULT_RESOLVER_MODEL,
                   help="Model used by F031 in production (default Sonnet).")
    p.add_argument("--out", type=Path, default=Path("reports/f031_resolution_eval.md"))
    p.add_argument("--out-json", type=Path, default=Path("reports/f031_resolution_eval.json"))
    args = p.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass

    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    settings = Settings()
    if not (settings.anthropic_api_key or settings.anthropic_auth_token):
        print("ERROR: ANTHROPIC creds required.", file=sys.stderr)
        return 2

    api_client = create_client(settings)
    await api_client.start()

    truth_pred_matrix: dict[tuple[str, str], int] = defaultdict(int)
    confidences: dict[str, list[float]] = defaultdict(list)
    downgrades: dict[str, int] = defaultdict(int)
    pairs_for_report: list[dict] = []

    try:
        for category in _CATEGORIES:
            for i in range(args.n_per_category):
                seed = _SEEDS[(i + _CATEGORIES.index(category)) % len(_SEEDS)]
                # Pause between scenarios to ease rate limits.
                if pairs_for_report:
                    await asyncio.sleep(2.0)
                fact_b = await _gen_fact_b(api_client, seed, category)
                if fact_b is None:
                    truth_pred_matrix[(category, "GEN_FAIL")] += 1
                    continue

                await asyncio.sleep(1.0)
                resolution = await _f031_resolve(
                    api_client, args.resolver_model, seed, fact_b,
                )
                if resolution is None:
                    truth_pred_matrix[(category, "CLF_FAIL")] += 1
                    continue

                action = str(resolution.get("action", "")).upper().strip()
                confidence = float(resolution.get("confidence", 0.0))

                # Apply F031's production downgrade logic.
                applied_action = action
                downgraded = False
                if confidence < _F031_CONFIDENCE_FLOOR and action != "KEEP_BOTH":
                    applied_action = "KEEP_BOTH"
                    downgrades[category] += 1
                    downgraded = True

                truth_pred_matrix[(category, applied_action)] += 1
                confidences[category].append(confidence)
                pairs_for_report.append({
                    "truth": category,
                    "fact_a": seed[:200],
                    "fact_b": fact_b[:200],
                    "raw_action": action,
                    "applied_action": applied_action,
                    "confidence": confidence,
                    "downgraded_by_floor": downgraded,
                    "reason": resolution.get("reason", "")[:200],
                })
    finally:
        await api_client.close()

    # ---- Score ----
    print()
    print("=" * 76)
    print(f"F031 RESOLUTION EVAL — n={len(pairs_for_report)} resolver={args.resolver_model}")
    print("=" * 76)
    print()
    print(f"  {'truth':<13}{'n':>4}{'correct':>9}{'acc':>7}{'avg_conf':>10}"
          f"{'downgrades':>13}")
    overall_correct = 0
    overall_n = 0
    for truth in _CATEGORIES:
        n = sum(c for (t, _), c in truth_pred_matrix.items() if t == truth)
        # "correct" = applied action matches truth (after downgrade)
        correct = truth_pred_matrix.get((truth, truth), 0)
        acc = correct / n if n else 0
        avg_conf = (sum(confidences[truth]) / len(confidences[truth])) if confidences[truth] else 0
        print(f"  {truth:<13}{n:>4}{correct:>9}{acc:>7.0%}{avg_conf:>10.2f}"
              f"{downgrades[truth]:>13}")
        overall_correct += correct
        overall_n += n
    overall_acc = overall_correct / overall_n if overall_n else 0
    total_downgrades = sum(downgrades.values())
    print(f"  {'OVERALL':<13}{overall_n:>4}{overall_correct:>9}{overall_acc:>7.0%}"
          f"{'':>10}{total_downgrades:>13}")
    print()

    # Confusion matrix
    pred_labels = sorted({p for (_, p) in truth_pred_matrix.keys()})
    print(f"Confusion matrix (rows=truth, cols=applied action AFTER 0.7 downgrade):")
    print(f"  {'':<13}" + "".join(f"{p:>14s}" for p in pred_labels))
    for truth in _CATEGORIES:
        row = f"  {truth:<13}"
        for pred in pred_labels:
            row += f"{truth_pred_matrix.get((truth, pred), 0):>14d}"
        print(row)
    print()

    # What did the LLM raw-say (before downgrade)?
    raw_action_dist: dict[str, int] = defaultdict(int)
    for p in pairs_for_report:
        raw_action_dist[p["raw_action"]] += 1
    print("Raw (pre-downgrade) action distribution:")
    for action, count in sorted(raw_action_dist.items(), key=lambda kv: -kv[1]):
        print(f"  {action:<14} {count:>4}")
    print()

    # Confidence histogram
    all_confs = [p["confidence"] for p in pairs_for_report]
    bins = [0.0, 0.5, 0.6, 0.7, 0.8, 0.9, 1.01]
    bin_counts = [0] * (len(bins) - 1)
    for c in all_confs:
        for i in range(len(bins) - 1):
            if bins[i] <= c < bins[i + 1]:
                bin_counts[i] += 1
                break
    print("Confidence histogram:")
    for i, n in enumerate(bin_counts):
        marker = "  <-- floor" if bins[i] == 0.6 else ("  <-- floor cut" if bins[i] == 0.7 else "")
        print(f"  [{bins[i]:.1f},{bins[i+1]:.2f})  n={n}{marker}")
    print()
    print(f"Total downgrades to KEEP_BOTH: {total_downgrades}/{overall_n} "
          f"({100*total_downgrades/max(1,overall_n):.0f}%)")
    print("=" * 76)

    # ---- Persist ----
    args.out.parent.mkdir(parents=True, exist_ok=True)
    md = [
        "# F031 contradiction-resolution synthetic eval",
        "",
        f"- resolver model: `{args.resolver_model}`",
        f"- pairs: {len(pairs_for_report)}",
        f"- overall accuracy (after 0.7 floor): **{overall_acc:.0%}**",
        f"- total downgrades to KEEP_BOTH: {total_downgrades}/{overall_n}",
        "",
        "## Per-category",
        "",
        "| truth | n | correct | acc | avg_conf | downgrades |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for truth in _CATEGORIES:
        n = sum(c for (t, _), c in truth_pred_matrix.items() if t == truth)
        correct = truth_pred_matrix.get((truth, truth), 0)
        acc = correct / n if n else 0
        avg_conf = (sum(confidences[truth]) / len(confidences[truth])) if confidences[truth] else 0
        md.append(f"| {truth} | {n} | {correct} | {acc:.0%} | {avg_conf:.2f} | {downgrades[truth]} |")
    md.extend([
        "",
        "## Confusion matrix (post-downgrade)",
        "",
        "rows=ground truth, cols=action after 0.7-floor downgrade",
        "",
    ])
    md.append("| truth \\ pred | " + " | ".join(pred_labels) + " |")
    md.append("|---" * (len(pred_labels) + 1) + "|")
    for truth in _CATEGORIES:
        md.append("| " + truth + " | " + " | ".join(
            str(truth_pred_matrix.get((truth, p), 0)) for p in pred_labels
        ) + " |")
    md.extend([
        "",
        "## Raw action distribution (before 0.7 floor)",
        "",
        "| action | n |",
        "|---|---:|",
    ])
    for action, count in sorted(raw_action_dist.items(), key=lambda kv: -kv[1]):
        md.append(f"| {action} | {count} |")
    md.extend([
        "",
        "## Confidence histogram",
        "",
        "| bin | n |",
        "|---|---:|",
    ])
    for i, n in enumerate(bin_counts):
        md.append(f"| [{bins[i]:.1f}, {bins[i+1]:.2f}) | {n} |")
    md.extend([
        "",
        f"**Floor effect**: {total_downgrades}/{overall_n} non-KEEP_BOTH actions "
        f"silently downgraded by the 0.7 floor at "
        f"`sleep_handler.py:668`.",
        "",
        "## Caveat",
        "",
        "Generator (Haiku) and resolver may share systematic biases. The "
        "categories REMOVE_A and REMOVE_B are conceptually adjacent to "
        "SUPERSEDE_A/B; intra-pair confusion is expected. Use the raw-action "
        "distribution and downgrade rate as the load-bearing signals.",
    ])
    args.out.write_text("\n".join(md), encoding="utf-8")
    args.out_json.write_text(json.dumps({
        "resolver_model": args.resolver_model,
        "n_per_category": args.n_per_category,
        "overall_accuracy": overall_acc,
        "total_downgrades": total_downgrades,
        "per_category": {
            t: {
                "n": sum(c for (tt, _), c in truth_pred_matrix.items() if tt == t),
                "correct": truth_pred_matrix.get((t, t), 0),
                "downgrades": downgrades[t],
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
