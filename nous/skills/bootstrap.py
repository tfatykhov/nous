"""Bootstrap local SKILL.md files into the procedures DB.

F011 v2: Scans workspace skills directory, deduplicates by exact name match,
and stores new skill procedures. Skills live in the DB after registration.

F110: the builtin skills ship in the package (``builtin/<name>/SKILL.md``) and
follow a setting at every start (``sync_builtin_skills``).
"""

from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path

from sqlalchemy import func, select

from nous.heart.heart import Heart
from nous.skills.parser import SkillParser
from nous.storage.models import Procedure

logger = logging.getLogger(__name__)

# F110: skills that ship with the code. Package data (pyproject.toml), so an installed copy has them too.
BUILTIN_DIR = Path(__file__).parent / "builtin"
# Marks a procedure stored from BUILTIN_DIR: only a row with it is updated or retired by sync_builtin_skills.
BUILTIN_TAG = "builtin-skill"
# The core concept that holds the sha256 of the file a builtin skill was stored from.
BODY_SHA_PREFIX = "body-sha:"


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


async def sync_builtin_skills(heart: Heart, enabled: bool) -> int:
    """Keep the builtin skills (``BUILTIN_DIR/*/SKILL.md``) in step with ``enabled`` and with their files.

    Called at startup after ``reactivate_skills``. Modelled on the tool-notes procedure
    (``nous/handlers/tool_notes.py`` ``sync_procedure``), not on ``bootstrap_local_skills``: the row is found
    active or not, by name and the ``builtin-skill`` tag (``get_procedure_by_name`` is active-only, so a second
    store would duplicate a retired row). Enabled: a missing skill is stored; a stored one is re-imported in place
    (a new version, learned counts kept) when the file's sha256 differs from its ``body-sha:`` concept, and
    reactivated when inactive; a name consolidated into another procedure is left alone. Disabled: an active row is
    retired. The one builtin skill is the spec skill, so ``enabled`` is ``NOUS_SPEC_SKILL_ENABLED``.

    Never raises: a failure is logged with its exception type only and that skill is skipped.

    Returns:
        Number of builtin skills whose row was stored, re-imported, reactivated or retired
    """
    try:
        skill_files = sorted(BUILTIN_DIR.glob("*/SKILL.md"))
    except Exception as exc:
        logger.warning("Builtin skills: scan failed (%s)", type(exc).__name__)
        return 0
    parser = SkillParser()
    changed = 0
    for skill_md in skill_files:
        try:
            changed += await _sync_builtin_skill(heart, parser, skill_md, enabled)
        except Exception as exc:
            logger.warning("Builtin skill %s: sync failed (%s)", skill_md.parent.name, type(exc).__name__)
    return changed


async def _sync_builtin_skill(heart: Heart, parser: SkillParser, skill_md: Path, enabled: bool) -> int:
    """Sync one builtin skill file; returns 1 when its row was written, else 0."""
    markdown = skill_md.read_text(encoding="utf-8")  # text mode: a CRLF checkout hashes as LF
    manifest = parser.parse(markdown, source_hint=f"builtin:{skill_md.parent.name}")
    if enabled and await heart.is_procedure_name_superseded(manifest.name):
        # Dedup Phase 0, as for workspace skills: a name consolidated into a canonical procedure is not brought back.
        logger.info("Builtin skill %s was consolidated into a canonical procedure, skipping", manifest.name)
        return 0
    async with heart.db.session() as session:
        row = (
            await session.execute(
                select(Procedure.id, Procedure.active, Procedure.core_concepts)
                .where(Procedure.agent_id == heart.agent_id)
                .where(func.lower(Procedure.name) == func.lower(manifest.name))
                .where(Procedure.tags.any(BUILTIN_TAG))
                .order_by(Procedure.created_at.desc())
                .limit(1)
            )
        ).first()

    if not enabled:
        if row is None or not row.active:
            return 0
        # Retired, not archived, as the tool-notes procedure is: reactivate_skills lists the row but re-enables only
        # a skill with ``requires:`` concepts, and the builtin skills declare none. It also runs just before this
        # call at startup, so the setting has the last word.
        await heart.retire_procedure(row.id, reason={"reason": "spec_skill_disabled"})
        logger.info("Builtin skill %s retired (disabled)", manifest.name)
        return 1

    body_sha = BODY_SHA_PREFIX + hashlib.sha256(markdown.encode("utf-8")).hexdigest()
    proc_input = parser.to_procedure_input(manifest)
    proc_input.tags = [*proc_input.tags, BUILTIN_TAG]
    proc_input.core_concepts = [*proc_input.core_concepts, body_sha]
    if row is None:
        await heart.store_procedure(proc_input)
        logger.info("Builtin skill %s loaded", manifest.name)
        return 1
    changed = body_sha not in (row.core_concepts or [])
    if changed:
        await heart.update_procedure_body(row.id, proc_input, reason="builtin_skill_changed")
        logger.info("Builtin skill %s re-imported (file changed)", manifest.name)
    if not row.active:
        await heart.reactivate_procedure(row.id)
        logger.info("Builtin skill %s reactivated", manifest.name)
    return int(changed or not row.active)
