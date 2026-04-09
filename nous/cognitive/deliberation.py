"""Deliberation engine — manages the deliberation lifecycle for a turn.

Thin wrapper around Brain.record(), Brain.think(), Brain.update()
providing a clean start -> think -> finalize flow.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select as sa_select
from sqlalchemy.ext.asyncio import AsyncSession

from nous.brain.brain import Brain
from nous.brain.schemas import ReasonInput, RecordInput
from nous.cognitive.schemas import FrameSelection
from nous.storage.models import Decision as DecisionModel

logger = logging.getLogger(__name__)

# Frames that trigger deliberation (D8, 009.5: removed task)
_DELIBERATION_FRAMES = {"decision", "debug"}

# 009.5: Dedup constants
_DEDUP_WINDOW_MINUTES = 5
_DEDUP_EMBEDDING_THRESHOLD = 0.85
_DEDUP_KEYWORD_THRESHOLD = 0.5

# 009.5: Chat pattern prefixes that indicate noise
_QUALITY_CHAT_PREFIXES = (
    "done",
    "on it",
    "here's",
    "got it",
    "sure",
    "okay",
    "alright",
    "working on",
    "let me",
    "i'll",
)


def _validate_decision_quality(
    description: str,
    confidence: float,
    stakes: str,
    *,
    has_tool_errors: bool = False,
) -> str | None:
    """Validate decision quality (009.5). Returns rejection reason or None if valid."""
    desc_stripped = description.strip()

    # Rule 1: Description too short
    if len(desc_stripped) < 20:
        return f"Description too short ({len(desc_stripped)} chars < 20)"

    # Rule 2: Default confidence + high stakes (never deliberated)
    # Skip if tool errors caused the 0.5 — that's a real decision with a transient failure
    if confidence == 0.5 and stakes in ("high", "critical") and not has_tool_errors:
        return f"Default confidence (0.5) with {stakes} stakes"

    # Rule 3: Chat pattern prefix
    desc_lower = desc_stripped.lower()
    for prefix in _QUALITY_CHAT_PREFIXES:
        if desc_lower.startswith(prefix):
            return f"Description starts with chat pattern: '{prefix}'"

    # Rule 4: Error template
    if "encountered an error processing your request" in desc_lower:
        return "Description is an error message template"

    return None


class DeliberationEngine:
    """Manages the deliberation lifecycle for a single turn.

    Wraps Brain.record(), Brain.think(), Brain.update() into
    a clean start -> think -> finalize flow.
    """

    def __init__(self, brain: Brain) -> None:
        self._brain = brain

    async def start(
        self,
        agent_id: str,
        description: str,
        frame: FrameSelection,
        session_id: str | None = None,
        session: AsyncSession | None = None,
    ) -> str | None:
        """Begin deliberation -- with dedup check (009.5).

        Returns decision_id as string, or None if duplicate detected.

        Creates a decision via Brain.record() with:
        - description: "Plan: {description}"
        - confidence: 0.5 (to be updated in finalize)
        - category: frame.default_category or "process"
        - stakes: frame.default_stakes or "low"
        - tags: [frame.frame_id]
        - reasons: at least 1 ReasonInput (required by guardrail)

        P1-2: Constructs RecordInput pydantic model.
        P2-6: Returns str(uuid) for TurnContext storage.
        """
        # 009.5: Dedup check before creating decision
        if session_id and await self._is_duplicate(
            agent_id,
            description,
            session_id,
            session=session,
        ):
            logger.debug("Skipping duplicate deliberation: %s", description[:80])
            return None

        # P1-2: Build RecordInput with at least 1 reason
        record_input = RecordInput(
            description=f"Plan: {description}",
            confidence=0.5,
            category=frame.default_category or "process",
            stakes=frame.default_stakes or "low",
            tags=[frame.frame_id],
            reasons=[
                ReasonInput(
                    type="analysis",
                    text=f"Frame '{frame.frame_name}' triggered deliberation for: {description[:100]}",
                )
            ],
            session_id=session_id,
        )

        detail = await self._brain.record(record_input, session=session)
        # P2-6: Return as string for TurnContext.decision_id
        return str(detail.id)

    async def think(
        self,
        decision_id: str,
        thought: str,
        agent_id: str,
        session: AsyncSession | None = None,
    ) -> None:
        """Capture a micro-thought during deliberation.

        P2-6: Converts str decision_id to UUID for Brain.think().
        """
        await self._brain.think(UUID(decision_id), thought, session=session)

    async def finalize(
        self,
        decision_id: str,
        description: str,
        confidence: float,
        context: str | None = None,
        pattern: str | None = None,
        tags: list[str] | None = None,
        has_tool_errors: bool = False,
        session: AsyncSession | None = None,
    ) -> str | None:
        """Update decision with final outcome. Returns decision_id or None if rejected (009.5).

        009.5: Validates quality before persisting. Deletes on rejection.
        """
        # 009.5: Fetch stakes from existing decision for quality gate
        detail = await self._brain.get(UUID(decision_id), session=session)
        if detail is None:
            return None

        stakes = detail.stakes

        # 009.5: Quality gate
        rejection = _validate_decision_quality(
            description,
            confidence,
            stakes,
            has_tool_errors=has_tool_errors,
        )
        if rejection:
            logger.info("Quality gate rejected decision %s: %s", decision_id, rejection)
            await self.delete(decision_id, session=session)
            return None

        await self._brain.update(
            decision_id=UUID(decision_id),
            description=description,
            context=context,
            pattern=pattern,
            confidence=confidence,
            tags=tags,
            session=session,
        )
        return decision_id

    async def delete(
        self,
        decision_id: str,
        session: AsyncSession | None = None,
    ) -> None:
        """Delete a deliberation record for non-decisions.

        Informational responses shouldn't create decision records at all.
        Instead of marking as failure (which pollutes Brain with noise),
        remove the record entirely.
        """
        await self._brain.delete(UUID(decision_id), session=session)

    async def should_deliberate(self, frame: FrameSelection) -> bool:
        """Should this frame trigger deliberation?

        Returns True for: decision, debug (D8, 009.5: removed task)
        Returns False for: task, conversation, question, creative
        """
        return frame.frame_id in _DELIBERATION_FRAMES

    # ------------------------------------------------------------------
    # 009.5: Dedup helpers
    # ------------------------------------------------------------------

    async def _is_duplicate(
        self,
        agent_id: str,
        description: str,
        session_id: str,
        session: AsyncSession | None = None,
    ) -> bool:
        """Check if a similar decision was recorded recently (009.5).

        Uses embedding similarity (0.85) with keyword fallback (0.5).
        Session-scoped to prevent cross-session suppression.
        """
        cutoff = datetime.now(UTC) - timedelta(minutes=_DEDUP_WINDOW_MINUTES)
        recent = await self._brain.get_recent_decisions(
            agent_id,
            since=cutoff,
            session_id=session_id,
            session=session,
        )
        if not recent:
            return False

        # Try embedding-based comparison
        if self._brain.embeddings:
            try:
                desc_embedding = await self._brain.embeddings.embed(description)
                had_comparison = False
                for decision in recent:
                    stored = await self._get_decision_embedding(decision.id, session=session)
                    if stored is not None:
                        had_comparison = True
                        sim = self._cosine_similarity(desc_embedding, stored)
                        if sim > _DEDUP_EMBEDDING_THRESHOLD:
                            return True
                # Only skip keyword fallback if we actually compared embeddings
                if had_comparison:
                    return False
                # No stored embeddings found — fall through to keyword overlap
            except Exception:
                logger.debug("Embedding dedup failed, falling back to keyword overlap")

        # Fallback: keyword containment coefficient
        desc_words = set(re.findall(r"\b\w{3,}\b", description.lower()))
        for decision in recent:
            existing_words = set(re.findall(r"\b\w{3,}\b", decision.description.lower()))
            if not desc_words or not existing_words:
                continue
            intersection = desc_words & existing_words
            overlap = len(intersection) / min(len(desc_words), len(existing_words))
            if overlap > _DEDUP_KEYWORD_THRESHOLD:
                return True
        return False

    async def _get_decision_embedding(
        self,
        decision_id: UUID,
        session: AsyncSession | None = None,
    ) -> list[float] | None:
        """Fetch stored embedding for a decision."""
        if session is None:
            async with self._brain.db.session() as session:
                result = await session.execute(
                    sa_select(DecisionModel.embedding).where(DecisionModel.id == decision_id)
                )
                return result.scalar_one_or_none()
        result = await session.execute(sa_select(DecisionModel.embedding).where(DecisionModel.id == decision_id))
        return result.scalar_one_or_none()

    @staticmethod
    def _cosine_similarity(a: list[float], b: list[float]) -> float:
        """Cosine similarity between two vectors."""
        dot = sum(x * y for x, y in zip(a, b))
        norm_a = sum(x * x for x in a) ** 0.5
        norm_b = sum(x * x for x in b) ** 0.5
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return dot / (norm_a * norm_b)
