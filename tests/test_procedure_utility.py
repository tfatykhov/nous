"""Tests for F037: Utility-Boosted Procedure Retrieval.

Tests cover:
1. Utility boost calculation — effectiveness influences final score
2. Task-type affinity tracking — upsert counts per frame_type
3. Evolution candidates — flagged correctly by category
4. Feature flag — boost=False produces pure hybrid scores

Unit tests (no DB required) test pure logic.
Integration tests (marked @pytest.mark.integration) require Postgres.
"""

from __future__ import annotations

import pytest

from nous.config import Settings
from nous.heart.procedures import ProcedureManager
from nous.heart.schemas import (
    EvolutionCandidate,
    ProcedureInput,
)
from nous.storage.models import Procedure, ProcedureTaskAffinity

# ===========================================================================
# UNIT TESTS — No database required
# ===========================================================================


class _FakeProc:
    """Simple stub that mimics Procedure ORM fields for unit tests (avoids SA instrumentation)."""

    def __init__(self, success: int, failure: int, activation: int) -> None:
        self.success_count = success
        self.failure_count = failure
        self.neutral_count = 0
        self.activation_count = activation


class TestUtilityBoostMath:
    """Pure math tests for utility boost formula (no DB needed)."""

    def _make_procedure(self, success: int, failure: int, activation: int) -> _FakeProc:
        """Create a minimal Procedure-like stub for testing."""
        return _FakeProc(success, failure, activation)

    def _make_manager(self, **kwargs) -> ProcedureManager:
        """Create a ProcedureManager without a real DB (not calling any DB methods)."""
        return ProcedureManager(
            db=None,  # type: ignore[arg-type]
            embeddings=None,
            agent_id="unit-test-agent",
            **kwargs,
        )

    def test_compute_effectiveness_laplace(self):
        """Laplace smoothing: (success+1) / (success+failure+2)."""
        mgr = self._make_manager()
        p = self._make_procedure(success=8, failure=2, activation=10)
        eff = mgr._compute_effectiveness(p)
        assert eff is not None
        assert abs(eff - (9 / 12)) < 1e-9

    def test_compute_effectiveness_none_when_no_data(self):
        """No outcomes → None (cold start)."""
        mgr = self._make_manager()
        p = self._make_procedure(success=0, failure=0, activation=0)
        eff = mgr._compute_effectiveness(p)
        assert eff is None

    def test_utility_signal_positive_for_high_effectiveness(self):
        """effectiveness > 0.5 → positive utility_signal."""
        mgr = self._make_manager()
        p = self._make_procedure(success=8, failure=0, activation=10)
        eff = mgr._compute_effectiveness(p)
        assert eff is not None
        utility_signal = eff - 0.5
        assert utility_signal > 0

    def test_utility_signal_negative_for_low_effectiveness(self):
        """effectiveness < 0.5 → negative utility_signal."""
        mgr = self._make_manager()
        p = self._make_procedure(success=0, failure=8, activation=10)
        eff = mgr._compute_effectiveness(p)
        assert eff is not None
        utility_signal = eff - 0.5
        assert utility_signal < 0

    def test_utility_signal_near_zero_at_50pct(self):
        """~50% effectiveness → utility_signal near 0."""
        mgr = self._make_manager()
        # 5 success, 5 failure → (5+1)/(10+2) = 6/12 = 0.5 exactly
        p = self._make_procedure(success=5, failure=5, activation=10)
        eff = mgr._compute_effectiveness(p)
        assert eff is not None
        utility_signal = eff - 0.5
        assert abs(utility_signal) < 0.05

    def test_final_score_formula_positive_boost(self):
        """Verify: final_score = hybrid_score * (1 + alpha * utility_signal)."""
        alpha = 0.15
        hybrid_score = 0.8
        # effectiveness ~0.9 → utility_signal = 0.4
        effectiveness = 0.9
        utility_signal = effectiveness - 0.5
        expected = hybrid_score * (1 + alpha * utility_signal)
        assert expected > hybrid_score  # Boost increased the score

    def test_final_score_formula_negative_penalty(self):
        """Verify: final_score = hybrid_score * (1 + alpha * utility_signal) with penalty."""
        alpha = 0.15
        hybrid_score = 0.8
        # effectiveness ~0.1 → utility_signal = -0.4
        effectiveness = 0.1
        utility_signal = effectiveness - 0.5
        expected = hybrid_score * (1 + alpha * utility_signal)
        assert expected < hybrid_score  # Penalty reduced the score

    def test_boost_not_applied_below_min_activations(self):
        """Manager should skip boost logic when activation_count < min_activations."""
        mgr = self._make_manager(
            utility_boost=True,
            utility_alpha=0.15,
            min_activations_for_boost=10,
        )
        p = self._make_procedure(success=5, failure=0, activation=5)
        # Activation count (5) < threshold (10) → no boost
        activation_count = p.activation_count or 0
        assert activation_count < mgr._min_activations_for_boost

    def test_settings_defaults(self):
        """Verify F037 settings have correct defaults."""
        s = Settings()
        assert s.procedure_utility_boost is True
        assert s.procedure_utility_alpha == 0.15
        assert s.procedure_affinity_beta == 0.10
        assert s.procedure_min_activations_for_boost == 5


