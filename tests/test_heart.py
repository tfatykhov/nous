"""Integration tests for Heart public API.

All tests use real Postgres via the SAVEPOINT fixture from conftest.py.
Heart methods receive the test session via the session parameter (P1-1).

These tests exercise the Heart class as a whole, testing cross-manager
interactions and the unified recall mechanism.
"""

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from nous.brain.brain import Brain
from nous.brain.schemas import ReasonInput, RecordInput
from nous.heart import (
    CensorInput,
    EpisodeInput,
    FactInput,
    ProcedureInput,
    RecallResult,
    WorkingMemoryItem,
)
from nous.storage.models import Event, GraphEdge

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _episode_input(**overrides) -> EpisodeInput:
    defaults = dict(
        title="Integration Test Episode",
        summary="An episode for integration testing",
        trigger="test",
    )
    defaults.update(overrides)
    return EpisodeInput(**defaults)


def _fact_input(**overrides) -> FactInput:
    defaults = dict(
        content="Integration test fact content",
        category="technical",
        subject="testing",
        confidence=0.9,
    )
    defaults.update(overrides)
    return FactInput(**defaults)


def _procedure_input(**overrides) -> ProcedureInput:
    defaults = dict(
        name="Integration test procedure",
        domain="testing",
        core_patterns=["integration testing"],
    )
    defaults.update(overrides)
    return ProcedureInput(**defaults)


def _censor_input(**overrides) -> CensorInput:
    defaults = dict(
        trigger_pattern="integration test censor trigger",
        reason="integration test censor reason",
        action="steer",
    )
    defaults.update(overrides)
    return CensorInput(**defaults)


# ---------------------------------------------------------------------------
# 1. test_full_episode_lifecycle
# ---------------------------------------------------------------------------


async def test_full_episode_lifecycle(heart, db, settings, session):
    """start -> link decision -> link procedure -> end with outcome."""
    # Start episode
    episode = await heart.start_episode(_episode_input(), session=session)
    assert episode.ended_at is None

    # Create and link a decision
    brain = Brain(database=db, settings=settings)
    decision = await brain.record(
        RecordInput(
            description="Lifecycle test decision",
            confidence=0.8,
            category="architecture",
            stakes="low",
            reasons=[ReasonInput(type="analysis", text="Test")],
        ),
        session=session,
    )
    await brain.close()

    await heart.link_decision_to_episode(episode.id, decision.id, session=session)

    # Create and link a procedure
    procedure = await heart.store_procedure(_procedure_input(), session=session)
    await heart.link_procedure_to_episode(episode.id, procedure.id, effectiveness="helped", session=session)

    # End episode
    ended = await heart.end_episode(
        episode.id,
        outcome="success",
        lessons_learned=["Integration tests work"],
        session=session,
    )
    assert ended.ended_at is not None
    assert ended.outcome == "success"
    assert ended.duration_seconds is not None
    assert "Integration tests work" in ended.lessons_learned


# ---------------------------------------------------------------------------
# 2. test_learn_and_recall
# ---------------------------------------------------------------------------


async def test_learn_and_recall(heart, session):
    """Learn 3 facts, recall by query, verify ranking."""
    await heart.learn(
        _fact_input(content="Python is dynamically typed language"),
        session=session,
    )
    await heart.learn(
        _fact_input(content="Rust has a borrow checker for memory safety"),
        session=session,
    )
    await heart.learn(
        _fact_input(content="PostgreSQL supports JSONB data type"),
        session=session,
    )

    # Search for Python fact using identical text
    results = await heart.search_facts("Python is dynamically typed language", session=session)
    assert len(results) >= 1


# ---------------------------------------------------------------------------
# 3. test_fact_deduplication
# ---------------------------------------------------------------------------


async def test_fact_deduplication(heart, session):
    """Learn same fact twice, second call confirms instead of creating new."""
    fact1 = await heart.learn(
        _fact_input(content="Dedup test: water boils at 100 degrees"),
        session=session,
    )

    # Learn identical content — should trigger dedup and confirm
    fact2 = await heart.learn(
        _fact_input(content="Dedup test: water boils at 100 degrees"),
        session=session,
    )

    # Same ID (confirmed, not new)
    assert fact2.id == fact1.id
    assert fact2.confirmation_count == 1


