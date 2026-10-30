"""cancel_task accepts an id prefix as well as a full id.

The short ids that notices and logs print (8 hex characters) used to fail as "Invalid task ID". A prefix is
resolved first, against this agent's pending or running subtasks and active schedules: one match is cancelled
by its full id, more than one is an error that lists them, none is the usual "No pending subtask or active
schedule" error. Then the id is parsed, then the F099 lineage check runs on the resolved id. The same rules
through the real dispatcher and stores are in test_f099_authority.py.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from test_f099_report_claims import _verify

from nous.api.tools import _CANCEL_TASK_SCHEMA, TASK_ID_PREFIX_MIN, _is_task_id_prefix, create_subtask_tools
from nous.cognitive.claim_verifier import Evidence
from nous.cognitive.ledger_store import durable_key_args
from nous.config import Settings

FULL = "3745ab2c-1111-4222-8333-444455556666"
OTHER = "3745ab2c-9999-4222-8333-444455556666"


def _heart(*, subtasks=(), schedules=()):
    heart = MagicMock()
    heart.subtasks = AsyncMock()
    heart.subtasks.find_cancellable_by_id_prefix = AsyncMock(return_value=list(subtasks))
    heart.subtasks.cancel = AsyncMock(return_value=True)
    heart.schedules = AsyncMock()
    heart.schedules.find_active_by_id_prefix = AsyncMock(return_value=list(schedules))
    heart.schedules.get = AsyncMock(return_value=None)
    heart.schedules.deactivate = AsyncMock()
    return heart


def _row(task_id: str, task: str = "check the snow report"):
    return SimpleNamespace(id=uuid.UUID(task_id), task=task)


async def _cancel_with(heart, task_id: str, **extra):
    tools = create_subtask_tools(heart, Settings(_env_file=None))
    return await tools["cancel_task"](task_id=task_id, **extra)


def _text(result) -> str:
    return result["content"][0]["text"]


@pytest.mark.parametrize(
    ("task_id", "is_prefix"),
    [
        ("3745ab2c", True),
        ("3745AB2C", True),
        ("3745ab2c-11", True),
        ("3745ab2c1111", True),
        ("3745ab2", False),  # shorter than the minimum
        ("3745ab2g", False),  # not hex
        ("3745ab2c%", False),  # a LIKE wildcard never reads as a prefix
        (FULL, False),  # a whole UUID
        (uuid.UUID(FULL).hex, False),  # a whole UUID without hyphens
        ("not-a-uuid", False),
    ],
)
def test_what_reads_as_a_prefix(task_id, is_prefix):
    assert _is_task_id_prefix(task_id) is is_prefix


async def test_a_unique_subtask_prefix_cancels_it_by_its_full_id():
    heart = _heart(subtasks=[_row(FULL)])
    result = await _cancel_with(heart, "3745ab2c")
    assert not result.get("is_error"), result
    heart.subtasks.find_cancellable_by_id_prefix.assert_awaited_once_with("3745ab2c")
    heart.subtasks.cancel.assert_awaited_once_with(uuid.UUID(FULL))
    assert _text(result) == f"Subtask {FULL} cancelled."


async def test_a_unique_schedule_prefix_deactivates_it_by_its_full_id():
    heart = _heart(schedules=[_row(FULL)])
    heart.subtasks.cancel = AsyncMock(return_value=False)
    heart.schedules.get = AsyncMock(return_value=_row(FULL))
    result = await _cancel_with(heart, "3745ab2c")
    assert not result.get("is_error"), result
    heart.schedules.deactivate.assert_awaited_once_with(uuid.UUID(FULL))
    assert _text(result) == f"Schedule {FULL} deactivated."


async def test_an_ambiguous_prefix_lists_the_matches_and_cancels_nothing():
    long_task = "x" * 100
    heart = _heart(subtasks=[_row(FULL, long_task)], schedules=[_row(OTHER, "daily digest")])
    result = await _cancel_with(heart, "3745ab2c")
    assert result.get("is_error") is True
    text = _text(result)
    assert "matches 2 tasks" in text
    assert f"- {FULL}: {'x' * 60}\n" in text and "x" * 61 not in text
    assert f"- {OTHER}: daily digest" in text
    heart.subtasks.cancel.assert_not_awaited()
    heart.schedules.deactivate.assert_not_awaited()


async def test_an_unknown_prefix_is_the_usual_not_found_error():
    heart = _heart()
    result = await _cancel_with(heart, "3745ab2c")
    assert result.get("is_error") is True
    assert _text(result) == "No pending subtask or active schedule found for 3745ab2c."
    heart.subtasks.cancel.assert_not_awaited()


@pytest.mark.parametrize("task_id", [FULL, uuid.UUID(FULL).hex])
async def test_a_full_id_is_not_looked_up_as_a_prefix(task_id):
    heart = _heart()
    result = await _cancel_with(heart, task_id)
    assert not result.get("is_error"), result
    heart.subtasks.find_cancellable_by_id_prefix.assert_not_awaited()
    heart.subtasks.cancel.assert_awaited_once_with(uuid.UUID(FULL))


@pytest.mark.parametrize("task_id", ["3745ab2", "not-a-uuid", "3745ab2c%"])
async def test_anything_else_is_still_an_invalid_task_id(task_id):
    heart = _heart(subtasks=[_row(FULL)])
    result = await _cancel_with(heart, task_id)
    assert result.get("is_error") is True and _text(result) == f"Invalid task ID: {task_id}"
    heart.subtasks.find_cancellable_by_id_prefix.assert_not_awaited()
    heart.subtasks.cancel.assert_not_awaited()


async def test_the_lineage_check_runs_on_the_resolved_id():
    """An internal_only turn's prefix is resolved before the F099 lineage lookup, which gets the full id."""
    heart = _heart(subtasks=[_row(FULL)])
    root = uuid.uuid4()
    heart.intentions = MagicMock()
    heart.intentions.get_for_source = AsyncMock(return_value=SimpleNamespace(root_id=uuid.uuid4()))
    result = await _cancel_with(heart, "3745ab2c", _authority="internal_only", _root_intention_id=str(root))
    assert result.get("is_error") is True and "is not part of this lineage" in _text(result)
    assert heart.intentions.get_for_source.await_args.args[1] == uuid.UUID(FULL)
    heart.subtasks.cancel.assert_not_awaited()


