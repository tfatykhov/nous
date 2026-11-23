"""F118 PR 3: the routes of the second judge's settings (``nous/api/judge_second_routes.py``), through the app.

Every route needs the owner's or the proxy's secret before its body is read. With the setting off the read answers
``{"enabled": false}`` and every change 409, with no database session. A body is bounded, typed strictly and refused
by field name; the key travels in a body only and is never answered, logged, put in the environment or stored but
sealed. Real Postgres for the row; the second judge runs as a stand-in service; no request leaves the process.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text

from nous.config import Settings
from nous.judge import second_settings
from nous.judge.second_settings import KEY_RULE, MODE_OFF, NO_KEY_FILE_TEXT, NOT_LOADED, SecondJudgeManager
from nous.judge.service import JudgeSet
from nous.mcp_client.secrets import SecretBox

ROOT = Path(__file__).resolve().parents[1]
OWNER_VALUE = "owner-only-test-value"
PROXY_VALUE = "proxy-only-test-value"
OWNER = {"X-Nous-Owner-Secret": OWNER_VALUE}
PROXY = {"X-Nous-Proxy-Secret": PROXY_VALUE}
JSON = {"content-type": "application/json"}
FORBIDDEN = {"error": "forbidden"}
NOT_JSON = {"error": "Content-Type must be application/json"}
OFF = {"error": "The second judge's settings are off on this server.", "enabled": False}
PATH = "/dashboard/outside-judge/second"
KEY = "ts-route-" + "R2" * 12
KEY_2 = "ts-route-" + "T6" * 12
ENV_KEY = "ts-route-env-" + "U1" * 10
MODEL = "jev-1.14.0"
BOX = SecretBox(b"\x66" * 32)
# Every change, with a body each accepts.
WRITES = [
    ("PATCH", PATH, {"on": False}),
    ("PUT", f"{PATH}/key", {"key": KEY}),
    ("DELETE", f"{PATH}/key", {}),
    ("POST", f"{PATH}/check", {}),
]


@pytest.fixture(autouse=True)
def _no_request_leaves_the_process(monkeypatch):
    async def refuse(self: Any, request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"a real request was attempted to {request.url.host}")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", refuse)


class RefusingDatabase:
    """A database that fails the test if a session is opened."""

    def session(self) -> Any:
        raise AssertionError("a database session was opened")


class FixedKeyFile:
    def __init__(self, box: SecretBox | None) -> None:
        self._box = box

    def box(self) -> SecretBox | None:
        return self._box


class StandIn:
    """The second judge's service."""

    def __init__(self, config: Any) -> None:
        self.config = config
        self.judge = SimpleNamespace(name="jev", version=f"jq-2:{config.model}", model=config.model)
        self.daily_calls = config.daily_calls
        self.closed = False

    def start(self) -> None:
        pass

    def submit_decision(self, decision_id: Any, **claim: Any) -> bool:
        return not self.closed

    def runtime(self) -> dict[str, Any]:
        return {"queued": 0, "dropped": 0, "over_cap": 0, "worker_alive": not self.closed}

    async def close(self, timeout: float = 5.0) -> None:
        self.closed = True


class Primary:
    judge = SimpleNamespace(name="background_model", version="dj-2", model="claude-test")
    daily_calls = 100

    def submit_decision(self, decision_id: Any, **claim: Any) -> bool:
        return True

    def runtime(self) -> dict[str, Any]:
        return {"queued": 0, "dropped": 0, "over_cap": 0, "worker_alive": True}

    async def close(self, timeout: float = 5.0) -> None:  # pragma: no cover - must never be called
        raise AssertionError("the first judge was closed")


def _settings(agent: str = "test-f118-routes", **over: Any) -> Settings:
    values: dict[str, Any] = {
        "_env_file": None,
        "agent_id": agent,
        "owner_route_secret_sha256": hashlib.sha256(OWNER_VALUE.encode()).hexdigest(),
        "mcp_client_ui_secret_sha256": hashlib.sha256(PROXY_VALUE.encode()).hexdigest(),
        "outside_judge_mode": "shadow",
        "outside_judge_settings_enabled": True,
    }
    return Settings(**{**values, **over})


