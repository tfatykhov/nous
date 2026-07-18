"""Fact management — semantic memory (what we know).

Manages facts with provenance, deduplication, superseding, and contradiction.
All methods follow Brain's session injection pattern (P1-1).
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import and_, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from nous.brain.embeddings import EmbeddingProvider
from nous.heart.admission import AdmissionController, AdmissionResult
from nous.heart.keys import is_keyable_entity, normalize_key
from nous.heart.schemas import ContradictionWarning, FactDetail, FactInput, FactRejected, FactSummary
from nous.heart.search import hybrid_search, hybrid_search_multi, set_local_ef_search
from nous.storage.database import Database
from nous.storage.models import Episode, Event, Fact, FactEntityKey, GraphEdge

if TYPE_CHECKING:
    from nous.config import Settings
    from nous.handlers import LLMClient
    from nous.heart.actionability import ActionabilityClassifier
    from nous.heart.date_window import DateWindow

logger = logging.getLogger(__name__)

# R3.3 (F085) codex P2: entity-key vocabulary cache TTL, in seconds. Lives on
# the FactManager instance (see entity_key_vocabulary below) rather than a
# module-level cache keyed by instance, so writes from THIS process invalidate
# it immediately instead of waiting out the TTL.
_ENTITY_VOCAB_TTL_SECONDS = 300.0

# F027: classifier prompt. Exported so the F027 eval script
# (scripts/eval/eval_f027_supersession.py) tests the same prompt prod uses.
# Apply this decision tree IN ORDER, returning the first match. UPDATE is
# listed first to counter the empirically measured CONTRADICTION-bias
# (issue #382): a stronger judge model amplified the bias rather than fixing
# it, so the fix must live in the prompt framing.
_SUPERSESSION_CLASSIFIER_PROMPT_TEMPLATE = (
    "Compare these two facts and classify their relationship.\n\n"
    "OLD fact: {old}\n\n"
    "NEW fact: {new}\n\n"
    "Step 1 — Subject overlap test. Do the two facts describe the SAME subject and "
    "the SAME aspect/property of that subject? If they share keywords but actually "
    "talk about different subjects, different entities, or different aspects, "
    "return UNRELATED. (Surface word-overlap is NOT subject overlap.)\n\n"
    "Step 2 — If subjects overlap, apply this decision tree IN ORDER and return "
    "the FIRST matching label:\n\n"
    "  A. REFINEMENT — The NEW fact adds detail or specificity without "
    "contradicting the OLD fact. Both can be simultaneously true; the NEW fact is "
    "just narrower or more precise.\n\n"
    "  B. UPDATE — The property described is inherently MUTABLE over time "
    "(schedule, status, value, count, version, location, configuration, role, "
    "ownership, price, quantity), the NEW fact reflects the current state, and "
    "the OLD fact was likely correct at an earlier time. Use UPDATE when the two "
    "facts disagree about a mutable property and time passing could plausibly "
    "explain the difference.\n\n"
    "  C. CONTRADICTION — The property described is INHERENTLY FIXED (identity, "
    "definition, historical fact, mathematical/logical claim, intrinsic "
    "characteristic), AND the two facts make incompatible claims about it. "
    "Or: the property is mutable but the two facts both claim to describe the "
    "same point in time and yet disagree. Use CONTRADICTION sparingly — only "
    "when no temporal interpretation reconciles the two facts.\n\n"
    "Examples to disambiguate:\n"
    "- 'X meeting is at 3pm' vs 'X meeting is at 5pm' → UPDATE (schedules move)\n"
    "- 'API returns 200' vs 'API returns 500' → UPDATE (status changes)\n"
    "- 'Pi equals 3.14' vs 'Pi equals 4' → CONTRADICTION (math is fixed)\n"
    "- 'Tim works at Acme' vs 'Tim works at Globex' → UPDATE (employment changes)\n"
    "- 'The config file is settings.yaml' vs 'The config file is config.toml' → "
    "UPDATE (configuration changes)\n"
    "- 'Database uses Postgres' vs 'Cache uses Redis' → UNRELATED (different "
    "subsystems despite shared 'uses' verb)\n\n"
    "For `current_fact`: 'new' for UPDATE/REFINEMENT; for CONTRADICTION it is "
    "advisory only (the caller resolves by statement order, NOT by which claim "
    "is true); 'new' by default for UNRELATED."
)


# F027: JSON schema for structured supersession classifier output
_SUPERSESSION_CLASSIFIER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "relation": {
            "type": "string",
            "enum": ["UPDATE", "CONTRADICTION", "REFINEMENT", "UNRELATED"],
            "description": "How the two facts relate to each other",
        },
        "current_fact": {
            "type": "string",
            "enum": ["new", "old"],
            "description": (
                "Which statement was made later / supersedes the other (advisory "
                "for CONTRADICTION — callers resolve by statement order)"
            ),
        },
        "confidence": {
            "type": "number",
            "minimum": 0.0,
            "maximum": 1.0,
            "description": "Confidence in this classification (0.0 to 1.0)",
        },
    },
    "required": ["relation", "current_fact", "confidence"],
}


# F377: Leg-1 dedup tiebreaker prompt. Purpose-built binary same-vs-distinct
# classifier (the supersession classifier above has no SAME/DUPLICATE label, so
# paraphrases map ambiguously). Used only when the RRF pre-check flagged a dup.
_DEDUP_TIEBREAKER_PROMPT_TEMPLATE = (
    "Two short facts were flagged as possible duplicates by keyword search. "
    "Decide whether they state the SAME fact or DIFFERENT facts.\n\n"
    "EXISTING fact: {existing}\n\n"
    "CANDIDATE fact: {candidate}\n\n"
    "Return DUPLICATE only if the candidate is a restatement or paraphrase of "
    "the existing fact with no materially different claim. Return DISTINCT if "
    "they differ in any value, sign, polarity, quantity, direction, entity, or "
    "time, or if one negates or contradicts the other — even when the wording "
    "is very similar (e.g. 'metric down 5%' vs 'metric up 5%' are DISTINCT)."
)

_DEDUP_TIEBREAKER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "verdict": {
            "type": "string",
            "enum": ["DUPLICATE", "DISTINCT"],
            "description": "DUPLICATE if the candidate restates the existing fact; "
            "DISTINCT if it makes any materially different or contradictory claim.",
        },
    },
    "required": ["verdict"],
}


class FactManager:
    """Manages semantic memory — what we know."""

    # Threshold for domain fact count before emitting compaction event
    DOMAIN_COMPACTION_THRESHOLD = 10

    # Re-emit threshold event every N facts above threshold
    DOMAIN_COMPACTION_INTERVAL = 5

    # Similarity range for contradiction detection (between dedup and unrelated)
    CONTRADICTION_SIMILARITY_MIN = 0.85
    CONTRADICTION_SIMILARITY_MAX = 0.95  # Above this is dedup, not contradiction

    def __init__(
        self,
        db: Database,
        embeddings: EmbeddingProvider | None,
        agent_id: str,
        admission_controller: AdmissionController | None = None,
        actionability_classifier: "ActionabilityClassifier | None" = None,
        settings: "Settings | None" = None,
    ) -> None:
        self.db = db
        self.embeddings = embeddings
        self.agent_id = agent_id
        self._admission_controller = admission_controller
        # F027: LLM client for write-time supersession classifier
        self._llm: LLMClient | None = None
        self._llm_model: str = "claude-haiku-4-5-20251001"
        # F047: Actionability classifier for write-time verdict
        self._actionability_classifier = actionability_classifier
        # F056 #377: Settings injected so _find_duplicate can read
        # fact_native_cosine_threshold (was hardcoded 0.95). Optional
        # for backwards-compat; defaults to 0.95 when settings is None.
        self._settings = settings
        # 1a: advisory per-hour cap on in-band classifier (Haiku) calls.
        self._band_bucket: int = -1
        self._band_calls: int = 0
        # 064 R2: advisory per-hour cap on key-conflict classifier calls.
        self._key_bucket: int = -1
        self._key_calls: int = 0
        # R3.3 (F085) codex P2: entity-key vocabulary cache, invalidated at
        # both entity-row write sites (see entity_key_vocabulary below).
        self._entity_vocab_cache: tuple[frozenset[str], float] | None = None
        # codex P2 round 3: generation counter, bumped at both entity-row
        # write sites AND by learn()'s post-commit (per-call) invalidation.
        # entity_key_vocabulary() snapshots this before its DB round-trip and
        # only STORES its result if nothing bumped it in the meantime —
        # closes the "late store" race: a rebuild that STARTS before a write
        # invalidates the cache but FINISHES after can otherwise clobber the
        # cache with a stale result.
        #
        # codex P2 round 5: round 2 originally gated learn()'s post-commit
        # re-invalidation on a SHARED `self._entity_vocab_dirty` boolean —
        # that broke under two overlapping learn() calls with entity_keys,
        # since whichever call's `finally` ran first would clear the flag on
        # behalf of BOTH, silently skipping the second call's own post-commit
        # invalidation. Removed entirely; see learn()'s finally block, which
        # now checks its OWN call-local `input.entity_keys` instead of any
        # instance-shared state.
        self._entity_vocab_gen: int = 0

    def _band_budget_ok(self) -> bool:
        """Advisory in-process per-hour cap on the in-band classifier. Returns
        False once the hourly budget is spent (band then falls open to confirm).
        Races are harmless for a cost cap, so no lock. 0/None disables."""
        cap = getattr(self._settings, "fact_band_classification_max_per_hour", 1000) if self._settings else 1000
        if not cap or cap <= 0:
            return True
        bucket = int(time.monotonic() // 3600)
        if bucket != self._band_bucket:
            self._band_bucket = bucket
            self._band_calls = 0
        if self._band_calls >= cap:
            return False
        self._band_calls += 1
        return True

    def _key_budget_ok(self) -> bool:
        """RC-5: advisory per-hour cap on key-conflict classifier calls.
        Mirrors _band_budget_ok. Spent budget => fail open to KEEP-BOTH."""
        cap = getattr(self._settings, "supersession_classifier_max_per_hour", 500) if self._settings else 500
        if not cap or cap <= 0:
            return True
        bucket = int(time.monotonic() // 3600)
        if bucket != self._key_bucket:
            self._key_bucket = bucket
            self._key_calls = 0
        if self._key_calls >= cap:
            return False
        self._key_calls += 1
        return True

    def key_budget_exhausted(self) -> bool:
        """Non-consuming peek at the hourly key-classifier budget. Rolls the
        hour bucket forward (resetting the counter) exactly like _key_budget_ok,
        but never consumes a slot."""
        cap = getattr(self._settings, "supersession_classifier_max_per_hour", 500) if self._settings else 500
        if not cap or cap <= 0:
            return False
        bucket = int(time.monotonic() // 3600)
        if bucket != self._key_bucket:
            self._key_bucket = bucket
            self._key_calls = 0
        return self._key_calls >= cap

    # ------------------------------------------------------------------
    # Event helper
    # ------------------------------------------------------------------

    async def _emit_event(self, session: AsyncSession, event_type: str, data: dict) -> None:
        """Insert event in same session (P2-1)."""
        event = Event(
            agent_id=self.agent_id,
            event_type=event_type,
            data=data,
        )
        session.add(event)

    async def _create_graph_edge(
        self,
        source_id: UUID,
        target_id: UUID,
        source_type: str,
        target_type: str,
        relation: str,
        weight: float,
        session: AsyncSession,
    ) -> None:
        """F022: Create a graph edge as side effect of fact operations.

        Uses a nested savepoint so failures don't abort the outer transaction.
        """
        try:
            from nous.brain.edge_provenance import classify  # F065
            async with session.begin_nested():
                stmt = (
                    pg_insert(GraphEdge)
                    .values(
                        source_id=source_id,
                        target_id=target_id,
                        source_type=source_type,
                        target_type=target_type,
                        agent_id=self.agent_id,
                        relation=relation,
                        weight=weight,
                        auto_linked=True,
                        extraction_method=classify(relation),  # F065
                    )
                    .on_conflict_do_nothing(
                        index_elements=["source_id", "target_id", "relation"],
                    )
                )
                await session.execute(stmt)
        except Exception:
            logger.debug("F022 graph edge creation failed for %s->%s", source_id, target_id)

    # ------------------------------------------------------------------
    # F027: LLM client setter
    # ------------------------------------------------------------------

    def set_llm_client(self, client: LLMClient, model: str | None = None) -> None:
        """F027: Configure LLM client for write-time supersession classifier."""
        self._llm = client
        if model:
            self._llm_model = model

    # ------------------------------------------------------------------
    # F027: Access tracking
    # ------------------------------------------------------------------

    async def track_access(self, fact_ids: list[UUID]) -> None:
        """Update recall_count and last_recalled_at for accessed facts."""
        if not fact_ids:
            return
        try:
            async with self.db.session() as session:
                stmt = (
                    update(Fact)
                    .where(Fact.id.in_(fact_ids))
                    .values(
                        recall_count=Fact.recall_count + 1,
                        last_recalled_at=datetime.now(UTC),
                    )
                )
                await session.execute(stmt)
                await session.commit()
        except Exception:
            logger.debug("F027: access tracking failed for %d facts", len(fact_ids))

    def _fire_track_access(self, fact_ids: list[UUID]) -> None:
        """Fire-and-forget access tracking."""
        if fact_ids:
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self.track_access(fact_ids))
            except RuntimeError:
                pass  # No running event loop

    # ------------------------------------------------------------------
    # F027: LLM supersession classifier
    # ------------------------------------------------------------------

    async def _classify_fact_pair(
        self, old_content: str, new_content: str
    ) -> dict | None:
        """Classify relationship between two facts using LLM.

        Returns {relation, current_fact, confidence} or None on failure.
        """
        if self._llm is None:
            return None

        from nous.handlers import call_background_llm_structured

        prompt = _SUPERSESSION_CLASSIFIER_PROMPT_TEMPLATE.format(
            old=old_content[:500],
            new=new_content[:500],
        )

        return await call_background_llm_structured(
            client=self._llm,
            model=self._llm_model,
            system_prompt="You are a memory management classifier. Analyze fact relationships precisely.",
            user_message=prompt,
            tool_name="classify_facts",
            tool_description="Classify the relationship between two facts.",
            output_schema=_SUPERSESSION_CLASSIFIER_SCHEMA,
            max_tokens=300,
        )

    async def is_distinct_fact(
        self, existing_content: str, candidate_content: str
    ) -> bool | None:
        """F377 dedup tiebreaker for the Leg-1 (RRF pre-check) path.

        Returns ``True`` if the candidate is a DISTINCT fact (store it, do not
        dedup), ``False`` if it is a DUPLICATE/paraphrase (dedup), or ``None``
        if the LLM is unavailable or the call fails. Callers MUST fail open on
        ``None`` — i.e. preserve the pre-tiebreaker behaviour (dedup) so the
        learn path is never blocked by a tiebreaker outage.
        """
        if self._llm is None:
            return None

        from nous.handlers import call_background_llm_structured

        prompt = _DEDUP_TIEBREAKER_PROMPT_TEMPLATE.format(
            existing=existing_content[:500],
            candidate=candidate_content[:500],
        )
        try:
            result = await call_background_llm_structured(
                client=self._llm,
                model=self._llm_model,
                system_prompt="You are a memory deduplication classifier. "
                "Decide whether two facts are the same or distinct.",
                user_message=prompt,
                tool_name="classify_dedup",
                tool_description="Decide whether two facts are duplicates or distinct.",
                output_schema=_DEDUP_TIEBREAKER_SCHEMA,
                max_tokens=100,
            )
        except Exception:
            logger.debug("F377 dedup tiebreaker call failed; failing open", exc_info=True)
            return None

        if not result or "verdict" not in result:
            return None
        return result["verdict"] == "DISTINCT"

    async def _same_slot_value_variant(self, input: FactInput, dupe: Fact) -> bool:
        """True when the same-slot pair differs in VALUE (route to conflict
        resolution); False when it is the same statement (confirm as dup).
        Fail-open TO STORE (True): dropping an update is silent data loss
        (audit S1). A stored near-dup is only recoverable by the sleep sweep
        when supersession_key_resolution_enabled (R2) is on — the sweep
        early-returns as a no-op when R2 is off. With R2 off (and no
        input.subject to engage the legacy _supersede_by_subject path), the
        pair simply accumulates KEEP-BOTH, unflagged — fail-safe (existence
        preserved, unmerged) rather than recovered."""
        folded_in = " ".join(input.content.lower().split())
        folded_dupe = " ".join((dupe.content or "").lower().split())
        if folded_in == folded_dupe:
            return False                      # identical statement -> dedup
        if not self._band_budget_ok():
            return True                       # budget spent -> STORE (never swallow)
        verdict = await self.is_distinct_fact(dupe.content, input.content)
        if verdict is None:
            return True                       # LLM down -> STORE (never swallow)
        return verdict                        # DISTINCT -> route; DUPLICATE -> dedup

    # ------------------------------------------------------------------
    # F027: Retrieval soft suppression
    # ------------------------------------------------------------------

    @staticmethod
    def apply_supersession_filter(results: list[FactSummary]) -> list[FactSummary]:
        """Apply soft suppression to superseded facts in search results.

        - If superseded_by is set and superseder is in result set -> drop old fact
        - If superseded_by is set but superseder absent -> score *= 0.3
        - If confidence < 0.5 -> score *= confidence
        Uses model_copy for immutability. Re-sorts by adjusted score.
        """
        if not results:
            return results

        result_ids = {r.id for r in results}
        filtered: list[FactSummary] = []

        for r in results:
            if r.superseded_by is not None:
                if r.superseded_by in result_ids:
                    # Superseder present in results — drop old fact entirely
                    continue
                # Superseder absent — soft penalty
                adjusted_score = (r.score or 0.0) * 0.3
                r = r.model_copy(update={"score": adjusted_score})

            if r.confidence < 0.5:
                adjusted_score = (r.score or 0.0) * r.confidence
                r = r.model_copy(update={"score": adjusted_score})

            filtered.append(r)

        # Re-sort by adjusted score descending
        filtered.sort(key=lambda x: x.score or 0.0, reverse=True)
        return filtered

    # ------------------------------------------------------------------
    # learn()
    # ------------------------------------------------------------------

    async def learn(
        self,
        input: FactInput,
        exclude_ids: list[UUID] | None = None,
        check_contradictions: bool = True,
        session: AsyncSession | None = None,
        encoded_frame: str | None = None,
        encoded_censors: list[str] | None = None,
        precomputed_embedding: list[float] | None = None,
    ) -> FactDetail | FactRejected:
        """Store a new fact with deduplication.

        Args:
            input: Fact data to store.
            exclude_ids: Fact IDs to exclude from dedup check (P1-2).
                Used by supersede/contradict to avoid matching the old fact.
            check_contradictions: Whether to check for contradictions and
                domain thresholds. Set False for bulk imports. Default True.
            session: Optional session for transaction injection.
            encoded_frame: Frame active when this fact was learned (003.2).
            encoded_censors: Censors active when this fact was learned (003.2).
            precomputed_embedding: Pre-computed vector to use verbatim instead
                of calling the embedder.  Enables RC-2 batched ingest (Task 4).
        """
        # W-1: precompute the admission LLM utility BEFORE opening the write
        # session, so the Haiku call doesn't hold a pooled connection through
        # the dedup/insert transaction (+ the W-8 advisory lock). Gated on the
        # same <30-char floor _learn applies, and skipped for bypass sources
        # inside precompute_utility — so the only wasted call is for a fact that
        # later turns out to be a Leg-2 dupe (rare; Leg-1 filters most upstream).
        #
        # Only precompute when learn() owns the session (`session is None`).
        # With an injected session the caller's transaction is already open
        # (e.g. CognitiveLayer.end_session updates the episode on its session
        # before calling heart.learn(..., session=session)), so running the
        # Haiku call here would still pin the caller's connection — no better
        # than the inline score() call. For injected sessions we pass
        # utility_override=None and score() computes utility inline, exactly as
        # before W-1. Moving the call ahead of an injected caller's transaction
        # is that caller's responsibility.
        utility_override: float | None = None
        _min_chars_gate = self._settings.fact_min_content_chars if self._settings else 30
        if input.source == "enumerative_extractor" and self._settings is not None:
            _min_chars_gate = self._settings.enumerative_min_content_chars
        if (
            session is None
            and self._admission_controller is not None
            and (_min_chars_gate == 0 or len(input.content.strip()) >= _min_chars_gate)
        ):
            utility_override = await self._admission_controller.precompute_utility(input)

        if session is None:
            async with self.db.session() as session:
                try:
                    result = await self._learn(
                        input,
                        list(exclude_ids or []),
                        check_contradictions,
                        session,
                        encoded_frame=encoded_frame,
                        encoded_censors=encoded_censors,
                        utility_override=utility_override,
                        precomputed_embedding=precomputed_embedding,
                    )
                    await session.commit()
                    return result
                finally:
                    # codex P2 round 2: _learn's in-txn cache=None (at either
                    # entity-write site) can be undone by a concurrent
                    # recall that rebuilds + re-caches entity_key_vocabulary()
                    # before THIS transaction commits — under READ COMMITTED
                    # isolation that rebuild can't see the uncommitted row,
                    # so it re-caches a stale vocab with no further trigger
                    # to invalidate it for the rest of the TTL. Re-invalidate
                    # here, after commit succeeds (the common case) or on
                    # exception (a poisoned cache is still worth clearing —
                    # it's only a cache, so over-invalidating on failure or
                    # on a rejected fact is harmless).
                    # codex P2 round 3: also bump the generation counter here
                    # — a rebuild that captured `gen` in the narrow window
                    # AFTER the write site's own bump but BEFORE this commit
                    # (so it still read the pre-commit, stale data) needs
                    # this SECOND bump to be detected as stale; the write
                    # site's bump alone only protects readers that captured
                    # `gen` before the write started.
                    # codex P2 round 5: gate this on the CALL-LOCAL
                    # `input.entity_keys` rather than a shared instance flag.
                    # A shared `self._entity_vocab_dirty` broke under two
                    # overlapping learn() calls with entity_keys: whichever
                    # call's `finally` ran first would clear the flag the
                    # OTHER call had set, so the second call's own commit
                    # never triggered its own post-commit invalidation —
                    # `input` is this call's own local variable, so there is
                    # no cross-talk between concurrent calls.
                    # codex P2 round 13: reuse invalidate_entity_vocab() (DRY)
                    # — the same post-commit invalidation is now also needed
                    # at inherit_conflict_slot_keys's caller-owned commit
                    # points (supersede, sleep_handler merge sites).
                    # codex P2 round 16: widened to also cover a subject-only
                    # write (no entity_keys) — _learn's insert block now
                    # seeds an entity row from input.subject_key alone, so
                    # this gate must match its own `input.subject_key or
                    # input.entity_keys` condition.
                    if input.entity_keys or input.subject_key:
                        self.invalidate_entity_vocab()
        # Injected-session callers own their own commit point, so there is no
        # place here to re-invalidate post-commit — the 300s TTL remains the
        # sole staleness bound for this path. No current caller passes
        # entity_keys through an injected session.
        return await self._learn(
            input,
            list(exclude_ids or []),
            check_contradictions,
            session,
            encoded_frame=encoded_frame,
            encoded_censors=encoded_censors,
            utility_override=utility_override,
            precomputed_embedding=precomputed_embedding,
        )

    async def _embed_with_retry(self, embed_text: str, *, attempts: int = 2) -> list[float] | None:
        """Embed with one retry; on final failure log ERROR (not a quiet warning).

        Mirrors ``ProcedureManager._embed_with_retry``. A transient embedding
        outage would otherwise insert a burst of facts with NULL embeddings —
        invisible to vector search AND the dedup cosine gate, i.e. permanently
        undedupable duplicates. The retry absorbs the common transient blip; on
        persistent failure we still persist the fact (never hard-delete memory)
        but with a LOUD error so a NULL row is recoverable, not silent.

        Returns ``None`` when no provider is configured (intentional — the row is
        stored without dedup) OR after all attempts fail (distinguished only by
        the log level: no-provider is silent, failure is ERROR).
        """
        if not self.embeddings:
            return None
        for attempt in range(1, attempts + 1):
            try:
                return await self.embeddings.embed(embed_text)
            except Exception:
                if attempt >= attempts:
                    logger.error(
                        "Embedding generation failed after %d attempts; storing fact with "
                        "NULL embedding (invisible to vector search/dedup until re-embedded)",
                        attempts,
                    )
                    return None
                logger.warning("Embedding attempt %d/%d failed for fact learn, retrying", attempt, attempts)
        return None

    async def _learn(
        self,
        input: FactInput,
        exclude_ids: list[UUID],
        check_contradictions: bool,
        session: AsyncSession,
        *,
        encoded_frame: str | None = None,
        encoded_censors: list[str] | None = None,
        utility_override: float | None = None,
        precomputed_embedding: list[float] | None = None,
    ) -> FactDetail | FactRejected:
        # F038-1.2: Reject facts with content < fact_min_content_chars characters.
        # 0 disables the gate entirely (useful for testing / low-noise corpora).
        min_chars = self._settings.fact_min_content_chars if self._settings else 30
        if input.source == "enumerative_extractor" and self._settings is not None:
            min_chars = self._settings.enumerative_min_content_chars
        if min_chars and len(input.content.strip()) < min_chars:
            logger.info(
                "Fact rejected by min-content floor (%d < %d): %.60s",
                len(input.content.strip()), min_chars, input.content,
            )
            return FactRejected(
                content=input.content,
                composite_score=0.0,
                threshold=0.0,
                scores={},
                explanation=f"Content too short (< {min_chars} chars)",
            )

        # W-8: serialize concurrent learns of identical content for this agent so
        # two racing callers (e.g. the fact_extractor tiebreaker runs its
        # LLM check outside any transaction) can't both pass dedup and
        # double-insert. The transaction-scoped advisory lock releases on
        # commit/rollback and is keyed per (agent, content), so distinct content
        # never serializes. The existing event_date-aware dedup below then runs
        # under the lock and decides correctly (F075 distinct-date facts still
        # coexist — the lock only orders the racers, it doesn't dedup).
        # Postgres-only (pg_advisory_xact_lock); the SQLite test backend is
        # serial, so there is no cross-connection race to protect there.
        if session.bind is not None and session.bind.dialect.name == "postgresql":
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
                {"k": f"fact_learn:{self.agent_id}:{input.content}"},
            )

        # Generate embedding (retry once; persistent failure logs ERROR and
        # stores a NULL-embed row rather than dropping the fact — 1b).
        # RC-2: when caller provides a precomputed vector, skip the embedder
        # entirely so batched ingest (Task 4) can embed once per batch.
        embedding = (
            precomputed_embedding
            if precomputed_embedding is not None
            else await self._embed_with_retry(input.content)
        )

        # Near-duplicate detection: cosine similarity > threshold.
        # F075: pass candidate's event_date so _find_duplicate prefers same-date
        # matches over different-date ones above threshold.
        #
        # Audit S3 (2026-06-09): when the operator lowers the dedup threshold
        # below the contradiction band (prod runs 0.80 vs band 0.85-0.95),
        # a blind _confirm would swallow the band entirely — a CONFLICTING
        # fact at 0.86 would increment confirmation_count on the stale fact
        # and be discarded. So in-band dupes are classified (same F027
        # classifier _find_contradiction uses) BEFORE confirming, and routed:
        # UNRELATED/REFINEMENT/UPDATE-new/CONTRADICTION proceed to insert
        # with a post-flush action; only UPDATE-old, low-confidence verdicts,
        # classifier failure, or out-of-band similarity confirm the dupe.
        band_action: str | None = None
        band_dupe: Fact | None = None
        band_sim: float = 0.0
        routed_dupe_id: UUID | None = None  # Gate-1 D2: same-slot value-variant routed past dedup
        if embedding is not None:
            # Codex r7: tell the selector which slot the D2 guard cares about
            # so a cross-slot near-duplicate (closer in embedding space, but
            # from an unrelated conflict slot) cannot mask a same-slot
            # value-variant sitting just below it — the guard can only route
            # what _find_duplicate surfaces. Only set when the guard could
            # actually fire below (mirrors its own gating minus the dupe-side
            # key check, which isn't known until a candidate is selected);
            # every other learn() call passes prefer_slot=None and is
            # unaffected.
            prefer_slot = (
                (input.subject_key, input.attribute_key)
                if (
                    check_contradictions
                    and getattr(self._settings, "same_slot_conflict_routing_enabled", True)
                    and input.subject_key and input.attribute_key
                )
                else None
            )
            found = await self._find_duplicate(
                embedding, exclude_ids, session,
                candidate_event_date=input.event_date,
                prefer_slot=prefer_slot,
            )
            if found is not None:
                dupe, dupe_similarity = found
                # F075 dedup bypass: distinct event_dates = distinct events.
                # Same entity, same content shape, but on different dates is
                # not a duplicate — it's a separate event temporal_reasoning
                # needs preserved (e.g., API key obtained March 10 vs rotated
                # March 12). Fall through to session.add(fact) below.
                if (
                    input.event_date is not None
                    and dupe.event_date is not None
                    and input.event_date != dupe.event_date
                ):
                    pass  # do NOT return; treat as new event
                elif (
                    # Codex r6: routing exists to FEED a resolver
                    # (_resolve_key_conflicts / legacy _supersede_by_subject /
                    # _find_contradiction) — every one of those is itself gated
                    # on check_contradictions. When the caller disables conflict
                    # work (e.g. a bulk-import/sentinel path), routing would
                    # insert an unadjudicated near-dupe that NO resolver will
                    # ever see, accumulating silently. First term so False
                    # short-circuits before the settings getattr or the
                    # is_distinct_fact budget/LLM call below.
                    check_contradictions
                    and getattr(self._settings, "same_slot_conflict_routing_enabled", True)
                    and input.subject_key and input.attribute_key
                    and dupe.subject_key and dupe.attribute_key
                    and input.subject_key == dupe.subject_key
                    and input.attribute_key == dupe.attribute_key
                    and await self._same_slot_value_variant(input, dupe)
                ):
                    # Gate-1 D2: a same-conflict-slot pair with a DIFFERENT value is
                    # an update/contradiction, not a duplicate — it must reach the
                    # F027/F084 resolver. The blind-confirm at similarity >= 0.95
                    # (CONTRADICTION_SIMILARITY_MAX) was silently swallowing exactly
                    # these (39pp of MAB answer statements). Fall through to INSERT;
                    # with R2 (supersession_key_resolution_enabled) on, adjudication
                    # happens in _resolve_key_conflicts below. With R2 OFF, the sleep
                    # sweep (_phase_sweep_key_conflicts) does NOT recover this pair —
                    # it early-returns when the flag is off — so resolution falls to
                    # the legacy _supersede_by_subject path, and only when the caller
                    # also set input.subject (later wins); if neither applies, the
                    # pair simply accumulates KEEP-BOTH, unflagged. Fail-safe: this
                    # still beats the pre-fix silent swallow — both facts exist and
                    # are retrievable, they are just unmerged. The dupe is shielded
                    # ONLY from _find_contradiction re-processing (safe_excludes
                    # below) — it MUST remain visible to _resolve_key_conflicts.
                    routed_dupe_id = dupe.id
                else:
                    band_action = await self._classify_dupe_in_band(
                        dupe, dupe_similarity, input, check_contradictions
                    )
                    if band_action is None:
                        # Confirm existing fact instead of creating new
                        return await self._confirm_duplicate(dupe, input, session)
                    # Routed: insert proceeds; exclude the dupe from the
                    # supersession + contradiction passes below so it is
                    # not re-classified or superseded against the verdict.
                    band_dupe = dupe
                    band_sim = dupe_similarity
                    exclude_ids.append(dupe.id)

        # F023: Admission gate — score candidate before storage
        admission_result: AdmissionResult | None = None
        if self._admission_controller is not None:
            # Codex r2: the routed dupe (Gate-1 D2) must NOT feed this
            # candidate's novelty term — it is definitionally near-identical
            # (that's why it hit the >= fact_native_cosine_threshold dedup
            # check), so leaving it visible to _find_max_similarity would
            # collapse novelty to ~0 and let admission REJECT the very pair
            # R2 must adjudicate, reintroducing a drop path. Admission-only
            # exclusion list — exclude_ids itself (which feeds
            # _resolve_key_conflicts below) is untouched; R2 visibility is
            # unchanged (Amendment 1).
            #
            # Codex r4: the routed dupe alone is insufficient when the
            # conflict slot holds MORE than one active variant — a second,
            # non-routed same-slot fact (one _find_duplicate's cosine
            # threshold didn't flag, because its embedding differs, but
            # _find_max_similarity's nearest-active-fact scan still finds)
            # can equally collapse novelty and get this candidate
            # admission-rejected before R2 ever sees the cluster. When
            # routing fired, exclude every active same-slot fact. Served by
            # idx_facts_conflict_slot.
            #
            # Codex r5: UNCAPPED — do not bound this query by
            # supersession_key_candidates_cap. That cap bounds R2's
            # ADJUDICATION reach (which pairs get resolved after storage);
            # this exclusion protects STORAGE itself (admission runs
            # BEFORE the fact exists). A cluster member outside R2's cap can
            # still be the nearest active neighbor and zero novelty, so
            # correctness here must not depend on the cap — real conflict
            # clusters are small in practice.
            if routed_dupe_id is not None:
                same_slot_rows = await session.execute(
                    select(Fact.id)
                    .where(
                        Fact.agent_id == self.agent_id,
                        Fact.active == True,  # noqa: E712
                        Fact.subject_key == input.subject_key,
                        Fact.attribute_key == input.attribute_key,
                    )
                )
                same_slot_ids = [row[0] for row in same_slot_rows.all()]
                admission_excludes = list({*exclude_ids, routed_dupe_id, *same_slot_ids})
            else:
                admission_excludes = exclude_ids
            max_sim = await self._find_max_similarity(embedding, admission_excludes, session) if embedding else None
            source_text = await self._get_source_text(input, session)

            admission_result = await self._admission_controller.score(
                input, embedding, max_sim, source_text, session,
                utility_override=utility_override,
            )
            if not admission_result.admitted:
                logger.info(
                    "Fact rejected by admission: %s — %s",
                    input.content[:80], admission_result.explanation,
                )
                await self._emit_event(session, "fact_rejected", {
                    "content": input.content[:200],
                    "source": input.source,
                    "scores": admission_result.scores,
                    "composite_score": admission_result.composite_score,
                })
                return FactRejected(
                    content=input.content,
                    composite_score=admission_result.composite_score,
                    threshold=admission_result.threshold,
                    scores=admission_result.scores,
                    explanation=admission_result.explanation,
                )

        # F047: Classify actionability at learn time.
        # Failure is non-fatal — fact still saved with actionable=NULL and
        # the heartbeat falls back to the legacy heuristic path for it.
        actionable_verdict: bool | None = None
        actionable_conf: float | None = None
        if self._actionability_classifier is not None:
            try:
                actionable_verdict, actionable_conf, _tier = await self._actionability_classifier.classify(
                    input.content,
                    input.category,
                    list(input.tags or []),
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("F047: actionability classify failed for new fact", exc_info=True)

        fact = Fact(
            agent_id=self.agent_id,
            content=input.content,
            category=input.category,
            subject=input.subject,
            confidence=input.confidence,
            source=input.source,
            source_episode_id=input.source_episode_id,
            source_decision_id=input.source_decision_id,
            contradiction_of=input.contradiction_of,
            tags=input.tags or None,
            embedding=embedding,
            encoded_frame=encoded_frame,
            encoded_censors=encoded_censors,
            admission_score=admission_result.composite_score if admission_result else None,
            admission_scores=(
                admission_result.scores if admission_result and not admission_result.bypassed and admission_result.scores
                else None
            ),
            actionable=actionable_verdict,
            actionable_confidence=actionable_conf,
            # F075: pure-sink semantics. _learn never injects values into
            # these fields — producers (Layer 1a/1b/Layer 4 backfill) decide.
            # Non-F075 callers (tools.py learn_fact, REST endpoint,
            # knowledge_extractor) leave both at None → backfill-eligible.
            event_date=input.event_date,
            event_date_classified_at=input.event_date_classified_at,
            subject_key=input.subject_key,
            attribute_key=input.attribute_key,
            source_ordinal=input.source_ordinal,
            overrides_prior=input.overrides_prior if input.overrides_prior else None,
        )
        session.add(fact)
        await session.flush()

        # R3.1 (F085): index entity-key rows in the same transaction as the fact.
        # codex P2 round 16: the candidate list unions the subject key FIRST
        # with entity_keys — mirrors the extractor's own subject-first union
        # and the backfill's phase_seed. Without this, a direct
        # FactInput(subject_key=..., attribute_key=...) with no entity_keys
        # wrote ZERO entity rows: visible to R2 same-key supersession (which
        # reads facts.subject_key directly) but invisible to the keyed
        # retrieval leg until an operator ran phase_seed. The block now
        # fires when EITHER subject_key or entity_keys is present.
        if input.subject_key or input.entity_keys:
            seen_keys: set[str] = set()
            max_keys = self._settings.entity_keys_max_per_fact if self._settings else 8
            min_chars = self._settings.entity_key_min_chars if self._settings else 3
            for raw_key in (input.subject_key, *input.entity_keys):
                if raw_key is None:
                    continue
                nk = normalize_key(raw_key)  # defensive re-normalize; idempotent (R3.2)
                # codex P2 round 4: enforce the same stop-policy the
                # extractor applies (enumerative_extractor.py:327) — a
                # caller passing FactInput.entity_keys directly to
                # Heart.learn bypasses the extractor entirely, so without
                # this gate junk keys ("red", "1876", "ab") would persist.
                # A scalar-only subject_key ("red") is filtered here too —
                # zero rows, no crash (round 16).
                if (
                    nk
                    and nk not in seen_keys
                    and is_keyable_entity(nk, min_chars=min_chars)
                ):
                    seen_keys.add(nk)
                    session.add(FactEntityKey(fact_id=fact.id, entity_key=nk, agent_id=self.agent_id))
                if len(seen_keys) >= max_keys:
                    break
            # codex P2: new entity keys just entered the vocab — invalidate
            # so the next recall's keyed leg sees them without waiting out
            # the TTL. codex P2 round 2: this transaction is still open (not
            # committed until learn() returns) — learn()'s finally performs
            # the actual post-commit re-invalidation (round 5: gated on this
            # call's own `input.entity_keys`, not a shared flag; round 16:
            # widened to `input.entity_keys or input.subject_key`). codex P2
            # round 3: bump the generation counter too, so a rebuild already
            # in flight when we reach this line (captured `gen` before this
            # point) is detected as stale by entity_key_vocabulary()'s
            # post-query check, even if it finishes reading pre-commit data.
            self._entity_vocab_cache = None
            self._entity_vocab_gen += 1

        # codex P2 round 9: stamping is a TRUE TRI-STATE signal, independent
        # of whether entity_keys was non-empty or any candidate survived the
        # stop-policy above. entity_extraction_complete alone answers "did an
        # entity-aware producer finish extracting this fact's participating
        # entities" — it is an explicit producer opt-in (default False as of
        # round 9), so legacy/non-entity-aware producers (fact_extractor,
        # learn_fact tool, REST endpoint) never set it and correctly stay
        # unstamped/backfill-eligible. Zero ACCEPTED keys (e.g. a
        # scalar-only subject with no object-side entities) is still a
        # VALID completion for an entity-aware producer — gating the stamp
        # on `input.entity_keys` non-empty (the round 6 behavior) would
        # leave those facts permanently re-sent to the LLM by every
        # backfill run, since they can never legitimately produce a
        # non-empty entity_keys list. codex P2 round 16: this stays
        # completely independent of the subject-key seeding above too — a
        # subject-only write (no entity_keys, entity_extraction_complete
        # not set) leaves this NULL, so value-side extraction still
        # revisits the fact later via the backfill's IS NULL predicate.
        if input.entity_extraction_complete:
            fact.entity_keys_extracted_at = datetime.now(UTC)

        # Audit S3: apply the in-band dupe verdict now that the new fact
        # has an id. Returns a ContradictionWarning for CONTRADICTION.
        band_warning: ContradictionWarning | None = None
        if band_action is not None and band_dupe is not None:
            band_warning = await self._apply_band_action(
                fact, band_dupe, band_action, band_sim, session
            )

        # Subject + similarity supersession (006.2)
        # F075: pass the new fact's event_date so supersession can bypass
        # candidates with distinct dates — same entity on a different date
        # is a new event, not a supersession.
        # Audit S11: exclude_ids threaded through so facts the F377
        # tiebreaker (or the band classifier above) just ruled distinct
        # are not silently superseded against that verdict.
        # 064 R2: skip legacy path ONLY when keyed resolution will actually
        # handle this fact (R2 enabled AND both keys present). With R2 off
        # or a key missing the legacy subject path is the only write-time
        # supersession guard — skipping it leaves stale unkeyed same-subject
        # rows active (codex r5).
        if check_contradictions and input.subject and embedding is not None and not (
            getattr(self._settings, "supersession_key_resolution_enabled", False) is True
            and input.subject_key
            and input.attribute_key
        ):
            await self._supersede_by_subject(
                fact.id, input.subject, embedding, session,
                new_content=input.content,
                new_event_date=input.event_date,
                exclude_ids=exclude_ids,
            )

        new_fact_lost = False  # codex r11: set True when keyed resolution deactivates the new fact
        if (
            check_contradictions
            and getattr(self._settings, "supersession_key_resolution_enabled", False) is True
            and input.subject_key
            and input.attribute_key
        ):
            new_fact_lost = await self._resolve_key_conflicts(fact, input, session, exclude_ids)

        await self._emit_event(
            session,
            "fact_learned",
            {
                "fact_id": str(fact.id),
                "category": input.category,
                "subject": input.subject,
            },
        )

        detail = self._to_detail(fact)
        if band_warning is not None:
            detail.contradiction_warning = band_warning

        if check_contradictions and not new_fact_lost:
            # Contradiction detection: similarity 0.85-0.95 with different content
            # codex r11: skipped when new fact lost keyed resolution (inactive fact
            # must not trigger contradiction edges or domain compaction).
            if embedding is not None:
                safe_excludes = list(exclude_ids) + [fact.id] + ([routed_dupe_id] if routed_dupe_id else [])
                contradiction = await self._find_contradiction(
                    embedding, fact.content, safe_excludes, session,
                    new_fact_id=fact.id,
                )
                if contradiction is not None:
                    detail.contradiction_warning = contradiction
                    logger.info(
                        "Contradiction detected for fact %s: similar to %s (%.2f)",
                        fact.id,
                        contradiction.existing_fact_id,
                        contradiction.similarity,
                    )

            # Domain compaction check: emit event if too many facts in same category
            if input.category:
                await self._check_domain_threshold(input.category, session)

        return detail

    async def _find_contradiction(
        self,
        embedding: list[float],
        new_content: str,
        exclude_ids: list[UUID],
        session: AsyncSession,
        new_fact_id: UUID | None = None,
    ) -> ContradictionWarning | None:
        """Detect potential contradictions: similar embedding (0.85-0.95) but different content.

        A contradiction is when two facts talk about the same thing but say
        different things. High similarity means same topic; below dedup
        threshold means different content.

        F027: When LLM is available, classifies the pair to route to the
        correct action (UNRELATED/REFINEMENT/UPDATE/CONTRADICTION).
        """
        if not embedding:
            return None

        embedding_str = "[" + ",".join(str(float(v)) for v in embedding) + "]"

        params: dict = {
            "embedding": embedding_str,
            "agent_id": self.agent_id,
            "sim_min": self.CONTRADICTION_SIMILARITY_MIN,
            "sim_max": self.CONTRADICTION_SIMILARITY_MAX,
        }

        exclude_clause = ""
        if exclude_ids:
            placeholders = ", ".join(f":excl_{i}" for i in range(len(exclude_ids)))
            exclude_clause = f"AND id NOT IN ({placeholders})"
            for i, eid in enumerate(exclude_ids):
                params[f"excl_{i}"] = eid

        sql = text(f"""
            SELECT id, content,
                   1 - (embedding <=> CAST(:embedding AS vector)) AS similarity
            FROM heart.facts
            WHERE agent_id = :agent_id
              AND active = true
              AND embedding IS NOT NULL
              AND 1 - (embedding <=> CAST(:embedding AS vector)) > :sim_min
              AND 1 - (embedding <=> CAST(:embedding AS vector)) <= :sim_max
              {exclude_clause}
            ORDER BY embedding <=> CAST(:embedding AS vector)
            LIMIT 1
        """)

        result = await session.execute(sql, params)
        row = result.first()
        if row is None:
            return None

        # F027: LLM classification for precise routing
        if self._llm is not None and new_fact_id is not None:
            classification = await self._classify_fact_pair(row.content, new_content)
            if classification:
                relation = classification.get("relation", "")
                current = classification.get("current_fact", "")
                try:
                    conf = float(classification.get("confidence", 0.0))
                except (TypeError, ValueError):
                    # Same fail-open contract as _classify_dupe_in_band —
                    # malformed confidence falls through to the plain
                    # ContradictionWarning instead of raising mid-learn.
                    classification = None
                    conf = 0.0
                # codex P1 (round 3): low-confidence verdicts must not
                # route here either — a 0.1-confidence CONTRADICTION wrote
                # an edge + decremented the old fact, and a low-confidence
                # UNRELATED suppressed the warning entirely. Falling
                # through to the plain ContradictionWarning is the
                # conservative pre-classifier behavior. (UPDATE keeps its
                # stricter 0.8 gate below.)
                if classification is not None and conf < 0.5:
                    classification = None

            if classification:
                if relation == "UNRELATED":
                    return None

                if relation == "REFINEMENT":
                    await self._create_graph_edge(
                        new_fact_id, row.id, "fact", "fact",
                        "refines", 0.8, session,
                    )
                    return None

                if relation == "UPDATE" and conf >= 0.8:
                    if current == "new":
                        # Supersede old fact
                        old_fact = await self._get_fact_orm(row.id, session)
                        if old_fact:
                            old_fact.superseded_by = new_fact_id
                            old_fact.active = False
                            old_fact.confidence = max(0.0, (old_fact.confidence or 1.0) * 0.3)
                        await self._create_graph_edge(
                            new_fact_id, row.id, "fact", "fact",
                            "supersedes", 1.0, session,
                        )
                        return None
                    else:
                        # Old is current — deactivate new fact
                        new_fact = await self._get_fact_orm(new_fact_id, session)
                        if new_fact:
                            new_fact.active = False
                        return None

                if relation == "UPDATE" and conf < 0.8:
                    # Low confidence UPDATE — fall through to ContradictionWarning
                    pass

                if relation == "CONTRADICTION":
                    # P1 fix: set contradiction_of, create edge, reduce confidence
                    if new_fact_id:
                        new_fact = await self._get_fact_orm(new_fact_id, session)
                        if new_fact:
                            new_fact.contradiction_of = row.id
                    await self._create_graph_edge(
                        new_fact_id, row.id, "fact", "fact",
                        "contradicts", 1.0, session,
                    )
                    old_fact = await self._get_fact_orm(row.id, session)
                    if old_fact:
                        old_fact.confidence = max(0.0, (old_fact.confidence or 1.0) - 0.2)
                    # Fall through to return ContradictionWarning

        return ContradictionWarning(
            existing_fact_id=row.id,
            existing_content=row.content[:500],
            similarity=float(row.similarity),
            message=f"Potential contradiction detected (similarity {row.similarity:.2f}). "
            f"Existing fact: '{row.content[:100]}' — review and resolve.",
        )

    async def _check_domain_threshold(
        self,
        category: str,
        session: AsyncSession,
    ) -> None:
        """Emit event if active fact count in a category exceeds threshold.

        To avoid event spam (P1-1 fix), only emits when count first crosses
        the threshold or at every DOMAIN_COMPACTION_INTERVAL facts above it.
        """
        sql = text("""
            SELECT COUNT(*) AS cnt
            FROM heart.facts
            WHERE agent_id = :agent_id
              AND category = :category
              AND active = true
        """)
        result = await session.execute(sql, {"agent_id": self.agent_id, "category": category})
        count = result.scalar() or 0

        if count <= self.DOMAIN_COMPACTION_THRESHOLD:
            return

        # Only emit at threshold+1, threshold+1+interval, threshold+1+2*interval, ...
        excess = count - self.DOMAIN_COMPACTION_THRESHOLD
        if excess == 1 or excess % self.DOMAIN_COMPACTION_INTERVAL == 0:
            await self._emit_event(
                session,
                "fact_threshold_exceeded",
                {
                    "category": category,
                    "count": count,
                    "threshold": self.DOMAIN_COMPACTION_THRESHOLD,
                },
            )

    async def _supersede_by_subject(
        self,
        new_fact_id: UUID,
        subject: str,
        embedding: list[float],
        session: AsyncSession,
        new_content: str = "",
        new_event_date: date | None = None,
        exclude_ids: list[UUID] | None = None,
    ) -> None:
        """Supersede older facts with same subject AND similar content (006.2).

        Only supersedes when both conditions are met:
        1. Same subject (case-insensitive exact match)
        2. Cosine similarity > ``fact_supersession_threshold`` setting (default 0.80)

        F027: For ambiguous range (``fact_supersession_threshold``–``fact_native_cosine_threshold``,
        default 0.80–0.95) with LLM available, use classifier to disambiguate
        UNRELATED/REFINEMENT/UPDATE before superseding.

        F075: when ``new_event_date`` is non-NULL and a candidate's
        ``event_date`` differs, skip supersession — same subject on different
        dates is a separate event (e.g., "API key obtained March 10" vs
        "API key obtained March 12"), not a supersession.

        This prevents "Nous version 0.2" from nuking "Nous uses PostgreSQL"
        while correctly superseding "Nous version 0.1".
        """
        result = await session.execute(
            select(Fact).where(
                Fact.agent_id == self.agent_id,
                Fact.active == True,  # noqa: E712
                func.lower(Fact.subject) == subject.lower(),
                Fact.id != new_fact_id,
            )
        )
        excluded = set(exclude_ids or [])
        _supersession_threshold = (
            self._settings.fact_supersession_threshold if self._settings else 0.80
        )
        for old in result.scalars().all():
            # Audit S11: a fact the F377 tiebreaker (or the S3 band
            # classifier) just ruled DISTINCT must not be superseded —
            # at similarity > 0.95 this path skips LLM disambiguation
            # entirely, silently undoing the store-both verdict.
            if old.id in excluded:
                continue
            # F075: skip supersession on date-disagreement.
            if (
                new_event_date is not None
                and old.event_date is not None
                and new_event_date != old.event_date
            ):
                continue
            if old.embedding is not None:
                similarity = self._cosine_similarity(embedding, old.embedding)
                if similarity > _supersession_threshold:
                    # F027: LLM disambiguation for ambiguous range
                    if similarity <= 0.95 and self._llm is not None and new_content:
                        classification = await self._classify_fact_pair(
                            old.content, new_content
                        )
                        if classification:
                            relation = classification.get("relation", "")
                            current = classification.get("current_fact", "")
                            if relation == "UNRELATED":
                                continue  # Skip — not actually related
                            if relation == "REFINEMENT":
                                await self._create_graph_edge(
                                    new_fact_id, old.id, "fact", "fact",
                                    "refines", 0.8, session,
                                )
                                continue  # Keep both
                            if relation == "UPDATE" and current == "old":
                                # Old is current — deactivate new fact
                                new_fact = await self._get_fact_orm(new_fact_id, session)
                                if new_fact:
                                    new_fact.active = False
                                continue
                            if relation == "CONTRADICTION":
                                # Gate-1 D1: this legacy path has no ordinal/
                                # learned_at signal to order by (it only fires
                                # for unkeyed facts or R2-off deployments) —
                                # always KEEP BOTH + flag, same as
                                # _pick_contradiction_winner's unordered
                                # fallback. The old fall-through silently
                                # superseded with a wrong-type 'supersedes' edge.
                                new_fact = await self._get_fact_orm(new_fact_id, session)
                                if new_fact is not None:
                                    await self._flag_contradiction_pair(old, new_fact, session)
                                continue  # keep-both; keep scanning remaining candidates
                            # UPDATE + current=="new" or unknown → fall through to supersede

                    old.active = False
                    old.superseded_by = new_fact_id
                    # Mirror the column write with the graph edge so the
                    # supersession reaches the graph layer (densifier, adjacency
                    # boost, dashboards). Every other supersede branch already
                    # writes this edge; this subject-based path set the column
                    # only — the dominant cause of the 261 superseded_by vs 2
                    # supersedes-edge gap in the 2026-06-13 prod audit.
                    await self._create_graph_edge(
                        new_fact_id, old.id, "fact", "fact", "supersedes", 1.0, session,
                    )
                    logger.info(
                        "Superseded fact %s (subject=%s, sim=%.2f) by %s",
                        old.id, subject, similarity, new_fact_id,
                    )

    @staticmethod
    def _cosine_similarity(a: list[float], b: list[float]) -> float:
        """Cosine similarity between two embedding vectors."""
        dot = sum(x * y for x, y in zip(a, b))
        norm_a = sum(x * x for x in a) ** 0.5
        norm_b = sum(x * x for x in b) ** 0.5
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return dot / (norm_a * norm_b)

    async def _classify_dupe_in_band(
        self,
        dupe: Fact,
        similarity: float,
        input: FactInput,
        check_contradictions: bool,
    ) -> str | None:
        """Audit S3: route an above-dedup-threshold hit that falls inside the
        contradiction band (0.85-0.95) through the F027 classifier instead of
        blindly confirming it.

        With the operator threshold below the band (prod runs 0.80), a blind
        confirm swallows the band — conflicting facts reinforce the stale one.

        Returns a band action ("unrelated" | "refines" | "supersede_old" |
        "contradiction") when the new fact should be INSERTED, or None when
        the dupe should be confirmed (similarity >= 0.95 true duplicate,
        sub-band paraphrase, UPDATE where the old fact is current,
        low-confidence verdict, no LLM, or classifier failure — fail-open to
        dedup, mirroring the F377 tiebreaker contract).
        """
        # 1a (2026-06-13 audit): the caller (_find_duplicate) only returns hits
        # >= fact_native_cosine_threshold, so the lower bound is already
        # guaranteed — gating on CONTRADICTION_SIMILARITY_MIN (0.85) excluded the
        # [threshold, 0.85) range and blind-confirmed it (swallowing contradictions
        # at the prod 0.80 threshold). Classify every dedup hit below MAX; true
        # duplicates (>= MAX) still confirm. At the 0.95 default this is a no-op
        # (the caller only returns >= 0.95 hits → similarity < MAX is False).
        if (
            not check_contradictions
            or self._llm is None
            or not (similarity < self.CONTRADICTION_SIMILARITY_MAX)
        ):
            return None
        # Cost cap: skip classification (fall open to confirm) when the hourly
        # Haiku budget is spent.
        if not self._band_budget_ok():
            return None
        classification = await self._classify_fact_pair(dupe.content, input.content)
        if not classification:
            return None
        relation = classification.get("relation", "")
        current = classification.get("current_fact", "")
        try:
            conf = float(classification.get("confidence", 0.0))
        except (TypeError, ValueError):
            # Malformed confidence must land on the documented fail-open
            # path (confirm), not raise out of _learn mid-transaction.
            return None
        # codex P2 (round 2): a low-confidence verdict must not route — the
        # band default is confirm, and acting on a 0.1-confidence
        # CONTRADICTION inserts a fact, writes an edge, and decrements the
        # existing fact's confidence. UPDATE keeps the stricter 0.8 gate it
        # has always had (it deactivates a fact — the most destructive
        # action); the others gate at 0.5.
        if conf < 0.5:
            return None
        # codex P1 (PR #519, round 2): in the EXTENDED range [threshold, 0.85) —
        # below the true contradiction band — the classifier has no DUPLICATE
        # verdict, so routing UNRELATED/REFINEMENT would INSERT a paraphrase and
        # defeat the operator's aggressive low-threshold dedup. But a real
        # state-change MUST still act: a CONTRADICTION (insert+edge+warn) and an
        # UPDATE that supersedes the stale fact ("API returns 200" -> "500", which
        # the classifier maps to UPDATE/current=new) — otherwise the current-state
        # change is swallowed onto the stale fact. Only UNRELATED/REFINEMENT/
        # UPDATE-old confirm (dedup). Full routing applies inside [0.85, 0.95).
        if similarity < self.CONTRADICTION_SIMILARITY_MIN:
            if relation == "CONTRADICTION":
                return "contradiction"
            if relation == "UPDATE" and current == "new" and conf >= 0.8:
                return "supersede_old"
            return None
        if relation == "UNRELATED":
            return "unrelated"
        if relation == "REFINEMENT":
            return "refines"
        if relation == "UPDATE" and current == "new" and conf >= 0.8:
            return "supersede_old"
        if relation == "CONTRADICTION":
            return "contradiction"
        return None

    async def _apply_band_action(
        self,
        new_fact: Fact,
        dupe: Fact,
        action: str,
        similarity: float,
        session: AsyncSession,
    ) -> ContradictionWarning | None:
        """Apply a _classify_dupe_in_band verdict post-flush.

        Mirrors the F027 routing in _find_contradiction so the two band
        entry points (dedup hit vs post-insert scan) act identically.
        """
        if action == "refines":
            await self._create_graph_edge(
                new_fact.id, dupe.id, "fact", "fact", "refines", 0.8, session,
            )
            return None
        if action == "supersede_old":
            dupe.superseded_by = new_fact.id
            dupe.active = False
            dupe.confidence = max(0.0, (dupe.confidence or 1.0) * 0.3)
            await self._create_graph_edge(
                new_fact.id, dupe.id, "fact", "fact", "supersedes", 1.0, session,
            )
            return None
        if action == "contradiction":
            new_fact.contradiction_of = dupe.id
            await self._create_graph_edge(
                new_fact.id, dupe.id, "fact", "fact", "contradicts", 1.0, session,
            )
            dupe.confidence = max(0.0, (dupe.confidence or 1.0) - 0.2)
            return ContradictionWarning(
                existing_fact_id=dupe.id,
                existing_content=dupe.content[:500],
                similarity=similarity,
                message=(
                    f"Potential contradiction detected (similarity {similarity:.2f}). "
                    f"Existing fact: '{dupe.content[:100]}' — review and resolve."
                ),
            )
        return None  # "unrelated" — both facts stand, no link

    async def _confirm_duplicate(
        self,
        dupe: Fact,
        input: FactInput,
        session: AsyncSession,
    ) -> FactDetail:
        """Confirm an existing fact as the dedup outcome of a new candidate.

        Audit S3: merge event_date onto an undated duplicate — with temporal
        extraction on, a dated candidate colliding with an older undated
        paraphrase was silently de-dated (the F075 bypass requires BOTH sides
        non-null, and plain _confirm never copied the date).

        codex r11 — dedup-confirm must not discard adjudication metadata;
        fill-if-empty only (never overwrite existing non-NULL values).
        """
        if input.event_date is not None and dupe.event_date is None:
            dupe.event_date = input.event_date
            dupe.event_date_classified_at = (
                input.event_date_classified_at or datetime.now(UTC)
            )
        # Fill subject_key + attribute_key as a PAIR — only when both input
        # keys are present and both row keys are NULL (avoids half-keyed rows).
        if (
            input.subject_key is not None
            and input.attribute_key is not None
            and dupe.subject_key is None
            and dupe.attribute_key is None
        ):
            dupe.subject_key = input.subject_key
            dupe.attribute_key = input.attribute_key
        # R3.1 (F085): backfill entity-key rows onto a dupe that hasn't been
        # entity-extracted yet. MUST be conflict-tolerant (review db-P2-2):
        # phase_seed writes subject-key rows WITHOUT stamping
        # entity_keys_extracted_at, so a live dedup-confirm on a
        # seeded-but-not-yet-extracted fact would PK-collide on a plain
        # session.add and abort the whole learn txn. Postgres-only construct
        # (pg_insert.on_conflict_do_nothing) — guarded the same way the W-8
        # advisory lock above gates on dialect.name, so the SQLite unit path
        # never reaches it.
        # codex P2 round 16: gate widened to fire on subject_key alone too
        # (mirrors _learn's identical widening) — a direct dedup-confirming
        # FactInput carrying only subject_key previously backfilled nothing.
        if (
            (input.subject_key or input.entity_keys)
            and dupe.entity_keys_extracted_at is None
            and session.bind is not None
            and session.bind.dialect.name == "postgresql"
        ):
            seen_dupe_keys: set[str] = set()
            max_keys = self._settings.entity_keys_max_per_fact if self._settings else 8
            min_chars = self._settings.entity_key_min_chars if self._settings else 3
            # codex P2 round 6: iterate the FULL list and filter BEFORE
            # capping — mirrors _learn's loop exactly. The previous
            # `input.entity_keys[:max_keys]` sliced the raw list before
            # normalize/stop-policy/dedup, so junk candidates ("1876", "red")
            # occupying early slots could crowd out valid keys past the cap
            # position even though those junk keys never insert a row.
            # codex P2 round 16: subject key unioned FIRST, mirrors _learn.
            for raw_key in (input.subject_key, *input.entity_keys):
                if raw_key is None:
                    continue
                nk = normalize_key(raw_key)
                # codex P2 round 4: mirrors the _learn stop-policy gate above
                # — same bypass-the-extractor risk applies to the dedup-confirm
                # path.
                if (
                    nk
                    and nk not in seen_dupe_keys
                    and is_keyable_entity(nk, min_chars=min_chars)
                ):
                    seen_dupe_keys.add(nk)
                    await session.execute(
                        pg_insert(FactEntityKey)
                        .values(fact_id=dupe.id, entity_key=nk, agent_id=self.agent_id)
                        .on_conflict_do_nothing()
                    )
                if len(seen_dupe_keys) >= max_keys:
                    break
            # codex P2 (+ round 3 gen-counter, round 5 per-call finally):
            # mirrors the _learn invalidation above — same still-open-
            # transaction race applies here.
            self._entity_vocab_cache = None
            self._entity_vocab_gen += 1

        # codex P2 round 9: stamping is independent of whether entity_keys
        # was non-empty (mirrors _learn's restructure — see its comment for
        # the full tri-state rationale) and independent of the postgres-only
        # dialect guard above (that guard is for the pg_insert entity-row
        # backfill construct; a plain attribute assignment works on any
        # dialect). Still fill-if-empty: never overwrite an already-stamped
        # dupe's watermark.
        if dupe.entity_keys_extracted_at is None and input.entity_extraction_complete:
            dupe.entity_keys_extracted_at = datetime.now(UTC)
        # Fill source_ordinal when the row has none and input provides one.
        if input.source_ordinal is not None and dupe.source_ordinal is None:
            dupe.source_ordinal = input.source_ordinal
        # Upgrade overrides_prior: True is sticky — never downgrade.
        if input.overrides_prior and not dupe.overrides_prior:
            dupe.overrides_prior = True
        return await self._confirm(dupe.id, session)

    async def _find_duplicate(
        self,
        embedding: list[float],
        exclude_ids: list[UUID],
        session: AsyncSession,
        candidate_event_date: date | None = None,
        prefer_slot: tuple[str, str] | None = None,
    ) -> tuple[Fact, float] | None:
        """Find a near-duplicate fact by cosine similarity > threshold.

        Returns ``(fact, similarity)`` so the caller can band-route the hit
        (audit S3), or None when nothing clears the threshold.

        Threshold is `Settings.fact_native_cosine_threshold` (default 0.95;
        F056 #377 made this env-tunable via NOUS_FACT_NATIVE_COSINE_THRESHOLD
        because the dedup eval showed 0.95 misses all semantic paraphrases).

        Audit S10: the query is a plain ``ORDER BY distance LIMIT 20`` so the
        HNSW index serves it — the previous compound ORDER BY (date-match
        first) forced a sequential scan over every active fact on every
        learn. Threshold and the F075 date preference are applied in Python
        over the top-20 instead.

        F075 (codex PR #461 P2): when ``candidate_event_date`` is provided,
        prefer above-threshold matches with the same event_date (NULL == NULL
        counts as same) over nearer different-date hits. Without this
        preference, a March-10 candidate could see a March-12 fact as the top
        vector hit, trigger the F075 bypass, and silently insert a duplicate
        of an already-stored March-10 fact lurking just below it.
        NOTE (review): the date preference now only sees the top-20 nearest
        rows — a same-date duplicate ranked 21+ by cosine is invisible to it.
        That horizon is the price of HNSW-servability; 20 comfortably covers
        any realistic above-threshold cluster (the old SQL date-first ordering
        saw ALL above-threshold rows but could never use the index).

        Codex r7: this method picks ONE candidate from the above-threshold
        set — the Gate-1 D2 same-slot guard in ``_learn`` can only route what
        THIS selector surfaces, so a cross-slot near-duplicate (closer in
        embedding space, but from an unrelated conflict slot) could mask a
        genuine same-slot value-variant sitting just below it, letting the
        legacy blind-confirm swallow the correction before R2 ever saw it.
        ``_learn`` passes ``prefer_slot`` ONLY when the D2 guard could
        actually fire (check_contradictions + routing flag + input
        both-keyed); every other caller passes ``None`` and is
        byte-identical to before. When set, above-threshold candidates
        matching ``(subject_key, attribute_key) == prefer_slot`` are
        preferred; the EXISTING F075 date-preference logic then runs within
        that narrower subset unchanged. When no same-slot candidate exists,
        selection falls back to today's behavior exactly.
        """
        embedding_str = "[" + ",".join(str(float(v)) for v in embedding) + "]"

        # Build exclude clause (P1-2)
        exclude_clause = ""
        threshold = (
            float(self._settings.fact_native_cosine_threshold)
            if self._settings is not None else 0.95
        )
        params: dict = {
            "embedding": embedding_str,
            "agent_id": self.agent_id,
        }
        if exclude_ids:
            placeholders = ", ".join(f":excl_{i}" for i in range(len(exclude_ids)))
            exclude_clause = f"AND id NOT IN ({placeholders})"
            for i, eid in enumerate(exclude_ids):
                params[f"excl_{i}"] = eid

        # codex P2 (2026-06-09): pgvector applies the agent_id/active
        # filters AFTER the approximate candidate walk, so on a large
        # multi-tenant table other agents' nearby vectors could exhaust the
        # horizon. ef_search=100 (5x the LIMIT) gives ample margin for the
        # single-agent prod shape; a true multi-tenant deployment should
        # move to hnsw.iterative_scan (pgvector >= 0.8) or per-agent
        # partial indexes.
        await set_local_ef_search(session, 100)
        sql = text(f"""
            SELECT id, event_date, subject_key, attribute_key,
                   1 - (embedding <=> CAST(:embedding AS vector)) AS similarity
            FROM heart.facts
            WHERE agent_id = :agent_id
              AND active = true
              AND embedding IS NOT NULL
              {exclude_clause}
            ORDER BY embedding <=> CAST(:embedding AS vector)
            LIMIT 20
        """)

        result = await session.execute(sql, params)
        above = [r for r in result.all() if float(r.similarity) > threshold]
        if not above:
            return None

        # Codex r7: restrict to same-slot candidates first when requested and
        # any exist among the above-threshold set; otherwise fall back to the
        # full set exactly as before prefer_slot existed.
        candidates = above
        if prefer_slot is not None:
            same_slot = [r for r in above if (r.subject_key, r.attribute_key) == prefer_slot]
            if same_slot:
                candidates = same_slot

        # F075 date preference in Python (rows are distance-ordered, so the
        # first date-match is the nearest one; None == None is a match).
        chosen = next(
            (r for r in candidates if r.event_date == candidate_event_date),
            candidates[0],
        )

        # Fetch the ORM object
        fact_result = await session.execute(select(Fact).where(Fact.id == chosen.id))
        fact = fact_result.scalars().first()
        if fact is None:
            return None
        return fact, float(chosen.similarity)

    async def _find_max_similarity(
        self,
        embedding: list[float],
        exclude_ids: list[UUID],
        session: AsyncSession,
    ) -> float | None:
        """Find highest cosine similarity to any existing active fact.

        Used by admission controller for novelty scoring.
        Returns None if no facts exist or no embedding available.
        """
        if not embedding:
            return None

        embedding_str = "[" + ",".join(str(float(v)) for v in embedding) + "]"
        params: dict = {"embedding": embedding_str, "agent_id": self.agent_id}

        exclude_clause = ""
        if exclude_ids:
            placeholders = ", ".join(f":excl_{i}" for i in range(len(exclude_ids)))
            exclude_clause = f"AND id NOT IN ({placeholders})"
            for i, eid in enumerate(exclude_ids):
                params[f"excl_{i}"] = eid

        sql = text(f"""
            SELECT 1 - (embedding <=> CAST(:embedding AS vector)) AS similarity
            FROM heart.facts
            WHERE agent_id = :agent_id
              AND active = true
              AND embedding IS NOT NULL
              {exclude_clause}
            ORDER BY embedding <=> CAST(:embedding AS vector)
            LIMIT 1
        """)

        result = await session.execute(sql, params)
        row = result.first()
        return float(row.similarity) if row else None

    async def _get_source_text(
        self,
        fact_input: FactInput,
        session: AsyncSession,
    ) -> str | None:
        """Retrieve source text for ROUGE-L grounding check.

        Priority: FactInput.source_text > Episode.transcript > Episode.summary.
        F025 P2-E: source_text passthrough avoids grounding against lossy summary.
        """
        # F025 P2-E: Use passed-through transcript if available
        if fact_input.source_text:
            return fact_input.source_text

        if not fact_input.source_episode_id:
            return None

        episode = await session.get(Episode, fact_input.source_episode_id)
        if not episode:
            return None

        # F025 P3-C: Prefer persisted transcript over lossy summary
        if episode.transcript:
            return episode.transcript
        if episode.summary:
            return episode.summary

        return None

    # ------------------------------------------------------------------
    # confirm()
    # ------------------------------------------------------------------

    async def confirm(self, fact_id: UUID, session: AsyncSession | None = None) -> FactDetail:
        """Confirm a fact is still true."""
        if session is None:
            async with self.db.session() as session:
                result = await self._confirm(fact_id, session)
                await session.commit()
                return result
        return await self._confirm(fact_id, session)

    async def _confirm(self, fact_id: UUID, session: AsyncSession) -> FactDetail:
        fact = await self._get_fact_orm(fact_id, session)
        if fact is None:
            raise ValueError(f"Fact {fact_id} not found")

        # P2-9: NULL-safe counter increment
        fact.confirmation_count = (fact.confirmation_count or 0) + 1
        fact.last_confirmed = datetime.now(UTC)
        await session.flush()

        await self._emit_event(
            session,
            "fact_confirmed",
            {
                "fact_id": str(fact_id),
                "confirmation_count": fact.confirmation_count,
            },
        )

        return self._to_detail(fact)

    # ------------------------------------------------------------------
    # supersede()
    # ------------------------------------------------------------------

    async def supersede(
        self,
        old_fact_id: UUID,
        new_fact: FactInput,
        session: AsyncSession | None = None,
    ) -> FactDetail:
        """Replace a fact with a newer version."""
        if session is None:
            async with self.db.session() as session:
                result = await self._supersede(old_fact_id, new_fact, session)
                await session.commit()
                # codex P2 round 13: _supersede's inherit_conflict_slot_keys
                # call can write new entity-key rows within THIS commit —
                # invalidate post-commit so entity_key_vocabulary() doesn't
                # serve a stale vocab for the rest of the TTL (mirrors
                # learn()'s own finally-block invalidation).
                self.invalidate_entity_vocab()
                return result
        # Injected-session callers own their own commit point (same contract
        # as learn()) — no place here to re-invalidate post-commit.
        return await self._supersede(old_fact_id, new_fact, session)

    async def _supersede(
        self,
        old_fact_id: UUID,
        new_fact: FactInput,
        session: AsyncSession,
    ) -> FactDetail:
        # Verify old fact exists
        old_fact = await self._get_fact_orm(old_fact_id, session)
        if old_fact is None:
            raise ValueError(f"Fact {old_fact_id} not found")

        # F023: Bypass admission gate for intentional replacements
        bypass_input = new_fact.model_copy(update={"source": "supersede"})
        new_detail = await self._learn(bypass_input, [old_fact_id], False, session)
        if isinstance(new_detail, FactRejected):
            raise RuntimeError("Supersede bypass failed — admission should not reject bypassed sources")

        # codex P2 round 11: callers like sleep_handler._handle_updates_prefix
        # supersede with a bare FactInput (no subject_key/entity_keys) — the
        # replacement would otherwise permanently lose exact-key recall for
        # the conflict slot the deactivated old_fact occupied. Reuses the
        # same helper the F031/F027 merge sites use; it no-ops the
        # subject_key/attribute_key copy when new_fact already carries its
        # own subject_key (a keyed caller wins over inheritance).
        await self.inherit_conflict_slot_keys(new_detail.id, [old_fact_id], session)

        # Update old fact
        old_fact.superseded_by = new_detail.id
        old_fact.active = False
        await session.flush()

        # F022: Bridge — also create graph edge
        await self._create_graph_edge(
            new_detail.id, old_fact_id, "fact", "fact", "supersedes", 1.0, session
        )

        await self._emit_event(
            session,
            "fact_superseded",
            {
                "old_fact_id": str(old_fact_id),
                "new_fact_id": str(new_detail.id),
            },
        )

        return new_detail

    # ------------------------------------------------------------------
    # apply_supersession()
    # ------------------------------------------------------------------

    async def apply_supersession(
        self,
        winner_id: UUID,
        loser_id: UUID,
        session: AsyncSession,
    ) -> bool:
        """064 R2: shared supersession primitive extracted from sleep _apply_supersede.

        Sets ``loser.superseded_by = winner_id``, ``loser.active = False``,
        and writes the ``supersedes`` graph edge — all within the caller-owned
        session.  Caller is responsible for ``session.commit()``.

        Returns ``True`` if the supersession was applied, ``False`` if skipped
        due to the clobber guard (loser not found, or already superseded by a
        prior path so the chain would be overwritten).

        Both IDs must belong to ``self.agent_id``; cross-agent IDs are not
        validated by this primitive and will return ``False`` silently if the
        loser row is not found.  Callers may ``commit()`` unconditionally even
        when this returns ``False`` — the no-op path writes nothing."""
        loser = await self._get_fact_orm(loser_id, session)
        if loser is None:
            return False
        if loser.superseded_by is not None:
            logger.debug(
                "apply_supersession: skip %s — already superseded by %s",
                loser_id,
                loser.superseded_by,
            )
            return False
        loser.superseded_by = winner_id
        loser.active = False
        await self._create_graph_edge(winner_id, loser_id, "fact", "fact", "supersedes", 1.0, session)
        return True

    def invalidate_entity_vocab(self) -> None:
        """codex P2 round 13: clear the cached entity-key vocabulary and bump
        its generation counter. Call this AFTER committing entity-key writes
        made outside learn()'s own session — e.g. inherit_conflict_slot_keys
        at a caller-owned commit point (supersede, sleep_handler's F031/F027
        merge sites) — since learn()'s own finally block only invalidates
        for writes made inside ITS OWN commit. Without a post-commit call
        here, a concurrent recall that rebuilds entity_key_vocabulary()
        before the caller's commit lands can re-cache a stale vocab with
        nothing left to invalidate it for the rest of the TTL (same race
        class as rounds 2/3/5, fixed there for learn() specifically)."""
        self._entity_vocab_cache = None
        self._entity_vocab_gen += 1

    async def inherit_conflict_slot_keys(
        self, replacement_id: UUID, source_ids: list[UUID], session: AsyncSession,
    ) -> None:
        """codex P2 round 9: merged/replacement facts (F031 contradiction-
        resolution MERGE, F027 cluster-consolidation MERGE in sleep_handler.py)
        are created via a bare FactInput carrying only
        subject/content/source/confidence/category — subject_key,
        attribute_key, and entity_keys never carry over from the facts being
        merged, so the replacement silently drops out of R2 same-key
        supersession AND the keyed retrieval leg despite occupying the exact
        same conflict slot the sources did. Call this AFTER the replacement
        fact row exists (its id is known), passing the ids of the facts
        being merged away.

        (a) subject_key/attribute_key are copied from the NEWEST source that
        has BOTH set (as a pair — a half-keyed row is worse than an unkeyed
        one; "newest" matches R2's general most-recent-wins resolution
        spirit when sources disagree).
        (b) entity_keys rows are the DISTINCT UNION of every source's
        fact_entity_keys, inserted for the replacement (ON CONFLICT DO
        NOTHING), capped at entity_keys_max_per_fact — nothing else about
        the source rows is preserved. Postgres-only (on_conflict_do_nothing
        is a Postgres construct), mirroring _confirm_duplicate's dialect
        guard. codex P2 round 10: the copied subject_key is inserted FIRST
        (when present and it passes the entity stop-policy), before the
        capped union query — an unordered ``distinct().limit(max_keys)``
        can return any subset when sources have more than max_keys distinct
        rows, and without reserving a slot for it the subject key set in
        (a) could silently be absent from the replacement's own
        fact_entity_keys rows. The union fill then excludes the subject key
        (already handled) and orders by entity_key for a stable, rerun-safe
        outcome.
        (c) entity_keys_extracted_at is deliberately left untouched (stays
        NULL on the fresh replacement row — the merge FactInput never sets
        entity_keys/entity_extraction_complete, so _learn's stamp never
        fires): the merged CONTENT is new text — an LLM-authored synthesis,
        not any one source's original wording — so the backfill's
        value-side extraction should re-derive entities for it rather than
        inheriting a stale completion signal from facts whose exact content
        no longer exists anywhere.

        codex P2 round 11: this is now also called from the generic
        supersede path (_supersede), whose caller — unlike the two merge
        sites, which always pass a bare subject/content-only FactInput —
        can pass a FactInput that already carries its own subject_key (a
        keyed replacement should win over an inherited one). So the
        subject_key/attribute_key copy in (a), and the subject-key
        reservation in (b), are skipped when the replacement already has a
        subject_key that DIFFERS from what these same sources would copy —
        that's a caller-owned key, not one this helper produced. When the
        existing key instead MATCHES what these sources would produce, the
        copy is treated as a no-op re-application rather than a foreign
        key: this keeps a second, idempotent invocation on an
        already-processed replacement (e.g. a retried merge/backfill call)
        stable, per round 10's rerun-stability contract — round 10's
        distinct().limit(max_keys) cap still needs the subject key
        reserved on every call, not just the first, or a rerun can admit
        one extra union row once the column is no longer NULL. The
        entity-key union insert in (b) stays unconditional and additive
        regardless (ON CONFLICT DO NOTHING makes it safe either way).

        codex P2 round 12: round 11's guard only compared subject_key,
        so a replacement with the SAME subject_key as these sources but a
        DIFFERENT, caller-supplied attribute_key still got that
        attribute_key silently clobbered by the pair-copy — a half-foreign
        pair is exactly the "worse than unkeyed" outcome (a) already warns
        about, just introduced by this guard instead of avoided by it. Both
        slots are now compared, and EITHER one differing from what these
        sources would produce blocks the WHOLE pair-copy (never copy just
        one slot) — the same equality-vs-bare-not-null distinction from
        round 11 is preserved for both slots, so round 10's rerun-stability
        contract still holds (on a rerun both slots already equal what the
        same sources would produce, so the copy is still treated as a
        no-op re-application, not a foreign pair).

        codex P2 round 13: two independent fixes. (1) The entity-key cap
        now counts the replacement's EXISTING rows before spending the
        remaining budget — a fresh max_keys allowance every call, ignoring
        rows the replacement already owns (a keyed FactInput's own
        entity_keys through supersede, or a merge-learn call that confirmed
        an existing fact), could push the total past 2x the configured cap.
        The subject-key reservation is best-effort within this remaining
        budget: it still claims a slot FIRST (displacing potential union
        "filler" keys, not the other way around), but if the replacement
        already has no budget left when this call starts, the subject key
        is simply not inserted this call — nothing is evicted to make room
        for it. (2) This helper's own in-txn cache invalidation (below)
        only protects a reader whose round-trip races the STILL-OPEN
        transaction; it does nothing once the caller's own commit lands,
        because that commit happens in code this helper doesn't control.
        Every caller must now also call the new `invalidate_entity_vocab()`
        AFTER its own commit — done at all three call seams (supersede,
        sleep_handler's F031/F027 merge sites) — mirroring the post-commit
        invalidation `learn()` already does for its own writes (rounds
        2/3/5).

        codex P2 round 15: round 13's cap fix counted only HOW MANY rows
        the replacement already owned, not WHICH ones — a candidate
        (subject key or union key) already present on the replacement
        still spent a slot of the remaining budget even though its insert
        was an ON CONFLICT DO NOTHING no-op (no row actually added). Under
        a tight cap this could silently crowd out a genuinely new source
        key. Now the replacement's existing keys are loaded as a SET, and
        every candidate is checked for membership in it BEFORE either the
        subject-key reservation or the union query spends any budget —
        an already-present candidate costs nothing, so the remaining slots
        go only to insertions that actually happen.

        No commit here — caller-owned session/transaction, same contract as
        apply_supersession above.
        """
        if not source_ids:
            return

        replacement_row = (
            await session.execute(
                select(Fact.subject_key, Fact.attribute_key).where(Fact.id == replacement_id)
            )
        ).first()
        replacement_subject_key = replacement_row.subject_key if replacement_row else None
        replacement_attribute_key = replacement_row.attribute_key if replacement_row else None

        sources = (
            await session.execute(
                select(Fact.subject_key, Fact.attribute_key, Fact.learned_at)
                .where(Fact.id.in_(source_ids))
            )
        ).all()
        complete = [s for s in sources if s.subject_key is not None and s.attribute_key is not None]
        newest = max(complete, key=lambda s: s.learned_at) if complete else None
        skip_slot_copy = newest is not None and (
            (replacement_subject_key is not None and replacement_subject_key != newest.subject_key)
            or (replacement_attribute_key is not None and replacement_attribute_key != newest.attribute_key)
        )
        if newest is not None and not skip_slot_copy:
            await session.execute(
                update(Fact)
                .where(Fact.id == replacement_id)
                .values(subject_key=newest.subject_key, attribute_key=newest.attribute_key)
            )

        if session.bind is not None and session.bind.dialect.name == "postgresql":
            max_keys = self._settings.entity_keys_max_per_fact if self._settings else 8
            min_chars = self._settings.entity_key_min_chars if self._settings else 3
            inserted_any = False

            # codex P2 round 13: the replacement may already own entity-key
            # rows (a keyed FactInput through supersede, or a merge-learn
            # call that confirmed an existing fact) — a fresh max_keys
            # budget on every call, on top of rows already there, could
            # push the total to 2x the configured cap. codex P2 round 15:
            # fetch the actual SET of existing keys (not just a count) —
            # round 13's count-only version still spent budget on a
            # candidate that was ALREADY in that set (an ON CONFLICT DO
            # NOTHING no-op adds no row), which could crowd out a
            # genuinely new source key under a tight cap. Every candidate
            # below is checked against this set before an insert is even
            # attempted — deterministic, no rowcount inspection needed.
            existing_keys = set(
                (await session.execute(
                    select(FactEntityKey.entity_key).where(FactEntityKey.fact_id == replacement_id)
                )).scalars().all()
            )
            remaining = max(0, max_keys - len(existing_keys))

            # Reserve a slot for the copied subject_key FIRST, before the
            # capped union query below can crowd it out. Skipped when the
            # pair-copy above was skipped (a foreign, caller-owned slot is
            # present in either subject_key or attribute_key), when no
            # budget remains, or when the subject key is ALREADY among the
            # replacement's existing rows (nothing to insert, no budget
            # spent) — subject-key reservation is best-effort within the
            # cap: a replacement already at/over the cap does not evict an
            # existing row to make room.
            subject_key_value = (
                newest.subject_key if newest is not None and not skip_slot_copy else None
            )
            if (
                subject_key_value
                and is_keyable_entity(subject_key_value, min_chars=min_chars)
                and subject_key_value not in existing_keys
                and remaining > 0
            ):
                await session.execute(
                    pg_insert(FactEntityKey)
                    .values(fact_id=replacement_id, entity_key=subject_key_value, agent_id=self.agent_id)
                    .on_conflict_do_nothing()
                )
                inserted_any = True
                remaining -= 1

            if remaining > 0:
                union_stmt = select(FactEntityKey.entity_key).where(
                    FactEntityKey.fact_id.in_(source_ids)
                )
                # codex P2 round 15: exclude keys already on the replacement
                # (existing rows, plus the subject key handled above) from
                # the candidates BEFORE the LIMIT — so remaining counts only
                # genuinely new rows, never a candidate that would no-op.
                excluded = existing_keys | ({subject_key_value} if subject_key_value else set())
                if excluded:
                    union_stmt = union_stmt.where(FactEntityKey.entity_key.notin_(excluded))
                union_keys = (
                    await session.execute(
                        union_stmt.distinct()
                        .order_by(FactEntityKey.entity_key)
                        .limit(remaining)
                    )
                ).scalars().all()
                for key in union_keys:
                    await session.execute(
                        pg_insert(FactEntityKey)
                        .values(fact_id=replacement_id, entity_key=key, agent_id=self.agent_id)
                        .on_conflict_do_nothing()
                    )
                inserted_any = inserted_any or bool(union_keys)

            if inserted_any:
                self.invalidate_entity_vocab()

    # ------------------------------------------------------------------
    # 064 R2: key-conflict resolution
    # ------------------------------------------------------------------

    async def find_key_conflict_pairs(
        self,
        limit: int = 25,
        session: AsyncSession | None = None,
        after: tuple | None = None,
    ) -> list[dict]:
        """064 R2 sleep sweep: active fact pairs sharing (subject_key,
        attribute_key) — the cross-episode conflicts write-time detection
        missed (it only sees pairs at insert). Oldest-first for determinism;
        resolution deactivates losers so re-runs converge.

        The JOIN uses row comparison ``(f1.learned_at, f1.id) <
        (f2.learned_at, f2.id)`` rather than a scalar ``<`` so that pairs
        whose ``learned_at`` values are identical (e.g. two facts committed
        in the same transaction) are still included, with ``id`` as the
        deterministic tiebreak.

        Args:
            after: Optional paging cursor ``(ts1, id1, ts2, id2)`` — the
                   full 4-tuple of the last pair processed.  The 4-tuple
                   order (f1.learned_at, f1.id, f2.learned_at, f2.id) makes
                   every pair uniquely addressable, so a single f1 with
                   multiple same-key f2 partners no longer causes starvation
                   when all first-page pairs are KEEP_BOTH.  Pass ``None``
                   to start from the oldest pair.

        Returns:
            List of dicts with keys ``id1``, ``id2``, ``c1``, ``c2``,
            ``ts1`` (f1.learned_at), ``ts2`` (f2.learned_at).
        """
        if after is not None:
            sql = text("""
                SELECT f1.id AS id1, f2.id AS id2,
                       f1.content AS c1, f2.content AS c2,
                       f1.learned_at AS ts1, f2.learned_at AS ts2
                FROM heart.facts f1
                JOIN heart.facts f2
                  ON f2.agent_id = f1.agent_id
                 AND f2.subject_key = f1.subject_key
                 AND f2.attribute_key = f1.attribute_key
                 AND (f1.learned_at, f1.id) < (f2.learned_at, f2.id)
                WHERE f1.agent_id = :agent_id
                  AND f1.active = true AND f2.active = true
                  AND f1.subject_key IS NOT NULL AND f1.attribute_key IS NOT NULL
                  AND (f1.event_date IS NULL OR f2.event_date IS NULL
                       OR f1.event_date = f2.event_date)
                  AND (f1.learned_at, f1.id, f2.learned_at, f2.id)
                      > (:after_ts1, :after_id1, :after_ts2, :after_id2)
                ORDER BY f1.learned_at ASC, f1.id ASC,
                         f2.learned_at ASC, f2.id ASC
                LIMIT :limit
            """)
            params: dict = {
                "agent_id": self.agent_id,
                "limit": limit,
                "after_ts1": after[0],
                "after_id1": after[1],
                "after_ts2": after[2],
                "after_id2": after[3],
            }
        else:
            sql = text("""
                SELECT f1.id AS id1, f2.id AS id2,
                       f1.content AS c1, f2.content AS c2,
                       f1.learned_at AS ts1, f2.learned_at AS ts2
                FROM heart.facts f1
                JOIN heart.facts f2
                  ON f2.agent_id = f1.agent_id
                 AND f2.subject_key = f1.subject_key
                 AND f2.attribute_key = f1.attribute_key
                 AND (f1.learned_at, f1.id) < (f2.learned_at, f2.id)
                WHERE f1.agent_id = :agent_id
                  AND f1.active = true AND f2.active = true
                  AND f1.subject_key IS NOT NULL AND f1.attribute_key IS NOT NULL
                  AND (f1.event_date IS NULL OR f2.event_date IS NULL
                       OR f1.event_date = f2.event_date)
                ORDER BY f1.learned_at ASC, f1.id ASC,
                         f2.learned_at ASC, f2.id ASC
                LIMIT :limit
            """)
            params = {"agent_id": self.agent_id, "limit": limit}
        if session is None:
            async with self.db.session() as session:
                rows = await session.execute(sql, params)
                return [dict(r._mapping) for r in rows.fetchall()]
        rows = await session.execute(sql, params)
        return [dict(r._mapping) for r in rows.fetchall()]

    async def resolve_key_conflict_pair(
        self, id1: UUID, id2: UUID, c1: str, c2: str
    ) -> bool:
        """064 R2 sweep/backfill seam: confirm a same-key pair via the F027
        classifier and resolve per policy. fact1 (id1/c1) is the OLDER fact.
        Owns its session + commit. Returns True iff a supersession was written.
        Fail-open (classifier None / low conf / budget spent / guard) => False.

        NOTE (devil-2 #5): the F075 distinct-event-date exclusion for the sweep
        lives in find_key_conflict_pairs' SQL; the write-time twin lives in
        _resolve_key_conflicts' Python. One rule, two encodings — any future
        change (tolerance windows, date ranges) MUST update both. Both compare
        `date` values (FactInput's validator coerces to datetime.date; the ORM
        column is Date) — the implementer must add the type-equality test below.

        Execution order (codex r8): (1) open session + fetch both rows + None
        guard + staleness guard — free, no budget consumed; (2) _key_budget_ok()
        — consuming; (3) classify; (4) adjudicate + apply_supersession + commit.
        Stale/missing pairs therefore never burn a classifier slot.
        """
        async with self.db.session() as session:
            f_old = await self._get_fact_orm(id1, session)
            f_new = await self._get_fact_orm(id2, session)
            if f_old is None or f_new is None:
                return False
            # Stale-pair guard (codex r4 P1): skip pairs where either row is
            # no longer active or already has a superseded_by set.  A sweep
            # page can contain A/B, A/C, B/C; resolving A/B deactivates B, so
            # the pre-fetched B/C pair must be skipped or C could be
            # superseded to an inactive winner.  active IS NULL is treated as
            # active (server_default=True, ORM may return None before flush).
            old_active = f_old.active if f_old.active is not None else True
            new_active = f_new.active if f_new.active is not None else True
            if (
                not old_active
                or not new_active
                or f_old.superseded_by is not None
                or f_new.superseded_by is not None
            ):
                logger.debug(
                    "resolve_key_conflict_pair: pair no longer current — skipping (%s, %s)",
                    id1,
                    id2,
                )
                return False
            # Budget check after staleness guard (codex r8): stale pairs must
            # not consume a classifier slot — dense clusters of already-resolved
            # pairs would otherwise exhaust the hourly cap on no-ops.
            if not self._key_budget_ok():
                return False
            cls = await self._classify_fact_pair(c1, c2)
            if not cls:
                return False
            relation = cls.get("relation", "")
            try:
                conf = float(cls.get("confidence", 0.0))
            except (TypeError, ValueError):
                # Malformed confidence (e.g. "high") → fail-open: KEEP BOTH.
                conf = 0.0
            if relation not in ("UPDATE", "CONTRADICTION") or conf < 0.8:
                return False
            if relation == "CONTRADICTION":
                # Gate-1 D1: statement order resolves CONTRADICTION, never the
                # classifier's current_fact verdict — see _pick_contradiction_winner.
                winner = self._pick_contradiction_winner(f_old, f_new)
                if winner is None:
                    await self._flag_contradiction_pair(f_old, f_new, session)
                    # store-P1: this method owns its session; Database.session()
                    # does NOT auto-commit, so an uncommitted KEEP-BOTH flag
                    # would be silently rolled back on close.
                    await session.commit()
                    return False  # KEEP-BOTH + flag
                loser = f_old if winner is f_new else f_new
            else:
                winner, loser = self._pick_winner(f_new, f_old, cls)
            ok = await self.apply_supersession(winner.id, loser.id, session)
            if ok:
                # R2.6 sampled precision audit hook: caller logs the texts.
                logger.info(
                    "R2 resolved: superseded %r ==> %r",
                    loser.content[:200],
                    winner.content[:200],
                )
            await session.commit()
            return ok

    async def _resolve_key_conflicts(
        self, fact: Fact, input: FactInput, session: AsyncSession,
        exclude_ids: list[UUID],
    ) -> bool:
        """064 R2.1/R2.2: same-(subject_key, attribute_key) conflict resolution.

        Precedence (binding, reviews RC-4/AC-3):
          1. F075 — differing non-null event_dates => distinct events, KEEP BOTH.
          2. Classifier confirm — only UPDATE/CONTRADICTION at conf >= 0.8
             counts as a same-slot conflict; anything else KEEPS BOTH.
          3. Policy picks the winner: ordinal (same-episode, both ordinals
             present) else recency (learned_at).
        Never deletes; loser keeps full lineage via apply_supersession.

        Returns True when the newly-inserted fact (``fact``) lost and is now
        inactive; False otherwise (codex r11).
        """
        cap = self._settings.supersession_key_candidates_cap if self._settings else 8
        rows = await session.execute(
            select(Fact)
            .where(
                Fact.agent_id == self.agent_id,
                Fact.active == True,  # noqa: E712
                Fact.subject_key == input.subject_key,
                Fact.attribute_key == input.attribute_key,
                Fact.id != fact.id,
            )
            .order_by(Fact.learned_at.desc())
            .limit(cap)
        )
        for old in rows.scalars().all():
            if old.id in exclude_ids:
                continue
            if (
                input.event_date is not None
                and old.event_date is not None
                and input.event_date != old.event_date
            ):
                continue  # F075 precedence: distinct events, never supersede
            if not self._key_budget_ok():
                logger.warning("R2: key-conflict classifier hourly budget spent — deferring to sleep sweep")
                return False
            cls = await self._classify_fact_pair(old.content, input.content)
            if not cls:
                continue  # fail-open: KEEP BOTH
            relation = cls.get("relation", "")
            try:
                conf = float(cls.get("confidence", 0.0))
            except (TypeError, ValueError):
                # Malformed confidence (e.g. "high") → fail-open: KEEP BOTH.
                conf = 0.0
            if relation not in ("UPDATE", "CONTRADICTION") or conf < 0.8:
                continue  # not a confirmed same-slot conflict
            if relation == "CONTRADICTION":
                # Gate-1 D1: statement order resolves CONTRADICTION, never the
                # classifier's current_fact verdict (a memory store records
                # what was said; a user's correction must beat the model's
                # prior). See _pick_contradiction_winner.
                winner = self._pick_contradiction_winner(old, fact)
                if winner is None:
                    await self._flag_contradiction_pair(old, fact, session)
                    continue  # KEEP-BOTH + flag
                loser = old if winner is fact else fact
            else:  # UPDATE — mutable state; ordinal (reading order) is the authority signal
                winner, loser = self._pick_winner(fact, old, cls)
            await self.apply_supersession(winner.id, loser.id, session)
            if loser.id == fact.id:
                return True  # codex r11: new fact lost — it is inactive; stop scanning
        return False

    def _pick_winner(self, new_fact: Fact, old_fact: Fact, classification: dict | None = None):
        """R2.2 policy for UPDATE conflicts. Returns (winner, loser).
        Precedence: same-episode positional ordinal (reading order) →
        classifier current_fact → recency (later learned_at). CONTRADICTION
        is resolved by _pick_contradiction_winner — statement order only,
        never this method or the classifier's current_fact verdict."""
        policy = getattr(self._settings, "supersession_policy", "ordinal") if self._settings else "ordinal"
        if (
            policy == "ordinal"
            and new_fact.source_ordinal is not None
            and old_fact.source_ordinal is not None
            and new_fact.source_episode_id is not None
            and new_fact.source_episode_id == old_fact.source_episode_id
        ):
            return (new_fact, old_fact) if new_fact.source_ordinal >= old_fact.source_ordinal else (old_fact, new_fact)
        # No comparable ordinals: respect an explicit classifier direction.
        current = (classification or {}).get("current_fact", "")
        if current == "old":
            return (old_fact, new_fact)
        if current == "new":
            return (new_fact, old_fact)
        # recency fallback (also the 'recency' policy): later learned_at wins;
        # the just-inserted fact's learned_at is now(), so new wins unless the
        # DB clock says otherwise (backfill can set learned_at explicitly).
        new_ts = new_fact.learned_at
        old_ts = old_fact.learned_at
        if new_ts is not None and old_ts is not None and old_ts > new_ts:
            return (old_fact, new_fact)
        return (new_fact, old_fact)

    def _pick_contradiction_winner(self, old_fact: Fact, new_fact: Fact) -> Fact | None:
        """Gate-1 D1: CONTRADICTION resolves by TESTIMONY ORDER, never by which
        claim the model believes is true (a memory store records what was said;
        a user's correction must beat the model's prior). Same-episode ordinal
        -> later learned_at -> None (KEEP-BOTH + flag, the fail-open default)."""
        if (
            old_fact.source_ordinal is not None
            and new_fact.source_ordinal is not None
            and old_fact.source_episode_id is not None
            and old_fact.source_episode_id == new_fact.source_episode_id
        ):
            return new_fact if new_fact.source_ordinal >= old_fact.source_ordinal else old_fact
        if old_fact.learned_at != new_fact.learned_at:
            return new_fact if new_fact.learned_at > old_fact.learned_at else old_fact
        return None

    async def _flag_contradiction_pair(self, a: Fact, b: Fact, session: AsyncSession) -> None:
        """KEEP-BOTH marking — mirrors _find_contradiction's flag primitives
        exactly (unconditional contradiction_of assignment + contradicts edge +
        the same -0.2 confidence decrement on the OLDER fact), so all keep-both
        sites behave uniformly (review store-P2 + devil-P3-2: match existing,
        don't innovate).

        Final review C1: a KEEP-BOTH pair never sets superseded_by, so it stays
        active and keeps re-surfacing to this same code path on every later
        sleep sweep / backfill re-run (find_key_conflict_pairs has no
        already-flagged exclusion and the sweep cursor wraps). Without a
        convergence guard, each re-run re-decrements `a.confidence` by another
        -0.2, compounding toward 0. Idempotent: a repeat call for an
        already-flagged pair is a no-op.

        Codex r1: the column-only guard above forgets pairs in a 3+ fact
        cluster — `contradiction_of` is a single-valued column, so flagging
        (B, C) after (A, C) overwrites C's column from A.id to B.id; a later
        re-scan of (A, C) then sees a column mismatch and would re-decrement
        A.confidence. A `contradicts` edge is written for EVERY flagged pair
        and is never overwritten, so it is the authoritative idempotence
        check — query it in EITHER direction. The column check stays as a
        cheap fast path for the common single-pair case; the edge query is
        the correctness backstop for clusters.

        Codex r3: the documented rollback (backfill_supersession.py header)
        deletes `contradicts` edges but leaves `contradiction_of` as accepted
        residue. A prior version of this guard returned early on either check
        passing — which left the graph permanently missing the edge after a
        rollback, since a re-run would hit the (still-set) column fast path
        and exit before ever touching the edge table. The -0.2 decrement is
        the only ONE-TIME effect and stays gated on `already_flagged`
        (column OR edge — either proves a prior pass); the column
        assignment and edge creation now run UNCONDITIONALLY every call —
        both are idempotent (column: same-value reassignment; edge:
        on_conflict_do_nothing) — so a rolled-back edge is recreated on the
        next pass while confidence is never touched twice."""
        already_flagged = b.contradiction_of == a.id
        if not already_flagged:
            existing = await session.execute(
                select(GraphEdge.id)
                .where(GraphEdge.agent_id == self.agent_id)
                .where(GraphEdge.relation == "contradicts")
                .where(
                    or_(
                        and_(GraphEdge.source_id == b.id, GraphEdge.target_id == a.id),
                        and_(GraphEdge.source_id == a.id, GraphEdge.target_id == b.id),
                    )
                )
                .limit(1)
            )
            already_flagged = existing.first() is not None
        if not already_flagged:
            a.confidence = max(0.0, (a.confidence or 1.0) - 0.2)
        b.contradiction_of = a.id
        await self._create_graph_edge(b.id, a.id, "fact", "fact", "contradicts", 1.0, session)

    # ------------------------------------------------------------------
    # contradict()
    # ------------------------------------------------------------------

    async def contradict(
        self,
        fact_id: UUID,
        contradicting_fact: FactInput,
        session: AsyncSession | None = None,
    ) -> FactDetail:
        """Store a fact that contradicts an existing one."""
        if session is None:
            async with self.db.session() as session:
                result = await self._contradict(fact_id, contradicting_fact, session)
                await session.commit()
                return result
        return await self._contradict(fact_id, contradicting_fact, session)

    async def _contradict(
        self,
        fact_id: UUID,
        contradicting_fact: FactInput,
        session: AsyncSession,
    ) -> FactDetail:
        # Verify target fact exists
        old_fact = await self._get_fact_orm(fact_id, session)
        if old_fact is None:
            raise ValueError(f"Fact {fact_id} not found")

        # F023: Bypass admission gate for intentional contradictions
        bypass_input = contradicting_fact.model_copy(update={"source": "contradict"})
        new_detail = await self._learn(bypass_input, [fact_id], False, session)
        if isinstance(new_detail, FactRejected):
            raise RuntimeError("Contradict bypass failed — admission should not reject bypassed sources")

        # Set contradiction_of on the new fact
        new_fact_orm = await self._get_fact_orm(new_detail.id, session)
        if new_fact_orm is not None:
            new_fact_orm.contradiction_of = fact_id
            await session.flush()

        # F022: Bridge — also create graph edge
        await self._create_graph_edge(
            new_detail.id, fact_id, "fact", "fact", "contradicts", 1.0, session
        )

        # Reduce confidence of old fact by 0.2 (min 0.0)
        old_confidence = old_fact.confidence or 1.0
        old_fact.confidence = max(0.0, old_confidence - 0.2)
        await session.flush()

        # Re-read new fact to get updated contradiction_of
        updated = await self._get_fact_orm(new_detail.id, session)
        return self._to_detail(updated)

    # ------------------------------------------------------------------
    # get()
    # ------------------------------------------------------------------

    async def get(self, fact_id: UUID, session: AsyncSession | None = None) -> FactDetail | None:
        """Fetch a single fact."""
        if session is None:
            async with self.db.session() as session:
                return await self._get(fact_id, session)
        return await self._get(fact_id, session)

    async def _get(self, fact_id: UUID, session: AsyncSession) -> FactDetail | None:
        fact = await self._get_fact_orm(fact_id, session)
        if fact is None:
            return None
        return self._to_detail(fact)

    # ------------------------------------------------------------------
    # list_by_category() — Tier 1 always-on facts
    # ------------------------------------------------------------------

    async def list_by_category(
        self,
        categories: list[str],
        active_only: bool = True,
        limit: int = 20,
        session: AsyncSession | None = None,
    ) -> list[FactSummary]:
        """Load facts by category without semantic search.

        Used for Tier 1 always-on context (preference, person, rule facts).
        """
        if session is None:
            async with self.db.session() as session:
                return await self._list_by_category(categories, active_only, limit, session)
        return await self._list_by_category(categories, active_only, limit, session)

    async def _list_by_category(
        self,
        categories: list[str],
        active_only: bool,
        limit: int,
        session: AsyncSession,
    ) -> list[FactSummary]:
        stmt = (
            select(Fact)
            .where(
                Fact.agent_id == self.agent_id,
                Fact.category.in_(categories),
            )
        )
        if active_only:
            stmt = stmt.where(Fact.active == True)  # noqa: E712
        stmt = stmt.order_by(Fact.confidence.desc()).limit(limit)
        result = await session.execute(stmt)
        facts = result.scalars().all()
        return [
            FactSummary(
                id=f.id,
                content=f.content,
                category=f.category,
                subject=f.subject,
                confidence=f.confidence or 1.0,
                active=f.active if f.active is not None else True,
                score=1.0,  # Tier 1: always-on, no relevance ranking
                actionable=f.actionable,
                actionable_confidence=f.actionable_confidence,
                tags=list(f.tags or []),
                event_date=f.event_date,  # F075
                overrides_prior=bool(f.overrides_prior or False),  # R2.4
            )
            for f in facts
        ]

    # ------------------------------------------------------------------
    # find_similar_for_dedup()
    # ------------------------------------------------------------------

    async def find_similar_for_dedup(
        self,
        content: str,
        limit: int = 5,
        session: AsyncSession | None = None,
    ) -> list[FactSummary]:
        """Raw-cosine nearest-neighbor probe for write-path dedup (audit S1).

        Unlike :meth:`search`, the ``score`` on each summary is the raw
        cosine similarity in ``[0, 1]`` — a calibrated closeness measure a
        dedup threshold can be meaningfully compared against. RRF scores
        from ``search()`` encode *rank*, not closeness: the nearest fact
        scores ~0.98 at limit=1 no matter how dissimilar it is, which made
        every RRF-thresholded dedup pre-check fire unconditionally.

        Also deliberately does NOT fire access tracking — a dedup probe is
        not a recall, and tracking it inflated ``recall_count`` /
        ``last_recalled_at`` on facts no consumer ever saw (audit S9).

        Returns ``[]`` when the embedding cannot be generated, so dedup
        degrades the same way ``_learn``'s embed-failure path does.
        Results are similarity-descending.
        """
        if session is None:
            async with self.db.session() as session:
                return await self._find_similar_for_dedup(content, limit, session)
        return await self._find_similar_for_dedup(content, limit, session)

    async def get_superseded_contents(
        self,
        fact_ids: list[UUID],
        session: AsyncSession | None = None,
    ) -> dict[UUID, list[str]]:
        """Map superseder fact id -> contents of facts it superseded (max 2, newest first).

        Reads the authoritative ``superseded_by`` column (NOT graph edges, which
        historically lag it). Includes inactive rows on purpose — supersession
        deactivates the old fact, and the old content is exactly what the
        lineage annotation needs. Agent-scoped like every FactManager read.
        """
        if not fact_ids:
            return {}
        if session is None:
            async with self.db.session() as session:
                return await self._get_superseded_contents_impl(fact_ids, session)
        return await self._get_superseded_contents_impl(fact_ids, session)

    async def _get_superseded_contents_impl(
        self, fact_ids: list[UUID], session: AsyncSession
    ) -> dict[UUID, list[str]]:
        stmt = (
            select(Fact.superseded_by, Fact.content)
            .where(
                Fact.agent_id == self.agent_id,
                Fact.superseded_by.in_(fact_ids),
            )
            .order_by(Fact.created_at.desc(), Fact.id.desc())
        )
        rows = (await session.execute(stmt)).all()
        out: dict[UUID, list[str]] = {}
        for superseder_id, content in rows:
            bucket = out.setdefault(superseder_id, [])
            if len(bucket) < 2:
                bucket.append(content or "")
        return out

    async def _find_similar_for_dedup(
        self,
        content: str,
        limit: int,
        session: AsyncSession,
    ) -> list[FactSummary]:
        if not self.embeddings:
            return []
        try:
            embedding = await self.embeddings.embed(content)
        except Exception:
            logger.warning("Embedding generation failed for dedup probe")
            return []

        embedding_str = "[" + ",".join(str(float(v)) for v in embedding) + "]"
        # Same ANN-horizon margin as _find_duplicate (codex P2) — filters
        # are post-applied to the approximate walk.
        await set_local_ef_search(session, 100)
        sql = text("""
            SELECT id, 1 - (embedding <=> CAST(:embedding AS vector)) AS similarity
            FROM heart.facts
            WHERE agent_id = :agent_id
              AND active = true
              AND embedding IS NOT NULL
            ORDER BY embedding <=> CAST(:embedding AS vector)
            LIMIT :limit
        """)
        rows = (await session.execute(sql, {
            "embedding": embedding_str,
            "agent_id": self.agent_id,
            "limit": limit,
        })).all()
        if not rows:
            return []

        ids = [r.id for r in rows]
        sims = {r.id: float(r.similarity) for r in rows}
        fact_result = await session.execute(select(Fact).where(Fact.id.in_(ids)))
        facts = {f.id: f for f in fact_result.scalars().all()}

        return [
            FactSummary(
                id=f.id,
                content=f.content,
                category=f.category,
                subject=f.subject,
                confidence=f.confidence or 1.0,
                active=f.active if f.active is not None else True,
                score=sims.get(f.id),
                superseded_by=f.superseded_by,
                actionable=f.actionable,
                actionable_confidence=f.actionable_confidence,
                tags=list(f.tags or []),
                event_date=f.event_date,
                overrides_prior=bool(f.overrides_prior or False),  # R2.4
            )
            for fid in ids
            if (f := facts.get(fid)) is not None
        ]

    # ------------------------------------------------------------------
    # search()
    # ------------------------------------------------------------------

    async def search(
        self,
        query: str,
        limit: int = 10,
        category: str | None = None,
        active_only: bool = True,
        exclude_categories: list[str] | None = None,
        session: AsyncSession | None = None,
        variant_pairs: list[tuple[str, list[float] | None]] | None = None,
        date_window: "DateWindow | None" = None,
    ) -> list[FactSummary]:
        """Hybrid search over facts.

        Args:
            variant_pairs: F050 — when set with len > 1, routes through
                hybrid_search_multi for RRF fusion across query variants.
                Defaults to None (single-query path; backwards compatible).
            date_window: F075 L3 — when set, fuses the date-window retrieval
                leg into the results via _rrf_merge_n.
        """
        if session is None:
            async with self.db.session() as session:
                return await self._search(query, limit, category, active_only, exclude_categories, session, variant_pairs, date_window)
        return await self._search(query, limit, category, active_only, exclude_categories, session, variant_pairs, date_window)

    async def _search(
        self,
        query: str,
        limit: int,
        category: str | None,
        active_only: bool,
        exclude_categories: list[str] | None,
        session: AsyncSession,
        variant_pairs: list[tuple[str, list[float] | None]] | None = None,
        date_window: "DateWindow | None" = None,
    ) -> list[FactSummary]:
        # Generate query embedding
        embedding = None
        if self.embeddings:
            try:
                embedding = await self.embeddings.embed(query)
            except Exception:
                logger.warning("Embedding generation failed for fact search")

        extra_where = ""
        extra_params: dict = {}
        if category:
            extra_where += " AND t.category = :category"
            extra_params["category"] = category
        if exclude_categories:
            # Tier 3: exclude Tier 1 categories from semantic search
            placeholders = ", ".join(f":exc_{i}" for i in range(len(exclude_categories)))
            extra_where += f" AND (t.category IS NULL OR t.category NOT IN ({placeholders}))"
            for i, cat in enumerate(exclude_categories):
                extra_params[f"exc_{i}"] = cat

        # Note: hybrid_search always applies active=true filter.
        # For active_only=False, we need a different approach.
        if not active_only:
            # F050 routing: active_only=False bypasses hybrid_search; variants ignored
            # on this path. If variant routing is needed here, wire hybrid_search_multi
            # similarly to the active_only=True path below.
            # Override the default active filter by using raw search
            # The hybrid_search helper always filters active=true,
            # so for inactive facts we do a simpler query.
            return await self._search_all(query, embedding, limit, category, session)

        if variant_pairs and len(variant_pairs) > 1:
            results = await hybrid_search_multi(
                session=session,
                table="heart.facts",
                queries=variant_pairs,
                agent_id=self.agent_id,
                extra_where=extra_where,
                extra_params=extra_params,
                limit=limit,
            )
        else:
            results = await hybrid_search(
                session=session,
                table="heart.facts",
                embedding=embedding,
                query_text=query,
                agent_id=self.agent_id,
                extra_where=extra_where,
                extra_params=extra_params,
                limit=limit,
            )

        # F075 L3: fuse the date-window leg (position-based RRF). Present only when
        # the caller parsed a window and we have a query embedding. Empty leg is a
        # no-op, so this preserves today's ordering when the window finds nothing.
        if date_window is not None and embedding is not None:
            from nous.heart.search import _rrf_merge_n, _resolve_rrf_k
            k_leg = self._settings.date_leg_k if self._settings else 15
            date_leg = await self._date_window_leg(session, embedding, date_window, k_leg)
            if date_leg:
                results = _rrf_merge_n([results, date_leg], _resolve_rrf_k(), limit)

        if not results:
            return []

        ids = [r[0] for r in results]
        scores = {r[0]: r[1] for r in results}

        fact_result = await session.execute(select(Fact).where(Fact.id.in_(ids)))
        facts = {f.id: f for f in fact_result.scalars().all()}

        summaries = [
            FactSummary(
                id=f.id,
                content=f.content,
                category=f.category,
                subject=f.subject,
                confidence=f.confidence or 1.0,
                active=f.active if f.active is not None else True,
                score=scores.get(f.id),
                superseded_by=f.superseded_by,
                actionable=f.actionable,
                actionable_confidence=f.actionable_confidence,
                tags=list(f.tags or []),
                event_date=f.event_date,  # F075
                overrides_prior=bool(f.overrides_prior or False),  # R2.4
            )
            for fid in ids
            if (f := facts.get(fid)) is not None
        ]

        # F027: Apply supersession filter and fire access tracking
        summaries = self.apply_supersession_filter(summaries)
        self._fire_track_access([s.id for s in summaries])
        return summaries

    async def _search_all(
        self,
        query: str,
        embedding: list[float] | None,
        limit: int,
        category: str | None,
        session: AsyncSession,
    ) -> list[FactSummary]:
        """Search all facts including inactive (no active filter).

        Uses RRF (Reciprocal Rank Fusion) for hybrid search — same approach
        as hybrid_search() but intentionally omits the active=true filter so
        superseded/inactive facts are included.
        """
        from nous.heart.search import _resolve_vector_weight, _resolve_rrf_k, _rrf_merge

        vw = _resolve_vector_weight()
        rrf_k = _resolve_rrf_k()

        params: dict = {
            "agent_id": self.agent_id,
            "query_text": query,
            "limit": limit,
            "limit_expanded": limit * 3,
        }
        filter_extra = ""
        if category:
            filter_extra = "AND t.category = :category"
            params["category"] = category

        vector_results: list[tuple] = []
        keyword_results: list[tuple] = []

        if embedding is not None:
            params["query_embedding"] = "[" + ",".join(str(float(v)) for v in embedding) + "]"
            vector_sql = text(f"""
                SELECT t.id, 1 - (t.embedding <=> CAST(:query_embedding AS vector)) AS score
                FROM heart.facts t
                WHERE t.embedding IS NOT NULL
                  AND t.agent_id = :agent_id {filter_extra}
                ORDER BY t.embedding <=> CAST(:query_embedding AS vector)
                LIMIT :limit_expanded
            """)
            result = await session.execute(vector_sql, params)
            vector_results = [(row.id, float(row.score)) for row in result.all()]

        keyword_sql = text(f"""
            SELECT t.id,
                ts_rank_cd(t.search_tsv, plainto_tsquery('english', :query_text))
                / (1.0 + ts_rank_cd(t.search_tsv, plainto_tsquery('english', :query_text))) AS score
            FROM heart.facts t
            WHERE t.search_tsv @@ plainto_tsquery('english', :query_text)
              AND t.agent_id = :agent_id {filter_extra}
            ORDER BY score DESC
            LIMIT :limit_expanded
        """)
        result = await session.execute(keyword_sql, params)
        keyword_results = [(row.id, float(row.score)) for row in result.all()]

        if embedding is None:
            ranked = keyword_results[:limit]
        else:
            ranked = _rrf_merge(vector_results, keyword_results, rrf_k, vw, limit)

        if not ranked:
            return []

        ids = [r[0] for r in ranked]
        scores = {r[0]: r[1] for r in ranked}

        fact_result = await session.execute(select(Fact).where(Fact.id.in_(ids)))
        facts = {f.id: f for f in fact_result.scalars().all()}

        summaries = [
            FactSummary(
                id=f.id,
                content=f.content,
                category=f.category,
                subject=f.subject,
                confidence=f.confidence or 1.0,
                active=f.active if f.active is not None else True,
                score=scores.get(f.id),
                superseded_by=f.superseded_by,
                actionable=f.actionable,
                actionable_confidence=f.actionable_confidence,
                tags=list(f.tags or []),
                event_date=f.event_date,  # F075
                overrides_prior=bool(f.overrides_prior or False),  # R2.4
            )
            for fid in ids
            if (f := facts.get(fid)) is not None
        ]

        # F027: Apply supersession filter and fire access tracking
        summaries = self.apply_supersession_filter(summaries)
        self._fire_track_access([s.id for s in summaries])
        return summaries

    async def _date_window_leg(
        self,
        session: "AsyncSession",
        embedding: list[float],
        window: "DateWindow",
        limit: int,
    ) -> list[tuple["UUID", float]]:
        """F075 L3: in-window active dated facts, ranked by cosine to the query.

        Returns (id, cosine) tuples ordered best-first. Ranking is cosine-to-query
        (relevance within the window), never date-distance alone.
        """
        qvec = "[" + ",".join(str(float(v)) for v in embedding) + "]"
        # Raise HNSW ef_search well above :limit: the agent/date/active predicates
        # are post-applied to the approximate walk, so a selective date window can
        # otherwise evict the in-window rows from the candidate set (codex P2).
        # Mirrors _find_duplicate (facts.py:1200) and censor probe (censors.py:409).
        await set_local_ef_search(session, 200)
        sql = text("""
            SELECT t.id, 1 - (t.embedding <=> CAST(:qvec AS vector)) AS score
            FROM heart.facts t
            WHERE t.agent_id = :agent_id
              AND t.active = true
              AND t.event_date IS NOT NULL
              AND t.event_date BETWEEN :lo AND :hi
              AND t.embedding IS NOT NULL
            ORDER BY t.embedding <=> CAST(:qvec AS vector)
            LIMIT :limit
        """)
        result = await session.execute(sql, {
            "agent_id": self.agent_id, "qvec": qvec,
            "lo": window.start, "hi": window.end, "limit": limit,
        })
        return [(row.id, float(row.score)) for row in result.all()]

    # ------------------------------------------------------------------
    # list_all() — F021 dashboard browse mode
    # ------------------------------------------------------------------

    async def list_all(
        self,
        limit: int = 50,
        offset: int = 0,
        category: str | None = None,
        active_only: bool = True,
        confidence_min: float | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        sort: str = "created_at",
        order: str = "desc",
        session: AsyncSession | None = None,
    ) -> tuple[list[FactSummary], int]:
        """Return paginated facts without search. Used by dashboard browse mode."""
        if session is None:
            async with self.db.session() as session:
                return await self._list_all(
                    limit, offset, category, active_only,
                    confidence_min, date_from, date_to, sort, order, session,
                )
        return await self._list_all(
            limit, offset, category, active_only,
            confidence_min, date_from, date_to, sort, order, session,
        )

    async def _list_all(
        self,
        limit: int,
        offset: int,
        category: str | None,
        active_only: bool,
        confidence_min: float | None,
        date_from: str | None,
        date_to: str | None,
        sort: str,
        order: str,
        session: AsyncSession,
    ) -> tuple[list[FactSummary], int]:
        from sqlalchemy import func as sa_func

        conditions = [Fact.agent_id == self.agent_id]
        if active_only:
            conditions.append(Fact.active == True)  # noqa: E712
        if category:
            conditions.append(Fact.category == category)
        if confidence_min is not None:
            conditions.append(Fact.confidence >= confidence_min)
        if date_from:
            conditions.append(Fact.created_at >= date_from)
        if date_to:
            conditions.append(Fact.created_at <= date_to)

        # Count
        count_q = select(sa_func.count()).select_from(Fact).where(*conditions)
        total = (await session.execute(count_q)).scalar() or 0

        # Sort — VALIDATE against allowlist to prevent attribute injection
        ALLOWED_SORTS = {"created_at", "confidence", "category", "subject"}
        if sort not in ALLOWED_SORTS:
            sort = "created_at"
        if order not in ("asc", "desc"):
            order = "desc"
        sort_col = getattr(Fact, sort)
        order_clause = sort_col.desc() if order == "desc" else sort_col.asc()

        # Fetch
        q = select(Fact).where(*conditions).order_by(order_clause).limit(limit).offset(offset)
        result = await session.execute(q)
        facts = list(result.scalars().all())

        # F047: FactSummary now carries tags + actionable; keep this site
        # in sync with _search / _search_all / _list_by_category.
        summaries = [
            FactSummary(
                id=f.id,
                content=f.content,
                category=f.category,
                subject=f.subject,
                confidence=f.confidence or 1.0,
                active=f.active if f.active is not None else True,
                actionable=f.actionable,
                actionable_confidence=f.actionable_confidence,
                tags=list(f.tags or []),
                event_date=f.event_date,  # F075
                overrides_prior=bool(f.overrides_prior or False),  # R2.4
            )
            for f in facts
        ]
        return summaries, total

    # ------------------------------------------------------------------
    # count_stale() — F034: Heartbeat health check
    # ------------------------------------------------------------------

    async def count_stale(self, older_than_days: int = 30, session: AsyncSession | None = None) -> int:
        """Count active facts not updated in N days."""
        if session is None:
            async with self.db.session() as session:
                return await self._count_stale(older_than_days, session)
        return await self._count_stale(older_than_days, session)

    async def _count_stale(self, older_than_days: int, session: AsyncSession) -> int:
        from datetime import timedelta
        cutoff = datetime.now(UTC) - timedelta(days=older_than_days)
        result = await session.execute(
            select(func.count())
            .select_from(Fact)
            .where(Fact.agent_id == self.agent_id)
            .where(Fact.active == True)  # noqa: E712
            .where(Fact.updated_at < cutoff)
        )
        return result.scalar() or 0

    # ------------------------------------------------------------------
    # get_current() — P3-5: recursive CTE
    # ------------------------------------------------------------------

    async def get_current(self, fact_id: UUID, session: AsyncSession | None = None) -> FactDetail:
        """Follow superseded_by chain to find current version of a fact.

        A detected supersession cycle is repaired in place; with a caller-provided
        session the repair is flushed and the caller owns the commit.
        """
        if session is None:
            async with self.db.session() as session:
                result = await self._get_current(fact_id, session)
                await session.commit()
                return result
        return await self._get_current(fact_id, session)

    async def _get_current(self, fact_id: UUID, session: AsyncSession) -> FactDetail:
        # Iterative chain walk: run a depth<100 path-guarded CTE from current_id
        # each iteration. A NULL-tip row → done (healthy chain). A depth-exhausted
        # CTE with no NULL tip → restart from the deepest visited row. A Python-side
        # visited set guards cross-restart cycles (A→…→B after 100 links → B already
        # in visited → repair). Intra-CTE cycles (superseded_by IN path) go to the
        # existing latest-learned cycle repair. Restarts are capped at 100 (≈10k-link
        # chains before ValueError).
        _tip_sql = text("""
            WITH RECURSIVE chain AS (
                SELECT id, superseded_by, 1 AS depth, ARRAY[id]::uuid[] AS path
                FROM heart.facts
                WHERE id = :start_id AND agent_id = :agent_id
                UNION ALL
                SELECT f.id, f.superseded_by, c.depth + 1, c.path || f.id
                FROM heart.facts f
                JOIN chain c ON f.id = c.superseded_by
                WHERE NOT (f.id = ANY(c.path))
                  AND c.depth < 100
            )
            SELECT id FROM chain WHERE superseded_by IS NULL
        """)
        _diag_sql = text("""
            WITH RECURSIVE chain AS (
                SELECT id, superseded_by, learned_at, 1 AS depth,
                       ARRAY[id]::uuid[] AS path
                FROM heart.facts WHERE id = :start_id AND agent_id = :agent_id
                UNION ALL
                SELECT f.id, f.superseded_by, f.learned_at, c.depth + 1,
                       c.path || f.id
                FROM heart.facts f JOIN chain c ON f.id = c.superseded_by
                WHERE NOT (f.id = ANY(c.path))
                  AND c.depth < 100
            )
            SELECT id, learned_at, superseded_by,
                   (superseded_by IS NOT NULL
                    AND superseded_by = ANY(path)) AS is_cycle
            FROM chain ORDER BY depth DESC LIMIT 1
        """)
        _winner_sql = text("""
            WITH RECURSIVE chain AS (
                SELECT id, superseded_by, learned_at, 1 AS depth,
                       ARRAY[id]::uuid[] AS path
                FROM heart.facts WHERE id = :start_id AND agent_id = :agent_id
                UNION ALL
                SELECT f.id, f.superseded_by, f.learned_at, c.depth + 1,
                       c.path || f.id
                FROM heart.facts f JOIN chain c ON f.id = c.superseded_by
                WHERE NOT (f.id = ANY(c.path))
                  AND c.depth < 100
            )
            SELECT id FROM chain ORDER BY learned_at DESC NULLS LAST LIMIT 1
        """)

        visited: set[UUID] = set()
        current_id = fact_id
        max_restarts = 100

        for _restart in range(max_restarts + 1):
            params = {"start_id": current_id, "agent_id": self.agent_id}

            result = await session.execute(_tip_sql, params)
            row = result.first()
            if row is not None:
                # Healthy path: CTE found the NULL-tip row.
                current_fact = await self._get_fact_orm(row.id, session)
                if current_fact is None:
                    raise ValueError(f"Current fact for {fact_id} not found")
                return self._to_detail(current_fact)

            # No NULL-tip — depth-exhausted or intra-CTE cycle.
            diag_rows = await session.execute(_diag_sql, params)
            deepest = diag_rows.first()
            if deepest is None:
                raise ValueError(f"Fact {fact_id} not found")

            if deepest.is_cycle or deepest.id in visited:
                # True intra-CTE cycle OR cross-restart cycle detected.
                # Repair: pick latest-learned member, null superseded_by + reactivate.
                if deepest.id in visited:
                    # Cross-restart cycle: the cycle spans >100 links so _winner_sql
                    # (a 100-depth CTE from the current restart point) may exclude the
                    # true latest-learned member.  Select over the full visited set
                    # which covers all restart endpoints accumulated so far.
                    winner_rows = await session.execute(
                        text("""
                            SELECT id FROM heart.facts
                            WHERE agent_id = :agent_id
                              AND id::text = ANY(:visited_strs)
                            ORDER BY learned_at DESC NULLS LAST, id DESC
                            LIMIT 1
                        """),
                        {
                            "agent_id": self.agent_id,
                            "visited_strs": [str(v) for v in visited],
                        },
                    )
                else:
                    # Intra-CTE cycle: the whole cycle fits within the 100-node
                    # window; the CTE-based winner correctly covers all members.
                    winner_rows = await session.execute(_winner_sql, params)
                tip = winner_rows.first()
                if tip is None:
                    raise ValueError(f"Fact {fact_id} not found")
                winner = await self._get_fact_orm(tip.id, session)
                if winner is None:
                    raise ValueError(f"Cycle winner {tip.id} not found for fact {fact_id}")
                logger.warning(
                    "Supersession CYCLE detected at fact %s — breaking: winner %s",
                    fact_id, tip.id,
                )
                winner.superseded_by = None
                winner.active = True
                await session.flush()
                return self._to_detail(winner)

            # Depth-exhausted, acyclic so far — restart from deepest visited row.
            visited.add(deepest.id)
            current_id = deepest.id

        raise ValueError(
            f"supersession chain exceeds walk bound ({max_restarts} restarts)"
        )

    # ------------------------------------------------------------------
    # deactivate()
    # ------------------------------------------------------------------

    async def deactivate(self, fact_id: UUID, session: AsyncSession | None = None) -> None:
        """Soft-delete a fact."""
        if session is None:
            async with self.db.session() as session:
                await self._deactivate(fact_id, session)
                await session.commit()
                return
        await self._deactivate(fact_id, session)

    async def _deactivate(self, fact_id: UUID, session: AsyncSession) -> None:
        fact = await self._get_fact_orm(fact_id, session)
        if fact is None:
            raise ValueError(f"Fact {fact_id} not found")
        fact.active = False
        await session.flush()

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _get_fact_orm(self, fact_id: UUID, session: AsyncSession) -> Fact | None:
        """Fetch Fact ORM scoped by agent_id."""
        result = await session.execute(select(Fact).where(Fact.id == fact_id).where(Fact.agent_id == self.agent_id))
        return result.scalars().first()

    def _to_detail(self, fact: Fact) -> FactDetail:
        """Convert ORM Fact to FactDetail DTO."""
        return FactDetail(
            id=fact.id,
            agent_id=fact.agent_id,
            content=fact.content,
            category=fact.category,
            subject=fact.subject,
            confidence=fact.confidence or 1.0,
            source=fact.source,
            source_episode_id=fact.source_episode_id,
            source_decision_id=fact.source_decision_id,
            learned_at=fact.learned_at,
            last_confirmed=fact.last_confirmed,
            confirmation_count=fact.confirmation_count or 0,
            superseded_by=fact.superseded_by,
            contradiction_of=fact.contradiction_of,
            active=fact.active if fact.active is not None else True,
            tags=fact.tags or [],
            created_at=fact.created_at,
            actionable=fact.actionable,
            actionable_confidence=fact.actionable_confidence,
            event_date=fact.event_date,  # F075
            overrides_prior=bool(fact.overrides_prior or False),  # R2.4
        )

    # ------------------------------------------------------------------
    # find_contradiction_candidates() — F031
    # ------------------------------------------------------------------

    async def find_contradiction_candidates(
        self,
        limit: int = 10,
        session: AsyncSession | None = None,
    ) -> list[dict]:
        """Find active fact pairs with same subject and high embedding similarity.

        Returns dicts with: fact1_id, fact2_id, content1, content2, date1, date2, subject, category, similarity.
        These are contradiction candidates that slipped past write-time detection.
        Uses similarity range 0.75-0.95 (below 0.75 is unrelated, above 0.95 is near-dupe).
        """
        if session is None:
            async with self.db.session() as session:
                return await self._find_contradiction_candidates(limit, session)
        return await self._find_contradiction_candidates(limit, session)

    async def _find_contradiction_candidates(
        self,
        limit: int,
        session: AsyncSession,
    ) -> list[dict]:
        params: dict = {"agent_id": self.agent_id, "limit": limit}
        # F031 re-check cooldown: skip pairs resolved within the cooldown window
        # so genuine-KEEP_BOTH pairs aren't re-resolved every sleep cycle (wasted
        # LLM calls + LIMIT-slot starvation). Reuses the persisted resolution
        # events — no new table. After the window the pair is reconsidered.
        # BEST-EFFORT BY DESIGN: this is an optimization, not a correctness gate.
        # The f031 event is written fire-and-forget (sleep_handler), so a lost
        # event (crash before the task runs, or insert failure) simply means the
        # pair is re-processed once more next cycle — re-resolved KEEP_BOTH
        # (idempotent) and the event re-written. It can never produce a wrong
        # result, only momentarily revert to the pre-cooldown re-processing it
        # reduces; not worth synchronizing the background event write to harden.
        cooldown_days = (
            getattr(self._settings, "contradiction_recheck_cooldown_days", 30)
            if self._settings is not None else 30
        )
        cooldown_clause = ""
        if cooldown_days and cooldown_days > 0:
            params["cooldown_days"] = int(cooldown_days)
            cooldown_clause = """
              AND NOT EXISTS (
                  SELECT 1 FROM nous_system.events e
                  WHERE e.agent_id = :agent_id
                    AND e.event_type = 'f031_contradiction_resolution'
                    AND e.created_at > now() - (:cooldown_days * interval '1 day')
                    -- Only cool down GENUINE keep-both verdicts. Truncation-
                    -- downgraded merges (raw_action='MERGE') must still retry —
                    -- they are mergeable and now succeed under the 1000-token cap,
                    -- so they should NOT be suppressed by the cooldown.
                    AND e.data->>'raw_action' = 'KEEP_BOTH'
                    AND (
                        (e.data->>'fact1_id' = f1.id::text AND e.data->>'fact2_id' = f2.id::text)
                        OR (e.data->>'fact1_id' = f2.id::text AND e.data->>'fact2_id' = f1.id::text)
                    )
              )"""
        sql = text(f"""
            SELECT f1.id AS fact1_id, f2.id AS fact2_id,
                   f1.content AS content1, f2.content AS content2,
                   f1.created_at AS date1, f2.created_at AS date2,
                   f1.subject AS subject, f1.category AS category,
                   1 - (f1.embedding <=> f2.embedding) AS similarity
            FROM heart.facts f1
            JOIN heart.facts f2 ON f1.agent_id = f2.agent_id
              AND f1.id < f2.id
              AND f2.active = true
              AND f2.embedding IS NOT NULL
              AND f2.subject IS NOT NULL
              AND LOWER(f1.subject) = LOWER(f2.subject)
              AND 1 - (f1.embedding <=> f2.embedding) > 0.75
              AND 1 - (f1.embedding <=> f2.embedding) < 0.95
            WHERE f1.agent_id = :agent_id
              AND f1.active = true
              AND f1.embedding IS NOT NULL
              AND f1.subject IS NOT NULL
              {cooldown_clause}
            ORDER BY similarity DESC
            LIMIT :limit
        """)
        result = await session.execute(sql, params)
        return [
            {
                "fact1_id": row.fact1_id,
                "fact2_id": row.fact2_id,
                "content1": row.content1,
                "content2": row.content2,
                "date1": row.date1,
                "date2": row.date2,
                "subject": row.subject,
                "category": row.category,
                "similarity": float(row.similarity),
            }
            for row in result.all()
        ]

    async def entity_key_vocabulary(self, limit: int = 50_000) -> frozenset[str]:
        """R3.3: distinct entity keys of ACTIVE facts for this agent (NER-lite
        vocab matching). Active join per the fact_entity_keys read invariant.

        codex P2: cached on this instance (TTL `_ENTITY_VOCAB_TTL_SECONDS`)
        and invalidated at both entity-row write sites (`_learn`,
        `_confirm_duplicate`) so a fact learned by THIS process is visible to
        the vocab immediately, not after the TTL elapses. The TTL still
        applies as a floor for entity keys written by a DIFFERENT process
        (e.g. the backfill script), which cannot invalidate this instance's
        cache.

        codex P2 round 3: the DB round-trip below has an `await`, so a write
        can land while this call is in flight. Snapshotting
        `self._entity_vocab_gen` before the round-trip and only STORING the
        result if it is unchanged afterward closes the "late store" race the
        round-2 dirty flag couldn't: a rebuild that STARTS before a write
        invalidates the cache but FINISHES after would otherwise clobber the
        (correctly cleared) cache with a now-stale result — the dirty flag
        only gates the write side's own re-invalidation, never a
        concurrently in-flight read's store. Race-free without a lock:
        asyncio is single-threaded/cooperative, so a writer can only
        interleave at the `await` inside the round-trip; the comparison and
        the store immediately after it are synchronous with no `await`
        between them, so nothing can interleave there either.
        """
        cached = self._entity_vocab_cache
        now = time.monotonic()
        if cached is not None and now - cached[1] < _ENTITY_VOCAB_TTL_SECONDS:
            return cached[0]
        gen = self._entity_vocab_gen
        async with self.db.session() as session:
            rows = await session.execute(
                text("SELECT DISTINCT ek.entity_key FROM heart.fact_entity_keys ek "
                     "JOIN heart.facts f ON f.id = ek.fact_id "
                     "WHERE ek.agent_id = :a AND f.active = true LIMIT :lim"),
                {"a": self.agent_id, "lim": limit},
            )
            vocab = frozenset(r[0] for r in rows)
        if gen == self._entity_vocab_gen:
            self._entity_vocab_cache = (vocab, now)
        return vocab

    async def fetch_by_entity_keys(self, keys: list[str], limit: int = 8):
        """R3.3: active facts matching any entity key, ranked by matched-key
        count then recency/ordinal. MUST join facts on active=true (entity
        rows survive supersession).

        codex P2: also returns ``subject``/``event_date`` so
        ``_keyed_to_pipeline`` can populate the same metadata keys the normal
        fact-recall path does — otherwise keyed-only dated facts are invisible
        to the recency resolver's same-subject grouping.

        codex P2 round 6: also returns ``source_episode_id`` (cast to text,
        matching ``_attach_fact_source_episodes``'s convention exactly).
        That helper runs BEFORE the keyed leg's results are merged into
        ``run_recall_pipeline``'s output list, so it can never attach this
        field to a keyed hit — without it here, keyed facts silently fall
        into the formatter's "-- Other --" session bucket under
        session-grouped display.

        codex P2 round 10: fires access tracking (recall_count,
        last_recalled_at) for every returned row, in the SAME
        session/transaction as the SELECT — retrieval == access, mirroring
        ``search()``'s semantics. This is deliberately the OPPOSITE of
        ``find_similar_facts``'s dedup probe, which opts OUT of tracking
        (audit S9: a dedup probe never surfaces to a consumer, so tracking
        it inflates the counters for nothing). A keyed hit DOES surface —
        it's a real retrieval leg result — so without this, a fact found
        ONLY via its entity keys never accumulates recall signal, and
        ``_phase_stale_scan`` can deactivate a fact that is in active keyed
        use. Skipped when the result set is empty; failures are swallowed
        (mirrors ``track_access``) so a tracking hiccup never turns into a
        lost retrieval result for the caller.
        """
        if not keys:
            return []
        async with self.db.session() as session:
            rows = await session.execute(
                text(
                    "SELECT f.id, f.content, f.learned_at, f.source_ordinal, "
                    "       f.subject, f.event_date, "
                    "       f.source_episode_id::text AS source_episode_id, "
                    "       COUNT(DISTINCT ek.entity_key) AS matched "
                    "FROM heart.fact_entity_keys ek "
                    "JOIN heart.facts f ON f.id = ek.fact_id "
                    "WHERE ek.agent_id = :a AND ek.entity_key = ANY(:keys) "
                    "  AND f.active = true AND f.agent_id = :a "
                    "GROUP BY f.id, f.content, f.learned_at, f.source_ordinal, "
                    "         f.subject, f.event_date, f.source_episode_id "
                    "ORDER BY matched DESC, f.learned_at DESC, "
                    "         f.source_ordinal DESC NULLS LAST "
                    "LIMIT :lim"
                ),
                {"a": self.agent_id, "keys": keys, "lim": limit},
            )
            result_rows = list(rows)
            if result_rows:
                try:
                    await session.execute(
                        update(Fact)
                        .where(Fact.id.in_([r.id for r in result_rows]))
                        .values(
                            recall_count=Fact.recall_count + 1,
                            last_recalled_at=datetime.now(UTC),
                        )
                    )
                    await session.commit()
                except Exception:
                    logger.debug(
                        "R3.3: keyed-leg access tracking failed for %d facts",
                        len(result_rows),
                    )
            return result_rows
