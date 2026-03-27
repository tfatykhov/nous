"""Tests for F024 Phase 3b rubric schemas and models."""
import uuid
from datetime import datetime, UTC

import pytest


class TestRubricVersionModel:
    def test_rubric_version_import(self):
        from nous.storage.models import RubricVersion
        assert RubricVersion.__tablename__ == "rubric_versions"

    def test_rubric_version_schema(self):
        from nous.storage.models import RubricVersion
        assert RubricVersion.__table_args__[-1]["schema"] == "heart"

    def test_rubric_version_status_constraint(self):
        from nous.storage.models import RubricVersion
        constraints = [a for a in RubricVersion.__table_args__ if hasattr(a, "name")]
        assert any(c.name == "ck_rubric_versions_status" for c in constraints)


class TestOutcomeSignalModel:
    def test_outcome_signal_import(self):
        from nous.storage.models import OutcomeSignal
        assert OutcomeSignal.__tablename__ == "outcome_signals"

    def test_outcome_signal_schema(self):
        from nous.storage.models import OutcomeSignal
        assert OutcomeSignal.__table_args__[-1]["schema"] == "heart"

    def test_outcome_signal_confidence_constraint(self):
        from nous.storage.models import OutcomeSignal
        constraints = [a for a in OutcomeSignal.__table_args__ if hasattr(a, "name")]
        assert any(c.name == "ck_outcome_signals_confidence" for c in constraints)


class TestRubricDimension:
    def test_dimension_defaults(self):
        from nous.cognitive.rubric_schemas import RubricDimension
        dim = RubricDimension(
            name="Recall",
            weight=0.25,
            description="Accuracy of memory retrieval",
            scoring_criteria="1-10 scale",
        )
        assert dim.min_weight == 0.10
        assert dim.max_weight == 0.40

    def test_dimension_weight_validation(self):
        from nous.cognitive.rubric_schemas import RubricDimension
        with pytest.raises(ValueError):
            RubricDimension(
                name="Bad",
                weight=0.50,
                description="test",
                scoring_criteria="test",
            )


class TestRubricVersionDetail:
    def test_version_detail(self):
        from nous.cognitive.rubric_schemas import RubricVersionDetail, RubricDimension
        dim = RubricDimension(
            name="Recall", weight=0.25,
            description="test", scoring_criteria="test",
        )
        rv = RubricVersionDetail(
            id=uuid.uuid4(),
            agent_id="test",
            version="1.0.0",
            change_reason="Initial",
            dimensions=[dim],
            status="active",
            created_at=datetime.now(UTC),
        )
        assert rv.version == "1.0.0"
        assert len(rv.dimensions) == 1


class TestOutcomeSignalDetail:
    def test_signal_types(self):
        from nous.cognitive.rubric_schemas import OutcomeSignalDetail, OutcomeSignalType
        assert "corrected" in [e.value for e in OutcomeSignalType]
        assert "praised" in [e.value for e in OutcomeSignalType]

    def test_signal_detail(self):
        from nous.cognitive.rubric_schemas import OutcomeSignalDetail
        sig = OutcomeSignalDetail(
            id=uuid.uuid4(),
            agent_id="test",
            episode_id=uuid.uuid4(),
            signal_type="corrected",
            confidence=0.85,
            evidence="User said 'no, actually...'",
            created_at=datetime.now(UTC),
        )
        assert sig.confidence == 0.85
