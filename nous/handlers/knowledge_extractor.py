"""Knowledge Extractor — extracts facts from messages before compaction.

Listens to: conversation_compacting
Extracts facts from the message snapshot (messages about to be compacted)
using a background LLM call. Stores via heart.learn_fact().

Conservative extraction — only clearly stated facts, max 5 per compaction.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from nous.config import Settings
from nous.events import Event, EventBus
from nous.handlers import build_anthropic_headers, parse_llm_json
from nous.heart.heart import Heart
from nous.heart.schemas import FactInput

logger = logging.getLogger(__name__)

_EXTRACT_PROMPT = """\
Review the following conversation messages (about to be compacted) and extract facts worth remembering long-term.

Focus on:
- User preferences (tools, formats, communication style, workflow preferences)
- Project/system facts (architecture, constraints, conventions, file paths)
- People facts (roles, names, relationships)
- Technical decisions made during the conversation
- Explicit rules or directives from the user

Conversation:
{conversation_text}

Return ONLY a valid JSON array (empty array if nothing worth storing):
[
  {{
    "subject": "<who/what the fact is about>",
    "content": "<the fact, stated clearly and completely>",
    "category": "<category>",
    "confidence": <0.6-1.0>
  }}
]

Categories (use the RIGHT one — this affects how facts are loaded):
- "preference" — User preferences (formats, units, style). Loaded EVERY turn.
- "person" — People facts (names, roles, relationships). Loaded EVERY turn.
- "rule" — ONLY explicit directives from the user (e.g., "never push to main"). Loaded EVERY turn.
- "technical" — Architecture, implementation, or project-specific knowledge. Loaded only when relevant.
- "concept" — General knowledge, research findings, theoretical insights. Loaded only when relevant.
- "tool" — Tool/library behavior, gotchas, configuration. Loaded only when relevant.

IMPORTANT: "rule" is for user-stated directives ONLY. Research findings, observations,
debug lessons, and architecture patterns should be "technical" or "concept".
If in doubt between "rule" and something else, choose the something else.

Only include facts genuinely useful across future conversations.
Skip transient details, task-specific context, and already-obvious information.
Be conservative — when in doubt, skip it.
Max 5 facts."""


class KnowledgeExtractor:
    """Extracts and stores facts from messages before compaction.

    Listens to conversation_compacting events. The message_snapshot in
    event data is a copy of messages[:cut_point] — decoupled from the
    compaction mutation, so we can safely process it.
    """

    def __init__(
        self,
        heart: Heart,
        settings: Settings,
        bus: EventBus,
        http_client: httpx.AsyncClient | None = None,
    ):
        self._heart = heart
        self._settings = settings
        self._bus = bus
        self._http = http_client
        bus.on("conversation_compacting", self.handle)

    async def handle(self, event: Event) -> None:
        """Handle conversation_compacting — extract and store facts."""
        snapshot: list[dict[str, Any]] = event.data.get("message_snapshot", [])
        if not snapshot:
            return

        try:
            # Serialize messages to text for LLM
            conversation_text = self._serialize_messages(snapshot)
            if len(conversation_text) < 100:
                # Too little content to extract from
                return

            candidates = await self._extract_facts(conversation_text)
            if not candidates:
                return

            stored = 0
            for fact in candidates[:5]:
                confidence = fact.get("confidence", 0.7)
                if confidence < 0.6:
                    logger.debug(
                        "Skipping low-confidence fact: %s",
                        fact.get("content", "")[:50],
                    )
                    continue

                content = fact.get("content", "")
                if not content:
                    continue

                # Dedup against existing facts
                existing = await self._heart.search_facts(content, limit=1)
                if (
                    existing
                    and existing[0].score is not None
                    and existing[0].score > 0.85
                ):
                    logger.debug("Skipping duplicate fact: %s", content[:50])
                    continue

                fact_input = FactInput(
                    subject=fact.get("subject", "unknown"),
                    content=content,
                    source="knowledge_extractor",
                    confidence=confidence,
                    category=fact.get("category"),
                )
                await self._heart.learn(fact_input)
                stored += 1

            if stored:
                logger.info(
                    "Extracted %d facts from compaction snapshot (session %s)",
                    stored,
                    event.session_id or "?",
                )

        except Exception:
            logger.exception(
                "Knowledge extraction failed for session %s",
                event.session_id,
            )

    @staticmethod
    def _serialize_messages(messages: list[dict[str, Any]]) -> str:
        """Serialize message snapshot as readable text for LLM."""
        lines = []
        for msg in messages:
            role = "User" if msg.get("role") == "user" else "Assistant"
            content = msg.get("content", "")
            if isinstance(content, str):
                # Truncate individual messages to avoid huge prompts
                lines.append(f"{role}: {content[:2000]}")
            elif isinstance(content, list):
                # Tool result messages — skip or summarize
                parts = []
                for item in content:
                    if isinstance(item, dict):
                        text = item.get("content") or item.get("text") or ""
                        if isinstance(text, str) and text:
                            parts.append(text[:500])
                if parts:
                    lines.append(f"{role}: {' '.join(parts)[:2000]}")
        return "\n\n".join(lines)

    async def _extract_facts(
        self, conversation_text: str
    ) -> list[dict[str, Any]]:
        """Call LLM to extract facts from conversation text."""
        if not self._http:
            return []

        # Truncate conversation to avoid exceeding context
        if len(conversation_text) > 12000:
            conversation_text = conversation_text[:12000] + "\n\n[...truncated...]"

        prompt = _EXTRACT_PROMPT.format(conversation_text=conversation_text)
        headers = build_anthropic_headers(self._settings)

        try:
            response = await self._http.post(
                f"{self._settings.api_base_url}/v1/messages",
                json={
                    "model": self._settings.background_model,
                    "max_tokens": 500,
                    "messages": [{"role": "user", "content": prompt}],
                },
                headers=headers,
                timeout=30,
            )

            if response.status_code != 200:
                logger.warning(
                    "Knowledge extraction LLM call failed: %d",
                    response.status_code,
                )
                return []

            data = response.json()
            # Find the text block — skip thinking blocks if present
            text = ""
            for block in data.get("content", []):
                if block.get("type") == "text":
                    text = block.get("text", "")
                    break
            return parse_llm_json(text)

        except (json.JSONDecodeError, httpx.TimeoutException):
            return []
