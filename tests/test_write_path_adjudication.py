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
    assert normalize_key("Tim's Laptop") == "tims laptop"
    assert normalize_key("  RED   Car!! ") == "red car"
    assert normalize_key("") is None
    assert normalize_key("   ") is None
    assert len(normalize_key("x" * 500)) <= 200


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
async def test_r2_row9_contradiction_current_fact_beats_ordinal(heart, session, monkeypatch):
    """Row 9: CONTRADICTION + current_fact=old, new has HIGHER ordinal → NEW superseded by old.
    Devil-2 #1: CONTRADICTION verdict is resolved ONLY by current_fact; ordinal is irrelevant."""
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
            source_ordinal=2,  # higher ordinal — but CONTRADICTION ignores it
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


# Row 10 ─────────────────────────────────────────────────────────────────────

@pytest.mark.postgres_only
async def test_r2_row10_contradiction_ambiguous_keep_both(heart, session, monkeypatch):
    """Row 10: CONTRADICTION conf 0.9 but current_fact missing/ambiguous → NO supersession."""
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


# Row 11 ─────────────────────────────────────────────────────────────────────

@pytest.mark.postgres_only
async def test_r2_row11_keyed_fact_skips_supersede_by_subject(heart, session, monkeypatch):
    """Row 11: fact with subject AND subject_key → _supersede_by_subject NOT invoked.
    Risk-2 #1: the legacy path is uncapped; keyed facts must bypass it unconditionally."""
    sentinel = AsyncMock()
    monkeypatch.setattr(heart.facts, "_supersede_by_subject", sentinel)

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
