# F062 + F063 — Typed `spawn_sync` + Blackboard Result Inbox

**Spec PR:** #426 (`feat/F062-F063-typed-subtask-specs`)
**Status:** 📝 Draft (2026-05-19)
**Owner:** nous core
**Implementer:** claude-opus-4-7 + reviewer subagents
**Scope:** Implement both features on the spec PR's branch so the final PR ships specs and code together. F062 lands first (it provides `SubtaskResult`); F063 builds on top.

---

## 0. Pre-flight invariants

These hold today (verified in this session against `nous/`):

- `heart.subtasks.final_outcome` accepts the seven canonical strings: `completed | incomplete_blocked | incomplete_no_terminal | validation_failed | timed_out | errored | cancelled` (see `sql/migrations/041_subtask_hardening.sql` and `nous/handlers/subtask_executor.py`).
- `submit_final_report` is the terminal contract for hardened subtasks; the schema lives in `nous/api/subtask_tools.py::SUBMIT_FINAL_REPORT_SCHEMA` and the payload is captured by `SubtaskReportCollector`.
- `execute_hardened` (in `nous/handlers/subtask_executor.py`) is the single executor used by both the inline `await_result=True` path (`tools.py::spawn_task`) and the background worker pool. F062/F063 extend this executor — no new code paths.
- `spawn_task`'s closure already wires `output_format` and `success_criteria` into `heart.subtasks.create()`. F062 piggybacks on this same pattern for `payload_schema`.
- Pydantic v2 is already in `pyproject.toml`.
- `jsonschema` is **not** currently in `pyproject.toml` — we will add it (a tiny, well-maintained dep already implicitly pulled by some transitive packages; we make it explicit).

If any of these invariants are violated when this plan runs, STOP and re-verify before continuing.

---

## 1. Goals & non-goals

### Goals
1. Introduce `SubtaskResult` + `SubtaskOutcome` as first-class Python types alongside existing subtask request/response models in `nous/api/models.py`.
2. Add an optional caller-provided JSON Schema (`payload_schema`) to `spawn_task` that:
   - Is injected into the subtask's system prompt as the required output shape.
   - Adds an optional `payload` field to `submit_final_report`'s input schema.
   - Triggers a post-execution schema validation step that flips `final_outcome` to `validation_failed` when the payload doesn't match.
3. Introduce a new `spawn_sync` tool that returns a `SubtaskResult`-shaped JSON to the calling LLM (typed counterpart to the existing string-returning inline path).
4. Add a `heart.boards` registry table + `board_id` column on `heart.subtasks`.
5. Introduce three tools: `create_board`, `fan_out`, `wait_results`.
6. Maintain backward compatibility for every existing `spawn_task` caller (no schema = no change in behavior or return shape).
7. Ship both features behind feature flags (default `false` for one release) so DAG node and conversational-agent flows can be flipped independently.

