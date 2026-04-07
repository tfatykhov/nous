# Implementation Plan: Issue #273 — on_complete Callback for Dynamic Heartbeat Checks

**Issue:** #273
**Date:** 2026-04-07
**Status:** Revised (post architecture review)
**Decision:** 7c192a77

## Summary

Add `on_complete_prompt` and `on_complete_tools` fields to dynamic heartbeat checks. When a check detects its terminal condition and self-disables, the callback prompt executes as a mini-task before the check is fully done. 3-layer failure handling: retry once, fail-notify via Telegram, disable anyway.

## Review Fixes Applied

3-agent review (nous-arch, nous-db, nous-devil) identified 3 P1s and 9 P2s:

**P1 fixes:**
1. Replace registry-absence detection with `CheckResult.self_disabled` flag
2. Move callback to background `asyncio.create_task` after tick loop
3. Gate callback behind `_has_budget()` before both attempts

**P2 fixes:**
1. `on_complete_tools` NOT NULL DEFAULT '{}' in migration + `nullable=False` in ORM
2. Re-validate `on_complete_tools` subset when `tools` is updated
3. Move execute logic to `HeartbeatRunner._execute_callback`, not `DynamicCheckLoader`
4. Callback session uses `skip_episode=True, is_subtask=True`
5. Inject prompt guard against re-enabling check in callback context
6. Include on_complete fields in create/list return dicts
7. Set `check_name` on failure Finding with `source=f"dynamic-callback:{name}"`
8. Handle None DB row for deleted checks in callback path
9. Correct feature tag to `#273` (not F035 which is already shipped)

## Files to Modify

1. `sql/migrations/029_on_complete_callback.sql` — New migration
2. `nous/storage/models.py` — Add fields to DynamicCheckModel
3. `nous/heartbeat/schemas.py` — Add `self_disabled` field to CheckResult
4. `nous/heartbeat/dynamic.py` — DynamicCheck + DynamicCheckLoader changes
5. `nous/heartbeat/runner.py` — `_execute_callback()` + tick integration
6. `nous/api/tools.py` — Tool schema updates
7. `nous/api/rest.py` — REST endpoint updates
8. `tests/test_heartbeat_dynamic.py` — ~20 new tests

## Implementation Steps

### Step 1: Database Migration (029_on_complete_callback.sql)

```sql
-- Issue #273: Add on_complete callback fields to dynamic_checks
ALTER TABLE nous_system.dynamic_checks
  ADD COLUMN on_complete_prompt TEXT,
  ADD COLUMN on_complete_tools TEXT[] NOT NULL DEFAULT '{}';
```

### Step 2: ORM Model (models.py)

Add to `DynamicCheckModel` after `last_error`:
```python
on_complete_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
on_complete_tools: Mapped[list] = mapped_column(ARRAY(String), nullable=False, server_default="{}")
```

### Step 3: CheckResult Schema (schemas.py)

Add to `CheckResult`:
```python
self_disabled: bool = False  # Set by DynamicCheck when it self-disables during run
```

### Step 4: DynamicCheck Class (dynamic.py)

**4a. Constants:**
```python
CALLBACK_RETRY_DELAY_SECONDS = 30
```

**4b. DynamicCheck.__init__:** Add `on_complete_prompt: str | None = None` and `on_complete_tools: list[str] | None = None`. Store as `self.on_complete_prompt` and `self.on_complete_tools` (filtered to ALLOWED_TOOLS).

**4c. DynamicCheck.run():** After `run_turn()` returns, check if the check is no longer in the registry (self-disabled via tool call). If so, set `result.self_disabled = True` on the CheckResult before returning. The check knows its own name and can check the registry... but actually the check doesn't have a registry reference.

**Revised approach:** The `heartbeat_check_manage(action='disable')` tool handler in tools.py sets a flag on the DynamicCheck object directly. Add `self._self_disabled = False` to `__init__`. The manage_check disable path calls `check._self_disabled = True` on the in-memory check before unregistering. Then `run()` reads `self._self_disabled` after `run_turn()` returns and sets it on CheckResult.

**4d. DynamicCheck.signature():** Include `on_complete_prompt` and `on_complete_tools` in signature string.

**4e. DynamicCheckLoader.sync():** Pass `on_complete_prompt` and `on_complete_tools` from DB row to DynamicCheck constructor.

**4f. DynamicCheckLoader.create_check():** Accept `on_complete_prompt` and `on_complete_tools`. Validate `on_complete_tools` is subset of validated check tools. Store in DB. Include in return dict.

