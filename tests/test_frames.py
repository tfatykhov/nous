"""Tests for FrameEngine — cognitive frame selection via pattern matching.

Uses unittest.mock throughout — no real database required, except the
migration 105 test (Postgres only).

Seed frame patterns (matching seed.sql defaults):
  task:         build, fix, create, implement, deploy, run, execute, check, show, list,
                install, update, delete, move, copy, write, draft, spec, send, deliver,
                add, edit
  question:     what, how, why, explain, tell me (multi-word)
  decision:     should, choose, decide, compare, trade-off
  creative:     imagine, brainstorm, what if (multi-word), design, explore
  conversation: hello, hi, thanks, how are you (multi-word)
  debug:        error, bug, broken, failing, crash, wrong
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError
from sqlalchemy import text

from nous.cognitive.frames import FRAME_PRIORITY, FrameEngine, _is_affirmative_led, _offers_action
from nous.cognitive.schemas import FrameSelection
from nous.config import Settings
from nous.storage.migrator import _split_sql_statements
from tests.fixtures.frame_referent_pairs import FRAME_REFERENT_PAIRS, WRITE_IT_UP


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class FakeFrame:
    """Lightweight stand-in for nous.storage.models.Frame."""

    def __init__(
        self,
        id_: str,
        name: str,
        patterns: list[str],
        category: str = "process",
        stakes: str = "low",
        description: str | None = None,
        usage_count: int = 0,
    ) -> None:
        self.id = id_
        self.agent_id = "nous-default"
        self.name = name
        self.description = description or f"{name} frame"
        self.activation_patterns = patterns
        self.default_category = category
        self.default_stakes = stakes
        self.questions_to_ask = []
        self.usage_count = usage_count
        self.last_used = None
        self.active = True


# Seed frames that mirror the production seed.sql defaults
SEED_FRAMES = [
    FakeFrame("task", "Task Execution", ["build", "fix", "create", "implement", "deploy", "run", "execute", "check", "show", "list", "install", "update", "delete", "move", "copy", "write", "draft", "spec", "send", "deliver", "add", "edit"], "tooling", "medium"),
    FakeFrame("question", "Question Answering", ["what", "how", "why", "explain", "tell me"], "process", "low"),
    FakeFrame("decision", "Decision Making", ["should", "choose", "decide", "compare", "trade-off"], "architecture", "high"),
    FakeFrame("creative", "Creative", ["imagine", "brainstorm", "what if", "design", "explore"], "architecture", "low"),
    FakeFrame("conversation", "Conversation", ["hello", "hi", "thanks", "how are you"], "process", "low"),
    FakeFrame("debug", "Debug", ["error", "bug", "broken", "failing", "crash", "wrong"], "tooling", "medium"),
]


def make_engine(*, context: bool = False) -> FrameEngine:
    """FrameEngine wired to a stub Database (session never used directly).

    The settings carry real F110 frame-context values (the flag off unless ``context``), so the
    referent rule never reads a truthy MagicMock attribute.
    """
    db = MagicMock()
    settings = MagicMock()
    settings.frame_context_enabled = context
    settings.frame_short_reply_max_words = 6
    settings.frame_switch_min_hits = 2
    return FrameEngine(db, settings)


def make_session() -> AsyncMock:
    """AsyncSession mock; flush() is a no-op coroutine."""
    session = AsyncMock()
    session.flush = AsyncMock()
    return session


async def select_with_frames(engine: FrameEngine, text: str, frames: list[FakeFrame], **kwargs: Any) -> FrameSelection:
    """Run engine.select() with a controlled frame list via _load_frames patch; kwargs go to select()."""
    session = make_session()
    with patch.object(engine, "_load_frames", AsyncMock(return_value=frames)):
        with patch.object(engine, "_increment_usage", AsyncMock()):
            return await engine.select("nous-default", text, session=session, **kwargs)


# ---------------------------------------------------------------------------
# 1. test_frame_select_decision
# ---------------------------------------------------------------------------


async def test_frame_select_decision():
    """'should' keyword selects the decision frame."""
    engine = make_engine()
    result = await select_with_frames(engine, "should we use Redis?", SEED_FRAMES)
    assert isinstance(result, FrameSelection)
    assert result.frame_id == "decision"
    assert result.match_method == "pattern"
    assert result.confidence > 0


# ---------------------------------------------------------------------------
# 2. test_frame_select_task
# ---------------------------------------------------------------------------


async def test_frame_select_task():
    """'build' keyword selects the task frame."""
    engine = make_engine()
    result = await select_with_frames(engine, "build a REST API", SEED_FRAMES)
    assert result.frame_id == "task"
    assert result.match_method == "pattern"


# ---------------------------------------------------------------------------
# 3. test_frame_select_debug
# ---------------------------------------------------------------------------


async def test_frame_select_debug():
    """'error' keyword selects the debug frame."""
    engine = make_engine()
    result = await select_with_frames(engine, "error in deployment", SEED_FRAMES)
    assert result.frame_id == "debug"
    assert result.match_method == "pattern"


# ---------------------------------------------------------------------------
# 4. test_frame_select_conversation — multi-word pattern (P2-1)
# ---------------------------------------------------------------------------


async def test_frame_select_conversation():
    """'hello how are you' matches conversation with 2 patterns.

    P2-1: multi-word 'how are you' uses substring match; single-word 'hello'
    uses set membership. Both hit conversation → count=2, beats question (count=1).
    """
    engine = make_engine()
    result = await select_with_frames(engine, "hello how are you", SEED_FRAMES)
    assert result.frame_id == "conversation"
    assert result.match_method == "pattern"


# ---------------------------------------------------------------------------
# 5. test_frame_select_no_match
# ---------------------------------------------------------------------------


async def test_frame_select_no_match():
    """No matching keywords falls back to the DB conversation frame."""
    engine = make_engine()
    result = await select_with_frames(engine, "xyzzy foobar", SEED_FRAMES)
    assert result.frame_id == "conversation"
    assert result.match_method == "default"


# ---------------------------------------------------------------------------
# 6. test_frame_select_tiebreak
# ---------------------------------------------------------------------------


async def test_frame_select_tiebreak():
    """'should we fix this bug' ties at 1 match each for decision and debug.

    Decision (priority=6) beats debug (priority=5) via tiebreak.
    """
    engine = make_engine()
    result = await select_with_frames(engine, "should we fix this bug", SEED_FRAMES)
    assert result.frame_id == "decision"


# ---------------------------------------------------------------------------
# 7. test_frame_list
# ---------------------------------------------------------------------------


async def test_frame_list():
    """list_frames returns a FrameSelection for every active frame."""
    engine = make_engine()
    session = make_session()
    with patch.object(engine, "_load_frames", AsyncMock(return_value=SEED_FRAMES)):
        frames = await engine.list_frames("nous-default", session=session)
    assert len(frames) == 6
    frame_ids = {f.frame_id for f in frames}
    assert frame_ids == {"task", "question", "decision", "creative", "conversation", "debug"}


# ---------------------------------------------------------------------------
# 8. test_frame_get
# ---------------------------------------------------------------------------


async def test_frame_get():
    """get() fetches a specific frame from the DB and returns a FrameSelection."""
    engine = make_engine()
    session = make_session()

    fake_task = SEED_FRAMES[0]  # task frame
    mock_result = MagicMock()
    mock_result.scalars.return_value.first.return_value = fake_task
    session.execute.return_value = mock_result

    result = await engine.get("task", "nous-default", session=session)
    assert isinstance(result, FrameSelection)
    assert result.frame_id == "task"
    assert result.frame_name == "Task Execution"
    assert result.default_category == "tooling"


# ---------------------------------------------------------------------------
# 9. test_frame_usage_count_increments
# ---------------------------------------------------------------------------


async def test_frame_usage_count_increments():
    """Selecting a frame calls _increment_usage exactly once."""
    engine = make_engine()
    session = make_session()
    increment_mock = AsyncMock()
    with patch.object(engine, "_load_frames", AsyncMock(return_value=SEED_FRAMES)):
        with patch.object(engine, "_increment_usage", increment_mock):
            await engine.select("nous-default", "build something", session=session)
    increment_mock.assert_called_once()


# ---------------------------------------------------------------------------
# 10. test_frame_select_question
# ---------------------------------------------------------------------------


async def test_frame_select_question():
    """'what' keyword selects the question frame."""
    engine = make_engine()
    result = await select_with_frames(engine, "what is the capital of France", SEED_FRAMES)
    assert result.frame_id == "question"
    assert result.match_method == "pattern"


# ---------------------------------------------------------------------------
# 11. test_frame_select_creative
# ---------------------------------------------------------------------------


async def test_frame_select_creative():
    """'brainstorm' keyword selects the creative frame."""
    engine = make_engine()
    result = await select_with_frames(engine, "brainstorm ideas for a product", SEED_FRAMES)
    assert result.frame_id == "creative"
    assert result.match_method == "pattern"


# ---------------------------------------------------------------------------
# 12. test_case_insensitivity
# ---------------------------------------------------------------------------


async def test_case_insensitivity():
    """Pattern matching is case-insensitive — uppercase input still matches."""
    engine = make_engine()
    result = await select_with_frames(engine, "BUILD a REST API", SEED_FRAMES)
    assert result.frame_id == "task"
    assert result.match_method == "pattern"


# ---------------------------------------------------------------------------
# 13. test_confidence_single_match
# ---------------------------------------------------------------------------


async def test_confidence_single_match():
    """Single pattern match yields confidence = 0.3."""
    engine = make_engine()
    result = await select_with_frames(engine, "build this", SEED_FRAMES)
    assert result.confidence == pytest.approx(0.3)


# ---------------------------------------------------------------------------
# 14. test_confidence_two_matches
# ---------------------------------------------------------------------------


async def test_confidence_two_matches():
    """Two pattern matches within one frame yield confidence = 0.6."""
    engine = make_engine()
    # 'build' and 'fix' both hit task
    result = await select_with_frames(engine, "build and fix the server", SEED_FRAMES)
    assert result.frame_id == "task"
    assert result.confidence == pytest.approx(0.6)


# ---------------------------------------------------------------------------
# 15. test_confidence_capped_at_one
# ---------------------------------------------------------------------------


async def test_confidence_capped_at_one():
    """Confidence caps at 1.0 regardless of how many patterns match.

    'build create implement deploy' → 4 task patterns → 4 × 0.3 = 1.2, capped to 1.0.
    """
    engine = make_engine()
    result = await select_with_frames(engine, "build create implement deploy", SEED_FRAMES)
    assert result.frame_id == "task"
    assert result.confidence == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# 16. test_frame_get_not_found
# ---------------------------------------------------------------------------


async def test_frame_get_not_found():
    """get() raises ValueError when the frame ID is not in the database."""
    engine = make_engine()
    session = make_session()

    mock_result = MagicMock()
    mock_result.scalars.return_value.first.return_value = None
    session.execute.return_value = mock_result

    with pytest.raises(ValueError, match="Frame 'nonexistent' not found"):
        await engine.get("nonexistent", "nous-default", session=session)


# ---------------------------------------------------------------------------
# 17. test_frame_select_empty_input
# ---------------------------------------------------------------------------


async def test_frame_select_empty_input():
    """Empty string has no token matches — falls back to default conversation."""
    engine = make_engine()
    result = await select_with_frames(engine, "", SEED_FRAMES)
    assert result.frame_id == "conversation"
    assert result.match_method == "default"


# ---------------------------------------------------------------------------
# 18. test_frame_select_no_frames_in_db
# ---------------------------------------------------------------------------


async def test_frame_select_no_frames_in_db():
    """When no frames exist for the agent, returns hardcoded default FrameSelection."""
    engine = make_engine()
    result = await select_with_frames(engine, "build something", frames=[])
    assert result.frame_id == "conversation"
    assert result.match_method == "default"
    # Hardcoded default has a known description
    assert result.description is not None


# ---------------------------------------------------------------------------
# 19. test_frame_list_no_frames
# ---------------------------------------------------------------------------


async def test_frame_list_no_frames():
    """list_frames returns empty list when no frames are loaded."""
    engine = make_engine()
    session = make_session()
    with patch.object(engine, "_load_frames", AsyncMock(return_value=[])):
        frames = await engine.list_frames("nous-default", session=session)
    assert frames == []


# ---------------------------------------------------------------------------
# 20. test_frame_list_match_method_is_list
# ---------------------------------------------------------------------------


async def test_frame_list_match_method_is_list():
    """All FrameSelection objects from list_frames use match_method='list' and confidence=0."""
    engine = make_engine()
    session = make_session()
    with patch.object(engine, "_load_frames", AsyncMock(return_value=SEED_FRAMES)):
        frames = await engine.list_frames("nous-default", session=session)
    assert len(frames) == 6
    for f in frames:
        assert isinstance(f, FrameSelection)
        assert f.match_method == "list"
        assert f.confidence == 0.0


# ---------------------------------------------------------------------------
# 21. test_default_fallback_uses_conversation_from_frames
# ---------------------------------------------------------------------------


async def test_default_fallback_uses_conversation_from_frames():
    """No-match with frames present uses the conversation frame from the DB list.

    The returned frame_name comes from the loaded frame (not the hardcoded
    _default_selection), confirming the correct code path was taken.
    """
    engine = make_engine()
    increment_mock = AsyncMock()
    result = None
    with patch.object(engine, "_load_frames", AsyncMock(return_value=SEED_FRAMES)):
        with patch.object(engine, "_increment_usage", increment_mock):
            session = make_session()
            result = await engine.select("nous-default", "xyzzy plugh quux", session=session)

    assert result.frame_id == "conversation"
    assert result.match_method == "default"
    # _increment_usage is called even for the default fallback path
    increment_mock.assert_called_once()


# ---------------------------------------------------------------------------
# 22. test_frame_priority_ordering
# ---------------------------------------------------------------------------


def test_frame_priority_ordering():
    """FRAME_PRIORITY values strictly increase: conversation < creative < question < task < debug < decision."""
    assert FRAME_PRIORITY["decision"] > FRAME_PRIORITY["debug"]
    assert FRAME_PRIORITY["debug"] > FRAME_PRIORITY["task"]
    assert FRAME_PRIORITY["task"] > FRAME_PRIORITY["question"]
    assert FRAME_PRIORITY["question"] > FRAME_PRIORITY["creative"]
    assert FRAME_PRIORITY["creative"] > FRAME_PRIORITY["conversation"]


# ---------------------------------------------------------------------------
# 23. test_frame_priority_all_frames_covered
# ---------------------------------------------------------------------------


def test_frame_priority_all_frames_covered():
    """FRAME_PRIORITY covers exactly the 6 known frame types."""
    expected = {"conversation", "creative", "question", "task", "debug", "decision"}
    assert set(FRAME_PRIORITY.keys()) == expected


# ---------------------------------------------------------------------------
# 24. test_frame_engine_default_frame_id
# ---------------------------------------------------------------------------


def test_frame_engine_default_frame_id():
    """FrameEngine._default_frame_id is 'conversation'."""
    engine = make_engine()
    assert engine._default_frame_id == "conversation"


# ---------------------------------------------------------------------------
# 25. test_frame_get_match_method_direct
# ---------------------------------------------------------------------------


async def test_frame_get_match_method_direct():
    """get() returns match_method='direct' and confidence=1.0."""
    engine = make_engine()
    session = make_session()

    fake_decision = SEED_FRAMES[2]  # decision frame
    mock_result = MagicMock()
    mock_result.scalars.return_value.first.return_value = fake_decision
    session.execute.return_value = mock_result

    result = await engine.get("decision", "nous-default", session=session)
    assert result.match_method == "direct"
    assert result.confidence == pytest.approx(1.0)
    assert result.frame_id == "decision"


# ---------------------------------------------------------------------------
# 26. test_multiword_pattern_substring_match
# ---------------------------------------------------------------------------


async def test_multiword_pattern_substring_match():
    """Multi-word patterns use substring match on the full lowercased input.

    'tell me' (a question pattern with a space) must appear verbatim as a
    substring — it cannot be detected by set membership on tokens alone.
    """
    engine = make_engine()
    result = await select_with_frames(engine, "tell me everything about it", SEED_FRAMES)
    assert result.frame_id == "question"


# ---------------------------------------------------------------------------
# 27. test_multiword_pattern_order_matters
# ---------------------------------------------------------------------------


async def test_multiword_pattern_order_matters():
    """Reversed word order defeats a multi-word pattern.

    'me tell' is not the same substring as 'tell me' so the pattern
    does not match, and neither does any other pattern here.
    """
    engine = make_engine()
    result = await select_with_frames(engine, "me tell everything", SEED_FRAMES)
    assert result.frame_id == "conversation"
    assert result.match_method == "default"


# ---------------------------------------------------------------------------
# 28. test_single_word_partial_no_match
# ---------------------------------------------------------------------------


async def test_single_word_partial_no_match():
    """Single-word patterns use set membership, not substring.

    'builds' ≠ 'build' — the task frame must NOT activate.
    """
    engine = make_engine()
    # Only word is 'buildings' — none of the task patterns are in the token set
    result = await select_with_frames(engine, "buildings everywhere", SEED_FRAMES)
    # No single-word or multi-word pattern matches 'buildings'
    assert result.match_method == "default"


# ---------------------------------------------------------------------------
# 29. test_frame_select_session_context_manager
# ---------------------------------------------------------------------------


async def test_frame_select_session_context_manager():
    """When no session is passed, FrameEngine opens one via db.session()."""
    engine = make_engine()

    # Set up db.session() as an async context manager that yields a session
    mock_session = make_session()
    mock_session_ctx = AsyncMock()
    mock_session_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session_ctx.__aexit__ = AsyncMock(return_value=False)
    engine._db.session.return_value = mock_session_ctx

    with patch.object(engine, "_load_frames", AsyncMock(return_value=SEED_FRAMES)):
        with patch.object(engine, "_increment_usage", AsyncMock()):
            result = await engine.select("nous-default", "build something")

    assert result.frame_id == "task"
    engine._db.session.assert_called_once()


# ---------------------------------------------------------------------------
# 30. F110 task keywords (migration 105)
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parents[1]
MIGRATION_105 = ROOT / "sql" / "migrations" / "105_task_frame_keywords.sql"
NEW_TASK_WORDS = ["write", "draft", "spec", "send", "deliver", "add", "edit"]
PRE_105_TASK_WORDS = [
    "build", "fix", "create", "implement", "deploy", "run", "execute", "check", "show", "list",
    "install", "update", "delete", "move", "copy",
]  # fmt: skip


async def test_task_keywords_spec_request():
    """The live request that went to conversation now selects task: write, spec and deliver are 3 hits."""
    engine = make_engine()
    result = await select_with_frames(engine, "Yes write short spec., deliver here", SEED_FRAMES)
    assert result.frame_id == "task"
    assert result.match_method == "pattern"
    assert result.confidence == pytest.approx(0.9)


async def test_task_keywords_haiku_accepted():
    """'write' alone selects task: the accepted cost of a common verb."""
    engine = make_engine()
    result = await select_with_frames(engine, "write me a haiku", SEED_FRAMES)
    assert result.frame_id == "task"
    assert result.match_method == "pattern"


def test_seed_task_patterns_contain_new_keywords():
    seed = (ROOT / "sql" / "seed.sql").read_text(encoding="utf-8")
    (row,) = [ln for ln in seed.splitlines() if ln.lstrip().startswith("('task',")]
    patterns = re.findall(r"'([^']*)'", re.search(r"ARRAY\[(.*?)\]", row).group(1))
    assert patterns == PRE_105_TASK_WORDS + NEW_TASK_WORDS
    assert next(f for f in SEED_FRAMES if f.id == "task").activation_patterns == patterns


@pytest.mark.postgres_only
async def test_migration_105_idempotent_and_adds_keywords(session):
    sql = MIGRATION_105.read_text(encoding="utf-8")
    assert all(";" not in ln for ln in sql.splitlines() if ln.lstrip().startswith("--"))
    (statement,) = _split_sql_statements(sql)
    seed_row = text(
        "INSERT INTO nous_system.frames (id, name, activation_patterns) VALUES ('task', 'Task Execution', "
        "CAST(:p AS text[])) ON CONFLICT (id) DO UPDATE SET activation_patterns = EXCLUDED.activation_patterns"
    )
    patterns = text("SELECT activation_patterns FROM nous_system.frames WHERE id = 'task'")

    await session.execute(seed_row, {"p": PRE_105_TASK_WORDS})
    assert [(await session.execute(text(statement))).rowcount for _ in range(2)] == [1, 0]
    after = (await session.execute(patterns)).scalar_one()
    assert after == PRE_105_TASK_WORDS + NEW_TASK_WORDS
    assert all(after.count(word) == 1 for word in NEW_TASK_WORDS)

    held = ["edit", "add", "deliver", "send", "spec", "draft", "write", "build"]
    await session.execute(seed_row, {"p": held})
    assert (await session.execute(text(statement))).rowcount == 0
    assert (await session.execute(patterns)).scalar_one() == held


# ---------------------------------------------------------------------------
# 31. F110 v2 PR 2b: frame-context settings and the referent rule
# ---------------------------------------------------------------------------

FRAME_CONTEXT_DEFAULTS = {
    "frame_context_enabled": False,
    "frame_short_reply_max_words": 6,
    "frame_switch_min_hits": 2,
}


def test_frame_context_settings_defaults():
    s = Settings(_env_file=None)
    assert {k: getattr(s, k) for k in FRAME_CONTEXT_DEFAULTS} == FRAME_CONTEXT_DEFAULTS


def test_frame_context_settings_environment_names(monkeypatch):
    monkeypatch.setenv("NOUS_FRAME_CONTEXT_ENABLED", "true")
    monkeypatch.setenv("NOUS_FRAME_SHORT_REPLY_MAX_WORDS", "4")
    monkeypatch.setenv("NOUS_FRAME_SWITCH_MIN_HITS", "3")
    s = Settings(_env_file=None)
    assert (s.frame_context_enabled, s.frame_short_reply_max_words, s.frame_switch_min_hits) == (True, 4, 3)


@pytest.mark.parametrize("field", ["frame_short_reply_max_words", "frame_switch_min_hits"])
def test_frame_context_threshold_below_one_is_rejected(field):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: 0})


def test_frame_context_settings_have_doc_rows():
    doc = (ROOT / "docs" / "reference" / "environment-variables.md").read_text(encoding="utf-8")
    for field, default in FRAME_CONTEXT_DEFAULTS.items():
        value = str(default).lower() if isinstance(default, bool) else str(default)
        assert f"| `NOUS_{field.upper()}` | `{value}` |" in doc, field


async def test_referent_yes_do_it_after_offer():
    """An affirmative reply to an offer is work: task, by the referent rule."""
    result = await select_with_frames(
        make_engine(context=True), "yes do it", SEED_FRAMES, last_assistant_text=WRITE_IT_UP
    )
    assert (result.frame_id, result.match_method) == ("task", "referent")
    assert result.confidence == pytest.approx(0.6)


async def test_referent_requires_flag():
    """Flag off: the previous assistant text changes nothing."""
    engine = make_engine(context=False)
    today = await select_with_frames(engine, "yes do it", SEED_FRAMES)
    result = await select_with_frames(engine, "yes do it", SEED_FRAMES, last_assistant_text=WRITE_IT_UP)
    assert (today.frame_id, today.match_method) == ("conversation", "default")
    assert result.model_dump() == today.model_dump()


async def test_referent_no_offer_no_override():
    result = await select_with_frames(
        make_engine(context=True), "yes", SEED_FRAMES, last_assistant_text="Here is the summary."
    )
    assert (result.frame_id, result.match_method) == ("conversation", "default")


async def test_referent_long_reply_not_referent():
    """Seven words is not short; six still is."""
    engine = make_engine(context=True)
    offer = WRITE_IT_UP
    seven = await select_with_frames(engine, "yes do it but keep it short", SEED_FRAMES, last_assistant_text=offer)
    six = await select_with_frames(engine, "yes do it but keep short", SEED_FRAMES, last_assistant_text=offer)
    assert (seven.frame_id, seven.match_method) == ("conversation", "default")
    assert (six.frame_id, six.match_method) == ("task", "referent")


async def test_two_hit_topic_beats_referent():
    """Rule 1: two keyword hits keep the keyword frame, even for a short affirmative-led reply."""
    engine = make_engine(context=True)
    for reply in ("What if we redesign it?", "yes, what if we redesign it"):
        result = await select_with_frames(engine, reply, SEED_FRAMES, last_assistant_text=WRITE_IT_UP)
        assert (result.frame_id, result.match_method) == ("creative", "pattern"), reply
    # two task hits: task either way, but by its keywords, not the referent rule
    result = await select_with_frames(engine, "yes, build and deploy it", SEED_FRAMES, last_assistant_text=WRITE_IT_UP)
    assert (result.frame_id, result.match_method) == ("task", "pattern")


async def test_referent_yes_explain_why_stays_question():
    """The ruling: the referent rule overrides only no hits or a task/conversation frame.

    "yes, explain why" is 2 question hits (rule 1); "yes, explain it" is 1 question hit, kept by the ruling.
    """
    engine = make_engine(context=True)
    for reply in ("yes, explain why", "yes, explain it"):
        result = await select_with_frames(engine, reply, SEED_FRAMES, last_assistant_text=WRITE_IT_UP)
        assert (result.frame_id, result.match_method) == ("question", "pattern"), reply


def test_referent_fixture_composition():
    assert len(FRAME_REFERENT_PAIRS) == 44
    assert sum(method != "referent" for _, _, _, method in FRAME_REFERENT_PAIRS) >= 25
    assert sum("\n" in prev for prev, _, _, _ in FRAME_REFERENT_PAIRS) >= 5
    assert sum(reply != reply.lower() for _, reply, _, method in FRAME_REFERENT_PAIRS if method == "referent") >= 3
    # the too-early offer really sits outside the 200-character tail
    (too_early,) = [prev for prev, _, _, _ in FRAME_REFERENT_PAIRS if "\n" in prev and len(prev) > 200]
    assert "?" in too_early and len(too_early) - too_early.index("?") > 200


@pytest.mark.parametrize(
    ("prev", "reply", "frame", "method"),
    FRAME_REFERENT_PAIRS,
    ids=[f"{i:02d}-{reply}" for i, (_, reply, _, _) in enumerate(FRAME_REFERENT_PAIRS)],
)
async def test_referent_fixture_30_pairs(prev: str, reply: str, frame: str, method: str):
    result = await select_with_frames(make_engine(context=True), reply, SEED_FRAMES, last_assistant_text=prev)
    assert (result.frame_id, result.match_method) == (frame, method)
    if method == "referent":
        assert result.confidence == pytest.approx(0.6)
    else:
        today = await select_with_frames(make_engine(), reply, SEED_FRAMES)
        assert result.model_dump() == today.model_dump()


async def test_referent_without_task_frame_falls_through():
    """No task frame for the agent: today's path."""
    frames = [f for f in SEED_FRAMES if f.id != "task"]
    result = await select_with_frames(make_engine(context=True), "yes do it", frames, last_assistant_text=WRITE_IT_UP)
    assert (result.frame_id, result.match_method) == ("conversation", "default")


