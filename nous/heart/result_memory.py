"""F098 Phase C: a finished background subtask result becomes memory.

Subtask turns run with ``skip_episode=True``, so a result lived only in
``heart.subtasks.result``, which ``recall_deep`` does not search. This module
writes a conversation-originated result (tier 1) and, behind a second flag, a
substantive scheduled ``notify=true`` result (tier 2) as:

* one closed episode whose summary is a deterministic header (marked as
  unverified subtask output, with the subtask id) plus the head of the result;
* the full text as F069 document chunks (``source_ref='subtask:<id>'``).

No LLM call, no ``session_ended`` event, so no fact extraction: web-research
output is the main source of plausible-but-wrong facts.

``heart.result_memory_log`` (migration 082) holds one write-or-skip decision
per source and makes the write idempotent: the primary key decides which
writer owns a source, the episode and its ``episode_id`` commit together, and
the chunk step is idempotent on ``(episode_id, source_ref)``. A failed or
abandoned write is retried by :class:`ResultMemoryPass`, a pass on the
:class:`~nous.heart.result_reconciler.TerminalSubtaskReconciler`.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import and_, exists, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from nous.heart.result_inbox import _aware, _percentile, is_dag_node_subtask
from nous.heart.schemas import EpisodeInput
from nous.heart.subtasks import INLINE_WORKER_ID
from nous.security.secrets import scan_secrets
from nous.storage.models import ResultMemoryLog, Subtask

if TYPE_CHECKING:  # pragma: no cover - typing only
    from nous.config import Settings
    from nous.heart.heart import Heart

logger = logging.getLogger(__name__)

SOURCE_SUBTASK = "subtask"
TRIGGER = "subtask_result"

# Launcher stubs: a scheduled task that creates a DAG and exits reports a
# receipt; the substance arrives later through the DAG's F087 summary episode.
STUB_MAX_CHARS = 600
STUB_RESULT_RE = re.compile(r"(?i)\b(dag|execution dag)\b.{0,80}\b(created|launched|started|id)\b", re.DOTALL)
STUB_TASK_RE = re.compile(r"(?i)create a DAG and exit|do NOT execute (the stages )?inline")

HEADER = "[Background subtask result — unverified output, not reviewed by Tim]"
_TITLE_TASK_CHARS = 120
_SUMMARY_TASK_CHARS = 300
_ERROR_MAX = 500
# A failed or abandoned ('pending') write is retried once it is this old.
RETRY_AFTER = timedelta(minutes=10)
# The reconciler abandons a pass after 30 s; stop starting writes before that.
_PASS_BUDGET_SECONDS = 20.0

# Strong references to fire-and-forget writes: asyncio holds only weak ones.
_pending_tasks: set[asyncio.Task[Any]] = set()


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MemoryDecision:
    decision: str  # write | skip
    reason: str

    @property
    def write(self) -> bool:
        return self.decision == "write"


def _write(reason: str) -> MemoryDecision:
    return MemoryDecision("write", reason)


def _skip(reason: str) -> MemoryDecision:
    return MemoryDecision("skip", reason)


def result_text(subtask: Any) -> str:
    """The text a result memory is made of: the result, led by the error on a failure."""
    result = (subtask.result or "").strip()
    if subtask.status == "failed" and (subtask.error or "").strip():
        return f"Error: {subtask.error.strip()}" + (f"\n\n{result}" if result else "")
    return result


def has_conversation_origin(subtask: Any) -> bool:
    return bool(getattr(subtask, "parent_channel", None) or subtask.parent_session_id)


def is_launcher_stub(task: str, result: str) -> bool:
    """A "create a DAG and exit" receipt rather than a result."""
    if len(result) < STUB_MAX_CHARS and STUB_RESULT_RE.search(result):
        return True
    return bool(STUB_TASK_RE.search(task or ""))


def classify_for_memory(subtask: Any, settings: Settings) -> MemoryDecision:
    """Decide whether a terminal subtask's result becomes memory (§3.1).

    The first matching rule wins. There is no per-spawn override (§8 Q3).
    """
    if is_dag_node_subtask(subtask):
        return _skip("dag_node")  # its DAG's F087 summary episode is the memory
    origin = has_conversation_origin(subtask)
    if getattr(subtask, "worker_id", None) == INLINE_WORKER_ID and not origin:
        return _skip("inline")
    if subtask.status not in ("completed", "failed"):
        return _skip("status")
    text = result_text(subtask)
    if len(text) < settings.result_memory_min_chars:
        return _skip("too_short")
    if origin:
        decision = _write("tier1")  # failures too: "we tried X, it failed because Y"
    elif getattr(subtask, "notify", False):
        if not settings.result_memory_scheduled:
            return _skip("scheduled_off")
        if subtask.status == "failed":
            return _skip("scheduled_failure")  # the scheduler/heartbeat already surface it
        if is_launcher_stub(subtask.task, text):
            return _skip("launcher_stub")
        decision = _write("tier2")
    else:
        return _skip("background")
    if scan_secrets(f"{subtask.task}\n{text}"):
        return _skip("secret_detected")
    return decision


# ---------------------------------------------------------------------------
# Episode content
# ---------------------------------------------------------------------------


def _one_line(text: str, limit: int) -> str:
    line = " ".join((text or "").split())
    return line if len(line) <= limit else line[: limit - 1].rstrip() + "…"


def _head(text: str, limit: int) -> str:
    """The first ``limit`` chars, cut at a paragraph or sentence boundary when one is near."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    para = cut.rfind("\n\n")
    if para >= limit // 2:
        return cut[:para].rstrip() + "\n…"
    sentence = max(cut.rfind(". "), cut.rfind(".\n"), cut.rfind("! "), cut.rfind("? "))
    if sentence >= limit // 2:
        return cut[: sentence + 1] + " …"
    return cut.rstrip() + "…"


