# Harness Autonomy — Phase 0 + Phase 1 Implementation Plan (v2.1)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the send-path risk prod actually shows, give every tool call a known execution context, measure (then refuse) tool calls the model was not offered, and persist every side-effecting tool call as a durable ledger row that exists before the side effect.

**Architecture:** Phase 0 fixes two send-path defects in place. Phase 1a adds a frozen `ExecutionContext` built by each turn's caller and threaded `run_turn → _tool_loop → ToolDispatcher.dispatch` (`stream_chat` builds an `interactive` one), plus one `_authorize_tool_call` choke point in both loops that measures (`warn`) or refuses (`enforce`) unoffered names. Phase 1b adds `LedgerStore` writing `nous_system.execution_ledger` rows — `pending` before dispatch, closed after — with a durable, redaction-first argument summarizer.

**Tech Stack:** Python 3.12+, SQLAlchemy 2 async ORM, asyncpg, PostgreSQL 17, pytest + pytest-asyncio.

**Spec:** `docs/superpowers/plans/2026-09-24-harness-autonomy-roadmap.md` (§1 findings, §3 PRs 0/1a/1b).

## Global Constraints

- Three PRs, in order: **PR 0** (Tasks 0.1–0.2), **PR 1a** (Tasks 1–5), **PR 1b** (Tasks 6–10). Each branches from `main` after the previous merges.
- Migration number is **074** (`073` is taken on unmerged branch `fix/retire-stale-calibration-factor`).
- No `;` inside `--` SQL comments; CI applies every migration with `psql -f` to a fresh Postgres.
- Every new table has `agent_id`; every sweep is agent-scoped.
- **New settings are plain pydantic fields** (`env_prefix="NOUS_"` already maps `NOUS_<UPPER_NAME>`), **not** `validation_alias` fields — `Settings` has no `populate_by_name`, so an aliased field ignores its field name as a constructor kwarg and tests that set it by name silently test the default. Tests construct `Settings(..., _env_file=None)`.
- Deployment assumption, written into migration 074 and CLAUDE.md: **one Nous process per `(database, agent_id)`** (docker-compose runs one `nous` service; eval/faculty instances use another DB and agent id). The startup ledger sweep depends on it.
- CI (Postgres) is the gate. Locally `uv run pytest` runs on SQLite; eight tests in `test_tools.py`/`test_brain.py` fail on `main` for pgvector reasons and are not regressions.
- Durable-ledger rows are the source of truth for Phase 2b/2c; the in-memory session ledger stays a prompt aid.

## Review v1 → v2 (3-agent review: architect / database / devil's advocate)

