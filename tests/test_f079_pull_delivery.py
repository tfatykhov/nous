"""F079 — unified procedure pull: single surface (recall_deep), no duplication, no bloat.

- procedures enter context ONLY via the pull path when proc_passive_injection_enabled
  is False (the passive `## Known Procedures` section is skipped);
- recall_deep gives the FULL one-line body to the TOP-ranked procedure only (others
  keep name+desc) — one body (no bloat), richer than name+desc (no get_procedure
  round-trip = no duplicate copy).

Body-line logic is a pure helper (DB-free). Top-1 expansion + passive toggle are
exercised via build() (mocked) and a PG-required recall test.
"""

from datetime import datetime, timezone
from uuid import uuid4

import pytest

from nous.cognitive.context import SECTION_TIERS
from nous.heart.heart import _procedure_body_line
from nous.heart.schemas import ProcedureSummary


class TestProcedureBodyLine:
    def test_empty_returns_blank(self):
        assert _procedure_body_line([], [], 800) == ""

    def test_collapses_newlines_to_one_line(self):
        out = _procedure_body_line(["result:\n- detail a\n- detail b"], [], 800)
        assert "\n" not in out
        assert "result: - detail a - detail b" in out

    def test_core_patterns_lead_then_notes(self):
        out = _procedure_body_line(["pattern1"], ["note1"], 800)
        assert out.index("pattern1") < out.index("note1")

    def test_word_boundary_cap_with_ellipsis(self):
        out = _procedure_body_line(["alpha beta gamma delta epsilon zeta eta"], [], 20)
        assert out.endswith("…")
        assert "  " not in out  # no mid-word/double-space artifact

    def test_cap_zero_returns_blank(self):
        assert _procedure_body_line(["x"], [], 0) == ""


class TestAwarenessTier:
    def test_awareness_section_is_static_tier(self):
        assert SECTION_TIERS.get("Procedure Awareness") == "static"


def _proc_summary(name: str, score: float = 0.9) -> ProcedureSummary:
    return ProcedureSummary(
        id=uuid4(), name=name, domain="reporting", description=f"desc {name}",
        activation_count=1, effectiveness=None, score=score,
        core_patterns=[f"{name} pattern"], implementation_notes=[f"{name} note"],
    )


class TestBuildSurfaces:
    """Passive `## Known Procedures` section appears iff proc_passive_injection_enabled."""

    def _engine(self, **flags):
        from unittest.mock import AsyncMock, MagicMock
        from nous.cognitive.context import ContextEngine
        from nous.config import Settings
        heart = MagicMock()
        heart.search_procedures = AsyncMock(return_value=[_proc_summary("p1")])
        for m in ("search_facts", "search_episodes", "list_facts_by_category",
                  "list_censors", "list_episodes"):
            setattr(heart, m, AsyncMock(return_value=[]))
        heart.get_procedure_by_name = AsyncMock(return_value=None)
        settings = Settings(_env_file=None, relevance_floor_enabled=False, **flags)
        brain = MagicMock(); brain.embeddings = None; brain.query = AsyncMock(return_value=[])
        return ContextEngine(brain, heart, settings, identity_prompt="Test")

    @staticmethod
    def _frame():
        from nous.cognitive.schemas import FrameSelection
        return FrameSelection(frame_id="task", frame_name="Task", confidence=0.9, match_method="test")

    @pytest.mark.asyncio
    async def test_passive_section_present_by_default(self):
        engine = self._engine(proc_passive_injection_enabled=True)
        r = await engine.build(agent_id="t", session_id="s1", input_text="do a task", frame=self._frame())
        assert any(s.label == "Known Procedures" for s in r.sections)

    @pytest.mark.asyncio
    async def test_passive_section_absent_when_disabled(self):
        engine = self._engine(proc_passive_injection_enabled=False)
        r = await engine.build(agent_id="t", session_id="s1", input_text="do a task", frame=self._frame())
        assert not any(s.label == "Known Procedures" for s in r.sections)

    @pytest.mark.asyncio
    async def test_awareness_cue_present_static_byte_stable(self):
        engine = self._engine(proc_awareness_cue=True)
        r1 = await engine.build(agent_id="t", session_id="s1", input_text="do a task", frame=self._frame())
        aware = [s for s in r1.sections if s.label == "Procedure Awareness"]
        assert len(aware) == 1 and aware[0].tier == "static"
        r2 = await engine.build(agent_id="t", session_id="s1", input_text="other task", frame=self._frame())
        assert [s for s in r2.sections if s.label == "Procedure Awareness"][0].content == aware[0].content

    @pytest.mark.asyncio
    async def test_awareness_cue_absent_by_default(self):
        engine = self._engine(proc_awareness_cue=False)
        r = await engine.build(agent_id="t", session_id="s1", input_text="do a task", frame=self._frame())
        assert not any(s.label == "Procedure Awareness" for s in r.sections)

    @pytest.mark.asyncio
    async def test_passive_off_skips_embedding_search(self):
        """Track B (embedding similarity) is what passive-off drops — the search must
        not even run, since recall_deep is the single surface for cosine-matched procs."""
        engine = self._engine(proc_passive_injection_enabled=False)
        await engine.build(agent_id="t", session_id="s1", input_text="do a task", frame=self._frame())
        engine._heart.search_procedures.assert_not_called()

    @pytest.mark.asyncio
    async def test_critic_track_survives_passive_off(self):
        """M1: passive-off gates ONLY Track B (embedding). Critic-recommended skills
        (Track A) have no recall_deep equivalent, so they must still reach context."""
        from unittest.mock import AsyncMock
        engine = self._engine(
            proc_passive_injection_enabled=False, critic_skill_injection="enabled",
        )
        engine._heart.get_procedure_by_name = AsyncMock(return_value=_proc_summary("critic-pick"))
        r = await engine.build(
            agent_id="t", session_id="s1", input_text="do a task",
            frame=self._frame(), critic_skills=["critic-pick"],
        )
        sec = [s for s in r.sections if s.label == "Known Procedures"]
        assert sec, "Critic track must still inject when passive embeddings are off"
        assert "critic-pick" in sec[0].content
        engine._heart.search_procedures.assert_not_called()  # Track B still gated


