"""Outside-content tagging, PR 2 (shadow): which calls bring in outside content, the session's list
recorded at dispatch time, the list on the events, and the extractors' would-tag log.

Pure unit tests: mocked brain/heart, a capturing bus, a real ToolDispatcher with stand-in tools.
"""

from __future__ import annotations

import logging
import uuid
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from nous.api.runner import AgentRunner, ApiResponse, Conversation, Message
from nous.api.tools import ToolDispatcher
from nous.cognitive.execution_ledger import classify_ingress
from nous.cognitive.layer import CognitiveLayer
from nous.cognitive.schemas import SessionMetadata
from nous.config import Settings
from nous.events import Event
from nous.handlers.episode_summarizer import EpisodeSummarizer
from nous.handlers.fact_extractor import FactExtractor
from nous.handlers.knowledge_extractor import KnowledgeExtractor

# The keys session_ended carried before this PR: with the mode off they must be exactly these.
_SESSION_ENDED_KEYS = {"session_id", "episode_id", "transcript", "reflection", "had_reflection",
                       "facts_extracted", "summary"}


class _Bus:
    """Captures emitted events; accepts handler registration."""

    def __init__(self) -> None:
        self.events: list[Event] = []

    def on(self, event_type, handler) -> None:  # noqa: ANN001 -- the EventBus signature
        pass

    async def emit(self, event: Event) -> None:
        self.events.append(event)


def _layer(mode: str | None, bus: _Bus | None = None) -> CognitiveLayer:
    """A CognitiveLayer over mocks. ``mode`` None leaves the setting a bare MagicMock attribute."""
    brain = MagicMock()
    heart = MagicMock()
    heart.clear_working_memory = AsyncMock()
    settings = MagicMock()
    settings.session_timeout = 1800
    if mode is not None:
        settings.outside_content_tagging_mode = mode
    return CognitiveLayer(brain, heart, settings, bus=bus)


# --- classify_ingress -------------------------------------------------------------------------


@pytest.mark.parametrize("name, tool_input, expected", [
    ("mcp__x__y", {}, "outside"),
    ("mcp__github__get_issue", None, "outside"),
    ("no_such_tool", {}, "none"),
    ("web_fetch", {"url": "https://example.com"}, "outside"),
    ("recall_deep", {"query": "q"}, "none"),
    ("bash", {"command": "curl https://example.com"}, "outside"),
    ("bash", {"command": "wget -q https://example.com/a.tar.gz"}, "outside"),
    ("bash", {"command": "git clone https://github.com/x/y"}, "outside"),
    ("bash", {"command": "cd repo && git pull"}, "outside"),
    ("bash", {"command": "gh api repos/x/y/issues"}, "outside"),
    ("bash", {"command": "pip install requests"}, "outside"),
    ("bash", {"command": "sudo npm install left-pad"}, "outside"),
    ("bash", {"command": "ls -la"}, "none"),
    ("bash", {"command": "cat notes.txt | grep install"}, "none"),
    ("bash", {"command": "pip list"}, "none"),
    ("bash", {}, "none"),
    ("run_python", {"code": "import requests\nprint(requests.get('https://example.com').text)"}, "outside"),
    ("run_python", {"code": "from urllib.request import urlopen\nurlopen(u).read()"}, "outside"),
    ("run_python", {"code": "print(sum(range(10)) * 3)"}, "none"),
    ("run_python", {}, "none"),
])
def test_classify_ingress(name, tool_input, expected):
    assert classify_ingress(name, tool_input) == expected


# --- the session's list, recorded at dispatch -------------------------------------------------


def test_dispatch_records_outside_tools_in_order_once_each():
    layer = _layer("shadow")
    layer._session_metadata["s1"] = SessionMetadata()
    layer.note_tool_dispatch("s1", "recall_deep", {"query": "q"})
    layer.note_tool_dispatch("s1", "bash", {"command": "ls"})
    layer.note_tool_dispatch("s1", "web_fetch", {"url": "https://a"})
    layer.note_tool_dispatch("s1", "bash", {"command": "curl https://b"})
    layer.note_tool_dispatch("s1", "web_fetch", {"url": "https://c"})
    assert layer.outside_tools("s1") == ["web_fetch", "bash"]


