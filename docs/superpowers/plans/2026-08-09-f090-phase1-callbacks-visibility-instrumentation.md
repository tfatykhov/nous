# F090 Phase 1 — Callback Execution, Finished-DAG Visibility, Phase-2 Gate Instrumentation

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make callback nodes actually execute, make finished DAGs discoverable, and measure whether Phase 2 (worklog/blackboard) is worth building.

**Architecture:** Callback nodes stop being a no-op stub and become subtask-backed — routed through the same `SubtaskManager` path as `subtask` nodes, with predecessor results injected by the existing `_build_predecessor_context`. Because they then carry a `subtask_id`, every F087 backstop (wall-clock reaper, token roll-up, status sync, cancellation confirmation) applies to them automatically. Seven call sites currently gate on `node_type == "subtask"`; all seven move to a single shared constant so the set can never drift again. Visibility and instrumentation are read-only additions over existing rows — no new tables.

**Tech Stack:** Python 3.12+, SQLAlchemy 2.0 async, pydantic-settings, pytest + pytest-asyncio, PostgreSQL 17.

## Global Constraints

- Every new table would need `agent_id`; this plan adds **no new tables** — all metrics compute from `nous_system.dag_nodes` / `execution_dags`.
- New behaviour ships behind a flag defaulting **OFF**. Read-only additions (visibility, metrics) need no flag.
- Tests construct `Settings(_env_file=None, ...)`. A bare `Settings()` inherits the developer `.env` and produces failures that are green in CI — this cost three red commits during F087.
- Verification gate is **CI on the head SHA**, not a local `-k` selection. A filtered local run must be checked with `pytest --collect-only -q <selection> | grep <file>` to confirm it includes the files touched.
- Match the surrounding comment density in `nous/dag/` — cite the finding or reasoning that motivated a non-obvious branch.

---

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `nous/dag/orchestrator.py` | State machine; the seven subtask-only gates; callback launch | Modify |
| `nous/config.py` | `dag_callback_execution_enabled` flag | Modify |
| `nous/api/tools.py` | `dag_create` callback field docs; `dag_manage` list/`recent` action | Modify |
| `nous/api/dashboard_queries.py` | Phase-2 gate signals block | Modify |
| `tests/test_dag_callback_execution.py` | Callback execution + all seven gates | Create |
| `tests/test_dag_visibility.py` | Finished-DAG listing | Create |
| `tests/test_dag_phase2_signals.py` | Instrumentation | Create |
| `CLAUDE.md` | New flag row; F090 feature row | Modify |

---

### Task 1: Collapse the seven subtask-only gates into one constant

Pure refactor. Callback nodes have no `subtask_id` yet, so every gate short-circuits exactly as before — this task must produce **zero behaviour change**. It exists separately so Task 2 cannot miss a site, which is the failure mode that produced seven review rounds on F087.

**Files:**
- Modify: `nous/dag/orchestrator.py` (7 sites: 718, 803, 1316, 1385, 1709, 2070, 2238)
- Test: `tests/test_dag_callback_execution.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `_SUBTASK_BACKED: frozenset[str]` — node types whose work runs as a `heart.subtasks` row. Task 2 relies on this name.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_dag_callback_execution.py
from __future__ import annotations

from nous.dag import orchestrator as orch_mod


def test_subtask_backed_covers_subtask_and_callback():
    """The seven gates that used to hardcode 'subtask' now share one set.

    F087 review found the same fix applied to some sites and not others,
    repeatedly. A single constant makes the set impossible to drift.
    """
    assert orch_mod._SUBTASK_BACKED == frozenset({"subtask", "callback"})


def test_no_bare_subtask_type_comparisons_remain():
    """Guard against a future site re-hardcoding the literal."""
    import inspect

    src = inspect.getsource(orch_mod)
    assert 'node_type == "subtask"' not in src
    assert 'node_type != "subtask"' not in src
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_dag_callback_execution.py -v`
Expected: FAIL — `AttributeError: module 'nous.dag.orchestrator' has no attribute '_SUBTASK_BACKED'`

- [ ] **Step 3: Add the constant**

In `nous/dag/orchestrator.py`, beside `_SETTLED_SUBTASK_STATUSES`:

