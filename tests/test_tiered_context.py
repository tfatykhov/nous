"""Tests for Spec 008 PR 2 — Tiered Context Model.

Verifies:
- Tier 1: User profile facts always loaded (no search)
- Tier 3: Thresholds filter low-relevance results
- Tier 1 categories excluded from Tier 3 fact search
- Budget includes user_profile field
"""


import uuid as _uuid

import pytest
import pytest_asyncio

from nous.brain.brain import Brain
from nous.cognitive.context import ContextEngine
from nous.cognitive.schemas import ContextBudget, FrameSelection
from nous.config import Settings
from nous.heart import FactInput, Heart


# ---------------------------------------------------------------------------
# Fixtures — use unique agent_id to isolate from other tests' data
# ---------------------------------------------------------------------------

_TIERED_AGENT_ID = f"test-tiered-context-{_uuid.uuid4().hex[:8]}"


@pytest.fixture
def tiered_settings(settings):
    return settings.model_copy(update={"agent_id": _TIERED_AGENT_ID})


@pytest_asyncio.fixture(autouse=True)
async def _ensure_agent(db):
    """Create test agent in DB so FK constraints pass."""
    from sqlalchemy import text
    async with db.session() as session:
        await session.execute(
            text("INSERT INTO nous_system.agents (id, name, config) VALUES (:id, :name, '{}'::jsonb) ON CONFLICT (id) DO NOTHING"),
            {"id": _TIERED_AGENT_ID, "name": "Test Tiered Agent"},
        )
        await session.commit()


@pytest_asyncio.fixture
async def brain(db, tiered_settings):
    b = Brain(database=db, settings=tiered_settings)
    yield b
    await b.close()


@pytest_asyncio.fixture
async def heart(db, mock_embeddings, tiered_settings):
    h = Heart(db, tiered_settings, embedding_provider=mock_embeddings)
    yield h
    await h.close()


@pytest_asyncio.fixture
async def context_engine(brain, heart, tiered_settings):
    return ContextEngine(brain, heart, tiered_settings, identity_prompt="You are Nous.")


def _frame(frame_id: str = "task") -> FrameSelection:
    return FrameSelection(
        frame_id=frame_id,
        frame_name="Task",
        confidence=0.9,
        match_method="pattern",
        default_category="tooling",
        default_stakes="medium",
    )


async def _fresh_engine(db, mock_embeddings, base_settings, *, identity_prompt: str, settings_update: dict | None = None):
    """Fresh-agent isolation: mint a unique agent so committed facts from other
    tests in this module (session-scoped db, no truncation) can't pollute
    presence/absence/count assertions. Derives from the conftest `settings`
    fixture (NOT a raw Settings() — that would re-read env/.env and drift from
    the test config). Returns (engine, heart, settings). Caller must
    `await heart.close()` when done."""
    from sqlalchemy import text as sqltext
    agent_id = f"test-upf-{_uuid.uuid4().hex[:8]}"
    async with db.session() as session:
        await session.execute(
            sqltext("INSERT INTO nous_system.agents (id, name, config) VALUES (:id, :name, '{}'::jsonb) ON CONFLICT (id) DO NOTHING"),
            {"id": agent_id, "name": "UPF Test Agent"},
        )
        await session.commit()
    upd = {"agent_id": agent_id}
    if settings_update:
        upd.update(settings_update)
    s = base_settings.model_copy(update=upd)
    heart = Heart(db, s, embedding_provider=mock_embeddings)
    brain = Brain(database=db, settings=s)
    engine = ContextEngine(brain, heart, s, identity_prompt=identity_prompt)
    return engine, heart, s


def _profile_section(result):
    return next((s for s in result.sections if s.label == "User Profile"), None)


# ---------------------------------------------------------------------------
# Budget
# ---------------------------------------------------------------------------


class TestContextBudget:
    def test_user_profile_field_exists(self):
        budget = ContextBudget()
        assert hasattr(budget, "user_profile")
        assert budget.user_profile == 200

    def test_user_profile_in_frame_budgets(self):
        for frame_id in ["conversation", "question", "task", "decision", "creative", "debug"]:
            budget = ContextBudget.for_frame(frame_id)
            assert hasattr(budget, "user_profile")


# ---------------------------------------------------------------------------
# Tier 1: User Profile (always loaded)
# ---------------------------------------------------------------------------


class TestTier1UserProfile:
    @pytest.mark.asyncio
    async def test_profile_facts_in_context(self, context_engine, heart, db):
        """Preference/person/rule facts appear in User Profile section."""
        async with db.session() as session:
            await heart.learn(
                FactInput(content="Tim prefers Celsius for all temperature readings", category="preference", subject="Tim"),
                session=session,
            )
            await heart.learn(
                FactInput(content="Tim lives in Silver Spring MD in the United States", category="person", subject="Tim"),
                session=session,
            )
            await session.commit()

        result = await context_engine.build(
            agent_id="test-agent",
            session_id="test-session",
            input_text="what is the weather?",
            frame=_frame(),
        )

        labels = [s.label for s in result.sections]
        assert "User Profile" in labels

        profile = next(s for s in result.sections if s.label == "User Profile")
        assert "Celsius" in profile.content
        assert "Silver Spring" in profile.content

    @pytest.mark.asyncio
    async def test_profile_facts_excluded_from_tier3(self, context_engine, heart, db):
        """Preference facts should NOT appear in Relevant Facts (Tier 3)."""
        async with db.session() as session:
            await heart.learn(
                FactInput(content="Tim prefers Celsius for all temperature readings", category="preference", subject="Tim"),
                session=session,
            )
            await heart.learn(
                FactInput(content="Nous uses PostgreSQL as its primary database", category="technical", subject="Nous"),
                session=session,
            )
            await session.commit()

        result = await context_engine.build(
            agent_id="test-agent",
            session_id="test-session",
            input_text="tell me about Nous database",
            frame=_frame(),
        )

        # Tier 3 facts should have technical but NOT preference
        tier3_facts = next((s for s in result.sections if s.label == "Relevant Facts"), None)
        if tier3_facts:
            assert "Celsius" not in tier3_facts.content

    @pytest.mark.asyncio
    async def test_no_profile_section_when_empty(self, db, mock_embeddings):
        """No User Profile section when no preference/person/rule facts exist."""
        # Use a completely fresh agent_id with no facts at all
        fresh_id = f"test-empty-profile-{_uuid.uuid4().hex[:8]}"
        from sqlalchemy import text as sql_text
        async with db.session() as session:
            await session.execute(
                sql_text("INSERT INTO nous_system.agents (id, name, config) VALUES (:id, :name, '{}'::jsonb) ON CONFLICT (id) DO NOTHING"),
                {"id": fresh_id, "name": "Empty Profile Agent"},
            )
            await session.commit()

        fresh_settings = Settings().model_copy(update={"agent_id": fresh_id})
        fresh_brain = Brain(database=db, settings=fresh_settings)
        fresh_heart = Heart(db, fresh_settings, embedding_provider=mock_embeddings)
        fresh_engine = ContextEngine(fresh_brain, fresh_heart, fresh_settings, identity_prompt="You are Nous.")

        result = await fresh_engine.build(
            agent_id=fresh_id,
            session_id="test-session",
            input_text="hello",
            frame=_frame(),
        )

        labels = [s.label for s in result.sections]
        assert "User Profile" not in labels
        await fresh_brain.close()
        await fresh_heart.close()


