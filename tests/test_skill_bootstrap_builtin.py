"""F110 PR 6: sync_builtin_skills keeps the builtin skills in step with the flag and their files (real Postgres)."""

from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime
from pathlib import Path

import pytest
from job_support import lesson_heart  # noqa: F401
from sqlalchemy import func, select, update

from nous.config import Settings
from nous.heart.schemas import ProcedureInput
from nous.skills import bootstrap
from nous.skills.bootstrap import BUILTIN_TAG, reactivate_skills, sync_builtin_skills
from nous.skills.parser import SkillParser
from nous.storage.models import Procedure

pytestmark = pytest.mark.postgres_only

ROOT = Path(__file__).resolve().parents[1]
NAME = "spec-writing-verify"
SKILL_MD = Path(bootstrap.__file__).parent / "builtin" / "spec-writing" / "SKILL.md"


def body_sha(markdown: str) -> str:
    return "body-sha:" + hashlib.sha256(markdown.encode("utf-8")).hexdigest()


async def rows(heart) -> list[Procedure]:
    """Every procedure of this agent with the skill's name, active or not."""
    async with heart.db.session() as s:
        res = await s.execute(
            select(Procedure)
            .where(Procedure.agent_id == heart.agent_id)
            .where(func.lower(Procedure.name) == NAME)
            .order_by(Procedure.created_at)
        )
        return list(res.scalars().all())


def edited_copy(tmp_path: Path) -> str:
    """A builtin dir under tmp_path whose spec skill has another description and a sixth step."""
    markdown = SKILL_MD.read_text(encoding="utf-8")
    markdown = markdown.replace("description: Writing a spec,", "description: Drafting a spec,")
    markdown += "6. Say which steps were skipped and why.\n"
    (tmp_path / "spec-writing").mkdir()
    (tmp_path / "spec-writing" / "SKILL.md").write_text(markdown, encoding="utf-8")
    return markdown


@pytest.mark.asyncio
async def test_loads_when_enabled(lesson_heart):  # noqa: F811
    markdown = SKILL_MD.read_text(encoding="utf-8")
    assert await sync_builtin_skills(lesson_heart, True) == 1
    (row,) = await rows(lesson_heart)
    manifest = SkillParser().parse(markdown)
    assert row.name == NAME and row.active
    assert row.description == manifest.description
    assert "skill" in row.tags and BUILTIN_TAG in row.tags
    assert body_sha(markdown) in row.core_concepts
    assert manifest.raw_content in row.implementation_notes
    # Active and found by name, as the critic, get_procedure and recall find a skill.
    found = await lesson_heart.get_procedure_by_name(NAME)
    assert found is not None and found.id == row.id


@pytest.mark.asyncio
async def test_idempotent_second_run_no_duplicate(lesson_heart):  # noqa: F811
    assert await sync_builtin_skills(lesson_heart, True) == 1
    assert await sync_builtin_skills(lesson_heart, True) == 0
    (row,) = await rows(lesson_heart)
    assert row.active and row.version == 1  # no body write on an unchanged file


@pytest.mark.asyncio
async def test_off_with_no_row_stores_nothing(lesson_heart):  # noqa: F811
    assert await sync_builtin_skills(lesson_heart, False) == 0
    assert await rows(lesson_heart) == []


@pytest.mark.asyncio
async def test_off_deactivates_and_on_reactivates_same_row(lesson_heart):  # noqa: F811
    await sync_builtin_skills(lesson_heart, True)
    (first,) = await rows(lesson_heart)
    assert await sync_builtin_skills(lesson_heart, False) == 1
    (row,) = await rows(lesson_heart)
    assert row.id == first.id and not row.active
    assert row.runtime_metadata["retired"] == {"reason": "spec_skill_disabled"}
    assert await sync_builtin_skills(lesson_heart, False) == 0  # already off
    assert await sync_builtin_skills(lesson_heart, True) == 1
    (row,) = await rows(lesson_heart)
    assert row.id == first.id and row.active


@pytest.mark.asyncio
async def test_stays_off_through_reactivate_skills(lesson_heart):  # noqa: F811
    await sync_builtin_skills(lesson_heart, True)
    await sync_builtin_skills(lesson_heart, False)
    (row,) = await rows(lesson_heart)
    # reactivate_skills does see the retired row (a skill, not archived) ...
    assert row.id in {p.id for p in await lesson_heart.list_inactive_skill_procedures()}
    # ... and leaves it off: it re-enables only a skill whose requires are met, and this one has none.
    # Startup order: bootstrap_local_skills, reactivate_skills, then sync_builtin_skills.
    assert await reactivate_skills(lesson_heart) == 0
    assert await sync_builtin_skills(lesson_heart, False) == 0
    (row,) = await rows(lesson_heart)
    assert not row.active


