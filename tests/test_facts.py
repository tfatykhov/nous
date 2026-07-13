"""Tests for FactManager — semantic memory (what we know).

All tests use real Postgres via the SAVEPOINT fixture from conftest.py.
Heart methods receive the test session via the session parameter (P1-1).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from sqlalchemy import update

from nous.heart import (
    EpisodeInput,
    FactDetail,
    FactInput,
)
from nous.storage.models import Fact

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fact_input(**overrides) -> FactInput:
    """Build a FactInput with sensible defaults."""
    defaults = dict(
        content="Python uses indentation for block scoping",
        category="technical",
        subject="python",
        confidence=0.9,
        source="documentation",
        tags=["python", "syntax"],
    )
    defaults.update(overrides)
    return FactInput(**defaults)


# ---------------------------------------------------------------------------
# 1. test_learn_fact
# ---------------------------------------------------------------------------


async def test_learn_fact(heart, session):
    """Basic creation with all fields."""
    inp = _fact_input()
    detail = await heart.learn(inp, session=session)

    assert isinstance(detail, FactDetail)
    assert detail.content == inp.content
    assert detail.category == "technical"
    assert detail.subject == "python"
    assert detail.confidence == 0.9
    assert detail.source == "documentation"
    assert detail.active is True
    assert detail.confirmation_count == 0


# ---------------------------------------------------------------------------
# 2. test_learn_with_provenance
# ---------------------------------------------------------------------------


async def test_learn_with_provenance(heart, session):
    """source_episode_id and source_decision_id set."""
    # Create a real episode to use as provenance
    episode = await heart.start_episode(
        EpisodeInput(summary="Learning session"),
        session=session,
    )

    inp = _fact_input(
        content="Unique fact with provenance for testing",
        source_episode_id=episode.id,
    )
    detail = await heart.learn(inp, session=session)

    assert detail.source_episode_id == episode.id


# ---------------------------------------------------------------------------
# 3. test_confirm_fact
# ---------------------------------------------------------------------------


async def test_confirm_fact(heart, session):
    """confirmation_count increments, last_confirmed updates."""
    inp = _fact_input(content="Fact to confirm for testing purposes")
    detail = await heart.learn(inp, session=session)

    assert detail.confirmation_count == 0
    assert detail.last_confirmed is None

    confirmed = await heart.confirm_fact(detail.id, session=session)
    assert confirmed.confirmation_count == 1
    assert confirmed.last_confirmed is not None

    # Confirm again
    confirmed2 = await heart.confirm_fact(detail.id, session=session)
    assert confirmed2.confirmation_count == 2


# ---------------------------------------------------------------------------
# 4. test_supersede_chain
# ---------------------------------------------------------------------------


async def test_supersede_chain(heart, session):
    """A superseded by B, B by C. A and B inactive, C active."""
    fact_a = await heart.learn(_fact_input(content="Fact A original version for supersede chain test"), session=session)
    fact_b = await heart.supersede_fact(
        fact_a.id,
        _fact_input(content="Fact B replaces A in supersede chain test"),
        session=session,
    )
    fact_c = await heart.supersede_fact(
        fact_b.id,
        _fact_input(content="Fact C replaces B in supersede chain test"),
        session=session,
    )

    # Verify chain: A and B inactive, C active
    a = await heart.get_fact(fact_a.id, session=session)
    assert a.active is False
    assert a.superseded_by == fact_b.id

    b = await heart.get_fact(fact_b.id, session=session)
    assert b.active is False
    assert b.superseded_by == fact_c.id

    c = await heart.get_fact(fact_c.id, session=session)
    assert c.active is True
    assert c.superseded_by is None

    # get_current from A should return C
    current = await heart.get_current_fact(fact_a.id, session=session)
    assert current.id == fact_c.id


# ---------------------------------------------------------------------------
# 5. test_contradict_reduces_confidence
# ---------------------------------------------------------------------------


async def test_contradict_reduces_confidence(heart, session):
    """Original confidence drops by 0.2."""
    original = await heart.learn(
        _fact_input(content="Earth is flat according to ancient beliefs", confidence=0.8),
        session=session,
    )

    contradicting = await heart.contradict_fact(
        original.id,
        _fact_input(content="Earth is round, contradicting flat earth claim"),
        session=session,
    )

    # Re-read the original
    updated_original = await heart.get_fact(original.id, session=session)
    assert updated_original.confidence == pytest.approx(0.6, abs=0.01)

    # New fact should have contradiction_of set
    assert contradicting.contradiction_of == original.id


# ---------------------------------------------------------------------------
# 6. test_contradict_floor_zero
# ---------------------------------------------------------------------------


async def test_contradict_floor_zero(heart, session):
    """Confidence can't go below 0.0."""
    original = await heart.learn(
        _fact_input(content="Very uncertain claim about something obscure", confidence=0.1),
        session=session,
    )

    await heart.contradict_fact(
        original.id,
        _fact_input(content="Contradicting the uncertain claim"),
        session=session,
    )

    updated = await heart.get_fact(original.id, session=session)
    assert updated.confidence >= 0.0