# ---------------------------------------------------------------------------
# Tier 3: Thresholds
# ---------------------------------------------------------------------------


class TestTier3Thresholds:
    @pytest.mark.asyncio
    async def test_budget_user_profile_override(self):
        """user_profile budget can be overridden."""
        budget = ContextBudget()
        budget.apply_overrides({"user_profile": 500})
        assert budget.user_profile == 500


# ---------------------------------------------------------------------------
# list_by_category
# ---------------------------------------------------------------------------


class TestListByCategory:
    @pytest.mark.asyncio
    async def test_returns_matching_categories(self, db, mock_embeddings):
        """list_facts_by_category returns only facts in specified categories."""
        # Use fresh agent_id to avoid accumulation from other tests
        fresh_id = f"test-list-cat-{_uuid.uuid4().hex[:8]}"
        from sqlalchemy import text as sql_text
        async with db.session() as session:
            await session.execute(
                sql_text("INSERT INTO nous_system.agents (id, name, config) VALUES (:id, :name, '{}'::jsonb) ON CONFLICT (id) DO NOTHING"),
                {"id": fresh_id, "name": "List Category Agent"},
            )
            await session.commit()

        fresh_settings = Settings().model_copy(update={"agent_id": fresh_id})
        fresh_heart = Heart(db, fresh_settings, embedding_provider=mock_embeddings)

        async with db.session() as session:
            await fresh_heart.learn(
                FactInput(content="Tim prefers Celsius for all temperature readings", category="preference", subject="Tim"),
                session=session,
            )
            await fresh_heart.learn(
                FactInput(content="Nous uses Postgres as its primary database", category="technical", subject="Nous"),
                session=session,
            )
            await session.commit()

        facts = await fresh_heart.list_facts_by_category(categories=["preference", "person", "rule"])
        assert len(facts) == 1
        assert "Celsius" in facts[0].content
        await fresh_heart.close()

    @pytest.mark.asyncio
    async def test_excludes_inactive(self, db, mock_embeddings):
        """list_facts_by_category skips inactive facts by default."""
        fresh_id = f"test-list-inact-{_uuid.uuid4().hex[:8]}"
        from sqlalchemy import text as sql_text
        async with db.session() as session:
            await session.execute(
                sql_text("INSERT INTO nous_system.agents (id, name, config) VALUES (:id, :name, '{}'::jsonb) ON CONFLICT (id) DO NOTHING"),
                {"id": fresh_id, "name": "List Inactive Agent"},
            )
            await session.commit()

        fresh_settings = Settings().model_copy(update={"agent_id": fresh_id})
        fresh_heart = Heart(db, fresh_settings, embedding_provider=mock_embeddings)

        async with db.session() as session:
            result = await fresh_heart.learn(
                FactInput(content="Old preference that is no longer relevant", category="preference", subject="Tim"),
                session=session,
            )
            await fresh_heart.deactivate_fact(result.id, session=session)
            await session.commit()

        facts = await fresh_heart.list_facts_by_category(categories=["preference"])
        assert len(facts) == 0
        await fresh_heart.close()


from nous.cognitive.context import _identity_coverage


class TestIdentityCoverage:
    """Directional per-line dedup helper (P1 fix)."""

    def test_verbatim_seeded_fact_fully_covered(self):
        # auto_seed_from_facts writes facts verbatim as "- {content}" lines
        fact = "Tim prefers Celsius for all temperature readings"
        identity_lines = [
            "### Preferences",
            "- Tim prefers Celsius for all temperature readings",
            "- Tim wants concise answers",
        ]
        assert _identity_coverage(fact, identity_lines) >= 0.99

    def test_scattered_vocabulary_not_covered(self):
        # Words appear ACROSS lines but no single line covers the fact —
        # the blob-level bug this helper fixes (max single-line ≈ 0.43)
        fact = "Tim prefers email delivery for weekly reports"
        identity_lines = [
            "- Tim prefers Celsius for temperature",
            "- send delivery notifications to Telegram",
            "- weekly summary reports enabled",
            "- contact via email is tfatykhov@gmail.com",
        ]
        assert _identity_coverage(fact, identity_lines) < 0.6

    def test_correction_survives_line_threshold(self):
        # devil-P2: a same-slot CORRECTION shares scaffolding words with the
        # bullet it corrects (4/6 ≈ 0.667) — must stay BELOW the 0.75 line
        # threshold so corrections reach the prompt, while verbatim (1.0) dedups.
        from nous.cognitive.context import _IDENTITY_LINE_COVERAGE_THRESHOLD
        fact = "Tim prefers Celsius for temperature readings"
        identity_lines = ["- Tim prefers Fahrenheit for temperature"]
        cov = _identity_coverage(fact, identity_lines)
        assert 0.6 <= cov < _IDENTITY_LINE_COVERAGE_THRESHOLD

    def test_short_header_line_does_not_suppress(self):
        # Directional coverage: a short "### Preferences" header must not
        # cover a fact that merely contains the word "preferences" (1/8 = 0.125)
        fact = "Tim has strong preferences about code review workflows"
        identity_lines = ["### Preferences"]
        assert _identity_coverage(fact, identity_lines) < 0.3

    def test_single_line_identity_equals_blob_metric(self):
        # devil-P2a: on a single-line prose identity, per-line coverage equals
        # the legacy blob metric (fact is the smaller set) — a DELIBERATE no-op,
        # pinned here so it is never mistaken for a regression.
        from nous.utils import text_overlap
        fact = "Tim is a cognitive agent developer"
        prose = "Tim is a cognitive agent developer building Nous on Minsky principles"
        assert abs(_identity_coverage(fact, [prose]) - text_overlap(fact, prose)) < 1e-9

    def test_empty_inputs(self):
        assert _identity_coverage("", ["- something"]) == 0.0
        assert _identity_coverage("a fact here", []) == 0.0


