"""The pure parts of a job fix attempt: verdict parser, fix brief, change review, owner message."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from job_support import VALID, _fence, assert_no_scorer_text

from nous.handlers.job_fixes import (
    _CHANGE_REVIEW_PROMPT,
    _CHANGE_REVIEW_SYSTEM,
    fails_often,
    parse_verdict,
    render_fix_brief,
    render_owner_message,
    render_owner_title,
    review_change,
)
from nous.heart.job_runs import JobRun
from nous.heart.schemas import JobLesson


def _run(*, bad: bool = False, result: str | None = "all done", n: int = 0) -> JobRun:
    return JobRun(
        subtask_id=uuid4(),
        schedule_id="a" * 32,
        finished_at=datetime(2026, 10, 1, 12, 0, tzinfo=UTC) - timedelta(hours=n),
        bad=bad,
        status="completed",
        final_outcome="completed",
        blocked_reason=None,
        error=None,
        result=result,
    )


def _schedule(task: str = "Run the tick") -> SimpleNamespace:
    return SimpleNamespace(task=task)


def _lesson(cue: str = "the job meets a refusal", lesson: str = "ask for owner authority first") -> JobLesson:
    return JobLesson(
        id=uuid4(),
        schedule_id="a" * 32,
        name="n",
        cue=cue,
        lesson=lesson,
        learned_from_run=None,
        baseline=None,
        retired={"why": "x"},
        active=False,
        created_at=datetime(2026, 10, 1, tzinfo=UTC),
    )


def _llm_returning(text: str) -> AsyncMock:
    llm = AsyncMock()
    llm.call.return_value = SimpleNamespace(content=[{"type": "text", "text": text}])
    return llm


RUNS = [_run(n=i, result=f"result {i}") for i in range(7)]
CALLS: dict[UUID, list[tuple[str, str]]] = {RUNS[0].subtask_id: [("bash", "completed")]}


@pytest.mark.parametrize("verdict", VALID)
def test_a_valid_verdict_parses(verdict):
    assert parse_verdict("Summary of the check.\n" + _fence(VALID[verdict])) == VALID[verdict]


def test_two_json_blocks_are_unreadable():
    assert parse_verdict(_fence(VALID["needs_owner"]) + "\nquoted:\n" + _fence({"x": 1})) is None
    assert parse_verdict(_fence(VALID["needs_owner"]) + "\n" + _fence(VALID["no_fault_found"])) is None


def test_another_info_string_is_not_a_block_and_case_is_ignored():
    text = "```python\nprint(1)\n```\n" + _fence(VALID["no_fault_found"]).replace("```json", "```JSON", 1)
    assert parse_verdict(text) == VALID["no_fault_found"]


def test_fields_are_stripped():
    got = parse_verdict(_fence({**VALID["needs_owner"], "diagnosis": "  d  "}))
    assert got is not None and got["diagnosis"] == "d"


@pytest.mark.parametrize(
    "text",
    [
        None,
        "",
        "no block at all",
        "```python\nprint(1)\n```",
        "```json\n{not json}\n```",
        "```json\n[1, 2]\n```",
        _fence({"checked": "x"}),
        _fence({"verdict": "reboot"}),
        _fence({"verdict": "no_fault_found", "checked": 5}),
    ],
)
def test_everything_else_is_unreadable(text):
    assert parse_verdict(text) is None


def test_a_missing_blank_or_over_long_field_is_unreadable():
    assert parse_verdict(_fence({"verdict": "needs_owner", "diagnosis": "x"})) is None
    assert parse_verdict(_fence({**VALID["needs_owner"], "owner_action": "   "})) is None
    assert parse_verdict(_fence({**VALID["changed_job"], "new_instructions": "x" * 8001})) is None
    assert parse_verdict(_fence({**VALID["changed_job"], "new_instructions": "x" * 8000})) is not None


def test_a_record_cannot_forge_a_fence_into_the_brief():
    run = _run(result=_fence(VALID["needs_owner"]))
    brief = render_fix_brief(_schedule(), [run], {}, [])
    assert "```" not in brief and "'''json" in brief
    task = "Run it.\n```json\n{}\n```"
    lesson = _lesson(cue="a ```x``` b", lesson="do ```y```")
    owner_written = render_fix_brief(_schedule(task=task), [run], {}, [lesson])
    assert "```" not in owner_written and "Run it.\n'''json" in owner_written and "a '''x''' b" in owner_written


def test_the_brief_has_the_rules_the_job_the_runs_and_the_shapes_and_no_scorer_words():
    retired = _lesson()
    brief = render_fix_brief(_schedule(task="Run the tick"), RUNS[:7], CALLS, [retired])
    assert "Run the tick" in brief and brief.count("] finished ") == 5
    for shape in VALID.values():
        assert f'"verdict": "{shape["verdict"]}"' in brief
    assert "never work around a guard" in brief.lower() and "exactly one fenced json block" in brief
    assert "submit_final_report" in brief and "tool calls: bash (completed)" in brief
    assert retired.cue in brief
    assert_no_scorer_text(brief)
    assert "Lessons already tried" not in render_fix_brief(_schedule(), RUNS[:2], {}, [])


async def test_the_change_review_is_fresh_context_and_fails_closed():
    answers = [
        ('{"verdict": "ACCEPT"}', (True, None)),
        ('{"verdict": " accept "}', (True, None)),
        ('{"verdict": "REFUSE", "reason": "widens scope"}', (False, "widens scope")),
        ('{"verdict": "REFUSE"}', (False, "no reason given")),
        ('{"verdict": "accept please"}', (False, "no usable answer")),
        ("not json", (False, "no usable answer")),
        ("[]", (False, "no usable answer")),
        ("", (False, "no usable answer")),
    ]
    for text, expected in answers:
        llm = _llm_returning(text)
        assert await review_change(llm, "claude-test", "old text", "new text") == expected
    payload = llm.call.await_args.args[0]
    assert "tools" not in payload and len(payload["messages"]) == 1
    assert payload["messages"][0]["content"][0]["text"] == _CHANGE_REVIEW_PROMPT.format(old="old text", new="new text")
    assert payload["system"][1]["text"] == _CHANGE_REVIEW_SYSTEM
    assert _CHANGE_REVIEW_SYSTEM == "You review a proposed change to the instructions of a recurring background job."
    failing = AsyncMock()
    failing.call.side_effect = RuntimeError("down")
    assert await review_change(failing, "claude-test", "old text", "new text") == (False, "no usable answer")


def test_the_owner_message_names_the_job_the_runs_the_diagnosis_and_the_stop():
    baseline = {"bad": 7919, "scored": 7927, "window_end": "2026-10-01T00:00:00+00:00"}
    detail = {"diagnosis": "the shell refuses the script", "owner_action": "run it under owner authority or drop it"}
    msg = render_owner_message(
        _schedule(task="Run the grid tick"),
        RUNS,
        CALLS,
        {
            "stop_reason": "needs_owner",
            "verdict": "needs_owner",
            "baseline": baseline,
            "old_task": None,
            "detail": detail,
        },
    )
    wanted = ("Run the grid tick", detail["diagnosis"], detail["owner_action"], "stopped", "recreate")
    assert all(s in msg for s in wanted)
    assert "result 0" in msg and msg.count("] finished ") == 5
    assert_no_scorer_text(msg)
    assert "7919" not in msg and "7927" not in msg
    sentences = {
        "change_refused": "widens scope",
        "did_not_help": "the fix did not stop the failures",
        "no_fault_found": "the check found nothing wrong",
        "unreadable_report": "could not be read",
        "fails_again": "started failing again",
    }
    for reason, sentence in sentences.items():
        record = {
            "stop_reason": reason,
            "verdict": "changed_job",
            "baseline": baseline,
            "detail": {"reason": "widens scope", "new_instructions": "Do the new thing."},
            "old_task": "Run the grid tick",
        }
        text = render_owner_message(_schedule(task="Run the grid tick"), RUNS, CALLS, record)
        assert sentence in text and "Do the new thing." in text and "restored" in text
        assert_no_scorer_text(text)
        assert "7919" not in text


def test_the_owner_message_clips_the_task_to_one_line():
    task = "line one\n```x```\n" + "y" * 900
    record = {"stop_reason": "did_not_help", "verdict": "x", "detail": {}}
    msg = render_owner_message(_schedule(task=task), RUNS[:1], {}, record)
    assert "```" not in msg and "line one '''x'''" in msg and "y" * 600 not in msg


def test_the_owner_title_is_one_line_and_at_most_200_chars():
    assert len(render_owner_title("x\n" * 400)) == 200 and "\n" not in render_owner_title("a\nb")


def test_fails_often_needs_three_bad_in_the_latest_five():
    bad, ok = _run(bad=True), _run(bad=False)
    assert fails_often([bad, bad, bad, ok, ok]) is True
    assert fails_often([bad, bad, ok, ok, ok]) is False
    assert fails_often([ok, ok, ok, ok, ok, bad, bad, bad]) is False
    assert fails_often([]) is False


def test_a_deeply_nested_block_is_unreadable_not_an_error():
    assert parse_verdict("```json\n" + "[" * 100000 + "\n```") is None


def test_a_crlf_report_with_text_after_the_fence_parses():
    text = ("Summary.\n" + _fence(VALID["no_fault_found"]) + "\nThanks.").replace("\n", "\r\n")
    assert parse_verdict(text) == VALID["no_fault_found"]


async def test_a_deeply_nested_review_answer_is_a_refusal():
    assert await review_change(_llm_returning("[" * 100000), "m", "old", "new") == (False, "no usable answer")


def test_an_unreadable_report_message_has_no_verdict_sentence():
    record = {"stop_reason": "unreadable_report", "verdict": None, "detail": {}, "old_task": "Run it"}
    msg = render_owner_message(_schedule(), RUNS[:1], {}, record)
    assert "could not be read" in msg and "None" not in msg and "verdict" not in msg