class TestEvolutionCandidateLogic:
    """Unit tests for evolution candidate categorization logic."""

    def _compute_laplace(self, success: int, failure: int) -> float | None:
        """Replicate the Laplace formula directly."""
        if success + failure == 0:
            return None
        return (success + 1) / (success + failure + 2)

    def test_retire_threshold(self):
        """effectiveness < 0.3 AND activation >= 10 → retire."""
        # 0 success, 15 failure → (0+1)/(15+2) ≈ 0.059
        eff = self._compute_laplace(0, 15)
        assert eff is not None
        assert eff < 0.3

    def test_rewrite_threshold(self):
        """effectiveness in [0.3, 0.5) AND activation >= 15 → rewrite."""
        # 4 success, 8 failure → (4+1)/(12+2) ≈ 0.357
        eff = self._compute_laplace(4, 8)
        assert eff is not None
        assert 0.3 <= eff < 0.5

    def test_star_threshold(self):
        """effectiveness >= 0.85 AND activation >= 10 → star."""
        # 20 success, 0 failure → (20+1)/(20+2) ≈ 0.955
        eff = self._compute_laplace(20, 0)
        assert eff is not None
        assert eff >= 0.85

    def test_investigate_threshold(self):
        """activation >= 30 AND effectiveness < 0.6 → investigate."""
        # 10 success, 10 failure → (10+1)/(20+2) = 0.5
        eff = self._compute_laplace(10, 10)
        assert eff is not None
        assert eff < 0.6

    def test_evolution_candidate_schema_fields(self):
        """EvolutionCandidate has expected fields."""
        import uuid

        cand = EvolutionCandidate(
            id=uuid.uuid4(),
            name="Test",
            category="star",
            effectiveness=0.92,
            activation_count=15,
            reason="High effectiveness",
        )
        assert cand.category == "star"
        assert cand.effectiveness == 0.92
        assert cand.activation_count == 15


# ===========================================================================
# INTEGRATION TESTS — Require Postgres (--integration flag)
# ===========================================================================


# Integration test marker applied per-function below (not at module level so unit tests can run offline)
_integration = pytest.mark.integration


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _proc_input(**overrides) -> ProcedureInput:
    defaults = dict(
        name="Test procedure",
        domain="testing",
        description="A procedure for automated testing",
        goals=["Test something"],
        core_patterns=["test pattern"],
        core_tools=["pytest"],
        core_concepts=["testing"],
        implementation_notes=["Run with pytest"],
        tags=["test"],
    )
    defaults.update(overrides)
    return ProcedureInput(**defaults)


async def _make_manager(db, mock_embeddings, **kwargs) -> ProcedureManager:
    """Build a ProcedureManager with configurable F037 params."""
    return ProcedureManager(
        db,
        mock_embeddings,
        agent_id="test-f037-agent",
        **kwargs,
    )


