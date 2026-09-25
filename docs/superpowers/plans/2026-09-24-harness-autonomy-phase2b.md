# Harness Autonomy Phase 2b — Idempotent Sends Implementation Plan (v2)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The same logical send reaches the recipient at most once across every retry layer, and "did it send?" is answered by the durable ledger row plus the provider's id.

**Architecture:** The runner derives an idempotency key for external sends from the `ExecutionContext` and opens the durable ledger row *with* it. A partial UNIQUE index lets one live row hold a key. Dispatch is gated on a second write (`dispatched_at`), so a row that was never dispatched can always be told apart and freed. A collision suppresses the call with a harness note; a ledger failure on a keyed call refuses it (fail closed). Handlers report the provider id and "delivery uncertain" through a `CallOutcome` object the runner passes into `dispatch()` explicitly.

**Tech Stack:** Python 3.12+, SQLAlchemy async + asyncpg, PostgreSQL partial unique index, `email.utils.make_msgid`, httpx, pytest.

**Spec:** `docs/superpowers/plans/2026-09-24-harness-autonomy-roadmap.md` §2 row P0.2, §3 row 2b and "Design forks resolved for Phase 2". Anchors from `main` `240c795`; this plan branches after Phase 2a merges (it reads `TOOL_CLASSES`, `ExecutionContext.check_name`).

**v2.2 (implemented):** anchors re-located on `main` `da5356a` (after 2c and 2a). Deviations, all additive: `record_blocked` passes the key with a `conflict_free` insert (a `blocked` row is outside the index predicate); `claim_dispatch` reads `rowcount` instead of `RETURNING` (portable to the SQLite test DB); the handler records an uncertain or partial send against the email rate limit (it may have gone out); the in-memory ledger records `blocked` for a refused suppression and `success` for "already sent"; an exception escaping dispatch closes `unknown` when the outcome was marked uncertain (defensive: the real ToolDispatcher catches handler exceptions, so the normal close is the path that runs). Extra tests: the same key on another tool, a fresh undispatched holder is in flight, a stale but dispatched holder is never freed, a definite failure frees the key, a login failure is definite.

**v2.5 (codex round 2):** the key material is a canonical JSON array (recipients as a sorted set across `to`+`cc`, or chat + basename, + label) instead of `|`-joined text: a `|` inside a basename or a label made two different sends share material.

**v2.4 (codex round 1):** the key is derived from the arguments the handler receives -- `ToolDispatcher.repaired_args` exposes the one repair `dispatch` runs (`_repair`), so a required arg the model leaked as an XML `<parameter>` and dispatch salvaged keys the same as a clean relaunch; retention reduces a keyed row that holds its key (`success` or `unknown`) to a tombstone (key, status, provider id and context ids kept; `key_args` and `result_summary` cleared) instead of deleting it, so a `retry_node` after the window cannot send again.

**v2.3 (after the verify-by-execution reviewer):** the key reads recipients through the handler's own `normalize_recipients` (one definition, moved to `nous/api/idempotency.py`) -- `to=["a, b"]` and `to="a, b"` were two keys and two deliveries; `send_file` decides by what reached Telegram -- a 5xx or an unreadable reply after the upload is `unknown`, a connect/pool timeout, a local protocol error or a proxy error is `error`, an `ok:true` reply is a success whatever its `result` shape; a keyed refusal says the ledger could not record the send (not "unavailable"); a keyed blocked row waits the keyed timeout; runner tests with the real `LedgerStore`. Documented limits (by design of the key, not changed): sibling sends from one scope share a key, a regenerated temp file is a new key, a subtask spawned from a node or callback is scoped by its own row, the F087 Telegram push is not keyed, and an SMTP reply parse error after the final dot (`Line too long`) is classified definite.

**v2.1 (after re-review):** a collision is decided by **attempt**, not by subject — v2's "different subject → call again with a `send_label`" reply would have taught a retrying model (which rewrites the subject) exactly how to send a duplicate. A key held from an earlier turn (a relaunch, an F061 attempt, a delivery retry) is always "already sent", with no mention of labels; only a second send of the same key **within one turn** is asked for a `send_label`. The stale-undispatched check moves into the `UPDATE` itself and every collision-path failure maps to `LedgerWriteError` (no naive/aware datetime compare on SQLite, nothing raw escapes). Three more test doubles take `outcome=`. The rare "claimed but never sent" row that the sweep turns `unknown` is documented.

