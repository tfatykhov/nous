"""F108 Task 10: the context estimate names every section it counts, and counts the preamble and tool results.

Every ``## `` heading the context builder emits is either in ``SECTION_MARKERS`` or reported as itself, never folded
into the section before it. System block 0 (the preamble) is the section ``preamble``, and a ``tool_result``'s content
counts toward the message estimate. Two guards keep the marker table complete: real ``ContextEngine`` builds that turn
on every optional section, and a scan of the source files that write headings.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

import nous
from nous.api.preamble import CLAUDE_CODE_PREAMBLE
from nous.cognitive.context import ContextEngine
from nous.cognitive.schemas import FrameSelection
from nous.config import Settings
from nous.heart.schemas import ProcedureSummary
from nous.observability.context_logger import ContextLogEntry
from nous.observability.model_calls import (
    SECTION_MARKERS,
    _estimate_messages_tokens,
    analyze_context,
    parse_system_sections,
)

# ---------------------------------------------------------------------------------------------------------------------
# The parser
# ---------------------------------------------------------------------------------------------------------------------


def test_unknown_heading_is_reported_as_itself():
    prompt = (
        "## Relevant Facts\n- [user] likes tea\n"
        "## Foo\nbar baz\n"
        "## Brand New Thing (v2)\nsomething else\n"
        "## Current Frame\ntask"
    )
    sections = parse_system_sections(prompt)
    assert sections == {
        "relevant_facts": "- [user] likes tea",
        "foo": "bar baz",
        "brand_new_thing_v2": "something else",
        "frame": "task",
    }
    # a deeper heading is content, not a section
    assert parse_system_sections("## Past Episodes\n- [success] one\n### detail\nmore") == {
        "past_episodes": "- [success] one\n### detail\nmore"
    }


def test_headings_inside_the_identity_and_skill_bodies_stay_in_their_section():
    """The identity prompt carries its own ``## Character`` ... headings (identity/manager.py ``assemble_prompt``) and a
    recommended skill's body is rendered verbatim: an unknown heading inside either stays part of it, so the
    ``identity`` and ``recommended_procedures`` numbers the dashboards show do not collapse. A heading in the table
    still starts its section."""
    prompt = (
        "## Identity\n\n## Character\nCurious.\n\n## Values\nHonest.\n\n"
        "## Recommended Procedures\n\n### deploy (ops)\n## Steps\n1. build\n\n"
        "## Past Episodes\n- [success] shipped\n"
        "## Foo\nbar"
    )
    sections = parse_system_sections(prompt)
    assert sections["identity"] == "## Character\nCurious.\n\n## Values\nHonest."
    assert sections["recommended_procedures"] == "### deploy (ops)\n## Steps\n1. build"
    assert sections["past_episodes"] == "- [success] shipped"
    assert sections["foo"] == "bar"
    assert set(sections) == {"identity", "recommended_procedures", "past_episodes", "foo"}


MISSING_HEADINGS = {
    "## Recall Before Clarifying": "recall_before_clarifying",
    "## Tools Outrank Memory": "tools_outrank_memory",
    "## Procedure Awareness": "procedure_awareness",
    "## Epistemic Routing": "epistemic_routing",
    "## Cached Results": "cached_results",
    "## Memory Retrieval Notice": "memory_retrieval_notice",
    "## Retrieval Failures": "retrieval_failures",
    "## Active Guidance": "active_guidance",
    "[DIAGNOSTIC OBSERVATIONS]": "diagnostic_nudges",  # the critic's nudges (critic.py format_nudges)
}


@pytest.mark.parametrize(("heading", "name"), MISSING_HEADINGS.items())
def test_known_missing_headings_have_names(heading, name):
    # in the table itself, not only named by the generic fallback
    assert SECTION_MARKERS[heading] == name
    sections = parse_system_sections(f"## Relevant Facts\n- one\n{heading}\nits text")
    assert sections == {"relevant_facts": "- one", name: "its text"}


def test_existing_section_names_are_unchanged():
    """Dashboards and history read these names: a fix that adds headings must not rename one."""
    assert {
        "## Current Date/Time": "datetime",
        "## Identity": "identity",
        "## User Profile": "user_profile",
        "## Context Safety": "context_safety",
        "## Active Censors": "censors",
        "## Current Frame": "frame",
        "## Working Memory": "working_memory",
        "## Related Decisions": "related_decisions",
        "## Relevant Facts": "relevant_facts",
        "## Session Profile": "session_profile",
        "## Known Procedures": "known_procedures",
        "## Procedure Catalog": "procedure_catalog",
        "## Recommended Procedures": "recommended_procedures",
        "## Past Episodes": "past_episodes",
        "## Recent Conversations": "recent_conversations",
        "## Tool Instructions": "frame_instructions",
        "[Execution Ledger]": "execution_ledger",
        "[Previous Turn Corrections]": "corrections",
        "## Output Formatting": "telegram_format",
    }.items() <= SECTION_MARKERS.items()
    # text before the first heading is still "other"
    assert parse_system_sections("You are Nous.\n## Identity\nme") == {"other": "You are Nous.", "identity": "me"}


# ---------------------------------------------------------------------------------------------------------------------
# The preamble and tool results
# ---------------------------------------------------------------------------------------------------------------------


def test_preamble_block_is_counted():
    system = "You are Nous.\n## Identity\nI am Nous."
    stats = analyze_context(system, [], None, 200_000, preamble=CLAUDE_CODE_PREAMBLE)
    without = analyze_context(system, [], None, 200_000)
    assert stats.section_tokens_est["preamble"] == len(CLAUDE_CODE_PREAMBLE) // 4
    assert stats.sections_present == ["preamble", "identity", "other"]  # "other" goes last, as it always has
    assert stats.total_tokens_est == sum(stats.section_tokens_est.values())
    assert stats.total_tokens_est == without.total_tokens_est + len(CLAUDE_CODE_PREAMBLE) // 4
    # the leading unheaded text keeps its own name: the preamble is a section of its own
    assert stats.section_tokens_est["other"] == without.section_tokens_est["other"]
    # the old ring path gives the same numbers, and keeps the preamble's text
    entry = ContextLogEntry.from_payload(
        session_id="s",
        turn_number=1,
        call_type="chat",
        model="m",
        system_prompt=system,
        messages=[],
        tools=None,
        frame_id="f",
        context_window=200_000,
        preamble=CLAUDE_CODE_PREAMBLE,
    )
    assert entry.token_breakdown == stats.section_tokens_est
    assert entry.total_tokens_est == stats.total_tokens_est
    assert entry.sections_text["preamble"] == CLAUDE_CODE_PREAMBLE


def test_tool_result_content_counts_in_message_estimate():
    def result(content):
        return [{"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": content}]}]

    assert _estimate_messages_tokens(result("")) == 0
    assert _estimate_messages_tokens(result("x" * 4000)) == 1000  # a string result
    blocks = [{"type": "text", "text": "x" * 3000}, {"type": "text", "text": "y" * 1000}]
    assert _estimate_messages_tokens(result(blocks)) == 1000  # a list of text blocks
    # an image block in a result has no text to count, and never breaks the estimate
    with_image = [*blocks, {"type": "image", "source": {"type": "base64", "data": "AAAA"}}]
    assert _estimate_messages_tokens(result(with_image)) == 1000
    stats = analyze_context("", result("x" * 4000), None, 200_000)
    assert stats.section_tokens_est["messages"] == 1000


# ---------------------------------------------------------------------------------------------------------------------
# Guard 1: real ContextEngine builds with every optional section on
# ---------------------------------------------------------------------------------------------------------------------

_SCANNED = ("cognitive/context.py", "cognitive/layer.py", "api/runner.py")
_PACKAGE = Path(nous.__file__).resolve().parent


class _Fact:
    def __init__(
        self,
        content: str,
        *,
        id: str,
        subject: str | None = None,
        score: float | None = 0.9,
        category: str | None = None,
    ) -> None:
        self.content = content
        self.subject = subject
        self.confidence = 0.9
        self.score = score
        self.id = id
        self.superseded_by = None
        self.category = category
        self.source = None
        self.recency_status = None
        self.recency_date = None


def _episode(summary: str, *, title: str | None = None, outcome: str = "success") -> SimpleNamespace:
    from datetime import UTC, datetime

    return SimpleNamespace(
        id=uuid4(),
        title=title,
        summary=summary,
        outcome=outcome,
        started_at=datetime.now(UTC),
        structured_summary=None,
        session_id="another-session",
        active=False,
        tags=["project"],
        score=0.9,
    )


def _procedure(name: str) -> ProcedureSummary:
    return ProcedureSummary(
        id=uuid4(),
        name=name,
        domain="ops",
        description=f"how to {name}",
        activation_count=3,
        effectiveness=0.5,
        score=0.9,
    )


# The identity prompt as identity/manager.py assembles it: headings of its own inside the Identity section.
_IDENTITY = "## Character\nCurious and careful.\n\n## Values\nHonesty over comfort."


def _engine(**flags) -> ContextEngine:
    brain = MagicMock()
    brain.embeddings = MagicMock()
    brain.query = AsyncMock(return_value=[])
    heart = MagicMock()
    for name in (
        "search_facts",
        "search_episodes",
        "list_facts_by_category",
        "list_censors",
        "list_episodes",
        "search_procedures",
    ):
        setattr(heart, name, AsyncMock(return_value=[]))
    heart.list_procedures = AsyncMock(return_value=([], 0))
    heart.get_working_memory = AsyncMock(return_value=None)
    heart.get_procedure_by_name = AsyncMock(return_value=None)
    heart.get_superseded_contents = AsyncMock(return_value={})
    settings = Settings(
        _env_file=None,
        relevance_floor_enabled=False,
        anti_hallucination_prompt=True,
        recall_before_clarify_prompt=True,
        epistemic_gate_enabled=True,
        temporal_context_enabled=True,
        proc_selection_graph_primary=False,
        **flags,
    )
    return ContextEngine(brain, heart, settings, identity_prompt=_IDENTITY)


def _frame() -> FrameSelection:
    return FrameSelection(
        frame_id="task", frame_name="Task", description="get a task done", confidence=0.9, match_method="pattern"
    )


async def _build_catalog_side():
    """Catalog on with the critic's pick (Recommended Procedures), no facts (Memory Retrieval Notice), a failing
    decision leg (Retrieval Failures), cache hints, profile facts, censors, working memory and recent conversations."""
    engine = _engine(proc_catalog_enabled=True, critic_skill_injection="enabled", recall_backstop_enabled=True)
    heart = engine._heart
    heart.list_procedures.return_value = ([_procedure("release")], 1)
    heart.get_procedure_by_name.return_value = _procedure("critic-pick")
    heart.list_facts_by_category.return_value = [_Fact("Prefers metric units", id="p1", category="preference")]
    heart.list_censors.return_value = [
        SimpleNamespace(
            id="c1", action="steer", trigger_pattern="rm -rf", reason="destructive", action_instruction="ask first"
        )
    ]
    heart.get_working_memory.return_value = SimpleNamespace(
        current_task="ship the work view", current_frame="task", open_threads=[], items=[]
    )
    heart.list_episodes.return_value = [_episode("talked about the dashboard", title="Dashboard chat")]
    engine._brain.query.side_effect = RuntimeError("connection reset")
    with patch("nous.api.tool_cache.get_cache_hints", AsyncMock(return_value=["- bash: build log (key abc)"])):
        return await engine.build(
            agent_id="a",
            session_id="s",
            input_text="what did we decide about the dashboard?",
            frame=_frame(),
            session=MagicMock(),
            critic_skills=["critic-pick"],
            epistemic_class="grounded",
        )


async def _build_memory_side():
    """Catalog off with the awareness cue and passive procedures (Known Procedures), facts (Relevant Facts and the
    Session Profile leg), decisions and past episodes."""
    engine = _engine(
        proc_catalog_enabled=False,
        proc_awareness_cue=True,
        proc_passive_injection_enabled=True,
        profile_intent_leg_enabled=True,
    )
    heart = engine._heart

    async def search_facts(*_args, **kwargs):
        if kwargs.get("include_categories"):  # the Session Profile leg
            return [
                _Fact(
                    "Works on the dashboard project on weekdays",
                    id="leg1",
                    subject="work",
                    score=0.9,
                    category="person",
                )
            ]
        return [_Fact("The dashboard reads the model call table", id="f1", subject="dashboard", score=0.9)]

    heart.search_facts.side_effect = search_facts
    heart.search_procedures.return_value = [_procedure("deploy")]
    heart.search_episodes.return_value = [_episode("reviewed the dashboard plan")]
    engine._brain.query.return_value = [
        SimpleNamespace(
            id=uuid4(),
            description="Read the table, not the ring",
            outcome="success",
            confidence=0.9,
            category="architecture",
            score=0.9,
            created_at=None,
        )
    ]
    return await engine.build(
        agent_id="a",
        session_id="s",
        input_text="what did we decide about the dashboard?",
        frame=_frame(),
    )


def _section_labels_in_source() -> set[str]:
    """Every ``ContextSection(label=...)`` in context.py: the headings its ``## {label}`` renderer can emit."""
    tree = ast.parse((_PACKAGE / "cognitive/context.py").read_text(encoding="utf-8"))
    labels: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)) != "ContextSection":
            continue
        (label,) = [k.value for k in node.keywords if k.arg == "label"]
        assert isinstance(label, ast.Constant) and isinstance(label.value, str), (
            f"context.py:{node.lineno}: a ContextSection label must be a string literal, so its heading can be checked"
        )
        labels.add(label.value)
    return labels