def test_decision_pipeline_carries_pattern():
    from nous.api.retrieval_pipeline import _decisions_to_pipeline
    from nous.brain.schemas import DecisionSummary
    d = DecisionSummary(
        id=uuid4(), description="chose cursor pagination", confidence=0.8,
        category="tooling", stakes="medium", outcome="success",
        pattern="prefer-cursor-pagination", created_at=datetime.now(timezone.utc),
    )
    assert _decisions_to_pipeline([d])[0].metadata["pattern"] == "prefer-cursor-pagination"


@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_recall_gives_body_to_top_procedure_only(heart, session):
    """PG-required: recall surfaces a body for exactly the TOP procedure (no bloat),
    and that body is richer than name+desc (no get_procedure round-trip needed)."""
    from nous.heart.schemas import ProcedureInput
    nonce = "zqxnonce77uniq"
    for n in (1, 2):
        await heart.store_procedure(
            ProcedureInput(
                name=f"proc-{nonce}-{n}", domain="reporting",
                description=f"{nonce} produce report variant {n}",
                core_patterns=[f"{nonce} executive summary step {n}"],
                implementation_notes=[f"{nonce} gather metrics {n}"],
            ),
            session=session,
        )
    heart.settings.recall_full_bodies = True
    results = await heart.recall(nonce, types=["procedure"], session=session)
    mine = [r for r in results if nonce in r.summary]
    assert len(mine) >= 2, "expected both seeded procedures retrieved"
    with_body = [r for r in mine if " | " in r.summary]
    # Exactly one procedure carries the full body (top-1) — bounded, no bloat.
    assert len(with_body) == 1
    assert "executive summary" in with_body[0].summary or "gather metrics" in with_body[0].summary


@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_recall_full_bodies_off_is_byte_identical(heart, session):
    """OFF path (recall_full_bodies=False): NO procedure summary carries a body and no
    body lists are carried in metadata — byte-identical legacy name+desc (F079 §8)."""
    from nous.heart.schemas import ProcedureInput
    nonce = "offpathnonce42uniq"
    for n in (1, 2):
        await heart.store_procedure(
            ProcedureInput(
                name=f"proc-{nonce}-{n}", domain="reporting",
                description=f"{nonce} produce report variant {n}",
                core_patterns=[f"{nonce} executive summary step {n}"],
                implementation_notes=[f"{nonce} gather metrics {n}"],
            ),
            session=session,
        )
    heart.settings.recall_full_bodies = False
    results = await heart.recall(nonce, types=["procedure"], session=session)
    mine = [r for r in results if nonce in r.summary]
    assert len(mine) >= 2, "expected both seeded procedures retrieved"
    assert all(" | " not in r.summary for r in mine), "OFF path must not add bodies"
    assert all("core_patterns" not in (r.metadata or {}) for r in mine), \
        "OFF path must not carry body lists in metadata"
