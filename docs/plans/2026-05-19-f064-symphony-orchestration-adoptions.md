# F064 Symphony Orchestration Adoptions — Implementation Plan

**Date:** 2026-05-19
**Plan version:** v1.2 (post-3-agent-review + re-review revision; see §13 for delta)
**Spec:** `docs/features/F064-symphony-orchestration-adoptions.md`
**Branch:** `feat/F064-implementation` (forked from `origin/main` at `f61c888`)
**Scope:** All six sub-features F064.1 – F064.6 in a single PR (user-chosen — supersedes the spec's recommended 4-PR split)
**Estimated LOE:** ~4 weeks of focused work, ~2.5 kLOC including tests
**Migrations claimed:** `043_dag_node_columns.sql` (F064.1 + F064.2 combined — folded per architecture P2-A), `044_procedure_runtime_metadata.sql` (F064.4), `045_schedule_continuation.sql` (F064.5), `046_work_queue_items.sql` (F064.6). Migration 042 is reserved by F061 for a CHECK-constraint backfill — confirmed at `sql/migrations/041_subtask_hardening.sql:8-9`.

---

## 0 — Scope acknowledgement

The spec itself recommends shipping F064 across four PRs (`F064.1+.2+.3`, then `.4`, then `.5`, then `.6`). The user explicitly chose all-six-in-one-PR after reviewing that recommendation. This plan honors that choice but compensates with:

- Risk-ascending commit order so the cheap-and-safe items land first and reviewers can stop mid-stack if needed.
- Every sub-feature behind a default-off feature flag so the merge itself is risk-free.
- After-every-commit review by the local code-reviewer subagent + `@codex` on the PR thread.

If at any point during implementation the PR becomes unreviewable, the fallback is to split the rear sub-features (.5 and .6) off into a follow-up PR and ship .1–.4 first.

---

## 1 — Pre-flight: facts verified, phantom APIs flagged

### 1.1 — Verified spec claims (against branch `main @ f61c888`)

| Claim | Verified |
|---|---|
| `nous/dag/orchestrator.py` is 840 LOC, zero matches for `stall\|reconcil` | ✅ |
| `_CHECK_CMD_TIMEOUT = 10.0` at `orchestrator.py:32` | ✅ |
| `DAG_STATUS_BASE_DIR` at `orchestrator.py:33` | ✅ |
| `_effective_timeout` at `orchestrator.py:514` | ✅ |
| `schemas.py:114` — `token_budget` is the only DAG-level capacity knob | ✅ |
| `SkillManifest` field set in `parser.py:14-29` matches spec | ✅ |
| `nous/heartbeat/checks.py` is a flat module (no `work_queue.py`) | ✅ |
| Symphony §3.1 / §5.3.5 / §5.3.6 / §6.2 / §7.1 / §8.1 / §8.3 / §8.5 / §9.1 / §9.2 / §10.5 quoted verbatim | ✅ (live SPEC.md re-fetched 2026-05-19) |
| Symphony defaults: poll 30 s, max_concurrent 10, max_turns 20, stall 5 min, regex `[A-Za-z0-9._-]` | ✅ |

### 1.2 — Phantom-API table (spec said X exists; reality is Y)

| # | Spec wording | Reality on `main` | Plan response |
|---|---|---|---|
| 1 | F064.1 hook updates `last_activity_at` from "subtask streaming events" | No per-chunk callback exists; `call_streaming_aggregated` consumes chunks internally | Hook **`_tool_loop` per-iteration** in `runner.py` (per-tool-result boundary) — matches Symphony "no agent event" semantics without plumbing through both `anthropic_client` backends |
| 2 | F064.2 reads "running-by-frame-type" from orchestrator | No `count_running_by_frame_type` method on `SubtaskManager`; only `list(status=...)` exists | Use **inline `SELECT frame_type, COUNT(*) FROM heart.subtasks WHERE status='running' AND agent_id=:a GROUP BY frame_type`** in `DAGOrchestrator._dispatch_with_caps()` — no new manager method |
| 3 | F064.3 sanitizes "before every subprocess launch" | Only one subprocess launch exists (`orchestrator.py:484`) and it has **no `cwd=`** | Add `cwd=workspace_path` at line 484 + add `sanitize_segment` to `DAGNodeSpec.model_validator` (insert-time defense, not runtime) |
| 4 | F064.4 "Wire into the DAG orchestrator and subtask launcher: when a node activates a skill (via procedure_id)" | `DAGNodeSpec` has **no `procedure_id` field**; skills fire inside `recall_deep`/`learn_skill` tools, not at node launch | **Scope F064.4 to manifest-extension + persistence only** (extend `SkillManifest`, persist runtime fields on `procedures.runtime_metadata` JSONB at `to_procedure_input` time). Orchestrator consumer wiring **deferred to F064.4-v2** with an explicit `# TODO(F064.4-v2)` marker. The deferred work is the larger half (~3 days of the original ~3-day estimate); shipping the metadata half unblocks future consumers without forcing the consumer wiring into this PR |
| 5 | F064.5 "Add `continuation_turns: int = 0` to `ScheduleSpec`" | **`ScheduleSpec` does not exist** anywhere in the codebase — schedules are managed via the `Schedule` ORM and `nous/api/models.py::ScheduleCreate` (REST DTO) directly | Add `continuation_turns` (and the new `continuation_session_id`) to: (a) `Schedule` ORM, (b) `nous/api/models.py::ScheduleCreate` Pydantic class, (c) migration `043` (column-only, no new table), (d) `task_scheduler.py` fire path. The plan never references a `ScheduleSpec` name |
| 6 | F064.5 reuses "the same live coding-agent thread … via episode_id" | `runner.run_turn` takes `session_id: str` (required), no `episode_id` param. Episode-per-session is enforced by `Episode.session_id` (models.py:350) | Reuse the **session_id channel** — when `continuation_turns > 0` and prior fire's `continuation_session_id` is set, pass it as the subtask's session_id; episode reuse happens for free via the existing `session_id → episode` lookup in `runner.run_turn` |
| 7 | F064.6 "Adapter pattern via the same registry pattern as F033 search routing" | `SearchProvider` in `search_providers.py:33` is a **`typing.Protocol`**, NOT an ABC; `SearchRouter` uses a plain dict, not a registry class | Adopt the **heartbeat `BaseCheck` ABC pattern** instead (`registry.py:18`). The plan documents this divergence in the `WorkQueueAdapter` module docstring so a reviewer comparing to the spec doesn't ask "why did you ignore F033?" |

### 1.3 — Migration numbering coordination

`sql/migrations/041_subtask_hardening.sql:8-9` contains the comment:

```sql
-- CHECK constraint on final_outcome is deferred to a follow-up migration
-- (042) after pre-flag rows are backfilled.
```

That reserves migration 042 for the F061 follow-up. F064 therefore uses (post-review revision: F064.1 and F064.2 columns folded into a single migration per architecture P2-A so the ORM column never precedes its DB column):

- `043_dag_node_columns.sql` — adds `nous_system.dag_nodes.last_activity_at TIMESTAMPTZ NULL` AND `nous_system.execution_dags.max_concurrent_by_frame_type JSONB`. Both columns land in commit 1 even though `max_concurrent_by_frame_type` is consumed in commit 2 — this prevents the ORM-load-against-missing-column failure mode.
- `044_procedure_runtime_metadata.sql` — adds `heart.procedures.runtime_metadata JSONB`.
- `045_schedule_continuation.sql` — adds `heart.schedules.continuation_*` columns.
- `046_work_queue_items.sql` — creates `nous_system.work_queue_items` (agent_id + UNIQUE(agent_id, source, external_id) + indexes).

If F061's 042 lands while F064 is in flight, no renumbering is needed (043–046 stay valid).

### 1.4 — SQL hygiene checklist (from `feedback_migration_semicolons.md`)

- ✋ Never put `;` inside a `-- ...` line comment in `sql/migrations/*.sql` — the migrator splits naively on `;` and the resulting trailing fragment breaks `docker compose up`.
- ✋ Always test against a **fresh DB** (`docker compose down -v && docker compose up -d`) before opening the PR, not just against the dev DB.

---

## 2 — Settings, env-vars, feature flags

All new settings live in `nous/config.py`. All flags default **off** (or to a value that preserves today's behavior).

### 2.1 — New env vars

| Var | Default | Owner | Purpose |
|---|---|---|---|
| `NOUS_DAG_STALL_DETECTION_ENABLED` | `false` | F064.1 | Master switch. When false, stall detection is a no-op (today's behavior). |
| `NOUS_DAG_NODE_DEFAULT_STALL_TIMEOUT` | `600` | F064.1 | Seconds without `last_activity_at` update before a running node is marked failed. `0` disables per-node. |
| `NOUS_DAG_NODE_MAX_STALL_TIMEOUT` | `3600` | F064.1 | Ceiling clamp on `stall_timeout_seconds`. Mirrors `dag_node_max_timeout` (F046). |
| `NOUS_DAG_FRAME_CONCURRENCY_ENABLED` | `false` | F064.2 | Master switch for per-frame-type caps. |
| `NOUS_DAG_GLOBAL_MAX_CONCURRENT_BY_FRAME` | `{}` | F064.2 | JSON dict; operator-level override that wins over per-DAG when set (e.g. `{"debug": 1, "research": 3}`). |
| `NOUS_DAG_WORKSPACE_SAFETY_ENABLED` | `false` | F064.3 | Master switch. When false, today's path-construction behavior is preserved (no sanitization). Note: read-time containment-assert runs unconditionally (defense in depth — see §6). |
| `NOUS_DAG_WORKSPACE_ROOT` | `tempfile.gettempdir() / "nous-workspace" / "dag-status"` (platform-dependent — `/tmp/nous-workspace/dag-status` on POSIX, `%TEMP%\nous-workspace\dag-status` on Windows) | F064.3 | Resolved-absolute root that every workspace path must be inside. Default is computed at Settings init via `default_factory=lambda: Path(tempfile.gettempdir()) / "nous-workspace" / "dag-status"`; **never** hardcoded `/tmp/...` (Windows incompatibility — see conventions P1-1). |
| `NOUS_SKILL_RUNTIME_METADATA_ENABLED` | `false` | F064.4 | **Consumer-side flag only.** Parser always reads new manifest fields and `procedures.runtime_metadata` always persists them (post-review revision: silent-drop fix per silent-failure P1-1 / conventions P1-2). The flag gates only the deferred-to-v2 *consumer* behavior (orchestrator enforcement). When false, fields are stored verbatim — they bit-rot zero risk because the column is already present (no schema drift between flag positions). |
| `NOUS_SCHEDULE_CONTINUATION_ENABLED` | `false` | F064.5 | Master switch. When false, every fire creates a fresh session_id (today's behavior). |
| `NOUS_SCHEDULE_MAX_CONTINUATION_TURNS` | `50` | F064.5 | Hard ceiling on `Schedule.continuation_turns`. Prevents unbounded thread growth. |
| `NOUS_SCHEDULE_CONTINUATION_DEFAULT_PROMPT` | `"Continue. The previous run completed at {last_fired_at}. Apply the same task to fresh context."` | F064.5 | Default continuation prompt when `Schedule.continuation_prompt` is null. |
| `NOUS_WORK_QUEUE_ENABLED` | `false` | F064.6 | Master switch for work-queue ingress. |
| `NOUS_WORK_QUEUE_SOURCE` | `file_jsonl` | F064.6 | Adapter name. v1 ships `file_jsonl`; `github_issues` and `linear` ship behind their own enabled flags or as no-op stubs. |
| `NOUS_WORK_QUEUE_INTERVAL_SECONDS` | `300` | F064.6 | Polling cadence. |
| `NOUS_WORK_QUEUE_FILE_JSONL_PATH` | `""` | F064.6 | Adapter-specific config — path to JSONL file for `file_jsonl` source. |
| `NOUS_WORK_QUEUE_MAX_DAGS_PER_TICK` | `5` | F064.6 | Per-tick admission cap to avoid an unbounded backlog flooding `MAX_ACTIVE_DAGS` (currently 5 at `store.py:19`). |

Existing `dag_node_max_timeout` (default 7200 s, F046) stays unchanged.

### 2.2 — Cross-field validators (model_validator(mode='after'))

```python
# in Settings.model_validator(mode='after')

# F064.1 — stall ≤ wall-clock (only when stall detection opted in)
if self.dag_stall_detection_enabled:
    # Per architecture P2-C: also clamp defaults if max_timeout is smaller than
    # default_stall_timeout, to avoid hard-startup-failure for ops who pinned
    # NOUS_DAG_NODE_MAX_TIMEOUT below the default 3600 stall ceiling.
    if self.dag_node_default_stall_timeout > self.dag_node_default_timeout:
        raise ValueError(
            "NOUS_DAG_NODE_DEFAULT_STALL_TIMEOUT must be <= NOUS_DAG_NODE_DEFAULT_TIMEOUT "
            "(stall ≤ wall-clock invariant)"
        )
    if self.dag_node_max_stall_timeout > self.dag_node_max_timeout:
        raise ValueError(
            "NOUS_DAG_NODE_MAX_STALL_TIMEOUT must be <= NOUS_DAG_NODE_MAX_TIMEOUT"
        )

# F064.2 — every per-frame cap must be >= 1 (architecture P2-C)
for frame, cap in self.dag_global_max_concurrent_by_frame.items():
    if cap < 1:
        raise ValueError(
            f"NOUS_DAG_GLOBAL_MAX_CONCURRENT_BY_FRAME['{frame}']={cap} is invalid; "
            "values must be >= 1"
        )
```

Plus field-level constraints to fail fast at startup:

- `schedule_max_continuation_turns: int = Field(default=50, ge=1, description=...)` (architecture P2-C — `0` would silently disable continuation)
- `work_queue_interval_seconds: int = Field(default=300, ge=30, description=...)` (sub-30s cadence would hammer the queue / hit DB)
- `work_queue_max_dags_per_tick: int = Field(default=5, ge=1, le=5, description=...)` (cap matches existing `MAX_ACTIVE_DAGS` at store.py:19)

### 2.3 — pydantic-settings conventions

- Plain attribute for bools / unbounded ints: `dag_stall_detection_enabled: bool = False`
- `Field(default=..., ge=..., description=...)` for bounded ints: `dag_node_default_stall_timeout: int = Field(default=600, ge=0, description=...)`
- **No `validation_alias`** — F046 P0 commit `764f890` removed it as harmful redundant-with-`env_prefix`. (Confirmed via `feedback_subagent_workflow.md` and decision `43b02ba4`.)

---

## 3 — Commit-by-commit landing plan

Branch: `feat/F064-implementation` off `origin/main @ f61c888`.

| # | Commit | Subject | Files | Migration |
|---|---|---|---|---|
| 0 | `feat(F064): plan + settings scaffold` | Add plan doc, settings additions (all default-off), env var table updates in CLAUDE.md | `docs/plans/2026-05-19-f064-symphony-orchestration-adoptions.md`, `nous/config.py`, `CLAUDE.md` | — |
| 1 | `feat(F064.1+.2): DAG node stall detection + per-frame concurrency caps (schema)` | Single migration adds both `dag_nodes.last_activity_at` and `execution_dags.max_concurrent_by_frame_type` so the ORM column never precedes its DB column. ORM-only changes for both sub-features so a partial cherry-pick remains consistent. | 043 sql + models.py | 043 |
| 2 | `feat(F064.1): stall detection logic` | `_check_stalled_nodes` in orchestrator + per-iteration ping at top of `_tool_loop` (revised per silent-failure P1-2 / architecture P1-C — covers text-only turns) + per-launch baseline ping + tests | dag/schemas.py + dag/store.py + dag/orchestrator.py + api/runner.py + handlers/subtask_worker.py + tests | (uses 043) |
| 3 | `feat(F064.2): per-frame-type concurrency caps` | `DAGCreateRequest.max_concurrent_by_frame_type` + orchestrator dispatch gating with try/except per-node-launch (silent-failure P1-6) + inline SQL count + tests | dag/schemas.py + dag/orchestrator.py + dag/store.py + tests | (uses 043) |
| 4 | `feat(F064.3): DAG workspace safety invariants` | New `nous/dag/_workspace.py` with pure helpers + insert-time sanitize in `DAGNodeSpec.model_validator` + **unconditional** containment assert in `_read_node_result` (architecture P2-B / silent-failure backward-compat) + `cwd=` in completion-check subprocess + Windows-safe `tempfile.gettempdir()` default | dag/_workspace.py + dag/schemas.py + dag/orchestrator.py + nous/config.py + tests | — |
| 5 | `feat(F064.4): workflow-as-code skill manifest fields (manifest-only v1, consumer deferred)` | Extend `SkillManifest` with `concurrency_cap`, `timeout_override_seconds`, `hooks`, `requires_human_review`. **Always-persist** semantics on `procedures.runtime_metadata` JSONB (silent-failure P1-1 / conventions P1-2 fix). Orchestrator consumer wiring deferred to F064.4-v2. | skills/parser.py + skills/bootstrap.py + heart/schemas.py (ProcedureInput) + heart/procedures.py (persist runtime_metadata) + storage/models.py (Procedure.runtime_metadata column) + 044 sql migration + tests | 044 |
| 6 | `feat(F064.5): scheduled task Episode reuse (v1 — no LLM thread continuity)` | **Re-scoped per architecture P1-B**: this ships *Episode reuse* only. Each fire still starts a fresh LLM context (because `runner.end_conversation` pops the Conversation; persisting state is out of scope for v1). Adds `Schedule.continuation_*` columns + `ScheduleCreate` extension + `task_scheduler.py` fire-path branch with **running-subtask debounce guard** (architecture P1-A fix). | 045 sql + storage/models.py + api/models.py + handlers/task_scheduler.py + heart/schedules.py + tests | 045 |
| 7 | `feat(F064.6): work-queue ingress heartbeat check` | `nous/heartbeat/work_queue.py` (new module) with `WorkQueueCheck(BaseCheck)` and `WorkQueueAdapter` ABC + `file_jsonl` adapter impl + `work_queue_items` table + **single-transaction ingestion** + **startup reconciler** for orphan DAGs (silent-failure P1-3/4/5 + architecture P1-D fixes) + tests | 046 sql + heartbeat/work_queue.py + heartbeat/registry.py (registration) + storage/models.py + heart/work_queue.py (CRUD on items) + tests | 046 |
| 8 | `docs(F064): INDEX.md update + env var table + acceptance criteria check` | Mark F064.1–.3 and F064.6 as shipped; **F064.4 marked `🟡 v1 partial — manifest persistence only, consumer enforcement deferred to F064.4-v2`** (silent-failure P1-7). **F064.5 marked `🟡 v1 partial — Episode reuse only, no LLM thread continuity`** (architecture P1-B). Finalize CLAUDE.md env table. | docs/features/INDEX.md + docs/features/F064-symphony-orchestration-adoptions.md + CLAUDE.md | — |

(Migration numbers: `043` (F064.1+.2 columns, lands in commit 1), `044` (F064.4 procedure metadata, commit 5), `045` (F064.5 schedule columns, commit 6), `046` (F064.6 work_queue_items table, commit 7). Numbers are sequential per Postgres lex-order. If F061's `042` lands first, no renumbering needed.)

After each commit:

1. `uv run pytest tests/test_*.py -x` — full suite must pass.
2. Dispatch `pr-review-toolkit:code-reviewer` subagent on the diff. Address all P1s before push.
3. `git push origin feat/F064-implementation`.
4. `gh pr comment <PR> --body "@codex review the latest commit"` — codex feedback is treated as another reviewer; address P1s before next commit.

The PR is opened **after commit 0** so codex can review incrementally. Codex calls are non-free, so **only substantive commits trigger an explicit `@codex` ping** (commit 0 doesn't need one; commits 1–6 do; commit 7 is docs-only and skips codex).

---

## 4 — F064.1: DAG Node Stall Detection

### 4.1 — Files touched

| File | Change | Lines (approx) |
|---|---|---|
| `sql/migrations/043_dag_node_columns.sql` | NEW (shared with F064.2 per migration-folding decision §1.3 / §13 C8). Adds `nous_system.dag_nodes.last_activity_at TIMESTAMPTZ` for F064.1 AND `nous_system.execution_dags.max_concurrent_by_frame_type JSONB` for F064.2 — single migration, two columns. See §4.2 for verbatim text. | ~25 |
| `nous/storage/models.py` | Add `last_activity_at: Mapped[datetime \| None]` to `DAGNode` (insert near existing `awaiting_check_at` column at the analogous line). | +3 |
| `nous/dag/schemas.py` | Add `stall_timeout_seconds: int \| None = Field(None, ge=0, description=...)` to `DAGNodeSpec`. Cross-validator on `DAGCreateRequest` rejecting `stall > timeout` when both set. | +12 |
| `nous/dag/store.py` | `create()` honors `spec.stall_timeout_seconds` with clamp `min(spec.stall_timeout_seconds, settings.dag_node_max_stall_timeout)`. New method `touch_node_activity(node_id) -> None` doing an UPDATE only. | +30 |
| `nous/dag/orchestrator.py` | New `_check_stalled_nodes(dag)` called inside `_advance_dag` before `_propagate_failures`. Iterates `node.status == "running"`, compares `now() - last_activity_at` against effective stall timeout, marks failed with `error="stalled: no activity for {Ns}"`, lets the existing `_propagate_failures` cancel-cascade descendants. | +50 |
| `nous/api/runner.py` | In `_tool_loop` (the per-iteration boundary inside `run_turn`), after a tool result is appended to messages, if `is_subtask` and `dag_node_id` is present in the subtask metadata, call `store.touch_node_activity(dag_node_id)`. **Plumb a `dag_node_id: UUID \| None = None` kwarg through `run_turn` → `_tool_loop`.** | +25 |
| `nous/handlers/subtask_worker.py` | In `_execute_subtask`, pull `dag_node_id` out of `subtask.metadata` (already populated at orchestrator.py:672) and pass to `runner.run_turn(..., dag_node_id=dag_node_id)`. | +5 |
| `tests/test_dag_stall_detection.py` | NEW. ≥5 unit tests + 1 integration (see §4.5). | ~250 |

### 4.2 — Migration text (verbatim)

```sql
-- 043: F064.1 + F064.2 — DAG schema additions (folded per §13 C8).
-- Adds last_activity_at to dag_nodes for stall-timeout enforcement,
-- and max_concurrent_by_frame_type to execution_dags for per-frame caps.
-- Updated by runner._tool_loop on every iteration boundary inside a
-- subtask; read by orchestrator._check_stalled_nodes once per tick.
-- max_concurrent_by_frame_type is read by orchestrator._dispatch_ready_nodes.

ALTER TABLE nous_system.dag_nodes
    ADD COLUMN IF NOT EXISTS last_activity_at TIMESTAMPTZ;

ALTER TABLE nous_system.execution_dags
    ADD COLUMN IF NOT EXISTS max_concurrent_by_frame_type JSONB;

-- Index supports the orchestrator scan: WHERE status='running' AND
-- last_activity_at < (now() - stall_timeout). Partial index keeps it cheap.
CREATE INDEX IF NOT EXISTS idx_dag_nodes_last_activity_running
    ON nous_system.dag_nodes (last_activity_at)
    WHERE status = 'running';
```

### 4.3 — Activity-ping design (revised post-review: silent-failure P1-2 + architecture P1-C)

The plan **does not** plumb a callback through `call_streaming_aggregated`. That would require touching both `anthropic_client.py` backends (SDK and raw httpx) and adding a parameter the streamer doesn't natively support.

Instead, pings fire at **three points** to cover all stall classes:

1. **At node launch** (in `_launch_subtask_node` after `update_node(status="running", started_at=...)`) — establishes a baseline `last_activity_at` so a node that fails before any LLM call still has a reference timestamp.
2. **At the TOP of every `_tool_loop` iteration** (before `_call_api`) — fires once per turn for both tool-using and text-only turns. This is the change that resolves architecture P1-C: a research subtask generating a 30k-token text response with `stop_reason="end_turn"` produces zero tool calls but still pings on the iteration boundary that started the LLM call.
3. **Inside the tool-call dispatch path** (just before `await tool_dispatcher.execute(...)`) — covers the long-running bash case raised by silent-failure P1-2: a 45-minute build doesn't trip stall detection because the dispatch-time ping was recorded immediately before the long block, and the next iteration's top-of-loop ping comes after the tool returns.

All three pings are **fire-and-forget** via `asyncio.create_task(...)` wrapped in `asyncio.shield()` so an in-flight write isn't cancelled mid-iteration when `wait_for` triggers a timeout. This matches the F026 persistence pattern (`f026_persistence_enabled` in CLAUDE.md). A write failure is logged at DEBUG and does not propagate — the ping is best-effort telemetry, not a critical path.

**Stall-detection policy:** orchestrator scans `dag_nodes WHERE status='running' AND last_activity_at IS NOT NULL AND last_activity_at < now() - stall_timeout_seconds`. The `IS NOT NULL` clause means newly-created nodes (no ping yet) are NEVER flagged as stalled — they're young, not stalled. A node sits in `status='running'` with NULL `last_activity_at` only if every one of the three ping sites failed silently; in that case the wall-clock timeout (`dag_node_max_timeout`) is still the fallback. Defense in depth, no single point of silent failure.

**Why three pings, not one:**
- Top-of-iteration alone misses long-bash case → silent-failure P1-2 still applies.
- Dispatch-time alone misses text-only turn case → architecture P1-C still applies.
- Tool-result-time alone (original v1 design) misses both.
- All three combined cover both classes uniformly. Each ping is one `UPDATE` keyed by primary key — write amplification is bounded by `(turns × (1 + tool_calls_per_turn))`, which for a typical subtask is `<20 writes total`.

### 4.4 — Settings additions (already listed in §2.1, repeated here for the per-feature block)

```python
# F064.1
dag_stall_detection_enabled: bool = False
dag_node_default_stall_timeout: int = Field(default=600, ge=0, description=...)
dag_node_max_stall_timeout: int = Field(default=3600, ge=1, description=...)
```

Cross-validator (already noted in §2.2) enforces stall ≤ wall-clock.

### 4.5 — Tests

| Test | Type | What it verifies |
|---|---|---|
| `test_stall_timeout_none_means_disabled` | unit | `stall_timeout_seconds=None` → orchestrator skips this node, no `last_activity_at` consultation |
| `test_stall_timeout_zero_means_disabled` | unit | `stall_timeout_seconds=0` → same as None; matches Symphony `stall_timeout_ms <= 0` semantics |
| `test_node_marked_failed_when_no_activity_for_timeout` | unit | Mock `datetime.now`, set `last_activity_at` 31 s ago, run `_check_stalled_nodes` with `stall_timeout_seconds=30`, assert node status=`failed` + error contains `"stalled"` |
| `test_node_kept_running_when_activity_recent` | unit | `last_activity_at` 5 s ago, `stall_timeout_seconds=30` → node still `running` |
| `test_stall_cascades_via_existing_propagation` | unit | Stalled node + downstream `dependency` edge → downstream node ends up `blocked` |
| `test_stall_cascades_via_cancel_cascade_edge` | unit | Stalled node + downstream `cancel_cascade` edge → downstream cancelled |
| `test_settings_validator_rejects_stall_above_walltime` | unit | `dag_node_default_stall_timeout=900, dag_node_default_timeout=600` raises ValueError |
| `test_activity_ping_updates_last_activity_at` | integration | Real DB, real `_tool_loop` invocation with a mock that calls a tool once; assert `dag_nodes.last_activity_at` is updated within ±5 s of now |
| `test_text_only_turn_pings_at_iteration_start` | unit | Mock LLM returns `stop_reason="end_turn"` with no tool_use blocks; assert ping fired (top-of-iteration site) — covers architecture P1-C |
| `test_long_tool_call_pings_at_dispatch` | unit | Tool takes 30s to return; ping recorded BEFORE the await; assert stall_timeout=10s does NOT fire mid-call — covers silent-failure P1-2 |
| `test_null_last_activity_at_never_flagged` | unit | Node with `status='running'`, `last_activity_at=NULL`, 6h since `started_at` → NOT flagged stalled (wall-clock timeout is the fallback) |
| `test_ping_write_failure_is_silent` | unit | Mock `store.touch_node_activity` raises; assert `_tool_loop` does not propagate the exception |
| `test_stall_during_sync_does_not_override_completion` | unit | Subtask completes between `_sync_node_statuses` and `_check_stalled_nodes` — assert final status is `completed` not `failed` (silent-failure P2-7) |

### 4.6 — What can fail in F064.1

| Failure mode | Mitigation |
|---|---|
| Activity ping writes flood the DB on a tool-heavy run | Use `update().where()` not ORM session.commit; one statement per ping; the index is partial so write amplification is small |
| Stall scan races a finishing node (node completes mid-scan) | `_check_stalled_nodes` only fires AFTER `_sync_node_statuses` — by the time we look at "running" nodes, the in-memory state reflects any subtask that just completed |
| Pre-existing rows have `last_activity_at=NULL` | Stall scan treats NULL as "newer than any timeout" (no-op); next ping populates it |
| Settings cross-validator fires on a fresh-DB startup with default values | Defaults are mutually consistent (600 ≤ 600, 3600 ≤ 7200) so the validator passes |
| Subtask is created outside a DAG and has no `dag_node_id` | `runner._tool_loop` accepts `dag_node_id=None` and short-circuits the ping; the existing non-DAG subtask path is unaffected |

---

## 5 — F064.2: Per-Frame-Type Concurrency Caps

### 5.1 — Files touched

| File | Change | Lines |
|---|---|---|
| `nous/dag/schemas.py` | `DAGCreateRequest.max_concurrent_by_frame_type: dict[str, int] \| None = None`. Per-key validator: values must be `>= 1`. | +10 |
| `nous/storage/models.py` | `ExecutionDAG.max_concurrent_by_frame_type: Mapped[dict \| None] = mapped_column(JSONB)`. The DB column ships in **migration 043** (folded with F064.1 — see §1.3 and §13 C8). No new migration in this commit; the column is already present when F064.2 code lands in commit 3. | +3 |
| `nous/dag/store.py` | `create()` persists the field. New helper `count_running_subtasks_by_frame_type(agent_id) -> dict[str, int]` doing a single grouped SELECT on `heart.subtasks`. | +25 |
| `nous/dag/orchestrator.py` | `_advance_dag` calls `await self._dispatch_ready_nodes(dag, ready_nodes)` instead of looping `_launch_node` directly. New `_dispatch_ready_nodes` consults running counts + per-frame-type cap + global override. Nodes that lose a slot stay `ready` (not yet launched) for the next tick. | +60 |
| `nous/config.py` | `dag_frame_concurrency_enabled: bool`, `dag_global_max_concurrent_by_frame: dict[str, int] = {}` (JSON-parsed via `field_validator`). | +15 |
| `tests/test_dag_concurrency_caps.py` | NEW. ≥5 unit tests + 1 integration. | ~300 |

### 5.2 — Concrete dispatch logic (revised post-review: silent-failure P1-6 / conventions P2-6)

```python
async def _dispatch_ready_nodes(self, dag, ready_nodes):
    if not self._settings.dag_frame_concurrency_enabled:
        # Backward-compatible path — preserve today's behavior.
        # Even in the legacy path we wrap per-node launch in try/except so a
        # single _launch_node failure doesn't abandon the rest of the wave.
        for node in ready_nodes:
            try:
                await self._launch_node(node, dag)
            except Exception:
                logger.exception("Failed to launch node %s in DAG %s", node.name, dag.id)
        return

    # Effective caps: env override > per-DAG > unlimited.
    caps = self._effective_frame_caps(dag)
    if not caps:
        for node in ready_nodes:
            try:
                await self._launch_node(node, dag)
            except Exception:
                logger.exception("Failed to launch node %s in DAG %s", node.name, dag.id)
        return

    running_by_frame = await self._store.count_running_subtasks_by_frame_type()
    for node in ready_nodes:
        frame = node.frame_type or "_default"
        cap = caps.get(frame)
        if cap is not None and running_by_frame.get(frame, 0) >= cap:
            # Defer to next tick. Node stays in 'ready' status.
            continue
        try:
            await self._launch_node(node, dag)
        except Exception:
            logger.exception("Failed to launch node %s in DAG %s", node.name, dag.id)
            # Do NOT increment the accumulator on failure — the slot was not used.
            continue
        running_by_frame[frame] = running_by_frame.get(frame, 0) + 1  # ← in-memory accumulator so we don't over-dispatch within a single tick
```

Two revisions from v1:

1. **Per-node try/except** around `_launch_node` (in BOTH the disabled and enabled paths) — addresses silent-failure P1-6 / conventions P2-6. Before this, an exception in `_launch_node` would propagate to `tick()`, get logged, and abandon all remaining ready_nodes in the same DAG mid-wave. Now, one failure doesn't poison the wave.
2. **Accumulator increment only on success** — defensive. If `_launch_node` raises, the slot was not actually consumed, so we don't fake the count.

`_default` frame contract: a node with `frame_type=None` is bucketed as `_default`. If operators want None to be uncapped, they leave `_default` out of the cap dict (the `cap is not None` check skips uncapped buckets). The CLAUDE.md env-table description for `NOUS_DAG_GLOBAL_MAX_CONCURRENT_BY_FRAME` documents this explicitly.

### 5.3 — Acceptance criterion mapping (from spec)

> A DAG with `max_concurrent_by_frame_type={"debug": 1, "research": 3}` and 1 debug + 4 research-frame nodes runs the debug node serially with the research nodes (3 research nodes run concurrently with the debug node; the 4th research node waits for an available slot). Unset = today's behavior (regression test).

Mapped to `test_concurrency_caps_acceptance.py::test_acceptance_scenario`. The test sets up 5 nodes in wave 0 (1 debug + 4 research), runs one tick, asserts `running` count is 4 (1 debug + 3 research). Runs second tick, asserts `running` count is still 4 (one research completes, the 4th launches).

### 5.4 — Settings additions

```python
dag_frame_concurrency_enabled: bool = False
dag_global_max_concurrent_by_frame: dict[str, int] = Field(default_factory=dict, description=...)
```

### 5.5 — Tests

| Test | Type | What it verifies |
|---|---|---|
| `test_disabled_flag_preserves_today_behavior` | unit | `dag_frame_concurrency_enabled=False` → all ready nodes launched in the same tick (regression) |
| `test_per_dag_caps_honored` | unit | Cap of 1 on `debug` → only one debug launches, second deferred |
| `test_env_override_wins_over_per_dag` | unit | DAG sets `{"debug": 3}`, env sets `{"debug": 1}` → effective cap is 1 |
| `test_unmapped_frame_type_is_unlimited` | unit | Cap dict has no `analysis` key → analysis nodes run uncapped |
| `test_in_memory_accumulator_prevents_overdispatch` | unit | 5 nodes of same frame, cap 2 → exactly 2 launched per tick |
| `test_concurrency_caps_acceptance` | integration | Full acceptance scenario from spec §F064.2 |

### 5.6 — What can fail

| Failure | Mitigation |
|---|---|
| `count_running_subtasks_by_frame_type` is stale w.r.t. subtasks created by NON-DAG sources (e.g. inline `spawn_task`) | Acceptable — F064.2 is DAG-scoped. The cap is a soft cap on **what THIS orchestrator dispatches per tick**, not a global throttle |
| Per-frame-type cap blocks all DAG progress (e.g. cap of 0 on every frame) | Pydantic validator rejects values `< 1`; cap of `0` is not expressible (matches spec's "positive integer" wording) |
| JSON env var parse failure | `Field(default_factory=dict)` + pydantic-settings handles the JSON parsing; bad JSON raises `SettingsError` at startup (fail-fast) |

---

## 6 — F064.3: DAG Workspace Safety Invariants

### 6.1 — Files touched

| File | Change | Lines |
|---|---|---|
| `nous/dag/_workspace.py` | NEW module with 3 pure functions: `sanitize_segment(s: str) -> str` (regex `[^A-Za-z0-9._-]` → `_`, also reject `..`, empty, leading-dot-only), `compute_workspace_path(dag_id: UUID, node_name: str, root: Path) -> Path`, `assert_inside_root(path: Path, root: Path) -> None` (raises if `path.resolve()` is not relative to `root.resolve()`). | ~80 |
| `nous/dag/schemas.py` | `DAGNodeSpec.model_validator(mode='after')` rejects names whose `sanitize_segment(name) != name` when `NOUS_DAG_WORKSPACE_SAFETY_ENABLED=true`. (Off-by-default preserves backward-compat for any existing rows / clients.) | +15 |
| `nous/dag/orchestrator.py` | `_read_node_result` (line 524) replaces raw `DAG_STATUS_BASE_DIR / dag.id.hex[:8] / node.name` with `compute_workspace_path(dag.id, node.name, settings.dag_workspace_root)` + **unconditional** `assert_inside_root` call (architecture P2-B — defense in depth: read-time check fires regardless of the feature flag, since path traversal is a security boundary not a feature). `_run_completion_check` (line 484) ensures `workspace_path.mkdir(parents=True, exist_ok=True)` then passes `cwd=workspace_path` to `create_subprocess_shell` (silent-failure P3-3). | +28 |
| `nous/config.py` | `dag_workspace_safety_enabled: bool = False`, `dag_workspace_root: Path = Field(default_factory=lambda: Path(tempfile.gettempdir()) / "nous-workspace" / "dag-status")`. **Critical**: never hardcode `Path("/tmp/...")` — that breaks Windows. (Conventions P1-1) | +6 |
| `tests/test_dag_workspace_safety.py` | NEW. ≥5 unit tests + 1 integration. | ~200 |

### 6.2 — Sanitize / containment behavior

```python
# nous/dag/_workspace.py
_UNSAFE_RE = re.compile(r"[^A-Za-z0-9._-]")

def sanitize_segment(s: str) -> str:
    if not s or s in (".", ".."):
        raise ValueError(f"invalid path segment: {s!r}")
    sanitized = _UNSAFE_RE.sub("_", s)
    if sanitized != s:
        raise ValueError(
            f"path segment contains characters outside [A-Za-z0-9._-]: {s!r} "
            f"(would sanitize to {sanitized!r}). Reject at insert time."
        )
    return s
```

Note the **reject-at-insert** posture rather than silent-rewrite. Symphony §9.1 silently rewrites because Linear ticket identifiers are caller-controlled and unsanitized. Nous DAG node names are caller-controlled inside a single agent's own DAGs, so we have the option to be stricter — reject is safer and matches the existing pydantic-validator pattern in `DAGCreateRequest`.

### 6.3 — Acceptance criterion mapping

> Attempting to insert a DAG node with `name="../escape"` or a path that resolves outside `DAG_STATUS_BASE_DIR` is rejected at insert time.

Mapped to `test_node_name_with_dotdot_rejected`. The `cwd` assertion is harder to test directly (asserting on subprocess argv); covered by `test_completion_check_cwd_is_workspace`.

### 6.4 — Tests

| Test | Type | What it verifies |
|---|---|---|
| `test_safe_name_passes` | unit | `name="research-step-1"` → no error |
| `test_dotdot_rejected` | unit | `name="../escape"` → ValueError |
| `test_dot_rejected` | unit | `name="."` → ValueError |
| `test_empty_rejected` | unit | `name=""` → ValueError (also fails existing `min_length=1`) |
| `test_unicode_rejected` | unit | `name="α-step"` → ValueError (α is not in `[A-Za-z0-9._-]`) |
| `test_assert_inside_root_passes` | unit | `compute_workspace_path("abc12345", "step1", root)` is inside root |
| `test_assert_inside_root_rejects_symlink_escape` | unit | If `step1` is itself a symlink to `/etc/shadow`, `resolve()` returns the target; assert rejection |
| `test_completion_check_cwd_is_workspace` | integration | Spawn a real completion-check that prints `$PWD`; assert stdout equals the workspace path |

### 6.5 — What can fail

| Failure | Mitigation |
|---|---|
| Existing DAG rows have names like `step-with spaces` | **Insert-time sanitize is flag-gated** so the rule doesn't apply to pre-flag rows. **Read-time `assert_inside_root` runs unconditionally** (security boundary) — but the read function `compute_workspace_path` uses a *transformation* rather than *rejection* for legacy names: an unsafe `node.name` gets sanitized via `_UNSAFE_RE.sub("_", name)` at read time, producing a safe-path equivalent. So a legacy row with `name="step with spaces"` reads from `workspace_root/dagid/step_with_spaces/result`, not the original ambiguous path. (Resolves silent-failure P2-5 / architecture P2-B back-compat tension) |
| `compute_workspace_path` errors when `dag.id` is a UUID (not a string) | Use `dag.id.hex[:8]` (hex-only, sanitization-safe) — matches existing line 524 pattern |
| Symlink escape (`workspace_root/foo` is a symlink to `/etc`) | `path.resolve()` follows symlinks; `assert_inside_root` compares resolved paths. Test covers this |
| Windows path separators leak in (project runs on Windows per session env) | `Path` normalizes separators; tests run on Windows but the workspace lives under `tempfile.gettempdir()` which is also Path-normalized |
| `cwd=workspace_path` raises FileNotFoundError when directory doesn't exist | `_run_completion_check` does `workspace_path.mkdir(parents=True, exist_ok=True)` before subprocess launch (silent-failure P3-3) |

---

## 7 — F064.4: Workflow-as-Code Skill Manifest Fields (manifest-only v1)

### 7.1 — Scope decision (revised post-review: silent-failure P1-1 / conventions P1-2 / silent-failure P1-7)

The spec proposes wiring runtime metadata into the orchestrator + subtask launcher. Verified phantom (per §1.2 row 4): `DAGNodeSpec` has no `procedure_id`, no skill-resolution at node launch. F064.4-v1 ships the **manifest-extension half only**:

- `SkillManifest` learns new fields.
- **New fields are persisted unconditionally** on `procedures.runtime_metadata` (JSONB) — the silent-drop pattern (skill author declares `concurrency_cap: 1`, gets a success response, no warning, field silently lost) is the canonical F032/F033/F060/F061 family of bugs and is the single biggest correctness risk in F064.4. The fix: always parse, always persist. The `NOUS_SKILL_RUNTIME_METADATA_ENABLED` flag gates only the (deferred) *consumer* behavior.
- A `# TODO(F064.4-v2)` marker in `_launch_node` documents the deferred consumer wiring.
- **`docs/features/INDEX.md` marks F064.4 as `🟡 v1 partial — manifest persistence only, consumer enforcement deferred to F064.4-v2`** — silent-failure P1-7 requires this honest labelling so a future reader doesn't assume `concurrency_cap` is enforced just because the manifest accepts it.

The deferred work (consumer wiring) is the larger half (~3 days of original 3-day estimate). v2 will live in a follow-up PR after this PR merges.

### 7.2 — Files touched

| File | Change | Lines |
|---|---|---|
| `nous/skills/parser.py` | Extend `SkillManifest` dataclass with `concurrency_cap: int \| None = None`, `timeout_override_seconds: int \| None = None`, `hooks: dict[str, str] = field(default_factory=dict)`, `requires_human_review: bool = False`. Parse from frontmatter; values default to today's behavior. | +20 |
| `nous/skills/parser.py::to_procedure_input` | **Always** embed new fields into `ProcedureInput.runtime_metadata` (new field) — no flag gating at write time (post-review fix per silent-failure P1-1). The persisted shape is `{"concurrency_cap": int\|None, "timeout_override_seconds": int\|None, "hooks": dict, "requires_human_review": bool, "schema_version": 1}`. The `schema_version` key lets v2 detect drift. | +15 |
| `nous/heart/schemas.py::ProcedureInput` | Add `runtime_metadata: dict \| None = None`. | +1 |
| `nous/storage/models.py::Procedure` | Add `runtime_metadata: Mapped[dict \| None] = mapped_column(JSONB)`. | +3 |
| `nous/heart/procedures.py` | Persist `runtime_metadata` in `store()` method. | +5 |
| `sql/migrations/044_procedure_runtime_metadata.sql` | NEW. `ALTER TABLE heart.procedures ADD COLUMN IF NOT EXISTS runtime_metadata JSONB`. | ~10 |
| `nous/config.py` | `skill_runtime_metadata_enabled: bool = False`. | +2 |
| `nous/dag/orchestrator.py::_launch_subtask_node` | `# TODO(F064.4-v2): apply procedure.runtime_metadata over node defaults (concurrency_cap, timeout_override_seconds, hooks, requires_human_review). Requires DAGNodeSpec.procedure_id wiring.` — comment only. No behavior change. | +5 |
| `tests/test_skill_runtime_metadata.py` | NEW. ≥5 unit tests. | ~150 |

### 7.3 — Acceptance criterion mapping (scope-adjusted)

The spec's acceptance criterion is:
> A skill with `concurrency_cap: 1` cannot be activated by two simultaneous DAG nodes; the second waits or fails per a configurable mode (default: wait).

v1 ships **only the manifest + persistence**, not the orchestrator gate. The v1 acceptance criterion is therefore amended to:

> A skill manifest declaring `concurrency_cap: 1` parses successfully, persists to `procedures.runtime_metadata = {"concurrency_cap": 1, ...}`, and round-trips through `learn_skill → recall_deep` with the field intact.

This is documented in commit 4's message and in the PR description so reviewers understand the deferred wiring is intentional.

### 7.4 — Tests

| Test | Type | What it verifies |
|---|---|---|
| `test_manifest_parses_new_fields` | unit | Frontmatter with all four new fields → all populated on `SkillManifest` |
| `test_manifest_missing_new_fields_uses_defaults` | unit | Frontmatter with none of the new fields → defaults (None/None/{}/False) |
| `test_manifest_rejects_invalid_concurrency_cap` | unit | `concurrency_cap: 0` or `concurrency_cap: -1` → ValueError at parse time |
| `test_to_procedure_input_embeds_runtime_metadata_unconditionally` | unit | Manifest with `concurrency_cap: 1` → ProcedureInput.runtime_metadata has the key REGARDLESS of `skill_runtime_metadata_enabled` flag (post-review silent-drop fix) |
| `test_runtime_metadata_persisted_when_consumer_flag_off` | integration | Flag OFF + `learn_skill` with `concurrency_cap: 1` in frontmatter → DB row at `heart.procedures.runtime_metadata` contains `{"concurrency_cap": 1, ..., "schema_version": 1}`. Asserts the canonical silent-drop class (C1) is closed end-to-end, not just at the parser layer |
| `test_schema_version_present` | unit | All persisted `runtime_metadata` dicts carry `schema_version: 1` so v2 can detect drift |
| `test_persistence_roundtrip` | integration | `learn_skill` → DB → `procedure.runtime_metadata` matches input |

### 7.5 — What can fail

| Failure | Mitigation |
|---|---|
| Existing procedures have `runtime_metadata=NULL` and consumer wiring assumes dict | v1 has no consumer; v2 must default-coalesce NULL → {} |
| Frontmatter `hooks: {before_run: "ls"}` parses as wrong type (the project's parser is minimal) | Add a parsed-type assertion in `SkillManifest.__post_init__` — `hooks` must be `dict[str, str]` or `{}`. Tests cover this |
| Hook security (Symphony's shell-snippet hooks are a real attack surface) | v1 **does not execute hooks**; they're persisted as metadata only. v2 must address shell vs Python (open question O1 in spec) |

---

## 8 — F064.5: Scheduled Task Episode Reuse (v1 partial — re-scoped)

> ### Scope correction (architecture P1-B)
>
> The spec proposes "same live coding-agent thread, continuation guidance only." Verified phantom: `nous/api/runner.py:552` (`end_conversation`) calls `self._conversations.pop(session_id, None)` which removes the in-memory `Conversation` object entirely. A subsequent subtask at the same `session_id` instantiates a fresh `Conversation` with empty `messages`. The LLM sees no prior thread, cannot send only "continuation guidance" — it must re-read the full task prompt every fire.
>
> **F064.5-v1 ships Episode reuse only.** Each fire still starts a fresh LLM context but appends to the **same `Episode.id`** (because `Episode.session_id` is the partitioning key and we're reusing it). This is enough to:
> - Carry calibration / outcome signals across fires (rubric evolution sees them as one episode)
> - Make the dashboard's per-schedule activity view coherent
> - Lay the persistence column foundation for F064.5-v2 (which will add explicit state serialization: compacted transcript → `working_memory` → re-inject)
>
> The acceptance criterion is amended accordingly. **`docs/features/INDEX.md` marks F064.5 as `🟡 v1 partial — Episode reuse only, no LLM thread continuity`.**

### 8.1 — Files touched

| File | Change | Lines |
|---|---|---|
| `sql/migrations/045_schedule_continuation.sql` | NEW. `ALTER TABLE heart.schedules ADD COLUMN IF NOT EXISTS continuation_turns INT NOT NULL DEFAULT 0`, `ADD COLUMN IF NOT EXISTS continuation_session_id TEXT`, `ADD COLUMN IF NOT EXISTS continuation_prompt TEXT`, `ADD COLUMN IF NOT EXISTS continuation_count INT NOT NULL DEFAULT 0`. (`max_concurrent_by_frame_type` on `execution_dags` lives in migration 043, not this one — see §1.3.) | ~25 |
| `nous/storage/models.py::Schedule` | Add the four new columns. | +8 |
| `nous/api/models.py::ScheduleCreate` | Add `continuation_turns: int = 0`, `continuation_prompt: str \| None = None`. Pydantic validator caps `continuation_turns` at `settings.schedule_max_continuation_turns` at construction time. | +10 |
| `nous/heart/schedules.py` | `create()` honors new fields. New helpers `bump_continuation_count(id)` and `reset_continuation(id)`. | +25 |
| `nous/handlers/task_scheduler.py` | Fire-path branch + **running-subtask debounce guard** (architecture P1-A): before creating a subtask, query `heart.subtasks` for an active row matching `metadata->>'schedule_id' = schedule.id AND status IN ('pending', 'running')`. If one exists, skip fire (the previous fire is still working) and log at INFO. Then the continuation branch: if `continuation_turns > 0` and `continuation_session_id` is not null AND `continuation_count < continuation_turns`, reuse session_id and bump count **only after the subtask is successfully enqueued** (so a failed enqueue doesn't bump count). Otherwise create a fresh session_id (`schedule-{id.hex[:8]}-{count}`), set `continuation_session_id`, reset count to 1. On `continuation_count >= continuation_turns`, reset on next fire. | +65 |
| `nous/handlers/subtask_worker.py::_execute_subtask` | If `subtask.metadata.get("session_id")` is set, use it instead of `f"subtask-{subtask.id.hex[:8]}"`. (Already has `metadata` plumbed — F064.1 adds `dag_node_id` to the same dict; this adds `session_id`.) | +5 |
| `nous/heart/subtasks.py::create` | Optional `session_id` parameter; stored on `subtask.metadata`. | +5 |
| `nous/config.py` | `schedule_continuation_enabled: bool`, `schedule_max_continuation_turns: int`, `schedule_continuation_default_prompt: str`. | +10 |
| `tests/test_schedule_continuation.py` | NEW. ≥5 unit tests + 1 integration. | ~250 |

### 8.2 — Continuation flow (re-scoped: Episode reuse, no LLM thread reuse)

```
fire #1: continuation_session_id is NULL → mint "schedule-{id.hex[:8]}-1", set continuation_session_id, count=1
fire #2 (running-subtask check passes): count=1 < continuation_turns=5 → reuse continuation_session_id, count=2
...
fire #5: count=4 < 5 → reuse, count=5
fire #6: count=5 == continuation_turns=5 → reset (NULL out continuation_session_id, count=0), next fire treats this as fire #1 (cold start)
```

**What "reuse" means in v1:** the subtask sets its session_id to `Schedule.continuation_session_id`, which means `runner.run_turn` looks up the existing `Episode` (via `Episode.session_id`) and APPENDS to it. The LLM context itself is fresh — there is no "send only continuation guidance" semantics; each fire receives the full task description and runs to completion independently.

**Prompt semantics:** v1 sends the same task prompt on every fire. The `Schedule.continuation_prompt` column is added by the migration but **not consumed by the v1 code path** — it's a forward-compat reservation for F064.5-v2 (when explicit transcript-state re-injection is implemented). The `NOUS_SCHEDULE_CONTINUATION_DEFAULT_PROMPT` env var is similarly reserved, not consumed in v1.

**Count semantics:** continuation_count tracks **dispatches**, not successes (silent-failure P2-1). A failed fire still consumed its slot. This is simpler than success-counting and matches the safety-belt intent of the cap.

**Deleted-episode handling (silent-failure P2-2):** if compaction collapses or deletes the Episode that `continuation_session_id` pointed at, the next fire's `runner.run_turn` will create a fresh Episode (its existing behavior). The continuation_count is unaffected because it tracks dispatches not Episode identity. Document this in a code comment, no special-case handling needed.

**Race on schedule update (silent-failure P3-2):** two concurrent PATCHes to `Schedule.continuation_turns` use last-write-wins (Postgres default). Documented but not guarded — multi-operator schedule edits are not a known use case.

### 8.3 — Token budget concern

A 50-turn reused thread accumulates tool calls + responses. Existing compaction (`NOUS_COMPACTION_THRESHOLD`, F036) kicks in automatically — no F064.5-specific budget logic needed. The hard cap on `continuation_turns` (default 50) is the safety belt; if a user sets it to 1000, compaction handles the bloat.

### 8.4 — Acceptance criterion mapping (revised per architecture P1-B)

Spec original:
> A scheduled task with `continuation_turns: 5` reuses the same `episode_id` thread across up to 5 consecutive fires, then resets on the 6th. The first fire uses the full prompt; subsequent fires use the continuation prompt.

**v1 amended (post-architecture-review):**
> A scheduled task with `continuation_turns: 5` shares the same `Episode.id` across up to 5 consecutive fires, then resets on the 6th. **Every fire (including fires 2–5) sends the full task prompt** — LLM thread continuity is deferred to F064.5-v2. The `continuation_prompt` column is reserved but not consumed in v1.

Mapped to `test_continuation_acceptance_v1` integration test that fires the schedule 6 times and asserts:
- Fires 1–5 share the same `Episode.id`
- Fire 6 creates a new `Episode.id`
- Every fire's subtask `task` field equals the original `Schedule.task` (no continuation-prompt substitution in v1)

### 8.5 — Tests

| Test | Type | What it verifies |
|---|---|---|
| `test_continuation_zero_means_disabled` | unit | `continuation_turns=0` → every fire creates a fresh session_id (regression) |
| `test_continuation_session_persists_across_fires` | unit | Two fires with `continuation_turns=3` → same `continuation_session_id` |
| `test_continuation_resets_at_cap` | unit | 4 fires with `continuation_turns=3` → fire 4 resets |
| `test_running_subtask_debounces_fire` | unit | Active subtask exists for schedule → next fire is skipped (architecture P1-A) |
| `test_failed_fire_still_bumps_count` | unit | Subtask fails mid-execution → continuation_count still incremented (silent-failure P2-1 — dispatch-counted semantics) |
| `test_deleted_episode_creates_fresh_one` | unit | continuation_session_id refers to a deleted episode → next fire creates a fresh episode without raising (silent-failure P2-2) |
| `test_settings_cap_overrides_per_schedule` | unit | Per-schedule `continuation_turns=100`, settings cap `50` → effective cap is 50 |
| `test_continuation_acceptance_v1` | integration | v1 amended acceptance scenario from §8.4 (Episode-id sharing only, full prompt every fire) |

### 8.6 — What can fail

| Failure | Mitigation |
|---|---|
| Schedule fires while previous fire's subtask is still running | **Architecture P1-A correction:** the v1 plan stated debounce comes "for free" via `last_fired_at + interval_seconds`. Verified false — `task_scheduler._fire_due_tasks` has no running-subtask check. v1 plan now adds an explicit query in `task_scheduler.py` before subtask creation (see §8.1 row 4) |
| Session_id collision across multiple agents on shared DB | Format is `schedule-{schedule_id.hex[:8]}-{n}` and `Schedule.agent_id` partitions; collision requires same agent + same schedule, which is the desired reuse case |
| Episode grows unboundedly past compaction threshold | F036 compaction triggers automatically at `NOUS_COMPACTION_THRESHOLD` tokens; verified working in F049 |
| Deleted/compacted Episode pointed to by `continuation_session_id` | `runner.run_turn` creates a fresh Episode for any unknown session_id — no exception, no special-case path needed. v1 documents this in a code comment (silent-failure P2-2) |

---

## 9 — F064.6: Work-Queue Ingress

### 9.1 — Files touched

| File | Change | Lines |
|---|---|---|
| `sql/migrations/046_work_queue_items.sql` | NEW. `CREATE TABLE IF NOT EXISTS nous_system.work_queue_items` with `id UUID PRIMARY KEY DEFAULT gen_random_uuid()`, `agent_id TEXT NOT NULL`, `source TEXT NOT NULL`, `external_id TEXT NOT NULL`, `created_at TIMESTAMPTZ NOT NULL DEFAULT now()`, `dispatched_at TIMESTAMPTZ NULL` (nullable on purpose — sentinel for "claimed but not yet linked to a DAG"; the reconciler at §9.3 queries `WHERE dispatched_at IS NULL AND created_at < now() - interval '5 min'`), `dag_id UUID NULL`, `terminal_state TEXT NULL`, `payload JSONB NULL`, `UNIQUE(agent_id, source, external_id)`, partial index on `(agent_id, source)` where `dispatched_at IS NULL` for the reconciler scan, plus index on `(dag_id) WHERE dag_id IS NOT NULL` for the terminal-state cancel path. | ~50 |
| `nous/storage/models.py` | New `WorkQueueItem` ORM class. | +30 |
| `nous/heart/work_queue.py` | NEW. `WorkQueueItemManager(agent_id=...)` with CRUD: `claim_for_dispatch(source, external_id, payload) -> WorkQueueItem \| None` (atomic upsert returning the row only if it's newly claimed AND `dispatched_at IS NULL`), `mark_dispatched(id, dag_id, session)` (called inside the SAME session as the `dag_create` — see §9.3), `mark_terminal(id, state)`, `list_undispatched(source, older_than: timedelta) -> list[WorkQueueItem]` (reconciler helper). Scoped by `agent_id`. | ~120 |
| `nous/heartbeat/work_queue.py` | NEW. `WorkQueueCheck(BaseCheck)` and `WorkQueueAdapter` (ABC). `FileJsonlAdapter(WorkQueueAdapter)` impl (smallest-useful, zero external deps). Stub `GithubIssuesAdapter` and `LinearAdapter` classes that raise `NotImplementedError` until ENABLED flags are added. | ~250 |
| `nous/heartbeat/registry.py` | If `settings.work_queue_enabled`, register `WorkQueueCheck` on startup. | +10 |
| `nous/api/tools.py` | (Optional, defer to v2 if scope blows up) Expose `work_queue_items_list` tool for the agent to inspect what's been ingested. | — (deferred) |
| `nous/config.py` | All `work_queue_*` settings from §2.1. | +25 |
| `tests/test_work_queue.py` | NEW. ≥6 unit tests + 1 integration. | ~350 |

### 9.2 — Adapter pattern (ABC, not Protocol)

```python
# nous/heartbeat/work_queue.py
from abc import ABC, abstractmethod
from dataclasses import dataclass

@dataclass
class WorkItem:
    external_id: str       # adapter-defined unique identifier (e.g. Linear issue ID, GH issue number, JSONL line hash)
    title: str             # human-readable title
    body: str              # task body / description for the DAG
    state: str             # adapter's native state name
    terminal: bool         # True if state is terminal
    payload: dict          # raw adapter payload (for audit / debugging)


class WorkQueueAdapter(ABC):
    """Base class for work-queue ingress adapters.

    Note: this is an ABC, not a typing.Protocol. The plan documents why this
    diverges from F033's SearchProvider (which IS a Protocol) — F033 is
    structural, F064.6 needs registration semantics consistent with
    nous/heartbeat/registry.py::BaseCheck.
    """

    @property
    @abstractmethod
    def source_name(self) -> str: ...

    @abstractmethod
    async def list_active(self) -> list[WorkItem]: ...

    @abstractmethod
    async def get_state(self, external_id: str) -> str | None: ...
```

The `FileJsonlAdapter` reads a JSONL file each tick, returns each line as a `WorkItem`, and considers an item "terminal" when a line has `{"terminal": true}`. This is the smallest-useful implementation for both demos and operator-defined task queues.

`GithubIssuesAdapter` and `LinearAdapter` are stubs in v1 — they exist so the registry pattern is exercised, but their `list_active` raises `NotImplementedError("F064.6-v2")`. Adding them is a follow-up PR.

### 9.3 — Reconciliation flow (revised: silent-failure P1-3/4/5 + architecture P1-D)

The v1 sequential `upsert → dag_create → mark_dispatched` had three correctness holes (all caught by reviewers):
- `upsert_seen` returning True on `INSERT … ON CONFLICT DO NOTHING` is implementation-dependent (silent-failure P1-3)
- `mark_dispatched` failing after `dag_create` succeeded creates orphan DAGs (silent-failure P1-4 / architecture P1-D)
- Restart mid-running DAG could re-dispatch via the orphan path (silent-failure P1-5)

**v1.1 design: single-transaction claim + cross-tick reconciler.**

```python
# Per heartbeat tick:
items = await adapter.list_active()
dispatched_this_tick = 0
for item in items[: self._settings.work_queue_max_dags_per_tick]:
    if item.terminal:
        await self._handle_terminal(item)
        continue

    # Atomic claim: upsert returns the row only if it was newly inserted with
    # dispatched_at = NULL (or if a prior partial-commit left it NULL > N seconds
    # ago — captured by the reconciler call below).
    claimed = await self._mgr.claim_for_dispatch(
        source=self._adapter.source_name,
        external_id=item.external_id,
        payload=item.payload,
    )
    if claimed is None:
        continue  # already dispatched, or someone else just claimed it

    try:
        # IMPORTANT: dag_create + mark_dispatched run in the SAME session.
        # If dag_create succeeds and mark_dispatched fails, the entire
        # transaction rolls back and the claim is released for retry.
        async with self._db.session() as session:
            dag = await self._dag_store.create_in_session(
                session,
                DAGCreateRequest(
                    name=f"work_queue:{item.external_id}",
                    source="heartbeat",
                    nodes=[...],
                    edges=[...],
                ),
            )
            await self._mgr.mark_dispatched(
                session, claimed.id, dag.id,
            )
            await session.commit()
        dispatched_this_tick += 1
    except Exception:
        logger.exception("Failed to create+dispatch DAG for %s", item.external_id)
        # The transaction rolled back; claim is released. The reconciler will
        # pick this up if the failure is transient AND the claim was committed.
        # Worst case: the row is in work_queue_items with dispatched_at=NULL
        # and the next reconciler pass re-dispatches.

# Cross-tick reconciler: catch any rows left in dispatched_at=NULL state
# longer than the grace window (covers restart-during-running cases).
stale = await self._mgr.list_undispatched(
    source=self._adapter.source_name,
    older_than=timedelta(minutes=5),
)
for row in stale:
    if dispatched_this_tick >= cap:
        break
    # ... retry the create+dispatch logic above, in a fresh transaction
```

**`claim_for_dispatch` implementation** (silent-failure P1-3):

```sql
-- Atomic UPSERT that returns the row only when this caller wins the race.
INSERT INTO nous_system.work_queue_items
    (agent_id, source, external_id, payload, created_at, dispatched_at)
VALUES
    (:agent_id, :source, :external_id, :payload, NOW(), NULL)
ON CONFLICT (agent_id, source, external_id) DO NOTHING
RETURNING id, source, external_id;
```

If the INSERT collided, `RETURNING` yields zero rows → `claim_for_dispatch` returns None → caller skips. If it inserted, `RETURNING` yields one row → caller proceeds to atomic `dag_create + mark_dispatched`.

For pre-existing rows whose previous tick committed the INSERT but failed before `mark_dispatched`, the reconciler picks them up via `list_undispatched(older_than=5min)`. This closes both architecture P1-D (orphan after partial commit) and silent-failure P1-5 (restart mid-running).

**Terminal handling:**

```python
async def _handle_terminal(self, item):
    existing = await self._mgr.get(source=..., external_id=item.external_id)
    if existing is None or existing.dispatched_at is None or existing.dag_id is None:
        # Never dispatched, or claim never finalized. Just mark as seen+terminal.
        await self._mgr.mark_terminal_unseen(item)
        return
    try:
        await self._orchestrator.cancel_dag(existing.dag_id, reason="external state terminal")
    except Exception:
        logger.exception("cancel_dag failed for %s — will retry next tick", existing.dag_id)
        return  # do NOT mark_terminal — retry next tick
    await self._mgr.mark_terminal(existing.id, item.state)
```

Per-tick admission cap (`NOUS_WORK_QUEUE_MAX_DAGS_PER_TICK=5`) prevents flooding `MAX_ACTIVE_DAGS` (currently 5 — see `store.py:19`); excess items wait for the next tick.

**`_default` frame interaction (silent-failure P2-3):** The DAG created by `claim_for_dispatch` uses an adapter-supplied default frame_type (e.g. `file_jsonl` → `research`). Operators can override per-DAG by amending the WorkItem payload. Default frame_type per adapter is set at adapter class level: `FileJsonlAdapter.DEFAULT_FRAME_TYPE = "research"`. Operators who set `{"debug": 1, "research": 3}` per-frame caps therefore see work_queue items go through the research bucket, not silently bypassing all caps.

**External_id sanitization (silent-failure P2-/cross-cutting):** The DAG name `f"work_queue:{item.external_id}"` and any node names derived from `external_id` pass through `sanitize_segment` (F064.3 helper). `claim_for_dispatch` does NOT sanitize — the DB column stores raw `external_id` because (source, external_id) is the dedup key. Only the path / name surfaces go through sanitize.

### 9.4 — Acceptance criterion mapping

> A `work_queue` heartbeat check pointed at a `file_jsonl` source emits exactly one DAG per new line and zero DAGs for already-seen lines across restarts. When a line is marked terminal in the source, the corresponding DAG is cancel-cascaded.

Mapped to `test_work_queue_acceptance` integration test that:
1. Writes 3 lines to a JSONL file
2. Fires `WorkQueueCheck.run()` once → asserts 3 DAGs created
3. Restarts the process (re-instantiates check + manager)
4. Fires again → asserts 0 new DAGs
5. Marks line 2 as `terminal: true` in the file
6. Fires again → asserts DAG #2 is cancelled

### 9.5 — Tests

| Test | Type | What it verifies |
|---|---|---|
| `test_file_jsonl_adapter_lists_lines` | unit | Adapter returns 3 items for a 3-line file |
| `test_claim_for_dispatch_returns_row_only_on_new_insert` | unit | Two calls with identical (source, external_id) → first returns row, second returns None (silent-failure P1-3) |
| `test_dag_created_per_new_item` | unit | New item → `dag_create` called once with item.body as task |
| `test_terminal_state_cancels_dag` | unit | Item turns terminal → `orchestrator.cancel_dag` called with the dispatched dag_id |
| `test_cancel_dag_failure_skips_mark_terminal` | unit | `cancel_dag` raises → `mark_terminal` is NOT called; next tick retries (architecture P2 / silent-failure P2-8) |
| `test_partial_commit_orphan_recovered_by_reconciler` | unit | Manually insert row with `dispatched_at=NULL` aged 6 min → reconciler picks it up and creates a DAG (architecture P1-D / silent-failure P1-4) |
| `test_restart_mid_running_dag_does_not_re_dispatch` | integration | Create row + DAG (running), restart the process (re-instantiate manager + check), tick again → no second DAG (silent-failure P1-5) |
| `test_admission_cap_defers_excess_items` | unit | 10 new items, cap=5 → 5 DAGs created, 5 wait for next tick |
| `test_empty_queue_is_not_a_failure` | unit | `adapter.list_active() == []` → check returns success, `consecutive_failures` NOT incremented (silent-failure P2-4) |
| `test_adapter_default_frame_type_applied` | unit | `file_jsonl` adapter → DAG node has `frame_type="research"` so per-frame caps engage (silent-failure P2-3) |
| `test_external_id_with_slash_sanitized_in_node_name` | unit | `external_id="foo/../bar"` → DAG node name is `work_queue_foo_bar`, path is contained inside workspace_root |
| `test_work_queue_acceptance` | integration | Full acceptance scenario from spec §F064.6 |

### 9.6 — What can fail

| Failure | Mitigation |
|---|---|
| Adapter raises mid-loop | `WorkQueueCheck.run` wraps each adapter call in try/except, increments `consecutive_failures` (inherited from `BaseCheck`), respects the circuit breaker |
| JSONL file is rewritten mid-read | `FileJsonlAdapter` reads the whole file in one pass with `Path.read_text()` (no streaming) → atomic-ish on small files; documented limitation |
| `dag_create` fails (MAX_ACTIVE_DAGS reached, store.py:44) | `upsert_seen` rolls back via context manager; the row is **NOT** inserted, so next tick retries |
| `cancel_dag` called on an already-terminal DAG | `cancel_dag` early-returns at orchestrator.py:107 if status in `("completed", "failed", "cancelled")` — no-op |
| `mark_terminal` and `mark_dispatched` race | The `work_queue_items` row is keyed by `(agent_id, source, external_id)`; both methods take that key and update; Postgres serializes the two UPDATEs |
| Operator misconfigures `work_queue_file_jsonl_path` to a missing file | `FileJsonlAdapter.list_active` returns `[]` and logs at WARN level. No exception bubbles up, no findings (zero work to do) |

---

## 10 — Cross-cutting: tests, docs, rollback

### 10.1 — Test strategy

Per spec acceptance criterion: each sub-feature ships with ≥5 unit tests and 1 integration test. Plan adds ~1700 lines of test code across 6 new `tests/test_*.py` files. Integration tests run against the existing pytest fixture stack (`tests/conftest.py` already provides agent_id-scoped DB sessions).

Regression suite: full `uv run pytest tests/` must pass after every commit. CI pre-commit hook does the same.

### 10.2 — Documentation updates (revised: silent-failure P1-7 / architecture P1-B)

- `CLAUDE.md` env table — add all new `NOUS_*` env vars in §"Environment Variables".
- `docs/features/INDEX.md` — at the end of commit 8:
  - F064.1 → `✅ Shipped`
  - F064.2 → `✅ Shipped`
  - F064.3 → `✅ Shipped`
  - F064.4 → `🟡 v1 partial — manifest persistence only, consumer enforcement deferred to F064.4-v2` (silent-failure P1-7)
  - F064.5 → `🟡 v1 partial — Episode reuse only, no LLM thread continuity (deferred to F064.5-v2)` (architecture P1-B)
  - F064.6 → `✅ Shipped`
- `docs/features/F064-symphony-orchestration-adoptions.md` — update Status from `📝 Draft` to `🟡 v1 partial — F064.4 + F064.5 are partial per amended acceptance criteria`. Add a `## v1 scope notes` section explicitly listing:
  - F064.4-v1 ships manifest persistence; orchestrator enforcement deferred.
  - F064.5-v1 ships Episode reuse; LLM thread continuity deferred.
  - Both deferrals were made deliberately during the 3-agent plan review (links to this plan doc).

### 10.3 — Rollback plan

All sub-features feature-flagged off. Rolling back is one of:

- **Per-sub-feature kill switch:** set the relevant env var to false (`NOUS_DAG_STALL_DETECTION_ENABLED=false`, etc.) and restart. No data migration to undo.
- **Schema rollback:** if a migration column proves harmful, write a `9XX_revert_*.sql` migration that DROPs the column. No data is lost because nothing in the column is load-bearing (all reads are guarded by the off-by-default flag).
- **Full PR revert:** `git revert <merge-commit>` followed by an Ansible-style env config rollback. Migrations 043–046 stay applied; their columns remain nullable / default-zero and are harmless.

---

## 11 — What can fail during plan creation itself (meta-risks)

This section addresses the user's request to "think what can fail during the plan creation."

| Risk | Why it matters | Mitigation in this plan |
|---|---|---|
| Phantom-API drift between plan and code | Surface map was 2 days old; code could have moved | Plan re-pins line numbers in the per-feature sections; implementation commits will re-verify |
| F061 migration 042 lands first and collides | We're claiming 043; if 042 doesn't exist, our 043 is harmless | The plan explicitly does not assume 042 exists; migration runner applies in lex order |
| New tests collide with existing test file names | Six new test files | Prefix all with `test_dag_*`, `test_skill_*`, `test_schedule_*`, `test_work_queue_*`; verified no collisions in `tests/` |
| Adding env vars without updating CLAUDE.md | Memory-recorded pattern | Plan commit 7 is dedicated to the docs update |
| Test infra requires docker compose up to be running | Tests use real Postgres | Memory note `feedback_eval_db_before_pr.md` applies; plan validates against fresh DB before PR |
| Reviewer fatigue on the 6-feature PR | Mega-PR review pattern | Risk-ascending commit order + commit-by-commit subagent reviews + `@codex` per substantive commit; fallback split into two PRs if review can't converge |
| `subtasks cannot spawn subtasks` rule (F024) blocking F064.6 | Spec called this out as preserved | F064.6's work_queue check is a **heartbeat check** (not a subtask). It calls `dag_create`, which creates a top-level DAG that THEN runs subtasks. No subtask-spawns-subtask violation |
| Per-frame in-memory accumulator drifts under concurrent ticks | Orchestrator already holds `self._lock` during `tick()` (line 70). Single-process tick serialization makes the accumulator safe | Plan §5.2 calls this out explicitly |
| Migration files reordered when F062/F063 (on `feat/F044-tinyhippo-lite` branch) merge | Only relevant if those merge into main during F064 implementation | Each new migration's name is unique (043, 044, 045, 046 don't collide with anything on main today); if a renumber is needed at merge time, it's mechanical |
| Existing tests break due to schema column additions | New columns are all nullable / default-zero / default-empty-dict | Verified by reading the existing `tests/test_dag_*.py` files — none rely on column count or exhaustive INSERT lists |
| Settings cross-validator fires on a deployment where `NOUS_DAG_NODE_MAX_TIMEOUT=120` (lower than `NOUS_DAG_NODE_MAX_STALL_TIMEOUT=3600` default) | Cross-validator would reject startup | The validator only fires when `dag_stall_detection_enabled=true` (default false). Operator must explicitly opt in; if they do and their max_timeout is below the default stall ceiling, the error is informative |

---

## 12 — Decision log

- **Scope:** all six in one PR (user choice, overrides spec's 4-PR recommendation).
- **F064.4 v1 scope:** manifest-only; consumer wiring deferred to v2 (avoids phantom `procedure_id` invention). Marked `🟡 v1 partial` in INDEX.md.
- **F064.5 v1 scope:** Episode reuse only; no LLM thread continuity (post-review: `runner.end_conversation` pops Conversation; thread continuity requires explicit state serialization out-of-scope-for-v1). Marked `🟡 v1 partial`. Deferred to F064.5-v2.
- **F064.6 adapter pattern:** ABC (not Protocol — spec misidentified F033's pattern).
- **Activity ping hook:** three sites — node launch, top-of-`_tool_loop`-iteration, tool-call dispatch. Covers both text-only-turn (architecture P1-C) and long-tool-call (silent-failure P1-2) stall classes. Fire-and-forget with `asyncio.shield`.
- **Concurrency counting:** inline SQL (not new manager method). Per-node try/except inside dispatch loop so one failed launch doesn't poison the wave.
- **Workspace sanitization posture:** reject-at-insert (flag-gated), transformation-at-read (unconditional, defense in depth). Windows-safe default via `tempfile.gettempdir()`.
- **Continuation reuse channel:** stable `session_id` (not phantom `episode_id` kwarg). Running-subtask debounce guard added explicitly (architecture P1-A).
- **F064.6 atomicity:** `claim_for_dispatch` returns row only on actual insert (via `INSERT … ON CONFLICT DO NOTHING RETURNING …`). `dag_create + mark_dispatched` run in a single session/transaction. Cross-tick reconciler picks up partial-commit orphans older than 5 min.
- **Migration numbering:** 043 (DAG columns — F064.1+.2 folded), 044 (procedure runtime_metadata), 045 (schedule continuation), 046 (work_queue_items). 042 reserved for F061.
- **Defaults:** every new flag off; behavior preserved when flag is off. Two exceptions: workspace read-time containment-assert (security boundary, unconditional) and skill manifest field persistence (silent-failure family fix, unconditional persistence, flag gates consumer only).
- **Test count target:** ≥5 unit + ≥1 integration per sub-feature; ~2000 lines total new test code (raised from ~1700 in v1 due to additional review-driven tests).

---

## 13 — Review delta (v1 → v1.1)

This plan was reviewed by three subagents in parallel (architecture, code-conventions, silent-failure-hunter). Each returned APPROVE_WITH_REVISIONS. Convergent P1 findings:

| # | Finding | Reviewers concurring | Resolution in v1.1 | Plan §s edited |
|---|---|---|---|---|
| C1 | F064.4 silent-drop of manifest fields when flag is off (canonical F032/F033/F060/F061 silent-failure family) | silent-failure P1-1, conventions P1-2 | Always parse + always persist; flag gates consumer only. `schema_version: 1` key for forward-compat. Marked `🟡 v1 partial` in INDEX.md | §2.1, §7.1, §7.2, §7.4 |
| C2 | F064.1 ping placement: tool-result-only misses text-only-turn case (architecture) AND long-tool-call case (silent-failure) | silent-failure P1-2, architecture P1-C | Three ping sites: node launch, top-of-iteration, tool-call dispatch. Fire-and-forget with `asyncio.shield`. New tests cover both gap classes | §4.3, §4.5 |
| C3 | F064.6 sequential 3-call ingestion has orphan window between `dag_create` and `mark_dispatched` | silent-failure P1-3/4/5, architecture P1-D, conventions P2-2 | `claim_for_dispatch` uses `INSERT … ON CONFLICT DO NOTHING RETURNING …` for the bool contract. `dag_create + mark_dispatched` in single session. Cross-tick reconciler for partial-commit recovery | §9.1, §9.3, §9.5 |
| C4 | F064.5 "send only continuation guidance" is structurally false: `runner.end_conversation` pops Conversation → fresh subtask has no LLM context | architecture P1-A and P1-B | Re-scoped F064.5-v1 to Episode reuse only. LLM thread continuity deferred to F064.5-v2. Acceptance criterion amended. Running-subtask debounce guard added | §8.0 (new), §8.2, §8.4, §8.5, §8.6 |
| C5 | Windows-hostile hardcoded `/tmp/...` Path default | conventions P1-1 | `Field(default_factory=lambda: Path(tempfile.gettempdir()) / ...)` | §2.1, §6.1 |
| C6 | F064.2 in-memory accumulator + `_launch_node` raise → wave abandoned mid-dispatch | silent-failure P1-6, conventions P2-6 | Per-node try/except wrapping `_launch_node` in BOTH legacy and capped paths. Accumulator only increments on success | §5.2 |
| C7 | F064.4 INDEX.md mis-labels v1 as fully shipped when it's actually manifest-only | silent-failure P1-7 | Marked `🟡 v1 partial — manifest persistence only`. Explicit `## v1 scope notes` section in spec | §10.2 |
| C8 | F064.2 ORM column precedes its DB migration by 3 commits | architecture P2-A | Folded F064.2's column into migration 043 (lands in commit 1). Renumbered downstream | §1.3, §3 |

Additional P2/P3 fixes folded in:

- Read-time `assert_inside_root` runs **unconditionally** as a security boundary (architecture P2-B); the flag gates only insert-time rejection.
- Settings `@field_validator` and `ge=1` constraints added for `dag_global_max_concurrent_by_frame` values, `schedule_max_continuation_turns`, `work_queue_*` (architecture P2-C, conventions P2-4).
- `_default` frame contract documented explicitly in env-var description (silent-failure P3-4).
- Empty work queue treated as success not failure (silent-failure P2-4).
- Adapter-supplied default `frame_type` per adapter (silent-failure P2-3) — work_queue DAGs no longer bypass per-frame caps.
- `cancel_dag` failure does NOT call `mark_terminal`; retry next tick (architecture P2 / silent-failure P2-8).
- `workspace_path.mkdir(parents=True, exist_ok=True)` before subprocess launch (silent-failure P3-3).
- New tests added for each fix; per-feature test counts raised.

What did NOT change in v1.1:

- The six-sub-features-in-one-PR scope (user choice).
- The ABC pattern for `WorkQueueAdapter` (matches `BaseCheck`, not F033's Protocol).
- The `_tool_loop` hook point (rather than plumbing through `call_streaming_aggregated`).
- Migration 042 reservation for F061.
- Risk-ascending commit order.

### v1.1 → v1.2 (after re-review cycle)

The re-review cycle returned APPROVE from architecture and silent-failure but REQUEST_CHANGES from the conventions reviewer over two mechanical bugs introduced by the v1.1 revisions plus one missing test:

| # | Finding | Fix |
|---|---|---|
| D1 | Migration filename drift between canonical list (§1.3 / §3) and per-feature `Files touched` tables (§4.1, §7.2, §8.1, §9.1) — implementer would create three mis-numbered files | Renumbered per-feature filenames: §4.1 `043_dag_node_columns.sql`, §7.2 `044_procedure_runtime_metadata.sql`, §8.1 `045_schedule_continuation.sql`, §9.1 `046_work_queue_items.sql`. §4.2 SQL block expanded to include the F064.2 column |
| D2 | `work_queue_items.dispatched_at TIMESTAMPTZ NOT NULL` declared in §9.1 contradicted the reconciler design which treats `dispatched_at IS NULL` as the "claimed but not yet dispatched" sentinel | Column declared `NULL` explicitly with a comment pinning the sentinel semantics. Added partial index on `(agent_id, source) WHERE dispatched_at IS NULL` for the reconciler scan |
| D3 | Test plan §7.4 had a parser-layer test for the silent-drop fix but no integration test asserting end-to-end persistence | Added `test_runtime_metadata_persisted_when_consumer_flag_off` integration test |
| D4 | `IF NOT EXISTS` audit on migrations 044/045/046 | Added `IF NOT EXISTS` to every `ADD COLUMN` and `CREATE TABLE` clause in the per-feature `Files touched` descriptions |

No new P1s expected on v1.2; the conventions reviewer should now return APPROVE.
