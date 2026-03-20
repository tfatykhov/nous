"""Event handlers for Nous.

Handlers listen to bus events and react asynchronously.
Each handler registers itself on specific event types during __init__.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Protocol, runtime_checkable

from nous.config import Settings

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


def parse_llm_json(text: str) -> Any:
    """Parse JSON from LLM response, handling markdown fences and preamble.

    Tries direct parse first, then strips markdown fences (including
    unclosed fences), then extracts the first JSON object/array from
    surrounding text using string-aware brace matching.

    Raises json.JSONDecodeError if no valid JSON found.
    """
    # Try direct parse first
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        logger.debug("Direct JSON parse failed, attempting extraction from: %s",
                      text[:200])

    # Strip markdown fences: ```json ... ``` or ``` ... ```
    # Also handles unclosed fences (LLM stopped without closing ```)
    fence_match = re.search(
        r"```(?:json)?\s*\n?(.*?)\n?\s*```",
        text, re.DOTALL,
    )
    if not fence_match:
        # Fallback: unclosed fence — take everything after ```json
        fence_match = re.search(
            r"```(?:json)?\s*\n?(.*)",
            text, re.DOTALL,
        )
    if fence_match:
        extracted = fence_match.group(1).strip()
        try:
            result = json.loads(extracted)
            logger.info("JSON extraction succeeded via markdown fence stripping")
            return result
        except json.JSONDecodeError:
            logger.debug("Markdown fence content was not valid JSON, "
                          "trying brace extraction on fence body")
            # The fence body may have trailing text; try brace extraction on it
            for opener, closer in [("{", "}"), ("[", "]")]:
                candidate = _extract_braces(extracted, opener, closer)
                if candidate:
                    try:
                        result = json.loads(candidate)
                        logger.info("JSON extraction succeeded via fence + "
                                     "brace-matching (%s...%s)", opener, closer)
                        return result
                    except json.JSONDecodeError:
                        continue

    # Extract first JSON object or array from surrounding text
    # Try whichever delimiter appears first
    candidates = [("{", "}"), ("[", "]")]
    candidates.sort(key=lambda pair: (
        text.find(pair[0]) if text.find(pair[0]) >= 0 else float("inf")
    ))
    for opener, closer in candidates:
        candidate = _extract_braces(text, opener, closer)
        if candidate:
            try:
                result = json.loads(candidate)
                logger.info("JSON extraction succeeded via brace-matching (%s...%s)",
                             opener, closer)
                return result
            except json.JSONDecodeError:
                continue

    logger.warning("No valid JSON found in LLM response (%d chars): %s", len(text), text[:1000])
    raise json.JSONDecodeError("No JSON object found in response", text, 0)


