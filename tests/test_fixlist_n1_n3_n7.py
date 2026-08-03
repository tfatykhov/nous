"""Fix-list items N1, N2, N3, N7 — retrieval observability + calibration.

Each test pins the exact defect the 2026-08-02 fix list described, verified
against ``be6361e`` before implementation:

- N1: ``Heart.recall`` swallowed per-leg failures with nothing on the return
  path, so a crashed fact leg was indistinguishable from an empty one.
- N2: ``QueryExpander`` cached the degenerate ``[query]`` fail-open result,
  pinning the no-op for the whole cache TTL after one transient failure.
- N3: the adjacency boost credited ``deterministic`` structural edges that
  its sibling ``_record_recall_reactivation`` already excludes.
- N7: the eval scored a fixed top-K window production never applies.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest


# ---------------------------------------------------------------------------
# N1 — Heart.recall reports per-leg failures
# ---------------------------------------------------------------------------


def _recall_settings() -> SimpleNamespace:
    """Minimal Settings stand-in for the fields ``_recall`` reads directly."""
    return SimpleNamespace(
        heart_rrf_penalty_limit=None,
        query_expansion_enabled=False,
        mmr_enabled=False,
        mmr_skip_after_ce=True,
        mmr_diversity_weight=0.7,
        cross_encoder_enabled=False,
    )


class TestN1LegFailureReporting:
    """A caller must be able to tell a crashed leg from an empty one."""

    @pytest.mark.asyncio
    async def test_failed_leg_increments_caller_supplied_counter(self):
        from nous.heart.heart import Heart

        heart = Heart.__new__(Heart)
        heart.settings = _recall_settings()
        heart._query_expander = None
        heart._embeddings = None
        heart._residual_activator = None
        heart.agent_id = "test-agent"

        # Fact leg raises the way an out-of-date store does; episode leg is fine.
        heart.facts = SimpleNamespace(
            search=AsyncMock(side_effect=RuntimeError("UndefinedColumnError"))
        )
        heart.episodes = SimpleNamespace(search=AsyncMock(return_value=[]))

        session = MagicMock()
        session.rollback = AsyncMock()

        stage_errors: dict[str, int] = {}
        results = await heart._recall(
            "q", 10, ["episode", "fact"], session,
            owns_session=True, stage_errors=stage_errors,
        )

        assert stage_errors == {"heart_recall_fact": 1}, (
            "N1: the failed fact leg must be reported to the caller; a bare "
            "log leaves 'leg crashed' and 'leg empty' indistinguishable."
        )
        # Behaviour is unchanged — still fail-open, still returns.
        assert results == []

    @pytest.mark.asyncio
    async def test_healthy_legs_report_nothing(self):
        from nous.heart.heart import Heart

        heart = Heart.__new__(Heart)
        heart.settings = _recall_settings()
        heart._query_expander = None
        heart._embeddings = None
        heart._residual_activator = None
        heart.agent_id = "test-agent"
        heart.facts = SimpleNamespace(search=AsyncMock(return_value=[]))
        heart.episodes = SimpleNamespace(search=AsyncMock(return_value=[]))

        stage_errors: dict[str, int] = {}
        await heart._recall(
            "q", 10, ["episode", "fact"], MagicMock(),
            owns_session=True, stage_errors=stage_errors,
        )
        assert stage_errors == {}

    @pytest.mark.asyncio
    async def test_omitting_the_dict_is_byte_identical(self):
        """The parameter is optional — existing callers must be unaffected."""
        from nous.heart.heart import Heart

        heart = Heart.__new__(Heart)
        heart.settings = _recall_settings()
        heart._query_expander = None
        heart._embeddings = None
        heart._residual_activator = None
        heart.agent_id = "test-agent"
        heart.facts = SimpleNamespace(search=AsyncMock(side_effect=RuntimeError("boom")))

        session = MagicMock()
        session.rollback = AsyncMock()

        # No stage_errors passed: must not raise, must still fail open.
        results = await heart._recall(
            "q", 10, ["fact"], session, owns_session=True,
        )
        assert results == []


# ---------------------------------------------------------------------------
# N2 — failed expansions are not cached
# ---------------------------------------------------------------------------


class TestN2NoPoisonedCache:
    """One transient Haiku failure must not disable expansion permanently."""

    @pytest.mark.asyncio
    async def test_failed_expansion_is_not_cached(self, monkeypatch):
        from nous.heart.query_expansion import QueryExpander

        settings = MagicMock()
        settings.query_expansion_enabled = True
        settings.query_expansion_timeout_seconds = 2.0
        settings.query_expansion_max_variants = 3
        settings.query_expansion_min_words = 3
        settings.query_expansion_max_per_hour = 500
        settings.query_expansion_cache_ttl_days = 30

        exp = QueryExpander(
            llm=MagicMock(), settings=settings, db=None,
            model="claude-haiku-4-5-20251001", budget_check=None,
        )

        puts: list = []
        monkeypatch.setattr(
            exp, "_cache_put",
            AsyncMock(side_effect=lambda *a, **k: puts.append(a)),
        )
        monkeypatch.setattr(exp, "_cache_get", AsyncMock(return_value=None))
        # Haiku failed: returns [] exactly as on API/auth/timeout error.
        monkeypatch.setattr(exp, "_call_haiku", AsyncMock(return_value=[]))

        out = await exp.expand("how do I configure the retrieval pipeline", "a")

        assert out == ["how do I configure the retrieval pipeline"]
        assert puts == [], (
            "N2: a failed expansion must not be cached — caching [query] "
            "pins the no-op for the full TTL, so expansion stays disabled "
            "for this hash long after the fault is fixed."
        )

    @pytest.mark.asyncio
    async def test_successful_expansion_is_still_cached(self, monkeypatch):
        """The guard must not break the working path."""
        from nous.heart.query_expansion import QueryExpander

        settings = MagicMock()
        settings.query_expansion_enabled = True
        settings.query_expansion_timeout_seconds = 2.0
        settings.query_expansion_max_variants = 3
        settings.query_expansion_min_words = 3
        settings.query_expansion_max_per_hour = 500
        settings.query_expansion_cache_ttl_days = 30

        exp = QueryExpander(
            llm=MagicMock(), settings=settings, db=None,
            model="claude-haiku-4-5-20251001", budget_check=None,
        )

        puts: list = []
        monkeypatch.setattr(
            exp, "_cache_put",
            AsyncMock(side_effect=lambda *a, **k: puts.append(a)),
        )
        monkeypatch.setattr(exp, "_cache_get", AsyncMock(return_value=None))
        monkeypatch.setattr(
            exp, "_call_haiku",
            AsyncMock(return_value=["configure retrieval", "pipeline setup"]),
        )

        out = await exp.expand("how do I configure the retrieval pipeline", "a")

        assert len(out) > 1
        assert len(puts) == 1, "a real expansion must still be cached"


# ---------------------------------------------------------------------------
# N3 — adjacency boost excludes deterministic edges when flagged
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Row:
    """Stand-in for a graph_edges row tuple."""

    src: str
    tgt: str
    weight: float
    state: str | None = None

    def __getitem__(self, i):
        return (self.src, self.tgt, self.weight, self.state)[i]

    def __len__(self):
        return 4


def _brain_with_captured_sql(captured: list[str], rows: list) -> MagicMock:
    brain = MagicMock()
    brain.agent_id = "a"

    async def _execute(stmt, params):
        captured.append(str(stmt))
        res = MagicMock()
        res.all = MagicMock(return_value=rows)
        return res

    sess = MagicMock()
    sess.execute = AsyncMock(side_effect=_execute)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=sess)
    ctx.__aexit__ = AsyncMock(return_value=False)
    brain.db.session = MagicMock(return_value=ctx)
    return brain


class TestN3DeterministicEdgeFilter:
    """Match the clause the sibling reactivation query already carries."""

    @pytest.mark.asyncio
    async def test_flag_off_keeps_query_unchanged(self):
        from nous.api.retrieval_pipeline import (
            PipelineResult, _apply_graph_adjacency_boost,
        )

        captured: list[str] = []
        brain = _brain_with_captured_sql(captured, [])
        brain.settings = SimpleNamespace(
            graph_adjacency_boost_exclude_deterministic=False,
            tinyhippo_lite_enabled=False,
            tinyhippo_consolidated_boost_enabled=False,
            tinyhippo_consolidated_boost_factor=1.0,
        )
        results = [
            PipelineResult(id=uuid4(), type="fact", description="x", score=1.0),
            PipelineResult(id=uuid4(), type="fact", description="y", score=1.0),
        ]

        await _apply_graph_adjacency_boost(brain, results)

        assert len(captured) == 1
        assert "extraction_method" not in captured[0], (
            "flag OFF must be byte-identical to pre-fix behaviour"
        )

    @pytest.mark.asyncio
    async def test_flag_on_adds_the_sibling_clause(self):
        from nous.api.retrieval_pipeline import (
            PipelineResult, _apply_graph_adjacency_boost,
        )

        captured: list[str] = []
        brain = _brain_with_captured_sql(captured, [])
        brain.settings = SimpleNamespace(
            graph_adjacency_boost_exclude_deterministic=True,
            tinyhippo_lite_enabled=False,
            tinyhippo_consolidated_boost_enabled=False,
            tinyhippo_consolidated_boost_factor=1.0,
        )
        results = [
            PipelineResult(id=uuid4(), type="fact", description="x", score=1.0),
            PipelineResult(id=uuid4(), type="fact", description="y", score=1.0),
        ]

        await _apply_graph_adjacency_boost(brain, results)

        assert "extraction_method IS DISTINCT FROM 'deterministic'" in captured[0], (
            "N3: must match _record_recall_reactivation's clause verbatim"
        )

    def test_clause_matches_sibling_verbatim(self):
        """The two queries must not drift apart again."""
        import inspect
        from nous.api import retrieval_pipeline

        src = inspect.getsource(retrieval_pipeline)
        clause = "AND extraction_method IS DISTINCT FROM 'deterministic'"
        assert src.count(clause) >= 2, (
            "both the boost and the reactivation query must carry the clause"
        )

    def test_stale_spreading_activation_comment_removed(self):
        """N3 also asked for the stale cross-reference to go."""
        import inspect
        from nous.api import retrieval_pipeline

        src = inspect.getsource(retrieval_pipeline._apply_graph_adjacency_boost)
        assert "spreading_activation.py:103" not in src, (
            "the cited line is now a docstring about MAX aggregation, not a filter"
        )

    def test_flag_defaults_off(self):
        from nous.config import Settings

        assert Settings().graph_adjacency_boost_exclude_deterministic is False, (
            "measured on one frozen clone with an unestablished mechanism — "
            "land dark, per F084/F085/F086 convention"
        )


# ---------------------------------------------------------------------------
# N7 — recall@served, the k-curve, and leg visibility
# ---------------------------------------------------------------------------


def _qrel_result(retrieved, gold, legs=None, error=None):
    from nous_eval.retrieval_runner import QrelResult

    return QrelResult(
        qrel_index=0,
        qrel_query="q",
        qrel_source="hand",
        retrieved_ids=list(retrieved),
        retrieved_types=["fact"] * len(retrieved),
        retrieved_legs=list(legs or []),
        rank_of_first_gold=None,
        n_gold_in_top_k=0,
        n_gold_total=len(gold),
        gold_ids=list(gold),
        error=error,
    )


def _run_result(per_qrel, name="baseline"):
    from nous_eval.retrieval_runner import RetrievalConfig, RunResult

    return RunResult(
        config=RetrievalConfig(name=name),
        per_qrel=list(per_qrel),
        duration_seconds=1.0,
    )


class TestN7ServedWindow:
    """Production does not truncate — the metric must not either."""

    def test_recall_at_served_sees_what_top_k_misses(self):
        from nous_eval.metrics import compute_metrics

        ids = [uuid4() for _ in range(40)]
        gold = [ids[35]]  # deep in the served block, far past k=10
        m = compute_metrics([_qrel_result(ids, gold)], top_k=10)

        assert m.r_at_10 == 0.0, "the gold is outside the fixed window"
        assert m.r_at_served == 1.0, (
            "N7: recall@served must find it — recall_deep hands the model "
            "the whole block, so a top-10 metric measures a window prod "
            "never applies"
        )
        assert m.mean_served == 40.0

    def test_recall_curve_covers_the_reported_ks(self):
        from nous_eval.metrics import RECALL_CURVE_KS, compute_metrics

        ids = [uuid4() for _ in range(60)]
        gold = [ids[24]]  # rank 25 — visible at k>=40, not at k<=20
        m = compute_metrics([_qrel_result(ids, gold)], top_k=10)

        assert set(m.recall_curve) == set(RECALL_CURVE_KS)
        assert m.recall_curve[10] == 0.0
        assert m.recall_curve[20] == 0.0
        assert m.recall_curve[40] == 1.0
        assert m.recall_curve[60] == 1.0

    def test_curve_is_monotonic_non_decreasing(self):
        from nous_eval.metrics import RECALL_CURVE_KS, compute_metrics

        ids = [uuid4() for _ in range(60)]
        gold = [ids[2], ids[30], ids[50]]
        m = compute_metrics([_qrel_result(ids, gold)], top_k=10)

        vals = [m.recall_curve[k] for k in sorted(RECALL_CURVE_KS)]
        assert vals == sorted(vals), "recall cannot fall as k grows"

    def test_errored_qrels_excluded_from_served_metrics(self):
        from nous_eval.metrics import compute_metrics

        ids = [uuid4() for _ in range(5)]
        good = _qrel_result(ids, [ids[0]])
        bad = _qrel_result([], [uuid4()], error="boom")
        m = compute_metrics([good, bad], top_k=10)

        assert m.n_errored == 1
        assert m.n_qrels == 1
        assert m.r_at_served == 1.0


class TestN7LegVisibility:
    """A null from a leg below the cutline is inconclusive, not negative."""

    def test_leg_banded_below_cutline_is_invisible(self):
        from nous_eval.metrics import leg_visibility

        # Primary heart hits occupy ranks 1-10; the keyed leg is banded
        # below them at 11-20 — the shape the fix list measured.
        legs = ["heart_primary"] * 10 + ["keyed"] * 10
        ids = [uuid4() for _ in legs]
        vis = {v.leg: v for v in leg_visibility([_qrel_result(ids, [], legs)])}

        assert vis["keyed"].visible is False
        assert vis["keyed"].participation_rate == 0.0
        assert vis["keyed"].best_rank == 11
        assert vis["heart_primary"].visible is True
        assert vis["heart_primary"].participation_rate == 1.0

    def test_cutoff_is_caller_supplied(self):
        """A harness scoring deeper sees legs a shallow one cannot."""
        from nous_eval.metrics import leg_visibility

        legs = ["heart_primary"] * 10 + ["keyed"] * 10
        ids = [uuid4() for _ in legs]
        q = _qrel_result(ids, [], legs)

        assert {v.leg: v.visible for v in leg_visibility([q], cutoff=10)}["keyed"] is False
        assert {v.leg: v.visible for v in leg_visibility([q], cutoff=30)}["keyed"] is True

    def test_least_observed_leg_reported_first(self):
        from nous_eval.metrics import leg_visibility

        # exemplar sits at ranks 11-20, outside the default cutoff of 10.
        legs = ["heart_primary"] * 10 + ["exemplar"] * 10
        ids = [uuid4() for _ in legs]
        out = leg_visibility([_qrel_result(ids, [], legs)])

        assert out[0].leg == "exemplar", "least-observed legs read at the top"

    def test_cutoff_defaults_to_the_harness_scoring_window(self):
        """Default must match run_matrix's top_k — the depth nulls are
        conditioned on."""
        import inspect

        from nous_eval.metrics import leg_visibility
        from nous_eval.retrieval_runner import run_matrix

        out = leg_visibility([_qrel_result([uuid4()], [], ["heart_primary"])])
        harness_top_k = inspect.signature(run_matrix).parameters["top_k"].default
        assert out[0].cutoff == harness_top_k

    def test_errored_qrels_contribute_no_ranks(self):
        from nous_eval.metrics import leg_visibility

        bad = _qrel_result([uuid4()], [], ["keyed"], error="boom")
        assert leg_visibility([bad]) == []


class TestN7LegLabelling:
    """The runner labels rows from the pipeline's own provenance markers."""

    @pytest.mark.parametrize(
        "result_kwargs,expected",
        [
            ({"metadata": {"retrieval_leg": "keyed"}}, "keyed"),
            ({"metadata": {"retrieval_leg": "keyed_r2"}}, "keyed_r2"),
            ({"metadata": {"retrieval_leg": "exemplar"}}, "exemplar"),
            ({"metadata": {"stage_origin": "heart_graph"}}, "heart_graph"),
            ({"source": "spreading_activation"}, "spreading_activation"),
            ({"source": "brain"}, "brain"),
            ({}, "heart_primary"),
        ],
    )
    def test_leg_of_reads_existing_markers(self, result_kwargs, expected):
        from nous.api.retrieval_pipeline import PipelineResult
        from nous_eval.retrieval_runner import _leg_of

        r = PipelineResult(
            id=uuid4(), type="fact", description="x", score=0.5, **result_kwargs
        )
        assert _leg_of(r) == expected

    def test_chunk_type_is_labelled_as_the_chunk_leg(self):
        from nous.api.retrieval_pipeline import PipelineResult
        from nous_eval.retrieval_runner import _leg_of

        r = PipelineResult(id=uuid4(), type="chunk", description="x", score=0.5)
        assert _leg_of(r) == "chunk"


