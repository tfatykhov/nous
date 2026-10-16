"""GET /dashboard/autonomy (the autonomy scoreboard): status codes, shape and agent scoping through the real app."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from nous.brain.brain import Brain
from nous.cognitive.layer import CognitiveLayer
from nous.config import Settings
from nous.storage.models import A2uiAction


class _Runner:
    _ledgers: dict = {}


@pytest_asyncio.fixture
async def agent() -> str:
    return f"test-autonomy-rest-{uuid.uuid4().hex[:8]}"


@pytest_asyncio.fixture
async def app(db, heart, agent):
    from nous.api.rest import create_app

    settings = Settings(_env_file=None, agent_id=agent)
    brain = Brain(database=db, settings=settings)
    cognitive = CognitiveLayer(brain, heart, settings, identity_prompt="You are Nous.")
    yield create_app(_Runner(), brain, heart, cognitive, db, settings)
    await brain.close()


async def _get(app, path):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        return await c.get(path)


@pytest.mark.parametrize(
    "path",
    [
        "/dashboard/autonomy?days=0",
        "/dashboard/autonomy?days=91",
        "/dashboard/autonomy?days=abc",
        "/dashboard/autonomy?days=-1",
    ],
)
async def test_bad_input_is_a_400_not_a_500(app, path):
    resp = await _get(app, path)
    assert resp.status_code == 400
    assert "days must be a whole number from 1 to 90" in resp.json()["error"]


async def test_the_route_answers_with_the_scoreboard_shape(app):
    resp = await _get(app, "/dashboard/autonomy")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {
        "window_days",
        "taps",
        "triage_taps",
        "approval_cards_asked",
        "decision_grades",
        "task_scores",
        "inventory",
        "events_persisted",
        "warn_classes",
    }
    assert body["window_days"] == 7
    assert (await _get(app, "/dashboard/autonomy?days=30")).json()["window_days"] == 30


async def test_the_route_reads_this_agents_rows_only(app, db, agent):
    now = datetime.now(UTC)
    async with db.session() as s:
        s.add_all(
            [
                A2uiAction(
                    agent_id=agent,
                    surface_id="s",
                    action_name="heartbeat.resolve",
                    actor="unattributed",
                    created_at=now - timedelta(hours=1),
                ),
                A2uiAction(
                    agent_id="someone-else",
                    surface_id="s",
                    action_name="heartbeat.resolve",
                    actor="unattributed",
                    created_at=now - timedelta(hours=1),
                ),
            ]
        )
        await s.commit()
    body = (await _get(app, "/dashboard/autonomy")).json()
    assert body["taps"]["total"] == 1 and body["triage_taps"]["heartbeat"] == 1


async def test_the_route_is_read_only(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        assert (await c.post("/dashboard/autonomy")).status_code == 405
        assert (await c.delete("/dashboard/autonomy")).status_code == 405
