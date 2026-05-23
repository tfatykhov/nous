"""F066.1 — fix-node action selection.

Phase 1 shipped a deterministic rule-based dispatcher. Phase 1.5 adds an
LLM-based dispatcher that uses tool-use constraint to pick an action from
``fix_actions`` and (optionally) generate an amended prompt for the
``retry_with_amended_prompt`` action.

Selection precedence at runtime:

1. If ``settings.dag_fix_llm_dispatch_enabled`` AND an LLM client is
   wired into the orchestrator → call ``choose_action_llm``.
2. If the LLM call fails (timeout, parse error, unsupported action) →
   fall back to ``choose_action`` (rule-based).
3. If the flag is off OR no LLM client is wired → ``choose_action``
   directly.

This keeps the Phase 1 contract intact when the flag is off and ensures
the system degrades gracefully when the LLM is unavailable.

Rule-based rule (Phase 1, unchanged):

  parent.error contains substring         → action (if in fix_actions)
  --------------------------------------------------------------
  "incomplete_no_terminal" / "validation_failed"
                                          → retry_as_is
  "timed_out"                             → skip_and_continue if available, else mark_unrecoverable
  any other / "errored"                   → skip_and_continue if available, else mark_unrecoverable
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

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


# ---------------------------------------------------------------------------
# Phase 1.5: LLM-based dispatcher
# ---------------------------------------------------------------------------


_CHOOSE_FIX_ACTION_TOOL_NAME = "choose_fix_action"


def _build_choose_fix_action_tool(fix_actions: list[str]) -> dict:
    """Build the tool schema for the LLM. The action enum is constrained
    to fix_actions verbatim so the model can't return an action the fix
    node didn't authorize."""
    return {
        "name": _CHOOSE_FIX_ACTION_TOOL_NAME,
        "description": (
            "Choose ONE action to apply to the parent failure. If you choose "
            "'retry_with_amended_prompt' you MUST also provide a "
            "non-empty 'amended_prompt' that addresses the failure cause."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": list(fix_actions),
                    "description": "The action to apply.",
                },
                "amended_prompt": {
                    "type": "string",
                    "description": (
                        "Required when action='retry_with_amended_prompt'. "
                        "The revised prompt to retry the parent with."
                    ),
                },
                "rationale": {
                    "type": "string",
                    "description": "One-sentence reasoning for the chosen action.",
                },
            },
            "required": ["action", "rationale"],
            "additionalProperties": False,
        },
    }


def _build_fix_prompt(
    parent_name: str,
    parent_instructions: str | None,
    parent_error: str | None,
    parent_result: str | None,
    fix_instructions: str | None,
    fix_actions: list[str],
) -> str:
    """Render the user-message prompt for the LLM dispatcher."""
    lines = []
    if fix_instructions:
        lines.append("# Fix-node instructions")
        lines.append(fix_instructions)
        lines.append("")
    lines.append("# Failing parent node")
    lines.append(f"name: {parent_name}")
    if parent_instructions:
        lines.append(f"instructions: {parent_instructions[:600]}")
    if parent_error:
        lines.append(f"error: {parent_error[:800]}")
    if parent_result:
        lines.append(f"result: {parent_result[:800]}")
    lines.append("")
    lines.append("# Allowed actions (you MUST choose one of these)")
    for a in fix_actions:
        lines.append(f"- {a}")
    lines.append("")
    lines.append(
        "Call the choose_fix_action tool ONCE with your chosen action, "
        "a one-sentence rationale, and (if action='retry_with_amended_prompt') "
        "an amended_prompt that addresses the failure cause."
    )
    return "\n".join(lines)


async def choose_action_llm(
    *,
    parent_name: str,
    parent_instructions: str | None,
    parent_error: str | None,
    parent_result: str | None,
    fix_instructions: str | None,
    fix_actions: list[str],
    llm_client: Any,
    model: str,
    timeout_seconds: float,
) -> FixActionResult:
    """LLM-driven free-form fix-action dispatch (Phase 1.5).

    Uses tool-use forced choice so the model returns a structured action
    from ``fix_actions``. Any failure (timeout, parse error, unsupported
    action) raises — the caller (orchestrator) catches and falls back to
    the rule-based ``choose_action``.

    Args:
        parent_name: parent node name (string label only).
        parent_instructions: parent's instructions / prompt (truncated).
        parent_error: parent's error column (truncated).
        parent_result: parent's result column, if any (truncated).
        fix_instructions: the fix node's instructions template.
        fix_actions: the allowed action vocabulary (passed verbatim into
            the tool's enum constraint).
        llm_client: Anthropic-compatible client (see nous.api.anthropic_client).
        model: model identifier.
        timeout_seconds: hard timeout for the call (passed to
            asyncio.wait_for so a slow LLM never hangs the orchestrator
            tick).

    Returns:
        FixActionResult with the chosen action (always ∈ fix_actions ∪
        {mark_unrecoverable}). Raises on any failure.
    """
    if not fix_actions:
        # Defensive: caller should have validated this. Surface as an
        # exception so the rule-based fallback can take over.
        raise ValueError("fix_actions must be non-empty for LLM dispatch")

    tool = _build_choose_fix_action_tool(fix_actions)
    prompt = _build_fix_prompt(
        parent_name=parent_name,
        parent_instructions=parent_instructions,
        parent_error=parent_error,
        parent_result=parent_result,
        fix_instructions=fix_instructions,
        fix_actions=fix_actions,
    )
    payload = {
        "model": model,
        "max_tokens": 512,
        # Codex P1 (2026-05-22): SdkAnthropicClient._payload_to_kwargs
        # at anthropic_client.py:1014 indexes payload["system"] (not .get),
        # so an empty string is required to keep the SDK backend from
        # KeyError-ing before the request ever reaches Anthropic. Mirrors
        # nous_eval/handlers/summary.py:158.
        "system": "",
        "tools": [tool],
        # Force the tool — Anthropic's tool_choice with a specific tool
        # name guarantees the model returns a tool_use block.
        "tool_choice": {"type": "tool", "name": _CHOOSE_FIX_ACTION_TOOL_NAME},
        "messages": [
            {"role": "user", "content": prompt},
        ],
    }

    response = await asyncio.wait_for(
        llm_client.call(payload),
        timeout=timeout_seconds,
    )

    # Extract the tool_use block.
    tool_input: dict | None = None
    for block in (response.content or []):
        if block.get("type") == "tool_use" and block.get("name") == _CHOOSE_FIX_ACTION_TOOL_NAME:
            tool_input = block.get("input") or {}
            break
    if tool_input is None:
        raise ValueError(
            f"LLM dispatch: no {_CHOOSE_FIX_ACTION_TOOL_NAME} tool_use block in response"
        )

    action = tool_input.get("action")
    if action not in fix_actions:
        raise ValueError(
            f"LLM dispatch: model returned action '{action}' not in "
            f"fix_actions {fix_actions}"
        )

    amended_prompt = tool_input.get("amended_prompt") or None
    if action == "retry_with_amended_prompt" and not amended_prompt:
        # Contract violation — fall back to rule-based.
        raise ValueError(
            "LLM dispatch: action='retry_with_amended_prompt' requires "
            "a non-empty amended_prompt"
        )

    rationale = tool_input.get("rationale") or "LLM dispatch: no rationale provided"
    return FixActionResult(
        action=action,
        amended_prompt=amended_prompt,
        rationale=f"LLM dispatch: {rationale}",
    )
