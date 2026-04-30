"""Calibration accuracy eval for brain.decisions.

For each reviewed decision, the agent recorded `confidence` (0-1) before
acting. Later, an `outcome` was assigned (success / partial / failure /
pending). A well-calibrated agent has confidence ≈ actual success rate
within each confidence bin.

Computes:
- Reliability curve (10 confidence bins, 0.0-0.1 ... 0.9-1.0)
- Brier score (mean squared error between confidence and outcome)
- Expected Calibration Error (ECE), Maximum Calibration Error (MCE)
- Per-category breakdown (architecture, process, integration, tooling, security)
- Per-stakes breakdown (low, medium, high, critical)
- Strict scoring (success=1, else=0) and lenient (partial=0.5)

No LLM cost. Pure SQL + Python.

Usage:
    NOUS_EVAL_DB_NAME=nous_eval_scratch \
      uv run python scripts/eval/eval_calibration.py
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


_AGENT_ID = "nous-prod-snapshot"

# Outcome -> numeric score
_STRICT = {"success": 1.0, "partial": 0.0, "failure": 0.0}
_LENIENT = {"success": 1.0, "partial": 0.5, "failure": 0.0}


def _bin_index(conf: float, n_bins: int = 10) -> int:
    """Map confidence to a bin index in [0, n_bins-1]."""
    idx = int(conf * n_bins)
    return min(idx, n_bins - 1)  # clamp 1.0 -> top bin


def _reliability_table(
    rows: list[tuple[float, float]], n_bins: int = 10
) -> list[dict]:
    """Build per-bin reliability data.

    rows: list of (confidence, outcome_score) pairs.
    Returns one dict per bin with: lo, hi, n, mean_conf, mean_acc, gap.
    """
    bins: list[list[tuple[float, float]]] = [[] for _ in range(n_bins)]
    for conf, outcome in rows:
        bins[_bin_index(conf, n_bins)].append((conf, outcome))

    table: list[dict] = []
    for i, members in enumerate(bins):
        lo, hi = i / n_bins, (i + 1) / n_bins
        n = len(members)
        if n == 0:
            table.append({
                "bin": f"[{lo:.1f},{hi:.1f})",
                "lo": lo, "hi": hi, "n": 0,
                "mean_conf": None, "mean_acc": None, "gap": None,
            })
            continue
        mean_conf = sum(c for c, _ in members) / n
        mean_acc = sum(o for _, o in members) / n
        table.append({
            "bin": f"[{lo:.1f},{hi:.1f})",
            "lo": lo, "hi": hi, "n": n,
            "mean_conf": mean_conf,
            "mean_acc": mean_acc,
            "gap": mean_conf - mean_acc,
        })
    return table


def _brier(rows: list[tuple[float, float]]) -> float:
    """Brier score: mean squared error between confidence and outcome."""
    if not rows:
        return float("nan")
    return sum((c - o) ** 2 for c, o in rows) / len(rows)


def _ece_mce(table: list[dict], total_n: int) -> tuple[float, float]:
    """Expected & Maximum Calibration Error from a reliability table."""
    if total_n == 0:
        return float("nan"), float("nan")
    ece = 0.0
    mce = 0.0
    for row in table:
        if row["n"] == 0:
            continue
        gap = abs(row["gap"])
        ece += (row["n"] / total_n) * gap
        if gap > mce:
            mce = gap
    return ece, mce


def _format_reliability(table: list[dict], total_n: int) -> str:
    lines = []
    lines.append(f"  {'bin':<13}{'n':>6}{'%':>7}{'conf':>9}{'acc':>9}{'gap':>9}{'bar':>7}")
    for row in table:
        if row["n"] == 0:
            lines.append(f"  {row['bin']:<13}{0:>6}{0:>7}{'-':>9}{'-':>9}{'-':>9}{'':>7}")
            continue
        pct = 100 * row["n"] / total_n
        gap = row["gap"]
        # ASCII reliability bar: + if overconfident (conf > acc), - if under
        bar_len = min(20, int(abs(gap) * 40))
        bar = ("+" if gap > 0 else "-") * bar_len if abs(gap) > 0.01 else "·"
        lines.append(
            f"  {row['bin']:<13}{row['n']:>6}{pct:>6.1f}%"
            f"{row['mean_conf']:>9.3f}{row['mean_acc']:>9.3f}"
            f"{gap:>+9.3f}  {bar}"
        )
    return "\n".join(lines)


def _interpret_calibration(brier: float, ece: float, gap: float) -> str:
    """Plain-language verdict."""
    parts = []
    if abs(gap) < 0.05:
        parts.append("WELL-CALIBRATED at the aggregate level.")
    elif gap > 0:
        parts.append(f"OVERCONFIDENT by {gap:+.1%} on average.")
    else:
        parts.append(f"UNDERCONFIDENT by {gap:+.1%} on average.")

    if brier < 0.10:
        parts.append("Brier score is excellent.")
    elif brier < 0.20:
        parts.append("Brier score is reasonable.")
    elif brier < 0.25:
        parts.append("Brier score is mediocre — close to random-guessing baseline.")
    else:
        parts.append("Brier score is POOR — confidence is barely informative.")

    if ece < 0.05:
        parts.append("ECE indicates reliable per-bin calibration.")
    elif ece < 0.10:
        parts.append("ECE suggests modest miscalibration in some bins.")
    else:
        parts.append("ECE is high — substantial miscalibration in specific bins.")
    return " ".join(parts)


async def main() -> int:
    # Windows cp1252 console can't encode Greek/math symbols. Force UTF-8
    # on stdout/stderr so the formatted reliability table prints cleanly.
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass

    p = argparse.ArgumentParser()
    p.add_argument("--agent-id", default=_AGENT_ID)
    p.add_argument("--n-bins", type=int, default=10)
    p.add_argument("--out", type=Path, default=Path("reports/calibration_eval.md"))
    p.add_argument("--out-json", type=Path, default=Path("reports/calibration_eval.json"))
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

    conn = await asyncpg.connect(
        host=args.eval_host, port=args.eval_port,
        user=args.eval_user, password=args.eval_password,
        database=args.eval_db,
    )

    try:
        # F058: prefer confidence_raw (pre-calibration agent claim) when
        # populated; falls back to confidence for rows recorded before the
        # calibration migration landed.
        rows = await conn.fetch(
            """
            SELECT COALESCE(confidence_raw, confidence) AS confidence,
                   outcome, category, stakes
            FROM brain.decisions
            WHERE agent_id = $1
              AND outcome IN ('success', 'partial', 'failure')
              AND confidence IS NOT NULL
            """,
            args.agent_id,
        )
    finally:
        await conn.close()

    if not rows:
        print("ERROR: no reviewed decisions with confidence found.", file=sys.stderr)
        return 2

    # Build (conf, outcome_score) pairs under both scoring schemes
    strict_pairs = [(r["confidence"], _STRICT[r["outcome"]]) for r in rows]
    lenient_pairs = [(r["confidence"], _LENIENT[r["outcome"]]) for r in rows]
    n_total = len(rows)
    logger.info("Loaded %d reviewed decisions for %s", n_total, args.agent_id)

    # Aggregate metrics
    strict_brier = _brier(strict_pairs)
    lenient_brier = _brier(lenient_pairs)
    strict_table = _reliability_table(strict_pairs, args.n_bins)
    lenient_table = _reliability_table(lenient_pairs, args.n_bins)
    strict_ece, strict_mce = _ece_mce(strict_table, n_total)
    lenient_ece, lenient_mce = _ece_mce(lenient_table, n_total)

    mean_conf = sum(c for c, _ in strict_pairs) / n_total
    strict_acc = sum(o for _, o in strict_pairs) / n_total
    lenient_acc = sum(o for _, o in lenient_pairs) / n_total
    strict_gap = mean_conf - strict_acc
    lenient_gap = mean_conf - lenient_acc

    # Per-category & per-stakes
    by_category: dict[str, list[tuple[float, float]]] = defaultdict(list)
    by_stakes: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for r in rows:
        score = _STRICT[r["outcome"]]
        by_category[r["category"]].append((r["confidence"], score))
        by_stakes[r["stakes"]].append((r["confidence"], score))

    print()
    print("=" * 72)
    print(f"CALIBRATION EVAL — agent_id={args.agent_id}, n={n_total}")
    print("=" * 72)
    print()
    print(f"Mean confidence:      {mean_conf:.3f}")
    print(f"Mean outcome (strict):{strict_acc:.3f}   "
          f"gap={strict_gap:+.3f}")
    print(f"Mean outcome (lenient):{lenient_acc:.3f}  "
          f"gap={lenient_gap:+.3f}")
    print()
    print(f"Brier (strict):  {strict_brier:.4f}   "
          f"ECE: {strict_ece:.4f}   MCE: {strict_mce:.4f}")
    print(f"Brier (lenient): {lenient_brier:.4f}   "
          f"ECE: {lenient_ece:.4f}   MCE: {lenient_mce:.4f}")
    print()
    print(_interpret_calibration(strict_brier, strict_ece, strict_gap))
    print()
    print("Reliability curve (strict scoring; conf > acc -> overconfident +):")
    print(_format_reliability(strict_table, n_total))
    print()
    print("Per-category (strict):")
    print(f"  {'category':<14}{'n':>5}{'mean_conf':>11}{'mean_acc':>11}"
          f"{'gap':>9}{'brier':>9}")
    cat_data: dict[str, dict] = {}
    for cat, pairs in sorted(by_category.items()):
        if not pairs:
            continue
        mc = sum(c for c, _ in pairs) / len(pairs)
        ma = sum(o for _, o in pairs) / len(pairs)
        br = _brier(pairs)
        cat_data[cat] = {"n": len(pairs), "mean_conf": mc, "mean_acc": ma,
                         "gap": mc - ma, "brier": br}
        print(f"  {cat:<14}{len(pairs):>5}{mc:>11.3f}{ma:>11.3f}"
              f"{mc - ma:>+9.3f}{br:>9.4f}")
    print()
    print("Per-stakes (strict):")
    print(f"  {'stakes':<14}{'n':>5}{'mean_conf':>11}{'mean_acc':>11}"
          f"{'gap':>9}{'brier':>9}")
    stakes_data: dict[str, dict] = {}
    stakes_order = ["low", "medium", "high", "critical"]
    for st in stakes_order:
        pairs = by_stakes.get(st, [])
        if not pairs:
            continue
        mc = sum(c for c, _ in pairs) / len(pairs)
        ma = sum(o for _, o in pairs) / len(pairs)
        br = _brier(pairs)
        stakes_data[st] = {"n": len(pairs), "mean_conf": mc, "mean_acc": ma,
                           "gap": mc - ma, "brier": br}
        print(f"  {st:<14}{len(pairs):>5}{mc:>11.3f}{ma:>11.3f}"
              f"{mc - ma:>+9.3f}{br:>9.4f}")
    print("=" * 72)

    # Persist markdown + JSON
    args.out.parent.mkdir(parents=True, exist_ok=True)
    md_lines = [
        f"# Calibration eval — agent_id={args.agent_id}",
        f"- decisions analyzed: {n_total}",
        f"- bins: {args.n_bins}",
        "",
        "## Aggregate",
        f"- mean confidence: **{mean_conf:.3f}**",
        f"- mean outcome (strict): {strict_acc:.3f} (gap {strict_gap:+.3f})",
        f"- mean outcome (lenient, partial=0.5): {lenient_acc:.3f} (gap {lenient_gap:+.3f})",
        "",
        f"- Brier (strict): **{strict_brier:.4f}**, ECE {strict_ece:.4f}, MCE {strict_mce:.4f}",
        f"- Brier (lenient): {lenient_brier:.4f}, ECE {lenient_ece:.4f}, MCE {lenient_mce:.4f}",
        "",
        f"**Verdict:** {_interpret_calibration(strict_brier, strict_ece, strict_gap)}",
        "",
        "## Reliability curve (strict scoring)",
        "",
        "| bin | n | % | mean_conf | mean_acc | gap |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for row in strict_table:
        if row["n"] == 0:
            md_lines.append(f"| {row['bin']} | 0 | 0% | – | – | – |")
            continue
        pct = 100 * row["n"] / n_total
        md_lines.append(
            f"| {row['bin']} | {row['n']} | {pct:.1f}% | "
            f"{row['mean_conf']:.3f} | {row['mean_acc']:.3f} | {row['gap']:+.3f} |"
        )
    md_lines.extend([
        "",
        "## Per-category",
        "",
        "| category | n | mean_conf | mean_acc | gap | brier |",
        "|---|---:|---:|---:|---:|---:|",
    ])
    for cat, d in sorted(cat_data.items(), key=lambda kv: -kv[1]["n"]):
        md_lines.append(
            f"| {cat} | {d['n']} | {d['mean_conf']:.3f} | "
            f"{d['mean_acc']:.3f} | {d['gap']:+.3f} | {d['brier']:.4f} |"
        )
    md_lines.extend([
        "",
        "## Per-stakes",
        "",
        "| stakes | n | mean_conf | mean_acc | gap | brier |",
        "|---|---:|---:|---:|---:|---:|",
    ])
    for st in stakes_order:
        if st not in stakes_data:
            continue
        d = stakes_data[st]
        md_lines.append(
            f"| {st} | {d['n']} | {d['mean_conf']:.3f} | "
            f"{d['mean_acc']:.3f} | {d['gap']:+.3f} | {d['brier']:.4f} |"
        )
    md_lines.extend([
        "",
        "## Method",
        "",
        "- **Brier score** = mean((confidence − outcome)²) — 0 perfect, 0.25 random",
        "- **ECE** = Σ (n_bin / N) · |mean_conf_bin − mean_acc_bin|",
        "- **MCE** = max over bins of |mean_conf − mean_acc|",
        "- **Strict**: success=1, partial=failure=0",
        "- **Lenient**: success=1, partial=0.5, failure=0",
        "- Pending decisions excluded.",
    ])
    args.out.write_text("\n".join(md_lines), encoding="utf-8")
    args.out_json.write_text(json.dumps({
        "agent_id": args.agent_id,
        "n_total": n_total,
        "mean_confidence": mean_conf,
        "strict": {
            "mean_outcome": strict_acc, "gap": strict_gap,
            "brier": strict_brier, "ece": strict_ece, "mce": strict_mce,
            "reliability": strict_table,
        },
        "lenient": {
            "mean_outcome": lenient_acc, "gap": lenient_gap,
            "brier": lenient_brier, "ece": lenient_ece, "mce": lenient_mce,
            "reliability": lenient_table,
        },
        "by_category": cat_data,
        "by_stakes": stakes_data,
    }, indent=2), encoding="utf-8")
    print(f"\nWrote: {args.out}")
    print(f"Wrote: {args.out_json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