```python
# Node types whose work executes as a heart.subtasks row, and which therefore
# carry a subtask_id. Every F087 backstop — wall-clock reaper, token roll-up,
# status sync, cancellation confirmation — keys off this set rather than the
# literal "subtask", because F087's review repeatedly found one site fixed and
# a sibling missed. Adding a subtask-backed node type must mean editing this
# line and nothing else.
_SUBTASK_BACKED = frozenset({"subtask", "callback"})
```

- [ ] **Step 4: Replace all seven gates**

`:718` in `_sync_node_statuses`:
```python
            if node.node_type in _SUBTASK_BACKED and node.subtask_id:
                await self._sync_subtask_node(node, dag)
```

`:803` in `_reconcile_token_accounting`:
```python
            if node.node_type not in _SUBTASK_BACKED or not node.subtask_id:
                continue  # gate/fix nodes never consume tokens
```

`:1316` and `:1385` in `_reap_overrun_nodes` (execution-clock lookup, and post-cancel confirmation):
```python
            if node.node_type in _SUBTASK_BACKED and node.subtask_id and self._subtask_mgr:
```

`:1709` in `_dispatch_ready_nodes` (F064.2 caps):
```python
            if node.node_type not in _SUBTASK_BACKED:
```

`:2070` in `_launch_node`:
```python
        if node_type in _SUBTASK_BACKED:
            await self._launch_subtask_node(node, dag)
```
Delete the now-dead `elif node_type == "callback":` branch entirely (lines 2084-2093) — Task 2 gives callbacks their real behaviour, and leaving the stub would shadow it.

`:2238` in `_cancel_node`:
```python
        if node.node_type in _SUBTASK_BACKED and node.subtask_id and self._subtask_mgr:
```

- [ ] **Step 5: Run tests to verify they pass and nothing regressed**

Run: `uv run pytest tests/test_dag_callback_execution.py -v`
Expected: PASS (2 tests)

Run: `uv run pytest tests/test_dag_orchestrator.py tests/test_dag_durability.py tests/test_dag_delivery.py tests/test_dag_tools.py tests/test_f066_1_fix_stage.py -q`
Expected: PASS — all of it. Callbacks still have no `subtask_id`, so `_launch_subtask_node` will now be *reached* for them but `_dispatch_ready_nodes` gates on cap only for subtask-backed types.

> **If `test_dag_orchestrator.py` callback tests fail here:** that is expected and is Task 2's job — a callback now routes to `_launch_subtask_node` and creates a real subtask instead of instantly completing. Do **not** patch it here. Note the failing test names and carry them to Task 2 Step 1.

- [ ] **Step 6: Commit**

```bash
git add nous/dag/orchestrator.py tests/test_dag_callback_execution.py
git commit -m "refactor(dag): one constant for subtask-backed node types

Seven sites hardcoded node_type == 'subtask'. F087's review found the same
fix applied to some and missed on others, round after round; a shared
frozenset makes the set impossible to drift. No behaviour change — callback
nodes still carry no subtask_id, so every gate short-circuits as before."
```

---

### Task 2: Callback nodes execute as real subtasks

**Files:**
- Modify: `nous/config.py` (new flag), `nous/dag/orchestrator.py` (`_launch_subtask_node` guard)
- Test: `tests/test_dag_callback_execution.py`

**Interfaces:**
- Consumes: `_SUBTASK_BACKED` from Task 1.
- Produces: `Settings.dag_callback_execution_enabled: bool` (default `False`).

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/test_dag_callback_execution.py
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

from nous.config import Settings
from nous.dag.orchestrator import DAGOrchestrator
from nous.dag.schemas import DAGCreateRequest, DAGEdgeSpec, DAGNodeSpec, DAGNodeType
from nous.dag.store import DAGStore


def _settings(**overrides) -> Settings:
    base = dict(_env_file=None, dag_callback_execution_enabled=True)
    base.update(overrides)
    return Settings(**base)


@pytest_asyncio.fixture
async def store(db):
    return DAGStore(db, f"test-cb-{uuid.uuid4().hex[:8]}", _settings())


@pytest.fixture
def subtask_mgr():
    mgr = AsyncMock()
    mgr.create.return_value = SimpleNamespace(id=uuid.uuid4(), status="pending")
    return mgr


