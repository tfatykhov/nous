"""N7 follow-up — leg visibility from pipeline-reported attempted legs.

The predecessor derived "which legs ran" from config flags inside the eval
harness. That reproduced pipeline control flow in a second place and could
not be correct: the one-hop graph fallback is SKIPPED when spreading
activation succeeds, which is decided at runtime. Four review rounds each
tightened the flag model and each time a producer or branch escaped it.

Here the pipeline reports what it entered (``PipelineStats.attempted_legs``,
marked at each stage's entry BEFORE its work) and the harness consumes that.
"""

from __future__ import annotations

import json
from dataclasses import replace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest


def _qrel(retrieved, gold, legs=None, error=None, attempted=()):
    from nous_eval.retrieval_runner import QrelResult

    return QrelResult(
        qrel_index=0,
        qrel_query="q",
        qrel_source="hand",
        retrieved_ids=list(retrieved),
        retrieved_types=["fact"] * len(retrieved),
        retrieved_legs=list(legs or []),
        attempted_legs=frozenset(attempted),
        rank_of_first_gold=None,
        n_gold_in_top_k=0,
        n_gold_total=len(gold),
        gold_ids=list(gold),
        error=error,
    )


def _run(per_qrel, name="baseline", attempted=()):
    from nous_eval.retrieval_runner import RetrievalConfig, RunResult

    return RunResult(
        config=RetrievalConfig(name=name),
        per_qrel=list(per_qrel),
        duration_seconds=1.0,
        attempted_legs=list(attempted),
    )


# ---------------------------------------------------------------------------
# The pipeline reports its own attempted legs
# ---------------------------------------------------------------------------


class TestPipelineReportsAttemptedLegs:
    def test_stats_exposes_attempted_legs(self):
        from nous.api.retrieval_pipeline import PipelineStats

        assert PipelineStats().attempted_legs == frozenset()

    def test_every_leg_is_marked_at_a_stage_entry(self):
        """Each label _leg_of can emit must have a producer-side mark."""
        import inspect

        from nous.api import retrieval_pipeline

        src = inspect.getsource(retrieval_pipeline)
        for leg in (
            "heart_primary", "chunk", "keyed", "keyed_r2", "exemplar",
            "brain", "heart_graph", "heart_graph_memory",
            "spreading_activation", "brain_graph",
        ):
            assert f'attempted_legs.add("{leg}")' in src, (
                f"{leg} can be produced but is never marked as attempted, so "
                "a run where it emits nothing would drop out of the report"
            )

    def test_fallback_marks_both_labels_it_can_emit(self):
        """The 1-hop fallback yields brain_graph AND heart_graph_memory rows.

        Codex round 8 found the flag-derived model missed this: Stage 4's
        untyped neighbours include non-decision nodes, which
        _graph_expanded_to_pipeline tags heart_graph_memory.
        """
        import inspect

        from nous.api import retrieval_pipeline

        src = inspect.getsource(retrieval_pipeline)
        idx = src.index("if not use_spreading:")
        window = src[idx: idx + 700]
        assert 'attempted_legs.add("brain_graph")' in window
        assert 'attempted_legs.add("heart_graph_memory")' in window

    def test_spreading_marked_inside_its_own_branch(self):
        """Marked under `if use_spreading`, NOT alongside the fallback.

        These are mutually exclusive at runtime — the distinction the flag
        model could not represent.
        """
        import inspect

        from nous.api import retrieval_pipeline

        src = inspect.getsource(retrieval_pipeline)
        taken = src.index("if use_spreading:")
        fallback = src.index("if not use_spreading:")
        mark = src.index('attempted_legs.add("spreading_activation")')
        assert taken < mark < fallback, (
            "spreading must be marked in the branch that actually ran"
        )


# ---------------------------------------------------------------------------
# leg_visibility consumes it
# ---------------------------------------------------------------------------


