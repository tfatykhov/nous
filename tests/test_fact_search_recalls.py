"""A browse is not a recall: the two GET handlers that search facts ask the heart not to count the facts they return
as recalled (``track_access=False``), so a dashboard search does not move ``recall_count``. No database: the heart is
a spy."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx

from nous.api.rest import create_app
from nous.config import Settings


def _app(tmp_path, heart: SimpleNamespace):
    settings = Settings(_env_file=None, workspace_dir=str(tmp_path))
    return create_app(SimpleNamespace(), SimpleNamespace(), heart, SimpleNamespace(), SimpleNamespace(), settings)


async def _get(app, path: str) -> httpx.Response:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://nous") as client:
        return await client.get(path)


async def test_a_fact_search_and_the_rubric_proposals_list_do_not_count_as_recalls(tmp_path) -> None:
    heart = SimpleNamespace(search_facts=AsyncMock(return_value=[]))
    app = _app(tmp_path, heart)

    searched = await _get(app, "/facts?q=snow&limit=5")
    listed = await _get(app, "/rubric/proposals")

    assert (searched.status_code, searched.json()) == (200, {"facts": [], "total": 0})
    assert (listed.status_code, listed.json()) == (200, [])
    assert heart.search_facts.await_count == 2
    for call in heart.search_facts.await_args_list:
        assert call.kwargs.get("track_access") is False, call
