"""F056 PR #4: unit tests for the summary handler eval CLI."""
from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from nous_eval.handlers._jsonl import load_jsonl
from nous_eval.handlers._models import SummaryRow
from nous_eval.handlers.summary import (
    _AGENT_ID,
    _settings_with_summary_overrides,
    compute_summary_quality,
    filter_rows,
)


# ---------------------------------------------------------------------------
# SummaryRow schema (F056 §D)
# ---------------------------------------------------------------------------


_VALID_TRANSCRIPT = (
    "User: This is a long enough transcript for the F051.5 short-transcript "
    "skip floor at episode_summarizer.py:130 (>= 50 chars)."
)


class TestSummaryRowSchema:
    def test_minimal_valid_row(self):
        r = SummaryRow(
            row_id="s1",
            transcript=_VALID_TRANSCRIPT,
            gold_key_points=["Some claim that the summary should surface."],
        )
        assert r.row_id == "s1"
        assert r.gold_summary_themes == []
        assert r.question_type is None

    def test_short_transcript_rejected(self):
        # episode_summarizer.py:130 short-transcript skip floor; schema enforces.
        with pytest.raises(ValidationError):
            SummaryRow(
                row_id="s1",
                transcript="too short",
                gold_key_points=["claim"],
            )

    def test_empty_gold_rejected(self):
        with pytest.raises(ValidationError):
            SummaryRow(
                row_id="s1",
                transcript=_VALID_TRANSCRIPT,
                gold_key_points=[],
            )


# ---------------------------------------------------------------------------
# Real fixture loads cleanly + 6-question-type stratification
# ---------------------------------------------------------------------------


class TestRealFixtureLoads:
    def test_full_fixture_validates(self):
        path = Path("tests/fixtures/handlers/summary_transcripts.jsonl")
        if not path.exists():
            pytest.skip(f"fixture not present at {path}")
        rows = load_jsonl(path, SummaryRow)
        # Spec §D mandates N=80 (raised from v1 N=20 for statistical sensitivity)
        assert len(rows) == 80
        # All reviewed by tim per spec §D
        unreviewed = [r.row_id for r in rows if not r.reviewed_by]
        assert unreviewed == [], f"unreviewed: {unreviewed}"
        # All 6 LongMemEval question types must be represented (~13 each)
        from collections import Counter
        counts = Counter(r.question_type for r in rows)
        expected_types = {
            "knowledge-update", "multi-session", "single-session-assistant",
            "single-session-preference", "single-session-user", "temporal-reasoning",
        }
        assert set(counts.keys()) == expected_types
        for qt in expected_types:
            assert 10 <= counts[qt] <= 20, f"{qt}: {counts[qt]} out of expected 10-20"

    def test_each_row_has_at_least_one_gold_key_point(self):
        path = Path("tests/fixtures/handlers/summary_transcripts.jsonl")
        if not path.exists():
            pytest.skip(f"fixture not present at {path}")
        rows = load_jsonl(path, SummaryRow)
        empty_gold = [r.row_id for r in rows if not r.gold_key_points]
        assert empty_gold == [], f"rows with empty gold_key_points: {empty_gold}"


# ---------------------------------------------------------------------------
# filter_rows
# ---------------------------------------------------------------------------


class TestFilterRows:
    def test_default_skips_unreviewed(self):
        rs = [
            SummaryRow(row_id="s1", transcript=_VALID_TRANSCRIPT, gold_key_points=["x"], reviewed_by="tim"),
            SummaryRow(row_id="s2", transcript=_VALID_TRANSCRIPT, gold_key_points=["x"], reviewed_by=None),
        ]
        filtered = filter_rows(rs, include_unreviewed=False)
        assert len(filtered) == 1
        assert filtered[0].row_id == "s1"

    def test_include_unreviewed_keeps_all(self):
        rs = [
            SummaryRow(row_id="s1", transcript=_VALID_TRANSCRIPT, gold_key_points=["x"], reviewed_by="tim"),
            SummaryRow(row_id="s2", transcript=_VALID_TRANSCRIPT, gold_key_points=["x"], reviewed_by=None),
        ]
        assert len(filter_rows(rs, include_unreviewed=True)) == 2


# ---------------------------------------------------------------------------
# compute_summary_quality (F056 §D formula)
# ---------------------------------------------------------------------------


