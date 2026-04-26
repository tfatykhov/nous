"""``python -m nous_eval.retrieval`` — main CLI entry for the eval harness.

Subcommand-free CLI: argparse with ``--configs``, ``--sources``, etc. The
harness:

1. Loads ``EvalSettings`` from env.
2. Loads source registry + qrels from each available source.
3. Runs the retrieval matrix.
4. Renders markdown + JSON reports into ``--report-dir``.
5. Best-effort persists the run to ``nous_system.eval_runs`` on the main DB.

Failure modes (per spec §"Silent-failure surface"):

- Eval DB unreachable → fast-fail with operator hint.
- Fixture version mismatch → WARN, run continues.
- Run history insert timeout → WARN, run continues; report on disk is the
  primary record.
- Unknown config name → fast-fail listing valid names.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import socket
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from nous.config import Settings
from nous_eval.config import EvalSettings
from nous_eval.metrics import compute_metrics
from nous_eval.qrels_loader import QrelSource, load_qrels
from nous_eval.report import (
    decide_gate_f050,
    render_json,
    render_markdown,
    write_reports,
)
from nous_eval.retrieval_runner import RetrievalConfig, run_matrix
from nous_eval.source_registry import SourceRegistry

if TYPE_CHECKING:
    from nous_eval.qrels_loader import Qrel
    from nous_eval.retrieval_runner import RunResult
    from nous_eval.source_registry import ResolvedSource

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Default config matrix — keep in sync with docs/features/F051 §5.
# ---------------------------------------------------------------------------

_DEFAULT_CONFIGS: dict[str, RetrievalConfig] = {
    "baseline": RetrievalConfig(
        name="baseline",
        flags={},
        description="Defaults from Settings() — nothing overridden.",
    ),
    "f050_on": RetrievalConfig(
        name="f050_on",
        flags={"query_expansion_enabled": True},
        description="F050 multi-query expansion enabled.",
    ),
    "f052_on": RetrievalConfig(
        name="f052_on",
        flags={
            "graph_backfill_multi_embedding_enabled": True,
            # F052 depends on the F050 expander being live — without it,
            # Heart.expand_query_pairs returns the single-pair fallback
            # and the densifier wedge collapses to byte-identical baseline.
            "query_expansion_enabled": True,
            # density_eval forces this to 0.0 for re-run determinism;
            # the retrieval-side matrix gets the same override here so
            # the two surfaces agree on the determinism contract.
            "query_expansion_temperature": 0.0,
        },
        description=(
            "F052 multi-embedding seed for _backfill_same_type. "
            "Eval-only retrieval-side measurement; density-side lives in "
            "`python -m nous_eval.density_eval`."
        ),
    ),
    "ce_off": RetrievalConfig(
        name="ce_off",
        flags={"cross_encoder_enabled": False},
        description="Cross-encoder reranking disabled (no-op against default-off baseline).",
    ),
    "ce_on": RetrievalConfig(
        name="ce_on",
        flags={"cross_encoder_enabled": True},
        description="F042 cross-encoder reranking enabled (retroactive A/B vs default-off).",
    ),
    "ce_on_mmr_off": RetrievalConfig(
        name="ce_on_mmr_off",
        flags={"cross_encoder_enabled": True, "mmr_enabled": False},
        description="CE rerank + MMR off — isolates CE's effect from MMR's diversity re-pick.",
    ),
    "f050_on_ce_mmr_off": RetrievalConfig(
        name="f050_on_ce_mmr_off",
        flags={
            "query_expansion_enabled": True,
            "cross_encoder_enabled": True,
            "mmr_enabled": False,
        },
        description="F050 multi-query expansion + CE rerank + MMR off — peak combo to measure F050's marginal lift on top of the CE-on-MMR-off ceiling.",
    ),
    "ce_mmr_on_lambda_0.7": RetrievalConfig(
        name="ce_mmr_on_lambda_0.7",
        flags={
            "cross_encoder_enabled": True,
            "mmr_enabled": True,
            "mmr_skip_after_ce": False,
            "mmr_diversity_weight": 0.7,
        },
        description="CE + MMR with default λ=0.7 (70% relevance, 30% diversity). F030.1's 'always skip' default validation.",
    ),
    "ce_mmr_on_lambda_0.85": RetrievalConfig(
        name="ce_mmr_on_lambda_0.85",
        flags={
            "cross_encoder_enabled": True,
            "mmr_enabled": True,
            "mmr_skip_after_ce": False,
            "mmr_diversity_weight": 0.85,
        },
        description="CE + MMR with λ=0.85 (relevance-heavy). MMR as light tiebreaker.",
    ),
    "ce_mmr_on_lambda_0.95": RetrievalConfig(
        name="ce_mmr_on_lambda_0.95",
        flags={
            "cross_encoder_enabled": True,
            "mmr_enabled": True,
            "mmr_skip_after_ce": False,
            "mmr_diversity_weight": 0.95,
        },
        description="CE + MMR with λ=0.95 (near-pure relevance). MMR almost a no-op except for near-duplicate breakup.",
    ),
    "mmr_off": RetrievalConfig(
        name="mmr_off",
        flags={"mmr_enabled": False},
        description="MMR diversity reranking disabled.",
    ),
    "graph_off": RetrievalConfig(
        name="graph_off",
        flags={"graph_recall_enabled": False},
        description="F022 graph recall + spreading activation disabled.",
    ),
}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """Construct the argparse parser used by ``main``."""
    parser = argparse.ArgumentParser(
        prog="python -m nous_eval.retrieval",
        description="F051 retrieval evaluation harness.",
    )
    parser.add_argument(
        "--configs",
        type=str,
        default="baseline",
        help="Comma-separated config names (baseline, f050_on, ce_off, ce_on, mmr_off, graph_off).",
    )
    parser.add_argument(
        "--sources",
        type=str,
        default=None,
        help="Comma-separated source whitelist; overrides enabled_by_default.",
    )
    parser.add_argument(
        "--exclude",
        type=str,
        default=None,
        help="Comma-separated source blacklist.",
    )
    parser.add_argument(
        "--gate-only",
        action="store_true",
        help="Restrict to sources marked gate_eligible: true.",
    )
    parser.add_argument(
        "--include-unreviewed",
        action="store_true",
        help="Bypass review_filter on sources that have one (e.g. ai_hand_labeled).",
    )
    parser.add_argument(
        "--top-k",
        type=int,
        default=None,
        help="Override EvalSettings.top_k (default: 10).",
    )
    parser.add_argument(
        "--report-dir",
        type=Path,
        default=None,
        help="Override EvalSettings.report_dir.",
    )
    parser.add_argument(
        "--smoke",
        action="store_true",
        help="Force smoke mode (no fixtures dir; probes-only).",
    )
    parser.add_argument(
        "--no-history",
        action="store_true",
        help="Skip the nous_system.eval_runs INSERT.",
    )
    parser.add_argument(
        "--gate-f050",
        action="store_true",
        help="Compute the F050 enable-gate decision (requires baseline + f050_on).",
    )
    parser.add_argument(
        "--notes",
        type=str,
        default="",
        help="Free-form notes string saved with the run.",
    )
    parser.add_argument(
        "--log-level",
        type=str,
        default="info",
        help="Logging verbosity (debug/info/warning).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns process exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    eval_settings = EvalSettings()
    eval_settings.warn_if_default_password()

    # Override from CLI
    if args.top_k is not None:
        eval_settings = eval_settings.model_copy(update={"top_k": args.top_k})
    if args.report_dir is not None:
        eval_settings = eval_settings.model_copy(
            update={"report_dir": args.report_dir}
        )
    if args.smoke:
        eval_settings = eval_settings.model_copy(update={"fixtures_dir": None})

    # Configs
    config_names = [c.strip() for c in args.configs.split(",") if c.strip()]
    unknown = [c for c in config_names if c not in _DEFAULT_CONFIGS]
    if unknown:
        print(
            f"ERROR: unknown config(s): {unknown}. "
            f"Known: {sorted(_DEFAULT_CONFIGS)}",
            file=sys.stderr,
        )
        return 2
    configs = [_DEFAULT_CONFIGS[c] for c in config_names]

    # Source registry
    sources_only = (
        [s.strip() for s in args.sources.split(",") if s.strip()]
        if args.sources
        else None
    )
    sources_excl = (
        [s.strip() for s in args.exclude.split(",") if s.strip()]
        if args.exclude
        else None
    )
    registry = SourceRegistry.load(fixtures_dir=eval_settings.fixtures_dir)
    try:
        resolved_sources = registry.resolve(
            only=sources_only,
            exclude=sources_excl,
            gate_only=args.gate_only,
            include_unreviewed=args.include_unreviewed,
        )
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    return asyncio.run(
        _run_async(
            args=args,
            eval_settings=eval_settings,
            configs=configs,
            resolved_sources=resolved_sources,
        )
    )


async def _verify_fixture_version(
    eval_settings: EvalSettings, expected_version: str
) -> None:
    """Query nous_eval_meta on the eval DB and warn on version mismatch.

    Schema is key/value (matches Dockerfile.eval-db.load.sh + corpus_loader).
    Missing table → eval DB was bootstrapped without the fixture stamp;
    log INFO and continue.
    Version row missing → same treatment.
    Version row present but mismatch → WARN with both tags so operator
    knows to run `python -m nous_eval.tasks rebuild`.
    """
    import asyncpg

    try:
        conn = await asyncpg.connect(
            host=eval_settings.db_host,
            port=eval_settings.db_port,
            user=eval_settings.db_user,
            password=eval_settings.db_password,
            database=eval_settings.db_name,
            timeout=5,
        )
    except Exception as exc:
        logger.info(
            "F051: fixture-version probe could not connect (%s); skipping check",
            exc,
        )
        return
    try:
        row = await conn.fetchrow(
            "SELECT value FROM nous_eval_meta WHERE key = $1",
            "fixture_version",
        )
    except asyncpg.exceptions.UndefinedTableError:
        logger.info(
            "F051: nous_eval_meta table not present on eval DB — fixture-version "
            "probe skipped (DB likely bootstrapped without the load.sh stamp)"
        )
        return
    except Exception as exc:
        logger.warning("F051: fixture-version probe query failed: %s", exc)
        return
    finally:
        await conn.close()

    if row is None:
        logger.info(
            "F051: nous_eval_meta has no 'fixture_version' row — fixture stamp missing"
        )
        return
    actual = row["value"]
    if actual != expected_version:
        logger.warning(
            "F051: fixture version mismatch — eval DB has '%s' but env expects '%s'. "
            "Run `python -m nous_eval.tasks rebuild` to sync.",
            actual,
            expected_version,
        )
    else:
        logger.debug("F051: fixture version OK (%s)", actual)


async def _verify_corpus_agent_id(eval_settings: EvalSettings) -> None:
    """Query the eval DB for the corpus's actual agent_id and warn on mismatch.

    The harness will silently produce MRR=0 across every qrel if the eval DB's
    corpus uses a different agent_id than EvalSettings.agent_id (Heart's
    sub-searches filter `WHERE agent_id = self.agent_id`). This probe surfaces
    that misconfiguration before the matrix run.
    """
    import asyncpg

    try:
        conn = await asyncpg.connect(
            host=eval_settings.db_host,
            port=eval_settings.db_port,
            user=eval_settings.db_user,
            password=eval_settings.db_password,
            database=eval_settings.db_name,
            timeout=5,
        )
    except Exception as exc:
        logger.info(
            "F051: agent_id probe could not connect (%s); skipping check", exc
        )
        return
    try:
        # Sample distinct agent_ids across the four memory tables. If any
        # contain only a single agent_id and it doesn't match settings,
        # warn loudly. Empty tables produce no signal (the corpus might be
        # legitimately small).
        rows = await conn.fetch(
            """
            SELECT 'heart.facts' AS tbl, agent_id, COUNT(*) AS n
              FROM heart.facts GROUP BY agent_id
            UNION ALL
            SELECT 'brain.decisions', agent_id, COUNT(*)
              FROM brain.decisions GROUP BY agent_id
            UNION ALL
            SELECT 'heart.episodes', agent_id, COUNT(*)
              FROM heart.episodes GROUP BY agent_id
            UNION ALL
            SELECT 'heart.procedures', agent_id, COUNT(*)
              FROM heart.procedures GROUP BY agent_id
            """
        )
    except asyncpg.exceptions.UndefinedTableError as exc:
        logger.info(
            "F051: agent_id probe found unexpected schema (%s); skipping check",
            exc,
        )
        return
    except Exception as exc:
        logger.warning("F051: agent_id probe query failed: %s", exc)
        return
    finally:
        await conn.close()

    expected = eval_settings.agent_id
    distinct_ids = {r["agent_id"] for r in rows if r["n"] > 0}
    if not distinct_ids:
        logger.warning(
            "F051: corpus tables are EMPTY on the eval DB — every qrel will "
            "score MRR=0. Re-run ingest or check NOUS_EVAL_FIXTURE_VERSION."
        )
        return
    if expected not in distinct_ids:
        logger.warning(
            "F051: agent_id mismatch — EvalSettings.agent_id='%s' but corpus "
            "uses %s. Every Heart sub-search WILL return zero rows. "
            "Set NOUS_EVAL_AGENT_ID to one of those values.",
            expected,
            sorted(distinct_ids),
        )
    else:
        logger.debug("F051: agent_id OK (%s)", expected)


async def _run_async(
    args: argparse.Namespace,
    eval_settings: EvalSettings,
    configs: list[RetrievalConfig],
    resolved_sources: list["ResolvedSource"],
) -> int:
    """Async entry point — does the actual matrix run + report writing.

    Smoke mode (``--smoke`` or no fixtures dir) is a graceful no-DB code
    path: we still load probes and write a report header, but skip the
    matrix run since there is no eval DB to query against. This lets PRs
    verify the harness wires up cleanly without needing the eval-DB
    container running locally.
    """
    db_reachable = _eval_db_reachable(eval_settings)

    # Preflight: socket check on the eval DB port. This produces a clearer
    # error than asyncpg's connection failure when the container is down.
    if not db_reachable and not eval_settings.smoke_mode:
        print(
            f"ERROR: nous-eval-db not reachable at "
            f"{eval_settings.db_host}:{eval_settings.db_port}.\n"
            f"  Run: docker compose --profile eval up -d nous-eval-db",
            file=sys.stderr,
        )
        return 1

    # Load qrels from each available source
    all_qrels: list[Qrel] = []
    for src in resolved_sources:
        if not src.available:
            logger.warning(
                "Source %s skipped: %s", src.spec.name, src._skip_reason
            )
            continue
        try:
            source_enum = QrelSource(src.spec.name)
        except ValueError:
            logger.warning(
                "Source %s not in QrelSource enum; skipping", src.spec.name
            )
            continue
        review_filter = bool(src.spec.review_filter) and not src.include_unreviewed
        try:
            qrels = load_qrels(
                src.resolved_path,
                source_override=source_enum,
                review_filter_enabled=review_filter,
            )
        except ValueError as exc:
            print(f"ERROR loading {src.spec.name}: {exc}", file=sys.stderr)
            return 2
        logger.info(
            "Loaded %d qrels from source=%s path=%s",
            len(qrels),
            src.spec.name,
            src.resolved_path,
        )
        all_qrels.extend(qrels)

    if not all_qrels:
        print(
            "ERROR: no qrels loaded — check fixtures dir + source filters.",
            file=sys.stderr,
        )
        return 1

    # Build base Settings (env-driven) and run the matrix
    main_settings = Settings()
    git_sha = _resolve_git_sha(eval_settings)
    fixture_version = eval_settings.fixture_version

    logger.info(
        "F051: run_started git_sha=%s configs=%s qrels=%d fixture_version=%s "
        "smoke_mode=%s db_reachable=%s",
        git_sha,
        ",".join(c.name for c in configs),
        len(all_qrels),
        fixture_version,
        eval_settings.smoke_mode,
        db_reachable,
    )

    if not db_reachable:
        # Smoke-mode-without-DB: emit an empty report so downstream automation
        # has something to look at, but skip the matrix run.
        logger.warning(
            "F051: smoke mode + eval DB unreachable; skipping matrix run "
            "and writing an empty report."
        )
        run_results: list["RunResult"] = []
    else:
        # Pre-flight integrity probes. Both warn-only — neither blocks the run,
        # but each produces a clear log line if the eval DB is misconfigured.
        await _verify_fixture_version(eval_settings, fixture_version)
        await _verify_corpus_agent_id(eval_settings)
        run_results = await run_matrix(
            configs=configs,
            qrels=all_qrels,
            eval_settings=eval_settings,
            main_settings_template=main_settings,
            top_k=eval_settings.top_k,
        )

    # Gate decision (optional)
    gate_decision = None
    if args.gate_f050:
        gate_decision = decide_gate_f050(
            run_results=run_results,
            resolved_sources=resolved_sources,
            threshold=eval_settings.f050_gate_threshold,
            max_single_regression=eval_settings.f050_gate_max_single_regression,
            require_majority_positive=eval_settings.f050_gate_require_majority_positive,
            top_k=eval_settings.top_k,
        )
        logger.info(
            "F051: gate_decision feature=F050 result=%s reason=%s",
            "PASS" if gate_decision.passed else "FAIL",
            gate_decision.reason,
        )

    # Render reports
    md = render_markdown(
        run_results=run_results,
        resolved_sources=resolved_sources,
        git_sha=git_sha,
        fixture_version=fixture_version,
        gate_decision=gate_decision,
        notes=args.notes,
        config_names_requested=[c.name for c in configs],
    )
    js = render_json(
        run_results=run_results,
        resolved_sources=resolved_sources,
        git_sha=git_sha,
        fixture_version=fixture_version,
        gate_decision=gate_decision,
        notes=args.notes,
    )
    md_path, json_path = write_reports(
        report_dir=eval_settings.report_dir,
        md_content=md,
        json_content=js,
        config_names=[c.name for c in configs],
    )
    print(f"Wrote: {md_path}")
    print(f"Wrote: {json_path}")

    # Persist run history (best-effort)
    if eval_settings.run_history_enabled and not args.no_history:
        try:
            await asyncio.wait_for(
                _persist_run_history(
                    eval_settings=eval_settings,
                    main_settings=main_settings,
                    run_results=run_results,
                    git_sha=git_sha,
                    fixture_version=fixture_version,
                    report_path=str(md_path),
                    notes=args.notes,
                ),
                timeout=eval_settings.run_history_insert_timeout_s,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "F051: run_history_persist_timed_out (%.1fs)",
                eval_settings.run_history_insert_timeout_s,
            )
        except Exception:
            logger.exception("F051: run_history_persist_failed")

    if gate_decision is not None and not gate_decision.passed:
        # Non-zero exit so CI / shell pipelines can detect failed gates.
        return 3
    return 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _eval_db_reachable(s: EvalSettings) -> bool:
    """TCP-connect preflight; faster + clearer error than asyncpg's failure."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sk:
            sk.settimeout(2.0)
            return sk.connect_ex((s.db_host, s.db_port)) == 0
    except OSError:
        return False


