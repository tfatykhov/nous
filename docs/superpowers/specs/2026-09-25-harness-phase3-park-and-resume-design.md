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
| A *proceed* default is an unapproved action (devil 3) | v1 requires the default to be a *stop* option (§3.1) — confirmed by the user, §9 |
| The card can lose the question, never states the deadline, overwrites the draft on "Ask me later", recommends `options[0]` by default, says "the DAG resumes" on a *stop* (architect 7, devil 4) | Card contract §3.4 |
| A human "no" reads as a failure, and the agent can simply re-ask it (devil 5) | Declined answers are rendered as answers, and only the companion can retry them (§3.10, §3.12) |
| No bound on parked DAGs; answered parked DAGs all resume past the admission cap (architect 6, db 9, devil 14) | A separate parked cap (§3.11) — confirmed by the user, §9 |
| The deadline compared in Python raises `TypeError` on SQLite's naive timestamps and aborts the DAG's tick (db 8) | The deadline is a predicate in the conditional write (§3.6) |
| A card from an earlier attempt can answer the next one (db 5, 16) | The previous attempt's live card is retired before the new attempt parks (§3.4 step 0) |
| CI applies migrations with `psql`, prod with its own statement splitter — one stray comment passes CI and breaks prod boot (db 12) | A splitter test for 076 (§3.2) |
| Interface, wiring, reserved key, censor, leaked cards, lock order, audit (architect 8–18, db 6–7, 10–21, devil 6–12, 15, 17–19) | §3.3–§3.14, §6 |

**v2.2–v2.3** fold the re-reviews of v2 (architect, devil, database). v2.3 (database): a node
could be stranded `ready` forever when a downstream-unblocked approval (its `started_at` kept)
failed before parking — steps 0–1 now fail closed through `_defer_node` and the unblock clears
`started_at`; `apply_retry`'s writes are conditional; `answer_node` gains `dag_ended` and
`stray_card` and writes `surface_id` only when a card is named; a re-push re-reads the row first;
the parked cap applies only to requests containing an approval node; `expire_sweep` writes no
`no_objection` for DAG cards. v2.2 (architect, devil):
`answer_source` is `companion`/`deadline`, never `human`; refused taps leave the card up (the
companion shows a message only when `ok` is false); the leaked-card sweep runs inside `_lock` and
never touches an unlinked node, which had let it kill a card between push and link; approval nodes
keep the `NOT NULL` default timeout, which v2 would have nulled and so failed every insert; step 0
defers instead of parking when it cannot retire the old card; a linked card lost for any reason is
re-pushed; `transition_node` carries `dag_statuses` and `due_by`; resuming parked DAGs is
admission-controlled at dispatch; the stop-tap censor exemption is dropped; fix nodes below an
approval may not amend it; `_dispatch_ready_nodes`' `ready` write is conditional; the ping carries
the question; the reserved prefix is enforced in `push_built`.

**v2.4** folds the late spec re-reviews (architect on v2.1, database on v2.2/v2.3), which arrived
after the plan was written:
- **P1 (architect).** The acting node never received the draft the person approved. The recommended
  wiring is draft → approval → acting node, and `_build_predecessor_context` passes only direct
  results. An approval now passes its own `context_flow` inputs through (§3.5).
- **P2 (database, probe).** A node could end up `awaiting_input` inside a cancelled DAG with no card,
  through conditional writes only. `dag_statuses` is a snapshot, and a concurrent companion retry
  plus a failed push got there. The node-driven sweep query is restored, with a partial index
  (§3.2, §3.7).
- **P2 (database).** The leaked-card sweep read a bounded page of cards that healthy cards could
  fill. It now reads every live DAG card, which the parked cap bounds (§3.7).
- **P2 (architect).** For subtask and check nodes, the write that resurrects a cancelled node is the
  launch's own `running` write after an await. It is now conditional, and a lost launch cancels
  what it just created (§3.3).
- **P2 (both).** `push_built`'s two internal retries forward the new flags. The reservation raises
  its own `ValueError` subclass (§3.14).
- **P3.** A re-push adopts an existing live card (§3.6). The dispatch gate exempts nodes that cost
  nothing, states its counter rule, and shows a held DAG in `dag_manage` (§3.11). The F064.2 cap
  demotion is conditional (§3.3). The ping text is built only from strings already on the card
  (§3.4).

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
| The companion button shows an action's message only when `ok` is false; a resolved card is deleted from the view | `dashboard-app/src/companion/catalog/ButtonView.svelte:55-56` |
| The Telegram ping carries only the card title and a link (relative, so untappable, unless `NOUS_A2UI_PUBLIC_BASE_URL` is set) | `service.py:600,1290-1305` |
| `dag.retry` is offered only on a `dag_monitor` card, which only the agent pushes | `nous/a2ui/builders/dag_monitor.py:26`; `nous/a2ui/tools.py:616` |
| A fix node's `retry_with_amended_prompt` replaces its parent's `instructions` | `orchestrator.py:2348-2356` |
| `_dispatch_ready_nodes` writes `ready` without a status predicate before launching | `orchestrator.py:1994` |
| `_check_dag_completion` writes `result_summary="Failed nodes: …"`; blocked dependents read `Predecessor failed` | `orchestrator.py:2653-2659,1945` |
| `dag_create` builds each `DAGNodeSpec` from a fixed dict (new fields must be threaded explicitly — the F066.1 silent-drop lesson) | `nous/api/tools.py:4846-4883` |