@pytest.mark.parametrize("mode", ["off", None])
def test_mode_off_records_nothing(mode):
    layer = _layer(mode)
    layer._session_metadata["s1"] = SessionMetadata()
    layer.note_tool_dispatch("s1", "web_fetch", {"url": "https://a"})
    assert layer.outside_tools("s1") == []


def test_a_session_without_pre_turn_gets_no_metadata():
    """An approved action's ``proposal-*`` session never had a pre_turn: nothing is created for it."""
    layer = _layer("shadow")
    layer.note_tool_dispatch("proposal-1", "web_fetch", {"url": "https://a"})
    assert "proposal-1" not in layer._session_metadata
    assert layer.outside_tools("proposal-1") == []


# --- session_ended and conversation_compacting ------------------------------------------------


@pytest.mark.parametrize("mode", ["off", None])
async def test_session_ended_is_unchanged_with_the_mode_off(mode):
    bus = _Bus()
    layer = _layer(mode, bus)
    layer._session_metadata["s1"] = SessionMetadata()
    layer.note_tool_dispatch("s1", "web_fetch", {"url": "https://a"})
    await layer.end_session("agent", "s1")
    (event,) = [e for e in bus.events if e.type == "session_ended"]
    assert "outside_tools" not in event.data
    assert set(event.data) == _SESSION_ENDED_KEYS


async def test_session_ended_carries_the_list_in_shadow():
    bus = _Bus()
    layer = _layer("shadow", bus)
    layer._session_metadata["s1"] = SessionMetadata()
    layer.note_tool_dispatch("s1", "web_fetch", {"url": "https://a"})
    await layer.end_session("agent", "s1")
    (event,) = [e for e in bus.events if e.type == "session_ended"]
    assert event.data["outside_tools"] == ["web_fetch"]
    assert set(event.data) == _SESSION_ENDED_KEYS | {"outside_tools"}


async def test_session_ended_carries_an_empty_list_for_a_clean_session():
    bus = _Bus()
    layer = _layer("shadow", bus)
    layer._session_metadata["s1"] = SessionMetadata()
    layer.note_tool_dispatch("s1", "recall_deep", {"query": "q"})
    await layer.end_session("agent", "s1")
    (event,) = [e for e in bus.events if e.type == "session_ended"]
    assert event.data["outside_tools"] == []


async def test_compaction_event_unions_the_session_list_and_the_snapshot():
    bus = _Bus()
    layer = _layer("shadow", bus)
    layer._session_metadata["s1"] = SessionMetadata()
    layer.note_tool_dispatch("s1", "web_search", {"query": "q"})
    snapshot = [
        {"role": "user", "content": "find it"},
        {"role": "assistant", "content": [
            {"type": "text", "text": "looking"},
            {"type": "tool_use", "id": "t1", "name": "web_search", "input": {"query": "q"}},
            {"type": "tool_use", "id": "t2", "name": "bash", "input": {"command": "ls"}},
            {"type": "tool_use", "id": "t3", "name": "mcp__docs__read", "input": {}},
        ]},
    ]
    await layer.pre_compaction("agent", "s1", snapshot)
    (event,) = bus.events
    assert event.type == "conversation_compacting"
    assert event.data["outside_tools"] == ["web_search", "mcp__docs__read"]
    assert event.data["message_snapshot"] is snapshot


@pytest.mark.parametrize("mode", ["off", None])
async def test_compaction_event_is_unchanged_with_the_mode_off(mode):
    bus = _Bus()
    layer = _layer(mode, bus)
    snapshot = [{"role": "assistant", "content": [
        {"type": "tool_use", "id": "t1", "name": "web_fetch", "input": {"url": "https://a"}}]}]
    await layer.pre_compaction("agent", "s1", snapshot)
    (event,) = bus.events
    assert event.data == {"message_snapshot": snapshot}


@pytest.mark.parametrize("tools", [["web_fetch"], [], None])
async def test_episode_summarized_forwards_the_list_only_when_session_ended_had_it(tools):
    bus = _Bus()
    summarizer = EpisodeSummarizer(MagicMock(), None, MagicMock(), bus)
    summarizer.summarize_episode = AsyncMock(return_value={"title": "t", "candidate_facts": []})
    data = {"session_id": "s1", "episode_id": str(uuid4()), "transcript": "x" * 80}
    if tools is not None:
        data["outside_tools"] = tools
    await summarizer.handle(Event(type="session_ended", agent_id="agent", session_id="s1", data=data))
    (event,) = [e for e in bus.events if e.type == "episode_summarized"]
    if tools is None:
        assert set(event.data) == {"episode_id", "summary", "candidate_facts", "transcript"}
    else:
        assert event.data["outside_tools"] == tools


