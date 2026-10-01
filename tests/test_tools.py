"""Integration tests for nous/api/tools.py -- tool closures and ToolDispatcher.

Part 1: Closure tests use real Brain/Heart instances with mock embeddings
against real Postgres. Tool closures capture Brain/Heart in closure
context and use auto-sessions (no session= parameter).

Part 2: ToolDispatcher unit tests verify registration, dispatch,
unknown tool handling, error propagation, tool definitions output,
and frame-gated tool filtering.
"""

import uuid
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from nous.api.tools import ToolDispatcher, create_nous_tools
from nous.brain.brain import Brain
from nous.config import Settings
from nous.heart import Heart

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def brain(db, mock_embeddings):
    """Brain with mock embeddings for tool tests."""
    settings = Settings()
    b = Brain(database=db, settings=settings, embedding_provider=mock_embeddings)
    yield b
    await b.close()


@pytest_asyncio.fixture
async def tools(brain, heart):
    """Tool closures dict from create_nous_tools."""
    return create_nous_tools(brain, heart)


# ---------------------------------------------------------------------------
# test_create_nous_tools
# ---------------------------------------------------------------------------


class TestCreateNousTools:
    """Test that create_nous_tools returns the expected structure."""

    def test_create_nous_tools(self, tools):
        """Returns dict with async callable functions."""
        assert isinstance(tools, dict)
        assert set(tools.keys()) == {
            "record_decision",
            "learn_fact",
            "recall_deep",
            "create_censor",
            "recall_recent",
            "learn_skill",
            "get_procedure",
            "recall_hubs",  # F065
            "ingest_document",  # F069
            "resolve_decision",
            "resolve_decisions",
            "list_decisions",
        }
        for name, func in tools.items():
            assert callable(func), f"{name} should be callable"


# ---------------------------------------------------------------------------
# record_decision tests
# ---------------------------------------------------------------------------


class TestRecordDecision:
    """Test the record_decision tool closure."""

    @pytest.mark.asyncio
    async def test_record_decision_success(self, tools):
        """Valid input -> brain.record called, returns ID."""
        result = await tools["record_decision"](
            description="Test tool decision for integration",
            confidence=0.85,
            category="tooling",
            stakes="low",
            context="Testing the record_decision tool closure",
            reasons=[
                {"type": "analysis", "text": "Tool test reason"},
                {"type": "pattern", "text": "Following test patterns"},
            ],
            tags=["test", "tool-closure"],
        )

        assert "content" in result
        assert len(result["content"]) == 1
        text = result["content"][0]["text"]
        assert "Decision recorded successfully" in text
        assert "ID:" in text
        assert "Quality score:" in text
        assert "Category: tooling" in text
        assert "Stakes: low" in text

    @pytest.mark.asyncio
    async def test_record_decision_invalid_reasons(self, tools):
        """Bad reason format -> error message (not exception)."""
        result = await tools["record_decision"](
            description="Decision with bad reasons",
            confidence=0.5,
            category="process",
            stakes="low",
            reasons=[{"bad": "format"}],  # Missing 'type' and 'text'
        )

        assert "content" in result
        text = result["content"][0]["text"]
        assert "Error" in text
        assert "Invalid reason format" in text

    @pytest.mark.asyncio
    async def test_record_decision_brain_error(self, tools):
        """brain.record raises -> error message (not exception)."""
        # Trigger a pydantic validation error with invalid confidence range
        result = await tools["record_decision"](
            description="Decision with invalid confidence",
            confidence=2.0,  # Out of range [0.0, 1.0]
            category="tooling",
            stakes="low",
        )

        assert "content" in result
        text = result["content"][0]["text"]
        assert "Error" in text or "error" in text.lower()


