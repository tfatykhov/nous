"""F061 PR-2: spawn_task schema + closure round-trip tests for the new fields.

Plan v2 §2.6 required this file. Verifies:
  - schema accepts ``output_format`` and ``success_criteria`` (both optional)
  - schema does NOT accept ``boundaries`` (intentionally not exposed —
    has no DB column; would silently drop)
  - the spawn_task closure persists the new fields via subtasks.create()
  - legacy callers (no new fields) continue to work
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from nous.api.tools import _SPAWN_TASK_SCHEMA, create_subtask_tools
from nous.config import Settings


class TestSpawnTaskSchema:
    """Schema-level checks — does not exercise the closure."""

    def test_output_format_field_present(self):
        props = _SPAWN_TASK_SCHEMA["properties"]
        assert "output_format" in props
        assert props["output_format"]["type"] == "string"

    def test_success_criteria_field_present(self):
        props = _SPAWN_TASK_SCHEMA["properties"]
        assert "success_criteria" in props
        assert props["success_criteria"]["type"] == "string"

    def test_boundaries_field_NOT_in_schema(self):
        """Per F061 PR-2 review: boundaries has no DB column or executor
        plumbing, so exposing it in the schema would silently drop the
        operator-supplied value. Excluded until follow-up PR adds storage.
        """
        assert "boundaries" not in _SPAWN_TASK_SCHEMA["properties"]

    def test_only_task_required(self):
        assert _SPAWN_TASK_SCHEMA["required"] == ["task"]


class TestSpawnTaskClosurePersistsNewFields:
    """The closure passes output_format and success_criteria through to
    subtasks.create(). Uses a mocked Heart so we don't touch the DB.
    """

    @pytest.fixture
    def mocks(self):
        heart = MagicMock()
        heart.subtasks = MagicMock()
        # Async create() returning a stub Subtask with the fields it was
        # asked to persist (so the closure can read them back if needed).
        async def _create(**kwargs):
            return MagicMock(
                id=uuid.uuid4(),
                task=kwargs.get("task"),
                output_format=kwargs.get("output_format"),
                success_criteria=kwargs.get("success_criteria"),
            )
        heart.subtasks.create = AsyncMock(side_effect=_create)
        heart.check_censors = AsyncMock(return_value=[])
        settings = Settings(
            _env_file=None,
            agent_id="test-spawn-schema",
            subtask_hardening_enabled=False,  # legacy path — no inline executor needed
            telegram_bot_token=None,
            telegram_chat_id=None,
        )
        closures = create_subtask_tools(heart, settings, runner=None)
        return heart, settings, closures

    @pytest.mark.asyncio
    async def test_persists_both_new_fields(self, mocks):
        heart, _settings, closures = mocks
        spawn_task = closures["spawn_task"]
        await spawn_task(
            task="research X",
            await_result=False,
            output_format="JSON-style summary.",
            success_criteria="Returns >= 3 candidates.",
        )
        heart.subtasks.create.assert_awaited_once()
        call_kwargs = heart.subtasks.create.await_args.kwargs
        assert call_kwargs["output_format"] == "JSON-style summary."
        assert call_kwargs["success_criteria"] == "Returns >= 3 candidates."
        # boundaries is NOT a kwarg — would TypeError if it were forwarded
        assert "boundaries" not in call_kwargs

    @pytest.mark.asyncio
    async def test_legacy_call_omits_new_fields(self, mocks):
        """Backward compat: callers (task_scheduler, MCP without new fields)
        work unchanged. New fields default to None and are persisted as NULL.
        """
        heart, _settings, closures = mocks
        spawn_task = closures["spawn_task"]
        await spawn_task(task="legacy call", await_result=False)
        call_kwargs = heart.subtasks.create.await_args.kwargs
        assert call_kwargs["output_format"] is None
        assert call_kwargs["success_criteria"] is None

    @pytest.mark.asyncio
    async def test_only_output_format_provided(self, mocks):
        heart, _settings, closures = mocks
        spawn_task = closures["spawn_task"]
        await spawn_task(
            task="t",
            await_result=False,
            output_format="Free-form prose.",
        )
        call_kwargs = heart.subtasks.create.await_args.kwargs
        assert call_kwargs["output_format"] == "Free-form prose."
        assert call_kwargs["success_criteria"] is None
