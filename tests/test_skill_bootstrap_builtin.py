"""F110 PR 6: sync_builtin_skills keeps the builtin skills in step with the flag and their files (real Postgres)."""

from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime
from pathlib import Path

import pytest
from job_support import lesson_heart, lesson_input  # noqa: F401
from sqlalchemy import func, select, update

from nous.config import Settings
from nous.heart.schemas import ProcedureInput
from nous.skills import bootstrap
from nous.skills.bootstrap import BUILTIN_TAG, bootstrap_local_skills, reactivate_skills, sync_builtin_skills
from nous.skills.parser import SkillParser
from nous.storage.models import Procedure

pytestmark = pytest.mark.postgres_only

ROOT = Path(__file__).resolve().parents[1]
NAME = "spec-writing-verify"
SKILL_MD = Path(bootstrap.__file__).parent / "builtin" / "spec-writing" / "SKILL.md"


def body_sha(markdown: str) -> str:
    return hashlib.sha256(markdown.encode("utf-8")).hexdigest()


def stored_sha(row: Procedure) -> str | None:
    """The file hash the sync keeps in the row's runtime_metadata."""
    return ((row.runtime_metadata or {}).get("builtin") or {}).get("body_sha")


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
    # The hash lives in runtime_metadata, which is neither embedded nor shown, not in core_concepts.
    assert row.runtime_metadata == {"builtin": {"body_sha": body_sha(markdown)}}
    assert row.core_concepts == ["general"]
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
    assert stored_sha(row) == body_sha(markdown)
    assert row.core_concepts == ["general"]
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
    assert not row.active and stored_sha(row) != body_sha(markdown)

    assert await sync_builtin_skills(lesson_heart, True) == 1
    (row,) = await rows(lesson_heart)
    assert row.active and stored_sha(row) == body_sha(markdown)


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


async def supersede(heart, procedure_id) -> None:
    """Archive a row as consolidated into a fresh canonical procedure."""
    canonical = await heart.store_procedure(
        ProcedureInput(name="spec-writing-canonical", description="The merged spec skill", tags=["skill"])
    )
    async with heart.db.session() as s:
        await s.execute(
            update(Procedure)
            .where(Procedure.id == procedure_id)
            .values(active=False, superseded_by=canonical.id, archived_at=datetime.now(UTC))
        )
        await s.commit()


@pytest.mark.asyncio
async def test_found_row_reimports_despite_a_superseded_namesake(lesson_heart, tmp_path, monkeypatch):  # noqa: F811
    """The consolidated-name skip applies only when no live row has the name: the builtin's own row follows the file."""
    await sync_builtin_skills(lesson_heart, True)
    (builtin,) = await rows(lesson_heart)
    old = await lesson_heart.store_procedure(
        ProcedureInput(name=NAME, description="An old copy", tags=["skill"], active=False)
    )
    await supersede(lesson_heart, old.id)
    assert await lesson_heart.is_procedure_name_superseded(NAME)
    markdown = edited_copy(tmp_path)
    monkeypatch.setattr(bootstrap, "BUILTIN_DIR", tmp_path)

    assert await sync_builtin_skills(lesson_heart, True) == 1
    by_id = {r.id: r for r in await rows(lesson_heart)}
    assert by_id[builtin.id].active and stored_sha(by_id[builtin.id]) == body_sha(markdown)
    assert not by_id[old.id].active and by_id[old.id].superseded_by is not None
    assert await sync_builtin_skills(lesson_heart, False) == 1
    assert not any(r.active for r in await rows(lesson_heart))


@pytest.mark.asyncio
async def test_same_name_row_without_tag_is_reclaimed(lesson_heart):  # noqa: F811
    """A same-name row without the builtin tag (learn_skill rewrote it, or a workspace copy) is the builtin's row."""
    agent_row = await lesson_heart.store_procedure(
        ProcedureInput(
            name=NAME, description="The agent's own spec notes", tags=["skill", "local"], core_concepts=["general"]
        )
    )
    assert await sync_builtin_skills(lesson_heart, True) == 1
    (row,) = await rows(lesson_heart)
    assert row.id == agent_row.id and row.active and row.version == 2
    assert BUILTIN_TAG in row.tags and stored_sha(row) == body_sha(SKILL_MD.read_text(encoding="utf-8"))
    assert row.description.startswith("Writing a spec,")
    assert await sync_builtin_skills(lesson_heart, True) == 0
    assert await sync_builtin_skills(lesson_heart, False) == 1
    (row,) = await rows(lesson_heart)
    assert row.id == agent_row.id and not row.active


