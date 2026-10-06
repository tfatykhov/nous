"""F099 Phase 2c-1: a lineage check's tokens count against its DAG, so a looping check reaches the budget."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from f099_support import CONT, ON, env_factory, make_dag, make_root  # noqa: F401
from sqlalchemy import select
from test_f099_phase0b import _runner as _heartbeat_runner

from nous.brain import continuation
from nous.config import Settings
from nous.dag.orchestrator import DAGOrchestrator
from nous.heartbeat.dynamic import DynamicCheck
from nous.heartbeat.registry import CheckRegistry
from nous.heartbeat.schemas import CheckResult
from nous.storage.models import DAGNode, ExecutionDAG

STAMP = {
    "id": "11111111-1111-1111-1111-111111111111",
    "root_id": "11111111-1111-1111-1111-111111111111",
    "authority": "internal_only",
}


def _check(name: str, *, stamp=STAMP) -> DynamicCheck:
    return DynamicCheck(
        check_id=f"{name}-id", name=name, prompt="Watch", tools=["web_search"], interval=300, intention=stamp
    )


def _loader():
    loader = MagicMock()
    loader.update_run_stats = AsyncMock()
    loader.record_final_findings = AsyncMock()
    loader.check_metadata = AsyncMock(return_value={"dag_node_id": "node-1"})
    return loader


def _hb(check: DynamicCheck, tokens: int, *, orchestrator=True, flags=CONT):
    registry = CheckRegistry()
    check.run = AsyncMock(return_value=CheckResult(has_updates=False, findings=[], tokens_used=tokens))
    registry.register(check)
    loader = _loader()
    hb, _ = _heartbeat_runner(registry, loader=loader)
    # The runner reads continuation.enabled(its settings): the helper's quiet-hours-off settings plus the flags.
    hb._settings = Settings(
        _env_file=None,
        heartbeat_enabled=True,
        heartbeat_quiet_start=0,
        heartbeat_quiet_end=0,
        heartbeat_daily_token_budget=10_000,
        heartbeat_dynamic_sync_ticks=0,
        **flags,
    )
    hb.dag_orchestrator = SimpleNamespace(add_check_tokens=AsyncMock(return_value=True)) if orchestrator else None
    return hb, loader


def test_a_check_exposes_the_lineage_stamp_it_was_built_with():
    assert _check("a").intention_stamp == STAMP
    assert _check("b", stamp=None).intention_stamp is None


@pytest.mark.parametrize("entry", ["tick", "trigger"])
async def test_a_lineage_check_run_adds_its_tokens_to_its_dag(entry):
    check = _check(f"lineage_{entry}")
    hb, loader = _hb(check, 120)
    await (hb._tick() if entry == "tick" else hb.trigger_check(check.name))
    loader.check_metadata.assert_awaited_once_with(check.name)
    hb.dag_orchestrator.add_check_tokens.assert_awaited_once_with({"dag_node_id": "node-1"}, 120)


@pytest.mark.parametrize("entry", ["tick", "trigger"])
async def test_a_check_with_no_lineage_does_no_extra_work(entry):  # PIN
    check = _check(f"plain_{entry}", stamp=None)
    hb, loader = _hb(check, 120)
    await (hb._tick() if entry == "tick" else hb.trigger_check(check.name))
    loader.check_metadata.assert_not_awaited()
    hb.dag_orchestrator.add_check_tokens.assert_not_awaited()


@pytest.mark.parametrize("entry", ["tick", "trigger"])
async def test_a_stamped_check_adds_nothing_with_continuation_off(entry):  # PIN: prod's flags
    """The orchestrator stamps EVERY check node of a DAG that has an intention, so with intentions on a
    check carries a stamp in prod: only the continuation flag may switch the roll-up on."""
    check = _check(f"prod_{entry}")
    hb, loader = _hb(check, 120, flags=ON)
    await (hb._tick() if entry == "tick" else hb.trigger_check(check.name))
    loader.check_metadata.assert_not_awaited()
    hb.dag_orchestrator.add_check_tokens.assert_not_awaited()


async def test_a_run_that_used_no_tokens_adds_nothing():
    check = _check("quiet")
    hb, loader = _hb(check, 0)
    await hb._tick()
    hb.dag_orchestrator.add_check_tokens.assert_not_awaited()


async def test_without_an_orchestrator_nothing_is_attempted():
    check = _check("orphan")
    hb, loader = _hb(check, 50, orchestrator=False)
    await hb._tick()
    loader.check_metadata.assert_not_awaited()


async def test_a_failing_roll_up_never_fails_the_run():
    check = _check("flaky")
    hb, loader = _hb(check, 50)
    hb.dag_orchestrator.add_check_tokens = AsyncMock(side_effect=RuntimeError("db down"))
    await hb._tick()  # the run still completes: the loader's stats write was reached
    loader.update_run_stats.assert_awaited_once()


async def _node_id(env, dag_id):
    async with env.db.session() as s:
        return (await s.execute(select(DAGNode.id).where(DAGNode.dag_id == dag_id))).scalar_one()


async def _consumed(env, dag_id) -> int:
    async with env.db.session() as s:
        return (await s.execute(select(ExecutionDAG.tokens_consumed).where(ExecutionDAG.id == dag_id))).scalar_one()


@pytest.mark.postgres_only
async def test_the_orchestrator_adds_a_checks_tokens_to_its_dag_by_the_owner_key(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    dag, store = await make_dag(env)
    node_id = await _node_id(env, dag.id)
    orchestrator = DAGOrchestrator(store, settings=env.settings)
    assert await orchestrator.add_check_tokens({"dag_node_id": str(node_id)}, 700) is True
    assert await orchestrator.add_check_tokens({"dag_node_id": str(node_id)}, 300) is True  # relative: it adds
    assert await _consumed(env, dag.id) == 1000


@pytest.mark.postgres_only
@pytest.mark.parametrize(
    "metadata", [{}, {"dag_node_id": "not-a-uuid"}, {"dag_node_id": "00000000-0000-0000-0000-000000000000"}]
)
async def test_an_unreadable_owner_key_adds_nothing(env_factory, metadata):  # noqa: F811
    env = await env_factory(**CONT)
    dag, store = await make_dag(env)
    assert await DAGOrchestrator(store, settings=env.settings).add_check_tokens(metadata, 500) is False
    assert await _consumed(env, dag.id) == 0


@pytest.mark.postgres_only
async def test_a_non_positive_token_count_adds_nothing(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    dag, store = await make_dag(env)
    node_id = await _node_id(env, dag.id)
    assert (
        await DAGOrchestrator(store, settings=env.settings).add_check_tokens({"dag_node_id": str(node_id)}, 0) is False
    )


@pytest.mark.postgres_only
async def test_a_looping_lineage_check_reaches_the_root_token_budget(env_factory):  # noqa: F811
    """Review Focus 9: the check's tokens are in its DAG's tokens_consumed, which is in the root's budget."""
    env = await env_factory(**CONT, continuation_max_tokens_per_root=1000)
    root = await make_root(env)
    dag, store = await make_dag(env, parent=root)
    node_id = await _node_id(env, dag.id)
    orchestrator = DAGOrchestrator(store, settings=env.settings)
    for _ in range(4):  # four runs of a check that never finishes
        await orchestrator.add_check_tokens({"dag_node_id": str(node_id)}, 250)
    async with env.db.session() as s:
        limits = await continuation.root_limits(s, env.agent, root.id, settings=env.settings)
    assert (limits.tokens, limits.escalate) == (1000, "budget_tokens")