class TestVisibilityUsesAttemptedLegs:
    def test_attempted_but_silent_leg_is_reported(self):
        from nous_eval.metrics import leg_visibility

        qs = [_qrel([uuid4()], [], ["heart_primary"]) for _ in range(5)]
        vis = {
            v.leg: v
            for v in leg_visibility(qs, attempted_legs=["heart_primary", "keyed"])
        }

        assert "keyed" in vis, (
            "a leg the pipeline ENTERED that emitted nothing is the most "
            "extreme unobserved case — omitting its row hides the warning "
            "exactly when it matters most"
        )
        assert vis["keyed"].n_rows == 0
        assert vis["keyed"].participation_rate == 0.0
        assert vis["keyed"].visible is False

    def test_leg_never_attempted_is_not_invented(self):
        """A disabled arm must NOT be reported as enabled-but-silent."""
        from nous_eval.metrics import leg_visibility

        qs = [_qrel([uuid4()], [], ["heart_primary"])]
        legs = {v.leg for v in leg_visibility(qs, attempted_legs=["heart_primary"])}
        assert "spreading_activation" not in legs

    def test_silent_leg_does_not_crash_on_empty_ranks(self):
        from nous_eval.metrics import leg_visibility

        vis = {v.leg: v for v in leg_visibility([], attempted_legs=["keyed"])}
        assert vis["keyed"].median_rank == 0.0
        assert vis["keyed"].best_rank == 0
        assert vis["keyed"].n_qrels_evaluated == 0

    def test_head_and_tail_leg_is_visible(self):
        """A leg's own tail must not hide its head (pooled-median trap)."""
        from nous_eval.metrics import leg_visibility

        legs = ["chunk"] + ["heart_primary"] * 18 + ["chunk"] * 11
        ids = [uuid4() for _ in legs]
        vis = {v.leg: v for v in leg_visibility([_qrel(ids, [], legs)])}

        assert vis["chunk"].median_rank > 10
        assert vis["chunk"].visible is True
        assert vis["chunk"].participation_rate == 1.0

    def test_sparse_leg_denominator_is_all_qrels(self):
        """1-of-10 must read 0.10, not 1.00 (emission-conditioned trap)."""
        from nous_eval.metrics import leg_visibility

        qs = [
            _qrel([uuid4()], [], ["keyed"]),
            *[_qrel([uuid4()], [], ["heart_primary"]) for _ in range(9)],
        ]
        vis = {v.leg: v for v in leg_visibility(qs)}
        assert vis["keyed"].n_qrels_evaluated == 10
        assert vis["keyed"].participation_rate == pytest.approx(0.1)

    def test_cutoff_inverts_the_verdict(self):
        from nous_eval.metrics import leg_visibility

        legs = ["heart_primary"] * 10 + ["keyed"] * 10
        ids = [uuid4() for _ in legs]
        q = _qrel(ids, [], legs)

        at10 = {v.leg: v.visible for v in leg_visibility([q], cutoff=10)}
        at30 = {v.leg: v.visible for v in leg_visibility([q], cutoff=30)}
        assert at10["keyed"] is False
        assert at30["keyed"] is True


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


class TestReporting:
    def test_markdown_flags_silent_legs(self):
        from nous_eval.report import render_markdown

        rr = _run(
            [_qrel([uuid4()], [], ["heart_primary"])],
            attempted=["heart_primary", "exemplar"],
        )
        md = render_markdown([rr], [])
        assert "exemplar *(silent)*" in md
        assert "emitted zero rows" in md

    def test_markdown_uses_configured_cutoff(self):
        from nous_eval.report import render_markdown

        rr = _run(
            [_qrel([uuid4()], [], ["heart_primary"])], attempted=["heart_primary"]
        )
        assert "observed@30" in render_markdown([rr], [], top_k=30)

    def test_json_persists_visibility_and_attempted(self):
        from nous_eval.report import render_json

        legs = ["heart_primary"] * 10 + ["keyed"] * 10
        ids = [uuid4() for _ in legs]
        rr = _run(
            [_qrel(ids, [], legs)], attempted=["heart_primary", "keyed", "exemplar"]
        )
        cfg = json.loads(render_json([rr], []))["configs"][0]

        assert cfg["attempted_legs"] == ["heart_primary", "keyed", "exemplar"]
        vis = {v["leg"]: v for v in cfg["leg_visibility"]}
        assert vis["keyed"]["visible"] is False
        assert vis["exemplar"]["n_rows"] == 0, "silent leg persisted too"

    def test_eval_runs_payload_carries_it(self):
        from nous_eval.retrieval import _metrics_compact

        rr = _run(
            [_qrel([uuid4()], [], ["heart_primary"])],
            attempted=["heart_primary", "keyed"],
        )
        payload = _metrics_compact(rr, top_k=20)
        assert payload["attempted_legs"] == ["heart_primary", "keyed"]
        assert {v["leg"] for v in payload["leg_visibility"]} == {
            "heart_primary", "keyed",
        }
        assert payload["leg_visibility"][0]["cutoff"] == 20
