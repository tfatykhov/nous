"""Task scoring v1: the upsert, one scoring pass, and the command."""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import event, select, text
from sqlalchemy.exc import IntegrityError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from nous.dag.approval import as_utc
from nous.observability import task_score_job as job
from nous.observability.task_scoring import SCORER_VERSION, TaskScore
from nous.storage.models import (
    Base,
    CompensationSnapshot,
    DAGNode,
    Decision,
    ExecutionDAG,
    ExecutionLedgerEntry,
    Intention,
    ScorerHeartbeat,
    Subtask,
)
from nous.storage.models import (
    TaskScore as TaskScoreRow,
)

NOW = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)


@pytest_asyncio.fixture
async def agent() -> str:
    return f"test-scorejob-{uuid.uuid4().hex[:8]}"


def score(
    task_id="t1", *, kind="subtask", version=SCORER_VERSION, outcome="done_verified", value=1.0, **kw
) -> TaskScore:
    fields = dict(
        task_kind=kind,
        task_id=task_id,
        finished_at=NOW - timedelta(hours=1),
        scorer_version=version,
        outcome=outcome,
        not_scored_reason=None,
        owner_grade=None,
        owner_overrides=0,
        unverified_claims=0,
        harness_errors=0,
        refusals=0,
        tokens=None,
        score=value,
        children_scored=None,
        children_mean_score=None,
        settled=False,
        evidence={},
    )
    return TaskScore(**{**fields, **kw})


async def rows(db, agent):
    async with db.session() as s:
        return (await s.execute(select(TaskScoreRow).where(TaskScoreRow.agent_id == agent))).scalars().all()


async def upsert(db, agent, scores, at=NOW):
    async with db.session() as s:
        n = await job.upsert_scores(s, agent, scores, scored_at=at)
        await s.commit()
    return n


async def heartbeats(db, agent):
    async with db.session() as s:
        return (await s.execute(select(ScorerHeartbeat).where(ScorerHeartbeat.agent_id == agent))).scalars().all()


async def test_the_unique_key_is_agent_kind_task_and_version(db, agent):
    await upsert(db, agent, [score("a"), score("a", version=SCORER_VERSION + 1), score("b")])
    assert sorted((r.task_id, r.scorer_version) for r in await rows(db, agent)) == [
        ("a", SCORER_VERSION),
        ("a", SCORER_VERSION + 1),
        ("b", SCORER_VERSION),
    ]


async def test_an_upsert_refreshes_the_row_and_keeps_when_scoring_began(db, agent):
    await upsert(db, agent, [score("a", value=1.0)], at=NOW)
    first = (await rows(db, agent))[0]
    await upsert(
        db,
        agent,
        [score("a", value=-0.5, owner_grade="failure", owner_overrides=1, settled=True, evidence={"undone": ["x"]})],
        at=NOW + timedelta(minutes=15),
    )
    got = await rows(db, agent)
    assert len(got) == 1
    row = got[0]
    assert row.id == first.id  # the same row
    assert (row.score, row.owner_grade, row.owner_overrides, row.settled, row.evidence) == (
        -0.5,
        "failure",
        1,
        True,
        {"undone": ["x"]},
    )
    assert row.scored_at.replace(tzinfo=UTC) == NOW + timedelta(minutes=15)
    assert row.first_scored_at.replace(tzinfo=UTC) == NOW  # set on insert, never moved


async def test_a_settled_row_is_never_rewritten_but_an_unsettled_one_is(db, agent):
    await upsert(db, agent, [score("a", value=0.0, settled=True), score("b", value=0.0, settled=False)])
    await upsert(
        db,
        agent,
        [
            score("a", value=-1.0, settled=True, owner_overrides=1),
            score("b", value=-1.0, settled=True, owner_overrides=1),
        ],
        at=NOW + timedelta(hours=1),
    )
    got = {r.task_id: r for r in await rows(db, agent)}
    assert (got["a"].score, got["a"].owner_overrides) == (0.0, 0)  # settled: left alone
    assert (got["a"].scored_at.replace(tzinfo=UTC)) == NOW
    assert (got["b"].score, got["b"].owner_overrides, got["b"].settled) == (-1.0, 1, True)  # settles now, once


async def test_another_scorer_version_is_kept_beside_the_old_rows(db, agent):
    await upsert(db, agent, [score("a", value=1.0)])
    await upsert(db, agent, [score("a", version=SCORER_VERSION + 1, value=0.2)])
    assert sorted(r.score for r in await rows(db, agent)) == [0.2, 1.0]


async def test_a_large_batch_is_written_in_chunks(db, agent, monkeypatch):
    monkeypatch.setattr(job, "UPSERT_CHUNK", 3)
    assert await upsert(db, agent, [score(f"t{i}") for i in range(8)]) == 8
    assert len(await rows(db, agent)) == 8


@pytest.mark.parametrize(
    "bad",
    [
        dict(outcome="great"),  # not an outcome
        dict(task_kind="turn"),  # not a kind
        dict(task_kind="dag", value=0.5),  # only a subtask carries a score
        dict(outcome="not_scored", value=0.0),  # a task that is not scored has no score
    ],
)
async def test_the_check_constraints_refuse_what_the_scorer_never_writes(db, agent, bad):
    with pytest.raises(IntegrityError):
        await upsert(db, agent, [score("a", **bad)])


