"""Tests for F036 runner integration — system prompt tier split and cache control."""

from __future__ import annotations

from unittest.mock import MagicMock

from nous.api.runner import AgentRunner
from nous.cognitive.schemas import FrameSelection, TurnContext
from nous.config import Settings


def _make_frame(frame_id: str = "conversation") -> FrameSelection:
    return FrameSelection(
        frame_id=frame_id,
        frame_name=frame_id.capitalize(),
        confidence=0.9,
        match_method="keyword",
    )


def _make_turn_context(
    *,
    sections_by_tier: dict[str, str] | None = None,
    frame_id: str = "conversation",
    system_prompt: str = "base prompt",
    diagnostic_nudges: str = "",
) -> TurnContext:
    return TurnContext(
        system_prompt=system_prompt,
        frame=_make_frame(frame_id),
        sections_by_tier=sections_by_tier or {},
        diagnostic_nudges=diagnostic_nudges,
    )


def _make_runner(
    *,
    cache_split: bool = True,
    single_breakpoint: bool = True,
    cache_break_detection: bool = False,
) -> AgentRunner:
    settings = Settings(
        NOUS_CACHE_SPLIT_SYSTEM_PROMPT=str(cache_split).lower(),
        NOUS_CACHE_SINGLE_BREAKPOINT=str(single_breakpoint).lower(),
        NOUS_CACHE_BREAK_DETECTION_ENABLED=str(cache_break_detection).lower(),
    )
    cognitive = MagicMock()
    brain = MagicMock()
    heart = MagicMock()
    return AgentRunner(cognitive=cognitive, brain=brain, heart=heart, settings=settings)


# ── Test 1: 3-block system prompt when split enabled ──


def test_build_system_prompt_returns_dict_when_split_enabled() -> None:
    runner = _make_runner(cache_split=True)
    tc = _make_turn_context(
        sections_by_tier={
            "static": "## Identity\n\nI am Nous.",
            "semi_stable": "## Current Frame\n\nConversation frame.",
            "dynamic": "## Working Memory\n\nCurrent task: test.",
        },
    )
    result = runner._build_system_prompt(tc)
    assert isinstance(result, dict)
    assert "static" in result
    assert "semi_stable" in result
    assert "dynamic" in result


def test_build_api_payload_3_system_blocks_when_split() -> None:
    runner = _make_runner(cache_split=True)
    tc = _make_turn_context(
        sections_by_tier={
            "static": "## Identity\n\nI am Nous.",
            "semi_stable": "## Current Frame\n\nConversation frame.",
            "dynamic": "## Working Memory\n\nCurrent task: test.",
        },
    )
    prompt = runner._build_system_prompt(tc)
    messages = [{"role": "user", "content": "hello"}]
    payload = runner._build_api_payload(prompt, messages)
    system_blocks = payload["system"]
    assert len(system_blocks) == 4  # preamble + static + semi_stable + dynamic
    assert system_blocks[0]["text"] == "You are Claude Code, Anthropic's official CLI for Claude."
    assert system_blocks[1]["text"] == "## Identity\n\nI am Nous."
    assert system_blocks[2]["text"] == "## Current Frame\n\nConversation frame."
    # Dynamic block text starts with working memory (may have frame instructions appended)
    assert "## Working Memory" in system_blocks[3]["text"]


# ── Test 2: 2-block fallback when split disabled ──


def test_build_system_prompt_returns_string_when_split_disabled() -> None:
    runner = _make_runner(cache_split=False)
    tc = _make_turn_context(
        sections_by_tier={
            "static": "## Identity",
            "semi_stable": "## Frame",
            "dynamic": "## Memory",
        },
    )
    result = runner._build_system_prompt(tc)
    assert isinstance(result, str)


def test_build_system_prompt_returns_string_when_tiers_empty() -> None:
    runner = _make_runner(cache_split=True)
    tc = _make_turn_context(sections_by_tier={})
    result = runner._build_system_prompt(tc)
    assert isinstance(result, str)


def test_build_api_payload_2_blocks_when_split_disabled() -> None:
    runner = _make_runner(cache_split=False)
    tc = _make_turn_context()
    prompt = runner._build_system_prompt(tc)
    messages = [{"role": "user", "content": "hello"}]
    payload = runner._build_api_payload(prompt, messages)
    system_blocks = payload["system"]
    assert len(system_blocks) == 2
    # Legacy path: first block is the "Claude Code" preamble
    assert "Claude Code" in system_blocks[0]["text"]


