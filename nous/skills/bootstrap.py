"""Bootstrap local SKILL.md files into the procedures DB.

F011 v2: Scans workspace skills directory, deduplicates by exact name match,
and stores new skill procedures. Skills live in the DB after registration.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from nous.heart.heart import Heart
from nous.skills.parser import SkillParser

logger = logging.getLogger(__name__)


async def bootstrap_local_skills(workspace_dir: str, heart: Heart) -> int:
    """Register local SKILL.md files into the procedures DB.

    Scans {workspace_dir}/skills/*/SKILL.md for skill manifests.
    Each skill is deduped by exact name match before storing.

    Returns:
        Number of skills registered
    """
    skills_dir = Path(workspace_dir) / "skills"
    if not skills_dir.exists():
        return 0

    parser = SkillParser()
    registered = 0

    for skill_dir in sorted(skills_dir.iterdir()):
        if not skill_dir.is_dir():
            continue
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.exists():
            continue

        try:
            markdown = skill_md.read_text(encoding="utf-8")
            manifest = parser.parse(markdown, source_hint=str(skill_md))

            # Dedup: check if procedure with this name already exists
            existing = await heart.get_procedure_by_name(manifest.name)
            if existing:
                logger.debug("Skill %s already registered, skipping", manifest.name)
                continue

            # Dedup Phase 0: don't resurrect a deliberately-consolidated duplicate.
            # If a row with this name was archived (superseded_by set) its capability
            # now lives in a canonical procedure — recreating it from disk would
            # reintroduce the duplicate on every restart (the audit's B1 loop).
            if await heart.is_procedure_name_superseded(manifest.name):
                logger.info("Skill %s was consolidated into a canonical procedure, skipping re-import", manifest.name)
                continue

            proc_input = parser.to_procedure_input(manifest)
            await heart.store_procedure(proc_input)
            registered += 1
            logger.info("Bootstrapped skill: %s (%s)", manifest.name, manifest.domain)

        except Exception:
            logger.warning("Failed to bootstrap skill from %s", skill_md, exc_info=True)

    if registered:
        logger.info("Bootstrapped %d local skills into procedures DB", registered)

    return registered


async def reactivate_skills(heart: Heart) -> int:
    """Re-check inactive skill procedures and reactivate if requires are now satisfied.

    Called at startup. Checks core_concepts for env var names (uppercase with underscores)
    and verifies they exist in os.environ.

    Returns:
        Number of skills reactivated
    """
    inactive = await heart.list_inactive_skill_procedures()
    if not inactive:
        return 0

    reactivated = 0
    for proc in inactive:
        concepts = proc.core_concepts or []
        # Extract requires from prefixed core_concepts (e.g. "requires:SERPER_API_KEY")
        requires = [c.removeprefix("requires:") for c in concepts if c.startswith("requires:")]
        if not requires:
            continue

        missing = [var for var in requires if not os.environ.get(var)]
        if not missing:
            await heart.reactivate_procedure(proc.id)
            logger.info("Reactivated skill %s — all requires now satisfied", proc.name)
            reactivated += 1

    if reactivated:
        logger.info("Reactivated %d skills at startup", reactivated)

    return reactivated
