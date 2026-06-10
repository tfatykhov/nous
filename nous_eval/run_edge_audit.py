"""F053 edge-precision audit runner.

Samples N newly-created edges per relation type from the eval DB, fetches
source/target content, feeds to ``edge_judge.judge_edges`` for YES/WEAK/NO
verdicts, and writes a precision report.

Designed to run AFTER ``density_eval`` against the same eval DB. The
``--since`` flag filters edges by ``created_at`` so you audit only the
edges the most-recent density_eval run produced (not the cumulative graph).

Typical workflow::

    # 1. Note start timestamp
    NOW=$(date -u +%Y-%m-%dT%H:%M:%S)

    # 2. Run density_eval (creates new edges)
    NOUS_EVAL_DB_NAME=nous_eval_scratch \\
    NOUS_QUERY_EXPANSION_ENABLED=true \\
    uv run python -m nous_eval.density_eval --configs baseline,f052_on

    # 3. Audit only the edges from step 2
    NOUS_EVAL_DB_NAME=nous_eval_scratch \\
    uv run python -m nous_eval.run_edge_audit --since "$NOW"

The audit writes ``reports/edge-audit-<timestamp>.md`` with per-relation
precision (YES / (YES + WEAK + NO)). Spec gate criterion 2 is
**precision >= 0.75 per type**.

Cost: 1 Sonnet call per BATCH_SIZE (30) edges. With 4 relation types ×
30 edges = ~4 calls per audit, so ~$0.05.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import sys
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import text

from nous.brain._entity_config import _ENTITY_CONFIG
from nous.config import Settings
from nous.storage.database import Database
from nous_eval.config import EvalSettings
from nous_eval.edge_judge import EdgeJudgment, judge_edges
from nous_eval.retrieval_runner import _settings_for_eval_db

logger = logging.getLogger(__name__)

# #354: single source of truth — derive the per-type content mapping from the
# densifier's _ENTITY_CONFIG so the judge sees EXACTLY what the densifier
# embeds/links on. The previous hand-rolled mapping had drifted on every
# non-fact type (decision read `context`, NULL for ~34% of prod decisions,
# while the densifier reads `description`; episode missed the F058 COALESCE;
# procedure used a different expr; chunk was absent entirely, so chunk edges
# were silently skipped). Exprs use the `t.` alias — the hydration query
# below aliases the table accordingly.
_CONTENT_BY_TYPE: dict[str, tuple[str, str]] = {
    etype: (table, content_expr)
    for etype, (table, _type_name, content_expr, _extra) in _ENTITY_CONFIG.items()
}


async def _sample_edges_per_type(
    db: Database,
    agent_id: str,
    since: datetime | None,
    limit_per_type: int,
) -> dict[str, list[dict[str, Any]]]:
    """Sample N edges per relation type, joining source+target content.

    Returns ``{relation: [edge_dict, ...]}`` keyed by ``relation`` (e.g.
    ``"related_to"``, ``"evidence_for"``). Edge dict has the shape
    ``edge_judge.judge_edges`` expects.
    """
    out: dict[str, list[dict[str, Any]]] = defaultdict(list)
    async with db.session() as session:
        rel_rows = await session.execute(text(
            "SELECT DISTINCT relation FROM brain.graph_edges WHERE agent_id = :aid"
        ), {"aid": agent_id})
        relations = [r[0] for r in rel_rows]

        for relation in relations:
            params: dict[str, Any] = {
                "aid": agent_id, "rel": relation, "lim": limit_per_type,
            }
            since_clause = ""
            if since is not None:
                since_clause = "AND e.created_at >= :since"
                params["since"] = since
            edge_rows = await session.execute(text(f"""
                SELECT e.source_id, e.source_type, e.target_id, e.target_type,
                       e.relation, e.weight
                FROM brain.graph_edges e
                WHERE e.agent_id = :aid AND e.relation = :rel {since_clause}
                ORDER BY random()
                LIMIT :lim
            """), params)
            for row in edge_rows:
                out[relation].append({
                    "source_id": str(row.source_id),
                    "source_type": row.source_type,
                    "target_id": str(row.target_id),
                    "target_type": row.target_type,
                    "relation": row.relation,
                    "weight": float(row.weight) if row.weight is not None else None,
                })

        # Hydrate content per edge (one round-trip per type to keep SQL simple).
        all_ids_by_type: dict[str, set[UUID]] = defaultdict(set)
        for edges in out.values():
            for e in edges:
                if e["source_type"] in _CONTENT_BY_TYPE:
                    all_ids_by_type[e["source_type"]].add(UUID(e["source_id"]))
                if e["target_type"] in _CONTENT_BY_TYPE:
                    all_ids_by_type[e["target_type"]].add(UUID(e["target_id"]))

        content_map: dict[tuple[str, str], str] = {}
        for entity_type, ids in all_ids_by_type.items():
            if not ids:
                continue
            table, content_expr = _CONTENT_BY_TYPE[entity_type]
            placeholders = ", ".join(f":id_{i}" for i in range(len(ids)))
            id_list = list(ids)
            params2 = {f"id_{i}": cid for i, cid in enumerate(id_list)}
            rows = await session.execute(text(f"""
                SELECT t.id, {content_expr} AS content
                FROM {table} t
                WHERE t.id IN ({placeholders})
            """), params2)
            for row in rows:
                content_map[(entity_type, str(row.id))] = row.content or ""

        # Stitch content into edge dicts; skip edges where either side is missing.
        for relation, edges in list(out.items()):
            stitched: list[dict[str, Any]] = []
            for e in edges:
                src_content = content_map.get((e["source_type"], e["source_id"]))
                tgt_content = content_map.get((e["target_type"], e["target_id"]))
                if src_content is None or tgt_content is None:
                    logger.debug("edge_audit: missing content for %s -> %s; skipping", e["source_id"], e["target_id"])
                    continue
                stitched.append({
                    **e,
                    "source_content": src_content,
                    "target_content": tgt_content,
                })
            out[relation] = stitched

    return dict(out)


def _summarize(judgments: list[EdgeJudgment]) -> dict[str, int]:
    counts = {"YES": 0, "WEAK": 0, "NO": 0, "PARSE_ERROR": 0}
    for j in judgments:
        counts[j.verdict] = counts.get(j.verdict, 0) + 1
    return counts


def _precision(counts: dict[str, int]) -> float:
    yes = counts.get("YES", 0)
    weak = counts.get("WEAK", 0)
    no = counts.get("NO", 0)
    denom = yes + weak + no
    return yes / denom if denom > 0 else 0.0


# ---------------------------------------------------------------------------
# Regression detection (EXEC-PLAN 2.3)
# ---------------------------------------------------------------------------
#
# Audit 2026-05-03 found that between Apr 26 and Apr 30 evidence_for jumped
# 0.53 → 0.75 (good) while related_to fell 0.83 → 0.70 (bad). Net: still
# failing the gate. Without per-relation regression tracking, the eval
# silently traded one relation for another. Fix: load the most recent prior
# audit's per-relation precisions and fail if any drops more than
# ``max_regression`` (default 0.05 absolute) below the prior value.


def _load_prior_precisions(prior_json: Path) -> dict[str, float]:
    """Return ``{relation: precision}`` from a prior audit JSON file.

    Returns ``{}`` if the file is missing or malformed — caller decides
    whether that's fatal. Prior runs that pre-date JSON output are
    handled gracefully (they just produce no regression baseline).
    """
    if not prior_json.exists():
        return {}
    try:
        data = json.loads(prior_json.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        logger.warning("F053 audit: prior baseline %s unreadable; skipping regression check", prior_json)
        return {}
    relations = data.get("relations", [])
    return {
        r["relation"]: float(r["precision"])
        for r in relations
        if "relation" in r and "precision" in r
    }


def _autodetect_prior_baseline(reports_dir: Path, current_path: Path) -> Path | None:
    """Find the most recent ``edge-audit-*.json`` other than ``current_path``.

    Returns ``None`` if no prior exists. Used when the operator doesn't
    pass ``--baseline-json`` explicitly so regression is on by default.
    """
    if not reports_dir.exists():
        return None
    candidates = sorted(
        (p for p in reports_dir.glob("edge-audit-*.json") if p != current_path),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


def _check_regressions(
    current: dict[str, float],
    prior: dict[str, float],
    max_regression: float,
) -> list[tuple[str, float, float, float]]:
    """Return ``[(relation, prior, current, delta)]`` for regressions.

    A regression is ``current < prior - max_regression`` modulo a
    small float epsilon (so an exact-on-threshold drop like 0.80 → 0.75
    at max_regression=0.05 is not flagged — IEEE 754 makes that delta
    ``-0.050000000000000044``). Relations present in current but
    missing from prior are NOT regressions (new relations).
    """
    eps = 1e-9
    out: list[tuple[str, float, float, float]] = []
    for relation, cur in sorted(current.items()):
        if relation not in prior:
            continue
        delta = cur - prior[relation]
        if delta < -max_regression - eps:
            out.append((relation, prior[relation], cur, delta))
    return out


def _write_json(
    per_relation: dict[str, list[EdgeJudgment]],
    out_path: Path,
    since: datetime | None,
    limit_per_type: int,
    threshold: float,
) -> None:
    """JSON sibling for machine-readable consumption (regression checks)."""
    relations: list[dict[str, Any]] = []
    for relation in sorted(per_relation.keys()):
        judgments = per_relation[relation]
        counts = _summarize(judgments)
        relations.append({
            "relation": relation,
            "n": len(judgments),
            "yes": counts.get("YES", 0),
            "weak": counts.get("WEAK", 0),
            "no": counts.get("NO", 0),
            "parse_error": counts.get("PARSE_ERROR", 0),
            "precision": _precision(counts),
        })
    payload = {
        "generated_utc": datetime.now(tz=UTC).isoformat(),
        "since": since.isoformat() if since else None,
        "limit_per_type": limit_per_type,
        "threshold": threshold,
        "relations": relations,
    }
    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _write_report(
    per_relation: dict[str, list[EdgeJudgment]],
    out_path: Path,
    since: datetime | None,
    limit_per_type: int,
    threshold: float,
    regressions: list[tuple[str, float, float, float]] | None = None,
    baseline_path: Path | None = None,
) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# F053 edge-precision audit",
        "",
        f"_Generated: {datetime.now(tz=UTC):%Y-%m-%d %H:%M:%S} UTC_",
        f"_since: {since.isoformat() if since else '(all edges)'}_  ",
        f"_sample-limit-per-type: {limit_per_type}_  ",
        f"_gate threshold (precision >= {threshold:.2f} per type)_",
        "",
        "| relation | n | YES | WEAK | NO | PARSE_ERROR | precision | gate |",
        "|----------|---|-----|------|----|-------------|-----------|------|",
    ]
    overall_pass = True
    for relation in sorted(per_relation.keys()):
        judgments = per_relation[relation]
        counts = _summarize(judgments)
        n = len(judgments)
        prec = _precision(counts)
        passes = prec >= threshold and n >= 15  # spec N-floor
        gate_marker = "PASS" if passes else ("UNDERPOWERED" if n < 15 else "FAIL")
        if not passes and n >= 15:
            overall_pass = False
        lines.append(
            f"| {relation} | {n} | {counts['YES']} | {counts['WEAK']} | {counts['NO']} | "
            f"{counts['PARSE_ERROR']} | {prec:.2f} | {gate_marker} |"
        )
    lines.append("")
    lines.append(f"**Overall gate**: {'PASS' if overall_pass else 'FAIL'} "
                 f"(all gate-eligible relations meet precision >= {threshold:.2f})")
    lines.append("")
    if regressions is not None:
        lines.append("## Per-relation regression check")
        lines.append("")
        if baseline_path is not None:
            lines.append(f"_Baseline: `{baseline_path}`_")
            lines.append("")
        if not regressions:
            lines.append("**REGRESSION CHECK PASS** — no relation dropped beyond tolerance.")
        else:
            lines.append("**REGRESSION CHECK FAIL** — the following relations regressed:")
            lines.append("")
            lines.append("| relation | prior | current | delta |")
            lines.append("|----------|------:|--------:|------:|")
            for relation, prior, current, delta in regressions:
                lines.append(
                    f"| {relation} | {prior:.2f} | {current:.2f} | {delta:+.2f} |"
                )
        lines.append("")
    lines.append("## Sample of NO + WEAK verdicts (for spot-check)")
    lines.append("")
    sample_count = 0
    for relation, judgments in per_relation.items():
        for j in judgments:
            if j.verdict in ("NO", "WEAK") and sample_count < 10:
                lines.append(f"- **{relation}** [{j.verdict}] {j.source_id} -> {j.target_id}: {j.reasoning[:200]}")
                sample_count += 1
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


async def run(
    settings: EvalSettings,
    since: datetime | None,
    limit_per_type: int,
    threshold: float,
    out_path: Path | None,
    baseline_json: Path | None = None,
    max_regression: float = 0.05,
    auto_baseline: bool = True,
) -> tuple[Path, list[tuple[str, float, float, float]]]:
    """Driver: build DB, sample edges, judge, write report.

    Returns ``(report_path, regressions)``. ``regressions`` is the list
    of ``(relation, prior, current, delta)`` for relations that dropped
    more than ``max_regression`` below the baseline; empty list means
    no regression (or no baseline available).
    """
    main_settings = Settings()
    eval_scoped = _settings_for_eval_db(settings, main_settings)
    db = Database(eval_scoped)
    await db.connect()
    try:
        agent_id = settings.agent_id
        per_relation_edges = await _sample_edges_per_type(db, agent_id, since, limit_per_type)
        if not per_relation_edges:
            logger.warning("F053 audit: no edges sampled (since=%s, agent_id=%s)", since, agent_id)

        per_relation_verdicts: dict[str, list[EdgeJudgment]] = {}
        for relation, edges in per_relation_edges.items():
            if not edges:
                continue
            logger.info("F053 audit: judging %d edges for relation=%s", len(edges), relation)
            verdicts = await judge_edges(edges, eval_scoped)
            per_relation_verdicts[relation] = verdicts

        if out_path is None:
            out_path = Path(settings.report_dir) / f"edge-audit-{datetime.now(tz=UTC):%Y%m%d-%H%M%S}.md"
        json_path = out_path.with_suffix(".json")

        # Resolve baseline (explicit > auto-detect from reports/) and
        # compute regressions BEFORE writing JSON so the new file isn't
        # accidentally picked as its own baseline.
        resolved_baseline: Path | None = baseline_json
        if resolved_baseline is None and auto_baseline:
            resolved_baseline = _autodetect_prior_baseline(out_path.parent, json_path)

        current_precisions = {
            r: _precision(_summarize(judgments))
            for r, judgments in per_relation_verdicts.items()
        }
        regressions: list[tuple[str, float, float, float]] = []
        if resolved_baseline is not None:
            prior = _load_prior_precisions(resolved_baseline)
            if prior:
                regressions = _check_regressions(current_precisions, prior, max_regression)
                if regressions:
                    logger.warning(
                        "F053 audit: regression detected vs %s: %s",
                        resolved_baseline,
                        [(r, f"{p:.2f}->{c:.2f}") for r, p, c, _ in regressions],
                    )

        _write_report(
            per_relation_verdicts, out_path, since, limit_per_type, threshold,
            regressions=regressions if resolved_baseline is not None else None,
            baseline_path=resolved_baseline,
        )
        _write_json(per_relation_verdicts, json_path, since, limit_per_type, threshold)
        return out_path, regressions
    finally:
        await db.engine.dispose()


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="python -m nous_eval.run_edge_audit")
    p.add_argument("--since", default=None,
                   help="ISO-8601 timestamp; only audit edges created at/after this. Default: all edges.")
    p.add_argument("--limit-per-type", type=int, default=30,
                   help="Max edges to sample per relation type. Default: 30 (matches spec gate sample size).")
    p.add_argument("--threshold", type=float, default=0.75,
                   help="Precision floor per relation type. Default: 0.75 (spec gate criterion 2).")
    p.add_argument("--output", type=Path, default=None,
                   help="Output markdown path. Default: reports/edge-audit-<timestamp>.md.")
    p.add_argument("--baseline-json", type=Path, default=None,
                   help="Prior audit JSON for per-relation regression check. "
                        "Default: auto-detect most-recent edge-audit-*.json in --output's parent.")
    p.add_argument("--no-auto-baseline", action="store_true",
                   help="Disable auto-detection of prior baseline (skip regression check entirely).")
    p.add_argument("--max-regression", type=float, default=0.05,
                   help="Max allowed per-relation precision drop vs baseline (absolute). Default: 0.05.")
    p.add_argument("--exit-on-regression", action="store_true",
                   help="Exit with code 3 if any relation regressed beyond --max-regression.")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    ns = _parse_args(argv)

    since = None
    if ns.since:
        try:
            since = datetime.fromisoformat(ns.since)
            if since.tzinfo is None:
                since = since.replace(tzinfo=UTC)
        except ValueError:
            logger.error("F053 audit: invalid --since value %r (expected ISO-8601)", ns.since)
            return 2

    settings = EvalSettings()
    out, regressions = asyncio.run(run(
        settings=settings,
        since=since,
        limit_per_type=ns.limit_per_type,
        threshold=ns.threshold,
        out_path=ns.output,
        baseline_json=ns.baseline_json,
        max_regression=ns.max_regression,
        auto_baseline=not ns.no_auto_baseline,
    ))
    print(f"Wrote: {out}")
    if regressions:
        print(f"REGRESSION CHECK: {len(regressions)} relation(s) dropped beyond {ns.max_regression}:",
              file=sys.stderr)
        for relation, prior, current, delta in regressions:
            print(f"  {relation}: {prior:.2f} -> {current:.2f} ({delta:+.2f})",
                  file=sys.stderr)
        if ns.exit_on_regression:
            return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
