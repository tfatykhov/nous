"""F118: the evidence a judge reads, its bounds, and the one JSON text it is rendered as.

``assemble`` turns a subject, its claim and the raw evidence items into a bounded ``JudgeInput``: every string is
redacted and cut to its cap (head and tail kept) before anything is serialised, and items are kept in priority order
(errors, then calls with a side effect, then the rest, newest first in each) while the rendered record stays within
``EVIDENCE_MAX_CHARS``. An item that does not fit is dropped whole and counted. Each string is kept as the redaction
of the serialised record leaves it where it sits; one that this redaction would break is replaced by a fixed
placeholder. ``render_record`` serialises the record, redacts the finished text once more, and refuses a text that no
longer parses as the same record or is over the cap: the text is never sliced.

The rendered record keeps what the agent wrote under ``written_by_author`` and what a tool returned under
``outside_data``, so each is a JSON string value labelled by where it came from.

``build_decision_evidence`` reads a decision's record (read-only, every query filtered on the agent): the decision's
own fields, then the session that recorded it (its ``session_id``, else the one ``record_decision`` call whose reply
names its id, made up to two minutes before it), then that session's tool calls, execution ledger rows and spawned
tasks from the decision's creation to the resolve, as they stood at the resolve: what finished later is shown as not
finished, without its result, so the record is the same however long the judging waits. Rows past each query's
limit are counted as left out. The calls that only record, list or resolve decisions are left out and counted.
Without a session, or without any other row, there is nothing to judge, and neither is there when the record cannot
be rendered.

``build_report_evidence`` reads a finished task's record the same way, as it stood when the task was completed: the
task as it was set and its report (the claim), and the task's own calls, ledger rows and child tasks. The finish
checks' verdict on the task is kept with the counts, not in the record. Only a task finished as ``completed`` with a
report is judged.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from nous.api.tool_classes import tool_class
from nous.cognitive.ledger_store import redact_output, redact_structure
from nous.judge.slot import Claim, JudgeInput

EVIDENCE_MAX_CHARS = 16_000
SUBJECT_CAP = 600
NOTE_CAP = 500
ARGS_CAP = 300
RESULT_CAP = 800
REPORT_CAP = 300
LABEL_CAP = 100  # a tool name, a status, a finish check verdict
# The marker ``detail_text`` puts where it cut a text: a string that holds it was cut (here or by its writer).
CUT_MARKER = "[middle cut]"
# What stands in for a string that the second redaction of the serialised record would break.
LEFT_OUT = "[text left out]"
# The label of a skipped verdict whose record could not be rendered.
NOT_RENDERED = "record_not_rendered"
# The top-level keys of a rendered record.
RECORD_KEYS = frozenset({"subject_kind", "written_by_author", "evidence", "evidence_truncated", "evidence_dropped"})


class RenderError(Exception):
    """The record could not be rendered within its bounds as valid JSON: nothing is sent."""


def _bounded(value: Any, cap: int) -> Any:
    return redact_structure(value, cap) if isinstance(value, str) else value


def _side_effect(item: Mapping[str, Any]) -> bool:
    if item.get("source") in ("ledger", "spawned_task"):
        return True
    declared = tool_class(str(item.get("tool") or ""))
    return declared is not None and declared.side_effect != "none"


def _priority(item: Mapping[str, Any]) -> int:
    if item.get("is_error"):
        return 0
    return 1 if _side_effect(item) else 2


def _render_item(item: Mapping[str, Any]) -> dict[str, Any]:
    """One raw item as the judge reads it, every string bounded and redacted."""
    source = item.get("source")
    out: dict[str, Any] = {"source": source}
    if source == "spawned_task":
        out["status"] = _bounded(item.get("status"), LABEL_CAP)
        out["finish_check"] = _bounded(item.get("finish_check"), LABEL_CAP)
        out["written_by_author"] = {"report": _bounded(item.get("report"), REPORT_CAP)}
        return out
    out["tool"] = _bounded(item.get("tool"), LABEL_CAP)
    out["status"] = _bounded(item.get("status"), LABEL_CAP)
    out["is_error"] = bool(item.get("is_error"))
    if source == "tool_call":
        out["written_by_author"] = {"args": _bounded(item.get("args"), ARGS_CAP)}
    out["outside_data"] = {"result": _bounded(item.get("result"), RESULT_CAP)}
    return out


def _strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for leaf in value.values():
            yield from _strings(leaf)
    elif isinstance(value, list | tuple):
        for leaf in value:
            yield from _strings(leaf)


def _document(record: JudgeInput) -> dict[str, Any]:
    claim = record.claim
    return {
        "subject_kind": record.kind,
        "written_by_author": {
            "subject": dict(record.subject),
            "claim": None if claim is None else {"grader": claim.grader, "grade": claim.grade, "note": claim.note},
        },
        "evidence": [dict(item) for item in record.evidence],
        "evidence_truncated": record.evidence_truncated,
        "evidence_dropped": record.evidence_dropped,
    }


def _dumps(document: Any) -> str:
    return json.dumps(document, ensure_ascii=False, default=str)


def render_record(record: JudgeInput) -> str:
    """The record as one JSON text, redacted once more as a whole. Raises ``RenderError`` when that text no longer
    parses as the same record or is over ``EVIDENCE_MAX_CHARS``; it is never sliced."""
    document = _document(record)
    try:
        text = redact_output(_dumps(document))
        parsed = json.loads(text)
    except Exception as exc:
        raise RenderError(type(exc).__name__) from None
    if not isinstance(parsed, dict) or set(parsed) != RECORD_KEYS:
        raise RenderError("the redacted text is not the record")
    if not isinstance(parsed["evidence"], list) or len(parsed["evidence"]) != len(document["evidence"]):
        raise RenderError("the redacted text lost an evidence item")
    if len(text) > EVIDENCE_MAX_CHARS:
        raise RenderError("over the cap")
    return text


# Where a string sits in the record: a JSON string value, its closing quote followed by more of the record (the next
# key of the same object, the next item of the list, a key of the enclosing object). Each places it and reads it back.
_CONTEXTS = (
    (lambda value: {"a": value, "b": "x"}, lambda doc: (doc["a"], doc["b"])),
    (lambda value: [{"a": value}, {"b": "x"}], lambda doc: (doc[0]["a"], doc[1]["b"])),
    (lambda value: {"a": {"a": value}, "b": "x"}, lambda doc: (doc["a"]["a"], doc["b"])),
)


def _in_context(value: str) -> str | None:
    """``value`` as the second redaction of the serialised record leaves it where it sits, or None when that pass
    breaks the JSON around it (a pattern that runs from its closing quote into what follows)."""
    kept: str | None = None
    for place, read in _CONTEXTS:
        try:
            got, after = read(json.loads(redact_output(_dumps(place(value)))))
        except Exception:
            return None
        if after != "x" or not isinstance(got, str) or (kept is not None and got != kept):
            return None
        kept = got
    return kept


def _settled_string(value: str) -> tuple[str, bool]:
    """``value`` as it reads after the second redaction, and whether it was left out. A mask at the very end of a
    string (``Bearer [REDACTED]``) is matched again in the serialised text; a space after it ends that match, so that
    is tried once. A string that still breaks the record is replaced by ``LEFT_OUT``."""
    for candidate in (value, value + " "):
        kept = _in_context(candidate)
        if kept is not None:
            return kept, False
    return LEFT_OUT, True


def _settled(value: Any) -> tuple[Any, bool]:
    """``value`` with every string settled (``_settled_string``), and whether any was left out."""
    if isinstance(value, str):
        return _settled_string(value)
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        left_out = False
        for key, leaf in value.items():
            out[key], left = _settled(leaf)
            left_out = left_out or left
        return out, left_out
    return value, False


def assemble(
    kind: str,
    subject_id: UUID,
    subject: Mapping[str, Any],
    claim: Claim | None,
    items: Iterable[Mapping[str, Any]],
    *,
    not_read: int = 0,
) -> JudgeInput:
    """The bounded record. ``items`` are raw evidence items, each with ``source`` (``tool_call``, ``ledger`` or
    ``spawned_task``), ``at`` (a number that orders them in time) and its fields; any order. ``not_read`` counts items
    the caller could not read (past a query's limit): they are counted as dropped and mark the record truncated. Every
    string is kept as the second redaction leaves it where it sits; one that pass would break is replaced by
    ``LEFT_OUT`` and the record is marked truncated. The kept items are returned in the order they ran. When the whole
    record still does not render, the last-kept item is dropped and it is tried again; ``RenderError`` is raised only
    when the record without any evidence does not render. CPU-bound: callers in the event loop run it in a thread."""
    bounded_subject, left_out = _settled({key: _bounded(value, SUBJECT_CAP) for key, value in subject.items()})
    bounded_claim = None
    if claim is not None:
        note, note_left_out = _settled(_bounded(claim.note, NOTE_CAP))
        left_out = left_out or note_left_out
        bounded_claim = Claim(grader=claim.grader, grade=claim.grade, note=note)
    ranked = sorted(items, key=lambda item: (_priority(item), -float(item.get("at") or 0.0)))
    rendered: list[tuple[float, dict[str, Any]]] = []
    for item in ranked:
        settled_item, item_left_out = _settled(_render_item(item))
        left_out = left_out or item_left_out
        rendered.append((float(item.get("at") or 0.0), settled_item))

    # The room for items: the record with no evidence, its two flags at their longest (false, three digits).
    empty = JudgeInput(kind, subject_id, bounded_subject, bounded_claim, (), False, 999)
    room = EVIDENCE_MAX_CHARS - len(_dumps(_document(empty)))
    kept: list[tuple[float, dict[str, Any]]] = []
    used = 0
    for at, item in rendered:
        size = len(_dumps(item)) + (2 if kept else 0)  # ", " between items
        if used + size <= room:
            kept.append((at, item))
            used += size
    while True:
        dropped = len(rendered) - len(kept) + not_read
        ordered = tuple(item for _at, item in sorted(kept, key=lambda pair: pair[0]))
        cut = any(
            CUT_MARKER in leaf for leaf in _strings([bounded_subject, bounded_claim and bounded_claim.note, ordered])
        )
        truncated = dropped > 0 or cut or left_out
        record = JudgeInput(kind, subject_id, bounded_subject, bounded_claim, ordered, truncated, dropped)
        try:
            render_record(record)
            return record
        except RenderError:
            # Over the cap (a mask longer than what it hides) or not the same record: drop the last-kept item.
            if not kept:
                raise
            kept.pop()


# ---- the decision's record ---------------------------------------------------------------------------------------

# The calls that only record, list or resolve decisions: they show nothing about an outcome.
BOOKKEEPING_TOOLS = frozenset({"record_decision", "resolve_decisions", "list_decisions"})
_BOOKKEEPING_SQL = "(" + ", ".join(f"'{name}'" for name in sorted(BOOKKEEPING_TOOLS)) + ")"
MAX_CALLS = 200
MAX_LEDGER_ROWS = 50
MAX_SUBTASKS = 5
# How long before the decision's creation its record_decision call may have started.
RECORD_CALL_WINDOW = timedelta(minutes=2)
# Room for the clocks of the process (a call's start) and the database (the decision's creation) to differ.
CLOCK_SLACK = timedelta(seconds=10)
# The status of a ledger row or a task that had not finished when the claim was fixed (its later result is not read).
NOT_FINISHED = "not finished when the claim was made"

_DECISION = text(
    "SELECT description, context, pattern, category, stakes, confidence, confidence_raw, session_id, created_at "
    "FROM brain.decisions WHERE agent_id = :agent AND id = :id"
)
_RECORD_CALLS = text(
    "SELECT session_id FROM nous_system.tool_call_log "
    "WHERE agent_id = :agent AND tool_name = 'record_decision' "
    "AND started_at >= :since AND started_at <= :until AND result_preview LIKE :mark "
    "LIMIT 2"
)
# Every source is read as of the claim (``:until``): a call that had ended by then (its row is written when it ends),
# a ledger row or a task that had started by then. What finished later is read without its result.
_CALL_WINDOW = (
    "FROM nous_system.tool_call_log WHERE agent_id = :agent AND session_id = :session AND started_at >= :since "
    "AND started_at + COALESCE(duration_ms, 0) * interval '1 millisecond' <= :until"
)
_CALL_COUNTS = text(
    f"SELECT count(*) FILTER (WHERE tool_name NOT IN {_BOOKKEEPING_SQL}), "
    f"count(*) FILTER (WHERE tool_name IN {_BOOKKEEPING_SQL}) {_CALL_WINDOW}"
)
_CALLS = text(
    "SELECT tool_name, outcome, is_error, input_text, result_preview, result_text, error_text, started_at "
    f"{_CALL_WINDOW} AND tool_name NOT IN {_BOOKKEEPING_SQL} ORDER BY started_at DESC LIMIT :limit"
)
_LEDGER_WINDOW = (
    "FROM nous_system.execution_ledger WHERE agent_id = :agent AND session_id = :session AND created_at >= :since "
    "AND created_at <= :until"
)
_LEDGER_COUNTS = text(
    f"SELECT count(*) FILTER (WHERE tool_name NOT IN {_BOOKKEEPING_SQL}), "
    f"count(*) FILTER (WHERE tool_name IN {_BOOKKEEPING_SQL}) {_LEDGER_WINDOW}"
)
_LEDGER = text(
    "SELECT tool_name, status, result_summary, created_at, completed_at "
    f"{_LEDGER_WINDOW} AND tool_name NOT IN {_BOOKKEEPING_SQL} ORDER BY created_at DESC LIMIT :limit"
)
_SUBTASK_WINDOW = (
    "FROM heart.subtasks WHERE agent_id = :agent AND parent_session_id = :session AND created_at >= :since "
    "AND created_at <= :until"
)
_SUBTASK_COUNT = text(f"SELECT count(*) {_SUBTASK_WINDOW}")
_SUBTASKS = text(
    "SELECT COALESCE(final_outcome, status) AS status, report_jsonb ->> 'summary' AS summary, "
    "metadata -> 'finish_check' ->> 'verdict' AS finish_check, created_at, completed_at "
    f"{_SUBTASK_WINDOW} ORDER BY created_at DESC LIMIT :limit"
)


@dataclass(frozen=True)
class DecisionEvidence:
    """What was found for one decision: the bounded record (None: nothing to judge, ``skip_label`` says why) and the
    counts kept on the verdict row: ``link``; ``tool_calls``, ``ledger_rows`` and ``subtasks`` in the claim's window;
    ``bookkeeping``; ``over_limit``, the rows past the queries' limits (never read); ``dropped``, every item left out
    (those and the ones the size cap dropped); ``chars``; ``truncated``."""

    record: JudgeInput | None
    counts: dict[str, Any]
    skip_label: str = "evidence_missing"


def _assemble_and_measure(
    decision_id: UUID, subject: dict[str, Any], claim: Claim | None, items: list[dict[str, Any]], not_read: int
) -> tuple[JudgeInput, int]:
    """The record and the length of its rendered text (run in a thread)."""
    record = assemble("decision", decision_id, subject, claim, items, not_read=not_read)
    return record, len(render_record(record))


async def _linked_session(session: AsyncSession, agent_id: str, decision_id: UUID, row: Any) -> tuple[str, str | None]:
    """How the decision is linked to the session that recorded it, and that session."""
    if row.session_id:
        return "session_id", row.session_id
    since, until = row.created_at - RECORD_CALL_WINDOW, row.created_at + CLOCK_SLACK
    params = {"agent": agent_id, "since": since, "until": until, "mark": f"%ID: {decision_id}%"}
    rows = (await session.execute(_RECORD_CALLS, params)).all()
    if len(rows) == 1 and rows[0].session_id:
        return "record_call", rows[0].session_id
    return "none", None


def _finished_by(completed_at: datetime | None, claim_at: datetime) -> bool:
    return completed_at is not None and completed_at <= claim_at


async def build_decision_evidence(
    session: AsyncSession,
    agent_id: str,
    decision_id: UUID,
    claim: Claim | None,
    resolved_at: datetime,
) -> DecisionEvidence | None:
    """The record of one decision as it stood at ``resolved_at`` (when the claim was fixed), or None when the decision
    does not exist for this agent. ``claim`` is the grade the judge reads with it (None: the record holds none).
    Read-only, and the same whenever it is read: a call counts once it had ended by
    then, a ledger row or a task once it had started, and one that finished later is shown as ``NOT_FINISHED``
    without its result. Each source reads its newest rows up to its limit; the rest are counted as left out. The record
    is assembled in a thread, off the event loop; one that cannot be rendered gives no record and the skip label
    ``NOT_RENDERED``."""
    row = (await session.execute(_DECISION, {"agent": agent_id, "id": decision_id})).one_or_none()
    if row is None:
        return None
    counts: dict[str, Any] = {
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
    counts["link"], linked = await _linked_session(session, agent_id, decision_id, row)
    if linked is None:
        return DecisionEvidence(None, counts)

    window = {"agent": agent_id, "session": linked, "since": row.created_at, "until": resolved_at}
    calls, calls_bookkeeping = (await session.execute(_CALL_COUNTS, window)).one()
    ledger_rows, ledger_bookkeeping = (await session.execute(_LEDGER_COUNTS, window)).one()
    subtasks = (await session.execute(_SUBTASK_COUNT, window)).scalar_one()
    counts.update(
        tool_calls=calls,
        ledger_rows=ledger_rows,
        subtasks=subtasks,
        bookkeeping=calls_bookkeeping + ledger_bookkeeping,
        over_limit=max(0, calls - MAX_CALLS) + max(0, ledger_rows - MAX_LEDGER_ROWS) + max(0, subtasks - MAX_SUBTASKS),
    )

    items: list[dict[str, Any]] = []
    for call in (await session.execute(_CALLS, {**window, "limit": MAX_CALLS})).all():
        items.append(
            {
                "source": "tool_call",
                "tool": call.tool_name,
                "status": call.outcome,
                "is_error": bool(call.is_error),
                "args": call.input_text,
                "result": call.error_text if call.is_error else (call.result_text or call.result_preview),
                "at": call.started_at.timestamp(),
            }
        )
    for entry in (await session.execute(_LEDGER, {**window, "limit": MAX_LEDGER_ROWS})).all():
        finished = _finished_by(entry.completed_at, resolved_at)
        items.append(
            {
                "source": "ledger",
                "tool": entry.tool_name,
                "status": entry.status if finished else NOT_FINISHED,
                "is_error": finished and entry.status == "error",
                "result": entry.result_summary if finished else None,
                "at": entry.created_at.timestamp(),
            }
        )
    for task in (await session.execute(_SUBTASKS, {**window, "limit": MAX_SUBTASKS})).all():
        finished = _finished_by(task.completed_at, resolved_at)
        items.append(
            {
                "source": "spawned_task",
                "status": task.status if finished else NOT_FINISHED,
                "finish_check": task.finish_check if finished else None,
                "report": task.summary if finished else None,
                "at": task.created_at.timestamp(),
            }
        )
    if not items:
        return DecisionEvidence(None, counts)

    subject = {
        "description": row.description,
        "context": row.context,
        "pattern": row.pattern,
        "category": row.category,
        "stakes": row.stakes,
        "stated_confidence": row.confidence_raw if row.confidence_raw is not None else row.confidence,
    }
    try:
        record, chars = await asyncio.to_thread(
            _assemble_and_measure, decision_id, subject, claim, items, counts["over_limit"]
        )
    except RenderError:
        return DecisionEvidence(None, counts, NOT_RENDERED)
    counts["dropped"] = record.evidence_dropped
    counts["truncated"] = record.evidence_truncated
    counts["chars"] = chars
    return DecisionEvidence(record, counts)


# ---- a finished task's report ------------------------------------------------------------------------------------

# The calls that only hand in the report, or record, list or resolve decisions: they show nothing about the work.
REPORT_BOOKKEEPING_TOOLS = BOOKKEEPING_TOOLS | frozenset({"submit_final_report"})
_REPORT_BOOKKEEPING_SQL = "(" + ", ".join(f"'{name}'" for name in sorted(REPORT_BOOKKEEPING_TOOLS)) + ")"
# What a finished task's report is graded by its author: the task marked completed.
REPORT_CLAIM_GRADER = "agent"
REPORT_CLAIM_GRADE = "completed"

_TASK = text(
    "SELECT task, success_criteria, output_format, status, final_outcome, report_jsonb, "
    "metadata ->> 'session_id' AS shared_session, metadata -> 'finish_check' AS finish_check, created_at, "
    "completed_at FROM heart.subtasks WHERE agent_id = :agent AND id = :id"
)
# The task's own rows: its id on the row, or (a row without one) its own session; ``:own`` is NULL for a task in a
# shared session, whose untagged rows may be other work's. Read as of the completion, like a decision's record.
_TASK_ROWS = "agent_id = :agent AND (subtask_id = :task OR (subtask_id IS NULL AND session_id = :own))"
_TASK_CALL_WINDOW = (
    f"FROM nous_system.tool_call_log WHERE {_TASK_ROWS} AND started_at >= :since "
    "AND started_at + COALESCE(duration_ms, 0) * interval '1 millisecond' <= :until"
)
_TASK_CALL_COUNTS = text(
    f"SELECT count(*) FILTER (WHERE tool_name NOT IN {_REPORT_BOOKKEEPING_SQL}), "
    f"count(*) FILTER (WHERE tool_name IN {_REPORT_BOOKKEEPING_SQL}) {_TASK_CALL_WINDOW}"
)
_TASK_CALLS = text(
    "SELECT tool_name, outcome, is_error, input_text, result_preview, result_text, error_text, started_at "
    f"{_TASK_CALL_WINDOW} AND tool_name NOT IN {_REPORT_BOOKKEEPING_SQL} ORDER BY started_at DESC LIMIT :limit"
)
_TASK_LEDGER_WINDOW = (
    f"FROM nous_system.execution_ledger WHERE {_TASK_ROWS} AND created_at >= :since AND created_at <= :until"
)
_TASK_LEDGER_COUNTS = text(
    f"SELECT count(*) FILTER (WHERE tool_name NOT IN {_REPORT_BOOKKEEPING_SQL}), "
    f"count(*) FILTER (WHERE tool_name IN {_REPORT_BOOKKEEPING_SQL}) {_TASK_LEDGER_WINDOW}"
)
_TASK_LEDGER = text(
    "SELECT tool_name, status, result_summary, created_at, completed_at "
    f"{_TASK_LEDGER_WINDOW} AND tool_name NOT IN {_REPORT_BOOKKEEPING_SQL} ORDER BY created_at DESC LIMIT :limit"
)


@dataclass(frozen=True)
class ReportEvidence:
    """What was found for one finished task: the bounded record (None: nothing to judge, ``skip_label`` says why), the
    claim (the report, graded ``completed`` by the agent), when it was made (the task's completion) and the counts
    kept on the verdict row: those of ``DecisionEvidence``, ``link`` being ``own_session`` or ``shared_session``, and
    ``finish_check``, the finish checks' verdict on the task (None without one). That verdict is what the judge's is
    compared with, so it is on the row only, never in the record."""

    record: JudgeInput | None
    counts: dict[str, Any]
    claim: Claim
    claim_at: datetime
    skip_label: str = "evidence_missing"


def _assemble_report(
    task_id: UUID, subject: dict[str, Any], claim: Claim, items: list[dict[str, Any]], not_read: int
) -> tuple[JudgeInput, int]:
    """The record of a report and the length of its rendered text (run in a thread)."""
    record = assemble("task_report", task_id, subject, claim, items, not_read=not_read)
    return record, len(render_record(record))


def _finish_check_verdict(value: Any) -> str | None:
    """The verdict of the finish check record (``metadata.finish_check``), or None without one."""
    verdict = value.get("verdict") if isinstance(value, Mapping) else None
    return verdict if isinstance(verdict, str) else None


async def build_report_evidence(session: AsyncSession, agent_id: str, task_id: UUID) -> ReportEvidence | None:
    """The record of one finished task's report as it stood when the task was completed, or None when the task is
    not this agent's task finished as ``completed`` (status and outcome) with a report that has a summary: a failed,
    cancelled or blocked task is never judged. Read-only, and the same whenever it is read: a call counts once it had
    ended by the completion, a ledger row or a child task once it had started, and one that finished later is shown
    as ``NOT_FINISHED`` without its result. The task's own rows are read (``_TASK_ROWS``); the calls that only hand in
    the report or record decisions are left out and counted. The finish checks' verdict on the task is kept in the
    counts only, never in the record. Without a call, a ledger row or a child task there is nothing to judge, and
    neither is there when the record cannot be rendered (``NOT_RENDERED``)."""
    row = (await session.execute(_TASK, {"agent": agent_id, "id": task_id})).one_or_none()
    if row is None or row.status != "completed" or row.final_outcome != "completed" or row.completed_at is None:
        return None
    report = row.report_jsonb if isinstance(row.report_jsonb, dict) else {}
    summary = report.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        return None
    claim = Claim(grader=REPORT_CLAIM_GRADER, grade=REPORT_CLAIM_GRADE, note=summary)
    claim_at = row.completed_at
    own = None if row.shared_session else f"subtask-{task_id.hex[:8]}"
    counts: dict[str, Any] = {
        "link": "own_session" if own else "shared_session",
        "tool_calls": 0,
        "ledger_rows": 0,
        "subtasks": 0,
        "bookkeeping": 0,
        "over_limit": 0,
        "dropped": 0,
        "chars": 0,
        "truncated": False,
        "finish_check": _finish_check_verdict(row.finish_check),
    }
    window = {"agent": agent_id, "task": task_id, "own": own, "since": row.created_at - CLOCK_SLACK, "until": claim_at}
    calls, calls_bookkeeping = (await session.execute(_TASK_CALL_COUNTS, window)).one()
    ledger_rows, ledger_bookkeeping = (await session.execute(_TASK_LEDGER_COUNTS, window)).one()
    children = 0
    if own is not None:  # a task's children name its own session as their parent
        children_window = {"agent": agent_id, "session": own, "since": window["since"], "until": claim_at}
        children = (await session.execute(_SUBTASK_COUNT, children_window)).scalar_one()
    counts.update(
        tool_calls=calls,
        ledger_rows=ledger_rows,
        subtasks=children,
        bookkeeping=calls_bookkeeping + ledger_bookkeeping,
        over_limit=max(0, calls - MAX_CALLS) + max(0, ledger_rows - MAX_LEDGER_ROWS) + max(0, children - MAX_SUBTASKS),
    )
    if calls + ledger_rows + children == 0:
        return ReportEvidence(None, counts, claim, claim_at)

    items: list[dict[str, Any]] = []
    for call in (await session.execute(_TASK_CALLS, {**window, "limit": MAX_CALLS})).all():
        items.append(
            {
                "source": "tool_call",
                "tool": call.tool_name,
                "status": call.outcome,
                "is_error": bool(call.is_error),
                "args": call.input_text,
                "result": call.error_text if call.is_error else (call.result_text or call.result_preview),
                "at": call.started_at.timestamp(),
            }
        )
    for entry in (await session.execute(_TASK_LEDGER, {**window, "limit": MAX_LEDGER_ROWS})).all():
        finished = _finished_by(entry.completed_at, claim_at)
        items.append(
            {
                "source": "ledger",
                "tool": entry.tool_name,
                "status": entry.status if finished else NOT_FINISHED,
                "is_error": finished and entry.status == "error",
                "result": entry.result_summary if finished else None,
                "at": entry.created_at.timestamp(),
            }
        )
    if children:
        children_window = {"agent": agent_id, "session": own, "since": window["since"], "until": claim_at}
        for task in (await session.execute(_SUBTASKS, {**children_window, "limit": MAX_SUBTASKS})).all():
            finished = _finished_by(task.completed_at, claim_at)
            items.append(
                {
                    "source": "spawned_task",
                    "status": task.status if finished else NOT_FINISHED,
                    "finish_check": task.finish_check if finished else None,
                    "report": task.summary if finished else None,
                    "at": task.created_at.timestamp(),
                }
            )

    findings = report.get("findings") if isinstance(report.get("findings"), list) else []
    stated = report.get("confidence") if report.get("confidence_reported") is True else None
    subject = {
        "task": row.task,
        "success_criteria": row.success_criteria,
        "output_format": row.output_format,
        "report_findings": "\n".join(f for f in findings if isinstance(f, str)) or None,
        "stated_confidence": stated if isinstance(stated, int | float) and not isinstance(stated, bool) else None,
    }
    try:
        record, chars = await asyncio.to_thread(_assemble_report, task_id, subject, claim, items, counts["over_limit"])
    except RenderError:
        return ReportEvidence(None, counts, claim, claim_at, NOT_RENDERED)
    counts["dropped"] = record.evidence_dropped
    counts["truncated"] = record.evidence_truncated
    counts["chars"] = chars
    return ReportEvidence(record, counts, claim, claim_at)