| # | Finding (who) | Disposition |
|---|---|---|
| 1 | `DispatchOutcome` breaks `tests/test_streaming_keepalive.py` (arch P1, devil P3) | Task 8 updates that file and runs it |
| 2 | `execution_ledger_task` not in `create_components` return dict → never cancelled (arch P1) | Task 9 adds keys + shutdown block |
| 3 | Durable ledger stores secrets: redaction is bash-only; `run_python` code / email body captured by the 5-arg fallback; truncate-then-redact breaks the URL pattern (db P1, devil P2) | New `durable_key_args` (Task 7): per-tool allowlist, hashes for bodies/code, names-only for unknown tools, redact-then-truncate, all tools + `result_summary` |
| 4 | `validation_alias` settings ignore field-name kwargs → kill-switch/bounds tests test defaults (db P2, devil P2) | Plain fields; Global Constraint rewritten |
| 5 | Sleep-first maintenance loop never runs under daily restarts (arch, db, devil) | Startup prune + orphan sweep, then `first=True` loop; orphan sweep every 30 min |
| 6 | Side-effect classes stale: `send_email` stored as `write`, `recall_hubs`/`list_decisions` persisted as writes (db, devil) | **PR 0** fixes the sets (one line each) |
| 7 | Enforcement ON with zero measurement; prod callbacks (`on_complete_tools=['bash']`, prompt "notify via Telegram") rely on the hole and would fail silently (devil P2) | Mode setting `off|warn|enforce`, default **warn**; warn persists a `harness_unoffered_tool_call` event; flip after data |
| 8 | `for_subtask` hides origin: prod has summary-turn-spawned email subtasks (devil P2) | `ExecutionContext.parent_session_id` from `subtask.parent_session_id`; ledger column |
| 9 | MCP labelled interactive (devil P3) | New `mcp` kind, foreground (a caller is waiting); `FOREGROUND_KINDS` |
| 10 | Substring test proves nothing; runtime wiring untested (devil P2) | Removed; caller tests assert `run_turn` kwargs; one test through `run_turn`; AST sweep requires `context=` everywhere, no exemptions |
| 11 | Success-path close unshielded; stuck `pending` ~26 h (db, devil) | Every close shielded via a strong-ref task set; 30-min sweep |
| 12 | Insert timeout after COMMIT → phantom row with no id (db P2) | Client-generated id returned even on failure; store raises `LedgerWriteError(entry_id)`, runner owns fail-open policy |
| 13 | Store swallows everything → Phase 2b can't tell "not side-effecting" from "write failed", would swallow unique violations (db P2) | `open_entry` returns `None` only for reads; raises on failure; policy in runner |
| 14 | `make_interval` not SQLite-portable (arch, db) | Python cutoff is the implementation |
| 15 | Missing imports: `NamedTuple` in runner, `datetime` in main (arch P2) | Called out in Tasks 8/9 |
| 16 | Wrong fixture pointer: streaming fixtures live in `tests/test_streaming.py` (arch, devil) | Fixed |
| 17 | Probe tool registered with a wrapped schema (arch P2) | Registers the JSON-Schema body |
| 18 | Test isolation: session-scoped DB + fixed agent id; unordered `[-1]` (db, devil) | uuid agent per test; `ORDER BY` |
| 19 | `reason[:20]` off-by-one (devil P3) | Test asserts `startswith` |
| 20 | `session_id VARCHAR(200)` can drop a row; speculative `dag_node_id` index (db P3) | `TEXT`; index dropped |
| 21 | App vs DB clock (db P3) | Startup sweep = "all pending rows" (no cutoff); `completed_at = now()` DB-side |
| 22 | 7800 s threshold hardcodes today's timeouts (db P3) | Effective threshold = `max(setting, dag_node_max_timeout, subtask_max_timeout, tool_timeout) + 600` computed at use |
| 23 | Self-review risk "can't await in `except GeneratorExit`" is wrong on 3.14 (devil P3) | Removed; shielded await is the implementation |
| 24 | Phase 0 before 1a: SMTP timeout, `send_email` → external (devil) | New PR 0. **Not adopted:** DAG node `max_attempts: 1` (removes one of two retry layers — the DAG fix stage re-sends too — at a reliability cost; Phase 2b's key covers every layer). **Deferred to 2a:** stripping spawn tools from `dag_summary` turns — prod shows those turns spawning email sends, so it is a behavior change the policy table must decide explicitly |
| 25 | Owner's late close vs sweep-set `unknown` (db P3) | Close accepts `pending` or `unknown` |
| 26 | Build row outside `try` (db P3) | Inside |

### Re-review v2 → v2.1

| # | Finding | Fix |
|---|---|---|
| R1 | `_log_f026_decision` fires `asyncio.create_task`, so `emit_event` is *called* but not yet *awaited* when `_run_loop` returns — `assert_awaited` fails and `assert_not_awaited` passes vacuously | Tests use `assert_called()` / `call_args.args[:2]` / `assert_not_called()` |
| R2 | `fork(self, api_client)` takes an argument | `r.fork(MagicMock())` |
| R3 | A non-dict `tool_input` does not raise in `durable_key_args` (`in` on a str is a substring test) | `_insert` rejects non-dict input first thing inside its `try` |
| R4 | `tests/test_streaming.py:770,802` assert exact `dispatch` kwargs | Task 2 adds `context=ANY` there |
| R5 | The `-p` pattern mangles `find -path`, `cp -pr` and redacts the wrong token of `openssl -passin` | `-p<secret>` redaction scoped to the mysql family and `sshpass`; `Authorization` consumes an optional scheme word; `--api-key`/`--token`, JSON `"password":` added; benign-command test added |
| R6 | `last_prune=0.0` vs monotonic `loop.time()` skips the startup prune on young hosts; two contradictory startup-sweep versions; unguarded inline sweep | One version: inline awaited, guarded, bounded startup sweep; the loop prunes when `last_prune is None` |
| R7 | `_DURABLE_ARGS` names that do not exist (`learn_skill`, `create_censor`, `schedule_task`, `write_file.file_path`) | Real names: `learn_skill`→`source`/`content`; `create_censor`→`domain`,`action`/`reason`,`trigger_pattern`; `schedule_task`→`every`,`when`; `write_file`→`path` |
| R8 | `stream_chat` snippet drops `result_text, is_error = "", False` and `start_time` | Kept |
| R9 | `_ledger_open` after `_start_activity_heartbeat` leaks `_hb` on a cancel during the insert | `_ledger_open` runs before `_hb` starts |

---

## File Structure

| File | PR | Responsibility |
|---|---|---|
| `nous/api/email_tools.py`, `nous/config.py` | 0 | SMTP timeout |
| `nous/cognitive/execution_ledger.py` | 0, 1b | side-effect sets; `summarize_args` hoist |
| `nous/api/execution_context.py` (create) | 1a | `ExecutionContext`, `ContextKind`, `CONTEXT_KINDS`, `FOREGROUND_KINDS`, `resolve_context` |
| `nous/api/runner.py` | 1a, 1b | thread context; `_authorize_tool_call`; ledger brackets |
| `nous/api/tools.py` | 1a | `dispatch(..., context=)`; `_BACKGROUND_AWARE_TOOLS`; inline spawn passes context |
| `nous/api/rest.py`, `nous/api/mcp.py`, `nous/handlers/subtask_worker.py`, `nous/handlers/subtask_executor.py`, `nous/heartbeat/runner.py`, `nous/heartbeat/dynamic.py`, `nous/dag/delivery.py` | 1a | pass contexts |
| `nous/cognitive/ledger_store.py` (create) | 1b | `LedgerStore`, `durable_key_args`, `LedgerWriteError` |
| `nous/storage/models.py` | 1b | `ExecutionLedgerEntry` |
| `sql/migrations/074_execution_ledger.sql` (create) | 1b | table + indexes |
| `nous/main.py` | 1b | wiring, sweeps, shutdown |
| `CLAUDE.md` | 0, 1a, 1b | env-var rows, table list, deployment assumption |
| tests (create): `test_email_smtp_timeout.py`, `test_execution_context.py`, `test_runner_authorization.py`, `test_ledger_store.py`, `test_runner_ledger.py` | | |
| tests (modify): `test_runner.py`, `test_runner_background.py`, `test_streaming_keepalive.py`, caller tests for heartbeat/dynamic/delivery/subtask/rest/mcp | | |

---

# PR 0 — Send-path fixes (no dependencies)

Branch: `fix/harness-phase0-send-path` from `origin/main`.

### Task 0.1: SMTP gets a timeout

**Files:** Modify `nous/config.py` (next to `email_smtp_port`, ~line 1538), `nous/api/email_tools.py:615`. Test: `tests/test_email_smtp_timeout.py`.

- [ ] **Step 1: Failing test**

```python
# tests/test_email_smtp_timeout.py
"""Harness Phase 0: the SMTP connection is bounded.

smtplib.SMTP had no timeout and nothing set socket.setdefaulttimeout, so a
hung SMTP server held a to_thread worker forever — and a caller's timeout
cancelled only the await, not the thread, which could still deliver later.
"""

from unittest.mock import MagicMock, patch

from nous.api.email_tools import _send_email_sync
from nous.config import Settings


def test_smtp_connection_is_opened_with_the_configured_timeout():
    settings = Settings(_env_file=None, email_user="u", email_password="p", email_smtp_timeout_seconds=17)
    with patch("nous.api.email_tools.smtplib.SMTP") as smtp:
        smtp.return_value = MagicMock()
        _send_email_sync(settings, ["a@example.com"], MagicMock())
    assert smtp.call_args.kwargs["timeout"] == 17


def test_default_timeout_is_finite():
    assert 0 < Settings(_env_file=None).email_smtp_timeout_seconds <= 120
```

- [ ] **Step 2:** `uv run pytest tests/test_email_smtp_timeout.py -q` → FAIL (`KeyError: 'timeout'` / unknown setting).
- [ ] **Step 3: Implement**

`nous/config.py`, after `email_smtp_port`:

```python
    # Harness Phase 0: bound every SMTP socket operation. Without it a hung
    # server held the to_thread worker forever, and a caller-side timeout only
    # cancelled the await — the thread could still deliver afterwards.
    email_smtp_timeout_seconds: float = Field(default=30.0, gt=0)
```

`nous/api/email_tools.py`:

```python
    server = smtplib.SMTP(
        settings.email_smtp_host, settings.email_smtp_port,
        timeout=settings.email_smtp_timeout_seconds,
    )
```

- [ ] **Step 4:** tests pass; `uv run pytest tests/ -q -k email` green vs baseline.
- [ ] **Step 5:** commit `fix(email): bound the SMTP connection with a timeout (harness Phase 0)`.

### Task 0.2: Side-effect classes match the registered tools

**Files:** Modify `nous/cognitive/execution_ledger.py` (`READ_TOOLS`, `EXTERNAL_TOOLS`, `_KEY_ARGS`). Test: `tests/test_execution_ledger_classes.py` (create).

- [ ] **Step 1: Failing test**

```python
# tests/test_execution_ledger_classes.py
"""Harness Phase 0: side-effect classes for tools registered after F026."""

from nous.cognitive.execution_ledger import (
    EXTERNAL_TOOLS, READ_TOOLS, WRITE_TOOLS, classify_side_effect,
)


def test_send_email_is_external():
    assert classify_side_effect("send_email", {"to": "a@b.c"}) == "external"


def test_pure_decision_and_graph_reads_are_reads():
    assert classify_side_effect("recall_hubs", {}) == "none"
    assert classify_side_effect("list_decisions", {}) == "none"


def test_f078_refuse_denylist_now_strips_send_email():
    """runner.py builds the refuse denylist from these sets."""
    assert "send_email" in (WRITE_TOOLS | EXTERNAL_TOOLS)


def test_sets_are_disjoint():
    assert not (READ_TOOLS & WRITE_TOOLS)
    assert not (READ_TOOLS & EXTERNAL_TOOLS)
    assert not (WRITE_TOOLS & EXTERNAL_TOOLS)
```

- [ ] **Step 2:** run → FAIL on the first two tests.
- [ ] **Step 3: Implement**

```python
READ_TOOLS: set[str] = {
    ...existing members...,
    "recall_hubs",
    "list_decisions",
}

# External side effects — leave the host (message delivery, remote pushes)
EXTERNAL_TOOLS: set[str] = {
    "send_file",   # Sends files to Telegram
    "send_email",  # Guarded SMTP send (email_tools.py) — registered after F026
}
```

and in `_KEY_ARGS` add `"send_email": ["to", "cc", "subject"],` (the in-memory session ledger
and ActionGate's duplicate check then key on recipient + subject instead of the 5-arg fallback
that captured `body[:80]`).

- [ ] **Step 4:** `uv run pytest tests/test_execution_ledger_classes.py tests/ -q -k "ledger or action_gate or claim or refuse or censor"` green vs baseline.
- [ ] **Step 5:** commit `fix(ledger): send_email is external; recall_hubs/list_decisions are reads (harness Phase 0)`; CLAUDE.md row for `NOUS_EMAIL_SMTP_TIMEOUT_SECONDS`; PR; codex; CI; merge.

---

# PR 1a — ExecutionContext + offered-set measurement/enforcement

Branch: `feat/harness-phase1a-execution-context` from `origin/main` after PR 0 merges (rebase the
plan-docs commit onto it).

### Task 1: `ExecutionContext` value type

**Files:** Create `nous/api/execution_context.py`; test `tests/test_execution_context.py`.

**Interfaces — Produces:** `ExecutionContext(kind, session_id=None, parent_session_id=None, subtask_id=None, dag_id=None, dag_node_id=None, dag_node_name=None, schedule_id=None, surface_id=None)` (frozen, slots); `.is_background -> bool`; `ExecutionContext.for_subtask(subtask, session_id) -> ExecutionContext`; `resolve_context(context, *, is_background, session_id) -> ExecutionContext`; `CONTEXT_KINDS`, `FOREGROUND_KINDS`.

- [ ] **Step 1: Failing tests**

```python
# tests/test_execution_context.py
"""Harness Phase 1a: every turn knows which harness path runs it."""

import uuid
from types import SimpleNamespace

import pytest

from nous.api.execution_context import (
    CONTEXT_KINDS, FOREGROUND_KINDS, ExecutionContext, resolve_context,
)


def _subtask(*, metadata=None, dag_node_id=None, sid=None, parent=None):
    return SimpleNamespace(
        id=sid or uuid.uuid4(),
        metadata_=metadata if metadata is not None else {},
        dag_node_id=dag_node_id,
        parent_session_id=parent,
    )


def test_foreground_kinds_are_exactly_interactive_and_mcp():
    assert FOREGROUND_KINDS == frozenset({"interactive", "mcp"})
    for kind in CONTEXT_KINDS:
        assert ExecutionContext(kind=kind).is_background is (kind not in FOREGROUND_KINDS)


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
        ({"a2ui_surface_id": "s1", "a2ui_action_id": "rebalance", "max_attempts": 1}, False, "agent_action"),
        ({"schedule_id": "ab12"}, False, "scheduled"),
        ({"schedule_id": "ab12", "session_id": "schedule-ab12"}, False, "scheduled"),
        ({}, False, "subtask"),
    ],
)
def test_for_subtask_derives_kind_from_the_row(metadata, has_node, expected):
    node_id = uuid.uuid4() if has_node else None
    ctx = ExecutionContext.for_subtask(_subtask(metadata=metadata, dag_node_id=node_id), "subtask-1")
    assert ctx.kind == expected and ctx.session_id == "subtask-1"


def test_for_subtask_carries_ids_and_the_spawning_session():
    dag_id, node_id, sid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    ctx = ExecutionContext.for_subtask(
        _subtask(metadata={"dag_id": str(dag_id), "node_name": "send"},
                 dag_node_id=node_id, sid=sid, parent="dag-summary-1a2b3c4d"),
        "subtask-x",
    )
    assert (ctx.subtask_id, ctx.dag_id, ctx.dag_node_id, ctx.dag_node_name) == (sid, dag_id, node_id, "send")
    assert ctx.parent_session_id == "dag-summary-1a2b3c4d"


def test_for_subtask_tolerates_legacy_rows():
    assert ExecutionContext.for_subtask(SimpleNamespace(id=uuid.uuid4()), "s").kind == "subtask"
    row = SimpleNamespace(id=uuid.uuid4(), metadata_=None, dag_node_id=None)
    assert ExecutionContext.for_subtask(row, "s").kind == "subtask"
    ctx = ExecutionContext.for_subtask(_subtask(metadata={"dag_id": "not-a-uuid"}), "s")
    assert ctx.kind == "dag_node" and ctx.dag_id is None


def test_resolve_context_defaults():
    assert resolve_context(None, is_background=False, session_id="s").kind == "interactive"
    bg = resolve_context(None, is_background=True, session_id="s")
    assert bg.kind == "background" and bg.session_id == "s"


def test_resolve_context_passes_an_explicit_context_through():
    ctx = ExecutionContext(kind="heartbeat_triage", session_id="h")
    assert resolve_context(ctx, is_background=True, session_id="other") is ctx
    assert resolve_context(ctx, is_background=False, session_id="h") is ctx


def test_resolve_context_rejects_a_contradiction():
    with pytest.raises(ValueError, match="contradicts"):
        resolve_context(ExecutionContext(kind="interactive"), is_background=True, session_id="s")
    with pytest.raises(ValueError, match="contradicts"):
        resolve_context(ExecutionContext(kind="mcp"), is_background=True, session_id="s")
```

- [ ] **Step 2:** `uv run pytest tests/test_execution_context.py -q` → collection error (module missing).
- [ ] **Step 3: Implement**

```python
# nous/api/execution_context.py
"""Which harness path is running a turn (harness-autonomy roadmap, Phase 1a).

Before this module the only context signal reaching tool dispatch was
``is_background: bool`` — heartbeat triage, dynamic checks, callbacks,
schedules, DAG nodes, companion agent actions and spawn_task all collapsed to
the same value. A capability policy (Phase 2a), an idempotency key (Phase 2b)
and the durable ledger (Phase 1b) each need to know WHICH path is running, so
the caller that starts a turn says so once, here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, get_args
from uuid import UUID

ContextKind = Literal[
    "interactive",         # REST /chat, /chat/stream — a person is in the loop
    "mcp",                 # MCP tool call — another agent is waiting on the answer
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
# A caller is waiting on the turn. Everything else runs with nobody in the loop.
FOREGROUND_KINDS: frozenset[str] = frozenset({"interactive", "mcp"})


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
    # The session that CREATED this turn's subtask (heart.subtasks.parent_session_id).
    # Prod has subtasks spawned by dag-summary turns; without this their origin
    # is invisible and they look like plain spawns.
    parent_session_id: str | None = None
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
        return self.kind not in FOREGROUND_KINDS

    @classmethod
    def for_subtask(cls, subtask: Any, session_id: str) -> ExecutionContext:
        """Derive the context of a subtask turn from its ``heart.subtasks`` row.

        The row says who created it: the DAG orchestrator stamps ``dag_node_id``
        + ``metadata.dag_id``/``node_name``; companion agent actions stamp
        ``metadata.a2ui_surface_id``; the scheduler stamps
        ``metadata.schedule_id``. A row with none of them is a plain spawn —
        ``parent_session_id`` still says which session spawned it.
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
            parent_session_id=getattr(subtask, "parent_session_id", None) or None,
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

    An explicit context wins; its ``is_background`` is authoritative. Callers
    that pass only the legacy flag get ``interactive`` or the generic
    ``background`` kind. ``is_background=True`` with a foreground context is a
    programming error — the two disagree about whether anyone is waiting.
    """
    if context is None:
        return ExecutionContext(
            kind="background" if is_background else "interactive",
            session_id=session_id,
        )
    if is_background and not context.is_background:
        raise ValueError(
            f"is_background=True contradicts a foreground ExecutionContext ({context.kind})"
        )
    return context
```

- [ ] **Step 4:** tests pass.
- [ ] **Step 5:** commit `feat(runner): ExecutionContext value type (harness Phase 1a)`.

### Task 2: Thread the context through `run_turn`, `_tool_loop`, `stream_chat`, `dispatch`

**Files:** Modify `nous/api/runner.py` (`run_turn` ~330-360 + its `_tool_loop(...)` call ~595; `_tool_loop` ~1655 + dispatch ~1989; `stream_chat` ~1021 + dispatch ~1497; `_dispatch_with_keepalive` ~2711); `nous/api/tools.py` (`ToolDispatcher.dispatch` defined at ~370; injection ~438); the three hand-written `dispatch` doubles in `tests/test_runner.py` / `tests/test_runner_background.py`. Test: `tests/test_runner_authorization.py` (create).

**Interfaces — Produces:** `run_turn(..., context: ExecutionContext | None = None)`, `_tool_loop(..., context: ExecutionContext | None = None)`, `ToolDispatcher.dispatch(..., context: ExecutionContext | None = None)`, `ToolDispatcher._BACKGROUND_AWARE_TOOLS: frozenset[str]`. Resolved context is local `ctx` in `_tool_loop`, `_ctx` in `run_turn`/`stream_chat`.

- [ ] **Step 1: Failing tests**

```python
# tests/test_runner_authorization.py
"""Harness Phase 1a: context threading + offered-set measurement/enforcement."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from nous.api.execution_context import ExecutionContext
from nous.api.models import ApiResponse
from nous.api.runner import AgentRunner, Conversation, Message
from nous.config import Settings
from tests.test_runner_background import _MockBrain, _MockCognitive, _MockHeart


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, ANTHROPIC_API_KEY="test-key", agent_id="test-agent", **overrides)


class _RecordingDispatcher:
    """Offers ``offered``; records every dispatch (name, context, is_background)."""

    def __init__(self, offered, store=None):
        self.offered = list(offered)
        self.store = store
        self.calls: list[tuple[str, ExecutionContext | None, bool]] = []

    def available_tools(self, frame_id):
        return [{"name": n, "description": n, "input_schema": {"type": "object"}} for n in self.offered]

    async def dispatch(self, name, inp, session_id=None, is_background=False,
                       turn_number=None, context=None):
        if self.store is not None:
            self.store.events.append(("dispatch", name))
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


def _runner(offered, **settings_overrides):
    r = AgentRunner(_MockCognitive(), _MockBrain(), _MockHeart(), _settings(**settings_overrides))
    d = _RecordingDispatcher(offered)
    r.set_dispatcher(d)
    return r, d


@pytest.mark.asyncio
async def test_tool_loop_passes_the_explicit_context_to_dispatch():
    r, d = _runner(["recall_deep"])
    r._call_api = _one_tool_call_then_done("recall_deep")
    ctx = ExecutionContext(kind="heartbeat_triage", session_id="s1")
    await _run_loop(r, is_background=True, context=ctx)
    assert d.calls == [("recall_deep", ctx, True)]


@pytest.mark.asyncio
async def test_tool_loop_without_context_resolves_a_generic_one():
    r, d = _runner(["recall_deep"])
    r._call_api = _one_tool_call_then_done("recall_deep")
    await _run_loop(r, is_background=True)
    ((_name, ctx, is_bg),) = d.calls
    assert ctx.kind == "background" and ctx.session_id == "s1" and is_bg is True


@pytest.mark.asyncio
async def test_background_context_makes_the_loop_background():
    r, d = _runner(["recall_deep"])
    r._call_api = _one_tool_call_then_done("recall_deep")
    await _run_loop(r, context=ExecutionContext(kind="dag_summary", session_id="s1"))
    assert d.calls[0][2] is True


@pytest.mark.asyncio
async def test_run_turn_forwards_its_context_to_the_tool_loop():
    """Guards the run_turn -> _tool_loop hop that the loop-level tests skip."""
    r, _ = _runner(["recall_deep"])
    captured = {}

    async def fake_tool_loop(**kwargs):
        captured.update(kwargs)
        return "done", [], {"input_tokens": 0, "output_tokens": 0}, []

    r._tool_loop = fake_tool_loop  # type: ignore[method-assign]
    ctx = ExecutionContext(kind="scheduled", session_id="sched-1")
    try:
        await r.run_turn("sched-1", "go", is_background=True, skip_episode=True, context=ctx)
    finally:
        r._api_shared = True
        await r.close()
    assert captured["context"] is ctx and captured["is_background"] is True


@pytest.fixture
def probe_dispatcher():
    """A real ToolDispatcher with one tool that reads the injected flag."""
    from nous.api.tools import ToolDispatcher

    seen: list[bool] = []
    dispatcher = ToolDispatcher()

    async def probe(_is_background: bool = False):
        seen.append(_is_background)
        return {"content": [{"type": "text", "text": "ok"}]}

    dispatcher.register("probe", probe, {"type": "object", "description": "p"})
    dispatcher._BACKGROUND_AWARE_TOOLS = dispatcher._BACKGROUND_AWARE_TOOLS | {"probe"}
    return dispatcher, seen


@pytest.mark.asyncio
async def test_dispatcher_derives_is_background_from_context(probe_dispatcher):
    dispatcher, seen = probe_dispatcher
    await dispatcher.dispatch("probe", {}, context=ExecutionContext(kind="scheduled", session_id="x"))
    await dispatcher.dispatch("probe", {}, context=ExecutionContext(kind="interactive"))
    await dispatcher.dispatch("probe", {}, context=ExecutionContext(kind="mcp"))
    assert seen == [True, False, False]
```

> `run_turn`'s positional signature is `(session_id, user_message, ...)`; if `run_turn` needs more
> mocking than `_tool_loop` (e.g. `_cognitive.pre_turn` returns the `_MockCognitive` preset, which
> is enough), keep the monkeypatch on `_tool_loop` only.

- [ ] **Step 2:** run → FAIL (`unexpected keyword argument 'context'`, missing `_BACKGROUND_AWARE_TOOLS`).
- [ ] **Step 3: Implement**

`nous/api/tools.py` — import `from nous.api.execution_context import ExecutionContext, resolve_context`; on `ToolDispatcher`:

```python
    # Tools that read the injected ``_is_background`` flag (#541/#642 decision
    # resolution; F092.1 compose_surface origin). One set for every consumer.
    _BACKGROUND_AWARE_TOOLS: frozenset[str] = frozenset(
        {"resolve_decision", "resolve_decisions", "compose_surface"}
    )
```

`dispatch` gains `context: ExecutionContext | None = None` (last parameter) and, as the first lines of its body:

```python
        ctx = resolve_context(context, is_background=is_background, session_id=session_id)
        is_background = ctx.is_background
```

docstring gains: `context: the turn's ExecutionContext (harness Phase 1a); when given it is authoritative and is_background is derived from it.` The injection becomes `if name in self._BACKGROUND_AWARE_TOOLS:` (keep its comment).

`nous/api/runner.py` — import `from nous.api.execution_context import ExecutionContext, resolve_context`.

`run_turn`: new last parameter `context: ExecutionContext | None = None,  # harness Phase 1a`; first body lines (before the first read of `is_background`, which is the `self._session_monitor.touch(...)` call):

```python
        _ctx = resolve_context(context, is_background=is_background, session_id=session_id)
        is_background = _ctx.is_background
```

and `context=_ctx,` in its `self._tool_loop(...)` call.

`_tool_loop`: new last parameter `context: ExecutionContext | None = None,  # harness Phase 1a`; right after the dispatcher check:

```python
        ctx = resolve_context(context, is_background=is_background, session_id=session_id)
        is_background = ctx.is_background
```

and `context=ctx,` in its `self._dispatcher.dispatch(...)` call.

`stream_chat`: after `_agent_id = ...`:

```python
        # stream_chat serves REST /chat/stream only — a person is in the loop.
        _ctx = ExecutionContext(kind="interactive", session_id=session_id)
```

and `context=_ctx,` in the `self._dispatch_with_keepalive(...)` call; `_dispatch_with_keepalive` gains `context: ExecutionContext | None = None` and forwards `context=context` to `dispatch`.

The three hand-written doubles become
`async def dispatch(self, name, inp, session_id=None, is_background=False, turn_number=None, context=None):`,
and the two exact-kwargs assertions in `tests/test_streaming.py` (~lines 770 and 802,
`dispatch.assert_called_once_with("web_search", {...}, session_id="s1", turn_number=1)`) gain
`context=ANY`.

- [ ] **Step 4:** `uv run pytest tests/test_runner_authorization.py tests/test_runner.py tests/test_runner_background.py tests/test_streaming.py tests/test_streaming_keepalive.py -q` → green.
- [ ] **Step 5:** commit `feat(runner): thread ExecutionContext run_turn -> _tool_loop -> dispatch (harness Phase 1a)`.

### Task 3: Measure, then refuse, tool calls outside the offered set

**Files:** Modify `nous/config.py` (after `stable_tool_set_enabled` ~974); `nous/api/runner.py` (new `_authorize_tool_call`; `_tool_loop` offered set + check after the `input_error` branch ~1917; `stream_chat` offered set after F078 stripping ~1175 + check after its `input_error` branch ~1466). Test: `tests/test_runner_authorization.py`.

**Interfaces — Produces:** `AgentRunner._authorize_tool_call(ctx, tool_name, offered_names: frozenset[str], session_id: str | None) -> str | None` (refusal text only in `enforce`); setting `tool_offered_set_enforcement_mode: Literal["off", "warn", "enforce"] = "warn"`; event type `harness_unoffered_tool_call` with data `{tool_name, context_kind, mode, offered_count}`.

- [ ] **Step 1: Failing tests** (append)

```python
@pytest.mark.asyncio
async def test_warn_mode_runs_the_call_and_records_it():
    """Default mode: nothing changes for the model, the event makes it measurable."""
    r, d = _runner(["recall_deep", "bash"])
    r._brain.emit_event = AsyncMock()
    r._call_api = _one_tool_call_then_done("bash")
    await _run_loop(r, is_background=True, tool_filter=["recall_deep"])
    assert [c[0] for c in d.calls] == ["bash"]
    # _log_f026_decision schedules the write with asyncio.create_task: the
    # coroutine was CALLED (created) but has not run yet when the loop returns.
    r._brain.emit_event.assert_called()
    event_type, data = r._brain.emit_event.call_args.args[:2]
    assert event_type == "harness_unoffered_tool_call"
    assert data["tool_name"] == "bash" and data["mode"] == "warn" and data["context_kind"] == "background"


@pytest.mark.asyncio
async def test_enforce_mode_refuses_and_never_dispatches():
    r, d = _runner(["recall_deep", "bash"], tool_offered_set_enforcement_mode="enforce")
    r._brain.emit_event = AsyncMock()
    r._call_api = _one_tool_call_then_done("bash")
    _text, results, _usage, _thinking = await _run_loop(r, is_background=True, tool_filter=["recall_deep"])
    assert d.calls == []
    (res,) = results
    assert res.tool_name == "bash" and "not available in this turn" in res.error


@pytest.mark.asyncio
async def test_enforce_mode_enforces_subtask_exclusions():
    r, d = _runner(["spawn_task", "recall_deep"], tool_offered_set_enforcement_mode="enforce")
    r._brain.emit_event = AsyncMock()
    r._call_api = _one_tool_call_then_done("spawn_task")
    await _run_loop(r, is_background=True, is_subtask=True)
    assert d.calls == []


@pytest.mark.asyncio
async def test_offered_tool_runs_in_every_mode():
    for mode in ("off", "warn", "enforce"):
        r, d = _runner(["recall_deep"], tool_offered_set_enforcement_mode=mode)
        r._brain.emit_event = AsyncMock()
        r._call_api = _one_tool_call_then_done("recall_deep")
        await _run_loop(r)
        assert [c[0] for c in d.calls] == ["recall_deep"]
        r._brain.emit_event.assert_not_called()


@pytest.mark.asyncio
async def test_extra_tools_count_as_offered():
    r, _d = _runner(["recall_deep"], tool_offered_set_enforcement_mode="enforce")
    r._call_api = _one_tool_call_then_done("submit_final_report")
    ran = []

    async def _submit(**_):
        ran.append(True)
        return "report accepted", False

    schema = {"name": "submit_final_report", "description": "s", "input_schema": {"type": "object"}}
    await _run_loop(r, is_background=True, extra_tools={"submit_final_report": (schema, _submit)})
    assert ran == [True]


@pytest.mark.asyncio
async def test_off_mode_is_silent():
    r, d = _runner(["recall_deep", "bash"], tool_offered_set_enforcement_mode="off")
    r._brain.emit_event = AsyncMock()
    r._call_api = _one_tool_call_then_done("bash")
    await _run_loop(r, is_background=True, tool_filter=["recall_deep"])
    assert [c[0] for c in d.calls] == ["bash"]
    r._brain.emit_event.assert_not_called()


@pytest.mark.asyncio
async def test_enforced_refusal_is_recorded_blocked_in_the_session_ledger():
    from nous.cognitive.execution_ledger import ExecutionLedger

    r, _d = _runner(["recall_deep", "bash"], tool_offered_set_enforcement_mode="enforce")
    r._brain.emit_event = AsyncMock()
    r._call_api = _one_tool_call_then_done("bash")
    ledger = ExecutionLedger(session_id="s1")
    await _run_loop(r, is_background=True, tool_filter=["recall_deep"], ledger=ledger)
    assert [(a.tool_name, a.status) for a in ledger.actions] == [("bash", "blocked")]


def test_mode_setting_rejects_unknown_values():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        _settings(tool_offered_set_enforcement_mode="block")
```

Streaming: build `_stream_one_tool_call(runner, tool_name) -> list[str]` from the fixtures in
**`tests/test_streaming.py`** (they mock `_call_api_stream`; copy that fake generator verbatim) and add

```python
@pytest.mark.asyncio
async def test_stream_chat_enforce_refuses_an_unoffered_tool():
    r, d = _runner(["recall_deep"], tool_offered_set_enforcement_mode="enforce")
    r._brain.emit_event = AsyncMock()
    names = await _stream_one_tool_call(r, "bash")
    assert d.calls == []
    assert "bash" in names  # a paired tool_result was still emitted (tool_end event)
```

(`_emit` uses `emit_event` via `_log_f026_decision`, which is gated by `f026_persistence_enabled`
— default `True`; the `_settings` helper leaves it on.)

- [ ] **Step 2:** run → FAIL.
- [ ] **Step 3: Implement**

`nous/config.py` after `stable_tool_set_enabled`:

```python
    # Harness Phase 1a: what happens when the model calls a tool it was NOT
    # offered this iteration. Every per-context restriction (subtask
    # exclusions, tool_filter, F078 refuse) edits only the schema list; the
    # dispatcher resolves any registered name, so such a call runs today.
    #   off     — no check
    #   warn    — run it, log WARNING, persist a harness_unoffered_tool_call event
    #   enforce — refuse it with a tool error (never dispatched)
    # Ships `warn`: prod dynamic-check callbacks with on_complete_tools=['bash']
    # are prompted to "notify via Telegram" and only succeed because the hole
    # exists; flip to `enforce` once the events show what would break.
    tool_offered_set_enforcement_mode: Literal["off", "warn", "enforce"] = "warn"
```

(add `Literal` to the `typing` import in `config.py` if absent.)

`nous/api/runner.py` — new method next to `_log_f026_decision`:

```python
    def _authorize_tool_call(
        self, ctx: ExecutionContext, tool_name: str,
        offered_names: frozenset[str], session_id: str | None,
    ) -> str | None:
        """Return a refusal for a call the harness must not execute, else None.

        Harness Phase 1a: the single choke point both loops call before gating
        and dispatch. Today it only knows the OFFERED set; Phase 2a adds the
        capability policy here, so one place decides whether a call may run.
        """
        mode = self._settings.tool_offered_set_enforcement_mode
        if mode == "off" or tool_name in offered_names:
            return None
        logger.warning(
            "Harness: %s unoffered tool call %r (context=%s, session=%s)",
            "refused" if mode == "enforce" else "allowed (warn mode)",
            tool_name, ctx.kind, session_id,
        )
        self._log_f026_decision(
            "harness_unoffered_tool_call",
            {
                "tool_name": tool_name,
                "context_kind": ctx.kind,
                "mode": mode,
                "offered_count": len(offered_names),
            },
            session_id=session_id,
        )
        if mode != "enforce":
            return None
        return (
            f"Tool error: '{tool_name}' is not available in this turn. "
            "Use only the tools offered to you."
        )
```

`_tool_loop` — after the per-iteration `tools` list is complete (after the `extra_tools` append loop):

```python
            offered_names = frozenset(t["name"] for t in tools)
```

and immediately after the `input_error` branch's `continue`:

```python
                    refusal = self._authorize_tool_call(ctx, tool_name, offered_names, session_id)
                    if refusal is not None:
                        tool_results_for_message.append({
                            "type": "tool_result",
                            "tool_use_id": tool_use_id,
                            "content": refusal,
                            "is_error": True,
                        })
                        all_tool_results.append(ToolResult(
                            tool_name=tool_name, arguments=tool_input,
                            result=None, error=refusal, duration_ms=0,
                        ))
                        if ledger:
                            ledger.record(tool_name, tool_input, refusal, "blocked")
                        continue
```

`stream_chat` — before `messages = self._format_messages(conversation)`:

```python
            offered_names = frozenset(t["name"] for t in tools)
```

and after its `input_error` branch's `continue`:

```python
                        refusal = self._authorize_tool_call(_ctx, tc["name"], offered_names, session_id)
                        if refusal is not None:
                            tool_results_for_message.append({
                                "type": "tool_result",
                                "tool_use_id": tc["id"],
                                "content": refusal,
                                "is_error": True,
                            })
                            all_tool_results.append(ToolResult(
                                tool_name=tc["name"], arguments=tc.get("input", {}),
                                result=None, error=refusal, duration_ms=0,
                            ))
                            if ledger:
                                ledger.record(tc["name"], tc.get("input", {}), refusal, "blocked")
                            yield StreamEvent(type="tool_end", tool_name=tc["name"])
                            continue
```

- [ ] **Step 4:** `uv run pytest tests/test_runner_authorization.py tests/test_runner.py tests/test_runner_background.py tests/test_streaming.py tests/test_streaming_keepalive.py -q` → green.
- [ ] **Step 5:** commit `feat(runner): measure (warn) / refuse (enforce) tool calls outside the offered set (harness Phase 1a)`.

### Task 4: Every caller names its context

**Files:** Modify `nous/handlers/subtask_worker.py` (~327), `nous/handlers/subtask_executor.py` (~253), `nous/api/tools.py` inline legacy `spawn_task` (~3143), `nous/heartbeat/runner.py` triage (~562) + callback (~643), `nous/heartbeat/dynamic.py` (~133), `nous/dag/delivery.py` (~277), `nous/api/rest.py` (~137), `nous/api/mcp.py` (~225, ~348). Tests: `tests/test_runner_authorization.py` (AST sweep) + the existing caller tests.

- [ ] **Step 1: Failing tests**

AST sweep (append to `tests/test_runner_authorization.py`):

```python
def test_every_production_run_turn_call_passes_a_context():
    """No exemptions: interactive entry points pass an explicit interactive/mcp
    context too, so a background call added to rest.py/mcp.py later cannot
    slip through as the generic kind."""
    import ast
    from pathlib import Path

    offenders = []
    for path in Path("nous").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "run_turn"
                and not any(k.arg == "context" for k in node.keywords)
            ):
                offenders.append(f"{path.as_posix()}:{node.lineno}")
    assert offenders == [], offenders
```

Runtime wiring — in each caller's existing test file, add one assertion on the mocked
`run_turn`'s `call_args.kwargs["context"]` (find the mock with
`grep -n "run_turn" tests/test_heartbeat.py tests/test_heartbeat_dynamic.py tests/test_dag_delivery.py tests/handlers/test_subtask_worker_cleanup.py tests/test_f061_subtask_executor.py tests/test_rest.py tests/test_mcp.py`):

