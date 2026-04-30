"""Frame selection engine — pattern-matches cognitive frames from input.

No LLM calls. Pure pattern matching for speed (<10ms).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

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
    ) -> FrameSelection:
        """Select the best cognitive frame for this input.

        Algorithm:
        1. Tokenize input into lowercase words
        2. For each frame, count activation_pattern matches
           - Multi-word patterns: substring match on full input
           - Single-word patterns: set membership on tokenized words
        3. Frame with most matches wins (ties broken by priority)
        4. If no matches, return default frame (conversation)

        No LLM call -- pure pattern matching for speed.
        """
        if session is None:
            async with self._db.session() as session:
                return await self._select(agent_id, input_text, session)
        return await self._select(agent_id, input_text, session)

    async def _select(
        self,
        agent_id: str,
        input_text: str,
        session: AsyncSession,
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
