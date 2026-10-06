"""F099 Phase 2c-1: the budgets derived from rows, and the deterministic gate (spec 4.5.3, 4.6).

The database tests are ``postgres_only`` (CAST(text AS uuid) joins, FILTER aggregates); the pure ones run on
every lane.
"""

from __future__ import annotations

import inspect
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID

import pytest
from f099_support import (
    CONT,
    add_arrival,
    claim,
    dag_kwargs,
    env_factory,  # noqa: F401
    intention_of,
    make_child,
    make_dag,
    make_root,
    record,
    set_intention,
)
from sqlalchemy import update

from nous.brain import continuation
from nous.heart.result_inbox import record_dag_result
from nous.storage.models import Decision, Subtask


async def _limits(env, root_id):
    async with env.db.session() as s:
        return await continuation.root_limits(s, env.agent, root_id, settings=env.settings)


async def _gate(env, got, plan=None, *, settings=None):
    """``continuation.gate``. The Plan question is the Brain's own read (as 2c-2 binds it) unless a test
    gives one."""
    async with env.db.session() as s:

        async def brain_outcome(decision_id):
            return await continuation.decision_outcome(s, env.agent, decision_id)

        return await continuation.gate(
            s, env.agent, got, settings=settings or env.settings, plan_outcome_of=plan or brain_outcome
        )


async def _claimed(env, root=None):
    """A root (a new one unless given) with a result ready, and its claim."""
    root = root or await make_root(env)
    await record(env, root)
    return root, await claim(env, root.id)


