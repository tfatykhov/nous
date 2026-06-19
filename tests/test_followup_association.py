from nous.config import Settings
from nous.cognitive.schemas import ContextBudget, FrameSelection
from nous.cognitive.intent import IntentClassifier


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