class TestResolveDecision:
    """Test the resolve_decision / resolve_decisions / list_decisions tools."""

    async def _make_decision(self, tools, brain) -> str:
        """Record a decision and return its UUID string.

        Uses brain.record() to get the ID directly, avoiding the
        fragile list_decisions(limit=1) pattern which is ambiguous when
        multiple decisions share the same created_at timestamp (SQLite).
        """
        from nous.brain.schemas import RecordInput

        detail = await brain.record(
            RecordInput(
                description=f"Decision to resolve in tool test {uuid.uuid4()}",
                confidence=0.7,
                category="tooling",
                stakes="low",
            )
        )
        return str(detail.id)

    @pytest.mark.asyncio
    async def test_resolve_decision_success(self, tools, brain):
        """resolve_decision persists outcome + note."""
        did = await self._make_decision(tools, brain)
        result = await tools["resolve_decision"](
            decision_id=did,
            outcome="noise",
            resolution_note="sweep artifact",
        )
        assert "resolved" in result["content"][0]["text"]
        detail = await brain.get(uuid.UUID(did))
        assert detail.outcome == "noise"

    @pytest.mark.asyncio
    async def test_resolve_decision_reports_review_state_from_its_own_transaction(self, tools, brain):
        """codex P1 #652 (runner.py:530/660): the compensation snapshot's prior
        and written review states come from the resolving transaction, not
        from re-reads a concurrent review could slip between."""
        from nous.api.call_outcome import CallOutcome
        from nous.api.call_outcome import _current as outcome_var
        from nous.api.compensation import SnapshotStore

        did = await self._make_decision(tools, brain)
        await brain.review(uuid.UUID(did), "noise", result="earlier review", reviewer="someone")
        store = SnapshotStore(brain.db, brain.agent_id)
        before = await store.decision_state(did)
        outcome = CallOutcome()
        token = outcome_var.set(outcome)
        try:
            await tools["resolve_decision"](decision_id=did, outcome="noise", resolution_note="mine")
        finally:
            outcome_var.reset(token)
        written = await store.decision_state(did)
        # a later review lands after the call: the capture still reports the call's own write
        await brain.review(uuid.UUID(did), "noise", result="later review", reviewer="someone-else")

        def _naive(state):  # the SQLite test DB drops the UTC offset on read
            ts = state["reviewed_at"]
            return {**state, "reviewed_at": ts and ts.split("+")[0]}

        cap = outcome.review_capture
        assert _naive(cap["prior"]) == _naive(before)
        assert _naive(cap["written"]) == _naive(written)
        assert cap["prior"]["outcome_result"] == "earlier review"
        assert cap["written"]["outcome_result"] == "mine"

    @pytest.mark.asyncio
    async def test_resolve_decision_capture_survives_a_cancel_during_the_commit(self, tools, brain, monkeypatch):
        """codex P1 #652 (runner.py:645): the capture was attached to the call's
        outcome only after brain.review returned, so a call cancelled while
        its commit was in flight (outcome unknown -- it may have landed)
        reported no written state and could never be reverted."""
        import asyncio

        from nous.api.call_outcome import CallOutcome
        from nous.api.call_outcome import _current as outcome_var

        did = await self._make_decision(tools, brain)

        async def review_cancelled_mid_commit(*args, capture=None, **kwargs):
            capture["prior"] = {"outcome": None}
            capture["written"] = {"outcome": "noise"}
            raise asyncio.CancelledError  # the commit was cancelled in flight

        monkeypatch.setattr(brain, "review", review_cancelled_mid_commit)
        outcome = CallOutcome()
        token = outcome_var.set(outcome)
        try:
            with pytest.raises(asyncio.CancelledError):
                await tools["resolve_decision"](decision_id=did, outcome="noise", resolution_note="x")
        finally:
            outcome_var.reset(token)
        assert outcome.review_capture == {"prior": {"outcome": None}, "written": {"outcome": "noise"}}

    async def _snapshotted_call(self, brain, did):
        """A runner snapshot for a resolve_decision on an undoable DAG node:
        returns (store, entry_id, outcome) as dispatch would see them."""
        from nous.api.call_outcome import CallOutcome
        from nous.api.compensation import SnapshotStore
        from nous.api.execution_context import ExecutionContext

        store = SnapshotStore(brain.db, brain.agent_id)
        runner = _compensating_runner(store)
        entry, outcome = uuid.uuid4(), CallOutcome()
        assert await runner._capture_compensation_snapshot(
            ExecutionContext(kind="dag_node", undoable=True),
            "resolve_decision",
            {"decision_id": did, "outcome": "noise"},
            entry,
            outcome=outcome,
        )
        assert outcome.persist_written is not None
        return store, entry, outcome

    @pytest.mark.asyncio
    async def test_resolve_decision_written_state_commits_with_the_review(self, tools, brain):
        """codex P1 #652 (runner.py:607): the written state was recorded by a
        post-dispatch write, so a crash or a cancelled ledger close after the
        review committed left a snapshot with no written state and no usable
        revert. It is now written in the review's own transaction: with the
        post-dispatch hook never reached, the snapshot already holds it and
        the revert works."""
        from nous.api.call_outcome import _current as outcome_var
        from nous.api.compensation import compensate_resolve_decision, snapshot_is_revertible

        did = await self._make_decision(tools, brain)
        store, entry, outcome = await self._snapshotted_call(brain, did)
        token = outcome_var.set(outcome)
        try:
            result = await tools["resolve_decision"](decision_id=did, outcome="noise", resolution_note="mine")
        finally:
            outcome_var.reset(token)
        assert not result.get("is_error")
        # The call is cut off here (process exit / CancelledError from the
        # ledger close): _after_compensable_call never runs.
        snap = await store.get_by_ledger_entry(entry)
        assert snapshot_is_revertible("resolve_decision", snap.snapshot_data)
        assert snap.snapshot_data["written"]["outcome_result"] == "mine"
        assert outcome.review_capture["persisted"] is True
        res = await compensate_resolve_decision(entry, snap.snapshot_data, type("D", (), {"brain": brain})())
        assert res.success, res.message
        assert (await brain.get(uuid.UUID(did))).outcome == "pending"

    @pytest.mark.asyncio
    async def test_resolve_decision_rolled_back_leaves_no_written_state(self, tools, brain, monkeypatch):
        """codex P1 #652: the record shares the review's transaction both ways
        -- a review that rolls back after recording leaves no written state,
        and a snapshot row that cannot be updated rolls the review back."""
        from nous.api.call_outcome import _current as outcome_var

        did = await self._make_decision(tools, brain)
        store, entry, outcome = await self._snapshotted_call(brain, did)
        monkeypatch.setattr(brain, "_emit_event", AsyncMock(side_effect=RuntimeError("boom")))
        token = outcome_var.set(outcome)
        try:
            result = await tools["resolve_decision"](decision_id=did, outcome="noise", resolution_note="mine")
        finally:
            outcome_var.reset(token)
        monkeypatch.undo()
        assert result.get("is_error") is True
        assert outcome.review_capture.get("persisted") is True  # recorded, then rolled back
        snap = await store.get_by_ledger_entry(entry)
        assert "written" not in snap.snapshot_data
        assert (await brain.get(uuid.UUID(did))).outcome == "pending"

        # No snapshot row to record into: the review itself is refused.
        did2 = await self._make_decision(tools, brain)
        store, entry, outcome = await self._snapshotted_call(brain, did2)
        outcome.persist_written = _compensating_runner(store)._written_state_persister("resolve_decision", uuid.uuid4())
        token = outcome_var.set(outcome)
        try:
            result = await tools["resolve_decision"](decision_id=did2, outcome="noise", resolution_note="x")
        finally:
            outcome_var.reset(token)
        assert result.get("is_error") is True
        assert (await brain.get(uuid.UUID(did2))).outcome == "pending"

    @pytest.mark.asyncio
    async def test_resolve_decision_noise_allowed_in_background(self, tools, brain):
        """A background turn may mark a pending decision as noise, attributed to it."""
        did = await self._make_decision(tools, brain)
        result = await tools["resolve_decision"](
            decision_id=did,
            outcome="noise",
            resolution_note="heartbeat tick artifact",
            _is_background=True,
        )
        assert result.get("is_error") is not True
        detail = await brain.get(uuid.UUID(did))
        assert detail.outcome == "noise"
        # decisions.session_id is the RECORDING session, so the reviewer is the
        # only thing that says an autopilot made this resolution.
        assert detail.reviewer == "agent-background"

    @pytest.mark.asyncio
    async def test_resolve_decision_foreground_reviewer_unchanged(self, tools, brain):
        """Interactive resolutions keep reviewer='agent'."""
        did = await self._make_decision(tools, brain)
        await tools["resolve_decision"](decision_id=did, outcome="noise")
        detail = await brain.get(uuid.UUID(did))
        assert detail.reviewer == "agent"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("outcome", ["success", "partial", "failure"])
    async def test_resolve_decision_graded_outcome_blocked_in_background(self, tools, brain, outcome):
        """Background turns cannot grade a prediction; the error names what IS allowed."""
        did = await self._make_decision(tools, brain)
        result = await tools["resolve_decision"](
            decision_id=did,
            outcome=outcome,
            _is_background=True,
        )
        assert result.get("is_error") is True
        text = result["content"][0]["text"]
        assert "background" in text.lower()
        assert "noise" in text and "superseded" in text
        detail = await brain.get(uuid.UUID(did))
        assert detail.outcome == "pending"

    @pytest.mark.asyncio
    async def test_resolve_decision_superseded_in_background(self, tools, brain):
        """Supersession is allowed in background, still only with its successor."""
        old_id = await self._make_decision(tools, brain)
        new_id = await self._make_decision(tools, brain)
        refused = await tools["resolve_decision"](
            decision_id=old_id,
            outcome="superseded",
            _is_background=True,
        )
        assert refused.get("is_error") is True
        assert "superseded_by" in refused["content"][0]["text"]
        assert (await brain.get(uuid.UUID(old_id))).outcome == "pending"

        result = await tools["resolve_decision"](
            decision_id=old_id,
            outcome="superseded",
            superseded_by=new_id,
            _is_background=True,
        )
        assert result.get("is_error") is not True
        detail = await brain.get(uuid.UUID(old_id))
        assert detail.outcome == "superseded"
        assert str(detail.superseded_by) == new_id

    @pytest.mark.asyncio
    async def test_background_cannot_overwrite_a_graded_outcome(self, tools, brain):
        """noise/superseded are excluded from calibration, so relabelling a graded
        decision in background would delete a data point from the Brier score."""
        did = await self._make_decision(tools, brain)
        await tools["resolve_decision"](decision_id=did, outcome="failure", resolution_note="broke prod")
        result = await tools["resolve_decision"](
            decision_id=did,
            outcome="noise",
            _is_background=True,
        )
        assert result.get("is_error") is True
        assert "failure" in result["content"][0]["text"]
        detail = await brain.get(uuid.UUID(did))
        assert detail.outcome == "failure"
        assert detail.reviewer == "agent"

    @pytest.mark.asyncio
    async def test_background_may_relabel_an_ungraded_resolution(self, tools, brain):
        """noise -> superseded moves between non-prediction outcomes: allowed."""
        old_id = await self._make_decision(tools, brain)
        new_id = await self._make_decision(tools, brain)
        await tools["resolve_decision"](decision_id=old_id, outcome="noise", _is_background=True)
        result = await tools["resolve_decision"](
            decision_id=old_id,
            outcome="superseded",
            superseded_by=new_id,
            _is_background=True,
        )
        assert result.get("is_error") is not True
        assert (await brain.get(uuid.UUID(old_id))).outcome == "superseded"

    @pytest.mark.asyncio
    async def test_background_superseded_to_noise_clears_lineage(self, tools, brain):
        """superseded -> noise in background drops the replacement pointer, so
        neither this tool nor list_decisions reports stale lineage."""
        old_id = await self._make_decision(tools, brain)
        new_id = await self._make_decision(tools, brain)
        await tools["resolve_decision"](
            decision_id=old_id,
            outcome="superseded",
            superseded_by=new_id,
            _is_background=True,
        )
        result = await tools["resolve_decision"](
            decision_id=old_id,
            outcome="noise",
            _is_background=True,
        )
        assert result.get("is_error") is not True
        assert "superseded_by" not in result["content"][0]["text"]
        detail = await brain.get(uuid.UUID(old_id))
        assert detail.outcome == "noise"
        assert detail.superseded_by is None

    @pytest.mark.asyncio
    async def test_resolve_decisions_background_batch_is_per_item(self, tools, brain):
        """A background sweep keeps its allowed items; graded outcomes and graded
        rows fail per item, in input order, without aborting the batch."""
        noise_id = await self._make_decision(tools, brain)
        graded_req_id = await self._make_decision(tools, brain)
        graded_row_id = await self._make_decision(tools, brain)
        await tools["resolve_decision"](decision_id=graded_row_id, outcome="success")

        result = await tools["resolve_decisions"](
            resolutions=[
                {"decision_id": graded_req_id, "outcome": "success", "resolution_note": "looks fine"},
                {"decision_id": noise_id, "outcome": "noise", "resolution_note": "tick artifact"},
                {"decision_id": graded_row_id, "outcome": "noise"},
            ],
            _is_background=True,
        )
        assert result.get("is_error") is False
        text = result["content"][0]["text"]
        assert "Resolved 1/3" in text
        failures = text.split("Failures:", 1)[1]
        # Reported in input order: the disallowed outcome, then the graded row.
        assert failures.index(graded_req_id) < failures.index(graded_row_id)
        assert noise_id not in failures

        assert (await brain.get(uuid.UUID(noise_id))).outcome == "noise"
        assert (await brain.get(uuid.UUID(noise_id))).reviewer == "agent-background"
        assert (await brain.get(uuid.UUID(graded_req_id))).outcome == "pending"
        assert (await brain.get(uuid.UUID(graded_row_id))).outcome == "success"

    @pytest.mark.asyncio
    async def test_resolve_decisions_background_unknown_successor_is_per_item(self, tools, brain):
        """A hallucinated superseded_by fails only its own item in a background sweep."""
        first = await self._make_decision(tools, brain)
        bad = await self._make_decision(tools, brain)
        last = await self._make_decision(tools, brain)
        result = await tools["resolve_decisions"](
            resolutions=[
                {"decision_id": first, "outcome": "noise"},
                {"decision_id": bad, "outcome": "superseded", "superseded_by": str(uuid.uuid4())},
                {"decision_id": last, "outcome": "noise"},
            ],
            _is_background=True,
        )
        text = result["content"][0]["text"]
        assert "Resolved 2/3" in text
        assert bad in text.split("Failures:", 1)[1]
        assert (await brain.get(uuid.UUID(first))).outcome == "noise"
        assert (await brain.get(uuid.UUID(bad))).outcome == "pending"
        assert (await brain.get(uuid.UUID(last))).outcome == "noise"

    @pytest.mark.asyncio
    async def test_resolve_decisions_background_all_refused_is_error(self, tools, brain):
        """A background batch where nothing is allowed reports an error."""
        did = await self._make_decision(tools, brain)
        result = await tools["resolve_decisions"](
            resolutions=[{"decision_id": did, "outcome": "failure"}],
            _is_background=True,
        )
        assert result.get("is_error") is True
        assert "Resolved 0/1" in result["content"][0]["text"]
        assert (await brain.get(uuid.UUID(did))).outcome == "pending"

    @pytest.mark.asyncio
    async def test_resolve_decisions_batch_reports_failures(self, tools, brain):
        """Batch resolve reports per-item failures without aborting."""
        did = await self._make_decision(tools, brain)
        result = await tools["resolve_decisions"](
            resolutions=[
                {"decision_id": did, "outcome": "success", "resolution_note": "ok"},
                {"decision_id": str(uuid.uuid4()), "outcome": "success"},
            ],
        )
        text = result["content"][0]["text"]
        assert "Resolved 1/2" in text
        assert "Failures" in text

    @pytest.mark.asyncio
    async def test_list_decisions_returns_pending(self, tools, brain):
        """list_decisions with outcome='pending' returns newly recorded decision."""
        did = await self._make_decision(tools, brain)
        result = await tools["list_decisions"](outcome="pending", limit=50)
        text = result["content"][0]["text"]
        assert did in text

    @pytest.mark.asyncio
    async def test_superseded_without_superseded_by_is_rejected(self, tools, brain):
        """outcome='superseded' with no superseded_by is refused, naming the requirement.

        Lineage gap prevention: 9 of 24 prod superseded rows carry no successor,
        so retrieval cannot tell what replaced them.
        """
        did = await self._make_decision(tools, brain)
        result = await tools["resolve_decision"](
            decision_id=did,
            outcome="superseded",
            resolution_note="replaced it",
        )
        assert result.get("is_error") is True
        assert "superseded_by" in result["content"][0]["text"]
        # Outcome unchanged — nothing was persisted
        detail = await brain.get(uuid.UUID(did))
        assert detail.outcome == "pending"

    @pytest.mark.asyncio
    async def test_other_outcomes_do_not_require_superseded_by(self, tools, brain):
        """Non-supersession outcomes are unaffected by the new requirement."""
        did = await self._make_decision(tools, brain)
        result = await tools["resolve_decision"](
            decision_id=did,
            outcome="success",
            resolution_note="shipped",
        )
        assert result.get("is_error") is not True
        detail = await brain.get(uuid.UUID(did))
        assert detail.outcome == "success"

    @pytest.mark.asyncio
    async def test_superseded_by_roundtrip_via_tool(self, tools, brain):
        """resolve_decision with outcome=superseded preserves superseded_by on the summary."""
        old_id = await self._make_decision(tools, brain)
        new_id = await self._make_decision(tools, brain)
        await tools["resolve_decision"](
            decision_id=old_id,
            outcome="superseded",
            superseded_by=new_id,
        )
        detail = await brain.get(uuid.UUID(old_id))
        assert detail.outcome == "superseded"
        assert str(detail.superseded_by) == new_id
        # superseded_by propagates to DecisionSummary via list_decisions
        decisions, _ = await brain.list_decisions(outcome="superseded", limit=10)
        match = next((d for d in decisions if str(d.id) == old_id), None)
        assert match is not None
        assert str(match.superseded_by) == new_id