# ---------------------------------------------------------------------------
# Codex round 1 — the instrumentation must reach its consumers
# ---------------------------------------------------------------------------


class TestCodexP1StageErrorsReachTheReport:
    """N1's counters were computed, then discarded by the eval runner."""

    def test_qrel_result_carries_stage_errors(self):
        from nous_eval.retrieval_runner import QrelResult

        q = _qrel_result([uuid4()], [])
        assert q.stage_errors == {}, "defaults to empty, never None"

    def test_run_matrix_sums_integer_counters_and_counts_booleans(self):
        """bool is a subclass of int — the aggregation must not conflate them."""
        stats_totals: dict[str, int] = {}
        # Two qrels: booleans count occurrences, error counters accumulate.
        for flags in (
            {"graph_expansion_used": True, "stage_error_heart_recall_fact": 1},
            {"graph_expansion_used": True, "stage_error_heart_recall_fact": 3},
        ):
            for k, v in flags.items():
                if isinstance(v, bool):
                    if v:
                        stats_totals[k] = stats_totals.get(k, 0) + 1
                else:
                    stats_totals[k] = stats_totals.get(k, 0) + int(v)

        assert stats_totals["graph_expansion_used"] == 2, "booleans count qrels"
        assert stats_totals["stage_error_heart_recall_fact"] == 4, (
            "integer counters sum; counting them as 1 each would understate "
            "how much of the run was partial"
        )

    def test_stage_errors_serialized_per_qrel(self):
        from nous_eval.report import render_json

        q = _qrel_result([uuid4()], [])
        q = replace(q, stage_errors={"heart_recall_fact": 1})
        payload = json.loads(render_json([_run_result([q])], []))

        assert payload["configs"][0]["per_qrel"][0]["stage_errors"] == {
            "heart_recall_fact": 1
        }, "a crashed leg must be visible in the persisted artifact"