def _headings(prompt: str) -> list[str]:
    """The prompt's heading lines: ``## `` at the start of a line, and ``[Execution Ledger]``-style headers."""
    return [ln.rstrip() for ln in prompt.split("\n") if ln.startswith("## ") or re.fullmatch(r"\[[A-Z][^\]]*\]", ln)]


@pytest.mark.asyncio
async def test_context_builder_headings_are_all_known():
    """Two builds between them render every section ContextEngine has (some are alternatives: the catalog or the
    awareness cue, the Memory Retrieval Notice only with no facts, Retrieval Failures only with a failed leg). Every
    heading the parser sees is in the marker table; none is named by the fallback."""
    known = set(SECTION_MARKERS.values())
    rendered: set[str] = set()
    for result in (await _build_catalog_side(), await _build_memory_side()):
        rendered |= {s.label for s in result.sections}
        sections = parse_system_sections(result.system_prompt)
        assert set(sections) - known - {"other"} == set(), sections.keys()
        # each section the engine rendered is one the parser names from the table
        for s in result.sections:
            assert SECTION_MARKERS.get(f"## {s.label}") in sections, s.label
        # every top-level heading line starts a table section; the identity prompt's own headings stay inside it
        for heading in _headings(result.system_prompt):
            if heading in ("## Character", "## Values"):
                assert heading in sections["identity"]
                continue
            assert any(heading.startswith(marker) for marker in SECTION_MARKERS), heading
    # the fixture really turns every section on: a new section in context.py must be added to a build here
    assert rendered == _section_labels_in_source()


