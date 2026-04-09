"""Unit tests for calibration engine (Brier score, accuracy, breakdowns)."""

import pytest

from nous.brain.calibration import CalibrationEngine
from nous.storage.models import Decision, DecisionReason


@pytest.fixture
def engine() -> CalibrationEngine:
    return CalibrationEngine()


async def _insert_decision(
    session,
    *,
    confidence: float,
    outcome: str,
    category: str = "architecture",
    stakes: str = "medium",
    reasons: list[dict] | None = None,
) -> Decision:
    """Helper to insert a reviewed decision."""
    d = Decision(
        agent_id="test-cal-agent",
        description=f"Calibration test (conf={confidence}, out={outcome})",
        confidence=confidence,
        category=category,
        stakes=stakes,
        outcome=outcome,
    )
    session.add(d)
    await session.flush()

    if reasons:
        for r in reasons:
            session.add(
                DecisionReason(
                    decision_id=d.id,
                    type=r["type"],
                    text=r["text"],
                )
            )
        await session.flush()

    return d


async def test_brier_perfect(session, engine):
    """All confidence=1.0 with outcome=success -> Brier=0.0."""
    for _ in range(3):
        await _insert_decision(session, confidence=1.0, outcome="success")

    report = await engine.compute(session, "test-cal-agent")
    assert report.brier_score is not None
    assert abs(report.brier_score - 0.0) < 1e-6


async def test_brier_worst(session, engine):
    """All confidence=1.0 with outcome=failure -> Brier=1.0."""
    for _ in range(3):
        await _insert_decision(session, confidence=1.0, outcome="failure")

    report = await engine.compute(session, "test-cal-agent")
    assert report.brier_score is not None
    assert abs(report.brier_score - 1.0) < 1e-6


async def test_brier_partial(session, engine):
    """Mix of outcomes -> expected Brier value.

    Decisions:
    - conf=0.9, outcome=success -> (0.9 - 1.0)^2 = 0.01
    - conf=0.6, outcome=failure -> (0.6 - 0.0)^2 = 0.36
    - conf=0.5, outcome=partial -> (0.5 - 0.5)^2 = 0.00
    Expected Brier = (0.01 + 0.36 + 0.00) / 3 = 0.1233...
    """
    await _insert_decision(session, confidence=0.9, outcome="success")
    await _insert_decision(session, confidence=0.6, outcome="failure")
    await _insert_decision(session, confidence=0.5, outcome="partial")

    report = await engine.compute(session, "test-cal-agent")
    expected_brier = (0.01 + 0.36 + 0.00) / 3
    assert report.brier_score is not None
    assert abs(report.brier_score - expected_brier) < 1e-4


async def test_accuracy_calculation(session, engine):
    """3 correct, 1 wrong -> 75%.

    Accuracy rules:
    - correct = (confidence >= 0.5 AND outcome in success/partial) OR
                (confidence < 0.5 AND outcome = failure)
    """
    # Correct: high conf + success
    await _insert_decision(session, confidence=0.9, outcome="success")
    # Correct: high conf + partial
    await _insert_decision(session, confidence=0.7, outcome="partial")
    # Correct: low conf + failure
    await _insert_decision(session, confidence=0.3, outcome="failure")
    # Wrong: high conf + failure
    await _insert_decision(session, confidence=0.8, outcome="failure")

    report = await engine.compute(session, "test-cal-agent")
    assert report.accuracy is not None
    assert abs(report.accuracy - 0.75) < 1e-6


async def test_no_reviewed_decisions(session, engine):
    """Returns None for brier/accuracy when all decisions are pending."""
    await _insert_decision(session, confidence=0.8, outcome="pending")

    report = await engine.compute(session, "test-cal-agent")
    assert report.total_decisions == 1
    assert report.reviewed_decisions == 0
    assert report.brier_score is None
    assert report.accuracy is None


async def test_per_category_breakdown(session, engine):
    """Stats grouped by category."""
    await _insert_decision(session, confidence=1.0, outcome="success", category="architecture")
    await _insert_decision(session, confidence=1.0, outcome="success", category="architecture")
    await _insert_decision(session, confidence=0.5, outcome="failure", category="tooling")

    report = await engine.compute(session, "test-cal-agent")
    assert "architecture" in report.category_stats
    assert report.category_stats["architecture"]["count"] == 2
    assert "tooling" in report.category_stats
    assert report.category_stats["tooling"]["count"] == 1


async def test_per_reason_type(session, engine):
    """Stats grouped by reason type."""
    await _insert_decision(
        session,
        confidence=0.9,
        outcome="success",
        reasons=[
            {"type": "analysis", "text": "Analyzed"},
            {"type": "pattern", "text": "Follows pattern"},
        ],
    )
    await _insert_decision(
        session,
        confidence=0.8,
        outcome="success",
        reasons=[
            {"type": "analysis", "text": "Another analysis"},
        ],
    )

    report = await engine.compute(session, "test-cal-agent")
    assert "analysis" in report.reason_type_stats
    assert report.reason_type_stats["analysis"]["count"] == 2
    assert "pattern" in report.reason_type_stats
    assert report.reason_type_stats["pattern"]["count"] == 1
