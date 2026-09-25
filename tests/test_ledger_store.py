"""Harness Phase 1b: durable execution ledger."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, update

from nous.api.execution_context import ExecutionContext
from nous.cognitive.execution_ledger import redact_text
from nous.cognitive.ledger_store import (
    KEY_ARG_CHARS,
    LedgerStore,
    LedgerWriteError,
    durable_key_args,
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


def test_send_email_stores_recipients_but_hashes_subject_and_body():
    args = durable_key_args("send_email", {"to": "tim@example.com", "subject": "Premarket", "body": "numbers"})
    assert args["to"] == "tim@example.com"
    assert "subject" not in args and "subject_sha256" in args
    assert "body" not in args and "body_sha256" in args


@pytest.mark.parametrize("tool, args", [
    ("send_email", {"to": "tim@example.com", "subject": "sk-ABCDEFGHIJKLMNOP"}),
    ("send_email", {"to": "sk-ABCDEFGHIJKLMNOP", "subject": "x"}),
    ("learn_fact", {"subject": "sk-ABCDEFGHIJKLMNOP", "category": "technical", "content": "c"}),
    ("learn_fact", {"subject": "s", "category": "sk-ABCDEFGHIJKLMNOP", "content": "c"}),
    ("learn_skill", {"source": "https://u:sk-ABCDEFGHIJKLMNOP@example.com/s.md?token=sk-ABCDEFGHIJKLMNOP"}),
    ("learn_skill", {"source": "sk-ABCDEFGHIJKLMNOP"}),
    ("cancel_task", {"task_id": "sk-ABCDEFGHIJKLMNOP"}),
    ("send_file", {"file_path": "/tmp/x.png", "chat_id": "sk-ABCDEFGHIJKLMNOP"}),
    ("heartbeat_check_create", {"name": "sk-ABCDEFGHIJKLMNOP", "prompt": "p"}),
    ("schedule_task", {"every": "sk-ABCDEFGHIJKLMNOP", "task": "t"}),
    ("ingest_document", {"source_ref": "sk-ABCDEFGHIJKLMNOP", "content": "c"}),
])
def test_free_text_values_are_never_kept(tool, args):
    """codex r3 on #645: a pattern redactor cannot see a bare `sk-...` key, so a
    value is kept only when its SHAPE proves it holds no free text."""
    assert "sk-" not in str(durable_key_args(tool, args))


def test_shape_proven_values_are_kept():
    import uuid

    task_id = str(uuid.uuid4())
    assert durable_key_args("cancel_task", {"task_id": task_id}) == {"task_id": task_id}
    assert durable_key_args("learn_fact", {"category": "technical"})["category"] == "technical"
    assert durable_key_args("send_file", {"chat_id": "-100123"})["chat_id"] == "-100123"
    assert durable_key_args("send_email", {"to": ["a@example.com", "b@example.org"]})["to"] == (
        "a@example.com,b@example.org")
    source = durable_key_args("learn_skill", {"source": "https://u:pw@example.com/skills/x.md?t=1"})
    assert source["source"] == "https://example.com" and "source_sha256" in source
    assert durable_key_args("learn_skill", {"source": "inline"})["source"] == "inline"


def test_unknown_tools_store_argument_names_only():
    args = durable_key_args("brand_new_tool", {"token": "abc", "target": "x"})
    assert args == {"arg_names": "target,token"}


@pytest.mark.parametrize("args", [
    {"sk-ABCDEFGHIJKLMNOP": ""},
    {"ok": 1, "x" * 200: 2},
    {f"name_{chr(97 + i)}{chr(97 + j)}": 0 for i in range(5) for j in range(5)},
])
def test_unshaped_or_unbounded_argument_names_are_hashed(args):
    """codex r5 on #645: the JSON keys are model-controlled too."""
    out = durable_key_args("brand_new_tool", args)
    assert set(out) == {"arg_count", "arg_names_sha256"}
    assert out["arg_count"] == str(len(args)) and "sk-" not in str(out)


def test_an_over_long_target_path_is_hashed_not_truncated():
    path = "/tmp/nous-workspace/" + "d" * (KEY_ARG_CHARS + 50) + "/x.txt"
    out = durable_key_args("write_file", {"path": path, "content": "c"})
    assert "path" not in out and out["path_len"] == str(len(path))


