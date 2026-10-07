"""F099 Phase 2e-7: the owner's view of the roots (GET /intentions) and the cancel (POST /intentions/{root}/cancel)."""

from __future__ import annotations

import asyncio
import inspect
import uuid
from datetime import UTC, datetime
from unittest.mock import MagicMock

import httpx
import pytest
from f099_support import (
    CONT,
    ON,
    add_arrival,
    ask_with_proposals,
    claim,
    env_factory,  # noqa: F401
    finish,
    make_child,
    make_root,
    record,
    runner_env,  # noqa: F401
    set_intention,
)
from sqlalchemy import select
from starlette.applications import Starlette
from test_f099_phase2c_parity import PROD

import nous.main as main
from nous import owner_actions
from nous.api import intention_routes
from nous.api.intention_routes import build_intention_routes
from nous.api.rest import create_app
from nous.brain import continuation
from nous.handlers.continuation_runner import ContinuationRunner
from nous.storage.models import Intention

pytestmark = pytest.mark.postgres_only


def _app(env, runner) -> Starlette:
    return Starlette(routes=build_intention_routes(database=env.db, settings=env.settings, continuation_runner=runner))


def _runner(env) -> ContinuationRunner:
    return ContinuationRunner(
        database=env.db,
        settings=env.settings,
        runner=env.runner,
        heart=env.heart,
        brain=env.brain,
        bus=env.bus,
        dispatcher=env.dispatcher,
    )


async def _call(app, method, path, **kwargs) -> httpx.Response:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://nous") as client:
        return await client.request(method, path, **kwargs)


async def _row(env, intention_id) -> Intention:
    async with env.db.session() as s:
        return (
            await s.execute(
                select(Intention).where(Intention.id == intention_id).execution_options(populate_existing=True)
            )
        ).scalar_one()


# ---- GET /intentions -----------------------------------------------------------------------------------------


async def test_the_list_shows_the_open_roots_newest_first_with_their_lineage_and_budgets(runner_env):  # noqa: F811
    env = await runner_env()
    older = await make_root(env)
    newer = await make_root(env)
    child = await make_child(env, newer)
    await record(env, newer)
    got = await claim(env, newer.id)
    assert got is not None
    response = await _call(_app(env, _runner(env)), "GET", "/intentions")
    assert response.status_code == 200
    body = response.json()
    assert body["continuation"] is True
    assert [r["id"] for r in body["roots"]] == [str(newer.id), str(older.id)]
    view = body["roots"][0]
    assert view["short_id"] == newer.id.hex[:8] and view["intent"] == newer.intent and view["state"] == "deciding"
    assert view["open_rows"] == 2 and view["root_cancelled_at"] is None
    assert [row["id"] for row in view["lineage"]] == [str(newer.id), str(child.id)]
    assert view["lineage"][1]["parent_id"] == str(newer.id) and view["lineage_truncated"] is False
    assert set(view["limits"]) == {"depth", "spawns", "turns", "tokens", "stalls", "spawn_blocked", "escalate"}
    assert view["limits"]["depth"] == 1 and view["limits"]["spawns"] == 1
    assert view["arrivals"] == [] and view["open_proposals"] == []


async def test_the_list_leaves_out_finished_cancelled_and_expired_roots_unless_asked_for_all(runner_env):  # noqa: F811
    env = await runner_env()
    open_root = await make_root(env)
    finished = await make_root(env)
    await finish(env, await env.heart.subtasks.get(uuid.UUID(finished.source_id)))
    await set_intention(env, finished.id, state="closed", close_reason="resolved")
    cancelled = await make_root(env)
    await _runner(env).cancel_root(cancelled.id, reason="t", actor="t")
    expired = await make_root(env)
    await set_intention(env, expired.id, state="expired", close_reason="expired", root_expired_at=datetime.now(UTC))
    app = _app(env, _runner(env))
    open_ids = {r["id"] for r in (await _call(app, "GET", "/intentions")).json()["roots"]}
    all_ids = {r["id"] for r in (await _call(app, "GET", "/intentions?state=all&limit=100")).json()["roots"]}
    assert open_ids == {str(open_root.id)}
    assert all_ids == {str(r.id) for r in (open_root, finished, cancelled, expired)}
    listed = {r["id"]: r for r in (await _call(app, "GET", "/intentions?state=all")).json()["roots"]}
    assert (
        listed[str(cancelled.id)]["root_cancelled_at"] is not None and listed[str(cancelled.id)]["state"] == "cancelled"
    )