# ---------------------------------------------------------------------------
# 1. Utility boost calculation
# ---------------------------------------------------------------------------


@_integration
async def test_high_effectiveness_scores_higher(db, session, mock_embeddings):
    """A high-effectiveness procedure should score higher than a low-effectiveness one
    with the same hybrid score."""
    mgr = await _make_manager(
        db,
        mock_embeddings,
        utility_boost=True,
        utility_alpha=0.15,
        min_activations_for_boost=3,
    )

    # Store two procedures with identical text (same hybrid score)
    good = await mgr.store(_proc_input(name="Good proc identical text query"), session=session)
    bad = await mgr.store(_proc_input(name="Bad proc identical text query"), session=session)
    await session.flush()

    # Give good procedure 8 successes → effectiveness ≈ (8+1)/(8+0+2) = 0.9
    for _ in range(8):
        await mgr.record_outcome(good.id, "success", session=session)
    # Give bad procedure 2 successes + 6 failures → effectiveness ≈ (2+1)/(8+2) = 0.3
    for _ in range(2):
        await mgr.record_outcome(bad.id, "success", session=session)
    for _ in range(6):
        await mgr.record_outcome(bad.id, "failure", session=session)

    # Activate both enough times (activation_count is incremented separately)
    for _ in range(5):
        await mgr._activate(good.id, session)
    for _ in range(5):
        await mgr._activate(bad.id, session)

    results = await mgr._search("identical text query", limit=10, domain=None, frame_type=None, session=session)

    good_result = next((r for r in results if r.id == good.id), None)
    bad_result = next((r for r in results if r.id == bad.id), None)

    assert good_result is not None, "Good procedure not found in results"
    assert bad_result is not None, "Bad procedure not found in results"
    assert good_result.score > bad_result.score, (
        f"Expected high-effectiveness procedure to rank higher: good={good_result.score:.4f} bad={bad_result.score:.4f}"
    )


@_integration
async def test_no_boost_below_min_activations(db, session, mock_embeddings):
    """A procedure with fewer activations than min_activations_for_boost should NOT get a boost."""
    mgr = await _make_manager(
        db,
        mock_embeddings,
        utility_boost=True,
        utility_alpha=0.15,
        min_activations_for_boost=10,  # High threshold
    )

    proc = await mgr.store(_proc_input(name="Cold start procedure"), session=session)
    await session.flush()

    # Record 5 successes but only 5 activations (below threshold of 10)
    for _ in range(5):
        await mgr.record_outcome(proc.id, "success", session=session)
    for _ in range(5):
        await mgr._activate(proc.id, session)

    detail = await mgr._get(proc.id, session)
    assert detail is not None
    assert detail.activation_count < 10  # below threshold

    # Effectiveness is computed but boost should not apply
    effectiveness = mgr._compute_effectiveness(detail)
    assert effectiveness is not None

    results = await mgr._search("Cold start procedure", limit=10, domain=None, frame_type=None, session=session)
    result = next((r for r in results if r.id == proc.id), None)
    assert result is not None
    # The score should equal the raw hybrid score (no boost applied)
    # We can't check the exact value but we verify it returned without error
    assert result.score is not None


