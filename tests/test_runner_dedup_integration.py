"""F071: integration tests for the runner ↔ recall_deep dedup wire.

Skips a full ``AgentRunner.run_turn`` roundtrip in favor of exercising the
three load-bearing pieces directly:

- ``_build_exclude_ids`` packs a ``TurnContext`` into a type-keyed set dict
- ``CURRENT_TURN_EXCLUDE_IDS`` ContextVar carries that dict across the call
- ``run_recall_pipeline(exclude_ids=...)`` filters results post-rerank

The full E2E ``run_turn`` path is exercised indirectly by the existing
``tests/test_recall_deep_*.py`` suite — those assert byte-identical output
with the default ``exclude_ids=None``, which is the strongest regression guard.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from nous.api.retrieval_pipeline import run_recall_pipeline
from nous.api.runner import CURRENT_TURN_EXCLUDE_IDS, _build_exclude_ids

from test_retrieval_pipeline import (  # noqa: E402
    DECISION_ONE_ID,
    EPISODE_ID,
    FACT_ID,
    _FakeContradictionEdge,
    _make_brain,
    _make_decision_summaries,
    _make_heart,
    _make_neighbors_for_brain_seed,
    _make_neighbors_for_heart_seed,
    _make_recall_results,
    _make_settings,
)


def _make_turn_context(
    fact_ids=None, decision_ids=None, episode_ids=None, procedure_ids=None,
):
    """Lightweight stand-in for cognitive.schemas.TurnContext — only the four
    list fields F071 reads. Avoids pulling in the full pydantic + cognitive
    stack just to feed a helper that only touches 4 attributes."""
    tc = MagicMock()
    tc.recalled_fact_ids = list(fact_ids or [])
    tc.recalled_decision_ids = list(decision_ids or [])
    tc.recalled_episode_ids = list(episode_ids or [])
    tc.recalled_procedure_ids = list(procedure_ids or [])
    return tc


def _baseline_pipeline_kwargs():
    """Re-uses the established fixture from test_retrieval_pipeline.py."""
    heart = _make_heart(recall_results=_make_recall_results())
    brain = _make_brain(
        neighbors_by_node={
            FACT_ID: _make_neighbors_for_heart_seed(),
            EPISODE_ID: [],
            DECISION_ONE_ID: _make_neighbors_for_brain_seed(),
        },
        contradictions=[
            _FakeContradictionEdge(
                DECISION_ONE_ID, "decision",
                DECISION_ONE_ID, "decision",  # self-loop placeholder
            )
        ],
        decision_results=_make_decision_summaries(),
    )
    return dict(
        query="anything",
        heart=heart,
        brain=brain,
        settings=_make_settings(),
        limit=10,
    )


class TestBuildExcludeIds:
    def test_flag_off_returns_none(self):
        """When the flag is off, _build_exclude_ids short-circuits → None,
        which makes the pipeline preserve baseline behavior byte-identically."""
        s = MagicMock()
        s.recall_exclude_context_ids = False
        tc = _make_turn_context(fact_ids=["a", "b"], decision_ids=["c"])
        assert _build_exclude_ids(s, tc) is None

    def test_no_turn_context_returns_none(self):
        s = MagicMock()
        s.recall_exclude_context_ids = True
        assert _build_exclude_ids(s, None) is None

    def test_flag_on_packs_4_types(self):
        s = MagicMock()
        s.recall_exclude_context_ids = True
        tc = _make_turn_context(
            fact_ids=["a", "b"],
            decision_ids=["c"],
            episode_ids=["d"],
            procedure_ids=["e", "f"],
        )
        out = _build_exclude_ids(s, tc)
        assert out is not None
        assert set(out.keys()) == {"fact", "decision", "episode", "procedure"}
        assert out["fact"] == {"a", "b"}
        assert out["decision"] == {"c"}
        assert out["episode"] == {"d"}
        assert out["procedure"] == {"e", "f"}

    def test_flag_on_empty_lists_yields_empty_sets(self):
        """Empty recalled_*_ids → empty sets in the dict (not absent keys).

        The pipeline's `exclude_ids.get(r.type, set())` is robust either way,
        but documenting the contract here pins it."""
        s = MagicMock()
        s.recall_exclude_context_ids = True
        tc = _make_turn_context()  # all empty
        out = _build_exclude_ids(s, tc)
        assert out == {"fact": set(), "decision": set(), "episode": set(), "procedure": set()}

    def test_flag_on_missing_field_treated_as_empty(self):
        """If a TurnContext-like object has ``recalled_fact_ids = None`` (e.g.
        partial pydantic init), the helper coerces to an empty set."""
        s = MagicMock()
        s.recall_exclude_context_ids = True
        tc = MagicMock()
        tc.recalled_fact_ids = None
        tc.recalled_decision_ids = None
        tc.recalled_episode_ids = None
        tc.recalled_procedure_ids = None
        out = _build_exclude_ids(s, tc)
        assert out["fact"] == set()


class TestContextVarToPipelineFlow:
    """End-to-end: set CURRENT_TURN_EXCLUDE_IDS, call run_recall_pipeline
    while reading the contextvar, observe the filter effect."""

    @pytest.mark.asyncio
    async def test_contextvar_value_reaches_pipeline_filter(self):
        # Capture baseline IDs (no filter)
        baseline_results, _ = await run_recall_pipeline(
            **_baseline_pipeline_kwargs(), exclude_ids=None,
        )
        # Pick a real fact ID that appeared in the baseline
        target = next(
            r.id for r in baseline_results if r.type == "fact"
        )

        # Set contextvar, read it, pass to pipeline (mimics what recall_deep does)
        token = CURRENT_TURN_EXCLUDE_IDS.set({"fact": {str(target)}})
        try:
            picked = CURRENT_TURN_EXCLUDE_IDS.get()
            filtered_results, stats = await run_recall_pipeline(
                **_baseline_pipeline_kwargs(), exclude_ids=picked,
            )
        finally:
            CURRENT_TURN_EXCLUDE_IDS.reset(token)

        # Target dropped, others survive
        assert all(r.id != target for r in filtered_results)
        assert stats.excluded_in_context == 1
        # Contextvar restored to default after reset
        assert CURRENT_TURN_EXCLUDE_IDS.get() is None

    @pytest.mark.asyncio
    async def test_unset_contextvar_short_circuits_pipeline(self):
        """When CURRENT_TURN_EXCLUDE_IDS is at its default (None) — the case
        for the F051 eval harness, tests, and any code path that doesn't
        enter run_turn — the pipeline must produce baseline output."""
        # Sanity: default is None (cleared even after prior test set/reset)
        assert CURRENT_TURN_EXCLUDE_IDS.get() is None

        picked = CURRENT_TURN_EXCLUDE_IDS.get()
        results, stats = await run_recall_pipeline(
            **_baseline_pipeline_kwargs(), exclude_ids=picked,
        )
        assert stats.excluded_in_context == 0
        assert len(results) == 6  # baseline shape