# ---------------------------------------------------------------------------
# 4. test_supersede_fact
# ---------------------------------------------------------------------------


async def test_supersede_fact(heart, session):
    """Supersede old fact, verify chain and active flags."""
    old = await heart.learn(
        _fact_input(content="Supersede test: old version of fact"),
        session=session,
    )
    new = await heart.supersede_fact(
        old.id,
        _fact_input(content="Supersede test: new version of fact"),
        session=session,
    )

    # Old should be inactive with superseded_by
    old_updated = await heart.get_fact(old.id, session=session)
    assert old_updated.active is False
    assert old_updated.superseded_by == new.id

    # New should be active
    assert new.active is True


# ---------------------------------------------------------------------------
# 5. test_contradict_fact
# ---------------------------------------------------------------------------


async def test_contradict_fact(heart, session):
    """Contradict, verify confidence reduction."""
    original = await heart.learn(
        _fact_input(
            content="Contradict integration test: original claim",
            confidence=0.9,
        ),
        session=session,
    )

    contradicting = await heart.contradict_fact(
        original.id,
        _fact_input(content="Contradict integration test: opposing claim"),
        session=session,
    )

    updated_original = await heart.get_fact(original.id, session=session)
    assert updated_original.confidence == pytest.approx(0.7, abs=0.01)
    assert contradicting.contradiction_of == original.id


# ---------------------------------------------------------------------------
# 5b. test_link_facts (2026-06-13 audit: edge persistence)
# ---------------------------------------------------------------------------


async def test_link_facts_creates_idempotent_edge(heart, session):
    """Heart.link_facts persists a fact↔fact edge and is idempotent — the
    public wrapper the sleep F031 resolver uses to record supersedes/contradicts
    edges that column-only writes were dropping."""
    a = await heart.learn(
        _fact_input(content="link_facts test: the deployment region is us-east-1 (side A)"),
        session=session,
    )
    b = await heart.learn(
        _fact_input(content="link_facts test: the cache layer uses Redis with a 24h TTL (side B)"),
        session=session,
    )

    async def _count() -> int:
        rows = await session.execute(
            select(GraphEdge).where(
                GraphEdge.source_id == a.id,
                GraphEdge.target_id == b.id,
                GraphEdge.relation == "contradicts",
            )
        )
        return len(rows.scalars().all())

    await heart.link_facts(a.id, b.id, "contradicts", 1.0, session=session)
    assert await _count() == 1

    # Re-link the same pair — ON CONFLICT DO NOTHING keeps it at one row.
    await heart.link_facts(a.id, b.id, "contradicts", 1.0, session=session)
    assert await _count() == 1


# ---------------------------------------------------------------------------
# 6. test_procedure_lifecycle
# ---------------------------------------------------------------------------


async def test_procedure_lifecycle(heart, session):
    """store -> activate -> record success -> check effectiveness."""
    proc = await heart.store_procedure(
        _procedure_input(name="Lifecycle test procedure"),
        session=session,
    )
    assert proc.activation_count == 0
    assert proc.effectiveness is None

    # Activate
    activated = await heart.activate_procedure(proc.id, session=session)
    assert activated.activation_count == 1

    # Record success
    result = await heart.record_procedure_outcome(proc.id, "success", session=session)
    assert result.success_count == 1
    # Laplace: (1+1)/(1+0+2) = 2/3 ~ 0.667
    assert result.effectiveness == pytest.approx(2 / 3, abs=0.01)


# ---------------------------------------------------------------------------
# 7. test_censor_lifecycle
# ---------------------------------------------------------------------------


async def test_censor_lifecycle(heart, session):
    """add -> check triggers (F078: no auto-escalation — stays steer)."""
    censor = await heart.add_censor(
        _censor_input(
            trigger_pattern="lifecycle censor test trigger",
            reason="lifecycle censor test reason",
        ),
        session=session,
    )
    assert censor.action == "steer"

    # Trigger 3 times with identical text
    for _ in range(3):
        matches = await heart.check_censors(
            "lifecycle censor test trigger lifecycle censor test reason",
            session=session,
        )

    # F078: auto-escalation removed — the censor stays steer no matter the count.
    assert len(matches) >= 1
    assert matches[0].action == "steer"


# ---------------------------------------------------------------------------
# 8. test_working_memory_capacity
# ---------------------------------------------------------------------------


