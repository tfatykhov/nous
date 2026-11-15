"""Goals PR 0c: the halt sweep cancels subtasks, chains, DAGs, shell commands and jobs, and reports once."""

from __future__ import annotations

import os
import sys
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from f099_support import env_factory, make_dag, make_root, runner_env  # noqa: F401
from sqlalchemy import select, update

from nous import background_halt, background_processes, cancel_provenance
from nous.background_halt import BackgroundHalt
from nous.brain import continuation, intentions
from nous.dag.delivery import DAGResultDelivery
from nous.handlers import halt_sweep
from nous.handlers.halt_sweep import HaltReport, HaltSweeper, open_root_ids
from nous.storage.models import Intention, Subtask

JOB_A, JOB_B = "job-20261115-093000-0a1b2c3d", "job-20261115-094500-0a1b2c3e"


class _Store:
    def __init__(self) -> None:
        self.audits: list = []

    async def read(self):
        return None

    async def write(self, row, audit):
        self.audits.append(audit)

    async def audit(self, entry):
        self.audits.append(entry)


def _halt() -> BackgroundHalt:
    halt = BackgroundHalt(host_hold=False, store=_Store())
    background_halt.install(halt)
    return halt


class _NoSessionDb:
    @asynccontextmanager
    async def session(self):
        yield None


def _fakes(order: list[str]):
    heart = MagicMock()

    async def cancel_all_active():
        order.append("subtasks")
        return [(uuid.uuid4(), "running"), (uuid.uuid4(), "pending"), (uuid.uuid4(), "pending")]

    heart.subtasks.cancel_all_active = cancel_all_active
    cont = MagicMock()
    cont_calls: list = []

    async def cancel_root(root, *, reason, actor, cancelled_by=None):
        order.append("chains")
        cont_calls.append((root, reason, actor, cancelled_by))
        return SimpleNamespace(truncated=False)

    cont.cancel_root = cancel_root
    dags = MagicMock()

    async def cancel_all_dags(cancelled_by):
        order.append("dags")
        return 1

    dags.cancel_all_active = cancel_all_dags
    return heart, cont, cont_calls, dags


async def test_the_sweep_cancels_in_order_and_reports_counts(monkeypatch, tmp_path):
    order: list[str] = []
    heart, cont, cont_calls, dags = _fakes(order)
    roots = [uuid.uuid4(), uuid.uuid4()]

    async def two_roots(session, agent_id, *, limit=200):
        return roots

    monkeypatch.setattr(halt_sweep, "open_root_ids", two_roots)

    def kill_all():
        order.append("commands")
        return 1

    monkeypatch.setattr(background_processes, "kill_all", kill_all)
    halt = _halt()
    await halt.halt(actor="dashboard", surface="dashboard")
    sweeper = HaltSweeper(
        settings=SimpleNamespace(agent_id="a", workspace_dir=str(tmp_path)),
        heart=heart,
        halt=halt,
        database=_NoSessionDb(),
        continuation_runner=cont,
        dag_orchestrator=dags,
    )
    report = await sweeper.run(actor="dashboard")
    assert order == ["subtasks", "chains", "chains", "dags", "commands"]  # the subtask UPDATE runs first
    assert (report.subtasks, report.subtasks_running, report.chains, report.dags, report.commands) == (3, 1, 2, 1, 1)
    assert report.failed == [] and report.jobs_checked
    assert {c[3]["by"] for c in cont_calls} == {"harness"} and {c[1] for c in cont_calls} == {"halted by the owner"}
    assert report.describe() == (
        "Background work is halted. Cancelled: 2 chains, 3 subtasks (1 was running), 1 DAG, 1 shell command. "
        "Nothing new can start, and every tool call from background work is refused. "
        "This does not stop a script that is already running or a caller that posts to chat."
    )
    sweep_rows = [a for a in halt._store.audits if a.action == "sweep"]
    assert len(sweep_rows) == 1 and sweep_rows[0].detail["cancelled"]["subtasks"] == 3