async def test_a_rollup_and_a_not_scored_row_are_valid_with_a_null_score(db, agent):
    await upsert(
        db,
        agent,
        [
            score("d", kind="dag", value=None, children_scored=2, children_mean_score=0.25),
            score("c", outcome="not_scored", value=None, not_scored_reason="cancelled"),
        ],
    )
    got = {r.task_id: r for r in await rows(db, agent)}
    assert (got["d"].score, got["d"].children_scored, got["d"].children_mean_score) == (None, 2, 0.25)
    assert (got["c"].score, got["c"].not_scored_reason) == (None, "cancelled")


async def test_a_pass_writes_leaves_with_scores_and_rollups_without(db, agent):
    dag = ExecutionDAG(
        id=uuid.uuid4(), agent_id=agent, name="d", status="completed", completed_at=NOW - timedelta(hours=1)
    )
    async with db.session() as s:
        s.add(dag)
        await s.commit()
    node = DAGNode(id=uuid.uuid4(), dag_id=dag.id, name="n", node_type="subtask")
    check = DAGNode(
        id=uuid.uuid4(),
        dag_id=dag.id,
        name="c",
        node_type="check",
        status="completed",
        completion_check="pytest -q passes",
    )
    ok = Subtask(
        id=uuid.uuid4(),
        agent_id=agent,
        task="t",
        status="completed",
        final_outcome="completed",
        completed_at=NOW - timedelta(hours=2),
        tokens_in=10,
        tokens_out=5,
    )
    blocked = Subtask(
        id=uuid.uuid4(),
        agent_id=agent,
        task="t",
        status="completed",
        final_outcome="incomplete_blocked",
        completed_at=NOW - timedelta(days=14, hours=12),
    )
    stale = Subtask(
        id=uuid.uuid4(),
        agent_id=agent,
        task="t",
        status="completed",
        final_outcome="completed",
        completed_at=NOW - timedelta(days=16),
    )
    cancelled = Subtask(
        id=uuid.uuid4(),
        agent_id=agent,
        task="t",
        status="cancelled",
        final_outcome="cancelled",
        completed_at=NOW - timedelta(hours=3),
    )
    async with db.session() as s:
        s.add_all([node, check])
        await s.commit()
        ok.dag_node_id = node.id
        s.add_all([ok, blocked, stale, cancelled])
        s.add(
            ExecutionLedgerEntry(
                agent_id=agent,
                context_kind="subtask",
                tool_name="send_email",
                side_effect_type="external",
                status="blocked",
                result_summary="refused by x",
                subtask_id=ok.id,
                dag_id=dag.id,
            )
        )
        s.add(
            ExecutionLedgerEntry(
                agent_id=agent,
                context_kind="dag_node",
                tool_name="bash",
                side_effect_type="write",
                status="error",
                dag_id=dag.id,
            )
        )
        await s.commit()

    assert await job.run_scoring_pass(db, agent, now=NOW) == 4  # ok, blocked, cancelled, the DAG; not the stale one
    assert await job.run_scoring_pass(db, agent, now=NOW + timedelta(minutes=15)) == 4  # idempotent

    by_task = {r.task_id: r for r in await rows(db, agent)}
    assert set(by_task) == {str(ok.id), str(blocked.id), str(cancelled.id), str(dag.id)}
    leaf = by_task[str(ok.id)]
    assert (leaf.outcome, leaf.score, leaf.tokens, leaf.refusals, leaf.settled) == ("done_verified", 0.75, 15, 1, False)
    assert (by_task[str(blocked.id)].outcome, by_task[str(blocked.id)].score, by_task[str(blocked.id)].settled) == (
        "partial",
        0.0,
        True,
    )
    gone = by_task[str(cancelled.id)]
    assert (gone.outcome, gone.not_scored_reason, gone.score) == ("not_scored", "cancelled", None)
    d = by_task[str(dag.id)]
    assert (d.outcome, d.score, d.harness_errors, d.refusals) == (
        "done_verified",
        None,
        1,
        0,
    )  # the DAG's own call only
    assert (d.children_scored, d.children_mean_score) == (1, 0.75)
    assert all(r.scorer_version == SCORER_VERSION for r in by_task.values())
    assert sum(r.refusals for r in by_task.values()) == 1  # the one refusal, counted on one row


async def test_an_intention_chain_gets_a_rollup_row_over_its_subtasks(db, agent):
    sub = Subtask(
        id=uuid.uuid4(),
        agent_id=agent,
        task="t",
        status="completed",
        final_outcome="completed",
        completed_at=NOW - timedelta(hours=2),
    )
    rid = uuid.uuid4()
    async with db.session() as s:
        s.add(sub)
        s.add(
            Intention(
                id=rid,
                agent_id=agent,
                root_id=rid,
                source_kind="subtask",
                source_id=str(sub.id),
                intent="i",
                origin_kind="interactive",
                wake_policy="continue",
                state="closed",
                close_reason="delivered",
                closed_at=NOW - timedelta(hours=1),
            )
        )
        await s.commit()
    await job.run_scoring_pass(db, agent, now=NOW)
    by_task = {r.task_id: r for r in await rows(db, agent)}
    chain = by_task[str(rid)]
    assert (chain.task_kind, chain.outcome, chain.score, chain.children_scored) == ("intention", "done_self", None, 1)
    assert by_task[str(sub.id)].score == 0.0  # delivered by its own report: no credit