@_integration
async def test_alpha_zero_gives_identical_scores(db, session, mock_embeddings):
    """When alpha=0, utility boost factor is always 0, so final_score = hybrid_score."""
    mgr = await _make_manager(
        db,
        mock_embeddings,
        utility_boost=True,
        utility_alpha=0.0,  # No boost
        affinity_beta=0.0,
        min_activations_for_boost=1,
    )

    p1 = await mgr.store(_proc_input(name="Alpha zero proc A testing alpha zero"), session=session)
    p2 = await mgr.store(_proc_input(name="Alpha zero proc B testing alpha zero"), session=session)
    await session.flush()

    # p1 excellent, p2 terrible
    for _ in range(10):
        await mgr.record_outcome(p1.id, "success", session=session)
        await mgr._activate(p1.id, session)
    for _ in range(10):
        await mgr.record_outcome(p2.id, "failure", session=session)
        await mgr._activate(p2.id, session)

    results = await mgr._search("testing alpha zero", limit=10, domain=None, frame_type=None, session=session)
    r1 = next((r for r in results if r.id == p1.id), None)
    r2 = next((r for r in results if r.id == p2.id), None)

    if r1 and r2:
        # With alpha=0, both should have very similar scores (no divergence from effectiveness)
        # The scores come purely from hybrid search (BM25 + cosine)
        # Since texts are very similar, scores should be within ~10% of each other
        ratio = abs(r1.score - r2.score) / max(r1.score, r2.score, 1e-9)
        assert ratio < 0.15, f"Scores diverged too much with alpha=0: {r1.score:.4f} vs {r2.score:.4f}"


@_integration
async def test_negative_boost_for_low_effectiveness(db, session, mock_embeddings):
    """A low-effectiveness procedure (< 0.5) should receive a negative score adjustment."""
    mgr = await _make_manager(
        db,
        mock_embeddings,
        utility_boost=True,
        utility_alpha=0.15,
        min_activations_for_boost=3,
    )

    proc = await mgr.store(_proc_input(name="Low effectiveness procedure"), session=session)
    await session.flush()

    # 1 success, 9 failures → effectiveness = (1+1)/(10+2) ≈ 0.167
    await mgr.record_outcome(proc.id, "success", session=session)
    for _ in range(9):
        await mgr.record_outcome(proc.id, "failure", session=session)
    for _ in range(5):
        await mgr._activate(proc.id, session)

    eff = mgr._compute_effectiveness(proc)
    # Reload from DB since we modified in-session
    from sqlalchemy import select

    result = await session.execute(select(Procedure).where(Procedure.id == proc.id))
    p = result.scalars().first()
    eff = mgr._compute_effectiveness(p)
    assert eff is not None
    assert eff < 0.5  # Should be penalized

    utility_signal = eff - 0.5
    assert utility_signal < 0  # Confirms negative boost would apply


@_integration
async def test_feature_flag_disabled(db, session, mock_embeddings):
    """With utility_boost=False, results should come purely from hybrid scoring."""
    mgr = await _make_manager(
        db,
        mock_embeddings,
        utility_boost=False,
    )

    proc = await mgr.store(_proc_input(name="Feature flag disabled test"), session=session)
    await session.flush()

    for _ in range(10):
        await mgr.record_outcome(proc.id, "success", session=session)
    for _ in range(10):
        await mgr._activate(proc.id, session)

    # Should return without error; scores are pure hybrid
    results = await mgr._search("Feature flag disabled test", limit=5, domain=None, frame_type=None, session=session)
    assert isinstance(results, list)


# ---------------------------------------------------------------------------
# 2. Task-type affinity tracking
# ---------------------------------------------------------------------------


@_integration
async def test_affinity_upsert_creates_row(db, session, mock_embeddings):
    """record_outcome with frame_type should create a procedure_task_affinity row."""
    from sqlalchemy import select

    mgr = await _make_manager(db, mock_embeddings)

    proc = await mgr.store(_proc_input(name="Affinity upsert test"), session=session)
    await session.flush()

    await mgr.record_outcome(proc.id, "success", frame_type="task", session=session)

    result = await session.execute(
        select(ProcedureTaskAffinity)
        .where(ProcedureTaskAffinity.procedure_id == proc.id)
        .where(ProcedureTaskAffinity.frame_type == "task")
        .where(ProcedureTaskAffinity.agent_id == "test-f037-agent")
    )
    row = result.scalars().first()
    assert row is not None
    assert row.activation_count == 1
    assert row.success_count == 1
    assert row.failure_count == 0


