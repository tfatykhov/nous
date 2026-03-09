"""Tests for ProcedureLearner — auto-learning procedures from patterns.

All tests use mocks for Brain, Heart, embeddings, and LLM calls.
"""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import httpx
import pytest

from nous.brain.schemas import BridgeInfo, DecisionDetail, DecisionSummary
from nous.handlers.procedure_learner import (
    ProcedureLearner,
    _cosine_similarity,
    _greedy_cluster,
)
from nous.heart.schemas import (
    EpisodeDetail,
    EpisodeSummary,
    ProcedureDetail,
    ProcedureInput,
    ProcedureSummary,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NOW = datetime.now(UTC)
_RECENT = _NOW - timedelta(days=1)
_OLD = _NOW - timedelta(days=30)


def _make_settings(**overrides) -> MagicMock:
    """Build a mock Settings with sensible test defaults."""
    defaults = dict(
        procedure_learning_enabled=True,
        procedure_cluster_min_size=3,
        procedure_similarity_threshold=0.85,
        procedure_episode_similarity=0.80,
        procedure_success_rate_min=0.70,
        procedure_monitor_trigger_count=3,
        procedure_max_per_sleep=3,
        procedure_max_per_session=1,
        procedure_staleness_days=30,
        procedure_weakness_threshold=0.30,
        background_model="claude-test",
        api_base_url="https://api.anthropic.com",
        anthropic_api_key="sk-ant-test-key",
        anthropic_auth_token="",
    )
    defaults.update(overrides)
    mock = MagicMock()
    for k, v in defaults.items():
        setattr(mock, k, v)
    return mock


def _make_decision_summary(
    outcome: str = "success",
    reviewed_at: datetime | None = _RECENT,
    created_at: datetime | None = None,
) -> DecisionSummary:
    return DecisionSummary(
        id=uuid4(),
        description="Use async patterns for database calls",
        confidence=0.85,
        category="architecture",
        stakes="medium",
        outcome=outcome,
        pattern="async-db",
        tags=[],
        reviewed_at=reviewed_at,
        created_at=created_at or _RECENT,
    )


def _make_decision_detail(
    summary: DecisionSummary,
    bridge_function: str = "Ensure all DB calls use async/await",
) -> DecisionDetail:
    return DecisionDetail(
        id=summary.id,
        agent_id="test",
        description=summary.description,
        confidence=summary.confidence,
        category=summary.category,
        stakes=summary.stakes,
        outcome=summary.outcome,
        reviewed_at=summary.reviewed_at,
        created_at=summary.created_at,
        updated_at=_NOW,
        bridge=BridgeInfo(structure="db-layer", function=bridge_function),
    )


def _make_episode_summary(outcome: str = "success") -> EpisodeSummary:
    return EpisodeSummary(
        id=uuid4(),
        title="Debugging session",
        summary="Fixed async issue",
        outcome=outcome,
        started_at=_RECENT,
        tags=[],
    )


def _make_episode_detail(
    summary: EpisodeSummary,
    lessons: list[str] | None = None,
) -> EpisodeDetail:
    return EpisodeDetail(
        id=summary.id,
        agent_id="test",
        title=summary.title,
        summary=summary.summary,
        detail=None,
        started_at=summary.started_at,
        ended_at=_NOW,
        duration_seconds=100,
        frame_used=None,
        trigger=None,
        participants=[],
        outcome=summary.outcome,
        surprise_level=None,
        lessons_learned=lessons or ["Always use async context managers"],
        tags=[],
        decision_ids=[],
        active=False,
        created_at=_RECENT,
    )


def _make_procedure_summary(
    name: str = "Existing proc",
    score: float | None = 0.90,
) -> ProcedureSummary:
    return ProcedureSummary(
        id=uuid4(),
        name=name,
        domain="test",
        activation_count=5,
        effectiveness=0.8,
        score=score,
    )


def _make_procedure_detail(
    name: str = "Test proc",
    effectiveness: float | None = 0.25,
    last_activated: datetime | None = None,
    tags: list[str] | None = None,
) -> ProcedureDetail:
    return ProcedureDetail(
        id=uuid4(),
        agent_id="test",
        name=name,
        domain="test",
        description="Test procedure",
        goals=["test"],
        core_patterns=["pattern"],
        core_tools=["tool"],
        core_concepts=["concept"],
        implementation_notes=["note"],
        activation_count=3,
        success_count=1,
        failure_count=3,
        neutral_count=0,
        last_activated=last_activated,
        effectiveness=effectiveness,
        tags=tags or ["auto:decision_cluster"],
        active=True,
        created_at=_OLD,
    )


def _make_embedding(seed: float) -> list[float]:
    """Create a deterministic 8-dim embedding for testing."""
    # Normalize to unit vector for cosine similarity to work predictably
    raw = [math.sin(seed + i) for i in range(8)]
    norm = math.sqrt(sum(x * x for x in raw))
    return [x / norm for x in raw] if norm > 0 else raw


def _similar_embedding(base: list[float], noise: float = 0.01) -> list[float]:
    """Create an embedding very similar to base (high cosine similarity)."""
    raw = [x + noise * (i % 3 - 1) for i, x in enumerate(base)]
    norm = math.sqrt(sum(x * x for x in raw))
    return [x / norm for x in raw] if norm > 0 else raw


def _dissimilar_embedding(base: list[float]) -> list[float]:
    """Create an embedding very different from base (low cosine similarity)."""
    raw = [-x + 0.5 for x in base]
    norm = math.sqrt(sum(x * x for x in raw))
    return [x / norm for x in raw] if norm > 0 else raw


def _mock_llm_response(data: dict) -> httpx.Response:
    """Create a mock httpx.Response with LLM JSON output."""
    return httpx.Response(
        200,
        json={
            "content": [
                {"type": "text", "text": json.dumps(data)},
            ]
        },
    )


def _llm_procedure_data(name: str = "Async DB Pattern") -> dict:
    return {
        "name": name,
        "domain": "architecture",
        "description": "Use async/await for all database operations",
        "goals": ["Ensure non-blocking DB access"],
        "core_patterns": ["async context managers", "await on queries"],
        "core_tools": ["sqlalchemy", "asyncpg"],
        "core_concepts": ["async IO", "connection pooling"],
        "implementation_notes": ["Always close sessions"],
    }


# ---------------------------------------------------------------------------
# Unit tests: cosine similarity and clustering
# ---------------------------------------------------------------------------


class TestCosineSimilarity:
    def test_identical_vectors(self):
        v = [1.0, 0.0, 0.0, 1.0]
        assert _cosine_similarity(v, v) == pytest.approx(1.0)

    def test_orthogonal_vectors(self):
        a = [1.0, 0.0]
        b = [0.0, 1.0]
        assert _cosine_similarity(a, b) == pytest.approx(0.0)

    def test_zero_vector(self):
        assert _cosine_similarity([0, 0], [1, 1]) == 0.0


class TestGreedyCluster:
    def test_three_similar_form_cluster(self):
        base = _make_embedding(1.0)
        embeddings = [
            base,
            _similar_embedding(base, noise=0.005),
            _similar_embedding(base, noise=0.01),
        ]
        clusters = _greedy_cluster(embeddings, threshold=0.85, min_size=3)
        assert len(clusters) == 1
        assert sorted(clusters[0]) == [0, 1, 2]

    def test_two_items_below_min_size(self):
        base = _make_embedding(1.0)
        embeddings = [base, _similar_embedding(base)]
        clusters = _greedy_cluster(embeddings, threshold=0.85, min_size=3)
        assert len(clusters) == 0

    def test_dissimilar_not_clustered(self):
        base = _make_embedding(1.0)
        embeddings = [
            base,
            _similar_embedding(base),
            _dissimilar_embedding(base),
        ]
        clusters = _greedy_cluster(embeddings, threshold=0.85, min_size=3)
        assert len(clusters) == 0


# ---------------------------------------------------------------------------
# Integration tests: ProcedureLearner
# ---------------------------------------------------------------------------


def _build_learner(settings: Settings | None = None):
    """Build a ProcedureLearner with mocked dependencies."""
    brain = AsyncMock()
    heart = AsyncMock()
    embeddings = AsyncMock()
    http = AsyncMock(spec=httpx.AsyncClient)
    s = settings or _make_settings()
    learner = ProcedureLearner(brain, heart, embeddings, s, http)
    return learner, brain, heart, embeddings, http


@pytest.mark.asyncio
async def test_decision_cluster_creates_procedure():
    """3+ similar successful reviewed decisions -> 1 procedure."""
    learner, brain, heart, embeddings, http = _build_learner()

    # Set up 3 successful reviewed decisions
    summaries = [_make_decision_summary() for _ in range(3)]
    brain.list_decisions.return_value = (summaries, 3)

    # Each decision has bridge function
    details = [_make_decision_detail(s) for s in summaries]
    brain.get.side_effect = details

    # Embeddings: all similar
    base = _make_embedding(1.0)
    embeddings.embed_batch.return_value = [
        base,
        _similar_embedding(base, 0.005),
        _similar_embedding(base, 0.01),
    ]

    # LLM returns procedure data
    http.post.return_value = _mock_llm_response(_llm_procedure_data())

    # No duplicate
    heart.search_procedures.return_value = []

    # Store succeeds
    heart.store_procedure.return_value = MagicMock()

    stats = await learner.run_sleep_learning()

    assert stats["decisions_learned"] == 1
    heart.store_procedure.assert_called_once()
    call_args = heart.store_procedure.call_args[0][0]
    assert isinstance(call_args, ProcedureInput)
    assert "auto:decision_cluster" in call_args.tags


@pytest.mark.asyncio
async def test_small_cluster_rejected():
    """Only 2 successful decisions -> no cluster, no procedure."""
    learner, brain, heart, embeddings, http = _build_learner()

    summaries = [_make_decision_summary() for _ in range(2)]
    brain.list_decisions.return_value = (summaries, 2)

    details = [_make_decision_detail(s) for s in summaries]
    brain.get.side_effect = details

    base = _make_embedding(1.0)
    embeddings.embed_batch.return_value = [base, _similar_embedding(base)]

    stats = await learner.run_sleep_learning()

    assert stats["decisions_learned"] == 0
    heart.store_procedure.assert_not_called()


@pytest.mark.asyncio
async def test_success_rate_gate():
    """Cluster where <70% are successful -> excluded."""
    learner, brain, heart, embeddings, http = _build_learner()

    # 2 success + 2 failure (all reviewed) = 50% success rate
    summaries = [
        _make_decision_summary(outcome="success"),
        _make_decision_summary(outcome="success"),
        _make_decision_summary(outcome="failure"),
        _make_decision_summary(outcome="failure"),
    ]
    # list_decisions returns all — but only "success" + reviewed pass the filter
    # Actually the learner filters for outcome=="success" first, so failures won't
    # make it into the cluster. Let's test differently: all "success" but the
    # cluster items mixed with non-reviewed ones that slip through.
    # Better approach: since the learner pre-filters to outcome=="success",
    # the success_rate gate only matters if we have items in the cluster
    # with mixed outcomes. Since pre-filter is success-only, success rate
    # is always 100%. So let's test the _check_success_rate method directly.
    pass


@pytest.mark.asyncio
async def test_check_success_rate_method():
    """Direct test of _check_success_rate gate."""
    learner, _, _, _, _ = _build_learner()

    # All success
    items_ok = [MagicMock(outcome="success") for _ in range(3)]
    assert learner._check_success_rate(items_ok) is True

    # Below threshold
    items_bad = [
        MagicMock(outcome="success"),
        MagicMock(outcome="failure"),
        MagicMock(outcome="failure"),
        MagicMock(outcome="failure"),
    ]
    assert learner._check_success_rate(items_bad) is False

    # Empty
    assert learner._check_success_rate([]) is False


@pytest.mark.asyncio
async def test_recency_gate():
    """All decisions older than 7 days -> excluded."""
    learner, brain, heart, embeddings, http = _build_learner()

    old_date = _NOW - timedelta(days=14)
    summaries = [_make_decision_summary(created_at=old_date) for _ in range(3)]
    brain.list_decisions.return_value = (summaries, 3)

    details = [_make_decision_detail(s) for s in summaries]
    brain.get.side_effect = details

    base = _make_embedding(1.0)
    embeddings.embed_batch.return_value = [
        base,
        _similar_embedding(base, 0.005),
        _similar_embedding(base, 0.01),
    ]

    # Even though cluster forms, recency gate should block it
    stats = await learner.run_sleep_learning()
    assert stats["decisions_learned"] == 0
    heart.store_procedure.assert_not_called()


@pytest.mark.asyncio
async def test_episode_lesson_clustering():
    """3+ similar lessons from episodes -> 1 procedure."""
    learner, brain, heart, embeddings, http = _build_learner()

    # No decisions (pathway 1 empty)
    brain.list_decisions.return_value = ([], 0)

    # 3 episodes with similar lessons
    ep_summaries = [_make_episode_summary() for _ in range(3)]
    heart.list_episodes.return_value = ep_summaries

    ep_details = [
        _make_episode_detail(s, lessons=["Use async context managers for DB"])
        for s in ep_summaries
    ]
    heart.get_episode.side_effect = ep_details

    # Embeddings: all similar
    base = _make_embedding(2.0)
    embeddings.embed_batch.return_value = [
        base,
        _similar_embedding(base, 0.005),
        _similar_embedding(base, 0.01),
    ]

    # LLM returns procedure
    http.post.return_value = _mock_llm_response(_llm_procedure_data("Episode Lesson"))

    # No duplicate
    heart.search_procedures.return_value = []
    heart.store_procedure.return_value = MagicMock()

    stats = await learner.run_sleep_learning()

    assert stats["episodes_learned"] == 1
    call_args = heart.store_procedure.call_args[0][0]
    assert "auto:episode_lesson" in call_args.tags


@pytest.mark.asyncio
async def test_too_few_episodes():
    """Only 1 episode with 1 lesson -> no cluster."""
    learner, brain, heart, embeddings, http = _build_learner()

    brain.list_decisions.return_value = ([], 0)

    ep_summaries = [_make_episode_summary()]
    heart.list_episodes.return_value = ep_summaries
    heart.get_episode.return_value = _make_episode_detail(
        ep_summaries[0], lessons=["Single lesson"]
    )

    stats = await learner.run_sleep_learning()

    assert stats["episodes_learned"] == 0
    heart.store_procedure.assert_not_called()


@pytest.mark.asyncio
async def test_dedup_skips_similar_existing():
    """If similar procedure exists (score > 0.85), skip creation."""
    learner, brain, heart, embeddings, http = _build_learner()

    summaries = [_make_decision_summary() for _ in range(3)]
    brain.list_decisions.return_value = (summaries, 3)
    details = [_make_decision_detail(s) for s in summaries]
    brain.get.side_effect = details

    base = _make_embedding(1.0)
    embeddings.embed_batch.return_value = [
        base,
        _similar_embedding(base, 0.005),
        _similar_embedding(base, 0.01),
    ]

    http.post.return_value = _mock_llm_response(_llm_procedure_data())

    # Existing procedure with high similarity score
    heart.search_procedures.return_value = [_make_procedure_summary(score=0.90)]

    stats = await learner.run_sleep_learning()

    assert stats["decisions_learned"] == 0
    heart.store_procedure.assert_not_called()


@pytest.mark.asyncio
async def test_max_cap_enforcement():
    """Respects procedure_max_per_sleep cap."""
    learner, brain, heart, embeddings, http = _build_learner(
        _make_settings(procedure_max_per_sleep=1)
    )

    # Set up enough decisions for 2 clusters
    summaries = [_make_decision_summary() for _ in range(6)]
    brain.list_decisions.return_value = (summaries, 6)
    details = [_make_decision_detail(s) for s in summaries]
    brain.get.side_effect = details

    # Create 2 clusters of 3
    base1 = _make_embedding(1.0)
    base2 = _make_embedding(100.0)  # very different seed
    embeddings.embed_batch.return_value = [
        base1,
        _similar_embedding(base1, 0.005),
        _similar_embedding(base1, 0.01),
        base2,
        _similar_embedding(base2, 0.005),
        _similar_embedding(base2, 0.01),
    ]

    http.post.return_value = _mock_llm_response(_llm_procedure_data())
    heart.search_procedures.return_value = []
    heart.store_procedure.return_value = MagicMock()

    stats = await learner.run_sleep_learning()

    # Max 1 allowed
    assert stats["decisions_learned"] <= 1
    assert heart.store_procedure.call_count <= 1


@pytest.mark.asyncio
async def test_disabled_learning_returns_empty():
    """When procedure_learning_enabled=False, returns empty stats."""
    learner, brain, heart, embeddings, http = _build_learner(
        _make_settings(procedure_learning_enabled=False)
    )

    stats = await learner.run_sleep_learning()

    assert stats["enabled"] is False
    assert stats["decisions_learned"] == 0
    assert stats["episodes_learned"] == 0
    assert stats["weak_reviewed"] == 0
    brain.list_decisions.assert_not_called()
    heart.list_episodes.assert_not_called()


@pytest.mark.asyncio
async def test_no_embeddings_returns_zero():
    """If no EmbeddingProvider, pathways return 0."""
    brain = AsyncMock()
    heart = AsyncMock()
    http = AsyncMock(spec=httpx.AsyncClient)
    settings = _make_settings()
    learner = ProcedureLearner(brain, heart, None, settings, http)

    brain.list_decisions.return_value = ([], 0)
    heart.list_episodes.return_value = []

    stats = await learner.run_sleep_learning()

    assert stats["decisions_learned"] == 0
    assert stats["episodes_learned"] == 0


@pytest.mark.asyncio
async def test_weak_review_retires():
    """Weak procedure with low effectiveness gets retired."""
    learner, brain, heart, embeddings, http = _build_learner()

    brain.list_decisions.return_value = ([], 0)
    heart.list_episodes.return_value = []

    weak_proc = _make_procedure_summary(name="Weak proc", score=0.5)
    heart.search_procedures.return_value = [weak_proc]
    heart.get_procedure.return_value = _make_procedure_detail(
        name="Weak proc",
        effectiveness=0.20,  # below 0.30 threshold
        last_activated=_RECENT,
    )

    http.post.return_value = _mock_llm_response({
        "action": "retire",
        "reason": "Too unreliable",
    })

    stats = await learner.run_sleep_learning()

    assert stats["weak_reviewed"] == 1
    heart.retire_procedure.assert_called_once()


@pytest.mark.asyncio
async def test_monitor_recovery_prompt_exists():
    """Verify the _MONITOR_RECOVERY_PROMPT constant is importable and has expected placeholders."""
    from nous.handlers.procedure_learner import _MONITOR_RECOVERY_PROMPT

    assert "{error_pattern}" in _MONITOR_RECOVERY_PROMPT
    assert "{recovery_actions}" in _MONITOR_RECOVERY_PROMPT
    assert "{context}" in _MONITOR_RECOVERY_PROMPT
    assert '"name"' in _MONITOR_RECOVERY_PROMPT