def _app(tmp_path, settings: Settings, database: Any, manager: Any = None):
    from nous.api.rest import create_app

    settings = settings.model_copy(update={"workspace_dir": str(tmp_path)})
    stand_in = SimpleNamespace()
    return create_app(
        stand_in, stand_in, SimpleNamespace(schedules=None), stand_in, database, settings, judge_second=manager
    )


async def _send(app: Any, method: str, path: str, **kwargs: Any) -> httpx.Response:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://nous") as client:
        return await client.request(method, path, **kwargs)


def _provider(answers: list[Any]) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        answer = answers.pop(0) if answers else (200, {"model": MODEL})
        if isinstance(answer, Exception):
            raise answer
        status, body = answer
        return httpx.Response(status, json=body)

    return httpx.MockTransport(handle)


class Served:
    """One test's app over a real manager."""

    def __init__(self, app: Any, manager: SecondJudgeManager, judges: JudgeSet | None, agent: str, db: Any) -> None:
        self.app, self.manager, self.judges, self.agent, self.db = app, manager, judges, agent, db

    async def send(self, method: str, path: str, body: Any = None, headers: dict[str, str] | None = None) -> Any:
        kwargs: dict[str, Any] = {"headers": {**JSON, **OWNER, **(headers or {})}}
        if body is not None:
            kwargs["content"] = body if isinstance(body, bytes) else json.dumps(body).encode()
        return await _send(self.app, method, path, **kwargs)

    async def row(self) -> Any:
        async with self.db.session() as session:
            return await second_settings.read_row(session, self.agent)


@pytest_asyncio.fixture
async def served(db, tmp_path, monkeypatch):
    made: list[Served] = []

    async def build(
        *, key_file: SecretBox | None = BOX, answers: list[Any] | None = None, load: bool = True, **over: Any
    ) -> Served:
        agent = f"test-f118-routes-{uuid.uuid4().hex[:8]}"
        settings = _settings(agent, **over)
        judges = JudgeSet(Primary(), None) if settings.outside_judge_mode == "shadow" else None
        manager = SecondJudgeManager(
            db, settings, judges, transport=_provider(answers or []), key_file=FixedKeyFile(key_file)
        )
        monkeypatch.setattr(manager, "_make_service", StandIn)
        if load:
            await manager.load()
        result = Served(_app(tmp_path, settings, db, manager), manager, judges, agent, db)
        made.append(result)
        return result

    yield build
    for result in made:
        await result.manager.close()
        if result.judges is not None and result.judges.second is not None:
            await result.judges.replace_second(None).close()
    async with db.session() as session:
        for result in made:
            await session.execute(
                text("DELETE FROM nous_system.judge_second_settings WHERE agent_id = :a"), {"a": result.agent}
            )
        await session.commit()


# ---- the setting off and the guard -----------------------------------------------------------------------------------


async def test_with_the_setting_off_the_read_answers_disabled_and_every_change_409_without_a_session(tmp_path):
    app = _app(tmp_path, _settings(outside_judge_settings_enabled=False), RefusingDatabase())
    read = await _send(app, "GET", PATH, headers=OWNER)
    assert (read.status_code, read.json()) == (200, {"enabled": False})
    for method, path, body in WRITES:
        # Not even read: a body the route would refuse gets the same answer.
        for content in (json.dumps(body).encode(), b"not json", b"x" * 10_000):
            response = await _send(app, method, path, content=content, headers={**OWNER, **JSON})
            assert (response.status_code, response.json()) == (409, OFF), (method, path)


async def test_with_the_setting_off_a_manager_given_is_never_used(tmp_path):
    class Untouchable:
        def __getattr__(self, name: str) -> Any:
            raise AssertionError(f"the manager was used: {name}")

        def __bool__(self) -> bool:
            return True

    app = _app(tmp_path, _settings(outside_judge_settings_enabled=False), RefusingDatabase(), Untouchable())
    assert (await _send(app, "GET", PATH, headers=OWNER)).json() == {"enabled": False}
    response = await _send(app, "PATCH", PATH, json={"on": True}, headers=OWNER)
    assert response.status_code == 409


