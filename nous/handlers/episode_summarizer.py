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
from nous.handlers import LLMClient, call_background_llm, cap_candidate_facts, parse_llm_json
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

NO PADDING RULE (#379): summary length must scale with the transcript. A short transcript (one or two exchanges) gets a 1-3 sentence summary — nothing more. NEVER pad with meta-commentary: do not describe what the transcript does NOT contain ("no additional context was provided", "no details such as X were specified"), do not characterize the interaction itself ("brief and transactional", "a single request followed by a confirmation"), and do not restate the same fact in different words. Every sentence must restate content literally present in the transcript.

Return ONLY valid JSON (no markdown, no explanation):
{{
  "title": "<5-10 word descriptive title focusing on WHAT WAS ACCOMPLISHED>",
  "summary": "<prose summary, up to 150 words for substantial transcripts, 1-3 sentences for short ones. Faithful to the transcript only. For assistant-action transcripts, describe the action(s) the assistant took.>",
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


# F075: optional addendum appended to _SUMMARY_PROMPT when
# settings.temporal_extraction_enabled is True. Directs the summarizer's LLM
# to extract date-anchored events from the TRANSCRIPT text (not the prose
# summary it's about to generate). When the episode start timestamp is
# known, it's also injected so relative date phrases can be resolved.
#
# NOTE: this string is CONCATENATED (not .format()'d) to the prompt at
# _summarize_single. Single braces (not {{ }}) — code-review round 1 P1.
_F075_TEMPORAL_INSTRUCTION = """

DATE-ANCHORED EVENTS (F075):
When the TRANSCRIPT above describes an event happening on a specific date —
particularly something the user did or completed — capture it as a SEPARATE
candidate_fact with the date attached. Extend the candidate_facts schema
above with an optional 4th field "event_date":

  {
    "subject": "<short descriptor of the event>",
    "content": "<entity> <action verb> <object> on <full date>.",
    "category": "event",
    "event_date": "YYYY-MM-DD"
  }

Examples:
  - User says "I got my OpenWeather API key on March 10" →
    {"subject": "OpenWeather API key acquisition",
     "content": "Christina obtained the OpenWeather API key on March 10, 2024.",
     "category": "event", "event_date": "2024-03-10"}
  - User says "we deployed v2.1 last Tuesday" (EPISODE_START_TIMESTAMP = 2024-04-11) →
    {"subject": "v2.1 deployment", "content": "Team deployed v2.1 to staging on 2024-04-09.",
     "category": "event", "event_date": "2024-04-09"}

DO NOT set event_date for these — they are metadata, not events in the user's
timeline (extract the fact WITHOUT event_date):
  - The publication / arXiv / release / version date of a paper, article,
    library, model, or any artifact the user is merely reading, citing, or
    discussing. "(Xu, Jan 2026, arXiv:2601.01743)" is bibliographic metadata,
    not something that happened on a timeline.
  - Any date you cannot pin to a specific DAY. If only a month or year is known
    ("Feb 2026", "last spring"), OMIT event_date — never default to the 1st.

CRITICAL: extract dates from the TRANSCRIPT text (above), not from any summary
you have generated. Dates mentioned in passing — inside code blocks, user
asides, scheduling discussions — are just as important as headline dates.
Resolve relative phrases ("yesterday", "last week", "3 days ago") against
the EPISODE_START_TIMESTAMP block when one is provided, and take the YEAR from
it — never assume a prior year. If a date is ambiguous or unresolvable, OMIT
event_date (set null) but still extract the fact without the date field — it
becomes a stable fact."""


_F075_EPISODE_TS_BLOCK = "EPISODE_START_TIMESTAMP: {iso}\n\n"


# Coverage fix (2026-06-14 audit): appended to _SUMMARY_PROMPT when
# settings.extraction_coverage_broadened is True. The base candidate_facts
# framing ("concrete, reusable knowledge: tool configs, preferences,
# architectural decisions, API behaviors") under-extracts user-task detail —
# the audit measured status_state 0.54 / dated_event 0.45 / preference 0.36
# missed. This block broadens the mandate to queryable specifics with a noise
# guard. CONCATENATED (not .format()'d) — single braces only.
_COVERAGE_EXPANSION_INSTRUCTION = """

COVERAGE EXPANSION:
candidate_facts should capture ANY concrete, queryable fact the user might later
ask about — not only reusable engineering knowledge. In ADDITION to the
categories above, extract each of the following as its OWN candidate_fact:
  - Specific things the user DID or experienced, with any place/date
    ("Tim travelled to Portland, ME on May 15-18, 2026"). category: "event"
  - Results or status the user may reference later: deliverables produced
    (filenames, word counts), figures/forecasts/data they requested, current
    configuration or state. category: "status"
  - Personal facts about the user: location, background, identity, traits.
    category: "person"
  - Specific named details tied to the above: people, numbers, IDs, versions,
    settings, measurements.
Emit one fact per discrete, separately-queryable item — do not bundle several
into one. EXCLUDE only pure conversational scaffolding with no queryable content
(greetings, acknowledgements, meta-chatter about the conversation itself)."""


# F083 B: appended to _SUMMARY_PROMPT when settings.episode_open_threads is True.
# CONCATENATED (single braces), mirroring _F075_TEMPORAL_INSTRUCTION. Respects the
# NO PADDING rule: empty/omitted when nothing is genuinely unfinished.
_OPEN_THREADS_INSTRUCTION = """

OPEN THREADS (F083):
If the TRANSCRIPT above leaves work unfinished — a next step the user intended, a
question left open, a task started but not completed — add a top-level "open_threads"
array, each entry one short actionable phrase:

  "open_threads": ["finish wiring the auth callback", "decide on retry budget"]

Return an empty array (or omit) when nothing is unfinished. Do NOT invent or pad."""


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
            summary = await self._generate_summary(
                transcript, decision_context, started_at=episode.started_at,
            )
            if not summary:
                return None
            await self._heart.update_episode_summary(episode_id, summary)
            # F067: Optional transcript chunking for retrieval. Failures are
            # logged + swallowed so they never block summary persistence.
            if getattr(self._settings, "episode_chunks_enabled", False):
                try:
                    await self._chunk_and_store_transcript(
                        episode_id=episode_id,
                        agent_id=agent_id or self._settings.agent_id,
                        transcript=transcript,
                    )
                except Exception:
                    logger.warning(
                        "F067: chunk-and-store failed for episode %s (non-fatal)",
                        episode_id, exc_info=True,
                    )
            return summary
        except Exception:
            logger.exception("F051.5: summarize_episode raised for %s", episode_id)
            return None

    async def _chunk_and_store_transcript(
        self,
        episode_id: UUID,
        agent_id: str,
        transcript: str,
    ) -> int:
        """F067: chunk transcript, embed each chunk, insert into heart.episode_chunks.

        Returns count of chunks written. Idempotent via the
        (episode_id, chunk_index) unique constraint — re-running on the same
        episode is a no-op (ON CONFLICT DO NOTHING).
        """
        from sqlalchemy import text as sa_text
        from nous.heart.chunking import chunk_text

        chunks = chunk_text(
            transcript,
            chunk_size=self._settings.episode_chunk_size,
            overlap=self._settings.episode_chunk_overlap,
            min_chars=self._settings.episode_chunk_min_transcript_chars,
        )
        if not chunks:
            return 0

        # Embed all chunks in one batched call. If the embedder fails or
        # isn't wired, we ABORT the insert entirely rather than persist
        # NULL-embedding rows — those rows would never appear in retrieval
        # (the recall query filters `embedding IS NOT NULL`) AND the
        # ON CONFLICT (episode_id, chunk_index) DO NOTHING idempotency
        # would prevent a later retry from patching them. Better to leave
        # the table clean; a backfill handler can re-attempt later.
        embedder = getattr(self._heart, "_embeddings", None) or self._embedder
        if embedder is None:
            logger.info(
                "F067: no embedder wired; skipping chunk store for episode %s",
                episode_id,
            )
            return 0
        try:
            vectors = await embedder.embed_batch(chunks)
        except Exception:
            logger.warning(
                "F067: embed_batch failed for episode %s; aborting chunk "
                "store (no NULL-embedding rows written — retry later)",
                episode_id, exc_info=True,
            )
            return 0
        if len(vectors) != len(chunks):
            logger.warning(
                "F067: embedder returned %d vectors for %d chunks (episode %s); "
                "aborting chunk store to avoid misalignment",
                len(vectors), len(chunks), episode_id,
            )
            return 0

        async with self._heart.db.session() as s:
            # Audit E1 (2026-06-09): serialize with ingest_document (F069)
            # via the SAME episode-scoped advisory lock, and append after
            # MAX(chunk_index) instead of numbering from 0. A document
            # ingested mid-session occupies indexes 0..M on this episode;
            # numbering transcript chunks from 0 collided on
            # (episode_id, chunk_index) and ON CONFLICT DO NOTHING silently
            # destroyed every transcript chunk with index <= M.
            await s.execute(
                sa_text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
                {"k": f"ingest_document:{episode_id}"},
            )
            # Idempotency moved from per-row index collision to a per-kind
            # existence check: a re-summarize (F060 recovery, concurrent
            # session_ended handlers) must not append a second copy now
            # that indexes are MAX+1-allocated.
            already = await s.execute(
                sa_text(
                    "SELECT 1 FROM heart.episode_chunks "
                    "WHERE agent_id = :agent_id AND episode_id = :ep "
                    "  AND source_kind = 'dialogue' LIMIT 1"
                ),
                {"agent_id": agent_id, "ep": str(episode_id)},
            )
            if already.first() is not None:
                await s.commit()  # release the advisory lock
                logger.info(
                    "F067: dialogue chunks already stored for episode %s; skipping",
                    episode_id,
                )
                return 0
            next_idx_row = await s.execute(
                sa_text(
                    "SELECT COALESCE(MAX(chunk_index), -1) + 1 "
                    "FROM heart.episode_chunks "
                    "WHERE agent_id = :agent_id AND episode_id = :ep"
                ),
                {"agent_id": agent_id, "ep": str(episode_id)},
            )
            start_idx = int(next_idx_row.scalar() or 0)
            for offset, (content, vec) in enumerate(zip(chunks, vectors)):
                if not vec:
                    # Defensive — shouldn't happen given the length check above,
                    # but a single None in the batch is still skippable.
                    continue
                vec_lit = "[" + ",".join(f"{v:.6f}" for v in vec) + "]"
                await s.execute(sa_text(
                    "INSERT INTO heart.episode_chunks "
                    "(agent_id, episode_id, chunk_index, content, embedding) "
                    "VALUES (:agent_id, :ep, :idx, :content, CAST(:vec AS vector)) "
                    "ON CONFLICT (episode_id, chunk_index) DO NOTHING"
                ), {
                    "agent_id": agent_id, "ep": str(episode_id),
                    "idx": start_idx + offset, "content": content, "vec": vec_lit,
                })
            await s.commit()
        return len(chunks)

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

                # F044: commit reinforcement-only sessions too (re-derived edges
                # increment LTP but create no new rows). Flag-gated to preserve
                # default-prod commit semantics when F044 is off.
                if edges_created > 0 or getattr(self._settings, "tinyhippo_lite_enabled", False):
                    await session.commit()
                    if edges_created > 0:
                        logger.debug(
                            "F040: Linked episode %s to %d similar episodes",
                            episode_id, edges_created,
                        )
                return edges_created
        except Exception:
            logger.debug("F040: Episode semantic linking failed for %s", episode_id)
            return 0

    async def _generate_summary(
        self,
        transcript: str,
        decision_context: str = "",
        started_at: datetime | None = None,
    ) -> dict[str, Any] | None:
        """Generate structured summary from transcript using LLM.

        F025 P3-B: For transcripts exceeding the limit, split into chunks,
        summarize each independently, then merge results.

        F075: ``started_at`` (episode start timestamp) is threaded through
        to ``_summarize_single`` so the LLM can resolve relative date phrases
        when ``settings.temporal_extraction_enabled`` is on.
        """
        if not self._llm:
            logger.warning("No LLM client for episode summarizer")
            return None

        max_chars = self._settings.transcript_max_chars
        chunks = self._chunk_transcript(transcript, max_chars=max_chars)

        if len(chunks) == 1:
            # Single chunk: truncate and summarize directly (original path)
            truncated = self._truncate_transcript(chunks[0], max_chars=max_chars)
            return await self._summarize_single(truncated, decision_context, started_at=started_at)

        # Multi-chunk: summarize each chunk, then merge
        chunk_summaries = []
        for chunk in chunks:
            truncated = self._truncate_transcript(chunk, max_chars=max_chars)
            summary = await self._summarize_single(truncated, decision_context, started_at=started_at)
            if summary:
                chunk_summaries.append(summary)

        if not chunk_summaries:
            return None
        if len(chunk_summaries) == 1:
            return chunk_summaries[0]

        return self._merge_summaries(chunk_summaries)

    def _build_summary_prompt(
        self,
        transcript: str,
        decision_context: str,
        started_at: "datetime | None",
    ) -> str:
        """Assemble the summarization prompt with optional instruction addenda.

        Flag-gated addenda are concatenated (never .format()'d) in a fixed
        order so each new flag only adds text at the tail.
        """
        prompt = _SUMMARY_PROMPT.format(transcript=transcript, decision_context=decision_context)
        if getattr(self._settings, "temporal_extraction_enabled", False):
            if started_at is not None:
                prompt = _F075_EPISODE_TS_BLOCK.format(iso=started_at.isoformat()) + prompt
            prompt = prompt + _F075_TEMPORAL_INSTRUCTION
        if getattr(self._settings, "extraction_coverage_broadened", False):
            prompt = prompt + _COVERAGE_EXPANSION_INSTRUCTION
        if getattr(self._settings, "episode_open_threads", False):
            prompt = prompt + _OPEN_THREADS_INSTRUCTION
        return prompt

    def _summary_max_tokens(self) -> int:
        """Return the max_tokens budget for a single summarization LLM call.

        F083 R5: open_threads competes with F075 events for output budget;
        raise the ceiling so a long transcript's JSON doesn't truncate
        (losing the whole summary).
        """
        if getattr(self._settings, "extraction_coverage_broadened", False):
            return 3000
        if getattr(self._settings, "episode_open_threads", False):
            return 3000
        return 1500

    async def _summarize_single(
        self,
        transcript: str,
        decision_context: str,
        started_at: datetime | None = None,
    ) -> dict[str, Any] | None:
        """Summarize a single transcript chunk via LLM.

        F075: when ``settings.temporal_extraction_enabled`` is True, the
        ``_F075_TEMPORAL_INSTRUCTION`` block is appended to the prompt and
        the episode start timestamp (when known) is injected so relative
        date phrases resolve deterministically.
        """
        prompt = self._build_summary_prompt(transcript, decision_context, started_at)
        max_tokens = self._summary_max_tokens()

        text = await call_background_llm(
            self._llm,
            model=self._settings.background_model,
            system_prompt="You are summarizing a conversation episode for an AI agent's long-term memory.",
            user_message=prompt,
            max_tokens=max_tokens,
        )

        if not text:
            logger.warning("Summary LLM returned empty text")
            return None

        try:
            result = parse_llm_json(text)
        except json.JSONDecodeError as e:
            logger.warning("Summary generation failed: %s", e)
            return None
        # parse_llm_json can return a bare list when the model emits a JSON
        # array instead of the summary object (truncation / format drift —
        # more likely with the longer coverage-broadened output). The caller
        # and _merge_summaries call .get() on the result, so a non-dict must be
        # a clean skip (None), not a crash that loses the whole episode.
        if not isinstance(result, dict):
            logger.warning(
                "Summary parse returned %s, not an object; skipping summary",
                type(result).__name__,
            )
            return None
        return result

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
        merged_open_threads: list = []

        for s in summaries:
            if s.get("summary"):
                merged_summary_parts.append(s["summary"])
            merged_key_points.extend(s.get("key_points", []))
            merged_candidate_facts.extend(s.get("candidate_facts", []))
            merged_topics.update(s.get("topics", []))
            v = s.get("open_threads")
            if isinstance(v, list):
                merged_open_threads.extend(str(t) for t in v if isinstance(t, (str, int, float)))

        # F075: split dated and stable candidates into separate pools with
        # independent caps. Dated events from later chunks must survive the
        # truncation — F075 specifically targets long multi-chunk transcripts
        # where temporal_reasoning failures originate. Stable cap stays at 5.
        # Double-getattr handles test fixtures that construct the summarizer
        # without _settings (test_f025_chunked.py uses EpisodeSummarizer.__new__).
        # F075 + coverage fix: shared partition/cap helper (single source of
        # truth — the storage paths in FactExtractor use the same helper, so a
        # broadened merge is never re-truncated downstream). The legacy hardcoded
        # stable cap of 5 was a hard ceiling on multi-chunk episodes (audit:
        # 8K-char transcripts → 1 fact); candidate_facts_stable_limit raises it
        # when extraction_coverage_broadened is on.
        merged_candidate_facts = cap_candidate_facts(
            merged_candidate_facts, getattr(self, "_settings", None)
        )

        return {
            "title": summaries[0].get("title", "Multi-part episode"),
            "summary": " ".join(merged_summary_parts),
            "key_points": merged_key_points[:10],
            "candidate_facts": merged_candidate_facts,
            "outcome": summaries[-1].get("outcome", "informational"),
            "outcome_rationale": summaries[-1].get("outcome_rationale", ""),
            "topics": sorted(merged_topics),
            "open_threads": merged_open_threads[:10],
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