async def test_the_list_carries_the_proposals_the_owner_can_still_decide_and_the_arrivals(runner_env):  # noqa: F811
    env = await runner_env()
    asked = await ask_with_proposals(env)
    (view,) = (await _call(_app(env, _runner(env)), "GET", "/intentions")).json()["roots"]
    assert view["state"] == "awaiting_owner"
    assert [p["id"] for p in view["open_proposals"]] == [str(asked.ids[0])]
    assert [(a["n"], a["decision"], a["outcome"]) for a in view["arrivals"]] == [(1, "ask", "resolved")]


async def test_a_container_is_listed_without_budgets(runner_env):  # noqa: F811
    from nous.brain.intentions import IntentionSpec

    env = await runner_env()
    await env.heart.schedules.create(
        task="watch",
        schedule_type="recurring",
        interval_seconds=1800,
        intention=IntentionSpec(intent="Watch the snow", origin_kind="interactive", container=True),
    )
    (view,) = (await _call(_app(env, _runner(env)), "GET", "/intentions")).json()["roots"]
    assert (view["wake_policy"], view["limits"]) == ("container", None)


async def test_a_root_with_the_expiry_marker_is_not_open_even_with_an_open_row(runner_env):  # noqa: F811  # PIN
    """Review m2: the marker half of the open filter on its own. The root and its child are still pending (an
    expiry and its close commit together, so this is defense in depth), and only the marker leaves the root out."""
    env = await runner_env()
    marked = await make_root(env)
    await make_child(env, marked)
    await set_intention(env, marked.id, root_expired_at=datetime.now(UTC))
    other = await make_root(env)
    app = _app(env, _runner(env))
    assert [r["id"] for r in (await _call(app, "GET", "/intentions")).json()["roots"]] == [str(other.id)]
    assert (await _row(env, marked.id)).state == "pending"


async def test_the_view_lists_the_newest_arrivals_oldest_first_and_says_when_the_lineage_is_cut(  # PIN
    runner_env,  # noqa: F811
    monkeypatch,
):
    """Review m3: the caps reached. The newest ``ARRIVALS_VIEW_MAX`` arrivals, in the order they came; a lineage
    longer than ``LINEAGE_VIEW_MAX`` is cut and says so (the cap is lowered here: a real lineage of 51 rows would
    first meet the spawn limit)."""
    env = await runner_env()
    root = await make_root(env)
    await make_child(env, root)
    for n in range(1, continuation.ARRIVALS_VIEW_MAX + 3):
        await add_arrival(env, root.id, n)
    monkeypatch.setattr(continuation, "LINEAGE_VIEW_MAX", 1)
    (view,) = (await _call(_app(env, _runner(env)), "GET", "/intentions")).json()["roots"]
    newest = list(range(3, continuation.ARRIVALS_VIEW_MAX + 3))
    assert [a["n"] for a in view["arrivals"]] == newest
    assert [row["id"] for row in view["lineage"]] == [str(root.id)] and view["lineage_truncated"] is True


async def test_a_bad_limit_or_state_is_400(runner_env):  # noqa: F811
    env = await runner_env()
    app = _app(env, _runner(env))
    for query in ("limit=0", "limit=101", "limit=x", "limit=%C2%B2", "state=everything"):
        assert (await _call(app, "GET", f"/intentions?{query}")).status_code == 400


# ---- POST /intentions/{root}/cancel -----------------------------------------------------------------------------


