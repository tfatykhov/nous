"""Execution ledger detail capture (migration 086): the redacted call input and output."""

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, update

from nous.api.execution_context import ExecutionContext
from nous.api.harness_dashboard import INPUT_PREVIEW_CHARS, get_execution_data, get_execution_detail
from nous.cognitive import ledger_store
from nous.cognitive.ledger_store import (
    DETAIL_OUTPUT_CHARS,
    SECRET_MASK,
    LedgerStore,
    detail_text,
)
from nous.storage.models import ExecutionLedgerEntry

CTX = ExecutionContext(kind="interactive")
MODES = {
    "persist": True,
    "retention_days": 90,
    "detail": True,
    "detail_retention_days": 30,
    "offered_set": "warn",
    "context_policy": "warn",
    "claim_verification": "enforce",
    "action_gating": "off",
    "events_persisted": True,
}


@pytest.fixture
def agent():
    return f"ledger-detail-{uuid.uuid4().hex[:8]}"


@pytest.fixture
def store(db, agent):
    return LedgerStore(db, agent, detail_enabled=True)


@pytest.fixture
def plain_store(db, agent):
    return LedgerStore(db, agent)


@pytest.fixture
def own_secret(monkeypatch):
    """A secret-named env var with a unique value; the lazy cache is rebuilt around it."""
    value = "zq" + uuid.uuid4().hex
    monkeypatch.setenv("NOUS_TEST_FAKE_TOKEN", value)
    ledger_store._own_secrets.cache_clear()
    yield value
    ledger_store._own_secrets.cache_clear()


async def _row(db, entry_id):
    async with db.session() as s:
        return (await s.execute(select(ExecutionLedgerEntry).where(ExecutionLedgerEntry.id == entry_id))).scalar_one()


async def _run(
    store,
    command="touch /tmp/ledger-detail-ok",
    output="made it\n[exit code: 0]",
    tool="bash",
    tool_input=None,
    status="success",
):
    entry = await store.open_entry(context=CTX, tool_name=tool, tool_input=tool_input or {"command": command}, turn=1)
    await store.close_entry(entry, status=status, result_summary=output, output_of=tool)
    return entry


async def _age(db, entry_id, days):
    async with db.session() as s:
        await s.execute(
            update(ExecutionLedgerEntry)
            .where(ExecutionLedgerEntry.id == entry_id)
            .values(created_at=datetime.now(UTC) - timedelta(days=days))
        )
        await s.commit()


# ---- the store ----


@pytest.mark.asyncio
async def test_with_the_flag_off_nothing_is_stored_and_the_summary_says_so(plain_store, db):
    """PIN: the default behaviour."""
    entry = await _run(plain_store, output="made it\n[exit code: 0]")
    row = await _row(db, entry)
    assert row.input_text is None and row.output_text is None
    assert row.result_summary.endswith("chars of output, not stored")


@pytest.mark.asyncio
async def test_with_the_flag_on_a_bash_command_and_its_output_are_stored(store, db):
    entry = await _run(store, command="touch /tmp/ledger-detail-ok", output="made it\n[exit code: 0]")
    row = await _row(db, entry)
    assert row.input_text == "touch /tmp/ledger-detail-ok"
    assert row.output_text == "made it\n[exit code: 0]"
    assert "chars of output" in row.result_summary and "not stored" not in row.result_summary
    # the hashed key_args stay as they were
    assert row.key_args["command_len"] == str(len("touch /tmp/ledger-detail-ok"))


@pytest.mark.asyncio
async def test_run_python_stores_the_code_and_other_tools_their_sorted_json(store, db):
    py = await _run(store, tool="run_python", tool_input={"code": "open('f', 'w').write('1')\n"})
    assert (await _row(db, py)).input_text == "open('f', 'w').write('1')\n"
    wf = await _run(store, tool="write_file", tool_input={"path": "/tmp/b", "content": "é", "a": 1})
    assert (await _row(db, wf)).input_text == json.dumps(
        {"path": "/tmp/b", "content": "é", "a": 1}, ensure_ascii=False, indent=2, sort_keys=True
    )


@pytest.mark.asyncio
async def test_an_own_secret_value_is_masked_in_input_and_output(store, db, own_secret):
    entry = await _run(store, command=f"echo {own_secret} > f", output=f"wrote {own_secret} ok")
    row = await _row(db, entry)
    assert own_secret not in row.input_text and own_secret not in row.output_text
    assert SECRET_MASK in row.input_text and SECRET_MASK in row.output_text


@pytest.mark.asyncio
async def test_a_labeled_password_is_masked_by_the_pattern_redaction(store, db):
    secret = "hunter" + "2" * 10
    entry = await _run(store, command=f"touch f && curl -d password={secret} x", output=f"password={secret}")
    row = await _row(db, entry)
    assert secret not in row.input_text and secret not in row.output_text


def test_a_long_text_keeps_head_and_tail_and_masks_a_secret_across_the_cut(own_secret):
    half = DETAIL_OUTPUT_CHARS // 2
    filler = "lorem ipsum dolor sit amet\n"
    lead = ("HEAD-" + filler * 400)[: half - 10]
    text = lead + own_secret + (filler * 4000) + "-TAIL"
    assert len(text) > 100_000 and lead.__len__() + 10 == half
    out = detail_text(text, DETAIL_OUTPUT_CHARS)
    assert out.startswith("HEAD-") and out.endswith("-TAIL")
    assert "chars cut]" in out and "\n…[" in out
    assert DETAIL_OUTPUT_CHARS < len(out) < DETAIL_OUTPUT_CHARS + 100
    assert own_secret not in out and own_secret[:12] not in out and own_secret[-12:] not in out
    assert "[REDACTED_" in out  # the mask itself may be cut by the head bound


