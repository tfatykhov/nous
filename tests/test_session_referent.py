"""F110 v2 PR 4: the session referent.

``ChannelLastReply`` keeps each channel's last assistant reply in process. The runner
writes it at the end of every interactive turn that has a channel and reads it on a
new session's first turn (empty conversation), so a short "yes" after the Telegram
session roll still reaches ``pre_turn`` with the offer it answers
(``last_assistant_text`` and ``last_assistant_age_s``). Behind
``NOUS_SESSION_REFERENT_ENABLED``; a conversation with messages keeps PR 2b's
in-session text and never reads the store.
"""

from __future__ import annotations

import time
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from nous.api.anthropic_client import StreamEvent
from nous.api.execution_context import ExecutionContext
from nous.api.models import Conversation, Message
from nous.api.runner import AgentRunner
from nous.api.session_referent import ChannelLastReply
from nous.api.tools import ToolDispatcher
from nous.cognitive.schemas import Assessment, FrameSelection, TurnContext
from nous.config import Settings

DAY = 24 * 3600.0
CH = "telegram:42"
OFFER = "Want me to write it up?"
REPLY = "Here is the plan. Want me to write it up?"
REFERENT_KWARGS = ("last_assistant_text", "last_assistant_age_s")
ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

SESSION_REFERENT_DEFAULTS = {
    "session_referent_enabled": False,
    "session_referent_max_age_hours": 24,
    "session_referent_max_chars": 1200,
}


def test_session_referent_settings_defaults():
    s = Settings(_env_file=None)
    assert {k: getattr(s, k) for k in SESSION_REFERENT_DEFAULTS} == SESSION_REFERENT_DEFAULTS


def test_session_referent_settings_environment_names(monkeypatch):
    monkeypatch.setenv("NOUS_SESSION_REFERENT_ENABLED", "true")
    monkeypatch.setenv("NOUS_SESSION_REFERENT_MAX_AGE_HOURS", "6")
    monkeypatch.setenv("NOUS_SESSION_REFERENT_MAX_CHARS", "300")
    s = Settings(_env_file=None)
    assert (s.session_referent_enabled, s.session_referent_max_age_hours, s.session_referent_max_chars) == (
        True,
        6,
        300,
    )


@pytest.mark.parametrize("field", ["session_referent_max_age_hours", "session_referent_max_chars"])
def test_session_referent_bound_below_one_is_rejected(field):
    """0 would make the window empty, or the tail ``text[-0:]`` the whole reply."""
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: 0})


def test_session_referent_settings_have_doc_rows():
    doc = (ROOT / "docs" / "reference" / "environment-variables.md").read_text(encoding="utf-8")
    for field, default in SESSION_REFERENT_DEFAULTS.items():
        value = str(default).lower() if isinstance(default, bool) else str(default)
        assert f"| `NOUS_{field.upper()}` | `{value}` |" in doc, field


# ---------------------------------------------------------------------------
# The store (pure, no runner)
# ---------------------------------------------------------------------------


def _store(*, max_chars: int = 1200) -> ChannelLastReply:
    return ChannelLastReply(max_age_s=DAY, max_chars=max_chars)


def test_lookup_same_channel_new_session_within_window():
    store = _store()
    store.record(CH, "s1", OFFER, now=1000.0)

    assert store.lookup(CH, "s2", now=1000.0 + 7 * 3600) == (OFFER, 7 * 3600)
    assert store.lookup(CH, "s2", now=1000.0 + DAY) == (OFFER, DAY)  # the window's edge is inside


def test_lookup_same_session_returns_none():
    store = _store()
    store.record(CH, "s1", OFFER, now=1000.0)

    assert store.lookup(CH, "s1", now=1060.0) is None


def test_lookup_other_channel_none():
    store = _store()
    store.record(CH, "s1", OFFER, now=1000.0)

    assert store.lookup("telegram:7", "s2", now=1060.0) is None


def test_lookup_expired_none():
    store = _store()
    store.record(CH, "s1", OFFER, now=1000.0)

    assert store.lookup(CH, "s2", now=1000.0 + DAY + 1) is None


def test_tail_cap_keeps_end():
    """The offer is the last paragraph, so the cap keeps the end of the reply."""
    body = "Findings.\n" + "x" * 5000 + "\n\n" + OFFER
    store = _store(max_chars=1200)
    store.record(CH, "s1", body, now=0.0)

    tail, _age = store.lookup(CH, "s2", now=1.0)
    assert tail == body[-1200:]
    assert tail.endswith(OFFER)
    assert not tail.startswith("Findings.")


