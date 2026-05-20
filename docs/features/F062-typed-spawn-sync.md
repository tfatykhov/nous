# F062 — Typed spawn_sync Primitive

**Status:** 📝 Draft (2026-05-18)
**Owner:** nous core
**Scope:** Give `spawn_task(await_result=True)` a structured, typed return contract — replacing the current raw-string result with a validated `SubtaskResult` object that carries status, structured payload, and confidence. P1 polish for the Schmid subagent taxonomy.
**Family:** Subtask Hardening (Doc 019 R1). See also F061 (outcome correctness), F063 (blackboard inbox).
**Related:** F064 (Symphony — depends on F061/F062/F063 landing first)

---

## Problem

`spawn_task(await_result=True)` currently returns the subtask's raw `result` string verbatim. The parent agent receives an untyped blob that it must parse/interpret, with no machine-readable status, no confidence signal, and no structured payload.

### Today's contract (weak)
```python
result_text: str = await spawn_task(
    task="summarise these 5 URLs",
    await_result=True
)
# result_text is whatever the subtask wrote — could be empty, truncated, or garbled
```

### Failures this causes
- Parent cannot distinguish `status='completed'` with empty result from a genuine empty response.
- No structured way to surface partial vs. full completion.
- No confidence score — parent must re-read and re-evaluate.
- Inconsistent with F061's `final_outcome` enum (`completed`, `incomplete_blocked`, `incomplete_no_terminal`, `validation_failed`, `timed_out`, `errored`, `cancelled` — see [F061 migration 041](../../sql/migrations/041_subtask_hardening.sql)).

---

## Proposed Design

### `SubtaskResult` return type

```python
# F062 introduces SubtaskOutcome as the formal Literal alias for the seven
# canonical strings F061 already writes to heart.subtasks.final_outcome.
SubtaskOutcome = Literal[
    "completed",
    "incomplete_blocked",
    "incomplete_no_terminal",
    "validation_failed",
    "timed_out",
    "errored",
    "cancelled",
]

@dataclass
class SubtaskResult:
    task_id: str
    status: SubtaskOutcome          # mirrors heart.subtasks.final_outcome (F061)
    payload: dict                    # structured JSON extracted from the terminal report (may reuse F061's report_jsonb — see Implementation Plan)
    raw_text: str                    # original report text (for debugging)
    confidence: float | None        # 0.0–1.0; None if subtask didn't emit one
    elapsed_seconds: float
```

### `spawn_task` signature change (backward-compatible)

```python
# Before (unchanged default)
result: str = spawn_task(task=..., await_result=False)

# After — await_result=True returns SubtaskResult
result: SubtaskResult = spawn_task(task=..., await_result=True)
```

Returning a `SubtaskResult` when `await_result=True` is backward-incompatible for callers that treat the return as `str`. Mitigation: introduce `spawn_sync(...)` as the new explicit entry-point; leave `spawn_task(await_result=True)` returning raw string for one release cycle, then migrate.

### `spawn_sync` entry-point

```python
result: SubtaskResult = spawn_sync(
    task="summarise these 5 URLs",
    frame_type="research",          # optional, defaults to current frame
    timeout_seconds=120,
    schema: dict | None = None,     # optional JSON Schema for payload validation
)
```

- `schema` is passed into the subtask's system prompt as the expected output shape.
- If the terminal report validates against `schema`, `status=completed` and `payload` is populated.
- If validation fails, `status=validation_failed`; parent can inspect `raw_text` and retry or escalate.

### Subtask system prompt injection (when schema provided)

```
Your terminal report MUST be valid JSON matching this schema:
<schema>
{schema_json}
</schema>
Return ONLY the JSON object — no prose wrapper.
```

---

## Implementation Plan

### Phase 1 — `SubtaskResult` dataclass + storage (≈2h)
- Define the `SubtaskOutcome` `Literal` alias and `SubtaskResult` dataclass in `nous/api/models.py` (alongside existing subtask request/response models).
- Decide storage path for the schema-validated payload: either (a) reuse F061's existing `heart.subtasks.report_jsonb` column by adding an optional `payload` field to `submit_final_report`'s input schema, or (b) add a new `result_payload JSONB` column via a follow-up migration. Default position: (a) — keeps the schema flat and avoids a new migration; revisit in the implementation plan.

### Phase 2 — `spawn_sync` tool (≈3h)
- Add `spawn_sync` as a new tool in `nous/api/tools.py`.
- Plumbs the caller-supplied schema into the subtask system prompt (instructing the model to populate `submit_final_report`'s payload field with data matching that schema).
- Calls the existing F061 hardened executor / `_await_subtask()` polling loop; wraps the terminal `report_jsonb` into `SubtaskResult`.

### Phase 3 — Schema validation (≈2h)
- **`SubtaskResult.status` always mirrors `heart.subtasks.final_outcome`** as persisted by F061's outer handler / hardened executor. Schema validation never overwrites a non-success terminal outcome — `timed_out`, `errored`, `cancelled`, `incomplete_blocked`, and `incomplete_no_terminal` flow through unchanged so parent retry / escalation logic and F061-aligned telemetry consumers stay correct.
- Schema validation runs **only on the path where F061 was about to record `completed`**. After F061's structural `validate_report` returns OK, parse the payload candidate (`parsed = json.loads(raw_payload)` if the model emitted a string, else use the JSONB dict directly) and run `jsonschema.validate(parsed, schema)` against the caller-supplied schema.
- On success: F061 persists `final_outcome="completed"` (unchanged) and we surface `SubtaskResult(status="completed", payload=parsed, ...)`.
- On `jsonschema.ValidationError` or `json.JSONDecodeError`: rewrite the in-flight `ValidationResult` to `validation_failed` so F061's existing retry loop sees the failure exactly as it does for `schema_invalid` / `summary_too_short`. The persisted `final_outcome` then ends up as either `completed` (retry succeeded) or `validation_failed` (retry exhausted). `SubtaskResult` is built from the persisted row, so its `status` is automatically correct.
- Schema-validation failures here are *additional* to F061's structural validator — they layer on top of `submit_final_report`'s own schema check, never bypass other failure modes.

**Total LOE estimate:** ~7h (three phases, single engineer)

---

## Alternatives Considered

- **Return `SubtaskResult` from existing `spawn_task(await_result=True)`** — breaks callers treating return as `str`. Deferred to a later migration.
- **LLM-based payload extraction** — rejected: adds latency and a second LLM call. Structural JSON schema in system prompt is sufficient.
- **Pydantic model instead of dataclass** — valid; pydantic v2 is already a project dependency (`pyproject.toml`) and is used for API schemas. Decision deferred to the implementation plan: dataclass is sufficient if `SubtaskResult` stays internal, but a `BaseModel` may be preferable if the type crosses the REST API boundary.

---

## Success Criteria

- `spawn_sync(task=..., schema={...})` returns a `SubtaskResult` with `status=completed` and `payload` populated when the subtask emits valid JSON.
- `status=validation_failed` when subtask emits prose or invalid JSON.
- All existing `spawn_task(await_result=True)` call sites continue to work unchanged.
- Unit test coverage for all three `status` paths.
