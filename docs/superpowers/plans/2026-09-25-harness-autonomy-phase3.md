# Harness Autonomy Phase 3 — Park-and-Resume Implementation Plan (v1.4)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A DAG node of the new type `approval` waits durably on a person's answer given on a companion card, and resumes on that answer or — at its deadline — on its declared *stop* default, exactly once per attempt and across restarts.

**Architecture:** The node owns its answer: migration 076 adds the `approval` type, the `awaiting_input` status and the answer columns to `dag_nodes`. Every status write that can race an answer goes through one conditional store transition (`DAGStore.transition_node`), so a tap, the deadline, a cancel and the budget path race on one row and exactly one wins. The orchestrator parks the node, pushes an `approval_gate` card under the reserved dedup key `dag-approval:<node_id>`, and links it; the `approval.choose` handler finds its node from that key. The feature lands dark behind `NOUS_DAG_APPROVAL_NODES_ENABLED`, which gates creation only.

**Tech Stack:** Python 3.12+, SQLAlchemy 2 async (Core `UPDATE … WHERE status IN …`), PostgreSQL 17 (CI) / SQLite (local tests), pydantic v2, A2UI `SurfaceService` + `ActionRouter`, pytest (`asyncio_mode = "auto"`).

**Spec:** `docs/superpowers/specs/2026-09-25-harness-phase3-park-and-resume-design.md` (v2.4, `ff841a9`). Section references below (§3.4 …) are to the spec. Anchors from `main` `1daa004`; branch `feat/harness-phase3-park-and-resume`.

**Not yet reviewed by anyone:** the dispatch-time admission gate (spec §3.11, Task 11) was added in v2.2 after the database reviewer's last pass. Plan reviewers: look at it for starvation and for its interaction with F064.2 caps and `_recover_stale_ready_nodes`.

## Global Constraints

- Every node-status write that can race another writer is conditional (`DAGStore.transition_node` or the conditional `apply_retry`). Add no new blind `update_node(status=…)` on DAG nodes.
- Lock order: orchestrator `_lock` → an approval card's surface lock, never the reverse. `answer_node` takes no orchestrator lock. `SurfaceService.close` does database work only.
- The deadline is decided in SQL (`due_by`), never by comparing a Python `datetime` with a loaded DB timestamp. In-memory pre-filters normalize to aware UTC with `_as_utc`.
- `answer_source` is `companion` or `deadline` — never `human`. Omit `" by <actor>"` from texts when the actor is `unattributed`.
- v1: `default_option` must be a `stop` option.
- Dedup prefix `dag-approval:` is reserved: `SurfaceService.push_built` refuses it unless `reserved_key_ok=True`, which only the orchestrator passes.
- Migration `076`: `IF NOT EXISTS`, full-line `--` comments only, no `;` inside comments, no `BEGIN`/`COMMIT`.
- New settings (plain pydantic fields in `nous/config.py`): `dag_approval_nodes_enabled: bool = False`; `dag_approval_default_wait_seconds = 86400` (`ge=900`); `dag_approval_max_wait_seconds = 604800` (`ge=900`); `dag_approval_card_grace_seconds = 3600` (`ge=60`); `dag_max_parked_dags = 20` (`ge=1`).
- Tests: `UV_PROJECT_ENVIRONMENT=E:/Projects/nous/.venv uv run --frozen pytest <path> -q`. Local runs use SQLite; `SurfaceService`/`ActionRouter` database tests carry `@pytest.mark.postgres_only` and run in CI (`NOUS_TEST_DB=postgres`). The local baseline has ~230 pre-existing failures (`test_config` reads the developer `.env`, `test_database`, `test_tools`) — judge by diff against `main`; CI is the gate.
- Lint: no NEW ruff findings in touched files (several touched files carry pre-existing findings CI tolerates).
- Commits: explicit paths only — never a directory, `.`, `-A` or `commit -a` (public repo). Message from a file, ending with the session's attribution lines. Put `set -o pipefail` before any `pytest … | … && git commit` chain, and never end such a chain with a `grep` that may match nothing.

## Why (verified on `1daa004`; full table in spec §2)

| Defect or gap | Anchor |
|---|---|
| Failure propagation and retry's unblock ignore `context_flow`, readiness does not — a failed node's context_flow-only successor stays `pending` forever | `nous/dag/orchestrator.py:1896-1900,616-625,2377-2380` |
| `cancel_dag`, the cascade cancel, the dispatcher's `ready` write and `apply_retry` write status blind | `orchestrator.py:531-536,1936-1948,1994,2007,2047,2074`; `nous/dag/store.py:apply_retry` |
| The downstream unblock keeps `started_at`; `_recover_stale_ready_nodes` only takes `ready` nodes with `started_at IS NULL` | `orchestrator.py:672`, `_recover_stale_ready_nodes` |
| No DAG node can wait on a person; `approval.choose` records the choice nowhere readable | `orchestrator.py:2403-2420`; `nous/a2ui/actions.py:478-500` |

## File map

| File | Responsibility | Tasks |
|---|---|---|
| `nous/dag/schemas.py` | `PREDECESSOR_EDGE_TYPES`; `approval` type, `awaiting_input` status; `ApprovalOption`; node + DAG validation | 1, 3, 4 |
| `nous/dag/store.py` | `transition_node`, conditional `apply_retry`, `get_node_with_dag_status`, parked predicate + admission, approval-node insert | 2, 3, 4, 5, 9 |
| `sql/migrations/076_dag_approval_nodes.sql` | type/status CHECKs + answer columns | 3 |
| `nous/storage/models.py` | ORM mirror of 076 | 3 |
| `nous/config.py` | five new settings + wait validator | 4 |
| `nous/dag/approval.py` (new) | Leaf module, no a2ui imports: dedup-key mapping, card text, answer texts, `AnswerResult`, refusal messages, `stopped_at_approval`, delivery lines | 6 |
| `nous/a2ui/builders/approval.py` | `recommend_first`, `outcome` in options data, `defer_label` | 7 |
| `nous/a2ui/service.py` | reserved prefix, `notify_text`, `close`, `close_by_dedup_key`, `live_ids`, `live_cards_by_prefix`, no `no_objection` for DAG cards | 7 |
| `nous/dag/orchestrator.py` | conditional writes, launch (park/push/link), `answer_node`, deadline poll, cancels, budget, leaked-card sweep, dispatch gate, retry refusal, completion text | 1, 2, 8–12, 14 |
| `nous/a2ui/actions.py` | `ActionContext.actor`; DAG routing for `approval.choose`/`approval.defer`; companion retry passes `allow_declined=True` | 12, 13 |
| `nous/dag/delivery.py` | `Approvals:` section, `stopped at an approval` verb | 14 |
| `nous/api/tools.py` | `dag_create` schema/threading/refusals; `dag_manage` output | 15 |
| `nous/main.py`, `CLAUDE.md` | wiring move; settings rows | 16 |
| Tests | New: `tests/test_dag_approval_prereqs.py`, `tests/test_dag_approval_store.py`, `tests/test_dag_approval_schemas.py`, `tests/test_dag_approval_text.py`, `tests/test_dag_approval.py`, `tests/test_a2ui_dag_approval_actions.py`, `tests/test_dag_approval_tools.py`, `tests/test_dag_approval_e2e.py`. Additions: `tests/test_migrator_split.py`, `tests/test_a2ui_builders.py`, `tests/test_a2ui_service.py`, `tests/test_dag_delivery.py` | all |

---

## Task group A — pre-existing fixes the feature depends on

### Task 1: One predecessor-edge set; the unblock clears `started_at`

**Files:**
- Modify: `nous/dag/schemas.py` (near `EdgeType`; `compute_waves`)
- Modify: `nous/dag/orchestrator.py` (`_find_ready_nodes`, `_propagate_failures`, `retry_node`)
- Create: `tests/test_dag_approval_prereqs.py`

**Interfaces:**
- Produces: `nous.dag.schemas.PREDECESSOR_EDGE_TYPES: frozenset[str]` = `{"dependency", "context_flow"}` — used by Tasks 4, 5, 8.

- [ ] **Step 1: Write the failing tests** — create `tests/test_dag_approval_prereqs.py`:

```python
"""Harness Phase 3 prerequisites: predecessor edges and conditional writes.

Pre-existing defects the approval node depends on (spec §3.3, §3.8):
failure propagation and retry's unblock followed `dependency` edges only
while readiness also waits on `context_flow`, and several status writes
were blind.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from nous.config import Settings
from nous.dag.orchestrator import DAGOrchestrator
from nous.dag.schemas import DAGCreateRequest, DAGEdgeSpec, DAGNodeSpec, DAGNodeType
from nous.dag.store import DAGStore


def _settings(**overrides) -> Settings:
    """Hermetic settings — never inherit the developer's .env."""
    base = dict(_env_file=None, dag_node_default_timeout=120, dag_node_max_timeout=3600)
    base.update(overrides)
    return Settings(**base)


@pytest_asyncio.fixture
async def store(db):
    return DAGStore(db, f"test-p3pre-{uuid.uuid4().hex[:8]}", _settings())


@pytest.fixture
def subtask_mgr():
    mgr = AsyncMock()
    mgr.create.return_value = SimpleNamespace(id=uuid.uuid4(), status="pending")
    mgr.get.return_value = None
    return mgr


def _orch(store, subtask_mgr) -> DAGOrchestrator:
    orch = DAGOrchestrator(
        store=store, subtask_mgr=subtask_mgr, dynamic_loader=AsyncMock(), settings=_settings()
    )
    orch.clock_wired = True
    return orch


def _two_node(edge_type: str) -> DAGCreateRequest:
    return DAGCreateRequest(
        name=f"p3-{edge_type}",
        nodes=[
            DAGNodeSpec(name="draft", type=DAGNodeType.subtask, instructions="draft it"),
            DAGNodeSpec(name="send", type=DAGNodeType.subtask, instructions="send it"),
        ],
        edges=[DAGEdgeSpec(from_node="draft", to_node="send", edge_type=edge_type)],
    )


async def _node(store, dag_id, name):
    dag = await store.get_dag(dag_id)
    return next(n for n in dag.nodes if n.name == name)


async def test_failed_node_blocks_its_context_flow_only_successor(store, subtask_mgr):
    orch = _orch(store, subtask_mgr)
    dag = await store.create(_two_node("context_flow"))
    await store.update_dag_status(dag.id, "running")
    draft = await _node(store, dag.id, "draft")
    await store.update_node(draft.id, status="failed", error="boom")

    await orch._advance_dag(await store.get_dag(dag.id))

    assert (await _node(store, dag.id, "send")).status == "blocked"
    assert (await store.get_dag(dag.id)).status == "failed"


async def test_retry_unblocks_a_context_flow_only_successor_and_clears_started_at(
    store, subtask_mgr
):
    orch = _orch(store, subtask_mgr)
    dag = await store.create(_two_node("context_flow"))
    await store.update_dag_status(dag.id, "running")
    draft = await _node(store, dag.id, "draft")
    await store.update_node(draft.id, status="failed", error="boom")
    await orch._advance_dag(await store.get_dag(dag.id))
    send = await _node(store, dag.id, "send")
    # A node that ran in an earlier attempt carries started_at; the stale-ready
    # sweep ignores a node that still has it (spec §3.9, §5).
    await store.update_node(send.id, started_at=datetime.now(UTC))

    await orch.retry_node(dag.id, "draft")

    send = await _node(store, dag.id, "send")
    assert send.status == "pending"
    assert send.started_at is None
    assert send.completed_at is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `UV_PROJECT_ENVIRONMENT=E:/Projects/nous/.venv uv run --frozen pytest tests/test_dag_approval_prereqs.py -q`
Expected: FAIL — `send` stays `pending` (not `blocked`); the second test fails on `send.status == "pending"` before retry even matters.

- [ ] **Step 3: Implement**

In `nous/dag/schemas.py`, directly under `EdgeType = Literal[...]`:

```python
# Harness Phase 3 §3.8: the ONE predecessor-edge set. Readiness, wave
# computation, failure propagation and retry's unblock all read it — the last
# two used to follow `dependency` alone, so a failed node's context_flow-only
# successor stayed pending forever and wedged its DAG `running`.
PREDECESSOR_EDGE_TYPES: frozenset[str] = frozenset({"dependency", "context_flow"})
```

In `compute_waves`, replace `if edge.edge_type in ("dependency", "context_flow"):` with `if edge.edge_type in PREDECESSOR_EDGE_TYPES:`.

In `nous/dag/orchestrator.py` add `from nous.dag.schemas import PREDECESSOR_EDGE_TYPES` next to the existing `from nous.dag.store import …` line, then:

- `_find_ready_nodes`: `if edge.edge_type in PREDECESSOR_EDGE_TYPES:`
- `_propagate_failures`: `if edge.edge_type in PREDECESSOR_EDGE_TYPES:` for `dep_map` (keep the `elif edge.edge_type == "cancel_cascade":` branch).
- `retry_node`, both loops (`dep_map` and `adj`): `if edge.edge_type in PREDECESSOR_EDGE_TYPES or edge.edge_type == "cancel_cascade":`
- `retry_node`, the downstream-unblock dict: add `"started_at": None, "completed_at": None` (the direct retry at `:598-599` already clears both).

- [ ] **Step 4: Run the tests to verify they pass**

Run: `UV_PROJECT_ENVIRONMENT=E:/Projects/nous/.venv uv run --frozen pytest tests/test_dag_approval_prereqs.py tests/test_dag_orchestrator.py tests/test_dag_schemas.py -q`
Expected: the two new tests PASS; the existing DAG suites show no new failures.

- [ ] **Step 5: Commit**

```bash
git add nous/dag/schemas.py nous/dag/orchestrator.py tests/test_dag_approval_prereqs.py
git commit -q -F <msgfile>   # "fix(dag): one predecessor-edge set for readiness, propagation and retry"
```

### Task 2: `transition_node` and the conditional status writes

**Files:**
- Modify: `nous/dag/store.py` (constants; new `transition_node`; `apply_retry`)
- Modify: `nous/dag/orchestrator.py` (module constants; `_mark_ready_and_launch`; `_cancel_one`; `cancel_dag`; `_propagate_failures`; `retry_node`; `_dispatch_ready_nodes` incl. the F064.2 demotion; `_defer_node`; the `running` writes in `_launch_subtask_node` and `_launch_check_node`)
- Test: `tests/test_dag_approval_prereqs.py`; update every caller of `apply_retry` found by `grep -rn "apply_retry" nous tests`

**Interfaces:**
- Produces (store): `LIVE_DAG_STATUSES = frozenset({"pending","running"})`; `TERMINAL_DAG_STATUSES = frozenset(_TERMINAL_DAG_STATUSES)`; `async def transition_node(self, node_id: UUID, *, from_statuses: Collection[str], dag_statuses: Collection[str] | None = None, **values: object) -> bool` (Task 3 adds `card`, `due_by`); `apply_retry(dag_id, primary: tuple[UUID, dict, Collection[str]], unblocks: list[tuple[UUID, dict, Collection[str]]], reactivate: bool) -> bool`.
- Produces (orchestrator): `_NON_TERMINAL: frozenset[str]` (derived from `DAGNodeStatus`), `_DISPATCHABLE = frozenset({"pending","ready"})`; `async def _mark_ready_and_launch(self, node, dag) -> None`; `async def _cancel_one(self, node, error: str) -> bool`.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_dag_approval_prereqs.py`:

```python
async def test_transition_node_applies_only_from_the_listed_statuses(store):
    dag = await store.create(_two_node("dependency"))
    draft = await _node(store, dag.id, "draft")  # wave 0 → 'ready'

    assert await store.transition_node(draft.id, from_statuses={"pending"}, status="running") is False
    assert await store.transition_node(draft.id, from_statuses={"ready"}, status="running") is True
    assert (await _node(store, dag.id, "draft")).status == "running"


async def test_transition_node_honours_dag_statuses(store):
    dag = await store.create(_two_node("dependency"))
    draft = await _node(store, dag.id, "draft")
    await store.update_dag_status(dag.id, "cancelled")

    assert (
        await store.transition_node(
            draft.id, from_statuses={"ready"}, dag_statuses={"pending", "running"}, status="running"
        )
        is False
    )


async def test_dispatch_does_not_resurrect_a_node_cancelled_after_the_load(store, subtask_mgr):
    orch = _orch(store, subtask_mgr)
    dag = await store.create(_two_node("dependency"))
    stale = await store.get_dag(dag.id)  # the tick's copy: draft is 'ready'
    draft = next(n for n in stale.nodes if n.name == "draft")
    await store.update_node(draft.id, status="cancelled", error="cancelled")  # cancel_dag lands

    await orch._dispatch_ready_nodes(stale, [draft])

    assert (await _node(store, dag.id, "draft")).status == "cancelled"
    subtask_mgr.create.assert_not_called()


async def test_cancel_dag_keeps_an_outcome_that_landed_after_its_load(
    store, subtask_mgr, monkeypatch
):
    orch = _orch(store, subtask_mgr)
    dag = await store.create(_two_node("dependency"))
    real_cancel = orch._cancel_node

    async def completes_first(node):
        # The node finishes between cancel_dag's load and its write.
        await store.update_node(node.id, status="completed", result="done")
        await real_cancel(node)

    monkeypatch.setattr(orch, "_cancel_node", completes_first)

    await orch.cancel_dag(dag.id)

    assert (await _node(store, dag.id, "draft")).status == "completed"


async def test_retry_refuses_when_the_node_changed_after_its_load(
    store, subtask_mgr, monkeypatch
):
    orch = _orch(store, subtask_mgr)
    dag = await store.create(_two_node("dependency"))
    draft = await _node(store, dag.id, "draft")
    await store.update_node(draft.id, status="failed", error="boom")
    await store.update_dag_status(dag.id, "failed")

    async def another_retry_lands(node, _dag):
        await store.update_node(node.id, status="pending", error=None)
        return True

    monkeypatch.setattr(orch, "_account_before_retry", another_retry_lands)

    with pytest.raises(ValueError, match="changed state"):
        await orch.retry_node(dag.id, "draft")
    assert (await store.get_dag(dag.id)).status == "failed"  # not reactivated


async def test_a_cascade_target_that_finished_first_does_not_block_its_dependents(
    store, subtask_mgr, monkeypatch
):
    orch = _orch(store, subtask_mgr)
    dag = await store.create(
        DAGCreateRequest(
            name="cascade",
            nodes=[
                DAGNodeSpec(name="src", type=DAGNodeType.subtask, instructions="s"),
                DAGNodeSpec(name="mid", type=DAGNodeType.subtask, instructions="m"),
                DAGNodeSpec(name="leaf", type=DAGNodeType.subtask, instructions="l"),
            ],
            edges=[
                DAGEdgeSpec(from_node="src", to_node="mid", edge_type="cancel_cascade"),
                DAGEdgeSpec(from_node="mid", to_node="leaf"),
            ],
        )
    )
    await store.update_dag_status(dag.id, "running")
    await store.update_node((await _node(store, dag.id, "src")).id, status="failed", error="boom")
    mid = await _node(store, dag.id, "mid")
    await store.update_node(mid.id, status="running", started_at=datetime.now(UTC))
    real_cancel = orch._cancel_node

    async def mid_finishes_first(node):
        if node.name == "mid":
            await store.update_node(node.id, status="completed", result="done")
        await real_cancel(node)

    monkeypatch.setattr(orch, "_cancel_node", mid_finishes_first)

    await orch._advance_dag(await store.get_dag(dag.id))

    assert (await _node(store, dag.id, "mid")).status == "completed"
    assert (await _node(store, dag.id, "leaf")).status == "pending"  # not blocked


async def test_a_deferral_does_not_resurrect_a_cancelled_node(store, subtask_mgr):
    orch = _orch(store, subtask_mgr)
    dag = await store.create(_two_node("dependency"))
    stale = await store.get_dag(dag.id)
    draft = next(n for n in stale.nodes if n.name == "draft")  # 'ready' in the tick's copy
    await store.update_node(draft.id, status="cancelled", error="cancelled")

    await orch._defer_node(draft, stale, "pool saturated")

    assert (await _node(store, dag.id, "draft")).status == "cancelled"


async def test_a_cancel_during_subtask_creation_is_not_overwritten(store, subtask_mgr):
    """The launch's own `running` write came after the await on create(): a
    cancel_dag in that window saw no subtask_id to cancel, and the blind
    write resurrected the node — the subtask then ran in a cancelled DAG."""
    orch = _orch(store, subtask_mgr)
    dag = await store.create(_two_node("dependency"))
    draft = await _node(store, dag.id, "draft")
    created = SimpleNamespace(id=uuid.uuid4(), status="pending")

    async def cancel_lands_during_create(**_):
        await store.update_node(draft.id, status="cancelled", error="cancelled")
        return created

    subtask_mgr.create.side_effect = cancel_lands_during_create

    await orch.start_dag(dag.id)

    assert (await _node(store, dag.id, "draft")).status == "cancelled"
    subtask_mgr.cancel.assert_awaited_once_with(created.id)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `UV_PROJECT_ENVIRONMENT=E:/Projects/nous/.venv uv run --frozen pytest tests/test_dag_approval_prereqs.py -q`
Expected: FAIL — `AttributeError: 'DAGStore' object has no attribute 'transition_node'`; the dispatch test launches the cancelled node.

- [ ] **Step 3: Implement the store side** — in `nous/dag/store.py` add `from collections.abc import Collection` to the imports, then under `_TERMINAL_DAG_STATUSES`:

```python
# Harness Phase 3 §3.3. LIVE: DAGs the tick advances. TERMINAL: the delivery
# sweep's domain, as a set for transition_node's dag_statuses.
LIVE_DAG_STATUSES: frozenset[str] = frozenset({"pending", "running"})
TERMINAL_DAG_STATUSES: frozenset[str] = frozenset(_TERMINAL_DAG_STATUSES)
```

Add to `DAGStore` (next to `update_node`):

```python
    async def transition_node(
        self,
        node_id: UUID,
        *,
        from_statuses: Collection[str],
        dag_statuses: Collection[str] | None = None,
        **values: object,
    ) -> bool:
        """Harness Phase 3 §3.3: one conditional node write.

        Applies ``values`` only while the node is still in one of
        ``from_statuses`` (and, when given, its DAG in one of ``dag_statuses``),
        agent-scoped like ``claim_and_add_node_tokens``. Returns whether it
        applied. Every write that can race another writer of the same row goes
        through here, so the row's own predicate — not a lock — decides the
        race, across processes. ``dag_statuses`` is a snapshot filter: the
        UPDATE takes no lock on the execution_dags row.
        """
        dag_scope = select(ExecutionDAG.id).where(ExecutionDAG.agent_id == self._agent_id)
        if dag_statuses is not None:
            dag_scope = dag_scope.where(ExecutionDAG.status.in_(sorted(dag_statuses)))
        stmt = (
            update(DAGNode)
            .where(DAGNode.id == node_id)
            .where(DAGNode.status.in_(sorted(from_statuses)))
            .where(DAGNode.dag_id.in_(dag_scope))
            .values(**values)
        )
        async with self._db.session() as session:
            result = await session.execute(stmt)
            await session.commit()
            return result.rowcount == 1
```

Change `apply_retry` to take the retried node's update separately from the unblocks — explicit, so a future reorder cannot turn a lost primary into a partial retry — with a status set per update, and to report whether the retry applied (docstring: add one paragraph — "Harness Phase 3 §3.3: each write is conditional on the status retry_node read; if the retried node's own write does not apply, the whole retry rolls back and False is returned"):

```python
    async def apply_retry(
        self,
        dag_id: UUID,
        primary: tuple[UUID, dict, Collection[str]],
        unblocks: list[tuple[UUID, dict, Collection[str]]],
        reactivate: bool,
    ) -> bool:
        async with self._db.session() as session:
            scoped = select(ExecutionDAG.id).where(ExecutionDAG.agent_id == self._agent_id)
            for is_primary, (node_id, values, from_statuses) in [(True, primary)] + [
                (False, u) for u in unblocks
            ]:
                result = await session.execute(
                    update(DAGNode)
                    .where(DAGNode.id == node_id)
                    .where(DAGNode.dag_id.in_(scoped))
                    .where(DAGNode.status.in_(sorted(from_statuses)))
                    .values(**values)
                )
                if is_primary and result.rowcount != 1:
                    await session.rollback()
                    return False
            if reactivate:
                ...  # unchanged ExecutionDAG update
            await session.commit()
            return True
