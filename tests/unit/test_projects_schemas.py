"""Tests for F047 project schemas — Pydantic validation."""

import pytest
from pydantic import ValidationError

from nous.projects.schemas import (
    ProjectDetail,
    ProjectEventDetail,
    ProjectInput,
    ProjectNoteInput,
    ProjectUpdateInput,
)


# ---------------------------------------------------------------------------
# ProjectInput
# ---------------------------------------------------------------------------


def test_project_input_minimal():
    """Minimal ProjectInput requires name and title."""
    inp = ProjectInput(name="test", title="Test")
    assert inp.name == "test"
    assert inp.priority == 0.5
    assert inp.tags == []
    assert inp.description == ""


def test_project_input_full():
    """Full ProjectInput with all fields."""
    inp = ProjectInput(
        name="F047-full",
        title="Full Test",
        description="A test",
        priority=0.8,
        tags=["a", "b"],
    )
    assert inp.priority == 0.8
    assert inp.tags == ["a", "b"]


def test_project_input_priority_bounds():
    """Priority must be between 0.0 and 1.0."""
    with pytest.raises(ValidationError):
        ProjectInput(name="x", title="X", priority=-0.1)
    with pytest.raises(ValidationError):
        ProjectInput(name="x", title="X", priority=1.1)


# ---------------------------------------------------------------------------
# ProjectUpdateInput
# ---------------------------------------------------------------------------


def test_project_update_all_none():
    """Update with all None fields is valid (no-op update)."""
    inp = ProjectUpdateInput()
    assert inp.status is None
    assert inp.priority is None


def test_project_update_status():
    """Update with valid status."""
    inp = ProjectUpdateInput(status="completed")
    assert inp.status == "completed"


def test_project_update_invalid_status():
    """Update with invalid status raises."""
    with pytest.raises(ValidationError):
        ProjectUpdateInput(status="invalid")


def test_project_update_priority_bounds():
    """Priority update must be between 0.0 and 1.0."""
    with pytest.raises(ValidationError):
        ProjectUpdateInput(priority=2.0)


# ---------------------------------------------------------------------------
# ProjectNoteInput
# ---------------------------------------------------------------------------


def test_project_note_default_type():
    """Default event_type is 'note'."""
    inp = ProjectNoteInput(summary="test note")
    assert inp.event_type == "note"


def test_project_note_milestone():
    """Can create milestone event."""
    inp = ProjectNoteInput(summary="shipped", event_type="milestone")
    assert inp.event_type == "milestone"


def test_project_note_invalid_type():
    """Invalid event_type raises."""
    with pytest.raises(ValidationError):
        ProjectNoteInput(summary="test", event_type="invalid_type")
