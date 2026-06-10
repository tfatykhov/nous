"""Tests for the edge-audit per-relation regression check (EXEC-PLAN 2.3).

Audit 2026-05-03 found that between Apr 26 and Apr 30, evidence_for
jumped 0.53 → 0.75 (good) while related_to fell 0.83 → 0.70 (bad).
The aggregate gate masks this — a regression on one relation can be
hidden by an improvement on another. These tests pin the regression
detector so a future change can't silently revert this.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from nous.brain._entity_config import _ENTITY_CONFIG
from nous_eval.run_edge_audit import (
    _CONTENT_BY_TYPE,
    _autodetect_prior_baseline,
    _check_regressions,
    _load_prior_precisions,
)


@pytest.fixture
def tmpreports():
    with tempfile.TemporaryDirectory() as d:
        yield Path(d)


class TestContentMappingSharedWithDensifier:
    """#354: the audit must read EXACTLY what the densifier reads — the two
    mappings drifted once (audit read decisions.context, densifier reads
    description) and F054 was tuned on the resulting noise."""

    def test_mapping_derived_from_entity_config(self):
        assert set(_CONTENT_BY_TYPE) == set(_ENTITY_CONFIG)
        for etype, (table, _tn, content_expr, _extra) in _ENTITY_CONFIG.items():
            assert _CONTENT_BY_TYPE[etype] == (table, content_expr), (
                f"audit content mapping for '{etype}' drifted from _ENTITY_CONFIG"
            )

    def test_decision_reads_description_not_context(self):
        table, expr = _CONTENT_BY_TYPE["decision"]
        assert table == "brain.decisions"
        assert "description" in expr
        assert "context" not in expr

    def test_chunk_type_present(self):
        # The old hand-rolled mapping lacked chunk — chunk edges were
        # silently dropped from every audit.
        assert "chunk" in _CONTENT_BY_TYPE


def _make_baseline(path: Path, precisions: dict[str, float], n: int = 30) -> None:
    payload = {
        "generated_utc": "2026-05-04T00:00:00+00:00",
        "since": None,
        "limit_per_type": n,
        "threshold": 0.75,
        "relations": [
            {"relation": r, "n": n, "yes": int(p * n), "weak": 0,
             "no": n - int(p * n), "parse_error": 0, "precision": p}
            for r, p in sorted(precisions.items())
        ],
    }
    path.write_text(json.dumps(payload, indent=2))


class TestLoadPriorPrecisions:
    def test_missing_file_returns_empty(self, tmpreports):
        assert _load_prior_precisions(tmpreports / "nope.json") == {}

    def test_loads_relations_from_json(self, tmpreports):
        path = tmpreports / "edge-audit-prior.json"
        _make_baseline(path, {"evidence_for": 0.83, "related_to": 0.83})
        out = _load_prior_precisions(path)
        assert out == {"evidence_for": 0.83, "related_to": 0.83}

    def test_malformed_json_returns_empty(self, tmpreports):
        path = tmpreports / "bad.json"
        path.write_text("not json")
        assert _load_prior_precisions(path) == {}


class TestCheckRegressions:
    def test_no_regression_when_within_tolerance(self):
        prior = {"evidence_for": 0.83, "related_to": 0.83}
        # related_to drops 0.03; below 0.05 tolerance — not a regression
        current = {"evidence_for": 0.85, "related_to": 0.80}
        regs = _check_regressions(current, prior, max_regression=0.05)
        assert regs == []

    def test_catches_audit_2026_05_03_pattern(self):
        """Pin the exact pattern the audit flagged: evidence_for up,
        related_to down beyond tolerance.
        """
        prior = {"evidence_for": 0.53, "related_to": 0.83, "informed_by": 0.69}
        # Apr 30 actuals: evidence_for improved to 0.75, related_to fell
        # to 0.70 (which IS the regression the audit caught).
        current = {"evidence_for": 0.75, "related_to": 0.70, "informed_by": 0.70}
        regs = _check_regressions(current, prior, max_regression=0.05)
        # related_to dropped 0.13 — should be caught
        rel_names = [r[0] for r in regs]
        assert "related_to" in rel_names
        assert "evidence_for" not in rel_names  # improvement
        assert "informed_by" not in rel_names   # marginal increase
        # Verify the delta is correctly computed
        rt = next(r for r in regs if r[0] == "related_to")
        _, prior_p, current_p, delta = rt
        assert prior_p == 0.83
        assert current_p == 0.70
        assert delta == pytest.approx(-0.13, abs=1e-9)

    def test_new_relation_in_current_not_a_regression(self):
        """A relation that didn't exist in the prior baseline can't
        regress — caller may have added a new edge type."""
        prior = {"evidence_for": 0.80}
        current = {"evidence_for": 0.85, "new_relation": 0.20}
        regs = _check_regressions(current, prior, max_regression=0.05)
        assert regs == []

    def test_relation_dropped_from_current_not_a_regression(self):
        """Symmetric: a relation in prior but missing in current is
        a coverage gap, not a regression. Surfaces elsewhere."""
        prior = {"evidence_for": 0.80, "old_relation": 0.90}
        current = {"evidence_for": 0.85}
        regs = _check_regressions(current, prior, max_regression=0.05)
        assert regs == []

    def test_exact_threshold_boundary_not_a_regression(self):
        """A drop exactly equal to max_regression is allowed (strict <)."""
        prior = {"r1": 0.80}
        current = {"r1": 0.75}  # exactly -0.05
        regs = _check_regressions(current, prior, max_regression=0.05)
        assert regs == []

    def test_just_past_threshold_is_a_regression(self):
        """Drop strictly greater than max_regression triggers."""
        prior = {"r1": 0.80}
        current = {"r1": 0.749}  # -0.051
        regs = _check_regressions(current, prior, max_regression=0.05)
        assert len(regs) == 1
        assert regs[0][0] == "r1"


class TestAutodetectPriorBaseline:
    def test_returns_none_when_no_priors(self, tmpreports):
        out = _autodetect_prior_baseline(
            tmpreports, tmpreports / "edge-audit-current.json"
        )
        assert out is None

    def test_picks_most_recent_other_than_current(self, tmpreports):
        a = tmpreports / "edge-audit-2026-04-26.json"
        b = tmpreports / "edge-audit-2026-04-30.json"
        current = tmpreports / "edge-audit-2026-05-04.json"
        for p in (a, b):
            _make_baseline(p, {"r": 0.8})
        # Touch b after a so it's most recent
        import time
        time.sleep(0.01)
        b.touch()
        out = _autodetect_prior_baseline(tmpreports, current)
        assert out == b

    def test_excludes_the_current_file(self, tmpreports):
        """Don't pick the file we're about to write as our own baseline."""
        prior = tmpreports / "edge-audit-prior.json"
        current = tmpreports / "edge-audit-current.json"
        _make_baseline(prior, {"r": 0.8})
        _make_baseline(current, {"r": 0.8})  # write current first to test exclusion
        out = _autodetect_prior_baseline(tmpreports, current)
        assert out == prior

    def test_returns_none_when_reports_dir_missing(self, tmpreports):
        out = _autodetect_prior_baseline(
            tmpreports / "does-not-exist", tmpreports / "current.json"
        )
        assert out is None
