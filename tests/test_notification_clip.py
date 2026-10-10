"""Telegram notifications say when they are cut: ``continuation.clip_text`` and the subtask push that uses it."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from nous.brain.continuation import clip_text, utf16_units
from nous.config import Settings
from nous.handlers.subtask_worker import SubtaskWorkerPool
from nous.storage.models import Subtask

MARKER = "… [truncated — {dropped} more chars]"


# ---- clip_text ----------------------------------------------------------------------------------------


def test_empty_text_is_unchanged():
    assert clip_text("", 10, marker=MARKER) == ""


def test_text_exactly_at_the_limit_is_unchanged():
    text = "a" * 50
    assert clip_text(text, 50, marker=MARKER) == text


def test_text_one_over_the_limit_is_cut_with_the_marker():
    text = "a" * 51
    out = clip_text(text, 50, marker=MARKER)
    assert utf16_units(out) <= 50
    kept = out.split("…")[0]
    assert out == kept + MARKER.replace("{dropped}", str(51 - len(kept)))


def test_the_cut_falls_on_a_word_boundary():
    text = "alpha beta gamma delta epsilon zeta eta theta iota kappa lambda"
    out = clip_text(text, 40)
    assert utf16_units(out) <= 40
    assert out.endswith("…")
    assert text.startswith(out[:-1])
    assert out[:-1].split()[-1] in text.split()  # no word cut in half


def test_the_cut_prefers_a_line_break():
    text = "first line of the result here\nsecond line that keeps going and going"
    out = clip_text(text, 40, marker="…")
    assert out == "first line of the result here…"


def test_no_boundary_near_the_end_means_a_hard_cut():
    text = "short " + "x" * 200
    out = clip_text(text, 50, marker="…")
    assert utf16_units(out) == 50
    assert out == text[:49] + "…"


def test_the_dropped_count_is_exact():
    text = "word " * 100
    out = clip_text(text, 60, marker=MARKER)
    kept = out.split("…")[0]
    assert f"{len(text) - len(kept)} more chars" in out


def test_emoji_count_as_two_utf16_units():
    text = "\U0001f600" * 30  # 60 UTF-16 units, 30 characters
    assert clip_text(text, 60) == text
    out = clip_text(text, 59)
    assert utf16_units(out) <= 59
    assert out == "\U0001f600" * 29 + "…"


def test_multibyte_text_is_cut_at_a_character_boundary():
    text = "привет " * 40  # Cyrillic
    out = clip_text(text, 100, marker=MARKER)
    assert utf16_units(out) <= 100
    out.encode("utf-8")  # valid text, no lone surrogate


def test_a_limit_smaller_than_the_marker_returns_the_marker_clipped():
    out = clip_text("x" * 100, 5, marker=MARKER)
    assert utf16_units(out) <= 5


# ---- the subtask push -----------------------------------------------------------------------------------


def _pool() -> tuple[SubtaskWorkerPool, AsyncMock]:
    http = AsyncMock()
    http.post = AsyncMock(return_value=MagicMock(status_code=200))
    settings = Settings(_env_file=None, telegram_bot_token="test-token", telegram_chat_id="12345")
    return SubtaskWorkerPool(runner=MagicMock(), heart=MagicMock(), settings=settings, http_client=http), http


def _subtask(task: str = "Summarise the report") -> Subtask:
    return Subtask(id=uuid.uuid4(), agent_id="a", task=task, notify=True)


@pytest.mark.asyncio
async def test_a_result_that_fits_is_sent_whole():
    pool, http = _pool()
    result = "word " * 700  # 3500 chars: the old code cut this at 500
    await pool._notify_telegram(_subtask(), result=result)
    text = http.post.await_args.kwargs["json"]["text"]
    assert text == f"Subtask completed: Summarise the report\n\nResult: {result}"
    assert "truncated" not in text


@pytest.mark.asyncio
async def test_a_long_result_fills_the_message_and_says_it_was_cut():
    pool, http = _pool()
    st = _subtask()
    await pool._notify_telegram(st, result="word " * 2000)
    text = http.post.await_args.kwargs["json"]["text"]
    assert 4000 < utf16_units(text) <= 4096
    assert f"more chars; full result: subtask {st.id.hex[:8]}]" in text
    assert "… [truncated — " in text


@pytest.mark.asyncio
async def test_a_long_emoji_result_stays_within_telegrams_utf16_limit():
    pool, http = _pool()
    await pool._notify_telegram(_subtask(), result="\U0001f680" * 3000)
    text = http.post.await_args.kwargs["json"]["text"]
    assert utf16_units(text) <= 4096
    assert "[truncated" in text


@pytest.mark.asyncio
async def test_a_long_error_gets_the_same_treatment():
    pool, http = _pool()
    st = _subtask()
    await pool._notify_telegram(st, error="Traceback line\n" * 400)
    text = http.post.await_args.kwargs["json"]["text"]
    assert text.startswith("Subtask failed: Summarise the report\n\nError: Traceback line\n")
    assert utf16_units(text) <= 4096
    assert f"full error: subtask {st.id.hex[:8]}]" in text


@pytest.mark.asyncio
async def test_a_clipped_task_preview_ends_with_an_ellipsis():
    pool, http = _pool()
    await pool._notify_telegram(_subtask(task="research " * 30), result="done")
    text = http.post.await_args.kwargs["json"]["text"]
    preview = text.split("\n\n")[0].removeprefix("Subtask completed: ")
    assert preview.endswith("…")
    assert utf16_units(preview) <= 100
