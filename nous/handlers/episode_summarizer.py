"""Episode Summarizer — generates structured summaries on session end.

Listens to: session_ended
Emits: episode_summarized

Uses a lightweight LLM call to summarize the conversation transcript.
Stores summary as JSONB on the episode record.
"""

from __future__ import annotations

import json
import logging
from typing import Any
from uuid import UUID

from nous.config import Settings
from nous.events import Event, EventBus
from nous.handlers import LLMClient, call_background_llm, parse_llm_json
from nous.brain.brain import Brain
from nous.brain.graph_linker import GraphLinker
from nous.heart.heart import Heart

logger = logging.getLogger(__name__)

_SUMMARY_PROMPT = """You are summarizing a conversation episode for an AI agent's long-term memory.

Context:
- Agent: Nous (cognitive agent framework)
- This summary will be used for: semantic search recall, context assembly, calibration

Transcript:
{transcript}

{decision_context}

Return ONLY valid JSON (no markdown, no explanation):
{{
  "title": "<5-10 word descriptive title focusing on WHAT WAS ACCOMPLISHED>",
  "summary": "<100-150 word prose summary emphasizing decisions made, problems solved, and outcomes>",
  "key_points": [
    "<lesson or reusable knowledge, not just event description>",
    "<pattern or insight that would help in similar future situations>"
  ],
  "outcome": "<resolved|partial|unresolved|informational>",
  "outcome_rationale": "<1 sentence explaining why this outcome classification>",
  "topics": ["<topic1>", "<topic2>"],
  "candidate_facts": [
    "<factual statement worth storing as long-term knowledge>"
  ]
}}

Outcome guidelines:
- resolved: The user's request was fully addressed, task completed, question answered
- partial: Work started but not finished, or only some requests addressed
- unresolved: Failed to complete the task, hit blockers
- informational: Casual chat, status check, no actionable work done

For key_points: Focus on WHAT WAS LEARNED, not what happened. Ask yourself:
"If this agent faces a similar situation, what from this episode would help?"

For candidate_facts: Extract concrete, reusable knowledge (tool configs, preferences,
architectural decisions, API behaviors) that should persist as standalone facts."""


