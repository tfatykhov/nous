# Harness Autonomy — Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give every tool call a known execution context, refuse tool calls the model was not offered, and persist every side-effecting tool call as a durable ledger row that exists before the side effect happens.

**Architecture:** A frozen `ExecutionContext` value (new module) is built by each turn's caller and threaded `run_turn → _tool_loop → ToolDispatcher.dispatch`; `stream_chat` builds an `interactive` one. One runner method `_authorize_tool_call` refuses names outside the iteration's offered set, in both loops. A `LedgerStore` (new module) writes `nous_system.execution_ledger` rows — `pending` before dispatch, closed after — for tools whose side-effect class is not `none`.

**Tech Stack:** Python 3.12+, SQLAlchemy 2 async ORM, asyncpg, PostgreSQL 17, pytest + pytest-asyncio.

**Spec:** `docs/superpowers/plans/2026-09-24-harness-autonomy-roadmap.md` (§1 findings 1–3, §3 PRs 1a/1b).

## Global Constraints

- Two PRs, in order: **PR 1a** = Tasks 1–5, **PR 1b** = Tasks 6–10. PR 1b branches from `main` after PR 1a merges.
- Migration number is **074** (`073` is taken on unmerged branch `fix/retire-stale-calibration-factor`).
- No `;` inside `--` SQL comments (the migrator splits on `;`); CI applies every migration with `psql -f` to a fresh Postgres.
- Every new table has `agent_id`; every sweep is agent-scoped.
- New settings use pydantic `Field(default=..., validation_alias="NOUS_...")` in `nous/config.py` and get a row in the CLAUDE.md env-var table.
- Kill switches default ON for safety fixes and additive telemetry (Phase 1 is both); nothing in Phase 1 changes which tools a context is *offered*.
- CI (Postgres) is the gate. Locally, `uv run pytest` runs on SQLite; eight tests in `test_tools.py`/`test_brain.py` fail on `main` for pgvector reasons and are not regressions.

---

## File Structure

| File | PR | Responsibility |
|---|---|---|
| `nous/api/execution_context.py` (create) | 1a | `ExecutionContext`, `ContextKind`, `CONTEXT_KINDS`, `resolve_context` |
| `nous/api/runner.py` (modify) | 1a, 1b | thread context; `_authorize_tool_call`; ledger open/close around dispatch |
| `nous/api/tools.py` (modify) | 1a | `ToolDispatcher.dispatch(..., context=)`; inline `spawn_task` passes context |
| `nous/handlers/subtask_worker.py`, `nous/handlers/subtask_executor.py` (modify) | 1a | pass `ExecutionContext.for_subtask` |
| `nous/heartbeat/runner.py`, `nous/heartbeat/dynamic.py`, `nous/dag/delivery.py` (modify) | 1a | pass heartbeat/dag_summary contexts |
| `nous/config.py` (modify) | 1a, 1b | new settings |
| `nous/cognitive/execution_ledger.py` (modify) | 1b | hoist `summarize_args` to module level |
| `nous/cognitive/ledger_store.py` (create) | 1b | `LedgerStore` — durable rows, orphan sweep, retention |
| `nous/storage/models.py` (modify) | 1b | `ExecutionLedgerEntry` ORM |
| `sql/migrations/074_execution_ledger.sql` (create) | 1b | table + indexes |
| `nous/main.py` (modify) | 1b | wire store, startup orphan sweep, daily retention |
| `CLAUDE.md` (modify) | 1a, 1b | env-var rows |
| `tests/test_execution_context.py`, `tests/test_runner_authorization.py`, `tests/test_ledger_store.py`, `tests/test_runner_ledger.py` (create) | 1a, 1b | tests |
| `tests/test_runner.py`, `tests/test_runner_background.py` (modify) | 1a | mock `dispatch` signatures accept `context` |

---

# PR 1a — ExecutionContext + offered-set enforcement

Branch: `feat/harness-phase1a-execution-context` from `origin/main`.

### Task 1: `ExecutionContext` value type

**Files:**
- Create: `nous/api/execution_context.py`
- Test: `tests/test_execution_context.py`

**Interfaces:**
- Produces: `ExecutionContext(kind, session_id=None, subtask_id=None, dag_id=None, dag_node_id=None, dag_node_name=None, schedule_id=None, surface_id=None)` (frozen); `.is_background -> bool`; `ExecutionContext.for_subtask(subtask, session_id) -> ExecutionContext`; `resolve_context(context, *, is_background, session_id) -> ExecutionContext`; `CONTEXT_KINDS: tuple[str, ...]`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_execution_context.py
"""Roadmap Phase 1a: every turn knows which harness path runs it."""

import uuid
from types import SimpleNamespace

import pytest

from nous.api.execution_context import (
    CONTEXT_KINDS,
    ExecutionContext,
    resolve_context,
)


def _subtask(*, metadata=None, dag_node_id=None, sid=None):
    return SimpleNamespace(
        id=sid or uuid.uuid4(),
        metadata_=metadata if metadata is not None else {},
        dag_node_id=dag_node_id,
    )


def test_only_interactive_is_foreground():
    assert ExecutionContext(kind="interactive").is_background is False
    for kind in CONTEXT_KINDS:
        if kind != "interactive":
            assert ExecutionContext(kind=kind).is_background is True


def test_unknown_kind_is_rejected():
    with pytest.raises(ValueError, match="unknown execution context kind"):
        ExecutionContext(kind="daemon")  # type: ignore[arg-type]


def test_context_is_immutable():
    ctx = ExecutionContext(kind="subtask")
    with pytest.raises(AttributeError):
        ctx.kind = "interactive"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("metadata", "has_node", "expected"),
    [
        ({"dag_id": str(uuid.uuid4()), "node_name": "fetch"}, True, "dag_node"),
        ({"dag_id": str(uuid.uuid4()), "node_name": "fetch"}, False, "dag_node"),
        ({"a2ui_surface_id": "s1", "a2ui_action_id": "rebalance"}, False, "agent_action"),
        ({"schedule_id": "ab12"}, False, "scheduled"),
        ({}, False, "subtask"),
    ],
)
def test_for_subtask_derives_kind_from_the_row(metadata, has_node, expected):
    node_id = uuid.uuid4() if has_node else None
    ctx = ExecutionContext.for_subtask(_subtask(metadata=metadata, dag_node_id=node_id), "subtask-1")
    assert ctx.kind == expected
    assert ctx.session_id == "subtask-1"


def test_for_subtask_carries_ids():
    dag_id, node_id, sid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    ctx = ExecutionContext.for_subtask(
        _subtask(metadata={"dag_id": str(dag_id), "node_name": "send"}, dag_node_id=node_id, sid=sid),
        "subtask-x",
    )
    assert (ctx.subtask_id, ctx.dag_id, ctx.dag_node_id, ctx.dag_node_name) == (sid, dag_id, node_id, "send")


def test_for_subtask_tolerates_legacy_rows():
    """Test mocks and pre-F064.5 rows have no metadata_ / a non-dict one."""
    row = SimpleNamespace(id=uuid.uuid4())
    assert ExecutionContext.for_subtask(row, "s").kind == "subtask"
    row = SimpleNamespace(id=uuid.uuid4(), metadata_=None, dag_node_id=None)
    assert ExecutionContext.for_subtask(row, "s").kind == "subtask"
    bad = _subtask(metadata={"dag_id": "not-a-uuid"})
    ctx = ExecutionContext.for_subtask(bad, "s")
    assert ctx.kind == "dag_node" and ctx.dag_id is None


def test_resolve_context_defaults():
    assert resolve_context(None, is_background=False, session_id="s").kind == "interactive"
    bg = resolve_context(None, is_background=True, session_id="s")
    assert bg.kind == "background" and bg.session_id == "s"


def test_resolve_context_passes_an_explicit_context_through():
    ctx = ExecutionContext(kind="heartbeat_triage", session_id="h")
    assert resolve_context(ctx, is_background=True, session_id="other") is ctx
    # is_background=False with a background context: the context wins.
    assert resolve_context(ctx, is_background=False, session_id="h") is ctx


