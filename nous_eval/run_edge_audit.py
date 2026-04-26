"""F052 edge-precision audit runner.

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
import logging
import sys
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import text

from nous.config import Settings
from nous.storage.database import Database
from nous_eval.config import EvalSettings
from nous_eval.edge_judge import EdgeJudgment, judge_edges
from nous_eval.retrieval_runner import _settings_for_eval_db

logger = logging.getLogger(__name__)

# Per-type content fetch is structural — facts.content, decisions.context,
# episodes.summary, procedures.steps_text. Mirrors fetch_candidate_content
# in nous/brain/backfill_rerank.py but inline here to keep the audit tool
# free of brain-internal imports.
_CONTENT_BY_TYPE: dict[str, tuple[str, str]] = {
    "fact": ("heart.facts", "content"),
    "decision": ("brain.decisions", "context"),
    "episode": ("heart.episodes", "summary"),
    "procedure": ("heart.procedures", "name || ': ' || description"),
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
                SELECT id, {content_expr} AS content
                FROM {table}
                WHERE id IN ({placeholders})
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


def _write_report(
    per_relation: dict[str, list[EdgeJudgment]],
    out_path: Path,
    since: datetime | None,
    limit_per_type: int,
    threshold: float,
) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# F052 edge-precision audit",
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
) -> Path:
    """Driver: build DB, sample edges, judge, write report. Returns the report path."""
    main_settings = Settings()
    eval_scoped = _settings_for_eval_db(settings, main_settings)
    db = Database(eval_scoped)
    await db.connect()
    try:
        agent_id = settings.agent_id
        per_relation_edges = await _sample_edges_per_type(db, agent_id, since, limit_per_type)
        if not per_relation_edges:
            logger.warning("F052 audit: no edges sampled (since=%s, agent_id=%s)", since, agent_id)

        per_relation_verdicts: dict[str, list[EdgeJudgment]] = {}
        for relation, edges in per_relation_edges.items():
            if not edges:
                continue
            logger.info("F052 audit: judging %d edges for relation=%s", len(edges), relation)
            verdicts = await judge_edges(edges, eval_scoped)
            per_relation_verdicts[relation] = verdicts

        if out_path is None:
            out_path = Path(settings.report_dir) / f"edge-audit-{datetime.now(tz=UTC):%Y%m%d-%H%M%S}.md"
        _write_report(per_relation_verdicts, out_path, since, limit_per_type, threshold)
        return out_path
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
            logger.error("F052 audit: invalid --since value %r (expected ISO-8601)", ns.since)
            return 2

    settings = EvalSettings()
    out = asyncio.run(run(
        settings=settings,
        since=since,
        limit_per_type=ns.limit_per_type,
        threshold=ns.threshold,
        out_path=ns.output,
    ))
    print(f"Wrote: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
