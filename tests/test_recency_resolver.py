"""Tests for §1 — EVENT_DATE-Only Recency Conflict Resolver.

Pure-logic unit tests on:
  - _resolve_recency_conflicts (trigger predicate, event_date-only ordering,
    down-rank-not-delete, NULL-subject safety, parser fail-open, the accepted
    R2 multi-valued false-positive).
  - _recency_key (parse + fail-open to (None, "")).
  - _format_pipeline_text annotation at all THREE emit sites + byte-identical
    flag-OFF guard.

No DB / no network.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

from nous.api.retrieval_pipeline import (
    PipelineResult,
    PipelineStats,
    _recency_key,
    _resolve_recency_conflicts,
    run_recall_pipeline,
)
from nous.api.tools import _format_pipeline_text
from nous.heart.schemas import RecallResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _settings(*, enabled: bool = True, floor: float = 0.55):
    return SimpleNamespace(
        recency_resolver_enabled=enabled,
        recency_resolver_similarity_floor=floor,
    )


def _fact(
    *,
    description: str,
    subject: str | None = "alice",
    event_date: str | None = None,
    score: float = 1.0,
    contradicts: list | None = None,
):
    return PipelineResult(
        id=uuid4(),
        type="fact",
        description=description,
        score=score,
        source="heart",
        contradicts=contradicts or [],
        metadata={"subject": subject, "event_date": event_date},
    )


def _status(results: list[PipelineResult], rid) -> str | None:
    for r in results:
        if r.id == rid:
            return r.metadata.get("recency_status")
    return None


def _by_id(results: list[PipelineResult], rid) -> PipelineResult:
    return next(r for r in results if r.id == rid)


# ---------------------------------------------------------------------------
# _recency_key
# ---------------------------------------------------------------------------


class TestRecencyKey:
    def test_parses_iso_date(self):
        d, label = _recency_key({"event_date": "2026-01-15"})
        assert d is not None
        assert label == "2026-01"

    def test_none_event_date_fails_open(self):
        assert _recency_key({"event_date": None}) == (None, "")

    def test_missing_event_date_fails_open(self):
        assert _recency_key({}) == (None, "")

    def test_malformed_fails_open(self):
        assert _recency_key({"event_date": "not-a-date"}) == (None, "")

    def test_nonstring_fails_open(self):
        assert _recency_key({"event_date": 12345}) == (None, "")


# ---------------------------------------------------------------------------
# _resolve_recency_conflicts — core trigger predicate
# ---------------------------------------------------------------------------


class TestResolveRecencyConflicts:
    def test_newer_current_older_superseded_with_downrank(self):
        old = _fact(description="Alice works at Acme Corp", event_date="2025-01-10")
        new = _fact(description="Alice works at Beta Inc", event_date="2026-01-10")
        out = _resolve_recency_conflicts([old, new], _settings())
        assert _status(out, new.id) == "current"
        assert _status(out, old.id) == "superseded"
        # Down-rank is on the score FIELD (dormant for ordering, no reorder).
        assert _by_id(out, old.id).score == 1.0 * 0.3
        assert _by_id(out, new.id).score == 1.0
        # Order preserved (no re-sort in the resolver).
        assert [r.id for r in out] == [old.id, new.id]
        # Month label matches each fact's own event_date.
        assert _by_id(out, new.id).metadata["recency_date"] == "2026-01"
        assert _by_id(out, old.id).metadata["recency_date"] == "2025-01"

    def test_identical_content_no_trigger(self):
        a = _fact(description="Alice works at Acme", event_date="2025-01-10")
        b = _fact(description="Alice works at Acme", event_date="2026-01-10")
        out = _resolve_recency_conflicts([a, b], _settings())
        assert _status(out, a.id) is None
        assert _status(out, b.id) is None

    def test_low_overlap_different_attributes_no_trigger(self):
        a = _fact(description="Alice's role is Director", event_date="2025-01-10")
        b = _fact(description="Alice lives in New York City", event_date="2026-01-10")
        out = _resolve_recency_conflicts([a, b], _settings())
        assert _status(out, a.id) is None
        assert _status(out, b.id) is None

    def test_multi_valued_false_positive_pins_r2(self):
        # ACCEPTED-WRONG behavior (plan R2): surface-similar same-subject facts
        # with differing event_date DO get tagged even though they coexist.
        old = _fact(
            description="prefers dark mode in VSCode", event_date="2025-01-10"
        )
        new = _fact(
            description="prefers dark mode in terminal", event_date="2026-01-10"
        )
        out = _resolve_recency_conflicts([old, new], _settings())
        assert _status(out, old.id) == "superseded"
        assert _status(out, new.id) == "current"

    def test_contradicts_edge_strong_signal_low_overlap(self):
        # ratio < floor, but a contradicts edge forces the conflict.
        new = _fact(description="Alice prefers tea", event_date="2026-01-10")
        old = _fact(
            description="X Y Z totally different words here",
            event_date="2025-01-10",
            contradicts=[new.id],
        )
        out = _resolve_recency_conflicts([old, new], _settings())
        assert _status(out, old.id) == "superseded"
        assert _status(out, new.id) == "current"

    def test_equal_dates_no_trigger(self):
        a = _fact(description="Alice works at Acme", event_date="2026-01-10")
        b = _fact(description="Alice works at Beta", event_date="2026-01-10")
        out = _resolve_recency_conflicts([a, b], _settings())
        assert _status(out, a.id) is None
        assert _status(out, b.id) is None

    def test_one_missing_date_no_trigger(self):
        a = _fact(description="Alice works at Acme", event_date="2025-01-10")
        b = _fact(description="Alice works at Beta", event_date=None)
        out = _resolve_recency_conflicts([a, b], _settings())
        assert _status(out, a.id) is None
        assert _status(out, b.id) is None

    def test_both_missing_dates_no_trigger(self):
        a = _fact(description="Alice works at Acme", event_date=None)
        b = _fact(description="Alice works at Beta", event_date=None)
        out = _resolve_recency_conflicts([a, b], _settings())
        assert _status(out, a.id) is None
        assert _status(out, b.id) is None

    def test_null_subject_skipped_no_attributeerror(self):
        # subject is real str | None — None.strip() would raise without the
        # (x or "") guard. Must skip cleanly, no tagging.
        a = _fact(description="thing one", subject=None, event_date="2025-01-10")
        b = _fact(description="thing two", subject=None, event_date="2026-01-10")
        out = _resolve_recency_conflicts([a, b], _settings())  # no raise
        assert _status(out, a.id) is None
        assert _status(out, b.id) is None

    def test_malformed_date_fail_open_does_not_crash(self):
        old = _fact(description="Alice works at Acme", event_date="2025-01-10")
        bad = _fact(description="Alice works at Beta", event_date="not-a-date")
        out = _resolve_recency_conflicts([old, bad], _settings())  # no raise
        # bad date => key None => pair unresolvable => no tags.
        assert _status(out, old.id) is None
        assert _status(out, bad.id) is None

    def test_different_subjects_no_trigger(self):
        a = _fact(description="works at Acme", subject="alice", event_date="2025-01-10")
        b = _fact(description="works at Acme", subject="bob", event_date="2026-01-10")
        out = _resolve_recency_conflicts([a, b], _settings())
        assert _status(out, a.id) is None
        assert _status(out, b.id) is None

    def test_non_fact_results_pass_through(self):
        fact = _fact(description="Alice works at Acme", event_date="2025-01-10")
        ep = PipelineResult(
            id=uuid4(), type="episode", description="some episode",
            score=0.9, source="heart", metadata={},
        )
        out = _resolve_recency_conflicts([fact, ep], _settings())
        # No conflict (single fact) => everything unchanged, episode intact.
        assert len(out) == 2
        assert _status(out, ep.id) is None

    def test_superseded_sticky_across_multiple_conflicts(self):
        # A fact superseded by ANY newer value stays superseded.
        oldest = _fact(description="Alice at Acme Corp inc", event_date="2024-01-10")
        mid = _fact(description="Alice at Acme Corp llc", event_date="2025-01-10")
        newest = _fact(description="Alice at Acme Corp gmbh", event_date="2026-01-10")
        out = _resolve_recency_conflicts([oldest, mid, newest], _settings())
        assert _status(out, oldest.id) == "superseded"
        assert _status(out, newest.id) == "current"
        # mid is superseded by newest even if it was "current" vs oldest.
        assert _status(out, mid.id) == "superseded"


# ---------------------------------------------------------------------------
# _format_pipeline_text — annotation at all three emit sites
# ---------------------------------------------------------------------------


def _tagged_fact(status: str, month: str, source_episode_id=None):
    meta = {"recency_status": status, "recency_date": month}
    if source_episode_id is not None:
        meta["source_episode_id"] = source_episode_id
    return PipelineResult(
        id=uuid4(), type="fact", description="Alice works somewhere",
        score=0.5, source="heart", metadata=meta,
    )


class TestFormatRecencyTag:
    def test_flat_loop_site(self):
        # session_group_heart=False -> legacy flat loop (site 3).
        sup = _tagged_fact("superseded", "2025-01")
        cur = _tagged_fact("current", "2026-01")
        text = _format_pipeline_text([sup, cur], PipelineStats(), ["all"])
        assert "[superseded 2025-01] (id:" in text
        assert "[current 2026-01] (id:" in text

    def test_session_bucket_and_other_sites(self):
        # session_group_heart=True -> session bucket loop (site 1) for facts
        # with a source_episode_id, and no_session/"Other" loop (site 2) for
        # facts without one.
        sess = str(uuid4())
        in_session = _tagged_fact("superseded", "2025-02", source_episode_id=sess)
        no_session = _tagged_fact("current", "2026-02")
        text = _format_pipeline_text(
            [in_session, no_session], PipelineStats(), ["all"],
            session_group_heart=True,
        )
        assert f"-- Session {sess[:8]} --" in text
        assert "[superseded 2025-02] (id:" in text  # site 1
        assert "-- Other --" in text
        assert "[current 2026-02] (id:" in text  # site 2

    def test_no_tag_when_status_absent_byte_identical(self):
        # Flag OFF / non-conflicting => no recency_status => byte-identical.
        plain = PipelineResult(
            id=uuid4(), type="fact", description="Alice works somewhere",
            score=0.5, source="heart", metadata={},
        )
        text = _format_pipeline_text([plain], PipelineStats(), ["all"])
        # No recency tag between description and "(id:" — the line is exactly
        # "description (id:" with a single space (byte-identical to pre-§1).
        assert "Alice works somewhere (id:" in text
        assert "[superseded" not in text
        assert "[current" not in text


# ---------------------------------------------------------------------------
# Wiring: the resolver fires from inside run_recall_pipeline (call-site test)
# ---------------------------------------------------------------------------


_OLD_FACT_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_NEW_FACT_ID = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")


def _pipeline_settings(*, recency_resolver_enabled: bool, floor: float = 0.55):
    # Minimal SimpleNamespace covering every flag run_recall_pipeline reads.
    return SimpleNamespace(
        graph_recall_enabled=False,
        cross_type_linking_enabled=False,
        spreading_activation_enabled="false",
        contradiction_detection=False,
        graph_recall_decay=0.7,
        graph_recall_max_expand=5,
        graph_recall_max_neighbors=3,
        heart_graph_all_types_enabled=False,
        heart_graph_neighbors_per_seed=3,
        episode_chunks_enabled=False,
        episode_chunk_recall_limit=10,
        session_group_heart_section=False,
        graph_adjacency_boost_enabled=False,
        recency_resolver_enabled=recency_resolver_enabled,
        recency_resolver_similarity_floor=floor,
    )


def _make_heart_with_conflicting_facts():
    heart = MagicMock()
    heart.recall = AsyncMock(
        return_value=[
            RecallResult(
                type="fact", id=_OLD_FACT_ID,
                summary="Alice works at Acme Corp", score=0.9,
                metadata={"subject": "alice", "event_date": "2025-01-10"},
            ),
            RecallResult(
                type="fact", id=_NEW_FACT_ID,
                summary="Alice works at Beta Inc", score=0.9,
                metadata={"subject": "alice", "event_date": "2026-01-10"},
            ),
        ]
    )
    return heart


def _make_inert_brain():
    brain = MagicMock()
    brain.agent_id = "nous-test-agent"
    brain.query = AsyncMock(return_value=[])
    brain.neighbors = AsyncMock(return_value=[])
    brain.db = MagicMock()
    return brain


class TestResolverWiredIntoPipeline:
    @pytest.mark.asyncio
    async def test_flag_on_tags_older_fact_superseded(self):
        results, _stats = await run_recall_pipeline(
            query="where does alice work",
            heart=_make_heart_with_conflicting_facts(),
            brain=_make_inert_brain(),
            settings=_pipeline_settings(recency_resolver_enabled=True),
            memory_types=["fact"],
        )
        old = next(r for r in results if r.id == _OLD_FACT_ID)
        new = next(r for r in results if r.id == _NEW_FACT_ID)
        assert old.metadata["recency_status"] == "superseded"
        assert new.metadata["recency_status"] == "current"
        assert old.score == 0.9 * 0.3  # down-rank applied at the call site

    @pytest.mark.asyncio
    async def test_flag_off_no_tags(self):
        results, _stats = await run_recall_pipeline(
            query="where does alice work",
            heart=_make_heart_with_conflicting_facts(),
            brain=_make_inert_brain(),
            settings=_pipeline_settings(recency_resolver_enabled=False),
            memory_types=["fact"],
        )
        for r in results:
            assert "recency_status" not in r.metadata
