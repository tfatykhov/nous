"""Intent classification for context retrieval.

Extracts signals from user input using pattern matching (no LLM).
Maps signals to declarative RetrievalPlans that guide ContextEngine.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from nous.cognitive.schemas import FrameSelection
from nous.config import Settings

# 008.6: Episode budget granted when temporal recency signals a recap/follow-up.
_TEMPORAL_RECENCY_EPISODE_BUDGET = 1000


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
    # The part of the input the signals describe when it is not all of it:
    # what follows a greeting. plan_retrieval's F27 fallback queries it.
    text: str | None = None


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
    # Conditional mode: the keyword bag, for a keyword gate that needs every
    # word to match (the Session Profile leg), so the natural-language
    # ``query_text`` adds no mandatory words to it. None: gate on query_text.
    keyword_text: str | None = None


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

# Patterns for memory type hints. Eval finding 2026-04-30: bare
# `\bdecid\b` doesn't match "decide" (no word boundary mid-word) and
# `\bhow (do|to|can)\b` misses past/conditional ("how did", "how could").
# Use stem patterns (`\bdecid\w*\b`) and broaden the verb set to capture
# the natural-language variants users actually produce.
_MEMORY_HINTS = {
    "decision": [
        # 'deci' stem catches both 'decide' (deci-d-) and 'decision' (deci-s-)
        # plus 'decided', 'deciding', 'decisive'. Use enumeration rather
        # than `\bdeci\w*\b` to avoid false matches on 'decimal'/'decimate'.
        r"\b(?:decide\w*|decision\w*|decisive\w*|deciding|decided)\b",
        r"\b(?:chose|choice|choose|chosen|recommend\w*)\b",
        r"\bshould\s+(?:we|i|they|you)\b",
    ],
    "fact": [
        r"\b(?:what\s+is|tell\s+me\s+about|fact|know\s+about|definition)\b",
        r"\bdefin\w*\b",
    ],
    "procedure": [
        r"\bhow\s+(?:do|did|to|can|could|should|would)\b",
        r"\b(?:steps|process|workflow|guide|procedure)\b",
    ],
    "episode": [
        r"\blast\s+time\b",
        r"\bwhen\s+(?:did|was|were)\b",
        r"\b(?:history|story|what\s+happened|recall|earlier)\b",
    ],
}


# The words an acknowledgement is made of, for conditional mode.
_ACK_WORDS = frozenset(
    "ok okay k kk yes yeah yep yup no nope nah sure thanks thank you thx ty cheers cool great nice good"
    " perfect awesome fine alright got it sounds noted lol haha hmm np there all everyone very much so".split()
)


def _is_short_input(signals: IntentSignals, text: str | None = None) -> bool:
    """F20: no question, no memory hint, no topic keyword and no strong recency:
    a short acknowledgement ("ok", "yes", "sure") that needs no retrieval.

    Conditional mode passes the input ``text`` instead. Then only an empty
    input, or one whose every word is an acknowledgement word (emoji and
    punctuation are not words), is short, so an imperative without a keyword
    ("fix the cron job") still retrieves.
    """
    if text is not None:
        return all(w in _ACK_WORDS for w in re.findall(r"\w+", text.lower()))
    return (
        not signals.is_question
        and not signals.memory_type_hints
        and not signals.topic_keywords
        and signals.temporal_recency <= 0.5
    )


# Lines a pre-turn prompt builder writes just before the task text. Each of
# these builders writes its marker before any text it did not write itself
# (result rows, findings, node results, the check's own prompt), so the
# EARLIEST marker line in the input is the builder's.
_PREFIX_MARKER = re.compile(
    r"^(?:## Why this work exists"  # continuation turn (handlers/continuation_runner.py)
    r"|=== DAG outcome ==="  # DAG delivery summary (dag/delivery.py)
    r"|\[Heartbeat\] The following items need attention:"  # heartbeat triage (heartbeat/runner.py)
    r"|Instructions:)",  # dynamic check and its callback (heartbeat/dynamic.py, heartbeat/runner.py)
    re.MULTILINE,
)
# A DAG node with context_flow inputs (dag/orchestrator.py) is the other way
# round: its predecessors' results come first and its own "## Instructions"
# LAST, so a result holding such a line must not win. A DAG check node wraps
# that text in a dynamic check, after "Instructions: ".
_DAG_CONTEXT = re.compile(r"^(?:Instructions: )?## Context from prior steps", re.MULTILINE)
_DAG_INSTRUCTIONS = re.compile(r"^## Instructions", re.MULTILINE)
_QUESTION_TAIL_CHARS = 600
# A sentence ending in "?" followed by whitespace or the end ("a?b=1" is a URL).
_QUESTION = re.compile(r"[^.!?\n]+\?(?=\s|$)")
_PARAGRAPH_BREAK = re.compile(r"\n\s*\n")


def _cap(text: str, limit: int) -> str:
    """``text`` cut to at most ``limit`` chars at a word boundary."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    if text[limit] != " " and " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip()