async def test_a_root_with_nothing_running_is_skipped_not_failed(monkeypatch, tmp_path):
    order: list[str] = []
    heart, cont, _calls, dags = _fakes(order)

    async def refused(root, **kwargs):
        raise continuation.CancelRefused("nothing running")

    cont.cancel_root = refused

    async def one_root(session, agent_id, *, limit=200):
        return [uuid.uuid4()]

    monkeypatch.setattr(halt_sweep, "open_root_ids", one_root)
    monkeypatch.setattr(background_processes, "kill_all", lambda: 0)
    halt = _halt()
    sweeper = HaltSweeper(
        settings=SimpleNamespace(agent_id="a", workspace_dir=str(tmp_path)),
        heart=heart,
        halt=halt,
        database=_NoSessionDb(),
        continuation_runner=cont,
        dag_orchestrator=dags,
    )
    report = await sweeper.run(actor="t")
    assert report.failed == [] and report.chains == 0


async def test_a_failing_step_is_reported_and_the_halt_still_holds(monkeypatch, tmp_path):
    order: list[str] = []
    heart, _cont, _calls, dags = _fakes(order)

    def broken():
        raise RuntimeError("no such process")

    monkeypatch.setattr(background_processes, "kill_all", broken)
    jobs_ran = []

    async def jobs(self, report):
        jobs_ran.append(True)

    monkeypatch.setattr(HaltSweeper, "_jobs", jobs)
    halt = _halt()
    await halt.halt(actor="t", surface="dashboard")
    sweeper = HaltSweeper(
        settings=SimpleNamespace(agent_id="a", workspace_dir=str(tmp_path)),
        heart=heart,
        halt=halt,
        dag_orchestrator=dags,
    )
    report = await sweeper.run(actor="t")
    assert report.failed == ["commands"] and jobs_ran == [True] and halt.halted
    assert "Could not stop: shell commands. Press Halt again to retry." in report.describe()
    assert halt.refuses("subtask", None, None)
    monkeypatch.setattr(background_processes, "kill_all", lambda: 0)
    assert (await sweeper.run(actor="t")).failed == []  # a second halt re-runs it


async def test_the_sweep_without_a_continuation_runner_still_cancels_dags_and_subtasks(monkeypatch, tmp_path):
    order: list[str] = []
    heart, _cont, _calls, dags = _fakes(order)
    monkeypatch.setattr(background_processes, "kill_all", lambda: 0)
    sweeper = HaltSweeper(
        settings=SimpleNamespace(agent_id="a", workspace_dir=str(tmp_path)),
        heart=heart,
        halt=_halt(),
        dag_orchestrator=dags,
    )
    report = await sweeper.run(actor="t")
    assert order == ["subtasks", "dags"] and report.subtasks == 3 and report.dags == 1 and report.failed == []


def test_report_text_is_fixed_vocabulary():
    assert HaltReport().describe().startswith("Background work is halted. Nothing was running. ")
    one = HaltReport(subtasks=1, subtasks_running=1, jobs=1, jobs_checked=True)
    assert "Cancelled: 1 subtask (1 was running), 1 Claude Code job." in one.describe()
    unchecked = HaltReport(jobs_checked=False, failed=["dags"], chains_truncated=True)
    text = unchecked.describe()
    assert "Could not stop: DAGs." in text and "Claude Code jobs were not checked." in text
    assert "Some chains were not reached." in text
    assert set(HaltReport().as_dict()["cancelled"]) == {
        "chains",
        "subtasks",
        "subtasks_running",
        "dags",
        "commands",
        "jobs",
    }


# ---- the jobs step -----------------------------------------------------------------------------------------------


def _job_dirs(tmp_path):
    jobs = tmp_path / "claude-jobs"
    for name in (JOB_A, JOB_B, "not-a-job", "job-20261115-093000-zzzzzzzz"):
        (jobs / name).mkdir(parents=True)
    return jobs


