# F099 Phase 2e: carry-over from 2a–2d, with lead rulings (binding for the 2e plan)

Base: `main` at `129d0776` (2d merged as #707). The store (`nous/brain/continuation.py`), the runner (`nous/handlers/continuation_runner.py`), the publisher, the owner routes (`nous/api/intention_routes.py`) and the bot's owner actions are on main and land dark. 2e is the PR after which `NOUS_CONTINUATION_ENABLED` may be turned on (contract §2): it brings cancel, closes the residuals that must land before the flip, and flips `CONTINUATION_RUNNER_READY`. Read the code, not the older plans, for every signature.

## Contract scope (contract §1.5, PR-2e)

- **`cancel_root` cascade.** It covers intentions, subtasks, DAGs, proposals, containers and their fires, and the running turn.
- **The cancelled-roots view**, checked by `_authorize_tool_call`. A call on behalf of a cancelled root is refused. The runner already has a `_root_cancelled` seam, `lambda _id: False`, which waits for `set_cancelled_roots`.
- **Lock order: container, then schedule.**
- **Routes and commands.** REST gets `POST /intentions/{root}/cancel` and `GET /intentions` (the open roots). Telegram gets `/intentions`. Add them to the surface-neutral layer the 2d routes use, so the Phase 3 "Active intentions" card can call the same function.
- **The flip.** Set `CONTINUATION_RUNNER_READY = True`. Flip the 2b pin that asserts it is False, and the gate that forces the flag off, in the same commit. Update the compose and reference docs.

## MUST land before the flip (each needs a test)

1. **Cancel moves proposals under the root lock** (2d-3 review m1, a hard requirement). `claim_execution`'s root-open `EXISTS` only sees a marker that has COMMITTED. `cancel_root` must therefore UPDATE `pending` and `approved` proposals to `cancelled` in its own transaction, under the root lock. Setting `root_cancelled_at` alone is not enough. A concurrent `claim_execution` then blocks on the proposal row and fails its `state = 'approved'` re-check. Test shape: a holder sets the cancel without committing, a claim runs in another session, the holder commits, and the claim returns None.
   - `_expire_root` has the same window, and gets the same fix and test.
2. **Failed attempts' tokens count against the root's token budget** (2c-2 final review M3). Today `fail_attempt`'s retry path writes no arrival row, and the cap path books `(0, 0)`. `_turn` holds `usage` when it calls `_fail` and drops it. Rule on a row design. The lead leans towards an arrival row that `root_limits`' turn count excludes but whose tokens count. A tokens column is the alternative. Test: a lineage whose turns keep failing reaches its token budget.
3. **A continuation turn starts with no leftover `intent-<root>` session** (2c-2 final re-review). When `end_conversation` times out, raises or is cancelled from outside, the next arrival runs on top of the old messages. Its `executed_tools` then sees the previous arrival's `learn_fact`, so a false `progress` gets verified. Fix: a no-DB `discard_conversation(session_id)` at the top of `_turn`. Test: a leftover session's messages and ledger do not reach the next turn.
4. **Cancel covers the DAGs a lineage started.** A lineage cannot stop its own DAG: `cancel_task` refuses DAG ids, and `dag_manage` is denylisted. `cancel_root` must cancel the root's lineage DAGs and their running nodes.
5. **The unified late-result rule, by marker.** An expired root reports the late result raw, everywhere. A cancelled root stamps it silently, in each of these places:
   - the gate;
   - `record_result`'s closed-root branch;
   - the stranded-row settle in `expire_roots`.

   Unread intention-keyed rows of a cancelled root must not be reported by the next sweep.
6. **The running turn of a cancelled root is cancelled.** `_running` is the cancel map. A `cancel_root` that cancels the task reaches `run_arrival`'s `cancel_requested()` branch. That branch releases the claim, fenced, which finds 0 rows after the cancel has moved them. The done callback then frees the slot. Pin this path, and pin that the cancelled turn's commit loses its fence.

## Also in 2e (each needs a test)

7. **`repair_missing_results` and cancelled intentions.** The repair's scope is `pending` and `expired`. Add `cancelled` if cancel can leave cancelled intentions with terminal sources that have no row and need settling.
8. **A legacy close of a lineage's only open child, under an open root.** It leaves the root with `_ttl_applies` false and no marker, so the root never expires or reports. Give such a root its TTL, or close it.
9. **Rollback with no owner channel** (2b R3). With the inbox on and no owner channel, or with the inbox off and no Telegram, the startup rollback closes the intention and leaves rows NULL-keyed and undelivered forever. Decide and pin the behaviour.
10. **Questions get the same quiet-hours window as proposals** (2d final re-review residual). A question's window starts at `max(now, push_after)`, as 2d's m2 does for proposals.
11. **An `approved` proposal on an open root after a crash** (2d final review m1). If the crash falls between the decision and the claim, nothing sweeps the row. Decide: either the sweep resumes such a row once (`claim_execution` is the fence, so running it is still at-most-once), or the row is left for the owner's re-tap and the root's TTL. If the latter, document it.
12. **`run_once` is not time-bounded through `push_due`** (about 200 s worst case). Run the push in its own task, or cap it per sweep, so a slow Telegram cannot delay continuation.

## Rulings carried in

- **R12: no auth on the new routes.** As C1 in 2d: every owner route has none, the network is the gate, and a shared token across all owner routes is a separate change the owner may request.
- **R13: the cancel route and `/intentions` follow 2d's surface-neutral pattern.** Look up first: 404 when there is no row, 503 when there is no runner, 409 on a refusal, 400 on malformed input. In the bot, a command whose id 404s falls through to chat (C18), and a reply falls through on any failure that is not a definite answer.
- **R14: prod parity until the flip, then the flip itself.** Every 2e commit before the flip commit must leave prod unchanged. The flip commit changes only `CONTINUATION_RUNNER_READY`, the gate and pins that depend on it, and the docs. The flag stays OFF in prod until the owner turns it on after deploy.

## Deploy notes (not code; for the 2e deploy, done by the lead)

- Add `NOUS_TELEGRAM_CHAT_ID` to the prod telegram service's compose environment, by hand. Without it the bot has no owner chat, so buttons and commands do nothing.
- Add `NOUS_CONTINUATION_ENABLED=${NOUS_CONTINUATION_ENABLED:-false}` to the prod compose, by hand. Turn it on only when the owner says so.

## Owner instructions in force

- Implementers run on Opus 5.5, plan writers on Sonnet 5.5, reviewers and architects on Fable 5.1.
- Merge gate: green CI plus a clean Fable final review.
- The repo is public: no private hosts, IPs, personal names or machine-local paths in committed files.

## Lead rulings on the 2e plan's open questions and conflicts (2026-10-07)

- **Item 2: accepted.** `brain.intentions.failed_tokens`, a column on the deepest claimed row, added by migration 085 with default 0. `root_limits` adds it to the token sum.
- **Item 9: accepted, with a requirement.** A row that cannot be delivered is stamped so it never stays NULL-keyed and pending forever. The stamp must not claim the owner saw it: log a WARNING naming the row, and record why it is closed. The plan reviewer must confirm that no owner-facing surface reports it as "delivered".
- **Item 11: accepted.** The sweep resumes an `approved` proposal once it has been decided for more than 60 s and less than the proposal TTL. `claim_execution` is the fence, so the run is still at-most-once.
- **Item 12: accepted.** The push runs in its own task: never two at once, and `stop()` lets it finish.
- **Open question 1:** keep `/cancel_intention` in the bot, using the C18 rules. A 404 falls through to chat, and only the owner chat may use it.
- **Open question 2:** no `GET /intentions/{id}` or `open_questions` in 2e (YAGNI). Phase 3 adds them if the cards need them.
- **Open question 3:** `GET /intentions` returns an empty list when continuation is off, for prod parity. Phase 1 roots are never listed.
- **Open question 4: confirmed.** A hanging root that is closed counts as expired, so a late result is reported raw and never reopens the root.
- **Open question 5:** the approved-resume window equal to the proposal TTL is fine.
- **Open question 6 (E16):** cancelling a root that is already finished is a no-op refusal, 409 with the current state, matching C3. The marker is written only on an open root.
- **E1–E15: accepted as the plan proposes.** The plan reviewer verifies each one against the code.
- **Deploy:** two restarts, as the plan recommends. First deploy with the flag off so the rollback and close sweeps run, then turn the flag on when the owner says so.