def _resolve_git_sha(eval_settings: EvalSettings) -> str:
    """Return EvalSettings.git_sha_override or `git rev-parse HEAD`.

    Falls back to "unknown" when neither is available — eval can run in
    detached / no-git contexts (CI containers).
    """
    if eval_settings.git_sha_override:
        return eval_settings.git_sha_override
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        )
        return proc.stdout.strip()
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return "unknown"


async def _persist_run_history(
    eval_settings: EvalSettings,
    main_settings: Settings,
    run_results: list["RunResult"],
    git_sha: str,
    fixture_version: str,
    report_path: str,
    notes: str,
) -> None:
    """Insert one row into ``nous_system.eval_runs`` on the main DB.

    Best-effort: caller wraps this in ``asyncio.wait_for`` so it can never
    block the harness for more than ``run_history_insert_timeout_s``.
    """
    from sqlalchemy import text

    from nous.storage.database import Database

    db = Database(main_settings)
    try:
        await db.connect()
    except Exception as exc:
        logger.warning("F051: main DB unreachable for run_history: %s", exc)
        await db.engine.dispose()
        return

    try:
        async with db.session() as session:
            metrics_payload = {
                r.config.name: {
                    "metrics": _metrics_compact(r),
                    "duration_seconds": r.duration_seconds,
                    "pipeline_stats_summary": r.pipeline_stats_summary,
                }
                for r in run_results
            }
            qrel_counts = _qrel_counts(run_results)
            configs_payload = [
                {
                    "name": r.config.name,
                    "flags": r.config.flags,
                    "description": r.config.description,
                }
                for r in run_results
            ]
            await session.execute(
                text(
                    """
                    INSERT INTO nous_system.eval_runs (
                        agent_id, git_sha, fixture_version, configs,
                        metrics, qrel_counts, report_path, notes
                    )
                    VALUES (
                        :agent_id, :git_sha, :fixture_version,
                        CAST(:configs AS JSONB),
                        CAST(:metrics AS JSONB),
                        CAST(:qrel_counts AS JSONB),
                        :report_path, :notes
                    )
                    """
                ),
                {
                    "agent_id": eval_settings.agent_id,
                    "git_sha": git_sha,
                    "fixture_version": fixture_version,
                    "configs": json.dumps(configs_payload),
                    "metrics": json.dumps(metrics_payload),
                    "qrel_counts": json.dumps(qrel_counts),
                    "report_path": report_path,
                    "notes": notes,
                },
            )
            await session.commit()
            logger.info("F051: run_history_persisted")
    finally:
        await db.engine.dispose()


def _metrics_compact(run: "RunResult") -> dict:
    m = compute_metrics(run.per_qrel)
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


def _qrel_counts(run_results: list["RunResult"]) -> dict[str, int]:
    """Per-source qrel counts (taken from the first config; identical across configs)."""
    if not run_results:
        return {}
    counts: dict[str, int] = {}
    for q in run_results[0].per_qrel:
        counts[q.qrel_source] = counts.get(q.qrel_source, 0) + 1
    return counts


if __name__ == "__main__":
    raise SystemExit(main())
