# Subtask & Schedule Improvements Design

**Date:** 2026-03-08
**Status:** Approved

## Problem

The subtask system has several operational issues:

1. **Model waste:** Subtasks fall back to the main chat model (`NOUS_MODEL`) instead of `NOUS_BACKGROUND_MODEL`, running background work on expensive models unnecessarily.
2. **Noisy notifications:** Per-subtask `notify` defaults to `True`, meaning all subtasks notify via Telegram once credentials are configured.
3. **Error swallowing:** `runner.py` catches exceptions in `run_turn()` and `stream_chat()`, replaces them with a generic message, and returns normally. The subtask worker then marks the task as "completed" with a useless result.
4. **Schedule gaps:** `schedule_task` doesn't support `model` or `frame_type` parameters, so scheduled tasks (often recurring background work) can't use cheaper models or frame-specific tooling.

## Changes

### 1. Subtask default model → `background_model`

**Files:** `nous/api/tools.py`, `nous/handlers/subtask_worker.py`

Update the model fallback chain for subtask execution:
- Current: explicit model → frame default → `settings.model`
- New: explicit model → frame default → `settings.background_model`

Affects both inline execution (`spawn_task` with `await_result=True`) and background worker execution.

### 2. Flip `notify` default to `False`

**Files:** `nous/heart/subtasks.py`, `nous/api/tools.py`, `nous/heart/schedules.py`

- Change `notify: bool = True` → `notify: bool = False` in SubtaskRequest dataclass
- Flip default in `schedule_task` tool and `schedules.create()`
- Users opt-in to notifications per-task with `notify=True`

### 3. Re-raise exceptions from `run_turn()` after cleanup

**File:** `nous/api/runner.py`

In both `run_turn()` (~line 382) and `stream_chat()` (~line 913):
- Keep generic message in conversation history (user-facing)
- After post-turn cleanup completes, re-raise the original exception
- Non-subtask callers (REST API, Telegram bot) already have their own try/except
- Subtask worker's existing `except` block then correctly marks status as "failed" with the real error message

### 4. Add `model` and `frame_type` to scheduled tasks

**Files:** `sql/init.sql`, `nous/storage/models.py`, `nous/heart/schedules.py`, `nous/api/tools.py`, `nous/handlers/task_scheduler.py`

- Add two nullable columns to `nous_system.schedules`: `model VARCHAR(100)`, `frame_type VARCHAR(20)`
- Accept `model` and `frame_type` parameters in `schedule_task` tool
- Store on Schedule record at creation time
- Pass through when scheduler creates subtasks from due schedules
- Worker uses them in the fallback chain: explicit model → frame default → `background_model`

### 5. Migration script

**File:** `sql/migrations/add_schedule_model_frame.sql`

```sql
ALTER TABLE nous_system.schedules ADD COLUMN IF NOT EXISTS model VARCHAR(100);
ALTER TABLE nous_system.schedules ADD COLUMN IF NOT EXISTS frame_type VARCHAR(20);
```

## Files Changed

| Area | Files |
|------|-------|
| Runner error handling | `nous/api/runner.py` |
| Model fallback | `nous/api/tools.py`, `nous/handlers/subtask_worker.py` |
| Notify default | `nous/heart/subtasks.py`, `nous/api/tools.py`, `nous/heart/schedules.py` |
| Schedule model/frame | `sql/init.sql`, `nous/storage/models.py`, `nous/heart/schedules.py`, `nous/api/tools.py`, `nous/handlers/task_scheduler.py` |
| Migration | `sql/migrations/add_schedule_model_frame.sql` |

~8 files, ~60 lines changed. No new env vars. One schema migration.
