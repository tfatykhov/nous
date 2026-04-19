# Implementation Plan — Issue #327 / F046

**Branch:** `feat/F046-dag-node-timeout-config`
**Base:** `main` @ `f9a4f85`
**Feature spec:** [`docs/features/F046-dag-node-timeout-config.md`](../../features/F046-dag-node-timeout-config.md)

---

## Summary

Wire env-var-driven timeouts into DAG node execution. Replace the hardcoded `timeout_seconds: int = Field(120, ge=1, le=600)` with:

- `timeout_seconds: int | None = Field(None, ge=1)` (spec)
- `NOUS_DAG_NODE_DEFAULT_TIMEOUT` (default 600) — substitutes for `None` at insert time
- `NOUS_DAG_NODE_MAX_TIMEOUT` (default 7200) — clamps both at insert and at read sites

`DAGStore` and `DAGOrchestrator` receive `Settings` via constructor DI.

---

## Task order

Single agent (python-engineer) runs tasks 1–7 sequentially on branch `feat/F046-dag-node-timeout-config`. Test-engineer runs task 8 after task 7 commits.

**Atomicity note (from code-review f263d88a).** Tasks 2 + 3 MUST land in the same commit. Applying Task 2 alone lets `spec.timeout_seconds` be `None`, and the current `store.py:78` passes it straight into an `INT NOT NULL` ORM column — a partial bisect/cherry-pick would break DB inserts. Python-engineer commits tasks 1–7 as a single "feat(dag): wire env-var timeouts" commit (one logical change).

### Task 1 — Config (`nous/config.py`)

Insert after the existing `subtask_*` block (~line 239):

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
    description="Hard ceiling (seconds) for DAG node timeout_seconds",
)
```

Add a standalone `@model_validator(mode="after")` method `_validate_dag_timeouts` that raises `ValueError` when `self.dag_node_default_timeout > self.dag_node_max_timeout` and returns `self`. **Do not merge** with existing validators — Pydantic v2 runs multiple `model_validator(mode="after")` methods in definition order without conflict. The Settings class already has 5 such validators (~lines 467–513); this becomes the 6th.

**Verify:**
- `uv run python -c "from nous.config import Settings; s = Settings(); print(s.dag_node_default_timeout, s.dag_node_max_timeout)"` → `600 7200`
- `NOUS_DAG_NODE_DEFAULT_TIMEOUT=300 NOUS_DAG_NODE_MAX_TIMEOUT=900 uv run python -c "..."` → `300 900`
- `NOUS_DAG_NODE_DEFAULT_TIMEOUT=9999 NOUS_DAG_NODE_MAX_TIMEOUT=1000 uv run python -c "..."` → ValidationError

### Task 2 — Schema (`nous/dag/schemas.py`)

Replace line 75:

```python
# before
timeout_seconds: int = Field(120, ge=1, le=600, description="Execution timeout")
# after
timeout_seconds: int | None = Field(
    None,
    ge=1,
    description=(
        "Execution timeout (seconds). None means 'use NOUS_DAG_NODE_DEFAULT_TIMEOUT'. "
        "Values above NOUS_DAG_NODE_MAX_TIMEOUT are clamped at insert."
    ),
)
```

No other edits in this file.

**Verify:**
- `DAGNodeSpec(name="x", type=DAGNodeType.subtask, timeout_seconds=3600)` → accepted
- `DAGNodeSpec(name="x", type=DAGNodeType.subtask)` → `timeout_seconds is None`
- `DAGNodeSpec(name="x", type=DAGNodeType.subtask, timeout_seconds=0)` → ValidationError

### Task 3 — Store (`nous/dag/store.py`)

Modify `DAGStore.__init__` — `settings` is **required** (no default), matching fail-fast DI pattern used by critic.py/checks.py:

```python
from nous.config import Settings

class DAGStore:
    def __init__(self, database: Database, agent_id: str, settings: Settings) -> None:
        self._db = database
        self._agent_id = agent_id
        self._settings = settings
```

In `create()`, just before constructing each `DAGNode(...)` inside the `for spec in request.nodes:` loop (around line 67), compute the resolved timeout and use it:

```python
resolved_timeout = min(
    spec.timeout_seconds if spec.timeout_seconds is not None else self._settings.dag_node_default_timeout,
    self._settings.dag_node_max_timeout,
)
node = DAGNode(
    ...
    timeout_seconds=resolved_timeout,
    ...
)
```

**Verify:** passes task-8 store tests.

### Task 4 — Orchestrator (`nous/dag/orchestrator.py`)

Modify `DAGOrchestrator.__init__` — `settings` is **required** (no default). Silent `None` fallback was rejected in spec review (6278cd50): would mask misconfiguration and defeat defense-in-depth clamping.

```python
from nous.config import Settings

