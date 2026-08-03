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

from dataclasses import dataclass, field
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
        assert vis["keyed"].best_rank == 11
        assert vis["heart_primary"].visible is True

    def test_cutoff_is_caller_supplied(self):
        """A harness scoring deeper sees legs a shallow one cannot."""
        from nous_eval.metrics import leg_visibility

        legs = ["heart_primary"] * 10 + ["keyed"] * 10
        ids = [uuid4() for _ in legs]
        q = _qrel_result(ids, [], legs)

        assert {v.leg: v.visible for v in leg_visibility([q], cutoff=10)}["keyed"] is False
        assert {v.leg: v.visible for v in leg_visibility([q], cutoff=30)}["keyed"] is True

    def test_deepest_leg_reported_first(self):
        from nous_eval.metrics import leg_visibility

        legs = ["heart_primary"] * 5 + ["exemplar"] * 5
        ids = [uuid4() for _ in legs]
        out = leg_visibility([_qrel_result(ids, [], legs)])

        assert out[0].leg == "exemplar", "least-visible legs read at the top"

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
