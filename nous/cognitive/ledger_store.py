"""Durable execution ledger (harness-autonomy roadmap, Phase 1b).

The in-memory F026 ExecutionLedger stays the per-session prompt aid. This
store is the durable record of side-effecting tool calls: a row is written
'pending' BEFORE dispatch and closed after, so "did it happen?" is a query.

The store RAISES on write failure (LedgerWriteError, carrying the
client-generated id — the COMMIT may have landed even when the wait timed
out). The RUNNER decides what a failure means: Phase 1b fails open; Phase 2b
makes keyed sends fail closed. Rows never hold bodies, code, or the values
of an unknown tool's arguments (durable_key_args).

Deployment assumption: ONE Nous process per (database, agent_id). The startup
sweep marks every 'pending' row 'unknown' because only a dead process can
have left one behind.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import UTC, datetime, timedelta
from typing import Any
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

# Per-tool durable argument policy: (values kept after redaction, values
# stored only as sha256 + length). A tool not listed here stores its argument
# NAMES only — an unknown tool's values may be anything, including secrets.
_DURABLE_ARGS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "bash": (("command",), ()),
    "write_file": (("path",), ("content",)),
    "run_python": ((), ("code",)),
    "send_email": (("to", "cc", "subject"), ("body", "html_body")),
    "send_file": (("file_path", "chat_id"), ("caption",)),
    "learn_fact": (("subject", "category"), ("content",)),
    "learn_skill": (("source",), ("content",)),
    "record_decision": (("category", "stakes"), ("description",)),
    "create_censor": (("domain", "action"), ("reason", "trigger_pattern")),
    "spawn_task": (("frame_type",), ("task",)),
    "spawn_sync": (("frame_type",), ("task",)),
    "schedule_task": (("every", "when", "frame_type"), ("task",)),
    "cancel_task": (("task_id",), ()),
    "heartbeat_check_create": (("name",), ("prompt",)),
    "heartbeat_check_manage": (("action", "name"), ()),
    "dag_create": (("name",), ("nodes",)),
    "dag_manage": (("action", "dag_id", "node_name"), ()),
    "push_surface": (("template", "dedup_key"), ("params",)),
    "compose_surface": (("dedup_key", "archetype"), ("intent", "data_sources")),
    "resolve_decision": (("decision_id", "outcome", "superseded_by"), ("resolution_note",)),
    "resolve_decisions": ((), ("resolutions",)),
    "ingest_document": (("source_ref", "episode_id"), ("content",)),
    "store_identity": (("section",), ("content",)),
}


def _digest(value: Any) -> tuple[str, int]:
    text = value if isinstance(value, str) else repr(value)
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16], len(text)


def durable_key_args(tool_name: str, args: dict[str, Any]) -> dict[str, str]:
    """What the durable ledger may keep about a call's arguments.

    Redact the FULL value, then truncate — truncating first can cut a secret
    in half and defeat the pattern that would have matched it.
    """
    policy = _DURABLE_ARGS.get(tool_name)
    if policy is None:
        return {"arg_names": ",".join(sorted(str(k) for k in args))}
    keep, hashed = policy
    out: dict[str, str] = {}
    for name in keep:
        if name in args and args[name] is not None:
            out[name] = redact_text(str(args[name]))[:KEY_ARG_CHARS]
    for name in hashed:
        if name in args and args[name] is not None:
            sha, length = _digest(args[name])
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


def _summary(text: str | None) -> str | None:
    if not text:
        return None
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
    ) -> None:
        """Move a row out of 'pending' (or a sweep-set 'unknown') to ``status``."""
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
                        result_summary=_summary(result_summary),
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
        """Delete this agent's rows older than ``retention_days``."""
        cutoff = datetime.now(UTC) - timedelta(days=retention_days)
        async with self._db.session() as s:
            result = await s.execute(
                delete(ExecutionLedgerEntry)
                .where(ExecutionLedgerEntry.agent_id == self._agent_id)
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