class TestProfileDedupScope:
    @pytest.mark.asyncio
    async def test_line_scope_retains_scattered_vocab_fact(self, db, mock_embeddings, settings):
        """A fact whose words are scattered across identity lines survives 'line' scope."""
        identity = (
            "### Preferences\n"
            "- Tim prefers Celsius for temperature\n"
            "- send delivery notifications to Telegram\n"
            "- weekly summary reports enabled\n"
            "- contact via email always"
        )
        engine, heart, s = await _fresh_engine(db, mock_embeddings, settings, identity_prompt=identity)
        try:
            async with db.session() as session:
                await heart.learn(
                    FactInput(content="Tim prefers email delivery for weekly reports", category="preference", subject="Tim"),
                    session=session,
                )
                await session.commit()
            result = await engine.build(
                agent_id=s.agent_id, session_id="s-dedup-line",
                input_text="hello", frame=_frame(),
            )
            profile = _profile_section(result)
            assert profile is not None
            assert "email delivery for weekly reports" in profile.content
        finally:
            await heart.close()

    @pytest.mark.asyncio
    async def test_line_scope_still_dedups_verbatim_seeded_fact(self, db, mock_embeddings, settings):
        """A fact restated verbatim as an identity bullet is still suppressed.
        (Green-first pin: legacy blob mode also suppresses this — correctness-F1.)"""
        content = "Tim prefers Celsius for all temperature readings"
        engine, heart, s = await _fresh_engine(
            db, mock_embeddings, settings, identity_prompt=f"### Preferences\n- {content}",
        )
        try:
            async with db.session() as session:
                await heart.learn(
                    FactInput(content=content, category="preference", subject="Tim"),
                    session=session,
                )
                await session.commit()
            result = await engine.build(
                agent_id=s.agent_id, session_id="s-dedup-verbatim",
                input_text="hello", frame=_frame(),
            )
            profile = _profile_section(result)
            assert profile is None or content not in profile.content
        finally:
            await heart.close()

    @pytest.mark.asyncio
    async def test_blob_scope_reproduces_legacy_suppression(self, db, mock_embeddings, settings):
        """scope='blob' suppresses the scattered-vocab fact exactly like today."""
        identity = (
            "### Preferences\n"
            "- Tim prefers Celsius for temperature\n"
            "- send delivery notifications to Telegram\n"
            "- weekly summary reports enabled\n"
            "- contact via email always"
        )
        engine, heart, s = await _fresh_engine(
            db, mock_embeddings, settings, identity_prompt=identity,
            settings_update={"profile_identity_dedup_scope": "blob"},
        )
        try:
            async with db.session() as session:
                await heart.learn(
                    FactInput(content="Tim prefers email delivery for weekly reports", category="preference", subject="Tim"),
                    session=session,
                )
                await session.commit()
            result = await engine.build(
                agent_id=s.agent_id, session_id="s-dedup-blob",
                input_text="hello", frame=_frame(),
            )
            profile = _profile_section(result)
            assert profile is None or "email delivery for weekly reports" not in profile.content
        finally:
            await heart.close()

    @pytest.mark.asyncio
    async def test_unknown_scope_falls_back_to_blob(self, db, mock_embeddings, settings):
        """tests-P3-1: a typo'd scope value degrades to legacy blob suppression,
        never to no-dedup."""
        identity = (
            "### Preferences\n"
            "- Tim prefers Celsius for temperature\n"
            "- send delivery notifications to Telegram\n"
            "- weekly summary reports enabled\n"
            "- contact via email always"
        )
        engine, heart, s = await _fresh_engine(
            db, mock_embeddings, settings, identity_prompt=identity,
            settings_update={"profile_identity_dedup_scope": "garbage"},
        )
        try:
            async with db.session() as session:
                await heart.learn(
                    FactInput(content="Tim prefers email delivery for weekly reports", category="preference", subject="Tim"),
                    session=session,
                )
                await session.commit()
            result = await engine.build(
                agent_id=s.agent_id, session_id="s-dedup-typo",
                input_text="hello", frame=_frame(),
            )
            profile = _profile_section(result)
            assert profile is None or "email delivery for weekly reports" not in profile.content
        finally:
            await heart.close()


class TestTier1Selection:
    @pytest.mark.asyncio
    async def test_equal_confidence_ordered_by_learned_at_desc(self, db, mock_embeddings, settings):
        """learned_at DESC tiebreak: equal-confidence facts come newest-first.
        NOTE (correctness-F3 / tests-P1-2): pre-implementation the tie order is
        DB-UNDEFINED, so the red run may pass by luck occasionally — 5 rows make
        accidental full order ~1/120. Post-implementation it is deterministic."""
        from datetime import datetime, timedelta, timezone
        from sqlalchemy import text as sqltext
        engine, heart, s = await _fresh_engine(db, mock_embeddings, settings, identity_prompt="")
        try:
            ids = []
            base = datetime.now(timezone.utc)
            async with db.session() as session:
                for i in range(5):
                    r = await heart.learn(
                        FactInput(content=f"Tim distinct person fact number {i} here", category="person", subject=f"tb-{i}", confidence=0.9),
                        session=session,
                    )
                    await session.execute(
                        sqltext("UPDATE heart.facts SET learned_at = :t WHERE id = :id"),
                        {"t": base - timedelta(days=i), "id": r.id},
                    )
                    ids.append(r.id)  # ids[0] newest ... ids[4] oldest
                await session.commit()
            got = [f.id for f in await heart.list_facts_by_category(categories=["person"], limit=50)]
            assert [i for i in got if i in ids] == ids  # strict learned_at DESC within equal confidence
        finally:
            await heart.close()

    @pytest.mark.asyncio
    async def test_profile_limit_setting_respected(self, db, mock_embeddings, settings):
        """NOUS_PROFILE_FACT_LIMIT caps the Tier-1 fetch. Fresh agent + exactly 4
        facts + empty identity (dedup skipped) => exactly 2 bullets render.
        NOTE (correctness-F2): model_copy(update=...) does NOT raise for the
        not-yet-existing field pre-impl; the red assertion is the bullet count
        (4 > 2 because the limit isn't consumed)."""
        engine, heart, s = await _fresh_engine(
            db, mock_embeddings, settings, identity_prompt="",
            settings_update={"profile_fact_limit": 2},
        )
        try:
            async with db.session() as session:
                for i in range(4):
                    await heart.learn(
                        FactInput(content=f"Distinct preference number {i} about unrelated topic {i}", category="preference", subject=f"limit-subj-{i}"),
                        session=session,
                    )
                await session.commit()
            result = await engine.build(
                agent_id=s.agent_id, session_id="s-limit",
                input_text="hello", frame=_frame(),
            )
            profile = _profile_section(result)
            assert profile is not None  # anti-vacuous (tests-P2-1)
            bullet_count = sum(
                1 for ln in profile.content.splitlines() if ln.strip().startswith("- ")
            )
            assert bullet_count == 2
        finally:
            await heart.close()

    @pytest.mark.asyncio
    async def test_recency_pass_tags_superseded_profile_fact(self, db, mock_embeddings, settings):
        """With BOTH profile_recency_enabled and recency_resolver_enabled on,
        conflicting same-subject dated facts get current/superseded tags in the
        User Profile section and the older sinks last. Fresh agent with ONLY
        these 2 facts (tests-P1-3: shared-agent budget pressure could truncate
        the demoted tail line and break the assertion)."""
        from datetime import date
        engine, heart, s = await _fresh_engine(
            db, mock_embeddings, settings, identity_prompt="",
            settings_update={"profile_recency_enabled": True, "recency_resolver_enabled": True},
        )
        try:
            async with db.session() as session:
                await heart.learn(
                    FactInput(content="Tim works at Initech as senior engineer", category="person", subject="recency-subj", event_date=date(2025, 1, 15)),
                    session=session,
                )
                await heart.learn(
                    FactInput(content="Tim works at Globex as senior engineer", category="person", subject="recency-subj", event_date=date(2026, 6, 15)),
                    session=session,
                )
                await session.commit()
            result = await engine.build(
                agent_id=s.agent_id, session_id="s-recency",
                input_text="hello", frame=_frame(),
            )
            profile = _profile_section(result)
            assert profile is not None
            assert "[superseded 2025-01]" in profile.content
            assert "[current 2026-06]" in profile.content
            assert profile.content.index("Globex") < profile.content.index("Initech")
        finally:
            await heart.close()

    @pytest.mark.asyncio
    async def test_recency_pass_dark_by_default(self, db, mock_embeddings, settings):
        """devil-P1: with profile_recency_enabled at its default (False), NO tags
        appear even though recency_resolver_enabled=True (prod's config)."""
        from datetime import date
        engine, heart, s = await _fresh_engine(
            db, mock_embeddings, settings, identity_prompt="",
            settings_update={"recency_resolver_enabled": True},
        )
        try:
            async with db.session() as session:
                await heart.learn(
                    FactInput(content="Tim works at Initech as senior engineer", category="person", subject="recency-subj", event_date=date(2025, 1, 15)),
                    session=session,
                )
                await heart.learn(
                    FactInput(content="Tim works at Globex as senior engineer", category="person", subject="recency-subj", event_date=date(2026, 6, 15)),
                    session=session,
                )
                await session.commit()
            result = await engine.build(
                agent_id=s.agent_id, session_id="s-recency-dark",
                input_text="hello", frame=_frame(),
            )
            profile = _profile_section(result)
            assert profile is not None
            assert "[superseded" not in profile.content
            assert "[current" not in profile.content
        finally:
            await heart.close()


