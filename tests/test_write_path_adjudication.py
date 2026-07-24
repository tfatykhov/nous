"""Write-path adjudication (R1 enumerative extraction + R2 store-time supersession)."""
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy import select

from nous.heart.schemas import FactInput
from nous.storage.models import Fact, GraphEdge


def test_fact_input_accepts_adjudication_fields():
    fi = FactInput(
        content="The red car belongs to Alice.",
        subject_key="red car",
        attribute_key="owner",
        source_ordinal=12,
        overrides_prior=True,
    )
    assert fi.subject_key == "red car"
    assert fi.attribute_key == "owner"
    assert fi.source_ordinal == 12
    assert fi.overrides_prior is True


def test_fact_input_adjudication_fields_default_none():
    fi = FactInput(content="x" * 40)
    assert fi.subject_key is None
    assert fi.attribute_key is None
    assert fi.source_ordinal is None
    assert fi.overrides_prior is False


@pytest.mark.postgres_only
async def test_learn_uses_precomputed_embedding(heart, session):
    """When precomputed_embedding is passed, the embedder must NOT be called."""
    vec = [0.1] * 1536
    heart.facts.embeddings.embed = AsyncMock(side_effect=AssertionError("must not embed"))
    detail = await heart.learn(
        FactInput(content="Precomputed embedding threading test fact content here."),
        session=session,
        precomputed_embedding=vec,
    )
    assert detail.id is not None


@pytest.mark.postgres_only
async def test_adjudication_fields_persist_round_trip(heart, session):
    """subject_key/attribute_key/source_ordinal/overrides_prior persist to DB."""
    # Fact WITH all four adjudication fields
    result = await heart.learn(
        FactInput(
            content="Round trip test: subject key attribute key source ordinal check.",
            subject_key="round trip",
            attribute_key="check",
            source_ordinal=7,
            overrides_prior=True,
        ),
        session=session,
    )
    assert result.id is not None

    row = await session.get(Fact, result.id)
    assert row is not None
    assert row.subject_key == "round trip"
    assert row.attribute_key == "check"
    assert row.source_ordinal == 7
    assert row.overrides_prior is True

    # Fact WITHOUT the fields should persist NULLs (overrides_prior False → stored as None)
    result2 = await heart.learn(
        FactInput(content="Round trip test: baseline fact without adjudication fields set."),
        session=session,
    )
    assert result2.id is not None

    row2 = await session.get(Fact, result2.id)
    assert row2 is not None
    assert row2.subject_key is None
    assert row2.attribute_key is None
    assert row2.source_ordinal is None
    assert row2.overrides_prior is None


# R1.1: Enumerative extractor (density heuristic + key normalizer)
from nous.handlers.enumerative_extractor import normalize_key, density_score, is_enumerable


def test_normalize_key_canonicalizes():
    # codex P2 round 12: possessive 's collapses to the base entity.
    assert normalize_key("Tim's Laptop") == "tim laptop"
    assert normalize_key("  RED   Car!! ") == "red car"
    assert normalize_key("") is None
    assert normalize_key("   ") is None
    assert len(normalize_key("x" * 500)) <= 200


def test_normalize_key_max_len_param():
    """FIX 2 (codex r5): max_len kwarg caps at column width for attribute_key (VARCHAR(100))."""
    # Default still caps at 200
    assert len(normalize_key("a" * 300)) == 200
    # max_len=100 caps at 100 — a 150-char normalized key is truncated to 100
    long_key = "word " * 30  # 150 chars
    result = normalize_key(long_key, max_len=100)
    assert result is not None
    assert len(result) <= 100
    # Default behavior for subject_key (VARCHAR(200)) unchanged
    assert len(normalize_key("a" * 250)) == 200


def test_density_score_high_for_enumerable():
    doc = "\n".join(f"Statement {i}: item {i} belongs to person {i}." for i in range(40))
    assert density_score(doc) > 0.8


def test_density_score_low_for_narrative():
    doc = (
        "User: hey, how was your weekend?\n"
        "Assistant: It went well! I spent most of it reading about distributed "
        "systems and thinking about how consensus algorithms deal with partial "
        "failure, which reminded me of a conversation we had a while back about "
        "why exactly-once delivery is impossible in asynchronous networks.\n"
    ) * 10
    assert density_score(doc) < 0.5


def test_is_enumerable_respects_threshold():
    doc = "\n".join(f"{i}. fact number {i} is stored here." for i in range(30))
    assert is_enumerable(doc, threshold=0.6) is True
    assert is_enumerable(doc, threshold=1.01) is False


def test_density_score_detects_unnumbered_short_fact_sheet():
    doc = "\n".join(["Alice is 30.", "City: Paris.", "Bob is CEO.", "Tim likes dogs.", "Bob owns the car.", "Sky is blue."])
    assert density_score(doc) > 0.8


# ---------------------------------------------------------------------------
# R1.2/R1.3: EnumerativeExtractor — chunked LLM extraction + batched store
# ---------------------------------------------------------------------------
from nous.handlers.enumerative_extractor import EnumerativeExtractor

_CHUNK_FACTS = {
    "facts": [
        {
            "content": "The red car belongs to Alice.",
            "subject": "red car",
            "subject_key": "Red Car",
            "attribute_key": "Owner",
            "category": "concept",
            "confidence": 0.9,
            "overrides_prior": False,
        },
        {
            "content": "The blue car belongs to Bob.",
            "subject": "blue car",
            "subject_key": "blue car",
            "attribute_key": "owner",
            "category": "concept",
            "confidence": 0.9,
            "overrides_prior": True,
        },
    ]
}


@pytest.fixture
def settings_fixture():
    """Factory fixture: returns a SimpleNamespace settings object with overrides."""

    def _make(**kwargs):
        defaults = {
            "episode_chunk_size": 600,
            "episode_chunk_overlap": 80,
            # 0 disables the min_chars guard — lets short test transcripts through.
            "episode_chunk_min_transcript_chars": 0,
            "background_model": "claude-haiku-4-5-20251001",
            "enumerative_density_threshold": 0.6,
            "enumerative_max_facts_per_episode": 1000,
            "enumerative_max_chunks_per_episode": 200,
            "enumerative_extraction_max_per_hour": 1000,
        }
        defaults.update(kwargs)
        return types.SimpleNamespace(**defaults)

    return _make


@pytest.mark.asyncio
async def test_enumerative_extraction_stores_atomic_facts(monkeypatch, settings_fixture):
    settings = settings_fixture(
        extraction_enumerative_enabled=True,
        enumerative_density_threshold=0.0,  # force-enumerable for the test
        enumerative_max_facts_per_episode=1000,
    )
    heart = AsyncMock()
    heart.learn = AsyncMock(return_value=SimpleNamespace(id=uuid4()))
    embedder = AsyncMock()
    embedder.embed_batch = AsyncMock(side_effect=lambda texts: [[0.0] * 1536] * len(texts))
    monkeypatch.setattr(
        "nous.handlers.enumerative_extractor.call_background_llm_structured",
        AsyncMock(return_value=_CHUNK_FACTS),
    )
    ex = EnumerativeExtractor(heart=heart, settings=settings, llm_client=object(), embedder=embedder)
    stored_ids = await ex.process_transcript("1. a.\n2. b.\n3. c.\n4. d.\n5. e.\n", episode_id=uuid4())
    assert len(stored_ids) == 2
    calls = heart.learn.call_args_list
    fi0 = calls[0].args[0]
    assert fi0.subject_key == "red car"  # normalized from "Red Car"
    assert fi0.attribute_key == "owner"  # normalized from "Owner"
    # devil-2 #2: ordinals are POSITIONAL ONLY (chunk_index * 1_000_000 + pos) —
    # explicit statement numbers in the source are never used as ordinals.
    assert fi0.source_ordinal == 0 * 1_000_000 + 0
    assert fi0.source == "enumerative_extractor"
    assert fi0.source_text == fi0.content  # per-statement grounding (RC-1a)
    assert calls[0].kwargs["precomputed_embedding"] == [0.0] * 1536
    fi1 = calls[1].args[0]
    assert fi1.source_ordinal == 0 * 1_000_000 + 1  # chunk 0, position 1
    assert fi1.overrides_prior is True


@pytest.mark.asyncio
async def test_enumerative_cap_truncates_loudly(monkeypatch, settings_fixture, caplog):
    settings = settings_fixture(
        extraction_enumerative_enabled=True,
        enumerative_density_threshold=0.0,
        enumerative_max_facts_per_episode=1,
    )
    heart = AsyncMock()
    heart.learn = AsyncMock(return_value=SimpleNamespace(id=uuid4()))
    embedder = AsyncMock()
    embedder.embed_batch = AsyncMock(side_effect=lambda texts: [[0.0] * 1536] * len(texts))
    monkeypatch.setattr(
        "nous.handlers.enumerative_extractor.call_background_llm_structured",
        AsyncMock(return_value=_CHUNK_FACTS),
    )
    ex = EnumerativeExtractor(heart=heart, settings=settings, llm_client=object(), embedder=embedder)
    with caplog.at_level("WARNING"):
        stored_ids = await ex.process_transcript("1. a.\n2. b.\n3. c.\n4. d.\n5. e.\n", episode_id=uuid4())
    assert len(stored_ids) == 1
    assert any("enumerative cap" in r.message.lower() for r in caplog.records)


# ---------------------------------------------------------------------------
# Task 5: R1.5 wiring — extract_and_store modal routing + admission bypass
#          + source-aware min-chars
# ---------------------------------------------------------------------------


def _stub_heart_for_extractor(stored_ids=None):
    """Return a minimal Heart stub that records learn() calls."""
    from types import SimpleNamespace

    captured = []

    class _StubResult:
        def __init__(self):
            from uuid import uuid4
            self.id = uuid4()

    class _StubHeart:
        async def search_facts(self, *a, **kw):
            return []

        async def learn(self, fact_input, **kw):
            captured.append(fact_input)
            return _StubResult()

    heart = _StubHeart()
    heart._captured = captured
    return heart


def _make_settings(*, flag_on: bool = False, **kw):
    import types
    defaults = {
        "extraction_enumerative_enabled": flag_on,
        "enumerative_classifier": "heuristic",
        "enumerative_density_threshold": 0.6,
        "enumerative_max_facts_per_episode": 1000,
        "enumerative_max_chunks_per_episode": 200,
        "enumerative_extraction_max_per_hour": 1000,
        "enumerative_min_content_chars": 15,
        "fact_min_content_chars": 30,
        "fact_dedup_threshold": 0.92,
        "episode_chunk_size": 600,
        "episode_chunk_overlap": 80,
        "episode_chunk_min_transcript_chars": 0,
        "background_model": "claude-haiku-4-5-20251001",
    }
    defaults.update(kw)
    return types.SimpleNamespace(**defaults)


# Dense enumerable transcript used by routing tests.
_DENSE_TRANSCRIPT = "\n".join(
    f"{i}. Fact number {i}: item {i} belongs to person {i}." for i in range(40)
)
_VALID_SUMMARY = {"candidate_facts": [{"content": "Tim likes coffee " + "x" * 20, "subject": "Tim", "category": "preference", "confidence": 0.9}]}


@pytest.mark.asyncio
async def test_flag_off_extract_and_store_never_touches_enumerative(monkeypatch):
    """GOLDEN: flag off => EnumerativeExtractor.process_transcript is never called."""
    sentinel = AsyncMock(side_effect=AssertionError("enumerative ran with flag off"))
    monkeypatch.setattr(
        "nous.handlers.enumerative_extractor.EnumerativeExtractor.process_transcript",
        sentinel,
    )

    from nous.handlers import fact_extractor as fe_mod

    ext = fe_mod.FactExtractor.__new__(fe_mod.FactExtractor)
    ext._heart = _stub_heart_for_extractor()
    ext._settings = _make_settings(flag_on=False)
    ext._dedup_via_search = False
    ext._llm = object()  # non-None so the flag is the ONLY gate

    # Dense transcript that WOULD trigger enumerative if flag were on.
    result = await ext.extract_and_store(
        summary=_VALID_SUMMARY,
        episode_id=str(uuid4()),
        transcript=_DENSE_TRANSCRIPT,
    )
    # Sentinel must not have been called.
    sentinel.assert_not_called()
    # Legacy candidate-facts path ran (returned the one stub UUID).
    assert len(result) == 1


@pytest.mark.asyncio
async def test_flag_on_enumerable_routes_modally(monkeypatch):
    """Flag on + ENUMERABLE transcript: process_transcript is invoked and the
    candidate-facts path is SKIPPED (modal routing).  extract_and_store returns
    the enumerative UUIDs."""
    from uuid import uuid4 as _uuid4

    fake_ids = [_uuid4(), _uuid4()]
    mock_process = AsyncMock(return_value=fake_ids)
    monkeypatch.setattr(
        "nous.handlers.enumerative_extractor.EnumerativeExtractor.process_transcript",
        mock_process,
    )

    from nous.handlers import fact_extractor as fe_mod

    heart = _stub_heart_for_extractor()
    ext = fe_mod.FactExtractor.__new__(fe_mod.FactExtractor)
    ext._heart = heart
    ext._settings = _make_settings(flag_on=True, enumerative_density_threshold=0.0)  # force-enumerable
    ext._dedup_via_search = False
    ext._llm = object()  # non-None required for modal branch

    result = await ext.extract_and_store(
        summary=_VALID_SUMMARY,
        episode_id=str(_uuid4()),
        transcript=_DENSE_TRANSCRIPT,
    )
    # Enumerative leg ran.
    mock_process.assert_called_once()
    # Candidate path did NOT run (heart.learn never called).
    assert heart._captured == []
    # Returns the enumerative UUIDs.
    assert result == fake_ids


@pytest.mark.asyncio
async def test_flag_on_narrative_keeps_legacy_path(monkeypatch):
    """Flag on + NARRATIVE transcript (density below threshold): candidate path
    runs exactly as today; process_transcript is NOT called."""
    sentinel = AsyncMock(side_effect=AssertionError("enumerative ran on narrative"))
    monkeypatch.setattr(
        "nous.handlers.enumerative_extractor.EnumerativeExtractor.process_transcript",
        sentinel,
    )

    from nous.handlers import fact_extractor as fe_mod

    heart = _stub_heart_for_extractor()
    ext = fe_mod.FactExtractor.__new__(fe_mod.FactExtractor)
    ext._heart = heart
    ext._settings = _make_settings(flag_on=True, enumerative_density_threshold=0.99)
    ext._dedup_via_search = False
    ext._llm = object()

    narrative = (
        "User: hey, how was your weekend?\n"
        "Assistant: It went well! I spent time reading about distributed systems.\n"
    ) * 20

    result = await ext.extract_and_store(
        summary=_VALID_SUMMARY,
        episode_id=str(uuid4()),
        transcript=narrative,
    )
    sentinel.assert_not_called()
    # Legacy candidate-facts path ran.
    assert len(result) == 1


@pytest.mark.asyncio
async def test_enumerative_leg_exception_falls_through_to_legacy(monkeypatch):
    """Flag on + enumerable transcript, but process_transcript raises RuntimeError.
    extract_and_store must NOT propagate the exception and must fall through to the
    legacy candidate-facts path so episode facts are never silently dropped."""
    monkeypatch.setattr(
        "nous.handlers.enumerative_extractor.EnumerativeExtractor.process_transcript",
        AsyncMock(side_effect=RuntimeError("simulated enumerative failure")),
    )

    from nous.handlers import fact_extractor as fe_mod

    heart = _stub_heart_for_extractor()
    ext = fe_mod.FactExtractor.__new__(fe_mod.FactExtractor)
    ext._heart = heart
    ext._settings = _make_settings(flag_on=True, enumerative_density_threshold=0.0)  # force-enumerable
    ext._dedup_via_search = False
    ext._llm = object()  # non-None so the enumerative branch is entered

    # Should not raise; exception is caught inside extract_and_store and the
    # legacy candidate-facts path is taken instead.
    result = await ext.extract_and_store(
        summary=_VALID_SUMMARY,
        episode_id=str(uuid4()),
        transcript=_DENSE_TRANSCRIPT,
    )

    # Legacy path stored the one candidate fact from _VALID_SUMMARY.
    assert len(heart._captured) == 1, "legacy candidate path must have stored the fact"
    assert len(result) == 1, "extract_and_store must return the legacy UUID"


@pytest.mark.asyncio
async def test_enumerative_leg_zero_stored_falls_through_to_legacy(monkeypatch):
    """Codex r3 FIX 3: process_transcript returns [] (no exception) — e.g. API
    failure swallowed inside call_background_llm_structured.  extract_and_store
    must fall through to the legacy candidate-facts path so episode facts are
    never silently dropped.  Mirrors the exception-fallthrough test structure."""
    monkeypatch.setattr(
        "nous.handlers.enumerative_extractor.EnumerativeExtractor.process_transcript",
        AsyncMock(return_value=[]),  # zero stored, no exception
    )

    from nous.handlers import fact_extractor as fe_mod

    heart = _stub_heart_for_extractor()
    ext = fe_mod.FactExtractor.__new__(fe_mod.FactExtractor)
    ext._heart = heart
    ext._settings = _make_settings(flag_on=True, enumerative_density_threshold=0.0)  # force-enumerable
    ext._dedup_via_search = False
    ext._llm = object()  # non-None so the enumerative branch is entered

    result = await ext.extract_and_store(
        summary=_VALID_SUMMARY,
        episode_id=str(uuid4()),
        transcript=_DENSE_TRANSCRIPT,
    )

    # Legacy candidate path must have stored the one fact from _VALID_SUMMARY.
    assert len(heart._captured) == 1, (
        "legacy candidate path must run when enumerative leg stores 0 facts"
    )
    assert len(result) == 1, (
        "extract_and_store must return the legacy UUID, not an empty list"
    )


@pytest.mark.asyncio
async def test_enumerative_budget_persists_across_episodes(monkeypatch):
    """Codex r6: EnumerativeExtractor is a singleton on FactExtractor — hourly cap is
    per-hour, not per-episode.  With budget=1, the second extract_and_store call
    hits the shared spent counter and falls through to the legacy candidate path."""
    from nous.handlers import fact_extractor as fe_mod

    settings = _make_settings(
        flag_on=True,
        enumerative_density_threshold=0.0,  # force-enumerable
        enumerative_extraction_max_per_hour=1,  # budget: 1 LLM call total
    )
    heart = _stub_heart_for_extractor()

    mock_llm = AsyncMock(return_value=_CHUNK_FACTS)
    monkeypatch.setattr(
        "nous.handlers.enumerative_extractor.call_background_llm_structured",
        mock_llm,
    )

    # Use real __init__ so _enumerative_extractor is initialized to None.
    fx = fe_mod.FactExtractor(
        heart=heart,
        settings=settings,
        bus=None,
        llm_client=object(),  # non-None satisfies the enumerative branch guard
        dedup_via_search=False,
    )
    assert fx._enumerative_extractor is None  # initialized by __init__

    # First call: budget ok → LLM called once → enumerative facts stored.
    result1 = await fx.extract_and_store(
        summary=_VALID_SUMMARY,
        episode_id=str(uuid4()),
        transcript=_DENSE_TRANSCRIPT,
    )
    singleton_ref = fx._enumerative_extractor
    assert singleton_ref is not None, "singleton must be created after first call"
    assert len(result1) >= 1, "first call must store facts via enumerative leg"

    # Second call: same singleton, budget spent → LLM NOT called → falls through to legacy.
    result2 = await fx.extract_and_store(
        summary=_VALID_SUMMARY,
        episode_id=str(uuid4()),
        transcript=_DENSE_TRANSCRIPT,
    )
    # Singleton must be the SAME object — not re-constructed.
    assert fx._enumerative_extractor is singleton_ref, (
        "extract_and_store must reuse the same EnumerativeExtractor instance"
    )
    # LLM called exactly once across both calls (second hit the shared spent budget).
    assert mock_llm.call_count == 1, (
        f"expected 1 LLM call (budget=1), got {mock_llm.call_count}"
    )
    # Second call fell through to legacy candidate path (stored the _VALID_SUMMARY fact).
    assert len(result2) == 1, (
        "second extract_and_store must fall through to legacy path and store the candidate fact"
    )


@pytest.mark.postgres_only
async def test_enumerative_min_chars_floor_applies_to_enumerative_source_only(heart, session):
    """_learn rejects a 20-char fact from source='fact_extractor' (floor 30)
    but accepts a 20-char fact from source='enumerative_extractor' (floor 15).

    Exercises the REAL gate in FactManager._learn, not a local replica.
    Content is 20 chars: above enumerative floor (15) but below standard floor (30).
    """
    from nous.heart.schemas import FactRejected

    # 20 chars — "Bob owns the red car" = B-o-b(3)+ (4)+o-w-n-s(8)+ (9)+t-h-e(12)
    #            + (13)+r-e-d(16)+ (17)+c-a-r(20)
    enum_content = "Bob owns the red car"
    assert len(enum_content) == 20

    # enumerative_extractor floor = 15 → 20 chars is ACCEPTED
    result_enum = await heart.learn(
        FactInput(
            content=enum_content,
            source="enumerative_extractor",
        ),
        session=session,
    )
    assert not isinstance(result_enum, FactRejected), (
        f"enumerative_extractor should accept a 20-char fact (floor=15), got: {result_enum}"
    )
    assert result_enum.id is not None

    # fact_extractor floor = 30 → 20 chars is REJECTED before any DB access
    # (different content avoids any dedup interaction with the row above)
    std_content = "Tim has the blue van"
    assert len(std_content) == 20
    result_std = await heart.learn(
        FactInput(
            content=std_content,
            source="fact_extractor",
        ),
        session=session,
    )
    assert isinstance(result_std, FactRejected), (
        "fact_extractor should reject a 20-char fact (floor=30)"
    )


def test_admission_bypasses_enumerative_source():
    from nous.heart.admission import AdmissionConfig
    assert "enumerative_extractor" in AdmissionConfig().bypass_sources


# ---------------------------------------------------------------------------
# Task 6: AC-4 — shared apply_supersession primitive
# ---------------------------------------------------------------------------


@pytest.mark.postgres_only
async def test_apply_supersession_sets_columns_and_edge(heart, session):
    """apply_supersession: sets loser.superseded_by, loser.active=False, writes supersedes edge, returns True."""
    winner = await heart.learn(
        FactInput(content="Winner fact: the office building has a rooftop garden now."),
        session=session,
    )
    loser = await heart.learn(
        FactInput(content="Loser fact: the office building has no rooftop garden yet."),
        session=session,
    )

    result = await heart.facts.apply_supersession(winner.id, loser.id, session)
    assert result is True

    await session.flush()

    loser_row = await session.get(Fact, loser.id)
    assert loser_row.superseded_by == winner.id
    assert loser_row.active is False

    edge_r = await session.execute(
        select(GraphEdge)
        .where(GraphEdge.source_id == winner.id)
        .where(GraphEdge.target_id == loser.id)
        .where(GraphEdge.relation == "supersedes")
    )
    edge = edge_r.scalars().first()
    assert edge is not None
    assert edge.weight == 1.0
    assert edge.auto_linked is True


@pytest.mark.postgres_only
async def test_apply_supersession_clobber_guard(heart, session):
    """apply_supersession: already-superseded loser => returns False, columns unchanged."""
    # Prior winner must exist in the DB (FK constraint on superseded_by)
    prior_winner = await heart.learn(
        FactInput(content="Prior winner fact: Alice relocated to Utrecht earlier this spring."),
        session=session,
    )
    winner = await heart.learn(
        FactInput(content="New winner fact: Alice now lives in Amsterdam city center."),
        session=session,
    )
    loser = await heart.learn(
        FactInput(content="Already superseded: Alice lived in Rotterdam last year."),
        session=session,
    )

    # Pre-set loser as already superseded by the prior winner
    loser_row = await session.get(Fact, loser.id)
    loser_row.superseded_by = prior_winner.id
    loser_row.active = False
    await session.flush()

    # Also write the prior_winner→loser supersedes edge so we can assert it survives.
    await heart.link_facts(prior_winner.id, loser.id, "supersedes", 1.0, session=session)
    await session.flush()

    result = await heart.facts.apply_supersession(winner.id, loser.id, session)
    assert result is False

    # Column must NOT be overwritten
    await session.refresh(loser_row)
    assert loser_row.superseded_by == prior_winner.id

    # No new edge written for this winner->loser pair
    edge_r = await session.execute(
        select(GraphEdge)
        .where(GraphEdge.source_id == winner.id)
        .where(GraphEdge.target_id == loser.id)
    )
    assert edge_r.scalars().first() is None

    # Prior winner→loser edge must NOT be disturbed by the clobber guard
    prior_edge_r = await session.execute(
        select(GraphEdge)
        .where(GraphEdge.source_id == prior_winner.id)
        .where(GraphEdge.target_id == loser.id)
        .where(GraphEdge.relation == "supersedes")
    )
    assert prior_edge_r.scalars().first() is not None


@pytest.mark.asyncio
async def test_sleep_apply_supersede_delegates():
    """_apply_supersede delegates to heart.facts.apply_supersession and calls commit."""
    from nous.handlers.sleep_handler import SleepHandler

    winner_id = uuid4()
    loser_id = uuid4()

    mock_session = AsyncMock()
    mock_session.commit = AsyncMock()

    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=mock_session)
    ctx.__aexit__ = AsyncMock(return_value=False)

    mock_heart = MagicMock()
    mock_heart.db.session = MagicMock(return_value=ctx)
    mock_heart.facts = MagicMock()
    mock_heart.facts.apply_supersession = AsyncMock(return_value=True)

    handler = SleepHandler.__new__(SleepHandler)
    handler._heart = mock_heart

    result = await handler._apply_supersede(winner_id, loser_id)

    assert result is True
    mock_heart.facts.apply_supersession.assert_awaited_once_with(winner_id, loser_id, mock_session)
    mock_session.commit.assert_awaited_once()


