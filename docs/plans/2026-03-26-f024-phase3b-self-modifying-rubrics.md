# F024 Phase 3b — Self-Modifying Evaluation Rubrics Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a self-modifying rubric system that evolves evaluation dimensions based on outcome signals, breaking the self-improvement loop's score plateau at 6.5-6.75.

**Architecture:** Four-phase rollout. Phase 0 collects ground truth (outcome signals per episode). Phase 1 correlates dimensions with outcomes and adjusts weights. Phase 2 splits/merges dimensions based on divergent sub-component correlations. Phase 3 discovers new dimensions with Tim's approval. All rubric versions are immutable and stored in a new `heart.rubric_versions` table. Outcome signals are detected by a new event handler listening to `episode_summarized`. A `RubricManager` in `nous/cognitive/` owns versioning, correlation, and evolution logic.

**Tech Stack:** Python 3.12+, pydantic v2, async SQLAlchemy, PostgreSQL + JSONB, pytest, pure-Python correlation (no scipy/numpy)

**Spec:** `docs/features/F024-phase3b-self-modifying-rubrics.md`

---

## File Structure

| File | Action | Responsibility |
|------|--------|---------------|
| `sql/migrations/022_rubric_outcome_signals.sql` | **Create** | Migration: `heart.rubric_versions` + `heart.outcome_signals` tables |
| `nous/storage/models.py` | **Modify** | ORM models: `RubricVersion`, `OutcomeSignal` |
| `nous/cognitive/rubric_schemas.py` | **Create** | Pydantic DTOs: `RubricVersionDetail`, `RubricDimension`, `OutcomeSignalDetail`, `CorrelationReport`, `DimensionProposal` |
| `nous/cognitive/rubric.py` | **Create** | `RubricManager` — version CRUD, active rubric lookup, weight adjustment, split/merge, proposal creation |
| `nous/cognitive/correlation.py` | **Create** | Pure-Python Pearson/Spearman correlation, dimension-outcome analysis |
| `nous/handlers/outcome_detector.py` | **Create** | Event handler: listens to `session_ended`, classifies outcome signals via LLM after episode summarization, stores in DB |
| `nous/handlers/episode_summarizer.py` | **Modify** | Add `transcript` to `episode_summarized` event data so downstream handlers can access it |
| `nous/handlers/rubric_evolver.py` | **Create** | Scheduled handler: runs correlation analysis weekly, proposes weight adjustments / splits / merges |
| `nous/config.py` | **Modify** | Add `rubric_*` settings |
| `nous/main.py` | **Modify** | Wire `OutcomeDetector` + `RubricEvolver` handlers |
| `nous/api/rest.py` | **Modify** | Add rubric REST endpoints |
| `tests/test_rubric_schemas.py` | **Create** | Schema validation tests |
| `tests/test_rubric.py` | **Create** | RubricManager unit tests |
| `tests/test_correlation.py` | **Create** | Correlation engine tests |
| `tests/test_outcome_detector.py` | **Create** | Outcome detector handler tests |
| `tests/test_rubric_evolver.py` | **Create** | Rubric evolver handler tests |
| `tests/test_rubric_rest.py` | **Create** | REST endpoint integration tests |
| `tests/conftest.py` | **Modify** | Add shared `AsyncContextMock` fixture (if not already present) |

---

## Chunk A — Schema Foundation (Tasks 1-3)

### Task 1: Database Migration

**Files:**
- Create: `sql/migrations/022_rubric_outcome_signals.sql`

- [ ] **Step 1: Write migration SQL**

```sql
-- 022: Rubric versions + outcome signals for F024 Phase 3b

-- Rubric versions — immutable snapshots of evaluation criteria
CREATE TABLE IF NOT EXISTS heart.rubric_versions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id VARCHAR(100) NOT NULL REFERENCES nous_system.agents(id),
    version VARCHAR(20) NOT NULL,          -- semver: "1.0.0", "1.1.0"
    parent_version VARCHAR(20),            -- previous version string
    change_reason TEXT NOT NULL,
    dimensions JSONB NOT NULL,             -- array of dimension objects
    outcome_correlations JSONB DEFAULT '{}', -- dimension->outcome correlation data
    status VARCHAR(20) NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'superseded', 'rollback')),
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Only one active rubric per agent at a time
CREATE UNIQUE INDEX idx_rubric_active_agent
    ON heart.rubric_versions(agent_id) WHERE status = 'active';

-- Outcome signals — per-episode ground truth for rubric evolution
CREATE TABLE IF NOT EXISTS heart.outcome_signals (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id VARCHAR(100) NOT NULL,
    episode_id UUID NOT NULL REFERENCES heart.episodes(id) ON DELETE CASCADE,
    signal_type VARCHAR(30) NOT NULL
        CHECK (signal_type IN ('corrected', 'completed', 'praised', 'reworked', 'self_corrected')),
    confidence FLOAT NOT NULL DEFAULT 0.5,  -- detector confidence in classification
    evidence TEXT,                           -- what triggered the classification
    self_improvement_scores JSONB,          -- snapshot of rubric scores at time of episode
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_outcome_signals_agent_episode
    ON heart.outcome_signals(agent_id, episode_id);

CREATE INDEX idx_outcome_signals_agent_type
    ON heart.outcome_signals(agent_id, signal_type);
```

- [ ] **Step 2: Verify migration file exists and SQL is valid syntax**

Run: `cat sql/migrations/022_rubric_outcome_signals.sql`
Expected: SQL content with two CREATE TABLE statements

- [ ] **Step 3: Commit**

```bash
git add sql/migrations/022_rubric_outcome_signals.sql
git commit -m "feat(f024-3b): add migration 022 for rubric_versions and outcome_signals tables"
```

---

### Task 2: ORM Models

**Files:**
- Modify: `nous/storage/models.py` (add after `EpisodeProcedure` class, ~line 465)
- Test: `tests/test_rubric_schemas.py`

- [ ] **Step 1: Write failing test for ORM model instantiation**

```python
# tests/test_rubric_schemas.py
"""Tests for F024 Phase 3b rubric schemas and models."""
import uuid
from datetime import datetime, UTC

import pytest


class TestRubricVersionModel:
    def test_rubric_version_import(self):
        from nous.storage.models import RubricVersion
        assert RubricVersion.__tablename__ == "rubric_versions"

    def test_rubric_version_schema(self):
        from nous.storage.models import RubricVersion
        # __table_args__ is a tuple: (CheckConstraint(...), {"schema": "heart"})
        assert RubricVersion.__table_args__[-1]["schema"] == "heart"

    def test_rubric_version_status_constraint(self):
        from nous.storage.models import RubricVersion
        constraints = [a for a in RubricVersion.__table_args__ if hasattr(a, "name")]
        assert any(c.name == "ck_rubric_versions_status" for c in constraints)


class TestOutcomeSignalModel:
    def test_outcome_signal_import(self):
        from nous.storage.models import OutcomeSignal
        assert OutcomeSignal.__tablename__ == "outcome_signals"

    def test_outcome_signal_schema(self):
        from nous.storage.models import OutcomeSignal
        # __table_args__ is a tuple: (CheckConstraint(...), {"schema": "heart"})
        assert OutcomeSignal.__table_args__[-1]["schema"] == "heart"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_rubric_schemas.py::TestRubricVersionModel::test_rubric_version_import -v`
Expected: FAIL with ImportError (RubricVersion not found)

- [ ] **Step 3: Implement ORM models in models.py**

Add after the `EpisodeProcedure` class (~line 465 in `nous/storage/models.py`):

```python
class RubricVersion(Base):
    __tablename__ = "rubric_versions"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'superseded', 'rollback')",
            name="ck_rubric_versions_status",
        ),
        {"schema": "heart"},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    agent_id: Mapped[str] = mapped_column(String(100), nullable=False)
    version: Mapped[str] = mapped_column(String(20), nullable=False)
    parent_version: Mapped[str | None] = mapped_column(String(20))
    change_reason: Mapped[str] = mapped_column(Text, nullable=False)
    dimensions: Mapped[dict] = mapped_column(JSONB, nullable=False)
    outcome_correlations: Mapped[dict] = mapped_column(JSONB, server_default="{}")
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class OutcomeSignal(Base):
    __tablename__ = "outcome_signals"
    __table_args__ = (
        CheckConstraint(
            "signal_type IN ('corrected', 'completed', 'praised', 'reworked', 'self_corrected')",
            name="ck_outcome_signals_type",
        ),
        {"schema": "heart"},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    agent_id: Mapped[str] = mapped_column(String(100), nullable=False)
    episode_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("heart.episodes.id", ondelete="CASCADE"), nullable=False)
    signal_type: Mapped[str] = mapped_column(String(30), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, server_default="0.5")
    evidence: Mapped[str | None] = mapped_column(Text)
    self_improvement_scores: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_rubric_schemas.py -v`
Expected: 4 PASSED

- [ ] **Step 5: Commit**

```bash
git add nous/storage/models.py tests/test_rubric_schemas.py
git commit -m "feat(f024-3b): add RubricVersion and OutcomeSignal ORM models"
```

---

### Task 3: Pydantic Schemas + Config Settings

**Files:**
- Create: `nous/cognitive/rubric_schemas.py`
- Modify: `nous/config.py` (~line 288, after `critic_*` settings)
- Test: `tests/test_rubric_schemas.py` (append)

- [ ] **Step 1: Write failing tests for Pydantic schemas**

Append to `tests/test_rubric_schemas.py`:

