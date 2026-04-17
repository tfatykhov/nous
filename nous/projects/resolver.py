"""ProjectResolver — pre-turn project matcher (F047 Phase 1).

Detects whether a user message references an existing project via:
1. Explicit F\\d{3} mention matching project.name
2. Exact name mention (case-insensitive substring)
3. Tag overlap

On match: touches last_touched_at so the project stays active in context.
Phase 2 will add embedding-based matching and auto-creation.
"""

from __future__ import annotations

import logging
import re
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from nous.projects.registry import ProjectRegistry, _parse_tags
from nous.storage.database import Database
from nous.storage.models import Project

logger = logging.getLogger(__name__)

# Matches F### patterns (e.g. F047, F011)
_FCODE_RE = re.compile(r"\bF(\d{3})\b", re.IGNORECASE)


class ProjectResolver:
    """Detects project references in user messages and touches them."""

    def __init__(
        self,
        registry: ProjectRegistry,
        db: Database,
        agent_id: str,
    ) -> None:
        self._registry = registry
        self._db = db
        self._agent_id = agent_id

    async def resolve(
        self,
        user_input: str,
        session: AsyncSession | None = None,
    ) -> list[UUID]:
        """Detect referenced projects and touch them.

        Returns list of matched project IDs.
        """
        if session is None:
            async with self._db.session() as session:
                result = await self._resolve(user_input, session)
                await session.commit()
                return result
        return await self._resolve(user_input, session)

    async def _resolve(
        self,
        user_input: str,
        session: AsyncSession,
    ) -> list[UUID]:
        # Load active projects for this agent
        stmt = (
            select(Project)
            .where(Project.agent_id == self._agent_id)
            .where(Project.status.in_(["active", "paused"]))
        )
        result = await session.execute(stmt)
        projects = result.scalars().all()

        if not projects:
            return []

        matched_ids: list[UUID] = []
        input_lower = user_input.lower()

        # Extract F-codes from input
        fcodes = {m.group(0).upper() for m in _FCODE_RE.finditer(user_input)}

        for project in projects:
            if self._matches(project, input_lower, fcodes):
                matched_ids.append(project.id)

        # Touch all matched projects
        for pid in matched_ids:
            try:
                await self._registry.touch(pid, session=session)
            except Exception:
                logger.debug("Failed to touch project %s", pid)

        if matched_ids:
            logger.info("F047 resolver: matched %d projects from input", len(matched_ids))

        return matched_ids

    def _matches(
        self,
        project: Project,
        input_lower: str,
        fcodes: set[str],
    ) -> bool:
        """Check if a project matches the user input."""
        name = project.name or ""

        # 1. F-code match (e.g. name starts with "F047")
        name_upper = name.upper()
        for fcode in fcodes:
            if name_upper.startswith(fcode) or fcode in name_upper:
                return True

        # 2. Exact name mention (case-insensitive)
        if name.lower() in input_lower and len(name) >= 3:
            return True

        # 3. Title mention (case-insensitive, only if title is >= 5 chars to avoid noise)
        title = project.title or ""
        if title and len(title) >= 5 and title.lower() in input_lower:
            return True

        # 4. Tag overlap
        tags = _parse_tags(project.tags)
        for tag in tags:
            if tag and len(tag) >= 3 and tag.lower() in input_lower:
                return True

        return False