@pytest.mark.postgres_only
async def test_a_quiet_root_has_nothing_to_escalate(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    root, got = await _claimed(env)
    limits = await _limits(env, root.id)
    assert (limits.depth, limits.spawns, limits.turns, limits.tokens, limits.stalls) == (0, 0, 0, 0, 0)
    assert limits.spawn_blocked is False and limits.escalate is None
    assert await _gate(env, got) is None


@pytest.mark.postgres_only
async def test_root_tokens_add_subtasks_dags_and_arrivals(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    root = await make_root(env)
    child = await make_child(env, root)
    for intention, (tin, tout) in ((root, (100, 20)), (child, (30, 5))):
        async with env.db.session() as s:
            await s.execute(
                update(Subtask).where(Subtask.id == UUID(intention.source_id)).values(tokens_in=tin, tokens_out=tout)
            )
            await s.commit()
    dag, store = await make_dag(env, parent=child)
    await store.update_dag_tokens(dag.id, 200)
    await add_arrival(env, root.id, 1, tokens=(10, 5))
    limits = await _limits(env, root.id)
    assert limits.tokens == (100 + 20) + (30 + 5) + 200 + (10 + 5)
    assert (limits.depth, limits.spawns, limits.turns) == (2, 2, 1)  # the DAG is the child's child


@pytest.mark.postgres_only
async def test_the_lineage_of_another_root_is_not_counted(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    root = await make_root(env)
    other = await make_root(env)
    await add_arrival(env, other.id, 1, tokens=(500, 500))
    await make_child(env, other)
    limits = await _limits(env, root.id)
    assert (limits.tokens, limits.spawns, limits.turns) == (0, 0, 0)


@pytest.mark.postgres_only
@pytest.mark.parametrize(("arrivals", "expected"), [(7, None), (8, "budget_turns")])
async def test_the_turn_budget_escalates_at_its_limit(env_factory, arrivals, expected):  # noqa: F811
    env = await env_factory(**CONT)
    root, got = await _claimed(env)
    for n in range(1, arrivals + 1):
        await add_arrival(env, root.id, n, progress=True)
    assert await _gate(env, got) == expected


@pytest.mark.postgres_only
async def test_a_gate_arrival_is_not_a_turn(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    root, got = await _claimed(env)
    for n in range(1, 9):
        await add_arrival(env, root.id, n, progress=None, gate_reason="budget_stall", decision="report")
    assert (await _limits(env, root.id)).turns == 0 and await _gate(env, got) is None


@pytest.mark.postgres_only
@pytest.mark.parametrize(("spent", "expected"), [((600, 399), None), ((600, 400), "budget_tokens")])
async def test_the_token_budget_escalates_at_its_limit(env_factory, spent, expected):  # noqa: F811
    env = await env_factory(**CONT, continuation_max_tokens_per_root=1000)
    root, got = await _claimed(env)
    await add_arrival(env, root.id, 1, progress=True, tokens=spent)
    assert await _gate(env, got) == expected


@pytest.mark.postgres_only
@pytest.mark.parametrize(
    ("progress", "expected"),
    [
        ([False, False], "budget_stall"),
        ([False, True, False], None),  # the run is cut by a verified progress
        ([True, False, False], "budget_stall"),
    ],
)
async def test_the_stall_budget_counts_the_trailing_false_run(env_factory, progress, expected):  # noqa: F811
    env = await env_factory(**CONT)
    root, got = await _claimed(env)
    for n, value in enumerate(progress, start=1):
        await add_arrival(env, root.id, n, progress=value)
    assert await _gate(env, got) == expected


@pytest.mark.postgres_only
async def test_the_stall_run_is_read_up_to_its_setting(env_factory):  # noqa: F811
    """The read is ``continuation_stall_limit`` arrivals, not a fixed count: ``stalls`` saturates there."""
    env = await env_factory(**CONT, continuation_stall_limit=3)
    root, got = await _claimed(env)
    for n in range(1, 6):
        await add_arrival(env, root.id, n, progress=False)
    assert (await _limits(env, root.id)).stalls == 3
    assert await _gate(env, got) == "budget_stall"


@pytest.mark.postgres_only
async def test_gate_arrivals_and_undecided_arrivals_do_not_cut_a_stall(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    root, got = await _claimed(env)
    await add_arrival(env, root.id, 1, progress=False)
    await add_arrival(env, root.id, 2, progress=None, gate_reason="past_deadline", decision="report")
    await add_arrival(env, root.id, 3, progress=None, outcome="failed_report", decision="report")
    await add_arrival(env, root.id, 4, progress=False)
    assert (await _limits(env, root.id)).stalls == 2


@pytest.mark.postgres_only
async def test_the_depth_limit_blocks_spawning_and_escalates(env_factory):  # noqa: F811
    env = await env_factory(**CONT, continuation_max_depth=1)
    root = await make_root(env)
    await make_child(env, root)  # depth 1 == the limit
    root, got = await _claimed(env, root)
    limits = await _limits(env, root.id)
    assert limits.spawn_blocked is True and limits.escalate == "limit_depth"
    assert await _gate(env, got) == "limit_depth"


@pytest.mark.postgres_only
async def test_the_spawn_limit_blocks_spawning_and_escalates(env_factory):  # noqa: F811
    env = await env_factory(**CONT, continuation_max_spawns_per_root=1)
    root = await make_root(env)
    await make_child(env, root)
    root, got = await _claimed(env, root)
    limits = await _limits(env, root.id)
    assert limits.spawn_blocked is True and limits.escalate == "limit_spawns"
    assert await _gate(env, got) == "limit_spawns"


@pytest.mark.postgres_only
async def test_a_root_below_its_limits_may_spawn(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    root = await make_root(env)
    await make_child(env, root)
    limits = await _limits(env, root.id)
    assert limits.spawn_blocked is False and limits.escalate is None


@pytest.mark.postgres_only
@pytest.mark.parametrize(("marker", "expected"), [("root_cancelled_at", "cancelled"), ("root_expired_at", "expired")])
async def test_a_cancelled_or_expired_root_is_dropped(env_factory, marker, expected):  # noqa: F811
    env = await env_factory(**CONT)
    root, got = await _claimed(env)
    await set_intention(env, root.id, **{marker: datetime.now(UTC)})
    assert await _gate(env, got) == expected


@pytest.mark.postgres_only
async def test_a_cancelled_dags_failure_row_is_a_result_unless_its_root_is_cancelled(env_factory):  # noqa: F811
    """Carry-over fact 7: a cancelled DAG's ``continue`` intention arrives with a FAILURE row."""
    env = await env_factory(**CONT)
    dag, _ = await make_dag(env, status="cancelled")
    await record_dag_result(env.heart.result_inbox, env.settings, **dag_kwargs(dag, status="cancelled"))
    root = await intention_of(env, "dag", dag.id)
    got = await claim(env, root.id)
    assert [row.msg_type for row in got.inbox_rows] == ["FAILURE"]
    assert await _gate(env, got) is None  # an open root: the turn sees the failure as a result
    await set_intention(env, root.id, root_cancelled_at=datetime.now(UTC))
    assert await _gate(env, got) == "cancelled"


@pytest.mark.postgres_only
async def test_a_past_deadline_reports_and_a_null_deadline_never_trips(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    legacy, legacy_claim = await _claimed(env)  # a Phase 1 row: no deadline
    assert await _gate(env, legacy_claim) is None
    late = await make_root(env)
    await set_intention(env, late.id, deadline=datetime.now(UTC) - timedelta(minutes=1))
    _, late_claim = await _claimed(env, late)
    assert await _gate(env, late_claim) == "past_deadline"
    soon = await make_root(env)
    await set_intention(env, soon.id, deadline=datetime.now(UTC) + timedelta(hours=1))
    _, soon_claim = await _claimed(env, soon)
    assert await _gate(env, soon_claim) is None


@pytest.mark.postgres_only
async def test_the_deadline_read_is_the_deepest_claimed_intentions(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    root = await make_root(env)
    child = await make_child(env, root)
    await set_intention(env, root.id, deadline=datetime.now(UTC) - timedelta(hours=1))  # expired, but not the deepest
    await set_intention(env, child.id, deadline=datetime.now(UTC) + timedelta(hours=1))
    await record(env, root)
    await record(env, child)
    got = await claim(env, root.id)
    assert got.deepest.id == child.id and await _gate(env, got) is None


@pytest.mark.postgres_only
@pytest.mark.parametrize(
    ("outcome", "expected"),
    [("superseded", "plan_resolved"), ("noise", "plan_resolved"), ("success", None), (None, None)],
)
async def test_a_resolved_plan_decision_drops_the_arrival(env_factory, outcome, expected):  # noqa: F811
    env = await env_factory(**CONT)
    plan = uuid.uuid4()
    root = await make_root(env)
    await set_intention(env, root.id, origin_decision_id=plan)
    _, got = await _claimed(env, root)
    asked: list = []

    async def plan_outcome_of(decision_id):
        asked.append(decision_id)
        return outcome

    assert await _gate(env, got, plan_outcome_of) == expected
    assert asked == [plan]


@pytest.mark.postgres_only
@pytest.mark.parametrize("holder", ["root", "child", "both"])
async def test_the_plan_decision_asked_is_the_roots_then_the_deepests(env_factory, holder):  # noqa: F811
    """Spec 4.5.3's "originating" Plan decision is the root's: a continuation child carries none."""
    env = await env_factory(**CONT)
    root_plan, child_plan = uuid.uuid4(), uuid.uuid4()
    root = await make_root(env)
    child = await make_child(env, root)
    if holder in ("root", "both"):
        await set_intention(env, root.id, origin_decision_id=root_plan)
    if holder in ("child", "both"):
        await set_intention(env, child.id, origin_decision_id=child_plan)
    await record(env, child)
    got = await claim(env, root.id)
    assert got.deepest.id == child.id
    asked: list = []

    async def plan_outcome_of(decision_id):
        asked.append(decision_id)
        return "superseded"

    assert await _gate(env, got, plan_outcome_of) == "plan_resolved"
    assert asked == [child_plan if holder == "child" else root_plan]


@pytest.mark.postgres_only
async def test_no_plan_decision_means_no_question_to_ask(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    _, got = await _claimed(env)

    async def never(decision_id):
        raise AssertionError("asked about a decision the intention does not have")

    assert await _gate(env, got, never) is None


@pytest.mark.postgres_only
async def test_the_gate_checks_in_the_specs_order(env_factory):  # noqa: F811
    env = await env_factory(**CONT, continuation_max_depth=1)
    root = await make_root(env)
    await make_child(env, root)  # limit_depth
    await set_intention(env, root.id, deadline=datetime.now(UTC) - timedelta(minutes=1))  # past_deadline
    _, got = await _claimed(env, root)
    assert await _gate(env, got) == "past_deadline"  # before the limits
    await set_intention(env, root.id, root_cancelled_at=datetime.now(UTC))
    assert await _gate(env, got) == "cancelled"  # before the deadline


@pytest.mark.postgres_only
async def test_the_budgets_and_limits_check_in_order_and_before_the_plan(env_factory):  # noqa: F811  # PIN
    """Every budget, every limit and the Plan decision trip at once; relaxing one setting at a time shows the
    next in line: turns, tokens, stall, depth, spawns, then the Plan decision."""
    env = await env_factory(
        **CONT,
        continuation_max_turns_per_root=2,
        continuation_max_tokens_per_root=1000,
        continuation_stall_limit=2,
        continuation_max_depth=1,
        continuation_max_spawns_per_root=1,
    )
    root = await make_root(env)
    await set_intention(env, root.id, origin_decision_id=uuid.uuid4())
    await make_child(env, root)  # depth 1, one spawn
    _, got = await _claimed(env, root)
    for n in (1, 2):
        await add_arrival(env, root.id, n, progress=False, tokens=(500, 0))  # two turns, 1000 tokens, a stall of 2

    async def superseded(decision_id):
        return "superseded"

    settings = env.settings
    for expected, relax, value in (
        ("budget_turns", "continuation_max_turns_per_root", 8),
        ("budget_tokens", "continuation_max_tokens_per_root", 400000),
        ("budget_stall", "continuation_stall_limit", 3),
        ("limit_depth", "continuation_max_depth", 3),
        ("limit_spawns", "continuation_max_spawns_per_root", 12),
    ):
        assert await _gate(env, got, superseded, settings=settings) == expected
        settings = settings.model_copy(update={relax: value})
    assert await _gate(env, got, superseded, settings=settings) == "plan_resolved"


@pytest.mark.postgres_only
async def test_decision_outcome_reads_the_decision_row(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    async with env.db.session() as s:
        decision = Decision(
            agent_id=env.agent,
            description="Plan: tell the user about the snow once the report is back",
            confidence=0.7,
            category="process",
            stakes="low",
            outcome="noise",
        )
        s.add(decision)
        await s.commit()
        decision_id = decision.id
    async with env.db.session() as s:
        assert await continuation.decision_outcome(s, env.agent, decision_id) == "noise"
        assert await continuation.decision_outcome(s, env.agent, uuid.uuid4()) is None
        assert await continuation.decision_outcome(s, "another-agent", decision_id) is None


def test_the_plan_question_is_a_required_argument_of_the_gate():
    """Gate row 5 cannot be switched off by leaving the question out."""
    parameter = inspect.signature(continuation.gate).parameters["plan_outcome_of"]
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY and parameter.default is inspect.Parameter.empty


def test_a_gate_drop_commits_a_drop_and_a_gate_escalation_a_report():
    rows = (SimpleNamespace(title="Snow", body="40 cm"), SimpleNamespace(title="Wind", body="gusty"))
    got = SimpleNamespace(inbox_rows=rows)
    for reason in ("cancelled", "expired", "plan_resolved"):
        resolution, report = continuation.gate_inputs(reason, got)
        assert (resolution.decision, report) == ("drop", None)
        assert resolution.note == continuation.GATE_TEXT[reason] and resolution.progress_claimed is False
    for reason in ("past_deadline", "budget_turns", "budget_tokens", "budget_stall", "limit_depth", "limit_spawns"):
        resolution, report = continuation.gate_inputs(reason, got)
        assert resolution.decision == "report" and resolution.note == continuation.GATE_TEXT[reason]
        assert continuation.GATE_TEXT[reason] in report and "40 cm" in report and "gusty" in report


def test_every_gate_reason_the_table_allows_has_a_text():  # PIN
    allowed = {"cancelled", "expired", "past_deadline", "plan_resolved", "limit_depth", "limit_spawns"}
    allowed |= {"budget_turns", "budget_tokens", "budget_stall"}  # the migration's CHECK list for gate_reason
    assert set(continuation.GATE_TEXT) == allowed
    assert set(continuation.GATE_REASONS) == allowed