```

- [ ] **Step 4: Implement the orchestrator side** — in `nous/dag/orchestrator.py`:

Imports: extend to `from nous.dag.store import _TERMINAL_DAG_STATUSES, LIVE_DAG_STATUSES, TERMINAL_DAG_STATUSES, DAGStore` and `from nous.dag.schemas import PREDECESSOR_EDGE_TYPES, DAGNodeStatus`.

Under `_RESOLVED`:

```python
# Harness Phase 3 §3.3: statuses a node can still be moved out of by a cancel
# or a block. Derived from the enum so awaiting_input (Task 3) is included.
_NON_TERMINAL = frozenset(s.value for s in DAGNodeStatus) - _TERMINAL
# Statuses the dispatcher may move to 'ready' (wave-0 nodes are created ready).
_DISPATCHABLE = frozenset({"pending", "ready"})
```

New methods on `DAGOrchestrator`:

```python
    async def _mark_ready_and_launch(self, node: DAGNode, dag: ExecutionDAG) -> None:
        """Mark a node ready and launch it, conditionally (spec §3.3).

        The ready write was blind at four sites: a cancel_dag landing between
        the tick's load and this write had its 'cancelled' overwritten and the
        node launched inside a DAG being cancelled.
        """
        if not await self._store.transition_node(
            node.id,
            from_statuses=_DISPATCHABLE,
            dag_statuses=LIVE_DAG_STATUSES,
            status="ready",
        ):
            logger.info(
                "Node %s in DAG %s changed state before launch — not launched",
                node.name, dag.id,
            )
            return
        node.status = "ready"
        try:
            await self._launch_node(node, dag)
        except Exception:
            logger.exception("Failed to launch node %s in DAG %s", node.name, dag.id)

    async def _cancel_one(self, node: DAGNode, error: str) -> bool:
        """Cancel one node's primitive, then its row — conditionally (§3.3).

        The row write loses to any writer that already moved the node to a
        terminal status (a completion, an answer), so a cancellation can no
        longer overwrite an outcome that landed after the caller's load.
        """
        await self._cancel_node(node)
        won = await self._store.transition_node(
            node.id, from_statuses=_NON_TERMINAL, status="cancelled", error=error
        )
        if won:
            node.status = "cancelled"
            node.error = error
        return won
```

Replace the four `update_node(node.id, status="ready")` + `node.status = "ready"` + `try: await self._launch_node(...)` blocks in `_dispatch_ready_nodes` with `await self._mark_ready_and_launch(node, dag)`. At the capped site keep the accumulator exactly as it is: call the helper, then `if node.status == "running": running_by_frame[frame] = running_by_frame.get(frame, 0) + 1` (a lost transition leaves the stale status, so nothing is counted).

`cancel_dag`: the loop becomes

```python
        for node in dag.nodes:
            if node.status not in _TERMINAL:
                await self._cancel_one(node, reason)
```

`_propagate_failures`: apply the cancels FIRST and build `poison` from the cancels that won, then compute and apply the blocks. A cascade target that completed between the tick's load and the write keeps its outcome, and its dependents must not be blocked for a cancellation that never happened. Replace everything from `# Transitively find nodes to block` to the end of the method with:

```python
        # Apply cancelled status (cancel_cascade targets) — conditional (§3.3).
        # Only a cancel that WON poisons its dependents: a target that finished
        # between the tick's load and this write keeps its outcome.
        cancelled: set[str] = set()
        for node_id in to_cancel:
            if await self._cancel_one(node_by_id[node_id], "Cancelled by predecessor failure"):
                cancelled.add(node_id)

        # Transitively find nodes to block (predecessor edges).
        poison = failed_ids | cancelled
        to_block: set[str] = set()
        changed = True
        while changed:
            changed = False
            for node_id, predecessors in dep_map.items():
                node = node_by_id[node_id]
                if node.status in _TERMINAL or node_id in to_block or node_id in to_cancel:
                    continue
                if predecessors & (poison | to_block):
                    to_block.add(node_id)
                    changed = True

        # Apply blocked status — conditional.
        for node_id in to_block:
            node = node_by_id[node_id]
            if await self._store.transition_node(
                node.id, from_statuses=_NON_TERMINAL, status="blocked", error="Predecessor failed"
            ):
                node.status = "blocked"
```

`_defer_node`: its demotion to `pending` becomes conditional — Task 8's fail-closed launch path calls it on a `ready` node that `cancel_dag` may have cancelled meanwhile. Replace `await self._store.update_node(node.id, status="pending")` / `node.status = "pending"` with:

```python
        if await self._store.transition_node(
            node.id, from_statuses={"ready"}, status="pending"
        ):
            node.status = "pending"
```

and its failure write after `_MAX_DEFERRALS` too (this plan rewrites the function, so it follows the global rule):

```python
            error = f"{reason} — still saturated after {count} deferrals"
            if await self._store.transition_node(
                node.id, from_statuses={"ready", "pending"}, status="failed", error=error
            ):
                node.status = "failed"
```

`retry_node`: split the list — `primary = (node.id, {…the existing reset dict…}, frozenset({"failed"}))` and `unblocks: list[tuple[UUID, dict, frozenset[str]]] = []`, each unblock appended as `(n.id, {…}, frozenset({"blocked", "cancelled"}))`. Replace the final `await self._store.apply_retry(...)` with:

```python
        applied = await self._store.apply_retry(
            dag_id, primary, unblocks, reactivate=dag.status in ("failed", "partial")
        )
        if not applied:
            raise ValueError(
                f"Node '{node_name}' changed state while the retry was being "
                "prepared (another retry or an answer landed first) — nothing "
                "was changed."
            )
```

Update every other `apply_retry` caller from `grep -rn "apply_retry" nous tests` to the `(dag_id, primary, unblocks, reactivate)` form.

Existing tests that assert on `store.update_node` for a write this task converts must assert on `store.transition_node` instead — convert the assertion, never weaken it. Known (database review): `tests/test_dag_orchestrator.py:2493` `test_queue_full_defers_node_to_pending` and `:2541` `test_check_pool_full_defers_node` read the deferral from `store.update_node.await_args_list`; assert `status="pending"` on `store.transition_node.await_args_list`. On a mocked store `transition_node` returns a truthy `MagicMock`, so the converted paths proceed as before. Sweep the DAG suites for any other `update_node` assertion on `status="ready" | "cancelled" | "blocked" | "running" | "pending"` and convert those too.

The launch's own `running` writes (spec §3.3). In `_launch_subtask_node`, replace the `await self._store.update_node(node.id, status="running", subtask_id=subtask.id, started_at=now, last_activity_at=now)` call (keep its F064.1 comment) with:

```python
            launched = await self._store.transition_node(
                node.id,
                from_statuses=_DISPATCHABLE,
                dag_statuses=LIVE_DAG_STATUSES,
                status="running",
                subtask_id=subtask.id,
                started_at=now,
                last_activity_at=now,
            )
            if not launched:
                # Harness Phase 3 §3.3: a cancel_dag landed during create() —
                # its snapshot had no subtask_id to cancel, so cancel it here or
                # it runs inside a cancelled DAG (and may send what the person
                # just cancelled).
                logger.warning(
                    "Node %s in DAG %s changed state while its subtask was being "
                    "created — cancelling subtask %s", node.name, dag.id, subtask.id,
                )
                try:
                    await self._subtask_mgr.cancel(subtask.id)
                except Exception:
                    logger.exception("Could not cancel orphaned subtask %s", subtask.id)
                return
```

In `_launch_check_node`, replace the `update_node(node.id, status="running", check_name=check_name, started_at=…)` call with:

```python
            launched = await self._store.transition_node(
                node.id,
                from_statuses=_DISPATCHABLE,
                dag_statuses=LIVE_DAG_STATUSES,
                status="running",
                check_name=check_name,
                started_at=datetime.now(UTC),
            )
            if not launched:
                # Same race as the subtask path. Record the check on the node so
                # the reconciliation sweep can retry the disable if this one fails.
                await self._store.update_node(node.id, check_name=check_name)
                try:
                    await self._dynamic_loader.manage_check(action="disable", name=check_name)
                except Exception:
                    logger.warning("Could not disable orphaned check %s — the sweep retries", check_name)
                return
```

The F064.2 cap demotion in `_dispatch_ready_nodes` (`if node.status == "ready": await self._store.update_node(node.id, status="pending") …`) becomes:

```python
                if node.status == "ready" and await self._store.transition_node(
                    node.id, from_statuses={"ready"}, status="pending"
                ):
                    node.status = "pending"
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `UV_PROJECT_ENVIRONMENT=E:/Projects/nous/.venv uv run --frozen pytest tests/test_dag_approval_prereqs.py tests/test_dag_orchestrator.py tests/test_dag_durability.py tests/test_dag_concurrency_caps.py tests/test_dag_callback_execution.py tests/test_dag_store.py -q`
Expected: all new tests PASS; no new failures in the existing DAG suites (compare with `main` if any fail).

- [ ] **Step 6: Commit**

```bash
git add nous/dag/store.py nous/dag/orchestrator.py tests/test_dag_approval_prereqs.py <any updated apply_retry test files>
git commit -q -F <msgfile>   # "fix(dag): conditional status writes — dispatch, defer, cancel, cascade, block, retry"
```

---

## Task group B — schema, validation, admission

### Task 3: Migration 076, ORM, enums, and the `card`/`due_by` predicates

**Files:**
- Create: `sql/migrations/076_dag_approval_nodes.sql`
- Modify: `nous/storage/models.py` (`DAGNode` constraints + columns)
- Modify: `nous/dag/schemas.py` (`DAGNodeType.approval`, `DAGNodeStatus.awaiting_input`)
- Modify: `nous/dag/store.py` (`transition_node` gains `card`, `due_by`)
- Modify: `tests/test_migrator_split.py`
- Create: `tests/test_dag_approval_store.py`

**Interfaces:**
- Consumes: `transition_node` (Task 2).
- Produces: `DAGNode.approval_spec: dict | None`, `answer_deadline`, `surface_id`, `answer`, `answered_by`, `answered_at`, `answer_source`, `answer_history: list | None`; `transition_node(..., card: str | None = None, due_by: datetime | None = None, **values)`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_migrator_split.py`:

```python
def test_split_full_migration_076():
    """Harness Phase 3: CI applies migrations with psql, prod with this
    splitter. Pin 076's statement count on the REAL file, so a stray inline
    comment (an apostrophe or a ';') that merges statements fails here
    instead of at prod boot."""
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "sql" / "migrations" / "076_dag_approval_nodes.sql"
    stmts = _split_sql_statements(path.read_text(encoding="utf-8"))
    assert len(stmts) == 8, stmts
    assert "ADD COLUMN IF NOT EXISTS answer_history JSONB" in stmts[-2]
    assert "idx_dag_nodes_awaiting_input" in stmts[-1]
    # Comments must stay apostrophe-free: an unbalanced quote in one would
    # open a string for the splitter and swallow the statements after it.
    comments = [
        line for line in path.read_text(encoding="utf-8").splitlines()
        if line.lstrip().startswith("--")
    ]
    assert not [line for line in comments if "'" in line or ";" in line], comments
```

Create `tests/test_dag_approval_store.py`:

```python
"""Harness Phase 3: approval columns and the card / deadline predicates."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy.exc import IntegrityError

from nous.config import Settings
from nous.dag.schemas import DAGCreateRequest, DAGNodeSpec, DAGNodeType
from nous.dag.store import DAGStore


def _settings(**overrides) -> Settings:
    base = dict(_env_file=None, dag_node_default_timeout=120, dag_node_max_timeout=3600)
    base.update(overrides)
    return Settings(**base)


@pytest_asyncio.fixture
async def store(db):
    return DAGStore(db, f"test-p3store-{uuid.uuid4().hex[:8]}", _settings())


async def _one_node(store):
    dag = await store.create(
        DAGCreateRequest(
            name="one", nodes=[DAGNodeSpec(name="n", type=DAGNodeType.subtask, instructions="x")]
        )
    )
    return dag, dag.nodes[0]


async def test_answer_columns_round_trip(store):
    dag, node = await _one_node(store)
    at = datetime.now(UTC)

    assert await store.transition_node(
        node.id, from_statuses={"ready"}, status="awaiting_input",
        answer_deadline=at + timedelta(hours=1), surface_id="card-1",
    )
    assert await store.transition_node(
        node.id, from_statuses={"awaiting_input"}, status="completed",
        answer="send", answered_by="unattributed", answered_at=at,
        answer_source="companion", answer_history=[{"answer": "hold"}],
    )
    got = (await store.get_dag(dag.id)).nodes[0]
    assert (got.status, got.answer, got.answer_source) == ("completed", "send", "companion")
    assert got.answer_history == [{"answer": "hold"}]


async def test_answer_source_is_checked(store):
    _, node = await _one_node(store)
    with pytest.raises(IntegrityError):
        await store.update_node(node.id, answer_source="human")


async def test_card_predicate(store):
    _, node = await _one_node(store)
    await store.transition_node(node.id, from_statuses={"ready"}, status="awaiting_input")

    # Unlinked: any card with the key may answer (the tap-before-link window).
    assert await store.transition_node(
        node.id, from_statuses={"awaiting_input"}, card="card-9", surface_id="card-1"
    )
    # Linked to card-1: a different card may not.
    assert not await store.transition_node(
        node.id, from_statuses={"awaiting_input"}, card="card-2", status="completed"
    )
    assert await store.transition_node(
        node.id, from_statuses={"awaiting_input"}, card="card-1", status="completed"
    )


async def test_due_by_predicate(store):
    _, node = await _one_node(store)
    now = datetime.now(UTC)
    await store.transition_node(
        node.id, from_statuses={"ready"}, status="awaiting_input",
        answer_deadline=now + timedelta(hours=1),
    )
    assert not await store.transition_node(
        node.id, from_statuses={"awaiting_input"}, due_by=now, status="failed"
    )
    await store.update_node(node.id, answer_deadline=now - timedelta(seconds=1))
    assert await store.transition_node(
        node.id, from_statuses={"awaiting_input"}, due_by=now, status="failed"
    )
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `UV_PROJECT_ENVIRONMENT=E:/Projects/nous/.venv uv run --frozen pytest tests/test_dag_approval_store.py tests/test_migrator_split.py -q`
Expected: FAIL — the migration file is missing; `status='awaiting_input'` violates the ORM CHECK; `transition_node()` rejects `card`.

- [ ] **Step 3: Write the migration** — `sql/migrations/076_dag_approval_nodes.sql`:

```sql
-- Harness Phase 3: approval nodes wait durably on an answer from a person.
-- New node type approval and new status awaiting_input, plus the columns
-- that hold the authored question, the deadline and the answer on the node.
-- Drop both possible constraint names first (the 048 pattern): 032 created
-- the constraints inline, so Postgres named them itself.
ALTER TABLE nous_system.dag_nodes
    DROP CONSTRAINT IF EXISTS dag_nodes_node_type_check;
ALTER TABLE nous_system.dag_nodes
    DROP CONSTRAINT IF EXISTS chk_dag_node_type;
ALTER TABLE nous_system.dag_nodes
    ADD CONSTRAINT chk_dag_node_type
    CHECK (node_type IN ('subtask', 'check', 'gate', 'callback', 'fix', 'approval'));

ALTER TABLE nous_system.dag_nodes
    DROP CONSTRAINT IF EXISTS dag_nodes_status_check;
ALTER TABLE nous_system.dag_nodes
    DROP CONSTRAINT IF EXISTS chk_dag_node_status;
ALTER TABLE nous_system.dag_nodes
    ADD CONSTRAINT chk_dag_node_status
    CHECK (status IN (
        'pending', 'ready', 'running', 'awaiting_check', 'awaiting_input',
        'completed', 'failed', 'blocked', 'cancelled', 'skipped'
    ));

-- answer_source says where an answer came from, not who gave it.
ALTER TABLE nous_system.dag_nodes
    ADD COLUMN IF NOT EXISTS approval_spec JSONB,
    ADD COLUMN IF NOT EXISTS answer_deadline TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS surface_id TEXT,
    ADD COLUMN IF NOT EXISTS answer TEXT,
    ADD COLUMN IF NOT EXISTS answered_by TEXT,
    ADD COLUMN IF NOT EXISTS answered_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS answer_source TEXT
        CONSTRAINT chk_dag_node_answer_source
        CHECK (answer_source IN ('companion', 'deadline')),
    ADD COLUMN IF NOT EXISTS answer_history JSONB;

-- The sweep reads waiting nodes of terminal DAGs through this index,
-- never the whole DAG history.
CREATE INDEX IF NOT EXISTS idx_dag_nodes_awaiting_input
    ON nous_system.dag_nodes (dag_id)
    WHERE status = 'awaiting_input';
```

- [ ] **Step 4: Mirror it in the ORM and the enums**

`nous/storage/models.py`, `DAGNode.__table_args__`: status CHECK gains `'awaiting_input'` (full list: `'pending', 'ready', 'running', 'awaiting_check', 'awaiting_input', 'completed', 'failed', 'blocked', 'cancelled', 'skipped'`); type CHECK gains `'approval'`; add

```python
        CheckConstraint(
            "answer_source IN ('companion', 'deadline')",
            name="chk_dag_node_answer_source",
        ),
        # Harness Phase 3 (076): the sweep's node-driven query (spec §3.7).
        Index(
            "idx_dag_nodes_awaiting_input",
            "dag_id",
            postgresql_where=text("status = 'awaiting_input'"),
            sqlite_where=text("status = 'awaiting_input'"),
        ),
```

(Place it before the trailing `{"schema": "nous_system"}` dict; `Index` and `text` are already imported for 075's index — check the import line.)

and, after `expected_modes`:

```python
    # Harness Phase 3 (migration 076): an approval node owns its question,
    # deadline and answer. approval_spec is authored and immutable; the rest
    # is written by the conditional transitions in nous/dag (spec §3.2).
    approval_spec: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    answer_deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    surface_id: Mapped[str | None] = mapped_column(Text)
    answer: Mapped[str | None] = mapped_column(Text)
    answered_by: Mapped[str | None] = mapped_column(Text)
    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    answer_source: Mapped[str | None] = mapped_column(Text)
    answer_history: Mapped[list | None] = mapped_column(JSONB, nullable=True)
```

`nous/dag/schemas.py`: add `approval = "approval"` to `DAGNodeType` (comment: "Harness Phase 3: waits durably on a person's answer on a companion card") and `awaiting_input = "awaiting_input"` to `DAGNodeStatus` after `awaiting_check`.

- [ ] **Step 5: Add the predicates** — `transition_node` gains two keyword parameters (add `or_` to the sqlalchemy import and `from datetime import …` already present):

```python
        card: str | None = None,
        due_by: datetime | None = None,
```

and, before `.values(**values)` is applied (build `stmt` first, then):

```python
        if card is not None:
            # A card answers only the attempt it belongs to: unlinked (the
            # tap-before-link window) or linked to this very card.
            stmt = stmt.where(or_(DAGNode.surface_id.is_(None), DAGNode.surface_id == card))
        if due_by is not None:
            # The deadline decides in SQL — SQLite returns stored timestamps
            # naive, so a Python comparison would raise TypeError.
            stmt = stmt.where(DAGNode.answer_deadline <= due_by)
```

Docstring: add one line each for `card` and `due_by`.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `UV_PROJECT_ENVIRONMENT=E:/Projects/nous/.venv uv run --frozen pytest tests/test_dag_approval_store.py tests/test_migrator_split.py tests/test_dag_approval_prereqs.py tests/test_dag_store.py -q`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add sql/migrations/076_dag_approval_nodes.sql nous/storage/models.py nous/dag/schemas.py nous/dag/store.py tests/test_migrator_split.py tests/test_dag_approval_store.py
git commit -q -F <msgfile>   # "feat(dag): migration 076 — approval type, awaiting_input status, answer columns"
```

### Task 4: Settings, node validation, and the approval-node insert

**Files:**
- Modify: `nous/config.py` (after `dag_callback_execution_enabled`; new validator after `_validate_dag_timeouts`)
- Modify: `nous/dag/schemas.py` (`ApprovalOption`, constants, `DAGNodeSpec` fields + validator, `DAGCreateRequest.validate_dag` additions)
- Modify: `nous/dag/store.py` (`create`: stall skip + `approval_spec`)
- Create: `tests/test_dag_approval_schemas.py`

**Interfaces:**
- Consumes: `PREDECESSOR_EDGE_TYPES` (Task 1); `DAGNodeType.approval` (Task 3).
- Produces: `ApprovalOption(id, label, outcome)`; `APPROVAL_QUESTION_MAX_CHARS = 2000`; `APPROVAL_MIN_WAIT_SECONDS = 900`; `DAGNodeSpec.options / default_option / recommended_option / answer_timeout_seconds`; stored `approval_spec = {"options": [{id,label,outcome}], "default_option", "recommended_option", "answer_timeout_seconds"}` (wait already clamped).

- [ ] **Step 1: Write the failing tests** — create `tests/test_dag_approval_schemas.py`:

```python
"""Harness Phase 3 §3.1: approval-node validation and insert."""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from pydantic import ValidationError

from nous.config import Settings
from nous.dag.schemas import DAGCreateRequest, DAGEdgeSpec, DAGNodeSpec, DAGNodeType
from nous.dag.store import DAGStore

_OPTIONS = [
    {"id": "send", "label": "Send it", "outcome": "proceed"},
    {"id": "hold", "label": "Don't send", "outcome": "stop"},
]


def _approval(name: str = "approve", **overrides) -> DAGNodeSpec:
    base = dict(
        name=name, type=DAGNodeType.approval, instructions="Send the drafted email?",
        options=_OPTIONS, default_option="hold",
    )
    base.update(overrides)
    return DAGNodeSpec(**base)


def _send(**overrides) -> DAGNodeSpec:
    base = dict(name="send", type=DAGNodeType.subtask, instructions="send it")
    base.update(overrides)
    return DAGNodeSpec(**base)


def _gated(approval: DAGNodeSpec | None = None, extra=(), edges=None) -> DAGCreateRequest:
    return DAGCreateRequest(
        name="gated",
        nodes=[approval or _approval(), _send(), *extra],
        edges=edges
        if edges is not None
        else [DAGEdgeSpec(from_node="approve", to_node="send", edge_type="context_flow")],
    )


def test_a_well_formed_approval_dag_validates():
    assert _gated().nodes[0].default_option == "hold"


@pytest.mark.parametrize(
    "overrides, match",
    [
        ({"default_option": "send"}, "must be a 'stop' option"),
        ({"default_option": None}, "default_option"),
        ({"default_option": "nope"}, "default_option"),
        ({"options": [_OPTIONS[1], {"id": "x", "label": "X", "outcome": "stop"}]}, "'proceed'"),
        ({"options": [_OPTIONS[1]]}, "2-4"),
        ({"options": _OPTIONS * 3}, "2-4"),
        ({"options": [_OPTIONS[0], dict(_OPTIONS[1], id="send")]}, "unique"),
        ({"recommended_option": "nope"}, "recommended_option"),
        ({"instructions": "   "}, "question"),
        ({"instructions": "x" * 2001}, "2000"),
        ({"answer_timeout_seconds": 899}, "900"),
        ({"timeout_seconds": 60}, "timeout_seconds"),
        ({"tools": ["bash"]}, "tools"),
        ({"model": "m"}, "model"),
    ],
)
def test_bad_approval_nodes_are_rejected(overrides, match):
    with pytest.raises(ValidationError, match=match):
        _approval(**overrides)


def test_bad_option_id_is_rejected():
    with pytest.raises(ValidationError):
        _approval(options=[{"id": "Send It", "label": "x", "outcome": "proceed"}, _OPTIONS[1]])


def test_approval_fields_are_rejected_on_other_types():
    with pytest.raises(ValidationError, match="only on approval nodes"):
        _send(default_option="hold")


def test_an_approval_that_gates_nothing_is_rejected():
    with pytest.raises(ValidationError, match="gates nothing"):
        _gated(edges=[])


def test_a_fix_node_cannot_attach_to_an_approval():
    fix = DAGNodeSpec(
        name="fix", type=DAGNodeType.fix, parent_node="approve", fix_actions=["retry_as_is"]
    )
    edges = [
        DAGEdgeSpec(from_node="approve", to_node="send", edge_type="context_flow"),
        DAGEdgeSpec(from_node="approve", to_node="fix", edge_type="on_failure"),
    ]
    with pytest.raises(ValidationError, match="cannot attach to approval"):
        _gated(extra=[fix], edges=edges)


@pytest.mark.parametrize("action, ok", [("retry_with_amended_prompt", False), ("retry_as_is", True)])
def test_a_fix_below_an_approval_may_not_amend_the_approved_step(action, ok):
    fix = DAGNodeSpec(name="fix", type=DAGNodeType.fix, parent_node="send", fix_actions=[action])
    edges = [
        DAGEdgeSpec(from_node="approve", to_node="send", edge_type="context_flow"),
        DAGEdgeSpec(from_node="send", to_node="fix", edge_type="on_failure"),
    ]
    if ok:
        _gated(extra=[fix], edges=edges)
    else:
        with pytest.raises(ValidationError, match="retry_with_amended_prompt"):
            _gated(extra=[fix], edges=edges)


def test_settings_reject_a_default_wait_above_the_max():
    with pytest.raises(ValidationError, match="dag_approval_default_wait_seconds"):
        Settings(
            _env_file=None, dag_approval_default_wait_seconds=10_000,
            dag_approval_max_wait_seconds=5_000,
        )


@pytest_asyncio.fixture
async def store(db):
    settings = Settings(
        _env_file=None, dag_node_default_timeout=120, dag_node_max_timeout=3600,
        dag_approval_default_wait_seconds=3600, dag_approval_max_wait_seconds=7200,
    )
    return DAGStore(db, f"test-p3schema-{uuid.uuid4().hex[:8]}", settings)


async def test_create_stores_the_spec_and_keeps_the_not_null_timeout(store):
    dag = await store.create(_gated(_approval(answer_timeout_seconds=90_000)))
    node = next(n for n in dag.nodes if n.name == "approve")

    assert node.node_type == "approval"
    assert node.timeout_seconds == 120  # NOT NULL column keeps its default
    assert node.stall_timeout_seconds is None
    assert node.approval_spec["default_option"] == "hold"
    assert node.approval_spec["answer_timeout_seconds"] == 7200  # clamped
    assert [o["outcome"] for o in node.approval_spec["options"]] == ["proceed", "stop"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `UV_PROJECT_ENVIRONMENT=E:/Projects/nous/.venv uv run --frozen pytest tests/test_dag_approval_schemas.py -q`
Expected: FAIL — `options` is not a `DAGNodeSpec` field (extra ignored), nothing rejects.

- [ ] **Step 3: Settings** — `nous/config.py`, after `dag_callback_execution_enabled`:

```python
    # Harness Phase 3: approval nodes (park-and-resume). Gates CREATION only —
    # a node already waiting still answers and defaults when this is off.
    dag_approval_nodes_enabled: bool = False
    dag_approval_default_wait_seconds: int = Field(
        86400, ge=900,
        description="Time an approval node waits for an answer when its spec sets none.",
    )
    dag_approval_max_wait_seconds: int = Field(
        604800, ge=900,
        description="Ceiling on an approval node's wait; clamped at insert.",
    )
    dag_approval_card_grace_seconds: int = Field(
        3600, ge=60,
        description=(
            "Backstop added to an approval card's expiry past the node's deadline. "
            ">= 60: a zero expiry is falsy and push_built would store expires_at NULL."
        ),
    )
    dag_max_parked_dags: int = Field(
        20, ge=1,
        description="Max DAGs waiting on answers before a DAG with an approval node is refused.",
    )
