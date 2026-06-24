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
    get_node_detail,
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


async def _insert_fact(session, *, category="preference", days_ago=0, active=True,
                       content=None):
    """Insert a test fact and return its id."""
    fid = uuid.uuid4()
    created = datetime.now(timezone.utc) - timedelta(days=days_ago)
    await session.execute(
        text("""
            INSERT INTO heart.facts (id, agent_id, content, category, active, created_at)
            VALUES (:id, :agent_id, :content, :cat, :active, :created)
        """),
        {
            "id": fid, "agent_id": AGENT_ID,
            "content": content or f"Test fact {fid}", "cat": category,
            "active": active, "created": created,
        },
    )
    return fid


async def _insert_episode(session, *, frame="task", days_ago=0, active=True):
    """Insert a test episode and return its id."""
    eid = uuid.uuid4()
    created = datetime.now(timezone.utc) - timedelta(days=days_ago)
    await session.execute(
        text("""
            INSERT INTO heart.episodes (id, agent_id, summary, frame_used, active, created_at, started_at)
            VALUES (:id, :agent_id, :summary, :frame, :active, :created, :created)
        """),
        {
            "id": eid, "agent_id": AGENT_ID,
            "summary": f"Test episode {eid}", "frame": frame,
            "active": active, "created": created,
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


async def _insert_event(session, event_type="turn_completed", days_ago=0, data=None):
    """Insert an event."""
    import json as _json
    created = datetime.now(timezone.utc) - timedelta(days=days_ago)
    await session.execute(
        text("""
            INSERT INTO nous_system.events (id, agent_id, event_type, data, created_at)
            VALUES (:id, :agent_id, :etype, CAST(:data AS jsonb), :created)
        """),
        {
            "id": uuid.uuid4(), "agent_id": AGENT_ID,
            "etype": event_type, "data": _json.dumps(data or {}),
            "created": created,
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


# ── Orphan metric excludes soft-deleted (inactive) nodes ────────────────


@pytest.mark.postgres_only
class TestOrphanActiveFilter:
    """Soft-deleted facts/episodes/procedures must not inflate orphan counts."""

    @pytest.mark.asyncio
    async def test_graph_orphans_exclude_inactive_facts(self, session):
        await _insert_fact(session, active=True)   # active orphan -> counts
        await _insert_fact(session, active=False)  # inactive orphan -> excluded

        data = await get_graph_data(session, AGENT_ID)
        assert data["stats"]["orphan_counts"]["facts"] == 1

    @pytest.mark.asyncio
    async def test_health_orphans_exclude_inactive_episodes(self, session):
        await _insert_episode(session, active=True)
        await _insert_episode(session, active=False)

        data = await get_health_data(session, AGENT_ID)
        # Only the active orphan episode counts.
        assert data["orphan_counts"]["episodes"] == 1

    @pytest.mark.asyncio
    async def test_density_excludes_inactive_from_total_and_orphans(self, session):
        # An inactive episode must drop out of BOTH the total and orphan counts,
        # keeping orphan_rate honest. (This is the bug: density used 1=1 for episodes.)
        await _insert_episode(session, active=True)
        await _insert_episode(session, active=False)

        from nous.api.dashboard_queries import get_density_data
        data = await get_density_data(session, AGENT_ID)
        ep = data["density_by_type"]["episode"]
        assert ep["total"] == 1
        assert ep["orphan"] == 1

    @pytest.mark.asyncio
    async def test_decisions_unaffected_no_active_column(self, session):
        # brain.decisions has no `active` column — all decisions still count.
        await _insert_decision(session)
        await _insert_decision(session)
        data = await get_graph_data(session, AGENT_ID)
        assert data["stats"]["orphan_counts"]["decisions"] == 2

    @pytest.mark.asyncio
    async def test_health_trend_last_point_matches_total_orphans(self, session):
        # The trend's `connected` set must exclude inactive endpoints too, or an
        # inactive episode WITH an edge deflates orphan_count below the
        # point-in-time total. Invariant: orphan_trend[-1].count == total_orphans.
        await _insert_fact(session, active=True)  # active orphan -> counts
        d1 = await _insert_decision(session)
        e_inactive = await _insert_episode(session, active=False)
        # Edge to the inactive episode: endpoint must NOT be counted as connected.
        await _insert_edge(session, d1, e_inactive,
                           source_type="decision", target_type="episode")

        data = await get_health_data(session, AGENT_ID)
        assert data["orphan_trend"][-1]["count"] == data["total_orphans"]


# ── get_node_detail (GET /dashboard/graph/node/{id}) ────────────────────


@pytest.mark.postgres_only
class TestGetNodeDetail:
    @pytest.mark.asyncio
    async def test_unknown_type_returns_not_found(self, session):
        data = await get_node_detail(session, AGENT_ID, str(uuid.uuid4()), "bogus")
        assert data == {"found": False}

    @pytest.mark.asyncio
    async def test_missing_node_returns_not_found(self, session):
        data = await get_node_detail(session, AGENT_ID, str(uuid.uuid4()), "fact")
        assert data == {"found": False}

    @pytest.mark.asyncio
    async def test_node_with_connection(self, session):
        d1 = await _insert_decision(session)
        f1 = await _insert_fact(session, content="A long fact body that should not be truncated in the node payload.")
        await _insert_edge(session, d1, f1, source_type="decision", target_type="fact",
                           relation="related_to")

        data = await get_node_detail(session, AGENT_ID, str(d1), "decision")
        assert data["found"] is True
        assert data["node"]["type"] == "decision"
        assert data["node"]["content"]  # full content present
        assert data["connection_count"] == 1
        c = data["connections"][0]
        assert c["neighbor_id"] == str(f1)
        assert c["neighbor_type"] == "fact"
        assert c["relation"] == "related_to"
        assert c["direction"] == "out"
        assert c["neighbor_active"] is True
        assert c["neighbor_label"]  # neighbor label hydrated

    @pytest.mark.asyncio
    async def test_incoming_edge_direction(self, session):
        d1 = await _insert_decision(session)
        f1 = await _insert_fact(session)
        await _insert_edge(session, d1, f1, source_type="decision", target_type="fact")

        # From the fact's perspective the edge is incoming.
        data = await get_node_detail(session, AGENT_ID, str(f1), "fact")
        assert data["connection_count"] == 1
        assert data["connections"][0]["direction"] == "in"
        assert data["connections"][0]["neighbor_id"] == str(d1)

    @pytest.mark.asyncio
    async def test_surfaces_contradicts_edge(self, session):
        # brain.neighbors() hides 'contradicts'; the dashboard detail must show it.
        f1 = await _insert_fact(session)
        f2 = await _insert_fact(session)
        await _insert_edge(session, f1, f2, source_type="fact", target_type="fact",
                           relation="contradicts")

        data = await get_node_detail(session, AGENT_ID, str(f1), "fact")
        relations = {c["relation"] for c in data["connections"]}
        assert "contradicts" in relations

    @pytest.mark.asyncio
    async def test_episode_returns_full_summary_not_just_title(self, session):
        # Node content for an episode must be the summary body, not a label.
        e1 = await _insert_episode(session)  # helper sets summary="Test episode <id>"
        d1 = await _insert_decision(session)
        await _insert_edge(session, d1, e1, source_type="decision", target_type="episode")

        data = await get_node_detail(session, AGENT_ID, str(e1), "episode")
        assert data["found"] is True
        assert data["node"]["content"].startswith("Test episode")

    @pytest.mark.asyncio
    async def test_inactive_neighbor_flagged(self, session):
        d1 = await _insert_decision(session)
        f_inactive = await _insert_fact(session, active=False)
        await _insert_edge(session, d1, f_inactive, source_type="decision",
                           target_type="fact")

        data = await get_node_detail(session, AGENT_ID, str(d1), "decision")
        assert data["connection_count"] == 1
        assert data["connections"][0]["neighbor_active"] is False


# ── Task 7: get_calibration_data ────────────────────────────────────────


class TestGetCalibrationData:
    @pytest.mark.asyncio
    async def test_empty_state(self, session):
        data = await get_calibration_data(session, AGENT_ID)
        assert data["calibration_curve"] == []
        assert data["confidence_histogram"] == []
        assert data["outcome_by_category"] == {}
        assert data["outcome_by_stakes"] == {}
        assert data["reason_type_stats"] == {}
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
        assert len(data["reason_type_stats"]) == 2


# ── Task 8: get_activity_data ───────────────────────────────────────────


async def _insert_censor(session, *, trigger_pattern="test pattern",
                         created_by="manual", activation_count=0, active=True):
    """Insert a test censor and return its id."""
    cid = uuid.uuid4()
    await session.execute(
        text("""
            INSERT INTO heart.censors
                (id, agent_id, trigger_pattern, action, reason, created_by,
                 activation_count, active)
            VALUES (:id, :agent_id, :pattern, 'steer', 'test', :created_by,
                    :act_count, :active)
        """),
        {
            "id": cid, "agent_id": AGENT_ID, "pattern": trigger_pattern,
            "created_by": created_by, "act_count": activation_count,
            "active": active,
        },
    )
    return cid


async def _insert_schedule(session, *, task="test task", active=True,
                           next_fire_at=None):
    """Insert a test schedule and return its id."""
    sid = uuid.uuid4()
    await session.execute(
        text("""
            INSERT INTO heart.schedules
                (id, agent_id, task, schedule_type, active, next_fire_at,
                 fire_at)
            VALUES (:id, :agent_id, :task, 'once', :active, :next_fire,
                    COALESCE(:next_fire, now() + interval '1 day'))
        """),
        {
            "id": sid, "agent_id": AGENT_ID, "task": task,
            "active": active, "next_fire": next_fire_at,
        },
    )
    return sid


class TestGetActivityData:
    @pytest.mark.asyncio
    async def test_empty_state(self, session):
        data = await get_activity_data(session, AGENT_ID)
        assert data["events"] == []
        assert data["censor_stats"]["total"] == 0
        assert data["censor_stats"]["total_activations_7d"] == 0
        assert data["schedule_stats"]["total"] == 0
        assert data["schedule_stats"]["fires_7d"] == 0
        assert data["sleep_stats"]["total_sleeps"] == 0
        assert data["sleep_stats"]["last_sleep"] is None
        assert data["sleep_stats"]["facts_created"] == 0

    @pytest.mark.asyncio
    async def test_with_events(self, session):
        await _insert_event(session, "turn_completed", days_ago=1)
        await _insert_event(session, "turn_completed", days_ago=1)
        await _insert_event(session, "sleep_started", days_ago=2)

        data = await get_activity_data(session, AGENT_ID)
        assert isinstance(data["events"], list)
        assert len(data["events"]) == 3
        # Verify event structure
        evt = data["events"][0]
        assert "type" in evt
        assert "created_at" in evt
        assert "data" in evt

    @pytest.mark.asyncio
    async def test_censor_stats(self, session):
        await _insert_censor(session, created_by="manual", activation_count=5)
        await _insert_censor(session, created_by="auto_failure", activation_count=3)
        await _insert_censor(session, created_by="auto_escalation", activation_count=0,
                             active=False)

        data = await get_activity_data(session, AGENT_ID)
        cs = data["censor_stats"]
        assert cs["total"] == 3
        assert cs["active"] == 2
        assert cs["manual_created"] == 1
        assert cs["auto_created"] == 2

    @pytest.mark.asyncio
    async def test_censor_activations_7d(self, session):
        """total_activations_7d counts censor_triggered events in last 7 days."""
        await _insert_censor(session, activation_count=2)
        # 3 events within 7 days
        for _ in range(3):
            await _insert_event(session, "censor_triggered", days_ago=1,
                                data={"censor_id": str(uuid.uuid4()), "matched_text": "test"})
        # 1 event outside 7-day window
        await _insert_event(session, "censor_triggered", days_ago=10,
                            data={"censor_id": str(uuid.uuid4()), "matched_text": "old"})

        data = await get_activity_data(session, AGENT_ID)
        assert data["censor_stats"]["total_activations_7d"] == 3

    @pytest.mark.asyncio
    async def test_top_censors(self, session):
        # Insert censors with varying activation counts
        for i in range(7):
            await _insert_censor(session, trigger_pattern=f"pattern-{i}",
                                 activation_count=i * 10, created_by="auto_failure")

        data = await get_activity_data(session, AGENT_ID)
        top = data["censor_stats"]["top_censors"]
        # Only censors with activation_count > 0, max 5
        assert len(top) == 5
        # Ordered by activation_count descending
        activations = [c["activations"] for c in top]
        assert activations == sorted(activations, reverse=True)
        assert activations[0] == 60  # pattern-6

    @pytest.mark.asyncio
    async def test_next_fires(self, session):
        now = datetime.now(timezone.utc)
        await _insert_schedule(session, task="task-a",
                               next_fire_at=now + timedelta(hours=2))
        await _insert_schedule(session, task="task-b",
                               next_fire_at=now + timedelta(hours=1))
        await _insert_schedule(session, task="task-c", active=False,
                               next_fire_at=now + timedelta(minutes=30))

        data = await get_activity_data(session, AGENT_ID)
        nf = data["schedule_stats"]["next_fires"]
        # Only active schedules with next_fire_at
        assert len(nf) == 2
        # Ordered by next_fire_at ascending
        assert nf[0]["task"] == "task-b"
        assert nf[1]["task"] == "task-a"

    @pytest.mark.asyncio
    async def test_sleep_stats(self, session):
        await _insert_event(session, "sleep_started", days_ago=1)
        await _insert_event(session, "sleep_started", days_ago=3)
        await _insert_event(session, "sleep_completed", days_ago=1, data={
            "phases_completed": ["reflect", "generalize"],
            "facts_created": 5,
            "procedures_created": 2,
            "censors_retired": 0,
        })

        data = await get_activity_data(session, AGENT_ID)
        ss = data["sleep_stats"]
        assert ss["total_sleeps"] == 2
        assert ss["last_sleep"] is not None
        assert ss["facts_created"] == 5
        assert ss["procedures_created"] == 2
        assert ss["censors_retired"] == 0

    @pytest.mark.asyncio
    async def test_hours_param(self, session):
        # Insert event at 2 days ago and 10 days ago
        await _insert_event(session, "turn_completed", days_ago=2)
        await _insert_event(session, "turn_completed", days_ago=10)

        # With 72 hours (3 days), should only see the recent event
        data = await get_activity_data(session, AGENT_ID, hours=72)
        assert len(data["events"]) == 1

        # With default 168 hours (7 days), should see one event
        data = await get_activity_data(session, AGENT_ID, hours=168)
        assert len(data["events"]) == 1

        # With 720 hours (30 days), should see both
        data = await get_activity_data(session, AGENT_ID, hours=720)
        assert len(data["events"]) == 2


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