class EpisodeSummarizer:
    """Generates episode summaries on session end.

    Listens to session_ended events. If the session had an active episode,
    fetches the transcript, calls LLM for summary, stores on episode record.
    """

    def __init__(
        self,
        heart: Heart,
        brain: Brain | None,
        settings: Settings,
        bus: EventBus,
        llm_client: LLMClient | None = None,
        graph_linker: GraphLinker | None = None,
    ):
        self._heart = heart
        self._brain = brain
        self._settings = settings
        self._bus = bus
        self._llm = llm_client
        self._graph_linker = graph_linker
        bus.on("session_ended", self.handle)

    async def handle(self, event: Event) -> None:
        """Handle session_ended — summarize the episode if one exists."""
        episode_id = event.data.get("episode_id")
        if not episode_id:
            return

        try:
            # Fetch episode — skip if already summarized
            episode = await self._heart.get_episode(UUID(episode_id))
            if episode.structured_summary is not None:
                logger.debug("Episode %s already summarized, skipping", episode_id)
                return

            # Get transcript from event data
            transcript = event.data.get("transcript", "")
            if not transcript or len(transcript) < 50:
                logger.debug("Episode %s too short for summary, skipping", episode_id)
                return

            # Call LLM for summary
            decision_context = await self._build_decision_context(episode_id)
            summary = await self._generate_summary(transcript, decision_context)
            if not summary:
                return

            # Store summary on episode
            await self._heart.update_episode_summary(UUID(episode_id), summary)

            # Emit for downstream handlers (fact extraction)
            await self._bus.emit(Event(
                type="episode_summarized",
                agent_id=event.agent_id,
                session_id=event.session_id,
                data={
                    "episode_id": episode_id,
                    "summary": summary,
                    "candidate_facts": summary.get("candidate_facts", []),
                },
            ))

            logger.info("Episode %s summarized: %s", episode_id, summary.get("title", "?"))

            # F022: Create deterministic graph edges for this episode
            if self._graph_linker:
                try:
                    async with self._heart.db.session() as link_session:
                        ep = await self._heart.get_episode(UUID(episode_id))
                        decision_ids = ep.decision_ids if ep and hasattr(ep, "decision_ids") and ep.decision_ids else []

                        # Get facts extracted from this episode
                        from sqlalchemy import select as sa_select
                        from nous.storage.models import Fact
                        fact_result = await link_session.execute(
                            sa_select(Fact.id).where(Fact.source_episode_id == UUID(episode_id))
                        )
                        fact_ids = [r[0] for r in fact_result.all()]

                        if decision_ids or fact_ids:
                            await self._graph_linker.link_episode_deterministic(
                                episode_id=UUID(episode_id),
                                decision_ids=decision_ids,
                                fact_ids=fact_ids,
                                session=link_session,
                            )
                            await link_session.commit()
                            logger.debug(
                                "F022: Linked episode %s to %d decisions, %d facts",
                                episode_id, len(decision_ids), len(fact_ids),
                            )
                except Exception:
                    logger.debug("F022 graph linking failed for episode %s", episode_id)

        except Exception:
            logger.exception("Failed to summarize episode %s", episode_id)

    async def _generate_summary(self, transcript: str, decision_context: str = "") -> dict[str, Any] | None:
        """Call LLM to generate structured summary."""
        if not self._llm:
            logger.warning("No LLM client for episode summarizer")
            return None

        transcript = self._truncate_transcript(transcript)

        prompt = _SUMMARY_PROMPT.format(transcript=transcript, decision_context=decision_context)

        text = await call_background_llm(
            self._llm,
            model=self._settings.background_model,
            system_prompt="You are summarizing a conversation episode for an AI agent's long-term memory.",
            user_message=prompt,
            max_tokens=800,
        )

        if not text:
            logger.warning("Summary LLM returned empty text")
            return None

        try:
            return parse_llm_json(text)
        except json.JSONDecodeError as e:
            logger.warning("Summary generation failed: %s", e)
            return None

    def _truncate_transcript(self, transcript: str, max_chars: int = 8000) -> str:
        """008.4: Truncate transcript preserving high-value turns.

        Scores turns by information density: decision language and user turns
        score higher, long tool outputs score lower. Always keeps first and
        last turns. Fills middle by score within budget.
        """
        if len(transcript) <= max_chars:
            return transcript

        turns = transcript.split("\n\n")
        if len(turns) <= 2:
            return transcript[:max_chars]

        # Score each turn by information density
        scored: list[tuple[float, int, str]] = []
        for i, turn in enumerate(turns):
            score = 1.0
            lower = turn.lower()
            # Boost: decision language, conclusions
            if any(w in lower for w in ["decided", "chose", "because", "learned", "conclusion", "chosen"]):
                score += 2.0
            # Boost: user turns (directives, questions)
            if lower.startswith("user:") or lower.startswith("human:"):
                score += 1.0
            # Penalize: long tool outputs, raw data
            if len(turn) > 500 and ("```" in turn or turn.count("\n") > 10):
                score -= 1.0
            scored.append((score, i, turn))

        # Always keep first and last turns
        first = turns[0]
        last = turns[-1]
        budget = max_chars - len(first) - len(last) - 50  # buffer for separators

        if budget <= 0:
            return first[:max_chars // 2] + "\n\n" + last[:max_chars // 2]

        # Sort middle turns by score (descending), break ties by original order
        middle = sorted(scored[1:-1], key=lambda x: (-x[0], x[1]))
        kept_indices: set[int] = set()
        used = 0
        for score, idx, turn in middle:
            if used + len(turn) > budget:
                continue
            kept_indices.add(idx)
            used += len(turn)

        # Reconstruct in original order
        result = [first]
        for score, idx, turn in scored[1:-1]:
            if idx in kept_indices:
                result.append(turn)
        result.append(last)

        return "\n\n".join(result)

    async def _build_decision_context(self, episode_id: str) -> str:
        """008.4: Fetch decisions linked to this episode for richer summarization."""
        if not self._brain:
            return ""

        try:
            episode = await self._heart.get_episode(UUID(episode_id))
            if not episode or not episode.decision_ids:
                return ""

            lines = ["Decisions made during this episode:"]
            for decision_id in episode.decision_ids:
                d = await self._brain.get(decision_id)
                if d:
                    lines.append(
                        f"- [{d.category}/{d.stakes}] {d.description} "
                        f"(confidence: {d.confidence})"
                    )

            return "\n".join(lines) if len(lines) > 1 else ""
        except Exception:
            logger.debug("Failed to build decision context for episode %s", episode_id)
            return ""
