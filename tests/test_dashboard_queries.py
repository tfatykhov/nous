"""Tests for F021 dashboard query functions.

Each test validates both empty-state (no data) and with-data scenarios
against a real Postgres database via the session fixture.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import text

from nous.api.dashboard_queries import (
    get_activity_data,
    get_calibration_data,
    get_dashboard_stats,
    get_graph_data,
    get_health_data,
)

AGENT_ID = "test-dashboard"


@pytest_asyncio.fixture(autouse=True)
async def _ensure_agent(session):
    """Ensure the test agent exists."""
    await session.execute(
        text("""
            INSERT INTO nous_system.agents (id, name)
            VALUES (:id, :name)
            ON CONFLICT (id) DO NOTHING
        """),
        {"id": AGENT_ID, "name": "Dashboard Test Agent"},
    )


# ── Helpers ──────────────────────────────────────────────────────────────


async def _insert_decision(session, *, category="architecture", stakes="low",
                           confidence=0.8, outcome="success", days_ago=0):
    """Insert a test decision and return its id."""
    did = uuid.uuid4()
    created = datetime.now(timezone.utc) - timedelta(days=days_ago)
    await session.execute(
        text("""
            INSERT INTO brain.decisions (id, agent_id, description, confidence,
                                         category, stakes, outcome, created_at)
            VALUES (:id, :agent_id, :desc, :conf, :cat, :stakes, :outcome, :created)
        """),
        {
            "id": did, "agent_id": AGENT_ID, "desc": f"Test decision {did}",
            "conf": confidence, "cat": category, "stakes": stakes,
            "outcome": outcome, "created": created,
        },
    )
    return did


async def _insert_fact(session, *, category="preference", days_ago=0):
    """Insert a test fact and return its id."""
    fid = uuid.uuid4()
    created = datetime.now(timezone.utc) - timedelta(days=days_ago)
    await session.execute(
        text("""
            INSERT INTO heart.facts (id, agent_id, content, category, created_at)
            VALUES (:id, :agent_id, :content, :cat, :created)
        """),
        {
            "id": fid, "agent_id": AGENT_ID,
            "content": f"Test fact {fid}", "cat": category, "created": created,
        },
    )
    return fid


async def _insert_episode(session, *, frame="task", days_ago=0):
    """Insert a test episode and return its id."""
    eid = uuid.uuid4()
    created = datetime.now(timezone.utc) - timedelta(days=days_ago)
    await session.execute(
        text("""
            INSERT INTO heart.episodes (id, agent_id, summary, frame_used, created_at, started_at)
            VALUES (:id, :agent_id, :summary, :frame, :created, :created)
        """),
        {
            "id": eid, "agent_id": AGENT_ID,
            "summary": f"Test episode {eid}", "frame": frame, "created": created,
        },
    )
    return eid


async def _insert_edge(session, source_id, target_id, *,
                       source_type="decision", target_type="fact",
                       relation="related_to", days_ago=0):
    """Insert a graph edge."""
    created = datetime.now(timezone.utc) - timedelta(days=days_ago)
    await session.execute(
        text("""
            INSERT INTO brain.graph_edges
                (id, agent_id, source_id, target_id, source_type, target_type, relation, created_at)
            VALUES (:id, :agent_id, :src, :tgt, :stype, :ttype, :rel, :created)
        """),
        {
            "id": uuid.uuid4(), "agent_id": AGENT_ID,
            "src": source_id, "tgt": target_id,
            "stype": source_type, "ttype": target_type,
            "rel": relation, "created": created,
        },
    )


async def _insert_event(session, event_type="turn_completed", days_ago=0):
    """Insert an event."""
    created = datetime.now(timezone.utc) - timedelta(days=days_ago)
    await session.execute(
        text("""
            INSERT INTO nous_system.events (id, agent_id, event_type, data, created_at)
            VALUES (:id, :agent_id, :etype, '{}', :created)
        """),
        {
            "id": uuid.uuid4(), "agent_id": AGENT_ID,
            "etype": event_type, "created": created,
        },
    )


async def _insert_reason(session, decision_id, reason_type="analysis"):
    """Insert a decision reason."""
    await session.execute(
        text("""
            INSERT INTO brain.decision_reasons (id, decision_id, type, text)
            VALUES (:id, :did, :type, :text)
        """),
        {
            "id": uuid.uuid4(), "did": decision_id,
            "type": reason_type, "text": f"Test reason for {decision_id}",
        },
    )


# ── Task 5: get_dashboard_stats ─────────────────────────────────────────


class TestGetDashboardStats:
    @pytest.mark.asyncio
    async def test_empty_state(self, session):
        data = await get_dashboard_stats(session, AGENT_ID)
        assert "deltas" in data
        assert "distributions" in data
        assert "timeseries" in data
        assert "graph_density" in data
        assert data["graph_density"] == 0.0
        for key in ("decisions", "facts", "episodes", "procedures"):
            assert data["deltas"][key]["total"] == 0
            assert data["deltas"][key]["last_7_days"] == 0

    @pytest.mark.asyncio
    async def test_with_data(self, session):
        # Insert some data
        await _insert_decision(session, days_ago=1)
        await _insert_decision(session, days_ago=10)
        await _insert_fact(session, days_ago=2)
        await _insert_episode(session, days_ago=3)

        data = await get_dashboard_stats(session, AGENT_ID)
        assert data["deltas"]["decisions"]["total"] == 2
        assert data["deltas"]["decisions"]["last_7_days"] == 1
        assert data["deltas"]["facts"]["total"] == 1
        assert data["deltas"]["facts"]["last_7_days"] == 1
        assert data["distributions"]["decision_outcomes"]["success"] == 2
        assert len(data["timeseries"]["decisions"]) == 31  # 30 days + today


# ── Task 6: get_graph_data ──────────────────────────────────────────────


class TestGetGraphData:
    @pytest.mark.asyncio
    async def test_empty_state(self, session):
        data = await get_graph_data(session, AGENT_ID)
        assert data["nodes"] == []
        assert data["edges"] == []
        assert data["stats"]["total_edges"] == 0

    @pytest.mark.asyncio
    async def test_with_edges(self, session):
        d1 = await _insert_decision(session)
        f1 = await _insert_fact(session)
        await _insert_edge(session, d1, f1, source_type="decision", target_type="fact")

        data = await get_graph_data(session, AGENT_ID)
        assert len(data["edges"]) == 1
        assert data["edges"][0]["relation"] == "related_to"
        assert len(data["nodes"]) == 2
        node_types = {n["type"] for n in data["nodes"]}
        assert "decision" in node_types
        assert "fact" in node_types
        assert data["stats"]["total_edges"] == 1

    @pytest.mark.asyncio
    async def test_orphan_counts(self, session):
        # Create orphan nodes (not connected by edges)
        await _insert_decision(session)
        await _insert_fact(session)

        data = await get_graph_data(session, AGENT_ID)
        assert data["stats"]["orphan_counts"]["decisions"] >= 1
        assert data["stats"]["orphan_counts"]["facts"] >= 1

    @pytest.mark.asyncio
    async def test_edge_limit(self, session):
        """Edges are bounded by limit * 4."""
        d1 = await _insert_decision(session)
        for _ in range(10):
            f = await _insert_fact(session)
            await _insert_edge(session, d1, f)

        data = await get_graph_data(session, AGENT_ID, limit=2)
        # limit=2, max_edges=8
        assert data["stats"]["displayed_edges"] == 8
        assert data["stats"]["total_edges"] == 10


# ── Task 7: get_calibration_data ────────────────────────────────────────


class TestGetCalibrationData:
    @pytest.mark.asyncio
    async def test_empty_state(self, session):
        data = await get_calibration_data(session, AGENT_ID)
        assert data["calibration_curve"] == []
        assert data["confidence_histogram"] == []
        assert data["outcome_by_category"] == {}
        assert data["outcome_by_stakes"] == {}
        assert data["reason_stats"] == []
        assert data["brier_history"] == []
        assert len(data["daily_decisions"]) == 31

    @pytest.mark.asyncio
    async def test_with_decisions(self, session):
        d1 = await _insert_decision(session, confidence=0.8, outcome="success",
                                     category="architecture", stakes="high")
        d2 = await _insert_decision(session, confidence=0.6, outcome="failure",
                                     category="tooling", stakes="low")
        await _insert_reason(session, d1, "analysis")
        await _insert_reason(session, d2, "pattern")

        data = await get_calibration_data(session, AGENT_ID)
        assert len(data["calibration_curve"]) >= 1
        assert len(data["confidence_histogram"]) >= 1
        assert "architecture" in data["outcome_by_category"]
        assert "high" in data["outcome_by_stakes"]
        assert len(data["reason_stats"]) == 2


# ── Task 8: get_activity_data ───────────────────────────────────────────


class TestGetActivityData:
    @pytest.mark.asyncio
    async def test_empty_state(self, session):
        data = await get_activity_data(session, AGENT_ID)
        assert data["timeline"] == {}
        assert data["event_totals"] == {}
        assert data["censor_stats"]["total"] == 0
        assert data["schedule_stats"]["total"] == 0
        assert data["sleep_stats"]["total_sleeps"] == 0

    @pytest.mark.asyncio
    async def test_with_events(self, session):
        await _insert_event(session, "turn_completed", days_ago=1)
        await _insert_event(session, "turn_completed", days_ago=1)
        await _insert_event(session, "sleep_started", days_ago=2)

        data = await get_activity_data(session, AGENT_ID)
        assert data["event_totals"]["turn_completed"] == 2
        assert data["event_totals"]["sleep_started"] == 1
        assert data["sleep_stats"]["total_sleeps"] == 1
        assert data["sleep_stats"]["last_sleep_at"] is not None
        assert len(data["timeline"]) >= 1


# ── Task 9: get_health_data ─────────────────────────────────────────────


class TestGetHealthData:
    @pytest.mark.asyncio
    async def test_empty_state(self, session):
        data = await get_health_data(session, AGENT_ID)
        assert data["density"] == 0.0
        assert data["total_edges"] == 0
        assert data["connected_nodes"] == 0
        assert data["total_orphans"] == 0
        assert data["degree_distribution"] == []
        assert len(data["daily_edges"]) == 31

    @pytest.mark.asyncio
    async def test_with_edges(self, session):
        d1 = await _insert_decision(session)
        d2 = await _insert_decision(session)
        f1 = await _insert_fact(session)
        await _insert_edge(session, d1, f1)
        await _insert_edge(session, d2, f1, source_type="decision", target_type="fact",
                           relation="supports")

        data = await get_health_data(session, AGENT_ID)
        assert data["total_edges"] == 2
        assert data["connected_nodes"] == 3
        assert data["density"] > 0.0
        assert len(data["degree_distribution"]) >= 1
        # d1 and d2 each have degree 1, f1 has degree 2
        degrees = {d["degree"]: d["count"] for d in data["degree_distribution"]}
        assert degrees.get(1, 0) >= 2  # d1, d2
        assert degrees.get(2, 0) >= 1  # f1

    @pytest.mark.asyncio
    async def test_orphan_counts(self, session):
        # Create nodes without edges
        await _insert_decision(session)
        await _insert_fact(session)
        await _insert_episode(session)

        data = await get_health_data(session, AGENT_ID)
        assert data["orphan_counts"]["decisions"] >= 1
        assert data["orphan_counts"]["facts"] >= 1
        assert data["orphan_counts"]["episodes"] >= 1
        assert data["total_orphans"] >= 3
