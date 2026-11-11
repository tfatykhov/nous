"""F111 obligation ledger: ``brain.commitments`` and ``nous_system.promise_gate_log`` (migration 107).

A commitment is a forward-looking obligation with a deadline: written by the ``commit`` tool (``detected_by='tool'``)
or by the turn-close promise gate (``detected_by='gate'``, ``nous/cognitive/promise_gate.py``) when a reply
promises work that no artifact backs. ``short_id`` is what the owner reads and types: ``C-0042``, ``/drop C-0042``.

Every write is agent-scoped. ``create_idempotent`` is safe to retry: one row per ``(agent_id, dedupe_key)``, where
the key is md5 of the turn key and the normalised ``what``. Nothing here sends, schedules or spawns anything.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import text

logger = logging.getLogger(__name__)

MAX_DUE_DAYS = 90  # a due_at further out than this is refused (a junk commit, not an obligation)
MIN_WHAT_CHARS = 8  # "do it" is not an obligation anyone can check
PROMISE_TEXT_MAX = 500
WHAT_MAX = 300
# Statuses the overdue watcher (PR B) reads, and from which the owner's /drop and /done move a row.
LIVE_STATUSES: frozenset[str] = frozenset({"open", "backed", "proposed"})
# What /drop, /done and the store's callers may move a live row to.
CLOSING_STATUSES: frozenset[str] = frozenset({"done", "broken", "cancelled", "deferred", "superseded"})
# The ledger's source_kind for each turn kind (ExecutionContext.kind); any other turn is a chat turn.
SOURCE_KIND_BY_CONTEXT: dict[str, str] = {
    "subtask": "subtask",
    "dag_node": "dag_node",
    "scheduled": "scheduled",
    "heartbeat_triage": "heartbeat",
    "heartbeat_check": "heartbeat",
    "heartbeat_callback": "heartbeat",
    "continuation": "continuation",
}

_SHORT_ID_RE = re.compile(r"^(?:c-?)?0*(\d{1,12})$", re.IGNORECASE)

_COLUMNS = (
    "id, short_id, status, kind, what, promise_text, due_at, grace_seconds, consent, source_kind, detected_by, "
    "session_id, turn_id, artifact_kind, artifact_id, close_reason, created_at"
)


_INSERT = text(
    "INSERT INTO brain.commitments (agent_id, source_kind, session_id, turn_id, detected_by, detector_version, "
    "promise_text, what, kind, consent, due_at, grace_seconds, status, artifact_kind, artifact_id, artifact_linked_at, "
    "dedupe_key) VALUES (:agent_id, :source_kind, :session_id, :turn_id, :detected_by, :detector_version, "
    ":promise_text, :what, :kind, :consent, :due_at, :grace_seconds, :status, :artifact_kind, :artifact_id, "
    "CASE WHEN :linked THEN now() END, :dedupe_key) "
    f"ON CONFLICT (agent_id, dedupe_key) DO NOTHING RETURNING {_COLUMNS}"
)
_BY_KEY = text(f"SELECT {_COLUMNS} FROM brain.commitments WHERE agent_id = :agent_id AND dedupe_key = :dedupe_key")
_LOG = text(
    "INSERT INTO nous_system.promise_gate_log (agent_id, session_id, turn_id, path, context_kind, mode, detected, "
    "backed, unbacked, outcome, latency_ms) VALUES (:agent_id, :session_id, :turn_id, :path, :context_kind, :mode, "
    ":detected, :backed, :unbacked, :outcome, :latency_ms)"
)


def _log_params(
    *,
    path: str,
    mode: str,
    detected: int,
    backed: int,
    unbacked: int,
    outcome: str,
    session_id: str | None = None,
    turn_id: str | None = None,
    context_kind: str | None = None,
    latency_ms: int | None = None,
) -> dict[str, Any]:
    return {
        "path": path,
        "mode": mode,
        "detected": detected,
        "backed": backed,
        "unbacked": unbacked,
        "outcome": outcome,
        "session_id": session_id,
        "turn_id": turn_id,
        "context_kind": context_kind,
        "latency_ms": latency_ms,
    }


class CommitmentRefused(ValueError):
    """A commitment the store will not write (a past or far deadline, an empty ``what``). The text is for the model."""


@dataclass(frozen=True)
class Commitment:
    id: UUID
    short_id: int
    status: str
    kind: str
    what: str
    promise_text: str
    due_at: datetime
    grace_seconds: int
    consent: str
    source_kind: str
    detected_by: str
    session_id: str | None
    turn_id: str | None
    artifact_kind: str | None
    artifact_id: str | None
    close_reason: str | None
    created_at: datetime
    inserted: bool = True  # False when create_idempotent found the row an earlier write made

    @property
    def label(self) -> str:
        """The owner-facing id, ``C-0042``."""
        return format_short_id(self.short_id)


def format_short_id(short_id: int) -> str:
    """``C-0042``: the form the owner reads in a footer and types after /drop."""
    return f"C-{short_id:04d}"


def parse_short_id(arg: str) -> int | None:
    """``C-0042``, ``c-42``, ``C42`` or ``42`` -> 42; anything else -> None."""
    m = _SHORT_ID_RE.match(arg.strip())
    if not m:
        return None
    value = int(m.group(1))
    return value if value > 0 else None


def normalise_what(what: str) -> str:
    """One line, single spaces, clipped: the form stored and hashed."""
    return " ".join(what.split())[:WHAT_MAX]


def dedupe_key(turn_key: str | None, what: str) -> str:
    """md5(turn key || normalised what): the same promise written twice in one turn is one row."""
    return hashlib.md5(f"{turn_key or ''}{normalise_what(what).lower()}".encode()).hexdigest()


def validate_due_at(due_at: datetime, now: datetime | None = None) -> datetime:
    """The deadline as an aware UTC datetime, or ``CommitmentRefused``: it must be in the future and at most
    ``MAX_DUE_DAYS`` away. A naive datetime is read as UTC."""
    now = now or datetime.now(UTC)
    if due_at.tzinfo is None:
        due_at = due_at.replace(tzinfo=UTC)
    due_at = due_at.astimezone(UTC)
    if due_at <= now:
        raise CommitmentRefused(f"due_at {due_at.isoformat()} is not in the future")
    if due_at > now + timedelta(days=MAX_DUE_DAYS):
        raise CommitmentRefused(f"due_at {due_at.isoformat()} is more than {MAX_DUE_DAYS} days away")
    return due_at


def _row(row: Any, inserted: bool = True) -> Commitment:
    m = row._mapping
    return Commitment(
        id=m["id"],
        short_id=int(m["short_id"]),
        status=m["status"],
        kind=m["kind"],
        what=m["what"],
        promise_text=m["promise_text"],
        due_at=m["due_at"],
        grace_seconds=int(m["grace_seconds"]),
        consent=m["consent"],
        source_kind=m["source_kind"],
        detected_by=m["detected_by"],
        session_id=m["session_id"],
        turn_id=m["turn_id"],
        artifact_kind=m["artifact_kind"],
        artifact_id=m["artifact_id"],
        close_reason=m["close_reason"],
        created_at=m["created_at"],
        inserted=inserted,
    )


class CommitmentStore:
    """Reads and writes ``brain.commitments`` and ``nous_system.promise_gate_log`` for one agent."""

    def __init__(self, database: Any, agent_id: str) -> None:
        self._db = database
        self._agent_id = agent_id

    def _params(
        self,
        *,
        source_kind: str,
        detected_by: str,
        promise_text: str,
        what: str,
        due_at: datetime,
        session_id: str | None = None,
        turn_id: str | None = None,
        kind: str = "action",
        consent: str = "none",
        grace_seconds: int = 900,
        detector_version: str | None = None,
        status: str = "open",
        artifact_kind: str | None = None,
        artifact_id: str | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """The insert's bind values, validated: raises ``CommitmentRefused`` before any SQL runs."""
        what = normalise_what(what)
        if len(what) < MIN_WHAT_CHARS:
            raise CommitmentRefused(f"what must say what will be done (at least {MIN_WHAT_CHARS} characters)")
        return {
            "agent_id": self._agent_id,
            "source_kind": source_kind,
            "session_id": session_id,
            "turn_id": turn_id,
            "detected_by": detected_by,
            "detector_version": detector_version,
            "promise_text": " ".join(promise_text.split())[:PROMISE_TEXT_MAX] or what,
            "what": what,
            "kind": kind,
            "consent": consent,
            "due_at": validate_due_at(due_at, now),
            "grace_seconds": grace_seconds,
            "status": status,
            "artifact_kind": artifact_kind,
            "artifact_id": artifact_id,
            "linked": artifact_id is not None,
            "dedupe_key": dedupe_key(turn_id, what),
        }

    async def _insert(self, session: Any, params: dict[str, Any]) -> Commitment:
        result = await session.execute(_INSERT, params)
        row = result.first()
        if row is not None:
            return _row(row, inserted=True)
        result = await session.execute(_BY_KEY, {"agent_id": self._agent_id, "dedupe_key": params["dedupe_key"]})
        return _row(result.one(), inserted=False)

    async def create_idempotent(self, **kwargs: Any) -> Commitment:
        """Insert the row, or return the one an earlier write of the same promise in the same turn made
        (``inserted`` False). Raises ``CommitmentRefused`` on a bad deadline or a ``what`` too short to check.
        Keyword arguments as ``_params``."""
        params = self._params(**kwargs)
        async with self._db.session() as session:
            row = await self._insert(session, params)
            await session.commit()
        return row

    async def record_gate_turn(self, rows: list[dict[str, Any]], log: dict[str, Any]) -> list[Commitment | None]:
        """The promise gate's writes for one turn, in ONE transaction: a row per promise (``_params`` keywords) and
        the ``promise_gate_log`` row (``log_gate_turn`` keywords). A failure or a cancellation part-way writes
        nothing, so no row exists that the owner was not shown. A row the store refuses is ``None`` and the rest
        are written."""
        prepared: list[dict[str, Any] | None] = []
        for spec in rows:
            try:
                prepared.append(self._params(**spec))
            except CommitmentRefused as refused:
                logger.info("promise gate: a promise was not recorded (%s)", refused)
                prepared.append(None)
        async with self._db.session() as session:
            written = [await self._insert(session, p) if p is not None else None for p in prepared]
            await session.execute(_LOG, {"agent_id": self._agent_id, **_log_params(**log)})
            await session.commit()
        return written

    async def get(self, short_id: int) -> Commitment | None:
        """The row with this short id, or None."""
        async with self._db.session() as session:
            result = await session.execute(
                text(f"SELECT {_COLUMNS} FROM brain.commitments WHERE agent_id = :agent_id AND short_id = :short_id"),
                {"agent_id": self._agent_id, "short_id": short_id},
            )
            row = result.first()
        return _row(row) if row is not None else None

    async def link_artifact(self, short_id: int, artifact_kind: str, artifact_id: str) -> Commitment | None:
        """Link an artifact to a live row and mark it ``backed``. None when no live row has that id."""
        async with self._db.session() as session:
            result = await session.execute(
                text(
                    "UPDATE brain.commitments SET status = 'backed', artifact_kind = :kind, artifact_id = :artifact, "
                    "artifact_linked_at = now(), updated_at = now() "
                    "WHERE agent_id = :agent_id AND short_id = :short_id AND status IN ('open', 'backed', 'proposed') "
                    f"RETURNING {_COLUMNS}"
                ),
                {"agent_id": self._agent_id, "short_id": short_id, "kind": artifact_kind, "artifact": artifact_id},
            )
            row = result.first()
            await session.commit()
        return _row(row) if row is not None else None

    async def list_watchable(self, now: datetime | None = None, limit: int = 100) -> list[Commitment]:
        """Live rows past ``due_at + grace`` with no artifact: what the PR B watcher will report. Never a
        ``goal`` or ``awaiting_user`` row, and never a cancelled, deferred or superseded one."""
        async with self._db.session() as session:
            result = await session.execute(
                text(
                    f"SELECT {_COLUMNS} FROM brain.commitments "
                    "WHERE agent_id = :agent_id AND status IN ('open', 'proposed') AND artifact_id IS NULL "
                    "AND kind NOT IN ('goal', 'awaiting_user') "
                    "AND due_at + make_interval(secs => grace_seconds) < :now "
                    "ORDER BY due_at LIMIT :limit"
                ),
                {"agent_id": self._agent_id, "now": now or datetime.now(UTC), "limit": limit},
            )
            rows = result.all()
        return [_row(r) for r in rows]

    async def mark(
        self, short_id: int, status: str, *, close_reason: str | None = None
    ) -> tuple[Commitment | None, bool]:
        """Move a live row to a closing status: ``(row, changed)``. ``(row, False)`` when the row exists but is no
        longer live (a repeat /drop changes nothing), ``(None, False)`` when no row has that id."""
        if status not in CLOSING_STATUSES:
            raise ValueError(f"status {status!r} is not a closing status")
        async with self._db.session() as session:
            result = await session.execute(
                text(
                    "UPDATE brain.commitments SET status = :status, close_reason = :reason, closed_at = now(), "
                    "updated_at = now() "
                    "WHERE agent_id = :agent_id AND short_id = :short_id AND status IN ('open', 'backed', 'proposed') "
                    f"RETURNING {_COLUMNS}"
                ),
                {"agent_id": self._agent_id, "short_id": short_id, "status": status, "reason": close_reason},
            )
            row = result.first()
            await session.commit()
        if row is not None:
            return _row(row), True
        return await self.get(short_id), False

    async def log_gate_turn(self, **log: Any) -> None:
        """One ``promise_gate_log`` row on its own (the gate's error row); keywords as ``_log_params``."""
        async with self._db.session() as session:
            await session.execute(_LOG, {"agent_id": self._agent_id, **_log_params(**log)})
            await session.commit()

    async def stats_week(self, now: datetime | None = None) -> dict[str, Any]:
        """The last 7 days: the gate's counts (turns, promises detected, backed and unbacked, footers, errors) and
        the commitments created, by status. For the weekly review; no ratio is computed here."""
        since = (now or datetime.now(UTC)) - timedelta(days=7)
        params = {"agent_id": self._agent_id, "since": since}
        async with self._db.session() as session:
            gate = (
                await session.execute(
                    text(
                        "SELECT count(*) AS turns, coalesce(sum(detected), 0) AS detected, "
                        "coalesce(sum(backed), 0) AS backed, coalesce(sum(unbacked), 0) AS unbacked, "
                        "count(*) FILTER (WHERE outcome = 'footer') AS footers, "
                        "count(*) FILTER (WHERE outcome = 'error') AS errors "
                        "FROM nous_system.promise_gate_log WHERE agent_id = :agent_id AND created_at >= :since"
                    ),
                    params,
                )
            ).one()
            by_status = (
                await session.execute(
                    text(
                        "SELECT status, count(*) AS n FROM brain.commitments "
                        "WHERE agent_id = :agent_id AND created_at >= :since GROUP BY status"
                    ),
                    params,
                )
            ).all()
        g = gate._mapping
        return {
            "since": since,
            "gate": {k: int(g[k]) for k in ("turns", "detected", "backed", "unbacked", "footers", "errors")},
            "commitments": {r._mapping["status"]: int(r._mapping["n"]) for r in by_status},
        }