async def test_with_the_setting_on_and_no_manager_every_route_answers_503(tmp_path):
    app = _app(tmp_path, _settings(), RefusingDatabase())
    assert (await _send(app, "GET", PATH, headers=OWNER)).status_code == 503
    for method, path, body in WRITES:
        response = await _send(app, method, path, json=body, headers=OWNER)
        assert response.status_code == 503, (method, path)


async def test_the_routes_read_the_manager_through_the_apps_lazy_component(tmp_path):
    """As main.build_app passes it: a proxy over the components, falsy until the lifespan has built the manager."""
    from nous import main

    components: dict[str, Any] = {}
    app = _app(tmp_path, _settings(), RefusingDatabase(), main._lazy_component(components, "judge_second"))
    assert (await _send(app, "GET", PATH, headers=OWNER)).status_code == 503
    components["judge_second"] = SecondJudgeManager(
        RefusingDatabase(), _settings(), JudgeSet(Primary(), None), key_file=FixedKeyFile(BOX)
    )
    read = await _send(app, "GET", PATH, headers=OWNER)
    assert read.status_code == 200 and read.json()["loaded"] is False and read.json()["locked"] is None
    write = await _send(app, "PATCH", PATH, json={"on": True}, headers=OWNER)
    assert (write.status_code, write.json()) == (503, {"error": NOT_LOADED})


@pytest.mark.parametrize("enabled", [False, True])
async def test_every_route_needs_the_owners_or_the_proxys_secret_before_the_body_is_read(tmp_path, enabled):
    app = _app(tmp_path, _settings(outside_judge_settings_enabled=enabled), RefusingDatabase())
    for method, path, _body in [("GET", PATH, None), *WRITES]:
        for headers in ({}, {"X-Nous-Owner-Secret": "a-wrong-value"}, {"X-Nous-Proxy-Secret": "a-wrong-value"}):
            response = await _send(app, method, path, content=b"not json", headers=headers)
            assert (response.status_code, response.json()) == (403, FORBIDDEN), (method, path)


async def test_a_proxy_only_change_that_is_not_json_is_415_a_delete_too(tmp_path):
    app = _app(tmp_path, _settings(), RefusingDatabase())
    for method, path, body in WRITES:
        for headers in ({**PROXY}, {**PROXY, "content-type": "text/plain"}):
            response = await _send(app, method, path, content=json.dumps(body).encode(), headers=headers)
            assert (response.status_code, response.json()) == (415, NOT_JSON), (method, path)
    # A read with the proxy's secret needs no type.
    assert (await _send(app, "GET", PATH, headers=PROXY)).status_code == 503


def test_exactly_these_five_routes_are_mounted_and_guarded(tmp_path):
    app = _app(tmp_path, _settings(outside_judge_settings_enabled=False), RefusingDatabase())
    found = {
        (method, route.path, getattr(route.endpoint, "owner_guarded", False))
        for route in app.routes
        if getattr(route, "path", "").startswith(PATH)
        for method in (route.methods or set()) - {"HEAD"}
    }
    assert found == {
        ("GET", PATH, True),
        ("PATCH", PATH, True),
        ("PUT", f"{PATH}/key", True),
        ("DELETE", f"{PATH}/key", True),
        ("POST", f"{PATH}/check", True),
    }


# ---- reading and the bodies ------------------------------------------------------------------------------------------


@pytest.mark.postgres_only
async def test_the_read_answers_each_value_and_where_it_comes_from(served):
    server = await served(outside_judge_second="jev", TYPESAFE_API_KEY=ENV_KEY, outside_judge_daily_calls=30)
    response = await server.send("GET", PATH)
    assert response.status_code == 200
    assert response.json() == {
        "enabled": True,
        "locked": None,
        "loaded": True,
        "running": True,
        "on": {"value": True, "source": "server"},
        "model": {"value": MODEL, "source": "server"},
        "daily_calls": {"value": 30, "most": 30, "source": "server"},
        "key": {"source": "server", "set_at": None, "problem": None, "server_has_key": True},
        "can_store_key": True,
        "sends_to": "api.typesafe.ai",
        "confirm_text": second_settings.confirm_text(30),
        "updated_at": None,
        "updated_by": None,
    }
    assert ENV_KEY not in response.text


