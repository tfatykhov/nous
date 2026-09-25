# Harness Autonomy Phase 2a — Tool Classes Declared Once + Per-Context Policy Implementation Plan (v2)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A tool's risk class is declared once, and whether an execution context may use it is decided by one table consulted at the harness choke point — measured (`warn`) before it is enforced.

**Architecture:** A leaf module `nous/api/tool_classes.py` holds one static table `TOOL_CLASSES` (side-effect level + `spawns`); the ledger's READ/WRITE/EXTERNAL/IRREVERSIBLE sets become views derived from it, which also fixes the F078 refuse denylist. A second leaf module `nous/api/tool_policy.py` holds `CONTEXT_POLICY` keyed by `ExecutionContext.kind` and an `evaluate()` function that `AgentRunner._authorize_tool_call` calls after the offered-set check. A new setting gates it `off|warn|enforce`, default `warn`.

**Tech Stack:** Python 3.12+, pydantic-settings, pytest, AST-based drift tests.

**Spec:** `docs/superpowers/plans/2026-09-24-harness-autonomy-roadmap.md` §1 findings 1/4, §2 row P0.1, §3 row 2a and "Design forks resolved for Phase 2", §4 rows marked 2a. Anchors from `main` `240c795`; this plan branches after Phase 2c merges.

**v2.1 (after re-review):** version-suffixed interpreters (`python3.12 -c`) are read like `python3 -c`; the deliberate over-classification of network detection is documented.

**v2 (after 3-agent review):** `_authorize_tool_call` is restructured so the policy is reached for offered calls (architect/devil P2-P3); a tool a check or callback *declared* satisfies the spawn rule — check pipelines declare `heartbeat_check_create` (both P2); an empty declared list (the DB default) is "undeclared", not "nothing" (devil P2); `reenable` compares against the callback's own check name, now on the context (devil P2); `dag_summary` may spawn only through `spawn_task` (devil P2); `run_python` code and `python -c` strings that reach the network classify `external`, so the level rules cannot be sidestepped by writing a script (devil P2).

## Global Constraints