class TestCodexP1CutoffThreading:
    """The visibility verdict inverts with the cutoff — it must be threaded."""

    def test_report_uses_the_configured_top_k_not_the_default(self):
        from nous_eval.report import render_markdown

        legs = ["heart_primary"] * 10 + ["keyed"] * 10
        ids = [uuid4() for _ in legs]
        rr = _run_result([_qrel_result(ids, [], legs)])

        at_10 = render_markdown([rr], [], top_k=10)
        at_30 = render_markdown([rr], [], top_k=30)

        assert "observed@10" in at_10
        assert "observed@30" in at_30, (
            "a run scored at k=30 must judge legs at 30 — judging at the "
            "default 10 turns a measured null into a false 'inconclusive'"
        )

    def test_json_records_the_scoring_depth(self):
        from nous_eval.report import render_json

        payload = json.loads(
            render_json([_run_result([_qrel_result([uuid4()], [])])], [], top_k=25)
        )
        assert payload["top_k"] == 25, "the artifact must self-describe its depth"


class TestCodexP2LegProvenanceSerialized:
    """The only copy of the analysis was the ephemeral markdown."""

    def test_retrieved_legs_serialized_and_aligned(self):
        from nous_eval.report import render_json

        legs = ["heart_primary", "chunk", "keyed"]
        ids = [uuid4() for _ in legs]
        payload = json.loads(render_json([_run_result([_qrel_result(ids, [], legs)])], []))

        pq = payload["configs"][0]["per_qrel"][0]
        assert pq["retrieved_legs"] == legs
        assert len(pq["retrieved_legs"]) == len(pq["retrieved_ids"]), (
            "legs must stay 1:1 with ids so visibility is recomputable"
        )

    def test_leg_visibility_rows_serialized(self):
        from nous_eval.report import render_json

        legs = ["heart_primary"] * 10 + ["keyed"] * 10
        ids = [uuid4() for _ in legs]
        payload = json.loads(render_json([_run_result([_qrel_result(ids, [], legs)])], []))

        vis = {v["leg"]: v for v in payload["configs"][0]["leg_visibility"]}
        assert vis["keyed"]["visible"] is False
        assert vis["heart_primary"]["visible"] is True
        assert vis["keyed"]["cutoff"] == 10