async def test_a_late_owner_undo_rescores_a_task_inside_the_window(db, agent):
    sub = Subtask(
        id=uuid.uuid4(),
        agent_id=agent,
        task="t",
        status="completed",
        final_outcome="completed",
        completed_at=NOW - timedelta(days=2),
    )
    entry = ExecutionLedgerEntry(
        agent_id=agent,
        context_kind="subtask",
        tool_name="write_file",
        side_effect_type="write",
        status="success",
        subtask_id=sub.id,
    )
    async with db.session() as s:
        s.add_all([sub, entry])
        await s.commit()
    await job.run_scoring_pass(db, agent, now=NOW)
    assert (await rows(db, agent))[0].score == 0.0

    async with db.session() as s:  # two days later the owner undoes the write
        s.add(
            CompensationSnapshot(
                ledger_entry_id=entry.id,
                agent_id=agent,
                tool_name="write_file",
                reverted_at=NOW + timedelta(days=2),
                revert_result="ok",
            )
        )
        await s.commit()
    await job.run_scoring_pass(db, agent, now=NOW + timedelta(days=2))

    got = await rows(db, agent)
    assert len(got) == 1 and (got[0].score, got[0].owner_overrides) == (-1.0, 1)


async def test_a_pass_writes_only_its_own_agents_rows(db, agent):
    other = f"{agent}-other"
    async with db.session() as s:
        s.add(
            Subtask(
                id=uuid.uuid4(),
                agent_id=other,
                task="t",
                status="completed",
                final_outcome="completed",
                completed_at=NOW - timedelta(hours=1),
            )
        )
        await s.commit()
    assert await job.run_scoring_pass(db, agent, now=NOW) == 0
    assert await rows(db, agent) == [] and await rows(db, other) == []


async def test_a_settling_rollup_still_counts_a_child_that_finished_before_the_window(db, agent):
    # A roll-up settles at SETTLE_DAYS (14) but the window is 15 days: a child that finished 2.5 days before its chain
    # closed is settled and out of the window by the time the chain settles. The pass reads its stored score instead.
    closed = NOW - timedelta(days=14, hours=1)

    async def chain(child_done: datetime) -> tuple[Subtask, uuid.UUID]:
        child = Subtask(
            id=uuid.uuid4(),
            agent_id=agent,
            task="t",
            status="completed",
            final_outcome="completed",
            completed_at=child_done,
        )
        rid = uuid.uuid4()
        async with db.session() as s:
            s.add(child)
            s.add(
                Intention(
                    id=rid,
                    agent_id=agent,
                    root_id=rid,
                    source_kind="subtask",
                    source_id=str(child.id),
                    intent="i",
                    origin_kind="interactive",
                    wake_policy="continue",
                    state="closed",
                    close_reason="delivered",
                    closed_at=closed,
                )
            )
            s.add(  # one harness error, so the child's score is not the 0.0 of a clean self-report
                ExecutionLedgerEntry(
                    agent_id=agent,
                    context_kind="subtask",
                    tool_name="bash",
                    side_effect_type="write",
                    status="error",
                    subtask_id=child.id,
                )
            )
            await s.commit()
        return child, rid

    kept, kept_root = await chain(NOW - timedelta(days=16, hours=12))  # settles at NOW-2d, out of the window at NOW
    never, never_root = await chain(NOW - timedelta(days=17, hours=12))  # outside the window at both passes

    await job.run_scoring_pass(db, agent, now=NOW - timedelta(days=2))
    by_task = {r.task_id: r for r in await rows(db, agent)}
    assert (by_task[str(kept_root)].settled, by_task[str(kept_root)].children_scored) == (False, 1)
    assert by_task[str(never_root)].children_scored == 0
    stored = by_task[str(kept.id)].score
    assert stored == -0.1
    assert by_task[str(kept.id)].settled  # so the pass at NOW has no provisional row to reach back for

    await job.run_scoring_pass(db, agent, now=NOW)  # the chains settle; neither child is in the window now
    by_task = {r.task_id: r for r in await rows(db, agent)}
    settled = by_task[str(kept_root)]
    assert (settled.settled, settled.children_scored, settled.children_mean_score) == (True, 1, stored)
    # A child that was never scored stays uncounted: no stored row, nothing made up.
    assert str(never.id) not in by_task
    control = by_task[str(never_root)]
    assert (control.settled, control.children_scored, control.children_mean_score) == (True, 0, None)


def _errored_subtask(agent, finished: datetime) -> tuple[Subtask, ExecutionLedgerEntry]:
    """A completed subtask with one harness error, so its fresh score is -0.1."""
    sub = Subtask(
        id=uuid.uuid4(), agent_id=agent, task="t", status="completed", final_outcome="completed", completed_at=finished
    )
    error = ExecutionLedgerEntry(
        agent_id=agent,
        context_kind="subtask",
        tool_name="bash",
        side_effect_type="write",
        status="error",
        subtask_id=sub.id,
    )
    return sub, error


async def test_a_provisional_row_past_the_window_is_rescored_and_settled(db, agent, caplog):
    # A missed pass: the row was written while the task was young and never rewritten as settled. The task is now
    # 17 days old, two days past the 15-day window start.
    finished = NOW - timedelta(days=17)
    sub, error = _errored_subtask(agent, finished)
    async with db.session() as s:
        s.add_all([sub, error])
        await s.commit()
    await upsert(db, agent, [score(str(sub.id), value=1.0, finished_at=finished)], at=NOW - timedelta(days=4))

    with caplog.at_level(logging.INFO, logger=job.__name__):
        assert await job.run_scoring_pass(db, agent, now=NOW) == 1
    [row] = await rows(db, agent)
    assert (row.settled, row.outcome, row.score, row.harness_errors) == (True, "done_self", -0.1, 1)  # recomputed
    assert abs(as_utc(row.scored_at) - NOW) < timedelta(seconds=1)
    assert any("reaches back to" in r.getMessage() for r in caplog.records)


