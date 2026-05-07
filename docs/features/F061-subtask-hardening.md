# F061 — Subtask Hardening

**Status:** 📝 Draft (2026-05-06)
**Owner:** nous core
**Scope:** Replace the unstructured subtask contract with a forced-terminal-tool report, structural validator, bounded retry, structured outcome enum, and full per-subtask telemetry — eliminating the dominant "completed with empty result" failure class.

**Issues addressed:** internal observation that a non-trivial fraction of `spawn_task` / scheduled / DAG-subtask invocations persist `status='completed', result=''`, wasting tokens and feeding garbage into parent context. Closes the P1 GAP at [`evaluations/2026-05-06-coverage-and-gaps.md:132`](../../evaluations/2026-05-06-coverage-and-gaps.md) (Subtask outcome correctness).

**Not in scope:**
- Heartbeat checks (`nous/heartbeat/*`) — they call `runner.run_turn(is_background=True)` directly and never go through `heart.subtasks`. F061 leaves their thin contract alone.
- `schedule_task` *firing* fidelity (cron correctness) — that is **F062 (sibling)**. Schedule-fired subtasks DO inherit F061 contracts because they go through `heart.subtasks.create()`.
- `run_python` programmatic tool calls — different code path (`nous/api/runner.py` programmatic execution), not a subtask.
- LLM critic / self-grading of report content — explicitly rejected (see _Design rationale_ → _Why no LLM critic_).

---

## Problem

### Today's contract is implicit and unenforced

[`nous/api/tools.py:1077-1086`](../../nous/api/tools.py) — the entire subtask system prompt:

```
You are executing a background subtask.
Deliver a clear, complete result. Do not ask questions.

Frame: <frame_type> — apply <frame_type>-appropriate reasoning and tool usage.

Task: <user-supplied free-text>
```

There is **no output schema, no termination tool, no success criteria, no length floor, no validator**. Whatever string the model returns at the end of the tool loop becomes `subtask.result`. If the model returns no text blocks (only `tool_use` / `thinking`), `result=""` is persisted as `status='completed'`.

### Five empirically-confirmed failure paths producing empty / fake-success completions

All verified by reading the code at the cited lines (2026-05-06).

| # | Path | File:line | Persisted state |
|---|---|---|---|
| 1 | `_extract_text` returns `""` when assistant content has only `tool_use`/`thinking` blocks | `nous/api/runner.py:1604-1610` → `nous/handlers/subtask_worker.py:157` | `status=completed, result=""` |
| 2 | Tool-call limit hit → one final no-tools call → may return empty | `nous/api/runner.py:1376-1388` | `status=completed, result=""` |
| 3 | Generic exception swallowed → fallback string returned as success | `nous/api/runner.py:386-391` ("I encountered an error processing your request. Please try again.") | `status=completed, result=<fallback>` |
| 4 | Censor-blocked at pre_turn → censor message returned as success | `nous/api/runner.py:286-291` | `status=completed, result=<censor msg>` |
| 5 | Max-turns fallback returns hardcoded "I reached the maximum number of tool iterations" | `nous/api/runner.py:1407-1423` | `status=completed, result=<fallback>` |

### The downstream amplifier

[`nous/cognitive/layer.py:80-94`](../../nous/cognitive/layer.py) — `_format_subtask_results` formats `Result: {s.result}` even when `s.result == ""`, then [`nous/cognitive/layer.py:563-571`](../../nous/cognitive/layer.py) marks the row delivered. The parent agent sees `Result:` (empty) injected into its system prompt as if it were authoritative output, and the row is never re-attempted.

### The DAG amplifier

[`nous/dag/orchestrator.py:240-282`](../../nous/dag/orchestrator.py) `_sync_subtask_node` reads `subtask.result` to advance node state. Empty result → DAG node marked `completed` with empty result → downstream nodes consume garbage.

### Why this isn't easily measured today

