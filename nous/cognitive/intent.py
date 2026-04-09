"""Intent classification for context retrieval.

Extracts signals from user input using pattern matching (no LLM).
Maps signals to declarative RetrievalPlans that guide ContextEngine.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from nous.cognitive.schemas import FrameSelection


@dataclass
class IntentSignals:
    """Extracted signals from user input."""

    frame_type: str
    entity_mentions: list[str] = field(default_factory=list)
    temporal_recency: float = 0.0  # 0=no signal, 1=strong recency
    memory_type_hints: dict[str, float] = field(default_factory=dict)
    is_question: bool = False
    is_greeting: bool = False
    topic_keywords: list[str] = field(default_factory=list)


@dataclass
class RetrievalQuery:
    """Single retrieval operation.

    Note: recency_weight removed (F7) -- Brain.query() and Heart search
    methods have no recency parameter. Temporal signals preserved on
    IntentSignals for future post-retrieval re-ranking.
    """

    memory_type: str  # "decision", "fact", "procedure", "episode"
    query_text: str
    limit: int = 5


@dataclass
class RetrievalPlan:
    """Declarative retrieval specification."""

    queries: list[RetrievalQuery] = field(default_factory=list)
    budget_overrides: dict[str, int] = field(default_factory=dict)
    dedup_against_conversation: bool = True
    max_results_per_type: dict[str, int] | None = None
    skip_types: set[str] = field(default_factory=set)


# Patterns for temporal signals
_RECENCY_PATTERNS = [
    (r"\b(today|just now|right now|currently)\b", 1.0),
    (r"\b(yesterday|recently|this week)\b", 0.8),
    (r"\b(last week|few days ago)\b", 0.5),
    (r"\b(last month|a while ago)\b", 0.3),
]

# Patterns for memory type hints
_MEMORY_HINTS = {
    "decision": [r"\b(decid|decision|chose|choice|should we|recommend)\b"],
    "fact": [r"\b(what is|tell me about|fact|know about|definition)\b"],
    "procedure": [r"\b(how (do|to|can)|steps|process|workflow|guide)\b"],
    "episode": [r"\b(last time|when did|history|story|what happened)\b"],
}


class IntentClassifier:
    """Extract intent signals from user input. No LLM -- pattern matching only."""

    # F17: Extended greeting patterns with "good afternoon", "good night", "howdy", "greetings"
    _GREETING_PATTERNS = re.compile(
        r"^(hey|hi|hello|sup|yo|good morning|good afternoon|good evening|good night"
        r"|howdy|greetings|what'?s up)\b",
        re.IGNORECASE,
    )

    # F18: Extended question starters with did|will|would|could|has|have|was|were|might
    _QUESTION_STARTERS = re.compile(
        r"^(what|where|when|why|how|who|which|can|should|is|are|do|does"
        r"|did|will|would|could|has|have|was|were|might)\b",
        re.IGNORECASE,
    )

    def classify(self, input_text: str, frame: FrameSelection) -> IntentSignals:
        """Classify input into retrieval-relevant signals."""
        signals = IntentSignals(frame_type=frame.frame_id)
        stripped = input_text.strip()

        # Greeting detection
        signals.is_greeting = bool(self._GREETING_PATTERNS.match(stripped))

        # Question detection (F18: expanded starters)
        signals.is_question = stripped.endswith("?") or bool(self._QUESTION_STARTERS.match(stripped))

        # Temporal recency
        for pattern, weight in _RECENCY_PATTERNS:
            if re.search(pattern, input_text, re.IGNORECASE):
                signals.temporal_recency = max(signals.temporal_recency, weight)

        # Memory type hints
        for mem_type, patterns in _MEMORY_HINTS.items():
            for pattern in patterns:
                if re.search(pattern, input_text, re.IGNORECASE):
                    signals.memory_type_hints[mem_type] = signals.memory_type_hints.get(mem_type, 0) + 0.5

        # Topic keywords: capitalized words + long words + ALL-CAPS acronyms (F19)
        words = re.findall(r"\b[A-Z][a-z]+\b|\b\w{6,}\b|\b[A-Z]{2,}\b", input_text)
        signals.topic_keywords = list(set(w.lower() for w in words))[:10]

        # Entity mentions (proper nouns -- capitalized words not at sentence start)
        entity_candidates = re.findall(r"(?<!^)(?<!\. )\b[A-Z][a-z]+\b", input_text)
        signals.entity_mentions = list(set(entity_candidates))[:10]

        return signals

    def plan_retrieval(self, signals: IntentSignals, input_text: str = "") -> RetrievalPlan:
        """Map intent signals to a retrieval plan.

        Args:
            signals: Extracted intent signals from classify().
            input_text: Original user input for query_text fallback (F27).
        """

        # Greetings: minimal retrieval
        if signals.is_greeting:
            return RetrievalPlan(
                queries=[],
                skip_types={"decision", "fact", "procedure", "episode"},
                budget_overrides={
                    "decisions": 0,
                    "facts": 0,
                    "procedures": 0,
                    "episodes": 0,
                },
            )

        # F20: Short non-question input short-circuit
        # Inputs with no extractable keywords, no memory hints, and not a question
        # are likely short acknowledgements ("ok", "yes", "thanks") -- skip retrieval
        # 008.6: Don't skip if temporal recency is high (recap queries)
        if (
            not signals.is_question
            and not signals.memory_type_hints
            and not signals.topic_keywords
            and signals.temporal_recency <= 0.5
        ):
            return RetrievalPlan(
                queries=[],
                skip_types={"decision", "fact", "procedure", "episode"},
                budget_overrides={
                    "decisions": 0,
                    "facts": 0,
                    "procedures": 0,
                    "episodes": 0,
                },
            )

        plan = RetrievalPlan()
        # F27: Fall back to original input_text, not empty string
        query_text = " ".join(signals.topic_keywords) if signals.topic_keywords else input_text

        # If strong memory type hints, bias toward those types
        if signals.memory_type_hints:
            dominant = max(signals.memory_type_hints, key=signals.memory_type_hints.get)
            for mem_type in ["decision", "fact", "procedure", "episode"]:
                limit = 8 if mem_type == dominant else 3
                plan.queries.append(
                    RetrievalQuery(
                        memory_type=mem_type,
                        query_text=query_text,
                        limit=limit,
                    )
                )
        else:
            # Default: uniform retrieval
            for mem_type in ["decision", "fact", "procedure", "episode"]:
                plan.queries.append(
                    RetrievalQuery(
                        memory_type=mem_type,
                        query_text=query_text,
                        limit=5,
                    )
                )

        # Frame-based budget overrides
        if signals.frame_type == "conversation":
            plan.budget_overrides = {
                "decisions": 500,
                "facts": 500,
                "procedures": 0,
                "episodes": 0,
            }
        elif signals.frame_type == "decision":
            plan.budget_overrides = {"decisions": 3500, "procedures": 2000}

        # 008.6: Temporal recency boost — ensure episodes are retrieved
        if signals.temporal_recency > 0.5:
            current_ep_budget = plan.budget_overrides.get("episodes", None)
            if current_ep_budget is not None and current_ep_budget == 0:
                plan.budget_overrides["episodes"] = 1000
            # Boost episode query limit
            for q in plan.queries:
                if q.memory_type == "episode":
                    q.limit = max(q.limit, 8)

        return plan
