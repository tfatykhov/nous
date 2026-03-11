"""Tests for F011 Skill Discovery — SkillParser and learn_skill tool."""

from __future__ import annotations

import pytest

from nous.skills.parser import SkillManifest, SkillParser, _parse_frontmatter


# ---------------------------------------------------------------------------
# Frontmatter parsing
# ---------------------------------------------------------------------------

class TestParseFrontmatter:
    def test_scalar_values(self):
        text = 'name: my-skill\ndescription: A test skill'
        result = _parse_frontmatter(text)
        assert result["name"] == "my-skill"
        assert result["description"] == "A test skill"

    def test_quoted_values(self):
        text = 'version: "1.0"\nname: \'my-skill\''
        result = _parse_frontmatter(text)
        assert result["version"] == "1.0"
        assert result["name"] == "my-skill"

    def test_inline_list(self):
        text = "triggers: [web search, google, find online]"
        result = _parse_frontmatter(text)
        assert result["triggers"] == ["web search", "google", "find online"]

    def test_block_list(self):
        text = "triggers:\n  - web search\n  - google\n  - find online"
        result = _parse_frontmatter(text)
        assert result["triggers"] == ["web search", "google", "find online"]

    def test_mixed_scalars_and_lists(self):
        text = (
            "name: serper-search\n"
            "description: Google search\n"
            "triggers:\n"
            "  - web search\n"
            "  - google\n"
            "domain: research"
        )
        result = _parse_frontmatter(text)
        assert result["name"] == "serper-search"
        assert result["triggers"] == ["web search", "google"]
        assert result["domain"] == "research"

    def test_empty_value_starts_block_list(self):
        text = "tools:\n  - web_search\n  - web_fetch"
        result = _parse_frontmatter(text)
        assert result["tools"] == ["web_search", "web_fetch"]


# ---------------------------------------------------------------------------
# SkillParser.parse()
# ---------------------------------------------------------------------------

MINIMAL_SKILL_MD = """\
---
name: test-skill
description: A minimal test skill
domain: testing
---

# Test Skill

## When to Use
Use this when testing.

## Details
More details here.
"""

FULL_SKILL_MD = """\
---
name: serper-search
description: Google search via Serper.dev API
domain: research
triggers:
  - web search
  - google
  - find online
  - research
  - look up
frames:
  - task
  - debug
tools:
  - web_search
  - web_fetch
requires:
  - SERPER_API_KEY
source_url: https://clawhub.com/skills/serper
version: "1.0"
---

# Serper Search

## When to Use
Use this skill when the user asks you to search the web, look something up online,
or find information about a topic.

## How to Use
1. Call web_search with the query
2. Parse the results
3. Summarize findings
"""


class TestSkillParser:
    def setup_method(self):
        self.parser = SkillParser()

    def test_parse_minimal(self):
        manifest = self.parser.parse(MINIMAL_SKILL_MD)
        assert manifest.name == "test-skill"
        assert manifest.description == "A minimal test skill"
        assert manifest.domain == "testing"
        assert manifest.triggers == []
        assert manifest.frames == []
        assert manifest.tools == []
        assert manifest.requires == []
        assert manifest.source_url is None
        assert manifest.version is None

    def test_parse_full(self):
        manifest = self.parser.parse(FULL_SKILL_MD)
        assert manifest.name == "serper-search"
        assert manifest.description == "Google search via Serper.dev API"
        assert manifest.domain == "research"
        assert manifest.triggers == ["web search", "google", "find online", "research", "look up"]
        assert manifest.frames == ["task", "debug"]
        assert manifest.tools == ["web_search", "web_fetch"]
        assert manifest.requires == ["SERPER_API_KEY"]
        assert manifest.source_url == "https://clawhub.com/skills/serper"
        assert manifest.version == "1.0"

    def test_parse_first_section(self):
        manifest = self.parser.parse(FULL_SKILL_MD)
        assert "Use this skill when the user asks" in manifest.first_section
        # Should NOT contain the second H2 section
        assert "How to Use" not in manifest.first_section

    def test_parse_raw_content(self):
        manifest = self.parser.parse(FULL_SKILL_MD)
        assert "# Serper Search" in manifest.raw_content
        assert "---" not in manifest.raw_content  # frontmatter stripped

    def test_parse_no_frontmatter_raises(self):
        with pytest.raises(ValueError, match="frontmatter"):
            self.parser.parse("# Just a heading\nNo frontmatter here.")

    def test_parse_missing_name_raises(self):
        md = "---\ndescription: no name\n---\nBody"
        with pytest.raises(ValueError, match="name"):
            self.parser.parse(md)

    def test_parse_missing_description_raises(self):
        md = "---\nname: test\n---\nBody"
        with pytest.raises(ValueError, match="description"):
            self.parser.parse(md)

    def test_parse_default_domain(self):
        md = "---\nname: test\ndescription: test skill\n---\nBody"
        manifest = self.parser.parse(md)
        assert manifest.domain == "general"

    def test_parse_source_hint(self):
        md = "---\nname: test\ndescription: test skill\n---\nBody"
        manifest = self.parser.parse(md, source_hint="https://example.com/skill.md")
        assert manifest.source_url == "https://example.com/skill.md"

    def test_parse_source_url_overrides_hint(self):
        manifest = self.parser.parse(FULL_SKILL_MD, source_hint="https://other.com")
        assert manifest.source_url == "https://clawhub.com/skills/serper"


