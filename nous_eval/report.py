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
    RECALL_CURVE_KS,
    MetricsResult,
    compute_delta,
    compute_metrics,
    filter_by_sources,
    leg_visibility,
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

    Four rules, all must pass:

    0. **No partial retrievals** on gate-eligible qrels in either config
       (N1). A stage failure invalidates the comparison, and the gate must
       say so too — otherwise a crashed leg yields a passing merge signal
       under a report that calls its own metrics invalid.
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

    # --- Rule 0: no partial retrievals (N1/codex-R6) ---
    # A stage failure makes the comparison meaningless, and the markdown
    # already says so. Without this the gate would still compute a normal
    # delta and return SUCCESS, so a crashed fact leg could produce a
    # passing automated merge signal while the report it accompanies
    # declares the metrics invalid. The gate is the machine-readable
    # output CI acts on — it must agree with the prose.
    partial: dict[str, int] = {}
    for run in (base, exp):
        n_partial = sum(
            1
            for q in filter_by_sources(run.per_qrel, gate_source_names)
            if q.stage_errors
        )
        if n_partial:
            partial[run.config.name] = n_partial
    if partial:
        detail = ", ".join(f"{k}={v}" for k, v in sorted(partial.items()))
        return GateDecision(
            feature="F050",
            passed=False,
            reason=(
                "partial retrieval on gate-eligible qrels "
                f"({detail}) — a stage failed, so the comparison is invalid"
            ),
            n_gate_eligible_sources=len(gate_sources),
        )

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
    top_k: int = 10,
) -> str:
    """Render the markdown report.

    Layout follows F051 spec §8: header, gate aggregate table, per-source
    breakdown, per-reasoning-type directional table.

    ``top_k`` is the depth the matrix actually scored at (``EvalSettings.top_k``,
    overridable via ``--top-k``). It is the cutline the N7 leg-visibility table
    judges against — defaulting it to 10 when the run scored at 20 would report
    a leg with median rank 15 as invisible even though the experiment measured
    it, turning a real null into a false "inconclusive".
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

    # N1/codex-R2: partial-run banner FIRST — an operator must see that a
    # leg crashed before reading any metric computed over the wreckage.
    _partial = _stage_error_summary(run_results)
    if _partial:
        lines.append(_partial)

    # Per-config aggregate metrics
    lines.append(f"## Aggregate metrics (all qrels, scored at k={top_k})")
    lines.append(_metrics_table([r for r in run_results], top_k))
    lines.append("")

    # Pairwise delta if exactly two configs
    if len(run_results) == 2:
        base, exp = run_results[0], run_results[1]
        base_m = compute_metrics(base.per_qrel, top_k=top_k)
        exp_m = compute_metrics(exp.per_qrel, top_k=top_k)
        lines.append(f"## Delta: {base.config.name} → {exp.config.name}")
        lines.append(_delta_table(base_m, exp_m, top_k))
        lines.append("")

    # N7: recall across k — how much a fixed cutoff cannot see.
    lines.append("## Recall over k (N7)")
    lines.append(
        "_Production does not truncate — `recall_deep` hands the model the "
        "whole returned block (median ~77 rows). Any single fixed-k row "
        "below describes a window prod never applies; read the curve to see "
        "how much that cutoff misses._"
    )
    lines.append("")
    lines.append(_recall_curve_table(run_results, top_k))
    lines.append("")
    _leg_table = _leg_visibility_table(run_results, top_k)
    if _leg_table:
        lines.append(_leg_table)
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


def _metrics_table(run_results: list["RunResult"], top_k: int = 10) -> str:
    """Build a markdown table with metrics for each config.

    This table is fixed-k by construction; the companion "Recall over k"
    table is what shows how much that cutoff misses of the block prod
    actually serves (N7).

    ``top_k`` must be the depth the matrix scored at: ``MetricsResult``'s
    ``p_at_10`` / ``r_at_10`` / ``ndcg_at_10`` fields are named for the
    default but actually hold whatever depth ``compute_metrics`` was given.
    Passing the default while the run scored at 30 would print numbers
    computed at 10 under a report that elsewhere declares ``top_k: 30`` —
    so the columns are labelled at the real depth too.
    """
    header = (
        f"| config | n_qrels | n_errored | MRR | P@1 | P@{top_k} | R@{top_k} "
        f"| nDCG@{top_k} |\n"
        "|---|---:|---:|---:|---:|---:|---:|---:|"
    )
    rows = []
    for r in run_results:
        m = compute_metrics(r.per_qrel, top_k=top_k)
        rows.append(
            f"| {r.config.name} | {m.n_qrels} | {m.n_errored} | "
            f"{m.mrr:.3f} | {m.p_at_1:.3f} | {m.p_at_10:.3f} | "
            f"{m.r_at_10:.3f} | {m.ndcg_at_10:.3f} |"
        )
    return header + "\n" + "\n".join(rows)


def _stage_error_summary(run_results: list["RunResult"]) -> str:
    """N1/codex-R2: surface partial runs in the OPERATOR-facing markdown.

    The counters reached ``pipeline_stats_summary`` and the JSON, but the
    markdown is the report an operator actually reads — and without this a
    crashed Heart leg still rendered an apparently healthy metrics table.
    That is the original N1 observability failure surviving on its most
    important consumer.
    """
    lines: list[str] = []
    for r in run_results:
        stage_keys = {
            k: v for k, v in (r.pipeline_stats_summary or {}).items()
            if k.startswith("stage_error_") and v
        }
        n_qrels_affected = sum(1 for q in r.per_qrel if q.stage_errors)
        if not stage_keys and not n_qrels_affected:
            continue
        detail = ", ".join(
            f"`{k.removeprefix('stage_error_')}`={v}"
            for k, v in sorted(stage_keys.items())
        )
        lines.append(
            f"- **{r.config.name}** — {n_qrels_affected} qrel(s) retrieved "
            f"PARTIALLY: {detail or 'see per-qrel stage_errors'}"
        )
    if not lines:
        return ""
    return (
        "\n## ⚠ Partial retrieval detected\n\n"
        "One or more retrieval legs FAILED during this run. The metrics "
        "below are computed over incomplete results and will look "
        "plausible anyway — treat them as **invalid for comparison** until "
        "the cause is fixed (a store behind the ORM's migration watermark "
        "is the usual cause).\n\n" + "\n".join(lines) + "\n"
    )


def _recall_curve_table(run_results: list["RunResult"], top_k: int = 10) -> str:
    """N7: recall over k, so the fixed-k cutoff's blind spot is visible."""
    ks = sorted(RECALL_CURVE_KS)
    header = (
        "| config | " + " | ".join(f"R@{k}" for k in ks) + " |\n"
        "|---|" + "---:|" * len(ks)
    )
    rows = []
    for r in run_results:
        # The curve itself is depth-independent (it evaluates every k in
        # RECALL_CURVE_KS), so the default here changes nothing about the
        # numbers — but pass it anyway so no future edit reintroduces a
        # silent depth mismatch between this table and its neighbours.
        m = compute_metrics(r.per_qrel, top_k=top_k)
        cells = " | ".join(f"{m.recall_curve.get(k, 0.0):.3f}" for k in ks)
        rows.append(f"| {r.config.name} | {cells} |")
    return header + "\n" + "\n".join(rows)


