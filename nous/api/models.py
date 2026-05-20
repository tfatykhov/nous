"""Shared data models for the API layer.

Extracted from runner.py to avoid circular imports with compaction.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from nous.cognitive.schemas import TurnContext


@dataclass
class Message:
    """A single message in a conversation."""

    role: str  # "user" or "assistant"
    content: str


@dataclass
class Conversation:
    """Tracks a multi-turn conversation."""

    session_id: str
    messages: list[Message] = field(default_factory=list)
    turn_contexts: list[TurnContext] = field(default_factory=list)
    summary: str | None = None
    compaction_count: int = 0


@dataclass
class ApiResponse:
    """Parsed response from Anthropic Messages API."""

    content: list[dict[str, Any]]  # Raw content blocks from API
    stop_reason: str  # end_turn, max_tokens, tool_use, stop_sequence
    usage: dict[str, int] | None = None


# F062: typed spawn_sync — formal alias for the seven canonical strings that
# F061 writes to heart.subtasks.final_outcome (see sql/migrations/041).
SubtaskOutcome = Literal[
    "completed",
    "incomplete_blocked",
    "incomplete_no_terminal",
    "validation_failed",
    "timed_out",
    "errored",
    "cancelled",
]


@dataclass
class SubtaskResult:
    """Typed return value from F062's spawn_sync tool.

    status mirrors heart.subtasks.final_outcome — never derived from
    schema-validation alone. Schema validation can only flip an in-flight
    'completed' to 'validation_failed' (handled inside execute_hardened);
    other terminal outcomes (errored / timed_out / cancelled /
    incomplete_*) flow through unchanged.

    payload is typed Any (not dict) because submit_final_report.payload
    accepts any JSON value (object/array/string/number/boolean/null).
    """

    task_id: str
    status: SubtaskOutcome
    payload: Any
    raw_text: str
    confidence: float | None
    elapsed_seconds: float
    validator_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "status": self.status,
            "payload": self.payload,
            "raw_text": self.raw_text,
            "confidence": self.confidence,
            "elapsed_seconds": round(self.elapsed_seconds, 3),
            "validator_reason": self.validator_reason,
        }