# ---------------------------------------------------------------------------
# 7. test_search_active_only
# ---------------------------------------------------------------------------


async def test_search_active_only(heart, session):
    """Superseded facts excluded by default."""
    fact_a = await heart.learn(
        _fact_input(content="Active search test original fact"),
        session=session,
    )
    await heart.supersede_fact(
        fact_a.id,
        _fact_input(content="Active search test replacement fact"),
        session=session,
    )

    results = await heart.search_facts("Active search test original fact", session=session)
    # The superseded fact should NOT appear in active-only search
    ids = [r.id for r in results]
    assert fact_a.id not in ids


# ---------------------------------------------------------------------------
# 8. test_search_with_category
# ---------------------------------------------------------------------------


async def test_search_with_category(heart, session):
    """Filter by category."""
    await heart.learn(
        _fact_input(
            content="Category filter test technical fact",
            category="technical",
        ),
        session=session,
    )
    await heart.learn(
        _fact_input(
            content="Category filter test preference fact",
            category="preference",
        ),
        session=session,
    )

    results = await heart.search_facts(
        "Category filter test",
        category="technical",
        session=session,
    )
    for r in results:
        assert r.category == "technical"


# ---------------------------------------------------------------------------
# 9. test_deactivate
# ---------------------------------------------------------------------------


async def test_deactivate(heart, session):
    """Soft delete, search excludes it."""
    fact = await heart.learn(
        _fact_input(content="Deactivation test fact to remove"),
        session=session,
    )

    await heart.deactivate_fact(fact.id, session=session)

    # Should not appear in active search
    results = await heart.search_facts("Deactivation test fact to remove", session=session)
    ids = [r.id for r in results]
    assert fact.id not in ids


# ---------------------------------------------------------------------------
# 10. test_dedup_exclude_ids (P1-2)
# ---------------------------------------------------------------------------


async def test_dedup_exclude_ids(heart, session):
    """Verify exclude_ids prevents supersede/contradict dedup collision."""
    # Learn a fact
    original = await heart.learn(
        _fact_input(content="Exclude IDs dedup test fact for verification"),
        session=session,
    )

    # Supersede with IDENTICAL content — without exclude_ids this would
    # confirm the original instead of creating a new fact
    new_fact = await heart.supersede_fact(
        original.id,
        _fact_input(content="Exclude IDs dedup test fact for verification"),
        session=session,
    )

    # The new fact should be a DIFFERENT fact, not the original confirmed
    assert new_fact.id != original.id

    # Original should be inactive
    updated_original = await heart.get_fact(original.id, session=session)
    assert updated_original.active is False
    assert updated_original.superseded_by == new_fact.id


# ---------------------------------------------------------------------------
# F022: Graph edge bridge tests
# ---------------------------------------------------------------------------


async def test_contradict_creates_graph_edge(heart, session):
    """facts.contradict() also creates a 'contradicts' graph edge."""
    from nous.storage.models import GraphEdge
    from sqlalchemy import select

    f1 = await heart.learn(
        _fact_input(content="Tim prefers Celsius for temperature readings", subject="Tim"),
        session=session,
    )
    f2 = await heart.contradict_fact(
        f1.id,
        _fact_input(content="Tim uses Fahrenheit for temperature readings", subject="Tim"),
        session=session,
    )

    result = await session.execute(
        select(GraphEdge).where(
            GraphEdge.source_id == f2.id,
            GraphEdge.target_id == f1.id,
            GraphEdge.relation == "contradicts",
        )
    )
    edge = result.scalar_one_or_none()
    assert edge is not None
    assert edge.source_type == "fact"
    assert edge.target_type == "fact"


async def test_supersede_creates_graph_edge(heart, session):
    """facts.supersede() also creates a 'supersedes' graph edge."""
    from nous.storage.models import GraphEdge
    from sqlalchemy import select

    f1 = await heart.learn(
        _fact_input(content="Python 3.11 is the latest stable release", subject="Python"),
        session=session,
    )
    f2 = await heart.supersede_fact(
        f1.id,
        _fact_input(content="Python 3.12 is the latest stable release", subject="Python"),
        session=session,
    )

    result = await session.execute(
        select(GraphEdge).where(
            GraphEdge.source_id == f2.id,
            GraphEdge.target_id == f1.id,
            GraphEdge.relation == "supersedes",
        )
    )
    edge = result.scalar_one_or_none()
    assert edge is not None
    assert edge.source_type == "fact"
    assert edge.target_type == "fact"


