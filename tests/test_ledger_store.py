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


def test_send_email_stores_recipients_and_subject_not_the_body():
    args = durable_key_args("send_email", {"to": "a@b.c", "subject": "Premarket", "body": "secret numbers"})
    assert args["to"] == "a@b.c" and args["subject"] == "Premarket"
    assert "body" not in args and "body_sha256" in args


def test_unknown_tools_store_argument_names_only():
    args = durable_key_args("brand_new_tool", {"token": "abc", "target": "x"})
    assert args == {"arg_names": "target,token"}


def test_redaction_runs_before_truncation():
    """A password cut by truncation must still be redacted."""
    url = "https://nous:" + "p" * (KEY_ARG_CHARS + 50) + "@example.com/skill.md"
    out = durable_key_args("learn_skill", {"source": url})
    assert "ppppp" not in out["source"]


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
    assert rows[0].completed_at is not None


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
async def test_prune_is_agent_scoped(store, db, agent):
    mine = await store.open_entry(context=ExecutionContext(kind="subtask"), tool_name="learn_fact",
                                  tool_input={}, turn=1)
    other = LedgerStore(db, f"{agent}-other")
    theirs = await other.open_entry(context=ExecutionContext(kind="subtask"), tool_name="learn_fact",
                                    tool_input={}, turn=1)
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
