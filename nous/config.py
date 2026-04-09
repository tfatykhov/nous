"""Settings via pydantic-settings with NOUS_ env prefix.

DB connection fields use validation_alias to read from the same unprefixed
env vars (DB_PASSWORD, DB_PORT, etc.) that docker-compose uses, so a single
.env file drives both the container and the Python app.
"""

from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="NOUS_", env_file=".env")

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
    log_level: str = "info"

    # Brain settings
    openai_api_key: str = Field("", validation_alias="OPENAI_API_KEY")
    auto_link_threshold: float = 0.85
    auto_link_max: int = 3
    quality_block_threshold: float = 0.5

    # Anti-hallucination (F016 Phase 0)
    anti_hallucination_prompt: bool = True

    # F025 prep: hybrid search vector weight (keyword_weight = 1 - vector_weight)
    vector_weight: float = 0.7
    rrf_k: int = 60  # RRF smoothing constant (F025)

    # F017: Relevance floor
    relevance_floor_enabled: bool = True

    # F038-2.1: Procedure score floor (embedding mode only)
    procedure_score_floor: float = 0.40

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
    context_budget_overrides: dict[str, int] = Field(default_factory=dict)

    # F017: Staleness penalty
    staleness_penalty_enabled: bool = True
    staleness_half_life_days: int = 30

    # F025 P2-C: Transcript truncation limit for episode summarization
    transcript_max_chars: int = 16000

    # F025 P2-D: Fact extractor dedup threshold (raised from 0.85)
    fact_dedup_threshold: float = 0.92

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
    workspace_dir: str = "/tmp/nous-workspace"

    # Web tools
    brave_search_api_key: str = Field("", validation_alias="BRAVE_SEARCH_API_KEY")
    web_search_daily_limit: int = 100  # Max web searches per day
    web_fetch_max_chars: int = 10000  # Default max chars for web_fetch
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
    subtask_default_timeout: int = 120
    subtask_max_timeout: int = 600
    subtask_max_concurrent: int = 3
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

    # F030: MMR Diversity Re-Ranking
    mmr_enabled: bool = False
    mmr_diversity_weight: float = Field(
        default=0.7, ge=0.0, le=1.0,
        description="MMR relevance vs diversity weight (1.0=pure relevance, 0.0=pure diversity)",
    )

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
