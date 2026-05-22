"""F066.1 — fix-node action selection.

Phase 1 ships a deterministic rule-based dispatcher. The spec calls for
free-form LLM dispatch in Phase 1; we deviate intentionally to keep the
state-machine surface (new node type, new terminal state, action
processing) testable and reviewable without standing up an LLM-call
mechanism inside the orchestrator. LLM-based diagnosis lands in a
Phase 1.5 follow-up that swaps `choose_action` for a single LLM call
with tool-use constraint on `fix_actions`.

The rule (Phase 1):

  parent.error contains substring         → action (if in fix_actions)
  --------------------------------------------------------------
  "incomplete_no_terminal" / "validation_failed"
                                          → retry_as_is
  "timed_out"                             → skip_and_continue if available, else mark_unrecoverable
  any other / "errored"                   → skip_and_continue if available, else mark_unrecoverable

Any action not present in `fix_actions` is replaced by the next allowed
fallback. If no allowed action matches, `mark_unrecoverable` is the
final fallback (always implicitly available).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class FixActionResult:
    """Outcome of a fix-node firing."""

    action: str
    amended_prompt: str | None = None
    mode: str | None = None
    rationale: str | None = None


def choose_action(
    parent_error: str | None,
    parent_status: str,
    fix_actions: list[str] | None,
) -> FixActionResult:
    """Pick the fix action for a failed parent based on its error string.

    Args:
        parent_error: the failing parent node's error column (may be None).
        parent_status: the parent's current status (typically 'failed').
        fix_actions: the allowed action vocabulary declared on the fix node.

    Returns:
        FixActionResult with chosen action + rationale.
    """
    allowed = set(fix_actions or [])
    # mark_unrecoverable is always implicitly available as a last resort,
    # even if not declared.
    allowed.add("mark_unrecoverable")

    err = (parent_error or "").lower()

    # Recoverable / structural failures → retry.
    if any(token in err for token in ("incomplete_no_terminal", "validation_failed")):
        if "retry_as_is" in allowed:
            return FixActionResult(
                action="retry_as_is",
                rationale=f"parent.error matched recoverable pattern; chose retry_as_is",
            )

    # Timed out → prefer skip over retry (retry of a timeout is wasteful).
    if "timed_out" in err or "timeout" in err:
        if "skip_and_continue" in allowed:
            return FixActionResult(
                action="skip_and_continue",
                rationale="parent timed out; chose skip_and_continue",
            )
        if "mark_unrecoverable" in allowed:
            return FixActionResult(
                action="mark_unrecoverable",
                rationale="parent timed out; no skip allowed; chose mark_unrecoverable",
            )

    # Everything else (errored, unknown) → prefer skip if allowed.
    if "skip_and_continue" in allowed:
        return FixActionResult(
            action="skip_and_continue",
            rationale=f"parent error ({err[:80] or 'unknown'}); chose skip_and_continue",
        )
    if "retry_with_amended_prompt" in allowed:
        # Amended prompt is a noop in Phase 1 (no LLM diagnosis to amend
        # with); fall through to mark_unrecoverable instead of retry-loop.
        logger.debug(
            "F066.1: retry_with_amended_prompt declared but Phase 1 has no "
            "LLM diagnosis to amend with; falling through"
        )

    return FixActionResult(
        action="mark_unrecoverable",
        rationale="no recoverable action matched; mark_unrecoverable (final fallback)",
    )