def test_resolve_context_rejects_a_contradiction():
    with pytest.raises(ValueError, match="contradicts"):
        resolve_context(ExecutionContext(kind="interactive"), is_background=True, session_id="s")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_execution_context.py -q`
Expected: collection error `ModuleNotFoundError: No module named 'nous.api.execution_context'`.

- [ ] **Step 3: Implement the module**

```python
# nous/api/execution_context.py
"""Which harness path is running a turn (harness-autonomy roadmap, Phase 1a).

Before this module the only context signal reaching tool dispatch was
``is_background: bool`` — heartbeat triage, dynamic checks, callbacks,
schedules, DAG nodes, companion agent actions and spawn_task all collapsed to
the same value. A capability policy (roadmap Phase 2a), an idempotency key
(Phase 2b) and the durable ledger (Phase 1b) each need to know WHICH path is
running, so the caller that starts a turn says so once, here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, get_args
from uuid import UUID

ContextKind = Literal[
    "interactive",         # REST /chat, /chat/stream, MCP — a person is in the loop
    "subtask",             # spawn_task / spawn_sync (worker or inline)
    "dag_node",            # a subtask launched by the DAG orchestrator
    "scheduled",           # a schedule_task fire
    "agent_action",        # F092.2 companion app.act tap
    "heartbeat_triage",    # heartbeat cognitive triage of findings
    "heartbeat_check",     # F034.5 DynamicCheck run
    "heartbeat_callback",  # F034.6 on_complete callback
    "dag_summary",         # F087 agent-authored DAG summary
    "background",          # a background turn whose caller named no kind
]
CONTEXT_KINDS: tuple[str, ...] = get_args(ContextKind)


def _as_uuid(value: Any) -> UUID | None:
    if value is None or isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True, slots=True)
class ExecutionContext:
    """Immutable description of the harness path running a turn."""

    kind: ContextKind
    session_id: str | None = None
    subtask_id: UUID | None = None
    dag_id: UUID | None = None
    dag_node_id: UUID | None = None
    dag_node_name: str | None = None
    schedule_id: str | None = None
    surface_id: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in CONTEXT_KINDS:
            raise ValueError(f"unknown execution context kind {self.kind!r}")

    @property
    def is_background(self) -> bool:
        """Every kind except ``interactive`` runs with nobody in the loop."""
        return self.kind != "interactive"

    @classmethod
    def for_subtask(cls, subtask: Any, session_id: str) -> ExecutionContext:
        """Derive the context of a subtask turn from its ``heart.subtasks`` row.

        The row already says who created it: the DAG orchestrator stamps
        ``dag_node_id`` + ``metadata.dag_id``/``node_name``; companion agent
        actions stamp ``metadata.a2ui_surface_id``; the scheduler stamps
        ``metadata.schedule_id``. A row with none of them is a plain spawn.
        ``getattr`` defaults keep SimpleNamespace test doubles working.
        """
        meta = getattr(subtask, "metadata_", None)
        if not isinstance(meta, dict):
            meta = {}
        dag_node_id = _as_uuid(getattr(subtask, "dag_node_id", None))
        kind: ContextKind
        if dag_node_id is not None or meta.get("dag_id"):
            kind = "dag_node"
        elif meta.get("a2ui_surface_id"):
            kind = "agent_action"
        elif meta.get("schedule_id"):
            kind = "scheduled"
        else:
            kind = "subtask"
        return cls(
            kind=kind,
            session_id=session_id,
            subtask_id=_as_uuid(getattr(subtask, "id", None)),
            dag_id=_as_uuid(meta.get("dag_id")),
            dag_node_id=dag_node_id,
            dag_node_name=meta.get("node_name") or None,
            schedule_id=meta.get("schedule_id") or None,
            surface_id=meta.get("a2ui_surface_id") or None,
        )


def resolve_context(
    context: ExecutionContext | None,
    *,
    is_background: bool,
    session_id: str | None,
) -> ExecutionContext:
    """The context a turn actually runs under.

    An explicit context wins. Callers that only pass the legacy
    ``is_background`` flag get ``interactive`` or the generic ``background``
    kind. ``is_background=True`` with an explicitly INTERACTIVE context is a
    programming error — the two would disagree about who is in the loop.
    """
    if context is None:
        return ExecutionContext(
            kind="background" if is_background else "interactive",
            session_id=session_id,
        )
    if is_background and not context.is_background:
        raise ValueError(
            "is_background=True contradicts an interactive ExecutionContext"
        )
    return context
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_execution_context.py -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add nous/api/execution_context.py tests/test_execution_context.py
git commit -m "feat(runner): ExecutionContext value type (harness Phase 1a)"
```

### Task 2: Thread the context through `run_turn`, `_tool_loop`, `stream_chat` and `dispatch`

**Files:**
- Modify: `nous/api/runner.py` — `run_turn` signature (~line 330-360) and its `_tool_loop(...)` call (~line 595); `_tool_loop` signature (~line 1655) and its dispatch call (~line 1989); `stream_chat` (~line 1021, dispatch call ~line 1497); `_dispatch_with_keepalive` (~line 2711)
- Modify: `nous/api/tools.py` — `ToolDispatcher.dispatch` (~line 370-442)
- Modify: `tests/test_runner.py`, `tests/test_runner_background.py` — the three hand-written `dispatch` doubles
- Test: `tests/test_runner_authorization.py` (create; context-threading tests here, enforcement tests in Task 3)

**Interfaces:**
- Consumes: `ExecutionContext`, `resolve_context` (Task 1).
- Produces: `AgentRunner.run_turn(..., context: ExecutionContext | None = None)`; `AgentRunner._tool_loop(..., context: ExecutionContext | None = None)`; `ToolDispatcher.dispatch(name, args, session_id=None, is_background=False, turn_number=None, context: ExecutionContext | None = None)`. Inside `_tool_loop` the resolved context is the local `ctx`; in `stream_chat` it is `_ctx`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_runner_authorization.py
"""Roadmap Phase 1a: context threading + offered-set enforcement."""

from __future__ import annotations

import pytest

from nous.api.execution_context import ExecutionContext
from nous.api.models import ApiResponse
from nous.api.runner import AgentRunner, Conversation, Message
from nous.config import Settings
from tests.test_runner_background import _MockBrain, _MockCognitive, _MockHeart


def _settings(**overrides) -> Settings:
    return Settings(ANTHROPIC_API_KEY="test-key", agent_id="test-agent", **overrides)


class _RecordingDispatcher:
    """Offers ``offered``; records every dispatch (name, context)."""

    def __init__(self, offered: list[str], registered: list[str] | None = None) -> None:
        self.offered = offered
        self.registered = set(registered or offered)
        self.calls: list[tuple[str, ExecutionContext | None, bool]] = []

    def available_tools(self, frame_id):
        return [
            {"name": n, "description": n, "input_schema": {"type": "object"}}
            for n in self.offered
        ]

    async def dispatch(self, name, inp, session_id=None, is_background=False,
                       turn_number=None, context=None):
        self.calls.append((name, context, is_background))
        return f"{name} ran", False


def _one_tool_call_then_done(tool_name: str):
    calls = {"n": 0}

    async def fake_call_api(system_prompt, messages, tools=None, skip_thinking=False,
                            model_override=None, is_background=False):
        calls["n"] += 1
        if calls["n"] == 1:
            return ApiResponse(
                content=[{"type": "tool_use", "id": "t1", "name": tool_name, "input": {}}],
                stop_reason="tool_use",
            )
        return ApiResponse(content=[{"type": "text", "text": "done"}], stop_reason="end_turn")

    return fake_call_api


async def _run_loop(runner: AgentRunner, **kwargs):
    conv = Conversation(session_id="s1")
    conv.messages.append(Message(role="user", content="go"))
    try:
        return await runner._tool_loop(
            system_prompt="sys", conversation=conv, frame_id="conversation",
            session_id="s1", **kwargs,
        )
    finally:
        runner._api_shared = True
        await runner.close()


@pytest.mark.asyncio
async def test_tool_loop_passes_the_explicit_context_to_dispatch():
    r = AgentRunner(_MockCognitive(), _MockBrain(), _MockHeart(), _settings())
    d = _RecordingDispatcher(["recall_deep"])
    r.set_dispatcher(d)
    r._call_api = _one_tool_call_then_done("recall_deep")
    ctx = ExecutionContext(kind="heartbeat_triage", session_id="s1")
    await _run_loop(r, is_background=True, context=ctx)
    assert d.calls == [("recall_deep", ctx, True)]


@pytest.mark.asyncio
async def test_tool_loop_without_context_resolves_a_generic_one():
    r = AgentRunner(_MockCognitive(), _MockBrain(), _MockHeart(), _settings())
    d = _RecordingDispatcher(["recall_deep"])
    r.set_dispatcher(d)
    r._call_api = _one_tool_call_then_done("recall_deep")
    await _run_loop(r, is_background=True)
    (_name, ctx, is_bg), = d.calls
    assert ctx.kind == "background" and ctx.session_id == "s1" and is_bg is True


@pytest.mark.asyncio
async def test_background_context_makes_the_loop_background():
    """A caller passing a background context without is_background still
    runs as background (F048 streaming + _is_background injection agree)."""
    r = AgentRunner(_MockCognitive(), _MockBrain(), _MockHeart(), _settings())
    d = _RecordingDispatcher(["recall_deep"])
    r.set_dispatcher(d)
    r._call_api = _one_tool_call_then_done("recall_deep")
    await _run_loop(r, context=ExecutionContext(kind="dag_summary", session_id="s1"))
    assert d.calls[0][2] is True


@pytest.mark.asyncio
async def test_dispatcher_derives_is_background_from_context(tools_dispatcher_with_probe):
    dispatcher, seen = tools_dispatcher_with_probe
    await dispatcher.dispatch(
        "probe", {}, context=ExecutionContext(kind="scheduled", session_id="x"),
    )
    assert seen == [True]
    await dispatcher.dispatch("probe", {}, context=ExecutionContext(kind="interactive"))
    assert seen == [True, False]


@pytest.fixture
def tools_dispatcher_with_probe():
    """A real ToolDispatcher with one tool that reads the injected flag."""
    from nous.api.tools import ToolDispatcher

    seen: list[bool] = []
    dispatcher = ToolDispatcher()

    async def probe(_is_background: bool = False):
        seen.append(_is_background)
        return {"content": [{"type": "text", "text": "ok"}]}

    dispatcher.register(
        "probe", probe, {"name": "probe", "description": "p", "input_schema": {"type": "object"}},
    )
    dispatcher._BACKGROUND_AWARE_TOOLS = dispatcher._BACKGROUND_AWARE_TOOLS | {"probe"}
    return dispatcher, seen
```

> Note for the implementer: `ToolDispatcher()` takes keyword-only settings in this repo — copy
> the constructor call used in `tests/test_tools.py::TestToolDispatcher` (or wherever the
> dispatcher is built in tests) if the bare call above does not match. The probe relies on
> Step 3's `_BACKGROUND_AWARE_TOOLS` class attribute.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_runner_authorization.py -q`
Expected: FAIL — `_tool_loop() got an unexpected keyword argument 'context'` / `dispatch() got an unexpected keyword argument 'context'` / `AttributeError: _BACKGROUND_AWARE_TOOLS`.

- [ ] **Step 3: Implement**

In `nous/api/tools.py`, replace the literal tuple at the `_is_background` injection with a class attribute, add the parameter, and resolve the context once:

```python
# ToolDispatcher class body (next to the other class-level constants)
    # Tools that read the injected ``_is_background`` flag (#541/#642
    # decision resolution; F092.1 compose_surface origin). One set, so a
    # test double or a new consumer does not need a second literal.
    _BACKGROUND_AWARE_TOOLS: frozenset[str] = frozenset(
        {"resolve_decision", "resolve_decisions", "compose_surface"}
    )
```

```python
    async def dispatch(
        self, name: str, args: dict[str, Any], session_id: str | None = None,
        is_background: bool = False, turn_number: int | None = None,
        context: ExecutionContext | None = None,
    ) -> tuple[str, bool]:
        """Dispatch a tool call and return (result_text, is_error).

        ...(keep the existing docstring lines)...

        context: the turn's ExecutionContext (harness Phase 1a). When given it
        is authoritative: ``is_background`` is derived from it.
        """
        ctx = resolve_context(context, is_background=is_background, session_id=session_id)
        is_background = ctx.is_background
        handler = self._handlers.get(name)
        ...
```

```python
            if name in self._BACKGROUND_AWARE_TOOLS:
                # compose_surface derives origin from it: a heartbeat or
                # scheduled turn composes origin="agent" apps (F092.1 push
                # path); a chat turn composes origin="chat".
                args = {**args, "_is_background": is_background}
```

Add the import at the top of `nous/api/tools.py`:

```python
from nous.api.execution_context import ExecutionContext, resolve_context
```

In `nous/api/runner.py` add the import:

```python
from nous.api.execution_context import ExecutionContext, resolve_context
```

`run_turn`: add the parameter after `dag_node_id`:

```python
        dag_node_id: UUID | None = None,
        # Harness Phase 1a: which path runs this turn. Callers that start a
        # background turn pass one; chat/MCP leave it None (interactive).
        context: ExecutionContext | None = None,
```

At the very top of the `run_turn` body (before `is_background` is first read at the
`self._session_monitor.touch(...)` call):

```python
        _ctx = resolve_context(context, is_background=is_background, session_id=session_id)
        is_background = _ctx.is_background
```

and in the `self._tool_loop(...)` call inside `run_turn`, next to `dag_node_id=dag_node_id,` add:

```python
                            context=_ctx,
```

`_tool_loop`: add the parameter after `turn_number`:

```python
        turn_number: int | None = None,
        context: ExecutionContext | None = None,  # harness Phase 1a
```

first lines of the body, right after the dispatcher check:

```python
        ctx = resolve_context(context, is_background=is_background, session_id=session_id)
        is_background = ctx.is_background
```

and the dispatch call:

```python
                                result_text, is_error = await self._dispatcher.dispatch(
                                    tool_name, tool_input, session_id=session_id,
                                    is_background=is_background,
                                    turn_number=turn_number,  # F091 (caller-captured)
                                    context=ctx,
                                )
```

`stream_chat`: right after `_agent_id = agent_id or self._settings.agent_id` add

```python
        # stream_chat serves /chat/stream only — always a person in the loop.
        _ctx = ExecutionContext(kind="interactive", session_id=session_id)
```

and pass it through `_dispatch_with_keepalive`:

```python
                            async for item in self._dispatch_with_keepalive(
                                tc["name"], dispatch_input, session_id=session_id,
                                turn_number=_stream_turn_number,  # F091
                                context=_ctx,
                            ):
```

`_dispatch_with_keepalive`: add `context: ExecutionContext | None = None` to the signature and
`context=context,` to its inner `self._dispatcher.dispatch(...)` call.

Update the three hand-written doubles (`grep -n "def dispatch(" tests/test_runner.py tests/test_runner_background.py`) to accept the new keyword:

```python
        async def dispatch(self, name, inp, session_id=None, is_background=False,
                           turn_number=None, context=None):
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_runner_authorization.py tests/test_runner.py tests/test_runner_background.py tests/test_tools.py -q -k "not recall_deep and not learn_fact"`
Expected: the new tests pass; nothing previously green turns red.

- [ ] **Step 5: Commit**

```bash
git add nous/api/runner.py nous/api/tools.py tests/test_runner_authorization.py tests/test_runner.py tests/test_runner_background.py
git commit -m "feat(runner): thread ExecutionContext run_turn -> _tool_loop -> dispatch (harness Phase 1a)"
```

### Task 3: Refuse tool calls outside the offered set

**Files:**
- Modify: `nous/config.py` (next to `stable_tool_set_enabled`, ~line 974)
- Modify: `nous/api/runner.py` — new method `_authorize_tool_call`; `_tool_loop` per-iteration offered set + check (after the `input_error` branch ~line 1917); `stream_chat` offered set (after F078 stripping ~line 1175) + check (after its `input_error` branch ~line 1466)
- Test: `tests/test_runner_authorization.py`

**Interfaces:**
- Consumes: `ctx` / `_ctx` (Task 2).
- Produces: `AgentRunner._authorize_tool_call(ctx: ExecutionContext, tool_name: str, offered_names: frozenset[str]) -> str | None` — returns the refusal text or `None`. Phase 2a extends this same method with the policy table. Setting `tool_offered_set_enforcement_enabled`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_runner_authorization.py`)

```python
@pytest.mark.asyncio
async def test_unoffered_tool_is_refused_and_never_dispatched():
    """tool_filter offers recall_deep only; the model emits bash (registered,
    but not offered). Before Phase 1a bash would run."""
    r = AgentRunner(_MockCognitive(), _MockBrain(), _MockHeart(), _settings())
    d = _RecordingDispatcher(["recall_deep", "bash"])
    r.set_dispatcher(d)
    r._call_api = _one_tool_call_then_done("bash")
    _text, results, _usage, _thinking = await _run_loop(
        r, is_background=True, tool_filter=["recall_deep"],
    )
    assert d.calls == []
    (res,) = results
    assert res.tool_name == "bash" and "not available in this turn" in res.error


@pytest.mark.asyncio
async def test_subtask_exclusions_are_enforced_not_just_hidden():
    r = AgentRunner(_MockCognitive(), _MockBrain(), _MockHeart(), _settings())
    d = _RecordingDispatcher(["spawn_task", "recall_deep"])
    r.set_dispatcher(d)
    r._call_api = _one_tool_call_then_done("spawn_task")
    await _run_loop(r, is_background=True, is_subtask=True)
    assert d.calls == []


@pytest.mark.asyncio
async def test_offered_tool_still_runs():
    r = AgentRunner(_MockCognitive(), _MockBrain(), _MockHeart(), _settings())
    d = _RecordingDispatcher(["recall_deep"])
    r.set_dispatcher(d)
    r._call_api = _one_tool_call_then_done("recall_deep")
    await _run_loop(r)
    assert [c[0] for c in d.calls] == ["recall_deep"]


@pytest.mark.asyncio
async def test_extra_tools_count_as_offered():
    """submit_final_report arrives via extra_tools, never the dispatcher."""
    r = AgentRunner(_MockCognitive(), _MockBrain(), _MockHeart(), _settings())
    d = _RecordingDispatcher(["recall_deep"])
    r.set_dispatcher(d)
    r._call_api = _one_tool_call_then_done("submit_final_report")
    ran = []

    async def _submit(**_):
        ran.append(True)
        return "report accepted", False

    schema = {"name": "submit_final_report", "description": "s", "input_schema": {"type": "object"}}
    await _run_loop(r, is_background=True, extra_tools={"submit_final_report": (schema, _submit)})
    assert ran == [True]


@pytest.mark.asyncio
async def test_enforcement_kill_switch():
    r = AgentRunner(
        _MockCognitive(), _MockBrain(), _MockHeart(),
        _settings(tool_offered_set_enforcement_enabled=False),
    )
    d = _RecordingDispatcher(["recall_deep", "bash"])
    r.set_dispatcher(d)
    r._call_api = _one_tool_call_then_done("bash")
    await _run_loop(r, is_background=True, tool_filter=["recall_deep"])
    assert [c[0] for c in d.calls] == ["bash"]


@pytest.mark.asyncio
async def test_refusal_is_recorded_in_the_session_ledger():
    from nous.cognitive.execution_ledger import ExecutionLedger

    r = AgentRunner(_MockCognitive(), _MockBrain(), _MockHeart(), _settings())
    d = _RecordingDispatcher(["recall_deep", "bash"])
    r.set_dispatcher(d)
    r._call_api = _one_tool_call_then_done("bash")
    ledger = ExecutionLedger(session_id="s1")
    await _run_loop(r, is_background=True, tool_filter=["recall_deep"], ledger=ledger)
    assert [(a.tool_name, a.status) for a in ledger.actions] == [("bash", "blocked")]
```

Streaming path — append:

```python
@pytest.mark.asyncio
async def test_stream_chat_refuses_an_unoffered_tool(monkeypatch):
    """F078 refuse strips bash from the streaming tool list; the model emits it anyway."""
    from nous.api.runner import StreamEvent  # noqa: F401 — import check

    r = AgentRunner(_MockCognitive(), _MockBrain(), _MockHeart(), _settings())
    d = _RecordingDispatcher(["recall_deep"], registered=["recall_deep", "bash"])
    r.set_dispatcher(d)
    names = await _stream_one_tool_call(r, "bash")
    assert d.calls == []
    assert "bash" in names  # a paired tool_result was still emitted (tool_end event)
```

> Implementer: `_stream_one_tool_call(runner, tool_name) -> list[str]` must drive `stream_chat`
> through one streamed `tool_use` for `tool_name` then an `end_turn`, returning the tool names
> seen in `tool_end` events. Build it from the existing streaming fixtures in
> `tests/test_runner.py` (search `stream_chat` there for the fake `_api.stream` generator);
> copy that generator verbatim rather than inventing a new event shape.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_runner_authorization.py -q`
Expected: the refusal tests FAIL (bash / spawn_task reach `d.calls`); `tool_offered_set_enforcement_enabled` rejected as an unknown setting.

- [ ] **Step 3: Implement**

`nous/config.py`, right after `stable_tool_set_enabled`:

```python
    # Harness Phase 1a: a tool call executes only if its name was OFFERED to
    # the model this iteration. Every per-context restriction (subtask
    # exclusions, tool_filter, F078 refuse) edits the schema list; before this
    # the dispatcher resolved any registered name, so a model naming a tool it
    # had seen elsewhere ran it anyway. Kill switch only.
    tool_offered_set_enforcement_enabled: bool = Field(
        default=True, validation_alias="NOUS_TOOL_OFFERED_SET_ENFORCEMENT_ENABLED"
    )
```

`nous/api/runner.py`, new method on `AgentRunner` (place it next to `_log_f026_decision`):

```python
    def _authorize_tool_call(
        self, ctx: ExecutionContext, tool_name: str, offered_names: frozenset[str],
    ) -> str | None:
        """Return a refusal for a call the harness must not execute, else None.

        Harness Phase 1a: a tool the model was not OFFERED this iteration never
        runs. The single choke point both loops call before gating/dispatch —
        Phase 2a adds the capability policy here, so there is one place that
        decides whether a call may execute.
        """
        if not self._settings.tool_offered_set_enforcement_enabled:
            return None
        if tool_name in offered_names:
            return None
        logger.warning(
            "Harness: refused unoffered tool call %r (context=%s, session=%s)",
            tool_name, ctx.kind, ctx.session_id,
        )
        return (
            f"Tool error: '{tool_name}' is not available in this turn. "
            "Use only the tools offered to you."
        )
```

`_tool_loop`: right after the per-iteration `tools` list is complete (after the `extra_tools`
append loop), add

```python
            offered_names = frozenset(t["name"] for t in tools)
```

and in the per-call loop, immediately after the `input_error` branch's `continue`:

```python
                    refusal = self._authorize_tool_call(ctx, tool_name, offered_names)
                    if refusal is not None:
                        tool_results_for_message.append({
                            "type": "tool_result",
                            "tool_use_id": tool_use_id,
                            "content": refusal,
                            "is_error": True,
                        })
                        all_tool_results.append(ToolResult(
                            tool_name=tool_name,
                            arguments=tool_input,
                            result=None,
                            error=refusal,
                            duration_ms=0,
                        ))
                        if ledger:
                            ledger.record(tool_name, tool_input, refusal, "blocked")
                        continue
```

`stream_chat`: after the F078 stripping block (right before `messages = self._format_messages(conversation)`):

```python
            offered_names = frozenset(t["name"] for t in (tools or []))
```

and in its per-call loop, immediately after the `input_error` branch's `continue`:

```python
                        refusal = self._authorize_tool_call(_ctx, tc["name"], offered_names)
                        if refusal is not None:
                            tool_results_for_message.append({
                                "type": "tool_result",
                                "tool_use_id": tc["id"],
                                "content": refusal,
                                "is_error": True,
                            })
                            all_tool_results.append(ToolResult(
                                tool_name=tc["name"],
                                arguments=tc.get("input", {}),
                                result=None,
                                error=refusal,
                                duration_ms=0,
                            ))
                            if ledger:
                                ledger.record(tc["name"], tc.get("input", {}), refusal, "blocked")
                            yield StreamEvent(type="tool_end", tool_name=tc["name"])
                            continue
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_runner_authorization.py tests/test_runner.py tests/test_runner_background.py -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add nous/config.py nous/api/runner.py tests/test_runner_authorization.py
git commit -m "fix(runner): refuse tool calls outside the offered set, in both loops (harness Phase 1a)"
```

### Task 4: Every background caller names its context

**Files:**
- Modify: `nous/handlers/subtask_worker.py` (~line 327), `nous/handlers/subtask_executor.py` (~line 253), `nous/api/tools.py` inline legacy `spawn_task` (~line 3143), `nous/heartbeat/runner.py` triage (~line 562) and callback (~line 643), `nous/heartbeat/dynamic.py` (~line 133), `nous/dag/delivery.py` (~line 277)
- Test: `tests/test_runner_authorization.py` (caller wiring), plus the existing caller tests that assert `run_turn` kwargs

**Interfaces:**
- Consumes: `ExecutionContext`, `ExecutionContext.for_subtask` (Task 1); `run_turn(..., context=)` (Task 2).

- [ ] **Step 1: Write the failing tests** (append)

```python
@pytest.mark.parametrize(
    ("module_path", "expected_kind"),
    [
        ("nous/heartbeat/runner.py", "heartbeat_triage"),
        ("nous/heartbeat/runner.py", "heartbeat_callback"),
        ("nous/heartbeat/dynamic.py", "heartbeat_check"),
        ("nous/dag/delivery.py", "dag_summary"),
    ],
)
def test_background_callers_name_their_context(module_path, expected_kind):
    """Structural guard: each background run_turn call site passes a context
    of its kind. A new caller that forgets resolves to the generic
    'background' kind — the most restricted one once Phase 2a lands."""
    from pathlib import Path

    src = Path(module_path).read_text(encoding="utf-8")
    assert f'kind="{expected_kind}"' in src


def test_every_run_turn_call_passes_a_context_or_is_interactive():
    """AST sweep: every production run_turn(...) call either passes context=
    or is one of the known interactive entry points (REST /chat, MCP)."""
    import ast
    from pathlib import Path

    interactive_ok = {"nous/api/rest.py", "nous/api/mcp.py"}
    offenders = []
    for path in Path("nous").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "run_turn"
                and not any(k.arg == "context" for k in node.keywords)
                and path.as_posix() not in interactive_ok
            ):
                offenders.append(f"{path.as_posix()}:{node.lineno}")
    assert offenders == [], offenders
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_runner_authorization.py -q -k "callers or every_run_turn"`
Expected: FAIL listing the seven call sites.

- [ ] **Step 3: Implement** — add one `context=` keyword at each call (keep every existing keyword):

`nous/handlers/subtask_worker.py` (legacy path):

```python
                is_background=True,
                dag_node_id=_dag_node_id,
                context=ExecutionContext.for_subtask(subtask, session_id),
```

`nous/handlers/subtask_executor.py` (`execute_hardened`):

```python
                    dag_node_id=getattr(subtask, "dag_node_id", None),
                    context=ExecutionContext.for_subtask(subtask, session_id),
```

`nous/api/tools.py` (inline legacy `spawn_task`, the `runner.run_turn(` inside `_asyncio.wait_for`):

```python
                        is_background=True,
                        context=ExecutionContext.for_subtask(subtask, subtask_session_id),
```

`nous/heartbeat/runner.py` triage:

```python
                is_background=True,
                context=ExecutionContext(kind="heartbeat_triage", session_id=session_id),
```

`nous/heartbeat/runner.py` on_complete callback:

```python
                    is_background=True,
                    context=ExecutionContext(kind="heartbeat_callback", session_id=session_id),
```

`nous/heartbeat/dynamic.py`:

```python
                is_background=True,
                context=ExecutionContext(kind="heartbeat_check", session_id=session_id),
```

`nous/dag/delivery.py`:

```python
                    is_background=True,
                    context=ExecutionContext(
                        kind="dag_summary",
                        session_id=f"dag-summary-{dag.id.hex[:8]}",
                        dag_id=dag.id,
                    ),
```

Each file adds `from nous.api.execution_context import ExecutionContext`. For
`subtask_executor.py` and `nous/api/tools.py` check for import cycles
(`subtask_executor` already late-imports from `tools.py`); `execution_context.py` imports
nothing from `nous`, so a top-level import is safe everywhere.

Any existing test that asserts the exact `run_turn` kwargs of these callers (search
`tests/` for `run_turn.assert_awaited_with` / `call_args.kwargs`) gains `context=ANY` or an
explicit expected context.

- [ ] **Step 4: Run**

Run: `uv run pytest tests/test_runner_authorization.py tests/ -q -k "heartbeat or dynamic or delivery or subtask or spawn or authorization"`
Expected: new tests pass; no previously-green test fails (compare against the `main` baseline for SQLite-only failures).

- [ ] **Step 5: Commit**

```bash
git add nous/handlers/subtask_worker.py nous/handlers/subtask_executor.py nous/api/tools.py nous/heartbeat/runner.py nous/heartbeat/dynamic.py nous/dag/delivery.py tests/
git commit -m "feat(runner): every background caller names its ExecutionContext (harness Phase 1a)"
```

### Task 5: Docs + PR 1a

**Files:**
- Modify: `CLAUDE.md` env-var table — add `NOUS_TOOL_OFFERED_SET_ENFORCEMENT_ENABLED` next to `NOUS_STABLE_TOOL_SET_ENABLED`
- Add: this plan and the roadmap to the PR

- [ ] **Step 1:** Add the row:

```markdown
| `NOUS_TOOL_OFFERED_SET_ENFORCEMENT_ENABLED` | `true` | Harness Phase 1a: a tool call executes only if its name was OFFERED to the model in that iteration (both `_tool_loop` and `stream_chat`). Every per-context restriction — subtask exclusions (`spawn_task`/`schedule_task`/`spawn_sync`), `tool_filter` for dynamic checks and callbacks, F078 refuse stripping — edits only the schema list; before this the dispatcher resolved any registered name, so a model that named a tool it had seen in history or prompt text ran it anyway. A refused call gets a paired `tool_result` error and a `blocked` row in the session ledger. `extra_tools` (e.g. `submit_final_report`) count as offered. Kill switch only. |
```

- [ ] **Step 2:** Run the full touched suites, lint the changed files against `main`'s baseline (`ruff check` counts must not grow).
- [ ] **Step 3:** Commit, push, open PR 1a, `@codex review`, CI watch. Merge on codex-clean + green CI.

---

# PR 1b — Persisted execution ledger

Branch: `feat/harness-phase1b-execution-ledger` from `origin/main` **after PR 1a merges**.

Invariant: every side-effecting tool call leaves a durable row that exists before the side
effect and ends `success` / `error` / `blocked` / `unknown` — never silently `pending`.

Scope decisions:
- **Only side-effecting calls are persisted** (`classify_side_effect(...) != "none"`). Reads are
  high-volume and already covered by F091; this table answers "what did the agent DO".
- `extra_tools` (`submit_final_report`) are harness-internal and not persisted.
- Persistence is **fail-open** in Phase 1b: if the insert fails or times out, the call still runs
  and a WARNING is logged. Phase 2b makes keyed sends fail-closed on the ledger.
- A call cancelled mid-flight (subtask timeout, client disconnect) is closed `unknown` — its
  side effect may or may not have happened (orphaned SMTP threads, `F-P0.2`).
- A `pending` row can only outlive its dispatch if the process died or a close failed; the
  startup sweep marks every pending row created before this process started as `unknown`, and a
  daily sweep does the same for rows older than `NOUS_EXECUTION_LEDGER_PENDING_UNKNOWN_AFTER_SECONDS`.

### Task 6: Migration 074 + ORM model

**Files:**
- Create: `sql/migrations/074_execution_ledger.sql`
- Modify: `nous/storage/models.py` (append after `A2uiAction`)
- Test: `tests/test_ledger_store.py` (schema round-trip in Task 7)

**Interfaces:**
- Produces: table `nous_system.execution_ledger`; ORM `ExecutionLedgerEntry`; `LEDGER_STATUSES = ("pending", "success", "error", "blocked", "unknown")`, `LEDGER_SIDE_EFFECTS = ("write", "external", "irreversible")` exported from `nous/storage/models.py`.

- [ ] **Step 1: Write the migration**

```sql
-- 074: Persisted execution ledger (harness-autonomy roadmap Phase 1b)
--
-- The F026 ExecutionLedger is in-memory and session-scoped: it is dropped at
-- end_conversation, on eviction and on restart, and its entries have no ids.
-- This table is the durable record of what the agent DID. One row per
-- side-effecting tool call (reads are not recorded here - F091 covers them).
--
-- Lifecycle: the runner inserts the row as 'pending' BEFORE dispatching the
-- tool and closes it after. A call cancelled mid-flight is closed 'unknown'
-- because its side effect may or may not have happened. A row left 'pending'
-- by a dead process is swept to 'unknown' at the next startup.
--
-- idempotency_key and external_ref are reserved for Phase 2b (keyed sends and
-- provider message ids). Nothing populates them yet.

CREATE TABLE IF NOT EXISTS nous_system.execution_ledger (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id          VARCHAR(100) NOT NULL,
    session_id        VARCHAR(200),
    context_kind      VARCHAR(32)  NOT NULL,
    subtask_id        UUID,
    dag_id            UUID,
    dag_node_id       UUID,
    turn              INTEGER,
    tool_name         VARCHAR(100) NOT NULL,
    side_effect_type  VARCHAR(20)  NOT NULL,
    key_args          JSONB        NOT NULL DEFAULT '{}',
    status            VARCHAR(20)  NOT NULL DEFAULT 'pending',
    result_summary    TEXT,
    idempotency_key   TEXT,
    external_ref      TEXT,
    created_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    completed_at      TIMESTAMPTZ,
    CONSTRAINT ck_execution_ledger_status
        CHECK (status IN ('pending', 'success', 'error', 'blocked', 'unknown')),
    CONSTRAINT ck_execution_ledger_side_effect
        CHECK (side_effect_type IN ('write', 'external', 'irreversible'))
);

CREATE INDEX IF NOT EXISTS idx_execution_ledger_agent_created
    ON nous_system.execution_ledger (agent_id, created_at DESC);

-- The orphan sweeps scan only pending rows.
CREATE INDEX IF NOT EXISTS idx_execution_ledger_pending
    ON nous_system.execution_ledger (agent_id, created_at)
    WHERE status = 'pending';

CREATE INDEX IF NOT EXISTS idx_execution_ledger_dag_node
    ON nous_system.execution_ledger (dag_node_id)
    WHERE dag_node_id IS NOT NULL;
```

- [ ] **Step 2: Add the ORM model** (`nous/storage/models.py`, after `A2uiAction`)

```python
LEDGER_STATUSES: tuple[str, ...] = ("pending", "success", "error", "blocked", "unknown")
LEDGER_SIDE_EFFECTS: tuple[str, ...] = ("write", "external", "irreversible")


class ExecutionLedgerEntry(Base):
    """Harness Phase 1b: durable record of one side-effecting tool call.

    See migration 074. Written by ``nous.cognitive.ledger_store.LedgerStore``.
    """

    __tablename__ = "execution_ledger"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'success', 'error', 'blocked', 'unknown')",
            name="ck_execution_ledger_status",
        ),
        CheckConstraint(
            "side_effect_type IN ('write', 'external', 'irreversible')",
            name="ck_execution_ledger_side_effect",
        ),
        {"schema": "nous_system"},
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
        server_default=func.gen_random_uuid(),
    )
    agent_id: Mapped[str] = mapped_column(String(100), nullable=False)
    session_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    context_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    subtask_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    dag_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    dag_node_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    turn: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tool_name: Mapped[str] = mapped_column(String(100), nullable=False)
    side_effect_type: Mapped[str] = mapped_column(String(20), nullable=False)
    key_args: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    result_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    external_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
```

(Use the imports already present at the top of `models.py`; add `CheckConstraint` to the
`sqlalchemy` import if it is not there.) Also check `tests/sqlite_compat.py` — if it has an
explicit schema/table allowlist, add `nous_system.execution_ledger` there.

- [ ] **Step 3:** Covered by Task 7's round-trip test. Commit together with Task 7.

### Task 7: `LedgerStore`

**Files:**
- Modify: `nous/cognitive/execution_ledger.py` — hoist `_summarize_args` to module-level `summarize_args(tool_name, args)`; the method delegates
- Create: `nous/cognitive/ledger_store.py`
- Test: `tests/test_ledger_store.py`

**Interfaces:**
- Consumes: `ExecutionContext` (1a); `classify_side_effect`, `redact_key_args`, `summarize_args` (`execution_ledger.py`); `ExecutionLedgerEntry` (Task 6); `Database.session()`.
- Produces:
  - `LedgerStore(database, agent_id: str, *, write_timeout_seconds: float = 2.0)`
  - `async open_entry(*, context, tool_name, tool_input, turn) -> UUID | None` — `None` when the tool is side-effect `none` or the write failed
  - `async record_blocked(*, context, tool_name, tool_input, turn, reason) -> None`
  - `async close_entry(entry_id: UUID | None, *, status: str, result_summary: str | None) -> None` — only moves a row out of `pending`
  - `async mark_orphans_unknown(*, created_before: datetime) -> int`
  - `async prune(*, retention_days: int) -> int`
  - `RESULT_SUMMARY_CHARS = 500`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_ledger_store.py
"""Harness Phase 1b: durable execution ledger."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, update