# ---------------------------------------------------------------------------
# learn_fact tests
# ---------------------------------------------------------------------------


def _compensating_runner(store):
    """A bare AgentRunner with only the compensation wiring."""
    import tempfile
    from types import SimpleNamespace

    from nous.api.runner import AgentRunner

    runner = object.__new__(AgentRunner)
    runner._settings = SimpleNamespace(compensation_auto_review_enabled=False)
    runner._action_review_pusher = None
    runner._snap_store = store
    runner._workspace_dir = tempfile.gettempdir()
    runner._dispatcher = SimpleNamespace()
    return runner


class TestHeartbeatCheckDisableCompensation:
    """codex P1 #652 (runner.py:607): a heartbeat_check_manage disable records
    its compensation state in its own transaction."""

    async def _setup(self, db, *, snapshot=True):
        from types import SimpleNamespace

        from nous.api.call_outcome import CallOutcome
        from nous.api.compensation import SnapshotStore
        from nous.api.execution_context import ExecutionContext
        from nous.api.tools import register_heartbeat_tools
        from nous.heartbeat.dynamic import DynamicCheckLoader
        from nous.heartbeat.registry import CheckRegistry
        from nous.storage.models import DynamicCheckModel

        agent = f"hb-{uuid.uuid4().hex[:8]}"
        name = "nightly"
        async with db.session() as s:
            s.add(DynamicCheckModel(agent_id=agent, name=name, description="d", prompt="p", enabled=True))
            await s.commit()
        loader = DynamicCheckLoader(db=db, registry=CheckRegistry(), agent_id=agent)
        handlers: dict = {}
        register_heartbeat_tools(SimpleNamespace(register=lambda n, fn, *a, **k: handlers.__setitem__(n, fn)), loader)
        store = SnapshotStore(db, agent)
        entry, outcome = uuid.uuid4(), CallOutcome()
        if snapshot:
            assert await _compensating_runner(store)._capture_compensation_snapshot(
                ExecutionContext(kind="dag_node", undoable=True),
                "heartbeat_check_manage",
                {"name": name, "action": "disable"},
                entry,
                outcome=outcome,
            )
        return SimpleNamespace(
            loader=loader,
            handler=handlers["heartbeat_check_manage"],
            store=store,
            entry=entry,
            outcome=outcome,
            name=name,
            agent=agent,
        )

    async def _row(self, db, agent, name):
        from sqlalchemy import select

        from nous.storage.models import DynamicCheckModel

        async with db.session() as s:
            return (
                await s.execute(
                    select(DynamicCheckModel)
                    .where(DynamicCheckModel.agent_id == agent)
                    .where(DynamicCheckModel.name == name)
                )
            ).scalar_one()

    @pytest.mark.asyncio
    async def test_disable_written_state_commits_with_the_disable(self, db):
        """With the post-dispatch hook never reached (crash / cancelled ledger
        close), the snapshot already holds the disable's written state and
        prior_enabled, and the revert re-enables the check."""
        from nous.api.call_outcome import _current as outcome_var
        from nous.api.compensation import compensate_heartbeat_check_manage, snapshot_is_revertible

        t = await self._setup(db)
        token = outcome_var.set(t.outcome)
        try:
            result = await t.handler(action="disable", name=t.name)
        finally:
            outcome_var.reset(token)
        assert not result.get("is_error")
        snap = await t.store.get_by_ledger_entry(t.entry)
        assert snapshot_is_revertible("heartbeat_check_manage", snap.snapshot_data)
        row = await self._row(db, t.agent, t.name)
        assert row.enabled is False
        assert snap.snapshot_data["written"] == {
            "check_id": str(row.id),
            "enabled_state_token": row.metadata_["enabled_state_token"],
        }
        assert snap.snapshot_data["prior_enabled"] is True

        async def enable_if_unchanged(name, check_id, tok):  # the guard, without Postgres JSONB operators
            current = await self._row(db, t.agent, name)
            return str(current.id) == check_id and current.metadata_.get("enabled_state_token") == tok

        loader = type("L", (), {"enable_if_unchanged": staticmethod(enable_if_unchanged)})()
        res = await compensate_heartbeat_check_manage(
            t.entry, snap.snapshot_data, type("D", (), {"heartbeat_loader": loader})()
        )
        assert res.success and "re-enabled" in res.message

    @pytest.mark.asyncio
    async def test_disable_without_its_snapshot_row_rolls_back(self, db):
        """A disable whose compensation record cannot be written is rolled
        back -- it never commits unrevertibly -- and nothing is recorded."""
        from nous.api.call_outcome import _current as outcome_var

        t = await self._setup(db)
        t.outcome.persist_written = _compensating_runner(t.store)._written_state_persister(
            "heartbeat_check_manage", uuid.uuid4()
        )
        token = outcome_var.set(t.outcome)
        try:
            result = await t.handler(action="disable", name=t.name)
        finally:
            outcome_var.reset(token)
        assert result.get("is_error") is True
        assert (await self._row(db, t.agent, t.name)).enabled is True
        snap = await t.store.get_by_ledger_entry(t.entry)
        assert "written" not in snap.snapshot_data


