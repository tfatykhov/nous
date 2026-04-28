"""F051 Phase 1 finish: regression detection across eval runs.

Reads `nous_system.eval_runs` (eval DB) and compares the LATEST row's metrics
against the LATEST row from N days ago (default 7) for the same `(harness,
config_name, agent_id)` triple. Exits non-zero when ANY config regresses
beyond a threshold, so this script can gate CI / weekly cron jobs.

Examples:

    # Default: compare latest vs 7 days ago, MRR drop > 3% triggers fail
    python -m nous_eval.regression

    # Tighter — fail on any drop > 1%, look back 14 days
    python -m nous_eval.regression --threshold 0.01 --days 14

    # Per-config / per-harness filtering
    python -m nous_eval.regression --harness multi_turn_eval --configs baseline,f055_on

    # Per-handler eval (F056)
    python -m nous_eval.regression --harness admission --primary-metric admission_f1

    # Read-only (don't exit non-zero)
    python -m nous_eval.regression --report-only

Schema assumption: rows in `nous_system.eval_runs.metrics` map config name
to a dict containing the harness-specific scalar metrics. The F051 retrieval
harness writes `mrr/r_at_10/p_at_1/ndcg_at_10`; F051.4 multi-turn writes the
same set; F056 handlers write handler-specific metrics (e.g. `admission_f1`).

Per-harness metric registries (F056):
- `_PRIMARY_METRIC_BY_HARNESS` — which scalar gates the run for each harness.
- `_ALL_REPORTED_METRICS_BY_HARNESS` — which scalars surface in delta tables.
  Sub-payloads (e.g. `confusion_matrix` dict) live in raw JSONB and are
  excluded from delta tables by design.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text

from nous.config import Settings
from nous.storage.database import Database
from nous_eval.config import EvalSettings
from nous_eval.retrieval_runner import _settings_for_eval_db

logger = logging.getLogger(__name__)


# F056: which scalar gates the run for each harness type. Lookup at fetch time.
_PRIMARY_METRIC_BY_HARNESS: dict[str, str] = {
    "retrieval": "mrr",
    "multi_turn_eval": "mrr",
    "admission": "admission_f1",
    "dedup": "dedup_f1",
    "backfill": "edge_precision",
    "summary": "summary_quality",
}

# F056: which scalar metrics surface in delta-table reports per harness.
# Sub-payloads (dicts/structs like admission's confusion_matrix) live in raw
# JSONB and are intentionally excluded — delta tables only handle scalars.
_ALL_REPORTED_METRICS_BY_HARNESS: dict[str, tuple[str, ...]] = {
    "retrieval":       ("mrr", "r_at_10", "p_at_1", "ndcg_at_10"),
    "multi_turn_eval": ("mrr", "r_at_10", "p_at_1", "ndcg_at_10"),
    "admission":       ("admission_f1", "admission_precision", "admission_recall"),
    "dedup":           ("dedup_f1", "dedup_f1_leg1", "dedup_f1_leg2"),
    "backfill":        ("edge_precision", "orphan_resolution_rate", "density_delta"),
    "summary":         ("summary_quality", "mean_key_point_coverage", "mean_summary_faithfulness"),
}

# Default fallback harness for legacy rows written before PR #368 added the
# `harness` key to configs payload. Behavior matches pre-F056 regression.py.
_DEFAULT_HARNESS = "retrieval"


def _reported_metrics_for(harness: str) -> tuple[str, ...]:
    """Look up the scalar metric tuple for a harness; fall back to retrieval set."""
    return _ALL_REPORTED_METRICS_BY_HARNESS.get(
        harness, _ALL_REPORTED_METRICS_BY_HARNESS[_DEFAULT_HARNESS]
    )


def _primary_metric_for(harness: str) -> str:
    """Look up the gating metric for a harness; fall back to retrieval set."""
    return _PRIMARY_METRIC_BY_HARNESS.get(harness, _PRIMARY_METRIC_BY_HARNESS[_DEFAULT_HARNESS])


def _validate_primary_metric(value: str, harness: str | None) -> str:
    """Resolve `--primary-metric` value, errors clearly on unknown metric.

    `value="auto"` → return registry default for `harness`.
    `value` in `_ALL_REPORTED_METRICS_BY_HARNESS[harness]` → return as-is.
    `value` valid for SOME harness but not the requested one → still accept
        (multi-harness reports may need to compare across harness types).
    `value` not in any registry → raise.

    `harness=None` (multi-harness run) → accept any value present in at least
    one harness's reported metrics.
    """
    if value == "auto":
        if harness is None:
            # Multi-harness: caller must pass --harness or use per-row primary
            # via `_primary_metric_for(row.harness)` later.
            return "auto"
        return _primary_metric_for(harness)

    all_known: set[str] = set()
    for tup in _ALL_REPORTED_METRICS_BY_HARNESS.values():
        all_known.update(tup)
    if value not in all_known:
        raise argparse.ArgumentTypeError(
            f"--primary-metric={value!r} is not a known metric. "
            f"Known: {sorted(all_known)}"
        )
    return value


@dataclass(frozen=True)
class _RunRow:
    created_at: datetime
    git_sha: str
    harness: str  # "retrieval", "multi_turn_eval", "admission", "dedup", "backfill", "summary"
    config_name: str
    metrics: dict[str, float]
    report_path: str | None


@dataclass(frozen=True)
class _Comparison:
    config_name: str
    harness: str
    latest: _RunRow
    baseline: _RunRow | None
    deltas: dict[str, float]  # metric → (latest - baseline)
    regressions: list[str]  # metric names that crossed threshold
    is_regression: bool
    primary_metric: str = "mrr"  # which metric was gated


async def _fetch_rows(
    eval_settings: EvalSettings,
    main_settings: Settings,
    *,
    harness_filter: str | None,
    config_filter: set[str] | None,
    cutoff_days: int,
) -> list[_RunRow]:
    """Read all eval_runs rows since `cutoff_days` ago, flattened by config."""
    db_settings = _settings_for_eval_db(eval_settings, main_settings)
    db = Database(db_settings)
    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=cutoff_days)

    rows: list[_RunRow] = []
    try:
        await db.connect()
    except Exception as exc:
        logger.error("F051-regression: eval DB unreachable: %s", exc)
        await db.engine.dispose()
        return []

    try:
        async with db.session() as session:
            result = await session.execute(
                text(
                    """
                    SELECT created_at, git_sha, configs, metrics, report_path
                    FROM nous_system.eval_runs
                    WHERE agent_id = :agent_id
                      AND created_at >= :cutoff
                    ORDER BY created_at ASC
                    """
                ),
                {"agent_id": eval_settings.agent_id, "cutoff": cutoff},
            )
            for created_at, git_sha, configs_json, metrics_json, report_path in result.all():
                # configs is a JSONB list; metrics is a JSONB dict keyed by config name.
                configs = configs_json or []
                metrics_by_config = metrics_json or {}
                for cfg in configs:
                    cfg_name = cfg.get("name") if isinstance(cfg, dict) else None
                    if not cfg_name:
                        continue
                    # F056: harness-aware. Legacy rows lacking the key default
                    # to "retrieval" — preserves pre-PR-#368 behavior.
                    harness = (
                        cfg.get("harness", _DEFAULT_HARNESS)
                        if isinstance(cfg, dict) else _DEFAULT_HARNESS
                    )
                    if harness_filter and harness != harness_filter:
                        continue
                    if config_filter and cfg_name not in config_filter:
                        continue
                    cfg_metrics = (
                        metrics_by_config.get(cfg_name, {}).get("metrics", {})
                        if isinstance(metrics_by_config, dict) else {}
                    )
                    # F056: only fetch the scalar metrics this harness reports.
                    # Sub-payloads (dicts) stay in raw JSONB, never enter _RunRow.
                    reported = _reported_metrics_for(harness)
                    rows.append(_RunRow(
                        created_at=created_at,
                        git_sha=git_sha or "?",
                        harness=harness,
                        config_name=cfg_name,
                        metrics={k: float(cfg_metrics.get(k, 0.0)) for k in reported},
                        report_path=report_path,
                    ))
    finally:
        await db.engine.dispose()
    return rows


def _bucketize(rows: list[_RunRow]) -> dict[tuple[str, str], list[_RunRow]]:
    """Group rows by (harness, config_name)."""
    buckets: dict[tuple[str, str], list[_RunRow]] = {}
    for r in rows:
        buckets.setdefault((r.harness, r.config_name), []).append(r)
    return buckets


def _compare_bucket(
    bucket: list[_RunRow],
    *,
    threshold: float,
    primary_metric: str,
    min_baseline_age_hours: float,
) -> _Comparison | None:
    """Compare the most recent row to the most recent row at least N hours older.

    Returns None when there's no baseline to compare to (single row in bucket
    or all rows clustered within the min-age window — fresh table edge case).

    F056: deltas are computed over the harness-specific reported metrics
    (not the global retrieval set). `primary_metric="auto"` resolves per-row
    via the harness registry.
    """
    if not bucket:
        return None
    bucket_sorted = sorted(bucket, key=lambda r: r.created_at)
    latest = bucket_sorted[-1]
    cutoff = latest.created_at - timedelta(hours=min_baseline_age_hours)
    candidates = [r for r in bucket_sorted[:-1] if r.created_at <= cutoff]
    baseline = candidates[-1] if candidates else None

    # F056: use the harness's reported metric set, not a global constant.
    reported = _reported_metrics_for(latest.harness)
    effective_primary = (
        _primary_metric_for(latest.harness) if primary_metric == "auto" else primary_metric
    )

    deltas: dict[str, float] = {}
    regressions: list[str] = []
    if baseline is not None:
        for metric in reported:
            delta = latest.metrics.get(metric, 0.0) - baseline.metrics.get(metric, 0.0)
            deltas[metric] = delta
        primary_delta = deltas.get(effective_primary, 0.0)
        if primary_delta < -abs(threshold):
            regressions.append(effective_primary)

    return _Comparison(
        config_name=latest.config_name,
        harness=latest.harness,
        latest=latest,
        baseline=baseline,
        deltas=deltas,
        regressions=regressions,
        is_regression=bool(regressions),
        primary_metric=effective_primary,
    )


def _format_report(
    comparisons: list[_Comparison],
    *,
    threshold: float,
    primary_metric: str,
    days: int,
) -> str:
    lines: list[str] = []
    lines.append(f"# F051 regression report - {datetime.now(tz=timezone.utc):%Y-%m-%d %H:%M:%S} UTC")
    lines.append("")
    lines.append(
        f"_Comparing latest run to most-recent run >={days*24:.0f}h older_  "
        f"_threshold: primary metric drop > {threshold*100:.1f}% triggers regression_"
    )
    lines.append("")

    if not comparisons:
        lines.append("No comparable runs found in `nous_system.eval_runs`. ")
        lines.append("Run the harness at least twice (separated by >=1 day) to populate baselines.")
        return "\n".join(lines)

    # F056: group by harness so each section gets harness-specific headers.
    by_harness: dict[str, list[_Comparison]] = {}
    for c in comparisons:
        by_harness.setdefault(c.harness, []).append(c)

    for harness in sorted(by_harness):
        section = by_harness[harness]
        reported = _reported_metrics_for(harness)
        primary = _primary_metric_for(harness) if primary_metric == "auto" else primary_metric

        lines.append(f"## {harness}")
        lines.append("")
        # Build dynamic headers: config | latest_<primary> | baseline_<primary> | d_<each metric> | status
        delta_headers = " | ".join(f"d_{m}" for m in reported)
        lines.append(
            f"| config | latest_{primary} | baseline_{primary} | {delta_headers} | status |"
        )
        sep = "|---|---:|---:|" + "|".join("---:" for _ in reported) + "|---|"
        lines.append(sep)

        for c in section:
            lat_primary = c.latest.metrics.get(primary, 0.0)
            if c.baseline is None:
                empty_deltas = " | ".join("--" for _ in reported)
                lines.append(
                    f"| {c.config_name} | {lat_primary:.3f} | _no baseline_ | "
                    f"{empty_deltas} | _new_ |"
                )
                continue
            bas_primary = c.baseline.metrics.get(primary, 0.0)
            delta_cells = " | ".join(f"{c.deltas.get(m, 0.0):+.3f}" for m in reported)
            status = "REGRESSION" if c.is_regression else "ok"
            lines.append(
                f"| {c.config_name} | {lat_primary:.3f} | {bas_primary:.3f} | "
                f"{delta_cells} | {status} |"
            )
        lines.append("")

    n_regressions = sum(1 for c in comparisons if c.is_regression)
    if n_regressions:
        lines.append(f"**{n_regressions} regression(s) detected.**")
    else:
        lines.append("No regressions detected.")
    return "\n".join(lines)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="python -m nous_eval.regression")
    p.add_argument(
        "--days", type=int, default=7,
        help="Look back N days for baseline rows (default 7).",
    )
    p.add_argument(
        "--min-baseline-age-hours", type=float, default=12.0,
        help="Baseline must be at least this many hours older than latest "
             "(default 12 - avoids same-day re-runs masking each other).",
    )
    p.add_argument(
        "--threshold", type=float, default=0.03,
        help="Primary-metric drop threshold for regression (default 0.03 = 3 percentage points).",
    )
    p.add_argument(
        # F056: free-form (was choices= over _PRIMARY_METRIC_BY_HARNESS keys).
        # Validation happens after parse_args via _validate_primary_metric.
        "--primary-metric", default="auto", type=str,
        help="Metric to gate on (default 'auto' - resolved from --harness via "
             "_PRIMARY_METRIC_BY_HARNESS registry; pass an explicit metric to override).",
    )
    p.add_argument(
        # F056: free-form (was choices=["retrieval", "multi_turn_eval"]).
        "--harness", default=None, type=str,
        help="Filter to one harness type (default: all). Known harnesses: "
             + ", ".join(sorted(_PRIMARY_METRIC_BY_HARNESS)),
    )
    p.add_argument(
        "--configs", default=None,
        help="Comma-separated config names to compare (default: all).",
    )
    p.add_argument(
        "--report-only", action="store_true",
        help="Print report but exit 0 even if regressions found.",
    )
    p.add_argument(
        "--log-level", default="INFO",
    )
    args = p.parse_args(argv)

    # F056: post-parse validation since --primary-metric depends on --harness.
    args.primary_metric = _validate_primary_metric(args.primary_metric, args.harness)
    return args


def main(argv: list[str] | None = None) -> int:
    ns = _parse_args(argv)
    logging.basicConfig(
        level=ns.log_level.upper(),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    eval_settings = EvalSettings()
    main_settings = Settings()
    config_filter = (
        {c.strip() for c in ns.configs.split(",") if c.strip()}
        if ns.configs else None
    )

    rows = asyncio.run(_fetch_rows(
        eval_settings=eval_settings,
        main_settings=main_settings,
        harness_filter=ns.harness,
        config_filter=config_filter,
        cutoff_days=ns.days,
    ))

    buckets = _bucketize(rows)
    comparisons: list[_Comparison] = []
    for bucket in buckets.values():
        comp = _compare_bucket(
            bucket,
            threshold=ns.threshold,
            primary_metric=ns.primary_metric,
            min_baseline_age_hours=ns.min_baseline_age_hours,
        )
        if comp is not None:
            comparisons.append(comp)

    # Sort: regressions first, then by harness/config
    comparisons.sort(key=lambda c: (not c.is_regression, c.harness, c.config_name))

    print(_format_report(
        comparisons,
        threshold=ns.threshold,
        primary_metric=ns.primary_metric,
        days=ns.days,
    ))

    if ns.report_only:
        return 0
    return 4 if any(c.is_regression for c in comparisons) else 0


if __name__ == "__main__":
    raise SystemExit(main())
