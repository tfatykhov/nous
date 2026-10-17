"""The owner-grade reviewer tag is reserved: the scorer counts a review that carries it as the owner's own grade, so
the REST review endpoint, which anything reaching the API can call, refuses it. Only the companion actions write it."""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from nous.brain.brain import Brain
from nous.cognitive.layer import CognitiveLayer
from nous.config import Settings
from nous.observability.task_score_gatherer import OWNER_GRADE_REVIEWER


class _Runner:
    _ledgers: dict = {}


@pytest_asyncio.fixture
async def app(db, heart):
    from nous.api.rest import create_app

    settings = Settings(_env_file=None, agent_id=f"test-reserved-reviewer-{uuid.uuid4().hex[:8]}")
    brain = Brain(database=db, settings=settings)
    cognitive = CognitiveLayer(brain, heart, settings, identity_prompt="You are Nous.")
    yield create_app(_Runner(), brain, heart, cognitive, db, settings)
    await brain.close()


async def _review(app, reviewer: str):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        return await c.post(f"/decisions/{uuid.uuid4()}/review", json={"outcome": "success", "reviewer": reviewer})


@pytest.mark.parametrize("reviewer", [OWNER_GRADE_REVIEWER, f" {OWNER_GRADE_REVIEWER.upper()} ", "A2ui"])
async def test_the_owner_grade_tag_is_refused(app, reviewer):
    resp = await _review(app, reviewer)
    assert resp.status_code == 400
    assert "reserved" in resp.json()["error"]


async def test_another_reviewer_gets_past_the_check(app):
    # No such decision: the request passes the reviewer check and fails on the lookup instead.
    resp = await _review(app, "external")
    assert resp.status_code == 404