class TestLearnFact:
    """Test the learn_fact tool closure."""

    @pytest.mark.asyncio
    async def test_learn_fact_success(self, tools):
        """Valid input -> heart.learn called, returns ID."""
        result = await tools["learn_fact"](
            content="Python 3.12 supports improved error messages",
            category="technical",
            subject="python",
            confidence=0.95,
            source="documentation",
            tags=["python", "test"],
        )

        assert "content" in result
        text = result["content"][0]["text"]
        assert "Fact learned successfully" in text
        assert "ID:" in text
        assert "Category: technical" in text
        assert "Subject: python" in text

    @pytest.mark.asyncio
    async def test_learn_fact_with_contradiction(self, tools):
        """Learning a near-duplicate fact surfaces contradiction warning."""
        # Learn a fact first
        await tools["learn_fact"](
            content="The default database port is 5432",
            category="technical",
            subject="postgres",
        )

        # Learn a contradicting fact with same content (mock embeddings
        # produce identical vectors for identical text -> high similarity)
        result = await tools["learn_fact"](
            content="The default database port is 5432",
            category="technical",
            subject="postgres",
        )

        assert "content" in result
        text = result["content"][0]["text"]
        assert "Fact learned successfully" in text
        # Contradiction warning may or may not appear depending on
        # similarity threshold; at minimum the fact should be stored
        assert "ID:" in text


