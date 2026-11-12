# F111: Obligation Ledger (promise gate, commit tool)

**Status:** PR A merged (foundation, shadow mode). PR B1 in review: the overdue watcher and the stage-2 classifier, both off by default (`NOUS_PROMISE_WATCHER_ENABLED`, `NOUS_PROMISE_GATE_CLASSIFIER_ENABLED`); see [PR B1](#pr-b1-the-overdue-watcher-and-stage-2). PR B2 (the enforce re-entry) not started. `NOUS_PROMISE_GATE_ENABLED` (default `true`), `NOUS_PROMISE_GATE_MODE` (default `shadow`). Needs migration 107 (`sql/migrations/107_commitments.sql`; see [The migration](#the-migration) and the [DDL](#appendix-the-ddl-of-migration-107)); B1 adds migration 108 (the [DDL](#appendix-the-ddl-of-migration-108)).

## Problem

Nous says "I'll do X" and nothing makes X happen. 16 incidents in 46 days (Sep 26 to Nov 10), 12 of them caught by the owner, median 2h15m to catch, worst 25h30m. Six prose rules were saved after incidents and each was followed by a recurrence. A promise with no artifact leaves no row anywhere, so the system cannot even count its misses. The research report behind this feature (ledger kernel spec 05, sections 0, C/M1, D and E) is the design source; this page records what was built.

## Design (PR A)

- **`brain.commitments`**: one row per obligation, with `due_at`, `status` (`open`, `backed`, `proposed`, `done`, `broken`, `cancelled`, `deferred`, `superseded`), `kind` (`action`, `goal`, `sweep_apply`, `follow_up`, `awaiting_user`), `consent` (`none`, `explicit`, `silence`), the artifact link and the watcher's bookkeeping columns (read by PR B). `short_id` is what the owner reads and types: `C-0042`. `(agent_id, dedupe_key)` is unique, the key being md5 of the turn key and the normalised `what`, so a retried write in the same turn is one row. `CommitmentStore` (`nous/brain/commitments.py`): `create_idempotent`, `link_artifact`, `list_watchable`, `mark`, `stats_week`, `log_gate_turn`.
- **`nous_system.promise_gate_log`**: one row per turn the gate ran on, clean turns included: the denominator (`detected`, `backed`, `unbacked`, `outcome` `clean`/`footer`/`error`, `latency_ms`).
- **`commit` tool** (`nous/api/tools.py`): `commit(what, due, consent?, awaiting_owner?)` writes a row (`detected_by='tool'`) and returns `Commitment C-0042 recorded: …`. `due` takes exactly two forms, `in N minutes|hours|days|weeks` or ISO 8601 with an offset; anything else is refused (no fuzzy parse). It must be in the future and at most 90 days away, and `what` must be at least 8 characters. Only a real `true` for `awaiting_owner` makes the row `awaiting_user`; the row's grace is `NOUS_PROMISE_GATE_GRACE_SECONDS`. Classified `_WRITE` (`tool_classes.py`), so every context may call it: chat, MCP, subtask, DAG node, scheduled, heartbeat triage, checks and callbacks (within what they declared), and `internal_only` lineages. It is not in `_SUBTASK_EXCLUDED_TOOLS`. The harness injects the session, the runner's per-turn id (the idempotency key) and the turn kind; the row's `source_kind` follows the turn kind, and a streamed chat turn's row is `chat_stream` as the gate's is (PR B1: the stream path dispatches with `stream=True`). Its output never says "committed" (the claim verifier reads that word as a git claim).
- **Promise gate** (`nous/cognitive/promise_gate.py`), called by `AgentRunner._promise_gate_step` at both terminal points: `stream_chat`'s `if not tool_calls:` (Telegram's path) and `run_turn` after `_tool_loop` returns.
  - `PromiseDetector` (stage 1, deterministic): code fences, inline code, quote-block lines and quoted strings are dropped; a sentence is a promise when it has an agent-subject future form (`I'll`, `I will`, `I'm going to`, `I plan to`, `we'll`, `will be launched|started|scheduled|…`, a sentence-initial `Doing X` or `next step`) and a deferral marker. A strong marker suffices (a clock time, a date, `tomorrow`, `tonight`, `in N hours`, `within the hour`, `next step`, or a consent-by-silence phrase: `unless you object`, `unless you prefer otherwise`, `if I don't hear`, …); a weak one (`today`, `later`, `then`, `when`, `after`, `once`, …) needs a verb from `ACTION_VERBS`. Questions and offers ("if you want", "want me to", "should I") are not promises. A promise conditional on the owner ("once you confirm") is `kind='awaiting_user'`. A future form followed by `not`/`never` (but not `not only`) or by filler (`I'll be honest`, `we'll see`), a passive one done `by` someone else (`by then`, `by morning`, `by the weekend`, `by November 20` are times, not someone), and a `next step` whose subject is the owner do not count; `HH:MM` after `at`/`by`/`around`/`before`/`until` is a time with or without a zone, a bare number only with am/pm or a zone (`at 9` is not one), and an ISO date is a marker only after `on`/`by`/`before`/`until`. Code, quotes and quote blocks are stripped from the whole reply first, then only the last 32 KB of the prose left is read (a cut never lands inside a fence), from the first sentence end or newline after the cut: the partial sentence before it, which may start inside a key, is not read, and a tail with no sentence end is not read at all. The stored sentence and `what` are cleaned and redacted whole before they are cut. Due time: `in N units` from the turn's time (capped), `within the hour`, a clock time only with an explicit UTC marker, bound to an ISO date or to `today`/`tomorrow` anywhere in the sentence; a clock time with a date the parser cannot bind (a weekday, `next week`, `Nov 20`), a day word next to a time it cannot read (`6 pm`, `18:15` without a zone) and anything else get now + `NOUS_PROMISE_GATE_DEFAULT_DUE_MINUTES`. The row's `evidence` says which: `{"due_parsed": true|false}`.
  - `BackingResolver`: by artifact id only. A promise is backed by a non-error result of `schedule_task`, `dag_create`, `spawn_task`, `propose_action`, `commit` or `heartbeat_check_create` whose text carries the id each handler writes (pinned by tests against the handlers' real success text). One such call backs every promise of the turn, except a lone `commit`, which backs only a promise whose words it shares; with more, a promise needs a call whose arguments (`propose_action`'s `tool`, `arguments` and `rationale` included) share at least 30% of its words. A turn ended by `submit_final_report` or `resolve_intention` is scanned on that call's report (`summary`, `note`), not on the loop's "Report submitted." marker. `record_decision`, `learn_fact`, `create_censor`, `submit_final_report` and reads never back a promise.
  - `PromiseGate.run`: writes one `open` row (`detected_by='gate'`) per unbacked promise and one log row. The same sentence twice is one promise. In `warn` it returns one footer line (each id with its own deadline as stored), e.g. `⏱ Recorded as commitment C-0042 due in 60 min (18:45 UTC); nothing is scheduled for it yet. /drop C-0042 to cancel.` The stream path yields it as one more `text_delta`; `run_turn` appends it to the returned text for a foreground caller only. Neither stores it: the conversation, the turn row, `post_turn` (the episode), the claim check and the channel's last reply all get the reply without it. Background turns get rows, never a footer.
  - Bounded and fail-open: the gate runs as a task of its own; in `shadow` the turn does not wait for it, in `warn` it awaits the footer through a shield. Every gate write takes one of `PROMISE_GATE_MAX_CONCURRENT` (4) slots shared with the runner's forks, and the slot wait plus the write are cut at 2 s (`PROMISE_GATE_TIMEOUT_SECONDS`); `AgentRunner.close()` drains what is left. Any exception or timeout is logged once at WARNING, an `outcome='error'` row is attempted, and the reply goes out unchanged. A cancellation of the turn still propagates. Stored text is cleaned (no category-C characters) and redacted before it is cut.
  - The gate never sends anything, never creates a schedule, DAG or spawn, and never acts on parsed prose.