def template_key(task: str) -> str:
    """Stable key of a recurring task: sha1 of its first 60 normalised chars, 12 hex."""
    norm = " ".join((task or "").lower().split())[:60]
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()[:12]


def build_episode_input(subtask: Any, text: str, tier: str, settings: Settings) -> EpisodeInput:
    first_line = (subtask.task or "subtask").strip().splitlines()[0] if (subtask.task or "").strip() else "subtask"
    finished = subtask.completed_at.isoformat() if subtask.completed_at else "unknown"
    summary = "\n".join(
        [
            HEADER,
            f"Task: {_one_line(subtask.task or '', _SUMMARY_TASK_CHARS)}",
            f"Status: {subtask.status} · Finished: {finished} · Subtask: {subtask.id}",
            _head(text, settings.result_memory_summary_chars),
        ]
    )
    frame = subtask.frame_type or "task"
    tags = ["subtask-result", f"tier:{tier[-1]}", f"status:{subtask.status}", f"frame:{frame}"]
    if tier == "tier2":
        tags.append(f"recurring:{template_key(subtask.task)}")
    return EpisodeInput(
        title=f"Subtask result: {_one_line(first_line, _TITLE_TASK_CHARS)}",
        summary=summary,
        frame_used=frame,
        trigger=TRIGGER,
        participants=["nous"],
        tags=tags,
        session_id=episode_session_id(subtask.id),
    )


def episode_session_id(subtask_id: UUID) -> str:
    return f"subtask-result:{subtask_id}"


def chunk_source_ref(subtask_id: UUID) -> str:
    return f"subtask:{subtask_id}"