@pytest.mark.postgres_only
async def test_with_the_reports_setting_on_the_read_and_the_refusal_carry_the_hourly_sentence(served):
    server = await served(outside_judge_reports_enabled=True, outside_judge_daily_calls=30)
    sentence = second_settings.confirm_text(30, hourly=True)
    assert (await server.send("GET", PATH)).json()["confirm_text"] == sentence
    assert (await server.send("PATCH", PATH, {"on": True})).status_code == 200  # no key yet: nothing is sent
    refused = await server.send("PUT", f"{PATH}/key", {"key": KEY})
    assert (refused.status_code, refused.json()) == (
        409,
        {"error": sentence, "needs_confirm": True, "confirm_daily_calls": 30},
    )


@pytest.mark.postgres_only
@pytest.mark.parametrize(
    ("method", "path", "body", "error"),
    [
        ("PATCH", PATH, {"on": False, "extra": 1}, "Not accepted: an unknown field."),
        ("PATCH", PATH, {"on": None}, "Not accepted: on."),
        ("PATCH", PATH, {"on": "true"}, "Not accepted: on."),
        ("PATCH", PATH, {"daily_calls": True}, "Not accepted: daily_calls."),
        ("PATCH", PATH, {"daily_calls": 50.0}, "Not accepted: daily_calls."),
        ("PATCH", PATH, {"daily_calls": "5"}, "Not accepted: daily_calls."),
        ("PATCH", PATH, {"model": 1}, "Not accepted: model."),
        ("PATCH", PATH, {"confirm_send": 1}, "Not accepted: confirm_send."),
        ("PATCH", PATH, {"on": True, "confirm_daily_calls": 5.0}, "Not accepted: confirm_daily_calls."),
        ("PATCH", PATH, {"on": True, "confirm_daily_calls": True}, "Not accepted: confirm_daily_calls."),
        ("PATCH", PATH, {}, "Nothing to change."),
        ("PATCH", PATH, {"confirm_send": True}, "Nothing to change."),
        ("PATCH", PATH, {"model": "jev-latest"}, second_settings.MODEL_RULE),
        ("PATCH", PATH, {"daily_calls": -1}, second_settings.calls_rule(100)),
        ("PATCH", PATH, {"daily_calls": 101}, second_settings.calls_rule(100)),
        ("PUT", f"{PATH}/key", {}, "Not accepted: key."),
        ("PUT", f"{PATH}/key", {"key": 12345678901234567}, "Not accepted: key."),
        ("PUT", f"{PATH}/key", {"key": "has a space-in-it-1234"}, KEY_RULE),
        ("PUT", f"{PATH}/key", {"key": "line\nbreak-key-12345678"}, KEY_RULE),
        ("PUT", f"{PATH}/key", {"key": "k" * 15}, KEY_RULE),
        ("PUT", f"{PATH}/key", {"key": "k" * 201}, KEY_RULE),
        ("PUT", f"{PATH}/key", {"key": KEY, "other": 1}, "Not accepted: an unknown field."),
        ("DELETE", f"{PATH}/key", {"key": KEY}, "Not accepted: an unknown field."),
        ("POST", f"{PATH}/check", {"key": KEY}, "Not accepted: an unknown field."),
        # A field name is not repeated: it could be anything typed.
        ("PATCH", PATH, {KEY: 1}, "Not accepted: an unknown field."),
        ("PATCH", PATH, [1], "The body must be a JSON object."),
        ("PATCH", PATH, b"{not json", "The body must be JSON."),
        ("PUT", f"{PATH}/key", b"x" * 5000, "The body must be at most 4096 bytes."),
    ],
)
async def test_a_body_outside_its_rule_is_400_naming_the_field_never_the_value(served, method, path, body, error):
    server = await served(outside_judge_daily_calls=100)
    response = await server.send(method, path, body)
    assert (response.status_code, response.json()) == (400, {"error": error})
    assert KEY not in response.text and "has a space" not in response.text
    assert await server.row() is None


