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
from nous.observability import model_calls
from nous.observability.context_logger import ContextLogEntry
from nous.observability.model_calls import (
    FREE_TEXT_SECTIONS,
    LINE_MARKERS,
    SECTION_MARKERS,
    _estimate_messages_tokens,
    analyze_context,
    parse_system_sections,
    payload_preamble,
)

# ---------------------------------------------------------------------------------------------------------------------
# The parser
# ---------------------------------------------------------------------------------------------------------------------


def test_unknown_heading_is_reported_as_itself():
    """An unknown heading between builder sections (none of them free text) is a section of its own."""
    prompt = (
        "## Current Date/Time\nnow\n"
        "## Foo\nbar baz\n"
        "## Brand New Thing (v2)\nsomething else\n"
        "## Context Safety\nbe careful"
    )
    sections = parse_system_sections(prompt)
    assert sections == {
        "datetime": "now",
        "foo": "bar baz",
        "brand_new_thing_v2": "something else",
        "context_safety": "be careful",
    }
    # a deeper heading is content, not a section
    assert parse_system_sections("## Tools Outrank Memory\nfollow the tool\n### detail\nmore") == {
        "tools_outrank_memory": "follow the tool\n### detail\nmore"
    }


# Each section whose body renders stored or authored text, and where that text comes from.
EXPECTED_FREE_TEXT = {
    "identity",  # identity_override: the identity sections in the DB (identity/manager.py assemble_prompt)
    "user_profile",  # profile facts or the profile card's lines (heart facts)
    "censors",  # censor trigger patterns, reasons and instructions (heart censors)
    "frame",  # the frame row's description and questions (cognitive/frames.py _frame_to_selection)
    "working_memory",  # current task (the user's input), open threads, items (heart working memory)
    "related_decisions",  # decision descriptions (brain)
    "relevant_facts",  # fact contents, verbatim up to the format cap (heart facts)
    "session_profile",  # fact contents (the profile intent leg)
    "known_procedures",  # procedure names and descriptions (heart procedures)
    "procedure_catalog",  # procedure names and descriptions (heart procedures)
    "recommended_procedures",  # skill bodies, newlines kept (_format_procedure_bodies)
    "past_episodes",  # episode summaries (heart episodes)
    "recent_conversations",  # episode titles, summaries and open threads (heart episodes)
    "cached_results",  # cached tool inputs (tool_cache.get_cache_hints)
    "execution_ledger",  # tool key arguments and error result summaries (execution_ledger.py _build_section)
    "corrections",  # quoted claims from the agent's own reply (claim_verifier.py _build_correction)
    "active_guidance",  # censor guidance text (layer.py, censor_injected)
    "subtask_results",  # subtask tasks, results, summaries, findings (layer.py _format_subtask_results)
    "background_results",  # finished subtask and DAG titles and bodies (result_inbox.py format_inbox_messages)
    "hub_shift",  # graph entity labels (brain/hub_snapshots.py format_hub_shift_block)
}


def test_free_text_sections_are_the_audited_set():
    assert FREE_TEXT_SECTIONS == EXPECTED_FREE_TEXT
    assert FREE_TEXT_SECTIONS <= set(SECTION_MARKERS.values())


_MARKER_FOR = {name: marker for marker, name in SECTION_MARKERS.items()}


@pytest.mark.parametrize("name", sorted(EXPECTED_FREE_TEXT))
def test_an_unknown_heading_inside_free_text_stays_in_its_section(name):
    """Stored text can carry its own ``## `` lines (an identity's ``## Character``, a skill's ``## Steps``, a fact or a
    cached markdown file): such a line stays part of the section, so its number does not collapse. A heading in the
    table still starts its section."""
    marker = _MARKER_FOR[name]
    head = f"{marker}\n" if marker.startswith(LINE_MARKERS) else ""  # a line marker's line is part of its section
    prompt = f"{marker}\nfirst line\n## Some Heading\nmore text\n## Context Safety\nbe careful"
    assert parse_system_sections(prompt) == {
        name: f"{head}first line\n## Some Heading\nmore text",
        "context_safety": "be careful",
    }


def _section_start_by_full_scan(line: str, current_name: str) -> str | None:
    """``_section_start`` as written before its first-character pre-test: every marker tried on every line."""
    for marker in sorted(SECTION_MARKERS, key=len, reverse=True):
        if line.strip().startswith(marker):
            return SECTION_MARKERS[marker]
    if line.startswith("## ") and current_name and current_name not in FREE_TEXT_SECTIONS:
        return re.sub(r"[^a-z0-9]+", "_", line[3:].lower()).strip("_") or None
    return None


