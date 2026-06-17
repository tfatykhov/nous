"""Integration tests for F024 inbound attachments wired into AgentRunner.

Reuses the test_runner.py harness pattern (MockCognitiveLayer + a mocked
_tool_loop). The KEY invariant verified here: base64 is sent to the API in
the outgoing multimodal blocks but is STRIPPED from the live in-memory
history after the turn (so it never reaches _save_conversation / the DB).

DB-backed coverage (real persist_attachment + record_attachment_fact) is
deferred to Task H — these tests stub Heart and disable disk persistence.
"""

import base64
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

from nous.api.runner import AgentRunner, StreamEvent
from nous.api.models import Attachment
from nous.api.attachments import classify_attachment
from nous.cognitive.schemas import FrameSelection, TurnContext
from nous.config import Settings


# A 1x1 transparent PNG.
_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR4nGNgYAAAAAMAASsJTYQAAAAASUVORK5CYII="
)


def _png_att() -> Attachment:
    raw = base64.b64decode(_PNG_B64)
    a = Attachment(
        filename="dot.png",
        media_type="image/png",
        data_base64=base64.b64encode(raw).decode(),
        size_bytes=len(raw),
        source="rest",
    )
    a.content_type = classify_attachment(a.filename, a.media_type)
    return a


# ---------------------------------------------------------------------------
# Minimal cognitive / brain / heart stubs (mirrors test_runner.py)
# ---------------------------------------------------------------------------