# ---------------------------------------------------------------------------------------------------------------------
# Guard 2: every heading literal in the source is in the table
# ---------------------------------------------------------------------------------------------------------------------


def _heading_literals(tree: ast.AST) -> tuple[list[tuple[int, str]], list[int]]:
    """The ``## `` heading of every string literal (and f-string whose literal part comes first) that begins with
    ``## ``, as (line, heading); and the lines of f-strings whose heading is all placeholder (``f"## {label}"``).
    Docstrings and bare string statements are left out, and so are the literal parts of an f-string on their own."""
    skip: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Expr) and isinstance(node.value, (ast.Constant, ast.JoinedStr)):
            skip.add(id(node.value))
        if isinstance(node, ast.JoinedStr):
            skip.update(id(v) for v in node.values)
    static: list[tuple[int, str]] = []
    dynamic: list[int] = []
    for node in ast.walk(tree):
        if id(node) in skip:
            continue
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            text = node.value
        elif isinstance(node, ast.JoinedStr) and node.values and isinstance(node.values[0], ast.Constant):
            text = str(node.values[0].value)
        else:
            continue
        if not text.startswith("## "):
            continue
        heading = text.split("\n", 1)[0].rstrip()
        if heading == "##":
            dynamic.append(node.lineno)
        else:
            static.append((node.lineno, heading))
    return static, dynamic


def test_context_builder_headings_are_all_known_in_source():
    """A heading written into the system prompt by the context builder, the cognitive layer or the runner must be in
    the marker table, so a heading added later fails here instead of being folded into its neighbour."""
    found: list[str] = []
    for rel in _SCANNED:
        static, dynamic = _heading_literals(ast.parse((_PACKAGE / rel).read_text(encoding="utf-8")))
        for line, heading in static:
            assert any(heading.startswith(marker) for marker in SECTION_MARKERS), f"nous/{rel}:{line}: {heading}"
            found.append(heading)
        # f"## {section.label}" is context.py's section renderer: its labels are checked below. Anywhere else a
        # placeholder heading cannot be checked.
        assert not dynamic or rel == "cognitive/context.py", f"nous/{rel}:{dynamic}: a heading that is all placeholder"
    for label in _section_labels_in_source():
        assert f"## {label}" in SECTION_MARKERS, label
    # the scan sees the headings it is meant to see
    assert {"## Active Guidance", "## Tool Instructions", "## Output Formatting (Telegram)"} <= set(found)