```

and after `_validate_dag_timeouts`:

```python
    @model_validator(mode="after")
    def _validate_dag_approval_waits(self) -> "Settings":
        if self.dag_approval_default_wait_seconds > self.dag_approval_max_wait_seconds:
            raise ValueError(
                f"dag_approval_default_wait_seconds ({self.dag_approval_default_wait_seconds}) "
                f"must be <= dag_approval_max_wait_seconds ({self.dag_approval_max_wait_seconds})"
            )
        return self
```

- [ ] **Step 4: Schemas** — `nous/dag/schemas.py`, above `DAGNodeSpec`:

```python
# Harness Phase 3 §3.1.
APPROVAL_QUESTION_MAX_CHARS = 2000
APPROVAL_MIN_WAIT_SECONDS = 900


class ApprovalOption(BaseModel):
    """One answer on an approval card."""

    id: str = Field(..., pattern=r"^[a-z0-9_-]{1,40}$")
    label: str = Field(..., min_length=1, max_length=80)
    outcome: Literal["proceed", "stop"]
```

`DAGNodeSpec`, after `expected_modes`:

```python
    # Harness Phase 3 — approval nodes (type='approval' only).
    options: list[ApprovalOption] | None = Field(
        None, description="2-4 answers, each 'proceed' or 'stop'; at least one of each."
    )
    default_option: str | None = Field(
        None, description="Option id applied when nobody answers by the deadline. Must STOP."
    )
    recommended_option: str | None = Field(
        None, description="Option id highlighted on the card. Default: none."
    )
    answer_timeout_seconds: int | None = Field(
        None, ge=APPROVAL_MIN_WAIT_SECONDS,
        description="Seconds allowed for an answer (default NOUS_DAG_APPROVAL_DEFAULT_WAIT_SECONDS).",
    )

    @model_validator(mode="after")
    def _validate_approval_fields(self) -> DAGNodeSpec:
        approval_only = {
            "options": self.options,
            "default_option": self.default_option,
            "recommended_option": self.recommended_option,
            "answer_timeout_seconds": self.answer_timeout_seconds,
        }
        if self.type != DAGNodeType.approval:
            given = sorted(k for k, v in approval_only.items() if v is not None)
            if given:
                raise ValueError(
                    f"Node '{self.name}': {given} are allowed only on approval nodes"
                )
            return self
        # dag_create passes every one of these as n.get(...), so None is
        # "not given"; a real value would be silently meaningless — reject it.
        runs_nothing = {
            "tools": self.tools, "frame_type": self.frame_type, "model": self.model,
            "timeout_seconds": self.timeout_seconds,
            "stall_timeout_seconds": self.stall_timeout_seconds,
            "completion_condition": self.completion_condition,
            "completion_check": self.completion_check,
            "completion_check_interval": self.completion_check_interval,
            "max_check_attempts": self.max_check_attempts,
            "parent_node": self.parent_node, "fix_actions": self.fix_actions,
        }
        given = sorted(k for k, v in runs_nothing.items() if v is not None)
        if given:
            raise ValueError(
                f"Approval node '{self.name}' does not take {given}: it runs nothing"
            )
        if not self.instructions.strip():
            raise ValueError(
                f"Approval node '{self.name}' needs the question in 'instructions'"
            )
        if len(self.instructions) > APPROVAL_QUESTION_MAX_CHARS:
            raise ValueError(
                f"Approval node '{self.name}': the question is capped at "
                f"{APPROVAL_QUESTION_MAX_CHARS} characters"
            )
        options = self.options or []
        if not 2 <= len(options) <= 4:
            raise ValueError(f"Approval node '{self.name}' needs 2-4 options")
        ids = [o.id for o in options]
        if len(set(ids)) != len(ids):
            raise ValueError(f"Approval node '{self.name}': option ids must be unique")
        if {o.outcome for o in options} != {"proceed", "stop"}:
            raise ValueError(
                f"Approval node '{self.name}' needs at least one 'proceed' and one 'stop' option"
            )
        by_id = {o.id: o for o in options}
        if self.default_option not in by_id:
            raise ValueError(
                f"Approval node '{self.name}': default_option must name one of {ids}"
            )
        if by_id[self.default_option].outcome != "stop":
            raise ValueError(
                f"Approval node '{self.name}': default_option must be a 'stop' option — "
                "an unanswered card must never approve the action it guards"
            )
        if self.recommended_option is not None and self.recommended_option not in by_id:
            raise ValueError(
                f"Approval node '{self.name}': recommended_option must name one of {ids}"
            )
        return self
```

`DAGCreateRequest.validate_dag`, directly after the "At most one fix child per parent" loop and before cycle detection:

```python
        # --- Harness Phase 3 §3.1: approval-node structure ---
        approval_names = {n.name for n in self.nodes if n.type == DAGNodeType.approval}
        if approval_names:
            gating = {e.from_node for e in self.edges if e.edge_type in PREDECESSOR_EDGE_TYPES}
            for name in sorted(approval_names - gating):
                raise ValueError(
                    f"Approval node '{name}' gates nothing: add a context_flow edge "
                    "from it to the node it guards"
                )
            downstream = self._downstream_of(approval_names)
            for fn in fix_nodes:
                if fn.parent_node in approval_names:
                    raise ValueError(
                        f"Fix node '{fn.name}' cannot attach to approval node "
                        f"'{fn.parent_node}': a declined answer is an answer, not a "
                        "failure to repair"
                    )
                if fn.parent_node in downstream and "retry_with_amended_prompt" in (
                    fn.fix_actions or []
                ):
                    raise ValueError(
                        f"Fix node '{fn.name}' may not use retry_with_amended_prompt: "
                        f"'{fn.parent_node}' runs under an approval, and amending its "
                        "instructions would run text nobody approved (use retry_as_is)"
                    )
```

and a helper method on `DAGCreateRequest`:

```python
    def _downstream_of(self, roots: set[str]) -> set[str]:
        """Every node reachable from ``roots`` along PREDECESSOR_EDGE_TYPES."""
        adj: dict[str, list[str]] = defaultdict(list)
        for e in self.edges:
            if e.edge_type in PREDECESSOR_EDGE_TYPES:
                adj[e.from_node].append(e.to_node)
        seen: set[str] = set()
        stack = list(roots)
        while stack:
            for child in adj[stack.pop()]:
                if child not in seen:
                    seen.add(child)
                    stack.append(child)
        return seen
```

- [ ] **Step 5: Store insert** — in `DAGStore.create`, import `DAGNodeType` from `nous.dag.schemas`. Wrap the existing stall-resolution block:

```python
                if spec.type == DAGNodeType.approval:
                    # Harness Phase 3 §3.1: an approval node never runs, so no
                    # stall timeout applies. timeout_seconds keeps its resolved
                    # default above — the column is NOT NULL, and nothing reads
                    # it for an approval node.
                    resolved_stall = None
                else:
                    ...  # existing stall resolution + validation, unchanged, one indent deeper
```

Before `node = DAGNode(...)`:

```python
                approval_spec = None
                if spec.type == DAGNodeType.approval:
                    approval_spec = {
                        "options": [o.model_dump() for o in spec.options or []],
                        "default_option": spec.default_option,
                        "recommended_option": spec.recommended_option,
                        "answer_timeout_seconds": min(
                            spec.answer_timeout_seconds
                            or self._settings.dag_approval_default_wait_seconds,
                            self._settings.dag_approval_max_wait_seconds,
                        ),
                    }
```

and pass `approval_spec=approval_spec` to `DAGNode(...)`.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `UV_PROJECT_ENVIRONMENT=E:/Projects/nous/.venv uv run --frozen pytest tests/test_dag_approval_schemas.py tests/test_dag_schemas.py tests/test_dag_store.py -q`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add nous/config.py nous/dag/schemas.py nous/dag/store.py tests/test_dag_approval_schemas.py
git commit -q -F <msgfile>   # "feat(dag): approval node spec, validation and settings"
```

### Task 5: Admission — the parked predicate and the parked cap

**Files:**
- Modify: `nous/dag/store.py` (`parked_clause()`; `create`; `count_active`; new `count_parked`)
- Test: `tests/test_dag_approval_store.py`

**Interfaces:**
- Consumes: `LIVE_DAG_STATUSES` (Task 2), `PREDECESSOR_EDGE_TYPES` (Task 1), `dag_max_parked_dags` (Task 4).
- Produces: `parked_clause() -> ColumnElement[bool]` over `ExecutionDAG`; `DAGStore.count_active()` = live DAGs that are NOT parked; `DAGStore.count_parked() -> int`.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_dag_approval_store.py`:

```python
from nous.dag.schemas import DAGEdgeSpec


def _approval_spec(name: str = "approve") -> DAGNodeSpec:
    return DAGNodeSpec(
        name=name, type=DAGNodeType.approval, instructions="Go?",
        options=[
            {"id": "go", "label": "Go", "outcome": "proceed"},
            {"id": "no", "label": "No", "outcome": "stop"},
        ],
        default_option="no",
    )


async def _parked_dag(store, *, extra=(), edges=()):
    dag = await store.create(
        DAGCreateRequest(
            name=f"parked-{uuid.uuid4().hex[:6]}",
            nodes=[
                _approval_spec(),
                DAGNodeSpec(name="send", type=DAGNodeType.subtask, instructions="s"),
                *extra,
            ],
            edges=[DAGEdgeSpec(from_node="approve", to_node="send", edge_type="context_flow"), *edges],
        )
    )
    await store.update_dag_status(dag.id, "running")
    by_name = {n.name: n for n in dag.nodes}
    await store.update_node(by_name["approve"].id, status="awaiting_input")
    return dag, by_name


async def test_a_dag_waiting_only_on_its_approval_is_parked(store):
    await _parked_dag(store)
    assert (await store.count_parked(), await store.count_active()) == (1, 0)


async def test_a_deferred_wave0_sibling_is_work(store):
    sibling = DAGNodeSpec(name="side", type=DAGNodeType.subtask, instructions="s")
    _, by_name = await _parked_dag(store, extra=[sibling])
    await store.update_node(by_name["side"].id, status="pending")  # deferred by a cap
    assert (await store.count_parked(), await store.count_active()) == (0, 1)


async def test_a_pending_node_behind_a_completed_predecessor_is_work(store):
    pre = DAGNodeSpec(name="pre", type=DAGNodeType.subtask, instructions="s")
    post = DAGNodeSpec(name="post", type=DAGNodeType.subtask, instructions="s")
    _, by_name = await _parked_dag(
        store, extra=[pre, post], edges=[DAGEdgeSpec(from_node="pre", to_node="post")]
    )
    await store.update_node(by_name["pre"].id, status="completed")
    await store.update_node(by_name["post"].id, status="pending")
    assert await store.count_active() == 1


async def test_a_pending_node_behind_the_waiting_approval_is_not_work(store):
    pre = DAGNodeSpec(name="pre", type=DAGNodeType.subtask, instructions="s")
    _, by_name = await _parked_dag(
        store, extra=[pre], edges=[DAGEdgeSpec(from_node="pre", to_node="send")]
    )
    await store.update_node(by_name["pre"].id, status="completed")
    assert await store.count_parked() == 1


async def test_a_running_sibling_is_work(store):
    sibling = DAGNodeSpec(name="side", type=DAGNodeType.subtask, instructions="s")
    _, by_name = await _parked_dag(store, extra=[sibling])
    await store.update_node(by_name["side"].id, status="running")
    assert await store.count_active() == 1


async def test_parked_dags_do_not_count_against_the_active_limit(store):
    for _ in range(5):
        await _parked_dag(store)
    # Five parked DAGs: an ordinary DAG is still admitted.
    await _one_node(store)


async def test_the_parked_cap_refuses_only_dags_with_an_approval(db):
    capped = DAGStore(
        db, f"test-p3cap-{uuid.uuid4().hex[:8]}",
        _settings(dag_max_parked_dags=1),
    )
    await _parked_dag(capped)
    with pytest.raises(ValueError, match="waiting on your answers"):
        await _parked_dag(capped)
    await _one_node(capped)  # no approval node: never refused by this cap
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `UV_PROJECT_ENVIRONMENT=E:/Projects/nous/.venv uv run --frozen pytest tests/test_dag_approval_store.py -q`
Expected: FAIL — `DAGStore` has no `count_parked`; `count_active` counts parked DAGs.

- [ ] **Step 3: Implement** — `nous/dag/store.py` imports: `from sqlalchemy import and_, exists` (add to the existing import), `from sqlalchemy.orm import aliased`, `from nous.dag.schemas import PREDECESSOR_EDGE_TYPES, DAGCreateRequest, DAGNodeType`. Module level:

```python
_WORK_NODE_STATUSES = ("ready", "running", "awaiting_check")
_RESOLVED_NODE_STATUSES = ("completed", "skipped")


def parked_clause():
    """Harness Phase 3 §3.11 — SQL predicate over ExecutionDAG: the DAG is PARKED.

    It has an awaiting_input node and no work: no node ready / running /
    awaiting_check, and no pending non-fix node whose predecessors (along
    PREDECESSOR_EDGE_TYPES) are all completed or skipped — such a node is a
    sibling about to launch, or one a frame cap or a full pool deferred
    (_defer_node returns it to pending), and it is work. The ONE definition:
    create() and count_active() both use it.
    """
    waiting = aliased(DAGNode)
    busy = aliased(DAGNode)
    pending = aliased(DAGNode)
    pred = aliased(DAGNode)
    # Correlation is explicit rather than left to auto-correlation: each outer
    # EXISTS belongs to the enclosing SELECT over ExecutionDAG, and the inner
    # one to the `pending` row of has_dispatchable.
    has_waiting = (
        exists()
        .where(waiting.dag_id == ExecutionDAG.id, waiting.status == "awaiting_input")
        .correlate(ExecutionDAG)
    )
    has_busy = (
        exists()
        .where(busy.dag_id == ExecutionDAG.id, busy.status.in_(_WORK_NODE_STATUSES))
        .correlate(ExecutionDAG)
    )
    unresolved_pred = (
        exists()
        .where(
            DAGEdge.to_node_id == pending.id,
            DAGEdge.edge_type.in_(sorted(PREDECESSOR_EDGE_TYPES)),
            pred.id == DAGEdge.from_node_id,
            pred.status.not_in(_RESOLVED_NODE_STATUSES),
        )
        .correlate(pending)
    )
    has_dispatchable = (
        exists()
        .where(
            pending.dag_id == ExecutionDAG.id,
            pending.status == "pending",
            pending.node_type != "fix",
            ~unresolved_pred,
        )
        .correlate(ExecutionDAG)
    )
    return and_(has_waiting, ~has_busy, ~has_dispatchable)
```

(The tests above are the check that each subquery correlates as intended on both SQLite and Postgres.)

In `create`, replace the active-count block:

```python
            live = (
                select(func.count())
                .select_from(ExecutionDAG)
                .where(ExecutionDAG.agent_id == self._agent_id)
                .where(ExecutionDAG.status.in_(sorted(LIVE_DAG_STATUSES)))
            )
            parked = parked_clause()
            # Harness Phase 3 §3.11: a parked DAG does no work, so it does not
            # count against MAX_ACTIVE_DAGS.
            active_count = await session.scalar(live.where(~parked))
            if active_count >= MAX_ACTIVE_DAGS:
                raise ValueError(
                    f"Active DAG limit reached ({MAX_ACTIVE_DAGS}). "
                    "Cancel or complete existing DAGs first."
                )
            # ...but parked DAGs are bounded on their own, and only a request
            # that could add one is refused, so a backlog of unanswered
            # questions never blocks ordinary work.
            if any(spec.type == DAGNodeType.approval for spec in request.nodes):
                parked_count = await session.scalar(live.where(parked))
                if parked_count >= self._settings.dag_max_parked_dags:
                    raise ValueError(
                        f"{parked_count} DAGs are waiting on your answers (limit "
                        f"NOUS_DAG_MAX_PARKED_DAGS={self._settings.dag_max_parked_dags}); "
                        "answer or cancel some first."
                    )
```

Replace `count_active` and add `count_parked`:

```python
    async def count_active(self) -> int:
        """Count live DAGs that are working — parked DAGs excluded (§3.11)."""
        return await self._count_live(parked=False)

    async def count_parked(self) -> int:
        """Count live DAGs waiting only on an approval answer (§3.11)."""
        return await self._count_live(parked=True)

    async def _count_live(self, *, parked: bool) -> int:
        clause = parked_clause()
        async with self._db.session() as session:
            count = await session.scalar(
                select(func.count())
                .select_from(ExecutionDAG)
                .where(ExecutionDAG.agent_id == self._agent_id)
                .where(ExecutionDAG.status.in_(sorted(LIVE_DAG_STATUSES)))
                .where(clause if parked else ~clause)
            )
            return count or 0
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `UV_PROJECT_ENVIRONMENT=E:/Projects/nous/.venv uv run --frozen pytest tests/test_dag_approval_store.py tests/test_dag_store.py tests/test_dag_tools.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add nous/dag/store.py tests/test_dag_approval_store.py
git commit -q -F <msgfile>   # "feat(dag): parked DAGs leave the active count; separate parked cap"
```

---

## Task group C — the approval node

### Task 6: `nous/dag/approval.py` — the pure pieces

**Files:**
- Create: `nous/dag/approval.py`
- Create: `tests/test_dag_approval_text.py`

**Interfaces:**
- Produces: `DEDUP_PREFIX`, `UNATTRIBUTED`, `DEADLINE_ACTOR`, `DEFER_LABEL`, `BLOCKED_BY_APPROVAL`; `approval_dedup_key(node_id) -> str`; `node_id_from_dedup_key(key) -> UUID | None`; `as_utc(dt)`; `fmt_time(dt) -> str`; `option_by_id(spec, id) -> dict | None`; `label_of(spec, id) -> str`; `build_card_summary(question, results, max_chars=4000) -> str`; `risk_line(deadline, default_label) -> str`; `button_label(label, outcome) -> str`; `notify_text(title, question, deadline, default_label) -> str`; `answer_values(spec, option_id, *, source, actor, at, deadline) -> dict`; `AnswerResult`; `refusal_message(result, option_id) -> str`; `is_answered_approval(node)`; `stopped_at_approval(nodes) -> bool`; `stopped_summary(nodes) -> str`; `approval_line(node) -> str`; `card_link(surface_id, base_url) -> str`; `declined_retry_refusal(node_name) -> str`; `history_entry(node) -> dict | None`.

- [ ] **Step 1: Write the failing tests** — `tests/test_dag_approval_text.py`:

```python
"""Harness Phase 3: the pure texts every surface renders from (spec §3.4-§3.12)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

from nous.dag import approval as ap

SPEC = {
    "options": [
        {"id": "send", "label": "Send it", "outcome": "proceed"},
        {"id": "hold", "label": "Don't send", "outcome": "stop"},
    ],
    "default_option": "hold",
    "recommended_option": None,
    "answer_timeout_seconds": 86400,
}
AT = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)


def _node(**kw):
    base = dict(
        name="approve", node_type="approval", status="awaiting_input", approval_spec=SPEC,
        answer=None, answer_source=None, answered_by=None, answered_at=None,
        answer_deadline=AT, result=None, error=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_dedup_key_round_trips_and_rejects_foreign_keys():
    nid = uuid.uuid4()
    assert ap.node_id_from_dedup_key(ap.approval_dedup_key(nid)) == nid
    assert ap.node_id_from_dedup_key("dag:" + str(nid)) is None
    assert ap.node_id_from_dedup_key("dag-approval:not-a-uuid") is None
    assert ap.node_id_from_dedup_key(None) is None


def test_summary_leads_with_the_whole_question_and_marks_each_cut():
    question = "Q" * 3000
    out = ap.build_card_summary(question, [("draft", "d" * 5000), ("notes", "short")])
    assert out.startswith(question)
    assert "[truncated, 5000 chars]" in out
    assert "From 'notes':\nshort" in out
    assert ap.build_card_summary("Send it?", []) == "Send it?"


def test_card_texts():
    assert ap.risk_line(AT, "Don't send") == (
        "If nobody answers by 2026-09-25 12:00 UTC, 'Don't send' applies."
    )
    assert ap.button_label("Send it", "proceed") == "Send it — continues"
    assert ap.button_label("Don't send", "stop") == "Don't send — stops here"
    ping = ap.notify_text("dag · approve", "Line one?\nline two", AT, "Don't send")
    assert ping.splitlines() == [
        "dag · approve", "Line one?", "No answer by 2026-09-25 12:00 UTC → 'Don't send'.",
    ]
    assert len(ap.notify_text("t", "x" * 500, AT, "d").splitlines()[1]) == 200


def test_answer_values_never_name_an_unattributed_actor():
    tap = ap.answer_values(SPEC, "send", source="companion", actor="unattributed", at=AT, deadline=AT)
    assert tap == {
        "status": "completed", "error": None,
        "result": "Answered in the companion: 'Send it' (send) at 2026-09-25 12:00 UTC",
    }
    named = ap.answer_values(SPEC, "hold", source="companion", actor="alice@example.com", at=AT, deadline=AT)
    assert named["status"] == "failed"
    assert named["error"].endswith("at 2026-09-25 12:00 UTC by alice@example.com")
    assert named["error"].startswith("declined in the companion: 'Don't send' (hold)")
    late = ap.answer_values(SPEC, "hold", source="deadline", actor=ap.DEADLINE_ACTOR, at=AT, deadline=AT)
    assert late == {
        "status": "failed",
        "error": "no answer by 2026-09-25 12:00 UTC; default 'Don't send' (hold) applied",
    }


def test_refusal_messages():
    closed = ap.AnswerResult(outcome="closed", option_label="Send it", answered_at=AT, answer_source="companion")
    assert ap.refusal_message(closed, "hold") == "already answered 'Send it' at 2026-09-25 12:00 UTC"
    defaulted = ap.AnswerResult(outcome="closed", option_label="Don't send", answered_at=AT, answer_source="deadline")
    assert "no answer by the deadline" in ap.refusal_message(defaulted, "send")
    cancelled = ap.AnswerResult(outcome="closed", node_status="cancelled")
    assert ap.refusal_message(cancelled, "send") == "this DAG step was cancelled"
    assert ap.refusal_message(ap.AnswerResult(outcome="not_open"), "x").endswith("on a new card")
    assert ap.refusal_message(ap.AnswerResult(outcome="dag_ended"), "x") == "this DAG has already ended"
    assert "out of date" in ap.refusal_message(ap.AnswerResult(outcome="stray_card"), "x")
    assert "'x'" in ap.refusal_message(ap.AnswerResult(outcome="invalid_option"), "x")
    assert ap.refusal_message(ap.AnswerResult(outcome="not_linked"), "x") == "this DAG step no longer exists"


def test_stopped_at_approval_and_its_summary():
    stop = _node(status="failed", answer="hold", answer_source="deadline")
    blocked = SimpleNamespace(name="send", node_type="subtask", status="blocked", answer_source=None)
    assert ap.stopped_at_approval([stop, blocked])
    assert ap.stopped_summary([stop, blocked]) == "Stopped at approval 'approve': 'Don't send'; 1 step not run"
    crashed = SimpleNamespace(name="x", node_type="subtask", status="failed", answer_source=None)
    assert not ap.stopped_at_approval([stop, crashed])
    assert not ap.stopped_at_approval([blocked])


def test_approval_lines():
    assert ap.approval_line(_node(status="completed", answer="send", answer_source="companion", answered_at=AT)) == (
        "approve: 'Send it' in the companion at 2026-09-25 12:00 UTC"
    )
    assert ap.approval_line(_node(status="failed", answer="hold", answer_source="deadline")) == (
        "approve: no answer by 2026-09-25 12:00 UTC; default 'Don't send' applied"
    )
    assert ap.approval_line(_node(status="cancelled")) == "approve: not answered (cancelled)"
    assert "waiting for an answer until" in ap.approval_line(_node())


def test_history_entry_and_retry_refusal():
    assert ap.history_entry(_node()) is None
    entry = ap.history_entry(_node(answer="hold", answer_source="deadline", answered_by="system:deadline", answered_at=AT))
    assert entry == {
        "answer": "hold", "label": "Don't send", "outcome": "stop", "answer_source": "deadline",
        "answered_by": "system:deadline", "answered_at": "2026-09-25T12:00:00+00:00",
    }
    assert "dag_monitor" in ap.declined_retry_refusal("approve")
    assert ap.card_link("card-1", "https://n.example/") == "https://n.example/companion#/s/card-1"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `UV_PROJECT_ENVIRONMENT=E:/Projects/nous/.venv uv run --frozen pytest tests/test_dag_approval_text.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'nous.dag.approval'`.

- [ ] **Step 3: Implement** — `nous/dag/approval.py`:

```python
"""Harness Phase 3 — approval nodes: the pure pieces (spec §3.4-§3.12).

