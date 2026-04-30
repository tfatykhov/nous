"""Simulate candidate calibration-scaling strategies against the eval data.

Reads `brain.decisions` rows for the eval agent_id, applies several
candidate scaling strategies to the recorded `confidence`, and reports
Brier / ECE / gap for each. Used to pick the best scaling strategy
before shipping it as production code.

Strategies tested:
  - none: baseline (no scaling)
  - global: all categories scaled by global accuracy/confidence ratio
  - per_category_strict: each category scaled by its own ratio
  - per_category_floor: per-category factors but only for n >= MIN_N;
    smaller categories fall back to global (avoids overfitting at n=5)
  - clipped_min: per_category_floor but bounded so no factor < 0.50
    (avoids over-flattening categories like tooling that may improve
    with prompt fixes)

Usage:
    NOUS_EVAL_DB_NAME=nous_eval_scratch \
      uv run python scripts/eval/simulate_calibration_fix.py
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import defaultdict
from pathlib import Path

import asyncpg


_AGENT_ID = "nous-prod-snapshot"
_STRICT = {"success": 1.0, "partial": 0.0, "failure": 0.0}
_MIN_N_FOR_PER_CATEGORY = 20


def _bin_index(conf: float, n_bins: int = 10) -> int:
    return min(int(conf * n_bins), n_bins - 1)


def _brier(pairs: list[tuple[float, float]]) -> float:
    if not pairs:
        return float("nan")
    return sum((c - o) ** 2 for c, o in pairs) / len(pairs)


def _ece(pairs: list[tuple[float, float]], n_bins: int = 10) -> float:
    bins: list[list[tuple[float, float]]] = [[] for _ in range(n_bins)]
    for c, o in pairs:
        bins[_bin_index(c, n_bins)].append((c, o))
    total = len(pairs) or 1
    ece = 0.0
    for bucket in bins:
        if not bucket:
            continue
        mc = sum(c for c, _ in bucket) / len(bucket)
        ma = sum(o for _, o in bucket) / len(bucket)
        ece += (len(bucket) / total) * abs(mc - ma)
    return ece


def _stats(pairs: list[tuple[float, float]]) -> dict:
    n = len(pairs)
    if n == 0:
        return {"n": 0}
    mc = sum(c for c, _ in pairs) / n
    ma = sum(o for _, o in pairs) / n
    return {
        "n": n,
        "mean_conf": mc,
        "mean_acc": ma,
        "gap": mc - ma,
        "brier": _brier(pairs),
        "ece": _ece(pairs),
    }


def _build_factors(
    rows: list[dict], min_n: int = _MIN_N_FOR_PER_CATEGORY,
    floor: float | None = None,
) -> tuple[float, dict[str, float]]:
    """Compute global factor + per-category factors from raw decisions.

    Returns (global_factor, per_category_factors). Categories with
    n < min_n are absent from per_category_factors → callers should
    fall back to global.
    """
    by_cat: dict[str, list[tuple[float, float]]] = defaultdict(list)
    all_pairs: list[tuple[float, float]] = []
    for r in rows:
        score = _STRICT[r["outcome"]]
        all_pairs.append((r["confidence"], score))
        by_cat[r["category"]].append((r["confidence"], score))

    g_conf = sum(c for c, _ in all_pairs) / len(all_pairs)
    g_acc = sum(o for _, o in all_pairs) / len(all_pairs)
    global_factor = g_acc / g_conf if g_conf > 0 else 1.0

    per_cat: dict[str, float] = {}
    for cat, pairs in by_cat.items():
        if len(pairs) < min_n:
            continue
        mc = sum(c for c, _ in pairs) / len(pairs)
        ma = sum(o for _, o in pairs) / len(pairs)
        if mc <= 0:
            continue
        f = ma / mc
        if floor is not None:
            f = max(f, floor)
        per_cat[cat] = f
    return global_factor, per_cat


def _apply_strategy(
    rows: list[dict], strategy: str,
    global_factor: float, per_cat: dict[str, float],
) -> list[tuple[float, float]]:
    """Apply scaling strategy and return (calibrated_conf, outcome_score) pairs."""
    pairs: list[tuple[float, float]] = []
    for r in rows:
        raw = r["confidence"]
        cat = r["category"]
        if strategy == "none":
            f = 1.0
        elif strategy == "global":
            f = global_factor
        elif strategy in {"per_category_strict", "per_category_floor",
                          "clipped_min"}:
            f = per_cat.get(cat, global_factor)
        else:
            f = 1.0
        scaled = max(0.0, min(1.0, raw * f))  # clip to [0, 1]
        pairs.append((scaled, _STRICT[r["outcome"]]))
    return pairs


async def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--agent-id", default=_AGENT_ID)
    p.add_argument("--out", type=Path, default=Path("reports/calibration_simulation.md"))
    p.add_argument("--out-json", type=Path, default=Path("reports/calibration_simulation.json"))
    p.add_argument("--eval-host", default="127.0.0.1")
    p.add_argument("--eval-port", type=int, default=5433)
    p.add_argument("--eval-user", default="nous")
    p.add_argument("--eval-password", default="nous_eval")
    p.add_argument("--eval-db", default="nous_eval_scratch")
    args = p.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass

    conn = await asyncpg.connect(
        host=args.eval_host, port=args.eval_port,
        user=args.eval_user, password=args.eval_password,
        database=args.eval_db,
    )
    try:
        rows = await conn.fetch(
            """
            SELECT confidence, outcome, category, stakes
            FROM brain.decisions
            WHERE agent_id = $1
              AND outcome IN ('success', 'partial', 'failure')
              AND confidence IS NOT NULL
            """,
            args.agent_id,
        )
    finally:
        await conn.close()

    rows_d = [dict(r) for r in rows]
    if not rows_d:
        print("ERROR: no reviewed decisions found.", file=sys.stderr)
        return 2

    # Compute factor sets for two clipping policies
    g_factor, per_cat_strict = _build_factors(rows_d)
    _, per_cat_clipped = _build_factors(rows_d, floor=0.50)

    strategies = [
        ("none", g_factor, {}),
        ("global", g_factor, {}),
        ("per_category_floor", g_factor, per_cat_strict),
        ("clipped_min", g_factor, per_cat_clipped),
    ]

    print()
    print("=" * 78)
    print(f"CALIBRATION SIMULATION — agent_id={args.agent_id}, n={len(rows_d)}")
    print("=" * 78)
    print(f"\nGlobal factor: {g_factor:.4f}")
    print(f"Per-category factors (n >= {_MIN_N_FOR_PER_CATEGORY}, no floor):")
    for cat, f in sorted(per_cat_strict.items()):
        print(f"  {cat:<14} factor={f:.4f}")
    print(f"Per-category factors (n >= {_MIN_N_FOR_PER_CATEGORY}, floor=0.50):")
    for cat, f in sorted(per_cat_clipped.items()):
        print(f"  {cat:<14} factor={f:.4f}")
    print()
    print(f"  {'strategy':<22}{'mean_conf':>11}{'gap':>9}{'brier':>9}{'ece':>9}")
    results: dict[str, dict] = {}
    for name, gfac, pcat in strategies:
        pairs = _apply_strategy(rows_d, name, gfac, pcat)
        s = _stats(pairs)
        results[name] = s
        print(f"  {name:<22}{s['mean_conf']:>11.3f}{s['gap']:>+9.3f}"
              f"{s['brier']:>9.4f}{s['ece']:>9.4f}")
    print()

    # Per-category breakdown for the recommended strategy
    print(f"Per-category breakdown — clipped_min:")
    print(f"  {'category':<14}{'n':>5}{'mc_raw':>10}{'mc_scaled':>11}"
          f"{'acc':>9}{'gap_raw':>11}{'gap_scaled':>13}")
    by_cat_raw: dict[str, list[tuple[float, float]]] = defaultdict(list)
    by_cat_scaled: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for r in rows_d:
        score = _STRICT[r["outcome"]]
        cat = r["category"]
        f = per_cat_clipped.get(cat, g_factor)
        by_cat_raw[cat].append((r["confidence"], score))
        by_cat_scaled[cat].append((max(0.0, min(1.0, r["confidence"] * f)), score))
    for cat in sorted(by_cat_raw):
        raw_pairs = by_cat_raw[cat]
        scaled_pairs = by_cat_scaled[cat]
        n = len(raw_pairs)
        mc_raw = sum(c for c, _ in raw_pairs) / n
        mc_scaled = sum(c for c, _ in scaled_pairs) / n
        acc = sum(o for _, o in raw_pairs) / n
        gap_raw = mc_raw - acc
        gap_scaled = mc_scaled - acc
        print(f"  {cat:<14}{n:>5}{mc_raw:>10.3f}{mc_scaled:>11.3f}"
              f"{acc:>9.3f}{gap_raw:>+11.3f}{gap_scaled:>+13.3f}")
    print("=" * 78)

    # Pick the best strategy by lowest ECE (most informative metric for gates)
    best_name = min(results.keys() - {"none"}, key=lambda k: results[k]["ece"])
    print(f"\nBest strategy by ECE: {best_name} "
          f"(brier {results[best_name]['brier']:.4f}, "
          f"ece {results[best_name]['ece']:.4f}, "
          f"gap {results[best_name]['gap']:+.3f})")
    delta_brier = results["none"]["brier"] - results[best_name]["brier"]
    delta_ece = results["none"]["ece"] - results[best_name]["ece"]
    print(f"Improvement vs none: ΔBrier {delta_brier:+.4f}, ΔECE {delta_ece:+.4f}")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    md = [
        f"# Calibration scaling simulation — agent_id={args.agent_id}",
        f"- decisions: {len(rows_d)}",
        f"- global factor: {g_factor:.4f}",
        f"- best strategy by ECE: **{best_name}**",
        "",
        "## Strategy comparison",
        "",
        "| strategy | mean_conf | gap | Brier | ECE |",
        "|---|---:|---:|---:|---:|",
    ]
    for name, _, _ in strategies:
        s = results[name]
        md.append(
            f"| {name} | {s['mean_conf']:.3f} | {s['gap']:+.3f} | "
            f"{s['brier']:.4f} | {s['ece']:.4f} |"
        )
    md.extend([
        "",
        "## Per-category factors (clipped_min, floor=0.50)",
        "",
        "| category | factor |",
        "|---|---:|",
    ])
    for cat, f in sorted(per_cat_clipped.items()):
        md.append(f"| {cat} | {f:.4f} |")
    md.extend([
        "",
        f"Categories with n < {_MIN_N_FOR_PER_CATEGORY} fall back to global factor "
        f"({g_factor:.4f}).",
        "",
        f"## Recommendation",
        "",
        f"Ship **{best_name}** scaling: ΔBrier {delta_brier:+.4f}, "
        f"ΔECE {delta_ece:+.4f} versus no scaling.",
    ])
    args.out.write_text("\n".join(md), encoding="utf-8")
    args.out_json.write_text(json.dumps({
        "agent_id": args.agent_id,
        "n": len(rows_d),
        "global_factor": g_factor,
        "per_category_strict": per_cat_strict,
        "per_category_clipped": per_cat_clipped,
        "results": results,
        "best_strategy": best_name,
    }, indent=2), encoding="utf-8")
    print(f"\nWrote: {args.out}")
    print(f"Wrote: {args.out_json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