class _MockCognitive:
    def __init__(self) -> None:
        self.preset_context = TurnContext(
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

    async def pre_turn(self, agent_id, session_id, user_input, session=None, *,
                       conversation_messages=None, user_id=None,
                       user_display_name=None, skip_episode=False, is_subtask=False):
        return self.preset_context

    async def post_turn(self, agent_id, session_id, turn_result, turn_context,
                        session=None, is_background=False):
        from nous.cognitive.schemas import Assessment
        return Assessment(actual=turn_result.response_text[:200])

    async def end_session(self, agent_id, session_id, reflection=None, session=None):
        pass

    def get_active_episode_id(self, session_id):
        return None  # Fix A: no active episode in these stubs — safe (str | None)


class _MockBrain:
    async def close(self):
        pass


class _MockHeart:
    def __init__(self) -> None:
        self.learn = AsyncMock()

    async def close(self):
        pass


@pytest.fixture
def _attach_settings() -> Settings:
    return Settings(
        _env_file=None,
        ANTHROPIC_API_KEY="test-key-123",
        agent_id="test-agent",
        model="claude-sonnet-4-5-20250514",
        max_tokens=1024,
        attachments_enabled=True,
        attachments_persist=False,  # keep test pure — no disk I/O
    )


@pytest_asyncio.fixture
async def runner_with_fake_api(_attach_settings):
    """AgentRunner with a mocked _tool_loop that captures the outgoing payload.

    Returns (runner, captured) where captured["messages"] is the message list
    that WOULD be sent to the Anthropic API (built from the live conversation
    via _format_messages at call time, before history compaction strips base64).
    """
    runner = AgentRunner(_MockCognitive(), _MockBrain(), _MockHeart(), _attach_settings)
    captured: dict = {}

    async def _fake_tool_loop(*, conversation, **kwargs):
        # Snapshot the outgoing payload exactly as _tool_loop would build it.
        captured["messages"] = runner._format_messages(conversation)
        return ("Looks like a tiny PNG dot.", [], {"input_tokens": 10, "output_tokens": 5}, [])

    runner._tool_loop = _fake_tool_loop  # type: ignore[assignment]
    yield runner, captured
    await runner.close()


@pytest.mark.asyncio
async def test_run_turn_with_image_sends_blocks_and_compacts(runner_with_fake_api):
    runner, captured = runner_with_fake_api
    await runner.run_turn("sess-img", "what is this?", attachments=[_png_att()])

    sent_user = [m for m in captured["messages"] if m["role"] == "user"][-1]
    assert isinstance(sent_user["content"], list)
    assert any(b.get("type") == "image" for b in sent_user["content"])

    conv = await runner._get_or_create_conversation("sess-img")
    stored = [m for m in conv.messages if m.role == "user"][-1]
    assert "iVBOR" not in str(stored.content)  # base64 gone from live history


@pytest.mark.asyncio
async def test_run_turn_text_only_unchanged(runner_with_fake_api):
    runner, captured = runner_with_fake_api
    resp, ctx, usage = await runner.run_turn("sess-txt", "hello, no attachments")

    sent_user = [m for m in captured["messages"] if m["role"] == "user"][-1]
    assert sent_user["content"] == "hello, no attachments" or isinstance(sent_user["content"], str)
    assert isinstance(resp, str)


@pytest.mark.asyncio
async def test_save_conversation_never_persists_base64(runner_with_fake_api):
    """F1: even before compaction runs, _save_conversation must strip base64."""
    runner, _captured = runner_with_fake_api
    captured_save: dict = {}

    async def _fake_save_state(*, agent_id, session_id, summary, messages,
                               turn_count, compaction_count):
        captured_save["messages"] = messages

    runner._heart.save_conversation_state = _fake_save_state  # type: ignore[attr-defined]

    conv = await runner._get_or_create_conversation("sess-save")
    # Simulate the pre-compaction state: a live multimodal user message that
    # still carries base64 (compaction hasn't run yet).
    from nous.api.attachments import build_content_blocks
    from nous.api.models import Message
    att = _png_att()
    conv.messages.append(Message(
        role="user",
        content=build_content_blocks("what is this?", [att]),
        attachments=[att],
        text_content="what is this?",
    ))

    await runner._save_conversation("test-agent", "sess-save", conv)

    dumped = str(captured_save["messages"])
    assert "iVBOR" not in dumped  # base64 never reaches the DB serialization boundary


@pytest.mark.asyncio
async def test_run_turn_records_attachment_fact_on_success(runner_with_fake_api):
    """F4: a Heart fact is recorded (with the response as analysis) on success."""
    runner, _captured = runner_with_fake_api
    await runner.run_turn("sess-fact", "what is this?", attachments=[_png_att()])
    assert runner._heart.learn.await_count >= 1


# ---------------------------------------------------------------------------
# Streaming path + leak-proof tests (review-driven additions)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_chat_with_image_sends_blocks_and_compacts(runner_with_fake_api):
    """Streaming mirror of the run_turn image test: the outgoing user message
    carries an image block, and after the turn the live history has no base64
    (the stream_chat `finally` compaction ran)."""
    runner, _captured = runner_with_fake_api

    # stream_chat requires a dispatcher; it never dispatches a tool here.
    dispatcher = MagicMock()
    dispatcher.available_tools.return_value = []
    runner.set_dispatcher(dispatcher)

    sent: dict = {}

    async def _fake_stream(system_prompt, messages, tools=None):
        sent["messages"] = messages  # outgoing payload as built before the loop
        yield StreamEvent(type="text_delta", text="Looks like a tiny PNG dot.")
        yield StreamEvent(type="done", stop_reason="end_turn")

    runner._call_api_stream = MagicMock(side_effect=_fake_stream)  # type: ignore[assignment]

    events = [e async for e in runner.stream_chat("sess-stream-img", "what is this?",
                                                  attachments=[_png_att()])]
    assert any(e.type == "done" for e in events)

    # (a) outgoing user message contains an image block
    sent_user = [m for m in sent["messages"] if m["role"] == "user"][-1]
    assert isinstance(sent_user["content"], list)
    assert any(b.get("type") == "image" for b in sent_user["content"])

    # (b) base64 stripped from live history after the finally compaction ran
    conv = await runner._get_or_create_conversation("sess-stream-img")
    stored = [m for m in conv.messages if m.role == "user"][-1]
    assert "iVBOR" not in str(stored.content)


@pytest.mark.asyncio
async def test_run_turn_exception_path_strips_base64_and_records_no_fact(runner_with_fake_api):
    """When _tool_loop raises, the turn re-raises but the `finally` still
    compacts the live user message (no base64 left), and NO attachment fact
    is recorded (record is success-only)."""
    runner, _captured = runner_with_fake_api
    runner._tool_loop = AsyncMock(side_effect=RuntimeError("boom"))  # type: ignore[assignment]

    with pytest.raises(RuntimeError, match="boom"):
        await runner.run_turn("sess-exc", "what is this?", attachments=[_png_att()])

    # finally compaction ran → base64 gone from the stored user message
    conv = await runner._get_or_create_conversation("sess-exc")
    stored = [m for m in conv.messages if m.role == "user"][-1]
    assert "iVBOR" not in str(stored.content)

    # record_attachment_fact is success-only → heart.learn never awaited
    assert runner._heart.learn.await_count == 0


@pytest.mark.asyncio
async def test_censor_block_with_attachment_stays_text_only(runner_with_fake_api):
    """A censor-blocked turn must early-return as plain text before any
    attachment block / base64 is built — proving the censor path never holds
    base64."""
    runner, captured = runner_with_fake_api

    # Flip the stubbed pre_turn result to a blocked context.
    runner._cognitive.preset_context.censor_blocked = True
    runner._cognitive.preset_context.censor_block_reason = "Blocked: not allowed."

    resp, ctx, usage = await runner.run_turn(
        "sess-censor", "what is this?", attachments=[_png_att()])

    assert resp == "Blocked: not allowed."

    # Stored user message is a plain string (never upgraded to multimodal blocks).
    conv = await runner._get_or_create_conversation("sess-censor")
    stored = [m for m in conv.messages if m.role == "user"][-1]
    assert isinstance(stored.content, str)
    assert stored.content == "what is this?"
    assert "iVBOR" not in str(stored.content)

    # _tool_loop (and thus the captured outgoing payload) was never reached.
    assert "messages" not in captured
