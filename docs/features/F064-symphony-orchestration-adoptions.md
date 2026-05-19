# F064: Symphony Orchestration Adoptions

**Status:** 📝 Draft
**Proposed by:** Tim (via analysis of `openai/symphony` SPEC.md)
**Date:** 2026-05-18
**Depends on:** F038 (Unified DAG Orchestration — shipped), F038.1 (DAG Completion Check — shipped), F046 (Configurable DAG Node Timeouts — shipped), F011 (Skill Discovery — shipped), F034 (Heartbeat — shipped)
**Blocks:** None
**Related:** F061 / F062 / F063 (subtask hardening family — Doc 019)
**Source:** [openai/symphony SPEC.md](https://github.com/openai/symphony/blob/main/SPEC.md) (24.1k★, Apache-2.0, Elixir reference impl; 13 commits; spec is "Draft v1 — language-agnostic")

---

## TL;DR

`openai/symphony` is OpenAI's open-source spec for orchestrating coding agents against an issue tracker. Comparing its design against Nous's DAG orchestrator (`nous/dag/`) surfaces **six concrete primitives Symphony has and Nous doesn't**. Three are high-ROI tactical (stall detection, per-class concurrency caps, workspace safety invariants); two are medium-effort architectural (workflow-as-code skills, continuation-turn pattern); one is a strategic gap (work-queue ingress) that closes a real autonomy ceiling.

This spec packages all six as **F064.1 through F064.6**, each independently shippable, with explicit priority ordering and concrete API/file targets. F064.1–F064.3 should land first as one PR; the rest are evaluated independently after F061/F062/F063 (the in-flight subtask-hardening family) merge.

---

## Background

### What Symphony is

A long-running daemon (Elixir reference impl) that:

1. Polls Linear on a fixed cadence (`polling.interval_ms`, default 30 s) for issues in active states.
2. For each eligible issue, creates a per-issue workspace at `<workspace.root>/<sanitized_identifier>` and launches `codex app-server` (default) via `bash -lc` in that cwd.
3. Manages the agent subprocess lifecycle with bounded concurrency, retries with exponential backoff, stall detection, and tracker-state reconciliation.
4. Reads its entire runtime contract — prompt template, tracker config, hooks, sandbox policy, concurrency caps — from a repo-owned `WORKFLOW.md` (YAML front matter + Liquid prompt body), hot-reloaded without restart.
5. Has **no persistent DB.** Recovery is tracker- and filesystem-driven. Restart loses in-memory scheduler state but resumes correctly because the tracker is the source of truth.

Verified facts (re-fetched 2026-05-18):
- 24.1k stars, 2.3k forks, Apache-2.0 license, 13 commits.
- 95.5% Elixir / 3.0% Python / 1.2% CSS by line count.
- Spec status: "Draft v1 (language-agnostic)" — README explicitly invites users to ask their coding agent to reimplement it in any language.
- Explicit non-goal: tracker writes (state, comments, PR links) are done **by the agent** via tools (`linear_graphql` client-side extension), **not** by the orchestrator.

### What this spec is not

- **Not** a proposal to adopt Symphony's tracker-polling shape wholesale. Nous's heartbeat-check primitive (F034.5) is strictly more flexible than a fixed-cadence Linear poller.
- **Not** a proposal to adopt Codex over Claude Code as Nous's agent subprocess. The Codex app-server protocol is irrelevant — Nous already has a working subprocess model.
- **Not** a steering-channel proposal. Symphony explicitly treats coding agents as batch jobs with only a kill switch (verified §10.5: "A run MUST NOT stall indefinitely waiting for user input"); Nous makes the same trade-off today. See *Alternatives considered → Mid-flight steering* below.

---

## Problem: six missing primitives in Nous

Each subsection cites the Symphony SPEC.md section and the Nous file where the gap lives.

### F064.1 — Stall detection + reconciliation tick (TACTICAL)

**Symphony (§8.5 Part A):**
> For each running issue, compute `elapsed_ms` since `last_codex_timestamp` (or `started_at`). If `elapsed_ms > codex.stall_timeout_ms`, terminate the worker and queue a retry. If `stall_timeout_ms <= 0`, skip stall detection entirely.

Default `codex.stall_timeout_ms = 300000` (5 min) — i.e. no agent event for 5 min ⇒ kill.

**Nous gap (`nous/dag/orchestrator.py`, 840 LOC):** zero matches for `stall|reconcil`. Only timeouts present are:

- `_CHECK_CMD_TIMEOUT = 10.0` — hard timeout per `completion_check` shell invocation (`orchestrator.py:32`).
- `_effective_timeout(node)` clamped to `settings.dag_node_max_timeout` — the total elapsed wall-clock budget (`orchestrator.py:514–520`).
- F046 raised default to 600 s and max to 7200 s.

**Failure mode this leaves open:** a Claude Code job that hangs after producing some output but before completing burns the **full** 2-hour wall-clock budget. The DAG node only fails when `elapsed_total > effective_timeout` (`orchestrator.py:397`) — there is no detection of "no progress for N minutes." This is the exact failure class Symphony's `stall_timeout_ms` was added for.

This also is a structural sibling to the silent-failure family already documented in F032, F033, F060, F061 — those address subtasks that exit "cleanly" with empty results; F064.1 addresses subtasks that don't exit at all.

### F064.2 — Per-class concurrency caps (TACTICAL)

**Symphony (§5.3.5, §8.3):**
> `agent.max_concurrent_agents_by_state` (map `state_name → positive integer`) — default empty map.
>
> Per-state limit: `max_concurrent_agents_by_state[state]` if present (state key normalized); otherwise fallback to global limit.

So Symphony has both a global cap (`agent.max_concurrent_agents`, default 10) *and* a per-tracker-state cap that takes precedence per-state.

**Nous gap (`nous/dag/schemas.py:114`):** the only DAG-level budget knob is `token_budget: int | None`. Caps are also enforced via two hardcoded numbers in F038 ("max 4 parallel per wave, max 5 active DAGs"). There is no way to say "let many cheap research nodes run in parallel but only one expensive Claude Code node at a time."

This matters concretely today: when Tim runs a DAG with one expensive `frame_type=debug` Claude Code node + three cheap `frame_type=research` nodes, the only way to throttle the expensive one is to serialize it via `dependency` edges — losing parallelism on the cheap ones. The Symphony pattern lets us say `max_concurrent_by_frame_type: {debug: 1, research: 3}` and keep the topology free.

### F064.3 — Workspace safety invariants (TACTICAL)

**Symphony (§9.1, §4.2):** three invariants enforced before every subprocess launch:
1. Per-issue workspace path MUST be `<workspace.root>/<sanitized_issue_identifier>`.
2. Workspace key derived from `issue.identifier` by replacing any char **not in `[A-Za-z0-9._-]`** with `_`.
3. `bash -lc` cwd MUST equal `workspace_path` (§5.3.6 `codex.command`).

Plus runtime: §9.2 requires `workspace_path` to be inside `workspace_root` (resolved-absolute, after `~` expansion and `$VAR` substitution) before any hook or agent launch.

**Nous gap:** the convention `/tmp/nous-workspace/dag-status/{dag_id}/{node_name}/` is followed by F038.1 (verified at `orchestrator.py:33`: `DAG_STATUS_BASE_DIR = Path(tempfile.gettempdir()) / "nous-workspace" / "dag-status"`), but:

- No explicit assertion that `cwd == workspace_path` before spawning subtasks.
- No sanitization of `dag_id` / `node_name` before they become path segments — they currently come from caller-supplied strings.
- No "inside workspace root" check before `completion_check` shell commands execute.

This is **not** a known live exploit — but it's a known latent class of bug that Symphony makes explicit as a MUST.

### F064.4 — Workflow-as-code in skills (ARCHITECTURAL)

**Symphony (§5):** `WORKFLOW.md` is repo-owned, version-controlled YAML+Markdown that contains **both** the prompt template *and* the runtime config (polling cadence, concurrency caps, hooks, sandbox policy, agent command, timeouts). Hot-reloaded on file change without restart (§6.2 — explicit `MUST`).

**Nous today (`nous/skills/parser.py`):** `SkillManifest` parses YAML front matter into `name`, `description`, `domain`, `triggers`, `frames`, `tools`, `requires`, `source_url`, `version`. These are all *discovery / activation* fields. Skills cannot declare:

- Their preferred concurrency cap (e.g. "only run one of me at a time")
- Their timeout overrides
- Pre/post hooks (analogous to Symphony's `after_create / before_run / after_run / before_remove`)
- A sandbox / approval posture
- Whether they should be hot-reloaded on edit

Symphony's design idea — *skills are self-describing executables, not just prompts* — would let Nous skills carry runtime policy alongside instructions.

### F064.5 — Continuation-turn pattern for scheduled tasks (ARCHITECTURAL)

**Symphony (§7.1):**
> After each normal turn completion, the worker re-checks the tracker issue state. If the issue is still in an active state, the worker SHOULD start another turn on the **same live coding-agent thread in the same workspace**, up to `agent.max_turns` (default 20). The first turn SHOULD use the full rendered task prompt; continuation turns SHOULD send only continuation guidance to the existing thread, not resend the original task prompt that is already present in thread history.

**Nous gap:** scheduled tasks (`schedule_task`) and recurring heartbeat checks fire a **fresh context** every time. There is no equivalent of "same thread, many turns, continuation guidance only" for stateful work. The daily ski report, the pre-market report, and the weekly crypto research DAG all start cold every fire.

Adopting this for *scheduled* tasks (not interactive ones) would let recurring work carry season-long memory natively, without external scaffolding.

### F064.6 — Work-queue ingress (STRATEGIC)

**Symphony (§3.1, §8.1):** the Issue Tracker Client (Linear adapter; pluggable) fetches active-state issues every tick. The orchestrator's poll-loop is fed exclusively by this stream. Engineers don't enqueue work into Symphony directly — they file issues; Symphony drains them.

**Nous gap:** Nous has three execution triggers — (1) Tim sends a message (reactive), (2) `schedule_task` cron fires (scheduled), (3) heartbeat check finding (event-driven from internal observations). It has **no work-queue ingress** — no way to autonomously drain an external backlog of "things to do." This is the single biggest structural pattern Symphony has that Nous lacks.

Symphony validates this pattern at OpenAI scale (24.1k★ is real adoption signal for a 13-commit spec repo). Adding a heartbeat check that polls GitHub Issues / a Linear project / a plain JSON queue file and emits a DAG per item would meaningfully change what "autonomous Nous" can be.

---

## Goals

For each sub-feature, in dependency order:

### F064.1 (Stall detection)
- Add `stall_timeout_seconds: int | None` to `DAGNodeSpec`. None = disabled (matches Symphony semantics for `stall_timeout_ms <= 0`).
- Track `last_activity_at` on each `DAGNode` row (event-driven update from subtask streaming events and `completion_check` exit-code observations).
- In `_poll_awaiting_checks` and the running-subtask reconcile path: if `now - last_activity_at > stall_timeout_seconds`, mark node failed with `error="stalled: no activity for {stall_timeout_seconds}s"`, propagate cancel_cascade.
- Settings: `NOUS_DAG_NODE_DEFAULT_STALL_TIMEOUT` (default 600 s; 0 disables globally), `NOUS_DAG_NODE_MAX_STALL_TIMEOUT` (default 3600 s; clamp ceiling).

### F064.2 (Per-class concurrency caps)
- Extend `DAGSpec` (`nous/dag/schemas.py:114` neighborhood) with `max_concurrent_by_frame_type: dict[str, int] | None`.
- In the orchestrator's dispatch tick (currently selecting nodes for execution), compute running-by-frame-type and gate accordingly. Falls back to existing global cap when the dict is empty/None — fully backward compatible.
- Settings: `NOUS_DAG_GLOBAL_MAX_CONCURRENT_BY_FRAME` (operator-level override that wins over per-DAG when set), default unset.

### F064.3 (Workspace safety invariants)
- Add `nous/dag/_workspace.py` with three pure functions: `sanitize_segment(s) -> str` (apply `[A-Za-z0-9._-]` → `_`), `assert_inside_root(path, root) -> None`, `compute_workspace_path(dag_id, node_name, root) -> Path`.
- Wire into `DAG_STATUS_BASE_DIR` consumers in `orchestrator.py`.
- Add an assertion in the subtask-launch path (and `completion_check` shell exec path) that `cwd == workspace_path` and `workspace_path` is inside the resolved workspace root.
- Sanitize `dag_id` and `node_name` at insert-time in `store.py`.

### F064.4 (Workflow-as-code skills)
- Extend `SkillManifest` (`nous/skills/parser.py`) with optional runtime fields:
  - `concurrency_cap: int | None` — max simultaneous activations of this skill across all DAGs/subtasks
  - `timeout_override_seconds: int | None`
  - `hooks: dict[str, str]` — before/after shell snippets (subset of Symphony's four hooks; start with `before_run` and `after_run` only)
  - `requires_human_review: bool` — explicit safety posture
- Wire into the DAG orchestrator and subtask launcher: when a node activates a skill (via procedure_id), apply the skill's runtime metadata over the node's defaults.
- Out of scope for v1: hot-reload semantics — skills already auto-reload on `learn_skill` re-registration.

### F064.5 (Continuation turns)
- Add `continuation_turns: int = 0` to `ScheduleSpec` (default 0 = today's behavior).
- When > 0: on schedule fire, instead of starting a fresh context, look up the previous fire's `episode_id` and reuse its conversation thread, sending only the continuation guidance (configurable per-schedule via `continuation_prompt` field; defaults to `"Continue. The previous run completed at <ts>. Apply the same task to fresh context."`).
- Hard cap at `min(continuation_turns, NOUS_SCHEDULE_MAX_CONTINUATION_TURNS)` (default ceiling 50) to prevent runaway threads.

### F064.6 (Work-queue ingress)
- Add `nous/heartbeat/checks/work_queue.py` — a built-in heartbeat check class that:
  - Reads an operator-configured source (`work_queue_source` setting: `github_issues` | `linear` | `file_jsonl`)
  - Polls every `interval_seconds` (operator-configurable; default 300 s)
  - For each new item not already in a tracking table (`work_queue_items` — new DB table: `external_id`, `source`, `dispatched_at`, `dag_id`, `terminal_state`), emits a DAG via `dag_create`
  - On subsequent polls, reconciles tracker state: if external item moves to terminal, cancel-cascade the corresponding DAG.
- Adapter pattern: `WorkQueueAdapter` ABC with `github_issues`, `linear`, `file_jsonl` (smallest-useful set) implementations. Implementations are pluggable via the same registry pattern as F033 search routing.
- This is the only sub-feature that requires a DB migration. The others are config + Python.

---

## Non-goals

- No port of Symphony's Linear GraphQL coupling — F064.6 abstracts it behind `WorkQueueAdapter`.
- No replacement of the heartbeat tick loop with a Symphony-style poll loop. Heartbeat checks are strictly more flexible.
- No adoption of `codex app-server` / Codex stdio protocol — Claude Code subprocess model already works.
- No removal of the "subtasks cannot spawn subtasks" rule (F024) — that limit is the right call (corroborated by Doc 018 §1.3).
- No LLM-critic on stall events. Stall = kill + retry, matching Symphony §8.5 and the existing F061 design rationale.

---

## Design rationale

### Why split into six sub-features

Each gap has independent value, an independent failure mode it closes, and independent code surface. Bundling them into one mega-PR would (a) be unreviewable and (b) couple unrelated risks. The split also lets Tim choose: ship F064.1–F064.3 fast (≤1 week total), defer F064.4–F064.5 until after F061/F062/F063 settle, and treat F064.6 as a Q3 strategic project.

### Why F064.1 first

Stall detection is the lowest-risk highest-ROI item. It strictly extends `DAGNodeSpec`, is fully optional (None = today's behavior), and closes a real failure class that Tim has hit in practice (smoke-test DAG `43b37234`, see F046). Estimated LOE: ~1 day.

### Why F064.6 last

Adding a work-queue poller changes Nous's *behavior model* — from "reactive + scheduled" to "reactive + scheduled + autonomous-backlog." That's a strategic move that warrants more discussion than a PR description provides. This spec packages the *option* but doesn't force the decision.

### Why no mid-flight steering primitive

Symphony's §10.5 explicit position: "A run MUST NOT stall indefinitely waiting for user input." The closest external lever it offers is `linear_graphql` *agent-side* tool — i.e. a human leaves a comment on the ticket and hopes the agent reads it. That's coordination-by-side-channel, not steering.

Nous today is in the same place: `runner.sh launch` is fire-and-forget, callbacks fire on completion, no mid-flight prompt-injection channel. Symphony's adoption at scale validates that "kill + restart with a better prompt" is sufficient for coding work. If Nous adds a steering primitive later, it should be motivated by a different use case (e.g. interactive research) — not by this analysis.

### Why workflow-as-code in skills, not a separate WORKFLOW.md

Symphony's `WORKFLOW.md` is one-per-repo. Nous skills are many-per-system, already version-controlled (via `learn_skill` + procedure store), already YAML-front-matter-parsed. Extending the existing skill manifest is the smaller change.

---

## Alternatives considered

1. **Adopt Symphony directly as a sidecar service.** Rejected: Symphony assumes Codex; we use Claude Code. The architecture rhymes, but adapter friction kills the win.

2. **Just adopt F064.1 and stop.** Tempting (it's the highest single-ROI item). Rejected because Tim asked for *all* missing components; F064.2 and F064.3 are nearly free and structurally adjacent.

3. **Skip F064.6 entirely as out of scope.** Tempting because it's the largest and most strategic. Kept because it represents the single biggest architectural pattern Nous is missing relative to Symphony; documenting it as a known gap (even if deferred) is the point of writing the spec.

---

## Implementation plan & estimated LOE

| Sub-feature | Tier | LOE | Files touched | DB migration | Depends on |
|---|---|---|---|---|---|
| F064.1 Stall detection | Tactical | ~1 day | `nous/dag/schemas.py`, `nous/dag/orchestrator.py`, `nous/dag/store.py`, `nous/config.py`, tests | No | F038, F046 |
| F064.2 Per-class concurrency | Tactical | ~1 day | `nous/dag/schemas.py`, `nous/dag/orchestrator.py`, `nous/config.py`, tests | No | F038 |
| F064.3 Workspace safety | Tactical | ~1 day | `nous/dag/_workspace.py` (new), `nous/dag/orchestrator.py`, `nous/dag/store.py`, tests | No | F038 |
| F064.4 Workflow-as-code skills | Architectural | ~3 days | `nous/skills/parser.py`, `nous/skills/bootstrap.py`, `nous/dag/orchestrator.py`, `nous/handlers/subtask_executor.py`, tests | No | F011, F037 |
| F064.5 Continuation turns | Architectural | ~3 days | `nous/handlers/task_scheduler.py`, `nous/api/runner.py`, schedule schema, `nous/config.py`, tests | Maybe (schedule field) | F009 |
| F064.6 Work-queue ingress | Strategic | ~2 weeks | New `nous/heartbeat/checks/work_queue.py`, new `work_queue_items` table, new migration, adapter ABC + 3 impls, settings, tests, docs | **Yes** | F034.5 |

Recommended landing order: **PR 1** = F064.1 + F064.2 + F064.3 (one bundle, ≤1 week, no DB migration). **PR 2** = F064.4 (after F061/F062/F063 merge). **PR 3** = F064.5 (independent). **PR 4** = F064.6 (after design review).

---

## Acceptance criteria

### Per sub-feature

- **F064.1:** A DAG node configured with `stall_timeout_seconds=30` that produces no `last_activity_at` updates for 31 s transitions to `failed` with the stall error and propagates `cancel_cascade`. None/unset = today's behavior (regression test).
- **F064.2:** A DAG with `max_concurrent_by_frame_type={"debug": 1, "research": 3}` and 1 debug + 4 research-frame nodes runs the debug node serially with the research nodes (3 research nodes run concurrently with the debug node; the 4th research node waits for an available slot). Unset = today's behavior (regression test).
- **F064.3:** Attempting to insert a DAG node with `name="../escape"` or a path that resolves outside `DAG_STATUS_BASE_DIR` is rejected at insert time. The `cwd` of any subprocess launched by orchestrator is asserted equal to the resolved workspace path.
- **F064.4:** A skill with `concurrency_cap: 1` cannot be activated by two simultaneous DAG nodes; the second waits or fails per a configurable mode (default: wait).
- **F064.5:** A scheduled task with `continuation_turns: 5` reuses the same `episode_id` thread across up to 5 consecutive fires, then resets on the 6th. The first fire uses the full prompt; subsequent fires use the continuation prompt.
- **F064.6:** A `work_queue` heartbeat check pointed at a `file_jsonl` source emits exactly one DAG per new line and zero DAGs for already-seen lines across restarts. When a line is marked terminal in the source, the corresponding DAG is cancel-cascaded.

### Cross-cutting

- All sub-features ship with feature flags (env var) defaulting to **off** so the merge is risk-free.
- Each sub-feature ships with at least 5 unit tests and 1 integration test.
- F046's `NOUS_DAG_NODE_MAX_TIMEOUT` ceiling continues to work alongside the new `NOUS_DAG_NODE_*_STALL_TIMEOUT` settings — stall ≤ wall-clock invariant is asserted in `Settings.model_validator`.

---

## Fact-check log

All claims in this spec were verified on **2026-05-18** against:

1. `openai/symphony` SPEC.md fetched live from `raw.githubusercontent.com/openai/symphony/main/SPEC.md` (30,017 chars). Section refs are stable per "Draft v1 (language-agnostic)" status.
2. GitHub repo page fetched live: 24.1k stars, 2.3k forks, Apache-2.0, 13 commits, 95.5% Elixir.
3. Nous code at branch `feat/F064-symphony-orchestration-adoptions` (forked from `origin/main` at `f42c01f`):
   - `nous/dag/orchestrator.py` (840 LOC) — zero matches for `stall|reconcil`; `_CHECK_CMD_TIMEOUT = 10.0` at line 32; `DAG_STATUS_BASE_DIR` at line 33; `_effective_timeout` at line 514.
   - `nous/dag/schemas.py:114` — `token_budget` is the only DAG-level capacity knob.
   - `nous/skills/parser.py` — `SkillManifest` fields enumerated directly from source.
   - `nous/heartbeat/` — listed; `work_queue.py` confirmed absent.
4. Prior decisions and facts:
   - Decision `c8d19a2a-25e0-438e-ab8f-de11abd368be` — earlier Symphony analysis (this spec supersedes by formalizing it).
   - Fact `de73aa5c-21d2-4190-895a-9f33b36cc506` — Symphony architecture summary.
   - Decision `4727efcf-c456-44f2-bfb2-37bcf1139906` — F038 design.
5. Sibling specs read in full to avoid scope overlap:
   - F046 (DAG node timeout config — shipped) — F064.1 deliberately uses a parallel naming scheme to F046's `NOUS_DAG_NODE_*_TIMEOUT`.
   - F061 (Subtask hardening — shipped to draft) — F064.1 closes the orthogonal "didn't exit at all" failure mode; F061 closes the "exited empty" mode.

Discrepancies caught and corrected during fact-check:
- Initial draft cited "Symphony exposes a steering channel via Linear comments" → corrected: it's agent-initiated polling of Linear, not operator-injected steering. Spec §10.5 is explicit that user-input mid-turn is a **hard failure**, not a feature.
- Initial draft cited F063 as latest spec number → corrected: F062/F063 are unmerged drafts on `feat/F044-tinyhippo-lite` branch; on `origin/main` the latest landed is F061. F064 is therefore the first safe number.
- Initial draft claimed Symphony has a persistent DB → corrected: §2.1 explicitly opts for "tracker/filesystem-driven restart recovery without requiring a persistent database; exact in-memory scheduler state is not restored."

---

## Open questions

1. **F064.4 hook semantics:** should Nous skill hooks run as shell snippets (Symphony parity) or as Python callables (Nous-native)? Shell is more portable; Python is safer. Recommend Python callables registered via a decorator; reject shell for security.
2. **F064.5 continuation prompt template:** should the continuation prompt be a per-schedule string, or derived from the schedule's frame_type? Recommend per-schedule string with a frame-type default fallback.
3. **F064.6 adapter ordering:** ship all 3 adapters in v1, or start with `file_jsonl` only? Recommend `file_jsonl` first (zero external deps, full e2e demonstration), then `github_issues` (high practical value for Tim's own work), then `linear` (parity).
