"""The pure parts of a job fix attempt: verdict parser, fix brief, change review, owner message."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import pytest
from job_support import NOW, VALID, _fence, assert_no_scorer_text

from nous.brain import continuation
from nous.config import Settings
from nous.handlers.job_fixes import (
    _CHANGE_REVIEW_PROMPT,
    _CHANGE_REVIEW_SYSTEM,
    JobFixer,
    fails_often,
    parse_verdict,
    render_fix_brief,
    render_owner_message,
    render_owner_title,
    review_change,
)
from nous.heart.job_runs import FIX_KEY, JobRun
from nous.heart.schemas import JobLesson

# Words the brief and the owner message must never use, on top of job_support's scorer sentinels.
_SCORER_WORDS = re.compile(r"\b(bad|good|partial|score|done_self|not_scored)\b", re.I)


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
    assert "Do not stop, cancel or recreate the job." in brief
    assert "submit_final_report" in brief and "tool calls: bash (completed)" in brief
    assert retired.cue in brief
    assert_no_scorer_text(brief)
    assert not _SCORER_WORDS.search(brief)
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
    assert "or works around a refusal, a guard, a protected path or a credential check" in _CHANGE_REVIEW_PROMPT
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
    assert not _SCORER_WORDS.search(msg)
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
        assert not _SCORER_WORDS.search(text)
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


def test_the_owner_message_shows_the_restored_instructions_not_the_changed_ones():
    changed = SimpleNamespace(task="Changed text")
    record = {"stop_reason": "did_not_help", "old_task": "Original text", "detail": {}}
    msg = render_owner_message(changed, RUNS[:1], {}, record)
    assert "Job instructions: Original text" in msg and "Changed text" not in msg
    assert "Job instructions: Changed text" in render_owner_message(changed, RUNS[:1], {}, {"stop_reason": "x"})


# --- JobFixer: the state machine, with a mocked heart, reader, LLM and fire helper ---

KEYS = ("started", "start_failed", "applied", "refused", "kept", "closed", "stopped", "waiting", "skipped")
NEEDS = _fence(VALID["needs_owner"])
CHANGED = _fence(VALID["changed_job"])
NO_FAULT = _fence(VALID["no_fault_found"])
FIX_RUN = uuid4()
HEX = uuid4().hex
_CONTAINER = SimpleNamespace(id=uuid4(), root_id=uuid4(), intent="Check the report", origin_channel=None)


@pytest.fixture(autouse=True)
def _stop_patches():
    yield
    patch.stopall()


def _days_ago(n: float) -> datetime:
    return NOW - timedelta(days=n)


def _record(state: str, **kw) -> dict:
    record = {
        "state": state, "trigger": "fails_often", "fix_run": str(FIX_RUN), "started_at": NOW,
        "baseline": {"bad": 2, "scored": 10, "window_end": NOW.isoformat()}, "verdict": None, "detail": {},
        "old_task": None, "applied_at": NOW, "settled_at": NOW, "stop_reason": None, "report_id": None,
    }
    record.update(kw)
    return {k: v.isoformat() if isinstance(v, datetime) else v for k, v in record.items()}


def _fix(status: str, result: str | None, error: str | None = None, outcome: str | None = None) -> SimpleNamespace:
    """A fix-run row as the fixer reads it."""
    return SimpleNamespace(status=status, result=result, error=error, final_outcome=outcome)


def _runs(n: int = 5, bad: int = 0) -> list[JobRun]:
    return [_run(bad=i < bad, n=i) for i in range(n)]


def _retired_lesson(hex_id: str, at: str | None = None) -> JobLesson:
    return JobLesson(
        id=uuid4(), schedule_id=hex_id, name="n", cue="the job meets a refusal", lesson="ask first",
        learned_from_run=None, baseline=None, active=False, created_at=NOW,
        retired={"reason": "no_improvement", "at": at or NOW.isoformat()},
    )


def _heart() -> MagicMock:
    heart = MagicMock()
    heart.agent_id = "agent"
    for owner, names in {
        "schedules": ("list_with_metadata_key", "set_metadata", "deactivate", "get"),
        "subtasks": ("get", "cancel"),
        "job_runs": ("scored_runs", "ledger_calls", "schedules_with_bad_runs", "fix_run_for"),
        "intentions": ("get_for_source",),
    }.items():
        for name in names:
            setattr(getattr(heart, owner), name, AsyncMock())
    heart.job_runs.scored_runs.return_value = []
    heart.job_runs.ledger_calls.return_value = {}
    heart.job_runs.schedules_with_bad_runs.return_value = []
    heart.job_runs.fix_run_for.return_value = None
    heart.list_job_lessons = AsyncMock(return_value=[])
    heart.intentions.get_for_source.return_value = _CONTAINER
    heart.schedules.list_with_metadata_key.return_value = []
    return heart


def _fixer(heart: MagicMock | None = None, **over) -> tuple[JobFixer, MagicMock, list[str]]:
    llm = over.pop("llm", AsyncMock())
    settings = Settings(
        _env_file=None,
        **{
            "result_inbox_enabled": True, "intentions_enabled": True, "continuation_enabled": True,
            "telegram_chat_id": "8080", "job_fixes_enabled": True, **over,
        },
    )
    heart = heart or _heart()
    order: list[str] = []
    heart.writes = []

    async def set_metadata(_sid, _key, value, *, task=None):
        order.append("set_metadata")
        heart.writes.append((value, task))

    async def deactivate(_sid):
        order.append("deactivate")

    async def write_report(_schedule, _record):
        order.append("_write_owner_report")
        return uuid4()

    heart.schedules.set_metadata.side_effect = set_metadata
    heart.schedules.deactivate.side_effect = deactivate
    fixer = JobFixer(heart, settings, llm, clock=lambda: NOW)
    fixer._write_owner_report = AsyncMock(side_effect=write_report)
    return fixer, heart, order


async def _step(record, *, active=True, fix=None, runs=(), review=None, task="Old text", heart=None):
    fixer, heart, order = _fixer(heart)
    schedule = SimpleNamespace(id=uuid4(), metadata_={FIX_KEY: record}, active=active, task=task)
    heart.schedules.list_with_metadata_key.return_value = [schedule]
    heart.subtasks.get.return_value = fix
    heart.job_runs.scored_runs.return_value = list(runs)
    with patch("nous.handlers.job_fixes.review_change", AsyncMock(return_value=review or (True, None))):
        stats = await fixer.run_sleep()
    return stats, order, heart.writes


def _start_env(*, recent, baseline=None, retired=(), schedules=1, with_record=0, container=_CONTAINER, ids=None):
    fixer, heart, _order = _fixer()
    made = [
        SimpleNamespace(
            id=ids[i] if ids else (UUID(HEX) if i == 0 else uuid4()), active=True, schedule_type="recurring",
            task="Check the report", timeout_seconds=120,
            metadata_={FIX_KEY: {"state": "kept"}} if i < with_record else {},
        )
        for i in range(schedules)
    ]
    by_id = {s.id: s for s in made}
    baseline = baseline if baseline is not None else recent

    async def scored_runs(_hex, **kw):
        return list(baseline if kw.get("until") is not None else recent)

    heart.schedules.get.side_effect = lambda sid: by_id.get(sid)
    heart.schedules.get.return_value = made[0]
    heart.job_runs.scored_runs.side_effect = scored_runs
    heart.job_runs.schedules_with_bad_runs.return_value = [s.id.hex for s in made]
    heart.list_job_lessons.return_value = list(retired)
    heart.intentions.get_for_source.return_value = container
    heart.made = made
    create_fire = AsyncMock(return_value=SimpleNamespace(id=uuid4()))
    patch("nous.handlers.job_fixes.create_fire", create_fire).start()
    return fixer, heart, create_fire


@pytest.mark.parametrize(
    "over", [{"intentions_enabled": False}, {"continuation_enabled": False}, {"telegram_chat_id": ""}, {"llm": None}]
)
async def test_missing_preconditions_skip_once_with_a_warning(over, caplog):
    fixer, heart, _order = _fixer(**over)
    assert await fixer.run_sleep() == {**dict.fromkeys(KEYS, 0), "skipped": 1}
    heart.schedules.list_with_metadata_key.assert_not_called()
    heart.job_runs.schedules_with_bad_runs.assert_not_called()
    await fixer.run_sleep()
    assert caplog.text.count("check-and-fix is skipped") == 1


@pytest.mark.parametrize(
    ("record", "fix", "runs", "review", "state", "stat", "stopped_for"),
    [
        (_record("running"), _fix("running", None), (), None, None, "waiting", None),
        (_record("running"), None, (), None, "stopped", "stopped", "unreadable_report"),
        (_record("running", fix_run="garbage"), None, (), None, "stopped", "stopped", "unreadable_report"),
        (_record("running", started_at=_days_ago(3)), _fix("pending", None), (), None,
         "stopped", "stopped", "unreadable_report"),
        (_record("running", started_at="garbage"), _fix("pending", None), (), None,
         "stopped", "stopped", "unreadable_report"),
        (_record("running"), _fix("cancelled", None), (), None,
         "stopped", "stopped", "unreadable_report"),
        (_record("running"), _fix("completed", CHANGED), (), (True, None),
         "judging", "applied", None),
        (_record("running"), _fix("completed", CHANGED), (), (False, "widens scope"),
         "stopped", "refused", "change_refused"),
        (_record("running"), _fix("completed", _fence(VALID["fixed_elsewhere"])), (), None,
         "judging", "applied", None),
        (_record("running"), _fix("completed", NEEDS), (), None,
         "stopped", "stopped", "needs_owner"),
        (_record("running"), _fix("completed", NO_FAULT), _runs(bad=1), None,
         "closed", "closed", None),
        (_record("running"), _fix("completed", NO_FAULT), _runs(bad=3), None,
         "stopped", "stopped", "no_fault_found"),
        (_record("running"), _fix("completed", "no block"), (), None,
         "stopped", "stopped", "unreadable_report"),
        (_record("running"), _fix("failed", None), (), None,
         "stopped", "stopped", "unreadable_report"),
        (_record("running"), _fix("completed", NEEDS + "\n" + _fence({"quoted": 1})), (),
         None, "stopped", "stopped", "unreadable_report"),
        (_record("judging"), None, _runs(n=4), None, None, "waiting", None),
        (_record("judging"), None, _runs(n=5, bad=0), None, "kept", "kept", None),
        (_record("judging"), None, _runs(n=5, bad=1), None, "stopped", "stopped", "did_not_help"),
        (_record("kept"), None, _runs(n=5, bad=3), None, "stopped", "stopped", "fails_again"),
        (_record("closed"), None, _runs(n=5, bad=2), None, None, None, None),
    ],
)
async def test_each_transition_of_the_state_table(record, fix, runs, review, state, stat, stopped_for):
    heart = _heart()
    stats, order, writes = await _step(record, fix=fix, runs=runs, review=review, heart=heart)
    if stat:
        assert stats[stat] == 1
    if state is None:
        assert writes == []
    else:
        assert writes[-1][0]["state"] == state
    if stopped_for:
        assert writes[-1][0]["stop_reason"] == stopped_for and stats["stopped"] == 1
        assert order[-3:] == ["_write_owner_report", "deactivate", "set_metadata"]
    else:
        assert "deactivate" not in order and "_write_owner_report" not in order
    assert all(c.kwargs["settled_only"] is False for c in heart.job_runs.scored_runs.await_args_list)


async def test_an_accepted_change_replaces_the_task_and_keeps_the_old_one():
    stats, _order, writes = await _step(
        _record("running"), fix=_fix("completed", CHANGED), review=(True, None)
    )
    record, task = writes[-1]
    assert task == "Run the check and report."
    assert (record["state"], record["old_task"], record["verdict"]) == ("judging", "Old text", "changed_job")
    assert record["applied_at"] and stats["applied"] == 1


async def test_a_fix_outside_the_job_leaves_the_task_alone():
    _stats, _order, writes = await _step(
        _record("running"), fix=_fix("completed", _fence(VALID["fixed_elsewhere"]))
    )
    assert writes[-1][1] is None and writes[-1][0]["old_task"] is None


async def test_a_failed_judging_restores_the_old_instructions_before_the_message():
    _stats, order, writes = await _step(_record("judging", old_task="Old text"), runs=_runs(n=5, bad=1))
    assert order == ["set_metadata", "_write_owner_report", "deactivate", "set_metadata"]
    assert writes[0][1] == "Old text" and writes[0][0]["state"] == "stopping"
    assert writes[-1][0]["state"] == "stopped" and writes[-1][1] is None


async def test_a_stopping_record_resumes_with_only_the_remaining_steps():
    stats, order, writes = await _step(_record("stopping", stop_reason="needs_owner", old_task=None))
    assert order == ["_write_owner_report", "deactivate", "set_metadata"]
    assert writes[-1][0]["state"] == "stopped" and stats["stopped"] == 1


async def test_a_refused_change_keeps_the_proposal_and_the_reason_in_the_detail():
    _stats, _order, writes = await _step(
        _record("running"), fix=_fix("completed", CHANGED), review=(False, "widens scope")
    )
    record = writes[0][0]
    assert record["verdict"] == "changed_job"
    assert record["detail"]["new_instructions"] == "Run the check and report."
    assert record["detail"]["reason"] == "widens scope"


async def test_a_stuck_fix_run_is_cancelled_when_it_is_given_up_on():
    heart = _heart()
    await _step(
        _record("running", started_at=_days_ago(3)), fix=_fix("pending", None), heart=heart
    )
    heart.subtasks.cancel.assert_awaited_once_with(FIX_RUN)
    young = await _step(
        _record("running", started_at=_days_ago(1)), fix=_fix("pending", None)
    )
    assert young[0]["waiting"] == 1 and young[2] == []


async def test_the_owner_stopping_the_job_mid_fix_ends_in_stopped_inactive_with_no_message():
    for state in ("running", "judging", "kept", "closed"):
        heart = _heart()
        _stats, order, writes = await _step(_record(state), active=False, heart=heart)
        assert (writes[-1][0]["state"], writes[-1][0]["stop_reason"]) == ("stopped", "inactive")
        assert "_write_owner_report" not in order and "deactivate" not in order and writes[-1][1] is None
        if state == "running":
            heart.subtasks.cancel.assert_awaited_once_with(FIX_RUN)
        else:
            heart.subtasks.cancel.assert_not_awaited()
    _stats, order, writes = await _step(_record("judging", old_task="Old text"), active=False)
    assert writes[-1][1] == "Old text" and writes[-1][0]["stop_reason"] == "inactive"
    assert "_write_owner_report" not in order


@pytest.mark.parametrize("baseline", [None, "garbage", {"bad": 1}, {"bad": 1, "scored": 0}, {"bad": "x", "scored": 5}])
async def test_a_judging_record_with_an_unusable_baseline_is_ignored_not_divided(baseline):
    stats, order, writes = await _step(_record("judging", baseline=baseline), runs=_runs(n=5, bad=0))
    assert writes == [] and order == [] and stats == dict.fromkeys(KEYS, 0)


@pytest.mark.parametrize("bad_record", ["garbage", {"state": "weird"}, {"state": "stopped"}, None, ["x"]])
async def test_a_stopped_or_malformed_record_is_ignored(bad_record):
    stats, order, writes = await _step(bad_record)
    assert writes == [] and order == [] and stats == dict.fromkeys(KEYS, 0)


async def test_three_bad_of_the_latest_five_starts_a_fix_run_with_the_pinned_arguments():
    fixer, heart, create_fire = _start_env(recent=_runs(n=5, bad=3), baseline=_runs(n=20, bad=6))
    stats = await fixer.run_sleep()
    assert stats["started"] == 1
    kwargs = create_fire.await_args.kwargs
    assert kwargs["metadata"] == {"job_fix": True, "max_attempts": 1}
    assert kwargs["notify"] is False and kwargs["timeout"] == 600
    assert "Job instructions:" in kwargs["task"]
    assert kwargs["intent"] == "Check a background job that keeps going wrong: Check the report"
    record = heart.schedules.set_metadata.await_args.args[2]
    assert (record["state"], record["trigger"]) == ("running", "fails_often")
    assert record["fix_run"] == str(create_fire.return_value.id)
    assert record["baseline"]["bad"] == 6 and record["baseline"]["scored"] == 20
    assert record["baseline"]["window_end"] == NOW.isoformat() and record["started_at"] == NOW.isoformat()
    assert all(c.kwargs["settled_only"] is False for c in heart.job_runs.scored_runs.await_args_list)
    assert_no_scorer_text(kwargs["task"], kwargs["intent"])


async def test_a_retired_lesson_with_one_bad_run_triggers_but_two_bad_runs_alone_do_not():
    fixer, heart, _fire = _start_env(recent=_runs(n=5, bad=1), retired=[_retired_lesson(HEX)])
    assert (await fixer.run_sleep())["started"] == 1
    assert heart.schedules.set_metadata.await_args.args[2]["trigger"] == "lesson_retired"
    fixer, heart, create_fire = _start_env(recent=_runs(n=5, bad=2))
    assert (await fixer.run_sleep())["started"] == 0 and create_fire.await_count == 0
    old = _retired_lesson(HEX, at=_days_ago(40).isoformat())
    fixer, heart, create_fire = _start_env(recent=_runs(n=5, bad=1), retired=[old])
    assert (await fixer.run_sleep())["started"] == 0 and create_fire.await_count == 0
    fixer, heart, create_fire = _start_env(recent=_runs(n=5, bad=1), retired=[_retired_lesson(HEX, at="soon")])
    assert (await fixer.run_sleep())["started"] == 0


async def test_one_attempt_starts_per_sleep_and_a_schedule_with_a_record_is_not_restarted():
    fixer, heart, create_fire = _start_env(recent=_runs(n=5, bad=3), schedules=3, with_record=1)
    assert (await fixer.run_sleep())["started"] == 1 and create_fire.await_count == 1


async def test_a_failed_fire_is_start_failed_and_leaves_no_record():
    fixer, heart, create_fire = _start_env(recent=_runs(n=5, bad=3))
    create_fire.side_effect = ValueError("pending subtask limit (5) reached")
    stats = await fixer.run_sleep()
    assert (stats["start_failed"], stats["started"]) == (1, 0) and heart.schedules.set_metadata.await_count == 0


async def test_a_job_without_a_container_is_counted_skipped_and_never_fired():
    fixer, heart, create_fire = _start_env(recent=_runs(n=5, bad=3), container=None)
    stats = await fixer.run_sleep()
    assert (stats["skipped"], stats["started"]) == (1, 0) and create_fire.await_count == 0


class _Session:
    def __init__(self) -> None:
        self.session = AsyncMock()

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, *exc):
        return False


async def test_the_owner_report_is_a_report_row_with_a_stable_id_and_shows_what_was_tried():
    fixer, heart, _order = _fixer()
    session = _Session()
    heart.db.session = MagicMock(return_value=session)
    heart.job_runs.scored_runs.return_value = _runs(n=5, bad=1)
    schedule = SimpleNamespace(id=uuid4(), metadata_={}, active=True, task="Old text")
    record = _record("judging", old_task="Old text", verdict="changed_job", detail=dict(VALID["changed_job"]))
    insert = AsyncMock(return_value=uuid4())
    push = AsyncMock(return_value=NOW)
    with patch.object(continuation, "insert_report", insert), patch.object(continuation, "owner_push_after", push):
        fixer._write_owner_report = JobFixer._write_owner_report.__get__(fixer)
        await fixer._stop_and_ask(schedule, record, "did_not_help", {}, dict.fromkeys(KEYS, 0))
    kwargs = insert.await_args.kwargs
    assert kwargs["kind"] == continuation.MSG_REPORT and kwargs["channel"] == "telegram:8080"
    assert kwargs["report_id"] == uuid5(NAMESPACE_URL, f"nous-job-fix:{schedule.id}")
    assert (kwargs["intention_id"], kwargs["root_id"]) == (_CONTAINER.id, _CONTAINER.root_id)
    assert "Run the check and report." in kwargs["body"]
    assert "The old instructions were restored." in kwargs["body"]
    assert_no_scorer_text(kwargs["body"], kwargs["title"])
    session.session.commit.assert_awaited()
    assert heart.job_runs.scored_runs.await_args.kwargs["settled_only"] is False
    assert heart.writes[-1][0]["state"] == "stopped" and heart.writes[-1][0]["report_id"] == str(insert.return_value)


async def test_without_a_container_no_report_is_written_and_the_job_is_still_stopped():
    fixer, heart, _order = _fixer()
    heart.intentions.get_for_source.return_value = None
    fixer._write_owner_report = JobFixer._write_owner_report.__get__(fixer)
    schedule = SimpleNamespace(id=uuid4(), metadata_={}, active=True, task="t")
    verdict = dict(VALID["needs_owner"])
    await fixer._stop_and_ask(schedule, _record("running"), "needs_owner", verdict, dict.fromkeys(KEYS, 0))
    assert heart.writes[-1][0]["state"] == "stopped" and heart.writes[-1][0]["report_id"] is None
    heart.schedules.deactivate.assert_awaited_once()


async def test_a_pending_fix_run_with_an_unreadable_start_time_is_cancelled_and_given_up_on():
    heart = _heart()
    _stats, _order, writes = await _step(
        _record("running", started_at="garbage"), fix=_fix("pending", None), heart=heart
    )
    heart.subtasks.cancel.assert_awaited_once_with(FIX_RUN)
    assert writes[-1][0]["stop_reason"] == "unreadable_report"


async def test_judging_reads_runs_after_the_change_and_settled_records_after_they_settled():
    heart = _heart()
    await _step(_record("judging", applied_at=_days_ago(2)), runs=_runs(n=5), heart=heart)
    assert heart.job_runs.scored_runs.await_args.kwargs["after"] == _days_ago(2)
    for state in ("kept", "closed"):
        heart = _heart()
        await _step(_record(state, settled_at=_days_ago(1)), runs=_runs(n=5), heart=heart)
        assert heart.job_runs.scored_runs.await_args.kwargs["after"] == _days_ago(1)


async def test_candidates_keep_the_reader_order_then_newest_retirement_and_none_is_tried_twice():
    fixer, heart, create_fire = _start_env(recent=_runs(n=5, bad=3), schedules=3)
    a, b, c = (s.id for s in heart.made)
    heart.job_runs.schedules_with_bad_runs.return_value = [c.hex, a.hex]  # newest bad run first
    assert (await fixer.run_sleep())["started"] == 1
    assert create_fire.await_args.args[2].id == c and create_fire.await_count == 1  # one attempt, the first candidate

    # Newest retirement (b) has the largest hex and the oldest (c) the smallest, so an id-sorted order is wrong.
    c, a, b = sorted([uuid4(), uuid4(), uuid4()], key=lambda x: x.hex)
    ids = [a, b, c]  # the first two hold a record, so only c can start
    fixer, heart, create_fire = _start_env(
        recent=_runs(n=5, bad=3), schedules=3, with_record=2, ids=ids,
        retired=[
            _retired_lesson(c.hex, at=(NOW - timedelta(days=5)).isoformat()),
            _retired_lesson(b.hex, at=(NOW - timedelta(days=1)).isoformat()),
            _retired_lesson(a.hex, at=(NOW - timedelta(days=2)).isoformat()),
        ],
    )
    heart.job_runs.schedules_with_bad_runs.return_value = [a.hex]
    assert (await fixer.run_sleep())["started"] == 1
    assert [call.args[0] for call in heart.schedules.get.await_args_list] == [a, b, c]


async def test_the_report_goes_to_the_containers_origin_channel_when_it_has_one():
    container = SimpleNamespace(id=uuid4(), root_id=uuid4(), intent="Check the report", origin_channel="telegram:1")
    fixer, heart, _order = _fixer()
    heart.db.session = MagicMock(return_value=_Session())
    heart.intentions.get_for_source.return_value = container
    fixer._write_owner_report = JobFixer._write_owner_report.__get__(fixer)
    insert, push = AsyncMock(return_value=uuid4()), AsyncMock(return_value=NOW)
    schedule = SimpleNamespace(id=uuid4(), metadata_={}, active=True, task="t")
    with patch.object(continuation, "insert_report", insert), patch.object(continuation, "owner_push_after", push):
        await fixer._stop_and_ask(
            schedule, _record("running"), "needs_owner", dict(VALID["needs_owner"]), dict.fromkeys(KEYS, 0)
        )
    assert insert.await_args.kwargs["channel"] == "telegram:1" and push.await_args.kwargs["channel"] == "telegram:1"


async def test_a_malformed_record_is_logged_once_across_sleeps(caplog):
    fixer, heart, _order = _fixer()
    schedule = SimpleNamespace(id=uuid4(), metadata_={FIX_KEY: "garbage"}, active=True, task="t")
    heart.schedules.list_with_metadata_key.return_value = [schedule]
    await fixer.run_sleep()
    await fixer.run_sleep()
    assert caplog.text.count("is malformed and is ignored") == 1


async def test_a_fix_run_left_by_a_crashed_start_is_adopted_not_fired_again():
    fixer, heart, create_fire = _start_env(recent=_runs(n=5, bad=3), baseline=_runs(n=20, bad=6))
    existing, created = uuid4(), NOW - timedelta(hours=30)
    heart.job_runs.fix_run_for.return_value = (existing, created)
    stats = await fixer.run_sleep()
    assert stats["started"] == 1 and create_fire.await_count == 0
    heart.job_runs.fix_run_for.assert_awaited_once_with(HEX)
    record = heart.schedules.set_metadata.await_args.args[2]
    assert (record["state"], record["trigger"]) == ("running", "fails_often")
    assert record["fix_run"] == str(existing) and record["started_at"] == created.isoformat()
    assert record["baseline"]["bad"] == 6 and record["baseline"]["scored"] == 20


CHECK_CASES = [
    (_fix("failed", None, error="boom ```x```", outcome="errored"),
     "The check itself ended failed/errored: boom '''x'''"),
    (_fix("cancelled", None), "The check itself ended cancelled"),
    (_fix("completed", "no block here", outcome="incomplete_blocked"),
     "The check itself ended completed/incomplete_blocked: no block here"),
]


@pytest.mark.parametrize(("fix", "line"), CHECK_CASES)
async def test_an_unreadable_report_carries_what_the_check_itself_reported(fix, line):
    _stats, _order, writes = await _step(_record("running"), fix=fix)
    record = writes[-1][0]
    assert record["stop_reason"] == "unreadable_report"
    check = record["detail"]["check"]
    assert check["status"] == fix.status and "```" not in str(check)
    msg = render_owner_message(_schedule(), RUNS[:1], {}, record)
    assert line in msg and "None" not in msg and "```" not in msg
    assert_no_scorer_text(msg, str(record["detail"]))
    assert not _SCORER_WORDS.search(msg)


async def test_a_check_that_never_started_says_so():
    _stats, _order, writes = await _step(
        _record("running", started_at=_days_ago(3)), fix=_fix("pending", None)
    )
    record = writes[-1][0]
    assert record["detail"] == {"check": {"status": "pending"}}
    msg = render_owner_message(_schedule(), RUNS[:1], {}, record)
    assert "the check never started" in msg and "could not be read" not in msg
    assert_no_scorer_text(msg)
    assert not _SCORER_WORDS.search(msg)


async def test_a_missing_fix_run_has_no_check_line():
    _stats, _order, writes = await _step(_record("running"), fix=None)
    assert writes[-1][0]["detail"] == {}


@pytest.mark.parametrize("kind", ["changed_job", "fixed_elsewhere"])
async def test_a_blocked_check_is_not_applied(kind):
    fix = _fix("completed", _fence(VALID[kind]), outcome="incomplete_blocked")
    review = AsyncMock(return_value=(True, None))
    heart = _heart()
    fixer, heart, _order = _fixer(heart)
    schedule = SimpleNamespace(id=uuid4(), metadata_={FIX_KEY: _record("running")}, active=True, task="Old text")
    heart.schedules.list_with_metadata_key.return_value = [schedule]
    heart.subtasks.get.return_value = fix
    with patch("nous.handlers.job_fixes.review_change", review):
        stats = await fixer.run_sleep()
    assert stats["applied"] == 0 and stats["stopped"] == 1 and review.await_count == 0
    value, task = heart.writes[0]
    assert value["stop_reason"] == "unreadable_report" and task is None
    assert value["detail"]["check"]["outcome"] == "incomplete_blocked"


async def test_a_blocked_check_can_still_need_the_owner():
    fix = _fix("completed", NEEDS, outcome="incomplete_blocked")
    _stats, _order, writes = await _step(_record("running"), fix=fix)
    assert writes[-1][0]["stop_reason"] == "needs_owner"