def test_bash_command_is_stored_as_a_hash_never_verbatim():
    """codex r2 on #645: a pattern redactor cannot see a bare `sk-...` key or
    a heredoc payload, so the command is code and is hashed like run_python's."""
    args = durable_key_args("bash", {"command": "echo sk-ABCDEFGHIJKLMNOP > file"})
    assert set(args) == {"command_sha256", "command_len"}
    assert "sk-" not in str(args)


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
async def test_a_bash_write_behind_a_read_command_is_persisted(store, db):
    """codex r1 on #645: `echo x > f` starts with a read command, and the
    first-token classifier skipped it -- a write with no durable row."""
    entry_id = await store.open_entry(context=ExecutionContext(kind="interactive"), tool_name="bash",
                                      tool_input={"command": "echo x > f"}, turn=1)
    row = await _row(db, entry_id)
    assert (row.status, row.side_effect_type) == ("pending", "write")


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
async def test_result_summary_redaction_runs_before_truncation(store, db):
    """A password cut by truncation must still be redacted."""
    entry_id = await store.open_entry(context=ExecutionContext(kind="subtask"), tool_name="learn_fact",
                                      tool_input={"content": "c"}, turn=1)
    await store.close_entry(entry_id, status="success",
                            result_summary="x" * 480 + " password=" + "p" * 80)
    assert "ppppp" not in (await _row(db, entry_id)).result_summary


@pytest.mark.asyncio
async def test_result_summary_is_redacted(store, db):
    entry_id = await store.open_entry(context=ExecutionContext(kind="subtask"),
                                      tool_name="run_python", tool_input={"code": "x"}, turn=1)
    await store.close_entry(entry_id, status="success", result_summary="DB_PASSWORD=hunter2 printed")
    assert "hunter2" not in (await _row(db, entry_id)).result_summary


@pytest.mark.asyncio
async def test_bash_output_keeps_only_its_shape(store, db):
    entry_id = await store.open_entry(context=ExecutionContext(kind="subtask"), tool_name="bash",
                                      tool_input={"command": "echo x > f"}, turn=1)
    await store.close_entry(entry_id, status="success", output_of="bash",
                            result_summary="sk-ABCDEFGHIJKLMNOP\nExit code: 0")
    summary = (await _row(db, entry_id)).result_summary
    assert "sk-" not in summary and "exit code 0" in summary


@pytest.mark.asyncio
async def test_bash_timeout_keeps_the_reason_not_the_echoed_command(store, db):
    entry_id = await store.open_entry(context=ExecutionContext(kind="subtask"), tool_name="bash",
                                      tool_input={"command": "echo x > f"}, turn=1)
    await store.close_entry(entry_id, status="error", output_of="bash",
                            result_summary="Command timed out after 30s.\nCommand: echo sk-ABCDEFGHIJKLMNOP > f")
    summary = (await _row(db, entry_id)).result_summary
    assert summary.startswith("Command timed out after 30s.") and "sk-" not in summary


@pytest.mark.asyncio
async def test_run_python_output_is_never_stored(store, db):
    entry_id = await store.open_entry(context=ExecutionContext(kind="subtask"), tool_name="run_python",
                                      tool_input={"code": "print(key)"}, turn=1)
    await store.close_entry(entry_id, status="success", output_of="run_python",
                            result_summary="sk-ABCDEFGHIJKLMNOP")
    assert "sk-" not in (await _row(db, entry_id)).result_summary


@pytest.mark.asyncio
async def test_no_tool_output_is_stored_because_handlers_echo_their_arguments(store, db):
    """learn_fact's success text quotes the subject that key_args hashes."""
    entry_id = await store.open_entry(context=ExecutionContext(kind="subtask"), tool_name="learn_fact",
                                      tool_input={"subject": "sk-ABCDEFGHIJKLMNOP", "content": "c"}, turn=1)
    await store.close_entry(entry_id, status="success", output_of="learn_fact",
                            result_summary="Fact learned successfully.\nSubject: sk-ABCDEFGHIJKLMNOP")
    summary = (await _row(db, entry_id)).result_summary
    assert "sk-" not in summary and "chars of output" in summary


@pytest.mark.asyncio
async def test_close_rejects_non_terminal_status(store):
    with pytest.raises(ValueError):
        await store.close_entry(uuid.uuid4(), status="pending", result_summary=None)


