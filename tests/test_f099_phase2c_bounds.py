"""F099 Phase 2c-1: the root TTL and the depth and spawn limits, written at spawn (spec 4.6)."""

from __future__ import annotations

import ast
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from f099_support import CONT, ON, env_factory, intention_of, make_root, set_intention  # noqa: F401
from sqlalchemy import select

from nous.api.execution_context import ExecutionContext
from nous.api.tools import ToolDispatcher, register_dag_tools, register_subtask_tools
from nous.brain import intentions
from nous.brain.intentions import IntentionLimitReached, IntentionSpec
from nous.config import Settings
from nous.dag.store import DAGStore
from nous.storage.models import Intention

SETTINGS_OFF = Settings(_env_file=None, **ON)
SETTINGS_ON = Settings(_env_file=None, **CONT)


def _root_spec() -> IntentionSpec:
    return IntentionSpec(intent="Check the snow", origin_kind="interactive")


def _child(parent, **over) -> IntentionSpec:
    return IntentionSpec(
        intent="next step", origin_kind="continuation", parent_id=parent.id, origin_authority="internal_only", **over
    )


async def _spawn(env, spec: IntentionSpec):
    st = await env.heart.subtasks.create(task="work", intention=spec)
    return await intention_of(env, "subtask", st.id)


def test_the_bounds_are_absent_with_continuation_off():  # PIN
    spec = _root_spec()
    assert intentions.ttl_for(SETTINGS_OFF) is None and intentions.limits_for(SETTINGS_OFF) is None
    assert intentions.with_bounds(spec, SETTINGS_OFF) is spec  # the very object: Phase 1's call, unchanged
    assert intentions.with_bounds(None, SETTINGS_ON) is None


def test_the_bounds_come_from_the_settings_with_continuation_on():
    spec = _root_spec()
    bounded = intentions.with_bounds(spec, SETTINGS_ON)
    assert (bounded.ttl_hours, bounded.limits) == (72.0, (3, 12))
    assert (spec.ttl_hours, spec.limits) == (None, None)  # the spec is frozen: a new object came back


def test_a_container_gets_the_limits_but_no_ttl():
    container = IntentionSpec(intent="rest: nightly", origin_kind="rest", container=True)
    bounded = intentions.with_bounds(container, SETTINGS_ON)
    assert (bounded.ttl_hours, bounded.limits) == (None, (3, 12))


