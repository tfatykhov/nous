"""Tests for F024 Phase 3b correlation engine."""
import pytest


class TestPearsonCorrelation:
    def test_perfect_positive(self):
        from nous.cognitive.correlation import pearson_r
        r = pearson_r([1, 2, 3, 4, 5], [2, 4, 6, 8, 10])
        assert abs(r - 1.0) < 0.001

    def test_perfect_negative(self):
        from nous.cognitive.correlation import pearson_r
        r = pearson_r([1, 2, 3, 4, 5], [10, 8, 6, 4, 2])
        assert abs(r - (-1.0)) < 0.001

    def test_no_correlation(self):
        from nous.cognitive.correlation import pearson_r
        r = pearson_r([1, 2, 3, 4, 5], [3, 1, 4, 1, 5])
        assert abs(r) < 0.5

    def test_too_few_samples(self):
        from nous.cognitive.correlation import pearson_r
        r = pearson_r([1], [2])
        assert r == 0.0

    def test_constant_values(self):
        from nous.cognitive.correlation import pearson_r
        r = pearson_r([5, 5, 5], [1, 2, 3])
        assert r == 0.0


class TestSpearmanCorrelation:
    def test_perfect_positive(self):
        from nous.cognitive.correlation import spearman_rho
        rho = spearman_rho([1, 2, 3, 4, 5], [2, 4, 6, 8, 10])
        assert abs(rho - 1.0) < 0.001

    def test_monotonic_nonlinear(self):
        from nous.cognitive.correlation import spearman_rho
        rho = spearman_rho([1, 2, 3, 4, 5], [1, 4, 9, 16, 25])
        assert abs(rho - 1.0) < 0.001


class TestDimensionOutcomeCorrelation:
    def test_correlate_dimensions_with_outcomes(self):
        from nous.cognitive.correlation import correlate_dimensions_with_outcomes

        episodes = [
            {"scores": {"Recall": 8, "Tool Selection": 5}, "signals": ["completed"]},
            {"scores": {"Recall": 3, "Tool Selection": 7}, "signals": ["corrected"]},
            {"scores": {"Recall": 9, "Tool Selection": 6}, "signals": ["completed", "praised"]},
            {"scores": {"Recall": 4, "Tool Selection": 4}, "signals": ["corrected", "reworked"]},
            {"scores": {"Recall": 7, "Tool Selection": 8}, "signals": ["completed"]},
        ]
        result = correlate_dimensions_with_outcomes(episodes, ["Recall", "Tool Selection"])
        assert len(result) > 0
        assert all(hasattr(r, "dimension") for r in result)
        assert all(hasattr(r, "pearson_r") for r in result)


class TestWeightSuggestion:
    def test_suggest_weights_from_correlations(self):
        from nous.cognitive.correlation import suggest_weights
        from nous.cognitive.rubric_schemas import CorrelationResult

        correlations = [
            CorrelationResult(dimension="Recall", signal_type="completed", pearson_r=0.7, spearman_rho=0.65, sample_size=50),
            CorrelationResult(dimension="Tool Selection", signal_type="completed", pearson_r=0.3, spearman_rho=0.25, sample_size=50),
            CorrelationResult(dimension="Confidence Calibration", signal_type="completed", pearson_r=0.5, spearman_rho=0.45, sample_size=50),
            CorrelationResult(dimension="Proactivity", signal_type="completed", pearson_r=0.2, spearman_rho=0.15, sample_size=50),
        ]
        current_weights = {"Recall": 0.25, "Tool Selection": 0.25, "Confidence Calibration": 0.25, "Proactivity": 0.25}
        suggested = suggest_weights(correlations, current_weights, cap=0.05)
        assert abs(sum(suggested.values()) - 1.0) < 0.01
        assert suggested["Recall"] >= 0.25
        for w in suggested.values():
            assert 0.10 <= w <= 0.40


class TestSplitDetection:
    def test_detect_split_candidate(self):
        from nous.cognitive.correlation import detect_split_candidates
        from nous.cognitive.rubric_schemas import CorrelationResult

        correlations = [
            CorrelationResult(dimension="Tool Selection", signal_type="completed", pearson_r=0.8, spearman_rho=0.75, sample_size=50),
            CorrelationResult(dimension="Tool Selection", signal_type="corrected", pearson_r=0.2, spearman_rho=0.15, sample_size=50),
        ]
        candidates = detect_split_candidates(correlations, threshold=0.3)
        assert "Tool Selection" in candidates


class TestMergeDetection:
    def test_detect_merge_candidate(self):
        from nous.cognitive.correlation import detect_merge_candidates

        dim_profiles = {
            "Recall": [0.7, 0.3, 0.5],
            "Memory Hygiene": [0.72, 0.28, 0.48],
            "Tool Selection": [0.2, 0.8, 0.1],
        }
        merges = detect_merge_candidates(dim_profiles, threshold=0.85)
        assert ("Recall", "Memory Hygiene") in merges or ("Memory Hygiene", "Recall") in merges


class TestNormalizeWeights:
    def test_iterative_normalization_respects_bounds(self):
        """Regression: single-pass normalize can violate max_weight."""
        from nous.cognitive.correlation import _normalize_weights
        # 3 dims: one at ceiling, others at floor → normalize pushes ceiling higher
        weights = {"A": 0.40, "B": 0.10, "C": 0.10}
        result = _normalize_weights(weights, min_w=0.10, max_w=0.40)
        assert abs(sum(result.values()) - 1.0) < 0.01
        for w in result.values():
            assert 0.10 <= w <= 0.40 + 0.001  # small tolerance for rounding

    def test_equal_weights_unchanged(self):
        from nous.cognitive.correlation import _normalize_weights
        weights = {"A": 0.25, "B": 0.25, "C": 0.25, "D": 0.25}
        result = _normalize_weights(weights)
        assert abs(sum(result.values()) - 1.0) < 0.01
