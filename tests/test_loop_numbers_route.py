"""GET /dashboard/loop-numbers through the real app: off answers without a session, on answers the shape, a bad window
is a 400, and the route only reads."""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock

import httpx
import pytest

from nous.api.rest import create_app
from nous.config import Settings


def _settings(**over) -> Settings:
    return Settings(_env_file=None, agent_id=f"test-loop-route-{uuid.uuid4().hex[:8]}", **over)


def _app(db, settings: Settings):
    return create_app(MagicMock(), MagicMock(), MagicMock(), MagicMock(), db, settings)


async def _call(app, method: str, path: str) -> httpx.Response:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://nous") as client:
        return await client.request(method, path)


def test_the_setting_is_off_by_default() -> None:
    assert Settings(_env_file=None).loop_numbers_enabled is False


async def test_off_it_answers_without_opening_a_session(db, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("a session was opened")

    app = _app(db, _settings())
    monkeypatch.setattr(db, "session", boom)
    resp = await _call(app, "GET", "/dashboard/loop-numbers?days=30")
    assert resp.status_code == 200
    assert resp.json() == {"enabled": False}


@pytest.mark.parametrize("query", ["days=0", "days=91", "days=x", "days=-1"])
async def test_a_bad_window_is_a_400(db, query):
    for enabled in (False, True):
        resp = await _call(_app(db, _settings(loop_numbers_enabled=enabled)), "GET", f"/dashboard/loop-numbers?{query}")
        assert resp.status_code == 400
        assert "days must be a whole number from 1 to 90" in resp.json()["error"]


async def test_on_it_answers_the_numbers_for_the_window(db):
    resp = await _call(_app(db, _settings(loop_numbers_enabled=True)), "GET", "/dashboard/loop-numbers?days=30")
    assert resp.status_code == 200
    body = resp.json()
    assert body["enabled"] is True and body["window_days"] == 30
    assert set(body["current"]) == {"from", "to", "truncated", "tasks", "workflow_steps", "goal_sessions"}
    assert body["claims_recorded"] is True  # f026_persistence_enabled, on by default
    assert (await _call(_app(db, _settings(loop_numbers_enabled=True)), "GET", "/dashboard/loop-numbers")).json()[
        "window_days"
    ] == 7


async def test_the_route_only_reads(db):
    app = _app(db, _settings(loop_numbers_enabled=True))
    assert (await _call(app, "POST", "/dashboard/loop-numbers")).status_code == 405
    assert (await _call(app, "DELETE", "/dashboard/loop-numbers")).status_code == 405
