"""Procedure management — K-lines with level-bands (how to do things).

Manages procedural memory: storing, activating, recording outcomes.
All methods follow Brain's session injection pattern (P1-1).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from nous.brain.embeddings import EmbeddingProvider
from nous.heart.schemas import ProcedureDetail, ProcedureInput, ProcedureOutcome, ProcedureSummary
from nous.heart.search import hybrid_search
from nous.storage.database import Database
from nous.storage.models import Event, Procedure

logger = logging.getLogger(__name__)


class ProcedureManager:
    """Manages procedural memory — how to do things (K-lines with level-bands)."""

    def __init__(
        self,
        db: Database,
        embeddings: EmbeddingProvider | None,
        agent_id: str,
    ) -> None:
        self.db = db
        self.embeddings = embeddings
        self.agent_id = agent_id

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

    async def _store(self, input: ProcedureInput, session: AsyncSession) -> ProcedureDetail:
        # Generate embedding from name + description + core_patterns
        embedding = None
        if self.embeddings:
            embed_text = (f"{input.name} {input.description or ''} {' '.join(input.core_patterns)}").strip()
            try:
                embedding = await self.embeddings.embed(embed_text)
            except Exception:
                logger.warning("Embedding generation failed for procedure store")

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
        )
        session.add(procedure)
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
        self, procedure_id: UUID, outcome: ProcedureOutcome, session: AsyncSession | None = None
    ) -> ProcedureDetail:
        """Record procedure activation outcome."""
        if session is None:
            async with self.db.session() as session:
                result = await self._record_outcome(procedure_id, outcome, session)
                await session.commit()
                return result
        return await self._record_outcome(procedure_id, outcome, session)

    async def _record_outcome(
        self, procedure_id: UUID, outcome: ProcedureOutcome, session: AsyncSession
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
        session: AsyncSession | None = None,
    ) -> list[ProcedureSummary]:
        """Hybrid search over procedures. Optional domain filter."""
        if session is None:
            async with self.db.session() as session:
                return await self._search(query, limit, domain, session)
        return await self._search(query, limit, domain, session)

    async def _search(
        self,
        query: str,
        limit: int,
        domain: str | None,
        session: AsyncSession,
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

        return [
            ProcedureSummary(
                id=p.id,
                name=p.name,
                domain=p.domain,
                description=p.description,
                activation_count=p.activation_count or 0,
                effectiveness=self._compute_effectiveness(p),
                score=scores.get(p.id),
            )
            for pid in ids
            if (p := procedures.get(pid)) is not None
        ]

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
        procedure.active = True

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
        )
        return [self._to_detail(p) for p in result.scalars().all()]

    async def get_by_name(self, name: str, session: AsyncSession | None = None) -> ProcedureDetail | None:
        """Fetch active procedure by exact name match."""
        if session is None:
            async with self.db.session() as session:
                return await self._get_by_name(name, session)
        return await self._get_by_name(name, session)

    async def _get_by_name(self, name: str, session: AsyncSession) -> ProcedureDetail | None:
        result = await session.execute(
            select(Procedure)
            .where(Procedure.name == name)
            .where(Procedure.agent_id == self.agent_id)
            .where(Procedure.active == True)  # noqa: E712
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
