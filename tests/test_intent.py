"""Unit tests for IntentClassifier — pure pattern matching, no DB needed."""

from nous.cognitive.intent import IntentClassifier
from nous.cognitive.schemas import FrameSelection


def _frame(frame_id: str = "conversation") -> FrameSelection:
    """Minimal FrameSelection for testing."""
    return FrameSelection(
        frame_id=frame_id,
        frame_name=frame_id.title(),
        confidence=0.9,
        match_method="pattern",
    )


classifier = IntentClassifier()


# ---------------------------------------------------------------------------
# classify() tests
# ---------------------------------------------------------------------------


def test_greeting_detection():
    """'hey' is detected as a greeting."""
    signals = classifier.classify("hey", _frame())
    assert signals.is_greeting is True


def test_question_detection():
    """'should we use Redis?' is detected as a question."""
    signals = classifier.classify("should we use Redis?", _frame())
    assert signals.is_question is True


def test_temporal_recency():
    """'yesterday' triggers temporal_recency=0.8."""
    signals = classifier.classify("what happened yesterday", _frame())
    assert signals.temporal_recency == 0.8


def test_memory_type_hints_decision():
    """'what was my decision' triggers decision hint via regex \\bdecision\\b."""
    signals = classifier.classify("what was my decision about the database", _frame())
    assert "decision" in signals.memory_type_hints
    assert signals.memory_type_hints["decision"] > 0


def test_memory_type_hints_procedure():
    """'how do I deploy' triggers procedure hint."""
    signals = classifier.classify("how do I deploy the application", _frame())
    assert "procedure" in signals.memory_type_hints
    assert signals.memory_type_hints["procedure"] > 0


# ---------------------------------------------------------------------------
# plan_retrieval() tests
# ---------------------------------------------------------------------------


def test_plan_greeting_skips_all():
    """Greeting plan has empty queries and skips all memory types."""
    signals = classifier.classify("hey", _frame())
    plan = classifier.plan_retrieval(signals)

    assert plan.queries == []
    assert "decision" in plan.skip_types
    assert "fact" in plan.skip_types
    assert "procedure" in plan.skip_types
    assert "episode" in plan.skip_types


def test_plan_decision_frame_budget():
    """Decision frame gets budget_overrides with decisions=3500."""
    signals = classifier.classify(
        "should we migrate to PostgreSQL", _frame("decision")
    )
    plan = classifier.plan_retrieval(signals)

    assert plan.budget_overrides.get("decisions") == 3500


def test_plan_hint_biased_limits():
    """Dominant memory type hint gets limit=8, others get 3."""
    signals = classifier.classify(
        "what was my decision about deployment strategy", _frame()
    )
    plan = classifier.plan_retrieval(signals)

    limits_by_type = {q.memory_type: q.limit for q in plan.queries}
    assert limits_by_type["decision"] == 8
    assert limits_by_type["fact"] == 3
    assert limits_by_type["procedure"] == 3
    assert limits_by_type["episode"] == 3
