# F049 Implementation Plan — Session & Memory Lifecycle Hygiene (v2, post-review)

**Spec:** `docs/features/F049-session-lifecycle-hygiene.md`
**Issues:** #187 (primary), #166 (scoped safety net). #184 deferred.
**Branch:** `feat/f049-session-lifecycle-hygiene`
**Date:** 2026-04-20 (v2 after 3-agent review)

Revisions in v2 (vs v1):
- Dropped Mechanism C (Telegram reactive cleanup) — `NousTelegramBot` is out-of-process, cannot subscribe to in-process bus.
- Mechanism B cleanup wrapped in `asyncio.shield(asyncio.wait_for(..., 30))`; three distinct except branches (TimeoutError / CancelledError / Exception) with ERROR-severity `logger.exception`.
- Mechanism A: LIMIT-batched DELETE (ctid subquery), `pg_try_advisory_xact_lock` on agent_id hash, per-batch commit.
- Config uses plain `validation_alias="NOUS_..."` strings.
- `SessionTimeoutMonitor.__init__` grows a `heart: Heart | None = None` kwarg.
- Corrected prose about `asyncio.wait_for` and `TimeoutError` caught by `_process_subtask`, not `_execute_subtask`.

---

## Pre-flight verification (all verified 2026-04-20)

- `nous/api/runner.py:380` — `end_conversation(session_id, agent_id=None)` idempotent.
- `nous/storage/models.py:603-619` — `WorkingMemory` has `agent_id`, `session_id`, `updated_at (DateTime(timezone=True))`.
- `sql/init.sql:630` — `CREATE TRIGGER set_updated_at BEFORE UPDATE ON heart.working_memory` (DB-level onupdate).
- `nous/cognitive/layer.py:1425-1429` — `end_session` calls `heart.clear_working_memory(session_id)` (WM is cleaned on happy-path end).
- `nous/handlers/subtask_worker.py:110-169` — `_process_subtask` wraps `_execute_subtask` in `asyncio.wait_for(...)`; `_execute_subtask` has no `finally`.
- `nous/handlers/session_monitor.py:39-57` — `SessionTimeoutMonitor.__init__` takes `(bus, settings, *, cognitive=None)` — **no `heart` param**.
- `nous/config.py` — convention is `Field(default=..., validation_alias="NOUS_...")` (plain string).

Note: `NousTelegramBot.__init__` takes `(bot_token, nous_url, allowed_users)` and is launched as a separate OS process via `python -m nous.telegram_bot`. Cannot share the in-process bus. Mechanism C is therefore out-of-scope for F049.

---

## Task order (TDD, 2 tasks)

### Task 1 — Mechanism B: Subtask session teardown

**Why first:** B is the primary fix (addresses the 86 stale rows directly). Landing B first lets A's test data stay stable.

#### 1.1 Tests first

**New file:** `tests/handlers/test_subtask_worker_cleanup.py`

Tests required:
1. `test_execute_subtask_success_ends_conversation` — mocked runner, assert `end_conversation` called once with the right session_id.
2. `test_execute_subtask_failure_ends_conversation` — mocked runner raises `ValueError`; assert `end_conversation` still called once; the warning log for "subtask failed" is produced.
3. `test_execute_subtask_cancellation_ends_conversation` — outer task cancelled mid-`run_turn`; assert `end_conversation` is awaited before `CancelledError` propagates. Cleanup is `asyncio.shield`-wrapped so it completes even if a second cancel arrives.
4. `test_execute_subtask_outer_timeout_ends_conversation` — wrap `_execute_subtask` in `asyncio.wait_for(..., 0.05)` with a mocked `run_turn` that sleeps 1s. Assert `end_conversation` still fires via the `finally`.
5. `test_cleanup_timeout_logs_error` — mocked `end_conversation` sleeps > `subtask_cleanup_timeout_seconds`; assert `asyncio.TimeoutError` branch hit, ERROR log emitted.
6. `test_cleanup_exception_logs_exception_and_preserves_original` — `run_turn` raises `RuntimeError`, `end_conversation` raises `ConnectionError` inside the finally. Assert the subtask's `heart.subtasks.fail` was called for the original RuntimeError (inner except fired) and that `logger.exception` ran for the cleanup failure.
7. `test_cleanup_cancelled_reraises` — inside the `finally`, the `asyncio.shield` is interrupted and a second `CancelledError` comes through. Assert WARNING log + re-raise (so caller sees the cancel).

Run: `uv run pytest tests/handlers/test_subtask_worker_cleanup.py -v` → all fail.

#### 1.2 Implementation

**File:** `nous/handlers/subtask_worker.py`

Add import at top:
```python
import asyncio  # already there; confirm
```

