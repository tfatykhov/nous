"""Task scoring v1 (autonomy plan v2, A1): the upsert, one scoring pass, and the command."""

from __future__ import annotations

import re
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import event, select, text
from sqlalchemy.exc import IntegrityError, ProgrammingError

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
    # A roll-up settles at SETTLE_DAYS (14) but the window is 15 days: a child that finished 1.5 days before its chain
    # closed is out of the window by the time the chain settles. The pass reads that child's stored score instead.
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

    kept, kept_root = await chain(NOW - timedelta(days=15, hours=12))  # inside the window at NOW-2d, outside at NOW
    never, never_root = await chain(NOW - timedelta(days=17, hours=12))  # outside the window at both passes

    await job.run_scoring_pass(db, agent, now=NOW - timedelta(days=2))
    by_task = {r.task_id: r for r in await rows(db, agent)}
    assert (by_task[str(kept_root)].settled, by_task[str(kept_root)].children_scored) == (False, 1)
    assert by_task[str(never_root)].children_scored == 0
    stored = by_task[str(kept.id)].score
    assert stored == -0.1

    await job.run_scoring_pass(db, agent, now=NOW)  # the chains settle; neither child is in the window now
    by_task = {r.task_id: r for r in await rows(db, agent)}
    settled = by_task[str(kept_root)]
    assert (settled.settled, settled.children_scored, settled.children_mean_score) == (True, 1, stored)
    # A child that was never scored stays uncounted: no stored row, nothing made up.
    assert str(never.id) not in by_task
    control = by_task[str(never_root)]
    assert (control.settled, control.children_scored, control.children_mean_score) == (True, 0, None)


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
    monkeypatch.setattr(job, "_open_database", lambda: _Shared(db))

    assert await job._run(agent, 15) == 0
    assert [r.task_id for r in await rows(db, agent)] == [str(sub.id)]


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
        if head == "INSERT":  # the only write is the scores upsert
            writes |= {name.split(".")[-1] for name in re.findall(r"\bINSERT\s+INTO\s+([\w.]+)", statement, re.I)}

    # The privilege contract is derived, not copied: the tables the pass reads are exactly SOURCE_TABLES plus
    # SCORE_TABLE (the stored scores of children that finished before the window), and the only table it writes is
    # SCORE_TABLE. Dropping a table from SOURCE_TABLES, granting one the pass never reads, or pointing SCORE_TABLE
    # elsewhere fails here on the SQLite lane as well as on Postgres.
    models_say: dict[str, set[str]] = {}
    for table in Base.metadata.tables.values():
        models_say.setdefault(table.name, set()).add(table.fullname)  # the schema the models put each table in
    assert reads, "the pass read nothing"
    assert writes == {job.SCORE_TABLE.split(".")[-1]}, writes
    for name in sorted(writes):
        assert models_say[name] == {job.SCORE_TABLE}, (name, models_say[name])
    read_tables = {fullname for name in reads for fullname in models_say[name]}
    granted = set(job.SOURCE_TABLES) | {job.SCORE_TABLE}
    assert read_tables == granted, (
        f"read but not granted: {sorted(read_tables - granted)}; granted but not read: {sorted(granted - read_tables)}"
    )


@pytest.mark.postgres_only
async def test_a_pass_runs_as_a_role_with_only_select_on_the_sources_and_write_on_the_scores(db, agent):
    role = f"scorer_probe_{uuid.uuid4().hex[:8]}"

    class AsRole:
        """The pool, with every session switched to the probe role for the length of the pass."""

        @asynccontextmanager
        async def session(self):
            async with db.session() as s:
                await s.execute(text(f"SET ROLE {role}"))
                try:
                    yield s
                finally:
                    await s.rollback()
                    await s.execute(text("RESET ROLE"))
                    await s.commit()

    async with db.engine.begin() as conn:
        await conn.execute(text(f"CREATE ROLE {role} NOLOGIN"))
        await conn.execute(text(f"GRANT {role} TO CURRENT_USER"))
        for schema in {name.split(".")[0] for name in (*job.SOURCE_TABLES, job.SCORE_TABLE)}:
            await conn.execute(text(f"GRANT USAGE ON SCHEMA {schema} TO {role}"))
        for table in job.SOURCE_TABLES:
            await conn.execute(text(f"GRANT SELECT ON {table} TO {role}"))
        await conn.execute(text(f"GRANT SELECT, INSERT, UPDATE ON {job.SCORE_TABLE} TO {role}"))
    try:
        await _seed_one_of_each(db, agent)
        assert await job.run_scoring_pass(AsRole(), agent) == 2
        assert await job.run_scoring_pass(AsRole(), agent) == 2  # the second pass takes the ON CONFLICT path
        assert len(await rows(db, agent)) == 2

        # and the role can do nothing more: no write elsewhere, no DDL, no delete of a score
        for statement in (
            "INSERT INTO nous_system.events (agent_id, event_type, data) VALUES ('x', 'x', '{}')",
            "CREATE TABLE scoring.not_allowed (id int)",
            "DELETE FROM scoring.task_scores",
        ):
            with pytest.raises(ProgrammingError):
                async with AsRole().session() as s:
                    await s.execute(text(statement))
    finally:
        async with db.engine.begin() as conn:
            await conn.execute(text(f"DROP OWNED BY {role}"))
            await conn.execute(text(f"DROP ROLE {role}"))
