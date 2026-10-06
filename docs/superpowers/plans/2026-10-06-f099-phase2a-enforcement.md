# F099 Phase 2a: Enforcement substrate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make "autonomy is internal only" a property of the harness, not of the model's good behaviour: a turn whose authority is `internal_only` is offered, and may dispatch, only the tools the spec's §4.4 table allows, whatever the enforcement modes say. Nothing here reads `NOUS_CONTINUATION_ENABLED`, and no code in this PR creates a `continuation` or `approved_action` context.

**Architecture:** One PR, "2a", on top of PR-1 (`main` at `1c10ed8d`).
- **Kinds and constants.** `continuation` and `approved_action` join `ContextKind` with their `CONTEXT_POLICY` rows. The `internal_only` allowed set, denylist and per-call rules live in `nous/api/tool_policy.py`, a leaf below the runner.
- **Offered set.** One helper, `AgentRunner._offered_tools`, builds the tool list for both loops (frame tools, subtask exclusion, `tool_filter`, F078 refuse, then the `internal_only` narrowing, then per-call extra tools).
- **Dispatch.** `_authorize_tool_call` gets a strict block that runs first, before the offered-set mode check and before the `policy_mode == "off"` return. It refuses an unoffered tool, a call classified `external`, and a `write_file` outside `<workspace_dir>/intentions/<root_id>/`, for every mode setting. `cancel_task`'s own-lineage rule lives in its handler, fed by the dispatcher.
- **Terminal extra tools.** Only a tool named in `TERMINAL_EXTRA_TOOLS` ends the loop.
- **Phase 1 carry-overs.** A child's authority is `min(context, parent row)`; `dag_create` refuses an `approval` node for an `internal_only` turn; the D7 downgrade of `wake_policy` is visible in the tool's result text; the orchestrator fails closed on a lineage that is not a stamp.

**Tech Stack:** Python 3.12+, SQLAlchemy 2 async, PostgreSQL 17 + pgvector (CI and the local Postgres lane), pydantic v2 / pydantic-settings, pytest with `asyncio_mode = "auto"`.

**Spec:** `docs/superpowers/specs/2026-10-05-f099-intentions-and-continuation-design.md` (§4.1 Authority and D7, §4.4 Tool surface and enforcement, §7 Phase 2) and the Phase 2 decomposition and interface contract, `docs/superpowers/plans/2026-10-06-f099-phase2-contract.md` (§1.1, §3, §4.4, §4.5, §4.6 and §4.15), as amended by the lead rulings (`docs/superpowers/plans/2026-10-06-f099-phase2-lead-rulings.md`). Section references below (§4.4 …) are to the spec unless prefixed "contract". **Code base:** `main` at `1c10ed8d`, which is the PR-1 content. Anchor by function name, not by line number; line numbers below are from that base and will drift.

## Contract conflicts (resolved here; the lead should confirm)

| # | The contract says | The code says | This plan's resolution |
|---|---|---|---|
| C1 | `_authorize_tool_call` returns `Refusal(…, "internal_only")` (contract §4.5). | Both loops pass `refusal.code` to `_ledger_blocked`, which calls `LedgerStore.record_blocked`, which raises `ValueError` for any code outside `REFUSAL_CODES = {offered_set, action_gate, context_policy, duplicate}` (`nous/cognitive/ledger_store.py:49,307`). `_ledger_blocked` catches only `LedgerWriteError`, so the first forged call would crash the turn. | Task 2a.4 adds `"internal_only"` to `REFUSAL_CODES`. The dashboard's `_refusal_code` accepts it by membership, so a refused call shows its code. The test runs with a validating ledger store, so it fails on the base with the `ValueError`. |
| C2 | The strict refusal records `harness_context_policy_violation` with `"mode": "force_block"` (contract §4.5). | The dashboard counts "refused" only for `mode == "enforce"` (`nous/api/harness_dashboard.py` home summary), and `harness.ts` `alsoUnder` words any other mode as "flagged under X (the calls ran)", which is false for a refusal. | The event records `"mode": "enforce"` and `"violation": "internal_only:<code>"`. No dashboard change is needed, and the page words it truthfully ("refused under enforce"). If the lead prefers the literal `force_block`, both `harness_dashboard.py` and `harness.ts` need a case for it, in this PR. |
| C3 | The strict block ends with `self._root_cancelled(...)` and `Refusal(…, "root_cancelled")`; 2a must add `_root_cancelled`. | Contract §1.5 assigns the cancelled-root refusal and `set_cancelled_roots` to 2e, and `"root_cancelled"` is not a ledger refusal code either. | 2a does not add either. 2e adds the check, its refusal code and the default view together. |
| C4 | "`_tool_loop` keeps rebuilding `tools` each iteration by calling the helper again" (contract §4.5). | `ExecutionContext` is frozen, so `spawn_blocked` cannot change within a turn, and a per-iteration call would repeat the F078 refuse WARNING on every loop. | `_tool_loop` calls the helper once per turn, before the `while`, and copies the list each iteration exactly as it does today. The offered sets are identical. |
| C5 | `ExecutionContext.__post_init__` requires `proposal_id` + one declared tool for `approved_action`, and `internal_only` + both ids for `continuation` (contract §4.4). | `tests/test_execution_context.py::test_foreground_kinds_are_exactly_interactive_and_mcp` builds `ExecutionContext(kind=kind)` bare for every kind, so it would raise. | Task 2a.1 edits that one test to build a valid context per kind. The assertion is unchanged. |
| C6 | `_origin_args` always sends `_origin_authority` (contract §4.5). | Two Phase 1 tests pin the exact set of hidden keys (`tests/test_f099_lineage.py`: `test_origin_arguments_reach_origin_aware_tools_only` and `test_origin_arguments_the_model_sent_are_dropped`). | Task 2a.6 adds `"_origin_authority": "owner"` to those two expected dicts. Mechanical. |
| C7 | The D7 note is read "via the created row's intention (`heart.intentions.get_for_source`)" (contract §4.5). | `register_dag_tools` has no `heart`. `spawn_sync` returns a JSON blob, `schedule_task` ignores `wake_policy` (D3), and an inline `spawn_task` returns the subtask's own text. | The note is added to the fire-and-forget `spawn_task` and to `dag_create`, the two places a `wake_policy` can be downgraded in a result the model reads as a spawn receipt. One new read, `intentions.wake_policy_for_source`, reached through `heart.intentions` and through a `DAGStore.intention_wake_policy` mirror of `intention_lineage`. |
| C8 | The `cancel_task` rule's injection is `if ctx.root_intention_id is not None and name == "cancel_task"` (contract §4.5). | A damaged lineage stamp gives `authority == "internal_only"` and **no** `root_intention_id` (`lineage_from_stamp`). The contract's condition then injects nothing and the handler allows any cancel: fail-open. | The dispatcher injects whenever `ctx.authority == "internal_only"`, with an empty root id when there is none, and the handler refuses when it cannot prove the target is in the caller's own lineage. |
| C9 | `prepare_intention` takes `min(context authority, parent row authority)` (contract §4.5). | `resolve_wake_policy` decides "inside an internal-only lineage every result goes to the continuation" from the PARENT ROW's authority only. A continuation turn spawning under an `owner` root row would be narrowed but still get the origin's default wake policy, which breaks spec §7 "a lineage `dag_create` with `wake_policy="none"` still gets `continue`". | Task 2a.6 makes `resolve_wake_policy` treat `spec.origin_authority == internal_only` like an internal parent. |
| C10 | "Forged `tool_use` for `send_email`, `run_python` and `bash` is refused in **both** loops" (spec §7). | `stream_chat` hard-codes `ExecutionContext(kind="interactive", …)`, so no `internal_only` context reaches it in production. | `_offered_tools` is used there for one definition of the offered set, and parity is pinned. The streaming refusal test substitutes the context constructor in the runner module (a test seam, stated in the test) so the real streaming loop, its `offered_names` and the strict block run end to end. |
| C11 | Contract §4.5 says `self._workspace_dir` "exists only after `set_snapshot_store`". | It is set in `AgentRunner.__init__`. | No effect on this plan, which uses `self._settings.workspace_dir` (the root `write_file` itself binds). 2d should not plan around the false premise. |

## Global Constraints

- **Scope.** No runner, no migration, no new routing, no setting, no new REST route. Nothing from 2b to 2e: no `resolve_intention` or `propose_action` (their schemas, executors and `TOOL_CLASSES` entries are 2c and 2d), no `execute_single_call`, no cancelled-root view, no `intention_arrivals`. The terminal-tool mechanism lands here and `submit_final_report` is its only member until 2c.
- **Flag-off parity.** Every `owner` context (every context Phase 1 produces, because Phase 1 writes no `internal_only` row) gets the offered set it has today, byte for byte (Task 2a.3 pins it against a reference implementation of the old code; "byte-identical" is the offered SET, not the log line: the helper logs the F078 refuse strip at INFO, where `_tool_loop` logged WARNING, and only when there was a tool list to strip. The PR description names the level change), and the same authorization result in every mode (Task 2a.4 pins it). **The one flag-off behaviour change** (lead ruling, contract §1.7 and §3): a turn whose lineage stamp is damaged fails closed to `internal_only` and now loses its non-allowed tools. It loses tools and never gains one. The PR description names it (Task 2a.8).
- **Narrowing has no flag gate.** It keys on `ctx.authority == "internal_only"` alone (contract §3). A flag-gated safety invariant would un-narrow a lineage mid-flight when the flag is turned off.
- **Hidden arguments are the dispatcher's (security).** Every `_`-prefixed argument is set by `ToolDispatcher.dispatch` only; the blanket strip from Phase 1 stays. Each new hidden argument here (`_origin_authority`, `_authority`, `_root_intention_id`) has a forged-value test.
- **One definition each.** The allowed set is `tool_policy.internal_only_allowed`; the per-call rules are `tool_policy.internal_only_call_violation`; the offered set is `AgentRunner._offered_tools`. No loop, handler or test re-derives them (tests compare against literals).
- **Fail closed.** An unclassified tool is not allowed. A missing root id refuses `write_file` and `cancel_task`. A lineage that is not a stamp defers the DAG node.
- **One patch target.** Consumers of `nous/brain/intentions.py` call it through the module (`from nous.brain import intentions`).
- **Tests.** Real Postgres where the database is touched (the local lane, and CI); SQL that SQLite cannot run carries `@pytest.mark.postgres_only`. Pure tests need no database. Settings are always hermetic: `Settings(_env_file=None, …)`. Every test that writes rows uses its own agent (`f"f099-…-{uuid.uuid4().hex[:8]}"`), and keeps at most 5 subtasks `pending` per agent. Every test that wants the intentions flag on sets **both** flags: `Settings(_env_file=None, result_inbox_enabled=True, intentions_enabled=True, …)`. A task that appends to an existing test file shows its imports with `# noqa: E402`, so each task's block stands alone; the implementer may hoist them to the top of the file instead.
- **Test expectations are not negotiable.** If a test in this plan fails after the implementation step, fix the implementation. If you are sure the test itself is wrong (a fixture name, a helper signature that differs on `main`), fix only that mechanical detail and say so in the task report. Never weaken an assertion. The edits Task 2a.1 and Task 2a.6 make to existing tests (C5, C6) are mechanical by construction: they add the new keys and kinds, and change no existing expectation.
- **Fail-on-base rule, and its exception.** Every task contains at least one test that calls production code and fails on the task's base before the change. The exception: **pin** tests, which pass on the base by design and must keep passing; they are marked `# PIN` below. Their expected values are literals, and changing one is a behaviour change that needs its own review. Task 2a.3's parity test is a pin that also fails on its base, because `_offered_tools` does not exist there.
- **Commits.** Use explicit `git add <path> …`; never a directory, `.`, `-A` or `commit -a` (this is a public repo). Write the message to a file (`git commit -F <file>`), ending with these two lines:
  ```
  Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_015q4W1nke7JzGaZkC21Whng
  ```
  Put `set -o pipefail` before any `… | … && git commit` chain. Never use `git stash`.
- **Lint.** `lint-delta.sh <worktree>` must report clean: no new ruff finding and no format drift in a touched file.
- **Public repo.** No machine-local path, private host name, credential or personal name in any file, test, comment, commit message or PR text. Use `$BIN`, `$WT` and `$DB` in commands; say "the owner" or "the user".
- **Docs in the same PR.** Task 2a.8 updates the reference docs and `docs/features/INDEX.md`. No new setting, module or table is added, so no env-var row, project-structure row or table count changes.

## Review Focus

