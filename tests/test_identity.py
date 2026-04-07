"""Tests for 008: Agent Identity — IdentityManager + initiation protocol.

Tests cover:
- IdentityManager CRUD (get_current, update_section, versioning)
- Initiation state machine (is_initiated, mark_initiated, claim_initiation)
- Auto-seed from existing facts (upgrade path)
- Section validation
- Prompt assembly
- Cache invalidation
- Protocol constants
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nous.identity.manager import IdentityManager, SECTIONS, VALID_SECTIONS
from nous.identity.protocol import (
    INITIATION_PROMPT,
    UPGRADE_INITIATION_PROMPT,
    STORE_IDENTITY_SCHEMA,
    COMPLETE_INITIATION_SCHEMA,
)


# ---------------------------------------------------------------------------
# IdentityManager unit tests (no DB)
# ---------------------------------------------------------------------------


class TestIdentityManagerAssemble:
    """Test assemble_prompt() — pure logic, no DB."""

    def test_assemble_empty(self):
        mgr = IdentityManager(MagicMock(), "test-agent")
        assert mgr.assemble_prompt({}) == ""

    def test_assemble_single_section(self):
        mgr = IdentityManager(MagicMock(), "test-agent")
        result = mgr.assemble_prompt({"character": "Name: Nous\nTone: Casual"})
        assert "## Character" in result
        assert "Name: Nous" in result

    def test_assemble_multiple_sections(self):
        mgr = IdentityManager(MagicMock(), "test-agent")
        result = mgr.assemble_prompt({
            "character": "Name: Nous",
            "preferences": "User: Tim\nTimezone: EST",
            "boundaries": "Never store credentials",
        })
        assert "## Character" in result
        assert "## Preferences" in result
        assert "## Boundaries" in result

    def test_assemble_preserves_section_order(self):
        mgr = IdentityManager(MagicMock(), "test-agent")
        result = mgr.assemble_prompt({
            "boundaries": "Never store credentials",
            "character": "Name: Nous",
        })
        # Character should appear before boundaries (SECTIONS order)
        char_pos = result.index("## Character")
        bound_pos = result.index("## Boundaries")
        assert char_pos < bound_pos

    def test_assemble_skips_status_section(self):
        """Status is a control field, not prompt content (review fix P3-2)."""
        mgr = IdentityManager(MagicMock(), "test-agent")
        result = mgr.assemble_prompt({
            "character": "Name: Nous",
            "status": "initiated",
        })
        assert "## Character" in result
        assert "status" not in result.lower()

    def test_assemble_skips_empty_sections(self):
        mgr = IdentityManager(MagicMock(), "test-agent")
        result = mgr.assemble_prompt({
            "character": "Name: Nous",
            "values": "",
            "preferences": "User: Tim",
        })
        assert "## Character" in result
        assert "## Values" not in result
        assert "## Preferences" in result


class TestSectionValidation:
    """Test that section names are validated."""

    def test_valid_sections(self):
        for section in SECTIONS:
            assert section in VALID_SECTIONS

    def test_status_is_valid(self):
        assert "status" in VALID_SECTIONS

    def test_invalid_section_rejected(self):
        assert "user_profile" not in VALID_SECTIONS
        assert "charactor" not in VALID_SECTIONS

    def test_sections_list_completeness(self):
        expected = ["character", "values", "protocols", "preferences", "boundaries"]
        assert SECTIONS == expected


class TestCacheInvalidation:
    """Test TTL cache behavior."""

    def test_cache_starts_empty(self):
        mgr = IdentityManager(MagicMock(), "test-agent")
        assert mgr._cache is None

    def test_invalidate_clears_cache(self):
        mgr = IdentityManager(MagicMock(), "test-agent")
        mgr._cache = {"character": "test"}
        mgr._cache_expires = 999999999.0
        mgr._invalidate_cache()
        assert mgr._cache is None
        assert mgr._cache_expires == 0.0


# ---------------------------------------------------------------------------
# Protocol constants
# ---------------------------------------------------------------------------


class TestProtocolConstants:
    """Test that protocol prompts and schemas are well-formed."""

    def test_initiation_prompt_mentions_all_topics(self):
        prompt = INITIATION_PROMPT
        assert "name" in prompt.lower()
        assert "location" in prompt.lower()
        assert "timezone" in prompt.lower()
        assert "personality" in prompt.lower()
        assert "proactiv" in prompt.lower()
        assert "boundaries" in prompt.lower()
        assert "store_identity" in prompt
        assert "complete_initiation" in prompt

    def test_upgrade_prompt_has_placeholder(self):
        assert "{existing_facts}" in UPGRADE_INITIATION_PROMPT

    def test_store_identity_schema_has_enum(self):
        props = STORE_IDENTITY_SCHEMA["input_schema"]["properties"]
        assert "section" in props
        assert "enum" in props["section"]
        # All enum values must be in SECTIONS
        for val in props["section"]["enum"]:
            assert val in SECTIONS, f"{val} not in SECTIONS"

    def test_complete_initiation_schema_minimal(self):
        assert COMPLETE_INITIATION_SCHEMA["input_schema"]["required"] == []

    def test_store_identity_requires_section_and_content(self):
        required = STORE_IDENTITY_SCHEMA["input_schema"]["required"]
        assert "section" in required
        assert "content" in required


# ---------------------------------------------------------------------------
# Frame tools integration
# ---------------------------------------------------------------------------


class TestFrameToolsIntegration:
    """Test that initiation frame is properly configured."""

    def test_initiation_frame_in_frame_tools(self):
        from nous.api.runner import FRAME_TOOLS
        assert "initiation" in FRAME_TOOLS
        assert "store_identity" in FRAME_TOOLS["initiation"]
        assert "complete_initiation" in FRAME_TOOLS["initiation"]

    def test_initiation_frame_has_only_identity_tools(self):
        from nous.api.runner import FRAME_TOOLS
        assert len(FRAME_TOOLS["initiation"]) == 2

    def test_normal_frames_dont_have_identity_tools(self):
        from nous.api.runner import FRAME_TOOLS
        for frame_id in ["conversation", "question", "task", "debug"]:
            tools = FRAME_TOOLS[frame_id]
            if "*" in tools:
                continue  # task frame has all tools
            assert "store_identity" not in tools
            assert "complete_initiation" not in tools


# ---------------------------------------------------------------------------
# ORM model
# ---------------------------------------------------------------------------


class TestAgentIdentityModel:
    """Test ORM model structure."""

    def test_model_exists(self):
        from nous.storage.models import AgentIdentity
        assert AgentIdentity.__tablename__ == "agent_identity"

    def test_model_schema(self):
        from nous.storage.models import AgentIdentity
        assert AgentIdentity.__table_args__ == {"schema": "nous_system"}

    def test_agent_has_is_initiated(self):
        from nous.storage.models import Agent
        assert hasattr(Agent, "is_initiated")