def _cap(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n[... truncated at {limit} chars — full text on the subtask row]"


async def _ingest_chunks(heart: Heart, settings: Settings, *, content: str, source_ref: str, episode_id: UUID) -> dict:
    """The F069 document chunker (lazy import: nous.api.tools imports Heart)."""
    from nous.api.tools import ingest_document_text

    return await ingest_document_text(
        heart, settings, content=content, source_ref=source_ref, episode_id=str(episode_id)
    )


# ---------------------------------------------------------------------------
# Writer
# ---------------------------------------------------------------------------


class ResultMemoryWriter:
    """Writes one source's result memory, exactly once (§3.3)."""

    def __init__(self, heart: Heart) -> None:
        self._heart = heart
        self._db = heart.db
        self._settings = heart.settings
        self._agent_id = heart.agent_id

    @property
    def enabled(self) -> bool:
        return self._settings.result_memory_enabled is True

    async def record_id(self, subtask_id: UUID) -> str | None:
        """Re-read the subtask (its committed terminal state) and record it. Never raises."""
        if not self.enabled:
            return None
        try:
            subtask = await self._heart.subtasks.get(subtask_id)
        except Exception:
            logger.warning("F098: could not re-read subtask %s for memory", subtask_id.hex[:8], exc_info=True)
            return None
        return await self.record(subtask)

    async def record(self, subtask: Any) -> str | None:
        """Classify, then write or log the skip. Returns the row's final state. Never raises."""
        if not self.enabled or subtask is None or subtask.status in ("pending", "running"):
            return None
        sid = subtask.id.hex[:8]
        try:
            decision = classify_for_memory(subtask, self._settings)
            row = await self._claim(subtask.id, decision)
        except Exception:
            logger.warning("F098: result memory log write failed for subtask %s", sid, exc_info=True)
            return None
        if row is None:
            return None  # another writer owns it, or nothing left to do
        if row.decision == "skip":
            logger.info("F098: result memory skip for subtask %s: %s", sid, row.reason)
            return "skipped"
        try:
            await self._write(subtask, row)
        except Exception as exc:
            await self._mark_failed(subtask.id, exc)
            logger.warning("F098: result memory write failed for subtask %s", sid, exc_info=True)
            return "failed"
        logger.info("F098: result memory written for subtask %s (%s)", sid, row.reason)
        return "written"

    def _pk(self, source_id: UUID) -> Any:
        return and_(
            ResultMemoryLog.agent_id == self._agent_id,
            ResultMemoryLog.source_kind == SOURCE_SUBTASK,
            ResultMemoryLog.source_id == source_id,
        )

    async def _claim(self, source_id: UUID, decision: MemoryDecision) -> ResultMemoryLog | None:
        """Own this source's log row, or return None when another writer owns it.

        A new row is inserted with the decision (a skip goes straight to
        ``skipped``). An existing write row is taken over only when it is
        retryable: ``failed`` under the attempt cap, or ``pending`` and
        abandoned (its writer crashed), in either case untouched for
        ``RETRY_AFTER``. The takeover is one conditional UPDATE, so of two
        racing writers exactly one wins. A taken-over row keeps its original
        decision.
        """
        now = datetime.now(UTC)
        stmt = (
            pg_insert(ResultMemoryLog)
            .values(
                agent_id=self._agent_id,
                source_kind=SOURCE_SUBTASK,
                source_id=source_id,
                decision=decision.decision,
                reason=decision.reason,
                state="pending" if decision.write else "skipped",
                created_at=now,
                updated_at=now,
            )
            .on_conflict_do_nothing(index_elements=["agent_id", "source_kind", "source_id"])
        )
        async with self._db.session() as session:
            inserted = (await session.execute(stmt)).rowcount
            if not inserted:
                stale = now - RETRY_AFTER
                taken = (
                    await session.execute(
                        update(ResultMemoryLog)
                        .where(self._pk(source_id))
                        .where(ResultMemoryLog.decision == "write")
                        .where(ResultMemoryLog.updated_at < stale)
                        .where(
                            or_(
                                and_(
                                    ResultMemoryLog.state == "failed",
                                    ResultMemoryLog.attempts < self._settings.result_memory_max_attempts,
                                ),
                                ResultMemoryLog.state == "pending",
                            )
                        )
                        .values(state="pending", updated_at=now)
                    )
                ).rowcount
                if not taken:
                    await session.commit()
                    return None
            await session.commit()
            return (await session.execute(select(ResultMemoryLog).where(self._pk(source_id)))).scalar_one()

    async def _write(self, subtask: Any, row: ResultMemoryLog) -> None:
        settings = self._settings
        text = _cap(result_text(subtask), settings.result_memory_max_chars)
        episode_id = row.episode_id
        if episode_id is None:
            episode_id = await self._write_episode(subtask, text, row.reason)

        chunks, chunk_reason = 0, None
        if len(text) <= settings.result_memory_summary_chars:
            chunk_reason = "short"  # the summary already holds the whole text
        else:
            res = await _ingest_chunks(
                self._heart, settings, content=text, source_ref=chunk_source_ref(subtask.id), episode_id=episode_id
            )
            code = res.get("code")
            if code == "disabled":
                chunk_reason = "ingest_disabled"
            elif code == "too_short":
                chunk_reason = "too_short"
            elif "error" in res:
                raise RuntimeError(f"chunk ingest failed: {res.get('code')}: {res['error']}")
            else:
                chunks = int(res.get("inserted") or 0) or int(res.get("existing") or 0)

        async with self._db.session() as session:
            await session.execute(
                update(ResultMemoryLog)
                .where(self._pk(subtask.id))
                .values(
                    state="written",
                    chunks=chunks,
                    chunk_reason=chunk_reason,
                    last_error=None,
                    updated_at=datetime.now(UTC),
                )
            )
            await session.commit()

    async def _write_episode(self, subtask: Any, text: str, tier: str) -> UUID:
        """Create and close the episode and record its id, in one transaction.

        Either both land or neither does, so a retry never writes a second
        episode for the same source.
        """
        heart = self._heart
        ep_input = build_episode_input(subtask, text, tier, self._settings)
        async with self._db.session() as session:
            # dedup=False: a short conversation seed whose words all appear in
            # the task would otherwise be reused as this result's episode.
            episode = await heart.start_episode(ep_input, session=session, dedup=False)
            if episode.session_id != ep_input.session_id:
                # Never close someone else's episode.
                raise RuntimeError(f"episode start returned unrelated episode {episode.id}")
            await heart.end_episode(
                episode.id, "success" if subtask.status == "completed" else "failure", session=session
            )
            await session.execute(
                update(ResultMemoryLog)
                .where(self._pk(subtask.id))
                .values(episode_id=episode.id, updated_at=datetime.now(UTC))
            )
            await session.commit()
        return episode.id

    async def _mark_failed(self, source_id: UUID, exc: BaseException) -> None:
        try:
            async with self._db.session() as session:
                await session.execute(
                    update(ResultMemoryLog)
                    .where(self._pk(source_id))
                    .values(
                        state="failed",
                        attempts=ResultMemoryLog.attempts + 1,
                        last_error=f"{type(exc).__name__}: {exc}"[:_ERROR_MAX],
                        updated_at=datetime.now(UTC),
                    )
                )
                await session.commit()
        except Exception:
            logger.warning("F098: could not mark result memory row %s failed", source_id.hex[:8], exc_info=True)

    async def metrics(self, days: int) -> dict[str, Any]:
        """Write/skip counts and write latency over the last ``days`` (§3.8)."""
        since = datetime.now(UTC) - timedelta(days=days)
        async with self._db.session() as session:
            rows = (
                await session.execute(
                    select(
                        ResultMemoryLog.state, ResultMemoryLog.reason, Subtask.completed_at, ResultMemoryLog.updated_at
                    )
                    .join(Subtask, Subtask.id == ResultMemoryLog.source_id, isouter=True)
                    .where(ResultMemoryLog.agent_id == self._agent_id)
                    .where(ResultMemoryLog.created_at > since)
                )
            ).all()
        skipped: dict[str, int] = {}
        for state, reason, _, _ in rows:
            if state == "skipped":
                skipped[reason] = skipped.get(reason, 0) + 1
        latencies = sorted(
            max(0.0, (_aware(r[3]) - _aware(r[2])).total_seconds())
            for r in rows
            if r[0] == "written" and r[2] is not None
        )
        return {
            "written": sum(1 for r in rows if r[0] == "written"),
            "skipped": skipped,
            "failed": sum(1 for r in rows if r[0] == "failed"),
            "pending": sum(1 for r in rows if r[0] == "pending"),
            "p50_write_latency_s": _percentile(latencies, 0.50),
        }


def schedule_subtask_memory(writer: ResultMemoryWriter | None, subtask_id: UUID) -> asyncio.Task[Any] | None:
    """Record a subtask's result memory in the background (the inline spawn path).

    The calling turn is not slowed; the reconciler pass is the backstop if
    the task dies with the process.
    """
    if not isinstance(writer, ResultMemoryWriter) or not writer.enabled:
        return None
    try:
        task = asyncio.get_running_loop().create_task(
            writer.record_id(subtask_id), name=f"result-memory-{subtask_id.hex[:8]}"
        )
    except Exception:
        logger.warning("F098: could not schedule the result memory write of %s", subtask_id.hex[:8], exc_info=True)
        return None
    _pending_tasks.add(task)
    task.add_done_callback(_pending_tasks.discard)
    return task


# ---------------------------------------------------------------------------
# Reconciler pass
# ---------------------------------------------------------------------------


class ResultMemoryPass:
    """Second pass on the TerminalSubtaskReconciler (§3.4).

    Finds terminal subtasks inside the lookback with no log row (a hook that
    never ran or lost its write) and write rows that are retryable (failed
    under the cap, or abandoned while pending), and records each.
    """

    name = "memory"

    def __init__(self, writer: ResultMemoryWriter, settings: Settings) -> None:
        self._writer = writer
        self._settings = settings

    async def run(self, *, limit: int) -> int:
        settings = self._settings
        agent_id = settings.agent_id
        now = datetime.now(UTC)
        since = now - timedelta(hours=settings.result_memory_sweep_lookback_hours)
        has_row = exists().where(
            ResultMemoryLog.agent_id == agent_id,
            ResultMemoryLog.source_kind == SOURCE_SUBTASK,
            ResultMemoryLog.source_id == Subtask.id,
        )
        async with self._writer._db.session() as session:
            fresh = (
                (
                    await session.execute(
                        select(Subtask)
                        .where(Subtask.agent_id == agent_id)
                        .where(Subtask.status.in_(("completed", "failed", "cancelled")))
                        .where(Subtask.completed_at.is_not(None), Subtask.completed_at > since)
                        .where(~has_row)
                        .order_by(Subtask.completed_at)
                        .limit(limit)
                    )
                )
                .scalars()
                .all()
            )
            retry_ids = (
                (
                    await session.execute(
                        select(ResultMemoryLog.source_id)
                        .where(ResultMemoryLog.agent_id == agent_id)
                        .where(ResultMemoryLog.source_kind == SOURCE_SUBTASK)
                        .where(ResultMemoryLog.decision == "write")
                        .where(ResultMemoryLog.created_at > since)
                        .where(ResultMemoryLog.updated_at < now - RETRY_AFTER)
                        .where(
                            or_(
                                and_(
                                    ResultMemoryLog.state == "failed",
                                    ResultMemoryLog.attempts < settings.result_memory_max_attempts,
                                ),
                                ResultMemoryLog.state == "pending",
                            )
                        )
                        .order_by(ResultMemoryLog.updated_at)
                        .limit(max(0, limit - len(fresh)))
                    )
                )
                .scalars()
                .all()
            )

        started = time.monotonic()
        done = 0
        for item in [*fresh, *retry_ids]:
            if time.monotonic() - started > _PASS_BUDGET_SECONDS:
                break  # the rest waits for the next tick
            if isinstance(item, UUID):
                state = await self._writer.record_id(item)
            else:
                state = await self._writer.record(item)
            if state in ("written", "skipped"):
                done += 1
        return done