from nous.api.execution_context import ExecutionContext
from nous.cognitive.ledger_store import LedgerStore
from nous.storage.models import ExecutionLedgerEntry

AGENT = "ledger-test-agent"


@pytest.fixture
def store(db):
    return LedgerStore(db, AGENT)


async def _row(db, entry_id):
    async with db.session() as s:
        return (await s.execute(
            select(ExecutionLedgerEntry).where(ExecutionLedgerEntry.id == entry_id)
        )).scalar_one()


@pytest.mark.asyncio
async def test_open_writes_a_pending_row_with_context(store, db):
    dag_id, node_id = uuid.uuid4(), uuid.uuid4()
    ctx = ExecutionContext(kind="dag_node", session_id="subtask-1", dag_id=dag_id, dag_node_id=node_id)
    entry_id = await store.open_entry(
        context=ctx, tool_name="write_file",
        tool_input={"file_path": "/tmp/nous-workspace/x.txt", "content": "hi"}, turn=2,
    )
    row = await _row(db, entry_id)
    assert (row.status, row.context_kind, row.dag_id, row.dag_node_id, row.turn) == (
        "pending", "dag_node", dag_id, node_id, 2)
    assert row.side_effect_type == "write"
    assert row.key_args == {"file_path": "/tmp/nous-workspace/x.txt"}
    assert row.completed_at is None


