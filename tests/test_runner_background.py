"""F048: Tests for ``is_background`` routing through AgentRunner.

Covers:
- _call_api routes to call_streaming_aggregated() when is_background=True
- _call_api falls back to call() when api_background_streaming_enabled is False
- run_turn threads is_background through to _tool_loop
- _tool_loop propagates is_background to every iteration's _call_api call
- Censor-block branch (pre-existing bug) now returns a 3-tuple
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

from nous.api.anthropic_client import StreamEvent
from nous.api.models import ApiResponse
from nous.api.runner import AgentRunner
from nous.cognitive.schemas import FrameSelection, TurnContext, TurnResult
from nous.config import Settings


# ---------------------------------------------------------------------------
# Reused scaffolding (parallels tests/test_runner.py mocks — local copy
# keeps this file self-contained).
# ---------------------------------------------------------------------------


class _MockCognitive:
    def __init__(self, preset: TurnContext | None = None) -> None:
        self.preset = preset or TurnContext(
            system_prompt="You are Nous.",
            frame=FrameSelection(
                frame_id="conversation",
                frame_name="Conversation",
                confidence=0.9,
                match_method="default",
            ),
            decision_id=None,
            active_censors=[],
            context_token_estimate=100,
        )

    async def pre_turn(self, agent_id, session_id, user_input, session=None, **_):
        return self.preset

    async def post_turn(self, agent_id, session_id, turn_result, turn_context, session=None, is_background=False):
        from nous.cognitive.schemas import Assessment
        return Assessment(actual=turn_result.response_text[:200])

    async def end_session(self, agent_id, session_id, reflection=None, session=None):
        pass

    async def list_frames(self, agent_id, session=None):
        return []


class _MockBrain:
    async def close(self):
        pass


class _MockHeart:
    async def close(self):
        pass


class _SpyApi:
    """Stub AnthropicClient recording which method the runner invoked."""

    def __init__(self, *, default_response: ApiResponse | None = None) -> None:
        self.default_response = default_response or ApiResponse(
            content=[{"type": "text", "text": "ok"}],
            stop_reason="end_turn",
            usage={"input_tokens": 10, "output_tokens": 5},
        )
        self.call = AsyncMock(return_value=self.default_response)
        self.call_streaming_aggregated = AsyncMock(return_value=self.default_response)
        self.stream = AsyncMock()

    async def start(self):
        pass

    async def close(self):
        pass


def _settings() -> Settings:
    return Settings(
        ANTHROPIC_API_KEY="test-key",
        agent_id="test-agent",
        api_background_streaming_enabled=True,
    )


@pytest_asyncio.fixture
async def runner_with_spy():
    """AgentRunner with a _SpyApi pre-wired. Start/close are skipped so
    no real httpx client is constructed."""
    cognitive = _MockCognitive()
    r = AgentRunner(cognitive, _MockBrain(), _MockHeart(), _settings())
    r._api = _SpyApi()
    yield r, r._api, cognitive
    # Mark api as "shared" so close() doesn't call r._api.close() — AsyncMock
    # returns fine either way, but keeps teardown honest.
    r._api_shared = True
    await r.close()


# ---------------------------------------------------------------------------
# _call_api routing tests (unit level — no run_turn, no tool_loop)
# ---------------------------------------------------------------------------


async def test_call_api_background_calls_streaming_aggregated(runner_with_spy):
    """F048: _call_api(is_background=True) routes through
    call_streaming_aggregated() when the feature flag is on."""
    r, api, _ = runner_with_spy
    await r._call_api(
        system_prompt="sys",
        messages=[{"role": "user", "content": "hi"}],
        tools=None,
        is_background=True,
    )
    assert api.call_streaming_aggregated.await_count == 1
    assert api.call.await_count == 0


async def test_call_api_default_is_foreground_uses_call(runner_with_spy):
    """F048: no is_background kwarg ⇒ foreground path uses call()."""
    r, api, _ = runner_with_spy
    await r._call_api(
        system_prompt="sys",
        messages=[{"role": "user", "content": "hi"}],
        tools=None,
    )
    assert api.call.await_count == 1
    assert api.call_streaming_aggregated.await_count == 0


async def test_call_api_background_respects_feature_flag_disabled(caplog):
    """F048: when api_background_streaming_enabled=False, is_background=True
    still falls back to call() (rollback switch). Also asserts a WARNING log
    is emitted so operators know the flag overrode the caller's request."""
    import logging

    cognitive = _MockCognitive()
    settings = Settings(
        ANTHROPIC_API_KEY="test-key",
        agent_id="test-agent",
        api_background_streaming_enabled=False,
    )
    r = AgentRunner(cognitive, _MockBrain(), _MockHeart(), settings)
    api = _SpyApi()
    r._api = api

    try:
        with caplog.at_level(logging.WARNING, logger="nous.api.runner"):
            await r._call_api(
                system_prompt="sys",
                messages=[{"role": "user", "content": "hi"}],
                tools=None,
                is_background=True,
            )
        assert api.call.await_count == 1
        assert api.call_streaming_aggregated.await_count == 0
        # F048 silent-failure fix: operator must see the override.
        assert any(
            "NOUS_API_BACKGROUND_STREAMING_ENABLED" in r.getMessage()
            for r in caplog.records if r.levelno == logging.WARNING
        ), (
            "expected WARNING log when feature flag disabled but is_background=True; "
            f"got: {[r.getMessage() for r in caplog.records]}"
        )
    finally:
        r._api_shared = True
        await r.close()


