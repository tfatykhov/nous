"""E-17: the pre-turn cap on untagged episodes (context_episode_untagged_diversity_cap).

Chat episodes carry no tags, so ``_enforce_diversity`` puts every one of them in the
"unknown" bucket and the episode cap (2) keeps at most two. The setting caps that bucket
alone: tagged buckets keep the cap of 2, and decisions and facts are unchanged. The
default (2) must reproduce today's Past Episodes section byte for byte.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from nous.cognitive.context import ContextEngine
from nous.cognitive.schemas import FrameSelection
from nous.config import Settings

STARTED = datetime.now(UTC) - timedelta(hours=1)


def _episode(i: int, score: float, tags: list[str]) -> SimpleNamespace:
    return SimpleNamespace(
        id=f"ep-{i}", title=f"episode {i}", summary=f"summary of episode {i}", outcome="success",
        started_at=STARTED, tags=tags, structured_summary=None, score=score,
    )


# Six untagged chat episodes and one tagged subtask result, scores close enough that the
# relevance filter's gap cut never fires.
EPISODES = [_episode(i, 0.9 - 0.02 * i, []) for i in range(6)] + [_episode(6, 0.79, ["subtask"])]


def _engine(**settings) -> ContextEngine:
    brain = AsyncMock()
    brain.embeddings = MagicMock()
    brain.query.return_value = []
    heart = AsyncMock()
    heart.search_facts.return_value = []
    heart.list_censors.return_value = []
    heart.list_facts_by_category.return_value = []
    heart.get_working_memory.return_value = None
    heart.search_episodes.return_value = list(EPISODES)
    heart.list_episodes.return_value = []
    heart.list_procedures.return_value = ([], 0)
    return ContextEngine(brain, heart, Settings(_env_file=None, **settings), identity_prompt="")


async def _past_episodes(engine: ContextEngine) -> str:
    frame = FrameSelection(
        frame_id="question", frame_name="Question", description="test", confidence=0.9, match_method="pattern",
    )
    result = await engine.build(agent_id="a", session_id="s", input_text="what did we decide last week?", frame=frame)
    return next(s for s in result.sections if s.label == "Past Episodes").content


def _lines(*ids: int) -> str:
    day = STARTED.strftime("%Y-%m-%d")
    return "\n".join(f"- [success] summary of episode {i} ({day})" for i in ids)


# Captured on origin/main 81e0aa2f before the setting existed.
GOLDEN_PAST_EPISODES = _lines(0, 1, 6)


async def test_default_settings_reproduce_the_past_episodes_golden():
    assert await _past_episodes(_engine()) == GOLDEN_PAST_EPISODES


@pytest.mark.parametrize(
    ("cap", "expected"),
    [
        (2, (0, 1, 6)),  # today: two untagged, plus the tagged one in its own bucket
        (4, (0, 1, 2, 3, 6)),
        (0, (0, 1, 2, 3, 4, 5)),  # no cap: the relevance filter's max_k (6) is the limit
    ],
)
async def test_untagged_cap_controls_how_many_chat_episodes_reach_context(cap, expected):
    engine = _engine(context_episode_untagged_diversity_cap=cap)
    assert await _past_episodes(engine) == _lines(*expected)


@pytest.mark.parametrize(("cap", "kept_untagged"), [(2, 2), (4, 4), (0, 6)])
def test_the_cap_applies_to_the_unknown_bucket_only(cap, kept_untagged):
    engine = _engine()
    tagged = [_episode(10 + i, 0.5, ["subtask"]) for i in range(3)]
    kept = engine._enforce_diversity(EPISODES[:6] + tagged, "tags", max_per_subject=2, unknown_cap=cap)
    assert sum(1 for e in kept if not e.tags) == kept_untagged
    assert sum(1 for e in kept if e.tags) == 2  # the tagged bucket keeps max_per_subject


def test_without_unknown_cap_every_bucket_uses_max_per_subject():
    engine = _engine()
    facts = [SimpleNamespace(subject=None) for _ in range(5)] + [SimpleNamespace(subject="x") for _ in range(5)]
    assert len(engine._enforce_diversity(facts, "subject", max_per_subject=2)) == 4


def test_the_setting_defaults_to_two_and_rejects_negatives():
    assert Settings(_env_file=None).context_episode_untagged_diversity_cap == 2
    with pytest.raises(ValidationError):
        Settings(_env_file=None, context_episode_untagged_diversity_cap=-1)