# ---------------------------------------------------------------------------
# Task 7: configurable fact_min_content_chars + fact_supersession_threshold
# ---------------------------------------------------------------------------


class _ScriptedSessionT7:
    """Minimal fake session for unit tests that don't need Postgres."""

    bind = None  # signals non-Postgres to W-8 advisory-lock guard

    def __init__(self, results=()):
        self._results = list(results)

    async def execute(self, statement=None, *_a, **_k):
        if self._results:
            return self._results.pop(0)
        return _ScalarsResultT7([])

    def add(self, obj):
        pass

    async def flush(self):
        pass

    async def commit(self):
        pass

    async def refresh(self, obj, attribute_names=None):
        pass


class _ScalarsResultT7:
    def __init__(self, objs=()):
        self._objs = list(objs)

    def scalars(self):
        return self

    def all(self):
        return self._objs


def _orm_fact_t7(content="some fact", subject="subj", embedding=None):
    return SimpleNamespace(
        id=uuid4(), content=content, subject=subject, confidence=1.0,
        superseded_by=None, active=True, contradiction_of=None,
        embedding=embedding, event_date=None, event_date_classified_at=None,
    )


# (a) fact_min_content_chars — unit tests (no Postgres needed)

class TestFactMinContentCharsConfigurable:
    """Task 7a: fact_min_content_chars drives the rejection floor."""

    def _fm(self, min_chars):
        from nous.heart.facts import FactManager
        return FactManager(
            db=MagicMock(), embeddings=None, agent_id="test",
            settings=SimpleNamespace(
                fact_min_content_chars=min_chars,
                fact_native_cosine_threshold=0.95,
                fact_supersession_threshold=0.80,
                fact_band_classification_max_per_hour=1000,
            ),
        )

    @pytest.mark.asyncio
    async def test_default_30_rejects_short_fact(self):
        """Default floor=30 rejects a 15-char fact with 'too short' explanation."""
        from nous.heart.schemas import FactRejected
        fm = self._fm(30)
        result = await fm._learn(
            FactInput(content="fifteen chars!!"),  # 15 chars
            exclude_ids=[],
            check_contradictions=False,
            session=_ScriptedSessionT7(),  # type: ignore[arg-type]
        )
        assert isinstance(result, FactRejected)
        assert "too short" in result.explanation.lower()
        assert "30" in result.explanation

    @pytest.mark.asyncio
    async def test_floor_10_passes_15_char_fact(self):
        """With floor=10, a 15-char fact is NOT rejected by the floor (reaches dedup).

        The fake session can't satisfy all of _learn's DB calls, so _learn may
        raise or return an error deeper in the pipeline — that's fine.  We only
        assert the early-exit FactRejected with "too short" does NOT fire.
        """
        from nous.heart.schemas import FactRejected
        fm = self._fm(10)
        try:
            result = await fm._learn(
                FactInput(content="fifteen chars!!"),  # 15 chars, passes floor=10
                exclude_ids=[],
                check_contradictions=False,
                session=_ScriptedSessionT7(),  # type: ignore[arg-type]
            )
        except (AttributeError, TypeError) as exc:
            pytest.fail(
                f"Wiring crash in _learn before the floor gate (AttributeError/TypeError): {exc}"
            )
        except Exception:
            # Any other exception deeper in _learn means the floor gate did NOT reject.
            return
        # If it returned a FactRejected it must NOT be the "too short" one.
        if isinstance(result, FactRejected):
            assert "too short" not in result.explanation.lower(), (
                f"Floor=10 should not reject 15-char fact; got: {result.explanation}"
            )

    @pytest.mark.asyncio
    async def test_floor_0_disables_check(self):
        """floor=0 disables the length gate entirely (0-disables semantics)."""
        from nous.heart.schemas import FactRejected
        fm = self._fm(0)
        try:
            result = await fm._learn(
                FactInput(content="x"),  # 1 char — would be rejected if gate active
                exclude_ids=[],
                check_contradictions=False,
                session=_ScriptedSessionT7(),  # type: ignore[arg-type]
            )
        except (AttributeError, TypeError) as exc:
            pytest.fail(
                f"Wiring crash in _learn before the floor gate (AttributeError/TypeError): {exc}"
            )
        except Exception:
            # Any other exception deeper in _learn means the floor gate did NOT reject.
            return
        if isinstance(result, FactRejected):
            assert "too short" not in result.explanation.lower(), (
                f"floor=0 should disable the gate; got: {result.explanation}"
            )


# (b) fact_supersession_threshold — unit tests (mocked embeddings, no Postgres)