@pytest.mark.asyncio
async def test_reads_are_not_persisted(store):
    ctx = ExecutionContext(kind="interactive")
    assert await store.open_entry(context=ctx, tool_name="recall_deep",
                                  tool_input={"query": "x"}, turn=1) is None
    assert await store.open_entry(context=ctx, tool_name="bash",
                                  tool_input={"command": "ls -la"}, turn=1) is None


@pytest.mark.asyncio
async def test_bash_side_effects_are_persisted_with_redaction(store, db):
    entry_id = await store.open_entry(
        context=ExecutionContext(kind="subtask"), tool_name="bash",
        tool_input={"command": "curl -H 'Authorization: Bearer sk-abc123' https://x"}, turn=1,
    )
    row = await _row(db, entry_id)
    assert row.side_effect_type == "external"
    assert "sk-abc123" not in row.key_args["command"]


@pytest.mark.asyncio
async def test_close_moves_pending_to_terminal_once(store, db):
    entry_id = await store.open_entry(
        context=ExecutionContext(kind="interactive"), tool_name="learn_fact",
        tool_input={"content": "c"}, turn=1,
    )
    await store.close_entry(entry_id, status="success", result_summary="x" * 900)
    row = await _row(db, entry_id)
    assert row.status == "success" and row.completed_at is not None
    assert len(row.result_summary) == 500
    # A second close never rewrites a terminal row.
    await store.close_entry(entry_id, status="error", result_summary="late")
    assert (await _row(db, entry_id)).status == "success"


