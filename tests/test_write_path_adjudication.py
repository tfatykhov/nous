"""Write-path adjudication (R1 enumerative extraction + R2 store-time supersession)."""
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from nous.heart.schemas import FactInput
from nous.storage.models import Fact


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
