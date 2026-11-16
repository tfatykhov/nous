"""execute_hardened validates a payload schema without retrieving references, and reports a schema it cannot finish.

The functions that could retrieve a reference are patched to record: none is called.
"""

from __future__ import annotations

import socket
import urllib.request
from unittest.mock import patch

import pytest
from test_f062_executor_schema_validation import (
    _make_heart_mock,
    _make_settings,
    _make_subtask,
    _scripted_runner,
)

from nous.handlers.subtask_executor import execute_hardened

_PAYLOAD = {
    "summary": "Returned a payload for a schema that refers to something else.",
    "confidence": 0.7,
    "payload": {"x": 1},
}


async def _run(schema: dict) -> tuple[object, list[str]]:
    calls: list[str] = []

    def record(name: str):
        def fn(*args: object, **kwargs: object) -> object:
            calls.append(name)
            raise OSError("no retrieval in this test")

        return fn

    runner = _scripted_runner(scripted_payloads=[_PAYLOAD, _PAYLOAD])
    with (
        patch.object(urllib.request, "urlopen", record("urlopen")),
        patch.object(socket, "create_connection", record("create_connection")),
        patch.object(socket, "getaddrinfo", record("getaddrinfo")),
    ):
        _final, result = await execute_hardened(
            _make_subtask(payload_schema=schema),
            "sess-1",
            runner=runner,
            heart=_make_heart_mock(),
            settings=_make_settings(max_attempts=2),
        )
    return result, calls


@pytest.mark.asyncio
@pytest.mark.parametrize("ref", ["http://example.com/schema.json", "other.json", "file:///tmp/schema.json"])
async def test_a_payload_schema_with_a_reference_outside_it_is_validation_failed_without_retrieval(ref: str) -> None:
    result, calls = await _run({"type": "object", "properties": {"x": {"$ref": ref}}})
    assert result.ok is False
    assert result.outcome == "validation_failed"
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "schema",
    [
        {"$ref": "#"},
        {"type": "object", "properties": {"x": {"$ref": "#"}}, "$defs": {}, "allOf": [{"$ref": "#"}]},
    ],
)
async def test_a_payload_schema_that_refers_to_itself_without_end_is_validation_failed(schema: dict) -> None:
    result, calls = await _run(schema)
    assert result.ok is False
    assert result.outcome == "validation_failed"
    assert calls == []
