"""F024 Phase 3b — RubricManager for self-modifying evaluation rubrics.

Manages rubric versions: CRUD, seeding v1.0.0, creating new versions
with weight adjustments, dimension splits/merges, and proposals.
All versions are immutable. Rollback = reactivate previous version.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from nous.cognitive.rubric_schemas import (
    RubricDimension,
    RubricVersionDetail,
    RubricVersionSummary,
)
from nous.storage.database import Database
from nous.storage.models import RubricVersion

logger = logging.getLogger(__name__)

# Default v1.0.0 dimensions (the fixed rubric from the self-improvement loop)
_DEFAULT_DIMENSIONS = [
    {
        "name": "Recall",
        "weight": 0.25,
        "description": "Accuracy and completeness of memory retrieval",
        "scoring_criteria": "1: No relevant memories retrieved. 5: Some relevant, some missed. 10: All relevant memories retrieved with high precision.",  # noqa: E501
        "min_weight": 0.10,
        "max_weight": 0.40,
    },
    {
        "name": "Tool Selection",
        "weight": 0.25,
        "description": "Choosing the right tool for the task and using it efficiently",
        "scoring_criteria": "1: Wrong tools or excessive calls. 5: Right tools, some inefficiency. 10: Optimal tool choice and call efficiency.",  # noqa: E501
        "min_weight": 0.10,
        "max_weight": 0.40,
    },
    {
        "name": "Confidence Calibration",
        "weight": 0.25,
        "description": "Accuracy of confidence estimates vs actual outcomes",
        "scoring_criteria": "1: Confidence wildly mismatched to outcomes. 5: Some calibration. 10: Confidence closely tracks actual success rates.",  # noqa: E501
        "min_weight": 0.10,
        "max_weight": 0.40,
    },
    {
        "name": "Proactivity",
        "weight": 0.25,
        "description": "Anticipating needs without being asked",
        "scoring_criteria": "1: Purely reactive. 5: Some anticipation. 10: Consistently anticipates and prepares for user needs.",  # noqa: E501
        "min_weight": 0.10,
        "max_weight": 0.40,
    },
]


class RubricManager:
    """Manages rubric versions for the self-improvement evaluation loop."""

    def __init__(self, db: Database, agent_id: str) -> None:
        self.db = db
        self.agent_id = agent_id

    async def get_active(self, session: AsyncSession | None = None) -> RubricVersion | None:
        """Get the currently active rubric version, or None."""

        async def _query(s: AsyncSession) -> RubricVersion | None:
            result = await s.execute(
                select(RubricVersion).where(
                    RubricVersion.agent_id == self.agent_id,
                    RubricVersion.status == "active",
                )
            )
            return result.scalar_one_or_none()

        if session:
            return await _query(session)
        async with self.db.session() as s:
            return await _query(s)

    async def get_history(self, limit: int = 20) -> list[RubricVersionSummary]:
        """Get rubric version history, newest first."""
        async with self.db.session() as session:
            result = await session.execute(
                select(RubricVersion)
                .where(RubricVersion.agent_id == self.agent_id)
                .order_by(RubricVersion.created_at.desc())
                .limit(limit)
            )
            rows = result.scalars().all()
            return [
                RubricVersionSummary(
                    id=r.id,
                    version=r.version,
                    status=r.status,
                    change_reason=r.change_reason,
                    dimension_count=len(r.dimensions) if isinstance(r.dimensions, list) else 0,
                    created_at=r.created_at,
                )
                for r in rows
            ]

    async def seed_v1(self, session: AsyncSession | None = None) -> RubricVersion:
        """Seed the initial v1.0.0 rubric if none exists."""

        async def _seed(s: AsyncSession) -> RubricVersion:
            rv = RubricVersion(
                agent_id=self.agent_id,
                version="1.0.0",
                parent_version=None,
                change_reason="Initial fixed rubric — 4 equal-weight dimensions",
                dimensions=_DEFAULT_DIMENSIONS,
                outcome_correlations={},
                status="active",
            )
            s.add(rv)
            await s.flush()
            return rv

        if session:
            return await _seed(session)
        async with self.db.session() as s:
            rv = await _seed(s)
            await s.commit()
            return rv

    async def create_version(
        self,
        new_version: str,
        dimensions: list[dict],
        change_reason: str,
        outcome_correlations: dict | None = None,
        session: AsyncSession | None = None,
    ) -> RubricVersion:
        """Create a new rubric version, superseding the current active one.

        Validates:
        - Dimension count in [3, 7]
        - Weights sum to ~1.0 (tolerance 0.01)
        - Each weight within its [min_weight, max_weight] bounds
        """
        if not (3 <= len(dimensions) <= 7):
            raise ValueError(f"Dimension count {len(dimensions)} outside [3, 7]")

        total_weight = sum(d["weight"] for d in dimensions)
        if abs(total_weight - 1.0) > 0.01:
            raise ValueError(f"Weights sum to {total_weight}, expected ~1.0")

        for d in dimensions:
            min_w = d.get("min_weight", 0.10)
            max_w = d.get("max_weight", 0.40)
            if d["weight"] < min_w or d["weight"] > max_w:
                raise ValueError(f"Dimension '{d['name']}' weight {d['weight']} outside [{min_w}, {max_w}]")

        async def _create(s: AsyncSession) -> RubricVersion:
            active = await self.get_active(session=s)
            if active:
                active.status = "superseded"

            rv = RubricVersion(
                agent_id=self.agent_id,
                version=new_version,
                parent_version=active.version if active else None,
                change_reason=change_reason,
                dimensions=dimensions,
                outcome_correlations=outcome_correlations or {},
                status="active",
            )
            s.add(rv)
            await s.flush()
            return rv

        if session:
            return await _create(session)
        async with self.db.session() as s:
            rv = await _create(s)
            await s.commit()
            return rv

    async def rollback(self, target_version: str, session: AsyncSession | None = None) -> RubricVersion | None:
        """Rollback to a previous version by reactivating it."""

        async def _rollback(s: AsyncSession) -> RubricVersion | None:
            active = await self.get_active(session=s)
            if active:
                active.status = "superseded"

            result = await s.execute(
                select(RubricVersion).where(
                    RubricVersion.agent_id == self.agent_id,
                    RubricVersion.version == target_version,
                )
            )
            target = result.scalar_one_or_none()
            if target:
                target.status = "rollback"
                from datetime import UTC, datetime

                ts = datetime.now(UTC).strftime("%Y%m%d%H%M")
                rv = RubricVersion(
                    agent_id=self.agent_id,
                    version=f"{target_version}-rb{ts}",
                    parent_version=active.version if active else None,
                    change_reason=f"Rollback to {target_version}",
                    dimensions=target.dimensions,
                    outcome_correlations=target.outcome_correlations,
                    status="active",
                )
                s.add(rv)
                await s.flush()
                return rv
            return None

        if session:
            return await _rollback(session)
        async with self.db.session() as s:
            rv = await _rollback(s)
            if rv:
                await s.commit()
            return rv

    async def get_signals(
        self,
        episode_id: str | None = None,
        limit: int = 100,
    ) -> list[dict]:
        """Get outcome signals, optionally filtered by episode."""
        from nous.storage.models import OutcomeSignal

        async with self.db.session() as session:
            q = (
                select(OutcomeSignal)
                .where(
                    OutcomeSignal.agent_id == self.agent_id,
                )
                .order_by(OutcomeSignal.created_at.desc())
                .limit(limit)
            )

            if episode_id:
                from uuid import UUID as _UUID

                q = q.where(OutcomeSignal.episode_id == _UUID(episode_id))

            result = await session.execute(q)
            rows = result.scalars().all()

        return [
            {
                "id": str(r.id),
                "episode_id": str(r.episode_id),
                "signal_type": r.signal_type,
                "confidence": r.confidence,
                "evidence": r.evidence,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ]

    def to_detail(self, rv: RubricVersion) -> RubricVersionDetail:
        """Convert ORM model to Pydantic DTO."""
        dims = [RubricDimension(**d) for d in rv.dimensions]
        return RubricVersionDetail(
            id=rv.id,
            agent_id=rv.agent_id,
            version=rv.version,
            parent_version=rv.parent_version,
            change_reason=rv.change_reason,
            dimensions=dims,
            outcome_correlations=rv.outcome_correlations or {},
            status=rv.status,
            created_at=rv.created_at,
        )
