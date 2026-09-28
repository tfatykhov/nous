"""F058: the write path must record WHICH factor produced each confidence.

Migration 073 exists because every way of inferring the factor after the fact
is unsound (see the module docstring there). These tests pin the write-path
half of that contract; the probe half lives in
tests/test_f058_calibration_probe.py.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from nous.storage.models import Decision


class TestDecisionModelCarriesProvenance:
    def test_columns_exist_and_are_nullable(self):
        """Nullable because rows written before migration 073 cannot be
        backfilled -- the factor that produced them is not recoverable."""
        cols = Decision.__table__.columns
        assert cols["calibration_factor"].nullable is True
        assert cols["calibration_applied_at"].nullable is True

    def test_init_sql_and_orm_agree(self):
        """A fresh database is built from init.sql, not from the migrations,
        so a column added only to the migration would be missing there."""
        from pathlib import Path

        init = (Path(__file__).resolve().parents[1] / "sql/init.sql").read_text()
        table = init.split("CREATE TABLE brain.decisions (")[1].split(");")[0]
        for col in ("confidence_raw", "calibration_factor",
                    "calibration_applied_at"):
            assert col in table, f"{col} missing from init.sql"


class _FakeSettings:
    def __init__(self, factor):
        self.confidence_calibration_factor = factor


class TestCalibrationStamping:
    """Brain._record and Brain._update must stamp both columns wherever they
    calibrate, and _update must NOT stamp them when it does not.
    """

    def _apply_update(self, decision, *, confidence, factor):
        """Mirror of the Brain._update confidence branch."""
        from nous.brain.calibration_scaling import calibrate_confidence

        if confidence is not None:
            decision.confidence = calibrate_confidence(confidence, factor)
            decision.confidence_raw = confidence
            decision.calibration_factor = factor
            decision.calibration_applied_at = datetime.now(UTC)
        return decision

    def test_recalibration_restamps_the_factor(self):
        d = Decision(
            agent_id="a", description="d", confidence=0.7627,
            confidence_raw=1.0, calibration_factor=0.7627,
            calibration_applied_at=datetime(2026, 1, 1, tzinfo=UTC),
            category="process", stakes="low",
        )
        self._apply_update(d, confidence=0.8, factor=1.0)
        assert d.calibration_factor == 1.0
        assert d.calibration_applied_at > datetime(2026, 1, 1, tzinfo=UTC)
        assert d.confidence == pytest.approx(d.confidence_raw * 1.0)

    def test_stored_confidence_always_matches_the_recorded_factor(self):
        """The integrity invariant the probe checks in both eras."""
        from nous.brain.calibration_scaling import calibrate_confidence

        for factor in (0.7627, 1.0, 0.5):
            for raw in (0.1, 0.5, 0.9):
                d = Decision(
                    agent_id="a", description="d",
                    confidence=calibrate_confidence(raw, factor),
                    confidence_raw=raw, calibration_factor=factor,
                    category="process", stakes="low",
                )
                assert d.confidence == pytest.approx(
                    d.confidence_raw * d.calibration_factor
                )

    def test_description_only_edit_leaves_the_stamp_alone(self):
        """Why calibration_applied_at cannot be replaced by updated_at: an
        edit that never touches confidence must not claim the stale factor was
        re-applied today, or the probe would flag correctly-scaled old rows.
        """
        stamped = datetime(2026, 1, 1, tzinfo=UTC)
        d = Decision(
            agent_id="a", description="before", confidence=0.7627,
            confidence_raw=1.0, calibration_factor=0.7627,
            calibration_applied_at=stamped, category="process", stakes="low",
        )
        d.description = "after"
        self._apply_update(d, confidence=None, factor=1.0)
        assert d.calibration_factor == 0.7627
        assert d.calibration_applied_at == stamped


class TestBrainSourceStampsBothWritePaths:
    """Guard against one of the two call sites drifting: both the create and
    the update path must set the columns next to confidence_raw.
    """

    def _brain_source(self):
        from pathlib import Path

        return (Path(__file__).resolve().parents[1]
                / "nous/brain/brain.py").read_text()

    def test_both_calibration_sites_stamp_the_factor(self):
        src = self._brain_source()
        assert src.count("calibration_factor=_factor") == 1, "create path"
        assert src.count("decision.calibration_factor = _factor") == 1, "update"

    def test_every_confidence_raw_write_is_accompanied_by_a_stamp(self):
        src = self._brain_source()
        assert (src.count("calibration_applied_at")
                >= src.count("confidence_raw=input.confidence")
                + src.count("decision.confidence_raw = confidence"))