def _first_paragraph(text: str) -> str:
    return " ".join(_PARAGRAPH_BREAK.split(text.strip(), maxsplit=1)[0].split())


def extract_query(input_text: str, short_chars: int = 300) -> str:
    """The retrieval query for ``input_text`` in conditional mode. No LLM.

    An input of at most ``short_chars`` (after whitespace normalisation) is its
    own query. A longer one is a prompt, usually templated, and its query is,
    in order:

    (a) the paragraph after the builder's task marker: the last
        "## Instructions" of a DAG node with prior-step context, else the
        earliest marker of ``_PREFIX_MARKER``;
    (b) else the last sentence ending in "?" within the last 600 chars;
    (c) else the first paragraph.

    Each is whitespace-normalised and cut to ``short_chars`` at a word boundary.
    (a) stops at the paragraph end because what follows a marker is often more
    template (a dynamic check's JSON reply format).
    """
    text = " ".join(input_text.split())
    if len(text) <= short_chars:
        return text
    prefix = _PREFIX_MARKER.search(input_text)
    dag = _DAG_CONTEXT.search(input_text)
    markers = []
    if dag and (prefix is None or dag.start() <= prefix.start()):
        markers += list(_DAG_INSTRUCTIONS.finditer(input_text, dag.end()))[-1:]  # the node's own: the last
    if prefix:
        markers.append(prefix)
    for found in markers:
        task = _first_paragraph(input_text[found.end() :])
        if task:
            return _cap(task, short_chars)
    questions = [q for q in _QUESTION.findall(input_text[-_QUESTION_TAIL_CHARS:]) if re.search(r"\w", q)]
    if questions:
        return _cap(" ".join(questions[-1].split()), short_chars)
    return _cap(_first_paragraph(input_text), short_chars)