class TestLineAwareTruncation:
    @pytest.mark.asyncio
    async def test_drops_whole_lines_only(self, db, mock_embeddings, settings):
        engine, heart, s = await _fresh_engine(db, mock_embeddings, settings, identity_prompt="")
        try:
            lines = [f"- fact number {i} with some padding text here" for i in range(10)]
            text = "\n".join(lines)
            # Budget fits ~3 lines: 3 lines * ~44 chars ≈ 131 chars ≤ 33*4=132
            out = engine._truncate_to_budget_lines(text, 33)
            assert len(out) <= 33 * engine.CHARS_PER_TOKEN
            for ln in out.split("\n"):
                assert ln in lines  # every emitted line is intact — no mid-word slice
            assert not out.endswith("...")
        finally:
            await heart.close()

    @pytest.mark.asyncio
    async def test_under_budget_unchanged(self, db, mock_embeddings, settings):
        engine, heart, s = await _fresh_engine(db, mock_embeddings, settings, identity_prompt="")
        try:
            text = "- short line"
            assert engine._truncate_to_budget_lines(text, 100) == text
        finally:
            await heart.close()

    @pytest.mark.asyncio
    async def test_single_huge_line_falls_back_to_char_slice(self, db, mock_embeddings, settings):
        engine, heart, s = await _fresh_engine(db, mock_embeddings, settings, identity_prompt="")
        try:
            text = "x" * 10_000
            out = engine._truncate_to_budget_lines(text, 25)
            assert out == engine._truncate_to_budget(text, 25)
            assert out.endswith("...")
        finally:
            await heart.close()


class TestProfileInstrumentation:
    @pytest.mark.asyncio
    async def test_all_deduped_state_logged_and_section_omitted(self, db, mock_embeddings, settings, caplog):
        """tests-P3-2 + observability: all-facts-deduped is distinguishable from
        no-facts-exist (raw=1 deduped_out=1 final=0), and the ContextSection is
        omitted entirely."""
        import logging
        content = "Tim uses spaces not tabs consistently everywhere"
        engine, heart, s = await _fresh_engine(
            db, mock_embeddings, settings, identity_prompt=f"### Preferences\n- {content}",
        )
        try:
            async with db.session() as session:
                await heart.learn(
                    FactInput(content=content, category="preference", subject="instr-subj"),
                    session=session,
                )
                await session.commit()
            with caplog.at_level(logging.INFO, logger="nous.cognitive.context"):
                result = await engine.build(
                    agent_id=s.agent_id, session_id="s-instr",
                    input_text="hello", frame=_frame(),
                )
            assert _profile_section(result) is None
            profile_logs = [r for r in caplog.records if "User Profile:" in r.getMessage()]
            assert profile_logs, "expected a User Profile instrumentation log line"
            msg = profile_logs[0].getMessage()
            assert "raw=1" in msg and "deduped_out=1" in msg and "final=0" in msg
        finally:
            await heart.close()

    @pytest.mark.asyncio
    async def test_truncated_flag_true_when_budget_tiny(self, db, mock_embeddings, settings, caplog):
        """tests-P3-3: the truncated=True branch fires under a tiny budget."""
        import logging
        engine, heart, s = await _fresh_engine(
            db, mock_embeddings, settings, identity_prompt="",
            settings_update={"context_budget_overrides": {"user_profile": 10}},
        )
        try:
            async with db.session() as session:
                for i in range(5):
                    await heart.learn(
                        FactInput(content=f"Verbose distinct preference number {i} with plenty of padding words attached", category="preference", subject=f"trunc-{i}"),
                        session=session,
                    )
                await session.commit()
            with caplog.at_level(logging.INFO, logger="nous.cognitive.context"):
                await engine.build(
                    agent_id=s.agent_id, session_id="s-trunc",
                    input_text="hello", frame=_frame(),
                )
            profile_logs = [r for r in caplog.records if "User Profile:" in r.getMessage()]
            assert profile_logs and "truncated=True" in profile_logs[0].getMessage()
        finally:
            await heart.close()


