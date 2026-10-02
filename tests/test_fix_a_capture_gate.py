"""Post-merge review of #652: a compensation snapshot is stored only when something can revert from it.

On 236c110 every compensable background call was snapshotted as soon as
compensation was enabled -- for write_file, the previous file content, into
Postgres, for the ledger retention window -- even with auto-review off, where
the harness publishes no review card to offer Revert on. And with
compensation off the same code path logged a WARNING per call.
"""

from __future__ import annotations

import base64
import logging
import uuid
from types import SimpleNamespace

import pytest
from test_runner_authorization import _one_tool_call_then_done_with, _run_loop
from test_runner_ledger import _FakeSnapStore, _FakeStore
from test_runner_ledger import _runner as _ledger_runner

from nous.api.call_outcome import CallOutcome
from nous.api.compensation import SnapshotStore, release_write_path_lock, write_path_lock
from nous.api.execution_context import ExecutionContext
from nous.api.runner import AgentRunner


async def _pusher(tool_name, entry_id, session_id):
    return "surface-1"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "undoable", "auto_review", "stored"),
    [
        pytest.param("subtask", False, False, False, id="background-no-card"),
        pytest.param("subtask", False, True, True, id="background-card"),
        pytest.param("dag_node", True, False, True, id="undoable"),
    ],
)
async def test_prior_bytes_are_stored_only_when_something_can_revert_from_them(
    db, tmp_path, kind, undoable, auto_review, stored
):
    """The production capture path against the real snapshot table: with no
    undoable promise and no review card to offer Revert on, the file's
    previous content never reaches the database."""
    agent = f"fix-a-{uuid.uuid4().hex[:8]}"
    store = SnapshotStore(db, agent)
    (tmp_path / "secrets.env").write_text("TOKEN=old", encoding="utf-8")
    runner = object.__new__(AgentRunner)
    runner._settings = SimpleNamespace(compensation_auto_review_enabled=auto_review)
    runner._action_review_pusher = _pusher if auto_review else None
    runner._snap_store = store
    runner._workspace_dir = str(tmp_path)
    runner._dispatcher = SimpleNamespace()
    entry = uuid.uuid4()
    lock = write_path_lock("secrets.env", str(tmp_path))
    await lock.acquire()
    try:
        captured = await runner._capture_compensation_snapshot(
            ExecutionContext(kind=kind, undoable=undoable),
            "write_file",
            {"path": "secrets.env", "content": "TOKEN=new"},
            entry,
            outcome=CallOutcome(write_lock=lock),
        )
    finally:
        release_write_path_lock(lock)

    row = await store.get_by_ledger_entry(entry)
    assert captured is stored
    assert (row is not None) is stored
    if stored:
        assert base64.b64decode(row.snapshot_data["prior_b64"]) == b"TOKEN=old"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("background", "auto_review", "pusher_wired", "expected"),
    [
        pytest.param(True, True, True, 1, id="card-publishable"),
        pytest.param(True, False, True, 0, id="auto-review-off"),
        pytest.param(True, True, False, 0, id="no-pusher"),
        pytest.param(False, True, True, 0, id="foreground"),
    ],
)
async def test_no_snapshot_without_a_card_and_no_card_without_a_snapshot(
    tmp_path, background, auto_review, pusher_wired, expected
):
    """End to end through _tool_loop: the capture and the card publication
    read one predicate, so a call nothing can revert leaves neither."""
    store = _FakeStore()
    r, _ = _ledger_runner(store, compensation_enabled=True, compensation_auto_review_enabled=auto_review)
    snaps = _FakeSnapStore()
    r.set_snapshot_store(snaps, str(tmp_path))
    pushed: list[tuple] = []

    async def pusher(tool_name, entry_id, session_id):
        pushed.append((tool_name, entry_id))

    if pusher_wired:
        r.set_action_review_pusher(pusher)
    r._call_api = _one_tool_call_then_done_with("write_file", {"path": "x.txt", "content": "hi"})

    kind = "subtask" if background else "interactive"
    await _run_loop(r, is_background=background, context=ExecutionContext(kind=kind, session_id="s1"))

    assert [c[1] for c in snaps.captured] == ["write_file"] * expected
    assert pushed == [("write_file", "id-write_file")] * expected
    assert ("dispatch", "write_file") in store.events  # the call itself always runs


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool", "tool_input"),
    [
        ("write_file", {"path": "plain.txt", "content": "x"}),
        ("resolve_decision", {"decision_id": "7b0c7b1e-3f0e-4c57-9d4e-0d4f8f0f6a11", "outcome": "noise"}),
        ("heartbeat_check_manage", {"name": "c", "action": "disable"}),
    ],
)
async def test_flags_off_background_compensable_call_logs_no_warning(caplog, tool, tool_input):
    """With compensation off there is nothing to be unable to revert: 236c110
    logged '<tool> not revertible ...: compensation is not wired' at WARNING
    for every compensable background call."""
    store = _FakeStore()
    r, _ = _ledger_runner(store, offered=(tool,))
    r._call_api = _one_tool_call_then_done_with(tool, tool_input)

    with caplog.at_level(logging.WARNING, logger="nous.api.runner"):
        await _run_loop(r, is_background=True, context=ExecutionContext(kind="subtask", session_id="s1"))

    assert ("dispatch", tool) in store.events
    assert [rec.getMessage() for rec in caplog.records if "not revertible" in rec.getMessage()] == []