# ---------------------------------------------------------------------------
# SkillParser.to_procedure_input()
# ---------------------------------------------------------------------------

class TestToProcedureInput:
    def setup_method(self):
        self.parser = SkillParser()

    def test_converts_to_procedure_input(self):
        manifest = self.parser.parse(FULL_SKILL_MD)
        proc = self.parser.to_procedure_input(manifest)

        assert proc.name == "serper-search"
        assert proc.domain == "research"
        assert proc.description == "Google search via Serper.dev API"
        assert proc.goals == ["web search", "google", "find online", "research", "look up"]
        assert proc.core_tools == ["web_search", "web_fetch"]
        assert proc.core_patterns == ["web search", "google", "find online", "research", "look up"]
        assert "research" in proc.core_concepts
        assert "SERPER_API_KEY" in proc.core_concepts

    def test_tags_include_skill_and_frames(self):
        manifest = self.parser.parse(FULL_SKILL_MD)
        proc = self.parser.to_procedure_input(manifest)

        assert "skill" in proc.tags
        assert "task" in proc.tags
        assert "debug" in proc.tags

    def test_marketplace_tag_for_clawhub(self):
        manifest = self.parser.parse(FULL_SKILL_MD)
        proc = self.parser.to_procedure_input(manifest)
        assert "marketplace" in proc.tags

    def test_local_tag_for_local_skills(self):
        md = "---\nname: local-skill\ndescription: test\ndomain: general\n---\nBody"
        manifest = self.parser.parse(md)
        proc = self.parser.to_procedure_input(manifest)
        assert "local" in proc.tags
        assert "marketplace" not in proc.tags

    def test_implementation_notes_include_source(self):
        manifest = self.parser.parse(FULL_SKILL_MD)
        proc = self.parser.to_procedure_input(manifest)

        notes = proc.implementation_notes
        assert any("source:https://clawhub.com" in n for n in notes)
        assert any("version:1.0" in n for n in notes)

    def test_first_section_in_implementation_notes(self):
        manifest = self.parser.parse(FULL_SKILL_MD)
        proc = self.parser.to_procedure_input(manifest)
        assert any("Use this skill when" in n for n in proc.implementation_notes)


# ---------------------------------------------------------------------------
# learn_skill tool (unit test with mocked Heart)
# ---------------------------------------------------------------------------