async def test_cancelling_over_rest_cancels_the_lineage_and_answers_with_the_counts(runner_env):  # noqa: F811
    env = await runner_env()
    root = await make_root(env)
    await make_child(env, root)
    app = _app(env, _runner(env))
    response = await _call(
        app,
        "POST",
        f"/intentions/{root.id.hex[:8]}/cancel",
        json={"reason": "no longer wanted", "actor": "telegram:42"},
    )
    assert response.status_code == 200
    assert response.json() == {
        "root_id": str(root.id),
        "short_id": root.id.hex[:8],
        "already_cancelled": False,
        "cancelled_intentions": 2,
        "cancelled_subtasks": 2,
        "cancelled_dags": 0,
        "cancelled_proposals": 0,
        "deactivated_schedules": 0,
        "turn_stopped": False,
    }
    assert (await _row(env, root.id)).root_cancelled_at is not None
    again = await _call(app, "POST", f"/intentions/{root.id}/cancel")  # no body at all: allowed
    assert again.status_code == 200 and again.json()["already_cancelled"] is True


async def test_a_cancel_of_work_that_is_finished_is_409_and_writes_nothing(runner_env):  # noqa: F811
    env = await runner_env()
    root = await make_root(env)
    await env.heart.subtasks.cancel(uuid.UUID(root.source_id))
    await set_intention(env, root.id, state="closed", close_reason="legacy")
    response = await _call(_app(env, _runner(env)), "POST", f"/intentions/{root.id}/cancel", json={})
    assert response.status_code == 409
    # The root's current state rides along, as on 2d's decide 409 (OQ6, 2e-7 review m4).
    assert response.json() == {
        "error": owner_actions.CANCEL_REFUSALS["finished"],
        "state": "closed",
        "refusal": "finished",
    }
    assert (await _row(env, root.id)).root_cancelled_at is None


async def test_a_cancel_answers_404_for_a_child_or_an_unknown_id_and_400_for_a_bad_one(runner_env):  # noqa: F811
    env = await runner_env()
    root = await make_root(env)
    child = await make_child(env, root)
    app = _app(env, _runner(env))
    assert (await _call(app, "POST", f"/intentions/{child.id}/cancel", json={})).status_code == 404
    assert (await _call(app, "POST", f"/intentions/{uuid.uuid4()}/cancel", json={})).status_code == 404
    for bad in ("nothex!!", "abc", "x" * 40):
        assert (await _call(app, "POST", f"/intentions/{bad}/cancel", json={})).status_code == 400
    assert (await _call(app, "POST", f"/intentions/{root.id}/cancel", content=b"[1, 2]")).status_code == 400
    assert (await _row(env, root.id)).root_cancelled_at is None


async def test_with_no_runner_a_known_root_is_503_and_an_unknown_one_404_and_nothing_is_cancelled(runner_env):  # noqa: F811
    """Prod's shape: Phase 1 roots exist, no runner does. A store-only cancel would cancel a real lineage without the
    runner that stops its turn and its DAGs, so the route does not cancel without one."""
    env = await runner_env()
    root = await make_root(env)
    child = await make_child(env, root)
    app = _app(env, None)
    assert (await _call(app, "POST", f"/intentions/{root.id}/cancel", json={})).status_code == 503
    assert (await _call(app, "POST", f"/intentions/{uuid.uuid4()}/cancel", json={})).status_code == 404
    # A child's id names no root (find_root_id's root-only filter), so it is 404 here too, never 503 (review m1).
    assert (await _call(app, "POST", f"/intentions/{child.id}/cancel", json={})).status_code == 404
    fresh = await _row(env, root.id)
    assert (fresh.state, fresh.root_cancelled_at) == ("pending", None)


async def test_the_cancel_stops_a_running_turn_through_the_route(runner_env):  # noqa: F811
    started, never = asyncio.Event(), asyncio.Event()

    async def blocked(_kwargs):
        started.set()
        await never.wait()

    env = await runner_env(blocked)
    root = await make_root(env)
    await record(env, root)
    cont = _runner(env)
    await cont.run_once()
    await asyncio.wait_for(started.wait(), timeout=30)
    response = await _call(_app(env, cont), "POST", f"/intentions/{root.id}/cancel", json={})
    assert response.status_code == 200 and response.json()["turn_stopped"] is True
    assert cont.running_roots == frozenset()


# ---- prod's exact flags --------------------------------------------------------------------------------------------


