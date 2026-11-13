"""F113 Phase A (shadow): the regex claim check of a validated subtask report.

``execute_hardened`` runs ``ClaimVerifier.verify_report`` over the report's
summary and findings, against the session's ledger, after the outcome is
persisted, and the runner writes one ``subtask_report_claim_check`` event.
Nothing else changes: not the outcome, not the score, not the text.

The runner is a real ``AgentRunner`` with a mocked brain (as in
``test_f026_persistence``) whose ``run_turn`` is scripted to drive the report
collector (as in ``test_f061_subtask_executor``); its ledger is the real one.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from nous.api.runner import REPORT_CLAIM_CHECK_EVENT, AgentRunner
from nous.cognitive.claim_verifier import ClaimVerifier
from nous.config import Settings
from nous.handlers.continuation_runner import ContinuationRunner
from nous.handlers.subtask_executor import _report_check_mode, execute_hardened
from nous.observability import task_score_gatherer as gatherer
from nous.storage.models import Event, Subtask

MERGE_CLAIM = {
    "summary": "PR #4 merged. The fix for the login redirect bug is now on the main branch.",
    "confidence": 0.9,
    "findings": ["The redirect now keeps the original path.", "No other route was touched."],
}
NO_CLAIM = {
    "summary": "Found 3 candidates matching the search criteria. Each was reviewed for fitness.",
    "confidence": 0.8,
}
PAYLOAD_KEYS = {"subtask_id", "mode", "verified", "claim_count", "unverified_count", "claims_by_kind"}


def _settings(**overrides) -> Settings:
    fields = dict(
        f026_persistence_enabled=True,
        claim_verification_enabled=True,
        claim_verification_reports_enabled=True,
        claim_verification_mode="shadow",
        execution_ledger_enabled=True,
        execution_ledger_persist_enabled=True,
        subtask_max_attempts=1,
        job_lessons_enabled=False,
        subtask_payload_schema_enabled=False,
    )
    return Settings(**{**fields, **overrides})


def _subtask(**overrides) -> SimpleNamespace:
    base = dict(
        id=uuid.uuid4(), task="merge the fix", frame_type="task", model=None,
        output_format=None, success_criteria=None,
    )
    return SimpleNamespace(**{**base, **overrides})


def _heart(order: list[str] | None = None) -> MagicMock:
    heart = MagicMock()

    async def _persist(*_args, **_kwargs) -> None:
        if order is not None:
            order.append("persist")

    heart.subtasks.complete = AsyncMock(side_effect=_persist)
    heart.subtasks.fail = AsyncMock(side_effect=_persist)
    return heart


def _runner(settings: Settings, payloads: list[dict | None]) -> tuple[AgentRunner, MagicMock]:
    """A real AgentRunner whose run_turn submits ``payloads[i]`` on attempt i (None: no report)."""
    brain = MagicMock()
    brain.emit_event = AsyncMock()
    runner = AgentRunner(MagicMock(), brain, MagicMock(), settings)
    calls = {"i": 0}

    async def _run_turn(**kwargs):
        i = calls["i"]
        calls["i"] += 1
        payload = payloads[i] if i < len(payloads) else None
        if payload is not None:
            _schema, submit = kwargs["extra_tools"]["submit_final_report"]
            await submit(**payload)
        return "Report submitted.", MagicMock(), {"input_tokens": 10, "output_tokens": 5, "tool_calls": 1}

    runner.run_turn = AsyncMock(side_effect=_run_turn)
    return runner, brain


async def _run(subtask, runner, settings, heart=None, session_id=None, **kwargs):
    heart = heart or _heart()
    out = await execute_hardened(
        subtask, session_id or f"subtask-{subtask.id.hex[:8]}",
        runner=runner, heart=heart, settings=settings, **kwargs,
    )
    return out, heart


async def _check_events(brain: MagicMock) -> list[tuple[dict, str | None]]:
    """(data, session_id) of every subtask_report_claim_check the runner handed the brain."""
    await asyncio.sleep(0)  # the F026 path is fire-and-forget (create_task)
    await asyncio.sleep(0)
    return [
        (call.args[1], call.kwargs.get("session_id"))
        for call in brain.emit_event.call_args_list
        if call.args[0] == REPORT_CLAIM_CHECK_EVENT
    ]


async def test_an_unbacked_claim_writes_one_unverified_event_with_labels_only():
    settings = _settings()
    runner, brain = _runner(settings, [MERGE_CLAIM])
    subtask = _subtask()

    (final_text, result), heart = await _run(subtask, runner, settings)

    assert result.ok and final_text == MERGE_CLAIM["summary"]
    assert heart.subtasks.complete.await_args.kwargs["final_outcome"] == "completed"
    [(data, session_id)] = await _check_events(brain)
    assert session_id == f"subtask-{subtask.id.hex[:8]}"
    assert set(data) == PAYLOAD_KEYS
    assert data == {
        "subtask_id": str(subtask.id),
        "mode": "shadow",
        "verified": False,
        "claim_count": 1,
        "unverified_count": 1,
        "claims_by_kind": {"merge": {"none": 1}},
    }
    flat = json.dumps(data)
    for words in ("PR #4", "merged", "redirect", "gh pr"):
        assert words not in flat  # no claim, report or command text


async def test_a_backed_claim_writes_one_verified_event():
    settings = _settings()
    runner, brain = _runner(settings, [MERGE_CLAIM])
    subtask = _subtask()
    session_id = f"subtask-{subtask.id.hex[:8]}"
    runner._get_or_create_ledger(session_id).record("bash", {"command": "gh pr merge 4 --squash"}, "Exit code: 0",
                                                    "success")

    await _run(subtask, runner, settings)

    [(data, _)] = await _check_events(brain)
    assert (data["verified"], data["unverified_count"], data["claims_by_kind"]) == (True, 0, {"merge": {"exact": 1}})
    assert "gh pr" not in json.dumps(data)


async def test_a_report_with_no_claim_still_writes_its_event():
    """The event is written whenever the check ran, so flagged reports have a denominator."""
    settings = _settings()
    runner, brain = _runner(settings, [NO_CLAIM])

    await _run(_subtask(), runner, settings)

    [(data, _)] = await _check_events(brain)
    assert (data["verified"], data["claim_count"], data["claims_by_kind"]) == (True, 0, {})


@pytest.mark.parametrize("flag", [
    "claim_verification_enabled",
    "claim_verification_reports_enabled",
    "execution_ledger_enabled",
    "execution_ledger_persist_enabled",
    "f026_persistence_enabled",  # the F026 event path's own switch
])
async def test_no_event_when_a_switch_is_off(flag):
    settings = _settings(**{flag: False})
    runner, brain = _runner(settings, [MERGE_CLAIM])

    (_, result), heart = await _run(_subtask(), runner, settings)

    assert result.ok
    heart.subtasks.complete.assert_awaited_once()
    assert await _check_events(brain) == []


async def test_a_subtask_in_a_shared_session_is_not_checked():
    settings = _settings()
    runner, brain = _runner(settings, [MERGE_CLAIM])
    runner.session_evidence = MagicMock(wraps=runner.session_evidence)
    shared = "schedule-abcd1234-1"
    subtask = _subtask(metadata_={"session_id": shared, "schedule_id": str(uuid.uuid4())})

    (_, result), _heart_ = await _run(subtask, runner, settings, session_id=shared)

    assert result.ok
    assert await _check_events(brain) == []
    runner.session_evidence.assert_not_called()


@pytest.mark.parametrize("payload, outcome", [
    ({"summary": "blocked", "confidence": 0.0, "incomplete": True,
      "blocked_reason": "I merged nothing: PR #4 merged by nobody"}, "incomplete_blocked"),
    ({"summary": "PR #4 merged.", "confidence": 0.9}, "validation_failed"),  # under the 50-char floor
    (None, "incomplete_no_terminal"),
])
async def test_no_event_for_an_outcome_that_is_not_a_validated_report(payload, outcome):
    settings = _settings()
    runner, brain = _runner(settings, [payload])

    (_, result), _heart_ = await _run(_subtask(), runner, settings)

    assert (result.ok, result.outcome) == (False, outcome)
    assert await _check_events(brain) == []


async def test_no_event_when_the_attempt_errored():
    settings = _settings()
    runner, brain = _runner(settings, [])
    runner.run_turn = AsyncMock(side_effect=RuntimeError("API 503"))

    (_, result), heart = await _run(_subtask(), runner, settings)

    assert result.outcome == "errored"
    heart.subtasks.fail.assert_awaited_once()
    assert await _check_events(brain) == []


@pytest.mark.parametrize("breaks", ["verifier", "evidence", "emit"])
async def test_a_failing_check_changes_nothing_and_warns_once(breaks, monkeypatch, caplog):
    settings = _settings()
    runner, brain = _runner(settings, [MERGE_CLAIM])
    boom = RuntimeError("PR #4 merged secret-text")
    if breaks == "verifier":
        monkeypatch.setattr(ClaimVerifier, "verify_report", MagicMock(side_effect=boom))
    elif breaks == "evidence":
        runner.session_evidence = MagicMock(side_effect=boom)
    else:
        runner.log_report_claim_check = MagicMock(side_effect=boom)
    emit = AsyncMock()
    subtask = _subtask()

    with caplog.at_level(logging.WARNING, logger="nous.handlers.subtask_executor"):
        (final_text, result), heart = await _run(subtask, runner, settings, emit_event=emit)

    assert result.ok and final_text == MERGE_CLAIM["summary"]
    heart.subtasks.complete.assert_awaited_once()
    assert heart.subtasks.complete.await_args.kwargs["final_outcome"] == "completed"
    heart.subtasks.fail.assert_not_called()
    emit.assert_awaited_once()  # the subtask_outcome event still goes out
    warnings = [r for r in caplog.records if r.name == "nous.handlers.subtask_executor"
                and r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "RuntimeError" in warnings[0].getMessage()
    assert "secret-text" not in warnings[0].getMessage() and warnings[0].exc_info is None
    assert await _check_events(brain) == []


async def test_the_check_runs_after_the_persist_and_before_the_outcome_event():
    order: list[str] = []
    settings = _settings()
    runner, brain = _runner(settings, [MERGE_CLAIM])
    real_evidence = runner.session_evidence

    def _evidence(session_id):
        order.append("check")
        return real_evidence(session_id)

    runner.session_evidence = _evidence

    async def _emit(*_args, **_kwargs):
        order.append("outcome_event")

    await _run(_subtask(), runner, settings, heart=_heart(order), emit_event=_emit)

    assert order == ["persist", "check", "outcome_event"]
    assert len(await _check_events(brain)) == 1


async def test_a_cancel_while_the_outcome_is_persisted_runs_no_check():
    """The persist is shielded and the check comes after it: a cancel there leaves the row
    exactly as before (written once, completed) and no check runs."""
    settings = _settings()
    runner, brain = _runner(settings, [MERGE_CLAIM])
    runner.session_evidence = MagicMock(wraps=runner.session_evidence)
    entered, release = asyncio.Event(), asyncio.Event()
    heart = _heart()

    async def _slow_persist(*_args, **_kwargs):
        entered.set()
        await release.wait()

    heart.subtasks.complete = AsyncMock(side_effect=_slow_persist)
    task = asyncio.create_task(execute_hardened(
        _subtask(), "subtask-x", runner=runner, heart=heart, settings=settings,
    ))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    release.set()
    await asyncio.sleep(0)

    heart.subtasks.complete.assert_awaited_once()
    assert heart.subtasks.complete.await_args.kwargs["final_outcome"] == "completed"
    heart.subtasks.fail.assert_not_called()
    runner.session_evidence.assert_not_called()
    assert await _check_events(brain) == []


async def test_a_cancel_during_the_check_leaves_the_persisted_outcome_alone():
    """The check has no await, so a cancel requested inside it lands at the next await after
    it, as one requested right after the persist would; the row was already written once."""
    settings = _settings()
    runner, brain = _runner(settings, [MERGE_CLAIM])
    real_evidence = runner.session_evidence

    def _cancel_then_read(session_id):
        asyncio.current_task().cancel()
        return real_evidence(session_id)

    runner.session_evidence = _cancel_then_read
    heart = _heart()
    emit = AsyncMock()

    task = asyncio.create_task(execute_hardened(
        _subtask(), "subtask-y", runner=runner, heart=heart, settings=settings, emit_event=emit,
    ))
    with pytest.raises(asyncio.CancelledError):
        await task

    heart.subtasks.complete.assert_awaited_once()
    assert heart.subtasks.complete.await_args.kwargs["final_outcome"] == "completed"
    heart.subtasks.fail.assert_not_called()
    assert len(await _check_events(brain)) == 1  # the check itself finished


def test_the_enabling_conditions_match_the_continuation_report_check():
    for enabled, reports, ledger, persist in itertools.product((True, False), repeat=4):
        for mode in ("shadow", "warn", "enforce"):
            settings = SimpleNamespace(
                claim_verification_enabled=enabled,
                claim_verification_reports_enabled=reports,
                execution_ledger_enabled=ledger,
                execution_ledger_persist_enabled=persist,
                claim_verification_mode=mode,
            )
            original = ContinuationRunner._reports_mode(SimpleNamespace(_settings=settings))
            assert _report_check_mode(settings) == original, (enabled, reports, ledger, persist, mode)


async def test_the_scorer_does_not_count_the_report_check_event(db):
    """An unverified subtask_report_claim_check changes no unverified_claims; the per-turn
    f026_claim_verification event beside it still counts."""
    agent = f"test-f113-{uuid.uuid4().hex[:8]}"
    now = datetime.now(UTC)
    sub = Subtask(
        id=uuid.uuid4(), agent_id=agent, task="t", status="completed", final_outcome="completed",
        completed_at=now - timedelta(hours=1), tokens_in=100, tokens_out=50,
    )
    session_id = f"subtask-{sub.id.hex[:8]}"
    async with db.session() as s:
        s.add_all([
            sub,
            Event(agent_id=agent, session_id=session_id, event_type=REPORT_CLAIM_CHECK_EVENT,
                  data={"verified": False, "unverified_count": 1}),
        ])
        await s.commit()

    async def _unverified() -> int:
        async with db.session() as s:
            got = await gatherer.gather_signals(s, agent, since=now - timedelta(days=15))
        return {x.task_id: x for x in got.leaves}[str(sub.id)].unverified_claims

    assert await _unverified() == 0

    async with db.session() as s:
        s.add(Event(agent_id=agent, session_id=session_id, event_type=gatherer.CLAIMS_EVENT,
                    data={"verified": False}))
        await s.commit()
    assert await _unverified() == 1