# --- through the runner: visible to a write later in the same turn ----------------------------


def _api(tool: str, tool_input: dict) -> ApiResponse:
    return ApiResponse(
        content=[{"type": "tool_use", "id": f"toolu_{uuid.uuid4().hex[:12]}", "name": tool, "input": tool_input}],
        stop_reason="tool_use",
    )


def _runner(layer, mode: str) -> tuple[AgentRunner, list[list[str]]]:
    """A runner over ``layer`` with stand-in web_fetch and learn_fact tools. The learn_fact stand-in
    records what the layer reports for the session at the moment it runs."""
    seen_by_learn_fact: list[list[str]] = []
    dispatcher = ToolDispatcher()

    async def web_fetch(url: str = "") -> dict:
        return {"content": [{"type": "text", "text": f"page at {url}"}]}

    async def learn_fact(content: str = "", category: str = "technical") -> dict:
        seen_by_learn_fact.append(layer.outside_tools("s1"))
        return {"content": [{"type": "text", "text": "stored"}]}

    dispatcher.register("web_fetch", web_fetch, {
        "type": "object", "description": "fetch", "properties": {"url": {"type": "string"}}, "required": ["url"]})
    dispatcher.register("learn_fact", learn_fact, {
        "type": "object", "description": "learn", "properties": {"content": {"type": "string"}},
        "required": ["content"]})
    settings = Settings(ANTHROPIC_API_KEY="test-key", agent_id="test-agent", max_turns=5, max_tokens=1024,
                        outside_content_tagging_mode=mode)
    runner = AgentRunner(layer, MagicMock(), MagicMock(), settings)
    runner.set_dispatcher(dispatcher)
    return runner, seen_by_learn_fact


async def test_a_learn_fact_later_in_the_same_turn_sees_the_fetch():
    layer = _layer("shadow")
    layer._session_metadata["s1"] = SessionMetadata()  # what pre_turn does
    runner, seen = _runner(layer, "shadow")
    runner._call_api = AsyncMock(side_effect=[
        _api("learn_fact", {"content": "before"}),
        _api("web_fetch", {"url": "https://example.com"}),
        _api("learn_fact", {"content": "after"}),
        ApiResponse(content=[{"type": "text", "text": "done"}], stop_reason="end_turn"),
    ])
    conv = Conversation(session_id="s1")
    conv.messages.append(Message(role="user", content="read it and remember"))
    try:
        await runner._tool_loop(system_prompt="t", conversation=conv, frame_id="task", session_id="s1")
    finally:
        await runner.close()
    assert seen == [[], ["web_fetch"]]  # the second learn_fact ran before the turn ended


async def test_the_runner_does_not_ask_the_layer_with_the_mode_off():
    """A layer stand-in without note_tool_dispatch: with the mode off it is never called."""
    layer = MagicMock(spec=["get_active_episode_id", "outside_tools"])
    layer.get_active_episode_id.return_value = None
    layer.outside_tools.return_value = []
    runner, seen = _runner(layer, "off")
    runner._call_api = AsyncMock(side_effect=[
        _api("web_fetch", {"url": "https://example.com"}),
        _api("learn_fact", {"content": "after"}),
        ApiResponse(content=[{"type": "text", "text": "done"}], stop_reason="end_turn"),
    ])
    conv = Conversation(session_id="s1")
    conv.messages.append(Message(role="user", content="go"))
    try:
        _text, results, _usage, _ = await runner._tool_loop(
            system_prompt="t", conversation=conv, frame_id="task", session_id="s1")
    finally:
        await runner.close()
    assert [r.tool_name for r in results] == ["web_fetch", "learn_fact"]
    assert all(r.error is None for r in results)


