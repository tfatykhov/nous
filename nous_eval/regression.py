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

    # Read-only (don't exit non-zero)
    python -m nous_eval.regression --report-only

Schema assumption: rows in `nous_system.eval_runs.metrics` map config name
to a dict containing `metrics.mrr` (and optionally other metrics). Both the
F051 retrieval harness (`harness=retrieval`) and F051.4 multi-turn harness
(`harness=multi_turn_eval`) write this shape.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from nous.config import Settings
from nous.storage.database import Database
from nous_eval.config import EvalSettings
from nous_eval.retrieval_runner import _settings_for_eval_db

logger = logging.getLogger(__name__)


# Metrics tracked for regression. Each maps DB-row metric key → human-readable
# label. Only `mrr` is gating by default; others are reported but not gated.
_TRACKED_METRICS = {
    "mrr": "MRR",
    "r_at_10": "R@10",
    "p_at_1": "P@1",
    "ndcg_at_10": "nDCG@10",
}


@dataclass(frozen=True)
class _RunRow:
    created_at: datetime
    git_sha: str
    harness: str  # "retrieval" or "multi_turn_eval"
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
                # Both are returned as Python objects by asyncpg.
                configs = configs_json or []
                metrics_by_config = metrics_json or {}
                for cfg in configs:
                    cfg_name = cfg.get("name") if isinstance(cfg, dict) else None
                    if not cfg_name:
                        continue
                    harness = (cfg.get("harness") or "retrieval") if isinstance(cfg, dict) else "retrieval"
                    if harness_filter and harness != harness_filter:
                        continue
                    if config_filter and cfg_name not in config_filter:
                        continue
                    cfg_metrics = (
                        metrics_by_config.get(cfg_name, {}).get("metrics", {})
                        if isinstance(metrics_by_config, dict) else {}
                    )
                    rows.append(_RunRow(
                        created_at=created_at,
                        git_sha=git_sha or "?",
                        harness=harness,
                        config_name=cfg_name,
                        metrics={k: float(cfg_metrics.get(k, 0.0)) for k in _TRACKED_METRICS},
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
    """
    if not bucket:
        return None
    bucket_sorted = sorted(bucket, key=lambda r: r.created_at)
    latest = bucket_sorted[-1]
    cutoff = latest.created_at - timedelta(hours=min_baseline_age_hours)
    candidates = [r for r in bucket_sorted[:-1] if r.created_at <= cutoff]
    baseline = candidates[-1] if candidates else None

    deltas: dict[str, float] = {}
    regressions: list[str] = []
    if baseline is not None:
        for metric in _TRACKED_METRICS:
            delta = latest.metrics.get(metric, 0.0) - baseline.metrics.get(metric, 0.0)
            deltas[metric] = delta
        # Gate on the primary metric only. Other metrics are informational.
        primary_delta = deltas.get(primary_metric, 0.0)
        if primary_delta < -abs(threshold):
            regressions.append(primary_metric)

    return _Comparison(
        config_name=latest.config_name,
        harness=latest.harness,
        latest=latest,
        baseline=baseline,
        deltas=deltas,
        regressions=regressions,
        is_regression=bool(regressions),
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
        f"_threshold: {primary_metric} drop > {threshold*100:.1f}% triggers regression_"
    )
    lines.append("")

    if not comparisons:
        lines.append("No comparable runs found in `nous_system.eval_runs`. ")
        lines.append("Run the harness at least twice (separated by >=1 day) to populate baselines.")
        return "\n".join(lines)

    header = "| harness | config | latest_MRR | baseline_MRR | d_MRR | d_R@10 | d_P@1 | status |"
    sep    = "|---|---|---:|---:|---:|---:|---:|---|"
    lines.append(header)
    lines.append(sep)

    for c in comparisons:
        lat_mrr = c.latest.metrics.get("mrr", 0.0)
        if c.baseline is None:
            lines.append(
                f"| {c.harness} | {c.config_name} | {lat_mrr:.3f} | _no baseline_ | "
                f"-- | -- | -- | _new_ |"
            )
            continue
        bas_mrr = c.baseline.metrics.get("mrr", 0.0)
        d_mrr = c.deltas.get("mrr", 0.0)
        d_r = c.deltas.get("r_at_10", 0.0)
        d_p = c.deltas.get("p_at_1", 0.0)
        status = "REGRESSION" if c.is_regression else "ok"
        lines.append(
            f"| {c.harness} | {c.config_name} | {lat_mrr:.3f} | {bas_mrr:.3f} | "
            f"{d_mrr:+.3f} | {d_r:+.3f} | {d_p:+.3f} | {status} |"
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
             "(default 12 — avoids same-day re-runs masking each other).",
    )
    p.add_argument(
        "--threshold", type=float, default=0.03,
        help="MRR drop threshold for regression (default 0.03 = 3 percentage points).",
    )
    p.add_argument(
        "--primary-metric", default="mrr",
        choices=sorted(_TRACKED_METRICS.keys()),
        help="Metric to gate on (default mrr; others reported but not gated).",
    )
    p.add_argument(
        "--harness", default=None, choices=["retrieval", "multi_turn_eval"],
        help="Filter to one harness type (default: both).",
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
    return p.parse_args(argv)


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