```python
class TestRubricDimension:
    def test_dimension_defaults(self):
        from nous.cognitive.rubric_schemas import RubricDimension
        dim = RubricDimension(
            name="Recall",
            weight=0.25,
            description="Accuracy of memory retrieval",
            scoring_criteria="1-10 scale",
        )
        assert dim.min_weight == 0.10
        assert dim.max_weight == 0.40

    def test_dimension_weight_validation(self):
        from nous.cognitive.rubric_schemas import RubricDimension
        with pytest.raises(ValueError):
            RubricDimension(
                name="Bad",
                weight=0.50,  # exceeds max_weight default of 0.40
                description="test",
                scoring_criteria="test",
            )


class TestRubricVersionDetail:
    def test_version_detail(self):
        from nous.cognitive.rubric_schemas import RubricVersionDetail, RubricDimension
        dim = RubricDimension(
            name="Recall", weight=0.25,
            description="test", scoring_criteria="test",
        )
        rv = RubricVersionDetail(
            id=uuid.uuid4(),
            agent_id="test",
            version="1.0.0",
            change_reason="Initial",
            dimensions=[dim],
            status="active",
            created_at=datetime.now(UTC),
        )
        assert rv.version == "1.0.0"
        assert len(rv.dimensions) == 1


class TestOutcomeSignalDetail:
    def test_signal_types(self):
        from nous.cognitive.rubric_schemas import OutcomeSignalDetail, OutcomeSignalType
        assert "corrected" in [e.value for e in OutcomeSignalType]
        assert "praised" in [e.value for e in OutcomeSignalType]

    def test_signal_detail(self):
        from nous.cognitive.rubric_schemas import OutcomeSignalDetail
        sig = OutcomeSignalDetail(
            id=uuid.uuid4(),
            agent_id="test",
            episode_id=uuid.uuid4(),
            signal_type="corrected",
            confidence=0.85,
            evidence="User said 'no, actually...'",
            created_at=datetime.now(UTC),
        )
        assert sig.confidence == 0.85
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_rubric_schemas.py::TestRubricDimension -v`
Expected: FAIL with ImportError

- [ ] **Step 3: Implement rubric_schemas.py**

```python
# nous/cognitive/rubric_schemas.py
"""Pydantic DTOs for F024 Phase 3b — Self-Modifying Rubrics."""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, model_validator


class OutcomeSignalType(str, Enum):
    CORRECTED = "corrected"
    COMPLETED = "completed"
    PRAISED = "praised"
    REWORKED = "reworked"
    SELF_CORRECTED = "self_corrected"


class RubricDimension(BaseModel):
    """A single evaluation dimension within a rubric."""
    name: str
    weight: float = Field(ge=0.0, le=1.0)
    description: str
    scoring_criteria: str
    min_weight: float = 0.10
    max_weight: float = 0.40

    @model_validator(mode="after")
    def weight_in_bounds(self) -> "RubricDimension":
        if self.weight < self.min_weight or self.weight > self.max_weight:
            raise ValueError(
                f"weight {self.weight} outside [{self.min_weight}, {self.max_weight}]"
            )
        return self


class RubricVersionDetail(BaseModel):
    """Full rubric version with all fields."""
    id: UUID
    agent_id: str
    version: str
    parent_version: str | None = None
    change_reason: str
    dimensions: list[RubricDimension]
    outcome_correlations: dict = Field(default_factory=dict)
    status: Literal["active", "superseded", "rollback"] = "active"
    created_at: datetime


class RubricVersionSummary(BaseModel):
    """Lightweight rubric version for listings."""
    id: UUID
    version: str
    status: str
    change_reason: str
    dimension_count: int
    created_at: datetime


class OutcomeSignalDetail(BaseModel):
    """A single outcome signal for an episode."""
    id: UUID
    agent_id: str
    episode_id: UUID
    signal_type: str
    confidence: float
    evidence: str | None = None
    self_improvement_scores: dict | None = None
    created_at: datetime


class CorrelationResult(BaseModel):
    """Correlation between a dimension and an outcome signal type."""
    dimension: str
    signal_type: str
    pearson_r: float
    spearman_rho: float
    sample_size: int


class CorrelationReport(BaseModel):
    """Full correlation analysis for a rubric version."""
    rubric_version: str
    correlations: list[CorrelationResult]
    suggested_weights: dict[str, float] | None = None
    suggested_splits: list[str] = Field(default_factory=list)
    suggested_merges: list[tuple[str, str]] = Field(default_factory=list)
    episode_count: int


class DimensionProposal(BaseModel):
    """Proposed new dimension for Tim's approval."""
    name: str
    description: str
    scoring_criteria: str
    evidence_episode_ids: list[UUID]
    gap_analysis: str
    suggested_weight: float = 0.15
```

- [ ] **Step 4: Add config settings to `nous/config.py`**

Add after the `critic_*` settings (~line 291):

```python
    # F024 Phase 3b: Self-Modifying Rubrics
    rubric_enabled: bool = True
    rubric_outcome_detection_enabled: bool = True
    rubric_evolution_enabled: bool = False  # Phase 1+ — start disabled
    rubric_min_episodes_for_correlation: int = 50
    rubric_weight_change_cap: float = 0.05
    rubric_min_dimensions: int = 3
    rubric_max_dimensions: int = 7
    rubric_max_versions_per_week: int = 1
    rubric_outcome_model: str = "claude-haiku-4-5-20251001"
```

- [ ] **Step 5: Run all schema tests**

Run: `uv run pytest tests/test_rubric_schemas.py -v`
Expected: All PASSED

- [ ] **Step 6: Commit**

```bash
git add nous/cognitive/rubric_schemas.py nous/config.py tests/test_rubric_schemas.py
git commit -m "feat(f024-3b): add rubric Pydantic schemas and config settings"
```

---

## Chunk B — Phase 0: Outcome Signal Collection (Tasks 4-6)

### Task 4: RubricManager — Core CRUD

**Files:**
- Create: `nous/cognitive/rubric.py`
- Test: `tests/test_rubric.py`

- [ ] **Step 1: Write failing tests for RubricManager**

```python
# tests/test_rubric.py
"""Tests for F024 Phase 3b RubricManager."""
import uuid
from datetime import datetime, UTC
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nous.cognitive.rubric_schemas import RubricDimension, RubricVersionDetail


# Default v1.0.0 dimensions for testing
def _default_dimensions() -> list[dict]:
    return [
        {"name": "Recall", "weight": 0.25, "description": "Accuracy and completeness of memory retrieval", "scoring_criteria": "1-10"},
        {"name": "Tool Selection", "weight": 0.25, "description": "Choosing the right tool for the task", "scoring_criteria": "1-10"},
        {"name": "Confidence Calibration", "weight": 0.25, "description": "Accuracy of confidence estimates", "scoring_criteria": "1-10"},
        {"name": "Proactivity", "weight": 0.25, "description": "Anticipating needs without being asked", "scoring_criteria": "1-10"},
    ]


class TestRubricManagerGetActive:
    @pytest.mark.asyncio
    async def test_get_active_returns_none_when_no_rubric(self):
        from nous.cognitive.rubric import RubricManager
        db = MagicMock()
        mock_session = AsyncMock()
        mock_result = AsyncMock()
        mock_result.scalar_one_or_none = MagicMock(return_value=None)
        mock_session.execute = AsyncMock(return_value=mock_result)
        db.session = MagicMock(return_value=AsyncContextMock(mock_session))

        mgr = RubricManager(db=db, agent_id="test")
        result = await mgr.get_active()
        assert result is None

    @pytest.mark.asyncio
    async def test_seed_creates_v1_when_none_exists(self):
        from nous.cognitive.rubric import RubricManager
        from nous.storage.models import RubricVersion

        db = MagicMock()
        mock_session = AsyncMock()
        # First call: get_active returns None
        mock_result_none = AsyncMock()
        mock_result_none.scalar_one_or_none = MagicMock(return_value=None)
        # Second call: after insert, return the new version
        mock_session.execute = AsyncMock(return_value=mock_result_none)
        mock_session.add = MagicMock()
        mock_session.flush = AsyncMock()
        db.session = MagicMock(return_value=AsyncContextMock(mock_session))

        mgr = RubricManager(db=db, agent_id="test")
        result = await mgr.seed_v1()
        mock_session.add.assert_called_once()
        added_obj = mock_session.add.call_args[0][0]
        assert added_obj.version == "1.0.0"
        assert len(added_obj.dimensions) == 4


class TestRubricManagerVersioning:
    @pytest.mark.asyncio
    async def test_create_version_supersedes_active(self):
        from nous.cognitive.rubric import RubricManager
        from nous.storage.models import RubricVersion

        db = MagicMock()
        mock_session = AsyncMock()
        # Mock active version exists
        active = MagicMock(spec=RubricVersion)
        active.id = uuid.uuid4()
        active.version = "1.0.0"
        active.status = "active"
        active.dimensions = _default_dimensions()
        mock_result = AsyncMock()
        mock_result.scalar_one_or_none = MagicMock(return_value=active)
        mock_session.execute = AsyncMock(return_value=mock_result)
        mock_session.add = MagicMock()
        mock_session.flush = AsyncMock()
        db.session = MagicMock(return_value=AsyncContextMock(mock_session))

        mgr = RubricManager(db=db, agent_id="test")
        new_dims = _default_dimensions()
        new_dims[0]["weight"] = 0.30  # Shift Recall up
        new_dims[3]["weight"] = 0.20  # Shift Proactivity down
        await mgr.create_version(
            new_version="1.1.0",
            dimensions=new_dims,
            change_reason="Weight adjustment based on correlation",
        )
        # Old version should be superseded
        assert active.status == "superseded"
        # New version added
        mock_session.add.assert_called_once()


# Helper for async context manager mocking
class AsyncContextMock:
    def __init__(self, session):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *args):
        pass
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_rubric.py -v`
Expected: FAIL with ImportError

