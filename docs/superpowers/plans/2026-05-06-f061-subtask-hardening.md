# F061 Implementation Plan — Subtask Hardening (v2, post-review)

**Spec:** `docs/features/F061-subtask-hardening.md`
**Branch:** `feat/f061-subtask-hardening`
**Date:** 2026-05-06 (v2 after 3-agent review)

## Revisions in v2 vs v1

Three reviewers (architecture / silent-failure-hunter / code-conventions) returned APPROVE_WITH_REVISIONS with 14 distinct P1s and 12 P2s. v2 incorporates all P1s plus the user-approved design choices on four open questions.

**P1 fixes baked into v2:**
- **Inline `await_result=true` path is hardened with the same executor as the worker** (Q2: refactor — both paths share `_execute_hardened`). This was the most critical finding.
- **`extra_tools` dispatch is plumbed through `_tool_loop` per-call** (not via `dispatcher.register()` which would leak into chat sessions on crash).
- **`_tool_loop` returns `tool_calls` count in the `usage` dict** so `tool_calls_made` actually populates.
- **`mark_delivered` fires unconditionally** for all undelivered IDs (Q1: simple unconditional, no `redelivery_attempts` column). After F061 ships, empty-completed rows are essentially impossible; this is purely defensive for legacy rows.
- **DAG `_sync_subtask_node` reads `final_outcome`** and treats `incomplete_blocked` / `incomplete_no_terminal` / `validation_failed` as node failures. `dag/orchestrator.py` is now in PR-2 file scope.
- **Step 1.5: SubtaskManager API extension** added to PR-1 — `create()` / `complete()` / `fail()` grow optional kwargs; PR-2 plumbs them.
- **Eval harness uses `await_result=true` inline mode** — no need to start a worker pool in the eval container.
- **Migration uses `DROP CONSTRAINT IF EXISTS ... ; ADD CONSTRAINT ...`** for FK idempotency.
- **NO `validation_alias`** — plain `Field(default=...)` per the F046 PR #318 lesson; rely on `env_prefix='NOUS_'`.
- **`UUID(as_uuid=True)`** direct (no `PG_UUID` alias) per `models.py` convention.
- **`Integer`** for `tokens_in` / `tokens_out` (Q4: not `BigInteger`) — matches dominant convention.
- **Per-attempt try/except** wraps `run_turn` so `last_result` is never `None`; initialized to a sentinel before the loop.
- **`SubtaskReportCollector` first-call-locks** — second submission returns an error string from the executor; first payload preserved.
- **`_emit_outcome_event` wraps body in try/except + `logger.exception`** so DB-write failures are loud, not silent.
- **Tests use the flat `tests/test_*.py` layout** (Q3) per dominant convention; do NOT create new `tests/api/`, `tests/cognitive/`, `tests/migrations/`, `tests/storage/` subdirs.

**P2 items folded into v2 inline (no separate fix step):** placeholder regex test fence, `incomplete_blocked` rendering branch in `_format_subtask_results`, eval gate `advisory|enforcing` mode flag, retry-message bounded length + plain-prose construction, CLAUDE.md env-var enumeration, Telegram notification × new outcomes test cases, thinking detection clarified to `settings.thinking_mode == "off" or "haiku" in effective_model`.