def test_the_first_character_pre_test_never_changes_a_section_start():
    """The pre-test skips the marker scan for a line no marker or ``## `` heading can start; on every kind of line,
    each marker and heading with and without leading whitespace or trailing text, it answers as the full scan does."""
    lines = ["", "   ", "\t", "- a bullet", "plain text", "#", "##", "## ", "#tag", "### deeper", "## Some Heading"]
    lines += ["  ## Indented Heading", "=", "== not a block", "[", "[note] text", "[graph]", "  - [a] fact", "## ="]
    for marker in SECTION_MARKERS:
        lines += [marker, f"  {marker}", f"\t{marker} tail", f"{marker}  ", marker[:-1], f"x{marker}"]
    for current in ("", "other", "datetime", "context_safety", *sorted(FREE_TEXT_SECTIONS)):
        for line in lines:
            expected = _section_start_by_full_scan(line, current)
            assert model_calls._section_start(line, current) == expected, (line, current)


def test_the_identity_prompts_own_headings_are_one_identity_section():
    prompt = "## Identity\n\n## Character\nCurious.\n\n## Values\nHonest.\n\n## Current Date/Time\nnow"
    assert parse_system_sections(prompt) == {
        "identity": "## Character\nCurious.\n\n## Values\nHonest.",
        "datetime": "now",
    }


def test_text_before_any_heading_is_free_text():
    """layer.py's degraded path (the context build failed) sends the identity prompt alone, with no ``## Identity``:
    its own ``## Character`` lines stay in the leading text instead of splitting it. A table heading still starts its
    section."""
    identity = "## Character\nCurious.\n\n## Values\nHonest."
    assert parse_system_sections(identity) == {"other": identity}
    assert parse_system_sections("lead\n## Foo\nbar\n## Current Date/Time\nnow") == {
        "other": "lead\n## Foo\nbar",
        "datetime": "now",
    }


def _subtask(status: str, **fields) -> SimpleNamespace:
    base = {"id": uuid4(), "task": "check the deploy", "result": None, "error": None, "final_outcome": None}
    return SimpleNamespace(status=status, **{**base, "report_jsonb": None, **fields})


def test_a_subtask_result_with_a_heading_stays_in_subtask_results():
    """The default path (``result_inbox_enabled`` off): layer.py appends ``=== Completed/Blocked/Failed Subtask ===``
    blocks after the context sections. Each block is ``subtask_results``, the repeated blocks add up, and a ``## ``
    line in a result stays inside."""
    from nous.cognitive.layer import _format_subtask_results

    block = _format_subtask_results(
        [
            _subtask("completed", result="Deployed.\n## Heading\nall green"),
            _subtask("failed", error="timeout"),
            _subtask("completed", final_outcome="incomplete_blocked", report_jsonb={"blocked_reason": "no access"}),
        ]
    )
    sections = parse_system_sections(f"## Retrieval Failures\nNote: decision recall failed.\n\n{block}")
    assert set(sections) == {"retrieval_failures", "subtask_results"}
    assert sections["retrieval_failures"] == "Note: decision recall failed."
    body = sections["subtask_results"]
    assert "Result: Deployed.\n## Heading\nall green" in body
    assert "Error: timeout" in body and "Blocked: no access" in body
    stats = analyze_context(f"## Retrieval Failures\nNote.\n\n{block}", [], None, 200_000)
    assert stats.section_tokens_est["subtask_results"] == len(body) // 4


def test_a_background_result_with_a_heading_stays_in_background_results():
    """The inbox path (``result_inbox_enabled`` on): the ``=== Background Results ===`` block is
    ``background_results``, and a ``## `` line in a result body stays inside."""
    from datetime import UTC, datetime

    from nous.heart.result_inbox import format_inbox_messages

    row = SimpleNamespace(
        created_at=datetime.now(UTC),
        msg_type="RESULT",
        source_kind="subtask",
        source_id=uuid4(),
        title="Deploy check",
        body="Deployed.\n## Heading\nall green",
    )
    block = format_inbox_messages([row], 5)
    sections = parse_system_sections(f"## Current Date/Time\nnow\n\n{block}")
    assert set(sections) == {"datetime", "background_results"}
    assert "Title: Deploy check\nDeployed.\n## Heading\nall green\n</result_message>" in sections["background_results"]


def test_a_hub_shift_notice_is_its_own_section():
    """Each ``[graph] Hub shift:`` line is content (the marker is a line prefix, not a heading): the lines are the
    section ``hub_shift``, counted, not folded into the section before them."""
    from nous.brain.hub_snapshots import format_hub_shift_block

    block = format_hub_shift_block(
        [
            {"kind": "entered", "label": "[entity] Toronto", "rank": 3, "degree": 12},
            {"kind": "left", "label": "[entity] Ottawa", "degree": 4},
        ]
    )
    sections = parse_system_sections(f"## Current Date/Time\nnow\n\n{block}")
    assert sections == {"datetime": "now", "hub_shift": block}
    # a line that merely mentions the marker later in it is not one
    assert parse_system_sections("## Current Date/Time\nsee [graph] Hub shift: x") == {
        "datetime": "see [graph] Hub shift: x"
    }


