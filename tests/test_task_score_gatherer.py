"""Task scoring v2: the gatherer reads the signals each finished task earned."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import event

from nous.cognitive.claim_verifier import UNVERIFIED_MARKER
from nous.observability import task_score_gatherer as gatherer
from nous.observability.task_scoring import score_leaf, score_rollup
from nous.storage.models import (
    CompensationSnapshot,
    DAGNode,
    Decision,
    Event,
    ExecutionDAG,
    ExecutionLedgerEntry,
    Intention,
    IntentionArrival,
    IntentionProposal,
    Subtask,
)

NOW = datetime.now(UTC)
SINCE = NOW - timedelta(days=15)


@pytest_asyncio.fixture
async def agent() -> str:
    return f"test-score-{uuid.uuid4().hex[:8]}"


async def add(db, *rows) -> None:
    async with db.session() as s:
        s.add_all(rows)
        await s.commit()


async def gather(db, agent, since=SINCE):
    """The three groups, each keyed by task id."""
    async with db.session() as s:
        got = await gatherer.gather_signals(s, agent, since=since)
    return ({x.task_id: x for x in got.leaves}, {x.task_id: x for x in got.dags}, {x.task_id: x for x in got.chains})


def subtask(agent, **kw) -> Subtask:
    fields = dict(
        id=uuid.uuid4(),
        agent_id=agent,
        task="t",
        status="completed",
        final_outcome="completed",
        completed_at=NOW - timedelta(hours=1),
        tokens_in=100,
        tokens_out=50,
    )
    return Subtask(**{**fields, **kw})


def ledger(agent, tool="bash", status="success", summary=None, **kw) -> ExecutionLedgerEntry:
    return ExecutionLedgerEntry(
        agent_id=agent,
        context_kind="subtask",
        tool_name=tool,
        side_effect_type="write",
        status=status,
        result_summary=summary,
        **kw,
    )


def decision(agent, outcome, reviewer="a2ui") -> Decision:
    return Decision(
        id=uuid.uuid4(),
        agent_id=agent,
        description="d",
        confidence=0.5,
        category="process",
        stakes="low",
        outcome=outcome,
        reviewer=reviewer,
    )


async def root(db, agent, *, close_reason="delivered", closed_ago=3600, **kw) -> Intention:
    rid = uuid.uuid4()
    fields = dict(
        id=rid,
        agent_id=agent,
        root_id=rid,
        source_kind="schedule",
        source_id=str(uuid.uuid4()),
        intent="i",
        origin_kind="interactive",
        wake_policy="continue",
        state="closed",
        close_reason=close_reason,
        closed_at=NOW - timedelta(seconds=closed_ago),
    )
    row = Intention(**{**fields, **kw})
    await add(db, row)
    return row


async def spawned(db, agent, parent: Intention, kind: str, source_id, **kw) -> Intention:
    row = Intention(
        id=uuid.uuid4(),
        agent_id=agent,
        root_id=parent.root_id,
        parent_id=parent.id,
        depth=1,
        source_kind=kind,
        source_id=str(source_id),
        intent="c",
        origin_kind="continuation",
        wake_policy="continue",
        authority="internal_only",
        state="closed",
        close_reason="resolved",
        closed_at=NOW,
        **kw,
    )
    await add(db, row)
    return row


async def test_a_subtask_collects_its_ledger_claims_undos_and_grades(db, agent):
    sub, other = subtask(agent), subtask(agent)
    old, foreign = subtask(agent, completed_at=NOW - timedelta(days=30)), subtask("someone-else")
    bad_exit = ledger(agent, summary="exit code 2; 5 chars of output, not stored", subtask_id=sub.id)
    refused = ledger(agent, "send_email", "blocked", "refused by internal_only", subtask_id=sub.id)
    errored = ledger(agent, "write_file", "unknown", subtask_id=sub.id)
    fine = ledger(agent, summary="exit code 0; 5 chars of output, not stored", subtask_id=sub.id)
    undone = ledger(agent, "write_file", subtask_id=sub.id)
    not_mine = ledger(agent, "bash", "error", subtask_id=other.id)
    d_bad, d_good = decision(agent, "failure"), decision(agent, "success")
    await add(db, sub, other, old, foreign, d_bad, d_good)
    await add(db, bad_exit, refused, errored, fine, undone, not_mine)
    await add(
        db,
        CompensationSnapshot(
            ledger_entry_id=undone.id, agent_id=agent, tool_name="write_file", reverted_at=NOW, revert_result="ok"
        ),
        Event(
            agent_id=agent,
            session_id=f"subtask-{sub.id.hex[:8]}",
            event_type=gatherer.CLAIMS_EVENT,
            data={"verified": False},
        ),
        Event(
            agent_id=agent,
            session_id=f"subtask-{sub.id.hex[:8]}",
            event_type=gatherer.CLAIMS_EVENT,
            data={"verified": True},
        ),
    )
    await root(db, agent, source_kind="subtask", source_id=str(sub.id), origin_decision_id=d_bad.id)

    leaves, _, _ = await gather(db, agent)

    assert set(leaves) == {str(sub.id), str(other.id)}  # not the old one, not another agent's
    s = leaves[str(sub.id)]
    assert (s.status, s.detail, s.tokens) == ("completed", "completed", 150)
    assert s.harness_errors == 2  # the exit-2 bash call and the unknown write_file; exit 0 is fine
    assert s.refusals == 1
    assert s.unverified_claims == 1
    assert s.owner_grade == "failure"
    assert s.owner_overrides == 2  # the undone write and the decision the owner graded failure
    assert s.evidence["undone"] == [str(undone.id)]
    assert s.evidence["failure_graded"] == [str(d_bad.id)]
    assert s.evidence["bash_exits"] == [str(bad_exit.id)]  # kept apart from the ledger's own errors
    assert s.evidence["ledger_errors"] == [str(errored.id)]
    assert leaves[str(other.id)].harness_errors == 1
    # done by its own report (0.0), then -2 for the overrides, -0.5 the claim, -0.2, -0.25: floored at -1
    assert score_leaf(s, NOW).score == -1.0


async def test_a_non_zero_bash_exit_is_a_harness_error_with_or_without_the_command_text(db, agent):
    sub = subtask(agent)
    await add(db, sub)
    await add(
        db,
        ledger(agent, summary="exit code 1; 9 chars of output, not stored", subtask_id=sub.id, input_text="git push x"),
        ledger(agent, summary="exit code 2", subtask_id=sub.id),  # from before detail capture: no command text
        ledger(agent, summary="exit code 0", subtask_id=sub.id),
    )
    s = (await gather(db, agent))[0][str(sub.id)]
    assert (s.harness_errors, len(s.evidence["bash_exits"])) == (2, 2)
    assert "ledger_errors" not in s.evidence


async def test_a_subtask_with_no_signals_is_clean(db, agent):
    sub = subtask(agent, status="failed", final_outcome="timed_out", tokens_in=0, tokens_out=0)
    await add(db, sub)
    s = (await gather(db, agent))[0][str(sub.id)]
    assert (s.harness_errors, s.refusals, s.owner_overrides, s.unverified_claims, s.owner_grade) == (0, 0, 0, 0, None)
    assert s.evidence == {} and s.dag_checked is False and s.finished_at.tzinfo is not None


async def test_a_subtask_is_checked_only_by_a_completed_check_node_in_a_completed_dag(db, agent):
    async def dag_with(status, check_status, completion_check="pytest -q passes"):
        dag = ExecutionDAG(id=uuid.uuid4(), agent_id=agent, name="d", status=status, completed_at=NOW)
        await add(db, dag)
        work = DAGNode(id=uuid.uuid4(), dag_id=dag.id, name="work", node_type="subtask")
        await add(db, work)
        if check_status:
            await add(
                db,
                DAGNode(
                    id=uuid.uuid4(),
                    dag_id=dag.id,
                    name="check",
                    node_type="check",
                    status=check_status,
                    completion_check=completion_check,
                ),
            )
        sub = subtask(agent, dag_node_id=work.id)
        await add(db, sub)
        return sub, dag

    ok, ok_dag = await dag_with("completed", "completed")
    unchecked, _ = await dag_with("completed", None)
    check_failed, _ = await dag_with("completed", "failed")
    dag_failed, _ = await dag_with("failed", "completed")
    no_check, _ = await dag_with("completed", "completed", completion_check=None)  # a model's self-disable
    blank_check, _ = await dag_with("completed", "completed", completion_check="   ")
    plain = subtask(agent)
    await add(db, plain)

    leaves, dags, _ = await gather(db, agent)

    assert [
        leaves[str(x.id)].dag_checked for x in (ok, unchecked, check_failed, dag_failed, no_check, blank_check, plain)
    ] == [True, False, False, False, False, False, False]
    assert dags[str(ok_dag.id)].dag_checked is True and dags[str(ok_dag.id)].child_ids == (str(ok.id),)


async def test_a_subtask_takes_its_own_node_check_for_the_nodes_current_attempt_only(db, agent):
    dag = ExecutionDAG(id=uuid.uuid4(), agent_id=agent, name="d", status="failed", completed_at=NOW)
    await add(db, dag)

    async def node_for(status, error=None, *, check="test -f /tmp/done", attempts=1, polled=True, current=True):
        """A subtask that reported completed, and the node it ran in; ``current=False`` is an earlier attempt."""
        sub = subtask(agent)
        node = DAGNode(
            id=uuid.uuid4(),
            dag_id=dag.id,
            name=f"n-{sub.id.hex[:6]}",
            node_type="subtask",
            status=status,
            error=error,
            completion_check=check,
            check_attempts=attempts,
            last_check_at=NOW if polled else None,
            subtask_id=sub.id if current else uuid.uuid4(),
        )
        sub.dag_node_id = node.id
        await add(db, node, sub)
        return sub, node

    passed, passed_node = await node_for("completed", attempts=2)
    failed, failed_node = await node_for("failed", "Completion check failed: exit code 1")
    timed_out, timed_out_node = await node_for(
        "failed", "Completion check timed out after 600s (5 attempts)", attempts=5
    )
    never_polled, _ = await node_for(
        "failed", "Completion check timed out after 600s (0 attempts)", attempts=0, polled=False
    )
    other_failure, _ = await node_for("failed", "Check disabled itself but its final run failed")
    no_check, _ = await node_for("completed", check=None, attempts=0, polled=False)
    skipped, skipped_node = await node_for("skipped", "Completion check failed: exit code 1")  # a fix node skipped it
    earlier_attempt, earlier_node = await node_for("completed", attempts=2, current=False)  # retried after it

    leaves, _, _ = await gather(db, agent)

    def node_check(sub):
        return leaves[str(sub.id)].node_check

    assert [
        node_check(x)
        for x in (passed, failed, timed_out, never_polled, other_failure, no_check, skipped, earlier_attempt)
    ] == [
        "passed",
        "failed",
        "unconfirmed",
        None,
        None,
        None,
        "failed",
        "superseded",
    ]
    assert leaves[str(passed.id)].evidence == {"node_check_passed": [str(passed_node.id)]}
    assert leaves[str(failed.id)].evidence == {"node_check_failed": [str(failed_node.id)]}
    assert leaves[str(timed_out.id)].evidence == {"node_check_unconfirmed": [str(timed_out_node.id)]}
    assert leaves[str(skipped.id)].evidence == {"node_check_failed": [str(skipped_node.id)]}
    assert leaves[str(earlier_attempt.id)].evidence == {"node_check_superseded": [str(earlier_node.id)]}
    outcomes = {
        name: score_leaf(leaves[str(sub.id)], NOW).outcome
        for name, sub in (
            ("passed", passed),
            ("failed", failed),
            ("timed_out", timed_out),
            ("skipped", skipped),
            ("earlier", earlier_attempt),
        )
    }
    assert outcomes == {
        "passed": "done_self",
        "failed": "failed",
        "timed_out": "partial",
        "skipped": "failed",
        "earlier": "partial",
    }


async def test_a_retried_nodes_earlier_attempt_is_not_credited_by_the_dags_check_node(db, agent):
    # The first attempt reported done, its node failed and was retried; the retry passed and the DAG completed with a
    # passing check node. The DAG's check confirms the retry's work, not the attempt the node failed after.
    dag = ExecutionDAG(id=uuid.uuid4(), agent_id=agent, name="d", status="completed", completed_at=NOW)
    await add(db, dag)
    first, retry = subtask(agent), subtask(agent)
    work = DAGNode(id=uuid.uuid4(), dag_id=dag.id, name="work", node_type="subtask", status="completed")
    work.subtask_id = retry.id
    first.dag_node_id = retry.dag_node_id = work.id
    check = DAGNode(
        id=uuid.uuid4(), dag_id=dag.id, name="check", node_type="check", status="completed", completion_check="true"
    )
    await add(db, work, check, first, retry)

    leaves, _, _ = await gather(db, agent)

    assert leaves[str(first.id)].dag_checked and leaves[str(retry.id)].dag_checked
    assert score_leaf(leaves[str(first.id)], NOW).outcome == "partial"
    assert score_leaf(leaves[str(retry.id)], NOW).outcome == "done_verified"


async def test_a_dag_keeps_only_its_own_calls_and_names_its_subtasks(db, agent):
    dag = ExecutionDAG(
        id=uuid.uuid4(), agent_id=agent, name="d", status="completed", completed_at=NOW, tokens_consumed=900
    )
    cancelled = ExecutionDAG(
        id=uuid.uuid4(),
        agent_id=agent,
        name="c",
        status="cancelled",
        completed_at=NOW,
        result_summary="cancelled by user",
    )
    running = ExecutionDAG(id=uuid.uuid4(), agent_id=agent, name="r", status="running")
    await add(db, dag, cancelled, running)
    node = DAGNode(id=uuid.uuid4(), dag_id=dag.id, name="n", node_type="subtask")
    await add(db, node)
    sub = subtask(agent, dag_node_id=node.id)
    await add(db, sub)
    await add(
        db,
        ledger(agent, "send_email", "blocked", "refused by x", dag_id=dag.id),  # the DAG's own call
        ledger(agent, "bash", "error", dag_id=dag.id),  # the DAG's own call
        ledger(agent, "bash", "error", dag_id=dag.id, subtask_id=sub.id),  # the subtask's call
        Event(
            agent_id=agent,
            session_id=f"subtask-{sub.id.hex[:8]}",
            event_type=gatherer.CLAIMS_EVENT,
            data={"verified": False},
        ),  # the subtask's claim
    )
    leaves, dags, _ = await gather(db, agent)

    d = dags[str(dag.id)]
    assert (d.tokens, d.refusals, d.harness_errors, d.unverified_claims) == (900, 1, 1, 0)
    assert d.child_ids == (str(sub.id),)
    assert (leaves[str(sub.id)].harness_errors, leaves[str(sub.id)].unverified_claims) == (1, 1)
    assert str(cancelled.id) in dags and str(running.id) not in dags


async def test_a_chain_keeps_only_its_own_sessions_and_names_its_subtasks(db, agent):
    sub = subtask(agent, tokens_in=10, tokens_out=5)
    dag = ExecutionDAG(id=uuid.uuid4(), agent_id=agent, name="d", status="completed", completed_at=NOW)
    await add(db, sub, dag)
    node = DAGNode(id=uuid.uuid4(), dag_id=dag.id, name="n", node_type="subtask")
    await add(db, node)
    in_dag = subtask(agent, dag_node_id=node.id)
    d_arrival, d_origin = decision(agent, "partial"), decision(agent, "failure")
    await add(db, in_dag, d_arrival, d_origin)
    # A schedule-kind depth-0 root that is not a container: the schedule shape from before containers (pre-F099 I1).
    top = await root(db, agent, origin_decision_id=d_origin.id)
    await spawned(db, agent, top, "subtask", sub.id)
    await spawned(db, agent, top, "dag", dag.id)
    arrival = IntentionArrival(
        agent_id=agent,
        root_id=top.id,
        n=1,
        intention_ids=[top.id],
        claim_token=uuid.uuid4(),
        outcome="resolved",
        note=f"{UNVERIFIED_MARKER} nothing backs it",
        tokens_in=40,
        tokens_out=20,
        decision_record_id=d_arrival.id,
    )

    def proposal(state, by):
        return IntentionProposal(
            agent_id=agent,
            intention_id=top.id,
            root_id=top.id,
            tool="send_email",
            arguments={},
            rationale="r",
            state=state,
            claim_token=uuid.uuid4(),
            decided_by=by,
        )

    rejected_owner, rejected_system = proposal("rejected", "telegram:42"), proposal("rejected", "system")
    approved = proposal("executed", "companion:owner")
    await add(db, arrival, rejected_owner, rejected_system, approved)
    await add(
        db,
        ledger(agent, "bash", "success", "exit code 1", session_id=f"intent-{top.id}"),  # the continuation turn's call
        ledger(agent, "write_file", "unknown", session_id=f"proposal-{approved.id}"),  # the approved action
        ledger(
            agent, "bash", "error", subtask_id=sub.id, session_id=f"intent-{top.id}"
        ),  # names a subtask: not the chain's
        ledger(agent, "bash", "error", dag_id=dag.id, session_id=f"intent-{top.id}"),  # names a DAG: not the chain's
        Event(
            agent_id=agent, session_id=f"intent-{top.id}", event_type=gatherer.CLAIMS_EVENT, data={"verified": False}
        ),
    )

    _, _, chains = await gather(db, agent)

    c = chains[str(top.id)]
    assert (c.status, c.harness_errors, c.refusals) == ("delivered", 2, 0)
    assert c.evidence["proposals_rejected"] == [str(rejected_owner.id)]  # not the system's
    assert c.owner_overrides == 2  # the owner's rejected proposal and the root's decision the owner graded failure
    assert c.unverified_claims == 2  # the flagged arrival note and the claim event
    assert c.owner_grade == "failure"  # the arrival's decision (partial) and the schedule root's (failure): the worst
    assert sorted(c.evidence["grade_decisions"]) == sorted([str(d_arrival.id), str(d_origin.id)])
    assert c.evidence["failure_graded"] == [str(d_origin.id)]
    assert sorted(c.child_ids) == sorted([str(sub.id), str(in_dag.id)])
    assert c.tokens is None  # a chain's spend is its subtasks' and DAGs': not summed on the roll-up


async def test_one_failing_subtask_inside_a_dag_inside_a_chain_is_counted_once(db, agent):
    dag = ExecutionDAG(id=uuid.uuid4(), agent_id=agent, name="d", status="completed", completed_at=NOW)
    await add(db, dag)
    node = DAGNode(id=uuid.uuid4(), dag_id=dag.id, name="n", node_type="subtask")
    await add(db, node)
    sub = subtask(agent, dag_node_id=node.id)
    await add(db, sub)
    top = await root(db, agent, source_kind="subtask", source_id=str(sub.id))
    await spawned(db, agent, top, "dag", dag.id)
    # the ONE penalty: a refused call by the subtask, tagged with its DAG and run in a session the chain also owns
    await add(
        db,
        ledger(
            agent,
            "send_email",
            "blocked",
            "refused by x",
            subtask_id=sub.id,
            dag_id=dag.id,
            session_id=f"intent-{top.id}",
        ),
    )

    leaves, dags, chains = await gather(db, agent)

    assert [x.refusals for x in (leaves[str(sub.id)], dags[str(dag.id)], chains[str(top.id)])] == [1, 0, 0]
    leaf = score_leaf(leaves[str(sub.id)], NOW)
    rows = [
        leaf,
        score_rollup(dags[str(dag.id)], {str(sub.id): leaf.score}, NOW),
        score_rollup(chains[str(top.id)], {str(sub.id): leaf.score}, NOW),
    ]
    assert sum(r.refusals for r in rows) == 1  # across all three rows the refusal exists once
    assert [r.score for r in rows] == [-0.25, None, None]  # and only the leaf carries a score
    assert [r.children_mean_score for r in rows[1:]] == [-0.25, -0.25]


async def test_roots_that_are_not_tasks_are_skipped(db, agent):
    open_root = await root(db, agent, close_reason=None, state="pending", closed_at=None)
    container = await root(db, agent, wake_policy="container")
    old = await root(db, agent, closed_ago=30 * 86400)
    legacy = await root(db, agent, close_reason="legacy")
    cancelled = await root(db, agent, close_reason="cancelled", state="cancelled")
    chains = (await gather(db, agent))[2]
    assert {str(open_root.id), str(container.id), str(old.id)}.isdisjoint(chains)
    assert (chains[str(legacy.id)].status, chains[str(cancelled.id)].status) == ("legacy", "cancelled")


async def test_a_subtask_is_graded_through_the_intention_that_spawned_it(db, agent):
    sub = subtask(agent)
    d = decision(agent, "success")
    await add(db, sub, d)
    await root(db, agent, source_kind="subtask", source_id=str(sub.id), origin_decision_id=d.id)
    s = (await gather(db, agent))[0][str(sub.id)]
    assert (s.owner_grade, s.owner_overrides) == ("success", 0)
    assert score_leaf(s, NOW).outcome == "done_verified"


async def test_the_agents_own_review_is_not_the_owners_grade(db, agent):
    sub = subtask(agent)
    d = decision(agent, "success", reviewer="agent")
    await add(db, sub, d)
    await root(db, agent, source_kind="subtask", source_id=str(sub.id), origin_decision_id=d.id)
    assert (await gather(db, agent))[0][str(sub.id)].owner_grade is None


async def test_the_number_of_queries_does_not_grow_with_the_number_of_tasks(db, agent):
    async def count(n: int) -> int:
        a = f"{agent}-{n}"
        for _ in range(n):
            dag = ExecutionDAG(id=uuid.uuid4(), agent_id=a, name="d", status="completed", completed_at=NOW)
            await add(db, dag)
            node = DAGNode(id=uuid.uuid4(), dag_id=dag.id, name="n", node_type="subtask")
            await add(db, node)
            sub = subtask(a, dag_node_id=node.id)
            top = await root(db, a, source_kind="subtask", source_id=str(sub.id))
            await add(
                db,
                sub,
                ledger(a, "bash", "error", subtask_id=sub.id, dag_id=dag.id),
                ledger(a, "bash", "error", dag_id=dag.id),
                Event(
                    agent_id=a,
                    session_id=f"intent-{top.id}",
                    event_type=gatherer.CLAIMS_EVENT,
                    data={"verified": False},
                ),
            )
        statements: list[str] = []

        def record(conn, cursor, statement, parameters, context, executemany):
            statements.append(statement)

        event.listen(db.engine.sync_engine, "before_cursor_execute", record)
        try:
            leaves, dags, chains = await gather(db, a)
        finally:
            event.remove(db.engine.sync_engine, "before_cursor_execute", record)
        assert (len(leaves), len(dags), len(chains)) == (n, n, n)
        return len(statements)

    assert await count(2) == await count(12)


async def test_a_ledger_row_older_than_the_lookback_is_not_counted(db, agent):
    sub = subtask(agent)
    await add(db, sub)
    await add(
        db,
        ledger(agent, "bash", "error", subtask_id=sub.id, created_at=NOW - timedelta(days=60)),
        ledger(agent, "bash", "error", subtask_id=sub.id, created_at=NOW - timedelta(days=3)),
    )
    assert (await gather(db, agent))[0][str(sub.id)].harness_errors == 1


@pytest.mark.parametrize(
    ("grade", "expected_grades", "expected_overrides"),
    [("failure", ["failure", None, None], [1, 0, 0]), ("success", ["success", None, None], [0, 0, 0])],
)
async def test_one_owner_grade_lands_on_one_unit_of_a_chain_not_on_its_children(
    db, agent, grade, expected_grades, expected_overrides
):
    # The continuation runner stamps the root's origin_decision_id on every child it spawns.
    d = decision(agent, grade)
    subs = [subtask(agent) for _ in range(3)]
    await add(db, d, *subs)
    top = await root(db, agent, source_kind="subtask", source_id=str(subs[0].id), origin_decision_id=d.id)
    for child in subs[1:]:
        await spawned(db, agent, top, "subtask", child.id, origin_decision_id=d.id)

    leaves, _, chains = await gather(db, agent)

    got = [leaves[str(x.id)] for x in subs]
    assert [x.owner_grade for x in got] == expected_grades
    assert [x.owner_overrides for x in got] == expected_overrides
    assert chains[str(top.id)].owner_grade is None
    assert [score_leaf(x, NOW).outcome for x in got] == [
        "done_verified" if grade == "success" else "done_self",
        "done_self",
        "done_self",
    ]


async def test_a_decision_shared_by_sibling_roots_goes_to_the_earliest_root_only(db, agent):
    d = decision(agent, "failure")
    first_sub, second_sub, third_sub = subtask(agent), subtask(agent), subtask(agent)
    await add(db, d, first_sub, second_sub, third_sub)
    early = NOW - timedelta(hours=5)
    # one turn spawned three roots; the creation order is not the order of the rows here
    for sub, at in ((second_sub, 30), (first_sub, 0), (third_sub, 30)):
        await root(
            db,
            agent,
            source_kind="subtask",
            source_id=str(sub.id),
            origin_decision_id=d.id,
            created_at=early + timedelta(seconds=at),
        )

    leaves, _, _ = await gather(db, agent)

    assert [leaves[str(x.id)].owner_overrides for x in (first_sub, second_sub, third_sub)] == [1, 0, 0]
    assert sum(x.owner_overrides for x in leaves.values()) == 1  # one tap, one override


async def test_sibling_roots_with_the_same_created_at_are_ordered_by_id(db, agent):
    d = decision(agent, "failure")
    a, b = subtask(agent), subtask(agent)
    await add(db, d, a, b)
    same = NOW - timedelta(hours=2)
    r1 = await root(db, agent, source_kind="subtask", source_id=str(a.id), origin_decision_id=d.id, created_at=same)
    r2 = await root(db, agent, source_kind="subtask", source_id=str(b.id), origin_decision_id=d.id, created_at=same)
    winner, loser = (a, b) if str(r1.id) < str(r2.id) else (b, a)
    leaves, _, _ = await gather(db, agent)
    assert {k: v.owner_overrides for k, v in leaves.items()} == {str(winner.id): 1, str(loser.id): 0}


async def test_an_earlier_schedule_container_does_not_take_a_shared_decision_from_a_subtask_root(db, agent):
    # A container is a depth-0 row carrying the creating turn's decision, but it is not a scored unit.
    d = decision(agent, "failure")
    sub = subtask(agent)
    await add(db, d, sub)
    early = NOW - timedelta(hours=5)
    await root(
        db,
        agent,
        wake_policy="container",
        state="pending",
        close_reason=None,
        closed_at=None,
        origin_decision_id=d.id,
        created_at=early,
    )
    await root(
        db,
        agent,
        source_kind="subtask",
        source_id=str(sub.id),
        origin_decision_id=d.id,
        created_at=early + timedelta(seconds=30),
    )
    s = (await gather(db, agent))[0][str(sub.id)]
    assert (s.owner_grade, s.owner_overrides) == ("failure", 1)


@pytest.mark.parametrize("dag_first", [True, False])
async def test_a_decision_shared_by_a_dag_root_and_a_subtask_root_goes_to_the_earliest_only(db, agent, dag_first):
    d = decision(agent, "failure")
    sub = subtask(agent)
    dag = ExecutionDAG(id=uuid.uuid4(), agent_id=agent, name="d", status="completed", completed_at=NOW)
    await add(db, d, sub, dag)
    early = NOW - timedelta(hours=5)
    dag_at, sub_at = (early, early + timedelta(seconds=30)) if dag_first else (early + timedelta(seconds=30), early)
    await root(db, agent, source_kind="dag", source_id=str(dag.id), origin_decision_id=d.id, created_at=dag_at)
    await root(db, agent, source_kind="subtask", source_id=str(sub.id), origin_decision_id=d.id, created_at=sub_at)

    leaves, dags, _ = await gather(db, agent)

    got = (dags[str(dag.id)].owner_overrides, leaves[str(sub.id)].owner_overrides)
    assert got == ((1, 0) if dag_first else (0, 1))  # one tap, one override, on the earliest root's unit


async def test_a_dag_subtask_lookup_is_scoped_to_the_agent(db, agent):
    dag = ExecutionDAG(id=uuid.uuid4(), agent_id=agent, name="d", status="completed", completed_at=NOW)
    await add(db, dag)
    node = DAGNode(id=uuid.uuid4(), dag_id=dag.id, name="n", node_type="subtask")
    await add(db, node)
    mine, foreign = subtask(agent, dag_node_id=node.id), subtask("someone-else", dag_node_id=node.id)
    await add(db, mine, foreign)
    assert (await gather(db, agent))[1][str(dag.id)].child_ids == (str(mine.id),)
