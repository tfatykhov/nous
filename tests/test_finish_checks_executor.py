"""F113 fixed finish checks in ``execute_hardened``: the shadow record, and that ``off`` changes nothing.

Built like ``test_f061_subtask_executor``: a scripted ``run_turn`` drives the report collector, the
heart is a recording mock, and the runner's ``session_evidence`` is a stub returning calls recorded
on a real ``ExecutionLedger``.

Parity: a run is recorded whole (every ``run_turn`` call's arguments, every ``complete`` / ``fail``
call, the outcome event, the notification and the returned value). With the setting absent and with
``off`` the recordings are equal. Their ``complete`` / ``fail`` calls and outcome events (``golden_view``)
equal ``tests/fixtures/finish_checks_parity_golden.json``, captured from the same scenarios on the
commit before the checks existed (bf260047). With ``shadow`` the only change is one ``metadata_patch``
keyword on ``complete``.

Regenerating the golden (only when a change to the persist or the event is intended): on the commit
that should be the reference, write ``{"generated_at_commit": <sha>, "scenarios": {name:
golden_view(await record(SCENARIOS[name], _settings())) for name in SCENARIOS}}`` with
``json.dumps(..., indent=1, sort_keys=True)``.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import logging
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from nous.api.execution_context import ExecutionContext
from nous.cognitive import finish_checks
from nous.cognitive.claim_verifier import ledger_evidence
from nous.cognitive.execution_ledger import ExecutionLedger
from nous.handlers.subtask_executor import execute_hardened
from nous.heart.subtask_validator import ValidationResult

GOLDEN = Path(__file__).parent / "fixtures" / "finish_checks_parity_golden.json"
SUBTASK_ID = uuid.UUID("00000000-0000-4000-8000-0000000000f1")
OWN_SESSION = f"subtask-{SUBTASK_ID.hex[:8]}"

VALID = {
    "summary": "Found 3 candidates matching the search criteria. Each was reviewed for fitness.",
    "confidence": 0.9,
    "findings": ["a", "b", "c"],
}
PLACEHOLDER = {"summary": "TODO", "confidence": 0.5}
BLOCKED = {
    "summary": "Could not reach the data source; the site refused every request made to it.",
    "incomplete": True,
    "blocked_reason": "the site refused the requests",
}
SCENARIOS: dict[str, list | str] = {
    "valid_first": [VALID],
    "empty_then_valid": [None, VALID],
    "placeholder_then_valid": [PLACEHOLDER, VALID],
    "incomplete_blocked": [BLOCKED],
    "api_error": "raise",
    "two_empties": [None, None],
}
NAMES_A_MISSING_FILE = {
    "summary": "I created /ws/reports/summary.md with the comparison of the three providers.",
    "confidence": 0.8,
    "findings": ["Provider B is the cheapest."],
}
FLAGGED_RECORD = {
    "v": 1,
    "mode": "shadow",
    "verdict": "flagged",
    "failed": ["named_file_missing"],
    "skipped": [],
    "why": None,
}


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------


def _plain(value, subtask):
    if value is subtask:
        return "<subtask>"
    if isinstance(value, ValidationResult):
        return {
            "ok": value.ok,
            "outcome": value.outcome,
            "reason": _plain(value.reason, subtask),
            "report": value.report.model_dump(mode="json") if value.report is not None else None,
        }
    if isinstance(value, ExecutionContext):
        return _plain(dataclasses.asdict(value), subtask)
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, str):
        if len(value) <= 120:
            return value
        return f"sha256:{hashlib.sha256(value.encode()).hexdigest()[:16]}:{len(value)}"
    if isinstance(value, dict):
        return {str(k): _plain(v, subtask) for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))}
    if isinstance(value, (list, tuple)):
        return [_plain(v, subtask) for v in value]
    if callable(value):
        return "<callable>"
    return value


def _schema_digest(extra_tools):
    return {
        name: "sha256:" + hashlib.sha256(json.dumps(schema, sort_keys=True).encode()).hexdigest()[:16]
        for name, (schema, _executor) in extra_tools.items()
    }


def _settings(**fields):
    base = dict(
        agent_id="agent-test",
        background_model="claude-haiku-4-5-20251001",
        subtask_max_attempts=2,
        subtask_report_min_summary_chars=50,
        subtask_tool_call_limit=20,
        subtask_force_tool_on_penultimate=True,
        workspace_dir="/ws",
        execution_ledger_enabled=True,
    )
    base.update(fields)
    return SimpleNamespace(**base)


def _runner(script, evidence=None):
    runner = MagicMock()
    calls = {"i": 0}

    async def _run_turn(**kwargs):
        i = calls["i"]
        calls["i"] += 1
        if script == "raise":
            raise RuntimeError("the model API answered 529")
        payload = script[i] if i < len(script) else None
        if payload is not None:
            _schema, submit = kwargs["extra_tools"]["submit_final_report"]
            await submit(**payload)
        return "Report submitted.", MagicMock(), {"input_tokens": 100, "output_tokens": 50, "tool_calls": 1}

    runner.run_turn = AsyncMock(side_effect=_run_turn)
    if evidence is not None:
        runner.session_evidence = MagicMock(return_value=evidence)
    return runner


def _subtask(**fields):
    base = dict(
        id=SUBTASK_ID, task="research X", frame_type="research", model=None, output_format=None, success_criteria=None
    )
    base.update(fields)
    return SimpleNamespace(**base)


async def record(script, settings, runner=None, subtask=None, session_id=OWN_SESSION):
    runner = runner or _runner(script)
    subtask = subtask or _subtask()
    heart = MagicMock()
    heart.subtasks.complete = AsyncMock()
    heart.subtasks.fail = AsyncMock()
    emit_event = AsyncMock()
    notify = AsyncMock()
    final_text, result = await execute_hardened(
        subtask,
        session_id,
        runner=runner,
        heart=heart,
        settings=settings,
        emit_event=emit_event,
        notify_telegram=notify,
    )
    run_turns = []
    for call in runner.run_turn.await_args_list:
        kwargs = dict(call.kwargs)
        kwargs["extra_tools"] = _schema_digest(kwargs["extra_tools"])
        run_turns.append(_plain(kwargs, subtask))
    events = []
    for call in emit_event.await_args_list:
        kwargs = {k: v for k, v in call.kwargs.items() if k != "duration_ms"}
        events.append(_plain([list(call.args), kwargs], subtask))
    return {
        "returned": _plain([final_text, result], subtask),
        "run_turn": run_turns,
        "complete": [_plain([list(c.args), c.kwargs], subtask) for c in heart.subtasks.complete.await_args_list],
        "fail": [_plain([list(c.args), c.kwargs], subtask) for c in heart.subtasks.fail.await_args_list],
        "events": events,
        "notify": [_plain([list(c.args), c.kwargs], subtask) for c in notify.await_args_list],
    }


def golden_view(recording):
    """The part of a recording the base-commit golden pins: the ``complete`` / ``fail`` calls and the events."""
    return json.loads(json.dumps({key: recording[key] for key in ("complete", "fail", "events")}))


def _evidence(*calls: tuple):
    ledger = ExecutionLedger(OWN_SESSION)
    for tool, tool_input, status in calls:
        ledger.record(tool, tool_input, "done\nExit code: 0" if tool == "bash" else "done", status)
    return ledger_evidence(ledger.actions)


WROTE_SUMMARY = ("write_file", {"path": "/ws/reports/summary.md", "content": "x"}, "success")


@pytest.fixture
def nothing_exists(monkeypatch):
    asked: list[str] = []

    def exists(path):
        asked.append(path)
        return False

    monkeypatch.setattr(finish_checks, "fs_exists", exists)
    return asked


def _raise(*_args, **_kwargs):
    raise AssertionError("must not run with the setting off")


# ---------------------------------------------------------------------------
# Default-off parity
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", list(SCENARIOS))
async def test_absent_and_off_record_the_same_run_as_the_base_commit(name):
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    absent = await record(SCENARIOS[name], _settings())
    off = await record(SCENARIOS[name], _settings(finish_checks_mode="off"))
    assert absent == off
    assert golden_view(off) == golden["scenarios"][name]


@pytest.mark.parametrize("name", list(SCENARIOS))
async def test_off_runs_none_of_the_checks(name, monkeypatch):
    monkeypatch.setattr(finish_checks, "evaluate", _raise)
    monkeypatch.setattr(finish_checks, "fs_exists", _raise)
    runner = _runner(SCENARIOS[name])
    runner.session_evidence = MagicMock(side_effect=_raise)
    off = await record(SCENARIOS[name], _settings(finish_checks_mode="off"), runner=runner)
    runner.session_evidence.assert_not_called()
    assert golden_view(off) == json.loads(GOLDEN.read_text(encoding="utf-8"))["scenarios"][name]


async def test_the_same_stubs_are_reached_in_shadow(monkeypatch, caplog):
    """The stubs of the test above are the ones the executor calls: in shadow they make the record check_error."""
    monkeypatch.setattr(finish_checks, "evaluate", _raise)
    runner = _runner([VALID], evidence=_evidence(WROTE_SUMMARY))
    with caplog.at_level(logging.WARNING):
        shadow = await record([VALID], _settings(finish_checks_mode="shadow"), runner=runner)
    [[_args, kwargs]] = shadow["complete"]
    assert kwargs["metadata_patch"]["finish_check"]["why"] == "check_error"
    assert "AssertionError" in caplog.text

    runner = _runner([VALID])
    runner.session_evidence = MagicMock(side_effect=_raise)
    shadow = await record([VALID], _settings(finish_checks_mode="shadow"), runner=runner)
    [[_args, kwargs]] = shadow["complete"]
    assert kwargs["metadata_patch"]["finish_check"]["why"] == "check_error"


async def test_shadow_changes_nothing_but_the_record(nothing_exists):
    evidence = _evidence(WROTE_SUMMARY)
    off = await record([NAMES_A_MISSING_FILE], _settings(), runner=_runner([NAMES_A_MISSING_FILE], evidence))
    shadow = await record(
        [NAMES_A_MISSING_FILE],
        _settings(finish_checks_mode="shadow"),
        runner=_runner([NAMES_A_MISSING_FILE], evidence),
    )
    [[_args, kwargs]] = shadow["complete"]
    assert kwargs.pop("metadata_patch") == {"finish_check": FLAGGED_RECORD}
    assert shadow == off
    assert nothing_exists == ["/ws/reports/summary.md"]


# ---------------------------------------------------------------------------
# The shadow record
# ---------------------------------------------------------------------------


async def _shadow_patch(payloads, *, evidence, settings=None, subtask=None, session_id=OWN_SESSION):
    runner = _runner(payloads, evidence)
    rec = await record(
        payloads,
        settings or _settings(finish_checks_mode="shadow"),
        runner=runner,
        subtask=subtask,
        session_id=session_id,
    )
    return rec, runner


async def test_a_report_naming_a_missing_file_is_flagged(nothing_exists):
    rec, runner = await _shadow_patch([NAMES_A_MISSING_FILE], evidence=_evidence(WROTE_SUMMARY))
    [[args, kwargs]] = rec["complete"]
    assert kwargs["metadata_patch"] == {"finish_check": FLAGGED_RECORD}
    assert kwargs["final_outcome"] == "completed"
    assert args[1] == NAMES_A_MISSING_FILE["summary"]
    runner.session_evidence.assert_called_once_with(OWN_SESSION)


async def test_failed_effects_and_a_silent_report_are_flagged(nothing_exists):
    evidence = _evidence(("write_file", {"path": "/ws/a.md", "content": "x"}, "error"))
    rec, _runner_ = await _shadow_patch([VALID], evidence=evidence)
    [[_args, kwargs]] = rec["complete"]
    assert kwargs["metadata_patch"]["finish_check"]["failed"] == ["effects_all_failed"]


async def test_the_record_names_a_skipped_check(nothing_exists):
    evidence = _evidence(WROTE_SUMMARY, ("run_python", {"code": "print(1)"}, "success"))
    rec, _runner_ = await _shadow_patch([NAMES_A_MISSING_FILE], evidence=evidence)
    [[_args, kwargs]] = rec["complete"]
    assert kwargs["metadata_patch"] == {
        "finish_check": {
            "v": 1,
            "mode": "shadow",
            "verdict": "clean",
            "failed": [],
            "skipped": ["named_file_missing"],
            "why": None,
        }
    }
    assert nothing_exists == []


async def test_a_clean_report(nothing_exists):
    rec, _runner_ = await _shadow_patch([VALID], evidence=_evidence(WROTE_SUMMARY))
    [[_args, kwargs]] = rec["complete"]
    assert kwargs["metadata_patch"] == {
        "finish_check": {"v": 1, "mode": "shadow", "verdict": "clean", "failed": [], "skipped": [], "why": None}
    }


async def test_a_retry_is_checked_once_with_every_attempts_calls(nothing_exists):
    rec, runner = await _shadow_patch([None, NAMES_A_MISSING_FILE], evidence=_evidence(WROTE_SUMMARY))
    [[_args, kwargs]] = rec["complete"]
    assert kwargs["attempts"] == 2
    assert kwargs["metadata_patch"] == {"finish_check": FLAGGED_RECORD}
    runner.session_evidence.assert_called_once_with(OWN_SESSION)


@pytest.mark.parametrize(
    ("subtask_fields", "session_id"),
    [
        ({"metadata_": {"session_id": "schedule-abc"}}, OWN_SESSION),
        ({}, "telegram-42"),
    ],
)
async def test_a_shared_session_is_not_checked(subtask_fields, session_id, nothing_exists):
    rec, runner = await _shadow_patch(
        [NAMES_A_MISSING_FILE],
        evidence=_evidence(WROTE_SUMMARY),
        subtask=_subtask(**subtask_fields),
        session_id=session_id,
    )
    [[_args, kwargs]] = rec["complete"]
    assert kwargs["metadata_patch"] == {
        "finish_check": {
            "v": 1,
            "mode": "shadow",
            "verdict": "not_checked",
            "failed": [],
            "skipped": [],
            "why": "shared_session",
        }
    }
    runner.session_evidence.assert_not_called()


async def test_no_recorded_calls_is_not_checked(nothing_exists):
    rec, _runner_ = await _shadow_patch([NAMES_A_MISSING_FILE], evidence=[])
    [[_args, kwargs]] = rec["complete"]
    assert kwargs["metadata_patch"]["finish_check"] == {
        "v": 1,
        "mode": "shadow",
        "verdict": "not_checked",
        "failed": [],
        "skipped": [],
        "why": "no_calls",
    }


async def test_the_ledger_off_is_not_checked(nothing_exists):
    settings = _settings(finish_checks_mode="shadow", execution_ledger_enabled=False)
    rec, runner = await _shadow_patch([NAMES_A_MISSING_FILE], evidence=_evidence(WROTE_SUMMARY), settings=settings)
    [[_args, kwargs]] = rec["complete"]
    assert kwargs["metadata_patch"]["finish_check"]["why"] == "no_calls"
    runner.session_evidence.assert_not_called()


@pytest.mark.parametrize("name", ["incomplete_blocked", "two_empties", "placeholder_only", "api_error"])
async def test_no_record_without_a_valid_report(name, nothing_exists):
    script = [PLACEHOLDER, PLACEHOLDER] if name == "placeholder_only" else SCENARIOS[name]
    rec, runner = await _shadow_patch(script, evidence=_evidence(WROTE_SUMMARY))
    calls = rec["complete"] + rec["fail"]
    assert len(calls) == 1
    assert all("metadata_patch" not in kwargs for _args, kwargs in calls)
    runner.session_evidence.assert_not_called()


async def test_an_error_in_the_checks_still_completes_the_task(monkeypatch, caplog):
    def boom(*_args, **_kwargs):
        raise ValueError("text of the report /ws/secret.md")

    monkeypatch.setattr(finish_checks, "evaluate", boom)
    with caplog.at_level(logging.WARNING):
        rec, _runner_ = await _shadow_patch([VALID], evidence=_evidence(WROTE_SUMMARY))
    assert rec["returned"][1]["ok"] is True
    [[_args, kwargs]] = rec["complete"]
    assert kwargs["final_outcome"] == "completed"
    assert kwargs["metadata_patch"]["finish_check"]["verdict"] == "not_checked"
    assert kwargs["metadata_patch"]["finish_check"]["why"] == "check_error"
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING and "F113" in r.getMessage()]
    assert len(warnings) == 1
    assert "ValueError" in warnings[0].getMessage()
    assert "secret" not in caplog.text


async def test_cancellation_persists_nothing_in_shadow():
    runner = MagicMock()
    runner.run_turn = AsyncMock(side_effect=asyncio.CancelledError())
    runner.session_evidence = MagicMock(side_effect=_raise)
    heart = MagicMock()
    heart.subtasks.complete = AsyncMock()
    heart.subtasks.fail = AsyncMock()
    emit = AsyncMock()
    with pytest.raises(asyncio.CancelledError):
        await execute_hardened(
            _subtask(),
            OWN_SESSION,
            runner=runner,
            heart=heart,
            settings=_settings(finish_checks_mode="shadow"),
            emit_event=emit,
        )
    heart.subtasks.complete.assert_not_called()
    heart.subtasks.fail.assert_not_called()
    emit.assert_not_called()
    runner.session_evidence.assert_not_called()


async def test_the_checks_add_no_thread_and_no_sleep(monkeypatch, nothing_exists):
    async def no_thread(*_args, **_kwargs):
        raise AssertionError("the finish path must not use a thread")

    async def no_sleep(*_args, **_kwargs):
        raise AssertionError("the finish path must not sleep")

    monkeypatch.setattr(asyncio, "to_thread", no_thread)
    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    rec, _runner_ = await _shadow_patch([NAMES_A_MISSING_FILE], evidence=_evidence(WROTE_SUMMARY))
    [[_args, kwargs]] = rec["complete"]
    assert kwargs["metadata_patch"] == {"finish_check": FLAGGED_RECORD}