@pytest.mark.postgres_only
async def test_apply_supersession_edge_parity_with_link_facts(heart, session):
    """Edge written by apply_supersession has same columns as one written by heart.link_facts."""
    # Pair 1: via apply_supersession
    w1 = await heart.learn(
        FactInput(content="Edge parity winner: solar panel efficiency increased to 25 percent."),
        session=session,
    )
    l1 = await heart.learn(
        FactInput(content="Edge parity loser: solar panel efficiency was 20 percent before."),
        session=session,
    )
    await heart.facts.apply_supersession(w1.id, l1.id, session)

    # Pair 2: via heart.link_facts (the thin wrapper apply_supersession replaced)
    w2 = await heart.learn(
        FactInput(content="Link facts winner: wind turbine output is now 5 megawatts rated power."),
        session=session,
    )
    l2 = await heart.learn(
        FactInput(content="Link facts loser: wind turbine output was 3 megawatts old rating."),
        session=session,
    )
    await heart.link_facts(w2.id, l2.id, "supersedes", 1.0, session=session)

    await session.flush()

    edge1_r = await session.execute(
        select(GraphEdge)
        .where(GraphEdge.source_id == w1.id)
        .where(GraphEdge.target_id == l1.id)
        .where(GraphEdge.relation == "supersedes")
    )
    edge1 = edge1_r.scalars().first()
    assert edge1 is not None

    edge2_r = await session.execute(
        select(GraphEdge)
        .where(GraphEdge.source_id == w2.id)
        .where(GraphEdge.target_id == l2.id)
        .where(GraphEdge.relation == "supersedes")
    )
    edge2 = edge2_r.scalars().first()
    assert edge2 is not None

    assert edge1.relation == edge2.relation == "supersedes"
    assert edge1.weight == edge2.weight == 1.0
    assert edge1.auto_linked == edge2.auto_linked is True
    assert edge1.extraction_method == edge2.extraction_method
    assert edge1.extraction_method is not None


# ---------------------------------------------------------------------------
# Task 6 carry-over fix 2: False-path delegation — commit still called
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sleep_apply_supersede_false_still_commits():
    """_apply_supersede commits even when apply_supersession returns False (clobber guard)."""
    from nous.handlers.sleep_handler import SleepHandler

    winner_id = uuid4()
    loser_id = uuid4()

    mock_session = AsyncMock()
    mock_session.commit = AsyncMock()

    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=mock_session)
    ctx.__aexit__ = AsyncMock(return_value=False)

    mock_heart = MagicMock()
    mock_heart.db.session = MagicMock(return_value=ctx)
    mock_heart.facts = MagicMock()
    mock_heart.facts.apply_supersession = AsyncMock(return_value=False)

    handler = SleepHandler.__new__(SleepHandler)
    handler._heart = mock_heart

    result = await handler._apply_supersede(winner_id, loser_id)

    assert result is False
    mock_heart.facts.apply_supersession.assert_awaited_once_with(winner_id, loser_id, mock_session)
    mock_session.commit.assert_awaited_once()


# ---------------------------------------------------------------------------
# Task 7: R2.1/R2.2 — write-time key-conflict supersession (11 contract rows)
# ---------------------------------------------------------------------------
from datetime import date as _date

from nous.storage.models import Episode


async def _insert_episode(session, agent_id="nous-default") -> uuid4:
    """Insert a minimal episode row (FK target for source_episode_id)."""
    ep = Episode(agent_id=agent_id, summary="Test episode for R2 ordinal tests.")
    session.add(ep)
    await session.flush()
    return ep.id


def _unit_vec(i: int) -> list[float]:
    """1536-dim one-hot vector — orthogonal to _unit_vec(j) for i != j, so
    distinct indices never collide with the native-cosine dedup path."""
    v = [0.0] * 1536
    v[i] = 1.0
    return v


def _embedding_at_similarity(target_sim: float) -> list[float]:
    """1536-dim vector with cosine similarity == target_sim vs _unit_vec(0)."""
    import math

    perp = math.sqrt(max(0.0, 1.0 - target_sim ** 2))
    v = [0.0] * 1536
    v[0] = target_sim
    v[1] = perp
    return v


async def _get_fact(heart, fact_id, session=None):
    """Fetch a Fact row by id. With no `session`, opens a FRESH
    heart.db.session() — a different connection than any ambient test
    session, so this is load-bearing for tests verifying a caller-owned
    commit (e.g. resolve_key_conflict_pair) actually persisted: an
    in-memory ORM attribute check on the original session would pass even
    if the commit were missing. Pass an explicit `session` to read within
    an ambient (uncommitted) transaction the caller still holds open."""
    if session is not None:
        return await session.get(Fact, fact_id, populate_existing=True)
    async with heart.db.session() as s:
        return await s.get(Fact, fact_id)


_UPDATE_NEW = {"relation": "UPDATE", "current_fact": "new", "confidence": 0.9}
_UPDATE_OLD = {"relation": "UPDATE", "current_fact": "old", "confidence": 0.9}
_CONTRADICTION_OLD = {"relation": "CONTRADICTION", "current_fact": "old", "confidence": 0.9}
_CONTRADICTION_AMBIG = {"relation": "CONTRADICTION", "current_fact": "", "confidence": 0.9}
_UNRELATED = {"relation": "UNRELATED", "current_fact": "new", "confidence": 0.9}
_UPDATE_NO_DIR = {"relation": "UPDATE", "confidence": 0.9}  # no current_fact key → recency


# Row 1 ─────────────────────────────────────────────────────────────────────

@pytest.mark.postgres_only
async def test_r2_row1_flag_off_no_supersession(heart, session):
    """Row 1 (golden): flag OFF — same-key conflict produces no write-time supersession."""
    old_r = await heart.learn(
        FactInput(
            content="Server average response time is two hundred milliseconds measured daily.",
            subject_key="server",
            attribute_key="response_time",
        ),
        session=session,
    )
    new_r = await heart.learn(
        FactInput(
            content="Server average response time is three hundred fifty milliseconds measured daily.",
            subject_key="server",
            attribute_key="response_time",
        ),
        session=session,
    )
    await session.flush()
    old_row = await session.get(Fact, old_r.id)
    new_row = await session.get(Fact, new_r.id)
    assert old_row.superseded_by is None
    assert new_row.superseded_by is None
    assert old_row.active is True
    assert new_row.active is True


# Row 2 ─────────────────────────────────────────────────────────────────────

@pytest.mark.postgres_only
async def test_r2_row2_update_new_higher_ordinal_wins(heart, session, monkeypatch):
    """Row 2: flag ON, UPDATE conf 0.9, new.ordinal > old.ordinal, same episode → old superseded."""
    heart.facts._settings = heart.facts._settings.model_copy(
        update={"supersession_key_resolution_enabled": True}
    )
    ep_id = await _insert_episode(session)
    monkeypatch.setattr(heart.facts, "_classify_fact_pair", AsyncMock(return_value=_UPDATE_NEW))

    old_r = await heart.learn(
        FactInput(
            content="The deployment version in production environment is one point two point three.",
            subject_key="deployment",
            attribute_key="version",
            source_ordinal=1,
            source_episode_id=ep_id,
        ),
        session=session,
    )
    new_r = await heart.learn(
        FactInput(
            content="The deployment version in production environment is two point zero point zero.",
            subject_key="deployment",
            attribute_key="version",
            source_ordinal=2,
            source_episode_id=ep_id,
        ),
        session=session,
    )
    await session.flush()
    old_row = await session.get(Fact, old_r.id)
    assert old_row.superseded_by == new_r.id
    assert old_row.active is False
    new_row = await session.get(Fact, new_r.id)
    assert new_row.superseded_by is None
    assert new_row.active is True

    # Assert supersedes graph edge: new_r (winner) → old_r (loser)
    edge_r = await session.execute(
        select(GraphEdge)
        .where(GraphEdge.source_id == new_r.id)
        .where(GraphEdge.target_id == old_r.id)
        .where(GraphEdge.relation == "supersedes")
    )
    edge = edge_r.scalars().first()
    assert edge is not None


# Row 3 ─────────────────────────────────────────────────────────────────────

@pytest.mark.postgres_only
async def test_r2_row3_old_higher_ordinal_new_loses(heart, session, monkeypatch):
    """Row 3: old has higher ordinal (late-arriving earlier statement) → NEW superseded by old."""
    heart.facts._settings = heart.facts._settings.model_copy(
        update={"supersession_key_resolution_enabled": True}
    )
    ep_id = await _insert_episode(session)
    monkeypatch.setattr(heart.facts, "_classify_fact_pair", AsyncMock(return_value=_UPDATE_NEW))

    old_r = await heart.learn(
        FactInput(
            content="Office building entrance security code was changed to nine nine nine nine.",
            subject_key="office building",
            attribute_key="security_code",
            source_ordinal=5,
            source_episode_id=ep_id,
        ),
        session=session,
    )
    new_r = await heart.learn(
        FactInput(
            content="Office building entrance security code was changed to one two three four.",
            subject_key="office building",
            attribute_key="security_code",
            source_ordinal=1,
            source_episode_id=ep_id,
        ),
        session=session,
    )
    await session.flush()
    new_row = await session.get(Fact, new_r.id)
    assert new_row.superseded_by == old_r.id
    assert new_row.active is False
    old_row = await session.get(Fact, old_r.id)
    assert old_row.superseded_by is None
    assert old_row.active is True

    # Assert supersedes graph edge: old_r (winner) → new_r (loser)
    edge_r = await session.execute(
        select(GraphEdge)
        .where(GraphEdge.source_id == old_r.id)
        .where(GraphEdge.target_id == new_r.id)
        .where(GraphEdge.relation == "supersedes")
    )
    edge = edge_r.scalars().first()
    assert edge is not None


# Row 4 ─────────────────────────────────────────────────────────────────────

@pytest.mark.postgres_only
async def test_r2_row4_differing_event_dates_no_supersession(heart, session, monkeypatch):
    """Row 4 (F075 precedence): differing non-null event_dates → distinct events, KEEP BOTH."""
    heart.facts._settings = heart.facts._settings.model_copy(
        update={"supersession_key_resolution_enabled": True}
    )
    sentinel = AsyncMock(side_effect=AssertionError("classifier must not be called for F075 bypass"))
    monkeypatch.setattr(heart.facts, "_classify_fact_pair", sentinel)

    old_r = await heart.learn(
        FactInput(
            content="API endpoint health check returned status two hundred on first January twenty twenty four.",
            subject_key="api endpoint",
            attribute_key="health_status",
            event_date=_date(2024, 1, 1),
        ),
        session=session,
    )
    new_r = await heart.learn(
        FactInput(
            content="API endpoint health check returned status five hundred on second January twenty twenty four.",
            subject_key="api endpoint",
            attribute_key="health_status",
            event_date=_date(2024, 1, 2),
        ),
        session=session,
    )
    await session.flush()
    old_row = await session.get(Fact, old_r.id)
    new_row = await session.get(Fact, new_r.id)
    assert old_row.superseded_by is None
    assert new_row.superseded_by is None
    assert old_row.active is True
    assert new_row.active is True


# Row 5 ─────────────────────────────────────────────────────────────────────

@pytest.mark.postgres_only
async def test_r2_row5_unrelated_verdict_no_supersession(heart, session, monkeypatch):
    """Row 5: UNRELATED / low confidence / None → fail-open, KEEP BOTH."""
    heart.facts._settings = heart.facts._settings.model_copy(
        update={"supersession_key_resolution_enabled": True}
    )
    monkeypatch.setattr(heart.facts, "_classify_fact_pair", AsyncMock(return_value=_UNRELATED))

    old_r = await heart.learn(
        FactInput(
            content="The database cluster has three replicas configured for high availability.",
            subject_key="database cluster",
            attribute_key="replica_count",
        ),
        session=session,
    )
    new_r = await heart.learn(
        FactInput(
            content="The database cluster has five replicas configured for high availability.",
            subject_key="database cluster",
            attribute_key="replica_count",
        ),
        session=session,
    )
    await session.flush()
    old_row = await session.get(Fact, old_r.id)
    new_row = await session.get(Fact, new_r.id)
    assert old_row.superseded_by is None
    assert new_row.superseded_by is None


# Row 6 ─────────────────────────────────────────────────────────────────────

@pytest.mark.postgres_only
async def test_r2_row6_cap_limits_classifier_calls(heart, session, monkeypatch):
    """Row 6: 10 same-key active candidates, cap=3 → classifier called ≤3 times."""
    heart.facts._settings = heart.facts._settings.model_copy(
        update={
            "supersession_key_resolution_enabled": True,
            "supersession_key_candidates_cap": 3,
        }
    )
    # Insert 10 pre-existing same-key facts with distinct content.
    for i in range(10):
        await heart.learn(
            FactInput(
                content=f"Cache cluster node count is {i + 1} nodes active right now in prod.",
                subject_key="cache cluster",
                attribute_key="node_count",
            ),
            session=session,
        )
    await session.flush()

    call_count = 0

    async def _counting_classifier(old_content, new_content):
        nonlocal call_count
        call_count += 1
        return _UNRELATED

    monkeypatch.setattr(heart.facts, "_classify_fact_pair", _counting_classifier)

    await heart.learn(
        FactInput(
            content="Cache cluster node count is eleven nodes active right now in prod now.",
            subject_key="cache cluster",
            attribute_key="node_count",
        ),
        session=session,
    )
    assert call_count <= 3


# Row 7 ─────────────────────────────────────────────────────────────────────