Modify `_execute_subtask` (replace the body):
```python
async def _execute_subtask(self, subtask: Subtask) -> None:
    session_id = f"subtask-{subtask.id.hex[:8]}"
    logger.info("Executing subtask %s: %s", subtask.id.hex[:8], subtask.task[:80])
    from nous.api.tools import build_subtask_prefix
    system_prefix = build_subtask_prefix(subtask.task, subtask.frame_type)

    try:
        try:
            response_text, _turn_ctx, _usage = await self._runner.run_turn(
                session_id=session_id,
                user_message=subtask.task,
                agent_id=self._settings.agent_id,
                system_prompt_prefix=system_prefix,
                skip_episode=True,
                is_subtask=True,
                max_tool_calls=self._settings.subtask_tool_call_limit,
                model_override=subtask.model or self._settings.background_model,
                is_background=True,
            )
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
        cleanup_timeout = self._settings.subtask_cleanup_timeout_seconds
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
            logger.warning("Subtask cleanup cancelled for %s", session_id)
            raise
        except Exception:
            logger.exception(
                "Subtask cleanup failed for session %s — end_conversation raised",
                session_id,
            )
```

#### 1.3 Config

**File:** `nous/config.py` — add one new setting (near other subtask settings):
```python
subtask_cleanup_timeout_seconds: int = Field(
    default=30,
    validation_alias="NOUS_SUBTASK_CLEANUP_TIMEOUT_SECONDS",
    description="Max seconds to wait for end_conversation inside subtask finally before logging ERROR.",
)
```

Run tests: green.

---

### Task 2 — Mechanism A: WM TTL safety-net sweep

#### 2.1 Tests first

**New file:** `tests/heart/test_working_memory_cleanup.py`

Tests required:
1. `test_cleanup_stale_deletes_old_rows` — insert WM with `updated_at = now() - 25h`, call `cleanup_stale(24)`, assert row deleted, return == 1.
2. `test_cleanup_stale_preserves_fresh_rows` — `updated_at = now() - 12h`, call `cleanup_stale(24)`, assert row stays, return == 0.
3. `test_cleanup_stale_agent_id_isolation` — insert stale rows for agent-a and agent-b; heart bound to agent-a; assert only agent-a row deleted.
4. `test_cleanup_stale_zero_hours_is_noop` — `cleanup_stale(0)` returns 0 without issuing DELETE.
5. `test_cleanup_stale_batch_limit` — insert 12 000 stale rows; call `cleanup_stale(24, batch_size=5000)`; assert return == 12 000 and 3 batches ran (can assert via capturing SQL or counting `logger.debug` "batch complete" lines if needed).
6. `test_cleanup_stale_advisory_lock_prevents_concurrent` — open a second DB session, acquire the lock on the same agent_id, then call `cleanup_stale` in the first session. Assert returns 0 (lock not acquired) and logs DEBUG "another replica holds".

#### 2.2 Implementation

**File:** `nous/heart/working_memory.py`

Add imports:
```python
from datetime import datetime, timedelta, timezone
from sqlalchemy import text
```

Add method to `WorkingMemoryManager`:
```python
async def cleanup_stale(
    self,
    max_age_hours: int = 24,
    batch_size: int = 5000,
) -> int:
    """Delete stale working_memory rows for this agent. Safety net for
    session paths that didn't call end_conversation.

    Uses a PostgreSQL transaction-scoped advisory lock keyed on agent_id
    to avoid multi-replica races. Returns total rows deleted; 0 if TTL
    disabled or another replica holds the lock.
    """
    if max_age_hours <= 0:
        return 0

    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
    lock_key = abs(hash(self.agent_id)) % (2**31)  # bigint-safe
    total_deleted = 0

    async with self.db.session() as session:
        acquired = (
            await session.execute(
                text("SELECT pg_try_advisory_xact_lock(:k)").bindparams(k=lock_key)
            )
        ).scalar()
        if not acquired:
            logger.debug(
                "WM sweep skipped — another replica holds the advisory lock for agent %s",
                self.agent_id,
            )
            return 0

        while True:
            result = await session.execute(
                text("""
                    DELETE FROM heart.working_memory
                    WHERE ctid IN (
                        SELECT ctid FROM heart.working_memory
                        WHERE agent_id = :agent_id AND updated_at < :cutoff
                        LIMIT :batch_size
                    )
                    RETURNING session_id
                """).bindparams(
                    agent_id=self.agent_id,
                    cutoff=cutoff,
                    batch_size=batch_size,
                )
            )
            deleted = result.scalars().all()
            total_deleted += len(deleted)
            if len(deleted) < batch_size:
                break
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

**Note on SQL:** using `text()` with bound params keeps PG advisory lock and CTID delete correct. `int(batch_size)` cast is defensive — SQLAlchemy will bind it as an int, but if batch_size is operator-supplied from an env var we should double-check config coerces to int.

**Note on lock key stability:** use `hashlib.sha256(agent_id).digest()[:4]` (big-endian int, mod 2**31) — NOT builtin `hash()`. Python randomizes its hash seed per process, so two replicas would compute different keys and the lock would not serialize them. The test in `test_cleanup_stale_advisory_lock_prevents_concurrent` must use the same SHA-256 formula.

**Note on commit scope:** `pg_try_advisory_xact_lock` is transaction-scoped — committing between batches would release the lock and defeat multi-replica protection. The implementation therefore issues one `session.commit()` after the whole batched DELETE loop. At scale this is acceptable because (a) each batch takes a lock only on its LIMIT-sized ctid slice, (b) advisory-lock collisions drop the total per-agent sweep to at most one active replica anyway.

#### 2.3 Session monitor wiring

**File:** `nous/handlers/session_monitor.py`

1. Add `heart: "Heart | None" = None` to `__init__`:
```python
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from nous.heart.heart import Heart