# ── Test 3: Dynamic tier gets runner-appended content ──


def test_frame_instructions_appended_to_dynamic_tier() -> None:
    runner = _make_runner(cache_split=True)
    tc = _make_turn_context(
        frame_id="decision",
        sections_by_tier={
            "static": "identity",
            "semi_stable": "frame context",
            "dynamic": "working memory",
        },
    )
    result = runner._build_system_prompt(tc)
    assert isinstance(result, dict)
    # Decision frame instructions should be appended to dynamic
    assert "## Tool Instructions" in result["dynamic"]
    assert "DECISION frame" in result["dynamic"]
    # Static and semi_stable should be unchanged
    assert result["static"] == "identity"
    assert result["semi_stable"] == "frame context"


def test_ledger_appended_to_dynamic_tier() -> None:
    runner = _make_runner(cache_split=True)
    tc = _make_turn_context(
        sections_by_tier={
            "static": "identity",
            "semi_stable": "frame",
            "dynamic": "memory",
        },
    )
    ledger = MagicMock()
    ledger.system_prompt_section.return_value = "## Execution Ledger\nAction 1: done"
    result = runner._build_system_prompt(tc, ledger=ledger)
    assert isinstance(result, dict)
    assert "Execution Ledger" in result["dynamic"]


def test_corrections_appended_to_dynamic_tier() -> None:
    runner = _make_runner(cache_split=True)
    tc = _make_turn_context(
        sections_by_tier={
            "static": "identity",
            "semi_stable": "frame",
            "dynamic": "memory",
        },
    )
    result = runner._build_system_prompt(tc, corrections=["fix typo", "use correct API"])
    assert isinstance(result, dict)
    assert "[Previous Turn Corrections]" in result["dynamic"]
    assert "fix typo" in result["dynamic"]
    assert "use correct API" in result["dynamic"]


def test_diagnostic_nudges_appended_to_dynamic_tier() -> None:
    runner = _make_runner(cache_split=True)
    tc = _make_turn_context(
        sections_by_tier={
            "static": "identity",
            "semi_stable": "frame",
            "dynamic": "",
        },
        diagnostic_nudges="Consider checking memory first.",
    )
    result = runner._build_system_prompt(tc)
    assert isinstance(result, dict)
    assert "Consider checking memory first." in result["dynamic"]


# ── Test 4: system_prompt_prefix goes to static tier ──


def test_system_prompt_prefix_prepends_to_static_tier() -> None:
    """When system_prompt is a dict, prefix prepends to static tier.

    This is tested at the call site level since _build_system_prompt
    does not handle prefix — it is applied by the caller (run/stream).
    We test the dict manipulation logic directly.
    """
    runner = _make_runner(cache_split=True)
    tc = _make_turn_context(
        sections_by_tier={
            "static": "## Identity\n\nI am Nous.",
            "semi_stable": "## Frame",
            "dynamic": "## Memory",
        },
    )
    system_prompt = runner._build_system_prompt(tc)
    assert isinstance(system_prompt, dict)

    # Simulate the prefix logic from run()/stream() in runner.py
    prefix = "Custom system prefix"
    existing = system_prompt.get("static", "")
    system_prompt["static"] = prefix + "\n\n" + existing if existing else prefix

    assert system_prompt["static"].startswith("Custom system prefix")
    assert "## Identity" in system_prompt["static"]


def test_system_prompt_prefix_prepends_to_flat_string() -> None:
    """When system_prompt is a string, prefix prepends normally."""
    runner = _make_runner(cache_split=False)
    tc = _make_turn_context()
    system_prompt = runner._build_system_prompt(tc)
    assert isinstance(system_prompt, str)

    # Simulate the prefix logic
    prefix = "Custom system prefix"
    system_prompt = prefix + "\n\n" + system_prompt
    assert system_prompt.startswith("Custom system prefix")


# ── Test 5: Cache break detector wired ──


def test_cache_break_detector_created_when_enabled() -> None:
    runner = _make_runner(cache_break_detection=True)
    assert runner._cache_break_detector is not None


def test_cache_break_detector_none_when_disabled() -> None:
    runner = _make_runner(cache_break_detection=False)
    assert runner._cache_break_detector is None


# ── Test 6: Single breakpoint strategy ──