@pytest.mark.asyncio
async def test_record_blocked_writes_a_terminal_row(store, db, agent):
    await store.record_blocked(context=ExecutionContext(kind="heartbeat_triage"), tool_name="send_file",
                               tool_input={"file_path": "/x"}, turn=1, refused_by="offered_set")
    async with db.session() as s:
        rows = (await s.execute(
            select(ExecutionLedgerEntry)
            .where(ExecutionLedgerEntry.agent_id == agent)
            .order_by(ExecutionLedgerEntry.created_at)
        )).scalars().all()
    assert [(r.tool_name, r.status, r.result_summary) for r in rows] == [
        ("send_file", "blocked", "refused by offered_set")]
    assert rows[0].completed_at is not None


@pytest.mark.asyncio
async def test_record_blocked_accepts_only_a_known_refusal_code(store):
    with pytest.raises(ValueError):
        await store.record_blocked(context=ExecutionContext(kind="interactive"), tool_name="send_file",
                                   tool_input={}, turn=1, refused_by="the gate said sk-ABCDEFGHIJKLMNOP")


@pytest.mark.parametrize("tool, args", [
    ("ingest_document", {"source_ref": "http://[", "content": "c"}),
    ("learn_skill", {"source": "https://[::1"}),
    ("send_email", {"to": {"nested": "dict"}}),
])
def test_a_value_that_fails_to_parse_is_hashed_not_raised(tool, args):
    """codex r4 on #645: urlsplit raises on `http://[`; that escaped, the
    insert failed open, and the call left no durable row."""
    out = durable_key_args(tool, args)
    assert any(k.endswith("_sha256") for k in out)


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
    old = await store.open_entry(context=ExecutionContext(kind="subtask"), tool_name="learn_fact",
                                 tool_input={}, turn=1)
    other = LedgerStore(db, f"{agent}-other")
    foreign = await other.open_entry(context=ExecutionContext(kind="subtask"), tool_name="learn_fact",
                                     tool_input={}, turn=1)
    async with db.session() as s:
        await s.execute(update(ExecutionLedgerEntry)
                        .where(ExecutionLedgerEntry.id.in_([old, foreign]))
                        .values(created_at=datetime.now(UTC) - timedelta(hours=3)))
        await s.commit()
    fresh = await store.open_entry(context=ExecutionContext(kind="subtask"), tool_name="learn_fact",
                                   tool_input={}, turn=1)

    assert await store.mark_orphans_unknown(older_than_seconds=3600) == 1
    assert (await _row(db, old)).status == "unknown"
    assert (await _row(db, fresh)).status == "pending"
    assert (await _row(db, foreign)).status == "pending"


@pytest.mark.asyncio
async def test_prune_never_deletes_a_pending_row(store, db):
    """codex r3 on #645: retention and the call timeouts are configured
    independently, so a long-running call's 'pending' row can be older than
    the retention window. Deleting it would make the call vanish."""
    live = await store.open_entry(context=ExecutionContext(kind="subtask"), tool_name="learn_fact",
                                  tool_input={}, turn=1)
    async with db.session() as s:
        await s.execute(update(ExecutionLedgerEntry).where(ExecutionLedgerEntry.id == live)
                        .values(created_at=datetime.now(UTC) - timedelta(days=200)))
        await s.commit()
    assert await store.prune(retention_days=90) == 0
    assert (await _row(db, live)).status == "pending"


@pytest.mark.asyncio
async def test_prune_is_agent_scoped(store, db, agent):
    mine = await store.open_entry(context=ExecutionContext(kind="subtask"), tool_name="learn_fact",
                                  tool_input={}, turn=1)
    other = LedgerStore(db, f"{agent}-other")
    theirs = await other.open_entry(context=ExecutionContext(kind="subtask"), tool_name="learn_fact",
                                    tool_input={}, turn=1)
    await store.close_entry(mine, status="success", result_summary=None)
    await other.close_entry(theirs, status="success", result_summary=None)
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


@pytest.mark.asyncio
async def test_shutdown_cancels_the_maintenance_loop_and_lets_pending_closes_land():
    """main.shutdown_components: the ledger loop is cancelled and in-flight
    shielded closes get a bounded chance to finish before the pool closes."""
    import asyncio
    from types import SimpleNamespace

    from nous.main import shutdown_components

    loop_task = asyncio.create_task(asyncio.sleep(3600))
    landed = []

    async def close():
        await asyncio.sleep(0.05)
        landed.append(True)

    pending = asyncio.ensure_future(close())

    async def _runner_close():
        return None

    await shutdown_components({
        "execution_ledger_task": loop_task,
        "runner": SimpleNamespace(_ledger_pending_tasks={pending}, close=_runner_close),
    })
    assert loop_task.cancelled()
    assert landed == [True]