class TestSectionOrder:
    @pytest.mark.asyncio
    async def test_identity_precedes_user_profile(self, db, mock_embeddings, settings):
        """Both sections carry priority=1; stable sort makes insertion order the
        contract. Pin it so a build() body reorder can't silently flip the prompt."""
        engine, heart, s = await _fresh_engine(
            db, mock_embeddings, settings,
            identity_prompt="You are Nous, a cognitive agent for testing.",
        )
        try:
            async with db.session() as session:
                await heart.learn(
                    FactInput(content="Tim enjoys hiking in national parks on weekends", category="person", subject="order-subj"),
                    session=session,
                )
                await session.commit()
            result = await engine.build(
                agent_id=s.agent_id, session_id="s-order",
                input_text="hello", frame=_frame(),
            )
            sp = result.system_prompt
            assert "## Identity" in sp and "## User Profile" in sp
            assert sp.index("## Identity") < sp.index("## User Profile")
        finally:
            await heart.close()


class TestProfileSourceExclusion:
    """2026-07-24 pollution fix: reflection-sourced facts are excluded from the
    Tier-1 User Profile at SQL level (NULL-source legacy facts always kept)."""

    @pytest.mark.asyncio
    async def test_reflection_source_excluded_null_source_kept(self, db, mock_embeddings, settings):
        engine, heart, s = await _fresh_engine(db, mock_embeddings, settings, identity_prompt="")
        try:
            async with db.session() as session:
                await heart.learn(
                    FactInput(content="Legacy pollution lesson about firmware update procedures", category="rule", subject="pol-1", source="reflection"),
                    session=session,
                )
                await heart.learn(
                    FactInput(content="Tim prefers dark roast coffee every single morning", category="preference", subject="pol-2"),
                    session=session,
                )
                await session.commit()
            result = await engine.build(
                agent_id=s.agent_id, session_id="s-srcexcl",
                input_text="hello", frame=_frame(),
            )
            profile = _profile_section(result)
            assert profile is not None
            assert "dark roast coffee" in profile.content  # NULL-source kept
            assert "firmware update procedures" not in profile.content  # reflection excluded
        finally:
            await heart.close()

    @pytest.mark.asyncio
    async def test_empty_exclusion_list_restores_legacy(self, db, mock_embeddings, settings):
        engine, heart, s = await _fresh_engine(
            db, mock_embeddings, settings, identity_prompt="",
            settings_update={"profile_exclude_sources": []},
        )
        try:
            async with db.session() as session:
                await heart.learn(
                    FactInput(content="Legacy pollution lesson about widget calibration steps", category="rule", subject="pol-3", source="reflection"),
                    session=session,
                )
                await session.commit()
            result = await engine.build(
                agent_id=s.agent_id, session_id="s-srcexcl-off",
                input_text="hello", frame=_frame(),
            )
            profile = _profile_section(result)
            assert profile is not None
            assert "widget calibration steps" in profile.content
        finally:
            await heart.close()

    @pytest.mark.asyncio
    async def test_sleep_reflected_preference_kept(self, db, mock_embeddings, settings):
        """codex r1: the exclusion is category-scoped — a GENUINE sleep-reflected
        preference must stay (Tier-1 is its only pre-turn channel; tier-1
        categories are excluded from tier-3 search)."""
        engine, heart, s = await _fresh_engine(db, mock_embeddings, settings, identity_prompt="")
        try:
            async with db.session() as session:
                await heart.learn(
                    FactInput(content="Tim prefers weekly summaries delivered on Monday mornings", category="preference", subject="pol-4", source="sleep_reflection"),
                    session=session,
                )
                await heart.learn(
                    FactInput(content="Legacy pollution lesson about cache eviction tuning", category="rule", subject="pol-5", source="sleep_reflection"),
                    session=session,
                )
                await session.commit()
            result = await engine.build(
                agent_id=s.agent_id, session_id="s-srcexcl-pref",
                input_text="hello", frame=_frame(),
            )
            profile = _profile_section(result)
            assert profile is not None
            assert "weekly summaries delivered on Monday" in profile.content  # preference kept
            assert "cache eviction tuning" not in profile.content  # rule excluded
        finally:
            await heart.close()


async def _tag_fact(db, fact_id, tag="profile_core"):
    """Add a tag to a fact's ARRAY column via SQL (Task 3 builds the API path)."""
    from sqlalchemy import text as sqltext
    async with db.session() as session:
        await session.execute(
            sqltext("UPDATE heart.facts SET tags = array_append(coalesce(tags, '{}'::text[]), :tag) WHERE id = :id"),
            {"tag": tag, "id": fact_id},
        )
        await session.commit()


async def _set_learned_at(db, fact_id, when):
    from sqlalchemy import text as sqltext
    async with db.session() as session:
        await session.execute(
            sqltext("UPDATE heart.facts SET learned_at = :t WHERE id = :id"),
            {"t": when, "id": fact_id},
        )
        await session.commit()