class TestComputeSummaryQuality:
    def test_perfect(self):
        quality, mean_kpc, mean_sf = compute_summary_quality([1.0, 1.0], [1.0, 1.0])
        assert quality == 1.0
        assert mean_kpc == 1.0
        assert mean_sf == 1.0

    def test_kpc_zero_collapses_quality(self):
        # F056 spec §D formula: quality = mean_kpc * mean_sf. If model
        # missed every key point, quality = 0 even with perfect faithfulness.
        quality, mean_kpc, mean_sf = compute_summary_quality([0.0, 0.0], [1.0, 1.0])
        assert quality == 0.0
        assert mean_kpc == 0.0
        assert mean_sf == 1.0

    def test_sf_zero_collapses_quality(self):
        # Equivalently: hallucinations destroy quality even with perfect coverage.
        quality, mean_kpc, mean_sf = compute_summary_quality([1.0, 1.0], [0.0, 0.0])
        assert quality == 0.0
        assert mean_kpc == 1.0
        assert mean_sf == 0.0

    def test_partial(self):
        # 0.5 coverage * 0.8 faithfulness = 0.4 quality
        quality, mean_kpc, mean_sf = compute_summary_quality([0.5], [0.8])
        assert mean_kpc == 0.5
        assert mean_sf == 0.8
        assert quality == pytest.approx(0.4)

    def test_empty_inputs_returns_zeros(self):
        # Avoid 0/0; spec §D treats empty as null result.
        quality, mean_kpc, mean_sf = compute_summary_quality([], [])
        assert (quality, mean_kpc, mean_sf) == (0.0, 0.0, 0.0)

    def test_uneven_lists_still_returns_zeros(self):
        # Defensive: caller mismatch shouldn't crash.
        quality, mean_kpc, mean_sf = compute_summary_quality([1.0], [])
        assert (quality, mean_kpc, mean_sf) == (0.0, 0.0, 0.0)


# ---------------------------------------------------------------------------
# Settings overrides (F056 §D)
# ---------------------------------------------------------------------------


class TestSettingsOverrides:
    def test_agent_id_set_to_handler_scope(self):
        from nous.config import Settings
        base = Settings()
        overridden = _settings_with_summary_overrides(base)
        assert overridden.agent_id == _AGENT_ID


# ---------------------------------------------------------------------------
# Constructor signature regression (F056 PR #4 v2 review caught this)
# ---------------------------------------------------------------------------


class TestEpisodeSummarizerConstructorContract:
    """Prevents PR #4 v1 regression: missing `brain` arg → TypeError at runtime.

    The eval handler instantiates EpisodeSummarizer in `_run_summary_eval`.
    The constructor signature requires `brain: Brain | None` as positional
    (no default). v1 omitted it; v2 adds `brain=None` explicitly. This
    test asserts the contract independent of the runtime path so future
    refactors that drop the arg fail loudly at test time, not silently
    at the first eval invocation.
    """

    def test_episode_summarizer_constructor_accepts_eval_kwargs(self):
        # Use sig.bind to verify the EXACT kwarg set _run_summary_eval
        # uses still satisfies EpisodeSummarizer.__init__. This is stronger
        # than a "param exists" check: bind() raises TypeError if any
        # required arg is missing OR if any kwarg is unexpected. Future
        # regressions in either direction (eval drops brain=, or constructor
        # adds a new required arg) fail at test time instead of at runtime.
        import inspect
        from nous.handlers.episode_summarizer import EpisodeSummarizer

        sig = inspect.signature(EpisodeSummarizer.__init__)
        # Bind exactly what summary.py:_run_summary_eval passes.
        # Use sentinels — no real DB/heart needed.
        try:
            sig.bind(
                self=object(),  # __init__ takes self positionally
                heart=object(),
                brain=None,
                settings=object(),
                bus=None,
                llm_client=object(),
            )
        except TypeError as exc:
            raise AssertionError(
                f"EpisodeSummarizer.__init__ signature has drifted from what "
                f"summary.py::_run_summary_eval passes: {exc}. Update either "
                f"the eval call site or the constructor — but the two MUST "
                f"stay in sync, or the eval crashes at first row with no "
                f"unit-test signal."
            ) from exc
