"""Tests for F047 project tools — tool handler functions.

Tests use the SAVEPOINT fixture from tests/conftest.py.
"""

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from nous.api.tools import ToolDispatcher, register_project_tools
from nous.projects.registry import ProjectRegistry
from nous.projects.schemas import ProjectInput


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def dispatcher():
    """Fresh ToolDispatcher for testing."""
    return ToolDispatcher(tool_schema_cache_enabled=False)


@pytest.fixture
def registered_dispatcher(dispatcher, registry):
    """Dispatcher with project tools registered."""
    register_project_tools(dispatcher, registry)
    return dispatcher


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_tools_registered(registered_dispatcher):
    """All four project tools are registered."""
    assert "project_register" in registered_dispatcher._handlers
    assert "project_update" in registered_dispatcher._handlers
    assert "project_note" in registered_dispatcher._handlers
    assert "project_list" in registered_dispatcher._handlers


def test_schemas_registered(registered_dispatcher):
    """All four tool schemas are registered."""
    assert "project_register" in registered_dispatcher._schemas
    assert "project_update" in registered_dispatcher._schemas
    assert "project_note" in registered_dispatcher._schemas
    assert "project_list" in registered_dispatcher._schemas


# ---------------------------------------------------------------------------
# project_register tool
# ---------------------------------------------------------------------------


async def test_tool_project_register(registered_dispatcher, session, registry):
    """project_register tool creates a project and returns info."""
    result, is_error = await registered_dispatcher.dispatch(
        "project_register",
        {"name": "F047-tool-test", "title": "Tool Test", "description": "testing tools"},
    )
    assert not is_error
    assert "F047-tool-test" in result
    assert "Tool Test" in result

    # Verify project was actually created
    project = await registry.get("F047-tool-test", session=session)
    assert project is not None


async def test_tool_project_register_duplicate(registered_dispatcher, registry):
    """project_register tool returns error for duplicate name."""
    await registered_dispatcher.dispatch(
        "project_register",
        {"name": "F047-dup-tool", "title": "First"},
    )
    result, is_error = await registered_dispatcher.dispatch(
        "project_register",
        {"name": "F047-dup-tool", "title": "Second"},
    )
    assert "Error" in result or "already exists" in result


# ---------------------------------------------------------------------------
# project_update tool
# ---------------------------------------------------------------------------


async def test_tool_project_update(registered_dispatcher, registry):
    """project_update tool updates status."""
    await registered_dispatcher.dispatch(
        "project_register",
        {"name": "F047-upd-tool", "title": "Update Tool"},
    )
    result, is_error = await registered_dispatcher.dispatch(
        "project_update",
        {"name_or_id": "F047-upd-tool", "status": "paused"},
    )
    assert not is_error
    assert "paused" in result.lower()


async def test_tool_project_update_not_found(registered_dispatcher):
    """project_update returns error for non-existent project."""
    result, is_error = await registered_dispatcher.dispatch(
        "project_update",
        {"name_or_id": "nonexistent", "status": "paused"},
    )
    assert "Error" in result or "not found" in result


# ---------------------------------------------------------------------------
# project_note tool
# ---------------------------------------------------------------------------


async def test_tool_project_note(registered_dispatcher, registry):
    """project_note tool adds a note."""
    await registered_dispatcher.dispatch(
        "project_register",
        {"name": "F047-note-tool", "title": "Note Tool"},
    )
    result, is_error = await registered_dispatcher.dispatch(
        "project_note",
        {"name_or_id": "F047-note-tool", "summary": "Phase 1 done"},
    )
    assert not is_error
    assert "Phase 1 done" in result


async def test_tool_project_note_milestone(registered_dispatcher, registry):
    """project_note with event_type=milestone."""
    await registered_dispatcher.dispatch(
        "project_register",
        {"name": "F047-ms-tool", "title": "Milestone Tool"},
    )
    result, is_error = await registered_dispatcher.dispatch(
        "project_note",
        {"name_or_id": "F047-ms-tool", "summary": "PR merged", "event_type": "milestone"},
    )
    assert not is_error
    assert "milestone" in result


# ---------------------------------------------------------------------------
# project_list tool
# ---------------------------------------------------------------------------


async def test_tool_project_list_empty(registered_dispatcher):
    """project_list returns empty message when no projects."""
    result, is_error = await registered_dispatcher.dispatch(
        "project_list",
        {},
    )
    assert not is_error
    assert "No projects" in result or "0" in result


async def test_tool_project_list_with_projects(registered_dispatcher, registry):
    """project_list shows registered projects."""
    await registered_dispatcher.dispatch(
        "project_register",
        {"name": "F047-list-tool-1", "title": "List 1"},
    )
    await registered_dispatcher.dispatch(
        "project_register",
        {"name": "F047-list-tool-2", "title": "List 2"},
    )
    result, is_error = await registered_dispatcher.dispatch(
        "project_list",
        {"status": "active"},
    )
    assert not is_error
    assert "F047-list-tool-1" in result
    assert "F047-list-tool-2" in result