def test_the_description_states_the_prefix_rule():
    assert f"{TASK_ID_PREFIX_MIN}+ characters" in _CANCEL_TASK_SCHEMA["description"]
    assert f"first {TASK_ID_PREFIX_MIN}+ characters" in _CANCEL_TASK_SCHEMA["properties"]["task_id"]["description"]


# -- the durable ledger and the claim verifier --------------------------------


def test_the_ledger_keeps_a_prefix_instead_of_hashing_it():
    assert durable_key_args("cancel_task", {"task_id": "3745AB2C"}) == {"task_id": "3745ab2c"}
    assert durable_key_args("cancel_task", {"task_id": FULL}) == {"task_id": FULL}
    hashed = durable_key_args("cancel_task", {"task_id": "sk-ABCDEFGHIJKLMNOP"})
    assert "task_id" not in hashed and "task_id_sha256" in hashed


@pytest.mark.parametrize(
    ("text", "kept", "verified"),
    [
        (f"I cancelled task {FULL}.", "3745ab2c", True),
        ("I cancelled task 3745ab2c.", FULL, True),
        ("I cancelled task 9a9a9a9a.", "3745ab2c", False),
    ],
)
def test_a_prefix_cancel_backs_a_claim_about_its_task(text, kept, verified):
    assert _verify(text, [Evidence("cancel_task", {"task_id": kept})]).verified is verified