def test_a_repeated_section_name_adds_its_text_never_replaces_it():
    """A name met twice, from a repeated marker or a repeated fallback heading, holds both texts, so the estimate
    counts both. The leading text ("other") and the preamble merge the same way with a heading of their name."""
    prompt = (
        "## Relevant Facts\n- [a] one\n"
        "## Context Safety\nbe careful\n"
        "## Foo\nx\n"
        "## Relevant Facts\n- [b] two\n"
        "## Current Date/Time\nnow\n"
        "## Foo\ny"
    )
    sections = parse_system_sections(prompt)
    assert sections == {
        "relevant_facts": "- [a] one\n\n- [b] two",
        "context_safety": "be careful",
        "foo": "x\n\ny",
        "datetime": "now",
    }
    stats = analyze_context(prompt, [], None, 200_000)
    assert stats.loaded_facts == 2
    assert stats.section_tokens_est["relevant_facts"] == len("- [a] one\n\n- [b] two") // 4
    # the leading text and a "## Other" heading are one section; so are block 0 and a "## Preamble" heading
    assert parse_system_sections("lead\n## Current Date/Time\nnow\n## Other\nz") == {
        "datetime": "now",
        "other": "z\n\nlead",
    }
    stats = analyze_context("## Current Date/Time\nnow\n## Preamble\nq", [], None, 200_000, preamble="P")
    assert stats.sections_present == ["preamble", "datetime"]
    assert stats.section_tokens_est["preamble"] == len("P\n\nq") // 4


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
    # layer.py's unheaded injections
    "=== Completed Subtask ===": "subtask_results",
    "=== Blocked Subtask ===": "subtask_results",
    "=== Failed Subtask ===": "subtask_results",
    "=== Background Results ===": "background_results",
    "[graph] Hub shift:": "hub_shift",  # a line prefix: the line is part of the section
}


@pytest.mark.parametrize(("heading", "name"), MISSING_HEADINGS.items())
def test_known_missing_headings_have_names(heading, name):
    # in the table itself, not only named by the generic fallback
    assert SECTION_MARKERS[heading] == name
    sections = parse_system_sections(f"## Relevant Facts\n- one\n{heading}\nits text")
    head = f"{heading}\n" if heading.startswith(LINE_MARKERS) else ""
    assert sections == {"relevant_facts": "- one", name: f"{head}its text"}


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


def test_block_0_is_the_preamble_only_when_it_is_the_claude_code_preamble():
    """The runner's flat prompt holds every system block but the Claude Code preamble. A block 0 that is anything else
    is already in the flat prompt, so it is not the section ``preamble`` and is counted there, never twice."""
    flat = "## Identity\nI am Nous."
    with_preamble = [{"type": "text", "text": CLAUDE_CODE_PREAMBLE}, {"type": "text", "text": flat}]
    assert payload_preamble(with_preamble) == CLAUDE_CODE_PREAMBLE
    without = [{"type": "text", "text": flat}]  # block 0 is the flat prompt itself
    assert payload_preamble(without) == ""
    assert payload_preamble([]) == ""
    once = analyze_context(flat, [], None, 200_000, preamble=payload_preamble(without))
    assert "preamble" not in once.section_tokens_est
    assert once.total_tokens_est == analyze_context(flat, [], None, 200_000).total_tokens_est


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
        # a stored fact whose text has a heading line of its own
        return [_Fact("The dashboard reads the table\n## Some Heading\nnot a section", id="f1", subject="dashboard")]

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
        labels_given = [k.value for k in node.keywords if k.arg == "label"]
        assert len(labels_given) == 1, (
            f"context.py:{node.lineno}: a ContextSection takes its label as a keyword, so its heading can be checked"
        )
        label = labels_given[0]
        assert isinstance(label, ast.Constant) and isinstance(label.value, str), (
            f"context.py:{node.lineno}: a ContextSection label must be a string literal, so its heading can be checked"
        )
        labels.add(label.value)
    return labels


def _headings(prompt: str) -> list[str]:
    """The prompt's heading lines: ``## `` at the start of a line, ``[Execution Ledger]``-style headers and
    ``=== Completed Subtask ===``-style headers."""
    return [
        ln.rstrip()
        for ln in prompt.split("\n")
        if ln.startswith("## ") or re.fullmatch(r"\[[A-Z][^\]]*\]", ln) or re.fullmatch(r"=== .* ===", ln)
    ]