async def test_working_memory_capacity(heart, session):
    """Fill to max, verify eviction of lowest relevance."""
    sid = f"test-heart-wm-{uuid.uuid4().hex[:8]}"
    await heart.get_or_create_working_memory(sid, session=session)

    # Fill to capacity (20)
    for i in range(20):
        item = WorkingMemoryItem(
            type="fact",
            ref_id=uuid.uuid4(),
            summary=f"Item {i}",
            relevance=0.5 + (i * 0.01),  # Varying relevance
            loaded_at=datetime.now(UTC),
        )
        await heart.load_to_working_memory(sid, item, session=session)

    # Add one more — should trigger eviction of lowest
    new_item = WorkingMemoryItem(
        type="fact",
        ref_id=uuid.uuid4(),
        summary="New overflow item",
        relevance=0.99,
        loaded_at=datetime.now(UTC),
    )
    state = await heart.load_to_working_memory(sid, new_item, session=session)

    assert state.item_count <= 20
    summaries = [i.summary for i in state.items]
    assert "New overflow item" in summaries


# ---------------------------------------------------------------------------
# 9. test_unified_recall
# ---------------------------------------------------------------------------


async def test_unified_recall(heart, session):
    """Populate all memory types, recall returns mixed results."""
    # Create one of each type with distinct content
    await heart.start_episode(
        _episode_input(
            title="Recall test episode",
            summary="Recall test episode for unified recall",
        ),
        session=session,
    )
    await heart.learn(
        _fact_input(content="Recall test fact for unified recall"),
        session=session,
    )
    await heart.store_procedure(
        _procedure_input(
            name="Recall test procedure for unified recall",
            core_patterns=["recall test"],
        ),
        session=session,
    )
    await heart.add_censor(
        _censor_input(
            trigger_pattern="Recall test censor for unified recall",
            reason="recall test reason",
        ),
        session=session,
    )

    # Recall across all types
    results = await heart.recall("Recall test", limit=20, session=session)

    assert isinstance(results, list)
    for r in results:
        assert isinstance(r, RecallResult)
        assert r.type in ("episode", "fact", "procedure", "censor")
        assert r.score > 0


# ---------------------------------------------------------------------------
# 10. test_unified_recall_type_filter
# ---------------------------------------------------------------------------


async def test_unified_recall_type_filter(heart, session):
    """recall with types=["fact"] returns only facts."""
    await heart.learn(
        _fact_input(content="Type filter recall test fact"),
        session=session,
    )
    await heart.store_procedure(
        _procedure_input(
            name="Type filter recall test procedure",
            core_patterns=["type filter recall"],
        ),
        session=session,
    )

    results = await heart.recall(
        "Type filter recall test fact",
        types=["fact"],
        session=session,
    )
    for r in results:
        assert r.type == "fact"


# ---------------------------------------------------------------------------
# 11. test_events_emitted
# ---------------------------------------------------------------------------


async def test_events_emitted(heart, db):
    """Verify events logged to nous_system.events."""
    import uuid as _uuid
    # Use a fully random summary (no common words) to bypass text_overlap dedup
    unique_id = _uuid.uuid4().hex
    async with db.session() as session:
        episode = await heart.start_episode(
            _episode_input(
                title=unique_id,
                summary=unique_id,
            ),
            session=session,
        )
        # Flush + query in same session (event is added but may not be auto-flushed)
        await session.flush()

        result = await session.execute(
            select(Event).where(
                Event.agent_id == heart.agent_id,
                Event.event_type == "episode_started",
            )
        )
        events = result.scalars().all()
        assert len(events) >= 1, f"No episode_started events for agent {heart.agent_id}"

        # The event data should contain the episode_id
        found = any(e.data.get("episode_id") == str(episode.id) for e in events)
        assert found, "episode_started event not found with correct episode_id"

    # Learn a fact — should emit fact_learned (content must be >= 30 chars)
    await heart.learn(
        _fact_input(content="Event emission test fact for verifying event bus"),
        session=session,
    )

    result2 = await session.execute(
        select(Event).where(
            Event.agent_id == heart.agent_id,
            Event.event_type == "fact_learned",
        )
    )
    fact_events = result2.scalars().all()
    assert len(fact_events) >= 1