@pytest.fixture
def dynamic_loader():
    loader = AsyncMock()
    loader._registry = MagicMock()
    loader._registry.get_check.return_value = None
    return loader


def _orch(store, subtask_mgr, dynamic_loader, settings=None):
    o = DAGOrchestrator(
        store=store, subtask_mgr=subtask_mgr,
        dynamic_loader=dynamic_loader, settings=settings or _settings(),
    )
    o.clock_wired = True
    return o


def _callback_after_work() -> DAGCreateRequest:
    return DAGCreateRequest(
        name="cb-dag",
        nodes=[
            DAGNodeSpec(name="work", type=DAGNodeType.subtask,
                        instructions="do the work", timeout_seconds=120),
            DAGNodeSpec(name="handle", type=DAGNodeType.callback,
                        instructions="Review the result and act on it",
                        tools=["bash"], timeout_seconds=120),
        ],
        edges=[DAGEdgeSpec(from_node="work", to_node="handle",
                           edge_type="context_flow")],
    )


class TestCallbackExecutes:
    @pytest.mark.asyncio
    async def test_callback_creates_a_subtask_with_predecessor_context(
        self, store, subtask_mgr, dynamic_loader
    ):
        """The whole point: a callback must READ its predecessor and act.

        Before F090.1 this node completed instantly with its own instruction
        text as the result — 103 callback nodes in the dev DB, all 'completed',
        none having executed anything.
        """
        dag = await store.create(_callback_after_work())
        await store.update_dag_status(dag.id, "running")
        dag = await store.get_dag(dag.id)
        work = next(n for n in dag.nodes if n.name == "work")
        await store.update_node(
            work.id, status="completed", result="BUILD OK: 0 errors",
        )
        subtask_mgr.get.return_value = SimpleNamespace(
            id=uuid.uuid4(), status="running", result=None, error=None,
            final_outcome=None, tokens_in=0, tokens_out=0, started_at=None,
        )

        await _orch(store, subtask_mgr, dynamic_loader).tick()

        assert subtask_mgr.create.await_count == 1
        task_text = subtask_mgr.create.await_args.kwargs["task"]
        assert "BUILD OK: 0 errors" in task_text
        assert "Review the result and act on it" in task_text
        handle = next(
            n for n in (await store.get_dag(dag.id)).nodes if n.name == "handle"
        )
        assert handle.status == "running"
        assert handle.subtask_id is not None

    @pytest.mark.asyncio
    async def test_callback_forwards_tools_and_timeout(
        self, store, subtask_mgr, dynamic_loader
    ):
        dag = await store.create(_callback_after_work())
        await store.update_dag_status(dag.id, "running")
        dag = await store.get_dag(dag.id)
        work = next(n for n in dag.nodes if n.name == "work")
        await store.update_node(work.id, status="completed", result="done")
        subtask_mgr.get.return_value = SimpleNamespace(
            id=uuid.uuid4(), status="running", result=None, error=None,
            final_outcome=None, tokens_in=0, tokens_out=0, started_at=None,
        )

        await _orch(store, subtask_mgr, dynamic_loader).tick()

        kwargs = subtask_mgr.create.await_args.kwargs
        assert kwargs["timeout"] == 120
        assert kwargs["dag_node_id"] is not None

    @pytest.mark.asyncio
    async def test_flag_off_keeps_the_legacy_instant_completion(
        self, store, subtask_mgr, dynamic_loader
    ):
        """Default is OFF: 83 existing DAGs must not start paying for LLM turns
        the moment this deploys."""
        dag = await store.create(_callback_after_work())
        await store.update_dag_status(dag.id, "running")
        dag = await store.get_dag(dag.id)
        work = next(n for n in dag.nodes if n.name == "work")
        await store.update_node(work.id, status="completed", result="done")

        orch = _orch(store, subtask_mgr, dynamic_loader,
                     _settings(dag_callback_execution_enabled=False))
        await orch.tick()

        handle = next(
            n for n in (await store.get_dag(dag.id)).nodes if n.name == "handle"
        )
        assert handle.status == "completed"
        assert handle.subtask_id is None
        assert subtask_mgr.create.await_count == 0

    @pytest.mark.asyncio
    async def test_executing_callback_inherits_the_wall_clock_reaper(
        self, store, subtask_mgr, dynamic_loader
    ):
        """It gets F087's backstops for free by having a subtask_id."""
        from datetime import UTC, datetime, timedelta

        dag = await store.create(_callback_after_work())
        await store.update_dag_status(dag.id, "running")
        dag = await store.get_dag(dag.id)
        handle = next(n for n in dag.nodes if n.name == "handle")
        sid = uuid.uuid4()
        long_ago = datetime.now(UTC) - timedelta(seconds=5000)
        await store.update_node(
            handle.id, status="running", subtask_id=sid, started_at=long_ago,
        )
        state = {"status": "running"}

        async def _get(_s):
            return SimpleNamespace(
                id=sid, status=state["status"], result=None, error=None,
                final_outcome=None, tokens_in=0, tokens_out=0,
                started_at=long_ago,
            )

        async def _cancel(_s):
            state["status"] = "cancelled"
            return True

        subtask_mgr.get = AsyncMock(side_effect=_get)
        subtask_mgr.cancel = AsyncMock(side_effect=_cancel)

        await _orch(store, subtask_mgr, dynamic_loader).tick()

        reaped = next(
            n for n in (await store.get_dag(dag.id)).nodes if n.name == "handle"
        )
        assert reaped.status == "failed"
        assert "exceeded wall-clock budget" in reaped.error
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_dag_callback_execution.py -v`
Expected: FAIL — `pydantic_core.ValidationError` on the unknown setting `dag_callback_execution_enabled`.

- [ ] **Step 3: Add the flag**

In `nous/config.py`, beside `dag_node_reaper_enabled`:

```python
    # F090.1: execute callback nodes instead of instantly completing them with
    # their own instruction text. Default OFF — the dev DB holds 103 callback
    # nodes across 83 DAGs, every one 'completed' having executed nothing, so
    # flipping this on deploy would silently start charging an LLM turn (and
    # adding wall-clock) to every one of those DAG shapes. Flip after reading
    # the callback execution stats from the Phase-2 signals block.
    dag_callback_execution_enabled: bool = False
