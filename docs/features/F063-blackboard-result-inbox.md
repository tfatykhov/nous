# F063 — Blackboard Result Inbox

**Status:** 📝 Draft (2026-05-18)
**Owner:** nous core
**Scope:** Add a general-purpose `wait_results(task_ids, ...)` mailbox primitive that lets a parent agent fan-out N async subtasks and collect their results — enabling Schmid P2 Fan-Out without manual polling loops. Doc 019 R2.
**Family:** Subtask Hardening (Doc 019 R2). See also F061 (outcome correctness), F062 (typed spawn_sync).
**Related:** F064 (Symphony — depends on F061/F062/F063 landing first)

---

## Problem

Nous covers Schmid P2 Fan-Out *structurally* (DAGs provide N-to-parent wait via completion_check), but there is **no general-purpose programmatic mailbox** a conversational agent can use to fan out N async subtasks and block until M of N complete.

### Today's gap
```python
# Fan-out: OK
task_ids = [spawn_task(task=t) for t in tasks]  # fires N subtasks

# Wait: broken — agent must poll manually, inject loops into its own turn
# No primitive for: "wait until at least 3 of 5 are done, return their results"
```

The agent either:
1. Blocks on each task serially (defeats parallelism), or
2. Invokes `spawn_task(await_result=True)` sequentially (same problem), or
3. Uses a DAG node with `completion_check` (only works for DAG orchestration, not conversational agent turns).

This is the **only active architectural gap** in Nous's Schmid subagent surface (P2 is rated PARTIAL).

---

## Proposed Design

### Concept: Blackboard

A **blackboard** is a named shared workspace:
- Parent writes task IDs to it at fan-out time.
- Each subtask writes its `SubtaskResult` (F062) back to the blackboard when done.
- Parent calls `wait_results(board_id, n_of_m, timeout)` to block until enough results land.

The blackboard is implemented as rows in `heart.subtasks` (no new table needed) with a shared `board_id` tag, plus a lightweight polling helper.

### New tools

#### `create_board(label: str) -> str`
Creates a named blackboard, returns `board_id` (UUID).

```python
board_id = create_board("summarise-urls")
```

#### `fan_out(board_id, tasks: list[str], frame_type=None, timeout_seconds=120) -> list[str]`
Spawns N async subtasks, registers them with the board, returns list of `task_id`s.

```python
task_ids = fan_out(board_id, tasks=["summarise URL1", "summarise URL2", "summarise URL3"])
```

#### `wait_results(board_id, n_of_m: int | None = None, timeout_seconds: int = 300) -> list[SubtaskResult]`
Blocks (polls with exponential back-off) until `n_of_m` tasks on the board complete (default: all), or `timeout_seconds` elapses.

```python
results = wait_results(board_id, n_of_m=2, timeout_seconds=60)
# Returns list[SubtaskResult] for completed tasks; others still running.
```

#### Complete fan-out pattern
```python
board_id = create_board("research-sprint")
fan_out(board_id, tasks=[
    "Summarise paper A",
    "Summarise paper B",
    "Extract key claims from paper C",
])
results = wait_results(board_id, timeout_seconds=180)
# results: list[SubtaskResult] — all three completed
```

---

## Implementation Plan

### Phase 1 — Blackboard storage (≈1h)
- Add `board_id UUID` and `board_label TEXT` columns to `heart.subtasks` (migration).
- Add `create_board()` tool: inserts a row into a new `heart.boards` table (board_id, label, created_at, status).
- Alternatively: store board metadata in a `heart.facts` row tagged `board:{board_id}` — avoids a new table.

### Phase 2 — `fan_out` tool (≈2h)
- Spawns N `spawn_task(await_result=False)` calls, sets `board_id` on each subtask row.
- Returns list of task UUIDs.
- Subtasks inherit F061 terminal-report contract; the schema-validated payload (F062) is read from `heart.subtasks.report_jsonb` (or `result_payload` if F062 chose the new-column path — see F062 Phase 1).

### Phase 3 — `wait_results` tool (≈3h)
- Polls `heart.subtasks WHERE board_id = $1` every 2s (exponential back-off to 10s).
- Treats a row as terminal when `final_outcome IN ('completed', 'incomplete_blocked', 'incomplete_no_terminal', 'validation_failed', 'timed_out', 'errored', 'cancelled')` — i.e. the full F061 outcome set. Returns once `n_of_m` rows have hit any terminal outcome (success or failure).
- On `wait_results` wall-clock timeout: returns whatever subtasks reached a terminal outcome so far; subtasks still running are left untouched (their own timeouts will fire and write `final_outcome='timed_out'` via F061's authoritative outer handler).
- Returns `list[SubtaskResult]` using F062 dataclass — caller can inspect `status` to distinguish completed vs. failed.

**Total LOE estimate:** ~6h (three phases, single engineer). **Requires F062 to land first** (SubtaskResult type).

---

## Alternatives Considered

- **New `heart.boards` table** — clean but adds migration complexity. Using `board_id` column on `heart.subtasks` + a config-table row is sufficient.
- **Event-driven (pg_notify)** — ideal latency but adds async complexity to the polling runner. Deferred: polling at 2s is acceptable for subtask workloads.
- **DAG-as-mailbox** — forces all fan-out through a DAG definition file. Too heavyweight for conversational agent turns. This feature targets the conversational (non-DAG) case.
- **Return results via Telegram notification injection** — Schmid's P1-async variant; possible as a follow-on, not the primary design.

---

## Schmid P2 Coverage After F063

| Schmid Concept | Nous Implementation |
|---|---|
| `spawn_agent` (immediate return) | `fan_out(board_id, tasks)` ✅ |
| `wait_agent` (global mailbox) | `wait_results(board_id)` ✅ |
| N-of-M collection | `wait_results(n_of_m=N)` ✅ |
| Per-agent kill | Not in scope (P3) |
| Persistent agent state | Not in scope (P3) |

**P2 moves from PARTIAL → COVERED once F063 ships.**

---

## Success Criteria

- `fan_out` + `wait_results` round-trip works end-to-end in a conversational turn.
- `wait_results(n_of_m=2)` returns when exactly 2 of 3 tasks complete (third still running).
- Timeout path: partial results returned, remaining tasks not orphaned.
- DAG-based fan-out (existing) continues to work unchanged.
- Schmid P2 assessment updated from PARTIAL → COVERED in memory.
