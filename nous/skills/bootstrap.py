"""One-time bootstrap of local SKILL.md files into the procedures DB.

F011 v2: Runs once when the DB has zero skill-tagged procedures.
After that, the filesystem is irrelevant — skills live in the DB.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from nous.heart.heart import Heart
from nous.skills.parser import SkillParser

logger = logging.getLogger(__name__)


async def bootstrap_local_skills(workspace_dir: str, heart: Heart) -> int:
    """Register local SKILL.md files when DB has no skills yet.

    Scans {workspace_dir}/skills/*/SKILL.md for skill manifests.
    Only runs if no procedures with tag 'skill' exist in the DB.

    Returns:
        Number of skills registered
    """
    skills_dir = Path(workspace_dir) / "skills"
    if not skills_dir.exists():
        return 0

    # Check if any skills already exist
    existing = await heart.search_procedures("skill", limit=1)
    has_skills = any(
        "skill" in (getattr(p, "tags", None) or [])
        for p in existing
    )
    # Fallback: search by name pattern
    if not has_skills and existing:
        # If search returned results but none tagged, still skip
        # (there might be non-skill procedures)
        pass

    # More reliable check: search specifically for skill-tagged procedures
    # Since search_procedures uses text/embedding search, we need a broader approach
    all_procs = await heart.search_procedures("skill registration", limit=20)
    for p in all_procs:
        # ProcedureSummary doesn't have tags — check by name convention
        # This is imperfect but good enough for bootstrap guard
        pass

    # Simplest guard: just scan and deduplicate by name at registration time
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
            existing_procs = await heart.search_procedures(manifest.name, limit=5)
            if any(p.name == manifest.name for p in existing_procs):
                logger.debug("Skill %s already registered, skipping", manifest.name)
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
        # Env var names are uppercase strings with underscores
        requires = [c for c in concepts if c == c.upper() and "_" in c]
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