These are the five failure modes most likely to bite (seeded from the contract's list, filtered to 2a). Each has a test in the task named; reviewers should also check them by reading.

1. **A forged or unoffered call runs because a mode is `warn` or `off` (Task 2a.4).** The strict block sits before the offered-set mode check and before the `policy_mode == "off"` return. The tests run the forged `send_email`, `run_python` and `bash` calls under `off/off`, `warn/warn` and `enforce/enforce`, in `_tool_loop` and in `stream_chat`. Mutation check: move the strict block below the `policy_mode == "off"` return and the `off/off` cases must fail. Also check the ledger: a refusal whose code is not in `REFUSAL_CODES` raises out of the loop (C1).
2. **The two loops build different offered sets, or an owner turn changes (Task 2a.3).** `_offered_tools` replaces two hand-written copies. The owner-parity pin compares it with a reference implementation of the old `_tool_loop` code across every kind, `is_subtask`, `tool_filter`, `refuse_active` and `extra_tools`. Check the order (narrowing after `tool_filter` and refuse, before extra tools), and that `stream_chat` passes `is_subtask=False, tool_filter=None, extra_tools=None`.
3. **A damaged or missing lineage fails open (Tasks 2a.2, 2a.4, 2a.7, 2a.8).** Five doors: `write_file` with no root id, `cancel_task` with no root id or a target with no intention row (C8), the orchestrator's `isinstance(lineage, dict)` guard, a context with no `_origin_authority`, and a heartbeat callback that did not inherit its check's stamp (review S1). Each has a test that removes the door and watches an unsafe call succeed on the base.
4. **Authority widens through a spawn (Task 2a.6).** A child is `min(context, parent row)` (a continuation under an `owner` root row is `internal_only`), its wake policy follows the narrowed authority (C9), and a model-sent `_origin_authority` is dropped by the dispatcher. Check that code paths leave `origin_authority` `None`.
5. **The terminal-tool change strands or truncates a loop (Task 2a.5).** Before this PR any successful extra tool ended the loop; now only `TERMINAL_EXTRA_TOOLS` do. The only production caller passes `submit_final_report` (`nous/handlers/subtask_executor.py`), which stays terminal. Check with `grep -rn "extra_tools=" nous/` that no other caller relies on the old rule.

Residual, accepted and written in the PR description: the `write_file` path rule resolves the path when the call is authorized, and the write itself follows later (the existing write refuses symlinks and non-regular files at open, but a directory swapped between the two is not caught). A lineage may also read any file in the workspace (`read_file` is class `none`). Both match the spec's table. The F087 summary turn (`dag_summary`, `nous/dag/delivery.py`) still runs as `owner` and its email `spawn_task` becomes an owner root: that escape is closed by 2b (`DAGResultDelivery.deliver` skips the turn for `internal_only` DAGs), and is unreachable in 2a alone because no `internal_only` row exists until 2c creates a continuation.

## File map

| File | Responsibility | Tasks |
|---|---|---|
| `nous/api/execution_context.py` | `continuation` and `approved_action` kinds; `proposal_id`, `arrival_id`, `claim_token`, `spawn_blocked`; `__post_init__` rules | 2a.1 |
| `nous/api/tool_policy.py` | two `CONTEXT_POLICY` rows (2a.1); `INTERNAL_ONLY_*` constants, `internal_only_allowed`, `internal_only_call_violation` (2a.2) | 2a.1, 2a.2 |
| `nous/api/runner.py` | `_SUBTASK_EXCLUDED_TOOLS`, `_offered_tools` and both loops (2a.3); strict block in `_authorize_tool_call` (2a.4); `TERMINAL_EXTRA_TOOLS` (2a.5) | 2a.3, 2a.4, 2a.5 |
| `nous/cognitive/ledger_store.py` | `"internal_only"` in `REFUSAL_CODES` | 2a.4 |
| `nous/brain/intentions.py` | `IntentionSpec.origin_authority`, `spec_from_tool_call`, `resolve_wake_policy`, `prepare_intention` (min), `wake_policy_for_source` | 2a.6 |
| `nous/api/tools.py` | `_origin_args` (+authority), spawn handlers accept `_origin_authority`, `dag_create` approval refusal, D7 note (2a.6); `cancel_task` rule, dispatch injection and lineage logging (2a.7) | 2a.6, 2a.7 |
| `nous/dag/store.py` | `DAGStore.intention_wake_policy` | 2a.6 |
| `nous/dag/orchestrator.py` | the two lineage guards fail closed | 2a.8 |
| `nous/heartbeat/runner.py` | `_execute_callback` carries the check's lineage into the callback context | 2a.8 |
| `dashboard-app/src/views/Ledger.svelte` | two kinds in the context filter list | 2a.1 |
| Tests (new) | `tests/test_f099_context_kinds.py`, `tests/test_f099_tool_policy.py`, `tests/test_f099_offered_tools.py`, `tests/test_f099_enforcement.py`, `tests/test_f099_terminal_tools.py`, `tests/test_f099_authority.py` | all |
| Tests (edited) | `tests/test_execution_context.py` (2a.1), `tests/test_f099_lineage.py` (2a.6 expected dicts; 2a.8 appended tests) | 2a.1, 2a.6, 2a.8 |
| Docs | `docs/reference/environment-variables.md`, `docs/reference/shipped-features.md`, `docs/reference/project-structure.md`, `docs/features/INDEX.md` | 2a.8 |

---

## Implementer notes

**Scripts** live in the test-lane script directory provided at hand-off; call that `$BIN` below, and `$MAIN_VENV` is the main checkout's virtualenv. Run them from Git Bash.

**Your own database, before any targeted run that touches Postgres** (Tasks 2a.6, 2a.7, 2a.8). `nous-test-linux.sh` mounts the worktree read-only and applies **no** migrations, and the template database `nous_fix_base` stops at migration 080. Create one database per implementer and never share it: other agents use the same Postgres, and some tests `LOCK TABLE`.

```bash
WT=<path to your worktree>
DB=f099_<task>_<yourname>          # unique, lowercase
docker exec nous-postgres psql -U nous -d postgres -qc "DROP DATABASE IF EXISTS $DB" -qc "CREATE DATABASE $DB TEMPLATE nous_fix_base"
for f in $(ls "$WT"/sql/migrations/*.sql | sort); do
  n=$(basename "$f" | cut -c1-3)
  [ "$((10#$n))" -ge 81 ] && docker exec -i nous-postgres psql -U nous -d "$DB" -v ON_ERROR_STOP=1 -q < "$f"
done
```

**Targeted run.** `"$BIN/nous-test-linux.sh" "$WT" runner pg "$DB" tests/test_f099_context_kinds.py -q`. You may add `-k <name>`. The pure tests (Tasks 2a.1 to 2a.5) need no migrations but run in the same lane.

**Full gate** (once, before review). `"$BIN/gate-with-migrations.sh" f099-2a:"$WT":<fresh_db>:81` writes its output to the lane's gate directory as `f099-2a.raw.txt`. Compare failures with a gate of the PR's base. A failure that is also on the base is not yours (CI is the final gate).

**Lint.** `"$BIN/lint-delta.sh" "$WT"` must say `clean`. It enforces ruff's `E`/`F`/`I`/`UP` rules at line length 120, and `ruff format` on every **new** file. Before running it, format and fix the files you created:
```bash
RUFF="$MAIN_VENV/Scripts/ruff.exe"
"$RUFF" check --config "$WT/pyproject.toml" --fix <your new test files>
"$RUFF" format --config "$WT/pyproject.toml" <your new test files>
```
Run `ruff format` on an existing file only if it was format-clean on the base (lint-delta reports "FORMAT drift" exactly in that case).

**Do not run** pytest against the shared `nous` database, or against another agent's database.

**Branch.** `feat/f099-2a-enforcement-substrate`, from `origin/main` (which must contain PR-1; check with `git log --oneline origin/main -- nous/brain/intentions.py`). 2b is planned in parallel and edits other files; the one shared file is `nous/brain/intentions.py` (this PR adds `origin_authority` and `wake_policy_for_source`; 2b adds the `IntentionClosePass` exclusion). Both edits are additive; whichever merges second rebases.

---

### Task 2a.1: The two context kinds, their policy rows and fields

**Files:**
- Modify: `nous/api/execution_context.py`: `ContextKind`, `ExecutionContext` fields and `__post_init__`
- Modify: `nous/api/tool_policy.py`: two rows in `CONTEXT_POLICY`
- Modify: `dashboard-app/src/views/Ledger.svelte`: the `CONTEXTS` list
- Modify: `tests/test_execution_context.py`: one test builds a valid context per kind (C5)
- Create: `tests/test_f099_context_kinds.py`

**Interfaces:**
- Produces: `ContextKind` gains `"continuation"` and `"approved_action"`. Neither joins `FOREGROUND_KINDS` (so `tool_policy.evaluate`, which returns `None` for a foreground kind, still applies the policy), and both are background (`is_background` is true).
- Produces: `ExecutionContext.proposal_id: UUID | None = None`, `arrival_id: UUID | None = None`, `claim_token: UUID | None = None`, `spawn_blocked: bool = False`. All defaulted, so every existing constructor call is unchanged.
- Produces: `__post_init__` rejects a `continuation` context that is not `internal_only` or lacks `intention_id` or `root_intention_id`, and an `approved_action` context without a `proposal_id` or without exactly one declared tool. A continuation can never be built wide.
- Produces: `CONTEXT_POLICY["continuation"] = ContextPolicy(_LOCAL, spawn=frozenset({"spawn_task", "dag_create"}))` and `CONTEXT_POLICY["approved_action"] = ContextPolicy(_ALL, spawn=True)`. The second narrows to one tool through `declared_tools`, via the existing `undeclared` rule.
- No caller creates either kind in this PR.

- [ ] **Step 1: Write the failing tests.** Create `tests/test_f099_context_kinds.py`:

```python
"""F099 Phase 2a: the continuation and approved_action contexts.

Neither kind is created by any caller yet (2c and 2d build them); this pins
what each may be, and what the policy table says each may do.
"""

from __future__ import annotations

import uuid

import pytest

from nous.api.execution_context import CONTEXT_KINDS, FOREGROUND_KINDS, ExecutionContext
from nous.api.tool_policy import CONTEXT_POLICY, evaluate

IID, RID, PID = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()


def _continuation(**over) -> ExecutionContext:
    base = {
        "kind": "continuation",
        "session_id": "intent-x",
        "authority": "internal_only",
        "intention_id": IID,
        "root_intention_id": RID,
    }
    return ExecutionContext(**{**base, **over})


def _approved(**over) -> ExecutionContext:
    base = {"kind": "approved_action", "session_id": "proposal-x", "proposal_id": PID, "declared_tools": ("send_email",)}
    return ExecutionContext(**{**base, **over})


@pytest.mark.parametrize("kind", ["continuation", "approved_action"])
def test_the_new_kinds_are_background_kinds(kind):
    assert kind in CONTEXT_KINDS
    assert kind not in FOREGROUND_KINDS
    assert (_continuation() if kind == "continuation" else _approved()).is_background is True


def test_the_new_fields_default_so_existing_constructors_are_unchanged():
    ctx = ExecutionContext(kind="subtask")
    assert (ctx.proposal_id, ctx.arrival_id, ctx.claim_token, ctx.spawn_blocked) == (None, None, None, False)


def test_a_continuation_carries_its_arrival_and_claim():
    arrival, token = uuid.uuid4(), uuid.uuid4()
    ctx = _continuation(arrival_id=arrival, claim_token=token, spawn_blocked=True)
    assert (ctx.arrival_id, ctx.claim_token, ctx.spawn_blocked) == (arrival, token, True)


@pytest.mark.parametrize(
    "over",
    [
        {"authority": "owner"},
        {"intention_id": None},
        {"root_intention_id": None},
    ],
    ids=["owner-authority", "no-intention", "no-root"],
)
def test_a_continuation_can_never_be_built_wide_or_without_its_lineage(over):
    with pytest.raises(ValueError, match="continuation"):
        _continuation(**over)


@pytest.mark.parametrize(
    "over",
    [
        {"proposal_id": None},
        {"declared_tools": None},
        {"declared_tools": ()},
        {"declared_tools": ("send_email", "bash")},
    ],
    ids=["no-proposal", "no-declared-tool", "empty-declared-tools", "two-declared-tools"],
)
def test_an_approved_action_names_its_proposal_and_exactly_one_tool(over):
    with pytest.raises(ValueError, match="approved_action"):
        _approved(**over)


def test_the_policy_rows_are_the_contracts():
    cont = CONTEXT_POLICY["continuation"]
    assert cont.levels == frozenset({"none", "write"})
    assert cont.spawn == frozenset({"spawn_task", "dag_create"})
    appr = CONTEXT_POLICY["approved_action"]
    assert appr.levels == frozenset({"none", "write", "external", "irreversible"})
    assert appr.spawn is True


def test_a_continuation_may_spawn_only_the_two_spawn_tools_and_never_send():
    ctx = _continuation()
    assert evaluate(ctx, "spawn_task", {}) is None
    assert evaluate(ctx, "dag_create", {}) is None
    assert evaluate(ctx, "recall_deep", {}) is None
    assert evaluate(ctx, "schedule_task", {}) == "spawn"
    assert evaluate(ctx, "spawn_sync", {}) == "spawn"
    assert evaluate(ctx, "send_email", {}) == "level:external"
    assert evaluate(ctx, "bash", {"command": "curl https://example.com"}) == "level:external"


def test_an_approved_action_runs_exactly_its_declared_tool():
    ctx = _approved()
    assert evaluate(ctx, "send_email", {}) is None
    assert evaluate(ctx, "bash", {"command": "ls"}) == "undeclared"
    assert evaluate(ctx, "recall_deep", {}) == "undeclared"
    # Wide levels: the proposed call is outward or a denylisted local tool by definition.
    assert evaluate(_approved(declared_tools=("bash",)), "bash", {"command": "curl https://example.com"}) is None
```

- [ ] **Step 2: Run them; confirm they fail on the base.**

Run: `"$BIN/nous-test-linux.sh" "$WT" runner pg "$DB" tests/test_f099_context_kinds.py -q`
Expected: every test FAILS (`unknown execution context kind 'continuation'`, or a `KeyError` on the policy row).

- [ ] **Step 3: Edit `nous/api/execution_context.py`.**

3a. In `ContextKind`, after the `"background"` line:

```python
    "background",          # a background turn whose caller named no kind
    "continuation",        # F099: Nous's own turn on a background result (internal_only)
    "approved_action",     # F099: one owner-approved proposal, run with no model
]
```

3b. In `ExecutionContext`, after `authority: str = AUTHORITY_OWNER`:

```python
    # F099 Phase 2: set only by the continuation runner (a continuation turn) and
    # by execute_approved_proposal (an approved_action call). Defaults leave every
    # other context unchanged.
    proposal_id: UUID | None = None  # approved_action: the proposal being run
    arrival_id: UUID | None = None  # continuation: the arrival this turn decides
    claim_token: UUID | None = None  # continuation: the claim this turn runs under
    spawn_blocked: bool = False  # continuation: the root is at its depth or spawn limit
```

3c. Extend `__post_init__`:

```python
    def __post_init__(self) -> None:
        if self.kind not in CONTEXT_KINDS:
            raise ValueError(f"unknown execution context kind {self.kind!r}")
        if self.authority not in AUTHORITIES:
            raise ValueError(f"unknown authority {self.authority!r}")
        if self.kind == "approved_action" and (self.proposal_id is None or len(self.declared_tools or ()) != 1):
            raise ValueError("an approved_action context needs a proposal_id and exactly one declared tool")
        if self.kind == "continuation" and (
            self.authority != AUTHORITY_INTERNAL or self.intention_id is None or self.root_intention_id is None
        ):
            raise ValueError("a continuation context is internal_only and names its intention and its root")
```

- [ ] **Step 4: Edit `nous/api/tool_policy.py`.** In `CONTEXT_POLICY`, after the `"background"` row:

```python
        "background": ContextPolicy(_LOCAL, spawn=False),
        # F099: Nous's own turn on a background result. Local levels only, and the
        # two spawn tools; _offered_tools and the strict path in
        # _authorize_tool_call narrow it further (the policy is the floor).
        "continuation": ContextPolicy(_LOCAL, spawn=frozenset({"spawn_task", "dag_create"})),
        # F099: one owner-approved call, declared_tools=(tool,). Levels wide because the
        # proposed call is by definition outward or a denylisted local tool.
        "approved_action": ContextPolicy(_ALL, spawn=True),
```

- [ ] **Step 5: Edit the two Phase 1 files that must change with the kinds.**

`dashboard-app/src/views/Ledger.svelte`: add the two kinds to `CONTEXTS`:

```ts
  const CONTEXTS = ['interactive', 'mcp', 'subtask', 'dag_node', 'scheduled', 'agent_action',
    'heartbeat_triage', 'heartbeat_check', 'heartbeat_callback', 'dag_summary', 'background',
    'continuation', 'approved_action'];
```

`tests/test_execution_context.py` (C5): the first test builds every kind bare. Replace it with a builder that gives the two new kinds what they require; the assertion is unchanged.

```python
def _valid_context(kind: str) -> ExecutionContext:
    """A constructible context of ``kind``: F099's two kinds need their own fields."""
    if kind == "continuation":
        return ExecutionContext(
            kind=kind, authority="internal_only", intention_id=uuid.uuid4(), root_intention_id=uuid.uuid4()
        )
    if kind == "approved_action":
        return ExecutionContext(kind=kind, proposal_id=uuid.uuid4(), declared_tools=("send_email",))
    return ExecutionContext(kind=kind)


def test_foreground_kinds_are_exactly_interactive_and_mcp():
    assert FOREGROUND_KINDS == frozenset({"interactive", "mcp"})
    for kind in CONTEXT_KINDS:
        assert _valid_context(kind).is_background is (kind not in FOREGROUND_KINDS)
```

- [ ] **Step 6: Run the tests and their neighbours.**

Run: `"$BIN/nous-test-linux.sh" "$WT" runner pg "$DB" tests/test_f099_context_kinds.py tests/test_execution_context.py tests/test_tool_policy.py tests/test_f099_lineage.py -q`
Expected: PASS. (`test_every_context_kind_has_a_policy` passes because the rows landed with the kinds.) Then, from `dashboard-app/`, `npm run build` still succeeds (the list is a string array).

- [ ] **Step 7: Lint and commit**

```bash
cd "$WT"
cat > /tmp/f099-2a-1.txt <<'EOF'
feat(F099): the continuation and approved_action context kinds

Two ContextKinds with CONTEXT_POLICY rows (a continuation is local-only and
may spawn spawn_task and dag_create; an approved_action runs exactly its one
declared tool) and the fields they carry. A continuation context can only be
built internal_only with its lineage; an approved_action needs its proposal
and exactly one declared tool. Nothing creates either kind yet.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_015q4W1nke7JzGaZkC21Whng
EOF
git add nous/api/execution_context.py nous/api/tool_policy.py dashboard-app/src/views/Ledger.svelte tests/test_execution_context.py tests/test_f099_context_kinds.py
git commit -F /tmp/f099-2a-1.txt
```

### Task 2a.2: The `internal_only` allowed set and per-call rules

**Files:**
- Modify: `nous/api/tool_policy.py`: four constants, `internal_only_allowed`, `internal_only_call_violation`
- Create: `tests/test_f099_tool_policy.py`

**Interfaces:**
- Consumes: `tool_class`, `classify_side_effect`, `ExecutionContext` (`kind`, `spawn_blocked`, `root_intention_id`), `AUTHORITY_INTERNAL`.
- Produces (contract §4.5, copied verbatim): `INTERNAL_ONLY_DENYLIST: frozenset[str]`, `INTERNAL_ONLY_SPAWN_TOOLS = frozenset({"spawn_task", "dag_create"})`, `INTERNAL_ONLY_CHECKED_TOOLS = frozenset({"write_file", "cancel_task"})`, `INTERNAL_ONLY_LOGGED_TOOLS = frozenset({"web_fetch", "web_search"})`.
- Produces: `internal_only_allowed(name: str, *, ctx: ExecutionContext) -> bool`. A tool is allowed when its class is `none` or `write`, it is not on the denylist, and, if it is a spawn tool, `ctx.kind == "continuation"` and `not ctx.spawn_blocked`. **An unclassified tool is not allowed.**
- Produces: `internal_only_call_violation(ctx, name, tool_input, *, workspace_dir) -> str | None`. `"external"` when `classify_side_effect` rates the call `external` or `irreversible` (a URL in `run_python`, a `curl` in `bash`, `send_email`); `"write_path"` for a `write_file` whose resolved target is not under `<workspace_dir>/intentions/<root_id>/` (no root id, no path, a path with a NUL byte, a `..` escape, a symlink out); `"foreign_cancel"` for a `cancel_task` whose `task_id` is not a UUID. The sync function cannot read rows, so the lineage check itself is in `cancel_task`'s handler (Task 2a.7). `None` otherwise.
- The allowed set for a lineage that is not a continuation is **16 names** (pinned below as a literal). A tool added later is denied until someone adds it on purpose; the pin test says so in its message.

- [ ] **Step 1: Write the failing tests.** Create `tests/test_f099_tool_policy.py`:

```python
"""F099 section 4.4: what an internal_only turn may be offered, and may do per call.

Pure: no database. The allowed set is pinned as a literal on purpose. A tool
added later fails closed (it is not in the set), and this test is where its
author decides, with a reviewer, whether a lineage may use it.
"""

from __future__ import annotations

import os
import uuid

import pytest

from nous.api.execution_context import CONTEXT_KINDS, ExecutionContext
from nous.api.tool_classes import TOOL_CLASSES
from nous.api.tool_policy import (
    INTERNAL_ONLY_CHECKED_TOOLS,
    INTERNAL_ONLY_DENYLIST,
    INTERNAL_ONLY_LOGGED_TOOLS,
    INTERNAL_ONLY_SPAWN_TOOLS,
    internal_only_allowed,
    internal_only_call_violation,
)

# Fixed ids: parametrize ids built from them must not change between collections (xdist).
IID, RID, OTHER = (uuid.UUID(f"00000000-0000-0000-0000-00000000000{n}") for n in (1, 2, 3))

# The tools a lineage may be offered when it is not a continuation. A class of
# none or write, not on the denylist, and not a spawn tool.
LINEAGE_ALLOWED = frozenset(
    {
        "recall_deep", "recall_recent", "read_file", "get_procedure", "web_search", "web_fetch", "list_tasks",
        "cache_retrieve", "recall_hubs", "list_decisions", "submit_final_report",
        "write_file", "learn_fact", "record_decision", "cancel_task", "ingest_document",
    }
)


def _internal(kind: str = "subtask", **over) -> ExecutionContext:
    base = {"kind": kind, "session_id": "s", "authority": "internal_only", "intention_id": IID, "root_intention_id": RID}
    return ExecutionContext(**{**base, **over})


def _allowed(ctx: ExecutionContext) -> frozenset[str]:
    return frozenset(name for name in TOOL_CLASSES if internal_only_allowed(name, ctx=ctx))


@pytest.mark.parametrize(
    "kind",
    [k for k in CONTEXT_KINDS if k not in ("continuation", "approved_action")],
)
def test_a_lineage_turn_that_is_not_a_continuation_is_offered_exactly_the_pinned_set(kind):
    got = _allowed(_internal(kind))
    assert got == LINEAGE_ALLOWED, (
        f"the internal_only allowed set changed. Added: {sorted(got - LINEAGE_ALLOWED)}; "
        f"removed: {sorted(LINEAGE_ALLOWED - got)}. A tool a lineage may use is a security decision: "
        "update LINEAGE_ALLOWED only with a reviewer's agreement."
    )


def test_a_continuation_is_also_offered_the_two_spawn_tools_until_its_root_is_at_a_limit():
    cont = _internal("continuation")
    assert _allowed(cont) == LINEAGE_ALLOWED | INTERNAL_ONLY_SPAWN_TOOLS
    assert _allowed(_internal("continuation", spawn_blocked=True)) == LINEAGE_ALLOWED


def test_no_denylisted_external_or_irreversible_tool_is_ever_allowed():
    for kind in CONTEXT_KINDS:
        if kind == "approved_action":
            continue  # not an internal_only context
        allowed = _allowed(_internal(kind))
        assert not allowed & INTERNAL_ONLY_DENYLIST
        assert all(TOOL_CLASSES[n].side_effect in ("none", "write") for n in allowed)
        assert not allowed & {"send_email", "send_file"}


def test_an_unclassified_tool_fails_closed():
    assert internal_only_allowed("a_tool_nobody_classified", ctx=_internal("continuation")) is False


def test_the_denylist_names_only_classified_tools_and_is_the_specs():
    assert INTERNAL_ONLY_DENYLIST <= set(TOOL_CLASSES), "a stale denylist entry guards nothing"
    assert INTERNAL_ONLY_DENYLIST == frozenset(
        {
            "schedule_task", "heartbeat_check_create", "heartbeat_check_manage", "create_censor", "learn_skill",
            "store_identity", "complete_initiation", "dag_manage", "push_surface", "compose_surface", "bash",
            "run_python", "spawn_sync", "resolve_decision", "resolve_decisions",
        }
    )
    assert INTERNAL_ONLY_CHECKED_TOOLS == frozenset({"write_file", "cancel_task"})
    assert INTERNAL_ONLY_LOGGED_TOOLS == frozenset({"web_fetch", "web_search"})


# -- per-call rules ---------------------------------------------------------


def _violation(name: str, tool_input: dict, tmp_path, ctx: ExecutionContext | None = None) -> str | None:
    return internal_only_call_violation(ctx or _internal(), name, tool_input, workspace_dir=str(tmp_path))


@pytest.mark.parametrize(
    ("name", "tool_input"),
    [
        ("send_email", {"to": "a@example.com", "subject": "s", "body": "b"}),
        ("send_file", {"path": "x"}),
        ("run_python", {"code": "import smtplib"}),
        ("run_python", {"code": "print(open('https://example.com'))"}),
        ("bash", {"command": "curl https://example.com"}),
    ],
)
def test_a_call_rated_external_is_a_violation_whatever_the_tool(name, tool_input, tmp_path):
    assert _violation(name, tool_input, tmp_path) == "external"


@pytest.mark.parametrize(
    ("name", "tool_input"),
    [("run_python", {"code": "print(1)"}), ("bash", {"command": "ls"}), ("recall_deep", {"query": "x"})],
)
def test_a_local_call_is_not(name, tool_input, tmp_path):
    assert _violation(name, tool_input, tmp_path) is None


def _write(tmp_path, path, ctx=None):
    return _violation("write_file", {"path": path, "content": "x"}, tmp_path, ctx)


def test_write_file_under_the_root_dir_is_allowed(tmp_path):
    assert _write(tmp_path, f"intentions/{RID}/notes.md") is None
    assert _write(tmp_path, f"intentions/{RID}/deep/er/notes.md") is None
    assert _write(tmp_path, str(tmp_path / "intentions" / str(RID) / "abs.md")) is None


@pytest.mark.parametrize(
    "path",
    [
        "notes.md",
        f"intentions/{OTHER}/notes.md",
        f"intentions/{RID}/../{OTHER}/notes.md",
        f"intentions/{RID}/../../outside.md",
        "../outside.md",
        "/etc/passwd",
        "intentions",
        f"intentions/{str(RID)[:8]}/notes.md",
    ],
)
def test_write_file_outside_the_root_dir_is_refused(tmp_path, path):
    assert _write(tmp_path, path) == "write_path"


def test_write_file_with_no_root_or_no_usable_path_is_refused(tmp_path):
    damaged = _internal(root_intention_id=None, intention_id=None)
    assert _write(tmp_path, f"intentions/{RID}/notes.md", damaged) == "write_path"
    for tool_input in ({}, {"path": ""}, {"path": 7}, {"path": "a\x00b"}):
        assert _violation("write_file", tool_input, tmp_path) == "write_path"


@pytest.mark.skipif(os.name == "nt", reason="needs symlinks")
def test_write_file_through_a_symlink_that_leaves_the_root_dir_is_refused(tmp_path):
    root_dir = tmp_path / "intentions" / str(RID)
    root_dir.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (root_dir / "link").symlink_to(outside)
    assert _write(tmp_path, f"intentions/{RID}/link/x.md") == "write_path"


def test_cancel_task_needs_a_uuid_here_and_the_lineage_check_is_the_handlers(tmp_path):
    assert _violation("cancel_task", {"task_id": str(uuid.uuid4())}, tmp_path) is None
    for tool_input in ({}, {"task_id": "not-a-uuid"}, {"task_id": None}, {"task_id": 12}):
        assert _violation("cancel_task", tool_input, tmp_path) == "foreign_cancel"
```

- [ ] **Step 2: Run them; confirm they fail on the base.**

Run: `"$BIN/nous-test-linux.sh" "$WT" runner pg "$DB" tests/test_f099_tool_policy.py -q`
Expected: collection ERROR (`ImportError: cannot import name 'INTERNAL_ONLY_CHECKED_TOOLS'`).

- [ ] **Step 3: Implement.** In `nous/api/tool_policy.py`, add `import uuid` and `from pathlib import Path` to the imports (the module already imports `Mapping`, `Any`, `ExecutionContext`, `tool_class` and `classify_side_effect`). After `CONTEXT_POLICY` (and `undoable_violation`/`evaluate` may stay where they are), append:

```python
# F099 section 4.4: tools an internal_only turn may NOT use although their class is
# none or write. They schedule, persist policy, reach the host, or resolve the Brain's
# own records.
INTERNAL_ONLY_DENYLIST: frozenset[str] = frozenset({
    "schedule_task", "heartbeat_check_create", "heartbeat_check_manage",
    "create_censor", "learn_skill", "store_identity", "complete_initiation",
    "dag_manage", "push_surface", "compose_surface", "bash", "run_python",
    "spawn_sync", "resolve_decision", "resolve_decisions",
})
# Offered to continuation turns only; removed when ctx.spawn_blocked.
INTERNAL_ONLY_SPAWN_TOOLS: frozenset[str] = frozenset({"spawn_task", "dag_create"})
# Per-call rules (path and lineage), evaluated at dispatch.
INTERNAL_ONLY_CHECKED_TOOLS: frozenset[str] = frozenset({"write_file", "cancel_task"})
# Logged with their root when called from a lineage (spec section 9).
INTERNAL_ONLY_LOGGED_TOOLS: frozenset[str] = frozenset({"web_fetch", "web_search"})


def internal_only_allowed(name: str, *, ctx: ExecutionContext) -> bool:
    """Whether an internal_only context may be OFFERED ``name``.

    Fails closed on an unclassified tool: a tool nobody classified is not
    allowed, so a new tool is denied to a lineage until someone adds it on purpose.
    """
    cls = tool_class(name)
    if cls is None or cls.side_effect not in ("none", "write"):
        return False
    if name in INTERNAL_ONLY_DENYLIST:
        return False
    if name in INTERNAL_ONLY_SPAWN_TOOLS:
        return ctx.kind == "continuation" and not ctx.spawn_blocked
    return True


def _write_path_allowed(ctx: ExecutionContext, tool_input: Mapping[str, Any], workspace_dir: str) -> bool:
    """A write_file target is inside ``<workspace_dir>/intentions/<root_id>/`` once resolved.

    Resolves the way the tool does (``builtin_tools._validate_path``): relative to the
    workspace, symlinks and ``..`` followed. A lineage with no root id, or a call with
    no usable path, is refused.
    """
    path = tool_input.get("path")
    if ctx.root_intention_id is None or not isinstance(path, str) or not path.strip():
        return False
    try:
        workspace = Path(workspace_dir).resolve()
        root_dir = workspace / "intentions" / str(ctx.root_intention_id)
        target = Path(path).resolve() if Path(path).is_absolute() else (workspace / path).resolve()
    except (OSError, ValueError):  # a NUL byte, an unresolvable name
        return False
    return target.is_relative_to(root_dir)


def internal_only_call_violation(
    ctx: ExecutionContext, name: str, tool_input: Mapping[str, Any], *, workspace_dir: str
) -> str | None:
    """The per-call rules of an internal_only turn, for a tool that WAS offered.

    ``"external"``: ``classify_side_effect`` rates the call external (or
    irreversible), such as a URL in run_python. ``"write_path"``: write_file outside
    its root's directory. ``"foreign_cancel"``: cancel_task with an id that is not a
    UUID. The cancel_task lineage check needs the target's row, so it lives in the
    handler (``tools.py``); this sync function only rejects what needs no read.
    """
    if classify_side_effect(name, dict(tool_input)) in ("external", "irreversible"):
        return "external"
    if name == "write_file" and not _write_path_allowed(ctx, tool_input, workspace_dir):
        return "write_path"
    if name == "cancel_task":
        try:
            uuid.UUID(str(tool_input.get("task_id", "")))
        except (TypeError, ValueError):
            return "foreign_cancel"
    return None
```

`ruff format` may reflow the compact set literals; let it.

- [ ] **Step 4: Run the tests.**

Run: `"$BIN/nous-test-linux.sh" "$WT" runner pg "$DB" tests/test_f099_tool_policy.py tests/test_tool_policy.py tests/test_tool_classes.py -q`
Expected: PASS. If `test_a_lineage_turn_that_is_not_a_continuation_is_offered_exactly_the_pinned_set` fails on the added/removed lists, the tool table on `main` differs from the 16 listed here: fix the literal only after checking each name against `TOOL_CLASSES` and the spec table, and say so in the task report.

- [ ] **Step 5: Lint and commit**

```bash
cd "$WT"
cat > /tmp/f099-2a-2.txt <<'EOF'
feat(F099): the internal_only allowed set and per-call rules

tool_policy gains internal_only_allowed (class none or write, not on the
spec's denylist, spawn tools only for a continuation with room to spawn,
fail closed on an unclassified tool) and internal_only_call_violation (a call
rated external, a write_file outside intentions/<root>/, a cancel_task with
no UUID). Nothing calls them yet.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_015q4W1nke7JzGaZkC21Whng
EOF
git add nous/api/tool_policy.py tests/test_f099_tool_policy.py
git commit -F /tmp/f099-2a-2.txt
```

### Task 2a.3: One offered-set helper for both loops, with the `internal_only` narrowing

**Files:**
- Modify: `nous/api/runner.py`: import `AUTHORITY_INTERNAL`; module constant `_SUBTASK_EXCLUDED_TOOLS`; method `AgentRunner._offered_tools`; `_tool_loop` (the `base_tools` block and the per-iteration `tools` build); `stream_chat` (the `tools = …available_tools(…)` block)
- Create: `tests/test_f099_offered_tools.py`

**Interfaces:**
- Consumes: `tool_policy.internal_only_allowed` (Task 2a.2), `ExecutionContext.authority` / `.kind` / `.spawn_blocked`.
- Produces (contract §4.5): `AgentRunner._offered_tools(self, ctx, frame_id, *, is_subtask, tool_filter, refuse_active, extra_tools=None) -> list[dict[str, Any]]`. It returns the tool definitions a turn is offered, in this order: frame tools (D5) → subtask exclusion (012.2) → `tool_filter` (F034.5) → F078 refuse denylist → **`internal_only` narrowing** → `extra_tools` schemas. The narrowing applies to every context whose `authority == "internal_only"`, a `heartbeat_check`'s `tool_filter` included, so a lineage check's declared `bash` or `heartbeat_check_create` is never offered (this closes Phase 1 carry-over S10, with no change to the `heartbeat_check_create` handler). Extra tools are appended after the narrowing and are not filtered: they are the caller's per-turn tools.
- Produces: `_tool_loop` calls the helper once per turn (C4); `stream_chat` calls it with `is_subtask=False, tool_filter=None, extra_tools=None` and its `refuse_active`. Both derive `offered_names` from the result, as today.
- Behaviour with no `internal_only` context: **identical to today** (pinned). The offered set is identical; the F078 refuse log line is not: the helper logs it once, at INFO, and only when the tool list was not empty (the old `_tool_loop` logged WARNING, even for an empty list). No test asserts it, and a grep for `F078 refuse` still matches.

- [ ] **Step 1: Write the failing tests.** Create `tests/test_f099_offered_tools.py`:

```python
"""F099 section 4.4: one helper builds the offered set for both loops.

An internal_only turn is offered no external tool, no denylisted tool and, unless
it is a continuation with room to spawn, no spawn tool. An owner turn is offered
exactly what it was before F099.
"""

from __future__ import annotations

import itertools
import json
import uuid
from unittest.mock import MagicMock

import pytest
from test_f099_tool_policy import LINEAGE_ALLOWED
from test_runner_authorization import _run_loop, _runner

from nous.api.execution_context import CONTEXT_KINDS, ExecutionContext
from nous.api.models import ApiResponse
from nous.api.tool_classes import TOOL_CLASSES, refuse_denylist
from nous.api.tool_policy import INTERNAL_ONLY_SPAWN_TOOLS
from nous.heartbeat.dynamic import ALLOWED_TOOLS as CHECK_TOOLS


IID, RID = (uuid.UUID(f"00000000-0000-0000-0000-00000000000{n}") for n in (1, 2))
OWNER_KINDS = [k for k in CONTEXT_KINDS if k not in ("continuation", "approved_action")]
ALL_TOOLS = list(TOOL_CLASSES)
SUBMIT = {"name": "submit_final_report", "description": "d", "input_schema": {"type": "object"}}
MODES = ("off", "warn", "enforce")


async def _noop(**_):
    return "ok", False


def _internal(kind: str = "subtask", **over) -> ExecutionContext:
    base = {"kind": kind, "session_id": "s1", "authority": "internal_only", "intention_id": IID, "root_intention_id": RID}
    return ExecutionContext(**{**base, **over})


def _names(tools) -> set[str]:
    return {t["name"] for t in tools}


def _legacy_offered(dispatcher, frame_id, *, is_subtask, tool_filter, refuse_active, extra_tools):
    """The offered set as _tool_loop built it before F099, kept here as the reference."""
    tools = dispatcher.available_tools(frame_id)
    if is_subtask:
        tools = [t for t in tools if t["name"] not in {"spawn_task", "schedule_task", "spawn_sync"}]
    if tool_filter is not None:
        tools = [t for t in tools if t["name"] in tool_filter]
    if refuse_active:
        denylist = refuse_denylist()
        tools = [t for t in tools if t["name"] not in denylist]
    out = list(tools)
    if extra_tools:
        for _name, (schema, _executor) in extra_tools.items():
            out.append(schema)
    return out


def test_owner_contexts_are_offered_exactly_what_they_were_before_f099():  # PIN (also fails on the base: no helper)
    r, d = _runner(ALL_TOOLS)
    checked = 0
    for kind in OWNER_KINDS:
        ctx = ExecutionContext(kind=kind, session_id="s1")
        for is_subtask, tool_filter, refuse_active, extra in itertools.product(
            (False, True),
            (None, ["web_search", "bash", "recall_deep", "heartbeat_check_create"]),
            (False, True),
            (None, {"submit_final_report": (SUBMIT, _noop)}),
        ):
            kwargs = {
                "is_subtask": is_subtask,
                "tool_filter": tool_filter,
                "refuse_active": refuse_active,
                "extra_tools": extra,
            }
            got = r._offered_tools(ctx, "conversation", **kwargs)
            assert json.dumps(got) == json.dumps(_legacy_offered(d, "conversation", **kwargs)), (kind, kwargs)
            checked += 1
    assert checked == len(OWNER_KINDS) * 16


@pytest.mark.parametrize("kind", OWNER_KINDS)
@pytest.mark.parametrize("is_subtask", [False, True])
def test_a_lineage_turn_that_is_not_a_continuation_is_offered_no_spawn_external_or_denylisted_tool(kind, is_subtask):
    r, _ = _runner(ALL_TOOLS)
    tools = r._offered_tools(
        _internal(kind), "conversation", is_subtask=is_subtask, tool_filter=None, refuse_active=False
    )
    assert _names(tools) == LINEAGE_ALLOWED


def test_a_continuation_is_offered_the_spawn_tools_until_its_root_is_at_a_limit():
    r, _ = _runner(ALL_TOOLS)

    def offered(**over):
        return _names(
            r._offered_tools(
                _internal("continuation", **over), "conversation", is_subtask=False, tool_filter=None, refuse_active=False
            )
        )

    assert offered() == LINEAGE_ALLOWED | INTERNAL_ONLY_SPAWN_TOOLS
    assert offered(spawn_blocked=True) == LINEAGE_ALLOWED


def test_the_narrowing_composes_with_refuse_and_the_subtask_exclusion():
    r, _ = _runner(ALL_TOOLS)
    tools = r._offered_tools(
        _internal("continuation"), "conversation", is_subtask=False, tool_filter=None, refuse_active=True
    )
    assert _names(tools) <= LINEAGE_ALLOWED and not _names(tools) & refuse_denylist()
    # A continuation run as a subtask would lose spawn_task to the 012.2 rule: is_subtask must stay False for it.
    tools = r._offered_tools(
        _internal("continuation"), "conversation", is_subtask=True, tool_filter=None, refuse_active=False
    )
    assert "spawn_task" not in _names(tools)


def test_a_lineage_check_loses_the_tools_it_declared_that_the_narrowing_denies():
    """Phase 1 carry-over: a stamped check cannot be offered heartbeat_check_create (or bash)."""
    r, _ = _runner(ALL_TOOLS)
    declared = sorted(CHECK_TOOLS)
    ctx = _internal("heartbeat_check", declared_tools=tuple(declared))
    tools = r._offered_tools(ctx, "conversation", is_subtask=True, tool_filter=declared, refuse_active=False)
    assert _names(tools) == {"web_search", "web_fetch", "recall_deep", "recall_recent", "read_file"}
    assert not _names(tools) & {"bash", "heartbeat_check_create", "heartbeat_check_manage"}
    # PIN: the same check with no lineage is offered everything it declared.
    owner = ExecutionContext(kind="heartbeat_check", session_id="s1", declared_tools=tuple(declared))
    tools = r._offered_tools(owner, "conversation", is_subtask=True, tool_filter=declared, refuse_active=False)
    assert _names(tools) == set(CHECK_TOOLS)


def test_extra_tools_follow_the_narrowing_and_are_not_filtered():
    r, _ = _runner(ALL_TOOLS)
    schema = {"name": "resolve_intention", "description": "d", "input_schema": {"type": "object"}}
    tools = r._offered_tools(
        _internal("continuation"),
        "conversation",
        is_subtask=False,
        tool_filter=None,
        refuse_active=False,
        extra_tools={"resolve_intention": (schema, _noop)},
    )
    assert tools[-1] == schema
    assert _names(tools) == LINEAGE_ALLOWED | INTERNAL_ONLY_SPAWN_TOOLS | {"resolve_intention"}


def _capturing_api(seen: list[set[str]]):
    async def fake_call_api(
        system_prompt, messages, tools=None, skip_thinking=False, model_override=None, is_background=False, **_
    ):
        seen.append({t["name"] for t in (tools or [])})
        return ApiResponse(content=[{"type": "text", "text": "done"}], stop_reason="end_turn")

    return fake_call_api


@pytest.mark.parametrize("offered_mode", MODES)
@pytest.mark.parametrize("policy_mode", MODES)
@pytest.mark.parametrize(
    ("ctx", "is_subtask", "expected"),
    [
        (_internal("continuation"), False, LINEAGE_ALLOWED | INTERNAL_ONLY_SPAWN_TOOLS),
        (_internal("subtask"), True, LINEAGE_ALLOWED),
        (_internal("dag_node"), True, LINEAGE_ALLOWED),
    ],
    ids=["continuation", "subtask", "dag_node"],
)
async def test_the_model_is_sent_no_external_or_denylisted_tool_under_any_mode(
    offered_mode, policy_mode, ctx, is_subtask, expected
):
    r, _ = _runner(
        ALL_TOOLS, tool_offered_set_enforcement_mode=offered_mode, tool_context_policy_mode=policy_mode
    )
    seen: list[set[str]] = []
    r._call_api = _capturing_api(seen)
    await _run_loop(r, is_background=True, is_subtask=is_subtask, context=ctx)
    assert seen == [expected]


async def test_stream_chat_offers_an_internal_only_turn_the_narrowed_set(monkeypatch, tmp_path):
    """stream_chat shares the helper, so it shares the narrowing.

    In production stream_chat's context is always interactive and owner, so no
    internal_only context reaches it today. The test substitutes the context
    constructor in the runner module (a test seam) to run the real streaming
    loop with one.
    """
    import functools

    from test_streaming import _make_mock_cognitive, _make_mock_settings, _make_runner

    from nous.api import runner as runner_module
    from nous.api.anthropic_client import StreamEvent

    cognitive, turn_context = _make_mock_cognitive()
    turn_context.refuse_active = False
    settings = _make_mock_settings()
    settings.workspace_dir = str(tmp_path)
    runner = _make_runner(cognitive, settings)
    runner._dispatcher.available_tools.return_value = [
        {"name": n, "description": n, "input_schema": {"type": "object"}}
        for n in ("recall_deep", "send_email", "bash", "write_file", "web_fetch", "spawn_task")
    ]
    monkeypatch.setattr(runner_module, "ExecutionContext", functools.partial(ExecutionContext, authority="internal_only"))
    offered: list[set[str]] = []

    async def fake_stream(*args, **kwargs):
        tools = kwargs.get("tools") or next(
            (a for a in args if isinstance(a, list) and a and isinstance(a[0], dict) and "name" in a[0]), []
        )
        offered.append({t["name"] for t in tools})
        yield StreamEvent(type="text_delta", text="ok")
        yield StreamEvent(type="done", stop_reason="end_turn")

    runner._call_api_stream = MagicMock(side_effect=fake_stream)
    [e async for e in runner.stream_chat("s1", "hi")]
    assert offered == [{"recall_deep", "write_file", "web_fetch"}]
```

- [ ] **Step 2: Run them; confirm they fail on the base.**

Run: `"$BIN/nous-test-linux.sh" "$WT" runner pg "$DB" tests/test_f099_offered_tools.py -q`
Expected: the helper tests FAIL (`AttributeError: 'AgentRunner' object has no attribute '_offered_tools'`); the loop test FAILS on the base because the model is sent `send_email`, `bash` and `run_python`; the stream test FAILS for the same reason.

- [ ] **Step 3: Implement.** In `nous/api/runner.py`:

3a. Import (isort places it after `nous.brain.brain`):

```python
from nous.brain.brain import Brain
from nous.brain.intentions import AUTHORITY_INTERNAL
```

3b. After the `Refusal` class, a module constant (hoisted from `_tool_loop`, text unchanged):

```python
# 012.2: a subtask may not delegate (no-nesting rule). F062: spawn_sync has identical
# inline-blocking semantics to spawn_task(await_result=True) and competes for the same
# worker pool; without exclusion a hardened subtask could call it recursively and
# starve the pool or create a circular wait between subtask sessions.
_SUBTASK_EXCLUDED_TOOLS = frozenset({"spawn_task", "schedule_task", "spawn_sync"})
```

3c. A method on `AgentRunner`, immediately before `_authorize_tool_call`:

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
        """Exactly the tool definitions a turn is offered, in this order: frame tools (D5),
        the subtask exclusion (012.2), ``tool_filter`` (F034.5), the F078 refuse denylist,
        F099's ``internal_only`` narrowing, then the per-call ``extra_tools`` schemas (F061).

        Both loops call this, so there is one definition of the offered set. The
        narrowing keeps only tools for which ``tool_policy.internal_only_allowed``
        is true, for every kind whose ``ctx.authority`` is ``internal_only`` (a lineage
        check's ``tool_filter`` included: the intersection spec section 4.4 names).
        Extra tools are the caller's and are not filtered.
        """
        tools = self._dispatcher.available_tools(frame_id)
        if is_subtask:
            tools = [t for t in tools if t["name"] not in _SUBTASK_EXCLUDED_TOOLS]
        if tool_filter is not None:
            tools = [t for t in tools if t["name"] in tool_filter]
        if refuse_active and tools:
            # F078 (R6): a `refuse`-tier censor matched. The LLM still runs, but its
            # state-modifying tools are stripped so it can only decline gracefully
            # (or answer read-only). A DENYLIST removal from the tool-class table
            # (harness 2a), distinct from the whitelist tool_filter above.
            denylist = refuse_denylist()
            before = len(tools)
            tools = [t for t in tools if t["name"] not in denylist]
            logger.info("F078 refuse: stripped %d state-modifying tool(s)", before - len(tools))
        if ctx.authority == AUTHORITY_INTERNAL:
            tools = [t for t in tools if tool_policy.internal_only_allowed(t["name"], ctx=ctx)]
        if extra_tools:
            tools = [*tools, *(schema for schema, _executor in extra_tools.values())]
        return tools
```

3d. `_tool_loop`: delete the lines that build and filter `base_tools`: from the comment `# Get base tools for current frame (D5)` and `base_tools = self._dispatcher.available_tools(frame_id)`, through the `if is_subtask:` block (with its local `_SUBTASK_EXCLUDED_TOOLS`, now hoisted), the `if tool_filter is not None:` block, and the whole `if refuse_active:` block, ending with the closing `)` of its `logger.warning("F078 refuse: stripped %d state-modifying tools for the turn", …)` call. The blank line and `# Build initial messages from conversation history` that follow stay. Put this in their place:

```python
        # F099: one helper builds the offered set for both loops (frame tools, the 012.2
        # subtask exclusion, F034.5 tool_filter, the F078 refuse denylist, the internal_only
        # narrowing, then the F061 per-call extra tools). ctx is frozen, so once per turn is exact.
        offered_tools = self._offered_tools(
            ctx,
            frame_id,
            is_subtask=is_subtask,
            tool_filter=tool_filter,
            refuse_active=refuse_active,
            extra_tools=extra_tools,
        )
```

and in the loop replace

```python
            # F020: Rebuild tool list each iteration for dynamic cache_retrieve
            tools = list(base_tools)
            # F061: append per-call extra tool schemas (NOT registered globally).
            if extra_tools:
                for _name, (_schema, _exec) in extra_tools.items():
                    tools.append(_schema)
```

with

```python
            # F020: copy the tool list each iteration (the F061 per-call extra tool
            # schemas, NOT registered globally, are already in it).
            tools = list(offered_tools)
```

(`offered_names = frozenset(t["name"] for t in tools)` below it is unchanged.)

3e. `stream_chat`: delete the line `tools = self._dispatcher.available_tools(turn_context.frame.frame_id)`, the five-line `# F078 (codex P1): …` comment that follows it, and the `if getattr(turn_context, "refuse_active", False) and tools:` block (five lines, ending with the `logger.info("F078 refuse: stripped %d state-modifying tool(s) (streaming)", …)` call). Put this in their place:

```python
            # F078 (codex P1): a refuse-tier censor must strip state-modifying tools on the
            # STREAMING path too (_offered_tools does it for both loops). refuse_active
            # already accounts for refuse_keep_tools (set in cognitive/layer.py).
            tools = self._offered_tools(
                _ctx,
                turn_context.frame.frame_id,
                is_subtask=False,
                tool_filter=None,
                refuse_active=getattr(turn_context, "refuse_active", False),
            )
```

keeping the following `# Harness Phase 1a: exactly what the model is offered this turn.` and `offered_names = …` lines.

- [ ] **Step 4: Run the tests and the runner neighbours.**

Run: `"$BIN/nous-test-linux.sh" "$WT" runner pg "$DB" tests/test_f099_offered_tools.py tests/test_runner_authorization.py tests/test_runner_ledger.py tests/test_runner.py tests/test_runner_background.py tests/test_streaming.py tests/test_f061_runner_subtask_hooks.py tests/test_compensation.py -q`
Expected: PASS. The F078 refuse tests (`test_stream_chat_refuse_strips_the_denylist` and the `_tool_loop` ones) are the regression net for the owner path.

- [ ] **Step 5: Lint and commit**

```bash
cd "$WT"
cat > /tmp/f099-2a-3.txt <<'EOF'
feat(F099): one offered-set helper for both loops, narrowing internal_only

AgentRunner._offered_tools builds the tool list for _tool_loop and
stream_chat: frame tools, the subtask exclusion, tool_filter, the F078 refuse
denylist, then, for an internal_only turn, only the tools the spec allows,
then per-call extra tools. An owner turn is offered exactly what it was
(pinned against a reference of the old code). A stamped heartbeat check can
no longer be offered heartbeat_check_create or bash.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_015q4W1nke7JzGaZkC21Whng
EOF
git add nous/api/runner.py tests/test_f099_offered_tools.py
git commit -F /tmp/f099-2a-3.txt
```

### Task 2a.4: The strict path in `_authorize_tool_call`

**Files:**
- Modify: `nous/api/runner.py`: `_authorize_tool_call` (a new first block); `_ledger_blocked` docstring
- Modify: `nous/cognitive/ledger_store.py`: `REFUSAL_CODES`
- Create: `tests/test_f099_enforcement.py`

**Interfaces:**
- Consumes: `tool_policy.internal_only_call_violation` (Task 2a.2), `AUTHORITY_INTERNAL` (imported in 2a.3).
- Produces: a block that runs FIRST in `_authorize_tool_call`, before the offered-set mode check and before the `policy_mode == "off"` return, for `ctx.authority == "internal_only"` or `ctx.kind == "approved_action"`. It refuses a tool that is not in `offered_names` (`not_offered`), and, for an `internal_only` turn, a call `internal_only_call_violation` rejects (`external`, `write_path`, `foreign_cancel`). The refusal is `Refusal("Tool error: '<tool>' is not allowed in this turn (<violation>).", "internal_only")`. The signature does not change.
- Produces: `"internal_only"` in `ledger_store.REFUSAL_CODES` (C1). Without it `_ledger_blocked` raises `ValueError` out of the loop.
- Note on the `stream_chat` tests below: the test seam keeps `kind="interactive"`, so `tool_policy.evaluate` is a foreground no-op there. They prove the shared offered set and the strict block on the streaming loop, and nothing about the context-policy rule on that loop. The PR description says so.
- Produces: the refusal is logged at WARNING and recorded as `harness_context_policy_violation` with `mode: "enforce"` and `violation: "<internal_only|approved_action>:<code>"` (C2).
- After the strict block the existing offered-set and policy rules still run, in whatever mode is configured: the strict block is the floor, not a replacement.
- An `approved_action` call is strict on the offered set only: 2d passes `offered_names = frozenset({tool})`.
- **Flag-off parity:** an `owner` context never enters the block (pinned).

- [ ] **Step 1: Write the failing tests.** Create `tests/test_f099_enforcement.py`:

```python
"""F099 section 4.4: dispatch enforcement for internal_only turns.

The strict block runs before the offered-set mode check and before the policy's
off switch, so a forged tool_use is refused whatever the modes say. Every test
that matters runs under off/off, warn/warn and enforce/enforce.
"""

from __future__ import annotations

import functools
import json
import uuid
from unittest.mock import MagicMock

import pytest
from test_runner_authorization import _run_loop, _runner, _tool_calls_then_done_with
from test_runner_ledger import _FakeStore
from test_runner_ledger import _runner as _ledger_runner

from nous.api.execution_context import ExecutionContext
from nous.cognitive.ledger_store import REFUSAL_CODES

# Fixed ids: parametrize ids built from them must not change between collections (xdist).
IID, RID, OTHER = (uuid.UUID(f"00000000-0000-0000-0000-00000000000{n}") for n in (1, 2, 3))
MODES = [("off", "off"), ("warn", "warn"), ("enforce", "enforce")]
OFFERED = ["recall_deep", "send_email", "run_python", "bash", "write_file", "cancel_task"]
FORGED = [
    ("send_email", {"to": "a@example.com", "subject": "s", "body": "b"}),
    ("run_python", {"code": "print(1)"}),
    ("bash", {"command": "id"}),
]


def _internal(kind: str = "subtask", **over) -> ExecutionContext:
    base = {"kind": kind, "session_id": "s1", "authority": "internal_only", "intention_id": IID, "root_intention_id": RID}
    return ExecutionContext(**{**base, **over})


def _auth(r, ctx, tool, offered, tool_input=None):
    return r._authorize_tool_call(ctx, tool, frozenset(offered), "s1", tool_input or {})


def test_the_ledger_knows_the_refusal_code():
    assert "internal_only" in REFUSAL_CODES


@pytest.mark.parametrize(("offered_mode", "policy_mode"), MODES)
@pytest.mark.parametrize(("tool", "tool_input"), FORGED, ids=[t for t, _ in FORGED])
async def test_a_forged_call_in_an_internal_only_turn_is_refused_whatever_the_modes_say(
    offered_mode, policy_mode, tool, tool_input
):
    r, d = _runner(OFFERED, tool_offered_set_enforcement_mode=offered_mode, tool_context_policy_mode=policy_mode)
    r._call_api = _tool_calls_then_done_with(tool, tool_input, times=1)
    _text, results, _usage, _thinking = await _run_loop(r, is_background=True, context=_internal())
    assert d.calls == []
    assert [x.tool_name for x in results] == [tool]
    assert "is not allowed in this turn (not_offered)" in (results[0].error or "")


class _ValidatingStore(_FakeStore):
    """Rejects a refusal code the way the real LedgerStore.record_blocked does."""

    async def record_blocked(self, *, context, tool_name, tool_input, turn, refused_by, idempotency_key=None):
        if refused_by not in REFUSAL_CODES:
            raise ValueError(f"unknown refusal code {refused_by!r}")
        await super().record_blocked(
            context=context,
            tool_name=tool_name,
            tool_input=tool_input,
            turn=turn,
            refused_by=refused_by,
            idempotency_key=idempotency_key,
        )


async def test_the_refusal_is_written_to_the_ledger_and_does_not_crash_the_turn():
    store = _ValidatingStore()
    r, d = _ledger_runner(
        store, offered=OFFERED, tool_offered_set_enforcement_mode="off", tool_context_policy_mode="off"
    )
    r._call_api = _tool_calls_then_done_with("send_email", FORGED[0][1], times=1)
    text, _results, _usage, _thinking = await _run_loop(r, is_background=True, context=_internal())
    assert text == "done" and d.calls == []
    assert store.events == [("blocked", "send_email", "internal_only")]


def test_the_refusal_is_recorded_as_an_enforced_policy_violation():
    r, _ = _runner(OFFERED, tool_offered_set_enforcement_mode="off", tool_context_policy_mode="off")
    events: list[tuple[str, dict]] = []
    r._log_f026_decision = lambda event_type, data, session_id=None: events.append((event_type, data))
    refusal = _auth(r, _internal(), "send_email", ["recall_deep"])
    assert refusal is not None and refusal.code == "internal_only"
    assert events == [
        (
            "harness_context_policy_violation",
            {
                "tool_name": "send_email",
                "context_kind": "subtask",
                "violation": "internal_only:not_offered",
                "mode": "enforce",
            },
        )
    ]


@pytest.mark.parametrize(("offered_mode", "policy_mode"), MODES)
@pytest.mark.parametrize(
    ("tool", "tool_input"),
    [("run_python", {"code": "import smtplib"}), ("bash", {"command": "curl https://example.com"})],
)
def test_a_call_rated_external_is_refused_even_when_the_tool_was_offered(offered_mode, policy_mode, tool, tool_input):
    """Unreachable through the real offered set (both tools are denylisted); the offered set is built by hand."""
    r, _ = _runner(OFFERED, tool_offered_set_enforcement_mode=offered_mode, tool_context_policy_mode=policy_mode)
    refusal = _auth(r, _internal(), tool, OFFERED, tool_input)
    assert refusal is not None and refusal.code == "internal_only" and "(external)" in refusal.text


@pytest.mark.parametrize(("offered_mode", "policy_mode"), MODES)
@pytest.mark.parametrize(
    "path",
    ["notes.md", f"intentions/{OTHER}/notes.md", f"intentions/{RID}/../../x.md", "../x.md", "/etc/passwd"],
)
def test_write_file_outside_the_root_dir_is_refused_in_every_mode(tmp_path, offered_mode, policy_mode, path):
    r, _ = _runner(
        OFFERED,
        workspace_dir=str(tmp_path),
        tool_offered_set_enforcement_mode=offered_mode,
        tool_context_policy_mode=policy_mode,
    )
    refusal = _auth(r, _internal(), "write_file", OFFERED, {"path": path, "content": "x"})
    assert refusal is not None and refusal.code == "internal_only" and "(write_path)" in refusal.text


@pytest.mark.parametrize(("offered_mode", "policy_mode"), MODES)
def test_write_file_inside_the_root_dir_is_authorized(tmp_path, offered_mode, policy_mode):
    r, _ = _runner(
        OFFERED,
        workspace_dir=str(tmp_path),
        tool_offered_set_enforcement_mode=offered_mode,
        tool_context_policy_mode=policy_mode,
    )
    tool_input = {"path": f"intentions/{RID}/notes.md", "content": "x"}
    assert _auth(r, _internal(), "write_file", OFFERED, tool_input) is None


async def test_write_file_in_the_root_dir_is_dispatched_and_outside_it_is_not(tmp_path):
    for tool_input, dispatched in (
        ({"path": f"intentions/{RID}/n.md", "content": "x"}, True),
        ({"path": "n.md", "content": "x"}, False),
    ):
        r, d = _runner(
            OFFERED,
            workspace_dir=str(tmp_path),
            tool_offered_set_enforcement_mode="off",
            tool_context_policy_mode="off",
        )
        r._call_api = _tool_calls_then_done_with("write_file", tool_input, times=1)
        await _run_loop(r, is_background=True, context=_internal())
        assert bool(d.calls) is dispatched, tool_input


def test_a_lineage_with_no_root_may_not_write_a_file(tmp_path):
    r, _ = _runner(OFFERED, workspace_dir=str(tmp_path), tool_offered_set_enforcement_mode="off")
    damaged = ExecutionContext(kind="subtask", session_id="s1", authority="internal_only")
    refusal = _auth(r, damaged, "write_file", OFFERED, {"path": f"intentions/{RID}/n.md", "content": "x"})
    assert refusal is not None and "(write_path)" in refusal.text


@pytest.mark.parametrize(("offered_mode", "policy_mode"), MODES)
def test_an_approved_action_runs_its_one_tool_and_nothing_else(offered_mode, policy_mode):
    r, _ = _runner(OFFERED, tool_offered_set_enforcement_mode=offered_mode, tool_context_policy_mode=policy_mode)
    ctx = ExecutionContext(
        kind="approved_action", session_id="proposal-x", proposal_id=uuid.uuid4(), declared_tools=("send_email",)
    )
    assert _auth(r, ctx, "send_email", ["send_email"], FORGED[0][1]) is None
    refusal = _auth(r, ctx, "bash", ["send_email"], {"command": "ls"})
    assert refusal is not None and refusal.code == "internal_only" and "(not_offered)" in refusal.text


@pytest.mark.parametrize(("offered_mode", "policy_mode"), MODES)
def test_an_owner_turn_is_authorized_exactly_as_before(offered_mode, policy_mode):  # PIN
    r, _ = _runner(OFFERED, tool_offered_set_enforcement_mode=offered_mode, tool_context_policy_mode=policy_mode)
    ctx = ExecutionContext(kind="subtask", session_id="s1")
    refusal = _auth(r, ctx, "send_email", ["recall_deep"], FORGED[0][1])
    if offered_mode == "enforce":
        assert refusal is not None and refusal.code == "offered_set"
    else:
        assert refusal is None
    assert _auth(r, ctx, "write_file", OFFERED, {"path": "anywhere.md", "content": "x"}) is None


@pytest.mark.parametrize(("offered_mode", "policy_mode"), MODES)
@pytest.mark.parametrize(("tool", "tool_input"), FORGED, ids=[t for t, _ in FORGED])
async def test_stream_chat_refuses_a_forged_call_in_an_internal_only_turn_whatever_the_modes_say(
    monkeypatch, tmp_path, offered_mode, policy_mode, tool, tool_input
):
    """The streaming loop's offered_names come from the shared helper and its call site
    runs the strict block. In production stream_chat's context is interactive/owner, so the
    test substitutes the context constructor in the runner module (a test seam)."""
    from test_streaming import _make_mock_cognitive, _make_mock_settings, _make_runner

    from nous.api import runner as runner_module
    from nous.api.anthropic_client import StreamEvent

    cognitive, turn_context = _make_mock_cognitive()
    turn_context.refuse_active = False
    settings = _make_mock_settings()
    settings.tool_offered_set_enforcement_mode = offered_mode
    settings.tool_context_policy_mode = policy_mode
    settings.workspace_dir = str(tmp_path)
    runner = _make_runner(cognitive, settings)
    runner._dispatcher.available_tools.return_value = [
        {"name": n, "description": n, "input_schema": {"type": "object"}} for n in OFFERED
    ]
    monkeypatch.setattr(runner_module, "ExecutionContext", functools.partial(ExecutionContext, authority="internal_only"))
    calls = {"n": 0}

    async def fake_stream(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            yield StreamEvent(type="tool_start", tool_name=tool, tool_id="t1", block_index=1)
            yield StreamEvent(type="tool_input_delta", text=json.dumps(tool_input), block_index=1)
            yield StreamEvent(type="block_stop", block_index=1)
            yield StreamEvent(type="done", stop_reason="tool_use")
        else:
            yield StreamEvent(type="text_delta", text="ok")
            yield StreamEvent(type="done", stop_reason="end_turn")

    runner._call_api_stream = MagicMock(side_effect=fake_stream)
    events = [e async for e in runner.stream_chat("s1", "run it")]

    assert not runner._dispatcher.dispatch.called
    assert any(e.type == "tool_end" and e.tool_name == tool for e in events)
    second_call_messages = runner._call_api_stream.call_args_list[1][0][1]
    results = [
        b
        for m in second_call_messages
        if isinstance(m.get("content"), list)
        for b in m["content"]
        if isinstance(b, dict) and b.get("type") == "tool_result"
    ]
    assert len(results) == 1 and results[0]["is_error"] is True
    assert "is not allowed in this turn (not_offered)" in results[0]["content"]
```

(The `functools.partial` line may need wrapping to 120 columns; let `ruff format` do it.)

- [ ] **Step 2: Run them; confirm they fail on the base.**

Run: `"$BIN/nous-test-linux.sh" "$WT" runner pg "$DB" tests/test_f099_enforcement.py -q`
Expected: FAIL. Before this task's edits the forged calls under `off/off` and `warn/warn` reach `dispatch`; `REFUSAL_CODES` lacks `"internal_only"`; `write_file` outside the root dir is dispatched.

- [ ] **Step 3: Implement.**

3a. `nous/cognitive/ledger_store.py`:

```python
REFUSAL_CODES = frozenset({"offered_set", "action_gate", "context_policy", "duplicate", "internal_only"})
```

and in `nous/api/runner.py` `_ledger_blocked`'s docstring add `internal_only` to the list of codes (`offered_set` / `action_gate` / `context_policy` / `duplicate` / `internal_only`).

3b. `nous/api/runner.py`, `_authorize_tool_call`: update the docstring's first paragraph to say "the strict F099 rule first, then the offered-set rule (Phase 1a), then the per-context policy (2a)", and insert, as the first statements of the body (before `offered_mode = …`):

```python
        # F099 section 4.4: an internal_only turn, and an approved_action call, are
        # enforced here FIRST, whatever the two mode settings say. The modes are for
        # tuning the ordinary rules; this is a security floor. A call to a tool that
        # was not offered is refused, and so is a call the per-call rules reject.
        # With intentions off, Phase 1 writes no internal_only row: only a damaged
        # lineage stamp reaches this, and it loses tools, never gains one.
        if ctx.authority == AUTHORITY_INTERNAL or ctx.kind == "approved_action":
            strict: str | None = None
            if tool_name not in offered_names:
                strict = "not_offered"
            elif ctx.authority == AUTHORITY_INTERNAL:
                strict = tool_policy.internal_only_call_violation(
                    ctx, tool_name, tool_input, workspace_dir=self._settings.workspace_dir
                )
            if strict is not None:
                scope = "internal_only" if ctx.authority == AUTHORITY_INTERNAL else ctx.kind
                logger.warning(
                    "F099: refused %r in a %s turn (%s:%s, session=%s)", tool_name, ctx.kind, scope, strict, session_id
                )
                # mode "enforce": it was refused, which is what the dashboard counts under it.
                self._log_f026_decision(
                    "harness_context_policy_violation",
                    {
                        "tool_name": tool_name,
                        "context_kind": ctx.kind,
                        "violation": f"{scope}:{strict}",
                        "mode": "enforce",
                    },
                    session_id=session_id,
                )
                return Refusal(f"Tool error: '{tool_name}' is not allowed in this turn ({strict}).", "internal_only")

```

- [ ] **Step 4: Run the tests and the neighbours.**

Run: `"$BIN/nous-test-linux.sh" "$WT" runner pg "$DB" tests/test_f099_enforcement.py tests/test_f099_offered_tools.py tests/test_runner_authorization.py tests/test_runner_ledger.py tests/test_ledger_store.py tests/test_dashboard_harness.py tests/test_dashboard_harness_rest.py tests/test_compensation.py -q`
Expected: PASS.

**Mutation check (do this once, then revert):** move the new block below the `if policy_mode == "off": return None` line (or wrap it in `if policy_mode != "off":`). The `off/off` cases of `test_a_forged_call_…` and `test_stream_chat_refuses_…` must FAIL. Then drop `"internal_only"` from `REFUSAL_CODES`: `test_the_refusal_is_written_to_the_ledger_and_does_not_crash_the_turn` must FAIL with the `ValueError`. Say in the task report that both were seen to fail.

- [ ] **Step 5: Lint and commit**

```bash
cd "$WT"
cat > /tmp/f099-2a-4.txt <<'EOF'
feat(F099): refuse unoffered and external calls in an internal_only turn

_authorize_tool_call gets a strict block that runs before the offered-set
mode check and before the policy's off switch: an internal_only turn (and an
approved_action call) is refused any tool that was not offered, any call
classified external, a write_file outside intentions/<root>/ and a
cancel_task with no UUID, whatever the two modes say. The refusal code
internal_only joins the ledger's REFUSAL_CODES (record_blocked raises on an
unknown code). Owner turns never enter the block.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_015q4W1nke7JzGaZkC21Whng
EOF
git add nous/api/runner.py nous/cognitive/ledger_store.py tests/test_f099_enforcement.py
git commit -F /tmp/f099-2a-4.txt
```

### Task 2a.5: Only a terminal extra tool ends the loop

**Files:**
- Modify: `nous/api/runner.py`: module constant `TERMINAL_EXTRA_TOOLS`; the extra-tool dispatch branch in `_tool_loop`
- Create: `tests/test_f099_terminal_tools.py`

**Interfaces:**
- Produces (contract §4.6): `nous.api.runner.TERMINAL_EXTRA_TOOLS: frozenset[str] = frozenset({"submit_final_report", "resolve_intention"})`. A successful call to an `extra_tools` tool ends the loop only when its name is in this set; any other extra tool (2d's `propose_action`) returns its result to the model like a registered tool. A failing terminal tool still does not end the loop (unchanged).
- The `extra_tools` dict shape `{name: (schema, executor)}` is unchanged, so the one production caller (`nous/handlers/subtask_executor.py`, `submit_final_report`) needs no edit. `resolve_intention` and `propose_action` themselves arrive in 2c and 2d; this task lands the mechanism.

- [ ] **Step 1: Write the failing tests.** Create `tests/test_f099_terminal_tools.py`:

```python
"""F099 section 4.4: only a tool flagged terminal ends the loop.

Before F099 any successful extra tool ended it (submit_final_report was the only
one). propose_action must not: the model resolves after it.
"""

from __future__ import annotations

import pytest
from test_runner_authorization import _run_loop, _runner, _tool_calls_then_done_with

from nous.api.runner import TERMINAL_EXTRA_TOOLS


def _extra(name: str, calls: list[dict], *, error: bool = False):
    async def executor(**kwargs):
        calls.append(kwargs)
        return ("it failed" if error else "Recorded."), error

    schema = {"name": name, "description": name, "input_schema": {"type": "object"}}
    return {name: (schema, executor)}


def _loop_runner():
    # Both modes off: the extra tools here are not in the tool-class table.
    return _runner(["recall_deep"], tool_offered_set_enforcement_mode="off", tool_context_policy_mode="off")


def test_the_terminal_set_is_exactly_the_two_resolving_tools():  # PIN
    assert TERMINAL_EXTRA_TOOLS == frozenset({"submit_final_report", "resolve_intention"})


async def test_a_successful_non_terminal_extra_tool_returns_to_the_model():
    calls: list[dict] = []
    r, _ = _loop_runner()
    r._call_api = _tool_calls_then_done_with("propose_action", {"tool": "send_email"}, times=1)
    text, results, _usage, _thinking = await _run_loop(r, is_background=True, extra_tools=_extra("propose_action", calls))
    assert calls == [{"tool": "send_email"}]
    assert text == "done"  # the model was called again and answered; the loop did not short-circuit
    assert [x.tool_name for x in results] == ["propose_action"]


@pytest.mark.parametrize("name", sorted(TERMINAL_EXTRA_TOOLS))
async def test_a_successful_terminal_extra_tool_ends_the_loop(name):  # PIN
    calls: list[dict] = []
    r, _ = _loop_runner()
    r._call_api = _tool_calls_then_done_with(name, {"decision": "report"}, times=3)
    text, _results, usage, _thinking = await _run_loop(r, is_background=True, extra_tools=_extra(name, calls))
    assert text == "Report submitted."
    assert len(calls) == 1 and usage["tool_calls"] == 1


async def test_a_failing_terminal_extra_tool_does_not_end_the_loop():  # PIN
    calls: list[dict] = []
    r, _ = _loop_runner()
    r._call_api = _tool_calls_then_done_with("resolve_intention", {}, times=1)
    text, _results, _usage, _thinking = await _run_loop(
        r, is_background=True, extra_tools=_extra("resolve_intention", calls, error=True)
    )
    assert len(calls) == 1 and text == "done"
```

- [ ] **Step 2: Run them; confirm the first behaviour test fails on the base.**

Run: `"$BIN/nous-test-linux.sh" "$WT" runner pg "$DB" tests/test_f099_terminal_tools.py -q`
Expected: collection ERROR (`cannot import name 'TERMINAL_EXTRA_TOOLS'`); after Step 3's constant alone it would still FAIL `test_a_successful_non_terminal_extra_tool_returns_to_the_model` (`text == "Report submitted."`), which is the behaviour this task changes.

- [ ] **Step 3: Implement.** In `nous/api/runner.py`:

3a. Module constant, next to `_SUBTASK_EXCLUDED_TOOLS`:

```python
# F099: only these extra_tools end the loop on success. submit_final_report (F061) and
# resolve_intention (a continuation's decision) mean "I am done"; every other extra tool
# (propose_action) returns its result to the model like a registered tool.
TERMINAL_EXTRA_TOOLS: frozenset[str] = frozenset({"submit_final_report", "resolve_intention"})
```

3b. In `_tool_loop`'s extra-tool branch, replace

```python
                            # Short-circuit: any successful extra_tools call
                            # terminates the loop. F061's submit_final_report
                            # is currently the only extra_tool and its
                            # semantics are "I am done" — even when the
                            # model voluntarily called it (force_tool was
                            # None on this turn), we still want to exit
                            # rather than make wasted follow-up API calls.
                            if not is_error:
                                terminate_after_tool_results = True
```

with

```python
                            # Short-circuit: a successful TERMINAL extra tool ends the
                            # loop. F061's submit_final_report means "I am done" — even
                            # when the model voluntarily called it (force_tool was None
                            # on this turn), we still want to exit rather than make
                            # wasted follow-up API calls. F099: any other extra tool
                            # returns to the model.
                            if not is_error and tool_name in TERMINAL_EXTRA_TOOLS:
                                terminate_after_tool_results = True
```

and update the comment on `terminate_after_tool_results = False  # set when submit_final_report fires` to `# set when a terminal extra tool fires`.

3c. Check the one caller: `grep -rn "extra_tools=" nous/` must show only `nous/api/runner.py` (the `run_turn` to `_tool_loop` pass-through) and `nous/handlers/subtask_executor.py`, whose dict holds `submit_final_report` alone.

- [ ] **Step 4: Run the tests and the F061 neighbours.**

Run: `"$BIN/nous-test-linux.sh" "$WT" runner pg "$DB" tests/test_f099_terminal_tools.py tests/test_f061_runner_subtask_hooks.py tests/test_f061_subtask_executor.py tests/test_f062_executor_schema_validation.py tests/test_runner_authorization.py tests/test_runner_ledger.py -q`
Expected: PASS.

- [ ] **Step 5: Lint and commit**

```bash
cd "$WT"
cat > /tmp/f099-2a-5.txt <<'EOF'
feat(F099): only a terminal extra tool ends the tool loop

TERMINAL_EXTRA_TOOLS (submit_final_report, resolve_intention) replaces "any
successful extra tool ends the loop". A non-terminal extra tool, such as the
propose_action 2d adds, returns its result to the model. The one existing
caller passes submit_final_report, so hardened subtasks stop as before.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_015q4W1nke7JzGaZkC21Whng
EOF
git add nous/api/runner.py tests/test_f099_terminal_tools.py
git commit -F /tmp/f099-2a-5.txt
```

### Task 2a.6: A child is never wider than its turn; no approval node from a lineage; the D7 downgrade is visible

**Files:**
- Modify: `nous/brain/intentions.py`: `IntentionSpec.origin_authority`; `spec_from_tool_call`; `resolve_wake_policy`; `prepare_intention`; new `wake_policy_for_source` and `IntentionStore.wake_policy_for_source`
- Modify: `nous/dag/store.py`: `DAGStore.intention_wake_policy`
- Modify: `nous/api/tools.py`: `_origin_args`; `spawn_task`, `spawn_sync`, `schedule_task` (accept `_origin_authority`); `dag_create` (approval refusal, `_origin_authority`, the note); new module function `_recorded_policy_note`
- Modify: `tests/test_f099_lineage.py`: two expected dicts (C6)
- Create: `tests/test_f099_authority.py`

**Interfaces:**
- Produces (Phase 1 carry-over, final-review Minor 3): `_origin_args` always sends `_origin_authority = ctx.authority`. `IntentionSpec.origin_authority: str | None = None`; `spec_from_tool_call(..., origin_authority: str | None = None)` stores it (an unknown value fails closed to `internal_only`). `prepare_intention` computes `authority = internal_only` when the lineage parent ROW is `internal_only` **or** `spec.origin_authority == "internal_only"`. A continuation (or any `internal_only` turn) spawning under an `owner` root row therefore creates an `internal_only` child. Code paths (scheduler, work queue, `app.act`, REST) leave `origin_authority` `None`: owner.
- Produces (C9): `resolve_wake_policy` treats `spec.origin_authority == "internal_only"` like an internal parent: every result goes back to the continuation (an inline one returns in-turn). So spec §7 holds: a lineage `dag_create` with `wake_policy="none"` still gets `continue`.
- Produces (Minor 8, C7): when the model passes `wake_policy` and the recorded policy differs, the fire-and-forget `spawn_task` and the `dag_create` result text end with `" (wake_policy '<requested>' is not available from a <origin_kind> turn; recorded '<policy>')"`. One read (`intentions.wake_policy_for_source`), only on the flag-on path with an explicit argument; a failed read adds no note and breaks nothing.
- Produces: `dag_create` returns `Error: an internal-only turn cannot create approval nodes; ask the owner through resolve_intention(decision='ask') instead.` for an `approval` node when `_origin_authority == "internal_only"`, before the approval-flag checks.
- The four spawn handlers accept `_origin_authority` as a keyword (`schedule_task` ignores it for the wake policy, D3, but its signature must accept every hidden argument `_origin_args` sends).

- [ ] **Step 1: Write the failing tests.** Create `tests/test_f099_authority.py`:

```python
"""F099 section 4.1 Authority: a child is never wider than the turn that spawned it.

Narrowing the offered set (Tasks 2a.3 and 2a.4) is not enough if a continuation
could spawn an owner-authority child: the child's own turns would be wide. These
tests drive the real dispatcher, the spawn handlers and the stores.
"""

from __future__ import annotations

import dataclasses
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from test_f099_lineage import _recording_dispatcher

from nous.api.execution_context import ExecutionContext
from nous.api.tools import ToolDispatcher, register_dag_tools, register_subtask_tools
from nous.brain import intentions
from nous.config import Settings
from nous.dag.store import DAGStore
from nous.storage.models import Intention, Subtask

ON = {"result_inbox_enabled": True, "intentions_enabled": True}
NODES = [{"name": "n", "type": "subtask", "instructions": "x"}]
IID, RID = (uuid.UUID(f"00000000-0000-0000-0000-00000000000{n}") for n in (1, 2))


class _Turn:
    async def run_turn(self, **_):
        return "done", None, {"input_tokens": 1, "output_tokens": 1}

    async def end_conversation(self, *a, **k):
        return True


class _Orchestrator:
    clock_wired = True
    approvals_wired = False

    def __init__(self, settings):
        self._settings = settings
        self.start_dag = AsyncMock()


@pytest.fixture
async def authority_env(db, mock_embeddings):
    from nous.heart import Heart

    hearts = []

    async def build(**over):
        agent = f"f099-auth-{uuid.uuid4().hex[:8]}"
        settings = Settings(
            _env_file=None,
            agent_id=agent,
            subtask_max_attempts=1,
            telegram_bot_token="",
            telegram_chat_id="",
            **{**ON, **over},
        )
        heart = Heart(db, settings, embedding_provider=mock_embeddings)
        hearts.append(heart)
        d = ToolDispatcher()
        register_subtask_tools(d, heart, settings, runner=_Turn())
        register_dag_tools(d, DAGStore(db, agent, settings), _Orchestrator(settings), settings=settings)
        return SimpleNamespace(agent=agent, settings=settings, heart=heart, d=d, db=db)

    yield build
    for heart in hearts:
        await heart.close()


async def _dispatch(env, name, args, ctx):
    return await env.d.dispatch(name, args, session_id=ctx.session_id, context=ctx)


async def _all(env, model):
    async with env.db.session() as s:
        return list((await s.execute(select(model).where(model.agent_id == env.agent))).scalars().all())


async def _root(env, *, wake_policy="continue", authority="owner") -> Intention:
    rid = uuid.uuid4()
    row = Intention(
        id=rid,
        agent_id=env.agent,
        root_id=rid,
        depth=0,
        source_kind="subtask",
        source_id=str(uuid.uuid4()),
        intent="the turn's own intention",
        origin_kind="interactive",
        wake_policy=wake_policy,
        authority=authority,
        state="pending",
    )
    async with env.db.session() as s:
        s.add(row)
        await s.commit()
    return row


def _cont(root: Intention, **over) -> ExecutionContext:
    base = {
        "kind": "continuation",
        "session_id": f"intent-{root.root_id}",
        "authority": "internal_only",
        "intention_id": root.id,
        "root_intention_id": root.root_id,
    }
    return ExecutionContext(**{**base, **over})


def _children(rows, root) -> list[Intention]:
    return [i for i in rows if i.id != root.id]


# -- min(context authority, parent row authority) ---------------------------


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("spawn_task", {"task": "t", "intent": "why"}),
        ("dag_create", {"name": "d", "nodes": NODES, "intent": "why"}),
    ],
    ids=["spawn_task", "dag_create"],
)
async def test_a_continuation_spawn_under_an_owner_root_is_internal_only(authority_env, tool, args):
    env = await authority_env()
    root = await _root(env)  # an owner root row: the continuation's own turn is what is narrow
    text, is_error = await _dispatch(env, tool, dict(args), _cont(root))
    assert not is_error, text
    (child,) = _children(await _all(env, Intention), root)
    assert (child.parent_id, child.root_id, child.depth) == (root.id, root.id, 1)
    assert (child.authority, child.wake_policy, child.origin_kind) == ("internal_only", "continue", "continuation")


async def test_the_spawned_subtask_carries_the_narrowed_stamp(authority_env):
    env = await authority_env()
    root = await _root(env)
    await _dispatch(env, "spawn_task", {"task": "t", "intent": "why"}, _cont(root))
    (child,) = _children(await _all(env, Intention), root)
    (subtask,) = await _all(env, Subtask)
    assert subtask.metadata_["intention"] == {"id": str(child.id), "root_id": str(root.id), "authority": "internal_only"}


async def test_a_context_narrower_than_its_parent_row_wins(authority_env):
    env = await authority_env()
    root = await _root(env, authority="owner")
    ctx = ExecutionContext(
        kind="subtask", session_id="subtask-1", authority="internal_only", intention_id=root.id, root_intention_id=root.id
    )
    await _dispatch(env, "spawn_task", {"task": "t", "intent": "why"}, ctx)
    (child,) = _children(await _all(env, Intention), root)
    assert child.authority == "internal_only"


@pytest.mark.parametrize(
    ("row_authority", "ctx_authority", "expected"),
    [("internal_only", "owner", "internal_only"), ("owner", "owner", "owner")],  # PIN: never widened, never over-narrowed
)
async def test_a_child_is_not_widened_and_an_owner_lineage_stays_owner(
    authority_env, row_authority, ctx_authority, expected
):
    env = await authority_env()
    root = await _root(env, authority=row_authority)
    ctx = ExecutionContext(
        kind="subtask", session_id="subtask-1", authority=ctx_authority, intention_id=root.id, root_intention_id=root.id
    )
    await _dispatch(env, "spawn_task", {"task": "t", "intent": "why"}, ctx)
    (child,) = _children(await _all(env, Intention), root)
    assert child.authority == expected


async def test_a_lineage_spawn_that_asks_for_none_still_goes_to_the_continuation(authority_env):
    env = await authority_env()
    root = await _root(env, wake_policy="remember")
    text, is_error = await _dispatch(
        env, "spawn_task", {"task": "t", "intent": "why", "wake_policy": "none"}, _cont(root)
    )
    assert not is_error, text
    (child,) = _children(await _all(env, Intention), root)
    assert child.wake_policy == "continue"
    assert text.endswith("(wake_policy 'none' is not available from a continuation turn; recorded 'continue')")


def test_origin_authority_narrows_the_wake_policy_like_an_internal_parent():
    parent = intentions.ParentView(id=IID, root_id=IID, depth=0, authority="owner", wake_policy="remember")
    spec = intentions.IntentionSpec(
        intent="x", origin_kind="continuation", wake_policy="none", origin_authority="internal_only"
    )
    assert intentions.resolve_wake_policy(spec, parent) == "continue"
    assert intentions.resolve_wake_policy(dataclasses.replace(spec, inline=True), parent) == "none"
    # PIN: with no claim an owner parent's policy is followed as before.
    plain = intentions.IntentionSpec(intent="x", origin_kind="subtask")
    assert intentions.resolve_wake_policy(plain, parent) == "remember"


def test_an_unknown_origin_authority_fails_closed_and_none_claims_nothing():
    def spec(authority):
        return intentions.spec_from_tool_call(
            intent="x", wake_policy=None, origin_kind="subtask", origin_authority=authority
        )

    assert spec("root").origin_authority == "internal_only"
    assert spec("owner").origin_authority == "owner"
    assert spec(None).origin_authority is None


async def test_the_dispatcher_sets_origin_authority_and_the_model_cannot():
    d, seen = _recording_dispatcher({"spawn_task": True})
    owner = ExecutionContext(kind="interactive", session_id="S1")
    await d.dispatch(
        "spawn_task", {"task": "t", "_origin_authority": "internal_only"}, session_id="S1", context=owner
    )
    assert seen["spawn_task"]["_origin_authority"] == "owner"
    narrow = ExecutionContext(
        kind="subtask", session_id="s", authority="internal_only", intention_id=IID, root_intention_id=RID
    )
    await d.dispatch("spawn_task", {"task": "t", "_origin_authority": "owner"}, session_id="s", context=narrow)
    assert seen["spawn_task"]["_origin_authority"] == "internal_only"


# -- no approval node from a lineage ----------------------------------------

APPROVAL = [
    {
        "name": "ask",
        "type": "approval",
        "instructions": "Proceed?",
        "options": [
            {"id": "go", "label": "Go", "outcome": "proceed"},
            {"id": "stop", "label": "Stop", "outcome": "stop"},
        ],
        "default_option": "stop",
    }
]


@pytest.mark.parametrize("approvals_on", [False, True])
async def test_an_internal_only_turn_cannot_create_an_approval_node(authority_env, approvals_on):
    env = await authority_env(dag_approval_nodes_enabled=approvals_on)
    root = await _root(env)
    text, is_error = await _dispatch(
        env, "dag_create", {"name": "d", "nodes": APPROVAL, "intent": "why"}, _cont(root)
    )
    assert is_error and "an internal-only turn cannot create approval nodes" in text
    assert "resolve_intention(decision='ask')" in text
    assert _children(await _all(env, Intention), root) == []


async def test_an_owner_turn_meets_the_existing_approval_rules_unchanged(authority_env):  # PIN
    env = await authority_env()
    ctx = ExecutionContext(kind="interactive", session_id="S1")
    text, is_error = await _dispatch(env, "dag_create", {"name": "d", "nodes": APPROVAL, "intent": "why"}, ctx)
    assert is_error and "approval nodes are disabled" in text


# -- the D7 downgrade is visible --------------------------------------------

NOTE = "(wake_policy 'continue' is not available from a background turn; recorded 'none')"


async def test_a_downgraded_wake_policy_is_named_in_the_spawn_receipt(authority_env):
    env = await authority_env()
    bg = ExecutionContext(kind="background", session_id="bg-1")
    text, is_error = await _dispatch(env, "spawn_task", {"task": "t", "intent": "why", "wake_policy": "continue"}, bg)
    assert not is_error and text.endswith(NOTE), text
    (it,) = await _all(env, Intention)
    assert it.wake_policy == "none"


async def test_a_downgraded_wake_policy_is_named_in_the_dag_receipt(authority_env):
    env = await authority_env()
    bg = ExecutionContext(kind="background", session_id="bg-2")
    args = {"name": "d", "nodes": NODES, "intent": "why", "wake_policy": "continue"}
    text, is_error = await _dispatch(env, "dag_create", args, bg)
    assert not is_error and text.endswith(NOTE), text


async def test_a_wake_policy_that_was_honoured_adds_no_note(authority_env):  # PIN
    env = await authority_env()
    chat = ExecutionContext(kind="interactive", session_id="S1", channel="telegram:1")
    for policy in ("continue", "remember"):
        text, is_error = await _dispatch(env, "spawn_task", {"task": "t", "intent": "why", "wake_policy": policy}, chat)
        assert not is_error and "not available" not in text, text


async def test_no_wake_policy_argument_means_no_read(authority_env):  # PIN
    env = await authority_env()
    spy = AsyncMock(return_value="none")
    env.heart.intentions.wake_policy_for_source = spy
    bg = ExecutionContext(kind="background", session_id="bg-3")
    text, is_error = await _dispatch(env, "spawn_task", {"task": "t", "intent": "why"}, bg)
    assert not is_error and "not available" not in text
    spy.assert_not_called()


async def test_a_failed_read_adds_no_note_and_does_not_fail_the_spawn(authority_env):
    env = await authority_env()
    env.heart.intentions.wake_policy_for_source = AsyncMock(side_effect=RuntimeError("db down"))
    bg = ExecutionContext(kind="background", session_id="bg-4")
    text, is_error = await _dispatch(env, "spawn_task", {"task": "t", "intent": "why", "wake_policy": "continue"}, bg)
    assert not is_error and "Subtask spawned." in text and "not available" not in text
    assert len(await _all(env, Subtask)) == 1
```

- [ ] **Step 2: Edit the two Phase 1 expected dicts (C6)** in `tests/test_f099_lineage.py`. In `test_origin_arguments_reach_origin_aware_tools_only` the expected dict gains `"_origin_authority": "owner"`:

```python
    assert {k: v for k, v in seen["dag_create"].items() if k.startswith("_")} == {
        "_origin_kind": "subtask",
        "_origin_authority": "owner",
        "_origin_session_id": "subtask-1",
        "_decision_id": PLAN,
        "_intention_id": str(IID),
    }
```

and in `test_origin_arguments_the_model_sent_are_dropped` the first `hidden ==` becomes

```python
    assert hidden == {"_origin_kind": "interactive", "_origin_authority": "owner", "_origin_session_id": "S1"}
```

(Neither changes an existing expectation: both only add the new key.)

- [ ] **Step 3: Run the tests; confirm they fail on the base.**

Run: `"$BIN/nous-test-linux.sh" "$WT" runner pg "$DB" tests/test_f099_authority.py tests/test_f099_lineage.py -q`
Expected: FAIL. `test_a_context_narrower_than_its_parent_row_wins` gets `owner` on the base; the approval refusal is absent; `IntentionSpec` has no `origin_authority`; the D7 note is absent; the two edited Phase 1 tests fail until `_origin_args` sends the key.

- [ ] **Step 4: Implement `nous/brain/intentions.py`.**

4a. `IntentionSpec`: add, after `origin_decision_id`:

```python
    # The spawning turn's authority (F099 Phase 2). A child is never wider than the turn
    # that spawned it: this narrows what the parent ROW says, and never widens it. None
    # for a code path (scheduler, work queue, app.act, REST): owner.
    origin_authority: str | None = None
```

4b. `resolve_wake_policy`: the internal-lineage branch becomes

```python
    if (parent is not None and parent.authority == AUTHORITY_INTERNAL) or spec.origin_authority == AUTHORITY_INTERNAL:
        # Inside an internal-only lineage, or from an internal-only turn, the argument is
        # ignored: every result goes back to the continuation (an inline one returns in-turn).
        return WAKE_NONE if spec.inline else WAKE_CONTINUE
```

4c. A helper and `spec_from_tool_call`: add the parameter `origin_authority: str | None = None` (after `intention_id`), and pass `origin_authority=_known_authority(origin_authority)` to `IntentionSpec(...)`. Above `spec_from_tool_call`:

```python
def _known_authority(value: Any) -> str | None:
    """No claim is None; an unknown value fails closed to internal_only."""
    if value is None:
        return None
    return value if value in AUTHORITIES else AUTHORITY_INTERNAL
```

4d. `prepare_intention`: replace the `narrowed = …` statement with

```python
    narrowed = (
        lineage_parent is not None and lineage_parent.authority == AUTHORITY_INTERNAL
    ) or spec.origin_authority == AUTHORITY_INTERNAL
```

and in its docstring replace "never wider authority (I3)" with "never wider authority than the parent row or the spawning turn (I3, min of the two)".

4e. A read for the D7 note, after `lineage_for_source`:

```python
async def wake_policy_for_source(session: AsyncSession, agent_id: str, source_kind: str, source_id: Any) -> str | None:
    """The wake policy recorded for a source's intention, or None: what a spawn tool reports back (D7)."""
    return (
        await session.execute(
            select(Intention.wake_policy).where(
                Intention.agent_id == agent_id,
                Intention.source_kind == source_kind,
                Intention.source_id == str(source_id),
            )
        )
    ).scalar_one_or_none()
```

and on `IntentionStore`:

```python
    async def wake_policy_for_source(self, source_kind: str, source_id: Any) -> str | None:
        async with self._db.session() as session:
            return await wake_policy_for_source(session, self._agent_id, source_kind, source_id)
```

- [ ] **Step 5: Implement `nous/dag/store.py`.** Next to `intention_lineage`:

```python
    async def intention_wake_policy(self, dag_id: UUID) -> str | None:
        """F099 D7: the wake policy recorded for the DAG's intention, for dag_create's receipt."""
        async with self._db.session() as session:
            return await intentions.wake_policy_for_source(session, self._agent_id, intentions.SOURCE_DAG, dag_id)
```

- [ ] **Step 6: Implement `nous/api/tools.py`.**

6a. `_origin_args`: start with both keys.

```python
    out: dict[str, Any] = {"_origin_kind": ctx.kind, "_origin_authority": ctx.authority}
```

6b. A module function, after `_origin_args`:

```python
async def _recorded_policy_note(spec: intentions.IntentionSpec | None, read: Callable[[], Any]) -> str:
    """F099 D7 made visible: when the model asked for a wake_policy and the recorded one
    differs (a background turn cannot widen to ``continue``; inside a lineage every result
    goes to the continuation), the spawn receipt says so. One read, and only when the model
    passed the argument. A failed read adds nothing: the spawn already succeeded."""
    if spec is None or spec.wake_policy is None:
        return ""
    try:
        recorded = await read()
    except Exception:
        logger.warning("F099: could not read the recorded wake policy", exc_info=True)
        return ""
    if recorded is None or recorded == spec.wake_policy:
        return ""
    return f" (wake_policy '{spec.wake_policy}' is not available from a {spec.origin_kind} turn; recorded '{recorded}')"
```

(`Callable` is already imported from `collections.abc`.)

6b'. `spawn_task`, `spawn_sync` and `schedule_task`: add `_origin_authority: str | None = None,` after `_intention_id: str | None = None,` in each signature. In `spawn_task` and `schedule_task` pass `origin_authority=_origin_authority,` as the last argument of `intentions.spec_from_tool_call(...)`; in `spawn_sync` forward `_origin_authority=_origin_authority,` in its call to `spawn_task`. (`schedule_task` passes `wake_policy=None` and `container=True`, so it never produces a downgrade note.)

6c. `spawn_task`'s fire-and-forget return (`if not await_result:`) becomes

```python
            if not await_result:
                # Fire-and-forget (existing behavior); F099 D7: say so when the wake policy was downgraded.
                note = await _recorded_policy_note(
                    spec, lambda: heart.intentions.wake_policy_for_source(intentions.SOURCE_SUBTASK, subtask.id)
                )
                return {
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                f"Subtask spawned.\n"
                                f"ID: {subtask.id}\n"
                                f"Priority: {priority}\n"
                                f"Timeout: {effective_timeout}s{note}"
                            ),
                        }
                    ]
                }
```

6d. `dag_create`: first, right after `wants_approval = …`:

```python
        if wants_approval and kwargs.get("_origin_authority") == AUTHORITY_INTERNAL:
            # F099 section 4.4: an approval node asks the OWNER a question, which an
            # internal-only turn may not do on its own; it asks through resolve_intention.
            return _tool_error(
                "Error: an internal-only turn cannot create approval nodes; "
                "ask the owner through resolve_intention(decision='ask') instead."
            )
```

then the spec call gains `origin_authority=kwargs.get("_origin_authority"),`, and the receipt is built as

```python
            note = await _recorded_policy_note(spec, lambda: store.intention_wake_policy(dag.id))
            return {"content": [{"type": "text", "text": "\n".join(lines) + note}]}
```

- [ ] **Step 7: Run the tests and the Phase 1 neighbours.**

Run: `"$BIN/nous-test-linux.sh" "$WT" runner pg "$DB" tests/test_f099_authority.py tests/test_f099_lineage.py tests/test_f099_capture.py tests/test_f099_intentions.py tests/test_f099_code_paths.py tests/test_f099_closing.py tests/test_f099_routing_pins.py tests/test_f099_tool_schemas.py tests/test_dag_tools.py tests/test_tools.py -q`
Expected: PASS. `test_f099_tool_schemas.py` (the schema snapshot) must still pass byte for byte: no schema changed.

- [ ] **Step 8: Lint and commit**

```bash
cd "$WT"
cat > /tmp/f099-2a-6.txt <<'EOF'
feat(F099): a child is never wider than the turn that spawned it

_origin_args always sends the turn's authority; IntentionSpec carries it and
prepare_intention takes the narrower of it and the parent row's, so a
continuation spawning under an owner root creates an internal_only child
whose results go back to the continuation. dag_create refuses an approval
node from an internal_only turn. The D7 downgrade of wake_policy is named in
the spawn_task and dag_create receipts.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_015q4W1nke7JzGaZkC21Whng
EOF
git add nous/brain/intentions.py nous/dag/store.py nous/api/tools.py tests/test_f099_authority.py tests/test_f099_lineage.py
git commit -F /tmp/f099-2a-6.txt
```

### Task 2a.7: `cancel_task` only on the turn's own lineage; lineage web calls are logged

**Files:**
- Modify: `nous/api/tools.py`: `ToolDispatcher.dispatch` (a block after the origin injection); `cancel_task` in `create_subtask_tools` and a helper beside it
- Modify: `tests/test_f099_authority.py`: appended tests

**Interfaces:**
- Produces: `dispatch` sets, for `cancel_task` only and only when `ctx.authority == "internal_only"`, the hidden arguments `_authority` and `_root_intention_id` (the root's id, or `""` when there is none: a damaged stamp, C8). With an owner context nothing is injected, so the handler is byte-identical to today.
- Produces: `cancel_task(task_id, _authority=None, _root_intention_id=None)`. When `_authority == "internal_only"` it cancels only a target whose intention (looked up as a subtask, then as a schedule) has `root_id == _root_intention_id`. A target with no intention row (work from before F099), another lineage's work, a schedule (a schedule is its own root) and a missing root id are all refused: `Tool error: task <id> is not part of this lineage; cancel_task may only cancel work this lineage started.`
- Produces (spec §9): `dispatch` logs at INFO `"F099: %s from lineage root %s (session %s)"` for `INTERNAL_ONLY_LOGGED_TOOLS` (`web_fetch`, `web_search`) when `ctx.authority == "internal_only"`, before the handler runs.
- Not produced: any change for a DAG node's subtask (it has a DAG intention, not a subtask one, so `cancel_task` on it is refused: cancel a DAG through `dag_manage`, which a lineage does not have).

- [ ] **Step 1: Write the failing tests.** Append to `tests/test_f099_authority.py` (the imports are repeated with `# noqa: E402`, so the block stands alone):

```python
# ---------------------------------------------------------------------------
# Task 2a.7: cancel_task only on the turn's own lineage; lineage web calls are logged
# ---------------------------------------------------------------------------

import logging  # noqa: E402
import re  # noqa: E402


async def _spawn(env, ctx, task="work") -> uuid.UUID:
    text, is_error = await _dispatch(env, "spawn_task", {"task": task, "intent": "why"}, ctx)
    assert not is_error, text
    return uuid.UUID(re.search(r"ID: ([0-9a-f-]{36})", text).group(1))


async def _status(env, task_id: uuid.UUID) -> str:
    async with env.db.session() as s:
        return (await s.execute(select(Subtask.status).where(Subtask.id == task_id))).scalar_one()


async def _cancel(env, task_id, ctx, **extra):
    return await _dispatch(env, "cancel_task", {"task_id": str(task_id), **extra}, ctx)


async def test_a_continuation_may_cancel_work_of_its_own_lineage(authority_env):
    env = await authority_env()
    root = await _root(env)
    cont = _cont(root)
    child = await _spawn(env, cont)
    text, is_error = await _cancel(env, child, cont)
    assert not is_error and "cancelled" in text, text
    assert await _status(env, child) == "cancelled"


async def test_a_continuation_may_not_cancel_foreign_work(authority_env):
    env = await authority_env()
    root = await _root(env)
    cont = _cont(root)
    chat = ExecutionContext(kind="interactive", session_id="S1", channel="telegram:1")
    foreign = await _spawn(env, chat)  # the owner's own background task, a root of its own
    other_root = await _root(env)
    siblings_lineage = await _spawn(env, _cont(other_root))
    legacy = (await env.heart.subtasks.create(task="work from before F099")).id  # no intention row
    for target in (foreign, siblings_lineage, legacy):
        text, is_error = await _cancel(env, target, cont)
        assert is_error and "is not part of this lineage" in text, text
        assert await _status(env, target) == "pending"


async def test_a_model_cannot_forge_the_authority_or_the_root_the_handler_checks(authority_env):
    env = await authority_env()
    root = await _root(env)
    foreign = await _spawn(env, ExecutionContext(kind="interactive", session_id="S1", channel="telegram:1"))
    forged = {"_authority": "owner", "_root_intention_id": str(foreign)}
    text, is_error = await _cancel(env, foreign, _cont(root), **forged)
    assert is_error and "is not part of this lineage" in text
    assert await _status(env, foreign) == "pending"


async def test_a_lineage_with_a_damaged_stamp_cancels_nothing(authority_env):
    """Fail closed (C8): internal_only with no root id cannot prove any target is its own."""
    env = await authority_env()
    root = await _root(env)
    child = await _spawn(env, _cont(root))
    damaged = ExecutionContext(kind="subtask", session_id="subtask-1", authority="internal_only")
    text, is_error = await _cancel(env, child, damaged)
    assert is_error and "is not part of this lineage" in text
    assert await _status(env, child) == "pending"


async def test_an_owner_turn_still_cancels_any_task(authority_env):  # PIN
    env = await authority_env()
    foreign = await _spawn(env, ExecutionContext(kind="interactive", session_id="S1", channel="telegram:1"))
    text, is_error = await _cancel(env, foreign, ExecutionContext(kind="interactive", session_id="S2"))
    assert not is_error and "cancelled" in text, text
    assert await _status(env, foreign) == "cancelled"


async def test_a_cancel_task_dispatch_from_an_owner_turn_injects_nothing():  # PIN
    d, seen = _recording_dispatcher({"cancel_task": False})
    owner = ExecutionContext(kind="interactive", session_id="S1")
    await d.dispatch("cancel_task", {"task_id": "x"}, session_id="S1", context=owner)
    assert not [k for k in seen["cancel_task"] if k.startswith("_")]


async def test_a_cancel_task_dispatch_from_an_internal_only_turn_injects_the_authority_and_root():
    d, seen = _recording_dispatcher({"cancel_task": False})
    lineage = ExecutionContext(
        kind="subtask", session_id="s", authority="internal_only", intention_id=IID, root_intention_id=RID
    )
    damaged = ExecutionContext(kind="subtask", session_id="s", authority="internal_only")
    await d.dispatch("cancel_task", {"task_id": "x"}, session_id="s", context=lineage)
    assert (seen["cancel_task"]["_authority"], seen["cancel_task"]["_root_intention_id"]) == ("internal_only", str(RID))
    await d.dispatch("cancel_task", {"task_id": "x"}, session_id="s", context=damaged)
    assert (seen["cancel_task"]["_authority"], seen["cancel_task"]["_root_intention_id"]) == ("internal_only", "")


async def test_web_calls_from_a_lineage_are_logged_with_their_root(caplog):
    d, _ = _recording_dispatcher({"web_fetch": False, "web_search": False, "recall_deep": False})
    lineage = ExecutionContext(
        kind="subtask", session_id="subtask-1", authority="internal_only", intention_id=IID, root_intention_id=RID
    )
    owner = ExecutionContext(kind="subtask", session_id="subtask-2")
    with caplog.at_level(logging.INFO, logger="nous.api.tools"):
        await d.dispatch("web_fetch", {}, session_id="subtask-1", context=lineage)
        await d.dispatch("web_search", {}, session_id="subtask-1", context=lineage)
        await d.dispatch("recall_deep", {}, session_id="subtask-1", context=lineage)
        await d.dispatch("web_fetch", {}, session_id="subtask-2", context=owner)
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("F099: web_")]
    assert lines == [
        f"F099: web_fetch from lineage root {RID} (session subtask-1)",
        f"F099: web_search from lineage root {RID} (session subtask-1)",
    ]
```

- [ ] **Step 2: Run them; confirm they fail on the base.**

Run: `"$BIN/nous-test-linux.sh" "$WT" runner pg "$DB" tests/test_f099_authority.py -q`
Expected: `test_a_continuation_may_not_cancel_foreign_work` and the forged-root and damaged-stamp tests FAIL (the cancel succeeds on the base); the logging test FAILS (no INFO line).

- [ ] **Step 3: Implement `nous/api/tools.py`.**

3a. Import: `from nous.api.tool_policy import INTERNAL_ONLY_LOGGED_TOOLS` (with the other `nous.api` imports; `nous.api.tool_policy` has no import of `nous.api.tools`, so there is no cycle).

3b. In `ToolDispatcher.dispatch`, after the `if name in self._origin_aware:` block and before the `ctx.channel` blocks, add:

```python
            if ctx.authority == AUTHORITY_INTERNAL:
                if name in INTERNAL_ONLY_LOGGED_TOOLS:
                    # Spec section 9: a lineage may fetch or search the web, and its root is logged.
                    logger.info("F099: %s from lineage root %s (session %s)", name, ctx.root_intention_id, session_id)
                if name == "cancel_task":
                    # The own-lineage rule needs the target's row, so the handler enforces it.
                    # An empty root (a damaged stamp) is passed too, and refuses everything (fail closed).
                    args = {
                        **args,
                        "_authority": ctx.authority,
                        "_root_intention_id": str(ctx.root_intention_id) if ctx.root_intention_id else "",
                    }
```

3c. In `create_subtask_tools`, a helper immediately before `cancel_task`, and the changed handler:

```python
    async def _in_lineage(uid: UUID, root_id: str | None) -> bool:
        """Whether the subtask or schedule ``uid`` belongs to the lineage rooted at ``root_id``.

        Looked up as a subtask, then as a schedule (the handler's order). Work with no intention
        row (from before F099), another lineage's work and a schedule (its own root) are not.
        """
        root = intentions.parse_uuid(root_id)
        if root is None:
            return False
        for kind in (intentions.SOURCE_SUBTASK, intentions.SOURCE_SCHEDULE):
            row = await heart.intentions.get_for_source(kind, uid)
            if row is not None:
                return row.root_id == root
        return False

    async def cancel_task(
        task_id: str,
        _authority: str | None = None,  # F099: injected by ToolDispatcher for an internal_only turn
        _root_intention_id: str | None = None,
    ) -> dict[str, Any]:
        """Cancel a subtask or deactivate a schedule by ID.

        F099: an ``internal_only`` turn (``_authority``, injected by the dispatcher) may
        cancel only a subtask whose intention has ``root_id == _root_intention_id``. A
        missing root (a damaged stamp), work from before F099 (no intention row), another
        lineage's work, a schedule (its own root) and a DAG node's subtask are all refused.
        The id is parsed first, so a non-UUID still answers "Invalid task ID".

        Args:
            task_id: UUID of the subtask or schedule to cancel

        Returns:
            MCP-compliant response confirming cancellation or error
        """
        try:
            from uuid import UUID as _UUID

            uid = _UUID(task_id)

            # F099 section 4.4: an internal_only turn may cancel only work its own lineage started.
            if _authority == AUTHORITY_INTERNAL and not await _in_lineage(uid, _root_intention_id):
                return _tool_error(
                    f"Tool error: task {task_id} is not part of this lineage; "
                    "cancel_task may only cancel work this lineage started."
                )

            # Try subtask cancel first
```

The `# Try subtask cancel first` block and everything after it in the handler (the schedule deactivation, the "nothing found" error and the two `except` clauses) stay exactly as they are.

- [ ] **Step 4: Run the tests and the task-tool neighbours.**

Run: `"$BIN/nous-test-linux.sh" "$WT" runner pg "$DB" tests/test_f099_authority.py tests/test_f099_lineage.py tests/test_tools.py tests/test_subtask_tools.py tests/test_f099_capture.py -q`
(Skip any file that does not exist on `main`: `ls tests | grep -i subtask_tools`.) Expected: PASS.

- [ ] **Step 5: Lint and commit**

```bash
cd "$WT"
cat > /tmp/f099-2a-7.txt <<'EOF'
feat(F099): cancel_task only on the lineage's own work; log lineage web calls

For an internal_only turn the dispatcher passes cancel_task the turn's
authority and root id, and the handler refuses any target whose intention is
not in that root (work from before F099, another lineage, a schedule, or a
damaged stamp with no root all fail closed). web_fetch and web_search from a
lineage are logged with their root (spec section 9). An owner turn is
unchanged.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_015q4W1nke7JzGaZkC21Whng
EOF
git add nous/api/tools.py tests/test_f099_authority.py
git commit -F /tmp/f099-2a-7.txt
```

### Task 2a.8: The orchestrator fails closed; docs; the PR

**Files:**
- Modify: `nous/dag/orchestrator.py`: `_launch_subtask_node` and `_launch_check_node` (the lineage guards)
- Modify: `nous/heartbeat/runner.py`: `_execute_callback` (the callback context carries the check's lineage)
- Modify: `tests/test_f099_lineage.py`: appended tests
- Modify: `docs/reference/environment-variables.md`, `docs/reference/shipped-features.md`, `docs/reference/project-structure.md`, `docs/features/INDEX.md`

**Interfaces:**
- Produces (Phase 1 carry-over, 1.7 note): after the existing lineage lookup in both launch functions, `lineage` must be `None` or a `dict`. Anything else defers the node with `_defer_node(node, dag, "lineage stamp unreadable", backstop="lineage still unreadable")` and creates nothing. The stamp conditions `isinstance(lineage, dict)` become `lineage is not None`. A node therefore never runs unstamped because its stamp was malformed (before, a non-dict silently meant "no lineage" and the node ran as `owner`).
- Produces (review S1): a check's `on_complete` callback runs under the check's lineage. `_execute_callback` builds its `ExecutionContext(kind="heartbeat_callback", …)` with `intention_id`, `root_intention_id` and `authority` from `lineage_from_stamp(check._intention)`, exactly as `DynamicCheck._run_turn` does for the check. A callback of a stamped check is therefore `internal_only` and narrowed by 2a.3 and 2a.4 (its `on_complete_tools` are intersected with the allowed set, so `bash` and `heartbeat_check_create` are not offered). Before, it ran as `owner`, and was safe only because no lineage path can create a check with an `on_complete`. A check object that is not a `DynamicCheck` (a test double) carries no stamp: `None`, owner.
- Produces: documentation of the flag-off exception and of the enforcement behaviour.

- [ ] **Step 1: Write the failing tests.** Append to `tests/test_f099_lineage.py` (it already defines `dag_env`, `_dag` and the imports at the Task 1.7 block; this block adds none):

```python
# ---------------------------------------------------------------------------
# F099 Phase 2a: a lineage that is not a stamp defers the launch (it never means "no lineage")
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("node_type", [DAGNodeType.subtask, DAGNodeType.check])
@pytest.mark.parametrize("bad", ["garbage", ["not", "a", "stamp"], 7], ids=["str", "list", "int"])
async def test_a_lineage_that_is_not_a_stamp_defers_the_launch_and_creates_nothing(dag_env, node_type, bad):
    dag, node, _ = await _dag(dag_env, node_type)
    dag_env.store.intention_lineage = AsyncMock(return_value=bad)
    dag_env.orch._defer_node = AsyncMock()
    if node_type == DAGNodeType.check:
        await dag_env.orch._launch_check_node(node, dag)
        dag_env.loader.create_check.assert_not_called()
    else:
        await dag_env.orch._launch_subtask_node(node, dag)
        dag_env.subtask_mgr.create.assert_not_called()
    dag_env.orch._defer_node.assert_awaited_once()
    assert dag_env.orch._defer_node.await_args.args[2] == "lineage stamp unreadable"
    assert dag_env.orch._defer_node.await_args.kwargs["backstop"] == "lineage still unreadable"


async def test_the_callback_of_a_stamped_check_runs_under_its_lineage():
    from test_f099_phase0b import _runner as _heartbeat_runner

    from nous.heartbeat.registry import CheckRegistry as _Registry

    hb, triage = _heartbeat_runner(_Registry())
    check = DynamicCheck(
        check_id="c-id",
        name="dag-x-chk",
        prompt="p",
        tools=["web_search"],
        interval=300,
        on_complete_prompt="Tell the user",
        on_complete_tools=["web_search", "bash", "heartbeat_check_create"],
        intention=STAMP,
    )
    await hb._execute_callback(check)
    ctx = triage.run_turn.call_args.kwargs["context"]
    assert (ctx.kind, ctx.intention_id, ctx.root_intention_id, ctx.authority) == (
        "heartbeat_callback",
        IID,
        RID,
        "internal_only",
    )


async def test_the_callback_of_an_unstamped_check_is_owner_as_before():  # PIN
    from test_f099_phase0b import _callback_check
    from test_f099_phase0b import _runner as _heartbeat_runner

    from nous.heartbeat.registry import CheckRegistry as _Registry

    hb, triage = _heartbeat_runner(_Registry())
    await hb._execute_callback(_callback_check())
    ctx = triage.run_turn.call_args.kwargs["context"]
    assert (ctx.authority, ctx.intention_id, ctx.root_intention_id) == ("owner", None, None)


async def test_a_readable_stamp_and_no_stamp_still_launch_as_before(dag_env):  # PIN
    dag, node, stamp = await _dag(dag_env, DAGNodeType.subtask, authority="internal_only")
    await dag_env.orch._launch_subtask_node(node, dag)
    assert dag_env.subtask_mgr.create.call_args.kwargs["metadata"]["intention"] == stamp
    dag, node, _ = await _dag(dag_env, DAGNodeType.subtask, with_intention=False)
    dag_env.subtask_mgr.create.reset_mock()
    await dag_env.orch._launch_subtask_node(node, dag)
    assert "intention" not in dag_env.subtask_mgr.create.call_args.kwargs["metadata"]
```

- [ ] **Step 2: Run them; confirm they fail on the base.**

Run: `"$BIN/nous-test-linux.sh" "$WT" runner pg "$DB" tests/test_f099_lineage.py -k "not_a_stamp" -q`
Expected: FAIL on the base (the node is launched unstamped: `create` is called and `_defer_node` is not).

- [ ] **Step 3: Implement `nous/dag/orchestrator.py`.** In `_launch_subtask_node`, immediately after the `try/except` that reads `lineage = await self._store.intention_lineage(dag.id)` (before `augmented = await self._build_predecessor_context(…)`), and in `_launch_check_node` at the same point:

```python
        if lineage is not None and not isinstance(lineage, dict):
            # F099 I3, fail closed: a lineage that is not a stamp must not become "no
            # lineage", which would run the node as owner.
            logger.warning(
                "Intention lineage of DAG %s is not a stamp (%s); deferring node %s",
                dag.id,
                type(lineage).__name__,
                node.name,
            )
            await self._defer_node(node, dag, "lineage stamp unreadable", backstop="lineage still unreadable")
            return
```

and change the two stamp conditions: `**({"intention": lineage} if isinstance(lineage, dict) else {})` in the `metadata=` of `self._subtask_mgr.create(…)`, and in the `metadata={…}` of `create_check()`, to `**({"intention": lineage} if lineage is not None else {})`.

- [ ] **Step 3b: Implement `nous/heartbeat/runner.py` (S1).** Import `lineage_from_stamp` (`from nous.api.execution_context import ExecutionContext, lineage_from_stamp`). In `_execute_callback`, after `run_id = uuid4().hex`:

```python
        # F099 I3: a callback runs under its check's lineage, as the check's own turn does
        # (DynamicCheck._run_turn). A test double that is not a DynamicCheck carries no stamp.
        stamp = check._intention if isinstance(check, DynamicCheck) else None
        intention_id, root_intention_id, authority = lineage_from_stamp(stamp)
```

and add `intention_id=intention_id, root_intention_id=root_intention_id, authority=authority,` to the `ExecutionContext(kind="heartbeat_callback", …)` the retry loop builds. Add `tests/test_heartbeat_dynamic.py` and `tests/test_f099_phase0b.py` to Step 5's targeted run.

- [ ] **Step 4: Update the docs.**

4a. `docs/reference/environment-variables.md`, the `NOUS_INTENTIONS_ENABLED` row: replace `and no tool set is narrowed.` with: `and the flag narrows no tool set. Narrowing keys on a turn's lineage authority (F099 Phase 2a), never on a flag: Phase 1 writes no \`internal_only\` row, so only a turn whose lineage stamp is damaged (it fails closed to \`internal_only\`) loses tools, and it never gains one.`

4b. Same file, the `NOUS_TOOL_CONTEXT_POLICY_MODE` row: append, inside the same cell before the closing ` |`: ` F099 Phase 2a: a turn whose lineage \`authority\` is \`internal_only\`, and an \`approved_action\` call, are enforced BEFORE this setting and \`NOUS_TOOL_OFFERED_SET_ENFORCEMENT_MODE\` are read, so neither \`off\` nor \`warn\` lets a call through that the lineage may not make: a tool that was not offered, a call rated \`external\`, a \`write_file\` outside \`<workspace_dir>/intentions/<root_id>/\` and a \`cancel_task\` outside the turn's own lineage are refused (ledger refusal code \`internal_only\`; the persisted violation is \`internal_only:<code>\` with mode \`enforce\`). The offered set of such a turn is narrowed to \`none\`/\`write\` tools minus a denylist (spec §4.4). Both rules ignore the modes by design.`

4c. `docs/reference/shipped-features.md`: after the `F099 Phase 1` row add:
`| F099 Phase 2a | [Enforcement substrate](../superpowers/specs/2026-10-05-f099-intentions-and-continuation-design.md) (the \`continuation\` and \`approved_action\` context kinds; \`AgentRunner._offered_tools\`, one offered-set helper for both loops, which narrows an \`internal_only\` turn to the spec's allowed set; a strict block at the head of \`_authorize_tool_call\` that refuses unoffered, external and out-of-path calls whatever the modes say; only \`TERMINAL_EXTRA_TOOLS\` end the loop; a child's authority is the narrower of its turn and its parent row; \`dag_create\` refuses approval nodes from a lineage; the D7 downgrade is named in the receipt; the orchestrator fails closed on a malformed lineage. No caller creates a continuation yet; the only flag-off change is that a turn with a damaged lineage stamp loses tools) | in review |`

4d. `docs/reference/project-structure.md`: the `tool_policy.py` line becomes `# Harness 2a: per-context policy at the choke point; F099: the internal_only allowed set, denylist and per-call rules`.

4e. `docs/features/INDEX.md`: append to the F099 status cell, after `Phases 0a/0b merged`: `; Phase 2a (enforcement substrate: internal_only narrowing and strict dispatch refusal, no caller yet) in review`.

- [ ] **Step 5: Run the tests, then the full gate.**

Run: `"$BIN/nous-test-linux.sh" "$WT" runner pg "$DB" tests/test_f099_lineage.py tests/test_dag_orchestrator.py tests/test_f099_authority.py -q`
Expected: PASS. Then the full gate once: `"$BIN/gate-with-migrations.sh" f099-2a:"$WT":<fresh_db>:81`, compared with a gate of the base. Run `"$BIN/lint-delta.sh" "$WT"`: clean.

- [ ] **Step 6: Commit, then open the PR**

```bash
cd "$WT"
cat > /tmp/f099-2a-8.txt <<'EOF'
feat(F099): a DAG lineage that is not a stamp defers the node; Phase 2a docs

The orchestrator no longer reads "a lineage that is not a dict" as "no
lineage" (which ran the node as owner): both launch paths defer the node
instead. Documents the enforcement substrate and its one flag-off change.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_015q4W1nke7JzGaZkC21Whng
EOF
git add nous/dag/orchestrator.py tests/test_f099_lineage.py docs/reference/environment-variables.md docs/reference/shipped-features.md docs/reference/project-structure.md docs/features/INDEX.md
git commit -F /tmp/f099-2a-8.txt
```

The PR description (write it to a file and use `gh pr create --body-file`) must contain, in this order: what the PR builds (the Architecture paragraph above); **"Flag-off behaviour change"**: a turn whose lineage stamp is damaged fails closed to `internal_only` and now loses its non-allowed tools, never gains one, and Phase 1 writes no `internal_only` row, so nothing else changes; the contract conflicts C1 to C10 with their resolutions (the lead's confirmation of C2 in particular); the residuals from Review Focus; the F078 refuse log now INFO (the offered set is unchanged); that the `stream_chat` tests prove the shared offered set and the strict block, not the context policy on that loop; the mutation checks run in Task 2a.4; and the line "2b is independent; both edit `nous/brain/intentions.py` additively, so whichever merges second rebases." End with the attribution line the harness gives for pull requests.

---

## Self-review (done while writing; reviewers may re-run it)

**Spec coverage.**

| Spec / contract item | Where |
|---|---|
| §4.4 new `ContextKind` `continuation` and its `CONTEXT_POLICY` row; contract §4.4 `approved_action` row and the new `ExecutionContext` fields, `__post_init__` | 2a.1 |
| §4.4 offered-set table: allowed (class none/write, fail closed on unknown), denied-though-allowed denylist, per-call `write_file` and `cancel_task`, spawn tools to continuation only and removed at a limit, never offered external/irreversible | 2a.2 (the rules, with a literal pin), 2a.3 (the helper) |
| §4.4 "one helper builds the offered set for both loops" | 2a.3 |
| §4.4 dynamic checks in a lineage get their declared tools intersected with the allowed set; Phase 1 carry-over S10 (`heartbeat_check_create`) | 2a.3 (`test_a_lineage_check_loses_…`) |
| §4.4 dispatch enforcement before the `off` early return, `force_block`: unoffered tool, `external` call, `write_file` path, `cancel_task` lineage | 2a.4 (first three; modes matrix, both loops), 2a.7 (`cancel_task`) |
| §4.4 extra-tool termination: only a terminal tool ends the loop; `submit_final_report` terminal | 2a.5 |
| §4.4 `resolve_intention` / `propose_action` and their `TOOL_CLASSES` entries | **not here**: 2c and 2d (the mechanism lands in 2a.5) |
| §4.1 Authority: a child under an `internal_only` parent or created by a continuation is `internal_only` (min of context and parent row); wake policy follows | 2a.6 |
| §4.1 D7 visible to the model (final-review Minor 8) | 2a.6 |
| §7 Phase 2: narrowing for continuation, subtask, DAG node/callback/fix, dynamic checks (every `OWNER_KINDS` kind, `is_subtask` both ways) | 2a.3 |
| §7 Phase 2: forged `send_email`, `run_python`, `bash` refused in both loops | 2a.4 (`_tool_loop`; `stream_chat` through the seam, C10) |
| §7 Phase 2: `write_file` outside `intentions/<root>/` refused; `cancel_task` on foreign work refused | 2a.2, 2a.4, 2a.7 |
| §7 Phase 2: `internal_only` `dag_create` with an approval node refused; lineage subtasks offered no spawn tools | 2a.6, 2a.3 |
| §7 Phase 2: "A continuation's `dag_create` creates a child, not a root" | 2a.6 (`test_a_continuation_spawn_under_an_owner_root_is_internal_only`, `dag_create` row) |
| §7 Phase 2: injection test, "a result body that asks for `send_email` produces no send" | 2a.4 asserts the refusal for every mode (the model's behaviour is not tested, as the spec says) |
| §9 `web_fetch` / `web_search` logged with their root | 2a.7 |
| Flag-off parity for `owner` contexts: offered sets byte-identical, authorization identical | 2a.3 (reference implementation), 2a.4 (`test_an_owner_turn_is_authorized_exactly_as_before`), 2a.7 (injection and cancel pins) |
| Carry-over: orchestrator `isinstance(lineage, dict)` fails open | 2a.8 |
| Review S1: a stamped check's `on_complete` callback inherits the lineage (authority `internal_only`) | 2a.8 (Step 3b and `test_the_callback_of_a_stamped_check_runs_under_its_lineage`) |
| Carry-over: `heartbeat_check_create` from a lineage | 2a.3 (offered-set intersection, as the contract chose) |
| Contract §4.15: Ledger view's context-kind list | 2a.1 |
| Contract §4.5 dashboard acceptance of the new event | 2a.4 (C2: recorded as `enforce`, so no change is needed) |

Not in this plan, by design: the cancelled-root refusal and `set_cancelled_roots` (2e, C3); `resolve_intention`, `propose_action`, their schemas and `TOOL_CLASSES` rows (2c, 2d); `execute_single_call` and the `proposal:{id}` idempotency scope (2d); the `IntentionClosePass` exclusion and every other 2b item; lineage tokens for DAG checks (2c); the continuation runner and any caller of the two new kinds.

**Type and name consistency** was checked against the contract:
- `ContextKind` members `continuation`, `approved_action`; `ExecutionContext` fields `proposal_id`, `arrival_id`, `claim_token`, `spawn_blocked`; the two `CONTEXT_POLICY` rows (`ContextPolicy(_LOCAL, spawn=frozenset({"spawn_task", "dag_create"}))`, `ContextPolicy(_ALL, spawn=True)`)
- `INTERNAL_ONLY_DENYLIST` (15 names, copied from the spec), `INTERNAL_ONLY_SPAWN_TOOLS`, `INTERNAL_ONLY_CHECKED_TOOLS`, `INTERNAL_ONLY_LOGGED_TOOLS`; `internal_only_allowed(name, *, ctx)`; `internal_only_call_violation(ctx, name, tool_input, *, workspace_dir)` with the codes `"external"`, `"write_path"`, `"foreign_cancel"`
- `AgentRunner._offered_tools(ctx, frame_id, *, is_subtask, tool_filter, refuse_active, extra_tools=None)`; `TERMINAL_EXTRA_TOOLS`
- `Refusal(text, "internal_only")` and `REFUSAL_CODES`; the event `harness_context_policy_violation` (mode and violation, C2)
- `_origin_args` key `_origin_authority`; `IntentionSpec.origin_authority`; `spec_from_tool_call(origin_authority=)`; handlers' `_origin_authority`
- `cancel_task(task_id, _authority=None, _root_intention_id=None)`; the dispatcher's injected `_authority` and `_root_intention_id`
- `intentions.wake_policy_for_source`, `IntentionStore.wake_policy_for_source`, `DAGStore.intention_wake_policy`; `_recorded_policy_note`
- `_defer_node(node, dag, "lineage stamp unreadable", backstop="lineage still unreadable")`
- Files named in the contract's 2a column (`runner.py`, `tool_policy.py`, `execution_context.py`, `tools.py`, `dag/orchestrator.py`, `brain/intentions.py`) are the ones edited, plus the four the code required: `cognitive/ledger_store.py` (C1), `dag/store.py` (C7), `heartbeat/runner.py` (review S1) and `dashboard-app/.../Ledger.svelte` (contract §4.15).

**Test hermeticity** was checked: every Settings is `Settings(_env_file=None, …)`, or a `_runner(...)`/`_make_mock_settings` double the existing runner tests use; every DB test has its own agent id; the intentions tests set both flags; no test depends on a model call; ids used in `parametrize` are fixed constants.