**v2 (after 3-agent review) — design changes:**
- The `CallOutcome` is passed into `ToolDispatcher.dispatch(..., outcome=)`, which sets/resets its context variable **inside** dispatch — one task. v1's `with outcome_scope()` around the keepalive generator crashed `stream_chat` on any tool slower than 10 s (`rest.py:207` resumes each chunk in a new task) — all three reviewers.
- `dispatched_at` gates dispatch; the orphan sweep turns a keyed `pending` row with `dispatched_at IS NULL` into `error` (never sent → key freed), and a collision with such a row older than the keyed timeout frees it — DB/architect P1: v1's best-effort close could lose the race with a late COMMIT and hold a never-sent key forever.
- The key no longer includes subject, body, caption or file size — the model rewrites those on retry (devil P1). Key = scope + canonical recipients (email) or resolved chat + file name (file) + an optional `send_label` for an intentional second message. A collision whose subject differs returns an error that asks for a `send_label` instead of a silent "already sent".
- SMTP errors: explicit `quit()` swallowed in `finally` (a failing QUIT no longer replaces "uncertain" with a definite error that frees the key); `SMTPServerDisconnected`/`TimeoutError` → uncertain, other `SMTPException` → definite, remaining `OSError` → uncertain, and only for the `send_message` stage (architect P1).
- Scopes: the DAG summary session is `dag-summary-{dag.id.hex}-g{delivery_generation}` (full id — v1's 8 hex chars could collide; generation — a `retry_node` re-announces deliberately); a heartbeat callback's retries share `callback:{check}:{run_id}` minted once before its retry loop (devil P2).
- Duplicate detection by lookup, not by matching the index name in the error text (works on SQLite too); `_held` bounded and wrapped; `sqlite_where` on the index; `external_ref` on every close path; the `duplicate` blocked row carries the key; prune never deletes a keyed `unknown` row.

## Global Constraints

- Keyed calls only: `send_email`, `send_file` (class `external`, excluding `bash`). Unkeyed calls keep Phase 1b behavior exactly (fail open).
- A key is held while its row is `pending`, `success` or `unknown`; `error` and `blocked` free it. `unknown` means "maybe delivered" — never auto-resent; release is an operator action (`UPDATE … SET status='error'`).
- Interactive, mcp, heartbeat checks and generic background are **unkeyed** — an operator re-send from chat always goes out.
- No tool output is stored; a suppressed repeat returns a harness note with the first row's time, status and provider id.
- Migration `075`: `IF NOT EXISTS`, no `;` inside `--` comments.
- New setting is a plain pydantic field. Tests: `UV_PROJECT_ENVIRONMENT=E:/Projects/nous/.venv uv run --frozen pytest …` (local DB tests run on SQLite via `create_all`; CI Postgres runs the migration). Commit explicit paths only.

## Why (verified, `main` `240c795`)

| Defect | Anchor |
|---|---|
| DAG retries re-send: LLM fix-dispatch is ON in prod; `retry_as_is`, `retry_with_amended_prompt`, `retry_node` relaunch a fresh subtask; DAG node subtasks carry no attempt cap | `nous/dag/orchestrator.py:2177-2372,542-679,2460-2467`; `.env.prod-snapshot:190` |
| DAG delivery re-runs the summary turn up to 5 times; prod's summary turn spawns email-send subtasks | `orchestrator.py:334-451`; `nous/dag/delivery.py:277-289` |
| A heartbeat callback retries under a new session with every tool when `on_complete_tools` is empty | `nous/heartbeat/runner.py:630-677` |
| Ledger columns `idempotency_key`/`external_ref` reserved, never written; no index | `sql/migrations/074_execution_ledger.sql:33-34,43-49` |
| A duplicate-key write would be wrapped as `LedgerWriteError` and the runner proceeds | `nous/cognitive/ledger_store.py:351-352`; `nous/api/runner.py:309-314` |
| No `Message-ID`; refused recipients discarded; a timeout after DATA reads as a definite failure | `nous/api/email_tools.py:532-586,610-628,769-777` |
| Telegram `message_id` discarded; a `ReadTimeout` after upload reads as a definite failure | `nous/api/telegram_tools.py:136-161` |
| Handlers cannot see the context; `dispatch` returns only `(text, is_error)` | `nous/api/tools.py:381-385,509-520` |

## Design

**Key** = `{scope}:{sha256(material)[:16]}`:

| Context | Scope |
|---|---|
| `dag_node` with `dag_id` + `dag_node_name` | `dag:{dag_id}:{node_name}` |
| `dag_summary` | its `session_id` = `dag-summary-{dag.id.hex}-g{delivery_generation}` |
| subtask whose `parent_session_id` starts `dag-summary-` | that `parent_session_id` (same scope as a direct send from the summary turn) |
| `heartbeat_callback` with `check_name` + `run_id` | `callback:{check_name}:{run_id}` |
| `subtask`, `scheduled`, `agent_action` with `subtask_id` | `subtask:{subtask_id}` |
| interactive, mcp, heartbeat_check, heartbeat_triage, background | — (unkeyed) |

`material`: `send_email` → sorted lowercase `to`+`cc` + `|` + `send_label`; `send_file` → (`chat_id` or the configured chat) + `|` + file basename + `|` + `send_label`.

**Keyed call flow** (one helper for both runner loops):

| Step | Outcome | Tool result | In-memory ledger | Durable |
|---|---|---|---|---|
| insert `pending` (key) → `claim_dispatch` sets `dispatched_at` | dispatch; close with status + `external_ref`; `unknown` if the handler marked the outcome uncertain | handler's | normal | closed |
| `DuplicateSend`, held `success`, key first used in an **earlier turn** (relaunch, F061 attempt, delivery retry) | not dispatched | "Already sent at … (ref …)." `is_error=False` — never mentions labels | `success` (2c claims stay grounded) | `blocked` `duplicate` + key |
| `DuplicateSend`, held `success`, key already sent **in this turn** | not dispatched | error: "already sent in this task; if this is intentionally a second message, add a `send_label`" | `blocked` | `blocked` `duplicate` + key |
| `DuplicateSend`, held `pending`/`unknown` | not dispatched | error "in flight / outcome unknown" | `blocked` | `blocked` `duplicate` + key |
| insert or `claim_dispatch` fails | not dispatched | error "ledger unavailable" | `blocked` | best-effort `error`; the sweep frees any row left `pending`+undispatched |

---

### Task 1: Migration 075 — live-key uniqueness and the dispatch gate

**Files:**
- Create: `sql/migrations/075_execution_ledger_idempotency.sql`
- Modify: `nous/storage/models.py` (`ExecutionLedgerEntry` columns + `__table_args__` `:1400-1429`; import `Index`, `text`)
- Test: `tests/test_ledger_store.py`

- [ ] **Step 1: Write the failing DB test**

```python
@pytest.mark.asyncio
async def test_a_live_key_is_unique_but_an_error_frees_it(db, agent):
    from sqlalchemy.exc import IntegrityError

    async def insert(status):
        async with db.session() as s:
            s.add(ExecutionLedgerEntry(
                id=uuid.uuid4(), agent_id=agent, context_kind="dag_node", tool_name="send_email",
                side_effect_type="external", key_args={}, status=status, idempotency_key="k1"))
            await s.commit()

    await insert("error")          # a failed send does not hold the key
    await insert("pending")
    with pytest.raises(IntegrityError):
        await insert("success")    # a second live row for the same key


def test_the_index_predicate_covers_every_closable_status():
    from nous.cognitive.ledger_store import _CLOSABLE, KEY_HOLDING_STATUSES
    assert set(_CLOSABLE) <= set(KEY_HOLDING_STATUSES)
```

- [ ] **Step 2: Run to verify it fails.**

- [ ] **Step 3: Implement** `sql/migrations/075_execution_ledger_idempotency.sql`:

```sql
-- Harness Phase 2b: idempotent sends.
-- dispatched_at is set by a second write immediately before a KEYED call is
-- dispatched. A keyed row still pending with dispatched_at NULL was never
-- sent, so the orphan sweep can free its key instead of holding it forever.
ALTER TABLE nous_system.execution_ledger
    ADD COLUMN IF NOT EXISTS dispatched_at TIMESTAMPTZ;

-- At most one LIVE row per key: pending (in flight), success (sent) or
-- unknown (maybe delivered, never auto-resent). error and blocked free it.
CREATE UNIQUE INDEX IF NOT EXISTS uq_execution_ledger_idempotency
    ON nous_system.execution_ledger (agent_id, tool_name, idempotency_key)
    WHERE idempotency_key IS NOT NULL
      AND status IN ('pending', 'success', 'unknown');
```

`models.py`: add `dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)` after `completed_at`, and in `__table_args__` (before the trailing `{"schema": ...}` dict):

```python
        Index(
            "uq_execution_ledger_idempotency", "agent_id", "tool_name", "idempotency_key",
            unique=True,
            postgresql_where=text(
                "idempotency_key IS NOT NULL AND status IN ('pending', 'success', 'unknown')"),
            sqlite_where=text(
                "idempotency_key IS NOT NULL AND status IN ('pending', 'success', 'unknown')"),
        ),
```

`ledger_store.py`: `KEY_HOLDING_STATUSES = ("pending", "success", "unknown")` (the index predicate, one definition for the store's lookups).

- [ ] **Step 4: Run** `uv run --frozen pytest tests/test_ledger_store.py tests/test_database.py -q` → PASS.

- [ ] **Step 5: Commit**

```bash
git add sql/migrations/075_execution_ledger_idempotency.sql nous/storage/models.py nous/cognitive/ledger_store.py tests/test_ledger_store.py
git commit -m "feat(ledger): live idempotency keys are unique; dispatch gate column (harness 2b, migration 075)"
```

---

### Task 2: The idempotency key

**Files:**
- Create: `nous/api/idempotency.py`
- Modify: `nous/api/execution_context.py` (add `run_id: str | None = None`)
- Modify: `nous/dag/delivery.py:278-285` (summary session id), `nous/heartbeat/runner.py` (callback `run_id`)
- Test: `tests/test_idempotency.py` (new), `tests/test_dag_delivery.py`, `tests/test_heartbeat.py`

**Interfaces:**
- Consumes: `tool_class` (2a); `ExecutionContext.check_name` (2a).
- Produces: `is_keyed_tool(name) -> bool`; `idempotency_key(ctx, tool_name, tool_input, *, default_chat_id: str | None = None) -> str | None`; `ExecutionContext.run_id`.

- [ ] **Step 1: Write the failing tests**

```python
import uuid

from nous.api.execution_context import ExecutionContext
from nous.api.idempotency import idempotency_key, is_keyed_tool

DAG = uuid.uuid4()
EMAIL = {"to": "Bob@X.io, alice@x.io", "subject": " Premarket ", "body": "b"}


def _node(**kw):
    return ExecutionContext(kind="dag_node", dag_id=DAG, dag_node_name="send", **kw)


def test_only_sends_are_keyed():
    assert is_keyed_tool("send_email") and is_keyed_tool("send_file")
    assert not is_keyed_tool("bash") and not is_keyed_tool("write_file")


def test_a_dag_node_key_survives_relaunch_and_rewording():
    first = idempotency_key(_node(subtask_id=uuid.uuid4(), session_id="subtask-aaaa"), "send_email", EMAIL)
    again = idempotency_key(_node(subtask_id=uuid.uuid4(), session_id="subtask-bbbb"), "send_email",
                            dict(EMAIL, subject="Premarket brief (retry)", body="reworded"))
    assert first == again and first.startswith(f"dag:{DAG}:send:")


def test_recipients_are_canonical():
    reordered = dict(EMAIL, to=["ALICE@x.io", "bob@x.io"])
    assert idempotency_key(_node(), "send_email", EMAIL) == idempotency_key(_node(), "send_email", reordered)


def test_a_label_makes_an_intentional_second_message():
    assert idempotency_key(_node(), "send_email", EMAIL) != idempotency_key(
        _node(), "send_email", dict(EMAIL, send_label="followup"))


def test_the_summary_turn_and_its_children_share_a_scope():
    session = f"dag-summary-{DAG.hex}-g1"
    summary = ExecutionContext(kind="dag_summary", dag_id=DAG, session_id=session)
    child = ExecutionContext(kind="subtask", subtask_id=uuid.uuid4(), parent_session_id=session)
    assert idempotency_key(summary, "send_email", EMAIL) == idempotency_key(child, "send_email", EMAIL)


def test_a_new_delivery_generation_is_a_new_announcement():
    g1 = ExecutionContext(kind="dag_summary", dag_id=DAG, session_id=f"dag-summary-{DAG.hex}-g1")
    g2 = ExecutionContext(kind="dag_summary", dag_id=DAG, session_id=f"dag-summary-{DAG.hex}-g2")
    assert idempotency_key(g1, "send_email", EMAIL) != idempotency_key(g2, "send_email", EMAIL)


def test_a_callback_retry_shares_its_run_scope():
    a = ExecutionContext(kind="heartbeat_callback", check_name="c", run_id="r1", session_id="hb-1")
    b = ExecutionContext(kind="heartbeat_callback", check_name="c", run_id="r1", session_id="hb-2")
    assert idempotency_key(a, "send_email", EMAIL) == idempotency_key(b, "send_email", EMAIL)


def test_a_plain_subtask_is_keyed_by_its_row():
    sid = uuid.uuid4()
    assert idempotency_key(ExecutionContext(kind="subtask", subtask_id=sid), "send_email", EMAIL).startswith(
        f"subtask:{sid}:")


def test_foreground_checks_and_background_are_unkeyed():
    for kind in ("interactive", "mcp", "heartbeat_triage", "heartbeat_check", "background"):
        assert idempotency_key(ExecutionContext(kind=kind), "send_email", EMAIL) is None


def test_send_file_key_uses_the_resolved_chat_and_file_name():
    implicit = idempotency_key(_node(), "send_file", {"file_path": "/tmp/r.png"}, default_chat_id="123")
    explicit = idempotency_key(_node(), "send_file", {"file_path": "/tmp/r.png", "chat_id": "123"},
                               default_chat_id="123")
    regenerated = idempotency_key(_node(), "send_file", {"file_path": "/tmp/r.png", "caption": "new"},
                                  default_chat_id="123")
    assert implicit == explicit == regenerated
```

In `tests/test_dag_delivery.py`: the summary `run_turn` receives `session_id == f"dag-summary-{dag.id.hex}-g{dag.delivery_generation}"` and the same value in `context.session_id`. In `tests/test_heartbeat.py`: both attempts of a retried callback carry the same `context.run_id`.

- [ ] **Step 2: Run to verify they fail.**

- [ ] **Step 3: Implement** `nous/api/idempotency.py`:

```python
"""Idempotency keys for external sends (harness Phase 2b).

Computed in the runner, where the execution context and the call's arguments
meet. The key names the logical send -- who it goes to, within which unit of
work -- not its wording: a retry re-runs the objective and rewrites subject,
body and caption, so those are never part of the key. An intentional second
message to the same recipients carries a `send_label`.
"""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Mapping
from typing import Any

from nous.api.execution_context import ExecutionContext
from nous.api.tool_classes import tool_class

SUMMARY_SESSION_PREFIX = "dag-summary-"


def is_keyed_tool(name: str) -> bool:
    """External sends: the calls a retry can duplicate at a recipient."""
    cls = tool_class(name)
    return name != "bash" and cls is not None and cls.side_effect == "external"


def _scope(ctx: ExecutionContext) -> str | None:
    if ctx.kind == "dag_node" and ctx.dag_id is not None and ctx.dag_node_name:
        return f"dag:{ctx.dag_id}:{ctx.dag_node_name}"
    if ctx.kind == "dag_summary" and ctx.session_id:
        return ctx.session_id
    if ctx.parent_session_id and ctx.parent_session_id.startswith(SUMMARY_SESSION_PREFIX):
        return ctx.parent_session_id
    if ctx.kind == "heartbeat_callback" and ctx.check_name and ctx.run_id:
        return f"callback:{ctx.check_name}:{ctx.run_id}"
    if ctx.kind in ("subtask", "scheduled", "agent_action") and ctx.subtask_id is not None:
        return f"subtask:{ctx.subtask_id}"
    return None


def canonical_recipients(value: Any) -> list[str]:
    items = value if isinstance(value, (list, tuple)) else re.split(r"[,;]", str(value or ""))
    return sorted({str(v).strip().lower() for v in items if str(v).strip()})


def _material(tool_name: str, args: Mapping[str, Any], default_chat_id: str | None) -> str | None:
    label = str(args.get("send_label") or "").strip()
    if tool_name == "send_email":
        to = ",".join(canonical_recipients(args.get("to")) + canonical_recipients(args.get("cc")))
        return f"{to}|{label}"
    if tool_name == "send_file":
        chat = str(args.get("chat_id") or default_chat_id or "")
        return f"{chat}|{os.path.basename(str(args.get('file_path') or ''))}|{label}"
    return None


def idempotency_key(
    ctx: ExecutionContext, tool_name: str, tool_input: Mapping[str, Any],
    *, default_chat_id: str | None = None,
) -> str | None:
    """``{scope}:{digest}``, or None when this call is not keyed."""
    if not is_keyed_tool(tool_name):
        return None
    scope = _scope(ctx)
    material = _material(tool_name, tool_input, default_chat_id)
    if scope is None or material is None:
        return None
    return f"{scope}:{hashlib.sha256(material.encode('utf-8')).hexdigest()[:16]}"
```

`ExecutionContext`: `run_id: str | None = None  # one logical run across its retries (heartbeat callback)`.
`delivery.py`: `session_id = f"{SUMMARY_SESSION_PREFIX}{dag.id.hex}-g{dag.delivery_generation}"`, used for both `run_turn(session_id=…)` and `ExecutionContext(session_id=…)` (import the prefix from `nous.api.idempotency`).
`heartbeat/runner.py`: before the `for attempt in range(2)` callback loop, `run_id = uuid.uuid4().hex`; pass `run_id=run_id` into the callback's `ExecutionContext` (alongside 2a's `declared_tools`/`check_name`).

- [ ] **Step 4: Run** `uv run --frozen pytest tests/test_idempotency.py tests/test_dag_delivery.py tests/test_heartbeat.py tests/test_execution_context.py -q` → PASS.

- [ ] **Step 5: Commit**

```bash
git add nous/api/idempotency.py nous/api/execution_context.py nous/dag/delivery.py nous/heartbeat/runner.py tests/test_idempotency.py tests/test_dag_delivery.py tests/test_heartbeat.py
git commit -m "feat(harness): idempotency keys name the logical send, not its wording (2b)"
```

---

### Task 3: The store: duplicate vs outage, the dispatch gate, the split sweep

**Files:**
- Modify: `nous/cognitive/ledger_store.py` (`open_entry`, `record_blocked`, `close_entry`, `mark_orphans_unknown`, `prune`, `_insert`, `REFUSAL_CODES`, `__init__`)
- Modify: `nous/config.py`, `nous/main.py` (construct `LedgerStore` with the new timeout)
- Test: `tests/test_ledger_store.py`

**Interfaces:**
- Produces: `open_entry(..., idempotency_key: str | None = None) -> UUID | None`; `@dataclass(frozen=True) class HeldKey: entry_id: UUID; status: str; external_ref: str | None; created_at: datetime; dispatched_at: datetime | None; key_args: dict`; `class DuplicateSend(Exception): held: HeldKey`; `claim_dispatch(entry_id) -> bool`; `close_entry(..., external_ref: str | None = None, keyed: bool = False)`; `record_blocked(..., idempotency_key: str | None = None)`; `REFUSAL_CODES` += `"duplicate"`; setting `execution_ledger_keyed_write_timeout_seconds: float = Field(default=10.0, gt=0)`; `LedgerStore(..., keyed_write_timeout_seconds: float = 10.0)`.

- [ ] **Step 1: Write the failing DB tests**

```python
@pytest.mark.asyncio
async def test_a_repeat_key_raises_duplicate_with_the_held_row(store):
    ctx = ExecutionContext(kind="dag_node")
    first = await store.open_entry(context=ctx, tool_name="send_email",
                                   tool_input={"to": "a@x.io", "subject": "s"}, turn=1, idempotency_key="k")
    assert await store.claim_dispatch(first)
    await store.close_entry(first, status="success", result_summary=None, external_ref="<m1@x>", keyed=True)
    with pytest.raises(DuplicateSend) as exc:
        await store.open_entry(context=ctx, tool_name="send_email",
                               tool_input={"to": "a@x.io"}, turn=2, idempotency_key="k")
    held = exc.value.held
    assert (held.entry_id, held.status, held.external_ref) == (first, "success", "<m1@x>")
    assert "subject_sha256" in held.key_args


@pytest.mark.asyncio
async def test_an_errored_first_attempt_does_not_block_the_retry(store):
    ctx = ExecutionContext(kind="dag_node")
    first = await store.open_entry(context=ctx, tool_name="send_email", tool_input={}, turn=1,
                                   idempotency_key="k2")
    await store.close_entry(first, status="error", result_summary=None)
    assert await store.open_entry(context=ctx, tool_name="send_email", tool_input={}, turn=2,
                                  idempotency_key="k2") is not None


@pytest.mark.asyncio
async def test_claim_dispatch_is_once_only(store):
    entry = await store.open_entry(context=ExecutionContext(kind="dag_node"), tool_name="send_email",
                                   tool_input={}, turn=1, idempotency_key="k3")
    assert await store.claim_dispatch(entry) is True
    assert await store.claim_dispatch(entry) is False


@pytest.mark.asyncio
async def test_the_sweep_frees_a_key_that_was_never_dispatched(store, db):
    ctx = ExecutionContext(kind="dag_node")
    never = await store.open_entry(context=ctx, tool_name="send_email", tool_input={}, turn=1,
                                   idempotency_key="k4")
    sent = await store.open_entry(context=ctx, tool_name="send_file", tool_input={}, turn=1,
                                  idempotency_key="k5")
    await store.claim_dispatch(sent)
    unkeyed = await store.open_entry(context=ctx, tool_name="learn_fact", tool_input={}, turn=1)
    await store.mark_orphans_unknown(older_than_seconds=None)
    assert (await _row(db, never)).status == "error"      # never sent: key freed
    assert (await _row(db, sent)).status == "unknown"     # maybe delivered: key held
    assert (await _row(db, unkeyed)).status == "unknown"  # Phase 1b behavior


@pytest.mark.asyncio
async def test_a_stale_undispatched_holder_is_freed_on_collision(store, db):
    ctx = ExecutionContext(kind="dag_node")
    stale = await store.open_entry(context=ctx, tool_name="send_email", tool_input={}, turn=1,
                                   idempotency_key="k6")
    async with db.session() as s:
        await s.execute(update(ExecutionLedgerEntry).where(ExecutionLedgerEntry.id == stale)
                        .values(created_at=datetime.now(UTC) - timedelta(minutes=5)))
        await s.commit()
    retry = await store.open_entry(context=ctx, tool_name="send_email", tool_input={}, turn=2,
                                   idempotency_key="k6")
    assert retry is not None and (await _row(db, stale)).status == "error"


@pytest.mark.asyncio
async def test_external_ref_and_key_are_written(store, db):
    entry = await store.open_entry(context=ExecutionContext(kind="dag_node"), tool_name="send_file",
                                   tool_input={}, turn=1, idempotency_key="k7")
    await store.close_entry(entry, status="success", result_summary=None, external_ref="42", keyed=True)
    row = await _row(db, entry)
    assert (row.idempotency_key, row.external_ref) == ("k7", "42")


@pytest.mark.asyncio
async def test_a_duplicate_blocked_row_carries_the_key(store, db, agent):
    await store.record_blocked(context=ExecutionContext(kind="dag_node"), tool_name="send_email",
                               tool_input={}, turn=1, refused_by="duplicate", idempotency_key="k8")
    async with db.session() as s:
        row = (await s.execute(select(ExecutionLedgerEntry).where(
            ExecutionLedgerEntry.agent_id == agent))).scalar_one()
    assert (row.status, row.idempotency_key) == ("blocked", "k8")


@pytest.mark.asyncio
async def test_prune_keeps_a_held_unknown_key(store, db):
    entry = await store.open_entry(context=ExecutionContext(kind="dag_node"), tool_name="send_email",
                                   tool_input={}, turn=1, idempotency_key="k9")
    await store.close_entry(entry, status="unknown", result_summary=None)
    async with db.session() as s:
        await s.execute(update(ExecutionLedgerEntry).where(ExecutionLedgerEntry.id == entry)
                        .values(created_at=datetime.now(UTC) - timedelta(days=400)))
        await s.commit()
    await store.prune(retention_days=90)
    assert (await _row(db, entry)).status == "unknown"


@pytest.mark.asyncio
async def test_an_outage_is_a_write_error_not_a_duplicate(agent):
    class _Broken:
        def session(self):
            raise RuntimeError("db down")

    with pytest.raises(LedgerWriteError):
        await LedgerStore(_Broken(), agent).open_entry(
            context=ExecutionContext(kind="dag_node"), tool_name="send_email", tool_input={},
            turn=1, idempotency_key="k10")
```

- [ ] **Step 2: Run to verify they fail** — `ImportError: DuplicateSend`.

- [ ] **Step 3: Implement**

`_insert` gains `idempotency_key` (and an internal `_retry: bool = False`); the row sets it. A unique violation is **captured**, then resolved outside the `except` clause so no exception raised while resolving it can escape unwrapped; the decision is by lookup, never by the error text:

```python
        conflict: IntegrityError | None = None
        try:
            await asyncio.wait_for(_write(), timeout=self._keyed_timeout if idempotency_key else self._timeout)
        except IntegrityError as exc:
            if idempotency_key is None:
                raise LedgerWriteError(entry_id, exc) from exc
            conflict = exc
        except Exception as exc:
            raise LedgerWriteError(entry_id, exc) from exc
        if conflict is None:
            return entry_id
        try:
            held = await asyncio.wait_for(self._held(tool_name, idempotency_key), timeout=self._keyed_timeout)
            if held is None or (not _retry and await asyncio.wait_for(
                    self._free_if_stale_undispatched(held.entry_id), timeout=self._keyed_timeout)):
                if _retry:
                    raise LedgerWriteError(entry_id, conflict)
                # the holder was freed (by its owner or just now, stale and never
                # dispatched): insert once more
                return await self._insert(context, tool_name, tool_input, turn, status,
                                          result_summary, idempotency_key=idempotency_key, _retry=True)
        except (DuplicateSend, LedgerWriteError):
            raise
        except Exception as exc:
            raise LedgerWriteError(entry_id, exc) from exc
        raise DuplicateSend(held) from conflict
```

- `_held` selects `id, status, external_ref, created_at, dispatched_at, key_args` for `(agent_id, tool_name, idempotency_key)` with `status IN KEY_HOLDING_STATUSES`; returns `None` if no row.
- `_free_if_stale_undispatched(id) -> bool`: the staleness test lives **in the UPDATE** (Python only computes the cutoff, the same pattern `prune` uses): `UPDATE … SET status='error', result_summary='never dispatched', completed_at=now() WHERE id=:id AND status='pending' AND dispatched_at IS NULL AND created_at < :cutoff` (cutoff = now − keyed timeout) → `rowcount == 1`. The owner's `claim_dispatch` updates the same row on the same condition, so exactly one of them wins.
- `claim_dispatch(entry_id) -> bool`: `UPDATE … SET dispatched_at=now() WHERE id=:id AND agent_id=:agent AND status='pending' AND dispatched_at IS NULL RETURNING id`, under the keyed timeout; returns `True` only when a row came back; raises `LedgerWriteError` on failure.
- `mark_orphans_unknown`: two updates in one session — keyed rows `pending AND idempotency_key IS NOT NULL AND dispatched_at IS NULL` → `error` ("never dispatched — outcome certain: not sent"); every other stale `pending` → `unknown` (unchanged). Returns the total.
- `prune`: add `.where(~((ExecutionLedgerEntry.idempotency_key.is_not(None)) & (ExecutionLedgerEntry.status == "unknown")))`.
- `close_entry(..., external_ref=None, keyed=False)`: sets `external_ref` when given; uses the keyed timeout when `keyed`.
- `record_blocked(..., idempotency_key=None)` passes it to `_insert` (a `blocked` row is outside the index predicate, so it cannot conflict).
- `REFUSAL_CODES = frozenset({"offered_set", "action_gate", "context_policy", "duplicate"})`.
- `nous/config.py`: `execution_ledger_keyed_write_timeout_seconds: float = Field(default=10.0, gt=0)` — a keyed send fails closed, so its writes wait longer than an ordinary row's.

- [ ] **Step 4: Run** `uv run --frozen pytest tests/test_ledger_store.py -q` → PASS.

- [ ] **Step 5: Commit**

```bash
git add nous/cognitive/ledger_store.py nous/config.py nous/main.py tests/test_ledger_store.py
git commit -m "feat(ledger): duplicate vs outage, dispatch gate, never-dispatched keys freed (2b)"
```

---

### Task 4: The outcome channel travels inside `dispatch()`

**Files:**
- Create: `nous/api/call_outcome.py`
- Modify: `nous/api/tools.py` (`ToolDispatcher.dispatch` `:381-520`)
- Test: `tests/test_call_outcome.py` (new)

**Interfaces:**
- Produces: `@dataclass class CallOutcome: external_ref: str | None = None; uncertain: bool = False`; `current_outcome() -> CallOutcome | None`; `ToolDispatcher.dispatch(..., outcome: CallOutcome | None = None)` — sets the context variable for the handler and resets it before returning, inside one task.

- [ ] **Step 1: Write the failing tests**

```python
import asyncio

import pytest

from nous.api.call_outcome import CallOutcome, current_outcome
from nous.api.tools import ToolDispatcher

_SCHEMA = {"name": "probe", "description": "d", "input_schema": {"type": "object", "properties": {}}}


def _dispatcher():
    d = ToolDispatcher()

    async def probe():
        o = current_outcome()
        o.external_ref, o.uncertain = "<m@x>", True
        return {"content": [{"type": "text", "text": "ok"}]}

    d.register("probe", probe, _SCHEMA)
    return d


def test_no_dispatch_means_no_outcome():
    assert current_outcome() is None


@pytest.mark.asyncio
async def test_a_handler_reports_through_dispatch():
    outcome = CallOutcome()
    await _dispatcher().dispatch("probe", {}, outcome=outcome)
    assert (outcome.external_ref, outcome.uncertain) == ("<m@x>", True)
    assert current_outcome() is None


@pytest.mark.asyncio
async def test_dispatch_in_a_task_resumed_elsewhere_does_not_leak_or_crash():
    """The keepalive path runs dispatch in a task and stream_chat is resumed chunk
    by chunk in new tasks (rest.py:207): nothing may span those boundaries."""
    d = _dispatcher()
    outcome = CallOutcome()

    async def gen():
        task = asyncio.create_task(d.dispatch("probe", {}, outcome=outcome))
        yield "keepalive"
        yield await task

    agen = gen()
    assert await asyncio.create_task(agen.__anext__()) == "keepalive"
    await asyncio.create_task(agen.__anext__())
    assert outcome.external_ref == "<m@x>"
```

- [ ] **Step 2: Run to verify they fail.**

- [ ] **Step 3: Implement** `nous/api/call_outcome.py`:

```python
"""What a tool call learned about its own effect (harness Phase 2b).

The runner creates a CallOutcome per call and passes it to
ToolDispatcher.dispatch(), which exposes it to the handler through a context
variable set and reset INSIDE dispatch -- a single task. It never spans a
generator yield (stream_chat is resumed chunk by chunk in new tasks).
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass


@dataclass
class CallOutcome:
    external_ref: str | None = None   # provider id: SMTP Message-ID, Telegram message_id
    uncertain: bool = False           # the provider may have acted although the call failed


_current: ContextVar[CallOutcome | None] = ContextVar("tool_call_outcome", default=None)


def current_outcome() -> CallOutcome | None:
    return _current.get()
```

In `ToolDispatcher.dispatch`, add `outcome: CallOutcome | None = None` to the signature and wrap the handler invocation:

```python
        token = _outcome_var.set(outcome)
        try:
            ...  # the existing handler call and result shaping, unchanged
        finally:
            _outcome_var.reset(token)
```

(`from nous.api.call_outcome import _current as _outcome_var` — the one setter.)

- [ ] **Step 4: Run** `uv run --frozen pytest tests/test_call_outcome.py tests/test_tools.py tests/test_tool_arg_salvage.py -q` → PASS.

- [ ] **Step 5: Commit**

```bash
git add nous/api/call_outcome.py nous/api/tools.py tests/test_call_outcome.py
git commit -m "feat(harness): per-call outcome channel inside dispatch (2b)"
```

---

### Task 5: Send handlers report provider ids and uncertain delivery

**Files:**
- Modify: `nous/api/email_tools.py` (`_build_message` `:532-586`, `_send_email_sync` `:610-628`, handler `:642-783`, schema `:792-837`)
- Modify: `nous/api/telegram_tools.py` (`send_file` `:75-161`, schema)
- Test: `tests/test_email_tools.py`, `tests/test_email_smtp_timeout.py`, `tests/test_telegram_tools.py`

**Interfaces:**
- Consumes: `current_outcome()` (Task 4).
- Produces: every built message has a `Message-ID`; `_send_email_sync(...) -> dict` (refused recipients); `class DeliveryUncertain(Exception)`; both tools accept `send_label: str | None = None` (schema: "Only when one task intentionally sends several messages to the same recipients: a short label that tells them apart.").

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_email_tools.py
from nous.api.call_outcome import CallOutcome, _current
from nous.api.email_tools import DeliveryUncertain, _build_message


def _with_outcome(coro):
    outcome = CallOutcome()
    token = _current.set(outcome)
    try:
        return asyncio.run(coro), outcome
    finally:
        _current.reset(token)


def test_every_message_has_a_message_id():
    msg, err = _build_message(_make_settings(), "s", "b", "nous@example.com", ["tim@example.com"], [], [])
    assert err is None and msg["Message-ID"].startswith("<") and msg["Message-ID"].endswith(">")


def test_the_message_id_reaches_the_outcome(no_real_send):
    resp, outcome = _with_outcome(create_send_email_tool(_make_settings())(
        to="tim@example.com", subject="hi", body="hello"))
    assert outcome.external_ref and outcome.external_ref.startswith("<")


def test_a_timeout_during_send_is_uncertain(monkeypatch):
    async def boom(func, *a, **k):
        raise DeliveryUncertain(TimeoutError("reply"))

    monkeypatch.setattr("nous.api.email_tools.asyncio.to_thread", boom)
    resp, outcome = _with_outcome(create_send_email_tool(_make_settings())(
        to="tim@example.com", subject="hi", body="hello"))
    assert resp.get("is_error") and outcome.uncertain and "uncertain" in _text(resp)


def test_a_partial_refusal_is_uncertain_and_named(monkeypatch):
    async def partial(func, *a, **k):
        return {"alice@example.com": (550, b"no such user")}

    monkeypatch.setattr("nous.api.email_tools.asyncio.to_thread", partial)
    resp, outcome = _with_outcome(create_send_email_tool(
        _make_settings(email_allowlist="tim@example.com, alice@example.com"))(
        to="tim@example.com, alice@example.com", subject="hi", body="hello"))
    assert resp.get("is_error") and outcome.uncertain and "alice@example.com" in _text(resp)


def test_send_label_is_accepted(no_real_send):
    resp, _ = _with_outcome(create_send_email_tool(_make_settings())(
        to="tim@example.com", subject="hi", body="hello", send_label="second"))
    assert not resp.get("is_error")
```

```python
# tests/test_email_smtp_timeout.py — the send stage is classified; QUIT never overrides it
import smtplib
import pytest
from nous.api.email_tools import DeliveryUncertain, _send_email_sync


class _SMTP:
    def __init__(self, fail_send=None, fail_quit=None, refused=None):
        self.fail_send, self.fail_quit, self.refused = fail_send, fail_quit, refused or {}

    def __call__(self, host, port, timeout):
        return self

    def starttls(self): pass
    def login(self, u, p): pass

    def send_message(self, msg, to_addrs=None):
        if self.fail_send:
            raise self.fail_send
        return self.refused

    def quit(self):
        if self.fail_quit:
            raise self.fail_quit


@pytest.mark.parametrize("error, uncertain", [
    (TimeoutError("reply"), True),
    (smtplib.SMTPServerDisconnected("gone"), True),
    (ConnectionResetError("rst"), True),
    (smtplib.SMTPDataError(554, b"rejected"), False),
    (smtplib.SMTPRecipientsRefused({}), False),
])
def test_send_stage_errors_are_classified(monkeypatch, error, uncertain):
    monkeypatch.setattr("nous.api.email_tools.smtplib.SMTP", _SMTP(fail_send=error, fail_quit=TimeoutError()))
    with pytest.raises(DeliveryUncertain if uncertain else type(error)):
        _send_email_sync(_settings(), ["a@x.io"], _msg())


def test_a_failing_quit_after_success_is_swallowed(monkeypatch):
    monkeypatch.setattr("nous.api.email_tools.smtplib.SMTP", _SMTP(fail_quit=TimeoutError()))
    assert _send_email_sync(_settings(), ["a@x.io"], _msg()) == {}
```

(`_settings()`/`_msg()` are small helpers in that file: a `Settings(_env_file=None, email_user=…, email_password=…)` and an `EmailMessage` with To/Subject.)

```python
# tests/test_telegram_tools.py
from nous.api.call_outcome import CallOutcome, _current


async def _run_with_outcome(coro):
    outcome = CallOutcome()
    token = _current.set(outcome)
    try:
        return await coro, outcome
    finally:
        _current.reset(token)


@pytest.mark.asyncio
async def test_message_id_reaches_the_outcome(tmp_png, mock_settings, mock_http):
    from nous.api.telegram_tools import create_send_file_tool

    mock_http.post = AsyncMock(return_value=_ok_response())
    _, outcome = await _run_with_outcome(create_send_file_tool(mock_settings, mock_http)(file_path=tmp_png))
    assert outcome.external_ref == "42"


@pytest.mark.asyncio
async def test_a_read_timeout_after_upload_is_uncertain(tmp_png, mock_settings, mock_http):
    import httpx

    from nous.api.telegram_tools import create_send_file_tool

    mock_http.post = AsyncMock(side_effect=httpx.ReadTimeout("slow"))
    resp, outcome = await _run_with_outcome(create_send_file_tool(mock_settings, mock_http)(file_path=tmp_png))
    assert resp.get("is_error") and outcome.uncertain


@pytest.mark.asyncio
async def test_a_connect_error_is_a_definite_failure(tmp_png, mock_settings, mock_http):
    import httpx

    from nous.api.telegram_tools import create_send_file_tool

    mock_http.post = AsyncMock(side_effect=httpx.ConnectError("down"))
    _, outcome = await _run_with_outcome(create_send_file_tool(mock_settings, mock_http)(file_path=tmp_png))
    assert not outcome.uncertain
```

- [ ] **Step 2: Run to verify they fail.**

- [ ] **Step 3: Implement**

`email_tools.py`:

```python
from email.utils import make_msgid

from nous.api.call_outcome import current_outcome


class DeliveryUncertain(Exception):
    """The server may have accepted the message: the connection failed during
    the send transaction. Never reported as a definite failure."""
```

`_build_message`: after the `Cc` header, `msg["Message-ID"] = make_msgid(domain=(from_addr.rsplit("@", 1)[-1] or "nous.local"))`.

`_send_email_sync` keeps the explicit server object and the swallowed `quit()` (a `with smtplib.SMTP(...)` block would let a failing QUIT replace the real error):

```python
    server = smtplib.SMTP(host, port, timeout=settings.email_smtp_timeout_seconds)
    try:
        server.starttls()
        server.login(user, password)
        try:
            refused = server.send_message(msg, to_addrs=recipients)
        except (smtplib.SMTPServerDisconnected, TimeoutError) as exc:
            raise DeliveryUncertain(exc) from exc
        except smtplib.SMTPException:
            raise  # the server answered and said no: definite
        except OSError as exc:
            raise DeliveryUncertain(exc) from exc
        return refused or {}
    finally:
        try:
            server.quit()
        except Exception:  # a failing QUIT never changes the send's outcome
            pass
```

(`SMTPServerDisconnected` is an `SMTPException`, which is an `OSError` — the order matters.)

Handler: add `send_label: str | None = None` (accepted, not used by the send); after `_build_message`, `if outcome := current_outcome(): outcome.external_ref = msg["Message-ID"]` (known before sending); then:

```python
        try:
            refused = await asyncio.to_thread(_send_email_sync, settings, all_recipients, msg)
        except DeliveryUncertain as exc:
            if outcome := current_outcome():
                outcome.uncertain = True
            logger.error("send_email delivery uncertain (to=%s): %s", to_list, type(exc.__cause__).__name__)
            return _error("email delivery uncertain: the server may have accepted it; not retrying automatically.")
        except Exception as e:
            ...  # existing generic definite-failure path, unchanged
        if refused:
            if outcome := current_outcome():
                outcome.uncertain = True
            return _error(f"email sent to some recipients but refused for: {', '.join(sorted(refused))}")
```

Schema: add `"send_label": {"type": "string", "description": "…"}` (not required).

`telegram_tools.py`: add `send_label: str | None = None`; after the `ok` check, `if outcome := current_outcome(): outcome.external_ref = str((result.get("result") or {}).get("message_id") or "") or None`. Split the network errors:

```python
        except (httpx.ConnectError, httpx.ConnectTimeout) as e:
            return _error(f"Failed to send file: network error ({type(e).__name__})")
        except httpx.HTTPError as e:  # the request may have been delivered
            if outcome := current_outcome():
                outcome.uncertain = True
            return _error(f"Could not confirm the file was sent ({type(e).__name__}); it may have been delivered.")
```

Schema: add `send_label`.

- [ ] **Step 4: Run** `uv run --frozen pytest tests/test_email_tools.py tests/test_email_smtp_timeout.py tests/test_telegram_tools.py tests/test_handler_error_flags.py -q` → PASS (the `no_real_send` fixture returns `None` from `to_thread`: `if refused:` treats it as none).

- [ ] **Step 5: Commit**

```bash
git add nous/api/email_tools.py nous/api/telegram_tools.py tests/test_email_tools.py tests/test_email_smtp_timeout.py tests/test_telegram_tools.py
git commit -m "feat(sends): Message-ID and message_id recorded; uncertain delivery never frees a key (2b)"
```

---

### Task 6: The runner keys sends, suppresses repeats, fails closed

**Files:**
- Modify: `nous/api/runner.py` (`_ledger_open`, `_ledger_close*`, `_ledger_blocked`; `stream_chat` `:1675-1702`; `_tool_loop` `:2203-2241`; `_dispatch_with_keepalive` `:2957-3012`)
- Test: `tests/test_runner_ledger.py`

**Interfaces:**
- Consumes: Tasks 2-5.
- Produces: `@dataclass(frozen=True) class Suppressed: text: str; is_error: bool`; `AgentRunner._open_for_call(ctx, tool_name, tool_input, turn, keys_this_turn: set[str]) -> tuple[Any, str | None, Suppressed | None]` (entry id, idempotency key, suppression) used by both loops — each loop invocation owns one `keys_this_turn` set and adds every key it dispatches; `_dispatch_with_keepalive(..., outcome=)`.

- [ ] **Step 1: Write the failing tests** — extend `_FakeStore` with: `keys: list`, `closes: list[(status, external_ref)]`, constructor options `duplicate: HeldKey | None`, `fail_open`, `fail_claim`; `open_entry(..., idempotency_key=None)` records the key and raises `DuplicateSend(self.duplicate)` / `LedgerWriteError` as configured (after the first call when `duplicate_after_first=True`); `claim_dispatch(entry_id)` returns `not fail_claim`; `record_blocked(..., refused_by, idempotency_key=None)`; `close_entry(..., output_of=None, external_ref=None, keyed=False)`. Every dispatcher double gains `outcome=None`: `_RecordingDispatcher` (`tests/test_runner_authorization.py`), and the fixed-signature fakes at `tests/test_runner.py:1084`, `:1131` and `tests/test_runner_background.py:281`.

```python
def _dag_ctx():
    return ExecutionContext(kind="dag_node", dag_id=uuid.uuid4(), dag_node_name="send")


def _held(status, subject="s"):
    from nous.cognitive.ledger_store import HeldKey
    from nous.cognitive.ledger_store import _digest
    return HeldKey(uuid.uuid4(), status, "<m1@x>", datetime.now(UTC), datetime.now(UTC),
                   {"subject_sha256": _digest(subject)[0]})


def _email_call():
    return _one_tool_call_then_done_with("send_email", {"to": "a@x.io", "subject": "s", "body": "b"})


@pytest.mark.asyncio
async def test_a_dag_node_send_opens_with_its_key_and_claims_dispatch():
    store = _FakeStore()
    r, d = _runner(store, offered=("send_email",))
    r._call_api = _email_call()
    await _run_loop(r, is_background=True, context=_dag_ctx())
    assert store.keys[0].startswith("dag:") and [c[0] for c in d.calls] == ["send_email"]


@pytest.mark.asyncio
@pytest.mark.parametrize("subject", ["s", "a reworded subject"])
async def test_a_key_held_from_an_earlier_turn_is_already_sent_whatever_the_wording(subject):
    """A retry rewrites the subject; it must never be told how to send again."""
    store = _FakeStore(duplicate=_held("success", subject="s"))
    r, d = _runner(store, offered=("send_email",))
    r._call_api = _one_tool_call_then_done_with("send_email", {"to": "a@x.io", "subject": subject, "body": "b"})
    ledger = ExecutionLedger(session_id="s1")
    await _run_loop(r, is_background=True, ledger=ledger, context=_dag_ctx())
    assert d.calls == []
    assert ("blocked", "send_email", "duplicate") in store.events
    assert [(a.tool_name, a.status) for a in ledger.actions] == [("send_email", "success")]  # 2c stays grounded
    assert "send_label" not in ledger.actions[0].result_summary


@pytest.mark.asyncio
async def test_a_second_send_in_the_same_turn_asks_for_a_label():
    store = _FakeStore(duplicate=_held("success"), duplicate_after_first=True)
    r, d = _runner(store, offered=("send_email",))
    r._call_api = _two_tool_calls_then_done_with("send_email", {"to": "a@x.io", "subject": "s", "body": "b"})
    ledger = ExecutionLedger(session_id="s1")
    await _run_loop(r, is_background=True, ledger=ledger, context=_dag_ctx())
    assert [c[0] for c in d.calls] == ["send_email"]                 # the first went out
    assert ledger.actions[1].status == "blocked" and "send_label" in ledger.actions[1].result_summary


@pytest.mark.asyncio
async def test_a_keyed_send_fails_closed_on_a_ledger_outage():
    store = _FakeStore(fail_open=True)
    r, d = _runner(store, offered=("send_email",))
    r._call_api = _email_call()
    await _run_loop(r, is_background=True, context=_dag_ctx())
    assert d.calls == [] and ("close", store.failed_id, "error") in store.events


@pytest.mark.asyncio
async def test_a_failed_dispatch_claim_refuses_the_send():
    store = _FakeStore(fail_claim=True)
    r, d = _runner(store, offered=("send_email",))
    r._call_api = _email_call()
    await _run_loop(r, is_background=True, context=_dag_ctx())
    assert d.calls == []


@pytest.mark.asyncio
async def test_an_unkeyed_call_still_fails_open():
    store = _FakeStore(fail_open=True)
    r, d = _runner(store, offered=("send_email",))
    r._call_api = _email_call()
    await _run_loop(r)  # interactive: unkeyed
    assert [c[0] for c in d.calls] == ["send_email"]


@pytest.mark.asyncio
async def test_an_uncertain_send_closes_unknown_with_its_ref():
    store = _FakeStore()
    r, d = _runner(store, offered=("send_email",))

    async def uncertain(name, inp, *, outcome=None, **kw):
        outcome.external_ref, outcome.uncertain = "<m@x>", True
        return "delivery uncertain", True

    d.dispatch = uncertain
    r._call_api = _email_call()
    await _run_loop(r, is_background=True, context=_dag_ctx())
    assert store.closes[-1] == ("unknown", "<m@x>")


@pytest.mark.asyncio
async def test_a_cancelled_send_keeps_its_ref():
    store = _FakeStore()
    r, d = _runner(store, offered=("send_email",))
    reached = asyncio.Event()

    async def hang(name, inp, *, outcome=None, **kw):
        outcome.external_ref = "<m@x>"
        reached.set()
        await asyncio.sleep(3600)

    d.dispatch = hang
    r._call_api = _email_call()
    task = asyncio.create_task(_run_loop(r, is_background=True, context=_dag_ctx()))
    await asyncio.wait_for(reached.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert store.closes[-1] == ("unknown", "<m@x>")


@pytest.mark.asyncio
async def test_stream_chat_survives_a_slow_tool_resumed_across_tasks():
    """rest.py:207 resumes stream_chat with create_task(aiter.__anext__())."""
    store = _FakeStore()

    async def slow(name, inp, *, outcome=None, **kw):
        await asyncio.sleep(0.05)
        return "done", False

    runner = _stream_runner(store, slow)  # keepalive_interval=0.01 < 0.05
    runner._call_api_stream = _one_streamed_call()
    agen = runner.stream_chat("s1", "go").__aiter__()
    events = []
    while True:
        try:
            events.append(await asyncio.create_task(agen.__anext__()))
        except StopAsyncIteration:
            break
    assert any(getattr(e, "type", None) == "keepalive" for e in events)
    assert store.events[-1][0] == "close"
```

(`_one_tool_call_then_done_with(name, input)` and `_two_tool_calls_then_done_with(name, input)` — variants of the shared helper that send a tool input once / in two successive iterations; add them next to `_one_tool_call_then_done` in `tests/test_runner_authorization.py`.)

- [ ] **Step 2: Run to verify they fail.**

- [ ] **Step 3: Implement**

```python
@dataclass(frozen=True)
class Suppressed:
    text: str
    is_error: bool


    async def _open_for_call(self, ctx, tool_name, tool_input, turn, keys_this_turn):
        """Open the durable row. A keyed send is dispatched only after its row
        exists AND its dispatch is claimed; a held key suppresses it; any
        ledger failure refuses it (fail closed). ``keys_this_turn`` holds the
        keys this loop invocation already dispatched."""
        if self._ledger_store is None or not self._dispatcher.is_registered(tool_name):
            return None, None, None
        key = idempotency_key(ctx, tool_name, tool_input,
                              default_chat_id=self._settings.telegram_chat_id)
        if key is None:
            return await self._ledger_open(ctx, tool_name, tool_input, turn), None, None
        entry_id = None
        try:
            entry_id = await self._ledger_store.open_entry(
                context=ctx, tool_name=tool_name, tool_input=tool_input, turn=turn,
                idempotency_key=key)
            if not await self._ledger_store.claim_dispatch(entry_id):
                raise LedgerWriteError(entry_id, RuntimeError("dispatch claim lost"))
            keys_this_turn.add(key)
            return entry_id, key, None
        except DuplicateSend as dup:
            await self._ledger_blocked(ctx, tool_name, tool_input, turn, "duplicate",
                                       idempotency_key=key)
            return None, key, self._suppression(dup.held, same_turn=key in keys_this_turn)
        except LedgerWriteError as exc:
            logger.error("Harness: keyed %s refused, ledger unavailable: %s", tool_name, exc)
            await self._ledger_close(exc.entry_id, "error", "ledger write failed; send refused",
                                     keyed=True)
            return None, key, Suppressed(
                "Send refused: the execution ledger is unavailable, so a duplicate "
                "cannot be ruled out. Retry later.", True)

    @staticmethod
    def _suppression(held: HeldKey, *, same_turn: bool) -> Suppressed:
        """Decided by ATTEMPT, never by wording: a retry rewrites the subject,
        so a key held from an earlier turn is simply "already sent" -- a reply
        that mentioned labels there would teach the retry how to duplicate."""
        when = held.created_at.isoformat(timespec="seconds")
        ref = f" (ref {held.external_ref})" if held.external_ref else ""
        if held.status != "success":
            return Suppressed(
                f"A send to the same recipients from this task is {held.status} since {when}{ref}; "
                "not sending again, to avoid a duplicate.", True)
        if not same_turn:
            return Suppressed(f"Already sent at {when}{ref}; not sending it again.", False)
        return Suppressed(
            f"This turn already sent to the same recipients at {when}{ref}. If this is "
            "intentionally a second message, call again with a distinct send_label.", True)
```

(`HeldKey`/`DuplicateSend` are imported from `nous.cognitive.ledger_store`; `idempotency_key` from `nous.api.idempotency`.)

In **both** loops, create `keys_this_turn: set[str] = set()` once per invocation (before the iteration loop) and replace the ledger open with `entry_id, key, suppressed = await self._open_for_call(..., keys_this_turn)`. When `suppressed` is set: skip dispatch; `result_text, is_error = suppressed.text, suppressed.is_error`; record the in-memory ledger `"success"` when not an error, else `"blocked"`; build the `tool_result`/`ToolResult` as for any call. Otherwise create `outcome = CallOutcome()` and pass it to the dispatch (`self._dispatcher.dispatch(..., outcome=outcome)` in `_tool_loop`; `_dispatch_with_keepalive(..., outcome=outcome)` forwards it to `dispatch` inside its task in `stream_chat`). Every close — normal, cancelled, exception, stream disconnect — passes `external_ref=outcome.external_ref, keyed=key is not None`; the normal close's status:

```python
            status = ("unknown" if timed_out or (is_error and outcome.uncertain)
                      else "error" if is_error else "success")
```

(`_tool_loop` has no `timed_out`: use `False` there.) `_ledger_close*` and `_ledger_blocked` gain the `external_ref`/`keyed`/`idempotency_key` pass-throughs.

- [ ] **Step 4: Run** `uv run --frozen pytest tests/test_runner_ledger.py tests/test_runner_authorization.py tests/test_streaming.py tests/test_streaming_keepalive.py tests/test_claim_verifier.py tests/test_runner.py tests/test_runner_background.py -q` → PASS.

- [ ] **Step 5: Commit**

```bash
git add nous/api/runner.py tests/test_runner_ledger.py tests/test_runner_authorization.py
git commit -m "feat(harness): keyed sends are claimed before dispatch, suppressed on repeat, refused on outage (2b)"
```

---

### Task 7: Docs

**Files:** `CLAUDE.md`

- [ ] **Step 1:** Add `| \`NOUS_EXECUTION_LEDGER_KEYED_WRITE_TIMEOUT_SECONDS\` | \`10.0\` | Harness Phase 2b: bound on each ledger write for a KEYED send (send_email / send_file in a DAG node, DAG summary, heartbeat callback, or subtask). A keyed send fails CLOSED when its row cannot be written or its dispatch claimed — a duplicate could not be ruled out — so it waits longer than an ordinary row. |` and extend the `NOUS_EXECUTION_LEDGER_PERSIST_ENABLED` row: "Phase 2b: an external send in a DAG node, the DAG summary turn (session `dag-summary-{dag id}-g{delivery generation}` and the subtasks it spawns), a heartbeat callback run, or a subtask carries an idempotency key naming the logical send — scope + recipients (or chat + file name) + an optional `send_label`, never the subject/body/caption the model rewrites on retry. A partial UNIQUE index (migration 075) lets one live row hold a key, and dispatch is gated on a second write (`dispatched_at`), so a keyed row that was never dispatched is freed by the orphan sweep instead of held. A repeat with the same subject is suppressed with a note carrying the first send's time and provider id (SMTP `Message-ID`, Telegram `message_id`, in `external_ref`); a different subject asks for a `send_label`; a ledger failure refuses the send. A connection failure during the SMTP send, a partial recipient refusal, or a Telegram timeout after upload closes the row `unknown`, which holds the key: never auto-resent (release: `UPDATE … SET status='error'`). Interactive, MCP and heartbeat-check sends are unkeyed — an operator re-send from chat always goes out."

- [ ] **Step 2: Commit** `git add CLAUDE.md && git commit -m "docs: idempotent sends (harness 2b)"`

---

## Out of scope (recorded)

- An operator REST endpoint to release a held `unknown` key.
- Keying `bash`/`run_python` sends (`curl`, `sendmail`, smtplib in a script): no structured recipient to key on.
- Splitting SMTP MAIL/RCPT from DATA so a timeout before DATA reads as definite (today: any send-stage connection failure is conservatively "uncertain").
- Heartbeat check sends: each tick is a distinct send by design.
- A claimed-but-never-sent row: if `claim_dispatch` commits after its wait times out and the best-effort close also fails, or a cancel lands between the claim and the dispatch, the row has `dispatched_at` set and nothing was sent; the sweep marks it `unknown`, which holds the key until an operator releases it. Rare (both writes must fail in the same window); the conservative direction.
