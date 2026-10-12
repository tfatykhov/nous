"""F099 Phase 3c-5: GET /dashboard/intentions through the real create_app."""

from __future__ import annotations

from unittest.mock import MagicMock

import httpx
import pytest
from f099_dashboard_support import put_arrivals, put_intention
from f099_support import CONT, ON, env_factory  # noqa: F401

from nous.api import intention_dashboard
from nous.api.rest import create_app

pytestmark = pytest.mark.postgres_only


def _app(env):
    return create_app(MagicMock(), MagicMock(), MagicMock(), MagicMock(), env.db, env.settings)


async def _get(app, path):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://nous") as client:
        return await client.get(path)


async def test_the_route_answers_with_the_contract_shape(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    root = await put_intention(env)
    await put_arrivals(env, root, [{}])
    resp = await _get(_app(env), "/dashboard/intentions")
    body = resp.json()
    assert resp.status_code == 200
    assert set(body) == {"continuation", "window", "roots", "metrics"} and body["window"] == "7d"
    assert set(body["metrics"]) == set(intention_dashboard.METRIC_KEYS)
    assert [r["id"] for r in body["roots"]] == [str(root.id)]
    assert (await _get(_app(env), "/dashboard/intentions?window=30d&state=open&limit=5")).json()["window"] == "30d"


async def test_flag_off_reads_no_row_through_create_app(env_factory, monkeypatch):  # noqa: F811
    """PIN (lands dark): continuation off, intentions on, as prod runs. Mutation: move the flag check below the
    session in the handler and this raises."""
    env = await env_factory(**ON)
    await put_intention(env)

    def boom(*a, **k):
        raise AssertionError("a session was opened")

    monkeypatch.setattr(env.db, "session", boom)
    resp = await _get(_app(env), "/dashboard/intentions")
    assert (resp.status_code, resp.json()) == (200, {"continuation": False, "roots": [], "metrics": None})


@pytest.mark.parametrize(
    "query",
    ["window=90d", "state=recent", "limit=0", "limit=201", "limit=abc", "limit=%C2%B2", "limit=-1", f"limit={1 << 70}"],
)
async def test_bad_input_is_a_400_with_continuation_on_or_off(env_factory, query):  # noqa: F811
    for flags in (CONT, ON):
        env = await env_factory(**flags)
        resp = await _get(_app(env), f"/dashboard/intentions?{query}")
        assert resp.status_code == 400 and "error" in resp.json()


async def test_a_failure_is_a_500_with_the_error_text(env_factory, monkeypatch):  # noqa: F811
    env = await env_factory(**CONT)

    async def broken(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(intention_dashboard, "get_intentions_data", broken)
    resp = await _get(_app(env), "/dashboard/intentions")
    assert resp.status_code == 500 and resp.json() == {"error": "db down"}