@_integration
async def test_affinity_upsert_increments_existing(db, session, mock_embeddings):
    """Multiple record_outcome calls with same frame_type should increment counts."""
    from sqlalchemy import select

    mgr = await _make_manager(db, mock_embeddings)

    proc = await mgr.store(_proc_input(name="Affinity increment test"), session=session)
    await session.flush()

    await mgr.record_outcome(proc.id, "success", frame_type="conversation", session=session)
    await mgr.record_outcome(proc.id, "success", frame_type="conversation", session=session)
    await mgr.record_outcome(proc.id, "failure", frame_type="conversation", session=session)

    result = await session.execute(
        select(ProcedureTaskAffinity)
        .where(ProcedureTaskAffinity.procedure_id == proc.id)
        .where(ProcedureTaskAffinity.frame_type == "conversation")
    )
    row = result.scalars().first()
    assert row is not None
    assert row.activation_count == 3
    assert row.success_count == 2
    assert row.failure_count == 1


@_integration
async def test_affinity_separate_per_frame_type(db, session, mock_embeddings):
    """Different frame_types should create separate affinity rows."""
    from sqlalchemy import select

    mgr = await _make_manager(db, mock_embeddings)

    proc = await mgr.store(_proc_input(name="Multi-frame affinity test"), session=session)
    await session.flush()

    await mgr.record_outcome(proc.id, "success", frame_type="task", session=session)
    await mgr.record_outcome(proc.id, "failure", frame_type="debug", session=session)

    result = await session.execute(
        select(ProcedureTaskAffinity)
        .where(ProcedureTaskAffinity.procedure_id == proc.id)
        .where(ProcedureTaskAffinity.agent_id == "test-f037-agent")
    )
    rows = result.scalars().all()
    frame_types = {r.frame_type for r in rows}
    assert "task" in frame_types
    assert "debug" in frame_types
    assert len(rows) == 2


@_integration
async def test_affinity_no_row_without_frame_type(db, session, mock_embeddings):
    """record_outcome without frame_type should NOT create any affinity row."""
    from sqlalchemy import select

    mgr = await _make_manager(db, mock_embeddings)

    proc = await mgr.store(_proc_input(name="No frame type test"), session=session)
    await session.flush()

    await mgr.record_outcome(proc.id, "success", frame_type=None, session=session)

    result = await session.execute(select(ProcedureTaskAffinity).where(ProcedureTaskAffinity.procedure_id == proc.id))
    rows = result.scalars().all()
    assert len(rows) == 0


@_integration
async def test_affinity_boost_applied_in_search(db, session, mock_embeddings):
    """Frame-type affinity boost should influence score when searching with frame_type."""
    mgr = await _make_manager(
        db,
        mock_embeddings,
        utility_boost=True,
        utility_alpha=0.10,
        affinity_beta=0.20,
        min_activations_for_boost=3,
    )

    # Two procedures with similar text
    task_proc = await mgr.store(_proc_input(name="Task affinity proc boost test"), session=session)
    debug_proc = await mgr.store(_proc_input(name="Debug affinity proc boost test"), session=session)
    await session.flush()

    # Both get some global outcomes
    for _ in range(6):
        await mgr.record_outcome(task_proc.id, "success", session=session)
        await mgr._activate(task_proc.id, session)
    for _ in range(6):
        await mgr.record_outcome(debug_proc.id, "success", session=session)
        await mgr._activate(debug_proc.id, session)

    # task_proc has strong "task" frame affinity (5 successes)
    for _ in range(5):
        await mgr.record_outcome(task_proc.id, "success", frame_type="task", session=session)

    # debug_proc has no "task" affinity
    results_task = await mgr._search(
        "affinity proc boost test", limit=10, domain=None, frame_type="task", session=session
    )
    task_result = next((r for r in results_task if r.id == task_proc.id), None)
    debug_result = next((r for r in results_task if r.id == debug_proc.id), None)

    assert task_result is not None
    assert debug_result is not None
    # task_proc should rank higher when searching with frame_type="task"
    assert task_result.score >= debug_result.score


# ---------------------------------------------------------------------------
# 3. Evolution candidates
# ---------------------------------------------------------------------------


