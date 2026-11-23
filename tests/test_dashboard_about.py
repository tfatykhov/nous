"""GET /dashboard/about: the About screen's answer. Its exact keys, the values read from the running process and the
database, the two optional build values and their shapes, and an answer when the database cannot be read."""

from __future__ import annotations

import contextlib
import hashlib
import json
import time
import tomllib
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

import nous
from nous.api import about_dashboard
from nous.api.about_dashboard import build_about
from nous.config import Settings

OWNER_VALUE = "about-owner-test-value"
OWNER = {"X-Nous-Owner-Secret": OWNER_VALUE}
OWNER_DIGEST = hashlib.sha256(OWNER_VALUE.encode()).hexdigest()

TOP_KEYS = "agent build database models name python schema started_at uptime_seconds version".split()
NESTED_KEYS = {
    "build": ["commit", "time"],
    "schema": ["latest"],
    "database": ["pgvector", "server"],
    "agent": ["id", "name"],
    "models": ["background", "critic", "embeddings", "heartbeat", "main"],
}
LATEST_SQL = "SELECT max(version::int) FROM nous_system.schema_migrations WHERE version ~ '^[0-9]{1,9}$'"


def _settings(tmp_path: Path, **kw: Any) -> Settings:
    # The workspace is the test's own: create_app makes a folder under it when the dashboard build exists.
    values: dict[str, Any] = {
        "build_commit": "",
        "build_time": "",
        "owner_route_secret_sha256": OWNER_DIGEST,
        "workspace_dir": str(tmp_path),
    }
    return Settings(_env_file=None, **{**values, **kw})


class _Unreadable:
    """A database whose session cannot be opened."""

    def session(self) -> Any:
        raise ConnectionError("no database here")


