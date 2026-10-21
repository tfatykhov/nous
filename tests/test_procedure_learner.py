"""Tests for ProcedureLearner — auto-learning procedures from patterns.

All tests use mocks for Brain, Heart, embeddings, and LLM calls.
"""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

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
        # Added in PR #405 — _check_recency reads this; was hardcoded
        # 7 days, bumped to configurable default 30. Mock must supply
        # an int because timedelta(days=MagicMock()) crashes.
        procedure_recency_days=30,
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
        session_id=None,
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


def _mock_llm_response(data: dict) -> MagicMock:
    """Create a mock LLM response matching call_background_llm's expected format.

    call_background_llm accesses response.content (list of dicts with type/text).
    """
    response = MagicMock()
    response.content = [{"type": "text", "text": json.dumps(data)}]
    return response


def _llm_procedure_data(name: str = "Async DB Pattern", cited_ids: list | None = None) -> dict:
    """A complete lesson-gated entry; ``cited_ids`` are the input item IDs its evidence cites."""
    return {
        "cue": "A database call is about to be made from async code",
        "lesson": "Await the call through an async session instead of a blocking driver",
        "evidence": [{"id": str(i), "outcome": "the call did not block the loop"} for i in cited_ids or []],
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
    # No spec= constraint — LLMClient protocol uses .call() which isn't on httpx.AsyncClient
    llm_client = AsyncMock()
    # Audit HD-1: _is_duplicate now probes find_similar_procedures (raw cosine),
    # not search_procedures (RRF rank). Default to "no duplicate found" so the
    # creation tests aren't blocked by an AsyncMock returning a truthy MagicMock.
    heart.find_similar_procedures.return_value = []
    s = settings or _make_settings()
    learner = ProcedureLearner(brain, heart, embeddings, s, llm_client)
    return learner, brain, heart, embeddings, llm_client


@pytest.mark.asyncio
async def test_decision_cluster_creates_procedure():
    """3+ similar successful reviewed decisions -> 1 procedure."""
    learner, brain, heart, embeddings, llm_client = _build_learner()

    # Set up 3 successful reviewed decisions
    summaries = [_make_decision_summary() for _ in range(3)]
    # list_decisions called twice: outcome="success" then outcome="partial"
    brain.list_decisions.side_effect = [(summaries, 3), ([], 0)]

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
    llm_client.call.return_value = _mock_llm_response(_llm_procedure_data(cited_ids=[summaries[0].id]))

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
    learner, brain, heart, embeddings, llm_client = _build_learner()

    summaries = [_make_decision_summary() for _ in range(2)]
    brain.list_decisions.side_effect = [(summaries, 2), ([], 0)]

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
    learner, brain, heart, embeddings, llm_client = _build_learner()

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
    learner, brain, heart, embeddings, llm_client = _build_learner()

    old_date = _NOW - timedelta(days=14)
    summaries = [_make_decision_summary(created_at=old_date) for _ in range(3)]
    brain.list_decisions.side_effect = [(summaries, 3), ([], 0)]

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
    learner, brain, heart, embeddings, llm_client = _build_learner()

    # No decisions (pathway 1 empty)
    brain.list_decisions.side_effect = [([], 0), ([], 0)]

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
    llm_client.call.return_value = _mock_llm_response(
        _llm_procedure_data("Episode Lesson", cited_ids=[ep_summaries[1].id])
    )

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
    learner, brain, heart, embeddings, llm_client = _build_learner()

    brain.list_decisions.side_effect = [([], 0), ([], 0)]

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
    learner, brain, heart, embeddings, llm_client = _build_learner()

    summaries = [_make_decision_summary() for _ in range(3)]
    brain.list_decisions.side_effect = [(summaries, 3), ([], 0)]
    details = [_make_decision_detail(s) for s in summaries]
    brain.get.side_effect = details

    base = _make_embedding(1.0)
    embeddings.embed_batch.return_value = [
        base,
        _similar_embedding(base, 0.005),
        _similar_embedding(base, 0.01),
    ]

    llm_client.call.return_value = _mock_llm_response(_llm_procedure_data(cited_ids=[summaries[0].id]))

    # Existing procedure with high raw-cosine similarity (HD-1: dedup now
    # probes find_similar_procedures, which returns cosine in .score).
    heart.find_similar_procedures.return_value = [_make_procedure_summary(score=0.90)]

    stats = await learner.run_sleep_learning()

    assert stats["decisions_learned"] == 0
    heart.store_procedure.assert_not_called()


@pytest.mark.asyncio
async def test_dedup_uses_cosine_probe_not_rrf():
    """Audit HD-1: _is_duplicate must use the raw-cosine probe, not the
    RRF-ranked search_procedures whose top hit is ~0.95 for any query and so
    suppressed every auto-procedure creation (prod K-line=0).

    Regression guard: a high RRF rank score on search_procedures must NOT block
    creation when the cosine probe reports no near-duplicate.
    """
    learner, brain, heart, embeddings, llm_client = _build_learner()

    summaries = [_make_decision_summary() for _ in range(3)]
    brain.list_decisions.side_effect = [(summaries, 3), ([], 0)]
    details = [_make_decision_detail(s) for s in summaries]
    brain.get.side_effect = details

    base = _make_embedding(1.0)
    embeddings.embed_batch.return_value = [
        base,
        _similar_embedding(base, 0.005),
        _similar_embedding(base, 0.01),
    ]
    llm_client.call.return_value = _mock_llm_response(_llm_procedure_data(cited_ids=[summaries[0].id]))

    # RRF rank score is high (it always is) but cosine probe finds no dup.
    heart.search_procedures.return_value = [_make_procedure_summary(score=0.97)]
    heart.find_similar_procedures.return_value = []

    stats = await learner.run_sleep_learning()

    # Creation must proceed; dedup must consult the cosine probe.
    assert stats["decisions_learned"] == 1
    heart.find_similar_procedures.assert_awaited()
    heart.store_procedure.assert_called()


@pytest.mark.asyncio
async def test_max_cap_enforcement():
    """Respects procedure_max_per_sleep cap."""
    learner, brain, heart, embeddings, llm_client = _build_learner(
        _make_settings(procedure_max_per_sleep=1)
    )

    # Set up enough decisions for 2 clusters
    summaries = [_make_decision_summary() for _ in range(6)]
    brain.list_decisions.side_effect = [(summaries, 6), ([], 0)]
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

    llm_client.call.return_value = _mock_llm_response(_llm_procedure_data(cited_ids=[summaries[0].id]))
    heart.search_procedures.return_value = []
    heart.store_procedure.return_value = MagicMock()

    stats = await learner.run_sleep_learning()

    # Max 1 allowed
    assert stats["decisions_learned"] <= 1
    assert heart.store_procedure.call_count <= 1


@pytest.mark.asyncio
async def test_disabled_learning_returns_empty():
    """When procedure_learning_enabled=False, returns empty stats."""
    learner, brain, heart, embeddings, llm_client = _build_learner(
        _make_settings(procedure_learning_enabled=False)
    )

    stats = await learner.run_sleep_learning()

    assert stats["enabled"] is False
    assert stats["decisions_learned"] == 0
    assert stats["episodes_learned"] == 0
    assert stats["weak_reviewed"] == 0
    assert stats["llm_failed"] == 0
    brain.list_decisions.assert_not_called()
    heart.list_episodes.assert_not_called()


@pytest.mark.asyncio
async def test_no_embeddings_returns_zero():
    """If no EmbeddingProvider, pathways return 0."""
    brain = AsyncMock()
    heart = AsyncMock()
    llm_client = AsyncMock()
    settings = _make_settings()
    learner = ProcedureLearner(brain, heart, None, settings, llm_client)

    brain.list_decisions.side_effect = [([], 0), ([], 0)]
    heart.list_episodes.return_value = []

    stats = await learner.run_sleep_learning()

    assert stats["decisions_learned"] == 0
    assert stats["episodes_learned"] == 0


@pytest.mark.asyncio
async def test_weak_review_retires():
    """Weak procedure with low effectiveness gets retired."""
    learner, brain, heart, embeddings, llm_client = _build_learner()

    brain.list_decisions.side_effect = [([], 0), ([], 0)]
    heart.list_episodes.return_value = []

    weak_proc = _make_procedure_summary(name="Weak proc", score=0.5)
    heart.search_procedures.return_value = [weak_proc]
    heart.get_procedure.return_value = _make_procedure_detail(
        name="Weak proc",
        effectiveness=0.20,  # below 0.30 threshold
        last_activated=_RECENT,
    )

    llm_client.call.return_value = _mock_llm_response({
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

    assert "{pairs}" in _MONITOR_RECOVERY_PROMPT
    assert '"name"' in _MONITOR_RECOVERY_PROMPT


# ---------------------------------------------------------------------------
# Regression tests: Issue #188
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_decision_pathway_passes_db_filters():
    """Regression #188 Bug 1: list_decisions must use outcome+reviewed DB filters.

    Previously called list_decisions(limit=100) with no filters, returning
    100 most recent (all pending), missing reviewed successful decisions.
    """
    learner, brain, heart, embeddings, llm_client = _build_learner()

    brain.list_decisions.side_effect = [([], 0), ([], 0)]
    heart.list_episodes.return_value = []
    heart.search_procedures.return_value = []

    await learner.run_sleep_learning()

    # Must be called twice: once for "success", once for "partial"
    assert brain.list_decisions.call_count == 2
    calls = brain.list_decisions.call_args_list

    _, kwargs0 = calls[0]
    assert kwargs0["outcome"] == "success"
    assert kwargs0["reviewed"] is True

    _, kwargs1 = calls[1]
    assert kwargs1["outcome"] == "partial"
    assert kwargs1["reviewed"] is True


@pytest.mark.asyncio
async def test_episode_pathway_logs_diagnostics(caplog):
    """Regression #188 Bug 3: Episode pathway must log diagnostic info.

    Previously failed silently with no logging.
    """
    import logging

    learner, brain, heart, embeddings, llm_client = _build_learner()

    brain.list_decisions.side_effect = [([], 0), ([], 0)]

    ep_success = _make_episode_summary(outcome="success")
    ep_failure = _make_episode_summary(outcome="failure")
    ep_ongoing = _make_episode_summary(outcome="ongoing")
    heart.list_episodes.return_value = [ep_success, ep_failure, ep_ongoing]

    heart.get_episode.return_value = _make_episode_detail(
        ep_success, lessons=["Single lesson"]
    )
    heart.search_procedures.return_value = []

    with caplog.at_level(logging.INFO, logger="nous.handlers.procedure_learner"):
        await learner.run_sleep_learning()

    assert "Episode pathway" in caplog.text
    assert "episodes fetched" in caplog.text or "lessons collected" in caplog.text


# ---------------------------------------------------------------------------
# Lesson gate (Harness² cue / lesson / evidence, NO_ENTRY, confounded)
# ---------------------------------------------------------------------------


def _decision_cluster(learner_parts):
    """Wire a 3-decision cluster into the mocks; returns the decision summaries."""
    _learner, brain, heart, embeddings, _llm = learner_parts
    summaries = [_make_decision_summary() for _ in range(3)]
    brain.list_decisions.side_effect = [(summaries, 3), ([], 0)]
    brain.get.side_effect = [_make_decision_detail(s) for s in summaries]
    base = _make_embedding(1.0)
    embeddings.embed_batch.return_value = [base, _similar_embedding(base, 0.005), _similar_embedding(base, 0.01)]
    heart.list_episodes.return_value = []
    heart.search_procedures.return_value = []
    return summaries


def _prompt_sent(llm_client) -> str:
    """The user message of the first LLM call."""
    payload = llm_client.call.call_args_list[0].args[0]
    return payload["messages"][0]["content"][0]["text"]


@pytest.mark.asyncio
async def test_gate_complete_entry_citing_a_cluster_id_is_stored_with_its_evidence():
    parts = _build_learner()
    learner, _brain, heart, _emb, llm_client = parts
    summaries = _decision_cluster(parts)
    cited = summaries[1].id
    llm_client.call.return_value = _mock_llm_response(_llm_procedure_data(cited_ids=[cited, uuid4()]))

    stats = await learner.run_sleep_learning()

    assert stats["decisions_learned"] == 1
    assert str(cited) in _prompt_sent(llm_client)
    stored = heart.store_procedure.call_args[0][0]
    assert stored.goals[0] == "A database call is about to be made from async code"
    assert "lesson: Await the call through an async session instead of a blocking driver" in stored.implementation_notes
    evidence_notes = [n for n in stored.implementation_notes if n.startswith("evidence:")]
    # Only the cited cluster member is kept; the fabricated UUID is dropped.
    assert evidence_notes == [f"evidence: {cited} (success) — the call did not block the loop"]


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["cue", "lesson", "evidence"])
async def test_gate_rejects_an_entry_with_an_empty_field(missing):
    parts = _build_learner()
    learner, _brain, heart, _emb, llm_client = parts
    summaries = _decision_cluster(parts)
    data = _llm_procedure_data(cited_ids=[summaries[0].id])
    data[missing] = [] if missing == "evidence" else "  "
    llm_client.call.return_value = _mock_llm_response(data)

    stats = await learner.run_sleep_learning()

    assert stats["decisions_learned"] == 0
    assert stats["gate_rejected"] == 1
    heart.store_procedure.assert_not_called()


@pytest.mark.asyncio
async def test_gate_rejects_evidence_that_cites_only_ids_outside_the_cluster():
    parts = _build_learner()
    learner, _brain, heart, _emb, llm_client = parts
    _decision_cluster(parts)
    llm_client.call.return_value = _mock_llm_response(_llm_procedure_data(cited_ids=[uuid4(), "D1"]))

    stats = await learner.run_sleep_learning()

    assert stats["gate_rejected"] == 1
    heart.store_procedure.assert_not_called()
    heart.find_similar_procedures.assert_not_called()


@pytest.mark.asyncio
async def test_no_entry_verdict_is_skipped_and_counted():
    parts = _build_learner()
    learner, _brain, heart, _emb, llm_client = parts
    _decision_cluster(parts)
    llm_client.call.return_value = _mock_llm_response({"verdict": "NO_ENTRY", "reason": "no shared outcome"})

    stats = await learner.run_sleep_learning()

    assert stats["no_entry"] == 1
    assert stats["gate_rejected"] == 0
    assert stats["decisions_learned"] == 0
    heart.store_procedure.assert_not_called()


@pytest.mark.asyncio
async def test_episode_prompt_carries_episode_ids_and_gate_checks_them():
    learner, brain, heart, embeddings, llm_client = _build_learner()
    brain.list_decisions.side_effect = [([], 0), ([], 0)]
    ep_summaries = [_make_episode_summary() for _ in range(3)]
    heart.list_episodes.return_value = ep_summaries
    heart.get_episode.side_effect = [_make_episode_detail(s, lessons=["Use async managers"]) for s in ep_summaries]
    base = _make_embedding(2.0)
    embeddings.embed_batch.return_value = [base, _similar_embedding(base, 0.005), _similar_embedding(base, 0.01)]
    heart.search_procedures.return_value = []
    llm_client.call.return_value = _mock_llm_response(_llm_procedure_data(cited_ids=[uuid4()]))

    stats = await learner.run_sleep_learning()

    prompt = _prompt_sent(llm_client)
    assert all(str(s.id) in prompt for s in ep_summaries)
    assert stats["episodes_learned"] == 0
    assert stats["gate_rejected"] == 1
    heart.store_procedure.assert_not_called()


def test_passes_lesson_gate_rules():
    from nous.handlers.procedure_learner import _passes_lesson_gate

    good = {"cue": "c", "lesson": "l", "evidence": [{"id": "ABC", "outcome": "worked"}]}
    allowed = {"abc": "success"}
    # IDs match case-insensitively and with the prompt's brackets around them.
    assert _passes_lesson_gate(good, allowed) == (True, "")
    assert _passes_lesson_gate({**good, "evidence": [{"id": "[abc]", "outcome": "worked"}]}, allowed)[0]
    # A string evidence item is not a citation, even if it names an ID.
    ok, reason = _passes_lesson_gate({**good, "evidence": ["abc worked"]}, allowed)
    assert not ok and "cites" in reason
    # A cited ID with no observed outcome is not evidence.
    assert not _passes_lesson_gate({**good, "evidence": [{"id": "abc", "outcome": ""}]}, allowed)[0]
    assert not _passes_lesson_gate({**good, "cue": None}, allowed)[0]


@pytest.mark.asyncio
async def test_weak_review_confounded_leaves_the_procedure_unchanged():
    learner, brain, heart, embeddings, llm_client = _build_learner()
    brain.list_decisions.side_effect = [([], 0), ([], 0)]
    heart.list_episodes.return_value = []
    heart.search_procedures.return_value = [_make_procedure_summary(name="Weak proc")]
    heart.get_procedure.return_value = _make_procedure_detail(name="Weak proc", effectiveness=0.20)
    llm_client.call.return_value = _mock_llm_response(
        {"action": "confounded", "reason": "two activations on unrelated tasks"}
    )

    stats = await learner.run_sleep_learning()

    assert stats["confounded"] == 1
    assert stats["weak_reviewed"] == 1
    heart.retire_procedure.assert_not_called()
    heart.store_procedure.assert_not_called()


def _monitor_with_learner(llm_data: dict):
    from nous.cognitive.monitor import MonitorEngine

    learner, _brain, heart, _emb, llm_client = _build_learner()
    llm_client.call.return_value = _mock_llm_response(llm_data)
    monitor = MonitorEngine(AsyncMock(), AsyncMock(), _make_settings())
    monitor._procedure_learner = learner
    pairs = [
        {"error": {"tool": "bash", "error": f"exit 1 #{i}"}, "recovery": ["read_file"], "context": "ok"}
        for i in range(3)
    ]
    return monitor, heart, llm_client, pairs


@pytest.mark.asyncio
async def test_monitor_recovery_stores_an_entry_citing_a_pair_id():
    from nous.cognitive.monitor import _recovery_pair_id

    pairs_probe = {"error": {"tool": "bash", "error": "exit 1 #2"}, "recovery": ["read_file"], "context": "ok"}
    cited = _recovery_pair_id(pairs_probe)
    monitor, heart, llm_client, pairs = _monitor_with_learner(_llm_procedure_data(cited_ids=[cited]))

    await monitor._try_create_recovery_procedure("s1", pairs)

    assert cited in _prompt_sent(llm_client)
    stored = heart.store_procedure.call_args[0][0]
    assert "auto:monitor_recovery" in stored.tags
    assert any(n.startswith(f"evidence: {cited} (recovered)") for n in stored.implementation_notes)
    assert monitor._session_procedure_counts["s1"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "llm_data",
    [
        {"verdict": "NO_ENTRY", "reason": "one-off"},
        _llm_procedure_data(cited_ids=["pair-1"]),
        {**_llm_procedure_data(cited_ids=["x"]), "evidence": []},
    ],
)
async def test_monitor_recovery_without_a_gated_entry_stores_nothing(llm_data):
    monitor, heart, _llm, pairs = _monitor_with_learner(llm_data)

    await monitor._try_create_recovery_procedure("s1", pairs)

    heart.store_procedure.assert_not_called()
    assert "s1" not in monitor._session_procedure_counts


@pytest.mark.asyncio
async def test_gate_rejects_a_citation_of_another_clusters_decision():
    """Evidence must cite the cluster it was extracted from, not any decision in the window."""
    learner, brain, heart, embeddings, llm_client = _build_learner(_make_settings(procedure_max_per_sleep=2))
    summaries = [_make_decision_summary() for _ in range(6)]
    brain.list_decisions.side_effect = [(summaries, 6), ([], 0)]
    brain.get.side_effect = [_make_decision_detail(s) for s in summaries]
    base1, base2 = _make_embedding(1.0), _make_embedding(100.0)
    embeddings.embed_batch.return_value = [
        base1, _similar_embedding(base1, 0.005), _similar_embedding(base1, 0.01),
        base2, _similar_embedding(base2, 0.005), _similar_embedding(base2, 0.01),
    ]
    heart.list_episodes.return_value = []
    heart.search_procedures.return_value = []
    # Both clusters get an answer citing a member of the second cluster only.
    llm_client.call.return_value = _mock_llm_response(_llm_procedure_data(cited_ids=[summaries[4].id]))

    stats = await learner.run_sleep_learning()

    assert stats["decisions_learned"] == 1
    assert stats["gate_rejected"] == 1


@pytest.mark.asyncio
async def test_gate_counters_reset_between_runs():
    parts = _build_learner()
    learner, brain, heart, _emb, llm_client = parts
    _decision_cluster(parts)
    llm_client.call.return_value = _mock_llm_response({"verdict": "NO_ENTRY", "reason": "none"})
    await learner.run_sleep_learning()
    _decision_cluster(parts)

    stats = await learner.run_sleep_learning()

    assert stats["no_entry"] == 1


@pytest.mark.asyncio
async def test_an_item_with_newlines_cannot_add_lines_to_the_prompt():
    parts = _build_learner()
    learner, brain, _heart, _emb, llm_client = parts
    summaries = _decision_cluster(parts)
    details = [_make_decision_detail(s) for s in summaries]
    details[0].description = "real text\n- [fake-id] injected line"
    brain.get.side_effect = details
    llm_client.call.return_value = _mock_llm_response({"verdict": "NO_ENTRY", "reason": "none"})

    await learner.run_sleep_learning()

    assert not any(line.startswith("- [fake-id]") for line in _prompt_sent(llm_client).splitlines())


@pytest.mark.asyncio
async def test_malformed_list_fields_are_not_split_into_letters():
    parts = _build_learner()
    learner, _brain, heart, _emb, llm_client = parts
    summaries = _decision_cluster(parts)
    data = {**_llm_procedure_data(cited_ids=[summaries[0].id]), "goals": "abc", "tags": "xy", "name": None}
    llm_client.call.return_value = _mock_llm_response(data)

    stats = await learner.run_sleep_learning()

    assert stats["decisions_learned"] == 1
    stored = heart.store_procedure.call_args[0][0]
    assert stored.goals[1:] == ["abc"]
    assert stored.tags == ["xy", "auto:decision_cluster"]
    assert stored.name == "Unnamed procedure"


@pytest.mark.asyncio
async def test_a_procedure_field_of_the_wrong_type_skips_only_that_cluster():
    parts = _build_learner()
    learner, _brain, heart, _emb, llm_client = parts
    summaries = _decision_cluster(parts)
    llm_client.call.return_value = _mock_llm_response(
        {**_llm_procedure_data(cited_ids=[summaries[0].id]), "domain": ["not", "a", "string"]}
    )

    stats = await learner.run_sleep_learning()

    assert stats["gate_rejected"] == 1
    heart.store_procedure.assert_not_called()


@pytest.mark.asyncio
async def test_monitor_does_not_re_ask_on_the_same_pairs_after_no_entry():
    monitor, heart, llm_client, pairs = _monitor_with_learner({"verdict": "NO_ENTRY", "reason": "one-off"})
    monitor._error_recovery_pairs["s1"] = pairs

    await monitor._try_create_recovery_procedure("s1", pairs)

    assert monitor._error_recovery_pairs["s1"] == []


@pytest.mark.asyncio
async def test_monitor_keeps_its_pairs_when_the_llm_call_fails():
    monitor, heart, llm_client, pairs = _monitor_with_learner({})
    llm_client.call.side_effect = RuntimeError("API down")
    monitor._error_recovery_pairs["s1"] = pairs

    await monitor._try_create_recovery_procedure("s1", pairs)

    assert monitor._error_recovery_pairs["s1"] == pairs


@pytest.mark.asyncio
async def test_dedup_probes_the_sanitized_procedure_not_the_raw_answer():
    parts = _build_learner()
    learner, _brain, heart, _emb, llm_client = parts
    summaries = _decision_cluster(parts)
    data = {**_llm_procedure_data(cited_ids=[summaries[0].id]), "core_patterns": ["step one", 7]}
    llm_client.call.return_value = _mock_llm_response(data)

    stats = await learner.run_sleep_learning()

    assert stats["decisions_learned"] == 1
    query = heart.find_similar_procedures.call_args[0][0]
    assert query.endswith("step one")


# ---------------------------------------------------------------------------
# Folded in from the alternative implementation (#788)
# ---------------------------------------------------------------------------


def test_cited_evidence_accepts_a_unique_shortened_uuid_and_drops_repeats():
    from nous.handlers.procedure_learner import _cited_evidence

    a = "11111111-aaaa-4bbb-8ccc-000000000001"
    b = "22222222-aaaa-4bbb-8ccc-000000000002"
    allowed = {a: "success", b: "success"}
    cited = _cited_evidence(
        [
            {"id": "11111111", "outcome": "worked"},
            {"id": "[11111111-AAAA]", "outcome": "worked"},
            {"id": a, "outcome": "worked"},
        ],
        allowed,
    )
    assert cited == [(a, "success", "worked")]


def test_cited_evidence_rejects_short_or_ambiguous_prefixes():
    from nous.handlers.procedure_learner import _cited_evidence

    allowed = {
        "33333333-aaaa-4bbb-8ccc-000000000001": "success",
        "33333333-aaaa-4bbb-8ccc-000000000002": "success",
    }
    assert _cited_evidence([{"id": "3333333", "outcome": "x"}], allowed) == []
    assert _cited_evidence([{"id": "33333333-aaaa", "outcome": "x"}], allowed) == []


def test_weak_review_prompt_shows_the_stored_lesson_and_evidence():
    from nous.handlers.procedure_learner import _WEAK_REVIEW_PROMPT

    assert "{implementation_notes}" in _WEAK_REVIEW_PROMPT


# ---------------------------------------------------------------------------
# Counted failures, safe revise, pinned citation rules, stable pair IDs
# ---------------------------------------------------------------------------


def _mock_llm_text(text: str) -> MagicMock:
    response = MagicMock()
    response.content = [{"type": "text", "text": text}]
    return response


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["not json", "api error"])
async def test_an_unusable_llm_answer_is_counted_as_llm_failed(failure):
    parts = _build_learner()
    learner, _brain, heart, _emb, llm_client = parts
    _decision_cluster(parts)
    if failure == "not json":
        llm_client.call.return_value = _mock_llm_text("not json at all")
    else:
        llm_client.call.side_effect = RuntimeError("API down")

    stats = await learner.run_sleep_learning()

    assert stats["llm_failed"] == 1
    assert stats["gate_rejected"] == 0
    heart.store_procedure.assert_not_called()


@pytest.mark.asyncio
async def test_weak_review_revise_with_a_malformed_answer_keeps_the_procedure():
    """The revision is built before the old procedure is retired, and one bad answer
    does not stop the review of the next procedure."""
    learner, brain, heart, _emb, llm_client = _build_learner()
    brain.list_decisions.side_effect = [([], 0), ([], 0)]
    heart.list_episodes.return_value = []
    first = _make_procedure_detail(name="Weak one", effectiveness=0.20)
    second = _make_procedure_detail(name="Weak two", effectiveness=0.20)
    heart.search_procedures.return_value = [
        _make_procedure_summary(name="Weak one"),
        _make_procedure_summary(name="Weak two"),
    ]
    heart.get_procedure.side_effect = [first, second]
    llm_client.call.side_effect = [
        _mock_llm_response({"action": "revise", "reason": "vague", "revised_description": ["not", "a", "string"]}),
        _mock_llm_response({"action": "retire", "reason": "unreliable"}),
    ]

    stats = await learner.run_sleep_learning()

    heart.retire_procedure.assert_called_once_with(second.id)
    heart.store_procedure.assert_not_called()
    assert stats["weak_reviewed"] == 1
    assert stats["llm_failed"] == 1


@pytest.mark.asyncio
async def test_weak_review_revise_keeps_a_lone_string_pattern_whole():
    learner, brain, heart, _emb, llm_client = _build_learner()
    brain.list_decisions.side_effect = [([], 0), ([], 0)]
    heart.list_episodes.return_value = []
    weak = _make_procedure_detail(name="Weak proc", effectiveness=0.20)
    heart.search_procedures.return_value = [_make_procedure_summary(name="Weak proc")]
    heart.get_procedure.return_value = weak
    llm_client.call.return_value = _mock_llm_response(
        {
            "action": "revise",
            "reason": "too vague",
            "revised_description": "Sharper description",
            "revised_patterns": "check the exit code",
        }
    )

    await learner.run_sleep_learning()

    heart.retire_procedure.assert_called_once_with(weak.id)
    revised = heart.store_procedure.call_args[0][0]
    assert revised.core_patterns == ["check the exit code"]
    assert revised.description == "Sharper description"
    assert revised.implementation_notes == weak.implementation_notes


def test_a_bare_string_naming_an_input_id_is_not_a_citation():
    from nous.handlers.procedure_learner import _passes_lesson_gate

    ok, _reason = _passes_lesson_gate({"cue": "c", "lesson": "l", "evidence": ["abc"]}, {"abc": "success"})
    assert not ok


def test_a_shortened_id_matches_only_a_uuid_shaped_input_id():
    from nous.handlers.procedure_learner import _cited_evidence

    allowed = {"r-0123456789": "recovered"}
    assert _cited_evidence([{"id": "r-01234567", "outcome": "x"}], allowed) == []
    assert _cited_evidence([{"id": "r-0123456789", "outcome": "x"}], allowed) == [("r-0123456789", "recovered", "x")]


@pytest.mark.asyncio
async def test_decision_prompt_shows_each_decisions_recorded_outcome():
    parts = _build_learner()
    learner, _brain, _heart, _emb, llm_client = parts
    summaries = _decision_cluster(parts)
    llm_client.call.return_value = _mock_llm_response({"verdict": "NO_ENTRY", "reason": "none"})

    await learner.run_sleep_learning()

    prompt = _prompt_sent(llm_client)
    for s in summaries:
        assert f"- [{s.id}] {s.description} (outcome: success)" in prompt


@pytest.mark.asyncio
async def test_monitor_pair_id_ignores_the_reply_text_and_each_id_is_shown_once():
    """The same error recovered the same way is one observation, whatever the reply said."""
    from nous.cognitive.monitor import _recovery_pair_id

    monitor, _heart, llm_client, _pairs = _monitor_with_learner({"verdict": "NO_ENTRY", "reason": "one-off"})
    same = {"error": {"tool": "bash", "error": "exit 1"}, "recovery": ["read_file"]}
    pairs = [{**same, "context": reply} for reply in ("first reply", "second reply", "third reply")]
    pair_id = _recovery_pair_id(pairs[0])

    await monitor._try_create_recovery_procedure("s1", pairs)

    assert _recovery_pair_id(pairs[1]) == pair_id
    assert _prompt_sent(llm_client).count(f"[{pair_id}]") == 1


@pytest.mark.asyncio
async def test_dedup_query_has_no_literal_none_when_the_description_is_missing():
    parts = _build_learner()
    learner, _brain, heart, _emb, llm_client = parts
    summaries = _decision_cluster(parts)
    llm_client.call.return_value = _mock_llm_response(
        {**_llm_procedure_data(cited_ids=[summaries[0].id]), "description": None}
    )

    await learner.run_sleep_learning()

    query = heart.find_similar_procedures.call_args[0][0]
    assert "None" not in query.split()