@pytest.mark.asyncio
async def test_close_of_none_is_a_noop(store):
    await store.close_entry(None, status="success", result_summary="ok")


@pytest.mark.asyncio
async def test_close_rejects_unknown_status(store):
    with pytest.raises(ValueError):
        await store.close_entry(uuid.uuid4(), status="done", result_summary=None)


@pytest.mark.asyncio
async def test_record_blocked_writes_a_terminal_row(store, db):
    await store.record_blocked(
        context=ExecutionContext(kind="heartbeat_triage"), tool_name="send_file",
        tool_input={"file_path": "/x"}, turn=1, reason="not offered",
    )
    async with db.session() as s:
        rows = (await s.execute(select(ExecutionLedgerEntry).where(
            ExecutionLedgerEntry.agent_id == AGENT,
            ExecutionLedgerEntry.tool_name == "send_file",
        ))).scalars().all()
    assert [(r.status, r.result_summary) for r in rows][-1] == ("blocked", "not offered")


@pytest.mark.asyncio
async def test_open_is_fail_open(db, caplog):
    class _Broken:
        def session(self):
            raise RuntimeError("db down")

    store = LedgerStore(_Broken(), AGENT)
    assert await store.open_entry(context=ExecutionContext(kind="interactive"),
                                  tool_name="learn_fact", tool_input={}, turn=1) is None
    assert "execution ledger write failed" in caplog.text


