"""F047: Actionability classification for facts.

Classifies each fact as actionable-or-not at learn time and persists the
verdict on heart.facts. Replaces the read-time _OBSERVATION_PATTERNS
arms-race in nous/heartbeat/checks.py with a single persisted decision.

Tiered classifier:
  Tier 0 — category/tag hard filters (free, deterministic)
  Tier 1 — action + observation substring heuristics (positive-wins)
  Tier 2 — Haiku LLM disambiguation (only for ambiguous / neither-match cases)
  Default — fail-closed (don't page user on uncertain facts)

This module also OWNS the canonical _OBSERVATION_PATTERNS list. The heartbeat
code imports it from here — inverting the ownership relative to the F034
implementation and eliminating the circular-import risk.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nous.handlers import LLMClient

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pattern lists (single source of truth, imported by nous.heartbeat.checks)
# ---------------------------------------------------------------------------

_ACTION_PATTERNS: tuple[str, ...] = (
    # Explicit TODO/action markers
    "todo",
    "action needed",
    "action required",
    "remind me",
    # "need to X" catches rebase/review/send/etc without enumerating verbs
    "need to ",
    "needs to ",
    # Follow-up / waiting phrasings
    "follow-up on",
    "follow up on",
    "should follow up",
    "must complete",
    "waiting for response",
    "hasn't been done",
    "not yet completed",
    "pending review",
    "pending approval",
)

# Canonical observation patterns. Previously lived in nous.heartbeat.checks.
# Kept as a list (not tuple) for import-compat — existing code iterates it.
_OBSERVATION_PATTERNS: list[str] = [
    # Generic descriptive/rule patterns
    "follows a pattern",
    "in general",
    "typically",
    "the process is",
    "is used for",
    "is designed to",
    "pattern of",
    # Resolved / completed language
    "are resolved",
    "is resolved",
    "has been resolved",
    "were resolved",
    "already completed",
    "no longer pending",
    "should no longer trigger",
    "was fixed",
    "has been done",
    "marked as done",
    "both emails rep",
    # False-alarm / meta-documentation
    "false alarm",
    "false positive",
    "false-alarm",
    "stale fact cleanup",
    "heartbeat repeatedly flags",
    "purely observational",
    "not action items",
    "not an action item",
    # Confirmed receipt / observation
    "confirmed receipt",
    "confirmed that it was",
    "tim confirmed",
    "indicating interest in",
    "has requested information about",
    # Lesson-learned / rule
    "lesson learned",
    "showed that",
    "need both a",
    "tasks need both",
    # Contact info / identity facts
    "email address is",
    "linkedin.com/in/",
    "two email addresses",
    "profile url is",
    # Broader resolved/encoded
    "resolved —",
    "encoded as censors",
    "failure modes encoded",
    "are stale and should no",
    "is a false positive",
    "recurring false alarm",
]


_LLM_CLASSIFIER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "actionable": {
            "type": "boolean",
            "description": "True if this fact describes a pending task requiring action",
        },
        "confidence": {
            "type": "number",
            "minimum": 0.0,
            "maximum": 1.0,
            "description": "Classifier confidence",
        },
        "reason": {
            "type": "string",
            "description": "One-line rationale",
        },
    },
    "required": ["actionable", "confidence", "reason"],
}


class ActionabilityClassifier:
    """Classify fact actionability via tiered heuristics + optional LLM."""

    _HARD_NO_CATEGORIES: frozenset[str] = frozenset({"person", "preference"})
    _HARD_NO_TAGS: frozenset[str] = frozenset({"resolved", "identity"})

    def __init__(
        self,
        llm: LLMClient | None = None,
        model: str = "claude-haiku-4-5-20251001",
        budget_check: Callable[[], bool] | None = None,
        default_when_unknown: bool = False,
    ) -> None:
        self._llm = llm
        self._model = model
        self._budget_check = budget_check
        self._default_when_unknown = default_when_unknown

    async def classify(
        self,
        content: str,
        category: str | None = None,
        tags: list[str] | None = None,
    ) -> tuple[bool, float, str]:
        """Return (actionable, confidence, tier).

        tier is one of:
            hard_filter | heuristic_action | heuristic_observation | llm | default
        """
        # Tier 0 — hard filters
        if category in self._HARD_NO_CATEGORIES:
            return (False, 1.0, "hard_filter")
        if tags and {t.lower() for t in tags} & self._HARD_NO_TAGS:
            return (False, 1.0, "hard_filter")

        lower = content.lower()
        has_action = any(p in lower for p in _ACTION_PATTERNS)
        has_observation = any(p in lower for p in _OBSERVATION_PATTERNS)

        # Tier 1 — unambiguous heuristic match (positive-wins-over-negative)
        if has_action and not has_observation:
            return (True, 0.85, "heuristic_action")
        if has_observation and not has_action:
            return (False, 0.85, "heuristic_observation")

        # Tier 2 — LLM disambiguation
        # Fires only for ambiguous (both match) or neither-match cases.
        if self._llm is not None and (self._budget_check is None or self._budget_check()):
            try:
                return await self._llm_classify(content, category, tags)
            except Exception:
                # WARNING not DEBUG — an LLM outage must surface in ops dashboards.
                logger.warning("F047: LLM classify failed, falling through to default", exc_info=True)

        # Default — fail-closed
        logger.info(
            "F047: classifier defaulted (no LLM or neither heuristic matched) for %r",
            content[:80],
        )
        return (self._default_when_unknown, 0.3, "default")

    async def _llm_classify(
        self,
        content: str,
        category: str | None,
        tags: list[str] | None,
    ) -> tuple[bool, float, str]:
        from nous.handlers import call_background_llm_structured

        prompt = (
            "Classify whether this fact describes a PENDING ACTION requiring "
            "future work, or an OBSERVATION / DESCRIPTION / RESOLVED statement.\n\n"
            f"Content: {content[:500]}\n"
            f"Category: {category or '<none>'}\n"
            f"Tags: {', '.join(tags or []) or '<none>'}\n\n"
            "Return actionable=true only if the fact describes work the user "
            "still needs to do. Observations about the world, descriptions of "
            "resolved state, or identity facts are NOT actionable."
        )

        result = await call_background_llm_structured(
            client=self._llm,  # type: ignore[arg-type]
            model=self._model,
            system_prompt="You classify whether a fact describes a pending action or an observation.",
            user_message=prompt,
            tool_name="classify_actionability",
            tool_description="Classify fact as actionable or not.",
            output_schema=_LLM_CLASSIFIER_SCHEMA,
            max_tokens=200,
        )

        # Validate response shape — missing or None verdict defaults to safe
        # fail-closed rather than silently coercing to False via bool(None).
        if (
            not isinstance(result, dict)
            or "actionable" not in result
            or result["actionable"] is None
        ):
            logger.warning(
                "F047: LLM classifier returned malformed response %r — defaulting",
                result,
            )
            return (self._default_when_unknown, 0.3, "default")

        return (
            bool(result["actionable"]),
            float(result.get("confidence", 0.5)),
            "llm",
        )
