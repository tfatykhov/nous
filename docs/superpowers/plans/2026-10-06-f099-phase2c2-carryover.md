# F099 Phase 2c-2: carry-over from the 2c-1 build, with lead rulings (binding for the reconciled 2c-2 plan)

Base: `main` at `2b8e4c26` (2c-1 merged as #705). The contract §4.1, §4.7 and §4.14 were **rewritten in #705 to describe the 2c-1 store as built**. It is now the source of truth for every 2c-1 signature. Read it against the code.

## 2c-1 interfaces that changed after the 2c-2 plan was written (reconcile every use)

- **`commit_arrival`**: full signature as in contract §4.7. It takes `tokens`, `brain=None`, `wrote_memory`, `arrival_id` and `now`. It refuses with `ValueError` in three cases:
  - an unknown outcome or decision;
  - a fallback whose decision is `ask`;
  - an `ask` with no owner channel at all.

  The Brain record is cut off with `SET LOCAL statement_timeout`, and `statement_timeout` is reset to DEFAULT afterwards. **Never set a transaction `statement_timeout` around `commit_arrival`.** Any test that passes a brain must be `postgres_only`.
- **`gate`**: `plan_outcome_of` is a **required keyword argument**. The gate asks the **root's** `origin_decision_id` first.
- **`eligible_roots(limit=50)`.**
- **`repair_missing_results(database, store, settings, *, limit)`** lives in `nous/heart/result_reconciler.py`, not in `continuation.py`. It is defined but **not registered** by `build_reconciler`. 2c-2 decides where it runs: register it in the reconciler's wake pass or in the runner's sweep. It must run only with continuation on.
- **`expire_roots(..., limit, now)`**
  - It fires only while `_ttl_applies` (an open continue/report intention).
  - It runs a stranded-row settle.
  - It takes roots in `(created_at, id)` order.
- **`wake_terminal_arrivals`** exists and runs as a sweep.
- **`fail_attempt` / `release_claim` / `release_stale_claims`.**
  - Attempts are kept across a failure and its reopen.
  - A successful commit resets them.
  - Every bump is fenced.
- **Lock order.** In-root: the root first, then claimed rows in id order, then inbox rows. Across roots: `(created_at, id)`. Every lock is `FOR NO KEY UPDATE`.
- **`with_bounds`** is applied at 7 sites. `IntentionLimitReached` is raised inside the store's transaction.

## Required in 2c-2 (each needs a test)

1. **Read the decision from the tool call.** `_tool_loop` returns the literal `"Report submitted."` for any terminal tool. The runner must take the decision from the captured `resolve_intention` arguments, never from the turn text.
2. **Confirm the tool name.** The `dag_create` approval-node refusal from 2a.6 names `resolve_intention`, so check that the registered tool name matches exactly.
3. **Pin the extra-tool names.** No `extra_tools` name (`resolve_intention`) may collide with a `TOOL_CLASSES` name or with the `internal_only` allowed set. 2d does the same for `propose_action`.
4. **Word the `IntentionLimitReached` refusal per turn kind.** Only a continuation turn can "end the turn with resolve_intention". A DAG node or subtask needs advice it can actually follow.
5. **Never launch a turn on a claim with zero rows.** This comes from the final 2c-1 review, Minor 1.
6. **`run_once` isolates each sweep step.** The steps are the lease release, expiry, wake, repair and push. A failure in one is logged and the rest continue. Isolate `wake_terminal_arrivals` per arrival too, since it has no per-arrival error handling.
7. **Cap the owner publisher's report bodies.** `raw_results_text` is unbounded, so use the inbox body cap.
8. **Roll check `on_complete` callback tokens into the DAG.** Add `await self._roll_check_tokens_into_dag(check, tokens)` after the callback's tally, gated on continuation, with a test.
9. **Make `_tick`'s cancel arm consistent for a cancelled roll-up.** The stats are now written before the roll-up, so a cancel during the roll-up means the run itself succeeded.
   - Record the registry outcome as success, matching `trigger_check`.
   - Correct the arm's ERROR text and the comment at `nous/heartbeat/runner.py` ~804-805, which name the wrong write.
   - The pinned log text at `tests/test_fix_b_dag_tick_recovery.py:463` changes only if the arm's text must change. Say so if it does.

## Rulings

- **R4: children inherit the root's Plan decision.** Continuation turns set `ctx.decision_id = root.origin_decision_id`, so spawned children inherit it. This is consistent with the gate and with Phase 3 calibration.
- **R5: the Brain record is written only for model decisions.** The runner passes `brain` to `commit_arrival` only when the outcome is `resolved`. Gate arrivals, `fallback_report` and `failed_report` get `brain=None`, because a deterministic gate or fallback is not a model decision and would pollute Phase 3 calibration.
- **R6: the `close_finished_sources` collision is accepted.** It can collide with `_expire_root`'s lineage UPDATE (scan order, no root lock). This is a liveness-only collision and both sides retry, so no change is needed. Document it as a residual.
- **R7: push suppression keys on `wake_policy == continue` only.** This stays as is: a cancelled or expired lineage's REPORT reaches you through the owner-facing path.

## Out of 2c-2 scope (2d / 2e / Phase 3)

- **2d**
  - Proposals.
  - `record_answer`, which takes the root lock first.
  - `_question_state` for proposals.
  - The A2UI-ready, surface-neutral owner actions.
- **2e**
  - Cancel. Unified late-result rule: expired → report raw, cancelled → stamp silently.
  - Rollback when there is no owner channel.
  - Cancelled intentions in the repair.
  - A legacy-closed sole child under an open root.
- **Phase 3:** A2UI cards (owner decision, 2026-10-06).