async def test_without_a_stale_provisional_row_the_window_is_unchanged(db, agent, caplog):
    # A settled row 17 days old does not pull the window back, so a subtask 16 days old (never scored) stays out.
    old, old_error = _errored_subtask(agent, NOW - timedelta(days=17))
    out, out_error = _errored_subtask(agent, NOW - timedelta(days=16))
    inside, inside_error = _errored_subtask(agent, NOW - timedelta(days=3))
    async with db.session() as s:
        s.add_all([old, old_error, out, out_error, inside, inside_error])
        await s.commit()
    settled = score(str(old.id), settled=True, finished_at=old.completed_at)
    await upsert(db, agent, [settled], at=NOW - timedelta(days=3))

    with caplog.at_level(logging.INFO, logger=job.__name__):
        assert await job.run_scoring_pass(db, agent, now=NOW) == 1
        # and a provisional row inside the window does not pull it back either
        assert await job.run_scoring_pass(db, agent, now=NOW + timedelta(minutes=15)) == 1
    by_task = {r.task_id: r for r in await rows(db, agent)}
    assert set(by_task) == {str(old.id), str(inside.id)}
    assert by_task[str(old.id)].score == 1.0  # settled: left alone
    assert not by_task[str(inside.id)].settled
    assert not any("reaches back to" in r.getMessage() for r in caplog.records)


async def test_the_reach_back_stops_at_the_cap_and_older_provisional_rows_are_logged(db, agent, caplog):
    # A provisional row 45 days old (its task can no longer be settled by a pass) does not pull the window past
    # MAX_REACHBACK_DAYS: a subtask 40 days old stays unscored and the 45-day row is left as it is. A provisional row
    # inside the cap (20 days) still pulls the window back to itself and settles.
    assert job.MAX_REACHBACK_DAYS == 30
    ancient, ancient_error = _errored_subtask(agent, NOW - timedelta(days=45))
    unscored, unscored_error = _errored_subtask(agent, NOW - timedelta(days=40))
    stale, stale_error = _errored_subtask(agent, NOW - timedelta(days=20))
    async with db.session() as s:
        s.add_all([ancient, ancient_error, unscored, unscored_error, stale, stale_error])
        await s.commit()
    await upsert(
        db,
        agent,
        [score(str(t.id), value=1.0, finished_at=t.completed_at) for t in (ancient, stale)],
        at=NOW - timedelta(days=50),
    )

    with caplog.at_level(logging.INFO, logger=job.__name__):
        assert await job.run_scoring_pass(db, agent, now=NOW) == 1
    by_task = {r.task_id: r for r in await rows(db, agent)}
    assert set(by_task) == {str(ancient.id), str(stale.id)}  # the 40-day subtask is out of the window
    assert (by_task[str(stale.id)].settled, by_task[str(stale.id)].score) == (True, -0.1)
    left = by_task[str(ancient.id)]
    assert (left.settled, left.score, left.outcome) == (False, 1.0, "done_verified")  # untouched
    [warning] = [r for r in caplog.records if r.levelno == logging.WARNING]
    message = warning.getMessage()
    assert "1 provisional row(s)" in message and (NOW - timedelta(days=45)).date().isoformat() in message, message
    [reach] = [r.getMessage() for r in caplog.records if "reaches back to" in r.getMessage()]
    assert (NOW - timedelta(days=20)).date().isoformat() in reach, reach


async def test_the_number_of_queries_of_a_pass_does_not_grow_with_the_number_of_tasks(db, agent):
    async def count(n: int) -> int:
        a = f"{agent}-{n}"
        stale: list[Subtask] = []
        async with db.session() as s:
            for i in range(n):
                dag = ExecutionDAG(id=uuid.uuid4(), agent_id=a, name="d", status="completed", completed_at=NOW)
                node = DAGNode(id=uuid.uuid4(), dag_id=dag.id, name="n", node_type="subtask")
                sub, error = _errored_subtask(a, NOW - timedelta(hours=1))
                sub.dag_node_id = node.id
                old, old_error = _errored_subtask(a, NOW - timedelta(days=17, hours=i))
                s.add(dag)
                await s.flush()
                s.add(node)
                await s.flush()
                s.add_all([sub, error, old, old_error])
                stale.append(old)
            await s.commit()
        # every stale task left a provisional row behind, so the window reaches back in both runs
        await upsert(db, a, [score(str(t.id), finished_at=t.completed_at) for t in stale], at=NOW - timedelta(days=5))

        statements: list[str] = []

        def record(conn, cursor, statement, parameters, context, executemany):
            statements.append(statement)

        event.listen(db.engine.sync_engine, "before_cursor_execute", record)
        try:
            await job.run_scoring_pass(db, a, now=NOW)
        finally:
            event.remove(db.engine.sync_engine, "before_cursor_execute", record)
        got = {r.task_id: r for r in await rows(db, a)}
        assert len(got) == 3 * n  # n subtasks in the window, their n DAG runs, and the n stale subtasks
        assert all(got[str(t.id)].settled and got[str(t.id)].score == -0.1 for t in stale)
        return len(statements)

    assert await count(2) == await count(12)


class _Shared:
    """The shared test database with a disconnect that leaves the pool alone."""

    def __init__(self, db):
        self._db = db

    def session(self):
        return self._db.session()

    async def connect(self):
        return None

    async def disconnect(self):
        return None


