"""F024 Phase 3b — Outcome Signal Detector.

Listens to: session_ended
Emits: outcome_signals_detected

Classifies episode outcomes using LLM analysis of the episode summary
and transcript. Stores structured outcome signals for rubric evolution.
"""

from __future__ import annotations

import json
import logging
from uuid import UUID

from nous.config import Settings
from nous.events import Event, EventBus
from nous.handlers import LLMClient, call_background_llm, parse_llm_json
from nous.storage.database import Database
from nous.storage.models import OutcomeSignal

logger = logging.getLogger(__name__)

_OUTCOME_PROMPT = """\
You are analyzing an AI conversation episode to detect outcome signals.

Episode summary:
{summary}

Transcript excerpt (last 2000 chars):
{transcript_tail}

Classify which outcome signals apply. Return ALL that apply:
- "corrected": User corrected the AI's response ("no, actually...", "that's wrong", explicit correction)
- "completed": Task was finished without rework or corrections
- "praised": User gave explicit positive feedback ("good job", "perfect", "thanks, that's exactly right")
- "reworked": User asked the AI to redo or significantly revise its work
- "self_corrected": AI caught and fixed its own error mid-conversation

Return ONLY valid JSON:
{{"signals": [
    {{"type": "<signal_type>", "confidence": <0.0-1.0>, "evidence": "<brief quote or description>"}}
]}}

If no clear signals detected, return: {{"signals": []}}"""


class OutcomeDetector:
    """Detects outcome signals from episode summaries."""

    def __init__(
        self,
        db: Database,
        settings: Settings,
        bus: EventBus,
        llm_client: LLMClient | None,
        agent_id: str,
    ) -> None:
        self._db = db
        self._settings = settings
        self._bus = bus
        self._llm = llm_client
        self._agent_id = agent_id
        bus.on("session_ended", self.handle)

    async def handle(self, event: Event) -> None:
        """Handle session_ended — detect and store outcome signals."""
        if not self._settings.rubric_outcome_detection_enabled:
            return

        episode_id = event.data.get("episode_id")
        if not episode_id:
            return

        transcript = event.data.get("transcript", "")
        if not transcript or len(transcript) < 50:
            return

        summary = event.data.get("summary", {})
        if not summary:
            summary = {"transcript_length": len(transcript)}

        try:
            signals = await self._detect_signals(summary, transcript)
            if not signals:
                return

            async with self._db.session() as session:
                for sig in signals:
                    signal_type = sig.get("type", "")
                    valid_types = {"corrected", "completed", "praised", "reworked", "self_corrected"}
                    if signal_type not in valid_types:
                        continue

                    obj = OutcomeSignal(
                        agent_id=self._agent_id,
                        episode_id=UUID(episode_id),
                        signal_type=signal_type,
                        confidence=max(0.0, min(1.0, float(sig.get("confidence", 0.5)))),
                        evidence=sig.get("evidence", ""),
                        self_improvement_scores=summary.get("scores"),
                    )
                    session.add(obj)
                await session.commit()

            logger.info(
                "F024-3b: Detected %d outcome signals for episode %s: %s",
                len(signals),
                episode_id,
                [s.get("type") for s in signals],
            )

            await self._bus.emit(
                Event(
                    type="outcome_signals_detected",
                    agent_id=event.agent_id,
                    session_id=event.session_id,
                    data={
                        "episode_id": episode_id,
                        "signals": signals,
                    },
                    trace_id=event.trace_id,  # F035.2: inherit from parent
                    caused_by=event.event_id,  # F035.2: point to parent
                )
            )

        except Exception:
            logger.exception("F024-3b: Failed to detect outcome signals for episode %s", episode_id)

    async def _detect_signals(self, summary: dict, transcript: str) -> list[dict]:
        """Use LLM to classify outcome signals from episode data."""
        if not self._llm:
            return self._detect_heuristic(summary)

        prompt = _OUTCOME_PROMPT.format(
            summary=json.dumps(summary, indent=2)[:2000],
            transcript_tail=transcript[-2000:] if transcript else "(no transcript)",
        )

        try:
            raw = await call_background_llm(
                self._llm,
                self._settings.rubric_outcome_model,
                "You are an outcome signal classifier. Respond only with JSON.",
                prompt,
                max_tokens=512,
            )
            if not raw:
                return self._detect_heuristic(summary)

            parsed = parse_llm_json(raw)
            return parsed.get("signals", []) if parsed else []

        except Exception:
            logger.warning("F024-3b: LLM outcome detection failed, falling back to heuristic")
            return self._detect_heuristic(summary)

    @staticmethod
    def _detect_heuristic(summary: dict) -> list[dict]:
        """Fallback heuristic when LLM is unavailable."""
        signals = []
        outcome = summary.get("outcome", "")

        if outcome in ("resolved", "success"):
            signals.append({"type": "completed", "confidence": 0.6, "evidence": f"Episode outcome: {outcome}"})
        elif outcome in ("unresolved", "failure"):
            signals.append({"type": "reworked", "confidence": 0.4, "evidence": f"Episode outcome: {outcome}"})

        return signals