There are no per-subtask telemetry fields beyond `status / result / error`. No `final_outcome` enum, no `attempts`, no token-usage record on the row, no `tool_calls_made`, no `report_jsonb`. Operators cannot answer: "what fraction of subtasks completed with non-empty validated reports?" without ad-hoc SQL on `length(result) > N`.

---

## Goals

1. **No subtask ever persists `status='completed'` with an empty/un-validated report.** A subtask is "completed" iff a structured `submit_final_report(...)` tool call produced a schema-valid, length-floored payload.
2. **Five-state structured outcome enum** distinguishes silent-empty from real failures: `completed`, `incomplete_no_terminal`, `validation_failed`, `timed_out`, `errored`, `cancelled`. Stored in `final_outcome` column.
3. **Bounded retry on recoverable failures** (`incomplete_no_terminal`, `validation_failed`) — exactly **one** retry, with the prior attempt + validator feedback fed back to the model. No retry on `timed_out` / `errored` / `cancelled`.
4. **Per-subtask telemetry** lands on `heart.subtasks` and on `nous_system.events`: `report_jsonb`, `attempts`, `final_outcome`, `tokens_in`, `tokens_out`, `tool_calls_made`, `output_format`, `success_criteria`, `dag_node_id`.
5. **Backward compatibility** for callers (schedules, DAG nodes, legacy `spawn_task`) that don't pass `output_format` / `success_criteria` — worker synthesizes sensible defaults.
6. **Single hardened executor for inline AND background paths.** Both `spawn_task(await_result=true)` (inline, `tools.py:1218-1234`) and the worker pool path (`subtask_worker.py:_execute_subtask`) MUST share one `_execute_hardened` helper. Shipping the flag-flip with two contracts would relocate the exact failure F061 fixes — the inline path would tell the model about a tool that isn't registered.
7. **Heartbeat unaffected.** All changes scoped to subtask layer (`spawn_task` tool, `subtask_worker`, `build_subtask_prefix`, `heart.subtasks` schema, `_format_subtask_results`).
8. **Eval harness** — 20 synthetic subtask scenarios under `nous_eval/`, run nightly, measure success rate / empty rate / retry rate / tokens-per-subtask. Harness uses inline (`await_result=true`) execution to avoid needing a worker pool in the eval container.
9. **Operator dashboard** at `/dashboard/subtasks` mirroring `/dashboard/heartbeat`.
10. **Staged rollout** behind `NOUS_SUBTASK_HARDENING_ENABLED` (default `false` for one release; flip after eval gate).

## Non-goals