## 3. Design

### 3.1 The node spec and its validation

A new node type `approval` in `DAGNodeType`, authorable through `dag_create`:

| Field | Meaning |
|---|---|
| `instructions` (required, 1–2 000 chars) | The question put to the human. Capped because the question is never cut on the card, and an unbounded one could exceed the censor's input cap and fail as "refused by censor". |
| `description` | Card title; defaults to the node name. |
| `options` (required) | 2–4 of `{id, label, outcome}`: `id` `^[a-z0-9_-]{1,40}$`, unique; `label` 1–80 chars; `outcome` `proceed` or `stop`. At least one of each outcome. |
| `default_option` (required) | An option `id` whose outcome is **`stop`** (v1): what happens when nobody answers by the deadline. |
| `recommended_option` | An option `id` highlighted on the card. Default: none — the card recommends nothing unless the author says so. |
| `answer_timeout_seconds` | Time allowed for an answer. Default `NOUS_DAG_APPROVAL_DEFAULT_WAIT_SECONDS` (86 400); `ge=900`; clamped at insert to `NOUS_DAG_APPROVAL_MAX_WAIT_SECONDS` (604 800). |

`DAGNodeSpec` / `DAGCreateRequest` validation:

- The four approval fields are allowed only on `approval` nodes, and `options` + `default_option`
  are required there.
- Rejected on `approval` nodes when set to a real value (explicitly, not ignored — `dag_create`
  passes every one of them as `n.get(...)`, and LLM-authored JSON routinely emits `[]` or `0` for
  "none", so `None`, empty containers, empty strings and `0` all count as "not given"):
  `completion_check*`, `parent_node`, `fix_actions`, `tools`, `frame_type`, `model`,
  `timeout_seconds`, `stall_timeout_seconds`.
- A fix node may not name an approval node as its `parent_node` (a human "no" is an answer, not a
  failure to repair).
