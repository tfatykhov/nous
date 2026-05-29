"""Fact Extractor — proactively learns facts from episode summaries.

Listens to: episode_summarized
Uses the structured summary to identify facts worth remembering.
Deduplicates against existing facts before storing.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID


def _parse_episode_uuid(episode_id: str | None) -> UUID | None:
    """Convert a fact_extractor episode_id to UUID, returning None on garbage.

    The handle() event path falls back to ``"?"`` when the upstream event
    data is missing ``episode_id``; previous bug (orphan-rate audit
    2026-04-30) was that FactInput.source_episode_id was never set, so
    every extracted fact had a NULL FK and ``link_episode_deterministic``
    produced zero edges.
    """
    if not episode_id or episode_id == "?":
        return None
    try:
        return UUID(episode_id)
    except (ValueError, TypeError):
        return None

from nous.config import Settings
from nous.events import Event, EventBus
from nous.handlers import LLMClient, call_background_llm, parse_llm_json
from nous.heart.heart import Heart
from nous.heart.schemas import FactInput, FactRejected

logger = logging.getLogger(__name__)

_EXTRACT_PROMPT = """Review the following conversation summary and extract facts worth remembering long-term.

Focus on:
- User preferences (tools, formats, units, communication style)
- Project/system facts (architecture, constraints, conventions)
- People facts (roles, names, relationships)
- Explicit rules or directives from the user

Summary: {summary}
Key Points: {key_points}

Return ONLY a valid JSON array (empty array if nothing worth storing):
[
  {{
    "subject": "<who/what the fact is about>",
    "content": "<the fact, stated clearly>",
    "category": "<category>",
    "confidence": <0.0-1.0>
  }}
]

Categories (use the RIGHT one — this affects how facts are loaded):
- "preference" — User preferences (formats, units, style). Loaded EVERY turn.
- "person" — People facts (names, roles, relationships). Loaded EVERY turn.
- "rule" — ONLY explicit directives from the user (e.g., "never push to main"). Loaded EVERY turn.
- "technical" — Architecture, implementation, or project-specific knowledge. Loaded only when relevant.
- "concept" — General knowledge, research findings, theoretical insights. Loaded only when relevant.
- "tool" — Tool/library behavior, gotchas, configuration. Loaded only when relevant.

IMPORTANT: "rule" is for user-stated directives ONLY. Research findings, observations,
debug lessons, and architecture patterns should be "technical" or "concept".
If in doubt between "rule" and something else, choose the something else.

