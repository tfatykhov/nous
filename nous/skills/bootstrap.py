"""Bootstrap local SKILL.md files into the procedures DB.

F011 v2: Scans workspace skills directory, deduplicates by exact name match,
and stores new skill procedures. Skills live in the DB after registration.

F110: the builtin skills ship in the package (``builtin/<name>/SKILL.md``) and
follow a setting at every start (``sync_builtin_skills``). A builtin skill owns
its name: a workspace skill with that name is not registered.
"""

from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path
from uuid import UUID

from sqlalchemy import func, select

from nous.heart.heart import Heart
from nous.heart.schemas import JOB_LESSON_KIND, STRATEGY_CARD_KIND
from nous.skills.parser import SkillParser
from nous.storage.models import Procedure

logger = logging.getLogger(__name__)

# F110: skills that ship with the code. Package data (pyproject.toml), so an installed copy has them too.
BUILTIN_DIR = Path(__file__).parent / "builtin"
# Stamped on a procedure stored or re-imported from BUILTIN_DIR (a marker: the sync finds its row by name).
BUILTIN_TAG = "builtin-skill"
# runtime_metadata key of a builtin row: {"body_sha": sha256 of the file it was stored from}. Not in core_concepts,
# which feed the embedding.
BUILTIN_META_KEY = "builtin"


def _builtin_skill_names(parser: SkillParser) -> set[str]:
    """Lower-cased names of the builtin skills; a file that does not parse owns no name."""
    names: set[str] = set()
    for skill_md in BUILTIN_DIR.glob("*/SKILL.md"):
        try:
            names.add(parser.parse(skill_md.read_text(encoding="utf-8")).name.lower())
        except Exception:
            continue
    return names


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
    builtin_names = _builtin_skill_names(parser)

    for skill_dir in sorted(skills_dir.iterdir()):
        if not skill_dir.is_dir():
            continue
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.exists():
            continue

        try:
            markdown = skill_md.read_text(encoding="utf-8")
            manifest = parser.parse(markdown, source_hint=str(skill_md))

            # F110: a builtin skill owns its name; sync_builtin_skills would take the row over (or retire it, off).
            if manifest.name.lower() in builtin_names:
                logger.info("Skill %s has a builtin skill's name, skipping the workspace copy", manifest.name)
                continue

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
    active or not (``get_procedure_by_name`` is active-only, so a second store would duplicate a retired row), by
    name, with or without the ``builtin-skill`` tag: a builtin skill owns its name and its file is the source of
    truth, so a same-name row ``learn_skill`` rewrote is taken back. Never a strategy card, a job lesson or a
    consolidated row; an active row is preferred. Enabled: a missing skill is stored, unless the name was
    consolidated into another procedure; a found row is re-imported in place (a new version, learned counts kept,
    the tag stamped again) when the file's sha256 differs from ``runtime_metadata["builtin"]["body_sha"]`` or is
    missing, and reactivated when inactive. Disabled: an active row is retired. The one builtin skill is the spec
    skill, so ``enabled`` is ``NOUS_SPEC_SKILL_ENABLED``.

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


async def _is_active(heart: Heart, procedure_id: UUID) -> bool:
    """The row's ``active`` as stored now."""
    async with heart.db.session() as session:
        active = await session.scalar(
            select(Procedure.active).where(Procedure.id == procedure_id).where(Procedure.agent_id == heart.agent_id)
        )
    return bool(active)


async def _sync_builtin_skill(heart: Heart, parser: SkillParser, skill_md: Path, enabled: bool) -> int:
    """Sync one builtin skill file; returns 1 when its row was written, else 0."""
    markdown = skill_md.read_text(encoding="utf-8")  # text mode: a CRLF checkout hashes as LF
    manifest = parser.parse(markdown, source_hint=f"builtin:{skill_md.parent.name}")
    async with heart.db.session() as session:
        row = (
            await session.execute(
                select(Procedure.id, Procedure.active, Procedure.runtime_metadata)
                .where(Procedure.agent_id == heart.agent_id)
                # Compared the way the name index compares: lower() in the database, both sides.
                .where(func.lower(Procedure.name) == func.lower(manifest.name))
                .where(Procedure.kind.is_distinct_from(STRATEGY_CARD_KIND))
                .where(Procedure.kind.is_distinct_from(JOB_LESSON_KIND))
                .where(Procedure.superseded_by.is_(None))
                # The active row first, so off retires the live one when an older copy of the name is inactive.
                .order_by(Procedure.active.desc().nulls_last(), Procedure.created_at.desc())
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

    if row is None and await heart.is_procedure_name_superseded(manifest.name):
        # Dedup Phase 0, as for workspace skills: a name consolidated into a canonical procedure is not brought back.
        logger.info("Builtin skill %s was consolidated into a canonical procedure, skipping", manifest.name)
        return 0

    body_sha = hashlib.sha256(markdown.encode("utf-8")).hexdigest()
    proc_input = parser.to_procedure_input(manifest)
    proc_input.tags = [*proc_input.tags, BUILTIN_TAG]
    # Merged into the manifest's runtime metadata (F064.4 keys): update_body replaces the column wholesale.
    proc_input.runtime_metadata = {**(proc_input.runtime_metadata or {}), BUILTIN_META_KEY: {"body_sha": body_sha}}
    if row is None:
        await heart.store_procedure(proc_input)
        logger.info("Builtin skill %s loaded", manifest.name)
        return 1
    stored_sha = ((row.runtime_metadata or {}).get(BUILTIN_META_KEY) or {}).get("body_sha")
    changed = stored_sha != body_sha
    if changed:
        await heart.update_procedure_body(row.id, proc_input, reason="builtin_skill_changed")
        logger.info("Builtin skill %s re-imported (file changed)", manifest.name)
    reactivated = False
    if not row.active:
        await heart.reactivate_procedure(row.id)
        # reactivate skips (with a warning) when another active row holds the name: count only what happened.
        reactivated = await _is_active(heart, row.id)
        if reactivated:
            logger.info("Builtin skill %s reactivated", manifest.name)
    return int(changed or reactivated)
