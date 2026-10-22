"""The job-lessons block shown to a scheduled run of the same job."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from nous.api.tools import build_subtask_prefix
from nous.handlers.job_lessons import SHOWN_LESSONS, job_lessons_block
from nous.handlers.subtask_executor import execute_hardened
from nous.handlers.subtask_worker import SubtaskWorkerPool
from nous.heart.schemas import JobLesson
from tests.test_f061_routing_integration import _make_settings as _make_routing_settings
from tests.test_f061_subtask_executor import (
    _make_heart_mock,
    _make_settings,
    _make_subtask,
    _scripted_runner,
)

HEX = uuid4().hex
on = SimpleNamespace(job_lessons_enabled=True)
off = SimpleNamespace(job_lessons_enabled=False)


def _sub(meta: dict) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(),
        task="t",
        frame_type=None,
        model=None,
        notify=False,
        output_format=None,
        success_criteria=None,
        metadata_=meta,
    )


sub = _sub({"schedule_id": HEX})


def _lesson(i: int) -> JobLesson:
    return JobLesson(
        id=uuid4(),
        schedule_id=HEX,
        name=f"n{i}",
        cue=f"cue {i}",
        lesson=f"do {i}",
        learned_from_run=None,
        baseline=None,
        retired=None,
        active=True,
        created_at=datetime.now(UTC),
    )


def _heart() -> MagicMock:
    heart = MagicMock()
    heart.list_job_lessons = AsyncMock(return_value=[_lesson(0)])
    return heart


async def test_the_block_is_none_unless_the_flag_is_on_and_the_run_is_scheduled():
    heart = _heart()
    assert await job_lessons_block(heart, off, sub) is None and heart.list_job_lessons.await_count == 0
    assert await job_lessons_block(heart, on, SimpleNamespace(metadata_={})) is None
    assert await job_lessons_block(heart, on, SimpleNamespace(metadata_={"schedule_id": HEX, "dag_id": "d"})) is None
    assert await job_lessons_block(heart, SimpleNamespace(), sub) is None
    assert heart.list_job_lessons.await_count == 0


async def test_the_block_lists_at_most_three_lessons_newest_first_in_the_pinned_shape():
    heart = _heart()
    every = [_lesson(i) for i in range(5)]
    heart.list_job_lessons.side_effect = lambda _sid, active, limit: every[:limit]
    assert await job_lessons_block(heart, on, sub) == (
        "# Lessons from earlier runs of this job\n"
        "- When: cue 0\n  Do: do 0\n- When: cue 1\n  Do: do 1\n- When: cue 2\n  Do: do 2"
    )
    heart.list_job_lessons.assert_awaited_once_with(HEX, active=True, limit=SHOWN_LESSONS)


async def test_no_lessons_is_none():
    heart = _heart()
    heart.list_job_lessons.return_value = []
    assert await job_lessons_block(heart, on, sub) is None


async def test_a_failing_read_returns_none_and_logs_a_warning(caplog):
    heart = _heart()
    heart.list_job_lessons.side_effect = RuntimeError("db down")
    assert await job_lessons_block(heart, on, sub) is None
    assert "job lessons block failed" in caplog.text.lower()


async def test_a_lesson_with_line_breaks_is_one_line_per_field():
    heart = _heart()
    heart.list_job_lessons.return_value = [_lesson(0).model_copy(update={"cue": "a\nb", "lesson": "do\n\nthis"})]
    expected = "# Lessons from earlier runs of this job\n- When: a b\n  Do: do this"
    assert await job_lessons_block(heart, on, sub) == expected


async def test_the_hardened_prefix_ends_with_the_block():
    subtask = _make_subtask(metadata_={"schedule_id": HEX})
    runner = _scripted_runner(
        scripted_payloads=[{"summary": "Checked the report and found nothing to change. " * 2, "confidence": 0.9}]
    )
    with patch("nous.handlers.job_lessons.job_lessons_block", new=AsyncMock(return_value="BLOCK")):
        await execute_hardened(subtask, "sess-1", runner=runner, heart=_make_heart_mock(), settings=_make_settings())
    assert runner.run_turn.await_args.kwargs["system_prompt_prefix"].endswith("\n\nBLOCK")


async def test_the_legacy_prefix_ends_with_the_block_and_is_unchanged_without_one():
    runner = AsyncMock()
    runner.run_turn = AsyncMock(return_value=("text", MagicMock(), {"input_tokens": 1, "output_tokens": 1}))
    subtask = SimpleNamespace(
        id=uuid4(),
        task="legacy task",
        frame_type=None,
        model=None,
        notify=False,
        output_format=None,
        success_criteria=None,
        metadata_={"schedule_id": HEX},
    )
    pool = SubtaskWorkerPool(
        runner=runner, heart=_make_heart_mock(), settings=_make_routing_settings(hardening_enabled=False)
    )
    with patch("nous.handlers.job_lessons.job_lessons_block", new=AsyncMock(return_value="BLOCK")):
        await pool._execute_legacy(subtask, "sess-1")
    assert runner.run_turn.await_args.kwargs["system_prompt_prefix"].endswith("\n\nBLOCK")
    with patch("nous.handlers.job_lessons.job_lessons_block", new=AsyncMock(return_value=None)):
        await pool._execute_legacy(subtask, "sess-1")
    assert runner.run_turn.await_args.kwargs["system_prompt_prefix"] == build_subtask_prefix("legacy task", None)