def test_prune_drops_old():
    store = _store()
    store.record("telegram:1", "s1", "old", now=0.0)
    store.record("telegram:2", "s2", "new", now=DAY - 10)  # the first is DAY - 10 old: kept
    assert set(store._replies) == {"telegram:1", "telegram:2"}

    store.prune(now=DAY + 1)

    assert set(store._replies) == {"telegram:2"}


def test_record_prunes_on_write():
    store = _store()
    store.record("telegram:1", "s1", "old", now=0.0)

    store.record("telegram:2", "s2", "new", now=DAY + 1)

    assert set(store._replies) == {"telegram:2"}


def test_record_ignores_blank_reply():
    """An empty reply (a turn cancelled before any text) does not replace the offer the user saw."""
    store = _store()
    store.record(CH, "s1", OFFER, now=0.0)

    store.record(CH, "s1", "", now=1.0)
    store.record(CH, "s1", "  \n", now=2.0)

    assert store.lookup(CH, "s2", now=3.0) == (OFFER, 3.0)


def test_record_replaces_the_channel_entry():
    store = _store()
    store.record(CH, "s1", "first", now=0.0)
    store.record(CH, "s2", "second", now=5.0)

    assert store.lookup(CH, "s3", now=10.0) == ("second", 5.0)
    assert store.lookup(CH, "s2", now=10.0) is None


# ---------------------------------------------------------------------------
# The runner
# ---------------------------------------------------------------------------


def _turn_context(*, censor_blocked: bool = False) -> TurnContext:
    return TurnContext(
        system_prompt="You are Nous.",
        frame=FrameSelection(
            frame_id="conversation", frame_name="Conversation", confidence=0.9, match_method="default"
        ),
        decision_id=None,
        active_censors=[],
        context_token_estimate=100,
        censor_blocked=censor_blocked,
        censor_block_reason="Blocked by a censor." if censor_blocked else None,
    )


class _SpyCognitive:
    """Records the keyword arguments of every pre_turn call."""

    def __init__(self, *, censor_blocked: bool = False) -> None:
        self.pre_turn_kwargs: list[dict] = []
        self._censor_blocked = censor_blocked

    async def pre_turn(self, agent_id, session_id, user_input, **kwargs):
        self.pre_turn_kwargs.append(kwargs)
        return _turn_context(censor_blocked=self._censor_blocked)

    async def post_turn(self, agent_id, session_id, turn_result, turn_context, **kwargs):
        return Assessment(actual=turn_result.response_text[:200])

    async def end_session(self, *args, **kwargs):
        return None

    async def list_frames(self, *args, **kwargs):
        return []


class _StrictCognitive(_SpyCognitive):
    """A pre_turn double with today's signature: no ``last_assistant_text``, no ``last_assistant_age_s``."""

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
        channel=None,
        context_kind=None,
    ):
        self.pre_turn_kwargs.append({})
        return _turn_context()


class _Stub:
    async def close(self):
        pass


def _runner(
    cognitive,
    *,
    referent: bool,
    frame_context: bool = False,
    reply: str = REPLY,
    **settings_kwargs,
) -> AgentRunner:
    settings = Settings(
        _env_file=None,
        ANTHROPIC_API_KEY="test-key",
        agent_id="test-agent",
        frame_context_enabled=frame_context,
        session_referent_enabled=referent,
        **settings_kwargs,
    )
    runner = AgentRunner(cognitive, _Stub(), _Stub(), settings)
    runner._tool_loop = AsyncMock(return_value=(reply, [], {"input_tokens": 1, "output_tokens": 1}, []))

    async def fake_stream(*args, **kwargs):
        yield StreamEvent(type="text_delta", text=reply)
        yield StreamEvent(type="done", stop_reason="end_turn")

    runner._call_api_stream = MagicMock(side_effect=fake_stream)
    runner.set_dispatcher(ToolDispatcher())
    return runner


async def _turn(runner: AgentRunner, entry: str, session_id: str, text: str, *, channel: str | None) -> None:
    if entry == "run_turn":
        await runner.run_turn(
            session_id, text, context=ExecutionContext(kind="interactive", session_id=session_id, channel=channel)
        )
    else:
        async for _event in runner.stream_chat(session_id, text, channel=channel):
            pass


