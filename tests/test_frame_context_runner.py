"""F110 v2 PR 2b: the runner hands pre_turn the session's last assistant text.

Both pre_turn call sites (run_turn and stream_chat) pass ``last_assistant_text``
only when ``frame_context_enabled`` is on and the conversation holds an
assistant message with text, so test doubles of pre_turn without the new
parameter keep working with the flag off.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from nous.api.anthropic_client import StreamEvent
from nous.api.models import Conversation, Message
from nous.api.runner import AgentRunner
from nous.api.tools import ToolDispatcher
from nous.cognitive.schemas import Assessment, FrameSelection, TurnContext
from nous.config import Settings

OFFER = "Want me to write it up?"


def _turn_context() -> TurnContext:
    return TurnContext(
        system_prompt="You are Nous.",
        frame=FrameSelection(
            frame_id="conversation", frame_name="Conversation", confidence=0.9, match_method="default"
        ),
        decision_id=None,
        active_censors=[],
        context_token_estimate=100,
    )


class _SpyCognitive:
    """Records the keyword arguments of every pre_turn call."""

    def __init__(self) -> None:
        self.pre_turn_kwargs: list[dict] = []

    async def pre_turn(self, agent_id, session_id, user_input, **kwargs):
        self.pre_turn_kwargs.append(kwargs)
        return _turn_context()

    async def post_turn(self, agent_id, session_id, turn_result, turn_context, **kwargs):
        return Assessment(actual=turn_result.response_text[:200])

    async def end_session(self, *args, **kwargs):
        return None

    async def list_frames(self, *args, **kwargs):
        return []


class _StrictCognitive(_SpyCognitive):
    """A pre_turn double with today's signature: no ``last_assistant_text``, no ``**kwargs``."""

    async def pre_turn(
        self,
        agent_id,
        session_id,
        user_input,
        session=None,
        *,
        conversation_messages=None,
        user_id=None,
        user_display_name=None,
        skip_episode=False,
        is_subtask=False,
        turn_number=None,
    ):
        self.pre_turn_kwargs.append({})
        return _turn_context()


class _Stub:
    async def close(self):
        pass


def _runner(cognitive, *, flag: bool) -> AgentRunner:
    settings = Settings(
        _env_file=None,
        ANTHROPIC_API_KEY="test-key",
        agent_id="test-agent",
        frame_context_enabled=flag,
    )
    runner = AgentRunner(cognitive, _Stub(), _Stub(), settings)
    runner._tool_loop = AsyncMock(return_value=("Done.", [], {"input_tokens": 1, "output_tokens": 1}, []))

    async def fake_stream(*args, **kwargs):
        yield StreamEvent(type="text_delta", text="Done.")
        yield StreamEvent(type="done", stop_reason="end_turn")

    runner._call_api_stream = MagicMock(side_effect=fake_stream)
    runner.set_dispatcher(ToolDispatcher())
    return runner


async def _turn(runner: AgentRunner, entry: str, session_id: str, text: str) -> None:
    if entry == "run_turn":
        await runner.run_turn(session_id, text)
    else:
        async for _event in runner.stream_chat(session_id, text):
            pass


def _seed(runner: AgentRunner, session_id: str, messages: list[Message]) -> None:
    runner._conversations[session_id] = Conversation(session_id=session_id, messages=messages)


ENTRIES = pytest.mark.parametrize("entry", ["run_turn", "stream_chat"])


@ENTRIES
async def test_runner_passes_last_assistant_text_when_flag_on(entry):
    cognitive = _SpyCognitive()
    runner = _runner(cognitive, flag=True)
    sid = f"fc-{uuid.uuid4().hex[:8]}"
    _seed(
        runner,
        sid,
        [
            Message(role="user", content="spec the frame carry", text_content="spec the frame carry"),
            Message(
                role="assistant",
                content=[
                    {"type": "text", "text": OFFER},
                    {"type": "tool_use", "id": "t1", "name": "recall_deep", "input": {"query": "frames"}},
                ],
            ),
        ],
    )
    try:
        await _turn(runner, entry, sid, "yes do it")
    finally:
        await runner.close()

    (kwargs,) = cognitive.pre_turn_kwargs
    assert kwargs["last_assistant_text"] == OFFER
    assert "last_assistant_age_s" not in kwargs


@ENTRIES
async def test_runner_passes_the_last_of_several_assistant_messages(entry):
    cognitive = _SpyCognitive()
    runner = _runner(cognitive, flag=True)
    sid = f"fc-{uuid.uuid4().hex[:8]}"
    _seed(
        runner,
        sid,
        [
            Message(role="user", content="hi", text_content="hi"),
            Message(role="assistant", content="Hello. What are we doing today?"),
            Message(role="user", content="spec it", text_content="spec it"),
            Message(role="assistant", content=OFFER),
        ],
    )
    try:
        await _turn(runner, entry, sid, "yes")
    finally:
        await runner.close()

    (kwargs,) = cognitive.pre_turn_kwargs
    assert kwargs["last_assistant_text"] == OFFER


@ENTRIES
async def test_runner_omits_kwargs_flag_off(entry):
    """Flag off: the call is today's, so a pre_turn double without the new parameters still works."""
    cognitive = _StrictCognitive()
    runner = _runner(cognitive, flag=False)
    sid = f"fc-{uuid.uuid4().hex[:8]}"
    _seed(
        runner,
        sid,
        [
            Message(role="user", content="spec it", text_content="spec it"),
            Message(role="assistant", content=OFFER),
        ],
    )
    try:
        await _turn(runner, entry, sid, "yes do it")
    finally:
        await runner.close()

    assert len(cognitive.pre_turn_kwargs) == 1


@ENTRIES
@pytest.mark.parametrize(
    "history",
    [
        [],
        [Message(role="user", content="earlier", text_content="earlier")],
        [
            Message(role="user", content="earlier", text_content="earlier"),
            Message(role="assistant", content=[{"type": "tool_use", "id": "t1", "name": "bash", "input": {}}]),
        ],
        [
            Message(role="user", content="earlier", text_content="earlier"),
            Message(role="assistant", content=["not a block"]),
        ],
    ],
    ids=["empty", "user-only", "assistant-without-text", "unreadable-content"],
)
async def test_runner_no_assistant_message_omits(entry, history):
    cognitive = _SpyCognitive()
    runner = _runner(cognitive, flag=True)
    sid = f"fc-{uuid.uuid4().hex[:8]}"
    _seed(runner, sid, list(history))
    try:
        await _turn(runner, entry, sid, "yes do it")
    finally:
        await runner.close()

    (kwargs,) = cognitive.pre_turn_kwargs
    assert "last_assistant_text" not in kwargs