@pytest.mark.asyncio
async def test_a_blocked_row_keeps_what_it_tried_to_run_and_no_output(store, db):
    await store.record_blocked(
        context=CTX, tool_name="bash", tool_input={"command": "touch /tmp/refused"}, turn=1, refused_by="action_gate"
    )
    async with db.session() as s:
        row = (
            await s.execute(select(ExecutionLedgerEntry).where(ExecutionLedgerEntry.agent_id == store._agent_id))
        ).scalar_one()
    assert row.status == "blocked" and row.input_text == "touch /tmp/refused" and row.output_text is None


@pytest.mark.asyncio
async def test_a_harness_note_close_stores_no_output(store, db):
    entry = await store.open_entry(context=CTX, tool_name="bash", tool_input={"command": "touch f"}, turn=1)
    await store.close_entry(entry, status="unknown", result_summary="cancelled mid-call")
    row = await _row(db, entry)
    assert row.output_text is None and row.result_summary == "cancelled mid-call"
    assert row.input_text == "touch f"


# ---- retention ----


@pytest.mark.asyncio
async def test_detail_retention_clears_the_texts_and_keeps_the_row(store, db):
    old = await _run(store)
    fresh = await _run(store)
    pending = await store.open_entry(context=CTX, tool_name="bash", tool_input={"command": "touch p"}, turn=1)
    await _age(db, old, 40)
    await _age(db, pending, 40)
    assert await store.prune(retention_days=90, detail_retention_days=30) == 0
    row = await _row(db, old)
    assert row.input_text is None and row.output_text is None and row.status == "success"
    assert (await _row(db, fresh)).input_text is not None
    assert (await _row(db, pending)).input_text == "touch p"


@pytest.mark.asyncio
async def test_a_tombstoned_keyed_row_loses_its_texts(store, db):
    entry = await store.open_entry(
        context=CTX,
        tool_name="send_email",
        tool_input={"to": "a@example.com", "subject": "s", "body": "b"},
        turn=1,
        idempotency_key="k-detail-1",
    )
    await store.close_entry(entry, status="success", result_summary="sent", output_of="send_email")
    assert (await _row(db, entry)).output_text == "sent"
    await _age(db, entry, 100)
    await store.prune(retention_days=90)
    row = await _row(db, entry)
    assert row.idempotency_key == "k-detail-1" and row.key_args == {}
    assert row.input_text is None and row.output_text is None


@pytest.mark.asyncio
async def test_zero_row_retention_with_a_detail_window_deletes_nothing(store, db):
    entry = await _run(store)
    await _age(db, entry, 400)
    assert await store.prune(retention_days=0, detail_retention_days=30) == 0
    row = await _row(db, entry)
    assert row.status == "success" and row.input_text is None


# ---- the dashboard ----


async def _exec(db, agent, **kw):
    async with db.session() as session:
        return await get_execution_data(session, agent, modes=MODES, **kw)


@pytest.mark.asyncio
async def test_a_list_row_carries_a_preview_and_flags_never_the_full_texts(store, db, agent):
    await _run(store, command="touch f # " + "see the log. " * 100, output="o" * 3000)
    row = (await _exec(db, agent))["rows"][0]
    assert len(row["input_preview"]) == INPUT_PREVIEW_CHARS and row["input_preview"].startswith("touch f")
    assert row["has_input"] is True and row["has_output"] is True
    assert "input_text" not in row and "output_text" not in row


@pytest.mark.asyncio
async def test_a_row_without_detail_has_no_preview(plain_store, db, agent):
    await _run(plain_store)
    row = (await _exec(db, agent))["rows"][0]
    assert row["input_preview"] is None and row["has_input"] is False and row["has_output"] is False


@pytest.mark.asyncio
async def test_the_detail_returns_both_texts_for_this_agent_only(store, db, agent):
    entry = await _run(store, command="touch f", output="the output")
    async with db.session() as session:
        got = await get_execution_detail(session, agent, str(entry))
        other = await get_execution_detail(session, agent + "-other", str(entry))
        missing = await get_execution_detail(session, agent, str(uuid.uuid4()))
        with pytest.raises(ValueError):
            await get_execution_detail(session, agent, "not-a-uuid")
    assert got == {
        "id": str(entry),
        "tool_name": "bash",
        "status": "success",
        "input_text": "touch f",
        "output_text": "the output",
    }
    assert other is None and missing is None


@pytest.mark.asyncio
async def test_search_matches_text_found_only_in_the_input_or_output(store, db, agent):
    await _run(store, command="touch /tmp/zebrafile", output="plain")
    await _run(store, command="touch /tmp/other", output="giraffe reply")
    assert [r["input_preview"] for r in (await _exec(db, agent, q="ZEBRAFILE"))["rows"]] == ["touch /tmp/zebrafile"]
    assert [r["input_preview"] for r in (await _exec(db, agent, q="giraffe"))["rows"]] == ["touch /tmp/other"]
    assert (await _exec(db, agent, q="nomatchanywhere"))["rows"] == []