def _leg_visibility_table(run_results: list["RunResult"], top_k: int = 10) -> str:
    """N7 follow-up: which legs the scoring depth actually observed.

    ``top_k`` MUST be the depth the matrix scored at — the verdict inverts
    with it. Legs come from the pipeline's own ``attempted_legs`` report, so
    a leg that ran and emitted nothing is still listed.
    """
    lines = []
    for r in run_results:
        vis = leg_visibility(
            r.per_qrel, cutoff=top_k, attempted_legs=r.attempted_legs,
        )
        if not vis:
            continue
        lines.append(f"\n**{r.config.name}**\n")
        lines.append(
            "| leg | rows | qrels w/ row | qrels within k | of all qrels "
            "| participation | median rank | best rank | observed@%d |"
            % vis[0].cutoff
        )
        lines.append("|---|---:|---:|---:|---:|---:|---:|---:|:---:|")
        for v in vis:
            mark = "yes" if v.visible else "**NO**"
            # A leg that emitted nothing has no ranks — em-dashes rather
            # than 0.0/0, which would imply a rank that never existed.
            med = f"{v.median_rank:.1f}" if v.n_rows else "—"
            best = f"{v.best_rank}" if v.n_rows else "—"
            label = v.leg if v.n_rows else f"{v.leg} *(silent)*"
            lines.append(
                f"| {label} | {v.n_rows} | {v.n_qrels_present} | "
                f"{v.n_qrels_within_cutoff} | {v.n_qrels_evaluated} | "
                f"{v.participation_rate:.2f} | {med} | {best} | {mark} |"
            )
    if not lines:
        return ""
    return (
        "\n### Leg visibility (N7)\n\n"
        "**participation** is `qrels within k / ALL valid qrels` — how much "
        "of the experiment this leg could have influenced. It is NOT "
        "conditioned on the leg having emitted anything, so a leg returning "
        "rows on 1 qrel in 100 reads 0.01, not 1.00. Treat a null from a leg "
        "at or near **0.00** as **inconclusive**, not negative: the "
        "measurement never reached it. (median/best rank are diagnostics — a "
        "leg's own long tail can drag its median below the cutline while its "
        "head scores every qrel.) Legs marked ***(silent)*** were ENTERED by "
        "the pipeline but emitted zero rows on every qrel; they are listed "
        "explicitly because an absent row would otherwise read as 'nothing "
        "to report' exactly when the arm contributed nothing at all.\n"
        + "\n".join(lines)
    )


