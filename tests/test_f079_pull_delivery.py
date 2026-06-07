"""F079 — catalog-first procedure delivery (progressive disclosure).

BREADTH: a static, cacheable `## Procedure Catalog` lists every active procedure by
  name+domain+desc (stable fields only -> byte-identical across turns -> caches).
DEPTH:   get_procedure(<name>) loads the full untruncated body on selection.

Passive embedding slots (Track B) stay gated off behind proc_passive_injection_enabled;
Critic-recommended skills (Track A) are NOT gated (no pull equivalent).
"""

from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from nous.cognitive.context import SECTION_TIERS
from nous.heart.schemas import ProcedureSummary


class TestSectionTiers:
    def test_catalog_and_awareness_are_static(self):
        # Both procedure breadth surfaces must ride the static cache tier.
        assert SECTION_TIERS.get("Procedure Catalog") == "static"
        assert SECTION_TIERS.get("Procedure Awareness") == "static"


def _proc_summary(name: str, score: float = 0.9) -> ProcedureSummary:
    return ProcedureSummary(
        id=uuid4(), name=name, domain="reporting", description=f"desc {name}",
        activation_count=7, effectiveness=0.5, score=score,
    )


class TestBuildSurfaces:
    def _engine(self, catalog_procs=None, **flags):
        from unittest.mock import AsyncMock, MagicMock
        from nous.cognitive.context import ContextEngine
        from nous.config import Settings
        heart = MagicMock()
        heart.search_procedures = AsyncMock(return_value=[_proc_summary("p1")])
        procs = catalog_procs if catalog_procs is not None else [_proc_summary("p1"), _proc_summary("p2")]
        heart.list_procedures = AsyncMock(return_value=(procs, len(procs)))
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

    async def _build(self, engine, text="do a task", **kw):
        return await engine.build(agent_id="t", session_id="s1", input_text=text, frame=self._frame(), **kw)

    # --- Passive embedding (Track B) gate -------------------------------------

    @pytest.mark.asyncio
    async def test_passive_section_present_by_default(self):
        engine = self._engine(proc_passive_injection_enabled=True)
        r = await self._build(engine)
        assert any(s.label == "Known Procedures" for s in r.sections)

    @pytest.mark.asyncio
    async def test_passive_section_absent_when_disabled(self):
        engine = self._engine(proc_passive_injection_enabled=False)
        r = await self._build(engine)
        assert not any(s.label == "Known Procedures" for s in r.sections)

    @pytest.mark.asyncio
    async def test_passive_off_skips_embedding_search(self):
        """passive-off drops only Track B (embedding) — the search must not even run."""
        engine = self._engine(proc_passive_injection_enabled=False)
        await self._build(engine)
        engine._heart.search_procedures.assert_not_called()

    @pytest.mark.asyncio
    async def test_critic_track_survives_passive_off(self):
        """Critic-recommended skills (Track A) have no recall_deep equivalent, so they
        must still reach context when passive embeddings are off."""
        from unittest.mock import AsyncMock
        engine = self._engine(proc_passive_injection_enabled=False, critic_skill_injection="enabled")
        engine._heart.get_procedure_by_name = AsyncMock(return_value=_proc_summary("critic-pick"))
        r = await self._build(engine, critic_skills=["critic-pick"])
        sec = [s for s in r.sections if s.label == "Known Procedures"]
        assert sec, "Critic track must still inject when passive embeddings are off"
        assert "critic-pick" in sec[0].content
        engine._heart.search_procedures.assert_not_called()

    # --- Catalog (breadth) ----------------------------------------------------

    @pytest.mark.asyncio
    async def test_catalog_absent_by_default(self):
        engine = self._engine()
        r = await self._build(engine)
        assert not any(s.label == "Procedure Catalog" for s in r.sections)
        engine._heart.list_procedures.assert_not_called()

    @pytest.mark.asyncio
    async def test_catalog_lists_procedures_when_enabled(self):
        engine = self._engine(proc_catalog_enabled=True)
        r = await self._build(engine)
        cat = [s for s in r.sections if s.label == "Procedure Catalog"]
        assert len(cat) == 1 and cat[0].tier == "static"
        assert "p1" in cat[0].content and "p2" in cat[0].content
        assert "get_procedure" in cat[0].content  # tells the agent how to load depth

    @pytest.mark.asyncio
    async def test_catalog_excludes_volatile_fields(self):
        """Cache-stability: activation/effectiveness change per use, so the catalog
        must NOT render them (only name/domain/desc)."""
        engine = self._engine(proc_catalog_enabled=True)
        r = await self._build(engine)
        content = next(s.content for s in r.sections if s.label == "Procedure Catalog")
        assert "activated" not in content.lower()
        assert "effectiveness" not in content.lower()
        assert "7x" not in content  # the mock's activation_count

    @pytest.mark.asyncio
    async def test_catalog_byte_stable_across_turns(self):
        engine = self._engine(proc_catalog_enabled=True)
        r1 = await self._build(engine, text="task one")
        r2 = await self._build(engine, text="a totally different task two")
        c1 = next(s.content for s in r1.sections if s.label == "Procedure Catalog")
        c2 = next(s.content for s in r2.sections if s.label == "Procedure Catalog")
        assert c1 == c2  # query-independent -> cacheable

    @pytest.mark.asyncio
    async def test_catalog_overrides_cue(self):
        """When the full catalog is on, the cue-only fallback is suppressed (one surface)."""
        engine = self._engine(proc_catalog_enabled=True, proc_awareness_cue=True)
        r = await self._build(engine)
        assert any(s.label == "Procedure Catalog" for s in r.sections)
        assert not any(s.label == "Procedure Awareness" for s in r.sections)

    @pytest.mark.asyncio
    async def test_cue_fallback_when_catalog_off(self):
        engine = self._engine(proc_catalog_enabled=False, proc_awareness_cue=True)
        r = await self._build(engine)
        aware = [s for s in r.sections if s.label == "Procedure Awareness"]
        assert len(aware) == 1 and aware[0].tier == "static"
        assert not any(s.label == "Procedure Catalog" for s in r.sections)


