"""F115: the four settings, their compose lines and doc rows, how main.py builds the store, and which code writes
the autonomy tables (no database)."""

from __future__ import annotations

import inspect
import logging
import re
from pathlib import Path

import pytest

from nous import main
from nous.api.rest import create_app
from nous.config import Settings

ROOT = Path(__file__).resolve().parents[1]
NEW = {
    "NOUS_AUTONOMY_SETTINGS_ENABLED": ("autonomy_settings_enabled", False, "false"),
    "NOUS_TOOL_GUARD_ENABLED": ("tool_guard_enabled", False, "false"),
    "NOUS_AUTONOMY_CEILINGS": ("autonomy_ceilings", "", ""),
    "NOUS_AUTONOMY_LOCKED_TOOLS": ("autonomy_locked_tools", "", ""),
}


def test_the_defaults_are_off_and_empty():
    s = Settings(_env_file=None)
    for field, default, _compose in NEW.values():
        assert getattr(s, field) == default, field


def test_the_tool_guard_needs_the_autonomy_settings(caplog):
    with caplog.at_level(logging.WARNING, logger="nous.config"):
        s = Settings(_env_file=None, tool_guard_enabled=True)
    assert s.tool_guard_enabled is False
    assert "NOUS_TOOL_GUARD_ENABLED=true needs NOUS_AUTONOMY_SETTINGS_ENABLED=true" in caplog.text
    s = Settings(_env_file=None, tool_guard_enabled=True, autonomy_settings_enabled=True)
    assert s.tool_guard_enabled is True


@pytest.mark.parametrize("name", list(NEW))
def test_the_environment_name_is_the_contract_name(monkeypatch, name):
    field, _default, _compose = NEW[name]
    value = "true" if isinstance(NEW[name][1], bool) else "max_turns=50"
    monkeypatch.setenv(name, value)
    if name == "NOUS_TOOL_GUARD_ENABLED":
        monkeypatch.setenv("NOUS_AUTONOMY_SETTINGS_ENABLED", "true")
    got = getattr(Settings(_env_file=None), field)
    assert got == (True if value == "true" else value)


@pytest.mark.parametrize("name", list(NEW))
def test_compose_passes_each_setting_with_its_default(name):
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert f"- {name}=${{{name}:-{NEW[name][2]}}}" in compose


@pytest.mark.parametrize("name", list(NEW))
def test_the_environment_variables_doc_has_a_row(name):
    doc = (ROOT / "docs" / "reference" / "environment-variables.md").read_text(encoding="utf-8")
    assert f"| `{name}` |" in doc


class _NoDatabase:
    def session(self):
        raise AssertionError("a session was opened")


async def test_main_builds_no_store_and_reads_nothing_with_the_flag_off():
    settings = Settings(_env_file=None)
    before = settings.model_dump()
    assert await main._load_autonomy(settings, _NoDatabase()) is None
    assert settings.model_dump() == before


def test_main_applies_the_limits_right_after_the_runtime_config_and_hands_the_store_to_the_app():
    source = inspect.getsource(main.create_components)
    loaded = source.index("runtime_cfg.load_from_db")
    applied = source.index("_load_autonomy(settings, database)")
    assert loaded < applied < source.index("EmbeddingProvider(")
    assert '"autonomy": autonomy' in source
    app_source = inspect.getsource(main.build_app)
    assert 'autonomy=_lazy_component(components, "autonomy")' in app_source
    assert 'dispatcher=_lazy_component(components, "dispatcher")' in app_source
    parameters = inspect.signature(create_app).parameters
    assert parameters["autonomy"].default is None and parameters["dispatcher"].default is None


def test_only_the_store_writes_the_autonomy_tables():
    """No agent tool reaches the tables: only the store names their models, and no SQL text in ``nous/`` writes
    them."""
    models = re.compile(r"(AutonomySetting|AutonomySettingLog|ToolGuardRow)")
    writes = re.compile(
        r"(INSERT\s+INTO|UPDATE|DELETE\s+FROM|TRUNCATE)\s+nous_system\.(autonomy_settings|tool_guard)", re.IGNORECASE
    )
    allowed = {Path("nous/autonomy/store.py"), Path("nous/storage/models.py")}
    for path in (ROOT / "nous").rglob("*.py"):
        relative = path.relative_to(ROOT)
        text = path.read_text(encoding="utf-8")
        assert not writes.search(text), relative
        if relative not in allowed:
            assert not models.search(text), relative


def test_codeowners_lists_the_package_and_the_routes():
    owners = (ROOT / ".github" / "CODEOWNERS").read_text(encoding="utf-8").splitlines()
    assert "/nous/autonomy/** @tfatykhov" in owners
    assert "/nous/api/autonomy_routes.py @tfatykhov" in owners


async def test_host_ceilings_or_locks_without_the_setting_they_need_say_so_once(caplog):
    caplog.set_level(logging.WARNING)
    off = Settings(_env_file=None, autonomy_ceilings="max_turns=50", autonomy_locked_tools="bash")
    assert await main._load_autonomy(off, _NoDatabase()) is None
    said = [r.getMessage() for r in caplog.records if r.name == main.__name__]
    assert said == [
        "NOUS_AUTONOMY_CEILINGS and NOUS_AUTONOMY_LOCKED_TOOLS do nothing while NOUS_AUTONOMY_SETTINGS_ENABLED is off"
    ]
    caplog.clear()
    assert await main._load_autonomy(Settings(_env_file=None), _NoDatabase()) is None
    assert not [r for r in caplog.records if r.name == main.__name__]


async def test_locked_tools_without_the_tool_guard_say_so(caplog, monkeypatch):
    caplog.set_level(logging.WARNING)
    on = Settings(_env_file=None, autonomy_settings_enabled=True, autonomy_locked_tools="bash")

    class _Store:
        def __init__(self, database, settings):
            pass

        async def load(self):
            return None

    monkeypatch.setattr("nous.autonomy.store.AutonomyStore", _Store)
    assert isinstance(await main._load_autonomy(on, _NoDatabase()), _Store)
    said = [r.getMessage() for r in caplog.records if r.name == main.__name__]
    assert said == ["NOUS_AUTONOMY_LOCKED_TOOLS does nothing while NOUS_TOOL_GUARD_ENABLED is off"]


def test_shutdown_stops_the_store_reading_again():
    source = inspect.getsource(main.shutdown_components)
    assert 'components.get("autonomy")' in source and "await autonomy.close()" in source