- [ ] **Step 3: Implement RubricManager**

```python
# nous/cognitive/rubric.py
"""F024 Phase 3b — RubricManager for self-modifying evaluation rubrics.

Manages rubric versions: CRUD, seeding v1.0.0, creating new versions
with weight adjustments, dimension splits/merges, and proposals.
All versions are immutable. Rollback = reactivate previous version.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select, update as sa_update
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
        "scoring_criteria": "1: No relevant memories retrieved. 5: Some relevant, some missed. 10: All relevant memories retrieved with high precision.",
        "min_weight": 0.10,
        "max_weight": 0.40,
    },
    {
        "name": "Tool Selection",
        "weight": 0.25,
        "description": "Choosing the right tool for the task and using it efficiently",
        "scoring_criteria": "1: Wrong tools or excessive calls. 5: Right tools, some inefficiency. 10: Optimal tool choice and call efficiency.",
        "min_weight": 0.10,
        "max_weight": 0.40,
    },
    {
        "name": "Confidence Calibration",
        "weight": 0.25,
        "description": "Accuracy of confidence estimates vs actual outcomes",
        "scoring_criteria": "1: Confidence wildly mismatched to outcomes. 5: Some calibration. 10: Confidence closely tracks actual success rates.",
        "min_weight": 0.10,
        "max_weight": 0.40,
    },
    {
        "name": "Proactivity",
        "weight": 0.25,
        "description": "Anticipating needs without being asked",
        "scoring_criteria": "1: Purely reactive. 5: Some anticipation. 10: Consistently anticipates and prepares for user needs.",
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
        # Validate dimensions
        if not (3 <= len(dimensions) <= 7):
            raise ValueError(f"Dimension count {len(dimensions)} outside [3, 7]")

        total_weight = sum(d["weight"] for d in dimensions)
        if abs(total_weight - 1.0) > 0.01:
            raise ValueError(f"Weights sum to {total_weight}, expected ~1.0")

        for d in dimensions:
            min_w = d.get("min_weight", 0.10)
            max_w = d.get("max_weight", 0.40)
            if d["weight"] < min_w or d["weight"] > max_w:
                raise ValueError(
                    f"Dimension '{d['name']}' weight {d['weight']} outside [{min_w}, {max_w}]"
                )

        async def _create(s: AsyncSession) -> RubricVersion:
            # Supersede current active version
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
            # Supersede current
            active = await self.get_active(session=s)
            if active:
                active.status = "superseded"

            # Find target and reactivate
            result = await s.execute(
                select(RubricVersion).where(
                    RubricVersion.agent_id == self.agent_id,
                    RubricVersion.version == target_version,
                )
            )
            target = result.scalar_one_or_none()
            if target:
                target.status = "rollback"
                # Create a new version that copies the target's dimensions
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_rubric.py -v`
Expected: All PASSED

- [ ] **Step 5: Commit**

```bash
git add nous/cognitive/rubric.py tests/test_rubric.py
git commit -m "feat(f024-3b): add RubricManager with version CRUD, seeding, rollback"
```

---

### Task 5: Outcome Signal Detector

**Files:**
- Create: `nous/handlers/outcome_detector.py`
- Test: `tests/test_outcome_detector.py`

- [ ] **Step 1: Write failing tests for outcome detection**

```python
# tests/test_outcome_detector.py
"""Tests for F024 Phase 3b outcome signal detector."""
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nous.events import Event


class TestOutcomeDetector:
    @pytest.mark.asyncio
    async def test_skip_when_no_episode_id(self):
        from nous.handlers.outcome_detector import OutcomeDetector
        settings = MagicMock()
        settings.rubric_outcome_detection_enabled = True
        settings.rubric_outcome_model = "test-model"
        bus = MagicMock()
        bus.on = MagicMock()
        db = MagicMock()

        detector = OutcomeDetector(db=db, settings=settings, bus=bus, llm_client=None, agent_id="test")
        event = Event(type="session_ended", agent_id="test", data={})
        await detector.handle(event)
        # No crash, no DB call

    @pytest.mark.asyncio
    async def test_skip_when_disabled(self):
        from nous.handlers.outcome_detector import OutcomeDetector
        settings = MagicMock()
        settings.rubric_outcome_detection_enabled = False
        bus = MagicMock()
        bus.on = MagicMock()
        db = MagicMock()

        detector = OutcomeDetector(db=db, settings=settings, bus=bus, llm_client=None, agent_id="test")
        event = Event(
            type="session_ended", agent_id="test",
            data={"episode_id": str(uuid.uuid4()), "transcript": "A long enough transcript for testing purposes here."},
        )
        await detector.handle(event)
        # No LLM call, no DB write

    @pytest.mark.asyncio
    async def test_skip_when_transcript_too_short(self):
        from nous.handlers.outcome_detector import OutcomeDetector
        settings = MagicMock()
        settings.rubric_outcome_detection_enabled = True
        bus = MagicMock()
        bus.on = MagicMock()
        db = MagicMock()

        detector = OutcomeDetector(db=db, settings=settings, bus=bus, llm_client=None, agent_id="test")
        event = Event(
            type="session_ended", agent_id="test",
            data={"episode_id": str(uuid.uuid4()), "transcript": "hi"},
        )
        await detector.handle(event)
        # Transcript too short, no processing

    @pytest.mark.asyncio
    async def test_detect_correction_signal(self):
        from nous.handlers.outcome_detector import OutcomeDetector

        settings = MagicMock()
        settings.rubric_outcome_detection_enabled = True
        settings.rubric_outcome_model = "test-model"
        bus = MagicMock()
        bus.on = MagicMock()
        bus.emit = AsyncMock()

        mock_session = AsyncMock()
        mock_session.add = MagicMock()
        mock_session.commit = AsyncMock()
        db = MagicMock()
        db.session = MagicMock(return_value=_AsyncCtx(mock_session))

        llm_client = AsyncMock()

        detector = OutcomeDetector(
            db=db, settings=settings, bus=bus,
            llm_client=llm_client, agent_id="test",
        )

        # Mock LLM response
        with patch("nous.handlers.outcome_detector.call_background_llm", new_callable=AsyncMock) as mock_llm:
            mock_llm.return_value = '{"signals": [{"type": "corrected", "confidence": 0.9, "evidence": "User said no actually"}]}'

            event = Event(
                type="session_ended", agent_id="test",
                data={
                    "episode_id": str(uuid.uuid4()),
                    "transcript": "User: Do X\nAssistant: Did Y\nUser: No, actually I meant X not Y\nAssistant: Sorry, doing X now",
                },
            )
            await detector.handle(event)
            mock_session.add.assert_called()


class _AsyncCtx:
    def __init__(self, s):
        self._s = s
    async def __aenter__(self):
        return self._s
    async def __aexit__(self, *a):
        pass
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_outcome_detector.py -v`
Expected: FAIL with ImportError

- [ ] **Step 3: Implement OutcomeDetector**

```python
# nous/handlers/outcome_detector.py
"""F024 Phase 3b — Outcome Signal Detector.

Listens to: session_ended
Emits: outcome_signals_detected

Classifies episode outcomes using LLM analysis of the episode summary
and transcript. Stores structured outcome signals for rubric evolution.
"""
from __future__ import annotations

import json
import logging
from typing import Any
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
        """Handle session_ended — detect and store outcome signals.

        Listens to session_ended (not episode_summarized) because session_ended
        includes the full transcript. The episode summary may not exist yet
        at this point, so we use the transcript directly for LLM classification.
        """
        if not self._settings.rubric_outcome_detection_enabled:
            return

        episode_id = event.data.get("episode_id")
        if not episode_id:
            return

        transcript = event.data.get("transcript", "")
        if not transcript or len(transcript) < 50:
            return

        # Build a lightweight summary from available data
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
                len(signals), episode_id,
                [s.get("type") for s in signals],
            )

            await self._bus.emit(Event(
                type="outcome_signals_detected",
                agent_id=event.agent_id,
                session_id=event.session_id,
                data={
                    "episode_id": episode_id,
                    "signals": signals,
                },
            ))

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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_outcome_detector.py -v`
Expected: All PASSED

- [ ] **Step 5: Commit**

```bash
git add nous/handlers/outcome_detector.py tests/test_outcome_detector.py
git commit -m "feat(f024-3b): add OutcomeDetector handler for episode outcome classification"
```

---

### Task 6: Wire Outcome Detector + REST Endpoints

**Files:**
- Modify: `nous/main.py` (~line 162, after FactExtractor wiring)
- Modify: `nous/api/rest.py` (add 3 rubric endpoints)
- Test: `tests/test_rubric_rest.py`

- [ ] **Step 1: Write failing tests for REST endpoints**

