"""F111 PR B1: stage 2 of the promise gate, a model call on each sentence stage 1 flagged.

Stage 1 (``PromiseDetector``) is a regex and over-flags: a standing policy ("I'll close each row when it is done"),
a description of the system and someone else's work look like promises to it. Stage 2 asks one question about one
sentence: does the speaker commit to specific work after this reply ends? The verdict is stored in the row's
``evidence.stage2`` (``promise``, ``not_promise`` or ``error``); the overdue watcher never pings a ``not_promise`` row.

What it sends: only the row's stored ``promise_text`` (the one flagged sentence, already cleaned and redacted, at most
500 characters). Never the reply, the conversation or a tool result. Anthropic only, through
``call_background_llm_structured`` (purpose ``promise_gate_classifier``), the helper the actionability and
contradiction calls use. Only gate rows are classified; a ``commit`` row is the agent's own explicit record.

When it runs: ``PromiseGate.run`` hands each row it wrote to ``ClassifierScheduler.submit`` and returns; the turn
never waits for the call. Fail-open: a timeout, an exception or a bad label stores ``error`` (best effort) with one
WARNING, and the watcher retries the row once before it pings it as "(not checked)".
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime
from typing import Any

from nous.brain.commitments import Commitment, clean_text
from nous.handlers import call_background_llm_structured

logger = logging.getLogger(__name__)

PROMPT_VERSION = "s2-v1"
PURPOSE = "promise_gate_classifier"
TOOL_NAME = "classify_promise"
MAX_TOKENS = 300
REASON_MAX = 200
VERDICTS: frozenset[str] = frozenset({"promise", "not_promise"})
CLOSE_TIMEOUT_SECONDS = 5.0

SYSTEM_PROMPT = """You classify ONE sentence an AI assistant wrote at the end of a reply to its owner.

Answer "promise" only when the speaker commits to do specific work AFTER this reply ends: one task it will carry
out later ("I'll deploy it when CI is green", "I'll send you the summary once the job finishes", "The PR will be
merged by then" said of its own PR).

Answer "not_promise" for:
- a standing policy or habit ("I close each row when the work is done", "I'll reply to every alert when it fires");
- a description of how the system behaves ("the heartbeat will retry the check later");
- work done in this turn ("I'll restart it now and report below");
- a conditional that describes behaviour rather than commits to a task ("if the build fails, I usually rerun it");
- something someone else will do, or a state of the world ("The market will be closed by then.", "Your order will be
  delivered by Friday.");
- a hypothetical ("if we had time, I'd rebuild the index");
- a question.

You see only the sentence. Give the verdict and a reason of at most one short sentence."""

OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["promise", "not_promise"]},
        "reason": {"type": "string", "maxLength": REASON_MAX},
    },
    "required": ["verdict", "reason"],
}


def _previous_attempts(row: Commitment) -> int:
    attempts = row.stage2.get("attempts")
    return attempts if isinstance(attempts, int) and attempts > 0 else 0


class ClassifierScheduler:
    """Runs stage-2 calls in the background: at most ``promise_gate_classifier_max_concurrency`` at once, one per
    row (a row in flight or with a verdict is skipped), each bounded by ``promise_gate_classifier_timeout_seconds``.
    ``close`` waits for the calls in flight, then cancels the rest; a cancelled call stores nothing."""

    def __init__(self, store: Any, settings: Any, client: Any) -> None:
        self._store = store
        self._settings = settings
        self._client = client
        self._slots = asyncio.Semaphore(settings.promise_gate_classifier_max_concurrency)
        self._in_flight: set[Any] = set()
        self._tasks: set[asyncio.Task] = set()
        self._closed = False

    @property
    def enabled(self) -> bool:
        return bool(self._settings.promise_gate_classifier_enabled)

    @property
    def pending(self) -> int:
        """Calls started by ``submit`` and not yet finished."""
        return len(self._tasks)

    def submit(self, row: Commitment) -> asyncio.Task | None:
        """Classify a gate row in the background; the task, or None when nothing was started (stage 2 off, closed,
        a ``commit`` row, a row already in flight, or one that already has a ``promise``/``not_promise`` verdict).
        Never raises and never waits."""
        if not self.enabled or self._closed or row.detected_by != "gate":
            return None
        if row.id in self._in_flight or row.stage2.get("verdict") in VERDICTS:
            return None
        self._in_flight.add(row.id)  # now, not when the task first runs: a second submit right after is skipped
        task = asyncio.ensure_future(self._classify_and_store(row))
        self._tasks.add(task)

        def _done(done: asyncio.Task) -> None:
            self._tasks.discard(done)
            self._in_flight.discard(row.id)

        task.add_done_callback(_done)
        return task

    async def classify_now(self, row: Commitment) -> dict[str, Any]:
        """Classify one row now (in a slot; the watcher's retry) and store the verdict; returns the stored
        document. Never raises, except a cancellation, which stores nothing."""
        self._in_flight.add(row.id)
        try:
            return await self._classify_and_store(row)
        finally:
            self._in_flight.discard(row.id)

    async def _classify_and_store(self, row: Commitment) -> dict[str, Any]:
        async with self._slots:
            doc = await self._classify(row)
        try:
            await self._store.set_stage2(row.id, doc)
        except Exception:
            logger.warning("F111 stage 2: the verdict for C-%04d was not stored", row.short_id, exc_info=True)
        return doc

    async def _classify(self, row: Commitment) -> dict[str, Any]:
        model = self._settings.promise_gate_classifier_model
        timeout = self._settings.promise_gate_classifier_timeout_seconds
        started = time.perf_counter()
        verdict, reason = "error", ""
        try:
            result = await asyncio.wait_for(
                call_background_llm_structured(
                    self._client,
                    model,
                    SYSTEM_PROMPT,
                    row.promise_text,
                    TOOL_NAME,
                    "Record whether the sentence is a promise of later work.",
                    OUTPUT_SCHEMA,
                    max_tokens=MAX_TOKENS,
                    purpose="promise_gate_classifier",  # PURPOSE, as a literal for the purpose-site check
                ),
                timeout=timeout,
            )
        except TimeoutError:
            logger.warning("F111 stage 2: no verdict for C-%04d within %s s", row.short_id, timeout)
        except Exception:
            logger.warning("F111 stage 2: the call for C-%04d failed", row.short_id, exc_info=True)
        else:
            # None: the helper failed and logged it once already.
            if result is not None:
                label = result.get("verdict")
                if label in VERDICTS:
                    verdict = label
                    reason = clean_text(str(result.get("reason") or ""))[:REASON_MAX]
                else:
                    logger.warning("F111 stage 2: C-%04d got no usable label (%r)", row.short_id, label)
        return {
            "verdict": verdict,
            "model": model,
            "prompt_version": PROMPT_VERSION,
            "latency_ms": round((time.perf_counter() - started) * 1000),
            "at": datetime.now(UTC).isoformat(),
            "attempts": _previous_attempts(row) + 1,
            "reason": reason,
        }

    async def close(self, timeout: float = CLOSE_TIMEOUT_SECONDS) -> None:
        """Stop taking work, wait up to ``timeout`` for the calls in flight, then cancel the rest."""
        self._closed = True
        pending = set(self._tasks)
        if not pending:
            return
        _, still = await asyncio.wait(pending, timeout=timeout)
        for task in still:
            task.cancel()
        await asyncio.gather(*still, return_exceptions=True)
