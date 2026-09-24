# Harness Autonomy — Proposal Analysis & Roadmap (2026-09-24)

**Source:** "Harness changes for autonomy + subagents — proposal (2026-09-24)" (user upload).
**Principle (from the proposal):** move autonomy limits out of prose and into the harness, so the model
is allowed to act *because the harness makes acting safe*.
**Code baseline:** `origin/main` `f793ad0` (#642 merged 2026-09-24).
**Method:** five read-only verification passes, one per proposal cluster. Every claim below is checked
against function bodies and call sites, not docs. File:line anchors refer to `f793ad0`.

This document is the **roadmap and analysis**. Each phase gets its own task-level plan
(`2026-09-24-harness-autonomy-phase1.md` is the first); later phases are planned after the phase
before them merges, because they build on its interfaces.

---

## 1. Headline findings (what the proposal did not say)

1. **Nothing enforces the offered tool set at dispatch.** Every per-context restriction
   (`FRAME_TOOLS`, stable tool set, `_SUBTASK_EXCLUDED_TOOLS` `runner.py:1705`, `tool_filter`
   `runner.py:1709`, F078 refuse `runner.py:1718`/`:1172`) edits the *schema list* sent to the model.
   `ToolDispatcher.dispatch` (`tools.py:383`) resolves any registered name, and `_tool_loop`
   (`runner.py:1989`) dispatches whatever name the model emits. A tool name outside the offered set
   **still executes**. So the proposal's principle is not true today for any restricted turn.
2. **Execution context does not reach dispatch.** The only signal is `is_background: bool`, only on
   the `_tool_loop` path (`stream_chat` drops it, `runner.py:2726`), and only three tools read it
   (`tools.py:438`). Heartbeat triage, dynamic checks, callbacks, schedules, DAG nodes, `app.act`
   and `spawn_task` all collapse to `is_subtask=True, is_background=True`. No context enum exists.
3. **The execution ledger is in-memory only** (`cognitive/execution_ledger.py:1-6`). It is dropped at
   `end_conversation`, on eviction and on restart, and has no ids. P0.2's delivery ledger, P1.4's
   `side_effects[ledger ids]`, P2.8's `ledger_entry_id` and P2.9's autonomous-action rate all assume
   a table that does not exist.
4. **Background turns are broadly privileged.** Heartbeat triage runs with `tool_filter=None`
   (`heartbeat/runner.py:562`), i.e. `bash`, `send_email`, `dag_create`, and skips censors entirely
   (`cognitive/layer.py:910`). DynamicCheck/callbacks get the same whenever their tool list is empty.
   The DAG summary turn (`dag/delivery.py:277`) runs with `is_subtask=False`, so it even keeps
   `spawn_task`/`schedule_task`.

Findings 1–3 are shared dependencies of most of the proposal, so they come first.

---

## 2. Proposal claims vs verified code

| # | Proposal claim | Verified | Correction / sharper cause |
|---|---|---|---|
| P0.1 | Background exclusions are hardcoded name sets; no per-context policy | **Confirmed**, and worse | No dispatch-time enforcement at all (finding 1). No context enum (finding 2). `ActionRouter` `irreversible=True` exists (`a2ui/actions.py:55-58,114`) but is **never read**. Ledger side-effect classes are stale: 11 of 34 tools default to `write`, `send_email` is not in `EXTERNAL_TOOLS` (`execution_ledger.py:53`), `IRREVERSIBLE_TOOLS` is empty. |
| P0.2 | `send_email` has no idempotency key and records no Message-ID | **Confirmed** | SMTP via `smtplib` in a thread with **no timeout** (`email_tools.py:615-619`); no `make_msgid`; nothing persisted. `send_file` discards Telegram's `message_id` (`telegram_tools.py:135-140`) and a slow reply after a successful upload reads as an error. |
| P0.2 | "premarket send-email timeout = duplicate risk on auto-retry" | **Partly wrong** | DAG nodes never pass through the `NOUS_TOOL_TIMEOUT` wrapper (only `stream_chat` does). Duplicate risk comes from (a) DAG node retry: LLM fix-dispatch is **ON** in the prod snapshot (`NOUS_DAG_FIX_LLM_DISPATCH_ENABLED=true`) and `retry_node`, each launching a fresh subtask/session; (b) F061 hardened retries re-running the whole objective (DAG node subtasks carry no `max_attempts` cap, unlike `app.act` `a2ui/actions.py:921`); (c) orphaned SMTP threads completing after cancellation. F026 ActionGate's duplicate check is **off in prod** and never blocks a retry of an `error` row. |
| P0.3 | `approval_gate` records a choice but does not resume | **Confirmed** | Also: nothing ever **reads the choice back** (no tool, no REST GET, snapshot is `None` once resolved), no surface↔DAG-node column, no event on resolve, gates auto-pass (`orchestrator.py:2411`), no DAG status waits on a person, approvals default to 24 h while nodes cap at 2 h. Reusable: the `awaiting_check` durable poll loop (`orchestrator.py:1088-1250`) and `expire_sweep`'s claim-under-lock (`a2ui/service.py:932-1043`). |
| P1.4 | Typed spawn_sync return model exists; no artifact-path enforcement | **Confirmed, with caveats** | `SubtaskResult` exists (`api/models.py:74-126`) but `spawn_sync` is **off in prod** (`NOUS_SUBTASK_PAYLOAD_SCHEMA_ENABLED` unset). DAG nodes never get the typed contract; `dag_nodes.result` is the summary TEXT only and successors see concatenated strings (`orchestrator.py:2558-2584`). `write_file` is already confined to `workspace_dir` (`builtin_tools.py:31-44`); `bash`/`run_python` are not. `/tmp/premarket` does not appear in the repo (DAGs are runtime-authored). |
| P1.5 | F062 shipped, F063 spec only | **Confirmed** | F062 is registered only with payload-schema + hardening flags (`tools.py:3787-3792`). F063: no code. |
| P1.6 | DAG token budget enforced; no per-node budgets | **Partly wrong** | Token budget is **counted but not enforced** (`dag_token_budget_enforcement_enabled=False`, `config.py:1716`, unset in prod); counted only at node settle, so a wave overshoots. Per-node time: yes (timeout, reaper, stall). Tool calls: one global limit, per attempt. Per-node tokens/spend: none. |
| P2.7 | claim_verifier maps a claim to one tool | **Confirmed** | Name-only check (`claim_verifier.py:39-60,94-104`), `re.DOTALL` greedy `.+` spans paragraphs. "enforce" does **not** block: it injects a correction into the *next* turn (`runner.py:2580-2584`). The backup false positive depends on wording ("saved to" / "created … file"), and there are false negatives ("I pushed" passes on any bash). |
| P2.8 | No revert executor | **Confirmed** | `compensation.handler` is free text never read; no `review.revert` handler (test asserts forging it is rejected). Nothing pushes an action_review automatically after a mutating call. |
| P2.9 | No autonomy metrics | **Confirmed** | Sources exist but nothing aggregates them: `a2ui_actions` (incl. `no_objection` on expiry), `f026_action_gate` events, `outcome_signals.corrected`, `decisions.reviewer`. `events` and `a2ui_actions` have **no retention**. |

---

## 3. Dependency graph and phasing

```
                ┌──────────────────────────────┐
                │ 1a ExecutionContext +        │
                │    offered-set enforcement   │
                └──────────────┬───────────────┘
                               │
                ┌──────────────▼───────────────┐
                │ 1b Persisted execution ledger│
                └───┬──────────────┬───────────┘
                    │              │
      ┌─────────────▼───┐   ┌──────▼───────────────┐    ┌──────────────────────┐
      │ 2a P0.1 tags +   │   │ 2b P0.2 idempotent   │    │ 2c P2.7 claim → tool │
      │ context policy   │   │ sends + message ids  │    │ set + arg evidence   │
      └────────┬─────────┘   └──────────────────────┘    └──────────────────────┘
               │                     (independent of 1b)
      ┌────────▼──────────────────────────────────────┐
      │ 3  P0.3 park-and-resume (spec first)           │
      └────────┬──────────────────────────────────────┘
               │
      later: P1.4 typed DAG contract · P1.6 budgets · P2.8 compensation · P2.9 metrics
```

| Phase | PR | Invariant (one sentence) | Depends on | Ships |
|---|---|---|---|---|
| 1 | **1a** ExecutionContext + offered-set enforcement | A tool call executes only if its name was offered to the model in that iteration, and every dispatch knows which execution context it runs in. | — | enforcement ON (kill switch) |
| 1 | **1b** Persisted execution ledger | Every side-effecting tool call leaves a durable row that exists *before* the side effect and ends `success`/`error`/`blocked`/`unknown` — never silently `pending`. | 1a | ON (additive telemetry) |
| 2 | **2a** P0.1 capability tags + policy table | A tool's risk class is declared once at registration, and whether a context may use it is decided by one table, not by call-site name sets. | 1a (+1b for audit) | `warn` first, then `enforce` after a week of data |
| 2 | **2b** P0.2 idempotent sends | The same logical send (same key) reaches the recipient at most once, and "did it send?" is answered by the ledger plus a provider id. | 1a, 1b | ON for keyed calls |
| 2 | **2c** P2.7 evidence-aware claim verification | A completion claim is grounded when *any* tool that can produce that effect succeeded with matching arguments. | — | ON (behavior of an existing check) |
| 3 | **3** P0.3 park-and-resume | A DAG node can wait durably on a human answer and resumes on the answer or on the deadline with the declared default. | 1a, 2a | spec + review first; land dark |
| later | P1.4, P1.6, P2.8, P2.9 | see §5 | 1b, 3 | — |

Per the proposal's "Deliberately NOT now": no peer-to-peer agent teams.

### Design forks resolved for Phase 2 (recorded now so Phase 1 does not paint over them)

- **Where enforcement lives.** One `_authorize(context, tool_name, offered_names)` call in **both**
  loops, before dispatch. Not inside `ToolDispatcher.dispatch` alone: `extra_tools`
  (`submit_final_report`, `runner.py:1962`) bypass the dispatcher, and `stream_chat` never passes
  context today. Phase 1a lands the offered-set half; 2a adds the policy half to the same function.
- **Tag shape.** `dispatcher.register(name, fn, schema, *, side_effect, irreversible=False,
  spends_money=False, external_recipient=False)`, mirroring `ActionRouter._HandlerMeta`. The
  ledger's static sets are **derived from registration** (one definition — the F092.2 lesson), which
  fixes the stale `write` defaults and puts `send_email` in `external`. **Untagged = deny in
  background.**
- **Idempotency key default.** `{dag_id}:{node_name}:{sha256(canonical recipient|subject)[:12]}` for
  DAG contexts; `{subtask_id}:{sha…}` for plain subtasks; never `session_id` (it changes on every
  retry). Dedup by a partial `UNIQUE (agent_id, tool_name, idempotency_key)` on the ledger; a repeat
  returns the first call's result; an explicit `force=true` re-send is an operator action.
- **P0.3 mechanism** — deliberately **not** decided here: generalize `awaiting_check`'s condition
  (shell → `a2ui:<surface_id>`) vs a new `awaiting_approval` status. It gets its own spec + review.

---

## 4. Found during verification, not in scope of any phase (tracked so nothing is lost)

| Finding | Anchor | Suggested home |
|---|---|---|
| Spawn censor gate misses `schedule_task` (create and fire) and DAG node launch | `censor_actions.py:158-216`; `tools.py:3205`, `task_scheduler.py:184`, `orchestrator.py:2460` | 2a (policy) |
| F078 refuse denylist built from stale static sets → a refuse-tier censor does not strip `send_email`, `push_surface`, `dag_*`, `spawn_sync`, `resolve_*`, `ingest_document` | `runner.py:1172,1718` | 2a (derive from tags) |
| DAG send nodes have no `max_attempts` cap; F061 retries re-run the whole objective | `orchestrator.py:2465`, `subtask_executor.py:241-416` | 2b |
| `smtplib.SMTP` has no timeout | `email_tools.py:615` | 2b |
| `nous_system.events` and `a2ui_actions` have no retention | — | P2.9 |
| `irreversible` flag on ActionRouter never read | `a2ui/actions.py:55-58` | 2a |
| `_validate_path` confines `write_file` only; `bash`/`run_python` unconfined | `builtin_tools.py:31-44` | P1.4 |
| DAG token budget counted only at settle; enforcement off | `orchestrator.py:722-741,1009-1025` | P1.6 |
| `spawn_sync` off in prod; not given `_session_id` | `config.py:1059`, `tools.py:443` | P1.5 |
| `SubtaskManager.create` does not clamp to `subtask_max_timeout` (prod: node 6000 s > subtask max 5000 s) | `heart/subtasks.py:77-91` | P1.6 |
| Heartbeat triage turns skip censors entirely | `cognitive/layer.py:910` | 2a |
| Callback "may NOT re-enable" rule exists only in prompt text | `heartbeat/runner.py:625`, `dynamic.py:547` | 2a |
| Migration `073` is already used on unmerged branch `fix/retire-stale-calibration-factor` | `f3b6516` | Phase 1b uses **074** |

---

## 5. Later items (outline only)

- **P1.4 typed DAG contract:** pass `payload_schema`/`success_criteria` to DAG node subtasks; carry
  `report_jsonb` (not just the summary) to successors; add `artifacts[{path, sha256}]` and
  `open_questions` to `submit_final_report`; reject artifact paths outside `workspace_dir`;
  `side_effects` = ledger ids from 1b.
- **P1.5:** flip `NOUS_SUBTASK_PAYLOAD_SCHEMA_ENABLED` after measuring; F063 blackboard stays deferred
  unless a concrete fan-in need appears.
- **P1.6 budgets:** enable token-budget enforcement after measuring overshoot; count mid-flight from
  `_tool_loop` usage; add per-node `max_tool_calls`/`max_tokens` to `DAGNodeSpec`; widening = an
  escalation via Phase 3.
- **P2.8 compensation registry:** `register(..., compensate=fn)` on the tag shape from 2a; a
  `review.revert` handler resolves the ledger row → compensator; `action_review` pushed automatically
  for `irreversible`/`external` calls in background contexts.
- **P2.9 autonomy metrics:** read-only queries over the 1b ledger (autonomous-action rate by context),
  `a2ui_actions` (escalation / `no_objection` / override), `decisions.reviewer`, and 2b/2c outcomes;
  plus retention for `events` and `a2ui_actions`.
