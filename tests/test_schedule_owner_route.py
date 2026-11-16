"""POST /schedules requires the owner's or the proxy's secret in every NOUS_OWNER_ENTRY_MODE (it has no switch). No
database: the app is built with stand-ins, as tests/test_entry_gate_routes.py does."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_entry_gate_routes import FORBIDDEN, MODES, NOT_JSON, PROOFS, PROVEN, _rest_app, _send


def _schedule_heart() -> SimpleNamespace:
    created = SimpleNamespace(
        id="5b0b5f1e-0000-4000-8000-000000000001", schedule_type="once", next_fire_at=None, active=True
    )
    return SimpleNamespace(
        schedules=SimpleNamespace(
            create=AsyncMock(return_value=created),
            list=AsyncMock(return_value=[]),
            get=AsyncMock(return_value=None),
            deactivate=AsyncMock(return_value=None),
        )
    )


SCHEDULE_BODY = {"task": "Check the snow report", "when": "in 1 hour"}


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("proof", list(PROOFS))
async def test_creating_a_schedule_needs_the_owners_or_the_proxys_secret_in_every_mode(
    tmp_path, mode: str, proof: str
) -> None:
    heart = _schedule_heart()
    app = _rest_app(tmp_path, mode, heart=heart)
    response = await _send(app, "POST", "/schedules", content=json.dumps(SCHEDULE_BODY).encode(), headers=PROOFS[proof])
    if proof in PROVEN:
        assert response.status_code == 200, response.text
        assert heart.schedules.create.await_count == 1
    else:
        expected = (415, NOT_JSON) if proof == "proxy-text" else (403, FORBIDDEN)
        assert (response.status_code, response.json()) == expected
        assert heart.schedules.create.await_count == 0
    assert app.state.entry_gate.drain_counts() == {}  # not the entry gate's: nothing is counted


async def test_creating_a_schedule_is_refused_before_the_body_is_read(tmp_path) -> None:
    heart = _schedule_heart()
    response = await _send(_rest_app(tmp_path, "off", heart=heart), "POST", "/schedules", content=b"\xff not json")
    assert (response.status_code, response.json()) == (403, FORBIDDEN)
    assert heart.schedules.create.await_count == 0


async def test_the_other_schedule_routes_are_unchanged(tmp_path) -> None:
    heart = _schedule_heart()
    app = _rest_app(tmp_path, "enforce", heart=heart)
    listed = await _send(app, "GET", "/schedules")
    assert (listed.status_code, listed.json()) == (200, {"schedules": []})
    deleted = await _send(app, "DELETE", "/schedules/5b0b5f1e-0000-4000-8000-000000000001")
    assert (deleted.status_code, deleted.json()) == (404, {"error": "Schedule not found"})  # its handler ran
    assert heart.schedules.get.await_count == 1
