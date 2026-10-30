"""Recalled memory that names a tool that changed gets one line saying what the tool is now.

It goes on the dynamic memory sections of the context (never a static or cached one) and on the output of
recall_deep and get_procedure, and it lists only the changed names the text actually contains.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from test_tool_names_canonical import _procedure

from nous.api.tools import create_nous_tools
from nous.cognitive.context import RECALLED_MEMORY_SECTIONS, SECTION_TIERS, ContextEngine
from nous.cognitive.schemas import ContextSection

NOTE_RECENT = "Note: some memories name tools that changed: recall_recent → recall_deep(hours=…)."


def _section(label: str, content: str, tier: str = "dynamic") -> ContextSection:
    return ContextSection(priority=5, label=label, content=content, token_estimate=10, tier=tier)


def _engine() -> ContextEngine:
    return object.__new__(ContextEngine)  # _note_changed_tools needs nothing the constructor sets


def test_a_memory_section_naming_a_changed_tool_ends_with_the_note():
    facts = _section("Relevant Facts", "- Use recall_recent for a recap\n- Use bash for builds")
    _engine()._note_changed_tools([facts])
    assert facts.content.splitlines()[-1] == NOTE_RECENT
    assert facts.content.count("Note:") == 1 and facts.token_estimate > 10


def test_only_the_names_present_are_listed():
    episodes = _section("Past Episodes", "- resolved it with resolve_decision")
    _engine()._note_changed_tools([episodes])
    assert episodes.content.splitlines()[-1] == (
        "Note: some memories name tools that changed: resolve_decision → resolve_decisions."
    )


@pytest.mark.parametrize(
    "section",
    [
        _section("Relevant Facts", "- Use recall_deep(hours=24) and resolve_decisions"),  # current names only
        _section("Procedure Catalog", "- Catch up: uses recall_recent", tier="static"),  # a static section
        _section("User Profile", "- Rule: always recall_recent first", tier="semi_stable"),  # a cached tier
        _section("Working Memory", "Current task: replace recall_recent"),  # not recalled memory
    ],
)
def test_no_note_without_a_changed_name_in_a_dynamic_memory_section(section):
    before = section.content
    _engine()._note_changed_tools([section])
    assert section.content == before


def test_every_memory_section_is_dynamic():
    assert all(SECTION_TIERS.get(label, "dynamic") == "dynamic" for label in RECALLED_MEMORY_SECTIONS)


@pytest.mark.asyncio
async def test_get_procedure_output_gets_the_note_only_when_its_text_names_a_changed_tool():
    heart = MagicMock()
    heart.get_procedure_by_name = AsyncMock(
        return_value=_procedure(["recall_recent"], notes=["Then call recall_hubs on the topic."])
    )
    text = (await create_nous_tools(MagicMock(), heart)["get_procedure"](procedure_id="Catch up"))["content"][0]["text"]
    # core_tools are rendered under the current name, so only the step that still names recall_hubs is noted
    assert (
        text.splitlines()[-1]
        == "Note: some memories name tools that changed: recall_hubs was retired (use recall_deep)."
    )
    heart.get_procedure_by_name = AsyncMock(return_value=_procedure(["recall_recent"], notes=["Then summarize."]))
    text = (await create_nous_tools(MagicMock(), heart)["get_procedure"](procedure_id="Catch up"))["content"][0]["text"]
    assert "Note:" not in text
