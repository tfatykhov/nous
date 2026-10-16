"""GET /dashboard/autonomy data (autonomy plan v2, A3): taps, grades, task scores, inventory, warn classes."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio

from nous.api import autonomy_dashboard as dash
from nous.observability.task_scoring import SCORER_VERSION
from nous.storage.models import (
    A2uiAction,
    A2uiSurface,
    Censor,
    Decision,
    Event,
    ExecutionLedgerEntry,
    Subtask,
    TaskScore,
)

NOW = datetime.now(UTC)
SHAPE = {
    "window_days",
    "taps",
    "triage_taps",
    "approval_cards_asked",
    "decision_grades",
    "task_scores",
    "inventory",
    "events_persisted",
    "warn_classes",
}


@pytest_asyncio.fixture
async def agent() -> str:
    return f"test-autonomy-{uuid.uuid4().hex[:8]}"


async def add(db, *rows) -> None:
    async with db.session() as s:
        s.add_all(rows)
        await s.commit()


async def data(db, agent, **kw):
    async with db.session() as s:
        return await dash.get_autonomy_data(s, agent, now=NOW, **kw)


def tap(agent, name, actor="unattributed", age_days=1) -> A2uiAction:
    return A2uiAction(
        agent_id=agent, surface_id="s", action_name=name, actor=actor, created_at=NOW - timedelta(days=age_days)
    )


def surface(agent, kind, age_days=1) -> A2uiSurface:
    return A2uiSurface(
        surface_id=f"s-{uuid.uuid4().hex[:8]}",
        agent_id=agent,
        origin="agent",
        kind=kind,
        catalog_id="c",
        title="t",
        components=[],
        nonce="n",
        created_at=NOW - timedelta(days=age_days),
    )


def score_row(
    agent,
    kind="subtask",
    value=0.0,
    *,
    outcome="done_self",
    settled=False,
    overrides=0,
    age_days=1,
    first=None,
    scored=None,
    version=SCORER_VERSION,
    **kw,
) -> TaskScore:
    return TaskScore(
        agent_id=agent,
        task_kind=kind,
        task_id=str(uuid.uuid4()),
        finished_at=NOW - timedelta(days=age_days),
        scorer_version=version,
        outcome=outcome,
        owner_overrides=overrides,
        score=value,
        settled=settled,
        first_scored_at=first or NOW - timedelta(days=20),
        scored_at=scored or NOW - timedelta(minutes=5),
        **kw,
    )


def done(agent, age_days=1, status="completed") -> Subtask:
    return Subtask(
        id=uuid.uuid4(), agent_id=agent, task="t", status=status, completed_at=NOW - timedelta(days=age_days)
    )


@pytest.mark.parametrize(("raw", "days"), [(None, 7), ("", 7), ("1", 1), ("7", 7), ("90", 90)])
def test_days_parses(raw, days):
    assert dash.parse_days(raw) == days


@pytest.mark.parametrize("raw", ["0", "91", "-3", "abc", "7.5", "1e1", "٣"])
def test_a_bad_days_is_a_value_error(raw):
    with pytest.raises(ValueError, match="days must be a whole number from 1 to 90"):
        dash.parse_days(raw)


@pytest.mark.parametrize(
    ("values", "q", "expected"),
    [
        ([], 0.5, None),
        ([4.0], 0.1, 4.0),
        ([1.0, 3.0], 0.5, 2.0),
        ([0.0, 1.0, 2.0, 3.0, 4.0], 0.9, 3.6),
        ([0.0, 1.0, 2.0, 3.0, 4.0], 0.5, 2.0),
        ([0.0, 10.0], 0.1, 1.0),
        ([1.0, 2.0, 3.0], 1.0, 3.0),
    ],
)
def test_percentile(values, q, expected):
    result = dash.percentile(values, q)
    assert result is None if expected is None else result == pytest.approx(expected)


async def test_taps_are_the_owners_in_the_window_grouped_by_action(db, agent):
    await add(
        db,
        tap(agent, "decision.resolve"),
        tap(agent, "decision.resolve"),
        tap(agent, "heartbeat.acknowledge"),
        tap(agent, "heartbeat.resolve"),
        tap(agent, "heartbeat.resolve"),
        tap(agent, "app.refresh"),
        tap(agent, "approval.choose"),
        tap(agent, "heartbeat.resolve", actor="system:expiry"),  # an expiry is not a tap
        tap(agent, "heartbeat.resolve", age_days=30),  # outside the window
        tap("someone-else", "heartbeat.resolve"),
    )
    got = await data(db, agent)
    assert got["window_days"] == 7
    assert got["taps"]["total"] == 7
    assert got["taps"]["by_action"][:2] == [
        {"action_name": "decision.resolve", "count": 2},
        {"action_name": "heartbeat.resolve", "count": 2},
    ]
    assert got["triage_taps"] == {"heartbeat": 3, "decision": 2}
    assert (await data(db, agent, days=60))["taps"]["total"] == 8


async def test_approval_cards_asked_counts_only_approval_surfaces_in_the_window(db, agent):
    await add(
        db,
        surface(agent, "approval_gate"),
        surface(agent, "approval_gate"),
        surface(agent, "approval_gate", age_days=20),
        surface(agent, "decision_review"),
        surface("someone-else", "approval_gate"),
    )
    assert (await data(db, agent))["approval_cards_asked"] == 2
    assert (await data(db, agent, days=30))["approval_cards_asked"] == 3


async def test_decision_grades_by_reviewer_and_outcome(db, agent):
    def decision(outcome, reviewer, age_days=1):
        return Decision(
            agent_id=agent,
            description="d",
            confidence=0.5,
            category="process",
            stakes="low",
            outcome=outcome,
            reviewer=reviewer,
            reviewed_at=NOW - timedelta(days=age_days),
        )

    await add(
        db,
        decision("success", "a2ui"),
        decision("success", "a2ui"),
        decision("failure", "a2ui"),
        decision("success", "auto"),
        decision("success", "a2ui", age_days=30),
        Decision(agent_id=agent, description="d", confidence=0.5, category="process", stakes="low"),  # never reviewed
    )
    assert (await data(db, agent))["decision_grades"] == [
        {"reviewer": "a2ui", "outcome": "failure", "count": 1},
        {"reviewer": "a2ui", "outcome": "success", "count": 2},
        {"reviewer": "auto", "outcome": "success", "count": 1},
    ]


async def test_subtask_scores_are_split_into_provisional_and_settled(db, agent):
    await add(
        db,
        *[score_row(agent, value=v) for v in (1.0, 0.5, 0.0, -1.0)],
        score_row(agent, value=1.0, overrides=2),
        score_row(agent, value=-0.5, settled=True, age_days=14.5, outcome="failed"),
        score_row(agent, value=0.0, version=SCORER_VERSION + 1),  # another version is not this scoreboard's
        score_row(agent, value=0.0, age_days=20),  # outside the window
        score_row("someone-else", value=0.0),
    )
    s = (await data(db, agent, days=15))["task_scores"]["subtasks"]
    assert (s["provisional"]["count"], s["provisional"]["mean"], s["provisional"]["p10"]) == (5, 0.3, -0.6)
    assert (s["provisional"]["p50"], s["provisional"]["p90"], s["provisional"]["overrides"]) == (0.5, 1.0, 2)
    assert s["settled"] == {"count": 1, "mean": -0.5, "p10": -0.5, "p50": -0.5, "p90": -0.5, "overrides": 0}
    assert s["by_outcome"]["failed"] == 1 and sum(s["by_outcome"].values()) == 6
    assert (await data(db, agent, days=7))["task_scores"]["subtasks"]["settled"]["count"] == 0


async def test_rollups_are_counted_apart_and_never_in_a_score_sum(db, agent):
    await add(
        db,
        score_row(agent, value=1.0, outcome="done_verified"),
        score_row(agent, "dag", None, outcome="done_self", harness_errors=2, refusals=1),
        score_row(agent, "dag", None, outcome="failed"),
        score_row(agent, "intention", None, outcome="not_scored", not_scored_reason="cancelled", overrides=1),
    )
    ts = (await data(db, agent))["task_scores"]
    assert ts["subtasks"]["provisional"]["count"] == 1 and ts["subtasks"]["provisional"]["mean"] == 1.0
    dag = ts["rollups"]["dag"]
    assert (dag["count"], dag["by_outcome"]["done_self"], dag["by_outcome"]["failed"]) == (2, 1, 1)
    assert dag["own_penalties"] == {"harness_errors": 2, "refusals": 1, "unverified_claims": 0, "owner_overrides": 0}
    chain = ts["rollups"]["intention"]
    assert (chain["count"], chain["by_outcome"]["not_scored"], chain["own_penalties"]["owner_overrides"]) == (1, 1, 1)


async def test_coverage_counts_leaves_that_finished_after_the_first_run(db, agent):
    first_run = NOW - timedelta(days=3)
    await add(
        db,
        done(agent, 1),
        done(agent, 2),
        done(agent, 2.5, status="cancelled"),
        done(agent, 2.9),
        done(agent, 5),
        done(agent, 6),  # finished before scoring began: neither expected nor counted
        done(agent, 1, status="running"),
    )
    await add(
        db,
        score_row(agent, age_days=1, first=first_run),
        score_row(agent, age_days=2, first=first_run),
        score_row(
            agent, age_days=2.5, first=first_run, outcome="not_scored", value=None, not_scored_reason="cancelled"
        ),
        score_row(agent, age_days=5, first=first_run),  # scored although earlier than the floor: not counted
    )
    s = (await data(db, agent))["task_scores"]["subtasks"]
    assert (s["finished"], s["covered"], s["coverage"]) == (4, 3, 0.75)  # one finished leaf was missed
    assert s["by_outcome"]["not_scored"] == 1


async def test_a_scorer_that_has_never_run_has_no_coverage_and_no_dates(db, agent):
    await add(db, done(agent, 1))
    ts = (await data(db, agent))["task_scores"]
    assert (ts["first_scored_at"], ts["last_scored_at"]) == (None, None)
    assert (ts["subtasks"]["finished"], ts["subtasks"]["covered"], ts["subtasks"]["coverage"]) == (None, None, None)


async def test_last_scored_at_shows_a_dead_cron(db, agent):
    stale = NOW - timedelta(hours=7)
    await add(db, score_row(agent, scored=stale, first=NOW - timedelta(days=9)))
    ts = (await data(db, agent))["task_scores"]
    assert abs(datetime.fromisoformat(ts["last_scored_at"]) - stale) < timedelta(seconds=1)
    assert abs(datetime.fromisoformat(ts["first_scored_at"]) - (NOW - timedelta(days=9))) < timedelta(seconds=1)


async def test_the_inventory_and_censors_are_part_of_the_scoreboard(db, agent):
    await add(
        db,
        ExecutionLedgerEntry(
            agent_id=agent,
            context_kind="scheduled",
            tool_name="send_email",
            side_effect_type="external",
            status="success",
            created_at=NOW - timedelta(days=20),
        ),
        Censor(agent_id=agent, trigger_pattern="p", reason="r", provenance="agent", created_at=NOW - timedelta(days=2)),
    )
    inventory = (await data(db, agent))["inventory"]  # 20 days ago is outside "days=7" but inside the inventory's 30
    assert inventory["days"] == 30
    assert inventory["tools"][0]["tool_name"] == "send_email"
    assert inventory["tools"][0]["de_facto_level"] == "acts_without_asking"
    assert inventory["censors"]["by_provenance"]["agent"] == {"created": 1, "deactivated": 0}


async def test_warn_classes_group_by_mode_context_violation_and_tool(db, agent):
    def policy(mode, context, violation, tool, age_days=1, who=agent):
        return Event(
            agent_id=who,
            event_type="harness_context_policy_violation",
            created_at=NOW - timedelta(days=age_days),
            data={"mode": mode, "context_kind": context, "violation": violation, "tool_name": tool},
        )

    await add(
        db,
        *[policy("warn", "heartbeat_triage", "level:external", "bash") for _ in range(3)],
        policy("warn", "scheduled", "spawn", "dag_create"),
        policy("enforce", "heartbeat_triage", "level:external", "bash"),
        policy("warn", "scheduled", "spawn", "dag_create", age_days=30),
        policy("warn", "scheduled", "spawn", "dag_create", who="someone-else"),
        Event(agent_id=agent, event_type="harness_unoffered_tool_call", data={"mode": "warn", "tool_name": "x"}),
    )
    got = await data(db, agent)
    assert got["events_persisted"] is True
    assert got["warn_classes"] == [
        {
            "mode": "warn",
            "context_kind": "heartbeat_triage",
            "violation": "level:external",
            "tool_name": "bash",
            "count": 3,
        },
        {
            "mode": "enforce",
            "context_kind": "heartbeat_triage",
            "violation": "level:external",
            "tool_name": "bash",
            "count": 1,
        },
        {"mode": "warn", "context_kind": "scheduled", "violation": "spawn", "tool_name": "dag_create", "count": 1},
    ]


async def test_with_event_persistence_off_there_is_no_warn_list_to_misread(db, agent):
    await add(
        db,
        Event(
            agent_id=agent,
            event_type="harness_context_policy_violation",
            data={"mode": "warn", "context_kind": "scheduled", "violation": "spawn", "tool_name": "dag_create"},
        ),
    )
    got = await data(db, agent, events_persisted=False)
    assert got["events_persisted"] is False and got["warn_classes"] == []


async def test_the_shape_with_nothing_recorded(db, agent):
    got = await data(db, agent)
    assert set(got) == SHAPE
    assert got["taps"] == {"total": 0, "by_action": []}
    assert got["triage_taps"] == {"heartbeat": 0, "decision": 0}
    assert got["inventory"]["tools"] == [] and set(got["inventory"]["censors"]["by_provenance"]) == {
        "auto",
        "agent",
        "human",
    }
    assert set(got["task_scores"]) == {
        "scorer_version",
        "first_scored_at",
        "last_scored_at",
        "headline",
        "subtasks",
        "rollups",
    }
    assert set(got["task_scores"]["rollups"]) == {"dag", "intention"}


async def test_the_headline_shows_verified_share_overrides_and_penalties_side_by_side(db, agent):
    await add(
        db,
        score_row(agent, value=1.0, outcome="done_verified"),
        score_row(agent, value=0.0, outcome="done_self"),
        score_row(agent, value=0.0, outcome="done_self", overrides=1, harness_errors=2),
        score_row(agent, value=1.0, outcome="done_verified", settled=True),
        score_row(agent, outcome="not_scored", value=None, not_scored_reason="cancelled"),  # not a scored leaf
        score_row(agent, "dag", None, outcome="done_self", harness_errors=3, refusals=1),
        score_row(agent, "intention", None, outcome="done_self", overrides=2, unverified_claims=1),
    )
    h = (await data(db, agent, days=15))["task_scores"]["headline"]
    assert h["verified_share"]["provisional"] == {"scored": 3, "verified": 1, "share": 0.3333}
    assert h["verified_share"]["settled"] == {"scored": 1, "verified": 1, "share": 1.0}
    # an override on a roll-up changes no subtask score, so it is counted over all units
    assert h["owner_overrides"] == {"total": 3, "subtasks": 1, "rollups": 2}
    assert h["harness_penalties"] == {"harness_errors": 5, "refusals": 1, "unverified_claims": 1}


async def test_the_headline_with_nothing_scored(db, agent):
    h = (await data(db, agent))["task_scores"]["headline"]
    assert h["verified_share"]["provisional"] == {"scored": 0, "verified": 0, "share": None}
    assert h["owner_overrides"] == {"total": 0, "subtasks": 0, "rollups": 0}
