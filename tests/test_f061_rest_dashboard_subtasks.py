"""F061 PR-3: tests for the GET /dashboard/subtasks REST endpoint.

Validates the route handler end-to-end: input parsing (hours bounds, type),
forwarding to ``get_subtask_dashboard_data``, and JSON response shape.
The query function itself is tested in ``test_f061_dashboard_subtasks.py``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio

pytest.importorskip("httpx")
pytest.importorskip("starlette")

from httpx import ASGITransport, AsyncClient

from nous.brain import Brain
from nous.cognitive.layer import CognitiveLayer
from nous.heart import Heart


class _MockAgentRunner:
    def __init__(self):
        self._conversations = {}

    async def start(self):
        pass

    async def close(self):
        pass


@pytest_asyncio.fixture
async def heart(db, settings):
    h = Heart(database=db, settings=settings)
    yield h
    await h.close()


@pytest_asyncio.fixture
async def brain(db, settings):
    b = Brain(database=db, settings=settings)
    yield b
    await b.close()


@pytest_asyncio.fixture
async def cognitive(brain, heart, settings):
    return CognitiveLayer(brain, heart, settings, identity_prompt="You are Nous.")


@pytest.fixture
def app(brain, heart, cognitive, db, settings):
    from nous.api.rest import create_app
    return create_app(_MockAgentRunner(), brain, heart, cognitive, db, settings)


@pytest_asyncio.fixture
async def client(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_endpoint_returns_400_on_invalid_hours(client):
    resp = await client.get("/dashboard/subtasks?hours=not-a-number")
    assert resp.status_code == 400
    body = resp.json()
    assert "hours must be an integer" in body["error"]


@pytest.mark.asyncio
async def test_endpoint_clamps_hours_to_max_168(client):
    """hours > 168 are clamped to 168 (7 days)."""
    with patch(
        "nous.api.dashboard_queries.get_subtask_dashboard_data",
        new_callable=AsyncMock,
    ) as spy:
        spy.return_value = {"window_hours": 168, "totals": {}}
        resp = await client.get("/dashboard/subtasks?hours=999")
        assert resp.status_code == 200
        # The handler clamps before forwarding: spy receives hours=168, not 999.
        spy.assert_awaited_once()
        assert spy.await_args.kwargs["hours"] == 168


@pytest.mark.asyncio
async def test_endpoint_clamps_hours_to_min_1(client):
    with patch(
        "nous.api.dashboard_queries.get_subtask_dashboard_data",
        new_callable=AsyncMock,
    ) as spy:
        spy.return_value = {"window_hours": 1, "totals": {}}
        resp = await client.get("/dashboard/subtasks?hours=0")
        assert resp.status_code == 200
        spy.assert_awaited_once()
        assert spy.await_args.kwargs["hours"] == 1


@pytest.mark.asyncio
async def test_endpoint_default_hours_is_24(client):
    with patch(
        "nous.api.dashboard_queries.get_subtask_dashboard_data",
        new_callable=AsyncMock,
    ) as spy:
        spy.return_value = {"window_hours": 24, "totals": {}}
        resp = await client.get("/dashboard/subtasks")
        assert resp.status_code == 200
        spy.assert_awaited_once()
        assert spy.await_args.kwargs["hours"] == 24


# ---------------------------------------------------------------------------
# End-to-end handler invocation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_endpoint_forwards_hours_to_query(client):
    """End-to-end: route → handler closure → patched query function with hours=6."""
    with patch(
        "nous.api.dashboard_queries.get_subtask_dashboard_data",
        new_callable=AsyncMock,
    ) as spy:
        spy.return_value = {
            "window_hours": 6,
            "totals": {"total_terminal": 0, "by_outcome": {}, "empty_rate": 0.0, "retry_rate": 0.0},
            "tokens_by_outcome": {},
            "top_failing_tasks": [],
            "dag_correlation": {},
            "recent_outcomes": [],
            "daily_trend": [],
        }
        resp = await client.get("/dashboard/subtasks?hours=6")
        assert resp.status_code == 200
        # Verify the handler actually invoked the query (not just the input parsing).
        spy.assert_awaited_once()
        # Positional: (session, agent_id). Keyword: hours.
        assert spy.await_args.kwargs["hours"] == 6
        # Response body matches the canned dict.
        body = resp.json()
        assert body["window_hours"] == 6


@pytest.mark.asyncio
async def test_endpoint_returns_500_on_query_error(client):
    with patch(
        "nous.api.dashboard_queries.get_subtask_dashboard_data",
        new_callable=AsyncMock,
    ) as spy:
        spy.side_effect = RuntimeError("DB unavailable")
        resp = await client.get("/dashboard/subtasks?hours=24")
        assert resp.status_code == 500
        body = resp.json()
        assert "DB unavailable" in body["error"]


@pytest.mark.asyncio
async def test_endpoint_response_shape_includes_all_cards(client):
    with patch(
        "nous.api.dashboard_queries.get_subtask_dashboard_data",
        new_callable=AsyncMock,
    ) as spy:
        spy.return_value = {
            "window_hours": 24,
            "totals": {"total_terminal": 5, "by_outcome": {"completed": 5}, "empty_rate": 0.0, "retry_rate": 0.0},
            "tokens_by_outcome": {"completed": {"mean_total_tokens": 800, "mean_tool_calls": 4.0, "n": 5}},
            "top_failing_tasks": [],
            "dag_correlation": {},
            "recent_outcomes": [],
            "daily_trend": [{"date": "2026-05-07", "by_outcome": {"completed": 5}}],
        }
        resp = await client.get("/dashboard/subtasks")
        assert resp.status_code == 200
        body = resp.json()
        for key in (
            "window_hours", "totals", "tokens_by_outcome",
            "top_failing_tasks", "dag_correlation",
            "recent_outcomes", "daily_trend",
        ):
            assert key in body, f"missing card: {key}"