```python
# tests/test_rubric_rest.py
"""Tests for F024 Phase 3b rubric REST endpoints."""
import uuid
from datetime import datetime, UTC
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestRubricEndpoints:
    @pytest.mark.asyncio
    async def test_get_rubric_returns_active(self):
        from nous.api.rest import get_rubric
        from nous.storage.models import RubricVersion

        mock_rv = MagicMock(spec=RubricVersion)
        mock_rv.id = uuid.uuid4()
        mock_rv.agent_id = "test"
        mock_rv.version = "1.0.0"
        mock_rv.parent_version = None
        mock_rv.change_reason = "Initial"
        mock_rv.dimensions = [
            {"name": "Recall", "weight": 0.25, "description": "test", "scoring_criteria": "test", "min_weight": 0.10, "max_weight": 0.40},
        ]
        mock_rv.outcome_correlations = {}
        mock_rv.status = "active"
        mock_rv.created_at = datetime.now(UTC)

        request = MagicMock()
        request.app.state.rubric_manager = MagicMock()
        request.app.state.rubric_manager.get_active = AsyncMock(return_value=mock_rv)
        request.app.state.rubric_manager.to_detail = MagicMock(
            return_value=MagicMock(model_dump=MagicMock(return_value={"version": "1.0.0"}))
        )

        response = await get_rubric(request)
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_get_rubric_returns_404_when_none(self):
        from nous.api.rest import get_rubric

        request = MagicMock()
        request.app.state.rubric_manager = MagicMock()
        request.app.state.rubric_manager.get_active = AsyncMock(return_value=None)

        response = await get_rubric(request)
        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_get_rubric_history(self):
        from nous.api.rest import get_rubric_history

        request = MagicMock()
        request.query_params = {}
        request.app.state.rubric_manager = MagicMock()
        request.app.state.rubric_manager.get_history = AsyncMock(return_value=[])

        response = await get_rubric_history(request)
        assert response.status_code == 200
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_rubric_rest.py -v`
Expected: FAIL with ImportError (get_rubric not found)

- [ ] **Step 3: Add REST endpoint functions to `nous/api/rest.py`**

Add before the `create_app` function:

```python
# --- F024 Phase 3b: Rubric endpoints ---

async def get_rubric(request: Request) -> JSONResponse:
    """GET /rubric — current active rubric version."""
    mgr = request.app.state.rubric_manager
    if not mgr:
        return JSONResponse({"error": "Rubric system not enabled"}, status_code=503)
    active = await mgr.get_active()
    if not active:
        return JSONResponse({"error": "No active rubric"}, status_code=404)
    detail = mgr.to_detail(active)
    return JSONResponse(detail.model_dump(mode="json"))


async def get_rubric_history(request: Request) -> JSONResponse:
    """GET /rubric/history — rubric version history."""
    mgr = request.app.state.rubric_manager
    if not mgr:
        return JSONResponse({"error": "Rubric system not enabled"}, status_code=503)
    limit = int(request.query_params.get("limit", "20"))
    history = await mgr.get_history(limit=limit)
    return JSONResponse([h.model_dump(mode="json") for h in history])


async def get_outcome_signals(request: Request) -> JSONResponse:
    """GET /rubric/signals — outcome signals with optional episode filter.

    Note: imports of OutcomeSignal and select should be added at the top of rest.py
    alongside the existing model/query imports.
    """
    db = request.app.state.database
    agent_id = request.app.state.settings.agent_id

    from sqlalchemy import select
    from uuid import UUID as _UUID
    from nous.storage.models import OutcomeSignal

    episode_id = request.query_params.get("episode_id")

    async with db.session() as session:
        q = select(OutcomeSignal).where(
            OutcomeSignal.agent_id == agent_id,
        ).order_by(OutcomeSignal.created_at.desc()).limit(100)

        if episode_id:
            q = q.where(OutcomeSignal.episode_id == _UUID(episode_id))

        result = await session.execute(q)
        rows = result.scalars().all()

    return JSONResponse([
        {
            "id": str(r.id),
            "episode_id": str(r.episode_id),
            "signal_type": r.signal_type,
            "confidence": r.confidence,
            "evidence": r.evidence,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ])
```

```python
async def trigger_evolution(request: Request) -> JSONResponse:
    """POST /rubric/evolve — manually trigger a rubric evolution cycle."""
    evolver = request.app.state.rubric_evolver
    if not evolver:
        return JSONResponse({"error": "Rubric evolution not enabled"}, status_code=503)

    try:
        report = await evolver.run_evolution_cycle()
        if report:
            return JSONResponse(report.model_dump(mode="json"))
        return JSONResponse({"status": "no_change", "message": "Insufficient data or no weight changes needed"})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
```

Also wire `rubric_evolver` into `app.state` in `create_app`:
```python
    app.state.rubric_evolver = components.get("rubric_evolver")
```

Add routes to the routes list in `create_app` (after the dashboard routes):

```python
        # F024 Phase 3b: Rubric endpoints
        Route("/rubric", get_rubric),
        Route("/rubric/history", get_rubric_history),
        Route("/rubric/signals", get_outcome_signals),
        Route("/rubric/evolve", trigger_evolution, methods=["POST"]),
```

- [ ] **Step 4: Wire OutcomeDetector and RubricManager in `nous/main.py`**

Add after the FactExtractor wiring (~line 162):

```python
        # F024 Phase 3b: Outcome signal detection
        try:
            from nous.handlers.outcome_detector import OutcomeDetector

            if settings.rubric_outcome_detection_enabled:
                OutcomeDetector(
                    db=database, settings=settings, bus=bus,
                    llm_client=api_client, agent_id=settings.agent_id,
                )
        except ImportError:
            logger.debug("OutcomeDetector not available yet")
```

Also wire the RubricManager (add after brain/heart/cognitive initialization in `create_components()`, ~line 90, before handler wiring):

```python
    # F024 Phase 3b: Rubric manager
    rubric_manager = None
    if settings.rubric_enabled:
        from nous.cognitive.rubric import RubricManager
        rubric_manager = RubricManager(db=database, agent_id=settings.agent_id)
        # Seed v1.0.0 if no active rubric exists
        existing = await rubric_manager.get_active()
        if not existing:
            await rubric_manager.seed_v1()
            logger.info("F024-3b: Seeded initial rubric v1.0.0")
```

Add `rubric_manager` to the components dict returned by `create_components()`:

```python
    return {
        ...
        "rubric_manager": rubric_manager,
    }
```

In `create_app()`, accept and wire `rubric_manager` to `app.state`:

```python
    app.state.rubric_manager = components.get("rubric_manager")
```

- [ ] **Step 5: Run all tests**

Run: `uv run pytest tests/test_rubric_rest.py tests/test_outcome_detector.py tests/test_rubric.py tests/test_rubric_schemas.py -v`
Expected: All PASSED

- [ ] **Step 6: Commit**

```bash
git add nous/main.py nous/api/rest.py tests/test_rubric_rest.py
git commit -m "feat(f024-3b): wire OutcomeDetector handler and rubric REST endpoints"
```

---

## Chunk C — Phase 1: Correlation Analysis + Weight Adjustment (Tasks 7-8)

### Task 7: Correlation Engine

**Files:**
- Create: `nous/cognitive/correlation.py`
- Test: `tests/test_correlation.py`

- [ ] **Step 1: Write failing tests for correlation functions**

```python
# tests/test_correlation.py
"""Tests for F024 Phase 3b correlation engine."""
import pytest


class TestPearsonCorrelation:
    def test_perfect_positive(self):
        from nous.cognitive.correlation import pearson_r
        r = pearson_r([1, 2, 3, 4, 5], [2, 4, 6, 8, 10])
        assert abs(r - 1.0) < 0.001

    def test_perfect_negative(self):
        from nous.cognitive.correlation import pearson_r
        r = pearson_r([1, 2, 3, 4, 5], [10, 8, 6, 4, 2])
        assert abs(r - (-1.0)) < 0.001

    def test_no_correlation(self):
        from nous.cognitive.correlation import pearson_r
        r = pearson_r([1, 2, 3, 4, 5], [3, 1, 4, 1, 5])
        assert abs(r) < 0.5

    def test_too_few_samples(self):
        from nous.cognitive.correlation import pearson_r
        r = pearson_r([1], [2])
        assert r == 0.0

    def test_constant_values(self):
        from nous.cognitive.correlation import pearson_r
        r = pearson_r([5, 5, 5], [1, 2, 3])
        assert r == 0.0


class TestSpearmanCorrelation:
    def test_perfect_positive(self):
        from nous.cognitive.correlation import spearman_rho
        rho = spearman_rho([1, 2, 3, 4, 5], [2, 4, 6, 8, 10])
        assert abs(rho - 1.0) < 0.001

    def test_monotonic_nonlinear(self):
        from nous.cognitive.correlation import spearman_rho
        # Monotonic but non-linear — Spearman should still be ~1.0
        rho = spearman_rho([1, 2, 3, 4, 5], [1, 4, 9, 16, 25])
        assert abs(rho - 1.0) < 0.001


class TestDimensionOutcomeCorrelation:
    def test_correlate_dimensions_with_outcomes(self):
        from nous.cognitive.correlation import correlate_dimensions_with_outcomes

        episodes = [
            {"scores": {"Recall": 8, "Tool Selection": 5}, "signals": ["completed"]},
            {"scores": {"Recall": 3, "Tool Selection": 7}, "signals": ["corrected"]},
            {"scores": {"Recall": 9, "Tool Selection": 6}, "signals": ["completed", "praised"]},
            {"scores": {"Recall": 4, "Tool Selection": 4}, "signals": ["corrected", "reworked"]},
            {"scores": {"Recall": 7, "Tool Selection": 8}, "signals": ["completed"]},
        ]
        result = correlate_dimensions_with_outcomes(episodes, ["Recall", "Tool Selection"])
        assert len(result) > 0
        assert all(hasattr(r, "dimension") for r in result)
        assert all(hasattr(r, "pearson_r") for r in result)


class TestWeightSuggestion:
    def test_suggest_weights_from_correlations(self):
        from nous.cognitive.correlation import suggest_weights
        from nous.cognitive.rubric_schemas import CorrelationResult

        correlations = [
            CorrelationResult(dimension="Recall", signal_type="completed", pearson_r=0.7, spearman_rho=0.65, sample_size=50),
            CorrelationResult(dimension="Tool Selection", signal_type="completed", pearson_r=0.3, spearman_rho=0.25, sample_size=50),
            CorrelationResult(dimension="Confidence Calibration", signal_type="completed", pearson_r=0.5, spearman_rho=0.45, sample_size=50),
            CorrelationResult(dimension="Proactivity", signal_type="completed", pearson_r=0.2, spearman_rho=0.15, sample_size=50),
        ]
        current_weights = {"Recall": 0.25, "Tool Selection": 0.25, "Confidence Calibration": 0.25, "Proactivity": 0.25}
        suggested = suggest_weights(correlations, current_weights, cap=0.05)
        assert abs(sum(suggested.values()) - 1.0) < 0.01
        # Recall had highest correlation, should get weight increase
        assert suggested["Recall"] >= 0.25
        # All within bounds
        for w in suggested.values():
            assert 0.10 <= w <= 0.40
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_correlation.py -v`
Expected: FAIL with ImportError

