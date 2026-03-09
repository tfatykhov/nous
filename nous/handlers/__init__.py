"""Event handlers for Nous.

Handlers listen to bus events and react asynchronously.
Each handler registers itself on specific event types during __init__.
"""

import json
import logging
import re
from typing import Any

from nous.config import Settings

logger = logging.getLogger(__name__)


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


def build_anthropic_headers(settings: Settings) -> dict[str, str]:
    """Build auth headers for Anthropic API calls.

    Shared by all handlers that make LLM calls (episode_summarizer,
    fact_extractor, sleep_handler).
    """
    headers: dict[str, str] = {"anthropic-version": "2023-06-01"}
    api_key = getattr(settings, "anthropic_auth_token", None) or getattr(
        settings, "anthropic_api_key", None
    )
    if api_key and "sk-ant-oat" in api_key:
        headers["Authorization"] = f"Bearer {api_key}"
        headers["anthropic-beta"] = "oauth-2025-04-20"
        headers["anthropic-dangerous-direct-browser-access"] = "true"
    else:
        headers["x-api-key"] = api_key or ""
    return headers
