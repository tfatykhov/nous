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

CRITICAL FAITHFULNESS RULE (F056 #379): Only include claims directly supported by the transcript above. Do NOT invent user motivation, prior session context, or success criteria that are not literally in the transcript. If the transcript is primarily an assistant action (e.g. "I sent X", "I booked Y", "I added a reminder"), summarize what was DONE — do not speculate about why the user wanted it.

Return ONLY valid JSON (no markdown, no explanation):
{{
  "title": "<5-10 word descriptive title focusing on WHAT WAS ACCOMPLISHED>",
  "summary": "<100-150 word prose summary. Faithful to the transcript only. For assistant-action transcripts, describe the action(s) the assistant took.>",
  "key_points": [
    "<lesson or reusable knowledge that IS supported by the transcript — NOT speculation>",
    "<pattern or insight that would help in similar future situations, only if it appears in the transcript>"
  ],
  "outcome": "<resolved|partial|unresolved|informational>",
  "outcome_rationale": "<1 sentence explaining why this outcome classification>",
  "topics": ["<topic1>", "<topic2>"],
  "candidate_facts": [
    {{
      "subject": "<who/what the fact is about>",
      "content": "<factual statement worth storing as long-term knowledge>",
      "category": "<preference|person|rule|technical|concept|tool>"
    }}
  ]
}}

Categories for candidate_facts:
- "preference" — User preferences (formats, units, style)
- "person" — People facts (names, roles, relationships)
- "rule" — ONLY explicit directives from the user
- "technical" — Architecture, implementation, project-specific knowledge
- "concept" — General knowledge, research findings, theoretical insights
- "tool" — Tool/library behavior, gotchas, configuration

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
        bus: EventBus | None,  # F051.5: nullable for direct-invocation paths (ingest)
        llm_client: LLMClient | None = None,
        graph_linker: GraphLinker | None = None,
    ):
        self._heart = heart
        self._brain = brain
        self._settings = settings
        self._bus = bus
        self._llm = llm_client
        self._graph_linker = graph_linker
        self._embedder = None  # F040: Set externally for episode↔episode linking
        if bus is not None:
            bus.on("session_ended", self.handle)

    async def summarize_episode(
        self,
        episode_id: UUID,
        transcript: str,
        agent_id: str | None = None,
    ) -> dict | None:
        """F051.5: Public direct-invocation entry point.

        Mirrors lines 110-129 of handle(): fetches the episode, checks not-already-
        summarized, generates summary via LLM, persists. Returns the summary dict
        on success, or None on early-return (already summarized, transcript too
        short) or LLM failure.

        Does NOT emit ``episode_summarized`` — caller's responsibility (production
        bus path uses handle() which still emits; ingest paths invoke fact_extractor
        directly so don't need the event).

        Does NOT trigger graph linking — caller-driven side effects belong on
        handle(), not the core summarization path.
        """
        try:
            episode = await self._heart.get_episode(episode_id)
            if episode.structured_summary is not None:
                logger.debug("F051.5: episode %s already summarized, skipping", episode_id)
                return None
            if not transcript or len(transcript) < 50:
                logger.debug("F051.5: episode %s transcript too short, skipping", episode_id)
                return None
            decision_context = await self._build_decision_context(str(episode_id))
            summary = await self._generate_summary(transcript, decision_context)
            if not summary:
                return None
            await self._heart.update_episode_summary(episode_id, summary)
            return summary
        except Exception:
            logger.exception("F051.5: summarize_episode raised for %s", episode_id)
            return None

    async def handle(self, event: Event) -> None:
        """Handle session_ended — summarize the episode if one exists."""
        episode_id = event.data.get("episode_id")
        if not episode_id:
            return

        try:
            # F051.5: delegate to the directly-invocable core. None return covers
            # all early-return + LLM-failure cases — same skip semantics as before.
            transcript = event.data.get("transcript", "")
            summary = await self.summarize_episode(
                episode_id=UUID(episode_id),
                transcript=transcript,
                agent_id=event.agent_id,
            )
            if summary is None:
                return

            # Emit for downstream handlers (fact extraction)
            await self._bus.emit(Event(
                type="episode_summarized",
                agent_id=event.agent_id,
                session_id=event.session_id,
                data={
                    "episode_id": episode_id,
                    "summary": summary,
                    "candidate_facts": summary.get("candidate_facts", []),
                    "transcript": transcript,  # F025 P2-E: pass for fact grounding
                },
                trace_id=event.trace_id,       # F035.2: inherit from parent
                caused_by=event.event_id,      # F035.2: point to parent
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

                # F040: Semantic episode↔episode linking
                summary_text = summary.get("summary", "")
                if summary_text:
                    await self._link_similar_episodes(UUID(episode_id), summary_text)

        except Exception:
            logger.exception("Failed to summarize episode %s", episode_id)

    async def _link_similar_episodes(self, episode_id: UUID, summary_text: str) -> int:
        """F040: Find and link semantically similar episodes."""
        if not self._graph_linker or not self._embedder or not summary_text:
            return 0
        try:
            from nous.brain.graph_linker import common_template_text
            from sqlalchemy import text as sa_text

            template = common_template_text("episode", summary_text)
            ep_emb = await self._embedder.embed(template)
            emb_str = "[" + ",".join(str(float(v)) for v in ep_emb) + "]"

            threshold = getattr(self._settings, "graph_threshold_episode_episode", 0.75)

            async with self._graph_linker.db.session() as session:
                sql = sa_text("""
                    SELECT id,
                           1 - (embedding <=> CAST(:embedding AS vector)) AS similarity
                    FROM heart.episodes
                    WHERE agent_id = :agent_id
                      AND id != :episode_id
                      AND embedding IS NOT NULL
                      AND 1 - (embedding <=> CAST(:embedding AS vector)) >= :threshold
                    ORDER BY embedding <=> CAST(:embedding AS vector)
                    LIMIT 3
                """)
                result = await session.execute(sql, {
                    "embedding": emb_str,
                    "agent_id": self._graph_linker.agent_id,
                    "episode_id": episode_id,
                    "threshold": threshold * 0.9,
                })

                edges_created = 0
                for row in result:
                    if row.similarity >= threshold:
                        edge = await self._graph_linker.create_edge(
                            source_id=episode_id, target_id=row.id,
                            source_type="episode", target_type="episode",
                            relation="related_to", weight=float(row.similarity),
                            session=session,
                        )
                        if edge:
                            edges_created += 1

                if edges_created > 0:
                    await session.commit()
                    logger.debug(
                        "F040: Linked episode %s to %d similar episodes",
                        episode_id, edges_created,
                    )
                return edges_created
        except Exception:
            logger.debug("F040: Episode semantic linking failed for %s", episode_id)
            return 0

    async def _generate_summary(self, transcript: str, decision_context: str = "") -> dict[str, Any] | None:
        """Generate structured summary from transcript using LLM.

        F025 P3-B: For transcripts exceeding the limit, split into chunks,
        summarize each independently, then merge results.
        """
        if not self._llm:
            logger.warning("No LLM client for episode summarizer")
            return None

        max_chars = self._settings.transcript_max_chars
        chunks = self._chunk_transcript(transcript, max_chars=max_chars)

        if len(chunks) == 1:
            # Single chunk: truncate and summarize directly (original path)
            truncated = self._truncate_transcript(chunks[0], max_chars=max_chars)
            return await self._summarize_single(truncated, decision_context)

        # Multi-chunk: summarize each chunk, then merge
        chunk_summaries = []
        for chunk in chunks:
            truncated = self._truncate_transcript(chunk, max_chars=max_chars)
            summary = await self._summarize_single(truncated, decision_context)
            if summary:
                chunk_summaries.append(summary)

        if not chunk_summaries:
            return None
        if len(chunk_summaries) == 1:
            return chunk_summaries[0]

        return self._merge_summaries(chunk_summaries)

    async def _summarize_single(self, transcript: str, decision_context: str) -> dict[str, Any] | None:
        """Summarize a single transcript chunk via LLM."""
        prompt = _SUMMARY_PROMPT.format(transcript=transcript, decision_context=decision_context)

        text = await call_background_llm(
            self._llm,
            model=self._settings.background_model,
            system_prompt="You are summarizing a conversation episode for an AI agent's long-term memory.",
            user_message=prompt,
            max_tokens=1500,
        )

        if not text:
            logger.warning("Summary LLM returned empty text")
            return None

        try:
            return parse_llm_json(text)
        except json.JSONDecodeError as e:
            logger.warning("Summary generation failed: %s", e)
            return None

    def _chunk_transcript(self, transcript: str, max_chars: int = 16000) -> list[str]:
        """F025 P3-B: Split long transcript into chunks at turn boundaries.

        Returns a list of chunks, each within max_chars. Splits on
        double-newline turn boundaries to preserve turn integrity.
        Short transcripts return as a single-element list.
        """
        if len(transcript) <= max_chars:
            return [transcript]

        turns = transcript.split("\n\n")
        chunks: list[str] = []
        current_turns: list[str] = []
        current_len = 0

        for turn in turns:
            turn_len = len(turn) + 2  # +2 for \n\n separator
            if current_len + turn_len > max_chars and current_turns:
                chunks.append("\n\n".join(current_turns))
                current_turns = []
                current_len = 0
            current_turns.append(turn)
            current_len += turn_len

        if current_turns:
            chunks.append("\n\n".join(current_turns))

        return chunks

    def _merge_summaries(self, summaries: list[dict]) -> dict:
        """F025 P3-B: Merge multiple chunk summaries into one.

        Uses first chunk's title, last chunk's outcome (most informed),
        and unions all other list fields.
        """
        merged_summary_parts = []
        merged_key_points: list[str] = []
        merged_candidate_facts: list = []
        merged_topics: set[str] = set()

        for s in summaries:
            if s.get("summary"):
                merged_summary_parts.append(s["summary"])
            merged_key_points.extend(s.get("key_points", []))
            merged_candidate_facts.extend(s.get("candidate_facts", []))
            merged_topics.update(s.get("topics", []))

        return {
            "title": summaries[0].get("title", "Multi-part episode"),
            "summary": " ".join(merged_summary_parts),
            "key_points": merged_key_points[:10],
            "candidate_facts": merged_candidate_facts[:5],
            "outcome": summaries[-1].get("outcome", "informational"),
            "outcome_rationale": summaries[-1].get("outcome_rationale", ""),
            "topics": sorted(merged_topics),
        }

    def _truncate_transcript(self, transcript: str, max_chars: int = 16000) -> str:
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
