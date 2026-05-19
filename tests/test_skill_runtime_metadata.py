"""Tests for F064.4 — workflow-as-code skill manifest fields (v1 partial).

Covers plan §7.5 acceptance + the codex P1-style silent-drop guarantee
(always-persist semantic): manifest fields are parsed and embedded into
ProcedureInput.runtime_metadata regardless of the
NOUS_SKILL_RUNTIME_METADATA_ENABLED flag (that flag only gates the
deferred-to-v2 orchestrator consumer).
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import select

from nous.skills.parser import SkillManifest, SkillParser
from nous.heart.procedures import ProcedureManager
from nous.heart.schemas import ProcedureInput
from nous.storage.models import Procedure


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


class TestSkillManifestParse:
    def test_parser_extracts_all_runtime_fields(self):
        md = """---
name: test-skill
description: A test skill
domain: testing
concurrency_cap: 2
timeout_override_seconds: 600
requires_human_review: true
hooks:
  before_run: echo before
  after_run: echo after
---

## Usage

Do the thing.
"""
        m = SkillParser().parse(md)
        assert m.concurrency_cap == 2
        assert m.timeout_override_seconds == 600
        assert m.requires_human_review is True
        assert m.hooks == {"before_run": "echo before", "after_run": "echo after"}

    def test_parser_defaults_when_runtime_fields_absent(self):
        md = """---
name: minimal-skill
description: minimal
domain: testing
---

## Section

body
"""
        m = SkillParser().parse(md)
        assert m.concurrency_cap is None
        assert m.timeout_override_seconds is None
        assert m.hooks == {}
        assert m.requires_human_review is False

    def test_parser_rejects_zero_concurrency_cap(self):
        md = """---
name: bad-skill
description: bad
domain: testing
concurrency_cap: 0
---

## S
b
"""
        with pytest.raises(ValueError, match="concurrency_cap"):
            SkillParser().parse(md)

    def test_parser_rejects_negative_timeout_override(self):
        md = """---
name: bad-skill
description: bad
domain: testing
timeout_override_seconds: -10
---

## S
b
"""
        with pytest.raises(ValueError, match="timeout_override_seconds"):
            SkillParser().parse(md)

    def test_parser_rejects_typo_in_requires_human_review(self):
        """@codex P2 on dc914be: a typo like 'tru' previously coerced
        silently to False. Now raises — closes the silent-drop hole."""
        md = """---
name: bad
description: d
domain: testing
requires_human_review: tru
---
## U
"""
        with pytest.raises(ValueError, match="requires_human_review"):
            SkillParser().parse(md)

    def test_parser_rejects_invalid_int_for_requires_human_review(self):
        md = """---
name: bad
description: d
domain: testing
requires_human_review: 42
---
## U
"""
        with pytest.raises(ValueError, match="requires_human_review"):
            SkillParser().parse(md)

    def test_parser_rejects_hooks_as_list(self):
        """@codex P2 on 2399032: falsy non-dict hooks (empty list, empty
        string) previously bypassed validation and coerced to {}. Now they
        raise — no silent drop."""
        md = """---
name: bad-hooks
description: d
domain: testing
hooks: [before_run, after_run]
---
## U
"""
        with pytest.raises(ValueError, match="hooks"):
            SkillParser().parse(md)

    def test_parser_rejects_hooks_as_string(self):
        md = """---
name: bad-hooks
description: d
domain: testing
hooks: just a string
---
## U
"""
        with pytest.raises(ValueError, match="hooks"):
            SkillParser().parse(md)

    def test_parser_accepts_requires_human_review_string_forms(self):
        for v in ["true", "True", "yes", "1"]:
            md = f"""---
name: skill
description: d
domain: testing
requires_human_review: {v}
---

## S
b
"""
            m = SkillParser().parse(md)
            assert m.requires_human_review is True, f"failed for {v!r}"

        for v in ["false", "no", "0"]:
            md = f"""---
name: skill
description: d
domain: testing
requires_human_review: {v}
---

## S
b
"""
            m = SkillParser().parse(md)
            assert m.requires_human_review is False, f"failed for {v!r}"


# ---------------------------------------------------------------------------
# Always-persist semantic (codex P1 silent-drop fix)
# ---------------------------------------------------------------------------


class TestAlwaysPersist:
    def test_to_procedure_input_embeds_runtime_metadata_unconditionally(self):
        """The skill_runtime_metadata_enabled flag is irrelevant at this layer.
        A manifest declaring concurrency_cap=1 always produces a ProcedureInput
        with runtime_metadata set."""
        md = """---