A leaf module with no a2ui imports, so nous/a2ui can import the reserved
dedup prefix without a cycle. Card text, answer texts, refusal messages, the
stopped-at-approval predicate and the delivery lines live here so the
orchestrator, the action handler, the F087 template and dag_manage render the
same words from one definition.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

DEDUP_PREFIX = "dag-approval:"
UNATTRIBUTED = "unattributed"
DEADLINE_ACTOR = "system:deadline"
DEFER_LABEL = "Decide later"
BLOCKED_BY_APPROVAL = "Blocked: an approval was declined or not answered"
SUMMARY_MAX_CHARS = 4000
NOTIFY_QUESTION_CHARS = 200

AnswerOutcome = Literal[
    "recorded", "closed", "not_open", "dag_ended", "stray_card", "not_linked", "invalid_option"
]


def approval_dedup_key(node_id: UUID) -> str:
    return f"{DEDUP_PREFIX}{node_id}"


def node_id_from_dedup_key(key: str | None) -> UUID | None:
    if not key or not key.startswith(DEDUP_PREFIX):
        return None
    try:
        return UUID(key[len(DEDUP_PREFIX):])
    except ValueError:
        return None


def as_utc(value: datetime | None) -> datetime | None:
    """SQLite returns stored timestamps naive; they are UTC."""
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def fmt_time(value: datetime | None) -> str:
    moment = as_utc(value)
    return moment.strftime("%Y-%m-%d %H:%M UTC") if moment else "an unknown time"


def option_by_id(spec: dict[str, Any] | None, option_id: str | None) -> dict[str, Any] | None:
    for option in (spec or {}).get("options", []):
        if option.get("id") == option_id:
            return option
    return None


def label_of(spec: dict[str, Any] | None, option_id: str | None) -> str:
    option = option_by_id(spec, option_id)
    return option["label"] if option else (option_id or "?")


def build_card_summary(
    question: str, results: list[tuple[str, str]], max_chars: int = SUMMARY_MAX_CHARS
) -> str:
    """The question first and never cut; each predecessor result cut on its
    own with a visible marker, so a long draft cannot push the question off."""
    head = question.strip()
    if not results:
        return head
    per_result = max(max_chars - len(head), 0) // len(results)
    blocks: list[str] = []
    for name, text in results:
        label = f"From '{name}':\n"
        room = max(per_result - len(label), 0)
        if len(text) > room:
            marker = f"\n[truncated, {len(text)} chars]"
            text = text[: max(room - len(marker), 0)] + marker
        blocks.append(label + text)
    return head + "\n\n" + "\n\n".join(blocks)


def risk_line(deadline: datetime | None, default_label: str) -> str:
    return f"If nobody answers by {fmt_time(deadline)}, '{default_label}' applies."


def button_label(label: str, outcome: str) -> str:
    return f"{label} — continues" if outcome == "proceed" else f"{label} — stops here"


def notify_text(title: str, question: str, deadline: datetime | None, default_label: str) -> str:
    """The Telegram ping body before the link (the service appends it). The
    ping is the only notice, and in prod its link is not tappable."""
    first_line = (question.strip().splitlines() or [""])[0][:NOTIFY_QUESTION_CHARS]
    return f"{title}\n{first_line}\nNo answer by {fmt_time(deadline)} → '{default_label}'."


def _by(actor: str | None) -> str:
    return "" if not actor or actor == UNATTRIBUTED else f" by {actor}"


def answer_values(
    spec: dict[str, Any],
    option_id: str,
    *,
    source: str,
    actor: str | None,
    at: datetime,
    deadline: datetime | None,
) -> dict[str, Any]:
    """Status and text columns the conditional answer write sets (§3.5)."""
    option = option_by_id(spec, option_id) or {}
    label, outcome = option.get("label", option_id), option.get("outcome")
    if source == "deadline":
        text = f"no answer by {fmt_time(deadline)}; default '{label}' ({option_id}) applied"
        if outcome == "proceed":  # unreachable in v1: the validator requires a stop default
            return {"status": "completed", "error": None, "result": text[0].upper() + text[1:]}
        return {"status": "failed", "error": text}
    when = f"at {fmt_time(at)}{_by(actor)}"
    if outcome == "proceed":
        return {
            "status": "completed",
            "error": None,
            "result": f"Answered in the companion: '{label}' ({option_id}) {when}",
        }
    return {"status": "failed", "error": f"declined in the companion: '{label}' ({option_id}) {when}"}


@dataclass(frozen=True)
class AnswerResult:
    """What an answer attempt did (§3.5). For `closed`, the label/source/time
    describe the answer that is actually recorded, not the one attempted."""

    outcome: AnswerOutcome
    node_id: UUID | None = None
    dag_id: UUID | None = None
    option_label: str | None = None
    option_outcome: str | None = None
    node_status: str | None = None
    answer_source: str | None = None
    answered_by: str | None = None
    answered_at: datetime | None = None


def refusal_message(result: AnswerResult, option_id: str) -> str:
    """The text a refused tap shows on the card (the companion shows a
    message only when the action fails)."""
    if result.outcome == "closed":
        if result.node_status == "cancelled":
            return "this DAG step was cancelled"
        if result.answer_source == "deadline":
            return (
                f"no answer by the deadline — '{result.option_label}' was applied at "
                f"{fmt_time(result.answered_at)}"
            )
        return f"already answered '{result.option_label}' at {fmt_time(result.answered_at)}"
    if result.outcome == "not_open":
        return "this question will be asked again on a new card"
    if result.outcome == "dag_ended":
        return "this DAG has already ended"
    if result.outcome == "stray_card":
        return "this card is out of date — answer the current one"
    if result.outcome == "invalid_option":
        return f"option {option_id!r} was not offered by this card"
    return "this DAG step no longer exists"


def is_answered_approval(node: Any) -> bool:
    return getattr(node, "node_type", None) == "approval" and bool(
        getattr(node, "answer_source", None)
    )


def stopped_at_approval(nodes: list[Any]) -> bool:
    """Every failed node is an answered approval — the ONE predicate behind
    the completion summary, the template verb and the blocked text (§3.12)."""
    failed = [n for n in nodes if n.status == "failed"]
    return bool(failed) and all(is_answered_approval(n) for n in failed)


def stopped_summary(nodes: list[Any]) -> str:
    stops = [n for n in nodes if n.status == "failed" and is_answered_approval(n)]
    parts = ", ".join(f"'{n.name}': '{label_of(n.approval_spec, n.answer)}'" for n in stops)
    not_run = sum(1 for n in nodes if n.status == "blocked")
    return f"Stopped at approval {parts}; {not_run} step{'s' if not_run != 1 else ''} not run"


def approval_line(node: Any) -> str:
    spec = node.approval_spec or {}
    label = label_of(spec, node.answer)
    if node.answer_source == "companion":
        return f"{node.name}: '{label}' in the companion at {fmt_time(node.answered_at)}"
    if node.answer_source == "deadline":
        return f"{node.name}: no answer by {fmt_time(node.answer_deadline)}; default '{label}' applied"
    if node.status == "awaiting_input":
        default = label_of(spec, spec.get("default_option"))
        return (
            f"{node.name}: waiting for an answer until {fmt_time(node.answer_deadline)} "
            f"(default '{default}')"
        )
    if node.status == "cancelled":
        return f"{node.name}: not answered (cancelled)"
    return f"{node.name}: {node.status}"


def card_link(surface_id: str, base_url: str | None) -> str:
    """Same shape as SurfaceService._notify_telegram's link."""
    return f"{(base_url or '').rstrip('/')}/companion#/s/{surface_id}"


def declined_retry_refusal(node_name: str) -> str:
    return (
        f"'{node_name}' was declined in the companion; the agent cannot re-ask it. "
        "If the person wants to reconsider, push a dag_monitor card for this DAG "
        "(push_surface template='dag_monitor') — its Retry button re-asks the question."
    )


def history_entry(node: Any) -> dict[str, Any] | None:
    """The previous attempt's answer, archived by the park write (§3.4)."""
    if not node.answer_source:
        return None
    option = option_by_id(node.approval_spec, node.answer) or {}
    answered_at = as_utc(node.answered_at)
    return {
        "answer": node.answer,
        "label": option.get("label"),
        "outcome": option.get("outcome"),
        "answer_source": node.answer_source,
        "answered_by": node.answered_by,
        "answered_at": answered_at.isoformat() if answered_at else None,
    }
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `UV_PROJECT_ENVIRONMENT=E:/Projects/nous/.venv uv run --frozen pytest tests/test_dag_approval_text.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add nous/dag/approval.py tests/test_dag_approval_text.py
git commit -q -F <msgfile>   # "feat(dag): approval texts, dedup key and stopped-at-approval predicate"
```

### Task 7: A2UI — builder options, reserved prefix, ping text, close helpers, no false `no_objection`

**Files:**
- Modify: `nous/a2ui/builders/approval.py`
- Modify: `nous/a2ui/service.py` (`push_built`, `_notify_telegram`, new `close`, `close_by_dedup_key`, `live_ids`, `live_cards_by_prefix`; `expire_sweep`)
- Test: `tests/test_a2ui_builders.py`, `tests/test_a2ui_service.py`

**Interfaces:**
- Consumes: `nous.dag.approval.DEDUP_PREFIX` (Task 6).
- Produces: `approval_gate` params `recommend_first: bool = True`, `defer_label: str | None`, options may carry `outcome` (kept in the data model); `push_built(..., notify_text: str | None = None, reserved_key_ok: bool = False)`; `async close(surface_id: str, status: str = "expired") -> None`; `async close_by_dedup_key(dedup_key: str, status: str = "expired") -> list[str]`; `async live_ids(surface_ids: Iterable[str]) -> set[str]`; `async live_cards_by_prefix(prefix: str) -> list[tuple[str, str]]` (no limit — spec §3.7); `class ReservedDedupKeyError(ValueError)`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_a2ui_builders.py`:

```python
def test_approval_gate_dag_options():
    import json

    from nous.a2ui.builders import approval_gate

    built = approval_gate(
        {
            "title": "t",
            "options": [
                {"id": "send", "label": "Send it — continues", "outcome": "proceed"},
                {"id": "hold", "label": "Don't send — stops here", "outcome": "stop"},
            ],
            "recommend_first": False,
            "defer_label": "Decide later",
        }
    )
    assert built.data_model["recommendation"] == ""
    assert [o["outcome"] for o in built.data_model["options"]] == ["proceed", "stop"]
    rendered = json.dumps(built.components)
    assert "Decide later" in rendered
    assert "(recommended)" not in rendered


def test_approval_gate_defaults_are_unchanged():
    import json

    from nous.a2ui.builders import approval_gate

    built = approval_gate({"title": "t", "options": [{"id": "a", "label": "A"}, {"id": "b", "label": "B"}]})
    assert built.data_model["recommendation"] == "a"
    assert built.data_model["options"] == [{"id": "a", "label": "A"}, {"id": "b", "label": "B"}]
    assert "Ask me later" in json.dumps(built.components)
```

Append to `tests/test_a2ui_service.py`:

```python
_DAG_CARD = {
    "title": "dag · approve",
    "summary": "Send it?",
    "options": [
        {"id": "send", "label": "Send", "outcome": "proceed"},
        {"id": "hold", "label": "Hold", "outcome": "stop"},
    ],
    "recommend_first": False,
}


async def test_push_built_refuses_the_reserved_dag_approval_prefix(db, a2ui_settings) -> None:
    """Runs on every backend: the refusal fires before any database work."""
    from nous.a2ui.service import ReservedDedupKeyError

    svc = SurfaceService(db, a2ui_settings)
    with pytest.raises(ReservedDedupKeyError, match="reserved"):
        await svc.push_built(approval_gate(_DAG_CARD), dedup_key=f"dag-approval:{uuid.uuid4()}")
    assert not issubclass(ReservedDedupKeyError, PermissionError)  # never read as a censor refusal


def test_every_push_built_retry_forwards_the_new_flags() -> None:
    """push_built re-enters itself on the dedup race and on IntegrityError.
    A hop that drops reserved_key_ok refuses the orchestrator's own card
    (the F092.3 refuse_fallback_overwrite lesson); one that drops
    notify_text loses the ping body."""
    import ast
    from pathlib import Path

    tree = ast.parse(Path(service_module.__file__).read_text(encoding="utf-8"))
    calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "push_built"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "self"
    ]
    assert len(calls) == 2, "expected the dedup-race and IntegrityError retries"
    for call in calls:
        assert {"reserved_key_ok", "notify_text"} <= {k.arg for k in call.keywords}
    # The ping is sent from _push_transaction_inner, so notify_text must also
    # ride every hop down to it — a missed hop is a NameError after the push
    # commits, reached only by postgres_only tests otherwise.
    hops = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in ("_push_transaction", "_push_transaction_inner")
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "self"
    ]
    assert hops, "expected push_built to call its transaction helpers"
    for call in hops:
        assert {"reserved_key_ok", "notify_text"} <= {k.arg for k in call.keywords}


@pytest.mark.postgres_only
async def test_close_helpers_and_liveness_queries(service, db, a2ui_agent_id: str) -> None:
    key = f"dag-approval:{uuid.uuid4()}"
    sid = await service.push_built(approval_gate(_DAG_CARD), dedup_key=key, reserved_key_ok=True)
    other = await service.push_built(approval_gate(_DAG_CARD))

    assert await service.live_ids([sid, other, "missing"]) == {sid, other}
    assert await service.live_cards_by_prefix("dag-approval:") == [(sid, key)]

    assert await service.close_by_dedup_key(key) == [sid]
    await service.close(sid)          # already closed: a no-op
    await service.close("missing")   # never existed: a no-op
    assert await service.live_ids([sid]) == set()
    surface = next(s for s in await _surfaces(db, a2ui_agent_id) if s.surface_id == sid)
    assert surface.status == "expired"


@pytest.mark.postgres_only
async def test_expire_sweep_writes_no_objection_only_for_non_dag_cards(
    service, db, a2ui_agent_id: str
) -> None:
    dag_card = await service.push_built(
        approval_gate(_DAG_CARD), dedup_key=f"dag-approval:{uuid.uuid4()}", reserved_key_ok=True
    )
    agent_card = await service.push_built(approval_gate(_DAG_CARD))
    async with db.session() as session:
        await session.execute(
            text(
                "UPDATE nous_system.a2ui_surfaces SET expires_at = now() - interval '1 minute' "
                "WHERE agent_id = :agent"
            ),
            {"agent": a2ui_agent_id},
        )
        await session.commit()

    assert await service.expire_sweep() == 2
    evidence = {a.surface_id for a in await _actions(db, a2ui_agent_id) if a.action_name == "no_objection"}
    assert evidence == {agent_card}
    assert dag_card not in evidence


@pytest.mark.postgres_only
async def test_notify_text_becomes_the_ping_body(service, monkeypatch) -> None:
    import asyncio

    sent: list[tuple] = []

    async def record(title, surface_id, text=None):
        sent.append((title, surface_id, text))

    monkeypatch.setattr(service, "_notify_telegram", record)
    sid = await service.push_built(
        approval_gate(_DAG_CARD), dedup_key=f"dag-approval:{uuid.uuid4()}",
        reserved_key_ok=True, notify=True, notify_text="dag · approve\nSend it?",
    )
    # _schedule_bg keeps strong refs in _pending_tasks; drain them.
    await asyncio.gather(*list(service._pending_tasks))
    assert sent == [("dag · approve", sid, "dag · approve\nSend it?")]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `UV_PROJECT_ENVIRONMENT=E:/Projects/nous/.venv uv run --frozen pytest tests/test_a2ui_builders.py tests/test_a2ui_service.py -q`
Expected (SQLite): the builder tests and the reserved-prefix test FAIL; the `postgres_only` tests are skipped locally and run in CI.

- [ ] **Step 3: Builder** — `nous/a2ui/builders/approval.py`:

```python
    # Harness Phase 3: a DAG card recommends nothing unless its author said
    # so; agent-pushed cards keep the options[0] fallback.
    recommend_first = params.get("recommend_first", True)
    recommendation = params.get("recommendation") or (options[0]["id"] if recommend_first else "")
```

the data-model options:

```python
            "options": [
                {
                    "id": o["id"],
                    "label": o["label"],
                    # Harness Phase 3: server-side record of what each answer does.
                    **({"outcome": o["outcome"]} if "outcome" in o else {}),
                }
                for o in options
            ],
```

and `Text("defer_label", params.get("defer_label") or "Ask me later")`.

- [ ] **Step 4: Service** — `nous/a2ui/service.py`: `from nous.dag.approval import DEDUP_PREFIX as _DAG_APPROVAL_PREFIX` (and `Iterable` from `collections.abc`).

Module level:

```python
class ReservedDedupKeyError(ValueError):
    """A push used a dedup-key prefix reserved for another producer.

    A ValueError, never a PermissionError: the DAG orchestrator reads a
    PermissionError from push_built as a censor refusal and fails the node
    for good (Harness Phase 3 §3.14).
    """
```

`push_built` gains `notify_text: str | None = None` and `reserved_key_ok: bool = False` (keyword-only, before `_dedup_retry`). First statement of the body:

```python
        if dedup_key and dedup_key.startswith(_DAG_APPROVAL_PREFIX) and not reserved_key_ok:
            # Harness Phase 3 §3.14: a push under this prefix would replace a
            # DAG approval card's text in place — same id, taps still answer
            # the node. Only the orchestrator may use it.
            raise ReservedDedupKeyError(
                f"dedup_key prefix {_DAG_APPROVAL_PREFIX!r} is reserved for DAG approval cards"
            )
```

`push_built` re-enters itself at TWO sites, and both must forward the new flags (the F092.3 lesson recorded for `refuse_fallback_overwrite`): the `_DedupRaceRetry` retry inside `push_built` (`service.py:351-361`) and the `IntegrityError` retry inside `_push_transaction_inner` (`:577-593`). Add `notify_text=notify_text, reserved_key_ok=reserved_key_ok` to both calls; the two values travel `push_built` → `_push_transaction` (`:309`) → `_push_transaction_inner` (`:363`) exactly the way `refuse_fallback_overwrite` already does (add them to both signatures and both calls). The AST test above fails if either hop drops one. The notify line becomes `self._schedule_bg(self._notify_telegram(built.title, surface_id, text=notify_text))`.

`_notify_telegram(self, title: str, surface_id: str, text: str | None = None)`: the message becomes `f"[companion] {text or title}\n{link}"`.

New methods (after `resolve`):

```python
    async def close(self, surface_id: str, status: str = "expired") -> None:
        """Retire a card under its surface lock (``resolve`` takes none).

        Harness Phase 3 §3.7: a card no longer live, or already deleted by
        retention (``resolve`` raises KeyError), counts as closed. Database
        work only — the tick holds the orchestrator lock while calling this.

        The per-surface lock is NOT reentrant: a caller already holding this
        card's lock (an action handler) must never call close() — return
        ``resolve_surface=True`` instead. Otherwise the handler deadlocks, and
        the tick then deadlocks on the same card under ``_lock``.
        """
        async with self.surface_lock(surface_id):
            try:
                await self.resolve(surface_id, status=status)
            except KeyError:
                return

    async def close_by_dedup_key(self, dedup_key: str, status: str = "expired") -> list[str]:
        """Close every live card carrying ``dedup_key``; returns their ids."""
        async with self._db.session() as session:
            ids = (
                await session.execute(
                    select(A2uiSurface.surface_id).where(
                        A2uiSurface.agent_id == self._settings.agent_id,
                        A2uiSurface.dedup_key == dedup_key,
                        A2uiSurface.status == "live",
                    )
                )
            ).scalars().all()
        for surface_id in ids:
            await self.close(surface_id, status)
        return list(ids)

    async def live_ids(self, surface_ids: Iterable[str]) -> set[str]:
        wanted = [s for s in surface_ids if s]
        if not wanted:
            return set()
        async with self._db.session() as session:
            rows = await session.execute(
                select(A2uiSurface.surface_id).where(
                    A2uiSurface.agent_id == self._settings.agent_id,
                    A2uiSurface.surface_id.in_(wanted),
                    A2uiSurface.status == "live",
                )
            )
            return set(rows.scalars().all())

    async def live_cards_by_prefix(self, prefix: str) -> list[tuple[str, str]]:
        """Every live card whose dedup key starts with ``prefix``, oldest first.

        No limit (Harness Phase 3 §3.7): a bounded page fills with healthy
        cards and never reaches the leaked ones behind them. For the DAG
        prefix the set is bounded by the parked cap times approvals per DAG.
        """
        async with self._db.session() as session:
            rows = await session.execute(
                select(A2uiSurface.surface_id, A2uiSurface.dedup_key)
                .where(
                    A2uiSurface.agent_id == self._settings.agent_id,
                    A2uiSurface.status == "live",
                    A2uiSurface.dedup_key.like(f"{prefix}%"),
                )
                .order_by(A2uiSurface.created_at)
            )
            return [(sid, key) for sid, key in rows.all()]
```

`expire_sweep`: the claim returns the key too, and a DAG card writes no evidence row:

```python
                                .returning(A2uiSurface.surface_id, A2uiSurface.dedup_key)
                            )
                        )
                        .all()
                    )
                    if not claimed:
                        continue
                    # Harness Phase 3 §3.7: a DAG approval card's node is the
                    # record of what happened; after an outage longer than
                    # wait + grace this startup expiry would otherwise write
                    # "no objection" for a card that was answered.
                    if not (claimed[0][1] or "").startswith(_DAG_APPROVAL_PREFIX):
                        session.add(A2uiAction(...))  # the existing no_objection row, unchanged
```

(`.scalars().all()` becomes `.all()`: two columns come back.)

- [ ] **Step 5: Run the tests to verify they pass**

Run: `UV_PROJECT_ENVIRONMENT=E:/Projects/nous/.venv uv run --frozen pytest tests/test_a2ui_builders.py tests/test_a2ui_service.py tests/test_a2ui_actions.py -q`
Expected: PASS locally (postgres-only tests skipped; CI runs them).

Import smoke (the new `nous.a2ui.service → nous.dag.approval` edge must not form a cycle; `nous/dag/__init__.py` is a docstring today):

Run: `UV_PROJECT_ENVIRONMENT=E:/Projects/nous/.venv uv run --frozen python -c "import nous.a2ui.service, nous.a2ui.actions, nous.dag.orchestrator, nous.dag.delivery"`
Expected: exits 0.

- [ ] **Step 6: Commit**

```bash
git add nous/a2ui/builders/approval.py nous/a2ui/service.py tests/test_a2ui_builders.py tests/test_a2ui_service.py
git commit -q -F <msgfile>   # "feat(a2ui): reserved dag-approval prefix, ping text, close helpers"
```

