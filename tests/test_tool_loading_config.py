"""F120 tool loading, watch only: the one setting, and the owner's choice in ``RuntimeConfig``.

``NOUS_TOOL_LOADING_ENABLED`` is off by default, passed by the compose file and documented. ``RuntimeConfig`` holds
each agent's choice (Off until one is stored or set) and reads only this agent's stored row, with a query of its own:
a malformed row is Off with one warning and leaves the other runtime keys loaded, and a bad other key never hides it.
The database tests run in the ``session`` fixture's transaction, which is rolled back.
"""

from __future__ import annotations

import json
import logging
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text

from nous.api.tool_loading import OFF, ToolLoadingConfig, config_key
from nous.config import Settings
from nous.runtime_config import RuntimeConfig

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _fresh_runtime_config():
    RuntimeConfig.reset()
    yield
    RuntimeConfig.reset()


def _agent() -> str:
    return f"test-tool-loading-{uuid.uuid4().hex[:12]}"


# ---- the setting ---------------------------------------------------------------------------------------------------


def test_the_setting_is_off_by_default_and_read_from_the_environment(monkeypatch):
    assert Settings(_env_file=None).tool_loading_enabled is False
    monkeypatch.setenv("NOUS_TOOL_LOADING_ENABLED", "true")
    assert Settings(_env_file=None).tool_loading_enabled is True


def test_compose_passes_the_setting_with_its_default():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "- NOUS_TOOL_LOADING_ENABLED=${NOUS_TOOL_LOADING_ENABLED:-false}" in compose


def test_the_setting_has_a_row_in_the_environment_reference():
    reference = (ROOT / "docs" / "reference" / "environment-variables.md").read_text(encoding="utf-8")
    assert "| `NOUS_TOOL_LOADING_ENABLED` | `false` |" in reference


# ---- RuntimeConfig, in memory ---------------------------------------------------------------------------------------


def test_each_agent_is_off_until_its_choice_is_set():
    runtime = RuntimeConfig.get()
    assert runtime.get_tool_loading("a") == OFF
    watch = ToolLoadingConfig(mode="watch", kinds=frozenset({"chat"}))
    runtime.set_tool_loading("a", watch)
    assert runtime.get_tool_loading("a") == watch
    assert runtime.get_tool_loading("b") == OFF  # another agent's choice is its own


# ---- RuntimeConfig, from the database ----------------------------------------------------------------------------


async def _put(session, key: str, value) -> None:
    await session.execute(
        text(
            "INSERT INTO nous_system.config (key, value) VALUES (:key, CAST(:value AS jsonb)) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"
        ),
        {"key": key, "value": json.dumps(value)},
    )


@pytest.mark.postgres_only
async def test_a_stored_choice_is_loaded_at_start(session):
    agent = _agent()
    stored = ToolLoadingConfig(
        "watch", frozenset({"chat", "subtask"}), ("db_query",), "2026-11-24T10:00:00+00:00", "proxy"
    )
    await _put(session, config_key(agent), stored.to_json())
    runtime = RuntimeConfig.get()
    await runtime.load_from_db(session, agent)
    assert runtime.get_tool_loading(agent) == stored
    assert runtime.tool_loading_problem(agent) is None


@pytest.mark.postgres_only
async def test_without_an_agent_id_no_tool_loading_row_is_read(session):
    agent = _agent()
    await _put(session, config_key(agent), ToolLoadingConfig(mode="watch").to_json())
    runtime = RuntimeConfig.get()
    await runtime.load_from_db(session)
    assert runtime.get_tool_loading(agent) == OFF


@pytest.mark.postgres_only
async def test_another_agents_row_is_never_read(session):
    mine, other = _agent(), _agent()
    await _put(session, config_key(other), ToolLoadingConfig(mode="watch", kinds=frozenset({"chat"})).to_json())
    runtime = RuntimeConfig.get()
    await runtime.load_from_db(session, mine)
    assert runtime.get_tool_loading(mine) == OFF
    assert runtime._tool_loading == {}  # the other agent's row was not read into memory either


@pytest.mark.postgres_only
async def test_a_malformed_row_is_off_with_one_warning_and_the_other_keys_still_load(session, caplog):
    agent = _agent()
    await _put(session, "vector_weight", 0.35)
    await _put(session, config_key(agent), {"version": 1, "mode": "on"})
    runtime = RuntimeConfig.get()
    with caplog.at_level(logging.WARNING, logger="nous.runtime_config"):
        await runtime.load_from_db(session, agent)
    assert runtime.get_vector_weight(Settings(_env_file=None)) == pytest.approx(0.35)
    assert runtime.get_tool_loading(agent) == OFF
    assert runtime.tool_loading_problem(agent) == "the stored mode is not off or watch"
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING and "F120" in r.getMessage()]
    assert warnings == ["F120: the tool-loading setting is off: the stored mode is not off or watch"]


@pytest.mark.postgres_only
async def test_a_bad_other_key_never_hides_the_tool_loading_row(session):
    agent = _agent()
    await _put(session, "rrf_k", "not a number")  # int() raises in the other keys' loop
    stored = ToolLoadingConfig(mode="watch", kinds=frozenset({"heartbeat"}))
    await _put(session, config_key(agent), stored.to_json())
    runtime = RuntimeConfig.get()
    await runtime.load_from_db(session, agent)
    assert runtime.get_tool_loading(agent) == stored


@pytest.mark.postgres_only
async def test_a_set_choice_clears_the_problem(session):
    agent = _agent()
    await _put(session, config_key(agent), "garbage")
    runtime = RuntimeConfig.get()
    await runtime.load_from_db(session, agent)
    assert runtime.tool_loading_problem(agent) is not None
    runtime.set_tool_loading(agent, ToolLoadingConfig(mode="watch"))
    assert runtime.tool_loading_problem(agent) is None
