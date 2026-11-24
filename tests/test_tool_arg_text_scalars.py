"""A text value for an argument declared integer, number or boolean.

Observed in prod: the model called recall_deep with hours="48" (the schema says integer) and the listing failed
inside timedelta with a traceback in the log. The dispatcher's shape check deliberately leaves scalar-for-scalar
mismatches alone, and several handlers compare, add or test such a value as it arrives. The dispatcher now converts a
text value for an argument whose schema declares exactly "integer", "number" or "boolean" when it parses and meets the
schema's enum / minimum / maximum; anything else is passed on exactly as sent.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

import pytest

from nous.api.tools import _RECALL_DEEP_SCHEMA, ToolDispatcher, create_nous_tools

_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "hours": {"type": "integer"},
        "limit": {"type": "integer", "minimum": 1, "maximum": 50},
        "ratio": {"type": "number", "minimum": 0.0, "maximum": 1.0},
        "size": {"type": "number"},
        "flag": {"type": "boolean"},
        "priority": {"type": "integer", "enum": [0, 1]},
        "name": {"type": "string"},
        "either": {"type": ["integer", "null"]},
        "anything": {},
        "_count": {"type": "integer"},
    },
    "required": [],
}


def _dispatcher(tool: str = "probe") -> tuple[ToolDispatcher, dict[str, Any]]:
    dispatcher = ToolDispatcher()
    received: dict[str, Any] = {}

    async def probe(**kwargs: Any) -> dict[str, Any]:
        received.update(kwargs)
        return {"content": [{"type": "text", "text": "ok"}]}

    dispatcher.register(tool, probe, _SCHEMA)
    return dispatcher, received


async def _received(args: dict[str, Any], tool: str = "probe") -> dict[str, Any]:
    dispatcher, received = _dispatcher(tool)
    text, is_error = await dispatcher.dispatch(tool, args)
    assert (text, is_error) == ("ok", False)
    return received


class TestTextIsConverted:
    @pytest.mark.asyncio
    async def test_text_integer_number_and_boolean_reach_the_handler_converted(self):
        received = await _received({"hours": "48", "ratio": "0.5", "flag": "false"})
        assert received["hours"] == 48 and type(received["hours"]) is int
        assert received["ratio"] == 0.5 and type(received["ratio"]) is float
        assert received["flag"] is False

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("key", "sent", "expected"),
        [
            ("hours", " 48 ", 48),  # surrounding white space
            ("hours", "+5", 5),
            ("hours", "-3", -3),
            ("hours", "1_000", 1000),  # Python's int() reads the underscore
            ("hours", "\uff14\uff18", 48),  # and digits of other scripts
            ("flag", "TRUE", True),
            ("flag", " true ", True),
            ("flag", "False", False),
            ("size", "1e3", 1000.0),  # an exponent is a JSON number
            ("size", "48", 48.0),
            ("ratio", "1e-1", 0.1),
            ("priority", "1", 1),  # in the enum
            ("limit", "50", 50),  # at the maximum
        ],
    )
    async def test_what_counts_as_parsing(self, key, sent, expected):
        received = await _received({key: sent})
        assert received[key] == expected and type(received[key]) is type(expected)

    def test_repaired_args_shows_the_converted_value(self):
        """The runner derives a send's idempotency key from repaired_args: it must see what the handler sees."""
        dispatcher, _ = _dispatcher()
        assert dispatcher.repaired_args("probe", {"hours": "48", "name": "n"}) == {"hours": 48, "name": "n"}

    @pytest.mark.asyncio
    async def test_the_handler_receives_exactly_the_repaired_args_and_the_input_stays_as_sent(self):
        """The salvage precedent: the call's input (what the runner's ledger and tool-call record keep) is the
        model's own; the handler and repaired_args get the repaired copy."""
        dispatcher, received = _dispatcher()
        args = {"hours": "48", "flag": "true", "name": "7"}
        expected = dispatcher.repaired_args("probe", args)
        await dispatcher.dispatch("probe", args)
        assert received == expected == {"hours": 48, "flag": True, "name": "7"}
        assert args == {"hours": "48", "flag": "true", "name": "7"}  # the caller's dict is not modified