### Task 8: Orchestrator launch — retire, park, push, link

**Files:**
- Modify: `nous/dag/orchestrator.py` (constructor, `approvals_wired`, `_launch_node`, new `_launch_approval_node`, `_push_and_link`, `_build_approval_card`, `_context_results`, `_fail_parked`, `_close_card`)
- Create: `tests/test_dag_approval.py`

**Interfaces:**
- Consumes: `transition_node` (Tasks 2–3); `nous.dag.approval.*` (Task 6); `SurfaceService.push_built/close/close_by_dedup_key` signatures (Task 7).
- Produces: `DAGOrchestrator(..., surface_service: Any | None = None)`; `approvals_wired: bool` property; `async _push_and_link(node, dag) -> None` (Task 9 re-pushes through it); `async _close_card(surface_id: str | None, *, node_id: UUID | None = None) -> None`.

- [ ] **Step 1: Write the failing tests** — create `tests/test_dag_approval.py`:

```python
"""Harness Phase 3: the approval node in the orchestrator (spec §3.4-§3.11).

A FakeSurfaceService stands in for SurfaceService (whose own DB tests are
postgres_only because a2ui_surfaces.allowed_actions does not round-trip on
SQLite). It models what the orchestrator relies on: dedup replaces a LIVE
card in place (same id) and pings only when it creates one.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from nous.config import Settings
from nous.dag.approval import approval_dedup_key
from nous.dag.orchestrator import DAGOrchestrator
from nous.dag.schemas import DAGCreateRequest, DAGEdgeSpec, DAGNodeSpec, DAGNodeType
from nous.dag.store import DAGStore


class FakeSurfaceService:
    def __init__(self) -> None:
        self.cards: dict[str, dict] = {}
        self.pings: list[str | None] = []
        self.push_errors: list[BaseException] = []
        self.close_errors: list[BaseException] = []
        self.on_push = None  # async callable(surface_id) run before push returns
        self._seq = 0

    async def push_built(self, built, *, dedup_key=None, notify=None, notify_text=None,
                         reserved_key_ok=False, **_):
        if self.push_errors:
            raise self.push_errors.pop(0)
        assert reserved_key_ok, "the orchestrator must pass reserved_key_ok"
        live = [s for s, c in self.cards.items() if c["dedup_key"] == dedup_key and c["status"] == "live"]
        if live:
            sid = live[0]
            self.cards[sid]["built"] = built
        else:
            self._seq += 1
            sid = f"card-{self._seq}"
            self.cards[sid] = {"dedup_key": dedup_key, "status": "live", "built": built}
            self.pings.append(notify_text)
        if self.on_push is not None:
            await self.on_push(sid)
        return sid

    async def close(self, surface_id, status="expired"):
        if self.close_errors:
            raise self.close_errors.pop(0)
        card = self.cards.get(surface_id)
        if card and card["status"] == "live":
            card["status"] = status

    async def close_by_dedup_key(self, dedup_key, status="expired"):
        if self.close_errors:
            raise self.close_errors.pop(0)
        hit = [s for s, c in self.cards.items() if c["dedup_key"] == dedup_key and c["status"] == "live"]
        for sid in hit:
            self.cards[sid]["status"] = status
        return hit

    async def live_ids(self, surface_ids):
        return {s for s in surface_ids if self.cards.get(s, {}).get("status") == "live"}

    async def live_cards_by_prefix(self, prefix):
        return [
            (s, c["dedup_key"]) for s, c in self.cards.items()
            if c["status"] == "live" and (c["dedup_key"] or "").startswith(prefix)
        ]

    def live(self) -> dict[str, dict]:
        return {s: c for s, c in self.cards.items() if c["status"] == "live"}


def _settings(**overrides) -> Settings:
    base = dict(_env_file=None, dag_node_default_timeout=120, dag_node_max_timeout=3600)
    base.update(overrides)
    return Settings(**base)


@pytest_asyncio.fixture
async def store(db):
    return DAGStore(db, f"test-p3orch-{uuid.uuid4().hex[:8]}", _settings())


@pytest.fixture
def subtask_mgr():
    mgr = AsyncMock()
    mgr.create.return_value = SimpleNamespace(id=uuid.uuid4(), status="pending")
    mgr.get.return_value = None
    return mgr


@pytest.fixture
def surfaces():
    return FakeSurfaceService()


def _orch(store, subtask_mgr, surfaces, **settings) -> DAGOrchestrator:
    orch = DAGOrchestrator(
        store=store, subtask_mgr=subtask_mgr, dynamic_loader=AsyncMock(),
        settings=_settings(**settings), surface_service=surfaces,
    )
    orch.clock_wired = True
    return orch


def _approve(**overrides) -> DAGNodeSpec:
    base = dict(
        name="approve", type=DAGNodeType.approval, instructions="Send the drafted email?",
        options=[
            {"id": "send", "label": "Send it", "outcome": "proceed"},
            {"id": "hold", "label": "Don't send", "outcome": "stop"},
        ],
        default_option="hold",
    )
    base.update(overrides)
    return DAGNodeSpec(**base)


def _request(with_draft: bool = False) -> DAGCreateRequest:
    nodes = [_approve(), DAGNodeSpec(name="send", type=DAGNodeType.subtask, instructions="send")]
    edges = [DAGEdgeSpec(from_node="approve", to_node="send", edge_type="context_flow")]
    if with_draft:
        nodes.insert(0, DAGNodeSpec(name="draft", type=DAGNodeType.subtask, instructions="draft"))
        edges.append(DAGEdgeSpec(from_node="draft", to_node="approve", edge_type="context_flow"))
    return DAGCreateRequest(name="mail", nodes=nodes, edges=edges)


async def _node(store, dag_id, name):
    dag = await store.get_dag(dag_id)
    return next(n for n in dag.nodes if n.name == name)


async def _parked(store, orch):
    dag = await store.create(_request())
    await orch.start_dag(dag.id)
    return dag, await _node(store, dag.id, "approve")


async def test_launch_parks_pushes_one_card_and_links_it(store, subtask_mgr, surfaces):
    orch = _orch(store, subtask_mgr, surfaces)
    dag, node = await _parked(store, orch)

    assert node.status == "awaiting_input"
    assert node.answer_deadline is not None
    assert node.surface_id in surfaces.live()
    card = surfaces.cards[node.surface_id]
    assert card["dedup_key"] == approval_dedup_key(node.id)
    data = card["built"].data_model
    assert data["summary"].startswith("Send the drafted email?")
    assert data["risk"].endswith("'Don't send' applies.")
    assert [o["label"] for o in data["options"]] == ["Send it — continues", "Don't send — stops here"]
    assert data["recommendation"] == ""
    assert len(surfaces.pings) == 1 and "No answer by" in surfaces.pings[0]


async def test_the_card_carries_the_draft(store, subtask_mgr, surfaces):
    orch = _orch(store, subtask_mgr, surfaces)
    dag = await store.create(_request(with_draft=True))
    await orch.start_dag(dag.id)
    draft = await _node(store, dag.id, "draft")
    await store.update_node(draft.id, status="completed", result="Dear Bob, ...")

    await orch._advance_dag(await store.get_dag(dag.id))

    node = await _node(store, dag.id, "approve")
    assert "From 'draft':\nDear Bob, ..." in surfaces.cards[node.surface_id]["built"].data_model["summary"]


async def test_step0_retires_the_previous_attempts_live_card(store, subtask_mgr, surfaces):
    orch = _orch(store, subtask_mgr, surfaces)
    dag = await store.create(_request())
    node = await _node(store, dag.id, "approve")
    surfaces.cards["old"] = {"dedup_key": approval_dedup_key(node.id), "status": "live", "built": None}

    await orch.start_dag(dag.id)

    node = await _node(store, dag.id, "approve")
    assert surfaces.cards["old"]["status"] == "expired"
    assert node.surface_id != "old" and node.surface_id in surfaces.live()


async def test_a_failing_step0_defers_instead_of_parking(store, subtask_mgr, surfaces):
    orch = _orch(store, subtask_mgr, surfaces)
    surfaces.close_errors = [RuntimeError("db down")]
    dag = await store.create(_request())

    await orch.start_dag(dag.id)

    node = await _node(store, dag.id, "approve")
    assert node.status == "pending"
    assert surfaces.cards == {}
    assert orch._defer_counts[node.id] == 1


async def test_a_transient_push_failure_leaves_the_node_parked(store, subtask_mgr, surfaces):
    orch = _orch(store, subtask_mgr, surfaces)
    surfaces.push_errors = [RuntimeError("flaky")]
    _, node = await _parked(store, orch)

    assert node.status == "awaiting_input"
    assert node.surface_id is None
    assert node.error.startswith("approval card not delivered yet")


async def test_a_censor_refusal_fails_the_node(store, subtask_mgr, surfaces):
    orch = _orch(store, subtask_mgr, surfaces)
    surfaces.push_errors = [PermissionError("surface blocked by censor: pii")]
    _, node = await _parked(store, orch)

    assert node.status == "failed"
    assert node.error.startswith("approval card refused by censor")


async def test_a_node_cancelled_before_the_link_gets_its_card_closed(store, subtask_mgr, surfaces):
    orch = _orch(store, subtask_mgr, surfaces)
    dag = await store.create(_request())
    node_id = (await _node(store, dag.id, "approve")).id

    async def cancel_lands(_sid):
        await store.update_node(node_id, status="cancelled", error="cancelled")

    surfaces.on_push = cancel_lands
    await orch.start_dag(dag.id)

    assert (await _node(store, dag.id, "approve")).status == "cancelled"
    assert surfaces.live() == {}


async def test_the_park_write_is_the_reset_point(store, subtask_mgr, surfaces):
    orch = _orch(store, subtask_mgr, surfaces)
    dag = await store.create(_request())
    await store.update_dag_status(dag.id, "running")
    node = await _node(store, dag.id, "approve")
    at = datetime.now(UTC) - timedelta(days=1)
    await store.update_node(
        node.id, status="pending", answer="hold", answer_source="deadline",
        answered_by="system:deadline", answered_at=at, error="no answer by …",
    )

    await orch._advance_dag(await store.get_dag(dag.id))

    node = await _node(store, dag.id, "approve")
    assert node.status == "awaiting_input"
    assert (node.answer, node.answer_source, node.answered_at, node.error) == (None, None, None, None)
    assert [h["answer"] for h in node.answer_history] == ["hold"]


def test_approvals_wired(store, subtask_mgr, surfaces):
    assert _orch(store, subtask_mgr, surfaces).approvals_wired
    assert not _orch(store, subtask_mgr, None).approvals_wired
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `UV_PROJECT_ENVIRONMENT=E:/Projects/nous/.venv uv run --frozen pytest tests/test_dag_approval.py -q`
Expected: FAIL — `DAGOrchestrator.__init__() got an unexpected keyword argument 'surface_service'`.

- [ ] **Step 3: Implement** — `nous/dag/orchestrator.py`:

Imports (the typing line becomes `from typing import TYPE_CHECKING, Any, Literal` — `Any` is not imported today and ruff would flag F821):

```python
from datetime import timedelta  # add to the existing datetime import
from nous.dag.approval import (
    DEFER_LABEL,
    approval_dedup_key,
    as_utc,
    build_card_summary,
    button_label,
    history_entry,
    label_of,
    notify_text,
    risk_line,
)
```

Constructor: add keyword `surface_service: Any | None = None` (after `delivery`) and in the body:

```python
        # Harness Phase 3: pushes and closes approval cards. Passed at
        # construction (main.py builds SurfaceService first) — None means the
        # companion is off and dag_create refuses approval nodes.
        self._surface_service = surface_service
```

and the property:

```python
    @property
    def approvals_wired(self) -> bool:
        """Harness Phase 3: approval cards can be pushed (the clock_wired pattern)."""
        return self._surface_service is not None
```

`_launch_node`, after the `gate` branch:

```python
        elif node_type == "approval":
            await self._launch_approval_node(node, dag)
```

New methods:

```python
    async def _launch_approval_node(self, node: DAGNode, dag: ExecutionDAG) -> None:
        """Harness Phase 3 §3.4: retire the previous attempt's card, park, push, link.

        Steps 0-1 fail closed: any exception before a successful park sends
        the node back to 'pending' via _defer_node. _dispatch_ready_nodes only
        logs a launch exception, and a node left 'ready' with a kept
        started_at is invisible to _recover_stale_ready_nodes — it would hold
        a working slot forever.
        """
        spec = node.approval_spec or {}
        wait = int(
            spec.get("answer_timeout_seconds") or self._settings.dag_approval_default_wait_seconds
        )
        try:
            # Step 0: a card left live by an earlier attempt carries the same
            # key and a valid nonce; between this park and the push it could
            # answer the new attempt with the old content (I2).
            if self._surface_service is not None:
                await self._surface_service.close_by_dedup_key(
                    approval_dedup_key(node.id), "expired"
                )
            now = datetime.now(UTC)
            history = list(node.answer_history or [])
            previous = history_entry(node)
            if previous is not None:
                history.append(previous)
            # Step 1: the park write is the single reset point for an attempt.
            parked = await self._store.transition_node(
                node.id,
                from_statuses=_DISPATCHABLE,
                dag_statuses=LIVE_DAG_STATUSES,
                status="awaiting_input",
                started_at=now,
                answer_deadline=now + timedelta(seconds=wait),
                surface_id=None,
                answer=None,
                answered_by=None,
                answered_at=None,
                answer_source=None,
                result=None,
                error=None,
                completed_at=None,
                answer_history=history or None,
            )
        except Exception as exc:
            logger.exception("Could not park approval node %s in DAG %s", node.name, dag.id)
            await self._defer_node(node, dag, f"approval could not be prepared: {exc}")
            return
        if not parked:
            return  # someone else moved the node
        self._defer_counts.pop(node.id, None)
        node.status = "awaiting_input"
        node.started_at = now
        node.answer_deadline = now + timedelta(seconds=wait)
        node.surface_id = None
        node.answer = node.answered_by = node.answered_at = node.answer_source = None
        node.result = node.error = node.completed_at = None
        node.answer_history = history or None
        await self._push_and_link(node, dag)

    async def _push_and_link(self, node: DAGNode, dag: ExecutionDAG) -> None:
        """Steps 2-3 (§3.4). Also the same-attempt re-push (§3.6)."""
        if self._surface_service is None:
            await self._store.transition_node(
                node.id, from_statuses={"awaiting_input"},
                error="approval card not delivered yet: the companion is not wired",
            )
            return
        try:
            built, ping = self._build_approval_card(node, dag)
        except Exception as exc:  # deterministic: it will not heal on retry
            await self._fail_parked(node, f"approval card could not be built: {exc}")
            return
        try:
            surface_id = await self._surface_service.push_built(
                built,
                dedup_key=approval_dedup_key(node.id),
                notify=True,
                notify_text=ping,
                reserved_key_ok=True,
            )
        except PermissionError as exc:
            await self._fail_parked(node, f"approval card refused by censor: {exc}")
            return
        except Exception as exc:
            # Transient: stay parked; _poll_awaiting_input pushes again each
            # tick until the deadline, which bounds the retries.
            logger.warning("Approval card push failed for node %s: %s", node.name, exc)
            reason = f"approval card not delivered yet: {exc}"
            if await self._store.transition_node(
                node.id, from_statuses={"awaiting_input"}, error=reason
            ):
                node.error = reason
            return
        if await self._store.transition_node(
            node.id, from_statuses={"awaiting_input"}, surface_id=surface_id, error=None
        ):
            node.surface_id = surface_id
            node.error = None
        else:
            # The node left awaiting_input between park and link (cancelled,
            # or answered by a tap that found it through the dedup key).
            await self._close_card(surface_id)

    def _build_approval_card(self, node: DAGNode, dag: ExecutionDAG) -> tuple[Any, str]:
        """(validated card, ping text) for the node's current attempt (§3.4)."""
        from nous.a2ui.builders import approval_gate

        spec = node.approval_spec or {}
        deadline = as_utc(node.answer_deadline)
        default_label = label_of(spec, spec.get("default_option"))
        title = f"{dag.name} · {node.description or node.name}"
        remaining = (
            max((deadline - datetime.now(UTC)).total_seconds(), 0.0) if deadline else 0.0
        )
        built = approval_gate(
            {
                "title": title,
                "summary": build_card_summary(
                    node.instructions or "", self._context_results(node, dag)
                ),
                "risk": risk_line(deadline, default_label),
                "options": [
                    {
                        "id": o["id"],
                        "label": button_label(o["label"], o["outcome"]),
                        "outcome": o["outcome"],
                    }
                    for o in spec.get("options", [])
                ],
                "recommendation": spec.get("recommended_option"),
                "recommend_first": False,
                "defer_label": DEFER_LABEL,
                # Floored at a minute: a re-push right at the deadline must not
                # produce a zero expiry (falsy → expires_at NULL, no backstop).
                "expires_hours": max(
                    remaining + self._settings.dag_approval_card_grace_seconds, 60.0
                )
                / 3600,
            }
        )
        built.validate()
        return built, notify_text(title, node.instructions or "", deadline, default_label)

    def _context_results(self, node: DAGNode, dag: ExecutionDAG) -> list[tuple[str, str]]:
        by_id = {str(n.id): n for n in dag.nodes}
        results: list[tuple[str, str]] = []
        for edge in dag.edges:
            if edge.edge_type == "context_flow" and str(edge.to_node_id) == str(node.id):
                pred = by_id.get(str(edge.from_node_id))
                if pred is not None and pred.result:
                    results.append((pred.name, pred.result))
        return results

    async def _fail_parked(self, node: DAGNode, error: str) -> None:
        if await self._store.transition_node(
            node.id, from_statuses={"awaiting_input"}, status="failed", error=error,
            completed_at=datetime.now(UTC),
        ):
            node.status = "failed"
            node.error = error

    async def _close_card(self, surface_id: str | None, *, node_id: UUID | None = None) -> None:
        """Best-effort card close (§3.7); the leaked-card sweep retries."""
        if self._surface_service is None:
            return
        try:
            if surface_id:
                await self._surface_service.close(surface_id, "expired")
            elif node_id is not None:
                await self._surface_service.close_by_dedup_key(
                    approval_dedup_key(node_id), "expired"
                )
        except Exception:
            logger.warning(
                "Could not close approval card %s — the leaked-card sweep will retry",
                surface_id or node_id,
            )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `UV_PROJECT_ENVIRONMENT=E:/Projects/nous/.venv uv run --frozen pytest tests/test_dag_approval.py tests/test_dag_orchestrator.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add nous/dag/orchestrator.py tests/test_dag_approval.py
git commit -q -F <msgfile>   # "feat(dag): launch an approval node — retire, park, push, link"
```

### Task 9: The answer and the deadline

**Files:**
- Modify: `nous/dag/store.py` (`get_node_with_dag_status`)
- Modify: `nous/dag/orchestrator.py` (`answer_node`, `_poll_awaiting_input`, `_refresh_node`, `_adopt_live_card`, `_build_predecessor_context`, `_advance_dag` step 1.52)
- Test: `tests/test_dag_approval.py`

**Interfaces:**
- Consumes: `answer_values`, `AnswerResult`, `label_of`, `DEADLINE_ACTOR` (Task 6); `live_cards_by_prefix(prefix)` (Task 7); `_push_and_link`, `_close_card`, `_context_results` (Task 8).
- Produces: `DAGStore.get_node_with_dag_status(node_id) -> tuple[DAGNode, str] | None`; `DAGOrchestrator.answer_node(node_id: UUID, option_id: str, *, source: Literal["companion","deadline"], actor: str | None, surface_id: str | None = None) -> AnswerResult`; `_poll_awaiting_input(dag) -> None`.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_dag_approval.py`:

```python
async def test_a_tap_that_proceeds_is_recorded_once(store, subtask_mgr, surfaces):
    orch = _orch(store, subtask_mgr, surfaces)
    dag, node = await _parked(store, orch)

    first = await orch.answer_node(node.id, "send", source="companion", actor="unattributed", surface_id=node.surface_id)
    second = await orch.answer_node(node.id, "hold", source="companion", actor="unattributed", surface_id=node.surface_id)

    assert first.outcome == "recorded"
    assert (second.outcome, second.option_label) == ("closed", "Send it")
    node = await _node(store, dag.id, "approve")
    assert (node.status, node.answer, node.answer_source, node.answered_by) == (
        "completed", "send", "companion", "unattributed",
    )
    assert node.result.startswith("Answered in the companion: 'Send it' (send)")
    assert " by " not in node.result


async def test_an_attributed_actor_is_named(store, subtask_mgr, surfaces):
    orch = _orch(store, subtask_mgr, surfaces)
    _, node = await _parked(store, orch)
    await orch.answer_node(node.id, "send", source="companion", actor="alice@example.com", surface_id=node.surface_id)
    assert (await _node(store, node.dag_id, "approve")).result.endswith("by alice@example.com")


async def test_a_stop_answer_blocks_the_successor_and_stops_the_dag(store, subtask_mgr, surfaces):
    orch = _orch(store, subtask_mgr, surfaces)
    dag, node = await _parked(store, orch)

    result = await orch.answer_node(node.id, "hold", source="companion", actor="unattributed", surface_id=node.surface_id)
    await orch._advance_dag(await store.get_dag(dag.id))

    assert result.outcome == "recorded"
    assert (await _node(store, dag.id, "approve")).error.startswith("declined in the companion")
    assert (await _node(store, dag.id, "send")).status == "blocked"
    assert (await store.get_dag(dag.id)).status == "failed"


async def test_a_tap_before_the_link_is_recorded_and_links(store, subtask_mgr, surfaces):
    orch = _orch(store, subtask_mgr, surfaces)
    surfaces.push_errors = [RuntimeError("flaky")]
    dag, node = await _parked(store, orch)

    result = await orch.answer_node(node.id, "send", source="companion", actor="unattributed", surface_id="card-7")

    assert result.outcome == "recorded"
    assert (await _node(store, dag.id, "approve")).surface_id == "card-7"


@pytest.mark.parametrize(
    "setup, expected",
    [("stray", "stray_card"), ("dag_ended", "dag_ended"), ("pending", "not_open")],
)
async def test_refused_taps_say_why(store, subtask_mgr, surfaces, setup, expected):
    orch = _orch(store, subtask_mgr, surfaces)
    dag, node = await _parked(store, orch)
    card = node.surface_id
    if setup == "stray":
        card = "some-other-card"
    elif setup == "dag_ended":
        await store.update_dag_status(dag.id, "cancelled")
    else:
        await store.update_node(node.id, status="pending")

    result = await orch.answer_node(node.id, "send", source="companion", actor="unattributed", surface_id=card)

    assert result.outcome == expected


async def test_invalid_option_and_unknown_node(store, subtask_mgr, surfaces):
    orch = _orch(store, subtask_mgr, surfaces)
    _, node = await _parked(store, orch)
    assert (await orch.answer_node(node.id, "maybe", source="companion", actor=None)).outcome == "invalid_option"
    assert (await orch.answer_node(uuid.uuid4(), "send", source="companion", actor=None)).outcome == "not_linked"


async def test_the_deadline_applies_the_stop_default_in_one_tick(store, subtask_mgr, surfaces):
    orch = _orch(store, subtask_mgr, surfaces)
    dag, node = await _parked(store, orch)
    await store.update_node(node.id, answer_deadline=datetime.now(UTC) - timedelta(seconds=1))

    await orch._advance_dag(await store.get_dag(dag.id))

    node = await _node(store, dag.id, "approve")
    assert (node.status, node.answer, node.answer_source) == ("failed", "hold", "deadline")
    assert node.error.endswith("default 'Don't send' (hold) applied")
    assert surfaces.live() == {}
    assert (await _node(store, dag.id, "send")).status == "blocked"
    assert (await store.get_dag(dag.id)).status == "failed"


async def test_nothing_happens_before_the_deadline(store, subtask_mgr, surfaces):
    orch = _orch(store, subtask_mgr, surfaces)
    dag, node = await _parked(store, orch)
    await orch._advance_dag(await store.get_dag(dag.id))
    assert (await _node(store, dag.id, "approve")).status == "awaiting_input"


async def test_a_lost_card_is_pushed_again_and_relinked(store, subtask_mgr, surfaces):
    orch = _orch(store, subtask_mgr, surfaces)
    dag, node = await _parked(store, orch)
    surfaces.cards[node.surface_id]["status"] = "expired"  # closed behind the node's back

    await orch._advance_dag(await store.get_dag(dag.id))

    relinked = await _node(store, dag.id, "approve")
    assert relinked.surface_id != node.surface_id
    assert relinked.surface_id in surfaces.live()
    assert len(surfaces.pings) == 2


async def test_a_transient_push_failure_heals_on_the_next_tick(store, subtask_mgr, surfaces):
    orch = _orch(store, subtask_mgr, surfaces)
    surfaces.push_errors = [RuntimeError("flaky")]
    dag, _ = await _parked(store, orch)

    await orch._advance_dag(await store.get_dag(dag.id))

    node = await _node(store, dag.id, "approve")
    assert node.surface_id in surfaces.live() and node.error is None


