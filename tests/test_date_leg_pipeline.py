"""F075 L3 Task 6: wiring test for the date-window retrieval leg pipeline integration.

Verifies two wiring assertions with zero DB dependency:

1. ``date_leg_enabled=False`` → ``heart.recall`` receives ``date_window=None``
   AND the parser's ``parse`` method is never called (byte-identical to today).

2. ``date_leg_enabled=True`` + a temporal query + a stub parser that returns a
   ``DateWindow`` → ``heart.recall`` receives that exact ``DateWindow`` as
   ``date_window``.

The test drives ``run_recall_pipeline`` directly with a lightweight stub heart,
brain, and settings (same pattern as ``tests/test_retrieval_pipeline.py``).
"""
from __future__ import annotations

import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from nous.api.retrieval_pipeline import run_recall_pipeline
from nous.heart.date_window import DateWindow
from nous.heart.schemas import RecallResult


# ---------------------------------------------------------------------------
# Minimal stubs (no DB, no network)
# ---------------------------------------------------------------------------


def _make_recall_results() -> list[RecallResult]:
    return [
        RecallResult(type="fact", id="11111111-1111-1111-1111-111111111111",
                     summary="a fact", score=0.9),
    ]


class _FakeSession:
    """Async context manager stand-in for brain.db.session()."""
    async def __aenter__(self):
        return self
    async def __aexit__(self, *_):
        return None
    async def execute(self, _stmt):
        class _R:
            def scalars(self):
                class _S:
                    def all(self):
                        return []
                return _S()
        return _R()


def _make_brain():
    brain = MagicMock()
    brain.agent_id = "nous-test-agent"
    brain.query = AsyncMock(return_value=[])
    brain.neighbors = AsyncMock(return_value=[])
    brain.db = MagicMock()
    brain.db.session = MagicMock(return_value=_FakeSession())
    return brain


def _make_heart(*, parser=None):
    heart = MagicMock()
    heart.agent_id = "nous-test-agent"
    heart.recall = AsyncMock(return_value=_make_recall_results())
    heart.date_window_parser = parser
    return heart


def _make_settings(*, date_leg_enabled: bool = False):
    """Minimal Settings SimpleNamespace with all flags the pipeline reads."""
    return SimpleNamespace(
        # F075 L3 flags under test
        date_leg_enabled=date_leg_enabled,
        # Graph / memory flags (all off to keep the test focused)
        graph_recall_enabled=False,
        cross_type_linking_enabled=False,
        spreading_activation_enabled="false",
        contradiction_detection=False,
        graph_recall_decay=0.7,
        graph_recall_max_expand=5,
        graph_recall_max_neighbors=3,
        heart_graph_all_types_enabled=False,
        heart_graph_neighbors_per_seed=3,
        # Other flags
        episode_chunks_enabled=False,
        episode_chunk_recall_limit=10,
        coherent_ranking_enabled=False,
        session_group_heart_section=False,
        graph_adjacency_boost_enabled=False,
        recall_exclude_context_ids=False,
    )


class _StubParser:
    """Stub DateWindowParser that tracks parse() calls and returns a fixed window."""

    def __init__(self, return_value):
        self._return_value = return_value
        self.call_count = 0

    async def parse(self, query: str, today: datetime.date):
        self.call_count += 1
        return self._return_value


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pipeline_flag_off_passes_no_window_and_skips_parser():
    """With date_leg_enabled=False the parser is never called and date_window=None."""
    stub_parser = _StubParser(return_value=DateWindow(
        start=datetime.date(2026, 4, 18),
        end=datetime.date(2026, 5, 2),
    ))
    heart = _make_heart(parser=stub_parser)
    brain = _make_brain()
    settings = _make_settings(date_leg_enabled=False)

    await run_recall_pipeline(
        query="what changed in late April 2026?",
        heart=heart,
        brain=brain,
        settings=settings,
        limit=10,
    )

    # Parser.parse must never have been called
    assert stub_parser.call_count == 0, "parser.parse was called when flag is off"

    # heart.recall must have been called with date_window=None
    assert heart.recall.called, "heart.recall was not called at all"
    call_kwargs = heart.recall.call_args_list[-1].kwargs
    assert call_kwargs.get("date_window") is None, (
        f"expected date_window=None, got {call_kwargs.get('date_window')!r}"
    )


@pytest.mark.asyncio
async def test_pipeline_flag_on_passes_window_from_parser():
    """With date_leg_enabled=True the parser runs and heart.recall gets the DateWindow."""
    expected_window = DateWindow(
        start=datetime.date(2026, 4, 18),
        end=datetime.date(2026, 5, 2),
    )
    stub_parser = _StubParser(return_value=expected_window)
    heart = _make_heart(parser=stub_parser)
    brain = _make_brain()
    settings = _make_settings(date_leg_enabled=True)

    await run_recall_pipeline(
        query="what happened in late April 2026?",
        heart=heart,
        brain=brain,
        settings=settings,
        limit=10,
    )

    # Parser.parse must have been called exactly once
    assert stub_parser.call_count == 1, (
        f"expected parser.parse to be called once, got {stub_parser.call_count}"
    )

    # heart.recall must have been called with the expected DateWindow
    assert heart.recall.called, "heart.recall was not called at all"
    call_kwargs = heart.recall.call_args_list[-1].kwargs
    assert call_kwargs.get("date_window") == expected_window, (
        f"expected date_window={expected_window!r}, got {call_kwargs.get('date_window')!r}"
    )


@pytest.mark.asyncio
async def test_pipeline_flag_on_no_parser_attribute_is_safe():
    """date_leg_enabled=True but heart has no date_window_parser → no crash, no window."""
    heart = _make_heart(parser=None)  # parser is None (wired but no-op)
    brain = _make_brain()
    settings = _make_settings(date_leg_enabled=True)

    # Must not raise
    await run_recall_pipeline(
        query="what happened in late April 2026?",
        heart=heart,
        brain=brain,
        settings=settings,
        limit=10,
    )

    call_kwargs = heart.recall.call_args_list[-1].kwargs
    assert call_kwargs.get("date_window") is None, (
        "expected date_window=None when parser is None"
    )


@pytest.mark.asyncio
async def test_pipeline_flag_on_parser_returns_none_passes_none():
    """When the parser fails open (returns None), heart.recall gets date_window=None."""
    stub_parser = _StubParser(return_value=None)  # fail-open scenario
    heart = _make_heart(parser=stub_parser)
    brain = _make_brain()
    settings = _make_settings(date_leg_enabled=True)

    await run_recall_pipeline(
        query="what happened in late April 2026?",
        heart=heart,
        brain=brain,
        settings=settings,
        limit=10,
    )

    assert stub_parser.call_count == 1
    call_kwargs = heart.recall.call_args_list[-1].kwargs
    assert call_kwargs.get("date_window") is None, (
        "when parser returns None, date_window must be None (fail-open)"
    )