- Multi-step sub-graphs / sub-agent-spawned-sub-agents (Anthropic's pattern explicitly forbids this; we keep the same flat-only constraint).
- Cross-process subtask execution.
- Replacing `_extract_text` or rewriting the runner tool loop.
- Changing schedule firing semantics (F062).
- Adding a per-attempt LLM critic — see _Design rationale_.

---

## Design

### Mechanism 1 — The `submit_final_report` terminal tool

A new tool registered **only when a subtask is being executed** (not in normal chat tools, not in heartbeat). Schema:

```python
SUBMIT_FINAL_REPORT_SCHEMA = {
    "name": "submit_final_report",
    "description": (
        "Submit your final, complete report for this subtask. You MUST call "
        "this tool exactly once when you are done. Do not produce a final "
        "text-only response — the parent agent receives ONLY this tool's payload."
    ),
    "input_schema": {
        "type": "object",
        "required": ["summary", "confidence"],
        "properties": {
            "summary": {
                "type": "string",
                "description": (
                    "1-3 paragraph synthesis of what you did and what the "
                    "answer is. Must be self-contained — the parent will "
                    "read this without seeing your tool calls."
                ),
                "minLength": 50,
            },
            "findings": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Key facts, numbers, or sub-conclusions discovered.",
                "default": [],
            },
            "next_actions": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Recommended next actions, if any. May be empty.",
                "default": [],
            },
            "confidence": {
                "type": "number",
                "minimum": 0.0,
                "maximum": 1.0,
                "description": (
                    "Your confidence (0.0-1.0) that the summary is correct "
                    "and addresses the task."
                ),
            },
            "evidence_refs": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "IDs of memories you produced (fact UUIDs, decision IDs) "
                    "or references to external sources used. Lightweight "
                    "pointers — DO NOT dump full content here."
                ),
                "default": [],
            },
            "incomplete": {
                "type": "boolean",
                "description": (
                    "Set true ONLY if you genuinely cannot complete the task "
                    "(missing tools, missing info, blocked by external system). "
                    "Otherwise false. The parent treats true as a fail-with-reason."
                ),
                "default": False,
            },
            "blocked_reason": {
                "type": "string",
                "description": (
                    "If incomplete=true, the specific reason. Required when "
                    "incomplete=true; ignored otherwise."
                ),
                "default": "",
            },
        },
        "additionalProperties": False,
    },
}
```

The tool's *executor* is a thin local function that just stores the payload on a context object and signals "terminate" — it does not roundtrip to a service. The runner sees a successful `tool_use` for `submit_final_report`, records the input, and exits the loop.

**Why a tool, not structured output via `output_format`:** `output_format` is a final-message-only contract; it doesn't help when the model never produces a final message (failure path #1). A tool, by contrast, can be **forced** via `tool_choice` on a specific turn and gives us a clean place to short-circuit the loop.

### Mechanism 2 — Tool-choice gating compatible with extended thinking

Anthropic's docs are explicit: forced `tool_choice: {"type": "tool", "name": ...}` is incompatible with extended thinking — only `auto` and `none` are allowed when thinking is on.

So the runner does **not** force the tool on every turn. The schedule:

| Turn | `tool_choice` | Effect |
|---|---|---|
| 1 .. (max_tool_calls - 2) | `auto` (or omitted) | Normal loop. `submit_final_report` is in toolset, model may call it whenever it's actually done. Thinking still works. |
| max_tool_calls - 1 (penultimate) | `{"type": "tool", "name": "submit_final_report"}` if thinking is OFF for this model; otherwise `auto` with a strong reminder appended to the user message: "Your next response must call submit_final_report." | Force terminal call when possible; nudge when not. |
| max_tool_calls (final) | tool stripped from toolset, `tool_choice="none"`, only-if-still-loop | Last-ditch text response (rare; counts as `incomplete_no_terminal`). |

**Implementation note:** the existing tool-call-limit branch at [`runner.py:1376-1388`](../../nous/api/runner.py) already runs a "final no-tools call." That branch becomes dead code for subtasks — replaced by the schedule above.

### Mechanism 3 — The four-part brief

`spawn_task`'s tool schema gains three new fields, all optional but strongly encouraged:

| Field | Type | Default if absent |
|---|---|---|
| `output_format` | string | Frame-derived default (see table below) |
| `success_criteria` | string | `"The summary directly addresses the task and is internally consistent."` |
| `boundaries` | string | `"Do not spawn further subtasks. Do not modify files unless the task explicitly requires it. Cap tool calls per the runner limit."` |

Frame-derived `output_format` defaults:

| frame_type | Default `output_format` |
|---|---|
| `task` | "Concise summary of what was done + verification of success." |
| `research` | "Synthesis of findings, with key facts in `findings[]` and sources in `evidence_refs[]`." |
| `decision` | "Decision recommendation + reasoning + confidence; record the decision via `record_decision` and reference its ID in `evidence_refs[]`." |
| `debug` | "Root cause + fix suggestion + verification steps." |
| `conversation` | "Direct natural-language answer to the question." |
| (any other / null) | "Free-form summary appropriate to the task." |

These are woven into the system prompt by a rewritten `build_subtask_prefix(task, frame_type, output_format, success_criteria, boundaries)`:

```
You are a Nous subtask agent. Your ONLY way to terminate is to call the
submit_final_report tool with a schema-valid payload.

# Objective
{task}

# Output format
{output_format}

# Success criteria
{success_criteria}

# Boundaries
{boundaries}

# Frame
{frame_type} — apply {frame_type}-appropriate reasoning and tool usage.

# Termination
When you are done — and ONLY when done — call submit_final_report. Do not
produce a final text-only response; the parent agent will read only the
report payload. If you genuinely cannot complete the task, call
submit_final_report with incomplete=true and a specific blocked_reason.
```

### Mechanism 4 — Structural validator (no LLM)

After the tool loop returns the report payload, the worker validates **structurally**:

```python
def validate_report(payload: dict | None, output_format: str) -> ValidationResult:
    if payload is None:
        return ValidationResult.fail("no_terminal_call",
            "Subtask exited without calling submit_final_report.")
    try:
        report = SubtaskReport.model_validate(payload)  # pydantic schema
    except ValidationError as e:
        return ValidationResult.fail("schema_invalid", str(e))
    if report.incomplete:
        return ValidationResult.incomplete(report.blocked_reason or "no_reason_given")
    summary = report.summary.strip()
    if len(summary) < 50:
        return ValidationResult.fail("summary_too_short", f"len={len(summary)}")
    if _is_placeholder(summary):  # "TODO", "I will...", "Let me...", lorem-ipsum patterns
        return ValidationResult.fail("placeholder_summary", summary[:100])
    if report.confidence < 0.0 or report.confidence > 1.0:
        return ValidationResult.fail("confidence_out_of_range", str(report.confidence))
    return ValidationResult.ok(report)
```

`_is_placeholder` is a small regex-based heuristic — **not** an LLM call. Conservative: only flags obviously-placeholder text ("TODO:", "Lorem ipsum", "I will research and report back", a stop-word-only summary). Detailed test fixtures in plan §6.

### Mechanism 5 — Bounded retry with validator feedback

```
attempt 1:
    run tool loop
    payload = collected via submit_final_report (or None if loop exited without)
    result = validate_report(payload, output_format)
    if result.ok:
        outcome = "completed"
        break
    elif result.outcome in {"no_terminal_call", "schema_invalid", "summary_too_short", "placeholder_summary"}:
        if attempts_remaining > 0:
            user_message_for_retry = (
                "Your previous attempt was rejected: " + result.reason + "\n"
                "Your prior payload was:\n" + json.dumps(payload, indent=2)[:2000] + "\n"
                "Try again. You MUST call submit_final_report with a valid payload."
            )
            attempts_remaining -= 1
            continue
        outcome = result.outcome  # "incomplete_no_terminal" / "validation_failed"
    elif result.incomplete:
        outcome = "incomplete_blocked"  # subtype of completed-with-failure
        break
    else:  # error/timeout/cancel — surfaced from outer try
        outcome = exception_class
        break
```

Cap = 1 retry (so 2 total attempts). Configurable via `NOUS_SUBTASK_MAX_ATTEMPTS` (default `2`, min `1`).

**No retry on:** `timed_out`, `errored` (httpx / API failure), `cancelled`. These already cost the full budget once; doubling spend on infrastructure failures is wasteful. Operators who want infrastructure retry should run a sibling subtask manually.

### Mechanism 6 — Five-state outcome enum

`final_outcome` column, NOT NULL after migration, values:

| Value | Meaning | `status` it maps to |
|---|---|---|
| `completed` | Validated report received | `completed` |
| `incomplete_blocked` | Agent called `submit_final_report(incomplete=true, blocked_reason=...)` | `completed` (with structured reason; treat as soft-fail in DAG) |
| `incomplete_no_terminal` | Loop exited (max turns / max tool calls) without calling the tool | `failed` |
| `validation_failed` | Tool was called, but payload failed schema/length/placeholder check on both attempts | `failed` |
| `timed_out` | `asyncio.wait_for` expired | `failed` |
| `errored` | Uncaught exception (API 5xx, network, etc.) | `failed` |
| `cancelled` | `CancelledError` propagated (shutdown, user cancel) | `cancelled` |

The legacy `status` column is kept (DAG and external API consumers depend on it). `final_outcome` is the richer signal new consumers should read.

### Mechanism 7 — Schema additions

New columns on `heart.subtasks` (migration `041_subtask_hardening.sql`):

```sql
ALTER TABLE heart.subtasks
    ADD COLUMN report_jsonb JSONB,
    ADD COLUMN final_outcome VARCHAR(32),
    ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN tokens_in BIGINT NOT NULL DEFAULT 0,
    ADD COLUMN tokens_out BIGINT NOT NULL DEFAULT 0,
    ADD COLUMN tool_calls_made INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN output_format TEXT,
    ADD COLUMN success_criteria TEXT,
    ADD COLUMN dag_node_id UUID NULL REFERENCES nous_system.dag_nodes(id) ON DELETE SET NULL;

CREATE INDEX idx_subtasks_outcome ON heart.subtasks (final_outcome, completed_at DESC);
CREATE INDEX idx_subtasks_dag_node ON heart.subtasks (dag_node_id) WHERE dag_node_id IS NOT NULL;
```

`final_outcome` is nullable (existing rows pre-migration will be NULL); new rows are required to populate it. The CHECK constraint on permissible values is added in a follow-up migration after backfill.

`dag_node_id` reverse-link: today `dag_nodes.subtask_id → subtasks.id` exists. Adding the reverse link lets the dashboard show "subtask reports for DAG node X" in one query.

### Mechanism 8 — `_format_subtask_results` defensive skip

`nous/cognitive/layer.py:71-94` rewritten so an empty / un-validated result never reaches the parent. The structural fix is upstream (worker won't persist empty), but a defensive check here protects against pre-migration rows and any future bug:

```python
def _format_subtask_results(subtasks: list) -> str:
    lines = []
    for s in subtasks:
        if s.status == "completed":
            # Use validated report if present; fall back to legacy result text.
            report = s.report_jsonb
            if report and report.get("summary", "").strip():
                lines.append("=== Completed Subtask ===")
                lines.append(f"[subtask-{s.id.hex[:8]}] Task: {s.task}")
                lines.append(f"Summary: {report['summary']}")
                if report.get("findings"):
                    lines.append("Findings:")
                    for f in report["findings"][:5]:
                        lines.append(f"  - {f}")
                if report.get("next_actions"):
                    lines.append("Recommended next actions:")
                    for a in report["next_actions"][:3]:
                        lines.append(f"  - {a}")
                lines.append(f"Confidence: {report.get('confidence', 0.0):.2f}")
                lines.append("")
            elif s.result and s.result.strip():
                # Legacy / pre-F061 row.
                lines.append("=== Completed Subtask ===")
                lines.append(f"[subtask-{s.id.hex[:8]}] Task: {s.task}")
                lines.append(f"Result: {s.result}")
                lines.append("")
            # else: empty completed row — skip silently. Caller marks delivered anyway.
        elif s.status == "failed":
            lines.append("=== Failed Subtask ===")
            lines.append(f"[subtask-{s.id.hex[:8]}] Task: {s.task}")
            lines.append(f"Outcome: {s.final_outcome or 'unknown'}")
            if s.error:
                lines.append(f"Reason: {s.error}")
            lines.append("")
    return "\n".join(lines).strip()
```

The caller at `layer.py:563-571` is updated so empty completions are still marked delivered (so they don't loop forever) but produce no injected text.

### Mechanism 9 — Bootstrap-vs-work timeout split

Today: single `timeout_seconds` covers everything. Research surfaced a recurring failure class where bootstrap (memory loading, embedding, cache warm) eats the entire budget. F061 splits:

```
NOUS_SUBTASK_BOOTSTRAP_TIMEOUT = 30  # seconds — pre_turn through first API call
NOUS_SUBTASK_WORK_TIMEOUT = 570       # seconds — first API call through report
```

Total still bounded by `subtask.timeout_seconds` (default 600). Implementation: the worker tracks `bootstrap_done_at` (set when the first API response is received). If `now - started_at > bootstrap_timeout AND bootstrap_done_at is None`, fail with `final_outcome='timed_out_bootstrap'` (a sub-variant of `timed_out`).

This is **opt-in and observability-first**: separate counters. The hard outer timeout is unchanged; this just labels which phase exhausted the budget.

### Mechanism 10 — Telemetry to `nous_system.events`

Following the F026 / F059 / F051 pattern, every terminal subtask emits a structured event:

```python
event_type = "subtask_outcome"
data = {
    "subtask_id": str(subtask.id),
    "agent_id": subtask.agent_id,
    "frame_type": subtask.frame_type,
    "final_outcome": final_outcome,    # five-state enum
    "attempts": attempts,
    "tokens_in": tokens_in,
    "tokens_out": tokens_out,
    "tool_calls_made": tool_calls_made,
    "duration_ms": duration_ms,
    "validator_reason": validator_reason or None,
    "dag_node_id": str(subtask.dag_node_id) if subtask.dag_node_id else None,
}
```

Fire-and-forget via `asyncio.create_task` so the worker hot path never blocks. Mirrors `NOUS_F026_PERSISTENCE_ENABLED` env-flag pattern; new flag `NOUS_SUBTASK_OUTCOME_PERSISTENCE_ENABLED` (default `true` once F061 ships, but tied to master flag).

### Mechanism 11 — Dashboard `/dashboard/subtasks`

New endpoint. Cards:

- **Outcomes (last 24h / 7d):** stacked bar of the five-state enum.
- **Empty-rate trend:** `incomplete_no_terminal + validation_failed / total_completed_attempts` over time.
- **Retry rate:** fraction of subtasks where `attempts > 1`.
- **Tokens / outcome:** mean tokens by outcome (catches "errored subtasks burn full budget" anti-pattern).
- **Top failing tasks:** group by `task[:80]` text, list highest-failure-rate prompts (helps operator spot bad upstream callers).
- **DAG correlation:** count of subtasks with `dag_node_id IS NOT NULL` and outcome breakdown.

Backed by `nous/api/dashboard_queries.py::subtask_dashboard()` — pure SQL aggregations on the new columns + `nous_system.events`.

### Mechanism 12 — Eval harness scenarios

Under `nous_eval/subtask_scenarios.yaml` (new file). 20 cases across:

| Category | n | Example |
|---|---|---|
| Single-fact research | 4 | "What's the latest stable Postgres version?" |
| Code-find | 4 | "Find every call site of `Heart.recall` and list them." |
| Decision-recall | 3 | "What did we decide about MMR weight in F030?" |
| Multi-step synthesis | 4 | "Compare F042 vs F043 on precision and tokens, recommend a default." |
| Adversarial / blocked | 3 | "Read /etc/shadow." (must return `incomplete=true`, blocked_reason) |
| Empty-trap | 2 | Tasks designed to make the model want to skip the report (e.g., "Echo this token"). |

Each case has a YAML rubric: required `summary` substrings, expected `final_outcome`, expected `incomplete` flag, max attempts. Run via `python -m nous_eval.subtask_outcomes` writing `reports/eval_subtask_outcomes_<timestamp>.{json,md}`. Threshold gate (F050-style):

- Aggregate `final_outcome=completed` rate ≥ **85%**
- `incomplete_no_terminal` rate ≤ **5%**
- `validation_failed` rate ≤ **3%**
- Tokens-per-completed-subtask within ±20% of baseline

---

## Configuration

New `Settings` fields (all `NOUS_SUBTASK_*`):

| Variable | Default | Description |
|---|---|---|
| `NOUS_SUBTASK_HARDENING_ENABLED` | `false` | Master flag. When false, all F061 code paths are bypassed; legacy behavior intact. |
| `NOUS_SUBTASK_MAX_ATTEMPTS` | `2` | Attempts including the original (so default = 1 retry). Min 1, max 3. |
| `NOUS_SUBTASK_REPORT_MIN_SUMMARY_CHARS` | `50` | Minimum stripped-summary length to pass the validator. |
| `NOUS_SUBTASK_REPORT_PLACEHOLDER_PATTERNS` | (built-in list) | Regex list for the placeholder-summary check. |
| `NOUS_SUBTASK_BOOTSTRAP_TIMEOUT` | `30` | Seconds for the bootstrap phase (pre_turn → first API response). |
| `NOUS_SUBTASK_WORK_TIMEOUT` | `570` | Seconds for the work phase (first API response → terminal tool). Sum bounded by outer `timeout_seconds`. |
| `NOUS_SUBTASK_OUTCOME_PERSISTENCE_ENABLED` | `true` | Emit `subtask_outcome` events to `nous_system.events`. |
| `NOUS_SUBTASK_FORCE_TOOL_ON_PENULTIMATE` | `true` | If true, runner sets `tool_choice` on penultimate turn. Disabled automatically if extended thinking is on. |

---

## Interaction with adjacent features

| Feature | Interaction | Required change |
|---|---|---|
| **F009 schedules** (`task_scheduler.py:91`) | Schedules call `heart.subtasks.create(task=...)` with no `output_format`. | Worker fills frame-derived defaults at execute-time. **No schedule code change required.** |
| **F038 DAG `subtask` nodes** (`dag/orchestrator.py:633`) | DAG creates subtasks with no `output_format`. | Same default-synthesis path. **`_sync_subtask_node` (`dag/orchestrator.py:240-282`) MUST be updated** to read `subtask.final_outcome` (not just `status`) and treat `incomplete_blocked` AND `incomplete_no_terminal` AND `validation_failed` as DAG node failures. Without this change, blocked / silent-empty subtasks silently advance the DAG even after F061. **DAG behavior change:** subtasks that previously persisted `result=""` and advanced the DAG will now fail the DAG node. This is a desirable surfacing of silent failures, but flag-gated to allow ramp. |
| **F048 background streaming** | Subtask runs use `is_background=True`. F061 changes do not touch that path. | None. |
| **F049 session cleanup** | Subtask `finally` block already calls `end_conversation`. F061 adds `submit_final_report` reception inside the inner try. | Inner try grows; finally is unchanged. |
| **F026 action gating / claim verification** | Subtask agents call tools that go through F026. Identical for F061. | None. |
| **F036 prompt caching** | New system prompt template breaks current cache for subtask runs (one-time). | Acceptable; cache rebuilds within hours. Document in rollout notes. |
| **Heartbeat (F034.x)** | Independent code path. | None. |
| **`run_python` programmatic tool** | Independent code path. | None. |

---

## Design rationale

### Why a forced terminal tool instead of structured-output JSON?

`output_format` is a contract on the *final assistant message*. It does not help when the loop exits without producing a final message — which is the dominant failure today (failure path #1). A tool can be:
- Required in the schema (parent literally cannot terminate cleanly without calling it).
- Forced via `tool_choice` on a chosen turn.
- Inspected as `tool_use` in the assistant content, which is structurally distinct from `text` and immune to "model wrote prose instead of JSON."

Anthropic's multi-agent research post (2025) and CrewAI's `output_pydantic` / `guardrail` pattern (2025) both converge on the tool-style termination contract for production sub-agents.

### Why no LLM critic?

Snorkel's [self-critique paradox study](https://snorkel.ai/blog/the-self-critique-paradox-why-ai-verification-fails-where-its-needed-most/) measured LLM self-critique driving 98%-accurate runs to 57% — the verifier injects errors on tasks the model was already solving. The structural validator (schema + length + placeholder regex) fails-closed on bad output without paying that tax.

### Why exactly one retry?

Two-retry policy doubled token spend in CrewAI benchmarks while only catching ~6% additional cases. One retry catches the bulk (model just forgot the tool; reminder fixes it) without unbounded cost. Configurable for users who want different trade-off.

### Why not also fix the heartbeat path?

Heartbeat checks have a fundamentally different contract: they fire findings, don't return a payload to a parent. Their "empty result" is benign (just no finding this tick). Forcing a tool there would create noise. F061 stays scoped.

### Why opt-in via flag?

The DAG behavior change (silent-empty → failed) will surface previously-hidden bad nodes immediately on rollout. Operators need a quiet window to inspect, fix upstream prompts, and ramp.

---

## Rollout

1. **PR-1: Schema + flag.** Migration `041_subtask_hardening.sql`, settings, ORM model. Default flag `false`. No behavior change.
2. **PR-2: Worker contract.** `submit_final_report` tool, validator, retry, prompt rewrite, defensive skip in `_format_subtask_results`. Gated entirely by `NOUS_SUBTASK_HARDENING_ENABLED`. Tests cover both flag states.
3. **PR-3: Telemetry + dashboard.** `nous_system.events` emission, `/dashboard/subtasks`. Read-only — does not affect execution.
4. **PR-4: Eval harness.** `nous_eval/subtask_outcomes.py` + 20 scenarios + nightly runner.
5. **PR-5: Flag flip.** After 5+ days of clean eval runs and dashboard inspection, flip `NOUS_SUBTASK_HARDENING_ENABLED` default to `true`. Document the DAG behavior change in CHANGELOG and migration notes.
6. **PR-6 (cleanup, deferred):** Once stable for 2 weeks, add the CHECK constraint on `final_outcome` enum values; delete the legacy "no validator" code path.

**Rollback:** Flip `NOUS_SUBTASK_HARDENING_ENABLED=false` and restart workers. Schema additions are forward-only but harmless on rollback (NULLs accepted).

---

## Success metrics (post-flip)

Measured weekly via `/dashboard/subtasks`:

- `final_outcome=completed` rate ≥ **90%** of all terminal subtasks (was unknown / no telemetry).
- `incomplete_no_terminal` rate ≤ **3%**.
- `validation_failed` rate ≤ **2%**.
- Mean retries per subtask ≤ **0.15** (i.e., < 15% of subtasks retry).
- Median tokens per `completed` subtask within ±20% of pre-F061 baseline.
- Zero "Result: " (empty) injections into parent context (verifiable by grep on `nous_system.events` of type `subtask_result_injected` if added in PR-3).

---

## Open questions (deferred)

- **Should `record_decision` and `learn_fact` calls inside a subtask carry the subtask's `dag_node_id`?** That would let the dashboard trace memory writes back to DAG nodes. Likely yes, but a separate small follow-up.
- **Forwarding-verbatim vs parent-paraphrase.** Current `_format_subtask_results` paraphrases (composes a "Summary:" / "Findings:" block). Some callers may want the report verbatim. Deferred — needs concrete use case.
- **Per-frame distinct timeout defaults.** Research subtasks need more bootstrap (memory loading); conversation subtasks need less. Out of scope for v1; flat defaults first.

---

## References

- Anthropic — [Multi-agent research system](https://www.anthropic.com/engineering/multi-agent-research-system) — objective + output_format + tools + boundaries contract.
- Anthropic — [Tool-choice cookbook](https://platform.claude.com/cookbook/tool-use-tool-choice) — forced tool_choice + extended-thinking constraint.
- CrewAI — [Tasks](https://docs.crewai.com/en/concepts/tasks) — `output_pydantic` + `guardrail_max_retries`.
- Snorkel — [Self-critique paradox](https://snorkel.ai/blog/the-self-critique-paradox-why-ai-verification-fails-where-its-needed-most/) — why LLM critics regress accuracy.
- LangGraph — [Supervisor](https://github.com/langchain-ai/langgraph-supervisor-py) — state schema as contract; verbatim forwarding tool.
- Pythagora — [5 silent failure modes](https://dev.to/zvone187/5-silent-failure-modes-in-production-ai-agents-and-how-we-instrument-for-them-oca) — semantically empty success taxonomy.
- Internal: F048 background streaming spec, F049 session lifecycle spec, F051 retrieval eval harness spec.
