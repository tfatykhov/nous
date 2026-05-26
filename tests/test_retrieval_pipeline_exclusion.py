"""F071: cross-context dedup exclusion-set tests for run_recall_pipeline.

Verifies the new ``exclude_ids`` parameter on
``nous.api.retrieval_pipeline.run_recall_pipeline`` and the new
``PipelineStats.excluded_in_context`` counter. All tests use the same mock
heart/brain/settings pattern as ``tests/test_retrieval_pipeline.py``.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from nous.api.retrieval_pipeline import run_recall_pipeline

# Sibling test module — pytest adds tests/ to sys.path so direct import works
from test_retrieval_pipeline import (  # noqa: E402
    DECISION_ONE_ID,
    DECISION_TWO_ID,
    EPISODE_ID,
    FACT_ID,
    GRAPH_DECISION_ID,
    _FakeContradictionEdge,
    _make_brain,
    _make_decision_summaries,
    _make_heart,
    _make_neighbors_for_brain_seed,
    _make_neighbors_for_heart_seed,
    _make_recall_results,
    _make_settings,
)


def _baseline_kwargs():
    """Common pipeline kwargs that produce a deterministic 6-item result list.

    Mirrors ``test_full_pipeline_all_stages_fire`` from
    ``test_retrieval_pipeline.py`` so we have a stable baseline to filter.
    """
    heart = _make_heart(recall_results=_make_recall_results())
    decisions = _make_decision_summaries()
    contradictions = [
        _FakeContradictionEdge(
            DECISION_ONE_ID, "decision", DECISION_TWO_ID, "decision"
        )
    ]
    brain = _make_brain(
        neighbors_by_node={
            FACT_ID: _make_neighbors_for_heart_seed(),
            EPISODE_ID: [],
            DECISION_ONE_ID: _make_neighbors_for_brain_seed(),
            DECISION_TWO_ID: [],
        },
        contradictions=contradictions,
        decision_results=decisions,
    )
    settings = _make_settings()
    return dict(
        query="anything",
        heart=heart,
        brain=brain,
        settings=settings,
        limit=10,
    )


class TestExcludeIdsParameter:
    """Direct exercise of run_recall_pipeline(exclude_ids=...)."""

    @pytest.mark.asyncio
    async def test_exclude_ids_none_returns_baseline(self):
        """exclude_ids=None: results untouched, counter == 0."""
        kw = _baseline_kwargs()
        results_baseline, stats_baseline = await run_recall_pipeline(
            **kw, exclude_ids=None,
        )
        assert stats_baseline.excluded_in_context == 0
        # Sanity: the baseline produces the expected 6-item list
        # (heart fact, heart episode, heart_graph decision, brain decision x2,
        # brain_graph decision)
        assert len(results_baseline) == 6

    @pytest.mark.asyncio
    async def test_exclude_ids_empty_dict_short_circuits(self):
        """exclude_ids={} behaves identically to None.

        Guards against accidental future strictening (e.g. requiring all 4
        keys to be present) — empty dict must remain a clean no-op so the
        short-circuit in run_recall_pipeline keeps the snapshot test honest.
        """
        kw = _baseline_kwargs()
        r_none, _ = await run_recall_pipeline(**kw, exclude_ids=None)
        kw2 = _baseline_kwargs()
        r_empty, s_empty = await run_recall_pipeline(**kw2, exclude_ids={})
        assert [r.id for r in r_none] == [r.id for r in r_empty]
        assert s_empty.excluded_in_context == 0

    @pytest.mark.asyncio
    async def test_exclude_single_fact_drops_only_that_fact(self):
        """exclude_ids={"fact": {<uuid>}}: drops the fact, keeps everything else."""
        kw = _baseline_kwargs()
        results, stats = await run_recall_pipeline(
            **kw, exclude_ids={"fact": {str(FACT_ID)}},
        )
        ids_remaining = [r.id for r in results]
        assert FACT_ID not in ids_remaining
        # Other items survive
        assert EPISODE_ID in ids_remaining
        assert DECISION_ONE_ID in ids_remaining
        assert stats.excluded_in_context == 1

    @pytest.mark.asyncio
    async def test_exclude_type_keyed_no_cross_filter(self):
        """Same UUID in `fact` exclusion set must not drop an episode with the
        same UUID.

        Defends the type-key invariant. The baseline has no such collision
        by default; we synthesize one by passing FACT_ID under "episode".
        Result: FACT_ID's *fact* row survives because the exclusion is keyed
        on "episode".
        """
        kw = _baseline_kwargs()
        # Exclude FACT_ID under the "episode" key — should NOT drop the fact
        results, stats = await run_recall_pipeline(
            **kw, exclude_ids={"episode": {str(FACT_ID)}},
        )
        # The fact survives because its type is "fact", not "episode"
        assert any(r.id == FACT_ID and r.type == "fact" for r in results)
        # The episode with EPISODE_ID survives because its id ≠ FACT_ID
        assert any(r.id == EPISODE_ID and r.type == "episode" for r in results)
        assert stats.excluded_in_context == 0

    @pytest.mark.asyncio
    async def test_exclude_unknown_type_no_op(self):
        """exclude_ids={"chunk": {...}} when no chunk-type results exist:
        graceful no-op (F072 territory; v1 should not raise)."""
        kw = _baseline_kwargs()
        results, stats = await run_recall_pipeline(
            **kw, exclude_ids={"chunk": {str(uuid4())}},
        )
        assert stats.excluded_in_context == 0
        assert len(results) == 6  # baseline length

    @pytest.mark.asyncio
    async def test_exclude_unrelated_type_does_not_drop_anything(self):
        """Result types `censor` and `chunk` (absent from baseline) and any
        type not present in exclude_ids' keys must pass through untouched.

        Guards against a future refactor to a stricter lookup that errors
        on unknown keys.
        """
        kw = _baseline_kwargs()
        # baseline has fact, episode, decision — exclude under "procedure"
        # (a type that's keyed in TurnContext but has no rows here).
        results, stats = await run_recall_pipeline(
            **kw, exclude_ids={"procedure": {str(uuid4()), str(uuid4())}},
        )
        assert stats.excluded_in_context == 0
        assert len(results) == 6

    @pytest.mark.asyncio
    async def test_exclude_total_overlap_returns_empty(self):
        """When every result id is in exclude_ids, the filter empties the
        list and the counter equals the original size."""
        kw = _baseline_kwargs()
        # First get the baseline so we know what's there
        baseline, _ = await run_recall_pipeline(**_baseline_kwargs(), exclude_ids=None)
        # Build a full per-type exclusion set
        per_type: dict[str, set[str]] = {}
        for r in baseline:
            per_type.setdefault(r.type, set()).add(str(r.id))

        results, stats = await run_recall_pipeline(**kw, exclude_ids=per_type)
        assert results == []
        assert stats.excluded_in_context == len(baseline)

    @pytest.mark.asyncio
    async def test_exclude_multiple_types_counts_sum(self):
        """Multiple type-keys: counter is the sum of drops across types."""
        kw = _baseline_kwargs()
        results, stats = await run_recall_pipeline(
            **kw,
            exclude_ids={
                "fact": {str(FACT_ID)},
                "decision": {str(DECISION_ONE_ID), str(DECISION_TWO_ID)},
            },
        )
        ids = [r.id for r in results]
        assert FACT_ID not in ids
        assert DECISION_ONE_ID not in ids
        assert DECISION_TWO_ID not in ids
        assert stats.excluded_in_context == 3
        # Episode and graph-expanded items survive
        assert EPISODE_ID in ids

    @pytest.mark.asyncio
    async def test_graph_expanded_neighbor_survives_when_seed_excluded(self):
        """F022 graph-expanded neighbor has its OWN id, distinct from the
        seed. Excluding the seed (DECISION_ONE_ID) must NOT drop the
        neighbor (GRAPH_DECISION_ID)."""
        kw = _baseline_kwargs()
        results, stats = await run_recall_pipeline(
            **kw, exclude_ids={"decision": {str(DECISION_ONE_ID)}},
        )
        # Seed is gone
        assert all(r.id != DECISION_ONE_ID for r in results)
        # Graph-expanded neighbor with different id survives
        neighbors = [r for r in results if r.source == "graph_expanded"]
        assert any(r.id == GRAPH_DECISION_ID for r in neighbors)
        # Counter == 1 (seed dropped, neighbor kept)
        assert stats.excluded_in_context == 1

    @pytest.mark.asyncio
    async def test_uuid_canonical_form_matches(self):
        """str(UUID(...)) yields canonical lowercase-with-dashes form.
        Exclusion set built from that form matches PipelineResult.id strings."""
        kw = _baseline_kwargs()
        canonical = str(FACT_ID)  # what TurnContext stores
        assert canonical == "11111111-1111-1111-1111-111111111111"  # documentary
        results, stats = await run_recall_pipeline(
            **kw, exclude_ids={"fact": {canonical}},
        )
        assert all(r.id != FACT_ID for r in results)
        assert stats.excluded_in_context == 1

    @pytest.mark.asyncio
    async def test_uuid_non_canonical_form_misses(self):
        """Non-canonical UUID strings (no-dashes form) are silently skipped.

        Documented precondition: callers (TurnContext.recalled_*_ids) populate
        the exclusion set via ``str(uuid.UUID(...))``, which produces canonical
        lowercase-with-dashes form. If a caller ever passes the hex32 form
        (no dashes), the filter no-ops — by design.
        """
        kw = _baseline_kwargs()
        non_canonical = str(FACT_ID).replace("-", "")  # "11111111…" (32 hex)
        assert non_canonical != str(FACT_ID)  # sanity
        results, stats = await run_recall_pipeline(
            **kw, exclude_ids={"fact": {non_canonical}},
        )
        # Non-canonical form does NOT match str(UUID) → 0 drops
        assert stats.excluded_in_context == 0
        assert len(results) == 6