async def test_the_command_scores_the_agent_it_is_given(db, agent, monkeypatch):
    sub = Subtask(
        id=uuid.uuid4(),
        agent_id=agent,
        task="t",
        status="completed",
        final_outcome="completed",
        completed_at=datetime.now(UTC) - timedelta(hours=1),
    )
    async with db.session() as s:
        s.add(sub)
        await s.commit()
    monkeypatch.setattr(job, "_open_database", lambda settings: _Shared(db))

    assert await job._run(agent, 15) == 0
    assert [r.task_id for r in await rows(db, agent)] == [str(sub.id)]


async def _done_subtask(db, agent) -> Subtask:
    sub = Subtask(
        id=uuid.uuid4(),
        agent_id=agent,
        task="t",
        status="completed",
        final_outcome="completed",
        completed_at=datetime.now(UTC) - timedelta(hours=1),
    )
    async with db.session() as s:
        s.add(sub)
        await s.commit()
    return sub


async def test_the_command_builds_its_settings_once_and_scores_their_agent(db, agent, monkeypatch):
    import nous.config
    import nous.storage.database

    made: list[object] = []
    opened: list[object] = []

    class OneSettings:
        def __init__(self):
            self.agent_id = agent
            made.append(self)

    def database(settings):
        opened.append(settings)
        return _Shared(db)

    monkeypatch.setattr(nous.config, "Settings", OneSettings)
    monkeypatch.setattr(nous.storage.database, "Database", database)
    sub = await _done_subtask(db, agent)

    assert await job._run(None, 15) == 0  # no --agent-id: the agent comes from the settings
    assert len(made) == 1 and opened == made
    assert [r.task_id for r in await rows(db, agent)] == [str(sub.id)]


async def test_the_log_counts_the_rows_submitted_by_kind(db, agent, monkeypatch, caplog):
    await _done_subtask(db, agent)
    async with db.session() as s:
        s.add(
            ExecutionDAG(
                id=uuid.uuid4(),
                agent_id=agent,
                name="d",
                status="completed",
                completed_at=datetime.now(UTC) - timedelta(hours=1),
            )
        )
        await s.commit()
    real = job.score_rollup

    def no_dag_rollup(signals, leaf_scores, now):  # a roll-up the scorer declines (score_rollup returns None)
        return None if signals.task_kind == "dag" else real(signals, leaf_scores, now)

    monkeypatch.setattr(job, "score_rollup", no_dag_rollup)
    with caplog.at_level(logging.INFO, logger=job.__name__):
        assert await job.run_scoring_pass(db, agent) == 1
    [line] = [r.getMessage() for r in caplog.records if r.getMessage().startswith("Task scores:")]
    assert "submitted 1 row(s): 1 subtask(s), 0 DAG run(s), 0 intention root(s)" in line, line


class _DisconnectFails(_Shared):
    async def disconnect(self):
        raise RuntimeError("the pool is already gone")


async def test_a_failing_disconnect_after_a_committed_pass_still_exits_zero(db, agent, monkeypatch, caplog):
    sub = await _done_subtask(db, agent)
    monkeypatch.setattr(job, "_open_database", lambda settings: _DisconnectFails(db))

    with caplog.at_level(logging.INFO, logger=job.__name__):
        assert await job._run(agent, 15) == 0
    assert [r.task_id for r in await rows(db, agent)] == [str(sub.id)]  # the pass committed
    assert [r.levelname for r in caplog.records if "disconnect" in r.getMessage()] == ["WARNING"]


async def test_a_failed_pass_is_not_masked_by_a_failing_disconnect(monkeypatch):
    class Broken(_DisconnectFails):
        def session(self):
            raise RuntimeError("database went away")

    monkeypatch.setattr(job, "_open_database", lambda settings: Broken(None))
    with pytest.raises(RuntimeError, match="database went away"):
        await job._run("nous-default", 15)


def test_the_exit_code_is_zero_after_a_pass_and_nonzero_after_a_failure(monkeypatch):
    async def passed(agent_id, window_days):
        assert (agent_id, window_days) == ("nous-default", 7)
        return 0

    async def failed(agent_id, window_days):
        raise RuntimeError("database went away")

    monkeypatch.setattr(job, "_run", passed)
    assert job.main(["--agent-id", "nous-default", "--window-days", "7"]) == 0
    monkeypatch.setattr(job, "_run", failed)
    assert job.main(["--agent-id", "nous-default", "--window-days", "7"]) == 1


def test_the_command_refuses_a_bad_argument_with_a_usage_error():
    with pytest.raises(SystemExit) as exit_info:
        job.main(["--window-days", "many"])
    assert exit_info.value.code == 2


async def test_a_committed_pass_leaves_one_ok_heartbeat_that_the_next_pass_overwrites(db, agent):
    async with db.session() as s:
        s.add_all(_errored_subtask(agent, NOW - timedelta(hours=1)))
        await s.commit()
    started = datetime.now(UTC)
    assert await job.run_scoring_pass(db, agent, now=NOW) == 1
    [hb] = await heartbeats(db, agent)
    assert (hb.scorer_version, hb.last_status, hb.last_error, hb.rows_submitted) == (SCORER_VERSION, "ok", None, 1)
    # stamped when written, not with the pass's now: a slow pass must not record a run that already looks stale
    assert as_utc(hb.last_run_at) == as_utc(hb.last_ok_at)
    assert started <= as_utc(hb.last_run_at) <= datetime.now(UTC)
    async with db.session() as s:  # one more task, so the overwrite shows in rows_submitted as well
        s.add_all(_errored_subtask(agent, NOW))
        await s.commit()
    started = datetime.now(UTC)
    assert await job.run_scoring_pass(db, agent, now=NOW + timedelta(minutes=15)) == 2
    [hb] = await heartbeats(db, agent)
    assert (hb.last_status, hb.last_error, hb.rows_submitted) == ("ok", None, 2)
    assert as_utc(hb.last_run_at) == as_utc(hb.last_ok_at)
    assert started <= as_utc(hb.last_run_at) <= datetime.now(UTC)


