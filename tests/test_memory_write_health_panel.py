"""Memory write health, PR-2: GET /dashboard/memory-write-health, the acknowledge action (REST and companion), the
companion card and the check keeping it in step. Each test that writes rows uses an agent of its own."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import delete, select, text

from nous.a2ui.actions import ActionContext, ActionRouter
from nous.a2ui.builders import TEMPLATES
from nous.a2ui.builders import memory_write_health as card
from nous.api import memory_write_health_dashboard as dash
from nous.api.rest import create_app
from nous.config import Settings
from nous.heartbeat.memory_write_health_check import MemoryWriteHealthCheck
from nous.observability import memory_write_health as mwh
from nous.storage.models import A2uiAction, A2uiSurface, Event, Fact

TODAY = date(2026, 11, 5)


def _agent() -> str:
    return f"test-mwh-{uuid4().hex[:12]}"


def _settings(agent: str, **over) -> Settings:
    return Settings(agent_id=agent, telegram_bot_token=None, telegram_chat_id=None, **over)


def _app(db, settings):
    return create_app(MagicMock(), MagicMock(), MagicMock(), MagicMock(), db, settings)


async def _call(app, method: str, path: str, **kw):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://nous") as client:
        return await client.request(method, path, **kw)


async def _gone_fact(db, agent: str) -> None:
    """A fact with its ledger event, deleted after the first run: H1 trips on the next one."""
    async with db.session() as s:
        row = Fact(id=uuid4(), agent_id=agent, content=f"fact {uuid4().hex}", active=True)
        s.add(row)
        s.add(Event(agent_id=agent, event_type="fact_learned", data={"fact_id": str(row.id)}))
        await s.commit()
    await mwh.run_once(db, agent)
    async with db.session() as s:
        await s.execute(text("DELETE FROM heart.facts WHERE id = :id"), {"id": row.id})
        await s.commit()


def _keys(node) -> set[str]:
    if isinstance(node, dict):
        return set(node) | {k for v in node.values() for k in _keys(v)}
    if isinstance(node, list):
        return {k for v in node for k in _keys(v)}
    return set()


# ---------------------------------------------------------------------------------------------------------------------
# GET /dashboard/memory-write-health
# ---------------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("raw", ["0", "91", "abc", "-1", "7.5"])
def test_the_window_accepts_1_to_90_only(raw):
    with pytest.raises(ValueError):
        dash.parse_window(raw)


def test_the_window_defaults_to_30():
    assert dash.parse_window(None) == dash.parse_window("") == 30
    assert dash.parse_window("90") == 90


@pytest.mark.postgres_only
@pytest.mark.parametrize("query", ["days=0", "days=91", "days=x"])
async def test_a_bad_window_is_a_400(db, query):
    resp = await _call(_app(db, _settings(_agent())), "GET", f"/dashboard/memory-write-health?{query}")
    assert resp.status_code == 400 and "error" in resp.json()


@pytest.mark.postgres_only
async def test_with_the_check_off_it_answers_without_a_session(db, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("a session was opened")

    app = _app(db, _settings(_agent(), memory_write_health_enabled=False))
    monkeypatch.setattr(db, "session", boom)
    resp = await _call(app, "GET", "/dashboard/memory-write-health")
    assert resp.status_code == 200
    assert resp.json() == dash.disabled_payload(30)


@pytest.mark.postgres_only
async def test_the_page_returns_the_rollup_rules_and_no_memory_text(db):
    agent = _agent()
    await _gone_fact(db, agent)
    await mwh.run_once(db, agent)

    resp = await _call(_app(db, _settings(agent)), "GET", "/dashboard/memory-write-health?days=7")

    body = resp.json()
    assert resp.status_code == 200 and body["enabled"] is True and body["window_days"] == 7
    assert len(body["days"]) == 7 and body["days"][0]["day"] < body["days"][-1]["day"]
    assert body["live"]["vanished_fact_ids"] == 1 and body["live"]["counters"]["vanished_fact_ids"] == 1
    states = {c["id"]: c["state"] for c in body["checks"]}
    assert set(states) == set(mwh.RULES) and states["H1"] == "tripped"
    assert [r["id"] for r in body["rules"]] == list(mwh.RULES)
    assert [a["actor"] for a in body["acknowledgements"]] == ["system"]
    assert not _keys(body) & {"content", "transcript", "subject", "text", "detail_text"}


@pytest.mark.postgres_only
async def test_before_the_first_run_the_page_is_empty_not_an_error(db):
    resp = await _call(_app(db, _settings(_agent())), "GET", "/dashboard/memory-write-health")
    body = resp.json()
    assert resp.status_code == 200 and body["days"] == [] and body["live"] is None and body["checks"] == []


# ---------------------------------------------------------------------------------------------------------------------
# POST /dashboard/memory-write-health/acknowledge
# ---------------------------------------------------------------------------------------------------------------------


@pytest.mark.postgres_only
async def test_an_acknowledgement_clears_h1_on_the_page_at_once(db):
    agent = _agent()
    await _gone_fact(db, agent)
    await mwh.run_once(db, agent)
    app = _app(db, _settings(agent))

    resp = await _call(
        app, "POST", "/dashboard/memory-write-health/acknowledge", json={"rule": "H1", "note": "cleanup"}
    )

    assert resp.status_code == 200
    assert resp.json()["acknowledged"]["actor"] == "dashboard:unattributed"
    page = (await _call(app, "GET", "/dashboard/memory-write-health")).json()
    assert {c["id"]: c["state"] for c in page["checks"]}["H1"] == "ok"
    assert page["acknowledgements"][0] == {
        "rule": "H1",
        "day": datetime.now(UTC).date().isoformat(),
        "note": "cleanup",
        "actor": "dashboard:unattributed",
        "at": page["acknowledgements"][0]["at"],
    }


@pytest.mark.postgres_only
async def test_the_actor_is_the_forwarded_identity_only_when_trusted(db):
    agent = _agent()
    await mwh.run_once(db, agent)
    headers = {"x-forwarded-email": "owner@example.com"}
    for trusted, actor in ((False, "dashboard:unattributed"), (True, "dashboard:owner@example.com")):
        app = _app(db, _settings(agent, a2ui_trust_forwarded_identity=trusted))
        resp = await _call(
            app, "POST", "/dashboard/memory-write-health/acknowledge", json={"rule": "F1"}, headers=headers
        )
        assert resp.json()["acknowledged"]["actor"] == actor


@pytest.mark.postgres_only
@pytest.mark.parametrize(
    "body",
    [
        {"rule": "Z9"},
        {"rule": "F1", "day": "05/11/2026"},
        {"rule": "F1", "day": 20261105},
        {"rule": "F1", "note": 3},
        {"rule": "F1", "day": "2999-01-01"},
        {"rule": "H1"},  # nothing observed yet
        ["H1"],
    ],
)
async def test_a_bad_acknowledgement_is_a_400_and_writes_nothing(db, body):
    agent = _agent()
    resp = await _call(_app(db, _settings(agent)), "POST", "/dashboard/memory-write-health/acknowledge", json=body)
    assert resp.status_code == 400 and "error" in resp.json()
    async with db.session() as s:
        assert await mwh.load_acks(s, agent) == []


@pytest.mark.postgres_only
async def test_with_the_check_off_an_acknowledgement_is_a_409(db):
    app = _app(db, _settings(_agent(), memory_write_health_enabled=False))
    resp = await _call(app, "POST", "/dashboard/memory-write-health/acknowledge", json={"rule": "F1"})
    assert resp.status_code == 409


# ---------------------------------------------------------------------------------------------------------------------
# The companion card
# ---------------------------------------------------------------------------------------------------------------------


def _states(*tripped: tuple[str, str]) -> list[mwh.RuleState]:
    out = [mwh.RuleState(rule, "tripped", summary=summary) for rule, summary in tripped]
    out.append(mwh.RuleState("A1", "ok"))
    return out


def test_the_card_has_one_acknowledge_per_tripped_rule_and_never_pings():
    built = card.memory_write_health_card(
        _states(("H1", "rows vanished"), ("F1", "flood from user_direct"), ("F1", "flood from other")), TODAY
    )
    assert (built.kind, built.origin, built.priority) == ("memory_write_health", "heartbeat", 0)
    assert built.allowed_actions == [card.ACK_ACTION]
    assert built.data_model["rules"] == {"H1": "tripped", "F1": "tripped"}
    assert built.data_model["day"] == "2026-11-05"
    buttons = [c for c in built.components if c["component"] == "Button"]
    assert [b["action"]["event"]["context"] for b in buttons] == [{"rule": "H1"}, {"rule": "F1"}]
    texts = " ".join(str(c.get("text")) for c in built.components if c["component"] == "Text")
    assert "flood from user_direct" in texts and "flood from other" in texts


def test_there_is_no_card_without_a_tripped_rule_and_no_model_can_build_one():
    with pytest.raises(ValueError):
        card.memory_write_health_card(_states(), TODAY)
    assert card.KIND not in TEMPLATES


async def _tap(router, surface, rule):
    ctx = ActionContext(
        surface=surface, name=card.ACK_ACTION, context={"rule": rule}, data_model=None, services=router, actor="owner"
    )
    return await router._handlers[card.ACK_ACTION].fn(ctx)


def _surface(rules: dict, day: str | None = None):
    return SimpleNamespace(
        surface_id="s-1", data_model={"day": day or datetime.now(UTC).date().isoformat(), "rules": rules, "status": {}}
    )


def test_the_acknowledge_handler_is_registered_as_mutating(db):
    router = ActionRouter(db, _settings(_agent()), SimpleNamespace())
    assert router._handlers[card.ACK_ACTION].mutating is True


@pytest.mark.postgres_only
async def test_a_tap_acknowledges_the_cards_rule_on_the_cards_day(db):
    agent = _agent()
    await mwh.run_once(db, agent)
    router = ActionRouter(db, _settings(agent), SimpleNamespace())
    yesterday = (datetime.now(UTC).date() - timedelta(days=1)).isoformat()

    result = await _tap(router, _surface({"F1": "tripped", "S1": "tripped"}, yesterday), "F1")

    assert result.ok and result.resolve_surface is False
    assert ("/rules/F1", "acknowledged") in result.data_patches
    async with db.session() as s:
        ack = (await mwh.load_acks(s, agent))[-1]
    assert (ack["rule"], ack["day"].isoformat(), ack["actor"]) == ("F1", yesterday, "companion:owner")


@pytest.mark.postgres_only
async def test_the_last_tap_closes_the_card(db):
    agent = _agent()
    await mwh.run_once(db, agent)
    router = ActionRouter(db, _settings(agent), SimpleNamespace())
    result = await _tap(router, _surface({"F1": "acknowledged", "S1": "tripped"}), "S1")
    assert result.ok and result.resolve_surface is True


@pytest.mark.postgres_only
@pytest.mark.parametrize(("rules", "rule"), [({"F1": "tripped"}, "S1"), ({"F1": "acknowledged"}, "F1"), ({}, None)])
async def test_a_tap_the_card_did_not_offer_writes_nothing(db, rules, rule):
    agent = _agent()
    router = ActionRouter(db, _settings(agent), SimpleNamespace())
    result = await _tap(router, _surface(rules), rule)
    assert result.ok is False
    async with db.session() as s:
        assert await mwh.load_acks(s, agent) == []


@pytest.mark.postgres_only
async def test_the_check_keeps_one_card_through_the_router_and_never_pings(db, monkeypatch):
    """End to end: the check pushes the card through the real surface service (no Telegram, no phone push), a tap
    through the router acknowledges H1, and the next run closes the card."""
    from nous.a2ui.service import SurfaceService

    agent = _agent()
    settings = _settings(agent).model_copy(update={"telegram_bot_token": "t", "telegram_chat_id": "c"})
    service = SurfaceService(db, settings)
    pinged = AsyncMock()
    monkeypatch.setattr(service, "_notify_telegram", pinged)
    router = ActionRouter(db, settings, service)
    check = MemoryWriteHealthCheck(db, agent)
    check.set_surface_service(service)
    await _gone_fact(db, agent)
    try:
        await check.run()
        await check.run()  # unchanged: no second push
        async with db.session() as s:
            (surface,) = (await s.execute(select(A2uiSurface).where(A2uiSurface.agent_id == agent))).scalars().all()
        assert (surface.status, surface.priority, surface.dedup_key) == ("live", 0, card.DEDUP_KEY)
        assert surface.push_notified_at is None and surface.data_model["rules"] == {"H1": "tripped"}
        pinged.assert_not_awaited()

        body = {
            "version": "v1.0",
            "action": {
                "name": card.ACK_ACTION,
                "surfaceId": surface.surface_id,
                "context": {"rule": "H1"},
                "metadata": {"extensions": {"com_nous_nonce": surface.nonce}},
            },
        }
        status, payload = await router.handle(body, content_type="application/json")
        assert (status, payload["resolved"]) == (200, True)

        result = await check.run()
        assert result.findings == []
        async with db.session() as s:
            statuses = (
                (await s.execute(select(A2uiSurface.status).where(A2uiSurface.agent_id == agent))).scalars().all()
            )
        assert statuses == ["resolved"]
        pinged.assert_not_awaited()
    finally:
        async with db.session() as s:
            await s.execute(delete(A2uiAction).where(A2uiAction.agent_id == agent))
            await s.execute(delete(A2uiSurface).where(A2uiSurface.agent_id == agent))
            await s.commit()


async def test_a_failed_card_push_does_not_fail_the_check(monkeypatch):
    async def fake_run_once(database, agent_id, *, now=None):
        return _states(("H1", "rows vanished"))

    monkeypatch.setattr(mwh, "run_once", fake_run_once)
    surfaces = SimpleNamespace(push_built=AsyncMock(side_effect=RuntimeError("down")), close_by_dedup_key=AsyncMock())
    check = MemoryWriteHealthCheck(object(), "test-mwh-push-fail")
    check.set_surface_service(surfaces)

    result = await check.run()

    assert [f.raw_data["rule"] for f in result.findings] == ["H1"]
    await check.run()
    assert surfaces.push_built.await_count == 2  # not marked as shown, so retried


async def test_without_the_companion_the_check_keeps_no_card(monkeypatch):
    async def fake_run_once(database, agent_id, *, now=None):
        return _states(("H1", "rows vanished"))

    monkeypatch.setattr(mwh, "run_once", fake_run_once)
    result = await MemoryWriteHealthCheck(object(), "test-mwh-no-companion").run()
    assert len(result.findings) == 1
