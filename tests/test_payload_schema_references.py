"""A payload schema is validated without retrieving any schema it refers to.

The functions that could retrieve a reference (urllib's urlopen, a socket connection, the resolver) are patched to
record: none of them is called.
"""

from __future__ import annotations

import socket
import urllib.request
from typing import Any
from unittest.mock import patch

import jsonschema
import pytest

from nous.handlers.subtask_executor import validate_payload


class _Calls:
    def __init__(self) -> None:
        self.names: list[str] = []

    def recorder(self, name: str, result: Any = None) -> Any:
        def record(*args: Any, **kwargs: Any) -> Any:
            self.names.append(name)
            raise OSError("no retrieval in this test")

        return record


@pytest.fixture
def retrievals():
    calls = _Calls()
    with (
        patch.object(urllib.request, "urlopen", calls.recorder("urlopen")),
        patch.object(socket, "create_connection", calls.recorder("create_connection")),
        patch.object(socket, "getaddrinfo", calls.recorder("getaddrinfo")),
    ):
        yield calls.names


@pytest.mark.parametrize(
    "ref",
    [
        "http://example.com/schema.json",
        "https://example.com/schema.json#/definitions/x",
        "other.json",
        "http://127.0.0.1/schema.json",
        "file:///etc/hostname",
    ],
)
def test_a_reference_to_a_schema_outside_the_payload_schema_is_a_validation_error(retrievals, ref: str) -> None:
    schema = {"type": "object", "properties": {"x": {"$ref": ref}}}
    with pytest.raises(jsonschema.ValidationError, match="not available"):
        validate_payload({"x": 1}, schema)
    assert retrievals == []


def test_a_reference_through_an_id_base_is_not_retrieved(retrievals) -> None:
    schema = {"$id": "http://example.com/root.json", "properties": {"x": {"$ref": "child.json"}}}
    with pytest.raises(jsonschema.ValidationError):
        validate_payload({"x": 1}, schema)
    assert retrievals == []


def test_a_reference_inside_the_schema_is_followed(retrievals) -> None:
    schema = {
        "type": "object",
        "properties": {"x": {"$ref": "#/definitions/n"}},
        "required": ["x"],
        "definitions": {"n": {"type": "integer"}},
    }
    validate_payload({"x": 1}, schema)
    with pytest.raises(jsonschema.ValidationError):
        validate_payload({"x": "one"}, schema)
    assert retrievals == []


def test_a_standard_meta_schema_reference_resolves_without_retrieval(retrievals) -> None:
    schema = {"type": "object", "properties": {"s": {"$ref": "https://json-schema.org/draft/2020-12/schema"}}}
    validate_payload({"s": {"type": "string"}}, schema)
    with pytest.raises(jsonschema.ValidationError):
        validate_payload({"s": {"type": 5}}, schema)
    assert retrievals == []


def test_an_ordinary_payload_is_validated_as_before(retrievals) -> None:
    schema = {"type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"]}
    validate_payload({"n": 3}, schema)
    with pytest.raises(jsonschema.ValidationError):
        validate_payload({}, schema)
    assert retrievals == []


def test_a_malformed_schema_is_still_a_schema_error(retrievals) -> None:
    with pytest.raises(jsonschema.SchemaError):
        validate_payload({}, {"type": 12})
    assert retrievals == []
