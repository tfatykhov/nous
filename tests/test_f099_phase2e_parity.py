"""F099 Phase 2e: on prod's exact flags (inbox, intentions and result memory ON, continuation OFF) nothing of 2e runs
before the flip commit. One file, so a reviewer reads the whole claim in one place."""

from __future__ import annotations

import ast
import inspect
import textwrap
import uuid
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest
from f099_support import (
    ON,
    env_factory,  # noqa: F401
    finish,
    inbox_rows,
    intention_of,
    make_root,
    make_subtask,
    runner_env,  # noqa: F401
)
from sqlalchemy import select
from starlette.applications import Starlette
from test_f099_phase2c_parity import PROD, NoDatabase, Untouchable

import nous.main as main
from nous.api import runner as runner_module
from nous.api.intention_routes import build_intention_routes
from nous.api.rest import create_app
from nous.brain import continuation
from nous.config import Settings
from nous.handlers.continuation_runner import ContinuationRunner
from nous.heart.result_reconciler import build_reconciler, repair_missing_results
from nous.storage.models import Intention

pytestmark = pytest.mark.postgres_only


async def _call(app, method, path, **kwargs) -> httpx.Response:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://nous") as client:
        return await client.request(method, path, **kwargs)


# ---- the store ---------------------------------------------------------------------------------------------


async def test_prods_writers_and_passes_never_reach_a_2e_store_path(env_factory, monkeypatch):  # noqa: F811  # PIN
    """Every 2e change to the store is behind the runner or behind ``continuation.enabled``. On prod's flags a
    subtask finishes, the reconciler ticks and the repair runs, and none of the changed functions is called."""

    def forbidden(name):
        async def raiser(*args, **kwargs):
            raise AssertionError(f"continuation.{name} ran on prod's flags")

        return raiser

    for name in (
        "record_result",
        "cancel_root",
        "close_cancelled_source",
        "end_hanging_root",
        "fail_attempt",
        "expire_roots",
        "stray_dag_ids",
        "stalled_approved_ids",
        "list_roots",
    ):
        monkeypatch.setattr(continuation, name, forbidden(name))
    env = await env_factory(**PROD, telegram_chat_id="4242")
    st = await make_subtask(env)  # a continue-policy subtask, spawned from chat
    await finish(env, st)
    assert await env.pool._record_inbox(st) in (True, False, None)  # the F098 writer, whatever it reports
    (row,) = await inbox_rows(env, st.id)
    assert row.channel == "telegram:8080" and row.delivered_at is None  # keyed by its channel, as in Phase 1
    assert (await intention_of(env, "subtask", st.id)).close_reason == "legacy"  # not 'delivered', not result_ready
    assert await repair_missing_results(env.db, env.heart.result_inbox, env.settings, limit=50) == 0
    reconciler = build_reconciler(env.db, env.heart.result_inbox, env.settings, continuation_wake=MagicMock())
    await reconciler.run_once()  # none of its passes touches a forbidden function


async def test_prods_rollback_finds_nothing_new(env_factory):  # noqa: F811  # PIN
    env = await env_factory(**ON, telegram_chat_id="4242")
    report = await continuation.rollback_at_startup(env.db, env.settings, telegram_push=None)
    assert report == continuation.RollbackReport(0, 0, 0, 0, 0)


async def test_a_new_intention_starts_with_no_failed_tokens(env_factory):  # noqa: F811  # PIN
    env = await env_factory(**PROD)
    root = await make_root(env)
    async with env.db.session() as s:
        row = (await s.execute(select(Intention).where(Intention.id == root.id))).scalar_one()
    assert row.failed_tokens == 0


def test_2e_added_one_migration_and_no_setting():  # PIN: changes when a later PR adds one on purpose
    migrations = sorted((Path(__file__).resolve().parents[1] / "sql" / "migrations").glob("*.sql"))
    assert migrations[-1].name == "085_intention_failed_tokens_and_cancel_index.sql"
    assert not [name for name in Settings.model_fields if "cancel" in name or "failed_tokens" in name]


# ---- the runner, the view and the wiring ------------------------------------------------------------------------


def test_the_cancel_view_is_a_set_lookup_and_the_default_reads_nothing():  # PIN
    """`_authorize_tool_call` runs on every tool call of every lineage turn in prod: its added cost is one
    attribute read and one call. With no runner the call is a function that returns False; with one it is a set
    membership. Neither awaits, neither reads a row."""
    assert not inspect.iscoroutinefunction(ContinuationRunner.root_is_cancelled)
    tree = ast.parse(textwrap.dedent(inspect.getsource(ContinuationRunner.root_is_cancelled)))
    assert not [node for node in ast.walk(tree) if isinstance(node, ast.Await)]
    assert runner_module._no_cancelled_roots(object()) is False
    source = inspect.getsource(runner_module.AgentRunner._authorize_tool_call)
    strict_block = source.index("if ctx.authority == AUTHORITY_INTERNAL or ctx.kind")
    head = source[:strict_block]  # the new check comes before even the strict rule: the first thing it does
    assert "self._root_cancelled(ctx.root_intention_id)" in head


async def test_prods_flags_install_no_view_and_build_no_runner(monkeypatch):  # PIN
    """Even with the constant flipped (the flip commit), prod's flags build no runner, so no view is installed."""
    monkeypatch.setattr(continuation, "CONTINUATION_RUNNER_READY", True)
    agent_runner = MagicMock()
    untouched = {name: Untouchable() for name in ("database", "heart", "brain", "bus", "dispatcher")}
    built = await main._build_continuation_runner(Settings(_env_file=None, **PROD), runner=agent_runner, **untouched)
    assert built is None
    agent_runner.set_cancelled_roots.assert_not_called()