@pytest.mark.asyncio
async def test_off_retires_the_live_row_among_same_name_rows(lesson_heart):  # noqa: F811
    """With an older active row and a newer inactive one of the name, off retires the active one."""
    await lesson_heart.store_procedure(ProcedureInput(name=NAME, description="Live", tags=["skill"]))
    await lesson_heart.store_procedure(
        ProcedureInput(name=NAME, description="Newer, off", tags=["skill"], active=False)
    )
    assert await sync_builtin_skills(lesson_heart, False) == 1
    assert [r.active for r in await rows(lesson_heart)] == [False, False]


@pytest.mark.asyncio
async def test_hash_merges_into_the_manifest_runtime_metadata(lesson_heart, tmp_path, monkeypatch):  # noqa: F811
    """F064.4 keys the manifest declares are kept beside the hash, on the store and on a re-import."""
    markdown = SKILL_MD.read_text(encoding="utf-8").replace(
        "name: spec-writing-verify\n", "name: spec-writing-verify\nconcurrency_cap: 2\n"
    )
    (tmp_path / "spec-writing").mkdir()
    path = tmp_path / "spec-writing" / "SKILL.md"
    path.write_text(markdown, encoding="utf-8")
    monkeypatch.setattr(bootstrap, "BUILTIN_DIR", tmp_path)
    assert await sync_builtin_skills(lesson_heart, True) == 1
    (row,) = await rows(lesson_heart)
    assert row.runtime_metadata["concurrency_cap"] == 2 and stored_sha(row) == body_sha(markdown)

    markdown += "6. Say which steps were skipped and why.\n"
    path.write_text(markdown, encoding="utf-8")
    assert await sync_builtin_skills(lesson_heart, True) == 1
    (row,) = await rows(lesson_heart)
    assert row.version == 2 and stored_sha(row) == body_sha(markdown)
    assert row.runtime_metadata["concurrency_cap"] == 2 and row.runtime_metadata["schema_version"] == 1


@pytest.mark.asyncio
async def test_a_skipped_reactivation_is_not_counted(lesson_heart, caplog):  # noqa: F811
    """When another active row holds the name, reactivate_procedure skips: not logged or counted as reactivated."""
    await sync_builtin_skills(lesson_heart, True)
    await sync_builtin_skills(lesson_heart, False)
    await lesson_heart.store_procedure(lesson_input("ab" * 16, NAME))  # an active job lesson holds the name
    with caplog.at_level(logging.INFO, logger="nous.skills.bootstrap"):
        assert await sync_builtin_skills(lesson_heart, True) == 0
    assert "reactivated" not in caplog.text
    (builtin,) = [r for r in await rows(lesson_heart) if r.kind is None]
    assert not builtin.active


@pytest.mark.asyncio
async def test_workspace_skill_named_after_a_builtin_is_skipped(lesson_heart, tmp_path, caplog):  # noqa: F811
    """bootstrap_local_skills leaves a builtin's name to the builtin (any case), and still loads other skills."""
    skills = tmp_path / "skills"
    (skills / "my-spec").mkdir(parents=True)
    workspace_copy = SKILL_MD.read_text(encoding="utf-8").replace(
        "name: spec-writing-verify", "name: Spec-Writing-Verify"
    )
    (skills / "my-spec" / "SKILL.md").write_text(workspace_copy, encoding="utf-8")
    (skills / "other").mkdir()
    (skills / "other" / "SKILL.md").write_text(
        "---\nname: other-workspace-skill\ndescription: Something else\n---\nBody\n", encoding="utf-8"
    )
    with caplog.at_level(logging.INFO, logger="nous.skills.bootstrap"):
        assert await bootstrap_local_skills(str(tmp_path), lesson_heart) == 1
    assert await rows(lesson_heart) == []
    assert "Spec-Writing-Verify" in caplog.text
    assert await lesson_heart.get_procedure_by_name("other-workspace-skill") is not None


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
