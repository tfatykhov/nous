# Harness Phase 2.8 — Compensation Registry + Proceed-Default for Undoable Approvals

**Goal:** Make actions undoable, and let DAG approval nodes default to 'proceed' when — and only when — every action behind them is declared compensable.

**Decision:** 695c356d (Tim approved 2026-09-26).

**Spec:** `docs/superpowers/plans/2026-09-24-harness-autonomy-roadmap.md` §5 P2.8; §2 row P2.8 ("No revert executor").

**Anchors from `main` at `0223d3b`:** Phases 1–3 merged (PRs #646–#650).

## Design

### 1. Compensation Registry

The existing `tool_classes.py` declares a tool's risk class in a static `TOOL_CLASSES` table. Compensation extends this: a tool may have a **compensator** — an async function that undoes a successful call given its ledger row.

**Shape:** A new leaf module `nous/api/compensation.py` holds the registry. Registration is `register(tool_name, compensator_fn)` where `compensator_fn: async (ledger_entry_id, snapshot_data) -> CompensationResult`. The registry is separate from `TOOL_CLASSES` because:
- `tool_classes.py` is a leaf with no imports from `nous` (the claim verifier, ActionGate, ledger all read it at import time)
- A compensator needs the database and domain objects, so it must be registered at runtime like tool handlers

**Compensable tools (v1):**

| Tool | Compensator | Snapshot |
|------|-------------|----------|
| `write_file` | Restore prior content (or delete if file was new) | Prior file content + whether file existed, stored in `nous_system.compensation_snapshots` |
| `schedule_task` | Cancel the created schedule | Schedule id from tool result |
| `heartbeat_check_create` | Disable the created check | Check name from tool args |
| `heartbeat_check_manage` | Reverse the action (enable↔disable) | Prior enabled state |
| `resolve_decision` | Restore prior outcome | Prior outcome/resolution_note from before the resolve |

**NOT compensable (explicitly):**

| Tool | Why |
|------|-----|
| `send_email` | Cannot unsend |
| `send_file` | Cannot unsend |
| `bash` | Arbitrary commands — no general undo |
| `run_python` | Arbitrary code — no general undo |
| `learn_fact` | Fact admission pipeline makes reversal non-trivial (dedup, graph links) |
| `record_decision` | Decision recording has downstream effects (calibration, graph) |
| `spawn_task` / `dag_create` | Spawned work may have side effects |

The `TOOL_CLASSES` table gains a `compensable: bool` field (default `False`). This is the **declaration** (readable at import time); the actual compensator function is registered at runtime.

### 2. Compensation Snapshots

Before a compensable tool dispatches, the runner captures a snapshot of the state that will change. Snapshots are stored in a new DB table `nous_system.compensation_snapshots` (migration 077).

```sql
CREATE TABLE nous_system.compensation_snapshots (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    ledger_entry_id UUID NOT NULL REFERENCES nous_system.execution_ledger(id),
    agent_id TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    snapshot_data JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    reverted_at TIMESTAMPTZ,
    revert_result TEXT
);
CREATE INDEX idx_compensation_snapshots_ledger ON nous_system.compensation_snapshots(ledger_entry_id);
```

The snapshot is captured BEFORE dispatch (in the runner's `_open_for_call` path) and linked to the ledger entry.

### 3. `review.revert` Action Handler

Registered on `ActionRouter`. The handler:
1. Reads `trace_id` from the action context → resolves to a `ledger_entry_id`
2. Looks up the compensation snapshot
3. Looks up the compensator function from the registry
4. Calls the compensator with the snapshot data
5. Records the result (success/already-reverted/failed) on the snapshot row
6. Updates the surface to show revert status

**Idempotency:** Double revert is a no-op — `reverted_at IS NOT NULL` means "already done".

**Security:** The existing test (`test_a2ui_actions.py:570`) verifies that a forged `review.revert` on a card that does not offer it is rejected at the allowlist. The handler additionally requires a real ledger row (no snapshot = no revert).

### 4. Auto Action Review

After a background turn performs a compensable mutating action, push an `action_review` surface automatically. The builder gains a Revert button when `compensation.revertible` is true AND the compensation registry has a compensator for the tool.

Non-compensable external actions (`send_email`, `send_file`) get a review card stating plainly they cannot be undone.

**Wiring:** The runner's `_ledger_close` path (on `status='success'` for a background mutating call) fires `_maybe_push_action_review`. Gated by `NOUS_COMPENSATION_AUTO_REVIEW_ENABLED` (default `false`).

### 5. Proceed-Default for Approval Nodes

**The design fork — how undoability is declared for a free-form LLM subtask node:**

A subtask node runs a free-form LLM turn with access to any offered tool. We cannot know at DAG-create time what tools the LLM will call. The solution: an explicit `undoable: true` field on `DAGNodeSpec` that the agent sets when authoring the DAG. When `undoable` is true, the harness enforces at runtime that every dispatched tool call from that node is compensable (per `TOOL_CLASSES.compensable`). If a non-compensable tool is called, the harness refuses it with an error ("this node is declared undoable; {tool} is not compensable").

This is enforced at the same `_authorize_tool_call` choke point as the context policy, as a new violation code `not_compensable`.

**Validator change:** `DAGNodeSpec._validate_approval_fields` relaxes to allow `default_option` to be a 'proceed' option IF:
1. `NOUS_DAG_APPROVAL_PROCEED_DEFAULT_ENABLED` is true
2. Every successor node reachable from this approval via dependency/context_flow edges has `undoable: true`

The check is in `DAGCreateRequest._validate_graph` (which already walks the graph), not in `DAGNodeSpec` (which sees only itself).

**Flag:** `NOUS_DAG_APPROVAL_PROCEED_DEFAULT_ENABLED` (default `false`). When false, the existing "default_option MUST be a 'stop' option" rule holds.

---

## Tasks

### Task 1: Compensation Registry + ToolClass.compensable

**Files:**
- Modify: `nous/api/tool_classes.py`
- Create: `nous/api/compensation.py`
- Test: `tests/test_compensation.py`

- [ ] Add `compensable: bool = False` to `ToolClass` dataclass
- [ ] Mark compensable tools in `TOOL_CLASSES`: `write_file`, `schedule_task`, `heartbeat_check_create`, `heartbeat_check_manage`, `resolve_decision`
- [ ] Create `nous/api/compensation.py` with `CompensationRegistry`, `CompensationResult`, `register()`, `get()`, `is_compensable()`
- [ ] Tests: registry stores and retrieves compensators; `is_compensable` matches `TOOL_CLASSES`

### Task 2: Compensation Snapshots (migration + model + capture)

**Files:**
- Create: `sql/migrations/077_compensation_snapshots.sql`
- Modify: `nous/storage/models.py`
- Modify: `nous/api/compensation.py` (add `SnapshotStore`)
- Test: `tests/test_compensation.py`

- [ ] Migration 077: `compensation_snapshots` table
- [ ] ORM model `CompensationSnapshot`
- [ ] `SnapshotStore` with `capture()`, `get()`, `mark_reverted()` methods
- [ ] Tests: capture creates a row; mark_reverted is idempotent

### Task 3: Compensator implementations

**Files:**
- Modify: `nous/api/compensation.py`
- Test: `tests/test_compensation.py`

- [ ] `compensate_write_file`: read prior content before write; on revert, restore or delete
- [ ] `compensate_schedule_task`: cancel the schedule
- [ ] `compensate_heartbeat_check_create`: disable the check
- [ ] `compensate_heartbeat_check_manage`: reverse enable↔disable
- [ ] `compensate_resolve_decision`: restore prior outcome
- [ ] Tests for each compensator

### Task 4: Snapshot capture in the runner

**Files:**
- Modify: `nous/api/runner.py`
- Modify: `nous/api/compensation.py`
- Test: `tests/test_compensation.py`

- [ ] Before dispatch of a compensable tool in background context, call `SnapshotStore.capture()`
- [ ] Pass ledger_entry_id to the capture
- [ ] Wire `CompensationRegistry` and `SnapshotStore` on `AgentRunner`

### Task 5: `review.revert` handler + builder Revert button

**Files:**
- Modify: `nous/a2ui/actions.py`
- Modify: `nous/a2ui/builders/action_review.py`
- Modify: `tests/test_a2ui_actions.py`
- Modify: `tests/test_a2ui_builders.py`

- [ ] Register `review.revert` handler on `ActionRouter`
- [ ] Handler: look up snapshot → run compensator → mark reverted → update surface
- [ ] Builder: when `compensation.revertible` is true, add Revert button + `review.revert` to allowed_actions
- [ ] Update existing tests: `test_action_review_withholds_revert_even_when_revertible` → now OFFERS revert
- [ ] Keep test that forged revert on non-revertible card is rejected at allowlist

### Task 6: Auto action_review push + config flag

**Files:**
- Modify: `nous/config.py`
- Modify: `nous/api/runner.py` (or appropriate wiring point)
- Test: `tests/test_compensation.py`

- [ ] `NOUS_COMPENSATION_AUTO_REVIEW_ENABLED: bool = False`
- [ ] `NOUS_COMPENSATION_ENABLED: bool = False` (master switch)
- [ ] After successful background mutating dispatch, push action_review surface
- [ ] Tests: flag off = no surface; flag on = surface pushed with correct compensation block

### Task 7: Proceed-default for approval nodes

**Files:**
- Modify: `nous/dag/schemas.py`
- Modify: `nous/config.py`
- Modify: `nous/api/tool_policy.py` (or runner choke point)
- Modify: `nous/api/tools.py` (dag_create description)
- Modify: `tests/test_dag_schemas.py`
- Test: `tests/test_compensation.py`

- [ ] Add `undoable: bool = False` to `DAGNodeSpec`
- [ ] `NOUS_DAG_APPROVAL_PROCEED_DEFAULT_ENABLED: bool = False`
- [ ] Relax validator: allow proceed default when flag on + all downstream nodes are undoable
- [ ] Runtime enforcement: refuse non-compensable tools on undoable nodes (violation `not_compensable`)
- [ ] Update `dag_create` tool description when flag is on
- [ ] Tests: proceed-default rejected when flag off; accepted when flag on + all undoable; rejected when not all undoable; non-compensable tool refused on undoable node

---

## Flags

| Setting | Default | Description |
|---------|---------|-------------|
| `NOUS_COMPENSATION_ENABLED` | `false` | Master switch for the compensation registry. When false, no snapshots are captured and compensators are not registered. |
| `NOUS_COMPENSATION_AUTO_REVIEW_ENABLED` | `false` | Push an action_review surface automatically after a compensable background mutation. Requires `NOUS_COMPENSATION_ENABLED`. |
| `NOUS_DAG_APPROVAL_PROCEED_DEFAULT_ENABLED` | `false` | Allow approval nodes to default to 'proceed' when all downstream nodes are declared undoable. Requires `NOUS_COMPENSATION_ENABLED`. |

## Deliberately deferred

- Compensators for `learn_fact`, `record_decision`, `create_censor` (graph/admission side effects make reversal non-trivial)
- Compensators for `bash` / `run_python` (no general undo for arbitrary commands)
- Auto-push for interactive contexts (only background turns get auto-review in v1)
- Compensation cost tracking / budget
- UI for browsing compensation history