- **Deliberate deviation from the roadmap fork "tag at the `register()` call site":** registration is conditional and happens in `main.py` after the runner starts, while every consumer (ActionGate, ledger_store, the runner's refuse denylist, the claim verifier) reads classes at import time without a dispatcher. One **static table** keeps the same invariant — one definition — and is readable everywhere; an AST drift test makes an unclassified registration a test failure.
- `side_effect` is a **level** (`none` < `write` < `external` < `irreversible`); `irreversible` stays a level because ActionGate Tier 3 and the DB CHECK on `execution_ledger.side_effect_type` read it that way.
- Policy ships `warn` (log + event, the call runs). `enforce` is a later operator flip after reading the events — Phase 1a/1b/2c are not deployed yet, so no prod data exists.
- Behavior changes that ship ON: the F078 refuse-tier denylist also strips the 8 previously unclassified tools; `run_python` whose code reaches the network is classed `external` (ledger + ActionGate tier, which is off in prod).
- New setting is a plain pydantic field (no `validation_alias`; Settings has no `populate_by_name`).
- Tests: `UV_PROJECT_ENVIRONMENT=E:/Projects/nous/.venv uv run --frozen pytest …`. Commit explicit paths only.

## Why (verified, `main` `240c795`)

| Defect | Anchor |
|---|---|
| 8 registered tools in no class → default `write`, and the F078 refuse denylist does not strip them: `ingest_document`, `resolve_decision(s)`, `spawn_sync`, `dag_create`, `dag_manage`, `push_surface`, `compose_surface` | `nous/cognitive/execution_ledger.py:24-63`; `nous/api/runner.py:1306,1907` |
| No single decision point for "may this context do this"; restrictions are call-site name sets | `runner.py:1888-1899` |
| Heartbeat triage runs with `tool_filter=None`: every tool except the spawn trio | `nous/heartbeat/runner.py:563-571` |
| "A callback may NOT re-enable its check" is prompt text only | `heartbeat/runner.py:627`; `dynamic.py:549-554` |
| `run_python` is always `write`, so a script that sends mail passes as a local write | `execution_ledger.py:40-54` |

## Design

`TOOL_CLASSES` (every registered tool; `submit_final_report` too — injected via `extra_tools` in every hardened subtask):

| Level | Tools |
|---|---|
| `none` | recall_deep, recall_recent, read_file, get_procedure, web_search, web_fetch, list_tasks, cache_retrieve, recall_hubs, list_decisions, submit_final_report |
| `write` | write_file, learn_fact, record_decision, create_censor, store_identity, learn_skill, complete_initiation, cancel_task, run_python (floor; network code → external), heartbeat_check_manage, ingest_document, resolve_decision, resolve_decisions, push_surface, compose_surface, dag_manage, bash (floor; classified per command) |
| `write` + `spawns` | spawn_task, spawn_sync, schedule_task, heartbeat_check_create, dag_create |
| `external` | send_file, send_email |

`CONTEXT_POLICY` (intended; `warn` shows where reality differs):

| Kind | Allowed levels | Spawn |
|---|---|---|
| interactive, mcp | all | any |
| subtask, dag_node, scheduled, agent_action | none, write, external | none |
| dag_summary | none, write, external | `spawn_task` only (prod delivers email through it) |
| heartbeat_triage | none, write | none |
| heartbeat_check, heartbeat_callback | none, write, external | only tools the check **declared** (pipelines declare `heartbeat_check_create`) |
| background | none, write | none |

Plus, any non-foreground context: an **unclassified** tool is a violation; a check/callback with a **non-empty** declared list may use only those tools; a callback may not `heartbeat_check_manage(action=enable)` **its own** check. Violation codes: `unclassified`, `level:<level>`, `spawn`, `undeclared`, `reenable`.

---

### Task 1: `TOOL_CLASSES` + derived ledger sets + network-aware scripts + drift test

**Files:**
- Create: `nous/api/tool_classes.py`
- Modify: `nous/cognitive/execution_ledger.py` (sets `:24-63`, `classify_side_effect` `:322-334`, `ExecutionLedger._classify_side_effect` `:262-280`)
- Modify: `nous/cognitive/bash_side_effect.py` (`_classify_program`: `python -c`)
- Test: `tests/test_tool_classes.py` (new)

**Interfaces:**
- Produces: `SideEffect = Literal["none","write","external","irreversible"]`; `@dataclass(frozen=True) class ToolClass: side_effect: SideEffect; spawns: bool = False`; `TOOL_CLASSES: Mapping[str, ToolClass]`; `tool_class(name) -> ToolClass | None`; `code_reaches_network(code: str) -> bool`. `READ_TOOLS`, `WRITE_TOOLS`, `EXTERNAL_TOOLS`, `IRREVERSIBLE_TOOLS` stay module-level `set[str]` in `execution_ledger` (tests monkeypatch them), derived from `TOOL_CLASSES`.

- [ ] **Step 1: Write the failing tests**

```python
"""Harness Phase 2a: a tool's class is declared once, and every registration has one."""

import ast
from pathlib import Path

from nous.api.tool_classes import TOOL_CLASSES, code_reaches_network, tool_class
from nous.cognitive import execution_ledger as el
from nous.cognitive.bash_side_effect import classify_bash_command


def _registered_names() -> set[str]:
    """Every literal tool name passed to ``dispatcher.register(...)`` in production code."""
    names: set[str] = set()
    for path in Path("nous").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "register"
                    and isinstance(node.func.value, ast.Name) and node.func.value.id == "dispatcher"):
                first = node.args[0] if node.args else None
                assert isinstance(first, ast.Constant) and isinstance(first.value, str), (
                    f"{path}:{node.lineno}: register() must take a literal name")
                names.add(first.value)
    return names


def test_every_registered_tool_is_classified():
    missing = _registered_names() - set(TOOL_CLASSES)
    assert not missing, f"registered without a class in nous/api/tool_classes.py: {sorted(missing)}"


def test_the_drift_scan_sees_the_known_registrations():
    assert {"bash", "send_email", "dag_create", "compose_surface", "store_identity"} <= _registered_names()


def test_injected_extra_tools_are_classified():
    assert tool_class("submit_final_report").side_effect == "none"


def test_ledger_sets_are_derived_from_the_table():
    for name, cls in TOOL_CLASSES.items():
        if name in ("bash", "run_python"):
            continue
        expected = {"none": el.READ_TOOLS, "write": el.WRITE_TOOLS,
                    "external": el.EXTERNAL_TOOLS, "irreversible": el.IRREVERSIBLE_TOOLS}[cls.side_effect]
        assert name in expected, name


def test_previously_unclassified_tools_are_writes_now():
    for name in ("ingest_document", "resolve_decision", "resolve_decisions", "spawn_sync",
                 "dag_create", "dag_manage", "push_surface", "compose_surface"):
        assert el.classify_side_effect(name, {}) == "write", name


def test_bash_is_still_classified_per_command():
    assert el.classify_side_effect("bash", {"command": "ls"}) == "none"
    assert el.classify_side_effect("bash", {"command": "curl https://x"}) == "external"


def test_a_script_that_reaches_the_network_is_external():
    assert el.classify_side_effect("run_python", {"code": "import smtplib\nsmtplib.SMTP('h')"}) == "external"
    assert el.classify_side_effect("run_python", {"code": "import requests\nrequests.get(u)"}) == "external"
    assert el.classify_side_effect("run_python", {"code": "print(1 + 1)"}) == "write"
    assert classify_bash_command("python3 -c \"import smtplib; smtplib.SMTP('h')\"") == "external"
    assert classify_bash_command("python3 -c 'print(1)'") == "write"


def test_network_detector():
    assert code_reaches_network("from urllib.request import urlopen")
    assert code_reaches_network("httpx.post('https://api.telegram.org/bot')")
    assert not code_reaches_network("import json\njson.loads(s)")


def test_spawn_flags():
    assert {n for n, c in TOOL_CLASSES.items() if c.spawns} == {
        "spawn_task", "spawn_sync", "schedule_task", "heartbeat_check_create", "dag_create"}
```

- [ ] **Step 2: Run to verify they fail** — `ModuleNotFoundError: nous.api.tool_classes`.

- [ ] **Step 3: Implement** `nous/api/tool_classes.py`:

```python
"""Every tool's risk class, declared once (harness Phase 2a).

A leaf module: no imports from the rest of ``nous``, so the ledger, ActionGate,
the claim verifier and the runner can all read it at import time. Registration
happens later and conditionally in ``main.py``; ``tests/test_tool_classes.py``
fails if any ``dispatcher.register("<name>", ...)`` has no entry here.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal

SideEffect = Literal["none", "write", "external", "irreversible"]


@dataclass(frozen=True)
class ToolClass:
    side_effect: SideEffect   # a level: none < write < external < irreversible
    spawns: bool = False      # starts other agent work: a subtask, schedule, check or DAG


_READ = ToolClass("none")
_WRITE = ToolClass("write")
_SPAWN = ToolClass("write", spawns=True)
_EXTERNAL = ToolClass("external")

TOOL_CLASSES: Mapping[str, ToolClass] = MappingProxyType({
    # reads
    "recall_deep": _READ, "recall_recent": _READ, "read_file": _READ, "get_procedure": _READ,
    "web_search": _READ, "web_fetch": _READ, "list_tasks": _READ, "cache_retrieve": _READ,
    "recall_hubs": _READ, "list_decisions": _READ,
    "submit_final_report": _READ,  # injected via extra_tools in hardened subtasks
    # local writes
    "write_file": _WRITE, "learn_fact": _WRITE, "record_decision": _WRITE, "create_censor": _WRITE,
    "store_identity": _WRITE, "learn_skill": _WRITE, "complete_initiation": _WRITE,
    "cancel_task": _WRITE, "heartbeat_check_manage": _WRITE, "ingest_document": _WRITE,
    "resolve_decision": _WRITE, "resolve_decisions": _WRITE, "push_surface": _WRITE,
    "compose_surface": _WRITE, "dag_manage": _WRITE,
    "run_python": _WRITE,  # the floor; code that reaches the network is external
    "bash": _WRITE,        # the floor; classify_side_effect reads the command itself
    # start other agent work
    "spawn_task": _SPAWN, "spawn_sync": _SPAWN, "schedule_task": _SPAWN,
    "heartbeat_check_create": _SPAWN, "dag_create": _SPAWN,
    # leave the host
    "send_file": _EXTERNAL, "send_email": _EXTERNAL,
})

# Python that opens a connection to another host.
_NETWORK_CODE = re.compile(
    r"\b(?:smtplib|requests|httpx|aiohttp|urllib|http\.client|socket|ftplib|paramiko|telnetlib)\b"
    r"|https?://")


def tool_class(name: str) -> ToolClass | None:
    """The declared class, or None for a tool nobody classified."""
    return TOOL_CLASSES.get(name)


def code_reaches_network(code: str) -> bool:
    """True when Python source can open a connection to another host."""
    return bool(_NETWORK_CODE.search(code))
```

In `execution_ledger.py`, replace the four literals with derived sets (keep the names, `set` type, and the monkeypatch seam):

```python
from nous.api.tool_classes import TOOL_CLASSES, code_reaches_network


def _names(level: str) -> set[str]:
    return {name for name, cls in TOOL_CLASSES.items() if cls.side_effect == level}


# Derived from nous/api/tool_classes.py -- one definition.
READ_TOOLS: set[str] = _names("none")
WRITE_TOOLS: set[str] = _names("write")
EXTERNAL_TOOLS: set[str] = _names("external")
IRREVERSIBLE_TOOLS: set[str] = _names("irreversible")
```

`classify_side_effect` checks the two content-classified tools **first**, then the sets:

```python
def classify_side_effect(tool_name: str, tool_input: dict[str, Any] | None = None) -> str:
    """Module-level classifier for ActionGate, the ledgers and the context policy."""
    tool_input = tool_input or {}
    if tool_name == "bash":
        return _classify_bash_command(_extract_bash_command(tool_input))
    if tool_name == "run_python" and code_reaches_network(str(tool_input.get("code") or "")):
        return "external"
    if tool_name in IRREVERSIBLE_TOOLS:
        return "irreversible"
    if tool_name in EXTERNAL_TOOLS:
        return "external"
    if tool_name in READ_TOOLS:
        return "none"
    return "write"  # WRITE_TOOLS and anything unclassified
```

`ExecutionLedger._classify_side_effect` delegates to it (one definition). In `bash_side_effect._classify_program`, before the `_EXTERNAL_COMMANDS` check:

```python
    if _PYTHON.fullmatch(cmd) and "-c" in args:
        i = args.index("-c")
        code = args[i + 1] if i + 1 < len(args) else ""
        return "external" if code_reaches_network(code) else "write"
```

with `_PYTHON = re.compile(r"python(?:\d+(?:\.\d+)*)?")` at module level, so `python3.12 -c …` is read the same as `python3 -c …` (`cmd` is already basename/`.exe`-normalized by `_program()`). Import `code_reaches_network` from `nous.api.tool_classes` — leaf module, no cycle. Add to the Step 1 test: `assert classify_bash_command("python3.12 -c \"import smtplib\"") == "external"`.

Detection errs toward `external` by design: any URL literal or a bare `socket` mention in the code counts, even when nothing is sent. `uv run python -c …` is not unwrapped (`uv` is not in `_WRAPPERS`), so it stays at the `write` floor like any other script runner.

- [ ] **Step 4: Run** `uv run --frozen pytest tests/test_tool_classes.py tests/test_execution_ledger.py tests/test_execution_ledger_classes.py tests/test_execution_integrity.py tests/test_action_gate.py -q` → PASS (`test_external_tools_set` holds: EXTERNAL = {send_file, send_email}, IRREVERSIBLE empty).

- [ ] **Step 5: Commit**

```bash
git add nous/api/tool_classes.py nous/cognitive/execution_ledger.py nous/cognitive/bash_side_effect.py tests/test_tool_classes.py
git commit -m "feat(harness): declare every tool's class once; scripts that reach the network are external (2a)"
```

---

### Task 2: The refuse denylist strips every non-read tool

**Files:**
- Modify: `nous/api/tool_classes.py`; `nous/api/runner.py:1305-1309` (`stream_chat`) and `:1906-1913` (`_tool_loop`)
- Test: `tests/test_tool_classes.py`, `tests/test_runner_authorization.py`

**Interfaces:**
- Produces: `refuse_denylist() -> frozenset[str]` — every classified tool whose level is not `none`.

- [ ] **Step 1: Write the failing tests**

```python
from nous.api.tool_classes import refuse_denylist


def test_refuse_strips_every_non_read_tool():
    denied = refuse_denylist()
    assert {"dag_create", "push_surface", "compose_surface", "ingest_document",
            "resolve_decision", "spawn_sync", "send_email", "bash"} <= denied
    assert not denied & {"recall_deep", "read_file", "submit_final_report"}
```

In `tests/test_runner_authorization.py` (no test covers the strip today):

```python
@pytest.mark.asyncio
async def test_refuse_active_strips_previously_unclassified_tools():
    r, _ = _runner(["recall_deep", "dag_create"])
    sent: list[set[str]] = []

    async def capture(system_prompt, messages, tools=None, skip_thinking=False,
                      model_override=None, is_background=False):
        sent.append({t["name"] for t in tools or []})
        return ApiResponse(content=[{"type": "text", "text": "done"}], stop_reason="end_turn")

    r._call_api = capture
    await _run_loop(r, refuse_active=True)
    assert sent and "dag_create" not in sent[0] and "recall_deep" in sent[0]
```

(`ApiResponse` is already imported in this test module, where `_one_tool_call_then_done` uses it.)

- [ ] **Step 2: Run to verify they fail** — `ImportError: refuse_denylist`.

- [ ] **Step 3: Implement** in `tool_classes.py`:

```python
def refuse_denylist() -> frozenset[str]:
    """Tools an F078 refuse-tier censor strips: everything that is not a read."""
    return frozenset(name for name, cls in TOOL_CLASSES.items() if cls.side_effect != "none")
```

Both runner sites become `_refuse_denylist = refuse_denylist()`; drop now-unused set imports.

- [ ] **Step 4: Run** `uv run --frozen pytest tests/test_tool_classes.py tests/test_runner_authorization.py tests/test_execution_ledger_classes.py -q` → PASS.

- [ ] **Step 5: Commit**

```bash
git add nous/api/tool_classes.py nous/api/runner.py tests/test_tool_classes.py tests/test_runner_authorization.py
git commit -m "fix(harness): F078 refuse strips every classified non-read tool (2a)"
```

---

### Task 3: Declared tool lists and the check name reach the context

**Files:**
- Modify: `nous/api/execution_context.py` (`ExecutionContext` `:44-67`)
- Modify: `nous/heartbeat/dynamic.py:134-143`; `nous/heartbeat/runner.py:630-654`
- Test: `tests/test_execution_context.py`, `tests/test_heartbeat_dynamic.py`, `tests/test_heartbeat.py`

**Interfaces:**
- Produces: `ExecutionContext.declared_tools: tuple[str, ...] | None = None` (None = nothing declared; a non-empty tuple = the declaration); `ExecutionContext.check_name: str | None = None` (the check a callback belongs to).

`[]` is the DB default for a dynamic check's tools (`models.py:974`, `dynamic.py:73`) and means "all tools" today, so an empty list maps to `None` (undeclared), never to "nothing". The `tool_filter=None`-on-empty behavior is unchanged in this PR.

- [ ] **Step 1: Write the failing tests**

```python
def test_declared_tools_and_check_name_default_to_none():
    ctx = ExecutionContext(kind="heartbeat_check")
    assert ctx.declared_tools is None and ctx.check_name is None
```

In `tests/test_heartbeat_dynamic.py` next to `test_tool_filter_none_keeps_all` (`:1217`): a check with `tools=[]` runs with `context.declared_tools is None` and still `tool_filter=None`; `tools=["web_search"]` → `declared_tools == ("web_search",)`. In `tests/test_heartbeat.py`, the callback path with `on_complete_tools=["bash"]` → `context.declared_tools == ("bash",)` and `context.check_name == check.name`; with `on_complete_tools=[]` → `declared_tools is None`.

- [ ] **Step 2: Run to verify they fail.**

- [ ] **Step 3: Implement** — two fields after `surface_id`:

```python
    # Tools a heartbeat check or callback declared (Phase 2a policy). None:
    # nothing declared -- an empty list is the DB default and means "all".
    declared_tools: tuple[str, ...] | None = None
    check_name: str | None = None  # the check a callback belongs to
```

`dynamic.py`: `context=ExecutionContext(kind="heartbeat_check", session_id=session_id, declared_tools=tuple(self._tools) or None, check_name=self.name)`.
`heartbeat/runner.py`: `context=ExecutionContext(kind="heartbeat_callback", session_id=session_id, declared_tools=tuple(check.on_complete_tools or ()) or None, check_name=check.name)`.

- [ ] **Step 4: Run** `uv run --frozen pytest tests/test_execution_context.py tests/test_heartbeat_dynamic.py tests/test_heartbeat.py -q` → PASS.

- [ ] **Step 5: Commit**

```bash
git add nous/api/execution_context.py nous/heartbeat/dynamic.py nous/heartbeat/runner.py tests/test_execution_context.py tests/test_heartbeat_dynamic.py tests/test_heartbeat.py
git commit -m "feat(harness): checks and callbacks carry their declared tools and check name (2a)"
```

---

### Task 4: `CONTEXT_POLICY` + `evaluate()`

**Files:**
- Create: `nous/api/tool_policy.py`
- Test: `tests/test_tool_policy.py` (new)

**Interfaces:**
- Consumes: `tool_class` (Task 1); `ExecutionContext.declared_tools/check_name` (Task 3); `classify_side_effect`.
- Produces: `@dataclass(frozen=True) class ContextPolicy: levels: frozenset[str]; spawn: bool | frozenset[str]`; `CONTEXT_POLICY: Mapping[str, ContextPolicy]`; `evaluate(ctx, tool_name, tool_input) -> str | None`.

- [ ] **Step 1: Write the failing tests**

```python
"""Harness Phase 2a: one table decides what each execution context may do."""

import pytest

from nous.api.execution_context import CONTEXT_KINDS, ExecutionContext
from nous.api.tool_policy import CONTEXT_POLICY, evaluate


def _ctx(kind, **kw):
    return ExecutionContext(kind=kind, **kw)


def test_every_context_kind_has_a_policy():
    assert set(CONTEXT_POLICY) == set(CONTEXT_KINDS)


@pytest.mark.parametrize("kind", ["interactive", "mcp"])
def test_foreground_may_do_anything_classified(kind):
    assert evaluate(_ctx(kind), "dag_create", {}) is None
    assert evaluate(_ctx(kind), "send_email", {}) is None
    assert evaluate(_ctx(kind), "brand_new_tool", {}) is None


@pytest.mark.parametrize("kind", ["subtask", "dag_node", "scheduled", "agent_action"])
def test_background_work_may_send_but_not_spawn(kind):
    assert evaluate(_ctx(kind), "spawn_task", {}) == "spawn"
    assert evaluate(_ctx(kind), "send_email", {}) is None


def test_dag_summary_may_spawn_only_the_delivery_subtask():
    assert evaluate(_ctx("dag_summary"), "spawn_task", {}) is None
    assert evaluate(_ctx("dag_summary"), "schedule_task", {}) == "spawn"
    assert evaluate(_ctx("dag_summary"), "dag_create", {}) == "spawn"


def test_triage_may_not_send_by_any_route():
    ctx = _ctx("heartbeat_triage")
    assert evaluate(ctx, "send_email", {}) == "level:external"
    assert evaluate(ctx, "bash", {"command": "curl https://x"}) == "level:external"
    assert evaluate(ctx, "run_python", {"code": "import smtplib"}) == "level:external"
    assert evaluate(ctx, "bash", {"command": "ls"}) is None


def test_a_check_may_use_only_what_it_declared():
    ctx = _ctx("heartbeat_check", declared_tools=("web_search",))
    assert evaluate(ctx, "web_search", {}) is None
    assert evaluate(ctx, "bash", {"command": "ls"}) == "undeclared"


def test_an_undeclared_check_falls_back_to_the_level_rules():
    ctx = _ctx("heartbeat_check", declared_tools=None)
    assert evaluate(ctx, "recall_deep", {}) is None
    assert evaluate(ctx, "spawn_task", {}) == "spawn"


def test_a_declared_spawn_tool_is_the_check_pipeline():
    ctx = _ctx("heartbeat_check", declared_tools=("heartbeat_check_create",))
    assert evaluate(ctx, "heartbeat_check_create", {"name": "step-2"}) is None


def test_a_callback_may_not_re_enable_its_own_check():
    ctx = _ctx("heartbeat_callback", declared_tools=("heartbeat_check_manage",), check_name="watch-ci")
    assert evaluate(ctx, "heartbeat_check_manage", {"action": "enable", "name": "watch-ci"}) == "reenable"
    assert evaluate(ctx, "heartbeat_check_manage", {"action": "enable", "name": "other"}) is None
    assert evaluate(ctx, "heartbeat_check_manage", {"action": "disable", "name": "watch-ci"}) is None


def test_unclassified_tools_are_denied_in_background():
    assert evaluate(_ctx("subtask"), "brand_new_tool", {}) == "unclassified"


def test_submit_final_report_is_allowed_in_hardened_subtasks():
    assert evaluate(_ctx("dag_node"), "submit_final_report", {}) is None
```

- [ ] **Step 2: Run to verify they fail** — `ModuleNotFoundError: nous.api.tool_policy`.

- [ ] **Step 3: Implement** `nous/api/tool_policy.py`:

```python
"""What each execution context may do (harness Phase 2a).

One table, consulted once per call at AgentRunner._authorize_tool_call. Rows
are the INTENDED policy; in `warn` mode a deviation is logged and persisted as
`harness_context_policy_violation` and the call still runs.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from nous.api.execution_context import FOREGROUND_KINDS, ExecutionContext
from nous.api.tool_classes import tool_class
from nous.cognitive.execution_ledger import classify_side_effect

_ALL = frozenset({"none", "write", "external", "irreversible"})
_WORK = frozenset({"none", "write", "external"})
_LOCAL = frozenset({"none", "write"})


@dataclass(frozen=True)
class ContextPolicy:
    levels: frozenset[str]          # side-effect levels this context may use
    spawn: bool | frozenset[str]    # True: any spawn tool; a set: only these; False: none


CONTEXT_POLICY: Mapping[str, ContextPolicy] = MappingProxyType({
    "interactive": ContextPolicy(_ALL, spawn=True),
    "mcp": ContextPolicy(_ALL, spawn=True),
    "subtask": ContextPolicy(_WORK, spawn=False),
    "dag_node": ContextPolicy(_WORK, spawn=False),
    "scheduled": ContextPolicy(_WORK, spawn=False),
    "agent_action": ContextPolicy(_WORK, spawn=False),
    # Prod delivers DAG results by email through spawn_task from this turn:
    # that one spawn tool is allowed explicitly, the rest are not (roadmap §4).
    "dag_summary": ContextPolicy(_WORK, spawn=frozenset({"spawn_task"})),
    "heartbeat_triage": ContextPolicy(_LOCAL, spawn=False),
    # A check/callback may spawn only what it declared (check pipelines
    # declare heartbeat_check_create) -- see evaluate().
    "heartbeat_check": ContextPolicy(_WORK, spawn=False),
    "heartbeat_callback": ContextPolicy(_WORK, spawn=False),
    "background": ContextPolicy(_LOCAL, spawn=False),
})


def evaluate(ctx: ExecutionContext, tool_name: str, tool_input: Mapping[str, Any]) -> str | None:
    """None if ``ctx`` may make this call; otherwise the violation code."""
    if ctx.kind in FOREGROUND_KINDS:
        return None
    policy = CONTEXT_POLICY[ctx.kind]
    cls = tool_class(tool_name)
    if cls is None:
        return "unclassified"
    declared = ctx.declared_tools
    if declared is not None and tool_name not in declared:
        return "undeclared"
    level = classify_side_effect(tool_name, dict(tool_input))
    if level not in policy.levels:
        return f"level:{level}"
    if cls.spawns and not (
        policy.spawn is True
        or (isinstance(policy.spawn, frozenset) and tool_name in policy.spawn)
        or (declared is not None and tool_name in declared)
    ):
        return "spawn"
    if (ctx.kind == "heartbeat_callback" and tool_name == "heartbeat_check_manage"
            and str(tool_input.get("action", "")).lower() == "enable"
            and ctx.check_name is not None
            and str(tool_input.get("name", "")).strip().lower() == ctx.check_name.lower()):
        return "reenable"
    return None
```

- [ ] **Step 4: Run** `uv run --frozen pytest tests/test_tool_policy.py -q` → PASS.

- [ ] **Step 5: Commit**

```bash
git add nous/api/tool_policy.py tests/test_tool_policy.py
git commit -m "feat(harness): per-context tool policy table (2a)"
```

---

### Task 5: The choke point consults the policy (`warn` default)

**Files:**
- Modify: `nous/config.py` (after `tool_offered_set_enforcement_mode` `:987`)
- Modify: `nous/api/runner.py` `_authorize_tool_call` `:250-283` and its call sites (`stream_chat` `:1606-1628`, `_tool_loop` `:2112-2133`)
- Modify: `nous/cognitive/ledger_store.py` `REFUSAL_CODES`
- Test: `tests/test_runner_authorization.py`

**Interfaces:**
- Consumes: `evaluate` (Task 4).
- Produces: setting `tool_context_policy_mode: Literal["off","warn","enforce"] = "warn"`; `@dataclass(frozen=True) class Refusal: text: str; code: str` in `runner.py`; `_authorize_tool_call(ctx, tool_name, offered_names, session_id, tool_input) -> Refusal | None`; event `harness_context_policy_violation {tool_name, context_kind, violation, mode}`; `REFUSAL_CODES = frozenset({"offered_set", "action_gate", "context_policy"})`.

- [ ] **Step 1: Write the failing tests** (in `tests/test_runner_authorization.py`)

```python
@pytest.mark.asyncio
async def test_policy_warn_runs_an_offered_call_and_records_it():
    r, d = _runner(["send_email"], tool_context_policy_mode="warn")
    events = []
    r._log_f026_decision = lambda kind, data, session_id: events.append((kind, data))
    r._call_api = _one_tool_call_then_done("send_email")
    await _run_loop(r, is_background=True, context=ExecutionContext(kind="heartbeat_triage"))
    assert [c[0] for c in d.calls] == ["send_email"]
    assert ("harness_context_policy_violation",
            {"tool_name": "send_email", "context_kind": "heartbeat_triage",
             "violation": "level:external", "mode": "warn"}) in events


@pytest.mark.asyncio
async def test_policy_enforce_refuses_and_records_a_blocked_row():
    from tests.test_runner_ledger import _FakeStore

    store = _FakeStore()
    r, d = _runner(["spawn_task"], tool_context_policy_mode="enforce")
    r.set_ledger_store(store)
    r._call_api = _one_tool_call_then_done("spawn_task")
    await _run_loop(r, is_background=True, context=ExecutionContext(kind="dag_node"))
    assert d.calls == []
    assert store.events == [("blocked", "spawn_task", "context_policy")]


@pytest.mark.asyncio
async def test_policy_off_is_silent():
    r, d = _runner(["spawn_task"], tool_context_policy_mode="off")
    events = []
    r._log_f026_decision = lambda kind, data, session_id: events.append(kind)
    r._call_api = _one_tool_call_then_done("spawn_task")
    await _run_loop(r, is_background=True, context=ExecutionContext(kind="dag_node"))
    assert [c[0] for c in d.calls] == ["spawn_task"] and events == []


@pytest.mark.asyncio
async def test_an_unoffered_call_is_checked_by_both_rules():
    r, d = _runner(["recall_deep", "spawn_task"],
                   tool_offered_set_enforcement_mode="warn", tool_context_policy_mode="warn")
    events = []
    r._log_f026_decision = lambda kind, data, session_id: events.append(kind)
    r._call_api = _one_tool_call_then_done("spawn_task")
    await _run_loop(r, is_background=True, tool_filter=["recall_deep"],
                    context=ExecutionContext(kind="dag_node"))
    assert events == ["harness_unoffered_tool_call", "harness_context_policy_violation"]


def test_policy_setting_defaults_to_warn():
    assert _settings().tool_context_policy_mode == "warn"
```

- [ ] **Step 2: Run to verify they fail.**

- [ ] **Step 3: Implement**

`nous/config.py`:

```python
    # Harness Phase 2a: what happens when a call breaks the per-context policy
    # in nous/api/tool_policy.py (heartbeat triage sending email, a DAG node
    # spawning, a callback re-enabling its own check, a check using a tool it
    # did not declare). off = no check; warn = run it, log WARNING, persist
    # harness_context_policy_violation; enforce = refuse with a blocked row.
    tool_context_policy_mode: Literal["off", "warn", "enforce"] = "warn"
```

`runner.py` — the whole function (both rules run for every call; each returns early only when *it* refuses):

```python
@dataclass(frozen=True)
class Refusal:
    text: str
    code: str  # a nous.cognitive.ledger_store.REFUSAL_CODES entry


    def _authorize_tool_call(
        self, ctx: ExecutionContext, tool_name: str, offered_names: frozenset[str],
        session_id: str | None, tool_input: dict,
    ) -> Refusal | None:
        """Return a refusal for a call the harness must not execute, else None.

        The single choke point both loops call before gating and dispatch:
        the offered-set rule (Phase 1a), then the per-context policy (2a).
        """
        offered_mode = self._settings.tool_offered_set_enforcement_mode
        if offered_mode != "off" and tool_name not in offered_names:
            logger.warning(
                "Harness: %s unoffered tool call %r (context=%s, session=%s)",
                "refused" if offered_mode == "enforce" else "allowed (warn mode)",
                tool_name, ctx.kind, session_id,
            )
            self._log_f026_decision(
                "harness_unoffered_tool_call",
                {"tool_name": tool_name, "context_kind": ctx.kind,
                 "mode": offered_mode, "offered_count": len(offered_names)},
                session_id=session_id,
            )
            if offered_mode == "enforce":
                return Refusal(
                    f"Tool error: '{tool_name}' is not available in this turn. "
                    "Use only the tools offered to you.", "offered_set")

        policy_mode = self._settings.tool_context_policy_mode
        if policy_mode == "off":
            return None
        violation = tool_policy.evaluate(ctx, tool_name, tool_input)
        if violation is None:
            return None
        logger.warning(
            "Harness: %s %r in a %s turn breaks the context policy (%s)",
            "refused" if policy_mode == "enforce" else "allowed (warn mode)",
            tool_name, ctx.kind, violation,
        )
        self._log_f026_decision(
            "harness_context_policy_violation",
            {"tool_name": tool_name, "context_kind": ctx.kind,
             "violation": violation, "mode": policy_mode},
            session_id=session_id,
        )
        if policy_mode != "enforce":
            return None
        return Refusal(
            f"Tool error: '{tool_name}' is not allowed in a {ctx.kind} turn ({violation}).",
            "context_policy")
```

Both call sites pass `tool_input` (`tc.get("input", {})` in `stream_chat`, `tool_input` in `_tool_loop`), use `refusal.text` where they used the string, and pass `refusal.code` to `_ledger_blocked`. Import `from nous.api import tool_policy`.

- [ ] **Step 4: Run** `uv run --frozen pytest tests/test_runner_authorization.py tests/test_runner_ledger.py tests/test_ledger_store.py tests/test_streaming.py tests/test_runner.py -q` → PASS.

- [ ] **Step 5: Commit**

```bash
git add nous/config.py nous/api/runner.py nous/cognitive/ledger_store.py tests/test_runner_authorization.py
git commit -m "feat(harness): the choke point consults the context policy, warn by default (2a)"
```

---

### Task 6: Docs

**Files:** `CLAUDE.md` (after `NOUS_TOOL_OFFERED_SET_ENFORCEMENT_MODE`)

- [ ] **Step 1:** Add `| \`NOUS_TOOL_CONTEXT_POLICY_MODE\` | \`warn\` | Harness Phase 2a: per-context tool policy (\`nous/api/tool_policy.py\`), consulted at the same choke point as the offered-set check. Every tool's class (none/write/external/irreversible + spawns) is declared once in \`nous/api/tool_classes.py\`; the ledger's sets and the F078 refuse denylist derive from it, a drift test fails on an unclassified registration, and \`run_python\`/\`python -c\` code that reaches the network classes as external. Rows: foreground may do anything; subtask/dag_node/scheduled/agent_action may send but not spawn; dag_summary may spawn only \`spawn_task\` (prod delivers email through it); heartbeat triage and generic background are local-only; a check/callback with a declared tool list may use only those tools (declaring \`heartbeat_check_create\` authorizes a pipeline), and a callback may not re-enable its own check. \`off\` = no check; \`warn\` = run it, log WARNING, persist \`harness_context_policy_violation\`; \`enforce\` = refuse with a \`blocked\` ledger row (code \`context_policy\`). Ships \`warn\`: flip after reading the events. |`

- [ ] **Step 2: Commit** `git add CLAUDE.md && git commit -m "docs: per-context tool policy (harness 2a)"`

---

## Out of scope (recorded for a 2a follow-up, roadmap §4)

- Spawn censor gate on `schedule_task` create/fire and DAG node launch — censor semantics, separate review.
- Heartbeat triage skipping input censors (`cognitive/layer.py:910`).
- Offering nothing for an empty declared list at the source (`dynamic.py:139`, `heartbeat/runner.py:630`) — an empty list is the DB default and means "all" today; changing it needs the warn data.
- `python script.py` in bash (the script is not inspectable) — classified `write`.
- Reading `ActionRouter._HandlerMeta.irreversible` — belongs with P2.8 compensation.