| Caller test | Assertion |
|---|---|
| heartbeat triage (`tests/test_heartbeat.py`) | `ctx.kind == "heartbeat_triage"` and `ctx.session_id` starts with `heartbeat-` |
| callback (`tests/test_heartbeat.py`, the `_execute_callback` test) | `ctx.kind == "heartbeat_callback"` |
| DynamicCheck (`tests/test_heartbeat_dynamic.py`) | `ctx.kind == "heartbeat_check"` |
| DAG summary (`tests/test_dag_delivery.py`) | `ctx.kind == "dag_summary"` and `ctx.dag_id == dag.id` |
| legacy worker (`tests/handlers/test_subtask_worker_cleanup.py`) | `ctx.kind == "subtask"`, `ctx.subtask_id == subtask.id` |
| hardened executor (`tests/test_f061_subtask_executor.py`) | `ctx.kind` matches the row (use a row with `metadata_={"dag_id": ...}` → `dag_node`) |
| REST `/chat` (`tests/test_rest.py`) | `ctx.kind == "interactive"` |
| MCP (`tests/test_mcp.py`) | `ctx.kind == "mcp"` |

Where an existing test asserts the exact kwargs with `assert_awaited_with(...)`, add `context=ANY`
and a separate kind assertion.

- [ ] **Step 2:** run → the AST sweep lists 10 sites; the kind assertions fail with `KeyError: 'context'`.
- [ ] **Step 3: Implement** — each call gains one keyword (keep all existing ones):

