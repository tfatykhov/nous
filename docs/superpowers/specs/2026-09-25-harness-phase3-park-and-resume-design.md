# Harness Phase 3 — Park-and-Resume: a DAG node waits durably on a human answer (design, v2)

**Roadmap:** `docs/superpowers/plans/2026-09-24-harness-autonomy-roadmap.md` §2 row P0.3, §3 phase 3
("spec + review first; land dark"; the mechanism was deliberately left undecided there).
**Anchors:** `main` `1daa004` (after harness 2c #646, 2a #647, 2b #648).
**Decisions taken with the user (2026-09-25):** mechanism C (new node type + new status); a *stop*
answer fails the node and blocks only its dependents; a parked DAG does not count against
`MAX_ACTIVE_DAGS`.

**v2 (2026-09-25)** folds three reviews of v1 (architect, database/concurrency, devil's advocate;
all APPROVE WITH REVISIONS). What changed, by the finding that forced it:

| Finding | v2 answer |
|---|---|
| A *stop* answer wedges any DAG whose successor is linked only by `context_flow`: failure propagation follows `dependency` only, readiness follows both (architect 1, devil 1) | One predecessor-edge set for readiness, propagation and retry (§3.8) — a pre-existing bug this feature makes the main path |
| A tap between the card push and the node write is recorded nowhere, and the relaunch sends a second card and ping (architect 2, devil 2, db 5) | Park first, then push, then link; a tap finds its node by the card's dedup key, never by a stored id (§3.4, §3.5) |
| Blind status writes (`cancel_dag`, `cancel_cascade`, the launch write) can overwrite an answer, or resurrect a cancelled node inside a dead DAG (architect 3, db 1–2, devil 16) | Every exit from `awaiting_input` goes through one conditional store transition, and so does the launch write (§3.3) |
| The budget path strands a waiting node (architect 4, db 3, devil 13) | It cancels `awaiting_input` like `awaiting_check` and closes the card (§3.9) |
| A downstream unblock relaunches a node with its old answer still set, so neither a tap nor the deadline can ever match (architect 5, db 4) | The park write is the single reset point for an attempt (§3.4) |
| A *proceed* default is an unapproved action (devil 3) | v1 requires the default to be a *stop* option (§3.1) — **needs the user's confirmation, §9** |
| The card can lose the question, never states the deadline, overwrites the draft on "Ask me later", recommends `options[0]` by default, says "the DAG resumes" on a *stop* (architect 7, devil 4) | Card contract §3.4 |
| A human "no" reads as a failure, and the agent can simply re-ask it (devil 5) | Declined answers are rendered as answers, and only the companion can retry them (§3.10, §3.12) |
| No bound on parked DAGs; answered parked DAGs all resume past the admission cap (architect 6, db 9, devil 14) | A separate parked cap (§3.11) — **needs the user's confirmation, §9** |
| The deadline compared in Python raises `TypeError` on SQLite's naive timestamps and aborts the DAG's tick (db 8) | The deadline is a predicate in the conditional write (§3.6) |
| A card from an earlier attempt can answer the next one (db 5, 16) | The previous attempt's live card is retired before the new attempt parks (§3.4 step 0) |
| CI applies migrations with `psql`, prod with its own statement splitter — one stray comment passes CI and breaks prod boot (db 12) | A splitter test for 076 (§3.2) |
| Interface, wiring, reserved key, censor, leaked cards, lock order, audit (architect 8–18, db 6–7, 10–21, devil 6–12, 15, 17–19) | §3.3–§3.14, §6 |

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
| The wall-clock reaper and stall detection act on `status='running'` only | `orchestrator.py:1540-1609,1809-1857` |
| Readiness counts `dependency` + `context_flow` predecessors; failure propagation blocks along `dependency` only; `retry_node`'s unblock walks `dependency` + `cancel_cascade` | `orchestrator.py:2377-2380`, `:1896-1900`, `:616-625` |
| `cancel_dag` and the `cancel_cascade` branch of `_propagate_failures` write `cancelled` unconditionally; `DAGStore.update_node` has no status predicate and returns nothing | `orchestrator.py:531-536,1936-1942`; `store.py:370-387` |
| `_handle_budget_exceeded` cancels `pending`/`ready`/`awaiting_check` only | `orchestrator.py:2709-2739` |
| `_defer_node` returns a node to `pending` | `orchestrator.py:199-224` |
| The F087 template lists every `failed`/`blocked`/`cancelled` node under "Problems:" | `nous/dag/delivery.py:159-206` |
| `approval_gate` (`push_surface`) takes `title`, `options=[{id,label}]`, `summary`, `risk`, `recommendation` (falls back to `options[0]`), `trace_id`, `expires_hours` (float hours) | `nous/a2ui/builders/approval.py:26-95` |
| `approval.choose` validates the option against the surface's server-side options, patches `/summary`, resolves — and records the choice **nowhere readable** (only `a2ui_actions.context->>'optionId'`); `approval.defer` also overwrites `/summary` | `nous/a2ui/actions.py:478-500` |
| The action gate runs, in order: rate limit, allowlist, shape, nonce, handler lookup, then an action-time censor over `surface.title + name + context` for mutating handlers — all inside the per-surface lock | `actions.py:226-350` |
| The router applies `data_patches` and `resolve_surface` whatever `ok` is; `ActionResult` has no resolve status; `ActionContext` has no actor | `actions.py:34-47,355-376` |
| `SurfaceService` locks are in-process; `resolve()` takes no lock and is a read-modify-write; dedup matches live cards only; a dedup replacement keeps the `surface_id`, rotates the nonce and never re-pings Telegram | `service.py:120,159-174,258,400,418-488,598-600,848-887` |
| `push_surface` and `compose_surface` accept any agent-supplied `dedup_key` | `nous/a2ui/tools.py:80,186,389` |
| `DAGStore.create` refuses when 5 DAGs are `pending`/`running` | `nous/dag/store.py:60-71` |
| `SurfaceService` is built after the orchestrator and only when `a2ui_enabled`; `ActionRouter` already holds the orchestrator | `nous/main.py:990,1069-1076,1110-1119` |
| `dag_create` builds each `DAGNodeSpec` from a fixed dict (new fields must be threaded explicitly — the F066.1 silent-drop lesson) | `nous/api/tools.py:4846-4883` |

## 3. Design

### 3.1 The node spec and its validation

A new node type `approval` in `DAGNodeType`, authorable through `dag_create`:

| Field | Meaning |
|---|---|
| `instructions` (required, non-empty) | The question put to the human. |
| `description` | Card title; defaults to the node name. |
| `options` (required) | 2–4 of `{id, label, outcome}`: `id` `^[a-z0-9_-]{1,40}$`, unique; `label` 1–80 chars; `outcome` `proceed` or `stop`. At least one of each outcome. |
| `default_option` (required) | An option `id` whose outcome is **`stop`** (v1): what happens when nobody answers by the deadline. |
| `recommended_option` | An option `id` highlighted on the card. Default: none — the card recommends nothing unless the author says so. |
| `answer_timeout_seconds` | Time allowed for an answer. Default `NOUS_DAG_APPROVAL_DEFAULT_WAIT_SECONDS` (86 400); `ge=900`; clamped at insert to `NOUS_DAG_APPROVAL_MAX_WAIT_SECONDS` (604 800). |

`DAGNodeSpec` / `DAGCreateRequest` validation:

- The four approval fields are allowed only on `approval` nodes, and `options` + `default_option`
  are required there.
- Rejected on `approval` nodes (explicitly, not ignored): `completion_check*`, `parent_node`,
  `fix_actions`, `tools`, `frame_type`, `model`, `timeout_seconds`, `stall_timeout_seconds`.
- A fix node may not name an approval node as its `parent_node` (a human "no" is an answer, not a
  failure to repair).
- An approval node must have at least one outgoing `dependency` or `context_flow` edge — an
  approval that gates nothing is a mistake.
- The feature flag is checked in the `dag_create` handler against the wired `Settings`, not in the
  validator (`schemas.py:233-241` builds a fresh `Settings()` from the environment).
- `DAGStore.create` skips timeout and stall resolution for approval nodes and stores them `NULL`
  (they never run; `store.py:110-135` would otherwise validate a stall timeout against a timeout
  that does not apply).

**Why the default must be *stop* in v1.** An unanswered card is the common case — the person is
asleep, the ping was missed, the link was not tappable. With a *proceed* default, silence becomes
an approval, and the action the card exists to guard runs with nobody having seen it. With a
*stop* default the worst case is a stopped DAG that the person can retry from the companion. A
*proceed* default is a one-line validator change once there is a use for it (§8).

### 3.2 Schema — migration `076_dag_approval_nodes.sql`

- `chk_dag_node_type` gains `'approval'`; `chk_dag_node_status` gains `'awaiting_input'`. Drop both
  possible names first (`dag_nodes_node_type_check` / `chk_dag_node_type`, `dag_nodes_status_check`
  / `chk_dag_node_status`), then re-add — the 048 pattern, because 032 created the constraints
  inline and Postgres named them itself. The full lists: status `'pending','ready','running',
  'awaiting_check','awaiting_input','completed','failed','blocked','cancelled','skipped'`; type
  `'subtask','check','gate','callback','fix','approval'`. `awaiting_input` fits `VARCHAR(20)`.
- Form: `ADD COLUMN IF NOT EXISTS` throughout; the `answer_source` CHECK is named inline
  (`CONSTRAINT chk_dag_node_answer_source CHECK (…)`) so it never needs the two-name dance; no
  `BEGIN`/`COMMIT` (the migrator wraps pending migrations in one transaction); full-line `--`
  comments only and no `;` inside them (071's header rule). CI applies migrations with `psql`
  while prod boots through `_split_sql_statements`, so a stray inline comment can pass CI and break
  prod boot — `tests/test_migrator_split.py` gains `test_split_full_migration_076` asserting the
  exact statement count (the 034 pattern).
- New nullable columns on `nous_system.dag_nodes`:

  | Column | Holds |
  |---|---|
  | `approval_spec JSONB` | The §3.1 fields as authored. Immutable after insert. |
  | `answer_deadline TIMESTAMPTZ` | Written at park. |
  | `surface_id TEXT` | The card's id once linked; display and a consistency check only (§3.5). |
  | `answer TEXT` | The chosen option `id`. |
  | `answered_by TEXT` | The actor as the router recorded it, or `system:deadline`. |
  | `answered_at TIMESTAMPTZ` | |
  | `answer_source TEXT CHECK (answer_source IN ('human', 'deadline'))` | |
  | `answer_history JSONB` | Earlier attempts' answers, archived at each relaunch (§3.4). |

  The park time is `started_at`; v1's `awaiting_since` is dropped as a duplicate. No new index: a
  tap resolves its node by primary key (§3.5), and the tick already loads every active DAG's nodes.
- The ORM mirrors all of it (both CHECK lists, the named `answer_source` CHECK). SQLite enforces
  the ORM's CHECKs, so a drift between migration and ORM fails the SQLite tests.
- **Rollback:** pre-076 code treats `awaiting_input` as non-terminal forever and counts it toward
  `MAX_ACTIVE_DAGS`, so cancel every DAG with an approval node before rolling back.

### 3.3 One conditional transition

`DAGStore.transition_node(node_id, *, from_statuses, require_live_dag=False, card=None, **values) -> bool`:
one `UPDATE … WHERE id = :node AND status IN (:from_statuses)` — agent-scoped through the
`execution_dags` subselect as `claim_and_add_node_tokens` is (`store.py:509-534`); with
`require_live_dag` also `AND dag_id IN (SELECT id … WHERE status IN ('pending','running'))`; with
`card` also `AND (surface_id IS NULL OR surface_id = :card)`. Returns `rowcount == 1`.

Every write that leaves `awaiting_input` uses it with `from_statuses={'awaiting_input'}`: the tap,
the deadline, `cancel_dag`, the `cancel_cascade` branch, the budget path, a failed card push and
the leaked-card sweep. The answer and the terminal status are written in the same statement, so
there is no "answered but still waiting" state, and the row's own predicate — not a lock — decides
the race between a tap, the deadline and a second tap. It holds across processes, which the
in-process surface locks do not.

Two existing blind writes become conditional too, because they can race a park or an answer:
`cancel_dag` and the `cancel_cascade` branch write `cancelled` with
`from_statuses = non-terminal statuses`. An approval node they win against has its card closed
(§3.7).

### 3.4 Launch: park, push, link

`_launch_node` gains an `approval` branch (today an unhandled type silently does nothing — it has
no `else`). `_launch_approval_node`, inside the tick with `_lock` held:

0. **Retire the previous attempt's card.** A card left live by an earlier attempt (its close lost
   to a crash, or never linked) carries the same dedup key and a valid nonce, so between this
   attempt's park and its push it could answer the new attempt with the old content.
   `SurfaceService.close_by_dedup_key(approval_dedup_key(node.id), 'expired')` closes it under its
   surface lock first; a tap already in flight on it finds the node not yet parked and gets
   `not_open` (§3.5). On a first attempt this finds nothing.
1. **Park** — `transition_node(from_statuses={'pending','ready'}, require_live_dag=True)` writing
   `status='awaiting_input'`, `started_at=now`, `answer_deadline=now + wait`, and `NULL` for
   `surface_id`, `answer`, `answered_by`, `answered_at`, `answer_source`, `result`, `error`,
   `completed_at`. If the node carried an answer from an earlier attempt, the same write appends
   it to `answer_history` (computed from the in-memory node — safe, because nothing answers a
   node that is not `awaiting_input`). **This write is the single reset point for an attempt**,
   whichever path (`retry_node`, the downstream unblock, a relaunch) made the node pending. If it
   returns `False`, someone else moved the node; stop.
2. **Push** — build the card (below) and call
   `SurfaceService.push_built(built, dedup_key=approval_dedup_key(node.id), notify=True)`.
   - A censor refusal (`PermissionError`) or a build/validation error → `transition_node` to
     `failed` with `error="approval card refused by censor: …"` or `"approval card could not be
     built: …"`. These do not heal on retry, so they fail at once.
   - Any other exception, or no `SurfaceService` wired → leave the node parked with
     `surface_id NULL` and the reason in `error`; `_poll_awaiting_input` pushes again each tick
     until the deadline, which bounds the retries (no `_MAX_DEFERRALS` needed).
3. **Link** — `transition_node(from_statuses={'awaiting_input'}, surface_id=sid, error=None)`. If
   it returns `False` the node left `awaiting_input` between 1 and 3 (cancelled, or answered by a
   tap that already found it); close the card just pushed unless an answer resolved it.

The card (`approval_gate` builder, with two small builder changes: a `recommend_first=False` switch
that disables the `options[0]` fallback, and `outcome` kept in the server-side options data):

| Slot | Content |
|---|---|
| `title` | `<DAG name> · <description or node name>` |
| `summary` | The question first, then each `context_flow` predecessor's result, each cut separately with a visible `[truncated, N chars]` marker, 4 000 chars in total. The question is never cut. |
| `risk` | `If nobody answers by <deadline, UTC>, '<default label>' applies.` |
| `options` | `[{id, label: "<label> — continues" \| "<label> — stops here", outcome}]` |
| `recommendation` | `recommended_option`, or none |
| `expires_hours` | `(wait + NOUS_DAG_APPROVAL_CARD_GRACE_SECONDS) / 3600` — a backstop only; the orchestrator owns the deadline (I3) |

Priority-2 `approval_gate` cards ping Telegram once, with a deep link. The link is tappable only
when `NOUS_A2UI_PUBLIC_BASE_URL` is set; `dag_create` says so in its result when it is not (a
*stop* default makes an unreachable ping safe, not useful).

### 3.5 The answer is the transition

One primitive for the tap and the deadline:

```python
@dataclass(frozen=True)
class AnswerResult:
    outcome: Literal["recorded", "closed", "not_open", "not_linked", "invalid_option"]
    node_id: UUID | None
    dag_id: UUID | None
    option_label: str | None      # the chosen option (recorded) or the recorded answer (closed)
    option_outcome: Literal["proceed", "stop"] | None
    node_status: str | None       # the node's status after the call
    answer_source: str | None
    answered_by: str | None
    answered_at: datetime | None

async def answer_node(self, node_id, option_id, *, source, actor, surface_id=None) -> AnswerResult
```

- Loads the node (agent-scoped). Missing, or not an `approval` node → `not_linked`. `option_id`
  not in its `approval_spec.options` → `invalid_option`.
- One `transition_node(from_statuses={'awaiting_input'}, require_live_dag=True, card=surface_id)`
  — the `card` predicate means a card can only answer the attempt it belongs to — writing
  `answer`, `answered_by`, `answered_at=now`,
  `answer_source`, `completed_at=now`, `surface_id` (a tap that lands before the link step links
  it), and:
  - *proceed*: `status='completed'`, `result="Human answer: '<label>' (<id>) at <UTC>[ by <actor>]"`;
  - *stop*, human: `status='failed'`, `error="declined: '<label>' (<id>) at <UTC>[ by <actor>]"`;
  - *stop*, deadline: `status='failed'`, `error="no answer by <deadline UTC>; default '<label>' (<id>) applied"`.

  `[ by <actor>]` is omitted when the actor is `unattributed` (the router's value unless
  `NOUS_A2UI_TRUST_FORWARDED_IDENTITY` is on); the column still stores it. Successor prompts see
  the `result`, never "by unattributed".
- `True` → `recorded`. `False` → re-read the node: `pending`/`ready` → `not_open` (this attempt has
  not parked yet, so the card is from an earlier one); otherwise `closed`, with what actually
  happened (answered, defaulted or cancelled, by whom, when).
- The lookup and the write are agent-scoped and require a live DAG, so no path tells a person "the
  DAG continues" into a DAG that has already ended.
- It takes no orchestrator lock. Lock order everywhere is orchestrator `_lock` → surface lock,
  never the reverse; the tap path holds only the surface lock.

**How a tap finds its node.** Every DAG card carries the dedup key `dag-approval:<node_id>`
(one leaf module, `nous/dag/approval.py`, owns the prefix and both directions of the mapping). The
`approval.choose` handler parses the node id from `ctx.surface.dedup_key` — so a tap is resolved
even in the window before the link step, and a DAG card never falls into the generic path.
`ActionContext` gains `actor` (the router already computes it); `ActionResult` gains
`resolve_status: str = "resolved"` so a handler can retire its card as `expired` without calling
`resolve` inside the surface lock it already holds.

| `answer_node` returns | Handler result |
|---|---|
| `recorded`, *proceed* | `ok`, resolve; patch `/risk` to `Decided: '<label>' — the DAG continues.` |
| `recorded`, *stop* | `ok`, resolve; patch `/risk` to `Decided: '<label>' — this step stops and the steps after it will not run.` |
| `closed` | `ok=False`, resolve as `expired`, a specific message: `already answered '<label>' at <time>`, `no answer by the deadline — '<label>' was applied at <time>`, or `this DAG step was cancelled`. Audited `rejected`. |
| `not_open` | `ok=False`, resolve as `expired`, `this question is being asked again — answer the new card`. |
| `invalid_option` | `ok=False`, as today. |
| `not_linked` (node gone) | `ok=False`, resolve as `expired`, `this DAG step no longer exists`. |
| orchestrator not wired | `ok=False`, card stays live: `DAG orchestration is not running; the answer cannot be recorded now`. |

`/summary` is never patched on a DAG card: it holds what the person is approving. `approval.defer`
on a DAG card patches `/risk` to `Deferred. If nobody answers by <deadline>, '<default label>'
applies.` Agent-pushed `approval_gate` cards keep today's handlers unchanged.

**Censor.** The action-time censor stays in force for DAG cards, with one exception: a *stop*
choice (outcome read from the card's server-side options) skips it. Refusing to stop can only let
the guarded action run.

**Resumption** needs nothing else: the next tick sees a `completed` node (dependents become ready;
the answer is the node's `result`, which `context_flow` successors receive) or a `failed` one
(`_propagate_failures` blocks every successor along the predecessor edges, §3.8; unrelated
branches finish; F087 delivers the outcome, §3.12).

### 3.6 The deadline

A new tick step `_poll_awaiting_input(dag)`, after `_sync_node_statuses` and before the budget and
failure steps, for each `awaiting_input` node:

- `answer_deadline <= now` → `answer_node(node.id, default_option, source='deadline',
  actor='system:deadline')`; on `recorded`, close the card and update the in-memory node so this
  tick's propagation sees it. The comparison that decides is a predicate in the conditional write
  (`AND answer_deadline <= :now`, with `:now` an aware UTC value computed in Python), as
  `expire_sweep` revalidates `expires_at` inside its claim. SQLite returns the stored timestamp
  naive, so a Python comparison against an aware `now` raises `TypeError` and aborts the DAG's
  whole tick. The in-memory pre-filter normalizes the timezone, as the five existing sites do
  (`orchestrator.py:1142,1182,1614,1832,2134`).
- else, `surface_id IS NULL` → push again (§3.4 steps 2–3).

A tap that lands first wins; a tap after gets `closed`. The deadline fires within one heartbeat
loop iteration of the time (the tick interval plus the tick's own duration).

### 3.7 The card follows the node

The node, not the card, is the source of truth. Closing a card is best effort and goes through a
new `SurfaceService.close(surface_id, status)` (and `close_by_dedup_key`) that takes the surface
lock (`resolve` does not) and treats a card that is no longer live, or already deleted by
retention (`resolve` raises `KeyError`, `service.py:855`), as closed.
The card is closed wherever the node leaves `awaiting_input`: by the router after a tap, by the
deadline step, and by `_finish_approval(node, **values)` — the helper that `cancel_dag`,
`cancel_cascade` and the budget path use (transition, then close on success).

**Leaked-card sweep.** A close lost to a crash would otherwise leave an answerable-looking card up
to a week (the backstop expiry), and that expiry would then write a false `no_objection`. A tick
step `_sweep_leaked_approval_cards()`, modelled on `_sweep_leaked_heartbeat_checks`
(`orchestrator.py:276-310`), reads live cards whose dedup key has the `dag-approval:` prefix (a
bounded batch), loads their nodes, and:

- node missing, or not `awaiting_input` → close the card `expired`;
- node `awaiting_input` but its DAG terminal → `transition_node` to `cancelled`
  (`error="DAG ended while waiting"`) and close the card;
- node `awaiting_input` with a different `surface_id` → close the stray card.

A second bounded query cancels `awaiting_input` nodes inside a terminal DAG that have no live card.

### 3.8 One predecessor-edge set (fixes a pre-existing bug)

Readiness treats `context_flow` as a predecessor; failure propagation and `retry_node`'s unblock do
not. So a failed node whose only successor is linked by `context_flow` leaves that successor
`pending` forever, the DAG `running` forever, and one `MAX_ACTIVE_DAGS` slot gone. It predates this
feature, but an approval linked to its successor by `context_flow` — the natural way to hand the
answer on — is exactly that shape.

One constant, `_PREDECESSOR_EDGES = ("dependency", "context_flow")`, used by `_find_ready_nodes`,
by `_propagate_failures`'s blocking walk, and by `retry_node`'s `dep_map` and reachability (which
keep `cancel_cascade` as today). A regression test: a *stop* answer with a `context_flow`-only
successor ends the DAG `failed`, and retrying the approval unblocks the successor.

### 3.9 Interaction with existing paths

| Path | Behavior |
|---|---|
| `cancel_dag` | Conditional `cancelled` write per non-terminal node; an approval node it wins against has its card closed. |
| `_propagate_failures` | Blocks along §3.8's edges. A `cancel_cascade` target that is `awaiting_input` goes through `_finish_approval`. |
| `retry_node` | Resets as today; the park write clears the answer columns and archives the previous answer (§3.4). A declined approval is refused unless the caller is the companion (§3.10). |
| Fix stage | Cannot attach (validator). |
| `_handle_budget_exceeded` | Cancels `awaiting_input` like `awaiting_check`, via `_finish_approval`. |
| Reaper, stall detection, `_sync_node_statuses` | Act on `running` only. |
| F064.2 frame caps | `approval` is not subtask-backed, so it is cap-exempt like `check`. |
| `_check_dag_completion` | `awaiting_input` is non-terminal, so the DAG waits. |
| `_recover_stale_ready_nodes` | Unchanged; with park-first an approval node is never left `ready` across a crash for long. |
| Harness 2a policy | An approval does not widen `dag_node`'s tool policy: an approved successor still cannot call an irreversible tool under `enforce`. |
| Harness 2b keys | The scope `dag:{dag_id}:{node_name}` holds across stop, retry and proceed, because the successor never ran. |

### 3.10 A human "no" stays a "no"

`retry_node(dag_id, node_name, *, allow_declined=False)` refuses an approval node whose
`answer_source` is `human` and whose status is `failed`: "'<node>' was declined by a person; only
they can re-ask it (Retry in the companion)". The agent's `dag_manage retry` (`tools.py:5052`)
passes the default; the companion's `dag.retry` handler (`actions.py:668`) passes `True`. A node
that stopped at its deadline can be retried by the agent — nobody said no; the question is asked
again.

### 3.11 Admission: parked DAGs have their own cap

A DAG is **parked** when it has an `awaiting_input` node and no work: no node in `ready`, `running`
or `awaiting_check`, and no `pending` non-fix node whose predecessors (§3.8's edges) are all
`completed` or `skipped` — that is a sibling about to launch or one deferred by a frame cap or a
full pool (`_defer_node` returns it to `pending`), and it is work.

`DAGStore.create`, in the transaction it already counts in:

- refuses when non-parked `pending`/`running` DAGs ≥ `MAX_ACTIVE_DAGS` (5) — parked DAGs do not
  count, as decided;
- refuses when parked DAGs ≥ `NOUS_DAG_MAX_PARKED_DAGS` (default 20): "20 DAGs are waiting on your
  answers; answer or cancel some first". Without it a looping agent could create any number of DAGs
  that park at once — each a priority-2 card, a Telegram ping and a larger tick.

The predicate is defined once in the store (two correlated `EXISTS` over the existing
`idx_dag_nodes_status (dag_id, status)` index, plus the edge check) and used by both `create` and
`count_active`, so they cannot drift. Resuming is not admission-controlled: an answered parked DAG
starts working again whatever the active count is, which is what the parked cap bounds. The count
and the insert stay non-atomic, as today.

`get_active_dags` still returns parked DAGs (the tick applies their deadlines). The dashboard's
active count gains a separate parked count.

### 3.12 What the person and the agent see afterwards

- **F087 template:** a new `Approvals:` section lists every approval node — `'<label>'` with its
  source and time, `no answer by <time>; default '<label>' applied`, or `not answered (cancelled)`.
  Answered approval nodes are left out of `Problems:`, and a DAG whose only failures are *stop*
  answers and the nodes they blocked is announced as `stopped at an approval` rather than `FAILED`.
- **`dag_manage`:** `list` marks DAGs waiting on a person; `status` gains an `awaiting_input` icon
  and shows the deadline, the default and the card link, and each answered node's answer and
  source.

### 3.13 Wiring and landing dark

- `SurfaceService` is built above the DAG block in `main.py` (it needs only the database, settings
  and heart) and passed to the `DAGOrchestrator` constructor. `DAGOrchestrator.approvals_wired` is
  true when a service is present — the `clock_wired` pattern.
- `NOUS_DAG_APPROVAL_NODES_ENABLED` (default `false`) gates **creation** only. `dag_create` checks
  it, and `approvals_wired`, at call time (`register_dag_tools` runs before the service exists);
  the tool schema advertises the type and its fields only when the flag and `NOUS_A2UI_ENABLED`
  are both on.
- Nodes already waiting when the flag or the companion is turned off still default at their
  deadline; with no service, pushes are skipped and taps cannot arrive.
- New settings: `NOUS_DAG_APPROVAL_DEFAULT_WAIT_SECONDS` (86 400),
  `NOUS_DAG_APPROVAL_MAX_WAIT_SECONDS` (604 800), `NOUS_DAG_APPROVAL_CARD_GRACE_SECONDS` (3 600),
  `NOUS_DAG_MAX_PARKED_DAGS` (20).

### 3.14 Tool text and the reserved key

- `dag_create` describes the approval node: connect it to the node it gates with a `context_flow`
  edge (the successor then waits for the answer and receives it); the node that acts must be a
  `subtask` (a `callback` executes nothing while `NOUS_DAG_CALLBACK_EXECUTION_ENABLED` is off, as
  in prod); the default must be a *stop* option.
- `push_surface` and `compose_surface` reject an agent-supplied `dedup_key` with the
  `dag-approval:` prefix. Otherwise an agent push could replace a DAG card's text in place, keeping
  its id, and taps would still answer the node.
- `push_surface`'s description stops promising that an approval choice can be "checked": outside
  DAGs nothing reads it back (§8).

## 4. Lifetimes and invariants (written first, per F087)

Three lifetimes interact: the **node attempt** (`pending → ready → awaiting_input → completed |
failed | cancelled`; the park write starts it), the **card** (`live → resolved | expired`, or
replaced in place by the same dedup key), and the **answer** (set at most once per attempt,
archived when the next attempt parks).

- **I1 — one answer per attempt.** Every write that ends `awaiting_input` is conditional on
  `status='awaiting_input'` and writes the terminal status itself. The tap, the deadline, a second
  tap, a cancel and the budget race on one row; exactly one wins.
- **I2 — a card answers only its own attempt.** A tap finds its node through the card's dedup key,
  and the write requires `surface_id` to be unset or equal to the card's. One node has at most one
  live card (dedup). A relaunch or retry while the old card is still live replaces it in place and
  rotates its nonce, so a tap from the old render fails `NONCE_MISMATCH`; otherwise it pushes a
  fresh card.
- **I3 — the deadline is the orchestrator's.** The card's expiry is always later than the node's
  deadline; the card never decides.
- **I4 — a parked DAG never blocks admission of working DAGs, and parked DAGs are bounded.**
- **I5 — creation is gated; resolution is not.** No flag strands a waiting node.
- **I6 — nothing reports an answer that was not recorded.** A DAG card never takes the generic
  path; every handler message is derived from what `answer_node` wrote or found.

## 5. Crash windows

| Crash between | State left | Recovery |
|---|---|---|
| `ready` and the park write | node `ready` | `_recover_stale_ready_nodes` relaunches it once the DAG is 300 s old |
| retiring the old card and the park write | node `ready`, old card closed | the relaunch finds nothing to retire and parks |
| park and push | node parked, `surface_id NULL`, no card | the next tick's `_poll_awaiting_input` pushes; the deadline bounds it |
| push commit and the Telegram ping | card live, no ping sent | the card is in the companion; a relaunch replaces in place and does not ping (accepted, §6) |
| push and link | node parked, `surface_id NULL`, live card | a tap still resolves by dedup key; the next tick pushes again, which replaces the same card in place, and links it |
| answer and card close | node terminal, live card | the leaked-card sweep closes it; taps before that get `closed` |
| a cancel's node write and card close | same | same |
| a tick holding a stale node while a tap lands | in-memory `awaiting_input`, row terminal | the tick's own conditional writes match nothing; the next tick sees the terminal status |

## 6. Accepted limits (documented, not fixed in v1)

- **The approval binds a text, not the action.** The person approves what the card shows; the
  successor subtask may still act differently. Binding the action is P1.4's job.
- **The agent could answer its own card.** It holds `bash`, the companion's action endpoint has no
  authentication of its own on the internal port, and a card snapshot carries its nonce. Such an
  answer is recorded as `unattributed` and the `bash` call is in the execution ledger. Closing this
  belongs with companion authentication, not this phase.
- **One Telegram ping per card, at most.** The ping runs detached and swallows failures, and an
  in-place replacement never pings, so a crash after the push commit or a Telegram outage means
  the person is not told. With a *stop* default the cost is bounded: the DAG stops at the deadline
  and F087 announces that on Telegram with the reason. A durable, retried ping is out of scope.
- **`answered_by` is usually `unattributed`** unless the deployment trusts forwarded identity.
- **Deadline precision** is one heartbeat loop iteration.

## 7. Testing

- **Validator:** option count, ids, labels and outcomes; at least one of each outcome; the default
  must be a *stop* option; recommended membership; every rejected field on an approval node;
  fix-on-approval rejected; an approval with no outgoing gating edge rejected; `ge=900`; flag off
  and not wired → rejected in the handler; the approval fields reach `DAGNodeSpec` through
  `dag_create` (an F066.1-style threading test).
- **Migration:** `test_split_full_migration_076` pins the statement count.
- **Store:** `transition_node` wins once and loses thereafter; the live-DAG and card predicates;
  the deadline predicate with a naive stored timestamp; the admission count with parked, working,
  deferred-sibling and mixed DAGs; the parked cap; `count_active` agrees with `create`.
- **Races:** the SQLite test engine shares one connection, so a race is tested by injection — the
  awaited step between a writer's load and its write is patched to run the competing write (a tap
  landing inside `cancel_dag`, a cancel landing between park and link). Real row-lock behavior is
  exercised only on CI Postgres.
- **Orchestrator** with a real `SurfaceService` on the SQLite test DB:
  - launch parks, pushes one card and links it; the card's summary leads with the question and
    marks truncation; the risk line states the deadline and the default;
  - a transient push failure leaves the node parked and the next tick pushes; a censor refusal
    fails the node at once;
  - a relaunch after a crash between push and link replaces the card in place (same id, one ping);
    a crash before the park is recovered by the stale-ready sweep once the DAG is aged past 300 s;
  - a retry retires the previous attempt's still-live card before parking, and a tap on it gets
    `not_open`;
  - the deadline applies the default, closes the card and the successor is blocked in the same
    tick;
  - *stop* blocks a `dependency` successor and a `context_flow`-only successor while a parallel
    branch completes, and the DAG ends `failed` (§3.8 regression);
  - `cancel_dag`, `cancel_cascade` and the budget path each cancel a waiting node, close its card,
    and lose cleanly to an answer that landed first;
  - retry of a deadline-stopped node asks again with a new card and archives the old answer; retry
    of a human-declined node is refused for the agent and allowed with `allow_declined=True`;
  - the leaked-card sweep closes a card whose node is terminal.
- **End to end through `ActionRouter`** (the POST `/a2ui/action` shape): tap → node `completed`,
  the next tick launches the successor with the answer in its context; a tap before the link step
  is recorded; a late tap on a card still live → `ok=False`, audited `rejected`, card retired with
  the specific message (once the card is closed the router answers 404 before auditing);
  a *stop* tap passes a censor that blocks the card's title; `/summary` is untouched after a tap or
  a defer; an agent-pushed `approval_gate` behaves exactly as today; `push_surface` and
  `compose_surface` reject the reserved prefix.
- **Delivery:** the template's `Approvals:` section and the `stopped at an approval` verb.

## 8. Out of scope

- A *proceed* default (a validator change once there is a use for it).
- A read-back tool for agent-pushed `approval_gate` choices, and fixing `approval.defer`'s
  `/summary` overwrite on agent-pushed cards.
- Per-option branching (different successors per answer).
- Telegram inline answer buttons; more than one approver; widening budgets through an approval
  (P1.6); binding the approved action (P1.4); companion authentication.
- Dashboard styling of `awaiting_input` (the views fall back to grey for unknown statuses).

## 9. Decisions for the user

1. **Default must be a *stop* option in v1** (§3.1). Recommended: yes.
2. **A separate parked cap, `NOUS_DAG_MAX_PARKED_DAGS=20`** (§3.11). Parked DAGs still do not count
   against the 5; the cap only bounds how many can wait at once. Recommended: yes.
3. **Fix the `context_flow` propagation bug in this PR** (§3.8). It changes behavior for existing
   DAGs with `context_flow`-only successors — they now end `failed` instead of hanging `running`.
   Recommended: yes.