def test_static_block_always_has_cache_control() -> None:
    runner = _make_runner(cache_split=True, single_breakpoint=True)
    tc = _make_turn_context(
        sections_by_tier={
            "static": "identity text",
            "semi_stable": "frame text",
            "dynamic": "memory text",
        },
    )
    prompt = runner._build_system_prompt(tc)
    messages = [{"role": "user", "content": "hello"}]
    payload = runner._build_api_payload(prompt, messages)
    # Block 0: preamble, Block 1: static identity — both always cached
    block0 = payload["system"][0]
    block1 = payload["system"][1]
    assert "cache_control" in block0
    assert block0["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" in block1
    assert block1["cache_control"] == {"type": "ephemeral"}


def test_dynamic_block_never_has_cache_control() -> None:
    runner = _make_runner(cache_split=True, single_breakpoint=True)
    tc = _make_turn_context(
        sections_by_tier={
            "static": "identity text",
            "semi_stable": "frame text",
            "dynamic": "memory text",
        },
    )
    prompt = runner._build_system_prompt(tc)
    messages = [{"role": "user", "content": "hello"}]
    payload = runner._build_api_payload(prompt, messages)
    # Dynamic is block index 3 (after preamble + static + semi_stable)
    block3 = payload["system"][3]
    assert "cache_control" not in block3


def test_last_user_message_has_cache_control() -> None:
    runner = _make_runner(cache_split=True, single_breakpoint=True)
    tc = _make_turn_context(
        sections_by_tier={
            "static": "identity",
            "semi_stable": "frame",
            "dynamic": "memory",
        },
    )
    prompt = runner._build_system_prompt(tc)
    messages = [
        {"role": "user", "content": "first message"},
        {"role": "assistant", "content": "response"},
        {"role": "user", "content": "second message"},
    ]
    payload = runner._build_api_payload(prompt, messages)
    # Last user message should be converted to list with cache_control
    last_user = payload["messages"][-1]
    assert last_user["role"] == "user"
    assert isinstance(last_user["content"], list)
    assert last_user["content"][0]["cache_control"] == {"type": "ephemeral"}


def test_non_last_user_message_no_cache_control() -> None:
    runner = _make_runner(cache_split=True)
    messages = [
        {"role": "user", "content": "first message"},
        {"role": "assistant", "content": "response"},
        {"role": "user", "content": "second message"},
    ]
    tc = _make_turn_context(
        sections_by_tier={
            "static": "identity",
            "semi_stable": "frame",
            "dynamic": "memory",
        },
    )
    prompt = runner._build_system_prompt(tc)
    payload = runner._build_api_payload(prompt, messages)
    # First user message should remain a plain string
    first_user = payload["messages"][0]
    assert first_user["role"] == "user"
    assert isinstance(first_user["content"], str)


# ── Test 7: Context logger receives flat string ──


def test_context_logger_receives_flat_string() -> None:
    runner = _make_runner(cache_split=True)
    logger_mock = MagicMock()
    entry_mock = MagicMock()
    entry_mock.id = "test-entry-id"
    logger_mock.log.return_value = entry_mock
    runner._context_logger = logger_mock

    tc = _make_turn_context(
        sections_by_tier={
            "static": "## Identity\n\nI am Nous.",
            "semi_stable": "## Frame\n\nConversation.",
            "dynamic": "## Memory\n\nTask: test.",
        },
    )
    prompt = runner._build_system_prompt(tc)
    messages = [{"role": "user", "content": "hello"}]
    runner._build_api_payload(prompt, messages)

    # Verify log was called
    logger_mock.log.assert_called_once()
    call_kwargs = logger_mock.log.call_args
    # system_prompt kwarg should be a flat string (joined tiers)
    system_prompt_arg = call_kwargs.kwargs.get("system_prompt") or call_kwargs[1].get("system_prompt")
    assert isinstance(system_prompt_arg, str)
    assert "## Identity" in system_prompt_arg
    assert "## Frame" in system_prompt_arg
    assert "## Memory" in system_prompt_arg


def test_context_logger_receives_flat_string_legacy_path() -> None:
    runner = _make_runner(cache_split=False)
    logger_mock = MagicMock()
    entry_mock = MagicMock()
    entry_mock.id = "test-entry-id"
    logger_mock.log.return_value = entry_mock
    runner._context_logger = logger_mock

    tc = _make_turn_context()
    prompt = runner._build_system_prompt(tc)
    messages = [{"role": "user", "content": "hello"}]
    runner._build_api_payload(prompt, messages)

    logger_mock.log.assert_called_once()
    call_kwargs = logger_mock.log.call_args
    system_prompt_arg = call_kwargs.kwargs.get("system_prompt") or call_kwargs[1].get("system_prompt")
    assert isinstance(system_prompt_arg, str)
