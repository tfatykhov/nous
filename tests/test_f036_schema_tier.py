"""Tests for F036 schema tier classification and context engine tier grouping."""

from __future__ import annotations

from nous.cognitive.context import SECTION_TIERS
from nous.cognitive.schemas import BuildResult, ContextSection, TurnContext


# --- ContextSection tier field ---


def test_context_section_default_tier_is_dynamic() -> None:
    section = ContextSection(priority=1, label="Test", content="hello", token_estimate=10)
    assert section.tier == "dynamic"


def test_context_section_accepts_static_tier() -> None:
    section = ContextSection(priority=1, label="Test", content="hello", token_estimate=10, tier="static")
    assert section.tier == "static"


def test_context_section_accepts_semi_stable_tier() -> None:
    section = ContextSection(priority=1, label="Test", content="hello", token_estimate=10, tier="semi_stable")
    assert section.tier == "semi_stable"


# --- BuildResult sections_by_tier ---


def test_build_result_sections_by_tier_default_empty() -> None:
    result = BuildResult(system_prompt="test")
    assert result.sections_by_tier == {}


def test_build_result_sections_by_tier_with_data() -> None:
    tiers = {"static": "## Identity\n\nI am Nous", "dynamic": "## Facts\n\nSome facts"}
    result = BuildResult(system_prompt="test", sections_by_tier=tiers)
    assert result.sections_by_tier == tiers
    assert "static" in result.sections_by_tier
    assert "dynamic" in result.sections_by_tier


# --- TurnContext sections_by_tier ---


def test_turn_context_sections_by_tier_default_empty() -> None:
    from nous.cognitive.schemas import FrameSelection

    ctx = TurnContext(
        system_prompt="test",
        frame=FrameSelection(
            frame_id="conversation",
            frame_name="Conversation",
            confidence=0.9,
            match_method="default",
        ),
    )
    assert ctx.sections_by_tier == {}


# --- SECTION_TIERS mapping ---


def test_section_tiers_identity_is_static() -> None:
    assert SECTION_TIERS["Identity"] == "static"


def test_section_tiers_context_safety_is_static() -> None:
    assert SECTION_TIERS["Context Safety"] == "static"


def test_section_tiers_user_profile_is_semi_stable() -> None:
    assert SECTION_TIERS["User Profile"] == "semi_stable"


def test_section_tiers_active_censors_is_semi_stable() -> None:
    assert SECTION_TIERS["Active Censors"] == "semi_stable"


def test_section_tiers_current_frame_is_semi_stable() -> None:
    assert SECTION_TIERS["Current Frame"] == "semi_stable"


# --- Tier grouping logic ---


def test_tier_grouping_groups_sections_correctly() -> None:
    """Replicate the tier grouping logic from ContextEngine.build and verify output."""
    sections = [
        ContextSection(priority=1, label="Identity", content="I am Nous", token_estimate=10, tier="static"),
        ContextSection(priority=2, label="Context Safety", content="Be safe", token_estimate=8, tier="static"),
        ContextSection(priority=3, label="User Profile", content="User prefs", token_estimate=12, tier="semi_stable"),
        ContextSection(priority=5, label="Relevant Facts", content="Fact A", token_estimate=15, tier="dynamic"),
        ContextSection(priority=6, label="Past Episodes", content="Episode X", token_estimate=20, tier="dynamic"),
    ]

    # Mirror the grouping logic from context.py lines 644-655
    tier_groups: dict[str, list[str]] = {"static": [], "semi_stable": [], "dynamic": []}
    for section in sorted(sections, key=lambda s: s.priority):
        tier = section.tier
        if tier not in tier_groups:
            tier = "dynamic"
        tier_groups[tier].append(f"## {section.label}\n\n{section.content}")

    sections_by_tier = {
        tier: "\n\n".join(parts)
        for tier, parts in tier_groups.items()
        if parts
    }

    assert "static" in sections_by_tier
    assert "semi_stable" in sections_by_tier
    assert "dynamic" in sections_by_tier

    assert "## Identity" in sections_by_tier["static"]
    assert "## Context Safety" in sections_by_tier["static"]
    assert "## User Profile" in sections_by_tier["semi_stable"]
    assert "## Relevant Facts" in sections_by_tier["dynamic"]
    assert "## Past Episodes" in sections_by_tier["dynamic"]


def test_tier_grouping_unknown_tier_falls_back_to_dynamic() -> None:
    """Sections with unrecognized tier values should fall back to dynamic."""
    sections = [
        ContextSection(priority=1, label="Weird", content="weird content", token_estimate=5, tier="unknown_tier"),
    ]

    tier_groups: dict[str, list[str]] = {"static": [], "semi_stable": [], "dynamic": []}
    for section in sorted(sections, key=lambda s: s.priority):
        tier = section.tier
        if tier not in tier_groups:
            tier = "dynamic"
        tier_groups[tier].append(f"## {section.label}\n\n{section.content}")

    sections_by_tier = {
        tier: "\n\n".join(parts)
        for tier, parts in tier_groups.items()
        if parts
    }

    assert "static" not in sections_by_tier
    assert "semi_stable" not in sections_by_tier
    assert "dynamic" in sections_by_tier
    assert "## Weird" in sections_by_tier["dynamic"]


def test_tier_grouping_empty_tiers_excluded() -> None:
    """Tiers with no sections should not appear in the output dict."""
    sections = [
        ContextSection(priority=1, label="Identity", content="I am Nous", token_estimate=10, tier="static"),
    ]

    tier_groups: dict[str, list[str]] = {"static": [], "semi_stable": [], "dynamic": []}
    for section in sorted(sections, key=lambda s: s.priority):
        tier = section.tier
        if tier not in tier_groups:
            tier = "dynamic"
        tier_groups[tier].append(f"## {section.label}\n\n{section.content}")

    sections_by_tier = {
        tier: "\n\n".join(parts)
        for tier, parts in tier_groups.items()
        if parts
    }

    assert "static" in sections_by_tier
    assert "semi_stable" not in sections_by_tier
    assert "dynamic" not in sections_by_tier