def __init__(
    self,
    store: DAGStore,
    subtask_mgr: SubtaskManager | None = None,
    dynamic_loader: DynamicCheckLoader | None = None,
    bus: EventBus | None = None,
    *,
    settings: Settings,
) -> None:
    ...
    self._settings = settings
```

(`settings` kept keyword-only behind `*,` to preserve positional-arg compatibility for existing callers that pass `store, subtask_mgr, dynamic_loader, bus` positionally.)

Add a helper method (place it near the other private helpers, e.g. after `_run_completion_check`):

```python
def _effective_timeout(self, node: DAGNode) -> int:
    """Clamp node.timeout_seconds to settings.dag_node_max_timeout.

    Defensive re-clamp: store already clamps at insert, but historical rows
    or direct DB writes may carry values above the current ceiling.
    """
    return min(node.timeout_seconds, self._settings.dag_node_max_timeout)
```

Replace three usages:

- Line 362 (completion-check timeout):
  ```python
  # before
  if elapsed_total > node.timeout_seconds:
  # after
  effective_timeout = self._effective_timeout(node)
  if elapsed_total > effective_timeout:
  ```
  And update the error message at line 366 to use `effective_timeout`.

- Line 624 (`subtask_mgr.create(timeout=...)`):
  ```python
  # before
  timeout=node.timeout_seconds,
  # after
  timeout=self._effective_timeout(node),
  ```

- Line 668 (`dynamic_loader.create_check(timeout_seconds=...)`):
  ```python
  # before
  timeout_seconds=node.timeout_seconds,
  # after
  timeout_seconds=self._effective_timeout(node),
  ```

**Verify:** passes task-8 orchestrator clamp test.

### Task 5 — Tools (`nous/api/tools.py`)

Line 1833:

```python
# before
timeout_seconds=n.get("timeout_seconds", 120),
# after
timeout_seconds=n.get("timeout_seconds"),
```

Line 1953 (JSON tool schema for `dag_create`): update the `"timeout_seconds"` entry to include a `minimum` and refreshed description:

```python
"timeout_seconds": {
    "type": "integer",
    "minimum": 1,
    "description": "Execution timeout in seconds (default: NOUS_DAG_NODE_DEFAULT_TIMEOUT, ceiling: NOUS_DAG_NODE_MAX_TIMEOUT)",
},
```

`minimum: 1` mirrors the `ge=1` Pydantic constraint so the LLM doesn't propose 0 or negative values that downstream validation would reject.

(Preserve surrounding JSON structure exactly.)

### Task 6 — main.py wiring (`nous/main.py`)

Lines 578–584:

```python
dag_store = DAGStore(database, agent_id=settings.agent_id, settings=settings)
dag_orchestrator = DAGOrchestrator(
    store=dag_store,
    subtask_mgr=heart.subtasks,
    dynamic_loader=dynamic_loader,
    bus=bus,
    settings=settings,
)
```

### Task 6b — Test fixture updates (included in implementation commit, not Task 8)

`DAGStore` now requires `settings`, so every test construction site must be updated. Enumerated exhaustively:

| File | Lines | Current | New |
|---|---|---|---|
| `tests/test_dag_store.py` | 22 | `DAGStore(db, agent_id)` | `DAGStore(db, agent_id, Settings())` |
| `tests/test_dag_store.py` | 200, 201, 217, 218 | `DAGStore(db, f"agent-...")` | add `, Settings()` positional |
| `tests/test_dag_orchestrator.py` | 26 | `DAGStore(db, agent_id)` | `DAGStore(db, agent_id, Settings())` |
| `tests/test_dag_tools.py` | 26 | `DAGStore(db, agent_id=agent_id)` | `DAGStore(db, agent_id=agent_id, settings=Settings())` |
| `tests/test_dag_dashboard.py` | 34, 62, 89, 118 | `DAGStore(db, agent_id=agent_id)` | `DAGStore(db, agent_id=agent_id, settings=Settings())` |

Every test file must `from nous.config import Settings` (import may already exist).

Orchestrator fixtures must also pass `settings=Settings()`:

| File | Location | Change |
|---|---|---|
| `tests/test_dag_orchestrator.py` | the `orchestrator` fixture (grep for `DAGOrchestrator(`) | add `settings=Settings()` keyword arg |

### Task 7 — docker-compose.yml

Insert next to the existing `NOUS_SUBTASK_*` block (~line 42, after `NOUS_SUBTASK_MAX_TIMEOUT`):

```yaml
      - NOUS_DAG_NODE_DEFAULT_TIMEOUT=${NOUS_DAG_NODE_DEFAULT_TIMEOUT:-600}
      - NOUS_DAG_NODE_MAX_TIMEOUT=${NOUS_DAG_NODE_MAX_TIMEOUT:-7200}
```

### Task 8 — Tests (test-engineer, after task 7 committed)

Add a new test class `TestDAGNodeSpecTimeout` to `tests/test_dag_schemas.py` (separate from `TestDAGNodeModel` which tests the ORM):

```python
class TestDAGNodeSpecTimeout:
    """F046: Pydantic spec-level timeout validation."""

    def test_timeout_accepts_values_above_legacy_ceiling(self):
        node = DAGNodeSpec(name="x", type=DAGNodeType.subtask, timeout_seconds=3600)
        assert node.timeout_seconds == 3600

    def test_timeout_default_is_none(self):
        node = DAGNodeSpec(name="x", type=DAGNodeType.subtask)
        assert node.timeout_seconds is None

    def test_timeout_rejects_non_positive(self):
        with pytest.raises(ValidationError):
            DAGNodeSpec(name="x", type=DAGNodeType.subtask, timeout_seconds=0)
        with pytest.raises(ValidationError):
            DAGNodeSpec(name="x", type=DAGNodeType.subtask, timeout_seconds=-1)
```

**Do NOT touch** `test_dag_schemas.py:65` — it lives in `TestDAGNodeModel.test_defaults()` which builds a `DAGNode` **ORM model** (SQLAlchemy), not a `DAGNodeSpec`. The ORM column has `server_default=120`; the ORM default stays 120, so the assertion `node.timeout_seconds == 120` at line 65 remains correct. Lines 83/91 also stay as-is (they pass explicit `timeout_seconds=60`).

Add hermetic store tests to `tests/test_dag_store.py` (use an explicit `Settings(...)` override so ambient env doesn't leak in — flagged by arch review e4b05866):

```python
_TEST_DAG_SETTINGS = Settings(dag_node_default_timeout=600, dag_node_max_timeout=7200)

@pytest.fixture
def dag_settings():
    return _TEST_DAG_SETTINGS

@pytest.mark.asyncio
async def test_create_resolves_none_to_default(db, agent_id, dag_settings):
    store = DAGStore(db, agent_id, dag_settings)
    req = _build_request(nodes=[DAGNodeSpec(name="n", type=DAGNodeType.subtask, instructions="x")])
    dag = await store.create(req)
    assert dag.nodes[0].timeout_seconds == dag_settings.dag_node_default_timeout  # 600

@pytest.mark.asyncio
async def test_create_clamps_to_max(db, agent_id, dag_settings):
    store = DAGStore(db, agent_id, dag_settings)
    req = _build_request(nodes=[DAGNodeSpec(name="n", type=DAGNodeType.subtask, instructions="x", timeout_seconds=999999)])
    dag = await store.create(req)
    assert dag.nodes[0].timeout_seconds == dag_settings.dag_node_max_timeout  # 7200

@pytest.mark.asyncio
async def test_create_preserves_explicit_value(db, agent_id, dag_settings):
    store = DAGStore(db, agent_id, dag_settings)
    req = _build_request(nodes=[DAGNodeSpec(name="n", type=DAGNodeType.subtask, instructions="x", timeout_seconds=300)])
    dag = await store.create(req)
    assert dag.nodes[0].timeout_seconds == 300
```

If a `_build_request` helper doesn't exist, mirror the shape of existing tests in the file (or inline a minimal `DAGCreateRequest`).

Add to `tests/test_dag_orchestrator.py`:

```python
@pytest.mark.asyncio
async def test_orchestrator_clamps_node_timeout_to_max(orchestrator, subtask_mgr):
    settings = orchestrator._settings  # the fixture-configured Settings
    node = DAGNode(
        id=uuid4(),
        dag_id=uuid4(),
        name="n",
        node_type="subtask",
        status="ready",
        timeout_seconds=99999,  # above ceiling
        wave=0,
        instructions="x",
    )
    dag = ExecutionDAG(id=uuid4(), agent_id="test", name="t", status="running", nodes=[node], edges=[])
    await orchestrator._launch_subtask_node(node, dag)
    subtask_mgr.create.assert_called_once()
    _, kwargs = subtask_mgr.create.call_args
    assert kwargs["timeout"] == settings.dag_node_max_timeout
```

The existing `orchestrator` fixture must be updated to pass `settings=Settings(dag_node_default_timeout=600, dag_node_max_timeout=7200)` for hermeticity.

Config test — `tests/test_config.py` does **not** exist (code review f263d88a verified). Create it:

```python
"""Core Settings validation tests."""

import pytest
from pydantic import ValidationError

from nous.config import Settings


class TestDAGTimeoutValidation:
    def test_default_must_not_exceed_max(self, monkeypatch):
        monkeypatch.setenv("NOUS_DAG_NODE_DEFAULT_TIMEOUT", "10000")
        monkeypatch.setenv("NOUS_DAG_NODE_MAX_TIMEOUT", "7200")
        with pytest.raises(ValidationError):
            Settings()

    def test_default_equals_max_is_ok(self, monkeypatch):
        monkeypatch.setenv("NOUS_DAG_NODE_DEFAULT_TIMEOUT", "900")
        monkeypatch.setenv("NOUS_DAG_NODE_MAX_TIMEOUT", "900")
        s = Settings()
        assert s.dag_node_default_timeout == 900

    def test_defaults(self):
        s = Settings()
        assert s.dag_node_default_timeout == 600
        assert s.dag_node_max_timeout == 7200
```

**Verify:**
- `uv run pytest tests/test_dag_schemas.py tests/test_dag_store.py tests/test_dag_orchestrator.py tests/test_config.py -v` → all pass
- `uv run pytest tests/ -v -k "dag or timeout"` → no regressions in adjacent DAG tests

---

## Review checkpoints

### Spec-phase review (before implementation)

Three parallel agents, each with own `agent_id` + forge-protocol:

- **code-architect** (`spec-reviewer-arch`) — wiring/DI correctness, blast radius, backward-compat analysis, migration decision review.
- **code-reviewer** (`spec-reviewer-code`) — verify the referenced line numbers match the actual codebase; catch phantom APIs/signature drift.
- **python-pro** (`spec-reviewer-python`) — Pydantic v2 idioms, `model_validator` placement, type annotations (`int | None`), settings injection pattern.

Each agent produces P0/P1/P2 findings, records decisions, verdict = APPROVE / APPROVE_WITH_REVISIONS / REWORK. Lead synthesizes; iterates until all three are APPROVE (or clear APPROVE_WITH_REVISIONS once fixes are applied).

### Implementation-phase review (after code lands on branch)

Three parallel agents against the diff:

- **code-reviewer** (`impl-reviewer-code`) — match the plan, style, no dead code, no unrelated edits.
- **silent-failure-hunter** (`impl-reviewer-silent`) — check error-handling paths: ValidationError propagation, settings=None fallback in orchestrator, missing type conversions.
- **python-pro** (`impl-reviewer-python`) — type hints, async correctness, idiom adherence.

Same verdict pattern. Iterate P0/P1 to zero.

---

## Files touched

| File | Change |
|---|---|
| `nous/config.py` | +14 lines (two Field + model_validator) |
| `nous/dag/schemas.py` | ±1 line (timeout_seconds field) |
| `nous/dag/store.py` | +1 import, +1 required ctor param, +4 lines in create() |
| `nous/dag/orchestrator.py` | +1 import, +1 required kw-only ctor param, +7-line helper, 3 call-site edits |
| `nous/api/tools.py` | 3 line edits (fallback, JSON schema description, minimum:1) |
| `nous/main.py` | 2 line edits (DI wiring) |
| `docker-compose.yml` | +2 lines |
| `tests/test_dag_schemas.py` | +1 class, 3 tests (DO NOT change line 65) |
| `tests/test_dag_store.py` | +3 tests, +1 fixture (dag_settings), 5 DAGStore() call-site updates |
| `tests/test_dag_orchestrator.py` | +1 test, 2 call-site updates (store fixture, orchestrator fixture) |
| `tests/test_dag_tools.py` | 1 DAGStore() call-site update |
| `tests/test_dag_dashboard.py` | 4 DAGStore() call-site updates |
| `tests/test_config.py` | **new file**, 3 tests |
| `docs/features/F046-dag-node-timeout-config.md` | new |
| `docs/features/INDEX.md` | +1 row for F046 under "Shipped" |
| `CLAUDE.md` | +2 rows in env-var table, +1 row in "What's Shipped" |

---

## Out of scope (explicit)

- DB migration to bump `dag_nodes.timeout_seconds` server_default.
- Changes to `completion_check_interval` or `max_check_attempts` caps.
- Touching `NOUS_SUBTASK_*` env vars or the regular subtask timeout system.
- Runner.sh changes (handled separately per issue context).