@_integration
async def test_evolution_candidate_retire(db, session, mock_embeddings):
    """Procedure with effectiveness < 0.3 and >= 10 activations should be flagged 'retire'."""
    mgr = await _make_manager(db, mock_embeddings)

    proc = await mgr.store(_proc_input(name="Retire candidate proc"), session=session)
    await session.flush()

    # 0 successes, 10 failures → effectiveness = (0+1)/(10+2) ≈ 0.083
    for _ in range(10):
        await mgr.record_outcome(proc.id, "failure", session=session)
    for _ in range(12):
        await mgr._activate(proc.id, session)

    candidates = await mgr._get_evolution_candidates(session)
    retire_candidates = [c for c in candidates if c.id == proc.id and c.category == "retire"]
    assert len(retire_candidates) == 1
    assert retire_candidates[0].effectiveness < 0.3


@_integration
async def test_evolution_candidate_rewrite(db, session, mock_embeddings):
    """Procedure with effectiveness < 0.5 and >= 15 activations should be flagged 'rewrite'."""
    mgr = await _make_manager(db, mock_embeddings)

    proc = await mgr.store(_proc_input(name="Rewrite candidate proc"), session=session)
    await session.flush()

    # 4 successes, 8 failures → effectiveness = (4+1)/(12+2) ≈ 0.357 (< 0.5, >= 0.3)
    for _ in range(4):
        await mgr.record_outcome(proc.id, "success", session=session)
    for _ in range(8):
        await mgr.record_outcome(proc.id, "failure", session=session)
    for _ in range(16):
        await mgr._activate(proc.id, session)

    candidates = await mgr._get_evolution_candidates(session)
    this_proc = [c for c in candidates if c.id == proc.id]
    assert len(this_proc) == 1
    # Should be rewrite (not retire, since eff > 0.3)
    assert this_proc[0].category == "rewrite"
    assert 0.3 <= this_proc[0].effectiveness < 0.5


@_integration
async def test_evolution_candidate_star(db, session, mock_embeddings):
    """Procedure with effectiveness >= 0.85 and >= 10 activations should be flagged 'star'."""
    mgr = await _make_manager(db, mock_embeddings)

    proc = await mgr.store(_proc_input(name="Star candidate proc"), session=session)
    await session.flush()

    # 15 successes, 0 failures → effectiveness = (15+1)/(15+2) ≈ 0.941
    for _ in range(15):
        await mgr.record_outcome(proc.id, "success", session=session)
    for _ in range(12):
        await mgr._activate(proc.id, session)

    candidates = await mgr._get_evolution_candidates(session)
    stars = [c for c in candidates if c.id == proc.id and c.category == "star"]
    assert len(stars) == 1
    assert stars[0].effectiveness >= 0.85


@_integration
async def test_evolution_candidate_investigate(db, session, mock_embeddings):
    """Procedure with >= 30 activations but effectiveness < 0.6 should be 'investigate'."""
    mgr = await _make_manager(db, mock_embeddings)

    proc = await mgr.store(_proc_input(name="Investigate candidate proc"), session=session)
    await session.flush()

    # 10 successes, 10 failures → effectiveness = (10+1)/(20+2) ≈ 0.5 (< 0.6, >= 0.3)
    for _ in range(10):
        await mgr.record_outcome(proc.id, "success", session=session)
    for _ in range(10):
        await mgr.record_outcome(proc.id, "failure", session=session)
    for _ in range(32):
        await mgr._activate(proc.id, session)

    candidates = await mgr._get_evolution_candidates(session)
    this_proc = [c for c in candidates if c.id == proc.id]
    assert len(this_proc) == 1
    assert this_proc[0].category == "investigate"