```

- [ ] **Step 4: Guard the launch**

In `nous/dag/orchestrator.py`, at the top of `_launch_subtask_node`, before the `if not self._subtask_mgr` check:

```python
        # F090.1: a callback node executes only when the flag is on. With it
        # off we reproduce the pre-F090 behaviour exactly — complete instantly,
        # carrying the instruction text as the result — so existing DAG shapes
        # keep their current cost and timing.
        if (
            node.node_type == "callback"
            and not self._settings.dag_callback_execution_enabled
        ):
            now = datetime.now(UTC)
            await self._store.update_node(
                node.id,
                status="completed",
                result=node.instructions or "Callback completed",
                started_at=now,
                completed_at=now,
            )
            node.status = "completed"
            return
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_dag_callback_execution.py -v`
Expected: PASS (6 tests)

- [ ] **Step 6: Fix the callback tests Task 1 flagged**

Run: `uv run pytest tests/test_dag_orchestrator.py -q`

Any test asserting a callback completes instantly must now construct its orchestrator with `Settings(_env_file=None, dag_callback_execution_enabled=False)` — that is the default and the behaviour those tests describe. Do not weaken an assertion to make it pass.

- [ ] **Step 7: Verify the full DAG surface**

Run: `uv run pytest tests/test_dag_callback_execution.py tests/test_dag_orchestrator.py tests/test_dag_durability.py tests/test_dag_delivery.py tests/test_dag_store.py tests/test_dag_tools.py tests/test_dag_schemas.py tests/test_dag_concurrency_caps.py tests/test_dag_stall_detection.py tests/test_dag_workspace_safety.py tests/test_dag_dashboard.py tests/test_f066_1_fix_stage.py tests/test_f066_1_llm_dispatch.py -q`
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add nous/config.py nous/dag/orchestrator.py tests/
git commit -m "feat(dag): F090.1 — callback nodes execute as real subtasks

A callback node was a no-op stub: it marked itself completed and copied its
instruction text into result. 103 callback nodes across 83 DAGs in the dev DB,
all 'completed', none having executed anything — a 100% success rate on a node
type that never ran, which is why it never surfaced as a bug.

Callbacks now route through _launch_subtask_node with predecessor results
injected by _build_predecessor_context, so they get tools, frame_type, model
and timeout — and inherit every F087 backstop by carrying a subtask_id.

Default OFF: flipping on deploy would start charging an LLM turn to 83
existing DAG shapes."
```

