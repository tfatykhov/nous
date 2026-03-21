# Implementation Plan: Issues #173 + #174

**Issues:** #173 (Sleep cycle silent failures + manual trigger) | #174 (Dashboard activity view always empty)
**Date:** 2026-03-21
**Decision:** 91eea7a3
**Review:** ab90931c — 3-agent review (architect + backend + devil's advocate). All approved with conditions. All P1s addressed below.

## Overview

Two related issues: #173 fixes sleep handler bugs and adds a manual trigger endpoint; #174 fixes the dashboard activity view by rewriting the backend query to match the frontend contract.

## Review Fixes Applied

| # | Finding | Fix |
|---|---------|-----|
| P1-1 | SQL column is `data` not `event_data` | Fixed in Task C1 queries |
| P1-2 | Manual trigger race — no feedback if already sleeping | Added `is_sleeping` property + 409 check in Task B1 |
| P1-3 | Bool vs tuple return conflict in A2/A3 | Phases return `bool` only; shared `sleep_stats` dict passed as param |
| P1-4 | "Nothing to do" early returns should be `True` | Specified: `True` for no-op, `False` only for exceptions |
| P2-1 | 7d censor stats use cumulative counters (wrong) | Use `censor_activated` events from events table |
| P2-2 | `hours` param needs validation | Added ValueError catch + cap 1-720 + return 400 |
| P2-3 | `fires_7d` self-contradiction | Removed wrong approach, events-based only |
| P2-4 | `censors_retired` placeholder | Keep as 0 with comment; prune is a stub |

## Phase A: Sleep Handler Bug Fixes (#173)

### Task A1: Add `exc_info=True` to silent exception handlers

**File:** `nous/handlers/sleep_handler.py`

Fix all bare exception handlers that swallow tracebacks:

- **Line 202:** `logger.warning("Decision review phase failed")` → add `exc_info=True`
- **Line 210:** `logger.warning("Prune phase failed")` → add `exc_info=True`
- **Line 225:** `logger.warning("Compress phase failed")` → add `exc_info=True`
- **Line 315:** `except (json.JSONDecodeError, Exception):` → simplify to `except Exception:` (redundant catch), add `exc_info=True`
- **Line 330:** `logger.warning("Generalize phase...")` → add `exc_info=True`

### Task A2: Fix `phases_completed` accuracy

**File:** `nous/handlers/sleep_handler.py`

Each phase method returns `bool`:
- `True` = phase completed (including no-op "nothing to do" cases like no LLM client, not enough episodes)
- `False` = phase failed (exception caught)

Caller only appends to `phases_completed` if return is `True`:

```python
# In _run_sleep (line 161-163):
if not self._interrupted:
    success = await self._phase_reflect(sleep_stats)
    if success:
        phases_completed.append("reflect")
```

### Task A3: Enrich `sleep_completed` event data

**File:** `nous/handlers/sleep_handler.py`

Pass shared `sleep_stats` dict into phases that produce stats. Phases mutate the dict directly (same pattern as `phases_completed` list).

```python
# In _run_sleep, after phases_completed:
sleep_stats = {"facts_created": 0, "procedures_created": 0, "censors_retired": 0}

# Phase signatures:
async def _phase_reflect(self, sleep_stats: dict) -> bool:
    # ... on each successful fact store:
    sleep_stats["facts_created"] += 1

async def _phase_generalize(self, sleep_stats: dict) -> bool:
    # ... after procedure learning:
    sleep_stats["procedures_created"] += stats.get("decisions_learned", 0)

# censors_retired stays 0 — _phase_prune is a stub (placeholder)

# In sleep_completed event data:
data={
    "phases_completed": phases_completed,
    "interrupted": self._interrupted,
    **sleep_stats,
}
```

### Task A4: Tests for sleep handler fixes

**File:** `tests/test_sleep_handler.py` (new)

- Test that failed phase returns `False` and is NOT in `phases_completed`
- Test that successful phase returns `True` and IS in `phases_completed`
- Test that no-op phase (no LLM client) returns `True` (not a failure)
- Test that `sleep_completed` event includes `facts_created`, `procedures_created`, `censors_retired`
- Test that exceptions are logged with `exc_info=True`

## Phase B: Manual Sleep Trigger Endpoint (#173)

### Task B1: Add `POST /sleep/trigger` endpoint

**File:** `nous/api/rest.py`

The endpoint must check if sleep is already running. Expose `is_sleeping` property on `SleepHandler`.

```python
# In SleepHandler class:
@property
def is_sleeping(self) -> bool:
    return self._sleeping

# In rest.py — sleep_handler must be accessible:
async def trigger_sleep(request: Request) -> JSONResponse:
    """POST /sleep/trigger - Manually trigger a sleep cycle."""
    if sleep_handler.is_sleeping:
        return JSONResponse(
            {"error": "Sleep cycle already in progress"},
            status_code=409,
        )
    await bus.emit(Event(
        type="sleep_started",
        agent_id=settings.agent_id,
        data={"manual": True},
    ))
    return JSONResponse({"status": "started", "message": "Sleep cycle triggered"})
```

Add route before dashboard routes:
```python
Route("/sleep/trigger", trigger_sleep, methods=["POST"]),
```

**Note:** The `sleep_handler` reference needs to be passed into `create_app()` or accessed from the app state. Follow existing pattern for how `bus`, `heart`, `brain` are accessed.

### Task B2: Test for manual sleep trigger

**File:** `tests/test_sleep_handler.py` (add to A4's new file)

- Test `POST /sleep/trigger` returns 200 with `{"status": "started"}`
- Test that 409 is returned if already sleeping
- Test that `sleep_started` event is emitted with `data.manual=True`

## Phase C: Dashboard Activity Backend Rewrite (#174)

### Task C1: Rewrite `get_activity_data()` to match frontend contract

**File:** `nous/api/dashboard_queries.py` (lines 419-524)

Accept `hours` parameter (default 168 = 7 days). Return shape matching frontend expectations:

```python
async def get_activity_data(session: AsyncSession, agent_id: str, hours: int = 168) -> dict:
```

**Return structure:**

```python
{
    "events": [                          # Individual event rows (was: timeline dict)
        {"type": "sleep_completed", "created_at": "...", "data": {...}},
        ...
    ],
    "censor_stats": {
        "total": 16,
        "active": 12,
        "total_activations_7d": 42,      # NEW: from censor_activated events
        "auto_created": 8,               # NEW: count by created_by != 'manual'
        "manual_created": 4,             # NEW: count by created_by = 'manual'
        "false_positives_7d": 3,         # NEW: from events (not cumulative counter)
        "top_censors": [                 # NEW: top 5 by activation_count
            {"trigger_pattern": "...", "activations": 15, "id": "..."},
        ]
    },
    "schedule_stats": {
        "total": 11,
        "active": 3,
        "fires_7d": 7,                   # NEW: count schedule_fired events in 7d
        "next_fires": [                  # NEW: upcoming scheduled fires
            {"task": "...", "next_fire_at": "...", "id": "..."},
        ]
    },
    "sleep_stats": {
        "total_sleeps": 69,
        "last_sleep": "...",             # RENAMED: was last_sleep_at
        "facts_created": 0,              # NEW: from last sleep_completed event data
        "procedures_created": 0,         # NEW
        "censors_retired": 0,            # NEW (placeholder — prune is a stub)
    }
}
```

**SQL queries:**

1. **Events (individual rows):**
   ```sql
   SELECT event_type AS type, created_at, data
   FROM nous_system.events
   WHERE agent_id = :agent_id AND created_at >= :since
   ORDER BY created_at DESC LIMIT 100
   ```
   Note: Column is `data`, NOT `event_data`.

2. **Censor base stats:**
   ```sql
   SELECT COUNT(*) AS total,
          COUNT(*) FILTER (WHERE active = true) AS active,
          COUNT(*) FILTER (WHERE created_by = 'manual') AS manual_created,
          COUNT(*) FILTER (WHERE created_by != 'manual') AS auto_created
   FROM heart.censors WHERE agent_id = :agent_id
   ```

3. **Censor 7d stats (from events, NOT cumulative counters):**
   ```sql
   SELECT COUNT(*) AS total_activations_7d
   FROM nous_system.events
   WHERE agent_id = :agent_id AND event_type = 'censor_activated' AND created_at >= :seven_days_ago
   ```
   Similarly for false positives — count `censor_false_positive` events (or check if false positives are tracked as events; if not, fall back to cumulative counter with a comment).

4. **Top censors:**
   ```sql
   SELECT id::text, trigger_pattern, activation_count AS activations
   FROM heart.censors
   WHERE agent_id = :agent_id AND activation_count > 0
   ORDER BY activation_count DESC LIMIT 5
   ```

5. **Schedule stats:**
   ```sql
   SELECT COUNT(*) AS total,
          COUNT(*) FILTER (WHERE active = true) AS active
   FROM heart.schedules WHERE agent_id = :agent_id
   ```

6. **fires_7d (from events):**
   ```sql
   SELECT COUNT(*) FROM nous_system.events
   WHERE agent_id = :agent_id AND event_type = 'schedule_fired' AND created_at >= :seven_days_ago
   ```

7. **Next fires:**
   ```sql
   SELECT id::text, task, next_fire_at
   FROM heart.schedules
   WHERE agent_id = :agent_id AND active = true AND next_fire_at IS NOT NULL
   ORDER BY next_fire_at LIMIT 5
   ```

8. **Sleep stats:**
   ```sql
   -- Count + last sleep
   SELECT COUNT(*) AS total_sleeps, MAX(created_at) AS last_sleep
   FROM nous_system.events
   WHERE agent_id = :agent_id AND event_type = 'sleep_started'
   ```
   ```sql
   -- Last sleep_completed event data for facts/procedures/censors
   SELECT data FROM nous_system.events
   WHERE agent_id = :agent_id AND event_type = 'sleep_completed'
   ORDER BY created_at DESC LIMIT 1
   ```
   Extract `facts_created`, `procedures_created`, `censors_retired` from JSONB `.get()` with default 0.

### Task C2: Wire `hours` query param in REST handler

**File:** `nous/api/rest.py` (line 807-817)

```python
async def dashboard_activity(request: Request) -> JSONResponse:
    """GET /dashboard/activity - Activity timeline data."""
    try:
        hours = int(request.query_params.get("hours", "168"))
    except ValueError:
        return JSONResponse({"error": "hours must be an integer"}, status_code=400)
    hours = max(1, min(hours, 720))  # Cap 1h to 30d
    try:
        from nous.api.dashboard_queries import get_activity_data
        async with database.session() as session:
            data = await get_activity_data(session, settings.agent_id, hours=hours)
        return JSONResponse(data)
    except Exception as e:
        logger.error("Dashboard activity error: %s", e)
        return JSONResponse({"error": str(e)}, status_code=500)
```

### Task C3: Update tests

**File:** `tests/test_dashboard_queries.py`

**Rewrite** `TestGetActivityData` (not incremental update — old assertions reference removed keys):

- `test_empty_state`: Assert `events` is empty list, `censor_stats.total_activations_7d == 0`, `schedule_stats.fires_7d == 0`, `sleep_stats.last_sleep is None`, `sleep_stats.facts_created == 0`
- `test_with_events`: Insert events, assert `events` is list with correct types, `sleep_stats.last_sleep` is not None
- `test_censor_stats_7d`: Insert `censor_activated` events with varying timestamps, verify 7d windowing
- `test_top_censors`: Insert censors with varying activation_count, verify top 5 ordering
- `test_next_fires`: Insert schedules with `next_fire_at`, verify ordering
- `test_hours_param`: Verify filtering respects hours parameter
- `test_sleep_stats_from_completed_event`: Insert `sleep_completed` event with data, verify facts_created etc.

## Execution Plan

**Parallel agents:**
- `dev-sleep`: Tasks A1 + A2 + A3 + A4 + B1 + B2 (sleep_handler.py + rest.py sleep route + is_sleeping property)
- `dev-dashboard`: Tasks C1 + C2 + C3 (dashboard_queries.py + rest.py hours handler + tests)

**Sequential after both complete:**
- Code review agent validates all changes
- Run full test suite

## Files Changed

| File | Changes |
|------|---------|
| `nous/handlers/sleep_handler.py` | exc_info, bool returns, sleep_stats dict, is_sleeping property |
| `nous/api/rest.py` | POST /sleep/trigger route (409 check), hours param + validation for dashboard_activity |
| `nous/api/dashboard_queries.py` | Rewrite get_activity_data() for frontend contract |
| `tests/test_sleep_handler.py` | New test file for sleep handler + trigger fixes |
| `tests/test_dashboard_queries.py` | Rewrite TestGetActivityData for new shape |
