# F015 — Subtask Creation Hardening & Configurability

**Status:** Draft
**Author:** Emerson (spec), Tim (requirements)
**Created:** 2026-03-06
**Priority:** High

---

## Problem

Subtask creation works but lacks fine-grained control. Timeout and tool limits are global settings — there's no way to configure them per-subtask-type or per-frame. The agent can spawn subtasks without guardrails on resource consumption, and there's no validation of subtask instructions before execution.

### Current State (Code Analysis)

**Config (config.py):**
| Setting | Default | Env Var |
|---------|---------|---------|
| `subtask_enabled` | `True` | — |
| `subtask_workers` | `2` | — |
| `subtask_poll_interval` | `2.0s` | — |
| `subtask_default_timeout` | `120s` | — |
| `subtask_max_timeout` | `600s` | — |
| `subtask_max_concurrent` | `3` | — |
| `subtask_tool_call_limit` | `20` | — |
| `inline_subtask_timeout` | `90s` | — |
| `frame_default_models` | `{}` | — |

**Flow (tools.py → subtask_worker.py):**
1. Agent calls `spawn_task(task, priority, timeout, frame_type, await_result, model)`
2. `create_subtask_tools()` clamps timeout to `subtask_max_timeout`
3. Task stored in DB via `heart.subtasks.create()`
4. Worker pool dequeues and calls `runner.run_turn()` with `is_subtask=True`
5. Subtask gets `subtask_tool_call_limit` max tools, no delegation tools (spawn/schedule excluded)
6. On completion: stores result, emits event, optionally notifies Telegram

**Gaps identified:**
- No per-frame timeout overrides (a research subtask needs more time than a quick lookup)
- No per-frame tool limits (a debug subtask may need more tools than a creative one)
- No validation of subtask task text (empty or trivially short tasks are accepted)
- No cost/resource tracking per subtask
- No limit on total concurrent subtasks per session (only global limit exists)
- No subtask depth protection (subtasks can't spawn subtasks, but this is enforced by tool exclusion — not explicitly tracked)
- Worker count and poll interval not configurable via env vars
- No priority queue — all subtasks are FIFO regardless of `priority` field

---

## Proposed Changes

### Phase 1: Configurable Timeouts & Tool Limits

#### 1.1 Per-Frame Configuration

Add to `config.py`:

```python
# Frame-specific subtask overrides (merged with defaults)
subtask_frame_config: dict[str, dict[str, int]] = Field(
    default={
        "task": {"timeout": 120, "tool_limit": 20},
        "question": {"timeout": 60, "tool_limit": 10},
        "decision": {"timeout": 180, "tool_limit": 15},
        "debug": {"timeout": 180, "tool_limit": 25},
        "creative": {"timeout": 90, "tool_limit": 10},
        "conversation": {"timeout": 60, "tool_limit": 10},
        "initiation": {"timeout": 60, "tool_limit": 5},
    },
    validation_alias="NOUS_SUBTASK_FRAME_CONFIG",
)
```

Update `create_subtask_tools()` in tools.py to use frame config when no explicit timeout/tool_limit is provided:

```python
frame_config = settings.subtask_frame_config.get(frame_type or "task", {})
effective_timeout = min(
    timeout or frame_config.get("timeout", settings.subtask_default_timeout),
    settings.subtask_max_timeout,
)
effective_tool_limit = frame_config.get("tool_limit", settings.subtask_tool_call_limit)
```

#### 1.2 Env Var Exposure for Worker Settings

Add `validation_alias` to existing settings:

```python
subtask_workers: int = Field(default=2, validation_alias="NOUS_SUBTASK_WORKERS")
subtask_poll_interval: float = Field(default=2.0, validation_alias="NOUS_SUBTASK_POLL_INTERVAL")
subtask_max_concurrent: int = Field(default=3, validation_alias="NOUS_SUBTASK_MAX_CONCURRENT")
```

#### 1.3 Task Text Validation

In `spawn_task()`, before creating:

```python
# Validate task text
task = task.strip()
if len(task) < 10:
    return _mcp_response("Error: Subtask description too short (min 10 chars)")
if len(task) > 5000:
    task = task[:5000]  # Truncate silently
```

### Phase 2: Resource Tracking & Session Limits

#### 2.1 Per-Session Subtask Budget

```python
subtask_max_per_session: int = Field(
    default=10, validation_alias="NOUS_SUBTASK_MAX_PER_SESSION"
)
```

Track in `spawn_task()`:
```python
active_count = await heart.subtasks.count_by_session(_session_id, status=["pending", "running"])
if active_count >= settings.subtask_max_per_session:
    return _mcp_response(f"Error: Session subtask limit reached ({settings.subtask_max_per_session})")
```

#### 2.2 Token Usage Tracking

Store `input_tokens` and `output_tokens` on the Subtask model. `run_turn()` already returns usage — pipe it to `heart.subtasks.complete()`.

#### 2.3 Priority Queue

Replace FIFO dequeue with priority-aware ordering:
```sql
SELECT * FROM subtasks
WHERE status = 'pending'
ORDER BY
    CASE priority WHEN 'urgent' THEN 0 WHEN 'normal' THEN 1 WHEN 'low' THEN 2 END,
    created_at ASC
LIMIT 1
FOR UPDATE SKIP LOCKED
```

---

## Affected Files

| File | Change |
|------|--------|
| `nous/config.py` | Add `subtask_frame_config`, env aliases, session limit |
| `nous/api/tools.py` | Frame-aware timeout/tool resolution, task validation |
| `nous/handlers/subtask_worker.py` | Priority queue, usage tracking |
| `nous/heart/subtasks.py` | `count_by_session()`, priority dequeue |
| `nous/storage/models.py` | Add `input_tokens`, `output_tokens` to Subtask |

---

## Success Metrics

- Subtask timeouts respect frame-specific defaults
- Tool call limits vary by frame type
- Empty/trivial subtask descriptions are rejected
- Session-level subtask limits prevent runaway spawning
- Token usage per subtask is tracked and queryable

---

## Open Questions

1. Should inline (`await_result=true`) subtasks share the parent's tool call count toward the parent's max_turns? Currently they don't — a parent at turn 9/10 can spawn an inline subtask that runs 20 more tool calls.
2. Should subtask results be cached? Identical tasks within a session could reuse results.
