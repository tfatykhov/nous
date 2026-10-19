"""Scorer v5: the weekly sampler of completion checks (``nous/audits/check_audit.py``)."""

from __future__ import annotations

import random
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from nous.audits.check_audit import (
    AUDIT_HOUR_UTC,
    AUDIT_WEEKDAY,
    CheckAuditSampler,
    round_robin,
    week_start,
)
from nous.storage.models import CheckAudit, DAGNode, ExecutionDAG, Subtask

MONDAY_10H = datetime(2026, 10, 19, 10, 0, tzinfo=UTC)  # a Monday
CHECKS = {
    "file_exists": "test -f /tmp/report.md",
    "job_status": "claude-jobs status JOB_ID",
    "pr_state": "gh pr view 12 --json state",
    "script": "bash check_report.sh",
    "pr": "python -m nous.dag.typed_checks pr owner/repo --pr 12",
}


class FakeSurfaces:
    def __init__(self, fail: int = 0) -> None:
        self.fail = fail
        self.pushed: list[tuple] = []

    async def push_built(self, built, **kw) -> str:
        if self.fail:
            self.fail -= 1
            raise RuntimeError("push refused")
        self.pushed.append((built, kw))
        return f"surface-{len(self.pushed)}"


@pytest_asyncio.fixture
async def agent() -> str:
    return f"test-sampler-{uuid.uuid4().hex[:8]}"


def make(db, settings, agent, surfaces, *, now=MONDAY_10H, enabled=True, size=5, seed=1):
    cfg = settings.model_copy(update={"agent_id": agent, "check_audit_enabled": enabled, "check_audit_size": size})
    return CheckAuditSampler(db, agent, surfaces, cfg, clock=lambda: now, rng=random.Random(seed))


async def add(db, *rows) -> None:
    async with db.session() as s:
        s.add_all(rows)
        await s.commit()


async def finished(db, agent, check, *, age_days=1.0, node_status="completed", current=True, final="completed"):
    """A subtask reported done in a DAG node that ran ``check``; returns the subtask."""
    dag = ExecutionDAG(id=uuid.uuid4(), agent_id=agent, name="d", status="completed", completed_at=MONDAY_10H)
    sub = Subtask(
        id=uuid.uuid4(),
        agent_id=agent,
        task="summarise the weekly report",
        result="The summary is in /tmp/report.md.",
        status="completed",
        final_outcome=final,
        completed_at=MONDAY_10H - timedelta(days=age_days),
    )
    node = DAGNode(
        id=uuid.uuid4(),
        dag_id=dag.id,
        name=f"n-{sub.id.hex[:6]}",
        node_type="subtask",
        status=node_status,
        completion_check=check,
        check_attempts=1,
        last_check_at=MONDAY_10H,
        subtask_id=sub.id if current else uuid.uuid4(),
    )
    sub.dag_node_id = node.id
    await add(db, dag)
    await add(db, node, sub)
    return sub


async def audit_rows(db, agent) -> list[CheckAudit]:
    async with db.session() as s:
        return list((await s.execute(select(CheckAudit).where(CheckAudit.agent_id == agent))).scalars())


# ---- pure ------------------------------------------------------------------------------------------------------


def test_the_trigger_constants_and_the_week_start():
    assert (AUDIT_WEEKDAY, AUDIT_HOUR_UTC) == (0, 9)
    assert week_start(datetime(2026, 10, 22, 15, 30, tzinfo=UTC)) == datetime(2026, 10, 19, tzinfo=UTC)
    assert week_start(MONDAY_10H) == datetime(2026, 10, 19, tzinfo=UTC)


def test_round_robin_takes_one_kind_at_a_time_and_stops_at_the_size():
    by_kind = {"a": [1, 2, 3], "b": [4], "c": [5, 6]}
    got = round_robin(by_kind, 4, random.Random(3))
    assert len(got) == 4
    assert {1, 2, 3} & set(got) and 4 in got and {5, 6} & set(got)  # every kind shows up before any repeats
    assert sorted(round_robin(by_kind, 99, random.Random(3))) == [1, 2, 3, 4, 5, 6]
    assert by_kind == {"a": [1, 2, 3], "b": [4], "c": [5, 6]}  # the input is not consumed


