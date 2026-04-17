"""Project Registry — CRUD operations for projects and events.

Follows the Brain/Heart manager pattern: public methods handle session
creation/commit, private methods work with injected sessions (P1-1).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select, update as sa_update, func
from sqlalchemy.ext.asyncio import AsyncSession

from nous.brain.embeddings import EmbeddingProvider
from nous.projects.schemas import (
    ProjectDetail,
    ProjectEventDetail,
    ProjectInput,
    ProjectNoteInput,
    ProjectStatus,
    ProjectUpdateInput,
)
from nous.storage.database import Database
from nous.storage.models import Project, ProjectEvent

logger = logging.getLogger(__name__)


def _parse_tags(raw: list | str | None) -> list[str]:
    """Safely parse tags from DB — handles Postgres ARRAY and SQLite JSON."""
    if raw is None:
        return []
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return [str(t) for t in parsed]
        except (json.JSONDecodeError, TypeError):
            pass
        return []
    if isinstance(raw, list):
        # SQLite compat: if ARRAY was stored as JSON and read back char-by-char
        if raw and all(len(str(x)) == 1 for x in raw):
            joined = "".join(str(x) for x in raw)
            try:
                parsed = json.loads(joined)
                if isinstance(parsed, list):
                    return [str(t) for t in parsed]
            except (json.JSONDecodeError, TypeError):
                pass
        return [str(t) for t in raw]
    return []


class ProjectRegistry:
    """CRUD manager for the project registry (F047)."""

    def __init__(
        self,
        db: Database,
        agent_id: str,
        embeddings: EmbeddingProvider | None = None,
    ) -> None:
        self.db = db
        self.agent_id = agent_id
        self._embeddings = embeddings

    # ------------------------------------------------------------------
    # register
    # ------------------------------------------------------------------

    async def register(
        self,
        inp: ProjectInput,
        session: AsyncSession | None = None,
    ) -> ProjectDetail:
        """Register a new project. Raises ValueError if name already exists."""
        if session is None:
            async with self.db.session() as session:
                result = await self._register(inp, session)
                await session.commit()
                return result
        return await self._register(inp, session)

    async def _register(self, inp: ProjectInput, session: AsyncSession) -> ProjectDetail:
        # Check uniqueness
        existing = await self._get_by_name(inp.name, session)
        if existing is not None:
            raise ValueError(f"Project '{inp.name}' already exists for this agent")

        embedding = None
        if self._embeddings and inp.description:
            try:
                embedding = await self._embeddings.embed(inp.description)
            except Exception:
                logger.warning("Failed to embed project description for '%s'", inp.name)

        project = Project(
            agent_id=self.agent_id,
            name=inp.name,
            title=inp.title,
            description=inp.description,
            priority=inp.priority,
            tags=inp.tags or [],
            embedding=embedding,
        )
        session.add(project)
        await session.flush()

        # Append 'created' event
        event = ProjectEvent(
            project_id=project.id,
            agent_id=self.agent_id,
            event_type="created",
            summary=f"Project registered: {inp.title}",
        )
        session.add(event)
        await session.flush()

        return self._to_detail(project, events=[event])

    # ------------------------------------------------------------------
    # update
    # ------------------------------------------------------------------

    async def update(
        self,
        name_or_id: str,
        inp: ProjectUpdateInput,
        session: AsyncSession | None = None,
    ) -> ProjectDetail:
        """Update project fields. Raises ValueError if not found."""
        if session is None:
            async with self.db.session() as session:
                result = await self._update(name_or_id, inp, session)
                await session.commit()
                return result
        return await self._update(name_or_id, inp, session)

    async def _update(
        self,
        name_or_id: str,
        inp: ProjectUpdateInput,
        session: AsyncSession,
    ) -> ProjectDetail:
        project = await self._resolve(name_or_id, session)
        if project is None:
            raise ValueError(f"Project '{name_or_id}' not found")

        old_status = project.status
        changes: list[str] = []

        if inp.status is not None and inp.status != project.status:
            project.status = inp.status
            changes.append(f"status: {old_status} → {inp.status}")
        if inp.priority is not None and inp.priority != project.priority:
            changes.append(f"priority: {project.priority} → {inp.priority}")
            project.priority = inp.priority
        if inp.description is not None and inp.description != project.description:
            project.description = inp.description
            changes.append("description updated")
            # Re-embed
            if self._embeddings and inp.description:
                try:
                    project.embedding = await self._embeddings.embed(inp.description)
                except Exception:
                    logger.warning("Failed to re-embed project description")
        if inp.title is not None and inp.title != project.title:
            project.title = inp.title
            changes.append(f"title → {inp.title}")
        if inp.tags is not None:
            project.tags = inp.tags
            changes.append("tags updated")

        now = datetime.now(timezone.utc)
        project.updated_at = now
        project.last_touched_at = now
        await session.flush()

        # Log status change event if status changed
        if inp.status is not None and inp.status != old_status:
            event = ProjectEvent(
                project_id=project.id,
                agent_id=self.agent_id,
                event_type="status_change",
                summary=f"Status changed: {old_status} → {inp.status}",
            )
            session.add(event)
            await session.flush()

        return await self._load_detail(project, session)

    # ------------------------------------------------------------------
    # add_note
    # ------------------------------------------------------------------

    async def add_note(
        self,
        name_or_id: str,
        inp: ProjectNoteInput,
        session: AsyncSession | None = None,
    ) -> ProjectEventDetail:
        """Add a note/event to a project."""
        if session is None:
            async with self.db.session() as session:
                result = await self._add_note(name_or_id, inp, session)
                await session.commit()
                return result
        return await self._add_note(name_or_id, inp, session)

    async def _add_note(
        self,
        name_or_id: str,
        inp: ProjectNoteInput,
        session: AsyncSession,
    ) -> ProjectEventDetail:
        project = await self._resolve(name_or_id, session)
        if project is None:
            raise ValueError(f"Project '{name_or_id}' not found")

        event = ProjectEvent(
            project_id=project.id,
            agent_id=self.agent_id,
            event_type=inp.event_type,
            summary=inp.summary,
            episode_id=inp.episode_id,
        )
        session.add(event)

        # Touch the project
        now = datetime.now(timezone.utc)
        project.last_touched_at = now
        project.updated_at = now
        await session.flush()

        return self._event_to_detail(event)

    # ------------------------------------------------------------------
    # list_projects
    # ------------------------------------------------------------------

    async def list_projects(
        self,
        status: ProjectStatus | None = "active",
        limit: int = 10,
        session: AsyncSession | None = None,
    ) -> list[ProjectDetail]:
        """List projects, optionally filtered by status."""
        if session is None:
            async with self.db.session() as session:
                return await self._list_projects(status, limit, session)
        return await self._list_projects(status, limit, session)

    async def _list_projects(
        self,
        status: ProjectStatus | None,
        limit: int,
        session: AsyncSession,
    ) -> list[ProjectDetail]:
        stmt = (
            select(Project)
            .where(Project.agent_id == self.agent_id)
        )
        if status is not None:
            stmt = stmt.where(Project.status == status)
        stmt = stmt.order_by(Project.last_touched_at.desc()).limit(limit)

        result = await session.execute(stmt)
        projects = result.scalars().all()

        details = []
        for p in projects:
            detail = await self._load_detail(p, session)
            details.append(detail)
        return details

    # ------------------------------------------------------------------
    # get
    # ------------------------------------------------------------------

    async def get(
        self,
        name_or_id: str,
        session: AsyncSession | None = None,
    ) -> ProjectDetail | None:
        """Get a project by name or ID."""
        if session is None:
            async with self.db.session() as session:
                project = await self._resolve(name_or_id, session)
                if project is None:
                    return None
                return await self._load_detail(project, session)
        project = await self._resolve(name_or_id, session)
        if project is None:
            return None
        return await self._load_detail(project, session)

    # ------------------------------------------------------------------
    # touch
    # ------------------------------------------------------------------

    async def touch(
        self,
        project_id: UUID,
        session: AsyncSession | None = None,
    ) -> None:
        """Update last_touched_at timestamp."""
        if session is None:
            async with self.db.session() as session:
                await self._touch(project_id, session)
                await session.commit()
                return
        await self._touch(project_id, session)

    async def _touch(self, project_id: UUID, session: AsyncSession) -> None:
        await session.execute(
            sa_update(Project)
            .where(Project.id == project_id)
            .where(Project.agent_id == self.agent_id)
            .values(last_touched_at=func.now())
        )
        await session.flush()

    # ------------------------------------------------------------------
    # list_active_for_context
    # ------------------------------------------------------------------

    async def list_active_for_context(
        self,
        limit: int = 5,
        session: AsyncSession | None = None,
    ) -> list[ProjectDetail]:
        """List active projects ordered by priority * recency for context injection."""
        if session is None:
            async with self.db.session() as session:
                return await self._list_active_for_context(limit, session)
        return await self._list_active_for_context(limit, session)

    async def _list_active_for_context(
        self, limit: int, session: AsyncSession
    ) -> list[ProjectDetail]:
        stmt = (
            select(Project)
            .where(Project.agent_id == self.agent_id)
            .where(Project.status == "active")
            .order_by(Project.priority.desc(), Project.last_touched_at.desc())
            .limit(limit)
        )
        result = await session.execute(stmt)
        projects = result.scalars().all()

        details = []
        for p in projects:
            detail = await self._load_detail(p, session)
            details.append(detail)
        return details

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _resolve(self, name_or_id: str, session: AsyncSession) -> Project | None:
        """Resolve a project by name or UUID string."""
        # Try UUID first
        try:
            uid = UUID(name_or_id)
            result = await session.execute(
                select(Project)
                .where(Project.id == uid)
                .where(Project.agent_id == self.agent_id)
            )
            project = result.scalars().first()
            if project is not None:
                return project
        except (ValueError, AttributeError):
            pass

        # Fall back to name lookup
        return await self._get_by_name(name_or_id, session)

    async def _get_by_name(self, name: str, session: AsyncSession) -> Project | None:
        result = await session.execute(
            select(Project)
            .where(Project.agent_id == self.agent_id)
            .where(Project.name == name)
        )
        return result.scalars().first()

    async def _load_detail(self, project: Project, session: AsyncSession) -> ProjectDetail:
        """Load project detail with recent events."""
        stmt = (
            select(ProjectEvent)
            .where(ProjectEvent.project_id == project.id)
            .order_by(ProjectEvent.created_at.desc())
            .limit(5)
        )
        result = await session.execute(stmt)
        events = result.scalars().all()
        return self._to_detail(project, events=events)

    def _to_detail(
        self, project: Project, events: list[ProjectEvent] | None = None
    ) -> ProjectDetail:
        return ProjectDetail(
            id=project.id,
            agent_id=project.agent_id,
            name=project.name,
            title=project.title,
            description=project.description,
            status=project.status,
            priority=project.priority,
            tags=_parse_tags(project.tags),
            source_decision_id=project.source_decision_id,
            created_at=project.created_at,
            updated_at=project.updated_at,
            last_touched_at=project.last_touched_at,
            recent_events=[self._event_to_detail(e) for e in (events or [])],
        )

    def _event_to_detail(self, event: ProjectEvent) -> ProjectEventDetail:
        return ProjectEventDetail(
            id=event.id,
            project_id=event.project_id,
            event_type=event.event_type,
            summary=event.summary,
            episode_id=event.episode_id,
            created_at=event.created_at,
        )