| File | Keyword |
|---|---|
| `subtask_worker.py` | `context=ExecutionContext.for_subtask(subtask, session_id),` |
| `subtask_executor.py` | `context=ExecutionContext.for_subtask(subtask, session_id),` |
| `tools.py` inline spawn | `context=ExecutionContext.for_subtask(subtask, subtask_session_id),` |
| `heartbeat/runner.py` triage | `context=ExecutionContext(kind="heartbeat_triage", session_id=session_id),` |
| `heartbeat/runner.py` callback | `context=ExecutionContext(kind="heartbeat_callback", session_id=session_id),` |
| `heartbeat/dynamic.py` | `context=ExecutionContext(kind="heartbeat_check", session_id=session_id),` |
| `dag/delivery.py` | `context=ExecutionContext(kind="dag_summary", session_id=f"dag-summary-{dag.id.hex[:8]}", dag_id=dag.id),` |
| `rest.py` `/chat` | `context=ExecutionContext(kind="interactive", session_id=<the session_id passed>),` |
| `mcp.py` (both) | `context=ExecutionContext(kind="mcp", session_id=<the session_id passed>),` |

Each file imports `from nous.api.execution_context import ExecutionContext` (the module imports
nothing from `nous`, so a top-level import is cycle-free everywhere).

- [ ] **Step 4:** `uv run pytest tests/test_runner_authorization.py tests/test_heartbeat.py tests/test_heartbeat_dynamic.py tests/test_dag_delivery.py tests/handlers tests/test_f061_subtask_executor.py tests/test_rest.py tests/test_mcp.py -q` → green vs baseline.
- [ ] **Step 5:** commit `feat(runner): every run_turn caller names its ExecutionContext (harness Phase 1a)`.

