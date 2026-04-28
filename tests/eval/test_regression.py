"""F056 PR #0: tests for nous_eval/regression.py per-harness extensibility."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from nous_eval.regression import (
    _ALL_REPORTED_METRICS_BY_HARNESS,
    _PRIMARY_METRIC_BY_HARNESS,
    _bucketize,
    _Comparison,
    _compare_bucket,
    _format_report,
    _parse_args,
    _primary_metric_for,
    _reported_metrics_for,
    _RunRow,
    _validate_harness,
    _validate_primary_metric,
)


# ---------------------------------------------------------------------------
# Registry lookups (F056)
# ---------------------------------------------------------------------------


class TestPrimaryMetricRegistry:
    def test_known_harness_returns_registered(self):
        assert _primary_metric_for("retrieval") == "mrr"
        assert _primary_metric_for("multi_turn_eval") == "mrr"
        assert _primary_metric_for("admission") == "admission_f1"
        assert _primary_metric_for("dedup") == "dedup_f1"
        assert _primary_metric_for("backfill") == "edge_precision"
        assert _primary_metric_for("summary") == "summary_quality"

    def test_unknown_harness_falls_back_to_retrieval(self):
        # Defensive: if a future harness isn't registered, we don't crash.
        assert _primary_metric_for("totally-new-harness") == "mrr"


class TestReportedMetricsRegistry:
    def test_admission_excludes_confusion_matrix(self):
        # F056 v4 design: confusion_matrix is a struct sub-payload, NOT a
        # delta-table scalar. Must not appear in the registry tuple.
        assert "confusion_matrix" not in _ALL_REPORTED_METRICS_BY_HARNESS["admission"]
        assert _ALL_REPORTED_METRICS_BY_HARNESS["admission"] == (
            "admission_f1", "admission_precision", "admission_recall",
        )

    def test_all_handler_harnesses_registered(self):
        # Every handler from F056 spec must have a primary AND a reported set.
        for harness in ("admission", "dedup", "backfill", "summary"):
            assert harness in _PRIMARY_METRIC_BY_HARNESS
            assert harness in _ALL_REPORTED_METRICS_BY_HARNESS
            # Primary metric must be in the reported set.
            assert _PRIMARY_METRIC_BY_HARNESS[harness] in _ALL_REPORTED_METRICS_BY_HARNESS[harness]


# ---------------------------------------------------------------------------
# _validate_primary_metric (F056)
# ---------------------------------------------------------------------------


class TestValidatePrimaryMetric:
    def test_auto_resolves_from_harness(self):
        assert _validate_primary_metric("auto", "admission") == "admission_f1"
        assert _validate_primary_metric("auto", "retrieval") == "mrr"

    def test_auto_with_no_harness_returns_auto(self):
        # Multi-harness reports defer per-row resolution.
        assert _validate_primary_metric("auto", None) == "auto"

    def test_explicit_metric_in_any_registry_accepted(self):
        # Cross-harness comparison: a user might gate retrieval on the dedup
        # leg primary out of curiosity. Not blocked.
        assert _validate_primary_metric("dedup_f1", "retrieval") == "dedup_f1"
        assert _validate_primary_metric("mrr", "admission") == "mrr"

    def test_unknown_metric_raises(self):
        # F056 PR #0 fix: ValueError (was ArgumentTypeError, but that only
        # renders cleanly when raised from a `type=` callable; we raise post-
        # parse and the parser.error() call translates to a clean CLI exit).
        with pytest.raises(ValueError, match="not_a_real_metric"):
            _validate_primary_metric("not_a_real_metric", "admission")


class TestValidateHarness:
    """F056 PR #0: --harness must reject typos.

    Without validation, a typo like `--harness adminssion` parses cleanly,
    matches no rows, exits 0 with "no comparable runs found" — silently
    passing weekly cron forever. Devil's advocate review of PR #371 caught
    this.
    """

    def test_known_harness_accepted(self):
        for h in _PRIMARY_METRIC_BY_HARNESS:
            assert _validate_harness(h) == h

    def test_none_returns_none(self):
        assert _validate_harness(None) is None

    def test_typo_raises_with_helpful_message(self):
        with pytest.raises(ValueError, match="adminssion"):
            _validate_harness("adminssion")

    def test_parse_args_translates_typo_to_clean_exit(self, capsys):
        # parser.error() should give "prog: error: ..." and SystemExit(2),
        # NOT a Python traceback (was the prior ArgumentTypeError bug).
        with pytest.raises(SystemExit) as excinfo:
            _parse_args(["--harness", "adminssion"])
        assert excinfo.value.code == 2
        captured = capsys.readouterr()
        assert "adminssion" in captured.err
        assert "Known:" in captured.err


# ---------------------------------------------------------------------------
# _compare_bucket per-harness (F056)
# ---------------------------------------------------------------------------


def _row(harness: str, ts_offset_h: float, metrics: dict[str, float]) -> _RunRow:
    return _RunRow(
        created_at=datetime.now(tz=timezone.utc) + timedelta(hours=ts_offset_h),
        git_sha="abc123",
        harness=harness,
        config_name="baseline",
        metrics=metrics,
        report_path=None,
    )


class TestCompareBucketPerHarness:
    def test_admission_uses_admission_f1_as_primary(self):
        baseline = _row("admission", -48, {"admission_f1": 0.90, "admission_precision": 0.92, "admission_recall": 0.88})
        latest   = _row("admission", 0,   {"admission_f1": 0.80, "admission_precision": 0.85, "admission_recall": 0.75})
        comp = _compare_bucket(
            [baseline, latest],
            threshold=0.05, primary_metric="auto", min_baseline_age_hours=12,
        )
        assert comp is not None
        assert comp.primary_metric == "admission_f1"
        assert comp.is_regression  # 0.80 - 0.90 = -0.10, exceeds 0.05 threshold
        assert "admission_f1" in comp.regressions

    def test_retrieval_uses_mrr_as_primary(self):
        baseline = _row("retrieval", -48, {"mrr": 0.90, "r_at_10": 0.50, "p_at_1": 0.85, "ndcg_at_10": 0.60})
        latest   = _row("retrieval", 0,   {"mrr": 0.85, "r_at_10": 0.50, "p_at_1": 0.85, "ndcg_at_10": 0.60})
        comp = _compare_bucket(
            [baseline, latest],
            threshold=0.03, primary_metric="auto", min_baseline_age_hours=12,
        )
        assert comp is not None
        assert comp.primary_metric == "mrr"
        assert comp.is_regression  # -0.05 < -0.03

    def test_no_regression_when_primary_within_threshold(self):
        baseline = _row("admission", -48, {"admission_f1": 0.90, "admission_precision": 0.92, "admission_recall": 0.88})
        latest   = _row("admission", 0,   {"admission_f1": 0.88, "admission_precision": 0.90, "admission_recall": 0.86})
        comp = _compare_bucket(
            [baseline, latest],
            threshold=0.05, primary_metric="auto", min_baseline_age_hours=12,
        )
        assert comp is not None
        assert not comp.is_regression  # 0.88 - 0.90 = -0.02, within 0.05

    def test_deltas_cover_all_reported_metrics(self):
        baseline = _row("dedup", -48, {"dedup_f1": 0.85, "dedup_f1_leg1": 0.88, "dedup_f1_leg2": 0.82})
        latest   = _row("dedup", 0,   {"dedup_f1": 0.83, "dedup_f1_leg1": 0.86, "dedup_f1_leg2": 0.80})
        comp = _compare_bucket(
            [baseline, latest],
            threshold=0.05, primary_metric="auto", min_baseline_age_hours=12,
        )
        assert comp is not None
        # All 3 dedup metrics surface in deltas (not just primary).
        assert set(comp.deltas) == {"dedup_f1", "dedup_f1_leg1", "dedup_f1_leg2"}

    def test_no_baseline_returns_comparison_with_None_baseline(self):
        latest = _row("admission", 0, {"admission_f1": 0.90, "admission_precision": 0.92, "admission_recall": 0.88})
        comp = _compare_bucket(
            [latest],
            threshold=0.05, primary_metric="auto", min_baseline_age_hours=12,
        )
        assert comp is not None
        assert comp.baseline is None
        assert not comp.is_regression


# ---------------------------------------------------------------------------
# Per-harness report formatting (F056)
# ---------------------------------------------------------------------------


class TestFormatReportPerHarness:
    def test_admission_report_has_admission_headers(self):
        baseline = _row("admission", -48, {"admission_f1": 0.90, "admission_precision": 0.92, "admission_recall": 0.88})
        latest   = _row("admission", 0,   {"admission_f1": 0.80, "admission_precision": 0.85, "admission_recall": 0.75})
        comp = _compare_bucket(
            [baseline, latest],
            threshold=0.05, primary_metric="auto", min_baseline_age_hours=12,
        )
        report = _format_report([comp], threshold=0.05, primary_metric="auto", days=7)
        # Headers must be admission-specific, NOT MRR/R@10.
        assert "latest_admission_f1" in report
        assert "d_admission_precision" in report
        assert "## admission" in report
        assert "REGRESSION" in report

    def test_multi_harness_report_has_separate_sections(self):
        retrieval_b = _row("retrieval", -48, {"mrr": 0.90, "r_at_10": 0.50, "p_at_1": 0.85, "ndcg_at_10": 0.60})
        retrieval_l = _row("retrieval", 0,   {"mrr": 0.90, "r_at_10": 0.50, "p_at_1": 0.85, "ndcg_at_10": 0.60})
        admission_b = _row("admission", -48, {"admission_f1": 0.90, "admission_precision": 0.92, "admission_recall": 0.88})
        admission_l = _row("admission", 0,   {"admission_f1": 0.92, "admission_precision": 0.93, "admission_recall": 0.90})

        c1 = _compare_bucket(
            [retrieval_b, retrieval_l],
            threshold=0.03, primary_metric="auto", min_baseline_age_hours=12,
        )
        c2 = _compare_bucket(
            [admission_b, admission_l],
            threshold=0.05, primary_metric="auto", min_baseline_age_hours=12,
        )
        report = _format_report([c1, c2], threshold=0.05, primary_metric="auto", days=7)

        assert "## admission" in report
        assert "## retrieval" in report
        # Each section should have its own headers.
        assert "latest_admission_f1" in report
        assert "latest_mrr" in report

    def test_no_baseline_renders_new_marker(self):
        latest = _row("backfill", 0, {"edge_precision": 0.85, "orphan_resolution_rate": 0.6, "density_delta": 42})
        comp = _compare_bucket(
            [latest],
            threshold=0.10, primary_metric="auto", min_baseline_age_hours=12,
        )
        report = _format_report([comp], threshold=0.10, primary_metric="auto", days=7)
        assert "_new_" in report
        assert "_no baseline_" in report


# ---------------------------------------------------------------------------
# Legacy-row backwards compat (F056 PR #0)
# ---------------------------------------------------------------------------


class TestLegacyRowFallback:
    def test_reported_metrics_for_unknown_harness_falls_back_to_retrieval(self):
        # Pre-PR-#368 rows lacked `harness` key; _fetch_rows defaults to
        # "retrieval" — verified separately in integration tests. Here we
        # confirm the unknown-harness lookup path is safe.
        assert _reported_metrics_for("totally-new") == _ALL_REPORTED_METRICS_BY_HARNESS["retrieval"]

    def test_compare_bucket_with_unknown_harness_uses_retrieval_metrics(self):
        # If a row's harness isn't in the registry, we fall back to retrieval
        # set rather than crashing or returning empty deltas.
        baseline = _row("legacy-retrieval", -48, {"mrr": 0.90, "r_at_10": 0.50, "p_at_1": 0.85, "ndcg_at_10": 0.60})
        latest   = _row("legacy-retrieval", 0,   {"mrr": 0.80, "r_at_10": 0.40, "p_at_1": 0.75, "ndcg_at_10": 0.55})
        comp = _compare_bucket(
            [baseline, latest],
            threshold=0.03, primary_metric="auto", min_baseline_age_hours=12,
        )
        assert comp is not None
        assert comp.is_regression
        assert "mrr" in comp.deltas
