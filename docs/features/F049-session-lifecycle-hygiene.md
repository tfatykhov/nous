# F049 — Session & Memory Lifecycle Hygiene

**Status:** ✅ Shipped (2026-04-20)
**Owner:** nous core
**Scope:** Close two session/memory lifecycle leaks: subtask session teardown (#187, **primary fix**) and `heart.working_memory` TTL safety net (#166, secondary).

**Issues addressed:** #187 (primary), #166 (scoped, safety net)
**Not in scope:** #184 reactive Telegram cleanup (architecturally requires out-of-process notification — see _Deferred_ below), #334, #179.

---

## Problem

Two independent gaps let session state accumulate. The reviews showed they share a single primary cause: **subtasks create sessions that never call `end_conversation`.**

### Evidence (measured 2026-04-20 against 192.168.1.141)

```
 total_wm_rows | stale_wm_over_24h
---------------+-------------------
            87 |                86
```

**98.8% of `heart.working_memory` rows are older than 24h.** These come from subtask sessions (`subtask-XXXXXXXX`) because `_execute_subtask` has no `finally: end_conversation(...)`. The session monitor's idle-timeout path already calls `cognitive.end_session()`, which calls `heart.clear_working_memory(session_id)` — so foreground sessions are NOT the leak source. Subtask sessions are.

### Root-cause model

| Leak | Primary? | Fix |
|---|---|---|
| Subtask `_execute_subtask` has no `finally` block — runner's `_conversations` and the heart WM row both survive completion | **Yes** | Mechanism B: wrap in `try/finally`, call `end_conversation` with timeout + shield |
| Any path that ever skips `end_conversation` (historical bugs, future regressions, abnormal terminations) leaves WM rows behind | Safety net | Mechanism A: periodic TTL sweep with LIMIT + advisory lock |

---

## Goals

1. **Subtask sessions tear down on every exit path** (success, failure, cancel, timeout) — primary fix for the 86 stale rows.
2. **A periodic safety-net sweep** removes `heart.working_memory` rows older than a configurable TTL, regardless of what path leaked them, **without** starving live writes or racing across replicas.
3. Zero foreground regression: `/chat`, `/chat/stream`, and already-working session monitor timeout behaviour stay identical.
4. Env-var kill switches for Mechanism A.
5. Every cleanup path has explicit observability so silent failures cannot hide.

## Non-goals

- Any in-process subscription from the Telegram bot (#184 reactive cleanup). `NousTelegramBot` runs as a separate OS process (`python -m nous.telegram_bot`) and communicates with Nous via REST — there is no shared `EventBus`. Addressing #184 would need a cross-process notification channel, which is a separate feature.
- Cross-restart persistent chat→session mapping (#184 Option C).
- Rewriting session monitor's idle/sleep logic; this feature adds a sweep pass, not a redesign.
- Changing `WorkingMemoryItem` eviction (#322 OpenThread pinning is its own issue).

## Deferred (with rationale)

- **#184 reactive Telegram cleanup** — existing lazy TTL (`SESSION_TTL_SECONDS=1800`, PR #185) already expires stale IDs within 30 min of idle. A cross-process notification would reduce the gap to ~0 but requires either an SSE endpoint on Nous or a Telegram API-level callback. Low ROI vs effort; deferred to a follow-up feature if the 30-min gap proves problematic in practice.

---

## Design

### Mechanism B — Subtask session teardown (primary)

**Where:** `nous/handlers/subtask_worker.py::_execute_subtask`
**Fix:** Wrap the body with `try/finally`. Inside the `finally`, call `end_conversation` with **a hard timeout** and **shielded from secondary cancellation**, catching only the narrow set of exceptions that indicate genuine cleanup failure.

```python
async def _execute_subtask(self, subtask: Subtask) -> None:
    session_id = f"subtask-{subtask.id.hex[:8]}"
    logger.info("Executing subtask %s: %s", subtask.id.hex[:8], subtask.task[:80])
    from nous.api.tools import build_subtask_prefix
    system_prefix = build_subtask_prefix(subtask.task, subtask.frame_type)

    try:
        try:
            response_text, _turn_ctx, _usage = await self._runner.run_turn(...)
            await self._heart.subtasks.complete(subtask.id, response_text)
            await self._emit_event("subtask_completed", subtask, result=response_text)
            await self._notify_telegram(subtask, result=response_text)
            logger.info("Subtask %s completed", subtask.id.hex[:8])
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error_msg = f"{type(exc).__name__}: {exc}"
            logger.exception("Subtask %s failed", subtask.id.hex[:8])
            await self._heart.subtasks.fail(subtask.id, error_msg)
            await self._emit_event("subtask_failed", subtask, error=error_msg)
            await self._notify_telegram(subtask, error=error_msg)
    finally:
        cleanup_timeout = self._settings.subtask_cleanup_timeout_seconds  # default 30
        try:
            await asyncio.shield(
                asyncio.wait_for(
                    self._runner.end_conversation(
                        session_id, agent_id=self._settings.agent_id
                    ),
                    timeout=cleanup_timeout,
                )
            )
            logger.debug("Ended subtask session %s", session_id)
        except asyncio.TimeoutError:
            logger.error(
                "Subtask cleanup timed out after %ds for session %s — possible runner/brain outage",
                cleanup_timeout, session_id,
            )
        except asyncio.CancelledError:
            # Allow cancellation to propagate; shield caught one, but a second may fire
            logger.warning("Subtask cleanup cancelled for %s", session_id)
            raise
        except Exception:
            logger.exception(
                "Subtask cleanup failed for session %s — end_conversation raised",
                session_id,
            )
```

Key properties (all responses to silent-failure review):

- **Bounded cleanup.** `asyncio.wait_for(..., timeout=30)` prevents a hung `_call_api` inside `end_conversation` from blocking a worker forever. Default 30s; env override.
- **Shielded.** `asyncio.shield` makes the cleanup survive a second `CancelledError` from the outer task or worker pool; the TimeoutError path still fires on real hangs.
- **ERROR severity + structured logging.** A cleanup failure surfaces at ERROR (not WARNING) and uses `logger.exception` so tracebacks and context reach the logging pipeline.
- **Narrow, intentional catches.** `TimeoutError`, `CancelledError`, and a final `Exception` bucket — each with its own distinct log statement so operators can tell the three modes apart.

Cancellation semantics (prose corrected from v1 of the spec):
- When `asyncio.wait_for(_execute_subtask, timeout=...)` in `_process_subtask` times out, Python cancels the inner coroutine with `CancelledError`. The inner `except CancelledError: raise` re-raises it, **and Python guarantees the outer `finally` block runs before the cancellation unwinds**. That is why `end_conversation` is reached on the timeout path even though `_process_subtask` catches the resulting `TimeoutError` itself.

### Mechanism A — `heart.working_memory` TTL safety net

**Where:** `nous/heart/working_memory.py` — new method `cleanup_stale(...)`; invoked from `nous/handlers/session_monitor.py::_check_timeouts` at most once per sweep interval.

```python
async def cleanup_stale(
    self,
    max_age_hours: int = 24,
    batch_size: int = 5000,
) -> int:
    """Delete stale working_memory rows for this agent.

    Uses a PostgreSQL transaction-scoped advisory lock keyed on agent_id so
    that two replicas cannot race on the same DELETE. Returns total rows
    deleted across any batches that ran; 0 when the lock is already held or
    TTL is disabled.
    """
    if max_age_hours <= 0:
        return 0
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
    total_deleted = 0
    # Lock key: stable hash of agent_id string
    lock_key = abs(hash(self.agent_id)) % (2**31)

    async with self.db.session() as session:
        acquired = (await session.execute(
            text("SELECT pg_try_advisory_xact_lock(:k)").bindparams(k=lock_key)
        )).scalar()
        if not acquired:
            logger.debug("WM sweep skipped — another replica holds the advisory lock")
            return 0

        # Batch delete. LIMIT via a subquery on ctid — pg-native pattern.
        while True:
            result = await session.execute(text("""
                DELETE FROM heart.working_memory
                WHERE ctid IN (
                    SELECT ctid FROM heart.working_memory
                    WHERE agent_id = :agent_id AND updated_at < :cutoff
                    LIMIT :batch_size
                )
                RETURNING session_id
            """).bindparams(agent_id=self.agent_id, cutoff=cutoff, batch_size=batch_size))
            deleted = result.scalars().all()
            total_deleted += len(deleted)
            if len(deleted) < batch_size:
                break  # no more matches
        await session.commit()

    if total_deleted:
        logger.info(
            "WM sweep deleted %d rows for agent %s (threshold=%dh)",
            total_deleted, self.agent_id, max_age_hours,
        )
    else:
        logger.debug("WM sweep: no stale rows for agent %s", self.agent_id)
    return total_deleted
```

Key properties (responses to silent-failure + architecture reviews):

- **LIMIT-batched.** Deletes in batches of 5 000 rows (configurable) via `ctid IN (SELECT ... LIMIT N)`. The whole loop runs in one transaction (required because the advisory lock is transaction-scoped — see below), but each batch takes a short exclusive lock on just its slice of rows instead of one multi-minute table-wide lock at scale.
- **Advisory lock.** `pg_try_advisory_xact_lock` keyed on a SHA-256 hash of `agent_id` (first 4 bytes → 31-bit int) prevents two replicas from issuing concurrent DELETEs against the same rows. The key is cross-process-stable — `hash()` would be randomized per-process and leak the serialization guarantee. When the lock is held, the sweep returns 0 with a DEBUG log, no blocking wait.
- **Activity-aware.** The `heart.working_memory.set_updated_at` trigger (sql/init.sql:630) keeps `updated_at` current on every UPDATE, so active sessions are never swept.
- **Distinct observability states.**
  - `total_deleted > 0` → INFO with row count.
  - `total_deleted == 0 AND acquired AND rows existed` → DEBUG "no stale rows".
  - `not acquired` → DEBUG "another replica holds lock".
  - `max_age_hours <= 0` → no code path runs; operator knows the sweep is disabled because env var is explicit.

**Session monitor wiring:**

```python
# nous/handlers/session_monitor.py
class SessionTimeoutMonitor:
    def __init__(
        self,
        bus: EventBus,
        settings: Settings,
        *,
        cognitive: object | None = None,
        heart: "Heart | None" = None,  # F049: required for WM sweep; None disables.
    ):
        ...
        self._heart = heart
        self._last_wm_sweep: float = 0.0
```

Invoked inside `_check_timeouts`:

```python
if (
    self._heart is not None
    and self._settings.working_memory_ttl_hours > 0
):
    sweep_interval = self._settings.working_memory_sweep_interval_seconds
    if time.monotonic() - self._last_wm_sweep >= sweep_interval:
        try:
            await self._heart.working_memory.cleanup_stale(
                max_age_hours=self._settings.working_memory_ttl_hours,
            )
        except Exception:
            logger.exception("WM TTL sweep raised")
        finally:
            self._last_wm_sweep = time.monotonic()
```

Main-wiring change: the construction site in `nous/main.py` passes `heart=heart` into `SessionTimeoutMonitor(...)`.

**Config:**
| Env var | Default | Description |
|---|---|---|
| `NOUS_WORKING_MEMORY_TTL_HOURS` | `24` | Age threshold for WM row deletion. `0` disables the sweep entirely. |
| `NOUS_WORKING_MEMORY_SWEEP_INTERVAL_SECONDS` | `3600` | Minimum seconds between WM TTL sweeps. |
| `NOUS_WORKING_MEMORY_SWEEP_BATCH_SIZE` | `5000` | Rows per DELETE batch. |
| `NOUS_SUBTASK_CLEANUP_TIMEOUT_SECONDS` | `30` | Hard timeout for `end_conversation` in subtask finally. |

All follow `validation_alias="NOUS_..."` plain-string convention (matches `config.py:117+`).

---

## Rollout

Single PR, two commits (one per mechanism + one for tests/docs). Both mechanisms default to **enabled**:

- Mechanism B: pure bugfix; no behavior change for callers.
- Mechanism A: gated by `NOUS_WORKING_MEMORY_TTL_HOURS`; set to `0` to disable.

No shadow mode needed — both are cleanup-only changes with no output-surface differences.

---

## Acceptance criteria

### Mechanism B (Subtask cleanup)
- [ ] Unit: Successful subtask → `end_conversation(session_id)` called exactly once.
- [ ] Unit: Failed subtask → `end_conversation(session_id)` called exactly once.
- [ ] Unit: Cancelled subtask → `end_conversation` called, CancelledError propagates.
- [ ] Unit: `_process_subtask` timeout path → `end_conversation` called via inner `finally` before outer `asyncio.TimeoutError` fires.
- [ ] Unit: `end_conversation` raising `TimeoutError` → logged at ERROR, original exception (if any) still propagates.
- [ ] Unit: `end_conversation` raising generic `Exception` → logged via `logger.exception`, does not mask original exception.
- [ ] Unit: Mocked runner hang > 30s → `asyncio.TimeoutError` branch logged at ERROR.
- [ ] Integration: After 100 subtasks complete, `runner._conversations` size returns to pre-subtask baseline; `heart.working_memory` row count for `session_id LIKE 'subtask-%'` is 0.

### Mechanism A (WM TTL safety net)
- [ ] Unit: Stale row (updated_at = now() − 25h) → deleted.
- [ ] Unit: Fresh row (updated_at = now() − 12h) → preserved.
- [ ] Unit: Agent-id isolation — rows for `agent_id='other'` are not touched.
- [ ] Unit: `max_age_hours=0` returns 0 without issuing DELETE.
- [ ] Unit: Two concurrent `cleanup_stale` calls — only one runs the DELETE; the other returns 0 due to advisory lock.
- [ ] Unit: 12 000 stale rows → 3 batches run; total_deleted == 12 000.
- [ ] Integration: `NOUS_WORKING_MEMORY_TTL_HOURS=0` in environment → sweep skipped; DB row count unchanged.
- [ ] Integration: After session_monitor run with stale rows present, count drops to 0.

---

## Observability

| Condition | Level | Message |
|---|---|---|
| Sweep deleted ≥1 row | INFO | `WM sweep deleted N rows for agent X (threshold=Hh)` |
| Sweep ran, 0 rows | DEBUG | `WM sweep: no stale rows for agent X` |
| Advisory lock held by peer | DEBUG | `WM sweep skipped — another replica holds the advisory lock` |
| Cleanup timeout in subtask finally | ERROR | `Subtask cleanup timed out after 30s for session S — possible runner/brain outage` |
| Cleanup `Exception` in subtask finally | ERROR (`.exception`) | `Subtask cleanup failed for session S — end_conversation raised` |
| Cleanup cancelled in subtask finally | WARNING | `Subtask cleanup cancelled for S` (then re-raised) |

Operators can also see ebbing WM row count on `/status` (already exposed).

---

## References

- Issue #187 — Subtask session cleanup (primary leak source)
- Issue #166 (scoped) — WM TTL safety net
- Issue #184 — Deferred; existing PR #185 lazy TTL is the de-facto fix
- `ISSUES_AUDIT_2026-04-20.md`
- Forge decisions: `685c84c3` (audit), `c1291760` (F049 scope), `b1961123` (silent-failure review), `f0136ebe` (architecture review), `8d6dd68b` (correctness review)