- [ ] **Step 3: Implement correlation engine**

```python
# nous/cognitive/correlation.py
"""F024 Phase 3b — Pure-Python correlation engine.

Computes Pearson and Spearman correlations between rubric dimension
scores and outcome signals. No scipy/numpy dependency.
"""
from __future__ import annotations

import math
from collections import defaultdict
from typing import Any

from nous.cognitive.rubric_schemas import CorrelationResult


def pearson_r(x: list[float], y: list[float]) -> float:
    """Compute Pearson correlation coefficient. Returns 0.0 on degenerate input."""
    n = len(x)
    if n < 2 or len(y) != n:
        return 0.0

    mean_x = sum(x) / n
    mean_y = sum(y) / n

    num = sum((xi - mean_x) * (yi - mean_y) for xi, yi in zip(x, y))
    den_x = math.sqrt(sum((xi - mean_x) ** 2 for xi in x))
    den_y = math.sqrt(sum((yi - mean_y) ** 2 for yi in y))

    if den_x == 0 or den_y == 0:
        return 0.0

    return num / (den_x * den_y)


def _rank(values: list[float]) -> list[float]:
    """Compute fractional ranks for a list of values."""
    n = len(values)
    indexed = sorted(range(n), key=lambda i: values[i])
    ranks = [0.0] * n

    i = 0
    while i < n:
        j = i
        while j < n - 1 and values[indexed[j]] == values[indexed[j + 1]]:
            j += 1
        avg_rank = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[indexed[k]] = avg_rank
        i = j + 1

    return ranks


def spearman_rho(x: list[float], y: list[float]) -> float:
    """Compute Spearman rank correlation. Falls back to Pearson on ranks."""
    if len(x) < 2:
        return 0.0
    return pearson_r(_rank(x), _rank(y))


def correlate_dimensions_with_outcomes(
    episodes: list[dict[str, Any]],
    dimension_names: list[str],
) -> list[CorrelationResult]:
    """Correlate dimension scores with outcome signal presence.

    Each episode dict must have:
    - "scores": dict mapping dimension name to numeric score
    - "signals": list of signal type strings

    For each (dimension, signal_type) pair, computes correlation between
    the dimension score and the binary presence of that signal.
    """
    all_signal_types = set()
    for ep in episodes:
        for sig in ep.get("signals", []):
            all_signal_types.add(sig)

    results = []
    for dim in dimension_names:
        dim_scores = [ep["scores"].get(dim, 0) for ep in episodes]

        for sig_type in sorted(all_signal_types):
            sig_binary = [
                1.0 if sig_type in ep.get("signals", []) else 0.0
                for ep in episodes
            ]

            r = pearson_r(dim_scores, sig_binary)
            rho = spearman_rho(dim_scores, sig_binary)

            results.append(CorrelationResult(
                dimension=dim,
                signal_type=sig_type,
                pearson_r=round(r, 4),
                spearman_rho=round(rho, 4),
                sample_size=len(episodes),
            ))

    return results


def suggest_weights(
    correlations: list[CorrelationResult],
    current_weights: dict[str, float],
    cap: float = 0.05,
    min_weight: float = 0.10,
    max_weight: float = 0.40,
) -> dict[str, float]:
    """Suggest new weights based on correlation strength.

    Uses average |pearson_r| per dimension as importance signal.
    Shifts weights toward higher-correlation dimensions,
    capped at ±cap per adjustment cycle.
    """
    # Compute average absolute correlation per dimension
    dim_importance: dict[str, float] = {}
    dim_counts: dict[str, int] = {}
    for c in correlations:
        dim_importance[c.dimension] = dim_importance.get(c.dimension, 0) + abs(c.pearson_r)
        dim_counts[c.dimension] = dim_counts.get(c.dimension, 0) + 1

    for dim in dim_importance:
        if dim_counts[dim] > 0:
            dim_importance[dim] /= dim_counts[dim]

    if not dim_importance:
        return dict(current_weights)

    # Normalize importance to target distribution
    total_importance = sum(dim_importance.values())
    if total_importance == 0:
        return dict(current_weights)

    target_weights = {
        dim: imp / total_importance
        for dim, imp in dim_importance.items()
    }

    # Apply capped adjustment
    new_weights = {}
    for dim, current in current_weights.items():
        target = target_weights.get(dim, current)
        delta = target - current
        clamped_delta = max(-cap, min(cap, delta))
        new_weight = max(min_weight, min(max_weight, current + clamped_delta))
        new_weights[dim] = round(new_weight, 4)

    # Normalize to sum to 1.0
    total = sum(new_weights.values())
    if total > 0:
        new_weights = {d: round(w / total, 4) for d, w in new_weights.items()}

    return new_weights
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_correlation.py -v`
Expected: All PASSED

- [ ] **Step 5: Commit**

```bash
git add nous/cognitive/correlation.py tests/test_correlation.py
git commit -m "feat(f024-3b): add pure-Python correlation engine for rubric evolution"
```

---

### Task 8: Rubric Evolver Handler (Phase 1 Weight Adjustment)

**Files:**
- Create: `nous/handlers/rubric_evolver.py`
- Modify: `nous/main.py` (wire handler)
- Test: `tests/test_rubric_evolver.py`

- [ ] **Step 1: Write failing tests for rubric evolver**

```python
# tests/test_rubric_evolver.py
"""Tests for F024 Phase 3b rubric evolver handler."""
import uuid
from datetime import datetime, UTC
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nous.events import Event


def _mock_rubric_version(version="1.0.0"):
    rv = MagicMock()
    rv.id = uuid.uuid4()
    rv.version = version
    rv.dimensions = [
        {"name": "Recall", "weight": 0.25, "description": "test", "scoring_criteria": "t", "min_weight": 0.10, "max_weight": 0.40},
        {"name": "Tool Selection", "weight": 0.25, "description": "test", "scoring_criteria": "t", "min_weight": 0.10, "max_weight": 0.40},
        {"name": "Confidence Calibration", "weight": 0.25, "description": "test", "scoring_criteria": "t", "min_weight": 0.10, "max_weight": 0.40},
        {"name": "Proactivity", "weight": 0.25, "description": "test", "scoring_criteria": "t", "min_weight": 0.10, "max_weight": 0.40},
    ]
    rv.outcome_correlations = {}
    rv.created_at = datetime.now(UTC)
    return rv


class TestRubricEvolver:
    @pytest.mark.asyncio
    async def test_skip_when_disabled(self):
        from nous.handlers.rubric_evolver import RubricEvolver

        settings = MagicMock()
        settings.rubric_evolution_enabled = False

        evolver = RubricEvolver(
            rubric_manager=MagicMock(),
            db=MagicMock(),
            settings=settings,
            agent_id="test",
        )
        # Should return without doing anything
        await evolver.run_evolution_cycle()

    @pytest.mark.asyncio
    async def test_skip_when_insufficient_episodes(self):
        from nous.handlers.rubric_evolver import RubricEvolver

        settings = MagicMock()
        settings.rubric_evolution_enabled = True
        settings.rubric_min_episodes_for_correlation = 50
        settings.rubric_weight_change_cap = 0.05

        mock_session = AsyncMock()
        mock_result = AsyncMock()
        mock_result.scalar_one_or_none = MagicMock(return_value=10)  # Only 10 episodes
        mock_session.execute = AsyncMock(return_value=mock_result)
        db = MagicMock()
        db.session = MagicMock(return_value=_AsyncCtx(mock_session))

        rubric_mgr = MagicMock()
        rubric_mgr.get_active = AsyncMock(return_value=_mock_rubric_version())

        evolver = RubricEvolver(
            rubric_manager=rubric_mgr,
            db=db,
            settings=settings,
            agent_id="test",
        )
        await evolver.run_evolution_cycle()
        # Should not create a new version
        rubric_mgr.create_version.assert_not_called()

    @pytest.mark.asyncio
    async def test_adjusts_weights_when_sufficient_data(self):
        from nous.handlers.rubric_evolver import RubricEvolver

        settings = MagicMock()
        settings.rubric_evolution_enabled = True
        settings.rubric_min_episodes_for_correlation = 5  # Low for test
        settings.rubric_weight_change_cap = 0.05
        settings.rubric_max_versions_per_week = 1

        # Mock DB returning enough outcome signals
        mock_session = AsyncMock()
        mock_signals = [
            MagicMock(episode_id=uuid.uuid4(), signal_type="completed", self_improvement_scores={"Recall": 8, "Tool Selection": 5, "Confidence Calibration": 7, "Proactivity": 7}),
            MagicMock(episode_id=uuid.uuid4(), signal_type="corrected", self_improvement_scores={"Recall": 3, "Tool Selection": 7, "Confidence Calibration": 4, "Proactivity": 6}),
            MagicMock(episode_id=uuid.uuid4(), signal_type="completed", self_improvement_scores={"Recall": 9, "Tool Selection": 6, "Confidence Calibration": 8, "Proactivity": 7}),
            MagicMock(episode_id=uuid.uuid4(), signal_type="praised", self_improvement_scores={"Recall": 8, "Tool Selection": 8, "Confidence Calibration": 7, "Proactivity": 9}),
            MagicMock(episode_id=uuid.uuid4(), signal_type="completed", self_improvement_scores={"Recall": 7, "Tool Selection": 4, "Confidence Calibration": 6, "Proactivity": 7}),
        ]

        mock_result_count = MagicMock()
        mock_result_count.scalar_one_or_none = MagicMock(return_value=5)

        mock_result_signals = MagicMock()
        mock_result_signals.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=mock_signals)))

        mock_result_recent = MagicMock()
        mock_result_recent.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))

        call_count = 0
        async def mock_execute(q):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return mock_result_count
            elif call_count == 2:
                return mock_result_signals
            return mock_result_recent

        mock_session.execute = mock_execute
        db = MagicMock()
        db.session = MagicMock(return_value=_AsyncCtx(mock_session))

        rubric_mgr = MagicMock()
        rubric_mgr.get_active = AsyncMock(return_value=_mock_rubric_version())
        rubric_mgr.create_version = AsyncMock()

        evolver = RubricEvolver(
            rubric_manager=rubric_mgr,
            db=db,
            settings=settings,
            agent_id="test",
        )
        await evolver.run_evolution_cycle()
        rubric_mgr.create_version.assert_called_once()


class _AsyncCtx:
    def __init__(self, s):
        self._s = s
    async def __aenter__(self):
        return self._s
    async def __aexit__(self, *a):
        pass
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_rubric_evolver.py -v`
Expected: FAIL with ImportError