@pytest.mark.asyncio
async def test_changed_body_reimports(lesson_heart, tmp_path, monkeypatch):  # noqa: F811
    await sync_builtin_skills(lesson_heart, True)
    (first,) = await rows(lesson_heart)
    markdown = edited_copy(tmp_path)
    monkeypatch.setattr(bootstrap, "BUILTIN_DIR", tmp_path)

    assert await sync_builtin_skills(lesson_heart, True) == 1
    (row,) = await rows(lesson_heart)
    assert row.id == first.id and row.active and row.version == 2
    assert row.description.startswith("Drafting a spec,")
    assert any("6. Say which steps were skipped" in note for note in row.implementation_notes)
    assert body_sha(markdown) in row.core_concepts
    assert body_sha(SKILL_MD.read_text(encoding="utf-8")) not in row.core_concepts
    assert BUILTIN_TAG in row.tags
    assert await sync_builtin_skills(lesson_heart, True) == 0  # the new hash is stored


@pytest.mark.asyncio
async def test_changed_body_while_off_reimports_and_reactivates(lesson_heart, tmp_path, monkeypatch):  # noqa: F811
    await sync_builtin_skills(lesson_heart, True)
    await sync_builtin_skills(lesson_heart, False)
    markdown = edited_copy(tmp_path)
    monkeypatch.setattr(bootstrap, "BUILTIN_DIR", tmp_path)
    assert await sync_builtin_skills(lesson_heart, False) == 0  # off: the edit waits
    (row,) = await rows(lesson_heart)
    assert not row.active and body_sha(markdown) not in row.core_concepts

    assert await sync_builtin_skills(lesson_heart, True) == 1
    (row,) = await rows(lesson_heart)
    assert row.active and body_sha(markdown) in row.core_concepts


@pytest.mark.asyncio
async def test_does_not_resurrect_consolidated(lesson_heart):  # noqa: F811
    await sync_builtin_skills(lesson_heart, True)
    (row,) = await rows(lesson_heart)
    canonical = await lesson_heart.store_procedure(
        ProcedureInput(name="spec-writing-canonical", description="The merged spec skill", tags=["skill"])
    )
    async with lesson_heart.db.session() as s:
        await s.execute(
            update(Procedure)
            .where(Procedure.id == row.id)
            .values(active=False, superseded_by=canonical.id, archived_at=datetime.now(UTC))
        )
        await s.commit()
    assert await lesson_heart.is_procedure_name_superseded(NAME)

    assert await sync_builtin_skills(lesson_heart, True) == 0
    await reactivate_skills(lesson_heart)
    (row,) = await rows(lesson_heart)
    assert not row.active and row.superseded_by == canonical.id


@pytest.mark.asyncio
async def test_failure_is_swallowed(lesson_heart, monkeypatch, caplog):  # noqa: F811
    async def boom(*_args, **_kwargs):
        raise RuntimeError("secret detail")

    monkeypatch.setattr(lesson_heart, "store_procedure", boom)
    with caplog.at_level(logging.WARNING, logger="nous.skills.bootstrap"):
        assert await sync_builtin_skills(lesson_heart, True) == 0
    assert "RuntimeError" in caplog.text and "secret detail" not in caplog.text
    assert await rows(lesson_heart) == []


@pytest.mark.asyncio
async def test_a_broken_file_does_not_stop_the_others(lesson_heart, tmp_path, monkeypatch, caplog):  # noqa: F811
    (tmp_path / "broken").mkdir()
    (tmp_path / "broken" / "SKILL.md").write_text("no front matter here\n", encoding="utf-8")
    (tmp_path / "spec-writing").mkdir()
    (tmp_path / "spec-writing" / "SKILL.md").write_text(SKILL_MD.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr(bootstrap, "BUILTIN_DIR", tmp_path)
    with caplog.at_level(logging.WARNING, logger="nous.skills.bootstrap"):
        assert await sync_builtin_skills(lesson_heart, True) == 1
    assert "ValueError" in caplog.text
    (row,) = await rows(lesson_heart)
    assert row.active


@pytest.mark.asyncio
async def test_unusable_heart_is_swallowed():
    assert await sync_builtin_skills(object(), True) == 0  # type: ignore[arg-type]
    assert await sync_builtin_skills(object(), False) == 0  # type: ignore[arg-type]


def test_spec_skill_setting_defaults_off_and_has_a_doc_row():
    assert Settings(_env_file=None).spec_skill_enabled is False
    doc = (ROOT / "docs" / "reference" / "environment-variables.md").read_text(encoding="utf-8")
    assert "| `NOUS_SPEC_SKILL_ENABLED` | `false` |" in doc
