"""Durable execution ledger (harness-autonomy roadmap, Phase 1b).

The in-memory F026 ExecutionLedger stays the per-session prompt aid. This
store is the durable record of side-effecting tool calls: a row is written
'pending' BEFORE dispatch and closed after, so "did it happen?" is a query.

The store RAISES on write failure (LedgerWriteError, carrying the
client-generated id — the COMMIT may have landed even when the wait timed
out). The RUNNER decides what a failure means: Phase 1b fails open; Phase 2b
makes keyed sends fail closed. Rows never hold free text: an argument value
is kept only when its shape proves it is not free text (or it is the call's
target path), and tool output is never stored (durable_key_args, _summary).

Deployment assumption: ONE Nous process per (database, agent_id). The startup
sweep marks every 'pending' row 'unknown' because only a dead process can
have left one behind.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from sqlalchemy import delete, func, update

from nous.api.execution_context import ExecutionContext
from nous.cognitive.execution_ledger import classify_side_effect, redact_text
from nous.storage.models import LEDGER_STATUSES, ExecutionLedgerEntry

logger = logging.getLogger(__name__)

KEY_ARG_CHARS = 200
RESULT_SUMMARY_CHARS = 500
_CLOSABLE = ("pending", "unknown")
_TERMINAL = frozenset(s for s in LEDGER_STATUSES if s != "pending")

# Per-tool durable argument policy. Pattern redaction cannot be trusted with
# free text -- a bare `sk-...` key matches no pattern -- so nothing is kept
# because it "looks safe". A value is kept only when its SHAPE proves it holds
# no free text (a UUID, a lowercase enum word, validated recipients, a URL cut
# to scheme://host), or when it is the call's target path, which is what the
# row records. Every other value -- and any value that fails its shape -- is
# stored as sha256 + length. A tool not listed here stores its argument NAMES
# only.
_WORD, _UUID, _EMAILS, _CHAT, _PATH, _SOURCE, _HASH = (
    "word", "uuid", "emails", "chat", "path", "source", "hash",
)
_DURABLE_ARGS: dict[str, dict[str, str]] = {
    # A bash command is code: hashed like run_python's, never kept.
    "bash": {"command": _HASH},
    "write_file": {"path": _PATH, "content": _HASH},
    "run_python": {"code": _HASH},
    "send_email": {"to": _EMAILS, "cc": _EMAILS, "subject": _HASH, "body": _HASH, "html_body": _HASH},
    "send_file": {"file_path": _PATH, "chat_id": _CHAT, "caption": _HASH},
    "learn_fact": {"category": _WORD, "subject": _HASH, "content": _HASH},
    "learn_skill": {"source": _SOURCE, "content": _HASH},
    "record_decision": {"category": _WORD, "stakes": _WORD, "description": _HASH},
    "create_censor": {"domain": _WORD, "action": _WORD, "reason": _HASH, "trigger_pattern": _HASH},
    "spawn_task": {"frame_type": _WORD, "task": _HASH},
    "spawn_sync": {"frame_type": _WORD, "task": _HASH},
    "schedule_task": {"frame_type": _WORD, "every": _HASH, "when": _HASH, "task": _HASH},
    "cancel_task": {"task_id": _UUID},
    "heartbeat_check_create": {"name": _HASH, "prompt": _HASH},
    "heartbeat_check_manage": {"action": _WORD, "name": _HASH},
    "dag_create": {"name": _HASH, "nodes": _HASH},
    "dag_manage": {"action": _WORD, "dag_id": _UUID, "node_name": _HASH},
    "push_surface": {"template": _WORD, "dedup_key": _HASH, "params": _HASH},
    "compose_surface": {"archetype": _WORD, "dedup_key": _HASH, "intent": _HASH, "data_sources": _HASH},
    "resolve_decision": {
        "decision_id": _UUID, "outcome": _WORD, "superseded_by": _UUID, "resolution_note": _HASH,
    },
    "resolve_decisions": {"resolutions": _HASH},
    "ingest_document": {"source_ref": _SOURCE, "episode_id": _UUID, "content": _HASH},
    "store_identity": {"section": _WORD, "content": _HASH},
}

_WORD_SHAPE = re.compile(r"[a-z][a-z_]{0,31}")
_EMAIL_SHAPE = re.compile(r"[^@\s,;<>\"']+@[^@\s,;<>\"']+\.[A-Za-z]{2,}")
_CHAT_SHAPE = re.compile(r"-?\d{1,20}")
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


def _digest(value: Any) -> tuple[str, int]:
    text = value if isinstance(value, str) else repr(value)
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16], len(text)


def _shaped(kind: str, value: Any) -> str | None:
    """``value`` in durable form if its shape proves it is not free text, else None."""
    if kind == _EMAILS:
        items = value if isinstance(value, list) else re.split(r"[,;]", str(value))
        parts = [str(v).strip() for v in items if str(v).strip()]
        if parts and all(_EMAIL_SHAPE.fullmatch(v) for v in parts):
            return ",".join(parts)[:KEY_ARG_CHARS]
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    text = str(value)
    if kind == _WORD:
        return text if _WORD_SHAPE.fullmatch(text) else None
    if kind == _UUID:
        try:
            return str(UUID(text))
        except ValueError:
            return None
    if kind == _CHAT:
        return text if _CHAT_SHAPE.fullmatch(text) else None
    if kind == _PATH:
        # Kept, not hashed: the target path is what the row records. Too long
        # or carrying control characters and it is not a path worth trusting.
        if len(text) > KEY_ARG_CHARS or _CONTROL_CHARS.search(text) or "://" in text:
            return None
        return redact_text(text)
    if kind == _SOURCE:
        if text == "inline":
            return text
        parts = urlsplit(text)
        if parts.scheme in ("http", "https") and parts.hostname:
            return f"{parts.scheme}://{parts.hostname}"
        return None
    return None


def durable_key_args(tool_name: str, args: dict[str, Any]) -> dict[str, str]:
    """What the durable ledger may keep about a call's arguments (see _DURABLE_ARGS)."""
    policy = _DURABLE_ARGS.get(tool_name)
    if policy is None:
        return {"arg_names": ",".join(sorted(str(k) for k in args))}
    out: dict[str, str] = {}
    for name, kind in policy.items():
        value = args.get(name)
        if value is None:
            continue
        kept = _shaped(kind, value) if kind != _HASH else None
        if kept is not None:
            out[name] = kept
        # A source keeps its full hash beside the host, so a later match on
        # the exact value is still possible.
        if kept is None or (kind == _SOURCE and kept != "inline"):
            sha, length = _digest(value)
            out[f"{name}_sha256"] = sha
            out[f"{name}_len"] = str(length)
    return out


