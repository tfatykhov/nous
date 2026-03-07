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


def parse_llm_json(text: str) -> Any:
    """Parse JSON from LLM response, handling markdown fences and preamble.

    Tries direct parse first, then strips markdown fences, then extracts
    the first JSON object/array from surrounding text.

    Raises json.JSONDecodeError if no valid JSON found.
    """
    # Try direct parse first
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        logger.info("Direct JSON parse failed, attempting extraction from: %s",
                     text[:200])

    # Strip markdown fences: ```json ... ``` or ``` ... ```
    fence_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?\s*```", text, re.DOTALL)
    if fence_match:
        extracted = fence_match.group(1).strip()
        try:
            result = json.loads(extracted)
            logger.info("Extracted JSON from markdown fence")
            return result
        except json.JSONDecodeError:
            logger.info("Markdown fence content was not valid JSON: %s",
                         extracted[:200])

    # Extract first JSON object or array from surrounding text
    # Try whichever delimiter appears first
    candidates = [("{", "}"), ("[", "]")]
    candidates.sort(key=lambda pair: (text.find(pair[0]) if text.find(pair[0]) >= 0 else float("inf")))
    for opener, closer in candidates:
        start = text.find(opener)
        if start >= 0:
            depth = 0
            for i in range(start, len(text)):
                if text[i] == opener:
                    depth += 1
                elif text[i] == closer:
                    depth -= 1
                    if depth == 0:
                        candidate = text[start : i + 1]
                        try:
                            result = json.loads(candidate)
                            logger.info("Extracted JSON %s from surrounding text",
                                         opener)
                            return result
                        except json.JSONDecodeError:
                            break

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
