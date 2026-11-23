"""F118: the decision evidence the judge reads, built from real rows (real Postgres).

The link to a session is the decision's ``session_id``, else the one ``record_decision`` call whose reply names the
decision's id (written here through the real tool and the real recorder). The calls, ledger rows and spawned tasks of
that session from the decision's creation to the resolve are read, never later ones and never another agent's.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import text

from nous.brain.brain import Brain
from nous.config import Settings
from nous.judge import evidence
from nous.judge.slot import Claim
from nous.observability.recording import BoundedWriter
from nous.observability.tool_calls import ToolCallRecord, ToolCallRecorder

pytestmark = pytest.mark.postgres_only

T0 = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
CLAIM = Claim(grader="agent", grade="success", note="The nightly job ran after the change")
TABLES = (
    ("brain.decisions", "agent_id"),
    ("nous_system.tool_call_log", "agent_id"),
    ("nous_system.execution_ledger", "agent_id"),
    ("heart.subtasks", "agent_id"),
)


@pytest_asyncio.fixture
async def agent(db):
    name = f"test-f118-ev-{uuid.uuid4().hex[:8]}"
    yield name
    async with db.session() as session:
        for table, column in TABLES:
            await session.execute(text(f"DELETE FROM {table} WHERE {column} LIKE :a"), {"a": f"{name}%"})
        await session.commit()


async def _decision(db, agent: str, *, session_id: str | None = None, created_at: datetime = T0) -> uuid.UUID:
    decision_id = uuid.uuid4()
    async with db.session() as session:
        await session.execute(
            text(
                "INSERT INTO brain.decisions (id, agent_id, description, context, pattern, confidence, "
                "confidence_raw, category, stakes, session_id, created_at) VALUES (:id, :a, :d, :c, NULL, 0.62, "
                "0.8, 'tooling', 'medium', :s, :t)"
            ),
            {
                "id": decision_id,
                "a": agent,
                "d": "Move the backup to 03:00",
                "c": "It collides with the deploy",
                "s": session_id,
                "t": created_at,
            },
        )
        await session.commit()
    return decision_id


def _recorder(db, agent: str) -> tuple[BoundedWriter, ToolCallRecorder]:
    writer = BoundedWriter("test-f118")
    return writer, ToolCallRecorder(writer, db, agent, Settings(_env_file=None))


def _call(
    recorder: ToolCallRecorder,
    session_id: str,
    tool: str,
    at: datetime,
    *,
    result: str = "done",
    is_error: bool = False,
    index: int = 0,
    tool_input: dict | None = None,
    duration_ms: int | None = None,
) -> None:
    recorder.record(
        ToolCallRecord(
            call_index=index,
            work_kind="conversation",
            tool_name=tool,
            tool_name_called=tool,
            outcome="error" if is_error else "ok",
            session_id=session_id,
            context_kind="interactive",
            input_text=json.dumps(tool_input or {"n": index}),
            result_preview="" if is_error else result,
            is_error=is_error,
            error_text=result if is_error else None,
            duration_ms=duration_ms,
            started_at=at,
        )
    )


_ONE_SECOND = timedelta(seconds=1)


async def _ledger(
    db,
    agent: str,
    session_id: str,
    tool: str,
    at: datetime,
    status: str = "success",
    completed_at: datetime | None | object = _ONE_SECOND,
) -> uuid.UUID:
    """A ledger row created at ``at``; completed a second later unless ``completed_at`` says otherwise (None: still
    pending)."""
    done = at + _ONE_SECOND if completed_at is _ONE_SECOND else completed_at
    row_id = uuid.uuid4()
    async with db.session() as session:
        await session.execute(
            text(
                "INSERT INTO nous_system.execution_ledger (id, agent_id, session_id, context_kind, tool_name, "
                "side_effect_type, status, result_summary, created_at, completed_at) VALUES (:id, :a, :s, "
                "'interactive', :t, 'write', :st, :r, :at, :done)"
            ),
            {
                "id": row_id,
                "a": agent,
                "s": session_id,
                "t": tool,
                "st": status,
                "r": None if done is None else f"{tool} {status}",
                "at": at,
                "done": done,
            },
        )
        await session.commit()
    return row_id


async def _subtask(
    db, agent: str, session_id: str, at: datetime, summary: str, completed_at: datetime | None | object = _ONE_SECOND
) -> uuid.UUID:
    """A task created at ``at`` and finished a second later with ``summary``, unless ``completed_at`` says otherwise
    (None: still running, no report yet)."""
    done = at + _ONE_SECOND if completed_at is _ONE_SECOND else completed_at
    row_id = uuid.uuid4()
    async with db.session() as session:
        await session.execute(
            text(
                "INSERT INTO heart.subtasks (id, agent_id, parent_session_id, task, status, final_outcome, "
                "report_jsonb, metadata, created_at, completed_at) VALUES (:id, :a, :s, 'check the job', :st, :fo, "
                "CAST(:r AS jsonb), CAST(:m AS jsonb), :at, :done)"
            ),
            {
                "id": row_id,
                "a": agent,
                "s": session_id,
                "st": "running" if done is None else "completed",
                "fo": None if done is None else "completed",
                "r": None if done is None else json.dumps({"summary": summary, "findings": []}),
                "m": json.dumps({} if done is None else {"finish_check": {"verdict": "clean", "failed": []}}),
                "at": at,
                "done": done,
            },
        )
        await session.commit()
    return row_id


async def _build(db, agent: str, decision_id: uuid.UUID, resolved_at: datetime):
    async with db.session() as session:
        return await evidence.build_decision_evidence(session, agent, decision_id, CLAIM, resolved_at)


def _tools(built) -> list[str]:
    return [item.get("tool") or item["source"] for item in built.record.evidence]


async def test_the_session_of_the_decision_links_its_calls_and_the_window_ends_at_the_resolve(db, agent):
    decision_id = await _decision(db, agent, session_id="sess-a")
    writer, recorder = _recorder(db, agent)
    _call(recorder, "sess-a", "read_file", T0 - timedelta(seconds=5), index=0)  # before the decision
    _call(recorder, "sess-a", "bash", T0 + timedelta(seconds=1), result="backup moved", index=1)
    _call(recorder, "sess-a", "write_file", T0 + timedelta(seconds=2), index=2)
    _call(recorder, "sess-other", "web_fetch", T0 + timedelta(seconds=3), index=3)  # another session
    _call(recorder, "sess-a", "send_email", T0 + timedelta(seconds=20), index=4)  # after the resolve
    await writer.drain(10)

    built = await _build(db, agent, decision_id, T0 + timedelta(seconds=10))
    assert built.counts["link"] == "session_id"
    assert _tools(built) == ["bash", "write_file"]
    assert built.counts["tool_calls"] == 2 and built.counts["ledger_rows"] == 0 and built.counts["subtasks"] == 0
    record = built.record
    assert record.subject["description"] == "Move the backup to 03:00"
    assert record.subject["stated_confidence"] == 0.8  # the raw confidence the author stated
    assert record.claim == CLAIM
    assert built.counts["chars"] == len(evidence.render_record(record))
    assert set(built.counts) == {
        "link",
        "tool_calls",
        "ledger_rows",
        "subtasks",
        "bookkeeping",
        "over_limit",
        "dropped",
        "chars",
        "truncated",
    }


async def test_the_record_decision_call_links_a_decision_without_a_session(db, agent, mock_embeddings):
    from nous.api.tools import create_nous_tools

    settings = Settings(_env_file=None, agent_id=agent)
    assert settings.decision_session_id_enabled is False
    brain = Brain(database=db, settings=settings, embedding_provider=mock_embeddings)
    try:
        started = datetime.now(UTC)
        reply = await create_nous_tools(brain, None, settings)["record_decision"](
            description="Pin the judge's model to the background model",
            confidence=0.7,
            category="tooling",
            stakes="low",
            _session_id="sess-b",
        )
    finally:
        await brain.close()
    text_reply = reply["content"][0]["text"]
    decision_id = uuid.UUID(re.search(r"ID: ([0-9a-f-]{36})", text_reply).group(1))
    async with db.session() as session:
        created_at, session_id = (
            await session.execute(
                text("SELECT created_at, session_id FROM brain.decisions WHERE id = :id"), {"id": decision_id}
            )
        ).one()
    assert session_id is None

    writer, recorder = _recorder(db, agent)
    # The reply as the runner hands it to the recorder, padded past the 500-character preview so the row is cut.
    _call(recorder, "sess-b", "record_decision", started, result=text_reply + "\n" + "x" * 3000, index=0)
    _call(recorder, "sess-b", "bash", created_at + timedelta(seconds=1), result="pinned", index=1)
    await writer.drain(10)
    async with db.session() as session:
        preview = await session.scalar(
            text(
                "SELECT result_preview FROM nous_system.tool_call_log WHERE agent_id = :a AND tool_name = "
                "'record_decision'"
            ),
            {"a": agent},
        )
    assert len(preview) < len(text_reply) + 3000 and f"ID: {decision_id}" in preview

    built = await _build(db, agent, decision_id, created_at + timedelta(seconds=5))
    assert built.counts["link"] == "record_call"
    assert _tools(built) == ["bash"]
    assert built.counts["bookkeeping"] == 0  # the record_decision call is before the window


async def test_two_record_decision_calls_naming_the_decision_give_no_link(db, agent):
    decision_id = await _decision(db, agent)
    writer, recorder = _recorder(db, agent)
    for i, session_id in enumerate(("sess-c1", "sess-c2")):
        _call(
            recorder,
            session_id,
            "record_decision",
            T0 - timedelta(seconds=2),
            result=f"Decision recorded successfully.\nID: {decision_id}\n",
            index=i,
        )
        _call(recorder, session_id, "bash", T0 + timedelta(seconds=1), index=i + 10)
    await writer.drain(10)
    built = await _build(db, agent, decision_id, T0 + timedelta(seconds=10))
    assert built.counts["link"] == "none" and built.record is None


async def test_a_record_decision_call_too_long_before_the_decision_gives_no_link(db, agent):
    decision_id = await _decision(db, agent)
    writer, recorder = _recorder(db, agent)
    _call(recorder, "sess-d", "record_decision", T0 - timedelta(minutes=3), result=f"ID: {decision_id}")
    _call(recorder, "sess-d", "bash", T0 + timedelta(seconds=1), index=1)
    await writer.drain(10)
    built = await _build(db, agent, decision_id, T0 + timedelta(seconds=10))
    assert built.counts["link"] == "none" and built.record is None


async def test_a_decision_with_no_link_is_skipped(db, agent):
    decision_id = await _decision(db, agent)
    built = await _build(db, agent, decision_id, T0 + timedelta(seconds=10))
    assert built.record is None
    assert built.counts == {
        "link": "none",
        "tool_calls": 0,
        "ledger_rows": 0,
        "subtasks": 0,
        "bookkeeping": 0,
        "over_limit": 0,
        "dropped": 0,
        "chars": 0,
        "truncated": False,
    }


async def test_bookkeeping_calls_are_dropped_and_counted_and_alone_they_are_no_evidence(db, agent):
    decision_id = await _decision(db, agent, session_id="sess-e")
    writer, recorder = _recorder(db, agent)
    for i, tool in enumerate(("list_decisions", "record_decision", "resolve_decisions")):
        _call(recorder, "sess-e", tool, T0 + timedelta(seconds=1 + i), index=i)
    await writer.drain(10)
    await _ledger(db, agent, "sess-e", "resolve_decisions", T0 + timedelta(seconds=4))
    built = await _build(db, agent, decision_id, T0 + timedelta(seconds=10))
    assert built.record is None and built.counts["link"] == "session_id"
    assert (built.counts["tool_calls"], built.counts["ledger_rows"], built.counts["bookkeeping"]) == (0, 0, 4)

    _call(recorder, "sess-e", "bash", T0 + timedelta(seconds=5), index=9)
    await writer.drain(10)
    built = await _build(db, agent, decision_id, T0 + timedelta(seconds=10))
    assert _tools(built) == ["bash"] and built.counts["bookkeeping"] == 4


async def test_ledger_rows_and_spawned_tasks_are_evidence(db, agent):
    decision_id = await _decision(db, agent, session_id="sess-f")
    await _ledger(db, agent, "sess-f", "write_file", T0 + timedelta(seconds=1))
    await _ledger(db, agent, "sess-f", "send_email", T0 + timedelta(seconds=30))  # after the resolve
    await _subtask(db, agent, "sess-f", T0 + timedelta(seconds=2), "Checked: the job ran at 03:00. " + "z" * 900)
    await _subtask(db, agent, "sess-other", T0 + timedelta(seconds=2), "another session")
    built = await _build(db, agent, decision_id, T0 + timedelta(seconds=10))
    assert (built.counts["tool_calls"], built.counts["ledger_rows"], built.counts["subtasks"]) == (0, 1, 1)
    ledger, task = built.record.evidence
    assert (ledger["source"], ledger["tool"], ledger["status"]) == ("ledger", "write_file", "success")
    assert ledger["outside_data"]["result"] == "write_file success"
    assert (task["source"], task["status"], task["finish_check"]) == ("spawned_task", "completed", "clean")
    report = task["written_by_author"]["report"]
    assert report.startswith("Checked: the job ran at 03:00.") and len(report) < 400
    assert built.record.evidence_truncated is True  # the report was cut to its cap


async def test_errors_and_calls_with_a_side_effect_survive_the_cap(db, agent):
    decision_id = await _decision(db, agent, session_id="sess-g")
    writer, recorder = _recorder(db, agent)
    for i in range(40):
        _call(recorder, "sess-g", "read_file", T0 + timedelta(seconds=10 + i), result="r" * 3000, index=i)
    _call(recorder, "sess-g", "write_file", T0 + timedelta(seconds=1), index=100)
    _call(
        recorder, "sess-g", "bash", T0 + timedelta(seconds=2), result="exit 1: no such file", is_error=True, index=101
    )
    await writer.drain(10)
    built = await _build(db, agent, decision_id, T0 + timedelta(minutes=5))
    tools = _tools(built)
    assert tools[:2] == ["write_file", "bash"] and "read_file" in tools
    assert built.counts["tool_calls"] == 42 and built.counts["dropped"] > 0
    assert built.counts["dropped"] == 42 - len(tools)
    assert built.counts["truncated"] is True and built.counts["chars"] <= evidence.EVIDENCE_MAX_CHARS
    error = built.record.evidence[1]
    assert error["is_error"] is True and error["outside_data"]["result"] == "exit 1: no such file"
    # A result kept in full (result_text, beside its cut preview) is read, cut to the judge's own cap.
    read = built.record.evidence[-1]
    assert read["outside_data"]["result"].startswith("r" * 100) and "[middle cut]" in read["outside_data"]["result"]


async def test_another_agents_rows_are_never_read(db, agent):
    decision_id = await _decision(db, agent)
    other = f"{agent}-other"
    writer, recorder = _recorder(db, agent)
    other_writer, other_recorder = _recorder(db, other)
    _call(recorder, "sess-h", "record_decision", T0 - timedelta(seconds=1), result=f"ID: {decision_id}")
    _call(recorder, "sess-h", "bash", T0 + timedelta(seconds=1), result="mine", index=1)
    # Another agent's call naming the same id, and calls in the same session id.
    _call(other_recorder, "sess-h2", "record_decision", T0 - timedelta(seconds=1), result=f"ID: {decision_id}")
    _call(other_recorder, "sess-h", "bash", T0 + timedelta(seconds=2), result="theirs", index=2)
    await writer.drain(10)
    await other_writer.drain(10)
    await _ledger(db, other, "sess-h", "write_file", T0 + timedelta(seconds=3))
    await _subtask(db, other, "sess-h", T0 + timedelta(seconds=3), "theirs")
    built = await _build(db, agent, decision_id, T0 + timedelta(seconds=10))
    assert built.counts["link"] == "record_call"
    assert [item["outside_data"]["result"] for item in built.record.evidence] == ["mine"]
    # Another agent's decision is not found at all.
    assert await _build(db, other, decision_id, T0 + timedelta(seconds=10)) is None


async def test_a_decision_that_no_longer_exists_gives_nothing(db, agent):
    assert await _build(db, agent, uuid.uuid4(), T0) is None


async def _longest_pause_while(work) -> tuple[float, object]:
    """Run ``work`` beside a ticker; the longest gap between two ticks (the event loop's longest stall)."""
    import asyncio

    loop = asyncio.get_running_loop()
    gaps: list[float] = []
    done = asyncio.Event()

    async def ticker() -> None:
        last = loop.time()
        while not done.is_set():
            await asyncio.sleep(0.01)
            now = loop.time()
            gaps.append(now - last)
            last = now

    ticking = asyncio.ensure_future(ticker())
    try:
        result = await work
    finally:
        done.set()
        await ticking
    return max(gaps), result


async def test_the_record_is_assembled_off_the_event_loop(db, agent, monkeypatch):
    import time

    decision_id = await _decision(db, agent, session_id="sess-loop")
    writer, recorder = _recorder(db, agent)
    _call(recorder, "sess-loop", "bash", T0 + timedelta(seconds=1))
    await writer.drain(10)
    real = evidence.assemble

    def slow(*args, **kwargs):
        time.sleep(0.6)  # stands for redaction over a full record
        return real(*args, **kwargs)

    monkeypatch.setattr(evidence, "assemble", slow)
    pause, built = await _longest_pause_while(_build(db, agent, decision_id, T0 + timedelta(seconds=10)))
    assert built.record is not None and built.counts["tool_calls"] == 1
    assert pause < 0.3, pause


# ---- the record as it stood when the claim was fixed --------------------------------------------------------------


async def test_the_same_claim_judged_at_two_delays_gets_the_same_record(db, agent):
    decision_id = await _decision(db, agent, session_id="sess-asof")
    claim_at = T0 + timedelta(seconds=10)
    writer, recorder = _recorder(db, agent)
    _call(recorder, "sess-asof", "bash", T0 + timedelta(seconds=1), result="moved", duration_ms=1000, index=1)
    # Started before the claim, ended after it: its result was not known when the claim was fixed.
    _call(recorder, "sess-asof", "web_fetch", T0 + timedelta(seconds=8), result="late page", duration_ms=5000, index=2)
    await writer.drain(10)
    ledger_id = await _ledger(db, agent, "sess-asof", "write_file", T0 + timedelta(seconds=3), "pending", None)
    task_id = await _subtask(db, agent, "sess-asof", T0 + timedelta(seconds=4), "", None)

    soon = await _build(db, agent, decision_id, claim_at)

    # Later, while the decision waits for the judge: the write and the task finish, and more work is done.
    async with db.session() as session:
        await session.execute(
            text(
                "UPDATE nous_system.execution_ledger SET status = 'success', result_summary = 'written late', "
                "completed_at = :t WHERE id = :id"
            ),
            {"t": claim_at + timedelta(seconds=5), "id": ledger_id},
        )
        await session.execute(
            text(
                "UPDATE heart.subtasks SET status = 'completed', final_outcome = 'completed', completed_at = :t, "
                "report_jsonb = CAST(:r AS jsonb), metadata = CAST(:m AS jsonb) WHERE id = :id"
            ),
            {
                "t": claim_at + timedelta(seconds=20),
                "r": json.dumps({"summary": "Checked after the claim.", "findings": []}),
                "m": json.dumps({"finish_check": {"verdict": "clean", "failed": []}}),
                "id": task_id,
            },
        )
        await session.commit()
    _call(recorder, "sess-asof", "bash", claim_at + timedelta(seconds=1), result="after the claim", index=3)
    await writer.drain(10)
    await _ledger(db, agent, "sess-asof", "send_email", claim_at + timedelta(seconds=2))
    await _subtask(db, agent, "sess-asof", claim_at + timedelta(seconds=3), "a later task")

    later = await _build(db, agent, decision_id, claim_at)
    assert later.record == soon.record and later.counts == soon.counts

    ledger, task, call = sorted(soon.record.evidence, key=lambda item: item["source"])
    assert (call["tool"], call["outside_data"]["result"]) == ("bash", "moved")  # the late call is not there
    assert (ledger["tool"], ledger["status"], ledger["outside_data"]["result"]) == (
        "write_file",
        evidence.NOT_FINISHED,
        None,
    )
    assert (task["status"], task["finish_check"], task["written_by_author"]["report"]) == (
        evidence.NOT_FINISHED,
        None,
        None,
    )
    assert "Checked after the claim." not in json.dumps(soon.record.evidence)


async def test_work_finished_by_the_claim_keeps_its_result(db, agent):
    decision_id = await _decision(db, agent, session_id="sess-done")
    claim_at = T0 + timedelta(seconds=10)
    await _ledger(db, agent, "sess-done", "write_file", T0 + timedelta(seconds=2), completed_at=claim_at)
    await _subtask(db, agent, "sess-done", T0 + timedelta(seconds=3), "Done in time.", completed_at=claim_at)
    built = await _build(db, agent, decision_id, claim_at)
    ledger, task = built.record.evidence
    assert (ledger["status"], ledger["outside_data"]["result"]) == ("success", "write_file success")
    assert (task["status"], task["finish_check"], task["written_by_author"]["report"]) == (
        "completed",
        "clean",
        "Done in time.",
    )


# ---- rows past the queries' limits -----------------------------------------------------------------------------


async def test_tasks_past_the_limit_are_counted_as_left_out(db, agent):
    decision_id = await _decision(db, agent, session_id="sess-many")
    for i in range(6):
        await _subtask(db, agent, "sess-many", T0 + timedelta(seconds=1 + i), f"report {i}")
    built = await _build(db, agent, decision_id, T0 + timedelta(minutes=1))
    reports = [item["written_by_author"]["report"] for item in built.record.evidence]
    assert reports == [f"report {i}" for i in range(1, 6)]  # the newest five
    assert (built.counts["subtasks"], built.counts["over_limit"], built.counts["dropped"]) == (6, 1, 1)
    assert built.counts["truncated"] is True
    assert (built.record.evidence_dropped, built.record.evidence_truncated) == (1, True)


async def test_calls_and_ledger_rows_past_their_limits_are_counted_as_left_out(db, agent, monkeypatch):
    monkeypatch.setattr(evidence, "MAX_CALLS", 2)
    monkeypatch.setattr(evidence, "MAX_LEDGER_ROWS", 1)
    decision_id = await _decision(db, agent, session_id="sess-cap")
    writer, recorder = _recorder(db, agent)
    for i in range(3):
        _call(recorder, "sess-cap", "read_file", T0 + timedelta(seconds=1 + i), index=i)
    for i, tool in enumerate(("record_decision", "list_decisions")):  # bookkeeping: counted, never evidence
        _call(recorder, "sess-cap", tool, T0 + timedelta(seconds=10 + i), index=10 + i)
    await writer.drain(10)
    for i in range(3):
        await _ledger(db, agent, "sess-cap", "write_file", T0 + timedelta(seconds=1 + i))
    built = await _build(db, agent, decision_id, T0 + timedelta(minutes=1))
    assert (built.counts["tool_calls"], built.counts["ledger_rows"], built.counts["bookkeeping"]) == (3, 3, 2)
    assert built.counts["over_limit"] == 1 + 2
    assert len(built.record.evidence) == 2 + 1
    assert built.record.evidence_dropped == 3 and built.record.evidence_truncated is True


async def test_rows_within_the_limits_leave_nothing_out(db, agent):
    decision_id = await _decision(db, agent, session_id="sess-few")
    for i in range(5):
        await _subtask(db, agent, "sess-few", T0 + timedelta(seconds=1 + i), f"report {i}")
    built = await _build(db, agent, decision_id, T0 + timedelta(minutes=1))
    assert (built.counts["over_limit"], built.counts["dropped"], built.record.evidence_truncated) == (0, 0, False)
