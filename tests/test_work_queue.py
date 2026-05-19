"""Tests for F064.6 — work-queue ingress.

Covers plan §9.5 acceptance criteria plus the cross-cutting fixes from
the 3-agent plan review (atomic claim, reconciler for partial-commit
orphans, terminal-state cancel ordering).
"""

from __future__ import annotations

import json
import tempfile
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

from nous.config import Settings
from nous.dag.orchestrator import DAGOrchestrator
from nous.dag.store import DAGStore
from nous.heart.work_queue import WorkQueueItemManager
from nous.heartbeat.work_queue import (
    FileJsonlAdapter,
    GithubIssuesAdapter,
    LinearAdapter,
    WorkItem,
    WorkQueueCheck,
    build_adapter,
)


# ---------------------------------------------------------------------------
# FileJsonlAdapter
# ---------------------------------------------------------------------------


class TestFileJsonlAdapter:
    @pytest.mark.asyncio
    async def test_parses_basic_jsonl_lines(self, tmp_path: Path):
        path = tmp_path / "queue.jsonl"
        path.write_text(
            "\n".join([
                json.dumps({"external_id": "a1", "title": "First", "body": "task A"}),
                json.dumps({"external_id": "a2", "title": "Second", "body": "task B"}),
            ])
        )
        items = await FileJsonlAdapter(str(path)).list_active()
        assert len(items) == 2
        assert items[0].external_id == "a1"
        assert items[1].external_id == "a2"

    @pytest.mark.asyncio
    async def test_missing_path_returns_empty_list_not_failure(self, tmp_path: Path):
        """Plan §9.6: missing source file is a no-op, not an error."""
        missing = tmp_path / "does-not-exist.jsonl"
        items = await FileJsonlAdapter(str(missing)).list_active()
        assert items == []

    @pytest.mark.asyncio
    async def test_skips_invalid_jsonl_lines(self, tmp_path: Path):
        path = tmp_path / "queue.jsonl"
        path.write_text(
            "\n".join([
                json.dumps({"external_id": "a1", "title": "ok", "body": "body"}),
                "not valid json",
                "",  # blank
                "# comment",  # skipped
                json.dumps({"external_id": "a2", "body": "more"}),
            ])
        )
        items = await FileJsonlAdapter(str(path)).list_active()
        # Only a1 and a2; broken/blank/comment lines skipped.
        assert {i.external_id for i in items} == {"a1", "a2"}

    @pytest.mark.asyncio
    async def test_derives_external_id_from_body_when_missing(self, tmp_path: Path):
        path = tmp_path / "queue.jsonl"
        path.write_text(json.dumps({"body": "body without id"}))
        items = await FileJsonlAdapter(str(path)).list_active()
        assert len(items) == 1
        # Stable 16-char sha256 prefix
        assert len(items[0].external_id) == 16

    @pytest.mark.asyncio
    async def test_terminal_flag_propagates(self, tmp_path: Path):
        path = tmp_path / "queue.jsonl"
        path.write_text(json.dumps({"external_id": "a1", "terminal": True}))
        items = await FileJsonlAdapter(str(path)).list_active()
        assert items[0].terminal is True


# ---------------------------------------------------------------------------
# Adapter factory + v2 stubs
# ---------------------------------------------------------------------------


class TestAdapterFactory:
    def test_build_adapter_file_jsonl(self):
        s = Settings(work_queue_source="file_jsonl", work_queue_file_jsonl_path="/tmp/q")
        adapter = build_adapter(s)
        assert isinstance(adapter, FileJsonlAdapter)
        assert adapter.source_name == "file_jsonl"

    def test_build_adapter_github_issues_returns_stub(self):
        s = Settings(work_queue_source="github_issues")
        adapter = build_adapter(s)
        assert isinstance(adapter, GithubIssuesAdapter)

    def test_build_adapter_linear_returns_stub(self):
        s = Settings(work_queue_source="linear")
        adapter = build_adapter(s)
        assert isinstance(adapter, LinearAdapter)

    @pytest.mark.asyncio
    async def test_github_stub_raises_not_implemented(self):
        with pytest.raises(NotImplementedError):
            await GithubIssuesAdapter().list_active()

    @pytest.mark.asyncio
    async def test_linear_stub_raises_not_implemented(self):
        with pytest.raises(NotImplementedError):
            await LinearAdapter().list_active()