def _sid() -> str:
    return f"sr-{uuid.uuid4().hex[:8]}"


def _no_referent(kwargs: dict) -> bool:
    return not any(k in kwargs for k in REFERENT_KWARGS)


ENTRIES = pytest.mark.parametrize("entry", ["run_turn", "stream_chat"])


@ENTRIES
async def test_runner_first_turn_new_session_passes_text_and_age(entry):
    cognitive = _SpyCognitive()
    runner = _runner(cognitive, referent=True)
    first, second = _sid(), _sid()
    try:
        await _turn(runner, entry, first, "plan the frame carry", channel=CH)
        await _turn(runner, entry, second, "yes", channel=CH)
    finally:
        await runner.close()

    before, after = cognitive.pre_turn_kwargs
    assert _no_referent(before)  # nothing stored yet on the channel's first turn
    assert after["last_assistant_text"] == REPLY
    assert 0.0 <= after["last_assistant_age_s"] < 5.0


@ENTRIES
async def test_runner_passes_the_stored_age(entry):
    cognitive = _SpyCognitive()
    runner = _runner(cognitive, referent=True)
    runner._channel_last_reply.record(CH, "earlier", OFFER, time.monotonic() - 600)
    try:
        await _turn(runner, entry, _sid(), "yes", channel=CH)
    finally:
        await runner.close()

    (kwargs,) = cognitive.pre_turn_kwargs
    assert kwargs["last_assistant_text"] == OFFER
    assert 600.0 <= kwargs["last_assistant_age_s"] < 605.0


@ENTRIES
async def test_runner_uses_the_window_and_cap_settings(entry):
    cognitive = _SpyCognitive()
    runner = _runner(cognitive, referent=True, session_referent_max_age_hours=1, session_referent_max_chars=8)
    try:
        await _turn(runner, entry, _sid(), "plan it", channel=CH)
        await _turn(runner, entry, _sid(), "yes", channel=CH)
        runner._channel_last_reply.record("telegram:9", "earlier", OFFER, time.monotonic() - 3601)
        await _turn(runner, entry, _sid(), "yes", channel="telegram:9")
    finally:
        await runner.close()

    _first, capped, expired = cognitive.pre_turn_kwargs
    assert capped["last_assistant_text"] == REPLY[-8:]
    assert _no_referent(expired)


@ENTRIES
async def test_flag_off_runner_never_records_or_passes(entry):
    """Flag off: nothing is stored and the call is today's (a double without the new parameters still works)."""
    cognitive = _StrictCognitive()
    runner = _runner(cognitive, referent=False)
    try:
        await _turn(runner, entry, _sid(), "plan it", channel=CH)
        await _turn(runner, entry, _sid(), "yes", channel=CH)
    finally:
        await runner.close()

    assert len(cognitive.pre_turn_kwargs) == 2
    assert runner._channel_last_reply._replies == {}


@ENTRIES
async def test_flag_off_ignores_a_stored_reply(entry):
    cognitive = _StrictCognitive()
    runner = _runner(cognitive, referent=False)
    runner._channel_last_reply.record(CH, "earlier", OFFER, time.monotonic() - 60)
    try:
        await _turn(runner, entry, _sid(), "yes", channel=CH)
    finally:
        await runner.close()

    assert len(cognitive.pre_turn_kwargs) == 1


@ENTRIES
async def test_no_channel_no_referent(entry):
    """The dashboard and a bare REST client send no channel: nothing is stored or passed."""
    cognitive = _SpyCognitive()
    runner = _runner(cognitive, referent=True)
    try:
        await _turn(runner, entry, _sid(), "plan it", channel=None)
        await _turn(runner, entry, _sid(), "yes", channel=None)
    finally:
        await runner.close()

    assert all(_no_referent(kwargs) for kwargs in cognitive.pre_turn_kwargs)
    assert runner._channel_last_reply._replies == {}


@ENTRIES
async def test_restart_empty_store_is_today(entry):
    """A new process starts with an empty store, so the first turn after a restart is today's."""
    before_restart = _runner(_SpyCognitive(), referent=True)
    try:
        await _turn(before_restart, entry, _sid(), "plan it", channel=CH)
    finally:
        await before_restart.close()

    cognitive = _SpyCognitive()
    after_restart = _runner(cognitive, referent=True)
    try:
        await _turn(after_restart, entry, _sid(), "yes", channel=CH)
    finally:
        await after_restart.close()

    (kwargs,) = cognitive.pre_turn_kwargs
    assert _no_referent(kwargs)