def test_decision_pipeline_carries_pattern():
    from nous.api.retrieval_pipeline import _decisions_to_pipeline
    from nous.brain.schemas import DecisionSummary
    d = DecisionSummary(
        id=uuid4(), description="chose cursor pagination", confidence=0.8,
        category="tooling", stakes="medium", outcome="success",
        pattern="prefer-cursor-pagination", created_at=datetime.now(timezone.utc),
    )
    assert _decisions_to_pipeline([d])[0].metadata["pattern"] == "prefer-cursor-pagination"


class TestGetProcedureToolResolution:
    """Catalog-first DEPTH: get_procedure must resolve by NAME (what the catalog shows),
    not only UUID, and render the full body (core_patterns + implementation_notes)."""

    def _dispatcher(self, detail):
        from unittest.mock import AsyncMock, MagicMock
        from nous.api.tools import ToolDispatcher, register_nous_tools
        from nous.config import Settings
        heart = MagicMock()
        heart.get_procedure = AsyncMock(return_value=detail)
        heart.get_procedure_by_name = AsyncMock(return_value=detail)
        brain = MagicMock()
        d = ToolDispatcher()
        register_nous_tools(d, brain, heart, Settings(_env_file=None))
        return d, heart

    @staticmethod
    def _detail(name="report-skill"):
        return SimpleNamespace(
            name=name, domain="reporting", description="produce a report",
            goals=["report"], core_tools=["write_file"],
            core_patterns=["start with an executive summary"],
            implementation_notes=["gather metrics first"],
            activation_count=3, active=True, effectiveness=0.5,
        )

    @pytest.mark.asyncio
    async def test_resolves_by_name_and_renders_full_body(self):
        detail = self._detail()
        d, heart = self._dispatcher(detail)
        out = await d._handlers["get_procedure"]("report-skill")
        text = out["content"][0]["text"]
        heart.get_procedure_by_name.assert_awaited_once_with("report-skill")
        heart.get_procedure.assert_not_called()  # name -> no UUID path
        assert "executive summary" in text       # core_patterns rendered
        assert "gather metrics first" in text     # implementation_notes rendered

    @pytest.mark.asyncio
    async def test_resolves_by_uuid(self):
        detail = self._detail()
        d, heart = self._dispatcher(detail)
        pid = str(uuid4())
        await d._handlers["get_procedure"](pid)
        heart.get_procedure.assert_awaited_once()
        heart.get_procedure_by_name.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_name_returns_not_found(self):
        d, _ = self._dispatcher(None)
        out = await d._handlers["get_procedure"]("nope")
        assert "No procedure found" in out["content"][0]["text"]
