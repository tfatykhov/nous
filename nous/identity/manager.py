"""IdentityManager — CRUD for agent identity sections.

Manages the slow-changing identity layer: character, values, protocols,
preferences, boundaries. Each section is independently versioned with
`is_current` flag for simple queries.

Review fix P1-3: IdentityManager is used only by CognitiveLayer.
ContextEngine receives the assembled identity string, never touches
the DB directly.

Review fix P3-2: 60s TTL cache to avoid per-turn DB hits.

The default goal prompt (``nous.goals.default_prompt``) is kept in the same versioned table under
``GOAL_PROMPT_SECTION``, a name in neither ``SECTIONS`` nor ``VALID_SECTIONS``: ``update_section`` refuses it, and
``get_current`` and ``reset_identity`` leave it out, so it is never part of the identity. Only ``set_goal_prompt``
and ``clear_goal_prompt`` write it (the owner's ``PUT`` and ``DELETE /goals/prompt``); its reads are not cached.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from nous.storage.database import Database
from nous.storage.models import Agent, AgentIdentity

logger = logging.getLogger(__name__)

SECTIONS = ["character", "values", "protocols", "preferences", "boundaries", "environment"]
VALID_SECTIONS = set(SECTIONS) | {"status"}  # status is internal control field

# The default goal prompt's row: a section outside SECTIONS and VALID_SECTIONS, at most this many characters.
GOAL_PROMPT_SECTION = "goal_prompt"
GOAL_PROMPT_MAX_CHARS = 1_500
GOAL_PROMPT_WRITERS = frozenset({"owner", "proxy"})  # the proofs the owner guard accepts
_CURRENT_INDEX = "idx_identity_agent_section_current"

# Cache TTL in seconds
_CACHE_TTL = 60.0


def normalize_goal_prompt(text: str) -> str:
    """The goal prompt as it is stored and counted: ``\\r\\n`` turned into ``\\n``, then stripped."""
    return text.replace("\r\n", "\n").strip()


@dataclass(frozen=True)
class GoalPromptRow:
    """The owner's saved goal prompt: its text, when it was saved and by which proof (``owner`` or ``proxy``)."""

    text: str
    updated_at: datetime | None
    updated_by: str | None


class GoalPromptChanged(Exception):
    """Another save of the goal prompt committed while this one was being written; nothing was written."""


