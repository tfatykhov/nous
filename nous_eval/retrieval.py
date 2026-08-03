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
from nous_eval.metrics import compute_metrics, leg_visibility
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
    # ------------------------------------------------------------------
    # F065 phase 4 — Edge-provenance penalty A/B.
    # Baseline (1.0, default) vs candidate (0.7). Pair with `baseline` to
    # measure the MRR delta of down-weighting `inferred`-tier graph edges
    # during recall_deep expansion. Flip the default in nous/config.py
    # only after this A/B clears the gate semantics in
    # `decide_gate_f050` (per-source regression cap + majority positive).
    # ------------------------------------------------------------------
    "f065_penalty_on": RetrievalConfig(
        name="f065_penalty_on",
        flags={"graph_inferred_edge_penalty": 0.7},
        description=(
            "F065 phase 4: penalize `inferred`-tier provenance edges by "
            "0.7x during recall_deep expansion. Pair with `baseline` "
            "(penalty=1.0) for A/B."
        ),
    ),
    # F065 autosurface neutrality probe.
    # Toggles the pre_turn hub-shift autosurface flag ON. The autosurface
    # writes into the system prompt only — it does NOT touch recall_deep,
    # so MRR is expected to be byte-identical to baseline. The config
    # exists so the F051 harness can produce the receipt that closes
    # the loop (zero delta confirmed) rather than asserting neutrality
    # by inspection.
    "f065_autosurface_on": RetrievalConfig(
        name="f065_autosurface_on",
        flags={"graph_hub_autosurface_enabled": True},
        description=(
            "F065 follow-up: enable pre_turn hub-shift autosurface. "
            "Autosurface affects system-prompt context, not recall_deep — "
            "expected MRR delta vs baseline is exactly 0."
        ),
    ),
    # ------------------------------------------------------------------
    # F053 — density-eval diagnostic configs (used by
    # `python -m nous_eval.density_eval`, not the retrieval matrix).
    # ------------------------------------------------------------------
    "baseline_loose_ce": RetrievalConfig(
        name="baseline_loose_ce",
        flags={
            # F045 CE-mode thresholds, relaxed ~10% across the board.
            # 2026-04-26 density-eval measured +59.6% edges over baseline
            # at identical related_to precision (0.83 → 0.83).
            "ce_backfill_threshold_fact_fact": 0.55,
            "ce_backfill_threshold_decision_decision": 0.50,
            "ce_backfill_threshold_fact_decision": 0.45,
            "ce_backfill_threshold_fact_episode": 0.45,
            "ce_backfill_threshold_episode_episode": 0.50,
            "ce_backfill_threshold_procedure_any": 0.45,
        },
        description=(
            "F053 diagnostic — F040+F043+F045 with CE-mode cosine thresholds "
            "loosened ~10%. Empirically catches +59.6% more edges on the "
            "F051 eval corpus at identical related_to precision (0.83). "
            "Cross-type evidence_for precision degrades (0.57 → 0.47), so "
            "any production tune-down should be selective per spec F054."
        ),
    ),
    # ------------------------------------------------------------------
    # F054 — selective CE-threshold relaxation (this PR's gate configs).
    # ------------------------------------------------------------------
    # Reproduces pre-F054 behavior. Use as the comparison baseline since
    # `baseline` on this branch already picks up the new F054 defaults.
    "f045_strict_baseline": RetrievalConfig(
        name="f045_strict_baseline",
        flags={
            "ce_backfill_threshold_fact_fact": 0.65,
            "ce_backfill_threshold_decision_decision": 0.60,
            "ce_backfill_threshold_episode_episode": 0.58,
            "ce_backfill_threshold_procedure_any": 0.55,
            "ce_backfill_min_decision_chars": 0,  # disable F054 guard
        },
        description=(
            "Pre-F054 strict thresholds. Reproduces F045 default behavior "
            "for comparison against f054_proposed on this branch."
        ),
    ),
    "f054_proposed": RetrievalConfig(
        name="f054_proposed",
        flags={
            "ce_backfill_threshold_fact_fact": 0.55,
            "ce_backfill_threshold_decision_decision": 0.50,
            "ce_backfill_threshold_episode_episode": 0.50,
            "ce_backfill_threshold_procedure_any": 0.45,
            # cross-type fact_decision/fact_episode UNCHANGED at 0.55
            "ce_backfill_min_decision_chars": 40,
        },
        description=(
            "F054 selective CE relaxation: same-type loosened, cross-type "
            "fact_decision/fact_episode KEPT STRICT, +decision content "
            "guard (40 chars). Eval gate config for the F054 PR."
        ),
    ),
    # ------------------------------------------------------------------
    # F055 — Cross-Turn Residual Activation (used by multi_turn_eval).
    # Pre-stages the F055 flag values; harmless until F055's Settings
    # fields exist (`_apply_config_flags` warns + ignores unknown keys).
    # When F055 ships, no eval-side changes needed — the config becomes
    # meaningful automatically.
    # ------------------------------------------------------------------
    "f055_on": RetrievalConfig(
        name="f055_on",
        flags={
            "residual_activation_enabled": True,
            "residual_decay_mode": "geometric",
            "residual_decay_per_turn": 0.5,
        },
        description=(
            "F055 Cross-Turn Residual Activation enabled (geometric decay). "
            "Used by `python -m nous_eval.multi_turn_eval` against "
            "LongMemEval qrels (F051.5). Pre-F055 implementation, this "
            "config produces baseline numbers (unknown keys ignored)."
        ),
    ),
    "f055_seed_only": RetrievalConfig(
        name="f055_seed_only",
        flags={
            "residual_activation_enabled": True,
            "residual_seed_weight": 0.3,
            "residual_boost_weight": 0.0,  # post-fusion boost off
        },
        description="F055 ablation A: seed injection only, no post-fusion boost.",
    ),
    "f055_boost_only": RetrievalConfig(
        name="f055_boost_only",
        flags={
            "residual_activation_enabled": True,
            "residual_seed_weight": 0.0,  # seed injection off
            "residual_boost_weight": 0.15,
        },
        description="F055 ablation B: post-fusion boost only, no seed injection.",
    ),
    # ------------------------------------------------------------------
    # F055 diagnostic configs — answer "is F055 masked by CE rerank?"
    # ------------------------------------------------------------------
    "ce_off": RetrievalConfig(
        name="ce_off",
        flags={"cross_encoder_enabled": False},
        description="Baseline with cross-encoder disabled — isolation control for f055_no_ce.",
    ),
    "f055_no_ce": RetrievalConfig(
        name="f055_no_ce",
        flags={
            "residual_activation_enabled": True,
            "cross_encoder_enabled": False,
            "residual_decay_mode": "geometric",
            "residual_decay_per_turn": 0.5,
            "residual_boost_weight": 0.15,
        },
        description="F055 on, CE off — does F055 help when nothing downstream overrides it?",
    ),
    "f055_high_boost": RetrievalConfig(
        name="f055_high_boost",
        flags={
            "residual_activation_enabled": True,
            "residual_boost_weight": 0.5,
            "residual_decay_mode": "geometric",
            "residual_decay_per_turn": 0.5,
        },
        description="F055 on with boost=0.5 (vs default 0.15) — can bigger boost escape CE head-cut?",
    ),
    # ------------------------------------------------------------------
    # Spreading-activation gate sensitivity. Default ``enabled="auto"`` +
    # ``density_threshold=3.0`` has never been A/B'd. ``graph_off`` kills
    # graph recall entirely; these isolate the gate logic alone.
    # ------------------------------------------------------------------
    "spread_force_on": RetrievalConfig(
        name="spread_force_on",
        flags={"spreading_activation_enabled": "true"},
        description=(
            "Force spreading activation regardless of density. If MRR/R@K "
            "improves vs baseline, the auto-gate is too conservative."
        ),
    ),
    "spread_force_off": RetrievalConfig(
        name="spread_force_off",
        flags={"spreading_activation_enabled": "false"},
        description=(
            "Disable spreading activation but keep 1-hop graph recall. "
            "Isolates spreading's lift from the rest of graph recall "
            "(unlike `graph_off` which kills both)."
        ),
    ),
    "spread_low_threshold": RetrievalConfig(
        name="spread_low_threshold",
        flags={
            "spreading_activation_enabled": "auto",
            "spreading_activation_density_threshold": 1.0,
        },
        description=(
            "Drop the auto-gate threshold from 3.0 to 1.0 — spreading "
            "fires on much sparser graphs. Pair with `spread_force_on` "
            "to triangulate the right default."
        ),
    ),
    # NOTE (2026-07-11): heart fact seeding is now DEFAULT spreading
    # behavior (unflagged, per owner directive + MAB no-harm A/B), so
    # `spread_force_on` exercises it — no separate heart-seeds config.
    # ------------------------------------------------------------------
    # RRF fusion knobs. Hybrid search blends vector + keyword via
    # ``rrf_score = vector_weight/(k+v_rank) + (1-vector_weight)/(k+k_rank)``.
    # Defaults: ``vector_weight=0.7``, ``rrf_k=60``. Both route through
    # RuntimeConfig (reset per-config in run_matrix), so Settings overrides
    # take effect.
    # ------------------------------------------------------------------
    "rrf_vector_heavy": RetrievalConfig(
        name="rrf_vector_heavy",
        flags={"vector_weight": 0.9},
        description=(
            "Vector-leaning fusion (0.9 vector / 0.1 keyword). Tests "
            "whether keyword signal hurts on this corpus."
        ),
    ),
    "rrf_balanced": RetrievalConfig(
        name="rrf_balanced",
        flags={"vector_weight": 0.5},
        description=(
            "Balanced fusion (0.5 vector / 0.5 keyword). Default-equivalent "
            "for a corpus where keyword recall matters more."
        ),
    ),
    "rrf_keyword_heavy": RetrievalConfig(
        name="rrf_keyword_heavy",
        flags={"vector_weight": 0.3},
        description=(
            "Keyword-leaning fusion (0.3 vector / 0.7 keyword). Useful "
            "when queries are jargon-rich and embeddings drift."
        ),
    ),
    "rrf_k_low": RetrievalConfig(
        name="rrf_k_low",
        flags={"rrf_k": 10},
        description=(
            "Sharper rank weighting (k=10 vs default 60). Top-1 dominates "
            "fused score; tail contributions decay fast."
        ),
    ),
    "rrf_k_high": RetrievalConfig(
        name="rrf_k_high",
        flags={"rrf_k": 200},
        description=(
            "Smoother rank weighting (k=200 vs default 60). Tail "
            "candidates contribute more; useful when relevant docs "
            "often land outside top-3 in either channel."
        ),
    ),
    # ------------------------------------------------------------------
    # CE-off + RRF combos. CE rerank dominates the head positions and
    # may flatten any RRF lift in baseline runs. These configs disable
    # CE so RRF's effect on the top-K is observable.
    # ------------------------------------------------------------------
    "ce_off_rrf_vector_heavy": RetrievalConfig(
        name="ce_off_rrf_vector_heavy",
        flags={"cross_encoder_enabled": False, "vector_weight": 0.9},
        description="CE off + RRF vector-leaning (0.9). Surface RRF effect without CE flattening.",
    ),
    "ce_off_rrf_balanced": RetrievalConfig(
        name="ce_off_rrf_balanced",
        flags={"cross_encoder_enabled": False, "vector_weight": 0.5},
        description="CE off + RRF balanced (0.5). Surface RRF effect without CE flattening.",
    ),
    "ce_off_rrf_keyword_heavy": RetrievalConfig(
        name="ce_off_rrf_keyword_heavy",
        flags={"cross_encoder_enabled": False, "vector_weight": 0.3},
        description="CE off + RRF keyword-leaning (0.3). Surface RRF effect without CE flattening.",
    ),
    "ce_off_rrf_k_low": RetrievalConfig(
        name="ce_off_rrf_k_low",
        flags={"cross_encoder_enabled": False, "rrf_k": 10},
        description="CE off + sharp k=10. Top-1 RRF dominance without CE override.",
    ),
    "ce_off_rrf_k_high": RetrievalConfig(
        name="ce_off_rrf_k_high",
        flags={"cross_encoder_enabled": False, "rrf_k": 200},
        description="CE off + smooth k=200. Tail-friendly RRF without CE override.",
    ),
    "ce_off_spread_on": RetrievalConfig(
        name="ce_off_spread_on",
        flags={"cross_encoder_enabled": False, "spreading_activation_enabled": "true"},
        description="CE off + spreading forced on. Surface graph-hopping lift without CE override.",
    ),
    "ce_off_mmr_on": RetrievalConfig(
        name="ce_off_mmr_on",
        flags={"cross_encoder_enabled": False, "mmr_enabled": True, "mmr_skip_after_ce": False},
        description="CE off + MMR diversity (default lambda=0.7). Tests if MMR adds when CE isn't head-cutting.",
    ),
    # ------------------------------------------------------------------
    # Channel-isolation diagnostics. Answers: does the keyword channel
    # actually contribute anything? Pure vector vs default-fused.
    # ------------------------------------------------------------------
    "vector_only": RetrievalConfig(
        name="vector_only",
        flags={"cross_encoder_enabled": False, "vector_weight": 1.0},
        description=(
            "Pure vector — no keyword fusion. If this ties ce_off, the "
            "keyword channel is silent on this corpus and could be "
            "removed to save one FTS query per recall."
        ),
    ),
    "keyword_only": RetrievalConfig(
        name="keyword_only",
        flags={"cross_encoder_enabled": False, "vector_weight": 0.0},
        description=(
            "Pure keyword — no vector fusion. Worst-case bound: how much "
            "does FTS alone recover? If close to ce_off, vector is "
            "redundant on this corpus."
        ),
    ),
    # ------------------------------------------------------------------
    # F052/F054 validation configs — exercise the flags this branch ships.
    # Each isolates the new behavior so eval predicts the production lift
    # before deployment.
    # ------------------------------------------------------------------
    "f052_on": RetrievalConfig(
        name="f052_on",
        flags={
            "cross_encoder_enabled": True,
            "cross_encoder_episode_skip_enabled": True,
            "cross_encoder_episode_skip_threshold": 0.5,
        },
        description=(
            "F052: CE rerank on with episode-share skip gate (default "
            "threshold 0.5). Predicted: ties baseline on nous_prod "
            "(fact-dominant); ties ce_off on longmemeval (episode-"
            "dominant) — recovers the +5.2% MRR longmemeval lift "
            "without losing the +2.2% nous_prod lift."
        ),
    ),
    "f052_off_explicit": RetrievalConfig(
        name="f052_off_explicit",
        flags={
            "cross_encoder_enabled": True,
            "cross_encoder_episode_skip_enabled": False,
        },
        description=(
            "F052 explicitly off — should match baseline-with-CE-on. "
            "Sanity check the gate flag default doesn't change behavior."
        ),
    ),
    "f054_keyword_off": RetrievalConfig(
        name="f054_keyword_off",
        flags={
            "cross_encoder_enabled": True,
            "hybrid_search_keyword_enabled": False,
        },
        description=(
            "F054: vector-only hybrid_search (keyword channel disabled). "
            "Predicted: ties baseline since channel-iso showed vector_only "
            "matches ce_off byte-for-byte on lme. Validates flag wires "
            "to the production hot path."
        ),
    ),
    "f052_low_threshold": RetrievalConfig(
        name="f052_low_threshold",
        flags={
            "cross_encoder_enabled": True,
            "cross_encoder_episode_skip_enabled": True,
            "cross_encoder_episode_skip_threshold": 0.15,
        },
        description=(
            "F052 with low threshold (0.15) — matches longmemeval corpus "
            "shape where facts outnumber episodes 4.7x but the qrels "
            "target episode-flavored recall. Tests whether F052's "
            "mechanism works at all on this data."
        ),
    ),
    "f052_very_low_threshold": RetrievalConfig(
        name="f052_very_low_threshold",
        flags={
            "cross_encoder_enabled": True,
            "cross_encoder_episode_skip_enabled": True,
            "cross_encoder_episode_skip_threshold": 0.05,
        },
        description=(
            "F052 with extremely low threshold (0.05) — fires whenever "
            "any episodes are present in the candidate set."
        ),
    ),
    "f052_and_f054_combined": RetrievalConfig(
        name="f052_and_f054_combined",
        flags={
            "cross_encoder_enabled": True,
            "cross_encoder_episode_skip_enabled": True,
            "cross_encoder_episode_skip_threshold": 0.5,
            "hybrid_search_keyword_enabled": False,
        },
        description=(
            "F052 + F054 stacked — predicted ceiling for this branch: "
            "longmemeval lifts (CE skip on episode dominance), nous_prod "
            "stays at baseline, keyword channel off everywhere."
        ),
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
        # N7: the depth the matrix actually scored at — the leg-visibility
        # cutline must match it, or --top-k runs mislabel measured legs.
        top_k=eval_settings.top_k,
    )
    js = render_json(
        run_results=run_results,
        resolved_sources=resolved_sources,
        git_sha=git_sha,
        fixture_version=fixture_version,
        gate_decision=gate_decision,
        notes=args.notes,
        top_k=eval_settings.top_k,
    )
    md_path, json_path = write_reports(
        report_dir=eval_settings.report_dir,
        md_content=md,
        json_content=js,
        config_names=[c.name for c in configs],
    )
    print(f"Wrote: {md_path}")
    print(f"Wrote: {json_path}")

    # Persist run history (best-effort) — uses shared helper that targets
    # the EVAL DB (Phase 1 finish, 2026-04-27).
    if eval_settings.run_history_enabled and not args.no_history:
        from nous_eval.run_history import persist_run_history as _persist_shared

        await _persist_shared(
            eval_settings=eval_settings,
            main_settings=main_settings,
            git_sha=git_sha,
            fixture_version=fixture_version,
            configs_payload=[
                {
                    "name": r.config.name,
                    "flags": r.config.flags,
                    "description": r.config.description,
                    "harness": "retrieval",
                }
                for r in run_results
            ],
            metrics_payload={
                r.config.name: {
                    "metrics": _metrics_compact(r, eval_settings.top_k),
                    "duration_seconds": r.duration_seconds,
                    "pipeline_stats_summary": r.pipeline_stats_summary,
                }
                for r in run_results
            },
            qrel_counts=_qrel_counts(run_results),
            report_path=str(md_path),
            notes=args.notes,
        )

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


def _metrics_compact(run: "RunResult", top_k: int = 10) -> dict:
    """Compact metrics for the persisted ``nous_system.eval_runs`` row.

    N7/codex-R2: this payload is built INDEPENDENTLY of the JSON report
    file, so it needs its own copy of the depth and the leg-visibility
    rows. Without them, historical regression analysis cannot reconstruct
    whether an old null came from a leg banded below the cutoff — the
    report file is not guaranteed to still exist.

    ``p_at_10``/``r_at_10``/``ndcg_at_10`` keep their historical key names
    for schema continuity; ``top_k`` records the depth they were actually
    computed at.
    """
    m = compute_metrics(run.per_qrel, top_k=top_k)
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
        # N7: the untruncated view + the depth these numbers mean.
        "top_k": top_k,
        "r_at_served": m.r_at_served,
        "mean_served": m.mean_served,
        "recall_curve": {str(k): v for k, v in sorted(m.recall_curve.items())},
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
                run.per_qrel, cutoff=top_k,
                expected_legs=run.expected_legs,
            )
        ],
        # N1: non-empty means these numbers came from a PARTIAL retrieval.
        "n_qrels_partial": sum(1 for q in run.per_qrel if q.stage_errors),
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
