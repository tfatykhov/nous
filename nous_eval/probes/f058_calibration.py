"""F058 confidence-calibration validation probe.

F058 (2026-04-30) shipped a 0.7627 multiplicative scaling factor
applied at decision-record time. Original confidence is preserved in
``brain.decisions.confidence_raw``; the scaled value lives in
``confidence``.

Validation is hard short-term because reviewed post-rollout decisions
accumulate slowly (~4 in the first 3 days). This probe runs three
checks instead of waiting:

  1. SANITY — two checks off ``brain.decisions.calibration_factor``
     (migration 073), which records the factor actually applied:
       (a) integrity: every row must satisfy ``confidence ==
           confidence_raw * calibration_factor``, in either era;
       (b) currency: every calibration performed at or after
           ``--retired-at`` must have used the configured factor, else the
           deployment is still scaling with a stale override.
     Rows predating migration 073 have a NULL factor and are skipped, so
     history cannot pin --strict to a permanent failure.

  2. COUNTERFACTUAL — apply the HISTORICAL F058 factor (0.7627, not the
     now-retired live factor) retroactively to all reviewed pre-F058
     decisions and recompute Brier / ECE. The aggregate gap
     collapses to ~0 by construction (the factor was derived from
     this same data), but **Brier and ECE deltas are NOT determined
     by the factor** — they tell us whether the scaling captures real
     signal or just shifts the mean. Mathematically: Brier(k) is a
     quadratic in the scaling factor k with minimum at
     k* = E[c·o] / E[c²]. The shipped factor k_mean = E[o]/E[c] only
     coincides with k* when the residual (c - o) structure is well
     approximated by a global rescale; the Brier delta surfaces how
     close k_mean lands to the per-instance-MSE-optimal k*.

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

Two distinct factors are in play since the retirement and must not be
conflated: ``--factor`` is what the write path should apply NOW (1.0),
while ``--counterfactual-factor`` is the scaling hypothesis step 2
evaluates (0.7627). Passing the live 1.0 into step 2 makes it multiply
by one, so the Brier/ECE gate would pass without testing anything.

``--retired-at`` dates the deploy and is compared against
``calibration_applied_at``, which is stamped only when confidence is
actually (re)calibrated — unlike ``updated_at``, which also moves for
description-only edits.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

import asyncpg

# Recompute through the SAME function the write path uses, so the
# integrity check cannot drift from the implementation it validates
# (clipping, and anything the curve grows later).
from nous.brain.calibration_scaling import calibrate_confidence


# What the write path should be applying RIGHT NOW. Keep in sync with
# Settings.confidence_calibration_factor. Retired to 1.0 on 2026-09-20.
_DEFAULT_FACTOR = 1.0

# What the write path applied during the F058 era (2026-04-30 .. 2026-09-20).
# This is the scaling HYPOTHESIS step 2 evaluates, and it is deliberately a
# separate constant from _DEFAULT_FACTOR: until the retirement the two were
# the same number, which hid the fact that `factor` was doing two unrelated
# jobs. At 1.0 the counterfactual would multiply by one and "prove" that
# scaling neither helps nor hurts, so the step-2 gate would pass vacuously.
_HISTORICAL_F058_FACTOR = 0.7627

# Tolerance for comparing two factors (a configuration value).
_FACTOR_TOLERANCE = 0.001

# Tolerance for comparing a stored confidence against the recomputed one.
# Both are double precision and produced by the same function, so this only
# absorbs the float round trip through Postgres.
_VALUE_TOLERANCE = 1e-9

# When the retirement reached prod. Rows created at or after this instant must
# have been written by the retired-factor build, so they -- and only they --
# are the cohort step 1 validates.
#
# This MUST be at or after the actual deploy. Set it earlier and rows written
# by the old build fall inside the cohort and fail forever; that permanent
# --strict exit 1 is the exact bug this cohort logic was introduced to fix.
# Override with --retired-at when the factor changes again.
_FACTOR_RETIRED_AT = datetime(2026, 9, 21, tzinfo=UTC)

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
    counterfactual_factor: float = _HISTORICAL_F058_FACTOR,
    retired_at: datetime = _FACTOR_RETIRED_AT,
) -> dict:
    """Execute all three checks against ``conn``. Returns a dict.

    ``factor`` is the scaling the write path is expected to apply now;
    ``counterfactual_factor`` is the historical scaling step 2 tests as a
    hypothesis against pre-F058 data. They are the same number only before
    the 2026-09-20 retirement. ``retired_at`` is the deploy instant that
    separates the two eras.

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
        SELECT confidence_raw, confidence, calibration_factor,
               (calibration_applied_at >= $2) AS is_current_era
        FROM brain.decisions
        WHERE agent_id = $1 AND confidence_raw IS NOT NULL
        """,
        agent_id, retired_at,
    )

    # Step 1: scaling-applied sanity.
    #
    # Two independent questions, which the old single check conflated:
    #
    #   (a) INTEGRITY -- does each row's stored confidence actually equal
    #       confidence_raw * the factor that was applied to it? This is a
    #       property of the write path and is checkable on ANY row that
    #       recorded its factor, in either era.
    #
    #   (b) CURRENCY -- is the deployment applying the factor we expect right
    #       now? Only calibration writes since the retirement can answer this.
    #
    # Both read brain.decisions.calibration_factor (migration 073) rather than
    # inferring the era. Every inference rule available is unsound: ratio
    # ordering breaks because Brain._update rescales historical rows with the
    # CURRENT factor while leaving created_at alone; created_at is immutable
    # and so misses exactly those rewrites, letting a stale override keep
    # writing bad values while the cohort stays empty; updated_at is bumped by
    # description-only edits that never touch confidence, which would sweep
    # correctly-scaled old rows in as false failures.
    #
    # calibration_applied_at is stamped only when confidence is actually
    # (re)calibrated, so it dates the factor application itself. Rows predating
    # migration 073 have NULL and are skipped -- the same fallback migration
    # 039 used for confidence_raw.
    # Integrity compares the stored value against the value the production
    # calibrator actually produces, NOT against the confidence/confidence_raw
    # ratio. The ratio is not a faithful signal:
    #   * calibrate_confidence clips to [0, 1], so a correct write with a
    #     factor above 1.0 (raw 0.95 at factor 1.2 -> stored 1.0) has a ratio
    #     of ~1.053 and would be reported as corruption;
    #   * raw 0.0 is a valid confidence but makes the ratio a division by
    #     zero, so those rows would silently bypass the check entirely.
    # Recomputing through the real function also keeps this check honest if
    # the calibration curve ever stops being a plain multiply.
    scoped = [r for r in post_f058_rows if r["calibration_factor"] is not None]
    bad_integrity = [
        r for r in scoped
        if abs(
            float(r["confidence"])
            - calibrate_confidence(float(r["confidence_raw"]),
                                   float(r["calibration_factor"]))
        ) > _VALUE_TOLERANCE
    ]
    # (b) deliberately does NOT age out: a persistent stale override must keep
    # failing until someone fixes the deployment, where a rolling window would
    # let it go quiet after a month.
    current_era = [r for r in scoped if r["is_current_era"]]
    bad_ratios = [
        r for r in current_era
        if abs(float(r["calibration_factor"]) - factor) > _FACTOR_TOLERANCE
    ]
    sanity_ok = not bad_ratios and not bad_integrity

    # Step 2: counterfactual on pre-F058 reviewed
    pre_f058 = [r for r in rows if not r["is_post_f058"]]
    raw_pairs = [(float(r["raw"]), _STRICT_OUTCOME[r["outcome"]])
                 for r in pre_f058]
    cal_pairs = [(float(r["raw"]) * counterfactual_factor,
                  _STRICT_OUTCOME[r["outcome"]])
                 for r in pre_f058]

    # Step 3: direction check on post-F058 reviewed
    post_f058 = [r for r in rows if r["is_post_f058"]]
    post_raw_pairs = [(float(r["raw"]), _STRICT_OUTCOME[r["outcome"]])
                      for r in post_f058]
    post_cal_pairs = [(float(r["stored"]), _STRICT_OUTCOME[r["outcome"]])
                      for r in post_f058]

    return {
        "factor": factor,
        "counterfactual_factor": counterfactual_factor,
        "retired_at": retired_at.isoformat(),
        "agent_id": agent_id,
        "sanity": {
            "ok": sanity_ok,
            "n_post_f058": len(post_f058_rows),
            "n_with_factor": len(scoped),
            "n_current_era": len(current_era),
            "n_prior_era": len(scoped) - len(current_era),
            "n_bad": len(bad_ratios),
            "n_bad_integrity": len(bad_integrity),
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


# Tolerance for ECE regression: a small drift is noise; >1% absolute
# drop in per-bin calibration is a real signal worth surfacing.
_ECE_REGRESSION_EPSILON = 0.01


def verdict_exit_code(result: dict) -> int:
    """0 = pass, 1 = real regression. Used by --strict.

    Fails on three orthogonal conditions, in order of severity:
      1. Sanity break — factor not applied correctly in prod write path.
      2. Brier degrades — applying factor makes per-instance MSE worse,
         meaning the global rescale is mis-set.
      3. ECE degrades by more than ``_ECE_REGRESSION_EPSILON`` — a future
         re-derivation could improve Brier by hurting per-bin calibration
         (e.g. helping the high-conf bin while collapsing low-conf bins).
         Brier alone misses this; the gate must inspect ECE too.
    """
    if not result["sanity"]["ok"]:
        return 1
    cf = result["counterfactual"]
    if cf["raw"]["n"] == 0 or cf["calibrated"]["n"] == 0:
        return 0
    if cf["calibrated"]["brier"] > cf["raw"]["brier"]:
        return 1  # F058 makes Brier WORSE — factor needs re-derivation
    if cf["calibrated"]["ece"] > cf["raw"]["ece"] + _ECE_REGRESSION_EPSILON:
        return 1  # Per-bin calibration regressed even though Brier didn't
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
    p.add_argument("--factor", type=float, default=_DEFAULT_FACTOR,
                   help="Scaling the prod write path should apply now.")
    p.add_argument("--retired-at", type=datetime.fromisoformat,
                   default=_FACTOR_RETIRED_AT,
                   help="Deploy instant separating the two factor eras. The "
                        "currency check covers calibrations performed at or "
                        "after it (by calibration_applied_at, not created_at).")
    p.add_argument("--counterfactual-factor", type=float,
                   default=_HISTORICAL_F058_FACTOR,
                   help="Scaling hypothesis tested against pre-F058 data. "
                        "Defaults to the historical F058 factor, NOT --factor: "
                        "after the retirement --factor is 1.0 and would make "
                        "step 2 a no-op that always passes.")
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
        # Defense-in-depth: this probe is read-only by intent. Marking the
        # session read-only at the Postgres level guarantees a future
        # contributor adding a third query cannot accidentally write to
        # prod even if they bypass the existing two SELECTs.
        await conn.execute("SET default_transaction_read_only = on")
        retired_at = args.retired_at
        if retired_at.tzinfo is None:
            retired_at = retired_at.replace(tzinfo=UTC)
        result = await run(conn, args.agent_id, args.factor,
                           args.counterfactual_factor, retired_at)
    finally:
        await conn.close()

    print()
    print("=" * 84)
    print(f"F058 CALIBRATION VALIDATION — agent={args.agent_id}, "
          f"factor={args.factor}, "
          f"counterfactual={args.counterfactual_factor}")
    print("=" * 84)
    print()
    print("## Step 1 — Sanity (factor applied in prod write path)")
    s = result["sanity"]
    print(f"   post-F058 rows: {s['n_post_f058']} "
          f"({s['n_with_factor']} recorded their factor; of those "
          f"{s['n_current_era']} calibrated since {result['retired_at']})")
    if s["n_bad_integrity"]:
        print(f"   [FAIL] {s['n_bad_integrity']} rows do not match the factor "
              f"they recorded — the write path is miscomputing confidence")
    elif s["n_current_era"] == 0:
        print("   [PASS] no confidence calibrated since the retirement deploy "
              "— nothing for the current factor to have got wrong")
    elif s["ok"]:
        print(f"   [PASS] all {s['n_current_era']} post-retirement "
              f"calibrations used factor {args.factor:.4f}")
    else:
        print(f"   [FAIL] {s['n_bad']} post-retirement calibrations used a "
              f"factor other than {args.factor:.4f} — the deployment is "
              f"still scaling with a stale override")
    print()
    print(f"## Step 2 — Counterfactual (apply "
          f"{args.counterfactual_factor:.4f} to pre-F058 reviewed)")
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
    args.out.write_text("\n".join(_build_md(result, args.factor,
                                            args.counterfactual_factor)),
                        encoding="utf-8")
    print(f"\nWrote: {args.out}")
    print(f"Wrote: {args.out_json}")

    if args.strict:
        return verdict_exit_code(result)
    return 0


def _build_md(result: dict, factor: float,
              counterfactual_factor: float = _HISTORICAL_F058_FACTOR,
              ) -> list[str]:
    s = result["sanity"]
    cf = result["counterfactual"]
    pd = result["post_f058_direction"]
    if s["n_bad_integrity"]:
        sanity_line = (f"- **FAIL** {s['n_bad_integrity']} rows do not match "
                       f"the factor they recorded — the write path is "
                       f"miscomputing `confidence`")
    elif s["n_current_era"] == 0:
        sanity_line = ("- **PASS** no confidence calibrated since the "
                       "retirement deploy — nothing for the current factor to "
                       "have got wrong")
    elif s["ok"]:
        sanity_line = (f"- **PASS** all {s['n_current_era']} post-retirement "
                       f"calibrations used factor `{factor:.4f}`")
    else:
        sanity_line = (f"- **FAIL** {s['n_bad']} post-retirement calibrations "
                       f"used a factor other than `{factor:.4f}` — the "
                       f"deployment is still scaling with a stale override")
    md = [
        "# F058 calibration validation",
        f"- agent_id: `{result['agent_id']}`",
        f"- factor: **{factor}**",
        f"- counterfactual factor: **{counterfactual_factor}**",
        "",
        "## Step 1 — Sanity (factor applied in prod)",
        f"- retired at: `{result['retired_at']}`",
        f"- post-F058 rows: {s['n_post_f058']} "
        f"({s['n_with_factor']} recorded their factor; of those "
        f"{s['n_current_era']} calibrated since retirement)",
        sanity_line,
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
            f"- Hypothesis tested: `confidence * {counterfactual_factor:.4f}`. "
            "Aggregate gap collapses to ~0 by construction "
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