# ---------------------------------------------------------------------------
# WorkQueueItemManager
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def items_mgr(db):
    return WorkQueueItemManager(db, f"test-wq-{uuid.uuid4().hex[:8]}")


class TestWorkQueueItemManager:
    @pytest.mark.asyncio
    async def test_claim_returns_row_on_new_insert(self, items_mgr):
        row = await items_mgr.claim_for_dispatch("src", "ext-1", {"a": 1})
        assert row is not None
        assert row.source == "src"
        assert row.external_id == "ext-1"
        assert row.dispatched_at is None
        assert row.dag_id is None

    @pytest.mark.asyncio
    async def test_claim_returns_none_on_conflict(self, items_mgr):
        first = await items_mgr.claim_for_dispatch("src", "ext-1", None)
        second = await items_mgr.claim_for_dispatch("src", "ext-1", None)
        assert first is not None
        assert second is None  # bool contract: None ⇒ already-seen

    @pytest.mark.asyncio
    async def test_mark_dispatched_links_to_dag(self, items_mgr):
        row = await items_mgr.claim_for_dispatch("src", "ext-1", None)
        dag_id = uuid.uuid4()
        await items_mgr.mark_dispatched(row.id, dag_id)
        fetched = await items_mgr.get_by_external("src", "ext-1")
        assert fetched.dispatched_at is not None
        assert fetched.dag_id == dag_id

    @pytest.mark.asyncio
    async def test_mark_terminal_records_state(self, items_mgr):
        row = await items_mgr.claim_for_dispatch("src", "ext-1", None)
        await items_mgr.mark_terminal(row.id, "closed")
        fetched = await items_mgr.get_by_external("src", "ext-1")
        assert fetched.terminal_state == "closed"

    @pytest.mark.asyncio
    async def test_list_undispatched_finds_aged_orphans(self, items_mgr, db):
        """Reconciler query: rows with dispatched_at=NULL older than grace window."""
        from datetime import UTC, datetime, timedelta
        from sqlalchemy import update

        from nous.storage.models import WorkQueueItem

        row = await items_mgr.claim_for_dispatch("src", "ext-1", None)
        # Backdate created_at so it qualifies as aged.
        async with db.session() as session:
            await session.execute(
                update(WorkQueueItem)
                .where(WorkQueueItem.id == row.id)
                .values(created_at=datetime.now(UTC) - timedelta(minutes=10))
            )
            await session.commit()
        orphans = await items_mgr.list_undispatched("src", timedelta(minutes=5))
        assert len(orphans) == 1
        assert orphans[0].external_id == "ext-1"

    @pytest.mark.asyncio
    async def test_list_undispatched_excludes_dispatched_rows(self, items_mgr, db):
        from datetime import UTC, datetime, timedelta
        from sqlalchemy import update

        from nous.storage.models import WorkQueueItem

        row = await items_mgr.claim_for_dispatch("src", "ext-1", None)
        # Mark dispatched + backdate created_at
        await items_mgr.mark_dispatched(row.id, uuid.uuid4())
        async with db.session() as session:
            await session.execute(
                update(WorkQueueItem)
                .where(WorkQueueItem.id == row.id)
                .values(created_at=datetime.now(UTC) - timedelta(minutes=10))
            )
            await session.commit()
        orphans = await items_mgr.list_undispatched("src", timedelta(minutes=5))
        assert orphans == []


# ---------------------------------------------------------------------------
# WorkQueueCheck — claim + dispatch flow
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def dag_store(db):
    return DAGStore(db, f"test-wq-dag-{uuid.uuid4().hex[:8]}", Settings())