def _constraint_of(exc: IntegrityError) -> str | None:
    """The name of the constraint or index a write violated, as the driver (asyncpg) reports it, else None."""
    for error in (exc.orig, getattr(exc.orig, "__cause__", None)):
        name = getattr(error, "constraint_name", None)
        if name:
            return str(name)
    return None


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
                select(AgentIdentity)
                .where(
                    AgentIdentity.agent_id == self.agent_id,
                    AgentIdentity.is_current == True,  # noqa: E712
                    AgentIdentity.section != GOAL_PROMPT_SECTION,
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
            await self._write_version(s, section, content, updated_by)
            self._invalidate_cache()

        if session is not None:
            await _update(session)
        else:
            async with self.db.session() as s:
                await _update(s)
                await s.commit()

    async def _write_version(self, s: AsyncSession, section: str, content: str, updated_by: str) -> None:
        """A new current version of ``section`` in ``s``; the current one, if any, is marked not current."""
        # Get current version number
        result = await s.execute(
            select(AgentIdentity)
            .where(
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

    async def get_goal_prompt(self, session: AsyncSession | None = None) -> GoalPromptRow | None:
        """The owner's saved goal prompt, or None when nothing is saved (the default is then used). Not cached."""

        async def _get(s: AsyncSession) -> GoalPromptRow | None:
            row = (
                await s.execute(
                    select(AgentIdentity).where(
                        AgentIdentity.agent_id == self.agent_id,
                        AgentIdentity.section == GOAL_PROMPT_SECTION,
                        AgentIdentity.is_current == True,  # noqa: E712
                    )
                )
            ).scalar_one_or_none()
            return None if row is None else GoalPromptRow(row.content, row.updated_at, row.updated_by)

        if session is not None:
            return await _get(session)
        async with self.db.session() as s:
            return await _get(s)

    async def set_goal_prompt(self, text: str, updated_by: str, session: AsyncSession | None = None) -> None:
        """Save ``text`` as the goal prompt, a new version. ``ValueError`` when it is blank or longer than
        ``GOAL_PROMPT_MAX_CHARS`` once normalized (``normalize_goal_prompt``), or when ``updated_by`` is not a proof
        the owner guard accepts; ``GoalPromptChanged`` when another save committed meanwhile."""
        content = normalize_goal_prompt(text)
        if not content:
            raise ValueError("the goal prompt is empty")
        if len(content) > GOAL_PROMPT_MAX_CHARS:
            raise ValueError(f"the goal prompt is longer than {GOAL_PROMPT_MAX_CHARS} characters")
        if updated_by not in GOAL_PROMPT_WRITERS:
            raise ValueError(f"the goal prompt is written by {sorted(GOAL_PROMPT_WRITERS)} only")

        async def _set(s: AsyncSession) -> None:
            try:
                await self._write_version(s, GOAL_PROMPT_SECTION, content, updated_by)
                if session is None:
                    await s.commit()
            except IntegrityError as exc:
                if _constraint_of(exc) == _CURRENT_INDEX:
                    raise GoalPromptChanged() from None
                raise

        if session is not None:
            await _set(session)
        else:
            async with self.db.session() as s:
                await _set(s)

    async def clear_goal_prompt(self, session: AsyncSession | None = None) -> bool:
        """Go back to the default: the saved goal prompt is marked not current (its versions stay). True when one
        was saved."""

        async def _clear(s: AsyncSession) -> bool:
            cleared = await s.execute(
                update(AgentIdentity)
                .where(
                    AgentIdentity.agent_id == self.agent_id,
                    AgentIdentity.section == GOAL_PROMPT_SECTION,
                    AgentIdentity.is_current == True,  # noqa: E712
                )
                .values(is_current=False)
                .returning(AgentIdentity.id)
            )
            return cleared.first() is not None

        if session is not None:
            return await _clear(session)
        async with self.db.session() as s:
            done = await _clear(s)
            await s.commit()
            return done

    async def is_initiated(self, session: AsyncSession | None = None) -> bool:
        """Check if agent has completed initiation.

        Review fix P1-1/P3-3: Uses is_initiated flag on agents table,
        not identity section count. Avoids partial initiation limbo.
        """
        async def _check(s: AsyncSession) -> bool:
            result = await s.execute(
                select(Agent.is_initiated).where(Agent.id == self.agent_id)
            )
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
            await s.execute(
                update(Agent)
                .where(Agent.id == self.agent_id)
                .values(is_initiated=True)
            )

        if session is not None:
            await _mark(session)
        else:
            async with self.db.session() as s:
                await _mark(s)
                await s.commit()

    async def reset_identity(self, session: AsyncSession | None = None) -> None:
        """Reset identity — deactivate all sections and clear is_initiated flag.

        Used by POST /reinitiate to trigger fresh initiation on next conversation. The goal prompt is not
        identity and is kept.
        """
        async def _reset(s: AsyncSession) -> None:
            # Deactivate all current identity sections
            await s.execute(
                update(AgentIdentity)
                .where(
                    AgentIdentity.agent_id == self.agent_id,
                    AgentIdentity.is_current == True,  # noqa: E712
                    AgentIdentity.section != GOAL_PROMPT_SECTION,
                )
                .values(is_current=False)
            )
            # Reset is_initiated flag
            await s.execute(
                update(Agent)
                .where(Agent.id == self.agent_id)
                .values(is_initiated=False)
            )

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

    async def auto_seed_from_facts(
        self, heart: Any, session: AsyncSession
    ) -> bool:
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
            pref_facts = await heart.search_facts("user preferences and settings", category="preference", limit=20, session=session)
            person_facts = await heart.search_facts("user personal information", category="person", limit=20, session=session)
            rule_facts = await heart.search_facts("behavior rules and constraints", category="rule", limit=20, session=session)
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
            len(person_facts), len(pref_facts), len(rule_facts),
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