class _PassBroke(Exception):
    """A failure inside a pass."""


class _HeartbeatBroke(Exception):
    """A failure of the heartbeat write after a failed pass."""


def _fail_after_the_upsert(monkeypatch) -> list[int]:
    """The pass sends its scores, then fails before it commits. Returns the row counts it sent."""
    real = job.upsert_scores
    sent: list[int] = []

    async def upsert_then_fail(session, agent_id, scores, *, scored_at):
        sent.append(await real(session, agent_id, scores, scored_at=scored_at))
        raise _PassBroke("after the upsert")

    monkeypatch.setattr(job, "upsert_scores", upsert_then_fail)
    return sent


def _raise(exc: Exception):
    async def fail(*args, **kwargs):
        raise exc

    return fail


async def test_a_failed_pass_leaves_an_error_heartbeat_and_none_of_its_scores(db, agent, monkeypatch, caplog):
    first, first_error = _errored_subtask(agent, NOW - timedelta(hours=1))
    async with db.session() as s:
        s.add_all([first, first_error])
        await s.commit()
    assert await job.run_scoring_pass(db, agent, now=NOW) == 1
    [scored] = await rows(db, agent)
    [ok] = await heartbeats(db, agent)
    last_ok = as_utc(ok.last_ok_at)

    async with db.session() as s:
        s.add_all(_errored_subtask(agent, datetime.now(UTC) - timedelta(hours=1)))
        await s.commit()
    sent = _fail_after_the_upsert(monkeypatch)
    monkeypatch.setattr(job, "_open_database", lambda settings: _Shared(db))
    started = datetime.now(UTC)
    with caplog.at_level(logging.INFO, logger=job.__name__), pytest.raises(_PassBroke):
        await job._run(agent, 15)

    assert sent and sent[0] >= 1  # the pass did send rows: they were rolled back, not never written
    [kept] = await rows(db, agent)
    assert (kept.task_id, kept.score, as_utc(kept.scored_at)) == (scored.task_id, scored.score, NOW)
    [hb] = await heartbeats(db, agent)
    assert (hb.last_status, hb.last_error, hb.scorer_version) == ("error", "_PassBroke", SCORER_VERSION)
    assert started <= as_utc(hb.last_run_at) <= datetime.now(UTC)
    # the last committed pass is still on record
    assert (as_utc(hb.last_ok_at), hb.rows_submitted) == (last_ok, 1)
    assert not any("heartbeat" in r.getMessage() for r in caplog.records)


async def test_the_scores_and_the_ok_heartbeat_commit_together_or_not_at_all(db, agent, monkeypatch):
    # The SQLite-lane twin of the role test without the heartbeat grant: an ok heartbeat that cannot be written takes
    # the pass's scores with it, because both are one transaction.
    async with db.session() as s:
        s.add_all(_errored_subtask(agent, NOW - timedelta(hours=1)))
        await s.commit()
    real = job.upsert_heartbeat

    async def ok_heartbeat_fails(session, agent_id, *, written_at, submitted=None, error=None):
        if error is None:
            raise _HeartbeatBroke("the ok heartbeat")
        await real(session, agent_id, written_at=written_at, submitted=submitted, error=error)

    monkeypatch.setattr(job, "upsert_heartbeat", ok_heartbeat_fails)
    with pytest.raises(_HeartbeatBroke):
        await job.run_scoring_pass(db, agent, now=NOW)
    assert await rows(db, agent) == []
    assert await heartbeats(db, agent) == []


async def test_a_first_pass_that_fails_leaves_an_error_heartbeat_with_nothing_committed_yet(db, agent, monkeypatch):
    failed_at: list[datetime] = []

    async def gatherer_fails(*args, **kwargs):
        await asyncio.sleep(0.05)  # well above any clock's granularity, so a start-time stamp would be earlier
        failed_at.append(datetime.now(UTC))
        raise _PassBroke("the gatherer failed")

    monkeypatch.setattr(job, "gather_signals", gatherer_fails)
    monkeypatch.setattr(job, "_open_database", lambda settings: _Shared(db))
    with pytest.raises(_PassBroke):
        await job._run(agent, 15)
    [hb] = await heartbeats(db, agent)
    assert (hb.last_status, hb.last_error, hb.last_ok_at, hb.rows_submitted) == ("error", "_PassBroke", None, None)
    assert as_utc(hb.last_run_at) >= failed_at[0]  # stamped when written, after the pass failed, not when it started


async def test_the_error_is_the_exception_class_name_only(db, agent, monkeypatch):
    class CarriesData(Exception):
        pass

    long_name = type("E" * 200, (Exception,), {})
    for exc, expected in (
        (CarriesData("token=abc in the message"), "CarriesData"),  # the message stays in the log
        (long_name("x"), "E" * 120),  # truncated to the column
    ):
        monkeypatch.setattr(job, "gather_signals", _raise(exc))
        monkeypatch.setattr(job, "_open_database", lambda settings: _Shared(db))
        with pytest.raises(type(exc)):
            await job._run(agent, 15)
        [hb] = await heartbeats(db, agent)
        assert hb.last_error == expected