class TestProfileCore:
    # codex r2 (#574): fresh-engine fixtures use Postgres-only SQL (::jsonb,
    # generated tsvector) — skip cleanly under the default sqlite backend.
    pytestmark = pytest.mark.postgres_only

    """Task 1: curated core (PROFILE_CORE_TAG) + probation window selection."""

    @pytest.mark.asyncio
    async def test_tagged_only_render(self, db, mock_embeddings, settings):
        """core on, probation off: only the tagged fact renders."""
        engine, heart, s = await _fresh_engine(
            db, mock_embeddings, settings, identity_prompt="",
            settings_update={"profile_core_enabled": True, "profile_core_probation_days": 0},
        )
        try:
            async with db.session() as session:
                r_tag = await heart.learn(
                    FactInput(content="Tim prefers Celsius for all temperature readings", category="preference", subject="core-tag"),
                    session=session,
                )
                await heart.learn(
                    FactInput(content="Tim enjoys sailing near the marina on weekends", category="person", subject="core-untag"),
                    session=session,
                )
                await session.commit()
            await _tag_fact(db, r_tag.id)
            result = await engine.build(
                agent_id=s.agent_id, session_id="s-core-tag",
                input_text="hello", frame=_frame(),
            )
            profile = _profile_section(result)
            assert profile is not None
            assert "Celsius" in profile.content
            assert "sailing near the marina" not in profile.content
        finally:
            await heart.close()

    @pytest.mark.asyncio
    async def test_probation_fresh_appears_old_absent(self, db, mock_embeddings, settings):
        """core on, no tags: a fresh untagged fact joins via probation; an old one does not."""
        from datetime import datetime, timedelta, timezone
        engine, heart, s = await _fresh_engine(
            db, mock_embeddings, settings, identity_prompt="",
            settings_update={"profile_core_enabled": True, "profile_core_probation_days": 14},
        )
        try:
            async with db.session() as session:
                r_fresh = await heart.learn(
                    FactInput(content="Tim just adopted a beagle named Biscuit recently", category="person", subject="prob-fresh"),
                    session=session,
                )
                r_old = await heart.learn(
                    FactInput(content="Tim once visited the Grand Canyon long ago", category="person", subject="prob-old"),
                    session=session,
                )
                await session.commit()
            now = datetime.now(timezone.utc)
            await _set_learned_at(db, r_fresh.id, now)
            await _set_learned_at(db, r_old.id, now - timedelta(days=30))
            result = await engine.build(
                agent_id=s.agent_id, session_id="s-core-prob",
                input_text="hello", frame=_frame(),
            )
            profile = _profile_section(result)
            assert profile is not None
            assert "beagle named Biscuit" in profile.content
            assert "Grand Canyon" not in profile.content
        finally:
            await heart.close()

    @pytest.mark.asyncio
    async def test_tagged_bypasses_identity_dedup(self, db, mock_embeddings, settings):
        """A tagged fact whose content is fully covered by an identity line still
        renders (explicit tag outranks the dedup heuristic). Legacy would drop it
        (see TestProfileInstrumentation::test_all_deduped_state)."""
        content = "Tim uses spaces not tabs consistently everywhere"
        engine, heart, s = await _fresh_engine(
            db, mock_embeddings, settings,
            identity_prompt=f"### Preferences\n- {content}",
            settings_update={"profile_core_enabled": True, "profile_core_probation_days": 0},
        )
        try:
            async with db.session() as session:
                r = await heart.learn(
                    FactInput(content=content, category="preference", subject="core-dedup"),
                    session=session,
                )
                await session.commit()
            await _tag_fact(db, r.id)
            result = await engine.build(
                agent_id=s.agent_id, session_id="s-core-dedup",
                input_text="hello", frame=_frame(),
            )
            profile = _profile_section(result)
            assert profile is not None
            assert content in profile.content
        finally:
            await heart.close()

    @pytest.mark.asyncio
    async def test_empty_core_falls_back_to_legacy(self, db, mock_embeddings, settings):
        """core on but zero tagged + zero probation → legacy top-N (no cliff)."""
        engine, heart, s = await _fresh_engine(
            db, mock_embeddings, settings, identity_prompt="",
            settings_update={"profile_core_enabled": True, "profile_core_probation_days": 0},
        )
        try:
            async with db.session() as session:
                await heart.learn(
                    FactInput(content="Tim prefers metric units for distance always", category="preference", subject="core-fallback"),
                    session=session,
                )
                await session.commit()
            result = await engine.build(
                agent_id=s.agent_id, session_id="s-core-fallback",
                input_text="hello", frame=_frame(),
            )
            profile = _profile_section(result)
            assert profile is not None
            assert "metric units for distance" in profile.content
        finally:
            await heart.close()

    @pytest.mark.asyncio
    async def test_flag_off_byte_identical(self, db, mock_embeddings, settings):
        """flag off (default): a tagged fact is treated exactly like any other —
        legacy top-N renders both tagged and untagged facts (tag inert)."""
        engine, heart, s = await _fresh_engine(db, mock_embeddings, settings, identity_prompt="")
        try:
            async with db.session() as session:
                r_tag = await heart.learn(
                    FactInput(content="Tim prefers Fahrenheit surprisingly for baking only", category="preference", subject="off-tag"),
                    session=session,
                )
                await heart.learn(
                    FactInput(content="Tim keeps a workshop in the garage for projects", category="person", subject="off-untag"),
                    session=session,
                )
                await session.commit()
            await _tag_fact(db, r_tag.id)
            result = await engine.build(
                agent_id=s.agent_id, session_id="s-off",
                input_text="hello", frame=_frame(),
            )
            profile = _profile_section(result)
            assert profile is not None
            assert "Fahrenheit surprisingly for baking" in profile.content
            assert "workshop in the garage" in profile.content
        finally:
            await heart.close()

    @pytest.mark.asyncio
    async def test_cap_respected_tagged_first(self, db, mock_embeddings, settings):
        """core_limit caps the combined set tagged-first: with limit 1, the tagged
        fact wins over a fresh probation candidate."""
        from datetime import datetime, timezone
        engine, heart, s = await _fresh_engine(
            db, mock_embeddings, settings, identity_prompt="",
            settings_update={
                "profile_core_enabled": True,
                "profile_core_limit": 1,
                "profile_core_probation_days": 14,
            },
        )
        try:
            async with db.session() as session:
                r_tag = await heart.learn(
                    FactInput(content="Tim prefers Celsius for all temperature readings", category="preference", subject="cap-tag"),
                    session=session,
                )
                r_prob = await heart.learn(
                    FactInput(content="Tim recently started learning to play the cello", category="person", subject="cap-prob"),
                    session=session,
                )
                await session.commit()
            await _tag_fact(db, r_tag.id)
            await _set_learned_at(db, r_prob.id, datetime.now(timezone.utc))
            result = await engine.build(
                agent_id=s.agent_id, session_id="s-cap",
                input_text="hello", frame=_frame(),
            )
            profile = _profile_section(result)
            assert profile is not None
            assert "Celsius" in profile.content
            assert "play the cello" not in profile.content
        finally:
            await heart.close()


def _session_profile_section(result):
    return next((s for s in result.sections if s.label == "Session Profile"), None)


