"""Procedure management — K-lines with level-bands (how to do things).

Manages procedural memory: storing, activating, recording outcomes.
All methods follow Brain's session injection pattern (P1-1).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from nous.brain.embeddings import EmbeddingProvider
from nous.heart.schemas import (
    EvolutionCandidate,
    ProcedureDetail,
    ProcedureInput,
    ProcedureOutcome,
    ProcedureSummary,
)
from nous.heart.search import hybrid_search, hybrid_search_multi
from nous.storage.database import Database
from nous.storage.models import Event, Procedure, ProcedureTaskAffinity

logger = logging.getLogger(__name__)


def _build_embed_text(
    name: str,
    description: str | None,
    core_patterns: list[str] | None,
    goals: list[str] | None,
    core_tools: list[str] | None,
    core_concepts: list[str] | None,
    implementation_notes: list[str] | None,
) -> str:
    """Build the text used for procedure embedding (issue #197).

    Includes all body fields, not just metadata, for full-body search accuracy.
    """
    text = (
        f"{name} {description or ''} "
        f"{' '.join(core_patterns or [])} "
        f"{' '.join(goals or [])} "
        f"{' '.join(core_tools or [])} "
        f"{' '.join(core_concepts or [])} "
        f"{' '.join(implementation_notes or [])}"
    ).strip()
    # Bound the EMBEDDING input only — OpenAI text-embedding-3 caps at 8191 tokens;
    # full skill bodies (F081) can exceed that and would 400 → NULL embedding →
    # invisible to vector search. The STORED body stays full; metadata
    # (name/description) leads so the body never crowds out the routing signal.
    # Conservative CHAR bound (tiktoken is not a dependency): realistic skill
    # markdown (English prose + code) runs >=2 chars/token, so 16000 chars stays
    # under 8191 tokens. _embed_with_retry NULLs+logs on a pathological (e.g.
    # CJK-dense) overflow as the backstop — but skill bodies are English markdown.
    _EMBED_CHAR_CAP = 16000
    return text[:_EMBED_CHAR_CAP] if len(text) > _EMBED_CHAR_CAP else text


class ProcedureManager:
    """Manages procedural memory — how to do things (K-lines with level-bands)."""

    def __init__(
        self,
        db: Database,
        embeddings: EmbeddingProvider | None,
        agent_id: str,
        *,
        utility_boost: bool = True,
        utility_alpha: float = 0.15,
        affinity_beta: float = 0.10,
        min_activations_for_boost: int = 5,
    ) -> None:
        self.db = db
        self.embeddings = embeddings
        self.agent_id = agent_id
        self._utility_boost = utility_boost
        self._utility_alpha = utility_alpha
        self._affinity_beta = affinity_beta
        self._min_activations_for_boost = min_activations_for_boost

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

    # ------------------------------------------------------------------
    # store()
    # ------------------------------------------------------------------

    async def store(self, input: ProcedureInput, session: AsyncSession | None = None) -> ProcedureDetail:
        """Store a new procedure."""
        if session is None:
            async with self.db.session() as session:
                result = await self._store(input, session)
                await session.commit()
                return result
        return await self._store(input, session)

    async def _embed_with_retry(self, embed_text: str, *, attempts: int = 2) -> list[float] | None:
        """Embed with one retry; on final failure log ERROR (not a quiet warning).

        A NULL embedding makes the row invisible to vector search and the dedup
        cosine gate (audit bug 8), so the failure must be loud, not swallowed.
        """
        if not self.embeddings:
            return None
        for attempt in range(1, attempts + 1):
            try:
                return await self.embeddings.embed(embed_text)
            except Exception:
                if attempt >= attempts:
                    logger.error(
                        "Embedding generation failed after %d attempts; storing procedure with "
                        "NULL embedding (invisible to vector search/dedup until reembed_all)",
                        attempts,
                    )
                    return None
                logger.warning("Embedding attempt %d/%d failed for procedure, retrying", attempt, attempts)
        return None

    async def _store(self, input: ProcedureInput, session: AsyncSession) -> ProcedureDetail:
        # Generate embedding from all body fields (issue #197)
        embedding = None
        if self.embeddings:
            embed_text = _build_embed_text(
                input.name, input.description, input.core_patterns,
                input.goals, input.core_tools, input.core_concepts,
                input.implementation_notes,
            )
            embedding = await self._embed_with_retry(embed_text)

        procedure = Procedure(
            agent_id=self.agent_id,
            name=input.name,
            domain=input.domain,
            description=input.description,
            goals=input.goals or None,
            core_patterns=input.core_patterns or None,
            core_tools=input.core_tools or None,
            core_concepts=input.core_concepts or None,
            implementation_notes=input.implementation_notes or None,
            tags=input.tags or None,
            embedding=embedding,
            active=input.active if input.active is not None else True,
            # F064.4: always-persist semantic — runtime_metadata is None
            # when the manifest didn't declare any of the new fields, but
            # we don't gate on a flag at write time (silent-drop fix).
            runtime_metadata=input.runtime_metadata,
        )
        session.add(procedure)
        await session.flush()

        # F040: Emit procedure_stored event for graph linking
        await self._emit_event(session, "procedure_stored", {
            "procedure_id": str(procedure.id),
            "name": input.name,
            "domain": input.domain or "",
            "description": input.description or "",
            "tags": input.tags or [],
        })

        return self._to_detail(procedure)

    # ------------------------------------------------------------------
    # update_body() — in-place skill refresh (audit bug 9)
    # ------------------------------------------------------------------

    async def update_body(
        self, procedure_id: UUID, input: ProcedureInput, session: AsyncSession | None = None
    ) -> ProcedureDetail:
        """Refresh a procedure's body fields in place, preserving learned stats.

        Used by learn_skill on a name collision instead of retire+store, which
        reset activation/success/failure counts and orphaned task affinity
        (audit bug 9). Counts, created_at, and supersession state are untouched.
        """
        if session is None:
            async with self.db.session() as session:
                result = await self._update_body(procedure_id, input, session)
                await session.commit()
                return result
        return await self._update_body(procedure_id, input, session)

    async def _update_body(
        self, procedure_id: UUID, input: ProcedureInput, session: AsyncSession
    ) -> ProcedureDetail:
        procedure = await self._get_procedure_orm(procedure_id, session)
        if procedure is None:
            raise ValueError(f"Procedure {procedure_id} not found")

        embedding = procedure.embedding
        if self.embeddings:
            embed_text = _build_embed_text(
                input.name, input.description, input.core_patterns,
                input.goals, input.core_tools, input.core_concepts,
                input.implementation_notes,
            )
            # Re-embed from the new body. On failure _embed_with_retry returns None and
            # logs ERROR; store NULL (consistent with _store) rather than keep the now-stale
            # vector for the old body — otherwise the row is served/deduped under an obsolete
            # embedding and reembed_all can't tell it needs repair.
            embedding = await self._embed_with_retry(embed_text)

        procedure.name = input.name
        procedure.domain = input.domain
        procedure.description = input.description
        procedure.goals = input.goals or None
        procedure.core_patterns = input.core_patterns or None
        procedure.core_tools = input.core_tools or None
        procedure.core_concepts = input.core_concepts or None
        procedure.implementation_notes = input.implementation_notes or None
        procedure.tags = input.tags or None
        procedure.runtime_metadata = input.runtime_metadata
        if input.active is not None:
            procedure.active = input.active
        procedure.embedding = embedding
        procedure.updated_at = datetime.now(UTC)
        await session.flush()
        return self._to_detail(procedure)

    # ------------------------------------------------------------------
    # activate()
    # ------------------------------------------------------------------

    async def activate(self, procedure_id: UUID, session: AsyncSession | None = None) -> ProcedureDetail:
        """Mark a procedure as activated."""
        if session is None:
            async with self.db.session() as session:
                result = await self._activate(procedure_id, session)
                await session.commit()
                return result
        return await self._activate(procedure_id, session)

    async def _activate(self, procedure_id: UUID, session: AsyncSession) -> ProcedureDetail:
        procedure = await self._get_procedure_orm(procedure_id, session)
        if procedure is None:
            raise ValueError(f"Procedure {procedure_id} not found")

        # P2-9: NULL-safe counter
        procedure.activation_count = (procedure.activation_count or 0) + 1
        procedure.last_activated = datetime.now(UTC)
        await session.flush()

        await self._emit_event(
            session,
            "procedure_activated",
            {"procedure_id": str(procedure_id)},
        )

        return self._to_detail(procedure)

    # ------------------------------------------------------------------
    # record_outcome()
    # ------------------------------------------------------------------

    _VALID_OUTCOMES: set[str] = {"success", "failure", "neutral"}

    async def record_outcome(
        self,
        procedure_id: UUID,
        outcome: ProcedureOutcome,
        frame_type: str | None = None,
        session: AsyncSession | None = None,
    ) -> ProcedureDetail:
        """Record procedure activation outcome.

        Args:
            procedure_id: The procedure to record outcome for.
            outcome: 'success', 'failure', or 'neutral'.
            frame_type: Optional cognitive frame type (e.g. 'task', 'conversation').
                        When provided, also upserts the procedure_task_affinity row (F037).
            session: Optional existing session.
        """
        if session is None:
            async with self.db.session() as session:
                result = await self._record_outcome(procedure_id, outcome, frame_type, session)
                await session.commit()
                return result
        return await self._record_outcome(procedure_id, outcome, frame_type, session)

    async def _record_outcome(
        self,
        procedure_id: UUID,
        outcome: ProcedureOutcome,
        frame_type: str | None,
        session: AsyncSession,
    ) -> ProcedureDetail:
        if outcome not in self._VALID_OUTCOMES:
            raise ValueError(f"Invalid outcome {outcome!r}; must be one of {sorted(self._VALID_OUTCOMES)}")

        procedure = await self._get_procedure_orm(procedure_id, session)
        if procedure is None:
            raise ValueError(f"Procedure {procedure_id} not found")

        # P2-9: NULL-safe counter increments
        if outcome == "success":
            procedure.success_count = (procedure.success_count or 0) + 1
        elif outcome == "failure":
            procedure.failure_count = (procedure.failure_count or 0) + 1
        elif outcome == "neutral":
            procedure.neutral_count = (procedure.neutral_count or 0) + 1

        await session.flush()

        # F037: Upsert task affinity row when frame_type is known
        if frame_type:
            await self._upsert_task_affinity(procedure_id, frame_type, outcome, session)

        await self._emit_event(
            session,
            "procedure_outcome",
            {"procedure_id": str(procedure_id), "outcome": outcome},
        )

        return self._to_detail(procedure)

    # ------------------------------------------------------------------
    # get()
    # ------------------------------------------------------------------

    async def get(self, procedure_id: UUID, session: AsyncSession | None = None) -> ProcedureDetail | None:
        """Fetch procedure with computed effectiveness."""
        if session is None:
            async with self.db.session() as session:
                return await self._get(procedure_id, session)
        return await self._get(procedure_id, session)

    async def _get(self, procedure_id: UUID, session: AsyncSession) -> ProcedureDetail | None:
        procedure = await self._get_procedure_orm(procedure_id, session)
        if procedure is None:
            return None
        return self._to_detail(procedure)

    # ------------------------------------------------------------------
    # search()
    # ------------------------------------------------------------------

    async def search(
        self,
        query: str,
        limit: int = 10,
        domain: str | None = None,
        frame_type: str | None = None,
        session: AsyncSession | None = None,
        variant_pairs: list[tuple[str, list[float] | None]] | None = None,
    ) -> list[ProcedureSummary]:
        """Hybrid search over procedures. Optional domain filter.

        When utility boost is enabled (F037), applies an effectiveness-weighted
        score boost: final_score = hybrid_score * (1 + α*utility + β*affinity).

        Args:
            variant_pairs: F050 — when set with len > 1, routes through
                hybrid_search_multi for RRF fusion across query variants.
                F037 utility-boost still applies downstream of routing.
                Defaults to None (single-query path; backwards compatible).
        """
        if session is None:
            async with self.db.session() as session:
                return await self._search(query, limit, domain, frame_type, session, variant_pairs)
        return await self._search(query, limit, domain, frame_type, session, variant_pairs)

    async def _search(
        self,
        query: str,
        limit: int,
        domain: str | None,
        frame_type: str | None,
        session: AsyncSession,
        variant_pairs: list[tuple[str, list[float] | None]] | None = None,
    ) -> list[ProcedureSummary]:
        embedding = None
        if self.embeddings:
            try:
                embedding = await self.embeddings.embed(query)
            except Exception:
                logger.warning("Embedding generation failed for procedure search")

        extra_where = " AND t.active = true"
        extra_params: dict = {}
        if domain:
            extra_where += " AND t.domain = :domain"
            extra_params["domain"] = domain

        if variant_pairs and len(variant_pairs) > 1:
            results = await hybrid_search_multi(
                session=session,
                table="heart.procedures",
                queries=variant_pairs,
                agent_id=self.agent_id,
                extra_where=extra_where,
                extra_params=extra_params,
                limit=limit,
            )
        else:
            results = await hybrid_search(
                session=session,
                table="heart.procedures",
                embedding=embedding,
                query_text=query,
                agent_id=self.agent_id,
                extra_where=extra_where,
                extra_params=extra_params,
                limit=limit,
            )

        if not results:
            return []

        ids = [r[0] for r in results]
        scores = {r[0]: r[1] for r in results}

        proc_result = await session.execute(select(Procedure).where(Procedure.id.in_(ids)))
        procedures = {p.id: p for p in proc_result.scalars().all()}

        # F037: Load affinity data for current frame_type if provided
        affinity_map: dict[UUID, tuple[int, int]] = {}
        if frame_type and self._utility_boost:
            affinity_rows = await session.execute(
                select(ProcedureTaskAffinity)
                .where(ProcedureTaskAffinity.procedure_id.in_(ids))
                .where(ProcedureTaskAffinity.frame_type == frame_type)
                .where(ProcedureTaskAffinity.agent_id == self.agent_id)
                .where(ProcedureTaskAffinity.active == True)
            )
            for row in affinity_rows.scalars().all():
                affinity_map[row.procedure_id] = (row.success_count, row.failure_count)

        summaries = []
        for pid in ids:
            p = procedures.get(pid)
            if p is None:
                continue

            hybrid_score = scores.get(p.id, 0.0)
            effectiveness = self._compute_effectiveness(p)

            # F037: Apply utility boost when enabled and procedure has sufficient history
            final_score = hybrid_score
            if self._utility_boost and effectiveness is not None:
                activation_count = p.activation_count or 0
                if activation_count >= self._min_activations_for_boost:
                    utility_signal = effectiveness - 0.5
                    boost = self._utility_alpha * utility_signal

                    # Apply frame-type affinity boost if data is available
                    if pid in affinity_map:
                        aff_success, aff_failure = affinity_map[pid]
                        aff_total = aff_success + aff_failure
                        if aff_total >= self._min_activations_for_boost:
                            frame_eff = (aff_success + 1) / (aff_total + 2)
                            boost += self._affinity_beta * (frame_eff - 0.5)

                    final_score = hybrid_score * (1.0 + boost)

            summaries.append(
                ProcedureSummary(
                    id=p.id,
                    name=p.name,
                    domain=p.domain,
                    description=p.description,
                    activation_count=p.activation_count or 0,
                    effectiveness=effectiveness,
                    score=final_score,
                    # F079 P1: carry body fields for the pull path (recall_deep).
                    # `p` is the full ORM row already in scope — no extra query.
                    core_patterns=list(p.core_patterns or []),
                    implementation_notes=list(p.implementation_notes or []),
                )
            )

        # Re-sort by final_score descending (boost may have changed order)
        summaries.sort(key=lambda s: s.score or 0.0, reverse=True)
        return summaries

    # ------------------------------------------------------------------
    # find_similar_for_selection() — §14 cosine leg (codex P2, PR #495)
    # ------------------------------------------------------------------

    async def find_similar_for_selection(
        self,
        query: str,
        limit: int = 10,
        session: AsyncSession | None = None,
    ) -> list[ProcedureSummary]:
        """Raw-cosine nearest-neighbor probe for §14 procedure selection.

        Same rationale as FactManager.find_similar_for_dedup (audit S1):
        ``search()`` returns normalized RRF scores that encode RANK, not
        closeness — the nearest procedure scores ~0.95 for ANY query, so a
        relevance floor compared against it never filters. The §14 cosine
        leg needs a score a floor can be calibrated against; this returns
        ``score`` = raw cosine similarity. Active procedures only.

        Returns [] when embeddings are unavailable or the embed fails —
        the §14 ladder then simply leaves the remaining slots empty.
        """
        if session is None:
            async with self.db.session() as session:
                return await self._find_similar_for_selection(query, limit, session)
        return await self._find_similar_for_selection(query, limit, session)

    async def _find_similar_for_selection(
        self,
        query: str,
        limit: int,
        session: AsyncSession,
    ) -> list[ProcedureSummary]:
        if not self.embeddings:
            return []
        try:
            embedding = await self.embeddings.embed(query)
        except Exception:
            logger.warning("Embedding generation failed for procedure selection probe")
            return []

        embedding_str = "[" + ",".join(str(float(v)) for v in embedding) + "]"
        # Same filtered-ANN horizon widening as the fact probes (codex P2):
        # agent_id/active are post-applied to the approximate walk.
        from nous.heart.search import set_local_ef_search
        await set_local_ef_search(session, 100)
        rows = (await session.execute(text("""
            SELECT id, 1 - (embedding <=> CAST(:embedding AS vector)) AS similarity
            FROM heart.procedures
            WHERE agent_id = :agent_id
              AND active = true
              AND embedding IS NOT NULL
            ORDER BY embedding <=> CAST(:embedding AS vector)
            LIMIT :limit
        """), {
            "embedding": embedding_str,
            "agent_id": self.agent_id,
            "limit": limit,
        })).all()
        if not rows:
            return []

        ids = [r.id for r in rows]
        sims = {r.id: float(r.similarity) for r in rows}
        proc_result = await session.execute(select(Procedure).where(Procedure.id.in_(ids)))
        procedures = {p.id: p for p in proc_result.scalars().all()}

        return [
            ProcedureSummary(
                id=p.id,
                name=p.name,
                domain=p.domain,
                description=p.description,
                activation_count=p.activation_count or 0,
                effectiveness=self._compute_effectiveness(p),
                score=sims.get(p.id),
                core_patterns=list(p.core_patterns or []),
                implementation_notes=list(p.implementation_notes or []),
            )
            for pid in ids
            if (p := procedures.get(pid)) is not None
        ]

    # ------------------------------------------------------------------
    # get_evolution_candidates() — F037 Part 3
    # ------------------------------------------------------------------

    async def get_evolution_candidates(
        self, session: AsyncSession | None = None
    ) -> list[EvolutionCandidate]:
        """Return procedures that should be rewritten, retired, or investigated.

        Categories:
        - 'retire': effectiveness < 0.3 AND activation_count >= 10
        - 'rewrite': effectiveness < 0.5 AND activation_count >= 15
        - 'investigate': activation_count >= 30 but effectiveness < 0.6
        - 'star': effectiveness >= 0.85 AND activation_count >= 10 (candidates for templates)

        A procedure can match multiple categories; the highest-priority one wins
        (retire > rewrite > investigate > star).
        """
        if session is None:
            async with self.db.session() as session:
                return await self._get_evolution_candidates(session)
        return await self._get_evolution_candidates(session)

    async def _get_evolution_candidates(self, session: AsyncSession) -> list[EvolutionCandidate]:
        result = await session.execute(
            select(Procedure)
            .where(Procedure.agent_id == self.agent_id)
            .where(Procedure.active == True)  # noqa: E712
        )
        procedures = list(result.scalars().all())

        candidates: list[EvolutionCandidate] = []
        for p in procedures:
            eff = self._compute_effectiveness(p)
            if eff is None:
                continue  # No outcome data yet
            activation_count = p.activation_count or 0

            if eff < 0.3 and activation_count >= 10:
                candidates.append(EvolutionCandidate(
                    id=p.id,
                    name=p.name,
                    category="retire",
                    effectiveness=eff,
                    activation_count=activation_count,
                    reason=f"Effectiveness {eff:.2f} below retirement threshold (0.3) with {activation_count} activations",
                ))
            elif eff < 0.5 and activation_count >= 15:
                candidates.append(EvolutionCandidate(
                    id=p.id,
                    name=p.name,
                    category="rewrite",
                    effectiveness=eff,
                    activation_count=activation_count,
                    reason=f"Effectiveness {eff:.2f} below rewrite threshold (0.5) with {activation_count} activations",
                ))
            elif activation_count >= 30 and eff < 0.6:
                candidates.append(EvolutionCandidate(
                    id=p.id,
                    name=p.name,
                    category="investigate",
                    effectiveness=eff,
                    activation_count=activation_count,
                    reason=f"High activation count ({activation_count}) but effectiveness {eff:.2f} below 0.6 — may be declining",
                ))
            elif eff >= 0.85 and activation_count >= 10:
                candidates.append(EvolutionCandidate(
                    id=p.id,
                    name=p.name,
                    category="star",
                    effectiveness=eff,
                    activation_count=activation_count,
                    reason=f"Effectiveness {eff:.2f} with {activation_count} activations — candidate for template",
                ))

        return candidates

    # ------------------------------------------------------------------
    # get_effectiveness() — F037 diagnostic helper
    # ------------------------------------------------------------------

    async def get_effectiveness(
        self, procedure_id: UUID, session: AsyncSession | None = None
    ) -> float | None:
        """Return Laplace-smoothed effectiveness for a procedure, or None if no outcome data."""
        if session is None:
            async with self.db.session() as session:
                return await self._get_effectiveness(procedure_id, session)
        return await self._get_effectiveness(procedure_id, session)

    async def _get_effectiveness(self, procedure_id: UUID, session: AsyncSession) -> float | None:
        procedure = await self._get_procedure_orm(procedure_id, session)
        if procedure is None:
            return None
        return self._compute_effectiveness(procedure)

    # ------------------------------------------------------------------
    # list_all() — F021 dashboard browse mode
    # ------------------------------------------------------------------

    async def list_all(
        self,
        limit: int = 50,
        offset: int = 0,
        domain: str | None = None,
        active_only: bool = True,
        min_activations: int | None = None,
        session: AsyncSession | None = None,
    ) -> tuple[list[ProcedureSummary], int]:
        """Paginated procedure list with filters (F021)."""
        if session is None:
            async with self.db.session() as session:
                return await self._list_all(limit, offset, domain, active_only, min_activations, session)
        return await self._list_all(limit, offset, domain, active_only, min_activations, session)

    async def _list_all(self, limit, offset, domain, active_only, min_activations, session):
        from sqlalchemy import func as sa_func
        conditions = [Procedure.agent_id == self.agent_id]
        if active_only:
            conditions.append(Procedure.active == True)  # noqa: E712
        if domain:
            conditions.append(Procedure.domain == domain)
        if min_activations is not None:
            conditions.append(Procedure.activation_count >= min_activations)

        count_q = select(sa_func.count()).select_from(Procedure).where(*conditions)
        total = (await session.execute(count_q)).scalar() or 0

        # Secondary sort by id: created_at can tie (NOW() is txn-start time, so
        # same-transaction batch inserts share it) — without a tiebreaker the row order
        # is non-deterministic, which would bust the static-tier Procedure Catalog cache
        # turn-to-turn (F079 §8.2).
        q = (select(Procedure).where(*conditions)
             .order_by(Procedure.created_at.desc(), Procedure.id).limit(limit).offset(offset))
        result = await session.execute(q)
        procs = list(result.scalars().all())

        # ProcedureSummary fields: id, name, domain, description, activation_count, effectiveness, score
        # NOTE: No success_count/failure_count fields. Compute effectiveness from counts.
        summaries = [
            ProcedureSummary(
                id=p.id, name=p.name, domain=p.domain,
                description=p.description,
                activation_count=p.activation_count or 0,
                effectiveness=self._compute_effectiveness(p),
            )
            for p in procs
        ]
        return summaries, total

    # ------------------------------------------------------------------
    # get_low_effectiveness() — F034: Heartbeat health check
    # ------------------------------------------------------------------

    async def get_low_effectiveness(
        self, threshold: float = 0.5, session: AsyncSession | None = None,
    ) -> list[ProcedureSummary]:
        """Return active procedures with effectiveness below threshold.

        Only includes procedures that have been activated at least once
        (i.e. have success + failure > 0).
        """
        if session is None:
            async with self.db.session() as session:
                return await self._get_low_effectiveness(threshold, session)
        return await self._get_low_effectiveness(threshold, session)

    async def _get_low_effectiveness(
        self, threshold: float, session: AsyncSession,
    ) -> list[ProcedureSummary]:
        result = await session.execute(
            select(Procedure)
            .where(Procedure.agent_id == self.agent_id)
            .where(Procedure.active == True)  # noqa: E712
        )
        procedures = list(result.scalars().all())

        low: list[ProcedureSummary] = []
        for p in procedures:
            eff = self._compute_effectiveness(p)
            if eff is not None and eff < threshold:
                low.append(ProcedureSummary(
                    id=p.id,
                    name=p.name,
                    domain=p.domain,
                    description=p.description,
                    activation_count=p.activation_count or 0,
                    effectiveness=eff,
                ))
        return low

    # ------------------------------------------------------------------
    # retire()
    # ------------------------------------------------------------------

    async def retire(self, procedure_id: UUID, session: AsyncSession | None = None) -> None:
        """Retire a procedure (set active=false)."""
        if session is None:
            async with self.db.session() as session:
                await self._retire(procedure_id, session)
                await session.commit()
                return
        await self._retire(procedure_id, session)

    async def _retire(self, procedure_id: UUID, session: AsyncSession) -> None:
        procedure = await self._get_procedure_orm(procedure_id, session)
        if procedure is None:
            raise ValueError(f"Procedure {procedure_id} not found")
        procedure.active = False
        await session.flush()

    # ------------------------------------------------------------------
    # reactivate()
    # ------------------------------------------------------------------

    async def reactivate(self, procedure_id: UUID, session: AsyncSession | None = None) -> None:
        """Set an inactive procedure back to active."""
        if session is None:
            async with self.db.session() as session:
                await self._reactivate(procedure_id, session)
                await session.commit()
                return
        await self._reactivate(procedure_id, session)

    async def _reactivate(self, procedure_id: UUID, session: AsyncSession) -> None:
        procedure = await self._get_procedure_orm(procedure_id, session)
        if procedure is None:
            raise ValueError(f"Procedure {procedure_id} not found")
        if procedure.active:
            return
        # B6: an active row with the same lower(name) already exists -> flipping this
        # one active would violate the (agent_id, lower(name)) WHERE active unique index
        # (migration 058) and, uncaught, abort the whole reactivate_skills loop. Skip
        # with a warning instead (the live row already provides the capability).
        clash = await session.execute(
            select(Procedure.id)
            .where(func.lower(Procedure.name) == (procedure.name or "").lower())
            .where(Procedure.agent_id == self.agent_id)
            .where(Procedure.active == True)  # noqa: E712
            .where(Procedure.id != procedure_id)
            .limit(1)
        )
        if clash.scalars().first() is not None:
            logger.warning(
                "Skipping reactivation of %s — an active procedure with the same name exists",
                procedure.name,
            )
            return
        procedure.active = True
        await session.flush()

    # ------------------------------------------------------------------
    # reembed_all() — issue #197 backfill
    # ------------------------------------------------------------------

    async def reembed_all(self, session: AsyncSession | None = None) -> int:
        """Recompute embeddings for all active procedures using expanded embed_text.

        Use after changing the embedding formula to backfill existing records.
        Returns the number of procedures re-embedded.
        """
        if session is None:
            async with self.db.session() as session:
                count = await self._reembed_all(session)
                await session.commit()
                return count
        return await self._reembed_all(session)

    async def _reembed_all(self, session: AsyncSession) -> int:
        if not self.embeddings:
            return 0

        result = await session.execute(
            select(Procedure)
            .where(Procedure.agent_id == self.agent_id)
            .where(Procedure.active == True)  # noqa: E712
        )
        procedures = list(result.scalars().all())

        count = 0
        for proc in procedures:
            embed_text = _build_embed_text(
                proc.name, proc.description, proc.core_patterns,
                proc.goals, proc.core_tools, proc.core_concepts,
                proc.implementation_notes,
            )
            try:
                proc.embedding = await self.embeddings.embed(embed_text)
                count += 1
            except Exception:
                logger.warning("Re-embed failed for procedure %s", proc.id)

        await session.flush()
        return count

    # ------------------------------------------------------------------
    # list_inactive_skills()
    # ------------------------------------------------------------------

    async def list_inactive_skills(self, session: AsyncSession | None = None) -> list[ProcedureDetail]:
        """List inactive procedures tagged as 'skill'."""
        if session is None:
            async with self.db.session() as session:
                return await self._list_inactive_skills(session)
        return await self._list_inactive_skills(session)

    async def _list_inactive_skills(self, session: AsyncSession) -> list[ProcedureDetail]:
        result = await session.execute(
            select(Procedure)
            .where(Procedure.agent_id == self.agent_id)
            .where(Procedure.active == False)  # noqa: E712
            .where(Procedure.tags.contains(["skill"]))
            # Dedup Phase 0: never resurrect a deliberately-consolidated duplicate.
            # A superseded row was archived because its capability now lives in the
            # canonical procedure; reactivate_skills must not un-archive it.
            .where(Procedure.superseded_by.is_(None))
            # F081: never resurrect a deliberately ARCHIVED skill either (e.g. a
            # source-gone stub archived by the re-import). archived_at is set on
            # any intentional archival; only env-inactive skills (archived_at NULL)
            # are eligible for requirements-based reactivation.
            .where(Procedure.archived_at.is_(None))
        )
        return [self._to_detail(p) for p in result.scalars().all()]

    async def is_name_superseded(self, name: str, session: AsyncSession | None = None) -> bool:
        """True if an archived (consolidated) procedure with this exact name exists.

        Dedup Phase 0: the bootstrap re-import path checks this before recreating a
        skill from disk. A superseded row means the capability was merged into a
        canonical procedure — recreating it would resurrect the duplicate.
        """
        if session is None:
            async with self.db.session() as session:
                return await self._is_name_superseded(name, session)
        return await self._is_name_superseded(name, session)

    async def _is_name_superseded(self, name: str, session: AsyncSession) -> bool:
        result = await session.execute(
            select(Procedure.id)
            .where(func.lower(Procedure.name) == name.lower())
            .where(Procedure.agent_id == self.agent_id)
            .where(Procedure.superseded_by.isnot(None))
            .limit(1)
        )
        return result.scalars().first() is not None

    async def get_by_name(self, name: str, session: AsyncSession | None = None) -> ProcedureDetail | None:
        """Fetch active procedure by exact name match."""
        if session is None:
            async with self.db.session() as session:
                return await self._get_by_name(name, session)
        return await self._get_by_name(name, session)

    async def _get_by_name(self, name: str, session: AsyncSession) -> ProcedureDetail | None:
        # Deterministic tiebreak: same `name` can have multiple active rows (name-dedup is
        # bypassable — see procedure-subsystem-audit). Without ORDER BY, LIMIT 1 returns an
        # arbitrary row. Order by created_at DESC, id — a STABLE, non-volatile key (NOT
        # activation_count, which changes per use and would make the resolved row drift, and
        # whose NULLs sort first under DESC). This is the SAME ordering the Procedure Catalog
        # uses to pick its displayed winner (list_all is created_at desc, id), so the catalog
        # entry and the loaded body always agree. Case-insensitive match (B6) — pairs with the
        # (agent_id, lower(name)) WHERE active unique index so a casing variant still resolves.
        result = await session.execute(
            select(Procedure)
            .where(func.lower(Procedure.name) == name.lower())
            .where(Procedure.agent_id == self.agent_id)
            .where(Procedure.active == True)  # noqa: E712
            .order_by(Procedure.created_at.desc(), Procedure.id)
            .limit(1)
        )
        procedure = result.scalars().first()
        if procedure is None:
            return None
        return self._to_detail(procedure)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _get_procedure_orm(self, procedure_id: UUID, session: AsyncSession) -> Procedure | None:
        """Fetch Procedure ORM scoped by agent_id."""
        result = await session.execute(
            select(Procedure).where(Procedure.id == procedure_id).where(Procedure.agent_id == self.agent_id)
        )
        return result.scalars().first()

    def _compute_effectiveness(self, procedure: Procedure) -> float | None:
        """P3-4: Laplace smoothing for effectiveness.

        effectiveness = (success + 1) / (success + failure + 2)
        """
        success = procedure.success_count or 0
        failure = procedure.failure_count or 0
        total = success + failure
        if total == 0:
            return None
        return (success + 1) / (success + failure + 2)

    async def _upsert_task_affinity(
        self,
        procedure_id: UUID,
        frame_type: str,
        outcome: str,
        session: AsyncSession,
    ) -> None:
        """Upsert a procedure_task_affinity row for the given frame_type (F037)."""
        now = datetime.now(UTC)
        success_inc = 1 if outcome == "success" else 0
        failure_inc = 1 if outcome == "failure" else 0

        # Try to find existing row
        result = await session.execute(
            select(ProcedureTaskAffinity)
            .where(ProcedureTaskAffinity.procedure_id == procedure_id)
            .where(ProcedureTaskAffinity.frame_type == frame_type)
            .where(ProcedureTaskAffinity.agent_id == self.agent_id)
        )
        row = result.scalars().first()

        if row is not None:
            row.activation_count += 1
            row.success_count += success_inc
            row.failure_count += failure_inc
            row.last_activated_at = now
        else:
            row = ProcedureTaskAffinity(
                procedure_id=procedure_id,
                frame_type=frame_type,
                activation_count=1,
                success_count=success_inc,
                failure_count=failure_inc,
                last_activated_at=now,
                agent_id=self.agent_id,
            )
            session.add(row)

        await session.flush()

    def _to_detail(self, procedure: Procedure) -> ProcedureDetail:
        """Convert ORM Procedure to ProcedureDetail DTO."""
        return ProcedureDetail(
            id=procedure.id,
            agent_id=procedure.agent_id,
            name=procedure.name,
            domain=procedure.domain,
            description=procedure.description,
            goals=procedure.goals or [],
            core_patterns=procedure.core_patterns or [],
            core_tools=procedure.core_tools or [],
            core_concepts=procedure.core_concepts or [],
            implementation_notes=procedure.implementation_notes or [],
            activation_count=procedure.activation_count or 0,
            success_count=procedure.success_count or 0,
            failure_count=procedure.failure_count or 0,
            neutral_count=procedure.neutral_count or 0,
            last_activated=procedure.last_activated,
            effectiveness=self._compute_effectiveness(procedure),
            related_procedures=procedure.related_procedures or [],
            censor_ids=procedure.censor_ids or [],
            tags=procedure.tags or [],
            active=procedure.active if procedure.active is not None else True,
            created_at=procedure.created_at,
        )