def test_when_the_heartbeat_cannot_be_written_either_the_pass_error_is_the_one_reported(monkeypatch, caplog):
    class Unreachable(_Shared):
        def __init__(self):
            self.calls = 0

        def session(self):
            self.calls += 1
            raise _PassBroke("the pass") if self.calls == 1 else _HeartbeatBroke("the heartbeat")

    database = Unreachable()
    monkeypatch.setattr(job, "_open_database", lambda settings: database)
    with caplog.at_level(logging.INFO, logger=job.__name__):
        assert job.main(["--agent-id", "nous-default"]) == 1
    assert database.calls == 2  # the pass, then the heartbeat
    records = [r for r in caplog.records if r.name == job.__name__]
    [failed] = [r for r in records if r.levelno == logging.ERROR]
    assert failed.getMessage() == "Task scores: the scoring pass failed"
    assert failed.exc_info[0] is _PassBroke  # the pass's error, not the heartbeat's
    [warning] = [r for r in records if r.levelno == logging.WARNING]
    assert warning.getMessage() == "Task scores: the heartbeat could not be written after a failed pass"
    assert warning.exc_info[0] is _HeartbeatBroke


async def test_a_pass_for_one_agent_leaves_another_agents_heartbeat_alone(db, agent, monkeypatch):
    other = f"{agent}-other"
    await job.run_scoring_pass(db, other, now=NOW)
    await job.run_scoring_pass(db, agent, now=NOW + timedelta(hours=1))  # an ok pass
    [before] = await heartbeats(db, other)
    their_run = as_utc(before.last_run_at)
    [ok] = await heartbeats(db, agent)
    my_ok = as_utc(ok.last_ok_at)
    monkeypatch.setattr(job, "gather_signals", _raise(_PassBroke("the gatherer failed")))
    monkeypatch.setattr(job, "_open_database", lambda settings: _Shared(db))
    with pytest.raises(_PassBroke):
        await job._run(agent, 15)  # and a failed one

    [theirs] = await heartbeats(db, other)
    assert (theirs.last_status, as_utc(theirs.last_run_at), theirs.rows_submitted) == ("ok", their_run, 0)
    [mine] = await heartbeats(db, agent)
    assert (mine.last_status, as_utc(mine.last_ok_at)) == ("error", my_ok)


# Statements a scoring role holding only SELECT on the sources and INSERT/UPDATE on the scores can run.
_FORBIDDEN = re.compile(
    r"\b(CREATE|DROP|ALTER|TRUNCATE|LOCK|GRANT|REVOKE|DELETE|TEMP|TEMPORARY|VACUUM|ANALYZE|LISTEN|NOTIFY)\b"
    r"|pg_advisory|nextval",
    re.IGNORECASE,
)


async def _seed_one_of_each(db, agent):
    """A subtask and a chain that reach every read of a pass: each of SOURCE_TABLES, and SCORE_TABLE itself."""
    sub = Subtask(
        id=uuid.uuid4(),
        agent_id=agent,
        task="t",
        status="completed",
        final_outcome="completed",
        completed_at=datetime.now(UTC) - timedelta(hours=1),
    )
    # A child of the chain that finished before the window: the pass looks up its stored score in SCORE_TABLE.
    early = Subtask(
        id=uuid.uuid4(),
        agent_id=agent,
        task="t",
        status="completed",
        final_outcome="completed",
        completed_at=datetime.now(UTC) - timedelta(days=16),
    )
    # The owner's grade on the decision that spawned the subtask: the grade path reads brain.decisions.
    grade = Decision(
        id=uuid.uuid4(),
        agent_id=agent,
        description="d",
        confidence=0.5,
        category="process",
        stakes="low",
        outcome="success",
        reviewer="a2ui",
    )
    rid = uuid.uuid4()
    async with db.session() as s:
        s.add_all([sub, early, grade])
        s.add(
            Intention(  # depth 0 and sourced from the subtask: it links the subtask to its origin decision
                id=rid,
                agent_id=agent,
                root_id=rid,
                source_kind="subtask",
                source_id=str(sub.id),
                origin_decision_id=grade.id,
                intent="i",
                origin_kind="interactive",
                wake_policy="continue",
                state="closed",
                close_reason="delivered",
                closed_at=datetime.now(UTC) - timedelta(minutes=30),
            )
        )
        s.add(
            Intention(
                id=uuid.uuid4(),
                agent_id=agent,
                root_id=rid,
                parent_id=rid,
                depth=1,
                source_kind="subtask",
                source_id=str(early.id),
                intent="c",
                origin_kind="continuation",
                wake_policy="continue",
                authority="internal_only",
                state="closed",
                close_reason="resolved",
                closed_at=early.completed_at,
            )
        )
        s.add(
            ExecutionLedgerEntry(
                agent_id=agent,
                context_kind="subtask",
                tool_name="bash",
                side_effect_type="write",
                status="success",
                result_summary="exit code 1",
                subtask_id=sub.id,
            )
        )
        await s.commit()