async def test_the_list_with_continuation_off_is_empty_and_reads_no_row(env_factory):  # noqa: F811  # PIN
    """Prod: intentions and the inbox on, continuation off. Phase 1 roots exist, and the list answers without
    reading them: the view is the continuation's."""
    env = await env_factory(**ON)
    await make_root(env)

    class NoDatabase:
        def session(self):
            raise AssertionError("the list read the database with continuation off")

    app = Starlette(
        routes=build_intention_routes(database=NoDatabase(), settings=env.settings, continuation_runner=None)
    )
    response = await _call(app, "GET", "/intentions")
    assert (response.status_code, response.json()) == (200, {"roots": [], "continuation": False})
    assert (await _call(app, "GET", "/intentions?state=all")).json() == {"roots": [], "continuation": False}


async def test_the_new_routes_are_mounted_by_create_app_and_never_shadow_the_2d_ones(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    routes = build_intention_routes(database=env.db, settings=env.settings, continuation_runner=None)
    paths = [route.path for route in routes]
    assert paths.index("/intentions/proposals") < paths.index("/intentions/{root_id}/cancel")
    assert "/intentions" in paths and paths.count("/intentions/{root_id}/cancel") == 1
    app = _app(env, None)
    assert (await _call(app, "GET", "/intentions/proposals")).json() == {"proposals": []}  # not read as a root id


def test_no_new_route_has_a_model_path():  # PIN
    """Cancel is an owner action: the module that holds it registers no tool and imports no dispatcher."""
    source = inspect.getsource(intention_routes)
    assert "dispatcher" not in source and "register(" not in source
    from nous.api.tool_classes import TOOL_CLASSES

    assert "cancel_root" not in TOOL_CLASSES and "cancel_intention" not in TOOL_CLASSES


def test_the_cancel_refusal_vocabulary_is_keyed_by_the_stores_codes():  # PIN
    assert set(owner_actions.CANCEL_REFUSALS) == {continuation.REFUSE_FINISHED}


async def test_through_create_app_on_prods_flags_the_routes_answer_empty_503_404_or_400_and_write_nothing(env_factory):  # noqa: F811
    """Prod: intentions, the inbox and result memory on, continuation off, and no runner (``main`` passes a proxy
    that is falsy). Through the real ``create_app``: the list is empty, a Phase 1 root's cancel is 503, any other id
    404, malformed input 400 and never a 500, and the root is not touched."""
    env = await env_factory(**PROD)
    root = await make_root(env)
    before = await _row(env, root.id)
    proxy = main._lazy_component({"continuation_runner": None}, "continuation_runner")
    app = create_app(
        MagicMock(), MagicMock(), MagicMock(), MagicMock(), env.db, env.settings, continuation_runner=proxy
    )
    for query in ("", "?state=all", "?state=open&limit=100"):
        listed = await _call(app, "GET", f"/intentions{query}")
        assert (listed.status_code, listed.json()) == (200, {"roots": [], "continuation": False})
    for query in (
        "limit=0",
        "limit=x",
        "limit=%C2%B2",
        f"limit={1 << 70}",
        "limit=%00",
        "limit=%ED%A0%80",
        "state=%00",
    ):
        assert (await _call(app, "GET", f"/intentions?{query}")).status_code == 400
    known = [
        await _call(app, "POST", f"/intentions/{root.id}/cancel", json={"reason": "r", "actor": "telegram:42"}),
        await _call(app, "POST", f"/intentions/{root.id.hex[:8]}/cancel"),  # no body at all
        await _call(app, "POST", f"/intentions/{root.id}/cancel", content=b'{"reason": "\\u0000\\ud800"}'),
    ]
    assert [r.status_code for r in known] == [503, 503, 503]
    assert (await _call(app, "POST", f"/intentions/{uuid.uuid4()}/cancel", json={})).status_code == 404
    for bad in ("nothex!!", "abc", "x" * 40, "%00" * 8, "%C2%B2" * 8, "%ED%A0%80" * 4):
        assert (await _call(app, "POST", f"/intentions/{bad}/cancel", json={})).status_code == 400
    for body in (b"[1, 2]", b"not json", b'"text"', b"\xff\xfe"):
        assert (await _call(app, "POST", f"/intentions/{root.id}/cancel", content=body)).status_code == 400
    after = await _row(env, root.id)
    assert (after.state, after.root_cancelled_at, after.updated_at) == (
        before.state,
        before.root_cancelled_at,
        before.updated_at,
    )