# ---------------------------------------------------------------------------
# run_turn / _tool_loop threading tests
# ---------------------------------------------------------------------------


async def test_run_turn_background_threads_is_background_to_tool_loop():
    """F048 P0-1: run_turn(is_background=True) passes is_background=True down
    into _tool_loop (which then passes it into _call_api on every iteration)."""
    cognitive = _MockCognitive()
    r = AgentRunner(cognitive, _MockBrain(), _MockHeart(), _settings())
    r._api = _SpyApi()

    received: dict = {}

    async def spy_tool_loop(**kwargs):
        received.update(kwargs)
        return (
            "response",
            [],
            {"input_tokens": 1, "output_tokens": 1},
            [],
        )

    r._tool_loop = spy_tool_loop

    try:
        session_id = f"s-{uuid.uuid4().hex[:8]}"
        response_text, turn_ctx, usage = await r.run_turn(
            session_id, "hi", is_background=True,
        )
        assert received.get("is_background") is True
        assert response_text == "response"
    finally:
        r._api_shared = True
        await r.close()


async def test_run_turn_default_is_foreground():
    """F048: when run_turn called without is_background, _tool_loop receives False."""
    cognitive = _MockCognitive()
    r = AgentRunner(cognitive, _MockBrain(), _MockHeart(), _settings())
    r._api = _SpyApi()

    received: dict = {}

    async def spy_tool_loop(**kwargs):
        received.update(kwargs)
        return "r", [], {"input_tokens": 1, "output_tokens": 1}, []

    r._tool_loop = spy_tool_loop

    try:
        await r.run_turn(f"s-{uuid.uuid4().hex[:8]}", "hi")
        assert received.get("is_background") is False
    finally:
        r._api_shared = True
        await r.close()


async def test_tool_loop_threads_is_background_to_all_iterations():
    """F048 P0-1: _tool_loop passes is_background into every _call_api call,
    including the tool-loop iteration calls AND the final wrap-up call."""
    cognitive = _MockCognitive()
    settings = _settings()
    settings.max_turns = 3
    r = AgentRunner(cognitive, _MockBrain(), _MockHeart(), settings)

    # Simulate: first call returns tool_use, second returns end_turn.
    call_count = 0
    recorded_is_background: list[bool] = []

    async def spy_call_api(system_prompt, messages, tools=None, skip_thinking=False,
                          model_override=None, is_background=False):
        nonlocal call_count
        call_count += 1
        recorded_is_background.append(is_background)
        if call_count == 1:
            return ApiResponse(
                content=[
                    {"type": "tool_use", "id": "t1", "name": "test_tool", "input": {}},
                ],
                stop_reason="tool_use",
            )
        return ApiResponse(
            content=[{"type": "text", "text": "done"}],
            stop_reason="end_turn",
        )

    r._call_api = spy_call_api

    class _Dispatcher:
        def available_tools(self, frame_id):
            return [{
                "name": "test_tool",
                "description": "t",
                "input_schema": {"type": "object"},
            }]

        async def dispatch(self, name, inp, session_id=None):
            return "tool result", False

    r.set_dispatcher(_Dispatcher())

    from nous.api.runner import Conversation, Message

    conv = Conversation(session_id="test-s")
    conv.messages.append(Message(role="user", content="go"))

    try:
        await r._tool_loop(
            system_prompt="sys",
            conversation=conv,
            frame_id="conversation",
            session_id="test-s",
            is_background=True,
        )
        # At least 2 _call_api iterations — both must have is_background=True.
        assert call_count >= 2
        assert all(recorded_is_background), (
            f"Expected all calls to propagate is_background=True, "
            f"got: {recorded_is_background}"
        )
    finally:
        r._api_shared = True
        await r.close()


# ---------------------------------------------------------------------------
# Censor-block regression: runner.py:249 returns a 3-tuple
# (pre-existing bug fixed by Impl-2 as part of F048)
# ---------------------------------------------------------------------------


async def test_run_turn_censor_blocked_returns_3_tuple():
    """F048 P0-4 regression: censor-blocked branch must return
    (response_text, turn_context, usage) — a 3-tuple that unpacks cleanly.

    Before the fix, the branch returned (response_text, usage) and crashed
    every caller that expected three values."""
    blocked_ctx = TurnContext(
        system_prompt="You are Nous.",
        frame=FrameSelection(
            frame_id="conversation",
            frame_name="Conversation",
            confidence=0.9,
            match_method="default",
        ),
        decision_id=None,
        active_censors=[],
        context_token_estimate=100,
        censor_blocked=True,
        censor_block_reason="Refused: test reason.",
    )

    cognitive = _MockCognitive(preset=blocked_ctx)
    r = AgentRunner(cognitive, _MockBrain(), _MockHeart(), _settings())
    r._api = _SpyApi()
    # _tool_loop must NOT be reached on the censor-block branch.
    r._tool_loop = AsyncMock(side_effect=AssertionError("tool_loop should be skipped"))

    try:
        result = await r.run_turn(f"s-{uuid.uuid4().hex[:8]}", "do something bad")
        # The assertion: must unpack as 3 values without raising.
        assert isinstance(result, tuple)
        assert len(result) == 3
        response_text, turn_ctx, usage = result
        assert isinstance(response_text, str)
        assert "Refused" in response_text
        assert isinstance(turn_ctx, TurnContext)
        assert turn_ctx.censor_blocked is True
        assert isinstance(usage, dict)
        assert "input_tokens" in usage
    finally:
        r._api_shared = True
        await r.close()