class TestSessionProfileLeg:
    # codex r2 (#574): fresh-engine fixtures use Postgres-only SQL (::jsonb,
    # generated tsvector) — skip cleanly under the default sqlite backend.
    pytestmark = pytest.mark.postgres_only

    """Task 2: intent-selected tier-1 domain facts, rendered as a dynamic
    section. postgres_only — the leg exercises hybrid FTS+vector search; the
    unique-token FTS leg drives deterministic membership (rank is probabilistic
    under the orthogonal mock-vector leg, so assertions are membership-only)."""

    @pytest.mark.postgres_only
    @pytest.mark.asyncio
    async def test_leg_surfaces_domain_fact_and_excludes_core(self, db, mock_embeddings, settings):
        """A non-core domain fact matching the turn appears in Session Profile;
        a core-tagged fact (already in User Profile) never repeats there."""
        engine, heart, s = await _fresh_engine(
            db, mock_embeddings, settings, identity_prompt="",
            settings_update={
                "profile_core_enabled": True,
                "profile_core_probation_days": 0,
                "profile_intent_leg_enabled": True,
            },
        )
        try:
            async with db.session() as session:
                r_core = await heart.learn(
                    FactInput(content="Tim prefers Celsius uniqtokencore for all temperatures", category="preference", subject="leg-core"),
                    session=session,
                )
                await heart.learn(
                    FactInput(content="Tim's trading rule zqxjflktoken sell underwater positions promptly", category="rule", subject="leg-domain"),
                    session=session,
                )
                await session.commit()
            await _tag_fact(db, r_core.id)
            result = await engine.build(
                agent_id=s.agent_id, session_id="s-leg-pos",
                input_text="what about zqxjflktoken", frame=_frame(),
            )
            sp = _session_profile_section(result)
            assert sp is not None
            assert "zqxjflktoken" in sp.content  # domain fact surfaced
            assert "uniqtokencore" not in sp.content  # core fact not double-injected
            # core fact still lives in User Profile
            profile = _profile_section(result)
            assert profile is not None and "uniqtokencore" in profile.content
        finally:
            await heart.close()

    @pytest.mark.postgres_only
    @pytest.mark.asyncio
    async def test_leg_absent_domain_fact_for_unrelated_input(self, db, mock_embeddings, settings):
        """Unrelated input: the domain fact is not FTS-matched and is outranked
        out of the fetch by FTS-matching decoys, so it does not surface.

        A runtime vector_weight=0.0 override isolates the deterministic FTS leg
        (mock vectors are orthogonal-but-arbitrary, so at the default 0.7 vector
        weight the domain fact's hash-seeded vector rank is unpredictable — the
        plan's stated approach is to let the keyword leg drive the match). The
        weight is resolved module-globally via RuntimeConfig, so a settings
        override would be inert; set the runtime override and clear it after."""
        from nous.runtime_config import RuntimeConfig
        engine, heart, s = await _fresh_engine(
            db, mock_embeddings, settings, identity_prompt="",
            settings_update={
                "profile_core_enabled": True,
                "profile_core_probation_days": 0,
                "profile_intent_leg_enabled": True,
            },
        )
        RuntimeConfig.get().set_vector_weight(0.0)
        try:
            async with db.session() as session:
                r_core = await heart.learn(
                    FactInput(content="Tim prefers Celsius uniqtokencore for all temperatures", category="preference", subject="negleg-core"),
                    session=session,
                )
                await heart.learn(
                    FactInput(content="Tim's trading rule zqxjflktoken sell underwater positions promptly", category="rule", subject="negleg-domain"),
                    session=session,
                )
                for i in range(6):
                    await heart.learn(
                        FactInput(content=f"Sailing note wntokendecoy marina slip number {i} details", category="person", subject=f"negleg-decoy-{i}"),
                        session=session,
                    )
                await session.commit()
            # Tag the core fact so the User Profile is a small curated set — the
            # decoys/domain stay OUT of profile_fact_ids and remain leg-eligible.
            await _tag_fact(db, r_core.id)
            # Query = the decoys' shared token + stopwords only. plainto_tsquery
            # ANDs its lexemes, so a non-stopword extra word (e.g. "tell") would
            # require that word in the fact too and match nothing; "what about"
            # are stopwords, leaving just "wntokendecoy".
            result = await engine.build(
                agent_id=s.agent_id, session_id="s-leg-neg",
                input_text="what about wntokendecoy", frame=_frame(),
            )
            sp = _session_profile_section(result)
            assert sp is not None  # decoys populate the section
            assert "zqxjflktoken" not in sp.content  # domain fact NOT surfaced
        finally:
            RuntimeConfig.get().clear_vector_weight()
            await heart.close()

    @pytest.mark.postgres_only
    @pytest.mark.asyncio
    async def test_leg_absent_when_flag_off(self, db, mock_embeddings, settings):
        """Flag off (default): no Session Profile section at all."""
        engine, heart, s = await _fresh_engine(
            db, mock_embeddings, settings, identity_prompt="",
        )
        try:
            async with db.session() as session:
                await heart.learn(
                    FactInput(content="Tim's trading rule zqxjflktoken sell underwater positions promptly", category="rule", subject="offleg-domain"),
                    session=session,
                )
                await session.commit()
            result = await engine.build(
                agent_id=s.agent_id, session_id="s-leg-off",
                input_text="what about zqxjflktoken", frame=_frame(),
            )
            assert _session_profile_section(result) is None
            assert "## Session Profile" not in result.system_prompt
        finally:
            await heart.close()

    @pytest.mark.asyncio
    async def test_leg_requires_keyword_anchor(self, db, mock_embeddings, settings):
        """codex r3: the RRF missing-leg penalty rank (limit+1) is nearly free
        (a vector-only rank-0 hit normalizes to ~0.97), so a score floor CANNOT
        exclude nearest-neighbor noise — the leg requires a keyword-leg
        (lexical) anchor instead. Core mode is enabled with a DIFFERENT tagged
        fact so the domain fact is NOT masked by the profile_fact_ids
        double-injection guard (the earlier test's silent pass cause)."""
        engine, heart, s = await _fresh_engine(
            db, mock_embeddings, settings, identity_prompt="",
            settings_update={"profile_intent_leg_enabled": True, "profile_core_enabled": True,
                             "profile_core_probation_days": 0},
        )
        try:
            async with db.session() as session:
                core_fact = await heart.learn(
                    FactInput(content="Tim prefers Celsius for all temperature readings", category="preference", subject="anchor-core"),
                    session=session,
                )
                await heart.learn(
                    FactInput(content="Tim's trading rule qanchortok sell underwater positions promptly", category="rule", subject="anchor-domain"),
                    session=session,
                )
                await session.commit()
            await heart.set_fact_tag(core_fact.id, "profile_core", True)

            # Unrelated input: vector leg still ranks the domain fact (~0.97
            # merged score!) but the FTS leg has no match -> keyword-anchor
            # gate drops it -> no Session Profile section.
            result = await engine.build(
                agent_id=s.agent_id, session_id="s-leg-anchor-neg",
                input_text="completely unrelated musings about gardens", frame=_frame(),
            )
            assert _session_profile_section(result) is None

            # Domain input: FTS matches the unique token -> leg renders it.
            result2 = await engine.build(
                agent_id=s.agent_id, session_id="s-leg-anchor-pos",
                input_text="what about qanchortok", frame=_frame(),
            )
            leg = _session_profile_section(result2)
            assert leg is not None
            assert "qanchortok" in leg.content
        finally:
            await heart.close()

    @pytest.mark.asyncio
    async def test_leg_respects_fact_skip_and_zero_budget(self, db, mock_embeddings, settings):
        """codex r1: the leg honors the retrieval plan's skip_types={'fact'}
        (greeting/no-fact turns) and a budget=0 operator disable."""
        from nous.cognitive.intent import RetrievalPlan

        engine, heart, s = await _fresh_engine(
            db, mock_embeddings, settings, identity_prompt="",
            settings_update={"profile_intent_leg_enabled": True},
        )
        try:
            async with db.session() as session:
                await heart.learn(
                    FactInput(content="Tim's trading rule vbnqrskiptok sell underwater positions promptly", category="rule", subject="skip-domain"),
                    session=session,
                )
                await session.commit()
            plan = RetrievalPlan(skip_types={"fact"})
            result = await engine.build(
                agent_id=s.agent_id, session_id="s-leg-skip",
                input_text="what about vbnqrskiptok", frame=_frame(),
                retrieval_plan=plan,
            )
            assert _session_profile_section(result) is None
        finally:
            await heart.close()

        engine2, heart2, s2 = await _fresh_engine(
            db, mock_embeddings, settings, identity_prompt="",
            settings_update={"profile_intent_leg_enabled": True, "profile_intent_leg_budget": 0},
        )
        try:
            async with db.session() as session:
                await heart2.learn(
                    FactInput(content="Tim's trading rule wmzeroBudgtok sell underwater positions promptly", category="rule", subject="zb-domain"),
                    session=session,
                )
                await session.commit()
            result = await engine2.build(
                agent_id=s2.agent_id, session_id="s-leg-zb",
                input_text="what about wmzeroBudgtok", frame=_frame(),
            )
            assert _session_profile_section(result) is None
        finally:
            await heart2.close()


