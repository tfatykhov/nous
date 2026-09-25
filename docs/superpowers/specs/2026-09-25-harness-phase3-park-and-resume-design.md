# Harness Phase 3 — Park-and-Resume: a DAG node waits durably on a human answer (design, v1)

**Roadmap:** `docs/superpowers/plans/2026-09-24-harness-autonomy-roadmap.md` §2 row P0.3, §3 phase 3
("spec + review first; land dark"; the mechanism was deliberately left undecided there).
**Anchors:** `main` `1daa004` (after harness 2c #646, 2a #647, 2b #648).
**Decisions taken with the user (2026-09-25):** mechanism C (new node type + new status); a *stop*
answer fails the node and blocks only its dependents; a parked DAG does not count against
`MAX_ACTIVE_DAGS`.

## 1. Invariant

> A DAG node can wait durably on a human answer, and resumes on the answer or — at its deadline —
> on its declared default. Exactly one of the two happens, once per node attempt, and it survives
> a restart.

## 2. Today (verified)

| Fact | Anchor |
|---|---|
| Gate nodes auto-complete at launch: `result="Gate auto-passed (Phase 1)"` | `nous/dag/orchestrator.py:2411-2420` |
| `nous/dag` has no reference to A2UI, surfaces or approvals | grep |
| The only DAG wait state, `awaiting_check`, is bound to heartbeat-worker evidence, shell polling and `check_attempts` | `orchestrator.py:1088-1250` |
| The wall-clock reaper and stall detection act on `status='running'` only; node timeouts clamp to `NOUS_DAG_NODE_MAX_TIMEOUT` (2 h default) | `orchestrator.py:1540-1609,1809-1857` |
| `approval_gate` (`push_surface`) takes `title`, `options=[{id,label}]`, `summary`, `risk`, `recommendation`, `trace_id`, `expires_hours` (default 24) | `nous/a2ui/builders/approval.py:26-95` |
| `approval.choose` validates the option against the surface's server-side options, patches `/summary`, resolves — and records the choice **nowhere readable**: only `a2ui_actions.context->>'optionId'` (client-verbatim JSON) keeps it; nothing selects from `a2ui_actions`; no event is emitted | `nous/a2ui/actions.py:478-490`; `service.py:848-887` |
| `SurfaceService` locks are in-process only; `resolve()` is a read-modify-write, not a conditional update | `service.py:120,159-174,848-887` |
| `expire_sweep` runs every 15 min, claims `live AND expires_at <= now` under the surface lock, writes `no_objection` | `service.py:932-1043`; `main.py:1129-1156` |
| A dedup replacement keeps the `surface_id`, rotates the nonce and never re-pings Telegram | `service.py:418-488,598-600` |
| `ActionContext` carries no actor (the actor is only written to the audit row) | `actions.py:35-40` |
| `DAGStore.create` refuses when 5 DAGs are `pending`/`running` | `nous/dag/store.py:60-71` |
| The orchestrator is built before `SurfaceService`; `ActionRouter` already holds the orchestrator | `nous/main.py:990,1076,1110-1119` |

## 3. Design

### 3.1 The node spec

A new node type `approval` in `DAGNodeType`, authorable through `dag_create`:

| Field | Meaning |
|---|---|
| `instructions` (required, non-empty) | The question put to the human. |
| `description` | Card title; defaults to the node name. |
| `options` (required) | 2–4 of `{id, label, outcome}`: `id` `^[a-z0-9_-]{1,40}$`, unique; `label` 1–80 chars; `outcome` `proceed` or `stop`. |
| `default_option` (required) | An option `id`: what happens when nobody answers by the deadline. |
| `recommended_option` | An option `id` highlighted on the card; defaults to `default_option`. |
| `answer_timeout_seconds` | Time allowed for an answer. Default `NOUS_DAG_APPROVAL_DEFAULT_WAIT_SECONDS` (86 400); clamped at insert to `NOUS_DAG_APPROVAL_MAX_WAIT_SECONDS` (604 800); `ge=60`. |

Validation (`DAGCreateRequest`): the four approval fields are required/allowed only on `approval`
nodes; `completion_check*`, `parent_node`, `fix_actions` are rejected on them; a fix node may not
name an approval node as its `parent_node` (a human "no" is an answer, not a failure to repair);
`tools`, `frame_type`, `model`, `timeout_seconds`, `stall_timeout_seconds` are ignored, as on
other non-check types.

### 3.2 Schema — migration `076_dag_approval_nodes.sql`

- `chk_dag_node_type` gains `'approval'`; `chk_dag_node_status` gains `'awaiting_input'`
  (drop + re-add, the 048 pattern).
- New nullable columns on `nous_system.dag_nodes`:
  `approval_spec JSONB` (the §3.1 fields as authored), `surface_id TEXT`,
  `awaiting_since TIMESTAMPTZ`, `answer_deadline TIMESTAMPTZ`, `answer TEXT`,
  `answered_by TEXT`, `answered_at TIMESTAMPTZ`,
  `answer_source TEXT CHECK (answer_source IN ('human', 'deadline'))`.
- `CREATE INDEX idx_dag_nodes_surface ON nous_system.dag_nodes (surface_id) WHERE surface_id IS NOT NULL`
  — the answer path looks a node up by its card.
- The ORM mirrors all of it (constraints + index, SQLite `sqlite_where` as in 075).

### 3.3 Launch: `_launch_approval_node`

`_launch_node` gains an `approval` branch. Inside the tick (`_lock` held):

1. Build the card with the existing `approval_gate` builder: `title` = description or node name
   (prefixed with the DAG name); `summary` = `_build_predecessor_context(node, dag)` — the question
   plus the results of `context_flow` predecessors, so the human sees what they are approving
   (e.g. the drafted email) — truncated to 4 000 chars; `options` = `[{id, label}]`;
   `recommendation` = `recommended_option`; `expires_hours` = the node's wait +
   `NOUS_DAG_APPROVAL_CARD_GRACE_SECONDS` (3 600) — a backstop only (§3.6).
2. `SurfaceService.push_built(..., dedup_key=f"dag-approval:{node.id}", notify=True)` → the card id.
   Priority-2 `approval_gate` cards ping Telegram with a deep link.
3. One node update: `status='awaiting_input'`, `surface_id`, `awaiting_since=now`,
   `answer_deadline=now + wait`, `started_at=now`.

A crash between 2 and 3 leaves the node `pending`/`ready`; the next tick relaunches and the same
`dedup_key` **replaces** the live card (same id, rotated nonce, no second Telegram ping) instead of
stacking a second one. Push failures: a censor refusal (`PermissionError`) or no `SurfaceService`
wired → the node fails with a plain error; any other exception → `_defer_node` (bounded by the
existing `_MAX_DEFERRALS`).

The orchestrator receives the service through `set_surface_service(...)`, called in `main.py` once
both exist.

### 3.4 The answer is the transition

`DAGOrchestrator.record_answer(surface_id, option_id, actor) -> AnswerOutcome`
(`recorded` | `closed` | `not_linked`):

- Looks up the node by `surface_id`. No node → `not_linked` (an agent-pushed `approval_gate`:
  today's behavior, unchanged).
- Validates `option_id` against the node's own `approval_spec.options`.
- **One conditional UPDATE** writes the answer AND the terminal status together:
  `SET answer, answered_by=actor, answered_at=now, answer_source='human', completed_at=now,`
  `status = 'completed'` (outcome `proceed`, `result = "Answered '<label>' (<id>) by <actor>"`)
  `or 'failed'` (outcome `stop`, `error = "declined: '<label>' (<id>) by <actor>"`)
  `WHERE id = :node AND status = 'awaiting_input' AND answered_at IS NULL`.
  `rowcount == 1` → `recorded`; `0` → `closed` (already answered, defaulted, or cancelled).

No orchestrator lock is taken (like `retry_node` and `cancel_dag`): the row's own predicate is the
guard, and it holds across processes, which the in-process surface locks do not. There is no
"answered but still waiting" state to recover from.

`ActionRouter`'s `approval.choose` handler calls `record_answer` first when the orchestrator is
wired: `recorded` → today's result, with `/summary` patched to `Decided: <label> — the DAG
resumes.`; `closed` → the handler closes the stale card itself (`resolve(..., status='expired')`,
best effort) and returns `ok=False`, "this decision is no longer open (answered, defaulted at its
deadline, or cancelled)" — audited `rejected`; `not_linked` → today's generic behavior. `ActionContext` gains `actor` (already computed
by the router for the audit row). `approval.defer` is unchanged: the card stays live, the deadline
still applies.

Resumption needs nothing else: the next tick sees a `completed` node (dependents become ready; the
answer is the node's `result`, so `context_flow` successors see it) or a `failed` one
(`_propagate_failures` blocks its dependents; unrelated branches finish; the DAG ends `failed` and
F087 delivers that outcome).

### 3.5 The deadline

A new tick step `_poll_awaiting_input(dag)` (after `_sync_node_statuses`): for each
`awaiting_input` node whose `answer_deadline <= now`, the same conditional UPDATE with
`default_option`, `answer_source='deadline'`, `answered_by='system:deadline'`; on `rowcount == 1`
the card is closed (`SurfaceService.resolve(surface_id, status='expired')`, best effort). A tap
that lands first wins; a tap that lands after gets `closed`. Precision is one tick (30 s).

### 3.6 The card follows the node

The node, not the card, is the source of truth. The card is closed where the node leaves
`awaiting_input` — by the handler (`recorded`), by the deadline step, and by `_cancel_node`'s new
`approval` branch (`resolve(..., status='expired')`) — each best effort. If a close is lost to a
crash, the card's own expiry (deadline + grace) retires it through `expire_sweep`, and any tap on
it before then is `closed`. The card's expiry can never cut an answer short: it is always later
than the node's deadline, which the orchestrator enforces itself.

### 3.7 Interaction with existing paths

| Path | Behavior |
|---|---|
| `cancel_dag` | `_cancel_node` closes the card; the node is marked `cancelled` as today. A tap after that is `closed`. |
| `retry_node` (on a `failed` approval node — a *stop* answer or a launch failure) | The reset clears `surface_id`, `awaiting_since`, `answer_deadline`, `answer`, `answered_by`, `answered_at`, `answer_source`; the relaunch asks again with a fresh card (the old one is no longer live, so the dedup key creates a new one). `_account_before_retry` is a no-op (no subtask). |
| Fix stage | Cannot attach (validator). |
| Reaper, stall detection, `_sync_node_statuses` | Act on `running` only; `awaiting_input` is outside them by construction. |
| F064.2 frame caps | `approval` is not subtask-backed, so it is cap-exempt like `check`. |
| Token budget | An approval node spends no tokens; `_handle_budget_exceeded` (dark) does not touch `awaiting_input`. |
| `_check_dag_completion` | `awaiting_input` is non-terminal, so the DAG waits. |
| F087 delivery | Unchanged: the terminal outcome is announced as today. |
| `dag_manage status` | Shows `awaiting_input` nodes with their deadline and card link, and answered nodes' answer and source. |

### 3.8 Admission: a parked DAG does not count

`DAGStore.create`'s active count excludes a DAG that has at least one `awaiting_input` node and no
node in `ready`, `running` or `awaiting_check` — it is doing no work, only waiting for a person.
`get_active_dags` still returns it (the tick must apply its deadline). The number of parked DAGs is
bounded by the deadline ceiling (7 days), not by a slot count.

### 3.9 Landing dark

- `NOUS_DAG_APPROVAL_NODES_ENABLED` (default `false`) gates CREATION only: with it off,
  `dag_create` rejects `type="approval"` naming the flag, and the tool schema does not advertise the
  type or its fields. With it on but the companion off (`NOUS_A2UI_ENABLED=false`) or no
  `SurfaceService` wired, `dag_create` refuses approval nodes (the `clock_wired` pattern).
- Nodes already waiting when the flag is turned off still answer and default normally.
- New settings: `NOUS_DAG_APPROVAL_DEFAULT_WAIT_SECONDS` (86 400),
  `NOUS_DAG_APPROVAL_MAX_WAIT_SECONDS` (604 800), `NOUS_DAG_APPROVAL_CARD_GRACE_SECONDS` (3 600).

## 4. Lifetimes and invariants (written first, per F087)

Three lifetimes interact: the **node attempt** (`pending → awaiting_input → completed | failed |
cancelled`; `retry_node` starts a new attempt), the **card** (`live → resolved | expired`, or
replaced in place by the same dedup key), and the **answer** (set at most once per attempt).

- **I1 — one answer per attempt.** Every write that ends `awaiting_input` is conditional on
  `status='awaiting_input' AND answered_at IS NULL`. The tap, the deadline and a concurrent tap
  race on one row; exactly one wins.
- **I2 — a card answers only the attempt it was shown for.** The node is found by `surface_id` and
  must be `awaiting_input`. A relaunch while the old card is live replaces it in place and rotates
  its nonce, so a tap from the stale render fails `NONCE_MISMATCH`; after a retry the old card is
  closed and the new attempt gets a new card.
- **I3 — the deadline is the orchestrator's.** The card's expiry is always later than the node's
  deadline; the card never decides.
- **I4 — a parked DAG never blocks admission.**
- **I5 — creation is gated; resolution is not.** The flag never strands a waiting node.

## 5. Crash windows

| Crash between | State left | Recovery |
|---|---|---|
| card push and node update | node `pending`/`ready`, live card | next tick relaunches; same dedup key replaces the card |
| node answered and card closed | node terminal, live card | card closed by its backstop expiry; taps before that are `closed` |
| deadline answer and card close | same | same |
| `cancel_dag`'s node writes and card close | node `cancelled`, live card | same |
| tick loads the DAG and a tap lands mid-tick | the tick's in-memory node is stale (`awaiting_input`) | its own deadline write is conditional and matches nothing; the next tick sees the terminal status |

## 6. Testing

- Validator: options count/ids/outcomes, `default_option`/`recommended_option` membership, fields
  rejected on the wrong type, fix-on-approval rejected, flag off → rejected.
- Store: the conditional answer UPDATE (`recorded` once, then `closed`); tap vs deadline vs tap;
  the admission count with parked, working and mixed DAGs.
- Orchestrator with a real `SurfaceService` on the SQLite test DB: launch pushes one card and parks
  the node; relaunch after a simulated crash replaces the card (same id, one Telegram ping);
  deadline applies the default and closes the card; `stop` fails the node and blocks dependents
  while a parallel branch completes; `cancel_dag` closes the card; `retry_node` asks again with a
  new card.
- End to end through `ActionRouter` (POST `/a2ui/action` shape): tap → node `completed`, next tick
  launches the successor with the answer in its context; late tap after the deadline → 422,
  audited `rejected`; a tap on an agent-pushed `approval_gate` behaves exactly as today.

## 7. Out of scope

- A read-back tool for agent-pushed `approval_gate` choices (the roadmap's "nothing reads the choice
  back" outside DAGs).
- Per-option branching (different successors per answer) — `proceed`/`stop` plus successors that
  read the answer cover the first use.
- Telegram inline answer buttons; more than one approver; widening budgets through an approval
  (P1.6).
- Dashboard styling of `awaiting_input` (it renders as an unknown status until then).