def _delta_table(
    base: MetricsResult, exp: MetricsResult, top_k: int = 10
) -> str:
    """Build a metric-by-metric delta table.

    ``MetricsResult`` field names carry the historical ``_at_10`` suffix but
    hold whatever depth ``compute_metrics`` was given, so the *labels* are
    rewritten at ``top_k``. Without this a k=30 report describes @30 deltas
    as @10 — the aggregate headers were parameterized earlier while these
    rows were not.
    """
    label_at_k = {"p_at_10": f"p_at_{top_k}", "r_at_10": f"r_at_{top_k}",
                  "ndcg_at_10": f"ndcg_at_{top_k}"}
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
            f"| {label_at_k.get(metric, metric)} | {d.baseline_mean:.3f} | "
            f"{d.experimental_mean:.3f} | "
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
    top_k: int = 10,
) -> str:
    """Render the full per-qrel per-config grid as a JSON string.

    This is the structure persisted to ``nous_system.eval_runs.metrics``
    for historical regression analysis.

    ``top_k`` is recorded so the artifact self-describes the depth it was
    scored at — a later consumer cannot judge a null without knowing which
    legs were reachable at that depth (N7).
    """
    payload: dict = {
        "version": 1,
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "git_sha": git_sha,
        "fixture_version": fixture_version,
        "notes": notes,
        "top_k": top_k,
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
                "metrics": _metrics_to_dict(
                    compute_metrics(r.per_qrel, top_k=top_k)
                ),
                # N7 follow-up: persist the visibility analysis and the
                # pipeline's attempted-leg report, so historical eval_runs
                # analysis can tell whether an old null came from a leg
                # below the cutoff — or from one that never ran at all.
                "attempted_legs": list(r.attempted_legs),
                "leg_visibility": [
                    {
                        "leg": v.leg,
                        "n_rows": v.n_rows,
                        "n_qrels_evaluated": v.n_qrels_evaluated,
                        "n_qrels_present": v.n_qrels_present,
                        "n_qrels_within_cutoff": v.n_qrels_within_cutoff,
                        "participation_rate": v.participation_rate,
                        "median_rank": v.median_rank,
                        "best_rank": v.best_rank,
                        "cutoff": v.cutoff,
                        "visible": v.visible,
                    }
                    for v in leg_visibility(
                        r.per_qrel, cutoff=top_k,
                        attempted_legs=r.attempted_legs,
                    )
                ],
                "per_qrel": [
                    {
                        "index": q.qrel_index,
                        "query": q.qrel_query,
                        "source": q.qrel_source,
                        "gold_ids": [str(g) for g in q.gold_ids],
                        "retrieved_ids": [str(rid) for rid in q.retrieved_ids],
                        "retrieved_types": q.retrieved_types,
                        # N7/codex-P2: aligned 1:1 with retrieved_ids, so a
                        # consumer can recompute leg visibility at any depth.
                        "retrieved_legs": q.retrieved_legs,
                        "rank_of_first_gold": q.rank_of_first_gold,
                        "n_gold_in_top_k": q.n_gold_in_top_k,
                        "n_gold_total": q.n_gold_total,
                        "error": q.error,
                        # N1/codex-P1: non-empty means these metrics are based
                        # on a PARTIAL retrieval (e.g. the fact leg crashed).
                        "stage_errors": q.stage_errors,
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


def _metrics_to_dict(m: MetricsResult) -> dict:
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
        # N7: the untruncated view. Keys are stringified because JSON object
        # keys must be strings — consumers cast back to int.
        "recall_curve": {str(k): v for k, v in sorted(m.recall_curve.items())},
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
