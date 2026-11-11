# F111: Obligation Ledger (promise gate, commit tool)

**Status:** PR A in review (foundation, shadow mode). PR B (the overdue watcher and the enforce re-entry) not started. `NOUS_PROMISE_GATE_ENABLED` (default `true`), `NOUS_PROMISE_GATE_MODE` (default `shadow`). Needs migration 107 (`sql/migrations/107_commitments.sql`; see [The migration](#the-migration)).

## Problem

Nous says "I'll do X" and nothing makes X happen. 16 incidents in 46 days (Sep 26 to Nov 10), 12 of them caught by the owner, median 2h15m to catch, worst 25h30m. Six prose rules were saved after incidents and each was followed by a recurrence. A promise with no artifact leaves no row anywhere, so the system cannot even count its misses. The research report behind this feature (ledger kernel spec 05, sections 0, C/M1, D and E) is the design source; this page records what was built.

## Design (PR A)

- **`brain.commitments`**: one row per obligation, with `due_at`, `status` (`open`, `backed`, `proposed`, `done`, `broken`, `cancelled`, `deferred`, `superseded`), `kind` (`action`, `goal`, `sweep_apply`, `follow_up`, `awaiting_user`), `consent` (`none`, `explicit`, `silence`), the artifact link and the watcher's bookkeeping columns (read by PR B). `short_id` is what the owner reads and types: `C-0042`. `(agent_id, dedupe_key)` is unique, the key being md5 of the turn key and the normalised `what`, so a retried write in the same turn is one row. `CommitmentStore` (`nous/brain/commitments.py`): `create_idempotent`, `link_artifact`, `list_watchable`, `mark`, `stats_week`, `log_gate_turn`.
- **`nous_system.promise_gate_log`**: one row per turn the gate ran on, clean turns included: the denominator (`detected`, `backed`, `unbacked`, `outcome` `clean`/`footer`/`error`, `latency_ms`).
- **`commit` tool** (`nous/api/tools.py`): `commit(what, due, consent?, awaiting_owner?)` writes a row (`detected_by='tool'`) and returns `Commitment C-0042 recorded: …`. `due` is read by `parse_when`, the resolver `schedule_task` uses; it must be in the future and at most 90 days away, and `what` must be at least 8 characters. Classified `_WRITE` (`tool_classes.py`), so every context may call it: chat, MCP, subtask, DAG node, scheduled, heartbeat triage, checks and callbacks (within what they declared), and `internal_only` lineages. It is not in `_SUBTASK_EXCLUDED_TOOLS`. The harness injects the session, turn number and turn kind; the row's `source_kind` follows the turn kind. Its output never says "committed" (the claim verifier reads that word as a git claim).
- **Promise gate** (`nous/cognitive/promise_gate.py`), called by `AgentRunner._promise_gate_step` at both terminal points: `stream_chat`'s `if not tool_calls:` (Telegram's path) and `run_turn` after `_tool_loop` returns.
  - `PromiseDetector` (stage 1, deterministic): code fences, inline code, quote-block lines and quoted strings are dropped; a sentence is a promise when it has an agent-subject future form (`I'll`, `I will`, `I'm going to`, `I plan to`, `we'll`, `will be launched|started|scheduled|…`, a sentence-initial `Doing X` or `next step`) and a deferral marker. A strong marker suffices (a clock time, a date, `tomorrow`, `tonight`, `in N hours`, `within the hour`, `next step`, or a consent-by-silence phrase: `unless you object`, `unless you prefer otherwise`, `if I don't hear`, …); a weak one (`today`, `later`, `then`, `when`, `after`, `once`, …) needs a verb from `ACTION_VERBS`. Questions and offers ("if you want", "want me to", "should I") are not promises. A promise conditional on the owner ("once you confirm") is `kind='awaiting_user'`. Due time: `in N units` through `parse_when`, `within the hour`, a clock time only with an explicit UTC marker, `tomorrow`; otherwise now + `NOUS_PROMISE_GATE_DEFAULT_DUE_MINUTES`.
  - `BackingResolver`: by artifact id only. A promise is backed by a non-error result of `schedule_task`, `dag_create`, `spawn_task`, `propose_action`, `commit` or `heartbeat_check_create` whose text carries the id each handler writes (pinned by tests against the handlers' real success text). One such call backs every promise of the turn; with more, a promise needs a call whose arguments share at least 30% of its words. `record_decision`, `learn_fact`, `create_censor`, `submit_final_report` and reads never back a promise.
  - `PromiseGate.run`: writes one `open` row (`detected_by='gate'`) per unbacked promise and one log row. In `warn` it returns one footer line, e.g. `⏱ Recorded as commitment C-0042, due in 60 min (18:45 UTC); nothing is scheduled for it yet. /drop C-0042 to cancel.` The stream path yields it as one more `text_delta`; `run_turn` appends it to the returned text for a foreground caller only. Neither stores it: the conversation, the turn row, `post_turn` (the episode), the claim check and the channel's last reply all get the reply without it. Background turns get rows, never a footer.
  - Fail-open: any exception, or a gate run longer than 2 s (`PROMISE_GATE_TIMEOUT_SECONDS`), is logged at WARNING, an `outcome='error'` row is attempted, and the reply goes out unchanged. A cancellation of the turn still propagates.
  - The gate never sends anything, never creates a schedule, DAG or spawn, and never acts on parsed prose.
- **`/drop C-n` and `/done C-n`** (Telegram, owner chat only, like `/cancel_intention`): `POST /commitments/{id}/drop|done` sets `cancelled` (`close_reason='owner_drop'`) or `done` (`owner_done`) on a live row; a repeat answers "already closed". An id the server does not know goes on to chat. Answers are fixed words and the id, never the row's text.

## Modes

| `NOUS_PROMISE_GATE_MODE` | Rows | Log | Footer |
|---|---|---|---|
| `off` (or `NOUS_PROMISE_GATE_ENABLED=false`) | none (the `commit` tool still writes) | none | none |
| `shadow` (default) | yes | yes | none |
| `warn` | yes | yes | one line per turn, foreground only |
| `enforce` | as `warn` in PR A (logged); PR B adds the capped repair re-entry | | |

Rollback: `NOUS_PROMISE_GATE_MODE=off`. The tables are additive and unused when off. `tool_context_policy_mode` is not touched (it stays `warn`).

## The migration

Migration 107 creates both tables in `brain` and `nous_system` (qualified names, no grant, no extension). `promise_gate_log` adds `agent_id` to the spec's columns (every table is agent-scoped), and `artifact_kind` adds `check` (a `heartbeat_check_create` id). No reader grant: `db_roles.sql` is not touched.

## Deferred to PR B

- The overdue watcher (reconciler v1-min): `list_watchable` over open rows past `due_at + grace`, one consolidated direct Telegram ping, quiet hours, the dead-man line, and the check of later same-chat tool calls before a ping.
- The `enforce` re-entry on both paths (repair message, cap `NOUS_PROMISE_GATE_MAX_REENTRIES`, only with a `max_turns` iteration left), and tests 6-enforce, 8, 9, 14 and 17 of the spec.
- Stage 2 (`NOUS_PROMISE_GATE_CLASSIFIER_ENABLED`): not built; the setting is read by nothing yet.

## Rollout

Shadow 2 to 3 days (log populated, no errors, p95 stage 1 < 5 ms, recall gate on the stored class (a) replies), then warn with PR B's watcher, then enforce on the stream path. Each step is a setting change.

## Known limits

- Detector recall is unmeasured until the hand-labelled samples are read. A promise the detector misses leaves no row.
- A clock time without a zone is not parsed (no owner time zone is known to the code); it gets the default due.
- Offers are not counted in `promise_gate_log` (no column for them).
