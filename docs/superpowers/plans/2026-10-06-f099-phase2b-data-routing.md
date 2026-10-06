# F099 Phase 2b: Data and routing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a `continue` result land where only Nous's own continuation can read it, close every other intention as `delivered`, and keep every flag-off path exactly as Phase 1 left it. After 2b the data exists and is routed, but nothing claims it: the flag stays forced off until 2e.

**Architecture:** One PR (`2b`), eight tasks, all behind `NOUS_CONTINUATION_ENABLED` (default `false`, and forced off in `main.py` by `CONTINUATION_RUNNER_READY = False` until 2e).
- **Schema (2b-1).** Migration `084` creates `brain.intention_arrivals` and `brain.intention_proposals` (tables only: 2c and 2d write them), widens `heart.result_inbox` (two CHECKs, `agent_id` in the UNIQUE key, five columns), and the ORM follows.
- **Settings and the gate (2b-2).** Fourteen `NOUS_CONTINUATION_*` / `NOUS_INTENTION_*` settings with their validators, the module `nous/brain/continuation.py` with `CONTINUATION_RUNNER_READY`, and `main.py`'s `_gate_continuation_flag`.
- **Inbox primitives (2b-3).** One `insert_inbox_row` (the only INSERT into `heart.result_inbox`), `ResultInboxStore.insert(session=)`, `insert_report` (owner-facing rows), `close_delivered`, `metrics()` and the PROPOSAL trailer.
- **The same-transaction move (2b-4).** `continuation.record_result` writes a NULL-keyed row and moves `pending` to `result_ready` in one transaction, holds rows while `awaiting_owner` or `deciding`, reopens a closed `continue` intention (T6), and turns an arrival nothing can reopen into an `intention_report`. The store emits `intention.result_ready` after commit.
- **Writers (2b-5).** The subtask worker hook, the DAG writer and the inbox passes route by wake policy: `continue` goes intention-only, `report` closes as `delivered` in its insert's transaction (I4), `none` and `remember` close as `delivered`, all only with the flag on.
- **Push suppression (2b-6).** `SubtaskWorkerPool._notify_telegram` and the F087 Telegram leg stand down for a `continue` source; the F087 summary turn is skipped for an `internal_only` DAG.
- **Chat never claims, reconciler selects (2b-7).** `pre_turn` skips the inbox for `intent-` sessions and the continuation context; `InboxDagPass` and `InboxSubtaskPass` select continuation work; `IntentionClosePass` leaves `continue` and `report` alone.
- **Rollback and wiring (2b-8).** `rollback_at_startup` re-routes, expires and closes with the flag off (both flags off included), `main.py` wires the gate, the rollback, the bus and the delivery's intention store, and the docs land.

**Tech Stack:** Python 3.12+, SQLAlchemy 2 async (ORM + Core `UPDATE … RETURNING`, `SELECT … FOR UPDATE`), PostgreSQL 17 + pgvector (CI and the local Postgres lane), pydantic v2 / pydantic-settings, pytest with `asyncio_mode = "auto"`.

**Spec:** `docs/superpowers/specs/2026-10-05-f099-intentions-and-continuation-design.md`, read §4.1, §4.3 Phase 2 (items 1 to 6), §5, §6 Phase 2 and §7 Phase 2 (Routing, Commit). The binding names, signatures, DDL, settings and states are in the Phase 2 contract, `2026-10-06-f099-phase2-contract.md` (the Phase 2 planning directory), section "2b"; where the lead rulings (`2026-10-06-f099-phase2-lead-rulings.md`) differ, they win. Section references below (§4.3 …) are to the spec; "contract §4.x" is to the contract.

**Code base:** `main` at `1c10ed8d` (PR-1 merged; identical to `2f23318a`). Line anchors (`~:N`) are from that commit and will drift: **anchor by function name, not by line number**.

## Contract conflicts and interpretations (read first; each has a recommended resolution, applied below)

