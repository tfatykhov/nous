"""Pydantic DTOs for the Cognitive Layer inputs and outputs.

These models define the data contract between the Cognitive Layer
and its consumers (the Runtime in 005).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from pydantic import BaseModel, Field

# Model context window sizes (tokens)
MODEL_CONTEXT_WINDOWS: dict[str, int] = {
    "claude-sonnet-4-6": 1_000_000,
    "claude-opus-4-6": 1_000_000,
    "claude-sonnet-4-5": 200_000,
    "claude-opus-4-5": 200_000,
    "claude-haiku-4-5": 200_000,
    "gpt-4o": 128_000,
    "gpt-4-turbo": 128_000,
}

COMPACTION_THRESHOLD_RATIO = 0.60
KEEP_RECENT_RATIO = 0.20

# F017 Phase 1: Per-type minimum relevance scores
RELEVANCE_FLOORS: dict[str, float] = {
    "fact": 0.45,
    "decision": 0.40,
    "procedure": 0.25,  # Lowered from 0.50 — old floor filtered nearly all
    # procedures, preventing activation and F012 reinforcement. 0.25 still
    # blocks noise while allowing curated knowledge through.
    "episode": 0.35,
}

# Sources exempt from relevance floor filtering
FLOOR_EXEMPT_SOURCES: set[str] = {
    "pre_prune_extraction",
}

# F016 Phase 4: Per-tool decay profiles for content-type-aware pruning
TOOL_DECAY_PROFILES: dict[str, str] = {
    "read_file": "preserve",
    "list_files": "aggressive",
    "recall_deep": "aggressive",
    "bash": "standard",
    "run_python": "standard",
    "web_search": "conservative",
    "web_fetch": "conservative",
}

# Profile -> (soft_trim_age, metadata_degrade_age, hard_clear_age)
DECAY_PROFILE_AGES: dict[str, tuple[int, int, int]] = {
    "preserve": (8, 999, 20),     # Skip metadata degradation
    "aggressive": (2, 4, 8),
    "standard": (3, 8, 12),       # Default
    "conservative": (5, 10, 15),
}

FRAME_TOOL_WINDOWS: dict[str, int] = {
    "debug": 4,
    "decision": 3,
    "task": 2,
    "question": 2,
    "conversation": 2,
    "creative": 1,
}


@dataclass
class SessionMetadata:
    """Tracks session-level signals for episode significance."""

    turn_count: int = 0
    tools_used: set[str] = field(default_factory=set)  # P2: set not list for O(1) lookup
    total_user_chars: int = 0
    total_assistant_chars: int = 0
    has_explicit_remember: bool = False
    transcript: list[str] = field(default_factory=list)


class FrameType(StrEnum):
    TASK = "task"
    QUESTION = "question"
    DECISION = "decision"
    CREATIVE = "creative"
    CONVERSATION = "conversation"
    DEBUG = "debug"


class FrameSelection(BaseModel):
    """Result of frame selection."""

    frame_id: str
    frame_name: str
    confidence: float = Field(ge=0.0, le=1.0)
    match_method: str  # "pattern" or "default"
    description: str | None = None
    default_category: str | None = None
    default_stakes: str | None = None
    questions_to_ask: list[str] = Field(default_factory=list)


class ContextBudget(BaseModel):
    """Token and turn budgets for context assembly.

    Token budgets (total, identity, … episodes): max estimated tokens per section.
    Turn budget (conversation_window): number of recent user turns checked for
    dedup so the context engine doesn't inject memories already visible in
    the conversation.
    """

    # -- Token budgets (estimated tokens per section) --
    total: int = 8000
    identity: int = 500
    user_profile: int = 200  # Tier 1: preference/person/rule facts (always loaded)
    censors: int = 300
    frame: int = 500
    working_memory: int = 700
    decisions: int = 2000
    facts: int = 1500
    procedures: int = 1500
    episodes: int = 1000
    # -- Turn budget (not tokens) --
    conversation_window: int = 5  # Recent user turns used for dedup

    @classmethod
    def for_frame(cls, frame_id: str, overrides: dict[str, int] | None = None) -> ContextBudget:
        """Return frame-adaptive budget with per-frame conversation windows (D7).

        Args:
            frame_id: The cognitive frame type.
            overrides: Optional dict of field overrides applied on top of
                per-frame defaults (e.g. from Settings.context_budget_overrides).
        """
        budgets = {
            "conversation": cls(total=3000, decisions=500, facts=500, procedures=0, episodes=0, conversation_window=3),
            "question": cls(total=6000, decisions=1000, facts=1500, procedures=500, episodes=500, conversation_window=5),
            "task": cls(total=8000, conversation_window=5),
            "decision": cls(total=12000, decisions=3000, facts=2000, procedures=2000, episodes=1000, conversation_window=8),
            "creative": cls(total=6000, censors=100, decisions=1000, facts=1500, procedures=500, episodes=500, conversation_window=4),
            "debug": cls(total=10000, decisions=1500, facts=1000, procedures=2500, episodes=1000, conversation_window=6),
        }
        budget = budgets.get(frame_id, cls())
        if overrides:
            budget.apply_overrides(overrides)
        return budget

    def apply_overrides(self, overrides: dict[str, int]) -> None:
        """Apply budget overrides with REPLACE semantics (F6).

        Each key in overrides maps to a field name on this model.
        Values replace (not add to) the current allocation.
        """
        for key, value in overrides.items():
            if hasattr(self, key):
                setattr(self, key, value)


class ContextSection(BaseModel):
    """A section of assembled context."""

    priority: int  # 1-8 (1=highest)
    label: str
    content: str
    token_estimate: int  # rough char/4 estimate


class BuildResult(BaseModel):
    """Output of ContextEngine.build() — system prompt + recalled IDs (F1)."""

    system_prompt: str
    sections: list[ContextSection] = Field(default_factory=list)
    recalled_ids: dict[str, list[str]] = Field(default_factory=dict)
    recalled_content_map: dict[str, str] = Field(default_factory=dict)
    recalled_score_map: dict[str, float] = Field(default_factory=dict)


class TurnContext(BaseModel):
    """Output of pre_turn -- everything the agent needs."""

    system_prompt: str
    frame: FrameSelection
    decision_id: str | None = None  # Set if frame is 'decision' or 'task'
    active_censors: list[str] = Field(default_factory=list)
    censor_blocked: bool = False  # Set if a block censor matched user input
    censor_block_reason: str | None = None  # Reason for the block
    context_token_estimate: int = 0
    recalled_decision_ids: list[str] = Field(default_factory=list)
    recalled_fact_ids: list[str] = Field(default_factory=list)
    recalled_procedure_ids: list[str] = Field(default_factory=list)
    recalled_episode_ids: list[str] = Field(default_factory=list)
    recalled_content_map: dict[str, str] = Field(default_factory=dict)
    recalled_score_map: dict[str, float] = Field(default_factory=dict)


class ToolResult(BaseModel):
    """Representation of a tool call result for post_turn."""

    tool_name: str
    arguments: dict = Field(default_factory=dict)
    result: str | None = None
    error: str | None = None
    duration_ms: int | None = None


class TurnResult(BaseModel):
    """Input to post_turn -- what happened during the turn."""

    response_text: str
    tool_results: list[ToolResult] = Field(default_factory=list)
    error: str | None = None
    duration_ms: int | None = None
    thinking_blocks: list[str] = Field(default_factory=list)


class Assessment(BaseModel):
    """Monitor engine output."""

    decision_id: str | None = None
    intended: str | None = None
    actual: str
    surprise_level: float = Field(ge=0.0, le=1.0, default=0.0)
    censor_candidates: list[str] = Field(default_factory=list)
    facts_extracted: int = 0
    episode_recorded: bool = False