async def _about(database: Any, settings: Settings, headers: dict[str, str] = OWNER) -> httpx.Response:
    from nous.api.rest import create_app

    app = create_app(
        SimpleNamespace(),
        SimpleNamespace(),
        SimpleNamespace(schedules=None),
        SimpleNamespace(),
        database,
        settings,
        heartbeat_runner=SimpleNamespace(finding_store=None),
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://nous") as client:
        return await client.get("/dashboard/about", headers=headers)


def _assert_keys(answer: dict[str, Any]) -> None:
    assert sorted(answer) == TOP_KEYS
    for key, nested in NESTED_KEYS.items():
        assert sorted(answer[key]) == nested, key


@pytest.mark.postgres_only
async def test_the_answer_has_exactly_its_keys_with_a_real_database(db, tmp_path: Path) -> None:
    response = await _about(db, _settings(tmp_path))
    assert response.status_code == 200, response.text
    answer = response.json()
    _assert_keys(answer)
    assert answer["name"] == "Nous"
    assert isinstance(answer["uptime_seconds"], int)
    assert all(isinstance(name, str) and name for name in answer["models"].values())


@pytest.mark.postgres_only
async def test_the_database_values_are_the_databases_own(db, tmp_path: Path) -> None:
    answer = (await _about(db, _settings(tmp_path))).json()
    async with db.session() as session:
        latest = (await session.execute(text(LATEST_SQL))).scalar()
        server = (await session.execute(text("SHOW server_version"))).scalar()
    assert answer["schema"] == {"latest": latest}
    assert answer["database"]["server"] == server.split(" ", 1)[0]
    assert isinstance(answer["database"]["pgvector"], str) and answer["database"]["pgvector"]


@pytest.mark.postgres_only
async def test_the_schema_is_the_highest_numeric_version(session: AsyncSession, tmp_path: Path) -> None:
    # Rolled back with the session: a long number, one with a letter and a high one. Only the high one counts, and the
    # long one does not stop the read.
    await session.execute(
        text(
            "INSERT INTO nous_system.schema_migrations (version, name, checksum) VALUES "
            "('9999999999', 'about-test', 'x'), ('12a', 'about-test', 'x'), ('123456789', 'about-test', 'x')"
        )
    )

    @contextlib.asynccontextmanager
    async def same_session() -> AsyncIterator[AsyncSession]:
        yield session

    answer = await build_about(_settings(tmp_path), SimpleNamespace(session=same_session))
    assert answer["schema"] == {"latest": 123456789}
    assert answer["database"]["server"] is not None


async def test_the_version_is_the_packages(tmp_path: Path) -> None:
    answer = (await _about(_Unreadable(), _settings(tmp_path))).json()
    assert answer["version"] == nous.__version__


def test_the_package_version_and_pyproject_agree() -> None:
    pyproject = tomllib.loads((Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8"))
    assert pyproject["project"]["version"] == nous.__version__


@pytest.mark.parametrize(
    ("commit", "built", "expected"),
    [
        ("", "", {"commit": None, "time": None}),
        ("2B97AA31C0DE5F", "2026-11-23T14:02:05Z", {"commit": "2b97aa31c0de", "time": "2026-11-23T14:02:05Z"}),
        ("  2b97aa3  ", "2026-11-23T09:02:05-05:00", {"commit": "2b97aa3", "time": "2026-11-23T14:02:05Z"}),
        ("not a hash; rm", "yesterday", {"commit": None, "time": None}),
        ("2b97aa", "2026-11-23T14:02:05", {"commit": None, "time": None}),
        ("a" * 41, "2026-11-23", {"commit": None, "time": None}),
    ],
    ids=["unset", "both", "short-and-offset", "other-shapes", "too-short-and-no-timezone", "too-long-and-date-only"],
)
async def test_the_build_values_are_shown_only_in_their_shape(
    tmp_path: Path, commit: str, built: str, expected: dict
) -> None:
    answer = (await _about(_Unreadable(), _settings(tmp_path, build_commit=commit, build_time=built))).json()
    assert answer["build"] == expected


async def test_a_database_that_cannot_be_read_still_answers(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level("WARNING", logger="nous.api.about_dashboard"):
        response = await _about(_Unreadable(), _settings(tmp_path))
    assert response.status_code == 200, response.text
    answer = response.json()
    _assert_keys(answer)
    assert answer["schema"] == {"latest": None}
    assert answer["database"] == {"server": None, "pgvector": None}
    assert answer["version"] and answer["python"] and answer["agent"]["id"]
    assert all(answer["models"].values())
    messages = [r.getMessage() for r in caplog.records if r.name == "nous.api.about_dashboard"]
    assert messages == ["About: the database part could not be read (ConnectionError)"]


async def test_the_answer_carries_no_other_setting_value(tmp_path: Path) -> None:
    proxy_digest = hashlib.sha256(b"about-proxy-test-value").hexdigest()
    sentinels = {
        "DB_PASSWORD": "about-sentinel-db-value",
        "DB_HOST": "about-sentinel-host",
        "ANTHROPIC_API_KEY": "about-sentinel-api-value",
        "workspace_dir": str(tmp_path / "about-sentinel-workspace"),
        "mcp_client_ui_secret_sha256": proxy_digest,
    }
    settings = _settings(tmp_path, **sentinels)
    assert (settings.db_password, settings.db_host, settings.anthropic_api_key, settings.workspace_dir) == (
        "about-sentinel-db-value",
        "about-sentinel-host",
        "about-sentinel-api-value",
        sentinels["workspace_dir"],
    )
    assert (settings.owner_route_secret_sha256, settings.mcp_client_ui_secret_sha256) == (OWNER_DIGEST, proxy_digest)
    body = json.dumps((await _about(_Unreadable(), settings)).json())
    for value in [*sentinels.values(), OWNER_DIGEST, OWNER_VALUE]:
        assert value not in body


async def test_without_the_owners_secret_the_answer_is_refused(tmp_path: Path) -> None:
    response = await _about(_Unreadable(), _settings(tmp_path), headers={})
    assert (response.status_code, response.json()) == (403, {"error": "forbidden"})


async def test_started_at_is_utc_and_the_uptime_never_negative(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(tmp_path)
    answer = (await _about(_Unreadable(), settings)).json()
    started = datetime.fromisoformat(answer["started_at"])
    assert started.utcoffset() is not None and started.utcoffset().total_seconds() == 0
    assert answer["started_at"].endswith("Z")
    assert started <= datetime.now(UTC)

    monkeypatch.setattr(about_dashboard, "_STARTED_MONO", time.monotonic() - 3 * 86400)
    uptime = (await _about(_Unreadable(), settings)).json()["uptime_seconds"]
    assert 3 * 86400 <= uptime < 3 * 86400 + 60

    monkeypatch.setattr(about_dashboard, "_STARTED_MONO", time.monotonic() + 3600)
    assert (await _about(_Unreadable(), settings)).json()["uptime_seconds"] == 0


def test_the_build_settings_are_empty_by_default_and_read_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert Settings.model_fields["build_commit"].default == ""
    assert Settings.model_fields["build_time"].default == ""
    assert Settings(_env_file=None, build_commit="x").build_commit == "x"
    monkeypatch.setenv("NOUS_BUILD_COMMIT", "2b97aa31")
    monkeypatch.setenv("NOUS_BUILD_TIME", "2026-11-23T14:02:05Z")
    settings = Settings(_env_file=None)
    assert (settings.build_commit, settings.build_time) == ("2b97aa31", "2026-11-23T14:02:05Z")
