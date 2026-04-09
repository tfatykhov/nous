"""Tests for F036 CacheBreakDetector."""

from nous.api.cache_optimizer import CacheBreakDetector, _hash


def test_first_call_returns_none() -> None:
    detector = CacheBreakDetector()
    result = detector.check("static", "semi", "dynamic", "tools", "model")
    assert result is None


def test_identical_consecutive_calls_return_none() -> None:
    detector = CacheBreakDetector()
    detector.check("static", "semi", "dynamic", "tools", "model")
    result = detector.check("static", "semi", "dynamic", "tools", "model")
    assert result is None


def test_changed_static_text_detected() -> None:
    detector = CacheBreakDetector()
    detector.check("static_v1", "semi", "dynamic", "tools", "model")
    result = detector.check("static_v2", "semi", "dynamic", "tools", "model")
    assert result is not None
    assert "static_identity" in result.components_changed
    assert len(result.components_changed) == 1


def test_changed_semi_stable_text_detected() -> None:
    detector = CacheBreakDetector()
    detector.check("static", "semi_v1", "dynamic", "tools", "model")
    result = detector.check("static", "semi_v2", "dynamic", "tools", "model")
    assert result is not None
    assert "semi_stable_context" in result.components_changed
    assert len(result.components_changed) == 1


def test_changed_tools_detected() -> None:
    detector = CacheBreakDetector()
    detector.check("static", "semi", "dynamic", "tools_v1", "model")
    result = detector.check("static", "semi", "dynamic", "tools_v2", "model")
    assert result is not None
    assert "tools" in result.components_changed
    assert len(result.components_changed) == 1


def test_changed_model_detected() -> None:
    detector = CacheBreakDetector()
    detector.check("static", "semi", "dynamic", "tools", "claude-sonnet")
    result = detector.check("static", "semi", "dynamic", "tools", "claude-opus")
    assert result is not None
    assert "model" in result.components_changed
    assert len(result.components_changed) == 1


def test_multiple_components_changed_simultaneously() -> None:
    detector = CacheBreakDetector()
    detector.check("static_v1", "semi_v1", "dynamic", "tools_v1", "claude-sonnet")
    result = detector.check("static_v2", "semi_v2", "dynamic", "tools_v2", "claude-opus")
    assert result is not None
    assert set(result.components_changed) == {
        "static_identity",
        "semi_stable_context",
        "tools",
        "model",
    }


def test_dynamic_text_change_does_not_trigger_break() -> None:
    detector = CacheBreakDetector()
    detector.check("static", "semi", "dynamic_v1", "tools", "model")
    result = detector.check("static", "semi", "dynamic_v2", "tools", "model")
    assert result is None


def test_token_loss_estimation_approximately_correct() -> None:
    static_text = "a" * 400  # 400 chars -> ~100 tokens
    semi_text = "b" * 800  # 800 chars -> ~200 tokens
    tools_text = "c" * 1200  # 1200 chars -> ~300 tokens

    detector = CacheBreakDetector()
    detector.check(static_text, semi_text, "dynamic", tools_text, "model")
    result = detector.check("changed", "changed", "dynamic", "changed", "model")
    assert result is not None
    # Previous call had 400+800+1200 chars, token estimate = len//4 on NEW text
    # New texts: "changed" (7 chars each) -> 7//4=1 per component = 3 total
    # But the estimation uses the NEW text lengths, not old
    expected = len("changed") // 4 + len("changed") // 4 + len("changed") // 4
    assert result.estimated_tokens_lost == expected


def test_token_loss_uses_current_text_lengths() -> None:
    detector = CacheBreakDetector()
    detector.check("old_static", "old_semi", "dynamic", "old_tools", "model")
    new_static = "x" * 100
    new_semi = "y" * 200
    new_tools = "z" * 400
    result = detector.check(new_static, new_semi, "dynamic", new_tools, "new_model")
    assert result is not None
    expected = 100 // 4 + 200 // 4 + 400 // 4  # model change adds 0 tokens
    assert result.estimated_tokens_lost == expected


def test_reset_clears_state() -> None:
    detector = CacheBreakDetector()
    detector.check("static", "semi", "dynamic", "tools", "model")
    detector.reset()
    result = detector.check("static_changed", "semi", "dynamic", "tools", "model")
    assert result is None  # First call after reset, no comparison


def test_previous_and_current_hashes_not_swapped() -> None:
    detector = CacheBreakDetector()
    detector.check("static_v1", "semi", "dynamic", "tools", "model")
    result = detector.check("static_v2", "semi", "dynamic", "tools", "model")
    assert result is not None

    # previous_hashes should contain hashes from the FIRST call
    assert result.previous_hashes["static_hash"] == _hash("static_v1")
    # current_hashes should contain hashes from the SECOND call
    assert result.current_hashes["static_hash"] == _hash("static_v2")

    # Verify they are different (not swapped)
    assert result.previous_hashes["static_hash"] != result.current_hashes["static_hash"]

    # Semi-stable should be the same in both (unchanged)
    assert result.previous_hashes["semi_stable_hash"] == _hash("semi")
    assert result.current_hashes["semi_stable_hash"] == _hash("semi")


def test_hashes_include_all_components() -> None:
    detector = CacheBreakDetector()
    detector.check("s1", "ss1", "d1", "t1", "m1")
    result = detector.check("s2", "ss1", "d1", "t1", "m1")
    assert result is not None

    expected_keys = {"static_hash", "semi_stable_hash", "dynamic_hash", "tools_hash", "model_hash"}
    assert set(result.previous_hashes.keys()) == expected_keys
    assert set(result.current_hashes.keys()) == expected_keys


def test_model_change_adds_zero_token_loss() -> None:
    detector = CacheBreakDetector()
    detector.check("static", "semi", "dynamic", "tools", "model_a")
    result = detector.check("static", "semi", "dynamic", "tools", "model_b")
    assert result is not None
    assert result.estimated_tokens_lost == 0