### Task 5: Docs + PR 1a

- [ ] CLAUDE.md row next to `NOUS_STABLE_TOOL_SET_ENABLED`:

```markdown
| `NOUS_TOOL_OFFERED_SET_ENFORCEMENT_MODE` | `warn` | Harness Phase 1a: what happens when the model calls a tool it was NOT offered in that iteration (both `_tool_loop` and `stream_chat`). Every per-context restriction — subtask exclusions (`spawn_task`/`schedule_task`/`spawn_sync`), `tool_filter` for dynamic checks and callbacks, F078 refuse stripping — edits only the schema list; the dispatcher resolves any registered name, so such a call runs. `off` = no check; `warn` = run it, log WARNING, persist a `harness_unoffered_tool_call` event (`tool_name`, `context_kind`, `mode`) to `nous_system.events`; `enforce` = refuse with a tool error and a `blocked` ledger row. Ships `warn` because prod dynamic-check callbacks with `on_complete_tools=['bash']` are prompted to notify via Telegram and currently succeed only through this hole — flip to `enforce` after reading the events. `extra_tools` (e.g. `submit_final_report`) count as offered. |
```

- [ ] Full touched suites; `ruff check` counts ≤ main baseline per touched file; push; PR; `@codex review`; CI; merge on codex-clean + green.

---

# PR 1b — Persisted execution ledger

Branch: `feat/harness-phase1b-execution-ledger` from `origin/main` after PR 1a merges.

Invariant: every side-effecting tool call leaves a durable row that exists before the side effect
and ends `success` / `error` / `blocked` / `unknown` — never silently `pending` for longer than one
sweep interval.

Decisions: only side-effecting calls (`classify_side_effect != "none"`); `extra_tools` not
persisted; **the store raises, the runner decides** (Phase 1b fails open; Phase 2b makes keyed
sends fail closed); cancelled mid-flight → `unknown`; rows store **no bodies, no code, no
unknown-tool values** (see `durable_key_args`).

### Task 6: Migration 074 + ORM

**Files:** Create `sql/migrations/074_execution_ledger.sql`; modify `nous/storage/models.py` (after `A2uiAction`).

- [ ] **Step 1: Migration**

```sql
-- 074: Persisted execution ledger (harness-autonomy roadmap Phase 1b)
--
-- The F026 ExecutionLedger is in-memory and session-scoped: it is dropped at
-- end_conversation, on eviction and on restart, and its entries have no ids.
-- This table is the durable record of what the agent DID. One row per
-- side-effecting tool call. Reads are not recorded here (F091 covers them).
--
-- Lifecycle: the runner inserts the row as 'pending' BEFORE dispatching the
-- tool and closes it after. A call cancelled mid-flight is closed 'unknown'
-- because its side effect may or may not have happened. Rows left 'pending'
-- by a dead process are swept to 'unknown' at startup, which assumes ONE Nous
-- process per (database, agent_id) - true for the docker-compose deployment.
--
-- key_args never holds bodies, code or secrets: see
-- nous/cognitive/ledger_store.py durable_key_args.
-- idempotency_key and external_ref are reserved for Phase 2b.

CREATE TABLE IF NOT EXISTS nous_system.execution_ledger (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id           TEXT         NOT NULL,
    session_id         TEXT,
    parent_session_id  TEXT,
    context_kind       VARCHAR(32)  NOT NULL,
    subtask_id         UUID,
    dag_id             UUID,
    dag_node_id        UUID,
    turn               INTEGER,
    tool_name          VARCHAR(100) NOT NULL,
    side_effect_type   VARCHAR(20)  NOT NULL,
    key_args           JSONB        NOT NULL DEFAULT '{}',
    status             VARCHAR(20)  NOT NULL DEFAULT 'pending',
    result_summary     TEXT,
    idempotency_key    TEXT,
    external_ref       TEXT,
    created_at         TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    completed_at       TIMESTAMPTZ,
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
```

- [ ] **Step 2: ORM** (`nous/storage/models.py`; add `CheckConstraint` to the sqlalchemy import if absent)

