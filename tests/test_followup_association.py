from nous.config import Settings


def test_followup_flags_defaults():
    s = Settings()
    assert s.followup_episode_budget_enabled is True
    assert s.followup_deictic_detection_enabled is True
    assert s.recall_before_clarify_prompt is True
    assert s.followup_first_turn_episode is False
    assert s.episode_open_threads is False