class TestCodexRound4:
    # codex r4 (#574): keyword-channel-off + no-embedder configs + strict censors.
    pytestmark = pytest.mark.postgres_only

    @pytest.mark.asyncio
    async def test_leg_works_with_keyword_channel_disabled(self, db, mock_embeddings, settings):
        """require_keyword_hit forces the FTS leg even when the keyword channel
        toggle is off — else the filter would drop every result."""
        engine, heart, s = await _fresh_engine(
            db, mock_embeddings, settings, identity_prompt="",
            settings_update={"profile_intent_leg_enabled": True,
                             "hybrid_search_keyword_enabled": False,
                             "profile_core_enabled": True,
                             "profile_core_probation_days": 0},
        )
        try:
            async with db.session() as session:
                decoy = await heart.learn(
                    FactInput(content="Tim prefers Celsius for all temperature readings", category="preference", subject="kwoff-core"),
                    session=session,
                )
                await heart.learn(
                    FactInput(content="Tim's trading rule qkwofftok sell underwater positions promptly", category="rule", subject="kwoff-domain"),
                    session=session,
                )
                await session.commit()
            # core mode + tagged decoy => the domain fact is NOT in the User
            # Profile render, so profile_fact_ids does not mask the leg.
            await heart.set_fact_tag(decoy.id, "profile_core", True)
            result = await engine.build(
                agent_id=s.agent_id, session_id="s-leg-kwoff",
                input_text="what about qkwofftok", frame=_frame(),
            )
            leg = _session_profile_section(result)
            assert leg is not None and "qkwofftok" in leg.content
        finally:
            await heart.close()

    @pytest.mark.asyncio
    async def test_leg_floor_skipped_without_embeddings(self, db, settings):
        """Keyword-only deployments score on the ts_rank scale (<<0.7) — the
        RRF floor must not filter their legitimate FTS hits."""
        from sqlalchemy import text as sqltext
        agent_id = f"test-upf-{_uuid.uuid4().hex[:8]}"
        async with db.session() as session:
            await session.execute(
                sqltext("INSERT INTO nous_system.agents (id, name, config) VALUES (:id, :n, '{}'::jsonb) ON CONFLICT (id) DO NOTHING"),
                {"id": agent_id, "n": "UPF NoEmbed Agent"},
            )
            await session.commit()
        s = settings.model_copy(update={"agent_id": agent_id, "profile_intent_leg_enabled": True,
                                        "profile_core_enabled": True, "profile_core_probation_days": 0})
        heart = Heart(db, s, embedding_provider=None)
        brain = Brain(database=db, settings=s)
        engine = ContextEngine(brain, heart, s, identity_prompt="")
        try:
            async with db.session() as session:
                decoy = await heart.learn(
                    FactInput(content="Tim prefers Celsius for all temperature readings", category="preference", subject="noemb-core"),
                    session=session,
                )
                await heart.learn(
                    FactInput(content="Tim's trading rule qnoembtok sell underwater positions promptly", category="rule", subject="noemb-domain"),
                    session=session,
                )
                await session.commit()
            # core mode + tagged decoy => domain fact not masked by
            # profile_fact_ids (it never renders in the User Profile section).
            await heart.set_fact_tag(decoy.id, "profile_core", True)
            result = await engine.build(
                agent_id=s.agent_id, session_id="s-leg-noemb",
                input_text="what about qnoembtok", frame=_frame(),
            )
            leg = _session_profile_section(result)
            assert leg is not None and "qnoembtok" in leg.content
        finally:
            await heart.close()
            await brain.close()


class TestCensorLineAwareTruncation:
    # codex r2 (#574): fresh-engine fixtures use Postgres-only SQL (::jsonb,
    # generated tsvector) — skip cleanly under the default sqlite backend.
    pytestmark = pytest.mark.postgres_only

    @pytest.mark.asyncio
    async def test_censor_section_drops_whole_lines(self, db, mock_embeddings, settings):
        """The Active Censors section uses line-aware truncation: an over-budget
        censor list drops whole rules, never char-slicing a rule mid-sentence."""
        from nous.heart import CensorInput
        engine, heart, s = await _fresh_engine(
            db, mock_embeddings, settings, identity_prompt="",
            settings_update={"context_budget_overrides": {"censors": 60}},
        )
        try:
            async with db.session() as session:
                for i in range(4):
                    await heart.add_censor(
                        CensorInput(
                            trigger_pattern=f"pattern number {i} triggering phrase here",
                            reason=f"reason number {i} explaining the enforcement rule in detail",
                            action="steer",
                        ),
                        session=session,
                    )
                await session.commit()
            result = await engine.build(
                agent_id=s.agent_id, session_id="s-censor",
                input_text="hello", frame=_frame(),
            )
            censor_sec = next((sec for sec in result.sections if sec.label == "Active Censors"), None)
            assert censor_sec is not None
            full = engine._format_censors(await heart.list_censors())
            full_lines = set(full.split("\n"))
            rendered_lines = [ln for ln in censor_sec.content.split("\n") if ln]
            assert rendered_lines, "expected at least one rendered censor line"
            for ln in rendered_lines:
                assert ln in full_lines  # every rendered line is a whole formatted rule
            assert not censor_sec.content.endswith("...")  # no char-slice signature
            assert len(censor_sec.content) < len(full)  # actually truncated
        finally:
            await heart.close()



def test_truncate_lines_strict_returns_empty_for_oversized_first_line():
    """codex r4: strict mode never char-slices — a censor whose first rule
    exceeds the whole budget renders NOTHING rather than a partial rule."""
    from types import SimpleNamespace
    from nous.cognitive.context import ContextEngine

    engine = ContextEngine.__new__(ContextEngine)
    engine._settings = SimpleNamespace()
    text = "x" * 10_000
    assert engine._truncate_to_budget_lines(text, 25, strict=True) == ""
    # default mode still falls back to the char slice
    assert engine._truncate_to_budget_lines(text, 25).endswith("...")
