"""F061: submit_final_report tool — terminal contract for hardened subtasks.

The hardened subtask executor injects this tool into the toolset for one
run only (NEVER via ``ToolDispatcher.register`` — that would leak it into
chat sessions on a worker crash). The runner detects a successful
``tool_use(name="submit_final_report")`` block and short-circuits the loop.

Lock-on-first semantics
-----------------------
A model that calls ``submit_final_report`` twice in one turn (e.g., trying
to "correct" itself) MUST NOT overwrite the first valid payload. The first
call wins; the executor returns an error string for any subsequent call so
the model sees the lockout and won't try a third time.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)


SUBMIT_FINAL_REPORT_SCHEMA: dict[str, Any] = {
    "name": "submit_final_report",
    "description": (
        "Submit your final, complete report for this subtask. You MUST call "
        "this tool exactly once when you are done. Do not produce a final "
        "text-only response — the parent agent receives ONLY this tool's "
        "payload."
    ),
    "input_schema": {
        "type": "object",
        "required": ["summary", "confidence"],
        "properties": {
            "summary": {
                "type": "string",
                "minLength": 50,
                "description": (
                    "1-3 paragraph synthesis of what you did and what the "
                    "answer is. Must be self-contained — the parent will "
                    "read this without seeing your tool calls."
                ),
            },
            "findings": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Key facts, numbers, or sub-conclusions discovered.",
                "default": [],
            },
            "next_actions": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Recommended next actions, if any.",
                "default": [],
            },
            "confidence": {
                "type": "number",
                "minimum": 0.0,
                "maximum": 1.0,
                "description": (
                    "Your confidence (0.0-1.0) that the summary is correct "
                    "and addresses the task."
                ),
            },
            "evidence_refs": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Lightweight references — fact UUIDs, decision IDs, or "
                    "external sources. DO NOT dump full content here."
                ),
                "default": [],
            },
            "incomplete": {
                "type": "boolean",
                "description": (
                    "Set true ONLY if you genuinely cannot complete the task "
                    "(missing tools, missing info, blocked by external system). "
                    "Otherwise false."
                ),
                "default": False,
            },
            "blocked_reason": {
                "type": "string",
                "description": (
                    "If incomplete=true, the specific reason. Required when "
                    "incomplete=true; ignored otherwise."
                ),
                "default": "",
            },
        },
        "additionalProperties": False,
    },
}


class SubtaskReportCollector:
    """Captures the FIRST submit_final_report payload of one subtask run.

    Lock-on-first protects against double-submission overwriting a valid
    payload (per F061 spec review P1 finding). Resettable across attempts
    in the retry loop.
    """

    __slots__ = ("_payload", "_submission_count")

    def __init__(self) -> None:
        self._payload: dict[str, Any] | None = None
        self._submission_count: int = 0

    def reset(self) -> None:
        """Clear state for a new attempt in the retry loop."""
        self._payload = None
        self._submission_count = 0

    def set(self, payload: dict[str, Any]) -> bool:
        """Lock-on-first. Returns True if accepted; False if rejected."""
        self._submission_count += 1
        if self._payload is not None:
            logger.warning(
                "submit_final_report called %d times; ignoring duplicate "
                "(first payload locked).",
                self._submission_count,
            )
            return False
        self._payload = payload
        return True

    def get(self) -> dict[str, Any] | None:
        return self._payload

    def is_set(self) -> bool:
        return self._payload is not None

    @property
    def submission_count(self) -> int:
        return self._submission_count


def make_submit_final_report_executor(
    collector: SubtaskReportCollector,
) -> Callable[..., Awaitable[tuple[str, bool]]]:
    """Build an async executor matching ToolDispatcher's contract.

    Returns ``(result_text: str, is_error: bool)`` where ``is_error`` is True
    only on duplicate-submission. The runner sees the successful first call
    as ``tool_use`` content and short-circuits the loop.
    """

    async def _executor(**kwargs: Any) -> tuple[str, bool]:
        accepted = collector.set(kwargs)
        if accepted:
            return ("Report received. Subtask will terminate.", False)
        return (
            "ERROR: submit_final_report has already been called for this "
            "subtask. Do not call it again. The first payload is locked.",
            True,
        )

    return _executor