- [ ] **Step 3: Implement RubricEvolver**

```python
# nous/handlers/rubric_evolver.py
"""F024 Phase 3b — Rubric Evolver.

Runs periodic correlation analysis between rubric dimensions and
outcome signals. Proposes weight adjustments (Phase 1), splits/merges
(Phase 2), and new dimensions (Phase 3).

Not event-driven — called on a schedule (weekly) or manually.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import func as sa_func, select

from nous.cognitive.correlation import correlate_dimensions_with_outcomes, suggest_weights
from nous.cognitive.rubric import RubricManager
from nous.cognitive.rubric_schemas import CorrelationReport
from nous.config import Settings
from nous.storage.database import Database
from nous.storage.models import OutcomeSignal, RubricVersion

logger = logging.getLogger(__name__)


class RubricEvolver:
    """Evolves rubric dimensions based on outcome correlation analysis."""

    def __init__(
        self,
        rubric_manager: RubricManager,
        db: Database,
        settings: Settings,
        agent_id: str,
    ) -> None:
        self._rubric = rubric_manager
        self._db = db
        self._settings = settings
        self._agent_id = agent_id

    async def run_evolution_cycle(self) -> CorrelationReport | None:
        """Run one evolution cycle: correlate, suggest, apply if warranted."""
        if not self._settings.rubric_evolution_enabled:
            logger.debug("F024-3b: Rubric evolution disabled")
            return None

        active = await self._rubric.get_active()
        if not active:
            logger.warning("F024-3b: No active rubric, skipping evolution")
            return None

        async with self._db.session() as session:
            # Check we have enough data
            count_result = await session.execute(
                select(sa_func.count(OutcomeSignal.id)).where(
                    OutcomeSignal.agent_id == self._agent_id,
                )
            )
            total_signals = count_result.scalar_one_or_none() or 0

            if total_signals < self._settings.rubric_min_episodes_for_correlation:
                logger.info(
                    "F024-3b: Only %d signals, need %d for correlation",
                    total_signals, self._settings.rubric_min_episodes_for_correlation,
                )
                return None

            # Fetch all outcome signals with scores
            sig_result = await session.execute(
                select(OutcomeSignal).where(
                    OutcomeSignal.agent_id == self._agent_id,
                    OutcomeSignal.self_improvement_scores.isnot(None),
                )
            )
            signals = sig_result.scalars().all()

            # Check rate limit: max N versions per week
            week_ago = datetime.now(UTC) - timedelta(days=7)
            recent_result = await session.execute(
                select(RubricVersion).where(
                    RubricVersion.agent_id == self._agent_id,
                    RubricVersion.created_at >= week_ago,
                )
            )
            recent_versions = recent_result.scalars().all()
            if len(recent_versions) >= self._settings.rubric_max_versions_per_week:
                logger.info("F024-3b: Rate limited — %d versions this week", len(recent_versions))
                return None

        # Build episodes data for correlation
        episode_signals: dict[UUID, dict] = defaultdict(lambda: {"scores": {}, "signals": []})
        for sig in signals:
            ep = episode_signals[sig.episode_id]
            ep["signals"].append(sig.signal_type)
            if sig.self_improvement_scores and not ep["scores"]:
                ep["scores"] = sig.self_improvement_scores

        episodes = [ep for ep in episode_signals.values() if ep["scores"]]

        if len(episodes) < 3:
            logger.info("F024-3b: Only %d episodes with scores, need at least 3", len(episodes))
            return None

        # Compute correlations
        dim_names = [d["name"] for d in active.dimensions]
        correlations = correlate_dimensions_with_outcomes(episodes, dim_names)

        # Suggest weights
        current_weights = {d["name"]: d["weight"] for d in active.dimensions}
        suggested = suggest_weights(
            correlations, current_weights,
            cap=self._settings.rubric_weight_change_cap,
        )

        report = CorrelationReport(
            rubric_version=active.version,
            correlations=correlations,
            suggested_weights=suggested,
            episode_count=len(episodes),
        )

        # Check if weights actually changed
        weight_changed = any(
            abs(suggested.get(d, 0) - current_weights.get(d, 0)) > 0.001
            for d in current_weights
        )

        if not weight_changed:
            logger.info("F024-3b: No meaningful weight changes suggested")
            return report

        # Anti-Goodhart check: pause if scores are high but outcomes are poor
        if self.check_goodhart(episodes):
            logger.warning("F024-3b: Anti-Goodhart triggered — scores high but outcomes poor. Pausing evolution.")
            report.suggested_weights = None  # Clear suggestion
            return report

        # Apply weight adjustment
        new_dims = []
        for d in active.dimensions:
            updated = dict(d)
            updated["weight"] = suggested.get(d["name"], d["weight"])
            new_dims.append(updated)

        # Bump version (strip rollback suffix before parsing)
        base_version = active.version.split("-")[0]
        parts = base_version.split(".")
        new_version = f"{parts[0]}.{int(parts[1]) + 1}.0"

        await self._rubric.create_version(
            new_version=new_version,
            dimensions=new_dims,
            change_reason=f"Phase 1 weight adjustment based on {len(episodes)} episodes",
            outcome_correlations={
                c.dimension: {
                    **(active.outcome_correlations or {}).get(c.dimension, {}),
                    c.signal_type: {"pearson_r": c.pearson_r, "spearman_rho": c.spearman_rho},
                }
                for c in correlations
            },
        )

        logger.info(
            "F024-3b: Created rubric %s — weights: %s",
            new_version, suggested,
        )

        return report
```

- [ ] **Step 4: Wire RubricEvolver in `nous/main.py`**

Add after the OutcomeDetector wiring:

```python
        # F024 Phase 3b: Rubric evolver (triggered via REST or sleep handler)
        rubric_evolver = None
        if rubric_manager:
            from nous.handlers.rubric_evolver import RubricEvolver
            rubric_evolver = RubricEvolver(
                rubric_manager=rubric_manager,
                db=database,
                settings=settings,
                agent_id=settings.agent_id,
            )
```

- [ ] **Step 5: Run all tests**

Run: `uv run pytest tests/test_rubric_evolver.py tests/test_correlation.py -v`
Expected: All PASSED

- [ ] **Step 6: Commit**

```bash
git add nous/handlers/rubric_evolver.py nous/main.py tests/test_rubric_evolver.py
git commit -m "feat(f024-3b): add RubricEvolver for Phase 1 weight adjustment"
```

---

## Chunk D — Phase 2: Dimension Splits/Merges (Task 9)

### Task 9: Split/Merge Detection in RubricEvolver

**Files:**
- Modify: `nous/handlers/rubric_evolver.py` (add split/merge methods)
- Modify: `nous/cognitive/correlation.py` (add split detection)
- Test: `tests/test_rubric_evolver.py` (append split/merge tests)

- [ ] **Step 1: Write failing tests for split detection**

Append to `tests/test_rubric_evolver.py`:

```python
class TestSplitDetection:
    def test_detect_split_candidate(self):
        from nous.cognitive.correlation import detect_split_candidates
        from nous.cognitive.rubric_schemas import CorrelationResult

        # Tool Selection correlates differently with different outcomes
        correlations = [
            CorrelationResult(dimension="Tool Selection", signal_type="completed", pearson_r=0.8, spearman_rho=0.75, sample_size=50),
            CorrelationResult(dimension="Tool Selection", signal_type="corrected", pearson_r=0.2, spearman_rho=0.15, sample_size=50),
        ]
        candidates = detect_split_candidates(correlations, threshold=0.3)
        assert "Tool Selection" in candidates


class TestMergeDetection:
    def test_detect_merge_candidate(self):
        from nous.cognitive.correlation import detect_merge_candidates

        # Two dimensions with very similar correlation profiles
        dim_profiles = {
            "Recall": [0.7, 0.3, 0.5],
            "Memory Hygiene": [0.72, 0.28, 0.48],
            "Tool Selection": [0.2, 0.8, 0.1],
        }
        merges = detect_merge_candidates(dim_profiles, threshold=0.85)
        assert ("Recall", "Memory Hygiene") in merges or ("Memory Hygiene", "Recall") in merges
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_rubric_evolver.py::TestSplitDetection -v`
Expected: FAIL with ImportError