# ---------------------------------------------------------------------------
# Codex round 2 — provenance ordering, depth consistency, operator surface
# ---------------------------------------------------------------------------


class TestCodexR2SpreadingClassifiedFirst:
    """Spreading rows carry BOTH source and stage_origin — order decides."""

    def test_spreading_wins_over_stage_origin(self):
        from nous.api.retrieval_pipeline import PipelineResult
        from nous_eval.retrieval_runner import _leg_of

        # Exactly what _graph_expanded_to_pipeline emits for a spreading hit.
        r = PipelineResult(
            id=uuid4(), type="fact", description="x", score=0.5,
            source="spreading_activation",
            metadata={"stage_origin": "heart_graph_memory"},
        )
        assert _leg_of(r) == "spreading_activation", (
            "checking stage_origin first silently folds every spreading row "
            "into a graph leg, so N7 could never report whether the "
            "spreading arm reached the scoring window"
        )

    def test_decision_shaped_spreading_row_also_classified(self):
        from nous.api.retrieval_pipeline import PipelineResult
        from nous_eval.retrieval_runner import _leg_of

        r = PipelineResult(
            id=uuid4(), type="decision", description="x", score=0.5,
            source="spreading_activation",
            metadata={"stage_origin": "brain_graph"},
        )
        assert _leg_of(r) == "spreading_activation"

    def test_non_spreading_graph_rows_keep_stage_origin(self):
        """The reorder must not steal rows from the graph legs."""
        from nous.api.retrieval_pipeline import PipelineResult
        from nous_eval.retrieval_runner import _leg_of

        r = PipelineResult(
            id=uuid4(), type="fact", description="x", score=0.5,
            source="graph_expanded",
            metadata={"stage_origin": "heart_graph_memory"},
        )
        assert _leg_of(r) == "heart_graph_memory"


