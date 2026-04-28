"""F056 PR #2: unit tests for the dedup handler eval CLI."""
from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from nous_eval.handlers._jsonl import load_jsonl
from nous_eval.handlers._models import DedupPair
from nous_eval.handlers.dedup import (
    _AGENT_ID,
    _classify_dedup_outcome,
    _confusion_increment,
    _settings_with_dedup_overrides,
    compute_f1,
    filter_pairs,
)


# ---------------------------------------------------------------------------
# DedupPair schema (F056 §B)
# ---------------------------------------------------------------------------


_VALID_ANCHOR = "Tim prefers FastAPI over Django for new microservices."  # 56 chars
_VALID_PARA = "For new microservices, Tim chooses FastAPI not Django."  # 54 chars


class TestDedupPairSchema:
    def test_minimal_valid_row(self):
        p = DedupPair(
            row_id="d1", anchor=_VALID_ANCHOR, paraphrase=_VALID_PARA,
            expected="dedup",
        )
        assert p.expected == "dedup"
        assert p.reviewed_by is None

    def test_distinct_label_accepted(self):
        p = DedupPair(
            row_id="x1", anchor=_VALID_ANCHOR, paraphrase=_VALID_PARA,
            expected="distinct",
        )
        assert p.expected == "distinct"

    def test_invalid_expected_value_rejected(self):
        with pytest.raises(ValidationError):
            DedupPair(
                row_id="d1", anchor=_VALID_ANCHOR, paraphrase=_VALID_PARA,
                expected="maybe",
            )

    def test_short_anchor_rejected(self):
        # F038-1.2 in Heart.learn rejects content < 30 chars; schema enforces
        # this at fixture-load so we don't waste a Heart.learn round-trip.
        with pytest.raises(ValidationError):
            DedupPair(
                row_id="d1", anchor="too short", paraphrase=_VALID_PARA,
                expected="dedup",
            )

    def test_short_paraphrase_rejected(self):
        with pytest.raises(ValidationError):
            DedupPair(
                row_id="d1", anchor=_VALID_ANCHOR, paraphrase="too short",
                expected="dedup",
            )


# ---------------------------------------------------------------------------
# Real fixture loads cleanly (catches schema violations in the 30-row gold set)
# ---------------------------------------------------------------------------


class TestRealFixtureLoads:
    def test_full_dedup_fixture_validates(self):
        path = Path("tests/fixtures/handlers/dedup_paraphrases.jsonl")
        if not path.exists():
            pytest.skip(f"fixture not present at {path}")
        rows = load_jsonl(path, DedupPair)
        assert len(rows) >= 30  # spec mandates 20 dedup + 10 distinct
        n_dedup = sum(1 for r in rows if r.expected == "dedup")
        n_distinct = sum(1 for r in rows if r.expected == "distinct")
        assert n_dedup == 20
        assert n_distinct == 10
        # All must be reviewed_by="tim" per spec §B
        unreviewed = [r.row_id for r in rows if not r.reviewed_by]
        assert unreviewed == [], f"unreviewed rows in gating fixture: {unreviewed}"


# ---------------------------------------------------------------------------
# filter_pairs
# ---------------------------------------------------------------------------


class TestFilterPairs:
    def test_default_skips_unreviewed(self):
        pairs = [
            DedupPair(row_id="d1", anchor=_VALID_ANCHOR, paraphrase=_VALID_PARA,
                      expected="dedup", reviewed_by="tim"),
            DedupPair(row_id="d2", anchor=_VALID_ANCHOR, paraphrase=_VALID_PARA,
                      expected="dedup", reviewed_by=None),
        ]
        filtered = filter_pairs(pairs, include_unreviewed=False)
        assert len(filtered) == 1
        assert filtered[0].row_id == "d1"

    def test_include_unreviewed_keeps_all(self):
        pairs = [
            DedupPair(row_id="d1", anchor=_VALID_ANCHOR, paraphrase=_VALID_PARA,
                      expected="dedup", reviewed_by="tim"),
            DedupPair(row_id="d2", anchor=_VALID_ANCHOR, paraphrase=_VALID_PARA,
                      expected="dedup", reviewed_by=None),
        ]
        assert len(filter_pairs(pairs, include_unreviewed=True)) == 2


