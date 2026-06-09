"""Settings via pydantic-settings with NOUS_ env prefix.

DB connection fields use validation_alias to read from the same unprefixed
env vars (DB_PASSWORD, DB_PORT, etc.) that docker-compose uses, so a single
.env file drives both the container and the Python app.
"""

import json
import tempfile
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="NOUS_",
        env_file=".env",
        # Tolerate env vars beyond what's declared on Settings. Production
        # deployments often carry feature-specific env vars (Google Drive,
        # third-party integrations, scratch experiments) that the in-tree
        # Settings doesn't know about yet. Without this, pydantic's
        # default extra="forbid" makes Settings() raise on first unknown
        # var — turning every prod-current .env into a checkout breakage
        # waiting for a config.py PR. Matches EvalSettings's behavior.
        extra="ignore",
    )

    # DB connection — unprefixed aliases match docker-compose env vars
    db_host: str = Field("localhost", validation_alias="DB_HOST")
    db_port: int = Field(5432, validation_alias="DB_PORT")
    db_user: str = Field("nous", validation_alias="DB_USER")
    db_password: str = Field("nous_dev_password", validation_alias="DB_PASSWORD")
    db_name: str = Field("nous", validation_alias="DB_NAME")

    db_pool_size: int = 10
    db_max_overflow: int = 5
    agent_id: str = "nous-default"
    embedding_model: str = "text-embedding-3-small"
    embedding_dimensions: int = 1536
    # Audit D2/S7 (2026-06-09): bounded in-process LRU on EmbeddingProvider
    # (entries; 0 disables). Eliminates the 4-7x repeat query embeds per
    # recall and repeat content/template embeds per learn + sleep cycle.
    # Vectors stored packed float32 (~4 B/dim): 1024 x 1536 ~ 6 MB.
    embedding_cache_size: int = Field(default=1024, ge=0)
    log_level: str = "info"

    # Brain settings
    openai_api_key: str = Field("", validation_alias="OPENAI_API_KEY")
    auto_link_threshold: float = 0.85
    auto_link_max: int = 3
    quality_block_threshold: float = 0.5

    # F058: Confidence calibration scaling. Agent-recorded confidence is
    # systemically ~20% overconfident on Nous prod data (Brier 0.252,
    # gap +19.8% across 401 reviewed decisions). Default 0.7627 was
    # derived empirically; set 1.0 to disable scaling (legacy behavior).
    confidence_calibration_factor: float = 0.7627

    # F022 live-linker content guard (post-F058 audit, edge precision
    # report 2026-04-30): empty/short source or target content drove ~30%
    # of NO/WEAK verdicts on `informed_by` and `evidence_for`. Mirrors
    # F054's NOUS_CE_BACKFILL_MIN_DECISION_CHARS=40 but for the live
    # event-bus linker (graph_linker.link_fact_to_decisions /
    # link_fact_to_facts). Set to 0 to disable.
    cross_type_link_min_content_chars: int = 40

    # F058 follow-up (2026-05-01): use tool-use structured output for
    # ConversationCompactor checkpoint summaries. The eval measured 31%
    # silent fact loss with the free-form prompt; structured output
    # forces the model to enumerate facts in an array that can't be
    # paraphrased away. Set to false to revert to the legacy free-form
    # prompt path.
    compaction_structured_facts_enabled: bool = True

    # F059 (2026-05-05): defense-in-depth hallucination guard on compaction
    # output. Extracts entity tokens (emails, IPs, version strings, file
    # paths, named tokens) from the input and the summary, flags entities
    # in the summary that are absent from the input. Warn-only by default
    # so we measure false-positive rate before activating fallback.
    # eval_compaction_fidelity.md showed worst-case substitutions:
    # `marcus.webb@acme.com` → `david.park@acmecorp.com` (authoritative-
    # looking but fabricated). This guard catches that pattern.
    compaction_hallucination_guard_enabled: bool = True
    compaction_hallucination_max_suspect_count: int = 2
    # Fall back to truncation when suspect count exceeds the threshold.
    # Default off — gather false-positive baseline from logs first.
    compaction_hallucination_fallback_enabled: bool = False
    # Persist every fire (any non-empty suspect list, threshold or not)
    # to nous_system.events for retrospective TP/FP audit. Without this,
    # Docker log rotation drops evidence we'd need to decide whether to
    # flip `_fallback_enabled`. Mirrors F026 persistence pattern.
    compaction_hallucination_persist_enabled: bool = True

    # F026 decision persistence — log every action-gate verdict and claim-
    # verification outcome to nous_system.events so a retrospective accuracy
    # eval can run against actual production behavior. Fire-and-forget via
    # asyncio.create_task to avoid latency on the gate path.
    f026_persistence_enabled: bool = True

    # Anti-hallucination (F016 Phase 0)
    anti_hallucination_prompt: bool = True

    # F025 prep: hybrid search vector weight (keyword_weight = 1 - vector_weight)
    vector_weight: float = 0.7
    rrf_k: int = 60  # RRF smoothing constant (F025)
    # F053 (2026-05-03): orphan-edge sleep cleanup.
    # F031 MERGE / F027 cluster_consolidation deactivate facts but the
    # `brain.graph_edges` rows incident to those facts remain. Spreading
    # activation walks edges only (no `active` filter) so it wastes hops
    # on dead nodes. New sleep phase prunes edges where either endpoint is
    # an inactive node, bounded per cycle to avoid long exclusive locks.
    dead_edge_pruning_enabled: bool = True
    dead_edge_pruning_max_per_cycle: int = 1000

    # F054 (2026-05-03): keyword channel toggle.
    # F051 channel-isolation eval (90 nous_prod + 20 longmemeval qrels) showed
    # vector_only ties default RRF byte-for-byte on longmemeval and -0.2% on
    # nous_prod; keyword_only collapses (MRR 0.07/0.35). Operators with
    # vector-dominant corpora can flip this off to save one FTS query per
    # recall. Default True preserves current behavior; eval corpora don't
    # represent jargon-heavy ingestion (codenames, IDs, rare terms).
    #
    # Caveat: when False, _rrf_merge sees an empty keyword list, so every
    # candidate's RRF score = vector_weight/(k + v_rank) + (1-vector_weight)/
    # (k + penalty_rank). The keyword half of the score is uniformly suppressed
    # rather than absent, which can shift absolute scores and interact with
    # downstream relevance_floor / staleness_penalty consumers. Order is
    # preserved; absolute thresholds may need tuning if you flip this off.
    hybrid_search_keyword_enabled: bool = True

    # F017: Relevance floor
    relevance_floor_enabled: bool = True

    # F038-2.1: Procedure score floor (embedding mode only)
    procedure_score_floor: float = 0.40

    # F079 catalog-first procedure delivery (progressive disclosure, à la Claude Code):
    #   BREADTH — a static `## Procedure Catalog` listing active procedure names+descs
    #     (proc_catalog_enabled). Renders stable fields only (no activation/effectiveness),
    #     so it is byte-identical BETWEEN procedure CRUD events → cached on the static tier.
    #     NOTE: it is NOT immutable — a learned/edited/retired procedure changes the bytes
    #     and busts the static block (identity+safety+catalog share one breakpoint) on the
    #     next turn. Acceptable: procedure CRUD (sleep learning) is rare vs turns, and sleep
    #     fires at session-end/idle when the cache is already cold; it re-caches immediately.
    #   DEPTH — the full untruncated body loaded on demand via get_procedure(<name>) when
    #     the agent SELECTS one from the catalog.
    # Default ON (catalog-first is the intended delivery mode). With the catalog rendered,
    # the duplicating Track-B embedding slots are auto-suppressed and Critic picks become a
    # slim pointer (option C), so proc_passive_injection_enabled below can stay True (it is
    # the safety-net that re-enables Track B only if the catalog query fails).
    # **PROD DEPLOY GATE:** do NOT ship this ON to prod until skill-path dedup lands — the
    # catalog faithfully lists duplicate procedures (51/62 audit). Pin
    # NOUS_PROC_CATALOG_ENABLED=false in the prod env until then.
    proc_catalog_enabled: bool = True  # render the static breadth catalog (catalog-first)
    proc_catalog_max: int = Field(default=100, ge=1, le=500)  # safety cap on catalog row count
    proc_catalog_desc_chars: int = Field(default=120, ge=20, le=500)  # per-row desc truncation
    proc_catalog_max_chars: int = Field(default=4000, ge=500, le=40000)  # hard total-size cap (>= header + 1 row)
    proc_awareness_cue: bool = False  # cue-only fallback (instruction, no list) when catalog off
    # When False, the passive embedding-similarity (Track B) procedure slots are skipped —
    # those duplicate the recall_deep cosine path. Critic-recommended skills (Track A) are
    # NOT gated by this flag (no pull equivalent). Unified mode = this flag OFF +
    # proc_catalog_enabled ON (breadth via catalog, depth via get_procedure).
    proc_passive_injection_enabled: bool = True

    # F080 §14.7: procedure selection ladder (graph K-line -> critic -> cosine).
    # The every-turn "Recommended Procedures" section preloads the BODY of the
    # query-relevant procedure (no get_procedure round-trip). Validated 2026-06-08
    # on the prod snapshot: with the cosine leg it reaches 14/14 coverage at 1.64/2
    # judged relevance (graph alone was sparse at 6/14). Default ON; set false to
    # restore the passive name-pointer injection.
    proc_selection_graph_primary: bool = True
    # Per-item char cap for a preloaded procedure body in the Recommended section.
    # Sized to fit a real (full-fidelity) skill body; oversized skills are capped
    # with a pointer to get_procedure for the untruncated full body. The total is
    # still bounded by the per-frame procedure budget (build() accumulates).
    proc_recommended_body_max_chars: int = 2500
    # Graph K-line fan-out: procedure neighbors pulled per recalled seed.
    proc_graph_neighbors_per_seed: int = 3

    # F017: Diminishing returns cutoff (used by adaptive relevance filter)
    relevance_drop_ratio: float = 0.5

    # Adaptive relevance filter: per-type min/max result overrides (JSON dicts)
    # e.g. NOUS_RELEVANCE_MIN_RESULTS='{"fact": 2, "decision": 1}'
    relevance_min_results: dict[str, int] = Field(default_factory=dict)
    relevance_max_results: dict[str, int] = Field(default_factory=dict)

    # F017: Budget scaling
    budget_scale_enabled: bool = True

    # Context budget overrides — JSON dict applied on top of per-frame defaults
    # e.g. NOUS_CONTEXT_BUDGET_OVERRIDES='{"total": 12000, "decisions": 3000}'
    # Audit ST-1 (2026-06-09): NoDecode + the before-validator below tolerate an
    # empty/whitespace env value. docker-compose passes
    # `NOUS_CONTEXT_BUDGET_OVERRIDES=${...:-}` (empty string) on a fresh install
    # with no host .env; pydantic-settings' default complex-field decoder calls
    # json.loads("") and raises SettingsError, crash-looping the container at
    # boot. NoDecode hands the raw string to our validator instead so "" -> {}.
    context_budget_overrides: Annotated[dict[str, int], NoDecode] = Field(
        default_factory=dict
    )

    @field_validator("context_budget_overrides", mode="before")
    @classmethod
    def _parse_context_budget_overrides(cls, v: object) -> object:
        """Decode the JSON env string ourselves so an empty value is tolerated."""
        if v is None:
            return {}
        if isinstance(v, str):
            if not v.strip():
                return {}
            return json.loads(v)
        return v

    # F017: Staleness penalty
    staleness_penalty_enabled: bool = True
    staleness_half_life_days: int = 30

    # Sleep-cycle stale_scan phase: deactivate facts that are old AND
    # never recalled. Prior filter (active=true AND superseded_by IS NOT NULL
    # AND confidence < 0.5) was structurally impossible — the supersede flow
    # already deactivates at the same time, and prod confidence distribution
    # never goes below 0.7. New filter targets the actual stale-fact pattern.
    stale_scan_age_days: int = 60
    # Categories excluded from stale_scan. `rule` represents explicit user
    # directives that may be infrequently exercised but still in force —
    # deactivating them on recall stats alone is unsafe.
    stale_scan_excluded_categories: list[str] = Field(
        default_factory=lambda: ["rule"]
    )

    # Sleep-cycle cluster_consolidation phase (F027): merge near-duplicate
    # facts under the same subject into one. Prior code picked top-5 clusters
    # by size, but prod has accumulating subjects like `lesson_learned`
    # (164 facts) and `Tim` (36 facts) at the top — the LLM correctly
    # refuses to merge those, leaving the 11 actually-mergeable small
    # clusters (3-5 facts each) untouched.
    # Cap cluster size so accumulating subjects are skipped and small
    # mergeable clusters get a chance.
    cluster_consolidation_min_facts: int = 3
    cluster_consolidation_max_facts: int = 10

    # F057 (2026-05-04): episode re-linker — periodic backfill of F022
    # episode-graph edges for episodes the live linker missed. Investigation
    # of nous-default prod (2026-05-04) found 99/102 active episodes were
    # graph orphans (97% rate); 94 of those are stuck-open sessions that
    # never received `episode_ended` (so episode_summarizer + F022 linker
    # never fired). Of the 99 orphans, 27 have facts referencing them via
    # source_episode_id — those should have linked; the rest need semantic
    # episode↔episode (handled separately by F040).
    # The re-linker queries active orphan episodes older than min_age_hours,
    # fetches their fact/decision anchors, and calls
    # graph_linker.link_episode_deterministic. Episodes stay active=true
    # so they remain searchable; only the linker's outputs change.
    episode_relink_enabled: bool = True
    episode_relink_min_age_hours: int = 24  # skip recent — live linker handles
    episode_relink_max_per_cycle: int = 30

    # F060 (2026-05-05): abandoned-episode recovery — sleep-cycle phase that
    # finds active episodes with NULL structured_summary AND last activity >
    # min_age_hours, then invokes EpisodeSummarizer.summarize_episode to
    # populate the missing summary. Closes the gap that F058 was patching at
    # the densifier layer. Requires the F025 P3-C transcript column (rows
    # without a persisted transcript can't be summarized retroactively).
    # Episodes stay active=true (matching F057 — search filters on active=true).
    abandoned_recovery_enabled: bool = True
    abandoned_recovery_min_age_hours: int = 24
    abandoned_recovery_max_per_cycle: int = 50
    # Minimum transcript length to bother summarizing — short transcripts
    # produce low-quality summaries (per F051.5 summarize_episode skip).
    abandoned_recovery_min_transcript_chars: int = 50

    # F060.1 (2026-05-05): fallback to plain `summary` when `transcript` is
    # missing. Prod audit found 0/103 stuck-open episodes had transcripts
    # but 93/103 had a plain summary (~77 chars avg). The plain summary is
    # usually just the user's first message — degraded input vs a full
    # transcript, but produces a usable structured_summary (title, topics)
    # and is strictly better than leaving the row stuck forever.
    abandoned_recovery_summary_fallback_enabled: bool = True
    abandoned_recovery_min_summary_chars: int = 20

    # F060.2 (2026-05-05): mark truly unrecoverable episodes as abandoned.
    # An episode with NULL structured_summary AND no usable transcript AND
    # no usable plain summary is data-empty — no path can recover it.
    # Marking active=false + outcome='abandoned' removes them from search
    # and stops the recovery loop from re-querying them every cycle.
    # Separate age threshold (days, not hours) so we give recovery enough
    # cycles to attempt before giving up.
    abandoned_recovery_mark_abandoned_enabled: bool = True
    abandoned_recovery_mark_age_days: int = 7
    abandoned_recovery_mark_max_per_cycle: int = 200

    # F025 P2-C: Transcript truncation limit for episode summarization
    transcript_max_chars: int = 16000

    # F025 P2-D: Fact extractor dedup threshold (raised from 0.85)
    # Leg 1 (hybrid-search RRF pre-check) at fact_extractor.py:243-248
    fact_dedup_threshold: float = 0.92

    # F056 #377: Leg 2 (native cosine in Heart.learn) at facts.py:683-691.
    # Was hardcoded 0.95; now env-tunable so the dedup eval (and operators)
    # can sweep both legs. F056 PR #2 smoke showed 0.95 misses ALL paraphrases —
    # text-embedding-3-small cosine on semantic paraphrases sits ~0.85-0.93.
    # Default kept 0.95 for backwards-compat.
    fact_native_cosine_threshold: float = 0.95

    # F377: Leg-1 dedup tiebreaker. When a fact's hybrid-search (RRF) pre-check
    # flags it as a duplicate, a same-vs-distinct Haiku classifier confirms the
    # verdict before skipping the write. Fixes RRF over-dedup on high-lexical-
    # overlap semantic opposites ("MRR -5%" vs "MRR +5%") that threshold tuning
    # cannot separate (sweep 0.92/0.95/0.97 = byte-identical). Fires only on the
    # RRF-dup path, fails open (None/error -> dedup as before). Reuses
    # contradiction_model. Default OFF (land dark; flip after the dedup eval).
    fact_dedup_tiebreaker_enabled: bool = False

    # Runtime
    host: str = "0.0.0.0"
    port: int = 8000
    anthropic_api_key: str = Field("", validation_alias="ANTHROPIC_API_KEY")
    # Dual auth: auth_token (Bearer) takes precedence over api_key (x-api-key)
    anthropic_auth_token: str = Field("", validation_alias="ANTHROPIC_AUTH_TOKEN")

    # Agent identity
    agent_name: str = "Nous"
    agent_description: str = "A thinking agent that learns from experience"
    identity_prompt: str = (
        "You are Nous, a cognitive AI agent that learns from experience. "
        "You record decisions with reasoning (record_decision), extract and store facts (learn_fact), "
        "search all memory types (recall_deep), and create guardrails (create_censor). "
        "Be concise, honest, and thoughtful. When you make a choice, record it. "
        "When you learn something new, store it as a fact."
    )

    # Event Bus
    event_bus_enabled: bool = True
    episode_summary_enabled: bool = True
    fact_extraction_enabled: bool = True
    sleep_enabled: bool = True
    decision_review_enabled: bool = True
    decision_sweep_interval: int = Field(default=3600, description="Seconds between periodic decision review sweeps (default: 1 hour)")
    temporal_context_enabled: bool = True  # 008.6
    github_token: str = Field(
        default="",
        validation_alias="GITHUB_TOKEN",
    )
    background_model: str = Field(
        default="claude-sonnet-4-6",
        validation_alias="NOUS_BACKGROUND_MODEL",
    )
    session_idle_timeout: int = Field(
        default=1800,
        validation_alias="NOUS_SESSION_TIMEOUT",
    )
    sleep_timeout: int = Field(
        default=7200,
        validation_alias="NOUS_SLEEP_TIMEOUT",
    )
    sleep_check_interval: int = Field(
        default=60,
        validation_alias="NOUS_SLEEP_CHECK_INTERVAL",
    )

    # MCP
    mcp_enabled: bool = True

    # LLM
    model: str = "claude-sonnet-4-6"
    max_tokens: int = 4096

    # Extended thinking
    thinking_mode: Literal["off", "adaptive", "manual"] = "off"
    thinking_budget: int = 10000  # budget_tokens for manual mode (min 1024)
    effort: Literal["low", "medium", "high", "max"] = "high"

    # Context window override (0 = auto-detect from model name)
    context_window: int = Field(
        default=0, validation_alias="NOUS_CONTEXT_WINDOW"
    )

    # API backend: "sdk" (official anthropic SDK) or "httpx" (direct httpx calls)
    api_backend: str = "sdk"

    # Direct API settings
    max_turns: int = 10  # Max tool use iterations per turn
    api_base_url: str = "https://api.anthropic.com"
    api_timeout_connect: int = 10  # seconds
    api_timeout_read: int = 120  # seconds

    # F048: Background streaming aggregation (Mechanism B)
    api_background_streaming_enabled: bool = True
    api_background_timeout_read: int = 600  # seconds — read timeout for background streaming aggregator

    # F048: TCP keep-alive on httpx transports (Mechanism A)
    api_socket_keepalive_enabled: bool = True
    api_socket_keepalive_idle: int = 30  # seconds before first keep-alive probe
    api_socket_keepalive_interval: int = 10  # seconds between probes
    api_socket_keepalive_count: int = 3  # failed probes before RST

    workspace_dir: str = "/tmp/nous-workspace"

    # Web tools
    brave_search_api_key: str = Field("", validation_alias="BRAVE_SEARCH_API_KEY")
    web_search_daily_limit: int = 100  # Max web searches per day
    # F069 (2026-05-26): bumped from 10000 -> 50000 so an arxiv paper /
    # long doc page is not silently truncated to 1-2 sections. Hard ceiling
    # in _web_fetch raised to 200000 in lockstep so callers can pass an
    # explicit max_chars=200000 when they intend to ingest a full document
    # (e.g. before calling ingest_document). Pure dialogue web_fetch calls
    # are still soft-trimmed downstream by tool_soft_trim_chars.
    web_fetch_max_chars: int = 50000
    tavily_api_key: str = Field("", validation_alias="TAVILY_API_KEY")
    exa_api_key: str = Field("", validation_alias="EXA_API_KEY")
    search_provider: str = "auto"  # auto, tavily, exa, brave

    # Tool execution
    tool_timeout: int = Field(
        default=120, validation_alias="NOUS_TOOL_TIMEOUT"
    )  # Max seconds for any single tool execution
    keepalive_interval: int = Field(
        default=10, validation_alias="NOUS_KEEPALIVE_INTERVAL"
    )  # Seconds between keepalive events during tool execution
    sse_ping_interval: int = Field(
        default=15, validation_alias="NOUS_SSE_PING_INTERVAL"
    )  # Seconds between SSE comment-line pings on /chat/stream — keeps
    # the socket alive during stalls in pre_turn, compaction, or any
    # other non-streaming phase of stream_chat. Comment lines (`:`) are
    # ignored by spec-compliant SSE clients but reset their read timer.

    # SmartCompress (F020 Phase 1)
    smart_compress_enabled: bool = Field(
        default=True, description="Enable ingestion-time tool output compression"
    )
    smart_compress_min_chars: int = Field(
        default=500, description="Below this, never compress"
    )
    smart_compress_max_k: int = Field(
        default=50, description="Max items to keep per compressed result"
    )
    smart_compress_elbow_threshold: float = Field(
        default=0.3, description="Score cliff threshold for adaptive K"
    )

    # F036: Prompt Cache Optimization
    cache_break_detection_enabled: bool = Field(
        default=True, validation_alias="NOUS_CACHE_BREAK_DETECTION_ENABLED"
    )
    cache_split_system_prompt: bool = Field(
        default=True, validation_alias="NOUS_CACHE_SPLIT_SYSTEM_PROMPT"
    )
    cache_single_breakpoint: bool = Field(
        default=True, validation_alias="NOUS_CACHE_SINGLE_BREAKPOINT"
    )
    tool_schema_cache_enabled: bool = Field(
        default=True, validation_alias="NOUS_TOOL_SCHEMA_CACHE_ENABLED"
    )

    # Compaction: Layer 1 (Tool Pruning)
    tool_pruning_enabled: bool = Field(
        default=True, validation_alias="NOUS_TOOL_PRUNING_ENABLED"
    )
    tool_soft_trim_chars: int = Field(
        default=4000, validation_alias="NOUS_TOOL_SOFT_TRIM_CHARS"
    )
    tool_soft_trim_head: int = Field(
        default=1500, validation_alias="NOUS_TOOL_SOFT_TRIM_HEAD"
    )
    tool_soft_trim_tail: int = Field(
        default=1500, validation_alias="NOUS_TOOL_SOFT_TRIM_TAIL"
    )
    tool_hard_clear_after: int = Field(
        default=12, validation_alias="NOUS_TOOL_HARD_CLEAR_AFTER"
    )
    keep_last_tool_results: int = Field(
        default=2, validation_alias="NOUS_KEEP_LAST_TOOL_RESULTS"
    )
    tool_metadata_degrade_after: int = Field(
        default=8, validation_alias="NOUS_TOOL_METADATA_DEGRADE_AFTER"
    )

    # Compaction: Layer 2 (History Compaction) — Phase 2
    compaction_enabled: bool = Field(
        default=True, validation_alias="NOUS_COMPACTION_ENABLED"
    )
    compaction_threshold: int = Field(
        default=100_000, validation_alias="NOUS_COMPACTION_THRESHOLD"
    )
    keep_recent_tokens: int = Field(
        default=20_000, validation_alias="NOUS_KEEP_RECENT_TOKENS"
    )

    # 011.1: Subtasks & Scheduling
    subtask_enabled: bool = True
    subtask_workers: int = 2
    subtask_poll_interval: float = 2.0
    subtask_default_timeout: int = 600  # F048: bumped from 120 so outer wait_for doesn't cancel inner streaming
    subtask_max_timeout: int = 3600  # F048: bumped from 900 to support long-running background streaming
    subtask_max_concurrent: int = 3
    subtask_cleanup_timeout_seconds: int = Field(
        default=30,
        validation_alias="NOUS_SUBTASK_CLEANUP_TIMEOUT_SECONDS",
        description="F049: max seconds to wait for end_conversation in subtask finally before logging ERROR",
    )

    # F061: Subtask Hardening — forced terminal-tool contract + structural validator + bounded retry.
    # All fields use plain names (no validation_alias); env_prefix="NOUS_" picks up NOUS_SUBTASK_*.
    subtask_hardening_enabled: bool = False
    subtask_max_attempts: int = Field(default=2, ge=1, le=3)
    subtask_report_min_summary_chars: int = Field(default=50, ge=1)
    # bootstrap/work timeouts are observability-only labels until PR-2 wires them
    # into _execute_hardened. The outer asyncio.wait_for(timeout_seconds) in the
    # worker is unchanged. Setting these in PR-1 has no runtime effect.
    subtask_bootstrap_timeout: int = Field(default=30, ge=1)
    subtask_work_timeout: int = Field(default=570, ge=1)
    subtask_outcome_persistence_enabled: bool = True
    subtask_force_tool_on_penultimate: bool = True

    # F062: typed spawn_sync — flag gates both the exposed `payload_schema`
    # property on _SPAWN_TASK_SCHEMA / submit_final_report AND the post-execution
    # jsonschema.validate step inside execute_hardened. When false, F062 is
    # entirely dormant — spawn_sync is not registered as a tool.
    subtask_payload_schema_enabled: bool = False

    # F049: WM TTL safety-net sweep
    working_memory_ttl_hours: int = Field(
        default=24,
        validation_alias="NOUS_WORKING_MEMORY_TTL_HOURS",
        description="F049: delete heart.working_memory rows older than this (0 disables)",
    )
    working_memory_sweep_interval_seconds: int = Field(
        default=3600,
        validation_alias="NOUS_WORKING_MEMORY_SWEEP_INTERVAL_SECONDS",
        description="F049: minimum seconds between WM TTL safety-net sweeps",
    )
    working_memory_sweep_batch_size: int = Field(
        default=5000,
        validation_alias="NOUS_WORKING_MEMORY_SWEEP_BATCH_SIZE",
        description="F049: rows per DELETE batch during WM sweep",
    )

    # F046: DAG node timeouts
    dag_node_default_timeout: int = Field(
        600,
        ge=1,
        description="Default timeout (seconds) for DAG nodes when node spec omits timeout_seconds",
    )
    dag_node_max_timeout: int = Field(
        7200,
        ge=1,
        description="Hard ceiling (seconds) for DAG node timeout_seconds",
    )
    schedule_enabled: bool = True
    schedule_check_interval: int = 60
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None

    # 012.3: Programmatic tool calling
    programmatic_tools_enabled: bool = True
    programmatic_tools_timeout: int = 10

    # 012.2: Subtask execution guardrails (configurable constants)
    subtask_tool_call_limit: int = 20
    inline_subtask_timeout: int = 90  # seconds
    frame_default_models: dict[str, str] = Field(
        default_factory=dict,
    )

    # F022: Graph-Augmented Recall
    graph_recall_enabled: bool = True
    graph_recall_max_expand: int = 5
    graph_recall_decay: float = 0.7
    graph_recall_max_neighbors: int = 3

    # F065: Edge-provenance penalty multiplier for `inferred`-tier edges
    # in recall_deep graph expansion. Defaults to 1.0 (dark launch — no
    # behavioral change). Flip to 0.7 after the F051 harness quantifies
    # the MRR impact.
    graph_inferred_edge_penalty: float = Field(
        default=1.0,
        ge=0.0,
        le=1.0,
        description="F065: down-weight multiplier for 'inferred' provenance edges in recall_deep (1.0 = disabled).",
    )

    # F065: Hub autosurface — when True, pre_turn detects rank shifts
    # in the top-10 hub list and injects a hub-shift notice into the
    # working-memory context. Disabled by default for one release of
    # bake-time on the snapshot table.
    graph_hub_autosurface_enabled: bool = False

    # F065: Hub-snapshot retention (sleep handler prunes older rows).
    # Set to 0 to disable the prune phase entirely (rows accumulate).
    graph_hub_snapshot_retention_days: int = Field(
        default=90,
        ge=0,
        description="F065: days to retain brain.graph_hub_snapshots rows. Sleep handler prunes older rows. 0 disables the prune phase.",
    )

    # F065: top-N hubs the autosurface tracks.
    graph_hub_autosurface_top_n: int = Field(
        default=10,
        ge=1,
        le=50,
        description="F065: size of the top-N hub list used for rank-shift detection.",
    )

    # F066.1 Phase 1.5: LLM-based fix-node dispatch. When False (default),
    # the orchestrator uses fix_executor.choose_action (rule-based, Phase 1
    # behavior). When True AND an LLM client is wired into DAGOrchestrator,
    # the orchestrator calls fix_executor.choose_action_llm; any failure
    # (timeout, parse error, unsupported action) falls back to the
    # rule-based path.
    dag_fix_llm_dispatch_enabled: bool = False
    dag_fix_llm_model: str = Field(
        default="claude-haiku-4-5-20251001",
        description="F066.1 Phase 1.5: model for fix-node LLM dispatch. Default Haiku — the action set is small (4 enum values) and the prompt is short.",
    )
    dag_fix_llm_timeout_seconds: float = Field(
        default=10.0,
        gt=0.0,
        le=60.0,
        description="F066.1 Phase 1.5: per-call timeout for the fix-node LLM dispatcher. Bounded so a slow LLM never hangs the orchestrator tick.",
    )

    # F022 Phase 2: Cross-type linking
    cross_type_linking_enabled: bool = True
    cross_type_threshold: float = 0.80
    cross_type_same_threshold: float = 0.90

    # F022 Phase 3: Contradiction detection
    contradiction_detection: bool = True
    contradiction_similarity_threshold: float = 0.85
    contradiction_model: str = "claude-haiku-4-5-20251001"

    # F022 Phase 4: Spreading activation
    spreading_activation_enabled: str = "auto"  # "auto", "true", "false"
    spreading_activation_density_threshold: float = 3.0
    spreading_activation_decay: float = 0.5
    spreading_activation_max_depth: int = 2
    spreading_activation_alpha: float = 0.5
    spreading_activation_beta: float = 0.3
    spreading_activation_gamma: float = 0.2

    # F040: Graph densification — backfill
    graph_backfill_enabled: bool = True
    graph_backfill_max_facts: int = 50
    graph_backfill_max_decisions: int = 30
    graph_backfill_max_episodes: int = 30
    graph_backfill_max_procedures: int = 20

    # F040: Per-relation thresholds
    graph_threshold_fact_fact: float = 0.82
    graph_threshold_fact_decision: float = 0.72
    graph_threshold_fact_episode: float = 0.70
    graph_threshold_decision_decision: float = 0.78
    graph_threshold_episode_episode: float = 0.75
    graph_threshold_procedure_any: float = 0.70

    # F040: Graph health monitoring
    graph_health_orphan_warn_threshold: float = 0.40
    graph_health_check_enabled: bool = True

    # F043: CE reranking for sleep-cycle graph backfill (reuses F042 reranker)
    ce_backfill_enabled: bool = False
    ce_backfill_top_k: int = 10
    ce_backfill_min_score: float = 0.30

    # F045: CE-aware relaxed thresholds. Only apply when ce_backfill_enabled=True;
    # when CE is off, _get_threshold() falls back to the strict graph_threshold_*
    # defaults above. fact_fact originally 0.65 (2026-04-14 A/B at 80% precision).
    # F054 (2026-04-26 F053 density-eval): same-type relations were over-filtered;
    # relaxing fact_fact/decision_decision/episode_episode/procedure_any to
    # 0.55/0.50/0.50/0.45 produced +71% same-type edges at unchanged precision
    # (related_to 0.83 → 0.83 on 30-edge sample). Cross-type fact_decision and
    # fact_episode KEPT STRICT at 0.55 because precision regressed (0.57 → 0.47)
    # when loosened — corpus-quality issue (empty brain.decisions.context),
    # addressed via the new ce_backfill_min_decision_chars guard below.
    ce_backfill_threshold_fact_fact: float = 0.55  # F054: 0.65 -> 0.55
    ce_backfill_threshold_fact_decision: float = 0.55  # KEEP STRICT (F054)
    ce_backfill_threshold_fact_episode: float = 0.55  # KEEP STRICT (F054)
    ce_backfill_threshold_decision_decision: float = 0.50  # F054: 0.60 -> 0.50
    ce_backfill_threshold_episode_episode: float = 0.50  # F054: 0.58 -> 0.50
    ce_backfill_threshold_procedure_any: float = 0.45  # F054: 0.55 -> 0.45

    # F045: content-length guard for CE backfill. Candidates whose content is
    # shorter than this (after strip) are dropped before CE inference — filters
    # URL-only / boilerplate facts that would otherwise co-score highly on
    # shared token shape with no semantic signal.
    ce_backfill_min_content_chars: int = 80

    # F054: content-length guard specifically for brain.decisions.context.
    # 2026-04-26 F053 edge_judge audit found ~5/9 NO/WEAK verdicts on
    # evidence_for edges traced to "source content is empty" (decisions
    # routinely have null/whitespace-only context in the eval corpus).
    # Symmetric to ce_backfill_min_content_chars but applies only when the
    # candidate's entity_type == 'decision'. Default 40 chars (lower than
    # facts because decisions are naturally shorter).
    ce_backfill_min_decision_chars: int = 40

    # F012: Procedure Learning (K-Line auto-creation)
    procedure_learning_enabled: bool = True
    procedure_cluster_min_size: int = 3
    procedure_similarity_threshold: float = 0.85
    procedure_episode_similarity: float = 0.80
    procedure_success_rate_min: float = 0.70
    procedure_monitor_trigger_count: int = 3
    procedure_max_per_sleep: int = 3
    procedure_max_per_session: int = 1
    procedure_staleness_days: int = 30
    procedure_weakness_threshold: float = 0.30
    # Recency gate for cluster eligibility — at least one cluster member
    # must be created within this window. Was hardcoded 7 days; sleep
    # cycle health monitor (PR #404) caught that 7 days yielded 3 of 200
    # eligible candidates, so most clusters never satisfied the gate.
    # 30 days gives ~71 of 200, making the gate meaningful instead of
    # blocking.
    procedure_recency_days: int = 30

    # F037: Utility-Boosted Procedure Retrieval
    procedure_utility_boost: bool = True
    procedure_utility_alpha: float = 0.15
    procedure_affinity_beta: float = 0.10
    procedure_min_activations_for_boost: int = 5

    # F023: Memory Admission Control (A-MAC)
    admission_control_enabled: bool = True
    admission_shadow_mode: bool = True
    admission_threshold: float = 0.55
    admission_w_utility: float = 0.25
    admission_w_confidence: float = 0.15
    admission_w_novelty: float = 0.20
    admission_w_recency: float = 0.10
    admission_w_type_prior: float = 0.30
    admission_recency_lambda: float = 0.01
    admission_utility_model: str = ""
    admission_utility_llm_enabled: bool = True

    # F026: Execution Integrity
    execution_ledger_enabled: bool = True
    execution_ledger_max_tokens: int = 500

    claim_verification_enabled: bool = True
    claim_verification_mode: Literal["shadow", "warn", "enforce"] = "enforce"

    action_gating_enabled: bool = True
    action_gating_mode: Literal["shadow", "warn", "enforce"] = "enforce"
    action_gating_model: str = "claude-haiku-4-5-20251001"
    action_gating_external_only: bool = False  # True = skip Tier 2, only gate external/irreversible
    action_gating_turn_window: int = 5  # Only block duplicates within this many turns

    # F026.1: Change-aware duplicate detection
    action_gating_change_aware: bool = True  # Layer 1: check for intervening state changes
    action_gating_repeat_threshold: int = 3  # Layer 2: identical calls allowed before warning
    action_gating_hard_block_threshold: int = 5  # Layer 2/4: identical calls before hard block
    action_gating_iterative_multiplier: float = 2.0  # Layer 3: threshold multiplier for iterative commands

    # F030: MMR Diversity Re-Ranking
    mmr_enabled: bool = False
    mmr_diversity_weight: float = Field(
        default=0.7, ge=0.0, le=1.0,
        description="MMR relevance vs diversity weight (1.0=pure relevance, 0.0=pure diversity)",
    )
    # F030.1: Skip MMR when cross-encoder rerank just reordered the head.
    # F051 retrieval-eval harness measured +30% MRR (0.372 -> 0.484, +190% on
    # jargon-drift) when MMR is gated off after CE fires — MMR's diversity
    # selection over CE's reordered top-20 neutralizes CE's relevance signal.
    # Default True; set False to restore pre-F030.1 behavior (chain CE then MMR).
    mmr_skip_after_ce: bool = True

    # F080: coherent cross-type ranking. When true, recall_deep excludes
    # censors and procedures from the ranked recall pool — they are capabilities
    # / guardrails with their own surfaces (Active Censors; Procedure Catalog +
    # get_procedure), not knowledge. The remaining facts/episodes/decisions/chunks
    # already share the normalized RRF [0,1] space, so no calibration is needed.
    # Validated 2026-06-08 on a fresh post-dedup prod snapshot: 0/12 censor+procedure
    # leak; excluding them shifts top-10 from ~40 procedure slots to knowledge.
    # Default ON; set false to restore the legacy ranked pool.
    coherent_ranking_enabled: bool = True

    # F042: Cross-encoder reranking
    cross_encoder_enabled: bool = False
    # BGE reranker-v2-m3 empirically beats MiniLM by +18.4pp chunks_off /
    # +3.3pp baseline hit@5 on per_haystack K=5 (LME N=60, eval runs
    # 2026-05-25T20-14-07 vs 22-34-46). Previously set via env var in
    # ad-hoc shell sessions; now persisted as the default so the win
    # doesn't silently regress when no override is supplied.
    cross_encoder_model: str = "BAAI/bge-reranker-v2-m3"
    cross_encoder_max_candidates: int = 30
    cross_encoder_text_limit: int = 512

    # F024: Critic Agent
    critic_enabled: bool = True
    critic_mode: Literal["shadow", "advised", "parallel"] = "shadow"
    critic_model: str = "claude-sonnet-4-6"
    critic_max_latency_ms: int = 5000
    critic_passthrough_max_words: int = 5
    critic_skill_injection: Literal["enabled", "disabled", "log_only"] = "disabled"
    critic_skill_slots: int = Field(default=2, ge=0)  # Reserved slots for Critic-recommended skills
    embedding_skill_slots: int = Field(default=3, ge=0)  # Slots for embedding similarity search

    # Email / integration credentials (used by heartbeat EmailCheck, other integrations)
    email: str = ""  # Nous agent email address
    email_user: str = ""  # IMAP login user
    email_password: str = ""  # IMAP login password
    # F078.1: Guarded send_email tool (recipient allowlist + secret scan + rate limit)
    email_allowlist: str = ""  # CSV of allowed recipients (case-insensitive exact). Empty = reject all.
    # F078.1.1: hot-reloadable allowlist file (one address per line, or CSV; '#' comments OK).
    # Read at send-time (mtime-cached), so adding an address takes effect WITHOUT a restart.
    # Effective allowlist = email_allowlist (env, static base) UNION this file's contents.
    email_allowlist_file: str = ""
    email_tool_enabled: bool = True  # Master switch for the guarded send_email tool
    email_max_per_hour: int = 5  # In-process sliding-window rate limit
    email_smtp_host: str = "smtp.gmail.com"  # SMTP host for the send_email tool
    email_smtp_port: int = 587  # SMTP STARTTLS port
    email_max_attachment_mb: int = 25  # F078.1.2: total attachment size cap for send_email (Gmail ~25MB)
    tim_chat_id: str = ""  # Tim's Telegram chat ID
    emerson_hook_url: str = ""  # Emerson presence hook URL
    emerson_hook_token: str = ""  # Emerson presence hook token
    google_service_account_json: str = Field("", validation_alias="GOOGLE_SERVICE_ACCOUNT_JSON")

    # F034: Heartbeat
    heartbeat_enabled: bool = True
    heartbeat_tick_interval: int = 30
    heartbeat_quiet_start: int = 23
    heartbeat_quiet_end: int = 8
    heartbeat_daily_token_budget: int = 50_000
    heartbeat_email_enabled: bool = False  # disabled by default — needs IMAP creds
    heartbeat_email_interval: int = 180
    heartbeat_email_imap_host: str = "imap.gmail.com"
    heartbeat_drive_enabled: bool = True
    heartbeat_drive_interval: int = 600  # every 10 minutes
    heartbeat_health_interval: int = 3600
    heartbeat_self_initiated_interval: int = 1800

    # F034.1: Finding lifecycle
    heartbeat_escalation_low_to_normal_hours: int = 72
    heartbeat_escalation_normal_to_high_hours: int = 24
    heartbeat_escalation_high_realert_hours: int = 12
    heartbeat_escalation_accumulation_threshold: int = 5
    heartbeat_digest_hour_utc: int = 9
    heartbeat_suppression_ttl_hours: int = 24

    # F034.3: Self-tuning
    heartbeat_tuning_enabled: bool = False  # off by default until stable
    heartbeat_tuning_interval_hours: int = 168  # weekly
    heartbeat_tuning_min_samples: int = 10
    heartbeat_tuning_learning_rate: float = 0.1
    heartbeat_tuning_rollback_threshold: float = 0.2

    # F034.5: Dynamic heartbeat checks
    heartbeat_model: str = ""  # Empty = use background_model fallback
    heartbeat_max_dynamic_checks: int = 10
    heartbeat_default_check_timeout: int = 30  # default max seconds per check run
    heartbeat_dynamic_sync_ticks: int = 60  # re-sync every N ticks (~30 min at 30s tick)

    # F035.3: Behavioral drift detection
    drift_detection_enabled: bool = True
    drift_detection_interval: int = 3600

    # F035.4: Context visibility
    context_log_enabled: bool = True
    context_log_full_payload: bool = False
    context_log_ring_size: int = 10
    context_log_max_total: int = 50
    context_log_retention_days: int = 30

    # F038: DAG Orchestration
    dag_enabled: bool = True

    # F064.1: DAG node stall detection. Off by default — opt-in feature flag
    # for the stall-scan + activity-ping plumbing. When false, the orchestrator
    # never reads `last_activity_at` and never marks nodes failed for stall.
    dag_stall_detection_enabled: bool = False
    dag_node_default_stall_timeout: int = Field(
        600,
        ge=0,
        description="Seconds without activity before a running node is marked stalled. 0 disables per-node.",
    )
    dag_node_max_stall_timeout: int = Field(
        3600,
        ge=1,
        description="Hard ceiling for per-DAGNodeSpec.stall_timeout_seconds.",
    )

    # F064.2: per-frame-type concurrency caps on DAG dispatch. Off by default.
    # When false, the orchestrator dispatches all ready nodes in a tick as it
    # always has. When true, `DAGCreateRequest.max_concurrent_by_frame_type`
    # is consulted, with the global env override below taking precedence.
    dag_frame_concurrency_enabled: bool = False
    dag_global_max_concurrent_by_frame: dict[str, int] = Field(
        default_factory=dict,
        description="Operator-level cap dict {frame_type: max_concurrent}. Overrides per-DAG values when set.",
    )

    # F064.3: workspace safety invariants. Off by default. When false,
    # insert-time sanitization is skipped (today's behavior). Read-time
    # containment-assert runs unconditionally as a security boundary — see
    # docs/plans/2026-05-19-f064-symphony-orchestration-adoptions.md §6.
    dag_workspace_safety_enabled: bool = False
    dag_workspace_root: Path = Field(
        default_factory=lambda: Path(tempfile.gettempdir()) / "nous-workspace" / "dag-status",
        description="Resolved-absolute root that every DAG workspace path must be inside. Platform-dependent default via tempfile.gettempdir().",
    )

    # F064.4: workflow-as-code skill manifest fields. The new SkillManifest
    # fields (concurrency_cap, timeout_override_seconds, hooks,
    # requires_human_review) are *always* parsed and *always* persisted on
    # procedures.runtime_metadata — this flag gates only the deferred-to-v2
    # consumer enforcement at the orchestrator level. Off by default until
    # F064.4-v2 ships the consumer.
    skill_runtime_metadata_enabled: bool = False

    # F064.5: scheduled task continuation (v1 Episode reuse only). Off by
    # default — every fire creates a fresh session_id, today's behavior. When
    # true, schedules with continuation_turns > 0 reuse the prior fire's
    # session_id up to the cap.
    schedule_continuation_enabled: bool = False
    schedule_max_continuation_turns: int = Field(
        50,
        ge=1,
        description="Hard ceiling on Schedule.continuation_turns. Prevents unbounded Episode growth.",
    )
    schedule_continuation_default_prompt: str = Field(
        "Continue. The previous run completed at {last_fired_at}. Apply the same task to fresh context.",
        description="Reserved for F064.5-v2 (LLM thread continuity). Not consumed in v1.",
    )

    # F064.6: work-queue ingress heartbeat check. Off by default. When true,
    # a WorkQueueCheck is registered that polls the configured adapter and
    # emits DAGs for new items, reconciling terminal-state items by cancel-
    # cascading the corresponding DAG.
    work_queue_enabled: bool = False
    work_queue_source: Literal["file_jsonl", "github_issues", "linear"] = "file_jsonl"
    work_queue_interval_seconds: int = Field(
        300,
        ge=30,
        description="Seconds between WorkQueueCheck runs. Sub-30s would hammer the queue/DB.",
    )
    work_queue_file_jsonl_path: str = Field(
        "",
        description="Adapter-specific config: path to JSONL file for file_jsonl source.",
    )
    work_queue_max_dags_per_tick: int = Field(
        5,
        ge=1,
        le=5,
        description="Per-tick admission cap to avoid flooding MAX_ACTIVE_DAGS (=5).",
    )

    @field_validator("dag_global_max_concurrent_by_frame")
    @classmethod
    def _validate_frame_caps(cls, v: dict[str, int]) -> dict[str, int]:
        """F064.2: every per-frame cap must be a positive integer.

        Setting a value to 0 would silently block all DAGs of that frame
        type — exactly the silent-failure shape the reviewer P1 callout
        warned about. Fail fast at Settings init instead.
        """
        for frame, cap in v.items():
            if cap < 1:
                raise ValueError(
                    f"NOUS_DAG_GLOBAL_MAX_CONCURRENT_BY_FRAME['{frame}']={cap} is invalid; "
                    "values must be >= 1"
                )
        return v

    # F039: Correction Learning Pipeline
    correction_extraction_enabled: bool = True

    # F024 Phase 3b: Self-Modifying Rubrics
    rubric_enabled: bool = True
    rubric_outcome_detection_enabled: bool = True
    rubric_evolution_enabled: bool = False  # Phase 1+ — start disabled
    rubric_min_episodes_for_correlation: int = 50
    rubric_weight_change_cap: float = 0.05
    rubric_min_dimensions: int = 3
    rubric_max_dimensions: int = 7
    rubric_max_versions_per_week: int = 1
    rubric_outcome_model: str = "claude-haiku-4-5-20251001"

    # F047: Actionability classification
    actionability_enabled: bool = True
    actionability_llm_enabled: bool = True
    actionability_model: str = "claude-haiku-4-5-20251001"
    actionability_default: bool = False  # Fail-closed on uncertain facts
    actionability_backfill_on_startup: bool = True
    actionability_backfill_token_budget: int = 10_000  # rough Haiku daily cap

    # F055 — Cross-Turn Residual Activation (spec docs/features/F055-...md)
    # Default OFF until eval gate via F051.4 multi_turn_eval validates.
    residual_activation_enabled: bool = Field(
        default=False,
        description="F055 master switch — enable session-scoped residual activation.",
    )
    residual_decay_mode: Literal["geometric", "power_law"] = Field(
        default="geometric",
        description="F055 — decay function: geometric (decay^t) or power_law (ACT-R style).",
    )
    residual_decay_per_turn: float = Field(
        default=0.5, ge=0.0, le=1.0,
        description="F055 — geometric decay base; activation drops by this factor per turn.",
    )
    residual_power_law_alpha: float = Field(
        default=0.5, ge=0.0, le=2.0,
        description="F055 — power-law decay exponent (ACT-R default 0.5).",
    )
    residual_activation_floor: float = Field(
        default=0.05, ge=0.0, le=1.0,
        description="F055 — drop activations below this floor (prunes long tail).",
    )
    residual_top_k_carried: int = Field(
        default=20, ge=1,
        description="F055 — max activations carried forward per session.",
    )
    residual_top_n_seeds: int = Field(
        default=5, ge=0,
        description="F055 — max residually-activated nodes added to F022 spreading seeds.",
    )
    residual_seed_weight: float = Field(
        default=0.3, ge=0.0, le=1.0,
        description="F055 — multiplier on activation when injecting into F022 seeds.",
    )
    residual_boost_weight: float = Field(
        default=0.15, ge=0.0, le=1.0,
        description="F055 — additive boost on RRF score (applied before F042 CE rerank).",
    )

    # F050: Multi-query expansion via Haiku (spec §Config)
    query_expansion_enabled: bool = Field(
        default=False,
        description="F050 master switch — expand recall queries via Haiku before hybrid_search.",
    )
    query_expansion_model: str = Field(
        default="claude-haiku-4-5-20251001",
        description="F050 — Haiku model used for query expansion.",
    )
    query_expansion_timeout_seconds: float = Field(
        default=2.0,
        description="F050 — per-call Haiku timeout. Blown timeout falls through to [query].",
    )
    query_expansion_max_variants: int = Field(
        default=3,
        description="F050 — total variants returned including the original.",
    )
    query_expansion_min_words: int = Field(
        default=3,
        description="F050 — gate threshold; queries with fewer words skip expansion.",
    )
    query_expansion_max_per_hour: int = Field(
        default=500,
        description="F050 — sliding-window budget cap on Haiku calls. Breach => fail open + WARN.",
    )
    query_expansion_cache_ttl_days: int = Field(
        default=30,
        description="F050 — cache row retention; sweep handler ships in F050.2.",
    )

    # §2: Haiku-layered three-way epistemic gate (grounded / world_knowledge /
    # abstain). All default OFF (dark-launch); fail-open to softened prose.
    epistemic_gate_enabled: bool = Field(
        default=False,
        description=(
            "§2 master switch — Haiku three-way epistemic routing "
            "(grounded / world_knowledge / abstain). When true, an "
            "EpistemicClassifier tags each turn and ContextEngine injects an "
            "Epistemic Routing instruction sibling to the anti-hallucination "
            "block. Fail-open: timeout/error/budget => softened abstain prose "
            "that PERMITS base-model knowledge. Default OFF."
        ),
    )
    epistemic_gate_model: str = Field(
        default="claude-haiku-4-5-20251001",
        description="§2 — Haiku model id for epistemic classification.",
    )
    epistemic_gate_timeout_seconds: float = Field(
        default=2.0,
        description=(
            "§2 — per-call Haiku timeout. Blown timeout fails open to "
            "softened prose."
        ),
    )
    epistemic_gate_max_per_hour: int = Field(
        default=500,
        description=(
            "§2 — in-process sliding-window budget cap on Haiku calls. "
            "Breach => fail open + WARN-once."
        ),
    )

    # §1: event_date-only recency conflict resolver. Default OFF; inert until
    # NOUS_TEMPORAL_EXTRACTION_ENABLED populates event_date.
    recency_resolver_enabled: bool = Field(
        default=False,
        description=(
            "§1: event_date-only recency conflict resolver. After retrieval, "
            "same-subject facts that conflict on a value AND both carry a "
            "non-null, DIFFERING event_date are resolved: newer => "
            "[current YYYY-MM], older => [superseded YYYY-MM] + down-ranked "
            "*0.3 (never deleted). Inert until NOUS_TEMPORAL_EXTRACTION_ENABLED "
            "populates event_date. Default OFF."
        ),
    )
    recency_resolver_similarity_floor: float = Field(
        default=0.55, ge=0.0, le=1.0,
        description=(
            "§1: difflib SequenceMatcher ratio above which two same-subject "
            "facts are treated as the SAME attribute restated/changed (so a "
            "differing event_date = supersession). Below this => different "
            "attributes => no trigger. Tuned to avoid 'Alice's role' vs "
            "'Alice's city' false conflicts."
        ),
    )

    # F067: Episode chunks (raw transcript chunks alongside lossy fact extraction)
    # and parent episode injection in recall_deep. Both default OFF — opt-in.
    # Validated on LongMemEval per-question isolation methodology (+13pp chunks,
    # +6pp parent episodes). NOT validated on shared-corpus retrieval where
    # cross-topic noise can regress performance. See
    # memory/project_lme_methodology_dependency for details.
    episode_chunks_enabled: bool = Field(
        default=False,
        description=(
            "F067 master switch. When true, episode_summarizer also chunks the raw "
            "transcript and embeds chunks into heart.episode_chunks. Default OFF."
        ),
    )
    episode_chunk_size: int = Field(
        default=600,
        description="F067 chunk size in chars (sliding window).",
    )
    episode_chunk_overlap: int = Field(
        default=80,
        description="F067 chunk overlap in chars to avoid splitting key tokens.",
    )
    episode_chunk_recall_limit: int = Field(
        default=10,
        description="F067 max chunks returned by the new chunk-recall leg before RRF merge.",
    )
    episode_chunk_min_transcript_chars: int = Field(
        default=50,
        description="F067 minimum transcript length to chunk (shorter transcripts are skipped).",
    )

    # ── F069: document-aware ingestion ───────────────────────────────────
    # Distinct from F067 because document bodies (arxiv papers, doc pages,
    # parsed PDF/.docx text) benefit from larger, structure-aware chunks
    # than the dialogue chunker provides. Opt-in surface: the agent calls
    # `ingest_document(content, source_ref)` with text it has already
    # extracted (e.g. via run_python + pypdf).
    document_ingest_enabled: bool = Field(
        default=True,
        description=(
            "F069 master switch for the ingest_document tool. When false, "
            "the tool refuses calls; existing dialogue chunks (F067) are "
            "unaffected. Default ON because the tool is opt-in at call "
            "time — no auto-classification in v1."
        ),
    )
    document_chunk_size: int = Field(
        default=1500,
        ge=200,
        description="F069 target chunk size in chars (~250 words).",
    )
    document_chunk_overlap: int = Field(
        default=200,
        ge=0,
        description="F069 overlap chars between document chunks.",
    )
    document_chunk_min_chars: int = Field(
        default=100,
        ge=10,
        description="F069 minimum doc length to chunk (shorter ingests rejected).",
    )
    recall_include_parent_episodes: bool = Field(
        default=False,
        description=(
            "F067 Phase 2. When true, recall_deep appends up to recall_max_parent_episodes "
            "parent episode summaries to its text output (deduped by source_episode_id). "
            "Validated on per-question isolation only; opt-in for prod."
        ),
    )
    recall_max_parent_episodes: int = Field(
        default=2,
        description="F067 cap on parent episode summaries appended (deduplicated).",
    )
    recall_parent_episode_truncate: int = Field(
        default=500,
        description="F067 per-parent-episode summary char truncation.",
    )
    recall_exclude_context_ids: bool = Field(
        default=False,
        description=(
            "F071. When true, recall_deep filters out items already loaded "
            "into the current turn's system prompt (facts, decisions, "
            "episodes, procedures). Land dark; flip in dev for measurement."
        ),
    )
    session_group_heart_section: bool = Field(
        default=False,
        description=(
            "P1.1 (2026-05-25). When true, recall_deep groups Heart Memory "
            "section items (facts/chunks/episodes) by source session_id, "
            "with section headers ('-- Session abc12345 --'). Helps the LLM "
            "synthesize across sessions for multi-session questions. "
            "Validated on labeled LME eval; opt-in for prod until A/B confirms."
        ),
    )
    graph_adjacency_boost_enabled: bool = Field(
        default=False,
        description=(
            "P2 (2026-05-25). When true, retrieval applies a multiplicative "
            "score boost to candidates connected via brain.graph_edges to "
            "other candidates in the same batch. Inspired by gbrain's "
            "adjacency-aware ranking. Leverages F040 sleep-built edges."
        ),
    )
    graph_adjacency_boost_alpha: float = Field(
        default=0.15,
        description="P2: max boost as a fraction of original score (default 0.15 = +15% for the most-connected candidate).",
    )
    # F075 — Temporal fact extraction
    # All flags default OFF for dark-launch consistency (F042/F047/F067/F071 pattern).
    temporal_extraction_enabled: bool = Field(
        default=False,
        description=(
            "F075: include date-anchored event extraction in summarizer + "
            "fact-extractor prompts. When True, the summarizer's candidate_facts "
            "schema accepts an optional event_date field and producer paths "
            "stamp event_date_classified_at on FactInput. Flip after measurement "
            "confirms no regression on existing test suite or LME baseline."
        ),
    )
    candidate_facts_event_limit: int = Field(
        default=30,
        ge=0,
        description=(
            "F075: per-episode cap on date-anchored candidate facts merged "
            "across chunks (before FactExtractor). Stable facts stay capped "
            "at 5. Default 30 covers BEAM-100K-shaped multi-day projects "
            "with daily check-ins."
        ),
    )
    temporal_backfill_default_token_budget: int = Field(
        default=50000,
        description="F075: default Haiku token cap for backfill script when --token-budget is not supplied.",
    )
    # Layer 3 (deferred — settings declared so plumbing is in place for F075.x)
    date_aware_boost_enabled: bool = Field(
        default=False,
        description=(
            "F075 Layer 3 (deferred): multiplicative boost on facts with event_date "
            "in query's inferred date window. Ships disabled until measurement "
            "shows Layer 1+2+4 alone falls short of acceptance criterion #5."
        ),
    )
    date_aware_boost_factor: float = Field(
        default=1.20, ge=1.0, le=2.0,
        description="F075 Layer 3: multiplier applied to in-window facts. 1.0 = no boost.",
    )
    date_aware_boost_window_pad_days: int = Field(
        default=30,
        description="F075 Layer 3: pad days around the inferred query date window.",
    )
    heart_graph_all_types_enabled: bool = Field(
        default=False,
        description=(
            "When true, the heart_graph_neighbors stage expands fact/episode "
            "seeds to neighbors of ALL node types (fact, episode, chunk, "
            "procedure, decision) instead of decisions only. Required to "
            "activate F022 cross-type + F040 densification + F070 chunk edges "
            "that today have no consumer in retrieval. Opt-in until eval "
            "validates on the F051 harness; ships disabled in prod."
        ),
    )
    heart_graph_neighbors_per_seed: int = Field(
        default=3,
        description=(
            "When heart_graph_all_types_enabled is true, this is the per-seed "
            "neighbor limit (vs the 2 used by the decision-only path). Higher "
            "default reflects the larger candidate pool when all types are "
            "surfaced."
        ),
    )
    graph_neighbor_seed_score_enabled: bool = Field(
        default=False,
        description=(
            "Graph-neighbor SCORING fix (eval-gated). When false (default), a "
            "Path-A graph neighbor scores edge_weight * graph_recall_decay, a "
            "hard ceiling of ~0.70 that sits BELOW the vector top-k cutline "
            "(~0.72-0.83), so graph-recovered items can never reach top-k and "
            "the whole association layer is invisible to recall@k (the F065 "
            "dead-end, measured 2026-05-30). When true, a neighbor inherits its "
            "SEED's retrieval score discounted by edge confidence "
            "(seed_score * edge_weight), putting it on the same scale as the "
            "candidate it was reached from so a strong-seed+strong-edge bridge "
            "can clear the cutline. Opt-in for eval measurement (mirrors "
            "rerank_by_score); prod adoption is a separate regression-gated "
            "decision."
        ),
    )
    # =========================================================================
    # F076 — Co-mention / shared-entity linking (associative graph layer)
    # =========================================================================
    comention_linking_enabled: bool = Field(
        default=True,
        description=(
            "F076: during sleep, link facts that NAME the same entity "
            "(shared mention) independent of embedding cosine — the associative "
            "edge the cosine-only graph misses (the Steve Hillage orphan case). "
            "Edges are relation='related_to', extraction_method='co_mention'. "
            "Default ON (core capability). NOTE: retrieval effect requires the "
            "consumers (heart_graph_all_types_enabled / graph_adjacency_boost / "
            "graph_neighbor_seed_score) and these edges also raise global graph "
            "density (can auto-trip spreading activation)."
        ),
    )
    comention_max_degree: int = Field(
        default=10, ge=2,
        description="F076: skip hub entities mentioned in > N facts (noise bound).",
    )
    comention_max_edges_per_node: int = Field(
        default=20, ge=1,
        description="F076: per-fact co-mention edge fan-out cap.",
    )
    comention_weight: float = Field(
        default=0.90, ge=0.0, le=1.0,
        description=(
            "F076: stored weight of a co-mention edge (raw INSERT, no relation multiplier). "
            "Default 0.90 (was 0.80): Path-A's seed-score composition scores a recovered "
            "neighbor at seed_score x comention_weight, and the private-fact value harness "
            "(scripts/diag/hippo/privfact/) showed 0.80 lands a fully-vector-missed disjoint "
            "bridge at rank 11 (one short of top-10) while 0.90 clears the cutline (rank 7) "
            "with zero control displacement. Only affects retrieval once the consumer flags "
            "(heart_graph_all_types_enabled, graph_neighbor_seed_score_enabled) are on."
        ),
    )
    comention_min_entity_chars: int = Field(
        default=6, ge=1,
        description="F076: minimum normalized entity-phrase length to link on.",
    )
    comention_max_facts_per_cycle: int = Field(
        default=5000, ge=1,
        description="F076: max active facts scanned per sleep cycle (most-recent first); safety bound on the O(corpus) pass.",
    )
    # =========================================================================
    # Gap-1 Formation — experiential co-occurrence linking (migration 055)
    # =========================================================================
    cooccurrence_linking_enabled: bool = Field(
        default=False,
        description=(
            "Gap-1 formation: during sleep, link facts learned from the SAME source "
            "episode (mentioned together in one occasion) with a co-activation edge — "
            "the associative link the cosine-only graph misses when two co-experienced "
            "facts share no words and aren't semantically near (no-handle case). Edges are "
            "relation='co_occurred' (carries the semantics so the agent can contextualise, "
            "unlike generic related_to), extraction_method='co_occurrence'. Default OFF "
            "(new, opt-in; flip after eval). Retrieval effect needs the consumers "
            "(heart_graph_all_types_enabled / graph_neighbor_seed_score / adjacency_boost) "
            "AND the recall_deep Graph-Connected Memories formatter section."
        ),
    )
    cooccurrence_weight: float = Field(
        default=0.90, ge=0.0, le=1.0,
        description=(
            "Stored weight of a co_occurred edge (raw INSERT). Default 0.90 mirrors "
            "comention_weight — the weight-sweep showed a single co-occurrence needs ~0.8+ "
            "to clear the top-10 cutline via Path-A seed-score composition; lower weights "
            "rank the neighbour too low for the agent to use. Strengthen-by-use (later) "
            "replaces this fixed weight with a learned one."
        ),
    )
    cooccurrence_max_episode_facts: int = Field(
        default=6, ge=2,
        description=(
            "Noise gate: skip episodes that produced more than N facts. A focused "
            "conversation co-mentions a few related things; a rambling one touches many "
            "unrelated topics — linking all pairs there is noise, not association."
        ),
    )
    cooccurrence_max_episodes_per_cycle: int = Field(
        default=2000, ge=1,
        description="Safety bound: max episodes scanned per sleep cycle (most-recent first).",
    )
    # =========================================================================
    # F070 — Chunk-aware sleep consolidation (v1: edges only, no schema change)
    # =========================================================================
    chunk_consolidation_enabled: bool = Field(
        default=False,
        description=(
            "F070 (2026-05-25). When true, sleep cycle and EpisodeSummarizer "
            "build graph edges to/from heart.episode_chunks rows. Fixes the "
            "gap that chunks have zero edges (audit 2026-05-25 found 1,775 "
            "edges, all fact↔fact / procedure↔procedure). Required for "
            "adjacency boost and F022 spreading activation to reach chunks."
        ),
    )
    graph_backfill_max_chunks: int = Field(
        default=100,
        description="F070: max orphan chunks processed per sleep cycle (caps embedding/LLM cost).",
    )
    graph_threshold_chunk_fact: float = Field(
        default=0.55,
        description="F070: cosine floor for chunk→fact same-episode edges.",
    )
    graph_threshold_chunk_chunk_intra: float = Field(
        default=0.70,
        description="F070: cosine floor for non-adjacent intra-episode chunk↔chunk edges. Adjacent pairs always linked (sequential edge_type, weight=1.0).",
    )
    graph_threshold_chunk_chunk_cross: float = Field(
        default=0.85,
        description="F070: cosine floor for cross-episode chunk↔chunk dedup edges.",
    )
    # =========================================================================
    # F070.1 — Cross-episode chunk graph edges (extends F070 v1 same-episode)
    # =========================================================================
    graph_threshold_chunk_fact_cross: float = Field(
        default=0.75,
        description=(
            "F070.1: cosine floor for chunk → fact summarized_by edges ACROSS "
            "episodes. Stricter than same-episode (0.55) because cross-episode "
            "is a noisier candidate pool — only strong semantic matches should "
            "bridge sessions."
        ),
    )
    graph_backfill_max_chunks_cross_episode: int = Field(
        default=2000,
        description=(
            "F070.1: per-cycle cap on chunks processed by the cross-episode "
            "backfill (mirrors graph_backfill_max_chunks for v1's same-episode "
            "pass)."
        ),
    )
    chunk_cross_episode_top_k: int = Field(
        default=20,
        description=(
            "F070.1: HNSW-bounded LIMIT for the cross-episode cosine scan "
            "per chunk. Higher = more candidates to threshold-gate; lower = "
            "tighter. 20 lands ~5 surviving neighbors per chunk in eval."
        ),
    )

    @model_validator(mode="after")
    def _detect_explicit_overrides(self) -> "Settings":
        object.__setattr__(self, '_compaction_threshold_explicit',
                          'compaction_threshold' in self.model_fields_set)
        object.__setattr__(self, '_keep_recent_explicit',
                          'keep_recent_tokens' in self.model_fields_set)
        return self

    @model_validator(mode="after")
    def _validate_keepalive(self) -> "Settings":
        if self.keepalive_interval >= self.tool_timeout:
            raise ValueError(
                f"keepalive_interval ({self.keepalive_interval}) must be < "
                f"tool_timeout ({self.tool_timeout})"
            )
        return self

    @model_validator(mode="after")
    def _validate_compaction(self) -> "Settings":
        if self.tool_soft_trim_head + self.tool_soft_trim_tail >= self.tool_soft_trim_chars:
            raise ValueError(
                f"tool_soft_trim_head ({self.tool_soft_trim_head}) + "
                f"tool_soft_trim_tail ({self.tool_soft_trim_tail}) must be < "
                f"tool_soft_trim_chars ({self.tool_soft_trim_chars})"
            )
        return self

    @model_validator(mode="after")
    def _validate_pruning_tiers(self) -> "Settings":
        if self.tool_metadata_degrade_after >= self.tool_hard_clear_after:
            raise ValueError(
                f"tool_metadata_degrade_after ({self.tool_metadata_degrade_after}) "
                f"must be < tool_hard_clear_after ({self.tool_hard_clear_after})"
            )
        return self

    @model_validator(mode="after")
    def _validate_thinking(self) -> "Settings":
        if self.thinking_mode == "manual":
            if self.thinking_budget < 1024:
                raise ValueError("thinking_budget must be >= 1024 (API minimum)")
            if self.thinking_budget >= self.max_tokens:
                raise ValueError(
                    f"thinking_budget ({self.thinking_budget}) must be < "
                    f"max_tokens ({self.max_tokens}). Increase max_tokens."
                )
        return self

    @model_validator(mode="after")
    def _validate_dag_timeouts(self) -> "Settings":
        if self.dag_node_default_timeout > self.dag_node_max_timeout:
            raise ValueError(
                f"dag_node_default_timeout ({self.dag_node_default_timeout}) must be <= "
                f"dag_node_max_timeout ({self.dag_node_max_timeout})"
            )
        return self

    @model_validator(mode="after")
    def _validate_dag_stall_timeouts(self) -> "Settings":
        """F064.1: stall ≤ wall-clock invariant.

        Only enforced when stall detection is opted in. Today's default
        (stall=600, wall=600, max_stall=3600, max_wall=7200) is mutually
        consistent so a fresh-DB startup with defaults passes.
        """
        if not self.dag_stall_detection_enabled:
            return self
        if self.dag_node_default_stall_timeout > self.dag_node_default_timeout:
            raise ValueError(
                f"dag_node_default_stall_timeout ({self.dag_node_default_stall_timeout}) must be <= "
                f"dag_node_default_timeout ({self.dag_node_default_timeout})"
            )
        if self.dag_node_max_stall_timeout > self.dag_node_max_timeout:
            raise ValueError(
                f"dag_node_max_stall_timeout ({self.dag_node_max_stall_timeout}) must be <= "
                f"dag_node_max_timeout ({self.dag_node_max_timeout})"
            )
        return self

    @property
    def db_url(self) -> str:
        return f"postgresql+asyncpg://{self.db_user}:{self.db_password}@{self.db_host}:{self.db_port}/{self.db_name}"

    def _get_context_window(self, model: str) -> int:
        if self.context_window > 0:
            return self.context_window
        from nous.cognitive.schemas import MODEL_CONTEXT_WINDOWS
        for key in sorted(MODEL_CONTEXT_WINDOWS, key=len, reverse=True):
            if key in model:
                return MODEL_CONTEXT_WINDOWS[key]
        return 200_000

    @property
    def effective_compaction_threshold(self) -> int:
        if getattr(self, '_compaction_threshold_explicit', False):
            return self.compaction_threshold
        from nous.cognitive.schemas import COMPACTION_THRESHOLD_RATIO
        return int(self._get_context_window(self.model) * COMPACTION_THRESHOLD_RATIO)

    @property
    def effective_keep_recent(self) -> int:
        if getattr(self, '_keep_recent_explicit', False):
            return self.keep_recent_tokens
        from nous.cognitive.schemas import KEEP_RECENT_RATIO
        return int(self._get_context_window(self.model) * KEEP_RECENT_RATIO)