class SessionTimeoutMonitor:
    def __init__(
        self,
        bus: EventBus,
        settings: Settings,
        *,
        cognitive: object | None = None,
        heart: "Heart | None" = None,
    ):
        ...
        self._heart = heart
        self._last_wm_sweep: float = 0.0
```

2. Inside `_check_timeouts` (or the main check method — confirm name by reading lines 100–150):
```python
# F049: periodic WM TTL safety-net sweep
if (
    self._heart is not None
    and self._settings.working_memory_ttl_hours > 0
):
    sweep_interval = self._settings.working_memory_sweep_interval_seconds
    if time.monotonic() - self._last_wm_sweep >= sweep_interval:
        try:
            await self._heart.working_memory.cleanup_stale(
                max_age_hours=self._settings.working_memory_ttl_hours,
                batch_size=self._settings.working_memory_sweep_batch_size,
            )
        except Exception:
            logger.exception("WM TTL sweep raised")
        finally:
            self._last_wm_sweep = time.monotonic()
```

#### 2.4 Config

**File:** `nous/config.py` — add three settings near other heart-related settings:
```python
working_memory_ttl_hours: int = Field(
    default=24,
    validation_alias="NOUS_WORKING_MEMORY_TTL_HOURS",
    description="F049 — delete heart.working_memory rows older than this. 0 disables.",
)
working_memory_sweep_interval_seconds: int = Field(
    default=3600,
    validation_alias="NOUS_WORKING_MEMORY_SWEEP_INTERVAL_SECONDS",
    description="F049 — minimum seconds between WM TTL safety-net sweeps.",
)
working_memory_sweep_batch_size: int = Field(
    default=5000,
    validation_alias="NOUS_WORKING_MEMORY_SWEEP_BATCH_SIZE",
    description="F049 — rows per DELETE batch during WM sweep.",
)
```

#### 2.5 Main wiring

**File:** `nous/main.py`

Find the `SessionTimeoutMonitor(...)` construction and add `heart=heart`:
```python
session_monitor = SessionTimeoutMonitor(
    bus=bus,
    settings=settings,
    cognitive=cognitive,
    heart=heart,  # F049: WM TTL safety-net sweep
)
```

Run all WM cleanup tests: green.

---

## CLAUDE.md + INDEX updates

After both mechanisms land and tests are green:

1. **`CLAUDE.md` env vars table** — add:
```
| NOUS_WORKING_MEMORY_TTL_HOURS | 24 | F049 — delete heart.working_memory rows older than this (0 disables) |
| NOUS_WORKING_MEMORY_SWEEP_INTERVAL_SECONDS | 3600 | F049 — minimum seconds between WM TTL sweeps |
| NOUS_WORKING_MEMORY_SWEEP_BATCH_SIZE | 5000 | F049 — rows per DELETE batch during WM sweep |
| NOUS_SUBTASK_CLEANUP_TIMEOUT_SECONDS | 30 | F049 — max seconds for end_conversation in subtask finally |
```

2. **`CLAUDE.md` shipped table** — add a row for F049.

3. **`docs/features/F049-session-lifecycle-hygiene.md`** — flip status `📐 Spec` → `✅ Shipped`.

4. **`docs/features/INDEX.md`** — append F049 entry (check current format first).

---

## Verification checklist before PR

- [ ] `uv run pytest tests/handlers/test_subtask_worker_cleanup.py -v` — green (7 tests)
- [ ] `uv run pytest tests/heart/test_working_memory_cleanup.py -v` — green (6 tests)
- [ ] `uv run pytest -x` — full suite green
- [ ] `uv run ruff check nous/ tests/` — clean
- [ ] Docker smoke test: bring up Nous, run one subtask, assert `heart.working_memory` row count for `session_id LIKE 'subtask-%'` is 0 afterward
- [ ] Trigger WM sweep manually (shrink TTL env to 0.001h, wait, restore); verify INFO log with row count

## Rollback

- Mechanism B: revert `subtask_worker.py` changes; no config needed.
- Mechanism A: `NOUS_WORKING_MEMORY_TTL_HOURS=0` disables at runtime without a redeploy.

---

## Build sequence

1. Pre-flight (skim `main.py`, `config.py`, `working_memory.py`, `subtask_worker.py`, `session_monitor.py` — 5 min).
2. Task 1 (Mechanism B): tests → impl → config → run tests (~45 min).
3. Task 2 (Mechanism A): tests → impl → monitor wiring → config → main wiring → run tests (~75 min).
4. Full suite + ruff (~10 min).
5. Docs updates (~5 min).
6. Commit → branch → push → PR (~5 min).

Total estimated wall-clock: **~2.5 hours**.
