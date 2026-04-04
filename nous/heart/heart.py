"""Main Heart class — public API for the memory organ.

Composes all five managers (episodes, facts, procedures, censors,
working memory) and provides unified recall across memory types.

All methods delegate to managers, passing session through for
transaction injection (P1-1).
"""

from __future__ import annotations

import logging
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from nous.brain.embeddings import EmbeddingProvider
from nous.config import Settings
from nous.heart.censors import CensorManager
from nous.heart.episodes import EpisodeManager
from nous.heart.facts import FactManager
from nous.heart.procedures import ProcedureManager
from nous.heart.admission import AdmissionConfig, AdmissionController
from nous.heart.schemas import (
    CensorDetail,
    CensorInput,
    CensorMatch,
    EpisodeDetail,
    EpisodeInput,
    EpisodeSummary,
    FactDetail,
    FactInput,
    FactRejected,
    FactSummary,
    OpenThread,
    ProcedureDetail,
    ProcedureInput,
    ProcedureOutcome,
    ProcedureSummary,
    RecallResult,
    WorkingMemoryItem,
    WorkingMemoryState,
)
from nous.heart.schedules import ScheduleManager
from nous.heart.subtasks import SubtaskManager
from nous.heart.working_memory import WorkingMemoryManager
from nous.heart.search import batch_fetch_embeddings, mmr_rerank
from nous.events import Event, EventBus
from nous.storage.database import Database
from nous.storage.models import ConversationState

logger = logging.getLogger(__name__)