@pytest.mark.postgres_only
async def test_the_limit_is_accepted_up_to_the_servers_number(served):
    server = await served(outside_judge_daily_calls=40)
    response = await server.send("PATCH", PATH, {"daily_calls": 40})
    assert response.status_code == 200 and response.json()["daily_calls"] == {
        "value": 40,
        "most": 40,
        "source": "stored",
    }
    assert (await server.send("PATCH", PATH, {"daily_calls": 41})).status_code == 400
    back = await server.send("PATCH", PATH, {"daily_calls": None})
    assert back.json()["daily_calls"]["source"] == "server"


# ---- the key ---------------------------------------------------------------------------------------------------------


@pytest.mark.postgres_only
async def test_saving_the_key_needs_the_confirmation_when_it_starts_the_sending_and_is_never_answered(served):
    server = await served()
    on = await server.send("PATCH", PATH, {"on": True})
    assert on.status_code == 200 and on.json()["running"] is False
    refused = await server.send("PUT", f"{PATH}/key", {"key": KEY})
    assert refused.status_code == 409
    assert refused.json() == {
        "error": second_settings.confirm_text(100),
        "needs_confirm": True,
        "confirm_daily_calls": 100,
    }
    # A yes that carries only the boolean, or another number, is asked again.
    for bare in (
        {"confirm_send": True},
        {"confirm_send": True, "confirm_daily_calls": 99},
        {"confirm_daily_calls": 100},
    ):
        again = await server.send("PUT", f"{PATH}/key", {"key": KEY, **bare})
        assert (again.status_code, again.json()) == (409, refused.json())
    assert await server.row() is not None and not (await server.row()).has_key
    saved = await server.send("PUT", f"{PATH}/key", {"key": KEY, "confirm_send": True, "confirm_daily_calls": 100})
    assert saved.status_code == 200 and saved.json()["running"] is True and saved.json()["changed"] is True
    assert saved.json()["key"]["source"] == "stored" and saved.json()["key"]["set_at"]
    for answer in (refused, saved, await server.send("GET", PATH)):
        assert KEY not in answer.text and KEY[-8:] not in answer.text
    # What the card is told of the key: where it comes from, when it was saved, why it cannot be used.
    assert set(saved.json()["key"]) == {"source", "set_at", "problem", "server_has_key"}


@pytest.mark.postgres_only
async def test_a_patch_keeps_the_key_and_a_removal_stops_the_sending(served):
    server = await served(outside_judge_daily_calls=5)
    await server.send("PUT", f"{PATH}/key", {"key": KEY})
    started = await server.send("PATCH", PATH, {"on": True, "confirm_send": True, "confirm_daily_calls": 5})
    assert started.json()["running"] is True
    changed = await server.send("PATCH", PATH, {"model": "jev-1.15.0"})
    assert changed.status_code == 200 and changed.json()["model"] == {"value": "jev-1.15.0", "source": "stored"}
    assert second_settings.open_key(BOX, server.agent, await server.row()) == (KEY, None)
    removed = await server.send("DELETE", f"{PATH}/key", {})
    assert removed.status_code == 200 and removed.json()["running"] is False
    assert removed.json()["key"] == {"source": "none", "set_at": None, "problem": None, "server_has_key": False}
    # The dashboard's DELETE with no body at all.
    assert (await server.send("DELETE", f"{PATH}/key")).json()["changed"] is False


@pytest.mark.postgres_only
async def test_without_a_key_file_a_key_cannot_be_saved(served):
    server = await served(key_file=None)
    response = await server.send("PUT", f"{PATH}/key", {"key": KEY, "confirm_send": True})
    assert (response.status_code, response.json()) == (409, {"error": NO_KEY_FILE_TEXT})
    assert (await server.send("GET", PATH)).json()["can_store_key"] is False