async def test_job_directories_are_listed_not_the_runner_list(tmp_path):
    jobs = _job_dirs(tmp_path)
    linked = tmp_path / "elsewhere"
    linked.mkdir()
    if sys.platform != "win32":
        os.symlink(linked, jobs / "job-20261115-095900-0a1b2c3f")
    calls: list[list[str]] = []
    statuses = {JOB_A: "Status: running\n", JOB_B: "Status: completed\n"}

    async def runner(workspace, args, timeout):
        calls.append(args)
        if args[0] == "status":
            return 0, statuses[args[1]]
        return 0, "cancelled\n"

    sweeper = HaltSweeper(
        settings=SimpleNamespace(agent_id="a", workspace_dir=str(tmp_path)),
        heart=MagicMock(),
        halt=_halt(),
        run_runner=runner,
    )
    report = HaltReport()
    await sweeper._jobs(report)
    assert sorted(c for c in calls if c[0] == "status") == [["status", JOB_A], ["status", JOB_B]]
    assert [c for c in calls if c[0] == "cancel"] == [["cancel", JOB_A]]
    assert report.jobs == 1 and report.jobs_checked
    if sys.platform != "win32":  # job marks need dir_fd and O_NOFOLLOW
        mark = cancel_provenance.describe_job_cancellation(str(tmp_path), JOB_A)
        assert mark == "cancelled by the harness (background halt)"


async def test_a_runner_that_prints_no_status_cancels_nothing_and_says_so(tmp_path):
    _job_dirs(tmp_path)
    calls: list = []

    async def silent(workspace, args, timeout):
        calls.append(args)
        return 0, ""

    sweeper = HaltSweeper(
        settings=SimpleNamespace(agent_id="a", workspace_dir=str(tmp_path)),
        heart=MagicMock(),
        halt=_halt(),
        run_runner=silent,
    )
    report = HaltReport()
    await sweeper._jobs(report)
    assert [c for c in calls if c[0] == "cancel"] == [] and report.jobs == 0
    assert not report.jobs_checked and "Claude Code jobs were not checked." in report.describe()


@pytest.mark.skipif(sys.platform == "win32", reason="runs a shell script")
async def test_the_real_runner_call_runs_the_stub_without_a_shell(tmp_path):
    from nous.api.builtin_tools import run_runner

    jobs = _job_dirs(tmp_path)
    stub = jobs / "runner.sh"
    stub.write_text(
        '#!/bin/sh\nif [ "$1" = status ]; then\n  if [ "$2" = ' + JOB_A + ' ]; then echo "Status: running"; '
        'else echo "Status: completed"; fi\nfi\n'
        'if [ "$1" = cancel ]; then echo "$2" >> "$(dirname "$0")/cancelled"; fi\n',
        encoding="utf-8",
    )
    stub.chmod(0o755)
    sweeper = HaltSweeper(
        settings=SimpleNamespace(agent_id="a", workspace_dir=str(tmp_path)), heart=MagicMock(), halt=_halt()
    )
    report = HaltReport()
    await sweeper._jobs(report)
    assert (jobs / "cancelled").read_text().split() == [JOB_A] and report.jobs == 1
    code, out = await run_runner(str(tmp_path), ["status", "job-20261115-093000-0a1b2c3d; touch x"], 5)
    assert out.strip() == "Status: completed" and not (tmp_path / "x").exists()  # one argument, no shell


# ---- with the real store -----------------------------------------------------------------------------------------


async def test_open_root_ids_leave_out_a_schedule_container(env_factory):  # noqa: F811
    env = await env_factory(intentions_enabled=True)
    plain = await make_root(env)
    container = await make_root(env)
    async with env.db.session() as s:
        await s.execute(
            update(Intention).where(Intention.id == container.id).values(wake_policy=intentions.WAKE_CONTAINER)
        )
        await s.commit()
    async with env.db.session() as s:
        ids = await open_root_ids(s, env.agent)
    assert plain.id in ids and container.id not in ids


