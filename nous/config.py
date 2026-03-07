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

    # F017: Relevance floor
    relevance_floor_enabled: bool = True

    # F017: Diminishing returns cutoff
    relevance_drop_ratio: float = 0.6

    # F017: Budget scaling
    budget_scale_enabled: bool = True

    # F017: Staleness penalty
    staleness_penalty_enabled: bool = True
    staleness_half_life_days: int = 14

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
        default="claude-sonnet-4-5-20250514",
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
    model: str = "claude-sonnet-4-5-20250514"
    max_tokens: int = 4096

    # Extended thinking
    thinking_mode: Literal["off", "adaptive", "manual"] = "off"
    thinking_budget: int = 10000  # budget_tokens for manual mode (min 1024)
    effort: Literal["low", "medium", "high", "max"] = "high"

    # Context window override (0 = auto-detect from model name)
    context_window: int = Field(
        default=0, validation_alias="NOUS_CONTEXT_WINDOW"
    )

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

    # Tool execution
    tool_timeout: int = Field(
        default=120, validation_alias="NOUS_TOOL_TIMEOUT"
    )  # Max seconds for any single tool execution
    keepalive_interval: int = Field(
        default=10, validation_alias="NOUS_KEEPALIVE_INTERVAL"
    )  # Seconds between keepalive events during tool execution

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
        default_factory=lambda: {"research": "claude-haiku-3-5-20241022"},
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