---

### Task 3: Document callback fields at the tool surface

**Files:**
- Modify: `nous/api/tools.py` (`dag_create` node schema)
- Test: `tests/test_dag_tools.py`

**Interfaces:**
- Consumes: Task 2's flag name for the doc text.
- Produces: nothing code-facing.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_dag_tools.py
class TestCallbackDocumented:
    def test_callback_semantics_are_described(self, tools):
        schema = tools._schemas["dag_create"]
        node_props = schema["properties"]["nodes"]["items"]["properties"]
        assert "callback" in node_props["type"]["enum"]
        blob = str(schema)
        assert "predecessor" in blob.lower()
        assert "NOUS_DAG_CALLBACK_EXECUTION_ENABLED" in blob
```

> If `tools._schemas` is not the dispatcher's attribute name, read
> `ToolDispatcher.register` in `nous/api/tools.py` and use the real one.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_dag_tools.py::TestCallbackDocumented -v`
Expected: FAIL on the `NOUS_DAG_CALLBACK_EXECUTION_ENABLED` assertion.

- [ ] **Step 3: Extend the node-type description**

In `nous/api/tools.py`, in the `dag_create` node schema, replace the bare `"type"` enum entry with:

```python
                        "type": {
                            "type": "string",
                            "enum": ["subtask", "check", "gate", "callback", "fix"],
                            "description": (
                                "'callback' runs AFTER its predecessors and receives "
                                "their results as context — use it to interpret or act "
                                "on what earlier nodes produced (point a context_flow "
                                "edge at it). It accepts the same tools / frame_type / "
                                "model / timeout_seconds as a subtask. Requires "
                                "NOUS_DAG_CALLBACK_EXECUTION_ENABLED=true; with the flag "
                                "off a callback completes instantly without running. "
                                "'gate' currently auto-passes — it is a marker, not an "
                                "enforced quality check."
                            ),
                        },
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_dag_tools.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add nous/api/tools.py tests/test_dag_tools.py
git commit -m "docs(dag): describe callback semantics at the tool surface

Also states plainly that gate nodes auto-pass, so an author stops treating
them as enforced quality checks."
```

---

### Task 4: F090.3 — make finished DAGs discoverable

**Files:**
- Modify: `nous/api/tools.py` (`dag_manage`)
- Test: `tests/test_dag_visibility.py`

**Interfaces:**
- Consumes: `DAGStore.get_recent_dags(limit)` — already exists at `nous/dag/store.py:226`.
- Produces: `dag_manage(action="recent")`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_dag_visibility.py
from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from unittest.mock import AsyncMock

from nous.api.tools import ToolDispatcher, register_dag_tools
from nous.config import Settings
from nous.dag.orchestrator import DAGOrchestrator
from nous.dag.schemas import DAGCreateRequest, DAGNodeSpec, DAGNodeType
from nous.dag.store import DAGStore


@pytest_asyncio.fixture
async def dag_store(db):
    return DAGStore(db, f"test-vis-{uuid.uuid4().hex[:8]}",
                    Settings(_env_file=None))


@pytest_asyncio.fixture
async def tools(dag_store):
    orch = DAGOrchestrator(
        store=dag_store, subtask_mgr=AsyncMock(), dynamic_loader=AsyncMock(),
        settings=Settings(_env_file=None),
    )
    orch.clock_wired = True
    d = ToolDispatcher()
    register_dag_tools(d, dag_store, orch)
    return d


def _one(name: str) -> DAGCreateRequest:
    return DAGCreateRequest(
        name=name,
        nodes=[DAGNodeSpec(name="n", type=DAGNodeType.subtask,
                           instructions="x", timeout_seconds=120)],
    )