class IntentClassifier:
    """Extract intent signals from user input. No LLM -- pattern matching only."""

    def __init__(self, settings: Settings | None = None):
        self._settings = settings or Settings()
        self._conditional = self._settings.context_query_mode == "conditional"

    # F17: Extended greeting patterns with "good afternoon", "good night", "howdy", "greetings"
    _GREETING_PATTERNS = re.compile(
        r"^(hey|hi|hello|sup|yo|good morning|good afternoon|good evening|good night"
        r"|howdy|greetings|what'?s up)\b",
        re.IGNORECASE,
    )
    # Every greeting at the start of a turn, with the non-word characters after each.
    _GREETING_RUN = re.compile(r"(?:" + _GREETING_PATTERNS.pattern.removeprefix("^") + r"\W*)+", re.IGNORECASE)

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
        greeting = self._GREETING_PATTERNS.match(stripped)
        if greeting and self._settings.followup_greeting_request_detection_enabled:
            # What follows the greeting is judged as any turn is (F20): the turn
            # is a greeting only if that carries no request. Either way it is
            # classified, and planned, on what follows -- its own signals and
            # text -- so a greeting never changes how a request is planned.
            rest_text = stripped[self._GREETING_RUN.match(stripped).end():]  # "hi hey, ...": every greeting
            rest = self.classify(rest_text, frame)
            rest.is_greeting = _is_short_input(rest, rest_text if self._conditional else None)
            rest.text = rest_text
            return rest
        signals.is_greeting = bool(greeting)

        # Question detection (F18: expanded starters)
        signals.is_question = stripped.endswith("?") or bool(
            self._QUESTION_STARTERS.match(stripped)
        )

        # Temporal recency
        for pattern, weight in _RECENCY_PATTERNS:
            if re.search(pattern, input_text, re.IGNORECASE):
                signals.temporal_recency = max(signals.temporal_recency, weight)

        # Memory type hints
        for mem_type, patterns in _MEMORY_HINTS.items():
            for pattern in patterns:
                if re.search(pattern, input_text, re.IGNORECASE):
                    signals.memory_type_hints[mem_type] = (
                        signals.memory_type_hints.get(mem_type, 0) + 0.5
                    )

        # Topic keywords: capitalized words + long words + ALL-CAPS acronyms (F19)
        #
        # N10 (2026-08-02): dedup via ``dict.fromkeys``, NOT ``set``. These
        # keywords are joined into ``query_text`` at plan_retrieval (:199-201)
        # and become the embedded retrieval query for every memory type, so
        # their ORDER is part of the query string. ``set`` iteration order for
        # str depends on PYTHONHASHSEED, which CPython randomises per process:
        # measured 5 distinct orderings of one sentence across 5 seeds. That
        # made identical user input retrieve differently on different
        # processes, made the ``[:10]`` cap drop a DIFFERENT keyword per run
        # once a message yielded more than ten, and fragmented the F050
        # expansion cache (keyed on a hash of this text).
        #
        # ``dict.fromkeys`` is order-preserving and deterministic, so it also
        # restores source word order — which matters because embeddings are
        # order-sensitive. Whether a keyword bag is the right query
        # representation AT ALL is a separate open question; this is only the
        # determinism fix.
        words = re.findall(
            r"\b[A-Z][a-z]+\b|\b\w{6,}\b|\b[A-Z]{2,}\b", input_text
        )
        signals.topic_keywords = list(dict.fromkeys(w.lower() for w in words))[:10]

        # Entity mentions (proper nouns -- capitalized words not at sentence start)
        # Same N10 treatment. No retrieval consumer reads this today (only
        # layer.py:670 copies it onto a rebuilt IntentSignals), so this is
        # consistency rather than a measured fix — but leaving one of two
        # adjacent dedups hash-order-dependent is how the next reader
        # concludes the ordering is deliberate.
        entity_candidates = re.findall(r"(?<!^)(?<!\. )\b[A-Z][a-z]+\b", input_text)
        signals.entity_mentions = list(dict.fromkeys(entity_candidates))[:10]

        return signals

    def plan_retrieval(
        self, signals: IntentSignals, input_text: str = ""
    ) -> RetrievalPlan:
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
        # Conditional mode skips only an empty or acknowledgement input.
        if _is_short_input(signals, (signals.text or input_text) if self._conditional else None):
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
        # F27: Fall back to original input_text, not empty string -- or to the part
        # of it the signals describe, when that is not all of it
        bag_text = " ".join(signals.topic_keywords) if signals.topic_keywords else signals.text or input_text
        keyword_text = None
        if self._conditional:
            # The user's words, or the task part of a long prompt; the bag stays
            # the lexical anchor of an all-words keyword gate.
            query_text = extract_query(signals.text or input_text, self._settings.context_query_short_chars)
            keyword_text = bag_text
        else:
            query_text = bag_text

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
                        keyword_text=keyword_text,
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
                        keyword_text=keyword_text,
                    )
                )

        # Frame-based budget overrides
        if signals.frame_type == "conversation":
            ep_budget = 600 if self._settings.followup_episode_budget_enabled else 0
            plan.budget_overrides = {
                "decisions": 500,
                "facts": 500,
                "procedures": 0,
                "episodes": ep_budget,
            }
        elif signals.frame_type == "decision":
            plan.budget_overrides = {"decisions": 3500, "procedures": 2000}

        # 008.6: Temporal recency boost — ensure episodes are retrieved
        if signals.temporal_recency > 0.5:
            current_ep_budget = plan.budget_overrides.get("episodes", None)
            if (
                current_ep_budget is not None
                and current_ep_budget < _TEMPORAL_RECENCY_EPISODE_BUDGET
            ):
                plan.budget_overrides["episodes"] = _TEMPORAL_RECENCY_EPISODE_BUDGET
            # Boost episode query limit
            for q in plan.queries:
                if q.memory_type == "episode":
                    q.limit = max(q.limit, 8)

        return plan
