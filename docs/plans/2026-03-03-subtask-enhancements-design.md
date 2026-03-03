# Subtask Enhancements (012.2) — Design

**Date:** 2026-03-03
**Spec:** `docs/implementation/012.2-subtask-enhancements-light.md`
**Approach:** Spec-faithful, all 7 items, single PR

## Decisions

- **All 7 items** implemented as specified
- **Approach A** (spec-faithful): follow spec closely, adapt where actual code diverges
- **Timeout defaults**: keep existing 120s for fire-and-forget, add 90s for inline
- **Frame-to-model mapping**: included (research -> Haiku)
- **Shared prefix builder**: extracted to avoid duplication between inline handler and background worker

## Section 1: Schema & Model

**Migration** (`sql/migrations/012_subtask_frame_type.sql`):
- `ALTER TABLE heart.subtasks ADD COLUMN frame_type VARCHAR(30)`
- `ALTER TABLE heart.subtasks ADD COLUMN model VARCHAR(100)`
- Both nullable, no backfill

**ORM** (`nous/storage/models.py`):
- Add `frame_type: Mapped[str | None]` and `model: Mapped[str | None]` to `Subtask`

**SubtaskManager** (`nous/heart/subtasks.py`):
- Add `frame_type` and `model` params to `create()`

## Section 2: Tool Schema & Handler

**Tool schema** — 3 new optional params on `spawn_task`:
- `frame_type`: enum of valid frame types
- `await_result`: boolean, default false
- `model`: string, free-form

**Handler** (`_handle_spawn_task`):
- Apply `FRAME_DEFAULT_MODELS` mapping when model omitted but frame_type set
- `await_result=False`: existing fire-and-forget path
- `await_result=True`: inline execution via `runner.run_turn()` with guardrails

**Constants**:
- `SUBTASK_TOOL_CALL_LIMIT = 20`
- `INLINE_SUBTASK_DEFAULT_TIMEOUT = 90`
- `FRAME_DEFAULT_MODELS = {"research": "claude-haiku-3-5-..."}`

## Section 3: Runner Changes

**`run_turn()` new kwargs:**
- `is_subtask: bool = False` — filters out `spawn_task`/`schedule_task` from tools
- `max_tool_calls: int | None = None` — enforces tool call ceiling with graceful wrap-up
- `model_override: str | None = None` — passed through to `_call_api()`

**Tool filtering** (no-nesting): remove `spawn_task` and `schedule_task` when `is_subtask=True`. All other tools retained.

**Tool call limit**: cumulative count in tool loop. At limit, append wrap-up message and break.

## Section 4: Worker Enhancements

**Shared prefix builder** — `build_subtask_prefix(task, frame_type)`:
- Base subtask instructions + frame-specific enrichment from `FRAME_TOOLS`
- Used by both inline handler and background worker

**Background worker** passes:
- `is_subtask=True` (no-nesting)
- `max_tool_calls=SUBTASK_TOOL_CALL_LIMIT` (20-call cap)
- `model_override=subtask.model` (per-subtask model)

## Section 5: Testing

16 test cases from the spec:
1. Migration — columns exist, NULL for existing rows
2. Backward compat — spawn_task without new params unchanged
3. Frame-aware context — research frame gets research instructions
4. Inline execution — await_result returns result, record shows completed
5. Timeout — inline 90s default, fire-and-forget keeps existing, override works
6. No-nesting — subtask tools exclude spawn_task/schedule_task
7. Tool call limit — graceful wrap-up at 20 calls
8. Model selection — explicit model, frame-default mapping, omitted uses default
9. Error handling — timeout and exception paths

## Files Changed

| File | Change |
|------|--------|
| `sql/migrations/012_subtask_frame_type.sql` | New: add frame_type + model columns |
| `nous/storage/models.py` | Add frame_type + model to Subtask |
| `nous/heart/subtasks.py` | Add frame_type + model to create() |
| `nous/api/tools.py` | Schema + handler + constants + shared prefix builder |
| `nous/api/runner.py` | is_subtask + max_tool_calls + model_override on run_turn() |
| `nous/handlers/subtask_worker.py` | Use shared prefix, pass guardrail params |

## Files NOT Changed

- `nous/cognitive/layer.py` — no changes to pre_turn/post_turn
- `nous/cognitive/schemas.py` — no changes to TurnContext
- No new tools — spawn_task gains optional parameters
