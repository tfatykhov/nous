"""F058 confidence-calibration validation probe.

F058 (2026-04-30) shipped a 0.7627 multiplicative scaling factor
applied at decision-record time. Original confidence is preserved in
``brain.decisions.confidence_raw``; the scaled value lives in
``confidence``.

Validation is hard short-term because reviewed post-rollout decisions
accumulate slowly (~4 in the first 3 days). This probe runs three
checks instead of waiting:

  1. SANITY — every post-F058 row in prod must show
     ``confidence == confidence_raw * factor`` exactly. If not, the
     scaling is silently broken in the write path.

  2. COUNTERFACTUAL — apply the factor retroactively to all reviewed
     pre-F058 decisions and recompute Brier / ECE. The aggregate gap
     collapses to ~0 by construction (the factor was derived from
     this same data), but **Brier and ECE deltas are NOT determined
     by the factor** — they tell us whether the scaling captures real
     signal or just shifts the mean.

  3. DIRECTION CHECK — measure raw vs calibrated calibration on the
     small post-F058 reviewed sample. Tiny n, but if the delta points
     the same way as the counterfactual, that's a weak-but-coherent
     confirmation.

Connects to live PROD (default 192.168.1.141), READ-ONLY.

Re-run weekly until ``n_post_f058_reviewed >= 50``, then re-derive
the factor from real post-rollout outcomes (the current factor was
derived from pre-F058 data; outcome distribution may have shifted).

Run:
    set -a; source .env; set +a
    uv run python -m nous_eval.probes.f058_calibration

Exit code (with --strict):
    0 — sanity passes AND counterfactual shows Brier improvement
    1 — sanity fails OR scaling DEGRADES Brier (factor mis-set)
    2 — env / connection error
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

import asyncpg


# Default factor — keep in sync with Settings.confidence_calibration_factor.
_DEFAULT_FACTOR = 0.7627
_DEFAULT_AGENT_ID = "nous-default"

_STRICT_OUTCOME = {"success": 1.0, "partial": 0.0, "failure": 0.0}


def _bin_index(c: float, n_bins: int = 10) -> int:
    return min(int(c * n_bins), n_bins - 1)


def brier(pairs: list[tuple[float, float]]) -> float:
    """Mean squared error between confidence and outcome (0=perfect, 0.25=random)."""
    if not pairs:
        return float("nan")
    return sum((c - o) ** 2 for c, o in pairs) / len(pairs)


def ece(pairs: list[tuple[float, float]], n_bins: int = 10) -> float:
    """Expected Calibration Error."""
    if not pairs:
        return float("nan")
    bins: list[list[tuple[float, float]]] = [[] for _ in range(n_bins)]
    for c, o in pairs:
        bins[_bin_index(c, n_bins)].append((c, o))
    total = len(pairs)
    score = 0.0
    for members in bins:
        if not members:
            continue
        mc = sum(c for c, _ in members) / len(members)
        mo = sum(o for _, o in members) / len(members)
        score += (len(members) / total) * abs(mc - mo)
    return score


def gap(pairs: list[tuple[float, float]]) -> float:
    if not pairs:
        return float("nan")
    n = len(pairs)
    return sum(c for c, _ in pairs) / n - sum(o for _, o in pairs) / n


def summarize(label: str, pairs: list[tuple[float, float]]) -> dict:
    if not pairs:
        return {"label": label, "n": 0}
    n = len(pairs)
    return {
        "label": label,
        "n": n,
        "mean_conf": sum(c for c, _ in pairs) / n,
        "mean_outcome": sum(o for _, o in pairs) / n,
        "gap": gap(pairs),
        "brier": brier(pairs),
        "ece": ece(pairs),
    }


def _print_summary(s: dict) -> None:
    if s["n"] == 0:
        print(f"  {s['label']:<26}  n=0 — skip")
        return
    print(
        f"  {s['label']:<26}  n={s['n']:>4}  "
        f"conf={s['mean_conf']:.3f}  outcome={s['mean_outcome']:.3f}  "
        f"gap={s['gap']:+.3f}  Brier={s['brier']:.4f}  ECE={s['ece']:.4f}"
    )


async def run(
    conn: asyncpg.Connection, agent_id: str, factor: float,
) -> dict:
    """Execute all three checks against ``conn``. Returns a dict.

    Caller owns connection lifetime.
    """
    rows = await conn.fetch(
        """
        SELECT
            COALESCE(confidence_raw, confidence) AS raw,
            confidence AS stored,
            confidence_raw IS NOT NULL AS is_post_f058,
            outcome
        FROM brain.decisions
        WHERE agent_id = $1
          AND outcome IN ('success', 'partial', 'failure')
          AND confidence IS NOT NULL
        ORDER BY created_at
        """,
        agent_id,
    )
    post_f058_rows = await conn.fetch(
        """
        SELECT confidence_raw, confidence,
               (confidence / NULLIF(confidence_raw, 0))::float8 AS r
        FROM brain.decisions
        WHERE agent_id = $1 AND confidence_raw IS NOT NULL
        """,
        agent_id,
    )

    # Step 1: scaling-applied sanity
    bad_ratios = [
        r for r in post_f058_rows
        if r["r"] is not None and abs(r["r"] - factor) > 0.001
    ]
    sanity_ok = len(bad_ratios) == 0

    # Step 2: counterfactual on pre-F058 reviewed
    pre_f058 = [r for r in rows if not r["is_post_f058"]]
    raw_pairs = [(float(r["raw"]), _STRICT_OUTCOME[r["outcome"]])
                 for r in pre_f058]
    cal_pairs = [(float(r["raw"]) * factor, _STRICT_OUTCOME[r["outcome"]])
                 for r in pre_f058]

    # Step 3: direction check on post-F058 reviewed
    post_f058 = [r for r in rows if r["is_post_f058"]]
    post_raw_pairs = [(float(r["raw"]), _STRICT_OUTCOME[r["outcome"]])
                      for r in post_f058]
    post_cal_pairs = [(float(r["stored"]), _STRICT_OUTCOME[r["outcome"]])
                      for r in post_f058]

    return {
        "factor": factor,
        "agent_id": agent_id,
        "sanity": {
            "ok": sanity_ok,
            "n_post_f058": len(post_f058_rows),
            "n_bad": len(bad_ratios),
        },
        "counterfactual": {
            "raw": summarize("Pre-F058 RAW", raw_pairs),
            "calibrated": summarize("Pre-F058 + counterfactual", cal_pairs),
        },
        "post_f058_direction": {
            "raw": summarize("Post-F058 RAW", post_raw_pairs),
            "calibrated": summarize("Post-F058 calibrated", post_cal_pairs),
        },
    }


def verdict_exit_code(result: dict) -> int:
    """0 = pass, 1 = real regression. Used by --strict."""
    if not result["sanity"]["ok"]:
        return 1
    cf = result["counterfactual"]
    if cf["raw"]["n"] == 0 or cf["calibrated"]["n"] == 0:
        return 0
    if cf["calibrated"]["brier"] > cf["raw"]["brier"]:
        return 1  # F058 makes Brier WORSE — factor needs re-derivation
    return 0


async def _async_main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass

    p = argparse.ArgumentParser(
        description="F058 confidence-calibration validation probe.",
    )
    p.add_argument("--prod-host",
                   default=os.environ.get("PROD_DB_HOST", "192.168.1.141"))
    p.add_argument("--prod-port", type=int,
                   default=int(os.environ.get("DB_PORT", "5432")))
    p.add_argument("--prod-user", default=os.environ.get("DB_USER", "nous"))
    p.add_argument("--prod-password",
                   default=os.environ.get("DB_PASSWORD"))
    p.add_argument("--prod-db", default=os.environ.get("DB_NAME", "nous"))
    p.add_argument("--agent-id", default=_DEFAULT_AGENT_ID)
    p.add_argument("--factor", type=float, default=_DEFAULT_FACTOR)
    p.add_argument("--strict", action="store_true",
                   help="Exit 1 on sanity fail or counterfactual Brier regression.")
    p.add_argument("--out", type=Path,
                   default=Path("reports/eval_f058_counterfactual.md"))
    p.add_argument("--out-json", type=Path,
                   default=Path("reports/eval_f058_counterfactual.json"))
    args = p.parse_args(argv)

    if not args.prod_password:
        print("ERROR: prod DB_PASSWORD not set in env / .env", file=sys.stderr)
        return 2

    conn = await asyncpg.connect(
        host=args.prod_host, port=args.prod_port,
        user=args.prod_user, password=args.prod_password,
        database=args.prod_db,
    )
    try:
        result = await run(conn, args.agent_id, args.factor)
    finally:
        await conn.close()

    print()
    print("=" * 84)
    print(f"F058 CALIBRATION VALIDATION — agent={args.agent_id}, "
          f"factor={args.factor}")
    print("=" * 84)
    print()
    print("## Step 1 — Sanity (factor applied in prod write path)")
    s = result["sanity"]
    print(f"   post-F058 rows: {s['n_post_f058']}")
    if s["ok"]:
        print(f"   [PASS] all rows show confidence == confidence_raw * "
              f"{args.factor:.4f}")
    else:
        print(f"   [FAIL] {s['n_bad']} rows have wrong ratio")
    print()
    print("## Step 2 — Counterfactual (apply F058 to pre-F058 reviewed)")
    cf = result["counterfactual"]
    _print_summary(cf["raw"])
    _print_summary(cf["calibrated"])
    if cf["raw"]["n"] > 0 and cf["calibrated"]["n"] > 0:
        d_brier = cf["calibrated"]["brier"] - cf["raw"]["brier"]
        d_ece = cf["calibrated"]["ece"] - cf["raw"]["ece"]
        print(f"   \u0394 Brier:  {d_brier:+.4f}  "
              f"({'better' if d_brier < 0 else 'worse'} per-instance error)")
        print(f"   \u0394 ECE:    {d_ece:+.4f}  "
              f"({'better' if d_ece < 0 else 'worse'} per-bin calibration)")
    print()
    print("## Step 3 — Direction check (post-F058 reviewed)")
    pd = result["post_f058_direction"]
    _print_summary(pd["raw"])
    _print_summary(pd["calibrated"])
    if pd["raw"]["n"] > 0:
        d_gap = abs(pd["calibrated"]["gap"]) - abs(pd["raw"]["gap"])
        print(f"   \u0394 |gap|:  {d_gap:+.3f}  "
              f"(consistent with F058 reducing overconfidence: "
              f"{d_gap < 0})")
        print(f"   Caveat: n={pd['raw']['n']} — directional only.")
    print()
    print("=" * 84)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out_json.write_text(
        json.dumps(result, indent=2, default=str), encoding="utf-8"
    )
    args.out.write_text("\n".join(_build_md(result, args.factor)),
                        encoding="utf-8")
    print(f"\nWrote: {args.out}")
    print(f"Wrote: {args.out_json}")

    if args.strict:
        return verdict_exit_code(result)
    return 0


def _build_md(result: dict, factor: float) -> list[str]:
    s = result["sanity"]
    cf = result["counterfactual"]
    pd = result["post_f058_direction"]
    md = [
        "# F058 calibration validation",
        f"- agent_id: `{result['agent_id']}`",
        f"- factor: **{factor}**",
        "",
        "## Step 1 — Sanity (factor applied in prod)",
        f"- post-F058 rows: {s['n_post_f058']}",
        (f"- **PASS** all rows show "
         f"`confidence = confidence_raw * {factor:.4f}`"
         if s["ok"] else f"- **FAIL** {s['n_bad']} rows have wrong ratio"),
        "",
        "## Step 2 — Counterfactual on pre-F058 reviewed",
        "",
        "| variant | n | mean_conf | mean_outcome | gap | Brier | ECE |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for v in (cf["raw"], cf["calibrated"]):
        if v["n"] == 0:
            continue
        md.append(
            f"| {v['label']} | {v['n']} | {v['mean_conf']:.3f} | "
            f"{v['mean_outcome']:.3f} | {v['gap']:+.3f} | "
            f"{v['brier']:.4f} | {v['ece']:.4f} |"
        )
    if cf["raw"]["n"] > 0:
        d_brier = cf["calibrated"]["brier"] - cf["raw"]["brier"]
        d_ece = cf["calibrated"]["ece"] - cf["raw"]["ece"]
        md += [
            "",
            f"- **\u0394 Brier**: {d_brier:+.4f}",
            f"- **\u0394 ECE**: {d_ece:+.4f}",
            "- Aggregate gap collapses to ~0 by construction "
            "(factor derived from this same data); Brier/ECE deltas "
            "are NOT determined by the factor.",
        ]
    md += [
        "",
        "## Step 3 — Direction check (post-F058 reviewed)",
        "",
        "| variant | n | mean_conf | mean_outcome | gap | Brier | ECE |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for v in (pd["raw"], pd["calibrated"]):
        if v["n"] == 0:
            continue
        md.append(
            f"| {v['label']} | {v['n']} | {v['mean_conf']:.3f} | "
            f"{v['mean_outcome']:.3f} | {v['gap']:+.3f} | "
            f"{v['brier']:.4f} | {v['ece']:.4f} |"
        )
    if pd["raw"]["n"] > 0:
        md.append("")
        md.append(
            f"- **Caveat**: post-F058 n={pd['raw']['n']} — directional only."
        )
    return md


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(_async_main(argv))


if __name__ == "__main__":
    raise SystemExit(main())