@pytest.mark.asyncio
async def test_context_builder_headings_are_all_known():
    """Two builds between them render every section ContextEngine has (some are alternatives: the catalog or the
    awareness cue, the Memory Retrieval Notice only with no facts, Retrieval Failures only with a failed leg). Every
    heading the parser sees is in the marker table; none is named by the fallback. The headings inside stored text (the
    identity prompt's own, and one in a fact) stay inside their section: the identity is one ``identity`` section."""
    known = set(SECTION_MARKERS.values())
    rendered: set[str] = set()
    inside = {"## Character": "identity", "## Values": "identity", "## Some Heading": "relevant_facts"}
    for result in (await _build_catalog_side(), await _build_memory_side()):
        rendered |= {s.label for s in result.sections}
        sections = parse_system_sections(result.system_prompt)
        assert set(sections) - known - {"other"} == set(), sections.keys()
        # each section the engine rendered is one the parser names from the table
        for s in result.sections:
            assert SECTION_MARKERS.get(f"## {s.label}") in sections, s.label
        # every top-level heading line starts a table section; a heading in stored text stays inside its section
        for heading in _headings(result.system_prompt):
            if heading in inside:
                assert heading in sections[inside[heading]], heading
                continue
            assert any(heading.startswith(marker) for marker in SECTION_MARKERS), heading
        assert "## Character\nCurious and careful.\n\n## Values\nHonesty over comfort." in sections["identity"]
    assert "## Some Heading\nnot a section" in sections["relevant_facts"]  # the second build has the fact
    # the fixture really turns every section on: a new section in context.py must be added to a build here
    assert rendered == _section_labels_in_source()


# ---------------------------------------------------------------------------------------------------------------------
# Guard 2: every heading literal in the source is in the table
# ---------------------------------------------------------------------------------------------------------------------


def _heading_literals(tree: ast.AST) -> tuple[list[tuple[int, str]], list[int]]:
    """Every heading line in the string literals and f-strings: each line, of every literal part, that starts with
    ``## `` or is a ``=== X ===`` header, as (line, heading); and the lines of f-strings whose ``## `` heading is all
    placeholder (``f"## {label}"``). A line that goes on from a placeholder is not a line start. Docstrings and bare
    string statements are left out, and so are the literal parts of an f-string on their own."""
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
            parts: list[ast.expr] = [node]
        elif isinstance(node, ast.JoinedStr):
            parts = list(node.values)
        else:
            continue
        for i, part in enumerate(parts):
            if not (isinstance(part, ast.Constant) and isinstance(part.value, str)):
                continue
            lines = part.value.split("\n")
            for j, line in enumerate(lines):
                if j == 0 and i > 0:
                    continue  # the rest of a line a placeholder began
                heading = line.rstrip()
                if line.startswith("## ") and heading == "##" and j == len(lines) - 1 and i + 1 < len(parts):
                    dynamic.append(node.lineno)
                elif line.startswith("## ") or re.fullmatch(r"=== .* ===", heading):
                    static.append((node.lineno, heading))
    return static, dynamic


def test_the_source_scan_sees_a_heading_on_any_line():
    """The guard's own check: a heading after a newline, in an implicit concatenation, in an f-string or in a
    triple-quoted string is caught; a docstring is not; an all-placeholder heading is dynamic."""
    caught = [
        'x = "## Foo\\n"',
        'x = "\\n\\n" + "## Foo\\n"',
        'x = f"## Foo {y}"',
        'x = "\\n\\n## Foo\\n"',
        'x = ("intro\\n\\n" "## Foo\\n")',
        'x = f"\\n## Foo {y}"',
        'x = """\n## Foo\nbody"""',
        'x = f"{y}\\n## Foo\\n"',
        'x = "\\n=== Foo ===\\n"',
    ]
    for source in caught:
        static, dynamic = _heading_literals(ast.parse(source))
        assert [h for _, h in static] == ["=== Foo ===" if "===" in source else "## Foo"], source
        assert dynamic == [], source
    assert _heading_literals(ast.parse('def f():\n    """## Foo\n\n## Bar"""\n')) == ([], [])
    assert _heading_literals(ast.parse('x = f"## {label}\\n\\n{body}"')) == ([], [1])
    assert _heading_literals(ast.parse('x = "see ## Foo inline"')) == ([], [])


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
    assert {
        "## Active Guidance",
        "## Tool Instructions",
        "## Output Formatting (Telegram)",
        "=== Completed Subtask ===",
        "=== Blocked Subtask ===",
        "=== Failed Subtask ===",
    } <= set(found)