async def test_a_built_runner_installs_its_view_and_loads_it_before_anything_runs(runner_env, monkeypatch):  # noqa: F811
    monkeypatch.setattr(continuation, "CONTINUATION_RUNNER_READY", True)
    env = await runner_env()
    agent_runner = MagicMock()
    order = []
    agent_runner.set_cancelled_roots.side_effect = lambda view: order.append(("view", view))
    real = ContinuationRunner.load_cancelled_roots

    async def load(self):
        order.append(("load", None))
        return await real(self)

    monkeypatch.setattr(ContinuationRunner, "load_cancelled_roots", load)
    built = await main._build_continuation_runner(
        env.settings,
        database=env.db,
        runner=agent_runner,
        heart=env.heart,
        brain=env.brain,
        bus=None,
        dispatcher=env.dispatcher,
    )
    assert [kind for kind, _ in order] == ["view", "load"]
    assert order[0][1] == built.root_is_cancelled and built._task is None  # wired, loaded, not started


async def test_a_view_that_cannot_be_loaded_does_not_fail_the_build(runner_env, monkeypatch, caplog):  # noqa: F811
    """N2 of the plan review: `start()` loads the view again and every sweep refreshes it, so a transient error in
    the first load is a warning, never a failed `create_components`."""
    monkeypatch.setattr(continuation, "CONTINUATION_RUNNER_READY", True)
    env = await runner_env()
    agent_runner = MagicMock()

    async def failing(self):
        raise RuntimeError("the database blinked")

    monkeypatch.setattr(ContinuationRunner, "load_cancelled_roots", failing)
    built = await main._build_continuation_runner(
        env.settings,
        database=env.db,
        runner=agent_runner,
        heart=env.heart,
        brain=env.brain,
        bus=None,
        dispatcher=env.dispatcher,
    )
    assert isinstance(built, ContinuationRunner)
    agent_runner.set_cancelled_roots.assert_called_once_with(built.root_is_cancelled)
    assert "could not load the cancelled roots" in caplog.text


def test_main_binds_the_orchestrators_cancel_where_the_orchestrator_exists():  # PIN
    source = inspect.getsource(main.create_components)
    bound = source.index("continuation_runner.set_cancel_dag(dag_orchestrator.cancel_dag)")
    assert source.index("dag_orchestrator = DAGOrchestrator(") < bound
    assert source.rindex("if continuation_runner is not None:", 0, bound) > source.index(
        "dag_orchestrator = DAGOrchestrator("
    )


async def test_an_inert_runner_runs_none_of_the_2e_sweep_on_prods_flags():  # PIN
    settings = Settings(_env_file=None, telegram_bot_token="test-token", **PROD)
    db = NoDatabase()
    runner = ContinuationRunner(
        database=db, settings=settings, runner=Untouchable(), heart=Untouchable(), brain=Untouchable()
    )
    await runner.run_once()
    await runner.start()
    assert db.sessions == 0 and runner._cancelled == set() and runner._push_task is None
    assert not runner.root_is_cancelled(uuid.uuid4())


# ---- REST ---------------------------------------------------------------------------------------------------------


async def test_the_new_routes_on_prods_flags_answer_empty_503_and_404_and_change_nothing(env_factory):  # noqa: F811  # PIN
    env = await env_factory(**PROD)
    root = await make_root(env)  # a Phase 1 root exists in prod
    app = Starlette(routes=build_intention_routes(database=env.db, settings=env.settings, continuation_runner=None))
    listed = await _call(app, "GET", "/intentions")
    assert (listed.status_code, listed.json()) == (200, {"roots": [], "continuation": False})
    assert (await _call(app, "POST", f"/intentions/{root.id}/cancel", json={})).status_code == 503
    assert (await _call(app, "POST", f"/intentions/{'ab' * 16}/cancel", json={})).status_code == 404
    fresh = await intention_of(env, "subtask", root.source_id)
    assert (fresh.state, fresh.root_cancelled_at) == ("pending", None)


async def test_the_new_routes_read_no_row_to_answer_the_list_on_prods_flags(env_factory):  # noqa: F811  # PIN
    env = await env_factory(**PROD)
    app = Starlette(
        routes=build_intention_routes(database=NoDatabase(), settings=env.settings, continuation_runner=None)
    )
    assert (await _call(app, "GET", "/intentions?state=all&limit=100")).json() == {"roots": [], "continuation": False}


def test_create_app_mounts_the_new_routes_and_keeps_the_old_ones():
    settings = Settings(_env_file=None, **PROD)
    app = create_app(MagicMock(), MagicMock(), MagicMock(), MagicMock(), MagicMock(), settings)
    paths = {getattr(route, "path", None) for route in app.routes}
    assert {"/intentions", "/intentions/{root_id}/cancel", "/intentions/proposals"} <= paths
    assert {"/chat", "/status", "/decisions", "/subtasks/{id}", "/schedules"} <= paths  # nothing was displaced


def test_a_cancel_is_not_a_tool():  # PIN: Review Focus 1
    from nous.api.tool_classes import TOOL_CLASSES

    for name in ("cancel_root", "cancel_intention", "intentions", "list_intentions"):
        assert name not in TOOL_CLASSES
    source = inspect.getsource(main.create_components)
    assert 'register("cancel_root"' not in source and "register('cancel_root'" not in source