@pytest.mark.postgres_only
async def test_r2_row7_budget_exhausted_no_classifier(heart, session, monkeypatch):
    """Row 7: hourly budget exhausted → classifier NOT called, no supersession."""
    heart.facts._settings = heart.facts._settings.model_copy(
        update={"supersession_key_resolution_enabled": True}
    )
    import time as _time
    # Exhaust the budget by setting calls to cap.
    heart.facts._key_bucket = int(_time.monotonic() // 3600)
    heart.facts._key_calls = 500  # default cap

    sentinel = AsyncMock(side_effect=AssertionError("classifier must not be called when budget is spent"))
    monkeypatch.setattr(heart.facts, "_classify_fact_pair", sentinel)

    old_r = await heart.learn(
        FactInput(
            content="Feature flag dark mode enabled is set to true in configuration.",
            subject_key="feature flag",
            attribute_key="dark_mode",
        ),
        session=session,
    )
    new_r = await heart.learn(
        FactInput(
            content="Feature flag dark mode enabled is set to false in configuration.",
            subject_key="feature flag",
            attribute_key="dark_mode",
        ),
        session=session,
    )
    await session.flush()
    old_row = await session.get(Fact, old_r.id)
    new_row = await session.get(Fact, new_r.id)
    assert old_row.superseded_by is None
    assert new_row.superseded_by is None
    sentinel.assert_not_called()


# Row 8 ─────────────────────────────────────────────────────────────────────

@pytest.mark.postgres_only
async def test_r2_row8_policy_recency_no_ordinals(heart, session, monkeypatch):
    """Row 8: policy=recency, no ordinals → later learned_at wins (new fact wins)."""
    heart.facts._settings = heart.facts._settings.model_copy(
        update={
            "supersession_key_resolution_enabled": True,
            "supersession_policy": "recency",
        }
    )
    # Return UPDATE with no current_fact direction → falls through to recency.
    monkeypatch.setattr(heart.facts, "_classify_fact_pair", AsyncMock(return_value=_UPDATE_NO_DIR))

    old_r = await heart.learn(
        FactInput(
            content="User subscription tier is currently basic plan active on the account.",
            subject_key="user subscription",
            attribute_key="tier",
        ),
        session=session,
    )
    new_r = await heart.learn(
        FactInput(
            content="User subscription tier is currently premium plan active on the account.",
            subject_key="user subscription",
            attribute_key="tier",
        ),
        session=session,
    )
    await session.flush()
    old_row = await session.get(Fact, old_r.id)
    assert old_row.superseded_by == new_r.id
    assert old_row.active is False
    new_row = await session.get(Fact, new_r.id)
    assert new_row.superseded_by is None
    assert new_row.active is True


# Row 9 ─────────────────────────────────────────────────────────────────────

@pytest.mark.postgres_only
async def test_r2_row9_contradiction_later_stated_wins_despite_classifier(heart, session, monkeypatch):
    """Row 9 (Gate-1 D1): CONTRADICTION + current_fact=old, new has HIGHER ordinal
    → NEW wins (later statement order), classifier's current_fact is ignored."""
    heart.facts._settings = heart.facts._settings.model_copy(
        update={"supersession_key_resolution_enabled": True}
    )
    ep_id = await _insert_episode(session)
    monkeypatch.setattr(heart.facts, "_classify_fact_pair", AsyncMock(return_value=_CONTRADICTION_OLD))

    old_r = await heart.learn(
        FactInput(
            content="The capital city of France has always been Paris throughout recorded history.",
            subject_key="france",
            attribute_key="capital_city",
            source_ordinal=1,
            source_episode_id=ep_id,
        ),
        session=session,
    )
    new_r = await heart.learn(
        FactInput(
            content="The capital city of France has always been London throughout recorded history.",
            subject_key="france",
            attribute_key="capital_city",
            source_ordinal=2,  # higher ordinal — later statement wins despite current_fact=old
            source_episode_id=ep_id,
        ),
        session=session,
    )
    await session.flush()
    old_row = await session.get(Fact, old_r.id)
    assert old_row.superseded_by == new_r.id
    assert old_row.active is False
    new_row = await session.get(Fact, new_r.id)
    assert new_row.superseded_by is None
    assert new_row.active is True

    # Assert supersedes graph edge: new_r (winner) → old_r (loser)
    edge_r = await session.execute(
        select(GraphEdge)
        .where(GraphEdge.source_id == new_r.id)
        .where(GraphEdge.target_id == old_r.id)
        .where(GraphEdge.relation == "supersedes")
    )
    edge = edge_r.scalars().first()
    assert edge is not None


# Row 10 ─────────────────────────────────────────────────────────────────────

@pytest.mark.postgres_only
async def test_r2_row10_contradiction_ambiguous_keep_both(heart, session, monkeypatch):
    """Row 10: CONTRADICTION conf 0.9, no ordinals/shared episode → NO supersession.
    Gate-1 D1: with no ordering signal (both facts share the conftest session's
    single transaction, so learned_at is transaction-constant and identical),
    _pick_contradiction_winner returns None → KEEP-BOTH, now also flagged via
    _flag_contradiction_pair (contradiction_of + contradicts edge) like the
    other keep-both sites."""
    heart.facts._settings = heart.facts._settings.model_copy(
        update={"supersession_key_resolution_enabled": True}
    )
    monkeypatch.setattr(heart.facts, "_classify_fact_pair", AsyncMock(return_value=_CONTRADICTION_AMBIG))

    old_r = await heart.learn(
        FactInput(
            content="Mathematical constant pi is equal to three point one four one five nine.",
            subject_key="mathematical_constant",
            attribute_key="pi_value",
        ),
        session=session,
    )
    new_r = await heart.learn(
        FactInput(
            content="Mathematical constant pi is equal to three point one four one five eight.",
            subject_key="mathematical_constant",
            attribute_key="pi_value",
        ),
        session=session,
    )
    await session.flush()
    old_row = await session.get(Fact, old_r.id)
    new_row = await session.get(Fact, new_r.id)
    assert old_row.superseded_by is None
    assert new_row.superseded_by is None
    assert old_row.active is True
    assert new_row.active is True
    # Gate-1 D1: unordered CONTRADICTION now flags on KEEP-BOTH (mirrors path A/C).
    assert old_row.learned_at == new_row.learned_at, (
        "sanity: same-transaction learned_at must be identical for the "
        "unordered branch to be the one under test"
    )
    assert new_row.contradiction_of == old_r.id
    edge_r = await session.execute(
        select(GraphEdge)
        .where(GraphEdge.source_id == new_r.id)
        .where(GraphEdge.target_id == old_r.id)
        .where(GraphEdge.relation == "contradicts")
    )
    assert edge_r.scalars().first() is not None


# Row 11 ─────────────────────────────────────────────────────────────────────

@pytest.mark.postgres_only
async def test_r2_row11_keyed_fact_runs_legacy_when_r2_off(heart, session, monkeypatch):
    """Row 11 (codex r5 inverted): R2 OFF + keyed fact → legacy _supersede_by_subject RUNS.
    With R2 disabled, keyed facts have no keyed resolver, so the legacy subject path is
    the only write-time supersession guard and must NOT be skipped."""
    sentinel = AsyncMock()
    monkeypatch.setattr(heart.facts, "_supersede_by_subject", sentinel)
    # Default Settings has supersession_key_resolution_enabled=False — R2 is off.

    await heart.learn(
        FactInput(
            content="Red sports car belongs to Alice who lives downtown near the park.",
            subject="red sports car",
            subject_key="red sports car",
            attribute_key="owner",
        ),
        session=session,
    )
    sentinel.assert_called_once()


@pytest.mark.postgres_only
async def test_r2_row11_keyed_fact_skips_legacy_when_r2_on(heart, session, monkeypatch):
    """Row 11 sibling (codex r5): R2 ON + keyed fact → legacy _supersede_by_subject SKIPPED.
    When R2 is enabled and both keys are present, keyed resolution owns the adjudication;
    the legacy uncapped subject path must NOT double-adjudicate."""
    sentinel = AsyncMock()
    monkeypatch.setattr(heart.facts, "_supersede_by_subject", sentinel)
    # Enable R2 on the facts module's settings copy.
    monkeypatch.setattr(
        heart.facts,
        "_settings",
        heart.facts._settings.model_copy(update={"supersession_key_resolution_enabled": True}),
    )

    await heart.learn(
        FactInput(
            content="Red sports car belongs to Alice who lives downtown near the park.",
            subject="red sports car",
            subject_key="red sports car",
            attribute_key="owner",
        ),
        session=session,
    )
    sentinel.assert_not_called()


# Gate-1 D1 ───────────────────────────────────────────────────────────────────
# CONTRADICTION resolves by statement order, never by which claim the
# classifier believes is true (F085 Gate-1 fixes plan, Task 1).


@pytest.mark.postgres_only
class TestGate1ContradictionOrderResolution:
    async def test_figaro_shape_later_stated_wins_despite_world_knowledge(self, heart, session, monkeypatch):
        """Gate-1 regression: the later-stated 'wrong' value must WIN.
        Statement 1: author is Pierre Beaumarchais (world-true, ordinal 100).
        Statement 2 (later): author is Thomas Kyd (benchmark gold, ordinal 200).
        Classifier says CONTRADICTION and (biased) current_fact='old' — the
        resolver must IGNORE current_fact and supersede the EARLIER fact."""
        monkeypatch.setattr(
            "nous.heart.facts.FactManager._classify_fact_pair",
            AsyncMock(return_value={"relation": "CONTRADICTION", "current_fact": "old", "confidence": 0.95}),
        )
        s = heart.facts._settings.model_copy(update={"supersession_key_resolution_enabled": True})
        monkeypatch.setattr(heart.facts, "_settings", s)
        ep_id = await _insert_episode(session)
        r1 = await heart.learn(FactInput(
            content="The author of The Marriage of Figaro is Pierre Beaumarchais.",
            subject="marriage of figaro", subject_key="marriage of figaro",
            attribute_key="author", source_episode_id=ep_id, source_ordinal=100,
        ), session=session, precomputed_embedding=_unit_vec(0))
        r2 = await heart.learn(FactInput(
            content="The author of The Marriage of Figaro is Thomas Kyd.",
            subject="marriage of figaro", subject_key="marriage of figaro",
            attribute_key="author", source_episode_id=ep_id, source_ordinal=200,
        ), session=session, precomputed_embedding=_unit_vec(1))  # orthogonal vec: dedup miss -> R2 path
        await session.flush()
        old = await _get_fact(heart, r1.id, session)
        new = await _get_fact(heart, r2.id, session)
        assert new.active and new.superseded_by is None      # later-stated WINS
        assert (not old.active) and old.superseded_by == new.id

    async def test_unordered_contradiction_keeps_both_and_flags(self, heart, monkeypatch):
        """No ordinals, no shared episode — force the unordered case via a
        direct ORM update pinning both facts to an IDENTICAL learned_at, then
        drive resolve_key_conflict_pair (the sweep seam) directly with the
        CONTRADICTION mock. _pick_contradiction_winner has no ordering signal
        left -> KEEP-BOTH, flagged via _flag_contradiction_pair (contradiction_of
        + contradicts edge). The sweep branch commits its own session, so this
        re-reads through a FRESH session to prove the commit actually landed."""
        from datetime import UTC, datetime

        from sqlalchemy import update as sa_update

        unique_agent = f"g1-{uuid4().hex[:8]}"
        heart.facts.agent_id = unique_agent
        heart.facts._settings = heart.facts._settings.model_copy(
            update={"supersession_key_resolution_enabled": True}
        )

        f1 = await heart.learn(FactInput(
            content="The store's return policy allows returns within thirty days.",
            subject_key="store", attribute_key="return_policy",
        ))
        f2 = await heart.learn(FactInput(
            content="The store's return policy allows returns within ninety days.",
            subject_key="store", attribute_key="return_policy",
        ))

        fixed_ts = datetime.now(UTC)
        async with heart.db.session() as s:
            await s.execute(
                sa_update(Fact).where(Fact.id.in_([f1.id, f2.id])).values(learned_at=fixed_ts)
            )
            await s.commit()

        monkeypatch.setattr(
            heart.facts,
            "_classify_fact_pair",
            AsyncMock(return_value={"relation": "CONTRADICTION", "current_fact": "old", "confidence": 0.95}),
        )

        result = await heart.facts.resolve_key_conflict_pair(f1.id, f2.id, f1.content, f2.content)
        assert result is False  # KEEP-BOTH: no supersession written

        old = await _get_fact(heart, f1.id)
        new = await _get_fact(heart, f2.id)
        assert old.active is True and new.active is True
        assert old.superseded_by is None and new.superseded_by is None
        assert new.contradiction_of == old.id

        async with heart.db.session() as s:
            edge_r = await s.execute(
                select(GraphEdge)
                .where(GraphEdge.source_id == f2.id)
                .where(GraphEdge.target_id == f1.id)
                .where(GraphEdge.relation == "contradicts")
            )
            assert edge_r.scalars().first() is not None

    async def test_flag_contradiction_pair_converges_on_repeat_sweep(self, heart, monkeypatch):
        """Final review C1: a KEEP-BOTH pair never sets superseded_by, so it
        stays active and keeps re-surfacing to resolve_key_conflict_pair on
        every later sleep sweep / backfill re-run. Calling it TWICE on the
        same unordered pair must decrement confidence exactly ONCE (not
        compound toward 0) and must not create a second contradicts edge."""
        from datetime import UTC, datetime

        from sqlalchemy import update as sa_update

        unique_agent = f"g1-{uuid4().hex[:8]}"
        heart.facts.agent_id = unique_agent
        heart.facts._settings = heart.facts._settings.model_copy(
            update={"supersession_key_resolution_enabled": True}
        )

        f1 = await heart.learn(FactInput(
            content="The warehouse shift schedule starts at six in the morning.",
            subject_key="warehouse", attribute_key="shift_schedule",
        ))
        f2 = await heart.learn(FactInput(
            content="The warehouse shift schedule starts at seven in the morning.",
            subject_key="warehouse", attribute_key="shift_schedule",
        ))

        fixed_ts = datetime.now(UTC)
        async with heart.db.session() as s:
            await s.execute(
                sa_update(Fact).where(Fact.id.in_([f1.id, f2.id])).values(learned_at=fixed_ts)
            )
            await s.commit()

        monkeypatch.setattr(
            heart.facts,
            "_classify_fact_pair",
            AsyncMock(return_value={"relation": "CONTRADICTION", "current_fact": "old", "confidence": 0.95}),
        )

        result1 = await heart.facts.resolve_key_conflict_pair(f1.id, f2.id, f1.content, f2.content)
        assert result1 is False  # KEEP-BOTH: no supersession written

        old_after_first = await _get_fact(heart, f1.id)
        assert old_after_first.confidence == pytest.approx(0.8)  # 1.0 - 0.2, decremented once

        # Repeat sweep re-processes the same still-active, unresolved pair.
        result2 = await heart.facts.resolve_key_conflict_pair(f1.id, f2.id, f1.content, f2.content)
        assert result2 is False  # still KEEP-BOTH

        old = await _get_fact(heart, f1.id)
        new = await _get_fact(heart, f2.id)
        assert old.active is True and new.active is True
        assert old.confidence == pytest.approx(0.8)  # unchanged — converged, not re-decremented
        assert new.contradiction_of == old.id

        async with heart.db.session() as s:
            edge_r = await s.execute(
                select(GraphEdge)
                .where(GraphEdge.source_id == f2.id)
                .where(GraphEdge.target_id == f1.id)
                .where(GraphEdge.relation == "contradicts")
            )
            edges = edge_r.scalars().all()
            assert len(edges) == 1  # single edge, not duplicated on repeat

    async def test_flag_contradiction_pair_converges_on_cluster(self, heart, monkeypatch):
        """Codex r1: the column-only guard (b.contradiction_of == a.id) forgets
        pairs in a 3+ fact cluster. Seed A, B, C same-slot, all unordered (equal
        learned_at). Flag (A, C) then (B, C) — the second call OVERWRITES
        C.contradiction_of from A.id to B.id, so a naive column-only check on a
        THIRD call re-processing (A, C) would no longer see the earlier flag
        and would re-decrement A.confidence. The edge-based idempotence check
        must catch this via the existing contradicts edge (either direction)."""
        from datetime import UTC, datetime

        from sqlalchemy import update as sa_update

        unique_agent = f"g1-{uuid4().hex[:8]}"
        heart.facts.agent_id = unique_agent
        heart.facts._settings = heart.facts._settings.model_copy(
            update={"supersession_key_resolution_enabled": True}
        )

        a = await heart.learn(FactInput(
            content="The team standup meeting time is nine in the morning.",
            subject_key="team", attribute_key="standup_time",
        ))
        b = await heart.learn(FactInput(
            content="The team standup meeting time is ten in the morning.",
            subject_key="team", attribute_key="standup_time",
        ))
        c = await heart.learn(FactInput(
            content="The team standup meeting time is eleven in the morning.",
            subject_key="team", attribute_key="standup_time",
        ))

        fixed_ts = datetime.now(UTC)
        async with heart.db.session() as s:
            await s.execute(
                sa_update(Fact).where(Fact.id.in_([a.id, b.id, c.id])).values(learned_at=fixed_ts)
            )
            await s.commit()

        monkeypatch.setattr(
            heart.facts,
            "_classify_fact_pair",
            AsyncMock(return_value={"relation": "CONTRADICTION", "current_fact": "old", "confidence": 0.95}),
        )

        # (A, C): flags C.contradiction_of -> A, decrements A, writes edge C->A.
        r1 = await heart.facts.resolve_key_conflict_pair(a.id, c.id, a.content, c.content)
        assert r1 is False

        # (B, C): C.contradiction_of currently points at A (not B), so the
        # column fast path misses and this proceeds — OVERWRITING
        # C.contradiction_of -> B, decrementing B, writing edge C->B.
        r2 = await heart.facts.resolve_key_conflict_pair(b.id, c.id, b.content, c.content)
        assert r2 is False

        # (A, C) AGAIN: the column now points at B, not A — the column-only
        # fast path can't catch this repeat. The edge-based check must.
        r3 = await heart.facts.resolve_key_conflict_pair(a.id, c.id, a.content, c.content)
        assert r3 is False

        fa = await _get_fact(heart, a.id)
        fb = await _get_fact(heart, b.id)
        fc = await _get_fact(heart, c.id)
        assert fa.confidence == pytest.approx(0.8)  # decremented exactly once, not compounded
        assert fb.confidence == pytest.approx(0.8)  # decremented exactly once
        assert fc.confidence == pytest.approx(1.0)  # C is never the "a" (older) side — untouched

        async with heart.db.session() as s:
            edge_r = await s.execute(
                select(GraphEdge)
                .where(GraphEdge.agent_id == unique_agent)
                .where(GraphEdge.relation == "contradicts")
            )
            edges = edge_r.scalars().all()
            assert len(edges) == 2  # C->A and C->B only; the third call created no new edge

    async def test_flag_contradiction_pair_recreates_edge_post_rollback(self, heart, monkeypatch):
        """Codex r3: the documented rollback (backfill_supersession.py header)
        deletes contradicts edges but leaves contradiction_of as accepted
        residue. A re-run's keep-both pass must recreate the missing edge —
        not just hit the column fast path and return before the graph is
        repaired — while still NOT re-decrementing confidence a second time."""
        from datetime import UTC, datetime

        from sqlalchemy import delete as sa_delete
        from sqlalchemy import update as sa_update

        unique_agent = f"g1-{uuid4().hex[:8]}"
        heart.facts.agent_id = unique_agent
        heart.facts._settings = heart.facts._settings.model_copy(
            update={"supersession_key_resolution_enabled": True}
        )

        f1 = await heart.learn(FactInput(
            content="The office parking policy allows two spots per employee.",
            subject_key="office", attribute_key="parking_policy",
        ))
        f2 = await heart.learn(FactInput(
            content="The office parking policy allows three spots per employee.",
            subject_key="office", attribute_key="parking_policy",
        ))

        fixed_ts = datetime.now(UTC)
        async with heart.db.session() as s:
            await s.execute(
                sa_update(Fact).where(Fact.id.in_([f1.id, f2.id])).values(learned_at=fixed_ts)
            )
            await s.commit()

        monkeypatch.setattr(
            heart.facts,
            "_classify_fact_pair",
            AsyncMock(return_value={"relation": "CONTRADICTION", "current_fact": "old", "confidence": 0.95}),
        )

        # First pass: flags the pair, decrements f1, writes the edge.
        r1 = await heart.facts.resolve_key_conflict_pair(f1.id, f2.id, f1.content, f2.content)
        assert r1 is False
        f1_after_first = await _get_fact(heart, f1.id)
        assert f1_after_first.confidence == pytest.approx(0.8)

        # Simulate the documented rollback: delete the contradicts edge only.
        # contradiction_of is accepted residue and is NOT reset (matches the
        # backfill_supersession.py rollback contract).
        async with heart.db.session() as s:
            await s.execute(
                sa_delete(GraphEdge)
                .where(GraphEdge.agent_id == unique_agent)
                .where(GraphEdge.relation == "contradicts")
            )
            await s.commit()

        async with heart.db.session() as s:
            edge_check = await s.execute(
                select(GraphEdge)
                .where(GraphEdge.agent_id == unique_agent)
                .where(GraphEdge.relation == "contradicts")
            )
            assert edge_check.scalars().first() is None  # rollback confirmed: edge gone

        # Second pass (post-rollback re-run): must recreate the edge without
        # re-decrementing confidence.
        r2 = await heart.facts.resolve_key_conflict_pair(f1.id, f2.id, f1.content, f2.content)
        assert r2 is False

        f1_after_second = await _get_fact(heart, f1.id)
        assert f1_after_second.confidence == pytest.approx(0.8)  # NOT re-decremented to 0.6

        async with heart.db.session() as s:
            edge_r = await s.execute(
                select(GraphEdge)
                .where(GraphEdge.agent_id == unique_agent)
                .where(GraphEdge.relation == "contradicts")
            )
            edges = edge_r.scalars().all()
            assert len(edges) == 1  # edge recreated

    async def test_flag_contradiction_pair_reversed_roles_no_duplicate_edge(self, heart, monkeypatch):
        """Codex r12: a sweep revisit can present the SAME pair with a/b
        roles REVERSED from the original flagging call (find_key_conflict_pairs
        orders equal-learned_at pairs by UUID, which can flip which fact is
        id1/id2 across runs). The bidirectional edge query already catches
        this for the confidence-decrement guard (no second decrement) -- but
        the old code only computed that query as a fallback INSIDE the
        column-fast-path branch, then unconditionally wrote the edge in
        whatever (b, a) direction THIS call uses. A reversed-roles revisit
        therefore fell through to an unconditional write in the OPPOSITE
        direction from the original edge, creating a second, duplicate
        contradicts edge for one pair.

        Codex r13: the column write had the same bug — a reversed-roles
        revisit unconditionally set THIS call's b.contradiction_of = a.id,
        which (with a/b swapped) pointed f1 back at f2 in addition to f2's
        original pointer at f1, leaving both facts pointing at each other on
        what should be an idempotent no-op."""
        from datetime import UTC, datetime

        from sqlalchemy import and_, or_
        from sqlalchemy import update as sa_update

        unique_agent = f"g1-{uuid4().hex[:8]}"
        heart.facts.agent_id = unique_agent
        heart.facts._settings = heart.facts._settings.model_copy(
            update={"supersession_key_resolution_enabled": True}
        )

        f1 = await heart.learn(FactInput(
            content="The lobby reception desk closes at five in the evening.",
            subject_key="lobby", attribute_key="reception_hours",
        ))
        f2 = await heart.learn(FactInput(
            content="The lobby reception desk closes at six in the evening.",
            subject_key="lobby", attribute_key="reception_hours",
        ))

        fixed_ts = datetime.now(UTC)
        async with heart.db.session() as s:
            await s.execute(
                sa_update(Fact).where(Fact.id.in_([f1.id, f2.id])).values(learned_at=fixed_ts)
            )
            await s.commit()

        monkeypatch.setattr(
            heart.facts,
            "_classify_fact_pair",
            AsyncMock(return_value={"relation": "CONTRADICTION", "current_fact": "old", "confidence": 0.95}),
        )

        # First pass: flags (f1, f2), decrements f1, writes edge f2->f1.
        r1 = await heart.facts.resolve_key_conflict_pair(f1.id, f2.id, f1.content, f2.content)
        assert r1 is False

        # Sweep revisit with roles REVERSED (f2 now "older"/id1, f1 "newer"/id2)
        # -- simulates find_key_conflict_pairs' UUID-ordering flipping which
        # fact lands in which slot across runs for an equal-learned_at pair.
        r2 = await heart.facts.resolve_key_conflict_pair(f2.id, f1.id, f2.content, f1.content)
        assert r2 is False

        f1_after = await _get_fact(heart, f1.id)
        f2_after = await _get_fact(heart, f2.id)
        assert f1_after.confidence == pytest.approx(0.8)  # decremented exactly once
        assert f2_after.confidence == pytest.approx(1.0)  # never the "a" (older) role -- untouched

        # Codex r13: the reversed revisit must be a true no-op on the column
        # too -- only f2 (the original "b") points at f1; f1 must NOT also
        # point back at f2 just because a later call flagged the pair with
        # roles reversed.
        assert f2_after.contradiction_of == f1.id
        assert f1_after.contradiction_of is None

        async with heart.db.session() as s:
            edge_r = await s.execute(
                select(GraphEdge)
                .where(GraphEdge.agent_id == unique_agent)
                .where(GraphEdge.relation == "contradicts")
                .where(
                    or_(
                        and_(GraphEdge.source_id == f2.id, GraphEdge.target_id == f1.id),
                        and_(GraphEdge.source_id == f1.id, GraphEdge.target_id == f2.id),
                    )
                )
            )
            edges = edge_r.scalars().all()
            assert len(edges) == 1, (
                "exactly one contradicts edge must exist between the pair regardless "
                "of which direction a reversed-roles revisit flags it from"
            )

    async def test_flag_contradiction_pair_rollback_plus_reversed_revisit(self, heart, monkeypatch):
        """Codex r14: the composite of r3 (post-rollback edge repair) + r13
        (reversed-roles no-op) -- the edge is deleted (rollback) AND the
        sweep revisit presents the pair in REVERSED order. The one-way
        column_match (b.contradiction_of == a.id) only checks THIS call's
        `b` side; the residue from the ORIGINAL flag sits on the other side
        (a.contradiction_of == b.id in this call's roles). With the edge
        gone and the column check missing the reversed residue,
        already_flagged fell through False pre-fix -- re-decrementing the
        (wrong) fact's confidence and writing a fresh column + edge in the
        REVERSE direction from the original. Edge repair must recreate the
        edge in its ORIGINAL recorded orientation, not this call's (b, a)."""
        from datetime import UTC, datetime

        from sqlalchemy import and_, or_
        from sqlalchemy import delete as sa_delete
        from sqlalchemy import update as sa_update

        unique_agent = f"g1-{uuid4().hex[:8]}"
        heart.facts.agent_id = unique_agent
        heart.facts._settings = heart.facts._settings.model_copy(
            update={"supersession_key_resolution_enabled": True}
        )

        old = await heart.learn(FactInput(
            content="The archive retention policy keeps records for five years.",
            subject_key="archive", attribute_key="retention_policy",
        ))
        new = await heart.learn(FactInput(
            content="The archive retention policy keeps records for seven years.",
            subject_key="archive", attribute_key="retention_policy",
        ))

        fixed_ts = datetime.now(UTC)
        async with heart.db.session() as s:
            await s.execute(
                sa_update(Fact).where(Fact.id.in_([old.id, new.id])).values(learned_at=fixed_ts)
            )
            await s.commit()

        monkeypatch.setattr(
            heart.facts,
            "_classify_fact_pair",
            AsyncMock(return_value={"relation": "CONTRADICTION", "current_fact": "old", "confidence": 0.95}),
        )

        # First pass: flags (old, new) -- new points at old, edge new->old,
        # old decremented to 0.8.
        r1 = await heart.facts.resolve_key_conflict_pair(old.id, new.id, old.content, new.content)
        assert r1 is False
        old_after_first = await _get_fact(heart, old.id)
        assert old_after_first.confidence == pytest.approx(0.8)

        # Simulate the documented rollback: delete the contradicts edge only
        # (contradiction_of residue is intentionally left, per r3).
        async with heart.db.session() as s:
            await s.execute(
                sa_delete(GraphEdge)
                .where(GraphEdge.agent_id == unique_agent)
                .where(GraphEdge.relation == "contradicts")
            )
            await s.commit()

        # Sweep revisit, roles REVERSED (new now id1/"older" role, old now
        # id2/"newer" role) -- simulates find_key_conflict_pairs presenting
        # this equal-learned_at pair in the opposite order post-rollback.
        r2 = await heart.facts.resolve_key_conflict_pair(new.id, old.id, new.content, old.content)
        assert r2 is False

        old_after = await _get_fact(heart, old.id)
        new_after = await _get_fact(heart, new.id)
        assert old_after.confidence == pytest.approx(0.8)  # no second decrement
        assert new_after.confidence == pytest.approx(1.0)  # never decremented -- not the "a" (older) role
        assert old_after.contradiction_of is None  # no reverse column write
        assert new_after.contradiction_of == old.id  # original pointer preserved

        async with heart.db.session() as s:
            edge_r = await s.execute(
                select(GraphEdge)
                .where(GraphEdge.agent_id == unique_agent)
                .where(GraphEdge.relation == "contradicts")
                .where(
                    or_(
                        and_(GraphEdge.source_id == new.id, GraphEdge.target_id == old.id),
                        and_(GraphEdge.source_id == old.id, GraphEdge.target_id == new.id),
                    )
                )
            )
            edges = edge_r.scalars().all()
            assert len(edges) == 1, "exactly one contradicts edge after repair"
            assert edges[0].source_id == new.id and edges[0].target_id == old.id, (
                "repaired edge must follow the ORIGINAL recorded direction (new->old), "
                "not this call's (b, a) orientation"
            )

    async def test_flag_contradiction_pair_preserves_explicit_zero_confidence(self, heart, monkeypatch):
        """Codex r8: `a.confidence or 1.0` treats a legitimate 0.0 as missing
        and RAISES it to 0.8 on flagging -- `is None` is the correct
        missing-value check. An older fact already at 0.0 confidence must
        stay at 0.0 after a keep-both flag, not jump up to 0.8."""
        from datetime import UTC, datetime

        from sqlalchemy import update as sa_update

        unique_agent = f"g1-{uuid4().hex[:8]}"
        heart.facts.agent_id = unique_agent
        heart.facts._settings = heart.facts._settings.model_copy(
            update={"supersession_key_resolution_enabled": True}
        )

        f1 = await heart.learn(FactInput(
            content="The warehouse loading dock closes at six in the evening.",
            subject_key="warehouse", attribute_key="dock_hours",
        ))
        f2 = await heart.learn(FactInput(
            content="The warehouse loading dock closes at seven in the evening.",
            subject_key="warehouse", attribute_key="dock_hours",
        ))

        fixed_ts = datetime.now(UTC)
        async with heart.db.session() as s:
            await s.execute(
                sa_update(Fact).where(Fact.id.in_([f1.id, f2.id])).values(learned_at=fixed_ts)
            )
            # f1 already carries a legitimate 0.0 confidence (e.g. from a
            # prior contradiction decrement floor) BEFORE this flag pass.
            await s.execute(
                sa_update(Fact).where(Fact.id == f1.id).values(confidence=0.0)
            )
            await s.commit()

        monkeypatch.setattr(
            heart.facts,
            "_classify_fact_pair",
            AsyncMock(return_value={"relation": "CONTRADICTION", "current_fact": "old", "confidence": 0.95}),
        )

        result = await heart.facts.resolve_key_conflict_pair(f1.id, f2.id, f1.content, f2.content)
        assert result is False  # KEEP-BOTH

        f1_after = await _get_fact(heart, f1.id)
        assert f1_after.confidence == pytest.approx(0.0)  # NOT raised to 0.8

    def test_prompt_and_schema_debias_pins(self):
        from nous.heart.facts import _SUPERSESSION_CLASSIFIER_PROMPT_TEMPLATE, _SUPERSESSION_CLASSIFIER_SCHEMA

        p = _SUPERSESSION_CLASSIFIER_PROMPT_TEMPLATE
        assert "factually correct" not in p
        assert "Capital of France" not in p
        assert "settings.yaml" in p and "config.toml" in p  # replacement example
        assert "correct" not in _SUPERSESSION_CLASSIFIER_SCHEMA["properties"]["current_fact"]["description"]

    async def test_legacy_subject_path_contradiction_resolves_by_order(self, heart, session, monkeypatch):
        """Codex r10: the legacy _supersede_by_subject path (R2 flag OFF,
        the prod default) is the ONLY write-time resolver for unkeyed
        facts — it must not throw away ordering data it actually has. When
        the two facts carry DIFFERING learned_at, CONTRADICTION now
        resolves by statement order (later wins), via the SAME uniform
        apply_supersession primitive the keyed paths use — not always
        KEEP-BOTH+flag regardless of available ordering data. The
        classifier's current_fact="old" verdict is advisory only and must
        be ignored (same contract as the keyed paths)."""
        from datetime import UTC, datetime, timedelta

        from sqlalchemy import update as sa_update

        monkeypatch.setattr(heart.facts, "_llm", MagicMock())  # truthy sentinel; classify is mocked below
        monkeypatch.setattr(
            heart.facts,
            "_classify_fact_pair",
            AsyncMock(return_value={"relation": "CONTRADICTION", "current_fact": "old", "confidence": 0.9}),
        )
        anchor = _unit_vec(0)
        candidate = _embedding_at_similarity(0.88)  # inside the 0.80-0.95 F027 band

        old_r = await heart.learn(FactInput(
            content="The office thermostat is set to seventy two degrees fahrenheit.",
            subject="office thermostat",
        ), session=session, precomputed_embedding=anchor)

        # Backdate old_r BEFORE learning new_r, within the same ambient
        # transaction, so _supersede_by_subject (which fires live during
        # new_r's learn() call, below) sees genuinely differing learned_at —
        # same-session learns otherwise share a transaction-constant "now()".
        await session.execute(
            sa_update(Fact).where(Fact.id == old_r.id).values(
                learned_at=datetime.now(UTC) - timedelta(hours=1)
            )
        )
        await session.flush()

        new_r = await heart.learn(FactInput(
            content="The office thermostat is set to sixty eight degrees fahrenheit.",
            subject="office thermostat",
        ), session=session, precomputed_embedding=candidate)

        await session.flush()
        old = await _get_fact(heart, old_r.id, session)
        new = await _get_fact(heart, new_r.id, session)
        assert new.active and new.superseded_by is None  # later-stated WINS
        assert not old.active and old.superseded_by == new_r.id

        edge_r = await session.execute(
            select(GraphEdge)
            .where(GraphEdge.source_id == new_r.id)
            .where(GraphEdge.target_id == old_r.id)
        )
        relations = {e.relation for e in edge_r.scalars().all()}
        assert "supersedes" in relations

    async def test_legacy_subject_path_contradiction_keeps_both_when_unordered(self, heart, session, monkeypatch):
        """Codex r10 sibling: when neither fact carries an ordering signal
        (same-session learns share a transaction-constant learned_at, no
        ordinals), _pick_contradiction_winner returns None and the legacy
        path falls back to KEEP-BOTH + flag — not a silent supersede with a
        wrong-type 'supersedes' edge (the pre-Gate-1 fall-through)."""
        monkeypatch.setattr(heart.facts, "_llm", MagicMock())  # truthy sentinel; classify is mocked below
        monkeypatch.setattr(
            heart.facts,
            "_classify_fact_pair",
            AsyncMock(return_value={"relation": "CONTRADICTION", "current_fact": "old", "confidence": 0.9}),
        )
        anchor = _unit_vec(0)
        candidate = _embedding_at_similarity(0.88)  # inside the 0.80-0.95 F027 band

        old_r = await heart.learn(FactInput(
            content="The office thermostat is set to seventy two degrees fahrenheit.",
            subject="office thermostat",
        ), session=session, precomputed_embedding=anchor)
        new_r = await heart.learn(FactInput(
            content="The office thermostat is set to sixty eight degrees fahrenheit.",
            subject="office thermostat",
        ), session=session, precomputed_embedding=candidate)

        await session.flush()
        old = await _get_fact(heart, old_r.id, session)
        new = await _get_fact(heart, new_r.id, session)
        assert old.active and new.active
        assert new.contradiction_of == old.id

        edge_r = await session.execute(
            select(GraphEdge)
            .where(GraphEdge.source_id == new_r.id)
            .where(GraphEdge.target_id == old_r.id)
        )
        relations = {e.relation for e in edge_r.scalars().all()}
        assert "supersedes" not in relations
        assert "contradicts" in relations

    async def test_pick_contradiction_winner_equal_ordinals_keeps_both(self, heart, session, monkeypatch):
        """Codex r15: EQUAL same-episode ordinals (e.g. a re-extraction or
        backfill run assigning duplicate positional ordinals) carry no order
        signal. The prior `>=` comparison crowned new_fact on a tie, making
        resolution a coin-flip on call/UUID order -- exactly the corruption
        class Gate-1 D1 removes. Equal ordinals + equal learned_at (the
        natural same-session-transaction value) must fall through all the
        way to None (KEEP-BOTH + flag), not silently supersede."""
        monkeypatch.setattr(heart.facts, "_llm", MagicMock())  # truthy sentinel; classify is mocked below
        monkeypatch.setattr(
            heart.facts,
            "_classify_fact_pair",
            AsyncMock(return_value={"relation": "CONTRADICTION", "current_fact": "old", "confidence": 0.9}),
        )
        ep_id = await _insert_episode(session)
        anchor = _unit_vec(0)
        candidate = _embedding_at_similarity(0.88)  # inside the 0.80-0.95 F027 band

        old_r = await heart.learn(FactInput(
            content="The office thermostat is set to seventy two degrees fahrenheit.",
            subject="office thermostat",
            source_episode_id=ep_id, source_ordinal=5,
        ), session=session, precomputed_embedding=anchor)
        new_r = await heart.learn(FactInput(
            content="The office thermostat is set to sixty eight degrees fahrenheit.",
            subject="office thermostat",
            source_episode_id=ep_id, source_ordinal=5,  # SAME ordinal as old -- no order signal
        ), session=session, precomputed_embedding=candidate)

        await session.flush()
        old = await _get_fact(heart, old_r.id, session)
        new = await _get_fact(heart, new_r.id, session)
        assert old.active and new.active  # KEEP-BOTH -- equal ordinal is NOT an order signal
        assert new.contradiction_of == old.id

        edge_r = await session.execute(
            select(GraphEdge)
            .where(GraphEdge.source_id == new_r.id)
            .where(GraphEdge.target_id == old_r.id)
        )
        relations = {e.relation for e in edge_r.scalars().all()}
        assert "supersedes" not in relations
        assert "contradicts" in relations

    async def test_pick_contradiction_winner_equal_ordinals_falls_through_to_learned_at(self, heart, session, monkeypatch):
        """Codex r15 sibling: equal same-episode ordinals fall through to the
        learned_at tiebreak (not straight to None) when learned_at actually
        differs -- the later-stated fact still wins by genuine testimony
        order, just via the next signal in the precedence chain rather than
        position."""
        from datetime import UTC, datetime, timedelta

        from sqlalchemy import update as sa_update

        monkeypatch.setattr(heart.facts, "_llm", MagicMock())  # truthy sentinel; classify is mocked below
        monkeypatch.setattr(
            heart.facts,
            "_classify_fact_pair",
            AsyncMock(return_value={"relation": "CONTRADICTION", "current_fact": "old", "confidence": 0.9}),
        )
        ep_id = await _insert_episode(session)
        anchor = _unit_vec(0)
        candidate = _embedding_at_similarity(0.88)  # inside the 0.80-0.95 F027 band

        old_r = await heart.learn(FactInput(
            content="The office thermostat is set to seventy two degrees fahrenheit.",
            subject="office thermostat",
            source_episode_id=ep_id, source_ordinal=5,
        ), session=session, precomputed_embedding=anchor)

        # Backdate old_r so learned_at genuinely differs once the ordinal
        # tie falls through (same-session learns otherwise share a
        # transaction-constant "now()").
        await session.execute(
            sa_update(Fact).where(Fact.id == old_r.id).values(
                learned_at=datetime.now(UTC) - timedelta(hours=1)
            )
        )
        await session.flush()

        new_r = await heart.learn(FactInput(
            content="The office thermostat is set to sixty eight degrees fahrenheit.",
            subject="office thermostat",
            source_episode_id=ep_id, source_ordinal=5,  # SAME ordinal as old -- falls through
        ), session=session, precomputed_embedding=candidate)

        await session.flush()
        old = await _get_fact(heart, old_r.id, session)
        new = await _get_fact(heart, new_r.id, session)
        assert new.active and new.superseded_by is None  # later-learned WINS
        assert not old.active and old.superseded_by == new_r.id

        edge_r = await session.execute(
            select(GraphEdge)
            .where(GraphEdge.source_id == new_r.id)
            .where(GraphEdge.target_id == old_r.id)
        )
        relations = {e.relation for e in edge_r.scalars().all()}
        assert "supersedes" in relations

    async def test_keep_both_pair_excluded_from_post_insert_contradiction_scan(self, heart, session, monkeypatch):
        """Codex r11 FIX 1: a pair the legacy subject path just flagged
        KEEP-BOTH must not be visible to the post-insert _find_contradiction
        scan that runs immediately afterward in the same learn() call. Pre-
        fix, old.id was absent from safe_excludes, so _find_contradiction
        re-discovered the SAME pair (same 0.85-0.95 band) and re-ran its own
        inline CONTRADICTION handling — decrementing old.confidence a SECOND
        time (1.0 -> 0.8 -> 0.6) even though the pair was already flagged
        once by _supersede_by_subject."""
        monkeypatch.setattr(heart.facts, "_llm", MagicMock())  # truthy sentinel; classify is mocked below
        monkeypatch.setattr(
            heart.facts,
            "_classify_fact_pair",
            AsyncMock(return_value={"relation": "CONTRADICTION", "current_fact": "old", "confidence": 0.9}),
        )
        anchor = _unit_vec(0)
        candidate = _embedding_at_similarity(0.88)  # inside the 0.80-0.95 F027 band

        old_r = await heart.learn(FactInput(
            content="The office thermostat is set to seventy two degrees fahrenheit.",
            subject="office thermostat",
        ), session=session, precomputed_embedding=anchor)
        new_r = await heart.learn(FactInput(
            content="The office thermostat is set to sixty eight degrees fahrenheit.",
            subject="office thermostat",
        ), session=session, precomputed_embedding=candidate)

        await session.flush()
        old = await _get_fact(heart, old_r.id, session)
        new = await _get_fact(heart, new_r.id, session)
        assert old.active and new.active  # unordered -> KEEP-BOTH
        assert new.contradiction_of == old.id
        assert old.confidence == pytest.approx(0.8), (
            "old fact must be decremented exactly ONCE by the legacy KEEP-BOTH "
            "flag — a second decrement means the post-insert scan re-matched "
            "the same already-flagged pair"
        )

        edge_r = await session.execute(
            select(GraphEdge)
            .where(GraphEdge.source_id == new_r.id)
            .where(GraphEdge.target_id == old_r.id)
            .where(GraphEdge.relation == "contradicts")
        )
        assert len(edge_r.scalars().all()) == 1

    async def test_legacy_path_loss_skips_contradiction_scan(self, heart, session, monkeypatch):
        """Codex r11 FIX 2: mirrors test_resolve_key_conflicts_loss_skips_
        contradiction_scan for the LEGACY _supersede_by_subject path (R2
        off, the prod default). When the NEW fact loses statement-order
        resolution (lower source_ordinal, same episode as the existing
        fact), _find_contradiction must not fire against the now-inactive
        loser."""
        monkeypatch.setattr(heart.facts, "_llm", MagicMock())  # truthy sentinel; classify is mocked below
        monkeypatch.setattr(
            heart.facts,
            "_classify_fact_pair",
            AsyncMock(return_value={"relation": "CONTRADICTION", "current_fact": "old", "confidence": 0.9}),
        )
        ep_id = await _insert_episode(session)
        anchor = _unit_vec(0)
        candidate = _embedding_at_similarity(0.88)  # inside the 0.80-0.95 F027 band

        old_r = await heart.learn(FactInput(
            content="The office thermostat is set to seventy two degrees fahrenheit.",
            subject="office thermostat",
            source_episode_id=ep_id, source_ordinal=200,
        ), session=session, precomputed_embedding=anchor)

        # current_fact is ignored for CONTRADICTION -- old wins via the
        # LOWER ordinal on the new fact below, not this verdict.
        find_contradiction_sentinel = AsyncMock(return_value=None)
        monkeypatch.setattr(heart.facts, "_find_contradiction", find_contradiction_sentinel)

        new_r = await heart.learn(FactInput(
            content="The office thermostat is set to sixty eight degrees fahrenheit.",
            subject="office thermostat",
            source_episode_id=ep_id, source_ordinal=100,  # LOWER than old's 200 -> new fact loses
        ), session=session, precomputed_embedding=candidate)

        find_contradiction_sentinel.assert_not_called()

        await session.flush()
        new_row = await session.get(Fact, new_r.id)
        assert new_row.active is False or new_row.superseded_by is not None, (
            "new fact must be inactive after losing legacy order resolution"
        )


# Gate-1 D2 ───────────────────────────────────────────────────────────────────
# Same-slot value variants must route to conflict resolution, never
# dedup-confirm (F085 Gate-1 fixes plan, Task 3).


@pytest.mark.postgres_only
class TestGate1SameSlotDedupRouting:
    async def test_same_slot_value_variant_not_swallowed(self, heart, session, monkeypatch):
        """'author of X is A' then '...is B' with near-identical embeddings
        (similarity >= 0.95, the blind-confirm region): the update MUST be
        stored and adjudicated (later wins), never confirmed as a duplicate."""
        monkeypatch.setattr(  # distinct values -> DISTINCT
            "nous.heart.facts.FactManager.is_distinct_fact",
            AsyncMock(return_value=True),
        )
        monkeypatch.setattr(  # conflict classifier: UPDATE
            "nous.heart.facts.FactManager._classify_fact_pair",
            AsyncMock(return_value={"relation": "UPDATE", "current_fact": "new", "confidence": 0.9}),
        )
        s = heart.facts._settings.model_copy(update={"supersession_key_resolution_enabled": True})
        monkeypatch.setattr(heart.facts, "_settings", s)
        ep_id = await _insert_episode(session)
        vec = _unit_vec(20)  # SAME embedding both times -> similarity 1.0
        r1 = await heart.learn(FactInput(
            content="The author of Work X is Alice Prior.",
            subject_key="work x", attribute_key="author",
            source_episode_id=ep_id, source_ordinal=1,
        ), session=session, precomputed_embedding=vec)
        r2 = await heart.learn(FactInput(
            content="The author of Work X is Bob Later.",
            subject_key="work x", attribute_key="author",
            source_episode_id=ep_id, source_ordinal=2,
        ), session=session, precomputed_embedding=vec)
        assert r2.id != r1.id                       # NOT dedup-confirmed
        old = await _get_fact(heart, r1.id, session)
        new = await _get_fact(heart, r2.id, session)
        assert new.active and not old.active and old.superseded_by == new.id

    async def test_same_slot_same_value_still_dedups(self, heart, session):
        """Identical folded content on a same-slot pair confirms as a
        duplicate via the fold-compare prefilter alone — no LLM call."""
        vec = _unit_vec(21)
        r1 = await heart.learn(FactInput(
            content="The server region is set to us-east-1.",
            subject_key="server", attribute_key="region",
        ), session=session, precomputed_embedding=vec)
        r2 = await heart.learn(FactInput(
            content="The server region is set to us-east-1.",
            subject_key="server", attribute_key="region",
        ), session=session, precomputed_embedding=vec)
        assert r2.id == r1.id

    async def test_unkeyed_pair_keeps_todays_dedup(self, heart, session):
        """Neither side carries subject_key/attribute_key — the guard's
        keyed gate is unsatisfied and today's confirm-on-match behavior
        is unchanged."""
        vec = _unit_vec(22)
        r1 = await heart.learn(FactInput(
            content="The office plant needs watering twice a week.",
        ), session=session, precomputed_embedding=vec)
        r2 = await heart.learn(FactInput(
            content="The office plant needs watering three times a week.",
        ), session=session, precomputed_embedding=vec)
        assert r2.id == r1.id

    async def test_guard_fails_open_to_store_on_classifier_outage(self, heart, session, monkeypatch):
        """is_distinct_fact -> None (LLM down): a differing-content
        same-slot pair MUST STORE (keep-both), never confirm as a dup."""
        monkeypatch.setattr(
            "nous.heart.facts.FactManager.is_distinct_fact",
            AsyncMock(return_value=None),
        )
        vec = _unit_vec(23)
        r1 = await heart.learn(FactInput(
            content="The meeting room capacity is twelve people.",
            subject_key="meeting room", attribute_key="capacity",
        ), session=session, precomputed_embedding=vec)
        r2 = await heart.learn(FactInput(
            content="The meeting room capacity is twenty people.",
            subject_key="meeting room", attribute_key="capacity",
        ), session=session, precomputed_embedding=vec)
        assert r2.id != r1.id
        old = await _get_fact(heart, r1.id, session)
        new = await _get_fact(heart, r2.id, session)
        assert old.active and new.active

    async def test_kill_switch_restores_legacy_swallow(self, heart, session, monkeypatch):
        """Kill switch OFF restores the legacy blind-confirm at
        similarity >= 0.95 even for a same-slot value-variant pair."""
        s = heart.facts._settings.model_copy(update={"same_slot_conflict_routing_enabled": False})
        monkeypatch.setattr(heart.facts, "_settings", s)
        vec = _unit_vec(24)
        r1 = await heart.learn(FactInput(
            content="The API rate limit is set to one hundred requests per minute.",
            subject_key="api", attribute_key="rate_limit",
        ), session=session, precomputed_embedding=vec)
        r2 = await heart.learn(FactInput(
            content="The API rate limit is set to five hundred requests per minute.",
            subject_key="api", attribute_key="rate_limit",
        ), session=session, precomputed_embedding=vec)
        assert r2.id == r1.id

    async def test_routed_dupe_excluded_from_admission_novelty_only(self, heart, session, monkeypatch):
        """Codex r2: a same-slot value-variant routed past dedup (Gate-1 D2)
        is definitionally near-identical to the dupe it was routed against
        (that's why it hit the >= fact_native_cosine_threshold dedup check).
        Leaving it visible to _find_max_similarity's novelty computation
        would collapse the candidate's novelty term to ~0 and let admission
        REJECT the very pair R2 must adjudicate -- reintroducing a drop path
        this fix was meant to close. Verify the seam directly (cheaper and
        more reliable than tuning real admission scores to straddle the
        composite threshold, per review guidance): capture the exclude_ids
        _find_max_similarity actually receives and confirm it includes the
        routed dupe's id, while R2 still successfully adjudicates the pair
        (proving exclude_ids feeding _resolve_key_conflicts is untouched --
        Amendment 1 preserved)."""
        from nous.heart.admission import AdmissionConfig, AdmissionController
        from nous.heart.schemas import FactRejected

        monkeypatch.setattr(
            "nous.heart.facts.FactManager.is_distinct_fact",
            AsyncMock(return_value=True))
        monkeypatch.setattr(
            "nous.heart.facts.FactManager._classify_fact_pair",
            AsyncMock(return_value={"relation": "UPDATE", "current_fact": "new", "confidence": 0.9}))
        s = heart.facts._settings.model_copy(update={"supersession_key_resolution_enabled": True})
        monkeypatch.setattr(heart.facts, "_settings", s)

        # Real (heuristic-only, no LLM) admission controller so the novelty
        # call actually fires -- mirrors heart_with_admission's conftest recipe.
        heart.facts._admission_controller = AdmissionController(
            config=AdmissionConfig(shadow_mode=False, utility_llm_enabled=False)
        )

        captured_excludes: list[list] = []
        original_find_max = heart.facts._find_max_similarity

        async def _capturing_find_max_similarity(embedding, exclude_ids, session):
            captured_excludes.append(list(exclude_ids))
            return await original_find_max(embedding, exclude_ids, session)

        monkeypatch.setattr(heart.facts, "_find_max_similarity", _capturing_find_max_similarity)

        ep_id = await _insert_episode(session)
        vec = _unit_vec(25)
        r1 = await heart.learn(FactInput(
            content="The author of Work Y is Carla First.",
            subject_key="work y", attribute_key="author",
            source_episode_id=ep_id, source_ordinal=1,
        ), session=session, precomputed_embedding=vec)
        r2 = await heart.learn(FactInput(
            content="The author of Work Y is Derek Later.",
            subject_key="work y", attribute_key="author",
            source_episode_id=ep_id, source_ordinal=2,
        ), session=session, precomputed_embedding=vec)

        # The admission novelty call for the SECOND learn (the one that hit
        # dedup and got routed past it) must have received the routed
        # dupe's id in its excludes -- otherwise max_sim ~= 1.0 and novelty
        # collapses to ~0.
        assert len(captured_excludes) == 2
        assert r1.id in captured_excludes[-1]

        # The candidate was NOT admission-rejected, and R2 still adjudicates
        # the pair (later ordinal wins) -- proves exclude_ids feeding
        # _resolve_key_conflicts was not also polluted by this change.
        assert not isinstance(r2, FactRejected)
        old = await _get_fact(heart, r1.id, session)
        new = await _get_fact(heart, r2.id, session)
        assert new.active and not old.active and old.superseded_by == new.id

    async def test_routed_dupe_cluster_excludes_all_active_same_slot_facts(self, heart, session, monkeypatch):
        """Codex r4/r5: the cluster extension of r2. When MORE than one
        active same-slot fact exists, admission's novelty exclusion must
        cover the WHOLE conflict-slot cluster, not just the single routed
        dupe -- other non-routed same-slot variants (ones _find_duplicate's
        cosine threshold never flagged, because their embeddings differ) can
        still be the nearest neighbor _find_max_similarity finds, collapsing
        novelty to ~0 and getting the correction admission-rejected before R2
        ever adjudicates the cluster.

        Codex r5: the exclusion must NOT be capped by
        supersession_key_candidates_cap -- that cap bounds R2's ADJUDICATION
        reach (which pairs get resolved), but admission runs BEFORE storage;
        an older cluster member outside the cap would still zero novelty and
        get the candidate rejected. Set the cap to 2 and seed cap+2 (4)
        active same-slot facts to prove the admission exclusion is uncapped."""
        from nous.heart.admission import AdmissionConfig, AdmissionController

        monkeypatch.setattr(
            "nous.heart.facts.FactManager.is_distinct_fact",
            AsyncMock(return_value=True))
        monkeypatch.setattr(
            "nous.heart.facts.FactManager._classify_fact_pair",
            AsyncMock(return_value={"relation": "UPDATE", "current_fact": "new", "confidence": 0.9}))
        s = heart.facts._settings.model_copy(update={"supersession_key_candidates_cap": 2})
        monkeypatch.setattr(heart.facts, "_settings", s)

        heart.facts._admission_controller = AdmissionController(
            config=AdmissionConfig(shadow_mode=False, utility_llm_enabled=False)
        )

        captured_excludes: list[list] = []
        original_find_max = heart.facts._find_max_similarity

        async def _capturing_find_max_similarity(embedding, exclude_ids, session):
            captured_excludes.append(list(exclude_ids))
            return await original_find_max(embedding, exclude_ids, session)

        monkeypatch.setattr(heart.facts, "_find_max_similarity", _capturing_find_max_similarity)

        ep_id = await _insert_episode(session)
        vec_first = _unit_vec(26)
        cluster_names = ["Alice Prior", "Bob Middle", "Dana Second", "Erin Third"]
        cluster_facts = []
        for i, name in enumerate(cluster_names):
            fact = await heart.learn(FactInput(
                content=f"The project lead is {name}.",
                subject_key="project", attribute_key="lead",
                source_episode_id=ep_id, source_ordinal=i + 1,
            ), session=session, precomputed_embedding=(vec_first if i == 0 else _unit_vec(27 + i)))
            cluster_facts.append(fact)

        # C shares the FIRST cluster member's embedding exactly -> only that
        # one is routed by _find_duplicate. cap=2 means R2 would only
        # adjudicate 2 of the 4 cluster members, but admission must exclude
        # ALL 4 -- it protects storage, not adjudication.
        await heart.learn(FactInput(
            content="The project lead is Carol Later.",
            subject_key="project", attribute_key="lead",
            source_episode_id=ep_id, source_ordinal=5,
        ), session=session, precomputed_embedding=vec_first)

        for fact in cluster_facts:
            assert fact.id in captured_excludes[-1], (
                f"cluster member {fact.id} missing from admission excludes "
                f"(cap={s.supersession_key_candidates_cap} must not limit this)"
            )

    async def test_same_slot_routing_honors_check_contradictions_false(self, heart, session, monkeypatch):
        """Codex r6: when check_contradictions=False (bulk-import/sentinel
        callers), _resolve_key_conflicts, the legacy _supersede_by_subject
        path, AND _find_contradiction are ALL gated on that same flag. If the
        D2 guard still routed a same-slot value-variant past confirm in this
        mode, the routed pair would be inserted with NO resolver ever
        adjudicating it -- unadjudicated near-dupes accumulating silently.
        The guard must instead take the legacy confirm path exactly as
        pre-D2-fix, and must not spend the is_distinct_fact budget/LLM call
        doing so."""
        is_distinct_mock = AsyncMock(return_value=True)
        monkeypatch.setattr(
            "nous.heart.facts.FactManager.is_distinct_fact",
            is_distinct_mock)

        vec = _unit_vec(28)
        r1 = await heart.facts.learn(FactInput(
            content="The office thermostat is set to seventy degrees.",
            subject_key="office", attribute_key="thermostat",
        ), session=session, precomputed_embedding=vec, check_contradictions=False)
        r2 = await heart.facts.learn(FactInput(
            content="The office thermostat is set to sixty five degrees.",
            subject_key="office", attribute_key="thermostat",
        ), session=session, precomputed_embedding=vec, check_contradictions=False)

        assert r2.id == r1.id  # legacy confirm, not routed past dedup
        is_distinct_mock.assert_not_awaited()

    async def test_find_duplicate_prefers_same_slot_over_closer_cross_slot(self, heart, session, monkeypatch):
        """Codex r7: _find_duplicate returns exactly ONE candidate, and the
        D2 guard can only route what that selector surfaces. Seed A in slot
        (book one, author) at cosine 0.97 to the incoming candidate C, and B
        in a DIFFERENT slot (book two, author) at cosine 0.99 to C (closer,
        but unrelated). A and B sit on OPPOSITE sides of C along the same
        orthogonal axis, so their MUTUAL similarity (~0.926) stays below the
        0.95 dedup threshold -- seeding them independently must not blind-
        merge them into each other (that would confound the test before C is
        even introduced). Without a same-slot preference, _find_duplicate
        returns the closer B and the guard can't fire (subject_key
        mismatch), so C -- a genuine value-variant of A -- would blind-
        confirm into the unrelated B. Post-fix, A is preferred and C is
        routed + adjudicated against it.

        Codex r10 (admission seam variant): B is a row _find_duplicate
        itself examined and judged above the dedup threshold -- exactly as
        close to C as the routed dupe A -- but r7's slot narrowing skips it.
        Round-4's same-slot admission query can't see B either (different
        slot). B must still land in the admission novelty exclusion via
        probed_ids, or it could zero C's novelty term and get it
        admission-rejected before R2 ever adjudicates it."""
        import math

        from nous.heart.admission import AdmissionConfig, AdmissionController

        heart.facts._admission_controller = AdmissionController(
            config=AdmissionConfig(shadow_mode=False, utility_llm_enabled=False)
        )
        captured_excludes: list[list] = []
        original_find_max = heart.facts._find_max_similarity

        async def _capturing_find_max_similarity(embedding, exclude_ids, session):
            captured_excludes.append(list(exclude_ids))
            return await original_find_max(embedding, exclude_ids, session)

        monkeypatch.setattr(heart.facts, "_find_max_similarity", _capturing_find_max_similarity)

        ep_id = await _insert_episode(session)
        vec_c = _unit_vec(0)
        vec_a = [0.0] * 1536
        vec_a[0] = 0.97
        vec_a[1] = math.sqrt(1.0 - 0.97 ** 2)
        vec_b = [0.0] * 1536
        vec_b[0] = 0.99
        vec_b[1] = -math.sqrt(1.0 - 0.99 ** 2)  # opposite sign -> low sim(A, B)

        # source_text=<own content> keeps the ROUGE-L confidence dimension
        # near 1.0 now that a real AdmissionController is wired (the round-7
        # version of this test predates admission scoring and had no
        # controller attached) -- without it, _get_source_text falls back to
        # _insert_episode's generic, content-unrelated summary and ROUGE-L
        # collapses confidence to 0.0, dropping the composite below the
        # 0.55 threshold and rejecting the fact outright.
        a_content = "The author of Book One is Alice Prior."
        b_content = "The author of Book Two is Bob Middle."
        c_content = "The author of Book One is Carol Later."
        a = await heart.learn(FactInput(
            content=a_content, source_text=a_content,
            subject_key="book one", attribute_key="author",
            source_episode_id=ep_id, source_ordinal=1,
        ), session=session, precomputed_embedding=vec_a)
        b = await heart.learn(FactInput(
            content=b_content, source_text=b_content,
            subject_key="book two", attribute_key="author",  # DIFFERENT slot
            source_episode_id=ep_id, source_ordinal=2,
        ), session=session, precomputed_embedding=vec_b)
        assert a.id != b.id  # sanity: seeding did not blind-merge A and B

        # Sanity / unchanged-legacy-path assertion: with no prefer_slot
        # (flag off / not set), _find_duplicate still returns the CLOSER
        # cross-slot fact (B, sim 0.99) over the same-slot fact (A, sim 0.97).
        legacy = await heart.facts._find_duplicate(vec_c, [], session)
        assert legacy is not None
        assert legacy[0].id == b.id

        monkeypatch.setattr(
            "nous.heart.facts.FactManager.is_distinct_fact",
            AsyncMock(return_value=True))
        monkeypatch.setattr(
            "nous.heart.facts.FactManager._classify_fact_pair",
            AsyncMock(return_value={"relation": "UPDATE", "current_fact": "new", "confidence": 0.9}))
        s = heart.facts._settings.model_copy(update={"supersession_key_resolution_enabled": True})
        monkeypatch.setattr(heart.facts, "_settings", s)

        c = await heart.learn(FactInput(
            content=c_content, source_text=c_content,
            subject_key="book one", attribute_key="author",  # SAME slot as A
            source_episode_id=ep_id, source_ordinal=3,
        ), session=session, precomputed_embedding=vec_c)

        # Codex r10: check the admission-exclusion MECHANISM first,
        # independent of C's eventual admit/reject outcome -- pre-fix, B (an
        # above-threshold row slot narrowing skipped) is missing from the
        # excludes, which can (and here does) get C admission-REJECTED
        # outright; asserting on c.id first would mask this exact-mechanism
        # check behind an AttributeError on FactRejected.
        assert b.id in captured_excludes[-1]

        # Post-fix: C is routed + adjudicated against A (same slot, later
        # ordinal wins) -- NOT blind-confirmed into B.
        assert c.id != a.id and c.id != b.id
        old = await _get_fact(heart, a.id, session)
        new = await _get_fact(heart, c.id, session)
        assert new.active and not old.active and old.superseded_by == new.id
        # B is untouched -- it was never in C's conflict slot.
        unrelated = await _get_fact(heart, b.id, session)
        assert unrelated.active

    async def test_date_match_outranks_same_slot_narrowing_in_duplicate_selection(self, heart, session):
        """Codex r9: F075's date-match preference must outrank r7's
        same-slot narrowing. Migration shape: a legacy, UNKEYED fact L
        already represents the March-10 event; same-slot fact S represents
        a DIFFERENT (March-12) event and is CLOSER in cosine distance; an
        incoming keyed candidate C is a March-10 duplicate whose embedding
        clears the dedup threshold against both. Slot narrowing must NOT
        pick S -- that would trip the F075 distinct-event bypass and INSERT
        a second row for the same March-10 event instead of confirming
        against L, which already claims that event regardless of slot."""
        import math
        from datetime import date

        vec_c = _unit_vec(0)
        vec_l = [0.0] * 1536
        vec_l[0] = 0.97
        vec_l[1] = math.sqrt(1.0 - 0.97 ** 2)
        vec_s = [0.0] * 1536
        vec_s[0] = 0.99
        vec_s[1] = -math.sqrt(1.0 - 0.99 ** 2)  # opposite sign -> low mutual sim(L, S)

        l = await heart.learn(FactInput(
            content="The office received a security audit on March tenth.",
            event_date=date(2026, 3, 10),
        ), session=session, precomputed_embedding=vec_l)
        s = await heart.learn(FactInput(
            content="The office received a security audit on March twelfth.",
            subject_key="office", attribute_key="security_audit",
            event_date=date(2026, 3, 12),
        ), session=session, precomputed_embedding=vec_s)
        assert l.id != s.id  # sanity: seeding did not blind-merge L and S

        c = await heart.learn(FactInput(
            content="The office received a security audit on March tenth.",
            subject_key="office", attribute_key="security_audit",
            event_date=date(2026, 3, 10),
        ), session=session, precomputed_embedding=vec_c)

        # Post-fix: the date match on L wins outright -- C confirms into L,
        # no new row for the same March-10 event.
        assert c.id == l.id

    async def test_null_date_match_outranks_same_slot_narrowing_when_candidate_undated(self, heart, session):
        """Codex r11 FIX 3: r9's date-match preference only fired when
        candidate_event_date is not None, so an UNDATED candidate with a
        genuinely undated (NULL==NULL) duplicate elsewhere in the above-
        threshold set could still be masked by a DATED same-slot variant.
        Migration shape: a legacy, unkeyed fact L is the true (undated)
        duplicate; same-slot fact S claims a specific (unrelated) date and
        is CLOSER in cosine distance. Slot narrowing must not pick S over
        L just because the candidate itself carries no date to disqualify
        S outright — S's own specific date, with no counterpart on the
        undated candidate, is not a confirmable match."""
        import math
        from datetime import date

        vec_c = _unit_vec(0)
        vec_l = [0.0] * 1536
        vec_l[0] = 0.97
        vec_l[1] = math.sqrt(1.0 - 0.97 ** 2)
        vec_s = [0.0] * 1536
        vec_s[0] = 0.99
        vec_s[1] = -math.sqrt(1.0 - 0.99 ** 2)  # opposite sign -> low mutual sim(L, S)

        l = await heart.learn(FactInput(
            content="The office thermostat schedule was reset to factory defaults.",
        ), session=session, precomputed_embedding=vec_l)  # legacy, unkeyed, undated
        s = await heart.learn(FactInput(
            content="The office thermostat schedule changed for the holiday season.",
            subject_key="office", attribute_key="thermostat_schedule",
            event_date=date(2026, 3, 12),
        ), session=session, precomputed_embedding=vec_s)  # same slot as C, but DATED
        assert l.id != s.id  # sanity: seeding did not blind-merge L and S

        c = await heart.learn(FactInput(
            content="The office thermostat schedule was reset to factory defaults.",
            subject_key="office", attribute_key="thermostat_schedule",
        ), session=session, precomputed_embedding=vec_c)  # undated candidate, same slot as S

        # Post-fix: L (the true undated duplicate) wins outright over S (a
        # date-incompatible same-slot variant) -- C confirms into L.
        assert c.id == l.id


# codex P2 round 7: subject_key/attribute_key canonicalized at the FactInput
# boundary ──────────────────────────────────────────────────────────────────

@pytest.mark.postgres_only
async def test_r7_raw_form_key_matches_canonical_form_for_r2(heart, session, monkeypatch):
    """codex P2 round 7: R2's same-key lookup is exact-string equality
    (facts.py: ``Fact.subject_key == input.subject_key``). A direct
    Heart.learn caller storing subject_key/attribute_key in RAW form
    ("api_gateway"/"The_Owner") must still exact-match another caller's
    canonical form ("api gateway"/"owner") — without normalizing at the
    FactInput boundary, this conflict was silently missed (the two rows
    never looked like the same slot to R2 at all).
    """
    heart.facts._settings = heart.facts._settings.model_copy(
        update={"supersession_key_resolution_enabled": True}
    )
    ep_id = await _insert_episode(session)
    monkeypatch.setattr(heart.facts, "_classify_fact_pair", AsyncMock(return_value=_UPDATE_NEW))

    old_r = await heart.learn(
        FactInput(
            content="The api gateway owner is listed as the platform team lead.",
            subject_key="api_gateway",  # RAW form
            attribute_key="The_Owner",  # RAW form
            source_ordinal=1,
            source_episode_id=ep_id,
        ),
        session=session,
    )
    new_r = await heart.learn(
        FactInput(
            content="The api gateway owner is listed as the infrastructure team lead.",
            subject_key="api gateway",  # canonical form
            attribute_key="owner",      # canonical form
            source_ordinal=2,
            source_episode_id=ep_id,
        ),
        session=session,
    )
    await session.flush()

    old_row = await session.get(Fact, old_r.id)
    new_row = await session.get(Fact, new_r.id)
    # Both rows normalize to the same slot, so R2 finds the conflict and the
    # mocked classifier verdict (UPDATE, current=new, higher ordinal wins)
    # resolves it — old superseded by new.
    assert old_row.subject_key == "api gateway"
    assert old_row.attribute_key == "owner"
    assert new_row.subject_key == "api gateway"
    assert new_row.attribute_key == "owner"
    assert old_row.superseded_by == new_r.id
    assert old_row.active is False
    assert new_row.active is True


# AC-5: supersession-cycle guard in _get_current ─────────────────────────────

@pytest.mark.postgres_only
async def test_get_current_breaks_supersession_cycle(heart, session):
    """AC-5: A→B→A cycle — get_current(A) returns later-learned B, cycle broken persistently."""
    from datetime import timedelta

    a = await heart.learn(
        FactInput(content="Fact A: the project deadline is the end of this month."),
        session=session,
    )
    b = await heart.learn(
        FactInput(content="Fact B: the project deadline has been pushed to next quarter."),
        session=session,
    )

    # Manufacture a cycle: A.superseded_by=B, B.superseded_by=A
    a_row = await session.get(Fact, a.id)
    b_row = await session.get(Fact, b.id)
    # Make B's learned_at strictly later so the cycle-guard picks B as winner.
    b_row.learned_at = a_row.learned_at + timedelta(seconds=10)
    a_row.superseded_by = b.id
    b_row.superseded_by = a.id
    await session.flush()

    # (a) Must not raise
    result = await heart.facts._get_current(a.id, session)

    # (b) Returns the later-learned fact (B)
    assert result.id == b.id

    # (c) B's cycle is broken persistently: superseded_by IS NULL, active=True
    await session.refresh(b_row)
    assert b_row.superseded_by is None
    assert b_row.active is True


@pytest.mark.postgres_only
async def test_get_current_healthy_chain_unaffected(heart, session):
    """AC-5 regression: A→B (no cycle) → get_current(A) returns B, cycle guard does not fire."""
    a = await heart.learn(
        FactInput(content="Healthy chain A: Alice used to work at the downtown branch."),
        session=session,
    )
    b = await heart.learn(
        FactInput(content="Healthy chain B: Alice now works at the uptown headquarters."),
        session=session,
    )

    a_row = await session.get(Fact, a.id)
    b_row = await session.get(Fact, b.id)
    a_row.superseded_by = b.id
    b_row.superseded_by = None
    await session.flush()

    result = await heart.facts._get_current(a.id, session)

    assert result.id == b.id
    # B must remain unchanged: superseded_by still None, active still True
    await session.refresh(b_row)
    assert b_row.superseded_by is None
    assert b_row.active is True


@pytest.mark.postgres_only
async def test_get_current_cycle_break_durable_without_session(heart):
    """AC-5 durability: cycle repair via session-less get_current is committed to DB.

    Creates A and B in committed state (heart.learn without session auto-commits),
    manufactures a cycle in a separate committed transaction, calls get_current
    without passing a session, then opens a FRESH session to confirm the repair
    was durably written — not just flushed inside a rolled-back transaction.
    """
    from datetime import timedelta

    # Step 1: create facts in committed state (no session → heart auto-commits)
    a = await heart.learn(
        FactInput(content="Durability cycle test A: the server cluster has four active nodes running.")
    )
    b = await heart.learn(
        FactInput(content="Durability cycle test B: the server cluster now has eight active nodes running.")
    )

    # Step 2: manufacture a cycle and commit it in a dedicated transaction
    async with heart.db.session() as s:
        a_row = await s.get(Fact, a.id)
        b_row = await s.get(Fact, b.id)
        # Make B's learned_at strictly later so the cycle-guard picks B as winner
        b_row.learned_at = a_row.learned_at + timedelta(seconds=10)
        a_row.superseded_by = b.id
        b_row.superseded_by = a.id
        await s.commit()

    # Step 3: call get_current WITHOUT a session — repair must be committed
    result = await heart.facts.get_current(a.id)
    assert result.id == b.id  # B is later-learned, wins

    # Step 4: verify repair is durable via a completely FRESH session
    async with heart.db.session() as s:
        fresh_b = await s.get(Fact, b.id)
        assert fresh_b is not None
        assert fresh_b.superseded_by is None
        assert fresh_b.active is True


@pytest.mark.postgres_only
async def test_get_current_long_chain_not_rewritten(heart, session):
    """Path-based cycle detection: a legitimate 12-link chain must NOT be
    mutated by get_current.  Only true cycles trigger the fallback repair.

    Chain: A1 → A2 → A3 → ... → A12 (tip, superseded_by=NULL, active=True).
    All intermediate nodes: superseded_by set, active=False.
    """
    n = 12
    facts = []
    for i in range(n):
        f = await heart.learn(
            FactInput(
                content=f"Long chain link {i + 1} of {n}: the deployment revision is v{i + 1}.0.0."
            ),
            session=session,
        )
        facts.append(f)

    # Wire the chain: A1.superseded_by=A2, A2.superseded_by=A3, …, A11.superseded_by=A12
    rows = [await session.get(Fact, f.id) for f in facts]
    for i, row in enumerate(rows[:-1]):
        row.superseded_by = facts[i + 1].id
        row.active = False
    rows[-1].superseded_by = None
    rows[-1].active = True
    await session.flush()

    # get_current from the head must reach the tip without any fallback
    result = await heart.facts._get_current(facts[0].id, session)
    assert result.id == facts[-1].id, (
        f"Expected tip {facts[-1].id}, got {result.id}"
    )

    # Every intermediate row must be untouched (superseded_by and active unchanged)
    for i, row in enumerate(rows[:-1]):
        await session.refresh(row)
        assert row.superseded_by == facts[i + 1].id, (
            f"Intermediate link {i + 1} had superseded_by mutated"
        )
        assert row.active is False, (
            f"Intermediate link {i + 1} was reactivated"
        )


@pytest.mark.postgres_only
async def test_get_current_deep_chain_no_repair(heart, session):
    """FIX 5 (codex r5): iterative walk returns true tip for chains >100 links.

    A 120-link acyclic chain (facts[0]→facts[1]→…→facts[119]) exceeds the
    depth<100 CTE backstop. The iterative walk restarts from the deepest visited
    row each iteration, converging at facts[119] (the true NULL-tip). No rows
    must be mutated (acyclic chain → no cycle repair).
    """
    n = 120
    facts = []
    for i in range(n):
        f = await heart.learn(
            FactInput(
                content=(
                    f"Deep chain link {i + 1} of {n}: the migration "
                    f"revision counter is now at step {i + 1}."
                )
            ),
            session=session,
        )
        facts.append(f)

    # Wire the chain: facts[0] → facts[1] → … → facts[119]
    rows = [await session.get(Fact, f.id) for f in facts]
    for i, row in enumerate(rows[:-1]):
        row.superseded_by = facts[i + 1].id
        row.active = False
    rows[-1].superseded_by = None
    rows[-1].active = True
    await session.flush()

    result = await heart.facts._get_current(facts[0].id, session)

    # Iterative walk must reach the true tip (facts[119])
    assert result.id == facts[119].id, (
        f"Expected true tip facts[119]={facts[119].id}, got {result.id}"
    )

    # No intermediate row must have been mutated (acyclic chain → no repair)
    for i, row in enumerate(rows[:-1]):
        await session.refresh(row)
        assert row.superseded_by == facts[i + 1].id, (
            f"Row {i} had superseded_by mutated unexpectedly"
        )
        assert row.active is False, (
            f"Row {i} was reactivated unexpectedly"
        )


# ---------------------------------------------------------------------------
# Codex r15 FIX 2: cycle repair selects winner over full visited set
# ---------------------------------------------------------------------------

@pytest.mark.postgres_only
async def test_get_current_long_cycle_winner_from_full_visited_set(heart, session):
    """R15 FIX 2: 150-node cycle where max learned_at is at index ~120 (past the
    first 100-depth CTE window).  get_current(facts[0]) must return facts[120] —
    the true latest-learned — and break the cycle there.

    Chain: facts[0] → facts[1] → … → facts[149] → facts[0] (cycle).
    learned_at: facts[120] has the globally maximum value; all others keep
    their natural insert ordering; facts[120] jumps to base + 200 days.

    The iterative walk accumulates restart endpoints in visited.  With step=98,
    d(k)=(k*98) mod 150 generates all even values; d(15)=120, so facts[120]
    IS a restart endpoint and appears in visited before cycle detection fires.
    The fix queries the full visited set for the winner; without it, _winner_sql
    starts at current_id near the detection point and misses facts[120].
    """
    from datetime import timedelta

    n = 150
    facts = []
    for i in range(n):
        f = await heart.learn(
            FactInput(
                content=(
                    f"Long cycle link {i + 1} of {n}: the index counter "
                    f"is at position {i + 1} in the rotation sequence."
                )
            ),
            session=session,
        )
        facts.append(f)

    # Wire the cycle: facts[0]→facts[1]→…→facts[149]→facts[0]
    rows = [await session.get(Fact, f.id) for f in facts]
    for i, row in enumerate(rows):
        row.superseded_by = facts[(i + 1) % n].id
        row.active = False

    # Set facts[120]'s learned_at to be the globally maximum value.
    # All others keep their natural insert ordering.
    base_time = rows[0].learned_at
    rows[120].learned_at = base_time + timedelta(days=200)

    await session.flush()

    # get_current must return facts[120] (the true latest-learned)
    result = await heart.facts._get_current(facts[0].id, session)
    assert result.id == facts[120].id, (
        f"Expected facts[120]={facts[120].id}, got {result.id} — "
        "cycle repair must select winner from the full visited set"
    )

    # Cycle must be broken at facts[120]: superseded_by=None, active=True
    await session.refresh(rows[120])
    assert rows[120].superseded_by is None, "winner's superseded_by must be cleared"
    assert rows[120].active is True, "winner must be reactivated"


# ---------------------------------------------------------------------------
# Task 9: sleep-phase key-conflict sweep (find_key_conflict_pairs,
#         resolve_key_conflict_pair, _phase_sweep_key_conflicts)
# ---------------------------------------------------------------------------


@pytest.mark.postgres_only
async def test_find_key_conflict_pairs_cross_episode(heart):
    """(a) Two same-key active facts from *different* episodes are found by
    find_key_conflict_pairs; facts WITHOUT keys are not returned."""
    # Fact with keys — the pair that should surface
    old_f = await heart.learn(
        FactInput(
            content="The database cluster has twelve active shards running in production.",
            subject_key="database",
            attribute_key="shard_count",
        )
    )
    new_f = await heart.learn(
        FactInput(
            content="The database cluster now has twenty-four active shards running in production.",
            subject_key="database",
            attribute_key="shard_count",
        )
    )
    # Fact WITHOUT keys — must NOT appear in results
    await heart.learn(
        FactInput(content="A completely unrelated observation about the weather today.")
    )

    pairs = await heart.facts.find_key_conflict_pairs(limit=25)

    # Both ids must appear together (ordering may vary at sub-ms learned_at resolution)
    pair_id_sets = [{str(p["id1"]), str(p["id2"])} for p in pairs]
    assert {str(old_f.id), str(new_f.id)} in pair_id_sets, (
        f"Expected {{{old_f.id}, {new_f.id}}} not found in {pair_id_sets}"
    )
    # Ensure no pair has a None id1 or id2 (i.e., keyless facts not included)
    for p in pairs:
        assert p["id1"] is not None
        assert p["id2"] is not None
        assert p["c1"] is not None
        assert p["c2"] is not None


@pytest.mark.postgres_only
async def test_phase_sweep_key_conflicts_resolves_and_increments_counters(heart):
    """(b) The phase resolves a same-key pair via classifier-confirm + policy
    and increments both key_conflicts_found and key_supersessions_written."""
    from nous.handlers.sleep_handler import SleepHandler
    from nous.heart.schemas import FactInput

    # Persist two same-key facts (committed, so a fresh session sees both)
    old_f = await heart.learn(
        FactInput(
            content="The API gateway processes one thousand requests per second at peak load.",
            subject_key="api_gateway",
            attribute_key="peak_rps",
        )
    )
    new_f = await heart.learn(
        FactInput(
            content="The API gateway now processes two thousand requests per second at peak load.",
            subject_key="api_gateway",
            attribute_key="peak_rps",
        )
    )

    # Enable the flag and wire a mock classifier that confirms UPDATE (new wins)
    settings = heart.facts._settings.model_copy(
        update={
            "supersession_key_resolution_enabled": True,
            "supersession_sweep_max_pairs": 25,
            "supersession_classifier_max_per_hour": 500,
        }
    )
    heart.facts._settings = settings

    heart.facts._classify_fact_pair = AsyncMock(
        return_value={"relation": "UPDATE", "current_fact": "new", "confidence": 0.95}
    )

    # Build a minimal SleepHandler with a mock LLM so it doesn't skip
    llm_mock = MagicMock()
    handler = SleepHandler(
        brain=AsyncMock(),
        heart=heart,
        settings=settings,
        bus=MagicMock(),
        llm_client=llm_mock,
    )
    handler._llm = llm_mock  # ensure it's set on the handler too
    handler._interrupted = False

    sleep_stats: dict = {}
    result = await handler._phase_sweep_key_conflicts(sleep_stats)

    assert result is True
    assert sleep_stats.get("key_conflicts_found", 0) >= 1
    assert sleep_stats.get("key_supersessions_written", 0) >= 1

    # Exactly one fact must have been superseded (the loser); chain must be valid
    async with heart.db.session() as s:
        old_row = await s.get(Fact, old_f.id)
        new_row = await s.get(Fact, new_f.id)
        assert old_row is not None and new_row is not None
        active_count = (1 if old_row.active else 0) + (1 if new_row.active else 0)
        assert active_count == 1, (
            f"Expected exactly 1 active fact, got old.active={old_row.active} new.active={new_row.active}"
        )
        loser = old_row if not old_row.active else new_row
        winner = new_row if not old_row.active else old_row
        assert loser.superseded_by == winner.id


@pytest.mark.asyncio
async def test_phase_sweep_key_conflicts_flag_off_returns_immediately():
    """(c) Flag off ⇒ phase returns True immediately with zero queries (golden).
    find_key_conflict_pairs must NOT be called at all."""
    from nous.handlers.sleep_handler import SleepHandler

    heart_mock = AsyncMock()
    heart_mock.facts = AsyncMock()
    heart_mock.facts.find_key_conflict_pairs = AsyncMock(
        side_effect=AssertionError("must not be called when flag is off")
    )

    settings = SimpleNamespace(
        supersession_key_resolution_enabled=False,
        supersession_sweep_max_pairs=25,
    )
    handler = SleepHandler(
        brain=AsyncMock(),
        heart=heart_mock,
        settings=settings,
        bus=MagicMock(),
        llm_client=MagicMock(),
    )
    handler._llm = MagicMock()
    handler._interrupted = False

    sleep_stats: dict = {}
    result = await handler._phase_sweep_key_conflicts(sleep_stats)

    assert result is True
    heart_mock.facts.find_key_conflict_pairs.assert_not_called()


@pytest.mark.postgres_only
async def test_phase_sweep_key_conflicts_cap_leaves_remainder(heart):
    """(d) Pairs beyond supersession_sweep_max_pairs are left for the next cycle.
    Resolution deactivates processed losers so unprocessed pairs re-surface."""
    from nous.handlers.sleep_handler import SleepHandler

    # Create 3 distinct same-key pairs
    pairs_data = [
        ("network", "latency", "Network latency is ten milliseconds at baseline average.", "Network latency is now twenty milliseconds at baseline average."),
        ("cache", "hit_rate", "Cache hit rate is eighty percent over the last hour sampled.", "Cache hit rate is now ninety percent over the last hour sampled."),
        ("cpu", "usage", "CPU usage averages thirty percent during peak traffic hours daily.", "CPU usage averages sixty percent during peak traffic hours daily."),
    ]
    for subj, attr, old_c, new_c in pairs_data:
        await heart.learn(FactInput(content=old_c, subject_key=subj, attribute_key=attr))
        await heart.learn(FactInput(content=new_c, subject_key=subj, attribute_key=attr))

    settings = heart.facts._settings.model_copy(
        update={
            "supersession_key_resolution_enabled": True,
            "supersession_sweep_max_pairs": 2,  # cap at 2 — one pair left over
            "supersession_classifier_max_per_hour": 500,
        }
    )
    heart.facts._settings = settings
    heart.facts._classify_fact_pair = AsyncMock(
        return_value={"relation": "UPDATE", "current_fact": "new", "confidence": 0.95}
    )

    llm_mock = MagicMock()
    handler = SleepHandler(
        brain=AsyncMock(),
        heart=heart,
        settings=settings,
        bus=MagicMock(),
        llm_client=llm_mock,
    )
    handler._llm = llm_mock
    handler._interrupted = False

    sleep_stats: dict = {}
    await handler._phase_sweep_key_conflicts(sleep_stats)

    # Only the capped number of pairs (2) were processed
    assert sleep_stats["key_conflicts_found"] == 2
    # At least one pair remains unprocessed (re-surfaces next cycle)
    remaining = await heart.facts.find_key_conflict_pairs(limit=25)
    assert len(remaining) >= 1


@pytest.mark.asyncio
async def test_key_conflict_sweep_cursor_pages_past_keep_both():
    """(e) Paging cursor: one f1 with three same-key f2 partners — KEEP_BOTH
    on pairs 1 and 2 must NOT starve pair 3; the 4-tuple cursor
    (ts1, id1, ts2, id2) addresses each pair uniquely so the same f1 is
    never skipped prematurely.

    Uses AsyncMock so the test runs without a real DB and proves cursor
    state transitions and per-cycle classifier dispatch via call inspection.
    """
    from datetime import datetime, timezone
    from uuid import uuid4
    from nous.handlers.sleep_handler import SleepHandler

    # One shared f1 with three f2 partners in ascending ts2 order
    uuid_f1 = uuid4()
    uuid_f2a = uuid4()
    uuid_f2b = uuid4()
    uuid_f2c = uuid4()
    ts_f1 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    ts_f2a = datetime(2026, 1, 2, tzinfo=timezone.utc)
    ts_f2b = datetime(2026, 1, 3, tzinfo=timezone.utc)
    ts_f2c = datetime(2026, 1, 4, tzinfo=timezone.utc)

    pair1 = {
        "id1": uuid_f1, "id2": uuid_f2a,
        "c1": "replica count is two nodes",
        "c2": "replica count is four nodes",
        "ts1": ts_f1, "ts2": ts_f2a,
    }
    pair2 = {
        "id1": uuid_f1, "id2": uuid_f2b,
        "c1": "replica count is two nodes",
        "c2": "replica count is six nodes",
        "ts1": ts_f1, "ts2": ts_f2b,
    }
    pair3 = {
        "id1": uuid_f1, "id2": uuid_f2c,
        "c1": "replica count is two nodes",
        "c2": "replica count is eight nodes",
        "ts1": ts_f1, "ts2": ts_f2c,
    }

    cursor_after_1 = (ts_f1, uuid_f1, ts_f2a, uuid_f2a)
    cursor_after_2 = (ts_f1, uuid_f1, ts_f2b, uuid_f2b)
    cursor_after_3 = (ts_f1, uuid_f1, ts_f2c, uuid_f2c)

    # find_key_conflict_pairs: branch on 4-tuple after= value
    find_mock = AsyncMock()
    find_mock.side_effect = lambda limit, after=None: (
        [pair1] if after is None
        else [pair2] if after == cursor_after_1
        else [pair3] if after == cursor_after_2
        else []
    )

    # resolve_key_conflict_pair: KEEP_BOTH for pair1/pair2, UPDATE for pair3
    resolve_mock = AsyncMock(side_effect=lambda id1, id2, c1, c2: id2 == uuid_f2c)

    heart_mock = AsyncMock()
    heart_mock.facts.find_key_conflict_pairs = find_mock
    heart_mock.facts.resolve_key_conflict_pair = resolve_mock
    # key_budget_exhausted is synchronous — pin it so the AsyncMock parent
    # doesn't return a truthy coroutine and fire the budget guard prematurely.
    heart_mock.facts.key_budget_exhausted = MagicMock(return_value=False)

    settings = SimpleNamespace(
        supersession_key_resolution_enabled=True,
        supersession_sweep_max_pairs=1,
        supersession_classifier_max_per_hour=500,
    )
    handler = SleepHandler(
        brain=AsyncMock(),
        heart=heart_mock,
        settings=settings,
        bus=MagicMock(),
        llm_client=MagicMock(),
    )
    handler._llm = MagicMock()
    handler._interrupted = False

    # Cycle 1: processes pair1 (KEEP_BOTH → resolve returns False)
    stats1: dict = {}
    await handler._phase_sweep_key_conflicts(stats1)
    assert stats1["key_conflicts_found"] == 1
    assert stats1["key_supersessions_written"] == 0
    assert handler._key_sweep_cursor == cursor_after_1, (
        f"Expected 4-tuple cursor {cursor_after_1} after cycle 1, "
        f"got {handler._key_sweep_cursor}"
    )
    resolve_mock.assert_awaited_once_with(uuid_f1, uuid_f2a, pair1["c1"], pair1["c2"])

    # Cycle 2: cursor=cursor_after_1; fetches pair2 (KEEP_BOTH), not pair1
    resolve_mock.reset_mock()
    stats2: dict = {}
    await handler._phase_sweep_key_conflicts(stats2)
    assert stats2["key_conflicts_found"] == 1
    assert stats2["key_supersessions_written"] == 0
    assert handler._key_sweep_cursor == cursor_after_2, (
        f"Expected 4-tuple cursor {cursor_after_2} after cycle 2, "
        f"got {handler._key_sweep_cursor}"
    )
    resolve_mock.assert_awaited_once_with(uuid_f1, uuid_f2b, pair2["c1"], pair2["c2"])

    # Cycle 3: cursor=cursor_after_2; fetches pair3 (UPDATE → resolved)
    resolve_mock.reset_mock()
    stats3: dict = {}
    await handler._phase_sweep_key_conflicts(stats3)
    assert stats3["key_conflicts_found"] == 1
    assert stats3["key_supersessions_written"] == 1
    assert handler._key_sweep_cursor == cursor_after_3, (
        f"Expected 4-tuple cursor {cursor_after_3} after cycle 3, "
        f"got {handler._key_sweep_cursor}"
    )
    resolve_mock.assert_awaited_once_with(uuid_f1, uuid_f2c, pair3["c1"], pair3["c2"])

    # Cycle 4: cursor=cursor_after_3 → fetch returns [] → cursor resets to None
    resolve_mock.reset_mock()
    stats4: dict = {}
    await handler._phase_sweep_key_conflicts(stats4)
    assert stats4["key_conflicts_found"] == 0
    assert handler._key_sweep_cursor is None, (
        "Cursor must reset to None when fetch returns 0 rows (table exhausted)"
    )


@pytest.mark.asyncio
async def test_key_conflict_sweep_cursor_on_interruption():
    """Codex r3 FIX 2: when _interrupted fires after the first resolve, the
    cursor must advance only to that first pair's 4-tuple, NOT to pairs[-1].

    Three pairs are returned in a single page (max_pairs=3).  The mock
    resolve sets handler._interrupted=True after pair1 is processed.  The
    loop then breaks at the top of pair2's iteration (before resolving it).
    The cursor must be pair1's 4-tuple so pair2 and pair3 are retried next
    cycle.
    """
    from datetime import datetime, timezone
    from uuid import uuid4
    from nous.handlers.sleep_handler import SleepHandler

    ts = datetime(2026, 3, 1, tzinfo=timezone.utc)
    id_f1 = uuid4()
    id_f2a = uuid4()
    id_f2b = uuid4()
    id_f2c = uuid4()

    pair1 = {"id1": id_f1, "id2": id_f2a, "c1": "c1a", "c2": "c2a", "ts1": ts, "ts2": ts}
    pair2 = {"id1": id_f1, "id2": id_f2b, "c1": "c1b", "c2": "c2b", "ts1": ts, "ts2": ts}
    pair3 = {"id1": id_f1, "id2": id_f2c, "c1": "c1c", "c2": "c2c", "ts1": ts, "ts2": ts}

    expected_cursor = (pair1["ts1"], pair1["id1"], pair1["ts2"], pair1["id2"])

    heart_mock = AsyncMock()
    heart_mock.facts.find_key_conflict_pairs = AsyncMock(return_value=[pair1, pair2, pair3])
    # key_budget_exhausted is synchronous — pin it so the AsyncMock parent
    # doesn't return a truthy coroutine and fire the budget guard prematurely.
    heart_mock.facts.key_budget_exhausted = MagicMock(return_value=False)

    call_count = 0

    async def resolve_and_interrupt(id1, id2, c1, c2):
        nonlocal call_count
        call_count += 1
        handler._interrupted = True  # trigger break on next loop iteration
        return False  # KEEP_BOTH

    heart_mock.facts.resolve_key_conflict_pair = resolve_and_interrupt

    settings = SimpleNamespace(
        supersession_key_resolution_enabled=True,
        supersession_sweep_max_pairs=3,
        supersession_classifier_max_per_hour=500,
    )
    handler = SleepHandler(
        brain=AsyncMock(),
        heart=heart_mock,
        settings=settings,
        bus=MagicMock(),
        llm_client=MagicMock(),
    )
    handler._llm = MagicMock()
    handler._interrupted = False

    stats: dict = {}
    result = await handler._phase_sweep_key_conflicts(stats)

    assert result is True
    assert call_count == 1, f"Expected resolve called once, got {call_count}"
    assert handler._key_sweep_cursor == expected_cursor, (
        f"Cursor must be pair1's 4-tuple {expected_cursor}, "
        f"got {handler._key_sweep_cursor} — the fix guards against advancing "
        "past unprocessed pairs on interruption"
    )


@pytest.mark.postgres_only
async def test_event_date_type_equality_round_trip(heart):
    """devil-2 #5 type-equality: event_date stored as datetime.date, not str.
    Pins that the Python != in _resolve_key_conflicts and the SQL = in
    find_key_conflict_pairs compare the same datetime.date type."""
    import datetime

    result = await heart.learn(
        FactInput(
            content="The quarterly report was published on the tenth of March two thousand twenty-six.",
            subject_key="quarterly_report",
            attribute_key="publication_date",
            event_date="2026-03-10",
        )
    )

    async with heart.db.session() as s:
        row = await s.get(Fact, result.id)
        assert row is not None
        assert isinstance(row.event_date, datetime.date), (
            f"expected datetime.date, got {type(row.event_date)}"
        )
        expected = FactInput(
            content="_" * 40,
            event_date="2026-03-10",
        ).event_date
        assert row.event_date == expected, (
            f"DB value {row.event_date!r} != FactInput-validated {expected!r}"
        )


@pytest.mark.postgres_only
async def test_find_key_conflict_pairs_equal_timestamp_pair(heart):
    """Codex r3 FIX 1: two same-key facts with an IDENTICAL learned_at must be
    returned by find_key_conflict_pairs.  The (f1.learned_at, f1.id) row-comparison
    uses id as the tiebreak so equal-timestamp pairs are not silently dropped."""
    import datetime

    from nous.storage.models import Fact as FactModel

    # Learn two same-key facts (timestamps may already coincide at sub-ms resolution,
    # but we set them explicitly to guarantee equality).
    f1 = await heart.learn(
        FactInput(
            content="The cluster has eight replicas running in production at peak.",
            subject_key="cluster",
            attribute_key="replica_count",
        )
    )
    f2 = await heart.learn(
        FactInput(
            content="The cluster now has sixteen replicas running in production at peak.",
            subject_key="cluster",
            attribute_key="replica_count",
        )
    )
    assert f1.id is not None and f2.id is not None

    # Force both rows to share the same learned_at timestamp.
    shared_ts = datetime.datetime(2026, 1, 1, 12, 0, 0, tzinfo=datetime.timezone.utc)
    async with heart.db.session() as s:
        row1 = await s.get(FactModel, f1.id)
        row2 = await s.get(FactModel, f2.id)
        assert row1 is not None and row2 is not None
        row1.learned_at = shared_ts
        row2.learned_at = shared_ts
        await s.commit()

    pairs = await heart.facts.find_key_conflict_pairs(limit=25)

    pair_id_sets = [{str(p["id1"]), str(p["id2"])} for p in pairs]
    assert {str(f1.id), str(f2.id)} in pair_id_sets, (
        f"Equal-timestamp pair {{{f1.id}, {f2.id}}} not found in {pair_id_sets}; "
        "the (f1.learned_at, f1.id) row-comparison fix may not be applied"
    )

    # id1 must be the lower UUID (PG row comparison uses uuid lexicographic order)
    matching = next(p for p in pairs if {str(p["id1"]), str(p["id2"])} == {str(f1.id), str(f2.id)})
    lower_id = min(f1.id, f2.id, key=lambda u: str(u))
    assert matching["id1"] == lower_id, (
        f"Expected id1={lower_id} (lower UUID), got id1={matching['id1']}"
    )


# ---------------------------------------------------------------------------
# Task 10: R2.3 retrieval-contract regression tests (RC-7 / DC-1)
# ---------------------------------------------------------------------------


@pytest.mark.postgres_only
async def test_superseded_fact_excluded_from_default_search(heart):
    """R2.3 contract: after apply_supersession, the loser never appears in
    search_facts (active=true filter in hybrid_search) — 'default recall
    returns 0 superseded facts' acceptance test (RC-7).

    Uses auto-committed learn calls (no session=) so that search_facts,
    which opens its own session, sees the committed state.
    """
    # Learn two distinct facts that share the query term "project deadline"
    a = await heart.learn(
        FactInput(content="The project deadline is March 1st twenty-six, confirmed by the team.")
    )
    b = await heart.learn(
        FactInput(content="The project deadline is April 15th twenty-six, revised after review.")
    )
    assert a.id is not None, f"Fact A was rejected: {a}"
    assert b.id is not None, f"Fact B was rejected: {b}"

    # Apply supersession: B wins over A (April deadline supersedes March deadline)
    async with heart.db.session() as s:
        ok = await heart.facts.apply_supersession(b.id, a.id, s)
        assert ok, "apply_supersession returned False — clobber guard fired unexpectedly"
        await s.commit()

    # Default search uses active_only=True — the loser (A) must be excluded
    results = await heart.search_facts("project deadline", limit=10)
    ids = {r.id for r in results}
    assert a.id not in ids, (
        f"Superseded fact A ({a.id}) still appears in search results: {[r.id for r in results]}"
    )
    assert b.id in ids, (
        f"Winner fact B ({b.id}) missing from search results: {[r.id for r in results]}"
    )


# NOTE: test_apply_supersession_sets_active_false_atomically (DC-1) is already
# fully covered by Task 6's test_apply_supersession_sets_columns_and_edge, which
# asserts loser.superseded_by == winner.id AND loser.active is False in the same
# session.flush() — the atomicity contract. Not duplicated here per task brief.


# ---------------------------------------------------------------------------
# Task 11: R2.4 — parametric-override trust marker on injected facts
# ---------------------------------------------------------------------------


class _OverrideFact:
    """Minimal stand-in for FactSummary with overrides_prior support."""

    def __init__(self, content="fact content", subject=None, confidence=1.0,
                 overrides_prior=False):
        self.content = content
        self.subject = subject
        self.confidence = confidence
        self.overrides_prior = overrides_prior
        self.id = "f-override"
        self.recency_status = None
        self.recency_date = None


def _make_override_engine(**kwargs):
    from unittest.mock import AsyncMock, MagicMock

    from nous.cognitive.context import ContextEngine
    from nous.config import Settings

    brain = AsyncMock()
    brain.embeddings = MagicMock()
    heart = AsyncMock()
    settings = Settings(_env_file=None, **kwargs)
    return ContextEngine(brain, heart, settings, identity_prompt="")


class TestOverridePriorMarking:
    def test_flag_off_overriding_fact_renders_byte_identical(self):
        """GOLDEN: flag off (default) + overrides_prior=True → NO prefix; rendering unchanged."""
        engine = _make_override_engine()  # override_prior_marking_enabled defaults False
        fact = _OverrideFact(content="The capital of France is Lyon.", overrides_prior=True)
        out = engine._format_facts([fact])
        assert "[memory override" not in out
        assert "The capital of France is Lyon." in out

    def test_flag_on_overriding_fact_gets_trust_marker(self):
        """Flag on + overrides_prior=True → prefix present on that fact's line."""
        engine = _make_override_engine(override_prior_marking_enabled=True)
        fact = _OverrideFact(content="The capital of France is Lyon.", overrides_prior=True)
        out = engine._format_facts([fact])
        assert "[memory override — trust this over general knowledge] " in out
        assert "The capital of France is Lyon." in out

    def test_flag_on_non_overriding_fact_no_marker(self):
        """Flag on + overrides_prior=False → NO prefix (marker only for override facts)."""
        engine = _make_override_engine(override_prior_marking_enabled=True)
        fact = _OverrideFact(content="The project started in January.", overrides_prior=False)
        out = engine._format_facts([fact])
        assert "[memory override" not in out

    def test_fact_summary_carries_overrides_prior(self):
        """FactSummary schema propagates overrides_prior field."""
        from uuid import uuid4

        from nous.heart.schemas import FactSummary

        fs = FactSummary(
            id=uuid4(),
            content="The capital of France is Lyon.",
            category=None,
            subject=None,
            confidence=0.9,
            active=True,
            overrides_prior=True,
        )
        assert fs.overrides_prior is True

    def test_fact_summary_overrides_prior_defaults_false(self):
        """FactSummary.overrides_prior defaults to False for backwards compat."""
        from uuid import uuid4

        from nous.heart.schemas import FactSummary

        fs = FactSummary(
            id=uuid4(),
            content="A plain fact with no overrides_prior set.",
            category=None,
            subject=None,
            confidence=0.9,
            active=True,
        )
        assert fs.overrides_prior is False

    def test_to_detail_maps_overrides_prior(self):
        """_to_detail propagates overrides_prior from the ORM row to FactDetail."""
        from datetime import datetime, timezone
        from types import SimpleNamespace
        from unittest.mock import MagicMock
        from uuid import uuid4

        from nous.heart.facts import FactManager

        fact_row = SimpleNamespace(
            id=uuid4(),
            agent_id="nous-default",
            content="The capital of France is Lyon.",
            category=None,
            subject=None,
            confidence=0.9,
            source=None,
            source_episode_id=None,
            source_decision_id=None,
            learned_at=datetime.now(timezone.utc),
            last_confirmed=None,
            confirmation_count=0,
            superseded_by=None,
            contradiction_of=None,
            active=True,
            tags=[],
            created_at=datetime.now(timezone.utc),
            actionable=None,
            actionable_confidence=None,
            event_date=None,
            overrides_prior=True,
        )

        mgr = FactManager.__new__(FactManager)
        mgr.agent_id = "nous-default"
        detail = mgr._to_detail(fact_row)
        assert detail.overrides_prior is True

    def test_config_flag_defaults_false(self):
        """override_prior_marking_enabled defaults to False in Settings."""
        from nous.config import Settings

        s = Settings(_env_file=None)
        assert s.override_prior_marking_enabled is False


# ---------------------------------------------------------------------------
# Task 12: R1.4 — backfill_enumerative_facts.py (select_backfill_episodes)
# ---------------------------------------------------------------------------

from datetime import datetime, timedelta, timezone


@pytest.mark.postgres_only
async def test_select_backfill_episodes_filters_empty_transcripts(session):
    """select_backfill_episodes: returns only episodes with non-empty transcript.

    Episodes with transcript=None or transcript='' must be excluded.
    """
    from scripts.backfill_enumerative_facts import select_backfill_episodes
    from nous.storage.models import Episode

    agent_id = f"test-enum-filter-{uuid4().hex[:8]}"
    now = datetime.now(timezone.utc)

    ep_with = Episode(
        agent_id=agent_id,
        summary="Episode with transcript",
        transcript="Item 1.\nItem 2.\nItem 3.",
        started_at=now,
    )
    ep_none = Episode(
        agent_id=agent_id,
        summary="Episode with None transcript",
        transcript=None,
        started_at=now,
    )
    ep_empty = Episode(
        agent_id=agent_id,
        summary="Episode with empty transcript",
        transcript="",
        started_at=now,
    )
    session.add_all([ep_with, ep_none, ep_empty])
    await session.flush()

    rows = await select_backfill_episodes(session, agent_id, None, 0)
    ids = {r.id for r in rows}

    assert ep_with.id in ids, "episode with transcript must be returned"
    assert ep_none.id not in ids, "episode with None transcript must be excluded"
    assert ep_empty.id not in ids, "episode with empty transcript must be excluded"


@pytest.mark.postgres_only
async def test_select_backfill_episodes_includes_closed_episodes(session):
    """#557 liveness lesson: episodes.active is a LIFECYCLE flag — _end() sets
    active=False on every properly closed episode (episodes.py:235). Closed
    episodes are exactly the ones with complete transcripts the backfill
    exists to remediate; filtering `active = true` selected only OPEN episodes
    and silently skipped the whole corpus. Closed (ended) episodes must be
    included; never-ended deactivated orphans stay excluded."""
    from scripts.backfill_enumerative_facts import select_backfill_episodes
    from nous.storage.models import Episode

    agent_id = f"test-enum-closed-{uuid4().hex[:8]}"
    now = datetime.now(timezone.utc)

    ep_closed = Episode(
        agent_id=agent_id,
        summary="Properly closed episode",
        transcript="Fact one.\nFact two.\nFact three.",
        started_at=now,
        ended_at=now,
        active=False,  # lifecycle close, NOT soft-delete
    )
    ep_open = Episode(
        agent_id=agent_id,
        summary="Still-open episode",
        transcript="Live fact 1.\nLive fact 2.\nLive fact 3.",
        started_at=now,
        active=True,
    )
    ep_orphan = Episode(
        agent_id=agent_id,
        summary="Never-ended deactivated orphan",
        transcript="Orphan fact 1.\nOrphan fact 2.\nOrphan fact 3.",
        started_at=now,
        ended_at=None,
        active=False,  # deactivated without ever closing — excluded
    )
    ep_abandoned = Episode(
        agent_id=agent_id,
        summary="Abandoned episode",
        transcript="Stale fact 1.\nStale fact 2.\nStale fact 3.",
        started_at=now,
        ended_at=now,  # F060.2 stamps ended_at on abandonment too
        active=False,
        outcome="abandoned",  # excluded, mirroring the recall predicate
    )
    session.add_all([ep_closed, ep_open, ep_orphan, ep_abandoned])
    await session.flush()

    rows = await select_backfill_episodes(session, agent_id, None, 0)
    ids = {r.id for r in rows}

    assert ep_closed.id in ids, "closed episode (the normal case) must be included"
    assert ep_open.id in ids, "open episode must still be included"
    assert ep_orphan.id not in ids, "never-ended deactivated orphan must stay excluded"
    assert ep_abandoned.id not in ids, "abandoned episode must stay excluded (F060.2)"


@pytest.mark.postgres_only
async def test_select_backfill_episodes_orders_oldest_first(session):
    """select_backfill_episodes: returns episodes ordered oldest-first (started_at ASC)."""
    from scripts.backfill_enumerative_facts import select_backfill_episodes
    from nous.storage.models import Episode

    agent_id = f"test-enum-order-{uuid4().hex[:8]}"
    now = datetime.now(timezone.utc)

    # Insert newest first (opposite of expected return order)
    ep_newest = Episode(
        agent_id=agent_id,
        summary="Newest episode",
        transcript="New fact 1.\nNew fact 2.\nNew fact 3.",
        started_at=now,
    )
    ep_oldest = Episode(
        agent_id=agent_id,
        summary="Oldest episode",
        transcript="Old fact 1.\nOld fact 2.\nOld fact 3.",
        started_at=now - timedelta(hours=2),
    )
    session.add_all([ep_newest, ep_oldest])
    await session.flush()

    rows = await select_backfill_episodes(session, agent_id, None, 0)
    # Filter to only our test episodes, preserving return order
    our_ids = {ep_oldest.id, ep_newest.id}
    ordered = [r for r in rows if r.id in our_ids]

    assert len(ordered) == 2, f"expected 2 episodes, got {len(ordered)}"
    assert ordered[0].id == ep_oldest.id, "oldest episode must come first"
    assert ordered[1].id == ep_newest.id, "newest episode must come last"


@pytest.mark.postgres_only
async def test_dry_run_performs_zero_writes(session):
    """Dry-run path: select_backfill_episodes + is_enumerable never calls heart.learn.

    Verifies the dry-run branch by running the exact same code it runs —
    select + classify — and asserting no facts were written.
    """
    from scripts.backfill_enumerative_facts import select_backfill_episodes
    from nous.handlers.enumerative_extractor import is_enumerable

    agent_id = f"test-enum-dryrun-{uuid4().hex[:8]}"
    now = datetime.now(timezone.utc)

    # Dense transcript that would be enumerable at the default threshold
    dense_transcript = "\n".join(
        f"{i}. Entity {i} belongs to agent {i} and is stored here." for i in range(40)
    )
    ep = Episode(
        agent_id=agent_id,
        summary="Dense episode for dry-run test",
        transcript=dense_transcript,
        started_at=now,
    )
    session.add(ep)
    await session.flush()

    from sqlalchemy import text as sa_text

    # Fact count before dry-run (must stay 0 — none were inserted for this agent)
    before = (
        await session.execute(
            sa_text("SELECT COUNT(*) FROM heart.facts WHERE agent_id = :a"),
            {"a": agent_id},
        )
    ).scalar()

    # Dry-run core: select + classify, no heart.learn call
    episodes = await select_backfill_episodes(session, agent_id, None, 0)
    _enumerable = [e for e in episodes if is_enumerable(e.transcript, 0.6)]

    # Fact count after — must equal before (no writes)
    after = (
        await session.execute(
            sa_text("SELECT COUNT(*) FROM heart.facts WHERE agent_id = :a"),
            {"a": agent_id},
        )
    ).scalar()

    assert before == after == 0, (
        f"dry-run must write 0 facts; before={before} after={after}"
    )
    # Sanity: the dense transcript IS enumerable so the path was exercised
    assert len(_enumerable) >= 1, "dense transcript should be classified enumerable"


# ---------------------------------------------------------------------------
# Task 13: R2.5/R2.6 — backfill_supersession.py (run_sweep)
# ---------------------------------------------------------------------------


@pytest.mark.postgres_only
async def test_backfill_dry_run_no_writes_no_classifier(heart, monkeypatch):
    """T13(a): dry-run counts candidate pairs without any classifier calls or writes.

    Seeds a same-key pair, verifies _classify_fact_pair is never invoked, and
    confirms no superseded_by rows appear in the DB for the test agent.
    """
    from scripts.backfill_supersession import run_sweep
    from sqlalchemy import text as sa_text
    from unittest.mock import AsyncMock

    # Unique agent_id so find_key_conflict_pairs only sees our facts.
    unique_agent = f"t13a-{uuid4().hex[:8]}"
    heart.facts.agent_id = unique_agent

    await heart.learn(
        FactInput(
            content="The production server CPU count is four cores running continuously.",
            subject_key="prod_server",
            attribute_key="cpu_count",
        )
    )
    await heart.learn(
        FactInput(
            content="The production server CPU count is eight cores running continuously.",
            subject_key="prod_server",
            attribute_key="cpu_count",
        )
    )

    sentinel = AsyncMock(side_effect=AssertionError("classifier must not be called in dry-run"))
    monkeypatch.setattr(heart.facts, "_classify_fact_pair", sentinel)

    settings = heart.facts._settings.model_copy(update={
        "supersession_key_resolution_enabled": True,
        "supersession_classifier_max_per_hour": 0,  # unlimited
    })

    result = await run_sweep(heart, settings, max_pairs=0, batch_size=25, dry_run=True)

    sentinel.assert_not_called()
    assert result["resolutions_written"] == 0
    assert result["pairs_examined"] >= 1  # at least our seeded pair

    # Confirm no supersessions in DB for this agent.
    async with heart.db.session() as s:
        count = (
            await s.execute(
                sa_text("SELECT COUNT(*) FROM heart.facts WHERE agent_id = :a AND superseded_by IS NOT NULL"),
                {"a": unique_agent},
            )
        ).scalar()
    assert count == 0, f"dry-run must write 0 supersessions; found {count}"


@pytest.mark.postgres_only
async def test_backfill_keep_both_seen_set_terminates(heart, monkeypatch):
    """T13(b): KEEP-BOTH pair terminates the loop via seen-set; pairs_examined == 1.

    Seeds exactly ONE same-key pair under an isolated agent_id, mocks the
    classifier to return UNRELATED (→ resolve returns False), and asserts the
    loop exits cleanly with pairs_examined==1 rather than looping infinitely.
    """
    from scripts.backfill_supersession import run_sweep
    from unittest.mock import AsyncMock

    # Unique agent_id for DB isolation.
    unique_agent = f"t13b-{uuid4().hex[:8]}"
    heart.facts.agent_id = unique_agent

    await heart.learn(
        FactInput(
            content="The cache server memory size is four gigabytes total allocated.",
            subject_key="cache_server",
            attribute_key="memory_size",
        )
    )
    await heart.learn(
        FactInput(
            content="The cache server memory size is eight gigabytes total allocated.",
            subject_key="cache_server",
            attribute_key="memory_size",
        )
    )

    # Classifier returns UNRELATED → resolve_key_conflict_pair returns False.
    monkeypatch.setattr(
        heart.facts,
        "_classify_fact_pair",
        AsyncMock(return_value={"relation": "UNRELATED", "current_fact": "new", "confidence": 0.9}),
    )

    settings = heart.facts._settings.model_copy(update={
        "supersession_key_resolution_enabled": True,
        "supersession_classifier_max_per_hour": 0,  # unlimited — so budget is NOT the stop
    })

    result = await run_sweep(heart, settings, max_pairs=0, batch_size=25, dry_run=False)

    # Loop must have terminated (test completion proves no infinite loop).
    assert result["pairs_examined"] == 1, (
        f"expected 1 pair examined (only our seeded pair); got {result['pairs_examined']}"
    )
    assert result["resolutions_written"] == 0
    assert result["keep_both"] == 1


@pytest.mark.postgres_only
async def test_chain_depth_histogram_accepts_iso_string_watermark(heart):
    """Field bug (sh_6k run): the run watermark is an ISO STRING (kept that way
    for the printed rollback key) but was bound directly into the histogram's
    timestamptz query — asyncpg raises TypeError, rc=2 AFTER all resolutions
    were committed (only the report printout was lost). The helper must accept
    both str and datetime watermarks."""
    from collections import Counter
    from datetime import UTC, datetime
    from uuid import uuid4

    from scripts.backfill_supersession import _chain_depth_histogram

    unique_agent = f"hist-{uuid4().hex[:8]}"
    wm_dt = datetime.now(UTC)
    async with heart.db.session() as s:
        # str watermark (the exact shape _run_backfill passes) must not raise.
        hist_str = await _chain_depth_histogram(s, unique_agent, wm_dt.isoformat())
        # datetime watermark keeps working.
        hist_dt = await _chain_depth_histogram(s, unique_agent, wm_dt)
    assert hist_str == hist_dt == Counter()


# ---------------------------------------------------------------------------
# Codex round-4 P1: stale-pair guard in resolve_key_conflict_pair
# ---------------------------------------------------------------------------


@pytest.mark.postgres_only
async def test_resolve_key_conflict_pair_skips_stale_pair(heart):
    """R4-P1 unit test: resolve_key_conflict_pair must return False immediately
    (without calling the classifier) when either row is already inactive or
    already has a superseded_by set.

    Scenario: seed B (older), A (middle), C (newest) with the same key.
    Deactivate B via apply_supersession (B.superseded_by=A, B.active=False).
    Then call resolve_key_conflict_pair(B.id, C.id, ...) — B is id1 (f_old).
    The stale guard must catch B.active=False and return False before the
    classifier is ever invoked."""
    # Seed: B oldest, A middle, C newest (so B is f_old in the B/C pair)
    f_b = await heart.learn(
        FactInput(
            content="The storage tier has four replicas across two availability zones.",
            subject_key="storage_tier",
            attribute_key="replica_count",
        )
    )
    f_a = await heart.learn(
        FactInput(
            content="The storage tier has six replicas across three availability zones.",
            subject_key="storage_tier",
            attribute_key="replica_count",
        )
    )
    f_c = await heart.learn(
        FactInput(
            content="The storage tier has eight replicas across four availability zones.",
            subject_key="storage_tier",
            attribute_key="replica_count",
        )
    )

    # Deactivate B: A wins over B (simulates a prior sweep resolution)
    async with heart.db.session() as s:
        await heart.facts.apply_supersession(f_a.id, f_b.id, s)
        await s.commit()

    # Confirm B is now inactive
    async with heart.db.session() as s:
        b_row = await s.get(Fact, f_b.id)
        assert b_row is not None and b_row.active is False and b_row.superseded_by == f_a.id

    # Sentinel: classifier must NOT be called for a stale pair
    classifier_sentinel = AsyncMock(
        side_effect=AssertionError("classifier must not fire for stale pair")
    )
    heart.facts._classify_fact_pair = classifier_sentinel

    # r8: stale pairs must also not consume a budget slot — capture before call
    key_calls_before = heart.facts._key_calls

    # B is f_old (id1), C is f_new (id2); B is inactive — must short-circuit
    result = await heart.facts.resolve_key_conflict_pair(
        f_b.id, f_c.id, f_b.content, f_c.content
    )

    assert result is False, "stale pair (inactive winner candidate) must return False"
    classifier_sentinel.assert_not_called()
    assert heart.facts._key_calls == key_calls_before, (
        "stale pair must not consume a budget slot (_key_calls incremented before staleness guard)"
    )

    # C must remain active and unsuperseded
    async with heart.db.session() as s:
        c_row = await s.get(Fact, f_c.id)
        assert c_row is not None
        assert c_row.active is not False, (
            f"C must remain active; got active={c_row.active}"
        )
        assert c_row.superseded_by is None, (
            f"C must not be superseded; got superseded_by={c_row.superseded_by}"
        )


@pytest.mark.postgres_only
async def test_phase_sweep_no_inactive_winner(heart, caplog):
    """R4-P1 integration test: after a full sweep-page over three same-key facts,
    the stale-pair guard must ensure no superseded_by value ever points to an
    inactive fact, and get_current must resolve all facts to a single active tip
    without emitting any cycle-repair WARNINGs.

    Ordering: A (oldest) < B (middle) < C (newest).
    Classifier: UPDATE / current=new (newer fact wins every pair).
    Expected flow with fix:
      (A,B) → B wins, A inactive;
      (A,C) → A inactive → stale guard skips;
      (B,C) → B active (won prior pair), C wins, B inactive.
    End state: A→B→C chain; C is the sole active tip."""
    import logging
    from nous.handlers.sleep_handler import SleepHandler

    # Isolate from other tests: unique agent_id so find_key_conflict_pairs
    # and get_current only see facts seeded by this test.
    unique_agent = f"r4p1-sweep-{uuid4().hex[:8]}"
    heart.facts.agent_id = unique_agent

    # Seed three same-key facts (A oldest, C newest by insertion order)
    f_a = await heart.learn(
        FactInput(
            content="The message queue backlog is one hundred thousand messages long.",
            subject_key="message_queue",
            attribute_key="backlog_size",
        )
    )
    f_b = await heart.learn(
        FactInput(
            content="The message queue backlog is fifty thousand messages currently.",
            subject_key="message_queue",
            attribute_key="backlog_size",
        )
    )
    f_c = await heart.learn(
        FactInput(
            content="The message queue backlog is ten thousand messages currently.",
            subject_key="message_queue",
            attribute_key="backlog_size",
        )
    )

    settings = heart.facts._settings.model_copy(
        update={
            "supersession_key_resolution_enabled": True,
            "supersession_sweep_max_pairs": 25,
            "supersession_classifier_max_per_hour": 500,
        }
    )
    heart.facts._settings = settings
    # Classifier always says newer (id2) wins
    heart.facts._classify_fact_pair = AsyncMock(
        return_value={"relation": "UPDATE", "current_fact": "new", "confidence": 0.95}
    )

    llm_mock = MagicMock()
    handler = SleepHandler(
        brain=AsyncMock(),
        heart=heart,
        settings=settings,
        bus=MagicMock(),
        llm_client=llm_mock,
    )
    handler._llm = llm_mock
    handler._interrupted = False

    sleep_stats: dict = {}
    with caplog.at_level(logging.WARNING, logger="nous.heart.facts"):
        await handler._phase_sweep_key_conflicts(sleep_stats)

    # Invariant 1: every superseded_by chain must transitively resolve to an
    # active tip (the P1 bug produces a dead-end inactive winner with no further
    # superseded_by, which would cause get_current to raise or need cycle repair).
    async with heart.db.session() as s:
        for label, fid in [("A", f_a.id), ("B", f_b.id), ("C", f_c.id)]:
            row = await s.get(Fact, fid)
            assert row is not None
            if row.superseded_by is not None:
                # Follow the chain: the target must itself be active OR must
                # have its own superseded_by pointing onward to an active tip.
                tip = await heart.facts.get_current(row.superseded_by)
                assert tip.id is not None, (
                    f"fact {label} superseded_by chain leads to no active tip"
                )

    # Invariant 2: no cycle-repair WARNING emitted during the sweep
    cycle_msgs = [m for m in caplog.messages if "cycle" in m.lower()]
    assert not cycle_msgs, f"Unexpected cycle-repair messages: {cycle_msgs}"

    # Invariant 3: all three facts' get_current must converge to a single
    # active tip, and that tip must be C (the newest fact).
    tips = set()
    for fid in [f_a.id, f_b.id, f_c.id]:
        tip = await heart.facts.get_current(fid)
        tips.add(tip.id)
    assert len(tips) == 1, (
        f"Expected all facts to converge to one active tip; got {tips}"
    )
    assert f_c.id in tips, (
        f"Expected C ({f_c.id}) to be the active tip; got tips={tips}"
    )


# ---------------------------------------------------------------------------
# FIX 3 (codex r5): backfill_enumerative_facts — fail fast without OPENAI_API_KEY
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_enumerative_backfill_live_fails_without_embedding_key(monkeypatch):
    """FIX 3: live backfill exits with code 2 before printing a rollback key
    when OPENAI_API_KEY is absent.

    Idempotency depends on embedding dedup (Leg-2 cosine dedup); without an
    embedder, NULL-embedding facts are stored and re-runs duplicate the full
    set.  The guard fires BEFORE the watermark print so no false rollback key
    is emitted.
    """
    from scripts.backfill_enumerative_facts import _run_backfill
    from nous.config import Settings

    # Patch Settings to return a config with an Anthropic key but no OpenAI key.
    fake_settings = Settings().model_copy(update={
        "anthropic_api_key": "sk-ant-fake",
        "openai_api_key": None,
    })
    monkeypatch.setattr(
        "scripts.backfill_enumerative_facts.Settings",
        lambda: fake_settings,
    )

    import io
    captured_stderr = io.StringIO()
    monkeypatch.setattr("sys.stderr", captured_stderr)

    exit_code = await _run_backfill(
        agent_id="nous-default",
        since=None,
        max_episodes=0,
        density_threshold=None,
        extraction_budget=0,
        dry_run=False,
    )

    assert exit_code == 2, f"Expected exit code 2, got {exit_code}"
    err = captured_stderr.getvalue()
    assert "OPENAI_API_KEY" in err, f"Expected OPENAI_API_KEY in stderr; got: {err!r}"


# ---------------------------------------------------------------------------
# Codex round-7 FIX 1: per-chunk failure isolation in process_transcript
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_process_transcript_partial_chunk_failure_no_raise(monkeypatch, settings_fixture):
    """FIX 1 (codex r7): if chunk 1 raises RuntimeError after chunk 0 stored facts,
    process_transcript must NOT propagate the exception — it returns the partial
    stored IDs from chunk 0 only, with truncated=True logged."""
    # Use chunk_size=600 and a 700-char transcript so chunk_text produces 2 chunks.
    long_chunk = "x" * 650
    two_chunk_transcript = long_chunk  # 650 chars > chunk_size=600 → 2 chunks

    settings = settings_fixture(
        enumerative_density_threshold=0.0,  # force-enumerable
        enumerative_max_facts_per_episode=1000,
        episode_chunk_size=600,
        episode_chunk_overlap=80,
        episode_chunk_min_transcript_chars=0,
    )

    heart = AsyncMock()
    heart.learn = AsyncMock(return_value=SimpleNamespace(id=uuid4()))
    embedder = AsyncMock()
    embedder.embed_batch = AsyncMock(side_effect=lambda texts: [[0.0] * 1536] * len(texts))

    # chunk 0 succeeds, chunk 1 raises
    monkeypatch.setattr(
        "nous.handlers.enumerative_extractor.call_background_llm_structured",
        AsyncMock(side_effect=[_CHUNK_FACTS, RuntimeError("simulated LLM failure on chunk 1")]),
    )

    ex = EnumerativeExtractor(heart=heart, settings=settings, llm_client=object(), embedder=embedder)
    # Must not raise — per-chunk failures are isolated
    stored_ids = await ex.process_transcript(two_chunk_transcript, episode_id=uuid4())

    # chunk 0 stored _CHUNK_FACTS (2 facts); chunk 1 raised before storing → 2 IDs total
    assert len(stored_ids) == 2, (
        f"Expected 2 IDs from chunk 0 only; got {len(stored_ids)}"
    )


@pytest.mark.asyncio
async def test_partial_chunk_failure_wiring_skips_legacy(monkeypatch):
    """FIX 1 (codex r7) wiring: when process_transcript returns a non-empty list
    (partial stores from chunk 0), extract_and_store must return those IDs and
    must NOT invoke the legacy candidate-facts path (heart.learn never called).

    Uses a 2-chunk side_effect on process_transcript at the wiring level so the
    test is independent of process_transcript's internals."""
    from uuid import uuid4 as _uuid4

    partial_ids = [_uuid4(), _uuid4()]  # 2 IDs from chunk 0

    # Simulate: chunk 0 stored 2 facts, chunk 1 raised inside process_transcript;
    # process_transcript caught it and returned the 2 partial IDs (no raise).
    mock_process = AsyncMock(return_value=partial_ids)
    monkeypatch.setattr(
        "nous.handlers.enumerative_extractor.EnumerativeExtractor.process_transcript",
        mock_process,
    )

    from nous.handlers import fact_extractor as fe_mod

    heart = _stub_heart_for_extractor()
    ext = fe_mod.FactExtractor.__new__(fe_mod.FactExtractor)
    ext._heart = heart
    ext._settings = _make_settings(flag_on=True, enumerative_density_threshold=0.0)
    ext._dedup_via_search = False
    ext._llm = object()  # non-None so the enumerative branch is entered

    result = await ext.extract_and_store(
        summary=_VALID_SUMMARY,
        episode_id=str(_uuid4()),
        transcript=_DENSE_TRANSCRIPT,
    )

    # Enumerative leg ran and returned partial IDs
    mock_process.assert_called_once()
    # Legacy candidate path must NOT have run (no heart.learn calls)
    assert heart._captured == [], (
        "legacy candidate path must NOT run when enumerative leg returns partial stored IDs"
    )
    # extract_and_store returns the partial enumerative IDs
    assert result == partial_ids, (
        f"Expected partial_ids={partial_ids}, got {result}"
    )


# ---------------------------------------------------------------------------
# Codex round-7 FIX 2: key_budget_exhausted — non-consuming peek with rollover
# ---------------------------------------------------------------------------


def test_key_budget_exhausted_reports_exhausted_and_resets_on_hour_boundary():
    """FIX 2 (codex r7): key_budget_exhausted() is non-consuming and rolls the
    hour bucket forward (resetting _key_calls) on a bucket mismatch.

    Sequence:
    1. cap=1, consume slot via _key_budget_ok() → _key_calls=1
    2. key_budget_exhausted() → True (budget spent)
    3. Simulate hour rollover: _key_bucket -= 1 (forces bucket mismatch)
    4. key_budget_exhausted() → False (_key_calls reset to 0 by rollover)
    5. _key_calls is still 0 (non-consuming: calling it twice doesn't increment)
    """
    from types import SimpleNamespace
    from nous.heart.facts import FactManager

    mgr = FactManager.__new__(FactManager)
    mgr._settings = SimpleNamespace(supersession_classifier_max_per_hour=1)
    mgr._key_bucket = -1
    mgr._key_calls = 0

    # Step 1: consume the one allowed slot
    ok = mgr._key_budget_ok()
    assert ok is True, "first call should be allowed"
    assert mgr._key_calls == 1

    # Step 2: budget exhausted
    assert mgr.key_budget_exhausted() is True, "budget should be exhausted after consuming 1/1"

    # Step 3: simulate next hour (decrement bucket so current bucket != stored)
    mgr._key_bucket -= 1

    # Step 4: key_budget_exhausted rolls the bucket, resets _key_calls, returns False
    assert mgr.key_budget_exhausted() is False, (
        "key_budget_exhausted must return False after bucket rollover resets counter"
    )
    assert mgr._key_calls == 0, (
        "bucket rollover inside key_budget_exhausted must reset _key_calls to 0"
    )

    # Step 5: call again — _key_calls must still be 0 (non-consuming)
    mgr.key_budget_exhausted()
    assert mgr._key_calls == 0, (
        "key_budget_exhausted must never increment _key_calls"
    )


# ---------------------------------------------------------------------------
# Codex round-9 P2: per-fact failure isolation in _store_batch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_store_batch_per_fact_failure_isolation():
    """Codex r9: heart.learn raises on fact 2 of 3 — _store_batch returns only
    the first fact's id, does NOT raise, and does NOT attempt the third fact.

    Before the fix _store_batch propagated the exception, causing
    process_transcript to treat the whole chunk as failed and return [] even
    though fact 1 was already committed to the DB."""
    first_id = uuid4()
    call_count = [0]

    async def _learn(fi, **kw):
        call_count[0] += 1
        if call_count[0] == 1:
            return SimpleNamespace(id=first_id)
        raise RuntimeError(f"simulated DB failure on call {call_count[0]}")

    heart = AsyncMock()
    heart.learn = AsyncMock(side_effect=_learn)

    settings = _make_settings()
    ex = EnumerativeExtractor(heart=heart, settings=settings, llm_client=object(), embedder=None)

    inputs = [
        FactInput(
            content=f"Content for fact {i} long enough to pass any validation check here.",
            subject_key=f"entity{i}",
            attribute_key="property",
        )
        for i in range(3)
    ]

    # Must not raise — per-fact failures are isolated
    result_pairs, store_failed = await ex._store_batch(inputs)

    # _store_batch returns (list[tuple[FactInput, UUID]], bool).
    # Only the first committed fact appears in the result; store_failed must be True.
    stored_uids = [uid for _, uid in result_pairs]
    assert stored_uids == [first_id], f"Expected [first_id] in stored uids; got {stored_uids}"
    assert store_failed is True, "store_failed must be True when Heart.learn raises mid-batch"
    # learn called twice: call 1 succeeds, call 2 raises → break (call 3 never reached)
    assert call_count[0] == 2, (
        f"Expected 2 learn calls (1 success + 1 raise, 3rd skipped); got {call_count[0]}"
    )


@pytest.mark.asyncio
async def test_partial_store_batch_wiring_skips_legacy(monkeypatch):
    """Codex r9 integration: heart.learn raises on the second fact of the first
    chunk — _store_batch returns [first_id] → process_transcript returns
    [first_id] → extract_and_store returns [first_id], legacy candidate path
    NOT invoked.

    Exercises the ACTUAL _store_batch mid-batch failure (not mocked at the
    process_transcript boundary), mirroring test_partial_chunk_failure_wiring_skips_legacy
    (r7) but at a deeper level."""
    from nous.handlers import fact_extractor as fe_mod

    first_id = uuid4()
    learn_calls = [0]

    class _HeartStub:
        """Heart stub that raises on the second learn call (simulates a DB error
        mid-batch in _store_batch).  Subsequent calls succeed so the test is not
        masked by legacy-path errors if the wiring accidentally reaches it."""
        _embeddings = None

        async def search_facts(self, *a, **kw):
            return []

        async def learn(self, fact_input, **kw):
            learn_calls[0] += 1
            if learn_calls[0] == 2:
                raise RuntimeError("simulated DB failure on enumerative fact 2")
            # Return a stable id so the result is predictable regardless of
            # which call produced it (whether enumerative or legacy).
            return SimpleNamespace(id=first_id)

    heart = _HeartStub()

    # LLM returns 2 facts for the single chunk — second will raise in heart.learn
    monkeypatch.setattr(
        "nous.handlers.enumerative_extractor.call_background_llm_structured",
        AsyncMock(return_value=_CHUNK_FACTS),
    )

    # Cap to 1 chunk so only the first chunk is processed — the partial-store
    # failure in that chunk is what we are testing.  _DENSE_TRANSCRIPT would
    # produce multiple chunks; later chunks would succeed and inflate the result.
    settings = _make_settings(
        flag_on=True,
        enumerative_density_threshold=0.0,
        enumerative_max_chunks_per_episode=1,
    )
    ext = fe_mod.FactExtractor.__new__(fe_mod.FactExtractor)
    ext._heart = heart
    ext._settings = settings
    ext._dedup_via_search = False
    ext._llm = object()  # non-None so the enumerative branch is entered
    ext._enumerative_extractor = None

    result = await ext.extract_and_store(
        summary=_VALID_SUMMARY,
        episode_id=str(uuid4()),
        transcript=_DENSE_TRANSCRIPT,
    )

    # With the fix: _store_batch isolates the failure → returns [first_id]
    # → process_transcript returns [first_id] (non-empty)
    # → extract_and_store returns those IDs without touching the legacy path.
    assert result == [first_id], (
        f"Expected [first_id] from enumerative partial store; got {result}"
    )
    # Exactly 2 learn calls: call 1 (enumerative fact 1, ok) + call 2 (raise).
    # If the legacy candidate path had run, _store_candidate_facts would issue
    # a third learn call → learn_calls[0] would be 3.
    assert learn_calls[0] == 2, (
        f"Expected exactly 2 learn calls (enumerative: 1 ok + 1 raise); "
        f"got {learn_calls[0]} — legacy path must NOT have been invoked"
    )


@pytest.mark.asyncio
async def test_mid_batch_store_failure_stops_later_chunks(monkeypatch, settings_fixture, caplog):
    """Codex r14 test (b): mid-batch Heart.learn failure in chunk-0 propagates
    store_failed=True from _store_batch → process_transcript logs a WARNING and
    BREAKs — chunk-1's LLM extraction is NEVER called.

    Verifies: only chunk-0's committed IDs returned, chunk-1 sentinel never
    invoked (call_count == 1), truncation WARNING logged."""
    transcript = "A" * 700  # two chunks: size=600, overlap=80

    settings = settings_fixture(
        enumerative_density_threshold=0.0,
        enumerative_max_facts_per_episode=1000,
        episode_chunk_size=600,
        episode_chunk_overlap=80,
        episode_chunk_min_transcript_chars=0,
    )

    first_id = uuid4()
    learn_calls = [0]

    async def _learn(fi, **kw):
        idx = learn_calls[0]
        learn_calls[0] += 1
        if idx == 0:
            return SimpleNamespace(id=first_id)
        raise RuntimeError("simulated store failure on second fact")

    heart_mock = AsyncMock()
    heart_mock.learn = AsyncMock(side_effect=_learn)

    embedder = AsyncMock()
    embedder.embed_batch = AsyncMock(side_effect=lambda texts: [[0.0] * 1536] * len(texts))

    chunk0_facts = {
        "facts": [
            {"content": "Fact A long enough to be valid: the server memory is eight gigabytes.",
             "subject_key": "server", "attribute_key": "memory"},
            {"content": "Fact B long enough to be valid: the server cpu is sixteen cores.",
             "subject_key": "server", "attribute_key": "cpu"},
        ]
    }
    chunk1_sentinel = {
        "facts": [
            {"content": "Fact C in chunk-1 that must never be extracted by the LLM.",
             "subject_key": "sentinel", "attribute_key": "value"},
        ]
    }

    llm_mock = AsyncMock(side_effect=[chunk0_facts, chunk1_sentinel])
    monkeypatch.setattr(
        "nous.handlers.enumerative_extractor.call_background_llm_structured",
        llm_mock,
    )

    ex = EnumerativeExtractor(
        heart=heart_mock, settings=settings, llm_client=object(), embedder=embedder
    )
    with caplog.at_level("WARNING"):
        stored_ids = await ex.process_transcript(transcript, episode_id=uuid4())

    # Only chunk-0's first committed fact — chunk-1 must not have been reached.
    assert stored_ids == [first_id], (
        f"Expected [first_id] only; got {stored_ids}"
    )
    # Exactly 1 LLM call: chunk-0 only; chunk-1 sentinel must never fire.
    assert llm_mock.call_count == 1, (
        f"chunk-1 LLM extraction must not happen; got {llm_mock.call_count} calls"
    )
    # Truncation warning must be logged for the mid-batch failure.
    assert any("mid-batch store failure" in r.message for r in caplog.records), (
        "WARNING must be logged when mid-batch store failure stops the chunk loop"
    )


# ---------------------------------------------------------------------------
# Codex round-10 FIX 1: in-run content dedup across chunk overlap
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_overlap_duplicate_dedup_first_ordinal_wins(monkeypatch, settings_fixture):
    """FIX 1 (codex r10): statements in the overlap window are extracted from both
    adjacent chunks; the in-run seen_contents filter drops the later-chunk copy,
    preserving the FIRST ordinal.

    chunk-0 → [X (pos 0), Y (pos 1)]
    chunk-1 → [Y (pos 0, verbatim dup from overlap), Z (pos 1)]

    Expected: 3 facts stored (Y once), stored Y has chunk-0 ordinal = 1.
    """
    # A 700-char transcript so chunk_text (size=600, overlap=80) produces 2 chunks.
    transcript = "A" * 700

    settings = settings_fixture(
        enumerative_density_threshold=0.0,  # force-enumerable
        enumerative_max_facts_per_episode=1000,
        episode_chunk_size=600,
        episode_chunk_overlap=80,
        episode_chunk_min_transcript_chars=0,
    )

    stored_args: list = []

    async def _learn(fi, **kw):
        stored_args.append(fi)
        return SimpleNamespace(id=uuid4())

    heart_mock = AsyncMock()
    heart_mock.learn = AsyncMock(side_effect=_learn)

    embedder = AsyncMock()
    embedder.embed_batch = AsyncMock(side_effect=lambda texts: [[0.0] * 1536] * len(texts))

    shared_y_content = "Y fact: the server memory allocation is sixteen gigabytes total."

    chunk0_facts = {
        "facts": [
            {
                "content": "X fact: the CPU core count is eight for each production node.",
                "subject_key": "cpu",
                "attribute_key": "core_count",
            },
            {
                "content": shared_y_content,
                "subject_key": "server",
                "attribute_key": "memory_allocation",
            },
        ]
    }
    chunk1_facts = {
        "facts": [
            {
                "content": shared_y_content,  # verbatim overlap duplicate
                "subject_key": "server",
                "attribute_key": "memory_allocation",
            },
            {
                "content": "Z fact: the disk storage capacity is two terabytes per node.",
                "subject_key": "disk",
                "attribute_key": "storage_capacity",
            },
        ]
    }

    monkeypatch.setattr(
        "nous.handlers.enumerative_extractor.call_background_llm_structured",
        AsyncMock(side_effect=[chunk0_facts, chunk1_facts]),
    )

    ex = EnumerativeExtractor(
        heart=heart_mock, settings=settings, llm_client=object(), embedder=embedder
    )
    stored_ids = await ex.process_transcript(transcript, episode_id=uuid4())

    # 3 facts stored: X (chunk-0 pos 0), Y (chunk-0 pos 1), Z (chunk-1 pos 1)
    # Y's chunk-1 copy (pos 0) is dropped by the in-run seen_contents filter.
    assert len(stored_ids) == 3, (
        f"Expected 3 stored IDs (X, Y once, Z); got {len(stored_ids)}"
    )

    # Y must appear exactly once and carry the FIRST ordinal: chunk_index=0, pos=1 → 1
    y_args = [fi for fi in stored_args if fi.content == shared_y_content]
    assert len(y_args) == 1, (
        f"Y content must be stored exactly once; found {len(y_args)} copies"
    )
    assert y_args[0].source_ordinal == 0 * 1_000_000 + 1, (
        f"Y's ordinal must be the chunk-0 ordinal (1); got {y_args[0].source_ordinal}"
    )


# ---------------------------------------------------------------------------
# Codex round-10 FIX 2: fail open on malformed classifier confidence
# ---------------------------------------------------------------------------


@pytest.mark.postgres_only
async def test_resolve_key_conflict_pair_malformed_confidence_keep_both(heart, monkeypatch):
    """FIX 2 (codex r10): resolve_key_conflict_pair must not raise when the
    classifier returns non-numeric confidence (e.g. "high") — fail open to
    KEEP BOTH (return False), no exception, no supersession written."""
    heart.facts._settings = heart.facts._settings.model_copy(
        update={"supersession_key_resolution_enabled": True}
    )

    f_old = await heart.learn(
        FactInput(
            content="The API response time averaged at two hundred milliseconds measured daily.",
            subject_key="api",
            attribute_key="response_time",
        )
    )
    f_new = await heart.learn(
        FactInput(
            content="The API response time averaged at three hundred milliseconds measured daily.",
            subject_key="api",
            attribute_key="response_time",
        )
    )

    # Classifier returns UPDATE with non-numeric confidence — must not raise
    monkeypatch.setattr(
        heart.facts,
        "_classify_fact_pair",
        AsyncMock(return_value={"relation": "UPDATE", "current_fact": "new", "confidence": "high"}),
    )

    result = await heart.facts.resolve_key_conflict_pair(
        f_old.id, f_new.id, f_old.content, f_new.content
    )
    assert result is False, (
        f"Expected False (fail-open on malformed confidence 'high'); got {result}"
    )

    async with heart.db.session() as s:
        old_row = await s.get(Fact, f_old.id)
        new_row = await s.get(Fact, f_new.id)
        assert old_row.superseded_by is None, "old fact must not be superseded"
        assert old_row.active is not False, "old fact must remain active"
        assert new_row.superseded_by is None, "new fact must not be superseded"
        assert new_row.active is not False, "new fact must remain active"


@pytest.mark.postgres_only
async def test_resolve_key_conflicts_malformed_confidence_keep_both(heart, session, monkeypatch):
    """FIX 2 (codex r10): write-time _resolve_key_conflicts (inside Heart.learn)
    must not raise on non-numeric confidence — both facts kept active, no
    transaction abort."""
    heart.facts._settings = heart.facts._settings.model_copy(
        update={"supersession_key_resolution_enabled": True}
    )

    old_r = await heart.learn(
        FactInput(
            content="The deployment region is us-east-1 primary availability zone.",
            subject_key="deployment",
            attribute_key="region",
        ),
        session=session,
    )

    # Malformed confidence on the second write — must fail-open
    monkeypatch.setattr(
        heart.facts,
        "_classify_fact_pair",
        AsyncMock(return_value={"relation": "UPDATE", "current_fact": "new", "confidence": "high"}),
    )

    new_r = await heart.learn(
        FactInput(
            content="The deployment region is us-west-2 primary availability zone.",
            subject_key="deployment",
            attribute_key="region",
        ),
        session=session,
    )

    await session.flush()

    old_row = await session.get(Fact, old_r.id)
    new_row = await session.get(Fact, new_r.id)
    assert old_row.superseded_by is None, "old fact must not be superseded on malformed confidence"
    assert old_row.active is not False, "old fact must remain active"
    assert new_row.superseded_by is None, "new fact must not be superseded on malformed confidence"
    assert new_row.active is not False, "new fact must remain active"


# ---------------------------------------------------------------------------
# codex r11 fixes
# ---------------------------------------------------------------------------


@pytest.mark.postgres_only
async def test_confirm_duplicate_fills_adjudication_metadata(heart, session):
    """codex r11 FIX 1: _confirm_duplicate must copy adjudication metadata onto
    an unkeyed legacy row when the new input carries them (fill-if-empty only).
    Uses identical content + precomputed_embedding so cosine=1.0 > threshold
    → _classify_dupe_in_band returns None → confirm path fires.
    """
    # Unit vector: two identical vectors produce cosine=1.0.
    vec = [1.0] + [0.0] * 1535

    # First learn: unkeyed legacy row.
    r1 = await heart.learn(
        FactInput(
            content="The application cache hit rate is above ninety percent.",
            subject="application",
        ),
        session=session,
        precomputed_embedding=vec,
    )
    await session.flush()
    row = await session.get(Fact, r1.id)
    assert row.subject_key is None
    assert row.attribute_key is None
    assert row.source_ordinal is None
    assert not row.overrides_prior

    # Second learn: same content + same vector → confirm path; input carries keys.
    r2 = await heart.learn(
        FactInput(
            content="The application cache hit rate is above ninety percent.",
            subject="application",
            subject_key="application",
            attribute_key="cache hit rate",
            source_ordinal=3_000_000,
            overrides_prior=True,
        ),
        session=session,
        precomputed_embedding=vec,
    )
    # dedup confirm: returned id == original fact id
    assert r2.id == r1.id, "confirm path must return the existing fact"

    await session.flush()
    row = await session.get(Fact, r1.id, populate_existing=True)
    assert row.subject_key == "application", "subject_key must be filled from second input"
    assert row.attribute_key == "cache hit rate", "attribute_key must be filled from second input"
    assert row.source_ordinal == 3_000_000, "source_ordinal must be filled from second input"
    assert row.overrides_prior is True, "overrides_prior must be upgraded to True"

    # Third learn: different keys — must NOT overwrite now that row is keyed.
    await heart.learn(
        FactInput(
            content="The application cache hit rate is above ninety percent.",
            subject="application",
            subject_key="different subject",
            attribute_key="different attr",
            source_ordinal=999,
            overrides_prior=False,
        ),
        session=session,
        precomputed_embedding=vec,
    )
    await session.flush()
    row = await session.get(Fact, r1.id, populate_existing=True)
    assert row.subject_key == "application", "subject_key must not be overwritten by third learn"
    assert row.attribute_key == "cache hit rate", "attribute_key must not be overwritten by third learn"
    assert row.source_ordinal == 3_000_000, "source_ordinal must not be overwritten by third learn"
    assert row.overrides_prior is True, "overrides_prior must not be downgraded by third learn"


@pytest.mark.postgres_only
async def test_resolve_key_conflicts_loss_skips_contradiction_scan(heart, session, monkeypatch):
    """codex r11 FIX 2: when the new fact loses keyed resolution (old wins),
    _find_contradiction must NOT be invoked — an inactive loser must not generate
    contradiction edges or trigger domain compaction.

    Gate-1 D1: CONTRADICTION now resolves by statement order, not current_fact
    (which the classifier mock below still sets, but the resolver ignores) —
    so the fixture makes the new fact lose via ORDER instead: same episode,
    LOWER source_ordinal than the old fact.
    """
    heart.facts._settings = heart.facts._settings.model_copy(
        update={"supersession_key_resolution_enabled": True}
    )

    # Orthogonal unit vectors: cosine between old and new = 0 → no dedup.
    vec_old = [1.0] + [0.0] * 1535
    vec_new = [0.0, 1.0] + [0.0] * 1534

    ep_id = await _insert_episode(session)

    # Learn old fact via FactManager directly to pass check_contradictions=False,
    # so the sentinel is not disturbed by the first learn's contradiction scan.
    await heart.facts.learn(
        FactInput(
            content="The server cluster handles 1000 requests per second at peak.",
            subject_key="server cluster",
            attribute_key="throughput",
            source_episode_id=ep_id,
            source_ordinal=200,
        ),
        session=session,
        precomputed_embedding=vec_old,
        check_contradictions=False,
    )

    # current_fact is now ignored for CONTRADICTION — old wins via the LOWER
    # ordinal on the new fact below, not this verdict.
    monkeypatch.setattr(
        heart.facts,
        "_classify_fact_pair",
        AsyncMock(return_value={"relation": "CONTRADICTION", "current_fact": "old", "confidence": 0.9}),
    )
    # Sentinel: _find_contradiction must NOT fire for a losing (inactive) fact.
    find_contradiction_sentinel = AsyncMock(return_value=None)
    monkeypatch.setattr(heart.facts, "_find_contradiction", find_contradiction_sentinel)

    new_r = await heart.facts.learn(
        FactInput(
            content="The server cluster handles 500 requests per second at peak.",
            subject_key="server cluster",
            attribute_key="throughput",
            source_episode_id=ep_id,
            source_ordinal=100,  # LOWER than old's 200 → new fact loses via order
        ),
        session=session,
        precomputed_embedding=vec_new,
    )

    find_contradiction_sentinel.assert_not_called()

    # Confirm the new fact is inactive (lost keyed resolution).
    await session.flush()
    new_row = await session.get(Fact, new_r.id)
    assert new_row.active is False or new_row.superseded_by is not None, (
        "new fact must be inactive after losing keyed resolution"
    )


# ---------------------------------------------------------------------------
# Codex round-12: F075 event_date on the enumerative extraction path
# ---------------------------------------------------------------------------


def test_enumerative_event_date_flag_on_valid_date(settings_fixture):
    """FIX (codex r12): flag ON + valid ISO date → event_date stored, classified_at non-None."""
    from datetime import date as _date

    settings = settings_fixture(temporal_extraction_enabled=True)
    ex = EnumerativeExtractor.__new__(EnumerativeExtractor)
    ex._settings = settings

    raw = [
        {
            "content": "BTC price on March tenth was thirty thousand dollars.",
            "subject_key": "btc",
            "attribute_key": "price",
            "event_date": "2026-03-10",
        }
    ]
    inputs = ex._to_fact_inputs(raw, chunk_index=0, episode_id=uuid4())
    assert len(inputs) == 1
    fi = inputs[0]
    assert fi.event_date == _date(2026, 3, 10), (
        f"event_date must be date(2026,3,10); got {fi.event_date!r}"
    )
    assert fi.event_date_classified_at is not None, (
        "event_date_classified_at must be set when flag is ON and date is valid"
    )


def test_enumerative_event_date_flag_on_malformed_date(settings_fixture):
    """FIX (codex r12): flag ON + malformed date → event_date=None AND classified_at=None (backfill-eligible)."""
    settings = settings_fixture(temporal_extraction_enabled=True)
    ex = EnumerativeExtractor.__new__(EnumerativeExtractor)
    ex._settings = settings

    raw = [
        {
            "content": "Revenue in March was fifty million dollars total.",
            "subject_key": "revenue",
            "attribute_key": "amount",
            "event_date": "March 10",  # malformed — validator will drop it
        }
    ]
    inputs = ex._to_fact_inputs(raw, chunk_index=0, episode_id=uuid4())
    assert len(inputs) == 1
    fi = inputs[0]
    assert fi.event_date is None, (
        f"malformed date must be dropped by validator; event_date={fi.event_date!r}"
    )
    assert fi.event_date_classified_at is None, (
        "classified_at must stay None when date is malformed so F075.1 backfill can retry"
    )


def test_enumerative_event_date_flag_off_ignored(settings_fixture):
    """GOLDEN (codex r12): flag OFF → event_date in LLM output ignored; both fields None."""
    settings = settings_fixture()  # temporal_extraction_enabled absent → defaults to False
    ex = EnumerativeExtractor.__new__(EnumerativeExtractor)
    ex._settings = settings

    raw = [
        {
            "content": "ETH price on March tenth was two thousand dollars.",
            "subject_key": "eth",
            "attribute_key": "price",
            "event_date": "2026-03-10",  # LLM emits a date, but flag is OFF
        }
    ]
    inputs = ex._to_fact_inputs(raw, chunk_index=0, episode_id=uuid4())
    assert len(inputs) == 1
    fi = inputs[0]
    assert fi.event_date is None, (
        "flag OFF: event_date must be ignored regardless of LLM output"
    )
    assert fi.event_date_classified_at is None, (
        "flag OFF: classified_at must be None (byte-identical current behavior)"
    )


@pytest.mark.asyncio
async def test_enumerative_seen_set_distinct_dates_preserved(monkeypatch, settings_fixture):
    """FIX (codex r12): same content + two different event_dates → both stored (distinct events).
    Same content + same date (overlap) → one stored (overlap dedup preserved).
    """
    from datetime import date as _date

    transcript = "A" * 700  # produces 2 chunks with chunk_size=600, overlap=80

    settings = settings_fixture(
        temporal_extraction_enabled=True,
        enumerative_density_threshold=0.0,  # force-enumerable
        enumerative_max_facts_per_episode=1000,
        episode_chunk_size=600,
        episode_chunk_overlap=80,
        episode_chunk_min_transcript_chars=0,
    )

    stored_args: list = []

    async def _learn(fi, **kw):
        stored_args.append(fi)
        return SimpleNamespace(id=uuid4())

    heart_mock = AsyncMock()
    heart_mock.learn = AsyncMock(side_effect=_learn)
    embedder = AsyncMock()
    embedder.embed_batch = AsyncMock(side_effect=lambda texts: [[0.0] * 1536] * len(texts))

    shared_content = "ETH price was two thousand dollars on the specified date."

    # chunk-0: one dated fact (2026-03-10)
    # chunk-1: same content with 2026-03-11 (distinct date → preserved),
    #          plus exact same (content + 2026-03-10) as chunk-0 (overlap dup → dropped)
    chunk0_facts = {
        "facts": [
            {
                "content": shared_content,
                "subject_key": "eth",
                "attribute_key": "price",
                "event_date": "2026-03-10",
            },
        ]
    }
    chunk1_facts = {
        "facts": [
            {
                "content": shared_content,
                "subject_key": "eth",
                "attribute_key": "price",
                "event_date": "2026-03-11",  # different date → distinct event, NOT deduped
            },
            {
                "content": shared_content,
                "subject_key": "eth",
                "attribute_key": "price",
                "event_date": "2026-03-10",  # same as chunk-0 → overlap dup, DROPPED
            },
        ]
    }

    monkeypatch.setattr(
        "nous.handlers.enumerative_extractor.call_background_llm_structured",
        AsyncMock(side_effect=[chunk0_facts, chunk1_facts]),
    )

    ex = EnumerativeExtractor(
        heart=heart_mock, settings=settings, llm_client=object(), embedder=embedder
    )
    stored_ids = await ex.process_transcript(transcript, episode_id=uuid4())

    # 2 facts: (content, 2026-03-10) + (content, 2026-03-11)
    # The third (content, 2026-03-10) from chunk-1 is dropped as overlap duplicate.
    assert len(stored_ids) == 2, (
        f"Expected 2 stored IDs (distinct dates); got {len(stored_ids)}"
    )
    dates_stored = {fi.event_date for fi in stored_args}
    assert _date(2026, 3, 10) in dates_stored, "2026-03-10 fact must be stored"
    assert _date(2026, 3, 11) in dates_stored, "2026-03-11 fact must be stored"


# ---------------------------------------------------------------------------
# Codex round-13 FIX 1: budget-exhausted sweep breaks without advancing cursor
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_key_conflict_sweep_budget_exhausted_cursor_stays_at_last_resolved():
    """Codex r13 FIX 1: when key_budget_exhausted() fires at the TOP of the
    loop for pair-2, the cursor must NOT advance past pair-1's 4-tuple.

    2 pairs in a page (max_pairs=2), budget cap=1.  The first resolve call
    consumes the single slot (classifier UNRELATED → keep-both).  On the next
    iteration key_budget_exhausted() returns True → handler breaks before
    calling resolve again.

    Assertions:
    - cursor == pair-1's 4-tuple (not pair-2's)
    - resolve called exactly once
    """
    from datetime import datetime, timezone
    from nous.handlers.sleep_handler import SleepHandler

    ts = datetime(2026, 5, 1, tzinfo=timezone.utc)
    id_f1 = uuid4()
    id_f2a = uuid4()
    id_f2b = uuid4()

    pair1 = {"id1": id_f1, "id2": id_f2a, "c1": "c1a", "c2": "c2a", "ts1": ts, "ts2": ts}
    pair2 = {"id1": id_f1, "id2": id_f2b, "c1": "c1b", "c2": "c2b", "ts1": ts, "ts2": ts}

    expected_cursor = (pair1["ts1"], pair1["id1"], pair1["ts2"], pair1["id2"])

    # Simulate budget=1: resolve call increments a counter; peek returns True
    # once the counter reaches the cap (mirrors _key_budget_ok / key_budget_exhausted).
    key_calls = [0]
    cap = 1

    def _key_budget_exhausted() -> bool:
        return key_calls[0] >= cap

    resolve_call_count = [0]

    async def _resolve(id1, id2, c1, c2) -> bool:
        resolve_call_count[0] += 1
        key_calls[0] += 1  # mirrors _key_budget_ok() consuming a slot inside resolve
        return False  # UNRELATED → keep-both

    heart_mock = AsyncMock()
    heart_mock.facts.find_key_conflict_pairs = AsyncMock(return_value=[pair1, pair2])
    heart_mock.facts.resolve_key_conflict_pair = _resolve
    heart_mock.facts.key_budget_exhausted = _key_budget_exhausted

    settings = SimpleNamespace(
        supersession_key_resolution_enabled=True,
        supersession_sweep_max_pairs=2,
        supersession_classifier_max_per_hour=1,
    )
    handler = SleepHandler(
        brain=AsyncMock(),
        heart=heart_mock,
        settings=settings,
        bus=MagicMock(),
        llm_client=MagicMock(),
    )
    handler._llm = MagicMock()
    handler._interrupted = False

    stats: dict = {}
    result = await handler._phase_sweep_key_conflicts(stats)

    assert result is True
    assert resolve_call_count[0] == 1, (
        f"resolve must be called exactly once (budget exhausted before pair-2); "
        f"got {resolve_call_count[0]}"
    )
    assert handler._key_sweep_cursor == expected_cursor, (
        f"cursor must be pair-1's 4-tuple {expected_cursor}; "
        f"got {handler._key_sweep_cursor} — budget exhaustion must not advance "
        "past the unadjudicated pair"
    )


# ---------------------------------------------------------------------------
# Codex round-13 FIX 2: budget-stopped pairs stay unseen in run_sweep
# ---------------------------------------------------------------------------


@pytest.mark.postgres_only
async def test_backfill_budget_stops_pair_not_marked_seen(heart, monkeypatch):
    """Codex r13 FIX 2: when key_budget_exhausted() fires on pair-2 BEFORE
    seen.add, that pair is NOT added to the seen set so a re-run can retry it.

    Seeds 2 distinct same-key pairs (2 × 2 facts with different attribute_keys).
    Budget cap=1.  First resolve consumes the slot via _key_budget_ok() inside
    the real resolve path (classifier mocked to UNRELATED).  Second pair hits
    the budget check before seen.add.

    Assertions:
    - pairs_examined == 1 (second pair not examined)
    - budget_stops >= 1
    - resolve_key_conflict_pair invoked exactly once
      (proxy: _classify_fact_pair called once)
    """
    from scripts.backfill_supersession import run_sweep

    unique_agent = f"t13c-{uuid4().hex[:8]}"
    heart.facts.agent_id = unique_agent

    # Pair A: two facts sharing subject_key + attribute_key "rate_a"
    await heart.learn(FactInput(
        content="The pipeline ingestion rate is one thousand records per minute at peak.",
        subject_key="r13_pipeline",
        attribute_key="rate_a",
    ))
    await heart.learn(FactInput(
        content="The pipeline ingestion rate is two thousand records per minute at peak.",
        subject_key="r13_pipeline",
        attribute_key="rate_a",
    ))

    # Pair B: two facts sharing subject_key + attribute_key "rate_b"
    await heart.learn(FactInput(
        content="The pipeline processing rate is one hundred batches per hour at capacity.",
        subject_key="r13_pipeline",
        attribute_key="rate_b",
    ))
    await heart.learn(FactInput(
        content="The pipeline processing rate is two hundred batches per hour at capacity.",
        subject_key="r13_pipeline",
        attribute_key="rate_b",
    ))

    classify_call_count = [0]

    async def _classify(c1, c2):
        classify_call_count[0] += 1
        return {"relation": "UNRELATED", "current_fact": "new", "confidence": 0.9}

    monkeypatch.setattr(heart.facts, "_classify_fact_pair", _classify)

    settings = heart.facts._settings.model_copy(update={
        "supersession_key_resolution_enabled": True,
        "supersession_classifier_max_per_hour": 1,
    })

    result = await run_sweep(heart, settings, max_pairs=0, batch_size=25, dry_run=False)

    assert result["pairs_examined"] == 1, (
        f"expected 1 pair examined (budget stops second); got {result['pairs_examined']}"
    )
    assert result["budget_stops"] >= 1, (
        f"expected budget_stops>=1; got {result['budget_stops']}"
    )
    assert classify_call_count[0] == 1, (
        f"classifier must be called exactly once (pair-1 only); "
        f"got {classify_call_count[0]}"
    )


# ---------------------------------------------------------------------------
# Codex round-13 FIX 3: seen_contents updated only after successful storage
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Codex r15 FIX 1: distinguish failed extraction from empty extraction
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_extract_chunk_failure_is_loud_and_continues(monkeypatch, settings_fixture, caplog):
    """R15 FIX 1 (a): LLM helper returns None for chunk 0 (API failure), valid fact for chunk 1.
    chunk-1 fact IS stored; a WARNING mentioning chunk 0 is logged; truncated=True fires."""
    transcript = "A" * 700  # two chunks: size=600, overlap=80

    settings = settings_fixture(
        enumerative_density_threshold=0.0,
        enumerative_max_facts_per_episode=1000,
        episode_chunk_size=600,
        episode_chunk_overlap=80,
        episode_chunk_min_transcript_chars=0,
    )

    chunk1_fact = {
        "facts": [
            {
                "content": "The primary cache cluster has sixteen nodes active in production.",
                "subject_key": "cache cluster",
                "attribute_key": "node_count",
            }
        ]
    }
    stored_id = uuid4()
    heart_mock = AsyncMock()
    heart_mock.learn = AsyncMock(return_value=SimpleNamespace(id=stored_id))

    embedder = AsyncMock()
    embedder.embed_batch = AsyncMock(side_effect=lambda texts: [[0.0] * 1536] * len(texts))

    # chunk 0: helper returns None (API failure); chunk 1: valid extraction
    monkeypatch.setattr(
        "nous.handlers.enumerative_extractor.call_background_llm_structured",
        AsyncMock(side_effect=[None, chunk1_fact]),
    )

    ex = EnumerativeExtractor(
        heart=heart_mock, settings=settings, llm_client=object(), embedder=embedder
    )
    with caplog.at_level("WARNING"):
        stored_ids = await ex.process_transcript(transcript, episode_id=uuid4())

    # chunk-1 fact must be stored despite chunk-0 failure
    assert stored_id in stored_ids, "chunk-1 fact must be stored when chunk-0 extraction fails"
    # truncation WARNING must be logged mentioning chunk 0
    assert any(
        "extraction failed" in r.message and "chunk 0" in r.message
        for r in caplog.records
    ), "WARNING must be logged mentioning chunk 0 failure"
    # overall truncation warning also fires (truncated=True)
    assert any("TRUNCATED" in r.message for r in caplog.records), (
        "end-of-run truncation WARNING must fire when truncated=True"
    )


@pytest.mark.asyncio
async def test_empty_extraction_is_silent(monkeypatch, settings_fixture, caplog):
    """R15 FIX 1 (b): well-formed {"facts": []} response → no chunk-failure WARNING,
    truncated stays False (no end-of-run truncation log either)."""
    transcript = "A" * 100  # one short chunk

    settings = settings_fixture(
        enumerative_density_threshold=0.0,
        enumerative_max_facts_per_episode=1000,
        episode_chunk_size=600,
        episode_chunk_overlap=80,
        episode_chunk_min_transcript_chars=0,
    )

    heart_mock = AsyncMock()
    heart_mock.learn = AsyncMock(side_effect=AssertionError("heart.learn must not be called for empty extraction"))
    embedder = AsyncMock()

    monkeypatch.setattr(
        "nous.handlers.enumerative_extractor.call_background_llm_structured",
        AsyncMock(return_value={"facts": []}),
    )

    ex = EnumerativeExtractor(
        heart=heart_mock, settings=settings, llm_client=object(), embedder=embedder
    )
    with caplog.at_level("WARNING"):
        stored_ids = await ex.process_transcript(transcript, episode_id=uuid4())

    assert stored_ids == [], "no facts should be stored for empty extraction"
    # No chunk-failure warning
    assert not any(
        "extraction failed" in r.message for r in caplog.records
    ), "empty extraction must not log a failure WARNING"
    # No truncation warning (truncated stays False)
    assert not any("TRUNCATED" in r.message for r in caplog.records), (
        "empty extraction must not trigger truncation WARNING"
    )


@pytest.mark.asyncio
async def test_failed_store_does_not_block_retry_in_later_chunk(monkeypatch, settings_fixture, caplog):
    """Codex r14 update (was r13 FIX 3): when Heart.learn raises mid-batch in chunk-0,
    _store_batch returns (stored_pairs, store_failed=True).  process_transcript logs a
    warning, sets truncated=True, and BREAKs — chunk-1 is NEVER attempted.

    The seen_contents fix (r13) still applies: Y is NOT added to seen_contents on failure.
    But because the loop breaks after chunk-0's store failure, the retry cannot occur.

    chunk-0 batch = [X, Y]  → Y's learn raises → _store_batch returns ([X], True)
                             → process_transcript breaks → chunk-1 never attempted
    Result: only X stored; 2 learn calls total; 1 LLM call; truncation WARNING logged.
    """
    transcript = "A" * 700  # two chunks: size=600, overlap=80

    settings = settings_fixture(
        enumerative_density_threshold=0.0,
        enumerative_max_facts_per_episode=1000,
        episode_chunk_size=600,
        episode_chunk_overlap=80,
        episode_chunk_min_transcript_chars=0,
    )

    x_id = uuid4()
    learn_results = [
        SimpleNamespace(id=x_id),  # call 1: X in chunk-0 → ok
        RuntimeError("Y fails"),    # call 2: Y in chunk-0 → raises → loop breaks
        # No call 3 or 4: chunk-1 is never extracted
    ]
    learn_calls = [0]

    async def _learn(fi, **kw):
        idx = learn_calls[0]
        learn_calls[0] += 1
        r = learn_results[idx]
        if isinstance(r, Exception):
            raise r
        return r

    heart_mock = AsyncMock()
    heart_mock.learn = AsyncMock(side_effect=_learn)

    embedder = AsyncMock()
    embedder.embed_batch = AsyncMock(side_effect=lambda texts: [[0.0] * 1536] * len(texts))

    shared_y_content = "Y fact: the worker thread count is sixteen for parallel processing."
    chunk0_facts = {
        "facts": [
            {"content": "X fact: the queue depth limit is one thousand messages maximum.",
             "subject_key": "queue", "attribute_key": "depth_limit"},
            {"content": shared_y_content,
             "subject_key": "worker", "attribute_key": "thread_count"},
        ]
    }
    chunk1_facts = {
        "facts": [
            {"content": shared_y_content,  # overlap copy of Y — never reached
             "subject_key": "worker", "attribute_key": "thread_count"},
            {"content": "Z fact: the retry backoff delay is five seconds exponential.",
             "subject_key": "retry", "attribute_key": "backoff_delay"},
        ]
    }

    llm_mock = AsyncMock(side_effect=[chunk0_facts, chunk1_facts])
    monkeypatch.setattr(
        "nous.handlers.enumerative_extractor.call_background_llm_structured",
        llm_mock,
    )

    ex = EnumerativeExtractor(
        heart=heart_mock, settings=settings, llm_client=object(), embedder=embedder
    )
    with caplog.at_level("WARNING"):
        stored_ids = await ex.process_transcript(transcript, episode_id=uuid4())

    # r14: mid-batch failure stops the chunk loop — only X (committed before failure) is stored.
    assert len(stored_ids) == 1, (
        f"Expected 1 stored ID (X only); got {len(stored_ids)} — "
        "chunk-1 must not be attempted when chunk-0's store fails"
    )
    assert x_id in stored_ids, "X must be stored from chunk-0"
    # chunk-1 was never extracted (only 1 LLM call for chunk-0)
    assert llm_mock.call_count == 1, (
        f"Expected 1 LLM call (chunk-0 only); got {llm_mock.call_count} — "
        "chunk-1 extraction must not happen after store failure"
    )
    # learn called only twice: X ok, Y raise → break
    assert learn_calls[0] == 2, (
        f"Expected 2 learn calls (X ok + Y raise); got {learn_calls[0]}"
    )
    # Truncation warning must be logged
    assert any("mid-batch store failure" in r.message for r in caplog.records), (
        "truncation WARNING must be logged when mid-batch store failure stops the chunk loop"
    )



# ---------------------------------------------------------------------------
# Tier-1 category integrity (2026-07-24): the enumerative extractor is a
# document/transcript atomizer — its atoms must never enter the always-on
# User Profile. Enum removal + post-map drift guard (mirrors #571 sleep).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tier1", ["preference", "person", "rule"])
def test_enumerative_tier1_category_post_mapped_to_technical(settings_fixture, tier1):
    """A drifted tier-1 category on an enumerative fact is stored as technical."""
    ex = EnumerativeExtractor.__new__(EnumerativeExtractor)
    ex._settings = settings_fixture()

    raw = [{
        "content": "The subject has some documented attribute value here.",
        "subject_key": "subject",
        "attribute_key": "attribute",
        "category": tier1,
    }]
    inputs = ex._to_fact_inputs(raw, chunk_index=0, episode_id=uuid4())
    assert len(inputs) == 1
    assert inputs[0].category == "technical", (
        f"tier-1 category {tier1!r} must post-map to technical; "
        f"got {inputs[0].category!r}"
    )


def test_enumerative_schema_enum_excludes_tier1_categories():
    """The schema offered to the LLM must not advertise the tier-1 categories."""
    from nous.handlers.enumerative_extractor import _EXTRACTION_SCHEMA

    enum = _EXTRACTION_SCHEMA["properties"]["facts"]["items"]["properties"]["category"]["enum"]
    for tier1 in ("preference", "person", "rule"):
        assert tier1 not in enum, f"{tier1!r} must not be in the enumerative category enum"


def test_enumerative_non_tier1_category_passes_through(settings_fixture):
    """A non-tier-1 category (concept) is stored unchanged — map is tier-1-only."""
    ex = EnumerativeExtractor.__new__(EnumerativeExtractor)
    ex._settings = settings_fixture()

    raw = [{
        "content": "The subject has some documented attribute value here.",
        "subject_key": "subject",
        "attribute_key": "attribute",
        "category": "concept",
    }]
    inputs = ex._to_fact_inputs(raw, chunk_index=0, episode_id=uuid4())
    assert len(inputs) == 1
    assert inputs[0].category == "concept"
