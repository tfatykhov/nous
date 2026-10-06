# F099 Phase 2c: carry-over from 2a/2b reviews, with lead rulings (binding for the 2c plan)

Base: `main` at `41275ce1`. 2a (#704) and 2b (#703) are merged. The merge-order precondition "2a and 2b before 2c" is met.

## Facts the 2c plan must build on (verified by the 2a/2b reviews)

1. **The bus is per instance, not module state.** Each `Heart` has one `ResultInboxStore` (`nous/heart/heart.py` ~131), and its `_bus` is set with `heart.result_inbox.set_bus(bus)` in `main.py` right after `bus = EventBus()`. The writers share that instance. `intention.result_ready` is emitted after commit. When it is "moved or stayed in result_ready" it is a wake-up **hint** only. The sweep is the backstop, because the bus drops on QueueFull and event_bus may be disabled.
2. **`report_id` is the owner-facing inbox row's `source_id`, not its PK.** C2's idempotence depends on keeping the two separate. `arrival.report_ids` and everything 2c records about reports key on `source_id`.
3. **`TERMINAL_EXTRA_TOOLS = {"submit_final_report", "resolve_intention"}` (2a.5).** `_tool_loop` returns the literal `"Report submitted."` for **any** terminal tool (`nous/api/runner.py` ~3402-3404). So the runner must read the continuation's decision **from the `resolve_intention` tool call's arguments**, captured by the extra-tool executor, never from the turn's text.
4. **The continuation turn must run with `is_subtask=False`.** Otherwise `_offered_tools` drops `spawn_task` under the 012.2 subtask rule. Pin this with a test.
5. **Extra tools bypass the narrowing by design.** They are appended after the `internal_only` filter and routed to the caller's executor. Pin that `resolve_intention` collides with no name in `TOOL_CLASSES` and with nothing in the `internal_only` allowed set. 2d does the same for `propose_action`.
6. **The 2a refusal names `resolve_intention`.** `dag_create`'s approval-node refusal message tells the model to use `resolve_intention`. Confirm the registered tool name matches exactly.
7. **A cancelled DAG with a `continue` intention** moves the intention to `result_ready` with a FAILURE row; the subtask side does not. The gate must handle a FAILURE arrival from a cancelled DAG. If the root is cancelled it drops. Otherwise the turn sees the failure as a result.
8. **2b's re-route keeps no claim:** a T6 reopen clears `claim_token`, `claimed_at` and `attempts` (2b final fix). 2c's lease and attempts logic starts clean.
9. **Flip-time safety lives in 2b.** `has_continue_intention(include_closed=True)` ignores `legacy` closes, and `record_result` never reopens a `legacy`-closed intention.

## Residuals 2c must close: `repair_missing_results` (the spec's §4.5.1 sweep: "the same pass repairs continue/report intentions whose source is terminal but whose inbox row is missing")

The reconciler pass must cover each of these, with one test per case. All of them are flag-on only.

- **(a) Unrouted `report` subtask whose hook raised.** The intention stays `pending`, there is no row, and the close pass excludes `report`. Repair: write the report row through 2b's route, falling back to the owner channel, or close `legacy` when there is no channel at all.
- **(b) A Phase 1 DAG retried after the flip whose Phase 2 write is lost.** `InboxDagPass` no longer selects it, because it excludes `legacy`. Repair: report it. (On prod, `NOUS_RESULT_INBOX_DAG_SCHEDULED=true` already makes the pass select it; cover the flag-off-scheduled configuration.)
- **(c) A `pending` `continue`/`report` intention whose source already has a row for its current generation.** This is the flip-time case where Phase 1's close failed. Repair: close it as `delivered`. Never re-deliver.
- **(d) Cancelled lineage subtasks (`continue`/`report`) that never reach a writer.** Repair: close the intention. Use `cancelled` if its root is cancelled. Otherwise use `legacy`, with no report, because a cancelled subtask produced no result.

## Rulings on open items

- **R1. An unrouted report with NO content.** With continuation on, this currently closes `delivered` through `close_intention_quietly`'s short-circuit. One with content and no owner channel closes `legacy`. **Align them:** no content and no delivery closes as `legacy`. "Delivered" must mean an owner-facing row was written. One-line fix plus a test, in 2c.
- **R2. Push suppression keys on `wake_policy == continue` only.** A cancelled or expired lineage REPORT gets no raw push, and that stays as is: the REPORT row reaches chat and Telegram through the owner-facing path. The double fault (intention not open, a swallowed write, no origin) is covered by `repair_missing_results` (a), so nothing more is needed.
- **R3. A rollback with no owner channel leaves NULL-keyed rows undelivered.** This is out of 2c scope, because the rollback runs only with continuation off. Record it as a 2e note.

## Owner instructions in force

- Implementers run on Opus 5.5, plan writers on Sonnet 5.5, reviewers and architects on Fable 5.1.
- Merge gate: green CI plus a clean Fable final review (Codex is at its limit).
- The flag stays forced off (`CONTINUATION_RUNNER_READY = False`) through 2c and 2d. Only 2e flips it.

## Lead rulings on the 2c plan's contract conflicts (2026-10-06)

- **Split:** accepted. 2c-1 (store) and 2c-2 (runner) are separate PRs, and 2c-2 depends on 2c-1 being merged.
- **C1 wiring:** accepted. The runner is constructed only when `continuation_enabled and CONTINUATION_RUNNER_READY`, and that is pinned. 2e flips only the constant.
- **C2:** accepted. `repair_missing_results` lives in the reconciler module with the signature `(database, store, settings, *, limit)`, because `continuation.py` cannot import `result_inbox` (cycle).
- **Cancelled lineage subtasks:** accepted, per the carry-over. The intention closes with no report row.
- **Per-root lock `FOR NO KEY UPDATE` instead of `FOR UPDATE`:** provisionally accepted. The plan reviewer must verify the claimed FK-check deadlock and confirm that `FOR NO KEY UPDATE` still serialises claimers as the spec's claim needs.
- **Brain decision in a savepoint inside the fenced commit:** provisionally accepted. The plan reviewer must confirm that a savepoint rollback cannot leave the fenced commit half-applied, and that a dropped record is logged.
- **Push sweep in 2c, REPORT and QUESTION only:** accepted per the lead's brief. 2d adds PROPOSAL.
- **`record_result`'s REPORT gets `push_after`:** accepted. R2 needs it.
- **NULL-deadline (Phase 1) roots — lead leaning, reviewer to confirm:** do NOT expire them silently at the first sweep after the flip. That would kill a legitimate in-flight Phase 1 chat spawn the moment 2e turns the flag on. Treat a NULL `deadline` as `created_at + NOUS_INTENTION_ROOT_TTL_HOURS`, which is bounded and not immediate. Phase 1's carry-over D2 said "treat NULL as no deadline", and this is the bounded refinement of it. If the reviewer finds a reason the plan's choice is safer, say so.
- **`update_dag_tokens` through a new orchestrator method,** since the contract's `dag_store.add_tokens` does not exist: accepted.

## Plan review 2c-1 (prev-2c1, Fable): Ready after MUST-FIX (4 MUST, 5 SHOULD, 9 NIT)

Ruling verdicts:
- R1 `FOR NO KEY UPDATE`: CONFIRMED. The FK cycle is real, and the weaker lock still serialises claimers.
- R2 Brain record in a savepoint: CONFIRMED. Keep it after the fenced moves.
- R3 NULL deadline: CONFIRMED (the lead's choice). The plan already expires at created_at + TTL, and MF-3 fixes the no-report divergence.

All 4 MUST-FIX items (gate the check-token roll-up on continuation; lock the root first everywhere; report when rows exist on a NULL-deadline expiry; lock, then read, in _expire_root) are to be folded in, along with the SHOULD-FIX items.

## Plan review 2c-2 (prev-2c2, Fable): Ready after MUST-FIX (2 MUST, 6 SHOULD, 8 NIT)
Grounding: 20+ citations hold, with no phantom API. Every 2c-1 name consumed matches. Prod parity, security (no owner-authority path; propose_action never offered) and the loop's Fix-Z/semaphore/lease/crash recovery all check out.
MUST-FIX: the batch-spawn test hits the depth limit (use max_depth=4). The runner start() runs before register_subtask_tools/register_dag_tools, so at the flip a boot-time root would get no spawn tools; build early and start() last in create_components.
Both reviews were sent to plan-2c to fold in (all MUST + SHOULD, cheap NITs).