def effective_orphan_threshold(settings: Any) -> float:
    """Never sweep a row whose call may legitimately still be running."""
    longest = max(
        getattr(settings, "dag_node_max_timeout", 0) or 0,
        getattr(settings, "subtask_max_timeout", 0) or 0,
        getattr(settings, "tool_timeout", 0) or 0,
    )
    return float(max(settings.execution_ledger_pending_unknown_after_seconds, longest + 600))


class LedgerWriteError(Exception):
    """A ledger write failed or timed out. ``entry_id`` may exist if the COMMIT landed."""

    def __init__(self, entry_id: UUID, cause: BaseException) -> None:
        super().__init__(f"execution ledger write failed for {entry_id}: {cause!r}")
        self.entry_id = entry_id


# bash_tool always appends this trailer; it is the authoritative wrapper status.
_BASH_EXIT_CODE = re.compile(r"(?:\A|\n)Exit code: (-?\d+)\s*\Z")
_BASH_TIMEOUT = re.compile(r"\ACommand timed out after \d+s\.")


def _summary(text: str | None, output_of: str | None = None) -> str | None:
    """Durable form of a result.

    ``output_of`` names the tool when ``text`` is that tool's output, which is
    never stored: handlers echo their arguments back (a fact's subject, a
    subtask's answer, a quoted email body) and bash / run_python print
    whatever the code prints (`cat .env; touch x` is a write whose output is a
    secrets file). None of that is vettable by pattern redaction, and the
    arguments are already hashed in key_args. Only the shape survives: its
    length, plus bash's authoritative exit-code trailer and timeout line.
    Harness-authored notes (outcome unknown, refusals) are kept, redacted.
    """
    if not text:
        return None
    if output_of is not None:
        parts: list[str] = []
        if output_of == "bash":
            if timeout := _BASH_TIMEOUT.match(text):
                parts.append(timeout.group(0))
            if exit_code := _BASH_EXIT_CODE.search(text):
                parts.append(f"exit code {exit_code.group(1)}")
        parts.append(f"{len(text)} chars of output, not stored")
        return "; ".join(parts)
    return redact_text(text)[:RESULT_SUMMARY_CHARS]