- **`/drop C-n` and `/done C-n`** (Telegram, owner chat only, like `/cancel_intention`): `POST /commitments/{id}/drop|done` sets `cancelled` (`close_reason='owner_drop'`) or `done` (`owner_done`) on a live row; a repeat answers "already closed", an id the server does not know answers `No commitment C-0042.` Answers are fixed words and the id, never the row's text. The route needs the owner's secret (`X-Nous-Owner-Secret`, checked against `NOUS_OWNER_ROUTE_SECRET_SHA256`; unset, it refuses everything), which the bot sends from `NOUS_OWNER_ROUTE_SECRET` (a random value, `openssl rand -hex 32`; the digest is `printf %s "$SECRET" | sha256sum | cut -d' ' -f1`), so the agent's own shell cannot close a row as the owner; the bot's `actor` is stored in the row's `evidence`.

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

## PR B1: the overdue watcher and stage 2

Everything in B1 is off at merge. The dead-man line and the overdue ping are the only things in B1 that reach the owner unasked.

- **Stage 2** (`nous/cognitive/promise_classifier.py`, `NOUS_PROMISE_GATE_CLASSIFIER_ENABLED`): after `PromiseGate.run` has written its rows it hands each to `ClassifierScheduler.submit` and returns; the turn never waits. One call per gate row (never a `commit` row) through `call_background_llm_structured` on the client the other background classifiers use: Anthropic only (`NOUS_PROMISE_GATE_CLASSIFIER_MODEL`, default `claude-haiku-5-5`; start-up refuses a model that does not start with `claude-`), purpose `promise_gate_classifier` in `model_call_log` (not a main-loop purpose), tool `classify_promise`, 300 max tokens, no temperature. The input is the row's stored `promise_text` only: the one sentence, already cleaned and redacted, at most 500 characters; never the reply, the conversation or a tool result. The verdict goes to `evidence.stage2` = `{verdict: promise|not_promise|error, model, prompt_version (s2-v1), latency_ms, at, attempts, reason}`; the reason is cleaned (`clean_text`) and cut to 200 characters, and is not shown to the owner. At most `NOUS_PROMISE_GATE_CLASSIFIER_MAX_CONCURRENCY` (2) calls at once, each bounded by `NOUS_PROMISE_GATE_CLASSIFIER_TIMEOUT_SECONDS` (10); a row in flight or with a verdict is not classified again. Fail-open: a timeout, an exception or a missing or unknown label stores `error` (best effort) with one WARNING. The root runner's `close()` waits 5 s for the calls in flight, then cancels them (a cancelled call stores nothing).
- **The overdue watcher** (`nous/heartbeat/commitment_watcher.py`, `NOUS_PROMISE_WATCHER_ENABLED`): a task of the heartbeat runner of its own (never routed through findings), one run every `NOUS_PROMISE_WATCHER_INTERVAL_SECONDS` (300). A run:
  1. marks `orphaned` (one WARNING with the ids) a claim older than 2 intervals that a crash left behind; it is never re-sent;
  2. reads this agent's `open`/`proposed` rows with no artifact, `notify_count = 0`, not suppressed, past `due_at + grace`, created at or after `NOUS_PROMISE_WATCHER_SINCE` (never a `goal`, `awaiting_user`, backed or closed row, and never a row with a stored `not_promise` verdict, whatever the classifier flag says now), by `due_at` then `id`;
  3. with the tool-call log on, leaves out (and marks `evidence.watch = {suppressed_by, at}`) a row a successful `schedule_task`, `spawn_task`, `dag_create` or `heartbeat_check_create` covers: one recorded in `tool_call_log` in the same session, on a later turn. The row stays open. With the log off, or its query failing, the row is pinged;
  4. stage 2 rules: a stored `not_promise` verdict keeps the row out of the ping whatever the classifier flag says (owner ruling I3 on PR #906, a change to plan D: turning the classifier off after some weeks must not make every accumulated `not_promise` row eligible), and such a row is never closed; the flag decides only what happens to rows with no verdict: with the classifier off every other gate row counts (shown "(not checked)"); `promise` counts; a `commit` row always counts; a gate row with no verdict, or `error` with fewer than 2 attempts, is classified once more in the run (at most 2 such retries per listed row per run; the rest wait for the next run), and if it is still not settled it is pinged "(not checked)";
  5. in quiet hours claims nothing (the rows go out in the first run after; the expiry and the stage-2 retry still run);
  6. claims at most `NOUS_PROMISE_WATCHER_MAX_ITEMS` (10, at most 15 so the message fits Telegram's limit) rows (`notify_count = 1`, `evidence.ping = {state: claimed, run_id, claimed_at, unknown_sends}`, only where `notify_count = 0`); only the rows this run claimed are listed, and the message ends "+N more will follow." when more are eligible;
  7. sends one message: `Possibly unbacked commitments:`, one line per row (`C-0012 'I'll check the deploy tonight' (due Thu 12 Nov 21:00 UTC)`, the sentence cut to 120 characters, "(no time stated)" when the deadline was the default, "(not checked)" when stage 2 did not say `promise`), then `Reply /done C-0012 or /drop C-0012.`;
  8. settles by what is known about the send (`SendResult`, from `HeartbeatRunner._send_telegram`): `SENT` (status below 400) sets `ping.state = 'sent'`; `NOT_SENT` (Telegram not configured, a connection that failed before the request, an HTTP error status, 429 and 5xx included) releases the rows for the next run, so an outage only delays the ping; `UNKNOWN` (a timeout or a dropped connection after the request was written, or anything else) releases them with `unknown_sends` one higher, and the third marks the row `failed` (one WARNING; never sent again). Every write is a set under a condition, never an increment. A sent ping whose record then fails keeps `notify_count = 1` and is never sent again.
  A run that fails is one WARNING; the heartbeat's other work is not affected.
- **Start-up checks** (each one WARNING when the heartbeat starts the watcher): `NOUS_PROMISE_WATCHER_SINCE` unset, empty or not ISO 8601 with an offset: the watcher does not run. Telegram not configured (bot token, owner chat id): it does not run. The classifier off: it runs, and says "promise watcher enabled without stage-2 classifier: every gate-detected row past due+grace will be pinged, false positives included". The heartbeat off: it does not run (WARNING from `main.py`).
- **The dead-man line**: the watcher keeps `last_tick_at` in memory (process start, then the end of each run that completed, quiet-hours runs and runs that pinged nothing included). When the watcher is running and no run completed for 3 intervals (one failed run between two good ones is not a stall), the next live chat reply (an `interactive` turn in a session a person converses in; never a subtask, DAG node, continuation, heartbeat turn, MCP call or a job's session) gets one more line after the reply is stored: `⚠ Commitment watcher has not run since 09:00 UTC; overdue promises are not being checked.` It is shown once per stall, whatever the gate mode, and never stored.
- **Stage 1, passive with a deadline**: a future passive (`will be`, `'ll be`, `is/are going to be`) of a verb in `ACTION_VERBS` with a `by <time>` is a promise when the existing due-time parser reads that deadline (`due_parsed = true`): "It'll be deployed by tomorrow.", "The chain will be relaunched by 18:15 UTC." A deadline the parser does not read ("by morning", "by November 20", "by 5pm") is not enough: the row would get the default due and the watcher would ping long before the stated date. `will be reviewed by Tim`, `was merged by Tim` and `will be delivered by Friday` (not an action verb) are not promises.
- **Retention**: `nous_system.promise_gate_log` rows older than `NOUS_PROMISE_GATE_LOG_RETENTION_DAYS` (30) are deleted at startup and daily, in batches, while the gate is on. `brain.commitments` rows are never deleted.
- **Migration 108** replaces 107's `commitments_watch_idx` with `(agent_id, status, due_at) WHERE status IN ('open', 'proposed')`, the watcher's read ([DDL](#appendix-the-ddl-of-migration-108)).

### Enabling B1

1. Apply migration 108 (the watcher works without it; the index makes its read cheap).
2. `NOUS_PROMISE_GATE_CLASSIFIER_ENABLED=true`. New gate rows get `evidence.stage2`.
3. Shadow check: read the verdicts against the rows (the query below), and the `promise_gate_classifier` rows in the Model calls view.
4. `NOUS_PROMISE_WATCHER_SINCE=<now, ISO 8601 with an offset>` and `NOUS_PROMISE_WATCHER_ENABLED=true` (the heartbeat on, Telegram configured). Rows created before the cutoff (the shadow era) are never watched; `/drop` and `/done` still work on them.

Rollback: `NOUS_PROMISE_WATCHER_ENABLED=false` and `NOUS_PROMISE_GATE_CLASSIFIER_ENABLED=false`. Nothing else changes.

The shadow-review query (orphaned, failed and suppressed rows included):

```
SELECT short_id, status, detected_by, created_at, due_at, evidence->'stage2'->>'verdict' AS verdict,
       evidence->'ping'->>'state' AS ping, evidence->'watch'->>'suppressed_by' AS suppressed_by, promise_text
FROM brain.commitments WHERE agent_id = 'nous-default' AND created_at > now() - interval '7 days'
ORDER BY created_at DESC;
```

## Deferred to PR B2

- The `enforce` re-entry on both paths (repair message, cap `NOUS_PROMISE_GATE_MAX_REENTRIES`, only with a `max_turns` iteration left), and tests 6-enforce, 8, 9, 14 and 17 of the spec.

## Rollout

Shadow 2 to 3 days (log populated, no errors, p95 stage 1 < 5 ms, recall gate on the stored class (a) replies), then warn with PR B's watcher, then enforce on the stream path. Each step is a setting change.

## Known limits

- Detector recall is unmeasured until the hand-labelled samples are read. A promise the detector misses leaves no row.
- A clock time without a zone is not parsed (no owner time zone is known to the code); it gets the default due.
- Offers are not counted in `promise_gate_log` (no column for them).
- B1: a `backed` row whose artifact later failed is not watched.
- B1: the classifier sees one redacted sentence with no context.
- B1: any backing tool call later in the same session suppresses the ping, even one unrelated to the promise.
- B1: the watcher on with the classifier off pings every gate row with no stored `not_promise` verdict, false positives included.
- B1: one ping per row, no repeat reminders. A crash between claim and send can lose a ping (a lost ping is preferred over a repeated one). A row is sent at most 3 times, and only when every send's outcome was unknown.
- B1: "+N more will follow" counts the eligible rows not listed, unclassified ones included; stage 2 may still drop some of them.
- B1: a send that was in flight when the process stopped leaves its rows claimed; they are marked `orphaned` and not re-sent.
- B1: the dead-man line cannot report a dead process (no process, no reply to carry it).
- B1: rows created before `NOUS_PROMISE_WATCHER_SINCE` (the shadow era) are never watched.
- B1: no owner time zone is known to the code: the ping and the dead-man line show UTC.
- B1: the passive rule only knows the verbs in `ACTION_VERBS`: "It'll be done by tomorrow." and "The report is going to be ready by 5pm." are not flagged ("do" and "ready" are not in the list).

## Appendix: the DDL of migration 107

`sql/migrations/107_commitments.sql` holds exactly this block (a test compares the two).

```sql
-- 107: the obligation ledger (F111 PR A)
--
-- brain.commitments: one row per forward-looking obligation ("I'll do X by T"), written by the `commit` tool or by
-- the turn-close promise gate (nous/cognitive/promise_gate.py) when a reply promises work that no artifact backs.
-- short_id is what the owner sees and types (C-0042, `/drop C-0042`). dedupe_key makes an insert idempotent: the
-- store writes md5(turn key || normalised what), so a retried write of the same promise in the same turn is one row.
-- status: open (nothing linked), backed (an artifact is linked), proposed (a background turn staged it), done,
-- broken, cancelled, deferred, superseded. The overdue watcher of PR B reads open/backed/proposed only.
-- artifact_kind adds 'check' (a heartbeat_check_create id) to the spec's list: that tool backs a promise too.
--
-- nous_system.promise_gate_log: one row per turn the gate ran on, clean turns included: the denominator of
-- detected, backed and unbacked promises. agent_id is added to the spec's columns (every table is agent-scoped).
--
-- No column is named like a credential. Writer: the app. Readers: the app only; no reader grant, and
-- scripts/ops/db_roles.sql is not touched. Created by the application role through the migrator.

CREATE TABLE IF NOT EXISTS brain.commitments (
    id                 UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    short_id           BIGSERIAL    UNIQUE,
    agent_id           TEXT         NOT NULL,
    created_at         TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at         TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    source_kind        TEXT         NOT NULL CHECK (source_kind IN
                       ('chat_stream', 'chat_turn', 'subtask', 'dag_node', 'scheduled', 'heartbeat', 'continuation',
                        'decision', 'sweep', 'manual')),
    session_id         TEXT,
    turn_id            TEXT,
    episode_id         UUID,
    origin_decision_id UUID,
    origin_subtask_id  UUID,
    detected_by        TEXT         NOT NULL CHECK (detected_by IN ('tool', 'gate', 'decision_link', 'bg_report', 'manual')),
    detector_version   TEXT,
    confidence         REAL,
    promise_text       TEXT         NOT NULL CHECK (char_length(promise_text) <= 500),
    what               TEXT         NOT NULL,
    kind               TEXT         NOT NULL DEFAULT 'action' CHECK (kind IN
                       ('action', 'goal', 'sweep_apply', 'follow_up', 'awaiting_user')),
    trigger_spec       JSONB,
    action_spec        JSONB,
    consent            TEXT         NOT NULL DEFAULT 'none' CHECK (consent IN ('none', 'explicit', 'silence')),
    due_at             TIMESTAMPTZ  NOT NULL,
    grace_seconds      INT          NOT NULL DEFAULT 900,
    status             TEXT         NOT NULL DEFAULT 'open' CHECK (status IN
                       ('open', 'backed', 'proposed', 'done', 'broken', 'cancelled', 'deferred', 'superseded')),
    artifact_kind      TEXT         CHECK (artifact_kind IN
                       ('schedule', 'dag', 'subtask', 'intention', 'proposal', 'job', 'file', 'pr', 'check')),
    artifact_id        TEXT,
    artifact_linked_at TIMESTAMPTZ,
    evidence           JSONB,
    closed_at          TIMESTAMPTZ,
    close_reason       TEXT,
    last_reconciled_at TIMESTAMPTZ,
    notified_at        TIMESTAMPTZ,
    notify_count       INT          NOT NULL DEFAULT 0,
    dedupe_key         TEXT         NOT NULL,
    UNIQUE (agent_id, dedupe_key)
);

-- The watcher's query (PR B): live rows by deadline.
CREATE INDEX IF NOT EXISTS commitments_watch_idx
    ON brain.commitments (status, due_at)
    WHERE status IN ('open', 'backed', 'proposed');

CREATE TABLE IF NOT EXISTS nous_system.promise_gate_log (
    id           BIGSERIAL    PRIMARY KEY,
    created_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    agent_id     TEXT         NOT NULL,
    session_id   TEXT,
    turn_id      TEXT,
    path         TEXT         NOT NULL CHECK (path IN ('stream', 'run_turn')),
    context_kind TEXT,
    mode         TEXT         NOT NULL,
    detected     INT          NOT NULL,
    backed       INT          NOT NULL,
    unbacked     INT          NOT NULL,
    reentries    INT          NOT NULL DEFAULT 0,
    outcome      TEXT         NOT NULL,
    latency_ms   INT
);

-- The weekly counts read the newest rows of an agent.
CREATE INDEX IF NOT EXISTS idx_promise_gate_log_agent_created
    ON nous_system.promise_gate_log (agent_id, created_at DESC);
```

## Appendix: the DDL of migration 108

`sql/migrations/108_commitments_watch_index.sql` holds exactly this block (a test compares the two, and applies it twice).

```sql
-- 108: the overdue watcher's index on brain.commitments (F111 PR B1)
--
-- 107 indexed (status, due_at) over open, backed and proposed rows. The watcher reads one agent's open and proposed
-- rows past their deadline (CommitmentStore.list_watchable: agent_id, status and due_at < now), so the index leads
-- with agent_id and leaves backed rows out. Idempotent: the old index is dropped if present and the new one created,
-- so a second run ends with the same index. No grant.

DROP INDEX IF EXISTS brain.commitments_watch_idx;

CREATE INDEX IF NOT EXISTS commitments_watch_idx
    ON brain.commitments (agent_id, status, due_at)
    WHERE status IN ('open', 'proposed');
```
