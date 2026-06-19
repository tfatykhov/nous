import pytest
from unittest.mock import AsyncMock

from nous.config import Settings
from nous.cognitive.context import ContextEngine
from nous.cognitive.schemas import ContextBudget, FrameSelection
from nous.cognitive.intent import IntentClassifier
from nous.cognitive.layer import _DEICTIC_FOLLOWUP, _should_boost_deictic


def test_followup_flags_defaults():
    s = Settings()
    assert s.followup_episode_budget_enabled is True
    assert s.followup_deictic_detection_enabled is True
    assert s.recall_before_clarify_prompt is True
    assert s.followup_first_turn_episode is False
    assert s.episode_open_threads is False


def _frame(frame_id="conversation"):
    return FrameSelection(frame_id=frame_id, frame_name=frame_id.title(),
                          confidence=0.9, match_method="pattern")


def test_conversation_frame_default_episode_budget_nonzero():
    assert ContextBudget.for_frame("conversation").episodes == 600


def test_intent_conversation_override_episodes_when_enabled():
    clf = IntentClassifier(settings=Settings())
    signals = clf.classify("let's keep chatting about the weather", _frame())
    plan = clf.plan_retrieval(signals, input_text="let's keep chatting about the weather")
    assert plan.budget_overrides.get("episodes") == 600


def test_intent_conversation_override_episodes_when_disabled():
    clf = IntentClassifier(settings=Settings(followup_episode_budget_enabled=False))
    signals = clf.classify("let's keep chatting", _frame())
    plan = clf.plan_retrieval(signals, input_text="let's keep chatting")
    assert plan.budget_overrides.get("episodes") == 0


def test_rescue_lifts_above_a1_floor():
    clf = IntentClassifier(settings=Settings())
    signals = clf.classify("let's keep chatting", _frame())
    signals.temporal_recency = 0.6
    plan = clf.plan_retrieval(signals, input_text="x")
    assert plan.budget_overrides.get("episodes") == 1000


def test_rescue_does_not_fire_below_threshold():
    clf = IntentClassifier(settings=Settings())
    signals = clf.classify("let's keep chatting", _frame())
    signals.temporal_recency = 0.4
    plan = clf.plan_retrieval(signals, input_text="x")
    assert plan.budget_overrides.get("episodes") == 600


# ---------------------------------------------------------------------------
# C2: recall-before-clarify instruction section (F083)
# Mirror the fixture pattern from tests/test_anti_hallucination.py.
# ---------------------------------------------------------------------------

def _make_context_engine(settings: Settings) -> ContextEngine:
    brain = AsyncMock()
    heart = AsyncMock()
    heart.search_facts = AsyncMock(return_value=[])
    heart.list_facts_by_category = AsyncMock(return_value=[])
    brain.query = AsyncMock(return_value=[])
    heart.search_procedures = AsyncMock(return_value=[])
    heart.search_episodes = AsyncMock(return_value=[])
    heart.list_censors = AsyncMock(return_value=[])
    heart.get_working_memory = AsyncMock(return_value=None)
    heart.list_episodes = AsyncMock(return_value=[])
    return ContextEngine(brain, heart, settings)


def _conv_frame() -> FrameSelection:
    return FrameSelection(
        frame_id="conversation",
        frame_name="Conversation",
        confidence=1.0,
        match_method="default",
        description=None,
    )


@pytest.mark.asyncio
async def test_recall_before_clarify_section_present():
    """C2: section is injected when recall_before_clarify_prompt=True."""
    s = Settings(recall_before_clarify_prompt=True)
    engine = _make_context_engine(s)

    result = await engine.build(
        agent_id="test",
        session_id="test-session",
        input_text="continue that thing you mentioned",
        frame=_conv_frame(),
    )
    prompt_lower = result.system_prompt.lower()
    assert "before asking" in prompt_lower
    assert "clarify" in prompt_lower


@pytest.mark.asyncio
async def test_recall_before_clarify_absent_when_off():
    """C2: section is NOT injected when recall_before_clarify_prompt=False."""
    s = Settings(recall_before_clarify_prompt=False)
    engine = _make_context_engine(s)

    result = await engine.build(
        agent_id="test",
        session_id="test-session",
        input_text="continue that thing you mentioned",
        frame=_conv_frame(),
    )
    assert "before asking" not in result.system_prompt.lower()


# ---------------------------------------------------------------------------
# C1: first-turn-gated deictic follow-up detector (F083)
# ---------------------------------------------------------------------------

def test_deictic_matches_cross_session_referents():
    for s in ["what about the second option you mentioned?",
              "can you continue what we were doing?",
              "did that fix work?"]:
        assert _DEICTIC_FOLLOWUP.search(s), s


def test_deictic_does_not_match_same_session_coding():
    for s in ["continue the loop until done",
              "use the first argument",
              "write a python function to reverse a string",
              "what about performance?"]:
        assert not _DEICTIC_FOLLOWUP.search(s), s


def test_c1_gate_fires_on_first_turn_deictic():
    assert _should_boost_deictic(True, True, "the second option you mentioned") is True


def test_c1_gate_blocked_when_not_first_turn():
    # same-session referent must NOT pull cross-session episodes (R2 safety property)
    assert _should_boost_deictic(False, True, "the second option you mentioned") is False


def test_c1_gate_blocked_when_flag_off():
    assert _should_boost_deictic(True, False, "the second option you mentioned") is False


def test_c1_gate_no_match_no_boost():
    assert _should_boost_deictic(True, True, "use the first argument") is False
