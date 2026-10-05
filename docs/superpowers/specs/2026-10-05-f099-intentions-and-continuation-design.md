# F099: Intentions and Continuation (design)

Status: design agreed with the owner 2026-10-05, revised the same day after a code-level spec review. Phase 1 is next.
Depends on F098 Phase A, the result inbox (#694). F098 Phase C (#696) supplies the memory write.
Supersedes F098 Phase B, the report-only wake turn (#695).
Research basis: a survey of 2025–2026 frameworks, protocols, literature and safety work on agents acting on background results. Key sources are in §10.

## 1. Problem

When Nous spawns background work (a subtask, a DAG or a schedule), the result should come back to **Nous's own loop**, so that Nous can keep acting on the intention that made it spawn the work. The goal is not to report to a person; reporting is one of several things Nous may decide to do.

Three things stand in the way today, and with F098 as built.

1. **Nothing records why work was spawned.**
   - `heart.subtasks` has `task`, `parent_session_id`, `parent_channel`, `notify` and `metadata`, but no goal, plan step or decision.
   - `execution_dags.original_request` exists, but no DAG creator fills it.
   - Each deliberated turn records a "Plan:" decision in the Brain (`TurnContext.decision_id`), but that id never reaches a spawn tool.
2. **Results lose their way back.**
   - A heartbeat callback turn never sees its check's findings.
   - A DAG check node completes with the fixed text "Check completed (self-disabled)", so its findings are lost.
   - No handler subscribes to `subtask_completed`.
   - Scheduled fires, companion actions and inline spawns record no origin at all.
3. **F098 delivers to the chat, and only reports.**
   - The inbox (#694) is keyed by the Telegram chat.
   - The wake turn (#695) is started by the bot, covers only results whose work began in a conversation, and is told to "report them briefly".
   - Its read-only policy is advisory, because `tool_context_policy_mode` defaults to `warn`.

Volume over 30 days (completed subtasks, excluding DAG nodes, plus DAGs):

| Origin | Count |
|---|---|
| Conversation | 12 |
| Scheduled, `notify=true` | 164 |
| Background monitors, `notify=false` | 937 |
| DAG-node subtasks | 523 |
| DAGs | 134 |

## 2. Goals and non-goals

**Goals**
- G1. Every spawn records the intention behind it, in the same transaction as the work it starts.
- G2. When the work finishes, its result returns to the intention, and the intention's wake policy decides what happens.
- G3. For a `continue` intention, Nous runs a continuation turn that decides among continue, revise, drop, report and ask, and acts within its authority.
- G4. A continuation and everything it spawns can never act outward on their own. Outward actions become proposals the owner approves.
- G5. Chains are bounded at their root and can be cancelled. Every arrival decision is recorded and later graded.
- G6. A result is never lost silently. If a continuation cannot run, the owner gets the raw result.

**Non-goals**
- Widening autonomy beyond internal-only. That is decided later, from the recorded decisions (§4.7).
- Checking structured assumptions. They are recorded in Phase 1 and checked no earlier than Phase 3.
- Giving heartbeat triage or check callbacks new spawn rights. Their tool sets stay as they are.
- External agent protocols (A2A, MCP tasks), and backfilling historical results.

## 3. Owner decisions (2026-10-05)

| Question | Decision |
|---|---|
| Autonomy of a continuation | **Internal only.** It may think, update memory and plans, and spawn work within budget. Every outward action becomes a proposal the owner approves. |
| Scope | **Every spawn records an intention, and its wake policy decides** what happens to the result (defaults in §4.1). |
| Intent capture | **Hybrid.** Context is captured automatically, plus one required `intent` line on spawn tools the model calls, enforced only while the flag is on. Spawns made by code paths generate theirs. |
| Where a continuation runs | **One thread per root intention.** Reports and proposals reach the owner through the chat inbox and Telegram. |
| Delivery | **Phased** (§8), each phase dark behind its own flag. |
| Tool enforcement | For `internal_only` contexts, both the offered tool set and dispatch are narrowed **whatever `tool_context_policy_mode` and `tool_offered_set_enforcement_mode` say**. This is the one stated exception to the warn-mode rule. |
| `web_fetch`, `web_search` | Allowed in an internal-only chain. Both send model-chosen text to an outside service (a URL, a search query), so both are an exfiltration path. That is an accepted risk for v1, and those calls are logged (§9). |
| `run_python` | **Denied** in an internal-only chain. It runs in-process with full Python, its network check is a regex, and it can reach Nous's own send paths. |

## 4. Design

### 4.1 The intention record

**`brain.intentions`**: one row per spawn. It is the "K-line" that re-activates the state of mind the work was started in.

| Column | Meaning |
|---|---|
| `id`, `agent_id` | Key and agent scope. |
| `root_id`, `parent_id`, `depth` | Lineage. A root has `root_id = id`, `depth 0`. Budgets and cancels are per `root_id`. |
| `source_kind`, `source_id` | `subtask`, `dag` or `schedule`, plus the id as TEXT. `UNIQUE (agent_id, source_kind, source_id)`. |
| `intent` | One line: why this is needed and what will be done with the result. |
| `origin_kind` | The spawning turn's `ContextKind`, or a code-path label: `scheduler`, `work_queue`, `app_act`. |
| `origin_session_id`, `origin_channel` | Where it was spawned. The channel is where owner-facing output goes; when it is NULL, output goes to the default chat. **Neither is a routing key on the work row** (see I5). |
| `origin_decision_id` | The spawning turn's Plan decision, if there was one. |
| `wake_policy` | `continue`, `remember`, `report`, `none` or `container`. |
| `authority` | `owner` (today's tool set) or `internal_only`. |
| `expected_result`, `assumptions` | Optional. Recorded, not checked yet. |
| `deadline` | When a late result stops being acted on. A root gets `created + root TTL`; a child gets `min(parent.deadline, created + TTL)`. |
| `state` | `pending`, `result_ready`, `deciding`, `awaiting_owner`, `closed`, `cancelled` or `expired`. |
| `close_reason` | Why the row closed: `resolved`, `legacy`, `delivered`, `cancelled`, `expired`, `fallback_report` or `failed_report`. |
| `root_cancelled_at`, `root_expired_at` | Set on **root rows only**, and always written by a cancel or by the TTL sweep. A root row can close after its own first arrival while its lineage lives on. These markers, not `state`, are what "the root is open" means. |
| `claimed_at`, `claim_token`, `attempts` | Claim lease (§4.5). |
| `created_at`, `result_at`, `closed_at`, `updated_at` | Timestamps. |

**`brain.intention_arrivals`** (Phase 2): one row per arrival decision. It holds the chain's history, its budgets and its calibration data. **One arrival consumes a batch**: every intention of the root that the claim moved to `deciding` (§4.5). The turn sees all their results and makes one decision for the batch.

| Column | Meaning |
|---|---|
| `id`, `agent_id`, `root_id`, `n`, `intention_ids` | One per claim, in order. `intention_ids` lists every intention the claim took. |
| `inbox_ids` | The result rows this arrival consumed. |
| `decision`, `note`, `progress`, `confidence` | Taken from `resolve_intention`, or from the fallback (§4.5). |
| `tokens_in`, `tokens_out` | Usage of the continuation turn. |
| `decision_record_id` | The Brain decision that records this arrival. |
| `outcome` | How the arrival ended: `resolved`, `fallback_report` or `failed_report`. |

**States.**
- Normal path: `pending` → `result_ready` → `deciding` → `closed`, or → `awaiting_owner`, which returns to `result_ready` when the owner answers or a proposal is decided.
- `cancelled` and `expired` are terminal.
- Every transition is a conditional `UPDATE … WHERE state IN (<expected>) RETURNING`.

**Authority.**
- `owner` roots: anything spawned by an owner-initiated turn (`interactive`, `mcp`) or by a code path (scheduler, work queue, `app.act`) keeps today's tool set.
- `internal_only`: an intention created **by a continuation turn**, or under an `internal_only` parent, is `internal_only`.
- Other background turns (`subtask`, `scheduled`, `dag_node`, `agent_action`, `heartbeat_*`, `background`) that spawn something give the child **their own intention's authority**. If they have no intention, as with rows created before the flag, the child gets `owner`.
- I3 below guarantees that authority only ever narrows.

**Default wake policy, by origin.**
- Outside a lineage, a spawn tool's optional `wake_policy` argument overrides the default. **Only foreground turns (`interactive`, `mcp`) may widen `none` to `continue`.** Code paths and background model turns cannot, so a background monitor cannot turn its own work into an autonomous chain.
- **Inside an `internal_only` lineage the argument is ignored.** The policy is always `continue`, or `none` for an inline spawn, so no lineage result is ever routed anywhere except back to the continuation.

| Origin of the spawn | Default |
|---|---|
| `interactive`, `mcp` | `continue` |
| A continuation, or anything in its lineage | `continue` |
| `heartbeat_check` / `heartbeat_callback` (only where their declared tools allow spawning) | `continue` (they are Nous's own follow-up work) |
| A `schedule_task` call | `container` (see Schedules) |
| A scheduler fire | `remember` if `notify=true`, otherwise `none` |
| Work-queue DAG | `remember` |
| Companion `app.act` | `none` (its watcher already consumes the result) |
| `dag_summary` | `none` (delivery mechanics) |
| Inline `spawn_task(await_result=true)` and `spawn_sync` | `none` (the result comes back in the same turn) |
| A model spawn from another background turn (`subtask`, `scheduled`, `dag_node`, `agent_action`, `heartbeat_triage`, `background`), for example a scheduled "create a DAG and exit" launcher | That turn's own intention's policy. With no intention: `none` |
| DAG node subtasks | No row of their own; the DAG's intention covers them, and they carry its lineage stamp (I3). |

**Schedules.**
- A schedule's intention is a `container`: it never receives a result, has no TTL while the schedule is active, and sits outside budgets.
- Each fire is a **new root** (`root_id = id`). It keeps `parent_id` = the container for lineage only, and takes its wake policy from the fire row above.
- Cancelling a container deactivates the schedule.
- When the schedule deactivates (a one-shot schedule has fired, or `max_fires` is reached), the scheduler closes the container.
- Fires of schedules created before the flag have no container; their `parent_id` is NULL.
- `schedules.continuation_*` (the F064.5 episode reuse across fires) is unrelated and stays unchanged.

**Closing.**
- A `none` intention, and a `remember` intention once its delivery is done, are closed by the writer that sees the source terminal, with `close_reason = 'legacy'` in Phase 1 and `'delivered'` in Phase 2. **The close happens before that writer's routing-key check**, since F098's writers return early when a row has no routing key. The reconciler also closes intentions whose source is terminal, whatever their routing key, so a failed worker hook does not leave one `pending`.
- Inline intentions are closed in `spawn_task`'s inline path, in the same `finally` that F098 Phase C uses (Phase 1 adds that `finally` itself if #696 has not merged), because inline runs never reach a worker writer.
- The TTL sweep (§4.6) expires only roots that have a `continue` or `report` intention, never containers.
- The repair sweep (§4.5) only considers `continue` and `report` intentions.

**Invariants**

- **I1. One intention per spawn, atomically.** With the flag on, every spawn path writes exactly one intention in the same transaction as the work row. Each store's `create()` (`SubtaskManager.create`, `ScheduleManager.create`, `DAGStore.create`) takes an `intention: IntentionSpec | None` and writes both rows, plus the lineage stamp (I3), in its own session. This covers:
  - the spawn tools;
  - scheduler fires;
  - both work-queue DAG creation sites;
  - `app.act`.

  A child intention is inserted only if its root is still open, meaning `root_cancelled_at` and `root_expired_at` are both NULL. The root row is read `FOR SHARE` in the same transaction, and a cancel always writes `root_cancelled_at` on it, so a cancel and a spawn conflict on the root row and the cancel stops new spawns.
- **I2. Intent source.** `spawn_task`, `dag_create`, `schedule_task` and `spawn_sync` take a required `intent` and an optional `wake_policy`, **only while `NOUS_INTENTIONS_ENABLED` is on**; with it off, the tool schemas are byte-identical to today. A missing or blank `intent` is **refused only in foreground and continuation turns** (`interactive`, `mcp`, `continuation`), with a tool error that says what to write. In every other kind (`dag_summary`, `scheduled`, `subtask`, `heartbeat_*`, `agent_action`, `dag_node`, `background`), a missing `intent` is generated as `"<origin_kind>: <first line of the task or description>"` and the spawn goes ahead. Existing background prompts were written before `intent` existed, and a refused spawn there (for example the F087 summary turn's email subtask) would silently break delivery. Code paths generate the intent:
  - **schedule fire:** the schedule's task text;
  - **work queue:** the item title;
  - **`app.act`:** the action label;
  - **`dag_create`:** also fills `original_request`.
- **I3. Authority only narrows, through every kind of turn in a lineage.** Every turn descended from an `internal_only` intention gets `authority = internal_only`, `intention_id` and `root_intention_id` in its `ExecutionContext`. That covers:
  - the continuation;
  - subtasks it spawns, including inline ones;
  - their DAGs' node subtasks, callback and fix nodes;
  - dynamic checks launched for those DAGs' check nodes.

  How the lineage travels:
  - **Subtasks:** `metadata.intention = {id, root_id, authority}`, written with the row by `create()`. `ExecutionContext.for_subtask` reads it, which needs no lookup and fails closed.
  - **DAG nodes:** at launch, the orchestrator reads the DAG's intention once and stamps it into the node subtask's metadata, or into the dynamic check's metadata, which `dynamic.py` reads into the `heartbeat_check` context. If that lookup fails, the launch is deferred; it is never run unrestricted.
  - **Spawn tools:** the dispatcher injects `_intention_id` for every turn whose context carries one, so any spawn in a lineage creates a child, never a new `owner` root.
  - **Who may spawn:** within a lineage, **only `continuation` turns** are offered `spawn_task` and `dag_create`. A lineage subtask, DAG node or check gets no spawn tools at all, which matches its `CONTEXT_POLICY` (`spawn=False`) in every policy mode.
  - **F087 summary turn:** it is skipped for **every DAG whose intention is `internal_only`**, whatever its wake policy, so it never runs with outward tools for a lineage DAG. The continuation is the consumer.
  - **Approval nodes:** an `internal_only` `dag_create` that includes an `approval` node is refused. Owner-facing questions from a lineage go only through proposals and questions (§4.4), which respect quiet hours.
  - **Dynamic checks created from a lineage** (`heartbeat_check_create` is denylisted for `internal_only`, but a lineage DAG's check node creates a check through the orchestrator) carry the lineage stamp into the check's metadata, so the narrowed tool set applies to every run of that check. Phase 2 must test this.
- **I4. One turn consumer per arrival.**
  - `continue` → the continuation runner.
  - `report` → the chat inbox, as F098 Phase A. In Phase 2 the writer inserts the inbox row and closes the intention (`close_reason = 'delivered'`) in one transaction, so a `report` intention never becomes `result_ready` and is never claimed by a continuation. In Phase 1 every close is `legacy`.
  - `remember` → today's delivery (the notify push and any F098 row), plus a memory write when F098 Phase C is on.
  - `none` → today's behaviour.

  The chat never also injects the raw result of a `continue` intention. Writing to memory is archival and may accompany any policy.
- **I5. Origin is not routing.** Recording the origin (session, channel, decision) never changes F098's routing or Phase C's classification. Today `parent_session_id` and `parent_channel` *are* routing keys: Phase A claims by them, and Phase C reads them as "conversation origin". So the origin is stored on the intention row, and the work row's routing fields are written exactly as today, until §4.3's Phase 2 routing applies.

### 4.2 Capture at spawn

| Spawn path | Intent | Notes |
|---|---|---|
| `spawn_task` (model) | `intent` argument | `_origin_session_id`, `_origin_channel`, `_origin_kind`, `_decision_id` and `_intention_id` are injected. |
| `dag_create` (model) | `intent` argument; fills `original_request` | same injections |
| `schedule_task` (model) | `intent` argument → container | same injections |
| `spawn_sync`, inline `spawn_task` (model) | `intent` argument, `none` | same injections |

**The `_origin_*` arguments go only into the intention row (I5).** The dispatcher's existing `_session_id` and `_channel` injections, and what each tool does with them, stay exactly as they are; F098 #694's MCP `dag_create` fix included. In particular, `spawn_sync` still sets no `parent_session_id`, and `schedule_task` still leaves `created_by_session` empty.
| Scheduler fire | the schedule's task text | `origin_kind = scheduler`; new root under the container |
| Work queue → DAG (two creation sites) | the item title | `origin_kind = work_queue` |
| Companion `app.act` | the action label | `origin_kind = app_act`; the channel comes from the surface's recorded session |

**Plan decision id.** `TurnContext.decision_id` is set in `pre_turn`, after `ExecutionContext` (a frozen dataclass) has been built. The runner passes it to `dispatch()` through `dataclasses.replace(ctx, decision_id=…)` once `pre_turn` returns.

### 4.3 Results return to the intention

**Phase 1** (`NOUS_INTENTIONS_ENABLED` on, `NOUS_CONTINUATION_ENABLED` off). Routing is **identical to F098 Phase A**.
- Inbox rows gain a nullable `intention_id`.
- The writers move the intention `pending → closed` with `close_reason = 'legacy'`, because F098 or the legacy path is its consumer. The close happens before each writer's routing-key check, and inline runs close in `spawn_task` (§4.1, Closing).
- Nothing is stranded, and Phase 1 data shows the wake-policy mix and the intent quality.

**Phase 2** (`NOUS_CONTINUATION_ENABLED` on):

1. **`continue` results are keyed by intention only.**
   - Their inbox rows are written with `channel = NULL` and `session_id = NULL`, keyed by `intention_id`.
   - Every writer treats a non-null `intention_id` as a routing key: the worker hook, the DAG listener, the F087 backstop and the reconciler.
   - The DAG writer never substitutes the default chat for a `continue` DAG.
   - A chat turn's claim (`channel = X OR session_id = Y`) can therefore never take these rows.
   - A `continuation` turn's `pre_turn` also skips F098's generic inbox injection, and so does any session named `intent-…`.
   - The continuation runner reads the rows by `intention_id`. It stamps `delivered_at` and `delivered_session_id = 'intent-<root>'` **in the fenced commit** (§4.5), from `arrival.inbox_ids`, not when it reads them. A released lease or a rollback therefore still finds them undelivered, and F098's delivery-rate metric stays meaningful.
   - F098's reconciler DAG pass (`InboxDagPass`) selects a DAG when it has an origin **or** an open `continue` intention. A DAG spawned by a continuation has no origin session, and without the second condition its lost row would never be repaired.
2. **Same-transaction transition.** `ResultInboxStore.insert` accepts a session (or the write uses one `WITH t AS (UPDATE brain.intentions … RETURNING id) INSERT …` statement). The inbox row and the move from `pending`/`awaiting_owner` to `result_ready` then commit together.
3. **Re-arrivals.**
   - A DAG `retry_node` bumps `delivery_generation`, and a decided proposal or an owner answer produces a new result.
   - If the intention is `closed` and its root is still open, the writer reopens it to `result_ready`. Otherwise the result becomes an `intention_report` carrying the raw result.
   - A result that arrives while the intention is `deciding` is simply inserted. At the fenced commit, if the intention still has unconsumed rows (rows not in `arrival.inbox_ids`), it goes back to `result_ready` instead of closing.
4. **Owner-facing rows.**
   - Reports, questions and proposals are inbox rows with `source_kind = 'intention_report'` and `msg_type` `REPORT`, `QUESTION` or `PROPOSAL`.
   - They are keyed to `origin_channel`, or to `telegram:<NOUS_TELEGRAM_CHAT_ID>`.
   - Their `source_id` is a fresh `report_id`, generated per row. Some rows have no arrival (a re-arrival on a closed root, a rollback); an arrival that produces reports records their ids.
   - The Phase 2 migration widens migration 081's two CHECK constraints and adds `agent_id` to its UNIQUE key. `ResultInboxStore.insert`'s `on_conflict_do_nothing(index_elements=…)` changes with that key.
   - `metrics()` counts the new source kind.
5. **Duplicate pushes are suppressed.**
   - For a `continue` source, the subtask worker's raw Telegram push (suppressed inside `_notify_telegram`, which covers all four call sites) and the F087 Telegram leg are suppressed.
   - The F087 leg becomes `ok=False, required=False, detail='superseded_by_continuation'`.
   - Because that leaves no required leg, **a `continue` DAG is never marked delivered without its inbox row.** The reconciler's DAG pass (`InboxDagPass`, added by #694) repairs a missing row, keyed by `delivery_generation`. Its filter is extended to continuation DAGs (item 1).
6. **Rollback.** At startup with `NOUS_CONTINUATION_ENABLED` off, open `continue` intentions in `result_ready`, `deciding` or `awaiting_owner`, and `pending` ones whose source is already terminal:
   - have their undelivered inbox rows re-routed to `origin_channel` (or the default chat);
   - have their pending proposals expired (a later tap is refused);
   - are closed with `close_reason = 'legacy'`.

   This runs whenever `brain.intentions` exists, even if `NOUS_INTENTIONS_ENABLED` is off too. If `NOUS_RESULT_INBOX_ENABLED` is also off, re-routed rows would be invisible, so the raw result is sent by Telegram instead. Turning the flags off never strands a result (G6).

### 4.4 Tool surface and enforcement

**New `ContextKind`: `continuation`.**
- Its `CONTEXT_POLICY` row is `ContextPolicy(_LOCAL, spawn=frozenset({"spawn_task", "dag_create"}))`.
- `ExecutionContext` gains `intention_id`, `root_intention_id`, `authority` and `decision_id`.

**One helper builds the offered set for both loops:** `_offered_tools(ctx, frame_id, is_subtask, tool_filter, refuse_active)`, used by `_tool_loop` and `stream_chat`. For `authority = internal_only`:

| Rule | Tools |
|---|---|
| Allowed | Tools whose `tool_class(name)` is `none` or `write`. **Fail closed** on an unknown tool. |
| Denied, although `none`/`write` | `schedule_task`, `heartbeat_check_create`, `heartbeat_check_manage`, `create_censor`, `learn_skill`, `store_identity`, `complete_initiation`, `dag_manage`, `push_surface`, `compose_surface`, `bash`, `run_python`, `spawn_sync`, `resolve_decision`, `resolve_decisions` |
| Allowed with a per-call check | `write_file` only under `<workspace_dir>/intentions/<root_id>/`; `cancel_task` only on its own lineage |
| Spawn tools | `spawn_task` and `dag_create` are offered **only to `continuation` turns**, and are removed when the root reaches its depth or spawn limit. Other turns in the lineage get no spawn tools. |
| Never offered | Class `external` or `irreversible` (today: `send_email`, `send_file`) |
| Continuation turn only, as `extra_tools` | `propose_action`, `resolve_intention` |

**Dispatch enforcement** happens in `_authorize_tool_call`, the choke point for both loops. For `internal_only` it runs **before** the `policy_mode == "off"` early return, and it uses `force_block`.
- It refuses any call to a tool that was not offered, whatever `tool_offered_set_enforcement_mode` says. A forged `tool_use` for `send_email` is therefore refused, not merely logged.
- It refuses any call that `classify_side_effect` rates `external`.
- It enforces the `write_file` path rule and the `cancel_task` lineage rule.
- Dynamic checks in a lineage get their declared tools intersected with the allowed set.

**Extra-tool termination.**
- Today any successful `extra_tools` call ends the loop. That changes: only a tool flagged *terminal* ends it.
- `resolve_intention` and F061's `submit_final_report` are terminal, so hardened subtasks still stop after their report. `propose_action` is not terminal.
- Both new tools get `TOOL_CLASSES` entries (`write`).
- Continuation turns never pass `force_tool_on_penultimate`. 5.5-generation models reject a forced `tool_choice`, and forcing is off anyway whenever thinking is on. The follow-up in §4.5.5 replaces it.

**Proposals** are stored in `brain.intention_proposals`.

| Column | Meaning |
|---|---|
| `id`, `agent_id`, `intention_id`, `root_id`, `arrival_id` | Keys. |
| `tool`, `arguments` | The exact call, as JSONB. |
| `rationale` | Why. |
| `state` | `pending`, `approved`, `rejected`, `expired`, `executed` or `failed`. |
| `deadline` | When it expires. |
| `ledger_key` | The execution-ledger key. |
| `decided_at`, `executed_at`, `result` | Outcome. |

1. `propose_action(tool, arguments, rationale)` only **records** a proposal. It validates that `tool` is registered and is **not in the internal allowed set**. That means any outward tool, or a denylisted local tool such as `schedule_task` or `bash`; the owner sees the exact call either way. A turn that proposed anything must resolve with `ask`, and the runner enforces this.
2. The owner sees the root intention, the proposed call and the rationale in a Telegram message with inline **Approve / Reject** buttons, and, when A2UI is on, in a companion card with the same two options. Each proposal has a short id.
3. **Approval is a deterministic owner action. It never passes through a model.** It can be:
   - a companion card tap;
   - a Telegram inline-button callback;
   - a bot command parsed by code (`/approve <id>`, `/reject <id>`).

   The Telegram bot handles these itself, before anything reaches the agent, and calls `POST /intentions/proposals/{id}/decide`. **No agent tool can approve, reject or answer.** No model-callable approval path exists. (An `owner`-authority chat turn that has `bash` could still call the REST route with `curl`, but that adds no capability it lacks today: it can already send outward directly.) The REST route has the same no-auth LAN posture as the rest of the API (§9).
4. **The default at the deadline is reject**: an expired proposal never runs.
5. On approve, the runner method `execute_approved_proposal(proposal_id)` runs exactly the stored `(tool, arguments)`, with no model in between:
   - **At most once, for every tool.** The fence is the proposal row's conditional transition `approved → executing`, made before the call. A crash while `executing` leaves a visible in-doubt proposal that is never re-run automatically.
   - **Context.** The call runs under `ContextKind` **`approved_action`**, whose `CONTEXT_POLICY` row admits exactly the one declared tool (`declared_tools = (tool,)`). It passes through `_authorize_tool_call` and the execution ledger's open/close (`_open_for_call`) like any loop call, so it is recorded. The ledger keys the call with a new `proposal:{id}` idempotency scope in `api/idempotency.py`.
   - **Authority.** This is the `owner_approved` authority: one call, the one the owner saw. It is the only way an outward tool runs on behalf of a lineage.
6. The execution result, a rejection or an expiry becomes **the next result of every intention in the arrival that made the proposal** (`awaiting_owner → result_ready` for each). The proposal records its `arrival_id`, and the arrival lists its `intention_ids`, so the next claim takes them together as one batch, and the chain continues.

**Questions** (`ask` with no proposal) are an `intention_report` of type `QUESTION`.
- The owner answers by replying to the question's Telegram message (the runner stores the pushed message's `message_id` on the report row, so the bot can map a reply to the intention), or with `/answer <id> <text>`. The bot recognises either in code and calls `POST /intentions/{id}/answer`.
- When A2UI is on, the question also shows as a card with fixed options.
- The answer arrives as the next result of every intention in the asking arrival (the report row carries the `arrival_id`), so the next claim takes them together. The model never routes the answer, so text inside a result cannot pose as the owner's answer.

### 4.5 Arrival pipeline

**1. Trigger.**
- Moving to `result_ready` emits `intention.result_ready` on the bus. This is a wake-up hint only, since the bus drops events when its queue is full.
- A reconciler pass (60 s, bounded) only enqueues and wakes the runner. It never runs a turn, because reconciler passes have a 30 s timeout.
- The same pass repairs `continue`/`report` intentions whose source is terminal but whose inbox row is missing.
- The continuation runner is one loop. It computes when the next root becomes eligible and sleeps until then, rather than sleeping once per event. It is registered with the Fix-Z maintenance-loop guard.

**2. Claim**, one transaction under READ COMMITTED, and only when a concurrency slot is free (an in-process semaphore, `NOUS_CONTINUATION_MAX_CONCURRENT`):

```sql
SELECT 1 FROM brain.intentions WHERE agent_id = :agent AND id = :root FOR UPDATE;  -- per-root mutex
UPDATE brain.intentions i
   SET state = 'deciding', claimed_at = now(), claim_token = :token, updated_at = now()
 WHERE i.agent_id = :agent AND i.root_id = :root AND i.state = 'result_ready' AND i.wake_policy = 'continue'
   AND NOT EXISTS (SELECT 1 FROM brain.intentions d
                    WHERE d.agent_id = :agent AND d.root_id = :root AND d.state = 'deciding')
   AND (   (SELECT max(result_at) FROM brain.intentions
             WHERE agent_id = :agent AND root_id = :root AND state = 'result_ready')
             <= now() - make_interval(secs => :debounce)
        OR (SELECT min(result_at) FROM brain.intentions
             WHERE agent_id = :agent AND root_id = :root AND state = 'result_ready')
             <= now() - make_interval(secs => :max_wait))
RETURNING i.*;
```

- **Lease.** On each sweep and at startup, any `deciding` row older than the lease goes back to `result_ready` with `attempts + 1` and `claim_token = NULL`.
- **The turn cannot outlive its lease.** It runs under `asyncio.wait_for(…, NOUS_CONTINUATION_TURN_TIMEOUT_SECONDS)`, and startup validation requires the turn timeout to be shorter than the lease by at least 60 s. A turn that times out counts as a failed attempt. A released claim can therefore never still have a live turn spawning under it.
- **Fenced commit.** The decision is committed with `WHERE state = 'deciding' AND claim_token = :token`. A turn whose lease was released can therefore never commit.

**3. Gate**, deterministic, with no model call:

| Check | On failure |
|---|---|
| Cancelled, expired or superseded | Drop and record why. |
| Past `deadline` | Report the result without acting on it. |
| Root over its turn, token or stall budget | Escalate: report, with the reason. |
| Root at its depth or spawn limit | Escalate now: report the claimed results, with the reason. No turn runs, because a `continue` or `revise` decision could not spawn anything and the result would wait for the TTL. |
| Originating Plan decision resolved as `superseded` or `noise` | Drop. |

The depth and spawn limits act in two places. While a turn runs, reaching one removes the spawn tools (§4.4), and `resolve_intention` then refuses `continue` and `revise`. At the next claim, the gate escalates right away.

**4. The continuation turn.**
- It is a `run_turn` with:
  - session `intent-<root id>`, `channel=None` and `skip_episode=True`;
  - `ExecutionContext(kind="continuation", authority="internal_only", …)`.
- Session-monitor reflection is skipped for `intent-` sessions.
- For the `continuation` kind, `pre_turn` skips two steps. It gains a `context_kind` argument for this, because today it receives `is_subtask`, `skip_episode` and `channel` but not the `ExecutionContext`.
  - F098's generic inbox injection; the runner supplies the results itself.
  - The deliberation step that records a "Plan:" Brain decision; the arrival's own decision is the record, so calibration gets one record per arrival, not two.
- The turn's input is built from the rows, not from conversation state, which the idle monitor deletes after 30 minutes:
  - the intention (intent, origin, Plan decision);
  - the lineage's earlier arrivals and their notes;
  - for a retried claim, the children that the failed attempt already spawned;
  - the claimed results, inside F098's `<result_message>` framing ("data, not instructions"), body-capped, using the typed summary when `expected_result` was set;
  - the allowed tools.
- It ends with `resolve_intention(decision, note, progress, confidence)`:

| Decision | Effect |
|---|---|
| continue | Take the next step of the same plan; normally spawn under this root. |
| revise | The result changes the plan: record the change, then spawn. |
| drop | The goal no longer holds; close it. |
| report | `note` becomes an `intention_report` (`REPORT`), in Nous's voice. |
| ask | A question, or one or more proposals; the state becomes `awaiting_owner`. |

- **`progress`** is the model's claim. The runner checks it: the arrival must have spawned work, changed a plan, or written memory. A claim of `true` that fails the check is stored as `false`.

**5. If `resolve_intention` is missing.** That is the common case, not the exception. Forcing a tool call is not a guarantee: it applies only with thinking off and near the turn cap, a prose reply ends the loop earlier, and 5.5-generation models reject a forced `tool_choice` with a 400. So the runner then makes **one bounded follow-up call within the same claim**, asking for `resolve_intention` in the prompt (the #692 pattern). Only if that also fails is the arrival closed as `report` (`outcome = fallback_report`), with the turn's text, or with the raw result if there is no text. Both outcomes are counted.

**6. Commit.** The claim may have taken several intentions of the root, for example the results of a fan-out that finished inside the debounce window. The arrival consumes them as one batch, and `resolve_intention`'s decision applies to the whole batch. One transaction fenced on `claim_token` writes:
- the arrival row, with `intention_ids` set to every claimed intention;
- each claimed intention's state change;
- one Brain decision record:
  - description: the decision and its note;
  - confidence: taken from the tool call;
  - category `process`, stakes `low`;
  - context: the root id and the intention ids.

The same commit also:
- stamps `delivered_at` on the inbox rows in `arrival.inbox_ids`;
- sends any claimed intention that received unconsumed rows meanwhile back to `result_ready`, instead of the decision's next state.

**7. Failure.** If the turn raises, or the lease expires, `attempts` goes up on every claimed intention. After `NOUS_CONTINUATION_MAX_ATTEMPTS` (default 3), the claimed intentions close as `report` with their **raw results** (`outcome = failed_report`).

**8. Quiet hours.**
- `HeartbeatRunner._in_quiet_hours` is extracted into a module function. It compares UTC hours, so prod's settings must be in UTC.
- Owner-facing rows are written immediately, so chat sees them at night.
- Only the Telegram push is deferred: `push_after` and `pushed_at` columns on the row, plus a sweep. The push is idempotent, keyed by the row id.
- Internal continuation runs at any hour.

### 4.6 Bounds and cancel

| Setting | Default | On reaching it |
|---|---|---|
| `NOUS_CONTINUATION_MAX_DEPTH` | 3 | Spawn tools removed, and `continue`/`revise` refused for the rest of the turn; the gate escalates the next claim at once. |
| `NOUS_CONTINUATION_MAX_SPAWNS_PER_ROOT` | 12 | Same as depth. |
| `NOUS_CONTINUATION_MAX_TURNS_PER_ROOT` | 8 | Escalate. |
| `NOUS_CONTINUATION_MAX_TOKENS_PER_ROOT` | 400000 | Escalate. Root tokens = the lineage's subtask `tokens_in/out` **excluding DAG-node subtasks** (their usage is already rolled into `tokens_consumed` by `DAGStore.claim_and_add_node_tokens`) + its DAGs' `tokens_consumed` + its arrivals' tokens. Phase 2 also adds DAG check-node usage to `tokens_consumed` (today `HeartbeatRunner._tick` counts a dynamic check's tokens only in its daily total), so a repeatedly running lineage check uses up the root budget. |
| `NOUS_CONTINUATION_STALL_LIMIT` | 2 consecutive verified `progress=false` | Escalate. |
| `NOUS_INTENTION_ROOT_TTL_HOURS` | 72 | Report what exists, then close. |
| `NOUS_CONTINUATION_MAX_CONCURRENT` | 2 | Wait for a slot. |
| `NOUS_CONTINUATION_DEBOUNCE_SECONDS` / `_MAX_WAIT_SECONDS` | 20 / 120 | Batching window, and the anti-starvation cap on it. |
| `NOUS_CONTINUATION_LEASE_SECONDS` | 900 | A stale claim is released. |
| `NOUS_CONTINUATION_TURN_TIMEOUT_SECONDS` | 780 | The turn is cancelled and counts as an attempt. Must be at least 60 s below the lease. |
| `NOUS_CONTINUATION_MAX_ATTEMPTS` | 3 | Report the raw result. |
| `NOUS_INTENTION_PROPOSAL_TTL_HOURS` | 24 | The proposal expires, which counts as a reject. |

All budgets are derived from rows when checked; there are no counters to drift.

**Cancel.**
- The owner can cancel through `POST /intentions/{root}/cancel`, the dashboard, or a companion "Active intentions" card.
- A cancel does the following:
  - always writes `root_cancelled_at` on the root row;
  - moves every open intention of the root to `cancelled`;
  - cancels the lineage's pending subtasks and DAGs;
  - expires pending proposals;
  - deactivates a container's schedule;
  - **stops a running continuation turn.** The runner keeps a map from root to its running turn task (one process per agent) and cancels that task. `_authorize_tool_call` also refuses every call from an `internal_only` context whose root has `root_cancelled_at` set. That check reads the root row and caches it for the turn, invalidated by the cancel. Lineage subtasks already running on workers are not pre-empted, but their next tool call is refused and the gate drops their late result.
- **TTL.** The continuation runner's sweep handles roots past their TTL that have a `continue` or `report` intention: it writes `root_expired_at`, reports what exists and closes them. Containers are excluded. This also covers roots that never get an arrival, so no gate ever runs for them.
- Child inserts check that the root is open (I1), so a turn already running cannot spawn under a cancelled root.
- `GET /intentions` lists open roots with their lineage and budget use. It has the same no-auth LAN posture as the rest of the REST API.

### 4.7 Observability and calibration

**Dashboard.**
- An intentions view shows each root as a tree, with every arrival's decision, note and progress, and the budget used.
- The Ledger view's hard-coded list of context kinds gains `continuation`.

**Metrics.**
- Arrivals by decision and by outcome (`resolved`, `fallback_report`, `failed_report`).
- Time from result to decision.
- Results lost; the target is 0.
- Proposals approved, rejected, expired and executed.
- Escalations by cause.
- Stalls.
- Continuation token spend.

**Calibration.** Each arrival's Brain decision is graded through the existing decision reviewer. The signals are:
- a proposal approved or rejected;
- a root cancelled;
- the chain reaching its goal or being dropped.

These decisions are the evidence for any later widening of autonomy.

## 5. Settings

| Variable | Default | Phase |
|---|---|---|
| `NOUS_INTENTIONS_ENABLED` | `false` | 1. Requires `NOUS_RESULT_INBOX_ENABLED`; startup logs a WARNING and stays off without it. |
| `NOUS_CONTINUATION_ENABLED` | `false` | 2. Requires `NOUS_INTENTIONS_ENABLED`. Proposal cards need A2UI; without it, proposals and questions go by Telegram and chat only. |
| `NOUS_CONTINUATION_*`, `NOUS_INTENTION_*` | §4.6 | 2 |

- Each setting gets its row in `docs/reference/environment-variables.md`.
- Prod's compose passes variables explicitly, so each flag needs its own compose line.

## 6. Data and migrations

**Numbering.** Migrations are numbered after F098's 081 (inbox) and 082 (result memory log): the next free number when each phase lands. They are additive, idempotent and agent-scoped.

**Phase 1:**
- `brain.intentions`, with indexes `(agent_id, state)` partial on open states, `(agent_id, root_id)`, and the UNIQUE source key;
- `heart.result_inbox.intention_id`.

**Phase 2:**
- `brain.intention_arrivals` and `brain.intention_proposals`;
- the inbox CHECK and UNIQUE changes (§4.3.4), plus `push_after`, `pushed_at` and `push_message_id`.

**In the same PR as each phase:** update `tests/test_database.py::test_all_tables_exist` and the CLAUDE.md table count.

`heart.result_inbox.wake_attempted_at` (from F098 Phase B) stays and is unused.

## 7. Testing

**Phase 0**

The F098 A and C classification of scheduled, inline and spawn rows is pinned, and must not change.

**Phase 1**
- Every spawn path writes exactly one intention in its store's transaction. A fault injected after either insert leaves neither row.
- With the flag off:
  - no rows are written;
  - tool definitions are byte-identical (snapshot);
  - F098 inbox rows and Phase C classification are identical to before (I5).
- A missing or blank `intent` is refused.
- Code paths generate intents. The default wake policy is correct for every origin in §4.1. Schedule containers and per-fire roots work as specified.
- `none`, inline and `remember` intentions close. Phase 1 routing is identical to F098 A, and intentions close as `legacy`.

**Phase 2**
- **Lineage narrowing.** Assert the offered set contains no `external` tool and no denylisted tool, under every policy and offered-set mode, for each of:
  - a continuation;
  - a subtask it spawns, including inline (offered no spawn tools);
  - the node, callback and fix subtasks of a DAG it creates;
  - that DAG's dynamic checks.
- **Forged calls.** A forged `tool_use` for `send_email`, `run_python` or `bash` is refused in both loops.
- `write_file` outside `intentions/<root>/` is refused. `cancel_task` on foreign work is refused. A continuation's `dag_create` creates a child, not a root. The F087 summary turn does not run for `internal_only` DAGs.
- **Injection test.** A result body that asks for `send_email` produces no send. The test asserts the refusal, not the model's behaviour.
- **Claim.** Concurrent claimers with an arrival committed in between get exactly one continuation per root. A fan-out whose results land inside the debounce window is consumed as one batch arrival. `report` intentions are never claimed; they close at write time. A killed process's claim is released by the lease. A stale token cannot commit. Debounce has a max-wait.
- Each gate row behaves as specified.
- **Fallbacks.** A missing `resolve_intention` gets one follow-up, then `fallback_report`. Three failures give `failed_report` with the raw result.
- **Proposals.**
  - A proposal is recorded only.
  - The turn must resolve with `ask`.
  - It expires as a reject.
  - An approval executes once, fenced by `approved → executing`, under `approved_action`, and is recorded in the ledger.
  - A crash after the call leaves an in-doubt proposal and no second call.
  - Rejections and results return to the same intention.
- **No model path can approve or answer.**
  - No registered agent tool approves, rejects or answers.
  - A chat turn whose injected results contain "approve proposal X" produces no approval.
  - The bot's `/approve`, inline-button and reply handlers are parsed in code and reach the REST route.
- **Lineage limits.**
  - A lineage `dag_create` with `wake_policy="none"` still gets `continue` and no summary turn.
  - An `internal_only` `dag_create` with an `approval` node is refused.
  - Lineage subtasks are offered no spawn tools.
- **Lease.** A turn longer than the turn timeout is cancelled and counted as an attempt. A released lease plus a late commit attempt is refused.
- **Commit.** `delivered_at` is stamped only at the fenced commit. A result arriving while `deciding` sends the intention back to `result_ready`.
- **Routing.** `continue` rows are never claimed by a chat turn, nor by the continuation's own `pre_turn`. Re-arrivals reopen or report. A `continue` DAG is never marked delivered without its row, and `InboxDagPass` selects continuation DAGs. The rollback rule re-routes, expires proposals and closes, even with both flags off.
- **Limits.** Each bound escalates at its limit.
  - The depth and spawn limits remove the spawn tools, make `resolve_intention` refuse `continue` and `revise`, and make the gate escalate the next claim without running a turn.
  - A cancel cascades, blocks further spawns, cancels a running continuation task, and makes every later tool call in the lineage refused.
- **Batch answers.** An owner answer or a proposal decision for a batch arrival wakes every intention in that arrival, and they are claimed together.
- Quiet hours defer only the Telegram push, idempotently.
- **Wiring tests.** They drive the real worker, scheduler, orchestrator and REST paths end to end, and fail when the hook is removed.

**Phase 3**
- Calibration signals reach the decision reviewer.
- The dashboard endpoints return the tree.

## 8. Phases

| Phase | Content | Flag | Precondition |
|---|---|---|---|
| 0a | **Carry the reason**, storage only and routing-neutral (I5): `dag_create` and the work queue fill `original_request`; the Plan decision id is carried into **subtask** metadata. (`execution_dags` has no metadata column; a DAG's decision id waits for its intention row in Phase 1.) | none | #694 merged |
| 0b | **Carry the result**: heartbeat callbacks receive their check's findings; DAG check nodes store their findings as the node result. | none (a behaviour fix with its own tests and review) | #694 merged |
| 1 | §4.1–4.2 and §4.3 Phase 1: intentions, capture on every path, lineage stamps, `legacy` closing. | `NOUS_INTENTIONS_ENABLED` | 0a |
| 2 | §4.3 Phase 2 routing, §4.4–4.6. | `NOUS_CONTINUATION_ENABLED` | 1 |
| 3 | §4.7 dashboard, metrics and calibration; an assumption re-check if the data calls for it. | – | 2 |

**Rollout.**
1. Turn on Phase 1 and read a week of intentions: the wake-policy mix and the quality of the intent lines.
2. Turn on Phase 2 and watch the decision and outcome mix and the results lost, which must stay 0, for a week.
3. Only then discuss widening autonomy.

## 9. Risks and accepted residuals

- **`web_fetch` and `web_search` in an internal-only chain (accepted for v1).**
  - The risk: untrusted text could steer a fetch URL or a search query that carries private data. Subtasks have the same exposure today.
  - Mitigation: fetches and searches in an `internal_only` lineage are logged with their root.
  - If this ever needs closing: a lineage may either fetch from the web or read private memory, not both.
- **Ahead of the evidence.** No benchmark measures an agent waking on its own result in a later turn. The decision rule therefore stays in code (gate, states, bounds, lease, fenced commit). The model is asked one narrow question, and `report` is the fallback on every failure path.
- **The REST routes have no auth (existing posture, accepted).**
  - The risk: approval, answer and cancel go through REST routes with the same no-auth LAN posture as `/chat` and the rest of `nous/api/rest.py`. A client on the LAN could approve a pending proposal.
  - Mitigations: proposals are short-lived (24 h) and owner-visible, and every execution is recorded.
  - Shared-token auth for these routes, or for all of REST, is a separate change.
- **Cost.** A root is bounded to 8 continuation turns and 400k tokens. The `continue` population is chat work plus continuation lineages, not the roughly 937 background monitors a month, which default to `none`.
- **Out of scope, found during review (latent).** F061's penultimate `tool_choice` forcing in `_tool_loop` would hit the same 400 on 5.5-generation models that #692 fixed for background calls. It is latent today: forcing only fires when thinking is off (`thinking_mode == "off"`, or a Haiku model), and a deployment running adaptive thinking never forces. The same fact is why §4.5.5 cannot rely on forcing at all.

## 10. Research notes (sources)

- **The handle and the record are the truth; a push is only a hint.**
  - MCP 2026-07-28 moved tasks into an extension and says clients should persist task ids durably.
  - A2A 1.0 sends a push notification, then expects a `GetTask` fetch.
  - Postgres `NOTIFY` is only a wake-up and needs a sweep behind it.
- **Commit, and reconsider only on relevant events.** Kinny & Georgeff (IJCAI-91) and Schut & Wooldridge (2001) found this beats both blind commitment and re-planning on every event.
- **Prospective memory is the bottleneck.** PM-Bench (2026) best is 65.1%; enforcing the intention's lifecycle in code took a 2B model from 4.2% to 66.2%.
- **Agents treat updates as context rather than revisions** (ClawArena-Team, 2026), and **over-trigger** when proactive (ProactiveBench).
- **Containment has to be structural.**
  - Meta's "Agents Rule of Two" (2025).
  - Design patterns for securing LLM agents against prompt injection (2025): plan-then-execute, action-selector.
  - DeepMind's CaMeL: capabilities and information flow.
  - OWASP LLM06: excessive agency.
- **Per-call caps do not bound a chain.** Claude Agent SDK budgets are per session. Magentic-One's progress ledger, which escalates after a stall, motivates the stall limit.