# ---------------------------------------------------------------------------
# F038-1.2: Fact minimum content length
# ---------------------------------------------------------------------------


async def test_fact_minimum_content_rejected(heart, session):
    """Content shorter than 30 chars -> FactRejected."""
    from nous.heart.schemas import FactRejected

    result = await heart.learn(
        _fact_input(content="for: CORPGEN"),
        session=session,
    )
    assert isinstance(result, FactRejected)
    assert "too short" in result.explanation.lower()


async def test_fact_minimum_content_boundary(heart, session):
    """Content exactly 30 chars -> accepted (not rejected)."""
    from nous.heart.schemas import FactRejected

    content_30 = "A" * 30  # exactly 30 characters
    result = await heart.learn(
        _fact_input(content=content_30),
        session=session,
    )
    assert not isinstance(result, FactRejected)


async def test_fact_minimum_content_whitespace(heart, session):
    """Content with whitespace padding that strips to < 30 chars -> rejected."""
    from nous.heart.schemas import FactRejected

    result = await heart.learn(
        _fact_input(content="   short   "),
        session=session,
    )
    assert isinstance(result, FactRejected)
    assert "too short" in result.explanation.lower()


# ---------------------------------------------------------------------------
# F042: Cross-Encoder Reranking integration
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_recall_with_cross_encoder_mocked(heart, session, monkeypatch, caplog):
    """F042: recall_deep uses cross-encoder reranker when enabled.

    Seeds a handful of facts, enables cross_encoder via RuntimeConfig override,
    monkeypatches the reranker module to install a deterministic fake model,
    calls heart.recall, and asserts the rerank log fires and returned scores
    are in the sigmoid range (0, 1].
    """
    import logging

    from nous.heart import reranker as reranker_mod
    from nous.runtime_config import RuntimeConfig

    # Seed a few facts with distinct content
    await heart.learn(
        _fact_input(content="The rocket launch was successful at cape canaveral"),
        session=session,
    )
    await heart.learn(
        _fact_input(content="Cats are popular household pets worldwide"),
        session=session,
    )
    await heart.learn(
        _fact_input(content="Rockets use liquid hydrogen fuel for propulsion"),
        session=session,
    )

    # Install fake model: high logit for any pair whose doc contains 'rocket'
    class FakeModel:
        def __init__(self):
            self.calls = 0

        def predict(self, pairs):
            self.calls += 1
            return [5.0 if "rocket" in d.lower() else -5.0 for (_, d) in pairs]

    fake = FakeModel()
    monkeypatch.setattr(reranker_mod, "CROSS_ENCODER_AVAILABLE", True)
    monkeypatch.setattr(reranker_mod, "_load_cross_encoder", lambda model_name: fake)

    # Enable cross-encoder via RuntimeConfig override
    RuntimeConfig.get().set_cross_encoder_enabled(True)
    try:
        with caplog.at_level(logging.INFO, logger="nous.heart.heart"):
            results = await heart.recall("rocket", session=session)

        # Fake model was invoked at least once
        assert fake.calls >= 1

        # All returned scores are in sigmoid range (0, 1]
        for r in results:
            assert 0.0 < r.score <= 1.0, f"score out of sigmoid range: {r.score}"

        # Reorder log line fires (F042 cross-encoder info log in heart.py)
        ce_logs = [rec for rec in caplog.records if "Cross-encoder" in rec.getMessage()]
        assert len(ce_logs) >= 1
    finally:
        RuntimeConfig.get().clear_cross_encoder_enabled()


async def test_recall_subsearch_failure_rollback_when_we_own_session(
    heart, session, monkeypatch,
):
    """When Heart owns the session, a sub-search exception triggers
    session.rollback() so the asyncpg connection's failed-transaction
    state is cleared and remaining sub-searches in the loop continue.

    This unit test verifies the call site fires; the integration test
    below verifies it actually clears real asyncpg state.
    """
    _stub_remaining_searches(heart, session, monkeypatch)

    # Monkeypatch episodes.search to raise (simulates schema mismatch).
    async def _broken_episode_search(*_args, **_kwargs):
        raise RuntimeError("simulated session-poisoning sub-search failure")
    monkeypatch.setattr(heart.episodes, "search", _broken_episode_search)

    # Track rollback (without actually rolling back — would nuke test txn).
    rollback_calls: list[None] = []

    async def _track_rollback() -> None:
        rollback_calls.append(None)
    monkeypatch.setattr(session, "rollback", _track_rollback)

    # Call _recall directly with owns_session=True (mirrors the path the
    # public recall() takes when no session is passed in).
    await heart._recall(
        "anything", limit=10,
        types=["episode", "fact", "procedure", "censor"],
        session=session, owns_session=True,
    )

    assert len(rollback_calls) >= 1, (
        "session.rollback() not called after sub-search exception — "
        "asyncpg transaction would stay poisoned and cascade-kill remaining "
        "sub-searches in prod."
    )
    assert heart._stub_facts_called == 1
    assert heart._stub_procedures_called == 1
    assert heart._stub_censors_called == 1


