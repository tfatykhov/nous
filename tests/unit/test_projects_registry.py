"""Tests for ProjectRegistry — CRUD operations (F047 Phase 1).

Tests use the SAVEPOINT fixture from tests/conftest.py.
"""

import uuid

import pytest

from nous.projects.registry import ProjectRegistry
from nous.projects.schemas import (
    ProjectDetail,
    ProjectEventDetail,
    ProjectInput,
    ProjectNoteInput,
    ProjectUpdateInput,
)


# ---------------------------------------------------------------------------
# register
# ---------------------------------------------------------------------------


async def test_register_creates_project(registry, session):
    """Registering a project creates it with correct fields."""
    inp = ProjectInput(
        name="F047-test",
        title="Test Project",
        description="A test project for unit testing",
        priority=0.7,
        tags=["test", "f047"],
    )
    detail = await registry.register(inp, session=session)

    assert isinstance(detail, ProjectDetail)
    assert detail.name == "F047-test"
    assert detail.title == "Test Project"
    assert detail.description == "A test project for unit testing"
    assert detail.priority == 0.7
    assert detail.status == "active"
    assert detail.tags == ["test", "f047"]
    assert detail.id is not None


async def test_register_creates_event(registry, session):
    """Registering a project also creates a 'created' event."""
    inp = ProjectInput(name="F047-events", title="Events Test")
    detail = await registry.register(inp, session=session)

    assert len(detail.recent_events) == 1
    event = detail.recent_events[0]
    assert event.event_type == "created"
    assert "Events Test" in event.summary


async def test_register_duplicate_name_raises(registry, session):
    """Registering a project with a duplicate name raises ValueError."""
    inp = ProjectInput(name="F047-dup", title="First")
    await registry.register(inp, session=session)

    with pytest.raises(ValueError, match="already exists"):
        await registry.register(
            ProjectInput(name="F047-dup", title="Second"),
            session=session,
        )


async def test_register_default_priority(registry, session):
    """Default priority is 0.5."""
    inp = ProjectInput(name="F047-default", title="Default Priority")
    detail = await registry.register(inp, session=session)
    assert detail.priority == 0.5


# ---------------------------------------------------------------------------
# update
# ---------------------------------------------------------------------------


async def test_update_status(registry, session):
    """Updating status changes it and logs a status_change event."""
    inp = ProjectInput(name="F047-upd-status", title="Status Test")
    detail = await registry.register(inp, session=session)
    assert detail.status == "active"

    updated = await registry.update(
        "F047-upd-status",
        ProjectUpdateInput(status="paused"),
        session=session,
    )
    assert updated.status == "paused"

    # Should have a status_change event
    status_events = [e for e in updated.recent_events if e.event_type == "status_change"]
    assert len(status_events) >= 1
    assert "paused" in status_events[0].summary


async def test_update_priority(registry, session):
    """Updating priority changes it."""
    inp = ProjectInput(name="F047-upd-pri", title="Priority Test", priority=0.3)
    await registry.register(inp, session=session)

    updated = await registry.update(
        "F047-upd-pri",
        ProjectUpdateInput(priority=0.9),
        session=session,
    )
    assert updated.priority == 0.9


async def test_update_description(registry, session):
    """Updating description changes it."""
    inp = ProjectInput(name="F047-upd-desc", title="Desc Test", description="old")
    await registry.register(inp, session=session)

    updated = await registry.update(
        "F047-upd-desc",
        ProjectUpdateInput(description="new description"),
        session=session,
    )
    assert updated.description == "new description"


async def test_update_not_found_raises(registry, session):
    """Updating a non-existent project raises ValueError."""
    with pytest.raises(ValueError, match="not found"):
        await registry.update(
            "nonexistent-project",
            ProjectUpdateInput(status="paused"),
            session=session,
        )


async def test_update_by_uuid(registry, session):
    """Can update by UUID string."""
    inp = ProjectInput(name="F047-uuid-upd", title="UUID Update Test")
    detail = await registry.register(inp, session=session)

    updated = await registry.update(
        str(detail.id),
        ProjectUpdateInput(title="Updated Title"),
        session=session,
    )
    assert updated.title == "Updated Title"


# ---------------------------------------------------------------------------
# add_note
# ---------------------------------------------------------------------------


async def test_add_note(registry, session):
    """Adding a note creates an event."""
    inp = ProjectInput(name="F047-note", title="Note Test")
    await registry.register(inp, session=session)

    event = await registry.add_note(
        "F047-note",
        ProjectNoteInput(summary="Phase 1 shipped"),
        session=session,
    )
    assert isinstance(event, ProjectEventDetail)
    assert event.event_type == "note"
    assert event.summary == "Phase 1 shipped"