async def test_a_crash_between_push_and_link_adopts_the_card(store, subtask_mgr, surfaces):
    orch = _orch(store, subtask_mgr, surfaces)
    dag, node = await _parked(store, orch)
    card = node.surface_id
    await store.update_node(node.id, surface_id=None)  # the link write never landed

    await orch._advance_dag(await store.get_dag(dag.id))

    assert (await _node(store, dag.id, "approve")).surface_id == card
    assert len(surfaces.cards) == 1 and len(surfaces.pings) == 1  # adopted, not re-pushed


async def test_the_acting_node_receives_the_approved_draft(store, subtask_mgr, surfaces):
    orch = _orch(store, subtask_mgr, surfaces)
    dag = await store.create(_request(with_draft=True))
    await orch.start_dag(dag.id)
    draft = await _node(store, dag.id, "draft")
    await store.update_node(draft.id, status="completed", result="Dear Bob, ...")
    await orch._advance_dag(await store.get_dag(dag.id))  # the approval parks
    node = await _node(store, dag.id, "approve")
    await orch.answer_node(node.id, "send", source="companion", actor=None, surface_id=node.surface_id)
    subtask_mgr.create.reset_mock()

    await orch._advance_dag(await store.get_dag(dag.id))  # 'send' launches

    task = subtask_mgr.create.call_args.kwargs["task"]
    assert "[Approved input from 'draft' (approved at 'approve')]: Dear Bob, ..." in task
    assert "Answered in the companion: 'Send it' (send)" in task


async def test_no_repush_for_a_node_a_tap_already_answered(store, subtask_mgr, surfaces):
    orch = _orch(store, subtask_mgr, surfaces)
    surfaces.push_errors = [RuntimeError("flaky")]
    dag, node = await _parked(store, orch)
    stale = await store.get_dag(dag.id)  # the tick's copy: parked, unlinked
    await orch.answer_node(node.id, "send", source="companion", actor=None, surface_id="card-9")

    await orch._poll_awaiting_input(stale)

    assert surfaces.cards == {}
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `UV_PROJECT_ENVIRONMENT=E:/Projects/nous/.venv uv run --frozen pytest tests/test_dag_approval.py -q`
Expected: FAIL — `'DAGOrchestrator' object has no attribute 'answer_node'`.

- [ ] **Step 3: Store** — `nous/dag/store.py`:

```python
    async def get_node_with_dag_status(self, node_id: UUID) -> tuple[DAGNode, str] | None:
        """One node plus its DAG's status, agent-scoped (Harness Phase 3)."""
        async with self._db.session() as session:
            row = (
                await session.execute(
                    select(DAGNode, ExecutionDAG.status)
                    .join(ExecutionDAG, ExecutionDAG.id == DAGNode.dag_id)
                    .where(DAGNode.id == node_id)
                    .where(ExecutionDAG.agent_id == self._agent_id)
                )
            ).first()
            return (row[0], row[1]) if row is not None else None
```

- [ ] **Step 4: Orchestrator** — imports add `AnswerResult, answer_values, DEADLINE_ACTOR, option_by_id` from `nous.dag.approval`, and `Literal` if not present. Module constant:

```python
# Columns answer_node / a re-read copy onto the tick's in-memory node.
_APPROVAL_ROW_FIELDS = (
    "status", "result", "error", "surface_id", "answer", "answered_by", "answered_at",
    "answer_source", "answer_deadline", "completed_at",
)
```

Methods:

```python
    async def answer_node(
        self,
        node_id: UUID,
        option_id: str,
        *,
        source: Literal["companion", "deadline"],
        actor: str | None,
        surface_id: str | None = None,
    ) -> AnswerResult:
        """Harness Phase 3 §3.5: the answer IS the transition.

        One conditional write sets the answer and the terminal status together,
        so a tap, the deadline and a second tap race on one row and exactly one
        wins. Takes no orchestrator lock (lock order: _lock → surface lock; the
        tap path holds only its card's surface lock).
        """
        loaded = await self._store.get_node_with_dag_status(node_id)
        if loaded is None or loaded[0].node_type != "approval" or not loaded[0].approval_spec:
            return AnswerResult(outcome="not_linked", node_id=node_id)
        node, _ = loaded
        option = option_by_id(node.approval_spec, option_id)
        if option is None:
            return AnswerResult(outcome="invalid_option", node_id=node_id, dag_id=node.dag_id)
        now = datetime.now(UTC)
        values: dict[str, Any] = {
            "answer": option_id,
            "answered_by": actor,
            "answered_at": now,
            "answer_source": source,
            "completed_at": now,
            **answer_values(
                node.approval_spec, option_id, source=source, actor=actor, at=now,
                deadline=node.answer_deadline,
            ),
        }
        if surface_id is not None:
            values["surface_id"] = surface_id  # a tap before the link step links it
        won = await self._store.transition_node(
            node_id,
            from_statuses={"awaiting_input"},
            dag_statuses=LIVE_DAG_STATUSES,
            card=surface_id,
            due_by=now if source == "deadline" else None,
            **values,
        )
        if won:
            return AnswerResult(
                outcome="recorded", node_id=node_id, dag_id=node.dag_id,
                option_label=option["label"], option_outcome=option["outcome"],
                node_status=values["status"], answer_source=source, answered_by=actor,
                answered_at=now,
            )
        fresh = await self._store.get_node_with_dag_status(node_id)
        if fresh is None:
            return AnswerResult(outcome="not_linked", node_id=node_id)
        row, dag_status = fresh
        if row.status in ("pending", "ready"):
            outcome = "not_open"
        elif row.status == "awaiting_input":
            outcome = "dag_ended" if dag_status not in LIVE_DAG_STATUSES else "stray_card"
        else:
            outcome = "closed"
        recorded = option_by_id(row.approval_spec, row.answer) or {}
        return AnswerResult(
            outcome=outcome, node_id=node_id, dag_id=row.dag_id,
            option_label=recorded.get("label"), option_outcome=recorded.get("outcome"),
            node_status=row.status, answer_source=row.answer_source,
            answered_by=row.answered_by, answered_at=row.answered_at,
        )

    async def _refresh_node(self, node: DAGNode) -> str | None:
        """Copy the row's approval columns onto the tick's node; returns the
        DAG status, or None if the node is gone."""
        fresh = await self._store.get_node_with_dag_status(node.id)
        if fresh is None:
            return None
        row, dag_status = fresh
        for field in _APPROVAL_ROW_FIELDS:
            setattr(node, field, getattr(row, field))
        return dag_status

    async def _poll_awaiting_input(self, dag: ExecutionDAG) -> None:
        """Harness Phase 3 §3.6: apply due deadlines; re-push a lost card."""
        waiting = [n for n in dag.nodes if n.status == "awaiting_input"]
        if not waiting:
            return
        now = datetime.now(UTC)
        linked = [n.surface_id for n in waiting if n.surface_id]
        live: set[str] = set()
        if self._surface_service is not None and linked:
            try:
                live = await self._surface_service.live_ids(linked)
            except Exception:
                logger.warning("Could not read approval card liveness for DAG %s", dag.id)
                live = set(linked)  # unknown ≠ dead: do not re-push on a read failure
        for node in waiting:
            deadline = as_utc(node.answer_deadline)
            if deadline is not None and deadline <= now:  # pre-filter; SQL decides
                result = await self.answer_node(
                    node.id, (node.approval_spec or {}).get("default_option", ""),
                    source="deadline", actor=DEADLINE_ACTOR,
                )
                if result.outcome == "recorded":
                    await self._close_card(node.surface_id, node_id=node.id)
                    await self._refresh_node(node)  # this tick's propagation sees it
                continue
            if self._surface_service is None:
                continue
            if node.surface_id is None or node.surface_id not in live:
                # Re-read first: the tick's copy may predate a tap that answered
                # through the unlinked card — pushing then would create a fresh
                # card and ping for an answered question.
                if await self._refresh_node(node) is None or node.status != "awaiting_input":
                    continue
                if node.surface_id is not None and node.surface_id in live:
                    continue
                if node.surface_id is None and await self._adopt_live_card(node):
                    continue
                await self._push_and_link(node, dag)

    async def _adopt_live_card(self, node: DAGNode) -> bool:
        """§3.6: link a live card that carries the node's key but was never
        linked (a crash between push and link) instead of replacing it — the
        person may be tapping it, and a replacement rotates its nonce."""
        try:
            cards = await self._surface_service.live_cards_by_prefix(approval_dedup_key(node.id))
        except Exception:
            return False
        if not cards:
            return False
        surface_id = cards[0][0]
        if await self._store.transition_node(
            node.id, from_statuses={"awaiting_input"}, card=surface_id,
            surface_id=surface_id, error=None,
        ):
            node.surface_id = surface_id
            node.error = None
            return True
        return False
```

`_build_predecessor_context` — the acting node receives what was approved (spec §3.5). In its loop over `context_preds`, before appending a predecessor's own result:

```python
        for pred_id in context_preds:
            pred = node_by_id.get(pred_id)
            if pred is None:
                continue
            if pred.node_type == "approval":
                # Harness Phase 3 §3.5: an approval's own result is only the
                # answer text. Pass its context_flow inputs (the draft the
                # person saw) through, or the acting node writes its own text.
                for inner_name, inner_result in self._context_results(pred, dag):
                    parts.append(
                        f"[Approved input from '{inner_name}' (approved at '{pred.name}')]: "
                        f"{inner_result}"
                    )
            if pred.result:
                parts.append(f"[Result from '{pred.name}']: {pred.result}")
```

`_advance_dag`: after `await self._poll_awaiting_checks(dag)` add

```python
        # 1.52 Harness Phase 3: approval deadlines and lost-card re-pushes.
        # Before the budget and failure steps, so a default applied here is
        # propagated on this same tick.
        await self._poll_awaiting_input(dag)
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `UV_PROJECT_ENVIRONMENT=E:/Projects/nous/.venv uv run --frozen pytest tests/test_dag_approval.py tests/test_dag_approval_store.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add nous/dag/store.py nous/dag/orchestrator.py tests/test_dag_approval.py
git commit -q -F <msgfile>   # "feat(dag): the answer is the transition; deadline default and lost-card re-push"
```

### Task 10: Cancels, the budget path, and the leaked-card sweep

**Files:**
- Modify: `nous/dag/orchestrator.py` (`_cancel_one`, `_handle_budget_exceeded`, new `_sweep_leaked_approval_cards`, `tick`)
- Modify: `nous/dag/store.py` (new `awaiting_input_nodes_in_terminal_dags`)
- Test: `tests/test_dag_approval.py`

**Interfaces:**
- Consumes: `_cancel_one` (Task 2), `_close_card` (Task 8), `get_node_with_dag_status` (Task 9), `node_id_from_dedup_key`, `DEDUP_PREFIX` (Task 6), `TERMINAL_DAG_STATUSES` (Task 2), `live_cards_by_prefix(prefix)` (Task 7), `idx_dag_nodes_awaiting_input` (Task 3).
- Produces: `DAGStore.awaiting_input_nodes_in_terminal_dags(limit: int) -> list[DAGNode]`; `async _sweep_leaked_approval_cards() -> None`, called inside `tick()`'s `_lock` block.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_dag_approval.py`:

```python
from sqlalchemy import update as sa_update

from nous.storage.models import ExecutionDAG


async def test_cancel_dag_cancels_a_waiting_node_and_closes_its_card(store, subtask_mgr, surfaces):
    orch = _orch(store, subtask_mgr, surfaces)
    dag, node = await _parked(store, orch)

    await orch.cancel_dag(dag.id)
    late = await orch.answer_node(node.id, "send", source="companion", actor=None, surface_id=node.surface_id)

    assert (await _node(store, dag.id, "approve")).status == "cancelled"
    assert surfaces.live() == {}
    assert (late.outcome, late.node_status) == ("closed", "cancelled")


async def test_cancel_dag_loses_to_an_answer_that_landed_first(store, subtask_mgr, surfaces, monkeypatch):
    orch = _orch(store, subtask_mgr, surfaces)
    dag, node = await _parked(store, orch)
    real_cancel = orch._cancel_node

    async def answered_first(n):
        if n.id == node.id:
            await orch.answer_node(n.id, "send", source="companion", actor=None, surface_id=node.surface_id)
        await real_cancel(n)

    monkeypatch.setattr(orch, "_cancel_node", answered_first)
    await orch.cancel_dag(dag.id)

    assert (await _node(store, dag.id, "approve")).status == "completed"


async def test_a_cascade_cancel_closes_the_card(store, subtask_mgr, surfaces):
    orch = _orch(store, subtask_mgr, surfaces)
    request = _request()
    request.nodes.append(DAGNodeSpec(name="src", type=DAGNodeType.subtask, instructions="s"))
    request.edges.append(DAGEdgeSpec(from_node="src", to_node="approve", edge_type="cancel_cascade"))
    dag = await store.create(DAGCreateRequest(name="c", nodes=request.nodes, edges=request.edges))
    await orch.start_dag(dag.id)
    src = await _node(store, dag.id, "src")
    await store.update_node(src.id, status="failed", error="boom")

    await orch._advance_dag(await store.get_dag(dag.id))

    assert (await _node(store, dag.id, "approve")).status == "cancelled"
    assert surfaces.live() == {}


async def test_the_budget_path_cancels_a_waiting_node(db, store, subtask_mgr, surfaces):
    orch = _orch(store, subtask_mgr, surfaces, dag_token_budget_enforcement_enabled=True)
    dag = await store.create(
        DAGCreateRequest(name="b", nodes=_request().nodes, edges=_request().edges, token_budget=10)
    )
    await orch.start_dag(dag.id)
    async with db.session() as session:
        await session.execute(
            sa_update(ExecutionDAG).where(ExecutionDAG.id == dag.id).values(tokens_consumed=20)
        )
        await session.commit()

    await orch._advance_dag(await store.get_dag(dag.id))

    assert (await _node(store, dag.id, "approve")).status == "cancelled"
    assert surfaces.live() == {}
    assert (await store.get_dag(dag.id)).status in ("failed", "partial")


async def test_the_sweep_closes_leaked_cards_and_leaves_fresh_ones(store, subtask_mgr, surfaces):
    """Two live cards under one dedup key cannot happen on Postgres (the
    partial UNIQUE index on (agent_id, dedup_key) WHERE live) — the stray
    rule is defensive, and the fake lets us exercise it."""
    orch = _orch(store, subtask_mgr, surfaces)
    dag, node = await _parked(store, orch)
    key = approval_dedup_key(node.id)
    surfaces.cards["stray"] = {"dedup_key": key, "status": "live", "built": None}
    surfaces.cards["ghost"] = {"dedup_key": approval_dedup_key(uuid.uuid4()), "status": "live", "built": None}

    await orch._sweep_leaked_approval_cards()

    assert set(surfaces.live()) == {node.surface_id}  # stray + ghost closed, the linked card kept


async def test_the_sweep_leaves_an_unlinked_fresh_card_alone(store, subtask_mgr, surfaces):
    orch = _orch(store, subtask_mgr, surfaces)
    dag = await store.create(_request())
    node_id = (await _node(store, dag.id, "approve")).id

    async def sweep_between_push_and_link(_sid):
        await orch._sweep_leaked_approval_cards()

    surfaces.on_push = sweep_between_push_and_link
    await orch.start_dag(dag.id)

    node = await _node(store, dag.id, "approve")
    assert node.id == node_id and node.surface_id in surfaces.live()


async def test_the_sweep_cancels_a_waiting_node_in_a_finished_dag(store, subtask_mgr, surfaces):
    orch = _orch(store, subtask_mgr, surfaces)
    dag, node = await _parked(store, orch)
    await store.update_dag_status(dag.id, "failed")

    await orch._sweep_leaked_approval_cards()

    assert (await _node(store, dag.id, "approve")).status == "cancelled"
    assert surfaces.live() == {}


async def test_the_sweep_finds_a_stranded_node_that_has_no_card(store, subtask_mgr, surfaces):
    """The probe's end state: awaiting_input in a cancelled DAG, no card.
    The card-driven pass cannot see it; the node-driven query must."""
    orch = _orch(store, subtask_mgr, surfaces)
    surfaces.push_errors = [RuntimeError("flaky")]
    dag, _ = await _parked(store, orch)  # parked, push failed: no card
    await store.update_dag_status(dag.id, "cancelled")

    await orch._sweep_leaked_approval_cards()

    assert (await _node(store, dag.id, "approve")).status == "cancelled"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `UV_PROJECT_ENVIRONMENT=E:/Projects/nous/.venv uv run --frozen pytest tests/test_dag_approval.py -q`
Expected: FAIL — the card stays live after `cancel_dag`; `_sweep_leaked_approval_cards` does not exist.

- [ ] **Step 3: Implement**

`_cancel_one`, after `if won:` updates the node:

```python
            if node.node_type == "approval":
                # The card is the approval's only primitive; close it after the
                # win so an answer that landed first keeps its card (§3.7).
                await self._close_card(None, node_id=node.id)
```

`_handle_budget_exceeded`, the cancel loop becomes:

```python
        for node in dag.nodes:
            if node.status in ("pending", "ready", "awaiting_check", "awaiting_input"):
                # Conditional (§3.3); awaiting_input (Harness Phase 3) is future
                # work the budget stops — its card closes with it.
                if await self._cancel_one(node, _BUDGET_CANCEL_ERROR):
                    cancelled_any = True
```

(Note in the commit message: `_cancel_one` also tears down an `awaiting_check` node's heartbeat check, which the old blind write left for the reconciliation sweep.)

Orchestrator imports: add `DEDUP_PREFIX, node_id_from_dedup_key` to the `nous.dag.approval` import (`TERMINAL_DAG_STATUSES` came in Task 2).

New store method (`nous/dag/store.py`), the sweep's node-driven query:

```python
    async def awaiting_input_nodes_in_terminal_dags(self, limit: int) -> list[DAGNode]:
        """Harness Phase 3 §3.7: awaiting_input nodes whose DAG has ended.

        The card-driven sweep cannot see one that has no card. A probe put a
        node there with conditional writes only: dag_statuses is a snapshot,
        so a concurrent retry plus a failed push can park a node in a DAG that
        turns terminal a moment later. Served by idx_dag_nodes_awaiting_input.
        """
        async with self._db.session() as session:
            rows = await session.execute(
                select(DAGNode)
                .join(ExecutionDAG, ExecutionDAG.id == DAGNode.dag_id)
                .where(ExecutionDAG.agent_id == self._agent_id)
                .where(DAGNode.status == "awaiting_input")
                .where(ExecutionDAG.status.in_(sorted(TERMINAL_DAG_STATUSES)))
                .limit(limit)
            )
            return list(rows.scalars().all())
```

New sweep (orchestrator):

```python
    _APPROVAL_SWEEP_BATCH = 20

    async def _sweep_leaked_approval_cards(self) -> None:
        """Harness Phase 3 §3.7: retire approval cards whose node moved on,
        and cancel waiting nodes stranded in a DAG that has ended.

        Modelled on _sweep_leaked_heartbeat_checks but run INSIDE `_lock`: it
        must never interleave with a launch between push and link. A card
        whose node is unlinked (surface_id NULL) is left alone — push and link
        own it, and it may be the card just pushed.
        """
        now = datetime.now(UTC)
        # Node-driven: a stranded node may have no card at all.
        try:
            stranded = await self._store.awaiting_input_nodes_in_terminal_dags(
                limit=self._APPROVAL_SWEEP_BATCH
            )
        except Exception:
            logger.exception("Error listing stranded approval nodes")
            stranded = []
        for node in stranded:
            if await self._store.transition_node(
                node.id,
                from_statuses={"awaiting_input"},
                dag_statuses=TERMINAL_DAG_STATUSES,
                status="cancelled",
                error="DAG ended while waiting",
                completed_at=now,
            ):
                await self._close_card(None, node_id=node.id)
        if self._surface_service is None:
            return
        # Card-driven: EVERY live DAG card — a bounded page would fill with
        # healthy cards once the parked cap is reached and never reach the
        # leaked ones. Keys map to nodes in Python (SQLite stores UUIDs
        # without dashes, so an SQL text join would differ from Postgres).
        try:
            cards = await self._surface_service.live_cards_by_prefix(DEDUP_PREFIX)
        except Exception:
            logger.exception("Error listing live approval cards")
            return
        for surface_id, dedup_key in cards:
            node_id = node_id_from_dedup_key(dedup_key)
            loaded = await self._store.get_node_with_dag_status(node_id) if node_id else None
            if loaded is None:
                await self._close_card(surface_id)
                continue
            node, dag_status = loaded
            if node.status != "awaiting_input":
                await self._close_card(surface_id)
            elif dag_status not in LIVE_DAG_STATUSES:
                if await self._store.transition_node(
                    node.id,
                    from_statuses={"awaiting_input"},
                    dag_statuses=TERMINAL_DAG_STATUSES,
                    status="cancelled",
                    error="DAG ended while waiting",
                    completed_at=now,
                ):
                    await self._close_card(surface_id)
            elif node.surface_id is not None and node.surface_id != surface_id:
                await self._close_card(surface_id)
```

`tick()`: as the last statement inside `async with self._lock:` (after the per-DAG loop), `await self._sweep_leaked_approval_cards()`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `UV_PROJECT_ENVIRONMENT=E:/Projects/nous/.venv uv run --frozen pytest tests/test_dag_approval.py tests/test_dag_durability.py tests/test_dag_orchestrator.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add nous/dag/orchestrator.py nous/dag/store.py tests/test_dag_approval.py
git commit -q -F <msgfile>   # "feat(dag): cancels and budget close approval cards; leaked-card sweep under _lock"
```

### Task 11: Dispatch-time admission for resumed DAGs

**Files:**
- Modify: `nous/dag/orchestrator.py` (`_is_working`, `_costs_nothing`, `held_reason`, `tick`, `_advance_dag(..., may_start=True)`)
- Test: `tests/test_dag_approval.py`

**Interfaces:**
- Consumes: `MAX_ACTIVE_DAGS` from `nous.dag.store`.
- Produces: `_advance_dag(self, dag, *, may_start: bool = True)`; `held_reason(dag_id: UUID) -> str | None` (Task 15 prints it in `dag_manage status`). `grep -rn "_advance_dag(" nous tests` (on `1daa004`): `tick()` is the only production caller; tests call it positionally, which the default keeps working.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_dag_approval.py`:

```python
from nous.dag.store import MAX_ACTIVE_DAGS


async def _working_dag(store):
    dag = await store.create(
        DAGCreateRequest(name="w", nodes=[DAGNodeSpec(name="n", type=DAGNodeType.subtask, instructions="x")])
    )
    await store.update_dag_status(dag.id, "running")
    await store.update_node(dag.nodes[0].id, status="running", started_at=datetime.now(UTC))
    return dag


async def test_a_resumed_dag_waits_for_a_working_slot(store, subtask_mgr, surfaces):
    orch = _orch(store, subtask_mgr, surfaces)
    resumed, node = await _parked(store, orch)  # created first: the oldest DAG
    others = [await _working_dag(store) for _ in range(MAX_ACTIVE_DAGS)]
    await orch.answer_node(node.id, "send", source="companion", actor=None, surface_id=node.surface_id)
    subtask_mgr.create.reset_mock()

    await orch.tick()

    assert (await _node(store, resumed.id, "send")).status == "pending"
    subtask_mgr.create.assert_not_called()
    assert orch._defer_counts.get((await _node(store, resumed.id, "send")).id) is None

    assert orch.held_reason(resumed.id) == (
        f"approved — waiting for a free slot ({MAX_ACTIVE_DAGS}/{MAX_ACTIVE_DAGS} DAGs working)"
    )

    await store.update_node(others[0].nodes[0].id, status="completed")
    await orch.tick()

    assert (await _node(store, resumed.id, "send")).status == "running"
    assert orch.held_reason(resumed.id) is None


async def test_a_held_dag_still_asks_its_next_question(store, subtask_mgr, surfaces):
    """Parking an approval takes no subtask; holding it would only delay the question."""
    orch = _orch(store, subtask_mgr, surfaces)
    dag = await store.create(
        DAGCreateRequest(
            name="two-step",
            nodes=[
                _approve(),
                _approve(name="approve2", instructions="And the follow-up?"),
                DAGNodeSpec(name="send", type=DAGNodeType.subtask, instructions="send"),
            ],
            edges=[
                DAGEdgeSpec(from_node="approve", to_node="approve2", edge_type="context_flow"),
                DAGEdgeSpec(from_node="approve2", to_node="send", edge_type="context_flow"),
            ],
        )
    )
    await orch.start_dag(dag.id)
    for _ in range(MAX_ACTIVE_DAGS):
        await _working_dag(store)
    first = await _node(store, dag.id, "approve")
    await orch.answer_node(first.id, "send", source="companion", actor=None, surface_id=first.surface_id)

    await orch.tick()

    assert (await _node(store, dag.id, "approve2")).status == "awaiting_input"


async def test_the_gate_is_inert_without_approval_nodes(store, subtask_mgr, surfaces, monkeypatch):
    orch = _orch(store, subtask_mgr, surfaces)
    for _ in range(3):
        await _working_dag(store)
    calls: list[bool] = []

    async def record(dag, *, may_start=True):
        calls.append(may_start)

    monkeypatch.setattr(orch, "_advance_dag", record)
    await orch.tick()

    assert calls == [True, True, True]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `UV_PROJECT_ENVIRONMENT=E:/Projects/nous/.venv uv run --frozen pytest tests/test_dag_approval.py -k "slot or inert" -q`
Expected: FAIL — the resumed DAG's `send` launches on the first tick; `_advance_dag` does not take `may_start`.

- [ ] **Step 3: Implement**

Import `MAX_ACTIVE_DAGS` from `nous.dag.store`. Module level:

```python
_WORKING_NODE_STATUSES = frozenset({"ready", "running", "awaiting_check"})