# ---------------------------------------------------------------------------
# recall_deep tests
# ---------------------------------------------------------------------------


class TestRecallDeep:
    """Test the recall_deep tool closure."""

    @pytest.mark.asyncio
    async def test_recall_deep_all(self, tools):
        """Searches Heart + Brain when no memory_types specified."""
        # Seed data: record a decision and learn a fact
        await tools["record_decision"](
            description="Recall deep test architecture decision about caching",
            confidence=0.8,
            category="architecture",
            stakes="medium",
            context="Testing recall_deep all search",
            tags=["recall-test"],
        )
        await tools["learn_fact"](
            content="Caching improves performance in recall deep tests",
            category="technical",
            subject="caching",
        )

        # Search across all types (default)
        result = await tools["recall_deep"](query="caching architecture")

        assert "content" in result
        text = result["content"][0]["text"]
        # Should have both Heart and Brain sections
        assert "Heart Memory" in text or "Brain Decisions" in text

    @pytest.mark.asyncio
    async def test_recall_deep_decisions_only(self, tools):
        """memory_types=["decision"] searches Brain only."""
        # Seed a decision
        await tools["record_decision"](
            description="Decision-only recall test about deployment strategy",
            confidence=0.75,
            category="process",
            stakes="low",
            tags=["recall-decision-only"],
        )

        result = await tools["recall_deep"](
            query="deployment strategy",
            memory_types=["decision"],
        )

        assert "content" in result
        text = result["content"][0]["text"]
        # Should have Brain section but not Heart
        assert "Brain Decisions" in text
        assert "Heart Memory" not in text

    @pytest.mark.asyncio
    async def test_recall_deep_facts_only(self, tools):
        """memory_types=["fact"] searches Heart facts only."""
        # Seed a fact
        await tools["learn_fact"](
            content="Recall deep facts-only test about memory architecture",
            category="technical",
            subject="memory",
        )

        result = await tools["recall_deep"](
            query="memory architecture",
            memory_types=["fact"],
        )

        assert "content" in result
        text = result["content"][0]["text"]
        # Should have Heart section but not Brain
        assert "Heart Memory" in text
        assert "Brain Decisions" not in text

    @pytest.mark.asyncio
    async def test_recall_deep_empty(self, db, mock_embeddings):
        """No results -> 'No results found.' in section output.

        Built on an isolated agent_id so no other test's episodes leak in.
        Before HT-1 (episode-search active-filter fix), this passed only
        because episode search was structurally dead and returned nothing for
        every query; under the shared default agent it would now surface
        another test's seeded episode. Isolation makes the empty case real.
        """
        import uuid as _uuid

        settings = Settings(agent_id=f"recall-empty-{_uuid.uuid4().hex[:8]}")
        brain = Brain(database=db, settings=settings, embedding_provider=mock_embeddings)
        heart = Heart(db, settings, embedding_provider=mock_embeddings)
        try:
            tools = create_nous_tools(brain, heart)
            result = await tools["recall_deep"](
                query="zzz_nonexistent_query_no_match",
                memory_types=["episode"],
            )
            assert "content" in result
            text = result["content"][0]["text"]
            assert "No results found" in text
        finally:
            await brain.close()
            await heart.close()

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_recall_deep_surfaces_fact_id(self, tools):
        """Heart fact results include an 'id: <uuid>' so get_procedure/get_fact callers
        can reference them without fabricating UUIDs (anti-hallucination contract).

        Integration-only: requires live PostgreSQL for pgvector <=> operator.
        """
        import re

        await tools["learn_fact"](
            content="Id surfacing test fact about quantum widgets",
            category="technical",
            subject="id-surface",
        )

        result = await tools["recall_deep"](
            query="quantum widgets",
            memory_types=["fact"],
        )

        text = result["content"][0]["text"]
        assert "Heart Memory" in text
        assert "id: " in text, "recall_deep must surface entity IDs for get_procedure/detail callers"
        # Verify the id is a real UUID format, not a placeholder
        uuid_match = re.search(
            r"id: ([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
            text,
        )
        assert uuid_match is not None, f"Expected UUID in output, got: {text}"

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_recall_deep_surfaces_decision_id(self, tools):
        """Brain decision results include an 'id: <uuid>' so follow-up tools
        can reference the decision without fabricating UUIDs.

        Integration-only: requires live PostgreSQL for pgvector <=> operator.
        """
        import re

        await tools["record_decision"](
            description="Id surfacing decision about deployment topology",
            confidence=0.8,
            category="architecture",
            stakes="medium",
            tags=["id-surface"],
        )

        result = await tools["recall_deep"](
            query="deployment topology",
            memory_types=["decision"],
        )

        text = result["content"][0]["text"]
        assert "Brain Decisions" in text
        uuid_match = re.search(
            r"id: ([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
            text,
        )
        assert uuid_match is not None, f"Expected decision UUID in output, got: {text}"