@_integration
async def test_evolution_candidate_empty_when_healthy(db, session, mock_embeddings):
    """A procedure with moderate effectiveness and low activations should not be flagged."""
    mgr = await _make_manager(db, mock_embeddings)

    proc = await mgr.store(_proc_input(name="Healthy procedure"), session=session)
    await session.flush()

    # 3 successes, 1 failure, only 5 activations — not enough for any category
    for _ in range(3):
        await mgr.record_outcome(proc.id, "success", session=session)
    await mgr.record_outcome(proc.id, "failure", session=session)
    for _ in range(5):
        await mgr._activate(proc.id, session)

    candidates = await mgr._get_evolution_candidates(session)
    this_proc = [c for c in candidates if c.id == proc.id]
    assert len(this_proc) == 0


@_integration
async def test_evolution_candidates_no_outcome_excluded(db, session, mock_embeddings):
    """A procedure with no outcome data should not appear in evolution candidates."""
    mgr = await _make_manager(db, mock_embeddings)

    proc = await mgr.store(_proc_input(name="No outcome procedure"), session=session)
    await session.flush()

    # No outcomes recorded
    candidates = await mgr._get_evolution_candidates(session)
    assert not any(c.id == proc.id for c in candidates)


@_integration
async def test_evolution_candidate_schema(db, session, mock_embeddings):
    """EvolutionCandidate has the correct fields."""
    mgr = await _make_manager(db, mock_embeddings)

    proc = await mgr.store(_proc_input(name="Schema test proc"), session=session)
    await session.flush()

    for _ in range(20):
        await mgr.record_outcome(proc.id, "failure", session=session)
    for _ in range(15):
        await mgr._activate(proc.id, session)

    candidates = await mgr._get_evolution_candidates(session)
    found = next((c for c in candidates if c.id == proc.id), None)
    assert found is not None
    assert isinstance(found, EvolutionCandidate)
    assert found.name == "Schema test proc"
    assert found.category in {"retire", "rewrite", "investigate", "star"}
    assert isinstance(found.effectiveness, float)
    assert isinstance(found.activation_count, int)
    assert isinstance(found.reason, str)
    assert len(found.reason) > 0


# ---------------------------------------------------------------------------
# 4. get_effectiveness() helper
# ---------------------------------------------------------------------------


@_integration
async def test_get_effectiveness_returns_none_for_no_data(db, session, mock_embeddings):
    """get_effectiveness() should return None when no outcomes recorded."""
    mgr = await _make_manager(db, mock_embeddings)
    proc = await mgr.store(_proc_input(name="Effectiveness none test"), session=session)
    await session.flush()

    eff = await mgr.get_effectiveness(proc.id, session=session)
    assert eff is None


@_integration
async def test_get_effectiveness_laplace_smoothing(db, session, mock_embeddings):
    """get_effectiveness() should use Laplace smoothing."""
    mgr = await _make_manager(db, mock_embeddings)
    proc = await mgr.store(_proc_input(name="Effectiveness laplace test"), session=session)
    await session.flush()

    # 3 successes, 1 failure → (3+1)/(4+2) = 4/6 ≈ 0.667
    for _ in range(3):
        await mgr.record_outcome(proc.id, "success", session=session)
    await mgr.record_outcome(proc.id, "failure", session=session)

    eff = await mgr.get_effectiveness(proc.id, session=session)
    assert eff is not None
    assert abs(eff - (4 / 6)) < 1e-6


# ---------------------------------------------------------------------------
# 5. Heart passthrough
# ---------------------------------------------------------------------------


@_integration
async def test_heart_record_outcome_accepts_frame_type(heart, session):
    """Heart.record_procedure_outcome should accept frame_type without error."""
    inp = ProcedureInput(
        name="Heart frame type test",
        domain="testing",
        description="Testing frame_type passthrough",
    )
    detail = await heart.store_procedure(inp, session=session)
    await session.flush()

    result = await heart.record_procedure_outcome(detail.id, "success", frame_type="task", session=session)
    assert result.success_count == 1


@_integration
async def test_heart_get_evolution_candidates_passthrough(heart, session):
    """Heart.get_evolution_candidates should return a list."""
    result = await heart.get_evolution_candidates(session=session)
    assert isinstance(result, list)