def test_the_same_seed_picks_the_same_items():
    by_kind = {"a": list(range(10)), "b": list(range(10, 20))}
    assert round_robin(by_kind, 5, random.Random(7)) == round_robin(by_kind, 5, random.Random(7))


# ---- when it runs ----------------------------------------------------------------------------------------------


async def test_nothing_happens_off_monday_before_nine_or_when_disabled(db, settings, agent):
    await finished(db, agent, CHECKS["file_exists"])
    for kw in (
        {"now": MONDAY_10H + timedelta(days=1)},  # Tuesday
        {"now": MONDAY_10H.replace(hour=AUDIT_HOUR_UTC - 1, minute=59)},  # before 09 UTC
        {"enabled": False},
    ):
        surfaces = FakeSurfaces()
        assert await make(db, settings, agent, surfaces, **kw).tick() == 0
        assert surfaces.pushed == []
    assert await audit_rows(db, agent) == []


async def test_it_runs_at_nine_sharp_on_monday(db, settings, agent):
    await finished(db, agent, CHECKS["file_exists"])
    surfaces = FakeSurfaces()
    assert await make(db, settings, agent, surfaces, now=MONDAY_10H.replace(hour=9, minute=0)).tick() == 1


async def test_one_card_a_week_even_across_ticks_and_a_new_sampler(db, settings, agent):
    await finished(db, agent, CHECKS["file_exists"])
    await finished(db, agent, CHECKS["script"])
    surfaces = FakeSurfaces()
    assert await make(db, settings, agent, surfaces, size=1).tick() == 1
    again = make(db, settings, agent, surfaces, size=1, now=MONDAY_10H + timedelta(hours=5))  # a restart, later
    assert await again.tick() == 0
    assert len(surfaces.pushed) == 1
    await finished(db, agent, CHECKS["pr_state"], age_days=-6.5)  # finished during the week after
    next_week = make(db, settings, agent, surfaces, size=1, now=MONDAY_10H + timedelta(days=7))
    assert await next_week.tick() == 1
    assert len(surfaces.pushed) == 2


async def test_a_failed_push_leaves_the_rows_unpushed_and_a_later_tick_reoffers_them(db, settings, agent):
    sub = await finished(db, agent, CHECKS["file_exists"])
    surfaces = FakeSurfaces(fail=1)
    sampler = make(db, settings, agent, surfaces)
    with pytest.raises(RuntimeError):
        await sampler.tick()
    (row,) = await audit_rows(db, agent)
    assert (row.task_id, row.surface_id, row.verdict) == (sub.id, None, None)

    assert await sampler.tick() == 1  # the same week: not done, so it is offered again
    (again,) = await audit_rows(db, agent)
    assert again.id == row.id  # the unpushed row was reused, not duplicated
    assert again.surface_id == "surface-1"
    assert str(row.id) in surfaces.pushed[0][0].data_model["items"]


async def test_the_loop_survives_a_failed_pass(db, settings, agent, monkeypatch):
    import asyncio

    from nous.audits import check_audit as module

    sampler = make(db, settings, agent, FakeSurfaces())
    calls = []

    async def tick():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("first pass fails")

    real_sleep = asyncio.sleep

    async def sleep(_):
        if len(calls) >= 2:
            asyncio.current_task().cancel()  # a real shutdown request: the loop must stop on it
        await real_sleep(0)

    monkeypatch.setattr(sampler, "tick", tick)
    monkeypatch.setattr(module.asyncio, "sleep", sleep)
    await asyncio.wait_for(sampler.run(), timeout=10)
    assert len(calls) == 2


# ---- who is eligible -------------------------------------------------------------------------------------------