- [ ] **Step 3: Add split/merge detection to `nous/cognitive/correlation.py`**

Append to `correlation.py`:

```python
def detect_split_candidates(
    correlations: list[CorrelationResult],
    threshold: float = 0.3,
) -> list[str]:
    """Detect dimensions whose sub-signals show divergent correlations.

    If max |pearson_r| - min |pearson_r| > threshold for a dimension,
    it's a split candidate (sub-components behave differently).
    """
    dim_rs: dict[str, list[float]] = defaultdict(list)
    for c in correlations:
        dim_rs[c.dimension].append(abs(c.pearson_r))

    candidates = []
    for dim, rs in dim_rs.items():
        if len(rs) >= 2 and (max(rs) - min(rs)) > threshold:
            candidates.append(dim)

    return candidates


def detect_merge_candidates(
    dim_profiles: dict[str, list[float]],
    threshold: float = 0.85,
) -> list[tuple[str, str]]:
    """Detect dimension pairs whose correlation profiles are highly similar.

    Uses Pearson correlation between profile vectors.
    """
    dims = list(dim_profiles.keys())
    merges = []

    for i in range(len(dims)):
        for j in range(i + 1, len(dims)):
            r = pearson_r(dim_profiles[dims[i]], dim_profiles[dims[j]])
            if r > threshold:
                merges.append((dims[i], dims[j]))

    return merges
```

Note: Add `from collections import defaultdict` at top of `correlation.py` if not already present.

- [ ] **Step 4: Add `execute_split` and `execute_merge` methods to `RubricEvolver`**

Append to `nous/handlers/rubric_evolver.py`:

```python
    async def execute_split(
        self,
        dimension_name: str,
        sub_names: list[str],
        sub_descriptions: list[str],
    ) -> bool:
        """Split a dimension into sub-dimensions. Phase 2.

        Each sub-dimension gets equal share of the parent's weight.
        """
        active = await self._rubric.get_active()
        if not active:
            return False

        dims = list(active.dimensions)
        parent = None
        parent_idx = -1
        for i, d in enumerate(dims):
            if d["name"] == dimension_name:
                parent = d
                parent_idx = i
                break

        if parent is None:
            logger.warning("F024-3b: Cannot split '%s' — not found", dimension_name)
            return False

        if len(dims) - 1 + len(sub_names) > self._settings.rubric_max_dimensions:
            logger.warning("F024-3b: Split would exceed max dimensions")
            return False

        # Create sub-dimensions with equal share of parent weight
        sub_weight = round(parent["weight"] / len(sub_names), 4)
        new_dims = []
        for name, desc in zip(sub_names, sub_descriptions):
            new_dims.append({
                "name": name,
                "weight": sub_weight,
                "description": desc,
                "scoring_criteria": parent.get("scoring_criteria", "1-10 scale"),
                "min_weight": 0.10,
                "max_weight": 0.40,
            })

        # Replace parent with sub-dimensions
        result_dims = dims[:parent_idx] + new_dims + dims[parent_idx + 1:]

        # Normalize weights
        total = sum(d["weight"] for d in result_dims)
        for d in result_dims:
            d["weight"] = round(d["weight"] / total, 4)

        base_version = active.version.split("-")[0]
        parts = base_version.split(".")
        new_version = f"{int(parts[0]) + 1}.0.0"  # Major version bump for structural change

        await self._rubric.create_version(
            new_version=new_version,
            dimensions=result_dims,
            change_reason=f"Phase 2 split: '{dimension_name}' -> {sub_names}",
        )
        return True

    async def execute_merge(
        self,
        dim_a: str,
        dim_b: str,
        merged_name: str,
        merged_description: str,
    ) -> bool:
        """Merge two dimensions into one. Phase 2."""
        active = await self._rubric.get_active()
        if not active:
            return False

        dims = list(active.dimensions)
        a_dim = None
        b_dim = None

        for d in dims:
            if d["name"] == dim_a:
                a_dim = d
            elif d["name"] == dim_b:
                b_dim = d

        if not a_dim or not b_dim:
            logger.warning("F024-3b: Cannot merge — dimensions not found")
            return False

        if len(dims) - 1 < self._settings.rubric_min_dimensions:
            logger.warning("F024-3b: Merge would go below min dimensions")
            return False

        # Merged weight = sum of both
        merged_weight = round(a_dim["weight"] + b_dim["weight"], 4)
        merged = {
            "name": merged_name,
            "weight": min(merged_weight, 0.40),
            "description": merged_description,
            "scoring_criteria": a_dim.get("scoring_criteria", "1-10 scale"),
            "min_weight": 0.10,
            "max_weight": 0.40,
        }

        result_dims = [d for d in dims if d["name"] not in (dim_a, dim_b)]
        result_dims.append(merged)

        # Normalize
        total = sum(d["weight"] for d in result_dims)
        for d in result_dims:
            d["weight"] = round(d["weight"] / total, 4)

        parts = active.version.split(".")
        new_version = f"{int(parts[0]) + 1}.0.0"

        await self._rubric.create_version(
            new_version=new_version,
            dimensions=result_dims,
            change_reason=f"Phase 2 merge: '{dim_a}' + '{dim_b}' -> '{merged_name}'",
        )
        return True
```

- [ ] **Step 5: Run all tests**

Run: `uv run pytest tests/test_rubric_evolver.py tests/test_correlation.py -v`
Expected: All PASSED

- [ ] **Step 6: Commit**

```bash
git add nous/cognitive/correlation.py nous/handlers/rubric_evolver.py tests/test_rubric_evolver.py
git commit -m "feat(f024-3b): add Phase 2 dimension split/merge detection and execution"
```

---

## Chunk E — Phase 3: New Dimension Discovery (Task 10)

### Task 10: Gap Analysis + Dimension Proposals

**Files:**
- Modify: `nous/handlers/rubric_evolver.py` (add gap analysis)
- Modify: `nous/api/rest.py` (add proposal endpoint)
- Test: `tests/test_rubric_evolver.py` (append gap analysis tests)
- Test: `tests/test_rubric_rest.py` (append proposal test)

- [ ] **Step 1: Write failing test for gap analysis**

Append to `tests/test_rubric_evolver.py`:

```python
class TestGapAnalysis:
    @pytest.mark.asyncio
    async def test_find_gap_episodes(self):
        """Episodes where all dims scored high but outcome was poor."""
        from nous.handlers.rubric_evolver import RubricEvolver

        settings = MagicMock()
        settings.rubric_evolution_enabled = True

        evolver = RubricEvolver(
            rubric_manager=MagicMock(),
            db=MagicMock(),
            settings=settings,
            agent_id="test",
        )

        episodes = [
            {"episode_id": "a", "scores": {"Recall": 8, "Tool": 8, "Cal": 8, "Pro": 8}, "signals": ["corrected"]},
            {"episode_id": "b", "scores": {"Recall": 9, "Tool": 7, "Cal": 8, "Pro": 9}, "signals": ["corrected", "reworked"]},
            {"episode_id": "c", "scores": {"Recall": 3, "Tool": 4, "Cal": 5, "Pro": 3}, "signals": ["corrected"]},  # low scores, expected
        ]
        gaps = evolver.find_gap_episodes(episodes, score_threshold=7)
        # Episodes a and b are gaps (high scores, bad outcomes)
        assert len(gaps) == 2
        assert gaps[0]["episode_id"] == "a"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_rubric_evolver.py::TestGapAnalysis -v`
Expected: FAIL (method not found)

- [ ] **Step 3: Add gap analysis to RubricEvolver**

Add to `nous/handlers/rubric_evolver.py`:

```python
    @staticmethod
    def find_gap_episodes(
        episodes: list[dict],
        score_threshold: int = 7,
    ) -> list[dict]:
        """Find episodes where all dimensions scored >= threshold but outcome was poor.

        A "gap episode" means the rubric thought things were fine,
        but the outcome signals say otherwise. These episodes reveal
        unmeasured failure modes — potential new dimensions.
        """
        negative_signals = {"corrected", "reworked"}
        gaps = []

        for ep in episodes:
            scores = ep.get("scores", {})
            signals = set(ep.get("signals", []))

            if not scores or not signals:
                continue

            all_high = all(v >= score_threshold for v in scores.values())
            has_negative = bool(signals & negative_signals)

            if all_high and has_negative:
                gaps.append(ep)

        return gaps
```

- [ ] **Step 4: Add proposal REST endpoint to `nous/api/rest.py`**

```python
async def propose_dimension(request: Request) -> JSONResponse:
    """POST /rubric/propose-dimension — propose a new rubric dimension (Tim approval required)."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    required = ["name", "description", "scoring_criteria", "gap_analysis"]
    missing = [k for k in required if k not in body]
    if missing:
        return JSONResponse({"error": f"Missing fields: {missing}"}, status_code=400)

    # Store as a pending proposal fact for Tim's review
    heart = request.app.state.heart
    settings = request.app.state.settings

    from nous.heart.schemas import FactInput
    fact = FactInput(
        content=f"[RUBRIC PROPOSAL] New dimension: {body['name']}\n\n"
                f"Description: {body['description']}\n"
                f"Scoring: {body['scoring_criteria']}\n"
                f"Evidence: {body['gap_analysis']}\n"
                f"Suggested weight: {body.get('suggested_weight', 0.15)}",
        category="technical",
        subject="rubric_dimension_proposal",
        source="f024_phase3b",
        tags=["rubric", "proposal", "pending_approval"],
    )
    result = await heart.learn_fact(fact)

    return JSONResponse({
        "status": "pending_approval",
        "fact_id": str(result.id) if result else None,
        "message": "Dimension proposal stored. Requires Tim's approval to activate.",
    }, status_code=201)
```