class TestCodexR2DepthConsistency:
    """A report must not declare one depth and compute at another."""

    def test_markdown_columns_labelled_at_the_real_depth(self):
        from nous_eval.report import render_markdown

        rr = _run_result([_qrel_result([uuid4()], [])])
        md = render_markdown([rr], [], top_k=30)

        assert "P@30" in md and "R@30" in md and "nDCG@30" in md
        assert "scored at k=30" in md
        assert "| P@10 |" not in md, (
            "declaring top_k=30 while printing k=10 columns makes the "
            "report contradict the run it describes"
        )

    def test_markdown_metrics_actually_computed_at_top_k(self):
        """Not just relabelled — the numbers must change with the depth."""
        from nous_eval.metrics import compute_metrics

        # Gold at rank 25: inside k=30, outside k=10.
        ids = [uuid4() for _ in range(40)]
        q = _qrel_result(ids, [ids[24]])

        assert compute_metrics([q], top_k=10).r_at_10 == 0.0
        assert compute_metrics([q], top_k=30).r_at_10 == 1.0

        # And the rendered table must carry the depth-30 value, not the
        # default-depth one.
        from nous_eval.report import _metrics_table

        def _r_at_k(top_k: int) -> str:
            row = _metrics_table([_run_result([q])], top_k=top_k).splitlines()[-1]
            # | config | n_qrels | n_errored | MRR | P@1 | P@k | R@k | ...
            return row.split("|")[7].strip()

        assert _r_at_k(30) == "1.000", "gold at rank 25 is inside k=30"
        assert _r_at_k(10) == "0.000", (
            "and outside k=10 — if this reads 1.000 the table is being "
            "computed at a depth it does not declare"
        )

    def test_eval_runs_payload_records_depth_and_visibility(self):
        from nous_eval.retrieval import _metrics_compact

        legs = ["heart_primary"] * 10 + ["keyed"] * 10
        ids = [uuid4() for _ in legs]
        rr = _run_result([_qrel_result(ids, [], legs)])

        payload = _metrics_compact(rr, top_k=20)
        assert payload["top_k"] == 20
        assert "leg_visibility" in payload
        assert "recall_curve" in payload
        vis = {v["leg"]: v for v in payload["leg_visibility"]}
        assert vis["keyed"]["cutoff"] == 20, (
            "the persisted row is built independently of the JSON report, "
            "so it needs its own copy of the depth"
        )