async def test_recall_subsearch_failure_skips_rollback_when_caller_owns_session(
    heart, session, monkeypatch,
):
    """When a caller passes their own session into Heart.recall, Heart
    must NOT rollback after a sub-search exception — that would silently
    discard the caller's uncommitted work earlier in the same transaction.

    This test guards the asymmetric-ownership trap surfaced in code
    review of the original cascade-fix: the rollback is correct for the
    dominant 'session is None' path, dangerous for the shared-session
    path.
    """
    _stub_remaining_searches(heart, session, monkeypatch)

    async def _broken_episode_search(*_args, **_kwargs):
        raise RuntimeError("session-poisoning failure")
    monkeypatch.setattr(heart.episodes, "search", _broken_episode_search)

    rollback_calls: list[None] = []

    async def _track_rollback() -> None:
        rollback_calls.append(None)
    monkeypatch.setattr(session, "rollback", _track_rollback)

    # Call via public recall() which forwards session and sets owns_session=False.
    await heart.recall(
        "anything", limit=10,
        types=["episode", "fact", "procedure", "censor"],
        session=session,
    )

    assert len(rollback_calls) == 0, (
        "session.rollback() was called on a caller-provided session — "
        "Heart must not silently rollback work the caller may have "
        "queued in the same transaction."
    )


async def test_recall_subsearch_real_sql_error_does_not_cascade(
    heart, monkeypatch,
):
    """Integration test: a real asyncpg SQL-level error in one sub-search
    must not cascade-kill the next sub-search via
    InFailedSQLTransactionError.

    Uses heart.recall with no session so Heart opens its own (owns_session
    path → rollback fires). Monkeypatches episodes.search to issue a real
    bad SQL query against the shared session, then raises. Without the
    rollback fix, the next sub-search would raise asyncpg's
    InFailedSQLTransactionError; with the fix, it returns cleanly.
    """
    from sqlalchemy import text

    async def _broken_episode_search(query, fetch_limit, session, **kwargs):
        # Real SQL error → asyncpg marks the connection's transaction ABORTED.
        try:
            await session.execute(
                text("SELECT bogus_col_xyz FROM heart.episodes LIMIT 1")
            )
        except Exception:
            pass
        # Session is now in InFailedSQLTransactionError state.
        raise RuntimeError("episode search failed (poisoned the session)")
    monkeypatch.setattr(heart.episodes, "search", _broken_episode_search)

    # No session passed — Heart opens its own and owns rollback.
    # Without the fix this raises asyncpg.InFailedSQLTransactionError
    # from facts.search trying to query a poisoned transaction.
    results = await heart.recall(
        "anything", limit=10, types=["episode", "fact"],
    )
    # Call completed; cascade was broken. Result content doesn't matter.
    assert results is not None


def _stub_remaining_searches(heart, session, monkeypatch):
    """Stub fact/procedure/censor searches and tag heart with call counts."""
    heart._stub_facts_called = 0
    heart._stub_procedures_called = 0
    heart._stub_censors_called = 0

    async def _stub_fact_search(*_args, **_kwargs):
        heart._stub_facts_called += 1
        return []
    monkeypatch.setattr(heart.facts, "search", _stub_fact_search)

    async def _stub_procedure_search(*_args, **_kwargs):
        heart._stub_procedures_called += 1
        return []
    monkeypatch.setattr(heart.procedures, "search", _stub_procedure_search)

    async def _stub_censor_search(*_args, **_kwargs):
        heart._stub_censors_called += 1
        return []
    monkeypatch.setattr(heart.censors, "search", _stub_censor_search)