@pytest.mark.asyncio
async def test_orphan_sweep_is_agent_scoped_and_leaves_new_rows(store, db):
    old = await store.open_entry(context=ExecutionContext(kind="subtask"),
                                 tool_name="learn_fact", tool_input={}, turn=1)
    other = LedgerStore(db, "someone-else")
    foreign = await other.open_entry(context=ExecutionContext(kind="subtask"),
                                     tool_name="learn_fact", tool_input={}, turn=1)
    async with db.session() as s:
        await s.execute(update(ExecutionLedgerEntry)
                        .where(ExecutionLedgerEntry.id.in_([old, foreign]))
                        .values(created_at=datetime.now(UTC) - timedelta(hours=3)))
        await s.commit()
    fresh = await store.open_entry(context=ExecutionContext(kind="subtask"),
                                   tool_name="learn_fact", tool_input={}, turn=1)

    n = await store.mark_orphans_unknown(created_before=datetime.now(UTC) - timedelta(hours=1))
    assert n == 1
    assert (await _row(db, old)).status == "unknown"
    assert (await _row(db, fresh)).status == "pending"
    assert (await _row(db, foreign)).status == "pending"


@pytest.mark.asyncio
async def test_prune_is_agent_scoped(store, db):
    old = await store.open_entry(context=ExecutionContext(kind="subtask"),
                                 tool_name="learn_fact", tool_input={}, turn=1)
    async with db.session() as s:
        await s.execute(update(ExecutionLedgerEntry)
                        .where(ExecutionLedgerEntry.id == old)
                        .values(created_at=datetime.now(UTC) - timedelta(days=200)))
        await s.commit()
    assert await store.prune(retention_days=90) >= 1
    async with db.session() as s:
        assert (await s.execute(select(ExecutionLedgerEntry)
                                .where(ExecutionLedgerEntry.id == old))).scalar_one_or_none() is None