| # | Contract / spec says | Code or rule says | Resolution this plan applies |
|---|---|---|---|
| C1 | §4.2 DDL: the comment above the `result_inbox` ALTERs reads "…as a new source kind; the UNIQUE key gains…" | The migration rule (and the Phase 0/1 plan) forbids `;` inside a `--` comment. The migrator strips comment lines first (`nous/storage/migrator.py:47`), so it would run, but the rule is a rule | The comment is reworded without a `;`. DDL statements are byte-for-byte the contract's. |
| C2 | §4.7 `insert_report(…, root_id, …) -> UUID` | `heart.result_inbox` has no `root_id` column, so the parameter has nowhere to go. Also a fixed `uuid4()` source id gives a re-arrival report no idempotency: the DAG listener and `DAGResultDelivery.deliver` both reach the writer, and two calls would write two reports | `root_id` is accepted (signature unchanged) and used for the log line only. `insert_report` gains one optional keyword, `report_id: UUID | None = None`; `record_result` passes a deterministic `uuid5` of `(source_kind, source_id, generation)`, so the UNIQUE key collapses the double write. |
| C3 | §4.9: `set_bus` "called once in `main.py` next to `ResultInboxDagListener(...).register(bus)`" | That line runs only when `dag_enabled`. The subtask worker hook also emits `intention.result_ready`, and must have the bus with DAGs off | `heart.result_inbox.set_bus(bus)` is called right after `bus = EventBus()` (one line, always when the bus exists). |
| C4 | §4.9 extends only `InboxDagPass`'s filter | `InboxSubtaskPass` requires `parent_channel` or `parent_session_id`. A `continue` subtask from a heartbeat check or a scheduled launcher has neither, so its lost row would never be repaired: door (c) of the contract's own Review Focus 4, for subtasks | `InboxSubtaskPass` gets the same extension (open `continue` intention of the subtask). |
| C5 | §4.1 T3 lists `container` among the intentions that close as `delivered` | `ScheduleManager` has no settings (the contract itself says "the stores have no settings"), so its container close cannot read the flag | Containers keep closing as `legacy` (also in `IntentionClosePass`'s container half). Nothing reads a container's reason. Writers, the inline close in `spawn_task` and the `IntentionClosePass` source half use `continuation.close_reason_for(settings)`. |
| C6 | §3 says internal_only narrowing has no flag gate "because a flag-gated safety invariant is a footgun", but §4.9 gates the F087 summary skip on the flag | Same hole: a lineage DAG that finishes after the flag went off would run the summary turn with outward tools | The summary skip for a **found** `internal_only` intention is gated on `intentions.enabled(settings)` (one point read per delivery), not on `continuation_enabled`: Phase 1 writes no `internal_only` row, so with continuation off nothing changes for any DAG the Phase 1 code could have created. A **failed** lookup fails closed (skips the summary, template used) **only when `continuation.enabled(settings)`**; with continuation off a failed lookup runs the summary turn exactly as Phase 1 does (lead ruling MF-2). The Telegram suppression stays gated on `continuation.enabled`. |
| C7 | §1.2 puts "`pre_turn(context_kind=)`" in 2b, §4.8 has `run_turn` pass `context_kind` | `ExecutionContext.__post_init__` rejects an unknown `kind` (`nous/api/execution_context.py:89`), so a `continuation` context cannot be built until 2a | 2b adds the `pre_turn` parameter and both skips (the `intent-` session prefix makes 2b complete without it). The `run_turn` pass-through line is 2c's, next to the `continuation` kind. |
| C8 | §4.9 writers: non-`continue` policy = "`close_delivered` then today's insert"; spec §4.3 item 3: "any other policy" re-arrival becomes an `intention_report` | A retried `remember` DAG routed by F098 already reaches the same chat, and F087's Telegram leg still pushes it, so a report would duplicate | Writers keep F098 routing for non-`continue` policies (incl. a re-arrival). `record_result` itself implements the spec rule defensively (a non-`continue` policy, a closed root, a `cancelled`/`expired` state all become a report), and a test pins it directly. |
| C9 | Contract §4.7 `Claim.inbox_rows`: "undelivered rows keyed by any claimed intention" | Owner-facing `intention_report` rows carry `intention_id` too (needed by 2c to tell a report's origin) but are channel-keyed and meant for chat | 2b adds `continuation.intention_keyed(agent_id, ids)`, the predicate "`intention_id` in ids AND channel IS NULL AND session_id IS NULL", with a test. 2c's claim must use it. |
| C10 | §4.15: `record_dag_result` gets "`status in TERMINAL_DAG_STATUSES` or return" | `nous.dag.store` imports `nous.brain.intentions`, so `result_inbox` cannot import it | The guard uses `intentions.TERMINAL_DAG_STATUSES` (already pinned equal to the store's by `tests/test_f099_intentions.py`). **This is a flag-off change** (SF-3): on Phase 1, `record_dag_result(status="running")` closes the intention as `legacy` and writes a `FAILURE` row, and after 2b it returns `False`. It is unreachable in prod (the listener, `deliver` and `InboxDagPass` pass terminal statuses only). It is named in the PR body's flag-off list and should be added to contract §1.7. |
| C11 | The contract's parallel-build note says 2b does not edit `nous/api/tools.py` | The inline close of an inline `spawn_task` (a `none` intention) must close as `delivered` under the flag (ruling 2) | One two-line edit in `_close_inline_intention` and its single call site, in a hunk (~:2969, ~:3360) far from 2a's (`_origin_args`, `dispatch`, `cancel_task`, `dag_create`). Whichever PR merges second rebases. |
| C12 | §4.3 item 1 says the continuation reads rows by `intention_id` and a result with nothing to say needs no row | A `continue` intention whose subtask completes with an empty result would stay `pending` forever (the worker writes no row; the pass would settle it) | For a `continue` intention an empty result is written as an `INFORM` row "The work finished and returned no output." |

## Global Constraints

- **Flag-off parity.** With `NOUS_CONTINUATION_ENABLED` off, routing and closing are Phase 1's, byte for byte: the Phase 1 writer bodies are kept verbatim and every new branch sits behind `continuation.enabled(settings)`. `tests/test_f099_routing_pins.py` runs **unchanged** (it already runs with intentions off and on); `tests/test_f099_closing.py` changes in exactly one place (Task 2b-5, on purpose). The stated exceptions, **the PR body's flag-off list** (and contract §1.7 should carry them): (1) migration `084` (DDL only); (2) the startup rollback (Task 2b-8): one SELECT on open `continue` intentions that finds nothing, plus the terminal-source sweep that `IntentionClosePass` already runs every 60 s (with intentions off it closes `pending` rows Phase 1 would have left alone: spec §4.3 item 6 mandates it); (3) C10, `record_dag_result` ignores a non-terminal status; (4) `metrics()` gains an `intention_report` key; (5) `main.py` forces the continuation flag off with a WARNING until 2e. The F087 summary skip adds no flag-off change beyond a found `internal_only` row, which Phase 1 never writes (C6).
- **No claim, no runner.** 2b adds no runner, no claim SQL, no tool, no REST route, no `ContextKind`, no enforcement. A `continue` row written with the flag on is read by nobody until 2c, which is why `CONTINUATION_RUNNER_READY` stays `False` and `main.py` forces the flag off (Task 2b-2).
- **One INSERT into `heart.result_inbox`.** Every writer, `ResultInboxStore.insert` included, goes through `continuation.insert_inbox_row`, so the four-column conflict target lives in one place.
- **One close reason.** Every close that replaces a `legacy` one under the flag asks `continuation.close_reason_for(settings)`: `delivered` with the flag on, `legacy` otherwise (ruling 2).
- **One patch target.** Callers reach `nous/brain/continuation.py` through the module (`from nous.brain import continuation`, then `continuation.record_result(...)`), as they reach `intentions`. A single `monkeypatch.setattr(continuation, "<name>", ...)` then reaches every writer.
- **Migration.** `sql/migrations/084_intention_arrivals_proposals.sql`, the next free number after `083` (run `ls sql/migrations | sort | tail -3` on your branch; if another migration landed, take the next number, and fix the file name in every step). Use `IF NOT EXISTS` / `ADD COLUMN IF NOT EXISTS`, drop-if-exists-then-add for the three changed constraints (the 076 pattern), full-line `--` comments only, **no `;` inside a comment**, no `BEGIN`/`COMMIT`, no `DO $$` blocks.
- **Settings.** Every test that wants the flag on sets all three: `Settings(_env_file=None, result_inbox_enabled=True, intentions_enabled=True, continuation_enabled=True, …)` (`f099_support.CONT`). A test that sets fewer silently runs with the flag off, because the validators force it. Settings are always hermetic: `Settings(_env_file=None, …)`.
- **Tests.** Real Postgres (the local lane, and CI). SQL that SQLite cannot run (`CAST(text AS uuid)` joins, `FOR UPDATE` conflicts, constraint introspection) carries `@pytest.mark.postgres_only`. Each test uses its own `agent_id` (`f099_support.env_factory` makes one per environment) and keeps at most 5 subtasks `pending` per agent.
- **Test expectations are not negotiable.** If a test in this plan fails after the implementation step, fix the implementation. If you are sure the test itself is wrong (a fixture name, a helper signature that differs on `main`), fix only that mechanical detail and say so in the task report. Never weaken an assertion.
- **Fail-on-base rule, and its exception.** Every task contains at least one test that calls production code and fails on the branch's base before the change. The exceptions are **pins**, which pass on the base by design and must keep passing, and each is marked `# PIN` in the code: the flag-off parity tests (2b-5), `test_the_chat_claim_still_takes_an_owner_row` (2b-7; it pins 2b's invariant, not base behaviour, since the 081 CHECK rejects an `intention_report` row on the base) and the routing-pins file itself. A pin's expected value is a literal; changing one is a behaviour change and needs its own review.
- **Commits.** Use explicit `git add <path> …`; never a directory, `.`, `-A` or `commit -a` (this is a public repo). Write the message to a file (`git commit -F <file>`), ending with these two lines:
  ```
  Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_015q4W1nke7JzGaZkC21Whng
  ```
  Put `set -o pipefail` before any `… | … && git commit` chain. Never use `git stash`.
- **Lint.** `lint-delta.sh <worktree>` must report clean: no new ruff finding and no format drift in a touched file.
- **Public repo.** No machine-local paths, private hosts, tokens or personal names in code, tests, comments, commit messages or docs. Test chat ids are made-up numbers; the fake bot token is `"test-token"`.
- **Docs in the same PR.** A new setting gets a row in `docs/reference/environment-variables.md` (2b-2), the two new tables an entry in `tests/test_database.py::test_all_tables_exist` and the count in `CLAUDE.md` (2b-1: 53 to 55, brain 10 to 12), the new module a row in `docs/reference/project-structure.md`, and the F099 status goes in `docs/features/INDEX.md` (2b-8). `docker-compose.yml` gets a line for every new Phase 2 setting (real default, never `${VAR:-}`) and the F098 lines it never had (`NOUS_RESULT_INBOX_DAG_SCHEDULED`, `NOUS_RESULT_MEMORY_ENABLED`, `NOUS_RESULT_MEMORY_SCHEDULED`, each `:-false`) in 2b-2 (lead ruling). `NOUS_RESULT_INBOX_ENABLED` and `NOUS_INTENTIONS_ENABLED` are already there (PR-1).

## Review Focus

These are the failure modes most likely to bite. Each names the test that catches it; reviewers should still check them by reading.

1. **A result is lost silently (contract Review Focus 4), three of its four doors are 2b's.** (a) The flag on with no runner: `test_the_gate_forces_a_requested_flag_off`, `test_create_components_gates_the_flag_before_anything_reads_it` (2b-2). (b) `IntentionClosePass` closes a `continue` intention as `legacy` with no inbox row: `test_the_close_pass_leaves_continue_and_report_alone` (2b-7; mutation check: drop `exclude_policies` and it fails). (c) A `continue` DAG is marked delivered with no row and `InboxDagPass` does not select it: `test_a_continue_dag_is_never_delivered_without_its_row` (2b-7; mutation check: revert the filter and it fails). (d) A cancelled lineage subtask (a `continue` **or a `report`** one, both policies being excluded from the close pass with the flag on) reaches no writer: **not closed in 2b**. Its intention stays `pending` until 2c's `repair_missing_results`. The flag cannot be on before 2e, so no prod row is affected; the residual is recorded in the PR body. (e) **A reported `continue` result must settle its work row**: when `record_result` turns a result into a report (cancelled or expired intention, closed root, no owner channel) it also writes the source-keyed row, NULL-keyed and already delivered, in the same transaction; otherwise `has_row` stays false and the F098 passes re-select the work row on every tick, eating a `RECONCILE_BATCH_SIZE` slot each (`test_a_reported_result_settles_its_work_row_for_the_reconciler_passes`, 2b-4; the pass-level pins `test_a_reported_continue_result_is_not_reselected_by_the_subtask_pass` and `test_a_reported_continue_dag_is_settled_for_the_dag_pass` are in 2b-7, because they need that task's pass changes).
2. **The state move and the row are one transaction.** `test_a_fault_after_the_insert_leaves_neither_row_nor_move` (2b-4) injects the fault between the INSERT and the UPDATE. The reverse order matters as much: the UPDATE runs only when the INSERT wrote a row, so a duplicate delivery (the bus listener plus `deliver`) never moves a state twice (`test_a_duplicate_arrival_changes_nothing`).
3. **Chat can never claim a `continue` row, and always can claim an owner row.** The rows are NULL/NULL-keyed by construction (`test_a_continue_result_is_keyed_by_the_intention_alone`, 2b-5), `pre_turn` skips `intent-` sessions and the continuation context even when a row is forged into one (2b-7), and the owner-facing rows stay channel-keyed (`test_the_chat_claim_still_takes_an_owner_row`, a PIN). 2c's claim must filter with `continuation.intention_keyed` (C9).
4. **Rollback strands nothing.** One transaction re-routes, expires and closes (`test_the_rollback_reroutes_expires_and_closes_with_both_flags_off`); with the inbox off a failed raw Telegram push keeps the intention open for the next start (`test_a_failed_raw_push_keeps_the_intention_open`); a process with the flag on never rolls back (`test_a_flag_on_process_never_rolls_back`).
5. **Flag-off parity is the largest silent-regression surface.** The Phase 1 writer bodies are unchanged and the new code is behind one predicate. `test_the_three_flag_states_route_a_continue_result_as_specified` (2b-5) and the unchanged routing pins are the net. The one Phase 1 test that changes is named in Task 2b-5. With intentions on and continuation off, a failed intention lookup in F087 delivery still runs the summary turn (`test_a_failed_dag_intention_lookup_follows_the_continuation_flag`, 2b-6).
6. **Held rows must not move a state.** A row that arrives while the intention is `awaiting_owner` or `deciding` is inserted and leaves the state alone (`test_rows_arriving_while_awaiting_or_deciding_are_held`); the commit in 2c is what consumes it. Getting this wrong wakes a root before its owner answered.
7. **The migration's UNIQUE change.** `agent_id` goes **last** in the key so the reconciler's `has_row` lookups keep their index prefix (`test_the_inbox_unique_key_carries_the_agent_last`).

## File map

| File | Responsibility | Tasks |
|---|---|---|
| `sql/migrations/084_intention_arrivals_proposals.sql` (new) | the two tables, the widened inbox | 2b-1 |
| `nous/storage/models.py` | `IntentionArrival`, `IntentionProposal`, `ResultInbox` (constraints, five columns) | 2b-1 |
| `nous/config.py` | fourteen settings, two validators | 2b-2 |
| `nous/brain/continuation.py` (new) | `CONTINUATION_RUNNER_READY`, constants, `insert_inbox_row`, `insert_report`, `close_delivered`, `close_reason_for`, `owner_channel`, `intention_keyed`, `has_continue_intention`, `record_result`, `rollback_at_startup` | 2b-2, 2b-3, 2b-4, 2b-7, 2b-8 |
| `nous/heart/result_inbox.py` | `insert(session=)`, `set_bus`, `record_continue_result`, `insert_and_close`, `intention_of`, `route_result`, the writers, `metrics`, the PROPOSAL trailer | 2b-3, 2b-4, 2b-5 |
| `nous/heart/result_reconciler.py` | pass filters, `InboxSubtaskPass` routing, `IntentionClosePass` exclusion | 2b-5, 2b-7 |
| `nous/brain/intentions.py` | `close_finished_sources(exclude_policies=)` | 2b-7 |
| `nous/handlers/subtask_worker.py` | `_notify_telegram` suppression | 2b-6 |
| `nous/dag/delivery.py` | `intentions=`, the Telegram leg and summary skips | 2b-6 |
| `nous/cognitive/layer.py` | `pre_turn(context_kind=)`, the `intent-` skip | 2b-7 |
| `nous/api/tools.py` | `_close_inline_intention(reason=)` (one hunk) | 2b-5 |
| `nous/main.py` | `_gate_continuation_flag`, `_rollback_continuation`, `set_bus`, delivery wiring | 2b-2, 2b-8 |
| Tests (new) | `tests/f099_support.py`, `tests/test_f099_phase2b_{schema,settings,inbox,record_result,writers,suppression,routing,rollback}.py` | all |
| Tests (edited) | `tests/test_database.py`, `tests/test_f099_closing.py` | 2b-1, 2b-5 |
| Docs | `CLAUDE.md`, `docs/reference/environment-variables.md`, `docs/reference/project-structure.md`, `docs/reference/rest-api.md`, `docs/features/INDEX.md` | 2b-1, 2b-2, 2b-8 |

---

## Implementer notes

**Branch** `feat/f099-phase2b-data-and-routing`, from `origin/main` at `1c10ed8d` (re-check with `git log --oneline -1 origin/main`; if `main` moved, rebase before Task 2b-1 and say so in the report). 2a may merge first or second: the only shared files are `nous/brain/intentions.py` (2a adds `origin_authority`; 2b adds one parameter to `close_finished_sources`) and `nous/api/tools.py` (disjoint hunks). Whichever merges second rebases.

**Scripts** live in the test-lane script directory provided at hand-off; call that `$BIN` below, and `$MAIN_VENV` is the main checkout's virtualenv. Run them from Git Bash.

**Your own database, before any targeted run.** `nous-test-linux.sh` mounts the worktree read-only and applies **no** migrations, and the template database `nous_fix_base` stops at migration 080. Create one database per implementer and never share it: other agents use the same Postgres, and some tests `LOCK TABLE`. Re-run the loop after Task 2b-1 adds migration `084`.

```bash
WT=<path to your worktree>
DB=f099_2b_<yourname>              # unique, lowercase
docker exec nous-postgres psql -U nous -d postgres -qc "DROP DATABASE IF EXISTS $DB" -qc "CREATE DATABASE $DB TEMPLATE nous_fix_base"
for f in $(ls "$WT"/sql/migrations/*.sql | sort); do
  n=$(basename "$f" | cut -c1-3)
  [ "$((10#$n))" -ge 81 ] && docker exec -i nous-postgres psql -U nous -d "$DB" -v ON_ERROR_STOP=1 -q < "$f"
done
```

**Targeted run.** `"$BIN/nous-test-linux.sh" "$WT" runner pg "$DB" tests/test_f099_phase2b_settings.py -q`. You may add `-k <name>`.

**Full gate** (once, before review). `"$BIN/gate-with-migrations.sh" f099-2b:"$WT":<fresh_db>:81`. Compare failures with a gate of the base. A failure that is also on the base is not yours (CI is the final gate).

**Lint.** `"$BIN/lint-delta.sh" "$WT"` must say `clean`. It enforces ruff's `E`/`F`/`I`/`UP` rules at line length 120, and `ruff format` on every **new** file. Before running it, format and fix the files you created:
```bash
RUFF="$MAIN_VENV/Scripts/ruff.exe"
"$RUFF" check --config "$WT/pyproject.toml" --fix <your new test files and new modules>
"$RUFF" format --config "$WT/pyproject.toml" <your new test files and new modules>
```
Run `ruff format` on an existing file only if it was format-clean on the base (lint-delta reports "FORMAT drift" exactly in that case).

**Do not run** pytest against the shared `nous` database, or against another agent's database.

**Shared test helpers.** Task 2b-3 creates `tests/f099_support.py` (the environment fixture and builders). Later test files import from it as `from f099_support import …` (the `tests/` directory is on `sys.path`, as `conftest.py`'s `from sqlite_compat import …` relies on). A fixture is imported by name and marked `# noqa: F401`.

---

## Task 2b-1: Migration 084, the ORM, the table count

**Files:**
- Create: `sql/migrations/084_intention_arrivals_proposals.sql`
- Modify: `nous/storage/models.py`: `IntentionArrival` and `IntentionProposal` (after `Intention`), `ResultInbox` (constraints, five columns)
- Modify: `tests/test_database.py` (two tuples, the brain count comment)
- Modify: `CLAUDE.md`, the Database bullet: recount from the expected set after your edit (53 on `main`; brain 10, heart 19, nous_system 24; after this task 55, brain 12)
- Create: `tests/test_f099_phase2b_schema.py`

**Interfaces:**
- Produces: tables `brain.intention_arrivals` and `brain.intention_proposals` exactly as contract §4.2; the `heart.result_inbox` changes of contract §4.2 (`source_kind` adds `intention_report`; `msg_type` adds `REPORT`, `QUESTION`, `PROPOSAL`; `UNIQUE (source_kind, source_id, source_generation, agent_id)`; columns `arrival_id`, `proposal_id`, `push_after`, `pushed_at`, `push_message_id`; two partial indexes).
- Produces: ORM `IntentionArrival`, `IntentionProposal`; `ResultInbox.arrival_id / proposal_id / push_after / pushed_at / push_message_id`.
- `brain.intentions` gets **no** new column (the contract's note: `claimed_at`, `claim_token`, `attempts` and `deadline` already exist in 083).

- [ ] **Step 1: Write the failing tests.** Create `tests/test_f099_phase2b_schema.py`:

```python
"""F099 Phase 2b: migration 084 — the arrivals and proposals tables, the widened inbox."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from nous.storage.models import Intention, IntentionArrival, IntentionProposal, ResultInbox

# The tables must come from migration 084, not from the ORM (contract risk 11), and the SQLite lane's
# list type cannot bind UUIDs. CI (Postgres) is the gate.
pytestmark = pytest.mark.postgres_only


def _agent() -> str:
    return f"f099-sch-{uuid.uuid4().hex[:8]}"


async def _root(db, agent: str) -> uuid.UUID:
    root_id = uuid.uuid4()
    async with db.session() as s:
        s.add(
            Intention(
                id=root_id,
                agent_id=agent,
                root_id=root_id,
                source_kind="subtask",
                source_id=str(uuid.uuid4()),
                intent="x",
                origin_kind="interactive",
                wake_policy="continue",
            )
        )
        await s.commit()
    return root_id


def _arrival(agent: str, root_id: uuid.UUID, n: int = 1, **over) -> IntentionArrival:
    values = dict(
        agent_id=agent,
        root_id=root_id,
        n=n,
        intention_ids=[root_id],
        claim_token=uuid.uuid4(),
        outcome="resolved",
    )
    values.update(over)
    return IntentionArrival(**values)


async def test_an_arrival_row_round_trips_with_its_defaults(db):
    agent = _agent()
    root_id = await _root(db, agent)
    async with db.session() as s:
        s.add(_arrival(agent, root_id, decision="ask", gate_reason="budget_turns"))
        await s.commit()
    async with db.session() as s:
        row = (await s.execute(select(IntentionArrival).where(IntentionArrival.agent_id == agent))).scalar_one()
    assert (row.decision, row.gate_reason, row.tokens_in, row.tokens_out) == ("ask", "budget_turns", 0, 0)
    assert list(row.inbox_ids) == [] and list(row.report_ids) == []


@pytest.mark.parametrize(
    "bad",
    [{"outcome": "bogus"}, {"decision": "bogus"}, {"gate_reason": "bogus"}],
    ids=["outcome", "decision", "gate_reason"],
)
async def test_an_arrival_check_constraint_rejects_a_foreign_value(db, bad):
    agent = _agent()
    root_id = await _root(db, agent)
    with pytest.raises(IntegrityError):
        async with db.session() as s:
            s.add(_arrival(agent, root_id, **bad))
            await s.commit()


async def test_arrival_numbers_are_unique_per_root(db):
    agent = _agent()
    root_id = await _root(db, agent)
    async with db.session() as s:
        s.add(_arrival(agent, root_id, n=1))
        await s.commit()
    with pytest.raises(IntegrityError):
        async with db.session() as s:
            s.add(_arrival(agent, root_id, n=1))
            await s.commit()


async def test_a_proposal_row_defaults_to_staged_and_rejects_a_foreign_state(db):
    agent = _agent()
    root_id = await _root(db, agent)
    values = dict(
        agent_id=agent,
        intention_id=root_id,
        root_id=root_id,
        tool="send_email",
        arguments={"to": "a@example.com"},
        rationale="the owner asked",
        claim_token=uuid.uuid4(),
    )
    async with db.session() as s:
        s.add(IntentionProposal(**values))
        await s.commit()
    async with db.session() as s:
        row = (await s.execute(select(IntentionProposal).where(IntentionProposal.agent_id == agent))).scalar_one()
    assert row.state == "staged" and row.arrival_id is None
    with pytest.raises(IntegrityError):
        async with db.session() as s:
            s.add(IntentionProposal(**values, state="bogus"))
            await s.commit()


def _inbox(agent: str, **over) -> ResultInbox:
    values = dict(
        agent_id=agent,
        channel="telegram:8080",
        source_kind="intention_report",
        source_id=uuid.uuid4(),
        msg_type="QUESTION",
        title="t",
        body="b",
    )
    values.update(over)
    return ResultInbox(**values)


async def test_the_inbox_accepts_an_owner_facing_row_with_the_new_columns(db):
    agent = _agent()
    async with db.session() as s:
        s.add(_inbox(agent, arrival_id=uuid.uuid4(), proposal_id=uuid.uuid4(), push_message_id=2**40))
        await s.commit()
    async with db.session() as s:
        row = (await s.execute(select(ResultInbox).where(ResultInbox.agent_id == agent))).scalar_one()
    assert row.push_message_id == 2**40 and row.push_after is None and row.pushed_at is None


@pytest.mark.parametrize("bad", [{"source_kind": "bogus"}, {"msg_type": "bogus"}], ids=["source_kind", "msg_type"])
async def test_the_inbox_still_rejects_a_foreign_kind(db, bad):
    with pytest.raises(IntegrityError):
        async with db.session() as s:
            s.add(_inbox(_agent(), **bad))
            await s.commit()


async def test_two_agents_may_share_a_source_key_but_one_agent_may_not(db):
    source_id = uuid.uuid4()
    a, b = _agent(), _agent()
    async with db.session() as s:
        s.add(_inbox(a, source_id=source_id))
        s.add(_inbox(b, source_id=source_id))
        await s.commit()
    with pytest.raises(IntegrityError):
        async with db.session() as s:
            s.add(_inbox(a, source_id=source_id))
            await s.commit()


@pytest.mark.postgres_only  # pg_constraint introspection
async def test_the_inbox_unique_key_carries_the_agent_last(db):
    """agent_id goes LAST so the reconciler's has_row lookups, which prefix on
    (source_kind, source_id), keep using the index (contract section 4.2)."""
    async with db.engine.connect() as conn:
        cols = (
            await conn.execute(
                text(
                    "SELECT a.attname FROM pg_constraint c "
                    "JOIN pg_class t ON t.oid = c.conrelid "
                    "JOIN pg_namespace n ON n.oid = t.relnamespace "
                    "JOIN LATERAL unnest(c.conkey) WITH ORDINALITY AS k(attnum, ord) ON true "
                    "JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = k.attnum "
                    "WHERE n.nspname = 'heart' AND t.relname = 'result_inbox' AND c.conname = 'uq_result_inbox_source' "
                    "ORDER BY k.ord"
                )
            )
        ).scalars().all()
    assert list(cols) == ["source_kind", "source_id", "source_generation", "agent_id"]


@pytest.mark.postgres_only  # pg_indexes
async def test_the_partial_indexes_exist(db):
    async with db.engine.connect() as conn:
        names = set(
            (
                await conn.execute(
                    text(
                        "SELECT indexname FROM pg_indexes WHERE schemaname IN ('brain', 'heart') AND indexname IN "
                        "('idx_intention_arrivals_root', 'idx_intention_proposals_open', "
                        "'idx_intention_proposals_arrival', 'idx_result_inbox_intention_undelivered', "
                        "'idx_result_inbox_push_due')"
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(names) == 5
```

- [ ] **Step 2: Run it; expect failure.** Re-create `$DB` (Implementer notes loop), then
  Run: `"$BIN/nous-test-linux.sh" "$WT" runner pg "$DB" tests/test_f099_phase2b_schema.py -q`
  Expected: collection error `cannot import name 'IntentionArrival'`.

- [ ] **Step 3: The migration.** Create `sql/migrations/084_intention_arrivals_proposals.sql` (DDL is contract §4.2; only the comments differ, per C1):

```sql
-- Migration 084: Intention arrivals and proposals (F099 Phase 2b)
--
-- brain.intention_arrivals: one row per arrival decision of a root's
-- continuation. brain.intention_proposals: an action the continuation may not
-- take itself, staged for the owner. Phase 2b creates the tables and the
-- widened inbox. Nothing writes the two new tables until Phase 2c and 2d.
--
-- brain.intentions needs no new column: claimed_at, claim_token, attempts and
-- deadline already exist in migration 083.

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
-- source kind, and the UNIQUE key gains agent_id (spec section 4.3 item 4).
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
-- agent_id goes LAST. The reconciler's correlated has_row lookups prefix on
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

- [ ] **Step 4: The ORM.** In `nous/storage/models.py`, after the `Intention` class (before the `# HEART SCHEMA` banner):

```python
class IntentionArrival(Base):
    """F099 Phase 2: one arrival decision of a root's continuation.

    One row per claim, in order (``n``). ``intention_ids`` lists every intention
    the claim took (one arrival consumes a batch). Written by the runner in
    Phase 2c, in the fenced commit. ``progress_claimed`` is the model's claim,
    ``progress`` the verified value.
    """

    __tablename__ = "intention_arrivals"
    __table_args__ = (
        UniqueConstraint("agent_id", "root_id", "n", name="uq_intention_arrivals_root_n"),
        CheckConstraint(
            "decision IS NULL OR decision IN ('continue', 'revise', 'drop', 'report', 'ask')",
            name="chk_intention_arrivals_decision",
        ),
        CheckConstraint(
            "outcome IN ('resolved', 'fallback_report', 'failed_report')", name="chk_intention_arrivals_outcome"
        ),
        CheckConstraint(
            "gate_reason IS NULL OR gate_reason IN ('cancelled', 'expired', 'past_deadline', 'budget_turns', "
            "'budget_tokens', 'budget_stall', 'limit_depth', 'limit_spawns', 'plan_resolved')",
            name="chk_intention_arrivals_gate_reason",
        ),
        {"schema": "brain"},
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, server_default=func.gen_random_uuid()
    )
    agent_id: Mapped[str] = mapped_column(String(100), nullable=False)
    root_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("brain.intentions.id"), nullable=False)
    n: Mapped[int] = mapped_column(Integer, nullable=False)
    intention_ids: Mapped[list] = mapped_column(ARRAY(UUID(as_uuid=True)), nullable=False)
    inbox_ids: Mapped[list] = mapped_column(
        ARRAY(UUID(as_uuid=True)), nullable=False, default=list, server_default="{}"
    )
    report_ids: Mapped[list] = mapped_column(
        ARRAY(UUID(as_uuid=True)), nullable=False, default=list, server_default="{}"
    )
    claim_token: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    decision: Mapped[str | None] = mapped_column(String(20))
    note: Mapped[str | None] = mapped_column(Text)
    progress_claimed: Mapped[bool | None] = mapped_column(Boolean)
    progress: Mapped[bool | None] = mapped_column(Boolean)
    confidence: Mapped[float | None] = mapped_column(Float)
    gate_reason: Mapped[str | None] = mapped_column(String(40))
    tokens_in: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    tokens_out: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    decision_record_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    outcome: Mapped[str] = mapped_column(String(20), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class IntentionProposal(Base):
    """F099 Phase 2: an action the continuation may not take itself, staged for the owner.

    ``staged`` until the arrival's fenced commit makes it ``pending`` (Phase
    2d); the default at the deadline is a reject.
    """

    __tablename__ = "intention_proposals"
    __table_args__ = (
        CheckConstraint(
            "state IN ('staged', 'pending', 'approved', 'executing', 'rejected', 'expired', 'executed', "
            "'failed', 'cancelled')",
            name="chk_intention_proposals_state",
        ),
        {"schema": "brain"},
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, server_default=func.gen_random_uuid()
    )
    agent_id: Mapped[str] = mapped_column(String(100), nullable=False)
    intention_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("brain.intentions.id"), nullable=False
    )
    root_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("brain.intentions.id"), nullable=False)
    arrival_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("brain.intention_arrivals.id"))
    tool: Mapped[str] = mapped_column(String(100), nullable=False)
    arguments: Mapped[dict] = mapped_column(JSONB, nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(String(20), nullable=False, default="staged", server_default="staged")
    claim_token: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ledger_key: Mapped[str | None] = mapped_column(Text)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decided_by: Mapped[str | None] = mapped_column(Text)
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    result: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
```

In `ResultInbox`, replace the docstring's second paragraph and `__table_args__`:

```python
    """F098: one finished background result waiting for the conversation.

    Routed by ``channel`` (preferred) or ``session_id``; claimed exactly once
    by setting ``delivered_at``. ``UNIQUE(source_kind, source_id,
    source_generation, agent_id)`` makes every writer idempotent, so the DAG bus
    listener and the F087 delivery backstop can both insert; a retried DAG
    (new ``delivery_generation``) gets a row of its own. F099 Phase 2: a row
    with ``channel`` and ``session_id`` both NULL is keyed by ``intention_id``
    alone and is read only by the continuation; ``source_kind =
    'intention_report'`` rows are owner-facing (REPORT, QUESTION, PROPOSAL).
    """

    __tablename__ = "result_inbox"
    __table_args__ = (
        UniqueConstraint("source_kind", "source_id", "source_generation", "agent_id", name="uq_result_inbox_source"),
        CheckConstraint("source_kind IN ('subtask', 'dag', 'intention_report')", name="chk_result_inbox_source_kind"),
        CheckConstraint(
            "msg_type IN ('INFORM', 'FAILURE', 'BLOCKED', 'REPORT', 'QUESTION', 'PROPOSAL')",
            name="chk_result_inbox_msg_type",
        ),
        {"schema": "heart"},
    )
```

and after `intention_id`:

```python
    # F099 Phase 2: the arrival a QUESTION or PROPOSAL row belongs to, and the
    # proposal a PROPOSAL row shows (no FK, like intention_id).
    arrival_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    proposal_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    # Owner push (Phase 2d): quiet-hours deferral, send stamp, the Telegram message id.
    push_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    pushed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    push_message_id: Mapped[int | None] = mapped_column(BigInteger)
```

- [ ] **Step 5: The table count.** In `tests/test_database.py::test_all_tables_exist`, change the comment `# brain (10)` to `# brain (12)` and add after `("brain", "intentions"),`:

```python
        # F099 Phase 2b: arrivals and proposals (migration 084)
        ("brain", "intention_arrivals"),
        ("brain", "intention_proposals"),
```

Recount `CLAUDE.md`'s Database bullet from the expected set (do not increment the old number): it becomes `55 tables total: brain 12, heart 19, nous_system 24`.

- [ ] **Step 6: The insert's conflict target.** The old `ON CONFLICT (source_kind, source_id, source_generation)` of `ResultInboxStore.insert` no longer matches a unique constraint once the key gains `agent_id`, so every inbox write would fail. In `nous/heart/result_inbox.py`, `ResultInboxStore.insert`, change

```python
            .on_conflict_do_nothing(index_elements=["source_kind", "source_id", "source_generation"])
```

to

```python
            .on_conflict_do_nothing(index_elements=["source_kind", "source_id", "source_generation", "agent_id"])
```

(a one-line edit that keeps this commit green; Task 2b-3 replaces the whole method with a delegation to one shared insert.)

- [ ] **Step 7: Re-create your database with the new migration** (the Implementer notes loop), then run:
  Run: `"$BIN/nous-test-linux.sh" "$WT" runner pg "$DB" tests/test_f099_phase2b_schema.py tests/test_database.py tests/test_f098_result_inbox.py tests/test_f099_closing.py -q`
  Expected: PASS. The F098 and Phase 1 inbox suites run against the widened table and are the regression net for the UNIQUE change.

- [ ] **Step 8: Lint and commit**

```bash
cd "$WT"
cat > /tmp/f099-2b-1.txt <<'EOF'
feat(F099): migration 084, intention arrivals and proposals, the widened inbox

Creates brain.intention_arrivals and brain.intention_proposals (tables only:
the runner writes them in later PRs) and widens heart.result_inbox: the
intention_report source kind, the REPORT, QUESTION and PROPOSAL message types,
agent_id last in the UNIQUE key, and the arrival, proposal and push columns.
The inbox insert's conflict target follows the new key.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_015q4W1nke7JzGaZkC21Whng
EOF
git add sql/migrations/084_intention_arrivals_proposals.sql nous/storage/models.py nous/heart/result_inbox.py tests/test_database.py tests/test_f099_phase2b_schema.py CLAUDE.md
git commit -F /tmp/f099-2b-1.txt
```

---

## Task 2b-2: The settings, the module skeleton, the gate

**Files:**
- Modify: `nous/config.py`: fourteen fields after `intentions_enabled`, two validators after `_validate_intentions_dependency`
- Create: `nous/brain/continuation.py` (the constant only; Task 2b-3 grows it)
- Modify: `nous/main.py`: `_gate_continuation_flag`, called in `create_components` right after `_warn_on_f098_flags(settings)`
- Modify: `docs/reference/environment-variables.md` (fourteen rows after `NOUS_INTENTIONS_ENABLED`)
- Modify: `docker-compose.yml` (fourteen Phase 2 lines, and the three F098 lines it never had)
- Create: `tests/test_f099_phase2b_settings.py`

**Interfaces:**
- Produces: `Settings.continuation_enabled` and the thirteen others of contract §4.3 (names, defaults, bounds exactly as the table). `_validate_continuation_dependency` forces the flag off with a WARNING when intentions are off (it runs after `_validate_intentions_dependency`, so a missing inbox also forces it off). `_validate_continuation_timing` raises `ValueError`.
- Produces: `nous.brain.continuation.CONTINUATION_RUNNER_READY: bool = False` and `nous.main._gate_continuation_flag(settings) -> None`.

- [ ] **Step 1: Write the failing tests.** Create `tests/test_f099_phase2b_settings.py`:

```python
"""F099 Phase 2b: the continuation settings, their validators, and the flag gate."""

from __future__ import annotations

import logging

import pytest

import nous.main as main
from nous.brain import continuation
from nous.config import Settings

BASE = {"result_inbox_enabled": True, "intentions_enabled": True}


def test_defaults_are_the_contract_values():
    s = Settings(_env_file=None)
    assert s.continuation_enabled is False
    assert (s.continuation_max_depth, s.continuation_max_spawns_per_root) == (3, 12)
    assert (s.continuation_max_turns_per_root, s.continuation_max_tokens_per_root) == (8, 400000)
    assert (s.continuation_stall_limit, s.intention_root_ttl_hours) == (2, 72)
    assert (s.continuation_max_concurrent, s.continuation_debounce_seconds) == (2, 20)
    assert (s.continuation_max_wait_seconds, s.continuation_lease_seconds) == (120, 900)
    assert (s.continuation_turn_timeout_seconds, s.continuation_max_attempts) == (780, 3)
    assert s.intention_proposal_ttl_hours == 24


@pytest.mark.parametrize(
    "name,bad",
    [
        ("continuation_max_depth", 0),
        ("continuation_max_spawns_per_root", 0),
        ("continuation_max_turns_per_root", 0),
        ("continuation_max_tokens_per_root", 999),
        ("continuation_stall_limit", 0),
        ("intention_root_ttl_hours", 0),
        ("continuation_max_concurrent", 0),
        ("continuation_debounce_seconds", -1),
        ("continuation_max_wait_seconds", -1),
        ("continuation_lease_seconds", 119),
        ("continuation_turn_timeout_seconds", 59),
        ("continuation_max_attempts", 0),
        ("intention_proposal_ttl_hours", 0),
    ],
)
def test_bounds_are_enforced(name, bad):
    with pytest.raises(ValueError):
        Settings(_env_file=None, **{name: bad})


def test_the_environment_names_are_the_contract_names(monkeypatch):
    monkeypatch.setenv("NOUS_CONTINUATION_MAX_DEPTH", "5")
    monkeypatch.setenv("NOUS_INTENTION_ROOT_TTL_HOURS", "36")
    s = Settings(_env_file=None)
    assert (s.continuation_max_depth, s.intention_root_ttl_hours) == (5, 36)


def test_continuation_without_intentions_is_forced_off_with_a_warning(caplog):
    with caplog.at_level(logging.WARNING, logger="nous.config"):
        s = Settings(_env_file=None, result_inbox_enabled=True, continuation_enabled=True)
    assert s.continuation_enabled is False
    assert "NOUS_CONTINUATION_ENABLED=true needs NOUS_INTENTIONS_ENABLED=true" in caplog.text


def test_a_missing_inbox_forces_intentions_and_then_continuation_off(caplog):
    """The two validators run in declaration order: intentions first."""
    with caplog.at_level(logging.WARNING, logger="nous.config"):
        s = Settings(_env_file=None, intentions_enabled=True, continuation_enabled=True)
    assert (s.intentions_enabled, s.continuation_enabled) == (False, False)
    assert "NOUS_INTENTIONS_ENABLED=true needs NOUS_RESULT_INBOX_ENABLED=true" in caplog.text
    assert "NOUS_CONTINUATION_ENABLED=true needs NOUS_INTENTIONS_ENABLED=true" in caplog.text


def test_continuation_stays_on_with_both_prerequisites():
    assert Settings(_env_file=None, continuation_enabled=True, **BASE).continuation_enabled is True


def test_the_turn_timeout_must_sit_at_least_60s_below_the_lease():
    Settings(_env_file=None, continuation_lease_seconds=840, continuation_turn_timeout_seconds=780)  # exactly 60
    with pytest.raises(ValueError, match="at least 60 s below NOUS_CONTINUATION_LEASE_SECONDS"):
        Settings(_env_file=None, continuation_lease_seconds=840, continuation_turn_timeout_seconds=781)
    with pytest.raises(ValueError, match="at least 60 s below NOUS_CONTINUATION_LEASE_SECONDS"):
        Settings(_env_file=None, continuation_lease_seconds=120)  # the default 780 s timeout no longer fits


def test_max_wait_may_not_be_below_the_debounce():
    Settings(_env_file=None, continuation_debounce_seconds=30, continuation_max_wait_seconds=30)
    with pytest.raises(ValueError, match="MAX_WAIT"):
        Settings(_env_file=None, continuation_debounce_seconds=30, continuation_max_wait_seconds=29)


PHASE2_ENV = {
    "NOUS_CONTINUATION_ENABLED": "false",
    "NOUS_CONTINUATION_MAX_DEPTH": "3",
    "NOUS_CONTINUATION_MAX_SPAWNS_PER_ROOT": "12",
    "NOUS_CONTINUATION_MAX_TURNS_PER_ROOT": "8",
    "NOUS_CONTINUATION_MAX_TOKENS_PER_ROOT": "400000",
    "NOUS_CONTINUATION_STALL_LIMIT": "2",
    "NOUS_INTENTION_ROOT_TTL_HOURS": "72",
    "NOUS_CONTINUATION_MAX_CONCURRENT": "2",
    "NOUS_CONTINUATION_DEBOUNCE_SECONDS": "20",
    "NOUS_CONTINUATION_MAX_WAIT_SECONDS": "120",
    "NOUS_CONTINUATION_LEASE_SECONDS": "900",
    "NOUS_CONTINUATION_TURN_TIMEOUT_SECONDS": "780",
    "NOUS_CONTINUATION_MAX_ATTEMPTS": "3",
    "NOUS_INTENTION_PROPOSAL_TTL_HOURS": "24",
    # F098 lines the repo compose never had.
    "NOUS_RESULT_INBOX_DAG_SCHEDULED": "false",
    "NOUS_RESULT_MEMORY_ENABLED": "false",
    "NOUS_RESULT_MEMORY_SCHEDULED": "false",
}


def test_compose_passes_every_phase2_setting_with_the_settings_default():
    """A setting prod's compose does not pass silently keeps its default and cannot be turned on."""
    from pathlib import Path

    compose = (Path(__file__).resolve().parents[1] / "docker-compose.yml").read_text(encoding="utf-8")
    for name, default in PHASE2_ENV.items():
        assert f"- {name}=${{{name}:-{default}}}" in compose, name
    s = Settings(_env_file=None)
    assert str(s.continuation_max_depth) == PHASE2_ENV["NOUS_CONTINUATION_MAX_DEPTH"]
    assert str(int(s.intention_root_ttl_hours)) == PHASE2_ENV["NOUS_INTENTION_ROOT_TTL_HOURS"]
    assert str(int(s.intention_proposal_ttl_hours)) == PHASE2_ENV["NOUS_INTENTION_PROPOSAL_TTL_HOURS"]


def test_the_runner_is_not_ready_in_this_build():
    """2e flips this assertion together with the gate test below."""
    assert continuation.CONTINUATION_RUNNER_READY is False


def test_the_gate_forces_a_requested_flag_off(caplog):
    settings = Settings(_env_file=None, continuation_enabled=True, **BASE)
    assert settings.continuation_enabled is True  # the validators alone do not gate it
    with caplog.at_level(logging.WARNING, logger="nous.main"):
        main._gate_continuation_flag(settings)
    assert settings.continuation_enabled is False
    assert "continuation runner is not shipped in this build" in caplog.text


def test_the_gate_is_silent_when_the_flag_is_off(caplog):
    settings = Settings(_env_file=None, **BASE)
    with caplog.at_level(logging.WARNING, logger="nous.main"):
        main._gate_continuation_flag(settings)
    assert settings.continuation_enabled is False
    assert "continuation runner" not in caplog.text


def test_the_gate_lets_a_ready_runner_through(monkeypatch):
    monkeypatch.setattr(continuation, "CONTINUATION_RUNNER_READY", True)
    settings = Settings(_env_file=None, continuation_enabled=True, **BASE)
    main._gate_continuation_flag(settings)
    assert settings.continuation_enabled is True


async def test_create_components_gates_the_flag_before_anything_reads_it(monkeypatch):
    """create_components must gate before it builds a single component. The
    first thing it builds is the Database, so a stand-in that stops there sees
    the flag already off."""
    settings = Settings(_env_file=None, continuation_enabled=True, **BASE)
    seen: dict[str, bool] = {}

    class _Stop(Exception):
        pass

    def _database(settings_arg, **kwargs):
        seen["flag_when_built"] = settings_arg.continuation_enabled
        raise _Stop

    monkeypatch.setattr(main, "Database", _database)
    with pytest.raises(_Stop):
        await main.create_components(settings)
    assert seen == {"flag_when_built": False}
```

- [ ] **Step 2: Run it; expect failure.**
  Run: `"$BIN/nous-test-linux.sh" "$WT" runner pg "$DB" tests/test_f099_phase2b_settings.py -q`
  Expected: every test fails (`cannot import name 'continuation'`, or `unexpected keyword`).

- [ ] **Step 3: The module skeleton.** Create `nous/brain/continuation.py`:

```python
"""F099 Phase 2: the continuation store (data and routing; the runner follows).

Phase 2b puts the data and the routing here: the inbox primitives, the
same-transaction move of an intention to ``result_ready``, owner-facing rows,
and the startup rollback. The runner, the claim, proposals and cancel are
later PRs and fill this module in. Callers use the module
(``continuation.record_result(...)``), not its names, so one monkeypatch reaches
every writer.
"""

from __future__ import annotations

# Flipped to True by PR-2e, in the commit that wires the runner into main.py.
# While it is False, main.py forces NOUS_CONTINUATION_ENABLED off: with the flag
# on and no runner, a continue result is written NULL-keyed and nothing claims it.
CONTINUATION_RUNNER_READY: bool = False
```

- [ ] **Step 4: The settings.** In `nous/config.py`, after `intentions_enabled: bool = False`:

```python
    # F099 Phase 2: continue-policy results return to Nous's own continuation
    # turn instead of the chat (spec section 4.3). Needs intentions_enabled and,
    # until PR-2e ships the runner, main.py forces it off (CONTINUATION_RUNNER_READY).
    continuation_enabled: bool = False
    # Bounds per root (section 4.6). Derived from rows, never counted in memory.
    continuation_max_depth: int = Field(default=3, ge=1)
    continuation_max_spawns_per_root: int = Field(default=12, ge=1)
    continuation_max_turns_per_root: int = Field(default=8, ge=1)
    continuation_max_tokens_per_root: int = Field(default=400000, ge=1000)
    continuation_stall_limit: int = Field(default=2, ge=1)
    # A root's TTL; a child's deadline is min(parent deadline, created + this).
    intention_root_ttl_hours: float = Field(default=72, gt=0)
    continuation_max_concurrent: int = Field(default=2, ge=1)
    # A claim waits this long after the newest result, but no longer than max_wait after the oldest.
    continuation_debounce_seconds: int = Field(default=20, ge=0)
    continuation_max_wait_seconds: int = Field(default=120, ge=0)
    # A claimed arrival's lease. The turn timeout must sit at least 60 s below it.
    continuation_lease_seconds: int = Field(default=900, ge=120)
    continuation_turn_timeout_seconds: int = Field(default=780, ge=60)
    continuation_max_attempts: int = Field(default=3, ge=1)
    intention_proposal_ttl_hours: float = Field(default=24, gt=0)
```

After `_validate_intentions_dependency` (declaration order matters: it must run after it):

```python
    @model_validator(mode="after")
    def _validate_continuation_dependency(self) -> "Settings":
        """F099 §5: a continuation needs the intentions its results belong to.
        Runs after _validate_intentions_dependency, so an intentions flag forced
        off by a missing inbox forces this off too."""
        if self.continuation_enabled and not self.intentions_enabled:
            logging.getLogger(__name__).warning(
                "NOUS_CONTINUATION_ENABLED=true needs NOUS_INTENTIONS_ENABLED=true; continuation stays OFF."
            )
            object.__setattr__(self, "continuation_enabled", False)
        return self

    @model_validator(mode="after")
    def _validate_continuation_timing(self) -> "Settings":
        """A lease shorter than the turn would release a claim under a live
        turn, and a max wait below the debounce could never be reached. Hard
        errors at any flag value (like _validate_keepalive): both are cheap to
        get right and unsafe to get wrong."""
        if self.continuation_turn_timeout_seconds > self.continuation_lease_seconds - 60:
            raise ValueError(
                "NOUS_CONTINUATION_TURN_TIMEOUT_SECONDS must be at least 60 s below NOUS_CONTINUATION_LEASE_SECONDS "
                f"({self.continuation_turn_timeout_seconds} > {self.continuation_lease_seconds} - 60)"
            )
        if self.continuation_max_wait_seconds < self.continuation_debounce_seconds:
            raise ValueError(
                "NOUS_CONTINUATION_MAX_WAIT_SECONDS must not be below NOUS_CONTINUATION_DEBOUNCE_SECONDS "
                f"({self.continuation_max_wait_seconds} < {self.continuation_debounce_seconds})"
            )
        return self
```

- [ ] **Step 5: The gate.** In `nous/main.py`, change the existing `from nous.brain import Brain` to `from nous.brain import Brain, continuation` (a top-level import is safe: `main.py` already imports `nous.brain`), and add next to `_warn_on_f098_flags` (it is called at the top of `create_components`):

```python
def _gate_continuation_flag(settings: Settings) -> None:
    """F099 Phase 2: keep NOUS_CONTINUATION_ENABLED off until the runner ships.

    With the flag on and no runner, a continue result is written keyed by its
    intention alone and nothing claims it (G6). The gate lives here and not in
    a Settings validator because config.py must not import nous.brain. PR-2e
    sets CONTINUATION_RUNNER_READY in the commit that wires the runner.
    """
    if settings.continuation_enabled and not continuation.CONTINUATION_RUNNER_READY:
        logger.warning(
            "NOUS_CONTINUATION_ENABLED=true but the continuation runner is not shipped in this build; "
            "continuation stays OFF."
        )
        object.__setattr__(settings, "continuation_enabled", False)
```

and in `create_components`, directly after `_warn_on_f098_flags(settings)`:

```python
    _gate_continuation_flag(settings)  # before any component reads the flag
```

- [ ] **Step 6: Docs.** `docs/reference/environment-variables.md`, after the `NOUS_INTENTIONS_ENABLED` row, one row per setting (default and rule in the second and third cell):

```markdown
| `NOUS_CONTINUATION_ENABLED` | `false` | F099 Phase 2. A `continue` intention's result is written keyed by the intention alone (`channel` and `session_id` NULL) and read only by Nous's own continuation turn, never by a chat turn; `report`, `none` and `remember` intentions close as `delivered` instead of `legacy`; the subtask worker's raw Telegram push and the F087 Telegram leg stand down for a `continue` source; the startup rollback does not run. Needs `NOUS_INTENTIONS_ENABLED` (otherwise a WARNING and it stays off). **Until PR-2e ships the runner, `main.py` forces it off with a WARNING** (`nous.brain.continuation.CONTINUATION_RUNNER_READY`). Passed by `docker-compose.yml` (default `false`). |
| `NOUS_CONTINUATION_MAX_DEPTH` | `3` | F099 Phase 2. Deepest lineage level a continuation may spawn (`>= 1`). |
| `NOUS_CONTINUATION_MAX_SPAWNS_PER_ROOT` | `12` | F099 Phase 2. Most spawns under one root (`>= 1`). |
| `NOUS_CONTINUATION_MAX_TURNS_PER_ROOT` | `8` | F099 Phase 2. Most continuation turns for one root (`>= 1`). |
| `NOUS_CONTINUATION_MAX_TOKENS_PER_ROOT` | `400000` | F099 Phase 2. Token budget for one root across its subtasks, DAGs and arrivals (`>= 1000`). |
| `NOUS_CONTINUATION_STALL_LIMIT` | `2` | F099 Phase 2. Consecutive arrivals without progress before the root escalates (`>= 1`). |
| `NOUS_INTENTION_ROOT_TTL_HOURS` | `72` | F099 Phase 2. A root's lifetime. A child's deadline is the earlier of its parent's and `created + this` (`> 0`). |
| `NOUS_CONTINUATION_MAX_CONCURRENT` | `2` | F099 Phase 2. Roots deciding at once (`>= 1`). |
| `NOUS_CONTINUATION_DEBOUNCE_SECONDS` | `20` | F099 Phase 2. A claim waits this long after the root's newest result (`>= 0`). |
| `NOUS_CONTINUATION_MAX_WAIT_SECONDS` | `120` | F099 Phase 2. ...but no longer than this after its oldest (`>= 0`, and not below the debounce: a startup error otherwise). |
| `NOUS_CONTINUATION_LEASE_SECONDS` | `900` | F099 Phase 2. How long a claimed arrival is held before the lease is released (`>= 120`). |
| `NOUS_CONTINUATION_TURN_TIMEOUT_SECONDS` | `780` | F099 Phase 2. A continuation turn is cancelled after this (`>= 60`, and at least 60 s below the lease: a startup error otherwise). |
| `NOUS_CONTINUATION_MAX_ATTEMPTS` | `3` | F099 Phase 2. Failed attempts before the raw results become a report (`>= 1`). |
| `NOUS_INTENTION_PROPOSAL_TTL_HOURS` | `24` | F099 Phase 2. A pending proposal expires, as a reject, after this (`> 0`). |
```

- [ ] **Step 7: Compose.** In `docker-compose.yml`, directly after the `NOUS_INTENTIONS_ENABLED` line (check first that it and `NOUS_RESULT_INBOX_ENABLED` are there: PR-1 added them), add, at the same indentation, every line with a real default (never `${VAR:-}`):

```yaml
      # F098: the rest of the result inbox and result memory (never passed before).
      - NOUS_RESULT_INBOX_DAG_SCHEDULED=${NOUS_RESULT_INBOX_DAG_SCHEDULED:-false}
      - NOUS_RESULT_MEMORY_ENABLED=${NOUS_RESULT_MEMORY_ENABLED:-false}
      - NOUS_RESULT_MEMORY_SCHEDULED=${NOUS_RESULT_MEMORY_SCHEDULED:-false}
      # F099 Phase 2: continuation (needs the intentions flag above; main.py keeps it off until the runner ships).
      - NOUS_CONTINUATION_ENABLED=${NOUS_CONTINUATION_ENABLED:-false}
      - NOUS_CONTINUATION_MAX_DEPTH=${NOUS_CONTINUATION_MAX_DEPTH:-3}
      - NOUS_CONTINUATION_MAX_SPAWNS_PER_ROOT=${NOUS_CONTINUATION_MAX_SPAWNS_PER_ROOT:-12}
      - NOUS_CONTINUATION_MAX_TURNS_PER_ROOT=${NOUS_CONTINUATION_MAX_TURNS_PER_ROOT:-8}
      - NOUS_CONTINUATION_MAX_TOKENS_PER_ROOT=${NOUS_CONTINUATION_MAX_TOKENS_PER_ROOT:-400000}
      - NOUS_CONTINUATION_STALL_LIMIT=${NOUS_CONTINUATION_STALL_LIMIT:-2}
      - NOUS_INTENTION_ROOT_TTL_HOURS=${NOUS_INTENTION_ROOT_TTL_HOURS:-72}
      - NOUS_CONTINUATION_MAX_CONCURRENT=${NOUS_CONTINUATION_MAX_CONCURRENT:-2}
      - NOUS_CONTINUATION_DEBOUNCE_SECONDS=${NOUS_CONTINUATION_DEBOUNCE_SECONDS:-20}
      - NOUS_CONTINUATION_MAX_WAIT_SECONDS=${NOUS_CONTINUATION_MAX_WAIT_SECONDS:-120}
      - NOUS_CONTINUATION_LEASE_SECONDS=${NOUS_CONTINUATION_LEASE_SECONDS:-900}
      - NOUS_CONTINUATION_TURN_TIMEOUT_SECONDS=${NOUS_CONTINUATION_TURN_TIMEOUT_SECONDS:-780}
      - NOUS_CONTINUATION_MAX_ATTEMPTS=${NOUS_CONTINUATION_MAX_ATTEMPTS:-3}
      - NOUS_INTENTION_PROPOSAL_TTL_HOURS=${NOUS_INTENTION_PROPOSAL_TTL_HOURS:-24}
```

The compose test of Step 1 (`test_compose_passes_every_phase2_setting_with_the_settings_default`) pins them.

- [ ] **Step 8: Run the tests, lint, commit**
  Run: `"$BIN/nous-test-linux.sh" "$WT" runner pg "$DB" tests/test_f099_phase2b_settings.py tests/test_config.py tests/test_fix_z_maintenance_loops.py -q`
  Expected: PASS (`test_config` may carry base failures from the developer `.env`; compare with the base).

```bash
cd "$WT"
cat > /tmp/f099-2b-2.txt <<'EOF'
feat(F099): the continuation settings and the flag gate

Adds the fourteen NOUS_CONTINUATION_* and NOUS_INTENTION_* settings, the
dependency validator (continuation needs intentions) and the timing validator
(the turn timeout sits at least 60 s below the lease). The module
nous.brain.continuation starts with CONTINUATION_RUNNER_READY = False, and
main.py forces the flag off with a WARNING while it is False, so a build with
no runner can never write a result nobody claims.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_015q4W1nke7JzGaZkC21Whng
EOF
git add nous/config.py nous/brain/continuation.py nous/main.py docs/reference/environment-variables.md docker-compose.yml tests/test_f099_phase2b_settings.py
git commit -F /tmp/f099-2b-2.txt
```

---

## Task 2b-3: Inbox primitives, owner-facing rows, the shared test support

**Files:**
- Modify: `nous/brain/continuation.py`: constants, `ResultRecorded`, `enabled`, `close_reason_for`, `owner_channel`, `insert_inbox_row`, `insert_report`, `close_delivered`, `intention_keyed`
- Modify: `nous/heart/result_inbox.py`: `ResultInboxStore.insert(session=…)` delegating to `insert_inbox_row`, `metrics()`, the PROPOSAL trailer in `format_inbox_messages`
- Create: `tests/f099_support.py`
- Create: `tests/test_f099_phase2b_inbox.py`

**Interfaces** (contract §4.7 and §4.9, names and signatures exactly):
- Consumes: Task 2b-1's table, Task 2b-2's module.
- Produces (`nous.brain.continuation`):

```python
INTENT_SESSION_PREFIX = "intent-"
SOURCE_INTENTION_REPORT = "intention_report"
MSG_REPORT, MSG_QUESTION, MSG_PROPOSAL = "REPORT", "QUESTION", "PROPOSAL"
CLOSE_DELIVERED, CLOSE_RESOLVED, CLOSE_CANCELLED, CLOSE_EXPIRED = "delivered", "resolved", "cancelled", "expired"
CLOSE_FALLBACK_REPORT, CLOSE_FAILED_REPORT = "fallback_report", "failed_report"
OUTCOME_RESOLVED, OUTCOME_FALLBACK, OUTCOME_FAILED = "resolved", "fallback_report", "failed_report"
DECISIONS = ("continue", "revise", "drop", "report", "ask")
PROPOSAL_TERMINAL = frozenset({"executed", "failed", "rejected", "expired", "cancelled"})
OPEN_STATES = ("pending", "result_ready", "deciding", "awaiting_owner")

def enabled(settings) -> bool
def close_reason_for(settings) -> str
def owner_channel(settings, origin_channel: str | None) -> str | None
async def insert_inbox_row(session, agent_id, *, source_kind, source_id, msg_type, title, body, channel=None,
                           session_id=None, source_generation=0, correlation_id=None, created_at=None,
                           intention_id=None, arrival_id=None, proposal_id=None, push_after=None,
                           delivered_at=None, delivered_session_id=None) -> UUID | None
async def insert_report(session, agent_id, *, kind, title, body, channel, intention_id, root_id, arrival_id=None,
                        proposal_id=None, push_after=None, report_id=None) -> UUID           # report_id: C2
async def close_delivered(session, agent_id, source_kind, source_id, *, with_result=True) -> UUID | None
def intention_keyed(agent_id, intention_ids) -> ColumnElement[bool]                          # C9
```

- Produces (`ResultInboxStore`): `insert(..., arrival_id=None, proposal_id=None, push_after=None, session=None) -> bool` (with a session it neither commits nor opens one).
- Produces (tests): `tests/f099_support.py` with `ON`, `CONT`, `CHAN`, `RESULT`, `RecordingBus`, the `env_factory` fixture, and the builders `make_subtask`, `finish`, `make_dag`, `dag_kwargs`, `inbox_rows`, `intention_of`, `set_intention`.

- [ ] **Step 1: The shared support.** Create `tests/f099_support.py`:

```python
"""Shared builders for the F099 Phase 2b tests.

The fixture is imported by name into a test module (``# noqa: F401``); the
builders are plain functions. Every environment gets its own agent id, so
tests never see each other's rows.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select, update

from nous.brain.intentions import IntentionSpec
from nous.config import Settings
from nous.storage.models import Intention, ResultInbox

ON = {"result_inbox_enabled": True, "intentions_enabled": True}
CONT = {**ON, "continuation_enabled": True}
CHAN = "telegram:8080"
RESULT = "Powder: 40cm overnight on the upper mountain."


class RecordingBus:
    """A bus that keeps what it was asked to emit."""

    def __init__(self) -> None:
        self.events: list = []

    async def emit(self, event) -> None:
        self.events.append(event)


@pytest.fixture
async def env_factory(db, mock_embeddings):
    """``await env_factory(**settings_overrides)``: heart, a worker pool with a mock
    HTTP client, and a recording bus, all on one fresh agent."""
    from nous.handlers.subtask_worker import SubtaskWorkerPool
    from nous.heart import Heart

    hearts = []

    async def build(**over):
        agent = f"f099-2b-{uuid.uuid4().hex[:8]}"
        values = {"telegram_bot_token": "", "telegram_chat_id": "", **over}
        settings = Settings(_env_file=None, agent_id=agent, **values)
        heart = Heart(db, settings, embedding_provider=mock_embeddings)
        hearts.append(heart)
        http = MagicMock()
        http.post = AsyncMock(return_value=SimpleNamespace(status_code=200))
        pool = SubtaskWorkerPool(MagicMock(), heart, settings, http_client=http)
        return SimpleNamespace(
            agent=agent, settings=settings, heart=heart, pool=pool, http=http, db=db, bus=RecordingBus()
        )

    yield build
    for heart in hearts:
        await heart.close()


async def make_subtask(env, *, policy: str = "continue", routed: bool = True, notify: bool = False):
    """A pending subtask with its intention. ``routed`` gives it F098's routing keys."""
    return await env.heart.subtasks.create(
        task="Check the snow report",
        parent_session_id="S1" if routed else None,
        parent_channel=CHAN if routed else None,
        notify=notify,
        intention=IntentionSpec(
            intent="Tell the user about the snow",
            origin_kind="interactive",
            wake_policy=policy,
            origin_channel=CHAN if routed else None,
        ),
    )


async def finish(env, subtask, how: str = "complete"):
    """Finish a subtask ('complete', 'empty' or 'fail') and return the fresh row."""
    if how == "complete":
        await env.heart.subtasks.complete(subtask.id, RESULT, final_outcome="completed")
    elif how == "empty":
        await env.heart.subtasks.complete(subtask.id, "", final_outcome="completed")
    else:
        await env.heart.subtasks.fail(subtask.id, "boom")
    return await env.heart.subtasks.get(subtask.id)


async def make_dag(env, *, policy: str = "continue", origin_channel: str | None = None, status: str = "completed"):
    """A terminal DAG with its intention. Returns ``(dag, store)``."""
    from nous.dag.schemas import DAGCreateRequest, DAGNodeSpec, DAGNodeType
    from nous.dag.store import DAGStore

    store = DAGStore(env.db, env.agent, env.settings)
    dag = await store.create(
        DAGCreateRequest(
            name="snow-dag",
            origin_channel=origin_channel,
            nodes=[DAGNodeSpec(name="n", type=DAGNodeType.subtask, instructions="x")],
        ),
        intention=IntentionSpec(
            intent="Summarise the alerts", origin_kind="interactive", wake_policy=policy, origin_channel=origin_channel
        ),
    )
    await store.update_dag_status(dag.id, status, result_summary="ok")
    return await store.get_dag(dag.id), store


def dag_kwargs(dag, *, origin_channel=None, origin_session_id=None, status="completed") -> dict:
    """The keyword arguments ``record_dag_result`` takes, for ``dag``."""
    return dict(
        dag_id=dag.id,
        name=dag.name,
        status=status,
        summary="ok",
        blocked=False,
        origin_channel=origin_channel,
        origin_session_id=origin_session_id,
        generation=dag.delivery_generation,
    )


async def inbox_rows(env, source_id=None) -> list[ResultInbox]:
    async with env.db.session() as s:
        query = select(ResultInbox).where(ResultInbox.agent_id == env.agent)
        if source_id is not None:
            query = query.where(ResultInbox.source_id == source_id)
        return list((await s.execute(query.order_by(ResultInbox.created_at))).scalars().all())


async def intention_of(env, source_kind: str, source_id) -> Intention | None:
    return await env.heart.intentions.get_for_source(source_kind, source_id)


async def set_intention(env, intention_id, **values) -> None:
    """Move an intention by hand, as a later PR's runner would."""
    async with env.db.session() as s:
        await s.execute(update(Intention).where(Intention.id == intention_id).values(**values))
        await s.commit()
```

- [ ] **Step 2: Write the failing tests.** Create `tests/test_f099_phase2b_inbox.py`:

```python
"""F099 Phase 2b: the inbox primitives — insert(session=), owner-facing rows, close_delivered, metrics."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from f099_support import CHAN, CONT, ON, env_factory, inbox_rows, intention_of, make_subtask  # noqa: F401
from sqlalchemy import select

from nous.brain import continuation
from nous.config import Settings
from nous.heart import result_inbox
from nous.heart.result_inbox import ResultInboxStore, format_inbox_messages
from nous.storage.models import ResultInbox


def _store(db) -> tuple[ResultInboxStore, str]:
    agent = f"f099-ib-{uuid.uuid4().hex[:8]}"
    return ResultInboxStore(db, agent), agent


async def _insert(store, **over) -> bool:
    values = dict(
        source_kind="subtask", source_id=uuid.uuid4(), msg_type="INFORM", title="t", body="b", channel=CHAN
    )
    values.update(over)
    return await store.insert(**values)


async def test_insert_with_a_session_neither_commits_nor_opens_one(db):
    store, agent = _store(db)
    source_id = uuid.uuid4()
    async with db.session() as s:
        assert await store.insert(
            session=s, source_kind="subtask", source_id=source_id, msg_type="INFORM", title="t", body="b",
            channel=CHAN,
        ) is True
        await s.rollback()
    async with db.session() as s:
        assert (await s.execute(select(ResultInbox).where(ResultInbox.source_id == source_id))).first() is None


async def test_insert_with_a_session_commits_with_the_callers_transaction(db):
    store, agent = _store(db)
    source_id = uuid.uuid4()
    async with db.session() as s:
        await store.insert(
            session=s, source_kind="subtask", source_id=source_id, msg_type="INFORM", title="t", body="b",
            channel=CHAN,
        )
        await s.commit()
    async with db.session() as s:
        assert (await s.execute(select(ResultInbox).where(ResultInbox.source_id == source_id))).scalar_one()


async def test_insert_stays_idempotent_on_the_widened_key(db):
    store, _ = _store(db)
    source_id = uuid.uuid4()
    assert await _insert(store, source_id=source_id) is True
    assert await _insert(store, source_id=source_id) is False
    assert await _insert(store, source_id=source_id, source_generation=1) is True


async def test_two_agents_insert_the_same_source_key(db):
    (a, _), (b, _) = _store(db), _store(db)
    source_id = uuid.uuid4()
    assert await _insert(a, source_id=source_id) is True
    assert await _insert(b, source_id=source_id) is True


def test_the_title_cap_matches_the_column():
    assert continuation.INBOX_TITLE_MAX == result_inbox._TITLE_MAX == 200


async def test_insert_report_writes_a_channel_keyed_owner_row(db):
    store, agent = _store(db)
    intention_id, root_id, arrival_id, proposal_id = (uuid.uuid4() for _ in range(4))
    async with db.session() as s:
        report_id = await continuation.insert_report(
            s, agent, kind=continuation.MSG_PROPOSAL, title="Send the email?", body="to a@example.com",
            channel=CHAN, intention_id=intention_id, root_id=root_id, arrival_id=arrival_id,
            proposal_id=proposal_id,
        )
        await s.commit()
    (row,) = (await _all(db, agent))
    assert row.id != report_id and row.source_id == report_id and row.source_generation == 0
    assert (row.source_kind, row.msg_type, row.channel, row.session_id) == ("intention_report", "PROPOSAL", CHAN, None)
    assert (row.intention_id, row.arrival_id, row.proposal_id, row.reply_to) == (
        intention_id, arrival_id, proposal_id, CHAN,
    )
    assert row.push_after is None and row.pushed_at is None


async def test_insert_report_takes_a_caller_report_id_and_is_idempotent_on_it(db):
    store, agent = _store(db)
    rid = uuid.uuid4()
    async with db.session() as s:
        for _ in range(2):
            returned = await continuation.insert_report(
                s, agent, kind="REPORT", title="t", body="b", channel=CHAN, intention_id=uuid.uuid4(),
                root_id=uuid.uuid4(), report_id=rid,
            )
            assert returned == rid
        await s.commit()
    assert len(await _all(db, agent)) == 1


@pytest.mark.parametrize("bad", [{"kind": "INFORM"}, {"channel": None}, {"channel": "  "}], ids=["kind", "none", "blank"])
async def test_insert_report_refuses_a_foreign_kind_or_an_empty_channel(db, bad):
    store, agent = _store(db)
    values = dict(kind="REPORT", title="t", body="b", channel=CHAN, intention_id=uuid.uuid4(), root_id=uuid.uuid4())
    values.update(bad)
    async with db.session() as s:
        with pytest.raises(ValueError):
            await continuation.insert_report(s, agent, **values)


async def test_push_after_is_stored(db):
    store, agent = _store(db)
    later = datetime(2026, 10, 7, 7, 0, tzinfo=UTC)
    async with db.session() as s:
        await continuation.insert_report(
            s, agent, kind="QUESTION", title="t", body="b", channel=CHAN, intention_id=uuid.uuid4(),
            root_id=uuid.uuid4(), push_after=later,
        )
        await s.commit()
    (row,) = await _all(db, agent)
    assert row.push_after == later


async def _all(db, agent) -> list[ResultInbox]:
    async with db.session() as s:
        return list((await s.execute(select(ResultInbox).where(ResultInbox.agent_id == agent))).scalars().all())


def test_owner_channel_prefers_the_origin_then_the_default_chat():
    s = Settings(_env_file=None, telegram_chat_id="4242")
    assert continuation.owner_channel(s, "telegram:7") == "telegram:7"
    assert continuation.owner_channel(s, None) == "telegram:4242"
    assert continuation.owner_channel(s, "  ") == "telegram:4242"
    assert continuation.owner_channel(Settings(_env_file=None, telegram_chat_id=""), None) is None


def test_enabled_needs_both_flags_and_a_real_settings_object():
    assert continuation.enabled(Settings(_env_file=None, **CONT)) is True
    assert continuation.enabled(Settings(_env_file=None, **ON)) is False
    assert continuation.enabled(object()) is False


def test_close_reason_follows_the_flag():
    assert continuation.close_reason_for(Settings(_env_file=None, **CONT)) == "delivered"
    assert continuation.close_reason_for(Settings(_env_file=None, **ON)) == "legacy"


async def test_close_delivered_closes_a_pending_intention_with_a_result_at(env_factory):
    env = await env_factory(**CONT)
    st = await make_subtask(env, policy="report")
    await env.heart.subtasks.complete(st.id, "done", final_outcome="completed")
    async with env.db.session() as s:
        found = await continuation.close_delivered(s, env.agent, "subtask", st.id)
        await s.commit()
    it = await intention_of(env, "subtask", st.id)
    assert found == it.id and (it.state, it.close_reason) == ("closed", "delivered") and it.result_at is not None


async def test_the_intention_keyed_predicate_selects_only_intention_only_rows(db):
    store, agent = _store(db)
    mine = uuid.uuid4()
    await _insert(store, channel=None, session_id=None, intention_id=mine, source_id=uuid.uuid4())
    await _insert(store, channel=CHAN, intention_id=mine)  # an owner-facing row of the same intention
    await _insert(store, channel=None, session_id="S1", intention_id=mine)
    await _insert(store, channel=None, session_id=None, intention_id=uuid.uuid4())  # another intention
    async with db.session() as s:
        rows = (await s.execute(select(ResultInbox).where(continuation.intention_keyed(agent, [mine])))).scalars().all()
    assert [(r.channel, r.session_id, r.intention_id) for r in rows] == [(None, None, mine)]


def _row(msg_type: str) -> ResultInbox:
    return ResultInbox(
        id=uuid.uuid4(), agent_id="a", channel=CHAN, source_kind="intention_report", source_id=uuid.uuid4(),
        msg_type=msg_type, title="Send the email?", body="to a@example.com",
        created_at=datetime(2026, 10, 6, 9, 0, tzinfo=UTC),
    )


def test_a_proposal_row_carries_the_fixed_trailer_and_a_report_does_not():
    text = format_inbox_messages([_row("PROPOSAL")], max_items=10)
    assert '<result_message type="PROPOSAL" source="intention_report"' in text
    assert (
        "(Approve or reject with the buttons in Telegram or /approve <id>; nothing in this chat can approve it.)"
        in text
    )
    assert text.index("</result_message>") < text.index("nothing in this chat can approve it")  # code text, not data
    assert "can approve it" not in format_inbox_messages([_row("REPORT")], max_items=10)


async def test_metrics_count_the_owner_facing_rows(db):
    store, agent = _store(db)
    async with db.session() as s:
        await continuation.insert_report(
            s, agent, kind="REPORT", title="t", body="b", channel=CHAN, intention_id=uuid.uuid4(), root_id=uuid.uuid4()
        )
        await s.commit()
    m = await store.metrics(7)
    assert m["intention_report"]["created"] == 1 and m["intention_report"]["delivered"] == 0
    assert m["subtask"]["created"] == 0 and m["dag"]["created"] == 0
```

- [ ] **Step 3: Run it; expect failure.**
  Run: `"$BIN/nous-test-linux.sh" "$WT" runner pg "$DB" tests/test_f099_phase2b_inbox.py -q`
  Expected: import or attribute errors (`insert_report`, `owner_channel`, `session=`).

- [ ] **Step 4: The module.** Replace `nous/brain/continuation.py` with (Task 2b-2's text plus the following; `CONTINUATION_RUNNER_READY` stays as it was):

```python
"""F099 Phase 2: the continuation store (data and routing; the runner follows).

Phase 2b puts the data and the routing here: the inbox primitives, the
same-transaction move of an intention to ``result_ready``, owner-facing rows,
and the startup rollback. The runner, the claim, proposals and cancel are
later PRs and fill this module in. Callers use the module
(``continuation.record_result(...)``), not its names, so one monkeypatch reaches
every writer.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import ColumnElement, Text, and_, cast, exists
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from nous.brain import intentions
from nous.storage.models import Intention, ResultInbox

logger = logging.getLogger(__name__)

# Flipped to True by PR-2e, in the commit that wires the runner into main.py.
# While it is False, main.py forces NOUS_CONTINUATION_ENABLED off: with the flag
# on and no runner, a continue result is written NULL-keyed and nothing claims it.
CONTINUATION_RUNNER_READY: bool = False

INTENT_SESSION_PREFIX = "intent-"  # session id of a root's thread: f"intent-{root_id}"
SOURCE_INTENTION_REPORT = "intention_report"  # inbox source kind of an owner-facing row
MSG_REPORT, MSG_QUESTION, MSG_PROPOSAL = "REPORT", "QUESTION", "PROPOSAL"
REPORT_KINDS = (MSG_REPORT, MSG_QUESTION, MSG_PROPOSAL)
CLOSE_DELIVERED, CLOSE_RESOLVED, CLOSE_CANCELLED, CLOSE_EXPIRED = "delivered", "resolved", "cancelled", "expired"
CLOSE_FALLBACK_REPORT, CLOSE_FAILED_REPORT = "fallback_report", "failed_report"
OUTCOME_RESOLVED, OUTCOME_FALLBACK, OUTCOME_FAILED = "resolved", "fallback_report", "failed_report"
DECISIONS = ("continue", "revise", "drop", "report", "ask")
PROPOSAL_TERMINAL = frozenset({"executed", "failed", "rejected", "expired", "cancelled"})
OPEN_STATES = ("pending", "result_ready", "deciding", "awaiting_owner")

STATE_PENDING, STATE_RESULT_READY, STATE_CLOSED = "pending", "result_ready", "closed"
STATE_CANCELLED, STATE_EXPIRED = "cancelled", "expired"

# heart.result_inbox.title is VARCHAR(200); tests pin it equal to result_inbox._TITLE_MAX.
INBOX_TITLE_MAX = 200
# The UNIQUE key of heart.result_inbox, in column order (migration 084): the one
# conflict target every insert uses.
INBOX_SOURCE_KEY = ["source_kind", "source_id", "source_generation", "agent_id"]


@dataclass(frozen=True, slots=True)
class ResultRecorded:
    """What ``record_result`` did (contract section 4.7)."""

    inbox_id: UUID | None
    inserted: bool
    state_after: str
    reopened: bool
    reported: bool
    intention_id: UUID
    root_id: UUID


def enabled(settings: Any) -> bool:
    """NOUS_CONTINUATION_ENABLED, read so that a mocked Settings counts as off.

    Also needs intentions: the validator forces both, this re-checks for a
    settings object that bypassed it."""
    return getattr(settings, "continuation_enabled", False) is True and intentions.enabled(settings)


def close_reason_for(settings: Any) -> str:
    """The close reason of a ``none``, ``remember`` or ``report`` intention (ruling 2):
    ``delivered`` with the flag on, ``legacy`` (Phase 1's) otherwise."""
    return CLOSE_DELIVERED if enabled(settings) else intentions.CLOSE_LEGACY


def owner_channel(settings: Any, origin_channel: str | None) -> str | None:
    """Where an owner-facing row goes: the intention's origin channel, else the
    default chat; None when neither exists (nothing can be routed)."""
    if origin_channel and origin_channel.strip():
        return origin_channel.strip()
    chat_id = str(getattr(settings, "telegram_chat_id", "") or "").strip()
    return f"telegram:{chat_id}" if chat_id else None


def intention_keyed(agent_id: str, intention_ids: Any) -> ColumnElement[bool]:
    """The inbox rows only the continuation may read: keyed by one of
    ``intention_ids`` alone. Owner-facing rows also carry an ``intention_id``
    but have a channel, so chat reads them and this does not (contract C9)."""
    return and_(
        ResultInbox.agent_id == agent_id,
        ResultInbox.intention_id.in_(list(intention_ids)),
        ResultInbox.channel.is_(None),
        ResultInbox.session_id.is_(None),
    )


def has_continue_intention(
    agent_id: str, source_kind: str, source_id_col: Any, *, include_closed: bool = False
) -> ColumnElement[bool]:
    """EXISTS: the work row (``source_id_col``, a uuid column of a subtask or DAG)
    has a ``continue`` intention that is still owed a result. The reconciler
    passes use it so a row keyed by the intention alone is still repaired. A
    closed intention counts only when ``include_closed`` (a DAG's retry re-arrives)."""
    states = OPEN_STATES + ((STATE_CLOSED,) if include_closed else ())
    return exists().where(
        Intention.agent_id == agent_id,
        Intention.source_kind == source_kind,
        Intention.source_id == cast(source_id_col, Text),
        Intention.wake_policy == intentions.WAKE_CONTINUE,
        Intention.state.in_(states),
    )


async def insert_inbox_row(
    session: AsyncSession,
    agent_id: str,
    *,
    source_kind: str,
    source_id: UUID,
    msg_type: str,
    title: str,
    body: str,
    channel: str | None = None,
    session_id: str | None = None,
    source_generation: int = 0,
    correlation_id: str | None = None,
    created_at: datetime | None = None,
    intention_id: UUID | None = None,
    arrival_id: UUID | None = None,
    proposal_id: UUID | None = None,
    push_after: datetime | None = None,
    delivered_at: datetime | None = None,
    delivered_session_id: str | None = None,
) -> UUID | None:
    """The one INSERT into ``heart.result_inbox``, in the caller's transaction.

    Returns the new row's id, or None when the UNIQUE key already had the row
    (every writer is idempotent). Does not commit. ``delivered_at`` writes a row
    already settled (the source-keyed row of a result that became a report).
    """
    row_id = uuid.uuid4()
    stmt = (
        pg_insert(ResultInbox)
        .values(
            id=row_id,
            agent_id=agent_id,
            channel=channel,
            session_id=session_id,
            source_kind=source_kind,
            source_id=source_id,
            source_generation=source_generation,
            msg_type=msg_type,
            correlation_id=correlation_id,
            reply_to=channel,
            title=title[:INBOX_TITLE_MAX],
            body=body,
            created_at=created_at or datetime.now(UTC),
            intention_id=intention_id,
            arrival_id=arrival_id,
            proposal_id=proposal_id,
            push_after=push_after,
            delivered_at=delivered_at,
            delivered_session_id=delivered_session_id,
        )
        .on_conflict_do_nothing(index_elements=INBOX_SOURCE_KEY)
    )
    result = await session.execute(stmt)
    return row_id if result.rowcount else None


async def insert_report(
    session: AsyncSession,
    agent_id: str,
    *,
    kind: str,
    title: str,
    body: str,
    channel: str,
    intention_id: UUID,
    root_id: UUID,
    arrival_id: UUID | None = None,
    proposal_id: UUID | None = None,
    push_after: datetime | None = None,
    report_id: UUID | None = None,
) -> UUID:
    """An owner-facing row (REPORT, QUESTION or PROPOSAL) in the caller's transaction.

    Keyed to ``channel`` (never NULL: section 4.3 item 4); its ``source_id`` is
    ``report_id`` (a fresh uuid unless the caller needs the write to be
    idempotent) and its generation 0. Returns ``report_id``. ``root_id`` is for
    the log only: the table has no root column (contract C2).
    """
    if kind not in REPORT_KINDS:
        raise ValueError(f"an owner-facing row is one of {REPORT_KINDS}, not {kind!r}")
    rid = report_id or uuid.uuid4()
    await _insert_report_row(
        session,
        agent_id,
        rid,
        kind=kind,
        title=title,
        body=body,
        channel=channel,
        intention_id=intention_id,
        root_id=root_id,
        arrival_id=arrival_id,
        proposal_id=proposal_id,
        push_after=push_after,
    )
    return rid


async def _insert_report_row(
    session: AsyncSession,
    agent_id: str,
    report_id: UUID,
    *,
    kind: str,
    title: str,
    body: str,
    channel: str,
    intention_id: UUID,
    root_id: UUID,
    arrival_id: UUID | None = None,
    proposal_id: UUID | None = None,
    push_after: datetime | None = None,
    created_at: datetime | None = None,
) -> UUID | None:
    """The row of ``insert_report``; its id, or None when ``report_id`` was already written."""
    if not channel or not channel.strip():
        raise ValueError("an owner-facing row needs a channel (spec section 4.3 item 4)")
    row_id = await insert_inbox_row(
        session,
        agent_id,
        source_kind=SOURCE_INTENTION_REPORT,
        source_id=report_id,
        msg_type=kind,
        title=title,
        body=body,
        channel=channel,
        correlation_id=str(report_id),
        created_at=created_at,
        intention_id=intention_id,
        arrival_id=arrival_id,
        proposal_id=proposal_id,
        push_after=push_after,
    )
    logger.info("F099: %s row %s for intention %s (root %s)", kind, report_id.hex[:8], intention_id, root_id)
    return row_id


async def close_delivered(
    session: AsyncSession, agent_id: str, source_kind: str, source_id: Any, *, with_result: bool = True
) -> UUID | None:
    """T3: close the pending intention of a finished source as ``delivered``, in the
    caller's transaction. Its id, or None when the source recorded none."""
    return await intentions.close_for_source(
        session, agent_id, source_kind, source_id, reason=CLOSE_DELIVERED, with_result=with_result
    )
```

- [ ] **Step 5: The store.** In `nous/heart/result_inbox.py`:

Imports: change `from nous.brain import intentions` to `from nous.brain import continuation, intentions`, and add `from sqlalchemy.ext.asyncio import AsyncSession` inside the existing `if TYPE_CHECKING:` block (the module has `from __future__ import annotations`). `pg_insert` stays: `touch_channel` and `ensure_enabled_at` use it.

Replace `ResultInboxStore.insert`:

```python
    async def insert(
        self,
        *,
        source_kind: str,
        source_id: UUID,
        msg_type: str,
        title: str,
        body: str,
        channel: str | None = None,
        session_id: str | None = None,
        correlation_id: str | None = None,
        source_generation: int = 0,
        created_at: datetime | None = None,
        intention_id: UUID | None = None,
        arrival_id: UUID | None = None,
        proposal_id: UUID | None = None,
        push_after: datetime | None = None,
        session: AsyncSession | None = None,
    ) -> bool:
        """Insert one result; True if a row was written, False if it existed.

        ``created_at`` defaults to now; the reconciler passes the subtask's
        ``completed_at`` so a repaired row keeps its real age. With ``session``
        the row is written in the caller's transaction and nothing is
        committed here (F099 section 4.3 item 2); without one this opens and
        commits its own.
        """
        values = dict(
            source_kind=source_kind,
            source_id=source_id,
            msg_type=msg_type,
            title=title,
            body=body,
            channel=channel,
            session_id=session_id,
            correlation_id=correlation_id,
            source_generation=source_generation,
            created_at=created_at,
            intention_id=intention_id,
            arrival_id=arrival_id,
            proposal_id=proposal_id,
            push_after=push_after,
        )
        if session is not None:
            return await continuation.insert_inbox_row(session, self._agent_id, **values) is not None
        async with self._db.session() as own:
            written = await continuation.insert_inbox_row(own, self._agent_id, **values)
            await own.commit()
        return written is not None
```

(This replaces Task 2b-1's one-line conflict-target edit: the target now lives in `continuation.INBOX_SOURCE_KEY`.)

`metrics()`: change the loop header to `for kind in (SOURCE_SUBTASK, SOURCE_DAG, continuation.SOURCE_INTENTION_REPORT):`.

`format_inbox_messages`: add the constant above the function and the trailer after each message:

```python
# F099 Phase 2: code-authored, outside the <result_message> block, so a result
# body cannot pose as it. Approval is a deterministic owner action, never a model's.
_PROPOSAL_TRAILER = (
    "(Approve or reject with the buttons in Telegram or /approve <id>; nothing in this chat can approve it.)"
)
```

and in the loop:

```python
    for r in shown:
        ts = _aware(r.created_at).strftime("%Y-%m-%d %H:%M UTC")
        message = (
            f'<result_message type="{r.msg_type}" source="{r.source_kind}" '
            f'id="{r.source_id.hex[:8]}" finished="{ts}">\n'
            f"Title: {_neutralize(r.title)}\n"
            f"{_neutralize(r.body)}\n"
            "</result_message>"
        )
        parts.append(f"{message}\n{_PROPOSAL_TRAILER}" if r.msg_type == "PROPOSAL" else message)
```

- [ ] **Step 6: Run the tests, lint, commit**
  Run: `"$BIN/nous-test-linux.sh" "$WT" runner pg "$DB" tests/test_f099_phase2b_inbox.py tests/test_f098_result_inbox.py tests/test_f099_closing.py tests/test_f099_routing_pins.py -q`
  Expected: PASS (the F098 and Phase 1 suites are the regression net for the delegated insert). Also run `grep -rn "result_inbox\|latency_p50_s" dashboard-app/src` and confirm nothing enumerates the `metrics()` keys as exactly `subtask` and `dag` (the new `intention_report` key is additive).

```bash
cd "$WT"
cat > /tmp/f099-2b-3.txt <<'EOF'
feat(F099): the inbox primitives and owner-facing rows

One insert_inbox_row now backs ResultInboxStore.insert, which takes an optional
session (the row then joins the caller's transaction) and the arrival, proposal
and push columns. Adds insert_report for REPORT, QUESTION and PROPOSAL rows,
close_delivered, close_reason_for, owner_channel and the intention_keyed
predicate, counts the new source kind in metrics(), and renders a fixed
approval trailer under a PROPOSAL row.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_015q4W1nke7JzGaZkC21Whng
EOF
git add nous/brain/continuation.py nous/heart/result_inbox.py tests/f099_support.py tests/test_f099_phase2b_inbox.py
git commit -F /tmp/f099-2b-3.txt
```

---

## Task 2b-4: The same-transaction move to `result_ready`

**Files:**
- Modify: `nous/brain/continuation.py`: `record_result`, `arrival_report_id`, `_set_result_ready`, `_root_is_open`
- Modify: `nous/heart/result_inbox.py`: `ResultInboxStore.set_bus`, `bus`, `intention_of`, `record_continue_result`, `_emit_result_ready`
- Create: `tests/test_f099_phase2b_record_result.py`

**Interfaces** (contract §4.7 and §4.9, names exactly):
- Consumes: Task 2b-3's `insert_inbox_row`, `_insert_report_row`, `owner_channel`, `ResultRecorded`.
- Produces:

```python
async def record_result(session, agent_id, *, intention_id, source_kind, source_id, msg_type, title, body,
                        source_generation=0, correlation_id=None, created_at=None, arrival_id=None,
                        settings) -> ResultRecorded                  # arrival_id: 2d's record_answer (SF-2)
def arrival_report_id(source_kind: str, source_id, generation: int) -> UUID      # uuid5: the report's idempotency
```

  One transaction, the intention row locked `FOR UPDATE` first:

  | The intention | The row | The state |
  |---|---|---|
  | `pending`, root open | NULL-keyed, `intention_id` | `result_ready`, `result_at = now` (T4) |
  | `closed`, `continue`, root open | NULL-keyed | `result_ready`, `close_reason` and `closed_at` cleared (T6: reopen) |
  | `result_ready`, `deciding`, `awaiting_owner` | NULL-keyed | unchanged (held: §4.3 items 2 and 3) |
  | any other policy, or a root cancelled or expired, or `cancelled`/`expired` | an `intention_report` REPORT keyed to `owner_channel`, raw body, **and** the source-keyed row (NULL channel and session, stamped delivered in the same transaction, MF-1) | unchanged |

  The source-keyed row of the last line is what makes `has_row` true for the F098 reconciler passes, so they stop re-selecting the work row on every tick, and (NULL-keyed and delivered) no chat turn can claim it; the raw result stays findable. It is written even when there is no owner channel (then there is no report, and the warning says so).

  The state `UPDATE` runs only when the INSERT wrote a row, so a duplicate delivery never moves a state. Emits nothing: the caller emits after commit.
- Produces (`ResultInboxStore`): `set_bus(bus)`, `bus` (property), `intention_of(source_kind, source_id) -> Intention | None`, `record_continue_result(*, intention_id, source_kind, source_id, generation, envelope, correlation_id, created_at, settings, arrival_id=None) -> ResultRecorded` (opens its own session, commits, then emits `intention.result_ready` with `{intention_id, root_id, agent_id}` when the state moved or stayed `result_ready` and a row was written).

- [ ] **Step 1: Write the failing tests.** Create `tests/test_f099_phase2b_record_result.py`:

```python
"""F099 Phase 2b: record_result — the same-transaction move, held rows, re-arrivals, reports."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from f099_support import CHAN, CONT, RESULT, env_factory, inbox_rows, intention_of, make_subtask, set_intention  # noqa: F401

from nous.brain import continuation
from nous.heart.result_inbox import Envelope


async def _record(env, st, *, generation: int = 0, body: str = RESULT, **over):
    it = await intention_of(env, "subtask", st.id)
    async with env.db.session() as s:
        recorded = await continuation.record_result(
            s,
            env.agent,
            intention_id=it.id,
            source_kind="subtask",
            source_id=st.id,
            msg_type="INFORM",
            title="Check the snow report",
            body=body,
            source_generation=generation,
            settings=env.settings,
            **over,
        )
        await s.commit()
    return recorded


async def test_a_pending_continue_intention_moves_to_result_ready_with_its_row(env_factory):
    env = await env_factory(**CONT)
    st = await make_subtask(env)
    recorded = await _record(env, st)
    it = await intention_of(env, "subtask", st.id)
    (row,) = await inbox_rows(env, st.id)
    assert (recorded.inserted, recorded.state_after, recorded.reopened, recorded.reported) == (
        True, "result_ready", False, False,
    )
    assert recorded.inbox_id == row.id and recorded.intention_id == it.id and recorded.root_id == it.root_id
    assert (row.channel, row.session_id, row.intention_id, row.source_kind) == (None, None, it.id, "subtask")
    assert (it.state, it.close_reason) == ("result_ready", None) and it.result_at is not None


async def test_a_fault_after_the_insert_leaves_neither_row_nor_move(env_factory, monkeypatch):
    """The row and the move are one transaction: a fault between them leaves the
    intention pending and no row, and the next arrival succeeds."""
    env = await env_factory(**CONT)
    st = await make_subtask(env)
    real = continuation._set_result_ready

    async def boom(*args, **kwargs):
        raise RuntimeError("fault between the INSERT and the UPDATE")

    monkeypatch.setattr(continuation, "_set_result_ready", boom)
    with pytest.raises(RuntimeError, match="fault between"):
        await _record(env, st)
    assert await inbox_rows(env, st.id) == []
    assert (await intention_of(env, "subtask", st.id)).state == "pending"
    monkeypatch.setattr(continuation, "_set_result_ready", real)
    assert (await _record(env, st)).state_after == "result_ready"
    assert len(await inbox_rows(env, st.id)) == 1


async def test_a_duplicate_arrival_changes_nothing(env_factory):
    env = await env_factory(**CONT)
    st = await make_subtask(env)
    assert (await _record(env, st)).inserted is True
    it = await intention_of(env, "subtask", st.id)
    await set_intention(env, it.id, state="closed", close_reason="resolved", closed_at=datetime.now(UTC))
    again = await _record(env, st)  # the same (kind, id, generation): the listener and deliver both write
    assert (again.inserted, again.reopened, again.state_after, again.inbox_id) == (False, False, "closed", None)
    assert (await intention_of(env, "subtask", st.id)).state == "closed"
    assert len(await inbox_rows(env, st.id)) == 1


@pytest.mark.parametrize("state", ["awaiting_owner", "deciding", "result_ready"])
async def test_rows_arriving_while_awaiting_or_deciding_are_held(env_factory, state):
    """Spec section 4.3 items 2 and 3: the row is inserted and the intention is left alone."""
    env = await env_factory(**CONT)
    st = await make_subtask(env)
    it = await intention_of(env, "subtask", st.id)
    await set_intention(env, it.id, state=state)
    recorded = await _record(env, st, generation=1)
    assert (recorded.inserted, recorded.state_after, recorded.reopened, recorded.reported) == (
        True, state, False, False,
    )
    (row,) = await inbox_rows(env, st.id)
    assert (row.channel, row.session_id, row.intention_id) == (None, None, it.id)
    assert (await intention_of(env, "subtask", st.id)).state == state


async def test_a_closed_continue_intention_with_an_open_root_reopens(env_factory):
    env = await env_factory(**CONT)
    st = await make_subtask(env)
    await _record(env, st)
    it = await intention_of(env, "subtask", st.id)
    await set_intention(env, it.id, state="closed", close_reason="resolved", closed_at=datetime.now(UTC))
    recorded = await _record(env, st, generation=1)  # a retried DAG, a decided proposal, an answer
    after = await intention_of(env, "subtask", st.id)
    assert (recorded.inserted, recorded.reopened, recorded.state_after) == (True, True, "result_ready")
    assert (after.state, after.close_reason, after.closed_at) == ("result_ready", None, None)
    assert len(await inbox_rows(env, st.id)) == 2


def _split(rows):
    """(the source-keyed rows, the intention_report rows) of an environment's inbox."""
    return (
        [r for r in rows if r.source_kind != "intention_report"],
        [r for r in rows if r.source_kind == "intention_report"],
    )


@pytest.mark.parametrize("marker", ["root_cancelled_at", "root_expired_at"])
async def test_a_re_arrival_on_a_closed_root_becomes_an_intention_report(env_factory, marker):
    env = await env_factory(**CONT)
    st = await make_subtask(env)
    it = await intention_of(env, "subtask", st.id)
    await set_intention(env, it.id, state="closed", close_reason="resolved", **{marker: datetime.now(UTC)})
    recorded = await _record(env, st, generation=1, body="the raw result")
    stamped, (row,) = _split(await inbox_rows(env))  # the report's source_id is its own, not the subtask's
    assert (recorded.inserted, recorded.reported, recorded.reopened, recorded.state_after) == (
        True, True, False, "closed",
    )
    assert (row.source_kind, row.msg_type, row.channel, row.session_id) == ("intention_report", "REPORT", CHAN, None)
    assert row.body == "the raw result" and row.intention_id == it.id
    assert row.source_id == continuation.arrival_report_id("subtask", st.id, 1)
    assert (await intention_of(env, "subtask", st.id)).state == "closed"  # a closed root is never reopened
    again = await _record(env, st, generation=1, body="the raw result")  # the second writer of the same outcome
    assert (again.inserted, again.reported) == (False, False)
    assert len(await inbox_rows(env)) == 2  # the report and its work row's settled twin, once each
    assert len(stamped) == 1


async def test_a_reported_result_settles_its_work_row_for_the_reconciler_passes(env_factory):
    """MF-1. The F098 passes decide 'needs repair' by a source-keyed row (has_row). A result that became a
    report must leave one, NULL-keyed and already delivered, or the passes re-select the work row on every
    tick (the pass-level pin is in Task 2b-7). It must never be claimable by a chat turn."""
    env = await env_factory(**CONT)
    st = await make_subtask(env)
    it = await intention_of(env, "subtask", st.id)
    await set_intention(env, it.id, state="closed", close_reason="resolved", root_cancelled_at=datetime.now(UTC))
    await _record(env, st, generation=0)
    (stamped,), _ = _split(await inbox_rows(env))
    assert (stamped.source_kind, stamped.source_id, stamped.source_generation) == ("subtask", st.id, 0)
    assert (stamped.channel, stamped.session_id, stamped.intention_id) == (None, None, it.id)
    assert stamped.delivered_at is not None and stamped.delivered_session_id.startswith("report:")
    rows, _ = await env.heart.result_inbox.claim(channel=CHAN, session_id="S1", max_age_hours=72, max_items=10)
    assert [r.source_kind for r in rows] == ["intention_report"]  # the report only, never the settled twin


async def test_a_cancelled_intention_reports_instead_of_waking(env_factory):
    env = await env_factory(**CONT)
    st = await make_subtask(env)
    it = await intention_of(env, "subtask", st.id)
    await set_intention(env, it.id, state="cancelled")
    recorded = await _record(env, st)
    assert (recorded.reported, recorded.state_after) == (True, "cancelled")


async def test_a_result_with_no_owner_channel_writes_only_the_settled_work_row(env_factory, caplog):
    env = await env_factory(**CONT)  # no default chat configured
    st = await make_subtask(env, routed=False)  # and no origin channel
    it = await intention_of(env, "subtask", st.id)
    await set_intention(env, it.id, root_cancelled_at=datetime.now(UTC))
    recorded = await _record(env, st)
    assert (recorded.inserted, recorded.reported) == (False, False)
    stamped, reports = _split(await inbox_rows(env))
    assert reports == [] and len(stamped) == 1 and stamped[0].delivered_at is not None
    assert "no owner channel" in caplog.text


async def test_a_report_falls_back_to_the_default_chat(env_factory):
    env = await env_factory(**CONT, telegram_chat_id="4242")
    st = await make_subtask(env, routed=False)
    it = await intention_of(env, "subtask", st.id)
    await set_intention(env, it.id, root_expired_at=datetime.now(UTC))
    await _record(env, st)
    _, (row,) = _split(await inbox_rows(env))
    assert row.channel == "telegram:4242"


async def test_a_non_continue_intention_reports_directly(env_factory):
    """Contract C8: the spec's 'any other policy' rule, applied by record_result itself."""
    env = await env_factory(**CONT)
    st = await make_subtask(env, policy="report")
    recorded = await _record(env, st)
    _, (row,) = _split(await inbox_rows(env))
    assert (recorded.reported, recorded.state_after) == (True, "pending")
    assert (row.source_kind, row.channel) == ("intention_report", CHAN)
    assert (await intention_of(env, "subtask", st.id)).state == "pending"


async def test_an_arrival_id_reaches_the_row(env_factory):
    """SF-2: 2d's record_answer passes the question's arrival."""
    import uuid

    env = await env_factory(**CONT)
    st = await make_subtask(env)
    arrival = uuid.uuid4()
    await _record(env, st, arrival_id=arrival)
    (row,) = await inbox_rows(env, st.id)
    assert row.arrival_id == arrival


async def test_an_unknown_intention_raises(env_factory):
    import uuid

    env = await env_factory(**CONT)
    st = await make_subtask(env)
    async with env.db.session() as s:
        with pytest.raises(LookupError):
            await continuation.record_result(
                s, env.agent, intention_id=uuid.uuid4(), source_kind="subtask", source_id=st.id, msg_type="INFORM",
                title="t", body="b", settings=env.settings,
            )


@pytest.mark.postgres_only  # two writers contend for one FOR UPDATE row lock
async def test_two_writers_on_one_intention_both_land_and_move_it_once(env_factory):
    env = await env_factory(**CONT)
    st = await make_subtask(env)
    first, second = await asyncio.gather(_record(env, st, generation=0), _record(env, st, generation=1))
    assert first.inserted and second.inserted
    assert {first.state_after, second.state_after} == {"result_ready"}
    assert len(await inbox_rows(env, st.id)) == 2
    assert (await intention_of(env, "subtask", st.id)).state == "result_ready"


async def test_the_store_emits_result_ready_after_the_commit(env_factory):
    env = await env_factory(**CONT)
    store = env.heart.result_inbox
    store.set_bus(env.bus)
    assert store.bus is env.bus
    st = await make_subtask(env)
    it = await intention_of(env, "subtask", st.id)
    kwargs = dict(
        intention_id=it.id, source_kind="subtask", source_id=st.id, correlation_id=None, created_at=None,
        settings=env.settings,
    )
    first = await store.record_continue_result(generation=0, envelope=Envelope("INFORM", "t", "b"), **kwargs)
    assert first.state_after == "result_ready"
    (event,) = env.bus.events
    assert event.type == "intention.result_ready"
    assert event.data == {"intention_id": str(it.id), "root_id": str(it.root_id), "agent_id": env.agent}
    await store.record_continue_result(generation=0, envelope=Envelope("INFORM", "t", "b"), **kwargs)  # duplicate
    assert len(env.bus.events) == 1


async def test_a_held_row_and_a_report_emit_nothing(env_factory):
    env = await env_factory(**CONT)
    store = env.heart.result_inbox
    store.set_bus(env.bus)
    st = await make_subtask(env)
    it = await intention_of(env, "subtask", st.id)
    await set_intention(env, it.id, state="awaiting_owner")
    kwargs = dict(
        intention_id=it.id, source_kind="subtask", source_id=st.id, generation=1, correlation_id=None,
        created_at=None, envelope=Envelope("INFORM", "t", "b"), settings=env.settings,
    )
    await store.record_continue_result(**kwargs)
    await set_intention(env, it.id, state="closed", root_cancelled_at=datetime.now(UTC))
    await store.record_continue_result(**{**kwargs, "generation": 2})
    assert env.bus.events == []


async def test_a_bus_failure_never_fails_the_write(env_factory):
    class _BrokenBus:
        async def emit(self, event):
            raise RuntimeError("bus down")

    env = await env_factory(**CONT)
    store = env.heart.result_inbox
    store.set_bus(_BrokenBus())
    st = await make_subtask(env)
    it = await intention_of(env, "subtask", st.id)
    recorded = await store.record_continue_result(
        intention_id=it.id, source_kind="subtask", source_id=st.id, generation=0, correlation_id=None,
        created_at=None, envelope=Envelope("INFORM", "t", "b"), settings=env.settings,
    )
    assert recorded.inserted is True and (await intention_of(env, "subtask", st.id)).state == "result_ready"
```

- [ ] **Step 2: Run it; expect failure.**
  Run: `"$BIN/nous-test-linux.sh" "$WT" runner pg "$DB" tests/test_f099_phase2b_record_result.py -q`
  Expected: `AttributeError: module 'nous.brain.continuation' has no attribute 'record_result'`.

- [ ] **Step 3: The store function.** In `nous/brain/continuation.py` add `select, update` to the sqlalchemy import (`from sqlalchemy import ColumnElement, Text, and_, cast, exists, select, update`) and append:

```python
# A fixed namespace: the report of an arrival nothing can reopen has a
# deterministic id, so the DAG bus listener and DAGResultDelivery.deliver, which
# both reach the writer, collapse on the inbox's UNIQUE key (contract C2).
_REPORT_NAMESPACE = uuid.UUID("5d0c7e1e-6a7b-4f0e-9a52-0f0990b2c3d4")


def arrival_report_id(source_kind: str, source_id: Any, generation: int) -> UUID:
    """The ``source_id`` of the ``intention_report`` a re-arrival becomes."""
    return uuid.uuid5(_REPORT_NAMESPACE, f"{source_kind}:{source_id}:{int(generation)}")


async def _root_is_open(session: AsyncSession, agent_id: str, root_id: UUID) -> bool:
    """A root is open while neither root marker is set. A separate statement from the
    intention's lock: under READ COMMITTED it sees a cancel that committed first."""
    markers = (
        await session.execute(
            select(Intention.root_cancelled_at, Intention.root_expired_at).where(
                Intention.agent_id == agent_id, Intention.id == root_id
            )
        )
    ).first()
    return markers is not None and markers.root_cancelled_at is None and markers.root_expired_at is None


async def _set_result_ready(
    session: AsyncSession, agent_id: str, intention_id: UUID, *, from_state: str, now: datetime
) -> None:
    """T4 (``pending``) and T6 (``closed``, a reopen): the conditional UPDATE. The caller holds
    the row lock, so a miss is a bug, not a race: it raises and the caller's transaction rolls back."""
    values: dict[str, Any] = {"state": STATE_RESULT_READY, "result_at": now, "updated_at": now}
    if from_state == STATE_CLOSED:
        values.update(close_reason=None, closed_at=None)
    moved = (
        await session.execute(
            update(Intention)
            .where(
                Intention.agent_id == agent_id,
                Intention.id == intention_id,
                Intention.state == from_state,
                Intention.wake_policy == intentions.WAKE_CONTINUE,
            )
            .values(**values)
            .returning(Intention.id)
            .execution_options(synchronize_session=False)
        )
    ).scalar_one_or_none()
    if moved is None:
        raise RuntimeError(f"intention {intention_id} left {from_state!r} while its row was locked")


async def record_result(
    session: AsyncSession,
    agent_id: str,
    *,
    intention_id: UUID,
    source_kind: str,
    source_id: UUID,
    msg_type: str,
    title: str,
    body: str,
    source_generation: int = 0,
    correlation_id: str | None = None,
    created_at: datetime | None = None,
    arrival_id: UUID | None = None,
    settings: Any,
) -> ResultRecorded:
    """The one Phase 2 writer for a ``continue`` result, in the caller's transaction (spec 4.3).

    Locks the intention ``FOR UPDATE``, then: a ``continue`` intention with an open root gets a row
    keyed by the intention alone (``channel`` and ``session_id`` NULL) and, if it was ``pending`` (T4)
    or ``closed`` (T6, a reopen), moves to ``result_ready`` in the same transaction. A row arriving
    while the intention is ``result_ready``, ``deciding`` or ``awaiting_owner`` is inserted and held:
    the state is left alone. Anything nothing can reopen (another policy, a closed root, a
    ``cancelled`` or ``expired`` intention) becomes an owner-facing ``intention_report`` carrying the
    raw result, plus the work row's own inbox row, NULL-keyed and stamped delivered, so the reconciler
    passes see the source as written. The state UPDATE runs only when the row was written, so a
    duplicate delivery is a no-op. ``arrival_id`` is the arrival an owner answer belongs to (2d).
    Emits nothing: the caller emits ``intention.result_ready`` after it commits.
    """
    row = (
        await session.execute(
            select(Intention).where(Intention.agent_id == agent_id, Intention.id == intention_id).with_for_update()
        )
    ).scalar_one_or_none()
    if row is None:
        raise LookupError(f"intention {intention_id} does not exist for agent {agent_id}")
    state, policy, root_id, origin_channel = row.state, row.wake_policy, row.root_id, row.origin_channel
    now = datetime.now(UTC)

    if (
        policy != intentions.WAKE_CONTINUE
        or state in (STATE_CANCELLED, STATE_EXPIRED)
        or not await _root_is_open(session, agent_id, root_id)
    ):
        report_id = arrival_report_id(source_kind, source_id, source_generation)
        # MF-1: the work row's own inbox row, NULL-keyed and already delivered. The F098 reconciler passes
        # decide "needs repair" by this row (has_row): without it they would re-select the work row on
        # every tick for good. NULL-keyed and delivered, no chat turn can claim it.
        await insert_inbox_row(
            session,
            agent_id,
            source_kind=source_kind,
            source_id=source_id,
            msg_type=msg_type,
            title=title,
            body=body,
            channel=None,
            session_id=None,
            source_generation=source_generation,
            correlation_id=correlation_id,
            created_at=created_at,
            intention_id=intention_id,
            arrival_id=arrival_id,
            delivered_at=now,
            delivered_session_id=f"report:{report_id.hex[:8]}",
        )
        channel = owner_channel(settings, origin_channel)
        if channel is None:
            logger.warning(
                "F099: a result of %s %s has no owner channel (intention %s: no origin channel, no default chat); "
                "it stays on its work row",
                source_kind,
                str(source_id)[:8],
                intention_id,
            )
            return ResultRecorded(None, False, state, False, False, intention_id, root_id)
        report_row = await _insert_report_row(
            session,
            agent_id,
            report_id,
            kind=MSG_REPORT,
            title=title,
            body=body,
            channel=channel,
            intention_id=intention_id,
            root_id=root_id,
            arrival_id=arrival_id,
            created_at=created_at,
        )
        wrote = report_row is not None
        return ResultRecorded(report_row, wrote, state, False, wrote, intention_id, root_id)

    inbox_id = await insert_inbox_row(
        session,
        agent_id,
        source_kind=source_kind,
        source_id=source_id,
        msg_type=msg_type,
        title=title,
        body=body,
        channel=None,
        session_id=None,
        source_generation=source_generation,
        correlation_id=correlation_id,
        created_at=created_at,
        intention_id=intention_id,
        arrival_id=arrival_id,
    )
    if inbox_id is None:
        return ResultRecorded(None, False, state, False, False, intention_id, root_id)
    reopened = state == STATE_CLOSED
    if state in (STATE_PENDING, STATE_CLOSED):
        await _set_result_ready(session, agent_id, intention_id, from_state=state, now=now)
        state = STATE_RESULT_READY
    return ResultRecorded(inbox_id, True, state, reopened, False, intention_id, root_id)
```

- [ ] **Step 4: The store's side.** In `nous/heart/result_inbox.py`:

Add `Intention` to the models import (`from nous.storage.models import ChannelSession, Intention, ResultInbox, ResultInboxState, Subtask`). In `ResultInboxStore.__init__` add `self._bus: EventBus | None = None`, and after `__init__` add:

```python
    def set_bus(self, bus: EventBus | None) -> None:
        """F099: the bus ``intention.result_ready`` goes out on. main.py wires it once."""
        self._bus = bus

    @property
    def bus(self) -> EventBus | None:
        return self._bus

    async def intention_of(self, source_kind: str, source_id: Any) -> Intention | None:
        """F099: the intention of a finished source, or None (a source from before the flag)."""
        return await intentions.IntentionStore(self._db, self._agent_id).get_for_source(source_kind, source_id)

    async def record_continue_result(
        self,
        *,
        intention_id: UUID,
        source_kind: str,
        source_id: UUID,
        generation: int,
        envelope: Envelope,
        correlation_id: str | None,
        created_at: datetime | None,
        settings: Settings,
        arrival_id: UUID | None = None,
    ) -> continuation.ResultRecorded:
        """F099 Phase 2: write a ``continue`` result through ``continuation.record_result`` in
        one transaction, then (after the commit) tell the runner there is work."""
        async with self._db.session() as session:
            recorded = await continuation.record_result(
                session,
                self._agent_id,
                intention_id=intention_id,
                source_kind=source_kind,
                source_id=source_id,
                msg_type=envelope.msg_type,
                title=envelope.title,
                body=envelope.body,
                source_generation=generation,
                correlation_id=correlation_id,
                created_at=created_at,
                arrival_id=arrival_id,
                settings=settings,
            )
            await session.commit()
        await self._emit_result_ready(recorded)
        return recorded

    async def _emit_result_ready(self, recorded: continuation.ResultRecorded) -> None:
        """A hint only (the bus drops on QueueFull; the runner's sweep is the backstop)."""
        if (
            self._bus is None
            or not recorded.inserted
            or recorded.reported
            or recorded.state_after != continuation.STATE_RESULT_READY
        ):
            return
        from nous.events import Event

        try:
            await self._bus.emit(
                Event(
                    type="intention.result_ready",
                    agent_id=self._agent_id,
                    data={
                        "intention_id": str(recorded.intention_id),
                        "root_id": str(recorded.root_id),
                        "agent_id": self._agent_id,
                    },
                )
            )
        except Exception:
            logger.warning("F099: could not emit intention.result_ready for %s", recorded.intention_id, exc_info=True)
```

(`Event` and `EventBus` are imported under `TYPE_CHECKING` at the top of the module; the local import above is the runtime one.)

- [ ] **Step 5: Run the tests, lint, commit**
  Run: `"$BIN/nous-test-linux.sh" "$WT" runner pg "$DB" tests/test_f099_phase2b_record_result.py tests/test_f099_phase2b_inbox.py -q`
  Expected: PASS.

```bash
cd "$WT"
cat > /tmp/f099-2b-4.txt <<'EOF'
feat(F099): record_result moves a continue intention to result_ready with its row

One transaction locks the intention, inserts the result keyed by the intention
alone, and moves pending (or a closed one with an open root) to result_ready.
Rows arriving while the intention is deciding or awaiting the owner are held.
An arrival nothing can reopen becomes an intention_report with the raw result,
under a deterministic id so a double write collapses. The store emits
intention.result_ready after the commit.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_015q4W1nke7JzGaZkC21Whng
EOF
git add nous/brain/continuation.py nous/heart/result_inbox.py tests/test_f099_phase2b_record_result.py
git commit -F /tmp/f099-2b-4.txt
```

---

## Task 2b-5: The writers route by wake policy; `report` closes in its insert's transaction

**Files:**
- Modify: `nous/heart/result_inbox.py`: `route_result`, `_no_output_envelope`, `ResultInboxStore.insert_and_close`, `close_source_intention(reason=)`, `close_intention_quietly`, `record_subtask_result`, `record_dag_result`
- Modify: `nous/heart/result_reconciler.py`: `InboxSubtaskPass.run` (the flag-on branch)
- Modify: `nous/api/tools.py`: `_close_inline_intention(reason=)` and its call site (one hunk, C11)
- Modify: `tests/test_f099_closing.py`: **the one Phase 1 test that changes**, plus one inline test
- Create: `tests/test_f099_phase2b_writers.py`

**Interfaces:**
- Consumes: Tasks 2b-3 and 2b-4.
- Produces: `route_result(store, settings, *, source_kind, source_id, generation, env, channel, session_id, default_channel=None, correlation_id=None, created_at=None, empty_title="result") -> bool` (the one flag-on router; True when a row was written), `ResultInboxStore.insert_and_close(*, close_kind, close_id, **insert_kwargs) -> bool` (I4: the insert and the `delivered` close in one transaction), `ResultInboxStore.close_source_intention(source_kind, source_id, *, reason=…)`.
- With `NOUS_CONTINUATION_ENABLED` on: a `continue` intention's result goes through `record_continue_result` (no routing key consulted, no default-chat substitution); a `report` intention's row (keyed as F098) and its `delivered` close are one transaction (T3, I4); `none`, `remember` and "no intention" close as `delivered` before the routing check and route as F098 A. With it off: Phase 1's body, untouched.
- **The Phase 1 test that changes on purpose.** `test_a_report_intention_also_closes_as_legacy_in_phase_1` in `tests/test_f099_closing.py` pins that every policy closes `legacy`. With `NOUS_CONTINUATION_ENABLED` on, a `report` intention closes `delivered` (I4) and a `continue` one is routed by its intention alone. Its body still holds with the flag off, so it is renamed and kept as the flag-off variant (a PIN, with the Phase 1 assertions unchanged), and a flag-on sibling is added. Say so in the task report.

- [ ] **Step 1: Write the failing tests.** Create `tests/test_f099_phase2b_writers.py`:

```python
"""F099 Phase 2b: the writers route by wake policy, and close as 'delivered' with the flag on."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from f099_support import (  # noqa: F401
    CHAN, CONT, ON, RESULT, dag_kwargs, env_factory, finish, inbox_rows, intention_of, make_dag, make_subtask,
    set_intention,
)

from nous.brain import continuation
from nous.heart.result_inbox import ResultInboxDagListener, record_dag_result, record_subtask_result


async def _hook(env, st, how: str = "complete"):
    """The worker's terminal hook, after the subtask finished."""
    await finish(env, st, how)
    await env.pool._record_inbox(st)


# ---- continue: keyed by the intention alone -------------------------------------------------------


@pytest.mark.parametrize("routed", [True, False], ids=["routed", "unrouted"])
async def test_a_continue_result_is_keyed_by_the_intention_alone(env_factory, routed):
    """Spec 4.3 item 1: channel and session NULL whatever the work row says, so a chat claim cannot take it."""
    env = await env_factory(**CONT)
    st = await make_subtask(env, routed=routed)
    await _hook(env, st)
    it = await intention_of(env, "subtask", st.id)
    (row,) = await inbox_rows(env, st.id)
    assert (row.channel, row.session_id, row.intention_id) == (None, None, it.id)
    assert (it.state, it.close_reason) == ("result_ready", None) and it.result_at is not None
    rows, older = await env.heart.result_inbox.claim(
        channel=CHAN, session_id="S1", max_age_hours=72, max_items=10
    )
    assert (rows, older) == ([], 0)  # a chat turn on the origin channel and session takes nothing
    assert (await inbox_rows(env, st.id))[0].delivered_at is None


async def test_a_failed_continue_subtask_writes_a_failure_row(env_factory):
    env = await env_factory(**CONT)
    st = await make_subtask(env)
    await _hook(env, st, "fail")
    (row,) = await inbox_rows(env, st.id)
    assert row.msg_type == "FAILURE" and (row.channel, row.session_id) == (None, None)


async def test_a_continue_subtask_with_no_output_still_wakes_its_intention(env_factory):
    """Contract C12: without a row the intention would stay pending forever."""
    env = await env_factory(**CONT)
    st = await make_subtask(env)
    await _hook(env, st, "empty")
    (row,) = await inbox_rows(env, st.id)
    assert row.msg_type == "INFORM" and "returned no output" in row.body
    assert (await intention_of(env, "subtask", st.id)).state == "result_ready"


async def test_the_worker_hook_announces_a_continue_result_on_the_bus(env_factory):
    env = await env_factory(**CONT)
    env.heart.result_inbox.set_bus(env.bus)
    st = await make_subtask(env)
    await _hook(env, st)
    it = await intention_of(env, "subtask", st.id)
    assert [(e.type, e.data["intention_id"]) for e in env.bus.events] == [("intention.result_ready", str(it.id))]


async def test_a_fault_in_the_move_leaves_the_result_undelivered_and_the_intention_pending(env_factory, monkeypatch):
    env = await env_factory(**CONT)
    st = await make_subtask(env)

    async def boom(*args, **kwargs):
        raise RuntimeError("fault")

    monkeypatch.setattr(continuation, "_set_result_ready", boom)
    await _hook(env, st)  # the hook swallows it
    assert await inbox_rows(env, st.id) == []
    assert (await intention_of(env, "subtask", st.id)).state == "pending"


# ---- report: I4, closed as delivered in the insert's transaction ----------------------------------


async def test_a_report_intention_closes_as_delivered_with_its_row(env_factory):
    env = await env_factory(**CONT)
    st = await make_subtask(env, policy="report")
    await _hook(env, st)
    it = await intention_of(env, "subtask", st.id)
    (row,) = await inbox_rows(env, st.id)
    assert (it.state, it.close_reason) == ("closed", "delivered") and it.result_at is not None
    assert (row.channel, row.session_id, row.intention_id) == (CHAN, "S1", it.id)  # routed as F098 A: chat consumes it


async def test_a_failed_close_rolls_the_report_row_back_and_a_retry_lands_both(env_factory, monkeypatch):
    env = await env_factory(**CONT)
    st = await make_subtask(env, policy="report")
    await finish(env, st)
    real = continuation.close_delivered

    async def boom(*args, **kwargs):
        raise RuntimeError("fault after the insert")

    monkeypatch.setattr(continuation, "close_delivered", boom)
    await env.pool._record_inbox(st)
    assert await inbox_rows(env, st.id) == []  # one transaction: neither the row nor the close
    assert (await intention_of(env, "subtask", st.id)).state == "pending"
    monkeypatch.setattr(continuation, "close_delivered", real)
    await env.pool._record_inbox(st)  # what the reconciler's pass does
    assert len(await inbox_rows(env, st.id)) == 1
    assert (await intention_of(env, "subtask", st.id)).close_reason == "delivered"


async def test_an_unrouted_report_still_closes_as_delivered(env_factory):
    env = await env_factory(**CONT)
    st = await make_subtask(env, policy="report", routed=False)
    await _hook(env, st)
    it = await intention_of(env, "subtask", st.id)
    assert (it.state, it.close_reason) == ("closed", "delivered") and await inbox_rows(env, st.id) == []


# ---- none and remember -----------------------------------------------------------------------------


@pytest.mark.parametrize("policy", ["none", "remember"])
async def test_none_and_remember_close_as_delivered_and_route_as_f098(env_factory, policy):
    env = await env_factory(**CONT)
    st = await make_subtask(env, policy=policy)
    await _hook(env, st)
    it = await intention_of(env, "subtask", st.id)
    (row,) = await inbox_rows(env, st.id)
    assert (it.state, it.close_reason) == ("closed", "delivered")
    assert (row.channel, row.session_id, row.intention_id) == (CHAN, "S1", it.id)


# ---- the three flag states --------------------------------------------------------------------------

STATES = {
    # PIN (first two): Phase 1, byte for byte.
    "off-off": ({}, "pending", None, (CHAN, "S1"), False),
    "on-off": (ON, "closed", "legacy", (CHAN, "S1"), True),
    "on-on": (CONT, "result_ready", None, (None, None), True),
}


@pytest.mark.parametrize("flags", list(STATES))
async def test_the_three_flag_states_route_a_continue_result_as_specified(env_factory, flags):
    over, state, reason, keys, names_it = STATES[flags]
    env = await env_factory(**{"result_inbox_enabled": True, **over})
    st = await make_subtask(env)  # as if spawned while the intentions flag was on
    await _hook(env, st)
    it = await intention_of(env, "subtask", st.id)
    (row,) = await inbox_rows(env, st.id)
    assert (it.state, it.close_reason) == (state, reason)
    assert (row.channel, row.session_id) == keys
    assert (row.intention_id == it.id) is names_it


# ---- DAGs -------------------------------------------------------------------------------------------


async def test_a_continue_dag_never_takes_the_default_chat(env_factory):
    """Contract risk 6: result_inbox_dag_scheduled would route a continue DAG to the chat."""
    env = await env_factory(**CONT, result_inbox_dag_scheduled=True, telegram_chat_id="4242")
    dag, _ = await make_dag(env, policy="continue")
    assert await record_dag_result(env.heart.result_inbox, env.settings, **dag_kwargs(dag)) is True
    it = await intention_of(env, "dag", dag.id)
    (row,) = await inbox_rows(env, dag.id)
    assert (row.channel, row.session_id, row.intention_id) == (None, None, it.id)
    assert it.state == "result_ready"
    remembered, _ = await make_dag(env, policy="remember")  # the non-continue branch keeps its substitution
    await record_dag_result(env.heart.result_inbox, env.settings, **dag_kwargs(remembered))
    (other,) = await inbox_rows(env, remembered.id)
    it2 = await intention_of(env, "dag", remembered.id)
    assert (other.channel, it2.close_reason) == ("telegram:4242", "delivered")


async def test_the_bus_listener_and_the_delivery_path_collapse_to_one_row(env_factory):
    env = await env_factory(**CONT)
    dag, _ = await make_dag(env)
    store = env.heart.result_inbox
    assert await record_dag_result(store, env.settings, **dag_kwargs(dag)) is True
    assert await record_dag_result(store, env.settings, **dag_kwargs(dag)) is False
    event = SimpleNamespace(
        data={"dag_id": str(dag.id), "name": "snow-dag", "status": "completed", "summary": "ok",
              "delivery_generation": dag.delivery_generation}
    )
    await ResultInboxDagListener(store, env.settings).handle(event)
    assert len(await inbox_rows(env, dag.id)) == 1
    assert (await intention_of(env, "dag", dag.id)).state == "result_ready"


async def test_a_retried_dag_reopens_its_closed_continue_intention(env_factory):
    env = await env_factory(**CONT)
    dag, _ = await make_dag(env)
    store = env.heart.result_inbox
    await record_dag_result(store, env.settings, **dag_kwargs(dag))
    it = await intention_of(env, "dag", dag.id)
    await set_intention(env, it.id, state="closed", close_reason="resolved", closed_at=datetime.now(UTC))
    await record_dag_result(store, env.settings, **{**dag_kwargs(dag), "generation": 1})
    after = await intention_of(env, "dag", dag.id)
    assert (after.state, after.close_reason) == ("result_ready", None)
    assert [r.source_generation for r in await inbox_rows(env, dag.id)] == [0, 1]


async def test_a_retried_dag_on_a_cancelled_root_reports_the_raw_result(env_factory):
    env = await env_factory(**CONT)
    dag, _ = await make_dag(env, origin_channel=CHAN)
    store = env.heart.result_inbox
    await record_dag_result(store, env.settings, **dag_kwargs(dag, origin_channel=CHAN))
    it = await intention_of(env, "dag", dag.id)
    await set_intention(
        env, it.id, state="closed", close_reason="resolved", root_cancelled_at=datetime.now(UTC)
    )
    await record_dag_result(store, env.settings, **{**dag_kwargs(dag, origin_channel=CHAN), "generation": 1})
    rows = await inbox_rows(env)
    reports = [r for r in rows if r.source_kind == "intention_report"]
    assert len(reports) == 1 and (reports[0].channel, reports[0].msg_type) == (CHAN, "REPORT")
    assert (await intention_of(env, "dag", dag.id)).state == "closed"


@pytest.mark.parametrize("flags", [CONT, ON], ids=["continuation-on", "continuation-off"])
async def test_a_non_terminal_dag_status_writes_and_closes_nothing(env_factory, flags):
    env = await env_factory(**flags)
    dag, _ = await make_dag(env, status="running")
    assert await record_dag_result(env.heart.result_inbox, env.settings, **dag_kwargs(dag, status="running")) is False
    assert await inbox_rows(env, dag.id) == []
    assert (await intention_of(env, "dag", dag.id)).state == "pending"


async def test_a_report_dag_closes_as_delivered_with_its_row_and_an_unrouted_one_without(env_factory):
    env = await env_factory(**CONT)
    routed, _ = await make_dag(env, policy="report", origin_channel=CHAN)
    bare, _ = await make_dag(env, policy="report")
    store = env.heart.result_inbox
    assert await record_dag_result(store, env.settings, **dag_kwargs(routed, origin_channel=CHAN)) is True
    assert await record_dag_result(store, env.settings, **dag_kwargs(bare)) is False
    assert (await intention_of(env, "dag", routed.id)).close_reason == "delivered"
    assert (await intention_of(env, "dag", bare.id)).close_reason == "delivered"
    assert [r.channel for r in await inbox_rows(env, routed.id)] == [CHAN]


# ---- the reconciler's subtask pass ------------------------------------------------------------------


async def test_the_inbox_pass_routes_a_lost_continue_result_by_the_intention(env_factory):
    from nous.heart.result_reconciler import InboxSubtaskPass

    env = await env_factory(**CONT)
    store = env.heart.result_inbox
    await store.ensure_enabled_at()  # repairs cover results finished after this
    st = await make_subtask(env)
    await finish(env, st)  # the hook's write was lost
    assert await InboxSubtaskPass(env.db, store, env.settings).run(limit=10) == 1
    (row,) = await inbox_rows(env, st.id)
    assert (row.channel, row.session_id) == (None, None)
    assert (await intention_of(env, "subtask", st.id)).state == "result_ready"
    assert await InboxSubtaskPass(env.db, store, env.settings).run(limit=10) == 0  # idempotent


async def test_the_inbox_pass_settles_a_non_continue_subtask_with_nothing_to_say(env_factory):
    from nous.heart.result_reconciler import InboxSubtaskPass

    env = await env_factory(**CONT)
    store = env.heart.result_inbox
    await store.ensure_enabled_at()
    st = await make_subtask(env, policy="remember")
    await finish(env, st, "empty")
    assert await InboxSubtaskPass(env.db, store, env.settings).run(limit=10) == 0
    assert (await env.heart.subtasks.get(st.id)).delivered is True
    assert (await intention_of(env, "subtask", st.id)).close_reason == "delivered"


async def test_record_subtask_result_skips_dag_node_subtasks_with_the_flag_on(env_factory):  # PIN (Phase 1 behaviour)
    env = await env_factory(**CONT)
    st = await make_subtask(env)
    row = await finish(env, st)
    row.metadata_ = {"dag_id": "d"}
    assert await record_subtask_result(env.heart.result_inbox, row, env.settings) is False
```

- [ ] **Step 2: Edit the Phase 1 closing test and add the inline one.** In `tests/test_f099_closing.py`, next to `ON = …` add `CONT = {**ON, "continuation_enabled": True}`. Replace `test_a_report_intention_also_closes_as_legacy_in_phase_1` with:

```python
@pytest.mark.parametrize("policy", ["report", "continue"])
async def test_a_report_or_continue_intention_closes_as_legacy_with_continuation_off(close_env, policy):
    """PIN. Spec section 4.3 Phase 1: with NOUS_CONTINUATION_ENABLED off, F098 still
    consumes every result, so every policy closes as 'legacy'. I4's report close-at-write
    ('delivered', in the insert's transaction) is Phase 2 and needs the flag: that is
    the next test, which is why this one was renamed (its assertions are Phase 1's, unchanged)."""
    env = await close_env(**ON)
    st = await env.heart.subtasks.create(
        task="Check the snow report",
        parent_session_id="S1",
        parent_channel=CHAN,
        intention=IntentionSpec(intent="Tell the user about the snow", origin_kind="interactive", wake_policy=policy),
    )
    await env.heart.subtasks.complete(st.id, RESULT, final_outcome="completed")
    await env.pool._record_inbox(st)
    it = await _of(env, "subtask", st.id)
    assert (it.wake_policy, it.state, it.close_reason) == (policy, "closed", "legacy")
    (row,) = await _inbox(env, st.id)
    assert (row.channel, row.session_id, row.intention_id) == (CHAN, "S1", it.id)  # routed as F098 A


async def test_a_report_intention_closes_as_delivered_with_continuation_on(close_env):
    """F099 Phase 2 (I4): a report closes as 'delivered' in its insert's transaction and still
    routes as F098 A (the chat consumes it); a continue one is no longer routed by F098 at all."""
    env = await close_env(**CONT)
    st = await env.heart.subtasks.create(
        task="Check the snow report",
        parent_session_id="S1",
        parent_channel=CHAN,
        intention=IntentionSpec(intent="Tell the user about the snow", origin_kind="interactive", wake_policy="report"),
    )
    await env.heart.subtasks.complete(st.id, RESULT, final_outcome="completed")
    await env.pool._record_inbox(st)
    it = await _of(env, "subtask", st.id)
    assert (it.wake_policy, it.state, it.close_reason) == ("report", "closed", "delivered")
    (row,) = await _inbox(env, st.id)
    assert (row.channel, row.session_id, row.intention_id) == (CHAN, "S1", it.id)
```

and after `test_an_inline_spawn_closes_its_intention_in_the_call` add:

```python
async def test_an_inline_spawn_closes_as_delivered_with_continuation_on(close_env):
    env = await close_env(**CONT)
    await env.d.dispatch(
        "spawn_task",
        {"task": "t", "await_result": True, "intent": "Tell the user about the snow"},
        session_id="S1",
        context=INTERACTIVE,
    )
    (st,) = await env.heart.subtasks.list(limit=10)
    it = await _of(env, "subtask", st.id)
    assert (it.wake_policy, it.state, it.close_reason) == ("none", "closed", "delivered")
```

- [ ] **Step 3: Run; expect failure.**
  Run: `"$BIN/nous-test-linux.sh" "$WT" runner pg "$DB" tests/test_f099_phase2b_writers.py tests/test_f099_closing.py -q`
  Expected: the new flag-on tests fail (rows keyed by F098, `legacy` reasons); the flag-off tests, the parity rows of `test_the_three_flag_states…` and the renamed pin pass.

- [ ] **Step 4: The store and the router.** In `nous/heart/result_inbox.py`:

`ResultInboxStore.close_source_intention` takes the reason:

```python
    async def close_source_intention(
        self, source_kind: str, source_id: UUID, *, reason: str = intentions.CLOSE_LEGACY
    ) -> UUID | None:
        """F099: close a finished source's intention (Phase 1: 'legacy'; Phase 2: see
        continuation.close_reason_for). Its id, or None."""
        async with self._db.session() as session:
            found = await intentions.close_for_source(session, self._agent_id, source_kind, source_id, reason=reason)
            await session.commit()
        return found

    async def insert_and_close(self, *, close_kind: str, close_id: UUID, **insert_kwargs: Any) -> bool:
        """F099 I4: the inbox row of a ``report`` intention and the ``delivered`` close of that
        intention in ONE transaction (T3). A fault after the INSERT rolls the row back too, so a
        report is never both closed and unwritten; the reconciler's pass re-runs the writer."""
        async with self._db.session() as session:
            written = await self.insert(session=session, **insert_kwargs)
            await continuation.close_delivered(session, self._agent_id, close_kind, close_id)
            await session.commit()
        return written
```

`close_intention_quietly` uses the flag's reason:

```python
    if not intentions.enabled(settings):
        return None
    try:
        return await store.close_source_intention(
            source_kind, source_id, reason=continuation.close_reason_for(settings)
        )
    except Exception:
        logger.warning("F099: could not close the intention of %s %s", source_kind, source_id, exc_info=True)
        return None
```

Above the writers add the router:

```python
_NO_OUTPUT = "The work finished and returned no output."


def _no_output_envelope(title: str) -> Envelope:
    """A ``continue`` intention is owed a wake even when its work said nothing (contract C12)."""
    return Envelope("INFORM", (title or "result").strip().replace("\n", " ")[:_TITLE_MAX], _NO_OUTPUT)


async def route_result(
    store: ResultInboxStore,
    settings: Settings,
    *,
    source_kind: str,
    source_id: UUID,
    generation: int,
    env: Envelope | None,
    channel: str | None,
    session_id: str | None,
    default_channel: str | None = None,
    correlation_id: str | None = None,
    created_at: datetime | None = None,
    empty_title: str = "result",
) -> bool:
    """F099 Phase 2 (``continuation.enabled(settings)``): where one finished result goes.

    The intention's wake policy decides (spec 4.3 and I4):

    * ``continue``: intention-only routing, no routing key consulted and no default chat
      (``record_continue_result``: the row and the move to ``result_ready`` in one transaction);
    * ``report``: the F098-keyed row and the ``delivered`` close in one transaction (``insert_and_close``);
    * ``none``, ``remember``, a container, or no intention: closed (before the routing check, so a
      result nobody is routed still closes) and routed as F098 Phase A.

    ``default_channel`` is the DAG default chat, applied only on the non-``continue`` branch. True when
    a row was written. May raise: the writers around it swallow.
    """
    intention = await store.intention_of(source_kind, source_id)
    policy = intention.wake_policy if intention is not None else None
    if intention is not None and policy == intentions.WAKE_CONTINUE:
        recorded = await store.record_continue_result(
            intention_id=intention.id,
            source_kind=source_kind,
            source_id=source_id,
            generation=generation,
            envelope=env or _no_output_envelope(empty_title),
            correlation_id=correlation_id,
            created_at=created_at,
            settings=settings,
        )
        return recorded.inserted
    if not channel and not session_id:
        channel = default_channel
    if env is None or not (channel or session_id):
        await close_intention_quietly(store, settings, source_kind, source_id)
        return False
    row = dict(
        source_kind=source_kind,
        source_id=source_id,
        source_generation=generation,
        msg_type=env.msg_type,
        title=env.title,
        body=env.body,
        channel=channel,
        session_id=session_id,
        correlation_id=correlation_id,
        created_at=created_at,
    )
    if intention is not None and policy == intentions.WAKE_REPORT:
        return await store.insert_and_close(
            close_kind=source_kind, close_id=source_id, intention_id=intention.id, **row
        )
    intention_id = await close_intention_quietly(store, settings, source_kind, source_id)
    return await store.insert(intention_id=intention_id, **row)
```

`record_subtask_result`: insert the flag-on branch after the status check (Phase 1's body below it is untouched):

```python
        if subtask.status not in ("completed", "failed") or is_dag_node_subtask(subtask):
            return False
        if continuation.enabled(settings):
            return await route_result(
                store,
                settings,
                source_kind=SOURCE_SUBTASK,
                source_id=subtask.id,
                generation=0,
                env=subtask_envelope(subtask, settings.result_inbox_body_max_chars),
                channel=getattr(subtask, "parent_channel", None),
                session_id=subtask.parent_session_id,
                correlation_id=str(subtask.id),
                empty_title=subtask.task or "subtask",
            )
        intention_id = await close_intention_quietly(store, settings, SOURCE_SUBTASK, subtask.id)
```

The last line above is Phase 1's first statement after the status check; it and everything below it, down to the `except`, stay exactly as they are (the routing-key check, the envelope, the `store.insert(...)`).

`record_dag_result`: after `dag_uuid = …` add the terminal guard (C10) and the flag-on branch:

```python
        dag_uuid = dag_id if isinstance(dag_id, UUID) else UUID(str(dag_id))
        if status not in intentions.TERMINAL_DAG_STATUSES:
            return False  # an intention must not close on a non-terminal event (Phase 1 follow-up)
        if continuation.enabled(settings):
            body = _cap(
                summary or f"DAG '{name}' {status}",
                settings.result_inbox_body_max_chars,
                f"dag_manage status {dag_uuid.hex[:8]}",
            )
            scheduled = settings.result_inbox_dag_scheduled and settings.telegram_chat_id
            return await route_result(
                store,
                settings,
                source_kind=SOURCE_DAG,
                source_id=dag_uuid,
                generation=int(generation or 0),
                env=Envelope(dag_msg_type(status, blocked), name or "DAG", body),
                channel=origin_channel,
                session_id=origin_session_id,
                default_channel=f"telegram:{settings.telegram_chat_id}" if scheduled else None,
                correlation_id=str(dag_uuid),
                created_at=created_at,
                empty_title=name or "DAG",
            )
        # F099: closed before the routing-key check below (section 4.1 Closing).
        intention_id = await close_intention_quietly(store, settings, SOURCE_DAG, dag_uuid)
```

The last two lines are Phase 1's, unchanged, and so is everything below them down to the `except` (the default-chat substitution, the body cap, the `store.insert(...)`).

- [ ] **Step 5: The reconciler's subtask pass.** In `nous/heart/result_reconciler.py` add `from nous.brain import continuation, intentions` and `route_result` to the `result_inbox` import list, and replace the loop of `InboxSubtaskPass.run`:

```python
        fixed = 0
        settle = []
        continuation_on = continuation.enabled(self._settings)
        for st in candidates:
            env = None if is_dag_node_subtask(st) else subtask_envelope(st, self._settings.result_inbox_body_max_chars)
            if continuation_on and not is_dag_node_subtask(st):
                # F099 Phase 2: the same routing as the worker hook. A continue result is written
                # by its intention (an empty one too); a non-continue one with nothing to say settles.
                written = await route_result(
                    self._store,
                    self._settings,
                    source_kind=SOURCE_SUBTASK,
                    source_id=st.id,
                    generation=0,
                    env=env,
                    channel=st.parent_channel,
                    session_id=st.parent_session_id,
                    correlation_id=str(st.id),
                    created_at=st.completed_at,
                    empty_title=st.task or "subtask",
                )
                if written:
                    fixed += 1
                    logger.info("F098: reconciler re-inserted the inbox row of subtask %s", st.id.hex[:8])
                elif env is None:
                    settle.append(st.id)
                continue
            if env is None:
                settle.append(st.id)
                continue
            intention_id = await close_intention_quietly(self._store, self._settings, SOURCE_SUBTASK, st.id)
            written = await self._store.insert(
                source_kind=SOURCE_SUBTASK,
                source_id=st.id,
                msg_type=env.msg_type,
                title=env.title,
                body=env.body,
                channel=st.parent_channel,
                session_id=st.parent_session_id,
                correlation_id=str(st.id),
                created_at=st.completed_at,
                intention_id=intention_id,
            )
            if written:
                fixed += 1
                logger.info("F098: reconciler re-inserted the inbox row of subtask %s", st.id.hex[:8])
```

The last lines above (from `if env is None:` down) are Phase 1's loop body, kept verbatim under the flag-off path; the loop is followed, as before, by the `if settle:` block and `return fixed`.

- [ ] **Step 6: The inline close.** In `nous/api/tools.py`, add `continuation` to the existing import (`from nous.brain import continuation, intentions`), and change

```python
async def _close_inline_intention(heart: Any, subtask_id: UUID, reason: str = intentions.CLOSE_LEGACY) -> None:
```

with `await store.close_for_source(intentions.SOURCE_SUBTASK, subtask_id, reason=reason)` inside, and the call site (`~:3360`) to

```python
                if intentions.enabled(settings):
                    await _close_inline_intention(heart, subtask.id, continuation.close_reason_for(settings))
```

- [ ] **Step 7: Run the tests, lint, commit**
  Run: `"$BIN/nous-test-linux.sh" "$WT" runner pg "$DB" tests/test_f099_phase2b_writers.py tests/test_f099_closing.py tests/test_f099_routing_pins.py tests/test_f098_result_inbox.py tests/test_f099_phase2b_record_result.py -q`
  Expected: PASS. The routing pins and the F098 suite must pass **unchanged**: if one fails, the Phase 1 branch was altered.

```bash
cd "$WT"
cat > /tmp/f099-2b-5.txt <<'EOF'
feat(F099): the writers route a result by its intention's wake policy

With NOUS_CONTINUATION_ENABLED on, a continue result is written keyed by its
intention alone (no routing key consulted, no default chat) and moves the
intention to result_ready in the same transaction. A report intention's row and
its delivered close are one transaction (I4). none, remember and an inline
spawn close as delivered. The subtask worker hook, the DAG writer and the
reconciler's subtask pass share one router. record_dag_result ignores a
non-terminal status. With the flag off the Phase 1 bodies are untouched.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_015q4W1nke7JzGaZkC21Whng
EOF
git add nous/heart/result_inbox.py nous/heart/result_reconciler.py nous/api/tools.py tests/test_f099_closing.py tests/test_f099_phase2b_writers.py
git commit -F /tmp/f099-2b-5.txt
```

---

## Task 2b-6: Push suppression for `continue` sources, and no summary turn for a lineage DAG

**Files:**
- Modify: `nous/handlers/subtask_worker.py`: `_superseded_by_continuation`, one early return in `_notify_telegram`
- Modify: `nous/dag/delivery.py`: `DAGResultDelivery(intentions=)`, `_dag_intention`, the summary and Telegram legs of `deliver`
- Create: `tests/test_f099_phase2b_suppression.py`

**Interfaces:**
- Consumes: Task 2b-5's writers (`deliver` reaches `record_dag_result`).
- Produces: `SubtaskWorkerPool._notify_telegram` sends nothing for a subtask whose intention is `continue`, with `continuation.enabled(settings)` (one point read, only on the `notify=True` path; one site covers all four callers). `DAGResultDelivery.__init__(..., intentions: IntentionStore | None = None)`.
- `deliver`, with the intention read once (only when `intentions.enabled(settings)` and a store was passed):
  - a `continue` DAG under `continuation.enabled`: the Telegram leg is `LegResult("telegram", ok=False, required=False, detail="superseded_by_continuation")`, nothing is posted, and, because no required leg is left, `delivered` is True. **Its inbox row is guaranteed by the reconciler** (Task 2b-7), not by this leg;
  - an `internal_only` DAG, whatever its policy and **whatever the continuation flag** (contract C6): the F087 summary turn is skipped, `LegResult("summary", ok=True, required=False, detail="internal_only")`. A failed lookup skips it too (fail closed, template used, Telegram push kept) **only with `continuation.enabled(settings)`**; with it off a failed lookup runs the summary turn as Phase 1 does (MF-2).

- [ ] **Step 1: Write the failing tests.** Create `tests/test_f099_phase2b_suppression.py`:

```python
"""F099 Phase 2b: the raw pushes stand down for a continue source; no summary turn for a lineage DAG."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from f099_support import CONT, ON, RESULT, env_factory, inbox_rows, intention_of, make_dag, make_subtask, set_intention  # noqa: F401

from nous.dag.delivery import DAGResultDelivery

TG = {"telegram_bot_token": "test-token", "telegram_chat_id": "4242"}


def _leg(outcome, name):
    return next(leg for leg in outcome.legs if leg.name == name)


# ---- the subtask worker ------------------------------------------------------------------------------


async def test_no_raw_push_for_a_continue_subtask(env_factory):
    env = await env_factory(**CONT, **TG)
    st = await make_subtask(env, notify=True)
    await env.pool._notify_telegram(st, result="done")
    env.http.post.assert_not_awaited()


async def test_the_raw_push_still_goes_out_with_continuation_off(env_factory):  # PIN
    env = await env_factory(**ON, **TG)
    st = await make_subtask(env, notify=True)
    await env.pool._notify_telegram(st, result="done")
    env.http.post.assert_awaited_once()


@pytest.mark.parametrize("policy", ["remember", "none", "report"])
async def test_the_raw_push_still_goes_out_for_every_other_policy(env_factory, policy):
    env = await env_factory(**CONT, **TG)
    st = await make_subtask(env, policy=policy, notify=True)
    await env.pool._notify_telegram(st, error="boom")
    env.http.post.assert_awaited_once()


async def test_a_failed_intention_lookup_never_costs_the_push(env_factory, monkeypatch):
    env = await env_factory(**CONT, **TG)
    st = await make_subtask(env, notify=True)

    async def boom(*args, **kwargs):
        raise RuntimeError("intentions down")

    monkeypatch.setattr(env.heart.intentions, "get_for_source", boom)
    await env.pool._notify_telegram(st, result="done")
    env.http.post.assert_awaited_once()


async def test_the_worker_path_end_to_end_sends_no_push_and_writes_the_intention_keyed_row(env_factory):
    """The real _process_subtask: its Telegram call and its terminal hook. Fails when either hook is removed."""
    from nous.handlers.subtask_worker import SubtaskWorkerPool

    class _WorkerTurn:
        async def run_turn(self, **kwargs):
            return RESULT, None, {"input_tokens": 1, "output_tokens": 1}

        async def end_conversation(self, *a, **k):
            return None

    env = await env_factory(**CONT, **TG)
    await make_subtask(env, notify=True)
    pool = SubtaskWorkerPool(_WorkerTurn(), env.heart, env.settings, http_client=env.http)
    await pool._process_subtask(await env.heart.subtasks.dequeue("worker-0"))
    env.http.post.assert_not_awaited()
    (row,) = await inbox_rows(env)
    assert (row.channel, row.session_id, row.body.startswith("Powder")) == (None, None, True)
    assert (await intention_of(env, "subtask", row.source_id)).state == "result_ready"


# ---- the F087 delivery -------------------------------------------------------------------------------


def _delivery(env, runner=None) -> DAGResultDelivery:
    return DAGResultDelivery(
        env.settings,
        agent_id=env.agent,
        http=env.http,
        runner=runner,
        inbox=env.heart.result_inbox,
        intentions=env.heart.intentions,
    )


async def test_the_telegram_leg_stands_down_for_a_continue_dag(env_factory):
    env = await env_factory(**CONT, **TG, dag_delivery_telegram_enabled=True)
    dag, _ = await make_dag(env, policy="continue")
    outcome = await _delivery(env).deliver(dag)
    leg = _leg(outcome, "telegram")
    assert (leg.ok, leg.required, leg.detail) == (False, False, "superseded_by_continuation")
    assert outcome.delivered is True  # no required leg: the reconciler guarantees the row (Task 2b-7)
    env.http.post.assert_not_awaited()
    (row,) = await inbox_rows(env, dag.id)
    assert (row.channel, row.session_id) == (None, None)


async def test_the_telegram_leg_still_pushes_with_continuation_off(env_factory):  # PIN
    env = await env_factory(**ON, **TG, dag_delivery_telegram_enabled=True)
    dag, _ = await make_dag(env, policy="continue")
    outcome = await _delivery(env).deliver(dag)
    assert _leg(outcome, "telegram").ok is True
    env.http.post.assert_awaited_once()


async def test_the_telegram_leg_still_pushes_for_a_remember_dag(env_factory):
    env = await env_factory(**CONT, **TG, dag_delivery_telegram_enabled=True)
    dag, _ = await make_dag(env, policy="remember")
    outcome = await _delivery(env).deliver(dag)
    assert _leg(outcome, "telegram").required is True
    env.http.post.assert_awaited_once()


def _summary_runner() -> AsyncMock:
    runner = AsyncMock()
    runner.run_turn.return_value = ("An authored summary.", None, {})
    return runner


async def test_an_owner_dag_still_gets_its_summary_turn(env_factory):  # PIN
    env = await env_factory(**ON, **TG, dag_delivery_agent_summary_enabled=True)
    dag, _ = await make_dag(env, policy="remember")
    runner = _summary_runner()
    outcome = await _delivery(env, runner).deliver(dag)
    runner.run_turn.assert_awaited_once()
    assert _leg(outcome, "summary").ok is True and outcome.summary == "An authored summary."


@pytest.mark.parametrize("flags", [CONT, ON], ids=["continuation-on", "continuation-off"])
async def test_an_internal_only_dag_gets_no_summary_turn_whatever_the_flag(env_factory, flags):
    """Contract C6: a lineage DAG that finishes after the flag went off must not run the
    summary turn with outward tools."""
    env = await env_factory(**flags, **TG, dag_delivery_agent_summary_enabled=True)
    dag, _ = await make_dag(env, policy="continue")
    it = await intention_of(env, "dag", dag.id)
    await set_intention(env, it.id, authority="internal_only")
    runner = _summary_runner()
    outcome = await _delivery(env, runner).deliver(dag)
    runner.run_turn.assert_not_awaited()
    leg = _leg(outcome, "summary")
    assert (leg.ok, leg.required, leg.detail) == (True, False, "internal_only")
    assert outcome.summary != "An authored summary."


async def test_a_failed_dag_intention_lookup_follows_the_continuation_flag(env_factory, monkeypatch):
    """MF-2. With continuation on, an unreadable lineage fails closed: no summary turn (the template is
    used) and the Telegram push is kept. With continuation off (intentions on), a failed lookup changes
    nothing: the summary turn runs exactly as in Phase 1."""

    async def boom(*args, **kwargs):
        raise RuntimeError("intentions down")

    results = {}
    for name, flags in (("continuation-on", CONT), ("continuation-off", ON)):
        env = await env_factory(**flags, **TG, dag_delivery_agent_summary_enabled=True, dag_delivery_telegram_enabled=True)
        dag, _ = await make_dag(env, policy="remember")
        monkeypatch.setattr(env.heart.intentions, "get_for_source", boom)
        runner = _summary_runner()
        outcome = await _delivery(env, runner).deliver(dag)
        results[name] = (runner.run_turn.await_count, _leg(outcome, "summary").detail, _leg(outcome, "telegram").ok)
    assert results["continuation-on"] == (0, "internal_only", True)
    assert results["continuation-off"][0] == 1 and results["continuation-off"][2] is True  # PIN: Phase 1


async def test_with_intentions_off_the_delivery_reads_no_intention(env_factory, monkeypatch):  # PIN
    env = await env_factory(result_inbox_enabled=True, **TG, dag_delivery_agent_summary_enabled=True)
    dag, _ = await make_dag(env, policy="remember")

    async def boom(*args, **kwargs):
        raise AssertionError("an intention was read with the flag off")

    monkeypatch.setattr(env.heart.intentions, "get_for_source", boom)
    runner = _summary_runner()
    await _delivery(env, runner).deliver(dag)
    runner.run_turn.assert_awaited_once()
```

- [ ] **Step 2: Run; expect failure.**
  Run: `"$BIN/nous-test-linux.sh" "$WT" runner pg "$DB" tests/test_f099_phase2b_suppression.py -q`
  Expected: the `intentions=` tests fail with `unexpected keyword argument`; `test_no_raw_push_for_a_continue_subtask` fails (the push goes out).

- [ ] **Step 3: The worker.** In `nous/handlers/subtask_worker.py` add `from nous.brain import continuation, intentions` to the imports, and in `SubtaskWorkerPool` (before `_notify_telegram`):

```python
    async def _superseded_by_continuation(self, subtask: Subtask) -> bool:
        """F099 Phase 2: a ``continue`` result goes to Nous's own continuation, so the raw push
        stands down (spec 4.3 item 5). One point read, only for a ``notify=True`` subtask with the
        flag on. A failed read sends the push: a duplicate costs less than a lost notification."""
        if not continuation.enabled(self._settings):
            return False
        store = getattr(self._heart, "intentions", None)
        if store is None:
            return False
        try:
            row = await store.get_for_source(intentions.SOURCE_SUBTASK, subtask.id)
        except Exception:
            logger.warning(
                "F099: could not read the intention of subtask %s; sending its push", subtask.id.hex[:8], exc_info=True
            )
            return False
        return row is not None and row.wake_policy == intentions.WAKE_CONTINUE
```

and in `_notify_telegram`, directly after the `if not token or not chat_id: return` check (so an unconfigured Telegram costs no read):

```python
        if await self._superseded_by_continuation(subtask):
            return
```

- [ ] **Step 4: The delivery.** In `nous/dag/delivery.py` add the imports

```python
from nous.brain import continuation
from nous.brain.intentions import AUTHORITY_INTERNAL, SOURCE_DAG, WAKE_CONTINUE
from nous.brain.intentions import enabled as intentions_enabled
```

(and under `TYPE_CHECKING`: `from nous.brain.intentions import IntentionStore` and `from nous.storage.models import Intention`). `__init__` gains the keyword and stores it:

```python
        inbox: ResultInboxStore | None = None,
        intentions: IntentionStore | None = None,
    ) -> None:
        # (the existing assignments of settings, agent_id, bus, runner and http stay as they are)
        self._inbox = inbox
        # F099: read once per delivery. A continue DAG's push stands down, and an
        # internal_only DAG gets no summary turn (spec I3).
        self._intentions = intentions
```

Add the helper next to the other private methods:

```python
    async def _dag_intention(self, dag: ExecutionDAG) -> tuple[Intention | None, bool]:
        """``(the DAG's intention, lookup_failed)``. One point read, only with intentions on and a store.
        Never raises: a failed lookup is reported so the caller can fail closed (with continuation on)."""
        if self._intentions is None or not intentions_enabled(self._settings):
            return None, False
        try:
            return await self._intentions.get_for_source(SOURCE_DAG, dag.id), False
        except Exception:
            logger.warning(
                "F099: could not read the intention of DAG %s", str(dag.id)[:8], exc_info=True,
            )
            return None, True
```

In `deliver`, replace the summary block and the Telegram leg:

```python
        intention, lookup_failed = await self._dag_intention(dag)
        # I3: a lineage DAG never runs the summary turn with outward tools, whatever its wake
        # policy and whatever the continuation flag says now (contract C6). A lookup that failed fails
        # closed only with continuation on (MF-2): with it off, Phase 1's behaviour is kept.
        lineage_closed = (lookup_failed and continuation.enabled(self._settings)) or (
            intention is not None and intention.authority == AUTHORITY_INTERNAL
        )
        superseded = (
            continuation.enabled(self._settings) and intention is not None and intention.wake_policy == WAKE_CONTINUE
        )

        summary = self.build_template(dag)
        cached = getattr(dag, "delivery_summary", None)
        if lineage_closed:
            legs.append(LegResult("summary", ok=True, required=False, detail="internal_only"))
            summary = cached or summary
        elif cached:
            summary = cached
            legs.append(LegResult("summary", ok=True, required=False, detail="cached"))
        elif self._settings.dag_delivery_agent_summary_enabled:
            leg, authored = await self._leg_agent_summary(dag, summary)
            legs.append(leg)
            if authored:
                summary = authored
                summary_authored = True
```

and

```python
        if self._settings.dag_delivery_telegram_enabled:
            if superseded:
                # Spec 4.3 item 5: Nous's own continuation is the consumer. No required leg is left, so
                # the DAG is marked delivered, and InboxDagPass repairs a row this attempt failed to write.
                legs.append(LegResult("telegram", ok=False, required=False, detail="superseded_by_continuation"))
            else:
                legs.append(await self._leg_telegram(dag, summary))
```

(The `# @codex P2 on da5dc06` comment on the cached summary stays where it is.)

- [ ] **Step 5: Run the tests, lint, commit**
  Run: `"$BIN/nous-test-linux.sh" "$WT" runner pg "$DB" tests/test_f099_phase2b_suppression.py tests/test_dag_delivery.py tests/test_f098_result_inbox.py -q`
  Expected: PASS.

```bash
cd "$WT"
cat > /tmp/f099-2b-6.txt <<'EOF'
feat(F099): the raw pushes stand down for a continue source

With NOUS_CONTINUATION_ENABLED on, the subtask worker's Telegram push and the
F087 Telegram leg (ok=False, required=False, superseded_by_continuation) do
not fire for a continue intention, and the DAG is still marked delivered with
no required leg left. The F087 summary turn is skipped for every internal_only
DAG, whatever the flag says now, and when the intention cannot be read.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_015q4W1nke7JzGaZkC21Whng
EOF
git add nous/handlers/subtask_worker.py nous/dag/delivery.py tests/test_f099_phase2b_suppression.py
git commit -F /tmp/f099-2b-6.txt
```

---

## Task 2b-7: Chat never claims a `continue` row; the reconciler selects continuation work

**Files:**
- Modify: `nous/cognitive/layer.py`: `pre_turn(context_kind=)`, the `intent-` skip, `_inject_result_inbox`
- Modify: `nous/brain/intentions.py`: `close_finished_sources(exclude_policies=)`
- Modify: `nous/heart/result_reconciler.py`: `IntentionClosePass`, the `InboxDagPass` and `InboxSubtaskPass` filters
- Create: `tests/test_f099_phase2b_routing.py`

**Interfaces:**
- Produces: `CognitiveLayer.pre_turn(..., context_kind: str | None = None)`. For `context_kind == "continuation"`, or a session id starting with `continuation.INTENT_SESSION_PREFIX`, `pre_turn` neither claims the inbox nor touches `channel_sessions`; for `"continuation"` it also starts no deliberation. `_inject_result_inbox` returns at once for an `intent-` session. (The runner's pass-through of `context_kind` is PR-2c's: contract C7.)
- Produces: `close_finished_sources(session, agent_id, *, limit, reason=CLOSE_LEGACY, exclude_policies: tuple[str, ...] = ())`.
- `IntentionClosePass.run` passes `exclude_policies=(WAKE_CONTINUE, WAKE_REPORT)` and `reason=continuation.close_reason_for(settings)` when `continuation.enabled(settings)`; its container half is unchanged (C5).
- `InboxDagPass`'s routable filter becomes `origin_channel IS NOT NULL OR origin_session_id IS NOT NULL OR EXISTS (continue intention of this DAG, open or closed)`; `InboxSubtaskPass`'s becomes `parent_channel … OR parent_session_id … OR EXISTS (open continue intention of this subtask)`. Both extensions apply only with `continuation.enabled(settings)` (C4).

- [ ] **Step 1: Write the failing tests.** Create `tests/test_f099_phase2b_routing.py`:

```python
"""F099 Phase 2b: chat never claims a continue row, and the reconciler selects continuation work."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from f099_support import (  # noqa: F401
    CHAN, CONT, ON, RESULT, env_factory, finish, inbox_rows, intention_of, make_dag, make_subtask, set_intention,
)
from sqlalchemy import update

from nous.brain import continuation, intentions
from nous.dag.delivery import DAGResultDelivery
from nous.heart.result_reconciler import InboxDagPass, InboxSubtaskPass, IntentionClosePass
from nous.storage.models import ExecutionDAG


@pytest.fixture
async def make_layer(env_factory):
    from nous.brain.brain import Brain
    from nous.cognitive.layer import CognitiveLayer

    brains = []

    async def build(**over):
        env = await env_factory(**{"result_inbox_enabled": True, **over})
        brain = Brain(database=env.db, settings=env.settings)
        brains.append(brain)
        env.layer = CognitiveLayer(brain, env.heart, env.settings, identity_prompt="You are Nous.")
        return env

    yield build
    for brain in brains:
        await brain.close()


def _prompt(ctx) -> str:
    return ctx.system_prompt if isinstance(ctx.system_prompt, str) else str(ctx.system_prompt)


async def _row(env, **over):
    import uuid

    values = dict(
        source_kind="subtask", source_id=uuid.uuid4(), msg_type="INFORM", title="t", body="forged body", channel=CHAN
    )
    values.update(over)
    await env.heart.result_inbox.insert(**values)


# ---- pre_turn -----------------------------------------------------------------------------------------


async def test_pre_turn_skips_the_inbox_for_an_intent_session(make_layer):
    env = await make_layer(**CONT)
    await _row(env, channel=None, session_id="intent-abc")  # forged into the continuation's own session
    ctx = await env.layer.pre_turn(env.agent, "intent-abc", "hi")
    assert "forged body" not in _prompt(ctx)
    assert (await inbox_rows(env))[0].delivered_at is None


async def test_pre_turn_skips_the_inbox_for_the_continuation_context(make_layer):
    env = await make_layer(**CONT)
    await _row(env)
    ctx = await env.layer.pre_turn(env.agent, "S2", "hi", channel=CHAN, context_kind="continuation")
    assert "forged body" not in _prompt(ctx)
    assert (await inbox_rows(env))[0].delivered_at is None
    assert await env.heart.result_inbox.get_channel_session(CHAN) is None  # it did not even touch the channel


async def test_the_chat_claim_still_takes_an_owner_row(make_layer):  # PIN
    env = await make_layer(**CONT)
    await _row(env, source_kind="intention_report", msg_type="REPORT", body="owner-facing report")
    ctx = await env.layer.pre_turn(env.agent, "S2", "hi", channel=CHAN)
    assert "owner-facing report" in _prompt(ctx)
    assert '<result_message type="REPORT" source="intention_report"' in _prompt(ctx)
    assert (await inbox_rows(env))[0].delivered_at is not None


async def test_a_continue_result_is_never_injected_into_a_chat_turn(make_layer):
    env = await make_layer(**CONT)
    st = await make_subtask(env)
    await finish(env, st)
    await env.pool._record_inbox(st)
    ctx = await env.layer.pre_turn(env.agent, "S1", "hi", channel=CHAN)  # the very channel and session it came from
    assert RESULT not in _prompt(ctx)
    assert (await inbox_rows(env, st.id))[0].delivered_at is None
    assert (await intention_of(env, "subtask", st.id)).state == "result_ready"


async def test_a_continuation_turn_starts_no_deliberation(make_layer):
    env = await make_layer(**CONT)
    env.layer._deliberation.should_deliberate = AsyncMock(return_value=True)
    env.layer._deliberation.start = AsyncMock(return_value=None)
    await env.layer.pre_turn(env.agent, "S2", "hi", context_kind="continuation")
    env.layer._deliberation.start.assert_not_awaited()
    await env.layer.pre_turn(env.agent, "S3", "hi")  # control: an ordinary turn does
    env.layer._deliberation.start.assert_awaited_once()


# ---- IntentionClosePass ---------------------------------------------------------------------------------


@pytest.mark.postgres_only  # CAST(text AS uuid) join
async def test_the_close_pass_leaves_continue_and_report_alone(env_factory):
    env = await env_factory(**CONT)
    done = {}
    for policy in ("continue", "report", "remember", "none"):
        st = await make_subtask(env, policy=policy, routed=False)
        await finish(env, st)  # no writer ran
        done[policy] = st.id
    assert await IntentionClosePass(env.db, env.settings).run(limit=50) == 2
    states = {p: (await intention_of(env, "subtask", i)) for p, i in done.items()}
    assert (states["continue"].state, states["report"].state) == ("pending", "pending")
    assert [(states[p].state, states[p].close_reason) for p in ("remember", "none")] == [
        ("closed", "delivered"), ("closed", "delivered"),
    ]


@pytest.mark.postgres_only  # CAST(text AS uuid) join
async def test_with_continuation_off_the_close_pass_closes_every_policy_as_legacy(env_factory):  # PIN
    env = await env_factory(**ON)
    ids = []
    for policy in ("continue", "report", "remember"):
        st = await make_subtask(env, policy=policy, routed=False)
        await finish(env, st)
        ids.append(st.id)
    assert await IntentionClosePass(env.db, env.settings).run(limit=50) == 3
    for i in ids:
        assert (await intention_of(env, "subtask", i)).close_reason == "legacy"


@pytest.mark.postgres_only  # CAST(text AS uuid) join
async def test_close_finished_sources_honours_exclude_policies(env_factory):
    env = await env_factory(**CONT)
    st = await make_subtask(env, policy="remember", routed=False)
    await finish(env, st)
    async with env.db.session() as s:
        assert await intentions.close_finished_sources(s, env.agent, limit=10, exclude_policies=("remember",)) == []
        assert len(await intentions.close_finished_sources(s, env.agent, limit=10)) == 1
        await s.commit()


# ---- InboxDagPass ----------------------------------------------------------------------------------------


async def _dag_orchestrator(env, dags):
    from nous.dag.orchestrator import DAGOrchestrator

    delivery = DAGResultDelivery(
        env.settings,
        agent_id=env.agent,
        http=env.http,
        inbox=env.heart.result_inbox,
        intentions=env.heart.intentions,
    )
    loader = AsyncMock()
    loader._registry = MagicMock()
    return DAGOrchestrator(
        store=dags, subtask_mgr=AsyncMock(), dynamic_loader=loader, settings=env.settings, delivery=delivery
    )


@pytest.mark.postgres_only  # CAST(text AS uuid) in the pass filter
async def test_a_continue_dag_is_never_delivered_without_its_row(env_factory, monkeypatch):
    """Spec 7 'Routing'. The F087 delivery marks the DAG delivered (no required leg is left once the
    push stands down) even though its row failed to land. InboxDagPass must select it: a continuation
    DAG has no origin, and without the extra condition its lost row would never be repaired."""
    env = await env_factory(
        **CONT, telegram_bot_token="test-token", telegram_chat_id="77", dag_delivery_telegram_enabled=True
    )
    store = env.heart.result_inbox
    await store.ensure_enabled_at()
    dag, dags = await make_dag(env, policy="continue")  # no origin channel, no origin session
    orch = await _dag_orchestrator(env, dags)

    real = continuation.record_result
    calls = {"n": 0}

    async def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionError("db blip")
        return await real(*args, **kwargs)

    monkeypatch.setattr(continuation, "record_result", flaky)
    await orch._deliver_terminal_dags()
    delivered = await dags.get_dag(dag.id)
    assert delivered.delivered_at is not None  # marked delivered...
    assert await inbox_rows(env, dag.id) == []  # ...with no row
    env.http.post.assert_not_awaited()  # and the push stood down

    assert await InboxDagPass(env.db, store, env.settings).run(limit=10) == 1
    (row,) = await inbox_rows(env, dag.id)
    assert (row.channel, row.session_id, row.source_generation) == (None, None, delivered.delivery_generation)
    assert (await intention_of(env, "dag", dag.id)).state == "result_ready"
    assert await InboxDagPass(env.db, store, env.settings).run(limit=10) == 0  # idempotent
    await orch._deliver_terminal_dags()
    env.http.post.assert_not_awaited()


async def _delivered_dag(env, dag_id):
    """Mark a DAG delivered by hand, as F087's delivery sweep does."""
    async with env.db.session() as s:
        await s.execute(update(ExecutionDAG).where(ExecutionDAG.id == dag_id).values(delivered_at=datetime.now(UTC)))
        await s.commit()


@pytest.mark.postgres_only  # CAST(text AS uuid) in the pass filter
async def test_the_dag_pass_still_skips_an_unroutable_non_continue_dag(env_factory):  # PIN
    env = await env_factory(**CONT)
    store = env.heart.result_inbox
    await store.ensure_enabled_at()
    dag, _ = await make_dag(env, policy="remember")
    await _delivered_dag(env, dag.id)
    assert await InboxDagPass(env.db, store, env.settings).run(limit=10) == 0
    assert await inbox_rows(env, dag.id) == []


@pytest.mark.postgres_only  # CAST(text AS uuid) in the pass filter
async def test_with_continuation_off_the_dag_pass_skips_a_continue_dag_without_origin(env_factory):  # PIN
    env = await env_factory(**ON)
    store = env.heart.result_inbox
    await store.ensure_enabled_at()
    dag, _ = await make_dag(env, policy="continue")
    await _delivered_dag(env, dag.id)
    assert await InboxDagPass(env.db, store, env.settings).run(limit=10) == 0


# ---- InboxSubtaskPass ------------------------------------------------------------------------------------


@pytest.mark.postgres_only  # CAST(text AS uuid) in the pass filter
async def test_the_subtask_pass_selects_an_unrouted_continue_subtask_and_not_a_remember_one(env_factory):
    env = await env_factory(**CONT)
    store = env.heart.result_inbox
    await store.ensure_enabled_at()
    cont = await make_subtask(env, policy="continue", routed=False)  # e.g. a heartbeat check's spawn
    other = await make_subtask(env, policy="remember", routed=False)
    await finish(env, cont)
    await finish(env, other)  # neither hook ran
    assert await InboxSubtaskPass(env.db, store, env.settings).run(limit=10) == 1
    (row,) = await inbox_rows(env)
    assert (row.source_id, row.channel, row.session_id) == (cont.id, None, None)


@pytest.mark.postgres_only  # CAST(text AS uuid) in the pass filter
async def test_a_reported_continue_result_is_not_reselected_by_the_subtask_pass(env_factory):
    """MF-1. A continue result that became a report (its root was cancelled after the work finished) must
    leave a source-keyed row behind. Otherwise the pass re-selects it on every tick and, with a small
    batch, starves the source behind it."""
    env = await env_factory(**CONT)
    store = env.heart.result_inbox
    await store.ensure_enabled_at()
    stuck = await make_subtask(env)
    await finish(env, stuck)
    it = await intention_of(env, "subtask", stuck.id)
    await set_intention(env, it.id, state="closed", close_reason="resolved", root_cancelled_at=datetime.now(UTC))
    healthy = await make_subtask(env)
    await finish(env, healthy)  # its hook's write was lost
    pass_ = InboxSubtaskPass(env.db, store, env.settings)
    assert await pass_.run(limit=1) == 1  # the oldest: the stuck one becomes a report
    assert await pass_.run(limit=1) == 1  # the next slot goes to the healthy one, not to the stuck one again
    assert (await intention_of(env, "subtask", healthy.id)).state == "result_ready"
    assert await pass_.run(limit=1) == 0
    assert [r.source_kind for r in await inbox_rows(env, stuck.id)] == ["subtask"]  # the settled twin


@pytest.mark.postgres_only  # CAST(text AS uuid) in the pass filter
async def test_a_reported_continue_dag_is_settled_for_the_dag_pass(env_factory):
    env = await env_factory(**CONT)
    store = env.heart.result_inbox
    await store.ensure_enabled_at()
    dag, _ = await make_dag(env, policy="continue")  # unrouted: selected by its intention alone
    it = await intention_of(env, "dag", dag.id)
    await set_intention(env, it.id, state="closed", close_reason="resolved", root_cancelled_at=datetime.now(UTC))
    await _delivered_dag(env, dag.id)
    pass_ = InboxDagPass(env.db, store, env.settings)
    await pass_.run(limit=1)
    (twin,) = await inbox_rows(env, dag.id)  # without the settled twin the next tick selects it again
    assert (twin.channel, twin.session_id) == (None, None) and twin.delivered_at is not None
    assert await pass_.run(limit=1) == 0
    assert len(await inbox_rows(env, dag.id)) == 1
```

- [ ] **Step 2: Run; expect failure.**
  Run: `"$BIN/nous-test-linux.sh" "$WT" runner pg "$DB" tests/test_f099_phase2b_routing.py -q`
  Expected: `unexpected keyword argument 'context_kind'`; the pass tests fail (continue results closed as `legacy`, DAG not selected).

- [ ] **Step 3: `pre_turn`.** In `nous/cognitive/layer.py`, with the other `nous.brain` imports add `from nous.brain.continuation import INTENT_SESSION_PREFIX`. In `pre_turn`'s signature, after `channel: str | None = None,`:

```python
        # F099 Phase 2: the turn's ContextKind when the runner passes one (only a
        # 'continuation' turn does, from PR-2c). A continuation reads its results
        # by intention, so it neither claims the chat inbox nor opens a Plan decision.
        context_kind: str | None = None,
```

Step 3b becomes:

```python
        if getattr(self._settings, "result_inbox_enabled", False) is True:
            # F099: a continuation turn and an intent-<root> session read their rows by
            # intention (the runner stamps delivery in its fenced commit), never by claim.
            if context_kind != "continuation" and not str(session_id or "").startswith(INTENT_SESSION_PREFIX):
                system_prompt = await self._inject_result_inbox(
                    session_id, channel, system_prompt, sections_by_tier,
                )
        else:
```

Step 4:

```python
            if context_kind != "continuation" and await self._deliberation.should_deliberate(frame):
```

and at the top of `_inject_result_inbox` (belt and braces: a caller other than `pre_turn`):

```python
        if str(session_id or "").startswith(INTENT_SESSION_PREFIX):
            return system_prompt
```

- [ ] **Step 4: `close_finished_sources`.** In `nous/brain/intentions.py`:

```python
async def close_finished_sources(
    session: AsyncSession,
    agent_id: str,
    *,
    limit: int,
    reason: str = CLOSE_LEGACY,
    exclude_policies: tuple[str, ...] = (),
) -> list[UUID]:
```

Document the parameter in the docstring ("``exclude_policies``: wake policies this pass leaves to their own writer: F099 Phase 2's ``continue`` and ``report``"), and build the `due` query from a condition list:

```python
    conditions = [Intention.agent_id == agent_id, Intention.state == STATE_PENDING]
    if exclude_policies:
        conditions.append(Intention.wake_policy.notin_(exclude_policies))
    due = (
        select(Intention.id)
        .where(*conditions)
        .where(
            or_(
                and_(Intention.source_kind == SOURCE_SUBTASK, subtask_done),
                and_(Intention.source_kind == SOURCE_DAG, dag_done),
            )
        )
        .order_by(Intention.created_at)
        .limit(limit)
    )
```

- [ ] **Step 5: The reconciler.** In `nous/heart/result_reconciler.py` (imports: `continuation` is already there from Task 2b-5; add `WAKE_CONTINUE`, `WAKE_REPORT` via `intentions.WAKE_CONTINUE` and `intentions.WAKE_REPORT`):

`IntentionClosePass.run`:

```python
    async def run(self, *, limit: int) -> int:
        agent_id = self._settings.agent_id
        on = continuation.enabled(self._settings)
        async with self._db.session() as session:
            closed = await intentions.close_finished_sources(
                session,
                agent_id,
                limit=limit,
                reason=continuation.close_reason_for(self._settings),
                # Phase 2: a continue result is the writer's (the row and the move to result_ready are one
                # transaction) and a report closes with its insert; closing either here would strand a
                # result with no row. Their repair is PR-2c's repair_missing_results.
                exclude_policies=(intentions.WAKE_CONTINUE, intentions.WAKE_REPORT) if on else (),
            )
            containers = await intentions.close_finished_containers(session, agent_id, limit=limit)
            await session.commit()
```

(the log line below it is unchanged). Add a sentence to the class docstring: "With the continuation flag on it leaves `continue` and `report` intentions to their writers (F099 Phase 2)."

`InboxDagPass.run`: replace the `if not (settings.result_inbox_dag_scheduled and settings.telegram_chat_id):` block:

```python
        if not (settings.result_inbox_dag_scheduled and settings.telegram_chat_id):
            routable = or_(ExecutionDAG.origin_channel.is_not(None), ExecutionDAG.origin_session_id.is_not(None))
            if continuation.enabled(settings):
                # F099 Phase 2: a DAG a continuation spawned has no origin, and its row is keyed by its
                # intention alone. Without this its lost row would never be repaired. A closed
                # intention counts: a retried DAG (new generation) reopens it.
                routable = or_(
                    routable,
                    continuation.has_continue_intention(
                        settings.agent_id, SOURCE_DAG, ExecutionDAG.id, include_closed=True
                    ),
                )
            query = query.where(routable)
```

`InboxSubtaskPass.run`: replace `.where(or_(Subtask.parent_channel.is_not(None), Subtask.parent_session_id.is_not(None)))` by `.where(routable)` with, before the query:

```python
            routable = or_(Subtask.parent_channel.is_not(None), Subtask.parent_session_id.is_not(None))
            if continuation.enabled(self._settings):
                routable = or_(routable, continuation.has_continue_intention(agent_id, SOURCE_SUBTASK, Subtask.id))
```

- [ ] **Step 6: Run the tests, lint, commit**
  Run: `"$BIN/nous-test-linux.sh" "$WT" runner pg "$DB" tests/test_f099_phase2b_routing.py tests/test_f099_closing.py tests/test_f098_result_inbox.py tests/test_f099_phase2b_writers.py tests/test_f099_phase2b_suppression.py -q`
  Expected: PASS. Mutation checks (do them once, then revert): drop `exclude_policies` from `IntentionClosePass` and `test_the_close_pass_leaves_continue_and_report_alone` must fail; remove the `has_continue_intention` branch from `InboxDagPass` and `test_a_continue_dag_is_never_delivered_without_its_row` must fail at the repair assertion.

```bash
cd "$WT"
cat > /tmp/f099-2b-7.txt <<'EOF'
feat(F099): chat never claims a continue row, and the reconciler selects continuation work

pre_turn skips the result inbox for an intent- session and for a continuation
context (and starts no Plan decision for the latter). InboxDagPass and
InboxSubtaskPass also select a work row with a continue intention, so a result
keyed by its intention alone is still repaired. IntentionClosePass leaves
continue and report intentions to their writers when the flag is on.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_015q4W1nke7JzGaZkC21Whng
EOF
git add nous/cognitive/layer.py nous/brain/intentions.py nous/heart/result_reconciler.py tests/test_f099_phase2b_routing.py
git commit -F /tmp/f099-2b-7.txt
```

---

## Task 2b-8: The startup rollback, the wiring, the docs

**Files:**
- Modify: `nous/brain/continuation.py`: `RollbackReport`, `rollback_at_startup`
- Modify: `nous/main.py`: `_telegram_text_push`, `_rollback_continuation` (called right after `run_migrations`), `heart.result_inbox.set_bus(bus)`, `intentions=heart.intentions` in the `DAGResultDelivery(...)` call
- Modify: `docs/reference/project-structure.md`, `docs/reference/rest-api.md`, `docs/features/INDEX.md`
- Create: `tests/test_f099_phase2b_rollback.py`, `tests/test_f099_phase2b_wiring.py`

**Interfaces** (contract §4.7 and §4.3 item 6, names exactly):

```python
@dataclass(frozen=True, slots=True)
class RollbackReport:
    closed: int; rerouted_rows: int; expired_proposals: int; pushed_raw: int

async def rollback_at_startup(database, settings, *, telegram_push: Callable[[str], Awaitable[bool]] | None) -> RollbackReport
```

With `continuation.enabled(settings)` False (any other flag value, `NOUS_INTENTIONS_ENABLED` off included): in ONE transaction, for every open `continue` intention in `result_ready`, `deciding` or `awaiting_owner`: its undelivered intention-keyed inbox rows are re-routed to `owner_channel(settings, origin_channel)`, its `staged` and `pending` proposals are expired, and it is closed as `legacy` with the claim cleared; plus every `pending` intention whose source is already terminal (task-1.9 carry-over 2), closed as `legacy`. With `NOUS_RESULT_INBOX_ENABLED` off, re-routed rows would be invisible, so each row is sent by `telegram_push` and stamped delivered; an intention whose push failed stays open for the next start. A flag-on process returns an empty report and touches nothing. Never raises past `_rollback_continuation`.

- [ ] **Step 1: Write the failing tests.** Create `tests/test_f099_phase2b_rollback.py`:

```python
"""F099 Phase 2b: the startup rollback, with the continuation flag off (both flags off included)."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

import pytest
from f099_support import CHAN, CONT, ON, RESULT, env_factory, finish, inbox_rows, intention_of, make_subtask, set_intention  # noqa: F401
from sqlalchemy import select

from nous.brain import continuation
from nous.brain.intentions import IntentionSpec
from nous.config import Settings
from nous.storage.models import IntentionProposal

pytestmark = pytest.mark.postgres_only  # the sweep half uses a CAST(text AS uuid) join


def _off(env, **over) -> Settings:
    """The settings of the process that restarts with the flags off (the same agent)."""
    return Settings(_env_file=None, agent_id=env.agent, telegram_bot_token="", telegram_chat_id="", **over)


async def _stuck(env, *, state: str = "result_ready", routed: bool = True):
    """A continue intention with its NULL-keyed result, as a flag-on process left it."""
    st = await make_subtask(env, routed=routed)
    await finish(env, st)
    await env.pool._record_inbox(st)
    it = await intention_of(env, "subtask", st.id)
    if state != "result_ready":
        await set_intention(env, it.id, state=state, claim_token=uuid.uuid4(), claimed_at=datetime.now(UTC))
    return st, await intention_of(env, "subtask", st.id)


async def _proposal(env, it, state: str) -> uuid.UUID:
    async with env.db.session() as s:
        row = IntentionProposal(
            agent_id=env.agent, intention_id=it.id, root_id=it.root_id, tool="send_email", arguments={},
            rationale="r", claim_token=uuid.uuid4(), state=state,
        )
        s.add(row)
        await s.commit()
        return row.id


async def _proposal_state(env, pid) -> str:
    async with env.db.session() as s:
        return (await s.execute(select(IntentionProposal.state).where(IntentionProposal.id == pid))).scalar_one()


@pytest.mark.parametrize("state", ["result_ready", "deciding", "awaiting_owner"])
async def test_the_rollback_reroutes_expires_and_closes_with_both_flags_off(env_factory, state):
    env = await env_factory(**CONT)
    st, it = await _stuck(env, state=state)
    staged = await _proposal(env, it, "staged")
    pending = await _proposal(env, it, "pending")
    executed = await _proposal(env, it, "executed")
    off = _off(env, result_inbox_enabled=True)  # intentions and continuation both off
    report = await continuation.rollback_at_startup(env.db, off, telegram_push=None)
    assert (report.closed, report.rerouted_rows, report.expired_proposals, report.pushed_raw) == (1, 1, 2, 0)
    after = await intention_of(env, "subtask", st.id)
    assert (after.state, after.close_reason, after.claim_token, after.claimed_at) == ("closed", "legacy", None, None)
    assert [await _proposal_state(env, p) for p in (staged, pending, executed)] == ["expired", "expired", "executed"]
    (row,) = await inbox_rows(env, st.id)
    assert (row.channel, row.reply_to, row.session_id) == (CHAN, CHAN, None)
    rows, _ = await env.heart.result_inbox.claim(channel=CHAN, session_id="S9", max_age_hours=72, max_items=10)
    assert [r.id for r in rows] == [row.id]  # the next chat turn sees the result F098 style


async def test_a_second_rollback_changes_nothing(env_factory):
    env = await env_factory(**CONT)
    await _stuck(env)
    off = _off(env, result_inbox_enabled=True)
    await continuation.rollback_at_startup(env.db, off, telegram_push=None)
    again = await continuation.rollback_at_startup(env.db, off, telegram_push=None)
    assert (again.closed, again.rerouted_rows, again.expired_proposals, again.pushed_raw) == (0, 0, 0, 0)


async def test_a_row_with_no_origin_channel_goes_to_the_default_chat(env_factory):
    env = await env_factory(**CONT)
    st, _ = await _stuck(env, routed=False)
    off = _off(env, result_inbox_enabled=True, telegram_chat_id="4242")
    await continuation.rollback_at_startup(env.db, off, telegram_push=None)
    (row,) = await inbox_rows(env, st.id)
    assert row.channel == "telegram:4242"


async def test_a_row_with_nowhere_to_go_is_left_and_the_intention_still_closes(env_factory, caplog):
    env = await env_factory(**CONT)
    st, _ = await _stuck(env, routed=False)
    off = _off(env, result_inbox_enabled=True)  # no default chat either
    with caplog.at_level(logging.WARNING, logger="nous.brain.continuation"):
        report = await continuation.rollback_at_startup(env.db, off, telegram_push=None)
    assert (report.closed, report.rerouted_rows) == (1, 0)
    assert "no owner channel" in caplog.text
    assert (await intention_of(env, "subtask", st.id)).state == "closed"


async def test_an_owner_facing_row_keeps_its_channel_and_a_held_row_is_rerouted(env_factory):
    env = await env_factory(**CONT)
    st, it = await _stuck(env, state="awaiting_owner")
    async with env.db.session() as s:
        await continuation.insert_report(
            s, env.agent, kind="QUESTION", title="q", body="which?", channel="telegram:7", intention_id=it.id,
            root_id=it.root_id,
        )
        await s.commit()
    await continuation.rollback_at_startup(env.db, _off(env, result_inbox_enabled=True), telegram_push=None)
    by_kind = {r.source_kind: r for r in await inbox_rows(env)}
    assert by_kind["intention_report"].channel == "telegram:7"
    assert by_kind["subtask"].channel == CHAN


async def test_a_pending_intention_whose_source_already_finished_closes_as_legacy(env_factory):
    """Task-1.9 carry-over 2: work that finished while the flags were off left its intention pending."""
    env = await env_factory(**ON)
    finished = await make_subtask(env, policy="remember", routed=False)
    await finish(env, finished)  # no writer, no reconciler: the flags went off
    running = await make_subtask(env, policy="remember", routed=False)
    container = await env.heart.schedules.create(
        task="t",
        schedule_type="recurring",
        interval_seconds=1800,
        intention=IntentionSpec(intent="x", origin_kind="interactive", container=True),
    )
    report = await continuation.rollback_at_startup(env.db, _off(env), telegram_push=None)
    assert report.closed == 1
    assert (await intention_of(env, "subtask", finished.id)).close_reason == "legacy"
    assert (await intention_of(env, "subtask", running.id)).state == "pending"
    assert (await intention_of(env, "schedule", container.id)).state == "pending"


async def test_a_flag_on_process_never_rolls_back(env_factory):
    env = await env_factory(**CONT)
    st, _ = await _stuck(env)
    report = await continuation.rollback_at_startup(env.db, env.settings, telegram_push=None)
    assert (report.closed, report.rerouted_rows, report.expired_proposals, report.pushed_raw) == (0, 0, 0, 0)
    assert (await intention_of(env, "subtask", st.id)).state == "result_ready"
    assert (await inbox_rows(env, st.id))[0].channel is None


# ---- the inbox is off too: a raw Telegram send -------------------------------------------------------------


class _Push:
    def __init__(self, ok: bool = True) -> None:
        self.ok, self.sent = ok, []

    async def __call__(self, text: str) -> bool:
        self.sent.append(text)
        return self.ok


async def test_with_the_inbox_off_the_raw_result_is_sent_by_telegram(env_factory):
    env = await env_factory(**CONT)
    st, _ = await _stuck(env)
    push = _Push()
    report = await continuation.rollback_at_startup(env.db, _off(env), telegram_push=push)  # inbox off too
    assert (report.closed, report.pushed_raw, report.rerouted_rows) == (1, 1, 0)
    assert len(push.sent) == 1 and RESULT in push.sent[0]
    (row,) = await inbox_rows(env, st.id)
    assert row.delivered_at is not None and row.delivered_session_id == "rollback" and row.channel is None
    assert (await intention_of(env, "subtask", st.id)).state == "closed"


async def test_a_failed_raw_push_keeps_the_intention_open(env_factory):
    env = await env_factory(**CONT)
    st, _ = await _stuck(env)
    report = await continuation.rollback_at_startup(env.db, _off(env), telegram_push=_Push(ok=False))
    assert (report.closed, report.pushed_raw) == (0, 0)
    assert (await intention_of(env, "subtask", st.id)).state == "result_ready"
    assert (await inbox_rows(env, st.id))[0].delivered_at is None
    retry = await continuation.rollback_at_startup(env.db, _off(env), telegram_push=_Push())  # the next start
    assert (retry.closed, retry.pushed_raw) == (1, 1)


async def test_with_the_inbox_off_and_no_telegram_the_intention_still_closes(env_factory, caplog):
    env = await env_factory(**CONT)
    st, _ = await _stuck(env)
    with caplog.at_level(logging.WARNING, logger="nous.brain.continuation"):
        report = await continuation.rollback_at_startup(env.db, _off(env), telegram_push=None)
    assert (report.closed, report.pushed_raw) == (1, 0)
    assert "cannot be delivered" in caplog.text
```

Create `tests/test_f099_phase2b_wiring.py`:

```python
"""F099 Phase 2b: main.py's wiring — the rollback call, the bus, the delivery's intention store."""

from __future__ import annotations

import inspect
import logging
from types import SimpleNamespace

import pytest

import nous.main as main
from nous.brain import continuation
from nous.config import Settings


async def test_a_failed_rollback_never_blocks_startup(monkeypatch, caplog):
    async def boom(*args, **kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(continuation, "rollback_at_startup", boom)
    with caplog.at_level(logging.WARNING, logger="nous.main"):
        await main._rollback_continuation(Settings(_env_file=None), object())
    assert "retried at the next start" in caplog.text


async def test_the_rollback_gets_a_push_only_when_telegram_is_configured(monkeypatch):
    seen = []

    async def spy(database, settings, *, telegram_push):
        seen.append(telegram_push)
        return continuation.RollbackReport(0, 0, 0, 0)

    monkeypatch.setattr(continuation, "rollback_at_startup", spy)
    await main._rollback_continuation(Settings(_env_file=None, telegram_bot_token="", telegram_chat_id=""), object())
    await main._rollback_continuation(
        Settings(_env_file=None, telegram_bot_token="test-token", telegram_chat_id="4242"), object()
    )
    assert seen[0] is None and callable(seen[1])


class _FakeClient:
    status = 200
    boom = False
    posted: list = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None, timeout=None):
        if self.boom:
            raise ConnectionError("down")
        self.posted.append((url, json))
        return SimpleNamespace(status_code=self.status)


@pytest.mark.parametrize(("status", "boom", "expected"), [(200, False, True), (500, False, False), (200, True, False)])
async def test_the_raw_telegram_push(monkeypatch, status, boom, expected):
    client = type("C", (_FakeClient,), {"status": status, "boom": boom, "posted": []})
    monkeypatch.setattr(main.httpx, "AsyncClient", client)
    push = main._telegram_text_push(Settings(_env_file=None, telegram_bot_token="test-token", telegram_chat_id="4242"))
    assert await push("x" * 5000) is expected
    if not boom:
        (url, payload), = client.posted
        assert url == "https://api.telegram.org/bottest-token/sendMessage"
        assert payload["chat_id"] == "4242" and len(payload["text"]) == 3900


def test_create_components_wires_the_pieces_in_order():
    """PIN (by source): the gate before the first component, the rollback after the migrations and
    before the heart, the bus on the inbox store, the intention store on the DAG delivery."""
    src = inspect.getsource(main.create_components)
    order = [
        "_gate_continuation_flag(settings)",
        "Database(settings",
        "await run_migrations(database.engine)",
        "await _rollback_continuation(settings, database)",
        "heart = Heart(",
    ]
    assert [src.index(s) for s in order] == sorted(src.index(s) for s in order)
    assert "heart.result_inbox.set_bus(bus)" in src
    assert "intentions=heart.intentions" in src
```

- [ ] **Step 2: Run; expect failure.**
  Run: `"$BIN/nous-test-linux.sh" "$WT" runner pg "$DB" tests/test_f099_phase2b_rollback.py tests/test_f099_phase2b_wiring.py -q`
  Expected: `module 'nous.brain.continuation' has no attribute 'rollback_at_startup'` and `_rollback_continuation`.

- [ ] **Step 3: The rollback.** In `nous/brain/continuation.py` add the imports `from collections.abc import Awaitable, Callable`, `from sqlalchemy import update` (extend the existing sqlalchemy import), `from nous.storage.models import Intention, IntentionProposal, ResultInbox` and append:

```python
@dataclass(frozen=True, slots=True)
class RollbackReport:
    """What ``rollback_at_startup`` did (contract section 4.7)."""

    closed: int
    rerouted_rows: int
    expired_proposals: int
    pushed_raw: int


_ROLLBACK_STATES = (STATE_RESULT_READY, "deciding", "awaiting_owner")
_SWEEP_BATCH = 200
_RAW_PUSH_CHARS = 3900
# delivered_session_id of a row the rollback sent by Telegram instead of routing.
ROLLBACK_SESSION_ID = "rollback"


async def rollback_at_startup(
    database: Any, settings: Any, *, telegram_push: Callable[[str], Awaitable[bool]] | None
) -> RollbackReport:
    """Spec 4.3 item 6, T15: take the continuation out of the loop at startup (flag off).

    Open ``continue`` intentions in ``result_ready``, ``deciding`` or ``awaiting_owner`` have their
    undelivered intention-keyed inbox rows re-routed to ``owner_channel`` (so F098's chat turn shows
    them), their ``staged`` and ``pending`` proposals expired (a later tap is refused), and are closed
    as ``legacy`` with the claim cleared; so is every ``pending`` intention whose source is already
    terminal (work that finished while the flags were off: task-1.9 carry-over 2). It runs whenever
    ``brain.intentions`` exists, ``NOUS_INTENTIONS_ENABLED`` off included, and does nothing when the
    continuation flag is on. If the inbox is off too, a re-routed row would be invisible: each row is
    sent by ``telegram_push`` instead and stamped delivered, and an intention whose push failed stays
    open for the next start (a result is never dropped to make the close succeed).

    One transaction applies the re-route, the expiry and the close, so a close can never outrun its
    rows. The network sends happen before it, outside any transaction, so the raw push is
    at-least-once: a crash between the push and the commit re-sends on the next start (a duplicate
    costs less than a lost result; the inbox-off state is not prod's).
    """
    if enabled(settings):
        return RollbackReport(0, 0, 0, 0)
    agent_id = settings.agent_id
    inbox_on = getattr(settings, "result_inbox_enabled", False) is True

    async with database.session() as session:
        open_rows = list(
            (
                await session.execute(
                    select(Intention)
                    .where(
                        Intention.agent_id == agent_id,
                        Intention.wake_policy == intentions.WAKE_CONTINUE,
                        Intention.state.in_(_ROLLBACK_STATES),
                    )
                    .order_by(Intention.created_at)
                )
            )
            .scalars()
            .all()
        )
        stuck: dict[UUID, list[ResultInbox]] = {it.id: [] for it in open_rows}
        if open_rows:
            rows = (
                await session.execute(
                    select(ResultInbox)
                    .where(intention_keyed(agent_id, list(stuck)), ResultInbox.delivered_at.is_(None))
                    .order_by(ResultInbox.created_at)
                )
            ).scalars()
            for row in rows:
                stuck[row.intention_id].append(row)

    pushed_ids: list[UUID] = []
    keep_open: set[UUID] = set()
    if not inbox_on:
        waiting = sum(len(rows) for rows in stuck.values())
        if telegram_push is None and waiting:
            logger.warning(
                "F099: the rollback found %d result(s) that cannot be delivered (the inbox is off and Telegram is not "
                "configured); they stay on their work rows",
                waiting,
            )
        elif telegram_push is not None:
            for it in open_rows:
                for row in stuck[it.id]:
                    if await telegram_push(f"{row.title}\n\n{row.body}"[:_RAW_PUSH_CHARS]):
                        pushed_ids.append(row.id)
                    else:
                        keep_open.add(it.id)

    now = datetime.now(UTC)
    closing = [it for it in open_rows if it.id not in keep_open]
    close_ids = [it.id for it in closing]
    rerouted = expired = closed = 0
    async with database.session() as session:
        if pushed_ids:
            await session.execute(
                update(ResultInbox)
                .where(ResultInbox.id.in_(pushed_ids), ResultInbox.delivered_at.is_(None))
                .values(delivered_at=now, delivered_session_id=ROLLBACK_SESSION_ID)
                .execution_options(synchronize_session=False)
            )
        if inbox_on:
            for it in closing:
                row_ids = [r.id for r in stuck[it.id]]
                channel = owner_channel(settings, it.origin_channel)
                if row_ids and channel is None:
                    logger.warning(
                        "F099: the rollback found %d result(s) of intention %s with no owner channel (no origin "
                        "channel, no default chat); they stay on their work row",
                        len(row_ids),
                        it.id,
                    )
                elif row_ids:
                    moved = await session.execute(
                        update(ResultInbox)
                        .where(ResultInbox.id.in_(row_ids), ResultInbox.delivered_at.is_(None))
                        .values(channel=channel, reply_to=channel)
                        .execution_options(synchronize_session=False)
                    )
                    rerouted += moved.rowcount or 0
        if close_ids:
            gone = await session.execute(
                update(IntentionProposal)
                .where(
                    IntentionProposal.agent_id == agent_id,
                    IntentionProposal.intention_id.in_(close_ids),
                    IntentionProposal.state.in_(("staged", "pending")),
                )
                .values(state="expired", updated_at=now)
                .execution_options(synchronize_session=False)
            )
            expired = gone.rowcount or 0
            closed += len(
                (
                    await session.execute(
                        update(Intention)
                        .where(
                            Intention.agent_id == agent_id,
                            Intention.id.in_(close_ids),
                            Intention.state.in_(_ROLLBACK_STATES),
                        )
                        .values(
                            state=STATE_CLOSED,
                            close_reason=intentions.CLOSE_LEGACY,
                            closed_at=now,
                            updated_at=now,
                            claim_token=None,
                            claimed_at=None,
                        )
                        .returning(Intention.id)
                        .execution_options(synchronize_session=False)
                    )
                )
                .scalars()
                .all()
            )
        while True:
            swept = await intentions.close_finished_sources(session, agent_id, limit=_SWEEP_BATCH)
            closed += len(swept)
            if len(swept) < _SWEEP_BATCH:
                break
        await session.commit()
    return RollbackReport(closed, rerouted, expired, len(pushed_ids))
```

The two WARNING texts must contain the substrings the tests assert: `"no owner channel"` and `"cannot be delivered"` (they do).

- [ ] **Step 4: `main.py`.** Add `from collections.abc import Awaitable, Callable` to the imports if the module does not already have them. Next to `_gate_continuation_flag`:

```python
def _telegram_text_push(settings: Settings) -> Callable[[str], Awaitable[bool]] | None:
    """A raw Telegram text sender for the rollback's no-inbox fallback, or None when Telegram is not configured."""
    token, chat_id = settings.telegram_bot_token, settings.telegram_chat_id
    if not token or not chat_id:
        return None

    async def push(text: str) -> bool:
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    json={"chat_id": chat_id, "text": text[:3900]},
                    timeout=10,
                )
            return response.status_code < 400
        except Exception:
            logger.warning("F099: the rollback's Telegram push failed", exc_info=True)
            return False

    return push


async def _rollback_continuation(settings: Settings, database: Database) -> None:
    """F099 spec 4.3 item 6: runs at every start with the continuation flag off. A failure is
    logged and retried at the next start: it never blocks startup."""
    try:
        report = await continuation.rollback_at_startup(database, settings, telegram_push=_telegram_text_push(settings))
    except Exception:
        logger.warning("F099: the continuation rollback failed; it is retried at the next start", exc_info=True)
        return
    if report.closed or report.rerouted_rows or report.expired_proposals or report.pushed_raw:
        logger.info(
            "F099: continuation rollback closed %d intention(s), re-routed %d result(s), expired %d proposal(s), "
            "sent %d raw result(s) by Telegram",
            report.closed,
            report.rerouted_rows,
            report.expired_proposals,
            report.pushed_raw,
        )
```

In `create_components`: directly after `await run_migrations(database.engine)  # Apply pending SQL migrations` add `await _rollback_continuation(settings, database)`; directly after `bus = EventBus()` add

```python
        heart.result_inbox.set_bus(bus)  # F099: intention.result_ready, from every writer
```

and add `intentions=heart.intentions,` to the `DAGResultDelivery(...)` call (after the `inbox=` line, with a comment `# F099: a continue DAG's push stands down; a lineage DAG gets no summary turn`).

- [ ] **Step 5: Docs.**
  - `docs/reference/project-structure.md`, in the `brain/` tree after the `intentions.py` line: `│   │   ├── continuation.py     # F099 Phase 2: the continuation store: the inbox primitives, the same-transaction move of a continue result, owner-facing rows, the startup rollback (the runner follows in PR-2c)`.
  - `docs/reference/rest-api.md`, the `GET /dashboard/subtasks` row: after "per source kind (`subtask`, `dag`)" add "and `intention_report` (F099: owner-facing rows)".
  - `docs/features/INDEX.md`, the F099 row: add to the status cell "Phase 2b (data and routing: migration 084, the `NOUS_CONTINUATION_*` settings, intention-only routing of `continue` results, `delivered` closes, the startup rollback) merged dark: `main.py` forces `NOUS_CONTINUATION_ENABLED` off until the runner ships (PR-2e)".

- [ ] **Step 6: Run the tests, the full gate, lint, commit**
  Run: `"$BIN/nous-test-linux.sh" "$WT" runner pg "$DB" tests/test_f099_phase2b_rollback.py tests/test_f099_phase2b_wiring.py tests/test_f099_phase2b_settings.py tests/test_fix_z_maintenance_loops.py -q`
  Expected: PASS.
  Then the **full gate** (Implementer notes) and compare with the base; then `lint-delta.sh`.

```bash
cd "$WT"
cat > /tmp/f099-2b-8.txt <<'EOF'
feat(F099): the startup rollback and the Phase 2b wiring

With the continuation flag off, startup re-routes the undelivered results of
open continue intentions to their origin channel (or sends them by Telegram
when the inbox is off too), expires their pending proposals, and closes them
as legacy, together with every pending intention whose work already finished.
It runs even with both flags off and never blocks startup. main.py also gives
the inbox store the bus and the DAG delivery the intention store. Docs: the
module, the metrics key and the F099 status.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_015q4W1nke7JzGaZkC21Whng
EOF
git add nous/brain/continuation.py nous/main.py tests/test_f099_phase2b_rollback.py tests/test_f099_phase2b_wiring.py docs/reference/project-structure.md docs/reference/rest-api.md docs/features/INDEX.md
git commit -F /tmp/f099-2b-8.txt
```

---

## Self-review (done while writing; reviewers may re-run it)

**Spec coverage.**

| Spec item (§4.3 Phase 2 unless marked) | Where |
|---|---|
| Item 1: `continue` rows keyed by intention only, NULL channel and session; every writer treats `intention_id` as a routing key (worker hook, DAG listener, F087 backstop, reconciler); no default-chat substitution; chat claim cannot take them | 2b-5 (`route_result`, `test_a_continue_result_is_keyed_by_the_intention_alone`, `test_a_continue_dag_never_takes_the_default_chat`), 2b-7 (passes) |
| Item 1: `continuation` `pre_turn` and `intent-…` sessions skip injection | 2b-7 (`pre_turn`, three tests) |
| Item 1: delivery stamped in the fenced commit, `InboxDagPass` selects continuation DAGs | 2b-7 (filter); the stamp is 2c's |
| Item 2: same-transaction transition; held while `awaiting_owner`; inserted while `deciding` | 2b-4 (`record_result`; fault test; held test) |
| Item 3: re-arrivals reopen a closed `continue` with an open root, otherwise an `intention_report` with the raw result | 2b-4 (T6; the report branch, deterministic id); C8 for the non-`continue` writer branch |
| Item 4: owner-facing rows (`intention_report`; REPORT, QUESTION, PROPOSAL), keyed to origin channel or the default chat, fresh `report_id`, the widened CHECKs and UNIQUE, `metrics()` | 2b-1 (migration), 2b-3 (`insert_report`, `owner_channel`, `metrics`) |
| Item 5: raw pushes suppressed for a `continue` source (worker, F087 leg `superseded_by_continuation`); a `continue` DAG never delivered without its row | 2b-6, 2b-7 (`test_a_continue_dag_is_never_delivered_without_its_row`) |
| Item 6: rollback re-routes, expires proposals, closes `legacy`; with both flags off; the inbox-off Telegram fallback; pending with a terminal source | 2b-8 |
| §4.1 I4: `report` closes `delivered` in the insert's transaction; `none` and `remember` close `delivered` only with the flag on (ruling 2) | 2b-5 (`insert_and_close`, `close_reason_for`) |
| §4.1 I3: the F087 summary turn skipped for every `internal_only` DAG | 2b-6 (C6) |
| §5: the Phase 2 settings, the dependency rule, the timing rule | 2b-2 |
| §6 Phase 2: the two tables, the inbox changes, `test_all_tables_exist`, the CLAUDE.md count | 2b-1 |
| §7 Phase 2 "Routing": chat never claims; re-arrivals; DAG never delivered without its row; `InboxDagPass`; rollback with both flags off | 2b-5, 2b-7, 2b-8 (each named in Review Focus) |
| §7 Phase 2 "Commit": a result arriving while `deciding` | 2b-4 (held); the commit that returns it to `result_ready` is 2c |
| Ruling "flag gate": `CONTINUATION_RUNNER_READY`, `main.py`'s gate, a test | 2b-2 |
| Task-1.9 carry-overs: pending with a terminal source (2), I4 (4), `record_dag_result` terminal guard (1.8 follow-up), flags-off rows stay pending (1.8 note) | 2b-8, 2b-5 |

Not in 2b, by design: the runner, the claim and lease, `resolve_intention`, proposals' lifecycle and the publisher, REST routes, Telegram handlers, cancel, `repair_missing_results` (so a cancelled `continue`/`report` subtask's intention stays `pending` until 2c: the residual of Review Focus 1d), the `run_turn` pass-through of `context_kind`, the compose lines, and enforcement (2a).

**Type and name consistency** was checked against the contract:
- Constants and dataclasses (`CONTINUATION_RUNNER_READY`, `INTENT_SESSION_PREFIX`, `SOURCE_INTENTION_REPORT`, `MSG_REPORT/QUESTION/PROPOSAL`, `CLOSE_*`, `OUTCOME_*`, `DECISIONS`, `PROPOSAL_TERMINAL`, `OPEN_STATES`, `ResultRecorded`, `RollbackReport`): names and field order exactly contract §4.7.
- Functions: `record_result`, `close_delivered`, `insert_report` (plus the optional `report_id`, C2), `rollback_at_startup`: signatures exactly §4.7 (`record_result` plus the optional `arrival_id`, SF-2, which 2d needs). `insert_inbox_row`, `owner_channel`, `close_reason_for`, `enabled`, `intention_keyed`, `has_continue_intention`, `arrival_report_id` are additions, none of which collides with a contract name.
- `ResultInboxStore`: `insert(session=, arrival_id=, proposal_id=, push_after=)`, `set_bus`, `bus` exactly §4.9; `intention_of`, `record_continue_result`, `insert_and_close` are additions.
- Settings names, defaults and bounds, and the two validator names: exactly §4.3. `DAGResultDelivery.__init__(…, intentions=)`, `IntentionClosePass` exclusion (`exclude_policies`), `InboxDagPass` filter: §4.9 and §4.7.
- Bus event `intention.result_ready` with `{intention_id, root_id, agent_id}`: §4.13. Migration DDL: §4.2 (only comments reworded, C1).
- Test identifiers: `f099_support.{ON, CONT, CHAN, RESULT, env_factory, make_subtask, finish, make_dag, dag_kwargs, inbox_rows, intention_of, set_intention}` are defined in 2b-3 and used unchanged by 2b-4 to 2b-8.

**Phase 1 and F098 tests that must keep passing unchanged:** `tests/test_f099_routing_pins.py` (all), `tests/test_f098_result_inbox.py`, `tests/test_dag_delivery.py`, and `tests/test_f099_closing.py` except the one test renamed in 2b-5 (its Phase 1 assertions are kept, as a PIN with the flag off).

**Residuals recorded in the PR body.** (1) A cancelled lineage subtask's `continue` or `report` intention stays `pending` with the flag on until 2c. (2) `record_result` can leave an undelivered intention-only row behind a cancel that commits between its root read and its commit; 2e's cancel handles rows of a cancelled root. (3) Containers close as `legacy` (C5).

**Plan review (Fable 5.1, 2026-10-06), all applied.**
- **MUST-FIX.** MF-1: `record_result`'s report branch also writes the source-keyed row, NULL-keyed and stamped delivered, in the same transaction (`insert_inbox_row` gained `delivered_at` and `delivered_session_id`); pinned by `test_a_reported_result_settles_its_work_row_for_the_reconciler_passes` (2b-4) and, at the pass level, `test_a_reported_continue_result_is_not_reselected_by_the_subtask_pass` and `test_a_reported_continue_dag_is_settled_for_the_dag_pass` (2b-7). MF-2 (lead ruling): the failed-lookup branch of the F087 summary skip is gated on `continuation.enabled`; C6 and `test_a_failed_dag_intention_lookup_follows_the_continuation_flag` (2b-6) say so.
- **SHOULD-FIX.** SF-1 the schema test module is `postgres_only`; SF-2 `arrival_id` on `record_result` and `record_continue_result` (and a test); SF-3 C10 is named a flag-off change (C10 row, Global Constraints, PR-body list); SF-4 the rollback's real footprint (the terminal-source sweep) is worded in Global Constraints; SF-5 the raw push is documented as at-least-once; SF-6 `str(session_id or "")` before `startswith` in both `layer.py` places.
- **NITs applied:** 1 (the trailer split over two lines; the `_cap` call wrapped), 2 (the push suppression read comes after the token and chat check), 4 (relabelled pins), 5 (top-level `continuation` import in `main.py`), 6 (a dashboard-keys grep step in 2b-3), 7 (Review Focus 1d names `report` subtasks too). Not applied: 3 (two intention reads per DAG delivery, accepted), 8 (an aside for the lead), 9 (a cancelled `none`/`remember` closes as `delivered`; harmless, left as is).
- **Compose (lead ruling).** Task 2b-2 Step 7 adds the fourteen Phase 2 lines and the three F098 lines (`NOUS_RESULT_INBOX_DAG_SCHEDULED`, `NOUS_RESULT_MEMORY_ENABLED`, `NOUS_RESULT_MEMORY_SCHEDULED`), all with a real default, pinned by `test_compose_passes_every_phase2_setting_with_the_settings_default`. `NOUS_RESULT_INBOX_ENABLED` and `NOUS_INTENTIONS_ENABLED` were already there (PR-1).
