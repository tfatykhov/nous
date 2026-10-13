"""F099 Phase 3b: intention.cancel on the active card. Handler level first, then the real runner, router and route."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from f099_cards_support import CARDS, cards_env, tap  # noqa: F401
from f099_support import env_factory, finish, make_child, make_root, runner_env, set_intention  # noqa: F401
from sqlalchemy import select
from starlette.applications import Starlette

from nous import owner_actions
from nous.a2ui.actions import ActionContext, ActionRouter
from nous.a2ui.intention_keys import ACTIVE_KEY, proposal_key
from nous.api.intention_routes import build_intention_routes
from nous.brain import continuation
from nous.config import Settings
from nous.storage.models import Intention

ACTOR = "alice@example.com"
_OWN = object()  # "the context is the one naming the root"


def _router(runner, **over) -> ActionRouter:
    return ActionRouter(None, Settings(_env_file=None, **{**CARDS, **over}), None, continuation_runner=runner)


def _ctx(router, root_id, *, key=ACTIVE_KEY, listed=None, context=_OWN):
    listed = [root_id] if listed is None else listed
    surface = SimpleNamespace(
        surface_id="card-1",
        dedup_key=key,
        title="Active intentions",
        data_model={"fingerprint": "f", "roots": {str(r): {"status": "Running"} for r in listed}},
    )
    ctx = {"rootId": str(root_id)} if context is _OWN else context
    return ActionContext(
        surface=surface, name="intention.cancel", context=ctx, data_model=None, services=router, actor=ACTOR
    )


async def _run(router, ctx):
    return await router._handlers[ctx.name].fn(ctx)


def _runner(**kw):
    return SimpleNamespace(cancel_root=AsyncMock(**kw))


def test_the_verb_is_registered_mutating_and_irreversible_metadata_only():
    meta = _router(_runner())._handlers["intention.cancel"]
    assert meta.mutating is True and meta.irreversible is True


async def test_a_listed_root_is_cancelled_through_the_runner_and_its_line_is_patched():
    rid = uuid.uuid4()
    runner = _runner(return_value=continuation.CancelOutcome(root_id=rid, cancelled_intentions=2, cancelled_subtasks=1))
    router = _router(runner)
    result = await _run(router, _ctx(router, rid))
    assert result.ok and not result.resolve_surface
    assert result.message == "Cancelled: 2 piece(s) of work stopped"
    assert result.data_patches == [(f"/roots/{rid}/status", result.message)]
    runner.cancel_root.assert_awaited_once_with(rid, reason="cancelled from companion", actor=f"companion:{ACTOR}")


async def test_a_repeat_says_so_and_a_truncated_cascade_says_to_tap_again():
    rid = uuid.uuid4()
    router = _router(_runner(return_value=continuation.CancelOutcome(root_id=rid, already_cancelled=True)))
    assert (await _run(router, _ctx(router, rid))).message == "Already cancelled"
    truncated = continuation.CancelOutcome(root_id=rid, cancelled_intentions=1, truncated=True)
    router = _router(_runner(return_value=truncated))
    assert (await _run(router, _ctx(router, rid))).message.endswith("(more remain: tap again)")


@pytest.mark.parametrize(
    "context",
    [{}, {"rootId": "not-a-uuid"}, {"rootId": 7}, {"rootId": str(uuid.uuid4())}, "oops", None, ["rootId"]],
    ids=["none-named", "malformed", "not-a-string", "not-on-the-card", "str-context", "null-context", "list-context"],
)
async def test_a_root_not_on_the_card_is_refused_and_nothing_is_called(context):
    runner = _runner()
    router = _router(runner)
    result = await _run(router, _ctx(router, uuid.uuid4(), context=context))
    assert not result.ok and result.data_patches == [] and not result.resolve_surface
    runner.cancel_root.assert_not_awaited()


@pytest.mark.parametrize("key", [None, "", proposal_key(uuid.uuid4()), "intention:active:extra", "dag-approval:x"])
async def test_a_card_without_the_active_key_refuses_the_verb(key):
    runner = _runner()
    router = _router(runner)
    result = await _run(router, _ctx(router, uuid.uuid4(), key=key))
    assert not result.ok and result.message == "this button does not belong to this card"
    runner.cancel_root.assert_not_awaited()


async def test_without_a_runner_or_with_cards_off_the_card_says_continuation_is_not_running():
    rid = uuid.uuid4()
    for router in (_router(None), _router(_runner(), intention_cards_enabled=False)):
        result = await _run(router, _ctx(router, rid))
        assert not result.ok and result.message == "continuation is not running"


async def test_the_stores_refusals_are_the_fixed_sentences_and_leave_the_card_up():
    rid = uuid.uuid4()
    router = _router(_runner(side_effect=continuation.CancelRefused(continuation.REFUSE_FINISHED)))
    result = await _run(router, _ctx(router, rid))
    assert not result.ok and result.message == owner_actions.CANCEL_REFUSALS["finished"] and result.data_patches == []
    router = _router(_runner(side_effect=continuation.RootNotFound(str(rid))))
    assert (await _run(router, _ctx(router, rid))).message == "no such intention"


async def test_a_runner_failure_is_a_refusal_not_a_500_and_the_card_is_untouched(caplog):
    rid = uuid.uuid4()
    router = _router(_runner(side_effect=RuntimeError("db is down")))
    result = await _run(router, _ctx(router, rid))
    assert not result.ok and result.message == "the cancel could not be processed" and result.data_patches == []
    assert rid.hex[:8] in caplog.text and str(rid) not in caplog.text


# ---- the real runner, the real router, the REST route -------------------------------------------------------


async def _row(env, intention_id) -> Intention:
    async with env.db.session() as s:
        stmt = select(Intention).where(Intention.id == intention_id).execution_options(populate_existing=True)
        return (await s.execute(stmt)).scalar_one()


@pytest.mark.postgres_only
async def test_the_card_and_the_rest_route_leave_the_same_rows_and_say_the_same_refusal(cards_env):  # noqa: F811
    """One function, two surfaces: two identical roots, one cancelled from the card, one by POST."""
    env = await cards_env()
    via_card, via_rest, finished = await make_root(env), await make_root(env), await make_root(env)
    child_card, child_rest = await make_child(env, via_card), await make_child(env, via_rest)
    app = Starlette(routes=build_intention_routes(database=env.db, settings=env.settings, continuation_runner=env.cont))

    card = await _run(env.router, _ctx(env.router, via_card.id))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://nous") as client:
        rest = (await client.post(f"/intentions/{via_rest.id}/cancel", json={"actor": "t"})).json()
        again = (await client.post(f"/intentions/{via_rest.id}/cancel", json={"actor": "t"})).json()
    assert card.ok and card.message == f"Cancelled: {rest['cancelled_intentions']} piece(s) of work stopped"
    for root, child in ((via_card, child_card), (via_rest, child_rest)):
        assert (await _row(env, root.id)).root_cancelled_at is not None
        assert (await _row(env, root.id)).state == "cancelled" == (await _row(env, child.id)).state
    assert again["already_cancelled"] is True
    assert (await _run(env.router, _ctx(env.router, via_card.id))).message == "Already cancelled"

    # Nothing running any more: the subtask finished and the intention closed (as in test_f099_phase2e_routes).
    await finish(env, await env.heart.subtasks.get(uuid.UUID(finished.source_id)))
    await set_intention(env, finished.id, state="closed", close_reason="resolved")
    refused = await _run(env.router, _ctx(env.router, finished.id))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://nous") as client:
        route = await client.post(f"/intentions/{finished.id}/cancel", json={})
    assert route.status_code == 409 and route.json()["refusal"] == "finished"
    assert not refused.ok and refused.message == owner_actions.CANCEL_REFUSALS["finished"]


@pytest.mark.postgres_only
async def test_a_root_not_on_the_card_is_refused_and_untouched(cards_env):  # noqa: F811
    env = await cards_env()
    listed, unlisted = await make_root(env), await make_root(env)
    result = await _run(env.router, _ctx(env.router, unlisted.id, listed=[listed.id]))
    assert not result.ok
    row = await _row(env, unlisted.id)
    assert row.root_cancelled_at is None and row.state != "cancelled"


@pytest.mark.postgres_only
async def test_a_tap_through_the_router_cancels_and_patches_the_server_copy(cards_env):  # noqa: F811
    """The whole gate: surface live, allowed action, shape, nonce, handler, patch, audit. Built by the real builder."""
    from nous.a2ui.builders.intention_cards import intention_active

    env = await cards_env()
    root = await make_root(env)
    async with env.db.session() as s:
        (view,) = await continuation.list_roots(s, env.agent, state="open", limit=10, settings=env.settings)
    maxima = {"depth": 3, "spawns": 12, "turns": 8, "tokens": 400000}
    built = intention_active({"roots": [view], "reports": {}, "maxima": maxima})
    surface_id = await env.service.push_built(built, dedup_key=ACTIVE_KEY, notify=False, reserved_key_ok=True)

    status, payload = await tap(env, surface_id, "intention.cancel", {"rootId": str(root.id)}, actor=ACTOR)
    assert status == 200 and payload["ok"] and not payload["resolved"]
    assert (await _row(env, root.id)).root_cancelled_at is not None
    model = await env.service.live_data_model(ACTIVE_KEY)
    assert model["roots"][str(root.id)]["status"].startswith("Cancelled: ")
    status, _ = await tap(env, surface_id, "intention.cancel", {"rootId": str(uuid.uuid4())}, actor=ACTOR)
    assert status == 422  # not on the card: ACTION_FAILED, nothing cancelled