async def test_referent_increments_task_usage():
    engine = make_engine(context=True)
    increment = AsyncMock()
    with patch.object(engine, "_load_frames", AsyncMock(return_value=SEED_FRAMES)):
        with patch.object(engine, "_increment_usage", increment):
            await engine.select("nous-default", "yes do it", make_session(), last_assistant_text=WRITE_IT_UP)
    increment.assert_called_once()
    assert increment.call_args.args[0].id == "task"


async def test_referent_fails_open():
    """An error inside the referent rule falls back to today's selection."""
    engine = make_engine(context=True)
    today = await select_with_frames(make_engine(), "yes do it", SEED_FRAMES)
    with patch("nous.cognitive.frames._offers_action", side_effect=RuntimeError("boom")):
        result = await select_with_frames(engine, "yes do it", SEED_FRAMES, last_assistant_text=WRITE_IT_UP)
    assert result.model_dump() == today.model_dump()


@pytest.mark.parametrize("reply", ["yes do it", "build it"])
async def test_referent_settings_error_fails_open(reply: str):
    """Settings without the frame-context fields: today's selection, not an error out of select()."""
    engine = FrameEngine(MagicMock(), SimpleNamespace())
    today = await select_with_frames(make_engine(), reply, SEED_FRAMES)
    result = await select_with_frames(engine, reply, SEED_FRAMES, last_assistant_text=WRITE_IT_UP)
    assert result.model_dump() == today.model_dump()