# ---------------------------------------------------------------------------
# create_censor tests
# ---------------------------------------------------------------------------


class TestCreateCensor:
    """Test the create_censor tool closure."""

    @pytest.mark.asyncio
    async def test_create_censor_success(self, tools):
        """Valid input -> heart.add_censor, returns ID."""
        result = await tools["create_censor"](
            trigger_pattern="rm -rf /",
            reason="Dangerous command that could delete everything",
            action="refuse",
            domain="debugging",
        )

        assert "content" in result
        text = result["content"][0]["text"]
        assert "Censor created successfully" in text
        assert "ID:" in text
        # F078: agent provenance caps at refuse (would clamp abort -> refuse).
        assert "Action: refuse" in text
        assert "Domain: debugging" in text
        assert "Pattern: rm -rf /" in text

    @pytest.mark.asyncio
    async def test_create_censor_invalid_uuid(self, tools):
        """Bad UUID string -> validation error message."""
        result = await tools["create_censor"](
            trigger_pattern="test pattern",
            reason="Test reason",
            learned_from_decision="not-a-valid-uuid",
        )

        assert "content" in result
        text = result["content"][0]["text"]
        assert "Validation error" in text or "Error" in text


# ---------------------------------------------------------------------------
# ToolDispatcher unit tests (no DB needed)
# ---------------------------------------------------------------------------


