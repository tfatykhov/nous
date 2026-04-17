"""ProjectContextInjector — surfaces Active Projects in working memory (F047).

Generates a markdown block listing active projects for injection into the
context engine's working memory section. Capped at ~400 tokens.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from nous.projects.registry import ProjectRegistry
from nous.projects.schemas import ProjectDetail

logger = logging.getLogger(__name__)


def _relative_time(dt: datetime) -> str:
    """Format a datetime as a human-readable relative time string."""
    now = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    diff = now - dt
    seconds = int(diff.total_seconds())
    if seconds < 60:
        return "just now"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    days = hours // 24
    if days == 1:
        return "yesterday"
    return f"{days}d ago"


class ProjectContextInjector:
    """Builds the Active Projects context block for injection."""

    CHARS_PER_TOKEN = 4

    def __init__(
        self,
        registry: ProjectRegistry,
        max_projects: int = 5,
        max_tokens: int = 400,
    ) -> None:
        self._registry = registry
        self._max_projects = max_projects
        self._max_tokens = max_tokens

    async def build_context_block(
        self,
        session: AsyncSession | None = None,
    ) -> str | None:
        """Build the Active Projects markdown block.

        Returns None if no active projects exist.
        """
        try:
            projects = await self._registry.list_active_for_context(
                limit=self._max_projects,
                session=session,
            )
        except Exception:
            logger.warning("F047: Failed to load active projects for context")
            return None

        if not projects:
            return None

        return self._format_projects(projects)

    def _format_projects(self, projects: list[ProjectDetail]) -> str:
        """Format projects as a compact markdown block."""
        max_chars = self._max_tokens * self.CHARS_PER_TOKEN
        lines: list[str] = []
        char_count = 0
        shown = 0

        for project in projects:
            touched = _relative_time(project.last_touched_at)
            status_str = project.status
            priority_str = f"priority {project.priority:.1f}"

            # Build the bullet line
            line = f"- **{project.name}** ({status_str}, {priority_str}, last touched {touched})"

            # Add description snippet if available
            desc = project.description
            if desc:
                desc_snippet = desc[:120]
                if len(desc) > 120:
                    desc_snippet = desc_snippet.rsplit(" ", 1)[0] + "..."
                line += f"\n  {desc_snippet}"

            # Add most recent event if available
            if project.recent_events:
                latest = project.recent_events[0]
                event_time = _relative_time(latest.created_at)
                line += f"\n  Last event: {latest.summary} ({event_time})"

            line_chars = len(line) + 1  # +1 for newline
            if char_count + line_chars > max_chars and shown > 0:
                remaining = len(projects) - shown
                if remaining > 0:
                    lines.append(f"- +{remaining} more project(s)")
                break

            lines.append(line)
            char_count += line_chars
            shown += 1

        return "\n".join(lines)