@ENTRIES
@pytest.mark.parametrize("frame_context", [True, False], ids=["frame-context-on", "frame-context-off"])
async def test_conversation_with_messages_does_not_read_store(entry, frame_context):
    """In-session wins: a conversation with messages passes PR 2b's text (frame context on) or nothing,
    never the store's."""
    cognitive = _SpyCognitive()
    runner = _runner(cognitive, referent=True, frame_context=frame_context)
    runner._channel_last_reply.record(CH, "earlier", "Stored reply from the last session.", time.monotonic() - 60)
    sid = _sid()
    runner._conversations[sid] = Conversation(
        session_id=sid,
        messages=[
            Message(role="user", content="spec it", text_content="spec it"),
            Message(role="assistant", content=OFFER),
        ],
    )
    try:
        await _turn(runner, entry, sid, "yes", channel=CH)
    finally:
        await runner.close()

    (kwargs,) = cognitive.pre_turn_kwargs
    assert "last_assistant_age_s" not in kwargs
    if frame_context:
        assert kwargs["last_assistant_text"] == OFFER
    else:
        assert "last_assistant_text" not in kwargs


@ENTRIES
async def test_user_only_history_does_not_read_store(entry):
    cognitive = _SpyCognitive()
    runner = _runner(cognitive, referent=True)
    runner._channel_last_reply.record(CH, "earlier", OFFER, time.monotonic() - 60)
    sid = _sid()
    runner._conversations[sid] = Conversation(
        session_id=sid, messages=[Message(role="user", content="earlier", text_content="earlier")]
    )
    try:
        await _turn(runner, entry, sid, "yes", channel=CH)
    finally:
        await runner.close()

    (kwargs,) = cognitive.pre_turn_kwargs
    assert _no_referent(kwargs)


@ENTRIES
async def test_censor_blocked_turn_records_its_reply(entry):
    cognitive = _SpyCognitive(censor_blocked=True)
    runner = _runner(cognitive, referent=True)
    try:
        await _turn(runner, entry, _sid(), "something blocked", channel=CH)
    finally:
        await runner.close()

    assert runner._channel_last_reply.lookup(CH, _sid(), time.monotonic())[0] == "Blocked by a censor."


@ENTRIES
async def test_errored_turn_keeps_the_stored_offer(entry):
    """An errored turn's reply ("I encountered an error...") never replaces the channel's stored offer."""
    cognitive = _SpyCognitive()
    runner = _runner(cognitive, referent=True, reply=OFFER)
    first = _sid()
    try:
        await _turn(runner, entry, first, "plan it", channel=CH)
        working = runner._tool_loop, runner._call_api_stream
        runner._tool_loop = AsyncMock(side_effect=RuntimeError("boom"))
        runner._call_api_stream = MagicMock(side_effect=RuntimeError("boom"))
        with pytest.raises(RuntimeError):
            await _turn(runner, entry, first, "and the rest?", channel=CH)
        assert runner._conversations[first].messages[-1].content.startswith("I encountered an error")
        runner._tool_loop, runner._call_api_stream = working
        await _turn(runner, entry, _sid(), "yes", channel=CH)
    finally:
        await runner.close()

    assert cognitive.pre_turn_kwargs[-1]["last_assistant_text"] == OFFER


@ENTRIES
async def test_store_errors_fail_open(entry):
    """A store that raises on read or write never changes the turn: no referent, the reply still returned."""
    cognitive = _SpyCognitive()
    runner = _runner(cognitive, referent=True)
    runner._channel_last_reply = MagicMock()
    runner._channel_last_reply.lookup.side_effect = RuntimeError("boom")
    runner._channel_last_reply.record.side_effect = RuntimeError("boom")
    try:
        await _turn(runner, entry, _sid(), "yes", channel=CH)
    finally:
        await runner.close()

    (kwargs,) = cognitive.pre_turn_kwargs
    assert _no_referent(kwargs)
    runner._channel_last_reply.lookup.assert_called_once()
    runner._channel_last_reply.record.assert_called_once()


# ---------------------------------------------------------------------------
# I1: the reply is stripped and redacted BEFORE the store cuts it
# ---------------------------------------------------------------------------