class TestEverythingElseIsLeftAlone:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("key", "sent"),
        [
            ("hours", "two days"),
            ("hours", "48.0"),  # int() does not read a decimal point: left for the handler
            ("hours", ""),
            ("hours", "   "),
            ("hours", "0x10"),
            ("hours", "1e3"),  # an exponent is not an integer literal
            ("size", "0x10"),
            ("size", "nan"),  # not finite
            ("size", "inf"),
            ("size", "Infinity"),
            ("flag", "yes"),
            ("flag", "1"),
            ("flag", "0"),
            ("flag", ""),
            ("limit", "0"),  # below the minimum
            ("limit", "100"),  # above the maximum
            ("ratio", "1.5"),
            ("priority", "2"),  # not in the enum
        ],
    )
    async def test_text_that_does_not_parse_or_fails_a_constraint_passes_unchanged(self, key, sent):
        received = await _received({key: sent})
        assert received[key] == sent and type(received[key]) is str

    @pytest.mark.asyncio
    async def test_a_string_argument_holding_digits_is_untouched(self):
        assert (await _received({"name": "48"}))["name"] == "48"

    @pytest.mark.asyncio
    async def test_values_that_already_have_a_json_type_are_untouched(self):
        received = await _received({"hours": 48, "size": 1, "ratio": 0.5, "flag": True, "limit": 100})
        assert type(received["size"]) is int and received["size"] == 1  # an integer is a number: not made a float
        assert received["flag"] is True and received["limit"] == 100  # out of range, but not text: not ours
        assert (received["hours"], received["ratio"]) == (48, 0.5)

    @pytest.mark.asyncio
    async def test_a_boolean_for_an_integer_stays_a_boolean(self):
        received = await _received({"hours": True})
        assert received["hours"] is True

    @pytest.mark.asyncio
    async def test_union_and_untyped_arguments_are_untouched(self):
        received = await _received({"either": "48", "anything": "48", "undeclared": "48"})
        assert received == {"either": "48", "anything": "48", "undeclared": "48"}

    def test_keys_starting_with_an_underscore_are_never_converted(self):
        dispatcher, _ = _dispatcher()
        assert dispatcher.repaired_args("probe", {"_count": "48"}) == {"_count": "48"}

    @pytest.mark.asyncio
    async def test_containers_are_still_refused_by_the_shape_check(self):
        dispatcher, received = _dispatcher()
        text, is_error = await dispatcher.dispatch("probe", {"hours": ["48"]})
        assert is_error is True and "hours must be integer, got array" in text
        assert not received

    @pytest.mark.asyncio
    async def test_an_outside_servers_tool_gets_its_arguments_as_sent(self):
        """An MCP client tool's schema is the outside server's, and the server checks its own arguments."""
        received = await _received({"hours": "48", "flag": "false"}, tool="mcp__srv__probe")
        assert received == {"hours": "48", "flag": "false"}


# ---------------------------------------------------------------------------
# recall_deep (the observed call), against the real database
# ---------------------------------------------------------------------------


def _recall_settings() -> MagicMock:
    s = MagicMock()
    s.episode_chunks_enabled = False
    s.recall_include_parent_episodes = False
    s.decision_resolution_enabled = True
    return s


@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_recall_deep_lists_episodes_when_hours_arrives_as_text(db, mock_embeddings):
    from nous.config import Settings
    from nous.heart import Heart
    from nous.storage.models import Episode

    agent_id = f"test-toolargs-{uuid.uuid4().hex[:8]}"
    heart = Heart(db, Settings().model_copy(update={"agent_id": agent_id}), embedding_provider=mock_embeddings)
    now = datetime.now(UTC)
    episode_id = uuid.uuid4()
    async with db.session() as session:
        session.add(
            Episode(
                id=episode_id,
                agent_id=agent_id,
                title="Text hours probe",
                summary="an ended conversation",
                active=False,
                started_at=now - timedelta(hours=2),
                ended_at=now - timedelta(hours=1),
                outcome="success",
                tags=[],
            )
        )
        await session.commit()
    try:
        dispatcher = ToolDispatcher()
        brain = MagicMock()
        brain.agent_id = agent_id
        dispatcher.register(
            "recall_deep",
            create_nous_tools(brain, heart, settings=_recall_settings())["recall_deep"],
            _RECALL_DEEP_SCHEMA,
        )
        text, is_error = await dispatcher.dispatch("recall_deep", {"hours": "48", "limit": "5"})
        assert is_error is False, text
        assert text.startswith("Recent episodes (last 48h):") and "Text hours probe" in text
    finally:
        from sqlalchemy import delete

        async with db.session() as session:
            await session.execute(delete(Episode).where(Episode.agent_id == agent_id))
            await session.commit()
        await heart.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("args", "named"),
    [
        ({"hours": "soon"}, ["hours"]),
        ({"hours": "48.0"}, ["hours"]),
        ({"query": "deploy", "limit": "ten"}, ["limit"]),
        ({"query": "deploy", "limit": "100"}, ["limit"]),  # out of range: not converted, so still text
        ({"hours": "soon", "limit": "ten"}, ["hours", "limit"]),
    ],
)
async def test_recall_deep_answers_a_clear_error_for_text_that_is_not_a_number(args, named, caplog):
    brain, heart = MagicMock(), MagicMock()
    brain.agent_id = "test"
    dispatcher = ToolDispatcher()
    dispatcher.register(
        "recall_deep",
        create_nous_tools(brain, heart, settings=_recall_settings())["recall_deep"],
        _RECALL_DEEP_SCHEMA,
    )
    with caplog.at_level(logging.DEBUG):
        text, is_error = await dispatcher.dispatch("recall_deep", args)
    assert is_error is True
    assert text.startswith("recall_deep: ")
    for key in named:
        assert f"{key} must be a number" in text
    assert "got string" in text
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
    heart.list_episodes.assert_not_called()


@pytest.mark.asyncio
async def test_recall_deep_keeps_a_real_fractional_hours_working():
    """hours=1.5 (a real number) worked before the guard and still does: only text that is not a number stops."""
    brain, heart = MagicMock(), MagicMock()
    brain.agent_id = "test"

    async def list_episodes(**_: Any) -> list:
        return []

    heart.list_episodes = list_episodes
    recall_deep = create_nous_tools(brain, heart, settings=_recall_settings())["recall_deep"]
    result = await recall_deep(hours=1.5)
    assert not result.get("is_error")
    assert "No episodes found in the last 1.5 hours." in result["content"][0]["text"]