class Heart:
    """Memory organ for Nous agents.

    Composes five manager classes for episodic, semantic, procedural,
    censor, and working memory. Provides unified recall with
    reciprocal rank fusion (RRF) for cross-type ranking.
    """

    def __init__(
        self,
        database: Database,
        settings: Settings,
        embedding_provider: EmbeddingProvider | None = None,
        owns_embeddings: bool = True,
    ) -> None:
        self.db = database
        self.settings = settings
        self.agent_id = settings.agent_id
        self._embeddings = embedding_provider
        self._owns_embeddings = owns_embeddings

        # F023: Construct admission controller if enabled
        admission_controller = None
        if settings.admission_control_enabled:
            admission_config = AdmissionConfig(
                weights={
                    "utility": settings.admission_w_utility,
                    "confidence": settings.admission_w_confidence,
                    "novelty": settings.admission_w_novelty,
                    "recency": settings.admission_w_recency,
                    "type_prior": settings.admission_w_type_prior,
                },
                threshold=settings.admission_threshold,
                recency_lambda=settings.admission_recency_lambda,
                utility_llm_enabled=settings.admission_utility_llm_enabled,
                utility_llm_model=settings.admission_utility_model or settings.background_model,
                shadow_mode=settings.admission_shadow_mode,
            )
            # LLM client injected post-init (same pattern as EventBus)
            admission_controller = AdmissionController(config=admission_config)

        # Initialize managers
        self.episodes = EpisodeManager(database, embedding_provider, settings.agent_id)
        self.facts = FactManager(database, embedding_provider, settings.agent_id, admission_controller)
        self.procedures = ProcedureManager(database, embedding_provider, settings.agent_id)
        self.censors = CensorManager(database, embedding_provider, settings.agent_id)
        self.working_memory = WorkingMemoryManager(database, settings.agent_id)
        self.subtasks = SubtaskManager(database, settings.agent_id)
        self.schedules = ScheduleManager(database, settings.agent_id)

        # F022 Phase 2: Optional EventBus for fact_learned emission.
        # Injected post-construction in main.py (not a constructor param
        # to keep Heart's interface stable).
        self._bus: EventBus | None = None

    # ------------------------------------------------------------------
    # Lifecycle (P2-2)
    # ------------------------------------------------------------------

    async def close(self) -> None:
        """Close owned resources (embedding provider httpx client).

        Only closes the embedding provider if this Heart instance owns it
        (owns_embeddings=True). When Brain and Heart share a provider,
        the caller should set owns_embeddings=False.
        """
        if self._owns_embeddings and self._embeddings:
            await self._embeddings.close()

    async def __aenter__(self) -> Heart:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()

    # ==================================================================
    # Episodes
    # ==================================================================

    async def start_episode(
        self,
        input: EpisodeInput,
        session: AsyncSession | None = None,
    ) -> EpisodeDetail:
        """Start a new episode."""
        return await self.episodes.start(input, session)

    async def end_episode(
        self,
        episode_id: UUID,
        outcome: str,
        lessons_learned: list[str] | None = None,
        surprise_level: float | None = None,
        transcript: str | None = None,  # F025 P3-C
        session: AsyncSession | None = None,
    ) -> EpisodeDetail:
        """Close an episode with outcome and lessons."""
        return await self.episodes.end(episode_id, outcome, lessons_learned, surprise_level, transcript, session)

    async def get_episode(self, episode_id: UUID, session: AsyncSession | None = None) -> EpisodeDetail:
        """Fetch a single episode. Raises ValueError if not found (P2-7)."""
        result = await self.episodes.get(episode_id, session)
        if result is None:
            raise ValueError(f"Episode {episode_id} not found")
        return result

    async def list_episodes(
        self,
        limit: int = 10,
        outcome: str | None = None,
        hours: int | None = None,
        session: AsyncSession | None = None,
    ) -> list[EpisodeSummary]:
        """List recent episodes."""
        return await self.episodes.list_recent(limit, outcome, hours=hours, session=session)

    async def list_episodes_paginated(
        self,
        limit: int = 50,
        offset: int = 0,
        outcome: str | None = None,
        frame: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        sort: str = "started_at",
        order: str = "desc",
        session: AsyncSession | None = None,
    ) -> tuple[list[EpisodeSummary], int]:
        """List episodes with pagination and filters (F021)."""
        return await self.episodes.list_all(
            limit, offset, outcome, frame, date_from, date_to, sort, order, session,
        )

    async def link_decision_to_episode(
        self,
        episode_id: UUID,
        decision_id: UUID,
        session: AsyncSession | None = None,
    ) -> None:
        """Link a decision to an episode."""
        await self.episodes.link_decision(episode_id, decision_id, session)

    async def link_procedure_to_episode(
        self,
        episode_id: UUID,
        procedure_id: UUID,
        effectiveness: str | None = None,
        session: AsyncSession | None = None,
    ) -> None:
        """Link a procedure to an episode."""
        await self.episodes.link_procedure(episode_id, procedure_id, effectiveness, session)

    async def deactivate_episode(self, episode_id: UUID, session: AsyncSession | None = None) -> None:
        """Soft-delete a trivial episode."""
        await self.episodes.deactivate(episode_id, session=session)

    async def update_episode_summary(
        self, episode_id: UUID, summary: dict, session: AsyncSession | None = None
    ) -> None:
        """Store structured summary on episode."""
        await self.episodes.update_summary(episode_id, summary, session=session)

    async def bump_episode_compaction_count(
        self, episode_id: UUID, session: AsyncSession | None = None
    ) -> None:
        """Increment compaction counter on episode."""
        await self.episodes.bump_compaction_count(episode_id, session=session)

    async def search_episodes(
        self,
        query: str,
        limit: int = 10,
        session: AsyncSession | None = None,
    ) -> list[EpisodeSummary]:
        """Search episodes."""
        return await self.episodes.search(query, limit, session)

    async def search_recent_episodes_by_embedding(
        self,
        query_embedding: list[float],
        hours: int = 48,
        limit: int = 1,
        session: AsyncSession | None = None,
    ) -> list[tuple[UUID, float]]:
        """Search recent episodes by direct cosine similarity for dedup."""
        return await self.episodes.search_recent_by_embedding(
            query_embedding, hours=hours, limit=limit, session=session
        )

    # ==================================================================
    # Facts
    # ==================================================================

    async def learn(
        self,
        input: FactInput,
        session: AsyncSession | None = None,
        encoded_frame: str | None = None,
        encoded_censors: list[str] | None = None,
    ) -> FactDetail | FactRejected:
        """Store a new fact with deduplication.

        Args:
            input: Fact data.
            session: Optional DB session.
            encoded_frame: Active frame when fact was learned (003.2).
            encoded_censors: Active censors when fact was learned (003.2).
        """
        result = await self.facts.learn(
            input,
            session=session,
            encoded_frame=encoded_frame,
            encoded_censors=encoded_censors,
        )

        # F023: Skip event emission for rejected facts (FactRejected has no .id)
        if isinstance(result, FactRejected):
            return result

        # F022 Phase 2: Emit on in-process EventBus for cross-type graph linking.
        # The DB audit event (via FactManager._emit_event) does NOT reach the bus.
        if self._bus is not None:
            await self._bus.emit(Event(
                type="fact_learned",
                agent_id=self.agent_id,
                data={
                    "fact_id": str(result.id),
                    "content": result.content,
                    "category": result.category,
                    "subject": result.subject,
                    "modifies": "memory",
                },
            ))

        return result

    async def confirm_fact(self, fact_id: UUID, session: AsyncSession | None = None) -> FactDetail:
        """Confirm a fact is still true."""
        return await self.facts.confirm(fact_id, session)

    async def supersede_fact(
        self,
        old_id: UUID,
        new_fact: FactInput,
        session: AsyncSession | None = None,
    ) -> FactDetail:
        """Replace a fact with a newer version."""
        return await self.facts.supersede(old_id, new_fact, session)

    async def contradict_fact(
        self,
        fact_id: UUID,
        new_fact: FactInput,
        session: AsyncSession | None = None,
    ) -> FactDetail:
        """Store a fact that contradicts an existing one."""
        return await self.facts.contradict(fact_id, new_fact, session)

    async def get_fact(self, fact_id: UUID, session: AsyncSession | None = None) -> FactDetail:
        """Fetch a single fact. Raises ValueError if not found (P2-7)."""
        result = await self.facts.get(fact_id, session)
        if result is None:
            raise ValueError(f"Fact {fact_id} not found")
        return result

    async def search_facts(
        self,
        query: str,
        limit: int = 10,
        category: str | None = None,
        active_only: bool = True,
        exclude_categories: list[str] | None = None,
        session: AsyncSession | None = None,
    ) -> list[FactSummary]:
        """Hybrid search over facts."""
        return await self.facts.search(query, limit, category, active_only, exclude_categories, session)

    async def list_facts_by_category(
        self,
        categories: list[str],
        active_only: bool = True,
        limit: int = 20,
        session: AsyncSession | None = None,
    ) -> list[FactSummary]:
        """Load facts by category without semantic search (Tier 1)."""
        return await self.facts.list_by_category(categories, active_only, limit, session)

    async def get_current_fact(self, fact_id: UUID, session: AsyncSession | None = None) -> FactDetail:
        """Follow superseded_by chain to find current version."""
        return await self.facts.get_current(fact_id, session)

    async def list_facts(
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
        """List facts with pagination and filters (F021 browse mode)."""
        return await self.facts.list_all(
            limit, offset, category, active_only,
            confidence_min, date_from, date_to, sort, order, session,
        )

    async def deactivate_fact(self, fact_id: UUID, session: AsyncSession | None = None) -> None:
        """Soft-delete a fact."""
        await self.facts.deactivate(fact_id, session)

    async def find_contradiction_candidates(
        self,
        limit: int = 10,
        session: AsyncSession | None = None,
    ) -> list[dict]:
        """F031: Find active fact pairs with same subject and high similarity."""
        return await self.facts.find_contradiction_candidates(limit=limit, session=session)

    # ==================================================================
    # Procedures
    # ==================================================================

    async def store_procedure(self, input: ProcedureInput, session: AsyncSession | None = None) -> ProcedureDetail:
        """Store a new procedure."""
        return await self.procedures.store(input, session)

    async def activate_procedure(self, procedure_id: UUID, session: AsyncSession | None = None) -> ProcedureDetail:
        """Mark a procedure as activated."""
        return await self.procedures.activate(procedure_id, session)

    async def record_procedure_outcome(
        self,
        procedure_id: UUID,
        outcome: ProcedureOutcome,
        session: AsyncSession | None = None,
    ) -> ProcedureDetail:
        """Record procedure activation outcome."""
        return await self.procedures.record_outcome(procedure_id, outcome, session)

    async def get_procedure(self, procedure_id: UUID, session: AsyncSession | None = None) -> ProcedureDetail:
        """Fetch a single procedure. Raises ValueError if not found (P2-7)."""
        result = await self.procedures.get(procedure_id, session)
        if result is None:
            raise ValueError(f"Procedure {procedure_id} not found")
        return result

    async def search_procedures(
        self,
        query: str,
        limit: int = 10,
        domain: str | None = None,
        session: AsyncSession | None = None,
    ) -> list[ProcedureSummary]:
        """Hybrid search over procedures."""
        return await self.procedures.search(query, limit, domain, session)

    async def list_procedures(
        self,
        limit: int = 50,
        offset: int = 0,
        domain: str | None = None,
        active_only: bool = True,
        min_activations: int | None = None,
        session: AsyncSession | None = None,
    ) -> tuple[list[ProcedureSummary], int]:
        """List procedures with pagination and filters (F021)."""
        return await self.procedures.list_all(
            limit, offset, domain, active_only, min_activations, session,
        )

    async def retire_procedure(self, procedure_id: UUID, session: AsyncSession | None = None) -> None:
        """Retire a procedure."""
        await self.procedures.retire(procedure_id, session)

    async def get_procedure_by_name(self, name: str, session: AsyncSession | None = None) -> ProcedureDetail | None:
        """Fetch active procedure by exact name."""
        return await self.procedures.get_by_name(name, session)

    async def reactivate_procedure(self, procedure_id: UUID, session: AsyncSession | None = None) -> None:
        """Reactivate an inactive procedure."""
        await self.procedures.reactivate(procedure_id, session)

    async def list_inactive_skill_procedures(self, session: AsyncSession | None = None) -> list[ProcedureDetail]:
        """List inactive skill procedures."""
        return await self.procedures.list_inactive_skills(session)

    async def reembed_procedures(self, session: AsyncSession | None = None) -> int:
        """Recompute embeddings for all active procedures (issue #197 backfill)."""
        return await self.procedures.reembed_all(session=session)

    # ==================================================================
    # Censors
    # ==================================================================

    async def add_censor(self, input: CensorInput, session: AsyncSession | None = None) -> CensorDetail:
        """Create a new censor."""
        return await self.censors.add(input, session)

    async def check_censors(
        self,
        text: str,
        domain: str | None = None,
        session: AsyncSession | None = None,
    ) -> list[CensorMatch]:
        """Check text against active censors (with side effects)."""
        return await self.censors.check(text, domain, session)

    async def record_false_positive(self, censor_id: UUID, session: AsyncSession | None = None) -> CensorDetail:
        """Record a false positive trigger."""
        return await self.censors.record_false_positive(censor_id, session)

    async def escalate_censor(self, censor_id: UUID, session: AsyncSession | None = None) -> CensorDetail:
        """Manually escalate censor severity."""
        return await self.censors.escalate(censor_id, session)

    async def list_censors(
        self,
        domain: str | None = None,
        session: AsyncSession | None = None,
    ) -> list[CensorDetail]:
        """List all active censors."""
        return await self.censors.list_active(domain, session)

    async def list_censors_paginated(
        self, limit: int = 50, offset: int = 0,
        action: str | None = None, active_only: bool = True,
        domain: str | None = None, session: AsyncSession | None = None,
    ) -> tuple[list[CensorDetail], int]:
        """List censors with pagination and filters (F021)."""
        return await self.censors.list_all(limit, offset, action, active_only, domain, session)

    async def deactivate_censor(self, censor_id: UUID, session: AsyncSession | None = None) -> None:
        """Deactivate a censor."""
        await self.censors.deactivate(censor_id, session)

    async def update_censor(
        self,
        censor_id: UUID,
        session: AsyncSession | None = None,
        **kwargs,
    ) -> CensorDetail:
        """Update specific fields on an existing censor (F031).

        Pass only the fields you want to change:
            trigger_action, action_instruction, unblock_pattern, reason, domain
        Pass None to clear a field.
        """
        return await self.censors.update(censor_id, session=session, **kwargs)

    # ==================================================================
    # Working Memory
    # ==================================================================

    async def get_or_create_working_memory(
        self, session_id: str, session: AsyncSession | None = None
    ) -> WorkingMemoryState:
        """Get or create working memory for a session."""
        return await self.working_memory.get_or_create(session_id, session)

    async def focus(
        self,
        session_id: str,
        task: str,
        frame: str | None = None,
        session: AsyncSession | None = None,
    ) -> WorkingMemoryState:
        """Set the current task and frame."""
        return await self.working_memory.focus(session_id, task, frame, session)

    async def load_to_working_memory(
        self,
        session_id: str,
        item: WorkingMemoryItem,
        session: AsyncSession | None = None,
    ) -> WorkingMemoryState:
        """Add an item to working memory."""
        return await self.working_memory.load_item(session_id, item, session)

    async def evict_from_working_memory(
        self,
        session_id: str,
        ref_id: UUID | None = None,
        session: AsyncSession | None = None,
    ) -> WorkingMemoryState:
        """Evict an item from working memory."""
        return await self.working_memory.evict(session_id, ref_id, session)

    async def get_working_memory(
        self, session_id: str, session: AsyncSession | None = None
    ) -> WorkingMemoryState | None:
        """Get current working memory state."""
        return await self.working_memory.get(session_id, session)

    async def clear_working_memory(self, session_id: str, session: AsyncSession | None = None) -> None:
        """Clear working memory for session."""
        await self.working_memory.clear(session_id, session)

    async def add_thread(
        self, session_id: str, thread: OpenThread, session: AsyncSession | None = None
    ) -> WorkingMemoryState:
        """Add an open thread to working memory."""
        return await self.working_memory.add_thread(session_id, thread, session)

    async def resolve_thread(
        self, session_id: str, description: str, session: AsyncSession | None = None
    ) -> WorkingMemoryState:
        """Resolve (remove) an open thread by description match."""
        return await self.working_memory.resolve_thread(session_id, description, session)

    # ==================================================================
    # Conversation State
    # ==================================================================

    async def save_conversation_state(
        self,
        agent_id: str,
        session_id: str,
        summary: str | None,
        messages: list[dict] | None,
        turn_count: int,
        compaction_count: int,
        session: AsyncSession | None = None,
    ) -> None:
        """Upsert conversation state for a session."""
        if session is None:
            async with self.db.session() as session:
                await self._save_conversation_state(
                    agent_id, session_id, summary, messages, turn_count, compaction_count, session
                )
                await session.commit()
                return
        await self._save_conversation_state(
            agent_id, session_id, summary, messages, turn_count, compaction_count, session
        )

    async def _save_conversation_state(
        self,
        agent_id: str,
        session_id: str,
        summary: str | None,
        messages: list[dict] | None,
        turn_count: int,
        compaction_count: int,
        session: AsyncSession,
    ) -> None:
        stmt = (
            pg_insert(ConversationState)
            .values(
                agent_id=agent_id,
                session_id=session_id,
                summary=summary,
                messages=messages,
                turn_count=turn_count,
                compaction_count=compaction_count,
            )
            .on_conflict_do_update(
                index_elements=["agent_id", "session_id"],
                set_={
                    "summary": summary,
                    "messages": messages,
                    "turn_count": turn_count,
                    "compaction_count": compaction_count,
                },
            )
        )
        await session.execute(stmt)
        await session.flush()

    async def load_conversation_state(
        self,
        agent_id: str,
        session_id: str,
        session: AsyncSession | None = None,
    ) -> dict | None:
        """Load conversation state for a session. Returns dict or None."""
        if session is None:
            async with self.db.session() as session:
                return await self._load_conversation_state(agent_id, session_id, session)
        return await self._load_conversation_state(agent_id, session_id, session)

    async def _load_conversation_state(
        self,
        agent_id: str,
        session_id: str,
        session: AsyncSession,
    ) -> dict | None:
        result = await session.execute(
            select(ConversationState)
            .where(ConversationState.agent_id == agent_id)
            .where(ConversationState.session_id == session_id)
        )
        row = result.scalars().first()
        if row is None:
            return None
        return {
            "id": str(row.id),
            "agent_id": row.agent_id,
            "session_id": row.session_id,
            "summary": row.summary,
            "messages": row.messages,
            "turn_count": row.turn_count,
            "compaction_count": row.compaction_count,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        }

    async def delete_conversation_state(
        self,
        agent_id: str,
        session_id: str,
        session: AsyncSession | None = None,
    ) -> None:
        """Hard delete conversation state for a session."""
        if session is None:
            async with self.db.session() as session:
                await self._delete_conversation_state(agent_id, session_id, session)
                await session.commit()
                return
        await self._delete_conversation_state(agent_id, session_id, session)

    async def _delete_conversation_state(
        self,
        agent_id: str,
        session_id: str,
        session: AsyncSession,
    ) -> None:
        result = await session.execute(
            select(ConversationState)
            .where(ConversationState.agent_id == agent_id)
            .where(ConversationState.session_id == session_id)
        )
        row = result.scalars().first()
        if row is not None:
            await session.delete(row)
            await session.flush()

    # ==================================================================
    # Unified Recall (P2-3: Reciprocal Rank Fusion)
    # ==================================================================

    async def recall(
        self,
        query: str,
        limit: int = 10,
        types: list[str] | None = None,
        session: AsyncSession | None = None,
    ) -> list[RecallResult]:
        """Search across ALL memory types, return ranked results.

        Results carry their original hybrid search scores (configurable
        vector/keyword weighting via hybrid_search(); default 0.7/0.3,
        overridable at runtime). Censors use cosine similarity. Since
        most sub-searches use the same scoring formula, scores are
        directly comparable.
        """
        if session is None:
            async with self.db.session() as session:
                return await self._recall(query, limit, types, session)
        return await self._recall(query, limit, types, session)

    async def _recall(
        self,
        query: str,
        limit: int,
        types: list[str] | None,
        session: AsyncSession,
    ) -> list[RecallResult]:
        search_types = types or ["episode", "fact", "procedure", "censor"]
        fetch_limit = limit * 2  # Fetch more for merging

        # Execute searches sequentially — AsyncSession is not safe for
        # concurrent use, so asyncio.gather would risk InvalidRequestError.
        search_map: dict[str, object] = {}
        if "episode" in search_types:
            search_map["episode"] = ("episodes", {"limit": fetch_limit})
        if "fact" in search_types:
            search_map["fact"] = ("facts", {"limit": fetch_limit})
        if "procedure" in search_types:
            search_map["procedure"] = ("procedures", {"limit": fetch_limit})
        if "censor" in search_types:
            search_map["censor"] = ("censors", {"limit": fetch_limit})

        if not search_map:
            return []

        keys: list[str] = []
        results_list: list[object] = []
        for memory_type, (_mgr_name, _kw) in search_map.items():
            try:
                if memory_type == "episode":
                    result = await self.episodes.search(query, fetch_limit, session)
                elif memory_type == "fact":
                    result = await self.facts.search(query, fetch_limit, session=session)
                elif memory_type == "procedure":
                    result = await self.procedures.search(query, fetch_limit, session=session)
                else:
                    # P1-5: Use read-only search, not check
                    result = await self.censors.search(query, fetch_limit, session=session)
                keys.append(memory_type)
                results_list.append(result)
            except Exception as exc:
                keys.append(memory_type)
                results_list.append(exc)

        # Use original search scores instead of RRF positional scores.
        # Episodes, facts, and procedures use hybrid_search() (configurable
        # vector/keyword weight; default 0.7/0.3). Censors use cosine
        # similarity. Scores are comparable enough for cross-type ranking.
        merged: list[RecallResult] = []

        for memory_type, raw_results in zip(keys, results_list):
            if isinstance(raw_results, Exception):
                logger.warning(
                    "Recall sub-search failed for %s: %s",
                    memory_type,
                    raw_results,
                )
                continue

            for item in raw_results:
                raw = getattr(item, "score", None)
                original_score = raw if raw is not None else 0.0
                recall_result = self._to_recall_result(memory_type, item, original_score)
                if recall_result is not None:
                    merged.append(recall_result)

        # F030: MMR diversity re-ranking
        if (
            self.settings.mmr_enabled
            and len(merged) > 1
            and self._embeddings is not None
        ):
            try:
                # Group IDs by type for batch fetch
                type_ids: dict[str, list[UUID]] = {}
                for r in merged:
                    type_ids.setdefault(r.type, []).append(r.id)

                # Batch-fetch embeddings for candidates
                embeddings = await batch_fetch_embeddings(
                    session, type_ids, self.agent_id
                )
                logger.info(
                    "MMR: fetched %d/%d embeddings for reranking (λ=%.2f)",
                    len(embeddings), len(merged), self.settings.mmr_diversity_weight,
                )

                # Generate query embedding for MMR relevance term
                query_embedding = await self._embeddings.embed(query)

                pre_mmr_order = [r.id for r in merged[:limit]]
                merged = mmr_rerank(
                    candidates=merged,
                    embeddings=embeddings,
                    query_embedding=query_embedding,
                    lambda_=self.settings.mmr_diversity_weight,
                    limit=limit,
                )
                post_mmr_order = [r.id for r in merged]
                reordered = pre_mmr_order != post_mmr_order
                types_in_result = set(r.type for r in merged)
                logger.info(
                    "MMR: selected %d results across %d types, reordered=%s",
                    len(merged), len(types_in_result), reordered,
                )
            except Exception as exc:
                logger.warning("MMR reranking failed, falling back to score sort: %s", exc)
                merged.sort(key=lambda r: r.score, reverse=True)
                merged = merged[:limit]
        else:
            # Sort by original hybrid score DESC
            merged.sort(key=lambda r: r.score, reverse=True)
            merged = merged[:limit]

        return merged

    def _to_recall_result(self, memory_type: str, item: object, score: float) -> RecallResult | None:
        """Convert a typed search result to a RecallResult."""
        if isinstance(item, EpisodeSummary):
            return RecallResult(
                type="episode",
                id=item.id,
                summary=item.summary,
                score=score,
                metadata={
                    "title": item.title,
                    "outcome": item.outcome,
                    "started_at": item.started_at.isoformat() if item.started_at else None,
                },
            )
        elif isinstance(item, FactSummary):
            return RecallResult(
                type="fact",
                id=item.id,
                summary=item.content,
                score=score,
                metadata={
                    "category": item.category,
                    "subject": item.subject,
                    "confidence": item.confidence,
                },
            )
        elif isinstance(item, ProcedureSummary):
            summary = f"{item.name}: {item.description}" if item.description else item.name
            return RecallResult(
                type="procedure",
                id=item.id,
                summary=summary,
                score=score,
                metadata={
                    "domain": item.domain,
                    "effectiveness": item.effectiveness,
                    "activation_count": item.activation_count,
                },
            )
        elif isinstance(item, CensorMatch):
            return RecallResult(
                type="censor",
                id=item.id,
                summary=f"{item.trigger_pattern}: {item.reason}",
                score=score,
                metadata={
                    "action": item.action,
                    "domain": item.domain,
                },
            )
        return None
