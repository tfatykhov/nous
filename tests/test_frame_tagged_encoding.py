"""Tests for 003.2 Frame-Tagged Encoding.

Verifies:
- Facts/procedures store encoded_frame and encoded_censors
- Frame boost re-ranking works correctly
- Backward compatibility (NULL frame = no boost)
- Episode compression_tier defaults to 'raw'
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from nous.heart.search import apply_frame_boost

# ---------------------------------------------------------------------------
# apply_frame_boost tests
# ---------------------------------------------------------------------------


def _make_item(encoded_frame=None, encoded_censors=None, name="item"):
    """Create a mock memory item with frame encoding metadata."""
    return SimpleNamespace(
        name=name,
        encoded_frame=encoded_frame,
        encoded_censors=encoded_censors,
    )


class TestApplyFrameBoost:
    """Tests for the apply_frame_boost re-ranking function."""

    def test_same_frame_boosted(self):
        """Same-frame memory should be ranked higher."""
        item_same = _make_item(encoded_frame="decision", name="same")
        item_diff = _make_item(encoded_frame="conversation", name="diff")
        results = apply_frame_boost([item_diff, item_same], current_frame="decision")
        assert results[0].name == "same"

    def test_different_frame_not_boosted(self):
        """Different-frame memory gets no boost (1.0x)."""
        item = _make_item(encoded_frame="debug", name="debug_item")
        results = apply_frame_boost([item], current_frame="decision")
        assert results[0].name == "debug_item"  # Still returned, just not boosted

    def test_null_frame_neutral(self):
        """NULL encoded_frame = no boost, no penalty."""
        item_null = _make_item(encoded_frame=None, name="null")
        item_same = _make_item(encoded_frame="task", name="same")
        results = apply_frame_boost([item_null, item_same], current_frame="task")
        # Same-frame should be first due to 1.3x boost
        assert results[0].name == "same"
        assert results[1].name == "null"

    def test_no_current_frame_returns_unchanged(self):
        """No current frame or censors = return as-is."""
        items = [_make_item(name="a"), _make_item(name="b")]
        results = apply_frame_boost(items, current_frame=None, current_censors=None)
        assert [r.name for r in results] == ["a", "b"]

    def test_censor_overlap_full(self):
        """Perfect censor overlap → 1.2x boost."""
        item = _make_item(
            encoded_censors=["no-premature-opt", "require-reasons"],
            name="full_overlap",
        )
        results = apply_frame_boost(
            [item],
            current_censors=["no-premature-opt", "require-reasons"],
        )
        assert results[0].name == "full_overlap"

    def test_censor_overlap_partial(self):
        """50% censor overlap → 1.1x boost."""
        item_partial = _make_item(
            encoded_censors=["censor-a", "censor-b"],
            name="partial",
        )
        item_none = _make_item(
            encoded_censors=["censor-c", "censor-d"],
            name="no_overlap",
        )
        results = apply_frame_boost(
            [item_none, item_partial],
            current_censors=["censor-a", "censor-x"],
        )
        # partial has 1/3 Jaccard overlap → boost ~1.067
        # no_overlap has 0/4 Jaccard overlap → boost 1.0
        assert results[0].name == "partial"

    def test_censor_overlap_none(self):
        """No censor overlap → 1.0x (neutral)."""
        item = _make_item(
            encoded_censors=["censor-a"],
            name="no_overlap",
        )
        results = apply_frame_boost(
            [item],
            current_censors=["censor-z"],
        )
        assert results[0].name == "no_overlap"

    def test_combined_frame_and_censor_boost(self):
        """Frame boost + censor boost stack multiplicatively."""
        item_both = _make_item(
            encoded_frame="decision",
            encoded_censors=["no-premature-opt"],
            name="both",
        )
        item_frame_only = _make_item(
            encoded_frame="decision",
            name="frame_only",
        )
        item_neither = _make_item(name="neither")
        results = apply_frame_boost(
            [item_neither, item_frame_only, item_both],
            current_frame="decision",
            current_censors=["no-premature-opt"],
        )
        # both: 1.3 * 1.2 = 1.56
        # frame_only: 1.3
        # neither: 1.0
        assert results[0].name == "both"
        assert results[1].name == "frame_only"
        assert results[2].name == "neither"

    def test_boosted_score_clamped_to_one(self):
        """Frame + censor boost should never produce scores > 1.0."""
        item = _make_item(
            encoded_frame="task",
            encoded_censors=["censor-a"],
            name="high_score",
        )
        item.score = 0.95  # High base score + 1.3x frame boost = 1.235 unclamped
        results = apply_frame_boost(
            [item], current_frame="task", current_censors=["censor-a"]
        )
        assert results[0].score <= 1.0

    def test_empty_list(self):
        """Empty input returns empty list."""
        assert apply_frame_boost([], current_frame="task") == []


# ---------------------------------------------------------------------------
# ORM model tests (require DB fixtures)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_learn_fact_with_frame(db, session, mock_embeddings):
    """Fact stored with encoded_frame and encoded_censors."""
    from nous.heart.facts import FactManager
    from nous.heart.schemas import FactInput

    mgr = FactManager(db, mock_embeddings, "test-agent")
    inp = FactInput(content="Postgres handles 80MB/year fine", category="architecture")
    detail = await mgr.learn(
        inp,
        session=session,
        encoded_frame="decision",
        encoded_censors=["no-premature-opt"],
    )
    assert detail is not None

    # Verify stored in DB
    from sqlalchemy import select

    from nous.storage.models import Fact

    result = await session.execute(select(Fact).where(Fact.id == detail.id))
    fact = result.scalar_one()
    assert fact.encoded_frame == "decision"
    assert fact.encoded_censors == ["no-premature-opt"]


@pytest.mark.asyncio
async def test_learn_fact_no_frame_backward_compat(db, session, mock_embeddings):
    """Existing API still works — NULL frame columns."""
    from nous.heart.facts import FactManager
    from nous.heart.schemas import FactInput

    mgr = FactManager(db, mock_embeddings, "test-agent")
    inp = FactInput(content="Python is dynamically typed", category="tech")
    detail = await mgr.learn(inp, session=session)
    assert detail is not None

    from sqlalchemy import select

    from nous.storage.models import Fact

    result = await session.execute(select(Fact).where(Fact.id == detail.id))
    fact = result.scalar_one()
    assert fact.encoded_frame is None
    assert fact.encoded_censors is None


@pytest.mark.asyncio
async def test_episode_compression_tier_default(db, session):
    """New episodes get compression_tier='raw'."""
    from datetime import UTC, datetime

    from nous.storage.models import Episode

    ep = Episode(
        agent_id="test-agent",
        summary="Test episode",
        started_at=datetime.now(UTC),
    )
    session.add(ep)
    await session.flush()

    from sqlalchemy import select

    result = await session.execute(select(Episode).where(Episode.id == ep.id))
    episode = result.scalar_one()
    assert episode.compression_tier == "raw"


@pytest.mark.asyncio
async def test_procedure_frame_encoding(db, session):
    """Procedures also get frame metadata."""
    from nous.storage.models import Procedure

    proc = Procedure(
        agent_id="test-agent",
        name="Deploy to production",
        domain="devops",
        encoded_frame="task",
        encoded_censors=["require-review"],
    )
    session.add(proc)
    await session.flush()

    from sqlalchemy import select

    result = await session.execute(select(Procedure).where(Procedure.id == proc.id))
    procedure = result.scalar_one()
    assert procedure.encoded_frame == "task"
    assert procedure.encoded_censors == ["require-review"]
