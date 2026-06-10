"""recall_recent must prefer the summarizer's structured summary (parity
with recall_deep's COALESCE(structured_summary->>'summary', summary) — the
codex fix that never reached this tool) and must mark un-summarized
episodes' raw creation echo as such."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from nous.api.tools import create_nous_tools
from nous.heart.schemas import EpisodeSummary


def _episode(summary: str, title: str | None, structured: dict | None) -> EpisodeSummary:
    return EpisodeSummary(
        id=uuid4(),
        title=title,
        summary=summary,
        outcome="success",
        started_at=datetime(2026, 6, 10, 12, 0, tzinfo=UTC),
        tags=[],
        structured_summary=structured,
    )


def _make_tool(episodes: list[EpisodeSummary]):
    heart = MagicMock()
    heart.list_episodes = AsyncMock(return_value=episodes)
    brain = MagicMock()
    tools = create_nous_tools(brain, heart)
    return tools["recall_recent"]


def _text(resp: dict) -> str:
    return resp["content"][0]["text"]


def test_summarized_episode_prefers_structured_summary():
    ep = _episode(
        summary="hey can you check the deploy?",  # raw creation echo
        title=None,
        structured={"title": "Deploy verification", "summary": "Verified the prod deploy and confirmed all services healthy."},
    )
    text = _text(asyncio.run(_make_tool([ep])(hours=48)))
    assert "Deploy verification" in text
    assert "Verified the prod deploy" in text
    assert "hey can you check the deploy?" not in text
    assert "(unsummarized)" not in text


def test_unsummarized_episode_marks_raw_echo():
    ep = _episode(
        summary="hey can you check the deploy?",
        title=None,
        structured=None,
    )
    text = _text(asyncio.run(_make_tool([ep])(hours=48)))
    # Legacy behavior preserved (echo still shown) but flagged so it can't
    # masquerade as a produced summary — on the TITLE line, since a short
    # echo becomes the title itself.
    assert "hey can you check the deploy?" in text
    assert "(unsummarized)" in text