```python
LEDGER_STATUSES: tuple[str, ...] = ("pending", "success", "error", "blocked", "unknown")
LEDGER_SIDE_EFFECTS: tuple[str, ...] = ("write", "external", "irreversible")


class ExecutionLedgerEntry(Base):
    """Harness Phase 1b: durable record of one side-effecting tool call (migration 074)."""

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
    agent_id: Mapped[str] = mapped_column(Text, nullable=False)
    session_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    parent_session_id: Mapped[str | None] = mapped_column(Text, nullable=True)
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

- [ ] **Step 3:** covered by Task 7's round-trip tests; commit with Task 7.

### Task 7: `durable_key_args`, redaction for every tool, `LedgerStore`

**Files:** Modify `nous/cognitive/execution_ledger.py` (hoist `summarize_args`; extend `_REDACT_PATTERNS`; add `redact_text`); create `nous/cognitive/ledger_store.py`; test `tests/test_ledger_store.py`.

**Interfaces — Produces:**
- `redact_text(text: str) -> str` (all patterns; any tool)
- `durable_key_args(tool_name: str, args: dict) -> dict[str, str]`
- `class LedgerWriteError(Exception)` with attribute `entry_id: UUID`
- `LedgerStore(database, agent_id, *, write_timeout_seconds=2.0)`:
  - `async open_entry(*, context, tool_name, tool_input, turn) -> UUID | None` — `None` only when the call is not side-effecting; raises `LedgerWriteError` on failure/timeout (its `entry_id` is the client-generated id, valid if the COMMIT landed)
  - `async record_blocked(*, context, tool_name, tool_input, turn, reason) -> None` — raises `LedgerWriteError`
  - `async close_entry(entry_id, *, status, result_summary) -> None` — moves `pending`/`unknown` → terminal; raises on failure
  - `async mark_orphans_unknown(*, older_than_seconds: float | None) -> int` — `None` = every pending row (startup)
  - `async prune(*, retention_days: int) -> int`
- constants `KEY_ARG_CHARS = 200`, `RESULT_SUMMARY_CHARS = 500`

- [ ] **Step 1: Failing tests**

```python
# tests/test_ledger_store.py
"""Harness Phase 1b: durable execution ledger."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, update

from nous.api.execution_context import ExecutionContext
from nous.cognitive.execution_ledger import redact_text
from nous.cognitive.ledger_store import (
    KEY_ARG_CHARS, LedgerStore, LedgerWriteError, durable_key_args,
)
from nous.storage.models import ExecutionLedgerEntry


@pytest.fixture
def agent():
    return f"ledger-test-{uuid.uuid4().hex[:8]}"


@pytest.fixture
def store(db, agent):
    return LedgerStore(db, agent)


async def _row(db, entry_id):
    async with db.session() as s:
        return (await s.execute(
            select(ExecutionLedgerEntry).where(ExecutionLedgerEntry.id == entry_id)
        )).scalar_one()


# ---- durable_key_args / redaction (pure) ----

def test_run_python_stores_a_hash_never_the_code():
    args = durable_key_args("run_python", {"code": 'API_KEY = "sk-live-123"\nprint(1)'})
    assert set(args) == {"code_sha256", "code_len"}
    assert "sk-live" not in str(args)


def test_send_email_stores_recipients_and_subject_not_the_body():
    args = durable_key_args("send_email", {"to": "a@b.c", "subject": "Premarket", "body": "secret numbers"})
    assert args["to"] == "a@b.c" and args["subject"] == "Premarket"
    assert "body" not in args and "body_sha256" in args


def test_unknown_tools_store_argument_names_only():
    args = durable_key_args("brand_new_tool", {"token": "abc", "target": "x"})
    assert args == {"arg_names": "target,token"}


def test_redaction_runs_before_truncation():
    """A password cut by truncation must still be redacted."""
    url = "postgresql://nous:" + "p" * (KEY_ARG_CHARS + 50) + "@db:5432/nous"
    out = durable_key_args("bash", {"command": f"psql {url}"})
    assert "ppppp" not in out["command"]


@pytest.mark.parametrize("secret", [
    "curl -u admin:hunter2 https://x",
    "mysql -phunter2 -u root",
    "sshpass -p hunter2 ssh host",
    "tool --password=hunter2",
    "tool --api-key hunter2",
    "curl -H 'X-Api-Key: hunter2' https://x",
    "curl -H 'Authorization: Basic hunter2' https://x",
    "curl -H 'Authorization: token hunter2' https://x",
    "https://x/api?api_key=hunter2",
    "token=hunter2",
    '{"password": "hunter2"}',
    "postgresql://u:pa@hunter2@db:5432/x",
])
def test_redact_text_covers_common_secret_shapes(secret):
    assert "hunter2" not in redact_text(secret)


@pytest.mark.parametrize("benign", [
    "ls -la /tmp",
    "find . -path ./x -prune -o -print",
    "cp -pr a b",
    "mkdir -p /tmp/x/y",
    "ssh -p 2222 host",
    "python -m pytest -p no:cacheprovider",
    "git commit -m 'x'",
    "pip install -r requirements.txt",
])
def test_redact_text_leaves_ordinary_commands_alone(benign):
    assert redact_text(benign) == benign


# ---- LedgerStore (DB) ----

@pytest.mark.asyncio
async def test_open_writes_a_pending_row_with_context(store, db):
    dag_id, node_id = uuid.uuid4(), uuid.uuid4()
    ctx = ExecutionContext(kind="dag_node", session_id="subtask-1", parent_session_id="dag-summary-1",
                           dag_id=dag_id, dag_node_id=node_id)
    entry_id = await store.open_entry(
        context=ctx, tool_name="write_file",
        tool_input={"path": "/tmp/nous-workspace/x.txt", "content": "hi"}, turn=2,
    )
    row = await _row(db, entry_id)
    assert (row.status, row.context_kind, row.dag_id, row.dag_node_id, row.turn, row.parent_session_id) == (
        "pending", "dag_node", dag_id, node_id, 2, "dag-summary-1")
    assert row.side_effect_type == "write"
    assert row.key_args["path"] == "/tmp/nous-workspace/x.txt"
    assert "content" not in row.key_args and row.completed_at is None


@pytest.mark.asyncio
async def test_reads_are_not_persisted(store):
    ctx = ExecutionContext(kind="interactive")
    assert await store.open_entry(context=ctx, tool_name="recall_deep", tool_input={"query": "x"}, turn=1) is None
    assert await store.open_entry(context=ctx, tool_name="bash", tool_input={"command": "ls -la"}, turn=1) is None


@pytest.mark.asyncio
async def test_close_moves_pending_to_terminal_once(store, db):
    entry_id = await store.open_entry(context=ExecutionContext(kind="interactive"),
                                      tool_name="learn_fact", tool_input={"content": "c"}, turn=1)
    await store.close_entry(entry_id, status="success", result_summary="x" * 900)
    row = await _row(db, entry_id)
    assert row.status == "success" and row.completed_at is not None and len(row.result_summary) == 500
    await store.close_entry(entry_id, status="error", result_summary="late")
    assert (await _row(db, entry_id)).status == "success"


@pytest.mark.asyncio
async def test_owner_close_replaces_a_sweep_set_unknown(store, db):
    entry_id = await store.open_entry(context=ExecutionContext(kind="subtask"),
                                      tool_name="learn_fact", tool_input={}, turn=1)
    await store.mark_orphans_unknown(older_than_seconds=None)
    assert (await _row(db, entry_id)).status == "unknown"
    await store.close_entry(entry_id, status="success", result_summary="finished late")
    assert (await _row(db, entry_id)).status == "success"


@pytest.mark.asyncio
async def test_result_summary_is_redacted(store, db):
    entry_id = await store.open_entry(context=ExecutionContext(kind="subtask"),
                                      tool_name="run_python", tool_input={"code": "x"}, turn=1)
    await store.close_entry(entry_id, status="success", result_summary="DB_PASSWORD=hunter2 printed")
    assert "hunter2" not in (await _row(db, entry_id)).result_summary


@pytest.mark.asyncio
async def test_close_rejects_non_terminal_status(store):
    with pytest.raises(ValueError):
        await store.close_entry(uuid.uuid4(), status="pending", result_summary=None)


@pytest.mark.asyncio
async def test_record_blocked_writes_a_terminal_row(store, db, agent):
    await store.record_blocked(context=ExecutionContext(kind="heartbeat_triage"), tool_name="send_file",
                               tool_input={"file_path": "/x"}, turn=1, reason="not offered")
    async with db.session() as s:
        rows = (await s.execute(
            select(ExecutionLedgerEntry)
            .where(ExecutionLedgerEntry.agent_id == agent)
            .order_by(ExecutionLedgerEntry.created_at)
        )).scalars().all()
    assert [(r.tool_name, r.status, r.result_summary) for r in rows] == [("send_file", "blocked", "not offered")]


@pytest.mark.asyncio
async def test_write_failure_raises_with_the_client_side_id(agent):
    class _Broken:
        def session(self):
            raise RuntimeError("db down")

    store = LedgerStore(_Broken(), agent)
    with pytest.raises(LedgerWriteError) as exc:
        await store.open_entry(context=ExecutionContext(kind="interactive"),
                               tool_name="learn_fact", tool_input={}, turn=1)
    assert isinstance(exc.value.entry_id, uuid.UUID)


@pytest.mark.asyncio
async def test_non_dict_input_does_not_escape_as_a_bare_exception(store):
    with pytest.raises(LedgerWriteError):
        await store.open_entry(context=ExecutionContext(kind="interactive"),
                               tool_name="learn_fact", tool_input="not-a-dict", turn=1)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_orphan_sweep_threshold_and_agent_scope(store, db, agent):
    old = await store.open_entry(context=ExecutionContext(kind="subtask"), tool_name="learn_fact", tool_input={}, turn=1)
    other = LedgerStore(db, f"{agent}-other")
    foreign = await other.open_entry(context=ExecutionContext(kind="subtask"), tool_name="learn_fact", tool_input={}, turn=1)
    async with db.session() as s:
        await s.execute(update(ExecutionLedgerEntry)
                        .where(ExecutionLedgerEntry.id.in_([old, foreign]))
                        .values(created_at=datetime.now(UTC) - timedelta(hours=3)))
        await s.commit()
    fresh = await store.open_entry(context=ExecutionContext(kind="subtask"), tool_name="learn_fact", tool_input={}, turn=1)

    assert await store.mark_orphans_unknown(older_than_seconds=3600) == 1
    assert (await _row(db, old)).status == "unknown"
    assert (await _row(db, fresh)).status == "pending"
    assert (await _row(db, foreign)).status == "pending"


@pytest.mark.asyncio
async def test_prune_is_agent_scoped(store, db, agent):
    mine = await store.open_entry(context=ExecutionContext(kind="subtask"), tool_name="learn_fact", tool_input={}, turn=1)
    other = LedgerStore(db, f"{agent}-other")
    theirs = await other.open_entry(context=ExecutionContext(kind="subtask"), tool_name="learn_fact", tool_input={}, turn=1)
    async with db.session() as s:
        await s.execute(update(ExecutionLedgerEntry)
                        .where(ExecutionLedgerEntry.id.in_([mine, theirs]))
                        .values(created_at=datetime.now(UTC) - timedelta(days=200)))
        await s.commit()
    assert await store.prune(retention_days=90) == 1
    async with db.session() as s:
        ids = set((await s.execute(select(ExecutionLedgerEntry.id).where(
            ExecutionLedgerEntry.id.in_([mine, theirs])))).scalars())
    assert ids == {theirs}
```

- [ ] **Step 2:** run → import errors.
- [ ] **Step 3: Implement**

`nous/cognitive/execution_ledger.py`:

1. Hoist `summarize_args(tool_name, args)` to module level (body moved verbatim from
   `ExecutionLedger._summarize_args`; the method delegates to it).
2. Extend `_REDACT_PATTERNS` and add `redact_text`; `redact_key_args` keeps its bash-only contract
   for the dashboard endpoint (unchanged behavior) but now calls `redact_text`:

```python
_REDACT_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"[A-Z_]{2,}=\S+"), "[REDACTED_ENV]"),
    (re.compile(r"Bearer\s+\S+", re.IGNORECASE), "Bearer [REDACTED]"),
    # user:password@host - the password may itself contain '@', so match to the LAST '@'
    (re.compile(r"://[^/\s:@]+:\S+@"), "://[REDACTED]@"),
    (re.compile(r"(-u\s+)[^\s:]+:\S+"), r"\1[REDACTED]"),
    # -p<password> only for tools that take it that way (not find -path, cp -pr, ssh -p 22)
    (re.compile(r"(\b(?:mysql|mysqldump|mysqladmin|mariadb)\b[^|;&]*?\s-p)(?!\s)\S+"), r"\1[REDACTED]"),
    (re.compile(r"(\bsshpass\s+-p\s*)\S+"), r"\1[REDACTED]"),
    (re.compile(r"(--(?:password|passwd|api-key|api_key|token|secret)[=\s])\S+", re.IGNORECASE), r"\1[REDACTED]"),
    # header value, including an optional scheme word (Basic / token / Bearer)
    (re.compile(r"((?:x-api-key|api-key|authorization)\s*:\s*)(?:[A-Za-z]+\s+)?[^'\"\s]+", re.IGNORECASE), r"\1[REDACTED]"),
    (re.compile(r"((?:api_key|apikey|access_token|token|secret|password|passwd)=)[^&\s'\"]+", re.IGNORECASE), r"\1[REDACTED]"),
    (re.compile(r'("(?:password|passwd|secret|token|api_key|apikey)"\s*:\s*")[^"]*"', re.IGNORECASE), r'\1[REDACTED]"'),
]


def redact_text(text: str) -> str:
    """Apply every redaction pattern. Safe for any tool's argument or output."""
    for pattern, replacement in _REDACT_PATTERNS:
        text = pattern.sub(replacement, text)
    return text