- A fix node whose `parent_node` is downstream of an approval node (along §3.8's edges) may not
  list `retry_with_amended_prompt`: it replaces the parent's instructions (`orchestrator.py:2348`),
  so an LLM-"corrected" recipient or body would run under an approval given for different text.
  `retry_as_is` stays allowed; harness 2b keys make a resend of the same logical send safe.
- An approval node must have at least one outgoing `dependency` or `context_flow` edge — an
  approval that gates nothing is a mistake.
- The feature flag is checked in the `dag_create` handler against the wired `Settings`, not in the
  validator (`schemas.py:233-241` builds a fresh `Settings()` from the environment).
- `DAGStore.create` keeps the resolved default `timeout_seconds` for approval nodes — the column is
  `NOT NULL` (`032:42`, `models.py:1137`) and `DAGNode.__init__` does not replace an explicit
  `None`, so a `NULL` would fail every insert; nothing reads it for an approval node, since
  `_effective_timeout` serves only the `running`, `awaiting_check` and subtask-launch paths. It
  skips only the stall resolution and validation (`store.py:106-143`) and stores
  `stall_timeout_seconds` `NULL` (that column is nullable).

**Why the default must be *stop* in v1.** An unanswered card is the common case — the person is
asleep, the ping was missed, the link was not tappable. With a *proceed* default, silence becomes
an approval, and the action the card exists to guard runs with nobody having seen it. With a
*stop* default the worst case is a stopped DAG: the agent can retry a node stopped at its
deadline, and a person who declined can ask the agent to re-open the question (§3.10). A
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
  | `answer_source TEXT CHECK (answer_source IN ('companion', 'deadline'))` | Where the answer came from — not who. A companion tap is `unattributed` in prod, and the server cannot show that a person made it (§6), so nothing is named `human`. |
  | `answer_history JSONB` | Earlier attempts' answers, archived at each relaunch (§3.4). The node row is the audit record for a deadline default — it writes no `a2ui_actions` row; a companion tap has both. |

  The park time is `started_at`; v1's `awaiting_since` is dropped as a duplicate. A tap resolves its
  node by primary key (§3.5), and the tick already loads every active DAG's nodes. One partial
  index, `idx_dag_nodes_awaiting_input ON dag_nodes (dag_id) WHERE status = 'awaiting_input'`,
  keeps the sweep's node-driven query (§3.7) off the DAG history.
- The ORM mirrors all of it (both CHECK lists, the named `answer_source` CHECK). SQLite enforces
  the ORM's CHECKs, so a drift between migration and ORM fails the SQLite tests.
- **Rollback:** pre-076 code treats `awaiting_input` as non-terminal forever and counts it toward
  `MAX_ACTIVE_DAGS`, so cancel every DAG with an approval node before rolling back.

### 3.3 One conditional transition

```python
async def transition_node(
    self, node_id, *, from_statuses, dag_statuses=None, card=None, due_by=None, **values
) -> bool
```

One `UPDATE … WHERE id = :node AND status IN (:from_statuses)` — agent-scoped through the
`execution_dags` subselect as `claim_and_add_node_tokens` is (`store.py:509-534`) — plus, when
given: `dag_statuses` → `AND dag_id IN (SELECT id … WHERE status IN (:dag_statuses))` (`LIVE_DAG =
{'pending','running'}` for answers, launches and cancels; the terminal set for the sweep, so a
`retry_node` that reactivates the DAG between the sweep's read and its write wins); `card` →
`AND (surface_id IS NULL OR surface_id = :card)`; `due_by` → `AND answer_deadline <= :due_by`.
Returns `rowcount == 1`.

Every write that leaves `awaiting_input` uses it with `from_statuses={'awaiting_input'}`: the tap,
the deadline, `cancel_dag`, the `cancel_cascade` branch, the budget path, a failed card push and
the leaked-card sweep. The answer and the terminal status are written in the same statement, so
there is no "answered but still waiting" state, and the row's own predicate — not a lock — decides
the race between a tap, the deadline and a second tap. It holds across processes, which the
in-process surface locks do not.

Existing blind writes become conditional too, because they can race a park or an answer:

- `cancel_dag` and the `cancel_cascade` branch write `cancelled` with
  `from_statuses = non-terminal statuses`, and an approval node they win against has its card
  closed (§3.7);
- `_dispatch_ready_nodes` writes `ready` (all four sites, `orchestrator.py:1994,2007,2047,2074`)
  with `from_statuses={'pending','ready'}` and skips the launch when it loses, so it cannot
  resurrect a node `cancel_dag` just cancelled;
- `DAGStore.apply_retry` makes each node write conditional on the status `retry_node` read —
  `{'failed'}` for the retried node, `{'blocked','cancelled'}` for the ones it unblocks — and rolls
  the whole retry back if the retried node's write does not apply. Otherwise two concurrent
  retries (agent and companion) with a tap between them let the second blind-write `pending` over
  a recorded answer.

- the launch's own `running` write for subtask and check nodes (`orchestrator.py:2469-2479,
  2532-2537`), which lands after an await on `SubtaskManager.create` or `create_check`. A
  `cancel_dag` during that await sees no `subtask_id`, so it cancels nothing, and the blind write
  overwrote its `cancelled`. The subtask then ran inside a cancelled DAG and could send what the
  person just cancelled. The write becomes `transition_node(from_statuses={'pending','ready'},
  dag_statuses=LIVE_DAG, status='running', …)`, and a lost write cancels the subtask or disables
  the check it just created;
- the F064.2 cap demotion and `_defer_node`'s demotion to `pending` (`from_statuses={'ready'}`).

These apply to every node type; outside the races they behave exactly as today. `dag_statuses` is
a snapshot filter — the `UPDATE` takes no lock on the `execution_dags` row — so a DAG that turns
terminal in the same instant can still gain a parked node; the sweep's node-driven query (§3.7) is
the backstop for that.

### 3.4 Launch: park, push, link

`_launch_node` gains an `approval` branch (today an unhandled type silently does nothing — it has
no `else`). `_launch_approval_node`, inside the tick with `_lock` held:

0. **Retire the previous attempt's card.** A card left live by an earlier attempt (its close lost
   to a crash, or never linked) carries the same dedup key and a valid nonce, so between this
   attempt's park and its push it could answer the new attempt with the old content.
   `SurfaceService.close_by_dedup_key(approval_dedup_key(node.id), 'expired')` closes it under its
   surface lock first; a tap already in flight on it finds the node not yet parked and gets
   `not_open` (§3.5). On a first attempt this finds nothing. I2 depends on this step, so it is not
   best effort.

   **Steps 0–1 fail closed.** Any exception before a successful park — the close in step 0, a
   transient database error in the park write — sends the node back to `pending` through
   `_defer_node` (bounded by `_MAX_DEFERRALS`, since no deadline exists yet). `_dispatch_ready_nodes`
   only logs a launch exception, so without this the node would stay `ready`, and
   `_recover_stale_ready_nodes` takes only `ready` nodes with `started_at IS NULL` — which a
   downstream-unblocked node does not have (see §3.9's `retry_node` row). The node would hold a
   working slot forever, the F087 wedge.
1. **Park** — `transition_node(from_statuses={'pending','ready'}, dag_statuses=LIVE_DAG)` writing
   `status='awaiting_input'`, `started_at=now`, `answer_deadline=now + wait`, and `NULL` for
   `surface_id`, `answer`, `answered_by`, `answered_at`, `answer_source`, `result`, `error`,
   `completed_at`. If the node carried an answer from an earlier attempt, the same write appends
   it to `answer_history` (computed from the in-memory node — safe, because nothing answers a
   node that is not `awaiting_input`). **This write is the single reset point for an attempt**,
   whichever path (`retry_node`, the downstream unblock, a relaunch) made the node pending. If it
   returns `False`, someone else moved the node; stop.
2. **Push** — build the card (below) and call
   `SurfaceService.push_built(built, dedup_key=approval_dedup_key(node.id), notify=True,
   notify_text=…, reserved_key_ok=True)`. `notify_text` is a new optional ping body (the title
   alone otherwise): `<title>` / the question's first line, cut at 200 chars / `No answer by
   <deadline UTC> → '<default label>'.` / the link — the ping is the only notice, and in prod its
   link is not tappable. It is built only from strings already on the card (title, the question,
   the risk line's deadline, a default option label), all of which the push censor checked, so the
   ping adds no uncensored text to an external channel.
   - A censor refusal (`PermissionError`) or a build/validation error → `transition_node` to
     `failed` with `error="approval card refused by censor: …"` or `"approval card could not be
     built: …"`. These do not heal on retry, so they fail at once.
   - Any other exception, or no `SurfaceService` wired → leave the node parked with
     `surface_id NULL` and the reason in `error`; `_poll_awaiting_input` pushes again each tick
     until the deadline, which bounds the retries (no `_MAX_DEFERRALS` needed).
3. **Link** — `transition_node(from_statuses={'awaiting_input'}, surface_id=sid, error=None)`. If
   it returns `False` the node left `awaiting_input` between 1 and 3 (cancelled, or answered by a
   tap that already found it); close the card just pushed (`close` treats a card an answer already
   resolved as closed).

The card (`approval_gate` builder, with three small builder changes: a `recommend_first=False`
switch that disables the `options[0]` fallback, `outcome` kept in the server-side options data,
and a `defer_label` — DAG cards say "Decide later", since nothing will ask again):

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
    outcome: Literal[
        "recorded", "closed", "not_open", "dag_ended", "stray_card", "not_linked", "invalid_option"
    ]
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
- One `transition_node(from_statuses={'awaiting_input'}, dag_statuses=LIVE_DAG, card=surface_id,
  due_by=now if source == 'deadline' else None)` — the `card` predicate means a card can only
  answer the attempt it belongs to, and `due_by` means the deadline decides in SQL (§3.6) — writing
  `answer`, `answered_by`, `answered_at=now`, `answer_source`, `completed_at=now`, `surface_id`
  only when a card is named (a tap that lands before the link step links it; the deadline, which
  names none, leaves the stored link alone), and:
  - *proceed*: `status='completed'`, `error=None` (a stale push-failure note must not survive),
    `result="Answered in the companion: '<label>' (<id>) at <UTC>[ by <actor>]"`;
  - *stop*, companion: `status='failed'`, `error="declined in the companion: '<label>' (<id>) at <UTC>[ by <actor>]"`;
  - *stop*, deadline: `status='failed'`, `error="no answer by <deadline UTC>; default '<label>' (<id>) applied"`.

  `[ by <actor>]` is omitted when the actor is `unattributed` (the router's value unless
  `NOUS_A2UI_TRUST_FORWARDED_IDENTITY` is on); the column still stores it. Successor prompts see
  the `result`, never "by unattributed".
- `True` → `recorded`. `False` → re-read the node: `pending`/`ready` → `not_open` (this attempt has
  not parked yet, so the card is from an earlier one); still `awaiting_input` → `dag_ended` if its
  DAG is no longer live, else `stray_card` (the card is not the one linked to this attempt);
  otherwise `closed`, with what actually happened (answered, defaulted or cancelled, by whom,
  when).
- The lookup and the write are agent-scoped and require a live DAG, so no path tells a person "the
  DAG continues" into a DAG that has already ended.
- It takes no orchestrator lock; the tap path holds only its card's surface lock.

**Lock order.** Orchestrator `_lock` → an approval card's surface lock, never the reverse: the tick
takes card locks in step 0, the deadline close, the sweep and the budget path, so the state
machine can now wait on an in-flight tap — acceptable because every holder does DB work only (no
Telegram or other slow I/O inside `close`; the ping is scheduled detached, and only on a push that
creates a card). Nested surface locks: the companion's `dag.cancel` holds its `dag_monitor` card's
lock while `cancel_dag` closes approval cards, so the allowed nesting is monitor card → approval
card; nothing holding an approval card's lock takes another surface lock or `_lock`.

**How a tap finds its node.** Every DAG card carries the dedup key `dag-approval:<node_id>`
(one leaf module, `nous/dag/approval.py`, owns the prefix and both directions of the mapping). The
`approval.choose` handler parses the node id from `ctx.surface.dedup_key` — so a tap is resolved
even in the window before the link step, and a DAG card never falls into the generic path.
`ActionContext` gains `actor: str = "unattributed"` (the router already computes it; both
construction sites — `handle_call` at `actions.py:198` and the action path at `:352` — pass it).

What the person sees is fixed by the companion as it is: a button shows the handler's message only
when `ok` is false (`ButtonView.svelte:55-56`), and a resolved card disappears. So a recorded
answer resolves the card — its disappearing is the confirmation, and any patched text would never
be seen — while every refusal leaves the card up, showing why, until the orchestrator retires it
(the leaked-card sweep, §3.7, or step 0 of the next attempt, §3.4). A card that vanished on a late
tap would read as accepted.

| `answer_node` returns | Handler result |
|---|---|
| `recorded` | `ok`, resolve. |
| `closed` | `ok=False`, card left up, a specific message: `already answered '<label>' at <time>`, `no answer by the deadline — '<label>' was applied at <time>`, or `this DAG step was cancelled`. Audited `rejected`. |
| `not_open` | `ok=False`, card left up: `this question will be asked again on a new card`. |
| `dag_ended` | `ok=False`, card left up: `this DAG has already ended`. |
| `stray_card` | `ok=False`, card left up: `this card is out of date — answer the current one`. |
| `invalid_option` | `ok=False`, as today. |
| `not_linked` (node gone) | `ok=False`, card left up: `this DAG step no longer exists`. |
| orchestrator not wired | `ok=False`, card left up: `DAG orchestration is not running; the answer cannot be recorded now`. |

`/summary` is never patched on a DAG card: it holds what the person is approving. `approval.defer`
on a DAG card patches `/risk` to `Deferred. If nobody answers by <deadline>, '<default label>'
applies.` Agent-pushed `approval_gate` cards keep today's handlers unchanged.

**Censor.** The action-time censor applies to DAG cards unchanged, with no exemption. It runs in
the router before any handler (`actions.py:330-339`), so an exemption would need a new router hook
and a way to keep unscreened client text out of the audit row. It is not worth either: the card's
text already passed the push-time censor, and a refused *stop* tap leaves the node waiting, so the
*stop* default applies at the deadline anyway.

**The acting node receives what was approved.** With the recommended wiring (draft → approval →
acting node, both `context_flow`), the approval's own `result` is only the answer text, and
`_build_predecessor_context` passes only direct `context_flow` predecessors' results. So the acting
subtask would never see the draft, and it would write its own text. When a `context_flow`
predecessor is an approval node, `_build_predecessor_context` therefore also includes that
approval's own `context_flow` predecessors' results, labelled as approved input. This happens in
one place and needs nothing from the author. The approval still binds the text, not the action
(§6), but now the acting node at least has the approved text in front of it.

**Resumption** needs nothing else: the next tick sees a `completed` node (dependents become ready;
the answer is the node's `result`, which `context_flow` successors receive) or a `failed` one
(`_propagate_failures` blocks every successor along the predecessor edges, §3.8; unrelated
branches finish; F087 delivers the outcome, §3.12).

### 3.6 The deadline

A new tick step `_poll_awaiting_input(dag)`, after `_sync_node_statuses` and before the budget and
failure steps, for each `awaiting_input` node:

- `answer_deadline <= now` → `answer_node(node.id, default_option, source='deadline',
  actor='system:deadline')`; on `recorded`, close the card and update the in-memory node so this
  tick's propagation sees it. The comparison that decides is the `due_by` predicate in the
  conditional write (`AND answer_deadline <= :due_by`, with `:due_by` an aware UTC value computed
  in Python), as `expire_sweep` revalidates `expires_at` inside its claim. SQLite returns the
  stored timestamp naive, so a Python comparison against an aware `now` raises `TypeError` and
  aborts the DAG's whole tick. The in-memory pre-filter normalizes the timezone, as the five
  existing sites do (`orchestrator.py:1142,1182,1614,1832,2134`).
- else, `surface_id IS NULL` while a live card with the node's key already exists (a crash between
  push and link) → **adopt** it: a conditional link (`card=<that id>`, `surface_id=<that id>`)
  instead of a re-push, which would replace the card the person may be tapping;
- else, `surface_id IS NULL`, or its linked card is no longer live (one
  `SurfaceService.live_ids(surface_ids)` query per tick over the waiting nodes) → push again
  (§3.4 steps 2–3; the link write re-links from the dead card's id). A card lost for any reason
  heals on the next tick instead of leaving the question unasked until the deadline. The node row
  is re-read immediately before a re-push — the tick's copy may predate a tap that answered it
  through the unlinked card, and pushing then would create a fresh card and ping for an answered
  question — and the re-pushed card's risk line uses the stored `answer_deadline`, never
  `now + wait`.

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
(`orchestrator.py:276-310`) but run **inside `_lock`** — the heartbeat sweep runs outside it, and
this one must not interleave with a launch between push and link — reads **every** live card whose
dedup key has the `dag-approval:` prefix (`SurfaceService.live_cards_by_prefix(prefix)`, no limit:
a bounded page would fill with healthy linked cards once the parked cap is reached, and never
reach the leaked ones behind them; the parked cap times approvals per DAG bounds the set), maps
keys to nodes in Python (SQLite stores UUIDs without dashes, so an SQL text join would behave
differently in tests), loads their nodes, and:

- node missing, or not `awaiting_input` → close the card `expired`;
- node `awaiting_input` but its DAG terminal → `transition_node(from_statuses={'awaiting_input'},
  dag_statuses=terminal)` to `cancelled` (`error="DAG ended while waiting"`) and close the card;
- node `awaiting_input` with a **non-NULL** `surface_id` different from the card's → close the
  stray card. A NULL `surface_id` means push and link own the node, and the card may be the one
  just pushed; the sweep leaves it alone.

The same sweep retires cards a refused tap left up (§3.5). A second, **node-driven** query covers
the case the card-driven one cannot see: an `awaiting_input` node inside a terminal DAG with no
card. A database probe produced that state with conditional writes only. `cancel_dag` loads the
DAG while the approval is `failed` and skips it, a concurrent companion retry resets it to
`pending`, and the tick parks it because the `dag_statuses` snapshot still reads `running`. The
push then fails, and the DAG turns `cancelled`. The query takes those nodes through
`idx_dag_nodes_awaiting_input` (bounded batch) and cancels each with
`transition_node(from_statuses={'awaiting_input'}, dag_statuses=terminal)`. It then closes any
card by dedup key.

`expire_sweep` writes no `no_objection` row for a `dag-approval:` card: the node, not the card, is
the record of what happened, and after an outage longer than wait + grace the startup expiry
(which runs before the first DAG tick) would otherwise record "no objection" for a card that was
answered.

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

Readiness is unchanged, so no existing DAG loses a path that worked. **Deploy note:** any DAG
wedged this way today terminalizes on the first tick after deploy, and F087 announces it as
failed — possibly weeks late. There are at most five (they hold the active slots). Before deploy,
count them read-only on prod (`running` DAGs with a `failed` node whose `context_flow`-only
successor is `pending`) and decide whether to let them announce or mark them delivered quietly.

### 3.9 Interaction with existing paths

| Path | Behavior |
|---|---|
| `cancel_dag` | Conditional `cancelled` write per non-terminal node; an approval node it wins against has its card closed. |
| `_propagate_failures` | Blocks along §3.8's edges. A `cancel_cascade` target that is `awaiting_input` goes through `_finish_approval`. |
| `retry_node` | Resets as today; the park write clears the answer columns and archives the previous answer (§3.4). The downstream-unblock reset also clears `started_at` and `completed_at`, as the direct retry already does (`orchestrator.py:598-599`), so a crash between `ready` and the park leaves a node `_recover_stale_ready_nodes` can see. Writes are conditional (§3.3). A declined approval is refused unless the caller is the companion (§3.10). |
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
`answer_source` is `companion` and whose status is `failed`. When it retries an approval node, its
reset archives the previous answer into `answer_history` and clears the answer columns. The park
write would do the same, but a retried node that never parks keeps no stale answer: one failed
by the deferral cap, or cancelled. A stale answer would otherwise make `approval_line` print the
old decline, make `stopped_at_approval` misread the cause, and refuse the agent's next retry as
"declined". The agent's `dag_manage retry`
(`tools.py:5052`) passes the default; the companion's `dag.retry` handler (`actions.py:668`) passes
`True`. `dag.retry` exists only on a `dag_monitor` card, which only the agent pushes, so the refusal
tells the agent the way forward: "'<node>' was declined in the companion; the agent cannot re-ask
it. If the person wants to reconsider, push a `dag_monitor` card for this DAG (push_surface) — its
Retry button re-asks the question." A person who says "actually, go ahead" in chat then taps Retry
and answers the new card: two taps, both theirs. A node that stopped at its deadline can be
retried by the agent — nobody said no; the question is asked again.

### 3.11 Admission: parked DAGs have their own cap

A DAG is **parked** when it has an `awaiting_input` node and no work: no node in `ready`, `running`
or `awaiting_check`, and no `pending` non-fix node whose predecessors (§3.8's edges) are all
`completed` or `skipped` — that is a sibling about to launch or one deferred by a frame cap or a
full pool (`_defer_node` returns it to `pending`), and it is work.

`DAGStore.create`, in the transaction it already counts in:

- refuses when non-parked `pending`/`running` DAGs ≥ `MAX_ACTIVE_DAGS` (5) — parked DAGs do not
  count, as decided;
- refuses a request **that contains an approval node** when parked DAGs ≥
  `NOUS_DAG_MAX_PARKED_DAGS` (default 20): "20 DAGs are waiting on your answers; answer or cancel
  some first". Without it a looping agent could create any number of DAGs that park at once — each
  a priority-2 card, a Telegram ping and a larger tick. A DAG with no approval node is never
  refused by this cap, so a backlog of unanswered questions cannot block ordinary work (or the work
  queue) for up to a week.

The predicate is defined once in the store (two correlated `EXISTS` over the existing
`idx_dag_nodes_status (dag_id, status)` index, plus the edge check) and used by both `create` and
`count_active`, so they cannot drift. The count and the insert stay non-atomic, as today.

**Resuming is admission-controlled at dispatch.** Without it, answering many parked DAGs at once
puts up to 5 + 20 DAGs to work together; only 5 subtasks may be pending agent-wide
(`heart/subtasks.py:17`), so the overflow meets `SubtaskQueueFull`, goes through `_defer_node`,
and an approved node fails after 30 deferrals (about 15 minutes). So each tick, oldest DAG first,
a DAG that is not already working (no node `ready`, `running` or `awaiting_check`) may dispatch
new nodes only while fewer than `MAX_ACTIVE_DAGS` DAGs are working; otherwise its ready nodes stay
`pending` this tick and no deferral is counted.

What the gate is and is not:
- **Scope.** It bounds working DAGs, not subtasks. Five working DAGs with parallel waves can still
  exceed the agent-wide pending-subtask limit, as they can today. The gate restores the
  pre-feature bound; it does not remove that older failure mode.
- **Exempt nodes.** Nodes that cost nothing are never held: `approval`, `gate`, and `callback`
  while `NOUS_DAG_CALLBACK_EXECUTION_ENABLED` is off. A held DAG dispatches only those, since
  parking takes no subtask and holding it would only delay the question.
- **Counter rule.** A DAG counts as newly working only when, after its dispatch, it has a node
  `ready` or `running`. An approval that parks, or an instant gate or callback, takes no slot. A
  DAG that finishes mid-tick frees its slot on the next tick (≤ one tick).
- **Oldest first, no preemption.** A held DAG waits until a working DAG finishes or parks.
  `dag_manage status` shows it as `approved — waiting for a free slot (N/5 working)`, so a person
  who said "proceed" can see why nothing happened yet.
- **`ready` counts as working.** Wave-0 `ready` nodes of a DAG whose `start_dag` failed hold a
  slot until the stale-ready sweep, at most 300 s. This is benign.
- **It holds only DAGs that contain an approval node.** Only those can resume from parking, which
  is what the gate exists for. A DAG without one is never held, even between waves, after a
  deferral, or after `retry_node` reactivates it. So the ordinary scheduler is unchanged even
  while some other DAG waits on a person (devil's-advocate plan review).
- **Working means a node `ready` or `running`.** `awaiting_check` holds no subtask-queue slot, so a
  DAG polling a check does not count. Counting it would let five slow checks hold a person's
  approved send for hours.

This is a structural change to `tick()`: a pre-pass over the loaded DAGs counts the working ones
before the per-DAG `_advance_dag` loop, and `_advance_dag` receives whether this DAG may dispatch.
The pre-pass runs **only when some loaded DAG contains an approval node** — otherwise it is skipped
entirely and every DAG dispatches exactly as today, so the change is inert on a deployment that has
never created one (prod, while the flag is off), without depending on the flag: parked DAGs left
from before the flag was turned off are still gated. A test pins the inert case (§7).

`get_active_dags` still returns parked DAGs (the tick applies their deadlines). The dashboard's
active count is unchanged and includes parked DAGs.

### 3.12 What the person and the agent see afterwards

- **F087 template:** a new `Approvals:` section lists every approval node — `approved — '<label>'`
  or `declined — '<label>'` with its source and time, `no answer by <time>; default '<label>'
  applied`, or `not answered (cancelled)`.
  Answered approval nodes are left out of `Problems:`, and a DAG whose only failures are *stop*
  answers and the nodes they blocked is announced as `stopped at an approval` rather than `FAILED`.
- **`result_summary`:** in that case `_check_dag_completion` writes `Stopped at approval '<node>':
  '<label>'; N steps not run` instead of `Failed nodes: …`, and `_propagate_failures` writes
  `Blocked: an approval was declined or not answered` on the nodes it blocks instead of
  `Predecessor failed`. Both read in `dag_manage recent` and in the template's `Summary:` line.
- One predicate, `stopped_at_approval(dag)` — every `failed` node is an approval node with
  `answer_source` set — decides the summary, the template's verb and the blocked text, so the three
  cannot disagree. The change is presentation only: the DAG row stays `failed`, the bus event stays
  `dag.failed` (`delivery.py:223`), and the dashboard success rate counts it as failed. The
  agent-summary prompt embeds the template (`delivery.py:376-379`), so the `Approvals:` section
  reaches it with no separate change.
- **`dag_manage`:** `list` marks DAGs waiting on a person; `status` gains an `awaiting_input` icon
  and shows the deadline, the default and the card link, and each answered node's answer and
  source.

### 3.13 Wiring and landing dark

- Only the `SurfaceService` constructor line (`main.py:1076`) moves above the DAG block, keeping
  its `if settings.a2ui_enabled` guard — it needs only the database, settings and heart, and
  nothing between `:990` and `:1076` reads it. The composer, `ActionRouter`,
  `register_a2ui_tools` and the sweep task need the DAG store and orchestrator, so they stay where
  they are. The service is passed to the `DAGOrchestrator` constructor;
  `DAGOrchestrator.approvals_wired` is true when a service is present — the `clock_wired` pattern.
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

- `dag_create` describes the approval node with both of its edges: the draft → approval by
  `context_flow` (the person sees the draft on the card) and approval → the acting node by
  `context_flow` (it waits for the answer and receives it). The acting node must be a `subtask` (a
  `callback` executes nothing while `NOUS_DAG_CALLBACK_EXECUTION_ENABLED` is off, as in prod); the
  default must be a *stop* option; and the agent cannot answer the card — it tells the person to
  open the companion.
- The `dag-approval:` prefix is reserved in `SurfaceService.push_built` itself: any push carrying
  it is refused (`ReservedDedupKeyError(ValueError)` — never `PermissionError`, which step 2 reads
  as a censor refusal and fails the node for good) unless the caller passes `reserved_key_ok=True`,
  which only the orchestrator does. `push_built` calls itself at two sites (the dedup-race retry
  and the `IntegrityError` retry); both forward `reserved_key_ok` and `notify_text`, the lesson
  F092.3 recorded for `refuse_fallback_overwrite`.
  Otherwise an agent push through `push_surface` or `compose_surface` — or a future producer —
  could replace a DAG card's text in place, keeping its id, and taps would still answer the node.
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
  live card (dedup). A new attempt retires the previous attempt's card before it parks (§3.4 step 0,
  not best effort), then pushes a fresh card with a new id and a ping. Only a same-attempt re-push
  (§3.6) replaces a live card in place, rotating its nonce so a tap from the old render fails
  `NONCE_MISMATCH`.
- **I3 — the deadline is the orchestrator's.** The card's expiry is always later than the node's
  deadline; the card never decides.
- **I4 — a parked DAG never blocks admission of working DAGs, and parked DAGs are bounded.**
- **I5 — creation is gated; resolution is not.** No flag strands a waiting node.
- **I6 — nothing reports an answer that was not recorded.** A DAG card never takes the generic
  path; every handler message is derived from what `answer_node` wrote or found.

## 5. Crash windows

| Crash between | State left | Recovery |
|---|---|---|
| `ready` and the park write (process death) | node `ready`, `started_at NULL` (every reset clears it, §3.9) | `_recover_stale_ready_nodes` returns it to `pending` once the DAG is 300 s old |
| `ready` and the park write (an exception) | — | steps 0–1 fail closed: `_defer_node` returns it to `pending` at once (§3.4) |
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
  answer is recorded exactly like a tap — `answer_source='companion'`, actor `unattributed` — and
  the execution ledger stores the `bash` command only as a hash, so v1 cannot tell the two apart.
  That is why nothing is labelled "human" (§3.2). Closing this belongs with companion
  authentication; recording the request's client host and user agent on the audit row is the
  cheap first step (§8).
- **A tap gives no confirmation beyond the card disappearing** (§3.5); a refusal is shown on the
  card until the next tick retires it.
- **§3.10 slows the agent; it does not bind it.** An agent refused a retry of a declined node can
  still ask the same question again through a new DAG.
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
  `dag_create` (an F066.1-style threading test), and a `dag_create` with an approval node inserts
  (the `NOT NULL` timeout keeps its default).
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
    fails the node at once; a linked card closed behind the node's back is re-pushed and re-linked
    on the next tick;
  - the leaked-card sweep, run between push and link, leaves the fresh unlinked card alone;
  - a failing step 0 or park write defers the node instead of leaving it `ready`; a
    downstream-unblocked node crashed in `ready` is recovered by the stale-ready sweep (its
    `started_at` was cleared);
  - two retries with a tap between them: the second retry's conditional write loses and the answer
    stands;
  - a tap on a card whose DAG ended gets `dag_ended`, on a stale card of the current attempt
    `stray_card`; a re-push after a tap answered through the unlinked card creates no card;
  - the parked cap refuses a DAG with an approval node and admits one without;
  - `expire_sweep` writes no `no_objection` row for a `dag-approval:` card;
  - the dispatch gate holds a resumed DAG's ready nodes (no deferral counted) while
    `MAX_ACTIVE_DAGS` others are working, and releases them when a slot frees; with no approval
    node among the loaded DAGs the pre-pass does not run and no ready node is ever held;
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
    of a node declined in the companion is refused for the agent (the message names the
    `dag_monitor` route) and allowed with `allow_declined=True`;
  - the leaked-card sweep closes a card whose node is terminal, including one a refused tap left up;
  - `_dispatch_ready_nodes` loses cleanly to a `cancel_dag` that landed first and does not launch.
- **Validator (downstream fix):** a fix node below an approval node listing
  `retry_with_amended_prompt` is rejected; `retry_as_is` is accepted.
- **End to end through `ActionRouter`** (the POST `/a2ui/action` shape): tap → node `completed`,
  card resolved, the next tick launches the successor with the answer in its context; a tap before
  the link step is recorded; a late tap on a card still live → `ok=False`, audited `rejected`, the
  card left up with the specific message until the sweep retires it (once it is closed the router
  answers 404 before auditing); `/summary` is untouched after a defer; an agent-pushed
  `approval_gate` behaves exactly as today; `push_built` refuses the reserved prefix without
  `reserved_key_ok`, so `push_surface` and `compose_surface` do too.
- **Ping:** the approval card's Telegram body carries the question's first line, the deadline and
  the default.
- **Delivery:** the template's `Approvals:` section, the `stopped at an approval` verb, the
  `Stopped at approval …` `result_summary` and the blocked nodes' text, all driven by the one
  `stopped_at_approval` predicate; the DAG row and bus event stay `failed`.

## 8. Out of scope

- A *proceed* default (a validator change once there is a use for it).
- A read-back tool for agent-pushed `approval_gate` choices, and fixing `approval.defer`'s
  `/summary` overwrite on agent-pushed cards.
- Per-option branching (different successors per answer).
- Telegram inline answer buttons; more than one approver; widening budgets through an approval
  (P1.6); binding the approved action (P1.4); companion authentication, and recording client host
  and user agent on `a2ui_actions`.
- A companion toast that outlives its surface, so a recorded answer can show a confirmation.
- Dashboard styling of `awaiting_input` (the views fall back to grey for unknown statuses).

## 9. Decisions taken with the user on v2 (2026-09-25)

1. **The default must be a *stop* option in v1** (§3.1).
2. **A separate parked cap, `NOUS_DAG_MAX_PARKED_DAGS=20`** (§3.11). Parked DAGs still do not count
   against the 5; the cap only bounds how many can wait at once.
3. **The `context_flow` propagation bug is fixed in this PR** (§3.8). Existing DAGs with
   `context_flow`-only successors now end `failed` instead of hanging `running`.