Only include facts genuinely useful across future conversations.
Skip transient, trivial, or already-known information.
Max 5 facts."""


class FactExtractor:
    """Extracts and stores facts from episode summaries.

    Listens to episode_summarized events. Calls LLM to identify facts,
    deduplicates against existing facts, stores new ones.
    Max 5 facts per episode.
    """

    def __init__(
        self,
        heart: Heart,
        settings: Settings,
        bus: EventBus | None,  # F051.5: nullable for direct-invocation paths (ingest)
        llm_client: LLMClient | None = None,
        dedup_via_search: bool = True,
    ):
        """
        Args:
            dedup_via_search: when True (production default), pre-checks
                ``search_facts(content)`` against ``fact_dedup_threshold``
                before calling ``Heart.learn``. The pre-check uses HYBRID
                search (vector + keyword RRF) which catches lexical
                paraphrases pure cosine misses. F051.5 ingest sets this
                False because hybrid RRF scores are unreliable on a
                near-empty corpus (the lone existing fact RRF≈1.0 trips
                dedup for every subsequent candidate). Heart.learn's
                native cosine > 0.95 dedup still runs regardless.
        """
        self._heart = heart
        self._settings = settings
        self._bus = bus
        self._llm = llm_client
        self._dedup_via_search = dedup_via_search
        if bus is not None:
            bus.on("episode_summarized", self.handle)

    async def _confirm_dedup(self, existing_content: str, candidate_content: str) -> bool:
        """F377: confirm an RRF-flagged duplicate before skipping the write.

        The hybrid-search (RRF) pre-check over-dedups high-lexical-overlap
        semantic opposites ("MRR -5%" vs "+5%"). When the tiebreaker flag is on,
        a same-vs-distinct Haiku classifier confirms the verdict. Returns True if
        the candidate should be deduped (skipped), False if it was judged DISTINCT
        and should be stored. Fails open to dedup (flag off, or None verdict) so a
        tiebreaker outage never changes legacy behaviour. Shared by both dedup
        sites so they cannot drift (cf. #354)."""
        # getattr default-off: duck-typed / stub settings without the attr must
        # not accidentally enable the tiebreaker (codex P2).
        if not getattr(self._settings, "fact_dedup_tiebreaker_enabled", False):
            return True
        if await self._heart.facts.is_distinct_fact(existing_content, candidate_content) is True:
            logger.debug(
                "F377 tiebreaker: DISTINCT — storing despite RRF dup: %s",
                candidate_content[:50],
            )
            return False
        return True

    async def _resolve_dedup(
        self, content: str, candidate_event_date: "date | None"
    ) -> UUID | None:
        """Return the canonical fact UUID to dedup against, or None to store new.

        Examines every above-threshold RRF hit (not just the top one) when the
        tiebreaker is enabled, so a high-overlap opposite ranked first cannot
        hide a real duplicate ranked lower (codex P2). With the flag off, only
        the top hit is examined — byte-identical to the pre-F377 path. Hits whose
        ``event_date`` differs from the candidate are skipped (F075: distinct
        dates = distinct events). Shared by both producer paths (cf. #354)."""
        if not self._dedup_via_search:
            return None
        tiebreaker = getattr(self._settings, "fact_dedup_tiebreaker_enabled", False)
        # Only widen the search when the tiebreaker can re-judge lower hits;
        # flag-off keeps the historical single-top-hit behaviour exactly.
        limit = 5 if tiebreaker else 1
        existing = await self._heart.search_facts(content, limit=limit)
        for cand in existing:
            # search_facts is score-desc; stop at the first sub-threshold hit.
            if cand.score is None or cand.score <= self._settings.fact_dedup_threshold:
                break
            # F075: distinct event_dates = distinct events, never a duplicate.
            if (
                candidate_event_date is not None
                and cand.event_date is not None
                and candidate_event_date != cand.event_date
            ):
                continue
            if await self._confirm_dedup(cand.content, content):
                return cand.id
        return None

    async def extract_and_store(
        self,
        summary: dict,
        episode_id: str,
        transcript: str | None = None,
        candidate_facts: list | None = None,
    ) -> list[UUID]:
        """F051.5: Public direct-invocation entry point.

        Mirrors the body of handle(): dispatches to candidate_facts path if
        pre-extracted facts present, else falls back to LLM extraction.
        Returns the list of fact UUIDs that materially exist after this call:
        newly-stored UUIDs PLUS canonical UUIDs of dedup-skipped facts (so
        ingest provenance can map gold IDs to the canonical fact regardless
        of which session originally stored it).

        ``candidate_facts`` is accepted as an explicit param because
        production handle() reads it from ``event.data`` (top-level), NOT
        from ``summary``. When ingest invokes directly with summary that
        already contains the candidates, leave candidate_facts=None and
        extract_and_store falls back to ``summary.get("candidate_facts")``.

        ``handle()`` discards the return value; ingest paths consume it.
        """
        if not summary:
            return []
        # 008.4: Use pre-extracted candidate_facts if available. Caller may
        # pass them explicitly (handle() does, reading event.data) or leave
        # candidate_facts=None and we fall back to the summary dict.
        cands = candidate_facts if candidate_facts is not None else summary.get("candidate_facts", [])
        if cands:
            return await self._store_candidate_facts(
                cands, episode_id, transcript=transcript
            )
        # Fallback: LLM extraction
        candidates = await self._extract_facts(summary)
        if not candidates:
            return []
        return await self._store_extracted_facts(candidates, episode_id, transcript)

    async def _store_extracted_facts(
        self,
        candidates: list[dict],
        episode_id: str,
        transcript: str | None,
    ) -> list[UUID]:
        """F051.5: LLM-fallback storage path, refactored to track UUIDs.

        Mirrors the loop body of pre-F051.5 handle() lines 108-139 verbatim,
        with the addition of ``stored_ids`` accumulation across both
        dedup-skip and successful-store branches (P2-fix per spec §7).
        """
        stored_ids: list[UUID] = []
        stored = 0
        # F075: split dated and stable candidates with separate caps so dated
        # events from later in the LLM list aren't dropped by the [:5] truncation.
        # NOTE: the _EXTRACT_PROMPT fallback path's prompt schema does NOT
        # include event_date (only the summarizer's prompt does). Any "dated"
        # candidates here would be the LLM hallucinating a field — possible
        # but rare. We still partition defensively to match _store_candidate_facts.
        dated = [c for c in candidates if isinstance(c, dict) and c.get("event_date")]
        stable = [c for c in candidates if not (isinstance(c, dict) and c.get("event_date"))]
        event_limit = getattr(self._settings, "candidate_facts_event_limit", 30)
        capped = dated[:event_limit] + stable[:5]
        for fact in capped:
            confidence = fact.get("confidence", 0.7)
            if confidence < 0.6:
                logger.debug("Skipping low-confidence fact: %s", fact.get("content", "")[:50])
                continue

            # F075: normalize candidate event_date BEFORE dedup compare so we
            # don't trip on LLM format drift (raw "2024-3-10" vs validated
            # date(2024,3,10) would be unequal as strings). Round-trip
            # through FactInput's validator: get back a date | None.
            raw_event_date = fact.get("event_date")
            candidate_event_date = (
                FactInput(content="_", event_date=raw_event_date).event_date
                if raw_event_date is not None
                else None
            )

            # Dedup: check if similar fact exists (production paraphrase guard).
            # _resolve_dedup examines all above-threshold RRF hits (F377) and
            # applies the F075 distinct-event_date bypass; None = store as new.
            content = fact.get("content", "")
            canonical = await self._resolve_dedup(content, candidate_event_date)
            if canonical is not None:
                stored_ids.append(canonical)
                logger.debug(
                    "Dedup skip — adding canonical UUID %s for content: %s",
                    canonical, content[:50],
                )
                continue

            # F075 (arch P2-1): the fallback _EXTRACT_PROMPT has no event_date
            # field, so any dated candidate here is best-effort and we should
            # NOT stamp classified_at — those rows must remain backfill-eligible.
            # The summarizer's _store_candidate_facts path DOES stamp because
            # its prompt explicitly asks for the field (see Layer 1a in spec).
            classified_at = (
                datetime.now(UTC)
                if (
                    getattr(self._settings, "temporal_extraction_enabled", False)
                    and candidate_event_date is not None
                )
                else None
            )
            # Store — P1-8 fix: pass category from LLM response.
            # F022 orphan-rate audit fix: tag the fact with its source
            # episode so link_episode_deterministic can create the
            # extracted_from edge in the next sleep/summary cycle.
            fact_input = FactInput(
                subject=fact.get("subject", "unknown"),
                content=content,
                source="fact_extractor",
                confidence=confidence,
                category=fact.get("category"),
                source_text=transcript,  # F025 P2-E
                source_episode_id=_parse_episode_uuid(episode_id),
                event_date=candidate_event_date,
                event_date_classified_at=classified_at,
            )
            result = await self._heart.learn(fact_input)
            if isinstance(result, FactRejected):
                logger.debug("Admission rejected extracted fact: %s", content[:50])
                continue
            stored_ids.append(result.id)
            stored += 1

        if stored:
            logger.info("Extracted %d facts from episode %s", stored, episode_id)
        return stored_ids

    async def handle(self, event: Event) -> None:
        """Handle episode_summarized — extract and store facts.

        008.4: If candidate_facts present in event data, store them directly
        without calling the LLM. Falls back to LLM extraction otherwise.
        """
        summary = event.data.get("summary", {})
        if not summary:
            return

        # F025 P2-E: Extract transcript for fact grounding
        transcript = event.data.get("transcript")

        try:
            # F051.5: delegate to the directly-invocable core. Pass
            # event.data.candidate_facts explicitly because production paths
            # carry them at the event-data level, NOT inside summary.
            # Return value discarded — production path doesn't need the UUIDs.
            await self.extract_and_store(
                summary=summary,
                episode_id=event.data.get("episode_id", "?"),
                transcript=transcript,
                candidate_facts=event.data.get("candidate_facts", []),
            )
        except Exception:
            logger.exception("Fact extraction failed for episode %s", event.data.get("episode_id"))

    async def _store_candidate_facts(self, candidates: list[str | dict], episode_id: str, transcript: str | None = None) -> list[UUID]:
        """008.4: Store pre-extracted candidate facts directly, with dedup.

        Accepts both structured dicts (with subject/category/content) and
        plain strings for backward compatibility.

        F051.5: returns list of fact UUIDs that materially exist after this
        call — newly-stored UUIDs PLUS canonical UUIDs of dedup-skipped
        facts. handle() callers ignore the return; ingest paths consume it.
        """
        stored_ids: list[UUID] = []
        stored = 0
        # F075: split dated and stable candidates with separate caps so dated
        # events from later chunks aren't dropped by the [:5] truncation.
        # The summarizer's _merge_summaries already partitions; mirror that
        # discipline here so a partial round-trip (e.g. test paths bypassing
        # the summarizer) doesn't re-truncate.
        dated = [c for c in candidates if isinstance(c, dict) and c.get("event_date")]
        stable = [c for c in candidates if not (isinstance(c, dict) and c.get("event_date"))]
        event_limit = getattr(self._settings, "candidate_facts_event_limit", 30)
        capped = dated[:event_limit] + stable[:5]
        for item in capped:
            # Handle both structured dicts and plain strings
            if isinstance(item, dict):
                content = item.get("content", "")
                subject = item.get("subject")
                category = item.get("category")
                # F075: normalize candidate event_date through the validator so
                # the dedup compare is type-safe (date == date, not str == str).
                raw_event_date = item.get("event_date")
                candidate_event_date = (
                    FactInput(content="_", event_date=raw_event_date).event_date
                    if raw_event_date is not None
                    else None
                )
            else:
                content = item
                subject = None
                category = None
                raw_event_date = None
                candidate_event_date = None

            if not content or not str(content).strip():
                continue

            # Dedup: check if similar fact exists. _resolve_dedup examines all
            # above-threshold RRF hits (F377) + the F075 distinct-date bypass;
            # None = store as new.
            canonical = await self._resolve_dedup(content, candidate_event_date)
            if canonical is not None:
                stored_ids.append(canonical)
                logger.debug("Dedup skip (candidate) — adding canonical UUID %s for: %s", canonical, content[:50])
                continue

            # F075: producer-path classification marker — gated on flag.
            # The summarizer's prompt DOES include event_date (Layer 1a in spec)
            # so the "classified, no date found" terminal-state contract applies
            # (NOT NULL classified_at + NULL event_date = "we tried, no date").
            #
            # BUT: do NOT stamp when the LLM emitted a date the validator
            # dropped as malformed (e.g. "2024-3-10", "March 10"). Stamping
            # there would permanently lock the row out of F075.1 backfill even
            # though a real date was present — a silent, permanent data loss
            # on exactly the rows F075 exists to capture (SFH final-review
            # Medium). Leave classified_at NULL so backfill can retry it.
            flag_on = getattr(self._settings, "temporal_extraction_enabled", False)
            date_dropped = raw_event_date is not None and candidate_event_date is None
            classified_at = datetime.now(UTC) if (flag_on and not date_dropped) else None
            # F022 orphan-rate audit fix: tag candidate facts with their
            # source episode (same change as the LLM-fallback path above).
            fact_input = FactInput(
                content=content,
                subject=subject or "unknown",
                category=category,
                source="episode_summarizer",
                confidence=0.8,  # Default confidence for LLM-extracted candidates
                source_text=transcript,  # F025 P2-E
                source_episode_id=_parse_episode_uuid(episode_id),
                event_date=candidate_event_date,
                event_date_classified_at=classified_at,
            )
            result = await self._heart.learn(fact_input)
            if isinstance(result, FactRejected):
                logger.debug("Admission rejected candidate fact: %s", content[:50])
                continue
            stored_ids.append(result.id)
            stored += 1

        if stored:
            logger.info(
                "Stored %d candidate facts from episode %s",
                stored,
                episode_id,
            )
        return stored_ids

    async def _extract_facts(self, summary: dict[str, Any]) -> list[dict[str, Any]]:
        """Call LLM to extract facts from episode summary."""
        if not self._llm:
            return []

        summary_text = summary.get("summary", "")
        key_points = ", ".join(summary.get("key_points", []))

        if not summary_text:
            return []

        prompt = _EXTRACT_PROMPT.format(summary=summary_text, key_points=key_points)

        text = await call_background_llm(
            self._llm,
            model=self._settings.background_model,
            system_prompt="You are extracting facts from an AI agent's conversation summary.",
            user_message=prompt,
            max_tokens=1500,
        )

        if not text:
            return []

        try:
            return parse_llm_json(text)
        except json.JSONDecodeError:
            return []
