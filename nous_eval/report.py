"""Report rendering — markdown + JSON — and the F050 gate decision.

The harness writes two files per run:

- ``reports/<utc_ts>_<configs>.md`` — primary human-readable surface, used by
  the operator to eyeball deltas before merging a retrieval-touching PR.
- ``reports/<utc_ts>_<configs>.json`` — full per-qrel per-config grid, also
  persisted to ``nous_system.eval_runs`` for historical regression analysis.

The :func:`decide_gate_f050` function encodes the F050 enable-gate rules
documented in the F051 spec §"Resolved decisions" #7 plus plan v2.1
delta #7 (the N=2-sources edge case for ``require_majority_positive``).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from nous_eval.metrics import (
    MetricsResult,
    compute_delta,
    compute_metrics,
    filter_by_sources,
)

if TYPE_CHECKING:
    from nous_eval.retrieval_runner import RunResult
    from nous_eval.source_registry import ResolvedSource

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Gate decision data structure
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GateDecision:
    """Outcome of a feature-gate evaluation (e.g. F050 enable check).

    ``passed`` is the merge signal. ``reason`` is a human-readable
    explanation suitable for printing alongside the markdown report.
    ``per_source_deltas`` is included so the operator can audit the
    decision without re-running the gate logic.
    """

    feature: str
    passed: bool
    reason: str
    aggregate_delta_pct: float = 0.0
    per_source_deltas: dict[str, float] = field(default_factory=dict)
    n_gate_eligible_sources: int = 0


# ---------------------------------------------------------------------------
# F050 gate logic
# ---------------------------------------------------------------------------


def decide_gate_f050(
    run_results: list["RunResult"],
    resolved_sources: list["ResolvedSource"],
    threshold: float = 0.07,
    max_single_regression: float = 0.03,
    require_majority_positive: bool = True,
    top_k: int = 10,
) -> GateDecision:
    """Evaluate F050's enable-gate against a baseline / experimental run pair.

    Three rules, all must pass:

    1. **Aggregate MRR delta >= threshold** (default +7%) over the union of
       gate-eligible sources.
    2. **No single gate-eligible source regresses by more than
       max_single_regression** (default 3%).
    3. **Majority of gate-eligible sources have positive delta** if
       ``require_majority_positive=True``. Note: with N=2 sources this
       reduces to "both must be positive", which is *stricter* than
       majority — documented edge case from plan v2.1 delta #7.

    Args:
        run_results: List of RunResult — must contain configs named
            ``"baseline"`` and ``"f050_on"``. Other configs ignored.
        resolved_sources: All sources resolved this run; used to identify
            which qrel sources count for the gate (``gate_eligible_effective``).
        threshold: Minimum aggregate MRR relative delta (fraction, e.g. 0.07).
        max_single_regression: Max allowed per-source negative delta
            (fraction).
        require_majority_positive: Require majority of sources to have
            positive delta.
        top_k: Retrieval cutoff used by the matrix run. Passed into
            ``compute_metrics`` so gate math is evaluated at the same K
            the runner scored at; mismatching this with the runner's
            ``eval_settings.top_k`` produces inconsistent pass/fail decisions
            (Codex P2 fix).
    """
    base = next((r for r in run_results if r.config.name == "baseline"), None)
    exp = next((r for r in run_results if r.config.name == "f050_on"), None)
    if base is None or exp is None:
        return GateDecision(
            feature="F050",
            passed=False,
            reason="missing baseline or f050_on RunResult",
        )

    gate_sources = [
        s for s in resolved_sources if s.gate_eligible_effective and s.available
    ]
    if not gate_sources:
        return GateDecision(
            feature="F050",
            passed=False,
            reason="no gate-eligible sources available",
        )
    gate_source_names = {s.spec.name for s in gate_sources}

    # --- Rule 1: aggregate ---
    base_filtered = filter_by_sources(base.per_qrel, gate_source_names)
    exp_filtered = filter_by_sources(exp.per_qrel, gate_source_names)
    base_agg = compute_metrics(base_filtered, top_k=top_k)
    exp_agg = compute_metrics(exp_filtered, top_k=top_k)
    agg_delta = compute_delta(base_agg, exp_agg, "mrr")

    if agg_delta.relative_pct < threshold * 100.0:
        return GateDecision(
            feature="F050",
            passed=False,
            reason=(
                f"aggregate MRR delta {agg_delta.relative_pct:+.1f}% "
                f"< threshold +{threshold * 100:.1f}%"
            ),
            aggregate_delta_pct=agg_delta.relative_pct,
            n_gate_eligible_sources=len(gate_sources),
        )

    # --- Rules 2 + 3: per-source ---
    per_source_deltas: dict[str, float] = {}
    for src in gate_sources:
        src_name = src.spec.name
        base_src = compute_metrics(
            filter_by_sources(base.per_qrel, {src_name}), top_k=top_k
        )
        exp_src = compute_metrics(
            filter_by_sources(exp.per_qrel, {src_name}), top_k=top_k
        )
        d = compute_delta(base_src, exp_src, "mrr")
        per_source_deltas[src_name] = d.relative_pct

    # Rule 2: single-source regression
    for src_name, pct in per_source_deltas.items():
        if pct < -max_single_regression * 100.0:
            return GateDecision(
                feature="F050",
                passed=False,
                reason=(
                    f"single-source regression: {src_name} {pct:+.1f}% "
                    f"(limit -{max_single_regression * 100:.1f}%)"
                ),
                aggregate_delta_pct=agg_delta.relative_pct,
                per_source_deltas=per_source_deltas,
                n_gate_eligible_sources=len(gate_sources),
            )

    # Rule 3: majority positive
    if require_majority_positive:
        n_positive = sum(1 for pct in per_source_deltas.values() if pct > 0)
        if n_positive * 2 <= len(per_source_deltas):
            return GateDecision(
                feature="F050",
                passed=False,
                reason=(
                    f"only {n_positive}/{len(per_source_deltas)} sources "
                    f"positive (need majority)"
                ),
                aggregate_delta_pct=agg_delta.relative_pct,
                per_source_deltas=per_source_deltas,
                n_gate_eligible_sources=len(gate_sources),
            )

    return GateDecision(
        feature="F050",
        passed=True,
        reason=(
            f"aggregate MRR {agg_delta.relative_pct:+.1f}% >= "
            f"+{threshold * 100:.1f}%; all per-source deltas within bounds"
        ),
        aggregate_delta_pct=agg_delta.relative_pct,
        per_source_deltas=per_source_deltas,
        n_gate_eligible_sources=len(gate_sources),
    )


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------


def render_markdown(
    run_results: list["RunResult"],
    resolved_sources: list["ResolvedSource"],
    git_sha: str = "",
    fixture_version: str = "",
    gate_decision: GateDecision | None = None,
    notes: str = "",
    config_names_requested: list[str] | None = None,
) -> str:
    """Render the markdown report.

    Layout follows F051 spec §8: header, gate aggregate table, per-source
    breakdown, per-reasoning-type directional table.
    """
    ts = datetime.now(tz=timezone.utc).isoformat(timespec="seconds")
    lines: list[str] = []
    lines.append(f"# F051 retrieval eval — {ts}")
    if git_sha:
        lines.append(f"- git_sha: `{git_sha}`")
    if fixture_version:
        lines.append(f"- fixture_version: `{fixture_version}`")
    # Show the configs that were *requested* (so smoke mode without a DB
    # still renders a useful header) plus a note if the matrix was skipped.
    if run_results:
        names = [r.config.name for r in run_results]
    else:
        names = list(config_names_requested or [])
    lines.append(f"- configs: {', '.join(names) if names else '(none)'}")
    if not run_results:
        lines.append(
            "- note: matrix was not executed (smoke mode + eval DB unreachable)"
        )
    if notes:
        lines.append(f"- notes: {notes}")
    lines.append("")

    # Source summary
    lines.append("## Sources")
    for s in resolved_sources:
        gate_tag = "gate-eligible" if s.gate_eligible_effective else "informational"
        avail = "available" if s.available else f"skipped ({s._skip_reason})"
        lines.append(f"- `{s.spec.name}` [{gate_tag}] — {avail}")
    lines.append("")

    # Per-config aggregate metrics
    lines.append("## Aggregate metrics (all qrels)")
    lines.append(_metrics_table([r for r in run_results]))
    lines.append("")

    # Pairwise delta if exactly two configs
    if len(run_results) == 2:
        base, exp = run_results[0], run_results[1]
        base_m = compute_metrics(base.per_qrel)
        exp_m = compute_metrics(exp.per_qrel)
        lines.append(f"## Delta: {base.config.name} → {exp.config.name}")
        lines.append(_delta_table(base_m, exp_m))
        lines.append("")

    # Gate decision
    if gate_decision is not None:
        verdict = "PASS" if gate_decision.passed else "FAIL"
        lines.append(f"## {gate_decision.feature} gate — {verdict}")
        lines.append(f"- reason: {gate_decision.reason}")
        if gate_decision.per_source_deltas:
            lines.append("- per-source deltas (MRR %):")
            for src, pct in sorted(gate_decision.per_source_deltas.items()):
                lines.append(f"  - `{src}`: {pct:+.1f}%")
        lines.append("")

    return "\n".join(lines) + "\n"


def _metrics_table(run_results: list["RunResult"]) -> str:
    """Build a markdown table with metrics for each config."""
    header = (
        "| config | n_qrels | n_errored | MRR | P@1 | P@10 | R@10 | nDCG@10 |\n"
        "|---|---:|---:|---:|---:|---:|---:|---:|"
    )
    rows = []
    for r in run_results:
        m = compute_metrics(r.per_qrel)
        rows.append(
            f"| {r.config.name} | {m.n_qrels} | {m.n_errored} | "
            f"{m.mrr:.3f} | {m.p_at_1:.3f} | {m.p_at_10:.3f} | "
            f"{m.r_at_10:.3f} | {m.ndcg_at_10:.3f} |"
        )
    return header + "\n" + "\n".join(rows)


def _delta_table(base: MetricsResult, exp: MetricsResult) -> str:
    """Build a metric-by-metric delta table."""
    rows = ["| metric | baseline | experimental | Δ | Δ% |", "|---|---:|---:|---:|---:|"]
    for metric in (
        "mrr",
        "p_at_1",
        "p_at_5",
        "p_at_10",
        "r_at_1",
        "r_at_5",
        "r_at_10",
        "ndcg_at_10",
    ):
        d = compute_delta(base, exp, metric)
        rows.append(
            f"| {metric} | {d.baseline_mean:.3f} | {d.experimental_mean:.3f} | "
            f"{d.absolute:+.3f} | {d.relative_pct:+.1f}% |"
        )
    return "\n".join(rows)


# ---------------------------------------------------------------------------
# JSON rendering
# ---------------------------------------------------------------------------


def render_json(
    run_results: list["RunResult"],
    resolved_sources: list["ResolvedSource"],
    git_sha: str = "",
    fixture_version: str = "",
    gate_decision: GateDecision | None = None,
    notes: str = "",
) -> str:
    """Render the full per-qrel per-config grid as a JSON string.

    This is the structure persisted to ``nous_system.eval_runs.metrics``
    for historical regression analysis.
    """
    payload: dict = {
        "version": 1,
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "git_sha": git_sha,
        "fixture_version": fixture_version,
        "notes": notes,
        "sources": [
            {
                "name": s.spec.name,
                "gate_eligible": s.gate_eligible_effective,
                "available": s.available,
                "skip_reason": s._skip_reason,
                "resolved_path": str(s.resolved_path),
            }
            for s in resolved_sources
        ],
        "configs": [
            {
                "name": r.config.name,
                "flags": r.config.flags,
                "description": r.config.description,
                "duration_seconds": r.duration_seconds,
                "pipeline_stats_summary": r.pipeline_stats_summary,
                "metrics": _metrics_to_dict(compute_metrics(r.per_qrel)),
                "per_qrel": [
                    {
                        "index": q.qrel_index,
                        "query": q.qrel_query,
                        "source": q.qrel_source,
                        "gold_ids": [str(g) for g in q.gold_ids],
                        "retrieved_ids": [str(rid) for rid in q.retrieved_ids],
                        "retrieved_types": q.retrieved_types,
                        "rank_of_first_gold": q.rank_of_first_gold,
                        "n_gold_in_top_k": q.n_gold_in_top_k,
                        "n_gold_total": q.n_gold_total,
                        "error": q.error,
                    }
                    for q in r.per_qrel
                ],
            }
            for r in run_results
        ],
    }
    if gate_decision is not None:
        payload["gate_decision"] = {
            "feature": gate_decision.feature,
            "passed": gate_decision.passed,
            "reason": gate_decision.reason,
            "aggregate_delta_pct": gate_decision.aggregate_delta_pct,
            "per_source_deltas": gate_decision.per_source_deltas,
            "n_gate_eligible_sources": gate_decision.n_gate_eligible_sources,
        }
    return json.dumps(payload, indent=2, default=str)


def _metrics_to_dict(m: MetricsResult) -> dict[str, float | int]:
    return {
        "mrr": m.mrr,
        "p_at_1": m.p_at_1,
        "p_at_5": m.p_at_5,
        "p_at_10": m.p_at_10,
        "r_at_1": m.r_at_1,
        "r_at_5": m.r_at_5,
        "r_at_10": m.r_at_10,
        "ndcg_at_10": m.ndcg_at_10,
        "n_qrels": m.n_qrels,
        "n_errored": m.n_errored,
    }


# ---------------------------------------------------------------------------
# File-system helpers
# ---------------------------------------------------------------------------


def write_reports(
    report_dir: Path,
    md_content: str,
    json_content: str,
    config_names: list[str],
) -> tuple[Path, Path]:
    """Write the markdown + JSON report to ``<report_dir>/<ts>_<configs>.{md,json}``.

    Returns the two written paths.
    """
    report_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")
    suffix = "-".join(config_names) or "run"
    base_name = f"{ts}_{suffix}"
    md_path = report_dir / f"{base_name}.md"
    json_path = report_dir / f"{base_name}.json"
    md_path.write_text(md_content, encoding="utf-8")
    json_path.write_text(json_content, encoding="utf-8")
    return md_path, json_path