```

> The bash-redaction test assumes `_REDACT_PATTERNS` already masks `Bearer <token>`; if it does
> not, assert on whatever the existing patterns mask (read `execution_ledger.py` `_REDACT_PATTERNS`
> first) — this task does not change redaction.

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_ledger_store.py -q`
Expected: `ModuleNotFoundError: nous.cognitive.ledger_store` (and `ExecutionLedgerEntry` import error before Task 6's model exists).

- [ ] **Step 3: Implement**

`nous/cognitive/execution_ledger.py` — hoist the arg summarizer (the method body moves verbatim):

```python
def summarize_args(tool_name: str, args: dict[str, Any]) -> dict[str, str]:
    """Extract key identifying args and truncate values to 80 chars."""
    key_names = _KEY_ARGS.get(tool_name, [])
    result: dict[str, str] = {}
    if key_names:
        for name in key_names:
            if name in args:
                result[name] = str(args[name])[:80]
    else:
        # Fallback: capture up to 5 args for unknown tools
        for k, v in list(args.items())[:5]:
            result[k] = str(v)[:80]
    return result
```

and the method becomes:

```python
    def _summarize_args(self, tool_name: str, args: dict[str, Any]) -> dict[str, str]:
        """Extract key identifying args and truncate values to 80 chars."""
        return summarize_args(tool_name, args)
```

`nous/cognitive/ledger_store.py`:

```python
"""Durable execution ledger (harness-autonomy roadmap, Phase 1b).

The in-memory F026 ExecutionLedger stays the per-session prompt aid. This
store is the durable record of side-effecting tool calls: a row is written
'pending' BEFORE dispatch and closed after, so "did it happen?" is a query.
Writes are bounded by a timeout and fail OPEN in Phase 1b — a ledger outage
must not stop the agent working; Phase 2b makes keyed sends fail closed.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import delete, func, update

from nous.api.execution_context import ExecutionContext
from nous.cognitive.execution_ledger import classify_side_effect, redact_key_args, summarize_args
from nous.storage.models import LEDGER_STATUSES, ExecutionLedgerEntry

logger = logging.getLogger(__name__)

RESULT_SUMMARY_CHARS = 500
_TERMINAL = frozenset(s for s in LEDGER_STATUSES if s != "pending")


class LedgerStore:
    def __init__(self, database: Any, agent_id: str, *, write_timeout_seconds: float = 2.0) -> None:
        self._db = database
        self._agent_id = agent_id
        self._timeout = write_timeout_seconds

    async def open_entry(
        self, *, context: ExecutionContext, tool_name: str,
        tool_input: dict[str, Any], turn: int | None,
    ) -> UUID | None:
        """Insert a 'pending' row for a side-effecting call; None otherwise."""
        return await self._insert(context, tool_name, tool_input, turn, "pending", None)

    async def record_blocked(
        self, *, context: ExecutionContext, tool_name: str,
        tool_input: dict[str, Any], turn: int | None, reason: str,
    ) -> None:
        """A side-effecting call the harness refused: one terminal row."""
        await self._insert(context, tool_name, tool_input, turn, "blocked", reason)

    async def close_entry(
        self, entry_id: UUID | None, *, status: str, result_summary: str | None,
    ) -> None:
        """Move a row out of 'pending'. A row already terminal is left alone."""
        if status not in _TERMINAL:
            raise ValueError(f"cannot close a ledger row as {status!r}")
        if entry_id is None:
            return

        async def _close() -> None:
            async with self._db.session() as s:
                await s.execute(
                    update(ExecutionLedgerEntry)
                    .where(ExecutionLedgerEntry.id == entry_id)
                    .where(ExecutionLedgerEntry.agent_id == self._agent_id)
                    .where(ExecutionLedgerEntry.status == "pending")
                    .values(
                        status=status,
                        result_summary=(result_summary or "")[:RESULT_SUMMARY_CHARS] or None,
                        completed_at=datetime.now(UTC),
                    )
                )
                await s.commit()

        try:
            await asyncio.wait_for(_close(), timeout=self._timeout)
        except Exception:
            logger.warning(
                "Harness: execution ledger close failed for %s (row stays pending "
                "until the orphan sweep marks it unknown)", entry_id, exc_info=True,
            )

    async def mark_orphans_unknown(self, *, created_before: datetime) -> int:
        """Pending rows older than ``created_before`` → 'unknown'. Agent-scoped."""
        async with self._db.session() as s:
            result = await s.execute(
                update(ExecutionLedgerEntry)
                .where(ExecutionLedgerEntry.agent_id == self._agent_id)
                .where(ExecutionLedgerEntry.status == "pending")
                .where(ExecutionLedgerEntry.created_at < created_before)
                .values(
                    status="unknown",
                    result_summary="process ended before this call reported back — outcome unknown",
                    completed_at=func.now(),
                )
            )
            await s.commit()
            return result.rowcount or 0

    async def prune(self, *, retention_days: int) -> int:
        """Delete rows older than ``retention_days``. Agent-scoped."""
        async with self._db.session() as s:
            result = await s.execute(
                delete(ExecutionLedgerEntry)
                .where(ExecutionLedgerEntry.agent_id == self._agent_id)
                .where(ExecutionLedgerEntry.created_at
                       < func.now() - func.make_interval(0, 0, 0, retention_days))
            )
            await s.commit()
            return result.rowcount or 0

    async def _insert(
        self, context: ExecutionContext, tool_name: str, tool_input: dict[str, Any],
        turn: int | None, status: str, result_summary: str | None,
    ) -> UUID | None:
        side_effect = classify_side_effect(tool_name, tool_input)
        if side_effect == "none":
            return None
        entry_id = uuid4()
        row = ExecutionLedgerEntry(
            id=entry_id,
            agent_id=self._agent_id,
            session_id=context.session_id,
            context_kind=context.kind,
            subtask_id=context.subtask_id,
            dag_id=context.dag_id,
            dag_node_id=context.dag_node_id,
            turn=turn,
            tool_name=tool_name,
            side_effect_type=side_effect,
            key_args=redact_key_args(tool_name, summarize_args(tool_name, tool_input or {})),
            status=status,
            result_summary=(result_summary or "")[:RESULT_SUMMARY_CHARS] or None,
            completed_at=None if status == "pending" else datetime.now(UTC),
        )

        async def _write() -> None:
            async with self._db.session() as s:
                s.add(row)
                await s.commit()

        try:
            await asyncio.wait_for(_write(), timeout=self._timeout)
        except Exception:
            logger.warning(
                "Harness: execution ledger write failed for %s (%s) — call proceeds unrecorded",
                tool_name, context.kind, exc_info=True,
            )
            return None
        return entry_id
```

> `func.make_interval(0, 0, 0, retention_days)` is Postgres-only; on SQLite the prune test must
> be `@pytest.mark.postgres_only`, OR compute the cutoff in Python
> (`datetime.now(UTC) - timedelta(days=retention_days)`) and compare `created_at < cutoff` —
> **prefer the Python cutoff** (works on both backends, one less dialect branch).

- [ ] **Step 4: Run**

Run: `uv run pytest tests/test_ledger_store.py tests/test_execution_ledger*.py -q`
Expected: all pass (SQLite). CI re-runs them against the migrated Postgres schema.

- [ ] **Step 5: Commit**

```bash
git add sql/migrations/074_execution_ledger.sql nous/storage/models.py nous/cognitive/execution_ledger.py nous/cognitive/ledger_store.py tests/test_ledger_store.py
git commit -m "feat(ledger): durable execution ledger table + LedgerStore (harness Phase 1b)"
```

### Task 8: Runner writes the ledger around dispatch

**Files:**
- Modify: `nous/api/runner.py` — `set_ledger_store`, `_fork` sharing (~line 315-321), `_tool_loop` dispatch site, gate-blocked and refused branches, `stream_chat` equivalents, `_dispatch_with_keepalive` outcome shape
- Test: `tests/test_runner_ledger.py` (create)

**Interfaces:**
- Consumes: `LedgerStore` (Task 7); `ctx`/`_ctx`, `_authorize_tool_call` (PR 1a).
- Produces: `AgentRunner.set_ledger_store(store: LedgerStore | None) -> None`; `_dispatch_with_keepalive` final yield becomes `DispatchOutcome(result_text: str, is_error: bool, timed_out: bool)` (a `NamedTuple` in `runner.py`).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_runner_ledger.py
"""Harness Phase 1b: the runner brackets every side-effecting dispatch."""

from __future__ import annotations

import asyncio

import pytest

from nous.api.execution_context import ExecutionContext
from tests.test_runner_authorization import (
    AgentRunner, _MockBrain, _MockCognitive, _MockHeart, _one_tool_call_then_done,
    _RecordingDispatcher, _run_loop, _settings,
)


class _FakeStore:
    def __init__(self):
        self.events: list[tuple] = []

    async def open_entry(self, *, context, tool_name, tool_input, turn):
        self.events.append(("open", tool_name, context.kind))
        return f"id-{tool_name}"

    async def record_blocked(self, *, context, tool_name, tool_input, turn, reason):
        self.events.append(("blocked", tool_name, reason[:20]))

    async def close_entry(self, entry_id, *, status, result_summary):
        self.events.append(("close", entry_id, status))


class _OrderedDispatcher(_RecordingDispatcher):
    def __init__(self, offered, store):
        super().__init__(offered)
        self.store = store

    async def dispatch(self, name, inp, **kw):
        self.store.events.append(("dispatch", name))
        return await super().dispatch(name, inp, **kw)


def _runner(store, dispatcher_cls=_OrderedDispatcher, offered=("learn_fact",)):
    r = AgentRunner(_MockCognitive(), _MockBrain(), _MockHeart(), _settings())
    d = dispatcher_cls(list(offered), store)
    r.set_dispatcher(d)
    r.set_ledger_store(store)
    return r, d


@pytest.mark.asyncio
async def test_row_opens_before_dispatch_and_closes_after():
    store = _FakeStore()
    r, _ = _runner(store)
    r._call_api = _one_tool_call_then_done("learn_fact")
    await _run_loop(r, is_background=True, context=ExecutionContext(kind="subtask", session_id="s1"))
    assert store.events == [
        ("open", "learn_fact", "subtask"),
        ("dispatch", "learn_fact"),
        ("close", "id-learn_fact", "success"),
    ]


@pytest.mark.asyncio
async def test_tool_error_closes_error():
    store = _FakeStore()

    class _Failing(_OrderedDispatcher):
        async def dispatch(self, name, inp, **kw):
            self.store.events.append(("dispatch", name))
            return "boom", True

    r, _ = _runner(store, _Failing)
    r._call_api = _one_tool_call_then_done("learn_fact")
    await _run_loop(r)
    assert store.events[-1] == ("close", "id-learn_fact", "error")


@pytest.mark.asyncio
async def test_cancellation_mid_call_closes_unknown_and_reraises():
    store = _FakeStore()

    class _Hanging(_OrderedDispatcher):
        async def dispatch(self, name, inp, **kw):
            self.store.events.append(("dispatch", name))
            await asyncio.sleep(3600)

    r, _ = _runner(store, _Hanging)
    r._call_api = _one_tool_call_then_done("learn_fact")
    task = asyncio.create_task(_run_loop(r, is_background=True))
    for _ in range(50):
        await asyncio.sleep(0)
        if ("dispatch", "learn_fact") in store.events:
            break
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert store.events[-1] == ("close", "id-learn_fact", "unknown")


@pytest.mark.asyncio
async def test_refused_call_is_recorded_blocked():
    store = _FakeStore()
    r, d = _runner(store, offered=("recall_deep", "learn_fact"))
    r._call_api = _one_tool_call_then_done("learn_fact")
    await _run_loop(r, is_background=True, tool_filter=["recall_deep"])
    assert d.calls == []
    assert store.events == [("blocked", "learn_fact", "Tool error: 'learn_fa")]


@pytest.mark.asyncio
async def test_no_store_means_no_ledger_calls():
    r = AgentRunner(_MockCognitive(), _MockBrain(), _MockHeart(), _settings())
    d = _RecordingDispatcher(["learn_fact"])
    r.set_dispatcher(d)
    r._call_api = _one_tool_call_then_done("learn_fact")
    await _run_loop(r)
    assert [c[0] for c in d.calls] == ["learn_fact"]


@pytest.mark.asyncio
async def test_extra_tools_are_not_persisted():
    store = _FakeStore()
    r, _ = _runner(store, offered=("recall_deep",))
    r._call_api = _one_tool_call_then_done("submit_final_report")

    async def _submit(**_):
        return "ok", False

    schema = {"name": "submit_final_report", "description": "s", "input_schema": {"type": "object"}}
    await _run_loop(r, is_background=True, extra_tools={"submit_final_report": (schema, _submit)})
    assert store.events == []
```

Add a streaming test that a `_dispatch_with_keepalive` timeout closes `unknown`
(`settings.tool_timeout=0.05`, a dispatcher that sleeps 1 s) using the `_stream_one_tool_call`
helper from PR 1a.

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_runner_ledger.py -q`
Expected: FAIL — `AttributeError: 'AgentRunner' object has no attribute 'set_ledger_store'`.

- [ ] **Step 3: Implement**

`AgentRunner.__init__`: `self._ledger_store: LedgerStore | None = None`, and

```python
    def set_ledger_store(self, store: LedgerStore | None) -> None:
        """Harness Phase 1b: durable ledger for side-effecting tool calls."""
        self._ledger_store = store
```

In the fork method (where `forked._ledgers = self._ledgers`), add
`forked._ledger_store = self._ledger_store`.

Add near the top of `runner.py`:

```python
class DispatchOutcome(NamedTuple):
    """Final item yielded by _dispatch_with_keepalive."""
    result_text: str
    is_error: bool
    timed_out: bool
```

and three helpers on `AgentRunner`:

```python
    async def _ledger_open(self, ctx, tool_name, tool_input, turn):
        if self._ledger_store is None:
            return None
        return await self._ledger_store.open_entry(
            context=ctx, tool_name=tool_name, tool_input=tool_input, turn=turn,
        )

    async def _ledger_close(self, entry_id, status, result_summary):
        if self._ledger_store is None or entry_id is None:
            return
        await self._ledger_store.close_entry(entry_id, status=status, result_summary=result_summary)

    async def _ledger_blocked(self, ctx, tool_name, tool_input, turn, reason):
        if self._ledger_store is None:
            return
        await self._ledger_store.record_blocked(
            context=ctx, tool_name=tool_name, tool_input=tool_input, turn=turn, reason=reason,
        )
```

`_tool_loop`, the refusal branch from PR 1a gains (before `continue`):

```python
                        await self._ledger_blocked(
                            ctx, tool_name, tool_input,
                            ledger.current_turn if ledger else None, refusal,
                        )
```

the ActionGate `enforce` branch gains, after its `ledger.record(..., "blocked")`:

```python
                                await self._ledger_blocked(
                                    ctx, tool_name, tool_input, ledger.current_turn, result_text,
                                )
```

and the dispatcher branch (the `else:` under the `extra_tools` check) becomes:

```python
                            entry_id = await self._ledger_open(
                                ctx, tool_name, tool_input,
                                ledger.current_turn if ledger else None,
                            )
                            try:
                                result_text, is_error = await self._dispatcher.dispatch(
                                    tool_name, tool_input, session_id=session_id,
                                    is_background=is_background,
                                    turn_number=turn_number,  # F091 (caller-captured)
                                    context=ctx,
                                )
                            except asyncio.CancelledError:
                                # Subtask timeout / shutdown: the side effect may or
                                # may not have happened (an orphaned SMTP thread can
                                # still deliver) — record exactly that, then re-raise.
                                await asyncio.shield(self._ledger_close(
                                    entry_id, "unknown", "cancelled mid-call — outcome unknown",
                                ))
                                raise
                            except Exception as exc:
                                await self._ledger_close(entry_id, "error", f"{type(exc).__name__}: {exc}")
                                raise
                            else:
                                await self._ledger_close(
                                    entry_id, "error" if is_error else "success", result_text,
                                )
                            finally:
                                await self._stop_activity_heartbeat(_hb)
```

(the existing `try/finally` around dispatch that stops `_hb` is folded into this block —
keep exactly one `_stop_activity_heartbeat` call.)

`_dispatch_with_keepalive`: set `timed_out = False` before the result block, `timed_out = True`
in the `except TimeoutError:` branch, and change the final `yield (result_text, is_error)` to
`yield DispatchOutcome(result_text, is_error, timed_out)`. Update its return annotation to
`AsyncGenerator[StreamEvent | DispatchOutcome, None]`.

`stream_chat`: the refusal and gate-blocked branches gain the same `_ledger_blocked` calls (with
`_ctx`, `tc["name"]`, `dispatch_input`/`tc.get("input", {})`); the dispatch site becomes

```python
                            entry_id = await self._ledger_open(
                                _ctx, tc["name"], dispatch_input,
                                ledger.current_turn if ledger else None,
                            )
                            timed_out = False
                            try:
                                async for item in self._dispatch_with_keepalive(
                                    tc["name"], dispatch_input, session_id=session_id,
                                    turn_number=_stream_turn_number,  # F091
                                    context=_ctx,
                                ):
                                    if isinstance(item, StreamEvent):
                                        yield item
                                    else:
                                        result_text, is_error, timed_out = item
                            except (asyncio.CancelledError, GeneratorExit):
                                await asyncio.shield(self._ledger_close(
                                    entry_id, "unknown", "stream closed mid-call — outcome unknown",
                                ))
                                raise
                            await self._ledger_close(
                                entry_id,
                                "unknown" if timed_out else ("error" if is_error else "success"),
                                result_text,
                            )
```

- [ ] **Step 4: Run**

Run: `uv run pytest tests/test_runner_ledger.py tests/test_runner_authorization.py tests/test_runner.py tests/test_runner_background.py -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add nous/api/runner.py tests/test_runner_ledger.py
git commit -m "feat(runner): bracket side-effecting dispatch with durable ledger rows (harness Phase 1b)"
```

### Task 9: Settings + wiring + sweeps

**Files:**
- Modify: `nous/config.py` (next to `execution_ledger_enabled`)
- Modify: `nous/main.py` (after `runner.set_dispatcher(dispatcher)`, ~line 527; retention loop beside the F091 one, ~line 624)
- Test: `tests/test_ledger_store.py` (settings validation)

**Interfaces:**
- Produces settings `execution_ledger_persist_enabled: bool = True`, `execution_ledger_write_timeout_seconds: float = 2.0` (`gt=0`), `execution_ledger_retention_days: int = 90` (`ge=0`, 0 disables pruning), `execution_ledger_pending_unknown_after_seconds: int = 7800` (`ge=60`).

- [ ] **Step 1: Test** (append to `tests/test_ledger_store.py`)

```python
def test_ledger_settings_defaults_and_bounds():
    from pydantic import ValidationError

    from nous.config import Settings

    s = Settings(ANTHROPIC_API_KEY="k")
    assert s.execution_ledger_persist_enabled is True
    assert s.execution_ledger_retention_days == 90
    assert s.execution_ledger_pending_unknown_after_seconds == 7800
    with pytest.raises(ValidationError):
        Settings(ANTHROPIC_API_KEY="k", execution_ledger_write_timeout_seconds=0)
    with pytest.raises(ValidationError):
        Settings(ANTHROPIC_API_KEY="k", execution_ledger_retention_days=-1)
```

- [ ] **Step 2: Implement the settings**

```python
    # Harness Phase 1b: persist side-effecting tool calls to
    # nous_system.execution_ledger (migration 074). Additive telemetry;
    # kill switch only. Reads are never persisted.
    execution_ledger_persist_enabled: bool = Field(
        default=True, validation_alias="NOUS_EXECUTION_LEDGER_PERSIST_ENABLED"
    )
    execution_ledger_write_timeout_seconds: float = Field(
        default=2.0, gt=0, validation_alias="NOUS_EXECUTION_LEDGER_WRITE_TIMEOUT_SECONDS"
    )
    execution_ledger_retention_days: int = Field(
        default=90, ge=0, validation_alias="NOUS_EXECUTION_LEDGER_RETENTION_DAYS"
    )
    # A 'pending' row older than this is swept to 'unknown'. Must exceed the
    # longest legitimate tool call: dag_node_max_timeout (7200) + 600.
    execution_ledger_pending_unknown_after_seconds: int = Field(
        default=7800, ge=60,
        validation_alias="NOUS_EXECUTION_LEDGER_PENDING_UNKNOWN_AFTER_SECONDS",
    )
```

- [ ] **Step 3: Wire in `nous/main.py`** — capture `process_started_at = datetime.now(UTC)` at the
top of the lifespan/startup function, then after `runner.set_dispatcher(dispatcher)`:

```python
    ledger_store = None
    execution_ledger_task = None
    if settings.execution_ledger_persist_enabled:
        from nous.cognitive.ledger_store import LedgerStore

        ledger_store = LedgerStore(
            database, settings.agent_id,
            write_timeout_seconds=settings.execution_ledger_write_timeout_seconds,
        )
        runner.set_ledger_store(ledger_store)
        try:
            # Any row still pending from before THIS process started belongs to
            # a dead dispatch: its outcome is unknown, not in flight.
            n = await ledger_store.mark_orphans_unknown(created_before=process_started_at)
            if n:
                logger.warning("Harness: %d execution-ledger rows orphaned by the previous process -> unknown", n)
        except Exception:
            logger.warning("Harness: startup ledger orphan sweep failed", exc_info=True)

        async def _execution_ledger_maintenance_loop():
            while True:
                try:
                    await asyncio.sleep(86400)
                    cutoff = datetime.now(UTC) - timedelta(
                        seconds=settings.execution_ledger_pending_unknown_after_seconds
                    )
                    await ledger_store.mark_orphans_unknown(created_before=cutoff)
                    if settings.execution_ledger_retention_days > 0:
                        n = await ledger_store.prune(
                            retention_days=settings.execution_ledger_retention_days
                        )
                        logger.info("Harness: execution ledger retention pruned %d rows", n)
                except asyncio.CancelledError:
                    break
                except Exception:
                    logger.debug("Harness: execution ledger maintenance failed", exc_info=True)

        execution_ledger_task = asyncio.create_task(_execution_ledger_maintenance_loop())
```

Cancel `execution_ledger_task` in shutdown alongside `retrieval_log_retention_task` (same pattern,
search `retrieval_log_retention_task` in `main.py`). Any other runner constructed in `main.py`
(grep `AgentRunner(`) that is not produced by the fork method also gets `set_ledger_store`.

- [ ] **Step 4: Run** `uv run pytest tests/test_ledger_store.py -q` and a `python -c "import nous.main"` import smoke.
- [ ] **Step 5: Commit** `git commit -m "feat(ledger): wire LedgerStore, startup orphan sweep and retention (harness Phase 1b)"`

### Task 10: Docs + PR 1b

- [ ] **Step 1:** CLAUDE.md env-var rows for the four settings (text from the Field comments), and
  add `nous_system.execution_ledger` to the Database section's table list.
- [ ] **Step 2:** Update the `A2uiAction` docstring's "(the F032 ledger is in-memory and
  session-scoped)" — it is now half-true; say `ledger_entry_id` can reference
  `nous_system.execution_ledger.id` (still unpopulated).
