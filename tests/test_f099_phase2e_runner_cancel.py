"""F099 Phase 2e-5: the runner's cancel: the view, the DAGs, the running turn, the sweep (spec 4.6)."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from f099_support import (
    ask_with_proposals,
    env_factory,  # noqa: F401
    inbox_rows,
    intention_of,
    make_dag,
    make_root,
    record,
    runner_env,  # noqa: F401
    set_intention,
    until_a_backend_waits_on_a_lock,
    use,
)
from sqlalchemy import select

import nous.handlers.continuation_runner as runner_module
from nous.api.execution_context import ExecutionContext
from nous.brain import continuation
from nous.handlers.continuation_runner import ContinuationRunner
from nous.storage.models import Intention, IntentionArrival

pytestmark = pytest.mark.postgres_only


def _cont(env, **kw) -> ContinuationRunner:
    env.cont = ContinuationRunner(
        database=env.db, settings=env.settings, runner=env.runner, heart=env.heart, brain=env.brain, bus=env.bus, **kw
    )
    env.runner.set_cancelled_roots(env.cont.root_is_cancelled)  # what main.py does
    return env.cont


def resolve(decision="report", note="Done.", progress=False, confidence=0.7):
    return use("resolve_intention", decision=decision, note=note, progress=progress, confidence=confidence)


async def _arrivals(env, root_id):
    async with env.db.session() as s:
        rows = await s.execute(select(IntentionArrival).where(IntentionArrival.root_id == root_id))
        return list(rows.scalars().all())


async def _ready_root(env):
    root = await make_root(env)
    await record(env, root)
    return root


async def _owner_rows(env):
    return [r for r in await inbox_rows(env) if r.source_kind == "intention_report"]


class DagRecorder:
    """A stand-in for ``DAGOrchestrator.cancel_dag``."""

    def __init__(self, fail_for=()) -> None:
        self.calls, self.fail_for = [], set(fail_for)

    async def __call__(self, dag_id, reason="cancelled"):
        self.calls.append((dag_id, reason))
        if dag_id in self.fail_for:
            raise RuntimeError("the orchestrator is down")


# ---- cancel_root ---------------------------------------------------------------------------------------------


async def test_cancel_root_cancels_the_lineage_the_dags_and_tells_the_bus(runner_env):  # noqa: F811
    env = await runner_env()
    asked = await ask_with_proposals(env)
    (pid,) = asked.ids
    running, _ = await make_dag(env, status="running", parent=asked.root)
    dags, view_held = DagRecorder(), []

    async def cancel_dag(dag_id, reason="cancelled"):  # the view holds the root before the orchestrator is asked
        view_held.append(cont.root_is_cancelled(asked.root.id))
        await dags(dag_id, reason)

    cont = _cont(env, cancel_dag=cancel_dag)

    out = await cont.cancel_root(asked.root.id, reason="no longer wanted", actor="owner-test")
    assert view_held == [True]

    assert (out.already_cancelled, out.cancelled_dags, out.cancelled_proposals, out.turn_stopped) == (
        False,
        1,
        1,
        False,
    )
    assert dags.calls == [(running.id, "cancelled by the owner")]
    assert cont.root_is_cancelled(asked.root.id) and not cont.root_is_cancelled(uuid.uuid4())
    events = {e.type: e.data for e in env.bus.events}
    assert events["intention.root_cancelled"] == {
        "root_id": str(asked.root.id),
        "reason": "no longer wanted",
        "actor": "owner-test",
    }
    assert events["intention.proposal_decided"] == {"proposal_id": str(pid), "state": "cancelled", "actor": "system"}


async def test_a_dag_the_orchestrator_cannot_cancel_is_left_to_the_sweep(runner_env, caplog):  # noqa: F811
    env = await runner_env()
    root = await make_root(env)
    running, _ = await make_dag(env, status="running", parent=root)
    dags = DagRecorder(fail_for={running.id})
    cont = _cont(env, cancel_dag=dags)
    out = await cont.cancel_root(root.id, reason="t", actor="t")
    assert out.cancelled_dags == 0 and "could not cancel DAG" in caplog.text
    assert cont.root_is_cancelled(root.id)  # the cancel itself stands: the lineage can do nothing
    dags.fail_for.clear()
    await cont.run_once()  # the sweep finds the DAG still running under a cancelled root
    assert [call[0] for call in dags.calls] == [running.id, running.id]


async def test_the_sweep_leaves_a_running_dag_of_a_live_root_alone(runner_env):  # noqa: F811
    """2e-5 review I1: the sweep acts on rows no owner named, so only the DAGs under a cancelled root are its."""
    env = await runner_env()
    doomed, alive = await make_root(env), await make_root(env)
    stray, _ = await make_dag(env, status="running", parent=doomed)
    live, _ = await make_dag(env, status="running", parent=alive)
    dags = DagRecorder(fail_for={stray.id})
    cont = _cont(env, cancel_dag=dags)
    await cont.cancel_root(doomed.id, reason="t", actor="t")
    dags.fail_for.clear()
    await cont.run_once()
    assert [call[0] for call in dags.calls] == [stray.id, stray.id]  # never live.id


async def test_with_no_orchestrator_bound_the_dags_are_reported_and_the_cancel_stands(runner_env, caplog):  # noqa: F811
    env = await runner_env()
    root = await make_root(env)
    await make_dag(env, status="running", parent=root)
    cont = _cont(env)
    out = await cont.cancel_root(root.id, reason="t", actor="t")
    assert out.cancelled_dags == 0 and len(out.dag_ids) == 1 and "no orchestrator is bound" in caplog.text


async def test_a_refused_or_unknown_cancel_raises_and_changes_nothing(runner_env):  # noqa: F811
    env = await runner_env()
    cont = _cont(env)
    with pytest.raises(continuation.RootNotFound):
        await cont.cancel_root(uuid.uuid4(), reason="t", actor="t")
    root = await make_root(env)
    await env.heart.subtasks.cancel(uuid.UUID(root.source_id))
    await set_intention(env, root.id, state="closed", close_reason="legacy")
    with pytest.raises(continuation.CancelRefused):
        await cont.cancel_root(root.id, reason="t", actor="t")
    assert not cont.root_is_cancelled(root.id) and env.bus.events == []


# ---- the running turn (carry-over 6) ---------------------------------------------------------------------------


async def test_a_cancel_stops_the_running_turn_releases_its_slot_and_commits_nothing(runner_env):  # noqa: F811
    started, never = asyncio.Event(), asyncio.Event()

    async def blocked(_kwargs):
        started.set()
        await never.wait()  # the model call hangs until the turn is cancelled

    env = await runner_env(blocked)
    root = await _ready_root(env)
    cont = _cont(env)
    assert (await cont.run_once()).launched == (root.id,)
    await asyncio.wait_for(started.wait(), timeout=30)
    assert cont.running_roots == frozenset({root.id})

    out = await cont.cancel_root(root.id, reason="stop", actor="owner-test")

    assert out.turn_stopped is True
    assert cont.running_roots == frozenset() and cont._slots._value == env.settings.continuation_max_concurrent
    assert (await intention_of(env, "subtask", root.source_id)).state == "cancelled"
    assert await _arrivals(env, root.id) == [] and await _owner_rows(env) == []
    assert env.cognitive.end_sessions == [f"intent-{root.id}"]  # the session was ended on the way out
    report = await cont.run_once()  # nothing is claimable and nothing is launched
    assert report.launched == ()


async def test_a_turn_that_finished_but_has_not_committed_loses_its_fence_to_the_cancel(runner_env):  # noqa: F811
    """The turn's last model call returns its decision, and the owner's cancel commits just before the commit."""
    holder = {}

    async def decide_then_get_cancelled(_kwargs):
        async with holder["env"].db.session() as s:  # the cancel, in its own committed transaction
            await continuation.cancel_root(s, holder["env"].agent, holder["root"].id, reason="t", actor="t")
            await s.commit()
        return [resolve("report", "A report nobody should read.", progress=True)]

    env = await runner_env(decide_then_get_cancelled)
    root = await _ready_root(env)
    holder.update(env=env, root=root)
    cont = _cont(env)
    done = await cont.run_arrival(root.id)
    assert done is None  # the commit's fence was gone
    assert await _arrivals(env, root.id) == [] and await _owner_rows(env) == []
    fresh = await intention_of(env, "subtask", root.source_id)
    assert (fresh.state, fresh.attempts) == ("cancelled", 0)  # not charged as a failed attempt


async def test_a_cancel_that_queues_behind_a_claim_stops_the_turn_that_claim_started(runner_env, monkeypatch):  # noqa: F811
    """The claim and the cancel serialise on the root row, and Postgres grants a row lock in the order it was asked
    for: the claim is first in the queue, so it wins (the spy sees a claim), moves the root to `deciding` and
    commits; the cancel then takes the lock, moves that `deciding` row and cancels the arrival's task. Deterministic:
    the lock is held while both queue."""
    never = asyncio.Event()

    async def blocked(_kwargs):
        await never.wait()  # a turn that reaches its model call waits there: the cancel stops it wherever it is

    env = await runner_env(blocked)
    root = await _ready_root(env)
    cont = _cont(env)
    claims = []
    real = continuation.claim_root

    async def spy(*args, **kwargs):
        got = await real(*args, **kwargs)
        claims.append(got is not None)
        return got

    monkeypatch.setattr(continuation, "claim_root", spy)
    async with env.db.session() as holder:
        await holder.execute(select(Intention.id).where(Intention.id == root.id).with_for_update(key_share=True))
        assert (await cont.run_once()).launched == (root.id,)  # the arrival task is made; its claim queues on the root
        try:
            await asyncio.wait_for(until_a_backend_waits_on_a_lock(env, at_least=1), timeout=10)
            cancel = asyncio.create_task(cont.cancel_root(root.id, reason="t", actor="t"))
            await asyncio.wait_for(until_a_backend_waits_on_a_lock(env, at_least=2), timeout=10)
        finally:
            await holder.commit()
    out = await asyncio.wait_for(cancel, timeout=30)
    assert claims == [True]  # the claim was first in the queue and won
    assert out.turn_stopped is True and out.cancelled_intentions == 1  # the cancel found its `deciding` row
    assert cont.running_roots == frozenset() and cont._slots._value == env.settings.continuation_max_concurrent
    assert await _arrivals(env, root.id) == [] and await _owner_rows(env) == []
    assert (await intention_of(env, "subtask", root.source_id)).state == "cancelled"


async def test_a_turn_that_is_slow_to_unwind_is_told_so_in_the_log(runner_env, monkeypatch, caplog):  # noqa: F811
    """N1 of the plan review: the route still says the turn was stopped (it was told to), and the log says it is not
    gone yet."""
    monkeypatch.setattr(runner_module, "CANCEL_WAIT_SECONDS", 0.1)
    env = await runner_env()
    cont = _cont(env)
    root_id = uuid.uuid4()

    async def stubborn():
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            await asyncio.sleep(0.5)  # a blocking call that has to finish first

    task = asyncio.create_task(stubborn())
    cont._running[root_id] = task
    await asyncio.sleep(0)
    assert await cont._stop_turns([root_id]) is True
    assert "still unwinding" in caplog.text
    await asyncio.wait_for(task, timeout=10)


async def test_the_proposals_an_expiry_ended_are_announced_on_the_bus(runner_env):  # noqa: F811
    """S3 of the plan review: the proposals sweep no longer finds the proposals of an expired root, so the expiry's
    own sweep step tells the bus, and the rows say who ended them."""
    env = await runner_env()
    asked = await ask_with_proposals(env)
    (pid,) = asked.ids
    await set_intention(env, asked.root.id, created_at=datetime.now(UTC) - timedelta(hours=100))
    cont = _cont(env)
    await cont.run_once()
    events = [e.data for e in env.bus.events if e.type == "intention.proposal_decided"]
    assert events == [{"proposal_id": str(pid), "state": "expired", "actor": "system"}]
    assert [e.data["root_id"] for e in env.bus.events if e.type == "intention.root_expired"] == [str(asked.root.id)]


# ---- the view: security, restart, refresh ---------------------------------------------------------------------------


async def test_after_a_cancel_the_lineage_can_dispatch_nothing(runner_env):  # noqa: F811
    """The security pin: the view is the one `_authorize_tool_call` reads, and a cancel puts the root in it."""
    env = await runner_env()
    root = await make_root(env, routed=False)
    cont = _cont(env)
    contexts = [
        ExecutionContext(kind="subtask", session_id="s", root_intention_id=root.id, intention_id=root.id),
        ExecutionContext(
            kind="continuation",
            session_id="s",
            authority="internal_only",
            root_intention_id=root.id,
            intention_id=root.id,
        ),
    ]
    before = [env.runner._authorize_tool_call(c, "web_search", frozenset({"web_search"}), "s", {}) for c in contexts]
    assert before == [None, None]
    await cont.cancel_root(root.id, reason="t", actor="t")
    after = [env.runner._authorize_tool_call(c, "web_search", frozenset({"web_search"}), "s", {}) for c in contexts]
    assert [r.code for r in after] == ["root_cancelled", "root_cancelled"]


async def test_a_restart_remembers_the_cancel_and_a_sweep_takes_in_one_made_elsewhere(runner_env):  # noqa: F811
    env = await runner_env()
    first, second = await make_root(env), await make_root(env)
    cont = _cont(env)
    await cont.cancel_root(first.id, reason="t", actor="t")
    restarted = _cont(env)  # a new process: an empty view
    assert not restarted.root_is_cancelled(first.id)
    assert await restarted.load_cancelled_roots() == 1 and restarted.root_is_cancelled(first.id)
    async with env.db.session() as s:  # another surface cancels the second root, in the store
        await continuation.cancel_root(s, env.agent, second.id, reason="t", actor="t")
        await s.commit()
    assert not restarted.root_is_cancelled(second.id)
    await restarted.run_once()
    assert restarted.root_is_cancelled(second.id)


async def test_start_loads_the_view_before_the_loop_runs(runner_env):  # noqa: F811
    env = await runner_env()
    root = await make_root(env)
    await _cont(env).cancel_root(root.id, reason="t", actor="t")
    fresh = _cont(env)
    await fresh.start()
    try:
        assert fresh.root_is_cancelled(root.id)
    finally:
        await fresh.stop()


async def test_a_cancelled_root_is_never_launched_again(runner_env):  # noqa: F811
    env = await runner_env()
    root = await _ready_root(env)
    cont = _cont(env)
    await cont.cancel_root(root.id, reason="t", actor="t")
    await record(env, root, generation=1)  # a late result lands on the cancelled root
    assert (await cont.run_once()).launched == ()
    assert env.model.calls == []
