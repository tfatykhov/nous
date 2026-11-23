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
tasks from the decision's creation to the resolve. The calls that only record, list or resolve decisions are left out
and counted. Without a session, or without any other row, there is nothing to judge, and neither is there when the
record cannot be rendered.
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
) -> JudgeInput:
    """The bounded record. ``items`` are raw evidence items, each with ``source`` (``tool_call``, ``ledger`` or
    ``spawned_task``), ``at`` (a number that orders them in time) and its fields; any order. Every string is kept as
    the second redaction leaves it where it sits; one that pass would break is replaced by ``LEFT_OUT`` and the record
    is marked truncated. The kept items are returned in the order they ran. When the whole record still does not
    render, the last-kept item is dropped and it is tried again; ``RenderError`` is raised only when the record
    without any evidence does not render. CPU-bound: callers in the event loop run it in a thread."""
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
        dropped = len(rendered) - len(kept)
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
MAX_CALLS = 200
MAX_LEDGER_ROWS = 50
MAX_SUBTASKS = 5
# How long before the decision's creation its record_decision call may have started.
RECORD_CALL_WINDOW = timedelta(minutes=2)
# Room for the clocks of the process (a call's start) and the database (the decision's creation) to differ.
CLOCK_SLACK = timedelta(seconds=10)

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
_CALLS = text(
    "SELECT tool_name, outcome, is_error, input_text, result_preview, result_text, error_text, started_at "
    "FROM nous_system.tool_call_log "
    "WHERE agent_id = :agent AND session_id = :session AND started_at >= :since AND started_at <= :until "
    f"ORDER BY started_at DESC LIMIT {MAX_CALLS}"
)
_LEDGER = text(
    "SELECT tool_name, status, result_summary, created_at FROM nous_system.execution_ledger "
    "WHERE agent_id = :agent AND session_id = :session AND created_at >= :since AND created_at <= :until "
    f"ORDER BY created_at DESC LIMIT {MAX_LEDGER_ROWS}"
)
_SUBTASKS = text(
    "SELECT COALESCE(final_outcome, status), report_jsonb ->> 'summary', metadata -> 'finish_check' ->> 'verdict', "
    "created_at FROM heart.subtasks "
    "WHERE agent_id = :agent AND parent_session_id = :session AND created_at >= :since AND created_at <= :until "
    f"ORDER BY created_at DESC LIMIT {MAX_SUBTASKS}"
)


@dataclass(frozen=True)
class DecisionEvidence:
    """What was found for one decision: the bounded record (None: nothing to judge, ``skip_label`` says why) and the
    counts kept on the verdict row (``link``, ``tool_calls``, ``ledger_rows``, ``subtasks``, ``bookkeeping``,
    ``dropped``, ``chars``, ``truncated``)."""

    record: JudgeInput | None
    counts: dict[str, Any]
    skip_label: str = "evidence_missing"


def _assemble_and_measure(
    decision_id: UUID, subject: dict[str, Any], claim: Claim, items: list[dict[str, Any]]
) -> tuple[JudgeInput, int]:
    """The record and the length of its rendered text (run in a thread)."""
    record = assemble("decision", decision_id, subject, claim, items)
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


async def build_decision_evidence(
    session: AsyncSession,
    agent_id: str,
    decision_id: UUID,
    claim: Claim,
    resolved_at: datetime,
) -> DecisionEvidence | None:
    """The record of one decision as of ``resolved_at``, or None when the decision does not exist for this agent.
    Read-only. The record is assembled in a thread, off the event loop; one that cannot be rendered gives no record
    and the skip label ``NOT_RENDERED``."""
    row = (await session.execute(_DECISION, {"agent": agent_id, "id": decision_id})).one_or_none()
    if row is None:
        return None
    counts: dict[str, Any] = {
        "link": "none",
        "tool_calls": 0,
        "ledger_rows": 0,
        "subtasks": 0,
        "bookkeeping": 0,
        "dropped": 0,
        "chars": 0,
        "truncated": False,
    }
    counts["link"], linked = await _linked_session(session, agent_id, decision_id, row)
    if linked is None:
        return DecisionEvidence(None, counts)

    window = {"agent": agent_id, "session": linked, "since": row.created_at, "until": resolved_at}
    items: list[dict[str, Any]] = []
    for call in (await session.execute(_CALLS, window)).all():
        if call.tool_name in BOOKKEEPING_TOOLS:
            counts["bookkeeping"] += 1
            continue
        counts["tool_calls"] += 1
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
    for entry in (await session.execute(_LEDGER, window)).all():
        if entry.tool_name in BOOKKEEPING_TOOLS:
            counts["bookkeeping"] += 1
            continue
        counts["ledger_rows"] += 1
        items.append(
            {
                "source": "ledger",
                "tool": entry.tool_name,
                "status": entry.status,
                "is_error": entry.status == "error",
                "result": entry.result_summary,
                "at": entry.created_at.timestamp(),
            }
        )
    for status, summary, finish_check, created_at in (await session.execute(_SUBTASKS, window)).all():
        counts["subtasks"] += 1
        items.append(
            {
                "source": "spawned_task",
                "status": status,
                "finish_check": finish_check,
                "report": summary,
                "at": created_at.timestamp(),
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
        record, chars = await asyncio.to_thread(_assemble_and_measure, decision_id, subject, claim, items)
    except RenderError:
        return DecisionEvidence(None, counts, NOT_RENDERED)
    counts["dropped"] = record.evidence_dropped
    counts["truncated"] = record.evidence_truncated
    counts["chars"] = chars
    return DecisionEvidence(record, counts)
