"""Tests for ProjectResolver — pre-turn project matcher (F047 Phase 1).

Tests use the SAVEPOINT fixture from tests/conftest.py.
"""

from nous.projects.registry import ProjectRegistry
from nous.projects.resolver import ProjectResolver
from nous.projects.schemas import ProjectInput


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


async def _setup_projects(registry, session):
    """Create a set of test projects."""
    await registry.register(
        ProjectInput(
            name="F047-goal-project-registry",
            title="Goal Project Registry",
            description="Give Nous persistent project tracking",
            tags=["registry", "memory"],
        ),
        session=session,
    )
    await registry.register(
        ProjectInput(
            name="F041-snn-sleep",
            title="SNN Sleep Densification",
            description="Sleep-phase graph densification from tinyHippo",
            tags=["sleep", "graph"],
        ),
        session=session,
    )
    await registry.register(
        ProjectInput(
            name="voice-output",
            title="Voice Output via TTS",
            description="TTS via Chromecast",
            tags=["voice", "tts", "chromecast"],
        ),
        session=session,
    )


# ---------------------------------------------------------------------------
# F-code matching
# ---------------------------------------------------------------------------


async def test_fcode_match(registry, session, agent_id, db):
    """F### codes in user input match project names."""
    await _setup_projects(registry, session)
    resolver = ProjectResolver(registry=registry, db=db, agent_id=agent_id)

    matched = await resolver.resolve("Let's work on F047 next", session=session)
    assert len(matched) == 1

    # Verify it matched the right project
    project = await registry.get("F047-goal-project-registry", session=session)
    assert project is not None
    assert project.id in matched


async def test_fcode_match_case_insensitive(registry, session, agent_id, db):
    """F-code matching is case-insensitive."""
    await _setup_projects(registry, session)
    resolver = ProjectResolver(registry=registry, db=db, agent_id=agent_id)

    matched = await resolver.resolve("What about f041?", session=session)
    assert len(matched) == 1


async def test_multiple_fcode_matches(registry, session, agent_id, db):
    """Multiple F-codes match multiple projects."""
    await _setup_projects(registry, session)
    resolver = ProjectResolver(registry=registry, db=db, agent_id=agent_id)

    matched = await resolver.resolve("F047 and F041 are both in progress", session=session)
    assert len(matched) == 2


# ---------------------------------------------------------------------------
# Name matching
# ---------------------------------------------------------------------------


async def test_name_match(registry, session, agent_id, db):
    """Exact project name in user input matches."""
    await _setup_projects(registry, session)
    resolver = ProjectResolver(registry=registry, db=db, agent_id=agent_id)

    matched = await resolver.resolve(
        "Let me check on voice-output progress",
        session=session,
    )
    assert len(matched) >= 1


# ---------------------------------------------------------------------------
# Title matching
# ---------------------------------------------------------------------------


async def test_title_match(registry, session, agent_id, db):
    """Project title in user input matches."""
    await _setup_projects(registry, session)
    resolver = ProjectResolver(registry=registry, db=db, agent_id=agent_id)

    matched = await resolver.resolve(
        "What's the status of Voice Output via TTS?",
        session=session,
    )
    assert len(matched) >= 1


# ---------------------------------------------------------------------------
# Tag matching
# ---------------------------------------------------------------------------


async def test_tag_match(registry, session, agent_id, db):
    """Tags in user input match."""
    await _setup_projects(registry, session)
    resolver = ProjectResolver(registry=registry, db=db, agent_id=agent_id)

    matched = await resolver.resolve(
        "I want to work on chromecast stuff",
        session=session,
    )
    assert len(matched) >= 1


# ---------------------------------------------------------------------------
# No match
# ---------------------------------------------------------------------------


async def test_no_match(registry, session, agent_id, db):
    """Unrelated input returns empty list."""
    await _setup_projects(registry, session)
    resolver = ProjectResolver(registry=registry, db=db, agent_id=agent_id)

    matched = await resolver.resolve("What's the weather like?", session=session)
    assert matched == []


async def test_no_projects_returns_empty(registry, session, agent_id, db):
    """No projects at all returns empty list."""
    resolver = ProjectResolver(registry=registry, db=db, agent_id=agent_id)

    matched = await resolver.resolve("F047 test", session=session)
    assert matched == []


# ---------------------------------------------------------------------------
# Touch on match
# ---------------------------------------------------------------------------


async def test_match_touches_project(registry, session, agent_id, db):
    """Matching a project updates its last_touched_at."""
    await _setup_projects(registry, session)
    resolver = ProjectResolver(registry=registry, db=db, agent_id=agent_id)

    project_before = await registry.get("F047-goal-project-registry", session=session)
    assert project_before is not None
    original_touched = project_before.last_touched_at

    await resolver.resolve("Working on F047", session=session)

    project_after = await registry.get("F047-goal-project-registry", session=session)
    assert project_after is not None
    assert project_after.last_touched_at >= original_touched


# ---------------------------------------------------------------------------
# Paused projects are still matched
# ---------------------------------------------------------------------------


async def test_paused_projects_matched(registry, session, agent_id, db):
    """Paused projects are still detected by the resolver."""
    await registry.register(
        ProjectInput(name="F099-paused-test", title="Paused Test"),
        session=session,
    )
    from nous.projects.schemas import ProjectUpdateInput
    await registry.update(
        "F099-paused-test",
        ProjectUpdateInput(status="paused"),
        session=session,
    )
    resolver = ProjectResolver(registry=registry, db=db, agent_id=agent_id)

    matched = await resolver.resolve("Check F099", session=session)
    assert len(matched) == 1


# ---------------------------------------------------------------------------
# Completed/abandoned projects are NOT matched
# ---------------------------------------------------------------------------


async def test_completed_projects_not_matched(registry, session, agent_id, db):
    """Completed projects are not detected by the resolver."""
    await registry.register(
        ProjectInput(name="F098-done", title="Done Test"),
        session=session,
    )
    from nous.projects.schemas import ProjectUpdateInput
    await registry.update(
        "F098-done",
        ProjectUpdateInput(status="completed"),
        session=session,
    )
    resolver = ProjectResolver(registry=registry, db=db, agent_id=agent_id)

    matched = await resolver.resolve("Check F098", session=session)
    assert len(matched) == 0
