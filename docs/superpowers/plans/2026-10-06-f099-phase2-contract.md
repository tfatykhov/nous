# F099 Phase 2: decomposition into sub-PRs and the shared interface contract

Base: `feat/f099-1-intentions-recorded` at `2f23318a` (PR-1, about to merge), on top of `3dae9eae` (main with #698 and #700).
Spec: `docs/superpowers/specs/2026-10-05-f099-intentions-and-continuation-design.md` (binding; section numbers below are its).
Phase 1 notes picked up: `.superpowers/sdd/f099-phase0-phase1/{progress.md, final-review.md, task-1.9-brief.md}`.

Every `file:line` cites `main` at `1c10ed8d` (the PR-1 content). Line numbers drift; anchor by function name.

This document settles (1) the sub-PR boundaries and (2) every name, signature, row shape, route and event that crosses a PR boundary. The per-PR plans copy from §4 and must not rename anything in it. It does not contain task-by-task steps.

---

## 1. Decomposition

Five sub-PRs, plus one optional. Every one lands dark behind `NOUS_CONTINUATION_ENABLED` (default `false`), with the named exceptions in §1.7.

| PR | Name | Scope in one line | Depends on | Tasks (est.) |
|---|---|---|---|---|
| 2a | Enforcement substrate | `continuation` and `approved_action` ContextKinds; `_offered_tools`; the `internal_only` strict path in `_authorize_tool_call` (`force_block`, before the `off` early return); terminal extra tools; `write_file` / `cancel_task` per-call rules; `_origin_authority` injection and min(context, parent row) authority; `dag_create` approval-node refusal under `internal_only`; the orchestrator fail-closed guard; D7 downgrade note in tool text; lineage `web_fetch`/`web_search` logging | PR-1 | 7 |
| 2b | Data and routing | Migration 084; all Phase 2 settings and validators; `insert(session=)` with the widened UNIQUE; intention-only routing for `continue`; I4 `report` close-at-write; `delivered` closes; same-transaction `pending/awaiting_owner → result_ready`; held rows; re-arrivals; owner-facing rows (`intention_report`); push suppression (worker and F087); F087 summary-turn skip for `internal_only` DAGs; `InboxDagPass` filter; `pre_turn(context_kind=)` and the `intent-` skip; `IntentionClosePass` exclusion; `metrics()`; startup rollback; the flag-off gate constant | PR-1 (independent of 2a) | 8 |
| 2c | Continuation runner | `nous/brain/continuation.py` (claim, lease, gate, budgets, fenced commit, repair, TTL, deadline) and `nous/handlers/continuation_runner.py` (the loop, the turn, `resolve_intention`, follow-up, fallbacks, failure, attempts); DAG check-node tokens into `tokens_consumed`; `end_conversation` reflection skip; the reconciler wake pass; bus events | 2a, 2b | 8 |
| 2d | Proposals and owner actions | `propose_action`; `brain.intention_proposals` lifecycle (staged → pending at the fenced commit; expiry); `execute_approved_proposal` under `approved_action`; `proposal:{id}` idempotency scope; the server-side owner publisher (Telegram push with inline buttons, `push_message_id`, quiet-hours deferral sweep); REST decide/answer/list; Telegram bot callback queries, `/approve`, `/reject`, `/answer`, reply-to; batch wake extended to proposals | 2c | 8 |
| 2e | Cancel and the flag gate | `cancel_root` cascade (intentions, subtasks, DAGs, proposals, containers and their fires, the running turn); the cancelled-roots view checked by `_authorize_tool_call`; container-then-schedule lock order; REST `POST /intentions/{root}/cancel` and `GET /intentions`; Telegram `/intentions`; `CONTINUATION_RUNNER_READY = True`; `main.py` wiring; compose and reference docs | 2d | 6 |
| 2f (optional, deferred) | A2UI cards | Companion cards for proposals and questions with fixed options, routed through the existing `ActionRouter` to the same REST-equivalent calls; an "Active intentions" card with cancel | 2e | 4 |

Order: 2a and 2b may be built in parallel on PR-1 (disjoint files: 2a edits `nous/api/runner.py`, `nous/api/tool_policy.py`, `nous/api/execution_context.py`, `nous/api/tools.py`, `nous/dag/orchestrator.py`; 2b edits `sql/migrations/`, `nous/storage/models.py`, `nous/config.py`, `nous/heart/result_inbox.py`, `nous/heart/result_reconciler.py`, `nous/handlers/subtask_worker.py`, `nous/dag/delivery.py`, `nous/cognitive/layer.py`, `nous/main.py`, `nous/brain/intentions.py`). The one shared edit is `nous/brain/intentions.py`: 2a adds `origin_authority` to `IntentionSpec`; 2b adds the `IntentionClosePass` exclusion. Both are additive; whichever merges second rebases. 2c → 2d → 2e are sequential. 2f is deferred: Telegram and REST approval paths exist from 2d, which the spec allows (§5: "without it, proposals and questions go by Telegram and chat only").

### 1.1 PR-2a: Enforcement substrate

Builds: §4.4's tool surface and the four Phase 1 carry-overs that concern authority. Nothing in 2a reads the continuation flag. With no `internal_only` rows (see §3) and no caller creating a `continuation` or `approved_action` context, every turn in prod behaves as on PR-1, except the named exception in §1.7.

Reviewable on its own: the forged-call tests (§7 "Forged calls"), the lineage-narrowing tests for each turn kind using hand-built `ExecutionContext(authority="internal_only")`, and the Phase 1 schema snapshot (`tests/fixtures/f099_spawn_tool_schemas.json`) still byte-identical with intentions off.

### 1.2 PR-2b: Data and routing

Builds: §6 Phase 2 migration, §5 settings, §4.3 Phase 2 routing items 1 to 6, and the task-1.9 carry-overs 2, 3 (partly), 4. With the flag off, the writers keep Phase 1's `legacy` close and F098 Phase A routing (the routing pins of `tests/test_f099_routing_pins.py` run unchanged, with the flag off AND with intentions on / continuation off).

Reviewable on its own: routing pins with three flag states (`off/off`, `on/off`, `on/on`), the same-transaction transition test (a fault after the inbox insert leaves the intention `pending`), re-arrival tests, the rollback test with both flags off.

### 1.3 PR-2c: Continuation runner

Builds: §4.5 (trigger, claim, lease, gate, turn, `resolve_intention`, follow-up, commit, failure), §4.6 bounds and TTL, the `ask` decision as a question only (proposals are 2d), the check-node token roll-up. `main.py` does NOT construct the runner in 2c; §2 explains why. Tests drive `ContinuationRunner.run_once()` directly against Postgres with a fake `AgentRunner._call_api`.

### 1.4 PR-2d: Proposals and owner actions

Builds: §4.4 proposals 1 to 6 and questions, the publisher, REST decide/answer, the Telegram bot's deterministic handlers. The batch-wake rule from 2c (`arrival_is_terminal`) is extended to count proposals. Still dark: the runner is not wired, so no proposal can be staged in prod; the REST routes answer 404 for every id because no rows exist.

### 1.5 PR-2e: Cancel and the flag gate

Builds: §4.6 cancel, the `_authorize_tool_call` cancelled-root refusal, task-1.9 carry-over 5, `GET /intentions`, and flips `CONTINUATION_RUNNER_READY`. This is the PR after which the flag may be turned on (§2).

### 1.6 PR-2f: A2UI cards (optional)

Deferred. When built: `nous/a2ui/builders.py` gets `intention_proposal` and `intention_question` card builders; `ActionRouter` handlers `proposal_decide` / `question_answer` call the same functions the REST routes call (`ContinuationRunner.decide_proposal`, `answer_question`); an "Active intentions" surface lists open roots with a cancel action. Dedup keys: `intention:proposal:<id>`, `intention:question:<id>`, `intention:active`.

### 1.7 Flag-off behaviour and the named exceptions

With `NOUS_CONTINUATION_ENABLED=false` (every PR), behaviour is identical to PR-1 except:

1. **2a: a damaged lineage stamp narrows the turn.** `lineage_from_stamp` already fails closed to `internal_only` (`nous/api/execution_context.py:149-158`); PR-1 recorded that but enforced nothing. After 2a that turn is offered and may dispatch only the `internal_only` allowed set. Read-only in effect (it loses tools, never gains them). Phase 1 never writes an `internal_only` row (§3), so no recorded intention changes behaviour.
2. **2b: migration 084 runs** (DDL only). The inbox UNIQUE gains `agent_id`; `insert`'s conflict target changes with it. Writers' observable behaviour is unchanged.
3. **2b: the startup rollback runs whenever `brain.intentions` exists**, even with both flags off (§4.3 item 6). On a database that never ran with the flag on, it is one SELECT that finds nothing.
4. **2b: `main.py` forces the flag off with a WARNING** until 2e (§2). Visible only as a log line when someone sets the flag early.
5. **2c: `end_conversation` skips reflection for `intent-` sessions** and `pre_turn` accepts `context_kind`. No caller passes either with the flag off.
6. **2b: `pre_turn` skips the result-inbox claim for a session whose id starts with `intent-`**, and so does `_inject_result_inbox` itself (which also skips that session's `touch_channel`). This skip is not gated on either F099 flag: it applies whenever `NOUS_RESULT_INBOX_ENABLED` is on. Nothing in `nous/` produces an `intent-` session id before 2c's runner, so no turn reaches it.

---

## 2. Which PR gates turning the flag on, and how the code enforces it

**PR-2e must be merged before `NOUS_CONTINUATION_ENABLED=true` in prod.** The owner's rule: autonomy without bounds and cancel is not safe. 2c brings the bounds, TTL and the gate; 2e brings cancel.

Why "the runner is not wired" is not enough on its own: once 2b is merged and the flag is on, `continue` results are written with `channel = NULL, session_id = NULL` (§4.3 item 1), and nothing claims them until a runner exists. That strands results, which breaks G6.

The mechanism, the simplest one that cannot be forgotten:

- `nous/brain/continuation.py` (created in 2b as the module skeleton) declares `CONTINUATION_RUNNER_READY: bool = False`.
- `nous/main.py` gets `_gate_continuation_flag(settings)`, defined next to `_warn_on_f098_flags` (`nous/main.py:220`) and called where that one is called, before any component construction reads the flag, in particular before `build_reconciler` (`main.py:1053`), the DAG delivery (`:1293`) and the subtask tools (`:1063`): if `settings.continuation_enabled and not continuation.CONTINUATION_RUNNER_READY`, log `WARNING "NOUS_CONTINUATION_ENABLED=true but the continuation runner is not shipped in this build; continuation stays OFF."` and `object.__setattr__(settings, "continuation_enabled", False)` (the pattern `nous/config.py:3067` uses). Done in `main.py`, not in a Settings validator, because `config.py` must not import `nous.brain` (cycle risk).
- 2e sets `CONTINUATION_RUNNER_READY = True` in the same commit that wires `ContinuationRunner` into `main.py`.
- A test in 2b pins `CONTINUATION_RUNNER_READY is False` and that the gate forces the flag off; 2e flips both assertions.

Also required before the flag goes on (not code-enforced, in the rollout notes): prod's `docker-compose.yml` gets its own `NOUS_CONTINUATION_ENABLED` line (§5), and the `.env.prod-snapshot` read confirms `NOUS_INTENTIONS_ENABLED=true` and `NOUS_RESULT_INBOX_ENABLED=true`.

---

## 3. The gating rule for `internal_only` narrowing

**Can Phase 1 produce `internal_only` rows?** No. `prepare_intention` writes `authority = internal_only` only when the lineage parent ROW is `internal_only` (`nous/brain/intentions.py:379-385`), and no Phase 1 code path writes an `internal_only` root: a root's authority is always `AUTHORITY_OWNER` (line 385, `narrowed` is False with no parent). A damaged stamp makes the CONTEXT `internal_only` (`nous/api/execution_context.py:156-157`), but a spawn from that context is refused (`UNREADABLE_LINEAGE`, `nous/api/tools.py:297-300`, `nous/brain/intentions.py:274-278`) or, when the id is readable, the child takes the parent row's authority, which is `owner` (final-review Minor 3). So every `brain.intentions` row written by Phase 1 has `authority = 'owner'`.

**Rule.** Narrowing is enforced on `ctx.authority == "internal_only"` with **no flag gate**. Reasons:

1. Phase 1 writes no `internal_only` row, so with the flag off the only `internal_only` contexts are damaged-stamp fail-closed ones, and those are exactly the turns that should lose tools (§1.7 exception 1).
2. A flag-gated safety invariant is a footgun: turning the flag off while a lineage is mid-flight would un-narrow its already-running subtasks and checks, which still carry `metadata.intention.authority = internal_only`.
3. `continuation` and `approved_action` contexts are created only by the runner (2c/2d), which exists only when the flag is on and 2e has shipped.

What IS gated on the flag: the routing of `continue` results (2b), the close reason `delivered` (2b), the runner (2c-2e), `propose_action` and `resolve_intention` being offered (2c/2d, via the runner), `deadline` being written (2c), the Telegram push suppression (2b), and the F087 summary skip (2b, keyed on the DAG's intention authority, which only a flag-on continuation can make `internal_only`).

---

## 4. The contract

### 4.1 The intention state machine

States (migration 083, unchanged): `pending`, `result_ready`, `deciding`, `awaiting_owner`, `closed`, `cancelled`, `expired`. Root markers `root_cancelled_at` / `root_expired_at` on root rows only; "the root is open" means both are NULL (§4.1). Every transition is `UPDATE … WHERE state IN (<expected>) [AND claim_token = :token] RETURNING id`; a transition that returns no row is a lost race and is logged, never retried blindly.

| # | From → To | Who | Transaction / fence | PR |
|---|---|---|---|---|
| T1 | (none) → `pending` | `prepare_intention` + `insert_prepared` inside each store's `create()` (`nous/heart/subtasks.py:101-133`, `nous/heart/schedules.py:25`, `nous/dag/store.py:164`) | the work row's transaction; root read `FOR SHARE` | 1 (deadline written from 2c) |
| T2 | `pending` → `closed` (`legacy`) | writers via `close_intention_quietly` (`nous/heart/result_inbox.py:370`), inline `finally` (`nous/api/tools.py:2969`), `IntentionClosePass` | own transaction | 1; kept with the flag off |
| T3 | `pending` → `closed` (`delivered`) | the same writers, flag on, for `none`, `remember`, `report`, `container`; `report` in the SAME transaction as its inbox insert (I4) | `continuation.record_result(session=…)` | 2b |
| T4 | `pending` → `result_ready` | `continuation.record_result` for `continue`: inbox insert + transition in one transaction, `result_at = now` | `WITH … UPDATE … WHERE state IN ('pending') RETURNING` + INSERT | 2b |
| T5 | `awaiting_owner` → `result_ready` | `continuation.wake_arrival` when every proposal and question of the arrival is terminal (§4.4 item 6); applies to every intention in `arrival.intention_ids` | one transaction, `WHERE state = 'awaiting_owner'` | 2c (questions), 2d (proposals) |
| T6 | `closed` → `result_ready` (reopen) | `record_result` on a re-arrival: policy `continue`, root open (§4.3 item 3) | same as T4, `WHERE state = 'closed' AND wake_policy = 'continue'` + root-open predicate | 2b |
| T7 | `result_ready` → `deciding` | `continuation.claim_root` (the §4.5 claim SQL, per-root `FOR UPDATE`, debounce / max-wait) | one transaction READ COMMITTED; sets `claimed_at`, `claim_token` | 2c |
| T8 | `deciding` → `result_ready` (lease released) | `continuation.release_stale_claims` at startup and every sweep: `claimed_at < now - lease`, `attempts + 1`, `claim_token = NULL` | own transaction | 2c |
| T9 | `deciding` → `closed` (`resolved`) | `continuation.commit_arrival` for `continue` (after spawning), `revise`, `drop`, `report`, and both fallbacks | the fenced commit: `WHERE state = 'deciding' AND claim_token = :token` | 2c |
| T10 | `deciding` → `awaiting_owner` | `commit_arrival` for `ask` | same fence | 2c |
| T11 | `deciding` → `result_ready` (unconsumed rows) | `commit_arrival` when the intention has undelivered inbox rows not in `arrival.inbox_ids`, for every decision except `ask` (§4.5 item 6) | same fence | 2c |
| T12 | `deciding` → `closed` (`failed_report`) | `continuation.fail_attempt` after `attempts >= max_attempts`; raw results become a REPORT row | own transaction, fenced on `claim_token` | 2c |
| T13 | any open → `cancelled` | `continuation.cancel_root`; also writes `root_cancelled_at` on the root row (`FOR UPDATE`) | one transaction, root first | 2e |
| T14 | any open → `expired` | `continuation.expire_roots` (TTL sweep); writes `root_expired_at`; reports what exists; containers excluded | one transaction per root | 2c |
| T15 | open `continue` → `closed` (`legacy`) | `continuation.rollback_at_startup` with the flag off | own transaction | 2b |

"Open" = `pending, result_ready, deciding, awaiting_owner`. A `report` intention never reaches `result_ready` (T3). A container never leaves `pending` except by T2/T3 (its schedule deactivated) or T13.

### 4.2 Migration 084 (`sql/migrations/084_intention_arrivals_proposals.sql`)

Rules: additive, idempotent, agent-scoped, full-line `--` comments with no `;` in them, no `DO $$` blocks (the migrator splits on `;`, `nous/storage/migrator.py:30`), the 076 drop-if-exists-then-add pattern for constraint changes (`sql/migrations/076_dag_approval_nodes.sql:6-23`). `tests/test_database.py::test_all_tables_exist` gains both tables; CLAUDE.md's count 53 → 55 (brain 10 → 12).

```sql
-- Migration 084: Intention arrivals and proposals (F099 Phase 2)

CREATE TABLE IF NOT EXISTS brain.intention_arrivals (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id VARCHAR(100) NOT NULL,
    root_id UUID NOT NULL REFERENCES brain.intentions(id),
    n INTEGER NOT NULL,
    intention_ids UUID[] NOT NULL,
    inbox_ids UUID[] NOT NULL DEFAULT '{}',
    report_ids UUID[] NOT NULL DEFAULT '{}',
    claim_token UUID NOT NULL,
    decision VARCHAR(20),
    note TEXT,
    progress_claimed BOOLEAN,
    progress BOOLEAN,
    confidence REAL,
    gate_reason VARCHAR(40),
    tokens_in INTEGER NOT NULL DEFAULT 0,
    tokens_out INTEGER NOT NULL DEFAULT 0,
    decision_record_id UUID,
    outcome VARCHAR(20) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    decided_at TIMESTAMPTZ,
    CONSTRAINT uq_intention_arrivals_root_n UNIQUE (agent_id, root_id, n),
    CONSTRAINT chk_intention_arrivals_decision CHECK (decision IS NULL OR decision IN ('continue', 'revise', 'drop', 'report', 'ask')),
    CONSTRAINT chk_intention_arrivals_outcome CHECK (outcome IN ('resolved', 'fallback_report', 'failed_report')),
    CONSTRAINT chk_intention_arrivals_gate_reason CHECK (gate_reason IS NULL OR gate_reason IN ('cancelled', 'expired', 'past_deadline', 'budget_turns', 'budget_tokens', 'budget_stall', 'limit_depth', 'limit_spawns', 'plan_resolved'))
);

CREATE INDEX IF NOT EXISTS idx_intention_arrivals_root
    ON brain.intention_arrivals (agent_id, root_id, n);

CREATE TABLE IF NOT EXISTS brain.intention_proposals (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id VARCHAR(100) NOT NULL,
    intention_id UUID NOT NULL REFERENCES brain.intentions(id),
    root_id UUID NOT NULL REFERENCES brain.intentions(id),
    arrival_id UUID REFERENCES brain.intention_arrivals(id),
    tool VARCHAR(100) NOT NULL,
    arguments JSONB NOT NULL,
    rationale TEXT NOT NULL,
    state VARCHAR(20) NOT NULL DEFAULT 'staged',
    claim_token UUID NOT NULL,
    deadline TIMESTAMPTZ,
    ledger_key TEXT,
    decided_at TIMESTAMPTZ,
    decided_by TEXT,
    executed_at TIMESTAMPTZ,
    result TEXT,
    error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT chk_intention_proposals_state CHECK (state IN ('staged', 'pending', 'approved', 'executing', 'rejected', 'expired', 'executed', 'failed', 'cancelled'))
);

CREATE INDEX IF NOT EXISTS idx_intention_proposals_open
    ON brain.intention_proposals (agent_id, state)
    WHERE state IN ('staged', 'pending', 'approved', 'executing');

CREATE INDEX IF NOT EXISTS idx_intention_proposals_arrival
    ON brain.intention_proposals (agent_id, arrival_id);

-- Owner-facing rows (reports, questions, proposals) join the inbox as a new
-- source kind; the UNIQUE key gains agent_id (section 4.3 item 4).
ALTER TABLE heart.result_inbox
    DROP CONSTRAINT IF EXISTS chk_result_inbox_source_kind;
ALTER TABLE heart.result_inbox
    ADD CONSTRAINT chk_result_inbox_source_kind
    CHECK (source_kind IN ('subtask', 'dag', 'intention_report'));

ALTER TABLE heart.result_inbox
    DROP CONSTRAINT IF EXISTS chk_result_inbox_msg_type;
ALTER TABLE heart.result_inbox
    ADD CONSTRAINT chk_result_inbox_msg_type
    CHECK (msg_type IN ('INFORM', 'FAILURE', 'BLOCKED', 'REPORT', 'QUESTION', 'PROPOSAL'));

ALTER TABLE heart.result_inbox
    DROP CONSTRAINT IF EXISTS uq_result_inbox_source;
-- agent_id LAST: the reconciler's correlated has_row lookups prefix on
-- (source_kind, source_id) and keep using this index.
ALTER TABLE heart.result_inbox
    ADD CONSTRAINT uq_result_inbox_source UNIQUE (source_kind, source_id, source_generation, agent_id);

ALTER TABLE heart.result_inbox
    ADD COLUMN IF NOT EXISTS arrival_id UUID,
    ADD COLUMN IF NOT EXISTS proposal_id UUID,
    ADD COLUMN IF NOT EXISTS push_after TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS pushed_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS push_message_id BIGINT;

CREATE INDEX IF NOT EXISTS idx_result_inbox_intention_undelivered
    ON heart.result_inbox (agent_id, intention_id) WHERE delivered_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_result_inbox_push_due
    ON heart.result_inbox (agent_id, push_after) WHERE pushed_at IS NULL AND push_after IS NOT NULL;
```

Notes on the columns:
- `brain.intentions` needs **no new column**: `claimed_at`, `claim_token`, `attempts`, `deadline` already exist in 083 (`sql/migrations/083_intentions.sql:30-37`). 2c starts writing `deadline`.
- `intention_arrivals.progress_claimed` is the model's claim; `progress` is the verified value (§4.5 item 4). `gate_reason` is set on arrivals the gate produced without a turn (§4.5 item 3), with `decision` = `drop` or `report` and `outcome = resolved`; it feeds "escalations by cause" (§4.7) without widening the spec's three outcomes. `report_ids` lists the `intention_report` rows the arrival produced.
- `result_inbox.arrival_id` is the arrival a QUESTION or PROPOSAL row belongs to (§4.4 "the report row carries the arrival_id"); `proposal_id` links a PROPOSAL row to its `intention_proposals` row. Both nullable, no FK (same reason as `intention_id`, 083:57-58).
- `result_inbox.source_id` stays `UUID NOT NULL`: an `intention_report` row's `source_id` is a fresh `uuid4()` (`report_id`), `source_generation = 0`.
- ORM: `nous/storage/models.py` gets `IntentionArrival` and `IntentionProposal` after `Intention` (line 443-501), and `ResultInbox` (line 967-1005) gets the five columns and the changed `__table_args__`.

### 4.3 Settings (`nous/config.py`, all in PR-2b)

Placed after `intentions_enabled` (`nous/config.py:1213`). Every one gets its row in `docs/reference/environment-variables.md` and its compose line in 2b (Task 2b-2, lead ruling).

| Field | Env | Default | Bounds |
|---|---|---|---|
| `continuation_enabled: bool` | `NOUS_CONTINUATION_ENABLED` | `False` | |
| `continuation_max_depth: int` | `NOUS_CONTINUATION_MAX_DEPTH` | `3` | `ge=1` |
| `continuation_max_spawns_per_root: int` | `NOUS_CONTINUATION_MAX_SPAWNS_PER_ROOT` | `12` | `ge=1` |
| `continuation_max_turns_per_root: int` | `NOUS_CONTINUATION_MAX_TURNS_PER_ROOT` | `8` | `ge=1` |
| `continuation_max_tokens_per_root: int` | `NOUS_CONTINUATION_MAX_TOKENS_PER_ROOT` | `400000` | `ge=1000` |
| `continuation_stall_limit: int` | `NOUS_CONTINUATION_STALL_LIMIT` | `2` | `ge=1` |
| `intention_root_ttl_hours: float` | `NOUS_INTENTION_ROOT_TTL_HOURS` | `72` | `gt=0` |
| `continuation_max_concurrent: int` | `NOUS_CONTINUATION_MAX_CONCURRENT` | `2` | `ge=1` |
| `continuation_debounce_seconds: int` | `NOUS_CONTINUATION_DEBOUNCE_SECONDS` | `20` | `ge=0` |
| `continuation_max_wait_seconds: int` | `NOUS_CONTINUATION_MAX_WAIT_SECONDS` | `120` | `ge=0` |
| `continuation_lease_seconds: int` | `NOUS_CONTINUATION_LEASE_SECONDS` | `900` | `ge=120` |
| `continuation_turn_timeout_seconds: int` | `NOUS_CONTINUATION_TURN_TIMEOUT_SECONDS` | `780` | `ge=60` |
| `continuation_max_attempts: int` | `NOUS_CONTINUATION_MAX_ATTEMPTS` | `3` | `ge=1` |
| `intention_proposal_ttl_hours: float` | `NOUS_INTENTION_PROPOSAL_TTL_HOURS` | `24` | `gt=0` |

Validators (`@model_validator(mode="after")`, next to `_validate_intentions_dependency`, `nous/config.py:3058`):

- `_validate_continuation_dependency`: `continuation_enabled and not intentions_enabled` → WARNING `"NOUS_CONTINUATION_ENABLED=true needs NOUS_INTENTIONS_ENABLED=true; continuation stays OFF."` and `object.__setattr__(self, "continuation_enabled", False)`. Runs AFTER `_validate_intentions_dependency` (declaration order), so an intentions flag forced off by the missing inbox also forces continuation off.
- `_validate_continuation_timing`: raises `ValueError` when `continuation_turn_timeout_seconds > continuation_lease_seconds - 60` (`"NOUS_CONTINUATION_TURN_TIMEOUT_SECONDS must be at least 60 s below NOUS_CONTINUATION_LEASE_SECONDS"`) or when `continuation_max_wait_seconds < continuation_debounce_seconds`. Hard errors, like `_validate_keepalive` (`nous/config.py:3070-3076`): a misconfigured lease is unsafe at any flag value, and these are cheap to get right.

Quiet hours reuse `heartbeat_quiet_start` / `heartbeat_quiet_end` (`nous/config.py:1667-1668`); the spec's note that they are UTC hours stands.

### 4.4 ContextKinds, `CONTEXT_POLICY` rows, `ExecutionContext` fields (PR-2a)

`nous/api/execution_context.py`:

```python
ContextKind = Literal[
    ..., "background",
    "continuation",      # F099: Nous's own turn on a background result (internal_only)
    "approved_action",   # F099: one owner-approved proposal, run with no model
]
```

Neither joins `FOREGROUND_KINDS` (line 34): `tool_policy.evaluate` returns `None` for foreground kinds (`nous/api/tool_policy.py:70`), which would skip the policy entirely.

New `ExecutionContext` fields (all defaulted, so every existing constructor call is unchanged):

```python
    # F099 Phase 2
    proposal_id: UUID | None = None      # approved_action: the proposal being run
    arrival_id: UUID | None = None       # continuation: the arrival this turn decides
    claim_token: UUID | None = None      # continuation: the claim this turn runs under
    spawn_blocked: bool = False          # continuation: the root is at its depth/spawn limit at claim time
```

`__post_init__` adds: `kind == "approved_action"` requires `proposal_id` and `declared_tools` of length 1; `kind == "continuation"` requires `authority == "internal_only"` and `intention_id` and `root_intention_id`. (A continuation context can never be built wide.)

`nous/api/tool_policy.py` rows:

```python
        "continuation": ContextPolicy(_LOCAL, spawn=frozenset({"spawn_task", "dag_create"})),
        # One declared tool (declared_tools=(tool,)); levels wide because the
        # proposed call is by definition outward or denylisted-local.
        "approved_action": ContextPolicy(_ALL, spawn=True),
```

The `declared_tools` check in `evaluate` (line 76-78) is what narrows `approved_action` to its one tool; the strict offered-set rule (§4.5) makes that refusal unconditional.

The spec's "`owner_approved` authority" is represented by `kind == "approved_action"`, not by a new `authority` value; `authority` stays `"owner"` (see open question 3).

### 4.5 `_offered_tools`, the denylist, and the strict path in `_authorize_tool_call` (PR-2a)

**Constants**, in `nous/api/tool_policy.py` (a leaf below the runner, importable by tests and by `heartbeat/dynamic.py`):

```python
# F099 section 4.4: tools an internal_only turn may use although their class
# is none/write. Denied because they schedule, persist policy, reach the
# host, or resolve the Brain's own records.
INTERNAL_ONLY_DENYLIST: frozenset[str] = frozenset({
    "schedule_task", "heartbeat_check_create", "heartbeat_check_manage",
    "create_censor", "learn_skill", "store_identity", "complete_initiation",
    "dag_manage", "push_surface", "compose_surface", "bash", "run_python",
    "spawn_sync", "resolve_decision", "resolve_decisions",
})
# Offered to continuation turns only; removed when ctx.spawn_blocked.
INTERNAL_ONLY_SPAWN_TOOLS: frozenset[str] = frozenset({"spawn_task", "dag_create"})
# Per-call rules (path and lineage) evaluated in _authorize_tool_call.
INTERNAL_ONLY_CHECKED_TOOLS: frozenset[str] = frozenset({"write_file", "cancel_task"})
# Logged with their root when called from a lineage (section 9).
INTERNAL_ONLY_LOGGED_TOOLS: frozenset[str] = frozenset({"web_fetch", "web_search"})


def internal_only_allowed(name: str, *, ctx: ExecutionContext) -> bool:
    """Whether an internal_only context may be OFFERED ``name``. Fails closed
    on an unclassified tool."""
    cls = tool_class(name)
    if cls is None or cls.side_effect not in ("none", "write"):
        return False
    if name in INTERNAL_ONLY_DENYLIST:
        return False
    if name in INTERNAL_ONLY_SPAWN_TOOLS:
        return ctx.kind == "continuation" and not ctx.spawn_blocked
    return True


def internal_only_call_violation(ctx: ExecutionContext, name: str, tool_input: Mapping[str, Any],
                                 *, workspace_dir: str) -> str | None:
    """The per-call rules: ``"external"`` (classify_side_effect rates the call
    external, e.g. a URL in run_python), ``"write_path"`` (write_file outside
    <workspace_dir>/intentions/<root_id>/), ``"foreign_cancel"`` (cancel_task on
    a task outside this root). None when the call is allowed."""
```

`cancel_task` lineage membership: the handler (`nous/api/tools.py:3507-3509`) takes a bare id; the rule needs the id's intention row. `internal_only_call_violation` is sync and cannot read rows, so for `cancel_task` it returns `"foreign_cancel"` only when the id is syntactically not a UUID; the real check happens in the handler. `cancel_task` is registered without `origin_aware` (`tools.py:3993`), so `_origin_args` does not reach it; `dispatch` gets its own injection line in the block at `tools.py:477-539`: `if ctx.root_intention_id is not None and name == "cancel_task": args = {**args, "_root_intention_id": str(ctx.root_intention_id), "_authority": ctx.authority}`, and the handler gains `_root_intention_id: str | None = None, _authority: str | None = None`. When `_authority == "internal_only"` it refuses unless the target's intention (`heart.intentions.get_for_source("subtask"|"schedule", uid)`) has `root_id == _root_intention_id`. With intentions off no context carries a root, so nothing is injected and parity holds. This is the one per-call rule that lives in a handler; the test "cancel_task on foreign work is refused" drives the handler.

**The helper** on `AgentRunner` (`nous/api/runner.py`), used by `_tool_loop` (replacing lines 2770-2798) and by `stream_chat` (replacing lines 2048-2060):

```python
    def _offered_tools(
        self,
        ctx: ExecutionContext,
        frame_id: str,
        *,
        is_subtask: bool,
        tool_filter: list[str] | None,
        refuse_active: bool,
        extra_tools: dict[str, tuple[dict, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """Exactly the tool definitions this iteration offers, in this order:
        frame tools (D5) → subtask exclusion (012.2) → tool_filter (F034.5) →
        F078 refuse denylist → internal_only narrowing (F099) → extra_tools.
        The internal_only step keeps only tools for which
        tool_policy.internal_only_allowed(name, ctx=ctx) is True; it applies to
        every kind whose ctx.authority == "internal_only", a heartbeat_check's
        tool_filter included (the intersection the spec names)."""
```

`stream_chat` passes `is_subtask=False, tool_filter=None, extra_tools=None`. Both loops derive `offered_names = frozenset(t["name"] for t in tools)` from the result, as today (`runner.py:2849`, `2060`). `_tool_loop` keeps rebuilding `tools` each iteration (line 2843) by calling the helper again, so a `ctx.spawn_blocked` set at claim time is honoured on every iteration; mid-turn limit changes are enforced by the store (§4.7 `IntentionLimitReached`), not by the offered set.

**The strict path** in `_authorize_tool_call` (`nous/api/runner.py:407-482`). The signature does not change. A new block runs FIRST, before the offered-set mode check at line 422 and before the `policy_mode == "off"` return at line 452:

```python
        strict = ctx.authority == AUTHORITY_INTERNAL or ctx.kind == "approved_action"
        if strict:
            violation: str | None = None
            if tool_name not in offered_names:
                violation = "not_offered"
            elif ctx.authority == AUTHORITY_INTERNAL:
                violation = tool_policy.internal_only_call_violation(
                    ctx, tool_name, tool_input, workspace_dir=self._settings.workspace_dir)
            if violation is not None:
                # force_block: refused whatever tool_offered_set_enforcement_mode
                # and tool_context_policy_mode say (section 3, "Tool enforcement").
                logger.warning("F099: refused %r in a %s turn (%s)", tool_name, ctx.kind, violation)
                self._log_f026_decision("harness_context_policy_violation",
                    {"tool_name": tool_name, "context_kind": ctx.kind,
                     "violation": f"internal_only:{violation}", "mode": "force_block"},
                    session_id=session_id)
                return Refusal(
                    f"Tool error: '{tool_name}' is not allowed in this turn ({violation}).",
                    "internal_only")
        if ctx.root_intention_id is not None and self._root_cancelled(ctx.root_intention_id):
            return Refusal("Tool error: this work was cancelled by the owner; stop.", "root_cancelled")
```

`self._root_cancelled` is `lambda _id: False` until 2e installs the view (§4.8 `set_cancelled_roots`). The workspace root is `self._settings.workspace_dir` (`nous/config.py:961`), NOT `self._workspace_dir`: that attribute exists only after `set_snapshot_store` (`runner.py:488-493`), so it is absent whenever compensation is off. After the strict block the existing offered-set and policy rules still run (an `internal_only` call that passes the strict block is also evaluated by `CONTEXT_POLICY["continuation"]` in whatever mode is configured; the strict block is the floor, not a replacement).

**Where `_log_f026_decision`'s event names matter:** the dashboard counts `harness_context_policy_violation`; a `mode: "force_block"` row is new and must be accepted by `nous/api/dashboard_queries.py`'s harness aggregation (grep `harness_context_policy_violation` there in 2a).

**Lineage logging (§9):** `dispatch` (`nous/api/tools.py:383`) logs at INFO `"F099: %s from lineage root %s (session %s)"` for `INTERNAL_ONLY_LOGGED_TOOLS` when `ctx.authority == internal_only`, before the handler runs.

**`_origin_args` gains authority** (`nous/api/tools.py:285-301`): `out["_origin_authority"] = ctx.authority` always. `IntentionSpec` gains `origin_authority: str | None = None`; `spec_from_tool_call` takes `origin_authority: str | None = None` and stores it; `prepare_intention` computes `authority = internal_only if (lineage parent row is internal_only) or (spec.origin_authority == internal_only) else owner` (final-review Minor 3: min(context, parent row)). `dag_create` (`nous/api/tools.py:5440-5451`) adds: `if kwargs.get("_origin_authority") == AUTHORITY_INTERNAL and wants_approval: return _tool_error("Error: an internal-only turn cannot create approval nodes; ask the owner through resolve_intention(decision='ask') instead.")`. The four spawn handlers accept `_origin_authority` as a keyword (they are `origin_aware`). Code-path `IntentionSpec`s (scheduler, work queue, app.act, REST) leave it `None` (owner).

**D7 note (final-review Minor 8):** when `resolve_wake_policy` returns a policy different from `spec.wake_policy` (`nous/brain/intentions.py:215-226`), the spawn tool's success text appends `" (wake_policy '<requested>' is not available from a <origin_kind> turn; recorded '<policy>')"`. `spec_from_tool_call` cannot know the outcome; the handlers read `prepared.wake_policy` via the created row's intention (`heart.intentions.get_for_source`) only when `wake_policy` was passed and intentions are on. One read, only on the flag-on path with an explicit argument.

**Orchestrator fail-closed guard (1.7 note):** `nous/dag/orchestrator.py:3419` and `:3524` replace `**({"intention": lineage} if isinstance(lineage, dict) else {})` with `**({"intention": lineage} if lineage is not None else {})` after asserting `lineage is None or isinstance(lineage, dict)` and deferring the node otherwise (`_defer_node(..., "lineage stamp unreadable")`, same shape as the failed-lookup defer added by Task 1.7).

**Lineage checks (`heartbeat_check` with a stamp):** `DynamicCheck._run_turn` (`nous/heartbeat/dynamic.py:339-362`) already builds the context with `authority`; `_offered_tools` intersects its `tool_filter` with the allowed set, so `heartbeat_check_create` (in `dynamic.ALLOWED_TOOLS`, line 77-85) is never offered to a lineage check, and the strict block refuses a forged call. That closes task-1.9 carry-over 1 without touching the handler.

### 4.6 The tools `resolve_intention` and `propose_action`; terminal extra tools

Both are **per-turn `extra_tools`** supplied by the continuation runner, never `dispatcher.register`ed (so they can never leak into another turn). Both get `TOOL_CLASSES` entries anyway (`nous/api/tool_classes.py:33`): `"resolve_intention": _WRITE, "propose_action": _WRITE` (the ledger and `is_keyed_tool` read the table; `tests/test_tool_classes.py` only checks registered names, so the entries are documentation plus ledger classification).

Schemas, in `nous/handlers/continuation_runner.py` as module constants (the same shape `build_submit_final_report_schema` produces, `nous/handlers/subtask_executor.py:33`):

```python
RESOLVE_INTENTION_SCHEMA = {
    "name": "resolve_intention",
    "description": "End this continuation by deciding what happens to the intention. Required: every continuation turn ends with exactly one call.",
    "input_schema": {
        "type": "object",
        "properties": {
            "decision": {"type": "string", "enum": ["continue", "revise", "drop", "report", "ask"]},
            "note": {"type": "string", "description": "One paragraph: what the result means and why this decision. For 'report', the text the owner reads, in Nous's voice. For 'ask', the question."},
            "progress": {"type": "boolean", "description": "Whether this arrival moved the goal forward (spawned work, changed the plan, or wrote memory)."},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        },
        "required": ["decision", "note", "progress", "confidence"],
    },
}
PROPOSE_ACTION_SCHEMA = {
    "name": "propose_action",
    "description": "Stage an action you may not take yourself (an outward send, a schedule, a shell command) for the owner to approve. Then resolve with decision='ask'.",
    "input_schema": {
        "type": "object",
        "properties": {
            "tool": {"type": "string"},
            "arguments": {"type": "object"},
            "rationale": {"type": "string"},
        },
        "required": ["tool", "arguments", "rationale"],
    },
}
```

Executors are closures over a per-turn `ArrivalState` (§4.8): `resolve_intention` validates the decision, refuses `continue`/`revise` when `state.limits.spawn_blocked` (re-derived from rows at call time: `continuation.root_limits(session, agent_id, root_id, settings)`), refuses any decision other than `ask` when `state.proposals` is non-empty, records the call on `state.resolution`, and returns `("Recorded.", False)`. `propose_action` (2d) validates `tool` is `dispatcher.is_registered` and `not tool_policy.internal_only_allowed(tool, ctx=ctx)`, inserts a `staged` proposal row carrying `ctx.claim_token`, appends its id to `state.proposals`, and returns the short id.

**Terminal mechanism** (`nous/api/runner.py:3084-3095`): a module constant

```python
# F099: only these extra_tools end the loop on success; every other extra tool
# (propose_action) returns to the model like a registered one.
TERMINAL_EXTRA_TOOLS: frozenset[str] = frozenset({"submit_final_report", "resolve_intention"})
```

and the line becomes `if not is_error and tool_name in TERMINAL_EXTRA_TOOLS: terminate_after_tool_results = True`. The `extra_tools` dict shape `{name: (schema, executor)}` is unchanged, so `nous/handlers/subtask_executor.py:178-181` (the one existing caller) needs no edit. A test pins that a successful non-terminal extra tool does not end the loop.

Continuation turns pass `force_tool_on_penultimate=None` (§4.4). The follow-up call (§4.5 item 5) is a second `run_turn` on the same session with the user message `CONTINUATION_FOLLOWUP_PROMPT` and the same `extra_tools`, under the same claim; it counts toward the turn's token total, not as a new attempt.

### 4.7 The store module: `nous/brain/continuation.py` (2b skeleton; 2c, 2d, 2e fill it)

Module-level, session-taking functions (the `intentions.py` style, so one monkeypatch target per operation), plus a thin `ContinuationStore` for callers without a transaction. All take `(session, agent_id, …)` unless noted. Constants:

```python
CONTINUATION_RUNNER_READY: bool = False          # flipped to True in PR-2e (section 2)
INTENT_SESSION_PREFIX = "intent-"                 # session id of a root's thread: f"intent-{root_id}"
SOURCE_INTENTION_REPORT = "intention_report"      # inbox source kind
MSG_REPORT, MSG_QUESTION, MSG_PROPOSAL = "REPORT", "QUESTION", "PROPOSAL"
CLOSE_DELIVERED, CLOSE_RESOLVED, CLOSE_CANCELLED, CLOSE_EXPIRED = "delivered", "resolved", "cancelled", "expired"
CLOSE_FALLBACK_REPORT, CLOSE_FAILED_REPORT = "fallback_report", "failed_report"
OUTCOME_RESOLVED, OUTCOME_FALLBACK, OUTCOME_FAILED = "resolved", "fallback_report", "failed_report"
DECISIONS = ("continue", "revise", "drop", "report", "ask")
PROPOSAL_TERMINAL = frozenset({"executed", "failed", "rejected", "expired", "cancelled"})
OPEN_STATES = ("pending", "result_ready", "deciding", "awaiting_owner")
```

Dataclasses:

```python
@dataclass(frozen=True, slots=True)
class RootLimits:        # derived from rows, never counted
    depth: int; spawns: int; turns: int; tokens: int; stalls: int
    spawn_blocked: bool  # depth >= max_depth or spawns >= max_spawns
    escalate: str | None # 'budget_turns' | 'budget_tokens' | 'budget_stall' | 'limit_depth' | 'limit_spawns' | None

@dataclass(frozen=True, slots=True)
class Claim:
    root_id: UUID; claim_token: UUID; intentions: tuple[Intention, ...]
    deepest: Intention                       # section 4.4: max depth, then earliest created_at, then lowest id
    inbox_rows: tuple[ResultInbox, ...]      # undelivered rows keyed by any claimed intention, read after the claim committed

@dataclass(frozen=True, slots=True)
class Resolution:
    decision: str; note: str; progress_claimed: bool; confidence: float

@dataclass(frozen=True, slots=True)
class ArrivalCommit:                         # what commit_arrival wrote (section 4.14)
    arrival_id: UUID; n: int; next_states: dict[UUID, str]; decision_record_id: UUID | None; report_ids: tuple[UUID, ...]

@dataclass(frozen=True, slots=True)
class ResultRecorded:                        # record_result
    inbox_id: UUID | None; inserted: bool; state_after: str; reopened: bool; reported: bool; intention_id: UUID; root_id: UUID

@dataclass(frozen=True, slots=True)
class RollbackReport:                        # rollback_at_startup
    closed: int; rerouted_rows: int; expired_proposals: int; pushed_raw: int

@dataclass(frozen=True, slots=True)
class SweepReport:                           # ContinuationRunner.run_once
    released: int; expired_roots: int; expired_proposals: int; pushed: int; launched: tuple[UUID, ...]; next_due: datetime | None

@dataclass(frozen=True, slots=True)
class ProposalExecution:                     # decide_proposal / execute_approved_proposal
    proposal_id: UUID; state: str; result: str | None; error: str | None; woke_arrival: bool

@dataclass(frozen=True, slots=True)
class AnswerRecorded:                        # answer_question
    question_id: UUID; arrival_id: UUID; intention_ids: tuple[UUID, ...]; woke_arrival: bool

@dataclass(frozen=True, slots=True)
class CancelOutcome:                         # cancel_root
    root_id: UUID; already_cancelled: bool; cancelled_intentions: int; cancelled_subtasks: int
    cancelled_dags: int; cancelled_proposals: int; deactivated_schedules: int; turn_stopped: bool
```

Functions (PR in brackets):

```python
async def record_result(session, agent_id, *, intention_id, source_kind, source_id, msg_type, title, body,
                        source_generation=0, correlation_id=None, created_at=None, settings) -> ResultRecorded   [2b]
    # The one Phase 2 writer for continue results. One transaction:
    # policy=='continue' and state in (pending, closed-with-open-root): INSERT inbox row with
    # channel=NULL, session_id=NULL, intention_id → UPDATE state='result_ready', result_at=now (T4/T6);
    # state=='awaiting_owner' or 'deciding': INSERT only (held) (section 4.3 items 2-3);
    # closed root or non-continue policy: INSERT an intention_report REPORT row carrying the raw
    # result, keyed to origin_channel or the default chat; state unchanged.
    # Returns ResultRecorded(inserted: bool, state_after: str, reopened: bool, reported: bool).
    # Emits nothing: the caller emits intention.result_ready after commit (section 4.13).

async def close_delivered(session, agent_id, source_kind, source_id, *, with_result=True) -> UUID | None     [2b]
    # Phase 2's T3: intentions.close_for_source(..., reason=CLOSE_DELIVERED). Used by every writer
    # for none/remember/report/container when settings.continuation_enabled; legacy otherwise.

async def insert_report(session, agent_id, *, kind, title, body, channel, intention_id, root_id, arrival_id=None,
                        proposal_id=None, push_after=None) -> UUID                                        [2b]
    # An intention_report row (REPORT/QUESTION/PROPOSAL). Returns its id (= source_id = report_id).
    # channel is origin_channel or f"telegram:{settings.telegram_chat_id}"; never NULL (section 4.3 item 4).

async def rollback_at_startup(database, settings, *, telegram_push) -> RollbackReport                     [2b]
    # Section 4.3 item 6, including task-1.9 carry-over 2 (pending with a terminal source).

async def claim_root(session, agent_id, root_id, *, token, debounce_s, max_wait_s) -> Claim | None       [2c]
    # The section 4.5 claim SQL, verbatim, after SELECT ... FOR UPDATE on the root row.
async def eligible_roots(session, agent_id, *, debounce_s, max_wait_s) -> list[tuple[UUID, datetime]]    [2c]
    # Roots with a result_ready continue intention and no deciding one, with the instant each becomes
    # claimable (the runner sleeps until the earliest).
async def release_stale_claims(session, agent_id, *, lease_s) -> list[UUID]                              [2c]  (T8)
async def root_limits(session, agent_id, root_id, *, settings) -> RootLimits                              [2c]
    # tokens = Σ subtasks.tokens_in+tokens_out over the root's subtask intentions with dag_node_id IS NULL
    #        + Σ execution_dags.tokens_consumed over its dag intentions + Σ arrivals.tokens_in+out;
    # turns = count(arrivals where gate_reason IS NULL); spawns = count(intentions where root_id=:root and depth>0);
    # depth = max(depth); stalls = length of the trailing run of arrivals with progress=false.
async def gate(session, agent_id, claim, *, settings, plan_outcome_of) -> str | None                     [2c]
    # Section 4.5 item 3 in order: cancelled/expired → 'cancelled'/'expired'; past deadline of the deepest
    # claimed intention → 'past_deadline'; root_limits.escalate; the Plan decision of the deepest claimed
    # intention resolved superseded/noise → 'plan_resolved'. Returns the gate_reason or None.
async def commit_arrival(session, agent_id, claim, *, resolution, outcome, gate_reason=None, tokens, brain,
                         settings, report_text=None) -> ArrivalCommit | None                            [2c]
    # Section 4.5 item 6 and section 4.14. Every UPDATE carries WHERE state='deciding' AND claim_token=:token;
    # None when the fence rejected (the lease was released).
async def fail_attempt(session, agent_id, claim, *, max_attempts, settings) -> str                         [2c]  (T8 or T12)
    # attempts+1 on every claimed intention; at the cap, REPORT rows with the raw results and close failed_report.
async def expire_roots(session, agent_id, *, ttl_hours, settings) -> list[UUID]                            [2c]  (T14)
async def repair_missing_results(session, agent_id, *, settings, limit) -> int                             [2c]
    # continue/report intentions whose source is terminal (cancelled included) and that have no inbox row for
    # the source's current generation. A continue intention is repaired through record_result (NULL keys; a
    # cancelled subtask gets a FAILURE row "Outcome: cancelled"); a report intention through the writer's
    # non-continue branch: today's routing keys plus close_delivered (T3). The Phase 2 counterpart of
    # IntentionClosePass for those two policies.
async def arrival_is_terminal(session, agent_id, arrival_id) -> bool                                       [2c, extended 2d]
    # True when every QUESTION row of the arrival is answered (its answer row exists) or past its deadline,
    # AND (2d) every proposal of the arrival is in PROPOSAL_TERMINAL.
async def wake_arrival(session, agent_id, arrival_id) -> list[UUID]                                        [2c]  (T5)
async def stage_proposal(session, agent_id, *, intention_id, root_id, claim_token, tool, arguments, rationale) -> UUID  [2d]
async def publish_staged(session, agent_id, *, arrival_id, claim_token, deadline) -> list[UUID]           [2d]
    # staged → pending for the claim's proposals, inside the fenced commit; inserts their PROPOSAL rows.
async def expire_staged(session, agent_id, *, claim_token) -> int                                          [2d]
    # staged → expired, on the failure path and on lease release.
async def decide_proposal(session, agent_id, proposal_id, *, approve: bool, actor) -> str                  [2d]
    # pending → approved | rejected (WHERE state='pending'); returns the new state or raises ProposalNotPending.
async def claim_execution(session, agent_id, proposal_id) -> IntentionProposal | None                      [2d]
    # approved → executing WHERE state='approved' AND NOT EXISTS (root with root_cancelled_at/root_expired_at).
async def finish_execution(session, agent_id, proposal_id, *, ok, result) -> None                          [2d]  (executing → executed|failed)
async def expire_proposals(session, agent_id) -> list[UUID]                                                [2d]  (pending past deadline → expired)
async def record_answer(session, agent_id, question_id, *, text, actor) -> UUID                            [2d]
    # Inserts the answer as the next continue result of every intention of the question's arrival
    # (section 4.9, "Answers"), then wakes the arrival when it is terminal.
async def cancel_root(session, agent_id, root_id, *, reason, actor) -> CancelOutcome                       [2e]  (T13)
async def cancelled_root_ids(session, agent_id, *, since) -> list[UUID]                                    [2e]
```

`IntentionLimitReached(ValueError)` is added to `nous/brain/intentions.py` in 2c and raised by `prepare_intention` when `spec.parent_id` is set and the new row would exceed `max_depth` (from the parent's `depth + 1`, exact) or the root's `max_spawns` (a `count(*)` of the root's rows with `depth > 0`, run inside the spawning transaction). Two concurrent spawns under one root both hold the root `FOR SHARE`, so the count can overshoot by the concurrency width; that is accepted and documented, since the gate escalates on the next claim either way and the depth limit, which is exact, bounds the chain. `prepare_intention` receives the limits through `IntentionSpec.limits: tuple[int, int] | None` (max_depth, max_spawns), set by the four spawn handlers from settings when `continuation_enabled`; code paths leave it `None`. The same route carries the TTL: `IntentionSpec.ttl_hours: float | None`, set by all eight spawn sites via `intentions.ttl_for(settings)` (returns `settings.intention_root_ttl_hours` when `continuation_enabled`, else `None`); `prepare_intention` writes `deadline = created + ttl` for a root and `min(parent.deadline, created + ttl)` for a child, NULL when `ttl_hours` is None. (The stores have no settings: `SubtaskManager.__init__` is `(database, agent_id)`, `nous/heart/subtasks.py:42`.)

`IntentionClosePass` exclusion (2b): `close_finished_sources` gains `exclude_policies: tuple[str, ...] = ()`; `IntentionClosePass.run` passes `(WAKE_CONTINUE, WAKE_REPORT)` when `settings.continuation_enabled`. Without it the pass races the Phase 2 writer and closes a `continue` result as `legacy` with no inbox row (`nous/heart/result_reconciler.py:248-253`, `nous/brain/intentions.py:484-535`).

### 4.8 The runner: `nous/handlers/continuation_runner.py` (PR-2c; 2d, 2e add methods)

```python
class ContinuationRunner:
    """One loop per process: sleeps until the next root is claimable, claims under a
    concurrency slot, runs the arrival inside asyncio.wait_for(turn_timeout), commits
    under the claim's fence. Registered with the Fix-Z guard (cancel_requested())."""

    def __init__(self, *, database: Database, settings: Settings, runner: AgentRunner, heart: Heart,
                 brain: Brain, bus: EventBus | None, dispatcher: ToolDispatcher,
                 publisher: OwnerPublisher | None = None,          # 2d
                 cancel_dag: Callable[[UUID, str], Awaitable[None]] | None = None,  # 2e: orchestrator.cancel_dag
                 ) -> None: ...

    async def start(self) -> None          # release_stale_claims, then create_task(self._loop(), name="continuation-runner")
    async def stop(self) -> None           # cancel the loop and every running turn task; await them
    def wake(self) -> None                 # set the asyncio.Event the loop sleeps on (bus handler and the reconciler pass call it)
    async def on_result_ready(self, event: Event) -> None   # bus handler for intention.result_ready → wake()
    async def run_once(self) -> SweepReport  # one sweep: release leases, expire roots, expire proposals (2d), push due rows (2d), claim and launch eligible roots; the loop and the tests call it
    async def run_arrival(self, root_id: UUID) -> ArrivalCommit | None   # claim → gate → turn → follow-up → commit; tests drive it directly
    @property
    def running_roots(self) -> frozenset[UUID]

    # 2d
    async def execute_approved_proposal(self, proposal_id: UUID) -> ProposalExecution
    async def decide_proposal(self, proposal_id: UUID, *, approve: bool, actor: str) -> ProposalExecution
    async def answer_question(self, question_id: UUID, *, text: str, actor: str) -> AnswerRecorded
    # 2e
    async def cancel_root(self, root_id: UUID, *, reason: str, actor: str) -> CancelOutcome
    def root_is_cancelled(self, root_id: UUID) -> bool   # the in-process view for _authorize_tool_call
```

**The turn** (`run_arrival`): session `f"{INTENT_SESSION_PREFIX}{root_id}"`, `runner.run_turn(session_id, user_message=<built from rows>, skip_episode=True, is_subtask=False, is_background=True, max_tool_calls=settings.subtask_tool_call_limit, model_override=settings.background_model, extra_tools={"resolve_intention": ..., "propose_action": ... (2d)}, force_tool_on_penultimate=None, context=ExecutionContext(kind="continuation", session_id=..., authority="internal_only", intention_id=claim.deepest.id, root_intention_id=root_id, arrival_id=..., claim_token=..., spawn_blocked=limits.spawn_blocked))`. `is_subtask` must be `False`: the 012.2 strip (`runner.py:2779-2780`) would remove `spawn_task` before `_offered_tools`' internal-only step decides whether to offer it. After the turn (and the follow-up) the runner calls `runner.end_conversation(session_id)` in a `finally`, so no conversation state accumulates; `end_conversation` (`nous/api/runner.py:1537-1608`) skips the reflection LLM call when `session_id.startswith(INTENT_SESSION_PREFIX)`.

`pre_turn` gains `context_kind: str | None = None` (`nous/cognitive/layer.py:471-494`); `run_turn` passes `**({"context_kind": _ctx.kind} if _ctx.kind == "continuation" else {})` (the F098 `channel` pattern, `runner.py:1272-1274`). For `"continuation"`, `pre_turn` skips `_inject_result_inbox` (line 830-833) and the deliberation start (line 884-894). `_inject_result_inbox` also returns early for `session_id.startswith("intent-")`.

**Turn input** (§4.5 item 4), one builder `build_arrival_prompt(claim, lineage_arrivals, children_of_failed_attempt, limits, settings) -> str` in the runner module. Results are rendered with `format_inbox_messages` (`nous/heart/result_inbox.py:534`) so the `<result_message>` framing and `_neutralize` are shared, not copied.

**Failure** (§4.5 item 7): a raise, a `TimeoutError` from `wait_for`, or a fence rejection → `fail_attempt` (`attempts + 1`, `claim_token = NULL`, `state = result_ready` or, at the cap, `failed_report`) and `expire_staged` (2d). A `CancelledError` from `stop()` or `cancel_root` re-raises after releasing the claim.

**`main.py` wiring (2e only):** after the result reconciler block (`nous/main.py:1039-1056`):

```python
    continuation_runner = None
    if settings.continuation_enabled:
        from nous.handlers.continuation_runner import ContinuationRunner
        continuation_runner = ContinuationRunner(database=database, settings=settings, runner=runner, heart=heart,
                                                 brain=brain, bus=bus, dispatcher=dispatcher, publisher=publisher,
                                                 cancel_dag=(dag_orchestrator.cancel_dag if dag_orchestrator else None))
        runner.set_cancelled_roots(continuation_runner.root_is_cancelled)
        if bus is not None:
            bus.on("intention.result_ready", continuation_runner.on_result_ready)
        await continuation_runner.start()
    components["continuation_runner"] = continuation_runner
```

It must be constructed after `dag_orchestrator` (line 1307) and the A2UI block (1445-1477), so it sits at the end of component construction; `shutdown_components` (line 1519) stops it before the heartbeat runner. `build_reconciler` (`nous/heart/result_reconciler.py:285`) takes `continuation: ContinuationRunner | None = None` and registers `ContinuationWakePass(database, settings, wake=continuation.wake)` (name `"continuation"`) when the flag is on: it runs `repair_missing_results` then calls `wake()`; it never runs a turn (the pass timeout is 30 s, line 51). `AgentRunner.set_cancelled_roots(view: Callable[[UUID], bool])` stores `self._root_cancelled`.

The runner's loop body uses the Fix-Z shape of `_result_reconciler_loop` (`nous/main.py:205-217`): `except asyncio.CancelledError: if cancel_requested(): break; logger.exception(...)`.

### 4.9 Inbox and routing contract (PR-2b unless marked)

`ResultInboxStore.insert` (`nous/heart/result_inbox.py:145-187`) gains `session: AsyncSession | None = None` and the new columns `arrival_id`, `proposal_id`, `push_after`; with a session it executes in the caller's transaction and does not commit. Its conflict target becomes `index_elements=["source_kind", "source_id", "source_generation", "agent_id"]` (the UNIQUE's column order, §4.2). The `has_row` lookups in `InboxSubtaskPass` (`result_reconciler.py:95`) and `InboxDagPass` (`:178-182`) are unchanged and keep their index prefix.

**Writer → bus seam.** `record_subtask_result` and `record_dag_result` carry no bus, nor do the reconciler passes. `ResultInboxStore` gains `set_bus(bus: EventBus | None)`, called once in `main.py` next to `ResultInboxDagListener(...).register(bus)` (`nous/main.py:1301-1305`); `record_result` returns `ResultRecorded`, and the writer that called it emits `intention.result_ready` through `store.bus` after its commit when `state_after == "result_ready"` and the bus is set. With no bus the hint is simply not sent and the runner learns of the row from the 60 s `ContinuationWakePass` plus `eligible_roots` (§4.7); correctness never depends on the event.

Writers, when `settings.continuation_enabled`:

- `record_subtask_result` (`result_inbox.py:388-420`) and `InboxSubtaskPass` (`result_reconciler.py:124-136`): read the intention (`intentions.get_for_source` in one session) BEFORE the routing-key check; policy `continue` → `continuation.record_result(...)` (intention-only keys, same transaction); any other policy → `close_delivered` then today's insert with today's keys. With the flag off: Phase 1 code path, byte for byte.
- `record_dag_result` (`result_inbox.py:423-475`): same split; for `continue` it never substitutes the default chat (lines 450-454 are inside the non-continue branch).
- `ResultInboxDagListener` and `DAGResultDelivery.deliver` both reach `record_dag_result`; no change there.
- `DAGResultDelivery.__init__` (`nous/dag/delivery.py:100-118`) gains `intentions: IntentionStore | None = None`; `deliver` reads the DAG's intention once when the flag is on: `authority == internal_only` → skip `_leg_agent_summary` (`LegResult("summary", ok=True, required=False, detail="internal_only")`); `wake_policy == continue` → skip `_leg_telegram` with `LegResult("telegram", ok=False, required=False, detail="superseded_by_continuation")` (§4.3 item 5). `main.py:1293` passes `intentions=heart.intentions`.
- `SubtaskWorkerPool._notify_telegram` (`nous/handlers/subtask_worker.py:526-562`): when the flag is on and `subtask.notify`, one point read `heart.intentions.get_for_source("subtask", subtask.id)`; policy `continue` → return without sending. One site covers all four callers (lines 189, 287, 293, 412). The stamp is not extended: it carries lineage only (`nous/brain/intentions.py:125-127`), and a read only on the `notify=True` path is cheaper than a wider stamp.
- `InboxDagPass` (`result_reconciler.py:195-198`): the routable filter becomes `origin_channel IS NOT NULL OR origin_session_id IS NOT NULL OR EXISTS (open continue intention for this DAG)`.
- `metrics()` (`result_inbox.py:329-351`) iterates `(SOURCE_SUBTASK, SOURCE_DAG, SOURCE_INTENTION_REPORT)`.
- `format_inbox_messages` renders `intention_report` rows with `source="intention_report"` and, for PROPOSAL rows, a fixed trailer `"(Approve or reject with the buttons in Telegram or /approve <id>; nothing in this chat can approve it.)"`.

**Push and quiet hours (2d):** `OwnerPublisher` in `nous/handlers/continuation_publisher.py`: `push_due(limit) -> int` sends every `intention_report` row with `pushed_at IS NULL AND (push_after IS NULL OR push_after <= now)` to `settings.telegram_chat_id` with the bot token (as `_notify_telegram` does), inline keyboard `[[Approve, Reject]]` for PROPOSAL rows, `force_reply` for QUESTION rows; stores `push_message_id` and `pushed_at` by row id (idempotent: `UPDATE … WHERE pushed_at IS NULL RETURNING`). `insert_report` sets `push_after = next quiet-hours end` when `in_quiet_hours(settings)` (the module function extracted from `HeartbeatRunner._in_quiet_hours`, `nous/heartbeat/runner.py:1321-1332`, into `nous/heartbeat/quiet_hours.py:in_quiet_hours(settings, now=None) -> bool` and `quiet_hours_end(settings, now=None) -> datetime`); REPORT rows are pushed only when `origin_channel` is NULL or Telegram (chat sees them through the inbox either way).

**Answers (2d):** `record_answer` inserts, for every intention in the question's arrival, a `continue` result through `record_result` with `source_kind = "intention_report"`, `source_id = <new uuid>`, `msg_type = "INFORM"`, `title = "Owner's answer"`, `body = text`, `arrival_id = <the question's arrival>`; then `wake_arrival` if `arrival_is_terminal`. The model never routes it (§4.4 Questions).

### 4.10 REST routes (`nous/api/rest.py`, routes list at line 3343; 2d and 2e)

All under the existing no-auth LAN posture (§9). Ids accept a full UUID or a hex prefix of at least 8 characters that is unique for the agent; an ambiguous prefix is 400.

| Route | PR | Request | 200 response | Errors |
|---|---|---|---|---|
| `GET /intentions?state=open\|all&limit=20` | 2e | | `{"roots": [RootView]}` | 400 bad limit |
| `GET /intentions/{root_id}` | 2e | | `RootView` | 404 |
| `POST /intentions/{root_id}/cancel` | 2e | `{"reason": str?}` | `{"root_id", "already_cancelled": bool, "cancelled_intentions": n, "cancelled_subtasks": n, "cancelled_dags": n, "cancelled_proposals": n, "deactivated_schedules": n, "turn_stopped": bool}` | 404 |
| `GET /intentions/proposals?state=pending&limit=20` | 2d | | `{"proposals": [ProposalView]}` | |
| `POST /intentions/proposals/{id}/decide` | 2d | `{"decision": "approve"\|"reject", "actor": str?}` | `{"proposal_id", "state": "rejected"\|"executed"\|"failed", "result": str?}` (approve executes inline, bounded by `settings.tool_timeout`) | 400 bad decision; 404; 409 `{"state": current}` when not `pending` |
| `POST /intentions/questions/{id}/answer` | 2d | `{"text": str, "actor": str?}` | `{"question_id", "arrival_id", "woke": bool}` | 400 blank; 404; 409 already answered |
| `POST /intentions/questions/answer` | 2d | `{"chat_id": int, "message_id": int, "text": str, "actor": str?}` (a Telegram reply, resolved by `push_message_id`) | as above | 404 no question for that message |

`RootView = {"id", "intent", "state", "wake_policy", "authority", "origin_kind", "origin_channel", "created_at", "deadline", "root_cancelled_at", "root_expired_at", "limits": RootLimits as dict, "lineage": [{"id", "parent_id", "depth", "source_kind", "source_id", "state", "wake_policy", "intent"}], "arrivals": [{"id", "n", "decision", "note", "progress", "outcome", "gate_reason", "decided_at"}], "open_proposals": [ProposalView], "open_questions": [{"id", "arrival_id", "body", "created_at"}]}`.
`ProposalView = {"id", "short_id", "root_id", "intention_id", "arrival_id", "tool", "arguments", "rationale", "state", "deadline", "decided_at", "decided_by", "result"}`.

`create_app` (line 78) needs `continuation_runner: Any | None = None`; routes answer 503 `{"error": "continuation is not running"}` when it is `None`, except the two GETs, which read rows directly.

### 4.11 Telegram (`nous/telegram_bot.py`, 2d; `/intentions` in 2e)

The bot is a separate process that proxies REST; it never approves on its own and never talks to the model for these. Pushes come from the server-side `OwnerPublisher` (§4.9).

- `_handle_update` (line 537-541) gains a branch before the `message` check: `callback_query = update.get("callback_query")`; access control reads `callback_query["from"]["id"]` against `allowed_users`; the data is parsed by `parse_callback(data) -> tuple[str, str, str] | None` and anything that does not parse is answered with `answerCallbackQuery(text="unknown button")`.
- **Callback data format** (≤ 64 bytes): `f099:p:<proposal_id hex, 32 chars>:a` (approve) and `…:r` (reject). Handling: `answerCallbackQuery(text="Approving…"|"Rejecting…")` → `POST /intentions/proposals/{id}/decide` → `editMessageReplyMarkup` to remove the buttons and `editMessageText` appending `"\n\n✅ Approved and executed."`, `"\n\n❌ Rejected."`, or the 409's current state.
- **Command grammar** (parsed in code, before anything reaches `/chat`):
  - `/approve <id>` and `/reject <id>`: `<id>` is a proposal id (full or ≥8-hex prefix) → the decide route.
  - `/answer <id> <text…>`: `<id>` is a question id → `POST /intentions/questions/{id}/answer`; blank text → usage reply.
  - a plain reply to a bot message (`message.reply_to_message.message_id` present): `POST /intentions/questions/answer` with `{chat_id, message_id, text}`; on 404 the message falls through to ordinary chat (it was a reply to something else).
  - `/intentions` (2e): `GET /intentions?state=open` rendered as a list with ids; `/cancel_intention <root_id>` → the cancel route.
- The bot's `_tg` is GET with params (line 926-933); `reply_markup` is passed as a JSON string, which the Bot API accepts.

### 4.12 The `proposal:{id}` idempotency scope (`nous/api/idempotency.py`, 2d)

`_scope` (line 32-45) gains, first: `if ctx.kind == "approved_action" and ctx.proposal_id is not None: return f"proposal:{ctx.proposal_id}"`. The proposal row's `ledger_key` stores the computed key after `_open_for_call`. The primary at-most-once fence is `claim_execution` (`approved → executing`); the key is the second fence for keyed sends, so a crash between the claim and the send cannot be followed by a duplicate from a retry nobody runs (there is no automatic re-run, §4.4 item 5).

`execute_approved_proposal` runs the stored call through `AgentRunner.execute_single_call(ctx, tool_name, tool_input) -> tuple[str, bool]`, a method extracted in 2d from the non-streaming per-call block (`nous/api/runner.py:3077-3266`: `_open_for_call` → snapshot → `dispatch` → `_ledger_close` → `_after_compensable_call`) with `offered_names = frozenset({tool_name})` and `ctx = ExecutionContext(kind="approved_action", session_id=f"proposal-{id}", proposal_id=id, declared_tools=(tool,), root_intention_id=root_id, intention_id=intention_id)`. `_authorize_tool_call` runs first (strict path, §4.5). `stream_chat` keeps its own copy of the block.

### 4.13 Bus event names (`nous/events.py` `Event(type=…, data=…)`)

| Event | Data | Emitted by | PR |
|---|---|---|---|
| `intention.result_ready` | `{intention_id, root_id, agent_id}` | the writer, after `record_result`'s transaction commits (T4/T5/T6) | 2b (emitted), 2c (consumed) |
| `intention.arrival_decided` | `{arrival_id, root_id, decision, outcome, gate_reason}` | `run_arrival` after the fenced commit | 2c |
| `intention.proposal_pending` | `{proposal_id, root_id, arrival_id, tool}` | the fenced commit (`publish_staged`) | 2d |
| `intention.proposal_decided` | `{proposal_id, state, actor}` | `decide_proposal` / `execute_approved_proposal` / `expire_proposals` | 2d |
| `intention.root_cancelled` | `{root_id, reason, actor}` | `cancel_root` | 2e |
| `intention.root_expired` | `{root_id}` | `expire_roots` | 2c |

All are hints (the bus drops on `QueueFull`, `nous/events.py:185-189`); rows are the truth and the sweep is the backstop.

### 4.14 Arrival commit contents (`commit_arrival`, one transaction, every UPDATE fenced on `state = 'deciding' AND claim_token = :token`)

1. The `intention_arrivals` row: `n = 1 + count(arrivals of root)`, `intention_ids = claim ids`, `inbox_ids = ids of the rows the turn was shown`, `claim_token`, `decision`, `note`, `progress_claimed`, `progress` (verified: the arrival spawned work (an intention with `parent_id` in `intention_ids`), changed a plan (a `revise` decision), or wrote memory (a `learn_fact`/`ingest_document` call in the turn's tool results); a `true` that fails the check is stored `false`), `confidence`, `gate_reason`, `tokens_in`, `tokens_out`, `outcome`, `decided_at = now`.
2. One Brain decision via `brain.record(RecordInput(description=f"{decision}: {note}"[:…], confidence, category="process", stakes="low", context=json.dumps({"root_id", "intention_ids", "arrival_n"}), session_id=f"intent-{root_id}", tags=["f099", decision]), session=session)` (`nous/brain/brain.py:353-372` supports a caller-owned session); its id → `decision_record_id`. Gate arrivals (`gate_reason` set) also get one, so "every arrival decision is recorded" (G5).
3. `delivered_at = now, delivered_session_id = f"intent-{root_id}"` on every row in `inbox_ids` (`WHERE delivered_at IS NULL`).
4. Each claimed intention's next state: `continue`/`revise`/`drop`/`report`/fallbacks → `closed` with `close_reason = resolved | fallback_report | failed_report`, `closed_at = now`; `ask` → `awaiting_owner`; any decision but `ask` for an intention with undelivered rows not in `inbox_ids` → `result_ready` instead (T11). `claim_token = NULL`, `claimed_at = NULL`, `updated_at = now`.
5. For `report`, and for both fallbacks: `insert_report(kind=REPORT, …)`; ids → `report_ids`. For `ask`: `insert_report(kind=QUESTION, arrival_id=…)` when the note is a question, plus (2d) `publish_staged` → PROPOSAL rows for each staged proposal of this `claim_token`, with `deadline = now + proposal_ttl`.
6. Post-commit (outside the transaction): `intention.arrival_decided`, `intention.proposal_pending` per proposal, `publisher.push_due()` kick.

Nothing in the commit spawns work: spawns happened during the turn through the normal spawn tools and are already rows with `parent_id = claim.deepest.id`.

### 4.15 Phase 1 carry-overs, where each lands

| Carry-over | Source | PR |
|---|---|---|
| Child authority = min(context, parent row) | final-review Minor 3 | 2a (`_origin_authority`, §4.5) |
| A D7-blocked `continue` is invisible to the model | final-review Minor 8 | 2a (§4.5, D7 note) |
| `heartbeat_check_create` from a lineage creates an unstamped check | task-1.9 item 1 | 2a (offered-set intersection, §4.5) |
| Rollback sweeps `pending` with a terminal source | task-1.9 item 2 | 2b (`rollback_at_startup`) |
| NULL deadlines mean "no deadline" | task-1.9 item 3 | 2c (`gate`: a NULL deadline never trips `past_deadline`; `expire_roots` uses `created_at + ttl` for roots with NULL deadline) |
| I4 `report` close-at-write as `delivered`, in the inbox insert's transaction; `test_a_report_intention_also_closes_as_legacy_in_phase_1` becomes flag-conditional | task-1.9 item 4 | 2b (T3) |
| Cancelling a container: `root_cancelled_at` on the container row, then `Schedule.active = False`, in that order, in one transaction | task-1.9 item 5; final-review "declined" items | 2e (`cancel_root`) |
| Orchestrator `isinstance(lineage, dict)` fails open | 1.7 note; final-review "declined" | 2a (§4.5) |
| `DynamicCheck.signature()` excludes `_intention` | 1.7 note | not needed: stamps never change; recorded as accepted |
| `record_dag_result` closes without a terminal-status guard | 1.8 follow-up | 2b (one-line guard in `record_dag_result`: `status in TERMINAL_DAG_STATUSES` or return) |
| Intentions recorded while on stay `pending` after the flag is off | 1.8 note | 2b (rollback) |
| `DAGStore.create` holds the root `FOR SHARE` during the node build | 1.3 note | constraint on 2c: `IntentionLimitReached` counting adds one indexed `count(*)`; nothing slower goes there |
| Ledger view's context-kind list | spec §4.7 | 2a adds `continuation` and `approved_action` to `dashboard-app/src/views/Ledger.svelte:23` (a one-line edit; the Phase 3 dashboard is separate) |

---

## 5. Open questions (with the recommended answer)

1. **What does `POST /intentions/{id}/answer` address?** The spec (§4.4 Questions) writes `{id}` as the intention. But the claim SQL blocks only on a `deciding` row, so one root can hold two arrivals in `awaiting_owner` (two result_ready intentions claimed at different times), each with its own question. **Recommended:** the route addresses the question row (`POST /intentions/questions/{id}/answer`, §4.10), and `/answer <id>` takes the question id; the Telegram reply path resolves by `push_message_id`. The intention-addressed form is dropped.
2. **Close reason for `none`/`remember` with intentions on and continuation off.** §4.1 Closing says `'legacy'` in Phase 1 and `'delivered'` in Phase 2, but does not say whether "Phase 2" means the code or the flag. **Recommended:** `delivered` only when `continuation_enabled`; `legacy` otherwise. Phase 1's closing tests then keep their literals with the flag off, and the week of Phase 1 data is comparable before and after the 2b deploy.
3. **The `owner_approved` authority.** §4.4 item 5 names it, but `AUTHORITIES` (`nous/brain/intentions.py:46`) feeds both the row CHECK and `lineage_from_stamp` (`execution_context.py:156`); adding a third value would let a stamp claim it. **Recommended:** no new authority value; `kind == "approved_action"` with `declared_tools = (tool,)` is the representation, `authority` stays `"owner"`, and the spec wording is read as the kind.
4. **Where the TTL and the limits enter `prepare_intention`.** The spec says a root gets `created + root TTL` but the stores have no settings. **Recommended:** `IntentionSpec.ttl_hours` and `IntentionSpec.limits`, filled at the eight spawn sites via `intentions.ttl_for(settings)` / `intentions.limits_for(settings)` (both `None` with the flag off), §4.7. The alternative, a module-level configured value, is global state the tests would have to reset.

---

## 6. Risks, and where the code makes the spec harder than it assumes

1. **`_authorize_tool_call` is synchronous** (`nous/api/runner.py:407`), so the §4.6 "reads the root row and caches it for the turn" cannot happen there. The contract uses an in-process view (`set_cancelled_roots`, §4.5/§4.8): `cancel_root` adds to a set, `start()` loads roots with `root_cancelled_at IS NOT NULL` that still have open work, and every sweep refreshes it with `cancelled_root_ids(since=last_sweep)`. Sound only under "one Nous process per (database, agent_id)", which the execution ledger already assumes (CLAUDE.md, Database). A second process would learn of a cancel at its next sweep, not instantly.
2. **`IntentionClosePass` races the Phase 2 writer** (`nous/heart/result_reconciler.py:224-260`): without the exclusion in §4.7 a `continue` intention whose source finished would be closed `legacy` with no inbox row, which is a lost result (G6). The 2b test must run the pass against a terminal subtask of a `continue` intention with the flag on and assert the intention is NOT closed by it.
3. **Push suppression order**: `_notify_telegram` runs before `_record_inbox` (`nous/handlers/subtask_worker.py:189, 192, 287, 412`), and the intention's policy is not on the row. The point read in `_notify_telegram` (§4.9) is the cheapest safe fix; a test must assert no HTTP call for a `continue` subtask with `notify=True`.
4. **Extracting `execute_single_call` from the 190-line per-call block** (`runner.py:3077-3266`) is the riskiest edit in 2d. Keep `stream_chat`'s copy untouched; the extraction must leave `_tool_loop`'s behaviour byte-identical (the existing ledger, snapshot and suppression tests are the regression net).
5. **Telegram bot ignores `callback_query` updates** (`nous/telegram_bot.py:539-541`) and tracks no sent message ids (`_get_last_bot_message_id` returns `None`, line 922-924). The server-side publisher owns `push_message_id` (§4.9); the bot never needs to remember what it sent.
6. **The default-chat substitution for DAGs** (`result_inbox.py:450-454`) and `result_inbox_dag_scheduled` would route a `continue` DAG's result to the chat; the writer split in §4.9 keeps it inside the non-continue branch. A pin test: a `continue` DAG with no origin and `result_inbox_dag_scheduled=True` writes a NULL-keyed row.
7. **`claim()` is keyed by channel/session only** (`result_inbox.py:217-222`), so NULL-keyed `continue` rows are unreachable from chat by construction; but owner-facing rows ARE channel-keyed and WILL be claimed by the next chat turn and shown to the model. That is intended (§4.3 item 4); the PROPOSAL trailer in `format_inbox_messages` and the §7 injection test ("approve proposal X" in a result produces no approval) guard the model-cannot-approve invariant.
8. **Debounce vs the fan-out**: a claim takes every `result_ready` intention of the root, so a batch can hold intentions at different depths; `claim.deepest` (max depth, earliest `created_at`, lowest id) is the parent of every spawn in that turn (§4.4). The test "Batch parent" in §7 pins it.
9. **Lease vs `wait_for`**: the turn timeout must stay ≥ 60 s below the lease (validator, §4.3); but `wait_for` cancels the turn task only at its next await, and a tool call in `asyncio.to_thread` (write_file) or an HTTP send finishes anyway. The fenced commit is what makes a late turn harmless; a test must show a turn whose lease was released cannot commit (`commit_arrival` returns `None`).
10. **`heartbeat_check` lineage tokens**: `HeartbeatRunner._tick` adds a check's tokens only to its daily total (`nous/heartbeat/runner.py:1055-1056`); 2c adds, for a check carrying `_intention` and `_CHECK_OWNER_KEY`, `dag_store.add_tokens(dag_id, tokens)` (`nous/dag/store.py:762`) after resolving the node's `dag_id` from the owner key. Without it a looping lineage check never reaches the token budget.
11. **Migration constraint renames**: 081 named its constraints (`uq_result_inbox_source`, `chk_result_inbox_*`), so the 076 drop-then-add pattern works without guessing Postgres-generated names. Tests must create the table from `init.sql` + migrations, not from the ORM, to catch a mismatch.
12. **`ContextKind` additions ripple into `tests/test_execution_context.py`-style enumerations and the harness dashboard's hard-coded lists** (`dashboard-app/src/views/Ledger.svelte:23`); 2a updates both.

---

## 7. Review Focus seed: the five failure modes most likely to bite

1. **A stale claim commits, or a result is marked delivered before the fence.** Every `UPDATE` in `commit_arrival` and `fail_attempt` carries `claim_token`; `delivered_at` is stamped only inside the fenced commit, never at read time. Mutation check: drop the `claim_token` predicate from one statement and the lease-release test must fail.
2. **A forged or unoffered call runs because the mode is `warn`.** The strict block sits before the `offered_mode != "off"` check and before `policy_mode == "off"` (§4.5). Mutation check: set both modes to `off` in the forged-`send_email` test and assert the refusal still happens, on both loops.
3. **A proposal becomes approvable from a failed, timed-out or lease-lost attempt.** `staged → pending` happens only in `publish_staged` inside the fenced commit; `expire_staged` runs on every failure path and on lease release. Mutation check: kill the turn after `propose_action` and assert no `pending` row exists and the Telegram publisher sends nothing.
4. **A result is lost silently.** Four doors: (a) the flag is on but no runner claims NULL-keyed rows (the §2 gate); (b) `IntentionClosePass` closes a `continue` intention as `legacy` (§4.7 exclusion); (c) a `continue` DAG is marked delivered with no inbox row and `InboxDagPass` does not select it (§4.9 filter); (d) a cancelled lineage subtask reaches no writer (`repair_missing_results`). Each needs a test that removes the hook and watches the count of undelivered-and-unclaimable results go above 0.
5. **Cancel races.** Three lock orders must hold everywhere: root `FOR UPDATE` (cancel, claim) vs root `FOR SHARE` (spawn, `_hold_open_root`); container row before schedule row (cancel of a container vs a fire, `_hold_open_container`); `approved → executing` with the root-open predicate in the same statement (cancel vs approve). Plus the running-turn task cancel: the turn's spawns after the cancel are refused by I1 (root closed) even if the task cancel lands late. Tests: cancel committed between `claim_execution`'s read and write; a fire in flight while a container is cancelled; a spawn in flight while its root is cancelled.
