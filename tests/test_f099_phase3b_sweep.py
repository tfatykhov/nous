"""F099 Phase 3b: the runner keeps one live Active intentions card in step with the roots."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from f099_cards_support import CARDS, card_row, cards_env, live_cards, report  # noqa: F401
from f099_dashboard_support import put_intention
from f099_support import CONT, add_arrival, ask_with_proposals, env_factory, make_root, runner_env  # noqa: F401
from test_f099_phase2c_parity import NoDatabase

from nous.a2ui.builders import intention_cards
from nous.a2ui.intention_keys import ACTIVE_KEY
from nous.brain import continuation
from nous.handlers.continuation_runner import ContinuationRunner

pytestmark = pytest.mark.postgres_only


async def test_no_open_root_means_no_card_and_a_card_is_closed_when_the_last_root_ends(cards_env):  # noqa: F811
    env = await cards_env()
    assert await env.cont._active_card() == 0 and await card_row(env, ACTIVE_KEY) is None
    root = await make_root(env)
    assert await env.cont._active_card() == 1
    await env.cont.cancel_root(root.id, reason="t", actor="t")
    assert await env.cont._active_card() == 1  # the one card it closed
    assert (await card_row(env, ACTIVE_KEY)).status == "resolved"
    assert await env.service.live_data_model(ACTIVE_KEY) is None


async def test_one_open_root_is_pushed_silently_with_the_reserved_key(cards_env):  # noqa: F811
    env = await cards_env(fcm=True)
    root = await make_root(env)
    assert await env.cont._active_card() == 1
    card = await card_row(env, ACTIVE_KEY)
    assert (card.kind, card.origin, card.priority) == ("intention_active", "continuation", 0)
    assert card.allowed_actions == ["intention.cancel"] and card.push_notified_at is None  # never notifies
    assert env.pings == [] and set(card.data_model["roots"]) == {str(root.id)}


async def test_a_schedule_container_is_not_on_the_card_but_the_bot_still_lists_it(cards_env):  # noqa: F811
    # A container stays open for as long as its schedule lives, and its cancel deactivates the schedule for good:
    # it is not work in flight, so the card leaves it out (prod had five open containers and no other root).
    env = await cards_env()
    container = await put_intention(env, intent="a daily report", wake_policy="container")
    assert await env.cont._active_card() == 0 and await card_row(env, ACTIVE_KEY) is None
    async with env.db.session() as session:
        listed = await continuation.list_roots(session, env.agent, state="open", limit=11, settings=env.settings)
    assert [v["id"] for v in listed] == [str(container.id)]  # GET /intentions and the bot are unchanged
    root = await make_root(env)
    assert await env.cont._active_card() == 1
    assert set((await card_row(env, ACTIVE_KEY)).data_model["roots"]) == {str(root.id)}


async def test_a_card_the_censor_refuses_is_logged_once_and_never_raises(cards_env, monkeypatch, caplog):  # noqa: F811
    env = await cards_env()
    await make_root(env)
    monkeypatch.setattr(env.service, "push_built", AsyncMock(side_effect=PermissionError("censored")))
    with caplog.at_level("INFO", logger="nous.handlers.continuation_runner"):
        assert await env.cont._active_card() == 0
        assert await env.cont._active_card() == 0  # the same state: no second line
    refused = [r for r in caplog.records if "refused by a censor" in r.getMessage()]
    assert len(refused) == 1 and refused[0].levelname == "INFO"


async def test_an_unchanged_state_is_not_pushed_again_and_each_change_is(cards_env):  # noqa: F811
    env = await cards_env()
    root = await make_root(env)
    await env.cont._active_card()
    first = await card_row(env, ACTIVE_KEY)
    assert await env.cont._active_card() == 0
    assert (await card_row(env, ACTIVE_KEY)).nonce == first.nonce  # no churn: a replacement would have rotated it
    await add_arrival(env, root.id, 1)  # the newest arrival moves
    assert await env.cont._active_card() == 1
    moved = await card_row(env, ACTIVE_KEY)
    assert moved.surface_id == first.surface_id and moved.nonce != first.nonce  # replaced in place
    assert moved.data_model["fingerprint"] != first.data_model["fingerprint"]


async def test_what_the_owner_is_awaited_on_and_the_newest_report_reach_the_card(cards_env):  # noqa: F811
    env = await cards_env()
    asked = await ask_with_proposals(env, count=1)
    await env.cont._active_card()
    model = (await card_row(env, ACTIVE_KEY)).data_model["roots"][str(asked.root.id)]
    assert model["awaiting_text"] == f"Waiting for you: proposal {str(asked.ids[0]).replace('-', '')[:8]}"
    assert model["report_text"] == ""
    await report(env, asked.root, asked.root, "Powder is deep.", hours_ago=1)
    assert await env.cont._active_card() == 1  # a new report is a change
    shown = (await card_row(env, ACTIVE_KEY)).data_model["roots"][str(asked.root.id)]["report_text"]
    assert "Powder is deep." in shown


async def test_more_roots_than_the_card_lists_add_the_more_line(cards_env, monkeypatch):  # noqa: F811
    monkeypatch.setattr(intention_cards, "ACTIVE_CARD_ROOTS", 2)
    env = await cards_env()
    for _ in range(3):
        await make_root(env)
    await env.cont._active_card()
    card = await card_row(env, ACTIVE_KEY)
    assert len(card.data_model["roots"]) == 2 and any(c["id"] == "more" for c in card.components)


async def test_the_step_is_part_of_run_once_and_a_failure_in_it_does_not_stop_the_sweep(cards_env, monkeypatch):  # noqa: F811
    env = await cards_env()
    monkeypatch.setattr(env.cont, "_active_card", AsyncMock(side_effect=RuntimeError("boom")))
    swept = await env.cont.run_once()
    env.cont._active_card.assert_awaited_once()
    assert isinstance(swept, continuation.SweepReport)


async def test_run_once_without_the_active_step_leaves_a_live_active_card_alone(cards_env, monkeypatch):  # noqa: F811  # PIN
    """3a's settle sweep parses `intention:*` keys. It must not close the active card (kind `active`). Mutation:
    make the settle sweep treat an unknown kind as settled and this fails."""
    env = await cards_env()
    await make_root(env)
    await env.cont._active_card()
    monkeypatch.setattr(env.cont, "_active_card", AsyncMock(return_value=0))
    await env.cont.run_once()
    assert [c.dedup_key for c in await live_cards(env)] == [ACTIVE_KEY]


async def test_with_cards_off_the_step_touches_nothing(runner_env):  # noqa: F811  # PIN
    """Flag off: not a session, not a push, not a close. Mutation: move the gate below `self._db.session()` and the
    session count is no longer 0."""
    env = await runner_env(**CONT)
    db = NoDatabase()
    service = SimpleNamespace(push_built=AsyncMock(), close_by_dedup_key=AsyncMock(), live_data_model=AsyncMock())
    runner = ContinuationRunner(
        database=db,
        settings=env.settings,
        runner=env.runner,
        heart=env.heart,
        brain=env.brain,
        bus=env.bus,
        dispatcher=env.dispatcher,
    )
    runner.set_surface_service(service)
    assert await runner._active_card() == 0 and db.sessions == 0
    service.push_built.assert_not_awaited()
    service.close_by_dedup_key.assert_not_awaited()
    service.live_data_model.assert_not_awaited()


async def test_without_a_surface_service_the_step_touches_nothing(runner_env):  # noqa: F811  # PIN
    env = await runner_env(**CARDS)
    db = NoDatabase()
    runner = ContinuationRunner(
        database=db,
        settings=env.settings,
        runner=env.runner,
        heart=env.heart,
        brain=env.brain,
        bus=env.bus,
        dispatcher=env.dispatcher,
    )
    assert await runner._active_card() == 0 and db.sessions == 0