# ---------------------------------------------------------------------------
# _classify_dedup_outcome (pure helper)
# ---------------------------------------------------------------------------


class TestClassifyDedupOutcome:
    def test_anchor_in_returned_means_dedup(self):
        anchor = uuid4()
        other = uuid4()
        # FactExtractor returns [anchor_uuid] when dedup fires against anchor
        assert _classify_dedup_outcome([anchor], anchor) == "dedup"
        # Multiple UUIDs returned, anchor present → still dedup
        assert _classify_dedup_outcome([anchor, other], anchor) == "dedup"

    def test_anchor_not_in_returned_means_distinct(self):
        anchor = uuid4()
        other = uuid4()
        # New UUID means a new fact was stored — no dedup against anchor
        assert _classify_dedup_outcome([other], anchor) == "distinct"
        # Empty return list also means no dedup against anchor
        assert _classify_dedup_outcome([], anchor) == "distinct"

    def test_dedup_against_background_classifies_distinct(self):
        # If the paraphrase dedup'd against a non-anchor (background) fact,
        # the eval treats it as "didn't dedup against THIS anchor" — see
        # docstring "Eval correctness depends on background facts being
        # dissimilar from anchors/paraphrases".
        anchor = uuid4()
        background_hit = uuid4()
        assert _classify_dedup_outcome([background_hit], anchor) == "distinct"


# ---------------------------------------------------------------------------
# _confusion_increment (per-leg confusion matrix)
# ---------------------------------------------------------------------------


class TestConfusionIncrement:
    def test_tp_correct_dedup(self):
        cm = {"tp": 0, "fp": 0, "tn": 0, "fn": 0}
        _confusion_increment(cm, "dedup", "dedup")
        assert cm == {"tp": 1, "fp": 0, "tn": 0, "fn": 0}

    def test_tn_correct_distinct(self):
        cm = {"tp": 0, "fp": 0, "tn": 0, "fn": 0}
        _confusion_increment(cm, "distinct", "distinct")
        assert cm == {"tp": 0, "fp": 0, "tn": 1, "fn": 0}

    def test_fp_over_dedup(self):
        # Spec §B: PR #364 was a "lowered Leg 1 precision" bug — paraphrase
        # of a distinct fact got incorrectly dedup'd. This case must surface
        # as fp so the regression catches it.
        cm = {"tp": 0, "fp": 0, "tn": 0, "fn": 0}
        _confusion_increment(cm, "distinct", "dedup")
        assert cm == {"tp": 0, "fp": 1, "tn": 0, "fn": 0}

    def test_fn_missed_dedup(self):
        cm = {"tp": 0, "fp": 0, "tn": 0, "fn": 0}
        _confusion_increment(cm, "dedup", "distinct")
        assert cm == {"tp": 0, "fp": 0, "tn": 0, "fn": 1}


# ---------------------------------------------------------------------------
# compute_f1 (duplicated from admission for handler independence)
# ---------------------------------------------------------------------------


class TestComputeF1:
    def test_perfect(self):
        precision, recall, f1 = compute_f1(tp=20, fp=0, fn=0)
        assert (precision, recall, f1) == (1.0, 1.0, 1.0)

    def test_zero_division_safe(self):
        precision, recall, f1 = compute_f1(tp=0, fp=0, fn=0)
        assert (precision, recall, f1) == (0.0, 0.0, 0.0)


# ---------------------------------------------------------------------------
# Settings overrides (F056 §B — admission disabled for isolation)
# ---------------------------------------------------------------------------


class TestSettingsOverrides:
    def test_admission_disabled(self):
        # Per F056 spec §B: admission off so a dedup'd paraphrase doesn't
        # get masked by an admission rejection.
        from nous.config import Settings
        base = Settings()
        overridden = _settings_with_dedup_overrides(base)
        assert overridden.admission_control_enabled is False

    def test_agent_id_set_to_handler_scope(self):
        from nous.config import Settings
        base = Settings()
        overridden = _settings_with_dedup_overrides(base)
        assert overridden.agent_id == _AGENT_ID
