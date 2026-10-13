"""F099 Phase 3b: the one service read the active card's sweep needs."""

from __future__ import annotations

import pytest
from f099_cards_support import cards_env  # noqa: F401
from f099_support import env_factory, runner_env  # noqa: F401

from nous.a2ui.builders.heartbeat_findings import heartbeat_findings
from nous.a2ui.service import SurfaceService
from nous.config import Settings

pytestmark = pytest.mark.postgres_only


async def test_live_data_model_reads_the_live_card_of_this_agent_only(cards_env):  # noqa: F811
    env = await cards_env()  # 3a's fixture: a real SurfaceService on this agent, torn down with its rows
    service = env.service
    stranger = SurfaceService(
        env.db, Settings(_env_file=None, agent_id=f"{env.agent}-other", telegram_bot_token="", telegram_chat_id="")
    )  # reads only: it never pushes, so it leaves nothing to clean up
    assert await service.live_data_model("probe:one") is None
    surface_id = await service.push_built(heartbeat_findings({"findings": []}), dedup_key="probe:one", notify=False)
    model = await service.live_data_model("probe:one")
    assert model is not None and "findings" in model
    assert await stranger.live_data_model("probe:one") is None
    await service.close(surface_id)
    assert await service.live_data_model("probe:one") is None