class TestCodexR2OperatorSurface:
    """The markdown is what an operator reads — partial runs must show there."""

    def test_partial_run_banner_rendered(self):
        from nous_eval.report import render_markdown

        q = replace(
            _qrel_result([uuid4()], []),
            stage_errors={"heart_recall_fact": 1},
        )
        rr = _run_result([q])
        rr = replace(
            rr, pipeline_stats_summary={"stage_error_heart_recall_fact": 1}
        )
        md = render_markdown([rr], [])

        assert "Partial retrieval detected" in md
        assert "heart_recall_fact" in md
        assert "invalid for comparison" in md

    def test_banner_precedes_the_metrics_table(self):
        """An operator must see the warning before the numbers."""
        from nous_eval.report import render_markdown

        q = replace(
            _qrel_result([uuid4()], []),
            stage_errors={"heart_recall_fact": 1},
        )
        rr = replace(
            _run_result([q]),
            pipeline_stats_summary={"stage_error_heart_recall_fact": 1},
        )
        md = render_markdown([rr], [])
        assert md.index("Partial retrieval detected") < md.index(
            "Aggregate metrics"
        )

    def test_healthy_run_renders_no_banner(self):
        from nous_eval.report import render_markdown

        md = render_markdown([_run_result([_qrel_result([uuid4()], [])])], [])
        assert "Partial retrieval detected" not in md


