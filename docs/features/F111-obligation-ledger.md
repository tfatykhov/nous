# F111: Obligation Ledger (promise gate, commit tool)

**Status:** PR A in review (foundation, shadow mode). PR B (the overdue watcher and the enforce re-entry) not started. `NOUS_PROMISE_GATE_ENABLED` (default `true`), `NOUS_PROMISE_GATE_MODE` (default `shadow`). Needs migration 107 (`sql/migrations/107_commitments.sql`; see [The migration](#the-migration) and the [DDL](#appendix-the-ddl-of-migration-107)).

## Problem

Nous says "I'll do X" and nothing makes X happen. 16 incidents in 46 days (Sep 26 to Nov 10), 12 of them caught by the owner, median 2h15m to catch, worst 25h30m. Six prose rules were saved after incidents and each was followed by a recurrence. A promise with no artifact leaves no row anywhere, so the system cannot even count its misses. The research report behind this feature (ledger kernel spec 05, sections 0, C/M1, D and E) is the design source; this page records what was built.

## Design (PR A)

- **`brain.commitments`**: one row per obligation, with `due_at`, `status` (`open`, `backed`, `proposed`, `done`, `broken`, `cancelled`, `deferred`, `superseded`), `kind` (`action`, `goal`, `sweep_apply`, `follow_up`, `awaiting_user`), `consent` (`none`, `explicit`, `silence`), the artifact link and the watcher's bookkeeping columns (read by PR B). `short_id` is what the owner reads and types: `C-0042`. `(agent_id, dedupe_key)` is unique, the key being md5 of the turn key and the normalised `what`, so a retried write in the same turn is one row. `CommitmentStore` (`nous/brain/commitments.py`): `create_idempotent`, `link_artifact`, `list_watchable`, `mark`, `stats_week`, `log_gate_turn`.
- **`nous_system.promise_gate_log`**: one row per turn the gate ran on, clean turns included: the denominator (`detected`, `backed`, `unbacked`, `outcome` `clean`/`footer`/`error`, `latency_ms`).
- **`commit` tool** (`nous/api/tools.py`): `commit(what, due, consent?, awaiting_owner?)` writes a row (`detected_by='tool'`) and returns `Commitment C-0042 recorded: …`. `due` takes exactly two forms, `in N minutes|hours|days|weeks` or ISO 8601 with an offset; anything else is refused (no fuzzy parse). It must be in the future and at most 90 days away, and `what` must be at least 8 characters. Only a real `true` for `awaiting_owner` makes the row `awaiting_user`; the row's grace is `NOUS_PROMISE_GATE_GRACE_SECONDS`. Classified `_WRITE` (`tool_classes.py`), so every context may call it: chat, MCP, subtask, DAG node, scheduled, heartbeat triage, checks and callbacks (within what they declared), and `internal_only` lineages. It is not in `_SUBTASK_EXCLUDED_TOOLS`. The harness injects the session, the runner's per-turn id (the idempotency key) and the turn kind; the row's `source_kind` follows the turn kind (the tool cannot tell the chat paths apart, so it stamps a streamed chat turn `chat_turn` where the gate stamps it `chat_stream`; the shared `turn_id` joins them). Its output never says "committed" (the claim verifier reads that word as a git claim).
- **Promise gate** (`nous/cognitive/promise_gate.py`), called by `AgentRunner._promise_gate_step` at both terminal points: `stream_chat`'s `if not tool_calls:` (Telegram's path) and `run_turn` after `_tool_loop` returns.
  - `PromiseDetector` (stage 1, deterministic): code fences, inline code, quote-block lines and quoted strings are dropped; a sentence is a promise when it has an agent-subject future form (`I'll`, `I will`, `I'm going to`, `I plan to`, `we'll`, `will be launched|started|scheduled|…`, a sentence-initial `Doing X` or `next step`) and a deferral marker. A strong marker suffices (a clock time, a date, `tomorrow`, `tonight`, `in N hours`, `within the hour`, `next step`, or a consent-by-silence phrase: `unless you object`, `unless you prefer otherwise`, `if I don't hear`, …); a weak one (`today`, `later`, `then`, `when`, `after`, `once`, …) needs a verb from `ACTION_VERBS`. Questions and offers ("if you want", "want me to", "should I") are not promises. A promise conditional on the owner ("once you confirm") is `kind='awaiting_user'`. A future form followed by `not`/`never` (but not `not only`) or by filler (`I'll be honest`, `we'll see`), a passive one done `by` someone else (`by then`, `by morning`, `by the weekend`, `by November 20` are times, not someone), and a `next step` whose subject is the owner do not count; `HH:MM` after `at`/`by`/`around`/`before`/`until` is a time with or without a zone, a bare number only with am/pm or a zone (`at 9` is not one), and an ISO date is a marker only after `on`/`by`/`before`/`until`. Code, quotes and quote blocks are stripped from the whole reply first, then only the last 32 KB of the prose left is read (a cut never lands inside a fence). The stored sentence and `what` are cleaned and redacted whole before they are cut. Due time: `in N units` from the turn's time (capped), `within the hour`, a clock time only with an explicit UTC marker, bound to an ISO date or to `today`/`tomorrow` anywhere in the sentence; a clock time with a date the parser cannot bind (a weekday, `next week`, `Nov 20`), a day word next to a time it cannot read (`6 pm`, `18:15` without a zone) and anything else get now + `NOUS_PROMISE_GATE_DEFAULT_DUE_MINUTES`. The row's `evidence` says which: `{"due_parsed": true|false}`.
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