- [ ] **Step 3:** Full touched suites; lint baseline; push; PR; `@codex review`; CI; merge on
  codex-clean + green.

---

## Self-review

- **Spec coverage:** roadmap §3 1a (context kinds, threading, both loops, offered set incl.
  `extra_tools`, kill switch) → Tasks 1–5. 1b (table, pending-before-dispatch, close states,
  cancellation → unknown, orphan sweep, retention, fail-open, reads excluded) → Tasks 6–10.
- **Known risks for review to attack:**
  1. `resolve_context` makes `context.is_background` authoritative — any caller that passes a
     background context but relied on foreground F048 behavior changes streaming mode (none today).
  2. Awaited ledger insert adds DB latency to every side-effecting call (bounded by
     `execution_ledger_write_timeout_seconds`).
  3. `asyncio.shield(...)` inside a `CancelledError` handler: a second cancel while awaiting the
     shielded close abandons the await but the close task still completes.
  4. `GeneratorExit` in an async generator cannot `await` safely in all interpreters — if the
     shielded close raises `RuntimeError: async generator ignored GeneratorExit`, fall back to
     scheduling the close with `asyncio.get_running_loop().create_task(...)` and a strong ref.
  5. `for_subtask` classifies by metadata keys written by three different subsystems — a fourth
     creator (e.g. a future F063 inbox) lands as plain `subtask`.