@pytest.fixture
def orchestrator(dag_store):
    return DAGOrchestrator(
        store=dag_store,
        subtask_mgr=AsyncMock(),
        dynamic_loader=MagicMock(),
        settings=Settings(),
    )


@pytest.fixture
def settings_wq_on():
    return Settings(
        work_queue_enabled=True,
        work_queue_source="file_jsonl",
        work_queue_interval_seconds=300,
        work_queue_max_dags_per_tick=5,
    )


def _make_check(adapter, items_mgr, dag_store, orchestrator, settings):
    return WorkQueueCheck(
        adapter=adapter,
        items_mgr=items_mgr,
        dag_store=dag_store,
        orchestrator=orchestrator,
        settings=settings,
    )


class TestWorkQueueCheckRun:
    @pytest.mark.asyncio
    async def test_dispatches_dag_per_new_item(
        self, tmp_path, dag_store, orchestrator, settings_wq_on, db
    ):
        path = tmp_path / "queue.jsonl"
        path.write_text(
            "\n".join([
                json.dumps({"external_id": "a1", "title": "first", "body": "do A"}),
                json.dumps({"external_id": "a2", "title": "second", "body": "do B"}),
            ])
        )
        agent = f"test-wq-run-{uuid.uuid4().hex[:8]}"
        items_mgr = WorkQueueItemManager(db, agent)
        # dag_store must share the same agent so cap-check / scoping align
        store = DAGStore(db, agent, settings_wq_on)
        orch = DAGOrchestrator(
            store=store, subtask_mgr=AsyncMock(),
            dynamic_loader=MagicMock(), settings=settings_wq_on,
        )
        check = _make_check(
            FileJsonlAdapter(str(path)), items_mgr, store, orch, settings_wq_on,
        )
        result = await check.run()
        # 2 dispatch findings
        dispatch_findings = [
            f for f in result.findings if "Dispatched" in f.summary
        ]
        assert len(dispatch_findings) == 2

    @pytest.mark.asyncio
    async def test_idempotent_same_item_not_re_dispatched(
        self, tmp_path, dag_store, orchestrator, settings_wq_on, db
    ):
        """Running the check twice on the same JSONL produces zero new DAGs
        on the second run — the unique constraint stops the duplicate claim."""
        path = tmp_path / "queue.jsonl"
        path.write_text(json.dumps({"external_id": "a1", "body": "task"}))
        agent = f"test-wq-idem-{uuid.uuid4().hex[:8]}"
        items_mgr = WorkQueueItemManager(db, agent)
        store = DAGStore(db, agent, settings_wq_on)
        orch = DAGOrchestrator(
            store=store, subtask_mgr=AsyncMock(),
            dynamic_loader=MagicMock(), settings=settings_wq_on,
        )
        check = _make_check(
            FileJsonlAdapter(str(path)), items_mgr, store, orch, settings_wq_on,
        )
        first = await check.run()
        second = await check.run()
        first_dispatch = sum(1 for f in first.findings if "Dispatched" in f.summary)
        second_dispatch = sum(1 for f in second.findings if "Dispatched" in f.summary)
        assert first_dispatch == 1
        assert second_dispatch == 0

    @pytest.mark.asyncio
    async def test_admission_cap_defers_excess(
        self, tmp_path, settings_wq_on, db
    ):
        """Plan §9.6: per-tick cap bounds DAGs created in one tick."""
        path = tmp_path / "queue.jsonl"
        lines = [
            json.dumps({"external_id": f"x{i}", "body": f"task {i}"})
            for i in range(10)
        ]
        path.write_text("\n".join(lines))
        s = Settings(
            work_queue_enabled=True,
            work_queue_source="file_jsonl",
            work_queue_max_dags_per_tick=3,  # ≤5 per Settings ceiling
            work_queue_interval_seconds=300,
        )
        agent = f"test-wq-cap-{uuid.uuid4().hex[:8]}"
        items_mgr = WorkQueueItemManager(db, agent)
        store = DAGStore(db, agent, s)
        orch = DAGOrchestrator(
            store=store, subtask_mgr=AsyncMock(),
            dynamic_loader=MagicMock(), settings=s,
        )
        check = _make_check(
            FileJsonlAdapter(str(path)), items_mgr, store, orch, s,
        )
        result = await check.run()
        dispatched = sum(1 for f in result.findings if "Dispatched" in f.summary)
        assert dispatched == 3  # capped

    @pytest.mark.asyncio
    async def test_v2_stub_adapter_returns_noop_success(
        self, dag_store, orchestrator, db
    ):
        """A v2 stub raising NotImplementedError is a no-op success, not
        a check failure. Operator configured a stub and shouldn't be paged."""
        s = Settings(
            work_queue_enabled=True,
            work_queue_source="github_issues",
            work_queue_interval_seconds=300,
        )
        agent = f"test-wq-stub-{uuid.uuid4().hex[:8]}"
        items_mgr = WorkQueueItemManager(db, agent)
        store = DAGStore(db, agent, s)
        orch = DAGOrchestrator(
            store=store, subtask_mgr=AsyncMock(),
            dynamic_loader=MagicMock(), settings=s,
        )
        check = _make_check(
            GithubIssuesAdapter(), items_mgr, store, orch, s,
        )
        result = await check.run()
        assert result.findings == []