# Every input the tests above select on, plus every fixture reply.
_TODAY_INPUTS = [
    "should we use Redis?", "build a REST API", "error in deployment", "hello how are you", "xyzzy foobar",
    "should we fix this bug", "build something", "what is the capital of France",
    "brainstorm ideas for a product", "BUILD a REST API", "build this", "build and fix the server",
    "build create implement deploy", "", "tell me everything about it", "me tell everything",
    "buildings everywhere", "xyzzy plugh quux", "Yes write short spec., deliver here", "write me a haiku",
    "yes do it but keep it short", "yes do it but keep short", "yes, explain it",
] + [reply for _, reply, _, _ in FRAME_REFERENT_PAIRS]  # fmt: skip


@pytest.mark.parametrize("reply", _TODAY_INPUTS)
async def test_context_flag_off_selects_as_today(reply: str):
    """Flag off, or no previous assistant text: select() returns what it returns without the new arguments."""
    today = await select_with_frames(make_engine(), reply, SEED_FRAMES)
    flag_off = await select_with_frames(
        make_engine(context=False), reply, SEED_FRAMES, carried=object(), last_assistant_text=WRITE_IT_UP
    )
    no_text = await select_with_frames(make_engine(context=True), reply, SEED_FRAMES, last_assistant_text=None)
    assert flag_off.model_dump() == today.model_dump()
    assert no_text.model_dump() == today.model_dump()


