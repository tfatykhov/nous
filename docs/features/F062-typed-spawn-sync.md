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
- Inconsistent with F061's `SubtaskOutcome` enum (completed / incomplete_no_terminal / validation_failed / error).

---

## Proposed Design

### `SubtaskResult` return type

```python
@dataclass
class SubtaskResult:
    task_id: str
    status: SubtaskOutcome          # from F061: completed | incomplete_no_terminal | validation_failed | error
    payload: dict                    # structured JSON extracted from the terminal report
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

### Phase 1 — `SubtaskResult` dataclass + DB column (≈2h)
- Add `result_payload JSONB` column to `heart.subtasks` (new migration).
- Add `SubtaskOutcome` import from F061 into `nous/api/tools.py`.
- Define `SubtaskResult` in `nous/models/subtask.py` (new file).

### Phase 2 — `spawn_sync` tool (≈3h)
- Add `spawn_sync` as a new tool in `nous/api/tools.py`.
- Plumbs through schema injection into subtask system prompt.
- Calls existing `_await_subtask()` polling loop; wraps result into `SubtaskResult`.

### Phase 3 — Schema validation (≈2h)
- On subtask completion, attempt `json.loads(result)` + `jsonschema.validate(result, schema)`.
- Write validated payload to `result_payload JSONB`; update `outcome` enum.
- Expose `SubtaskResult.payload` to parent caller.

**Total LOE estimate:** ~7h (three phases, single engineer)

---

## Alternatives Considered

- **Return `SubtaskResult` from existing `spawn_task(await_result=True)`** — breaks callers treating return as `str`. Deferred to a later migration.
- **LLM-based payload extraction** — rejected: adds latency and a second LLM call. Structural JSON schema in system prompt is sufficient.
- **Pydantic model instead of dataclass** — valid; deferred until Pydantic is added as a dependency (currently not in `requirements.txt`).

---

## Success Criteria

- `spawn_sync(task=..., schema={...})` returns a `SubtaskResult` with `status=completed` and `payload` populated when the subtask emits valid JSON.
- `status=validation_failed` when subtask emits prose or invalid JSON.
- All existing `spawn_task(await_result=True)` call sites continue to work unchanged.
- Unit test coverage for all three `status` paths.