# ---------------------------------------------------------------------------
# Codex round 3 — false alarms, wrong statistic, incomplete guard
# ---------------------------------------------------------------------------


class TestCodexR3NoFalsePartialAlarms:
    """A warning that fires on success trains operators to ignore it."""

    def test_duplicate_telemetry_is_not_an_error(self):
        """heart_graph_memory_duplicates is corroboration signal, not failure."""
        from nous_eval.retrieval_runner import _NON_ERROR_STAGE_COUNTERS

        assert "heart_graph_memory_duplicates" in _NON_ERROR_STAGE_COUNTERS

    def test_duplicates_alone_render_no_partial_banner(self):
        from nous_eval.report import render_markdown

        # A healthy graph run: duplicates recorded, no real failure.
        rr = replace(
            _run_result([_qrel_result([uuid4()], [])]),
            pipeline_stats_summary={"stage_info_heart_graph_memory_duplicates": 7},
        )
        md = render_markdown([rr], [])
        assert "Partial retrieval detected" not in md, (
            "graph corroboration must never be reported as a crashed run"
        )

    def test_real_error_still_banners_alongside_duplicates(self):
        from nous_eval.report import render_markdown

        q = replace(
            _qrel_result([uuid4()], []), stage_errors={"heart_recall_fact": 1}
        )
        rr = replace(
            _run_result([q]),
            pipeline_stats_summary={
                "stage_info_heart_graph_memory_duplicates": 7,
                "stage_error_heart_recall_fact": 1,
            },
        )
        md = render_markdown([rr], [])
        assert "Partial retrieval detected" in md
        assert "heart_recall_fact" in md
        assert "duplicates" not in md.split("## Aggregate")[0]


