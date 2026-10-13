"""F099 Phase 3d: the calibration step of the continuation sweep, end to end."""

from __future__ import annotations

import asyncio
import logging

import pytest
from f099_calibration_support import outcome_of
from f099_support import env_factory, make_root, record, runner_env, use  # noqa: F401
from sqlalchemy import select

from nous.handlers.continuation_runner import ContinuationRunner
from nous.storage.models import IntentionArrival

pytestmark = pytest.mark.postgres_only


def _cont(env) -> ContinuationRunner:
    env.cont = ContinuationRunner(
        database=env.db, settings=env.settings, runner=env.runner, heart=env.heart, brain=env.brain, bus=env.bus
    )
    return env.cont


def resolve(decision="report", note="The snow is deep."):
    return use("resolve_intention", decision=decision, note=note, progress=False, confidence=0.7)


async def _arrival(env, root_id):
    async with env.db.session() as s:
        query = select(IntentionArrival).where(
            IntentionArrival.agent_id == env.agent, IntentionArrival.root_id == root_id
        )
        return (await s.execute(query)).scalar_one()


async def _settle(cont: ContinuationRunner) -> None:
    tasks = list(cont._running.values())
    if tasks:
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=30)


async def test_a_sweep_grades_the_arrival_of_a_chain_that_reported(runner_env):  # noqa: F811
    env = await runner_env([resolve("report")], intention_calibration_enabled=True)
    root = await make_root(env)
    await record(env, root)
    cont = _cont(env)
    await cont.run_arrival(root.id)
    arrival = await _arrival(env, root.id)
    assert (await outcome_of(env, arrival.decision_record_id)).outcome == "pending"
    await cont.run_once()
    got = await outcome_of(env, arrival.decision_record_id)
    assert (got.outcome, got.reviewer, got.result) == ("success", "f099", "chain reported after 1 arrival")


async def test_flag_off_the_sweep_writes_no_review(runner_env):  # noqa: F811
    """PIN. Two checks keep the flag-off sweep from writing: `_calibrate`'s and `grade_arrivals`'s own (pinned
    alone by test_f099_phase3d_grader.py::test_the_flag_gates_the_grader_itself). Mutation: drop both and the
    decision is graded; dropping only `_calibrate`'s leaves this green."""
    env = await runner_env([resolve("report")])  # calibration off (the default)
    root = await make_root(env)
    await record(env, root)
    cont = _cont(env)
    await cont.run_arrival(root.id)
    await cont.run_once()
    arrival = await _arrival(env, root.id)
    got = await outcome_of(env, arrival.decision_record_id)
    assert (got.outcome, got.reviewer, got.reviewed_at) == ("pending", None, None)


async def test_a_failing_grader_does_not_stop_the_sweep(runner_env, monkeypatch, caplog):  # noqa: F811
    """The step runs (its failure is logged under its name) and is isolated: the launch after it still runs.
    Mutation: call `self._calibrate()` directly in `run_once` instead of through `_step`; the test then raises
    RuntimeError."""
    from nous.brain import intention_calibration

    env = await runner_env([resolve("report")], intention_calibration_enabled=True)

    async def boom(*args, **kwargs):
        raise RuntimeError("grader down")

    monkeypatch.setattr(intention_calibration, "grade_arrivals", boom)
    root = await make_root(env)
    await record(env, root)
    cont = _cont(env)
    with caplog.at_level(logging.WARNING):
        report = await cont.run_once()  # must not raise; the launch step still ran
    assert report.launched == (root.id,)
    assert "the continuation calibration failed" in caplog.text
    await _settle(cont)
