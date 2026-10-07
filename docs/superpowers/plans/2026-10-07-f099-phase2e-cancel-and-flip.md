# F099 Phase 2e: Cancel and the flag flip Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the owner stop a root, and everything under it, for good, and then let the owner turn `NOUS_CONTINUATION_ENABLED` on. `cancel_root` marks the root and cancels its lineage, subtasks, DAGs, proposals, containers and their fires and the running turn; every tool call on behalf of a cancelled root is refused at dispatch whatever the authority or the modes say; a cancelled root says nothing it has not said, and an expired one reports what came back. The residuals that must land before the flip close with it, and the flip itself is the last commit, which changes only the constant, the pins that depend on it and the docs.

**Architecture:** The last of the Phase 2 PRs (2a, 2b, 2c-1, 2c-2 and 2d are merged and land dark). Nine tasks, the first eight dark behind `NOUS_CONTINUATION_ENABLED` and `CONTINUATION_RUNNER_READY = False`, the ninth the flip.
- **The store (2e-1, 2e-2).** `continuation.cancel_root` is one transaction, the root locked first, then the lineage's containers and their schedules, then the writes: the marker, the open intentions, the subtasks, `staged`/`pending`/`approved` proposals (so a claim that read the marker before this commit finds its row already moved), the unread results stamped silently, and the open fires of each container. `_expire_root` gets the same proposal fix. The unified late-result rule is one idea in three places (the gate, `record_result`'s closed-root branch, the stranded-row settle): an expired root reports the late result raw, a cancelled root stamps it silently.
- **The choke point (2e-3).** `AgentRunner._authorize_tool_call` asks an in-process view of cancelled roots, first, for every context that names a root. `discard_conversation` gives a continuation turn an empty thread.
- **The budget (2e-4).** Migration 085: the tokens of a failed attempt are kept on the claim's deepest intention and counted by `root_limits`.
- **The runner (2e-5).** `ContinuationRunner.cancel_root` commits the store's cancel, takes the roots into the view, cancels the DAGs through the orchestrator, cancels the running turn tasks, and tells the bus. A sweep step keeps the view fresh and cancels stray DAGs. `main.py` wires the view and the orchestrator's cancel (inert until the flip).
- **The residuals (2e-6).** Nothing is left undelivered by the rollback; a question's window starts at its push; the sweep resumes an approved proposal nobody started; the push runs in its own task.
- **The surfaces (2e-7, 2e-8).** `GET /intentions`, `POST /intentions/{root}/cancel`, Telegram `/intentions` and `/cancel_intention`, all owner actions on the same surface-neutral pattern as 2d, with the prod parity pins in one file.
- **The flip (2e-9).** `CONTINUATION_RUNNER_READY = True`.

**Tech Stack:** Python 3.12+, SQLAlchemy 2 async, PostgreSQL 17 + pgvector, Starlette, httpx, pytest with `asyncio_mode = "auto"`.

**Spec:** `docs/superpowers/specs/2026-10-05-f099-intentions-and-continuation-design.md`, read §4.5 and §4.6 (Cancel, TTL, bounds). Binding names are in the Phase 2 contract (`2026-10-06-f099-phase2-contract.md` §1.5, §2, §4.1 T13, §4.7 `[2e]` lines, §4.8, the "Superseded by 2d" block) and the 2e carry-over (`2026-10-07-f099-phase2e-carryover.md`: items 1 to 12, rulings R12 to R14 and the deploy notes), which wins where it differs; the lead rulings win over both. **Code base:** `main` at `129d0776` (2d merged as #707). The merged code is the source of truth for every signature. Line anchors drift: **anchor by function name, not by line number**.

## Contract conflicts and interpretations (read first; each has a resolution this plan applies)

| # | Contract / carry-over says | Code or rule says | Resolution this plan applies (lead to rule) |
|---|---|---|---|
| E1 | Carry-over: "the runner already has a `_root_cancelled` seam, `lambda _id: False`". Contract §4.5: `self._root_cancelled` "is `lambda _id: False` until 2e installs the view", and the check sits after the strict block. | There is no such seam. `grep` finds no `_root_cancelled` and no `set_cancelled_roots` in `nous/`, and `_authorize_tool_call` has no cancelled-root check. | 2e adds all three: the attribute (default `_no_cancelled_roots`), `AgentRunner.set_cancelled_roots(view)` and the check. The check is the **first** statement of `_authorize_tool_call`, before the strict block, so the model reads "cancelled by the owner" rather than a policy refusal, and it cannot depend on a mode. It runs only when the context names a root (`ctx.root_intention_id is not None`): a chat turn pays one `is not None`. Pinned (2e-3). |
| E2 | Contract §4.5: `Refusal("...", "root_cancelled")`. | `Refusal.code` must be in `ledger_store.REFUSAL_CODES`; `record_blocked` raises `ValueError` for any other code, so the first refused call of a cancelled lineage would have raised inside the tool loop. | `"root_cancelled"` joins `REFUSAL_CODES` (2e-3), and a test runs the refusal through a ledger that enforces the set. |
| E3 | Contract §4.8: `ContinuationRunner(..., cancel_dag=orchestrator.cancel_dag)`. | `_build_continuation_runner` runs at `main.py` ~1133; the DAG orchestrator is built at ~1415. | The constructor keeps an optional `cancel_dag` (tests pass a recorder); `set_cancel_dag` binds the orchestrator's method inside the DAG block, guarded by `if continuation_runner is not None`. Inert until the flip (the runner is `None`). |
| E4 | Contract §4.7: `CancelOutcome(root_id, already_cancelled, cancelled_intentions, cancelled_subtasks, cancelled_dags, cancelled_proposals, deactivated_schedules, turn_stopped)`. | The store cannot cancel a DAG (the orchestrator does, in its own transactions) or a task. | The store fills what it can and reports `dag_ids`, `proposal_ids` (the ones the owner had been shown) and `root_ids` (the root and the fires of its containers); `ContinuationRunner.cancel_root` fills `cancelled_dags` and `turn_stopped` with `dataclasses.replace`. Every field has a default. `cancel_root(session, agent_id, root_id, *, reason, actor)` keeps the contract's signature plus an optional `now`. |
| E5 | Contract §4.7: `cancelled_root_ids(session, agent_id, *, since)`; §6 risk 1: `start()` "loads roots with `root_cancelled_at IS NOT NULL` that still have open work". | After a cancel nothing is open (every row is `cancelled`), so "still have open work" would load nothing, yet a lineage check or a DAG node of a cancelled root can run in a new process. | `since` is optional (None reads every cancelled root, at most 10,000, newest first). `start()` and `main.py` load them all; every sweep re-reads the roots cancelled since the last sweep less two minutes. The view is a set of UUIDs: a thousand cancels cost nothing. One process per (database, agent_id) holds, as for the ledger. |
| E6 | Contract §4.10: `GET /intentions?state=open\|all&limit=20` and `GET /intentions/{root_id}` (RootView with lineage, arrivals, open proposals and open questions). | The carry-over scopes 2e to `GET /intentions` and the cancel; nothing calls a detail route. | `GET /intentions` returns the contract's RootView, compact: the root's columns, `root_limits` (none for a container), up to 50 lineage rows (`lineage_truncated` says so), the newest 5 arrivals and the open proposals. `open_questions` and `GET /intentions/{root_id}` are not built; Phase 3's card can add either. **Open question 2.** |
| E7 | Brief: the new routes "answer 404 or an empty result" in prod. | Prod has intentions ON, so Phase 1 roots exist and would be listed. A cancel needs the runner (it stops the turn and the DAGs), which prod does not build. | `GET /intentions` answers `200 {"roots": [], "continuation": false}` with continuation off and **reads no row**; `POST .../cancel` looks the root up first (404 for no root) and answers 503 for a root when there is no runner, **never** cancelling through the store alone (that would cancel a real Phase 1 lineage without the turn and DAG stop). With the flag ON the list shows every open root, the open Phase 1 roots (a schedule's container included) among them, and each can be cancelled; the empty list is the flag-off case only. Pinned with prod's flags (2e-7, 2e-8). **Open question 3** (a lead who wants the list to show Phase 1 roots drops one `if`). |
| E8 | Contract §4.11: `/intentions` and `/cancel_intention <root_id>`. Carry-over and brief: Telegram `/intentions`. | A list with no way to act on it is half a feature; the contract names the command. | Both commands (2e-8). `/intentions` falls through to chat unless the server answers 200 with `continuation: true`; `/cancel_intention` falls through on a 404 and on a malformed id (C18, strict parity). **Open question 1:** drop `/cancel_intention` if the lead wants only the list: one handler and its tests. |
| E9 | Carry-over: "deactivates a container's schedule, and cancels the open roots of that container's fires". | A container is also a CHILD row inside a lineage that scheduled something (`schedule_task` from a lineage, `prepare_intention` with `parent_id`), and each fire is a new root whose `parent_id` is the container. | `_cancel_lineage` scans the whole lineage for `wake_policy = container` rows, not only the root; it locks them, then their schedules, in the one order container then schedule, before it writes, and then cancels the fires that still have something open, recursively and bounded (`CANCEL_ROOTS_MAX = 50`). A fire that finished is not marked: a marker on a finished root would only silence a later result. |
| E10 | Spec §4.5.3 and contract `GATE_DROP_REASONS`: a cancelled **or expired** root is dropped silently. | Carry-over item 5 (binding): an expired root reports the late result raw everywhere. | `GATE_DROP_REASONS` loses `expired`: an expired claim is an escalation that reports the raw rows. Four existing tests encode the old rule and change on purpose (2e-2, listed). |
| E11 | Spec §4.6: `_authorize_tool_call` "reads the root row and caches it for the turn, invalidated by the cancel". | `_authorize_tool_call` is synchronous (contract risk 1). | The in-process view of E5. A call already past the check when the marker commits runs; the next one is refused; a spawn is refused by the database (I1) from the commit on; an approval is refused by `claim_execution`. Stated in `cancel_root`'s docstring. |
| E12 | Carry-over item 7: add `cancelled` to the repair's scope "if cancel can leave cancelled intentions with terminal sources that have no row and need settling". | Cancelled sources are cancelled by the cascade (their status flips in the same transaction), so no `cancelled` intention has a cancelled source awaiting a row. A source that completed just before the cancel and whose writer never ran leaves no row, and nothing selects it again (the repair reads `pending` and `expired`; `InboxSubtaskPass` settles a routable one through `record_result`, silently). | **No `cancelled` arm.** Two tests pin that it neither spins nor reports (2e-2). Ruling 7 below. |
| E13 | Carry-over item 8: "give such a root its TTL, or close it". | The only legacy close that leaves a lineage with nothing open and no result is repair (d): a cancelled subtask with no result. An empty `report` also closes legacy, but nothing was waiting on it. | `close_cancelled_source` takes the root lock first and, when the close leaves a lineage whose newest arrival decided `continue` or `revise` with nothing open and no marker, `end_hanging_root` writes `root_expired_at` and one REPORT. Ruling 8 below, and the T6 consequence. |
| E14 | The 2c-1 test `test_a_root_cancel_that_commits_just_before_the_close_is_honoured` lets a cancel commit between the close's read and its UPDATE. | The close now locks the root first (the one lock order), which makes that interleaving impossible and, in that test's shape, a deadlock. | The test is replaced by its lock-ordered form (2e-2): a cancel that holds the root makes the close wait, and the close then sees the marker. A changed pin. |
| E15 | Contract §4.7 and the 2d plan: `claim_execution` "wins" against a committed marker only. | A marker that is **not yet committed** is invisible to the claim's `EXISTS`, so a claim could pass while a cancel was in flight (2d-3 review m1). | Carry-over item 1: `cancel_root` and `_expire_root` move `staged`, `pending` and `approved` proposals in their own transaction under the root lock. The claim's UPDATE then waits on the proposal row and fails its `state = 'approved'` re-check. Tested with the lock held, not with two racing coroutines (2e-1). |
| E16 | Spec §4.6: a cancel "always writes `root_cancelled_at` on the root row". | A cancel of a root with nothing running (every row closed, no subtask or DAG alive, no schedule active) would only put a marker on finished work, and the marker silences any later result of that root (a DAG retry, say). | `cancel_root` refuses it (`CancelRefused("finished")`, 409 over REST, nothing written) and a repeat of a cancel that did something is allowed (`already_cancelled`). A deviation from the spec's "always", on purpose: ruling 8. **Open question 6.** |

**Plan review folded in (Fable 5.1, 2026-10-07; every item applied).** **M1:** a cancel closes the lineage's unsent owner-facing rows, so a deferred PROPOSAL is never pushed and F098's chat claim never reads it (2e-1). **M2:** the inbox metrics count the item-9 stamp and the cancel's stamp apart from `delivered`, and the inbox-off WARNING names the rows (2e-6). **S1:** a real claim-then-cancel race test, deterministic through Postgres' lock queue (2e-5). **S2:** the retap test waits on events, not sleeps (2e-6). **S3:** `_expire_root` writes `decided_at` and `decided_by` on the proposals it ends and the runner announces them (2e-1, 2e-5). **S4:** "After the flip" names the two changes the owner meets first. **S5:** the cancel's docstring and the route's rest-api row say what a cancel cannot take back (2e-1, 2e-7, 2e-9). **S6:** the flag-off rollback ends `approved` proposals too (2e-6). **N1, N2, N3, N5, N6, N7** applied (2e-5, 2e-5, 2e-2, 2e-3, deploy notes, 2e-7). **N4 skipped:** `cancel_root` reads the root `FOR NO KEY UPDATE` and `_cancel_lineage` locks it again; the second lock on a row already held is a no-op, and removing it would need a flag through `_cancel_lineage` for the first root, which is more code than the line it saves.

**Migration: one is needed, 085** (`sql/migrations/085_intention_failed_tokens_and_cancel_index.sql`). Migration 084 already has every state and column the cancel needs (`root_cancelled_at` on `brain.intentions`, the `cancelled` intention and proposal states, `decided_by`). It has nothing that keeps the tokens of a failed attempt (item 2: a retried attempt writes no arrival row), so 085 adds `brain.intentions.failed_tokens INTEGER NOT NULL DEFAULT 0` (a constant default: metadata only on PG 11 and later). It also adds the partial index `idx_intentions_cancelled (agent_id, root_cancelled_at) WHERE root_cancelled_at IS NOT NULL` that the view's load and refresh read. No new table, so `agent_id` scoping is inherited from `brain.intentions`; the comments hold no semicolon (the migrator splits on one), and both statements are idempotent.

## Rulings on the open choices (reasons)

1. **Item 2, the row design: a column, not an arrival row.** An arrival row per failed attempt needs a new `outcome` value (a widened CHECK), a `root_limits` turn count that excludes it, a prompt that skips it (`_lineage_context`, `build_arrival_prompt`), and every other reader of `intention_arrivals` (`wake_terminal_arrivals`, the views, the dashboard) to learn that a row with no decision is not a decision. Each failed attempt would also consume an `n`, which the owner reads as "arrival 4". A column on `brain.intentions` is read by exactly one query (`root_limits`, which already scans the lineage) and written by exactly one statement, in `fail_attempt`'s own fence. It is charged to the claim's **deepest** intention only, so the root's budget is a plain sum that counts a claim of several intentions once. At the cap the cost goes where it always went, the `failed_report` arrival row. A lease release knows no usage and charges nothing; a call that raised before it returned is the one cost the runner cannot see (the usage of the calls that finished is counted).
2. **Item 9, the rollback with nowhere to deliver: close and stamp.** Close as today, and mark the undeliverable rows `delivered_at = now, delivered_session_id = 'rollback-undeliverable'`, with the WARNING that was already logged. The rows would otherwise be NULL-keyed, undelivered and read by nobody for ever: the backlog of "undelivered and unclaimable" results would never reach zero, and that is the signal Review Focus 4 of the contract watches for. The result is not lost: it stays on its work row, as `record_result`'s own "no owner channel" branch says. Keeping the intention open instead is not safe: with the flag off nothing claims it, and a later flip would wake a turn on a result that is days old. A failed push (a transient error) is a different case and still keeps everything open. No owner-facing surface may report the stamp as delivered (the lead's requirement): `ResultInboxStore.metrics` counts the rollback's and the cancel's stamps in buckets of their own (`undeliverable`, `closed_by_cancel`), outside `delivered` and `delivery_rate`, and the WARNING names the rows (2e-6). In prod the branch is unreachable twice over: no flag-on row exists, and the prod process has a default chat.
3. **Item 11, an approved proposal after a crash: the sweep resumes it.** `_proposals_terminal` counts `approved` as unfinished, so an approved proposal nobody starts holds its arrival in `awaiting_owner` until the root's TTL (72 h): "document and wait for a re-tap" strands the lineage. The sweep starts, in a tracked task like the one a decision starts, every `approved` proposal of an open root that was decided more than `max(tool_timeout + 5 s, 60 s)` ago (an inline run is not stolen) and less than `intention_proposal_ttl_hours` ago (an approval is not honoured days later: that one ends with its root). `claim_execution` is the fence, so at most once holds, and one in flight is never started twice. The owner's re-tap resumed it already (`decide_proposal`'s `approved` branch); that stays as the second path.
4. **Item 12, the push bound: its own task, held on the runner.** The sweep starts the push (never two at once), waits `PUSH_WAIT_SECONDS` (5 s) for it and goes on; the next sweep sees it finished. The push is **not** cancelled when the wait ends (a send cancelled between the request and the stamp would be sent again), and `stop()` lets it finish (bounded) before ending it. A per-sweep cap alone still blocks the launch for as long as a send takes. `SweepReport.pushed` keeps its meaning for a push that finished inside the wait, so the 2c-2 pin `report.pushed == 2` does not change.
5. **Item 7, the repair and cancelled intentions: no arm** (E12).
6. **Item 8, a hanging root: close it and say so** (E13), not "give it a TTL": nothing is open, so a TTL has nothing to expire, and a root that ends in silence is the defect.
7. **Item 10, the question window:** `question_window_start(row) = max(created_at, push_after)`, used by `_question_state` (the sweep's wake) and `record_answer`: the same rule as a proposal's deadline, with no schema change.
8. **A cancel of a finished root is refused (409), a repeat is allowed** (E16, a deviation from the spec's "always writes the marker"). A marker on a root with nothing running would only silence a later result, so a root with nothing to cancel is refused and nothing is written; a repeated cancel cancels whatever is still running and says `already_cancelled`.


## Global Constraints

- **One migration, no setting, no new table.** Migration 085 (above). No new module, so `docs/reference/project-structure.md` needs no row. The two routes get their rows in `docs/reference/rest-api.md`, the F099 status goes in `docs/reference/shipped-features.md` and `docs/features/INDEX.md`, the env-vars row of `NOUS_CONTINUATION_ENABLED` is rewritten, and the contract gets its "Superseded by 2e" block (2e-9).
- **Flag-off parity and prod's exact flags (R14).** Prod runs `NOUS_RESULT_INBOX_ENABLED=true`, `NOUS_INTENTIONS_ENABLED=true`, `NOUS_RESULT_MEMORY_ENABLED=true` and continuation OFF. Every task states what runs in prod and pins it under those flags. The answer for tasks 2e-1, 2e-2, 2e-5 and 2e-6 is **nothing new** (the runner is `None`, and every changed store function is called only by the runner or behind `continuation.enabled`). The paths that do run in prod are named: `_authorize_tool_call` (2e-3), the startup rollback and migration 085 (2e-4, 2e-6), the new routes (2e-7) and the bot (2e-8). **Every commit before the flip leaves prod unchanged; the flip commit changes only `CONTINUATION_RUNNER_READY`, the pins and docs that depend on it, and nothing else.**
- **Cancel is an owner action, never a model one.** `cancel_root` is called by `ContinuationRunner.cancel_root`, by the REST route and (Phase 3) by the A2UI `ActionRouter`. It is not registered with the dispatcher, is not in `TOOL_CLASSES`, and is not an extra tool of any turn. A model can cancel only its own children (`cancel_task`, with its lineage rule), never a root. Pinned (2e-8).
- **A cancelled root's lineage dispatches nothing, whatever the authority or the mode.** The view is checked first in `_authorize_tool_call`, for every context that names a root, with both enforcement modes `off`. Pinned for seven kinds and authorities (2e-3).
- **Locks (copied from 2c-1).** Every row lock on a root or a claimed intention is `FOR NO KEY UPDATE`, in ONE order: the **root first**, then the lineage's containers (in id order), then their schedules, then the lineage's rows, then proposals and inbox rows. Across roots a sweep or a cascade takes them in `(created_at, id)` order. A path that took an intention or a schedule before its root would deadlock against the TTL sweep or a cancel. `record_result` keeps its own `FOR UPDATE` and holds no second lock. `_hold_open_root` (a spawn) is `FOR SHARE` on the root, which conflicts with the cancel's `FOR NO KEY UPDATE`: that is the whole of I1.
- **Fenced means in the statement.** Every write to a claimed intention goes through `_fenced_move` (the failed-attempt charge too); `claim_execution` keeps `state = 'approved'` and the root-open predicate in one UPDATE, and the cancel and the expiry now also move the proposal row under the root lock.
- **Settings and tests.** Every test that wants the flag on sets all three (`f099_support.CONT`); a runner-building test passes `ANTHROPIC_API_KEY="test-key"` through `runner_env`. Real Postgres; SQL SQLite cannot run carries `pytestmark = pytest.mark.postgres_only`. **The model is always faked** (`f099_support.ScriptedModel`); no test reaches an API or Telegram. Each test uses its own agent (`env_factory`).
- **Deterministic concurrency.** As in 2c-1 and 2d: hold a lock in a session, start the other side in a task, wait (bounded) with `until_a_backend_waits_on_a_lock`, release, assert. Never race two coroutines and assert a winner. Every `await` on a task is inside `asyncio.wait_for`. A test that needs the model "mid-turn" blocks it on an `asyncio.Event` it controls. The helper reports backends waiting on a lock whose statement mentions `brain.intentions`; every race of this PR waits on such a statement (the claim's UPDATE of a proposal carries a sub-select on `brain.intentions`, and the cancel's own first statement is a lock on it), and each race test fails under its mutation, which is how that was checked.
- **Test expectations are not negotiable.** If a test in this plan fails after the implementation step, fix the implementation. If you are sure the test itself is wrong (a fixture name, a helper signature that differs on `main`), fix only that mechanical detail and say so in the task report. Never weaken an assertion.
- **Fail-on-base rule, and its exception.** Every task contains tests that call production code and fail on the task's base before the change. The exceptions are **pins**, which pass on the base by design; each is marked `# PIN`. Tests of earlier PRs that **change on purpose** in this PR are listed in the task that changes them (2e-2: thirteen; 2e-4: two; 2e-6: five edits to the tests that age a question by hand; 2e-7: one; 2e-9: the flip's six).
- **Commits.** Use explicit `git add <path> …`; never a directory, `.`, `-A` or `commit -a` (this is a public repo). Write the message to a file (`git commit -F <file>`), ending with these two lines:
  ```
  Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_015q4W1nke7JzGaZkC21Whng
  ```
  Put `set -o pipefail` before any `… | … && git commit` chain. Never use `git stash`.
- **Lint.** `lint-delta.sh <worktree>` must report clean: no new ruff finding and no format drift in a touched file. Files that are format-clean on the base (run `ruff format` on them after editing): `nous/brain/continuation.py`, `nous/handlers/continuation_runner.py`, `nous/api/runner.py`, `nous/main.py`, `nous/api/intention_routes.py`, `nous/owner_actions.py`, `nous/heart/result_reconciler.py`, `tests/f099_support.py`, and every F099 test. These are not, so edit them without reformatting: `nous/telegram_bot.py`, `nous/storage/models.py`, `nous/cognitive/ledger_store.py`, `nous/api/rest.py`.
- **Public repo.** No machine-local path, private host name, IP, credential or personal name in any file, test, comment, commit message or PR text. Use `$BIN`, `$WT`, `$DB` in commands; say "the owner" or "the user". Test chat ids are made-up numbers; the fake bot token is `"test-token"`.
- **Implementers never run `uv run` in the worktree.** The lane is `"$BIN/nous-test-linux.sh" "$WT" runner pg "$DB" <files>`.

## Review Focus

The five failure modes most likely to bite, most likely first. Each names the tests that pin it; the mutation to run is in the task.

1. **A cancelled lineage still acts.** A subtask that was running when the owner cancelled, a DAG node, a lineage check or an approved call must not do one more thing, in any mode. Pinned: `test_a_call_on_behalf_of_a_cancelled_root_is_refused_whatever_the_modes_say` (seven kinds and authorities, both modes off), `test_a_cancelled_roots_tool_call_does_not_run_through_a_real_turn` (the handler never runs, the ledger takes the new code) and `test_the_refusal_is_checked_before_the_strict_rule_and_a_cancelled_root_is_not_offered_anything` (2e-3); `test_after_a_cancel_the_lineage_can_dispatch_nothing`, `test_a_restart_remembers_the_cancel_and_a_sweep_takes_in_one_made_elsewhere` and `test_start_loads_the_view_before_the_loop_runs` (2e-5, the view survives a restart and a cancel made elsewhere); `test_a_cancel_is_not_a_tool` (2e-8). Mutation: remove the check, and ten of the sixteen 2e-3 tests fail.
2. **An approved call starts after the cancel (the hard requirement of 2d-3 review m1).** Pinned: `test_a_cancel_not_yet_committed_stops_an_approved_call_from_starting` and `test_an_expiry_not_yet_committed_stops_an_approved_call_from_starting` (2e-1: the marker is not committed, the lock is held, the claim waits on the proposal row and returns None), `test_a_cancel_cancels_every_proposal_that_could_still_start`, `test_a_decision_after_a_cancel_is_refused_as_ended`. Mutation: drop the proposal UPDATE from `_cancel_lineage` and three tests fail; revert `_expire_root` to `staged` only and the expiry race fails.
3. **A cancel races something and loses a row or leaks a slot.** A spawn in flight (`test_a_spawn_in_flight_makes_the_cancel_wait_and_is_cancelled_with_the_lineage` and `test_a_cancel_in_flight_refuses_a_spawn`), a fire in flight (`test_a_fire_in_flight_is_part_of_the_cancel`, `test_a_cancel_in_flight_refuses_a_fire`), a claim (`test_a_cancel_clears_a_live_claim_so_the_turns_commit_loses_its_fence`, `test_a_claim_after_a_cancel_finds_nothing_to_claim`, and the real race `test_a_cancel_that_queues_behind_a_claim_stops_the_turn_that_claim_started`), a turn past its model call (`test_a_turn_that_finished_but_has_not_committed_loses_its_fence_to_the_cancel`), a running turn (`test_a_cancel_stops_the_running_turn_releases_its_slot_and_commits_nothing`: the task is cancelled, its claim release finds 0 rows, the slot is back, no arrival or report was written), the close of a cancelled subtask (`test_a_root_cancel_in_flight_is_honoured_by_the_close`). Mutation: not cancelling the task leaves the turn blocked and the slot taken.
4. **A cancelled root reports, or an expired one goes quiet.** The unified late-result rule in three places: `test_an_expired_roots_arrival_reports_what_came_back_and_a_cancelled_ones_does_not`, `test_a_late_result_of_a_cancelled_root_is_stamped_and_never_reported`, `test_a_late_result_of_an_expired_root_is_reported_raw`, `test_the_sweep_stamps_the_rows_stranded_on_a_cancelled_intention_without_a_report` and its expired twin, `test_a_cancelled_roots_unread_results_are_not_reported_by_the_next_sweep`, plus `test_a_cancelled_marker_wins_over_an_expired_one` and `test_the_sweep_judges_a_stranded_row_by_the_roots_marker_as_well_as_the_intentions_state`. A cancelled root's owner rows are not pushed or claimed either: `test_a_cancel_closes_the_lineages_unsent_owner_rows_so_nothing_is_pushed_or_claimed` (2e-1: a deferred question is never pushed, no chat claim reads it), and no metric counts a stamped row as delivered: `test_the_inbox_metrics_count_a_row_nobody_read_apart_from_the_delivered_ones` (2e-6). A lineage left hanging is closed and reported once (`test_the_last_child_of_a_lineage_waiting_on_it_is_cancelled_and_the_root_is_closed_and_reported`, four negative cases).
5. **The flip, or anything before it, changes prod.** `test_prods_writers_and_passes_never_reach_a_2e_store_path` (every changed store function replaced by a raiser while a subtask finishes, the reconciler ticks and the repair runs), `test_prods_rollback_finds_nothing_new`, `test_the_cancel_view_is_a_set_lookup_and_the_default_reads_nothing`, `test_a_context_that_names_no_root_never_asks_the_view`, `test_prods_flags_install_no_view_and_build_no_runner` (the constant flipped, prod's flags: still no runner), `test_the_new_routes_on_prods_flags_answer_empty_503_and_404_and_change_nothing`, `test_with_no_owner_chat_set_intentions_and_cancel_make_no_request_and_go_to_chat`, `test_without_a_definite_answer_intentions_goes_on_to_chat_unchanged`. The flip commit touches the files listed in 2e-9 and nothing else.


## File map

| File | Responsibility | Tasks |
|---|---|---|
| `nous/brain/continuation.py` | `cancel_root`, `cancelled_root_ids`, `find_root_id`, `stray_dag_ids`, `list_roots`; the proposal fix in `_expire_root`; the late-result rule (gate, `record_result`, stranded settle); `close_cancelled_source` and `end_hanging_root`; the failed-attempt charge; `question_window_start`; `stalled_approved_ids`; the rollback's undeliverable stamp; the flag constant | 2e-1, 2e-2, 2e-4, 2e-5, 2e-6, 2e-7, 2e-9 |
| `nous/heart/result_reconciler.py` | `_close_cancelled` delegates to the store | 2e-2 |
| `nous/heart/result_inbox.py` | `metrics()` counts the rollback's and the cancel's stamps apart from `delivered` | 2e-6 |
| `nous/api/runner.py` | the view of cancelled roots, its check in `_authorize_tool_call`, `discard_conversation`, no restore of an `intent-` thread | 2e-3 |
| `nous/cognitive/ledger_store.py` | `root_cancelled` joins `REFUSAL_CODES` | 2e-3 |
| `nous/storage/models.py`, `sql/migrations/085_…sql` | `Intention.failed_tokens`; the index of cancelled roots | 2e-4 |
| `nous/handlers/continuation_runner.py` | `discard_conversation` at the top of `_turn`; the failed attempt's tokens; `cancel_root`, the view, the cancel sweep; the approved-resume step; the push in its own task | 2e-3 to 2e-6 |
| `nous/main.py` | the view and the orchestrator's cancel wired (inert), the rollback's log | 2e-5, 2e-6, 2e-9 |
| `nous/owner_actions.py`, `nous/api/intention_routes.py` | `CANCEL_REFUSALS`, `GET /intentions`, `POST /intentions/{root_id}/cancel` | 2e-7 |
| `nous/telegram_bot.py` | `/intentions`, `/cancel_intention`, `describe_intentions`, `describe_cancel` | 2e-8 |
| Tests (new) | `tests/test_f099_phase2e_{cancel,late_results,authorize,failed_tokens,runner_cancel,residuals,routes,bot,parity,flip}.py` | all |
| Tests (edited) | `tests/test_f099_phase2b_record_result.py`, `…2c_gate.py`, `…2c_repair.py`, `…2c_expiry_wake.py`, `…2d_parity.py`, `…2b_settings.py`, `…2c_parity.py` | 2e-2, 2e-4, 2e-9 |
| Docs | `docs/reference/{environment-variables,rest-api,shipped-features}.md`, `docs/features/INDEX.md`, the Phase 2 contract (supersession block) | 2e-9 |

---

## Implementer notes

**Branch** `feat/f099-phase2e-cancel-and-flip`, from `origin/main` (check `git log --oneline origin/main -3` shows `feat(F099): Phase 2d` at the top, and `python -c "from nous.handlers.continuation_runner import ContinuationRunner"` imports). Never branch off an in-flight PR.

**Scripts** live in the test-lane script directory provided at hand-off; call that `$BIN` below, and `$MAIN_VENV` is the main checkout's virtualenv. Run them from Git Bash.

**Your own database, before any targeted run.** Create one database per implementer and never share it: other agents use the same Postgres, and some tests `LOCK TABLE`. 2e adds migration 085, so apply `081` and up (the loop does) after Task 2e-4 changes the file list, and **re-apply 085 to a database you made earlier**.

```bash
WT=<path to your worktree>
DB=f099_2e_<yourname>              # unique, lowercase
docker exec nous-postgres psql -U nous -d postgres -qc "DROP DATABASE IF EXISTS $DB" -qc "CREATE DATABASE $DB TEMPLATE nous_fix_base"
for f in $(ls "$WT"/sql/migrations/*.sql | sort); do
  n=$(basename "$f" | cut -c1-3)
  [ "$((10#$n))" -ge 81 ] && docker exec -i nous-postgres psql -U nous -d "$DB" -v ON_ERROR_STOP=1 -q < "$f"
done
```

**Targeted run.** `"$BIN/nous-test-linux.sh" "$WT" runner pg "$DB" tests/test_f099_phase2e_cancel.py -q`. You may add `-k <name>`. The bot tests need no database: `"$BIN/nous-test-linux.sh" "$WT" runner sqlite - tests/test_f099_phase2e_bot.py -q`. Pass test files by name: the lane does not expand a glob.

**Full gate** (once, before review). `"$BIN/gate-with-migrations.sh" f099-2e:"$WT":<fresh_db>:81`. Compare failures with a gate of the base. A failure that is also on the base is not yours (CI is the final gate).

**Lint.** `"$BIN/lint-delta.sh" "$WT"` must say `clean`.
```bash
RUFF="$MAIN_VENV/Scripts/ruff.exe"
"$RUFF" check --config "$WT/pyproject.toml" --fix <your new test files>
"$RUFF" format --config "$WT/pyproject.toml" <your new test files and the format-clean files you edited>
```

**How the code is given.** A new file is given whole under **Create**. A change to an existing file is a unified diff under **Apply to** (it is exact: `git apply --ignore-whitespace` takes it, or make the same edit by hand; the diff's context lines say where). Both are verbatim: the plan was applied in order to a scratch copy of `129d0776`, formatted, and every test below passed on a scratch database.

**Shared test helpers.** `tests/f099_support.py` is not edited by 2e. Test files import from it as `from f099_support import …`; a fixture is imported by name and marked `# noqa: F401`, with `# noqa: F811` on the parameter that shadows it. `until_a_backend_waits_on_a_lock` is there.

**Reading order for a task.** Read the task's Interfaces first, then the existing function it extends (the plan names it), then the tests, then the code.

---

## Task 2e-1: `cancel_root` in the store, and the proposals that move under the root lock

**Prod runs:** nothing new. `cancel_root`, `cancelled_root_ids` and `find_root_id` are called by nothing yet, and the one edit to existing code, `_expire_root`'s proposal UPDATEs (with `expire_roots`' new optional `proposals_out`), is reached only through `expire_roots`, which only the continuation runner calls (prod builds none).

**Files:**
- Modify: `nous/brain/continuation.py` (the `Schedule` import and `dataclasses.field`; `_SHOWN_PROPOSAL_STATES` and `_STARTABLE_PROPOSAL_STATES`; the proposal UPDATEs of `_expire_root` and the `proposals_out` of `expire_roots`; a new section at the end of the module)
- Create: `tests/test_f099_phase2e_cancel.py`

**Interfaces:**
- Consumes: `_lock_root`, `intention_keyed`, `normalize_id`, `_unique`, `OPEN_STATES`, `PROPOSAL_*`, `INTENT_SESSION_PREFIX`, `intentions.WAKE_CONTAINER`, `intentions.TERMINAL_DAG_STATUSES`, `Schedule`, `Subtask`, `ExecutionDAG`.
- Produces:
```python
REFUSE_FINISHED = "finished"
CANCEL_ROOTS_MAX = 50
CANCELLED_VIEW_MAX = 10_000
class RootNotFound(LookupError): ...
class CancelRefused(Exception):  # .reason == REFUSE_FINISHED
@dataclass(frozen=True, slots=True)
class CancelOutcome:
    root_id: UUID
    already_cancelled: bool = False
    cancelled_intentions: int = 0
    cancelled_subtasks: int = 0
    cancelled_dags: int = 0          # the runner fills it
    cancelled_proposals: int = 0
    deactivated_schedules: int = 0
    turn_stopped: bool = False       # the runner fills it
    dag_ids: tuple[UUID, ...] = ()       # the lineage's DAGs still running: the runner cancels them
    proposal_ids: tuple[UUID, ...] = ()  # the pending or approved proposals the owner had been shown
    root_ids: tuple[UUID, ...] = ()      # the root and the fires of its containers: all marked
async def cancel_root(session, agent_id, root_id, *, reason: str, actor: str, now: datetime | None = None) -> CancelOutcome
async def cancelled_root_ids(session, agent_id, *, since: datetime | None = None, limit: int = CANCELLED_VIEW_MAX) -> list[UUID]
async def find_root_id(session, agent_id, prefix: str) -> UUID | None   # a ROOT only; AmbiguousId for two
SILENT_SESSION_ID = "cancelled"   # delivered_session_id of a row a cancel closed (and, in 2e-2, of the twin of a dropped late result): "closed by the cancel", never "delivered"
async def expire_roots(session, agent_id, *, ttl_hours, settings, limit=EXPIRE_BATCH, now=None, proposals_out: list[tuple[UUID, str]] | None = None) -> list[UUID]
    # proposals_out receives (proposal_id, "expired") for each proposal the owner had been shown that an expiry ended
```

**Three rules this task also carries** (from the plan review): the cancel closes the lineage's **unsent owner-facing rows** (a REPORT, QUESTION or PROPOSAL, which are channel-keyed, so the intention-keyed stamp does not reach them), stamping `delivered_at`, `delivered_session_id = SILENT_SESSION_ID` and `pushed_at`, so a push that quiet hours deferred or that waits for a retry never goes out for cancelled work and F098's chat claim never injects the row (a row already pushed keeps its `pushed_at`; `push_message_id` stays NULL, so a reply to a message that was never sent resolves to nothing); `_expire_root`'s proposal move keeps its one transaction but writes `decided_at` and `decided_by = 'system'` on the shown ones (the proposals sweep used to) and reports them through `proposals_out` (the runner tells the bus in 2e-5); and the docstring says what a cancel cannot take back: an `executing` call is left alone, a send in flight completes and its outcome is written to nobody.

- [ ] **Step 1: Write the tests**

**Create `tests/test_f099_phase2e_cancel.py`:**

```python
"""F099 Phase 2e-1: cancel in the store (spec 4.6, T13): the cascade and the proposals under the root lock."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from f099_support import (
    CONT,
    ask_with_proposals,
    claimed,
    commit_ask,
    env_factory,  # noqa: F401
    finish,
    inbox_rows,
    intention_of,
    make_child,
    make_dag,
    make_root,
    proposal_row,
    record,
    set_intention,
    stage,
    until_a_backend_waits_on_a_lock,
)
from sqlalchemy import select, update

from nous.brain import continuation, intentions
from nous.brain.continuation import Resolution
from nous.brain.intentions import IntentionSpec
from nous.storage.models import Intention, IntentionArrival, IntentionProposal, ResultInbox

pytestmark = pytest.mark.postgres_only  # FOR NO KEY UPDATE, savepoints, = ANY(array)


async def _cancel(env, root_id, *, reason="test", actor="owner-test"):
    async with env.db.session() as s:
        out = await continuation.cancel_root(s, env.agent, root_id, reason=reason, actor=actor)
        await s.commit()
    return out


async def _row(env, intention_id) -> Intention:
    async with env.db.session() as s:
        return (
            await s.execute(
                select(Intention).where(Intention.id == intention_id).execution_options(populate_existing=True)
            )
        ).scalar_one()


async def _set_proposal(env, proposal_id, **values):
    async with env.db.session() as s:
        await s.execute(update(IntentionProposal).where(IntentionProposal.id == proposal_id).values(**values))
        await s.commit()


async def _decide(env, proposal_id, *, approve=True):
    async with env.db.session() as s:
        out = await continuation.decide_proposal(
            s, env.agent, proposal_id, approve=approve, actor="owner-test", settings=env.settings
        )
        await s.commit()
    return out


async def _claim_execution(env, proposal_id):
    async with env.db.session() as s:
        row = await continuation.claim_execution(s, env.agent, proposal_id)
        await s.commit()
    return row


async def _schedule_container(env):
    """A live recurring schedule and its container intention (a root with the ``container`` policy)."""
    schedule = await env.heart.schedules.create(
        task="watch the snow",
        schedule_type="recurring",
        interval_seconds=1800,
        intention=IntentionSpec(intent="Watch the snow", origin_kind="interactive", container=True),
    )
    container = await intention_of(env, "schedule", schedule.id)
    return schedule, container


def _fire_spec(schedule) -> IntentionSpec:
    return IntentionSpec(
        intent="Snow check",
        origin_kind="scheduler",
        parent_source=("schedule", str(schedule.id)),
        wake_policy="remember",
    )


async def _fire(env, schedule):
    """One schedule fire: a new root under the container, with a pending subtask."""
    st = await env.heart.subtasks.create(task="snow check", intention=_fire_spec(schedule))
    return st, await intention_of(env, "subtask", st.id)


# ---- the cascade ---------------------------------------------------------------------------------------------


async def test_a_cancel_moves_the_whole_lineage_and_stops_its_work(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    root = await make_root(env)
    child = await make_child(env, root)
    other = await make_root(env)  # another lineage: untouched

    out = await _cancel(env, root.id)

    assert (out.already_cancelled, out.cancelled_intentions, out.cancelled_subtasks) == (False, 2, 2)
    assert out.root_ids == (root.id,) and out.dag_ids == () and out.proposal_ids == ()
    for fresh in (await _row(env, root.id), await _row(env, child.id)):
        assert (fresh.state, fresh.close_reason, fresh.claim_token) == ("cancelled", "cancelled", None)
        assert fresh.closed_at is not None
    assert (await _row(env, root.id)).root_cancelled_at is not None
    assert (await _row(env, child.id)).root_cancelled_at is None  # the marker lives on the root row only
    for source_id in (root.source_id, child.source_id):
        stopped = await env.heart.subtasks.get(uuid.UUID(source_id))
        assert (stopped.status, stopped.final_outcome) == ("cancelled", "cancelled") and stopped.completed_at
    assert (await _row(env, other.id)).state == "pending"
    assert (await env.heart.subtasks.get(uuid.UUID(other.source_id))).status == "pending"


async def test_a_cancel_leaves_a_finished_subtask_and_its_result_as_they_were(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    root = await make_root(env)
    child = await make_child(env, root)
    done = await env.heart.subtasks.get(uuid.UUID(child.source_id))
    await env.heart.subtasks.complete(done.id, "40 cm", final_outcome="completed")
    out = await _cancel(env, root.id)
    assert out.cancelled_subtasks == 1  # the root's own subtask, which was still pending
    assert (await env.heart.subtasks.get(done.id)).status == "completed"


async def test_a_cancel_clears_a_live_claim_so_the_turns_commit_loses_its_fence(env_factory):  # noqa: F811
    """Review Focus 3: the claim token is cleared in the cancel, and everything a fenced write needs goes with it."""
    env = await env_factory(**CONT)
    root, got = await claimed(env)
    pid = await stage(env, got)
    assert (await intention_of(env, "subtask", root.source_id)).state == "deciding"

    out = await _cancel(env, root.id)

    assert out.cancelled_intentions == 1 and out.cancelled_proposals == 1
    assert (await proposal_row(env, pid)).state == "cancelled"
    async with env.db.session() as s:
        done = await continuation.commit_arrival(
            s,
            env.agent,
            got,
            resolution=Resolution("drop", "gone", False, 0.5),
            outcome="resolved",
            settings=env.settings,
        )
        await s.commit()
    assert done is None  # the fence: nothing of the turn is written
    async with env.db.session() as s:
        arrivals = (await s.execute(select(IntentionArrival).where(IntentionArrival.root_id == root.id))).all()
    assert arrivals == []


async def test_a_cancel_stamps_the_unread_results_and_reports_nothing(env_factory):  # noqa: F811
    """The unified late-result rule, the cancel half: a cancelled root says nothing it has not said."""
    env = await env_factory(**CONT)
    root = await make_root(env)
    await record(env, root)
    (unread,) = [r for r in await inbox_rows(env) if r.intention_id == root.id]
    assert unread.delivered_at is None

    await _cancel(env, root.id)

    rows = await inbox_rows(env)
    (stamped,) = [r for r in rows if r.intention_id == root.id]
    assert stamped.delivered_at is not None and stamped.delivered_session_id == f"intent-{root.id}"
    assert [r for r in rows if r.source_kind == "intention_report"] == []


async def test_a_cancel_cancels_every_proposal_that_could_still_start(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    asked = await ask_with_proposals(env, count=4)
    pending, approved, staged, executing = asked.ids
    await _decide(env, approved, approve=True)
    await _set_proposal(env, staged, state="staged")
    await _set_proposal(env, executing, state="executing")

    out = await _cancel(env, asked.root.id)

    assert out.cancelled_proposals == 3 and set(out.proposal_ids) == {pending, approved}  # staged was never shown
    assert [(await proposal_row(env, p)).state for p in (pending, approved, staged, executing)] == [
        "cancelled",
        "cancelled",
        "cancelled",
        "executing",  # the call has started: a cancel cannot take it back
    ]
    assert (await proposal_row(env, approved)).decided_by == "system"


async def test_a_cancel_reports_the_running_dags_and_not_the_finished_ones(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    root = await make_root(env)
    running, _store = await make_dag(env, status="running", parent=root)
    done, _store = await make_dag(env, status="completed", parent=root)
    out = await _cancel(env, root.id)
    assert out.dag_ids == (running.id,) and done.id not in out.dag_ids
    assert (await intention_of(env, "dag", running.id)).state == "cancelled"


async def test_a_finished_root_is_refused_and_nothing_is_written(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    root = await make_root(env)
    await finish(env, await env.heart.subtasks.get(uuid.UUID(root.source_id)))
    await set_intention(env, root.id, state="closed", close_reason="resolved", closed_at=datetime.now(UTC))
    with pytest.raises(continuation.CancelRefused) as refused:
        await _cancel(env, root.id)
    assert refused.value.reason == continuation.REFUSE_FINISHED
    assert (await _row(env, root.id)).root_cancelled_at is None  # no marker on a finished root


async def test_a_repeated_cancel_is_allowed_and_says_so(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    root = await make_root(env)
    first = await _cancel(env, root.id)
    again = await _cancel(env, root.id)
    assert (first.already_cancelled, again.already_cancelled) == (False, True)
    assert (again.cancelled_intentions, again.cancelled_subtasks) == (0, 0)
    stamp = (await _row(env, root.id)).root_cancelled_at
    await _cancel(env, root.id)
    assert (await _row(env, root.id)).root_cancelled_at == stamp  # the marker is written once


async def test_only_a_root_can_be_cancelled(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    root = await make_root(env)
    child = await make_child(env, root)
    for bad in (child.id, uuid.uuid4()):
        with pytest.raises(continuation.RootNotFound):
            await _cancel(env, bad)
    assert (await _row(env, child.id)).state == "pending"


async def test_the_view_of_cancelled_roots_and_the_root_lookup(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    one, two, kept = await make_root(env), await make_root(env), await make_root(env)
    child = await make_child(env, one)
    await _cancel(env, one.id)
    await _cancel(env, two.id)
    async with env.db.session() as s:
        every = await continuation.cancelled_root_ids(s, env.agent)
        recent = await continuation.cancelled_root_ids(s, env.agent, since=datetime.now(UTC) + timedelta(seconds=5))
        found = await continuation.find_root_id(s, env.agent, one.id.hex[:10])
        missing_child = await continuation.find_root_id(s, env.agent, child.id.hex[:12])
        nonsense = await continuation.find_root_id(s, env.agent, "not hex")
    assert set(every) == {one.id, two.id} and kept.id not in every and recent == []
    assert (found, missing_child, nonsense) == (one.id, None, None)


async def test_a_cancel_closes_the_lineages_unsent_owner_rows_so_nothing_is_pushed_or_claimed(env_factory):  # noqa: F811
    """M1 of the plan review. An owner-facing row (REPORT, QUESTION, PROPOSAL) is keyed to a channel, so the stamp of
    the intention-keyed rows does not reach it: a push that quiet hours deferred would go out for cancelled work, and
    F098's chat claim would read it into the next chat turn."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    from f099_support import CHAN, commit_ask

    from nous.handlers.continuation_publisher import OwnerPublisher

    env = await env_factory(**CONT, telegram_bot_token="test-token")
    root, got = await claimed(env)
    await commit_ask(env, got)
    (question,) = [r for r in await inbox_rows(env) if r.msg_type == "QUESTION"]
    later = datetime.now(UTC) + timedelta(hours=1)
    async with env.db.session() as s:
        await s.execute(update(ResultInbox).where(ResultInbox.id == question.id).values(push_after=later))
        report_id = await continuation.insert_report(
            s,
            env.agent,
            kind="REPORT",
            title="Update",
            body="already on Telegram",
            channel=CHAN,
            intention_id=root.id,
            root_id=root.id,
            push_after=datetime.now(UTC) - timedelta(minutes=5),
        )
        await s.execute(
            update(ResultInbox)
            .where(ResultInbox.source_id == report_id)
            .values(pushed_at=datetime.now(UTC), push_message_id=7)
        )
        await s.commit()

    pushed_before = await _stored_pushed_at(env, report_id)
    await _cancel(env, root.id)

    rows = {r.msg_type: r for r in await inbox_rows(env) if r.source_kind == "intention_report"}
    assert rows["QUESTION"].delivered_session_id == continuation.SILENT_SESSION_ID and rows["QUESTION"].pushed_at
    assert rows["QUESTION"].push_message_id is None  # a reply to a message that was never sent resolves to nothing
    assert rows["REPORT"].delivered_session_id == continuation.SILENT_SESSION_ID
    assert rows["REPORT"].pushed_at == pushed_before  # a push that happened keeps its time
    http = MagicMock()
    http.post = AsyncMock(return_value=SimpleNamespace(status_code=200, json=lambda: {"result": {"message_id": 9}}))
    publisher = OwnerPublisher(database=env.db, settings=env.settings, http_client=http)
    assert await publisher.push_due(now=later + timedelta(seconds=1)) == 0
    http.post.assert_not_called()  # the deferred question is never pushed
    claimed_rows, _ = await env.heart.result_inbox.claim(channel=CHAN, session_id="S9", max_age_hours=72, max_items=10)
    assert claimed_rows == []  # and no chat turn reads it either


async def _stored_pushed_at(env, source_id):
    async with env.db.session() as s:
        return (await s.execute(select(ResultInbox.pushed_at).where(ResultInbox.source_id == source_id))).scalar_one()


# ---- containers and fires ------------------------------------------------------------------------------------


async def test_a_cancelled_container_deactivates_its_schedule_and_cancels_its_fires(env_factory):  # noqa: F811
    """Each fire is its own root, so the root cascade alone would not reach it (spec 4.6)."""
    env = await env_factory(**CONT)
    schedule, container = await _schedule_container(env)
    st, fire = await _fire(env, schedule)
    assert fire.root_id == fire.id and fire.parent_id == container.id
    _closed_st, closed_fire = await _fire(env, schedule)
    await set_intention(env, closed_fire.id, state="closed", close_reason="delivered", closed_at=datetime.now(UTC))

    out = await _cancel(env, container.id)

    assert out.deactivated_schedules == 1 and set(out.root_ids) == {container.id, fire.id}
    assert (await env.heart.schedules.get(schedule.id)).active is False
    assert (await _row(env, container.id)).state == "cancelled"
    cancelled = await _row(env, fire.id)
    assert (cancelled.state, cancelled.root_cancelled_at is not None) == ("cancelled", True)
    assert (await env.heart.subtasks.get(st.id)).status == "cancelled"
    assert (await _row(env, closed_fire.id)).root_cancelled_at is None  # a fire that was done is left alone


async def test_a_fire_in_flight_is_part_of_the_cancel(env_factory):  # noqa: F811
    """Lock order container, schedule: a fire holds both FOR SHARE, so the cancel waits and then finds its root."""
    env = await env_factory(**CONT)
    schedule, container = await _schedule_container(env)
    async with env.db.session() as firing:
        prepared = await intentions.prepare_intention(firing, env.agent, _fire_spec(schedule))
        await intentions.insert_prepared(firing, env.agent, prepared, source_kind="subtask", source_id=uuid.uuid4())
        cancel = asyncio.create_task(_cancel(env, container.id))
        try:
            await asyncio.wait_for(until_a_backend_waits_on_a_lock(env), timeout=10)
        finally:
            await firing.commit()
    out = await asyncio.wait_for(cancel, timeout=30)
    assert prepared.id in out.root_ids
    assert (await _row(env, prepared.id)).root_cancelled_at is not None


async def test_a_cancel_in_flight_refuses_a_fire(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    schedule, container = await _schedule_container(env)
    async with env.db.session() as canceller:
        await continuation.cancel_root(canceller, env.agent, container.id, reason="t", actor="t")
        fire = asyncio.create_task(_fire(env, schedule))
        try:
            await asyncio.wait_for(until_a_backend_waits_on_a_lock(env), timeout=10)
        finally:
            await canceller.commit()
    with pytest.raises(intentions.IntentionRootClosed):
        await asyncio.wait_for(fire, timeout=30)
    assert (await env.heart.schedules.get(schedule.id)).active is False


async def test_a_container_inside_a_lineage_is_cancelled_with_it(env_factory):  # noqa: F811
    """A lineage that scheduled something: the container is a child row of the root, and its fires are other roots."""
    env = await env_factory(**CONT)
    root = await make_root(env)
    schedule = await env.heart.schedules.create(
        task="inner",
        schedule_type="recurring",
        interval_seconds=1800,
        intention=IntentionSpec(
            intent="Inner schedule",
            origin_kind="continuation",
            container=True,
            parent_id=root.id,
            origin_authority="internal_only",
        ),
    )
    _st, fire = await _fire(env, schedule)
    out = await _cancel(env, root.id)
    assert out.deactivated_schedules == 1 and set(out.root_ids) == {root.id, fire.id}
    assert (await env.heart.schedules.get(schedule.id)).active is False


# ---- the races ------------------------------------------------------------------------------------------------


async def test_a_cancel_not_yet_committed_stops_an_approved_call_from_starting(env_factory):  # noqa: F811
    """2d-3 review m1, a hard requirement. claim_execution's root-open predicate sees only a COMMITTED marker, so the
    cancel must move the proposal in its own transaction: the claim then waits on the row and fails its re-check."""
    env = await env_factory(**CONT)
    asked = await ask_with_proposals(env)
    (pid,) = asked.ids
    await _decide(env, pid, approve=True)
    async with env.db.session() as holder:
        await continuation.cancel_root(holder, env.agent, asked.root.id, reason="t", actor="t")  # uncommitted
        claim = asyncio.create_task(_claim_execution(env, pid))
        try:
            await asyncio.wait_for(until_a_backend_waits_on_a_lock(env), timeout=10)
        finally:
            await holder.commit()
    assert await asyncio.wait_for(claim, timeout=30) is None
    assert (await proposal_row(env, pid)).state == "cancelled"


async def test_an_expiry_not_yet_committed_stops_an_approved_call_from_starting(env_factory):  # noqa: F811
    """The same window in _expire_root: its marker and its proposals move in one transaction under the root lock."""
    env = await env_factory(**CONT)
    asked = await ask_with_proposals(env, count=2)
    approved, pending = asked.ids
    await _decide(env, approved, approve=True)
    later = datetime.now(UTC) + timedelta(hours=100)
    async with env.db.session() as holder:
        expired = await continuation.expire_roots(
            holder, env.agent, ttl_hours=env.settings.intention_root_ttl_hours, settings=env.settings, now=later
        )
        assert expired == [asked.root.id]
        claim = asyncio.create_task(_claim_execution(env, approved))
        try:
            await asyncio.wait_for(until_a_backend_waits_on_a_lock(env), timeout=10)
        finally:
            await holder.commit()
    assert await asyncio.wait_for(claim, timeout=30) is None
    assert [(await proposal_row(env, p)).state for p in (approved, pending)] == ["expired", "expired"]
    for proposal_id in (approved, pending):  # S3: the proposals sweep used to write these; the expiry does now
        row = await proposal_row(env, proposal_id)
        assert (row.decided_by, row.decided_at is not None) == ("system", True)


async def test_the_expiry_names_the_proposals_it_ended_for_the_bus_and_not_the_ones_never_shown(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    asked = await ask_with_proposals(env, count=2)
    shown, never_shown = asked.ids
    await _set_proposal(env, never_shown, state="staged")
    ended: list = []
    async with env.db.session() as s:
        await continuation.expire_roots(
            s,
            env.agent,
            ttl_hours=env.settings.intention_root_ttl_hours,
            settings=env.settings,
            now=datetime.now(UTC) + timedelta(hours=100),
            proposals_out=ended,
        )
        await s.commit()
    assert ended == [(shown, "expired")]
    assert (await proposal_row(env, never_shown)).state == "expired"  # ended, but the owner never saw it


async def test_a_spawn_in_flight_makes_the_cancel_wait_and_is_cancelled_with_the_lineage(env_factory):  # noqa: F811
    """I1: a spawn reads the root FOR SHARE, which conflicts with the cancel's FOR NO KEY UPDATE. The cancel waits,
    then reads the lineage and finds the new child: nothing a spawn in flight made escapes it."""
    env = await env_factory(**CONT)
    root = await make_root(env)
    spec = IntentionSpec(
        intent="next step", origin_kind="continuation", parent_id=root.id, origin_authority="internal_only"
    )
    async with env.db.session() as spawning:
        prepared = await intentions.prepare_intention(spawning, env.agent, spec)
        await intentions.insert_prepared(spawning, env.agent, prepared, source_kind="subtask", source_id=uuid.uuid4())
        cancel = asyncio.create_task(_cancel(env, root.id))
        try:
            await asyncio.wait_for(until_a_backend_waits_on_a_lock(env), timeout=10)
        finally:
            await spawning.commit()
    out = await asyncio.wait_for(cancel, timeout=30)
    assert out.cancelled_intentions == 2
    assert (await _row(env, prepared.id)).state == "cancelled"


async def test_a_cancel_in_flight_refuses_a_spawn(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    root = await make_root(env)
    async with env.db.session() as canceller:
        await continuation.cancel_root(canceller, env.agent, root.id, reason="t", actor="t")
        spawn = asyncio.create_task(make_child(env, root))
        try:
            await asyncio.wait_for(until_a_backend_waits_on_a_lock(env), timeout=10)
        finally:
            await canceller.commit()
    with pytest.raises(intentions.IntentionRootClosed):
        await asyncio.wait_for(spawn, timeout=30)


async def test_a_decision_after_a_cancel_is_refused_as_ended(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    asked = await ask_with_proposals(env)
    (pid,) = asked.ids
    await _cancel(env, asked.root.id)
    out = await _decide(env, pid, approve=True)
    assert (out.state, out.changed, out.refusal) == ("cancelled", False, "ended")
    assert await _claim_execution(env, pid) is None


async def test_an_answer_after_a_cancel_is_refused_and_writes_nothing(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    root, got = await claimed(env)
    await commit_ask(env, got)
    (question,) = [r for r in await inbox_rows(env) if r.msg_type == "QUESTION"]
    await _cancel(env, root.id)
    before = len(await inbox_rows(env))
    async with env.db.session() as s:
        with pytest.raises(continuation.AnswerRefused) as refused:
            await continuation.record_answer(
                s, env.agent, question.source_id, text="yes", actor="t", settings=env.settings
            )
    assert refused.value.reason == "ended" and len(await inbox_rows(env)) == before


async def test_a_claim_after_a_cancel_finds_nothing_to_claim(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    root = await make_root(env)
    await record(env, root)
    await _cancel(env, root.id)
    from f099_support import claim

    assert await claim(env, root.id) is None


async def test_cancel_and_ask_leave_a_woken_arrival_nothing_to_wake(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    root, got = await claimed(env)
    await commit_ask(env, got)  # a question: awaiting_owner
    assert (await intention_of(env, "subtask", root.source_id)).state == "awaiting_owner"
    await _cancel(env, root.id)
    async with env.db.session() as s:
        woken = await continuation.wake_terminal_arrivals(s, env.agent, settings=env.settings)
        await s.commit()
    assert woken == []
    assert (await intention_of(env, "subtask", root.source_id)).state == "cancelled"
```

- [ ] **Step 2: Run them and watch them fail**

`"$BIN/nous-test-linux.sh" "$WT" runner pg "$DB" tests/test_f099_phase2e_cancel.py -q` fails at import (`continuation.cancel_root` does not exist).

- [ ] **Step 3: Implement**

The order inside `_cancel_lineage` is the point: root lock, lineage read, containers then schedules, the open rows, the subtasks, the DAG ids, the marker, the proposals, the unread rows, the fires. `cancel_root` runs it in a SAVEPOINT so a refusal leaves nothing behind.

**Apply to `nous/brain/continuation.py`:**

```diff
diff --git a/nous/brain/continuation.py b/nous/brain/continuation.py
index fbb589dc..27fb6ba5 100644
--- a/nous/brain/continuation.py
+++ b/nous/brain/continuation.py
@@ -16,7 +16,7 @@ import re
 import unicodedata
 import uuid
 from collections.abc import Awaitable, Callable, Mapping
-from dataclasses import dataclass
+from dataclasses import dataclass, field
 from datetime import UTC, datetime, timedelta
 from typing import Any
 from uuid import UUID
@@ -36,6 +36,7 @@ from nous.storage.models import (
     IntentionArrival,
     IntentionProposal,
     ResultInbox,
+    Schedule,
     Subtask,
 )
 
@@ -1374,13 +1375,16 @@ async def expire_roots(
     settings: Any,
     limit: int = EXPIRE_BATCH,
     now: datetime | None = None,
+    proposals_out: list[tuple[UUID, str]] | None = None,
 ) -> list[UUID]:
     """T14: expire the roots whose TTL ran out (spec 4.6), at most ``limit``, one SAVEPOINT per root.
 
     Due: open (neither root marker set), not a container, with an open ``continue`` or ``report``
     intention in its lineage, and ``deadline`` past (a NULL deadline: ``created_at + ttl_hours`` past).
     Then settles the rows held on intentions a gate arrival closed (``_settle_stranded_rows``).
-    Does not commit; the runner emits ``intention.root_expired`` for the returned ids.
+    Does not commit; the runner emits ``intention.root_expired`` for the returned ids. ``proposals_out``, when given,
+    receives ``(proposal_id, "expired")`` for each proposal the owner had been shown that an expiry ended (its decision
+    columns are written, as the proposals sweep wrote them before 2e), for the runner to tell the bus.
     """
     now = now or datetime.now(UTC)
     root = aliased(Intention)
@@ -1405,8 +1409,13 @@ async def expire_roots(
     for root_id in (await session.execute(due)).scalars().all():
         try:
             async with session.begin_nested():
-                if await _expire_root(session, agent_id, root_id, ttl_hours=ttl_hours, settings=settings, now=now):
+                ended: list[tuple[UUID, str]] = []
+                if await _expire_root(
+                    session, agent_id, root_id, ttl_hours=ttl_hours, settings=settings, now=now, shown=ended
+                ):
                     expired.append(root_id)
+                    if proposals_out is not None:
+                        proposals_out.extend(ended)
         except Exception:
             logger.warning("F099: could not expire root %s; retried at the next sweep", root_id, exc_info=True)
     try:
@@ -1497,7 +1506,14 @@ async def _settle_stranded_rows(session: AsyncSession, agent_id: str, *, setting
 
 
 async def _expire_root(
-    session: AsyncSession, agent_id: str, root_id: UUID, *, ttl_hours: float, settings: Any, now: datetime
+    session: AsyncSession,
+    agent_id: str,
+    root_id: UUID,
+    *,
+    ttl_hours: float,
+    settings: Any,
+    now: datetime,
+    shown: list[tuple[UUID, str]],
 ) -> bool:
     row = (
         await session.execute(
@@ -1553,10 +1569,24 @@ async def _expire_root(
         .where(Intention.agent_id == agent_id, Intention.id == root_id)
         .values(root_expired_at=now, updated_at=now)
     )
-    # F099 2d: this closed the root's claim (the token is cleared), so a late commit loses its fence and the
-    # staged proposals of the turn can never be published: expire them with it. Staged rows only: the owner never
-    # saw them. A pending proposal of an ended root is the proposals sweep's (expire_proposals), which also tells
-    # the bus.
+    # F099 2d, 2e: this closed the root's claim (the token is cleared), so a late commit loses its fence and the
+    # staged proposals of the turn can never be published. Every proposal that could still start moves with the
+    # root marker, in THIS transaction and under the root lock (2d-3 review m1): claim_execution's root-open
+    # predicate sees only a marker that has COMMITTED, so a proposal left `approved` here could be claimed by a
+    # call that read the marker before this commit. Its UPDATE of the claim waits on this row instead and then
+    # fails its `state = 'approved'` re-check. `executing` is left alone (the call has started).
+    ended = await session.execute(
+        update(IntentionProposal)
+        .where(
+            IntentionProposal.agent_id == agent_id,
+            IntentionProposal.root_id == root_id,
+            IntentionProposal.state.in_(_SHOWN_PROPOSAL_STATES),
+        )
+        .values(state=PROPOSAL_EXPIRED, decided_at=now, decided_by="system", updated_at=now)
+        .returning(IntentionProposal.id)
+        .execution_options(synchronize_session=False)
+    )
+    shown.extend((proposal_id, PROPOSAL_EXPIRED) for proposal_id in ended.scalars().all())
     await session.execute(
         update(IntentionProposal)
         .where(
@@ -2145,6 +2175,10 @@ async def rollback_at_startup(
 PROPOSAL_STAGED, PROPOSAL_PENDING, PROPOSAL_APPROVED = "staged", "pending", "approved"
 PROPOSAL_EXECUTING, PROPOSAL_REJECTED, PROPOSAL_EXPIRED = "executing", "rejected", "expired"
 PROPOSAL_EXECUTED, PROPOSAL_FAILED, PROPOSAL_CANCELLED = "executed", "failed", "cancelled"
+# The proposals the owner has been shown and may still decide or run, and (with the one that was never shown) the states
+# from which a call can still START: an ended root takes all three with its marker, under the root lock.
+_SHOWN_PROPOSAL_STATES = (PROPOSAL_PENDING, PROPOSAL_APPROVED)
+_STARTABLE_PROPOSAL_STATES = (PROPOSAL_STAGED, *_SHOWN_PROPOSAL_STATES)
 MAX_PROPOSALS_PER_ARRIVAL = 5
 # What the owner is shown must fit one Telegram message whole (4096 UTF-16 units, which is what the caps count: a
 # character above U+FFFF is two): a call that does not is refused at staging, never clipped, because a clipped call
@@ -3231,3 +3265,351 @@ async def list_proposals(session: AsyncSession, agent_id: str, *, state: str, li
         .all()
     )
     return [proposal_view(row) for row in rows]
+
+
+# ---------------------------------------------------------------------------
+# F099 Phase 2e: cancel (spec 4.6)
+# ---------------------------------------------------------------------------
+
+REFUSE_FINISHED = "finished"  # cancel_root's one refusal: nothing under the root was running
+# delivered_session_id of a row a cancel closed, and of the twin of a late result a cancelled root dropped. It says
+# "closed by the cancel", never "delivered": the owner never saw it (``ResultInboxStore.metrics`` counts it apart).
+SILENT_SESSION_ID = "cancelled"
+CANCEL_ROOTS_MAX = 50  # roots one cancel may cascade over: the root, the fires of its containers, and theirs
+CANCELLED_VIEW_MAX = 10_000  # the most cancelled roots one load of the in-process view reads
+
+
+class RootNotFound(LookupError):
+    """No root intention with this id for the agent (a child's id is not a root's)."""
+
+
+class CancelRefused(Exception):
+    """A cancel the store did not make: ``reason`` is ``REFUSE_FINISHED``; nothing was written."""
+
+    def __init__(self, reason: str) -> None:
+        super().__init__(reason)
+        self.reason = reason
+
+
+@dataclass(frozen=True, slots=True)
+class CancelOutcome:
+    """What ``cancel_root`` did (contract section 4.7), plus what the runner needs to finish the cancel.
+
+    The store cancels rows. A DAG is cancelled by the orchestrator, and a running turn by its task, so the store
+    reports ``dag_ids`` (the lineage's DAGs that were still running) and the runner fills ``cancelled_dags`` and
+    ``turn_stopped`` after it acted. ``root_ids`` is every root this call marked (the root, and the fires of its
+    containers): the runner's in-process view takes all of them. ``proposal_ids`` are the proposals the owner had
+    been shown (pending or approved), for the bus."""
+
+    root_id: UUID
+    already_cancelled: bool = False
+    cancelled_intentions: int = 0
+    cancelled_subtasks: int = 0
+    cancelled_dags: int = 0
+    cancelled_proposals: int = 0
+    deactivated_schedules: int = 0
+    turn_stopped: bool = False
+    dag_ids: tuple[UUID, ...] = ()
+    proposal_ids: tuple[UUID, ...] = ()
+    root_ids: tuple[UUID, ...] = ()
+
+
+@dataclass(slots=True)
+class _CancelTally:
+    intentions: int = 0
+    subtasks: int = 0
+    proposals: int = 0
+    schedules: int = 0
+    dag_ids: list[UUID] = field(default_factory=list)
+    proposal_ids: list[UUID] = field(default_factory=list)
+    root_ids: list[UUID] = field(default_factory=list)
+
+    def nothing_was_running(self) -> bool:
+        return not (self.intentions or self.subtasks or self.proposals or self.schedules or self.dag_ids)
+
+
+def _uuid_or_none(value: Any) -> UUID | None:
+    try:
+        return UUID(str(value))
+    except (TypeError, ValueError):
+        return None
+
+
+async def _cancel_lineage(
+    session: AsyncSession, agent_id: str, root_id: UUID, *, now: datetime, tally: _CancelTally
+) -> list[UUID]:
+    """Cancel one root's lineage in the caller's transaction (T13) and return the open roots of its containers'
+    fires, which the caller cancels next.
+
+    One order everywhere (contract 4.7 Locks): the root first, then every container of the lineage, then their
+    schedules, and only then the writes. A fire that is in flight holds its container and its schedule FOR SHARE
+    (``_hold_open_container``), so this waits for it and finds its root below; one that starts later is refused.
+    The lineage is read AFTER the root lock: a spawn in flight holds the root FOR SHARE, and its rows are visible
+    once this has the lock. Then, as ``_expire_root`` does: the open rows close first (a ``record_result`` in
+    flight either committed before and is stamped below, or waits and finds its intention cancelled), then the
+    marker, then the proposals, then the unread rows."""
+    await _lock_root(session, agent_id, root_id)
+    lineage = (
+        await session.execute(
+            select(Intention.id, Intention.source_kind, Intention.source_id, Intention.wake_policy)
+            .where(Intention.agent_id == agent_id, Intention.root_id == root_id)
+            .order_by(Intention.id)
+        )
+    ).all()
+    ids = [row.id for row in lineage]
+    containers = [row for row in lineage if row.wake_policy == intentions.WAKE_CONTAINER]
+    if containers:
+        await session.execute(
+            select(Intention.id)
+            .where(Intention.agent_id == agent_id, Intention.id.in_([c.id for c in containers]))
+            .order_by(Intention.id)
+            .with_for_update(key_share=True)
+        )
+        schedule_ids = [sid for sid in (_uuid_or_none(c.source_id) for c in containers) if sid is not None]
+        if schedule_ids:
+            deactivated = await session.execute(
+                update(Schedule)
+                .where(Schedule.agent_id == agent_id, Schedule.id.in_(schedule_ids), Schedule.active.is_(True))
+                .values(active=False)
+                .returning(Schedule.id)
+                .execution_options(synchronize_session=False)
+            )
+            tally.schedules += len(deactivated.scalars().all())
+    closed = await session.execute(
+        update(Intention)
+        .where(Intention.agent_id == agent_id, Intention.root_id == root_id, Intention.state.in_(OPEN_STATES))
+        .values(
+            state=STATE_CANCELLED,
+            close_reason=CLOSE_CANCELLED,
+            closed_at=now,
+            claim_token=None,
+            claimed_at=None,
+            updated_at=now,
+        )
+        .returning(Intention.id)
+        .execution_options(synchronize_session=False)
+    )
+    tally.intentions += len(closed.scalars().all())
+    subtask_ids = [
+        sid
+        for sid in (_uuid_or_none(r.source_id) for r in lineage if r.source_kind == intentions.SOURCE_SUBTASK)
+        if sid is not None
+    ]
+    if subtask_ids:
+        stopped = await session.execute(
+            update(Subtask)
+            .where(
+                Subtask.agent_id == agent_id, Subtask.id.in_(subtask_ids), Subtask.status.in_(("pending", "running"))
+            )
+            .values(status="cancelled", final_outcome="cancelled", completed_at=now)
+            .returning(Subtask.id)
+            .execution_options(synchronize_session=False)
+        )
+        tally.subtasks += len(stopped.scalars().all())
+    dag_source_ids = [
+        did
+        for did in (_uuid_or_none(r.source_id) for r in lineage if r.source_kind == intentions.SOURCE_DAG)
+        if did is not None
+    ]
+    if dag_source_ids:
+        running = await session.execute(
+            select(ExecutionDAG.id).where(
+                ExecutionDAG.agent_id == agent_id,
+                ExecutionDAG.id.in_(dag_source_ids),
+                ExecutionDAG.status.notin_(intentions.TERMINAL_DAG_STATUSES),
+            )
+        )
+        tally.dag_ids.extend(running.scalars().all())
+    await session.execute(
+        update(Intention)
+        .where(Intention.agent_id == agent_id, Intention.id == root_id, Intention.root_cancelled_at.is_(None))
+        .values(root_cancelled_at=now, updated_at=now)
+        .execution_options(synchronize_session=False)
+    )
+    tally.root_ids.append(root_id)
+    # Everything that could still START: the owner sees pending and approved ones (the bus is told), a staged one
+    # was never shown. Under the root lock and in this transaction, so claim_execution cannot win a race on a
+    # marker it cannot see yet (2d-3 review m1).
+    seen = await session.execute(
+        update(IntentionProposal)
+        .where(
+            IntentionProposal.agent_id == agent_id,
+            IntentionProposal.root_id == root_id,
+            IntentionProposal.state.in_(_SHOWN_PROPOSAL_STATES),
+        )
+        .values(state=PROPOSAL_CANCELLED, decided_at=now, decided_by="system", updated_at=now)
+        .returning(IntentionProposal.id)
+        .execution_options(synchronize_session=False)
+    )
+    shown = seen.scalars().all()
+    tally.proposals += len(shown)
+    tally.proposal_ids.extend(shown)
+    unshown = await session.execute(
+        update(IntentionProposal)
+        .where(
+            IntentionProposal.agent_id == agent_id,
+            IntentionProposal.root_id == root_id,
+            IntentionProposal.state == PROPOSAL_STAGED,
+        )
+        .values(state=PROPOSAL_CANCELLED, updated_at=now)
+        .returning(IntentionProposal.id)
+        .execution_options(synchronize_session=False)
+    )
+    tally.proposals += len(unshown.scalars().all())
+    # A cancelled root reports nothing it has not said (the unified late-result rule): its unread results are
+    # stamped delivered, never shown, never reported.
+    await session.execute(
+        update(ResultInbox)
+        .where(intention_keyed(agent_id, ids), ResultInbox.delivered_at.is_(None))
+        .values(delivered_at=now, delivered_session_id=f"{INTENT_SESSION_PREFIX}{root_id}")
+        .execution_options(synchronize_session=False)
+    )
+    # M1 (plan review): the owner-facing rows of the lineage that nobody has seen (a REPORT, QUESTION or PROPOSAL, keyed
+    # to a channel, so the statement above does not reach them) are closed too. A push that quiet hours deferred, or
+    # that waits for a retry, would otherwise go out for work the owner cancelled (a PROPOSAL with its buttons), and
+    # F098's chat claim would inject the row into the next chat turn. ``push_message_id`` stays NULL, so a reply to a
+    # message that was never sent resolves to nothing; a row already pushed keeps its ``pushed_at``.
+    await session.execute(
+        update(ResultInbox)
+        .where(
+            ResultInbox.agent_id == agent_id,
+            ResultInbox.source_kind == SOURCE_INTENTION_REPORT,
+            ResultInbox.intention_id.in_(ids),
+            ResultInbox.delivered_at.is_(None),
+        )
+        .values(
+            delivered_at=now,
+            delivered_session_id=SILENT_SESSION_ID,
+            pushed_at=func.coalesce(ResultInbox.pushed_at, now),
+        )
+        .execution_options(synchronize_session=False)
+    )
+    if not containers:
+        return []
+    # Only a fire with something open: a fire that finished is not marked (a marker on a finished root would only
+    # silence a later result). A fire that starts after this is refused by its container check.
+    lineage_row = aliased(Intention)
+    still_open = exists().where(
+        lineage_row.agent_id == agent_id, lineage_row.root_id == Intention.id, lineage_row.state.in_(OPEN_STATES)
+    )
+    fires = await session.execute(
+        select(Intention.id)
+        .where(
+            Intention.agent_id == agent_id,
+            Intention.parent_id.in_([c.id for c in containers]),
+            Intention.id == Intention.root_id,
+            Intention.root_cancelled_at.is_(None),
+            still_open,
+        )
+        .order_by(Intention.created_at, Intention.id)  # the one cross-root lock order
+    )
+    return list(fires.scalars().all())
+
+
+async def cancel_root(
+    session: AsyncSession, agent_id: str, root_id: UUID, *, reason: str, actor: str, now: datetime | None = None
+) -> CancelOutcome:
+    """T13: cancel a root and everything under it, in the caller's transaction (spec 4.6). Does not commit.
+
+    One transaction, the root locked first (``FOR NO KEY UPDATE``). It writes ``root_cancelled_at`` and moves every
+    open intention of the lineage to ``cancelled``, which also clears a live claim, so a turn that is deciding loses
+    its fence. It cancels the lineage's pending and running subtasks, moves its ``staged``, ``pending`` and
+    ``approved`` proposals to ``cancelled`` (so no call can start), stamps its unread intention-keyed results
+    delivered without reporting them, closes its unsent owner-facing rows (a REPORT, QUESTION or PROPOSAL that was
+    deferred or waits for a retry is never pushed, and no chat turn claims it: ``SILENT_SESSION_ID``, "closed by the
+    cancel", not "delivered"), and, for every container in the lineage, deactivates its schedule and then
+    cancels the open roots of the container's fires. The lineage's running DAGs are reported in ``dag_ids``: the
+    orchestrator cancels them (the runner calls it after this commits).
+
+    Repeating a cancel is allowed: it cancels whatever is still running and says ``already_cancelled``. A root with
+    nothing running that was not cancelled before is refused (``CancelRefused``, nothing written): a marker on a
+    finished root would only silence a later result. Raises ``RootNotFound`` for an id that is not a root.
+
+    What a cancel cannot take back: a call the owner approved that has already ``executing`` state is left alone (the
+    call has started). It is refused if it has not passed ``_authorize_tool_call`` yet; a send already in flight
+    completes, and its outcome is written to nobody (``_settle_proposal`` writes nothing for an ended root)."""
+    now = now or datetime.now(UTC)
+    root = (
+        await session.execute(
+            select(Intention)
+            .where(Intention.agent_id == agent_id, Intention.id == root_id)
+            .with_for_update(key_share=True)
+            .execution_options(populate_existing=True)
+        )
+    ).scalar_one_or_none()
+    if root is None or root.root_id != root.id:
+        raise RootNotFound(str(root_id))
+    already = root.root_cancelled_at is not None
+    tally = _CancelTally()
+    async with session.begin_nested():  # a refusal rolls the cascade back to here
+        queue: list[UUID] = [root_id]
+        visited: set[UUID] = set()
+        while queue:
+            next_root = queue.pop(0)
+            if next_root in visited:
+                continue
+            if len(visited) >= CANCEL_ROOTS_MAX:
+                logger.warning("F099: a cancel of root %s stopped at %d roots", root_id, CANCEL_ROOTS_MAX)
+                break
+            visited.add(next_root)
+            queue.extend(await _cancel_lineage(session, agent_id, next_root, now=now, tally=tally))
+        if not already and tally.nothing_was_running():
+            raise CancelRefused(REFUSE_FINISHED)
+    logger.info(
+        "F099: root %s cancelled by %s (%s): %d intention(s), %d subtask(s), %d DAG(s) to stop, %d proposal(s), "
+        "%d schedule(s)",
+        root_id,
+        actor,
+        (reason or "no reason")[:200],
+        tally.intentions,
+        tally.subtasks,
+        len(tally.dag_ids),
+        tally.proposals,
+        tally.schedules,
+    )
+    return CancelOutcome(
+        root_id=root_id,
+        already_cancelled=already,
+        cancelled_intentions=tally.intentions,
+        cancelled_subtasks=tally.subtasks,
+        cancelled_proposals=tally.proposals,
+        deactivated_schedules=tally.schedules,
+        dag_ids=tuple(tally.dag_ids),
+        proposal_ids=tuple(tally.proposal_ids),
+        root_ids=tuple(tally.root_ids),
+    )
+
+
+async def cancelled_root_ids(
+    session: AsyncSession, agent_id: str, *, since: datetime | None = None, limit: int = CANCELLED_VIEW_MAX
+) -> list[UUID]:
+    """The roots that carry ``root_cancelled_at`` (since ``since`` when given), newest first: what the runner's
+    in-process view of cancelled roots is loaded and refreshed from."""
+    query = select(Intention.id).where(Intention.agent_id == agent_id, Intention.root_cancelled_at.is_not(None))
+    if since is not None:
+        query = query.where(Intention.root_cancelled_at >= since)
+    rows = await session.execute(query.order_by(Intention.root_cancelled_at.desc()).limit(limit))
+    return list(rows.scalars().all())
+
+
+async def find_root_id(session: AsyncSession, agent_id: str, prefix: str) -> UUID | None:
+    """The one ROOT intention whose id starts with ``prefix`` (a child is never matched: the owner cancels roots)."""
+    cleaned = normalize_id(prefix)
+    if cleaned is None:
+        return None
+    ids = (
+        (
+            await session.execute(
+                select(Intention.id)
+                .where(
+                    Intention.agent_id == agent_id,
+                    Intention.id == Intention.root_id,
+                    func.replace(cast(Intention.id, Text), "-", "").like(f"{cleaned}%"),
+                )
+                .limit(2)
+            )
+        )
+        .scalars()
+        .all()
+    )
+    return _unique(list(ids), prefix)
```

- [ ] **Step 4: Run the tests and the neighbours**

`tests/test_f099_phase2e_cancel.py tests/test_f099_phase2c_expiry_wake.py tests/test_f099_phase2d_decisions.py -q`: all pass (24 new).

- [ ] **Step 5: Mutation checks** (each must fail the named tests; restore the file after each)
  1. In `_cancel_lineage`, change `IntentionProposal.state.in_((PROPOSAL_PENDING, PROPOSAL_APPROVED))` to `in_(("nothing",))`: `test_a_cancel_cancels_every_proposal_that_could_still_start`, `test_a_cancel_not_yet_committed_stops_an_approved_call_from_starting` and `test_a_decision_after_a_cancel_is_refused_as_ended` fail.
  2. In `_expire_root`, change `IntentionProposal.state.in_(_STARTABLE_PROPOSAL_STATES)` to `IntentionProposal.state == PROPOSAL_STAGED`: `test_an_expiry_not_yet_committed_stops_an_approved_call_from_starting` fails (the claim does not wait).
  3. Remove the `still_open` condition of the fires query: `test_a_cancelled_container_deactivates_its_schedule_and_cancels_its_fires` fails (a finished fire is marked).
  4. In the owner-rows UPDATE of `_cancel_lineage`, change `ResultInbox.source_kind == SOURCE_INTENTION_REPORT` to `== 'nothing'`: `test_a_cancel_closes_the_lineages_unsent_owner_rows_so_nothing_is_pushed_or_claimed` fails (the deferred question is pushed, and the chat claim reads it).

- [ ] **Step 6: Lint and commit**

`$RUFF format` the two files, `lint-delta.sh` clean, then `git add nous/brain/continuation.py tests/test_f099_phase2e_cancel.py` and commit `feat(F099): 2e-1 cancel_root in the store (the cascade, the proposals under the root lock)`.

---

## Task 2e-2: the unified late-result rule, a lineage left hanging, and the repair's cancel cases

**Prod runs:** nothing new. `record_result` is reached only by the flag-on writers (`record_subtask_result`, `record_dag_result` and the reconciler passes call `route_result` only under `continuation.enabled`), the gate and `_settle_stranded_rows` by the runner, and `close_cancelled_source` by `repair_missing_results`, which returns 0 before any query with the flag off. Pinned in 2e-8 by a test that replaces each with a raiser while a subtask finishes.

**Files:**
- Modify: `nous/brain/continuation.py`, `nous/heart/result_reconciler.py`
- Create: `tests/test_f099_phase2e_late_results.py`
- Modify (changed pins): `tests/test_f099_phase2b_record_result.py`, `tests/test_f099_phase2b_routing.py`, `tests/test_f099_phase2b_writers.py`, `tests/test_f099_phase2c_publisher.py`, `tests/test_f099_phase2c_gate.py`, `tests/test_f099_phase2c_repair.py`, `tests/test_f099_phase2c_expiry_wake.py`

**Interfaces:**
- Produces:
```python
# SILENT_SESSION_ID = "cancelled" is defined in 2e-1 (the cancel's stamp); `record_result` writes it on the twin of a dropped late result
GATE_DROP_REASONS = ("cancelled", "plan_resolved")   # "expired" leaves: an expired claim escalates, with the raw rows
async def close_cancelled_source(session, agent_id, intention, *, settings, now=None) -> int   # 0 or 1; root locked FIRST
async def end_hanging_root(session, agent_id, root_id, *, settings, now=None) -> bool          # caller holds the root
```
- Changes: `record_result` writes the work row's settled twin for any result nothing can reopen and, unless the root is cancelled (marker) or the intention is `cancelled`, also the raw REPORT; `_settle_stranded_rows` stamps the rows of a `cancelled` intention, or of an intention under a root with `root_cancelled_at` (the rule is by marker in all three places), and reports an `expired` one's; `result_reconciler._close_cancelled(database, settings, intention)` delegates to `close_cancelled_source`.

**Thirteen tests of earlier PRs change on purpose** (the rule itself changes what they assert; a test that wanted a REPORT from a CANCELLED root now uses an EXPIRED one, which still reports): in `test_f099_phase2b_routing.py`, `test_a_reported_continue_result_is_not_reselected_by_the_subtask_pass`; in `test_f099_phase2b_writers.py`, `test_a_retried_dag_on_a_cancelled_root_reports_the_raw_result` (renamed `…expired_root…`); in `test_f099_phase2c_publisher.py`, `test_a_reported_late_result_reaches_telegram_too`; in `test_f099_phase2b_record_result.py`, `test_a_re_arrival_on_a_closed_root_becomes_an_intention_report`, `test_a_duplicate_delivery_after_a_root_cancel_writes_no_report` (renamed `…root_expiry…`), `test_a_reported_result_settles_its_work_row_for_the_reconciler_passes`, `test_a_report_of_an_old_result_gets_a_fresh_claim_window_and_its_twin_keeps_the_works_time`, `test_a_cancelled_intention_reports_instead_of_waking` (renamed `test_an_expired_intention_…`) and `test_a_result_with_no_owner_channel_writes_only_the_settled_work_row` move from a cancelled marker to an expired one, which still reports; in `test_f099_phase2c_gate.py`, `test_a_gate_drop_commits_a_drop_and_a_gate_escalation_a_report` moves `expired` to the escalations; in `test_f099_phase2c_expiry_wake.py`, `test_a_row_held_on_an_intention_a_gate_arrival_closed_is_reported_by_the_sweep` expects silence for `cancelled` and an extra report for `expired` (the gate's own); in `test_f099_phase2c_repair.py`, `test_the_repair_leaves_a_row_held_on_a_gate_closed_intention_to_the_sweep` says the same, and `test_a_root_cancel_that_commits_just_before_the_close_is_honoured` is replaced by its lock-ordered form (E14: with the root locked first the old interleaving cannot happen, and in its shape it would deadlock). Each edit is in the diffs below.

- [ ] **Step 1: Write the tests**

**Create `tests/test_f099_phase2e_late_results.py`:**

```python
"""F099 Phase 2e-2: the unified late-result rule (an expired root reports raw, a cancelled root stamps silently),
and the repair's two cancel cases (carry-over 5, 7 and 8)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from f099_support import (
    CHAN,
    CONT,
    add_arrival,
    claim,
    env_factory,  # noqa: F401
    finish,
    inbox_rows,
    intention_of,
    make_child,
    make_root,
    make_subtask,
    record,
    set_intention,
)
from sqlalchemy import select

from nous.brain import continuation
from nous.brain.continuation import Resolution
from nous.heart.result_reconciler import InboxSubtaskPass, repair_missing_results
from nous.storage.models import Intention

pytestmark = pytest.mark.postgres_only  # CAST(text AS uuid) joins, FOR NO KEY UPDATE

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)


async def _reports(env):
    return [r for r in await inbox_rows(env) if r.source_kind == "intention_report"]


async def _gate(env, got):
    async with env.db.session() as s:

        async def plan_outcome_of(_decision_id):
            return None

        return await continuation.gate(s, env.agent, got, settings=env.settings, plan_outcome_of=plan_outcome_of)


async def _commit_gate(env, got, reason):
    resolution, report_text = continuation.gate_inputs(reason, got)
    async with env.db.session() as s:
        done = await continuation.commit_arrival(
            s,
            env.agent,
            got,
            resolution=resolution,
            outcome="resolved",
            gate_reason=reason,
            settings=env.settings,
            report_text=report_text,
        )
        await s.commit()
    return done


async def _sweep(env, **kwargs):
    async with env.db.session() as s:
        out = await continuation.expire_roots(
            s, env.agent, ttl_hours=env.settings.intention_root_ttl_hours, settings=env.settings, **kwargs
        )
        await s.commit()
    return out


# ---- the gate ------------------------------------------------------------------------------------------------


async def test_an_expired_roots_arrival_reports_what_came_back_and_a_cancelled_ones_does_not(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    expired = await make_root(env)
    await record(env, expired, body="Powder: 40 cm.")
    got_expired = await claim(env, expired.id)
    await set_intention(env, expired.id, root_expired_at=datetime.now(UTC))
    cancelled = await make_root(env)
    await record(env, cancelled, body="Powder: 10 cm.")
    got_cancelled = await claim(env, cancelled.id)
    await set_intention(env, cancelled.id, root_cancelled_at=datetime.now(UTC))
    assert await _gate(env, got_expired) == "expired" and await _gate(env, got_cancelled) == "cancelled"

    expired_resolution, expired_text = continuation.gate_inputs("expired", got_expired)
    cancelled_resolution, cancelled_text = continuation.gate_inputs("cancelled", got_cancelled)
    assert (expired_resolution.decision, "Powder: 40 cm." in expired_text) == ("report", True)
    assert (cancelled_resolution.decision, cancelled_text) == ("drop", None)

    await _commit_gate(env, got_expired, "expired")
    await _commit_gate(env, got_cancelled, "cancelled")

    (report,) = await _reports(env)
    assert report.intention_id == expired.id and "Powder: 40 cm." in report.body and "expired" in report.body
    assert (await intention_of(env, "subtask", expired.source_id)).state == "expired"
    assert (await intention_of(env, "subtask", cancelled.source_id)).state == "cancelled"
    # Both arrivals consumed their rows: nothing is left unread for a later sweep to report.
    assert [r.delivered_at is not None for r in await inbox_rows(env) if r.source_kind == "subtask"] == [True, True]


# ---- record_result -------------------------------------------------------------------------------------------


async def test_a_late_result_of_a_cancelled_root_is_stamped_and_never_reported(env_factory):  # noqa: F811
    env = await env_factory(**CONT, telegram_chat_id="8080")
    root = await make_root(env)
    await set_intention(env, root.id, root_cancelled_at=NOW, state="cancelled", close_reason="cancelled")
    recorded = await record(env, root, body="late")
    assert (recorded.inserted, recorded.reported, recorded.state_after) == (False, False, "cancelled")
    assert await _reports(env) == []
    (twin,) = await inbox_rows(env, uuid.UUID(root.source_id))
    assert twin.delivered_at is not None and twin.delivered_session_id == continuation.SILENT_SESSION_ID
    assert twin.channel is None and twin.session_id is None  # no chat turn can claim it
    again = await record(env, root, body="late")
    assert (again.inserted, again.reported) == (False, False) and len(await inbox_rows(env)) == 1


async def test_a_late_result_under_a_cancelled_marker_is_silent_whatever_the_intention_state(env_factory):  # noqa: F811
    """By marker, not only by state: an intention that is still open (or closed) under a cancelled root."""
    env = await env_factory(**CONT, telegram_chat_id="8080")
    root = await make_root(env)
    child = await make_child(env, root)
    await set_intention(env, root.id, root_cancelled_at=NOW)  # the child is still `pending` under it
    await record(env, child, body="late")
    assert await _reports(env) == []
    (twin,) = await inbox_rows(env, uuid.UUID(child.source_id))
    assert twin.delivered_session_id == continuation.SILENT_SESSION_ID


async def test_a_late_result_of_an_expired_root_is_reported_raw(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    root = await make_root(env)
    await set_intention(env, root.id, root_expired_at=NOW, state="expired", close_reason="expired")
    recorded = await record(env, root, body="late snow")
    assert (recorded.inserted, recorded.reported) == (True, True)
    (report,) = await _reports(env)
    assert report.channel == CHAN and "late snow" in report.body


async def test_a_cancelled_marker_wins_over_an_expired_one(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    root = await make_root(env)
    await set_intention(env, root.id, root_cancelled_at=NOW, root_expired_at=NOW)
    await record(env, root, body="late")
    assert await _reports(env) == []


# ---- the stranded rows ---------------------------------------------------------------------------------------


async def _strand(env, root, *, state):
    """A row a gate arrival left behind on an intention it closed (``state``): unread, intention-keyed."""
    await record(env, root, body="stranded")
    await set_intention(
        env,
        root.id,
        state=state,
        close_reason=state,
        root_cancelled_at=NOW if state == "cancelled" else None,
        root_expired_at=NOW if state == "expired" else None,
    )


async def test_the_sweep_stamps_the_rows_stranded_on_a_cancelled_intention_without_a_report(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    root = await make_root(env)
    await _strand(env, root, state="cancelled")
    await _sweep(env)
    assert await _reports(env) == []
    (row,) = await inbox_rows(env, uuid.UUID(root.source_id))
    assert row.delivered_at is not None and row.delivered_session_id == f"intent-{root.id}"
    await _sweep(env)  # and it stays quiet
    assert await _reports(env) == []


async def test_the_sweep_judges_a_stranded_row_by_the_roots_marker_as_well_as_the_intentions_state(env_factory):  # noqa: F811
    """One rule in three places: by marker. An intention that expired before the owner cancelled its root is still
    under a cancelled root, so its stranded row is stamped and nothing is said."""
    env = await env_factory(**CONT)
    root = await make_root(env)
    await _strand(env, root, state="expired")
    await set_intention(env, root.id, root_cancelled_at=NOW)
    await _sweep(env)
    assert await _reports(env) == []
    (row,) = await inbox_rows(env, uuid.UUID(root.source_id))
    assert row.delivered_at is not None


async def test_the_sweep_reports_the_rows_stranded_on_an_expired_intention_once(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    root = await make_root(env)
    await _strand(env, root, state="expired")
    await _sweep(env)
    (report,) = await _reports(env)
    assert "stranded" in report.body
    await _sweep(env)
    assert len(await _reports(env)) == 1


async def test_a_cancelled_roots_unread_results_are_not_reported_by_the_next_sweep(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    root = await make_root(env)
    child = await make_child(env, root)
    await record(env, root)
    await record(env, child)
    async with env.db.session() as s:
        await continuation.cancel_root(s, env.agent, root.id, reason="t", actor="t")
        await s.commit()
    await _sweep(env, now=datetime.now(UTC) + timedelta(hours=100))
    assert await _reports(env) == []
    assert all(r.delivered_at is not None for r in await inbox_rows(env))


# ---- carry-over 7: the repair and a cancelled root ------------------------------------------------------------


async def test_the_repair_leaves_a_cancelled_roots_finished_work_alone_and_does_not_spin(env_factory):  # noqa: F811
    """No `cancelled` arm in the repair: the owner cancelled this work, so a finished subtask with no row has no
    result to deliver, and nothing selects it again."""
    env = await env_factory(**CONT)
    root = await make_root(env, routed=False)
    child = await make_child(env, root)
    done = await env.heart.subtasks.get(uuid.UUID(child.source_id))
    await finish(env, done)  # completed before the cancel; its writer never ran
    async with env.db.session() as s:
        await continuation.cancel_root(s, env.agent, root.id, reason="t", actor="t")
        await s.commit()
    for _ in range(2):
        assert await repair_missing_results(env.db, env.heart.result_inbox, env.settings, limit=50) == 0
    assert await inbox_rows(env) == []
    assert (await intention_of(env, "subtask", child.source_id)).state == "cancelled"


async def test_the_inbox_pass_settles_a_routable_result_of_a_cancelled_root_silently(env_factory):  # noqa: F811
    env = await env_factory(**CONT, telegram_chat_id="8080")
    await env.heart.result_inbox.ensure_enabled_at()
    st = await make_subtask(env)  # routed: it has a parent channel and session
    await finish(env, st)  # the worker hook never ran
    async with env.db.session() as s:
        root = await intention_of(env, "subtask", st.id)
        await continuation.cancel_root(s, env.agent, root.id, reason="t", actor="t")
        await s.commit()
    assert await InboxSubtaskPass(env.db, env.heart.result_inbox, env.settings).run(limit=10) == 0
    assert await _reports(env) == []
    (twin,) = await inbox_rows(env, st.id)
    assert twin.delivered_at is not None and twin.delivered_session_id == continuation.SILENT_SESSION_ID
    assert await InboxSubtaskPass(env.db, env.heart.result_inbox, env.settings).run(limit=10) == 0  # not selected again


# ---- carry-over 8: a lineage left hanging --------------------------------------------------------------------


async def _hang(env, *, decision="continue", with_arrival=True):
    """A root a continuation resolved with `decision` (it waits on `child`), whose child then gets cancelled."""
    root = await make_root(env)
    child = await make_child(env, root)
    await set_intention(env, root.id, state="closed", close_reason="resolved", closed_at=NOW)
    if with_arrival:
        await add_arrival(env, root.id, 1, decision=decision, progress=True)
    return root, child


async def _cancel_source(env, child):
    await env.heart.subtasks.cancel(uuid.UUID(child.source_id))
    return await repair_missing_results(env.db, env.heart.result_inbox, env.settings, limit=50)


async def test_the_last_child_of_a_lineage_waiting_on_it_is_cancelled_and_the_root_is_closed_and_reported(env_factory):  # noqa: F811
    env = await env_factory(**CONT, telegram_chat_id="8080")
    root, child = await _hang(env)
    await _cancel_source(env, child)
    fresh_child = await intention_of(env, "subtask", child.source_id)
    assert (fresh_child.state, fresh_child.close_reason) == ("closed", "legacy")
    async with env.db.session() as s:
        fresh_root = (await s.execute(select(Intention).where(Intention.id == root.id))).scalar_one()
    assert fresh_root.root_expired_at is not None
    (report,) = await _reports(env)
    assert report.msg_type == "REPORT" and root.intent in report.body and report.channel == CHAN
    await _cancel_source(env, child)  # idempotent
    assert len(await _reports(env)) == 1
    recorded = await record(env, root, body="late")  # a late result is now reported raw, never silently reopened
    assert recorded.reported is True


@pytest.mark.parametrize("case", ["drop", "report", "no_arrival", "other_child_open"])
async def test_a_cancelled_child_ends_nothing_the_lineage_is_not_waiting_on(env_factory, case):  # noqa: F811
    env = await env_factory(**CONT, telegram_chat_id="8080")
    root, child = await _hang(
        env,
        decision="drop" if case == "drop" else "report" if case == "report" else "continue",
        with_arrival=case != "no_arrival",
    )
    if case == "other_child_open":
        await make_child(env, root)  # something else is still running
    await _cancel_source(env, child)
    async with env.db.session() as s:
        fresh_root = (await s.execute(select(Intention).where(Intention.id == root.id))).scalar_one()
    assert fresh_root.root_expired_at is None and await _reports(env) == []


async def test_a_cancelled_child_under_a_cancelled_root_reports_nothing(env_factory):  # noqa: F811
    env = await env_factory(**CONT, telegram_chat_id="8080")
    root, child = await _hang(env)
    await set_intention(env, root.id, root_cancelled_at=NOW)
    await _cancel_source(env, child)
    assert (await intention_of(env, "subtask", child.source_id)).close_reason == "cancelled"
    assert await _reports(env) == []


async def test_a_claim_gate_commit_chain_still_closes_a_resolved_arrival(env_factory):  # noqa: F811  # PIN
    """The commit path of an ordinary decision is untouched by the rule: a `drop` closes `resolved`, no report."""
    env = await env_factory(**CONT)
    root = await make_root(env)
    await record(env, root)
    got = await claim(env, root.id)
    async with env.db.session() as s:
        done = await continuation.commit_arrival(
            s,
            env.agent,
            got,
            resolution=Resolution("drop", "n/a", False, 0.5),
            outcome="resolved",
            settings=env.settings,
        )
        await s.commit()
    assert done is not None and await _reports(env) == []
```

Edit the changed pins of earlier PRs:

**Apply to `tests/test_f099_phase2b_record_result.py`:**

```diff
diff --git a/tests/test_f099_phase2b_record_result.py b/tests/test_f099_phase2b_record_result.py
index a4f46e24..5fcb0685 100644
--- a/tests/test_f099_phase2b_record_result.py
+++ b/tests/test_f099_phase2b_record_result.py
@@ -175,7 +175,8 @@ def _split(rows):
     )
 
 
-@pytest.mark.parametrize("marker", ["root_cancelled_at", "root_expired_at"])
+# 2e: the report is the EXPIRED half of the unified late-result rule; a cancelled root is silent (2e tests).
+@pytest.mark.parametrize("marker", ["root_expired_at"])
 async def test_a_re_arrival_on_a_closed_root_becomes_an_intention_report(env_factory, marker):  # noqa: F811
     env = await env_factory(**CONT)
     st = await make_subtask(env)
@@ -223,15 +224,15 @@ async def test_a_new_generation_of_a_dag_closed_as_legacy_reports_and_never_reop
     assert (after.state, after.close_reason) == ("closed", "legacy")
 
 
-async def test_a_duplicate_delivery_after_a_root_cancel_writes_no_report(env_factory):  # noqa: F811
+async def test_a_duplicate_delivery_after_a_root_expiry_writes_no_report(env_factory):  # noqa: F811
     """The REPORT is written only with a newly written settled twin. Generation 0 landed on the continue
-    path and the root was cancelled before anything consumed it: a duplicate of generation 0 (the bus
+    path and the root expired before anything consumed it: a duplicate of generation 0 (the bus
     listener and deliver both write) changes nothing. A real re-arrival reports once, however often it comes."""
     env = await env_factory(**CONT)
     st = await make_subtask(env)
     assert (await _record(env, st)).inserted is True
     it = await intention_of(env, "subtask", st.id)
-    await set_intention(env, it.id, root_cancelled_at=datetime.now(UTC))
+    await set_intention(env, it.id, root_expired_at=datetime.now(UTC))
     again = await _record(env, st)
     assert (again.inbox_id, again.inserted, again.reported) == (None, False, False)
     stamped, reports = _split(await inbox_rows(env))
@@ -277,7 +278,7 @@ async def test_a_reported_result_settles_its_work_row_for_the_reconciler_passes(
     env = await env_factory(**CONT)
     st = await make_subtask(env)
     it = await intention_of(env, "subtask", st.id)
-    await set_intention(env, it.id, state="closed", close_reason="resolved", root_cancelled_at=datetime.now(UTC))
+    await set_intention(env, it.id, state="closed", close_reason="resolved", root_expired_at=datetime.now(UTC))
     await _record(env, st, generation=0)
     (stamped,), _ = _split(await inbox_rows(env))
     assert (stamped.source_kind, stamped.source_id, stamped.source_generation) == ("subtask", st.id, 0)
@@ -295,7 +296,7 @@ async def test_a_report_of_an_old_result_gets_a_fresh_claim_window_and_its_twin_
     env = await env_factory(**CONT)
     st = await make_subtask(env)
     it = await intention_of(env, "subtask", st.id)
-    await set_intention(env, it.id, state="closed", close_reason="resolved", root_cancelled_at=datetime.now(UTC))
+    await set_intention(env, it.id, state="closed", close_reason="resolved", root_expired_at=datetime.now(UTC))
     finished = datetime.now(UTC) - timedelta(hours=100)
     before = datetime.now(UTC)
     assert (await _record(env, st, created_at=finished)).reported is True
@@ -305,20 +306,20 @@ async def test_a_report_of_an_old_result_gets_a_fresh_claim_window_and_its_twin_
     assert [r.id for r in rows] == [report.id]
 
 
-async def test_a_cancelled_intention_reports_instead_of_waking(env_factory):  # noqa: F811
+async def test_an_expired_intention_reports_instead_of_waking(env_factory):  # noqa: F811
     env = await env_factory(**CONT)
     st = await make_subtask(env)
     it = await intention_of(env, "subtask", st.id)
-    await set_intention(env, it.id, state="cancelled")
+    await set_intention(env, it.id, state="expired")
     recorded = await _record(env, st)
-    assert (recorded.reported, recorded.state_after) == (True, "cancelled")
+    assert (recorded.reported, recorded.state_after) == (True, "expired")
 
 
 async def test_a_result_with_no_owner_channel_writes_only_the_settled_work_row(env_factory, caplog):  # noqa: F811
     env = await env_factory(**CONT)  # no default chat configured
     st = await make_subtask(env, routed=False)  # and no origin channel
     it = await intention_of(env, "subtask", st.id)
-    await set_intention(env, it.id, root_cancelled_at=datetime.now(UTC))
+    await set_intention(env, it.id, root_expired_at=datetime.now(UTC))
     recorded = await _record(env, st)
     assert (recorded.inserted, recorded.reported) == (False, False)
     stamped, reports = _split(await inbox_rows(env))
```

**Apply to `tests/test_f099_phase2b_routing.py`:**

```diff
diff --git a/tests/test_f099_phase2b_routing.py b/tests/test_f099_phase2b_routing.py
index d029a6f2..e7ea7726 100644
--- a/tests/test_f099_phase2b_routing.py
+++ b/tests/test_f099_phase2b_routing.py
@@ -292,7 +292,7 @@ async def test_with_continuation_off_the_subtask_pass_skips_an_unrouted_continue
 
 @pytest.mark.postgres_only  # CAST(text AS uuid) in the pass filter
 async def test_a_reported_continue_result_is_not_reselected_by_the_subtask_pass(env_factory):  # noqa: F811  # PIN
-    """MF-1. A continue result that became a report (its root was cancelled after the work finished) must
+    """MF-1. A continue result that became a report (its root expired after the work finished) must
     leave a source-keyed row behind. Otherwise the pass re-selects it on every tick and, with a small
     batch, starves the source behind it."""
     env = await env_factory(**CONT)
@@ -301,7 +301,7 @@ async def test_a_reported_continue_result_is_not_reselected_by_the_subtask_pass(
     stuck = await make_subtask(env)
     await finish(env, stuck)
     it = await intention_of(env, "subtask", stuck.id)
-    await set_intention(env, it.id, state="closed", close_reason="resolved", root_cancelled_at=datetime.now(UTC))
+    await set_intention(env, it.id, state="closed", close_reason="resolved", root_expired_at=datetime.now(UTC))
     healthy = await make_subtask(env)
     await finish(env, healthy)  # its hook's write was lost
     pass_ = InboxSubtaskPass(env.db, store, env.settings)
```

**Apply to `tests/test_f099_phase2b_writers.py`:**

```diff
diff --git a/tests/test_f099_phase2b_writers.py b/tests/test_f099_phase2b_writers.py
index 8f9d6ef4..d6c12794 100644
--- a/tests/test_f099_phase2b_writers.py
+++ b/tests/test_f099_phase2b_writers.py
@@ -243,13 +243,13 @@ async def test_a_retried_dag_reopens_its_closed_continue_intention(env_factory):
     assert [r.source_generation for r in await inbox_rows(env, dag.id)] == [0, 1]
 
 
-async def test_a_retried_dag_on_a_cancelled_root_reports_the_raw_result(env_factory):  # noqa: F811
+async def test_a_retried_dag_on_an_expired_root_reports_the_raw_result(env_factory):  # noqa: F811
     env = await env_factory(**CONT)
     dag, _ = await make_dag(env, origin_channel=CHAN)
     store = env.heart.result_inbox
     await record_dag_result(store, env.settings, **dag_kwargs(dag, origin_channel=CHAN))
     it = await intention_of(env, "dag", dag.id)
-    await set_intention(env, it.id, state="closed", close_reason="resolved", root_cancelled_at=datetime.now(UTC))
+    await set_intention(env, it.id, state="closed", close_reason="resolved", root_expired_at=datetime.now(UTC))
     await record_dag_result(store, env.settings, **{**dag_kwargs(dag, origin_channel=CHAN), "generation": 1})
     rows = await inbox_rows(env)
     reports = [r for r in rows if r.source_kind == "intention_report"]
```

**Apply to `tests/test_f099_phase2c_publisher.py`:**

```diff
diff --git a/tests/test_f099_phase2c_publisher.py b/tests/test_f099_phase2c_publisher.py
index 9219f5c7..7a7306e4 100644
--- a/tests/test_f099_phase2c_publisher.py
+++ b/tests/test_f099_phase2c_publisher.py
@@ -211,7 +211,7 @@ async def test_a_reported_late_result_reaches_telegram_too(env_factory):  # noqa
     no push time, so it never left chat."""
     env = await _env(env_factory)
     root = await make_root(env)
-    await set_intention(env, root.id, root_cancelled_at=datetime.now(UTC))  # the root is closed
+    await set_intention(env, root.id, root_expired_at=datetime.now(UTC))  # the root is closed
     recorded = await record(env, root, body="it finished after the cancel")
     assert recorded.reported is True
     (report,) = [r for r in await _owner_rows(env)]
```

**Apply to `tests/test_f099_phase2c_gate.py`:**

```diff
diff --git a/tests/test_f099_phase2c_gate.py b/tests/test_f099_phase2c_gate.py
index c2e71655..51f7b314 100644
--- a/tests/test_f099_phase2c_gate.py
+++ b/tests/test_f099_phase2c_gate.py
@@ -381,11 +381,20 @@ def test_the_plan_question_is_a_required_argument_of_the_gate():
 def test_a_gate_drop_commits_a_drop_and_a_gate_escalation_a_report():
     rows = (SimpleNamespace(title="Snow", body="40 cm"), SimpleNamespace(title="Wind", body="gusty"))
     got = SimpleNamespace(inbox_rows=rows)
-    for reason in ("cancelled", "expired", "plan_resolved"):
+    # 2e, the unified late-result rule: a cancelled root says nothing, an expired one reports what came back.
+    for reason in ("cancelled", "plan_resolved"):
         resolution, report = continuation.gate_inputs(reason, got)
         assert (resolution.decision, report) == ("drop", None)
         assert resolution.note == continuation.GATE_TEXT[reason] and resolution.progress_claimed is False
-    for reason in ("past_deadline", "budget_turns", "budget_tokens", "budget_stall", "limit_depth", "limit_spawns"):
+    for reason in (
+        "expired",
+        "past_deadline",
+        "budget_turns",
+        "budget_tokens",
+        "budget_stall",
+        "limit_depth",
+        "limit_spawns",
+    ):
         resolution, report = continuation.gate_inputs(reason, got)
         assert resolution.decision == "report" and resolution.note == continuation.GATE_TEXT[reason]
         assert continuation.GATE_TEXT[reason] in report and "40 cm" in report and "gusty" in report
```

**Apply to `tests/test_f099_phase2c_expiry_wake.py`:**

```diff
diff --git a/tests/test_f099_phase2c_expiry_wake.py b/tests/test_f099_phase2c_expiry_wake.py
index ad95cd57..1471d469 100644
--- a/tests/test_f099_phase2c_expiry_wake.py
+++ b/tests/test_f099_phase2c_expiry_wake.py
@@ -372,9 +372,10 @@ async def test_an_expiry_leaves_a_row_chat_will_deliver_alone(env_factory):  # n
 @pytest.mark.parametrize("reason", ["cancelled", "expired"])
 async def test_a_row_held_on_an_intention_a_gate_arrival_closed_is_reported_by_the_sweep(env_factory, reason, caplog):  # noqa: F811
     """Lead note (2c1-4). A row that lands after the claim read its rows, and before the root's marker, is held.
-    The gate arrival then closes its intention, so nothing can claim the row any more. The sweep reports it
-    raw, as record_result reports a result that lands after the close, and stamps it, once, with a WARNING
-    (review Minor 2: the settle is a backstop, so its firing is worth seeing in the log)."""
+    The gate arrival then closes its intention, so nothing can claim the row any more. The sweep stamps it, once,
+    with a WARNING (review Minor 2: the settle is a backstop, so its firing is worth seeing in the log), and, by
+    the unified late-result rule (2e), reports it raw when the root EXPIRED and says nothing when it was
+    CANCELLED. An expired root's gate arrival escalates too, so its own rows are reported as well."""
     env = await env_factory(**CONT)
     root = await make_root(env)
     await record(env, root)
@@ -415,10 +416,14 @@ async def test_a_row_held_on_an_intention_a_gate_arrival_closed_is_reported_by_t
         assert "settled 1 result(s)" in warning.getMessage()
         (row,) = await held()
         assert row.delivered_at is not None and row.delivered_session_id == f"intent-{root.id}"
-        (report,) = await _owner_rows(env)
-        assert (report.msg_type, report.channel, report.intention_id) == ("REPORT", CHAN, root.id)
-        assert "landed while the gate ran" in report.body
-        assert await _expire(env) == [] and len(await _owner_rows(env)) == 1  # settled once
+        reported = [r for r in await _owner_rows(env) if "landed while the gate ran" in r.body]
+        if reason == "cancelled":
+            assert reported == [] and await _owner_rows(env) == []
+        else:
+            (report,) = reported
+            assert (report.msg_type, report.channel, report.intention_id) == ("REPORT", CHAN, root.id)
+        owner_rows = len(await _owner_rows(env))
+        assert await _expire(env) == [] and len(await _owner_rows(env)) == owner_rows  # settled once
         assert len(settle_warnings()) == 1  # and a sweep that settles nothing says nothing
 
 
```

**Apply to `tests/test_f099_phase2c_repair.py`:**

```diff
diff --git a/tests/test_f099_phase2c_repair.py b/tests/test_f099_phase2c_repair.py
index ecdf3d11..0be9f87e 100644
--- a/tests/test_f099_phase2c_repair.py
+++ b/tests/test_f099_phase2c_repair.py
@@ -2,8 +2,8 @@
 
 from __future__ import annotations
 
+import asyncio
 import uuid
-from contextlib import asynccontextmanager
 from datetime import UTC, datetime, timedelta
 from types import SimpleNamespace
 
@@ -22,8 +22,9 @@ from f099_support import (
     make_subtask,
     record,
     set_intention,
+    until_a_backend_waits_on_a_lock,
 )
-from sqlalchemy import Update, select, update
+from sqlalchemy import select, update
 
 from nous.brain import continuation
 from nous.brain.intentions import IntentionSpec
@@ -234,47 +235,28 @@ async def test_a_cancelled_subtask_of_a_cancelled_root_closes_cancelled(env_fact
     assert (fresh.state, fresh.close_reason) == ("cancelled", "cancelled")
 
 
-class _Spy:
-    """A session that runs ``before_update`` just before each UPDATE it is asked to execute."""
-
-    def __init__(self, real, before_update) -> None:
-        self._real, self._before_update = real, before_update
-
-    def __getattr__(self, name):
-        return getattr(self._real, name)
-
-    async def execute(self, statement, *args, **kwargs):
-        if isinstance(statement, Update):
-            await self._before_update()
-        return await self._real.execute(statement, *args, **kwargs)
-
-
-async def test_a_root_cancel_that_commits_just_before_the_close_is_honoured(env_factory):  # noqa: F811
-    """2c1-7 review, minor 4: the root's marker is decided inside the close's UPDATE. A cancel that commits
-    after a separate read of the marker, and before the UPDATE, must not close the child `legacy`."""
+async def test_a_root_cancel_in_flight_is_honoured_by_the_close(env_factory):  # noqa: F811
+    """2c1-7 review, minor 4, as 2e keeps it: the close takes the root lock first (the one lock order), so a cancel
+    that holds the root makes it wait, and the close then decides the marker inside its UPDATE: the child is
+    `cancelled`, never `legacy`. (2c let a cancel commit between the close's read and its UPDATE; the lock closes
+    that window instead of tolerating it.)"""
     env = await env_factory(**CONT)
     root = await make_root(env, policy="remember")
     st = await make_subtask(env, policy="continue")
     child = await intention_of(env, "subtask", st.id)
     await set_intention(env, child.id, root_id=root.id, parent_id=root.id, depth=1)
     await env.heart.subtasks.cancel(st.id)
-    fired = []
-
-    async def cancel_the_root_once():
-        if not fired:
-            fired.append(True)
-            await set_intention(env, root.id, root_cancelled_at=datetime.now(UTC))  # its own committed transaction
-
-    @asynccontextmanager
-    async def session():
-        async with env.db.session() as real:
-            yield _Spy(real, cancel_the_root_once)
-
-    assert (
-        await repair_missing_results(SimpleNamespace(session=session), env.heart.result_inbox, env.settings, limit=50)
-        == 1
-    )
-    assert fired == [True]
+    async with env.db.session() as canceller:
+        await canceller.execute(select(Intention.id).where(Intention.id == root.id).with_for_update(key_share=True))
+        await canceller.execute(
+            update(Intention).where(Intention.id == root.id).values(root_cancelled_at=datetime.now(UTC))
+        )
+        repair = asyncio.create_task(_repair(env))
+        try:
+            await asyncio.wait_for(until_a_backend_waits_on_a_lock(env), timeout=10)
+        finally:
+            await canceller.commit()
+    assert await asyncio.wait_for(repair, timeout=30) == 1
     fresh = await intention_of(env, "subtask", st.id)
     assert (fresh.state, fresh.close_reason) == ("cancelled", "cancelled")
 
@@ -550,6 +532,9 @@ async def test_the_repair_leaves_a_row_held_on_a_gate_closed_intention_to_the_sw
     await env.heart.subtasks.complete(uuid.UUID(root.source_id), "done", final_outcome="completed")
     assert await _repair(env) == 0
     assert await _expire(env) == []
-    assert len(await _owner_rows(env)) == 1  # the sweep's report of the held row
+    # The unified late-result rule (2e): a cancelled root says nothing (the held row is stamped), an expired one
+    # reports twice, once for the arrival's own rows (the gate escalates) and once for the row the sweep settles.
+    reports = 0 if reason == "cancelled" else 2
+    assert len(await _owner_rows(env)) == reports
     assert await _repair(env) == 0
-    assert len(await _owner_rows(env)) == 1
+    assert len(await _owner_rows(env)) == reports
```

- [ ] **Step 2: Run and watch them fail**

`tests/test_f099_phase2e_late_results.py` and the edited files fail on the base (the rule does not exist yet).

- [ ] **Step 3: Implement**

**Apply to `nous/brain/continuation.py`:**

```diff
diff --git a/nous/brain/continuation.py b/nous/brain/continuation.py
index 27fb6ba5..f72b8b69 100644
--- a/nous/brain/continuation.py
+++ b/nous/brain/continuation.py
@@ -21,7 +21,7 @@ from datetime import UTC, datetime, timedelta
 from typing import Any
 from uuid import UUID
 
-from sqlalchemy import ColumnElement, Text, and_, any_, cast, exists, func, literal, or_, select, text, update
+from sqlalchemy import ColumnElement, Text, and_, any_, case, cast, exists, func, literal, or_, select, text, update
 from sqlalchemy.dialects.postgresql import JSONB
 from sqlalchemy.dialects.postgresql import insert as pg_insert
 from sqlalchemy.ext.asyncio import AsyncSession
@@ -110,13 +110,15 @@ GATE_REASONS = (
     "limit_spawns",
     "plan_resolved",
 )
-# The gate reasons whose arrival drops the work (the others escalate: a report). Spec 4.5.3.
-GATE_DROP_REASONS = ("cancelled", "expired", "plan_resolved")
+# The gate reasons whose arrival drops the work silently (the others escalate: a report). Spec 4.5.3, and the
+# unified late-result rule (2e): a CANCELLED root says nothing it has not said, an EXPIRED one reports the late
+# result raw, here and in record_result and in the stranded-row settle.
+GATE_DROP_REASONS = ("cancelled", "plan_resolved")
 PLAN_DROP_OUTCOMES = ("superseded", "noise")
 # The owner-facing sentence for each gate reason (the arrival's note, and the head of a report).
 GATE_TEXT = {
     "cancelled": "The owner cancelled this work.",
-    "expired": "This work expired before its result could be acted on.",
+    "expired": "This work expired before its result could be acted on, so I did not act on it.",
     "past_deadline": "This result arrived after its deadline, so I did not act on it.",
     "budget_turns": "The follow-up budget for this work is used up, so I stopped here.",
     "budget_tokens": "The token budget for this work is used up, so I stopped here.",
@@ -637,9 +639,10 @@ def raw_results_text(rows: Any) -> str:
 def gate_inputs(reason: str, claim: Claim) -> tuple[Resolution, str | None]:
     """What a gate arrival commits: a synthetic decision and the text of its report.
 
-    A drop (cancelled, expired, plan resolved) writes no report. An escalation (past deadline, a
+    A drop (cancelled, plan resolved) writes no report. An escalation (expired, past deadline, a
     budget, a limit) reports the claimed results without acting on them: the reason, then the raw
-    results (spec 4.5.3).
+    results (spec 4.5.3; an expired root reports what came back, a cancelled one is silent: the unified
+    late-result rule of 2e).
     """
     explanation = GATE_TEXT[reason]
     if reason in GATE_DROP_REASONS:
@@ -1431,13 +1434,17 @@ async def expire_roots(
 async def _settle_stranded_rows(session: AsyncSession, agent_id: str, *, settings: Any, now: datetime) -> int:
     """Rows held on an intention a gate arrival closed (``cancelled`` or ``expired``): they landed after the
     claim read its rows and before the root's marker, so the arrival did not consume them, and nothing claims
-    a closed intention. Each root's are stamped delivered and reported raw in one REPORT, as ``record_result``
-    reports a result that lands after the close. The expiry never strands one (it reads after it closes).
+    a closed intention. The unified late-result rule: the rows of an ``expired`` intention are stamped delivered and
+    reported raw in one REPORT per root, as ``record_result`` reports a result that lands after the close; the rows
+    of a ``cancelled`` one are stamped and nothing is said (the owner cancelled that work). The expiry never strands
+    one (it reads after it closes), and ``cancel_root`` stamps its own.
     Writes no intention row, so it takes no intention lock. Returns the number of rows settled."""
+    root = aliased(Intention)
     stranded = (
         await session.execute(
-            select(ResultInbox.id, Intention.root_id)
+            select(ResultInbox.id, Intention.root_id, Intention.state, root.root_cancelled_at)
             .join(Intention, and_(Intention.agent_id == agent_id, Intention.id == ResultInbox.intention_id))
+            .join(root, and_(root.agent_id == agent_id, root.id == Intention.root_id))
             .where(
                 ResultInbox.agent_id == agent_id,
                 ResultInbox.channel.is_(None),
@@ -1449,11 +1456,14 @@ async def _settle_stranded_rows(session: AsyncSession, agent_id: str, *, setting
             .limit(STRANDED_BATCH)
         )
     ).all()
-    by_root: dict[UUID, list[UUID]] = {}
-    for row_id, root_id in stranded:
-        by_root.setdefault(root_id, []).append(row_id)
+    # Keyed by (root, silent): a cancelled intention's rows are stamped and never reported (the unified late-result
+    # rule), an expired one's are reported raw.
+    by_root: dict[tuple[UUID, bool], list[UUID]] = {}
+    for row_id, root_id, state, cancelled_at in stranded:
+        # By marker, as record_result judges (N3 of the plan review): the intention's state or the root's marker.
+        by_root.setdefault((root_id, state == STATE_CANCELLED or cancelled_at is not None), []).append(row_id)
     settled = 0
-    for root_id, row_ids in by_root.items():
+    for (root_id, silent), row_ids in by_root.items():
         # Stamp and read in one statement: only the rows this call moved are reported, so a row is reported once.
         rows = sorted(
             await session.execute(
@@ -1467,6 +1477,9 @@ async def _settle_stranded_rows(session: AsyncSession, agent_id: str, *, setting
         )
         if not rows:
             continue
+        if silent:
+            settled += len(rows)
+            continue
         root = (
             await session.execute(
                 select(Intention.intent, Intention.origin_channel).where(
@@ -1499,9 +1512,7 @@ async def _settle_stranded_rows(session: AsyncSession, agent_id: str, *, setting
     if settled:
         # A backstop: today only a gate arrival's late rows land here, so a count is how a path that writes a root
         # marker without closing its lineage (2e's cancel_root, say) shows up in the log.
-        logger.warning(
-            "F099: settled %d result(s) held on closed intentions (left by a gate arrival or a cancel)", settled
-        )
+        logger.warning("F099: settled %d result(s) held on closed intentions (left by a gate arrival)", settled)
     return settled
 
 
@@ -1913,15 +1924,20 @@ async def record_result(
     state, policy, root_id, origin_channel = row.state, row.wake_policy, row.root_id, row.origin_channel
     now = datetime.now(UTC)
 
+    ended = await _root_end_state(session, agent_id, root_id)
     if (
         policy != intentions.WAKE_CONTINUE
         or state in (STATE_CANCELLED, STATE_EXPIRED)
         # A legacy close is never reopened (a DAG's retry_node re-arrival included): it reports, and its
         # settled twin keeps InboxDagPass from re-selecting the work row (MF-1).
         or (state == STATE_CLOSED and row.close_reason == intentions.CLOSE_LEGACY)
-        or not await _root_is_open(session, agent_id, root_id)
+        or ended is not None
     ):
         report_id = arrival_report_id(source_kind, source_id, source_generation)
+        # The unified late-result rule (2e, by marker): a CANCELLED root reports nothing, and an EXPIRED one
+        # reports the late result raw. The twin below is written either way, so the reconciler passes see the
+        # source as written.
+        silent = state == STATE_CANCELLED or ended == STATE_CANCELLED
         # MF-1: the work row's own inbox row, NULL-keyed and already delivered. The F098 reconciler passes
         # decide "needs repair" by this row (has_row): without it they would re-select the work row on
         # every tick for good. NULL-keyed and delivered, no chat turn can claim it, so it keeps the work's
@@ -1942,12 +1958,20 @@ async def record_result(
             intention_id=intention_id,
             arrival_id=arrival_id,
             delivered_at=now,
-            delivered_session_id=f"report:{report_id.hex[:8]}",
+            delivered_session_id=SILENT_SESSION_ID if silent else f"report:{report_id.hex[:8]}",
         )
         if twin is None:
             # This generation was already written (on this path, or on the continue path before the
             # root closed): the first delivery decided its fate, so a duplicate writes no REPORT.
             return ResultRecorded(None, False, state, False, False, intention_id, root_id)
+        if silent:
+            logger.info(
+                "F099: a late result of %s %s on cancelled root %s was dropped without a report",
+                source_kind,
+                str(source_id)[:8],
+                root_id,
+            )
+            return ResultRecorded(None, False, state, False, False, intention_id, root_id)
         channel = owner_channel(settings, origin_channel)
         if channel is None:
             logger.warning(
@@ -3613,3 +3637,122 @@ async def find_root_id(session: AsyncSession, agent_id: str, prefix: str) -> UUI
         .all()
     )
     return _unique(list(ids), prefix)
+
+
+# ---------------------------------------------------------------------------
+# F099 Phase 2e: a lineage whose last open work was cancelled (carry-over 8)
+# ---------------------------------------------------------------------------
+
+# The source_id of the REPORT that ends a hanging root: one per root, so a retried close writes it once.
+_HANGING_NAMESPACE = uuid.UUID("c2f1a8d4-6b7e-4c39-8a15-9d0e3f4b5a67")
+
+
+async def close_cancelled_source(
+    session: AsyncSession, agent_id: str, intention: Intention, *, settings: Any, now: datetime | None = None
+) -> int:
+    """(d) of ``repair_missing_results``: a cancelled subtask produced no result, so its pending intention closes
+    with no row: ``cancelled`` when its root is cancelled, ``legacy`` otherwise. The root is locked FIRST (the one
+    lock order), and the root marker is read inside the UPDATE, so no cancel can commit between a read of it and the
+    close. Returns the number of intentions closed (0 or 1). Then ``end_hanging_root``. Does not commit."""
+    now = now or datetime.now(UTC)
+    await _lock_root(session, agent_id, intention.root_id)
+    root = aliased(Intention)
+    root_cancelled = exists().where(
+        root.agent_id == agent_id, root.id == intention.root_id, root.root_cancelled_at.is_not(None)
+    )
+    moved = await session.execute(
+        update(Intention)
+        .where(Intention.agent_id == agent_id, Intention.id == intention.id, Intention.state == STATE_PENDING)
+        .values(
+            state=case((root_cancelled, STATE_CANCELLED), else_=STATE_CLOSED),
+            close_reason=case((root_cancelled, CLOSE_CANCELLED), else_=intentions.CLOSE_LEGACY),
+            closed_at=now,
+            updated_at=now,
+        )
+        .execution_options(synchronize_session=False)
+    )
+    closed = moved.rowcount or 0
+    if closed:
+        await end_hanging_root(session, agent_id, intention.root_id, settings=settings, now=now)
+    return closed
+
+
+async def end_hanging_root(
+    session: AsyncSession, agent_id: str, root_id: UUID, *, settings: Any, now: datetime | None = None
+) -> bool:
+    """Carry-over 8: close a root that nothing will ever wake or expire, and tell the owner. The caller holds the
+    root (``FOR NO KEY UPDATE``).
+
+    A continuation that ended ``continue`` or ``revise`` is waiting for the work it spawned. If that work is
+    cancelled (``cancel_task`` by the owner or the model, a DAG node, a worker) and closes as ``legacy`` with no
+    result, the lineage has no open row left: ``_ttl_applies`` is false, so the TTL sweep never reaches it, no
+    result will come, and the goal would end without a word. This writes ``root_expired_at`` (a later result is then
+    reported raw, never silently reopened: the consequence of closing it) and one REPORT to the owner channel.
+    Nothing happens while any intention of the lineage is open, for a root that carries a marker, for a container,
+    or when the newest arrival did not leave the lineage waiting (a ``drop``, ``report`` or ``ask``, or no arrival
+    at all: a Phase 1 root has nothing to report). Returns whether the root was closed."""
+    now = now or datetime.now(UTC)
+    root = (
+        await session.execute(
+            select(Intention)
+            .where(Intention.agent_id == agent_id, Intention.id == root_id)
+            .with_for_update(key_share=True)
+            .execution_options(populate_existing=True)
+        )
+    ).scalar_one_or_none()
+    if (
+        root is None
+        or root.root_cancelled_at is not None
+        or root.root_expired_at is not None
+        or root.wake_policy == intentions.WAKE_CONTAINER
+    ):
+        return False
+    open_left = (
+        await session.execute(
+            select(
+                exists().where(
+                    Intention.agent_id == agent_id, Intention.root_id == root_id, Intention.state.in_(OPEN_STATES)
+                )
+            )
+        )
+    ).scalar_one()
+    if open_left:
+        return False
+    newest = (
+        await session.execute(
+            select(IntentionArrival.decision)
+            .where(IntentionArrival.agent_id == agent_id, IntentionArrival.root_id == root_id)
+            .order_by(IntentionArrival.n.desc())
+            .limit(1)
+        )
+    ).scalar_one_or_none()
+    if newest not in ("continue", "revise"):
+        return False
+    await session.execute(
+        update(Intention)
+        .where(Intention.agent_id == agent_id, Intention.id == root_id)
+        .values(root_expired_at=now, updated_at=now)
+        .execution_options(synchronize_session=False)
+    )
+    channel = owner_channel(settings, root.origin_channel)
+    if channel is None:
+        logger.warning(
+            "F099: root %s was left with nothing running and has no owner channel; nothing was reported", root_id
+        )
+        return True
+    await insert_report(
+        session,
+        agent_id,
+        kind=MSG_REPORT,
+        title=f"Stopped: {root.intent}",
+        body=clip_body(
+            f"The work I was waiting on was cancelled, so nothing is left running for this: {root.intent}", settings
+        ),
+        channel=channel,
+        intention_id=root_id,
+        root_id=root_id,
+        push_after=push_after_for(settings, now),
+        report_id=uuid.uuid5(_HANGING_NAMESPACE, str(root_id)),
+    )
+    logger.info("F099: root %s was left with nothing running; it was closed and reported", root_id)
+    return True
```

**Apply to `nous/heart/result_reconciler.py`:**

```diff
diff --git a/nous/heart/result_reconciler.py b/nous/heart/result_reconciler.py
index 7a24b74e..efd3c8c6 100644
--- a/nous/heart/result_reconciler.py
+++ b/nous/heart/result_reconciler.py
@@ -24,9 +24,9 @@ from datetime import UTC, datetime, timedelta
 from typing import TYPE_CHECKING, Any, Protocol
 from uuid import UUID
 
-from sqlalchemy import Text, and_, case, cast, exists, func, or_, select, update
+from sqlalchemy import Text, and_, cast, exists, func, or_, select, update
 from sqlalchemy.dialects.postgresql import UUID as PG_UUID
-from sqlalchemy.orm import aliased, selectinload
+from sqlalchemy.orm import selectinload
 
 from nous.brain import continuation, intentions
 from nous.heart.result_inbox import (
@@ -351,32 +351,14 @@ async def repair_missing_results(database: Database, store: ResultInboxStore, se
     return fixed
 
 
-async def _close_cancelled(database: Database, agent_id: str, intention: Intention) -> int:
+async def _close_cancelled(database: Database, settings: Settings, intention: Intention) -> int:
     """(d): a cancelled source produced no result: close its intention with no report (``cancelled``
-    when its root is cancelled, ``legacy`` otherwise). The root's marker is read inside the UPDATE, so no
-    cancel can commit between a read of it and the close."""
-    root = aliased(Intention)
-    root_cancelled = exists().where(
-        root.agent_id == agent_id, root.id == intention.root_id, root.root_cancelled_at.is_not(None)
-    )
-    now = datetime.now(UTC)
+    when its root is cancelled, ``legacy`` otherwise), and, when that leaves a lineage that was waiting on it with
+    nothing running, close the root and tell the owner (``continuation.end_hanging_root``, carry-over 8)."""
     async with database.session() as session:
-        moved = await session.execute(
-            update(Intention)
-            .where(
-                Intention.agent_id == agent_id,
-                Intention.id == intention.id,
-                Intention.state == continuation.STATE_PENDING,
-            )
-            .values(
-                state=case((root_cancelled, continuation.STATE_CANCELLED), else_=continuation.STATE_CLOSED),
-                close_reason=case((root_cancelled, continuation.CLOSE_CANCELLED), else_=intentions.CLOSE_LEGACY),
-                closed_at=now,
-                updated_at=now,
-            )
-        )
+        closed = await continuation.close_cancelled_source(session, settings.agent_id, intention, settings=settings)
         await session.commit()
-    return moved.rowcount or 0
+    return closed
 
 
 async def _close_settled(database: Database, agent_id: str, source_kind: str, source_id: Any) -> int:
@@ -485,7 +467,7 @@ async def _repair_subtask_results(
     for intention, st in pairs:
         try:
             if st.status == "cancelled":
-                fixed += await _close_cancelled(database, agent_id, intention)  # (d)
+                fixed += await _close_cancelled(database, settings, intention)  # (d)
             elif st.id in kinds:
                 if kinds[st.id] == "keyed":
                     fixed += await _close_settled(database, agent_id, SOURCE_SUBTASK, st.id)  # (c)
```

- [ ] **Step 4: Run** `tests/test_f099_phase2e_late_results.py tests/test_f099_phase2b_record_result.py tests/test_f099_phase2c_gate.py tests/test_f099_phase2c_repair.py tests/test_f099_phase2c_expiry_wake.py tests/test_f099_phase2c_commit.py tests/test_f099_phase2c_arrival.py tests/test_f099_phase2b_routing.py tests/test_f099_phase2b_writers.py tests/test_f099_phase2c_publisher.py -q`: all pass. **Do not** run the 2c repair file against a database where another run holds the root lock: the lock-ordered test waits on purpose.

- [ ] **Step 5: Mutation checks**
  1. `silent = state == STATE_CANCELLED or ended == STATE_CANCELLED` to `silent = False`: four 2e tests fail (a cancelled root reports).
  2. `GATE_DROP_REASONS` back to `("cancelled", "expired", "plan_resolved")`: `test_an_expired_roots_arrival_reports_what_came_back_and_a_cancelled_ones_does_not` fails.
  3. Make the stranded settle ignore `silent`: `test_the_sweep_stamps_the_rows_stranded_on_a_cancelled_intention_without_a_report` fails.
  4. Skip `end_hanging_root` in `close_cancelled_source`: the hanging-root test fails.

- [ ] **Step 6: Lint and commit** (`feat(F099): 2e-2 the unified late-result rule, hanging roots, the repair's cancel cases`). Add the new file and the edited files by name.

---

## Task 2e-3: the owner's cancel reaches every tool call, and a continuation turn starts with an empty thread

**Prod runs:** `_authorize_tool_call` runs on every tool call of every turn in prod. The change is one `ctx.root_intention_id is not None` test, and, for the contexts that name a root (the subtasks of an intention-recording spawn, in prod), one call of `_no_cancelled_roots`, which returns False. No row is read, nothing is logged, no allocation. `_restore_conversation` gains one `startswith("intent-")` (no prod session id has that prefix; a continuation thread is never restored). `REFUSAL_CODES` gains a name. `discard_conversation` and the call in `_turn` are reached only by the runner. Pinned: `test_a_context_that_names_no_root_never_asks_the_view`, `test_with_no_view_installed_nothing_is_cancelled_and_nothing_is_read`.

**Files:**
- Modify: `nous/api/runner.py`, `nous/cognitive/ledger_store.py`, `nous/handlers/continuation_runner.py`
- Create: `tests/test_f099_phase2e_authorize.py`

**Interfaces:**
- Produces: `AgentRunner.set_cancelled_roots(view: Callable[[UUID], bool]) -> None` (the view is `ContinuationRunner.root_is_cancelled`); `AgentRunner.discard_conversation(session_id: str) -> None` (no DB, no await); the module function `_no_cancelled_roots(_root_id) -> bool`; `"root_cancelled"` in `ledger_store.REFUSAL_CODES`. `_authorize_tool_call` returns `Refusal("Tool error: this work was cancelled by the owner; stop.", "root_cancelled")` first, for any context whose `root_intention_id` the view calls cancelled.
- A residual to state, not fix: `heart.conversation_state` rows are written only after a compaction; a no-DB discard leaves one, and the restore guard makes sure it is never read for an `intent-` thread. The guard is what makes the surviving row harmless, so a later reader must not "fix" the discard by adding a database write to the top of every turn (the docstring says so).

- [ ] **Step 1: Write the tests**

**Create `tests/test_f099_phase2e_authorize.py`:**

```python
"""F099 Phase 2e-3: the owner's cancel reaches every tool call (the view of cancelled roots in
``_authorize_tool_call``), and a continuation turn starts with no leftover session (carry-over 3)."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

import pytest
from f099_support import (
    CONT,
    ScriptedModel,
    build_runner,
    env_factory,  # noqa: F401
    inbox_rows,
    make_root,
    record,
    runner_env,  # noqa: F401
    say,
    use,
)
from sqlalchemy import select

from nous.api import runner as runner_module
from nous.api.execution_context import ExecutionContext
from nous.api.models import Conversation, Message
from nous.cognitive.ledger_store import REFUSAL_CODES
from nous.handlers.continuation_runner import ContinuationRunner
from nous.storage.models import IntentionArrival

ROOT = uuid.uuid4()
MODES_OFF = {"tool_offered_set_enforcement_mode": "off", "tool_context_policy_mode": "off"}


def _ctx(kind: str = "subtask", *, authority: str = "owner", root=ROOT, **extra) -> ExecutionContext:
    if kind == "continuation":
        extra = {"intention_id": ROOT, **extra}
    if kind == "approved_action":
        extra = {"proposal_id": uuid.uuid4(), "declared_tools": ("web_search",), **extra}
    return ExecutionContext(kind=kind, session_id="s", authority=authority, root_intention_id=root, **extra)


class CountingView:
    """A view of cancelled roots that counts its questions."""

    def __init__(self, cancelled=()) -> None:
        self.cancelled, self.asked = set(cancelled), []

    def __call__(self, root_id) -> bool:
        self.asked.append(root_id)
        return root_id in self.cancelled


async def _runner(env_factory, **settings):  # noqa: F811
    env = await env_factory(**CONT, ANTHROPIC_API_KEY="test-key", **settings)
    runner, _cognitive, _dispatcher = build_runner(env, ScriptedModel())
    return env, runner


@pytest.mark.parametrize(
    ("kind", "authority"),
    [
        ("subtask", "owner"),  # a subtask of an owner root that was already running when the owner cancelled
        ("dag_node", "owner"),
        ("heartbeat_check", "owner"),
        ("scheduled", "owner"),
        ("subtask", "internal_only"),
        ("continuation", "internal_only"),
        ("approved_action", "owner"),
    ],
)
async def test_a_call_on_behalf_of_a_cancelled_root_is_refused_whatever_the_modes_say(env_factory, kind, authority):  # noqa: F811
    """Security: the view is checked at dispatch, for every kind and authority, with both enforcement modes off."""
    env, runner = await _runner(env_factory, **MODES_OFF)
    runner.set_cancelled_roots(CountingView({ROOT}))
    refusal = runner._authorize_tool_call(
        _ctx(kind, authority=authority), "web_search", frozenset({"web_search"}), "s", {"query": "snow"}
    )
    assert refusal is not None and refusal.code == "root_cancelled"
    assert "cancelled by the owner" in refusal.text


async def test_the_refusal_is_checked_before_the_strict_rule_and_a_cancelled_root_is_not_offered_anything(env_factory):  # noqa: F811
    env, runner = await _runner(env_factory)
    runner.set_cancelled_roots(CountingView({ROOT}))
    # An internal_only call that the strict rule would refuse as not offered is refused as cancelled: the owner's
    # stop is the answer the model reads.
    refusal = runner._authorize_tool_call(
        _ctx("continuation", authority="internal_only"), "send_email", frozenset(), "s", {}
    )
    assert refusal.code == "root_cancelled"


async def test_a_root_that_is_not_cancelled_is_untouched_by_the_view(env_factory):  # noqa: F811
    env, runner = await _runner(env_factory, **MODES_OFF)
    view = CountingView({uuid.uuid4()})
    runner.set_cancelled_roots(view)
    assert runner._authorize_tool_call(_ctx(), "web_search", frozenset({"web_search"}), "s", {}) is None
    assert view.asked == [ROOT]


async def test_a_context_that_names_no_root_never_asks_the_view(env_factory):  # noqa: F811  # PIN
    """Prod parity: a chat turn, a heartbeat triage and every pre-F099 background context name no root, so for them
    the check is one `is not None` and the view is never called."""
    env, runner = await _runner(env_factory)
    view = CountingView({ROOT})
    runner.set_cancelled_roots(view)
    for kind in ("interactive", "mcp", "heartbeat_triage", "background", "subtask"):
        assert (
            runner._authorize_tool_call(_ctx(kind, root=None), "web_search", frozenset({"web_search"}), "s", {}) is None
        )
    assert view.asked == []


async def test_with_no_view_installed_nothing_is_cancelled_and_nothing_is_read(env_factory):  # noqa: F811  # PIN
    """Prod parity (continuation off): no runner installs a view, so the default answers False without a lookup, for
    every context, and the ordinary rules decide as they did before 2e."""
    env, runner = await _runner(env_factory, tool_offered_set_enforcement_mode="enforce")
    assert runner._root_cancelled is runner_module._no_cancelled_roots
    assert runner._root_cancelled(ROOT) is False
    assert runner._authorize_tool_call(_ctx(), "web_search", frozenset({"web_search"}), "s", {}) is None
    unoffered = runner._authorize_tool_call(_ctx(), "bash", frozenset({"web_search"}), "s", {})
    assert unoffered is not None and unoffered.code == "offered_set"  # the existing rule, unchanged


def test_the_refusal_code_is_a_ledger_code():
    """`_ledger_blocked` writes the code into the durable row, and the ledger rejects a code it does not know."""
    assert "root_cancelled" in REFUSAL_CODES


async def test_a_cancelled_roots_tool_call_does_not_run_through_a_real_turn(runner_env):  # noqa: F811
    """End to end through the tool loop: the handler is never called, the model reads the refusal, and the blocked
    call is written to the ledger with the new code (a ledger that rejected the code would fail the turn)."""
    env = await runner_env(
        [use("web_search", query="snow")],
        [say("I stop.")],
        ANTHROPIC_API_KEY="test-key",
    )
    calls = []

    async def web_search(**kwargs):
        calls.append(kwargs)
        return {"content": [{"type": "text", "text": "40 cm"}]}

    env.dispatcher.register(
        "web_search", web_search, {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}
    )
    env.runner.set_cancelled_roots(CountingView({ROOT}))
    blocked = []

    class Ledger:
        async def record_blocked(self, **kwargs):
            blocked.append(kwargs["refused_by"])

    env.runner.set_ledger_store(Ledger())
    ctx = ExecutionContext(kind="subtask", session_id="sub-1", root_intention_id=ROOT, intention_id=ROOT)
    await env.runner.run_turn("sub-1", "look", is_background=True, is_subtask=True, context=ctx)
    assert calls == [] and blocked == ["root_cancelled"]
    result_messages = [m for m in env.model.calls[-1]["messages"] if "cancelled by the owner" in str(m)]
    assert result_messages


# ---- carry-over 3: a turn starts with no leftover session ------------------------------------------------------


async def test_discard_conversation_forgets_the_whole_in_memory_session(env_factory):  # noqa: F811
    env, runner = await _runner(env_factory)
    sid = "intent-x"
    runner._conversations[sid] = Conversation(session_id=sid, messages=[Message(role="user", content="old")])
    runner._get_or_create_ledger(sid).record("learn_fact", {}, "ok", "success")
    runner._pending_corrections[sid] = ["nudge"]
    runner._compaction_locks[sid] = object()
    runner.discard_conversation(sid)
    assert (sid in runner._conversations, sid in runner._ledgers) == (False, False)
    assert (sid in runner._pending_corrections, sid in runner._compaction_locks) == (False, False)
    assert runner.executed_tools(sid) == []
    runner.discard_conversation(sid)  # nothing to forget is not an error


async def test_a_continuation_thread_is_never_restored_from_the_database(env_factory):  # noqa: F811
    env, runner = await _runner(env_factory)
    env.heart.load_conversation_state = AsyncMock(
        return_value={"messages": [{"role": "user", "content": "from a crashed arrival"}], "summary": None}
    )
    assert await runner._restore_conversation("intent-" + str(ROOT)) is None
    env.heart.load_conversation_state.assert_not_awaited()
    ordinary = await runner._restore_conversation("S1")  # every other session restores as before
    assert ordinary is not None and ordinary.messages[0].content == "from a crashed arrival"


async def test_a_leftover_session_does_not_reach_the_next_arrival(runner_env):  # noqa: F811
    """The previous arrival's end_conversation failed: its messages and its `learn_fact` stayed in memory. The next
    arrival must not see the messages, and must not verify a false `progress` with the leftover ledger."""
    env = await runner_env([use("resolve_intention", decision="report", note="Done.", progress=True, confidence=0.6)])
    cont = ContinuationRunner(
        database=env.db, settings=env.settings, runner=env.runner, heart=env.heart, brain=env.brain, bus=env.bus
    )
    root = await make_root(env)
    await record(env, root)
    sid = f"intent-{root.id}"
    env.runner._conversations[sid] = Conversation(
        session_id=sid,
        messages=[Message(role="user", content="LEFTOVER ask"), Message(role="assistant", content="LEFTOVER reply")],
    )
    env.runner._get_or_create_ledger(sid).record("learn_fact", {"fact": "x"}, "stored", "success")

    done = await cont.run_arrival(root.id)

    assert done is not None
    (call,) = env.model.calls
    assert "LEFTOVER" not in str(call["messages"])
    async with env.db.session() as s:
        (arrival,) = (await s.execute(select(IntentionArrival).where(IntentionArrival.root_id == root.id))).scalars()
    # Claimed true, and nothing this arrival did backs it (no spawn, no revise, no memory write): stored false.
    assert (arrival.progress_claimed, arrival.progress) == (True, False)
    assert [r for r in await inbox_rows(env) if r.source_kind == "intention_report"]  # the report was written
```

- [ ] **Step 2: Run and watch them fail** (`set_cancelled_roots` does not exist).

- [ ] **Step 3: Implement**

**Apply to `nous/api/runner.py`:**

```diff
diff --git a/nous/api/runner.py b/nous/api/runner.py
index c2aa6b98..97ea394f 100644
--- a/nous/api/runner.py
+++ b/nous/api/runner.py
@@ -80,6 +80,12 @@ class Refusal:
     code: str
 
 
+def _no_cancelled_roots(_root_id: UUID) -> bool:
+    """The default view of cancelled roots: none. Installed until ``set_cancelled_roots`` (F099 2e) replaces it,
+    so a deployment with continuation off pays one attribute read and one call per lineage tool call."""
+    return False
+
+
 # 012.2: a subtask may not delegate (no-nesting rule). F062: spawn_sync has identical
 # inline-blocking semantics to spawn_task(await_result=True) and competes for the same
 # worker pool; without exclusion a hardened subtask could call it recursively and
@@ -396,6 +402,9 @@ class AgentRunner:
         # to tasks - the F091 _pending_tasks lesson).
         self._ledger_store: LedgerStore | None = None
         self._ledger_pending_tasks: set[asyncio.Task] = set()
+        # F099 2e: the owner's cancel, as an in-process view (``set_cancelled_roots``); nothing is cancelled until
+        # the continuation runner installs one.
+        self._root_cancelled: Callable[[UUID], bool] = _no_cancelled_roots
         # Phase 2.8: compensation snapshot store (wired when compensation_enabled).
         self._snap_store: Any | None = None
         self._workspace_dir: str = settings.workspace_dir
@@ -492,7 +501,31 @@ class AgentRunner:
         the per-context policy (2a). The last two run for every call the strict
         rule lets through; each returns early only when IT refuses, so a
         warn-mode deviation is recorded by both.
+
+        F099 2e: before all of them, a call on behalf of a CANCELLED root is refused, whatever the authority, the
+        kind of turn and the two mode settings say: the owner stopped this work, and a subtask that was already
+        running, a DAG node or a lineage check must not do one more thing. The view is a set lookup, read only
+        when the context names a root (a chat turn names none).
         """
+        if ctx.root_intention_id is not None and self._root_cancelled(ctx.root_intention_id):
+            logger.warning(
+                "F099: refused %r in a %s turn (the root %s was cancelled, session=%s)",
+                tool_name,
+                ctx.kind,
+                ctx.root_intention_id,
+                session_id,
+            )
+            self._log_f026_decision(
+                "harness_context_policy_violation",
+                {
+                    "tool_name": tool_name,
+                    "context_kind": ctx.kind,
+                    "violation": "root_cancelled",
+                    "mode": "enforce",
+                },
+                session_id=session_id,
+            )
+            return Refusal("Tool error: this work was cancelled by the owner; stop.", "root_cancelled")
         # F099 section 4.4: an internal_only turn, and an approved_action call, are
         # enforced here FIRST, whatever the two mode settings say. The modes are for
         # tuning the ordinary rules; this is a security floor. For both, a call to a
@@ -595,6 +628,11 @@ class AgentRunner:
         """Harness Phase 1b: durable ledger for side-effecting tool calls."""
         self._ledger_store = store
 
+    def set_cancelled_roots(self, view: Callable[[UUID], bool]) -> None:
+        """F099 2e: install the in-process view of cancelled roots (``ContinuationRunner.root_is_cancelled``).
+        ``_authorize_tool_call`` refuses every call whose context names a root the view says is cancelled."""
+        self._root_cancelled = view
+
     def set_snapshot_store(self, store: Any, workspace_dir: str) -> None:
         """Phase 2.8: compensation snapshots for compensable calls in background contexts."""
         self._snap_store = store
@@ -1647,6 +1685,22 @@ class AgentRunner:
 
             return response_text, turn_context, usage
 
+    def discard_conversation(self, session_id: str) -> None:
+        """Forget everything this runner keeps in memory for ``session_id``: its messages, its execution ledger,
+        its pending corrections and its compaction lock. No database and no await (F099 2e, carry-over 3).
+
+        A continuation turn calls it first. ``end_conversation`` can time out, raise, or be cancelled from outside
+        (a cancel of the root, a stop), and then the thread of the root would carry the previous arrival's messages
+        and its ``executed_tools`` (a ``learn_fact`` of the last arrival would verify this one's ``progress``).
+
+        A ``heart.conversation_state`` row (written only after a compaction) survives this on purpose, and it is
+        harmless because ``_restore_conversation`` never reads one for an ``intent-`` thread: do not "fix" the discard
+        by adding a database write to the top of every turn."""
+        self._conversations.pop(session_id, None)
+        self._compaction_locks.pop(session_id, None)
+        self._ledgers.pop(session_id, None)
+        self._pending_corrections.pop(session_id, None)
+
     async def end_conversation(
         self,
         session_id: str,
@@ -4276,6 +4330,8 @@ Rules:
 
     async def _restore_conversation(self, session_id: str) -> Conversation | None:
         """Restore conversation from Heart persistence if available."""
+        if session_id.startswith(INTENT_SESSION_PREFIX):
+            return None  # F099 2e: a continuation thread is rebuilt from rows every arrival, never restored
         try:
             state = await self._heart.load_conversation_state(
                 agent_id=self._settings.agent_id,
```

**Apply to `nous/cognitive/ledger_store.py`:**

```diff
diff --git a/nous/cognitive/ledger_store.py b/nous/cognitive/ledger_store.py
index 1504ed3b..02bcef05 100644
--- a/nous/cognitive/ledger_store.py
+++ b/nous/cognitive/ledger_store.py
@@ -46,7 +46,9 @@ KEY_HOLDING_STATUSES = ("pending", "success", "unknown")
 # Why the harness refused a call. A code, never prose: the ActionGate model's
 # reason is written from a prompt that carries the call's arguments, so it can
 # echo a subject, a body or a bare key.
-REFUSAL_CODES = frozenset({"offered_set", "action_gate", "context_policy", "duplicate", "internal_only"})
+REFUSAL_CODES = frozenset(
+    {"offered_set", "action_gate", "context_policy", "duplicate", "internal_only", "root_cancelled"}
+)
 _TERMINAL = frozenset(s for s in LEDGER_STATUSES if s != "pending")
 
 # Per-tool durable argument policy. Pattern redaction cannot be trusted with
```

**Apply to `nous/handlers/continuation_runner.py`:**

```diff
diff --git a/nous/handlers/continuation_runner.py b/nous/handlers/continuation_runner.py
index 6f534b99..e2716feb 100644
--- a/nous/handlers/continuation_runner.py
+++ b/nous/handlers/continuation_runner.py
@@ -683,6 +683,9 @@ class ContinuationRunner:
         settings = self._settings
         session_id = f"{INTENT_SESSION_PREFIX}{claim.root_id}"
         arrival_id = uuid.uuid4()
+        # Carry-over 3: the thread of this root starts empty. A previous arrival whose end_conversation timed out,
+        # raised or was cancelled from outside left its messages and its ledger behind, and this one would run on top.
+        self._runner.discard_conversation(session_id)
         async with self._db.session() as session:
             earlier, spawned, root_intent, root_decision = await self._lineage_context(session, claim)
         prompt = build_arrival_prompt(claim, earlier, spawned, limits, settings, root_intent=root_intent)
```

- [ ] **Step 4: Run** `tests/test_f099_phase2e_authorize.py tests/test_runner_ledger.py tests/test_runner_authorization.py tests/test_f099_enforcement.py tests/test_f099_phase2c_arrival.py tests/test_f099_phase2c_loop.py -q`: all pass (16 new).

- [ ] **Step 5: Mutation checks**
  1. Make the check `if False:`: ten of the sixteen new tests fail.
  2. Delete `self._runner.discard_conversation(session_id)` from `_turn`: `test_a_leftover_session_does_not_reach_the_next_arrival` fails.

- [ ] **Step 6: Lint and commit** (`feat(F099): 2e-3 the cancelled-roots view in _authorize_tool_call, discard_conversation`).

---

## Task 2e-4: the tokens of failed attempts count against the root's budget (migration 085)

**Prod runs:** migration 085 runs at the first start of the new image, whatever the flags: `ADD COLUMN … NOT NULL DEFAULT 0` is a catalog change on PG 17 and the partial index matches no row (no cancelled root exists), so both are instant. Every `select(Intention)` now names the new column, which is safe because `create_components` runs the migrations before any query. `fail_attempt`, `root_limits` and `_fail` are reached only by the runner. Pinned: `test_a_new_intention_starts_with_no_failed_tokens` (2e-8).

**Files:**
- Create: `sql/migrations/085_intention_failed_tokens_and_cancel_index.sql`, `tests/test_f099_phase2e_failed_tokens.py`
- Modify: `nous/storage/models.py`, `nous/brain/continuation.py`, `nous/handlers/continuation_runner.py`
- Modify (changed pins): `tests/test_f099_phase2d_parity.py` (`test_2d_added_no_migration_and_no_setting` now expects 085 to be the last migration) and `tests/test_f099_intentions.py` (`INTENTION_COLUMNS` gains `failed_tokens`)

**Interfaces:**
- Produces: `Intention.failed_tokens: Mapped[int]` (NOT NULL, default 0); `fail_attempt(session, agent_id, claim, *, max_attempts, settings, brain=None, now=None, arrival_id=None, tokens: tuple[int, int] = (0, 0)) -> str`; `ContinuationRunner._fail(claim, *, tokens=(0, 0))`. `root_limits(...).tokens` now adds `Σ failed_tokens` over the lineage.
- Rules: a retried attempt charges `tokens_in + tokens_out` to the claim's **deepest** intention through `_fenced_move` (so a stale claim charges nothing); at the cap the tokens go to the `failed_report` arrival row, not to the column; `release_stale_claims` passes none.

- [ ] **Step 1: Create the migration and apply it to your database**

**Create `sql/migrations/085_intention_failed_tokens_and_cancel_index.sql`:**

```sql
-- Migration 085: failed-attempt tokens and the cancelled-roots index (F099 Phase 2e)
--
-- brain.intentions.failed_tokens: the tokens (in plus out) that the failed
-- attempts of a claim spent. A failed attempt that is retried writes no arrival
-- row, so without this column its tokens were counted nowhere, and a lineage
-- whose turns keep failing could never reach its token budget. It is written on
-- the deepest intention of the claim only, so a root's budget is a plain sum
-- over its lineage. brain.intention_arrivals needs no change: a failed attempt
-- is not a turn, and the arrival rows keep their meaning.
--
-- idx_intentions_cancelled: the in-process view of cancelled roots is loaded at
-- startup and refreshed at every sweep from the roots that carry a cancel marker.

ALTER TABLE brain.intentions
    ADD COLUMN IF NOT EXISTS failed_tokens INTEGER NOT NULL DEFAULT 0;

CREATE INDEX IF NOT EXISTS idx_intentions_cancelled
    ON brain.intentions (agent_id, root_cancelled_at)
    WHERE root_cancelled_at IS NOT NULL;
```

`docker exec -i nous-postgres psql -U nous -d "$DB" -v ON_ERROR_STOP=1 -q < sql/migrations/085_intention_failed_tokens_and_cancel_index.sql`

- [ ] **Step 2: Write the tests**

**Create `tests/test_f099_phase2e_failed_tokens.py`:**

```python
"""F099 Phase 2e-4: the tokens of failed attempts count against the root's token budget (carry-over 2), and
migration 085."""

from __future__ import annotations

from pathlib import Path

import pytest
from f099_support import (
    CONT,
    claim,
    env_factory,  # noqa: F401
    make_child,
    make_root,
    record,
    runner_env,  # noqa: F401
    say,
    set_intention,
)
from sqlalchemy import select, text

from nous.brain import continuation
from nous.handlers.continuation_runner import ContinuationRunner
from nous.storage.models import Intention, IntentionArrival

pytestmark = pytest.mark.postgres_only  # FOR NO KEY UPDATE, savepoints, = ANY(array)
MIGRATION = (
    Path(__file__).resolve().parents[1] / "sql" / "migrations" / "085_intention_failed_tokens_and_cancel_index.sql"
)


async def _fail(env, got, *, max_attempts=3, tokens=(0, 0)):
    async with env.db.session() as s:
        out = await continuation.fail_attempt(
            s, env.agent, got, max_attempts=max_attempts, settings=env.settings, tokens=tokens
        )
        await s.commit()
    return out


async def _limits(env, root_id):
    async with env.db.session() as s:
        return await continuation.root_limits(s, env.agent, root_id, settings=env.settings)


async def _failed_tokens(env, *intentions):
    async with env.db.session() as s:
        rows = await s.execute(
            select(Intention.id, Intention.failed_tokens).where(Intention.id.in_([i.id for i in intentions]))
        )
    return dict(rows.all())


async def _pair(env):
    """A root and its child, both with a result, claimed together: the child is the deepest."""
    root = await make_root(env)
    child = await make_child(env, root)
    await record(env, root)
    await record(env, child)
    got = await claim(env, root.id)
    assert got.deepest.id == child.id and len(got.intentions) == 2
    return root, child, got


# ---- migration 085 ---------------------------------------------------------------------------------------------


async def test_migration_085_adds_the_column_and_the_index(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    async with env.db.session() as s:
        column = (
            await s.execute(
                text(
                    "SELECT data_type, is_nullable, column_default FROM information_schema.columns "
                    "WHERE table_schema = 'brain' AND table_name = 'intentions' AND column_name = 'failed_tokens'"
                )
            )
        ).one()
        index = (
            await s.execute(
                text(
                    "SELECT indexdef FROM pg_indexes "
                    "WHERE schemaname = 'brain' AND indexname = 'idx_intentions_cancelled'"
                )
            )
        ).scalar_one()
    assert (column.data_type, column.is_nullable, column.column_default) == ("integer", "NO", "0")
    assert "root_cancelled_at IS NOT NULL" in index


def test_the_migration_keeps_the_runners_conventions():
    """The migrator splits on a semicolon, so no comment may hold one; and it must be idempotent."""
    lines = MIGRATION.read_text(encoding="utf-8").splitlines()
    assert all(";" not in line for line in lines if line.startswith("--"))
    body = "\n".join(line for line in lines if not line.startswith("--"))
    assert "ADD COLUMN IF NOT EXISTS" in body and "CREATE INDEX IF NOT EXISTS" in body and "DO $$" not in body


# ---- fail_attempt ----------------------------------------------------------------------------------------------


async def test_a_retried_attempt_charges_the_deepest_intention_once(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    root, child, got = await _pair(env)
    assert await _fail(env, got, tokens=(100, 10)) == "retry"
    assert await _failed_tokens(env, root, child) == {root.id: 0, child.id: 110}
    assert (await _limits(env, root.id)).tokens == 110  # the root's budget counts it once, not once per claimed row


async def test_failed_attempts_add_up(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    root, child, got = await _pair(env)
    await _fail(env, got, tokens=(100, 10))
    again = await claim(env, root.id)
    await _fail(env, again, tokens=(50, 5))
    assert (await _limits(env, root.id)).tokens == 165


async def test_a_failure_that_spent_nothing_writes_no_charge(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    root, child, got = await _pair(env)
    assert await _fail(env, got) == "retry"
    assert await _failed_tokens(env, root, child) == {root.id: 0, child.id: 0}


async def test_a_stale_claim_charges_nothing(env_factory):  # noqa: F811
    """The charge goes through the same fence as the release: a claim that was released or cancelled is not charged."""
    env = await env_factory(**CONT)
    root, child, got = await _pair(env)
    await set_intention(env, root.id, state="cancelled", claim_token=None)
    await set_intention(env, child.id, state="cancelled", claim_token=None)
    assert await _fail(env, got, tokens=(100, 10)) == continuation.FAIL_LOST
    assert await _failed_tokens(env, root, child) == {root.id: 0, child.id: 0}


async def test_the_failed_report_books_the_last_attempts_tokens_on_its_arrival_and_keeps_the_earlier_ones(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    root, child, got = await _pair(env)
    await _fail(env, got, max_attempts=2, tokens=(100, 10))  # attempt 1 of 2: retried, charged on the child
    again = await claim(env, root.id)
    assert await _fail(env, again, max_attempts=2, tokens=(70, 7)) == continuation.CLOSE_FAILED_REPORT
    async with env.db.session() as s:
        (arrival,) = (await s.execute(select(IntentionArrival).where(IntentionArrival.root_id == root.id))).scalars()
    assert (arrival.outcome, arrival.tokens_in, arrival.tokens_out) == ("failed_report", 70, 7)
    assert (await _limits(env, root.id)).tokens == 110 + 77  # each attempt once: no double count at the cap


async def test_a_lease_release_knows_no_usage_and_charges_nothing(env_factory):  # noqa: F811
    from datetime import UTC, datetime, timedelta

    env = await env_factory(**CONT)
    root, child, got = await _pair(env)
    async with env.db.session() as s:
        released = await continuation.release_stale_claims(
            s,
            env.agent,
            lease_s=900,
            max_attempts=3,
            settings=env.settings,
            now=datetime.now(UTC) + timedelta(hours=1),
        )
        await s.commit()
    assert set(released) == {root.id, child.id}
    assert (await _limits(env, root.id)).tokens == 0


# ---- the runner -------------------------------------------------------------------------------------------------


async def test_a_lineage_whose_turns_keep_failing_reaches_its_token_budget(runner_env):  # noqa: F811
    """Each attempt answers in prose (110 tokens), then the follow-up raises: a failed attempt that spent 110. Before
    2e those tokens were counted nowhere, so this lineage would fail for ever. After ten of them the budget (1000)
    is spent and the next claim escalates to the owner instead of running another turn."""
    steps = []
    for _ in range(10):
        steps += [[say("let me think")], RuntimeError("the follow-up failed")]
    env = await runner_env(*steps, continuation_max_tokens_per_root=1000, continuation_max_attempts=50)
    cont = ContinuationRunner(
        database=env.db, settings=env.settings, runner=env.runner, heart=env.heart, brain=env.brain, bus=env.bus
    )
    root = await make_root(env)
    await record(env, root)
    for _ in range(10):
        assert await cont.run_arrival(root.id) is None  # a failed attempt
    assert (await _limits(env, root.id)).tokens == 1100 and (await _limits(env, root.id)).escalate == "budget_tokens"
    assert len(env.model.calls) == 20
    done = await cont.run_arrival(root.id)  # the gate: no model call, a report with the reason
    assert done is not None and len(env.model.calls) == 20
    async with env.db.session() as s:
        (arrival,) = (await s.execute(select(IntentionArrival).where(IntentionArrival.root_id == root.id))).scalars()
    assert (arrival.gate_reason, arrival.decision) == ("budget_tokens", "report")
```

**Apply to `tests/test_f099_phase2d_parity.py`:**

```diff
diff --git a/tests/test_f099_phase2d_parity.py b/tests/test_f099_phase2d_parity.py
index b2699878..4a53d639 100644
--- a/tests/test_f099_phase2d_parity.py
+++ b/tests/test_f099_phase2d_parity.py
@@ -183,7 +183,7 @@ async def test_the_real_bot_against_the_real_routes_is_inert_under_prods_flags(e
 
 def test_2d_added_no_migration_and_no_setting():  # PIN: changes when a later PR adds one on purpose
     migrations = Path(__file__).resolve().parents[1] / "sql" / "migrations"
-    assert sorted(migrations.glob("*.sql"))[-1].name.startswith("084_")
+    assert sorted(migrations.glob("*.sql"))[-1].name.startswith("085_")  # 2e adds 085; 2d added none
     named = {name for name in Settings.model_fields if "proposal" in name or "owner_action" in name}
     assert named == {"intention_proposal_ttl_hours"}
 
```

**Apply to `tests/test_f099_intentions.py`:**

```diff
diff --git a/tests/test_f099_intentions.py b/tests/test_f099_intentions.py
index 0535c1dc..31c77fb2 100644
--- a/tests/test_f099_intentions.py
+++ b/tests/test_f099_intentions.py
@@ -15,7 +15,7 @@ INTENTION_COLUMNS = {
     "origin_kind", "origin_session_id", "origin_channel", "origin_decision_id", "wake_policy",
     "authority", "expected_result", "assumptions", "deadline", "state", "close_reason",
     "root_cancelled_at", "root_expired_at", "claimed_at", "claim_token", "attempts",
-    "created_at", "result_at", "closed_at", "updated_at",
+    "created_at", "result_at", "closed_at", "updated_at", "failed_tokens",
 }  # fmt: skip
 
 
```

- [ ] **Step 3: Run and watch them fail** (`fail_attempt` takes no `tokens`).

- [ ] **Step 4: Implement**

**Apply to `nous/storage/models.py`:**

```diff
diff --git a/nous/storage/models.py b/nous/storage/models.py
index fe0e5ee7..0c9beb29 100644
--- a/nous/storage/models.py
+++ b/nous/storage/models.py
@@ -495,6 +495,9 @@ class Intention(Base):
     claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
     claim_token: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
     attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
+    # F099 2e (migration 085): tokens (in + out) that failed attempts of a claim spent, on the claim's deepest
+    # intention. A retried attempt writes no arrival row, so this is where its cost is kept.
+    failed_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
     created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
     result_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
     closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
```

**Apply to `nous/brain/continuation.py`:**

```diff
diff --git a/nous/brain/continuation.py b/nous/brain/continuation.py
index f72b8b69..8e4c6cbb 100644
--- a/nous/brain/continuation.py
+++ b/nous/brain/continuation.py
@@ -496,13 +496,15 @@ async def root_limits(session: AsyncSession, agent_id: str, root_id: UUID, *, se
     Tokens are the lineage's subtask ``tokens_in/out`` (a DAG-node subtask has no intention of its own, and
     ``dag_node_id IS NULL`` keeps it out should one ever have one: its usage is already in its DAG's
     ``tokens_consumed``), plus its DAGs' ``tokens_consumed`` (which Task 2c1-8 feeds with check-node usage),
-    plus its arrivals' tokens.
+    plus its arrivals' tokens, plus what its failed attempts spent (``failed_tokens``, 2e: a retried attempt
+    writes no arrival row).
     """
-    depth, spawns = (
+    depth, spawns, failed_tokens = (
         await session.execute(
             select(
                 func.coalesce(func.max(Intention.depth), 0),
                 func.count(Intention.id).filter(Intention.depth > 0),
+                func.coalesce(func.sum(Intention.failed_tokens), 0),
             ).where(Intention.agent_id == agent_id, Intention.root_id == root_id)
         )
     ).one()
@@ -563,7 +565,7 @@ async def root_limits(session: AsyncSession, agent_id: str, root_id: UUID, *, se
         if verified is not False:
             break
         stalls += 1
-    tokens = int(subtask_tokens) + int(dag_tokens) + int(arrival_tokens)
+    tokens = int(subtask_tokens) + int(dag_tokens) + int(arrival_tokens) + int(failed_tokens)
     depth, spawns, turns = int(depth), int(spawns), int(turns)
     max_depth, max_spawns = settings.continuation_max_depth, settings.continuation_max_spawns_per_root
     escalate: str | None = None
@@ -1162,6 +1164,7 @@ async def fail_attempt(
     brain: Any = None,
     now: datetime | None = None,
     arrival_id: UUID | None = None,
+    tokens: tuple[int, int] = (0, 0),
 ) -> str:
     """T8 and T12: one claimed attempt failed (the turn raised or timed out, or its lease expired).
 
@@ -1178,6 +1181,11 @@ async def fail_attempt(
     cap), so the claim token in that UPDATE is the fence and nothing else stands in for it. ``arrival_id`` is the
     id of the cap's arrival row (a new one when not given), as ``commit_arrival`` takes it: the caller can name
     that row in ``intention.arrival_decided``.
+
+    ``tokens`` is ``(tokens_in, tokens_out)`` the failed attempt spent (2e, carry-over 2): a retry keeps it on the
+    claim's deepest intention (``failed_tokens``, in the same fence as the retry), the cap books it on the arrival
+    row of the ``failed_report``. Either way ``root_limits`` counts it, so a lineage whose turns keep failing reaches
+    its token budget. A lease release knows no usage and passes none.
     """
     now = now or datetime.now(UTC)
     ids = sorted(i.id for i in claim.intentions)
@@ -1196,7 +1204,20 @@ async def fail_attempt(
                 )
             ).scalars()
             worst = max(counts) + 1
+            spent = int(tokens[0]) + int(tokens[1])
             if worst < max_attempts:
+                if spent > 0:
+                    # Through the fence like every write to a claimed intention: it is checked before the release
+                    # below, so a stale claim charges nothing. One row only (the deepest), so it is summed once.
+                    charged = await _fenced_move(
+                        session,
+                        agent_id,
+                        [claim.deepest.id],
+                        claim.claim_token,
+                        {"failed_tokens": Intention.failed_tokens + spent, "updated_at": now},
+                    )
+                    if charged != {claim.deepest.id}:
+                        raise _FenceLost
                 released = await _fenced_move(
                     session,
                     agent_id,
@@ -1223,7 +1244,7 @@ async def fail_attempt(
                 resolution=Resolution("report", f"Failed after {worst} attempts.", False, 0.0),
                 outcome=OUTCOME_FAILED,
                 gate_reason=None,
-                tokens=(0, 0),
+                tokens=(int(tokens[0]), int(tokens[1])),
                 brain=brain,
                 settings=settings,
                 report_text=f"{body}\n\n{raw}" if raw else body,
```

**Apply to `nous/handlers/continuation_runner.py`:**

```diff
diff --git a/nous/handlers/continuation_runner.py b/nous/handlers/continuation_runner.py
index e2716feb..767e9e92 100644
--- a/nous/handlers/continuation_runner.py
+++ b/nous/handlers/continuation_runner.py
@@ -730,7 +730,9 @@ class ContinuationRunner:
                 logger.warning(
                     "F099: the continuation of root %s failed (%s)", claim.root_id, type(exc).__name__, exc_info=True
                 )
-                await self._fail(claim)
+                # What the calls that finished before the failure cost (2e, carry-over 2): a call that raised
+                # returned no usage, so it is the one cost this cannot see.
+                await self._fail(claim, tokens=(usage[0], usage[1]))
                 return None
             # Read BEFORE the session ends: end_conversation pops the ledger.
             wrote_memory = any(
@@ -950,11 +952,11 @@ class ContinuationRunner:
             raise
         except ValueError as exc:  # commit_arrival refused the arrival: an ask with nowhere to ask, a bad outcome
             logger.warning("F099: the commit of root %s was refused (%s)", claim.root_id, exc)
-            await self._fail(claim)
+            await self._fail(claim, tokens=tokens)
             return None
         except Exception:
             logger.warning("F099: the commit of root %s failed", claim.root_id, exc_info=True)
-            await self._fail(claim)
+            await self._fail(claim, tokens=tokens)
             return None
         await self._emit(
             "intention.arrival_decided",
@@ -979,9 +981,10 @@ class ContinuationRunner:
         self.wake()
         return done
 
-    async def _fail(self, claim: continuation.Claim) -> None:
+    async def _fail(self, claim: continuation.Claim, *, tokens: tuple[int, int] = (0, 0)) -> None:
         """A failed attempt (spec 4.5.7): one more attempt on every claimed intention; the cap closes them
-        with their raw results. If even this fails, the lease recovers the claim."""
+        with their raw results. ``tokens`` is what the attempt spent, which counts against the root's budget (2e).
+        If even this fails, the lease recovers the claim."""
         arrival_id = uuid.uuid4()  # the cap's arrival row, named in arrival_decided (contract 4.13)
         try:
             async with self._db.session() as session:
@@ -993,6 +996,7 @@ class ContinuationRunner:
                     settings=self._settings,
                     brain=None,  # R5: a failed report is not a model decision
                     arrival_id=arrival_id,
+                    tokens=tokens,
                 )
                 await session.commit()
         except asyncio.CancelledError:
```

- [ ] **Step 5: Run** `tests/test_f099_phase2e_failed_tokens.py tests/test_f099_phase2d_parity.py tests/test_f099_intentions.py tests/test_f099_phase2c_failure.py tests/test_f099_phase2c_bounds.py tests/test_database.py -q`: all pass.

- [ ] **Step 6: Mutation checks**
  1. `if spent > 0:` to `if False:` in `fail_attempt`: four tests fail, `test_a_lineage_whose_turns_keep_failing_reaches_its_token_budget` among them.
  2. Drop `+ int(failed_tokens)` from `root_limits`: the same four fail.
  3. In `_turn`, call `await self._fail(claim)` without the tokens: only the loop test fails (the store tests pass the tokens themselves).

- [ ] **Step 7: Lint and commit** (`feat(F099): 2e-4 failed attempts' tokens count against the root budget (migration 085)`).

---

## Task 2e-5: the runner's cancel: the view, the DAGs, the running turn, the sweep, and the wiring

**Prod runs:** nothing new. `main.py` builds no runner (`CONTINUATION_RUNNER_READY` is False), so `_build_continuation_runner` returns before the new lines, and the new `if continuation_runner is not None:` in the DAG block is false. Pinned: `test_prods_flags_install_no_view_and_build_no_runner` (with the constant flipped as well), `test_main_binds_the_orchestrators_cancel_where_the_orchestrator_exists`, `test_an_inert_runner_runs_none_of_the_2e_sweep_on_prods_flags` (2e-8).

**Files:**
- Modify: `nous/brain/continuation.py` (`stray_dag_ids`), `nous/handlers/continuation_runner.py`, `nous/main.py`
- Create: `tests/test_f099_phase2e_runner_cancel.py`

**Interfaces:**
- Produces:
```python
class ContinuationRunner:
    def __init__(self, *, database, settings, runner, heart, brain, bus=None, dispatcher=None, publisher=None,
                 cancel_dag: Callable[[UUID, str], Awaitable[None]] | None = None) -> None
    def set_cancel_dag(self, cancel_dag) -> None
    def root_is_cancelled(self, root_id: UUID) -> bool            # a set lookup: no await, no row
    async def load_cancelled_roots(self) -> int                   # every cancelled root into the view
    async def cancel_root(self, root_id: UUID, *, reason: str, actor: str) -> continuation.CancelOutcome
        # raises continuation.RootNotFound, continuation.CancelRefused (nothing written)
async def stray_dag_ids(session, agent_id, *, limit: int = 10) -> list[UUID]   # DAGs still running under a cancelled root
CANCEL_WAIT_SECONDS = 5.0; CANCEL_VIEW_MARGIN_SECONDS = 120; STRAY_DAG_BATCH = 10
```
- `cancel_root` order: the store's transaction commits; the view takes `outcome.root_ids`; `cancel_dag(dag_id, "cancelled by the owner")` per DAG (one that raises is logged and left to the sweep); each running turn task of those roots is cancelled and awaited (bounded); `intention.root_cancelled` per root and `intention.proposal_decided` (`cancelled`, actor `system`) per shown proposal; `wake()`.
- The running turn: `task.cancel()` reaches `run_arrival`'s `cancel_requested()` branch, which releases the claim (fenced: the cancel already moved the rows, so it finds none), and the arrival task's done callback frees the slot. A turn that is past its model call loses its fence in `commit_arrival` and returns None.
- **S3, the bus:** `_expire` passes `proposals_out` and emits `intention.proposal_decided` (`expired`, actor `system`) for each proposal an expiry ended: the proposals sweep no longer finds them. **N1:** `_stop_turns` logs a WARNING with the count of cancelled turns still unwinding after `CANCEL_WAIT_SECONDS` (the route still says `turn_stopped`: the turn was told to). **N2:** `_build_continuation_runner` wraps `load_cancelled_roots()` in a try/except WARNING (`start()` loads again): a transient error there must not fail `create_components`.
- `run_once` gains a first step, `"cancel sweep"`: refresh the view since the last sweep (less 120 s), then cancel the DAGs still running under a cancelled root. `start()` loads the view before the loop. `SweepReport` is unchanged.
- `main.py`: `_build_continuation_runner` calls `runner.set_cancelled_roots(continuation_runner.root_is_cancelled)` and `await continuation_runner.load_cancelled_roots()` before returning (so the view is loaded before any loop or worker runs); the DAG block calls `continuation_runner.set_cancel_dag(dag_orchestrator.cancel_dag)` after the orchestrator is built.

- [ ] **Step 1: Write the tests**

**Create `tests/test_f099_phase2e_runner_cancel.py`:**

```python
"""F099 Phase 2e-5: the runner's cancel: the view, the DAGs, the running turn, the sweep (spec 4.6)."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from f099_support import (
    ask_with_proposals,
    env_factory,  # noqa: F401
    inbox_rows,
    intention_of,
    make_dag,
    make_root,
    record,
    runner_env,  # noqa: F401
    set_intention,
    until_a_backend_waits_on_a_lock,
    use,
)
from sqlalchemy import select

import nous.handlers.continuation_runner as runner_module
from nous.api.execution_context import ExecutionContext
from nous.brain import continuation
from nous.handlers.continuation_runner import ContinuationRunner
from nous.storage.models import Intention, IntentionArrival

pytestmark = pytest.mark.postgres_only


def _cont(env, **kw) -> ContinuationRunner:
    env.cont = ContinuationRunner(
        database=env.db, settings=env.settings, runner=env.runner, heart=env.heart, brain=env.brain, bus=env.bus, **kw
    )
    env.runner.set_cancelled_roots(env.cont.root_is_cancelled)  # what main.py does
    return env.cont


def resolve(decision="report", note="Done.", progress=False, confidence=0.7):
    return use("resolve_intention", decision=decision, note=note, progress=progress, confidence=confidence)


async def _arrivals(env, root_id):
    async with env.db.session() as s:
        rows = await s.execute(select(IntentionArrival).where(IntentionArrival.root_id == root_id))
        return list(rows.scalars().all())


async def _ready_root(env):
    root = await make_root(env)
    await record(env, root)
    return root


async def _owner_rows(env):
    return [r for r in await inbox_rows(env) if r.source_kind == "intention_report"]


class DagRecorder:
    """A stand-in for ``DAGOrchestrator.cancel_dag``."""

    def __init__(self, fail_for=()) -> None:
        self.calls, self.fail_for = [], set(fail_for)

    async def __call__(self, dag_id, reason="cancelled"):
        self.calls.append((dag_id, reason))
        if dag_id in self.fail_for:
            raise RuntimeError("the orchestrator is down")


# ---- cancel_root ---------------------------------------------------------------------------------------------


async def test_cancel_root_cancels_the_lineage_the_dags_and_tells_the_bus(runner_env):  # noqa: F811
    env = await runner_env()
    asked = await ask_with_proposals(env)
    (pid,) = asked.ids
    running, _ = await make_dag(env, status="running", parent=asked.root)
    dags = DagRecorder()
    cont = _cont(env, cancel_dag=dags)

    out = await cont.cancel_root(asked.root.id, reason="no longer wanted", actor="owner-test")

    assert (out.already_cancelled, out.cancelled_dags, out.cancelled_proposals, out.turn_stopped) == (
        False,
        1,
        1,
        False,
    )
    assert dags.calls == [(running.id, "cancelled by the owner")]
    assert cont.root_is_cancelled(asked.root.id) and not cont.root_is_cancelled(uuid.uuid4())
    events = {e.type: e.data for e in env.bus.events}
    assert events["intention.root_cancelled"] == {
        "root_id": str(asked.root.id),
        "reason": "no longer wanted",
        "actor": "owner-test",
    }
    assert events["intention.proposal_decided"] == {"proposal_id": str(pid), "state": "cancelled", "actor": "system"}


async def test_a_dag_the_orchestrator_cannot_cancel_is_left_to_the_sweep(runner_env, caplog):  # noqa: F811
    env = await runner_env()
    root = await make_root(env)
    running, _ = await make_dag(env, status="running", parent=root)
    dags = DagRecorder(fail_for={running.id})
    cont = _cont(env, cancel_dag=dags)
    out = await cont.cancel_root(root.id, reason="t", actor="t")
    assert out.cancelled_dags == 0 and "could not cancel DAG" in caplog.text
    assert cont.root_is_cancelled(root.id)  # the cancel itself stands: the lineage can do nothing
    dags.fail_for.clear()
    await cont.run_once()  # the sweep finds the DAG still running under a cancelled root
    assert [call[0] for call in dags.calls] == [running.id, running.id]


async def test_with_no_orchestrator_bound_the_dags_are_reported_and_the_cancel_stands(runner_env, caplog):  # noqa: F811
    env = await runner_env()
    root = await make_root(env)
    await make_dag(env, status="running", parent=root)
    cont = _cont(env)
    out = await cont.cancel_root(root.id, reason="t", actor="t")
    assert out.cancelled_dags == 0 and len(out.dag_ids) == 1 and "no orchestrator is bound" in caplog.text


async def test_a_refused_or_unknown_cancel_raises_and_changes_nothing(runner_env):  # noqa: F811
    env = await runner_env()
    cont = _cont(env)
    with pytest.raises(continuation.RootNotFound):
        await cont.cancel_root(uuid.uuid4(), reason="t", actor="t")
    root = await make_root(env)
    await env.heart.subtasks.cancel(uuid.UUID(root.source_id))
    await set_intention(env, root.id, state="closed", close_reason="legacy")
    with pytest.raises(continuation.CancelRefused):
        await cont.cancel_root(root.id, reason="t", actor="t")
    assert not cont.root_is_cancelled(root.id) and env.bus.events == []


# ---- the running turn (carry-over 6) ---------------------------------------------------------------------------


async def test_a_cancel_stops_the_running_turn_releases_its_slot_and_commits_nothing(runner_env):  # noqa: F811
    started, never = asyncio.Event(), asyncio.Event()

    async def blocked(_kwargs):
        started.set()
        await never.wait()  # the model call hangs until the turn is cancelled

    env = await runner_env(blocked)
    root = await _ready_root(env)
    cont = _cont(env)
    assert (await cont.run_once()).launched == (root.id,)
    await asyncio.wait_for(started.wait(), timeout=30)
    assert cont.running_roots == frozenset({root.id})

    out = await cont.cancel_root(root.id, reason="stop", actor="owner-test")

    assert out.turn_stopped is True
    assert cont.running_roots == frozenset() and cont._slots._value == env.settings.continuation_max_concurrent
    assert (await intention_of(env, "subtask", root.source_id)).state == "cancelled"
    assert await _arrivals(env, root.id) == [] and await _owner_rows(env) == []
    assert env.cognitive.end_sessions == [f"intent-{root.id}"]  # the session was ended on the way out
    report = await cont.run_once()  # nothing is claimable and nothing is launched
    assert report.launched == ()


async def test_a_turn_that_finished_but_has_not_committed_loses_its_fence_to_the_cancel(runner_env):  # noqa: F811
    """The turn's last model call returns its decision, and the owner's cancel commits just before the commit."""
    holder = {}

    async def decide_then_get_cancelled(_kwargs):
        async with holder["env"].db.session() as s:  # the cancel, in its own committed transaction
            await continuation.cancel_root(s, holder["env"].agent, holder["root"].id, reason="t", actor="t")
            await s.commit()
        return [resolve("report", "A report nobody should read.", progress=True)]

    env = await runner_env(decide_then_get_cancelled)
    root = await _ready_root(env)
    holder.update(env=env, root=root)
    cont = _cont(env)
    done = await cont.run_arrival(root.id)
    assert done is None  # the commit's fence was gone
    assert await _arrivals(env, root.id) == [] and await _owner_rows(env) == []
    fresh = await intention_of(env, "subtask", root.source_id)
    assert (fresh.state, fresh.attempts) == ("cancelled", 0)  # not charged as a failed attempt


async def test_a_cancel_that_queues_behind_a_claim_stops_the_turn_that_claim_started(runner_env, monkeypatch):  # noqa: F811
    """The claim and the cancel serialise on the root row, and Postgres grants a row lock in the order it was asked
    for: the claim is first in the queue, so it wins (the spy sees a claim), moves the root to `deciding` and
    commits; the cancel then takes the lock, moves that `deciding` row and cancels the arrival's task. Deterministic:
    the lock is held while both queue."""
    env = await runner_env()
    root = await _ready_root(env)
    cont = _cont(env)
    claims = []
    real = continuation.claim_root

    async def spy(*args, **kwargs):
        got = await real(*args, **kwargs)
        claims.append(got is not None)
        return got

    monkeypatch.setattr(continuation, "claim_root", spy)
    async with env.db.session() as holder:
        await holder.execute(select(Intention.id).where(Intention.id == root.id).with_for_update(key_share=True))
        assert (await cont.run_once()).launched == (root.id,)  # the arrival task is made; its claim queues on the root
        try:
            await asyncio.wait_for(until_a_backend_waits_on_a_lock(env, at_least=1), timeout=10)
            cancel = asyncio.create_task(cont.cancel_root(root.id, reason="t", actor="t"))
            await asyncio.wait_for(until_a_backend_waits_on_a_lock(env, at_least=2), timeout=10)
        finally:
            await holder.commit()
    out = await asyncio.wait_for(cancel, timeout=30)
    assert claims == [True]  # the claim was first in the queue and won
    assert out.turn_stopped is True and out.cancelled_intentions == 1  # the cancel found its `deciding` row
    assert cont.running_roots == frozenset() and cont._slots._value == env.settings.continuation_max_concurrent
    assert await _arrivals(env, root.id) == [] and await _owner_rows(env) == []
    assert (await intention_of(env, "subtask", root.source_id)).state == "cancelled"


async def test_a_turn_that_is_slow_to_unwind_is_told_so_in_the_log(runner_env, monkeypatch, caplog):  # noqa: F811
    """N1 of the plan review: the route still says the turn was stopped (it was told to), and the log says it is not
    gone yet."""
    monkeypatch.setattr(runner_module, "CANCEL_WAIT_SECONDS", 0.1)
    env = await runner_env()
    cont = _cont(env)
    root_id = uuid.uuid4()

    async def stubborn():
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            await asyncio.sleep(0.5)  # a blocking call that has to finish first

    task = asyncio.create_task(stubborn())
    cont._running[root_id] = task
    await asyncio.sleep(0)
    assert await cont._stop_turns([root_id]) is True
    assert "still unwinding" in caplog.text
    await asyncio.wait_for(task, timeout=10)


async def test_the_proposals_an_expiry_ended_are_announced_on_the_bus(runner_env):  # noqa: F811
    """S3 of the plan review: the proposals sweep no longer finds the proposals of an expired root, so the expiry's
    own sweep step tells the bus, and the rows say who ended them."""
    env = await runner_env()
    asked = await ask_with_proposals(env)
    (pid,) = asked.ids
    await set_intention(env, asked.root.id, created_at=datetime.now(UTC) - timedelta(hours=100))
    cont = _cont(env)
    await cont.run_once()
    events = [e.data for e in env.bus.events if e.type == "intention.proposal_decided"]
    assert events == [{"proposal_id": str(pid), "state": "expired", "actor": "system"}]
    assert [e.data["root_id"] for e in env.bus.events if e.type == "intention.root_expired"] == [str(asked.root.id)]


# ---- the view: security, restart, refresh ---------------------------------------------------------------------------


async def test_after_a_cancel_the_lineage_can_dispatch_nothing(runner_env):  # noqa: F811
    """The security pin: the view is the one `_authorize_tool_call` reads, and a cancel puts the root in it."""
    env = await runner_env()
    root = await make_root(env, routed=False)
    cont = _cont(env)
    contexts = [
        ExecutionContext(kind="subtask", session_id="s", root_intention_id=root.id, intention_id=root.id),
        ExecutionContext(
            kind="continuation",
            session_id="s",
            authority="internal_only",
            root_intention_id=root.id,
            intention_id=root.id,
        ),
    ]
    before = [env.runner._authorize_tool_call(c, "web_search", frozenset({"web_search"}), "s", {}) for c in contexts]
    assert before == [None, None]
    await cont.cancel_root(root.id, reason="t", actor="t")
    after = [env.runner._authorize_tool_call(c, "web_search", frozenset({"web_search"}), "s", {}) for c in contexts]
    assert [r.code for r in after] == ["root_cancelled", "root_cancelled"]


async def test_a_restart_remembers_the_cancel_and_a_sweep_takes_in_one_made_elsewhere(runner_env):  # noqa: F811
    env = await runner_env()
    first, second = await make_root(env), await make_root(env)
    cont = _cont(env)
    await cont.cancel_root(first.id, reason="t", actor="t")
    restarted = _cont(env)  # a new process: an empty view
    assert not restarted.root_is_cancelled(first.id)
    assert await restarted.load_cancelled_roots() == 1 and restarted.root_is_cancelled(first.id)
    async with env.db.session() as s:  # another surface cancels the second root, in the store
        await continuation.cancel_root(s, env.agent, second.id, reason="t", actor="t")
        await s.commit()
    assert not restarted.root_is_cancelled(second.id)
    await restarted.run_once()
    assert restarted.root_is_cancelled(second.id)


async def test_start_loads_the_view_before_the_loop_runs(runner_env):  # noqa: F811
    env = await runner_env()
    root = await make_root(env)
    await _cont(env).cancel_root(root.id, reason="t", actor="t")
    fresh = _cont(env)
    await fresh.start()
    try:
        assert fresh.root_is_cancelled(root.id)
    finally:
        await fresh.stop()


async def test_a_cancelled_root_is_never_launched_again(runner_env):  # noqa: F811
    env = await runner_env()
    root = await _ready_root(env)
    cont = _cont(env)
    await cont.cancel_root(root.id, reason="t", actor="t")
    await record(env, root, generation=1)  # a late result lands on the cancelled root
    assert (await cont.run_once()).launched == ()
    assert env.model.calls == []
```

- [ ] **Step 2: Run and watch them fail** (`ContinuationRunner` has no `cancel_root`).

- [ ] **Step 3: Implement**

**Apply to `nous/brain/continuation.py`:**

```diff
diff --git a/nous/brain/continuation.py b/nous/brain/continuation.py
index 8e4c6cbb..7fb4244a 100644
--- a/nous/brain/continuation.py
+++ b/nous/brain/continuation.py
@@ -3637,6 +3637,34 @@ async def cancelled_root_ids(
     return list(rows.scalars().all())
 
 
+async def stray_dag_ids(session: AsyncSession, agent_id: str, *, limit: int = 10) -> list[UUID]:
+    """The DAGs that are still running under a cancelled root, oldest first. ``cancel_root`` reports its lineage's
+    DAGs and the runner cancels them after the commit, so a process that stopped in between, or an orchestrator that
+    raised, leaves one running: the runner's sweep cancels it from here. A stray DAG cannot act (the root is
+    cancelled, so every tool call of its nodes is refused), it can only keep its nodes busy."""
+    root = aliased(Intention)
+    rows = await session.execute(
+        select(ExecutionDAG.id)
+        .join(
+            Intention,
+            and_(
+                Intention.agent_id == agent_id,
+                Intention.source_kind == intentions.SOURCE_DAG,
+                Intention.source_id == cast(ExecutionDAG.id, Text),
+            ),
+        )
+        .join(root, and_(root.agent_id == agent_id, root.id == Intention.root_id))
+        .where(
+            ExecutionDAG.agent_id == agent_id,
+            ExecutionDAG.status.notin_(intentions.TERMINAL_DAG_STATUSES),
+            root.root_cancelled_at.is_not(None),
+        )
+        .order_by(ExecutionDAG.created_at, ExecutionDAG.id)
+        .limit(limit)
+    )
+    return list(rows.scalars().all())
+
+
 async def find_root_id(session: AsyncSession, agent_id: str, prefix: str) -> UUID | None:
     """The one ROOT intention whose id starts with ``prefix`` (a child is never matched: the owner cancels roots)."""
     cleaned = normalize_id(prefix)
```

**Apply to `nous/handlers/continuation_runner.py`:**

```diff
diff --git a/nous/handlers/continuation_runner.py b/nous/handlers/continuation_runner.py
index 767e9e92..d4e403a3 100644
--- a/nous/handlers/continuation_runner.py
+++ b/nous/handlers/continuation_runner.py
@@ -328,6 +328,11 @@ NO_ROWS_NOTE = "A result was ready but no result row came with it; nothing to de
 # An approved call is bounded by the tool timeout plus this. The runner's wait_for is the only bound: the dispatch
 # path (_dispatch_with_ledger) applies no tool timeout of its own. stop() also waits this long for a call in flight.
 EXECUTION_GRACE_SECONDS = 5.0
+# cancel_root waits this long for the turns it cancelled to unwind (a turn in a blocking call finishes it, then ends).
+CANCEL_WAIT_SECONDS = 5.0
+# A sweep re-reads the roots cancelled since the last one, less this margin (a second process would lag by a sweep).
+CANCEL_VIEW_MARGIN_SECONDS = 120
+STRAY_DAG_BATCH = 10  # the DAGs under cancelled roots one sweep cancels
 # What the model and the owner are told of a call whose outcome is not known. Never the exception's message: it
 # can echo the call's arguments.
 TIMEOUT_TEXT = (
@@ -357,6 +362,7 @@ class ContinuationRunner:
         bus: Any = None,
         dispatcher: Any = None,
         publisher: Any = None,
+        cancel_dag: Callable[[UUID, str], Awaitable[None]] | None = None,
     ) -> None:
         self._db = database
         self._settings = settings
@@ -375,11 +381,26 @@ class ContinuationRunner:
         self._executing: set[asyncio.Task[Any]] = set()
         self._cooldown: dict[UUID, datetime] = {}
         self._task: asyncio.Task[None] | None = None
+        # 2e: the owner's cancel. ``_cancelled`` is the in-process view AgentRunner._authorize_tool_call asks (loaded at
+        # startup, grown by cancel_root, refreshed by every sweep); ``_cancel_dag`` is the orchestrator's cancel, bound
+        # after the DAG block of main.py exists (the runner is built before it).
+        self._cancelled: set[UUID] = set()
+        self._cancel_dag = cancel_dag
+        self._view_checked_at: datetime | None = None
 
     @property
     def running_roots(self) -> frozenset[UUID]:
         return frozenset(self._running)
 
+    def set_cancel_dag(self, cancel_dag: Callable[[UUID, str], Awaitable[None]] | None) -> None:
+        """Bind ``DAGOrchestrator.cancel_dag`` (main.py, once the orchestrator exists)."""
+        self._cancel_dag = cancel_dag
+
+    def root_is_cancelled(self, root_id: UUID) -> bool:
+        """The in-process view of cancelled roots, for ``AgentRunner.set_cancelled_roots``. A set lookup: no
+        database, no await. A root the owner cancelled is in it from the moment ``cancel_root`` committed."""
+        return root_id in self._cancelled
+
     def wake(self) -> None:
         """Tell the loop to look again: a result is ready (the bus hint, the reconciler pass), or an
         arrival ended and left work behind."""
@@ -394,6 +415,7 @@ class ContinuationRunner:
         if not continuation.enabled(self._settings) or self._task is not None:
             return
         await self._step("startup lease release", self._release_stale)
+        await self._step("cancelled roots load", self.load_cancelled_roots)
         self._task = asyncio.create_task(self._loop(), name="continuation-runner")
 
     async def stop(self) -> None:
@@ -429,6 +451,7 @@ class ContinuationRunner:
         while a slot is free. Every step is isolated; with continuation off it does nothing."""
         if not continuation.enabled(self._settings):
             return continuation.SweepReport(0, 0, 0, 0, (), None)
+        await self._step("cancel sweep", self._cancel_sweep)
         released = await self._step("lease release", self._release_stale, [])
         expired = await self._step("TTL sweep", self._expire, [])
         # The proposal expiry runs in its own session and AFTER the lease release, and the order matters: its
@@ -451,6 +474,52 @@ class ContinuationRunner:
             logger.warning("F099: the continuation %s failed; the next sweep tries again", name, exc_info=True)
             return default
 
+    async def load_cancelled_roots(self) -> int:
+        """Load every cancelled root into the view (2e). main.py calls it when it builds the runner, before any loop
+        or worker runs, and ``start`` calls it again: a restart must not forget a cancel. Returns how many roots
+        the view holds."""
+        await self._refresh_cancelled(since=None)
+        return len(self._cancelled)
+
+    async def _refresh_cancelled(self, *, since: datetime | None) -> None:
+        checked = datetime.now(UTC)
+        async with self._db.session() as session:
+            ids = await continuation.cancelled_root_ids(session, self._agent_id, since=since)
+        self._cancelled.update(ids)
+        self._view_checked_at = checked
+
+    async def _cancel_sweep(self) -> int:
+        """Every sweep: take in the roots cancelled since the last one (a cancel through another surface, or by a
+        second process), then cancel the DAGs that are still running under a cancelled root (a cancel whose
+        orchestrator call failed, or whose process stopped after the commit). Returns the DAGs it cancelled."""
+        since = (
+            self._view_checked_at - timedelta(seconds=CANCEL_VIEW_MARGIN_SECONDS)
+            if self._view_checked_at is not None
+            else None
+        )
+        await self._refresh_cancelled(since=since)
+        if self._cancel_dag is None:
+            return 0
+        async with self._db.session() as session:
+            strays = await continuation.stray_dag_ids(session, self._agent_id, limit=STRAY_DAG_BATCH)
+        return await self._cancel_dags(strays)
+
+    async def _cancel_dags(self, dag_ids: Sequence[UUID]) -> int:
+        """The orchestrator's cancel for each DAG; one that raises is logged and left for the sweep."""
+        done = 0
+        for dag_id in dag_ids:
+            if self._cancel_dag is None:
+                logger.warning("F099: DAG %s is running under a cancelled root and no orchestrator is bound", dag_id)
+                continue
+            try:
+                await self._cancel_dag(dag_id, "cancelled by the owner")
+                done += 1
+            except asyncio.CancelledError:
+                raise
+            except Exception:
+                logger.warning("F099: could not cancel DAG %s; the sweep tries again", dag_id, exc_info=True)
+        return done
+
     async def _release_stale(self) -> list[UUID]:
         settings = self._settings
         async with self._db.session() as session:
@@ -469,13 +538,25 @@ class ContinuationRunner:
 
     async def _expire(self) -> list[UUID]:
         settings = self._settings
+        ended: list[tuple[UUID, str]] = []
         async with self._db.session() as session:
             expired = await continuation.expire_roots(
-                session, self._agent_id, ttl_hours=settings.intention_root_ttl_hours, settings=settings
+                session,
+                self._agent_id,
+                ttl_hours=settings.intention_root_ttl_hours,
+                settings=settings,
+                proposals_out=ended,
             )
             await session.commit()
         for root_id in expired:
             await self._emit("intention.root_expired", {"root_id": str(root_id)})
+        for (
+            proposal_id,
+            state,
+        ) in ended:  # an expiry ends the proposals of its root with it (the proposals sweep no longer finds them)
+            await self._emit(
+                "intention.proposal_decided", {"proposal_id": str(proposal_id), "state": state, "actor": "system"}
+            )
         return expired
 
     async def _expire_proposals(self) -> list[tuple[UUID, str]]:
@@ -1194,6 +1275,56 @@ class ContinuationRunner:
             self.wake()
         return recorded
 
+    # ------------------------------------------------------------------
+    # The owner's cancel (spec 4.6). An owner action like the three above: the REST route, the bot and (Phase 3)
+    # the A2UI card call this one function. Not a tool: no model can cancel (it can only stop its own children).
+    # ------------------------------------------------------------------
+
+    async def cancel_root(self, root_id: UUID, *, reason: str, actor: str) -> continuation.CancelOutcome:
+        """Cancel a root and everything under it (T13). In this order:
+
+        1. The store's one transaction (``continuation.cancel_root``): the marker, the lineage, its subtasks, its
+           proposals, its containers and their fires. It commits.
+        2. The in-process view takes every cancelled root, so the next tool call of the lineage is refused
+           (``AgentRunner._authorize_tool_call``). A call already past that check when the marker committed
+           runs; the one after it does not, and a spawn is refused by the database (I1) from the commit on.
+        3. The lineage's running DAGs, by the orchestrator (the store cannot): one that fails is left to the sweep.
+        4. The running turn of each cancelled root: its task is cancelled, which releases its claim (fenced: the
+           cancel already moved the rows, so it finds none), ends its session and frees its slot. A turn that is
+           past its model call and about to commit loses its fence instead.
+        5. The bus.
+
+        Raises ``continuation.RootNotFound`` and ``continuation.CancelRefused`` (nothing written)."""
+        async with self._db.session() as session:
+            outcome = await continuation.cancel_root(session, self._agent_id, root_id, reason=reason, actor=actor)
+            await session.commit()
+        self._cancelled.update(outcome.root_ids)
+        cancelled_dags = await self._cancel_dags(outcome.dag_ids)
+        stopped = await self._stop_turns(outcome.root_ids)
+        for cancelled in outcome.root_ids:
+            await self._emit("intention.root_cancelled", {"root_id": str(cancelled), "reason": reason, "actor": actor})
+        for proposal_id in outcome.proposal_ids:
+            await self._emit(
+                "intention.proposal_decided",
+                {"proposal_id": str(proposal_id), "state": continuation.PROPOSAL_CANCELLED, "actor": "system"},
+            )
+        self.wake()
+        return dataclasses.replace(outcome, cancelled_dags=cancelled_dags, turn_stopped=stopped)
+
+    async def _stop_turns(self, root_ids: Sequence[UUID]) -> bool:
+        """Cancel the arrival task of each root that has one and wait (bounded) for it to unwind. Whether any was
+        running."""
+        tasks = [task for root in root_ids if (task := self._running.get(root)) is not None]
+        for task in tasks:
+            task.cancel()
+        if tasks:
+            _done, pending = await asyncio.wait(tasks, timeout=CANCEL_WAIT_SECONDS)
+            if pending:  # a turn in a long blocking call: it was told to stop and is still unwinding
+                logger.warning(
+                    "F099: %d cancelled turn(s) are still unwinding after %ss", len(pending), CANCEL_WAIT_SECONDS
+                )
+        return bool(tasks)
+
     async def _emit_decided(self, outcome: continuation.ProposalExecution, actor: str) -> None:
         await self._emit(
             "intention.proposal_decided",
```

**Apply to `nous/main.py`:**

```diff
diff --git a/nous/main.py b/nous/main.py
index 45b60794..b55e9b47 100644
--- a/nous/main.py
+++ b/nous/main.py
@@ -316,6 +316,15 @@ async def _build_continuation_runner(
     )
     if bus is not None:
         bus.on("intention.result_ready", continuation_runner.on_result_ready)
+    # 2e: the owner's cancel reaches every tool call. The view is loaded BEFORE any loop or worker runs, so a restart
+    # forgets no cancel; the DAG cancel is bound later, where the orchestrator is built (this runs before it).
+    runner.set_cancelled_roots(continuation_runner.root_is_cancelled)
+    try:
+        await continuation_runner.load_cancelled_roots()
+    except Exception:
+        # The migrations just ran on this database, so this is improbable; the build must not fail for it: start()
+        # loads the view again, and every sweep refreshes it.
+        logger.warning("F099: could not load the cancelled roots; start() loads them again", exc_info=True)
     return continuation_runner
 
 
@@ -1428,6 +1437,10 @@ async def create_components(settings: Settings) -> dict:
                 surface_service=surface_service,
             )
 
+            if continuation_runner is not None:
+                # F099 2e: an owner's cancel stops the DAGs of its lineage through the orchestrator.
+                continuation_runner.set_cancel_dag(dag_orchestrator.cancel_dag)
+
             if heartbeat_runner is not None:
                 heartbeat_runner.dag_orchestrator = dag_orchestrator
                 # F087: the heartbeat loop is the orchestrator's only clock.
```

- [ ] **Step 4: Run** `tests/test_f099_phase2e_runner_cancel.py tests/test_f099_phase2c_loop.py tests/test_f099_phase2c_plumbing.py tests/test_f099_phase2c_failure.py tests/test_f099_phase2c_parity.py tests/test_f099_phase2d_execute.py tests/test_f099_phase2d_actions.py -q`: all pass (13 new). The claim-versus-cancel race is real: the root lock is held, the arrival's claim and the cancel queue on it in that order (Postgres grants a row lock in the order asked), and a spy on `claim_root` proves the claim won before the cancel stopped its turn. The one-sweep tests of 2c-2 still pass unchanged: the new step is isolated by `_step` and does nothing with no cancelled root.

- [ ] **Step 5: Mutation checks**
  1. Remove the `task.cancel()` loop from `_stop_turns`: `test_a_cancel_stops_the_running_turn_releases_its_slot_and_commits_nothing` fails.
  2. Remove `self._cancelled.update(outcome.root_ids)`: three tests fail, the security pin among them.
  3. Remove the refresh from `_cancel_sweep`: `test_a_restart_remembers_the_cancel_and_a_sweep_takes_in_one_made_elsewhere` fails.
  4. Drop the `ended` emit loop from `_expire`: `test_the_proposals_an_expiry_ended_are_announced_on_the_bus` fails.

- [ ] **Step 6: Lint and commit** (`feat(F099): 2e-5 ContinuationRunner.cancel_root, the view, the cancel sweep and the wiring (inert)`).

---

## Task 2e-6: the residuals: nothing undelivered by the rollback, a question's window from its push, an approved call nobody started, a push that cannot hold the sweep

**Prod runs:** the startup rollback runs in prod at every start. Its new branch is reached only for an open flag-on `continue` row with no owner channel, and prod has neither (no flag-on row exists, and the prod process has a default chat). `RollbackReport` gains a defaulted field. Everything else is called only by the runner. Pinned: `test_prods_flags_roll_back_nothing_new`, `test_a_deliverable_result_is_still_rerouted_and_counts_nothing_as_undeliverable`, `test_prods_rollback_finds_nothing_new` (2e-8).

**Files:**
- Modify: `nous/brain/continuation.py`, `nous/handlers/continuation_runner.py`, `nous/main.py` (the rollback's log line), `nous/heart/result_inbox.py` (`metrics`)
- Create: `tests/test_f099_phase2e_residuals.py`
- Modify (changed pins): the tests that age a question by hand set `created_at` back 25 h and, since a question's window now starts at its push, must clear `push_after` too: `tests/test_f099_phase2c_expiry_wake.py` (`_age_question` and two direct updates), `tests/test_f099_phase2c_plumbing.py` (one update), `tests/test_f099_phase2d_answers.py` (`_age_question`), `tests/test_f099_phase2d_routes.py` (one update)

**Interfaces:**
- Produces:
```python
ROLLBACK_UNDELIVERABLE_ID = "rollback-undeliverable"
class RollbackReport: closed, rerouted_rows, expired_proposals, pushed_raw, undeliverable: int = 0
def question_window_start(question: ResultInbox) -> datetime        # max(created_at, push_after)
async def stalled_approved_ids(session, agent_id, *, settings, now=None, limit=10) -> list[UUID]
ContinuationRunner._start_execution(proposal_id) -> asyncio.Task    # shared by decide_proposal and the resume; one per proposal
ContinuationRunner._resume_approved() -> int                        # a sweep step, after the proposal expiry
ContinuationRunner._push() -> int                                   # the push in its own task; PUSH_WAIT_SECONDS = 5.0
RESUME_BATCH = 5
```
- Rules: see the rulings above (9, 11, 12, 10).
- **M2 (plan review): no owner-facing surface reports a stamped row as delivered.** `ResultInboxStore.metrics(days)` (served by the dashboard's `result_inbox` block, the one consumer) leaves the rollback's `ROLLBACK_UNDELIVERABLE_ID` rows and the cancel's `SILENT_SESSION_ID` rows out of `delivered`, of the latencies and of the denominator of `delivery_rate` (they were never deliverable), and reports them in their own buckets, `undeliverable` and `closed_by_cancel`, per source kind. The inbox-off WARNING names the rows (`(row id, intention id)`, the first 20). (The `report:` twins of 2b count as delivered as before: a 2b decision.)
- **S6 (plan review): the flag-off rollback ends every proposal that could still run**, `approved` ones included (`_SHOWN_PROPOSAL_STATES`, with `decided_at` and `decided_by = 'system'`; `staged` ones as before). A later flag-on restart inside the proposal window therefore never resumes a call whose intention the rollback closed: its outcome would reach nobody.
- **S2 (plan review): the resume and the owner's re-tap test uses no sleep**: the fake send sets an event, the test waits for it, the re-tap returns at once (it sees `executing`), then the call is released.

- [ ] **Step 1: Write the tests**

**Create `tests/test_f099_phase2e_residuals.py`:**

```python
"""F099 Phase 2e-6: the residuals that must land before the flip: the rollback with nowhere to deliver (9), the answer
window of a question (10), the approved proposal nobody started (11) and the push that must not hold the sweep (12)."""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from f099_support import (
    CONT,
    ON,
    SEND_EMAIL_ARGS,
    SEND_EMAIL_SCHEMA,
    ask_with_proposals,
    claimed,
    commit_ask,
    env_factory,  # noqa: F401
    finish,
    inbox_rows,
    intention_of,
    make_root,
    make_subtask,
    proposal_row,
    record,
    register_send_email,
    runner_env,  # noqa: F401
    set_intention,
)
from sqlalchemy import update

import nous.handlers.continuation_runner as runner_module
from nous.brain import continuation
from nous.config import Settings
from nous.handlers.continuation_runner import ContinuationRunner
from nous.storage.models import IntentionProposal, ResultInbox

pytestmark = pytest.mark.postgres_only


# ---- carry-over 9: the rollback with nowhere to deliver ----------------------------------------------------------


def _off(env, **over) -> Settings:
    values = {"telegram_bot_token": "", "telegram_chat_id": "", **over}
    return Settings(_env_file=None, agent_id=env.agent, **values)


async def _stuck(env, *, routed=True):
    """A continue intention with its NULL-keyed result, as a flag-on process left it."""
    st = await make_subtask(env, routed=routed)
    await finish(env, st)
    await env.pool._record_inbox(st)
    return st


async def test_a_result_with_no_owner_channel_is_stamped_so_it_is_not_left_undelivered_for_good(env_factory, caplog):  # noqa: F811
    """Ruling: the intention closes (nothing can deliver it), and its row is marked delivered with its own id, so
    the backlog of undelivered, unclaimable results stays at zero. The result stays on its work row."""
    env = await env_factory(**CONT)
    st = await _stuck(env, routed=False)  # no origin channel, and the process has no default chat
    with caplog.at_level(logging.WARNING, logger="nous.brain.continuation"):
        report = await continuation.rollback_at_startup(
            env.db, _off(env, result_inbox_enabled=True), telegram_push=None
        )
    assert (report.closed, report.rerouted_rows, report.undeliverable) == (1, 0, 1)
    assert "no owner channel" in caplog.text
    (row,) = await inbox_rows(env, st.id)
    assert (row.channel, row.session_id) == (None, None)
    assert row.delivered_at is not None and row.delivered_session_id == continuation.ROLLBACK_UNDELIVERABLE_ID
    assert (await intention_of(env, "subtask", st.id)).state == "closed"
    again = await continuation.rollback_at_startup(env.db, _off(env, result_inbox_enabled=True), telegram_push=None)
    assert (again.closed, again.undeliverable) == (0, 0)


async def test_with_the_inbox_off_and_no_telegram_the_rows_are_stamped_too(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    st = await _stuck(env)
    report = await continuation.rollback_at_startup(env.db, _off(env), telegram_push=None)
    assert (report.closed, report.pushed_raw, report.undeliverable) == (1, 0, 1)
    (row,) = await inbox_rows(env, st.id)
    assert row.delivered_session_id == continuation.ROLLBACK_UNDELIVERABLE_ID


async def test_a_transient_push_failure_is_not_undeliverable_and_keeps_everything_open(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    st = await _stuck(env)

    async def failing(_text):
        return False

    report = await continuation.rollback_at_startup(env.db, _off(env), telegram_push=failing)
    assert (report.closed, report.undeliverable) == (0, 0)
    assert (await inbox_rows(env, st.id))[0].delivered_at is None
    assert (await intention_of(env, "subtask", st.id)).state == "result_ready"


async def test_a_deliverable_result_is_still_rerouted_and_counts_nothing_as_undeliverable(env_factory):  # noqa: F811  # PIN
    env = await env_factory(**CONT, telegram_chat_id="4242")
    st = await _stuck(env, routed=False)
    report = await continuation.rollback_at_startup(
        env.db, _off(env, result_inbox_enabled=True, telegram_chat_id="4242"), telegram_push=None
    )
    assert (report.rerouted_rows, report.undeliverable) == (1, 0)
    assert (await inbox_rows(env, st.id))[0].channel == "telegram:4242"


async def test_prods_flags_roll_back_nothing_new(env_factory):  # noqa: F811  # PIN
    """Prod runs inbox and intentions on with continuation off, with a default chat: no flag-on row exists, so the
    rollback finds no open continuation row, and the new branch has nothing to reach."""
    env = await env_factory(**ON, telegram_chat_id="4242")
    report = await continuation.rollback_at_startup(env.db, env.settings, telegram_push=None)
    assert report == continuation.RollbackReport(0, 0, 0, 0, 0)


async def test_the_flag_off_rollback_ends_an_approved_proposal_so_a_later_flag_on_never_resumes_it(env_factory):  # noqa: F811
    """S6 of the plan review. The owner approved, the process died before the call started, and the operator turned
    the flag off (the rollback closes the intention) and on again inside the proposal window: without this the sweep
    would run a call whose intention was closed, and its outcome would reach nobody."""
    env = await env_factory(**CONT)
    asked = await ask_with_proposals(env, count=2)
    approved, pending = asked.ids
    async with env.db.session() as s:
        await continuation.decide_proposal(
            s, env.agent, approved, approve=True, actor="telegram:42", settings=env.settings
        )
        await s.commit()
    report = await continuation.rollback_at_startup(env.db, _off(env, result_inbox_enabled=True), telegram_push=None)
    assert report.expired_proposals == 2
    for proposal_id in (approved, pending):
        row = await proposal_row(env, proposal_id)
        assert (row.state, row.decided_by) == ("expired", "system")
    async with env.db.session() as s:
        resumable = await continuation.stalled_approved_ids(
            s, env.agent, settings=env.settings, now=datetime.now(UTC) + timedelta(hours=1)
        )
    assert resumable == []  # the flag is back on, and there is nothing for the sweep to resume


async def test_the_inbox_metrics_count_a_row_nobody_read_apart_from_the_delivered_ones(env_factory):  # noqa: F811
    """M2 of the plan review: the owner-facing rule of item 9 is that nothing reports the stamp as "delivered"."""
    env = await env_factory(**CONT)
    now = datetime.now(UTC)
    async with env.db.session() as s:
        for session_id in ("S1", continuation.ROLLBACK_UNDELIVERABLE_ID, continuation.SILENT_SESSION_ID, None):
            await continuation.insert_inbox_row(
                s,
                env.agent,
                source_kind="subtask",
                source_id=uuid.uuid4(),
                msg_type="INFORM",
                title="r",
                body="b",
                delivered_at=now if session_id is not None else None,
                delivered_session_id=session_id,
            )
        await s.commit()
    bucket = (await env.heart.result_inbox.metrics(1))["subtask"]
    assert (bucket["created"], bucket["delivered"]) == (4, 1)  # only the row a chat turn read
    assert (bucket["undeliverable"], bucket["closed_by_cancel"]) == (1, 1)
    assert bucket["delivery_rate"] == 0.5  # 1 of the 2 rows that could have been delivered
    assert bucket["latency_p50_s"] is not None


async def test_the_inbox_off_warning_names_the_rows_it_marked(env_factory, caplog):  # noqa: F811
    env = await env_factory(**CONT)
    st = await _stuck(env)
    (row,) = await inbox_rows(env, st.id)
    with caplog.at_level(logging.WARNING, logger="nous.brain.continuation"):
        await continuation.rollback_at_startup(env.db, _off(env), telegram_push=None)
    assert str(row.id) in caplog.text and str(row.intention_id) in caplog.text


# ---- carry-over 10: a question's answer window starts at its push --------------------------------------------------

T0 = datetime(2026, 10, 7, 2, 0, tzinfo=UTC)  # the small hours: a push deferred to eight


async def _question(env, *, push_after):
    root, got = await claimed(env)
    await commit_ask(env, got)
    (row,) = [r for r in await inbox_rows(env) if r.msg_type == "QUESTION"]
    async with env.db.session() as s:
        await s.execute(
            update(ResultInbox).where(ResultInbox.id == row.id).values(created_at=T0, push_after=push_after)
        )
        await s.commit()
    return root, row


async def _answer(env, question, *, now):
    async with env.db.session() as s:
        out = await continuation.record_answer(
            s, env.agent, question.source_id, text="yes", actor="t", settings=env.settings, now=now
        )
        await s.commit()
    return out


async def test_a_question_deferred_by_quiet_hours_can_be_answered_for_the_whole_window_after_its_push(env_factory):  # noqa: F811
    """The same rule as a proposal's deadline (2d review m2): the window is `max(created_at, push_after) + ttl`.
    Written at 02:00 and pushed at 08:00 with a 24 h window, it is answerable until 08:00 the next day."""
    env = await env_factory(**CONT, intention_proposal_ttl_hours=24)
    root, question = await _question(env, push_after=T0 + timedelta(hours=6))
    assert continuation.question_window_start(await _fresh(env, question)) == T0 + timedelta(hours=6)
    assert (await _answer(env, question, now=T0 + timedelta(hours=29))).woke_arrival is True  # 29 h after the write


async def test_after_that_window_the_question_has_expired(env_factory):  # noqa: F811
    env = await env_factory(**CONT, intention_proposal_ttl_hours=24)
    root, question = await _question(env, push_after=T0 + timedelta(hours=6))
    with pytest.raises(continuation.AnswerRefused) as refused:
        await _answer(env, question, now=T0 + timedelta(hours=31))  # 25 h after the push
    assert refused.value.reason == "expired"


async def test_a_question_with_no_push_time_keeps_the_window_from_its_write(env_factory):  # noqa: F811  # PIN
    env = await env_factory(**CONT, intention_proposal_ttl_hours=24)
    root, question = await _question(env, push_after=None)
    with pytest.raises(continuation.AnswerRefused):
        await _answer(env, question, now=T0 + timedelta(hours=25))


async def test_the_wake_sweep_judges_a_question_by_the_same_window(env_factory):  # noqa: F811
    env = await env_factory(**CONT, intention_proposal_ttl_hours=24)
    root, question = await _question(env, push_after=T0 + timedelta(hours=6))

    async def sweep(now):
        async with env.db.session() as s:
            woken = await continuation.wake_terminal_arrivals(s, env.agent, settings=env.settings, now=now)
            await s.commit()
        return woken

    assert await sweep(T0 + timedelta(hours=29)) == []  # inside the window from the push: still waiting
    assert await sweep(T0 + timedelta(hours=31)) == [root.id]  # past it: expired unanswered, so it wakes


async def _fresh(env, row):
    async with env.db.session() as s:
        return await s.get(ResultInbox, row.id)


# ---- carry-over 11: an approved proposal nobody started ----------------------------------------------------


def _cont(env) -> ContinuationRunner:
    env.cont = ContinuationRunner(
        database=env.db,
        settings=env.settings,
        runner=env.runner,
        heart=env.heart,
        brain=env.brain,
        bus=env.bus,
        dispatcher=env.dispatcher,
    )
    return env.cont


async def _approved_and_stalled(env, *, age_seconds=600):
    """The crash window: the owner's decision committed, and nothing ran the call."""
    asked = await ask_with_proposals(env)
    (pid,) = asked.ids
    async with env.db.session() as s:
        await continuation.decide_proposal(s, env.agent, pid, approve=True, actor="telegram:42", settings=env.settings)
        await s.commit()
    then = datetime.now(UTC) - timedelta(seconds=age_seconds)
    async with env.db.session() as s:
        await s.execute(
            update(IntentionProposal).where(IntentionProposal.id == pid).values(updated_at=then, decided_at=then)
        )
        await s.commit()
    return asked, pid


async def _settle(cont):
    tasks = list(cont._executing)
    if tasks:
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=30)


async def test_the_sweep_resumes_an_approved_proposal_once_and_the_arrival_wakes(runner_env):  # noqa: F811
    env = await runner_env()
    sent = register_send_email(env, text="Message sent.")
    asked, pid = await _approved_and_stalled(env)
    assert (await proposal_row(env, pid)).state == "approved" and sent == []  # the crash left it so
    assert (await intention_of(env, "subtask", asked.root.source_id)).state == "awaiting_owner"  # and the batch waits
    cont = _cont(env)
    await cont.run_once()
    await _settle(cont)
    assert sent == [{**SEND_EMAIL_ARGS, "subject": "Snow 0"}]
    assert (await proposal_row(env, pid)).state == "executed"
    assert (await intention_of(env, "subtask", asked.root.source_id)).state == "result_ready"
    await cont.run_once()
    await _settle(cont)
    assert len(sent) == 1  # at most once: the second sweep finds nothing approved


async def test_a_fresh_approval_is_not_taken_from_the_request_that_is_running_it(runner_env):  # noqa: F811
    env = await runner_env()
    sent = register_send_email(env)
    _asked, pid = await _approved_and_stalled(env, age_seconds=1)
    cont = _cont(env)
    await cont.run_once()
    await _settle(cont)
    assert sent == [] and (await proposal_row(env, pid)).state == "approved"


async def test_an_approval_older_than_the_proposal_window_is_not_honoured_late(runner_env):  # noqa: F811
    env = await runner_env(intention_proposal_ttl_hours=1)
    sent = register_send_email(env)
    _asked, pid = await _approved_and_stalled(env, age_seconds=2 * 3600)
    cont = _cont(env)
    await cont.run_once()
    await _settle(cont)
    assert sent == [] and (await proposal_row(env, pid)).state == "approved"  # the root's TTL ends it


@pytest.mark.parametrize("marker", ["root_cancelled_at", "root_expired_at"])
async def test_an_approval_on_work_that_ended_is_ended_not_resumed(runner_env, marker):  # noqa: F811
    env = await runner_env()
    sent = register_send_email(env)
    asked, pid = await _approved_and_stalled(env)
    await set_intention(env, asked.root.id, **{marker: datetime.now(UTC)})
    cont = _cont(env)
    await cont.run_once()
    await _settle(cont)
    assert sent == [] and (await proposal_row(env, pid)).state in ("cancelled", "expired")


async def test_a_resume_and_an_owners_retap_run_the_call_once(runner_env):  # noqa: F811
    started, release = asyncio.Event(), asyncio.Event()
    env = await runner_env()
    calls = []

    async def send_email(**kwargs):
        calls.append(kwargs)
        started.set()
        await release.wait()
        return {"content": [{"type": "text", "text": "sent"}]}

    env.dispatcher.register("send_email", send_email, SEND_EMAIL_SCHEMA)
    _asked, pid = await _approved_and_stalled(env)
    cont = _cont(env)
    await cont.run_once()  # the sweep starts it
    await asyncio.wait_for(started.wait(), timeout=10)  # the call is running: its claim is `executing`
    assert len(calls) == 1 and (await proposal_row(env, pid)).state == "executing"
    # The owner's re-tap sees a call in flight and returns at once: it starts no other (so it runs before the release).
    out = await asyncio.wait_for(cont.decide_proposal(pid, approve=True, actor="telegram:42"), timeout=30)
    assert out.state == "executing" and len(calls) == 1
    release.set()
    await _settle(cont)
    assert (await proposal_row(env, pid)).state == "executed" and cont._executing_ids == {}


# ---- carry-over 12: the push does not hold the sweep ------------------------------------------------------


class SlowPublisher:
    def __init__(self) -> None:
        self.release, self.calls = asyncio.Event(), 0

    async def push_due(self, limit=20, *, now=None):
        self.calls += 1
        await self.release.wait()
        return 3


async def test_a_slow_push_does_not_delay_a_launch_and_is_never_started_twice(runner_env, monkeypatch):  # noqa: F811
    monkeypatch.setattr(runner_module, "PUSH_WAIT_SECONDS", 0.2)
    from f099_support import use

    env = await runner_env([use("resolve_intention", decision="drop", note="n", progress=False, confidence=0.5)])
    root = await make_root(env)
    await record(env, root)
    publisher = SlowPublisher()
    cont = ContinuationRunner(
        database=env.db,
        settings=env.settings,
        runner=env.runner,
        heart=env.heart,
        brain=env.brain,
        bus=env.bus,
        publisher=publisher,
    )
    report = await asyncio.wait_for(cont.run_once(), timeout=10)  # not the 200 s a stalled Telegram could cost
    assert report.launched == (root.id,) and report.pushed == 0 and publisher.calls == 1
    await asyncio.wait_for(asyncio.gather(*cont._running.values()), timeout=30)
    again = await asyncio.wait_for(cont.run_once(), timeout=10)
    assert again.pushed == 0 and publisher.calls == 1  # one push at a time
    publisher.release.set()
    await asyncio.wait_for(cont._push_task, timeout=10)
    done = await cont.run_once()
    assert done.pushed == 3 and publisher.calls == 2  # the next sweep starts the next push


async def test_stop_lets_a_push_in_flight_finish_its_send_and_then_ends_it(runner_env, monkeypatch):  # noqa: F811
    monkeypatch.setattr(runner_module, "PUSH_WAIT_SECONDS", 0.1)
    monkeypatch.setattr(runner_module, "EXECUTION_GRACE_SECONDS", 0.3)
    env = await runner_env()
    publisher = SlowPublisher()
    cont = ContinuationRunner(
        database=env.db,
        settings=env.settings,
        runner=env.runner,
        heart=env.heart,
        brain=env.brain,
        bus=env.bus,
        publisher=publisher,
    )
    await cont.run_once()
    push = cont._push_task
    assert push is not None and not push.done()
    await cont.stop()
    assert push.cancelled() or push.done()
    assert cont._push_task is None


async def test_a_failing_push_is_logged_and_the_sweep_goes_on(runner_env, caplog):  # noqa: F811
    env = await runner_env()

    class Broken:
        async def push_due(self, limit=20, *, now=None):
            raise RuntimeError("telegram is down")

    cont = ContinuationRunner(
        database=env.db,
        settings=env.settings,
        runner=env.runner,
        heart=env.heart,
        brain=env.brain,
        bus=env.bus,
        publisher=Broken(),
    )
    report = await cont.run_once()
    assert report.pushed == 0 and "owner push failed" in caplog.text
    assert (await cont.run_once()).pushed == 0  # and the next sweep tries again
```

**Apply to `tests/test_f099_phase2c_expiry_wake.py`:**

```diff
diff --git a/tests/test_f099_phase2c_expiry_wake.py b/tests/test_f099_phase2c_expiry_wake.py
index 1471d469..373ebbb4 100644
--- a/tests/test_f099_phase2c_expiry_wake.py
+++ b/tests/test_f099_phase2c_expiry_wake.py
@@ -486,7 +486,7 @@ async def _age_question(env, arrival_id, hours=25):
         await s.execute(
             update(ResultInbox)
             .where(ResultInbox.arrival_id == arrival_id, ResultInbox.msg_type == "QUESTION")
-            .values(created_at=datetime.now(UTC) - timedelta(hours=hours))
+            .values(created_at=datetime.now(UTC) - timedelta(hours=hours), push_after=None)
         )
         await s.commit()
 
@@ -509,7 +509,7 @@ async def test_an_expired_question_is_terminal_only_with_the_ttl_to_judge_it_by(
         await s.execute(
             update(ResultInbox)
             .where(ResultInbox.arrival_id == done.arrival_id, ResultInbox.msg_type == "QUESTION")
-            .values(created_at=datetime.now(UTC) - timedelta(hours=25))
+            .values(created_at=datetime.now(UTC) - timedelta(hours=25), push_after=None)
         )
         await s.commit()
     assert await _terminal(env, done.arrival_id, with_settings=False) is False
@@ -565,7 +565,7 @@ async def test_an_expired_question_wakes_with_a_row_saying_nobody_answered(env_f
         await s.execute(
             update(ResultInbox)
             .where(ResultInbox.arrival_id == done.arrival_id, ResultInbox.msg_type == "QUESTION")
-            .values(created_at=datetime.now(UTC) - timedelta(hours=25))
+            .values(created_at=datetime.now(UTC) - timedelta(hours=25), push_after=None)
         )
         await s.commit()
     assert await _wake(env) == [root.id]
```

**Apply to `tests/test_f099_phase2c_plumbing.py`:**

```diff
diff --git a/tests/test_f099_phase2c_plumbing.py b/tests/test_f099_phase2c_plumbing.py
index 466500cb..c4580459 100644
--- a/tests/test_f099_phase2c_plumbing.py
+++ b/tests/test_f099_phase2c_plumbing.py
@@ -313,7 +313,7 @@ async def test_a_failure_after_the_wake_rolls_that_arrival_back_and_the_next_sti
             await s.execute(
                 update(ResultInbox)
                 .where(ResultInbox.arrival_id == done.arrival_id, ResultInbox.msg_type == "QUESTION")
-                .values(created_at=datetime.now(UTC) - timedelta(hours=48))
+                .values(created_at=datetime.now(UTC) - timedelta(hours=48), push_after=None)
             )
             await s.commit()
         asked.append((root, done))
```

**Apply to `tests/test_f099_phase2d_answers.py`:**

```diff
diff --git a/tests/test_f099_phase2d_answers.py b/tests/test_f099_phase2d_answers.py
index a3d55ae7..d7491918 100644
--- a/tests/test_f099_phase2d_answers.py
+++ b/tests/test_f099_phase2d_answers.py
@@ -89,7 +89,7 @@ async def _age_question(env, arrival_id, hours=25):
         await s.execute(
             update(ResultInbox)
             .where(ResultInbox.arrival_id == arrival_id, ResultInbox.msg_type == "QUESTION")
-            .values(created_at=datetime.now(UTC) - timedelta(hours=hours))
+            .values(created_at=datetime.now(UTC) - timedelta(hours=hours), push_after=None)
         )
         await s.commit()
 
```

**Apply to `tests/test_f099_phase2d_routes.py`:**

```diff
diff --git a/tests/test_f099_phase2d_routes.py b/tests/test_f099_phase2d_routes.py
index 3062857a..5a5d9924 100644
--- a/tests/test_f099_phase2d_routes.py
+++ b/tests/test_f099_phase2d_routes.py
@@ -281,7 +281,8 @@ async def test_an_answer_to_an_unknown_question_or_a_proposal_is_404(runner_env)
 async def test_an_answer_after_the_question_expired_is_409_expired(runner_env):  # noqa: F811
     env = await runner_env()
     qid = await _question(env)
-    await _set(env, ResultInbox, await _row_id(env, qid), created_at=datetime.now(UTC) - timedelta(hours=25))
+    aged = datetime.now(UTC) - timedelta(hours=25)
+    await _set(env, ResultInbox, await _row_id(env, qid), created_at=aged, push_after=None)
     path = f"/intentions/questions/{qid.hex[:8]}/answer"
     response = await _call(_app(env, _runner(env)), "POST", path, json={"text": "Yes"})
     assert response.status_code == 409 and response.json()["reason"] == "expired"
```

- [ ] **Step 2: Run and watch them fail.**

- [ ] **Step 3: Implement**

**Apply to `nous/brain/continuation.py`:**

```diff
diff --git a/nous/brain/continuation.py b/nous/brain/continuation.py
index 7fb4244a..58ea4b88 100644
--- a/nous/brain/continuation.py
+++ b/nous/brain/continuation.py
@@ -1671,6 +1671,16 @@ async def _expire_root(
     return True
 
 
+def question_window_start(question: ResultInbox) -> datetime:
+    """When the answer window of a QUESTION row opens: the later of when it was written and when it may be pushed.
+    Quiet hours defer the push, not the row, so a question written at night is not answerable for a shorter time
+    than one written by day: the same rule as a proposal's deadline (``max(now, push_after) + ttl``, 2d review m2).
+    A row written outside the quiet hours has ``push_after`` at its own creation, so the two agree."""
+    if question.push_after is None:
+        return question.created_at
+    return max(question.created_at, question.push_after)
+
+
 async def _proposals_terminal(session: AsyncSession, agent_id: str, arrival_id: UUID) -> bool:
     """Every proposal of the arrival is in ``PROPOSAL_TERMINAL`` (2d). A ``staged`` row has no arrival yet, so it
     never holds an arrival back: it is not approvable."""
@@ -1718,7 +1728,7 @@ async def _question_state(
     ).scalar_one()
     ttl = timedelta(hours=float(settings.intention_proposal_ttl_hours)) if settings is not None else None
     answered = [newest_answer is not None and newest_answer >= q.created_at for q in questions]
-    expired = [ttl is not None and q.created_at <= now - ttl for q in questions]
+    expired = [ttl is not None and question_window_start(q) <= now - ttl for q in questions]
     terminal = proposals_done and all(a or e for a, e in zip(answered, expired, strict=True))
     return terminal, all(answered), questions
 
@@ -2052,6 +2062,7 @@ class RollbackReport:
     rerouted_rows: int
     expired_proposals: int
     pushed_raw: int
+    undeliverable: int = 0  # rows stamped delivered because there was nowhere to send them (2e, carry-over 9)
 
 
 _ROLLBACK_STATES = (STATE_RESULT_READY, "deciding", "awaiting_owner")
@@ -2059,6 +2070,10 @@ _SWEEP_BATCH = 200
 RAW_PUSH_CHARS = 3900
 # delivered_session_id of a row the rollback sent by Telegram instead of routing.
 ROLLBACK_SESSION_ID = "rollback"
+# delivered_session_id of a row the rollback could not deliver at all (no owner channel, or the inbox off and no
+# Telegram): the intention closes, so the row would never be read by anyone. It is stamped, so nothing counts it as
+# an undelivered result; the result itself stays on its work row (the subtask or the DAG).
+ROLLBACK_UNDELIVERABLE_ID = "rollback-undeliverable"
 
 
 async def rollback_at_startup(
@@ -2118,14 +2133,18 @@ async def rollback_at_startup(
                 stuck[row.intention_id].append(row)
 
     pushed_ids: list[UUID] = []
+    undeliverable_ids: list[UUID] = []
     keep_open: set[UUID] = set()
     if not inbox_on:
         waiting = sum(len(rows) for rows in stuck.values())
         if telegram_push is None and waiting:
+            undeliverable_ids = [row.id for rows in stuck.values() for row in rows]
             logger.warning(
                 "F099: the rollback found %d result(s) that cannot be delivered (the inbox is off and Telegram is not "
-                "configured); they stay on their work rows",
+                "configured); they stay on their work rows and are marked delivered, not read by anyone "
+                "(row, intention; the first 20): %s",
                 waiting,
+                [(str(row.id), str(row.intention_id)) for rows in stuck.values() for row in rows][:20],
             )
         elif telegram_push is not None:
             for it in open_rows:
@@ -2154,10 +2173,11 @@ async def rollback_at_startup(
                 if row_ids and channel is None:
                     logger.warning(
                         "F099: the rollback found %d result(s) of intention %s with no owner channel (no origin "
-                        "channel, no default chat); they stay on their work row",
+                        "channel, no default chat); they stay on their work row and are marked delivered",
                         len(row_ids),
                         it.id,
                     )
+                    undeliverable_ids.extend(row_ids)
                 elif row_ids:
                     # created_at is when chat can see the row, so F098's claim window starts now and
                     # not at the age of the work (a result that waited past it would never be shown).
@@ -2168,18 +2188,40 @@ async def rollback_at_startup(
                         .execution_options(synchronize_session=False)
                     )
                     rerouted += moved.rowcount or 0
+        if undeliverable_ids:
+            # The intention closes below and nothing reads these rows again: a row left undelivered would sit in the
+            # backlog for good (carry-over 9). Stamped, not deleted: the row stays as the record.
+            await session.execute(
+                update(ResultInbox)
+                .where(ResultInbox.id.in_(undeliverable_ids), ResultInbox.delivered_at.is_(None))
+                .values(delivered_at=now, delivered_session_id=ROLLBACK_UNDELIVERABLE_ID)
+                .execution_options(synchronize_session=False)
+            )
         if close_ids:
-            gone = await session.execute(
+            # S6 of the plan review: every proposal that could still run, the `approved` ones too. The intention
+            # closes below, so a call approved and not yet started would otherwise be resumed by a later flag-on
+            # sweep and its outcome would reach nobody. A later tap is refused, as before.
+            shown = await session.execute(
+                update(IntentionProposal)
+                .where(
+                    IntentionProposal.agent_id == agent_id,
+                    IntentionProposal.intention_id.in_(close_ids),
+                    IntentionProposal.state.in_(_SHOWN_PROPOSAL_STATES),
+                )
+                .values(state=PROPOSAL_EXPIRED, decided_at=now, decided_by="system", updated_at=now)
+                .execution_options(synchronize_session=False)
+            )
+            unshown = await session.execute(
                 update(IntentionProposal)
                 .where(
                     IntentionProposal.agent_id == agent_id,
                     IntentionProposal.intention_id.in_(close_ids),
-                    IntentionProposal.state.in_(("staged", "pending")),
+                    IntentionProposal.state == PROPOSAL_STAGED,
                 )
-                .values(state="expired", updated_at=now)
+                .values(state=PROPOSAL_EXPIRED, updated_at=now)
                 .execution_options(synchronize_session=False)
             )
-            expired = gone.rowcount or 0
+            expired = (shown.rowcount or 0) + (unshown.rowcount or 0)
             closed += len(
                 (
                     await session.execute(
@@ -2210,7 +2252,7 @@ async def rollback_at_startup(
             if len(swept) < _SWEEP_BATCH:
                 break
         await session.commit()
-    return RollbackReport(closed, rerouted, expired, len(pushed_ids))
+    return RollbackReport(closed, rerouted, expired, len(pushed_ids), len(undeliverable_ids))
 
 
 # ---------------------------------------------------------------------------
@@ -2914,6 +2956,38 @@ async def finish_execution(
     return ProposalExecution(proposal_id, final, proposal.result, proposal.error, woke, True, None)
 
 
+async def stalled_approved_ids(
+    session: AsyncSession, agent_id: str, *, settings: Any, now: datetime | None = None, limit: int = 10
+) -> list[UUID]:
+    """The ``approved`` proposals of an open root that nothing started (carry-over 11): the process stopped between
+    the owner's decision and ``claim_execution``, or the request that was to run the call went away. Oldest first.
+
+    Only a proposal decided at least ``max(tool_timeout + 5 s, 60 s)`` ago (an inline run, started by the
+    decision itself, is not stolen: and ``claim_execution`` is the fence in any case), and at most
+    ``intention_proposal_ttl_hours`` ago (an approval is not honoured days later: that one ends with its root).
+    ``approved`` is not terminal, so without a resume the arrival would wait in ``awaiting_owner`` until the
+    root's TTL."""
+    now = now or datetime.now(UTC)
+    grace = timedelta(seconds=max(float(settings.tool_timeout) + 5.0, 60.0))
+    window = timedelta(hours=float(settings.intention_proposal_ttl_hours))
+    root = aliased(Intention)
+    rows = await session.execute(
+        select(IntentionProposal.id)
+        .join(root, and_(root.agent_id == agent_id, root.id == IntentionProposal.root_id))
+        .where(
+            IntentionProposal.agent_id == agent_id,
+            IntentionProposal.state == PROPOSAL_APPROVED,
+            IntentionProposal.updated_at < now - grace,
+            IntentionProposal.decided_at > now - window,
+            root.root_cancelled_at.is_(None),
+            root.root_expired_at.is_(None),
+        )
+        .order_by(IntentionProposal.updated_at, IntentionProposal.id)
+        .limit(limit)
+    )
+    return list(rows.scalars().all())
+
+
 async def end_unrunnable(
     session: AsyncSession, agent_id: str, proposal_id: UUID, *, settings: Any, now: datetime | None = None
 ) -> ProposalExecution:
@@ -3136,7 +3210,7 @@ async def record_answer(
     if owner_answered:
         raise AnswerRefused(REFUSE_ANSWERED)
     ttl = timedelta(hours=float(settings.intention_proposal_ttl_hours))
-    if question.created_at <= now - ttl:
+    if question_window_start(question) <= now - ttl:
         raise AnswerRefused(REFUSE_EXPIRED)
     waiting = list(
         (
```

**Apply to `nous/handlers/continuation_runner.py`:**

```diff
diff --git a/nous/handlers/continuation_runner.py b/nous/handlers/continuation_runner.py
index d4e403a3..589f18c0 100644
--- a/nous/handlers/continuation_runner.py
+++ b/nous/handlers/continuation_runner.py
@@ -333,6 +333,9 @@ CANCEL_WAIT_SECONDS = 5.0
 # A sweep re-reads the roots cancelled since the last one, less this margin (a second process would lag by a sweep).
 CANCEL_VIEW_MARGIN_SECONDS = 120
 STRAY_DAG_BATCH = 10  # the DAGs under cancelled roots one sweep cancels
+# The sweep waits this long for the owner push, then goes on: a slow Telegram must not delay a launch (12).
+PUSH_WAIT_SECONDS = 5.0
+RESUME_BATCH = 5  # the approved calls one sweep starts again
 # What the model and the owner are told of a call whose outcome is not known. Never the exception's message: it
 # can echo the call's arguments.
 TIMEOUT_TEXT = (
@@ -379,6 +382,8 @@ class ContinuationRunner:
         self._running: dict[UUID, asyncio.Task[Any]] = {}
         # The shielded approved calls (decide_proposal): held here so that stop() can wait for them.
         self._executing: set[asyncio.Task[Any]] = set()
+        self._executing_ids: dict[UUID, asyncio.Task[Any]] = {}  # by proposal: a resume never starts one twice
+        self._push_task: asyncio.Task[int] | None = None  # the owner push in flight (2e, item 12)
         self._cooldown: dict[UUID, datetime] = {}
         self._task: asyncio.Task[None] | None = None
         # 2e: the owner's cancel. ``_cancelled`` is the in-process view AgentRunner._authorize_tool_call asks (loaded at
@@ -427,6 +432,12 @@ class ContinuationRunner:
         for task in ([loop_task] if loop_task is not None else []) + running:
             task.cancel()
         await asyncio.gather(*([loop_task] if loop_task is not None else []), *running, return_exceptions=True)
+        push, self._push_task = self._push_task, None
+        if push is not None and not push.done():
+            # A push in flight finishes its send: cancelling between the send and the stamp would send the row twice.
+            await asyncio.wait({push}, timeout=EXECUTION_GRACE_SECONDS)
+            push.cancel()
+            await asyncio.gather(push, return_exceptions=True)
         if self._executing:
             # An execution's exception is retrieved by its done callback (_execution_done), whenever it ends.
             _done, pending = await asyncio.wait(set(self._executing), timeout=EXECUTION_GRACE_SECONDS)
@@ -459,6 +470,7 @@ class ContinuationRunner:
         # release_stale_claims has already expired the staged rows of every stale claim (2d-3 review m3).
         expired_proposals = await self._step("proposal expiry", self._expire_proposals, [])
         await self._step("question wake", self._wake_questions)
+        await self._step("approved resume", self._resume_approved)
         pushed = await self._step("owner push", self._push, 0)
         launched, next_due = await self._step("launch", self._launch, ([], None))
         return continuation.SweepReport(
@@ -580,8 +592,42 @@ class ContinuationRunner:
         if woken:  # the sweep launches next, so no wake()
             logger.info("F099: woke answered or expired question(s): %s", woken)
 
+    async def _resume_approved(self) -> int:
+        """Start again the approved calls nothing started (a crash between the decision and the claim, carry-over 11).
+        Each runs in its own tracked task, like the one a decision starts, so the sweep is not held for a call;
+        ``claim_execution`` stays the fence (at most once), and a proposal already running is not started twice."""
+        async with self._db.session() as session:
+            stalled = await continuation.stalled_approved_ids(
+                session, self._agent_id, settings=self._settings, limit=RESUME_BATCH
+            )
+        for proposal_id in stalled:
+            logger.warning("F099: resuming the approved proposal %s that nothing started", proposal_id.hex[:8])
+            self._start_execution(proposal_id)
+        return len(stalled)
+
     async def _push(self) -> int:
-        return await self._publisher.push_due() if self._publisher is not None else 0
+        """The owner push, in a task of its own (2e, item 12). The publisher sends up to a batch of rows, each with
+        its own timeout, so a slow Telegram could hold this sweep (about 200 s at worst) and with it every launch.
+        The sweep starts the push (never two at once), waits ``PUSH_WAIT_SECONDS`` for it and goes on; the push
+        finishes in the background and the next sweep sees it done. Not cancelled when the wait ends: a send that
+        is cancelled between the request and the stamp would be sent again."""
+        if self._publisher is None:
+            return 0
+        task = self._push_task
+        if task is None or task.done():
+            task = self._push_task = asyncio.create_task(self._publisher.push_due(), name="continuation-push")
+            task.add_done_callback(self._push_ended)
+        done, _pending = await asyncio.wait({task}, timeout=PUSH_WAIT_SECONDS)
+        if task not in done or task.cancelled() or task.exception() is not None:
+            return 0
+        return task.result()
+
+    def _push_ended(self, task: asyncio.Task[int]) -> None:
+        """Retrieve the push's exception whenever it ends (the sweep may have stopped waiting for it)."""
+        if not task.cancelled() and task.exception() is not None:
+            logger.warning(
+                "F099: the continuation owner push failed; the next sweep tries again", exc_info=task.exception()
+            )
 
     async def _launch(self) -> tuple[list[UUID], datetime | None]:
         """Claim-and-run every root that is due, while a slot is free. The claim itself happens inside the
@@ -1151,12 +1197,26 @@ class ContinuationRunner:
             # owner approved half way (it would sit `executing` until the in-doubt sweep). Tracked, so a graceful
             # stop() waits for it (bounded); a process stop still ends it, and the proposal is failed in doubt as
             # C13 says.
-            task = asyncio.create_task(self.execute_approved_proposal(proposal_id))
-            self._executing.add(task)
-            task.add_done_callback(self._execution_done)
-            return await asyncio.shield(task)
+            return await asyncio.shield(self._start_execution(proposal_id))
         return decision
 
+    def _start_execution(self, proposal_id: UUID) -> asyncio.Task[Any]:
+        """The tracked task that runs an approved proposal (``decide_proposal`` and the sweep's resume share it). One
+        at a time per proposal: a proposal that is already running returns its task."""
+        running = self._executing_ids.get(proposal_id)
+        if running is not None and not running.done():
+            return running
+        task = asyncio.create_task(self.execute_approved_proposal(proposal_id))
+        self._executing.add(task)
+        self._executing_ids[proposal_id] = task
+        task.add_done_callback(self._execution_done)
+        task.add_done_callback(lambda done, pid=proposal_id: self._forget_execution(pid, done))
+        return task
+
+    def _forget_execution(self, proposal_id: UUID, task: asyncio.Task[Any]) -> None:
+        if self._executing_ids.get(proposal_id) is task:
+            del self._executing_ids[proposal_id]
+
     def _execution_done(self, task: asyncio.Task[Any]) -> None:
         """Forget a finished execution and retrieve its exception. A store error in the claim or the finish ends the
         task with one, and its caller may be gone (the shield case) or stop() may have stopped waiting: retrieved
```

**Apply to `nous/main.py`:**

```diff
diff --git a/nous/main.py b/nous/main.py
index b55e9b47..f357136a 100644
--- a/nous/main.py
+++ b/nous/main.py
@@ -275,14 +275,15 @@ async def _rollback_continuation(settings: Settings, database: Database) -> None
     except Exception:
         logger.warning("F099: the continuation rollback failed; it is retried at the next start", exc_info=True)
         return
-    if report.closed or report.rerouted_rows or report.expired_proposals or report.pushed_raw:
+    if report.closed or report.rerouted_rows or report.expired_proposals or report.pushed_raw or report.undeliverable:
         logger.info(
             "F099: continuation rollback closed %d intention(s), re-routed %d result(s), expired %d proposal(s), "
-            "sent %d raw result(s) by Telegram",
+            "sent %d raw result(s) by Telegram, marked %d result(s) delivered that had nowhere to go",
             report.closed,
             report.rerouted_rows,
             report.expired_proposals,
             report.pushed_raw,
+            report.undeliverable,
         )
 
 
```

**Apply to `nous/heart/result_inbox.py`:**

```diff
diff --git a/nous/heart/result_inbox.py b/nous/heart/result_inbox.py
index 54c28eea..1a8ca7ab 100644
--- a/nous/heart/result_inbox.py
+++ b/nous/heart/result_inbox.py
@@ -422,12 +422,23 @@ class ResultInboxStore:
             return await session.get(ChannelSession, (self._agent_id, channel))
 
     async def metrics(self, days: int) -> dict[str, Any]:
-        """Delivery rate and latency per source kind over the last ``days``."""
+        """Delivery rate and latency per source kind over the last ``days``.
+
+        A row a person or a runner read is ``delivered``. A row F099 closed without anyone reading it is not: the
+        rollback's ``undeliverable`` (a result with nowhere to go) and the cancel's ``closed_by_cancel`` (work the
+        owner stopped) carry ``delivered_at`` as a stamp, and counting them as delivered would improve the rate by
+        exactly the rows nobody read. They are reported in their own buckets and left out of the rate's
+        denominator (they were never deliverable) and of the latencies."""
         since = datetime.now(UTC) - timedelta(days=days)
         async with self._db.session() as session:
             rows = (
                 await session.execute(
-                    select(ResultInbox.source_kind, ResultInbox.created_at, ResultInbox.delivered_at)
+                    select(
+                        ResultInbox.source_kind,
+                        ResultInbox.created_at,
+                        ResultInbox.delivered_at,
+                        ResultInbox.delivered_session_id,
+                    )
                     .where(ResultInbox.agent_id == self._agent_id)
                     .where(ResultInbox.created_at > since)
                 )
@@ -435,11 +446,21 @@ class ResultInboxStore:
         out: dict[str, Any] = {}
         for kind in (SOURCE_SUBTASK, SOURCE_DAG, continuation.SOURCE_INTENTION_REPORT):
             mine = [r for r in rows if r[0] == kind]
-            latencies = sorted((_aware(r[2]) - _aware(r[1])).total_seconds() for r in mine if r[2] is not None)
+            undeliverable = [r for r in mine if r[2] is not None and r[3] == continuation.ROLLBACK_UNDELIVERABLE_ID]
+            closed = [r for r in mine if r[2] is not None and r[3] == continuation.SILENT_SESSION_ID]
+            latencies = sorted(
+                (_aware(r[2]) - _aware(r[1])).total_seconds()
+                for r in mine
+                if r[2] is not None
+                and r[3] not in (continuation.ROLLBACK_UNDELIVERABLE_ID, continuation.SILENT_SESSION_ID)
+            )
+            deliverable = len(mine) - len(undeliverable) - len(closed)
             out[kind] = {
                 "created": len(mine),
                 "delivered": len(latencies),
-                "delivery_rate": round(len(latencies) / len(mine), 4) if mine else None,
+                "undeliverable": len(undeliverable),
+                "closed_by_cancel": len(closed),
+                "delivery_rate": round(len(latencies) / deliverable, 4) if deliverable else None,
                 "latency_p50_s": _percentile(latencies, 0.50),
                 "latency_p95_s": _percentile(latencies, 0.95),
             }
```

- [ ] **Step 4: Run** `tests/test_f099_phase2e_residuals.py tests/test_f099_phase2b_rollback.py tests/test_f099_phase2c_loop.py tests/test_f099_phase2c_expiry_wake.py tests/test_f099_phase2c_plumbing.py tests/test_f099_phase2d_actions.py tests/test_f099_phase2d_answers.py tests/test_f099_phase2d_routes.py -q`: all pass (21 new). `report.pushed == 2` of 2c-2 is unchanged: a push that finishes inside the wait still reports its count.

- [ ] **Step 5: Mutation checks**
  1. Disable the undeliverable stamp: two tests fail.
  2. Make `question_window_start` return `created_at`: two tests fail.
  3. Remove the `"approved resume"` step: two tests fail.
  4. Wait for the push without the timeout (`asyncio.wait({task})`): the slow-publisher test times out (the sweep is held).
  5. In `metrics`, drop the sentinel exclusion from the latencies: `test_the_inbox_metrics_count_a_row_nobody_read_apart_from_the_delivered_ones` fails.
  6. In the rollback, restrict the shown-proposal UPDATE to `pending`: `test_the_flag_off_rollback_ends_an_approved_proposal_so_a_later_flag_on_never_resumes_it` fails.

- [ ] **Step 6: Lint and commit** (`feat(F099): 2e-6 residuals: rollback stamp, question window, approved resume, push task`).

---

## Task 2e-7: `GET /intentions` and `POST /intentions/{root}/cancel`

**Prod runs:** two routes that exist and answer without work. `GET /intentions` answers `200 {"roots": [], "continuation": false}` with continuation off and reads no row (Phase 1 roots exist in prod, and they are not listed: the view belongs to the continuation). `POST /intentions/{root}/cancel` looks the root up first: 404 for an id that is no root, 400 for a malformed one, 503 for a root (prod has no runner), and cancels nothing. The routes are mounted by `create_app` through `build_intention_routes`, which 2d already mounts. Pinned in this task and in 2e-8.

**Files:**
- Modify: `nous/brain/continuation.py` (`list_roots`, `ROOT_STATES`, `asdict`), `nous/owner_actions.py` (`CANCEL_REFUSALS`), `nous/api/intention_routes.py`
- Create: `tests/test_f099_phase2e_routes.py`
- Modify (changed pin): `tests/test_f099_phase2d_routes.py` (`test_the_routes_touch_only_the_runners_owner_actions` now lists `cancel_root`)

**Interfaces:**
- Produces:
```python
ROOT_STATES = ("open", "all"); LINEAGE_VIEW_MAX = 50; ARRIVALS_VIEW_MAX = 5
async def list_roots(session, agent_id, *, state: str, limit: int, settings) -> list[dict]   # ValueError for another state
CANCEL_REFUSALS = {"finished": "That work has already finished, so there was nothing to cancel."}   # nous/owner_actions.py
```
- `GET /intentions?state=open|all&limit=1..100` (default `open`, 20): `200 {"roots": [RootView], "continuation": bool}`; 400 for a bad limit or state. `RootView` is the contract's, compact: `id, short_id, intent, state, wake_policy, authority, origin_kind, origin_channel, created_at, deadline, root_cancelled_at, root_expired_at, open_rows, limits (null for a container), lineage (≤ 50), lineage_truncated, arrivals (newest 5, oldest first), open_proposals`. `open` means no marker and an open intention in the lineage.
- `POST /intentions/{root_id}/cancel` with an optional `{"reason"?, "actor"?}` (no body is allowed): `200 {"root_id","short_id","already_cancelled","cancelled_intentions","cancelled_subtasks","cancelled_dags","cancelled_proposals","deactivated_schedules","turn_stopped"}`; 400 malformed or ambiguous id or a non-object body; 404 no such root (a child's id is no root); 409 `{"error", "refusal": "finished"}`; 503 no runner (`continuation is not running`). No authentication, as every owner route (R12). The route and list docstrings say what a cancel cannot take back (an approved call already `executing`: a send in flight completes) and that, with the flag on, the open Phase 1 roots (containers included) are listed and can be cancelled, with the budgets read per root (a card asks for `limit=10`).

- [ ] **Step 1: Write the tests**

**Create `tests/test_f099_phase2e_routes.py`:**

```python
"""F099 Phase 2e-7: the owner's view of the roots (GET /intentions) and the cancel (POST /intentions/{root}/cancel)."""

from __future__ import annotations

import asyncio
import inspect
import uuid
from datetime import UTC, datetime

import httpx
import pytest
from f099_support import (
    CONT,
    ON,
    ask_with_proposals,
    claim,
    env_factory,  # noqa: F401
    finish,
    make_child,
    make_root,
    record,
    runner_env,  # noqa: F401
    set_intention,
)
from sqlalchemy import select
from starlette.applications import Starlette

from nous import owner_actions
from nous.api import intention_routes
from nous.api.intention_routes import build_intention_routes
from nous.brain import continuation
from nous.handlers.continuation_runner import ContinuationRunner
from nous.storage.models import Intention

pytestmark = pytest.mark.postgres_only


def _app(env, runner) -> Starlette:
    return Starlette(routes=build_intention_routes(database=env.db, settings=env.settings, continuation_runner=runner))


def _runner(env) -> ContinuationRunner:
    return ContinuationRunner(
        database=env.db,
        settings=env.settings,
        runner=env.runner,
        heart=env.heart,
        brain=env.brain,
        bus=env.bus,
        dispatcher=env.dispatcher,
    )


async def _call(app, method, path, **kwargs) -> httpx.Response:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://nous") as client:
        return await client.request(method, path, **kwargs)


async def _row(env, intention_id) -> Intention:
    async with env.db.session() as s:
        return (
            await s.execute(
                select(Intention).where(Intention.id == intention_id).execution_options(populate_existing=True)
            )
        ).scalar_one()


# ---- GET /intentions -----------------------------------------------------------------------------------------


async def test_the_list_shows_the_open_roots_newest_first_with_their_lineage_and_budgets(runner_env):  # noqa: F811
    env = await runner_env()
    older = await make_root(env)
    newer = await make_root(env)
    child = await make_child(env, newer)
    await record(env, newer)
    got = await claim(env, newer.id)
    assert got is not None
    response = await _call(_app(env, _runner(env)), "GET", "/intentions")
    assert response.status_code == 200
    body = response.json()
    assert body["continuation"] is True
    assert [r["id"] for r in body["roots"]] == [str(newer.id), str(older.id)]
    view = body["roots"][0]
    assert view["short_id"] == newer.id.hex[:8] and view["intent"] == newer.intent and view["state"] == "deciding"
    assert view["open_rows"] == 2 and view["root_cancelled_at"] is None
    assert [row["id"] for row in view["lineage"]] == [str(newer.id), str(child.id)]
    assert view["lineage"][1]["parent_id"] == str(newer.id) and view["lineage_truncated"] is False
    assert set(view["limits"]) == {"depth", "spawns", "turns", "tokens", "stalls", "spawn_blocked", "escalate"}
    assert view["limits"]["depth"] == 1 and view["limits"]["spawns"] == 1
    assert view["arrivals"] == [] and view["open_proposals"] == []


async def test_the_list_leaves_out_finished_cancelled_and_expired_roots_unless_asked_for_all(runner_env):  # noqa: F811
    env = await runner_env()
    open_root = await make_root(env)
    finished = await make_root(env)
    await finish(env, await env.heart.subtasks.get(uuid.UUID(finished.source_id)))
    await set_intention(env, finished.id, state="closed", close_reason="resolved")
    cancelled = await make_root(env)
    await _runner(env).cancel_root(cancelled.id, reason="t", actor="t")
    expired = await make_root(env)
    await set_intention(env, expired.id, state="expired", close_reason="expired", root_expired_at=datetime.now(UTC))
    app = _app(env, _runner(env))
    open_ids = {r["id"] for r in (await _call(app, "GET", "/intentions")).json()["roots"]}
    all_ids = {r["id"] for r in (await _call(app, "GET", "/intentions?state=all&limit=100")).json()["roots"]}
    assert open_ids == {str(open_root.id)}
    assert all_ids == {str(r.id) for r in (open_root, finished, cancelled, expired)}
    listed = {r["id"]: r for r in (await _call(app, "GET", "/intentions?state=all")).json()["roots"]}
    assert (
        listed[str(cancelled.id)]["root_cancelled_at"] is not None and listed[str(cancelled.id)]["state"] == "cancelled"
    )


async def test_the_list_carries_the_proposals_the_owner_can_still_decide_and_the_arrivals(runner_env):  # noqa: F811
    env = await runner_env()
    asked = await ask_with_proposals(env)
    (view,) = (await _call(_app(env, _runner(env)), "GET", "/intentions")).json()["roots"]
    assert view["state"] == "awaiting_owner"
    assert [p["id"] for p in view["open_proposals"]] == [str(asked.ids[0])]
    assert [(a["n"], a["decision"], a["outcome"]) for a in view["arrivals"]] == [(1, "ask", "resolved")]


async def test_a_container_is_listed_without_budgets(runner_env):  # noqa: F811
    from nous.brain.intentions import IntentionSpec

    env = await runner_env()
    await env.heart.schedules.create(
        task="watch",
        schedule_type="recurring",
        interval_seconds=1800,
        intention=IntentionSpec(intent="Watch the snow", origin_kind="interactive", container=True),
    )
    (view,) = (await _call(_app(env, _runner(env)), "GET", "/intentions")).json()["roots"]
    assert (view["wake_policy"], view["limits"]) == ("container", None)


async def test_a_bad_limit_or_state_is_400(runner_env):  # noqa: F811
    env = await runner_env()
    app = _app(env, _runner(env))
    for query in ("limit=0", "limit=101", "limit=x", "limit=%C2%B2", "state=everything"):
        assert (await _call(app, "GET", f"/intentions?{query}")).status_code == 400


# ---- POST /intentions/{root}/cancel -----------------------------------------------------------------------------


async def test_cancelling_over_rest_cancels_the_lineage_and_answers_with_the_counts(runner_env):  # noqa: F811
    env = await runner_env()
    root = await make_root(env)
    await make_child(env, root)
    app = _app(env, _runner(env))
    response = await _call(
        app,
        "POST",
        f"/intentions/{root.id.hex[:8]}/cancel",
        json={"reason": "no longer wanted", "actor": "telegram:42"},
    )
    assert response.status_code == 200
    assert response.json() == {
        "root_id": str(root.id),
        "short_id": root.id.hex[:8],
        "already_cancelled": False,
        "cancelled_intentions": 2,
        "cancelled_subtasks": 2,
        "cancelled_dags": 0,
        "cancelled_proposals": 0,
        "deactivated_schedules": 0,
        "turn_stopped": False,
    }
    assert (await _row(env, root.id)).root_cancelled_at is not None
    again = await _call(app, "POST", f"/intentions/{root.id}/cancel")  # no body at all: allowed
    assert again.status_code == 200 and again.json()["already_cancelled"] is True


async def test_a_cancel_of_work_that_is_finished_is_409_and_writes_nothing(runner_env):  # noqa: F811
    env = await runner_env()
    root = await make_root(env)
    await env.heart.subtasks.cancel(uuid.UUID(root.source_id))
    await set_intention(env, root.id, state="closed", close_reason="legacy")
    response = await _call(_app(env, _runner(env)), "POST", f"/intentions/{root.id}/cancel", json={})
    assert response.status_code == 409
    assert response.json() == {"error": owner_actions.CANCEL_REFUSALS["finished"], "refusal": "finished"}
    assert (await _row(env, root.id)).root_cancelled_at is None


async def test_a_cancel_answers_404_for_a_child_or_an_unknown_id_and_400_for_a_bad_one(runner_env):  # noqa: F811
    env = await runner_env()
    root = await make_root(env)
    child = await make_child(env, root)
    app = _app(env, _runner(env))
    assert (await _call(app, "POST", f"/intentions/{child.id}/cancel", json={})).status_code == 404
    assert (await _call(app, "POST", f"/intentions/{uuid.uuid4()}/cancel", json={})).status_code == 404
    for bad in ("nothex!!", "abc", "x" * 40):
        assert (await _call(app, "POST", f"/intentions/{bad}/cancel", json={})).status_code == 400
    assert (await _call(app, "POST", f"/intentions/{root.id}/cancel", content=b"[1, 2]")).status_code == 400
    assert (await _row(env, root.id)).root_cancelled_at is None


async def test_with_no_runner_a_known_root_is_503_and_an_unknown_one_404_and_nothing_is_cancelled(runner_env):  # noqa: F811
    """Prod's shape: Phase 1 roots exist, no runner does. A store-only cancel would cancel a real lineage without the
    runner that stops its turn and its DAGs, so the route does not cancel without one."""
    env = await runner_env()
    root = await make_root(env)
    app = _app(env, None)
    assert (await _call(app, "POST", f"/intentions/{root.id}/cancel", json={})).status_code == 503
    assert (await _call(app, "POST", f"/intentions/{uuid.uuid4()}/cancel", json={})).status_code == 404
    fresh = await _row(env, root.id)
    assert (fresh.state, fresh.root_cancelled_at) == ("pending", None)


async def test_the_cancel_stops_a_running_turn_through_the_route(runner_env):  # noqa: F811
    started, never = asyncio.Event(), asyncio.Event()

    async def blocked(_kwargs):
        started.set()
        await never.wait()

    env = await runner_env(blocked)
    root = await make_root(env)
    await record(env, root)
    cont = _runner(env)
    await cont.run_once()
    await asyncio.wait_for(started.wait(), timeout=30)
    response = await _call(_app(env, cont), "POST", f"/intentions/{root.id}/cancel", json={})
    assert response.status_code == 200 and response.json()["turn_stopped"] is True
    assert cont.running_roots == frozenset()


# ---- prod's exact flags --------------------------------------------------------------------------------------------


async def test_the_list_with_continuation_off_is_empty_and_reads_no_row(env_factory):  # noqa: F811  # PIN
    """Prod: intentions and the inbox on, continuation off. Phase 1 roots exist, and the list answers without
    reading them: the view is the continuation's."""
    env = await env_factory(**ON)
    await make_root(env)

    class NoDatabase:
        def session(self):
            raise AssertionError("the list read the database with continuation off")

    app = Starlette(
        routes=build_intention_routes(database=NoDatabase(), settings=env.settings, continuation_runner=None)
    )
    response = await _call(app, "GET", "/intentions")
    assert (response.status_code, response.json()) == (200, {"roots": [], "continuation": False})
    assert (await _call(app, "GET", "/intentions?state=all")).json() == {"roots": [], "continuation": False}


async def test_the_new_routes_are_mounted_by_create_app_and_never_shadow_the_2d_ones(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    routes = build_intention_routes(database=env.db, settings=env.settings, continuation_runner=None)
    paths = [route.path for route in routes]
    assert paths.index("/intentions/proposals") < paths.index("/intentions/{root_id}/cancel")
    assert "/intentions" in paths and paths.count("/intentions/{root_id}/cancel") == 1
    app = _app(env, None)
    assert (await _call(app, "GET", "/intentions/proposals")).json() == {"proposals": []}  # not read as a root id


def test_no_new_route_has_a_model_path():
    """Cancel is an owner action: the module that holds it registers no tool and imports no dispatcher."""
    source = inspect.getsource(intention_routes)
    assert "dispatcher" not in source and "register(" not in source
    from nous.api.tool_classes import TOOL_CLASSES

    assert "cancel_root" not in TOOL_CLASSES and "cancel_intention" not in TOOL_CLASSES


def test_the_cancel_refusal_vocabulary_is_keyed_by_the_stores_codes():  # PIN
    assert set(owner_actions.CANCEL_REFUSALS) == {continuation.REFUSE_FINISHED}
```

- [ ] **Step 2: Run and watch them fail.**

- [ ] **Step 3: Implement**

**Apply to `nous/brain/continuation.py`:**

```diff
diff --git a/nous/brain/continuation.py b/nous/brain/continuation.py
index 58ea4b88..fc4dc085 100644
--- a/nous/brain/continuation.py
+++ b/nous/brain/continuation.py
@@ -16,7 +16,7 @@ import re
 import unicodedata
 import uuid
 from collections.abc import Awaitable, Callable, Mapping
-from dataclasses import dataclass, field
+from dataclasses import asdict, dataclass, field
 from datetime import UTC, datetime, timedelta
 from typing import Any
 from uuid import UUID
@@ -3879,3 +3879,135 @@ async def end_hanging_root(
     )
     logger.info("F099: root %s was left with nothing running; it was closed and reported", root_id)
     return True
+
+
+# ---------------------------------------------------------------------------
+# F099 Phase 2e: the owner's view of the roots (contract RootView, spec 4.6 "GET /intentions")
+# ---------------------------------------------------------------------------
+
+LINEAGE_VIEW_MAX = 50  # the lineage rows one root's view lists (a root's spawn limit is 12 with continuation on)
+ARRIVALS_VIEW_MAX = 5  # the newest arrivals one root's view lists
+ROOT_STATES = ("open", "all")
+
+
+def _lineage_row(row: Intention) -> dict[str, Any]:
+    return {
+        "id": str(row.id),
+        "parent_id": str(row.parent_id) if row.parent_id is not None else None,
+        "depth": row.depth,
+        "source_kind": row.source_kind,
+        "source_id": row.source_id,
+        "state": row.state,
+        "wake_policy": row.wake_policy,
+        "intent": row.intent,
+    }
+
+
+def _limits_view(limits: RootLimits) -> dict[str, Any]:
+    """``RootLimits`` as JSON. ``stalls`` saturates at the stall limit (it says whether the budget is spent, not how
+    long the run is): shown as it is, and a surface must not present it as a total."""
+    return asdict(limits)
+
+
+async def list_roots(
+    session: AsyncSession, agent_id: str, *, state: str, limit: int, settings: Any
+) -> list[dict[str, Any]]:
+    """The roots the owner can act on, newest first, each as a JSON-ready view (contract ``RootView``), in the
+    caller's transaction. ``state`` is ``open`` (no root marker, and an intention of the lineage still open: a
+    finished lineage, and one that was cancelled or expired, are not listed) or ``all``. A view carries the root's
+    own columns, its budgets (``root_limits``, none for a container), up to ``LINEAGE_VIEW_MAX`` lineage rows, its
+    newest ``ARRIVALS_VIEW_MAX`` arrivals and its open proposals. Raises ``ValueError`` for another ``state``.
+    Read-only; four queries for the page plus the budget reads of each root."""
+    if state not in ROOT_STATES:
+        raise ValueError(f"state must be one of {ROOT_STATES}, not {state!r}")
+    query = select(Intention).where(Intention.agent_id == agent_id, Intention.id == Intention.root_id)
+    if state == "open":
+        lineage_row = aliased(Intention)
+        query = query.where(
+            Intention.root_cancelled_at.is_(None),
+            Intention.root_expired_at.is_(None),
+            exists().where(
+                lineage_row.agent_id == agent_id,
+                lineage_row.root_id == Intention.id,
+                lineage_row.state.in_(OPEN_STATES),
+            ),
+        )
+    roots = list(
+        (await session.execute(query.order_by(Intention.created_at.desc(), Intention.id).limit(limit))).scalars()
+    )
+    if not roots:
+        return []
+    ids = [root.id for root in roots]
+    lineage: dict[UUID, list[Intention]] = {root_id: [] for root_id in ids}
+    for row in (
+        await session.execute(
+            select(Intention)
+            .where(Intention.agent_id == agent_id, Intention.root_id.in_(ids))
+            .order_by(Intention.depth, Intention.created_at, Intention.id)
+        )
+    ).scalars():
+        lineage[row.root_id].append(row)
+    arrivals: dict[UUID, list[IntentionArrival]] = {root_id: [] for root_id in ids}
+    for arrival in (
+        await session.execute(
+            select(IntentionArrival)
+            .where(IntentionArrival.agent_id == agent_id, IntentionArrival.root_id.in_(ids))
+            .order_by(IntentionArrival.root_id, IntentionArrival.n.desc())
+        )
+    ).scalars():
+        if len(arrivals[arrival.root_id]) < ARRIVALS_VIEW_MAX:
+            arrivals[arrival.root_id].append(arrival)
+    proposals: dict[UUID, list[dict[str, Any]]] = {root_id: [] for root_id in ids}
+    for proposal in (
+        await session.execute(
+            select(IntentionProposal)
+            .where(
+                IntentionProposal.agent_id == agent_id,
+                IntentionProposal.root_id.in_(ids),
+                IntentionProposal.state.in_(_OPEN_PROPOSAL_STATES),
+            )
+            .order_by(IntentionProposal.created_at, IntentionProposal.id)
+        )
+    ).scalars():
+        proposals[proposal.root_id].append(proposal_view(proposal))
+    views: list[dict[str, Any]] = []
+    for root in roots:
+        rows = lineage[root.id]
+        limits = (
+            None
+            if root.wake_policy == intentions.WAKE_CONTAINER
+            else _limits_view(await root_limits(session, agent_id, root.id, settings=settings))
+        )
+        views.append(
+            {
+                "id": str(root.id),
+                "short_id": short_id(root.id),
+                "intent": root.intent,
+                "state": root.state,
+                "wake_policy": root.wake_policy,
+                "authority": root.authority,
+                "origin_kind": root.origin_kind,
+                "origin_channel": root.origin_channel,
+                "created_at": _iso(root.created_at),
+                "deadline": _iso(root.deadline),
+                "root_cancelled_at": _iso(root.root_cancelled_at),
+                "root_expired_at": _iso(root.root_expired_at),
+                "open_rows": sum(1 for row in rows if row.state in OPEN_STATES),
+                "limits": limits,
+                "lineage": [_lineage_row(row) for row in rows[:LINEAGE_VIEW_MAX]],
+                "lineage_truncated": len(rows) > LINEAGE_VIEW_MAX,
+                "arrivals": [
+                    {
+                        "n": a.n,
+                        "decision": a.decision,
+                        "progress": a.progress,
+                        "outcome": a.outcome,
+                        "gate_reason": a.gate_reason,
+                        "decided_at": _iso(a.decided_at),
+                    }
+                    for a in reversed(arrivals[root.id])
+                ],
+                "open_proposals": proposals[root.id],
+            }
+        )
+    return views
```

**Apply to `nous/owner_actions.py`:**

```diff
diff --git a/nous/owner_actions.py b/nous/owner_actions.py
index 4ebeb35c..e5bb547c 100644
--- a/nous/owner_actions.py
+++ b/nous/owner_actions.py
@@ -29,6 +29,11 @@ DECISION_REFUSALS = {
     "ended": "That work has already ended, so nothing ran.",
     "not_pending": "That proposal was already decided the other way.",
 }
+# 2e: the one refusal of a cancel. A root with nothing running is not marked: a marker would only silence a
+# later result.
+CANCEL_REFUSALS = {
+    "finished": "That work has already finished, so there was nothing to cancel.",
+}
 ANSWER_REFUSALS = {
     "answered": "That question was already answered.",
     "expired": "That question expired before it was answered.",
```

**Apply to `nous/api/intention_routes.py`:**

```diff
diff --git a/nous/api/intention_routes.py b/nous/api/intention_routes.py
index 9ae8850d..0b2c5f3f 100644
--- a/nous/api/intention_routes.py
+++ b/nous/api/intention_routes.py
@@ -21,11 +21,16 @@ from starlette.responses import JSONResponse
 from starlette.routing import Route
 
 from nous.brain import continuation
-from nous.owner_actions import ANSWER_REFUSALS, DECISION_REFUSALS  # the one refusal vocabulary (fixed sentences)
+from nous.owner_actions import (  # the one refusal vocabulary (fixed sentences)
+    ANSWER_REFUSALS,
+    CANCEL_REFUSALS,
+    DECISION_REFUSALS,
+)
 
 logger = logging.getLogger(__name__)
 
 ACTOR_MAX_CHARS = 100
+REASON_MAX_CHARS = 200
 ANSWER_MAX_CHARS = 8000  # the owner's text; record_answer clips it again to the inbox body cap
 LIST_LIMIT_MAX = 100
 NOT_RUNNING = "continuation is not running"
@@ -55,9 +60,16 @@ def _is_int(value: Any) -> bool:
     return isinstance(value, int) and not isinstance(value, bool) and -(1 << 63) <= value < 1 << 63
 
 
+def _reason(body: dict[str, Any]) -> str:
+    """Why the owner cancelled, as data: printable characters only, clipped; empty when none was given."""
+    raw = str(body.get("reason") or "")
+    return "".join(ch for ch in raw if ch.isprintable())[:REASON_MAX_CHARS].strip()
+
+
 def build_intention_routes(*, database: Any, settings: Any, continuation_runner: Any) -> list[Route]:
-    """The four owner-action routes. ``continuation_runner`` may be None, or a proxy that is falsy until the
-    component exists: it is read per request."""
+    """The owner-action routes: the four of 2d and, from 2e, the list of the owner's roots and the cancel.
+    ``continuation_runner`` may be None, or a proxy that is falsy until the component exists: it is read per
+    request."""
     agent_id = settings.agent_id
 
     async def list_proposals(request: Request) -> JSONResponse:
@@ -193,10 +205,93 @@ def build_intention_routes(*, database: Any, settings: Any, continuation_runner:
             return _error(404, "no question was sent as that message")
         return await _record(question_id, text, _actor(body))
 
-    # Literal paths first: a later /intentions/{root_id} (2e) must not shadow them.
+    async def list_roots(request: Request) -> JSONResponse:
+        """GET /intentions?state=open&limit=20: the roots the owner can act on (contract ``RootView``), newest first.
+
+        With continuation off the answer is an empty list marked ``"continuation": false`` and no row is read: the
+        view belongs to the continuation, and with it off nothing can be cancelled (the cancel answers 503), so a
+        deployment on prod's flags sees no change and pays nothing. With it on, every open root is listed, the
+        open Phase 1 roots (a schedule's container included) among them, and they can be cancelled. The budgets of
+        each root are read per root (several queries): a card should ask for ``limit=10``."""
+        state = request.query_params.get("state", "open")
+        raw_limit = request.query_params.get("limit", "20")
+        if not (raw_limit.isascii() and raw_limit.isdigit()) or not 1 <= int(raw_limit) <= LIST_LIMIT_MAX:
+            return _error(400, f"limit must be a whole number from 1 to {LIST_LIMIT_MAX}")
+        if state not in continuation.ROOT_STATES:
+            return _error(400, "state must be 'open' or 'all'")
+        if not continuation.enabled(settings):
+            return JSONResponse({"roots": [], "continuation": False})
+        async with database.session() as session:
+            views = await continuation.list_roots(
+                session, agent_id, state=state, limit=int(raw_limit), settings=settings
+            )
+        return JSONResponse({"roots": views, "continuation": True})
+
+    async def cancel(request: Request) -> JSONResponse:
+        """POST /intentions/{root_id}/cancel: the owner's cancel of a root (spec 4.6), `{"reason"?, "actor"?}`.
+
+        Looked up before the runner is needed: an id that names no root is a 404 whatever is wired, and a root with
+        no runner is a 503, so a deployment with continuation off answers 503 for a root of Phase 1 and 404 for the
+        rest, and cancels nothing (a store-only cancel would cancel a real Phase 1 lineage without the runner that
+        stops its turn and its DAGs). 409 when nothing was running. Deterministic: no model takes part and no agent
+        tool reaches it.
+
+        What a cancel cannot take back: a call the owner approved that is already ``executing`` is left alone. It is
+        refused if it has not passed the tool authorisation yet; a send already in flight completes (its outcome is
+        written to nobody: the work has ended)."""
+        raw = await request.body()
+        body: dict[str, Any] = {}
+        if raw.strip():
+            parsed = await _object_body(request)
+            if parsed is None:
+                return _error(400, "the body must be a JSON object")
+            body = parsed
+        shape = continuation.normalize_id(request.path_params["root_id"])
+        if shape is None:
+            return _error(400, "id must be 8 to 32 hex characters")
+        try:
+            async with database.session() as session:
+                root_id = await continuation.find_root_id(session, agent_id, shape)
+        except continuation.AmbiguousId:
+            return _error(400, "that id matches more than one intention: use more characters")
+        if root_id is None:
+            return _error(404, "no such intention")
+        if not continuation_runner:
+            return _error(503, NOT_RUNNING)
+        try:
+            outcome = await continuation_runner.cancel_root(root_id, reason=_reason(body), actor=_actor(body))
+        except continuation.RootNotFound:
+            return _error(404, "no such intention")
+        except continuation.CancelRefused as refused:
+            return _error(
+                409,
+                CANCEL_REFUSALS.get(refused.reason, "That work cannot be cancelled."),
+                refusal=refused.reason,
+            )
+        except Exception:
+            logger.exception("F099: cancelling root %s failed", root_id.hex[:8])
+            return _error(500, "the cancel could not be processed")
+        return JSONResponse(
+            {
+                "root_id": str(outcome.root_id),
+                "short_id": continuation.short_id(outcome.root_id),
+                "already_cancelled": outcome.already_cancelled,
+                "cancelled_intentions": outcome.cancelled_intentions,
+                "cancelled_subtasks": outcome.cancelled_subtasks,
+                "cancelled_dags": outcome.cancelled_dags,
+                "cancelled_proposals": outcome.cancelled_proposals,
+                "deactivated_schedules": outcome.deactivated_schedules,
+                "turn_stopped": outcome.turn_stopped,
+            }
+        )
+
+    # Literal paths first: /intentions/{root_id}/cancel must not shadow them (it has its own suffix, but order keeps
+    # the rule one sentence long).
     return [
         Route("/intentions/proposals", list_proposals),
         Route("/intentions/proposals/{id}/decide", decide, methods=["POST"]),
         Route("/intentions/questions/answer", answer_by_message, methods=["POST"]),
         Route("/intentions/questions/{id}/answer", answer, methods=["POST"]),
+        Route("/intentions", list_roots),
+        Route("/intentions/{root_id}/cancel", cancel, methods=["POST"]),
     ]
```

**Apply to `tests/test_f099_phase2d_routes.py`:**

```diff
diff --git a/tests/test_f099_phase2d_routes.py b/tests/test_f099_phase2d_routes.py
index 5a5d9924..8994acaf 100644
--- a/tests/test_f099_phase2d_routes.py
+++ b/tests/test_f099_phase2d_routes.py
@@ -368,7 +368,11 @@ def test_the_routes_touch_only_the_runners_owner_actions():
     """Surface neutrality: the cards of Phase 3 call the same two functions; the module reaches the runner through
     nothing else, and no model-facing object."""
     source = (Path(__file__).resolve().parents[1] / "nous" / "api" / "intention_routes.py").read_text(encoding="utf-8")
-    assert set(re.findall(r"continuation_runner\.(\w+)", source)) == {"decide_proposal", "answer_question"}
+    assert set(re.findall(r"continuation_runner\.(\w+)", source)) == {
+        "decide_proposal",
+        "answer_question",
+        "cancel_root",
+    }
     assert "dispatcher" not in source and "AgentRunner" not in source
 
 
```

- [ ] **Step 4: Run** `tests/test_f099_phase2e_routes.py tests/test_f099_phase2d_routes.py tests/test_f099_phase2d_parity.py -q`: all pass (14 new).

- [ ] **Step 5: Mutation checks**
  1. Remove the `continuation.enabled` gate from the list: the prod pin fails (it reads the database).
  2. Remove the `if not continuation_runner` guard from the cancel: the no-runner test fails (the store would cancel a Phase 1 root).

- [ ] **Step 6: Lint and commit** (`feat(F099): 2e-7 GET /intentions and the owner's cancel route`).

---

## Task 2e-8: the bot's `/intentions` and `/cancel_intention`, and the prod parity pins

**Prod runs:** the bot is a separate process that exists in prod, so this is the one task where prod code runs differently, and it is built so that nothing the owner or the agent can see differs. Prod's telegram service has no `NOUS_TELEGRAM_CHAT_ID` today, so `owner_chat_id` is None: `_is_owner` is False and both commands are chat, with no request made. Once the owner adds the chat id (a deploy note), `/intentions` asks the server, which answers `continuation: false` while the flag is off, and the bot then passes the message on to chat unchanged (so the flag-off behaviour is the same as today's); `/cancel_intention` with a root id of Phase 1 gets the server's 503 and says "not running its follow-up work", and with any other id a 404 and goes to chat. The parity file is the collected proof for the whole PR; it also pins N2 of the plan review (`test_a_view_that_cannot_be_loaded_does_not_fail_the_build`).

**Files:**
- Modify: `nous/telegram_bot.py` (not format-clean on the base: edit, do not reformat)
- Create: `tests/test_f099_phase2e_bot.py`, `tests/test_f099_phase2e_parity.py`

**Interfaces:**
- Produces (module level in `nous/telegram_bot.py`): `describe_intentions(body: dict) -> str` (HTML; each root's intent is HTML-escaped inside `<pre>`, one printable line clipped to 160 characters, at most 10 roots, at most 3900 characters, a short id accepted only as 8 hex characters, a state word looked up in `ROOT_STATE_WORDS`); `describe_cancel(status: int, body: dict, short_id: str) -> str` (fixed vocabulary). On the bot: `_owner_get(path) -> tuple[int, dict]`; `_handle_owner_text` handles `/intentions` (no argument; `GET /intentions?state=open&limit=10`; consumed only on a 200 with `"continuation": true`) and `/cancel_intention <id>` (one id of 8 to 32 hex characters; `POST /intentions/{id}/cancel` with `{"actor": "telegram:<user>", "reason": "cancelled from Telegram"}`; a 404 and a malformed id fall through to chat).

- [ ] **Step 1: Write the tests**

**Create `tests/test_f099_phase2e_bot.py`:**

```python
"""F099 Phase 2e-8: the bot's `/intentions` and `/cancel_intention`. Parsed in code, answered from fixed vocabulary,
and passed on to chat whenever the server does not give a definite answer (lead ruling C18: strict prod parity)."""

from __future__ import annotations

import pytest
from test_f099_phase2d_bot import _bot, _message, _methods, _Response, _sent

from nous import owner_actions
from nous.telegram_bot import describe_cancel, describe_intentions

ROOT = "ab12cd34" + "0" * 24
SHORT = ROOT[:8]
CANCEL = f"/intentions/{SHORT}/cancel"
LIST = "/intentions?state=open&limit=10"
NOT_RUNNING = "Nous is not running its follow-up work."
UNREACHABLE = "I could not reach Nous. Try again in a moment."


class _Http:
    """The REST API as the bot sees it: ``posts`` and ``gets`` map a path to ``(status, body)``; any other is a 404."""

    def __init__(self, *, posts=None, gets=None, down=False):
        self.posts, self.gets, self.down = posts or {}, gets or {}, down
        self.calls: list[tuple[str, str, dict | None]] = []

    async def post(self, url, json=None, timeout=None):  # noqa: A002 - httpx's keyword
        path = url.removeprefix("http://nous.test")
        self.calls.append(("POST", path, json))
        if self.down:
            raise ConnectionError("down")
        return _Response(*self.posts.get(path, (404, {"error": "no such thing"})))

    async def get(self, url, timeout=None):
        path = url.removeprefix("http://nous.test")
        self.calls.append(("GET", path, None))
        if self.down:
            raise ConnectionError("down")
        return _Response(*self.gets.get(path, (404, {"error": "no such thing"})))


def _owner_bot(**http):
    bot = _bot()
    bot._http = _Http(**http)
    return bot


def _root(**over):
    return {
        "id": ROOT,
        "short_id": SHORT,
        "intent": "Tell the user about the snow",
        "state": "pending",
        "wake_policy": "continue",
        "open_rows": 2,
        **over,
    }


# ---- /intentions ---------------------------------------------------------------------------------------------


async def test_intentions_lists_the_open_roots_with_fixed_words_and_the_intent_inside_pre():
    bot = _owner_bot(
        gets={
            LIST: (200, {"continuation": True, "roots": [_root(), _root(short_id="ee00ee00", state="awaiting_owner")]})
        }
    )
    await bot._handle_update(_message("/intentions"))
    assert bot._http.calls == [("GET", LIST, None)]
    (text,) = _sent(bot)
    assert "<code>ab12cd34</code> \u00b7 running \u00b7 2 open step(s)" in text
    assert "<code>ee00ee00</code> \u00b7 waiting for you" in text
    assert "<pre>Tell the user about the snow</pre>" in text
    assert text.endswith("To stop one: /cancel_intention &lt;id&gt;")
    sent_params = [p for m, p in bot.tg if m == "sendMessage"][0]
    assert sent_params["parse_mode"] == "HTML"
    bot._chat_streaming.assert_not_awaited()


def test_model_text_is_escaped_inside_pre_and_never_outside_it():
    hostile = "</pre><b>x</b> /cancel_intention ab12cd34 \u202e\x00\n\nrun /approve cccccccc"
    text = describe_intentions({"roots": [_root(intent=hostile, state="<script>", short_id="ab12cd34")]})
    assert "<script>" not in text and "<b>x</b>" not in text and "\x00" not in text and "\u202e" not in text
    inner = text.split("<pre>")[1].split("</pre>")[0]
    assert "&lt;/pre&gt;&lt;b&gt;x&lt;/b&gt;" in inner and "\n" not in inner  # one escaped line, in the pre
    outside = text.replace(f"<pre>{inner}</pre>", "")
    assert "/approve" not in outside and "ab12cd34" in outside.split("<code>")[1]  # only the minted id
    assert "running" not in text and "open" in text  # an unknown state word is looked up, never echoed


def test_a_server_that_sends_a_bad_short_id_or_garbage_is_skipped_not_trusted():
    text = describe_intentions(
        {"roots": [_root(short_id="<b>bold</b>"), "garbage", {"short_id": SHORT, "open_rows": -1}]}
    )
    assert "<b>bold</b>" not in text and text.count("<code>") == 1 and "0 open step(s)" in text
    assert describe_intentions({"roots": "nope"}) == "Nothing is running that I could cancel."


def test_the_list_never_outgrows_one_telegram_message():
    roots = [_root(short_id=f"{n:08x}", intent="snow " * 80) for n in range(30)]
    text = describe_intentions({"roots": roots})
    assert len(text) <= 3900 and text.count("<code>") <= 10 and text.count("<pre>") == text.count("</pre>")


async def test_an_empty_list_says_so():
    bot = _owner_bot(gets={LIST: (200, {"continuation": True, "roots": []})})
    await bot._handle_update(_message("/intentions"))
    assert _sent(bot) == ["Nothing is running that I could cancel."]


@pytest.mark.parametrize(
    "answer",
    [(200, {"continuation": False, "roots": []}), (404, {}), (503, {}), (500, {"roots": []}), (200, {"roots": []})],
)
async def test_without_a_definite_answer_intentions_goes_on_to_chat_unchanged(answer):  # PIN
    """Prod parity: continuation off (the route says so), an older server, an error or an outage: the message is
    ordinary chat, exactly as before 2e."""
    bot = _owner_bot(gets={LIST: answer})
    await bot._handle_update(_message("/intentions"))
    assert _sent(bot) == []
    bot._chat_streaming.assert_awaited_once()
    assert bot._chat_streaming.await_args.args[1] == "/intentions"


async def test_intentions_with_the_server_down_goes_on_to_chat():
    bot = _owner_bot(down=True)
    await bot._handle_update(_message("/intentions"))
    bot._chat_streaming.assert_awaited_once()


async def test_intentions_with_extra_words_is_chat():
    bot = _owner_bot(gets={LIST: (200, {"continuation": True, "roots": [_root()]})})
    await bot._handle_update(_message("/intentions please"))
    assert bot._http.calls == []
    bot._chat_streaming.assert_awaited_once()


async def test_with_no_owner_chat_set_intentions_and_cancel_make_no_request_and_go_to_chat():  # PIN
    """Prod today: the telegram service has no owner chat, so the owner commands are inert and every message is chat."""
    for text in ("/intentions", f"/cancel_intention {SHORT}"):
        bot = _bot(owner=None)
        bot._http = _Http(gets={LIST: (200, {"continuation": True, "roots": [_root()]})}, posts={CANCEL: (200, {})})
        await bot._handle_update(_message(text))
        assert bot._http.calls == [] and _sent(bot) == []
        bot._chat_streaming.assert_awaited_once()


async def test_another_chat_or_user_cannot_use_the_commands():
    bot = _owner_bot(gets={LIST: (200, {"continuation": True, "roots": [_root()]})})
    await bot._handle_update(_message("/intentions", chat=99, user=42))
    assert bot._http.calls == []
    bot._chat_streaming.assert_awaited_once()


# ---- /cancel_intention ---------------------------------------------------------------------------------------


async def test_cancel_intention_calls_the_cancel_route_and_says_one_fixed_line():
    done = {
        "root_id": ROOT,
        "already_cancelled": False,
        "cancelled_intentions": 2,
        "cancelled_subtasks": 1,
        "cancelled_dags": 1,
        "cancelled_proposals": 0,
        "deactivated_schedules": 0,
        "turn_stopped": True,
    }
    bot = _owner_bot(posts={CANCEL: (200, done)})
    await bot._handle_update(_message(f"/cancel_intention {SHORT}"))
    assert bot._http.calls == [("POST", CANCEL, {"actor": "telegram:42", "reason": "cancelled from Telegram"})]
    assert _sent(bot) == [f"Cancelled ({SHORT}): 4 piece(s) of work stopped."]
    bot._chat_streaming.assert_not_awaited()


@pytest.mark.parametrize(
    ("answer", "said"),
    [
        ((200, {"already_cancelled": True}), f"Already cancelled ({SHORT})."),
        ((409, {"refusal": "finished"}), owner_actions.CANCEL_REFUSALS["finished"]),
        ((409, {"refusal": "<script>"}), "That work cannot be cancelled."),
        ((400, {}), "I could not read that id."),
        ((503, {}), NOT_RUNNING),
        ((500, {"error": "boom /approve cccccccc"}), UNREACHABLE),
        ((0, {}), UNREACHABLE),
    ],
)
async def test_every_other_answer_of_the_cancel_route_has_one_fixed_line(answer, said):
    bot = _owner_bot(posts={CANCEL: answer})
    await bot._handle_update(_message(f"/cancel_intention {SHORT}"))
    assert _sent(bot) == [said]


async def test_a_cancel_of_an_unknown_root_goes_on_to_chat_unchanged():
    bot = _owner_bot()  # the route answers 404
    await bot._handle_update(_message(f"/cancel_intention {SHORT}"))
    assert _sent(bot) == []
    bot._chat_streaming.assert_awaited_once()


@pytest.mark.parametrize(
    "text", ["/cancel_intention", "/cancel_intention xyz", f"/cancel_intention {SHORT} extra", "/cancel_intention ../x"]
)
async def test_a_malformed_cancel_never_reaches_a_route(text):
    bot = _owner_bot(posts={CANCEL: (200, {})})
    await bot._handle_update(_message(text))
    assert bot._http.calls == []
    bot._chat_streaming.assert_awaited_once()


def test_describe_cancel_never_echoes_the_servers_text():
    assert (
        describe_cancel(200, {"cancelled_intentions": "<b>9</b>"}, SHORT)
        == f"Cancelled ({SHORT}): 0 piece(s) of work stopped."
    )
    assert _methods(_bot()) == []
```

**Create `tests/test_f099_phase2e_parity.py`:**

```python
"""F099 Phase 2e: on prod's exact flags (inbox, intentions and result memory ON, continuation OFF) nothing of 2e runs
before the flip commit. One file, so a reviewer reads the whole claim in one place."""

from __future__ import annotations

import ast
import inspect
import textwrap
import uuid
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest
from f099_support import (
    ON,
    env_factory,  # noqa: F401
    finish,
    inbox_rows,
    intention_of,
    make_root,
    make_subtask,
    runner_env,  # noqa: F401
)
from sqlalchemy import select
from starlette.applications import Starlette
from test_f099_phase2c_parity import PROD, NoDatabase, Untouchable

import nous.main as main
from nous.api import runner as runner_module
from nous.api.intention_routes import build_intention_routes
from nous.api.rest import create_app
from nous.brain import continuation
from nous.config import Settings
from nous.handlers.continuation_runner import ContinuationRunner
from nous.heart.result_reconciler import build_reconciler, repair_missing_results
from nous.storage.models import Intention

pytestmark = pytest.mark.postgres_only


async def _call(app, method, path, **kwargs) -> httpx.Response:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://nous") as client:
        return await client.request(method, path, **kwargs)


# ---- the store ---------------------------------------------------------------------------------------------


async def test_prods_writers_and_passes_never_reach_a_2e_store_path(env_factory, monkeypatch):  # noqa: F811  # PIN
    """Every 2e change to the store is behind the runner or behind ``continuation.enabled``. On prod's flags a
    subtask finishes, the reconciler ticks and the repair runs, and none of the changed functions is called."""

    def forbidden(name):
        async def raiser(*args, **kwargs):
            raise AssertionError(f"continuation.{name} ran on prod's flags")

        return raiser

    for name in (
        "record_result",
        "cancel_root",
        "close_cancelled_source",
        "end_hanging_root",
        "fail_attempt",
        "expire_roots",
        "stray_dag_ids",
        "stalled_approved_ids",
        "list_roots",
    ):
        monkeypatch.setattr(continuation, name, forbidden(name))
    env = await env_factory(**PROD, telegram_chat_id="4242")
    st = await make_subtask(env)  # a continue-policy subtask, spawned from chat
    await finish(env, st)
    assert await env.pool._record_inbox(st) in (True, False, None)  # the F098 writer, whatever it reports
    (row,) = await inbox_rows(env, st.id)
    assert row.channel == "telegram:8080" and row.delivered_at is None  # keyed by its channel, as in Phase 1
    assert (await intention_of(env, "subtask", st.id)).close_reason == "legacy"  # not 'delivered', not result_ready
    assert await repair_missing_results(env.db, env.heart.result_inbox, env.settings, limit=50) == 0
    reconciler = build_reconciler(env.db, env.heart.result_inbox, env.settings, continuation_wake=MagicMock())
    await reconciler.run_once()  # none of its passes touches a forbidden function


async def test_prods_rollback_finds_nothing_new(env_factory):  # noqa: F811  # PIN
    env = await env_factory(**ON, telegram_chat_id="4242")
    report = await continuation.rollback_at_startup(env.db, env.settings, telegram_push=None)
    assert report == continuation.RollbackReport(0, 0, 0, 0, 0)


async def test_a_new_intention_starts_with_no_failed_tokens(env_factory):  # noqa: F811  # PIN
    env = await env_factory(**PROD)
    root = await make_root(env)
    async with env.db.session() as s:
        row = (await s.execute(select(Intention).where(Intention.id == root.id))).scalar_one()
    assert row.failed_tokens == 0


def test_2e_added_one_migration_and_no_setting():  # PIN: changes when a later PR adds one on purpose
    migrations = sorted((Path(__file__).resolve().parents[1] / "sql" / "migrations").glob("*.sql"))
    assert migrations[-1].name == "085_intention_failed_tokens_and_cancel_index.sql"
    assert not [name for name in Settings.model_fields if "cancel" in name or "failed_tokens" in name]


# ---- the runner, the view and the wiring ------------------------------------------------------------------------


def test_the_cancel_view_is_a_set_lookup_and_the_default_reads_nothing():  # PIN
    """`_authorize_tool_call` runs on every tool call of every lineage turn in prod: its added cost is one
    attribute read and one call. With no runner the call is a function that returns False; with one it is a set
    membership. Neither awaits, neither reads a row."""
    assert not inspect.iscoroutinefunction(ContinuationRunner.root_is_cancelled)
    tree = ast.parse(textwrap.dedent(inspect.getsource(ContinuationRunner.root_is_cancelled)))
    assert not [node for node in ast.walk(tree) if isinstance(node, ast.Await)]
    assert runner_module._no_cancelled_roots(object()) is False
    source = inspect.getsource(runner_module.AgentRunner._authorize_tool_call)
    strict_block = source.index("if ctx.authority == AUTHORITY_INTERNAL or ctx.kind")
    head = source[:strict_block]  # the new check comes before even the strict rule: the first thing it does
    assert "self._root_cancelled(ctx.root_intention_id)" in head


async def test_prods_flags_install_no_view_and_build_no_runner(monkeypatch):  # PIN
    """Even with the constant flipped (the flip commit), prod's flags build no runner, so no view is installed."""
    monkeypatch.setattr(continuation, "CONTINUATION_RUNNER_READY", True)
    agent_runner = MagicMock()
    untouched = {name: Untouchable() for name in ("database", "heart", "brain", "bus", "dispatcher")}
    built = await main._build_continuation_runner(Settings(_env_file=None, **PROD), runner=agent_runner, **untouched)
    assert built is None
    agent_runner.set_cancelled_roots.assert_not_called()


async def test_a_built_runner_installs_its_view_and_loads_it_before_anything_runs(runner_env, monkeypatch):  # noqa: F811
    monkeypatch.setattr(continuation, "CONTINUATION_RUNNER_READY", True)
    env = await runner_env()
    agent_runner = MagicMock()
    order = []
    agent_runner.set_cancelled_roots.side_effect = lambda view: order.append(("view", view))
    real = ContinuationRunner.load_cancelled_roots

    async def load(self):
        order.append(("load", None))
        return await real(self)

    monkeypatch.setattr(ContinuationRunner, "load_cancelled_roots", load)
    built = await main._build_continuation_runner(
        env.settings,
        database=env.db,
        runner=agent_runner,
        heart=env.heart,
        brain=env.brain,
        bus=None,
        dispatcher=env.dispatcher,
    )
    assert [kind for kind, _ in order] == ["view", "load"]
    assert order[0][1] == built.root_is_cancelled and built._task is None  # wired, loaded, not started


async def test_a_view_that_cannot_be_loaded_does_not_fail_the_build(runner_env, monkeypatch, caplog):  # noqa: F811
    """N2 of the plan review: `start()` loads the view again and every sweep refreshes it, so a transient error in
    the first load is a warning, never a failed `create_components`."""
    monkeypatch.setattr(continuation, "CONTINUATION_RUNNER_READY", True)
    env = await runner_env()
    agent_runner = MagicMock()

    async def failing(self):
        raise RuntimeError("the database blinked")

    monkeypatch.setattr(ContinuationRunner, "load_cancelled_roots", failing)
    built = await main._build_continuation_runner(
        env.settings,
        database=env.db,
        runner=agent_runner,
        heart=env.heart,
        brain=env.brain,
        bus=None,
        dispatcher=env.dispatcher,
    )
    assert isinstance(built, ContinuationRunner)
    agent_runner.set_cancelled_roots.assert_called_once_with(built.root_is_cancelled)
    assert "could not load the cancelled roots" in caplog.text


def test_main_binds_the_orchestrators_cancel_where_the_orchestrator_exists():  # PIN
    source = inspect.getsource(main.create_components)
    bound = source.index("continuation_runner.set_cancel_dag(dag_orchestrator.cancel_dag)")
    assert source.index("dag_orchestrator = DAGOrchestrator(") < bound
    assert source.rindex("if continuation_runner is not None:", 0, bound) > source.index(
        "dag_orchestrator = DAGOrchestrator("
    )


async def test_an_inert_runner_runs_none_of_the_2e_sweep_on_prods_flags():  # PIN
    settings = Settings(_env_file=None, telegram_bot_token="test-token", **PROD)
    db = NoDatabase()
    runner = ContinuationRunner(
        database=db, settings=settings, runner=Untouchable(), heart=Untouchable(), brain=Untouchable()
    )
    await runner.run_once()
    await runner.start()
    assert db.sessions == 0 and runner._cancelled == set() and runner._push_task is None
    assert not runner.root_is_cancelled(uuid.uuid4())


# ---- REST ---------------------------------------------------------------------------------------------------------


async def test_the_new_routes_on_prods_flags_answer_empty_503_and_404_and_change_nothing(env_factory):  # noqa: F811  # PIN
    env = await env_factory(**PROD)
    root = await make_root(env)  # a Phase 1 root exists in prod
    app = Starlette(routes=build_intention_routes(database=env.db, settings=env.settings, continuation_runner=None))
    listed = await _call(app, "GET", "/intentions")
    assert (listed.status_code, listed.json()) == (200, {"roots": [], "continuation": False})
    assert (await _call(app, "POST", f"/intentions/{root.id}/cancel", json={})).status_code == 503
    assert (await _call(app, "POST", f"/intentions/{'ab' * 16}/cancel", json={})).status_code == 404
    fresh = await intention_of(env, "subtask", root.source_id)
    assert (fresh.state, fresh.root_cancelled_at) == ("pending", None)


async def test_the_new_routes_read_no_row_to_answer_the_list_on_prods_flags(env_factory):  # noqa: F811  # PIN
    env = await env_factory(**PROD)
    app = Starlette(
        routes=build_intention_routes(database=NoDatabase(), settings=env.settings, continuation_runner=None)
    )
    assert (await _call(app, "GET", "/intentions?state=all&limit=100")).json() == {"roots": [], "continuation": False}


def test_create_app_mounts_the_new_routes_and_keeps_the_old_ones():
    settings = Settings(_env_file=None, **PROD)
    app = create_app(MagicMock(), MagicMock(), MagicMock(), MagicMock(), MagicMock(), settings)
    paths = {getattr(route, "path", None) for route in app.routes}
    assert {"/intentions", "/intentions/{root_id}/cancel", "/intentions/proposals"} <= paths
    assert {"/chat", "/status", "/decisions", "/subtasks/{id}", "/schedules"} <= paths  # nothing was displaced


def test_a_cancel_is_not_a_tool():  # PIN: Review Focus 1
    from nous.api.tool_classes import TOOL_CLASSES

    for name in ("cancel_root", "cancel_intention", "intentions", "list_intentions"):
        assert name not in TOOL_CLASSES
    source = inspect.getsource(main.create_components)
    assert 'register("cancel_root"' not in source and "register('cancel_root'" not in source
```

- [ ] **Step 2: Run and watch the bot tests fail** (`describe_intentions` does not exist). The parity file passes except where it names code from earlier tasks.

- [ ] **Step 3: Implement**

**Apply to `nous/telegram_bot.py`:**

```diff
diff --git a/nous/telegram_bot.py b/nous/telegram_bot.py
index 4122db38..62e36df8 100644
--- a/nous/telegram_bot.py
+++ b/nous/telegram_bot.py
@@ -29,7 +29,7 @@ import httpx
 from nous.api.attachments import classify_attachment, sanitize_filename
 from nous.api.models import Attachment
 from nous.log_redaction import configure_logging
-from nous.owner_actions import ANSWER_REFUSALS, DECISION_REFUSALS, parse_callback
+from nous.owner_actions import ANSWER_REFUSALS, CANCEL_REFUSALS, DECISION_REFUSALS, parse_callback
 
 logger = logging.getLogger(__name__)
 
@@ -129,6 +129,71 @@ def describe_answer(status: int, body: dict) -> str:
     return _UNREACHABLE
 
 
+INTENTIONS_SHOWN = 10  # the roots /intentions lists (the route is asked for no more)
+INTENT_SHOWN_CHARS = 160  # one root's intent, as /intentions shows it
+TELEGRAM_TEXT_LIMIT = 3900  # a message stays under Telegram's 4096
+# Fixed vocabulary for a root's state: the state names of the server are looked up, never echoed.
+ROOT_STATE_WORDS = {
+    "pending": "running",
+    "result_ready": "result ready",
+    "deciding": "thinking",
+    "awaiting_owner": "waiting for you",
+}
+_SHORT_ID_RE = re.compile(r"[0-9a-f]{8}")
+
+
+def _shown(text: object, limit: int) -> str:
+    """Model-authored text as one printable line, clipped: control characters and runs of white space become a space."""
+    cleaned = " ".join("".join(ch if ch.isprintable() else " " for ch in str(text or "")).split())
+    return cleaned if len(cleaned) <= limit else cleaned[: limit - 1].rstrip() + "\u2026"
+
+
+def describe_intentions(body: dict) -> str:
+    """The HTML text of ``/intentions`` for the body of ``GET /intentions``. The roots' intents are model-authored
+    (a spawning turn wrote them from whatever it read), so each sits inside ``<pre>``, HTML-escaped, where Telegram
+    parses no command, link or mention; everything outside ``<pre>`` is fixed words, a short id the server minted
+    (checked to be 8 hex characters) and a looked-up state word."""
+    roots = body.get("roots")
+    shown = [r for r in roots if isinstance(r, dict)][:INTENTIONS_SHOWN] if isinstance(roots, list) else []
+    if not shown:
+        return "Nothing is running that I could cancel."
+    lines = ["<b>Running</b>"]
+    for root in shown:
+        short = root.get("short_id")
+        if not isinstance(short, str) or not _SHORT_ID_RE.fullmatch(short):
+            continue
+        steps = root.get("open_rows")
+        steps = steps if isinstance(steps, int) and not isinstance(steps, bool) and steps >= 0 else 0
+        state = ROOT_STATE_WORDS.get(str(root.get("state")), "open")
+        if root.get("wake_policy") == "container":
+            state = "scheduled"
+        lines.append(f"<code>{short}</code> \u00b7 {state} \u00b7 {steps} open step(s)")
+        lines.append(f"<pre>{html_module.escape(_shown(root.get('intent'), INTENT_SHOWN_CHARS))}</pre>")
+    lines.append("To stop one: /cancel_intention &lt;id&gt;")
+    text = "\n".join(lines)
+    return text if len(text) <= TELEGRAM_TEXT_LIMIT else text[:TELEGRAM_TEXT_LIMIT].rsplit("\n", 1)[0]
+
+
+def describe_cancel(status: int, body: dict, short_id: str) -> str:
+    """The text for the answer of the cancel route (fixed vocabulary, like ``describe_decision``). Not called for a
+    404: a command whose id the server does not know goes on to chat (C18)."""
+    if status == 200:
+        counts = [body.get(k) for k in ("cancelled_intentions", "cancelled_subtasks", "cancelled_dags")]
+        stopped = sum(c for c in counts if isinstance(c, int) and not isinstance(c, bool))
+        if body.get("already_cancelled") is True:
+            return f"Already cancelled ({short_id})."
+        return f"Cancelled ({short_id}): {stopped} piece(s) of work stopped."
+    if status == 409:
+        refusal = body.get("refusal")
+        text = CANCEL_REFUSALS.get(refusal) if isinstance(refusal, str) else None
+        return text or "That work cannot be cancelled."
+    if status == 400:
+        return "I could not read that id."
+    if status == 503:
+        return _NOT_RUNNING
+    return _UNREACHABLE
+
+
 def sanitize_telegram(text: str) -> str:
     """Convert markdown patterns that don't render in Telegram.
 
@@ -769,6 +834,19 @@ class NousTelegramBot:
             body = {}
         return response.status_code, body if isinstance(body, dict) else {}
 
+    async def _owner_get(self, path: str) -> tuple[int, dict]:
+        """GET a REST owner route: ``(status, body)``; status 0 when the server could not be reached."""
+        try:
+            response = await self._http.get(f"{self.nous_url}{path}", timeout=30)
+        except Exception as exc:
+            logger.warning("owner request failed (%s)", type(exc).__name__)
+            return 0, {}
+        try:
+            body = response.json()
+        except Exception:
+            body = {}
+        return response.status_code, body if isinstance(body, dict) else {}
+
     async def _answer_callback(self, query_id: Any, text: str) -> None:
         if query_id is not None:
             await self._tg("answerCallbackQuery", params={"callback_query_id": query_id, "text": text})
@@ -818,7 +896,8 @@ class NousTelegramBot:
             logger.warning("could not send the follow-up of an owner action (%s)", type(exc).__name__)
 
     async def _handle_owner_text(self, message: dict[str, Any], chat_id: Any, user_id: Any, text: str) -> bool:
-        """``/approve``, ``/reject``, ``/answer`` and a reply to one of our messages, in the owner chat. True when
+        """``/approve``, ``/reject``, ``/answer``, ``/intentions``, ``/cancel_intention`` and a reply to one of our
+        messages, in the owner chat. True when
         the message was consumed; anything else (another chat, another command, a malformed or missing id, an id
         the server answers 404 for, a reply the answer route does not confirm with a 200 or a 409) is left for the
         ordinary path, unchanged: under
@@ -849,6 +928,26 @@ class NousTelegramBot:
                 return False  # no such question: ordinary chat, as before 2d
             await self._send(chat_id, describe_answer(status, body))
             return True
+        if command == "/intentions" and not rest.strip():
+            status, body = await self._owner_get(f"/intentions?state=open&limit={INTENTIONS_SHOWN}")
+            if status != 200 or body.get("continuation") is not True:
+                # Anything but a definite answer from a server that runs the continuation (an older server, an
+                # outage, continuation off) is not ours: the message goes on to chat as it always did.
+                return False
+            await self._send(chat_id, describe_intentions(body), parse_mode="HTML")
+            return True
+        if command == "/cancel_intention":
+            args = rest.split()
+            hex_id = _hex_id(args[0]) if len(args) == 1 else None
+            if hex_id is None:
+                return False  # no id, or not one: it was never ours (and no argument reaches a URL)
+            status, body = await self._owner_post(
+                f"/intentions/{hex_id}/cancel", {"actor": f"telegram:{user_id}", "reason": "cancelled from Telegram"}
+            )
+            if status == 404:
+                return False  # no such root: ordinary chat, as before 2e
+            await self._send(chat_id, describe_cancel(status, body, hex_id[:8]))
+            return True
         reply_to = message.get("reply_to_message")
         if (
             isinstance(reply_to, dict)
```

- [ ] **Step 4: Run** `"$BIN/nous-test-linux.sh" "$WT" runner sqlite - tests/test_f099_phase2e_bot.py tests/test_f099_phase2d_bot.py -q` (142 pass) and, on the database, `tests/test_f099_phase2e_parity.py tests/test_f099_phase2c_parity.py tests/test_f099_phase2d_parity.py -q` (all pass).

- [ ] **Step 5: Mutation checks**
  1. Remove the `"continuation"` condition from `/intentions`: the flag-off pin fails (two parametrisations).
  2. Drop `html_module.escape`: `test_model_text_is_escaped_inside_pre_and_never_outside_it` fails.
  3. Disable the `/cancel_intention` branch: the cancel tests fail.
  4. In the parity file: replace `ContinuationRunner.root_is_cancelled` by an `async def`, or move the view check below the strict rule: `test_the_cancel_view_is_a_set_lookup_and_the_default_reads_nothing` fails.

- [ ] **Step 6: Lint and commit** (`feat(F099): 2e-8 /intentions and /cancel_intention in the bot, the prod parity pins`).


## Task 2e-9: the flip (its own commit)

**Prod runs:** the same as before it. Prod's flags have continuation OFF, so `_build_continuation_runner` still returns `None` with the constant True (pinned: `test_prods_flags_build_no_runner_after_the_flip`, `test_prods_flags_install_no_view_and_build_no_runner`), no loop, sweep, push or view exists, and nothing runs until the owner sets `NOUS_CONTINUATION_ENABLED=true` and restarts. **This commit changes only:** the constant, comments that said "until 2e" (four files, no code), the tests that asserted "not ready", one new test file, and the docs. The repo's `docker-compose.yml` already passes `NOUS_CONTINUATION_ENABLED=${NOUS_CONTINUATION_ENABLED:-false}` (it has since 2b), so it is not touched; the deploy notes below are for prod's own compose.

**Files:**
- Modify: `nous/brain/continuation.py` (the constant and its comment), `nous/main.py` (the gate's and the builder's docstrings, one comment), `nous/handlers/continuation_runner.py` (two docstrings), `nous/config.py` (one comment)
- Modify (the pins that depend on the constant): `tests/test_f099_phase2b_settings.py`, `tests/test_f099_phase2c_parity.py`
- Create: `tests/test_f099_phase2e_flip.py`
- Modify (docs): `docs/reference/environment-variables.md`, `docs/reference/rest-api.md`, `docs/reference/shipped-features.md`, `docs/features/INDEX.md`, `docs/superpowers/plans/2026-10-06-f099-phase2-contract.md` (the "Superseded by 2e" block)

**Interfaces:** none new. `CONTINUATION_RUNNER_READY: bool = True`; `_gate_continuation_flag` and `_build_continuation_runner` keep their code: the gate is now a guard for a build that clears the constant.

**The six tests that change on purpose** (everything else in the F099 files and their neighbours passes unchanged): `test_the_runner_is_not_ready_in_this_build` becomes `…is_ready…`; `test_the_gate_forces_a_requested_flag_off` and `test_create_components_gates_the_flag_before_anything_reads_it` clear the constant with `monkeypatch` (the mechanism stays tested); `test_the_gate_lets_a_ready_runner_through` loses its `monkeypatch`; in 2c-2's parity file `test_the_runner_is_not_ready_in_this_pr` becomes `…is_ready…` and the two "no runner is built" tests clear the constant.

- [ ] **Step 1: Write the flip's tests and update the pins**

**Create `tests/test_f099_phase2e_flip.py`:**

```python
"""F099 Phase 2e-9: the flip. `CONTINUATION_RUNNER_READY` is True, the owner turns the flag on, and until then prod runs
exactly as before. The pins that said "not ready" are replaced here and in two earlier files."""

from __future__ import annotations

import logging
import re
from pathlib import Path

import pytest
from f099_support import env_factory, runner_env  # noqa: F401
from test_f099_phase2c_parity import PROD, Untouchable

import nous.main as main
from nous.brain import continuation
from nous.config import Settings
from nous.handlers.continuation_runner import ContinuationRunner

ROOT = Path(__file__).resolve().parents[1]
# The settings that make a flag-on process valid: intentions need the inbox.
BASE = {"result_inbox_enabled": True, "intentions_enabled": True}


def test_the_runner_is_ready_and_the_constant_is_set_in_one_place():  # PIN
    assert continuation.CONTINUATION_RUNNER_READY is True
    assignments = [
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / "nous").rglob("*.py")
        if re.search(r"^CONTINUATION_RUNNER_READY\b.*=", path.read_text(encoding="utf-8"), re.MULTILINE)
    ]
    assert assignments == ["nous/brain/continuation.py"]  # nothing else sets or clears it


def test_the_flag_is_off_by_default_and_compose_passes_it_with_a_false_default():  # PIN
    assert Settings(_env_file=None).continuation_enabled is False
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "- NOUS_CONTINUATION_ENABLED=${NOUS_CONTINUATION_ENABLED:-false}" in compose


def test_the_gate_lets_the_owners_flag_through_without_a_warning(caplog):
    settings = Settings(_env_file=None, continuation_enabled=True, **BASE)
    with caplog.at_level(logging.WARNING, logger="nous.main"):
        main._gate_continuation_flag(settings)
    assert settings.continuation_enabled is True
    assert "not shipped in this build" not in caplog.text


def test_a_build_that_clears_the_constant_is_still_gated(monkeypatch, caplog):
    """The mechanism stays: a build without the runner forces the flag off with the same WARNING."""
    monkeypatch.setattr(continuation, "CONTINUATION_RUNNER_READY", False)
    settings = Settings(_env_file=None, continuation_enabled=True, **BASE)
    with caplog.at_level(logging.WARNING, logger="nous.main"):
        main._gate_continuation_flag(settings)
    assert settings.continuation_enabled is False
    assert "continuation runner is not shipped in this build" in caplog.text


async def test_prods_flags_build_no_runner_after_the_flip():  # PIN: the whole point of "until the owner turns it on"
    """Inbox, intentions and result memory on, continuation off: with the constant True, still no runner."""
    settings = Settings(_env_file=None, **PROD)
    assert continuation.CONTINUATION_RUNNER_READY is True and settings.continuation_enabled is False
    main._gate_continuation_flag(settings)
    assert settings.continuation_enabled is False
    untouched = {name: Untouchable() for name in ("database", "runner", "heart", "brain", "bus", "dispatcher")}
    assert await main._build_continuation_runner(settings, **untouched) is None


@pytest.mark.postgres_only
async def test_with_the_flag_on_the_runner_is_built_wired_and_started(runner_env):  # noqa: F811
    """What the owner gets: the same build the 2c-2 rehearsal made with the constant monkeypatched, with none."""
    env = await runner_env()  # continuation on
    main._gate_continuation_flag(env.settings)
    assert env.settings.continuation_enabled is True
    built = await main._build_continuation_runner(
        env.settings,
        database=env.db,
        runner=env.runner,
        heart=env.heart,
        brain=env.brain,
        bus=None,
        dispatcher=env.dispatcher,
    )
    try:
        assert isinstance(built, ContinuationRunner) and built._task is None
        assert env.runner._root_cancelled == built.root_is_cancelled  # the cancel reaches every tool call
        await built.start()
        assert built._task is not None
    finally:
        await built.stop()


def test_the_docs_say_what_the_flip_did():
    env_doc = (ROOT / "docs/reference/environment-variables.md").read_text(encoding="utf-8")
    row = next(line for line in env_doc.splitlines() if line.startswith("| `NOUS_CONTINUATION_ENABLED`"))
    assert "Until PR-2e" not in row and "the owner turns it on" in row
    rest = (ROOT / "docs/reference/rest-api.md").read_text(encoding="utf-8")
    assert "| GET | `/intentions` |" in rest and "| POST | `/intentions/{root_id}/cancel` |" in rest
    contract = (ROOT / "docs/superpowers/plans/2026-10-06-f099-phase2-contract.md").read_text(encoding="utf-8")
    assert "Superseded by 2e" in contract
    shipped = (ROOT / "docs/reference/shipped-features.md").read_text(encoding="utf-8")
    assert "| F099 Phase 2e |" in shipped
```

**Apply to `tests/test_f099_phase2b_settings.py`:**

```diff
diff --git a/tests/test_f099_phase2b_settings.py b/tests/test_f099_phase2b_settings.py
index ecc57cea..bd2c0476 100644
--- a/tests/test_f099_phase2b_settings.py
+++ b/tests/test_f099_phase2b_settings.py
@@ -127,12 +127,13 @@ def test_compose_passes_every_phase2_setting_with_the_settings_default():
     assert str(int(s.intention_proposal_ttl_hours)) == PHASE2_ENV["NOUS_INTENTION_PROPOSAL_TTL_HOURS"]
 
 
-def test_the_runner_is_not_ready_in_this_build():
-    """2e flips this assertion together with the gate test below."""
-    assert continuation.CONTINUATION_RUNNER_READY is False
+def test_the_runner_is_ready_in_this_build():
+    """2e flipped this assertion together with the gate tests below."""
+    assert continuation.CONTINUATION_RUNNER_READY is True
 
 
-def test_the_gate_forces_a_requested_flag_off(caplog):
+def test_the_gate_forces_a_requested_flag_off_in_a_build_without_the_runner(caplog, monkeypatch):
+    monkeypatch.setattr(continuation, "CONTINUATION_RUNNER_READY", False)  # a build that has not got the runner
     settings = Settings(_env_file=None, continuation_enabled=True, **BASE)
     assert settings.continuation_enabled is True  # the validators alone do not gate it
     with caplog.at_level(logging.WARNING, logger="nous.main"):
@@ -149,8 +150,7 @@ def test_the_gate_is_silent_when_the_flag_is_off(caplog):
     assert "continuation runner" not in caplog.text
 
 
-def test_the_gate_lets_a_ready_runner_through(monkeypatch):
-    monkeypatch.setattr(continuation, "CONTINUATION_RUNNER_READY", True)
+def test_the_gate_lets_a_ready_runner_through():
     settings = Settings(_env_file=None, continuation_enabled=True, **BASE)
     main._gate_continuation_flag(settings)
     assert settings.continuation_enabled is True
@@ -159,7 +159,8 @@ def test_the_gate_lets_a_ready_runner_through(monkeypatch):
 async def test_create_components_gates_the_flag_before_anything_reads_it(monkeypatch):
     """create_components must gate before it builds a single component. The
     first thing it builds is the Database, so a stand-in that stops there sees
-    the flag already off."""
+    the flag already off (in a build without the runner: the constant is cleared here)."""
+    monkeypatch.setattr(continuation, "CONTINUATION_RUNNER_READY", False)
     settings = Settings(_env_file=None, continuation_enabled=True, **BASE)
     seen: dict[str, bool] = {}
 
```

**Apply to `tests/test_f099_phase2c_parity.py`:**

```diff
diff --git a/tests/test_f099_phase2c_parity.py b/tests/test_f099_phase2c_parity.py
index 49795a42..01de9408 100644
--- a/tests/test_f099_phase2c_parity.py
+++ b/tests/test_f099_phase2c_parity.py
@@ -30,11 +30,12 @@ class Untouchable:
 UNTOUCHED = {name: Untouchable() for name in ("database", "runner", "heart", "brain", "bus", "dispatcher")}
 
 
-def test_the_runner_is_not_ready_in_this_pr():  # PIN: 2e flips this assertion, and nothing else flips the constant
-    assert continuation.CONTINUATION_RUNNER_READY is False
+def test_the_runner_is_ready_in_this_build():  # PIN: 2e flipped this assertion, and nothing else flips the constant
+    assert continuation.CONTINUATION_RUNNER_READY is True
 
 
-async def test_a_requested_flag_is_forced_off_and_no_runner_is_built():  # PIN (from 2c-2 on)
+async def test_a_requested_flag_is_forced_off_and_no_runner_is_built_without_the_runner(monkeypatch):  # PIN
+    monkeypatch.setattr(continuation, "CONTINUATION_RUNNER_READY", False)  # a build that has not got the runner
     settings = Settings(_env_file=None, **CONT)
     assert settings.continuation_enabled is True  # as an operator set it
     main._gate_continuation_flag(settings)
@@ -42,7 +43,8 @@ async def test_a_requested_flag_is_forced_off_and_no_runner_is_built():  # PIN (
     assert await main._build_continuation_runner(settings, **UNTOUCHED) is None
 
 
-async def test_the_second_guard_holds_even_if_the_gate_were_bypassed():  # PIN (from 2c-2 on)
+async def test_the_second_guard_holds_even_if_the_gate_were_bypassed(monkeypatch):  # PIN (from 2c-2 on)
+    monkeypatch.setattr(continuation, "CONTINUATION_RUNNER_READY", False)  # a build that has not got the runner
     settings = Settings(_env_file=None, **CONT)  # the flag on, the gate never run
     assert await main._build_continuation_runner(settings, **UNTOUCHED) is None
 
```

- [ ] **Step 2: Run them: `test_the_runner_is_ready_and_the_constant_is_set_in_one_place` fails** (the constant is False).

- [ ] **Step 3: Flip the constant and the comments**

**Apply to `nous/brain/continuation.py`:**

```diff
diff --git a/nous/brain/continuation.py b/nous/brain/continuation.py
index fc4dc085..4c345efa 100644
--- a/nous/brain/continuation.py
+++ b/nous/brain/continuation.py
@@ -42,10 +42,10 @@ from nous.storage.models import (
 
 logger = logging.getLogger(__name__)
 
-# Flipped to True by PR-2e, in the commit that wires the runner into main.py.
-# While it is False, main.py forces NOUS_CONTINUATION_ENABLED off: with the flag
-# on and no runner, a continue result is written NULL-keyed and nothing claims it.
-CONTINUATION_RUNNER_READY: bool = False
+# True since PR-2e: the runner, its bounds, the proposals and the owner's cancel all ship. A build that
+# has not got them sets it False, and main.py then forces NOUS_CONTINUATION_ENABLED off: with the flag on
+# and no runner, a continue result is written NULL-keyed and nothing claims it.
+CONTINUATION_RUNNER_READY: bool = True
 
 INTENT_SESSION_PREFIX = "intent-"  # session id of a root's thread: f"intent-{root_id}"
 SOURCE_INTENTION_REPORT = "intention_report"  # inbox source kind of an owner-facing row
```

**Apply to `nous/main.py`:**

```diff
diff --git a/nous/main.py b/nous/main.py
index f357136a..ca717467 100644
--- a/nous/main.py
+++ b/nous/main.py
@@ -229,13 +229,13 @@ def _warn_on_f098_flags(settings: Settings) -> None:
 
 
 def _gate_continuation_flag(settings: Settings) -> None:
-    """F099 Phase 2: keep NOUS_CONTINUATION_ENABLED off until the runner ships.
+    """F099 Phase 2: keep NOUS_CONTINUATION_ENABLED off in a build that has no runner.
 
     With the flag on and no runner, a continue result is written keyed by its
     intention alone and nothing claims it (G6). The gate lives here and not in
-    a Settings validator because config.py must not import nous.brain. The
-    runner is already wired (``_build_continuation_runner``, inert while the
-    constant is False); PR-2e sets CONTINUATION_RUNNER_READY.
+    a Settings validator because config.py must not import nous.brain. PR-2e
+    set CONTINUATION_RUNNER_READY, so the gate lets the flag through; it stays
+    as the guard of any build that clears the constant.
     """
     if settings.continuation_enabled and not continuation.CONTINUATION_RUNNER_READY:
         logger.warning(
@@ -293,7 +293,7 @@ async def _build_continuation_runner(
     """F099 Phase 2c: build and wire the continuation runner (NOT started), or None.
 
     A runner exists only when continuation is on AND this build may run it
-    (``continuation.CONTINUATION_RUNNER_READY``, flipped by PR-2e). ``_gate_continuation_flag``
+    (``continuation.CONTINUATION_RUNNER_READY``, True since PR-2e). ``_gate_continuation_flag``
     already forces the flag off while the constant is False; this is the second guard, so a change to the
     gate alone cannot start a runner. With either off, nothing is constructed: no loop, no sweep, no
     push, no reconciler pass. The runner is returned unstarted: `create_components` starts it as its LAST
@@ -1138,8 +1138,8 @@ async def create_components(settings: Settings) -> dict:
                 _context_log_retention_loop(settings, database), name="context-log-retention"
             )
 
-    # F099 Phase 2c: the continuation runner. None while CONTINUATION_RUNNER_READY is False (until PR-2e), so
-    # the reconciler's pass below and the shutdown see none.
+    # F099 Phase 2c: the continuation runner. None unless NOUS_CONTINUATION_ENABLED is on (and
+    # CONTINUATION_RUNNER_READY, True since PR-2e), so the reconciler's pass below and the shutdown see none.
     continuation_runner = await _build_continuation_runner(
         settings, database=database, runner=runner, heart=heart, brain=brain, bus=bus, dispatcher=dispatcher
     )
```

**Apply to `nous/handlers/continuation_runner.py`:**

```diff
diff --git a/nous/handlers/continuation_runner.py b/nous/handlers/continuation_runner.py
index 589f18c0..12489e62 100644
--- a/nous/handlers/continuation_runner.py
+++ b/nous/handlers/continuation_runner.py
@@ -5,7 +5,7 @@ thread of its own (``intent-<root>``), that ends with one decision: ``resolve_in
 that decide whether, when and how the turn's outcome is committed are rows-level and live in
 ``nous.brain.continuation``; this module is the part that runs: the decision tool, the turn's input,
 the arrival (claim, gate, turn, follow-up, commit), and the loop. Nothing here starts unless
-``NOUS_CONTINUATION_ENABLED`` is on, which ``main.py`` forces off until PR-2e.
+``NOUS_CONTINUATION_ENABLED`` is on (default off: the owner turns it on).
 """
 
 from __future__ import annotations
@@ -413,7 +413,7 @@ class ContinuationRunner:
 
     async def start(self) -> None:
         """Release claims older than the lease, then run the loop (spec 4.5.2). Does nothing, and builds no
-        task, with continuation off: main.py forces it off until PR-2e.
+        task, with continuation off.
 
         Must not overlap ``stop()``: nothing here guards a start that is still releasing claims against a stop.
         main.py calls ``start()`` once, as the last step of ``create_components``."""
```

**Apply to `nous/config.py`:**

```diff
diff --git a/nous/config.py b/nous/config.py
index 8a4c571d..19dd4061 100644
--- a/nous/config.py
+++ b/nous/config.py
@@ -1212,8 +1212,8 @@ class Settings(BaseSettings):
     # stays off.
     intentions_enabled: bool = False
     # F099 Phase 2: continue-policy results return to Nous's own continuation
-    # turn instead of the chat (spec section 4.3). Needs intentions_enabled and,
-    # until PR-2e ships the runner, main.py forces it off (CONTINUATION_RUNNER_READY).
+    # turn instead of the chat (spec section 4.3). Needs intentions_enabled. Default off:
+    # the owner turns it on (PR-2e shipped the runner and the owner's cancel).
     continuation_enabled: bool = False
     # Bounds per root (section 4.6). Derived from rows, never counted in memory.
     continuation_max_depth: int = Field(default=3, ge=1)
```

- [ ] **Step 4: The docs** (the env-vars row loses "until PR-2e ships the runner" and says the owner turns it on, and what turning it off does; the two routes get their rows; the F099 status and the shipped-features row; the contract's supersession block)

**Apply to `docs/reference/environment-variables.md`:**

```diff
diff --git a/docs/reference/environment-variables.md b/docs/reference/environment-variables.md
index 9199a7b7..7e3c4663 100644
--- a/docs/reference/environment-variables.md
+++ b/docs/reference/environment-variables.md
@@ -152,7 +152,7 @@ DB connection vars are **unprefixed** (shared with docker-compose). All others u
 | `NOUS_RESULT_INBOX_BODY_MAX_CHARS` | `4000` | F098: per-result body cap; the full text stays on the subtask/DAG row. |
 | `NOUS_RESULT_INBOX_DAG_SCHEDULED` | `false` | F098: route DAGs with no origin (scheduler/heartbeat) to `telegram:<NOUS_TELEGRAM_CHAT_ID>`. Off: such DAGs keep only the F087 push. |
 | `NOUS_INTENTIONS_ENABLED` | `false` | F099 Phase 1. Every spawn records the **intention** behind it in `brain.intentions` (migration 083), written by the store that creates the work row in the same transaction: `spawn_task`, `spawn_sync`, `schedule_task` (a `container`), `dag_create`, scheduler fires (a new root under the schedule's container), both work-queue DAG sites, companion `app.act` and REST `POST /schedules` (a container). While on, the four spawn tools take a one-line `intent` (required in the schema) and an optional `wake_policy`. A missing intent is refused in a foreground turn (`interactive`, `mcp`) and generated as `<origin_kind>: <first line of the task>` in any other, so background prompts written before F099 (the F087 summary turn's email spawn) keep working. Off, their schemas are byte-identical. Subtasks carry the lineage in `metadata.intention`; DAG nodes and DAG check nodes get it at launch. Recording only: routing stays F098 Phase A's, every intention closes as `legacy` when its source finishes (the inbox writers, the inline path, and the `intentions` reconciler pass), and the flag narrows no tool set. Narrowing keys on a turn's lineage authority (F099 Phase 2a), never on a flag: Phase 1 writes no `internal_only` row, so only a turn whose lineage stamp is damaged (it fails closed to `internal_only`) loses tools, and it never gains one. Requires `NOUS_RESULT_INBOX_ENABLED` (otherwise a WARNING and it stays off). Needs its own compose line. |
-| `NOUS_CONTINUATION_ENABLED` | `false` | F099 Phase 2. A `continue` intention's result is written keyed by the intention alone (`channel` and `session_id` NULL) and read only by Nous's own continuation turn, never by a chat turn; `report`, `none` and `remember` intentions close as `delivered` instead of `legacy`; the subtask worker's raw Telegram push and the F087 Telegram leg stand down for a `continue` source; the startup rollback does not run. Needs `NOUS_INTENTIONS_ENABLED` (otherwise a WARNING and it stays off). **Until PR-2e ships the runner, `main.py` forces it off with a WARNING** (`nous.brain.continuation.CONTINUATION_RUNNER_READY`). Passed by `docker-compose.yml` (default `false`). |
+| `NOUS_CONTINUATION_ENABLED` | `false` | F099 Phase 2. A `continue` intention's result is written keyed by the intention alone (`channel` and `session_id` NULL) and read only by Nous's own continuation turn, never by a chat turn; `report`, `none` and `remember` intentions close as `delivered` instead of `legacy`; the subtask worker's raw Telegram push and the F087 Telegram leg stand down for a `continue` source; the startup rollback does not run; the continuation runner starts (one loop, a sweep at least every 60 s) and the owner can cancel a root (`POST /intentions/{root}/cancel`, Telegram `/cancel_intention`). **Off by default: the owner turns it on** (PR-2e shipped the runner, the bounds, the proposals and the cancel, so `main.py` no longer forces it off; `nous.brain.continuation.CONTINUATION_RUNNER_READY` stays as the guard of a build that clears it). Needs `NOUS_INTENTIONS_ENABLED` (otherwise a WARNING and it stays off). Turning it off again is safe: the startup rollback re-routes the results nothing claimed to chat, expires the open proposals and closes the open `continue` intentions as `legacy`. The Telegram service also needs `NOUS_TELEGRAM_CHAT_ID` for the owner's buttons and commands. Passed by `docker-compose.yml` (default `false`). |
 | `NOUS_CONTINUATION_MAX_DEPTH` | `3` | F099 Phase 2. Deepest lineage level a continuation may spawn (`>= 1`). |
 | `NOUS_CONTINUATION_MAX_SPAWNS_PER_ROOT` | `12` | F099 Phase 2. Most spawns under one root (`>= 1`). |
 | `NOUS_CONTINUATION_MAX_TURNS_PER_ROOT` | `8` | F099 Phase 2. Most continuation turns for one root (`>= 1`). |
```

**Apply to `docs/reference/rest-api.md`:**

```diff
diff --git a/docs/reference/rest-api.md b/docs/reference/rest-api.md
index 52edcd93..79cec9a5 100644
--- a/docs/reference/rest-api.md
+++ b/docs/reference/rest-api.md
@@ -38,6 +38,8 @@ Documented routes served by `nous/api/rest.py`, which is the full list. Part of
 | POST | `/intentions/proposals/{id}/decide` | F099 Phase 2d: the owner's decision, `{"decision": "approve" \| "reject", "actor"?}`. `{id}` is a UUID or a hex prefix of 8 to 32 characters. An approve runs the staged call once and answers with its state (`executed`, `failed`) and the call's raw `result` and `error`, which a surface that renders them must escape; a repeat is 200 with `changed: false`; a late (expired, work ended) or contradictory decision is 409 with a fixed message; 404 for an id that names nothing (all that a deployment with continuation off ever answers); 503 when a row exists and the runner is not running. Deterministic: no model takes part, and no agent tool can reach it. No in-app authentication (the existing LAN posture) |
 | POST | `/intentions/questions/{id}/answer` | F099 Phase 2d: the owner's answer to a question, `{"text", "actor"?}`, recorded as the next result of every intention of the asking arrival. 409 when already answered, expired or the work ended (nothing is written) |
 | POST | `/intentions/questions/answer` | F099 Phase 2d: the same, addressed by the Telegram message the question was pushed as: `{"chat_id", "message_id", "text", "actor"?}`. 404 when no question was sent as that message (the bot then treats the reply as ordinary chat) |
+| GET | `/intentions` | F099 Phase 2e: the roots the owner can act on, newest first (`state` `open`, the default, is a root with no cancel or expiry marker and an intention of its lineage still open, `all` adds the rest; `limit` 1 to 100, default 20). Each carries its columns, its budgets (`limits`, none for a schedule's container), up to 50 lineage rows, its newest 5 arrivals and its open proposals. With `NOUS_CONTINUATION_ENABLED` **on**, every open root is listed, the open Phase 1 roots (a schedule's container included) among them, and each can be cancelled; with it **off** the answer is `200 {"roots": [], "continuation": false}` and no row is read (Phase 1 roots exist but cannot be cancelled without the runner). The budgets are read per root: ask for `limit=10` from a card. No in-app authentication (the existing LAN posture) |
+| POST | `/intentions/{root_id}/cancel` | F099 Phase 2e: the owner's cancel of a root (spec 4.6), `{"reason"?, "actor"?}` (no body is allowed). `{root_id}` is a UUID or a hex prefix of 8 to 32 characters of a ROOT. One transaction marks the root and cancels its lineage, subtasks, proposals, schedules and the open fires of its containers; then the lineage's DAGs, and the running turn, are cancelled, and every later tool call of the lineage is refused; the lineage's unsent owner rows (a REPORT, QUESTION or PROPOSAL that was deferred or waits for a retry) are closed, so nothing is pushed or shown for it afterwards. A call the owner already approved and that is `executing` is not taken back: a send in flight completes, and its outcome is written to nobody. `200` with the counts (`already_cancelled`, `cancelled_intentions`, `cancelled_subtasks`, `cancelled_dags`, `cancelled_proposals`, `deactivated_schedules`, `turn_stopped`); a repeat is 200 with `already_cancelled: true`; 409 `{"refusal": "finished"}` when nothing was running; 404 for an id that is no root; 400 for a malformed or ambiguous id; 503 when there is no continuation runner (a root exists, and nothing is cancelled). Deterministic: no model takes part and no agent tool can reach it |
 | GET | `/admin/search-weights` | Get search weights |
 | POST | `/admin/search-weights` | Set search weights |
 | GET | `/rubric` | Current rubric |
```

**Apply to `docs/reference/shipped-features.md`:**

```diff
diff --git a/docs/reference/shipped-features.md b/docs/reference/shipped-features.md
index 786f5a1d..71ffc24b 100644
--- a/docs/reference/shipped-features.md
+++ b/docs/reference/shipped-features.md
@@ -72,3 +72,4 @@ The [Feature Index](../features/INDEX.md) is the complete list of features and t
 | F099 Phase 2c-1 | [Continuation store](../superpowers/specs/2026-10-05-f099-intentions-and-continuation-design.md) (`nous/brain/continuation.py`: the per-root claim (`FOR NO KEY UPDATE`, debounce, max-wait, batch with its deepest member as parent), the deterministic gate, every budget derived from rows, one fenced commit per arrival (arrival row, state moves, one Brain decision that cannot abort it, delivery stamped only there, owner-facing REPORT or QUESTION with a quiet-hours-aware push time), failed attempts and the lease (the cap applies at lease release too), the TTL sweep (a root with no deadline and nothing unread closes without a report), the single wake rule of an `ask`, `repair_missing_results` in the reconciler module, the root TTL and depth/spawn limits written at spawn through `intentions.with_bounds`, and lineage-check tokens counted in their DAG, with the flag on only. A report with no content closes `legacy` (R1). Nothing calls the store yet; the flag is still forced off) | #705 |
 | F099 Phase 2c-2 | [Continuation runner](../superpowers/specs/2026-10-05-f099-intentions-and-continuation-design.md) (`nous/handlers/continuation_runner.py`: one loop that sleeps until a root is due, claims it under a concurrency slot, runs the deterministic gate, runs a `continuation` turn in its own `intent-<root>` thread whose only way out is `resolve_intention` (the decision is read from the tool call, never the text), asks once more if the model forgot with no forced `tool_choice`, falls back to a report, and commits under the claim's fence; a `continue` or `revise` with nothing left running under the root is refused, so the model must spawn the next step first or end with report, drop or ask; a raise or a timeout counts an attempt, a released lease makes a late commit a no-op; `OwnerPublisher` (`nous/handlers/continuation_publisher.py`) pushes REPORT and QUESTION rows to Telegram once each, quiet hours deferring only the push: a row Telegram refuses (HTTP 400 or 403, or an empty chat id) is stamped and skipped, while an outage, or a 401, 404, 429 or 5xx, pauses the queue and is retried at the next sweep; the reconciler's `ContinuationWakePass` repairs and wakes and never runs a turn; `main.py` builds the runner only when the flag is on and `CONTINUATION_RUNNER_READY`, which stays False until 2e, so nothing runs in prod) | #706 |
 | F099 Phase 2d | [Proposals and owner actions](../superpowers/specs/2026-10-05-f099-intentions-and-continuation-design.md) (`propose_action`, a per-turn internal-only extra tool that stages a call (`brain.intention_proposals`, state `staged`, carrying the turn's claim token) and runs nothing; the arrival's fenced commit makes the staged rows `pending` and writes their PROPOSAL rows (an `ask` with proposals writes no QUESTION), and every other outcome, a failed or released attempt included, expires them. The owner decides through `ContinuationRunner.decide_proposal` / `answer_question`, called by four REST routes (`/intentions/...`), the Telegram bot (inline Approve/Reject buttons, `/approve`, `/reject`, `/answer`, reply-to; accepted only from the owner chat, which needs `NOUS_ALLOWED_USERS` when it is a group; an id the server does not know is passed on to chat unchanged, and so is a reply the route does not confirm with a 200 or a 409) and, in Phase 3, the A2UI cards: nothing a model can call. The refusal sentences are one map in `nous/owner_actions.py`, and the decide route's 200 body carries the call's raw `result` and `error`, which any surface that renders them (the Phase 3 cards) must escape. `execute_approved_proposal` runs exactly the stored `(tool, arguments)` once, behind `claim_execution` (`approved` to `executing` in one statement that also requires the root open: the cancel seam), through the execution ledger under the `proposal:{id}` scope and an `approved_action` context with owner authority; a timeout is failed in doubt and never re-run. The batch wakes when every proposal and question of the arrival is terminal; a proposal expires as a rejection at its deadline, which counts from its push (after any quiet hours). Telegram shows model-authored text (a proposal, a question, a report) escaped inside `<pre>`, and a proposal is never truncated (a call too long to read in one message is refused at staging). The `telegram` compose service now receives `NOUS_TELEGRAM_CHAT_ID` (empty default): where it is set, that chat is the bot's owner chat, and an owner command with a well-formed id or a reply to a bot message makes one REST lookup first, which under continuation off answers 404 and the message goes on to chat unchanged. Lands dark: `CONTINUATION_RUNNER_READY` is still False, so nothing runs in prod, and no existing behaviour changes there. The one new answer prod gives is `GET /intentions/proposals`: `200 {"proposals": []}` where Starlette answered 404 before) | #707 |
+| F099 Phase 2e | [Cancel and the flip](../superpowers/specs/2026-10-05-f099-intentions-and-continuation-design.md) (`continuation.cancel_root`: one transaction, the root locked first, then the lineage's containers and their schedules, writes the marker, cancels the open intentions (a live claim is cleared, so a deciding turn loses its fence), the lineage's subtasks, its `staged`, `pending` and `approved` proposals (so a claim that read the marker before the commit finds its row already moved: `_expire_root` got the same fix), stamps its unread results and closes its unsent owner rows (a deferred push never goes out for cancelled work) without reporting them, and cancels the open fires of every container of the lineage; `ContinuationRunner.cancel_root` then takes the roots into an in-process view, cancels the lineage's DAGs through the orchestrator and the running turn tasks, and a sweep step keeps the view and the DAGs right. `AgentRunner._authorize_tool_call` refuses first, whatever the authority or the modes, every call whose context names a cancelled root. The unified late-result rule: an expired root reports a late result raw (the gate included), a cancelled root stamps it silently, in the gate, `record_result` and the stranded-row settle; a lineage whose last waiting child was cancelled closes and reports once. Migration 085: the tokens of a failed attempt are kept on the claim's deepest intention and count against the root's token budget. Residuals: the rollback marks a result it cannot deliver as delivered (the inbox metrics count that stamp, and a cancel's, in their own buckets, never as delivered) and ends approved proposals, a question's window starts at its push, the sweep resumes an approved proposal nobody started, the owner push runs in its own task. `GET /intentions` and `POST /intentions/{root}/cancel`; Telegram `/intentions` and `/cancel_intention` (owner chat only; model-authored text escaped inside `<pre>`; passed on to chat unless the server gives a definite answer). `CONTINUATION_RUNNER_READY` is True: the owner turns `NOUS_CONTINUATION_ENABLED` on, and until then prod runs exactly as before) | this PR |
```

**Apply to `docs/features/INDEX.md`:**

```diff
diff --git a/docs/features/INDEX.md b/docs/features/INDEX.md
index befefdac..668a027c 100644
--- a/docs/features/INDEX.md
+++ b/docs/features/INDEX.md
@@ -43,7 +43,7 @@
 | F093 | [Micro-App Design System](F093-microapp-design-system.md) | 📋 Proposed — spec merged, nothing built | Gives the micro-app grammar a vocabulary for visual *intent*, which it has none of today: 24 display primitives, one 17-token `:root`, no escape hatch, and a 40/5/5 structural cap that makes a hand-built page like the Italy Departure Console literally unrepresentable. Four changes: (1) named themes chosen by id from a curated enum, hand-designed and contrast-checked — the model never emits a colour; (2) semantic tokens (`--ok/--warn/--crit/--locked/--soft`, `--font-display`) replacing colour-named ones across 42 catalog call sites; (3) computed components (`GateTimer`, `Countdown`) generalising the four renderer-owned computations that already exist; (4) `Section.layout` + `Repeat` + raised caps, where the renderer half is already built but unreachable (`Children.svelte:24-52`). Evidence table is generated by `f093_evidence.py`, not hand-maintained. Reviewed adversarially over 4 rounds; no architectural defect found. |
 | F096 | [Report Vocabulary](F096-report-vocabulary.md) | 🟢 Shipped (#630) | Five closed-enum `nous-core` components a periodic trend report is made of — `MetricCard`, `ScoreCard`, `DeltaList`, `DataTable`, `ChipRow` — plus `Section.caption` / `layout: cards`, `AppHeader.note`, Sparkline end dot + per-run `trendline` + source-declared focus window, a `report` theme and a `report` archetype (80/10 caps). Generalized from a hand-authored health trend report; the design canvas mocks it on Nous-ops data to prove nothing is domain-specific. Backend: series-aware `_bound` (embedded sparks shrink before a record is dropped), one shared path resolver for charts + list components walking the grammar's child keys, array/column rules, per-source prompt sample cap. Spec reviewed by a 3-lens adversarial workflow (29 findings folded). |
 | F098 | [Result Inbox & Wake Turn](F098-result-inbox-and-wake.md) | 🌑 Phases A and C merged dark (#694, #696); Phase B superseded by F099 | Subtask and DAG results routed to the conversation by a stable **channel** (`telegram:<chat_id>`) instead of an ephemeral session id, through one `heart.result_inbox` table read at pre-turn (claimed atomically, exactly once). Phase B (superseded by F099): optional wake turn so a finished result reports itself; Phase C ([spec](F098-phaseC-result-memory.md), merged dark, #696): results become memory — an episode + document chunks per conversation-originated result, idempotent through `heart.result_memory_log`. All flags default OFF. |
-| F099 | [Intentions and Continuation](../superpowers/specs/2026-10-05-f099-intentions-and-continuation-design.md) | 🚧 Phases 0a (#698), 0b (#700) and 1 (#702) merged and deployed (intentions on); Phases 2a (#704) and 2b (#703) merged dark; Phases 2c-1 (store), 2c-2 (runner) and 2d (proposals and owner actions) merged dark; NOUS_CONTINUATION_ENABLED forced off until 2e | Every spawn records the **intention** behind it (`brain.intentions`, same transaction). When the work finishes, the result returns to that intention, and its wake policy (`continue` / `remember` / `report` / `none`) decides what happens. A `continue` intention runs a **continuation turn** in its own thread that decides continue, revise, drop, report or ask. Autonomy is **internal only**: outward actions become proposals the owner approves, enforced by narrowing the offered tool set for the whole lineage. Chains are bounded at the root and can be cancelled. Builds on F098 Phase A (inbox) and Phase C (memory); supersedes F098 Phase B. Flags `NOUS_INTENTIONS_ENABLED`, `NOUS_CONTINUATION_ENABLED`, default OFF. |
+| F099 | [Intentions and Continuation](../superpowers/specs/2026-10-05-f099-intentions-and-continuation-design.md) | 🚧 Phases 0a (#698), 0b (#700) and 1 (#702) merged and deployed (intentions on); Phases 2a (#704) and 2b (#703) merged dark; Phases 2c-1 (store), 2c-2 (runner) and 2d (proposals and owner actions) merged dark; Phase 2e (the owner's cancel, the pre-flip residuals and the flip of `CONTINUATION_RUNNER_READY`) shipped: the owner turns `NOUS_CONTINUATION_ENABLED` on | Every spawn records the **intention** behind it (`brain.intentions`, same transaction). When the work finishes, the result returns to that intention, and its wake policy (`continue` / `remember` / `report` / `none`) decides what happens. A `continue` intention runs a **continuation turn** in its own thread that decides continue, revise, drop, report or ask. Autonomy is **internal only**: outward actions become proposals the owner approves, enforced by narrowing the offered tool set for the whole lineage. Chains are bounded at the root and can be cancelled. Builds on F098 Phase A (inbox) and Phase C (memory); supersedes F098 Phase B. Flags `NOUS_INTENTIONS_ENABLED`, `NOUS_CONTINUATION_ENABLED`, default OFF. |
 | F081 | [Side-Effect Verification](F081-side-effect-verification.md) | 📋 Proposed | Adds runtime assertion checks confirming that declared side effects (email, Telegram, dag_create, file write) actually occurred. Closes the structural gap left by F061 which validates result shape but not side-effect execution. Three phases: declarative assertions in success_criteria → probe-based verification → retroactive heuristic alerting. Suppresses recurring heartbeat false-positive from fact 972360c1. |
 | F050 | [Multi-Query Expansion](F050-multi-query-expansion.md) | 🌑 Phase 1 (dark) | Haiku-driven query expansion behind `NOUS_QUERY_EXPANSION_ENABLED=false`. Module + cache + wiring + 64 tests landed; Phase 3 flag-flip gated on F051 harness MRR +7% |
 | F064 | [Symphony Orchestration Adoptions](F064-symphony-orchestration-adoptions.md) | 🟡 v1 partial | Six DAG/skill orchestrator primitives from openai/symphony. **Shipped:** F064.1 stall detection (`last_activity_at` + 3-site activity ping incl. heartbeat-during-tool-dispatch), F064.2 per-frame-type DAG dispatch caps (subtask-only enforcement, scoped to current DAG), F064.3 workspace safety (sanitize-at-insert + unconditional read-time containment + hash-suffix collision-protection), F064.6 work-queue ingress (file_jsonl adapter + atomic claim + 5-min reconciler). **🟡 v1 partial:** F064.4 manifest persistence only — orchestrator consumer enforcement deferred to F064.4-v2; F064.5 Episode reuse only — LLM thread continuity deferred to F064.5-v2 (`runner.end_conversation` pops the in-memory Conversation between fires). All sub-features gated by per-flag env vars defaulting to off. PR #425. |
```

**Apply to `docs/superpowers/plans/2026-10-06-f099-phase2-contract.md`:**

```diff
diff --git a/docs/superpowers/plans/2026-10-06-f099-phase2-contract.md b/docs/superpowers/plans/2026-10-06-f099-phase2-contract.md
index 3d117004..242920ba 100644
--- a/docs/superpowers/plans/2026-10-06-f099-phase2-contract.md
+++ b/docs/superpowers/plans/2026-10-06-f099-phase2-contract.md
@@ -962,6 +962,15 @@ Nothing in the commit spawns work: spawns happened during the turn through the n
 > - §4.13: `intention.proposal_decided` carries `actor` (the owner's actor, or `system` for the sweep).
 > - §4.14 item 5: an `ask` that staged proposals writes no QUESTION; each PROPOSAL row's `source_id` is its proposal's id and `arrival.report_ids` lists them.
 
+> **Superseded by 2e (as built, `docs/superpowers/plans/2026-10-07-f099-phase2e-cancel-and-flip.md`):**
+> - §4.5 and §6 risk 1: there was no `_root_cancelled` seam; 2e adds it. `AgentRunner.set_cancelled_roots(view)` and the check, which is the FIRST statement of `_authorize_tool_call` (before the strict block), for every context that names a root; the view is `ContinuationRunner.root_is_cancelled`, a set lookup. The refusal code `root_cancelled` joined `ledger_store.REFUSAL_CODES` (the ledger rejects an unknown code).
+> - §4.7: `CancelOutcome` has defaults for every field and three more, `dag_ids`, `proposal_ids` and `root_ids`; the store fills what it can and `ContinuationRunner.cancel_root` fills `cancelled_dags` and `turn_stopped`. `cancel_root` marks the root and cancels its lineage, subtasks, `staged`/`pending`/`approved` proposals (in its own transaction under the root lock: `claim_execution`'s root-open predicate sees only a committed marker) and the open fires of every container of the lineage, a container being a row of the lineage as well as a root; `_expire_root` moves the same proposals (the shown ones with `decided_by = system`, and the runner tells the bus), and the cancel closes the lineage's unsent owner rows. `cancelled_root_ids(since=None)` reads every cancelled root; `find_root_id` finds a ROOT by prefix; `stray_dag_ids`, `list_roots`, `close_cancelled_source`, `end_hanging_root`, `question_window_start` and `stalled_approved_ids` are new; `fail_attempt(..., tokens=(0, 0))`; `RollbackReport.undeliverable`; `root_limits` adds `Intention.failed_tokens` (migration 085).
+> - §4.8: `ContinuationRunner.set_cancel_dag` binds the orchestrator's cancel in `main.py` (the runner is built before the orchestrator); `load_cancelled_roots` runs when the runner is built and again at `start`; `run_once` gains a first step (the cancel sweep: refresh the view, cancel stray DAGs) and an approved-resume step, and the owner push runs in its own task (the sweep waits `PUSH_WAIT_SECONDS`, never starts two).
+> - §4.10: `GET /intentions` returns the contract's RootView, compact (lineage capped at 50, the newest 5 arrivals, the open proposals; no `open_questions`), and answers `{"roots": [], "continuation": false}` without reading a row when continuation is off; `GET /intentions/{root_id}` is not built; the cancel answers 409 `{"refusal": "finished"}` for a root with nothing running, 503 for a root with no runner (it never cancels through the store alone).
+> - §4.11: `/intentions` (consumed only on a 200 with `continuation: true`, else chat) and `/cancel_intention <root_id>` (a 404 or a malformed id goes on to chat); model-authored intents are shown escaped inside `<pre>`.
+> - §4.1 T14 and the gate: the unified late-result rule. `GATE_DROP_REASONS` is `("cancelled", "plan_resolved")`: an `expired` claim reports the raw rows. `record_result` writes the settled twin for any result nothing can reopen and the REPORT unless the root is cancelled (marker) or the intention is `cancelled`; the stranded-row settle stamps a cancelled intention's rows without a report. A lineage left with nothing open after a cancelled child closes `legacy` (the newest arrival decided `continue` or `revise`) gets `root_expired_at` and one REPORT.
+> - §2: `CONTINUATION_RUNNER_READY = True`, in its own commit; `_gate_continuation_flag` stays as the guard of a build that clears it.
+
 ## 5. Open questions (with the recommended answer)
 
 1. **What does `POST /intentions/{id}/answer` address?** The spec (§4.4 Questions) writes `{id}` as the intention. But the claim SQL blocks only on a `deciding` row, so one root can hold two arrivals in `awaiting_owner` (two result_ready intentions claimed at different times), each with its own question. **Recommended:** the route addresses the question row (`POST /intentions/questions/{id}/answer`, §4.10), and `/answer <id>` takes the question id; the Telegram reply path resolves by `push_message_id`. The intention-addressed form is dropped.
```

Replace "this PR" in the new shipped-features row by the PR number when it is known.

- [ ] **Step 5: Run the whole F099 suite and its neighbours** (the plan's validation ran `tests/test_f099_*.py tests/test_runner_ledger.py tests/test_runner_authorization.py tests/test_idempotency.py tests/test_database.py tests/test_tool_classes.py tests/test_telegram_bot.py tests/test_config.py tests/test_result_inbox*.py tests/test_f098*.py`: 1,980 passed), then the full gate.

- [ ] **Step 6: Check the commit is only the flip.** `git show --stat HEAD` lists exactly the twelve files above. Mutation: set the constant back to `False` and `test_the_runner_is_ready_and_the_constant_is_set_in_one_place` and `test_with_the_flag_on_the_runner_is_built_wired_and_started` fail.

- [ ] **Step 7: Commit** (`feat(F099): 2e-9 flip CONTINUATION_RUNNER_READY: the owner may turn continuation on`), in its own commit, after every other task is merged into the branch.

---

## After the flip: what changes in prod when the owner turns the flag on

Written for the deploy. Nothing below happens until `NOUS_CONTINUATION_ENABLED=true` reaches the nous container. **Recommended: two restarts**, because with the flag on the startup rollback returns at once and `repair_missing_results` routes pending `continue` intentions whose source finished inside `result_inbox_max_age_hours` (72 h), so Phase 1's leftovers should be cleaned by the flag-off start first.

1. **Deploy the image with the flag OFF (and `NOUS_TELEGRAM_CHAT_ID` added to the telegram service).** The chat id turns on the bot's owner chat (Configuration B) on this restart, before the flag: 2d's owner commands, `/intentions` and `/cancel_intention` each make one REST lookup and, with continuation off, go on to chat, except `/cancel_intention` with the id of a real root, which answers the 503 line (see the deploy notes). Startup: migrations run (085: a metadata-only column and an empty partial index); `_gate_continuation_flag` is a no-op; `_rollback_continuation` runs as at every start: it finds no open `continue` row in `result_ready`, `deciding` or `awaiting_owner` (none was ever written), and its `close_finished_sources` sweep closes the pending intentions whose source already finished as `legacy` (Phase 1 leftovers; a legacy close is never reopened, so the flip cannot re-deliver them). No runner, no view, no new loop. The bot answers `/intentions` as chat (the server says `continuation: false`).
2. **Set the flag and restart.** Startup sequence: the gate lets the flag through; `_rollback_continuation` returns at once (`enabled`); the runner is built (`ContinuationRunner` with an `OwnerPublisher`), subscribed to `intention.result_ready`, the AgentRunner gets the cancelled-roots view and `load_cancelled_roots` fills it (no cancelled root exists yet: an empty set); the reconciler registers `ContinuationWakePass`; the orchestrator's cancel is bound once the DAG block exists; the runner is started last (`start`: `release_stale_claims`, none; the view loaded again; the loop).
3. **The first sweep** (the loop runs one immediately, then at least every 60 s): the cancel sweep (the view is current, no stray DAG); the lease release finds no claim; the TTL sweep `expire_roots` takes **at most `EXPIRE_BATCH = 10` roots per sweep**, oldest first, those with an open `continue` or `report` intention and a deadline past (a Phase 1 root has none, so `created_at + 72 h`). **A root older than the TTL at flip time is the only kind a sweep expires**: if intentions have been on for less than the TTL (check the oldest pending root's `created_at`), the first sweeps expire nothing; once roots pass it, only those whose work is still not terminal remain (the finished ones were closed by the flag-off restart): stuck or orphaned subtasks. Each closes **silently** (no deadline and nothing unread means no report), one with unread rows reports them once; a backlog of N such roots takes N/10 sweeps. `_expire_root` does not cancel the subtask, so nothing live is killed. Then the hanging-root sweep (none: it needs a root whose newest arrival is `continue` or `revise`, and no arrival exists before the flip), the proposal expiry (none), the question wake (none), the approved resume (none), the owner push, and the launch (no root is `result_ready` yet).
   **Check before the flip, after the flag-off restart:** `SELECT count(*) FROM brain.intentions WHERE wake_policy IN ('continue','report') AND state = 'pending' AND created_at < now() - interval '72 hours'` is what the first sweeps will expire, and `SELECT count(*) FROM brain.intentions i JOIN heart.subtasks s ON s.id::text = i.source_id WHERE i.source_kind = 'subtask' AND i.state = 'pending' AND s.status IN ('completed','failed','cancelled')` (pending intentions whose subtask is terminal) should be 0 once the flag-off start's `close_finished_sources` sweep has run.
4. **The publisher backlog is empty.** Flag-off writers never set `push_after`, and `push_due` reads only `intention_report` rows with `push_after` set and `pushed_at` null, so the first push has nothing to send.
5. **What the owner sees.** The WARNING "continuation stays OFF" is gone from the log. A `continue` result no longer goes to chat raw: the next result of a spawn from chat becomes a continuation arrival, and the owner hears only what Nous decides to say (a report, a question, a proposal with Approve and Reject buttons), pushed to Telegram once each (quiet hours defer the push, not the row). `/intentions` lists the open roots (schedule containers included, newest first, the intent shown escaped); `/cancel_intention <id>` or `POST /intentions/{root}/cancel` stops one. Budgets apply from the first arrival (8 turns, 400,000 tokens, depth 3, 12 spawns per root).
   **The two changes the owner meets first, both from work that began before the flip:** (a) a Phase 1 root older than the TTL whose subtask is still running when a sweep runs is expired silently; when that subtask finishes, `record_result` reports its late result raw as a REPORT **and pushes it to Telegram once** (today that result goes to chat through F098 routing): one message per such result, never a flood; (b) a Phase 1 root younger than the TTL with running work finishes **into a continuation turn**, under an `owner`-authority root with no deadline (its children get `created_at + TTL` as theirs): these are the first continuation turns the owner sees, on work spawned before the flip.
   **An approval orphaned by a restart is resumed slowly, by design.** The sweep resumes an `approved` proposal that no process started only once it was approved `max(NOUS_TOOL_TIMEOUT + 5 s, 60 s)` ago (so an inline run is never stolen): the grace scales with the tool timeout, about 33 minutes at a tool timeout of 2000 s. A call left `executing` by a process that stopped is failed in doubt, never re-run, after `max(NOUS_CONTINUATION_LEASE_SECONDS, 2 × NOUS_TOOL_TIMEOUT)`, about 67 minutes at 2000 s. Check the deployed `NOUS_TOOL_TIMEOUT` to know the figure.
   **Cancelling a scheduled root turns its schedule off for good.** `/intentions` shows a schedule's container as "scheduled"; its cancel deactivates the schedule (it never fires again) and the bot's reply says so.
   **`/intentions` and the flag.** With the flag ON the list shows every open root, the open Phase 1 roots (a schedule's container included) among them, and each can be cancelled; with it OFF the answer is empty (and the bot passes the message on to chat). The lead's ruling "Phase 1 roots are never listed" is the flag-off case only.
6. **Turning it off again** (restart with the flag off): the startup rollback re-routes the results nothing claimed to chat, expires the open proposals and closes the open `continue` intentions as `legacy`; a result it cannot deliver is marked delivered (carry-over 9). A cancel is permanent: a cancelled root stays cancelled.

**Deploy notes (for the lead, not code):** add `NOUS_TELEGRAM_CHAT_ID` to the prod telegram service's compose environment by hand (without it the bot has no owner chat, so the buttons and commands do nothing); add `NOUS_CONTINUATION_ENABLED=${NOUS_CONTINUATION_ENABLED:-false}` to the prod compose by hand and turn it on only when the owner says so. The prod nous process already has a default chat, which the owner rows and the rollback use. **Adding the chat id turns on Configuration B immediately (2e-8 review I2):** the telegram service has its owner chat from the 2e deploy's restart on, with continuation still off, so 2d's owner commands (`/approve`, `/reject`, `/answer`, a reply to a bot message), `/intentions` and `/cancel_intention` are live for the owner chat from that moment. Each makes one REST lookup first; `/cancel_intention <the id of a Phase 1 root>` answers "Nous is not running its follow-up work." (the route's 503) while the flag is off: a new visible answer before the flip, and harmless; every other id, and `/intentions`, goes on to chat as before. A compose from 2d on already passes the chat id to the telegram service, so deploying it has the same effect as adding the line by hand.

---

## Self-review

**Spec coverage.** §4.6 Cancel: the marker, the open intentions, the lineage's pending subtasks (and running: the cascade cancels both) and DAGs (2e-1, 2e-5), pending and approved proposals (2e-1), the container's schedule and the open fires (2e-1), the running turn (2e-5), `_authorize_tool_call` for every context that carries a cancelled root (2e-3), "child inserts check that the root is open" (existing I1, pinned against the cancel in 2e-1), `GET /intentions` (2e-7). §4.5.3 "the gate drops the late result of a cancelled root": 2e-2 (and the expired half by the lead's ruling). Contract §1.5, §2 (the flip and its pins), §4.1 T13, §4.7 `[2e]`, §4.8 `set_cancelled_roots` and `cancel_dag`, "Superseded by 2d". Carry-over: item 1 (2e-1), 2 (2e-4), 3 (2e-3), 4 (2e-1, 2e-5), 5 (2e-2), 6 (2e-5), 7 (2e-2: no arm, pinned), 8 (2e-2), 9 (2e-6), 10 (2e-6), 11 (2e-6), 12 (2e-6); R12 (no auth, 2e-7), R13 (the 404/503/409/400 pattern and the bot's fall-through, 2e-7 and 2e-8), R14 (the parity file and the flip task); the deploy notes (above).

**Placeholders.** None: every code step carries the code, and every command is exact. One value is filled at merge time: the PR number in the shipped-features row.

**Types and names.** `CancelOutcome` is defined in 2e-1 and consumed in 2e-5 (`dataclasses.replace`) and 2e-7 (the route reads its fields); `root_is_cancelled` (2e-5) is what `main.py` installs with `set_cancelled_roots` (2e-3); `stray_dag_ids` (2e-5) is called by the cancel sweep; `question_window_start` and `stalled_approved_ids` (2e-6) are used by the wake, `record_answer` and the resume; `ROLLBACK_UNDELIVERABLE_ID` and `RollbackReport.undeliverable` (2e-6) by `main.py`'s log; `list_roots` and `ROOT_STATES` (2e-7) by the route; `describe_intentions` and `describe_cancel` (2e-8) by `_handle_owner_text`. The CANCEL vocabulary key `finished` equals `continuation.REFUSE_FINISHED` (pinned).

**Review Focus coverage.** Every line of Review Focus names its tests, and each task lists the mutation that makes them fail. The plan review's M1, M2 and S6 tests fail without their fixes (mutations 2e-1 #4 and 2e-6 #5 and #6, run on the scratch copy).

## Lead rulings on the open questions (applied)

The lead's rulings (recorded in the carry-over, "Lead rulings on the 2e plan") are applied as the plan proposed them: `/cancel_intention` stays, with the C18 rules; no `GET /intentions/{id}` and no `open_questions` in 2e; `GET /intentions` is empty with continuation off (and, with it on, lists the open Phase 1 roots, which can be cancelled); a closed hanging root counts as expired, so a late result is reported raw and never reopens it; the approved-resume window equals the proposal TTL; a cancel of a finished root is a 409 no-op and the marker is written only on an open root. Deploy: two restarts. Nothing is open.