class TestCodexR3ParticipationNotMedian:
    """A leg's own tail must not hide its head."""

    def test_leg_with_scoring_head_and_long_tail_is_visible(self):
        from nous_eval.metrics import leg_visibility

        # chunk places rank 1 on every qrel, then a long tail at 20-30.
        legs = ["chunk"] + ["heart_primary"] * 18 + ["chunk"] * 11
        ids = [uuid4() for _ in legs]
        vis = {v.leg: v for v in leg_visibility([_qrel_result(ids, [], legs)])}

        assert vis["chunk"].median_rank > 10, "pooled median IS below the cutline"
        assert vis["chunk"].visible is True, (
            "but its rank-1 row scores on every qrel — median-of-all-rows "
            "would wrongly tell operators to discount this leg's null"
        )
        assert vis["chunk"].participation_rate == 1.0

    def test_participation_rate_is_per_qrel(self):
        from nous_eval.metrics import leg_visibility

        # keyed reaches the window on 1 of 2 qrels.
        hit = ["keyed"] + ["heart_primary"] * 19
        miss = ["heart_primary"] * 19 + ["keyed"]
        qs = [
            _qrel_result([uuid4() for _ in hit], [], hit),
            _qrel_result([uuid4() for _ in miss], [], miss),
        ]
        vis = {v.leg: v for v in leg_visibility(qs)}

        assert vis["keyed"].n_qrels_present == 2
        assert vis["keyed"].n_qrels_within_cutoff == 1
        assert vis["keyed"].participation_rate == 0.5

    def test_report_surfaces_participation(self):
        from nous_eval.report import render_markdown

        legs = ["heart_primary"] * 10 + ["keyed"] * 10
        ids = [uuid4() for _ in legs]
        md = render_markdown([_run_result([_qrel_result(ids, [], legs)])], [])
        assert "participation" in md


class TestCodexR3ExpansionGuardComplete:
    """cleaned non-empty does not mean the fusion produced a variant."""

    @pytest.mark.asyncio
    async def test_echoed_query_is_not_cached(self, monkeypatch):
        from nous.heart.query_expansion import QueryExpander

        settings = MagicMock()
        settings.query_expansion_enabled = True
        settings.query_expansion_timeout_seconds = 2.0
        settings.query_expansion_max_variants = 3
        settings.query_expansion_min_words = 3
        settings.query_expansion_max_per_hour = 500
        settings.query_expansion_cache_ttl_days = 30

        exp = QueryExpander(
            llm=MagicMock(), settings=settings, db=None,
            model="claude-haiku-4-5-20251001", budget_check=None,
        )
        puts: list = []
        monkeypatch.setattr(
            exp, "_cache_put", AsyncMock(side_effect=lambda *a, **k: puts.append(a))
        )
        monkeypatch.setattr(exp, "_cache_get", AsyncMock(return_value=None))
        # Haiku echoes the query back with different case — cleaned is
        # NON-empty, but _fuse dedupes on lower().strip() so final == [query].
        monkeypatch.setattr(
            exp, "_call_haiku",
            AsyncMock(return_value=["How Do I Configure The Retrieval Pipeline"]),
        )

        out = await exp.expand("how do i configure the retrieval pipeline", "a")

        assert out == ["how do i configure the retrieval pipeline"]
        assert puts == [], (
            "a case-only echo fuses back to the bare query — caching it "
            "pins the no-op for the whole TTL, the exact failure N2 fixes"
        )


# ---------------------------------------------------------------------------
# N8 — the bands are a deliberate choice, not a unit mismatch
# ---------------------------------------------------------------------------


class TestN8BandsDocumented:
    def test_band_constants_record_the_distribution(self):
        from nous.config import Settings

        for fname in ("keyed_fact_leg_score", "exemplar_leg_score"):
            desc = Settings.model_fields[fname].description or ""
            assert "percentile" in desc.lower() or "p10" in desc.lower(), (
                f"N8: {fname} must document where 0.55 sits in the observed "
                "DISTRIBUTION, not just on the [0,1] scale"
            )