class TestLearnSkillTool:
    """Test the learn_skill tool closure via create_nous_tools."""

    @pytest.fixture
    def mock_heart(self):
        """Create a minimal mock Heart."""
        from unittest.mock import AsyncMock, MagicMock
        from uuid import uuid4

        heart = MagicMock()
        heart.search_procedures = AsyncMock(return_value=[])

        mock_detail = MagicMock()
        mock_detail.id = uuid4()
        mock_detail.active = True
        heart.store_procedure = AsyncMock(return_value=mock_detail)
        heart.retire_procedure = AsyncMock()

        return heart

    @pytest.fixture
    def mock_brain(self):
        from unittest.mock import MagicMock
        return MagicMock()

    @pytest.fixture
    def mock_settings(self):
        from unittest.mock import MagicMock
        s = MagicMock()
        s.workspace_dir = "."
        return s

    @pytest.mark.asyncio
    async def test_learn_skill_inline(self, mock_brain, mock_heart, mock_settings):
        from nous.api.tools import create_nous_tools

        tools = create_nous_tools(mock_brain, mock_heart, settings=mock_settings)
        learn_skill = tools["learn_skill"]

        result = await learn_skill(
            source="inline",
            content=FULL_SKILL_MD,
        )

        text = result["content"][0]["text"]
        assert "registered successfully" in text
        assert "serper-search" in text
        mock_heart.store_procedure.assert_called_once()

    @pytest.mark.asyncio
    async def test_learn_skill_inline_no_content(self, mock_brain, mock_heart, mock_settings):
        from nous.api.tools import create_nous_tools

        tools = create_nous_tools(mock_brain, mock_heart, settings=mock_settings)
        result = await tools["learn_skill"](source="inline")

        text = result["content"][0]["text"]
        assert "required" in text.lower()

    @pytest.mark.asyncio
    async def test_learn_skill_invalid_markdown(self, mock_brain, mock_heart, mock_settings):
        from nous.api.tools import create_nous_tools

        tools = create_nous_tools(mock_brain, mock_heart, settings=mock_settings)
        result = await tools["learn_skill"](source="inline", content="no frontmatter")

        text = result["content"][0]["text"]
        assert "Parse error" in text

    @pytest.mark.asyncio
    async def test_learn_skill_dedup_updates(self, mock_brain, mock_heart, mock_settings):
        from unittest.mock import MagicMock
        from uuid import uuid4
        from nous.api.tools import create_nous_tools

        # Simulate existing procedure with same name
        existing = MagicMock()
        existing.name = "serper-search"
        existing.id = uuid4()
        mock_heart.search_procedures.return_value = [existing]

        tools = create_nous_tools(mock_brain, mock_heart, settings=mock_settings)
        result = await tools["learn_skill"](source="inline", content=FULL_SKILL_MD)

        text = result["content"][0]["text"]
        assert "updated successfully" in text
        mock_heart.retire_procedure.assert_called_once_with(existing.id)
        mock_heart.store_procedure.assert_called_once()

    @pytest.mark.asyncio
    async def test_learn_skill_local_file_not_found(self, mock_brain, mock_heart, mock_settings):
        from nous.api.tools import create_nous_tools

        tools = create_nous_tools(mock_brain, mock_heart, settings=mock_settings)
        result = await tools["learn_skill"](source="nonexistent/SKILL.md")

        text = result["content"][0]["text"]
        assert "not found" in text.lower()


# ---------------------------------------------------------------------------
# ProcedureSummary description field
# ---------------------------------------------------------------------------

class TestProcedureSummaryDescription:
    def test_summary_has_description_field(self):
        from nous.heart.schemas import ProcedureSummary
        from uuid import uuid4

        summary = ProcedureSummary(
            id=uuid4(),
            name="test-skill",
            domain="general",
            activation_count=0,
            effectiveness=None,
            description="A test skill description",
        )
        assert summary.description == "A test skill description"

    def test_summary_description_defaults_none(self):
        from nous.heart.schemas import ProcedureSummary
        from uuid import uuid4

        summary = ProcedureSummary(
            id=uuid4(),
            name="test-skill",
            domain="general",
            activation_count=0,
            effectiveness=None,
        )
        assert summary.description is None


# ---------------------------------------------------------------------------
# Active filter in search_procedures
# ---------------------------------------------------------------------------

class TestActiveFilter:
    @pytest.mark.asyncio
    async def test_search_passes_active_filter_to_hybrid_search(self):
        from unittest.mock import AsyncMock, MagicMock, patch
        from nous.heart.procedures import ProcedureManager

        db = MagicMock()
        mgr = ProcedureManager(db, embeddings=None, agent_id="test-agent")
        mock_session = MagicMock()

        with patch("nous.heart.procedures.hybrid_search", new_callable=AsyncMock) as mock_hs:
            mock_hs.return_value = []
            await mgr._search("test query", 10, None, mock_session)

            _, kwargs = mock_hs.call_args
            assert "active" in kwargs.get("extra_where", "")
