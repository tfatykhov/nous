"""Frame selection engine — pattern-matches cognitive frames from input.

No LLM calls. Pure pattern matching for speed (<10ms).
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from nous.cognitive.schemas import FrameSelection
from nous.config import Settings
from nous.storage.database import Database
from nous.storage.models import Frame

logger = logging.getLogger(__name__)

# Priority tiebreaking: higher number wins
FRAME_PRIORITY: dict[str, int] = {
    "conversation": 1,
    "creative": 2,
    "question": 3,
    "task": 4,
    "debug": 5,
    "decision": 6,
}

# F110 v2 referent rule: a short affirmative reply to an assistant offer is work.
_AFFIRMATIVE_TOKENS = frozenset(
    {"yes", "yep", "yeah", "ok", "okay", "sure", "go", "do", "proceed", "please", "agreed", "confirm"}
)
_OFFER_PHRASES = re.compile(r"\b(?:want me to|shall i|should i|i can|would you like)\b")
_OFFER_TAIL_CHARS = 200
_REFERENT_FRAME_ID = "task"
_REFERENT_CONFIDENCE = 0.6
# A one-hit keyword frame the referent rule may override (a zero-hit reply always may).
_REFERENT_OVERRIDABLE = frozenset({"task", "conversation"})


def _is_affirmative_led(text: str) -> bool:
    """True when the reply's first word, case-insensitive with punctuation stripped, is an affirmative.

    The words are ``yes yep yeah ok okay sure go do proceed please agreed confirm``; a leading
    ``do it`` or ``go ahead`` is covered by ``do`` and ``go``.
    """
    tokens = re.sub(r"[^\w\s]", " ", text.lower()).split()
    return bool(tokens) and tokens[0] in _AFFIRMATIVE_TOKENS


def _offers_action(assistant_tail: str) -> bool:
    """True when the assistant text's last 200 characters (trailing whitespace dropped) offer something.

    An offer is a ``?`` or one of the whole-word phrases ``want me to``, ``shall i``, ``should i``,
    ``i can``, ``would you like``, case-insensitive.
    """
    tail = assistant_tail.rstrip()[-_OFFER_TAIL_CHARS:].lower()
    return "?" in tail or _OFFER_PHRASES.search(tail) is not None


class FrameEngine:
    """Selects cognitive frame for each interaction."""

    def __init__(self, database: Database, settings: Settings) -> None:
        self._db = database
        self._settings = settings
        self._default_frame_id = "conversation"

    async def select(
        self,
        agent_id: str,
        input_text: str,
        session: AsyncSession | None = None,
        *,
        carried: Any = None,
        last_assistant_text: str | None = None,
    ) -> FrameSelection:
        """Select the best cognitive frame for this input.

        Algorithm:
        1. Tokenize input into lowercase words
        2. For each frame, count activation_pattern matches
           - Multi-word patterns: substring match on full input
           - Single-word patterns: set membership on tokenized words
        3. Referent rule (F110, only with ``frame_context_enabled`` and
           ``last_assistant_text``): a short, affirmative-led reply to an
           assistant offer selects ``task`` (match_method ``referent``),
           unless the reply has ``frame_switch_min_hits`` hits or its one
           best frame is neither task nor conversation
        4. Frame with most matches wins (ties broken by priority)
        5. If no matches, return default frame (conversation)

        ``carried`` is accepted and ignored (the frame carry rule comes later).
        No LLM call -- pure pattern matching for speed.
        """
        if session is None:
            async with self._db.session() as session:
                return await self._select(
                    agent_id, input_text, session, carried=carried, last_assistant_text=last_assistant_text
                )
        return await self._select(
            agent_id, input_text, session, carried=carried, last_assistant_text=last_assistant_text
        )

    async def _select(
        self,
        agent_id: str,
        input_text: str,
        session: AsyncSession,
        *,
        carried: Any = None,
        last_assistant_text: str | None = None,
    ) -> FrameSelection:
        frames = await self._load_frames(agent_id, session)
        if not frames:
            return self._default_selection()

        # Eval finding 2026-04-30: punctuation-attached tokens like
        # "broken." were missing single-word pattern matches because
        # `set(text.split())` keeps the trailing period. Strip
        # non-word non-space chars before tokenizing so punctuation
        # never breaks matching.
        import re as _re
        input_lower = input_text.lower()
        input_clean = _re.sub(r"[^\w\s]", " ", input_lower)
        input_words = set(input_clean.split())

        best_frame: Frame | None = None
        best_count = 0
        best_priority = -1

        for frame in frames:
            patterns = frame.activation_patterns or []
            match_count = 0

            for pattern in patterns:
                pattern_lower = pattern.lower()
                # P2-1 + 2026-04-30 fix: multi-word patterns use
                # substring match (against the cleaned input so
                # punctuation between pattern words doesn't block
                # the match), and they count as 2 matches each. The
                # original priority-tiebreak biased against multi-word
                # patterns ("what if" + creative lost to bare "what" +
                # question). Weighting two-word patterns as 2 hits
                # keeps higher-specificity patterns winning.
                if " " in pattern_lower:
                    if pattern_lower in input_clean:
                        match_count += 2
                else:
                    if pattern_lower in input_words:
                        match_count += 1

            if match_count > 0:
                priority = FRAME_PRIORITY.get(frame.id, 0)
                if (match_count > best_count) or (match_count == best_count and priority > best_priority):
                    best_frame = frame
                    best_count = match_count
                    best_priority = priority

        referent = self._referent_frame(frames, input_text, best_frame, best_count, last_assistant_text)
        if referent is not None:
            await self._increment_usage(referent, session)
            return self._frame_to_selection(referent, "referent", _REFERENT_CONFIDENCE)

        if best_frame is None:
            # No matches -- use default frame
            default = next((f for f in frames if f.id == self._default_frame_id), None)
            if default is not None:
                await self._increment_usage(default, session)
                return self._frame_to_selection(default, "default", 0.0)
            return self._default_selection()

        confidence = min(1.0, best_count * 0.3)
        await self._increment_usage(best_frame, session)
        return self._frame_to_selection(best_frame, "pattern", confidence)

    async def get(
        self,
        frame_id: str,
        agent_id: str,
        session: AsyncSession | None = None,
    ) -> FrameSelection:
        """Fetch a specific frame by ID."""
        if session is None:
            async with self._db.session() as session:
                return await self._get(frame_id, agent_id, session)
        return await self._get(frame_id, agent_id, session)

    async def _get(
        self,
        frame_id: str,
        agent_id: str,
        session: AsyncSession,
    ) -> FrameSelection:
        result = await session.execute(select(Frame).where(Frame.id == frame_id, Frame.agent_id == agent_id))
        frame = result.scalars().first()
        if frame is None:
            raise ValueError(f"Frame '{frame_id}' not found for agent '{agent_id}'")
        return self._frame_to_selection(frame, "direct", 1.0)

    async def list_frames(
        self,
        agent_id: str,
        session: AsyncSession | None = None,
    ) -> list[FrameSelection]:
        """List all frames for an agent."""
        if session is None:
            async with self._db.session() as session:
                return await self._list_frames(agent_id, session)
        return await self._list_frames(agent_id, session)

    async def _list_frames(
        self,
        agent_id: str,
        session: AsyncSession,
    ) -> list[FrameSelection]:
        frames = await self._load_frames(agent_id, session)
        return [self._frame_to_selection(f, "list", 0.0) for f in frames]

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _referent_frame(
        self,
        frames: list[Frame],
        input_text: str,
        best_frame: Frame | None,
        best_count: int,
        last_assistant_text: str | None,
    ) -> Frame | None:
        """The task frame when the reply accepts the assistant's offer, else None (today's path).

        Rule 1 comes first: ``frame_switch_min_hits`` keyword hits keep the keyword frame. A
        reply is short at ``frame_short_reply_max_words`` whitespace-separated words or fewer.
        Fail-open: an error here returns None.
        """
        if not (last_assistant_text and self._settings.frame_context_enabled):
            return None
        try:
            if best_count >= self._settings.frame_switch_min_hits:
                return None
            if best_frame is not None and best_frame.id not in _REFERENT_OVERRIDABLE:
                return None
            if len(input_text.split()) > self._settings.frame_short_reply_max_words:
                return None
            if not (_is_affirmative_led(input_text) and _offers_action(last_assistant_text)):
                return None
            return next((f for f in frames if f.id == _REFERENT_FRAME_ID), None)
        except Exception:
            logger.warning("Frame referent rule failed; using keyword selection", exc_info=True)
            return None

    async def _load_frames(self, agent_id: str, session: AsyncSession) -> list[Frame]:
        result = await session.execute(
            select(Frame).where(Frame.agent_id == agent_id, Frame.active == True)  # noqa: E712
        )
        return list(result.scalars().all())

    async def _increment_usage(self, frame: Frame, session: AsyncSession) -> None:
        frame.usage_count = (frame.usage_count or 0) + 1
        frame.last_used = datetime.now(UTC)
        await session.flush()

    def _frame_to_selection(self, frame: Frame, match_method: str, confidence: float) -> FrameSelection:
        return FrameSelection(
            frame_id=frame.id,
            frame_name=frame.name,
            confidence=confidence,
            match_method=match_method,
            description=frame.description,
            default_category=frame.default_category,
            default_stakes=frame.default_stakes,
            questions_to_ask=frame.questions_to_ask or [],
        )

    def _default_selection(self) -> FrameSelection:
        return FrameSelection(
            frame_id="conversation",
            frame_name="Conversation",
            confidence=0.0,
            match_method="default",
            description="Casual or social interaction",
            default_category="process",
            default_stakes="low",
        )
