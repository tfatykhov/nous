# F062 + F063 — Typed `spawn_sync` + Blackboard Result Inbox

**Spec PR:** #426 (`feat/F062-F063-typed-subtask-specs`)
**Status:** 📝 Draft (2026-05-19)
**Owner:** nous core
**Implementer:** claude-opus-4-7 + reviewer subagents

**This-PR scope (PR #426 final form):**
- Both spec drafts (F062 + F063).
- **F062 implementation only** (Commits A + B below).
- F063 implementation (Commit C) is deferred to a **follow-up PR** so the diff stays reviewable and the F062 foundation lands clean. F063's spec stays in this PR as the contract that follow-up will fulfil.

**Why split:** F062 (~5.5h) is achievable end-to-end with green CI in one session; piling F063 (~3.5h) on top doubles failure surface for the final-review pass and risks leaving F063 half-implemented when the session budget runs out. Better to ship F062 cleanly + ship F063 next than to half-land both.

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
| `sql/migrations/047_f063_blackboard.sql` | Adds `heart.boards` table + `board_id UUID NULL` column on `heart.subtasks` with FK to `heart.boards.id`. **Verified 2026-05-19: migrations 043–046 are taken** (`043_dag_node_columns`, `044_procedure_runtime_metadata`, `045_schedule_continuation`, `046_work_queue_items`); 047 is the next free slot. |
| `nous/heart/boards.py` | Thin CRUD layer for `heart.boards` rows (`create`, `get`, `list_by_agent`). v1 omits `mark_completed` since no v1 caller closes a board — see Commit C note. |
| `tests/test_f062_payload_schema.py` | Unit tests for SubtaskResult, schema-validation success/failure, no-schema back-compat. |
| `tests/test_f062_spawn_sync_tool.py` | spawn_sync tool tests (success, validation_failed, timeout). |
| `tests/test_f063_boards.py` | Unit tests for boards CRUD. |
| `tests/test_f063_fan_out_wait_results.py` | Integration tests for the full fan_out → wait_results flow against in-memory subtasks. |

### Modify
| Path | Change |
|---|---|
| `nous/api/models.py` | Add `SubtaskOutcome` Literal alias + `SubtaskResult` dataclass. |
| `nous/api/subtask_tools.py` | Add optional `payload` property to `SUBMIT_FINAL_REPORT_SCHEMA.input_schema.properties` (no `required` change). |
| `nous/api/tools.py` | (a) Add `payload_schema` arg to `spawn_task` closure; **only expose it via `_SPAWN_TASK_SCHEMA` when `settings.subtask_payload_schema_enabled=True`** (build the schema dict conditionally at `register_subtask_tools` time — see P1 fix below). Persist through `heart.subtasks.create()`. (b) Add `spawn_sync`, `create_board`, `fan_out`, `wait_results` tools + their schemas + register them. (c) Extend `build_subtask_prefix` to inject the payload schema instructions when present (hardening_enabled path only). |
| `nous/handlers/subtask_executor.py` | After F061's structural `validate_report` passes, if `subtask.payload_schema` is non-NULL, run `jsonschema.validate(report['payload'], subtask.payload_schema)`. On failure: rewrite `last_result` to `ValidationResult.failed("validation_failed", str(e))` and let the existing retry/persist path handle it. |
| `nous/heart/subtasks.py` | `SubtaskManager.create()` (line 27 today) gains `payload_schema: dict \| None = None` and `board_id: UUID \| None = None` kwargs; thread both to the INSERT. **NOT** `nous/heart/facts.py` — that holds `FactManager`. |
| `nous/heart/heart.py` | Add `self.boards = BoardManager(self._session_factory)` alongside the existing `self.subtasks` wiring; expose `boards` as a public attribute. |
| `nous/storage/models.py` | Add `payload_schema`, `payload_schema_valid`, `board_id` columns to `Subtask`; add `Board` ORM model (agent_id, label, created_at). |
| `nous/config.py` | Add `subtask_payload_schema_enabled: bool = False` + `blackboard_enabled: bool = False` + `blackboard_poll_interval_seconds: float = 2.0` + `blackboard_poll_max_interval_seconds: float = 10.0`. |
| `pyproject.toml` | Pin `jsonschema>=4,<5` in `[project] dependencies`. |
| `CLAUDE.md` | Document the new env vars + REST surfaces if any (F062/F063 are pure tool layer; no new REST endpoints in v1). |
| `docs/features/INDEX.md` | Flip F062/F063 from Draft → Implementing. (Note: spec PR explicitly left INDEX.md alone; we update it as part of the implementation PR.) |

### Touch-but-don't-rewrite
- `nous/cognitive/layer.py::_format_subtask_results` — already handles `report_jsonb`. If F062's payload is stored under `report_jsonb.payload`, no change needed; the existing formatter sees `report.summary` and skips unknown keys.

---

## 3. Build sequence (single PR, three commits)

### Commit A — F062 storage + SubtaskResult type (≈2.5h)

1. **Migration 042** — add `payload_schema JSONB` (caller-supplied schema, NULL when not used) and `payload_schema_valid BOOLEAN` (NULL pre-validation, true/false post). No CHECK constraint in v1.
2. **ORM update** — `nous/storage/models.py::Subtask` gains both columns; `Mapped[dict | None]` and `Mapped[bool | None]`.
3. **API model** — `nous/api/models.py` (matches the existing file convention — every type in that file today is `@dataclass`; using `pydantic.BaseModel` would mix styles. If a follow-up needs `SubtaskResult` on the REST surface, switch the file's models en bloc):
   ```python
   SubtaskOutcome = Literal[
       "completed", "incomplete_blocked", "incomplete_no_terminal",
       "validation_failed", "timed_out", "errored", "cancelled",
   ]

   @dataclass
   class SubtaskResult:
       task_id: str
       status: SubtaskOutcome
       payload: Any   # full JSON value (object/array/string/number/boolean/null); typed as Any to match submit_final_report.payload's permissive schema
       raw_text: str
       confidence: float | None
       elapsed_seconds: float
       validator_reason: str | None = None

       def to_dict(self) -> dict[str, Any]:
           # payload is passed through unchanged — could be any JSON-serializable value
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

   **Flag-gated schema exposure** (P1 fix from plan-review 2026-05-19): the `payload_schema` JSON Schema property MUST be added to `_SPAWN_TASK_SCHEMA.properties` only when `settings.subtask_payload_schema_enabled=True`. Otherwise the model sees a tool-schema property that silently drops at the executor — exactly the silent-failure anti-pattern the codebase already enforces against. Implementation: build `_SPAWN_TASK_SCHEMA` lazily inside `register_subtask_tools` and conditionally inject the property; do NOT mutate the module-level constant. Same pattern applied to `submit_final_report`'s optional `payload` property (only added when flag is on).

   **Fail-closed behavior when flag is off** (P2 documentation fix): `submit_final_report`'s `input_schema.additionalProperties=False` means a model that emits a `payload` key with the flag off will be rejected by Anthropic's tool-validator. This is intentional — pre-flag rows can't have schema-validated payloads anyway. Document this in `nous/api/subtask_tools.py` and in the Commit B acceptance checklist so reviewers don't flag it as a regression.

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

1. **Migration 047** (free slot — 043–046 are taken; see §2 Create table) — `heart.boards` table:
   ```sql
   CREATE TABLE IF NOT EXISTS heart.boards (
       id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
       agent_id    TEXT NOT NULL,
       label       TEXT NOT NULL,
       created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
   );
   CREATE INDEX idx_boards_agent ON heart.boards (agent_id, created_at DESC);

   ALTER TABLE heart.subtasks
       ADD COLUMN IF NOT EXISTS board_id UUID NULL
           REFERENCES heart.boards(id) ON DELETE SET NULL;
   CREATE INDEX IF NOT EXISTS idx_subtasks_board ON heart.subtasks (board_id)
       WHERE board_id IS NOT NULL;
   ```
   Agent-scoped (required per project convention: every new table needs `agent_id`). `status`/`closed_at` columns are intentionally **omitted** in v1 — no caller closes a board, so the column would be permanently `'open'`. Add them in a follow-up when a real lifecycle requirement appears.

2. **ORM + CRUD** — `nous/storage/models.py::Board`; `nous/heart/boards.py::BoardManager` (v1 surface: `create`, `get`, `list_by_agent`).

3. **Tools** —
   - `create_board(label)` → `{"board_id": uuid}`.
   - `fan_out(board_id, tasks, frame_type=None, timeout_seconds=None)` → spawns N subtasks each with `board_id` set, returns `{"task_ids": [...]}`.
   - `wait_results(board_id, n_of_m=None, timeout_seconds=300)` — see step 4 for semantics + return shape.

4. **`wait_results` semantics and contract** (P2 clarifications from plan-review):
   - **Terminal predicate**: a row is terminal when `final_outcome IS NOT NULL` (covers the full F061 set: `completed`, `incomplete_blocked`, `incomplete_no_terminal`, `validation_failed`, `timed_out`, `errored`, `cancelled`).
   - **Return shape**: `wait_results` always returns **all terminal rows on the board at the time the wait condition trips**, sorted by `completed_at ASC`. Each row is rendered as a `SubtaskResult.to_dict()` blob. Still-running subtasks are NOT included — the caller can re-poll for them later. (This is option (a) from the review; (b) "first n_of_m only" is ambiguous when ≥ n_of_m terminate in the same poll tick.)
   - **Wait condition**: returns when EITHER (i) at least `n_of_m` rows are terminal (or all of them if `n_of_m=None`), OR (ii) wall-clock deadline (`timeout_seconds`) expires.
   - **Polling loop**: pure-async via `asyncio.sleep` with exponential backoff (start `blackboard_poll_interval_seconds`, cap `blackboard_poll_max_interval_seconds`). Single `SELECT id, final_outcome, completed_at, report_jsonb FROM heart.subtasks WHERE board_id=$1` per poll — no per-row round trips. Index `idx_subtasks_board` covers the predicate.
   - **CancelledError handling**: the polling loop MUST **propagate `asyncio.CancelledError` unchanged** to the caller. No `except asyncio.CancelledError`, no fall-through `except Exception:` that catches BaseException. Per `feedback_gather_cancellederror.md`, swallowing CancelledError here would hang agent shutdown. The caller (`spawn_task`'s outer `asyncio.wait_for` or the agent cancellation handler) is responsible for cleanup.
   - **Helper location**: `nous/heart/boards.py::poll_terminal(board_id, n_of_m, deadline_monotonic)`. Returns the same dict-list as `wait_results`.

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
- `docker compose up` against fresh DB applies migrations **042 + 047** in order (verify the F063 migration is actually present at `sql/migrations/047_f063_blackboard.sql`).
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
| Race between F063 polling and F061 timeout writer | Safe by construction: `wait_results` is **read-only** on `heart.subtasks` — no `UPDATE`, no row lock. The only writer of `final_outcome` is F061's outer handler. Polling sees whatever the latest committed value is; eventual-consistency is acceptable since the worst case is one extra poll cycle. |
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