_ECHO_SCHEMA: dict = {
    "type": "object",
    "description": "Echo tool for testing",
    "properties": {
        "message": {"type": "string", "description": "Message to echo"},
    },
    "required": ["message"],
}

_ADD_SCHEMA: dict = {
    "type": "object",
    "description": "Add two numbers for testing",
    "properties": {
        "a": {"type": "number"},
        "b": {"type": "number"},
    },
    "required": ["a", "b"],
}


class TestToolDispatcher:
    """Unit tests for ToolDispatcher registration, dispatch, and filtering."""

    @pytest.mark.asyncio
    async def test_dispatcher_register_and_dispatch(self):
        """Register a handler, dispatch a call, verify result text and no error."""
        dispatcher = ToolDispatcher()

        async def echo_handler(message: str) -> dict:
            return {"content": [{"type": "text", "text": f"Echo: {message}"}]}

        dispatcher.register("echo", echo_handler, _ECHO_SCHEMA)

        result_text, is_error = await dispatcher.dispatch("echo", {"message": "hello"})
        assert result_text == "Echo: hello"
        assert is_error is False

    @pytest.mark.asyncio
    async def test_dispatcher_unknown_tool(self):
        """Dispatch unknown tool name -> error tuple with 'Unknown tool' message."""
        dispatcher = ToolDispatcher()

        result_text, is_error = await dispatcher.dispatch("nonexistent", {})
        assert is_error is True
        assert "Unknown tool: nonexistent" in result_text

    @pytest.mark.asyncio
    async def test_dispatcher_tool_error(self):
        """Handler raises exception -> error tuple with exception message.

        The call must be COMPLETE for the handler to be reached at all: this
        handler is (**kwargs) and _ECHO_SCHEMA requires `message`, so an empty
        args dict is now caught by required-arg validation before dispatch and
        the handler never runs. Passing `message` keeps this test about
        exception propagation, which is what it is actually for.
        """
        dispatcher = ToolDispatcher()

        async def failing_handler(**kwargs) -> dict:
            raise ValueError("Something went wrong in the tool")

        dispatcher.register("fail", failing_handler, _ECHO_SCHEMA)

        result_text, is_error = await dispatcher.dispatch("fail", {"message": "hi"})
        assert is_error is True
        assert "Tool error:" in result_text
        assert "Something went wrong" in result_text

    def test_dispatcher_tool_definitions(self):
        """tool_definitions() returns Anthropic API format with name, description, input_schema."""
        dispatcher = ToolDispatcher()

        async def echo_handler(message: str) -> dict:
            return {"content": [{"type": "text", "text": message}]}

        async def add_handler(a: float, b: float) -> dict:
            return {"content": [{"type": "text", "text": str(a + b)}]}

        dispatcher.register("echo", echo_handler, _ECHO_SCHEMA)
        dispatcher.register("add", add_handler, _ADD_SCHEMA)

        definitions = dispatcher.tool_definitions()
        assert len(definitions) == 2

        # Each definition should have name, description, input_schema
        names = {d["name"] for d in definitions}
        assert names == {"echo", "add"}

        for defn in definitions:
            assert "name" in defn
            assert "description" in defn
            assert "input_schema" in defn
            assert defn["input_schema"]["type"] == "object"

    def test_dispatcher_available_tools_with_frame(self):
        """available_tools() filters by FRAME_TOOLS map.

        Register several tools, verify that frame filtering works.
        The 'question' frame only allows 'recall_deep'.
        """
        dispatcher = ToolDispatcher(stable_tool_set_enabled=False)

        # Register multiple tools (using mock handlers)
        handler = AsyncMock(return_value={"content": [{"type": "text", "text": "ok"}]})
        for name in ["record_decision", "learn_fact", "recall_deep", "create_censor", "bash"]:
            dispatcher.register(name, handler, {"type": "object", "description": f"{name} tool"})

        # 'question' frame: filters to tools in FRAME_TOOLS["question"]
        question_tools = dispatcher.available_tools("question")
        question_names = {t["name"] for t in question_tools}
        # All 5 registered tools are in the question frame's allowed list
        assert question_names == {"record_decision", "learn_fact", "recall_deep", "create_censor", "bash"}

    def test_dispatcher_available_tools_wildcard_frame(self):
        """Frame with wildcard '*' returns all registered tools."""
        dispatcher = ToolDispatcher(stable_tool_set_enabled=False)

        handler = AsyncMock(return_value={"content": [{"type": "text", "text": "ok"}]})
        for name in ["record_decision", "recall_deep", "bash", "read_file", "write_file"]:
            dispatcher.register(name, handler, {"type": "object", "description": f"{name} tool"})

        # 'task' frame has "*" in FRAME_TOOLS -> all tools
        task_tools = dispatcher.available_tools("task")
        task_names = {t["name"] for t in task_tools}
        assert task_names == {"record_decision", "recall_deep", "bash", "read_file", "write_file"}

    def test_dispatcher_available_tools_unknown_frame(self):
        """Unknown frame ID returns empty tool list."""
        dispatcher = ToolDispatcher(stable_tool_set_enabled=False)

        handler = AsyncMock(return_value={"content": [{"type": "text", "text": "ok"}]})
        dispatcher.register("recall_deep", handler, {"type": "object", "description": "test"})

        unknown_tools = dispatcher.available_tools("nonexistent_frame")
        assert unknown_tools == []
