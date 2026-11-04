"""Settings for one harness run: the scratch DB, the per-conversation agent, and
the arm's setting overrides.

An arm is a set of overrides (``--set NOUS_NAME=VALUE``), parsed by ``Settings``
exactly as the deployed environment is. A base config can come from a dotenv file
(``--flags-file``, e.g. a prod env snapshot). Both accept only the settings named
in ``ARM_SETTINGS``; every other name, credentials included, is refused (``--set``)
or ignored (flags file). Credentials come from the process environment / ``.env``
as for every eval script. Run records store override names, and values only as
the allowlisted settings snapshot shows them.

Compare arms that differ in one override. A read-path setting (e.g.
``NOUS_CONTEXT_QUERY_MODE``) is compared on one ingest; a write-path setting needs
its own ingest under its own ``--agent-tag``.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# The settings an arm may change: memory, retrieval, context, sleep, extraction,
# graph, spreading, temporal, profile-card and model choices. Written out by hand
# from nous/config.py; no prefix matching. Credentials, URLs, logins, delivery,
# the summarizer and fact-extractor switches (episode_summary_enabled,
# fact_extraction_enabled) and anything the harness sets itself are absent. The
# outcome-detection and correction-extraction switches are arm settings.
ARM_SETTINGS = frozenset(
    {
        # models. embedding_model/embedding_dimensions must match between an ingest and the
        # answers on it (they come in with a prod flags file); never vary them on an answer arm.
        "model", "background_model", "contradiction_model", "rubric_outcome_model", "query_expansion_model",
        "epistemic_gate_model", "date_leg_model", "cross_encoder_model", "embedding_model", "embedding_dimensions",
        # retrieval and ranking
        "vector_weight", "rrf_k", "hybrid_search_keyword_enabled", "hybrid_keyword_or_fallback_enabled",
        "relevance_floor_enabled", "relevance_drop_ratio", "relevance_min_results", "relevance_max_results",
        "staleness_penalty_enabled", "staleness_half_life_days", "recall_backstop_enabled",
        "override_prior_marking_enabled", "keyed_fact_leg_enabled", "keyed_fact_leg_k", "keyed_fact_leg_score",
        "keyed_fact_leg_rounds", "keyed_fact_leg_k2", "keyed_fact_leg_r2_max_keys", "keyed_fact_leg_r2_max_candidates",
        "exemplar_mode_enabled", "exemplar_top_k", "exemplar_leg_score", "exemplar_min_similarity",
        "exemplar_max_query_words", "mmr_enabled", "mmr_diversity_weight", "mmr_skip_after_ce",
        "coherent_ranking_enabled", "cross_encoder_enabled", "cross_encoder_max_candidates", "cross_encoder_text_limit",
        "residual_activation_enabled", "residual_decay_mode", "residual_decay_per_turn", "residual_power_law_alpha",
        "residual_activation_floor", "residual_top_k_carried", "residual_top_n_seeds", "residual_seed_weight",
        "residual_boost_weight", "query_expansion_enabled", "query_expansion_timeout_seconds",
        "query_expansion_max_variants", "query_expansion_min_words", "epistemic_gate_enabled",
        "epistemic_gate_timeout_seconds", "recency_resolver_enabled", "recency_resolver_similarity_floor",
        "episode_chunk_recall_limit", "heart_rrf_penalty_limit", "chunk_rrf_penalty_limit",
        "chunk_hybrid_search_enabled", "recall_include_parent_episodes", "recall_max_parent_episodes",
        "recall_parent_episode_truncate", "recall_exclude_context_ids",
        # pre-turn context
        "context_query_mode", "context_query_short_chars", "context_skip_unchanged_query",
        "context_skip_unchanged_ttl_seconds", "budget_scale_enabled", "context_budget_overrides",
        "fact_format_max_chars", "fact_format_full_top_n", "fact_pin_top_k", "followup_episode_budget_enabled",
        "followup_deictic_detection_enabled", "followup_greeting_request_detection_enabled",
        "followup_first_turn_episode", "session_group_heart_section", "temporal_context_enabled",
        # profile and profile card
        "profile_identity_dedup_scope", "profile_fact_limit", "profile_recency_enabled", "profile_exclude_sources",
        "profile_core_enabled", "profile_core_limit", "profile_core_probation_days", "profile_intent_leg_enabled",
        "profile_intent_leg_limit", "profile_intent_leg_budget", "profile_intent_leg_min_score",
        "profile_card_writer_enabled", "profile_card_enabled", "profile_card_max_tokens", "profile_card_max_entries",
        "profile_card_source_limit", "profile_card_episodes",
        # extraction, admission, supersession
        "transcript_max_chars", "transcript_message_max_chars", "episode_summary_max_chunks",
        "episode_summary_max_tokens", "episode_seed_summary_chars", "episode_min_content_length",
        "episode_chunks_enabled", "episode_chunk_size", "episode_chunk_overlap", "episode_chunk_max_per_episode",
        "episode_chunk_min_transcript_chars", "extraction_enumerative_enabled", "enumerative_density_threshold",
        "enumerative_max_facts_per_episode", "enumerative_max_chunks_per_episode", "enumerative_classifier",
        "enumerative_min_content_chars", "exemplar_extraction_enabled", "exemplar_density_threshold",
        "exemplar_max_per_episode", "exemplar_min_content_chars", "extraction_coverage_broadened",
        "extraction_input_hardening_enabled", "candidate_facts_event_limit", "candidate_facts_stable_limit",
        "fact_min_content_chars", "fact_supersession_threshold", "fact_dedup_threshold", "fact_native_cosine_threshold",
        "fact_dedup_tiebreaker_enabled", "admission_control_enabled", "admission_shadow_mode", "admission_threshold",
        "admission_w_utility", "admission_w_confidence", "admission_w_novelty", "admission_w_recency",
        "admission_w_type_prior", "admission_recency_lambda", "admission_utility_llm_enabled",
        "supersession_lineage_mode", "supersession_key_resolution_enabled", "supersession_policy",
        "supersession_key_candidates_cap", "supersession_sweep_max_pairs", "same_slot_conflict_routing_enabled",
        "entity_keys_max_per_fact", "entity_key_min_chars", "contradiction_detection",
        "contradiction_similarity_threshold", "contradiction_recheck_cooldown_days", "correction_extraction_enabled",
        "rubric_outcome_detection_enabled",
        # temporal
        "temporal_extraction_enabled", "happened_before_relatedness_threshold", "date_aware_boost_enabled",
        "date_aware_boost_factor", "date_aware_boost_window_pad_days", "date_leg_enabled", "date_leg_k",
        "date_leg_pad_days", "date_leg_timeout_seconds",
        # sleep and consolidation
        "sleep_reflection_hours", "sleep_reflection_summary_chars", "sleep_contradiction_fact_chars",
        "stale_scan_excluded_categories", "cluster_consolidation_min_facts", "cluster_consolidation_max_facts",
        "cluster_refusal_ttl_days", "episode_relink_enabled", "episode_relink_min_age_hours",
        "episode_relink_max_per_cycle", "chunk_consolidation_enabled", "dead_edge_pruning_enabled",
        "dead_edge_pruning_max_per_cycle", "tinyhippo_lite_enabled", "tinyhippo_prp_threshold",
        "tinyhippo_recall_touch_enabled", "tinyhippo_consolidated_boost_factor", "tinyhippo_consolidated_boost_enabled",
        "tinyhippo_alpha", "tinyhippo_downscale_enabled",
        # graph and linking
        "auto_link_threshold", "auto_link_max", "cross_type_link_min_content_chars", "graph_link_candidate_window_days",
        "cross_type_linking_enabled", "cross_type_threshold", "cross_type_same_threshold", "graph_recall_enabled",
        "graph_recall_max_expand", "graph_recall_decay", "graph_recall_max_neighbors", "graph_inferred_edge_penalty",
        "graph_backfill_enabled", "graph_backfill_max_facts", "graph_backfill_max_decisions",
        "graph_backfill_max_episodes", "graph_backfill_max_procedures", "graph_backfill_max_chunks",
        "graph_backfill_max_chunks_cross_episode", "graph_threshold_fact_fact", "graph_threshold_fact_decision",
        "graph_threshold_fact_episode", "graph_threshold_decision_decision", "graph_threshold_episode_episode",
        "graph_threshold_procedure_any", "graph_threshold_chunk_fact", "graph_threshold_chunk_chunk_intra",
        "graph_threshold_chunk_chunk_cross", "graph_threshold_chunk_fact_cross", "chunk_cross_episode_top_k",
        "ce_backfill_enabled", "ce_backfill_top_k", "ce_backfill_min_score", "ce_backfill_threshold_fact_fact",
        "ce_backfill_threshold_fact_decision", "ce_backfill_threshold_fact_episode",
        "ce_backfill_threshold_decision_decision", "ce_backfill_threshold_episode_episode",
        "ce_backfill_threshold_procedure_any", "ce_backfill_min_content_chars", "ce_backfill_min_decision_chars",
        "heart_graph_all_types_enabled", "heart_graph_neighbors_per_seed", "graph_neighbor_seed_score_enabled",
        "graph_adjacency_boost_enabled", "graph_adjacency_boost_alpha", "graph_adjacency_boost_exclude_deterministic",
        "graph_associative_query_gate_enabled", "graph_associative_query_gate_mode",
        "graph_associative_query_gate_min_cosine", "graph_associative_query_gate_seed_min",
        "graph_associative_query_gate_drop_below", "graph_associative_edge_multiplier", "comention_linking_enabled",
        "comention_max_degree", "comention_max_edges_per_node", "comention_weight", "comention_min_entity_chars",
        "comention_max_facts_per_cycle", "comention_v2_enabled", "comention_index_max_facts",
        "comention_prune_max_per_cycle", "comention_v2_max_degree", "comention_min_cosine",
        "cooccurrence_linking_enabled", "cooccurrence_weight", "cooccurrence_max_episode_facts",
        "cooccurrence_max_episodes_per_cycle", "cooccurrence_v2_enabled", "cooccurrence_min_cosine",
        "cooccurrence_ordinal_window", "cooccurrence_max_edges_per_fact", "cooccurrence_prune_max_per_cycle",
        # spreading activation
        "spreading_activation_enabled", "spreading_activation_density_threshold", "spreading_activation_decay",
        "spreading_activation_max_depth", "spreading_score_depth1_parity", "spreading_activation_floor",
        "spreading_fan_normalisation_enabled", "spreading_fan_free_degree", "spreading_fan_exponent",
        "spreading_associative_edges_enabled", "spreading_associative_weight_mult", "spreading_query_gate_enabled",
        "spreading_query_gate_mode", "spreading_query_gate_q_lo", "spreading_query_gate_q_hi",
        "spreading_query_gate_seed_min", "spreading_budget_enabled", "spreading_budget_max_rows",
        "spreading_budget_cutline_rank", "spreading_aggregation", "spreading_rescore_existing_enabled",
        "spreading_rescore_beta",
    }
)  # fmt: skip

# Set by the harness for every arm, recorded, never arm-able. Facts carry their
# BEAM dates, so the sleep stale scan (created_at older than this many days and
# not recalled -> deactivated) would otherwise wipe the corpus.
EVAL_OVERRIDES: dict[str, Any] = {"stale_scan_age_days": 36500}

# What still runs on the wall clock, recorded with every run.
CLOCK_NOTES = (
    "Episodes (started_at, ended_at, created_at) and facts (created_at, learned_at, and mentioned_at where the "
    "column exists) carry the BEAM session's date; everything else is written at ingest time.",
    "stale_scan_age_days is set to 36500 for every arm, so the sleep stale scan never deactivates a fact.",
    "Tier 0 of the pre-turn context shows the wall-clock date, not the conversation's.",
    "DateWindowParser resolves relative date phrases in a question against the wall clock.",
    "Windows measured from now (staleness penalty, episode recency decay, profile probation, sleep reflection "
    "hours) see every BEAM memory as old.",
)

# Recorded with every run (plus each overridden field and EVAL_OVERRIDES). Never a dump.
SNAPSHOT_FIELDS = (
    "model",
    "background_model",
    "contradiction_model",
    "api_backend",
    "embedding_model",
    "embedding_dimensions",
    "context_query_mode",
    "context_query_short_chars",
    "context_skip_unchanged_query",
    "query_expansion_enabled",
    "episode_summary_enabled",
    "fact_extraction_enabled",
    "rubric_outcome_detection_enabled",
    "correction_extraction_enabled",
    "episode_chunks_enabled",
    "temporal_extraction_enabled",
    "temporal_context_enabled",
    "admission_control_enabled",
    "cross_type_linking_enabled",
    "graph_backfill_enabled",
    "residual_activation_enabled",
    "epistemic_gate_enabled",
    "staleness_penalty_enabled",
    "transcript_message_max_chars",
    "sleep_reflection_hours",
    "stale_scan_age_days",
)

# A database the harness may write: eval and test databases only, unless overridden.
SCRATCH_DB_RE = re.compile(r"^(nous_eval|nous_test|beam)")


def env_name_to_field() -> dict[str, str]:
    """``NOUS_CONTEXT_QUERY_MODE`` -> ``context_query_mode``, aliases included."""
    from nous.config import Settings

    prefix = Settings.model_config.get("env_prefix", "")
    out: dict[str, str] = {}
    for name, info in Settings.model_fields.items():
        alias = info.validation_alias
        env = alias if isinstance(alias, str) else f"{prefix}{name}"
        out[env.upper()] = name
    return out


def parse_overrides(pairs: list[str]) -> dict[str, str]:
    """``["NOUS_X=1", ...]`` -> ``{"NOUS_X": "1"}``. A name outside ARM_SETTINGS is an error."""
    known = env_name_to_field()
    out: dict[str, str] = {}
    for pair in pairs:
        name, sep, value = pair.partition("=")
        name = name.strip().upper()
        if not sep or not name:
            raise SystemExit(f"--set expects NAME=VALUE, got {pair!r}")
        if known.get(name) not in ARM_SETTINGS:
            raise SystemExit(f"--set {name}: not an arm setting (see nous_eval/beam/arms.py ARM_SETTINGS)")
        out[name] = value
    return out


def load_flags_file(path: Path) -> tuple[dict[str, str], int]:
    """The ARM_SETTINGS entries of a dotenv file; returns (flags, number of other names ignored)."""
    from dotenv import dotenv_values

    known = env_name_to_field()
    flags: dict[str, str] = {}
    ignored = 0
    for name, value in dotenv_values(path).items():
        if value is not None and known.get(name.upper()) in ARM_SETTINGS:
            flags[name.upper()] = value
        else:
            ignored += 1
    return flags, ignored


@contextmanager
def _environ(values: dict[str, str]) -> Iterator[None]:
    saved = {k: os.environ.get(k) for k in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def check_scratch_db(db_url: str, *, allow_any: bool = False) -> None:
    """Refuse a database whose name is not an eval/test name, unless told it is scratch."""
    name = urlparse(db_url.replace("postgresql+asyncpg://", "postgresql://")).path.strip("/")
    if not allow_any and not SCRATCH_DB_RE.match(name):
        raise SystemExit(
            f"database {name!r} does not look like a scratch database (expected a name starting with "
            "nous_eval, nous_test or beam); pass --i-know-this-is-scratch if it is one"
        )


def build_settings(db_url: str, agent_id: str, overrides: dict[str, str]) -> Any:
    """Settings parsed with ``overrides`` in the environment, bound to ``db_url`` and
    ``agent_id``, with EVAL_OVERRIDES applied."""
    from nous.config import Settings

    p = urlparse(db_url.replace("postgresql+asyncpg://", "postgresql://"))
    if not (p.hostname and p.username and p.path.strip("/")):
        raise SystemExit("--db-url must look like postgresql+asyncpg://user:password@host:port/dbname")
    with _environ(overrides):
        base = Settings()
    for name, value in EVAL_OVERRIDES.items():
        if getattr(base, name) != value:
            logger.info("eval override: %s=%r replaces the configured %r", name, value, getattr(base, name))
    return base.model_copy(
        update={
            "db_host": p.hostname,
            "db_port": p.port or 5432,
            "db_user": p.username,
            "db_password": p.password or "",
            "db_name": p.path.strip("/"),
            "agent_id": agent_id,
            **EVAL_OVERRIDES,
        }
    )


def settings_snapshot(settings: Any, override_names: list[str]) -> dict[str, Any]:
    """Allowlisted settings values; a name this code lacks is recorded as ``"absent"``."""
    fields = type(settings).model_fields
    by_env = env_name_to_field()
    names = list(dict.fromkeys([*SNAPSHOT_FIELDS, *EVAL_OVERRIDES, *(by_env[n] for n in override_names)]))
    return {n: (getattr(settings, n) if n in fields else "absent") for n in names}


def code_revision() -> dict[str, Any]:
    """Git revision of the imported ``nous`` package (the code under test)."""
    import nous

    root = Path(nous.__file__).resolve().parent.parent

    def git(*args: str) -> str:
        try:
            return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True).stdout.strip()
        except OSError:
            return ""

    dirty = git("status", "--porcelain", "--", "nous", "nous_eval")
    return {
        "sha": git("rev-parse", "HEAD") or "unknown",
        "branch": git("rev-parse", "--abbrev-ref", "HEAD") or "unknown",
        "dirty_files": len(dirty.splitlines()) if dirty else 0,
    }


def check_out_dir(path: Path) -> None:
    """Refuse an output directory that git would track: eval outputs never enter the repo."""
    probe = path.resolve()
    while not probe.exists():
        probe = probe.parent
    top = subprocess.run(["git", "-C", str(probe), "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    if top.returncode != 0:
        return  # not inside a git work tree
    ignored = subprocess.run(["git", "-C", top.stdout.strip(), "check-ignore", "-q", str(path.resolve())])
    if ignored.returncode != 0:
        raise SystemExit(f"--out-dir {path} is inside a git work tree and not ignored; use a path outside the repo")