class TestFinishedDagsAreDiscoverable:
    @pytest.mark.asyncio
    async def test_list_still_shows_only_active(self, tools, dag_store):
        finished = await dag_store.create(_one("finished-one"))
        await dag_store.update_dag_status(finished.id, "completed",
                                          result_summary="done")
        await dag_store.create(_one("still-going"))

        text = (await tools._handlers["dag_manage"](action="list"))["content"][0]["text"]

        assert "still-going" in text
        assert "finished-one" not in text

    @pytest.mark.asyncio
    async def test_recent_shows_finished_dags(self, tools, dag_store):
        """Before F090.3 a finished DAG could only be reached by status if you
        already knew its id prefix — there was no way to discover one."""
        finished = await dag_store.create(_one("finished-one"))
        await dag_store.update_dag_status(finished.id, "completed",
                                          result_summary="all good")

        text = (await tools._handlers["dag_manage"](action="recent"))["content"][0]["text"]

        assert "finished-one" in text
        assert str(finished.id)[:8] in text
        assert "completed" in text

    @pytest.mark.asyncio
    async def test_recent_is_empty_message_not_error(self, tools):
        text = (await tools._handlers["dag_manage"](action="recent"))["content"][0]["text"]
        assert "Error" not in text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_dag_visibility.py -v`
Expected: `test_recent_shows_finished_dags` FAILS with `Error: unknown action 'recent'`.

- [ ] **Step 3: Implement the action**

In `nous/api/tools.py` in `dag_manage`, directly after the `if action == "list":` block:

```python
            if action == "recent":
                # F090.3: `list` serves only pending/running. A finished DAG
                # was reachable by `status` only if you already knew its id
                # prefix — there was no way to DISCOVER one, which made the
                # F087 delivery notification the sole record of an outcome.
                dags = await store.get_recent_dags(limit=20)
                finished = [d for d in dags
                            if d.status not in ("pending", "running")]
                if not finished:
                    return {"content": [{"type": "text",
                                         "text": "No finished DAGs."}]}
                lines = [f"Recent finished DAGs ({len(finished)}):"]
                for d in finished:
                    done = sum(1 for n in d.nodes if n.status == "completed")
                    when = d.completed_at.strftime("%Y-%m-%d %H:%M") if d.completed_at else "—"
                    lines.append(
                        f"  {str(d.id)[:8]} | {d.name} | {d.status} | "
                        f"{done}/{len(d.nodes)} nodes | {when}"
                    )
                    if d.result_summary:
                        lines.append(f"      {d.result_summary[:120]}")
                return {"content": [{"type": "text", "text": "\n".join(lines)}]}
```

Add `"recent"` to the action enum:

```python
            "action": {"type": "string",
                       "enum": ["list", "recent", "status", "cancel", "retry_node"]},
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_dag_visibility.py tests/test_dag_tools.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add nous/api/tools.py tests/test_dag_visibility.py
git commit -m "feat(dag): F090.3 — dag_manage action=recent lists finished DAGs

list serves only pending/running, and _resolve_dag's prefix lookup can reach a
finished DAG only if you already know its id. There was no way to discover
one, which left F087's delivery notification as the sole record of an outcome."
```

---

### Task 5: F090.4 — Phase-2 gate instrumentation

Two signals, both computed from existing rows — no new tables, no LLM. **Sibling overlap** answers whether mutual deafness costs anything; **callback/gate execution stats** answer whether the new capability is even used, which is a prerequisite for trusting the first number.

**Files:**
- Modify: `nous/api/dashboard_queries.py`
- Test: `tests/test_dag_phase2_signals.py`

**Interfaces:**
- Consumes: `nous_system.dag_nodes`, `nous_system.execution_dags`.
- Produces: `get_dag_phase2_signals(session, agent_id) -> dict`, surfaced under `phase2_signals` in `get_dag_dashboard_data`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_dag_phase2_signals.py
from __future__ import annotations

import pytest

from nous.api.dashboard_queries import _shingle_overlap, get_dag_phase2_signals


class TestShingleOverlap:
    def test_identical_text_is_total_overlap(self):
        t = "the build succeeded with zero errors and all tests passing"
        assert _shingle_overlap(t, t) == pytest.approx(1.0)

    def test_disjoint_text_is_no_overlap(self):
        a = "the build succeeded with zero errors and all tests passing"
        b = "database migration applied cleanly across every configured shard"
        assert _shingle_overlap(a, b) == pytest.approx(0.0)

    def test_short_text_cannot_form_shingles(self):
        assert _shingle_overlap("too short", "also short") == 0.0

    def test_partial_overlap_is_between(self):
        a = "the build succeeded with zero errors and all tests passing"
        b = "the build succeeded with zero errors but coverage dropped sharply"
        assert 0.0 < _shingle_overlap(a, b) < 1.0


class TestPhase2Signals:
    @pytest.mark.asyncio
    async def test_signals_shape_on_empty_db(self, db):
        async with db.session() as session:
            out = await get_dag_phase2_signals(session, "nobody")
        assert out["sibling_pairs"] == 0
        assert out["overlapping_sibling_pairs"] == 0
        assert out["sibling_overlap_rate"] == 0.0
        assert out["callback_nodes"] == 0
        assert out["callback_executed"] == 0
        assert out["gate_nodes"] == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_dag_phase2_signals.py -v`