```

`nous/cognitive/ledger_store.py`:

```python
"""Durable execution ledger (harness-autonomy roadmap, Phase 1b).

The in-memory F026 ExecutionLedger stays the per-session prompt aid. This
store is the durable record of side-effecting tool calls: a row is written
'pending' BEFORE dispatch and closed after, so "did it happen?" is a query.

The store RAISES on write failure (LedgerWriteError, carrying the
client-generated id — the COMMIT may have landed even when the wait timed
out). The RUNNER decides what a failure means: Phase 1b fails open; Phase 2b
makes keyed sends fail closed. Rows never hold bodies, code, or the values
of an unknown tool's arguments (durable_key_args).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import delete, func, update

from nous.api.execution_context import ExecutionContext
from nous.cognitive.execution_ledger import classify_side_effect, redact_text
from nous.storage.models import LEDGER_STATUSES, ExecutionLedgerEntry

logger = logging.getLogger(__name__)

KEY_ARG_CHARS = 200
RESULT_SUMMARY_CHARS = 500
_CLOSABLE = ("pending", "unknown")
_TERMINAL = frozenset(s for s in LEDGER_STATUSES if s != "pending")

# Per-tool durable argument policy: (values kept after redaction, values stored as sha256+len).
# A tool not listed here stores its argument NAMES only.
_DURABLE_ARGS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "bash": (("command",), ()),
    "write_file": (("path",), ("content",)),
    "run_python": ((), ("code",)),
    "send_email": (("to", "cc", "subject"), ("body", "html_body")),
    "send_file": (("file_path", "chat_id"), ("caption",)),
    "learn_fact": (("subject", "category"), ("content",)),
    "learn_skill": (("source",), ("content",)),
    "record_decision": (("category", "stakes"), ("description",)),
    "create_censor": (("domain", "action"), ("reason", "trigger_pattern")),
    "spawn_task": (("frame_type",), ("task",)),
    "spawn_sync": (("frame_type",), ("task",)),
    "schedule_task": (("every", "when", "frame_type"), ("task",)),
    "cancel_task": (("task_id",), ()),
    "heartbeat_check_create": (("name",), ("prompt",)),
    "heartbeat_check_manage": (("action", "name"), ()),
    "dag_create": (("name",), ("nodes",)),
    "dag_manage": (("action", "dag_id", "node_name"), ()),
    "push_surface": (("template", "dedup_key"), ("params",)),
    "compose_surface": (("dedup_key", "archetype"), ("intent", "data_sources")),
    "resolve_decision": (("decision_id", "outcome", "superseded_by"), ("resolution_note",)),
    "resolve_decisions": ((), ("resolutions",)),
    "ingest_document": (("source_ref", "episode_id"), ("content",)),
    "store_identity": (("section",), ("content",)),
}


def _digest(value: Any) -> tuple[str, int]:
    text = value if isinstance(value, str) else repr(value)
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16], len(text)


def durable_key_args(tool_name: str, args: dict[str, Any]) -> dict[str, str]:
    """What the durable ledger may keep about a call's arguments.

    Redact the FULL value, then truncate — truncating first can cut a secret
    in half and defeat the pattern that would have matched it.
    """
    policy = _DURABLE_ARGS.get(tool_name)
    if policy is None:
        return {"arg_names": ",".join(sorted(str(k) for k in args))}
    keep, hashed = policy
    out: dict[str, str] = {}
    for name in keep:
        if name in args and args[name] is not None:
            out[name] = redact_text(str(args[name]))[:KEY_ARG_CHARS]
    for name in hashed:
        if name in args and args[name] is not None:
            sha, length = _digest(args[name])
            out[f"{name}_sha256"] = sha
            out[f"{name}_len"] = str(length)
    return out


class LedgerWriteError(Exception):
    """A ledger write failed or timed out. ``entry_id`` may exist if the COMMIT landed."""

    def __init__(self, entry_id: UUID, cause: BaseException) -> None:
        super().__init__(f"execution ledger write failed for {entry_id}: {cause!r}")
        self.entry_id = entry_id


class LedgerStore:
    def __init__(self, database: Any, agent_id: str, *, write_timeout_seconds: float = 2.0) -> None:
        self._db = database
        self._agent_id = agent_id
        self._timeout = write_timeout_seconds

    async def open_entry(self, *, context: ExecutionContext, tool_name: str,
                         tool_input: dict[str, Any], turn: int | None) -> UUID | None:
        return await self._insert(context, tool_name, tool_input, turn, "pending", None)

    async def record_blocked(self, *, context: ExecutionContext, tool_name: str,
                             tool_input: dict[str, Any], turn: int | None, reason: str) -> None:
        await self._insert(context, tool_name, tool_input, turn, "blocked", reason)

    async def close_entry(self, entry_id: UUID, *, status: str, result_summary: str | None) -> None:
        if status not in _TERMINAL:
            raise ValueError(f"cannot close a ledger row as {status!r}")

        async def _close() -> None:
            async with self._db.session() as s:
                await s.execute(
                    update(ExecutionLedgerEntry)
                    .where(ExecutionLedgerEntry.id == entry_id)
                    .where(ExecutionLedgerEntry.agent_id == self._agent_id)
                    .where(ExecutionLedgerEntry.status.in_(_CLOSABLE))
                    .values(status=status, result_summary=_summary(result_summary),
                            completed_at=func.now())
                )
                await s.commit()

        try:
            await asyncio.wait_for(_close(), timeout=self._timeout)
        except Exception as exc:
            raise LedgerWriteError(entry_id, exc) from exc

    async def mark_orphans_unknown(self, *, older_than_seconds: float | None) -> int:
        """Pending rows → 'unknown'. ``None`` = every pending row (process startup)."""
        stmt = (
            update(ExecutionLedgerEntry)
            .where(ExecutionLedgerEntry.agent_id == self._agent_id)
            .where(ExecutionLedgerEntry.status == "pending")
        )
        if older_than_seconds is not None:
            cutoff = datetime.now(UTC) - timedelta(seconds=older_than_seconds)
            stmt = stmt.where(ExecutionLedgerEntry.created_at < cutoff)
        async with self._db.session() as s:
            result = await s.execute(stmt.values(
                status="unknown",
                result_summary="no report back from the call — outcome unknown",
                completed_at=func.now(),
            ))
            await s.commit()
            return result.rowcount or 0

    async def prune(self, *, retention_days: int) -> int:
        cutoff = datetime.now(UTC) - timedelta(days=retention_days)
        async with self._db.session() as s:
            result = await s.execute(
                delete(ExecutionLedgerEntry)
                .where(ExecutionLedgerEntry.agent_id == self._agent_id)
                .where(ExecutionLedgerEntry.created_at < cutoff)
            )
            await s.commit()
            return result.rowcount or 0

    async def _insert(self, context: ExecutionContext, tool_name: str, tool_input: dict[str, Any],
                      turn: int | None, status: str, result_summary: str | None) -> UUID | None:
        entry_id = uuid4()
        try:
            if not isinstance(tool_input, dict):
                raise TypeError(f"tool_input must be a dict, got {type(tool_input).__name__}")
            side_effect = classify_side_effect(tool_name, tool_input)
            if side_effect == "none":
                return None
            row = ExecutionLedgerEntry(
                id=entry_id,
                agent_id=self._agent_id,
                session_id=context.session_id,
                parent_session_id=context.parent_session_id,
                context_kind=context.kind,
                subtask_id=context.subtask_id,
                dag_id=context.dag_id,
                dag_node_id=context.dag_node_id,
                turn=turn,
                tool_name=tool_name,
                side_effect_type=side_effect,
                key_args=durable_key_args(tool_name, tool_input),
                status=status,
                result_summary=_summary(result_summary),
                completed_at=None if status == "pending" else func.now(),
            )

            async def _write() -> None:
                async with self._db.session() as s:
                    s.add(row)
                    await s.commit()

            await asyncio.wait_for(_write(), timeout=self._timeout)
        except Exception as exc:
            raise LedgerWriteError(entry_id, exc) from exc
        return entry_id


def _summary(text: str | None) -> str | None:
    if not text:
        return None
    return redact_text(text)[:RESULT_SUMMARY_CHARS]
```

> A non-dict `tool_input` is rejected explicitly (neither `classify_side_effect` nor
> `durable_key_args` would raise on a str) → `LedgerWriteError`, which the test asserts.
> If `completed_at=func.now()` on an ORM insert does not compile on SQLite through
> `sqlite_compat`, use `datetime.now(UTC)` for the insert path only and keep `func.now()` in the
> UPDATEs.

- [ ] **Step 4:** `uv run pytest tests/test_ledger_store.py tests/test_execution_ledger*.py -q` → green.
- [ ] **Step 5:** commit `feat(ledger): durable execution ledger table, redaction-first args, LedgerStore (harness Phase 1b)`.

### Task 8: Runner brackets side-effecting dispatch

**Files:** `nous/api/runner.py` (imports `NamedTuple`; `__init__`; `set_ledger_store`; `fork` ~305-326; helpers; both loops; `_dispatch_with_keepalive`); `tests/test_streaming_keepalive.py` (update the 2-tuple assertions to `DispatchOutcome` fields); test `tests/test_runner_ledger.py`.

**Interfaces — Produces:** `AgentRunner.set_ledger_store(store)`; `DispatchOutcome(result_text, is_error, timed_out)` NamedTuple; `_ledger_pending_tasks: set[asyncio.Task]`.

- [ ] **Step 1: Failing tests**

```python
# tests/test_runner_ledger.py
"""Harness Phase 1b: the runner brackets every side-effecting dispatch."""

from __future__ import annotations

import asyncio

import pytest

from nous.api.execution_context import ExecutionContext
from nous.cognitive.ledger_store import LedgerWriteError
from tests.test_runner_authorization import (
    AgentRunner, _MockBrain, _MockCognitive, _MockHeart, _one_tool_call_then_done,
    _RecordingDispatcher, _run_loop, _settings,
)


class _FakeStore:
    def __init__(self, *, fail_open=False, fail_close=False):
        self.events: list[tuple] = []
        self.fail_open = fail_open
        self.fail_close = fail_close

    async def open_entry(self, *, context, tool_name, tool_input, turn):
        self.events.append(("open", tool_name, context.kind))
        if self.fail_open:
            raise LedgerWriteError("id-failed", RuntimeError("db down"))
        return f"id-{tool_name}"

    async def record_blocked(self, *, context, tool_name, tool_input, turn, reason):
        self.events.append(("blocked", tool_name, reason))

    async def close_entry(self, entry_id, *, status, result_summary):
        self.events.append(("close", entry_id, status))
        if self.fail_close:
            raise LedgerWriteError(entry_id, RuntimeError("db down"))


def _runner(store, offered=("learn_fact",), **settings):
    r = AgentRunner(_MockCognitive(), _MockBrain(), _MockHeart(), _settings(**settings))
    d = _RecordingDispatcher(list(offered), store)
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
    r, d = _runner(store)

    async def failing(name, inp, **kw):
        store.events.append(("dispatch", name))
        return "boom", True

    d.dispatch = failing
    r._call_api = _one_tool_call_then_done("learn_fact")
    await _run_loop(r)
    assert store.events[-1] == ("close", "id-learn_fact", "error")


@pytest.mark.asyncio
async def test_cancellation_mid_call_closes_unknown_and_reraises():
    store = _FakeStore()
    r, d = _runner(store)
    reached = asyncio.Event()

    async def hanging(name, inp, **kw):
        store.events.append(("dispatch", name))
        reached.set()
        await asyncio.sleep(3600)

    d.dispatch = hanging
    r._call_api = _one_tool_call_then_done("learn_fact")
    task = asyncio.create_task(_run_loop(r, is_background=True))
    await asyncio.wait_for(reached.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert store.events[-1] == ("close", "id-learn_fact", "unknown")


@pytest.mark.asyncio
async def test_ledger_outage_fails_open_and_still_closes_the_client_id():
    store = _FakeStore(fail_open=True)
    r, d = _runner(store)
    r._call_api = _one_tool_call_then_done("learn_fact")
    await _run_loop(r)
    assert [c[0] for c in d.calls] == ["learn_fact"]
    assert store.events[-1] == ("close", "id-failed", "success")


@pytest.mark.asyncio
async def test_close_failure_never_breaks_the_turn(caplog):
    store = _FakeStore(fail_close=True)
    r, _ = _runner(store)
    r._call_api = _one_tool_call_then_done("learn_fact")
    text, *_ = await _run_loop(r)
    assert text == "done" and "execution ledger" in caplog.text


@pytest.mark.asyncio
async def test_enforced_refusal_is_recorded_blocked():
    store = _FakeStore()
    r, d = _runner(store, offered=("recall_deep", "learn_fact"), tool_offered_set_enforcement_mode="enforce")
    r._call_api = _one_tool_call_then_done("learn_fact")
    await _run_loop(r, is_background=True, tool_filter=["recall_deep"])
    assert d.calls == []
    (event,) = store.events
    assert event[:2] == ("blocked", "learn_fact") and event[2].startswith("Tool error:")


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


def test_fork_shares_the_ledger_store():
    from unittest.mock import MagicMock

    store = _FakeStore()
    r, _ = _runner(store)
    assert r.fork(MagicMock())._ledger_store is store
```

Streaming (using `tests/test_streaming.py`'s fake generator): a `stream_chat` tool call whose
dispatch outlives `tool_timeout` closes `unknown`; an `aclose()` of the `stream_chat` generator
while a dispatch is in flight closes `unknown`.

- [ ] **Step 2:** run → FAIL (`set_ledger_store` missing).
- [ ] **Step 3: Implement**

Imports: `from typing import Any, NamedTuple`; `from nous.cognitive.ledger_store import LedgerStore, LedgerWriteError` (type import under `TYPE_CHECKING` for `LedgerStore` if a cycle appears).

```python
class DispatchOutcome(NamedTuple):
    """Final item yielded by _dispatch_with_keepalive."""
    result_text: str
    is_error: bool
    timed_out: bool
```

`__init__`: `self._ledger_store: LedgerStore | None = None` and
`self._ledger_pending_tasks: set[asyncio.Task] = set()`. `fork()`:
`forked._ledger_store = self._ledger_store` and
`forked._ledger_pending_tasks = self._ledger_pending_tasks`.

```python
    def set_ledger_store(self, store: LedgerStore | None) -> None:
        """Harness Phase 1b: durable ledger for side-effecting tool calls."""
        self._ledger_store = store

    async def _ledger_open(self, ctx, tool_name, tool_input, turn):
        """Phase 1b policy: FAIL OPEN. Returns the row id (or the client id
        of a write that may have committed), None when not side-effecting."""
        if self._ledger_store is None:
            return None
        try:
            return await self._ledger_store.open_entry(
                context=ctx, tool_name=tool_name, tool_input=tool_input, turn=turn,
            )
        except LedgerWriteError as exc:
            logger.warning("Harness: execution ledger open failed for %s (%s) — call proceeds: %s",
                           tool_name, ctx.kind, exc)
            return exc.entry_id

    async def _ledger_close(self, entry_id, status, result_summary):
        """Shielded close: a cancellation arriving now must not leave the row
        pending. The task is strongly referenced (asyncio keeps only weak refs
        to tasks — the F091 _pending_tasks lesson)."""
        if self._ledger_store is None or entry_id is None:
            return
        task = asyncio.ensure_future(self._ledger_close_now(entry_id, status, result_summary))
        self._ledger_pending_tasks.add(task)
        task.add_done_callback(self._ledger_pending_tasks.discard)
        await asyncio.shield(task)

    async def _ledger_close_now(self, entry_id, status, result_summary):
        try:
            await self._ledger_store.close_entry(entry_id, status=status, result_summary=result_summary)
        except LedgerWriteError as exc:
            logger.warning("Harness: execution ledger close failed (row stays pending until the sweep): %s", exc)

    async def _ledger_blocked(self, ctx, tool_name, tool_input, turn, reason):
        if self._ledger_store is None:
            return
        try:
            await self._ledger_store.record_blocked(
                context=ctx, tool_name=tool_name, tool_input=tool_input, turn=turn, reason=reason,
            )
        except LedgerWriteError as exc:
            logger.warning("Harness: execution ledger blocked-row write failed: %s", exc)
```

`_tool_loop`:
- enforced-refusal branch (Task 3): before `continue`,
  `await self._ledger_blocked(ctx, tool_name, tool_input, ledger.current_turn if ledger else None, refusal)`;
- ActionGate `enforce` branch: after its `ledger.record(..., "blocked")`,
  `await self._ledger_blocked(ctx, tool_name, tool_input, ledger.current_turn, result_text)`;
- dispatcher branch (fold the existing `_hb` try/finally — exactly one `_stop_activity_heartbeat`).
  `_ledger_open` runs **before** `_hb = self._start_activity_heartbeat(...)`, so a cancellation
  during the insert cannot leave the activity heartbeat running:

```python
                            entry_id = await self._ledger_open(
                                ctx, tool_name, tool_input, ledger.current_turn if ledger else None,
                            )
                            try:
                                result_text, is_error = await self._dispatcher.dispatch(
                                    tool_name, tool_input, session_id=session_id,
                                    is_background=is_background,
                                    turn_number=turn_number,  # F091 (caller-captured)
                                    context=ctx,
                                )
                            except asyncio.CancelledError:
                                # Subtask timeout / shutdown: the side effect may or may
                                # not have happened (an orphaned SMTP thread can still
                                # deliver). Record exactly that, then re-raise.
                                await self._ledger_close(entry_id, "unknown", "cancelled mid-call — outcome unknown")
                                raise
                            except Exception as exc:
                                await self._ledger_close(entry_id, "error", f"{type(exc).__name__}: {exc}")
                                raise
                            finally:
                                await self._stop_activity_heartbeat(_hb)
                            await self._ledger_close(entry_id, "error" if is_error else "success", result_text)
```

`_dispatch_with_keepalive`: `timed_out = False` before the result block; `timed_out = True` in the
`except TimeoutError:` branch; final `yield DispatchOutcome(result_text, is_error, timed_out)`;
annotation `AsyncGenerator[StreamEvent | DispatchOutcome, None]`.

`stream_chat`: enforced-refusal and ActionGate-blocked branches gain `_ledger_blocked(_ctx, ...)`;
the dispatch site:

```python
                            start_time = time.monotonic()
                            result_text, is_error = "", False
                            entry_id = await self._ledger_open(
                                _ctx, tc["name"], dispatch_input, ledger.current_turn if ledger else None,
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
                                await self._ledger_close(entry_id, "unknown", "stream closed mid-call — outcome unknown")
                                raise
                            await self._ledger_close(
                                entry_id,
                                "unknown" if timed_out else ("error" if is_error else "success"),
                                result_text,
                            )
```

`tests/test_streaming_keepalive.py`: every `("x", False)` comparison becomes a field comparison
(`out.result_text == "x" and out.is_error is False`) and every 2-name unpack becomes a 3-name
unpack (`result_text, is_error, timed_out = results[0]`); add one assertion that the timeout case
yields `timed_out is True`.

- [ ] **Step 4:** `uv run pytest tests/test_runner_ledger.py tests/test_runner_authorization.py tests/test_runner.py tests/test_runner_background.py tests/test_streaming.py tests/test_streaming_keepalive.py -q` → green.
- [ ] **Step 5:** commit `feat(runner): bracket side-effecting dispatch with durable ledger rows (harness Phase 1b)`.

### Task 9: Settings + wiring + sweeps + shutdown

**Files:** `nous/config.py` (next to `execution_ledger_enabled` ~1462); `nous/main.py` (`create_components`: after `runner.set_dispatcher(dispatcher)` ~527; return dict ~1100; `shutdown_components` ~1178). Test: `tests/test_ledger_store.py`.

- [ ] **Step 1: Test**

```python
def test_ledger_settings_defaults_and_bounds():
    from pydantic import ValidationError

    from nous.config import Settings

    s = Settings(_env_file=None)
    assert s.execution_ledger_persist_enabled is True
    assert s.execution_ledger_retention_days == 90
    assert s.execution_ledger_pending_unknown_after_seconds == 7800
    assert s.execution_ledger_sweep_interval_seconds == 1800
    with pytest.raises(ValidationError):
        Settings(_env_file=None, execution_ledger_write_timeout_seconds=0)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, execution_ledger_retention_days=-1)


def test_effective_orphan_threshold_never_undercuts_a_legitimate_call():
    from nous.cognitive.ledger_store import effective_orphan_threshold
    from nous.config import Settings

    s = Settings(_env_file=None, dag_node_max_timeout=10000)
    assert effective_orphan_threshold(s) >= 10000 + 600
```

- [ ] **Step 2: Implement settings** (plain fields):

```python
    # Harness Phase 1b: persist side-effecting tool calls to
    # nous_system.execution_ledger (migration 074). Additive; kill switch only.
    execution_ledger_persist_enabled: bool = True
    execution_ledger_write_timeout_seconds: float = Field(default=2.0, gt=0)
    execution_ledger_retention_days: int = Field(default=90, ge=0)  # 0 disables pruning
    # 'pending' rows older than this are swept to 'unknown'. The effective
    # threshold is never below the longest legitimate call (see
    # ledger_store.effective_orphan_threshold).
    execution_ledger_pending_unknown_after_seconds: int = Field(default=7800, ge=60)
    execution_ledger_sweep_interval_seconds: int = Field(default=1800, ge=60)
```

and in `ledger_store.py`:

```python
def effective_orphan_threshold(settings: Any) -> float:
    """Never sweep a row whose call may legitimately still be running."""
    longest = max(
        getattr(settings, "dag_node_max_timeout", 0),
        getattr(settings, "subtask_max_timeout", 0),
        getattr(settings, "tool_timeout", 0),
    )
    return float(max(settings.execution_ledger_pending_unknown_after_seconds, longest + 600))
```

- [ ] **Step 3: Wire `nous/main.py`** (`create_components`; add `from datetime import UTC, datetime` only if a timestamp is needed — the sweep below uses none):

```python
    ledger_store = None
    execution_ledger_task = None
    if settings.execution_ledger_persist_enabled:
        from nous.cognitive.ledger_store import LedgerStore, effective_orphan_threshold

        ledger_store = LedgerStore(
            database, settings.agent_id,
            write_timeout_seconds=settings.execution_ledger_write_timeout_seconds,
        )
        runner.set_ledger_store(ledger_store)

        # Startup sweep: inline, awaited, bounded, guarded. Every row still
        # pending belongs to the previous process (one process per agent_id),
        # so its outcome is unknown. Runs before the subtask pool and heartbeat
        # start (both are created later in create_components), so no live
        # dispatch can be caught by it; a row it races anyway is healed by the
        # owner's close, which accepts 'unknown'.
        try:
            n = await asyncio.wait_for(
                ledger_store.mark_orphans_unknown(older_than_seconds=None), timeout=30,
            )
            if n:
                logger.warning("Harness: %d execution-ledger rows orphaned by the previous process -> unknown", n)
        except Exception:
            logger.warning("Harness: startup execution-ledger sweep failed", exc_info=True)

        async def _execution_ledger_maintenance_loop():
            # Prune at startup (a process restarted daily must still prune -
            # the F091 lesson) and then at most daily; sweep stale pending rows
            # every interval.
            last_prune: float | None = None
            loop = asyncio.get_running_loop()
            while True:
                try:
                    if settings.execution_ledger_retention_days > 0 and (
                        last_prune is None or loop.time() - last_prune >= 86400
                    ):
                        n = await ledger_store.prune(retention_days=settings.execution_ledger_retention_days)
                        last_prune = loop.time()
                        logger.info("Harness: execution ledger retention pruned %d rows", n)
                    await asyncio.sleep(settings.execution_ledger_sweep_interval_seconds)
                    await ledger_store.mark_orphans_unknown(
                        older_than_seconds=effective_orphan_threshold(settings)
                    )
                except asyncio.CancelledError:
                    break
                except Exception:
                    logger.warning("Harness: execution ledger maintenance failed", exc_info=True)
                    await asyncio.sleep(60)

        execution_ledger_task = asyncio.create_task(_execution_ledger_maintenance_loop())
```

Return dict (~1100): add `"ledger_store": ledger_store, "execution_ledger_task": execution_ledger_task,`.
`shutdown_components` (~1178), beside the retrieval-log task block:

```python
    execution_ledger_task = components.get("execution_ledger_task")
    if execution_ledger_task is not None:
        execution_ledger_task.cancel()
        try:
            await execution_ledger_task
        except asyncio.CancelledError:
            pass
```

Any other `AgentRunner(` constructed in `main.py` that is not a `fork()` also gets `set_ledger_store`.

- [ ] **Step 4:** `uv run pytest tests/test_ledger_store.py -q`; `uv run python -c "import nous.main"`.
- [ ] **Step 5:** commit `feat(ledger): wire LedgerStore, startup orphan sweep, periodic sweep + retention (harness Phase 1b)`.

### Task 10: Docs + PR 1b

- [ ] CLAUDE.md: rows for the five settings (text from the field comments); add
  `nous_system.execution_ledger` to the Database section; state the one-process-per-agent_id
  deployment assumption.
- [ ] `A2uiAction` docstring: `ledger_entry_id` can reference `nous_system.execution_ledger.id`
  (still unpopulated).
- [ ] Full touched suites; lint baseline; push; PR; `@codex review`; CI; merge on codex-clean + green.

---

## Self-review (v2)

- **Coverage:** roadmap §3 PR 0 → Tasks 0.1–0.2; 1a → Tasks 1–5 (kinds incl. `mcp`, parent
  session, threading, both loops, `off|warn|enforce`, AST sweep with no exemptions, runtime kwarg
  assertions); 1b → Tasks 6–10 (table, redaction-first args, store raises / runner fails open,
  shielded closes, cancellation → unknown, startup + periodic sweeps, retention, shutdown wiring).
- **Type consistency:** `ExecutionContext` fields used by `LedgerStore._insert` (`session_id`,
  `parent_session_id`, `kind`, `subtask_id`, `dag_id`, `dag_node_id`) exist in Task 1;
  `_authorize_tool_call(ctx, tool_name, offered_names, session_id)` is called with four args in
  both loops; `DispatchOutcome` is unpacked into three names at its single consumer.
- **Residual risks for implementation review:** (1) awaited insert adds low-ms latency per
  side-effecting call; (2) `for_subtask` is a heuristic over three writers' metadata — a fourth
  creator lands as `subtask` with `parent_session_id` still set; (3) warn-mode events volume is
  unknown until measured.