async def test_resume_reruns_the_subtask_and_dag_cancel_steps_before_clearing_the_flag(env_factory, monkeypatch):  # noqa: F811
    env = await env_factory()
    halt = _halt()
    dags = MagicMock()
    dags.cancel_all_active = AsyncMock(return_value=0)
    sweeper = HaltSweeper(settings=env.settings, heart=env.heart, halt=halt, dag_orchestrator=dags)
    halt.set_resume_sweep(sweeper.cancel_new_work)
    await halt.halt(actor="t", surface="dashboard")
    planted = Subtask(agent_id=env.agent, task="planted while halted", status="pending")  # a direct insert
    async with env.db.session() as s:
        s.add(planted)
        await s.commit()
    assert (await halt.resume(actor="t", via="dashboard")).changed
    async with env.db.session() as s:
        row = (await s.execute(select(Subtask).where(Subtask.id == planted.id))).scalar_one()
    assert row.status == "cancelled"
    dags.cancel_all_active.assert_awaited_once()
    pending = await env.heart.subtasks.cancel_all_active()
    assert pending == []  # nothing pending at the resume


# ---- delivery and the cancel record -------------------------------------------------------------------------------


async def test_a_halt_cancelled_dag_is_delivered_silently():
    settings = SimpleNamespace(
        dag_delivery_agent_summary_enabled=True,
        dag_delivery_bus_enabled=True,
        dag_delivery_telegram_enabled=True,
        agent_id="a",
    )
    delivery = DAGResultDelivery.__new__(DAGResultDelivery)
    delivery._settings = settings
    delivery._inbox = MagicMock()
    delivery._leg_telegram = AsyncMock()
    delivery._leg_bus = AsyncMock()
    delivery._leg_agent_summary = AsyncMock()
    delivery._dag_intention = AsyncMock(return_value=(None, False))
    delivery.build_template = MagicMock(return_value="DAG x cancelled")
    dag = SimpleNamespace(
        id=uuid.uuid4(),
        name="d",
        status="cancelled",
        postmortem=cancel_provenance.dag_postmortem(cancel_provenance.halt_record()),
        delivery_generation=1,
    )
    outcome = await delivery.deliver(dag)
    assert outcome.delivered and [leg.name for leg in outcome.legs] == ["halt"]
    delivery._leg_telegram.assert_not_awaited()
    delivery._leg_bus.assert_not_awaited()
    delivery._leg_agent_summary.assert_not_awaited()


@pytest.mark.postgres_only
async def test_cancel_root_records_the_given_canceller_and_otherwise_the_owner(runner_env):  # noqa: F811
    from nous.handlers.continuation_runner import ContinuationRunner

    env = await runner_env()
    by: list = []

    async def cancel_dag(dag_id, reason="cancelled", cancelled_by=None):
        by.append(cancelled_by)

    cont = ContinuationRunner(
        database=env.db,
        settings=env.settings,
        runner=env.runner,
        heart=env.heart,
        brain=env.brain,
        bus=env.bus,
        cancel_dag=cancel_dag,
    )
    for given in (cancel_provenance.halt_record(), None):
        root = await make_root(env)
        await make_dag(env, status="running", parent=root)
        await cont.cancel_root(root.id, reason="r", actor="telegram:1", cancelled_by=given)
    assert [(b["by"], b["via"]) for b in by] == [("harness", "background halt"), ("owner", "Telegram")]


async def test_the_orchestrator_cancels_every_active_dag_and_reports_a_failure_after_the_rest():
    from nous.dag.orchestrator import DAGOrchestrator

    ids = [uuid.uuid4(), uuid.uuid4(), uuid.uuid4()]
    orchestrator = DAGOrchestrator.__new__(DAGOrchestrator)
    orchestrator._store = MagicMock()
    orchestrator._store.get_active_dags = AsyncMock(return_value=[SimpleNamespace(id=i) for i in ids])
    calls: list = []

    async def cancel_dag(dag_id, reason="cancelled", cancelled_by=None):
        calls.append((dag_id, reason, cancelled_by["by"]))
        if dag_id == ids[1]:
            raise RuntimeError("the database blinked")

    orchestrator.cancel_dag = cancel_dag
    with pytest.raises(RuntimeError):
        await orchestrator.cancel_all_active(cancel_provenance.halt_record())
    assert [c[0] for c in calls] == ids and {c[1] for c in calls} == {"halted by the owner"}
    assert {c[2] for c in calls} == {"harness"}
    orchestrator._store.get_active_dags = AsyncMock(return_value=[SimpleNamespace(id=ids[0])])
    assert await orchestrator.cancel_all_active(cancel_provenance.halt_record()) == 1
