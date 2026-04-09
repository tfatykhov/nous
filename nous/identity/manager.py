"""IdentityManager — CRUD for agent identity sections.

Manages the slow-changing identity layer: character, values, protocols,
preferences, boundaries. Each section is independently versioned with
`is_current` flag for simple queries.

Review fix P1-3: IdentityManager is used only by CognitiveLayer.
ContextEngine receives the assembled identity string, never touches
the DB directly.

Review fix P3-2: 60s TTL cache to avoid per-turn DB hits.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from nous.storage.database import Database
from nous.storage.models import Agent, AgentIdentity

logger = logging.getLogger(__name__)

SECTIONS = ["character", "values", "protocols", "preferences", "boundaries", "environment"]
VALID_SECTIONS = set(SECTIONS) | {"status"}  # status is internal control field

# Cache TTL in seconds
_CACHE_TTL = 60.0


class IdentityManager:
    """Manages agent identity — the slow-changing 'soul' layer."""

    def __init__(self, database: Database, agent_id: str):
        self.db = database
        self.agent_id = agent_id
        self._cache: dict[str, str] | None = None
        self._cache_expires: float = 0.0

    def _invalidate_cache(self) -> None:
        """Invalidate the in-memory cache."""
        self._cache = None
        self._cache_expires = 0.0

    async def get_current(self, session: AsyncSession | None = None) -> dict[str, str]:
        """Load current identity (latest version of each section).

        Returns dict of section_name -> content for all is_current=True rows.
        Uses 60s TTL cache to avoid per-turn DB hits (review fix P3-2).
        """
        if self._cache is not None and time.monotonic() < self._cache_expires:
            return dict(self._cache)

        async def _load(s: AsyncSession) -> dict[str, str]:
            # Review fix P2-3: is_current=True instead of DISTINCT ON
            result = await s.execute(
                select(AgentIdentity).where(
                    AgentIdentity.agent_id == self.agent_id,
                    AgentIdentity.is_current == True,  # noqa: E712
                )
            )
            rows = result.scalars().all()
            sections = {r.section: r.content for r in rows}
            self._cache = sections
            self._cache_expires = time.monotonic() + _CACHE_TTL
            return dict(sections)

        if session is not None:
            return await _load(session)
        async with self.db.session() as s:
            return await _load(s)

    async def update_section(
        self,
        section: str,
        content: str,
        updated_by: str = "user",
        session: AsyncSession | None = None,
    ) -> None:
        """Create new version of a section. Marks old version as not current.

        Review fix P2-3: Uses is_current flag instead of DISTINCT ON versioning.
        """
        if section not in VALID_SECTIONS:
            raise ValueError(f"Invalid section '{section}'. Valid: {sorted(VALID_SECTIONS)}")

        async def _update(s: AsyncSession) -> None:
            # Get current version number
            result = await s.execute(
                select(AgentIdentity).where(
                    AgentIdentity.agent_id == self.agent_id,
                    AgentIdentity.section == section,
                    AgentIdentity.is_current == True,  # noqa: E712
                )
            )
            current = result.scalar_one_or_none()

            if current is not None:
                # Mark old version as not current
                current.is_current = False
                new_version = current.version + 1
                previous_id = current.id
            else:
                new_version = 1
                previous_id = None

            # Insert new version
            new_row = AgentIdentity(
                agent_id=self.agent_id,
                section=section,
                content=content,
                version=new_version,
                is_current=True,
                updated_by=updated_by,
                previous_version_id=previous_id,
            )
            s.add(new_row)
            await s.flush()
            self._invalidate_cache()

        if session is not None:
            await _update(session)
        else:
            async with self.db.session() as s:
                await _update(s)
                await s.commit()

    async def is_initiated(self, session: AsyncSession | None = None) -> bool:
        """Check if agent has completed initiation.

        Review fix P1-1/P3-3: Uses is_initiated flag on agents table,
        not identity section count. Avoids partial initiation limbo.
        """

        async def _check(s: AsyncSession) -> bool:
            result = await s.execute(select(Agent.is_initiated).where(Agent.id == self.agent_id))
            row = result.scalar_one_or_none()
            return bool(row)

        if session is not None:
            return await _check(session)
        async with self.db.session() as s:
            return await _check(s)

    async def mark_initiated(self, session: AsyncSession | None = None) -> None:
        """Mark agent as having completed initiation.

        Review fix P2-1: Uses atomic UPDATE on agents table.
        """

        async def _mark(s: AsyncSession) -> None:
            await s.execute(update(Agent).where(Agent.id == self.agent_id).values(is_initiated=True))

        if session is not None:
            await _mark(session)
        else:
            async with self.db.session() as s:
                await _mark(s)
                await s.commit()

    async def reset_identity(self, session: AsyncSession | None = None) -> None:
        """Reset identity — deactivate all sections and clear is_initiated flag.

        Used by POST /reinitiate to trigger fresh initiation on next conversation.
        """

        async def _reset(s: AsyncSession) -> None:
            # Deactivate all current identity sections
            await s.execute(
                update(AgentIdentity)
                .where(
                    AgentIdentity.agent_id == self.agent_id,
                    AgentIdentity.is_current == True,  # noqa: E712
                )
                .values(is_current=False)
            )
            # Reset is_initiated flag
            await s.execute(update(Agent).where(Agent.id == self.agent_id).values(is_initiated=False))

        self._invalidate_cache()
        if session is not None:
            await _reset(session)
        else:
            async with self.db.session() as s:
                await _reset(s)
                await s.commit()

    async def claim_initiation(self, session: AsyncSession) -> bool:
        """Atomically claim initiation ownership.

        Review fix P2-1: Prevents race condition when two concurrent
        first messages both try to start initiation. Uses UPDATE with
        WHERE is_initiated=FALSE — only one caller wins.

        Returns True if this caller won the claim, False if someone else did.
        """
        result = await session.execute(
            update(Agent)
            .where(Agent.id == self.agent_id, Agent.is_initiated == False)  # noqa: E712
            .values(is_initiated=None)  # NULL = in_progress (tri-state: False/None/True)
            .returning(Agent.id)
        )
        return result.scalar_one_or_none() is not None

    async def auto_seed_from_facts(self, heart: Any, session: AsyncSession) -> bool:
        """Auto-seed identity from existing facts for upgrade path.

        Review fix P2-2: If heart.facts has preference/person/rule facts
        but agent_identity is empty, auto-seed and mark initiated.
        Prevents "I'm new!" after upgrade.

        Returns True if seeding occurred.
        """
        # Check if identity already exists
        identity = await self.get_current(session=session)
        if identity:
            return False

        # Check for existing user facts
        try:
            # P2-3: Use descriptive queries instead of empty string for meaningful embeddings
            pref_facts = await heart.search_facts(
                "user preferences and settings", category="preference", limit=20, session=session
            )
            person_facts = await heart.search_facts(
                "user personal information", category="person", limit=20, session=session
            )
            rule_facts = await heart.search_facts(
                "behavior rules and constraints", category="rule", limit=20, session=session
            )
        except Exception:
            logger.warning("Failed to search facts for auto-seed")
            return False

        if not (pref_facts or person_facts or rule_facts):
            return False

        # Build preferences section from existing facts
        parts = []
        if person_facts:
            parts.append("### User")
            for f in person_facts:
                parts.append(f"- {f.content}")
        if pref_facts:
            parts.append("### Preferences")
            for f in pref_facts:
                parts.append(f"- {f.content}")
        if rule_facts:
            parts.append("### Rules")
            for f in rule_facts:
                parts.append(f"- {f.content}")

        await self.update_section("preferences", "\n".join(parts), updated_by="auto_seed", session=session)
        await self.mark_initiated(session=session)
        self._invalidate_cache()

        logger.info(
            "Auto-seeded identity from %d existing facts (person=%d, pref=%d, rule=%d)",
            len(pref_facts) + len(person_facts) + len(rule_facts),
            len(person_facts),
            len(pref_facts),
            len(rule_facts),
        )
        return True

    def assemble_prompt(self, sections: dict[str, str]) -> str:
        """Assemble identity sections into a system prompt prefix.

        Note: 'status' is a control field intentionally excluded from
        prompt output (review fix P3-2).
        """
        parts = []
        for section_name in SECTIONS:
            if section_name in sections and sections[section_name]:
                parts.append(f"## {section_name.title()}\n{sections[section_name]}")
        return "\n\n".join(parts)
