# F087 — DAG Durability Spine

**Date:** 2026-08-08
**Status:** design approved, implementing
**Depends on:** F038 (DAG orchestration), F038.1 (completion check), F046 (node timeouts), F064.1–.6 (Symphony adoptions), F066.1 (fix stage), F009 (subtask result delivery)
**Migration:** `069_dag_durability_spine.sql`

---

## Problem

The DAG state machine is sound. Every gap is at the edges, where the subsystem meets the rest of Nous.

### P1 — Result delivery to the main loop does not exist

`DAGOrchestrator.__init__` accepts `bus` and assigns `self._bus` (`orchestrator.py:79`), then never reads it. `grep emit|publish` across `nous/dag/` returns zero matches. `_check_dag_completion` writes `result_summary` to a row and stops: no event, no Telegram, no memory write.

`_launch_subtask_node` (`orchestrator.py:1261`) also calls `subtask_mgr.create()` without `notify=`, which defaults `False`, so the per-node subtask notifications are off too.

A multi-hour DAG completes in total silence. The only way to learn the outcome is to poll `dag_manage` or open the dashboard.

### P1 — No wall-clock backstop on `running` nodes

`_effective_timeout` is consumed only at launch (handed down to the subtask/check) and inside `_poll_awaiting_checks`. Nothing checks elapsed time for `status='running'`. Timeout enforcement is fully delegated to the subtask executor.

`reclaim_stale` runs once at worker start and only touches rows already past their timeout, so a crash mid-run with time left on the clock orphans the subtask row in `running` permanently. The DAG node then stays `running` forever, which keeps its DAG `running` forever, which consumes one of `MAX_ACTIVE_DAGS = 5` forever. Five orphans brick `dag_create` permanently.

### P1 — Token budget enforcement is unreachable code

`DAGStore.update_dag_tokens` (`store.py:364`) has no caller anywhere in `nous/` — its only callers are two tests. `tokens_consumed` is therefore structurally always `0`, so the `ratio >= 1.0` branch in `_advance_dag` step 2 can never fire. `token_budget` is a knob the agent can set that does nothing.

### P2 — The tick is silently unwired when heartbeat is off

`main.py:816` guards the tick wiring with `if heartbeat_runner is not None`, but `register_dag_tools` at `:821` runs unconditionally. With `NOUS_HEARTBEAT_ENABLED=false` the agent creates DAGs that never advance, and nothing reports it.

### P2 — F064.1 stall detection ships dark

`NOUS_DAG_STALL_DETECTION_ENABLED` defaults `false`, so the one built backstop for hung nodes is inert in production.

---

## Design

Additive seams only. `_advance_dag`'s existing steps are not rewritten.

### 1. Durable delivery state machine

The governing constraint: **`EventBus.emit` drops on `QueueFull` and never blocks** (`events.py:180-184`). The bus is lossy by design, so it can be a delivery leg but never the durability mechanism. Durability comes from the database.

Migration `069` adds to `nous_system.execution_dags`:

| Column | Type | Purpose |
|---|---|---|
| `delivered_at` | `TIMESTAMPTZ NULL` | set once the result has left the box |
| `delivery_attempts` | `INT NOT NULL DEFAULT 0` | bounds retry |
| `delivery_error` | `TEXT NULL` | last failure, and the give-up reason |

plus a partial index on `(agent_id)` where the DAG is terminal and `delivered_at IS NULL`.

**Reaching terminal and being delivered are two separate transitions.** `_check_dag_completion` is unchanged — it sets terminal status and nothing more. A new `tick()` phase, `_deliver_terminal_dags()`, sweeps `status IN (completed, failed, partial, cancelled) AND delivered_at IS NULL` and delivers.

A crash between the two writes re-delivers on the next tick. This is at-least-once by construction and survives process restart, because the queue is a table rather than memory.

Retries are bounded by `dag_delivery_max_attempts` (default 5). On exhaustion the DAG is marked delivered with `delivery_error` populated — it stops looping but stays visible rather than vanishing.

### 2. Three delivery legs — `nous/dag/delivery.py`

A `DAGResultDelivery` collaborator, injected optionally so the orchestrator stays unit-testable without it. Each leg is independently flagged and independently wrapped in try/except; one leg failing never suppresses the others.

- **Bus emit** (`dag_delivery_bus_enabled`, default on) — `dag.completed` / `dag.failed` carrying dag id, name, status, per-node outcomes and token totals. Best-effort. This is the extensibility seam for follow-up work and audit.
- **Agent-authored summary** (`dag_delivery_agent_summary_enabled`, default off) — a background turn reads the finished DAG and writes prose. Bounded by `dag_delivery_agent_summary_timeout_seconds`; on timeout or failure it falls back to the deterministic template. It can never block delivery. Because it runs as a normal cognitive turn, its episode lands in Heart on its own, which is how the outcome reaches the next conversation's context without a separate leg.
- **Telegram push** (`dag_delivery_telegram_enabled`, default on) — sends the agent summary when available, the deterministic template otherwise. Mirrors `subtask_worker._notify_telegram`.

