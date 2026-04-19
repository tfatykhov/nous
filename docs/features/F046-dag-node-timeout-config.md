# F046: Configurable DAG Node Timeouts

**Status:** Shipped
**Proposed by:** Tim
**Date:** 2026-04-18
**Depends on:** F038 (Unified DAG Orchestration — shipped), F038.1 (DAG Completion Check — shipped)
**Blocks:** None
**Issue:** [#327](https://github.com/tfatykhov/nous/issues/327)

---

## Problem

`DAGNodeSpec.timeout_seconds` in `nous/dag/schemas.py:75` is hardcoded:

```python
timeout_seconds: int = Field(120, ge=1, le=600, description="Execution timeout")
```

Three consequences:

1. **Default is too short.** 120 s. Any real subtask — Claude Code job, deep-research, multi-phase workflow — overruns immediately.
2. **Ceiling is too low.** 600 s hard-blocks long-running work. Raising it requires a code change + redeploy.
3. **No env-var override.** Unlike the sibling regular-subtask timeouts (`NOUS_SUBTASK_DEFAULT_TIMEOUT`, `NOUS_SUBTASK_MAX_TIMEOUT`), DAG nodes have no operator knob. Raising the subtask envs does *not* touch DAG nodes — the subsystems are separate.

### Symptom

Smoke-test DAG `43b37234` (2026-04-19) launched a Claude Code job with `completion_check_interval=60, max_check_attempts=40` (designed to poll for ~2400 s). It died at 120 s with:

```
Completion check timed out after 120s (2 attempts)
```

The Claude Code job itself completed successfully in 1 m 45 s — but the DAG had already marked the node failed. Even raising the ceiling manually to 600 s only buys 10 min, insufficient for typical Claude Code (20–40 min) or long-running research.

---

## Goals

- Let operators tune DAG-node timeouts via env vars, matching the regular-subtask pattern.
- Raise the ceiling so long-running DAG nodes (Claude Code, deep-research) aren't architecturally blocked.
- Keep the change backward compatible for existing callers that pass explicit `timeout_seconds ≤ 600`.
- Surface the value in both the inline-subtask path (`subtask_mgr.create(timeout=...)`) and the completion-check polling path (`_poll_awaiting_checks`) and the dynamic-check path (`dynamic_loader.create_check(timeout_seconds=...)`).

## Non-goals

- No change to `completion_check_interval` or `max_check_attempts` semantics.
- No change to inline-subtask (`await_result`) timeouts (`NOUS_INLINE_SUBTASK_TIMEOUT`).
- No change to regular-subtask timeouts (`NOUS_SUBTASK_*`).
- No database migration. Historical rows (server_default=120) remain valid — Python code always writes an explicit resolved value going forward.

---

## Design

### 1. Config — two new settings (`nous/config.py`)

```python
# F046: DAG node timeouts
dag_node_default_timeout: int = Field(
    600,
    validation_alias="NOUS_DAG_NODE_DEFAULT_TIMEOUT",
    ge=1,
    description="Default timeout (seconds) for DAG nodes when node spec omits timeout_seconds",
)
dag_node_max_timeout: int = Field(
    7200,
    validation_alias="NOUS_DAG_NODE_MAX_TIMEOUT",
    ge=1,
    description="Hard ceiling (seconds) for DAG node timeout_seconds — clamps both insert and read paths",
)
```

A `model_validator(mode="after")` enforces `dag_node_default_timeout <= dag_node_max_timeout`. If an operator sets the default above the max, Settings construction fails fast with a clear error — rather than silently shipping a clamped default that later surprises them.

### 2. Schema — make timeout_seconds optional (`nous/dag/schemas.py`)

```python
timeout_seconds: int | None = Field(
    None,
    ge=1,
    description="Execution timeout (seconds). None means 'use NOUS_DAG_NODE_DEFAULT_TIMEOUT'. "
                "Values above NOUS_DAG_NODE_MAX_TIMEOUT are clamped at insert time.",
)
```

Three changes vs today:

- **Default `None`, not `120`** — decouples schema default from config default so `NOUS_DAG_NODE_DEFAULT_TIMEOUT` actually takes effect when node spec omits the field.
- **Removed `le=600`** — the upper bound moves to config (`dag_node_max_timeout`). Schema only enforces `ge=1` (sanity: no negative/zero timeouts).
- **Optional type** — `int | None`. None is the "unspecified" marker; store.create resolves it.

### 3. Store — resolve and clamp at insert (`nous/dag/store.py`)

`DAGStore.__init__` grows a `settings: Settings` parameter. In `create()`, before building each `DAGNode` ORM row:

```python
resolved_timeout = min(
    spec.timeout_seconds if spec.timeout_seconds is not None else self._settings.dag_node_default_timeout,
    self._settings.dag_node_max_timeout,
)
```

The resolved value is written to the ORM column (still `NOT NULL`). Downstream readers see a concrete int; no DB-level nullability change required.

### 4. Orchestrator — defensive clamp at read sites (`nous/dag/orchestrator.py`)

`DAGOrchestrator.__init__` grows a `settings: Settings` parameter. Three call sites read `node.timeout_seconds`:

| Line | Purpose | Fix |
|---|---|---|
| 362 | completion-check timeout comparison | `effective = min(node.timeout_seconds, settings.dag_node_max_timeout)` |
| 624 | passed to `subtask_mgr.create(timeout=...)` | same clamp |
| 668 | passed to `dynamic_loader.create_check(timeout_seconds=...)` | same clamp |

Factored into a private helper `_effective_timeout(node) -> int`. Clamp runs *even though* the store already clamped on insert — this is defense-in-depth for:

- Legacy rows written before F046 (they're below the new ceiling, so no-op; but the code is correct under all ordering).
- Manual DB edits or data imports that bypass the store.
- Future callers that update the ORM directly.

### 5. Tools — drop hardcoded 120 fallback (`nous/api/tools.py:1833`)

```python
# before
timeout_seconds=n.get("timeout_seconds", 120),
# after
timeout_seconds=n.get("timeout_seconds"),
```

The JSON tool schema description (line 1953) is updated: `"timeout_seconds": {"type": "integer", "description": "Execution timeout in seconds (default: NOUS_DAG_NODE_DEFAULT_TIMEOUT, ceiling: NOUS_DAG_NODE_MAX_TIMEOUT)"}`.

### 6. main.py wiring

```python
dag_store = DAGStore(database, agent_id=settings.agent_id, settings=settings)
dag_orchestrator = DAGOrchestrator(
    store=dag_store,
    subtask_mgr=heart.subtasks,
    dynamic_loader=dynamic_loader,
    bus=bus,
    settings=settings,  # F046
)
```

### 7. docker-compose.yml — pass-through

```yaml
- NOUS_DAG_NODE_DEFAULT_TIMEOUT=${NOUS_DAG_NODE_DEFAULT_TIMEOUT:-600}
- NOUS_DAG_NODE_MAX_TIMEOUT=${NOUS_DAG_NODE_MAX_TIMEOUT:-7200}
```

Inserted in the `nous` service `environment:` block adjacent to existing `NOUS_SUBTASK_*` entries for discoverability.

### 8. No DB migration

The `dag_nodes.timeout_seconds` column is `INT NOT NULL DEFAULT 120` (migration 032). Python code always writes an explicit resolved int now, so the server_default is irrelevant for new rows and the existing rows remain valid. Bumping the server_default to 600 would be cosmetic; skipped to keep the change surgical.

---

## Tests

`tests/test_dag_schemas.py`:

- `test_timeout_accepts_large_value` — `DAGNodeSpec(..., timeout_seconds=3600)` succeeds (no `le=600` anymore).
- `test_timeout_default_is_none` — spec omitting the field has `timeout_seconds is None`.
- `test_timeout_rejects_zero_or_negative` — `ge=1` still enforced.
- Update existing `assert node.timeout_seconds == 120` — now `assert node.timeout_seconds is None`.

`tests/test_dag_store.py`:

- `test_create_resolves_none_to_default` — spec with `timeout_seconds=None` produces a row with `settings.dag_node_default_timeout` written.
- `test_create_clamps_to_max` — spec with `timeout_seconds=999999` produces a row with `settings.dag_node_max_timeout` written.
- `test_create_passes_explicit_value_through` — spec with `timeout_seconds=300` produces a row with `timeout_seconds=300`.

`tests/test_dag_orchestrator.py`:

- `test_orchestrator_clamps_node_timeout_to_max` — construct a DAGNode with `timeout_seconds=99999` in memory (bypassing store), launch_subtask, verify `subtask_mgr.create` is called with `timeout=settings.dag_node_max_timeout`.
- Existing `test_completion_check_timeout` keeps working (uses `timeout=1`).

`tests/test_config.py` (if it exists — otherwise a new small test):

- `test_dag_node_default_must_not_exceed_max` — `Settings(dag_node_default_timeout=10000, dag_node_max_timeout=7200)` raises `ValidationError`.

---

## Backward compatibility

- Existing callers passing explicit `timeout_seconds ≤ 600` → unchanged behavior (value ≤ new ceiling of 7200, so clamp is no-op).
- Existing callers passing explicit `timeout_seconds = 601..7200` → previously rejected by schema (`le=600`); now accepted. Strict broadening only.
- Existing callers omitting `timeout_seconds` → previously got 120, now get `NOUS_DAG_NODE_DEFAULT_TIMEOUT` (default 600). This is the **only observable behavior change** for callers that don't set the field.
  - Risk: a caller that relied on the 120 s default as a short safety net may now let nodes run longer.
  - Mitigation: callers who want the old behavior pass `timeout_seconds=120` explicitly, or set `NOUS_DAG_NODE_DEFAULT_TIMEOUT=120`.
  - The change is documented in CLAUDE.md and the PR body.
- Historical DB rows (server_default=120) → orchestrator reads them, `min(120, 7200) = 120`, no behavior change.

---

## Rollout

1. Land PR to `main`.
2. Operators can tune immediately via env vars — no code change, no migration.
3. The DAG-orchestrator timeout is no longer the bottleneck for long-running Claude Code delegation (primary motivating use case).

---

## Open questions

None blocking. The migration-skip decision is conservative; if an operator later wants DB-level defaults to match Python defaults, a one-line `ALTER TABLE dag_nodes ALTER COLUMN timeout_seconds SET DEFAULT 600` migration can be added without affecting this PR.