### Non-goals
- Replacing the existing `spawn_task(await_result=True)` string contract. We add `spawn_sync` alongside; we do **not** mutate the legacy return shape.
- LLM-based payload extraction (rejected in spec).
- Heartbeat path integration (heartbeat doesn't go through `heart.subtasks`).
- DAG-node wiring of boards (DAGs already cover N-to-parent structurally; F063 targets the conversational gap).

---

## 2. Files to create / modify

### Create
| Path | Purpose |
|---|---|
| `sql/migrations/042_f062_payload_schema.sql` | Adds `payload_schema JSONB` and `payload_schema_valid BOOLEAN` columns on `heart.subtasks`. Both nullable; pre-flag rows untouched. |
| `sql/migrations/043_f063_blackboard.sql` | Adds `heart.boards` table + `board_id UUID NULL` column on `heart.subtasks` with FK to `heart.boards.id`. |
| `nous/heart/boards.py` | Thin CRUD layer for `heart.boards` rows (`create`, `get`, `list_by_agent`, `mark_completed`). |
| `tests/test_f062_payload_schema.py` | Unit tests for SubtaskResult, schema-validation success/failure, no-schema back-compat. |
| `tests/test_f062_spawn_sync_tool.py` | spawn_sync tool tests (success, validation_failed, timeout). |
| `tests/test_f063_boards.py` | Unit tests for boards CRUD. |
| `tests/test_f063_fan_out_wait_results.py` | Integration tests for the full fan_out → wait_results flow against in-memory subtasks. |

### Modify
| Path | Change |
|---|---|
| `nous/api/models.py` | Add `SubtaskOutcome` Literal alias + `SubtaskResult` dataclass. |
| `nous/api/subtask_tools.py` | Add optional `payload` property to `SUBMIT_FINAL_REPORT_SCHEMA.input_schema.properties` (no `required` change). |
| `nous/api/tools.py` | (a) Add `payload_schema` arg to `spawn_task` closure + `_SPAWN_TASK_SCHEMA`; persist through `heart.subtasks.create()`. (b) Add `spawn_sync`, `create_board`, `fan_out`, `wait_results` tools + their schemas + register them. (c) Extend `build_subtask_prefix` to inject the payload schema instructions when present (hardening_enabled path only). |
| `nous/handlers/subtask_executor.py` | After F061's structural `validate_report` passes, if `subtask.payload_schema` is non-NULL, run `jsonschema.validate(report['payload'], subtask.payload_schema)`. On failure: rewrite `last_result` to `ValidationResult.failed("validation_failed", str(e))` and let the existing retry/persist path handle it. |
| `nous/heart/facts.py` *(or wherever `SubtaskCRUD` lives — verify path before editing)* | Add `payload_schema` + `board_id` to `Subtask.create` signature; thread through to the INSERT. |
| `nous/heart/heart.py` *(verify)* | Surface `boards` collection accessor. |
| `nous/storage/models.py` | Add `payload_schema`, `payload_schema_valid`, `board_id` columns to `Subtask`; add `Board` ORM model. |
| `nous/config.py` | Add `subtask_payload_schema_enabled: bool = False` + `blackboard_enabled: bool = False` + `blackboard_poll_interval_seconds: float = 2.0` + `blackboard_poll_max_interval_seconds: float = 10.0`. |
| `CLAUDE.md` | Document the new env vars + REST surfaces if any (F062/F063 are pure tool layer; no new REST endpoints in v1). |
| `docs/features/INDEX.md` | Flip F062/F063 from Draft → Implementing. (Note: spec PR explicitly left INDEX.md alone; we update it as part of the implementation PR.) |

### Touch-but-don't-rewrite
- `nous/cognitive/layer.py::_format_subtask_results` — already handles `report_jsonb`. If F062's payload is stored under `report_jsonb.payload`, no change needed; the existing formatter sees `report.summary` and skips unknown keys.

---

## 3. Build sequence (single PR, three commits)

### Commit A — F062 storage + SubtaskResult type (≈2.5h)

1. **Migration 042** — add `payload_schema JSONB` (caller-supplied schema, NULL when not used) and `payload_schema_valid BOOLEAN` (NULL pre-validation, true/false post). No CHECK constraint in v1.
2. **ORM update** — `nous/storage/models.py::Subtask` gains both columns; `Mapped[dict | None]` and `Mapped[bool | None]`.
3. **API model** — `nous/api/models.py`:
   ```python
   SubtaskOutcome = Literal[
       "completed", "incomplete_blocked", "incomplete_no_terminal",
       "validation_failed", "timed_out", "errored", "cancelled",
   ]

   @dataclass
   class SubtaskResult:
       task_id: str
       status: SubtaskOutcome
       payload: dict[str, Any]
       raw_text: str
       confidence: float | None
       elapsed_seconds: float
       validator_reason: str | None = None

       def to_dict(self) -> dict[str, Any]:
           return {
               "task_id": self.task_id,
               "status": self.status,
               "payload": self.payload,
               "raw_text": self.raw_text,
               "confidence": self.confidence,
               "elapsed_seconds": round(self.elapsed_seconds, 3),
               "validator_reason": self.validator_reason,
           }
   ```
4. **Settings** — add `subtask_payload_schema_enabled`.
5. **Tests for the dataclass + ORM** — round-trip serialization, NULL-defaulting.

Acceptance: pytest `tests/test_f062_payload_schema.py` green; `nous_eval` smoke unchanged; `docker compose up` against fresh DB cleanly applies migration 042.

### Commit B — F062 spawn_sync + schema validation wiring (≈3h)

1. **submit_final_report extension** — `SUBMIT_FINAL_REPORT_SCHEMA` gains an optional `payload` property:
   ```python
   "payload": {
       "type": ["object", "array", "string", "number", "boolean", "null"],
       "description": (
           "Schema-typed result payload. Required when the spawning "
           "tool supplied a payload_schema; ignored otherwise."
       ),
   }
   ```
   No change to `required` (still `["summary", "confidence"]`). Schema is enforced post-hoc.

2. **build_subtask_prefix extension** — when `hardening_enabled=True` and `payload_schema` is non-None, append a new section to the prefix:
   ```
   # Result schema (REQUIRED)
   When you call submit_final_report, the `payload` field MUST be a JSON
   value that validates against this schema:

   <schema>
   {compact_json}
   </schema>

   Use the schema's property names exactly; do not invent keys.
   ```

3. **spawn_task extension** — accept optional `payload_schema: dict | None`; persist via `heart.subtasks.create(payload_schema=...)`; thread through `build_subtask_prefix`.

4. **execute_hardened extension** — schema validation runs **only** when F061's structural validator just returned `ok` (i.e., the in-flight attempt was about to record `completed`). Other terminal outcomes (`errored`, `timed_out`, `cancelled`, `incomplete_blocked`, `incomplete_no_terminal`) are **never** rewritten — `SubtaskResult.status` must always mirror the persisted `heart.subtasks.final_outcome`. Implementation:
   ```python
   # last_result.ok is True here — F061 has accepted the structural payload.
   if last_result.ok and subtask.payload_schema is not None:
       raw_payload = last_payload.get("payload") if last_payload else None
       try:
           if isinstance(raw_payload, str):
               parsed = json.loads(raw_payload)
           else:
               parsed = raw_payload
           jsonschema.validate(parsed, subtask.payload_schema)
           payload_schema_valid = True
       except (json.JSONDecodeError, jsonschema.ValidationError) as e:
           last_result = ValidationResult.failed(
               "validation_failed", f"payload schema mismatch: {e}"
           )
           payload_schema_valid = False
   ```
   On `validation_failed`, drop through to F061's existing retry loop (treated identically to structural `schema_invalid`). On retry exhaustion, F061 persists `final_outcome="validation_failed"` and `SubtaskResult` reads back the correct status from the row.

5. **spawn_sync tool** — new closure parallel to `spawn_task` that:
   - Always sets `await_result=True` and `hardening_enabled=True`.
   - Takes a required `task`, an optional `payload_schema`, an optional `frame_type`, an optional `timeout_seconds`.
   - On completion, packages `SubtaskResult` and returns it as `{"content": [{"type": "text", "text": json.dumps(result.to_dict(), indent=2)}]}` so the LLM gets a clean, parseable JSON blob.
   - Reuses `_persist_and_emit_inline_outcome` for timeout/error paths.

6. **Schema** — `_SPAWN_SYNC_SCHEMA` mirroring `_SPAWN_TASK_SCHEMA` minus `await_result` (always true), `priority` (always "normal"), `notify` (always false), and `output_format` (derived from frame as before); plus `payload_schema: dict`.

7. **Register the tool** — `register_subtask_tools` registers `spawn_sync` only when `settings.subtask_payload_schema_enabled` is true.

Acceptance:
- New tests cover: (a) `spawn_sync` happy path with payload_schema → SubtaskResult.status == "completed", payload dict matches. (b) Subtask returns a payload that fails schema → status="validation_failed", validator_reason populated. (c) `spawn_sync` without payload_schema → still returns SubtaskResult but with `payload={}`.
- Existing `tests/test_f061_*` all still green.
- `_SPAWN_TASK_SCHEMA` integration test verifies adding optional `payload_schema` doesn't break legacy callers.

### Commit C — F063 boards + fan_out + wait_results (≈3.5h)

1. **Migration 043** — `heart.boards` table:
   ```sql
   CREATE TABLE IF NOT EXISTS heart.boards (
       id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
       agent_id    TEXT NOT NULL,
       label       TEXT NOT NULL,
       status      VARCHAR(16) NOT NULL DEFAULT 'open',
       created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
       closed_at   TIMESTAMPTZ NULL,
       CONSTRAINT boards_status_chk
           CHECK (status IN ('open', 'closed'))
   );
   CREATE INDEX idx_boards_agent_open ON heart.boards (agent_id, status, created_at DESC);

   ALTER TABLE heart.subtasks
       ADD COLUMN IF NOT EXISTS board_id UUID NULL
           REFERENCES heart.boards(id) ON DELETE SET NULL;
   CREATE INDEX IF NOT EXISTS idx_subtasks_board ON heart.subtasks (board_id)
       WHERE board_id IS NOT NULL;
   ```
   Agent-scoped (required per memory note: every new table needs `agent_id`).

2. **ORM + CRUD** — `nous/storage/models.py::Board`; `nous/heart/boards.py::BoardManager`.

3. **Tools** —
   - `create_board(label)` → `{"board_id": uuid}`.
   - `fan_out(board_id, tasks, frame_type=None, timeout_seconds=None)` → spawns N subtasks each with `board_id` set, returns `{"task_ids": [...]}`.
   - `wait_results(board_id, n_of_m=None, timeout_seconds=300)` → polls `heart.subtasks WHERE board_id=$1` every 2s (exponential backoff up to 10s), terminal predicate uses the full F061 outcome set, returns when `n_of_m` rows terminal OR wall-clock timeout. Returns a list of `SubtaskResult.to_dict()` blobs.

4. **Polling helper** — `nous/heart/boards.py::poll_terminal(board_id, n_of_m, deadline_monotonic)`. Pure-async loop; no busy-wait. Uses `asyncio.sleep` with backoff. Single SQL query per poll (no per-row trips). Cancellable.

5. **Tools schema + registration** — gated by `settings.blackboard_enabled`.

6. **Tests** —
   - Boards CRUD round-trip.
   - `fan_out` spawns N subtasks, each with `board_id` set, all in `pending` status.
   - `wait_results(n_of_m=2)` returns when 2 of 3 subtasks are terminal (use direct DB writes to simulate completion).
   - `wait_results` timeout returns whatever's terminal; running subtasks untouched.
   - `wait_results` includes failed terminal outcomes (`validation_failed`, `errored`, `timed_out`, `cancelled`) — not just `completed`.

Acceptance:
- All new tests green.
- Existing test suite unchanged.
- `docker compose up` against fresh DB applies migrations 042 + 043 in order.
- A 5-line end-to-end smoke (in `tests/test_f063_fan_out_wait_results.py`) round-trips: `create_board` → `fan_out(3 tasks)` → `wait_results(n_of_m=2)` against in-memory subtasks.

---

## 4. Configuration & rollout

New `Settings` fields (all `NOUS_*`):

| Variable | Default | Notes |
|---|---|---|
| `NOUS_SUBTASK_PAYLOAD_SCHEMA_ENABLED` | `false` | Master flag for F062. When false: `payload_schema` kwarg silently ignored; `spawn_sync` not registered; `submit_final_report` schema unchanged. |
| `NOUS_BLACKBOARD_ENABLED` | `false` | Master flag for F063. When false: `create_board`/`fan_out`/`wait_results` not registered. |
| `NOUS_BLACKBOARD_POLL_INTERVAL_SECONDS` | `2.0` | Initial poll cadence. |
| `NOUS_BLACKBOARD_POLL_MAX_INTERVAL_SECONDS` | `10.0` | Exponential backoff ceiling. |

Rollout:
1. Land the migrations forward-compatible (NULL on new columns); flags default `false`.
2. After 1 release of bake-time, flip flags to `true` (separate PR; not in scope here).

---

## 5. Risks & mitigations

| Risk | Mitigation |
|---|---|
| `jsonschema` lib version drift breaks valid schemas | Pin `jsonschema>=4,<5` in `pyproject.toml`; ship one regression test that uses each draft (4/6/7/2020-12) by tagging the `$schema` key. |
| Adding `payload` field to submit_final_report makes models on legacy paths emit a payload nobody validates | New field is optional + nullable; pre-flag `payload_schema=None` means we never inspect the payload. |
| Polling `wait_results` hammers DB at scale | Single GROUP BY query per tick; cap on `n_of_m` ≤ 50; per-board deadline forces cancellation. |
| `fan_out` spawn loop spawns more subtasks than `subtask_max_concurrent` allows | `fan_out` does not bypass admission control — calls into `heart.subtasks.create()` the same way `spawn_task` does. Worker pool enforces concurrency. |
| Race between F063 polling and F061 timeout writer | Both write to different columns at non-overlapping times: F063 only reads; F061's outer handler writes `final_outcome`. No mutation race. |
| New `boards` table introduces orphan rows if agent restarts mid-fan-out | `boards.status` field + nightly sleep handler can prune `open` boards older than 24h (deferred — out of scope; orphan rows are harmless). |

---

## 6. Out of scope (explicit deferrals)

- DAG node `subtask` integration with boards (DAGs already cover N-to-parent structurally).
- Persistent board state across agent restarts (in-DB already; we just don't add UI yet).
- `/dashboard/boards` page (post-flag-flip follow-up).
- Per-agent kill / `cancel_board(board_id)` tool.
- `agent_id`-scoped board listing in `list_tasks` tool.

---

## 7. Definition of done

- All migrations apply cleanly against a fresh `docker compose up -d postgres` + restart cycle.
- Every new test in `tests/test_f06[23]_*.py` passes; existing `tests/test_f061_*` and broader suite untouched.
- `uv run pytest tests/ -v` is green.
- `uv run ruff check nous/` is green.
- Codex re-review on the final commit yields no new P1.
- `gh pr view 426 --json statusCheckRollup` shows all checks SUCCESS.
- The PR description in #426 is updated to document that both spec drafts and implementations now ship together.

---

## 8. Review checkpoints

Before each commit lands on the branch:

| Commit | Reviewer | Lens |
|---|---|---|
| A (storage + types) | `python-pro` + `code-reviewer` | ORM correctness, migration safety, dataclass design. |
| B (spawn_sync + validation) | `code-reviewer` + `silent-failure-hunter` | jsonschema integration, retry-loop interactions, error-path coverage. |
| C (boards + fan_out) | `code-reviewer` + `architecture-designer` | Schema design, polling correctness, race-condition analysis. |

After commit C: one consolidated review pass against the full diff before requesting Codex re-review and pushing.
