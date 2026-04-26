"""F052 density-eval harness mode.

Measures graph_edges + orphan-rate deltas across baseline vs ``f052_on``
configs on the F051 eval DB, using a snapshot-reset-run-snapshot loop with
transactional restore on per-config failure.

Determinism: forces ``query_expansion_temperature=0.0`` for the duration of
the run (set on the ``f052_on`` ``RetrievalConfig`` in
:mod:`nous_eval.retrieval`). Reproducibility: ``heart.query_expansions``
cache rows are preserved across runs (no truncate).

Per-config behavior:

1. ``RuntimeConfig.reset()`` — clear leakage from the prior config.
2. ``_apply_config_flags(template, cfg)`` — Settings overlay (NOT
   ``RuntimeConfig.set``; mirrors :mod:`retrieval_runner`).
3. ``_settings_for_eval_db(eval_settings, overridden)`` — redirect DB.
4. ``_ensure_zero_edge_baseline`` — DELETE current edges; (re-)create the
   ``brain.eval_baseline_edges_snapshot`` anchor table; TRUNCATE it. The
   anchor is intentionally empty — it represents zero-edge state and is
   used by ``_restore_baseline`` if a config crashes mid-cycle.
5. Snapshot pre-state (orphan + edge counts).
6. Build Heart + densifier (sharing the embedding provider, mirroring
   :mod:`nous/main.py:300`).
7. Run ``GraphDensifier.run_backfill_cycle()`` + ``discover_clusters``.
8. On exception: ``_restore_baseline`` and tag the run as failed.
9. Snapshot post-state.

A real (non-TEMP) snapshot table is used because TEMP tables are
session-scoped in Postgres — a per-config run uses several pooled
connections, and a TEMP table would silently disappear on connection
swap.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import text

from nous.brain._entity_config import _ENTITY_CONFIG
from nous.config import Settings
from nous.runtime_config import RuntimeConfig
from nous.storage.database import Database
from nous_eval.config import EvalSettings
from nous_eval.retrieval import _DEFAULT_CONFIGS, RetrievalConfig
from nous_eval.retrieval_runner import (
    _apply_config_flags,
    _build_densifier_for_eval,
    _build_heart_for_eval,
    _settings_for_eval_db,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DensitySnapshot:
    """Edge + orphan counts at one point in time, scoped to one ``agent_id``."""

    edge_count_total: int
    edge_count_per_relation: dict[str, int] = field(default_factory=dict)
    orphan_count_per_type: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class DensityRunResult:
    """Outcome of one config under one density-eval run."""

    config_name: str
    pre: DensitySnapshot
    post: DensitySnapshot | None
    edges_created: int
    ce_pruned: int
    wall_seconds: float
    failure: str | None = None  # set on exception path


# ---------------------------------------------------------------------------
# Snapshot / restore helpers
# ---------------------------------------------------------------------------


_SNAPSHOT_TABLE = "brain.eval_baseline_edges_snapshot"


async def _ensure_zero_edge_baseline(db: Database, agent_id: str) -> None:
    """Pre-condition: zero edges for ``agent_id``; ensure persistent snapshot.

    The snapshot table is REAL (not TEMP) so it survives across the multiple
    pooled connections that one per-config run uses. It is intentionally
    empty: it represents the "zero-edge baseline" anchor state, restored by
    :func:`_restore_baseline` when a config crashes mid-cycle.

    Idempotent: ``CREATE TABLE IF NOT EXISTS`` + ``TRUNCATE``.
    """
    async with db.session() as session:
        await session.execute(
            text("DELETE FROM brain.graph_edges WHERE agent_id = :aid"),
            {"aid": agent_id},
        )
        await session.execute(
            text(
                f"CREATE TABLE IF NOT EXISTS {_SNAPSHOT_TABLE} "
                "(LIKE brain.graph_edges INCLUDING ALL)"
            )
        )
        await session.execute(text(f"TRUNCATE {_SNAPSHOT_TABLE}"))
        await session.commit()


async def _restore_baseline(db: Database, agent_id: str) -> None:
    """Restore graph_edges to the empty-snapshot anchor state for ``agent_id``.

    The snapshot table is intentionally empty, so the INSERT-from-snapshot
    is a no-op; the DELETE alone returns to zero-edge state. Kept as
    DELETE+INSERT so that if a future iteration of the harness wants to
    snapshot a non-empty baseline, the restore semantics already match.
    """
    async with db.session() as session:
        await session.execute(
            text("DELETE FROM brain.graph_edges WHERE agent_id = :aid"),
            {"aid": agent_id},
        )
        await session.execute(
            text(
                f"INSERT INTO brain.graph_edges "
                f"SELECT * FROM {_SNAPSHOT_TABLE}"
            )
        )
        await session.commit()


async def _snapshot(db: Database, agent_id: str) -> DensitySnapshot:
    """Capture edge count (per-relation + total) and orphan count per type.

    Both counts are scoped to ``agent_id``. Orphans are computed via the
    same NOT EXISTS clause :class:`GraphDensifier.find_orphans` uses so the
    pre-state matches what the densifier sees on its first iteration.
    """
    async with db.session() as session:
        edge_total_row = await session.execute(
            text(
                "SELECT COUNT(*) AS n FROM brain.graph_edges "
                "WHERE agent_id = :aid"
            ),
            {"aid": agent_id},
        )
        edge_total = int(edge_total_row.scalar() or 0)

        per_rel_rows = await session.execute(
            text(
                "SELECT relation, COUNT(*) AS n FROM brain.graph_edges "
                "WHERE agent_id = :aid GROUP BY relation"
            ),
            {"aid": agent_id},
        )
        per_rel = {row.relation: int(row.n) for row in per_rel_rows}

        orphan_per_type: dict[str, int] = {}
        for entity_type, (table, type_name, _content_col, extra) in _ENTITY_CONFIG.items():
            sql = text(
                f"""
                SELECT COUNT(*) AS n FROM {table} t
                WHERE t.agent_id = :aid
                  AND {extra}
                  AND t.embedding IS NOT NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM brain.graph_edges e
                      WHERE e.agent_id = :aid
                        AND (
                            (e.source_id = t.id AND e.source_type = :type_name)
                            OR (e.target_id = t.id AND e.target_type = :type_name)
                        )
                  )
                """
            )
            row = await session.execute(
                sql, {"aid": agent_id, "type_name": type_name}
            )
            orphan_per_type[entity_type] = int(row.scalar() or 0)

    return DensitySnapshot(
        edge_count_total=edge_total,
        edge_count_per_relation=per_rel,
        orphan_count_per_type=orphan_per_type,
    )


# ---------------------------------------------------------------------------
# Per-config runner
# ---------------------------------------------------------------------------


async def _run_one_config(
    config: RetrievalConfig,
    main_settings_template: Settings,
    eval_settings: EvalSettings,
    db: Database,
) -> DensityRunResult:
    """Run one config end-to-end. Mirrors ``retrieval_runner.run_matrix``.

    Sequence (matches F051 pattern at retrieval_runner.py:160-220):

    1. ``RuntimeConfig.reset()``
    2. ``overridden = _apply_config_flags(template, cfg)``
    3. ``eval_scoped = _settings_for_eval_db(eval_settings, overridden)``
    4. snapshot baseline (zero-edge anchor)
    5. snapshot pre-state
    6. build Heart + densifier; run cycle
    7. on exception: restore baseline; record failure
    8. snapshot post-state
    """
    RuntimeConfig.reset()
    logger.info("F052: running density config=%s", config.name)
    overridden = _apply_config_flags(main_settings_template, config)
    eval_scoped = _settings_for_eval_db(eval_settings, overridden)
    # SFH P1-1: F051's _settings_for_eval_db forces graph_backfill_enabled=False
    # to suppress the production sleep handler. density_eval invokes the densifier
    # DIRECTLY, so we must re-enable backfill here or every config silently
    # returns 0 edges. Same applies to event_bus (we want emission events from
    # backfill to fire) and a few related toggles. We override only what we need.
    eval_scoped = eval_scoped.model_copy(update={"graph_backfill_enabled": True})
    assert eval_scoped.graph_backfill_enabled, (
        "density_eval requires graph_backfill_enabled=True; check _settings_for_eval_db override"
    )
    agent_id = eval_scoped.agent_id

    t0 = time.monotonic()
    await _ensure_zero_edge_baseline(db, agent_id)
    pre = await _snapshot(db, agent_id)

    edges_created = 0
    ce_pruned = 0
    failure: str | None = None
    post: DensitySnapshot | None = None

    try:
        async with _build_heart_for_eval(db, eval_scoped) as heart:
            densifier = await _build_densifier_for_eval(
                eval_scoped, db, agent_id, heart
            )
            try:
                cycle_result = await densifier.run_backfill_cycle()
            except Exception as exc:
                logger.exception(
                    "F052: run_backfill_cycle raised for config=%s", config.name
                )
                await _restore_baseline(db, agent_id)
                wall = time.monotonic() - t0
                return DensityRunResult(
                    config_name=config.name,
                    pre=pre,
                    post=None,
                    edges_created=0,
                    ce_pruned=0,
                    wall_seconds=wall,
                    failure=f"{type(exc).__name__}: {exc}",
                )

            # cycle_result keys: facts/decisions/episodes/procedures + _ce_stats
            edges_created = sum(
                v for k, v in cycle_result.items() if not k.startswith("_")
            )
            ce_stats = cycle_result.get("_ce_stats") or {}
            ce_pruned = int(ce_stats.get("pruned", 0))

            try:
                # Cluster discovery is best-effort; failure here doesn't
                # invalidate the backfill cycle's edge counts.
                await densifier.discover_clusters(max_bridges=20)
            except Exception:
                logger.warning(
                    "F052: discover_clusters raised for config=%s; continuing",
                    config.name,
                    exc_info=True,
                )

            post = await _snapshot(db, agent_id)
    except Exception as exc:
        logger.exception("F052: harness setup failed for config=%s", config.name)
        restore_failure: str | None = None
        try:
            await _restore_baseline(db, agent_id)
        except Exception as restore_exc:
            logger.exception(
                "F052: _restore_baseline also failed for config=%s", config.name
            )
            restore_failure = f"{type(restore_exc).__name__}: {restore_exc}"
        failure = f"{type(exc).__name__}: {exc}"
        if restore_failure is not None:
            failure = f"{failure} | restore_also_failed: {restore_failure}"

    wall = time.monotonic() - t0
    return DensityRunResult(
        config_name=config.name,
        pre=pre,
        post=post,
        edges_created=edges_created,
        ce_pruned=ce_pruned,
        wall_seconds=wall,
        failure=failure,
    )


# ---------------------------------------------------------------------------
# Top-level orchestration + reporting
# ---------------------------------------------------------------------------


async def run_density_eval(
    config_names: list[str],
    eval_settings: EvalSettings,
    n_runs: int = 1,
) -> list[DensityRunResult]:
    """Run the density eval matrix and return results in run-order.

    One ``Database`` is created and reused across all configs/runs to avoid
    per-config asyncpg-pool churn. Each per-config call still gets a fresh
    Heart + densifier (mirrors ``run_matrix`` semantics).
    """
    main_settings_template = Settings()
    db = Database(_settings_for_eval_db(eval_settings, main_settings_template))
    await db.connect()
    try:
        results: list[DensityRunResult] = []
        for run_idx in range(n_runs):
            for name in config_names:
                if name not in _DEFAULT_CONFIGS:
                    raise ValueError(
                        f"Unknown config: {name!r}. "
                        f"Known: {sorted(_DEFAULT_CONFIGS)}"
                    )
                config = _DEFAULT_CONFIGS[name]
                logger.info(
                    "F052: density-eval run %d/%d config=%s",
                    run_idx + 1, n_runs, name,
                )
                results.append(
                    await _run_one_config(
                        config, main_settings_template, eval_settings, db
                    )
                )
        return results
    finally:
        await db.engine.dispose()


def _format_snapshot(label: str, snap: DensitySnapshot | None) -> str:
    if snap is None:
        return f"  {label}: <none — config failed>\n"
    parts = [f"  {label}: total={snap.edge_count_total}"]
    if snap.edge_count_per_relation:
        rels = ", ".join(
            f"{k}={v}" for k, v in sorted(snap.edge_count_per_relation.items())
        )
        parts.append(f"    per_relation: {rels}")
    if snap.orphan_count_per_type:
        orphs = ", ".join(
            f"{k}={v}" for k, v in sorted(snap.orphan_count_per_type.items())
        )
        parts.append(f"    orphans:      {orphs}")
    return "\n".join(parts) + "\n"


def _write_report(results: list[DensityRunResult], path: Path) -> None:
    """Write a human-readable Markdown report.

    Each config gets a section with pre/post snapshots, edges_created,
    CE-rerank pruning count, wall-clock seconds, and failure tag (if any).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    lines.append("# F052 density-eval report\n")
    lines.append(
        f"_Generated: {datetime.now(tz=UTC):%Y-%m-%d %H:%M:%S UTC}_\n"
    )
    lines.append("")
    lines.append(
        "| config | edges_created | ce_pruned | wall_s | status |"
    )
    lines.append(
        "|--------|---------------|-----------|--------|--------|"
    )
    for r in results:
        status = "FAIL" if r.failure else "OK"
        lines.append(
            f"| {r.config_name} | {r.edges_created} | {r.ce_pruned} | "
            f"{r.wall_seconds:.1f} | {status} |"
        )
    lines.append("")

    for r in results:
        lines.append(f"## {r.config_name}")
        if r.failure:
            lines.append(f"**FAILURE**: `{r.failure}`")
        lines.append("")
        lines.append(_format_snapshot("pre ", r.pre))
        lines.append(_format_snapshot("post", r.post))
        lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns process exit code."""
    p = argparse.ArgumentParser(
        prog="python -m nous_eval.density_eval",
        description="F052 density-eval harness — backfill A/B on the eval DB.",
    )
    p.add_argument(
        "--configs",
        type=str,
        default="baseline,f052_on",
        help="Comma-separated config names (default: baseline,f052_on).",
    )
    p.add_argument(
        "--n-runs",
        type=int,
        default=1,
        help="Number of times to repeat the matrix (for variance).",
    )
    p.add_argument(
        "--report-dir",
        type=Path,
        default=None,
        help="Override EvalSettings.report_dir.",
    )
    p.add_argument(
        "--log-level",
        type=str,
        default="info",
        help="Logging verbosity (debug/info/warning).",
    )
    args = p.parse_args(argv)

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    eval_settings = EvalSettings()
    eval_settings.warn_if_default_password()
    if args.report_dir is not None:
        eval_settings = eval_settings.model_copy(
            update={"report_dir": args.report_dir}
        )

    config_names = [c.strip() for c in args.configs.split(",") if c.strip()]

    try:
        results = asyncio.run(
            run_density_eval(config_names, eval_settings, args.n_runs)
        )
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    out = (
        eval_settings.report_dir
        / f"density-eval-{datetime.now(tz=UTC):%Y%m%d-%H%M%S}.md"
    )
    _write_report(results, out)
    print(f"Wrote: {out}")
    logger.info("F052: density_eval report written: %s", out)

    # Non-zero exit if any config failed — useful for CI signaling.
    if any(r.failure for r in results):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