class TestFactSupersessionThresholdConfigurable:
    """Task 7b: fact_supersession_threshold drives the supersession cosine gate."""

    def _fm(self, threshold):
        from nous.heart.facts import FactManager
        return FactManager(
            db=MagicMock(), embeddings=None, agent_id="test",
            settings=SimpleNamespace(
                fact_min_content_chars=30,
                fact_native_cosine_threshold=0.95,
                fact_supersession_threshold=threshold,
                fact_band_classification_max_per_hour=1000,
            ),
        )

    @staticmethod
    def _embedding_at_similarity(target_sim: float) -> list[float]:
        """Return a 3-d unit vector with cosine similarity target_sim vs [1,0,0]."""
        import math
        perp = math.sqrt(max(0.0, 1.0 - target_sim ** 2))
        return [target_sim, perp, 0.0]

    @pytest.mark.asyncio
    async def test_default_threshold_triggers_at_0_82(self):
        """Default threshold=0.80 → similarity 0.82 triggers supersession."""
        fm = self._fm(0.80)
        anchor = [1.0, 0.0, 0.0]
        candidate = self._embedding_at_similarity(0.82)
        fact = _orm_fact_t7(content="old fact about subject", embedding=list(anchor))
        session = _ScriptedSessionT7([_ScalarsResultT7([fact])])
        new_id = uuid4()

        await fm._supersede_by_subject(new_id, "subj", candidate, session)

        assert fact.active is False
        assert fact.superseded_by == new_id

    @pytest.mark.asyncio
    async def test_higher_threshold_skips_supersession_at_0_82(self):
        """With threshold=0.90 → similarity 0.82 does NOT trigger supersession."""
        fm = self._fm(0.90)
        anchor = [1.0, 0.0, 0.0]
        candidate = self._embedding_at_similarity(0.82)
        fact = _orm_fact_t7(content="old fact about subject", embedding=list(anchor))
        session = _ScriptedSessionT7([_ScalarsResultT7([fact])])
        new_id = uuid4()

        await fm._supersede_by_subject(new_id, "subj", candidate, session)

        assert fact.active is True, "Similarity 0.82 < threshold 0.90 should NOT supersede"
        assert fact.superseded_by is None


# ---------------------------------------------------------------------------
# 8. test_get_superseded_contents_maps_and_caps
# ---------------------------------------------------------------------------


async def test_get_superseded_contents_maps_and_caps(heart, session):
    """get_superseded_contents returns up to 2 superseded contents, newest first.

    OLDs may be inactive — that is the normal supersession end-state.
    Empty input must short-circuit without issuing SQL.
    """
    # Arrange: create the superseder fact (NEW).
    # Contents must exceed the fact_min_content_chars floor (default 30).
    new_fact = await heart.learn(
        _fact_input(content="new content superseder: earth is definitely round"),
        session=session,
    )
    assert isinstance(new_fact, FactDetail), f"learn returned {type(new_fact)}"
    new_id = new_fact.id

    # Create three OLD facts
    old1 = await heart.learn(
        _fact_input(content="old1 content: earth was believed to be flat pancake"),
        session=session,
    )
    assert isinstance(old1, FactDetail)
    old2 = await heart.learn(
        _fact_input(content="old2 content: earth was believed to be a cube shape"),
        session=session,
    )
    assert isinstance(old2, FactDetail)
    old3 = await heart.learn(
        _fact_input(content="old3 content: earth was believed to be a disc shape"),
        session=session,
    )
    assert isinstance(old3, FactDetail)

    # Set superseded_by = new_id on all three OLDs (inactive, as in real supersession).
    # Assign distinct created_at so that OLD3 is newest → returned first.
    base_time = datetime.now(timezone.utc)
    await session.execute(
        update(Fact).where(Fact.id == old1.id).values(
            superseded_by=new_id, active=False,
            created_at=base_time - timedelta(minutes=2),
        )
    )
    await session.execute(
        update(Fact).where(Fact.id == old2.id).values(
            superseded_by=new_id, active=False,
            created_at=base_time - timedelta(minutes=1),
        )
    )
    await session.execute(
        update(Fact).where(Fact.id == old3.id).values(
            superseded_by=new_id, active=False,
            created_at=base_time,  # newest
        )
    )

    # Act
    result = await heart.get_superseded_contents([new_id], session=session)

    # Assert: only NEW is a key; cap 2; newest (old3) first
    assert set(result.keys()) == {new_id}
    assert len(result[new_id]) == 2
    assert result[new_id][0] == "old3 content: earth was believed to be a disc shape"   # newest first

    # Empty input must short-circuit without SQL
    assert await heart.get_superseded_contents([], session=session) == {}
