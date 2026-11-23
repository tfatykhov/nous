"""F118: the record of a finished task's report the judge reads, built from real rows (real Postgres).

A task is judged when it finished as ``completed`` with a valid report. Its claim is the report (graded ``completed``
by the agent, the summary as the note) at the moment the task was completed, and only what had happened by then is
read: calls that had ended, ledger rows and child tasks as they stood. Calls and ledger rows are the task's own (its
id on the row, or its own session for a row without one), never another task's or another agent's. Rows past a
query's limit are counted and mark the record truncated. The finish checks' verdict on the task, which the judge's
is compared with, is kept in the counts and never in the record.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text

from nous.config import Settings
from nous.judge import evidence
from nous.observability.recording import BoundedWriter
from nous.observability.tool_calls import ToolCallRecord, ToolCallRecorder

pytestmark = pytest.mark.postgres_only

T0 = datetime(2026, 11, 21, 9, 0, tzinfo=UTC)
DONE = T0 + timedelta(minutes=5)
REPORT = {
    "summary": "Fixed the link on the pricing page and deployed the site.",
    "findings": ["pricing.html now links to the new plan"],
    "confidence": 0.9,
    "confidence_reported": True,
}
FLAGGED = {
    "v": 1,
    "mode": "shadow",
    "verdict": "flagged",
    "failed": ["effects_all_failed"],
    "skipped": [],
    "why": None,
}
TABLES = ("nous_system.tool_call_log", "nous_system.execution_ledger", "heart.subtasks")


@pytest_asyncio.fixture
async def agent(db):
    name = f"test-f118-rep-{uuid.uuid4().hex[:8]}"
    yield name
    async with db.session() as session:
        for table in TABLES:
            await session.execute(text(f"DELETE FROM {table} WHERE agent_id LIKE :a"), {"a": f"{name}%"})
        await session.commit()


def own_session(task_id: uuid.UUID) -> str:
    return f"subtask-{task_id.hex[:8]}"


async def _task(
    db,
    agent: str,
    *,
    status: str = "completed",
    final_outcome: str | None = "completed",
    report: Any = REPORT,
    metadata: dict | None = None,
    created_at: datetime = T0,
    completed_at: datetime | None = DONE,
    parent_session_id: str | None = None,
    task: str = "Fix the broken link on the pricing page and deploy the site",
    task_id: uuid.UUID | None = None,
) -> uuid.UUID:
    task_id = task_id or uuid.uuid4()
    async with db.session() as session:
        await session.execute(
            text(
                "INSERT INTO heart.subtasks (id, agent_id, parent_session_id, task, status, final_outcome, "
                "report_jsonb, metadata, success_criteria, created_at, completed_at) VALUES (:id, :a, :p, :t, :st, "
                ":fo, CAST(:r AS jsonb), CAST(:m AS jsonb), 'The live page links to the new plan', :at, :done)"
            ),
            {
                "id": task_id,
                "a": agent,
                "p": parent_session_id,
                "t": task,
                "st": status,
                "fo": final_outcome,
                "r": None if report is None else json.dumps(report),
                "m": json.dumps(metadata if metadata is not None else {"finish_check": FLAGGED}),
                "at": created_at,
                "done": completed_at,
            },
        )
        await session.commit()
    return task_id


def _recorder(db, agent: str) -> tuple[BoundedWriter, ToolCallRecorder]:
    writer = BoundedWriter("test-f118-rep")
    return writer, ToolCallRecorder(writer, db, agent, Settings(_env_file=None))


def _call(
    recorder: ToolCallRecorder,
    session_id: str,
    tool: str,
    at: datetime,
    *,
    subtask_id: uuid.UUID | None,
    result: str = "done",
    is_error: bool = False,
    index: int = 0,
    duration_ms: int | None = None,
) -> None:
    recorder.record(
        ToolCallRecord(
            call_index=index,
            work_kind="subtask",
            tool_name=tool,
            tool_name_called=tool,
            outcome="error" if is_error else "ok",
            session_id=session_id,
            context_kind="subtask",
            subtask_id=subtask_id,
            input_text=json.dumps({"n": index}),
            result_preview="" if is_error else result,
            is_error=is_error,
            error_text=result if is_error else None,
            duration_ms=duration_ms,
            started_at=at,
        )
    )


async def _ledger(
    db,
    agent: str,
    session_id: str,
    tool: str,
    at: datetime,
    *,
    subtask_id: uuid.UUID | None,
    status: str = "success",
    completed_at: datetime | None = None,
) -> None:
    async with db.session() as session:
        await session.execute(
            text(
                "INSERT INTO nous_system.execution_ledger (agent_id, session_id, context_kind, subtask_id, tool_name, "
                "side_effect_type, status, result_summary, created_at, completed_at) VALUES (:a, :s, 'subtask', :sub, "
                ":t, 'write', :st, :r, :at, :done)"
            ),
            {
                "a": agent,
                "s": session_id,
                "sub": subtask_id,
                "t": tool,
                "st": status,
                "r": f"{tool} {status}",
                "at": at,
                "done": completed_at or at + timedelta(seconds=1),
            },
        )
        await session.commit()


async def _build(db, agent: str, task_id: uuid.UUID):
    async with db.session() as session:
        return await evidence.build_report_evidence(session, agent, task_id)


def _sources(built) -> list[str]:
    return [item.get("tool") or item["source"] for item in built.record.evidence]


async def test_the_tasks_own_calls_up_to_its_completion_are_read(db, agent):
    task_id = await _task(db, agent)
    own = own_session(task_id)
    other_task = uuid.uuid4()
    writer, recorder = _recorder(db, agent)
    _call(recorder, own, "read_file", T0 + timedelta(seconds=10), subtask_id=task_id, index=0)
    _call(
        recorder,
        own,
        "bash",
        T0 + timedelta(seconds=20),
        subtask_id=task_id,
        is_error=True,
        result="login failed: the token was refused",
        index=1,
    )
    _call(recorder, own, "write_file", T0 + timedelta(seconds=30), subtask_id=None, index=2)  # untagged, own session
    _call(recorder, "subtask-other", "web_fetch", T0 + timedelta(seconds=40), subtask_id=other_task, index=3)
    _call(recorder, own, "submit_final_report", DONE - timedelta(seconds=1), subtask_id=task_id, index=4)
    _call(recorder, own, "send_email", DONE + timedelta(seconds=5), subtask_id=task_id, index=5)  # after completion
    # Started before the completion but ended after it: not read.
    _call(recorder, own, "bash", DONE - timedelta(seconds=2), subtask_id=task_id, duration_ms=5000, index=6)
    await writer.drain(10)

    built = await _build(db, agent, task_id)
    assert built.claim_at == DONE
    assert (built.claim.grader, built.claim.grade, built.claim.note) == ("agent", "completed", REPORT["summary"])
    assert _sources(built) == ["read_file", "bash", "write_file"]
    assert built.counts == {
        "link": "own_session",
        "tool_calls": 3,
        "ledger_rows": 0,
        "subtasks": 0,
        "bookkeeping": 1,
        "over_limit": 0,
        "dropped": 0,
        "chars": len(evidence.render_record(built.record)),
        "truncated": False,
        "finish_check": "flagged",
    }
    record = built.record
    assert record.kind == "task_report"
    assert record.subject == {
        "task": "Fix the broken link on the pricing page and deploy the site",
        "success_criteria": "The live page links to the new plan",
        "output_format": None,
        "report_findings": "pricing.html now links to the new plan",
        "stated_confidence": 0.9,
    }
    failed_call = record.evidence[1]
    assert failed_call["is_error"] is True
    assert failed_call["outside_data"] == {"result": "login failed: the token was refused"}


async def test_ledger_rows_and_child_tasks_are_read_as_they_stood_at_the_completion(db, agent):
    task_id = await _task(db, agent)
    own = own_session(task_id)
    await _ledger(db, agent, own, "write_file", T0 + timedelta(seconds=5), subtask_id=task_id)
    await _ledger(
        db,
        agent,
        own,
        "send_email",
        T0 + timedelta(seconds=6),
        subtask_id=task_id,
        completed_at=DONE + timedelta(minutes=1),  # finished after the completion
    )
    await _ledger(db, agent, "subtask-elsewhere", "write_file", T0 + timedelta(seconds=7), subtask_id=uuid.uuid4())
    await _ledger(db, agent, own, "write_file", DONE + timedelta(seconds=1), subtask_id=task_id)  # after
    child = await _task(
        db,
        agent,
        parent_session_id=own,
        created_at=T0 + timedelta(seconds=8),
        completed_at=DONE + timedelta(minutes=2),  # finished after the parent reported
    )
    await _task(db, agent, parent_session_id=own, created_at=DONE + timedelta(seconds=3))  # started later

    built = await _build(db, agent, task_id)
    assert (built.counts["ledger_rows"], built.counts["subtasks"], built.counts["tool_calls"]) == (2, 1, 0)
    by_source = [(item["source"], item.get("tool"), item.get("status")) for item in built.record.evidence]
    assert by_source == [
        ("ledger", "write_file", "success"),
        ("ledger", "send_email", evidence.NOT_FINISHED),
        ("spawned_task", None, evidence.NOT_FINISHED),
    ]
    pending = built.record.evidence[1]
    assert pending["outside_data"] == {"result": None}
    spawned = built.record.evidence[2]
    assert spawned["finish_check"] is None and spawned["written_by_author"] == {"report": None}
    # The child is the one created inside the window, read as it stood: not yet finished, no report.
    async with db.session() as session:
        assert await session.scalar(text("SELECT status FROM heart.subtasks WHERE id = :id"), {"id": child}) == (
            "completed"
        )


async def test_a_task_in_a_shared_session_reads_only_its_tagged_rows(db, agent):
    task_id = await _task(db, agent, metadata={"session_id": "schedule-shared", "finish_check": FLAGGED})
    writer, recorder = _recorder(db, agent)
    _call(recorder, "schedule-shared", "bash", T0 + timedelta(seconds=10), subtask_id=task_id, index=0)
    _call(recorder, "schedule-shared", "write_file", T0 + timedelta(seconds=11), subtask_id=None, index=1)
    await writer.drain(10)
    await _ledger(db, agent, "schedule-shared", "write_file", T0 + timedelta(seconds=12), subtask_id=None)

    built = await _build(db, agent, task_id)
    assert built.counts["link"] == "shared_session"
    assert _sources(built) == ["bash"]
    assert (built.counts["tool_calls"], built.counts["ledger_rows"]) == (1, 0)


async def test_a_task_in_a_shared_session_without_tagged_rows_has_nothing_to_judge(db, agent):
    task_id = await _task(db, agent, metadata={"session_id": "schedule-shared", "finish_check": FLAGGED})
    writer, recorder = _recorder(db, agent)
    _call(recorder, "schedule-shared", "bash", T0 + timedelta(seconds=10), subtask_id=None, index=0)
    await writer.drain(10)
    built = await _build(db, agent, task_id)
    assert built.record is None and built.skip_label == "evidence_missing"
    assert built.counts["tool_calls"] == 0 and built.counts["finish_check"] == "flagged"


async def test_the_report_call_alone_is_not_evidence(db, agent):
    task_id = await _task(db, agent)
    writer, recorder = _recorder(db, agent)
    _call(recorder, own_session(task_id), "submit_final_report", T0 + timedelta(seconds=10), subtask_id=task_id)
    await writer.drain(10)
    built = await _build(db, agent, task_id)
    assert built.record is None and built.skip_label == "evidence_missing"
    assert (built.counts["tool_calls"], built.counts["bookkeeping"]) == (0, 1)


async def test_the_record_never_holds_the_tasks_finish_check(db, agent):
    """The finish checks' verdict is what the judge's is compared with: it is kept in the counts, never in the record."""
    task_id = await _task(db, agent)  # finish check: flagged, effects_all_failed
    writer, recorder = _recorder(db, agent)
    _call(recorder, own_session(task_id), "bash", T0 + timedelta(seconds=10), subtask_id=task_id)
    await writer.drain(10)
    built = await _build(db, agent, task_id)
    rendered = evidence.render_record(built.record)
    assert built.counts["finish_check"] == "flagged"
    assert "effects_all_failed" not in rendered and "flagged" not in rendered and "finish_check" not in rendered
    assert [item["source"] for item in json.loads(rendered)["evidence"]] == ["tool_call"]


@pytest.mark.parametrize("verdict", ["made_up", "", 3, None])
async def test_a_finish_check_verdict_a_finish_check_does_not_give_is_held_as_none(db, agent, verdict):
    task_id = await _task(db, agent, metadata={"finish_check": {"verdict": verdict, "failed": []}})
    writer, recorder = _recorder(db, agent)
    _call(recorder, own_session(task_id), "bash", T0 + timedelta(seconds=10), subtask_id=task_id)
    await writer.drain(10)
    assert (await _build(db, agent, task_id)).counts["finish_check"] is None


@pytest.mark.parametrize("verdict", ["clean", "flagged", "not_checked"])
async def test_each_verdict_a_finish_check_gives_is_held(db, agent, verdict):
    task_id = await _task(db, agent, metadata={"finish_check": {"verdict": verdict, "failed": []}})
    writer, recorder = _recorder(db, agent)
    _call(recorder, own_session(task_id), "bash", T0 + timedelta(seconds=10), subtask_id=task_id)
    await writer.drain(10)
    assert (await _build(db, agent, task_id)).counts["finish_check"] == verdict


async def test_without_a_finish_check_the_counts_hold_none(db, agent):
    task_id = await _task(db, agent, metadata={})
    writer, recorder = _recorder(db, agent)
    _call(recorder, own_session(task_id), "bash", T0 + timedelta(seconds=10), subtask_id=task_id)
    await writer.drain(10)
    built = await _build(db, agent, task_id)
    assert _sources(built) == ["bash"] and built.counts["finish_check"] is None


@pytest.mark.parametrize(
    "row",
    [
        {"status": "failed", "final_outcome": "validation_failed"},
        {"status": "cancelled", "final_outcome": "cancelled"},
        {"status": "completed", "final_outcome": "incomplete_blocked"},
        {"status": "running", "final_outcome": None, "completed_at": None},
        {"status": "completed", "final_outcome": None},  # a task run before reports were validated
        {"report": {"guard_skipped": True, "guard": {"exit_code": 0}}},  # a scheduled run its guard skipped
        {"report": {"summary": ""}},
        {"report": {"summary": " \n\t\r\x0b\x0c "}},  # blank: the characters the sweep's query trims too
        {"report": None},
        {"completed_at": None},
    ],
)
async def test_a_task_without_a_completed_valid_report_is_not_judged(db, agent, row):
    task_id = await _task(db, agent, **row)
    writer, recorder = _recorder(db, agent)
    _call(recorder, own_session(task_id), "bash", T0 + timedelta(seconds=10), subtask_id=task_id)
    await writer.drain(10)
    assert await _build(db, agent, task_id) is None


async def test_another_agents_task_and_rows_are_never_read(db, agent):
    task_id = await _task(db, agent)
    writer, recorder = _recorder(db, f"{agent}-other")
    _call(recorder, own_session(task_id), "bash", T0 + timedelta(seconds=10), subtask_id=task_id)
    await writer.drain(10)
    await _ledger(db, f"{agent}-other", own_session(task_id), "write_file", T0, subtask_id=task_id)
    built = await _build(db, agent, task_id)
    assert built.record is None and built.counts["tool_calls"] == 0 and built.counts["ledger_rows"] == 0
    assert await _build(db, f"{agent}-other", task_id) is None


async def test_rows_past_a_limit_are_counted_and_mark_the_record_truncated(db, agent, monkeypatch):
    monkeypatch.setattr(evidence, "MAX_CALLS", 2)
    task_id = await _task(db, agent)
    writer, recorder = _recorder(db, agent)
    for i in range(5):
        _call(recorder, own_session(task_id), "read_file", T0 + timedelta(seconds=10 + i), subtask_id=task_id, index=i)
    await writer.drain(10)
    built = await _build(db, agent, task_id)
    assert built.counts["tool_calls"] == 5 and built.counts["over_limit"] == 3
    assert built.record.evidence_truncated and built.record.evidence_dropped == 3
    assert built.counts["dropped"] == 3 and built.counts["truncated"] is True
    assert _sources(built) == ["read_file", "read_file"]


async def test_a_call_that_started_after_the_completion_is_never_read_whatever_its_duration(db, agent):
    task_id = await _task(db, agent)
    writer, recorder = _recorder(db, agent)
    _call(recorder, own_session(task_id), "bash", T0 + timedelta(seconds=10), subtask_id=task_id, index=0)
    # A row whose start is after the completion and whose duration would bring its end back before it.
    _call(
        recorder,
        own_session(task_id),
        "send_email",
        DONE + timedelta(seconds=5),
        subtask_id=task_id,
        duration_ms=-60_000,
        index=1,
    )
    await writer.drain(10)
    built = await _build(db, agent, task_id)
    assert _sources(built) == ["bash"] and built.counts["tool_calls"] == 1


async def test_the_record_is_the_same_whenever_it_is_read(db, agent):
    task_id = await _task(db, agent)
    own = own_session(task_id)
    writer, recorder = _recorder(db, agent)
    _call(recorder, own, "bash", T0 + timedelta(seconds=10), subtask_id=task_id)
    await writer.drain(10)
    first = await _build(db, agent, task_id)
    _call(recorder, own, "write_file", DONE + timedelta(minutes=10), subtask_id=task_id, index=1)
    await writer.drain(10)
    await _ledger(db, agent, own, "send_email", DONE + timedelta(minutes=11), subtask_id=task_id)
    await _task(db, agent, parent_session_id=own, created_at=DONE + timedelta(minutes=12))
    second = await _build(db, agent, task_id)
    assert evidence.render_record(first.record) == evidence.render_record(second.record)
    assert first.counts == second.counts


async def test_a_secret_in_the_report_or_a_result_is_masked(db, agent):
    secret = "sk-ant-api03-" + "K" * 40
    task_id = await _task(db, agent, report={**REPORT, "summary": f"Deployed with {secret} as the key."})
    writer, recorder = _recorder(db, agent)
    _call(recorder, own_session(task_id), "bash", T0 + timedelta(seconds=10), subtask_id=task_id, result=secret)
    await writer.drain(10)
    built = await _build(db, agent, task_id)
    assert secret not in evidence.render_record(built.record)


async def test_reading_the_record_changes_no_row(db, agent):
    task_id = await _task(db, agent)
    writer, recorder = _recorder(db, agent)
    _call(recorder, own_session(task_id), "bash", T0 + timedelta(seconds=10), subtask_id=task_id)
    await writer.drain(10)

    async def snapshot() -> list:
        async with db.session() as session:
            rows = []
            for table in TABLES:
                result = await session.execute(
                    text(f"SELECT to_jsonb(t) FROM {table} t WHERE agent_id = :a"), {"a": agent}
                )
                rows.append(sorted(json.dumps(r[0], sort_keys=True) for r in result))
            return rows

    before = await snapshot()
    await _build(db, agent, task_id)
    assert await snapshot() == before
