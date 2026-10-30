"""recall_deep(hours=N) took over recall_recent; recall_hubs is retired.

query only searches, hours only lists the latest conversations (no retrieval trace, nothing recorded as
surfaced), both lists first and searches second, neither is an error. The old names stay callable through the
dispatcher's alias table.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nous.api.retrieval_pipeline import PipelineResult, PipelineStats
from nous.api.tools import ToolDispatcher, create_nous_tools, register_nous_tools
from nous.heart.schemas import EpisodeSummary


def _settings() -> MagicMock:
    s = MagicMock()
    s.episode_chunks_enabled = False
    s.residual_activation_enabled = True  # on, so a skipped record_surfaced is the code's choice
    s.recall_include_parent_episodes = False
    s.session_group_heart_section = False
    s.decision_resolution_enabled = True
    return s


def _episode(title: str, summary: str = "s") -> EpisodeSummary:
    return EpisodeSummary(
        id=uuid.uuid4(),
        title=title,
        summary=summary,
        outcome="success",
        started_at=datetime(2026, 10, 29, 9, 30, tzinfo=UTC),
        tags=[],
        structured_summary={"title": title, "summary": summary},
    )


def _env(episodes=()):
    brain, heart = MagicMock(), MagicMock()
    brain.agent_id = "test"
    heart.list_episodes = AsyncMock(return_value=list(episodes))
    heart._residual_activator = MagicMock()
    heart._residual_activator.current_turn = AsyncMock(return_value=0)
    heart._residual_activator.compute_activations = AsyncMock(return_value={})
    heart._residual_activator.record_surfaced = AsyncMock()
    return brain, heart


def _pipeline():
    results = [PipelineResult(id=uuid.uuid4(), type="fact", description="the deploy uses blue/green", score=0.9)]
    return AsyncMock(return_value=(results, PipelineStats()))


def _text(result: dict) -> str:
    return result["content"][0]["text"]


@pytest.mark.asyncio
async def test_hours_only_lists_episodes_and_opens_no_trace():
    brain, heart = _env([_episode("Ski trip")])
    recall_deep = create_nous_tools(brain, heart, settings=_settings())["recall_deep"]
    logger = MagicMock()
    with (
        patch("nous.api.retrieval_pipeline.run_recall_pipeline", new=_pipeline()) as pipeline,
        patch("nous.api.tools.get_active_retrieval_logger", return_value=logger),
    ):
        result = await recall_deep(hours=24, limit=5, _session_id="s1", _turn_number=3)
    assert not result.get("is_error")
    assert _text(result).startswith("Recent episodes (last 24h):") and "Ski trip" in _text(result)
    heart.list_episodes.assert_awaited_once_with(limit=5, hours=24)
    pipeline.assert_not_called()
    logger.start.assert_not_called()  # no F091 trace
    heart._residual_activator.record_surfaced.assert_not_called()  # no F055 surfacing


@pytest.mark.asyncio
async def test_query_only_searches():
    brain, heart = _env()
    recall_deep = create_nous_tools(brain, heart, settings=_settings())["recall_deep"]
    with patch("nous.api.retrieval_pipeline.run_recall_pipeline", new=_pipeline()) as pipeline:
        result = await recall_deep(query="deploy", limit=4, memory_types=["fact"])
    assert "blue/green" in _text(result) and "Recent episodes" not in _text(result)
    heart.list_episodes.assert_not_called()
    kwargs = pipeline.call_args.kwargs
    assert (kwargs["query"], kwargs["limit"], kwargs["memory_types"]) == ("deploy", 4, ["fact"])


@pytest.mark.asyncio
async def test_both_list_first_then_search_with_the_limit_on_each():
    brain, heart = _env([_episode("Ski trip")])
    recall_deep = create_nous_tools(brain, heart, settings=_settings())["recall_deep"]
    with patch("nous.api.retrieval_pipeline.run_recall_pipeline", new=_pipeline()) as pipeline:
        result = await recall_deep(query="deploy", hours=48, limit=3, memory_types=["fact"])
    text = _text(result)
    assert text.index("Ski trip") < text.index("blue/green")
    heart.list_episodes.assert_awaited_once_with(limit=3, hours=48)
    assert pipeline.call_args.kwargs["limit"] == 3 and pipeline.call_args.kwargs["memory_types"] == ["fact"]


@pytest.mark.asyncio
@pytest.mark.parametrize("call", [{}, {"query": ""}, {"limit": 5}])
async def test_neither_is_an_error(call):
    brain, heart = _env()
    result = await create_nous_tools(brain, heart, settings=_settings())["recall_deep"](**call)
    assert result["is_error"] is True
    assert "query" in _text(result) and "hours" in _text(result)
    heart.list_episodes.assert_not_called()


@pytest.mark.asyncio
async def test_a_listing_that_names_a_changed_tool_ends_with_the_note():
    brain, heart = _env([_episode("Used recall_recent to catch up")])
    result = await create_nous_tools(brain, heart, settings=_settings())["recall_deep"](hours=48)
    assert _text(result).splitlines()[-1] == (
        "Note: some memories name tools that changed: recall_recent → recall_deep(hours=…)."
    )
    brain, heart = _env([_episode("Plain day")])
    assert "Note:" not in _text(await create_nous_tools(brain, heart, settings=_settings())["recall_deep"](hours=48))


def test_query_is_optional_and_hours_is_offered():
    d = ToolDispatcher()
    brain, heart = _env()
    register_nous_tools(d, brain, heart, settings=_settings())
    schema = next(t for t in d.tool_definitions() if t["name"] == "recall_deep")["input_schema"]
    assert schema["required"] == [] and schema["properties"]["hours"]["type"] == "integer"
    assert "default" not in schema["properties"]["hours"]  # absent hours means "do not list"
    assert d.validate_call("recall_deep", {"hours": 24}) == []


def test_the_old_names_are_aliases_not_tools():
    d = ToolDispatcher()
    brain, heart = _env()
    register_nous_tools(d, brain, heart, settings=_settings())
    names = {t["name"] for t in d.tool_definitions()}
    assert not {"recall_recent", "recall_hubs"} & names
    assert d.alias_for("recall_recent").target == "recall_deep"
    assert d.alias_for("recall_hubs").target is None
    assert d.validate_call("recall_recent", {"limit": 5}) == []


@pytest.mark.asyncio
async def test_a_recall_recent_call_dispatches_as_recall_deep_hours():
    """What the runner does with an alias, done by hand: translate, then dispatch the target."""
    d = ToolDispatcher()
    brain, heart = _env([_episode("Ski trip")])
    register_nous_tools(d, brain, heart, settings=_settings())
    alias = d.alias_for("recall_recent")
    text, is_error = await d.dispatch(alias.target, alias.translate({}), session_id="s1")
    assert not is_error and "Recent episodes (last 48h):" in text
    heart.list_episodes.assert_awaited_once_with(limit=10, hours=48)