async def test_the_streaming_dispatch_records_too():
    layer = _layer("shadow")
    layer._session_metadata["s1"] = SessionMetadata()
    runner, _ = _runner(layer, "shadow")
    try:
        items = [i async for i in runner._dispatch_with_keepalive(
            "web_fetch", {"url": "https://example.com"}, session_id="s1")]
    finally:
        await runner.close()
    assert items[-1].is_error is False
    assert layer.outside_tools("s1") == ["web_fetch"]


# --- the extractors' would-tag log -------------------------------------------------------------


def _extractor_settings(mode: str | None) -> MagicMock:
    s = MagicMock()
    if mode is not None:
        s.outside_content_tagging_mode = mode
    s.extraction_input_hardening_enabled = False
    s.knowledge_extractor_max_chars = 24000
    s.background_model = "m"
    return s


def _summarized(outside_tools: list[str] | None) -> Event:
    data = {"episode_id": str(uuid4()), "summary": {"title": "t"}, "candidate_facts": [], "transcript": "x"}
    if outside_tools is not None:
        data["outside_tools"] = outside_tools
    return Event(type="episode_summarized", agent_id="agent", session_id="s1", data=data)


@pytest.mark.parametrize("mode, tools, logged", [
    ("shadow", ["web_fetch"], True),
    ("on", ["web_fetch"], True),
    ("shadow", [], False),
    ("shadow", None, False),
    ("off", ["web_fetch"], False),
    (None, ["web_fetch"], False),
])
async def test_fact_extractor_shadow_line(caplog, mode, tools, logged):
    bus = _Bus()
    extractor = FactExtractor(MagicMock(), _extractor_settings(mode), bus)
    extractor.extract_and_store = AsyncMock(return_value=[uuid4(), uuid4()])
    with caplog.at_level(logging.INFO, logger="nous.handlers"):
        await extractor.handle(_summarized(tools))
    lines = [r.getMessage() for r in caplog.records if "outside-content shadow" in r.getMessage()]
    shadow_events = [e for e in bus.events if e.type == "outside_content_shadow"]
    if logged:
        assert len(lines) == 1
        assert "2 facts would be tagged" in lines[0] and "fact_extractor" in lines[0]
        assert "session s1" in lines[0] and "web_fetch" in lines[0]
        (ev,) = shadow_events
        assert ev.data["facts"] == 2 and ev.data["outside_tools"] == ["web_fetch"]
        assert ev.data["extractor"] == "fact_extractor"
    else:
        assert lines == [] and shadow_events == []


def _compacting(outside_tools: list[str] | None) -> Event:
    snapshot = [{"role": "user", "content": "I work on the Nous project, which keeps its memory in Postgres " * 3},
                {"role": "assistant", "content": "Noted: the memory store is Postgres with pgvector."}]
    data: dict = {"message_snapshot": snapshot}
    if outside_tools is not None:
        data["outside_tools"] = outside_tools
    return Event(type="conversation_compacting", agent_id="agent", session_id="s1", data=data)


@pytest.mark.parametrize("mode, tools, logged", [
    ("shadow", ["web_search"], True),
    ("shadow", [], False),
    ("shadow", None, False),
    ("off", ["web_search"], False),
    (None, ["web_search"], False),
])
async def test_knowledge_extractor_shadow_line(caplog, mode, tools, logged):
    bus = _Bus()
    heart = MagicMock()
    heart.find_similar_facts = AsyncMock(return_value=[])
    heart.learn = AsyncMock(return_value=MagicMock())
    extractor = KnowledgeExtractor(heart, _extractor_settings(mode), bus)
    extractor._extract_facts = AsyncMock(return_value=[
        {"subject": "Nous", "content": "Nous keeps its memory in Postgres.", "category": "technical",
         "confidence": 0.9},
    ])
    with caplog.at_level(logging.INFO, logger="nous.handlers"):
        await extractor.handle(_compacting(tools))
    heart.learn.assert_awaited_once()  # storage itself is unchanged in every mode
    lines = [r.getMessage() for r in caplog.records if "outside-content shadow" in r.getMessage()]
    shadow_events = [e for e in bus.events if e.type == "outside_content_shadow"]
    if logged:
        assert len(lines) == 1
        assert "1 facts would be tagged" in lines[0] and "knowledge_extractor" in lines[0]
        (ev,) = shadow_events
        assert ev.data["facts"] == 1 and ev.data["outside_tools"] == ["web_search"]
    else:
        assert lines == [] and shadow_events == []
