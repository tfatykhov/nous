"""F079 P1 — pull-path delivery: richer recall_deep procedure bodies + awareness cue.

The body-formatting logic is a pure helper (no DB), unit-tested here. The flag-gated
awareness section + live recall_deep behavior are validated on the eval instance.
"""

import pytest

from nous.cognitive.context import SECTION_TIERS
from nous.heart.heart import _format_procedure_recall_summary
from nous.heart.schemas import ProcedureSummary


def _proc(**kw) -> ProcedureSummary:
    base = dict(
        id="00000000-0000-0000-0000-000000000001",
        name="send-email",
        domain="comms",
        description="Send email via the guarded tool",
        activation_count=5,
        effectiveness=0.9,
        score=0.7,
    )
    base.update(kw)
    return ProcedureSummary(**base)


class TestRecallSummary:
    def test_off_is_byte_identical_legacy(self):
        p = _proc(implementation_notes=["step one", "step two"])
        assert _format_procedure_recall_summary(p, False, 240) == "send-email: Send email via the guarded tool"

    def test_off_no_description(self):
        p = _proc(description=None)
        assert _format_procedure_recall_summary(p, False, 240) == "send-email"

    def test_on_appends_body(self):
        p = _proc(implementation_notes=["use the send_email tool", "verify recipient"])
        out = _format_procedure_recall_summary(p, True, 240)
        assert out.startswith("send-email: Send email via the guarded tool | ")
        assert "use the send_email tool" in out and "verify recipient" in out

    def test_on_collapses_newlines_to_one_line(self):
        # codex-relevant: verbatim multiline fields must NOT produce multiple lines
        # (would be shredded by SmartCompress).
        p = _proc(implementation_notes=["line a\n- embedded bullet\nline b"])
        out = _format_procedure_recall_summary(p, True, 240)
        assert "\n" not in out
        assert "line a - embedded bullet line b" in out

    def test_on_caps_length_with_ellipsis(self):
        p = _proc(implementation_notes=["x" * 1000])
        out = _format_procedure_recall_summary(p, True, 50)
        body = out.split(" | ", 1)[1]
        assert len(body) <= 51  # 50 + the ellipsis char
        assert body.endswith("…")

    def test_on_empty_body_no_separator(self):
        p = _proc(implementation_notes=[], core_patterns=[])
        assert _format_procedure_recall_summary(p, True, 240) == "send-email: Send email via the guarded tool"

    def test_on_core_patterns_lead(self):
        # core_patterns (concise "what to do") lead the body, then implementation_notes.
        p = _proc(implementation_notes=["note1"], core_patterns=["pattern1"])
        out = _format_procedure_recall_summary(p, True, 240)
        assert out.index("pattern1") < out.index("note1")

    def test_on_truncates_on_word_boundary(self):
        p = _proc(core_patterns=["alpha beta gamma delta epsilon zeta eta theta iota"])
        out = _format_procedure_recall_summary(p, True, 30)
        body = out.split(" | ", 1)[1]
        assert body.endswith("…")
        # No mid-word cut: the char before the ellipsis is part of a whole word.
        assert "  " not in body


class TestAwarenessTier:
    def test_awareness_section_is_static_tier(self):
        # Static => cached, never busts (the caching invariant of the design).
        assert SECTION_TIERS.get("Procedure Awareness") == "static"


class TestAwarenessCueBuild:
    """Behavioral: the cue section appears in ContextEngine.build output (flag on),
    lands in the static tier, and is byte-stable across turns (caching invariant)."""

    def _engine(self, cue: bool):
        from unittest.mock import AsyncMock, MagicMock
        from nous.cognitive.context import ContextEngine
        from nous.config import Settings
        heart = MagicMock()
        for m in ("search_procedures", "search_facts", "search_episodes",
                  "list_facts_by_category", "list_censors", "list_episodes"):
            setattr(heart, m, AsyncMock(return_value=[]))
        heart.get_procedure_by_name = AsyncMock(return_value=None)
        settings = Settings(_env_file=None, proc_awareness_cue=cue, relevance_floor_enabled=False)
        brain = MagicMock(); brain.embeddings = None; brain.query = AsyncMock(return_value=[])
        return ContextEngine(brain, heart, settings, identity_prompt="Test")

    @staticmethod
    def _frame():
        from nous.cognitive.schemas import FrameSelection
        return FrameSelection(frame_id="task", frame_name="Task", confidence=0.9, match_method="test")

    @pytest.mark.asyncio
    async def test_cue_present_static_and_byte_stable(self):
        engine = self._engine(cue=True)
        r1 = await engine.build(agent_id="t", session_id="s1", input_text="do a task", frame=self._frame())
        aware = [s for s in r1.sections if s.label == "Procedure Awareness"]
        assert len(aware) == 1
        assert aware[0].tier == "static"
        r2 = await engine.build(agent_id="t", session_id="s1", input_text="another task", frame=self._frame())
        aware2 = [s for s in r2.sections if s.label == "Procedure Awareness"]
        assert aware2[0].content == aware[0].content  # byte-stable -> cache-safe

    @pytest.mark.asyncio
    async def test_cue_absent_when_flag_off(self):
        engine = self._engine(cue=False)
        r = await engine.build(agent_id="t", session_id="s1", input_text="do a task", frame=self._frame())
        assert not any(s.label == "Procedure Awareness" for s in r.sections)


def test_decision_pipeline_carries_pattern():
    """F079 P1: recall_deep delivers the decision abstract pattern (was dropped)."""
    from datetime import datetime, timezone
    from uuid import uuid4
    from nous.api.retrieval_pipeline import _decisions_to_pipeline
    from nous.brain.schemas import DecisionSummary
    d = DecisionSummary(
        id=uuid4(), description="chose cursor pagination", confidence=0.8,
        category="tooling", stakes="medium", outcome="success",
        pattern="prefer-cursor-pagination", created_at=datetime.now(timezone.utc),
    )
    out = _decisions_to_pipeline([d])
    assert out[0].metadata["pattern"] == "prefer-cursor-pagination"


@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_recall_search_populates_body_from_orm(heart, session):
    """PG-required: a stored procedure's body fields flow from the ORM into the
    ProcedureSummary on the search path (data source NOT mocked)."""
    from nous.heart.schemas import ProcedureInput
    # Unique nonce so the search retrieves THIS procedure regardless of other rows
    # in the DB (robust to a populated/shared test DB).
    nonce = "zqxnonce42marker"
    await heart.store_procedure(
        ProcedureInput(
            name="proc-" + nonce, domain="reporting",
            description=f"{nonce} produce the quarterly report",
            core_patterns=[f"{nonce} start with an executive summary"],
            implementation_notes=[f"{nonce} gather metrics", "write three sections"],
        ),
        session=session,
    )
    results = await heart.search_procedures(nonce, session=session)
    mine = [r for r in results if nonce in r.name]
    assert mine, "expected the seeded procedure to be retrieved"
    top = mine[0]
    # Body fields populated from the ORM (the P1 enrichment source).
    assert top.implementation_notes or top.core_patterns
    summary = _format_procedure_recall_summary(top, True, 240)
    assert "executive summary" in summary or "gather metrics" in summary