async def test_select_without_frames_ignores_context():
    result = await select_with_frames(make_engine(context=True), "yes do it", [], last_assistant_text=WRITE_IT_UP)
    assert (result.frame_id, result.match_method) == ("conversation", "default")


@pytest.mark.parametrize(
    ("reply", "expected"),
    [("yes", True), ("Go-ahead!", True), ("  OK, sure", True), ("do it", True), ("Do that.", True),
     ("please", True), ("please do", True), ("yes thanks", True),
     ("no", False), ("don't", False), ("", False), ("...", False), ("yesterday", False),
     # fix round 1: a lone "do" is not an affirmative; a "?" or a negation/deferral word vetoes
     ("do", False), ("do you think so", False), ("yes?", False), ("ok, why?", False),
     ("please don't", False), ("please don\u2019t", False), ("yes dont", False), ("do not", False),
     ("yeah no", False), ("yes, later", False), ("sure, but not today", False), ("ok, hold on", False),
     ("please stop", False), ("ok cancel", False), ("yes wait", False), ("sure, never mind", False),
     ("ok nope", False), ("YES NAH", False),
     # follow-up ruling: skip, leave, away; "yes, no problem" stays a cautious non-acceptance
     ("go away", False), ("ok, skip it", False), ("yes, leave it", False), ("yes, no problem", False)],
)  # fmt: skip
def test_is_affirmative_led(reply: str, expected: bool):
    assert _is_affirmative_led(reply) is expected


@pytest.mark.parametrize(
    ("assistant", "expected"),
    [("Want me to?", True), ("I CAN draft it.", True), ("Would you like a copy.", True), ("Shall I.", True),
     ("Done.", False), ("Notes should include it.", False), ("Want me to rerun it? " + "x" * 200, False),
     ("Ready. Should I merge?\n\n", True),
     # fix round 1: "I can't" / "I cannot" are not offers
     ("I can't run that tonight.", False), ("I can\u2019t do that.", False), ("I cannot do that.", False),
     ("I can. ", True)],
)  # fmt: skip
def test_offers_action(assistant: str, expected: bool):
    assert _offers_action(assistant) is expected
