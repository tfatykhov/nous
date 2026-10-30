"""Stored tool lists are read under the tools' current names (no data migration rewrites them).

A dynamic check stored with ``tools: ["recall_recent"]`` keeps its recall: the old name becomes recall_deep in
the check's tool filter, its declared tools and its signature, on every way a list enters a check (row load,
create, update, journal restore).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from test_heartbeat_dynamic import _make_dynamic_check, _mock_db, _mock_db_row

from nous.heartbeat.dynamic import ALLOWED_TOOLS, DynamicCheckLoader
from nous.heartbeat.registry import CheckRegistry


def test_the_check_tool_set_holds_current_names_only():
    assert "recall_recent" not in ALLOWED_TOOLS and "recall_deep" in ALLOWED_TOOLS


def test_a_check_built_with_an_old_name_uses_the_new_one():
    check = _make_dynamic_check(tools=["recall_recent", "bash", "recall_deep"])
    assert check._tools == ["recall_deep", "bash"]
    same = _make_dynamic_check(tools=["recall_deep", "bash"])
    assert check.signature() == same.signature()


@pytest.mark.asyncio
async def test_the_run_filters_and_declares_the_new_name():
    runner = AsyncMock()
    runner.run_turn = AsyncMock(return_value=('{"has_findings": false, "findings": []}', MagicMock(), {}))
    runner.end_conversation = AsyncMock()
    check = _make_dynamic_check(tools=["recall_recent", "web_search"], runner=runner)
    await check.run()
    kwargs = runner.run_turn.call_args.kwargs
    assert kwargs["tool_filter"] == ["recall_deep", "web_search"]
    assert kwargs["context"].declared_tools == ("recall_deep", "web_search")


def _loader():
    db, session = _mock_db()
    registry = CheckRegistry()
    return DynamicCheckLoader(db=db, registry=registry, runner=AsyncMock(), agent_id="test-agent"), registry, session


@pytest.mark.asyncio
async def test_a_stored_row_loads_under_the_new_names():
    loader, registry, _session = _loader()
    row = _mock_db_row(name="news", tools=["recall_recent", "web_search"], on_complete_tools=["recall_recent"])
    loader._fetch_enabled = AsyncMock(return_value=[row])
    await loader.sync()
    check = registry.get_check("news")
    assert check._tools == ["recall_deep", "web_search"] and check.on_complete_tools == ["recall_deep"]


@pytest.mark.asyncio
async def test_create_stores_the_new_names():
    loader, registry, session = _loader()
    session.add, session.commit, session.refresh = MagicMock(), AsyncMock(), AsyncMock()
    with patch("nous.storage.models.DynamicCheckModel") as model:
        model.return_value = MagicMock(id="new-id")
        result = await loader.create_check(
            name="c",
            description="d",
            prompt="p",
            tools=["recall_recent", "bash"],
            on_complete_tools=["recall_recent"],
        )
    stored = model.call_args.kwargs
    assert stored["tools"] == ["recall_deep", "bash"] and stored["on_complete_tools"] == ["recall_deep"]
    assert result["tools"] == ["recall_deep", "bash"]


@pytest.mark.asyncio
async def test_an_update_against_a_stored_old_name_passes_the_subset_check():
    loader, _registry, session = _loader()
    loader.sync = AsyncMock()
    stored = MagicMock()
    stored.tools = ["recall_recent", "web_search"]  # written before the rename
    stored.on_complete_tools = []
    stored.cron_expr, stored.interval_seconds = None, 3600
    found = MagicMock()
    found.scalar_one_or_none.return_value = stored
    session.execute, session.commit = AsyncMock(return_value=found), AsyncMock()
    result = await loader.manage_check(action="update", name="c", updates={"on_complete_tools": ["recall_recent"]})
    assert result["status"] == "updated" and stored.on_complete_tools == ["recall_deep"]


# ---- procedures and skills -------------------------------------------------------------------------------------


def _procedure(core_tools: list[str], notes: list[str] | None = None):
    from datetime import UTC, datetime
    from uuid import uuid4

    from nous.heart.schemas import ProcedureDetail

    return ProcedureDetail(
        id=uuid4(),
        agent_id="a",
        name="Catch up",
        domain="memory",
        description="Catch up on the day",
        goals=[],
        core_patterns=[],
        core_tools=core_tools,
        core_concepts=[],
        implementation_notes=notes or [],
        activation_count=0,
        success_count=0,
        failure_count=0,
        neutral_count=0,
        last_activated=None,
        effectiveness=None,
        tags=[],
        active=True,
        created_at=datetime.now(UTC),
    )


@pytest.mark.asyncio
async def test_get_procedure_renders_core_tools_under_their_current_names():
    from nous.api.tools import create_nous_tools

    heart = MagicMock()
    heart.get_procedure_by_name = AsyncMock(return_value=_procedure(["recall_recent", "bash", "recall_hubs"]))
    result = await create_nous_tools(MagicMock(), heart)["get_procedure"](procedure_id="Catch up")
    assert "Tools: recall_deep, bash\n" in result["content"][0]["text"] + "\n"


def test_a_skill_file_naming_an_old_tool_is_parsed_with_the_new_name():
    from nous.skills.parser import SkillParser

    manifest = SkillParser().parse(
        "---\nname: catch-up\ndescription: Catch up\ntools:\n  - recall_recent\n  - bash\n---\nBody\n"
    )
    assert manifest.tools == ["recall_deep", "bash"]
    assert SkillParser().to_procedure_input(manifest).core_tools == ["recall_deep", "bash"]


def test_a_learned_procedure_stores_the_new_tool_names():
    from uuid import uuid4

    from test_procedure_learner import _build_learner, _llm_procedure_data

    learner, *_ = _build_learner()
    ids = {str(uuid4()): "completed", str(uuid4()): "completed"}
    data = {**_llm_procedure_data(cited_ids=list(ids)), "core_tools": ["recall_recent", "resolve_decision", "bash"]}
    proc = learner._gated_procedure_input(data, ids, "auto:x", min_sources=2)
    assert proc.core_tools == ["recall_deep", "resolve_decisions", "bash"]