async def test_add_milestone(registry, session):
    """Adding a milestone event works."""
    inp = ProjectInput(name="F047-milestone", title="Milestone Test")
    await registry.register(inp, session=session)

    event = await registry.add_note(
        "F047-milestone",
        ProjectNoteInput(summary="PR merged", event_type="milestone"),
        session=session,
    )
    assert event.event_type == "milestone"


async def test_add_note_not_found_raises(registry, session):
    """Adding a note to a non-existent project raises ValueError."""
    with pytest.raises(ValueError, match="not found"):
        await registry.add_note(
            "nonexistent",
            ProjectNoteInput(summary="test"),
            session=session,
        )


# ---------------------------------------------------------------------------
# list_projects
# ---------------------------------------------------------------------------


async def test_list_active_projects(registry, session):
    """Listing active projects returns only active ones."""
    await registry.register(
        ProjectInput(name="F047-list-a", title="Active 1"),
        session=session,
    )
    await registry.register(
        ProjectInput(name="F047-list-b", title="Active 2"),
        session=session,
    )
    # Create and pause one
    await registry.register(
        ProjectInput(name="F047-list-p", title="Paused"),
        session=session,
    )
    await registry.update(
        "F047-list-p",
        ProjectUpdateInput(status="paused"),
        session=session,
    )

    active = await registry.list_projects(status="active", session=session)
    active_names = {p.name for p in active}
    assert "F047-list-a" in active_names
    assert "F047-list-b" in active_names
    assert "F047-list-p" not in active_names


async def test_list_all_projects(registry, session):
    """Listing with status=None returns all projects."""
    await registry.register(
        ProjectInput(name="F047-all-1", title="All 1"),
        session=session,
    )
    await registry.register(
        ProjectInput(name="F047-all-2", title="All 2"),
        session=session,
    )
    await registry.update(
        "F047-all-2",
        ProjectUpdateInput(status="completed"),
        session=session,
    )

    all_projects = await registry.list_projects(status=None, session=session)
    all_names = {p.name for p in all_projects}
    assert "F047-all-1" in all_names
    assert "F047-all-2" in all_names


async def test_list_respects_limit(registry, session):
    """Listing with limit caps the results."""
    for i in range(5):
        await registry.register(
            ProjectInput(name=f"F047-lim-{i}", title=f"Limit {i}"),
            session=session,
        )

    projects = await registry.list_projects(status="active", limit=3, session=session)
    assert len(projects) <= 3


# ---------------------------------------------------------------------------
# get
# ---------------------------------------------------------------------------


async def test_get_by_name(registry, session):
    """Get a project by name."""
    inp = ProjectInput(name="F047-get", title="Get Test")
    created = await registry.register(inp, session=session)

    fetched = await registry.get("F047-get", session=session)
    assert fetched is not None
    assert fetched.id == created.id
    assert fetched.name == "F047-get"


async def test_get_by_uuid(registry, session):
    """Get a project by UUID string."""
    inp = ProjectInput(name="F047-get-uuid", title="UUID Get")
    created = await registry.register(inp, session=session)

    fetched = await registry.get(str(created.id), session=session)
    assert fetched is not None
    assert fetched.name == "F047-get-uuid"


async def test_get_not_found(registry, session):
    """Get returns None for non-existent project."""
    fetched = await registry.get("nonexistent", session=session)
    assert fetched is None


# ---------------------------------------------------------------------------
# touch
# ---------------------------------------------------------------------------


async def test_touch_updates_timestamp(registry, session):
    """Touch updates last_touched_at."""
    inp = ProjectInput(name="F047-touch", title="Touch Test")
    detail = await registry.register(inp, session=session)

    original_touched = detail.last_touched_at
    await registry.touch(detail.id, session=session)

    fetched = await registry.get("F047-touch", session=session)
    assert fetched is not None
    # The timestamp should be >= original (may be equal in fast tests)
    assert fetched.last_touched_at >= original_touched


# ---------------------------------------------------------------------------
# list_active_for_context
# ---------------------------------------------------------------------------


async def test_list_active_for_context(registry, session):
    """Context listing returns active projects ordered by priority."""
    await registry.register(
        ProjectInput(name="F047-ctx-low", title="Low", priority=0.3),
        session=session,
    )
    await registry.register(
        ProjectInput(name="F047-ctx-high", title="High", priority=0.9),
        session=session,
    )
    await registry.register(
        ProjectInput(name="F047-ctx-paused", title="Paused", priority=1.0),
        session=session,
    )
    await registry.update(
        "F047-ctx-paused",
        ProjectUpdateInput(status="paused"),
        session=session,
    )

    context_projects = await registry.list_active_for_context(limit=5, session=session)
    names = [p.name for p in context_projects]

    # Paused should not appear
    assert "F047-ctx-paused" not in names
    # High priority should come first
    if len(names) >= 2:
        assert names[0] == "F047-ctx-high"
