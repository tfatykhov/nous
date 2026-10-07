# F099 Phase 2d: carry-over from 2a–2c, with lead rulings (binding for the 2d plan)

Base: `main` at `9a3121e8` (2c-2 merged as #706). The runner (`nous/handlers/continuation_runner.py`), the owner publisher (`nous/handlers/continuation_publisher.py`) and the store (`nous/brain/continuation.py`) are on main and land dark. 2d builds on them; read the code, not the 2c plans, for every signature.

## Already in place for 2d (verified by the 2c-2 final review, section 7)

- `ArrivalState.proposals` exists. The `resolve_intention` executor already refuses any decision other than `ask` when a proposal was staged this turn.
- Extra tools are executor closures over the per-turn state (`make_resolve_intention_executor` is the pattern). `propose_action` takes `ctx` the same way.
- `_commit`'s `ValueError` arm already treats a refused commit as a failed attempt.
- `fail_attempt(..., arrival_id=)` exists (2c-2 final wave), so `arrival_decided` carries the arrival id for every outcome.
- `OwnerPublisher` sends REPORT and QUESTION rows. A send is `sent`, `transient` (an exception, 401, 404, 429 or 5xx; the queue pauses) or `refused` (400, 403, or an empty chat id; the row is stamped and skipped). `PUSHED_KINDS` has a comment that says 2d adds `MSG_PROPOSAL`.
- The store's lock order: in one root, the root row first, then the claimed intentions in id order, then the inbox rows. Across roots: `(created_at, id)`. Every lock is `FOR NO KEY UPDATE`.
- `report_id` is the owner-facing inbox row's `source_id`, not its PK. `/approve <id>`, `/answer <id>` and `record_answer` key on `source_id`.

## Required in 2d (each needs a test)

1. **`propose_action` is an internal-only extra tool and is not terminal.**
   - Add it to `tool_policy.INTERNAL_ONLY_EXTRA_TOOLS`, or the 2a `LINEAGE_ALLOWED` pins break.
   - Keep it **out of** `TERMINAL_EXTRA_TOOLS`. A terminal name would end the loop.
   - Pin that the name collides with no `TOOL_CLASSES` name and with nothing in the `internal_only` allowed set. 2c-2 already has this pin for `resolve_intention`; extend it.
2. **`propose_action` validates and stages; it never runs anything.**
   - `tool` must satisfy `dispatcher.is_registered` and `not tool_policy.internal_only_allowed(tool, ctx=ctx)`. A tool the lineage may already run is not a proposal.
   - The arguments are validated against the tool's input schema when one exists.
   - It inserts a `staged` proposal row carrying the claim token and appends its id to `state.proposals`.
   - It returns the short id.
3. **Staged → pending happens only at the fenced commit.**
   - `_commit` calls `publish_staged` inside the store's SAVEPOINT, fenced on the same claim token.
   - `_fail` and `_release` call `expire_staged(claim_token)`, so a failed or released turn leaves no orphan proposal.
   - A commit that loses its fence publishes nothing.
4. **The owner approves deterministically, and the approved call runs exactly once.**
   - `execute_approved_proposal` runs the tool under an `approved_action` context with **authority `owner`**. An `internal_only` approved context would still be refused and would log `internal_only:undeclared`.
   - It runs through the execution ledger with the idempotency scope `proposal:{id}`. A second approve, a retry or a crash re-run never runs the call twice.
   - No model call takes part in the approval or the execution.
5. **One semantic layer serves every owner surface.** This is the owner's decision of 2026-10-06; the A2UI cards move into Phase 3.
   - Approve, reject and answer each go through one store/runner function (contract: `decide_proposal`, `answer_question` / `record_answer`).
   - The REST routes call those functions. The Telegram bot calls the REST routes, with inline buttons, `/approve`, `/reject`, `/answer` and reply-to. The Phase 3 cards will call the same functions through the F092 `ActionRouter`, so 2d adds no card code.
   - Every owner action is deterministic and never model-mediated.
   - Each route is idempotent. A second approve of an approved proposal returns its current state, not an error that makes the bot retry.
6. **`record_answer` locks the root first** (the 2c1-6 deadlock shape), with a lock-order test against `expire_roots` and `commit_arrival`.
7. **The batch wake counts proposals.**
   - Extend `_question_state` as well as `arrival_is_terminal` and `wake_terminal_arrivals`, so an arrival waits until every question **and** proposal it raised is terminal (answered, approved, rejected or expired).
   - `wake_terminal_arrivals` today asks only about questions.
8. **The publisher sends PROPOSAL rows.**
   - Add `MSG_PROPOSAL` to `PUSHED_KINDS`.
   - Proposals get Telegram inline buttons (Approve/Reject) whose callback data carries the proposal's `source_id`.
   - Questions get `force_reply`, so a reply-to answers them.
   - Keep the sent/transient/refused outcomes. Respect quiet hours, as REPORT and QUESTION rows already do.
9. **Proposals expire.** A proposal unanswered by its deadline expires; the arrival then counts it as terminal and wakes. An approve that arrives after expiry is refused with a clear message.

## Rulings

- **R8, an answer to a question whose root expired or closed:** `record_answer` refuses it with a clear owner-facing message ("this work has already ended"). It never comes back as a raw REPORT through `record_result`'s closed-root branch.
- **R9, `propose_action` gets no execution-ledger row and no F064.1 activity ping of its own.** Staging is a row write, not a side effect. The approved execution gets the ledger row, keyed `proposal:{id}`.
- **R10, approval needs owner authority on the route.** The REST decide/answer routes use the same owner authentication as the existing owner routes. The bot's calls carry it. No agent tool can reach them: `decide_proposal` is not a registered tool.
- **R11, still dark.** Nothing is wired while `CONTINUATION_RUNNER_READY` is False. The REST routes answer 404 for every id, because no proposal rows exist in prod, and the bot handlers are inert without rows. Pin both under prod's exact flags (intentions, inbox and result memory ON; continuation OFF).

## Out of 2d scope (2e / Phase 3)

- **2e, before the flip:**
  - cancel (`cancel_root` cascade, the cancelled-roots view in `_authorize_tool_call`, cancel of the DAGs a lineage started);
  - the unified late-result rule (expired → report raw; cancelled → stamp silently);
  - counting failed attempts' tokens;
  - discarding a leftover `intent-<root>` session at the start of each turn;
  - rollback when there is no owner channel;
  - flipping `CONTINUATION_RUNNER_READY`.
- **Phase 3:** the A2UI cards (proposal, question, active intentions with cancel, reports), calibration and the dashboard.

## Owner instructions in force

- Implementers run on Opus 5.5, plan writers on Sonnet 5.5, reviewers and architects on Fable 5.1.
- Merge gate: green CI plus a clean Fable final review.
- The repo is public: no private hosts, IPs, personal names or machine-local paths in committed files.

## Lead rulings on the 2d plan's conflicts (2026-10-07)

- **C1: accepted.** 2d adds no REST authentication. Every existing owner route has none (spec §9: the network is the gate; a shared token is a separate change). `POST /chat` already runs an owner turn with outward tools, so an unauthenticated approve route grants nothing new. The plan's gates are the only gates: the owner chat, `NOUS_ALLOWED_USERS`, 404 without rows, 503 without a runner, and no agent tool reaching the routes. A shared secret across **all** owner routes is put to the owner as a separate hardening change.
- **C2–C11, C14–C17: accepted as proposed.** The plan reviewer must verify each one against the code.
- **C12: accepted.** Approving one call does not widen what that call starts. An approved `schedule_task` or spawn stays in the lineage and remains `internal_only` (spec: every descendant inherits it).
- **C13: accepted.** A timeout or an exception finishes the proposal `failed`, with the in-doubt text, immediately. An outward action is at-most-once and is never re-run. Only a process stop leaves `executing`, and the sweep then fails it the same way.
- **C18: changed.** Prod parity is strict, so there must be **no** visible prod difference. A `/approve`, `/reject` or `/answer` whose id the route answers 404 for **falls through to chat unchanged**, exactly as a reply-to does. The bot answers "no longer available" only when the route returns 409 (a known proposal that is decided, expired or ended). Under prod's flags every id is 404, so the bot behaves as it does today. Pin it.
- **Residual accepted (open question 4):** REPORT rows stay plain text. A model-authored report may contain a tappable `/command`, but acting on it needs another proposal's id, and the owner-chat gate still applies.