def _is_working(dag: ExecutionDAG) -> bool:
    """A node ready, running or awaiting_check. The counter rule (§3.11): a
    parked approval or an instant gate/callback leaves a DAG NOT working, so
    it never takes a slot."""
    return any(n.status in _WORKING_NODE_STATUSES for n in dag.nodes)
```

Constructor additions:

```python
        # Harness Phase 3 §3.11: DAGs the dispatch gate held on the LAST tick
        # (rebuilt every tick, so a DAG cancelled while held drops out), and
        # the working count it saw — read by dag_manage via held_reason().
        self._held: set[UUID] = set()
        self._held_this_tick: set[UUID] = set()
        self._working_count = 0
```

Methods:

```python
    def _costs_nothing(self, node: DAGNode) -> bool:
        """Never held by the gate: parking an approval takes no subtask, a gate
        auto-passes, and a callback executes nothing while its flag is off."""
        return node.node_type in ("approval", "gate") or (
            node.node_type == "callback" and not self._settings.dag_callback_execution_enabled
        )

    def held_reason(self, dag_id: UUID) -> str | None:
        """Why a DAG the person approved has not moved yet (§3.11)."""
        if dag_id not in self._held:
            return None
        return (
            f"approved — waiting for a free slot "
            f"({self._working_count}/{MAX_ACTIVE_DAGS} DAGs working)"
        )
```

`tick()`, the locked block:

```python
        async with self._lock:
            self.last_tick_at = datetime.now(UTC)
            dags = await self._store.get_active_dags()  # oldest first
            # Harness Phase 3 §3.11: resuming parked DAGs is admission-
            # controlled at dispatch. Parked DAGs do not count at creation, so
            # answering many at once could put up to MAX_ACTIVE_DAGS + the
            # parked cap to work together, overflow the agent-wide subtask
            # queue, and fail approved nodes after _MAX_DEFERRALS. The pre-
            # pass runs only when a loaded DAG has an approval node — without
            # one, creation already keeps the working set at the limit, and
            # every DAG dispatches exactly as before.
            gated = any(n.node_type == "approval" for d in dags for n in d.nodes)
            working = sum(1 for d in dags if _is_working(d)) if gated else 0
            self._working_count = working
            self._held_this_tick = set()
            for dag in dags:
                was_working = _is_working(dag)
                may_start = not gated or was_working or working < MAX_ACTIVE_DAGS
                try:
                    await self._advance_dag(dag, may_start=may_start)
                except Exception:
                    logger.exception("Error advancing DAG %s", dag.id)
                # Counter rule: count a DAG only if it now has real work. A
                # DAG that finished this tick frees its slot on the next one.
                # "Working" is re-counted every tick, so a DAG between waves
                # (nothing ready/running at tick start) can be held too, not
                # only a resumed one — oldest first, that is benign.
                if gated and not was_working and _is_working(dag):
                    working += 1
                    self._working_count = working
            # Fresh each tick: a DAG cancelled while held is never advanced
            # again, and must not keep reporting "waiting for a free slot".
            self._held = self._held_this_tick
            await self._sweep_leaked_approval_cards()
```

`_advance_dag(self, dag: ExecutionDAG, *, may_start: bool = True)`, step 4:

```python
        # 4. Find and launch ready nodes (F064.2 dispatch with optional per-
        # frame caps; falls back to legacy behavior when flag is off).
        # Harness Phase 3 §3.11: a DAG held by the dispatch gate keeps its
        # costly ready nodes 'pending' this tick — no deferral is counted —
        # and still dispatches the ones that cost nothing (an approval's next
        # question, a gate, an inert callback).
        ready_nodes = self._find_ready_nodes(dag)
        if not may_start:
            held = [n for n in ready_nodes if not self._costs_nothing(n)]
            ready_nodes = [n for n in ready_nodes if self._costs_nothing(n)]
            if held:
                self._held_this_tick.add(dag.id)
                logger.info(
                    "DAG %s holds %d ready node(s): %d DAGs are already working",
                    dag.id, len(held), MAX_ACTIVE_DAGS,
                )
        await self._dispatch_ready_nodes(dag, ready_nodes)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `UV_PROJECT_ENVIRONMENT=E:/Projects/nous/.venv uv run --frozen pytest tests/test_dag_approval.py tests/test_dag_orchestrator.py tests/test_dag_concurrency_caps.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add nous/dag/orchestrator.py tests/test_dag_approval.py
git commit -q -F <msgfile>   # "feat(dag): dispatch-time admission for resumed parked DAGs"
```

### Task 12: A person's "no" stays a "no" — retry refusal

**Files:**
- Modify: `nous/dag/orchestrator.py` (`retry_node` gains `allow_declined`)
- Modify: `nous/a2ui/actions.py` (`_dag_verb` retry passes `allow_declined=True`)
- Test: `tests/test_dag_approval.py`

**Interfaces:**
- Consumes: `declined_retry_refusal` (Task 6).
- Produces: `retry_node(self, dag_id: UUID, node_name: str, *, allow_declined: bool = False) -> None`.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_dag_approval.py`:

```python
async def _stopped(store, orch, *, source: str):
    dag, node = await _parked(store, orch)
    if source == "companion":
        await orch.answer_node(node.id, "hold", source="companion", actor=None, surface_id=node.surface_id)
    else:
        await store.update_node(node.id, answer_deadline=datetime.now(UTC) - timedelta(seconds=1))
    await orch._advance_dag(await store.get_dag(dag.id))
    assert (await store.get_dag(dag.id)).status == "failed"
    return dag, node


async def test_the_agent_cannot_re_ask_a_declined_question(store, subtask_mgr, surfaces):
    orch = _orch(store, subtask_mgr, surfaces)
    dag, _ = await _stopped(store, orch, source="companion")

    with pytest.raises(ValueError, match="dag_monitor"):
        await orch.retry_node(dag.id, "approve")


async def test_the_companion_can_re_ask_a_declined_question(store, subtask_mgr, surfaces):
    orch = _orch(store, subtask_mgr, surfaces)
    dag, first = await _stopped(store, orch, source="companion")

    await orch.retry_node(dag.id, "approve", allow_declined=True)
    await orch._advance_dag(await store.get_dag(dag.id))

    node = await _node(store, dag.id, "approve")
    assert node.status == "awaiting_input"
    assert node.surface_id != first.surface_id
    assert surfaces.cards[first.surface_id]["status"] == "expired"  # step 0 retired it
    assert [h["answer_source"] for h in node.answer_history] == ["companion"]
    assert (await _node(store, dag.id, "send")).status == "pending"


async def test_the_agent_may_re_ask_a_question_nobody_answered(store, subtask_mgr, surfaces):
    orch = _orch(store, subtask_mgr, surfaces)
    dag, _ = await _stopped(store, orch, source="deadline")

    await orch.retry_node(dag.id, "approve")
    await orch._advance_dag(await store.get_dag(dag.id))

    node = await _node(store, dag.id, "approve")
    assert node.status == "awaiting_input"
    assert [h["answer_source"] for h in node.answer_history] == ["deadline"]
    assert len(surfaces.pings) == 2
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `UV_PROJECT_ENVIRONMENT=E:/Projects/nous/.venv uv run --frozen pytest tests/test_dag_approval.py -k "re_ask" -q`
Expected: FAIL — `retry_node()` got an unexpected keyword argument `allow_declined`; the agent retry is not refused.

- [ ] **Step 3: Implement** — `retry_node` signature `async def retry_node(self, dag_id: UUID, node_name: str, *, allow_declined: bool = False) -> None`; import `declined_retry_refusal` from `nous.dag.approval`; directly after the `if node.status != "failed": raise …` check:

```python
        # Harness Phase 3 §3.10: a person's "no" stays a "no". The agent may
        # re-ask a question nobody answered (deadline), never one a person
        # declined — only the companion's dag.retry passes allow_declined.
        if (
            node.node_type == "approval"
            and node.answer_source == "companion"
            and not allow_declined
        ):
            raise ValueError(declined_retry_refusal(node_name))
```

`nous/a2ui/actions.py`, `_dag_verb`: `await orchestrator.retry_node(UUID(dag_id), node, allow_declined=True)` with the comment `# Harness Phase 3 §3.10: a person tapping Retry may re-ask their own "no".`

- [ ] **Step 4: Run the tests to verify they pass**

Run: `UV_PROJECT_ENVIRONMENT=E:/Projects/nous/.venv uv run --frozen pytest tests/test_dag_approval.py tests/test_a2ui_actions.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add nous/dag/orchestrator.py nous/a2ui/actions.py tests/test_dag_approval.py
git commit -q -F <msgfile>   # "feat(dag): the agent cannot re-ask a declined approval; the companion can"
```

### Task 13: The action handler — DAG cards answer the node

**Files:**
- Modify: `nous/a2ui/actions.py` (`ActionContext.actor`; both construction sites; `approval_choose`; `approval_defer`)
- Create: `tests/test_a2ui_dag_approval_actions.py`

**Interfaces:**
- Consumes: `DAGOrchestrator.answer_node` (Task 9); `node_id_from_dedup_key`, `refusal_message`, `AnswerResult` (Task 6).
- Produces: `ActionContext.actor: str = "unattributed"`.

- [ ] **Step 1: Write the failing tests** — `tests/test_a2ui_dag_approval_actions.py`:

```python
"""Harness Phase 3 §3.5: approval.choose / approval.defer on a DAG card.

Handler-level (no database): the router's gate is unchanged; what changes is
that a card carrying the dag-approval: key answers its node and never takes
the generic path.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

from nous.a2ui.actions import ActionContext, ActionRouter
from nous.config import Settings
from nous.dag.approval import AnswerResult, approval_dedup_key

RISK = "If nobody answers by 2026-09-26 12:00 UTC, 'Hold' applies."


def _router(orchestrator):
    return ActionRouter(None, Settings(_env_file=None), None, dag_orchestrator=orchestrator)


def _ctx(router, *, name="approval.choose", option="send", dag_card=True, context=None, data=None):
    surface = SimpleNamespace(
        surface_id="card-1",
        dedup_key=approval_dedup_key(uuid.UUID(int=7)) if dag_card else None,
        data_model=data or {
            "options": [{"id": "send", "label": "Send"}, {"id": "hold", "label": "Hold"}],
            "risk": RISK,
            "summary": "Send it?",
        },
    )
    return ActionContext(
        surface=surface, name=name, context=context if context is not None else {"optionId": option},
        data_model=None, services=router, actor="alice@example.com",
    )


async def _run(router, ctx):
    return await router._handlers[ctx.name].fn(ctx)


async def test_a_recorded_answer_retires_the_card():
    orch = SimpleNamespace(answer_node=AsyncMock(return_value=AnswerResult(outcome="recorded")))
    router = _router(orch)

    result = await _run(router, _ctx(router))

    assert result.ok and result.resolve_surface and result.data_patches == []
    orch.answer_node.assert_awaited_once_with(
        uuid.UUID(int=7), "send", source="companion", actor="alice@example.com", surface_id="card-1"
    )


async def test_a_refused_answer_leaves_the_card_up_saying_why():
    closed = AnswerResult(outcome="closed", option_label="Send", answer_source="companion")
    router = _router(SimpleNamespace(answer_node=AsyncMock(return_value=closed)))

    result = await _run(router, _ctx(router, option="hold"))

    assert not result.ok and not result.resolve_surface
    assert result.message.startswith("already answered 'Send'")


async def test_an_unwired_orchestrator_keeps_the_card():
    router = _router(None)
    result = await _run(router, _ctx(router))
    assert not result.ok and not result.resolve_surface
    assert "not running" in result.message


async def test_an_agent_pushed_card_behaves_as_before():
    orch = SimpleNamespace(answer_node=AsyncMock())
    router = _router(orch)

    result = await _run(router, _ctx(router, dag_card=False))

    assert result.ok and result.resolve_surface
    assert result.data_patches == [("/summary", "Decided: send.")]
    orch.answer_node.assert_not_awaited()


async def test_defer_on_a_dag_card_keeps_the_draft_and_restates_the_default():
    router = _router(SimpleNamespace())
    result = await _run(router, _ctx(router, name="approval.defer", context={}))
    assert result.data_patches == [("/risk", f"Deferred. {RISK}")]


async def test_companion_retry_may_re_ask_a_declined_question():
    orch = SimpleNamespace(retry_node=AsyncMock())
    router = _router(orch)
    dag_id = uuid.uuid4()
    ctx = _ctx(
        router, name="dag.retry", dag_card=False, context={"node": "approve"},
        data={"dag_id": str(dag_id), "nodes": [{"name": "approve"}]},
    )

    result = await _run(router, ctx)

    assert result.ok
    orch.retry_node.assert_awaited_once_with(dag_id, "approve", allow_declined=True)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `UV_PROJECT_ENVIRONMENT=E:/Projects/nous/.venv uv run --frozen pytest tests/test_a2ui_dag_approval_actions.py -q`
Expected: FAIL — `ActionContext.__init__() got an unexpected keyword argument 'actor'`.

- [ ] **Step 3: Implement** — `nous/a2ui/actions.py`: `from nous.dag.approval import node_id_from_dedup_key, refusal_message`.

`ActionContext`: add, after `services`:

```python
    # Harness Phase 3: who acted, as the router recorded it for the audit
    # row — 'unattributed' unless forwarded identity is trusted.
    actor: str = "unattributed"
```

Pass `actor=actor` at both construction sites (`handle_call`, `actions.py:198`, and `_handle_locked`, `:352`).

`approval_choose` (keep the existing `offered` validation first):

```python
    async def approval_choose(ctx: ActionContext) -> ActionResult:
        option = str(ctx.context.get("optionId", ""))
        offered = {...}  # unchanged
        if option not in offered:
            return ActionResult(ok=False, message=f"option {option!r} was not offered by this surface")
        node_id = node_id_from_dedup_key(getattr(ctx.surface, "dedup_key", None))
        if node_id is not None:
            return await _dag_approval_choose(ctx, node_id, option)
        return ActionResult(  # unchanged generic path
            message=f"chose {option}",
            resolve_surface=True,
            data_patches=[("/summary", f"Decided: {option}.")],
        )

    async def _dag_approval_choose(ctx: ActionContext, node_id: UUID, option: str) -> ActionResult:
        """Harness Phase 3 §3.5: a DAG card never takes the generic path.

        A recorded answer retires the card — its disappearing is the
        confirmation. Every refusal leaves the card up with the reason: the
        companion shows a message only when ok is false, and a card that
        vanished on a late tap would read as accepted. The orchestrator
        retires it (leaked-card sweep, or the next attempt's step 0).
        """
        orchestrator = router._dag_orchestrator
        if orchestrator is None:
            return ActionResult(
                ok=False,
                message="DAG orchestration is not running; the answer cannot be recorded now",
            )
        result = await orchestrator.answer_node(
            node_id, option, source="companion", actor=ctx.actor,
            surface_id=ctx.surface.surface_id,
        )
        if result.outcome == "recorded":
            return ActionResult(message=f"recorded {option}", resolve_surface=True)
        return ActionResult(ok=False, message=refusal_message(result, option))
```

`approval_defer`:

```python
    async def approval_defer(ctx: ActionContext) -> ActionResult:
        if node_id_from_dedup_key(getattr(ctx.surface, "dedup_key", None)) is not None:
            # Harness Phase 3: a DAG card's /summary holds what the person is
            # approving — never overwrite it; restate what the deadline does.
            risk = str((ctx.surface.data_model or {}).get("risk") or "")
            patched = risk if risk.startswith("Deferred.") else f"Deferred. {risk}".strip()
            return ActionResult(
                message="deferred — the default applies at the deadline",
                data_patches=[("/risk", patched)],
            )
        ...  # unchanged agent-card path
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `UV_PROJECT_ENVIRONMENT=E:/Projects/nous/.venv uv run --frozen pytest tests/test_a2ui_dag_approval_actions.py tests/test_a2ui_actions.py -q`
Expected: PASS (postgres-only tests skipped locally).

- [ ] **Step 5: Commit**

```bash
git add nous/a2ui/actions.py tests/test_a2ui_dag_approval_actions.py
git commit -q -F <msgfile>   # "feat(a2ui): a DAG approval card answers its node; refusals stay on the card"
```

---

## Task group D — what people and the agent see, wiring, end to end

### Task 14: Stopped at an approval — completion summary, blocked text, F087 template

**Files:**
- Modify: `nous/dag/orchestrator.py` (`_check_dag_completion` failed branch; `_propagate_failures` blocked text)
- Modify: `nous/dag/delivery.py` (`build_template`)
- Test: `tests/test_dag_approval.py`, `tests/test_dag_delivery.py`

**Interfaces:**
- Consumes: `stopped_at_approval`, `stopped_summary`, `approval_line`, `is_answered_approval`, `BLOCKED_BY_APPROVAL` (Task 6).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_dag_approval.py`:

```python
from nous.dag.approval import BLOCKED_BY_APPROVAL


async def test_a_stop_reads_as_a_stop(store, subtask_mgr, surfaces):
    orch = _orch(store, subtask_mgr, surfaces)
    dag, _ = await _stopped(store, orch, source="companion")

    assert (await store.get_dag(dag.id)).result_summary == (
        "Stopped at approval 'approve': 'Don't send'; 1 step not run"
    )
    assert (await _node(store, dag.id, "send")).error == BLOCKED_BY_APPROVAL
```

Append to `tests/test_dag_delivery.py`:

```python
def test_template_for_a_dag_stopped_at_an_approval():
    import uuid
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from nous.config import Settings
    from nous.dag.delivery import DAGResultDelivery

    spec = {
        "options": [
            {"id": "send", "label": "Send it", "outcome": "proceed"},
            {"id": "hold", "label": "Don't send", "outcome": "stop"},
        ],
        "default_option": "hold",
    }
    approve = SimpleNamespace(
        name="approve", node_type="approval", status="failed", approval_spec=spec,
        answer="hold", answer_source="companion", answered_at=datetime(2026, 9, 25, 12, 0, tzinfo=UTC),
        answer_deadline=None, error="declined in the companion: …",
    )
    send = SimpleNamespace(name="send", node_type="subtask", status="blocked",
                           error="Blocked: an approval was declined or not answered")
    dag = SimpleNamespace(
        id=uuid.uuid4(), name="mail", status="failed", nodes=[approve, send],
        token_budget=None, tokens_consumed=0, result_summary=None,
    )

    text = DAGResultDelivery(Settings(_env_file=None), agent_id="t", bus=None, runner=None).build_template(dag)

    assert text.splitlines()[0].startswith("DAG 'mail' stopped at an approval")
    assert "Approvals:\n  approve: 'Don't send' in the companion at 2026-09-25 12:00 UTC" in text
    assert "Not run:\n  [blocked] send" in text
    assert "Problems:" not in text and "[failed] approve" not in text
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `UV_PROJECT_ENVIRONMENT=E:/Projects/nous/.venv uv run --frozen pytest tests/test_dag_approval.py -k "reads_as_a_stop" tests/test_dag_delivery.py -q`
Expected: FAIL — the summary reads `Failed nodes: approve`; the template reads `FAILED`.

- [ ] **Step 3: Implement**

`nous/dag/orchestrator.py` — import `BLOCKED_BY_APPROVAL, stopped_at_approval, stopped_summary`. In `_propagate_failures`, before the blocked loop: `blocked_error = BLOCKED_BY_APPROVAL if stopped_at_approval(dag.nodes) else "Predecessor failed"`, and use `error=blocked_error` in the transition. (Decided once per DAG, deliberately: in a DAG with both a stop answer and a crashed node, a node blocked only by the approval reads "Predecessor failed" — accurate for the DAG, which is failed, and the completion summary is unaffected. Per-node ancestry is not worth the code in v1.) `_check_dag_completion`, the failed branch:

```python
        elif any(n.status == "failed" for n in dag.nodes):
            # Harness Phase 3 §3.12: a DAG whose only failures are answered
            # approvals STOPPED — presentation only; the row stays 'failed'.
            if stopped_at_approval(dag.nodes):
                summary = stopped_summary(dag.nodes)
            else:
                failed_names = [n.name for n in dag.nodes if n.status == "failed"]
                summary = f"Failed nodes: {', '.join(failed_names)}"
            await self._store.update_dag_status(dag.id, "failed", result_summary=summary)
```

`nous/dag/delivery.py` — import `approval_line, is_answered_approval, stopped_at_approval` from `nous.dag.approval`. In `build_template`, after `nodes = list(dag.nodes or [])`:

```python
        # Harness Phase 3 §3.12: one predicate decides the verb, the summary
        # and the blocked text. Presentation only — the row and the bus event
        # stay 'failed'.
        stopped = dag.status == "failed" and stopped_at_approval(nodes)
        if stopped:
            verb = "stopped at an approval"
```

After the `Summary:` line and before the failures block:

```python
        approvals = [n for n in nodes if getattr(n, "node_type", None) == "approval"]
        if approvals:
            lines.append("")
            lines.append("Approvals:")
            for node in approvals[:_TEMPLATE_MAX_NODE_LINES]:
                lines.append(f"  {approval_line(node)}")
```

The failures block: `failed = [n for n in nodes if n.status in ("failed", "blocked", "cancelled") and not is_answered_approval(n)]` and the header `lines.append("Not run:" if stopped else "Problems:")`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `UV_PROJECT_ENVIRONMENT=E:/Projects/nous/.venv uv run --frozen pytest tests/test_dag_approval.py tests/test_dag_delivery.py tests/test_dag_orchestrator.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add nous/dag/orchestrator.py nous/dag/delivery.py tests/test_dag_approval.py tests/test_dag_delivery.py
git commit -q -F <msgfile>   # "feat(dag): a DAG stopped at an approval says so"
```

### Task 15: The agent's tools — `dag_create`, `dag_manage`, `push_surface` text

**Files:**
- Modify: `nous/api/tools.py` (`register_dag_tools`: `settings` param, refusals, field threading, schema, `dag_manage` list/status)
- Modify: `nous/a2ui/tools.py` (`push_surface` `approval_gate` description, lines 40-46)
- Create: `tests/test_dag_approval_tools.py`

**Interfaces:**
- Consumes: `approvals_wired` (Task 8); `approval_line`, `card_link` (Task 6).
- Produces: `register_dag_tools(dispatcher, store, orchestrator, settings: Any = None) -> None` (Task 16 passes `settings=settings`).

- [ ] **Step 1: Write the failing tests** — `tests/test_dag_approval_tools.py`:

```python
"""Harness Phase 3 §3.13-§3.14: the approval node through the agent's tools."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest_asyncio

from nous.api.tools import ToolDispatcher, register_dag_tools
from nous.config import Settings
from nous.dag.orchestrator import DAGOrchestrator
from nous.dag.store import DAGStore

APPROVAL = {
    "name": "approve", "type": "approval", "instructions": "Send the drafted email?",
    "options": [
        {"id": "send", "label": "Send it", "outcome": "proceed"},
        {"id": "hold", "label": "Don't send", "outcome": "stop"},
    ],
    "default_option": "hold",
}
SEND = {"name": "send", "type": "subtask", "instructions": "send"}
EDGES = [{"from_node": "approve", "to_node": "send", "edge_type": "context_flow"}]


def _settings(**overrides) -> Settings:
    base = dict(_env_file=None, dag_node_default_timeout=120, dag_node_max_timeout=3600)
    base.update(overrides)
    return Settings(**base)


class _Cards:
    """Just enough SurfaceService for a launch to park and link."""

    async def close_by_dedup_key(self, key, status="expired"):
        return []

    async def close(self, surface_id, status="expired"):
        # Present so a path that closes a card is exercised, not swallowed:
        # _close_card catches exceptions, so a missing method would pass silently.
        return None

    async def live_ids(self, surface_ids):
        return set(surface_ids)

    async def live_cards_by_prefix(self, prefix):
        return []

    async def push_built(self, built, **_):
        return "card-1"


@pytest_asyncio.fixture
async def store(db):
    return DAGStore(db, f"test-p3tools-{uuid.uuid4().hex[:8]}", _settings())


def _tools(store, settings, *, wired=True):
    subtasks = AsyncMock()
    subtasks.create.return_value = SimpleNamespace(id=uuid.uuid4(), status="pending")
    orch = DAGOrchestrator(
        store=store, subtask_mgr=subtasks, dynamic_loader=AsyncMock(), settings=settings,
        surface_service=_Cards() if wired else None,
    )
    orch.clock_wired = True
    dispatcher = ToolDispatcher()
    register_dag_tools(dispatcher, store, orch, settings=settings)
    return dispatcher


def _text(result) -> str:
    return result["content"][0]["text"]


async def test_the_schema_advertises_approval_only_when_enabled(store):
    off = _tools(store, _settings())
    on = _tools(store, _settings(dag_approval_nodes_enabled=True))
    item = lambda d: d._schemas["dag_create"]["properties"]["nodes"]["items"]  # noqa: E731
    assert "approval" not in item(off)["properties"]["type"]["enum"]
    assert "options" not in item(off)["properties"]
    assert "approval" in item(on)["properties"]["type"]["enum"]
    assert {"options", "default_option", "recommended_option", "answer_timeout_seconds"} <= set(
        item(on)["properties"]
    )


async def test_dag_create_refuses_approval_nodes_while_the_flag_is_off(store):
    result = await _tools(store, _settings())._handlers["dag_create"](
        name="m", nodes=[APPROVAL, SEND], edges=EDGES
    )
    assert "NOUS_DAG_APPROVAL_NODES_ENABLED" in _text(result)


async def test_dag_create_refuses_approval_nodes_without_the_companion(store):
    result = await _tools(store, _settings(dag_approval_nodes_enabled=True), wired=False)._handlers[
        "dag_create"
    ](name="m", nodes=[APPROVAL, SEND], edges=EDGES)
    assert "companion" in _text(result)


async def test_dag_create_threads_the_approval_fields(store):
    settings = _settings(dag_approval_nodes_enabled=True)
    dispatcher = _tools(store, settings)

    result = await dispatcher._handlers["dag_create"](name="m", nodes=[APPROVAL, SEND], edges=EDGES)

    assert "Created DAG" in _text(result)
    assert "NOUS_A2UI_PUBLIC_BASE_URL is unset" in _text(result)
    dag = (await store.get_active_dags())[0]
    node = next(n for n in dag.nodes if n.name == "approve")
    assert node.approval_spec["default_option"] == "hold"
    assert node.status == "awaiting_input"


async def test_dag_manage_shows_who_is_waiting(store):
    settings = _settings(dag_approval_nodes_enabled=True, a2ui_public_base_url="https://n.example")
    dispatcher = _tools(store, settings)
    await dispatcher._handlers["dag_create"](name="m", nodes=[APPROVAL, SEND], edges=EDGES)
    dag = (await store.get_active_dags())[0]

    listing = _text(await dispatcher._handlers["dag_manage"](action="list"))
    status = _text(await dispatcher._handlers["dag_manage"](action="status", dag_id=str(dag.id)))

    assert "waiting on you: approve" in listing
    assert "[?] approve (approval, w0) — awaiting_input" in status
    assert "waiting for an answer until" in status
    assert "card: https://n.example/companion#/s/card-1" in status
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `UV_PROJECT_ENVIRONMENT=E:/Projects/nous/.venv uv run --frozen pytest tests/test_dag_approval_tools.py -q`
Expected: FAIL — `register_dag_tools() got an unexpected keyword argument 'settings'`.

- [ ] **Step 3: Implement** — `nous/api/tools.py`, `register_dag_tools`:

Signature `def register_dag_tools(dispatcher: ToolDispatcher, store: "Any", orchestrator: "Any", settings: "Any" = None) -> None:` and, at the top of the body:

```python
    from nous.dag.approval import approval_line, card_link

    # Harness Phase 3 §3.13: the wired Settings, not a fresh Settings() —
    # the flag is read at call time; the schema advertises the approval type
    # only when the flag and the companion are both on.
    cfg = settings if settings is not None else getattr(orchestrator, "_settings", None)
    approvals_advertised = bool(
        getattr(cfg, "dag_approval_nodes_enabled", False) and getattr(cfg, "a2ui_enabled", False)
    )
```

In `dag_create`, after the `clock_wired` check:

```python
        wants_approval = any(n.get("type") == "approval" for n in kwargs.get("nodes", []))
        if wants_approval:
            if not getattr(cfg, "dag_approval_nodes_enabled", False):
                return _tool_error(
                    "Error: approval nodes are disabled — set "
                    "NOUS_DAG_APPROVAL_NODES_ENABLED=true to create them."
                )
            if not getattr(orchestrator, "approvals_wired", False):
                return _tool_error(
                    "Error: approval nodes need the companion app "
                    "(NOUS_A2UI_ENABLED=true), which is not running in this "
                    "process — the question could never be shown."
                )
```

In the node loop, after the `max_fix_attempts` block:

```python
                # Harness Phase 3: approval-node fields — threaded explicitly
                # (the F066.1 silent-drop lesson above).
                for key in ("options", "default_option", "recommended_option", "answer_timeout_seconds"):
                    if key in n:
                        node_data[key] = n[key]
```

Before `return {"content": …}` on success:

```python
            if wants_approval and not getattr(cfg, "a2ui_public_base_url", ""):
                lines.append(
                    "Note: NOUS_A2UI_PUBLIC_BASE_URL is unset, so the Telegram "
                    "ping's link is not tappable — tell the person to open the "
                    "companion to answer."
                )
```

`dag_manage list`, inside the loop:

```python
                    line = f"  {str(d.id)[:8]} | {d.name} | {d.status} | {completed}/{total} nodes done"
                    waiting = [n.name for n in d.nodes if n.status == "awaiting_input"]
                    if waiting:
                        line += f" | waiting on you: {', '.join(waiting)}"
                    lines.append(line)
```

`dag_manage status`: add `"awaiting_input": "?"` to `status_icons`; after the result block:

```python
                    if node.node_type == "approval":
                        line += f" | {approval_line(node)}"
                        if node.status == "awaiting_input" and node.surface_id:
                            base = getattr(cfg, "a2ui_public_base_url", "")
                            line += f" | card: {card_link(node.surface_id, base)}"
```

and, right after the `Status: {dag.status}` header line is built, the dispatch gate's reason (spec §3.11 — a person who said "proceed" can see why nothing moved yet):

```python
                held = getattr(orchestrator, "held_reason", lambda _dag_id: None)(dag.id)
                if held and dag.status in ("pending", "running"):
                    lines.insert(2, f"Held: {held}")
```

Schema: replace the literal type `enum` with `node_type_enum` and extend the description, and splice approval properties into the node item:

```python
    node_type_enum = ["subtask", "check", "gate", "callback", "fix"]
    approval_help = ""
    approval_properties: dict[str, Any] = {}
    if approvals_advertised:
        node_type_enum.append("approval")
        approval_help = (
            " 'approval' asks the person a question on a companion card and waits for "
            "the answer (up to answer_timeout_seconds, default 24 h). Put the question "
            "in instructions and give 2-4 options, each 'proceed' or 'stop'; "
            "default_option (applied if nobody answers) MUST be a 'stop' option. Wire it "
            "with two context_flow edges: draft → approval (the card shows the draft) and "
            "approval → the acting node (it runs only after a 'proceed' answer and "
            "receives the answer). The acting node must be a 'subtask' — a 'callback' "
            "executes nothing while NOUS_DAG_CALLBACK_EXECUTION_ENABLED is off. You cannot "
            "answer the card yourself: tell the person to open the companion. You cannot "
            "re-ask a question the person declined."
        )
        approval_properties = {
            "options": {
                "type": "array", "minItems": 2, "maxItems": 4,
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "pattern": "^[a-z0-9_-]{1,40}$"},
                        "label": {"type": "string", "maxLength": 80},
                        "outcome": {"type": "string", "enum": ["proceed", "stop"]},
                    },
                    "required": ["id", "label", "outcome"],
                },
                "description": "(type='approval' only) the answers; at least one 'proceed' and one 'stop'.",
            },
            "default_option": {"type": "string", "description": "(type='approval' only) option id applied at the deadline. Must be a 'stop' option."},
            "recommended_option": {"type": "string", "description": "(type='approval' only) option id to highlight. Default: none."},
            "answer_timeout_seconds": {"type": "integer", "minimum": 900, "description": "(type='approval' only) seconds to wait for an answer."},
        }
```

then `"enum": node_type_enum`, `"description": (… existing text …) + approval_help`, and `**approval_properties` inside the node item's `"properties"`.

`nous/a2ui/tools.py`, the `approval_gate` sentence (lines 42-46) becomes:

```python
                "recommendation, trace_id). It records the user's choice only "
                "as an audit row and resolves the card — nothing reads that "
                "choice back to you and nothing resumes for you, so do not "
                "wait on it. (With DAG approval nodes enabled, an 'approval' "
                "node in dag_create is how work waits on a person's answer.) "
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `UV_PROJECT_ENVIRONMENT=E:/Projects/nous/.venv uv run --frozen pytest tests/test_dag_approval_tools.py tests/test_dag_tools.py tests/test_dag_durability.py tests/test_dag_visibility.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add nous/api/tools.py nous/a2ui/tools.py tests/test_dag_approval_tools.py
git commit -q -F <msgfile>   # "feat(dag): approval nodes in dag_create and dag_manage (land dark)"
```

### Task 16: Wiring and documentation

**Files:**
- Modify: `nous/main.py` (DAG block and A2UI block)
- Modify: `CLAUDE.md` (environment-variable rows)
- Test: `tests/test_dag_approval_tools.py`

- [ ] **Step 1: Write the failing test** — append to `tests/test_dag_approval_tools.py`:

```python
def test_main_builds_the_surface_service_before_the_orchestrator():
    """Harness Phase 3 §3.13: the orchestrator receives the service at
    construction, so the service must exist first."""
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "nous" / "main.py").read_text(encoding="utf-8")
    orchestrator_at = source.index("dag_orchestrator = DAGOrchestrator(")
    assert source.count("SurfaceService(database, settings, heart=heart)") == 1
    assert source.index("SurfaceService(database, settings, heart=heart)") < orchestrator_at
    # The A2UI block used to start with its own `surface_service = None`; left
    # in place it would wipe the service built above, and every companion
    # action — every approval tap — would fail.
    assert source.count("surface_service = None") == 1
    assert source.index("surface_service = None") < orchestrator_at
    assert "surface_service = " not in source[orchestrator_at:]
    assert "surface_service=surface_service" in source
    assert "register_dag_tools(dispatcher, dag_store, dag_orchestrator, settings=settings)" in source
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `UV_PROJECT_ENVIRONMENT=E:/Projects/nous/.venv uv run --frozen pytest tests/test_dag_approval_tools.py -k main_builds -q`
Expected: FAIL — the service is constructed after the orchestrator.

- [ ] **Step 3: Implement** — `grep -n "surface_service" nous/main.py` first; the constructor line must be the only assignment. Immediately above `# F038: DAG Orchestration`:

```python
    # Harness Phase 3 §3.13: built BEFORE the DAG block so the orchestrator
    # can push approval cards. Only the constructor moves — it needs only the
    # database, settings and heart; the composer, ActionRouter,
    # register_a2ui_tools and the sweep task need the DAG store and
    # orchestrator, so they stay in the A2UI block below.
    surface_service = None
    if settings.a2ui_enabled:
        from nous.a2ui.service import SurfaceService

        surface_service = SurfaceService(database, settings, heart=heart)
```

In the DAG block: `DAGOrchestrator(..., surface_service=surface_service)` and `register_dag_tools(dispatcher, dag_store, dag_orchestrator, settings=settings)`. In the A2UI block delete THREE lines: its leading `surface_service = None` (`main.py:1066` — left in place it resets the service built above to `None`, so `ActionRouter`, `register_a2ui_tools` and the expiry sweep all get `None` and every approval tap fails), `from nous.a2ui.service import SurfaceService`, and `surface_service = SurfaceService(database, settings, heart=heart)`. Nothing else changes; the later uses (`:1113`, `:1122`, `:1143`, `:1148`, the components dict at `:1187`) read the one service.

`CLAUDE.md`, after the `NOUS_DAG_CALLBACK_EXECUTION_ENABLED` row, add:

```markdown
| `NOUS_DAG_APPROVAL_NODES_ENABLED` | `false` | Harness Phase 3 (park-and-resume). Lets `dag_create` author an `approval` node: it parks in status `awaiting_input`, pushes an `approval_gate` companion card under the reserved dedup key `dag-approval:<node_id>` (a Telegram ping carries the question, deadline and default), and resumes on the answer or — at its deadline — on its default, which v1 requires to be a `stop` option. The answer is one conditional write on the node (migration 076), so a tap, the deadline, a cancel and the budget path race on one row and exactly one wins; every other status write that can race it is conditional too (`DAGStore.transition_node`, conditional `apply_retry`). A `stop` answer fails the node and blocks its successors (now along `context_flow` too — before this, a failed node's context_flow-only successor stayed pending forever); the F087 message reads "stopped at an approval". The agent cannot re-ask a question a person declined; a `dag_monitor` card's Retry can. Gates CREATION only: nodes already waiting still answer and default when it is off. Requires `NOUS_A2UI_ENABLED`. **Rollback:** pre-076 code treats `awaiting_input` as non-terminal forever and counts it toward `MAX_ACTIVE_DAGS` — cancel every DAG with an approval node first. |
| `NOUS_DAG_APPROVAL_DEFAULT_WAIT_SECONDS` | `86400` | Harness Phase 3: an approval node's wait when its spec sets none (`ge=900`). |
| `NOUS_DAG_APPROVAL_MAX_WAIT_SECONDS` | `604800` | Harness Phase 3: ceiling on an approval node's wait, clamped at insert. |
| `NOUS_DAG_APPROVAL_CARD_GRACE_SECONDS` | `3600` | Harness Phase 3: an approval card's expiry is the node's deadline plus this (`ge=60`) — a backstop only; the orchestrator owns the deadline, and a leaked-card sweep (inside the tick lock) retires cards whose node moved on. `expire_sweep` writes no `no_objection` row for these cards. |
| `NOUS_DAG_MAX_PARKED_DAGS` | `20` | Harness Phase 3: a DAG waiting only on an approval answer is PARKED and does not count against `MAX_ACTIVE_DAGS=5`; this separately caps parked DAGs, refusing only a request that contains an approval node. Resuming is admission-controlled at dispatch: a resumed DAG starts new nodes only while fewer than 5 DAGs are working (the pre-pass is skipped when no loaded DAG has an approval node). |
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `UV_PROJECT_ENVIRONMENT=E:/Projects/nous/.venv uv run --frozen pytest tests/test_dag_approval_tools.py -q && UV_PROJECT_ENVIRONMENT=E:/Projects/nous/.venv uv run --frozen python -c "import nous.main"`
Expected: PASS; the import succeeds.

- [ ] **Step 5: Commit**

```bash
git add nous/main.py CLAUDE.md tests/test_dag_approval_tools.py
git commit -q -F <msgfile>   # "feat(dag): wire the surface service into the orchestrator; document the settings"
```

### Task 17: End to end with the real companion (Postgres)

**Files:**
- Create: `tests/test_dag_approval_e2e.py`

- [ ] **Step 1: Write the tests**

```python
"""Harness Phase 3 end to end: a companion tap resumes the DAG.

postgres_only: the real SurfaceService and ActionRouter
(a2ui_surfaces.allowed_actions does not round-trip on SQLite), the real
DAGStore and DAGOrchestrator. CI runs these (NOUS_TEST_DB=postgres).
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import delete, select

from nous.a2ui.actions import ActionRouter
from nous.a2ui.service import SurfaceService
from nous.dag.orchestrator import DAGOrchestrator
from nous.dag.schemas import DAGCreateRequest, DAGEdgeSpec, DAGNodeSpec, DAGNodeType
from nous.dag.store import DAGStore
from nous.storage.models import A2uiAction, A2uiSurface, ExecutionDAG

pytestmark = pytest.mark.postgres_only


@pytest_asyncio.fixture
async def world(db, settings):
    agent = f"test-p3e2e-{uuid.uuid4().hex[:10]}"
    cfg = settings.model_copy(
        update={
            "agent_id": agent, "telegram_bot_token": None, "telegram_chat_id": None,
            "dag_approval_nodes_enabled": True,
        }
    )
    surfaces = SurfaceService(db, cfg)
    store = DAGStore(db, agent, cfg)
    orch = DAGOrchestrator(
        store=store, subtask_mgr=AsyncMock(), dynamic_loader=AsyncMock(), settings=cfg,
        surface_service=surfaces,
    )
    orch.clock_wired = True
    yield SimpleNamespace(
        db=db, store=store, orch=orch, router=ActionRouter(db, cfg, surfaces, dag_orchestrator=orch)
    )
    async with db.session() as session:
        await session.execute(delete(ExecutionDAG).where(ExecutionDAG.agent_id == agent))
        await session.execute(delete(A2uiAction).where(A2uiAction.agent_id == agent))
        await session.execute(delete(A2uiSurface).where(A2uiSurface.agent_id == agent))
        await session.commit()


def _request() -> DAGCreateRequest:
    return DAGCreateRequest(
        name="mail",
        nodes=[
            DAGNodeSpec(
                name="approve", type=DAGNodeType.approval, instructions="Send it?",
                options=[
                    {"id": "send", "label": "Send it", "outcome": "proceed"},
                    {"id": "hold", "label": "Don't send", "outcome": "stop"},
                ],
                default_option="hold",
            ),
            DAGNodeSpec(name="after", type=DAGNodeType.gate),  # auto-passes: shows it launched
        ],
        edges=[DAGEdgeSpec(from_node="approve", to_node="after", edge_type="context_flow")],
    )


async def _node(world, dag_id, name):
    return next(n for n in (await world.store.get_dag(dag_id)).nodes if n.name == name)


async def _tap(world, surface_id, option):
    async with world.db.session() as session:
        nonce = (
            await session.execute(select(A2uiSurface.nonce).where(A2uiSurface.surface_id == surface_id))
        ).scalar_one()
    return await world.router.handle(
        {
            "action": {
                "name": "approval.choose", "surfaceId": surface_id,
                "context": {"optionId": option},
                "metadata": {"extensions": {"com_nous_nonce": nonce}},
            }
        },
        content_type="application/json",
    )


async def _started(world):
    dag = await world.store.create(_request())
    await world.orch.start_dag(dag.id)
    node = await _node(world, dag.id, "approve")
    assert node.status == "awaiting_input" and node.surface_id
    return dag, node


async def test_a_tap_resumes_the_dag(world):
    dag, node = await _started(world)

    status, body = await _tap(world, node.surface_id, "send")
    await world.orch.tick()

    assert (status, body["resolved"]) == (200, True)
    assert (await _node(world, dag.id, "after")).status == "completed"
    assert (await world.store.get_dag(dag.id)).status == "completed"


async def test_a_stop_tap_stops_the_dag(world):
    dag, node = await _started(world)

    status, _ = await _tap(world, node.surface_id, "hold")
    await world.orch.tick()

    assert status == 200
    assert (await _node(world, dag.id, "after")).status == "blocked"
    final = await world.store.get_dag(dag.id)
    assert final.status == "failed" and final.result_summary.startswith("Stopped at approval")


async def test_a_second_tap_is_refused_and_the_card_stays_up_until_the_sweep(world):
    dag, node = await _started(world)
    await world.orch.answer_node(node.id, "send", source="companion", actor="other-device", surface_id=node.surface_id)

    status, body = await _tap(world, node.surface_id, "hold")

    assert status == 422
    assert body["error"]["message"].startswith("already answered 'Send it'")
    async with world.db.session() as session:
        card = (await session.execute(select(A2uiSurface).where(A2uiSurface.surface_id == node.surface_id))).scalar_one()
    assert card.status == "live"

    await world.orch.tick()  # the leaked-card sweep retires it
    async with world.db.session() as session:
        card = (await session.execute(select(A2uiSurface).where(A2uiSurface.surface_id == node.surface_id))).scalar_one()
    assert card.status == "expired"
```

- [ ] **Step 2: Run** — locally these are skipped (SQLite). Confirm they collect:

Run: `UV_PROJECT_ENVIRONMENT=E:/Projects/nous/.venv uv run --frozen pytest tests/test_dag_approval_e2e.py -q`
Expected: `3 skipped`. They run in CI; if a Postgres is available locally, `NOUS_TEST_DB=postgres` runs them.

- [ ] **Step 3: Commit**

```bash
git add tests/test_dag_approval_e2e.py
git commit -q -F <msgfile>   # "test(dag): approval node end to end through the real companion (postgres)"
```

---

## Final verification (before the PR)

- [ ] **Full suite, compared with `main`** — run the whole suite on the branch and on a throwaway `main` worktree (`git worktree add ../nous-main-baseline main`), and compare the sets of failing test ids. Expected: no test fails on the branch that passes on `main`.

```bash
set -o pipefail
UV_PROJECT_ENVIRONMENT=E:/Projects/nous/.venv uv run --frozen pytest tests -q -p no:randomly 2>&1 | tee <scratch>/branch.txt | tail -5
```

- [ ] **Lint** — no new ruff findings in the touched files relative to `main`.
- [ ] **Verify-by-execution reviewer** — dispatch the usual reviewer on the branch diff (race injections, the SQLite/Postgres split, the flag-off path, the deploy note in spec §3.8) and fix by class before opening the PR.
- [ ] **PR** — the body names:
  - the deploy note (spec §3.8: count DAGs wedged by the old propagation on prod, read-only, before deploy);
  - the rollback note (cancel approval DAGs before rolling back past 076);
  - that these land **unflagged, for every DAG and node type**, behavior-identical outside the races they close: `PREDECESSOR_EDGE_TYPES` (failure propagation and retry now follow `context_flow`), the conditional writes in `_dispatch_ready_nodes` (four sites), `_defer_node`, `cancel_dag`, the cascade cancel, the block write and `apply_retry`, and the unblock clearing `started_at`/`completed_at`;
  - that the dispatch gate had no spec-level database review.
  Check `gh pr view --json files` lists only intended paths. The merge is the user's call.

## Self-review (writing-plans checklist, done)

- **Spec coverage:** §3.1 → T4 (+ handler flag check T15); §3.2 → T3 (+ rollback in T16 docs); §3.3 → T2, T3; §3.4 → T7, T8 (+ public-URL note T15); §3.5 → T9, T13; §3.6 → T9; §3.7 → T7, T10; §3.8 → T1 (+ deploy note in the PR); §3.9 → T2, T10, T12; §3.10 → T12; §3.11 → T5, T11; §3.12 → T14, T15; §3.13 → T15, T16; §3.14 → T7 (prefix), T15 (tool text); §7 tests → spread across tasks, e2e T17.
- **Types used across tasks:** `transition_node(node_id, *, from_statuses, dag_statuses=None, card=None, due_by=None, **values) -> bool`; `apply_retry(dag_id, primary, unblocks, reactivate) -> bool`; `get_node_with_dag_status(node_id) -> tuple[DAGNode, str] | None`; `awaiting_input_nodes_in_terminal_dags(limit) -> list[DAGNode]`; `answer_node(node_id, option_id, *, source, actor, surface_id=None) -> AnswerResult`; `retry_node(dag_id, node_name, *, allow_declined=False)`; `held_reason(dag_id) -> str | None`; `push_built(..., notify_text=None, reserved_key_ok=False)`; `ReservedDedupKeyError(ValueError)`; `close(surface_id, status="expired")`; `close_by_dedup_key(key, status="expired") -> list[str]`; `live_ids(ids) -> set[str]`; `live_cards_by_prefix(prefix) -> list[tuple[str, str]]`; `register_dag_tools(dispatcher, store, orchestrator, settings=None)`.
- **v1.2 (late spec re-reviews, spec v2.4):** the approved draft reaches the acting node (T9); the launch `running` writes and the F064.2 demotion are conditional, and a lost launch cancels what it created (T2); the node-driven sweep query + `idx_dag_nodes_awaiting_input` (T3, T10); the sweep reads every live card (T7, T10); `push_built`'s two retries forward the flags, with `ReservedDedupKeyError` and an AST guard (T7); adopt a live card before re-pushing (T9); the gate exempts nodes that cost nothing and reports `held_reason` (T11, T15).
- **Known seams to watch while executing:** the nested `EXISTS` correlation in `parked_clause` (T5 tests are the check); `tick()` now runs `_sweep_leaked_approval_cards` every tick (one small query on `a2ui_surfaces`); `_cancel_one` now tears down an `awaiting_check` node's heartbeat check on the budget path (previously left to the reconciliation sweep).
