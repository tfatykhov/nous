"""The formatter reports what it emitted; F091 consumes that report.

Which results reach the rendered text depends on `search_types` section
eligibility. Every attempt to re-derive that rule elsewhere has been wrong:
`nous_eval/metrics.py:49-55` records two harness attempts that OVERSTATED served
recall, and the F091 scope-filter block replaced here was incomplete under
`search_types=["decision"]` after an earlier revision had inverted the error.

These tests pin the report itself, and the two failure directions that motivated
it.
"""

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from nous.api.retrieval_pipeline import PipelineResult, PipelineStats
from nous.api.tools import _format_pipeline_text

NOW = datetime(2026, 1, 5, tzinfo=UTC)


def _row(typ, src, origin=None, *, desc=None, edge=None, meta=None):
    md = dict(meta or {})
    if origin:
        md["stage_origin"] = origin
    kw = {"edge_relation": edge} if edge else {}
    return PipelineResult(
        id=uuid4(), type=typ, description=desc or f"body for {typ}/{src}",
        score=0.5, source=src, metadata=md, **kw)


def _mixed():
    """One row of every shape the pipeline can put in `results`."""
    return [
        _row("fact", "heart"),
        _row("chunk", "heart"),
        _row("episode", "heart"),
        _row("procedure", "heart"),
        _row("decision", "brain"),
        _row("decision", "graph_expanded", "brain_graph", edge="related_to"),
        _row("fact", "graph_expanded", "heart_graph_memory", edge="related_to"),
        _row("fact", "spreading_activation", "heart_graph_memory",
             edge="spreading_activation"),
        _row("decision", "graph_expanded", "heart_graph", edge="related_to"),
        # The exemplar section renders from a metadata tag and prints NO
        # "(id: ...)" — collection works off the row, but any test checking
        # served-ness by regexing rendered ids is blind here.
        _row("fact", "heart", desc="an utterance\nlabel: 1",
             meta={"retrieval_leg": "exemplar", "label": 1, "similarity": 0.9}),
    ]


class TestCollectorIsASideChannel:
    @pytest.mark.parametrize("scope", [["all"], ["fact"], ["decision"]])
    @pytest.mark.parametrize("grouped", [False, True])
    def test_text_is_byte_identical_with_and_without_collection(self, scope, grouped):
        """The `recall_deep` snapshot contract. Collection must not alter output."""
        rows = _mixed()
        a = _format_pipeline_text(rows, PipelineStats(), scope,
                                  session_group_heart=grouped)
        out: list = []
        b = _format_pipeline_text(rows, PipelineStats(), scope,
                                  session_group_heart=grouped, emitted_out=out)
        assert a == b

    def test_default_collects_nothing(self):
        rows = _mixed()
        _format_pipeline_text(rows, PipelineStats(), ["all"])  # must not raise


class TestConservation:
    @pytest.mark.parametrize("grouped", [False, True])
    def test_under_all_scope_everything_is_emitted(self, grouped):
        """Measured property: the formatter drops NOTHING under ["all"].

        Parametrised over `session_group_heart` because there are three separate
        heart emission paths (session-bucket, "Other", flat) — a collector wired
        only to the flat one passes with grouping off and under-reports with it on.
        """
        rows = _mixed()
        out: list = []
        _format_pipeline_text(rows, PipelineStats(), ["all"],
                              session_group_heart=grouped, emitted_out=out)
        assert {(r.id, r.type) for r in rows} == set(out)

    def test_emitted_is_always_a_subset_of_results(self):
        """`emitted_out` must never invent a row — parent episodes are served
        content that is NOT in `results`, so collecting them would break every
        consumer's `emitted ⊆ results` assumption."""
        rows = _mixed()
        out: list = []
        _format_pipeline_text(
            rows, PipelineStats(), ["all"],
            parent_episodes=[(str(uuid4()), "a parent episode summary")],
            emitted_out=out)
        assert set(out) <= {(r.id, r.type) for r in rows}

    def test_parent_episodes_are_not_collected(self):
        """Pins the scoping choice above so it fails loudly if reversed."""
        ep_id = str(uuid4())
        out: list = []
        text = _format_pipeline_text(
            _mixed(), PipelineStats(), ["all"],
            parent_episodes=[(ep_id, "parent summary text")], emitted_out=out)
        assert "parent summary text" in text, "it IS served content"
        assert not any(str(i) == ep_id for i, _t in out), "but is not collected"


