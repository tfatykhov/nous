"""F118 task reports: the setting, and how main.py builds the first judge's report judge and the sweep.

``NOUS_OUTSIDE_JUDGE_REPORTS_ENABLED`` defaults to false. With it off, the judge in shadow is built exactly as before:
no report judge and no sweep. With it on (and the mode shadow), the first judge's service gets the built-in report
judge over the same client and model, and runs the sweep; the second judge's service gets no report judge and runs its
own sweep of decisions. With the mode off nothing is built whatever the setting says. The tools a turn is offered do
not change.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from test_outside_judge_wiring import _boot_settings, _run

import nous.main as main
from nous import judge as judge_slot
from nous.config import Settings
from nous.judge import slot
from nous.judge.jev_judge import TIMEOUT_SECONDS
from nous.judge.model_judge import BackgroundModelJudge, BackgroundModelReportJudge
from nous.judge.service import CLOSE_TIMEOUT_SECONDS, OutsideJudge, call_bound

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _no_request_leaves_the_process(monkeypatch):
    import httpx

    async def refuse(self, request):
        raise AssertionError(f"a real request was attempted to {request.url.host}")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", refuse)


def test_the_setting_defaults_to_off_and_reads_the_environment(monkeypatch):
    assert Settings(_env_file=None).outside_judge_reports_enabled is False
    monkeypatch.setenv("NOUS_OUTSIDE_JUDGE_REPORTS_ENABLED", "true")
    assert Settings(_env_file=None).outside_judge_reports_enabled is True


def test_the_compose_file_passes_the_setting_with_its_default():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    line = "- NOUS_OUTSIDE_JUDGE_REPORTS_ENABLED=${NOUS_OUTSIDE_JUDGE_REPORTS_ENABLED:-false}"
    assert compose.count(line) == 1


def test_the_setting_has_a_row_in_the_environment_reference():
    text = (ROOT / "docs" / "reference" / "environment-variables.md").read_text(encoding="utf-8")
    assert text.count("| `NOUS_OUTSIDE_JUDGE_REPORTS_ENABLED` | `false` |") == 1


def test_with_reports_off_the_judge_is_built_as_before(monkeypatch):
    monkeypatch.setattr(slot, "PROVIDERS", {})
    built = main._build_outside_judge(
        Settings(_env_file=None, outside_judge_mode="shadow"), database=object(), api_client=object()
    )
    assert isinstance(built, OutsideJudge) and isinstance(built.judge, BackgroundModelJudge)
    assert built.report_judge is None and built.sweep_numbers() is None
    assert built._close_timeout == CLOSE_TIMEOUT_SECONDS  # the first judge's close waits as before


def test_with_reports_on_the_first_judge_gets_the_report_judge_and_the_sweep(monkeypatch):
    monkeypatch.setattr(slot, "PROVIDERS", {})
    client = object()
    settings = Settings(
        _env_file=None,
        outside_judge_mode="shadow",
        outside_judge_reports_enabled=True,
        outside_judge_model="claude-haiku-5-5",
    )
    built = main._build_outside_judge(settings, database=object(), api_client=client)
    report = built.report_judge
    assert isinstance(report, BackgroundModelReportJudge)
    assert (report.name, report.model, report._client) == (built.judge.name, "claude-haiku-5-5", client)
    assert report.version != built.judge.version
    assert built.sweep_numbers() == {
        "passes": 0,
        "queued_decisions": {},
        "queued_reports": 0,
        "held_at_half_limit": 0,
        "failed": 0,
        "last_pass_at": None,
    }
    assert judge_slot.current() is None


def test_with_the_mode_off_nothing_is_built_whatever_the_setting(monkeypatch):
    monkeypatch.setattr(slot, "PROVIDERS", {})
    settings = Settings(_env_file=None, outside_judge_reports_enabled=True)
    assert main._build_outside_judge(settings, database=object(), api_client=object()) is None


@pytest.mark.parametrize("reports", [True, False])
def test_the_second_judge_gets_no_report_judge_and_its_own_sweep_with_the_setting(monkeypatch, reports):
    monkeypatch.setattr(slot, "PROVIDERS", {})
    settings = Settings(
        _env_file=None,
        outside_judge_mode="shadow",
        outside_judge_reports_enabled=reports,
        outside_judge_second="jev",
        TYPESAFE_API_KEY="ts-wiring-" + "R7" * 16,
    )
    second = main._build_second_judge(settings, database=object())
    assert second is not None and second.judge.name == "jev"
    assert second.report_judge is None  # no report
    # Its own sweep of decisions with the setting on (built, not started: the numbers say whether it is on).
    assert (second.sweep_numbers() is not None) is reports
    # Its close waits out a request it has sent, so the request ends with its row.
    assert second._close_timeout == call_bound(TIMEOUT_SECONDS) > CLOSE_TIMEOUT_SECONDS
    assert second.submit_report(uuid.uuid4(), claimed_at=datetime.now(UTC)) is False
    assert second.runtime()["dropped"] == 0 and second.idle  # nothing queued, nothing counted


@pytest.mark.postgres_only
def test_with_reports_on_the_tools_are_those_of_the_judge_off_and_the_sweep_stops_at_shutdown(monkeypatch):
    from nous.api.execution_context import ExecutionContext

    agents = {key: f"test-f118-rboot-{key}-{uuid.uuid4().hex[:6]}" for key in ("off", "reports")}

    async def already_migrated(engine: Any) -> None:
        async with engine.begin() as connection:
            for agent_id in agents.values():
                await connection.execute(
                    text(
                        "INSERT INTO nous_system.agents (id, name, config) VALUES (:id, 'x', '{}') "
                        "ON CONFLICT (id) DO NOTHING"
                    ),
                    {"id": agent_id},
                )

    monkeypatch.setattr(main, "run_migrations", already_migrated)
    seen: dict[str, Any] = {}

    async def scenario() -> None:
        for key, agent_id in agents.items():
            settings = (
                _boot_settings(agent_id, "off")
                if key == "off"
                else _boot_settings(agent_id, "shadow", outside_judge_reports_enabled=True)
            )
            components = await main.create_components(settings)
            try:
                dispatcher, runner = components["dispatcher"], components["runner"]
                ctx = ExecutionContext(kind="interactive", session_id=f"boot-{key}")
                component = components.get("outside_judge")
                seen[key] = {
                    "definitions": dispatcher.tool_definitions(),
                    "offered": runner._offered_tools(
                        ctx, "task", is_subtask=False, tool_filter=None, refuse_active=False
                    ),
                    "component": component,
                    "sweeping": component is not None and not component._sweeper.done(),
                }
            finally:
                await main.shutdown_components(components)

    try:
        _run(scenario)
    finally:
        import asyncio

        async def cleanup() -> None:
            from nous.storage.database import Database

            database = Database(_boot_settings(agents["off"], "off"))
            await database.connect()
            try:
                async with database.session() as session:
                    for agent_id in agents.values():
                        await session.execute(
                            text("DELETE FROM heart.rubric_versions WHERE agent_id = :a"), {"a": agent_id}
                        )
                        await session.execute(text("DELETE FROM nous_system.agents WHERE id = :a"), {"a": agent_id})
                    await session.commit()
            finally:
                await database.disconnect()

        asyncio.run(cleanup())

    off, reports = seen["off"], seen["reports"]
    assert off["component"] is None
    component = reports["component"]
    assert isinstance(component, OutsideJudge) and isinstance(component.report_judge, BackgroundModelReportJudge)
    assert reports["sweeping"] is True and component._sweeper.done()  # stopped at shutdown
    assert reports["definitions"] == off["definitions"]
    assert reports["offered"] == off["offered"]
    assert not [d for d in reports["definitions"] if "judge" in d["name"]]


@pytest.mark.postgres_only
def test_with_reports_and_the_second_judges_settings_on_only_the_first_judge_judges_reports_and_both_sweep(monkeypatch):
    """Both new settings on: the first judge (in the settings' JudgeSet) has the report judge and the sweep; the
    second judge the settings manager starts has no report judge and its own sweep of decisions. At shutdown the
    manager closes before the judges, and both judges' sweeps stop with them."""
    import asyncio

    from test_outside_judge_wiring import JEV_KEY

    from nous.api.execution_context import ExecutionContext
    from nous.judge.second_settings import SHUTTING_DOWN, SecondJudgeManager
    from nous.judge.service import JudgeSet

    agent_id = f"test-f118-rboot2-{uuid.uuid4().hex[:6]}"

    async def already_migrated(engine: Any) -> None:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO nous_system.agents (id, name, config) VALUES (:id, 'x', '{}') ON CONFLICT DO NOTHING"
                ),
                {"id": agent_id},
            )

    monkeypatch.setattr(main, "run_migrations", already_migrated)
    settings = _boot_settings(
        agent_id,
        "shadow",
        outside_judge_reports_enabled=True,
        outside_judge_settings_enabled=True,
        outside_judge_second="jev",
        TYPESAFE_API_KEY=JEV_KEY,
    )
    seen: dict[str, Any] = {}

    async def scenario() -> None:
        components = await main.create_components(settings)
        try:
            ctx = ExecutionContext(kind="interactive", session_id="rboot2")
            current = judge_slot.current()
            seen.update(
                current=current,
                manager=components.get("judge_second"),
                sweeping=not current.primary._sweeper.done(),
                second=current.second,
                second_sweeping=current.second is not None and not current.second._sweeper.done(),
                offered=components["runner"]._offered_tools(
                    ctx, "task", is_subtask=False, tool_filter=None, refuse_active=False
                ),
            )
        finally:
            await main.shutdown_components(components)
        seen["after"] = await seen["manager"].patch({"on": False}, confirmed=None, actor="owner")

    try:
        _run(scenario)
    finally:

        async def cleanup() -> None:
            from nous.storage.database import Database

            database = Database(settings)
            await database.connect()
            try:
                async with database.session() as session:
                    for table in ("heart.rubric_versions", "nous_system.judge_second_settings"):
                        await session.execute(text(f"DELETE FROM {table} WHERE agent_id = :a"), {"a": agent_id})
                    await session.execute(text("DELETE FROM nous_system.agents WHERE id = :a"), {"a": agent_id})
                    await session.commit()
            finally:
                await database.disconnect()

        asyncio.run(cleanup())

    current, second = seen["current"], seen["second"]
    assert isinstance(seen["manager"], SecondJudgeManager) and isinstance(current, JudgeSet)
    assert isinstance(current.primary.report_judge, BackgroundModelReportJudge) and seen["sweeping"] is True
    assert current.report_judge is current.primary.report_judge
    assert isinstance(second, OutsideJudge) and second.report_judge is None and second.sweep_numbers() is not None
    assert seen["second_sweeping"] is True
    assert not [tool for tool in seen["offered"] if "judge" in str(tool)]
    # Shut down: the manager takes no more changes, both judges' sweeps stopped and both judges take no report.
    assert (seen["after"].status, seen["after"].body["error"]) == (503, SHUTTING_DOWN)
    assert current.primary._sweeper.done() and second._closed and second._sweeper.done()
    assert current.submit_report(uuid.uuid4(), claimed_at=datetime.now(UTC)) is False
