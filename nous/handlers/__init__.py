"""Event handlers for Nous.

Handlers listen to bus events and react asynchronously.
Each handler registers itself on specific event types during __init__.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Protocol, runtime_checkable

from nous.config import Settings  # noqa: F401 — re-exported

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Background LLM helper — shared by all handlers that need LLM calls
# ---------------------------------------------------------------------------


@runtime_checkable
class LLMClient(Protocol):
    """Minimal protocol matching AnthropicClient.call()."""

    async def call(self, payload: dict[str, Any]) -> Any: ...


async def call_background_llm(
    client: LLMClient,
    model: str,
    system_prompt: str,
    user_message: str,
    max_tokens: int = 800,
) -> str | None:
    """Call LLM for background tasks using the same API contract as the runner.

    Builds a proper payload with system blocks + cache_control (matching
    runner._build_api_payload format), then delegates to client.call()
    which handles auth, retries, HTTP/2, and beta headers.

    Returns the text content from the response, or None on failure.
    """
    payload: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "system": [
            {
                "type": "text",
                "text": "You are Claude Code, Anthropic's official CLI for Claude.",
                "cache_control": {"type": "ephemeral"},
            },
            {
                "type": "text",
                "text": system_prompt,
                "cache_control": {"type": "ephemeral"},
            },
        ],
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": user_message,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
            }
        ],
    }

    try:
        response = await client.call(payload)
        # Extract text from response content blocks
        for block in response.content:
            if isinstance(block, dict) and block.get("type") == "text":
                return block.get("text", "")
        return None
    except Exception as e:
        logger.warning("Background LLM call failed: %s", e)
        return None


async def call_background_llm_structured(
    client: LLMClient,
    model: str,
    system_prompt: str,
    user_message: str,
    tool_name: str,
    tool_description: str,
    output_schema: dict[str, Any],
    max_tokens: int = 1500,
) -> dict[str, Any] | None:
    """Call LLM using tool_use trick for guaranteed structured JSON output.

    Defines a fake tool whose input_schema matches the desired output schema,
    then forces the model to "call" it via tool_choice. The API enforces valid
    JSON matching the schema at generation time — no post-hoc parsing needed.

    Returns the structured dict, or None on failure.
    """
    payload: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "system": [
            {
                "type": "text",
                "text": "You are Claude Code, Anthropic's official CLI for Claude.",
                "cache_control": {"type": "ephemeral"},
            },
            {
                "type": "text",
                "text": system_prompt,
                "cache_control": {"type": "ephemeral"},
            },
        ],
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": user_message,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
            }
        ],
        "tools": [
            {
                "name": tool_name,
                "description": tool_description,
                "input_schema": output_schema,
            }
        ],
        "tool_choice": {"type": "tool", "name": tool_name},
    }

    try:
        response = await client.call(payload)
        # Extract tool_use block — guaranteed by tool_choice
        for block in response.content:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                return block["input"]
        logger.warning("No tool_use block in structured LLM response")
        return None
    except Exception as e:
        logger.warning("Structured background LLM call failed: %s", e)
        return None


def _extract_braces(text: str, opener: str, closer: str) -> str | None:
    """Extract the first balanced {…} or […] from text, respecting JSON strings.

    Tracks whether we're inside a double-quoted string so that braces
    within string values don't break the depth counter.
    """
    start = text.find(opener)
    if start < 0:
        return None

    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if escape:
            escape = False
            continue
        if ch == "\\":
            if in_string:
                escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def _repair_json(text: str) -> str:
    """Attempt to fix common LLM JSON errors before json.loads.

    Handles: trailing commas before } or ], control characters in strings.
    """
    # Strip trailing commas before } or ] (common LLM mistake)
    repaired = re.sub(r",\s*([}\]])", r"\1", text)
    # Replace bare control characters (tabs, newlines inside strings break json.loads)
    # Only replace control chars that aren't \n or \r at the structural level
    # This is a best-effort repair
    return repaired


def _try_parse_json(candidate: str, context: str) -> Any | None:
    """Try json.loads, then try with repairs. Returns parsed result or None."""
    try:
        return json.loads(candidate)
    except json.JSONDecodeError as e:
        logger.debug(
            "json.loads failed (%s) on %s candidate (%d chars): %s", e.msg, context, len(candidate), candidate[:300]
        )
    # Try with repairs
    repaired = _repair_json(candidate)
    if repaired != candidate:
        try:
            result = json.loads(repaired)
            logger.info("JSON parse succeeded after repair (%s)", context)
            return result
        except json.JSONDecodeError:
            pass
    return None


def parse_llm_json(text: str) -> Any:
    """Parse JSON from LLM response, handling markdown fences and preamble.

    Tries direct parse first, then strips markdown fences (including
    unclosed fences), then extracts the first JSON object/array from
    surrounding text using string-aware brace matching.

    Always prefers dict ({}) over array ([]) results when both are possible.

    Raises json.JSONDecodeError if no valid JSON found.
    """
    # Try direct parse first
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        logger.debug("Direct JSON parse failed, attempting extraction from: %s", text[:200])

    # Try direct parse with repairs
    repaired = _repair_json(text)
    if repaired != text:
        try:
            result = json.loads(repaired)
            logger.info("Direct JSON parse succeeded after repair")
            return result
        except json.JSONDecodeError:
            pass

    # Strip markdown fences: ```json ... ``` or ``` ... ```
    # Also handles unclosed fences (LLM stopped without closing ```)
    fence_match = re.search(
        r"```(?:json)?\s*\n?(.*?)\n?\s*```",
        text,
        re.DOTALL,
    )
    if not fence_match:
        # Fallback: unclosed fence — take everything after ```json
        fence_match = re.search(
            r"```(?:json)?\s*\n?(.*)",
            text,
            re.DOTALL,
        )
    if fence_match:
        extracted = fence_match.group(1).strip()
        result = _try_parse_json(extracted, "fence-stripped")
        if result is not None:
            logger.info("JSON extraction succeeded via markdown fence stripping")
            return result

        # The fence body may have trailing text; try brace extraction on it
        # Always try {} first, then [] — prefer dict over array
        dict_candidate = _extract_braces(extracted, "{", "}")
        if dict_candidate:
            result = _try_parse_json(dict_candidate, "fence+brace {}")
            if result is not None:
                logger.info("JSON extraction succeeded via fence + brace-matching ({...})")
                return result
            logger.warning(
                "fence+brace {} extraction found candidate (%d chars) but "
                "json.loads failed even after repair. Candidate: %s",
                len(dict_candidate),
                dict_candidate[:500],
            )

        list_candidate = _extract_braces(extracted, "[", "]")
        if list_candidate:
            result = _try_parse_json(list_candidate, "fence+brace []")
            if result is not None:
                if dict_candidate:
                    # We had a dict candidate but it failed — log diagnostic
                    logger.warning(
                        "JSON extraction fell back to array ([...]) because dict "
                        "extraction failed. This may indicate malformed JSON in "
                        "the LLM response. Full fence content (%d chars): %s",
                        len(extracted),
                        extracted[:1000],
                    )
                logger.info("JSON extraction succeeded via fence + brace-matching ([...])")
                return result

    # Extract first JSON object or array from surrounding text
    # Always try {} first to prefer dict results
    dict_candidate = _extract_braces(text, "{", "}")
    if dict_candidate:
        result = _try_parse_json(dict_candidate, "raw-brace {}")
        if result is not None:
            logger.info("JSON extraction succeeded via brace-matching ({...})")
            return result
        logger.warning(
            "raw-brace {} extraction found candidate (%d chars) but json.loads failed even after repair. Candidate: %s",
            len(dict_candidate),
            dict_candidate[:500],
        )

    list_candidate = _extract_braces(text, "[", "]")
    if list_candidate:
        result = _try_parse_json(list_candidate, "raw-brace []")
        if result is not None:
            if dict_candidate:
                logger.warning(
                    "JSON extraction fell back to array ([...]) because dict "
                    "extraction failed. Full text (%d chars): %s",
                    len(text),
                    text[:1000],
                )
            logger.info("JSON extraction succeeded via brace-matching ([...])")
            return result

    logger.warning("No valid JSON found in LLM response (%d chars): %s", len(text), text[:1000])
    raise json.JSONDecodeError("No JSON object found in response", text, 0)