Add route:

```python
        Route("/rubric/propose-dimension", propose_dimension, methods=["POST"]),
        Route("/rubric/proposals", list_proposals),
        Route("/rubric/proposals/{id}/approve", approve_proposal, methods=["POST"]),
```

Also add the list/approve endpoints:

```python
async def list_proposals(request: Request) -> JSONResponse:
    """GET /rubric/proposals — list pending dimension proposals."""
    heart = request.app.state.heart
    settings = request.app.state.settings

    results = await heart.search_facts(
        query="rubric_dimension_proposal",
        agent_id=settings.agent_id,
        limit=20,
        tags=["rubric", "proposal", "pending_approval"],
    )
    return JSONResponse([
        {"id": str(f.id), "content": f.content, "created_at": f.created_at.isoformat() if f.created_at else None}
        for f in results
    ])


async def approve_proposal(request: Request) -> JSONResponse:
    """POST /rubric/proposals/{id}/approve — approve a proposed dimension.

    Reads the proposal fact, extracts dimension info, and adds it to the
    active rubric as a new dimension.
    """
    proposal_id = request.path_params["id"]
    mgr = request.app.state.rubric_manager
    if not mgr:
        return JSONResponse({"error": "Rubric system not enabled"}, status_code=503)

    heart = request.app.state.heart
    from uuid import UUID as _UUID

    # Get the proposal fact
    fact = await heart.get_fact(_UUID(proposal_id))
    if not fact:
        return JSONResponse({"error": "Proposal not found"}, status_code=404)

    # Parse proposal from fact content
    try:
        body = await request.json()
    except Exception:
        body = {}

    # Require explicit dimension details in the approval body
    name = body.get("name")
    description = body.get("description")
    scoring_criteria = body.get("scoring_criteria", "1-10 scale")
    weight = float(body.get("weight", 0.15))

    if not name or not description:
        return JSONResponse({"error": "Approval must include 'name' and 'description'"}, status_code=400)

    active = await mgr.get_active()
    if not active:
        return JSONResponse({"error": "No active rubric"}, status_code=404)

    new_dims = list(active.dimensions) + [{
        "name": name,
        "weight": weight,
        "description": description,
        "scoring_criteria": scoring_criteria,
        "min_weight": 0.10,
        "max_weight": 0.40,
    }]

    # Normalize weights
    total = sum(d["weight"] for d in new_dims)
    for d in new_dims:
        d["weight"] = round(d["weight"] / total, 4)

    parts = active.version.split(".")
    new_version = f"{int(parts[0]) + 1}.0.0"

    try:
        await mgr.create_version(
            new_version=new_version,
            dimensions=new_dims,
            change_reason=f"Phase 3: Added '{name}' dimension (Tim approved)",
        )
        # Remove pending_approval tag from the fact
        return JSONResponse({"status": "approved", "new_version": new_version})
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
```

- [ ] **Step 5: Run all tests**

Run: `uv run pytest tests/test_rubric_evolver.py tests/test_rubric_rest.py -v`
Expected: All PASSED

- [ ] **Step 6: Commit**

```bash
git add nous/handlers/rubric_evolver.py nous/api/rest.py tests/test_rubric_evolver.py tests/test_rubric_rest.py
git commit -m "feat(f024-3b): add Phase 3 gap analysis and dimension proposal endpoint"
```

---

## Chunk F — Safety Guardrails + Final Integration (Task 11)

### Task 11: Anti-Goodhart Guardrail + Rollback Trigger

**Files:**
- Modify: `nous/handlers/rubric_evolver.py` (add safety checks)
- Test: `tests/test_rubric_evolver.py` (append safety tests)

- [ ] **Step 1: Write failing tests for safety guardrails**

Append to `tests/test_rubric_evolver.py`:

```python
class TestAntiGoodhartGuardrail:
    def test_detect_score_inflation(self):
        from nous.handlers.rubric_evolver import RubricEvolver

        evolver = RubricEvolver(
            rubric_manager=MagicMock(),
            db=MagicMock(),
            settings=MagicMock(),
            agent_id="test",
        )

        episodes = [
            {"scores": {"Recall": 9, "Tool": 9, "Cal": 9, "Pro": 9}, "signals": ["corrected"]},
            {"scores": {"Recall": 8, "Tool": 9, "Cal": 8, "Pro": 9}, "signals": ["reworked"]},
            {"scores": {"Recall": 9, "Tool": 8, "Cal": 9, "Pro": 8}, "signals": ["corrected"]},
        ]
        assert evolver.check_goodhart(episodes, score_threshold=8) is True

    def test_no_inflation_when_outcomes_good(self):
        from nous.handlers.rubric_evolver import RubricEvolver

        evolver = RubricEvolver(
            rubric_manager=MagicMock(),
            db=MagicMock(),
            settings=MagicMock(),
            agent_id="test",
        )

        episodes = [
            {"scores": {"Recall": 9, "Tool": 9, "Cal": 9, "Pro": 9}, "signals": ["completed", "praised"]},
            {"scores": {"Recall": 8, "Tool": 9, "Cal": 8, "Pro": 9}, "signals": ["completed"]},
        ]
        assert evolver.check_goodhart(episodes, score_threshold=8) is False


class TestRollbackTrigger:
    def test_detect_outcome_degradation(self):
        from nous.handlers.rubric_evolver import RubricEvolver

        evolver = RubricEvolver(
            rubric_manager=MagicMock(),
            db=MagicMock(),
            settings=MagicMock(),
            agent_id="test",
        )

        # Before: 80% positive, After: 60% positive = 25% degradation
        before = [{"signals": ["completed"]}] * 8 + [{"signals": ["corrected"]}] * 2
        after = [{"signals": ["completed"]}] * 6 + [{"signals": ["corrected"]}] * 4
        assert evolver.check_degradation(before, after, threshold=0.15) is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_rubric_evolver.py::TestAntiGoodhartGuardrail -v`
Expected: FAIL (method not found)

- [ ] **Step 3: Add safety methods to RubricEvolver**

Add to `nous/handlers/rubric_evolver.py`:

```python
    @staticmethod
    def check_goodhart(
        episodes: list[dict],
        score_threshold: int = 8,
    ) -> bool:
        """Anti-Goodhart: detect score inflation without outcome improvement.

        Returns True if most episodes have all scores >= threshold
        but negative outcome signals dominate.
        """
        if not episodes:
            return False

        negative_signals = {"corrected", "reworked"}
        high_score_negative = 0

        for ep in episodes:
            scores = ep.get("scores", {})
            signals = set(ep.get("signals", []))
            if not scores:
                continue

            all_high = all(v >= score_threshold for v in scores.values())
            has_negative = bool(signals & negative_signals)

            if all_high and has_negative:
                high_score_negative += 1

        # Flag if majority of high-scoring episodes have negative outcomes
        return high_score_negative > len(episodes) * 0.5

    @staticmethod
    def check_degradation(
        before: list[dict],
        after: list[dict],
        threshold: float = 0.15,
    ) -> bool:
        """Check if outcomes have degraded by > threshold after rubric change.

        Compares positive signal ratio before and after.
        Returns True if degradation exceeds threshold.
        """
        positive_signals = {"completed", "praised"}

        def positive_ratio(eps: list[dict]) -> float:
            if not eps:
                return 0.0
            pos = sum(1 for ep in eps if set(ep.get("signals", [])) & positive_signals)
            return pos / len(eps)

        before_ratio = positive_ratio(before)
        after_ratio = positive_ratio(after)

        if before_ratio == 0:
            return False

        degradation = (before_ratio - after_ratio) / before_ratio
        return degradation > threshold
```

- [ ] **Step 4: Run all tests**

Run: `uv run pytest tests/test_rubric_evolver.py -v`
Expected: All PASSED

- [ ] **Step 5: Run full test suite to verify no regressions**

Run: `uv run pytest tests/test_rubric_schemas.py tests/test_rubric.py tests/test_correlation.py tests/test_outcome_detector.py tests/test_rubric_evolver.py tests/test_rubric_rest.py -v`
Expected: All PASSED

- [ ] **Step 6: Commit**

```bash
git add nous/handlers/rubric_evolver.py tests/test_rubric_evolver.py
git commit -m "feat(f024-3b): add Anti-Goodhart guardrail and outcome degradation rollback trigger"
```

---

## Summary

| Chunk | Tasks | What It Delivers |
|-------|-------|-----------------|
| A | 1-3 | Schema foundation: migration, ORM models, Pydantic DTOs, config |
| B | 4-6 | Phase 0: RubricManager CRUD, OutcomeDetector handler, REST endpoints |
| C | 7-8 | Phase 1: Correlation engine, weight adjustment via RubricEvolver |
| D | 9 | Phase 2: Dimension split/merge detection and execution |
| E | 10 | Phase 3: Gap analysis, new dimension proposals |
| F | 11 | Safety: Anti-Goodhart guardrail, degradation rollback trigger |

**Total: 11 tasks, ~17 files, 6 new Python modules, 1 migration, 7 REST endpoints**

**Key safety constraints enforced:**
- Dimension count bounded [3, 7]
- Weight changes capped at ±0.05 per cycle
- Max 1 rubric version per week
- All versions immutable (rollback = new version copying old dimensions)
- Anti-Goodhart detection pauses evolution when scores inflate without outcome improvement
- Auto-rollback when outcomes degrade >15%
- New dimensions require Tim's approval (stored as pending proposal)
- No recursive self-evaluation (respects F024 Hard Constraint #1)