Expected: FAIL — `ImportError: cannot import name '_shingle_overlap'`

- [ ] **Step 3: Implement the helper and the query**

Append to `nous/api/dashboard_queries.py`:

```python
def _shingle_overlap(a: str | None, b: str | None) -> float:
    """Jaccard overlap over 6-word shingles. Zero when either side is short.

    F090.4 needs a cheap, deterministic proxy for "these two siblings did
    overlapping work". Six words is long enough that shared boilerplate
    ("the build succeeded") does not by itself register, and an LLM judge is
    deliberately avoided so the gate metric costs nothing to run.
    """
    def _sh(text: str | None) -> set[tuple[str, ...]]:
        words = (text or "").lower().split()
        if len(words) < 6:
            return set()
        return {tuple(words[i:i + 6]) for i in range(len(words) - 5)}

    sa, sb = _sh(a), _sh(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


# Two sibling results this similar are treated as overlapping work.
_SIBLING_OVERLAP_THRESHOLD = 0.15


async def get_dag_phase2_signals(
    session: AsyncSession, agent_id: str
) -> dict[str, Any]:
    """F090.4: the evidence base for whether Phase 2 (worklog/blackboard) is
    worth building.

    Phase 2 exists because parallel siblings are mutually deaf —
    _build_predecessor_context is the only inter-node channel and it reads
    only terminated predecessors. `sibling_overlap_rate` measures whether that
    deafness actually produces duplicated work; the callback/gate counters
    say whether those node types run at all, without which the first number
    describes a graph nobody uses.
    """
    rows = (await session.execute(
        text("""
            SELECT n.dag_id, n.wave, n.result
            FROM nous_system.dag_nodes n
            JOIN nous_system.execution_dags d ON d.id = n.dag_id
            WHERE d.agent_id = :agent_id
              AND n.status = 'completed'
              AND n.result IS NOT NULL
        """),
        {"agent_id": agent_id},
    )).all()

    by_wave: dict[tuple[Any, int], list[str]] = {}
    for dag_id, wave, result in rows:
        by_wave.setdefault((dag_id, wave or 0), []).append(result)

    pairs = 0
    overlapping = 0
    for results in by_wave.values():
        for i in range(len(results)):
            for j in range(i + 1, len(results)):
                pairs += 1
                if _shingle_overlap(results[i], results[j]) >= _SIBLING_OVERLAP_THRESHOLD:
                    overlapping += 1

    counts = (await session.execute(
        text("""
            SELECT n.node_type,
                   count(*)                                    AS total,
                   count(*) FILTER (WHERE n.subtask_id IS NOT NULL) AS executed
            FROM nous_system.dag_nodes n
            JOIN nous_system.execution_dags d ON d.id = n.dag_id
            WHERE d.agent_id = :agent_id
              AND n.node_type IN ('callback', 'gate')
            GROUP BY n.node_type
        """),
        {"agent_id": agent_id},
    )).all()
    by_type = {r[0]: (r[1], r[2]) for r in counts}

    return {
        "sibling_pairs": pairs,
        "overlapping_sibling_pairs": overlapping,
        "sibling_overlap_rate": round(overlapping / pairs, 4) if pairs else 0.0,
        "overlap_threshold": _SIBLING_OVERLAP_THRESHOLD,
        "callback_nodes": by_type.get("callback", (0, 0))[0],
        # subtask_id is non-NULL only when F090.1 actually executed it.
        "callback_executed": by_type.get("callback", (0, 0))[1],
        "gate_nodes": by_type.get("gate", (0, 0))[0],
    }
```