class LedgerStore:
    """Writes ``nous_system.execution_ledger`` (migration 074)."""

    def __init__(self, database: Any, agent_id: str, *, write_timeout_seconds: float = 2.0) -> None:
        self._db = database
        self._agent_id = agent_id
        self._timeout = write_timeout_seconds

    async def open_entry(
        self, *, context: ExecutionContext, tool_name: str,
        tool_input: dict[str, Any], turn: int | None,
    ) -> UUID | None:
        """Insert a 'pending' row. ``None`` only when the call is not side-effecting."""
        return await self._insert(context, tool_name, tool_input, turn, "pending", None)

    async def record_blocked(
        self, *, context: ExecutionContext, tool_name: str,
        tool_input: dict[str, Any], turn: int | None, reason: str,
    ) -> None:
        """A side-effecting call the harness refused: one terminal row."""
        await self._insert(context, tool_name, tool_input, turn, "blocked", reason)

    async def close_entry(
        self, entry_id: UUID, *, status: str, result_summary: str | None,
        output_of: str | None = None,
    ) -> None:
        """Move a row out of 'pending' (or a sweep-set 'unknown') to ``status``.

        Pass ``output_of`` (the tool name) when ``result_summary`` is the
        tool's raw output, so it is stored in the form that tool allows.
        """
        if status not in _TERMINAL:
            raise ValueError(f"cannot close a ledger row as {status!r}")

        async def _close() -> None:
            async with self._db.session() as s:
                await s.execute(
                    update(ExecutionLedgerEntry)
                    .where(ExecutionLedgerEntry.id == entry_id)
                    .where(ExecutionLedgerEntry.agent_id == self._agent_id)
                    .where(ExecutionLedgerEntry.status.in_(_CLOSABLE))
                    .values(
                        status=status,
                        result_summary=_summary(result_summary, output_of),
                        completed_at=func.now(),
                    )
                )
                await s.commit()

        try:
            await asyncio.wait_for(_close(), timeout=self._timeout)
        except Exception as exc:
            raise LedgerWriteError(entry_id, exc) from exc

    async def mark_orphans_unknown(self, *, older_than_seconds: float | None) -> int:
        """Pending rows → 'unknown'. ``None`` = every pending row (process startup)."""
        stmt = (
            update(ExecutionLedgerEntry)
            .where(ExecutionLedgerEntry.agent_id == self._agent_id)
            .where(ExecutionLedgerEntry.status == "pending")
        )
        if older_than_seconds is not None:
            cutoff = datetime.now(UTC) - timedelta(seconds=older_than_seconds)
            stmt = stmt.where(ExecutionLedgerEntry.created_at < cutoff)
        async with self._db.session() as s:
            result = await s.execute(stmt.values(
                status="unknown",
                result_summary="no report back from the call — outcome unknown",
                completed_at=func.now(),
            ))
            await s.commit()
            return result.rowcount or 0

    async def prune(self, *, retention_days: int) -> int:
        """Delete this agent's CLOSED rows older than ``retention_days``.

        Never a 'pending' row: retention and the call timeouts are configured
        independently, so a live long-running call can be older than the
        window, and deleting its row would make the call vanish -- the owner's
        close would then update nothing. The orphan sweep closes a genuinely
        dead 'pending' row first; retention takes it after that.
        """
        cutoff = datetime.now(UTC) - timedelta(days=retention_days)
        async with self._db.session() as s:
            result = await s.execute(
                delete(ExecutionLedgerEntry)
                .where(ExecutionLedgerEntry.agent_id == self._agent_id)
                .where(ExecutionLedgerEntry.status != "pending")
                .where(ExecutionLedgerEntry.created_at < cutoff)
            )
            await s.commit()
            return result.rowcount or 0

    async def _insert(
        self, context: ExecutionContext, tool_name: str, tool_input: dict[str, Any],
        turn: int | None, status: str, result_summary: str | None,
    ) -> UUID | None:
        entry_id = uuid4()
        try:
            if not isinstance(tool_input, dict):
                raise TypeError(f"tool_input must be a dict, got {type(tool_input).__name__}")
            side_effect = classify_side_effect(tool_name, tool_input)
            if side_effect == "none":
                return None
            row = ExecutionLedgerEntry(
                id=entry_id,
                agent_id=self._agent_id,
                session_id=context.session_id,
                parent_session_id=context.parent_session_id,
                context_kind=context.kind,
                subtask_id=context.subtask_id,
                dag_id=context.dag_id,
                dag_node_id=context.dag_node_id,
                turn=turn,
                tool_name=tool_name,
                side_effect_type=side_effect,
                key_args=durable_key_args(tool_name, tool_input),
                status=status,
                result_summary=_summary(result_summary),
                completed_at=None if status == "pending" else datetime.now(UTC),
            )

            async def _write() -> None:
                async with self._db.session() as s:
                    s.add(row)
                    await s.commit()

            await asyncio.wait_for(_write(), timeout=self._timeout)
        except Exception as exc:
            raise LedgerWriteError(entry_id, exc) from exc
        return entry_id
