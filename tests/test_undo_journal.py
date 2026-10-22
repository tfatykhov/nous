"""Tests for the passive undo journal (nous/undo_journal.py).

Per kind -- workspace file, schedule, dynamic heartbeat check, runtime
heartbeat config -- a mutation records its before-state, a restore round-trips
it (snapshotting the current state first), and a failed snapshot never blocks
the mutation. One end-to-end test drives every kind through the REST surface
and checks the restored artifacts are byte/row identical.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import threading
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from nous import undo_journal
from nous.api.builtin_tools import write_file_tool
from nous.brain.intentions import IntentionSpec
from nous.heart.schedules import ScheduleManager
from nous.heartbeat.dynamic import DynamicCheckLoader
from nous.heartbeat.registry import CheckRegistry
from nous.storage.models import DynamicCheckModel, Schedule
from nous.undo_journal import RestoreDeps, UndoJournal

# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def workspace(tmp_path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    return ws


@pytest.fixture
def journal(workspace):
    j = UndoJournal(str(workspace / ".nous-undo"))
    undo_journal.set_journal(j)
    yield j
    undo_journal.set_journal(None)


@pytest.fixture
def failing_journal(journal, monkeypatch):
    async def _boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(journal, "record", _boom)
    return journal


async def _only(journal: UndoJournal, kind: str, action: str | None = None) -> dict:
    entries = [
        e
        for e in await journal.list(500)
        if e["kind"] == kind and (action is None or e["action"] == action) and e["source"] == "mutation"
    ]
    assert len(entries) == 1, entries
    return entries[0]


async def _row(db, model, row_id) -> dict | None:
    table = model.__table__
    async with db.session() as s:
        row = (await s.execute(select(table).where(table.c.id == row_id))).mappings().first()
    return dict(row) if row is not None else None


def _agent() -> str:
    return f"test-undo-{uuid.uuid4().hex[:8]}"


def _slow_write(journal: UndoJournal, monkeypatch) -> tuple[threading.Event, threading.Event, threading.Event]:
    """Hold journal writes in their worker thread until ``release`` is set."""
    write = journal._write
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()

    def _write(entry):
        entered.set()
        release.wait(5)
        try:
            write(entry)
        finally:
            finished.set()

    monkeypatch.setattr(journal, "_write", _write)
    return entered, release, finished


# ---------------------------------------------------------------------------
# Journal store: retention
# ---------------------------------------------------------------------------


class TestJournalStore:
    async def test_prunes_oldest_beyond_max_entries(self, tmp_path):
        j = UndoJournal(str(tmp_path / "j"), max_entries=3)
        ids = [await j.record("config", "x", "t", {"n": i}) for i in range(5)]
        kept = [e["id"] for e in await j.list(10)]
        assert kept == list(reversed(ids[2:]))

    async def test_prunes_oldest_beyond_max_bytes(self, tmp_path):
        j = UndoJournal(str(tmp_path / "j"), max_bytes=10_000)
        ids = [await j.record("config", "x", "t", {"blob": "a" * 4_000}) for _ in range(4)]
        kept = [e["id"] for e in await j.list(10)]
        assert kept == list(reversed(ids[-2:]))

    async def test_entry_over_max_bytes_is_refused_and_evicts_nothing(self, tmp_path):
        # Codex P2 on #712: one oversized entry must not prune every older snapshot.
        j = UndoJournal(str(tmp_path / "j"), max_bytes=10_000)
        ids = [await j.record("config", "x", "t", {"n": i}) for i in range(3)]
        with pytest.raises(ValueError, match="exceeds"):
            await j.record("config", "x", "t", {"blob": "a" * 20_000})
        assert [e["id"] for e in await j.list(10)] == list(reversed(ids))
        assert await undo_journal.record_safe("config", "x", "t", {"blob": "a" * 20_000}) is None

    async def test_concurrent_writes_at_the_bound_never_prune_each_other(self, tmp_path, monkeypatch):
        # Codex P2 on #712 (7c7c4d2): two writers at max_entries each pruned
        # protecting only their own file, so each could delete the other's.
        # Here the first pruner waits for the second write; unlocked, the
        # second prunes the first and the first then prunes the second.
        j = UndoJournal(str(tmp_path / "j"), max_entries=1)
        first_pruning = threading.Event()
        second_written = threading.Event()
        write_file, prune = j._write_file, j._prune

        def _write_file(name, entry, data=None):
            write_file(name, entry, data)
            if first_pruning.is_set():
                second_written.set()

        def _prune(*, keep):
            if not first_pruning.is_set():
                first_pruning.set()
                second_written.wait(timeout=0.5)
            prune(keep=keep)

        monkeypatch.setattr(j, "_write_file", _write_file)
        monkeypatch.setattr(j, "_prune", _prune)

        async def _second():
            await asyncio.to_thread(first_pruning.wait, 5)
            return await j.record("config", "x", "t", {"n": 2})

        await asyncio.gather(j.record("config", "x", "t", {"n": 1}), _second())
        assert len(await j.list(10)) == 1

    async def test_list_omits_payload_and_get_returns_it(self, tmp_path):
        j = UndoJournal(str(tmp_path / "j"))
        sid = await j.record("config", "x", "t", {"secret": 1})
        assert "before" not in (await j.list())[0]
        assert (await j.get(sid))["before"] == {"secret": 1}
        assert await j.get("../../etc/passwd") is None

    async def test_row_codec_round_trips(self):
        from datetime import UTC, datetime
        from decimal import Decimal

        row = {
            "id": uuid.uuid4(),
            "at": datetime.now(UTC),
            "n": Decimal("1.50"),
            "tools": ["a"],
            "metadata": {"k": [1, None]},
            "none": None,
        }
        assert undo_journal.decode_row(undo_journal.encode_row(row)) == row

    async def test_restore_endpoint_maps_missing_to_404_and_malformed_to_409(self, workspace, journal):
        # Review of #712: journal.get() ran outside restore()'s try, so a
        # pruned or malformed snapshot raised through the endpoint (a 500).
        from nous.api.rest import create_app
        from nous.config import Settings

        app = create_app(
            SimpleNamespace(),
            SimpleNamespace(),
            SimpleNamespace(schedules=None),
            SimpleNamespace(),
            SimpleNamespace(),
            Settings(workspace_dir=str(workspace)),
        )
        bad = uuid.uuid4().hex
        journal._root.mkdir(parents=True, exist_ok=True)
        journal._write_file(f"20260101T000000000000-{bad}.json", {}, b"not json")
        keyless = uuid.uuid4().hex
        journal._write_file(f"20260101T000000000001-{keyless}.json", {"restorable": True})
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            missing = await client.post(f"/undo/snapshots/{uuid.uuid4().hex}/restore")
            malformed = await client.post(f"/undo/snapshots/{bad}/restore")
            shapeless = await client.post(f"/undo/snapshots/{keyless}/restore")
        assert missing.status_code == 404 and missing.json()["success"] is False
        assert malformed.status_code == 409 and malformed.json()["success"] is False
        assert shapeless.status_code == 409 and "malformed" in shapeless.json()["message"]

    async def test_stale_tmp_from_a_killed_write_does_not_block_mark_restored(self, tmp_path):
        # Review of #712: a hard kill between os.open and os.replace left
        # .{name}.tmp behind, and every later rewrite of that id hit O_EXCL.
        j = UndoJournal(str(tmp_path / "j"))
        sid = await j.record("config", "x", "t", {"n": 1})
        (j.root / f".{j._find(sid).name}.tmp").write_bytes(b"partial")
        await j.mark_restored(sid, "ok")
        assert (await j.get(sid))["restore_result"] == "ok"

    def test_configure_respects_flag(self, tmp_path):
        s = SimpleNamespace(
            undo_journal_enabled=False,
            undo_journal_dir="",
            workspace_dir=str(tmp_path),
            undo_journal_max_entries=5,
            undo_journal_max_bytes=2**20,
        )
        assert undo_journal.configure(s) is None
        s.undo_journal_enabled = True
        try:
            j = undo_journal.configure(s)
            assert j is not None and j.root == tmp_path / ".nous-undo"
        finally:
            undo_journal.set_journal(None)


# ---------------------------------------------------------------------------
# Kind: workspace file (write_file)
# ---------------------------------------------------------------------------


class TestFileKind:
    async def test_write_file_records_prior_bytes(self, workspace, journal):
        (workspace / "notes.txt").write_bytes(b"old\xff bytes")
        out = await write_file_tool("notes.txt", "new", _workspace_dir=str(workspace))
        assert "is_error" not in out
        entry = await journal.get((await _only(journal, "file"))["id"])
        assert entry["restorable"] is True
        assert base64.b64decode(entry["before"]["prior_b64"]) == b"old\xff bytes"

    async def test_restore_round_trips_and_snapshots_current_first(self, workspace, journal):
        target = workspace / "notes.txt"
        target.write_bytes(b"old\xff bytes")
        await write_file_tool("notes.txt", "new", _workspace_dir=str(workspace))
        sid = (await _only(journal, "file"))["id"]

        result = await undo_journal.restore(sid, RestoreDeps(workspace_dir=str(workspace)), journal)
        assert result.success, result.message
        assert target.read_bytes() == b"old\xff bytes"
        # The state the restore replaced is itself restorable.
        pre = await journal.get(result.pre_restore_id)
        assert pre["source"] == "pre_restore" and pre["restores"] == sid
        again = await undo_journal.restore(result.pre_restore_id, RestoreDeps(workspace_dir=str(workspace)), journal)
        assert again.success and target.read_bytes() == b"new"

    async def test_restore_of_created_file_removes_it(self, workspace, journal):
        await write_file_tool("fresh.txt", "made", _workspace_dir=str(workspace))
        sid = (await _only(journal, "file"))["id"]
        assert (await undo_journal.restore(sid, RestoreDeps(workspace_dir=str(workspace)), journal)).success
        assert not (workspace / "fresh.txt").exists()

    async def test_oversized_prior_is_recorded_not_restorable(self, workspace, journal):
        (workspace / "big.bin").write_bytes(b"x" * (undo_journal.FILE_SNAPSHOT_MAX_BYTES + 1))
        await write_file_tool("big.bin", "small", _workspace_dir=str(workspace))
        entry = await _only(journal, "file")
        assert entry["restorable"] is False
        result = await undo_journal.restore(entry["id"], RestoreDeps(workspace_dir=str(workspace)), journal)
        assert not result.success and (workspace / "big.bin").read_bytes() == b"small"

    async def test_concurrent_writes_snapshot_in_turn(self, workspace, journal):
        # Codex P1 on #712: with compensation off, two writes to one path
        # must not both snapshot the same prior content.
        target = workspace / "notes.txt"
        target.write_text("A")
        outs = await asyncio.gather(
            write_file_tool("notes.txt", "B", _workspace_dir=str(workspace)),
            write_file_tool("notes.txt", "C", _workspace_dir=str(workspace)),
        )
        assert all("is_error" not in out for out in outs)
        final = target.read_text()
        first = "C" if final == "B" else "B"
        newest, older = [e for e in await journal.list(10) if e["source"] == "mutation"]
        assert base64.b64decode((await journal.get(older["id"]))["before"]["prior_b64"]) == b"A"
        result = await undo_journal.restore(newest["id"], RestoreDeps(workspace_dir=str(workspace)), journal)
        assert result.success, result.message
        assert target.read_text() == first  # the state just before the second write, not A

    async def test_pre_restore_snapshot_uses_the_per_file_cap(self, workspace, journal):
        # Codex P2 on #712: a current file over 1 MiB cannot be saved first,
        # so the restore refuses rather than journal it whole.
        target = workspace / "notes.txt"
        target.write_bytes(b"small")
        await write_file_tool("notes.txt", "next", _workspace_dir=str(workspace))
        sid = (await _only(journal, "file"))["id"]
        big = b"x" * (undo_journal.FILE_SNAPSHOT_MAX_BYTES + 1)
        target.write_bytes(big)
        result = await undo_journal.restore(sid, RestoreDeps(workspace_dir=str(workspace)), journal)
        assert not result.success and "too large" in result.message
        assert target.read_bytes() == big
        assert [e["id"] for e in await journal.list(10)] == [sid]

    async def test_tampered_snapshot_is_refused(self, workspace, journal):
        (workspace / "a.txt").write_bytes(b"before")
        await write_file_tool("a.txt", "after", _workspace_dir=str(workspace))
        entry = await journal.get((await _only(journal, "file"))["id"])
        entry["before"]["prior_b64"] = base64.b64encode(b"evil").decode()
        journal._write_file(journal._find(entry["id"]).name, entry)
        result = await undo_journal.restore(entry["id"], RestoreDeps(workspace_dir=str(workspace)), journal)
        assert not result.success and (workspace / "a.txt").read_bytes() == b"after"

    async def test_restore_outside_configured_workspace_is_refused(self, workspace, journal, tmp_path):
        outside = tmp_path / "outside.txt"
        outside.write_bytes(b"keep")
        await write_file_tool("a.txt", "x", _workspace_dir=str(workspace))
        entry = await journal.get((await _only(journal, "file"))["id"])
        # A forged entry pointing outside the workspace, its root rewritten to match.
        entry["before"].update(full_path=str(outside), workspace_root=str(tmp_path))
        journal._write_file(journal._find(entry["id"]).name, entry)
        result = await undo_journal.restore(entry["id"], RestoreDeps(workspace_dir=str(workspace)), journal)
        assert not result.success and outside.read_bytes() == b"keep"

    async def test_snapshot_failure_does_not_block_write(self, workspace, failing_journal, caplog):
        with caplog.at_level(logging.WARNING, logger="nous.undo_journal"):
            out = await write_file_tool("notes.txt", "written", _workspace_dir=str(workspace))
        assert "is_error" not in out
        assert (workspace / "notes.txt").read_text() == "written"
        assert "file snapshot" in caplog.text

    async def test_write_into_journal_is_refused(self, workspace, journal):
        out = await write_file_tool(".nous-undo/x.json", "{}", _workspace_dir=str(workspace))
        assert out.get("is_error") is True
        assert not (workspace / ".nous-undo" / "x.json").exists()


# ---------------------------------------------------------------------------
# Kind: schedule
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def schedules(db):
    return ScheduleManager(db, _agent())


@pytest.mark.postgres_only
class TestScheduleKind:
    async def test_deactivate_records_and_restore_round_trips(self, db, schedules, journal):
        sched = await schedules.create(task="Water plants", schedule_type="recurring", interval_seconds=3600)
        original = await _row(db, Schedule, sched.id)
        await schedules.deactivate(sched.id)
        entry = await _only(journal, "schedule", "deactivate")
        assert entry["target"] == str(sched.id)

        result = await undo_journal.restore(entry["id"], RestoreDeps(schedules=schedules), journal)
        assert result.success, result.message
        assert await _row(db, Schedule, sched.id) == original
        pre = await journal.get(result.pre_restore_id)
        assert undo_journal.decode_row(pre["before"])["active"] is False

    async def test_deactivate_snapshot_leaves_out_the_check_and_fix_record(self, db, schedules, journal):
        sched = await schedules.create(task="Water plants", schedule_type="recurring", interval_seconds=3600)
        record = {"state": "running", "baseline": {"bad": 7919, "scored": 7927}}
        await schedules.set_metadata(sched.id, "job_fix", record)
        await schedules.set_metadata(sched.id, "other_key", {"keep": "me"})
        await schedules.deactivate(sched.id)
        entry = await journal.get((await _only(journal, "schedule", "deactivate"))["id"])
        text = json.dumps(entry)
        assert "job_fix" not in text and "7919" not in text and "7927" not in text
        assert undo_journal.decode_row(entry["before"])["metadata"] == {
            "other_key": {"keep": "me"}
        }
        live = await schedules.get(sched.id)
        assert live.metadata_["job_fix"] == record  # the stored row is untouched

    async def test_restore_keeps_fires_committed_after_the_snapshot(self, db, schedules, journal):
        # Codex P1 on #712: a restore never rewinds fire_count, last_fired_at
        # or next_fire_at, so the same run cannot fire twice.
        sched = await schedules.create(task="Water plants", schedule_type="recurring", interval_seconds=3600)
        await schedules.deactivate(sched.id)
        entry = await _only(journal, "schedule", "deactivate")
        fired_at = datetime.now(UTC)
        await schedules.advance(sched.id, fired_at)  # a fire that committed after the snapshot
        fired = await _row(db, Schedule, sched.id)

        result = await undo_journal.restore(entry["id"], RestoreDeps(schedules=schedules), journal)
        assert result.success, result.message
        row = await _row(db, Schedule, sched.id)
        assert row["active"] is True
        assert row["fire_count"] == 1 and row["last_fired_at"] == fired_at
        assert row["next_fire_at"] == fired["next_fire_at"] == fired_at + timedelta(seconds=3600)

    async def test_deactivate_snapshot_holds_the_row_lock(self, db, schedules, journal, monkeypatch):
        # Codex P1 on #712: a fire cannot commit between the snapshot and the
        # deactivate -- the snapshot is read under the deactivate's row lock.
        sched = await schedules.create(task="t", schedule_type="recurring", interval_seconds=3600)
        record_safe = undo_journal.record_safe
        fire: list[asyncio.Task] = []

        async def _record_during_fire(*args, **kwargs):
            fire.append(asyncio.create_task(schedules.advance(sched.id, datetime.now(UTC))))
            await asyncio.sleep(0.5)
            assert not fire[0].done()  # blocked on the row lock
            return await record_safe(*args, **kwargs)

        monkeypatch.setattr(undo_journal, "record_safe", _record_during_fire)
        await schedules.deactivate(sched.id)
        await fire[0]
        entry = await journal.get((await _only(journal, "schedule", "deactivate"))["id"])
        assert undo_journal.decode_row(entry["before"])["fire_count"] == 0
        assert (await _row(db, Schedule, sched.id))["fire_count"] == 1

    async def test_restore_of_create_deactivates_never_deletes(self, db, schedules, journal):
        sched = await schedules.create(task="t", schedule_type="recurring", interval_seconds=3600)
        entry = await _only(journal, "schedule", "create")
        result = await undo_journal.restore(entry["id"], RestoreDeps(schedules=schedules), journal)
        assert result.success, result.message
        row = await _row(db, Schedule, sched.id)
        assert row is not None and row["active"] is False

    async def test_deactivate_that_does_not_commit_discards_its_snapshot(self, db, schedules, journal, monkeypatch):
        # Review of #712: the deactivate snapshot is recorded before the
        # commit; a failed commit left it behind for a later restore.
        sched = await schedules.create(task="t", schedule_type="recurring", interval_seconds=3600)

        async def _fail(self):
            raise RuntimeError("commit failed")

        monkeypatch.setattr(AsyncSession, "commit", _fail)
        with pytest.raises(RuntimeError, match="commit failed"):
            await schedules.deactivate(sched.id)
        monkeypatch.undo()
        assert [e["action"] for e in await journal.list(500)] == ["create"]
        assert (await _row(db, Schedule, sched.id))["active"] is True

    async def test_deactivate_cancelled_mid_snapshot_write_leaves_no_orphan(self, db, schedules, journal, monkeypatch):
        # Review of PR B: the deactivate's journal-minted id was lost to a
        # cancel during the write, which still landed afterwards.
        sched = await schedules.create(task="t", schedule_type="recurring", interval_seconds=3600)
        entered, release, finished = _slow_write(journal, monkeypatch)
        task = asyncio.create_task(schedules.deactivate(sched.id))
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        release.set()
        assert await asyncio.to_thread(finished.wait, 5)
        assert [e["action"] for e in await journal.list(500)] == ["create"]
        assert not journal._discarded
        assert (await _row(db, Schedule, sched.id))["active"] is True

    async def test_refused_restore_records_no_pre_restore(self, db, schedules, journal):
        # Review of PR B: the owner/id refusal ran after on_current.
        sched = await schedules.create(task="t", schedule_type="recurring", interval_seconds=3600)
        row = await _row(db, Schedule, sched.id)
        row["agent_id"] = "someone-else"
        sid = await journal.record("schedule", "deactivate", str(sched.id), undo_journal.encode_row(row))
        result = await undo_journal.restore(sid, RestoreDeps(schedules=schedules), journal)
        assert not result.success and result.pre_restore_id is None
        assert not [e for e in await journal.list(500) if e["source"] == "pre_restore"]

    async def test_noop_restore_of_create_records_no_pre_restore(self, db, schedules, journal):
        # Review of #712: a restore that changes nothing still recorded a pre_restore entry.
        sched = await schedules.create(task="t", schedule_type="recurring", interval_seconds=3600)
        await schedules.deactivate(sched.id)
        entry = await _only(journal, "schedule", "create")
        result = await undo_journal.restore(entry["id"], RestoreDeps(schedules=schedules), journal)
        assert result.success and result.pre_restore_id is None
        assert not [e for e in await journal.list(500) if e["source"] == "pre_restore"]

    async def test_container_backed_deactivate_is_not_restorable(self, db, schedules, journal):
        spec = IntentionSpec(intent="Check daily", origin_kind="interactive", container=True)
        sched = await schedules.create(task="Check", schedule_type="recurring", interval_seconds=1800, intention=spec)
        await schedules.deactivate(sched.id)  # closes the container
        entry = await _only(journal, "schedule", "deactivate")
        assert entry["restorable"] is False and "container" in entry["note"]
        result = await undo_journal.restore(entry["id"], RestoreDeps(schedules=schedules), journal)
        assert not result.success
        assert (await _row(db, Schedule, sched.id))["active"] is False

    async def test_restore_never_rearms_against_a_closed_container(self, db, schedules, journal):
        spec = IntentionSpec(intent="Check daily", origin_kind="interactive", container=True)
        sched = await schedules.create(task="Check", schedule_type="recurring", interval_seconds=1800, intention=spec)
        active_row = undo_journal.encode_row(await _row(db, Schedule, sched.id))
        await schedules.deactivate(sched.id)
        # e.g. a snapshot taken before intentions were on, restored after
        sid = await journal.record("schedule", "deactivate", str(sched.id), active_row)
        result = await undo_journal.restore(sid, RestoreDeps(schedules=schedules), journal)
        assert result.success and "inactive" in result.message
        assert (await _row(db, Schedule, sched.id))["active"] is False

    async def test_snapshot_failure_does_not_block(self, db, schedules, failing_journal, caplog):
        with caplog.at_level(logging.WARNING, logger="nous.undo_journal"):
            sched = await schedules.create(task="t", schedule_type="recurring", interval_seconds=3600)
            await schedules.deactivate(sched.id)
        assert (await _row(db, Schedule, sched.id))["active"] is False
        assert "schedule snapshot" in caplog.text


# ---------------------------------------------------------------------------
# Kind: dynamic heartbeat check
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def loader(db):
    return DynamicCheckLoader(db=db, registry=CheckRegistry(), agent_id=_agent())


async def _make_check(loader: DynamicCheckLoader) -> uuid.UUID:
    created = await loader.create_check(
        name=f"watch-{uuid.uuid4().hex[:6]}", description="d", prompt="look", interval_seconds=600
    )
    return uuid.UUID(created["id"])


@pytest.mark.postgres_only
class TestCheckKind:
    @pytest.mark.parametrize(
        ("action", "updates"),
        [("update", {"interval_seconds": 7200, "prompt": "changed"}), ("disable", None), ("delete", None)],
    )
    async def test_action_records_and_restore_round_trips(self, db, loader, journal, action, updates):
        check_id = await _make_check(loader)
        original = await _row(db, DynamicCheckModel, check_id)
        await loader.manage_check(action, original["name"], updates)
        entry = await _only(journal, "heartbeat_check", action)

        result = await undo_journal.restore(entry["id"], RestoreDeps(check_loader=loader), journal)
        assert result.success, result.message
        assert await _row(db, DynamicCheckModel, check_id) == original
        live = loader._registry.get_check(original["name"])
        assert live is not None and live.interval == 600 and live._prompt == "look"

    async def test_restore_keeps_runs_completed_after_the_snapshot(self, db, loader, journal):
        # Codex P1 on #712: a restore never rewinds run_count, error_count,
        # last_run_at or last_error, which status and self-tuning read.
        check_id = await _make_check(loader)
        name = (await _row(db, DynamicCheckModel, check_id))["name"]
        await loader.manage_check("update", name, {"prompt": "p2"})
        entry = await _only(journal, "heartbeat_check", "update")
        await loader.update_run_stats(str(check_id), success=True)  # runs that completed after the snapshot
        await loader.update_run_stats(str(check_id), success=False, error_msg="boom")
        ran = await _row(db, DynamicCheckModel, check_id)
        assert ran["last_run_at"] is not None

        result = await undo_journal.restore(entry["id"], RestoreDeps(check_loader=loader), journal)
        assert result.success, result.message
        row = await _row(db, DynamicCheckModel, check_id)
        assert row["prompt"] == "look"
        assert (row["run_count"], row["error_count"], row["last_error"]) == (2, 1, "boom")
        assert row["last_run_at"] == ran["last_run_at"]

    async def test_restore_of_create_disables_never_deletes(self, db, loader, journal):
        check_id = await _make_check(loader)
        entry = await _only(journal, "heartbeat_check", "create")
        result = await undo_journal.restore(entry["id"], RestoreDeps(check_loader=loader), journal)
        assert result.success, result.message
        row = await _row(db, DynamicCheckModel, check_id)
        assert row is not None and row["enabled"] is False
        assert loader._registry.get_check(row["name"]) is None

    async def test_noop_restore_of_create_records_no_pre_restore(self, db, loader, journal):
        # Review of #712: a restore that changes nothing still recorded a pre_restore entry.
        check_id = await _make_check(loader)
        await loader.manage_check("disable", (await _row(db, DynamicCheckModel, check_id))["name"])
        entry = await _only(journal, "heartbeat_check", "create")
        result = await undo_journal.restore(entry["id"], RestoreDeps(check_loader=loader), journal)
        assert result.success and result.pre_restore_id is None
        assert not [e for e in await journal.list(500) if e["source"] == "pre_restore"]

    async def test_name_clash_refuses(self, db, loader, journal):
        check_id = await _make_check(loader)
        name = (await _row(db, DynamicCheckModel, check_id))["name"]
        await loader.manage_check("delete", name)
        await loader.create_check(name=name, description="d", prompt="other", interval_seconds=600)
        entry = await _only(journal, "heartbeat_check", "delete")
        result = await undo_journal.restore(entry["id"], RestoreDeps(check_loader=loader), journal)
        assert not result.success and "nothing changed" in result.message
        # Review of PR B: a refused restore records no pre_restore entry.
        assert result.pre_restore_id is None
        assert not [e for e in await journal.list(500) if e["source"] == "pre_restore"]

    async def test_dag_check_restore_is_refused(self, db, loader, journal):
        created = await loader.create_check(name=f"dag-{uuid.uuid4().hex[:6]}", description="d", prompt="p")
        await loader.manage_check("disable", created["name"])
        entry = await _only(journal, "heartbeat_check", "disable")
        result = await undo_journal.restore(entry["id"], RestoreDeps(check_loader=loader), journal)
        assert not result.success and "DAG node" in result.message
        assert (await _row(db, DynamicCheckModel, uuid.UUID(created["id"])))["enabled"] is False

    async def test_restored_row_gets_the_tool_filter(self, db, loader, journal):
        check_id = await _make_check(loader)
        name = (await _row(db, DynamicCheckModel, check_id))["name"]
        await loader.manage_check("update", name, {"prompt": "p2"})
        entry = await journal.get((await _only(journal, "heartbeat_check", "update"))["id"])
        entry["before"]["tools"] = ["web_search", "send_email"]  # forged
        journal._write_file(journal._find(entry["id"]).name, entry)
        assert (await undo_journal.restore(entry["id"], RestoreDeps(check_loader=loader), journal)).success
        assert (await _row(db, DynamicCheckModel, check_id))["tools"] == ["web_search"]

    @pytest.mark.parametrize(
        "updates",
        [
            {"interval_seconds": 60},
            {"on_complete_tools": ["web_search"]},
            {"timeout_seconds": "slow"},
            {"cron_expr": None, "prompt": "p2"},
        ],
    )
    async def test_rejected_update_records_nothing(self, db, loader, journal, updates):
        # Codex P2 on #712 (7c7c4d2): an update that fails validation was
        # snapshotted before the error, leaving a journal entry for a change
        # that never happened.
        created = await loader.create_check(
            name=f"watch-{uuid.uuid4().hex[:6]}", description="d", prompt="look", interval_seconds=600, tools=[]
        )
        if "cron_expr" in updates:  # an interval below the minimum, set behind validation's back
            async with db.session() as s:
                row = await s.get(DynamicCheckModel, uuid.UUID(created["id"]))
                row.interval_seconds = 60
                await s.commit()
        before = await _row(db, DynamicCheckModel, uuid.UUID(created["id"]))
        with pytest.raises(ValueError):
            await loader.manage_check("update", created["name"], updates)
        assert [e["action"] for e in await journal.list(500)] == ["create"]
        assert await _row(db, DynamicCheckModel, uuid.UUID(created["id"])) == before

    async def test_update_with_no_allowed_field_records_and_commits_nothing(self, db, loader, journal):
        # Review of #712 (residual of #714 item 2): an update whose keys are
        # all outside allowed_fields still journaled a snapshot and committed.
        check_id = await _make_check(loader)
        before = await _row(db, DynamicCheckModel, check_id)
        result = await loader.manage_check("update", before["name"], {"bogus": 1, "name": "renamed"})
        assert result == {"status": "updated", "name": before["name"]}
        assert [e["action"] for e in await journal.list(500)] == ["create"]
        assert await _row(db, DynamicCheckModel, check_id) == before

    async def test_refused_disable_records_nothing(self, db, loader, journal, monkeypatch):
        check_id = await _make_check(loader)
        name = (await _row(db, DynamicCheckModel, check_id))["name"]
        monkeypatch.setattr(loader, "_has_cancellable_run", lambda _name: True)
        with pytest.raises(ValueError, match="active run"):
            await loader.manage_check("disable", name, capture={"refuse_if_running": True})
        assert [e["action"] for e in await journal.list(500)] == ["create"]
        assert (await _row(db, DynamicCheckModel, check_id))["enabled"] is True

    async def test_run_starting_during_the_disable_snapshot_leaves_no_orphan(self, db, loader, journal, monkeypatch):
        # Codex P2 on #715: a run starting while the disable's snapshot was
        # being written got the disable refused after the snapshot existed;
        # a later undo could apply it over newer settings.
        check_id = await _make_check(loader)
        name = (await _row(db, DynamicCheckModel, check_id))["name"]
        check = loader._registry.get_check(name)
        check._runner = object()
        check._active_runs = loader._active_runs
        turn_started = asyncio.Event()

        async def _turn(session_id, instruction):
            turn_started.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(check, "_run_turn", _turn)
        record = journal.record
        runs = []

        async def _record_while_a_run_starts(*args, **kwargs):
            runs.append(asyncio.create_task(check.run()))
            await asyncio.sleep(0)
            return await record(*args, **kwargs)

        monkeypatch.setattr(journal, "record", _record_while_a_run_starts)
        capture = {"refuse_if_running": True}
        try:
            result = await loader.manage_check("disable", name, capture=capture)
        finally:
            for task in runs:
                task.cancel()
        # The disable proceeds: its gate kept the run from starting.
        assert result["status"] == "disabled"
        assert (await runs[0]).skipped and not turn_started.is_set()
        assert (await _row(db, DynamicCheckModel, check_id))["enabled"] is False
        entry = await _only(journal, "heartbeat_check", "disable")
        assert (await journal.get(entry["id"]))["before"]["enabled"] is True

    async def test_disable_that_does_not_commit_discards_its_snapshot(self, db, loader, journal):
        check_id = await _make_check(loader)
        name = (await _row(db, DynamicCheckModel, check_id))["name"]

        async def _persist(session, capture):
            raise RuntimeError("compensation write failed")

        with pytest.raises(RuntimeError):
            await loader.manage_check("disable", name, capture={"refuse_if_running": True, "persist": _persist})
        assert [e["action"] for e in await journal.list(500)] == ["create"]
        assert (await _row(db, DynamicCheckModel, check_id))["enabled"] is True
        assert not loader._registry.get_check(name)._self_disabled

    @pytest.mark.parametrize(("action", "updates"), [("enable", None), ("delete", None), ("update", {"prompt": "p2"})])
    async def test_mutation_that_does_not_commit_discards_its_snapshot(
        self, db, loader, journal, monkeypatch, action, updates
    ):
        # Review of #712: enable, delete and update snapshot before the
        # commit, so a failed commit left a snapshot that, restored later,
        # silently overwrote newer edits.
        check_id = await _make_check(loader)
        before = await _row(db, DynamicCheckModel, check_id)

        async def _fail(self):
            raise RuntimeError("commit failed")

        monkeypatch.setattr(AsyncSession, "commit", _fail)
        with pytest.raises(RuntimeError, match="commit failed"):
            await loader.manage_check(action, before["name"], updates)
        monkeypatch.undo()
        assert [e["action"] for e in await journal.list(500)] == ["create"]
        assert await _row(db, DynamicCheckModel, check_id) == before

    @pytest.mark.parametrize(("action", "updates"), [("enable", None), ("delete", None), ("update", {"prompt": "p2"})])
    async def test_mutation_cancelled_mid_snapshot_write_leaves_no_orphan(
        self, db, loader, journal, monkeypatch, action, updates
    ):
        # Review of PR B: cancelled while record()'s thread write was in
        # flight, the mutation never learned the journal-minted id, so the
        # write that landed afterwards survived; restoring it later reverted
        # a newer committed edit.
        check_id = await _make_check(loader)
        before = await _row(db, DynamicCheckModel, check_id)
        entered, release, finished = _slow_write(journal, monkeypatch)
        task = asyncio.create_task(loader.manage_check(action, before["name"], updates))
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        release.set()
        assert await asyncio.to_thread(finished.wait, 5)
        assert [e["action"] for e in await journal.list(500)] == ["create"]
        assert not journal._discarded
        assert await _row(db, DynamicCheckModel, check_id) == before

    async def test_disable_cancelled_mid_snapshot_write_leaves_no_orphan(self, db, loader, journal, monkeypatch):
        # Codex P2 on #717: cancelled while record()'s thread write was in
        # flight, the disable never learned the snapshot id, so the write that
        # landed afterwards survived as a snapshot of a disable that never
        # committed.
        check_id = await _make_check(loader)
        name = (await _row(db, DynamicCheckModel, check_id))["name"]
        write = journal._write
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()

        def _slow_write(entry):
            entered.set()
            release.wait(5)
            try:
                write(entry)
            finally:
                finished.set()

        monkeypatch.setattr(journal, "_write", _slow_write)
        task = asyncio.create_task(loader.manage_check("disable", name, capture={"refuse_if_running": True}))
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        release.set()
        assert await asyncio.to_thread(finished.wait, 5)
        # No disable snapshot: undo has nothing to apply.
        assert [e["action"] for e in await journal.list(500)] == ["create"]
        assert not journal._discarded
        assert (await _row(db, DynamicCheckModel, check_id))["enabled"] is True

    async def test_discard_does_not_block_the_event_loop(self, db, loader, journal):
        # #718 (1): discard took the journal's threading lock on the event
        # loop, while _write holds that lock across fsync. Here the lock is
        # released only by a loop task: on the loop, the discard waits out
        # the holder's 5 s timeout and the task never runs before it.
        check_id = await _make_check(loader)
        name = (await _row(db, DynamicCheckModel, check_id))["name"]
        held, release = threading.Event(), threading.Event()
        ticked: list[bool] = []
        tasks: list[asyncio.Task] = []

        def _hold():
            with journal._lock:
                held.set()
                release.wait(5)

        async def _tick():
            await asyncio.sleep(0.05)
            ticked.append(True)
            release.set()

        async def _persist(session, capture):
            threading.Thread(target=_hold, daemon=True).start()
            assert await asyncio.to_thread(held.wait, 5)
            tasks.append(asyncio.create_task(_tick()))
            raise RuntimeError("compensation write failed")

        with pytest.raises(RuntimeError):
            await loader.manage_check("disable", name, capture={"refuse_if_running": True, "persist": _persist})
        assert ticked == [True]
        assert [e["action"] for e in await journal.list(500)] == ["create"]
        await tasks[0]

    async def test_failed_snapshot_write_then_failed_disable_leaves_no_tombstone(
        self, db, loader, journal, monkeypatch
    ):
        # #718 (2): the snapshot write failed (nothing landed), the disable
        # then failed, and its discard tombstoned an id no write would ever
        # consume, so _discarded grew without bound during an outage.
        check_id = await _make_check(loader)
        name = (await _row(db, DynamicCheckModel, check_id))["name"]

        def _disk_full(*args, **kwargs):
            raise OSError("disk full")

        async def _persist(session, capture):
            raise RuntimeError("compensation write failed")

        monkeypatch.setattr(journal, "_write_file", _disk_full)
        with pytest.raises(RuntimeError):
            await loader.manage_check("disable", name, capture={"refuse_if_running": True, "persist": _persist})
        assert not journal._discarded

    async def test_snapshot_failure_does_not_block(self, db, loader, failing_journal, caplog):
        with caplog.at_level(logging.WARNING, logger="nous.undo_journal"):
            check_id = await _make_check(loader)
            name = (await _row(db, DynamicCheckModel, check_id))["name"]
            await loader.manage_check("update", name, {"prompt": "p2"})
        assert (await _row(db, DynamicCheckModel, check_id))["prompt"] == "p2"
        assert "heartbeat_check snapshot" in caplog.text


# ---------------------------------------------------------------------------
# Kind: runtime config (restore logic; the REST hook is covered end to end)
# ---------------------------------------------------------------------------


class TestConfigKind:
    async def test_restore_applies_before_and_snapshots_current(self, journal):
        live = {"tick_interval": 99}
        sid = await journal.record("config", "heartbeat_config", "heartbeat_config", {"tick_interval": 30})
        deps = RestoreDeps(
            read_config=lambda fields: {f: live[f] for f in fields},
            apply_config=lambda body: [k for k in body if live.__setitem__(k, body[k]) is None],
        )
        result = await undo_journal.restore(sid, deps, journal)
        assert result.success and live == {"tick_interval": 30}
        assert (await journal.get(result.pre_restore_id))["before"] == {"tick_interval": 99}

    async def test_failed_pre_restore_snapshot_changes_nothing(self, journal, monkeypatch):
        live = {"tick_interval": 99}
        sid = await journal.record("config", "heartbeat_config", "heartbeat_config", {"tick_interval": 30})

        async def _boom(*args, **kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(journal, "record", _boom)
        deps = RestoreDeps(read_config=lambda fields: dict(live), apply_config=lambda body: live.update(body) or [])
        result = await undo_journal.restore(sid, deps, journal)
        assert not result.success and live == {"tick_interval": 99}


    async def test_restore_waits_for_the_config_lock(self, journal):
        # Codex P2 on #712 (#713 item 1): a restore takes the same lock as
        # PUT /heartbeat/config around read + record + apply.
        live = {"tick_interval": 99}
        sid = await journal.record("config", "heartbeat_config", "heartbeat_config", {"tick_interval": 30})
        lock = asyncio.Lock()
        deps = RestoreDeps(
            read_config=lambda fields: {f: live[f] for f in fields},
            apply_config=lambda body: [k for k in body if live.__setitem__(k, body[k]) is None],
            config_lock=lock,
        )
        async with lock:
            task = asyncio.create_task(undo_journal.restore(sid, deps, journal))
            await asyncio.sleep(0.05)
            assert not task.done() and live == {"tick_interval": 99}
            live["tick_interval"] = 55  # a writer holding the lock changes it
        result = await task
        assert result.success and live == {"tick_interval": 30}
        assert (await journal.get(result.pre_restore_id))["before"] == {"tick_interval": 55}

    async def test_concurrent_config_puts_snapshot_each_prior_value(self, journal, workspace, monkeypatch):
        # Codex P2 on #712 (#713 item 1): two concurrent PUTs both read the
        # same prior value while the journal write yielded, so the later
        # snapshot skipped the state just before it.
        from nous.api.rest import create_app
        from nous.config import Settings

        settings = Settings(workspace_dir=str(workspace))
        heartbeat_runner = SimpleNamespace(registry=CheckRegistry())
        app = create_app(
            SimpleNamespace(),
            SimpleNamespace(),
            SimpleNamespace(),
            SimpleNamespace(),
            SimpleNamespace(),
            settings,
            heartbeat_runner=heartbeat_runner,
        )
        record = journal.record

        async def _slow_record(*args, **kwargs):
            await asyncio.sleep(0.05)
            return await record(*args, **kwargs)

        monkeypatch.setattr(journal, "record", _slow_record)
        original = settings.heartbeat_tick_interval
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            first = asyncio.create_task(client.put("/heartbeat/config", json={"tick_interval": original + 1}))
            await asyncio.sleep(0.01)
            second = asyncio.create_task(client.put("/heartbeat/config", json={"tick_interval": original + 2}))
            assert (await first).status_code == 200 and (await second).status_code == 200
        befores = [(await journal.get(e["id"]))["before"]["tick_interval"] for e in reversed(await journal.list(10))]
        assert befores == [original, original + 1]
        assert settings.heartbeat_tick_interval == original + 2


# ---------------------------------------------------------------------------
# End to end: every kind mutated through Nous, restored over REST
# ---------------------------------------------------------------------------


@pytest.mark.postgres_only
async def test_end_to_end_restore_every_kind_identically(db, heart, workspace, journal):
    from nous.api.rest import create_app
    from nous.config import Settings

    settings = Settings(workspace_dir=str(workspace))
    loader = DynamicCheckLoader(db=db, registry=CheckRegistry(), agent_id=settings.agent_id)
    heartbeat_runner = SimpleNamespace(registry=loader._registry, dynamic_loader=loader)
    app = create_app(
        SimpleNamespace(), SimpleNamespace(), heart, SimpleNamespace(), db, settings, heartbeat_runner=heartbeat_runner
    )

    async def restore(client: AsyncClient, kind: str, action: str) -> None:
        sid = (await _only(journal, kind, action))["id"]
        resp = await client.post(f"/undo/snapshots/{sid}/restore")
        assert resp.status_code == 200, resp.json()
        assert resp.json()["pre_restore_id"]

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # file
        target = workspace / "report.md"
        target.write_bytes(b"# v1\n\x00\xfe binary tail")
        await write_file_tool("report.md", "# v2", _workspace_dir=str(workspace))
        assert target.read_bytes() == b"# v2"
        await restore(client, "file", "write_file")
        assert target.read_bytes() == b"# v1\n\x00\xfe binary tail"

        # schedule (cancelled over REST)
        sched = await heart.schedules.create(task="e2e", schedule_type="recurring", interval_seconds=3600)
        sched_row = await _row(db, Schedule, sched.id)
        assert (await client.delete(f"/schedules/{sched.id}")).status_code == 200
        await restore(client, "schedule", "deactivate")
        assert await _row(db, Schedule, sched.id) == sched_row

        # heartbeat check (updated over REST)
        name = f"e2e-{uuid.uuid4().hex[:6]}"
        created = await loader.create_check(name=name, description="d", prompt="p", interval_seconds=900)
        check_row = await _row(db, DynamicCheckModel, uuid.UUID(created["id"]))
        resp = await client.patch(f"/heartbeat/checks/dynamic/{name}", json={"interval_seconds": 4000})
        assert resp.status_code == 200
        await restore(client, "heartbeat_check", "update")
        assert await _row(db, DynamicCheckModel, uuid.UUID(created["id"])) == check_row

        # runtime config
        before = settings.heartbeat_tick_interval
        resp = await client.put("/heartbeat/config", json={"tick_interval": before + 17})
        assert resp.json()["fields"] == ["tick_interval"] and settings.heartbeat_tick_interval == before + 17
        await restore(client, "config", "heartbeat_config")
        assert settings.heartbeat_tick_interval == before

        listed = (await client.get("/undo/snapshots")).json()["snapshots"]
        assert {e["kind"] for e in listed} == {"file", "schedule", "heartbeat_check", "config"}
        assert all("before" not in e for e in listed)
