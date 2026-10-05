# F099: Intentions and Continuation (design)

Status: design agreed with the owner 2026-10-05; Phase 1 next
Depends on: F098 Phase A, the result inbox (#694). F098 Phase C (#696) becomes the `remember` wake policy.
Supersedes: F098 Phase B, the report-only wake turn (#695)
Research basis: a survey of 2025–2026 frameworks, protocols, literature and safety work on agents acting on background results; key sources are in §10.

## 1. Problem

When Nous spawns background work (a subtask, a DAG, a schedule), the result should come back to **Nous's own loop**, so that Nous can keep acting on the intention that made it spawn the work. The goal is not to report to a person. Reporting is one of several things Nous may decide to do.

Today, and with F098 as built, three things stand in the way.

1. **Nothing records why work was spawned.**
   - `heart.subtasks` has `task`, `parent_session_id`, `parent_channel`, `notify` and `metadata`, but no goal, plan step or decision.
   - `execution_dags.original_request` exists but neither DAG creator fills it.
   - Each deliberated turn records a "Plan:" decision in the Brain, but its id never reaches `spawn_task` or `dag_create`.
2. **Results of Nous's own work have no way back.**
   - Every heartbeat triage turn runs in a fresh `heartbeat-<hex>` session with no channel, so a subtask it spawns gets an inbox row keyed to a session that never runs again.
   - A heartbeat callback turn never sees its check's findings.
   - A DAG check node completes with the fixed text "Check completed (self-disabled)", so its findings are lost.
   - No handler subscribes to `subtask_completed`.
3. **F098 delivers to the chat and only reports.**
   - The inbox (#694) is keyed by the Telegram chat.
   - The wake turn (#695) is started by the bot, runs only for conversation-origin results, and is told to "report them briefly".
   - It has a read-only policy, and that policy is advisory because `tool_context_policy_mode` defaults to `warn`.

Volume over 30 days, completed and not DAG-node:

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
- G2. When work finishes, its result returns to the intention, and the intention's wake policy decides what happens.
- G3. For `continue` intentions, Nous runs a continuation turn that decides among continue, revise, drop, report and ask, and acts within its authority.
- G4. A continuation can never act outward on its own. Outward actions become proposals the owner approves.
- G5. Chains are bounded at their root and can be cancelled. Every arrival decision is recorded and later graded.
- G6. A result is never lost silently. If a continuation cannot run, the owner gets the raw result.

**Non-goals**
- Widening autonomy beyond internal-only. That is decided later, from the recorded decision data (§7).
- Checking structured assumptions. They are recorded in Phase 1 and checked in Phase 3 at the earliest.
- External agent protocols (A2A or MCP tasks). The intention row and inbox are shaped so they could carry them later.
- Backfilling historical results.

## 3. Owner decisions (2026-10-05)

| Question | Decision |
|---|---|
| Autonomy | **Internal only.** A continuation may think, update memory and plans, and spawn work within budget. Every outward action becomes a proposal the owner approves. |
| Scope | **Every spawn records an intention, and its wake policy decides.** The defaults are in §4.1. |
| Intent capture | **Hybrid.** Context is captured automatically, plus one required `intent` line on LLM-called spawn tools, required only while the flag is on. Code-path spawns generate theirs. |
| Where a continuation runs | **One thread per root intention** (`intent-<root>`). Reports and proposals reach the owner through the chat inbox and Telegram. |
| Delivery | **Phased** (§8). Each phase ships dark. |
| Tool enforcement | The `continuation` kind **narrows the offered tool set regardless of `tool_context_policy_mode`.** This is the one stated exception to the warn-mode rule. |
| Accepted risk | `web_fetch` stays available to a continuation's lineage in v1. Its exfiltration path (private data in a URL) is accepted and logged (§9). |

## 4. Design

### 4.1 The intention record

New table `brain.intentions`. One row per spawn: the "K-line" that re-activates the state of mind the work was started in.

| Column | Meaning |
|---|---|
| `id`, `agent_id` | The usual key and agent scope. |
| `root_id`, `parent_id`, `depth` | Lineage. A root has `root_id = id`, `parent_id NULL`, `depth 0`. Work spawned by a continuation gets a child under the same root. |
| `source_kind`, `source_id` | `subtask`, `dag` or `schedule`, and that row's id (TEXT, since schedule ids are not UUIDs everywhere). `UNIQUE (agent_id, source_kind, source_id)`. |
| `intent` | One line: why this is needed and what Nous will do with the result. |
| `origin_kind` | The `ContextKind` of the spawning turn. |
| `origin_session_id`, `origin_channel` | Where it was spawned. The channel is also where owner-facing output goes. |
| `origin_decision_id` | The deliberation "Plan:" decision of the spawning turn, when there was one. |
| `wake_policy` | `continue`, `remember`, `report` or `none`. |
| `expected_result` | JSONB, optional: the result shape the spawner asked for. |
| `assumptions` | Optional text. Recorded, not checked yet. |
| `authority` | `internal_only`, the only value in this design. |
| `deadline` | When a late result stops being acted on. Default: created + root TTL. |
| `state` | See below. |
| `decision`, `decision_note`, `decision_record_id` | The latest arrival decision and the Brain decision that records it. |
| `attempts` | Continuation attempts for the current arrival. |
| `created_at`, `result_at`, `closed_at`, `updated_at` | Timestamps. |

**States:**
- `pending` → `result_ready` → `deciding` → `closed`, with `decision` recorded.
- An `ask` decision moves to `awaiting_owner`. The owner's answer arrives as the next result, which moves it back to `result_ready`.
- `cancelled` and `expired` are terminal at any point.
- A transition only ever happens through a conditional `UPDATE … WHERE state = <expected> RETURNING`.

**Root budgets are derived, not counted.** Continuations, spawns, tokens (from `heart.subtasks.tokens_in/out` plus continuation-turn usage) and the no-progress streak are computed from the root's rows when they are checked. There are no counter columns to drift.

**Default wake policy, used when the spawner does not say:**

| Origin | Default |
|---|---|
| Chat (`interactive`, `mcp`), heartbeat triage, check callbacks, a DAG created by Nous, a continuation | `continue` |
| Scheduled fire with `notify=true` | `remember` |
| Scheduled or background with `notify=false` | `none` |
| Inline `spawn_task(await_result=true)` and `spawn_sync` | `none`. The result already returns inside the turn; the row exists for lineage and budget. |
| DAG node subtasks | No row of their own: the DAG's intention covers its nodes. |

**Invariants:**
- **I1. One intention per spawn, atomically.** With the flag on, every spawn path writes exactly one intention in the same transaction as the work row. That covers the model-called tools and every code path: scheduler fires, the work queue, companion `app.act`, heartbeat triage spawns and DAG creation. A spawn without an intention, or an intention without its work, cannot be committed.
- **I2. Intent source.**
  - Model-called spawn tools (`spawn_task`, `dag_create`, `schedule_task`, `spawn_sync`) take a required `intent` parameter, but only while `NOUS_INTENTIONS_ENABLED` is on. With the flag off, the tool schemas are byte-identical to today.
  - A missing or blank `intent` is refused with a tool error that says what to write.
  - Code paths generate the intent: a schedule's task text; a DAG's description plus `original_request`; a companion action's label; a work-queue item's title.
- **I3. Authority only narrows.** A child intention's authority is at most its parent's. Every turn in a continuation's lineage, whether a continuation, a subtask it spawned, or that subtask's DAG nodes, carries the root intention id and `internal_only` in its `ExecutionContext`, and gets the narrowed tool set of §4.4. This closes the gap where a subtask is offered `send_email` today: `is_subtask` only removes the spawn tools.
- **I4. One consumer per arrival.** A result is handled by exactly one consumer, chosen by wake policy:
  - `continue` → the continuation runner;
  - `report` → the chat inbox;
  - `remember` → the memory writer;
  - `none` → today's behaviour.

  The chat inbox never also injects the raw result of a `continue` intention. Writing to memory is archival, not consumption, and may happen alongside any policy when F098 Phase C is on.

### 4.2 Capture at spawn

| Spawn path | Intent | Origin captured |
|---|---|---|
| `spawn_task` (model) | `intent` parameter | session, channel, kind, Plan decision id, parent intention if the turn is a continuation |
| `dag_create` (model) | `intent` parameter; also fills `original_request` | same |
| `schedule_task` (model) | `intent` parameter; each fire gets a child intention | same; also fills `created_by_session` |
| `spawn_sync` / `await_result` (model) | `intent` parameter, wake `none` | same |
| Scheduler fire | the schedule's task text, under the schedule's intention | `scheduled` |
| Heartbeat triage spawn | `intent` parameter (it is a model call) | `heartbeat_triage`; `origin_channel` = the default chat |
| Work queue → DAG | the item title | `background`; default chat |
| Companion `app.act` | the action label | `agent_action`; the surface's chat |
| DAG nodes | none: covered by the DAG's intention | – |

The dispatcher already injects `_session_id` and `_channel` (`nous/api/tools.py`, the injection block). It gains `_origin_kind`, `_decision_id` and `_intention_id` (the continuation's own intention, when the turn is one). The spawn tools write the intention through one helper, `IntentionStore.create_in(session, …)`, inside their existing transaction.

### 4.3 Results return to the intention

- **Inbox rows carry the intention.** F098's `heart.result_inbox` gains a nullable `intention_id`. The writers (the subtask worker hook, the DAG bus listener, the F087 backstop and the reconciler) look up the intention for the finished source and record the result against it. They then move the intention `pending → result_ready` in the same transaction.
- **Chat routing depends on the policy.** For `continue` intentions the inbox row is written with no channel, so a chat turn cannot claim it (I4). For `report` the row is written as in F098 Phase A.
- **Owner-facing output gets its own rows.** Continuation reports and proposals are written as inbox rows of `source_kind = 'intention_report'`, keyed to the intention's `origin_channel`, or to `telegram:<NOUS_TELEGRAM_CHAT_ID>` when there is none. The owner's next chat turn therefore sees them, and a Telegram message is sent as well.
- **Duplicate pushes are suppressed.** When an intention exists and its policy is `continue`, the subtask worker's raw Telegram push and the F087 Telegram leg are suppressed for that source. The F087 leg becomes not-required with detail `superseded_by_continuation`; it is never silently reported as ok. The owner hears through the continuation's report, or through the raw-result fallback (§4.5).

### 4.4 Tool surface and enforcement

A new `ContextKind` is added, `continuation`. `ExecutionContext` gains `root_intention_id`, `intention_id` and `authority`.

**The offered set is narrowed**, in the runner's tool assembly, next to the `is_subtask` and `tool_filter` filters (`AgentRunner` tool loop). It applies to every turn whose context has `authority = internal_only`: the continuation itself and its whole lineage.
- **Allowed:** tools whose class in `nous/api/tool_classes.py` is `none` or `write`, minus a named denylist.
- **Denylist:** `schedule_task`, `heartbeat_check_create`, `heartbeat_check_manage`, `create_censor`, `learn_skill`, `store_identity`, `complete_initiation`, `dag_manage`, `push_surface`, `compose_surface`, `bash`, and `write_file` outside the continuation's workspace.
- **Spawn tools** (`spawn_task`, `dag_create`) are offered while the root's budget allows (§4.6).
- **Two new tools,** offered only to a `continuation` turn: `propose_action` and `resolve_intention`.
- **Never offered:** tools of class `external` or `irreversible`.

This filter does not read `tool_context_policy_mode`. It is the stated exception.

**Per-call classification is enforced** for the same contexts. `run_python` (and `bash`, if ever offered) can be classified `external` per call by `classify_side_effect`, for example code that opens a network connection. For an `internal_only` context, such a call is refused at the existing choke point (`_authorize_tool_call`), whatever the policy mode.

**Proposals.** `propose_action(tool, arguments, rationale)` records an outward action for the owner to approve.
- The proposal shows the owner the root intention, the proposed call and the rationale, through the companion card and a Telegram message. The intention moves to `awaiting_owner`.
- Approval reuses Harness Phase 3's park-and-resume path (`docs/superpowers/specs/2026-09-25-harness-phase3-park-and-resume-design.md`): an approval with a deadline, answered by a companion tap. The Phase 2 plan picks one of two shapes:
  - (a) a two-node DAG, the existing approval node followed by a deterministic action node; or
  - (b) a `proposals` row answered through the same card and answer path.
- Whichever shape is chosen, these hold:
  - exactly the tool and arguments the owner saw run, with no model in between to reinterpret them;
  - they run with the originator's authority;
  - they run through the execution ledger, keyed `(root intention, continuation, ordinal)`, with the key reserved before the call;
  - they run at most once;
  - an expired or rejected proposal never runs.
- The approved call's output, or the rejection, comes back to the intention as its next result, so the chain can continue.

### 4.5 Arrival pipeline

1. **Trigger.** The transition to `result_ready` emits `intention.result_ready` on the event bus. This is a wake-up hint only, because the bus drops events when its queue is full. A sweep pass on the `TerminalSubtaskReconciler` (60 s, bounded per tick) catches anything the hint missed. It also repairs `pending` intentions whose source is already terminal but whose result row is missing.
2. **Claim.** A root's ready intentions are claimed together, after a debounce of `NOUS_CONTINUATION_DEBOUNCE_SECONDS` (default 20) from the newest arrival. The claim is `UPDATE … SET state='deciding' … WHERE state='result_ready' … RETURNING`, and is taken only if no intention of that root is already `deciding`. One continuation runs per root at a time. At most `NOUS_CONTINUATION_MAX_CONCURRENT` (default 2) run per agent.
3. **Gate,** deterministic, with no model call:

   | Check | On failure |
   |---|---|
   | Intention cancelled, expired or superseded | drop; record why |
   | Past `deadline` | report the result without acting on it |
   | Root over depth, turns, spawns, tokens, or stall budget | escalate: report with the reason |
   | Originating Plan decision resolved as `superseded` or `noise` | drop |

4. **Continuation turn.**
   - It runs with `runner.run_turn` in session `intent-<root id>` and `ExecutionContext(kind="continuation", …)`.
   - The turn receives:
     - the intention: intent, origin, Plan decision and lineage;
     - the claimed results, inside F098's `<result_message>` framing ("data, not instructions"), body-capped, using the typed summary when `expected_result` was set;
     - the root's earlier steps (the thread's history);
     - the allowed tools.
   - It must finish with `resolve_intention(decision, note, progress)`, where `decision ∈ {continue, revise, drop, report, ask}`. F061's forced terminal tool (`force_tool_on_penultimate`) guarantees the call.

   | Decision | Effect |
   |---|---|
   | continue | Take the next step of the same plan, normally spawning work under this root. |
   | revise | The result changes the plan: record the change, then spawn. |
   | drop | The goal no longer holds; close it. |
   | report | `note` becomes an `intention_report` to the owner, written in Nous's voice. |
   | ask | A question to the owner, or a `propose_action`; state becomes `awaiting_owner`. |

5. **Commit.** The decision, the state transition and a Brain decision record (category `process`, linked to the intention and its root) are written in one transaction.
6. **Failure.**
   - If the turn raises (API error, crash, timeout), the claim is released back to `result_ready` and `attempts` is incremented.
   - After `NOUS_CONTINUATION_MAX_ATTEMPTS` (default 3), the intention is closed as `report`, with the **raw result** sent to the owner.
   - If the turn ends without `resolve_intention` despite the forcing, it is treated as `report`, and the miss is logged and counted.
   - A result is never lost silently.
7. **Timing.** Internal continuation runs at any hour. Owner-facing output (reports, proposals, questions) produced during quiet hours waits until quiet hours end. Quiet hours use the same function as the heartbeat; it compares UTC hours, so prod's quiet-hours settings must be in UTC.

### 4.6 Bounds and cancel

Bounds are per root, checked by the gate and when a spawn tool is offered or called:

| Setting | Default | On reaching it |
|---|---|---|
| `NOUS_CONTINUATION_MAX_DEPTH` | 3 | no further spawning; the next result is reported |
| `NOUS_CONTINUATION_MAX_TURNS_PER_ROOT` | 8 | escalate (report) |
| `NOUS_CONTINUATION_MAX_SPAWNS_PER_ROOT` | 12 | escalate |
| `NOUS_CONTINUATION_MAX_TOKENS_PER_ROOT` | 400000 | escalate |
| `NOUS_CONTINUATION_STALL_LIMIT` | 2 consecutive `progress=false` | escalate |
| `NOUS_INTENTION_ROOT_TTL_HOURS` | 72 | report what exists and close |
| `NOUS_CONTINUATION_MAX_CONCURRENT` | 2 | queue |

**Cancel:**
- The owner can cancel a root through the companion card ("Active intentions"), the dashboard, or `POST /intentions/{root}/cancel`.
- A cancel moves every open intention of the root to `cancelled`, cancels that lineage's pending subtasks and DAGs, and makes the gate drop late results.
- `GET /intentions` lists open roots with their lineage and budget use.
- The REST API has the same no-auth LAN posture as the rest of `nous/api/rest.py`.

### 4.7 Observability and calibration

- **Dashboard.** An intentions view shows open roots as a tree, with each decision and note and the budget used. The Ledger view's hard-coded list of context kinds gains `continuation`.
- **Metrics:**
  - arrivals by decision;
  - time from result to decision;
  - results lost (target 0);
  - proposals approved, rejected and expired;
  - escalations by cause;
  - stalls;
  - continuation-turn token spend.
- **Calibration.** Each arrival decision is a Brain decision with a confidence. Owner actions grade it later through the existing decision reviewer: proposal approved or rejected, root cancelled, chain reached its goal or was dropped. That record is the evidence for any later widening of autonomy.

## 5. Settings

| Variable | Default | Phase |
|---|---|---|
| `NOUS_INTENTIONS_ENABLED` | `false` | 1. Record intentions and require `intent`. |
| `NOUS_CONTINUATION_ENABLED` | `false` | 2. Requires `NOUS_INTENTIONS_ENABLED` and `NOUS_RESULT_INBOX_ENABLED`. Startup logs a WARNING and stays off otherwise. |
| `NOUS_CONTINUATION_*` and `NOUS_INTENTION_ROOT_TTL_HOURS` | §4.5, §4.6 | 2 |

Each setting gets its row in `docs/reference/environment-variables.md`. Prod's compose file passes variables explicitly, so turning a flag on there needs its own compose line.

## 6. Data and migrations

- One migration per phase, numbered after F098's 081 (inbox) and 082 (result memory log), whichever is free when the phase lands.
- **Phase 1:** `brain.intentions` with its indexes, `(agent_id, state)` partial on the open states plus `(root_id)`; `heart.result_inbox.intention_id`.
- **Phase 2:** proposal storage, if shape (b) is chosen.
- All additive and idempotent (`IF NOT EXISTS`), agent-scoped.
- `tests/test_database.py::test_all_tables_exist` and the CLAUDE.md table count are updated in the same PR.
- **Reserved fields:**
  - `execution_dags.original_request` and `schedules.created_by_session` start being filled (Phase 0);
  - `schedules.continuation_*` is documented as superseded by intentions, not removed.

## 7. Testing

**Phase 1**
- Every spawn path writes exactly one intention in the spawn's transaction. A failure injected after the work insert leaves neither row; a failure injected in the intention insert leaves no work row.
- With the flag off: no rows, and the tool definitions are byte-identical (snapshot).
- A missing or blank `intent` is refused.
- Code-path spawns get generated intents.
- The default wake policy is correct for every origin in §4.1.

**Phase 2**
- The offered tools for `continuation` and for a subtask it spawned contain no `external` or `irreversible` tool, and none from the denylist. This holds under policy mode `off`, `warn` and `enforce`.
- A `run_python` call that reaches the network is refused in that lineage.
- **Injection test.** A result whose body asks for `send_email` cannot produce a send. The tool is not offered: assert the offered list, not the model's behaviour.
- Exactly-once claim under concurrent sweeps; one continuation per root.
- Each gate row has a test.
- Three turn failures produce a report with the raw result.
- A missing `resolve_intention` becomes a report.
- An approved proposal runs once, with the ledger key reserved before the call. A crash after the call and before the record leaves a visible in-doubt row and no second call. An expired or rejected proposal never runs.
- Cancel cascades. Each bound escalates at its limit.
- Quiet hours delay only owner-facing output.
- **Wiring tests** drive the real worker, scheduler and REST paths end to end. They must fail when the hook is removed.

**Phase 3**
- Calibration signals reach the decision reviewer.
- The dashboard endpoints return the tree.

## 8. Phases

| Phase | Content | Flag | Precondition |
|---|---|---|---|
| 0a | **Carry the reason** (storage only): pass the Plan decision id into spawns; fill `original_request`; keep the session and channel on `spawn_sync`; fill `created_by_session`. | none | #694 merged |
| 0b | **Carry the result:** heartbeat callbacks receive their check's findings; DAG check nodes store their findings as the node result. | none (behaviour fix, own tests and review) | #694 merged |
| 1 | §4.1–4.3 recording: intentions table, capture on every path, inbox `intention_id`. | `NOUS_INTENTIONS_ENABLED` | 0a |
| 2 | §4.3 suppression, §4.4–4.6: continuation kind, narrowed tools, proposals, arrival pipeline, bounds, cancel. | `NOUS_CONTINUATION_ENABLED` | 1 |
| 3 | §4.7: dashboard view, metrics, calibration signals; assumption re-check if the data asks for it. | – | 2 |

**Rollout.** Turn on Phase 1 and read a week of intentions (wake-policy mix, intent quality). Then turn on Phase 2, watch the decision mix and results lost (must stay 0) for a week, and only then discuss widening autonomy.

## 9. Risks and accepted residuals

- **`web_fetch` in a continuation's lineage (accepted for v1).** Untrusted page text could steer a fetch URL that carries private data. Subtasks have the same exposure today. Fetches made in an `internal_only` lineage are logged with their root. If it ever needs closing: a lineage may either fetch from the web or read private memory, never both.
- **Ahead of the evidence.** No benchmark measures an agent waking on its own result in a later turn. The design therefore keeps the decision rule in code (gate, states, bounds) and asks the model one narrow question, and autonomy widens only on recorded outcomes.
- **Cost.** Defaults bound a root to 8 continuation turns and 400k tokens. The `continue` population is roughly chat work plus Nous's own heartbeat and DAG work, not the 937 background monitors per month, which default to `none`.
- **Over-acting.** Models over-trigger when left to judge freely. The gate filters first, and `report` is the fallback for every failure path.

## 10. Research notes (sources)

- **The handle and the record are the truth; a push is only a hint.**
  - MCP 2026-07-28 moved tasks to an extension and says clients should persist task ids durably.
  - A2A 1.0 sends a push notification, then expects a `GetTask` fetch.
  - Postgres `NOTIFY` is a wake-up only and needs a sweep behind it.
- **Commit, and reconsider only on relevant events.** Kinny & Georgeff (IJCAI-91) and Schut & Wooldridge (2001) found this beats both blind commitment and re-planning on every event.
- **Prospective memory is the bottleneck.** On PM-Bench (2026) the best score is 65.1%; enforcing the intention lifecycle in code took a 2B model from 4.2% to 66.2%.
- **Agents treat updates as context rather than as revisions to their beliefs** (ClawArena-Team, 2026), and over-trigger when acting proactively (ProactiveBench).
- **Containment has to be structural.**
  - Meta's "Agents Rule of Two" (2025).
  - Design patterns for securing LLM agents against prompt injection (2025): plan-then-execute and action-selector.
  - CaMeL (DeepMind): capabilities and information flow.
  - OWASP LLM06, excessive agency.
- **Per-call caps do not bound a chain.** Claude Agent SDK budgets are per session. Magentic-One's progress ledger, which escalates after a stall, motivates the stall limit.