async def test_only_current_attempt_passed_non_pr_recent_unoffered_subtasks_are_sampled(db, settings, agent):
    ok = await finished(db, agent, CHECKS["file_exists"])
    await finished(db, agent, CHECKS["pr"])  # a typed check is never sampled
    await finished(db, agent, CHECKS["file_exists"], age_days=8)  # too old
    await finished(db, agent, CHECKS["file_exists"], node_status="failed")  # its check did not pass
    await finished(db, agent, CHECKS["file_exists"], current=False)  # an earlier attempt of its node
    await finished(db, agent, CHECKS["file_exists"], final="incomplete_blocked")  # not a raw done
    await finished(db, agent, None)  # no check at all
    pushed = await finished(db, agent, CHECKS["file_exists"])
    answered = await finished(db, agent, CHECKS["file_exists"])
    await add(
        db,
        CheckAudit(
            agent_id=agent,
            task_id=pushed.id,
            node_id=uuid.uuid4(),
            check_kind="file_exists",
            surface_id="s",
            sampled_at=MONDAY_10H - timedelta(days=30),  # an old card, so this week is not yet done
        ),
        CheckAudit(
            agent_id=agent,
            task_id=answered.id,
            node_id=uuid.uuid4(),
            check_kind="file_exists",
            verdict="right",
            actor="companion:x",
            answered_at=MONDAY_10H,
        ),
    )
    surfaces = FakeSurfaces()
    sampler = make(db, settings, agent, surfaces)
    assert await sampler.tick() == 1
    rows = {r.task_id for r in await audit_rows(db, agent)}
    assert ok.id in rows and len(rows - {pushed.id, answered.id}) == 1
    assert pushed.id in rows and answered.id in rows  # their old rows are untouched, no new ones for them


async def test_it_spreads_over_kinds_and_stops_at_the_size(db, settings, agent):
    for name in ("file_exists", "job_status", "pr_state", "script"):
        for _ in range(4):
            await finished(db, agent, CHECKS[name])
    surfaces = FakeSurfaces()
    assert await make(db, settings, agent, surfaces, size=4, seed=5).tick() == 4
    kinds = sorted(r.check_kind for r in await audit_rows(db, agent))
    assert kinds == ["file_exists", "job_status", "pr_state", "script"]  # one of each, not four of one
    assert all(r.surface_id == "surface-1" for r in await audit_rows(db, agent))


async def test_fewer_qualifying_than_the_size_pushes_what_there_is(db, settings, agent):
    await finished(db, agent, CHECKS["file_exists"])
    await finished(db, agent, CHECKS["script"])
    surfaces = FakeSurfaces()
    assert await make(db, settings, agent, surfaces, size=5).tick() == 2


async def test_nothing_eligible_pushes_nothing(db, settings, agent):
    surfaces = FakeSurfaces()
    assert await make(db, settings, agent, surfaces).tick() == 0
    assert surfaces.pushed == []


async def test_the_card_goes_out_without_a_dedup_key_and_says_nothing_of_scoring(db, settings, agent):
    await finished(db, agent, CHECKS["file_exists"])
    surfaces = FakeSurfaces()
    await make(db, settings, agent, surfaces).tick()
    built, kw = surfaces.pushed[0]
    assert "dedup_key" not in kw and "reserved_key_ok" not in kw
    assert built.kind == "check_audit" and kw["notify"] is True
    for word in ("score", "credit", "reward", "points"):
        assert word not in kw["notify_text"].lower()


async def test_a_second_row_for_the_same_task_is_refused_by_the_database(db, agent):
    task = uuid.uuid4()
    await add(db, CheckAudit(agent_id=agent, task_id=task, node_id=uuid.uuid4(), check_kind="script"))
    with pytest.raises(IntegrityError):
        await add(db, CheckAudit(agent_id=agent, task_id=task, node_id=uuid.uuid4(), check_kind="script"))


def test_the_fixture_checks_are_the_kinds_they_are_named_for():
    from nous.observability.task_scoring import check_kind

    assert {name: check_kind(cmd) for name, cmd in CHECKS.items()} == {name: name for name in CHECKS}