class TestDropsAreReported:
    def test_narrow_scope_drops_are_reported_not_predicted(self):
        """Rows outside the scoped sections are absent from the report."""
        rows = _mixed()
        out: list = []
        _format_pipeline_text(rows, PipelineStats(), ["fact"], emitted_out=out)
        emitted = set(out)
        assert emitted < {(r.id, r.type) for r in rows}, "some rows must drop"
        # Everything reported emitted must genuinely be in the text.
        text = _format_pipeline_text(rows, PipelineStats(), ["fact"])
        for r in rows:
            if (r.id, r.type) in emitted:
                assert str(r.id) in text or r.description in text

    def test_heart_graph_memory_row_under_decision_scope_is_reported_dropped(self):
        """THE DEFECT that motivated this change.

        The predictive block only fired when `decision` was ABSENT from scope,
        and covered only brain-side rows — so this row was dropped by the
        formatter and still read `rendered` in F091. A collector cannot make
        that mistake, because it reports rather than predicts.
        """
        target = _row("fact", "spreading_activation", "heart_graph_memory",
                      desc="UNIQUEHGMEM body", edge="spreading_activation")
        rows = [_row("decision", "brain"), target]
        out: list = []
        text = _format_pipeline_text(rows, PipelineStats(), ["decision"],
                                     emitted_out=out)
        assert "UNIQUEHGMEM" not in text, "formatter drops it under this scope"
        assert (target.id, target.type) not in set(out), (
            "and the report must say so — the predictive block did not"
        )

    def test_brain_row_under_fact_scope_is_reported_dropped(self):
        """The direction the predictive block DID cover — must still hold."""
        target = _row("decision", "brain", desc="UNIQUEBRAIN body")
        rows = [_row("fact", "heart"), target]
        out: list = []
        text = _format_pipeline_text(rows, PipelineStats(), ["fact"],
                                     emitted_out=out)
        assert "UNIQUEBRAIN" not in text
        assert (target.id, target.type) not in set(out)

    def test_graph_connected_decisions_render_unconditionally(self):
        """The direction an EARLIER revision inverted: it downgraded every
        decision-typed row, but the Graph-Connected section is ungated, so rows
        that DID reach the model were reported as filtered out."""
        # NB: the Graph-Connected bucket keys on stage_origin == "heart_graph"
        # (tools.py:814-819) — NOT "heart_graph_memory". An earlier draft of this
        # test used the latter, so the row never reached the section and the test
        # passed for the wrong reason; mutation testing is what exposed it.
        target = _row("decision", "graph_expanded", "heart_graph",
                      desc="UNIQUEHGDEC body", edge="related_to")
        out: list = []
        text = _format_pipeline_text([target], PipelineStats(), ["fact"],
                                     emitted_out=out)
        assert "UNIQUEHGDEC" in text, "this section is not scope-gated"
        assert (target.id, target.type) in set(out), "so it must be reported emitted"


class TestDuplicateEmission:
    def test_a_row_rendered_twice_is_collected_twice(self):
        """A decision found by Stage 2 AND Stage 3 renders in both the
        Graph-Connected and Brain sections — they do not cross-dedup. Consumers
        must use set/multiset semantics; a uniqueness assert would fire on
        correct output."""
        dup = _row("decision", "brain", desc="UNIQUEDUP body")
        twin = PipelineResult(
            id=dup.id, type="decision", description="UNIQUEDUP body", score=0.5,
            source="graph_expanded", edge_relation="related_to",
            metadata={"stage_origin": "heart_graph_memory"})
        out: list = []
        _format_pipeline_text([twin, dup], PipelineStats(), ["all"],
                              emitted_out=out)
        assert out.count((dup.id, "decision")) == 2
        assert len(set(out)) == 1


class TestHarnessMetric:
    """`r_at_served` is a conservation tripwire, not a sharper recall number.

    On a well-formed qrel it is IDENTICALLY recall-over-retrieved: `memory_types`
    routes retrieval to where gold lives, so gold's type is inside the scope, and
    the formatter only drops types OUTSIDE the scoped sections — a gold row can
    never be in the dropped set.
    """

    def _qr(self, **kw):
        from nous_eval.retrieval_runner import QrelResult
        base = dict(qrel_index=0, qrel_query="q", qrel_source="probe",
                    retrieved_ids=[], retrieved_types=[], rank_of_first_gold=None,
                    n_gold_in_top_k=0, n_gold_total=1)
        base.update(kw)
        return QrelResult(**base)

    def test_none_when_never_collected(self):
        """Absence is not a value — an uncollected run must not read as 0.0."""
        from nous_eval.metrics import compute_metrics
        g = uuid4()
        m = compute_metrics([self._qr(gold_ids=[g], retrieved_ids=[g],
                                      n_gold_in_top_k=1, rank_of_first_gold=1)])
        assert m.r_at_served is None

    def test_scores_gold_over_served_ids(self):
        from nous_eval.metrics import compute_metrics
        g, other = uuid4(), uuid4()
        m = compute_metrics([self._qr(gold_ids=[g], retrieved_ids=[g, other],
                                      served_ids=[g, other],
                                      n_gold_in_top_k=1, rank_of_first_gold=1)])
        assert m.r_at_served == pytest.approx(1.0)

    def test_a_gold_dropped_by_the_formatter_scores_zero(self):
        """The case the tripwire exists to catch: gold retrieved but not served."""
        from nous_eval.metrics import compute_metrics
        g, other = uuid4(), uuid4()
        m = compute_metrics([self._qr(gold_ids=[g], retrieved_ids=[g, other],
                                      served_ids=[other],
                                      n_gold_in_top_k=1, rank_of_first_gold=1)])
        assert m.r_at_served == pytest.approx(0.0)
        assert m.r_at_10 > 0.0, "and it diverges from retrieved-basis recall"

    def test_excluded_from_delta_so_none_cannot_raise(self):
        """`compute_delta` does float(getattr(...)) — None would TypeError."""
        from nous_eval.metrics import compute_delta, compute_metrics
        g = uuid4()
        m = compute_metrics([self._qr(gold_ids=[g], retrieved_ids=[g],
                                      n_gold_in_top_k=1, rank_of_first_gold=1)])
        assert m.r_at_served is None
        compute_delta(m, m)  # must not raise
