"""F108 Phase 3: tool notes, short capped notes about repeated tool mistakes.

This part holds the table's store. ``ToolNoteStore`` keeps the notes, and also the "declined" and "guard" rows
that stop a pattern from being offered to the review again for ``REREVIEW_DAYS`` days. Every query is scoped to
the agent, a retired note is soft deleted, and the caps are checked in the transaction that writes.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from nous.observability.tool_patterns import Pattern
from nous.storage.database import Database

logger = logging.getLogger(__name__)

NOTES_PER_TOOL = 3  # active notes of one tool
NOTES_TOTAL = 40  # active notes in all
NOTE_MAX_CHARS = 300  # also a CHECK in migration 103
REREVIEW_DAYS = 14  # a pattern with a note row this recent is not offered to the review again
CLOSE_REASONS = ("declined", "guard")  # why a pattern got an inactive row with no note


class NoteCapError(ValueError):
    """A note would take its tool past ``NOTES_PER_TOOL`` or the agent past ``NOTES_TOTAL``."""


def _pattern_id(kind: str, tool_name: str, key: str) -> str:
    """A pattern id, built by ``Pattern.id`` itself so the format has one definition."""
    return Pattern(kind=kind, tool_name=tool_name, key=key, hits=0, calls=0, guard=None, example="").id


@dataclass(frozen=True)
class StoredNote:
    """One active note, with the baseline of its pattern at the time it was written."""

    id: UUID
    tool_name: str
    pattern_kind: str
    pattern_key: str
    note_text: str
    baseline_rate: float
    baseline_calls: int
    baseline_hits: int
    created_at: datetime
    judged: dict[str, Any] | None

    @property
    def pattern_id(self) -> str:
        return _pattern_id(self.pattern_kind, self.tool_name, self.pattern_key)


_ACTIVE_SQL = text(
    """
    SELECT id, tool_name, pattern_kind, pattern_key, note_text, baseline_rate, baseline_calls, baseline_hits,
           created_at, judged
    FROM nous_system.tool_note
    WHERE agent_id = :agent AND active AND note_text <> ''
    ORDER BY created_at DESC, id
    """
)

_REVIEWED_SQL = text(
    """
    SELECT DISTINCT pattern_kind, tool_name, pattern_key
    FROM nous_system.tool_note
    WHERE agent_id = :agent AND greatest(created_at, coalesce(retired_at, created_at)) >= :since
    """
)

_INSERT_SQL = text(
    """
    INSERT INTO nous_system.tool_note
        (agent_id, tool_name, pattern_kind, pattern_key, note_text, baseline_rate, baseline_calls, baseline_hits,
         active, retired, created_at, retired_at)
    VALUES
        (:agent, :tool, :kind, :key, :note, :rate, :calls, :hits, :active, CAST(:retired AS jsonb), :at, :retired_at)
    RETURNING id
    """
)

_RETIRE_SQL = text(
    """
    UPDATE nous_system.tool_note
    SET active = FALSE, retired = CAST(:retired AS jsonb), retired_at = :at
    WHERE agent_id = :agent AND id = :id AND active
    RETURNING tool_name
    """
)


def _json(value: dict[str, Any]) -> str:
    return json.dumps(value, default=lambda o: o.isoformat() if isinstance(o, datetime) else str(o))


class ToolNoteStore:
    """The tool-note table for one agent."""

    def __init__(self, database: Database, agent_id: str) -> None:
        self._db = database
        self._agent_id = agent_id

    async def active(self) -> list[StoredNote]:
        """The active notes with text, newest first."""
        async with self._db.session() as session:
            rows = (await session.execute(_ACTIVE_SQL, {"agent": self._agent_id})).all()
        return [
            StoredNote(
                id=r.id,
                tool_name=r.tool_name,
                pattern_kind=r.pattern_kind,
                pattern_key=r.pattern_key,
                note_text=r.note_text,
                baseline_rate=r.baseline_rate,
                baseline_calls=r.baseline_calls,
                baseline_hits=r.baseline_hits,
                created_at=r.created_at,
                judged=r.judged,
            )
            for r in rows
        ]

    async def recently_reviewed(self, now: datetime, days: int = REREVIEW_DAYS) -> set[str]:
        """Pattern ids with any row (note, retired note, declined or guard) created or retired in the last ``days``."""
        async with self._db.session() as session:
            rows = (
                await session.execute(_REVIEWED_SQL, {"agent": self._agent_id, "since": now - timedelta(days=days)})
            ).all()
        return {_pattern_id(r.pattern_kind, r.tool_name, r.pattern_key) for r in rows}

    async def add(self, p: Pattern, text_: str, *, now: datetime, replaces: UUID | None = None) -> UUID:
        """Store a note for ``p``. One transaction: retire ``replaces`` (reason ``rewritten``), check the caps, insert.

        Raises ``NoteCapError`` when a cap would be exceeded after the replace and ``ValueError`` for empty or
        over-long text, for ``replaces`` that is not an active note, and for a second active note on one pattern.
        """
        body = text_.strip()
        if not body:
            raise ValueError("note text is empty")
        if len(body) > NOTE_MAX_CHARS:
            raise ValueError(f"note text is {len(body)} characters, the limit is {NOTE_MAX_CHARS}")
        params = {"agent": self._agent_id}
        async with self._db.session() as session:
            try:
                # One writer at a time per agent, so the cap counts below cannot race another add.
                await session.execute(
                    text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"), {"k": f"tool_note:{self._agent_id}"}
                )
                if replaces is not None:
                    done = await session.execute(
                        _RETIRE_SQL,
                        {**params, "id": replaces, "at": now, "retired": _json({"reason": "rewritten", "at": now})},
                    )
                    if done.first() is None:
                        raise ValueError("replaces is not an active note of this agent")
                counts = (
                    await session.execute(
                        text(
                            "SELECT count(*) AS total, count(*) FILTER (WHERE tool_name = :tool) AS per_tool "
                            "FROM nous_system.tool_note WHERE agent_id = :agent AND active AND note_text <> ''"
                        ),
                        {**params, "tool": p.tool_name},
                    )
                ).one()
                if counts.per_tool >= NOTES_PER_TOOL:
                    raise NoteCapError(f"{p.tool_name} already has {NOTES_PER_TOOL} notes")
                if counts.total >= NOTES_TOTAL:
                    raise NoteCapError(f"the agent already has {NOTES_TOTAL} notes")
                row = await session.execute(
                    _INSERT_SQL,
                    {
                        **params,
                        "tool": p.tool_name,
                        "kind": p.kind,
                        "key": p.key,
                        "note": body,
                        "rate": p.rate,
                        "calls": p.calls,
                        "hits": p.hits,
                        "active": True,
                        "retired": None,
                        "at": now,
                        "retired_at": None,
                    },
                )
                note_id: UUID = row.scalar_one()
                await session.commit()
                return note_id
            except IntegrityError as exc:
                await session.rollback()
                raise ValueError(f"{p.id} already has an active note") from exc
            except BaseException:
                await session.rollback()
                raise

    async def close(self, p: Pattern, reason: str, *, now: datetime) -> None:
        """Record that ``p`` was reviewed without a note (``declined``) or reported to the owner (``guard``)."""
        if reason not in CLOSE_REASONS:
            raise ValueError(f"reason must be one of {CLOSE_REASONS}")
        async with self._db.session() as session:
            await session.execute(
                _INSERT_SQL,
                {
                    "agent": self._agent_id,
                    "tool": p.tool_name,
                    "kind": p.kind,
                    "key": p.key,
                    "note": "",
                    "rate": p.rate,
                    "calls": p.calls,
                    "hits": p.hits,
                    "active": False,
                    "retired": _json({"reason": reason, "at": now}),
                    "at": now,
                    "retired_at": now,
                },
            )
            await session.commit()

    async def retire(self, note_id: UUID, reason: dict[str, Any], *, now: datetime) -> None:
        """Soft delete a note: inactive, ``retired`` = ``reason`` plus ``at``, ``retired_at`` = ``now``."""
        async with self._db.session() as session:
            await session.execute(
                _RETIRE_SQL,
                {"agent": self._agent_id, "id": note_id, "at": now, "retired": _json({**reason, "at": now})},
            )
            await session.commit()

    async def mark_judged(self, note_id: UUID, judged: dict[str, Any]) -> None:
        """Store the one judgement of a kept note."""
        async with self._db.session() as session:
            await session.execute(
                text(
                    "UPDATE nous_system.tool_note SET judged = CAST(:judged AS jsonb) "
                    "WHERE agent_id = :agent AND id = :id"
                ),
                {"agent": self._agent_id, "id": note_id, "judged": _json(judged)},
            )
            await session.commit()
