"""2026-07-02 memory-fidelity scan: new Settings fields for previously hardcoded constants."""
import pytest

from nous.config import Settings


def _settings(**env):
    return Settings(_env_file=None, **env)


def test_fidelity_defaults_match_reviewed_values():
    """Defaults per the 2026-07-02 architecture-reviewed table (improved, not
    behavior-preserving, except the three destructive/admission gates)."""
    s = _settings()
    assert s.transcript_message_max_chars == 8000   # was literal 500 — sanity bound
    assert s.episode_lessons_max_chars == 8000      # was 500 — sanity bound
    assert s.episode_summary_max_chunks == 4        # NEW downstream bound (0=unlimited)
    assert s.episode_chunk_max_per_episode == 100   # NEW downstream bound (0=unlimited)
    assert s.episode_seed_summary_chars == 500      # was 200
    assert s.episode_dedup_threshold == 0.85        # gate — keep
    assert s.episode_dedup_window_hours == 48       # gate — keep
    assert s.episode_min_content_length == 200      # gate — keep
    assert s.correction_input_max_chars == 2000     # was 1000
    assert s.correction_max_tokens == 1024          # was 512 (F031 precedent)
    assert s.correction_min_principle_chars == 20   # was 30
    assert s.episode_summary_max_tokens == 0        # 0 = auto (3000 extended / 1500 base)
    assert s.knowledge_extractor_max_chars == 24000  # was 12000
    assert s.sleep_reflection_summary_chars == 500  # was 200
    assert s.sleep_contradiction_fact_chars == 1000  # was 500
    assert s.fact_min_content_chars == 30           # gate — keep
    assert s.fact_supersession_threshold == 0.80    # gate — keep
    assert s.graph_link_candidate_window_days == 60  # was 30


def test_fidelity_env_override(monkeypatch):
    monkeypatch.setenv("NOUS_TRANSCRIPT_MESSAGE_MAX_CHARS", "2000")
    monkeypatch.setenv("NOUS_FACT_SUPERSESSION_THRESHOLD", "0.9")
    monkeypatch.setenv("NOUS_GRAPH_LINK_CANDIDATE_WINDOW_DAYS", "0")
    s = _settings()
    assert s.transcript_message_max_chars == 2000
    assert s.fact_supersession_threshold == 0.9
    assert s.graph_link_candidate_window_days == 0


def test_fidelity_bounds_rejected():
    with pytest.raises(Exception):
        _settings(transcript_message_max_chars=0)   # ge=50
    with pytest.raises(Exception):
        _settings(fact_supersession_threshold=1.5)  # le=1.0