async def test_a_pass_issues_only_selects_and_the_scores_upsert(db, agent):
    await _seed_one_of_each(db, agent)
    statements: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(db.engine.sync_engine, "before_cursor_execute", record)
    try:
        assert await job.run_scoring_pass(db, agent) == 2
    finally:
        event.remove(db.engine.sync_engine, "before_cursor_execute", record)

    assert statements
    reads: set[str] = set()
    writes: set[str] = set()
    for statement in statements:
        head = statement.lstrip().split(None, 1)[0].upper()
        assert head in ("SELECT", "INSERT"), statement
        assert not _FORBIDDEN.search(statement), statement  # no DDL, no temp table, no advisory lock, no delete
        # table names with the schema stripped: the SQLite lane maps every schema to "main"
        reads |= {name.split(".")[-1] for name in re.findall(r"\b(?:FROM|JOIN)\s+([\w.]+)", statement, re.I)}
        if head == "INSERT":  # the only writes are the scores upsert and the heartbeat upsert
            writes |= {name.split(".")[-1] for name in re.findall(r"\bINSERT\s+INTO\s+([\w.]+)", statement, re.I)}

    # The privilege contract is derived, not copied: the tables the pass reads are exactly SOURCE_TABLES plus
    # SCORE_TABLE (the oldest provisional row, the stored scores of children that finished before the window), and the
    # only tables it writes are SCORE_TABLE and HEARTBEAT_TABLE (an upsert with no FROM or JOIN, so not a read here).
    # Dropping a table from SOURCE_TABLES, granting one the pass never reads, or pointing SCORE_TABLE or
    # HEARTBEAT_TABLE elsewhere fails here on the SQLite lane as well as on Postgres.
    models_say: dict[str, set[str]] = {}
    for table in Base.metadata.tables.values():
        models_say.setdefault(table.name, set()).add(table.fullname)  # the schema the models put each table in
    assert reads, "the pass read nothing"
    write_tables = {fullname for name in writes for fullname in models_say[name]}
    assert write_tables == {job.SCORE_TABLE, job.HEARTBEAT_TABLE}, write_tables
    read_tables = {fullname for name in reads for fullname in models_say[name]}
    granted = set(job.SOURCE_TABLES) | {job.SCORE_TABLE}
    assert read_tables == granted, (
        f"read but not granted: {sorted(read_tables - granted)}; granted but not read: {sorted(granted - read_tables)}"
    )


@asynccontextmanager
async def _scoring_role(db, *, written: tuple[str, ...]):
    """A role holding the scoring privilege contract with write on ``written`` only, and a stand-in for the database
    whose sessions run as it. Dropped afterwards."""
    role = f"scorer_probe_{uuid.uuid4().hex[:8]}"

    class AsRole:
        """Sessions on one pooled connection each, switched to the probe role for the session's length and reset before
        the connection goes back to the pool."""

        @asynccontextmanager
        async def session(self):
            # One connection for the whole session. A session bound to the engine gives its connection back to the
            # pool at each commit, so a RESET ROLE after the pass's commit ran on whichever connection the pool handed
            # out next and could leave this one in the pool still switched to the role, for later tests to inherit.
            async with db.engine.connect() as conn:
                await conn.execute(text(f"SET ROLE {role}"))
                await conn.commit()  # no transaction left open for the session to join: its commit must COMMIT
                try:
                    async with AsyncSession(bind=conn, expire_on_commit=False) as s:
                        yield s
                finally:
                    await conn.rollback()
                    await conn.execute(text("RESET ROLE"))
                    await conn.commit()

    async with db.engine.begin() as conn:
        await conn.execute(text(f"CREATE ROLE {role} NOLOGIN"))
        await conn.execute(text(f"GRANT {role} TO CURRENT_USER"))
        for schema in {name.split(".")[0] for name in (*job.SOURCE_TABLES, *written)}:
            await conn.execute(text(f"GRANT USAGE ON SCHEMA {schema} TO {role}"))
        for table in job.SOURCE_TABLES:
            await conn.execute(text(f"GRANT SELECT ON {table} TO {role}"))
        for table in written:
            await conn.execute(text(f"GRANT SELECT, INSERT, UPDATE ON {table} TO {role}"))
    try:
        yield AsRole()
    finally:
        async with db.engine.begin() as conn:
            await conn.execute(text(f"DROP OWNED BY {role}"))
            await conn.execute(text(f"DROP ROLE {role}"))


@pytest.mark.postgres_only
async def test_a_pass_runs_as_a_role_with_only_select_on_the_sources_and_write_on_the_scores(db, agent):
    async with _scoring_role(db, written=(job.SCORE_TABLE, job.HEARTBEAT_TABLE)) as as_role:
        await _seed_one_of_each(db, agent)
        assert await job.run_scoring_pass(as_role, agent) == 2
        assert await job.run_scoring_pass(as_role, agent) == 2  # the second pass takes the ON CONFLICT path
        assert len(await rows(db, agent)) == 2
        [hb] = await heartbeats(db, agent)  # written by the role: every session of the pass ran as it
        assert (hb.last_status, hb.rows_submitted) == ("ok", 2)

        # and the role can do nothing more: no write elsewhere, no DDL, no delete of a score or a heartbeat
        for statement in (
            "INSERT INTO nous_system.events (agent_id, event_type, data) VALUES ('x', 'x', '{}')",
            "CREATE TABLE scoring.not_allowed (id int)",
            "DELETE FROM scoring.task_scores",
            "DELETE FROM scoring.scorer_heartbeat",
        ):
            with pytest.raises(ProgrammingError):
                async with as_role.session() as s:
                    await s.execute(text(statement))


@pytest.mark.postgres_only
async def test_without_the_heartbeat_grant_a_pass_fails_and_its_scores_do_not_commit(db, agent):
    async with _scoring_role(db, written=(job.SCORE_TABLE,)) as as_role:
        await _seed_one_of_each(db, agent)
        with pytest.raises(ProgrammingError, match="permission denied for table scorer_heartbeat"):
            await job.run_scoring_pass(as_role, agent)
    assert await rows(db, agent) == []  # the scores and the heartbeat are one transaction
    assert await heartbeats(db, agent) == []