# ---------------------------------------------------------------------------
# Terminal-state handling
# ---------------------------------------------------------------------------


class TestTerminalState:
    @pytest.mark.asyncio
    async def test_terminal_item_cancels_dispatched_dag(
        self, tmp_path, settings_wq_on, db
    ):
        # First run dispatches; second run with same item flipped terminal
        # cancels the DAG.
        path = tmp_path / "queue.jsonl"
        path.write_text(json.dumps({"external_id": "a1", "body": "task"}))
        agent = f"test-wq-term-{uuid.uuid4().hex[:8]}"
        items_mgr = WorkQueueItemManager(db, agent)
        store = DAGStore(db, agent, settings_wq_on)
        cancel_calls: list = []

        class _SpyOrch:
            async def cancel_dag(self, dag_id, reason="cancelled"):
                cancel_calls.append((dag_id, reason))

        check = _make_check(
            FileJsonlAdapter(str(path)), items_mgr, store, _SpyOrch(), settings_wq_on,
        )
        first = await check.run()
        # Flip to terminal
        path.write_text(json.dumps({
            "external_id": "a1", "body": "task",
            "state": "closed", "terminal": True,
        }))
        second = await check.run()
        assert len(cancel_calls) == 1
        fetched = await items_mgr.get_by_external("file_jsonl", "a1")
        assert fetched.terminal_state == "closed"

    @pytest.mark.asyncio
    async def test_cancel_dag_failure_skips_mark_terminal(
        self, tmp_path, settings_wq_on, db
    ):
        """Plan §9.6: if cancel_dag raises, mark_terminal is NOT called.
        Next tick retries the cancel (terminal_state still NULL)."""
        path = tmp_path / "queue.jsonl"
        path.write_text(json.dumps({"external_id": "a1", "body": "task"}))
        agent = f"test-wq-canfail-{uuid.uuid4().hex[:8]}"
        items_mgr = WorkQueueItemManager(db, agent)
        store = DAGStore(db, agent, settings_wq_on)

        class _FailingOrch:
            async def cancel_dag(self, dag_id, reason="cancelled"):
                raise RuntimeError("simulated cancel failure")

        check = _make_check(
            FileJsonlAdapter(str(path)), items_mgr, store, _FailingOrch(), settings_wq_on,
        )
        await check.run()  # dispatch
        path.write_text(json.dumps({
            "external_id": "a1", "body": "task",
            "state": "closed", "terminal": True,
        }))
        result = await check.run()
        # mark_terminal NOT called — terminal_state still NULL
        fetched = await items_mgr.get_by_external("file_jsonl", "a1")
        assert fetched.terminal_state is None
        # A high-urgency finding reports the failure
        assert any(f.urgency == "high" for f in result.findings)