- [ ] **Step 4: Surface it on the dashboard**

At the end of `get_dag_dashboard_data`, before its `return`, add the key to the returned dict:

```python
        "phase2_signals": await get_dag_phase2_signals(session, agent_id),
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_dag_phase2_signals.py tests/test_dag_dashboard.py -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add nous/api/dashboard_queries.py tests/test_dag_phase2_signals.py
git commit -m "feat(dag): F090.4 — Phase-2 gate signals

sibling_overlap_rate measures whether parallel siblings being mutually deaf
actually produces duplicated work — the premise Phase 2 (worklog/blackboard)
rests on. callback_executed / gate_nodes say whether those node types run at
all, without which the overlap number describes a graph nobody uses.

Computed from existing rows: no new tables, no LLM, cheap enough to read on
every dashboard load."
```

---

### Task 6: Documentation

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Add the flag row**

In the env-var table, after `NOUS_DAG_NODE_TIMEOUT_GRACE_SECONDS`:

```markdown
| `NOUS_DAG_CALLBACK_EXECUTION_ENABLED` | `false` | F090.1 — execute callback nodes instead of instantly completing them with their own instruction text. A callback routes through `_launch_subtask_node` with predecessor results injected by `_build_predecessor_context`, so it accepts the same `tools` / `frame_type` / `model` / `timeout_seconds` as a subtask and inherits every F087 backstop by carrying a `subtask_id`. **Default OFF** because the dev DB holds 103 callback nodes across 83 DAGs, every one `completed` having executed nothing — flipping this on deploy silently charges an LLM turn and adds wall-clock to every one of those shapes. Read `phase2_signals.callback_executed` on `/dashboard/dag` after flipping. |
```

- [ ] **Step 2: Add the feature row**

After the F087 row in the shipped-features table:

```markdown
| F090.1/.3/.4 | Callback execution + finished-DAG visibility + Phase-2 gate signals (callback nodes were a no-op stub that copied their instruction text into `result` — 103 nodes across 83 DAGs, all `completed`, none having executed anything, a 100% success rate on a node type that never ran; they now execute as real subtasks behind `NOUS_DAG_CALLBACK_EXECUTION_ENABLED`, gaining tools and every F087 backstop via `subtask_id`, with the seven `node_type == "subtask"` gates collapsed into one `_SUBTASK_BACKED` constant so the set cannot drift. `dag_manage action=recent` makes finished DAGs discoverable — previously reachable only by a prefix you already knew. `phase2_signals` on `/dashboard/dag` reports `sibling_overlap_rate` and callback/gate execution counts, the go/no-go evidence for Phase 2. **Gate nodes still auto-pass** — F038 Phase 2 Critic integration remains unbuilt) | — |
```

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: F090 Phase 1 flag and feature rows"
```

---

## Self-Review

**Spec coverage.** F090.1 → Tasks 1-3. F090.3 → Task 4. F090.4 → Task 5. F090.2 → already delivered by F087, no task. Phase 2 (worklog/blackboard, F063) is explicitly out of scope — Task 5 exists to decide whether it is ever built.

**Not covered, deliberately:** gate nodes still auto-pass. F038's Phase 2 Critic integration is a separate unbuilt feature; this plan documents the fact at the tool surface (Task 3) and counts them (Task 5) rather than pretending to fix it.

**Type consistency.** `_SUBTASK_BACKED` (Task 1) is consumed by Task 2. `dag_callback_execution_enabled` (Task 2) is quoted in Tasks 3 and 6. `get_dag_phase2_signals` / `_shingle_overlap` (Task 5) match between test and implementation. `get_recent_dags(limit)` (Task 4) matches the existing signature at `store.py:226`.

**Known risk.** Task 1 will break existing callback tests in `test_dag_orchestrator.py`; that is called out in Task 1 Step 5 and repaired in Task 2 Step 6, with an explicit instruction not to weaken assertions. Two F087 rounds were spent on tests that passed against impossible mocks — a green run is not by itself evidence.