@pytest.mark.postgres_only
async def test_a_root_gets_created_plus_ttl(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    before = datetime.now(UTC)
    root = await _spawn(env, intentions.with_bounds(_root_spec(), env.settings))
    assert abs((root.deadline - (before + timedelta(hours=72))).total_seconds()) < 60


@pytest.mark.postgres_only
async def test_a_root_spawned_without_bounds_keeps_a_null_deadline(env_factory):  # noqa: F811  # PIN (Phase 1)
    env = await env_factory(**ON)
    root = await _spawn(env, _root_spec())
    assert root.deadline is None


@pytest.mark.postgres_only
async def test_a_child_deadline_is_the_earlier_of_its_parent_and_its_own(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    short = Settings(_env_file=None, **CONT, intention_root_ttl_hours=1)
    root = await _spawn(env, intentions.with_bounds(_root_spec(), short))
    child = await _spawn(env, intentions.with_bounds(_child(root), env.settings))  # 72 h of its own
    assert child.deadline == root.deadline
    # A fresh parent with no deadline (a Phase 1 row): its created_at + TTL, about the child's own.
    old_root = await make_root(env)
    own = await _spawn(env, intentions.with_bounds(_child(old_root), env.settings))
    assert own.deadline > datetime.now(UTC) + timedelta(hours=71)


@pytest.mark.postgres_only
async def test_a_child_of_an_old_root_with_no_deadline_gets_what_is_left_of_its_ttl(env_factory):  # noqa: F811
    """Ruling R3: a NULL deadline (a Phase 1 root) means created_at + TTL, the rule the expiry judges the root
    by. A child spawned 70 hours into a 72-hour TTL gets about 2 hours, not 72."""
    env = await env_factory(**CONT)
    old_root = await make_root(env)  # no bounds: a NULL deadline
    await set_intention(env, old_root.id, created_at=datetime.now(UTC) - timedelta(hours=70))
    child = await _spawn(env, intentions.with_bounds(_child(old_root), env.settings))
    assert abs((child.deadline - (datetime.now(UTC) + timedelta(hours=2))).total_seconds()) < 60


@pytest.mark.postgres_only
async def test_the_depth_limit_is_exact(env_factory):  # noqa: F811
    env = await env_factory(**CONT, continuation_max_depth=1)
    root = await make_root(env)
    child = await _spawn(env, intentions.with_bounds(_child(root), env.settings))
    assert child.depth == 1  # a child AT the limit is allowed: only its own children are not
    with pytest.raises(IntentionLimitReached, match="depth"):
        await env.heart.subtasks.create(task="deeper", intention=intentions.with_bounds(_child(child), env.settings))
    assert len(await env.heart.subtasks.list(limit=10)) == 2  # the refused spawn left no work row either


@pytest.mark.postgres_only
async def test_the_spawn_limit_counts_the_roots_rows(env_factory):  # noqa: F811
    env = await env_factory(**CONT, continuation_max_spawns_per_root=2)
    root = await make_root(env)
    first = await _spawn(env, intentions.with_bounds(_child(root), env.settings))
    await _spawn(env, intentions.with_bounds(_child(first), env.settings))  # a grandchild counts too
    with pytest.raises(IntentionLimitReached, match="spawn limit"):  # the depth refusal says "spawned" too
        over = intentions.with_bounds(_child(root), env.settings)
        await env.heart.subtasks.create(task="one too many", intention=over)


@pytest.mark.postgres_only
async def test_a_spawn_with_no_limits_is_never_counted(env_factory):  # noqa: F811  # PIN (Phase 1)
    env = await env_factory(**ON)
    chain = await make_root(env)
    for _ in range(3):
        chain = await _spawn(env, _child(chain))  # no with_bounds: depth 3 is fine, and so is deeper
    assert chain.depth == 3


class _Orchestrator:
    clock_wired = True
    approvals_wired = False

    def __init__(self, settings):
        self._settings = settings
        self.start_dag = AsyncMock()


PROD_SPAWNS = [
    ("spawn_task", {"task": "work", "intent": "next step"}),
    ("dag_create", {"name": "d", "nodes": [{"name": "n", "type": "subtask", "instructions": "x"}], "intent": "next"}),
]


@pytest.mark.postgres_only
@pytest.mark.parametrize(("tool", "args"), PROD_SPAWNS, ids=[s[0] for s in PROD_SPAWNS])
async def test_under_prods_flags_a_tool_spawn_past_every_bound_is_phase_1s(env_factory, tool, args):  # noqa: F811  # PIN
    """Prod runs intentions and the result inbox on and continuation off. Through the real
    construction site, the limits and the TTL are inert whatever they are set to: a spawn two
    levels past a depth limit of 1 and a spawn limit of 1 goes ahead, and no row gets a deadline."""
    env = await env_factory(
        **ON, continuation_max_depth=1, continuation_max_spawns_per_root=1, intention_root_ttl_hours=1
    )
    assert env.settings.continuation_enabled is False
    d = ToolDispatcher()
    register_subtask_tools(d, env.heart, env.settings)
    register_dag_tools(d, DAGStore(env.db, env.agent, env.settings), _Orchestrator(env.settings), settings=env.settings)
    chain = await make_root(env)
    for _ in range(2):
        ctx = ExecutionContext(
            kind="scheduled", session_id="subtask-p", intention_id=chain.id, root_intention_id=chain.root_id
        )
        text, is_error = await d.dispatch(tool, dict(args), session_id=ctx.session_id, context=ctx)
        assert not is_error, text
        async with env.db.session() as s:
            chain = (await s.execute(select(Intention).where(Intention.parent_id == chain.id))).scalar_one()
    assert chain.depth == 2
    async with env.db.session() as s:
        rows = (await s.execute(select(Intention).where(Intention.agent_id == env.agent))).scalars().all()
    assert len(rows) == 3 and [r.deadline for r in rows] == [None, None, None]


def test_every_intention_spec_site_applies_the_bounds():
    """A new construction site that forgets ``with_bounds`` would write roots with no deadline and
    children with no limit. Every function that builds an IntentionSpec (directly or through
    spec_from_tool_call) must also call ``intentions.with_bounds`` (the attribute form: this repo reaches the module,
    never its names, so ``from ... import with_bounds`` would be reported). Any such call in the enclosing function
    satisfies the check: a drift detector, not a proof of order."""
    repo = Path(__file__).resolve().parents[1]  # not the cwd: a scan that reads nothing must not pass
    builders = {"IntentionSpec", "spec_from_tool_call"}
    sites: list[str] = []
    unbounded: list[str] = []
    for path in sorted((repo / "nous").rglob("*.py")):
        if path.as_posix().endswith("nous/brain/intentions.py"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        funcs = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)]
        for call in (n for n in ast.walk(tree) if isinstance(n, ast.Call)):
            name = call.func.attr if isinstance(call.func, ast.Attribute) else getattr(call.func, "id", None)
            if name not in builders:
                continue
            site = f"{path.relative_to(repo).as_posix()}:{call.lineno}"
            sites.append(site)
            enclosing = [f for f in funcs if f.lineno <= call.lineno <= f.end_lineno]
            assert enclosing, f"{site} builds an IntentionSpec at module level"
            inner = min(enclosing, key=lambda f: f.end_lineno - f.lineno)
            applies = any(
                isinstance(n, ast.Call) and getattr(n.func, "attr", None) == "with_bounds" for n in ast.walk(inner)
            )
            if not applies:
                unbounded.append(site)
    assert sites, "the scan found no construction site at all"
    assert not unbounded, f"IntentionSpec built without with_bounds: {unbounded}"