name: skill
description: d
domain: testing
concurrency_cap: 1
---

## Usage
"""
        manifest = SkillParser().parse(md)
        pi = SkillParser().to_procedure_input(manifest)
        assert pi.runtime_metadata is not None
        assert pi.runtime_metadata["concurrency_cap"] == 1
        assert pi.runtime_metadata["schema_version"] == 1

    def test_runtime_metadata_none_when_no_fields_declared(self):
        md = """---
name: skill
description: d
domain: testing
---

## Usage
"""
        manifest = SkillParser().parse(md)
        pi = SkillParser().to_procedure_input(manifest)
        assert pi.runtime_metadata is None

    def test_runtime_metadata_present_when_any_field_set(self):
        """ANY of the four fields → dict present (with schema_version)."""
        md = """---
name: skill
description: d
domain: testing
requires_human_review: true
---

## Usage
"""
        manifest = SkillParser().parse(md)
        pi = SkillParser().to_procedure_input(manifest)
        assert pi.runtime_metadata is not None
        assert pi.runtime_metadata["requires_human_review"] is True
        # Other fields default to None / {} / False
        assert pi.runtime_metadata["concurrency_cap"] is None
        assert pi.runtime_metadata["timeout_override_seconds"] is None
        assert pi.runtime_metadata["hooks"] == {}

    def test_runtime_metadata_includes_schema_version_for_v2_drift_detection(self):
        md = """---
name: skill
description: d
domain: testing
concurrency_cap: 3
---
## U
"""
        manifest = SkillParser().parse(md)
        pi = SkillParser().to_procedure_input(manifest)
        assert pi.runtime_metadata is not None
        # schema_version present so F064.4-v2 can detect dict-shape drift.
        assert pi.runtime_metadata["schema_version"] == 1


# ---------------------------------------------------------------------------
# Persistence integration
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def procedures_mgr(db):
    """ProcedureManager with no embedding provider (faster tests, no API calls)."""
    return ProcedureManager(
        db=db,
        agent_id=f"test-skill-meta-{uuid.uuid4().hex[:8]}",
        embeddings=None,
    )


class TestPersistence:
    @pytest.mark.asyncio
    async def test_runtime_metadata_persisted_when_consumer_flag_off(
        self, procedures_mgr, db, monkeypatch
    ):
        """The acceptance test: a manifest declaring runtime fields persists
        them to heart.procedures.runtime_metadata REGARDLESS of the
        NOUS_SKILL_RUNTIME_METADATA_ENABLED flag (closes the silent-drop hole).
        """
        monkeypatch.setenv("NOUS_SKILL_RUNTIME_METADATA_ENABLED", "false")
        md = """---
name: persisted-skill
description: ships v1
domain: testing
concurrency_cap: 1
requires_human_review: true
hooks:
  before_run: echo before
---
## Usage
"""
        manifest = SkillParser().parse(md)
        pi = SkillParser().to_procedure_input(manifest)
        detail = await procedures_mgr.store(pi)
        # Read back from DB and verify runtime_metadata is intact.
        async with db.session() as session:
            row = await session.scalar(
                select(Procedure).where(Procedure.id == detail.id)
            )
            assert row is not None
            assert row.runtime_metadata is not None
            assert row.runtime_metadata["concurrency_cap"] == 1
            assert row.runtime_metadata["requires_human_review"] is True
            assert row.runtime_metadata["hooks"] == {"before_run": "echo before"}
            assert row.runtime_metadata["schema_version"] == 1

    @pytest.mark.asyncio
    async def test_runtime_metadata_null_for_legacy_manifests(
        self, procedures_mgr, db
    ):
        """A manifest that doesn't declare any runtime field stores NULL —
        legacy data shape preserved."""
        md = """---
name: legacy-skill
description: no runtime fields
domain: testing
---

## Usage
"""
        manifest = SkillParser().parse(md)
        pi = SkillParser().to_procedure_input(manifest)
        detail = await procedures_mgr.store(pi)
        async with db.session() as session:
            row = await session.scalar(
                select(Procedure).where(Procedure.id == detail.id)
            )
            assert row is not None
            assert row.runtime_metadata is None