@pytest.mark.postgres_only
async def test_the_key_is_in_no_answer_no_log_record_no_environment_and_no_column_but_the_sealed_ones(served, caplog):
    caplog.set_level(logging.DEBUG)
    before = dict(os.environ)
    server = await served(answers=[RuntimeError(KEY), (200, {"model": MODEL, "echo": KEY_2}), (500, {"k": KEY})])
    answers = []
    for method, path, body in [
        ("PUT", f"{PATH}/key", {"key": KEY}),
        ("PATCH", PATH, {"on": True}),
        ("PATCH", PATH, {"on": True, "confirm_send": True, "confirm_daily_calls": 100}),
        ("POST", f"{PATH}/check", {}),
        ("PUT", f"{PATH}/key", {"key": f" {KEY_2} ", "confirm_send": True}),
        ("PUT", f"{PATH}/key", {"key": KEY + " x"}),
        ("PUT", f"{PATH}/key", {"key": KEY, "bad": 1}),
        ("GET", PATH, None),
    ]:
        answers.append(await server.send(method, path, body))
        assert dict(os.environ) == before, (method, path)
    server.manager._last_check = None
    answers.append(await server.send("POST", f"{PATH}/check", {}))
    server.manager._last_check = None
    answers.append(await server.send("POST", f"{PATH}/check", {}))
    assert [answer.status_code for answer in answers] == [200, 409, 200, 200, 200, 400, 400, 200, 200, 200]
    assert [answer.json()["result"] for answer in (answers[3], answers[8], answers[9])] == [
        "unreachable",
        "ok",
        "unreachable",
    ]
    for answer in answers:
        for key in (KEY, KEY_2):
            assert key not in answer.text and key[-8:] not in answer.text
    for record in caplog.records:
        logged = record.getMessage() + repr(record.args) + repr(record.exc_info) + str(record.exc_text)
        for key in (KEY, KEY_2):
            assert key not in logged and key[-8:] not in logged, record.name
    async with server.db.session() as session:
        row = (
            (
                await session.execute(
                    text("SELECT * FROM nous_system.judge_second_settings WHERE agent_id = :a"), {"a": server.agent}
                )
            )
            .mappings()
            .one()
        )
    for name, value in row.items():
        raw = value if isinstance(value, bytes) else str(value).encode()
        for key in (KEY, KEY_2):
            assert key.encode() not in raw and key[-8:].encode() not in raw, name
    assert second_settings.open_key(BOX, server.agent, await server.row()) == (KEY_2, None)
    assert dict(os.environ) == before


# ---- the states the routes refuse --------------------------------------------------------------------------------


@pytest.mark.postgres_only
async def test_while_the_settings_are_not_loaded_the_changes_answer_503(served):
    server = await served(load=False)
    assert (await server.send("GET", PATH)).json()["loaded"] is False
    for method, path, body in WRITES:
        response = await server.send(method, path, body)
        assert (response.status_code, response.json()) == (503, {"error": NOT_LOADED}), (method, path)


@pytest.mark.postgres_only
async def test_with_the_mode_off_only_the_removal_is_allowed(served):
    server = await served(outside_judge_mode="off")
    assert (await server.send("GET", PATH)).json()["locked"] == "off"
    for method, path, body in WRITES:
        response = await server.send(method, path, body)
        if method == "DELETE":
            assert response.status_code == 200
        else:
            assert (response.status_code, response.json()) == (409, {"error": MODE_OFF}), (method, path)


@pytest.mark.postgres_only
async def test_the_check_without_a_key_is_409_and_too_soon_is_429(served):
    server = await served()
    response = await server.send("POST", f"{PATH}/check", {})
    assert (response.status_code, response.json()) == (409, {"error": second_settings.NO_KEY_TO_CHECK})
    await server.send("PUT", f"{PATH}/key", {"key": KEY})
    assert (await server.send("POST", f"{PATH}/check")).json() == {"result": "ok"}
    again = await server.send("POST", f"{PATH}/check", {})
    assert (again.status_code, again.json()) == (429, {"error": second_settings.CHECK_LIMIT})


# ---- the agent -------------------------------------------------------------------------------------------------------


def test_no_tool_and_no_other_module_names_the_table_or_the_routes():
    # The module, its routes, and the setting's comment.
    allowed = {"nous/judge/second_settings.py", "nous/api/judge_second_routes.py", "nous/config.py"}
    for path in (ROOT / "nous").rglob("*.py"):
        rel = path.relative_to(ROOT).as_posix()
        if rel in allowed:
            continue
        source = path.read_text(encoding="utf-8")
        assert "judge_second_settings" not in source, rel
        assert "outside-judge/second" not in source, rel