**Success rule.** `delivered_at` is set when every *required* leg succeeds. Telegram is required only when both a bot token and a chat id are configured; bus emit and agent summary are always best-effort. If Telegram is not configured, delivery completes after the bus emit is attempted — no retry loop against a channel that does not exist.

Per-node subtask `notify` stays opt-in per node rather than forced globally: a 20-node DAG must not produce 20 Telegram messages plus the summary.

### 3. Wall-clock node reaper

New step in `_advance_dag` after the sync and awaiting-check phases, before stall detection. A `running` node whose `now - started_at` exceeds `_effective_timeout(node) + grace` gets `_cancel_node()` first to tear down the primitive, then `status='failed'` — the ordering F064.1 established. Cascade handles the rest on the same tick, unchanged.

`NOUS_DAG_NODE_TIMEOUT_GRACE_SECONDS` defaults to 300 so the subtask executor always gets first chance to fail the node with its own richer error. The reaper only catches the case where the primitive is gone or wedged.

**This defaults ON.** A backstop that ships dark is not a backstop, and it only fires in an already-broken state. Kill switch: `NOUS_DAG_NODE_REAPER_ENABLED=false`. This is what unwedges the `MAX_ACTIVE_DAGS` slot leak.

### 4. Token accounting

`Subtask` already carries `tokens_in`/`tokens_out` (`models.py:874-875`), so `_sync_subtask_node` calls `store.update_dag_tokens()` on the running→terminal edge with no new plumbing. The trigger is the *subtask* reaching terminal rather than the node, so the `awaiting_check` path (subtask done, node still polling a shell command) is counted too — its tokens are already final.

`_sync_subtask_node` is re-entrant across ticks, so migration `069` also adds `dag_nodes.tokens_counted BOOLEAN NOT NULL DEFAULT false`, set in the same UPDATE that marks the node terminal. A re-sync cannot double-count.

Accounting goes live immediately — it is pure observability and the dashboard already reads the column. **Enforcement stays dark** behind `NOUS_DAG_TOKEN_BUDGET_ENFORCEMENT_ENABLED=false`. That `ratio >= 1.0` branch has never once executed in production; turning it on silently would begin cancelling DAGs for anyone who set `token_budget` casually. Measure first, then flip.

### 5. Fail-loud wiring

`DAGOrchestrator` gains `clock_wired: bool` set by whoever installs the tick — explicit, because inferring from `last_tick_at` would false-negative during the first tick interval — plus `last_tick_at` for observability.

`main.py` logs at ERROR when `dag_enabled` is true but no heartbeat runner exists. `dag_create` refuses with a message naming the cause instead of creating a DAG that will never advance. Both surface in `dag_manage list`.

### 6. Stall detection (F064.1)

No code change. An end-to-end test walks `runner._tool_loop` → `dag_store.touch_activity` to prove the three ping sites fire, then the recommended flip is documented in CLAUDE.md. The wall-clock reaper is the hard bound; stall detection is the finer-grained early kill, and it should be turned on by an operator flag flip rather than by changing a default here.

---

## Settings

| Variable | Default | Description |
|---|---|---|
| `NOUS_DAG_RESULT_DELIVERY_ENABLED` | `true` | master switch for the delivery sweep |
| `NOUS_DAG_DELIVERY_BUS_ENABLED` | `true` | emit `dag.completed` / `dag.failed` |
| `NOUS_DAG_DELIVERY_TELEGRAM_ENABLED` | `true` | push the summary to Telegram when configured |
| `NOUS_DAG_DELIVERY_AGENT_SUMMARY_ENABLED` | `false` | agent-authored prose summary (costs an LLM turn) |
| `NOUS_DAG_DELIVERY_AGENT_SUMMARY_TIMEOUT_SECONDS` | `120` | bound on that turn; falls back to template |
| `NOUS_DAG_DELIVERY_MAX_ATTEMPTS` | `5` | retries before giving up loudly |
| `NOUS_DAG_DELIVERY_BATCH_SIZE` | `5` | DAGs delivered per tick |
| `NOUS_DAG_NODE_REAPER_ENABLED` | `true` | wall-clock backstop on running nodes |
| `NOUS_DAG_NODE_TIMEOUT_GRACE_SECONDS` | `300` | grace past the node timeout before reaping |
| `NOUS_DAG_TOKEN_BUDGET_ENFORCEMENT_ENABLED` | `false` | act on the now-live `tokens_consumed` |

---

## Testing

- `tests/test_dag_delivery.py` — state machine, crash-resume, bounded retry, per-leg isolation, Telegram-unconfigured path, agent-summary fallback.
- `tests/test_dag_wiring.py` — fail-loud when the clock is unwired.
- Extensions to `tests/test_dag_orchestrator.py` — reaper fires past grace, does not fire inside grace, cancels the primitive before marking failed; token accounting idempotency across re-sync.
- The existing 4,806 lines of DAG tests must stay green. Every change is additive and, except the reaper, flag-gated off.

---

## Out of scope

Deliberately excluded:

- Moving the tick off the heartbeat loop onto its own asyncio task (the independent-clock option was declined; only the fail-loud guard is added).
- Phase-2 Critic gate evaluation — `gate` nodes still auto-pass.
- F064.4 / F064.5 v2 consumers (skill runtime metadata enforcement, LLM thread continuity).
