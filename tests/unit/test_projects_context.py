"""Tests for ProjectContextInjector — Active Projects block (F047 Phase 1).

Tests use the SAVEPOINT fixture from tests/conftest.py.
"""

from datetime import datetime, timezone

from nous.projects.context import ProjectContextInjector, _relative_time
from nous.projects.schemas import ProjectInput, ProjectNoteInput, ProjectUpdateInput


# ---------------------------------------------------------------------------
# _relative_time helper
# ---------------------------------------------------------------------------


def test_relative_time_just_now():
    """Times within 60 seconds show 'just now'."""
    now = datetime.now(timezone.utc)
    assert _relative_time(now) == "just now"


def test_relative_time_minutes():
    """Times within an hour show minutes."""
    from datetime import timedelta
    t = datetime.now(timezone.utc) - timedelta(minutes=15)
    result = _relative_time(t)
    assert "m ago" in result


def test_relative_time_hours():
    """Times within a day show hours."""
    from datetime import timedelta
    t = datetime.now(timezone.utc) - timedelta(hours=3)
    result = _relative_time(t)
    assert "h ago" in result


def test_relative_time_days():
    """Times beyond a day show days."""
    from datetime import timedelta
    t = datetime.now(timezone.utc) - timedelta(days=5)
    result = _relative_time(t)
    assert "5d ago" in result


# ---------------------------------------------------------------------------
# Context block building
# ---------------------------------------------------------------------------


async def test_build_context_empty(registry, session):
    """No active projects returns None."""
    injector = ProjectContextInjector(registry=registry)
    block = await injector.build_context_block(session=session)
    assert block is None


async def test_build_context_single_project(registry, session):
    """Single active project appears in context block."""
    await registry.register(
        ProjectInput(
            name="F047-ctx",
            title="Context Test",
            description="Testing context injection",
            priority=0.8,
        ),
        session=session,
    )
    injector = ProjectContextInjector(registry=registry)
    block = await injector.build_context_block(session=session)

    assert block is not None
    assert "F047-ctx" in block
    assert "active" in block
    assert "0.8" in block
    assert "Testing context injection" in block


async def test_build_context_includes_recent_event(registry, session):
    """Context block includes a recent event summary."""
    await registry.register(
        ProjectInput(name="F047-evt", title="Event Test"),
        session=session,
    )
    await registry.add_note(
        "F047-evt",
        ProjectNoteInput(summary="Phase 1 shipped"),
        session=session,
    )

    injector = ProjectContextInjector(registry=registry)
    block = await injector.build_context_block(session=session)

    assert block is not None
    # Should include at least one event (either the note or the created event)
    assert "Last event:" in block
    # The block should mention the project
    assert "F047-evt" in block


async def test_build_context_multiple_projects(registry, session):
    """Multiple active projects all appear."""
    for i in range(3):
        await registry.register(
            ProjectInput(
                name=f"F047-multi-{i}",
                title=f"Multi {i}",
                priority=0.5 + i * 0.1,
            ),
            session=session,
        )

    injector = ProjectContextInjector(registry=registry, max_projects=5)
    block = await injector.build_context_block(session=session)

    assert block is not None
    for i in range(3):
        assert f"F047-multi-{i}" in block


async def test_build_context_excludes_paused(registry, session):
    """Paused projects do not appear in context."""
    await registry.register(
        ProjectInput(name="F047-paused-ctx", title="Paused Ctx"),
        session=session,
    )
    await registry.update(
        "F047-paused-ctx",
        ProjectUpdateInput(status="paused"),
        session=session,
    )

    injector = ProjectContextInjector(registry=registry)
    block = await injector.build_context_block(session=session)

    # Should be None since the only project is paused
    assert block is None


async def test_build_context_respects_limit(registry, session):
    """Context respects max_projects limit."""
    for i in range(10):
        await registry.register(
            ProjectInput(name=f"F047-lim-ctx-{i}", title=f"Limit {i}"),
            session=session,
        )

    injector = ProjectContextInjector(registry=registry, max_projects=3)
    block = await injector.build_context_block(session=session)

    assert block is not None
    # Count bullet points
    bullets = [line for line in block.split("\n") if line.startswith("- **")]
    # Should have at most 3 project bullets (maybe +1 for the "more" line)
    assert len(bullets) <= 4


async def test_build_context_truncates_description(registry, session):
    """Long descriptions are truncated."""
    long_desc = "A " * 200  # 400 chars
    await registry.register(
        ProjectInput(
            name="F047-long-desc",
            title="Long Description",
            description=long_desc,
        ),
        session=session,
    )

    injector = ProjectContextInjector(registry=registry)
    block = await injector.build_context_block(session=session)

    assert block is not None
    # Description should be truncated with ellipsis
    assert "..." in block


async def test_build_context_priority_ordering(registry, session):
    """Higher priority projects appear first."""
    await registry.register(
        ProjectInput(name="F047-low-pri", title="Low", priority=0.1),
        session=session,
    )
    await registry.register(
        ProjectInput(name="F047-high-pri", title="High", priority=0.9),
        session=session,
    )

    injector = ProjectContextInjector(registry=registry)
    block = await injector.build_context_block(session=session)

    assert block is not None
    # High priority should appear before low priority
    high_pos = block.index("F047-high-pri")
    low_pos = block.index("F047-low-pri")
    assert high_pos < low_pos