# ---- harness Phase 2b: idempotency keys (migration 075) ----


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
    await insert("blocked")        # nor does a refusal
    await insert("pending")
    with pytest.raises(IntegrityError):
        await insert("success")    # a second live row for the same key


def test_the_index_predicate_covers_every_closable_status():
    from nous.cognitive.ledger_store import _CLOSABLE, KEY_HOLDING_STATUSES

    assert set(_CLOSABLE) <= set(KEY_HOLDING_STATUSES)
    assert set(KEY_HOLDING_STATUSES) == {"pending", "success", "unknown"}


def test_the_model_index_matches_the_migration():
    from pathlib import Path

    from nous.cognitive.ledger_store import KEY_HOLDING_STATUSES

    sql = Path("sql/migrations/075_execution_ledger_idempotency.sql").read_text(encoding="utf-8")
    assert "uq_execution_ledger_idempotency" in sql and "dispatched_at" in sql
    for status in KEY_HOLDING_STATUSES:
        assert f"'{status}'" in sql
    (index,) = [i for i in ExecutionLedgerEntry.__table__.indexes if i.name == "uq_execution_ledger_idempotency"]
    assert index.unique and [c.name for c in index.columns] == ["agent_id", "tool_name", "idempotency_key"]
    assert "dispatched_at" in ExecutionLedgerEntry.__table__.columns


# ---- harness Phase 2b: the store keys sends ----


@pytest.mark.asyncio
async def test_a_repeat_key_raises_duplicate_with_the_held_row(store):
    from nous.cognitive.ledger_store import DuplicateSend

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
    assert held.dispatched_at is not None
    assert "subject_sha256" in held.key_args


@pytest.mark.asyncio
async def test_the_same_key_on_another_tool_is_a_different_send(store):
    ctx = ExecutionContext(kind="dag_node")
    await store.open_entry(context=ctx, tool_name="send_email", tool_input={}, turn=1, idempotency_key="kx")
    assert await store.open_entry(context=ctx, tool_name="send_file", tool_input={}, turn=1,
                                  idempotency_key="kx") is not None


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
    assert await store.mark_orphans_unknown(older_than_seconds=None) == 3
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
async def test_a_fresh_undispatched_holder_is_in_flight(store):
    from nous.cognitive.ledger_store import DuplicateSend

    ctx = ExecutionContext(kind="dag_node")
    await store.open_entry(context=ctx, tool_name="send_email", tool_input={}, turn=1, idempotency_key="k6b")
    with pytest.raises(DuplicateSend) as exc:
        await store.open_entry(context=ctx, tool_name="send_email", tool_input={}, turn=2, idempotency_key="k6b")
    assert exc.value.held.status == "pending"


@pytest.mark.asyncio
async def test_a_stale_but_dispatched_holder_is_never_freed(store, db):
    from nous.cognitive.ledger_store import DuplicateSend

    ctx = ExecutionContext(kind="dag_node")
    held = await store.open_entry(context=ctx, tool_name="send_email", tool_input={}, turn=1,
                                  idempotency_key="k6c")
    await store.claim_dispatch(held)
    async with db.session() as s:
        await s.execute(update(ExecutionLedgerEntry).where(ExecutionLedgerEntry.id == held)
                        .values(created_at=datetime.now(UTC) - timedelta(minutes=5)))
        await s.commit()
    with pytest.raises(DuplicateSend):
        await store.open_entry(context=ctx, tool_name="send_email", tool_input={}, turn=2, idempotency_key="k6c")
    assert (await _row(db, held)).status == "pending"


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
    with pytest.raises(LedgerWriteError):
        await LedgerStore(_Broken(), agent).claim_dispatch(uuid.uuid4())


def test_the_keyed_timeout_setting():
    from nous.config import Settings

    assert Settings(_env_file=None).execution_ledger_keyed_write_timeout_seconds == 10.0
    assert LedgerStore(object(), "a", keyed_write_timeout_seconds=3.0)._keyed_timeout == 3.0