**Deferred to follow-up (P3):** truncation indicators on `findings[:5]`, concurrent-collector test (worth a one-line code comment confirming SKIP LOCKED safety), bootstrap-measurement-point clarification (label-only, doesn't affect correctness), defense-in-depth comments.

---

## Pre-flight verification (run before PR-1)

```bash
# Confirm citations match HEAD
grep -n "def _execute_subtask"        nous/handlers/subtask_worker.py
grep -n "def _extract_text"           nous/api/runner.py
grep -n "Subtask tool call limit"     nous/api/runner.py
grep -n "_format_subtask_results"     nous/cognitive/layer.py
grep -n "build_subtask_prefix"        nous/api/tools.py
grep -n "_launch_subtask_node\|_sync_subtask_node"  nous/dag/orchestrator.py
grep -n "subtasks.create"             nous/handlers/task_scheduler.py
grep -n "async def create\|async def complete\|async def fail"  nous/heart/subtasks.py
ls sql/migrations/ | tail -5          # confirm 040 is latest, next is 041
grep -n "BigInteger\|PG_UUID"         nous/storage/models.py   # expect zero hits
grep -n "validation_alias"            nous/config.py           # F046-cleaned section
ls tests/                              # confirm flat layout
```

Expected:
- `subtask_worker.py::_execute_subtask` lines 129–196.
- `runner.py:1376–1388` is tool-call-limit branch; `:1604–1610` is `_extract_text`.
- `cognitive/layer.py:71–94` `_format_subtask_results`; `:563–571` caller.
- `tools.py:1069–1086` `build_subtask_prefix`; `:1218–1234` inline subtask path.
- `dag/orchestrator.py:240–282` `_sync_subtask_node`; `:618–663` `_launch_subtask_node`.
- `task_scheduler.py:91` `subtasks.create(task=...)`.
- `heart/subtasks.py:27–125` `create`/`complete`/`fail`.
- Latest migration `040_*.sql`.
- `models.py` has no `BigInteger` / `PG_UUID` references.
- `tests/` is flat (no `tests/api/`, etc.).

Drift → stop and re-validate before writing code.

---

## PR-1 — Schema, settings, ORM model, SubtaskManager API extension

**Goal:** all surfaces required by PR-2 exist with default flag `false`. Zero behavior change.

### 1.1 Migration

**New file:** `sql/migrations/041_subtask_hardening.sql`

```sql
-- F061: Subtask Hardening — schema additions.
-- Backward-compatible: all new columns NULL or DEFAULT'd.
-- CHECK constraint on final_outcome deferred to migration 042 after backfill.

ALTER TABLE heart.subtasks
    ADD COLUMN IF NOT EXISTS report_jsonb     JSONB,
    ADD COLUMN IF NOT EXISTS final_outcome    VARCHAR(32),
    ADD COLUMN IF NOT EXISTS attempts         INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS tokens_in        INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS tokens_out       INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS tool_calls_made  INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS output_format    TEXT,
    ADD COLUMN IF NOT EXISTS success_criteria TEXT,
    ADD COLUMN IF NOT EXISTS dag_node_id      UUID NULL;

-- FK idempotency: DROP first, then ADD. Postgres has no ADD CONSTRAINT IF NOT EXISTS.
-- Cross-schema FK (heart -> nous_system); selective `pg_dump --schema=heart` restores
-- require nous_system.dag_nodes to exist first. ON DELETE SET NULL preserves subtask
-- history when a DAG is deleted.
ALTER TABLE heart.subtasks
    DROP CONSTRAINT IF EXISTS fk_subtasks_dag_node;
ALTER TABLE heart.subtasks
    ADD CONSTRAINT fk_subtasks_dag_node
    FOREIGN KEY (dag_node_id) REFERENCES nous_system.dag_nodes(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_subtasks_outcome
    ON heart.subtasks (final_outcome, completed_at DESC);
CREATE INDEX IF NOT EXISTS idx_subtasks_dag_node
    ON heart.subtasks (dag_node_id) WHERE dag_node_id IS NOT NULL;

COMMENT ON COLUMN heart.subtasks.report_jsonb IS
    'F061: Validated SubtaskReport from submit_final_report tool. NULL for legacy / pre-flag rows.';
COMMENT ON COLUMN heart.subtasks.final_outcome IS
    'F061: Outcome enum: completed, incomplete_blocked, incomplete_no_terminal, validation_failed, timed_out, errored, cancelled. NULL for pre-flag rows.';
```

**Test:** `tests/test_f061_migration_041.py`
- Apply against fresh test DB; verify all 9 columns present with correct types.
- Verify FK constraint name `fk_subtasks_dag_node` exists.
- Verify both indexes exist.
- Idempotency: re-apply the migration; should not error.
- Precondition probe: `SELECT 1 FROM information_schema.tables WHERE table_schema='nous_system' AND table_name='dag_nodes'` — fail with clear error if false.

### 1.2 ORM model

**File:** `nous/storage/models.py::Subtask`

```python
# Add inside the existing Subtask class. Use Integer (not BigInteger) per convention.
report_jsonb:     Mapped[dict | None]      = mapped_column(JSONB, nullable=True)
final_outcome:    Mapped[str | None]       = mapped_column(String(32), nullable=True)
attempts:         Mapped[int]              = mapped_column(Integer, nullable=False, default=0)
tokens_in:        Mapped[int]              = mapped_column(Integer, nullable=False, default=0)
tokens_out:       Mapped[int]              = mapped_column(Integer, nullable=False, default=0)
tool_calls_made:  Mapped[int]              = mapped_column(Integer, nullable=False, default=0)
output_format:    Mapped[str | None]       = mapped_column(Text, nullable=True)
success_criteria: Mapped[str | None]       = mapped_column(Text, nullable=True)
dag_node_id:      Mapped[uuid.UUID | None] = mapped_column(
    UUID(as_uuid=True),
    ForeignKey("nous_system.dag_nodes.id", ondelete="SET NULL"),
    nullable=True,
)
```

`UUID` and `JSONB` already imported in `models.py:19`. `uuid` already imported at `models.py:3`. Add `Text` to the `sqlalchemy` import line at `models.py:7-18` if not present (verify before edit; it is currently used at e.g. `models.py:142`).

**Test:** `tests/test_f061_subtask_model.py`
- Round-trip a `Subtask` with all new fields populated.
- Defaults on minimal insert: `attempts=0`, `tokens_in=0`, `tokens_out=0`, `tool_calls_made=0`, others NULL.

### 1.3 Settings

**File:** `nous/config.py`

Add 7 fields under the existing `# Subtask configuration` block (~line 409). The 8th setting (`nous_eval_subtask_gate_mode`) belongs on `EvalSettings` and lands with the harness in PR-4:

```python
subtask_hardening_enabled:               bool = False
subtask_max_attempts:                    int  = Field(default=2, ge=1, le=3)
subtask_report_min_summary_chars:        int  = Field(default=50, ge=1)
subtask_bootstrap_timeout:               int  = Field(default=30, ge=1)
subtask_work_timeout:                    int  = Field(default=570, ge=1)
subtask_outcome_persistence_enabled:     bool = True
subtask_force_tool_on_penultimate:       bool = True
# placeholder_patterns not env-tunable in v1 — built-in regex list lives in subtask_validator.py
```

**No `validation_alias`.** `model_config` already declares `env_prefix="NOUS_"` (`config.py:15-16`); plain field names are how every other `subtask_*` field is configured (see `config.py:409-414`).

**Test:** `tests/test_f061_config.py`
- Defaults match.
- `NOUS_SUBTASK_HARDENING_ENABLED=true` env override sets True.
- `NOUS_SUBTASK_MAX_ATTEMPTS=4` raises `ValidationError` (ge=3).

### 1.4 Pydantic report schema

**New file:** `nous/heart/subtask_report.py`

```python
"""F061: SubtaskReport — payload of the submit_final_report tool."""
from __future__ import annotations
from pydantic import BaseModel, ConfigDict, Field


class SubtaskReport(BaseModel):
    """Validated payload an agent submits to terminate a subtask.

    NOTE: extra='forbid' is intentionally a new pattern not used in
    nous/heart/schemas.py or nous/brain/schemas.py — needed here because the
    payload is adversarial (the model may invent fields like
    "confidence_level" instead of "confidence"). Validator-fronting schemas
    in F061+ may use this pattern; existing internal schemas remain bare.

    The summary length floor (default 50 chars) is enforced by the structural
    validator (subtask_validator.py), NOT by this Pydantic model — keep
    min_length=1 here so the validator owns the threshold and can be tuned
    via NOUS_SUBTASK_REPORT_MIN_SUMMARY_CHARS.
    """
    model_config = ConfigDict(extra="forbid")

    summary:        str        = Field(min_length=1)
    findings:       list[str]  = Field(default_factory=list)
    next_actions:   list[str]  = Field(default_factory=list)
    confidence:     float      = Field(ge=0.0, le=1.0)
    evidence_refs:  list[str]  = Field(default_factory=list)
    incomplete:     bool       = False
    blocked_reason: str        = ""
```

**Test:** `tests/test_f061_subtask_report.py` — round-trip; reject extra fields; reject confidence out of range.

### 1.5 SubtaskManager API extension

**File:** `nous/heart/subtasks.py`

Extend `create()`, `complete()`, `fail()` with optional kwargs (defaults preserve existing call sites):

```python
async def create(
    self,
    task: str,
    *,
    parent_session_id: str | None = None,
    priority: str = "normal",
    timeout: int = 120,
    notify: bool = False,
    metadata: dict | None = None,
    frame_type: str | None = None,
    model: str | None = None,
    # F061 additions (all optional; NULL persisted when None)
    output_format: str | None = None,
    success_criteria: str | None = None,
    dag_node_id: "uuid.UUID | None" = None,
) -> Subtask: ...

async def complete(
    self,
    subtask_id: "uuid.UUID",
    result: str,
    *,
    final_outcome: str | None = None,
    report_jsonb: dict | None = None,
    attempts: int | None = None,
    tokens_in: int | None = None,
    tokens_out: int | None = None,
    tool_calls_made: int | None = None,
) -> None: ...

async def fail(
    self,
    subtask_id: "uuid.UUID",
    error: str,
    *,
    final_outcome: str | None = None,
    report_jsonb: dict | None = None,
    attempts: int | None = None,
    tokens_in: int | None = None,
    tokens_out: int | None = None,
    tool_calls_made: int | None = None,
) -> None: ...
```

When new kwargs are `None`, the column is left at its DB default (so legacy callers continue to work and existing rows are unchanged). When provided, they overwrite via the UPDATE statement.

**Tests added to existing** `tests/test_subtasks.py`:
- `create()` with all new kwargs persists them.
- `create()` without new kwargs: row has NULL `output_format`, `success_criteria`, `dag_node_id`; `0` for token/attempt counters.
- `complete()` with `final_outcome='completed'` + `report_jsonb={...}` updates correctly.
- Backward compat: `task_scheduler.py:91`-style `create(task=..., parent_session_id=..., priority=..., timeout_seconds=...)` works unchanged (smoke test).

### 1.6 Acceptance for PR-1

- `uv run pytest tests/test_f061_*.py tests/test_subtasks.py tests/test_config.py -v` all pass.
- Full test suite passes.
- Manual: `docker compose down -v && docker compose up -d`; observe migration log; query `\d heart.subtasks`.

---

## PR-2 — Worker contract: tool, prompt, validator, retry, inline-path harden, DAG branch

**Goal:** the heart of F061. Entirely behind `NOUS_SUBTASK_HARDENING_ENABLED`. Flag off → byte-identical to today.

### 2.1 The terminal tool + collector

**New file:** `nous/api/subtask_tools.py`

```python
"""F061: submit_final_report tool — terminal contract for hardened subtasks."""
from __future__ import annotations
import logging
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)


SUBMIT_FINAL_REPORT_SCHEMA: dict[str, Any] = {
    "name": "submit_final_report",
    "description": (
        "Submit your final, complete report for this subtask. You MUST call "
        "this tool exactly once when you are done. Do not produce a final "
        "text-only response — the parent agent receives ONLY this tool's "
        "payload."
    ),
    "input_schema": {  # full schema from spec mechanism 1
        "type": "object",
        "required": ["summary", "confidence"],
        "properties": {
            "summary":        {"type": "string",  "minLength": 50},
            "findings":       {"type": "array",   "items": {"type": "string"}, "default": []},
            "next_actions":   {"type": "array",   "items": {"type": "string"}, "default": []},
            "confidence":     {"type": "number",  "minimum": 0.0, "maximum": 1.0},
            "evidence_refs":  {"type": "array",   "items": {"type": "string"}, "default": []},
            "incomplete":     {"type": "boolean", "default": False},
            "blocked_reason": {"type": "string",  "default": ""},
        },
        "additionalProperties": False,
    },
}


class SubtaskReportCollector:
    """Stores the FIRST submit_final_report payload; locks against double-submit.

    Per F061 review P1.4: a model that calls submit_final_report twice in one
    turn must NOT overwrite the first valid payload. The first call wins; the
    executor returns an error string for any subsequent call.
    """
    __slots__ = ("_payload", "_submission_count")

    def __init__(self) -> None:
        self._payload: dict[str, Any] | None = None
        self._submission_count: int = 0

    def reset(self) -> None:
        self._payload = None
        self._submission_count = 0

    def set(self, payload: dict[str, Any]) -> bool:
        """Lock-on-first-call. Returns True if accepted, False if rejected."""
        self._submission_count += 1
        if self._payload is not None:
            logger.warning(
                "submit_final_report called %d times; ignoring duplicate (first payload locked)",
                self._submission_count,
            )
            return False
        self._payload = payload
        return True

    def get(self) -> dict[str, Any] | None:
        return self._payload

    def is_set(self) -> bool:
        return self._payload is not None


def make_submit_final_report_executor(
    collector: SubtaskReportCollector,
) -> Callable[..., Awaitable[tuple[str, bool]]]:
    """Returns an async tool executor matching ToolDispatcher's contract:
    `(result_text: str, is_error: bool)`.
    """
    async def _executor(**kwargs: Any) -> tuple[str, bool]:
        accepted = collector.set(kwargs)
        if accepted:
            return ("Report received. Subtask will terminate.", False)
        return (
            "ERROR: submit_final_report has already been called for this "
            "subtask. Do not call it again. The first payload is locked.",
            True,
        )
    return _executor
```

**Tests** (`tests/test_f061_subtask_tools.py`):
- Schema validates (round-trip via Pydantic JSON schema parser).
- Executor stores input on first call; collector.is_set() True.
- Second submission with different payload: collector still has first; executor returns is_error=True.
- `reset()` clears state.

### 2.2 The validator

**New file:** `nous/heart/subtask_validator.py`

```python
"""F061: structural validator for SubtaskReport payloads. NO LLM calls."""
from __future__ import annotations
import re
from dataclasses import dataclass

from pydantic import ValidationError

from nous.heart.subtask_report import SubtaskReport


# Each pattern is anchored at start-of-summary and case-insensitive.
# Verbs in the "I will" list are intentionally narrow: only verbs that signal
# the agent has NOT done the work yet. "I will recommend ..." is LEGITIMATE
# (it's a verdict) and must not match.
_PLACEHOLDER_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"^\s*todo[\s:]",                                          re.IGNORECASE),
    re.compile(r"^\s*lorem\s+ipsum",                                      re.IGNORECASE),
    re.compile(r"^\s*i\s+(will|am\s+going\s+to)\s+(research|investigate|analyze|look\s+into|check)\b",
               re.IGNORECASE),
    re.compile(r"^\s*let\s+me\s+(think|check|investigate|see)\b",         re.IGNORECASE),
    re.compile(r"^\s*(no\s+answer|cannot\s+answer|n/?a)\s*\.?\s*$",       re.IGNORECASE),
]


@dataclass(frozen=True, slots=True)
class ValidationResult:
    ok: bool
    outcome: str  # one of: ok, incomplete_blocked, incomplete_no_terminal, validation_failed
    reason: str = ""
    report: SubtaskReport | None = None

    @classmethod
    def passed(cls, report: SubtaskReport) -> "ValidationResult":
        return cls(ok=True, outcome="ok", report=report)

    @classmethod
    def failed(cls, outcome: str, reason: str) -> "ValidationResult":
        return cls(ok=False, outcome=outcome, reason=reason)

    @classmethod
    def incomplete(cls, blocked_reason: str, report: SubtaskReport) -> "ValidationResult":
        return cls(ok=False, outcome="incomplete_blocked",
                   reason=blocked_reason or "no_reason_given", report=report)


def validate_report(payload: dict | None, *, min_summary_chars: int) -> ValidationResult:
    if payload is None:
        return ValidationResult.failed(
            "incomplete_no_terminal",
            "Subtask exited without calling submit_final_report.",
        )
    try:
        report = SubtaskReport.model_validate(payload)
    except ValidationError as e:
        return ValidationResult.failed("validation_failed", f"schema_invalid: {e}")
    summary = report.summary.strip()
    if report.incomplete:
        return ValidationResult.incomplete(report.blocked_reason, report)
    if len(summary) < min_summary_chars:
        return ValidationResult.failed(
            "validation_failed",
            f"summary_too_short: len={len(summary)} (min {min_summary_chars})",
        )
    if any(p.search(summary[:200]) for p in _PLACEHOLDER_PATTERNS):
        return ValidationResult.failed(
            "validation_failed",
            f"placeholder_summary: {summary[:80]!r}",
        )
    return ValidationResult.passed(report)
```

**Tests** (`tests/test_f061_subtask_validator.py`) — table-driven, ~18 cases:

| Case | Expected outcome |
|---|---|
| valid 100-char summary, confidence 0.7 | passed |
| None payload | incomplete_no_terminal |
| 49-char summary | validation_failed (length) |
| 50-char summary exactly | passed |
| `"TODO: investigate the database"` | validation_failed (placeholder) |
| `"I will research and report back"` | validation_failed (placeholder) |
| **`"I will recommend that we proceed with option A based on three considerations: cost, latency, maintenance burden."`** | **passed** (positive case — `recommend` is NOT a forbidden verb) |
| `"Let me check the docs"` | validation_failed (placeholder) |
| `"Lorem ipsum dolor sit amet, consectetur adipiscing elit, sed do."` | validation_failed (placeholder) |
| confidence 1.5 | validation_failed (schema) |
| extra field `"foo": "bar"` | validation_failed (schema) |
| `incomplete=true, blocked_reason="permission denied"` + 0-char summary | incomplete (NOT failed) |
| `incomplete=true, blocked_reason=""` | incomplete with reason="no_reason_given" |
| placeholder pattern hit only at char 250 (>200) of summary | passed |
| mixed-case `"TODO: x"` | failed (case-insensitive) |
| whitespace-only summary | validation_failed (length after strip) |
| emoji-only 4-char summary | validation_failed (length) |
| valid Unicode 60-char summary | passed |

### 2.3 Subtask prefix builder rewrite

**File:** `nous/api/tools.py::build_subtask_prefix`

```python
def build_subtask_prefix(
    task: str,
    frame_type: str | None = None,
    *,
    output_format: str | None = None,
    success_criteria: str | None = None,
    boundaries: str | None = None,
    hardening_enabled: bool = False,
) -> str:
    """Build the subtask system-prompt prefix.

    When hardening_enabled is False (default), emits the legacy text — byte
    identical to pre-F061 — so non-hardened code paths are unchanged. Used by
    both inline (await_result) and worker-pool subtask execution.
    """
    from nous.api.runner import FRAME_TOOLS

    if not hardening_enabled:
        # Legacy text — DO NOT MODIFY. Removing this path is PR-6's job.
        base = (
            "You are executing a background subtask.\n"
            "Deliver a clear, complete result. Do not ask questions."
        )
        frame_instruction = ""
        if frame_type and frame_type in FRAME_TOOLS:
            frame_instruction = (
                f"\n\nFrame: {frame_type} — apply {frame_type}-appropriate "
                "reasoning and tool usage."
            )
        return f"{base}{frame_instruction}\n\nTask: {task}"

    # F061 hardened prompt
    of = output_format or _frame_default_output_format(frame_type)
    sc = success_criteria or _DEFAULT_SUCCESS_CRITERIA
    bd = boundaries or _DEFAULT_BOUNDARIES
    frame_block = ""
    if frame_type and frame_type in FRAME_TOOLS:
        frame_block = (
            f"\n# Frame\n{frame_type} — apply {frame_type}-appropriate "
            "reasoning and tool usage.\n"
        )
    return (
        "You are a Nous subtask agent. Your ONLY way to terminate is to call "
        "the submit_final_report tool with a schema-valid payload.\n\n"
        f"# Objective\n{task}\n\n"
        f"# Output format\n{of}\n\n"
        f"# Success criteria\n{sc}\n\n"
        f"# Boundaries\n{bd}\n"
        f"{frame_block}\n"
        "# Termination\n"
        "When you are done — and ONLY when done — call submit_final_report. "
        "Do not produce a final text-only response; the parent agent will read "
        "only the report payload. If you genuinely cannot complete the task, "
        "call submit_final_report with incomplete=true and a specific "
        "blocked_reason."
    )


_DEFAULT_SUCCESS_CRITERIA = (
    "The summary directly addresses the task and is internally consistent."
)
_DEFAULT_BOUNDARIES = (
    "Do not spawn further subtasks. Do not modify files unless the task "
    "explicitly requires it. Cap tool calls per the runner limit."
)


def _frame_default_output_format(frame_type: str | None) -> str:
    return {
        "task":         "Concise summary of what was done + verification of success.",
        "research":     "Synthesis of findings, with key facts in `findings[]` and sources in `evidence_refs[]`.",
        "decision":     "Decision recommendation + reasoning + confidence; record the decision via record_decision and reference its ID in `evidence_refs[]`.",
        "debug":        "Root cause + fix suggestion + verification steps.",
        "conversation": "Direct natural-language answer to the question.",
    }.get(frame_type or "", "Free-form summary appropriate to the task.")
```

**Tests** (`tests/test_f061_build_subtask_prefix.py`):
- `hardening_enabled=False` → exact-match legacy text.
- `hardening_enabled=True, frame_type="research"`, all defaults → contains research-default `output_format`.
- `hardening_enabled=True, output_format="X"` → user value wins.
- All 5 frames produce non-empty defaults; unknown frame → free-form fallback.
- All 5 frames produce a system prompt < 4000 chars (caching budget guard).

### 2.4 Runner support: extra_tools + forced tool_choice + dispatch override

**File:** `nous/api/runner.py`

`run_turn` and `_tool_loop` grow two parameters with safe defaults:

```python
async def run_turn(
    self,
    *,
    session_id: str,
    user_message: str,
    agent_id: str,
    # ... existing params ...
    extra_tools: dict[str, tuple[dict, Callable[..., Awaitable[tuple[str, bool]]]]] | None = None,
    force_tool_on_penultimate: str | None = None,
) -> tuple[str, TurnContext, dict]:
    ...
```

**Inside `_tool_loop`** (the actual change):

1. **Tool list construction.** When building the per-call `tools=[...]` list, if `extra_tools` is provided, append each `extra_tools[name][0]` schema to the list. The list is constructed fresh per-call; no global mutation.

2. **Dispatch override.** Before the existing `result_text, is_error = await self._dispatcher.dispatch(...)` (`runner.py:1314`), insert:

   ```python
   if extra_tools and tool_name in extra_tools:
       _schema, executor = extra_tools[tool_name]
       result_text, is_error = await executor(**tool_input)
   else:
       result_text, is_error = await self._dispatcher.dispatch(tool_name, tool_input, ...)
   ```

   This routes `submit_final_report` to the collector executor without touching the dispatcher. **No `dispatcher.register()` / `unregister()`** — that pattern leaks tools into chat sessions on crash.

3. **Penultimate-turn forcing.** Compute `is_penultimate = (turns + 1 == max_turns - 1) or (max_tool_calls and total_tool_calls + 1 == max_tool_calls - 1)`. When `force_tool_on_penultimate` is set, `is_penultimate` is True, AND thinking is OFF for the effective model, set `tool_choice={"type": "tool", "name": force_tool_on_penultimate}` on the next `_call_api`. Otherwise, append a fresh user-role text message immediately before that `_call_api`:

   ```python
   messages.append({
       "role": "user",
       "content": [{"type": "text", "text": (
           f"REMINDER: This is your final allowed turn. You MUST call "
           f"{force_tool_on_penultimate} now with a valid payload."
       )}],
   })
   ```

   **Thinking detection** (clarified per P2-1):
   ```python
   thinking_off = (
       self._settings.thinking_mode == "off"
       or "haiku" in (model_override or self._settings.model).lower()
   )
   ```

4. **Short-circuit on `submit_final_report` tool_use.** When the API response contains a `tool_use` block whose `name == "submit_final_report"` AND it dispatched successfully (is_error=False), exit the tool loop after recording the tool result. Do not make a follow-up API call. Return `("Report submitted.", ctx, total_usage)` — caller reads payload from the collector.

5. **`tool_calls` in returned usage** (P1-C fix). Initialize `total_usage = {"input_tokens": 0, "output_tokens": 0, "tool_calls": 0}` (`runner.py:283` AND `:840`). Increment `total_usage["tool_calls"] += len(tool_results_for_message)` at the same point `total_tool_calls += ...` is incremented (`runner.py:1373`). All return tuples include the new key.

The existing tool-call-limit branch at `runner.py:1376-1388` is **NOT removed** — it remains the safety net for callers without `force_tool_on_penultimate`. Subtasks short-circuit before reaching it in the happy path.

**Tests** (`tests/test_f061_runner_subtask_hooks.py`):
- `extra_tools` schemas appear in the tools list passed to `_call_api`; absent for normal runs.
- `submit_final_report` tool_use → loop exits without follow-up `_call_api`; `total_usage["tool_calls"] == 1`.
- Penultimate turn with thinking off → `tool_choice={"type": "tool", ...}` argument observed.
- Penultimate turn with thinking on → reminder user message inserted; no `tool_choice` override.
- Concurrent runs: two parallel `run_turn` calls with separate collectors don't cross-contaminate (use `asyncio.gather`, scripted fakes).
- `total_usage["tool_calls"]` correctly counts dispatched calls (including extra_tools).

### 2.5 Worker rewrite + inline path harden (shared executor)

**File:** `nous/handlers/subtask_worker.py`

```python
async def _execute_subtask(self, subtask: Subtask) -> None:
    """Background-worker entry point. Routes to legacy or hardened executor."""
    session_id = f"subtask-{subtask.id.hex[:8]}"
    try:
        if not self._settings.subtask_hardening_enabled:
            await self._execute_legacy(subtask, session_id)
            return
        await execute_hardened(
            subtask, session_id,
            runner=self._runner, heart=self._heart, settings=self._settings,
            emit_event=self._emit_event, notify_telegram=self._notify_telegram,
        )
    finally:
        # F049 cleanup unchanged
        await self._cleanup_session(session_id)
```

`_execute_legacy` is the existing `_execute_subtask` body (renamed, unchanged).

**New file:** `nous/handlers/subtask_executor.py`

Houses `execute_hardened` so both the worker and the inline `spawn_task` closure can call it — addressing P1-A.

```python
"""F061: shared hardened-execution helper. Used by both:
  - background worker (subtask_worker.py)
  - inline await_result=True path (api/tools.py spawn_task)
"""
from __future__ import annotations
import asyncio
import json
import logging
from typing import Any, Awaitable, Callable

from nous.api.subtask_tools import (
    SUBMIT_FINAL_REPORT_SCHEMA,
    SubtaskReportCollector,
    make_submit_final_report_executor,
)
from nous.api.tools import build_subtask_prefix
from nous.heart.subtask_report import SubtaskReport
from nous.heart.subtask_validator import validate_report, ValidationResult

logger = logging.getLogger(__name__)


async def execute_hardened(
    subtask,
    session_id: str,
    *,
    runner,
    heart,
    settings,
    emit_event: Callable[..., Awaitable[None]] | None = None,
    notify_telegram: Callable[..., Awaitable[None]] | None = None,
) -> tuple[str, ValidationResult]:
    """Run a subtask under the F061 contract. Returns (final_text, last_result).

    Caller (worker or inline) is responsible for cleanup (end_conversation).
    Persistence is done HERE — both worker and inline get correct final_outcome
    rows with no path divergence.
    """
    max_attempts = settings.subtask_max_attempts
    collector = SubtaskReportCollector()
    extra_tools = {
        "submit_final_report": (
            SUBMIT_FINAL_REPORT_SCHEMA,
            make_submit_final_report_executor(collector),
        ),
    }
    output_format = subtask.output_format  # may be None; build_subtask_prefix synthesizes default
    success_criteria = subtask.success_criteria
    system_prefix = build_subtask_prefix(
        subtask.task, subtask.frame_type,
        output_format=output_format,
        success_criteria=success_criteria,
        hardening_enabled=True,
    )

    user_message = subtask.task
    last_payload: dict[str, Any] | None = None
    # P1-L: initialize last_result to a sentinel so _persist_outcome never sees None.
    last_result = ValidationResult.failed("errored", "no attempts ran")
    total_in = total_out = total_calls = 0

    for attempt in range(1, max_attempts + 1):
        collector.reset()
        try:
            response_text, _ctx, usage = await runner.run_turn(
                session_id=session_id,
                user_message=user_message,
                agent_id=settings.agent_id,
                system_prompt_prefix=system_prefix,
                skip_episode=True,
                is_subtask=True,
                max_tool_calls=settings.subtask_tool_call_limit,
                model_override=subtask.model or settings.background_model,
                is_background=True,
                extra_tools=extra_tools,
                force_tool_on_penultimate=(
                    "submit_final_report"
                    if settings.subtask_force_tool_on_penultimate
                    else None
                ),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # P1-L: catch per-attempt; do NOT let it bubble before _persist_outcome.
            error_msg = f"{type(exc).__name__}: {exc}"
            logger.exception("Subtask %s attempt %d errored", subtask.id.hex[:8], attempt)
            last_result = ValidationResult.failed("errored", error_msg)
            break

        total_in    += usage.get("input_tokens",  0)
        total_out   += usage.get("output_tokens", 0)
        total_calls += usage.get("tool_calls",    0)

        last_payload = collector.get()
        last_result = validate_report(
            last_payload, min_summary_chars=settings.subtask_report_min_summary_chars,
        )
        if last_result.ok or last_result.outcome == "incomplete_blocked":
            break
        if attempt < max_attempts and last_result.outcome in {
            "incomplete_no_terminal", "validation_failed",
        }:
            user_message = _build_retry_message(
                subtask.task, last_payload, last_result.reason,
            )
            continue
        break

    await _persist_outcome(
        heart, subtask, last_result, last_payload,
        attempts=attempt, tokens_in=total_in, tokens_out=total_out,
        tool_calls_made=total_calls,
    )
    if emit_event:
        await emit_event(subtask, last_result, last_payload)  # fire-and-forget telemetry
    if notify_telegram:
        await notify_telegram(subtask, last_result, last_payload)

    final_text = (
        last_result.report.summary if (last_result.ok and last_result.report)
        else (last_payload or {}).get("summary", "") or last_result.reason
    )
    return final_text, last_result


def _build_retry_message(task: str, prior_payload: dict | None, reason: str) -> str:
    """Plain prose, hard-capped, NEVER embedded JSON (P2.1)."""
    parts = [f"Your previous attempt was rejected: {reason}"]
    if prior_payload:
        prior_summary = (prior_payload.get("summary") or "")[:300]
        prior_conf    = prior_payload.get("confidence")
        if prior_summary:
            parts.append(f"Your previous summary (first 300 chars): {prior_summary}")
        if prior_conf is not None:
            parts.append(f"Your previous confidence: {prior_conf}")
    parts.append(
        "Try again. You MUST call submit_final_report with a schema-valid "
        "payload (summary >= 50 chars, no placeholder text, confidence 0-1)."
    )
    msg = "\n\n".join(parts)
    return msg if len(msg) <= 2000 else msg[:1997] + "..."


async def _persist_outcome(
    heart, subtask, last_result, last_payload,
    *, attempts, tokens_in, tokens_out, tool_calls_made,
) -> None:
    """Map ValidationResult → DB row. status / final_outcome / report / error."""
    common = dict(
        attempts=attempts, tokens_in=tokens_in, tokens_out=tokens_out,
        tool_calls_made=tool_calls_made, report_jsonb=last_payload,
    )
    if last_result.ok:
        await heart.subtasks.complete(
            subtask.id, last_result.report.summary,
            final_outcome="completed", **common,
        )
    elif last_result.outcome == "incomplete_blocked":
        # status='completed' (per spec) but final_outcome surfaces the block.
        # _format_subtask_results renders the dedicated "Blocked Subtask" section.
        # _sync_subtask_node treats this as DAG node failure.
        summary = last_result.report.summary if last_result.report else last_result.reason
        await heart.subtasks.complete(
            subtask.id, summary,
            final_outcome="incomplete_blocked", **common,
        )
    else:
        await heart.subtasks.fail(
            subtask.id, last_result.reason,
            final_outcome=last_result.outcome, **common,
        )
```

**Inline path** (`nous/api/tools.py::spawn_task`):

```python
if await_result:
    # F061: under hardening, route inline path through the SAME executor
    # as the worker. With flag off, fall back to the legacy direct call.
    if settings.subtask_hardening_enabled:
        from nous.handlers.subtask_executor import execute_hardened
        try:
            response_text, _result = await asyncio.wait_for(
                execute_hardened(
                    subtask, f"subtask-{subtask.id.hex[:8]}",
                    runner=runner, heart=heart, settings=settings,
                ),
                timeout=effective_timeout,
            )
            return _format_inline_result(subtask, response_text)
        except asyncio.TimeoutError:
            await heart.subtasks.fail(
                subtask.id, f"Timeout after {effective_timeout}s",
                final_outcome="timed_out", attempts=1,
                tokens_in=0, tokens_out=0, tool_calls_made=0,
            )
            return f"[Subtask timed out after {effective_timeout}s]"
    else:
        # legacy code unchanged
        ...
```

**Tests** (`tests/test_f061_subtask_executor.py` and existing `tests/test_subtask_worker_cleanup.py`):
- Happy path: tool called with valid payload attempt 1 → status=completed, final_outcome=completed, attempts=1, report_jsonb populated.
- Empty path (collector empty after attempt 1) → retry → attempt 2 valid → status=completed, attempts=2.
- Empty path 2x → status=failed, final_outcome=incomplete_no_terminal, attempts=2, report_jsonb=None.
- Placeholder summary → retry → valid → completed in 2 attempts.
- `incomplete=true` payload → status=completed, final_outcome=incomplete_blocked, NO retry.
- API exception attempt 1 → caught per-attempt → status=failed, final_outcome=errored, attempts=1, real exception in error.
- Outer wait_for times out → final_outcome=timed_out (set by caller).
- Token / tool_call accumulation across retries is correct.
- Flag off: `_execute_legacy` called instead (spy-verified) — bytewise unchanged.
- **Inline path with flag on**: `spawn_task(await_result=true)` invokes `execute_hardened`; row gets correct `final_outcome`.
- **Inline path with flag off**: legacy direct call.

### 2.6 `spawn_task` schema — three new optional fields

**File:** `nous/api/tools.py::_SPAWN_TASK_SCHEMA` and the closure

Add `output_format`, `success_criteria`, `boundaries` — all optional strings. Persisted via `subtasks.create(...)` from §1.5. Inline path forwards them.

**Tests** (`tests/test_f061_spawn_task_schema.py`):
- Schema accepts all three; defaults to None.
- Round-trip: spawn with each → row has the value persisted.
- Schedule firing path: `task_scheduler.py:91` calls `subtasks.create()` without these fields → row has NULL → worker synthesizes defaults at exec time.

### 2.7 `_format_subtask_results` rewrite + unconditional mark-delivered

**File:** `nous/cognitive/layer.py:71-94` and `:563-571`

The formatter:

```python
def _format_subtask_results(subtasks: list) -> str:
    lines: list[str] = []
    for s in subtasks:
        if s.status == "completed" and getattr(s, "final_outcome", None) == "incomplete_blocked":
            # P1-6: distinct "Blocked Subtask" rendering.
            blocked_reason = (s.report_jsonb or {}).get("blocked_reason", "no_reason_given")
            lines.append("=== Blocked Subtask ===")
            lines.append(f"[subtask-{s.id.hex[:8]}] Task: {s.task}")
            lines.append(f"Blocked: {blocked_reason}")
            if s.result and s.result.strip():
                lines.append(f"Partial summary: {s.result}")
            lines.append("")
            continue
        if s.status == "completed":
            report = s.report_jsonb
            if report and (report.get("summary") or "").strip():
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
                # Legacy / pre-flag row.
                lines.append("=== Completed Subtask ===")
                lines.append(f"[subtask-{s.id.hex[:8]}] Task: {s.task}")
                lines.append(f"Result: {s.result}")
                lines.append("")
            # else: empty completed row — skip silently.
        elif s.status == "failed":
            lines.append("=== Failed Subtask ===")
            lines.append(f"[subtask-{s.id.hex[:8]}] Task: {s.task}")
            lines.append(f"Outcome: {getattr(s, 'final_outcome', None) or 'unknown'}")
            if s.error:
                lines.append(f"Reason: {s.error}")
            lines.append("")
    return "\n".join(lines).strip()
```

**Caller change at `cognitive/layer.py:563-571`** (P1-D fix):

```python
if undelivered:
    subtask_context = _format_subtask_results(undelivered)
    delivered_ids = [s.id for s in undelivered]
    # Mark ALL undelivered as delivered, even if context is empty (legacy empty rows).
    # Otherwise the same rows reload on every parent turn.
    await self._heart.subtasks.mark_delivered(delivered_ids)
    if subtask_context:
        system_prompt = system_prompt + "\n\n" + subtask_context
        logger.info("Injected %d subtask results into session %s",
                    len(undelivered), session_id)
    else:
        logger.debug("Skipped %d empty subtask rows (still marked delivered)",
                     len(undelivered))
```

**Tests** (`tests/test_f061_format_subtask_results.py`):
- Legacy row (status=completed, result="real text", report_jsonb=None) → injected as `Result: real text`.
- F061 row (status=completed, final_outcome=completed, report_jsonb=valid) → new `Summary:` block.
- F061 row with findings/next_actions → bullets.
- `incomplete_blocked` row → `=== Blocked Subtask ===` section with blocked_reason.
- Empty row (status=completed, result="", report_jsonb=None) → no injection, BUT mark_delivered called for it (spy assertion).
- Failed row → `=== Failed Subtask ===` with outcome.

### 2.8 DAG branch on final_outcome

**File:** `nous/dag/orchestrator.py::_sync_subtask_node` (currently `:240-282`)

Add a branch BEFORE the existing `if subtask.status == "completed":`:

```python
async def _sync_subtask_node(self, node: DAGNode) -> None:
    if not self._subtask_mgr:
        return
    subtask = await self._subtask_mgr.get(node.subtask_id)
    if subtask is None:
        # ... existing missing-row branch unchanged ...
        return

    # P1-E: F061 outcome-aware branch. Read final_outcome rather than status alone.
    final = getattr(subtask, "final_outcome", None)
    if final in {"incomplete_blocked", "incomplete_no_terminal", "validation_failed"}:
        await self._store.update_node(
            node.id,
            status="failed",
            error=f"subtask {final}: {subtask.error or (subtask.report_jsonb or {}).get('blocked_reason') or 'no reason'}",
            result=subtask.result,
        )
        node.status = "failed"
        node.error  = f"subtask {final}"
        node.result = subtask.result
        return

    # ... existing branches for status == "completed" / "failed" / "running" unchanged ...
```

**Tests** (extend `tests/test_dag_orchestrator.py`):
- Subtask with `final_outcome='incomplete_blocked'` → DAG node marked failed with error containing `incomplete_blocked` and the blocked_reason.
- Subtask with `final_outcome='incomplete_no_terminal'` → same; error contains `incomplete_no_terminal`.
- Subtask with `final_outcome='validation_failed'` → same.
- Subtask with `final_outcome='completed'` → DAG node advances normally (regression test).
- Subtask with `final_outcome=None` (legacy pre-flag row) → falls through to existing `status=='completed'` logic.

### 2.9 Acceptance for PR-2

- All new tests pass.
- With `NOUS_SUBTASK_HARDENING_ENABLED=false`: full existing test suite passes byte-identical.
- With `NOUS_SUBTASK_HARDENING_ENABLED=true`: full existing test suite passes (some subtask tests need flag-aware assertions).
- Manual smoke: `docker compose up`, flag on, REST `spawn_task` → `report_jsonb` populated.
- Manual smoke: inline `spawn_task(await_result=true)` flag on → returns hardened executor's text; row has correct `final_outcome`.

---

## PR-3 — Telemetry + dashboard

### 3.1 Outcome events with loud failure logging

**File:** `nous/handlers/subtask_executor.py::_emit_outcome_event` (passed as `emit_event` callback from worker / inline path)

```python
async def _emit_outcome_event(bus, subtask, last_result, last_payload, settings) -> None:
    """Wrap entire body in try/except — fire-and-forget must NOT silently lose errors."""
    if not settings.subtask_outcome_persistence_enabled:
        return
    try:
        from nous.events import Event
        await bus.emit(Event(
            type="subtask_outcome",
            agent_id=settings.agent_id,
            session_id=f"subtask-{subtask.id.hex[:8]}",
            data={
                "subtask_id": str(subtask.id),
                "frame_type": subtask.frame_type,
                "final_outcome": last_result.outcome if last_result else None,
                "attempts": getattr(last_result, "attempts", None),
                "tokens_in": ...,
                "tokens_out": ...,
                "tool_calls_made": ...,
                "validator_reason": last_result.reason if last_result and not last_result.ok else None,
                "dag_node_id": str(subtask.dag_node_id) if subtask.dag_node_id else None,
            },
        ))
    except Exception:
        # P1-N: convert silent loss into loud one. Telemetry must not vanish.
        logger.exception(
            "Failed to emit subtask_outcome event for subtask %s",
            subtask.id.hex[:8],
        )
```

Worker passes `partial(_emit_outcome_event, self._bus, settings=self._settings)` as `emit_event`.

`asyncio.create_task` ONLY at the call site; the coroutine body is now self-protecting.

**Tests** (`tests/test_f061_outcome_event.py`):
- All 7 outcomes emit when flag on.
- Flag off → no emit.
- DB write raises → exception logged but worker proceeds (no propagation up).
- Event payload matches schema.

### 3.2 Dashboard query

**File:** `nous/api/dashboard_queries.py` — new function `subtask_dashboard(window_hours: int)`. SQL aggregations only (no Python row loops).

### 3.3 REST endpoint

**File:** `nous/api/rest.py` — `GET /dashboard/subtasks?window=24h`.

### 3.4 Frontend tab

`evaluations/index.html` gets a "Subtasks" tab mirroring "Heartbeat."

### 3.5 Acceptance

- `curl localhost:8000/dashboard/subtasks?window=24h` returns 200 with sensible JSON.
- Tab renders.
- Tests cover dashboard query at fixture-row level.

---

## PR-4 — Eval harness (inline-mode, gate-advisory by default)

### 4.1 Scenario file: `nous_eval/subtask_scenarios.yaml`

20 cases per spec mechanism 12.

### 4.2 Runner: `nous_eval/subtask_outcomes.py`

`python -m nous_eval.subtask_outcomes` — uses **inline mode** (`await_result=True`) per P1-G; no worker pool needed in eval container. Hits `nous-eval-scratch` at 127.0.0.1:5433 (per memory `reference_eval_db_connection.md`).

Asserts each case's rubric. Writes `reports/eval_subtask_outcomes_<timestamp>.{json,md}`.

**Gate mode flag:**

```python
# config.py addition
nous_eval_subtask_gate_mode: Literal["advisory", "enforcing"] = "advisory"
```

In `advisory` (default until PR-5): writes report, exits 0 even on threshold miss. Logs WARNING with the miss.

In `enforcing` (set in PR-5): exits non-zero on miss → CI red.

### 4.3 Register in `evaluations/RUNS.md`.

### 4.4 Acceptance

- One full run completes locally with flag on; report files written.
- Advisory mode never fails CI.
- Enforcing mode fails on synthetic threshold miss.

---

## PR-5 — Flag flip + gate enforcement + docs

### 5.1 Defaults

- `nous/config.py`: `subtask_hardening_enabled` default → `True`.
- `nous_eval_subtask_gate_mode` default → `"enforcing"`.

### 5.2 CLAUDE.md env-var table — exact additions

```
| `NOUS_SUBTASK_HARDENING_ENABLED` | `true` | F061: master flag for hardened subtask execution (forced terminal tool, validator, retry). |
| `NOUS_SUBTASK_MAX_ATTEMPTS` | `2` | F061: total attempts including original. Min 1, max 3. |
| `NOUS_SUBTASK_REPORT_MIN_SUMMARY_CHARS` | `50` | F061: minimum stripped-summary length to pass the validator. |
| `NOUS_SUBTASK_BOOTSTRAP_TIMEOUT` | `30` | F061: seconds for bootstrap phase (pre_turn → first API response). Observability only. |
| `NOUS_SUBTASK_WORK_TIMEOUT` | `570` | F061: seconds for work phase (first API response → terminal tool). Observability only. |
| `NOUS_SUBTASK_OUTCOME_PERSISTENCE_ENABLED` | `true` | F061: emit subtask_outcome events to nous_system.events. |
| `NOUS_SUBTASK_FORCE_TOOL_ON_PENULTIMATE` | `true` | F061: runner sets tool_choice on penultimate turn. Disabled automatically when extended thinking is on. |
| `NOUS_EVAL_SUBTASK_GATE_MODE` | `enforcing` | F061: subtask eval gate mode (advisory|enforcing). |
```

### 5.3 Other docs

- `docs/features/INDEX.md` — F061 marked Shipped.
- `evaluations/index.html` + `RUNS.md` — eval baseline recorded.
- `CHANGELOG.md` — DAG behavior change callout: *"Subtasks that previously persisted `result=''` and silently advanced their DAG node now fail it. Inspect any DAGs that depended on this behavior before upgrading."*

### 5.4 Earliest merge timing

Per P2-4: earliest PR-5 merge is `PR-4 merge + 5 days` of consecutive green nightly eval runs and dashboard inspection (or `+ 7 days` without manual sign-off). Realistic timeline: PR-1 merge + ~10–14 days.

### 5.5 Acceptance

- Defaults flipped.
- Existing tests pass with new defaults.
- Eval enforcing mode green for 5+ consecutive days.

---

## PR-6 — Cleanup (deferred ~2 weeks after PR-5 stability)

### 6.1 Migration `042_subtask_outcome_check.sql`

```sql
-- Backfill any pre-flag rows.
UPDATE heart.subtasks
SET final_outcome = CASE
    WHEN status = 'completed' AND result IS NOT NULL AND length(result) > 0 THEN 'completed'
    WHEN status = 'completed' THEN 'incomplete_no_terminal'
    WHEN status = 'failed'    THEN 'errored'
    WHEN status = 'cancelled' THEN 'cancelled'
    ELSE 'errored'
END
WHERE final_outcome IS NULL;

ALTER TABLE heart.subtasks
    ALTER COLUMN final_outcome SET NOT NULL;

ALTER TABLE heart.subtasks
    DROP CONSTRAINT IF EXISTS chk_subtasks_final_outcome;
ALTER TABLE heart.subtasks
    ADD CONSTRAINT chk_subtasks_final_outcome
    CHECK (final_outcome IN (
        'completed', 'incomplete_blocked', 'incomplete_no_terminal',
        'validation_failed', 'timed_out', 'errored', 'cancelled'
    ));
```

### 6.2 Remove legacy code

After 2 stable weeks: delete `_execute_legacy`, the flag check in `_execute_subtask`, and the legacy branch of `build_subtask_prefix`. The flag itself stays one more release as a kill-switch before final removal.

---

## Risks and mitigations (v2 update)

| Risk | Mitigation |
|---|---|
| Forced tool_choice breaks extended-thinking models | Detection: `thinking_mode == "off" or "haiku" in model`. Fallback: prompt-only reminder. Tested in §2.4. |
| DAG nodes that previously silent-empty-passed now fail visibly | Flag-gated. CHANGELOG callout. Operator inspection window (PR-3 dashboard) before flip (PR-5). |
| Validator rejects legitimate short answers | min 50 chars; placeholder regex narrowly scoped (verbs that signal NOT-DONE only). Positive-case test pinned for "I will recommend...". |
| Token cost rises due to retries | Cap=1 retry; `tokens_*` and `tool_calls_made` columns measure; eval gate enforces ±20%. |
| Cache miss on flag flip | One-time, hours. Documented PR-5. |
| Migration FK lock on busy `dag_nodes` | New column only; FK validation scans PK index — fast. Brief `AccessShare`. |
| Concurrent submit_final_report | First-call locks (P1-M); duplicate returns is_error from executor. |
| Telemetry write under burst load | Bounded by `NOUS_SUBTASK_WORKERS=2` + asyncpg pool. No additional rate limit needed in v1. |
| Inline path silently bypasses contract | **FIXED in v2**: shared `execute_hardened` helper called from both worker and inline. |
| `_emit_outcome_event` silently drops DB errors | **FIXED in v2**: try/except + `logger.exception` inside the coroutine body. |

---

## File-by-file summary (v2)

| File | Change | PR |
|---|---|---|
| `sql/migrations/041_subtask_hardening.sql` | NEW: schema + idempotent FK | 1 |
| `sql/migrations/042_subtask_outcome_check.sql` | NEW: backfill + CHECK | 6 |
| `nous/storage/models.py` | Subtask: 9 new mapped columns (Integer, UUID(as_uuid=True)) | 1 |
| `nous/heart/subtasks.py` | `create()` / `complete()` / `fail()` grow optional kwargs | 1 |
| `nous/config.py` | 7 new Settings fields in PR-1 (no validation_alias) + 1 new EvalSettings field (nous_eval_subtask_gate_mode) in PR-4 | 1, 4 |
| `nous/heart/subtask_report.py` | NEW: pydantic schema | 1 |
| `nous/heart/subtask_validator.py` | NEW: validator + ValidationResult | 2 |
| `nous/api/subtask_tools.py` | NEW: schema + collector (lock-on-first) + executor | 2 |
| `nous/handlers/subtask_executor.py` | NEW: shared `execute_hardened` helper | 2 |
| `nous/api/tools.py` | `build_subtask_prefix` rewrite; `spawn_task` schema +3 fields; inline path routes through `execute_hardened` | 2 |
| `nous/handlers/subtask_worker.py` | `_execute_subtask` routes to legacy or hardened | 2 |
| `nous/api/runner.py` | `extra_tools` + `force_tool_on_penultimate` params; per-call dispatch override; usage["tool_calls"]; short-circuit | 2 |
| `nous/cognitive/layer.py` | `_format_subtask_results` rewrite + unconditional mark_delivered | 2 |
| `nous/dag/orchestrator.py` | `_sync_subtask_node` reads `final_outcome` | 2 |
| `nous/api/dashboard_queries.py` | NEW: `subtask_dashboard` | 3 |
| `nous/api/rest.py` | NEW: `/dashboard/subtasks` endpoint | 3 |
| `evaluations/index.html` | NEW: Subtasks tab | 3 |
| `nous_eval/subtask_scenarios.yaml` | NEW: 20 cases | 4 |
| `nous_eval/subtask_outcomes.py` | NEW: harness, inline-mode, advisory gate | 4 |
| `evaluations/RUNS.md` | Register eval | 4 |
| `CLAUDE.md` | Env vars (8 entries enumerated above) + Shipped matrix | 5 |
| `docs/features/INDEX.md` | F061 Shipped | 5 |
| `CHANGELOG.md` | DAG behavior callout | 5 |
| `tests/test_f061_migration_041.py` | NEW | 1 |
| `tests/test_f061_subtask_model.py` | NEW | 1 |
| `tests/test_f061_subtask_report.py` | NEW | 1 |
| `tests/test_f061_config.py` | NEW | 1 |
| `tests/test_subtasks.py` | EXTEND for SubtaskManager API | 1 |
| `tests/test_f061_subtask_validator.py` | NEW (~18 cases incl. positive "I will recommend..." case) | 2 |
| `tests/test_f061_subtask_tools.py` | NEW (collector lock-on-first, double-submit) | 2 |
| `tests/test_f061_build_subtask_prefix.py` | NEW (legacy/hardened both) | 2 |
| `tests/test_f061_spawn_task_schema.py` | NEW | 2 |
| `tests/test_f061_runner_subtask_hooks.py` | NEW (extra_tools, force_tool, short-circuit, tool_calls counter) | 2 |
| `tests/test_f061_subtask_executor.py` | NEW (hardened executor) | 2 |
| `tests/test_subtask_worker_cleanup.py` | EXTEND for hardened-flag routing | 2 |
| `tests/test_f061_format_subtask_results.py` | NEW (incl. mark_delivered spy) | 2 |
| `tests/test_dag_orchestrator.py` | EXTEND for final_outcome branch | 2 |
| `tests/test_f061_outcome_event.py` | NEW (incl. DB-write exception logged) | 3 |
| `tests/test_f061_dashboard_subtasks.py` | NEW | 3 |
| `tests/test_f061_rest_dashboard_subtasks.py` | NEW | 3 |

Total v2: ~13 new source files, ~7 modified source files, ~13 new test files (flat), 2 migrations.

---

## Out-of-scope (deferred, unchanged from v1)

- Per-frame distinct timeout defaults.
- Verbatim-vs-paraphrase toggle on report injection.
- LLM critic on the report.
- F062 schedule firing fidelity eval.
- Heartbeat path changes.
- `redelivery_attempts` column (Q1 chose unconditional mark-delivered instead).
- `timed_out_bootstrap` enum sub-variant (P3-3 architecture review — label-only; defer to follow-up if observability demands it).
- Truncation indicator on `findings[:5]` (P3-4 architecture review — minor UX polish).