async def test_secret_straddling_the_store_cut_never_reaches_prompt_or_critic(db, heart, settings, monkeypatch):
    """A reply over max_chars with an own secret straddling the cut: no 8+ char fragment of it reaches the next
    session's system prompt or the critic's previous-message input (redaction precedes the store's cut)."""
    from nous.brain.brain import Brain
    from nous.cognitive.critic_schemas import CriticResult, RoutingMode
    from nous.cognitive.layer import CognitiveLayer
    from nous.cognitive.ledger_store import _own_secrets

    secret = "Zq7Kp2Xw9Lm4Tn8Vb3Yc6Rd1Sf5Gh0J"
    monkeypatch.setenv("NOUS_TEST_F110_TOKEN", secret)
    _own_secrets.cache_clear()
    try:
        monkeypatch.setattr(settings, "session_referent_enabled", True)
        monkeypatch.setattr(settings, "frame_context_enabled", True)
        monkeypatch.setattr(settings, "critic_enabled", True)
        monkeypatch.setattr(settings, "critic_mode", "shadow")
        max_chars = settings.session_referent_max_chars
        # The cut falls 12 characters into the secret: its last 19 characters sit at the head of the stored tail.
        trailing = max_chars - len(secret) + 12
        reply = "intro words " * 40 + secret + (" ab cd" * trailing)[:trailing]
        assert len(reply) - max_chars > 0

        brain = Brain(database=db, settings=settings)
        cognitive = CognitiveLayer(brain, heart, settings, identity_prompt="You are Nous.")
        seen: list[dict] = []

        async def classify(**kwargs):
            seen.append(kwargs)
            return CriticResult(
                routing=RoutingMode.PASSTHROUGH, recommended_frame=kwargs["heuristic_frame"].frame_id, rationale="x"
            )

        critic = MagicMock()
        critic.classify = classify
        monkeypatch.setattr(cognitive, "_critic", critic)
        monkeypatch.setattr(cognitive, "post_turn", AsyncMock(return_value=Assessment(actual="x")))

        runner = AgentRunner(cognitive, _Stub(), _Stub(), settings)
        runner._tool_loop = AsyncMock(return_value=(reply, [], {"input_tokens": 1, "output_tokens": 1}, []))
        runner.set_dispatcher(ToolDispatcher())
        prompts: list[str] = []
        real_pre_turn = cognitive.pre_turn

        async def spy_pre_turn(*args, **kwargs):
            ctx = await real_pre_turn(*args, **kwargs)
            prompts.append(ctx.system_prompt)
            return ctx

        monkeypatch.setattr(cognitive, "pre_turn", spy_pre_turn)
        try:
            await _turn(runner, "run_turn", _sid(), "plan it", channel=CH)
            await _turn(runner, "run_turn", _sid(), "yes do it", channel=CH)
        finally:
            await runner.close()
            await brain.close()

        assert "Previous Conversation" in prompts[1]
        fragments = {secret[i : i + 8] for i in range(len(secret) - 7)}
        for frag in fragments:
            assert frag not in prompts[1], frag
        critic_texts = [k["last_assistant_text"] for k in seen if "last_assistant_text" in k]
        assert critic_texts, "the critic should have received the previous message"
        for text in critic_texts:
            for frag in fragments:
                assert frag not in text, frag
    finally:
        monkeypatch.delenv("NOUS_TEST_F110_TOKEN", raising=False)
        _own_secrets.cache_clear()


@pytest.mark.parametrize(
    "kind", ["background", "scheduled", "subtask", "dag_node", "heartbeat_triage", "agent_action", "mcp"]
)
async def test_non_interactive_kinds_neither_write_nor_read_the_store(kind):
    """Only an interactive turn touches the channel's last reply: a channelled turn of another kind reads none
    and replaces none."""
    cognitive = _SpyCognitive()
    runner = _runner(cognitive, referent=True)
    first, other = _sid(), _sid()
    try:
        await _turn(runner, "run_turn", first, "plan it", channel=CH)  # stores REPLY
        runner._tool_loop = AsyncMock(
            return_value=("A different reply.", [], {"input_tokens": 1, "output_tokens": 1}, [])
        )
        await runner.run_turn(
            other, "background work", context=ExecutionContext(kind=kind, session_id=other, channel=CH)
        )
    finally:
        await runner.close()

    interactive_kwargs, other_kwargs = cognitive.pre_turn_kwargs
    assert _no_referent(interactive_kwargs)
    assert _no_referent(other_kwargs)  # no read
    assert runner._channel_last_reply.lookup(CH, _sid(), time.monotonic())[0] == REPLY  # no write