**4g. DynamicCheckLoader.manage_check():**
- Add `on_complete_prompt` and `on_complete_tools` to allowed update fields.
- When `tools` is updated, re-validate that current `on_complete_tools` remains a subset. Reject with error if not.
- When `on_complete_tools` is updated, validate subset of current `tools`.
- On `disable` action: set `check._self_disabled = True` on the in-memory DynamicCheck before unregistering.

**4h. DynamicCheckLoader._list_checks():** Include `on_complete_prompt` (truncated to 200 chars) and `on_complete_tools` in list output.

### Step 5: HeartbeatRunner Integration (runner.py)

**5a. _tick():** After the `for check in due_checks` loop completes, check collected results for any `self_disabled=True` DynamicChecks with `on_complete_prompt`. Fire callbacks as background tasks:

```python
# After the for loop, before triage:
for check, result in callback_candidates:
    if self._has_budget():
        asyncio.create_task(
            self._execute_callback(check),
            name=f"callback-{check.name}",
        )
```

**5b. _execute_callback(check: DynamicCheck):** New async method on HeartbeatRunner:
1. Check `_has_budget()` — skip with log warning if exhausted
2. Build callback instruction with prompt guard: "You may NOT re-enable the check that triggered this callback."
3. Run via `self._get_triage_runner().run_turn()` with `skip_episode=True, is_subtask=True, tool_filter=check.on_complete_tools`
4. Track tokens in `self._tokens_used_today`
5. On success: log completion
6. On failure: `asyncio.sleep(CALLBACK_RETRY_DELAY_SECONDS)`, retry once
7. On second failure: send Telegram notification, create warning Finding with `source=f"dynamic-callback:{check.name}"`, `check_name=check.name`
8. Always `end_conversation()` in finally block

**5c. trigger_check():** After check runs successfully, check `result.self_disabled`. If True and `check.on_complete_prompt`, fire `_execute_callback` as background task.

### Step 6: Tool Schema (tools.py)

Add to `heartbeat_check_create` function: pass `on_complete_prompt` and `on_complete_tools` from kwargs.

Add to schema properties:
```python
"on_complete_prompt": {"type": "string", "description": "Prompt to execute when check self-disables (callback)"},
"on_complete_tools": {"type": "array", "items": {"type": "string"}, "description": "Tools for callback (must be subset of check tools)"},
```

Add to `heartbeat_check_manage` updates description mention.

### Step 7: REST API (rest.py)

**dynamic_checks_create:** Pass `on_complete_prompt` and `on_complete_tools` from body to `loader.create_check()`.

**dynamic_checks_update:** Already passes full body as updates — manage_check handles allowed fields. No change needed.

### Step 8: Tests (~20 new tests)

**TestOnCompleteFields (3):**
- on_complete fields stored on DynamicCheck and included in signature
- on_complete_tools filtered to ALLOWED_TOOLS
- self_disabled flag defaults False, settable

**TestOnCompleteExecution (6):**
- successful callback after self-disable
- callback not fired when check doesn't self-disable
- callback not fired when on_complete_prompt is None
- retry on first failure, succeed on retry
- retry fails → Telegram notification + warning Finding
- budget exhausted → callback skipped with log

**TestOnCompleteValidation (4):**
- on_complete_tools must be subset of check tools at creation
- on_complete_tools validated on update
- updating tools rejects if on_complete_tools no longer subset
- on_complete_prompt can be None

**TestOnCompleteCRUD (4):**
- create check with on_complete fields included in return dict
- list includes on_complete fields (prompt truncated)
- update on_complete_prompt and on_complete_tools
- self_disabled flag set by manage_check disable action

**TestRunnerCallback (3):**
- _tick fires background task for self-disabled check with callback
- trigger_check fires callback on self-disable
- callback token usage tracked in daily budget

## Execution Plan

Tasks are grouped by dependency. Groups can be parallelized internally.

**Group A (no dependencies):** Migration + ORM + Schema
- Step 1: Migration SQL
- Step 2: ORM model
- Step 3: CheckResult.self_disabled

**Group B (depends on A):** Core logic
- Step 4: DynamicCheck + DynamicCheckLoader
- Step 5: HeartbeatRunner

**Group C (depends on B):** API surface
- Step 6: Tool schema
- Step 7: REST API

**Group D (depends on B):** Tests
- Step 8: All test classes
