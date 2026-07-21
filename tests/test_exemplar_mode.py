"""F086 ICL exemplar mode tests."""

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import select

from nous.handlers.exemplar_ingest import ingest_exemplars
from nous.heart.exemplars import (
    ExemplarPair,  # noqa: F401 -- imported to assert the exported type exists
    exemplar_density,
    is_exemplar_stream,
    parse_exemplars,
    parse_label,
)
from nous.storage.models import Episode, Fact

logger = logging.getLogger(__name__)

PURE_STREAM = "how do I reset my card pin\nlabel: 21\nmy card is lost\nlabel: 41\nwhat's the exchange rate\nlabel: 32\n"
TRANSCRIPT_STREAM = (
    "User: how do I reset my card pin\nlabel: 21\n"
    "Assistant: Noted.\n"
    "User: my card is lost\nlabel: 41\n"
    "Assistant: Stored.\n"
    "User: what's the exchange rate\nlabel: 32\n"
)


class TestExemplarParser:
    def test_density_pure_stream_is_high(self):
        assert exemplar_density(PURE_STREAM) >= 0.9

    def test_density_prose_is_zero(self):
        prose = "\n".join(f"This is ordinary sentence number {i}." for i in range(10))
        assert exemplar_density(prose) == 0.0

    def test_density_short_input_is_zero(self):
        assert exemplar_density("hello\nlabel: 1\n") == 0.0  # < 3 pairs

    def test_is_exemplar_stream_threshold(self):
        assert is_exemplar_stream(PURE_STREAM, threshold=0.8)
        assert not is_exemplar_stream("just chatting about the weather today", threshold=0.8)

    def test_parse_pure_stream(self):
        pairs = parse_exemplars(PURE_STREAM)
        assert [p.label for p in pairs] == ["21", "41", "32"]
        assert pairs[0].text == "how do I reset my card pin"
        assert [p.ordinal for p in pairs] == [0, 1, 2]

    def test_parse_transcript_skips_assistant_and_strips_user_prefix(self):
        pairs = parse_exemplars(TRANSCRIPT_STREAM)
        assert [p.label for p in pairs] == ["21", "41", "32"]
        assert pairs[1].text == "my card is lost"  # no "User: " prefix

    def test_parse_multiline_utterance(self):
        s = "line one\nline two of same utterance\nlabel: 7\nnext utt\nlabel: 8\n"
        pairs = parse_exemplars(s)
        assert pairs[0].text == "line one\nline two of same utterance"
        assert pairs[0].label == "7"

    def test_parse_skips_empty_utterance(self):
        s = "label: 5\nreal utterance\nlabel: 6\n"
        pairs = parse_exemplars(s)
        assert len(pairs) == 1 and pairs[0].label == "6"

    def test_parse_label_from_content(self):
        assert parse_label("some utterance\nlabel: 42") == "42"
        assert parse_label("no label here") is None
        assert parse_label("text\nlabel: atm_support") == "atm_support"


# ---------------------------------------------------------------------------
# Task 2: write-path wiring -- ingest_exemplars (parse -> cap -> embed -> learn)
# ---------------------------------------------------------------------------


async def _insert_episode(heart):
    async with heart.db.session() as s:
        ep = Episode(agent_id=heart.agent_id, summary="F086 exemplar ingest test episode.")
        s.add(ep)
        await s.commit()
        return ep.id


async def _fact_rows(heart, episode_id):
    async with heart.db.session() as s:
        result = await s.execute(select(Fact).where(Fact.source_episode_id == episode_id).order_by(Fact.source_ordinal))
        return result.scalars().all()


@pytest.mark.postgres_only
class TestExemplarIngest:
    """Heart.learn commits for real (no rollback-isolated session) -- every
    stream below embeds a fresh per-test uuid tag into its utterance text so
    the native-cosine dedup (agent-scoped, not episode-scoped) can never
    confuse a residual row from an earlier test or an earlier suite run for
    a duplicate of this test's own facts."""

    async def test_ingest_stores_pair_facts(self, heart, settings):
        episode_id = await _insert_episode(heart)
        tag = str(episode_id)
        stream = (
            f"how do I reset my card pin {tag}\nlabel: 21\n"
            f"my card is lost {tag}\nlabel: 41\n"
            f"what is the exchange rate {tag}\nlabel: 32\n"
        )
        n = await ingest_exemplars(heart, settings, stream, episode_id, heart.agent_id, logger)
        assert n == 3

        rows = await _fact_rows(heart, episode_id)
        assert len(rows) == 3
        assert rows[0].content == f"how do I reset my card pin {tag}\nlabel: 21"
        assert rows[0].source == "exemplar_extractor"
        assert rows[0].subject_key is None
        assert rows[0].attribute_key == "label"
        assert [r.source_ordinal for r in rows] == [0, 1, 2]
        assert all(r.embedding is not None for r in rows)

    async def test_min_chars_floor_source_aware(self, heart, settings):
        episode_id = await _insert_episode(heart)
        tag = str(episode_id)
        # "yes <tag>\nlabel: 1" is well below the global 30-char floor but
        # above the exemplar-specific floor (default 5). Must be STORED.
        stream = f"yes {tag}\nlabel: 1\nno {tag}\nlabel: 0\nmaybe so {tag}\nlabel: 1\n"
        n = await ingest_exemplars(heart, settings, stream, episode_id, heart.agent_id, logger)
        assert n == 3
        rows = await _fact_rows(heart, episode_id)
        assert len(rows) == 3

    async def test_admission_bypassed(self, heart_with_strict_admission, session):
        heart = heart_with_strict_admission
        episode_id = await _insert_episode(heart)
        tag = str(episode_id)
        # Near-identical banking-shaped utterances -- a scoring admission
        # controller (threshold 0.99 here) would reject these as low-novelty;
        # exemplar_extractor is in bypass_sources so scoring never runs.
        stream = (
            f"What is my checking account balance right now {tag}?\n"
            "label: balance\n"
            f"What is my checking account balance today please {tag}?\n"
            "label: balance\n"
            f"What is my savings account balance right now {tag}?\n"
            "label: balance\n"
        )
        n = await ingest_exemplars(heart, heart.settings, stream, episode_id, heart.agent_id, logger)
        assert n == 3
        rows = await _fact_rows(heart, episode_id)
        assert len(rows) == 3

    async def test_different_label_near_dupes_not_dropped(self, heart, settings, monkeypatch):
        episode_id = await _insert_episode(heart)
        tag = str(episode_id)
        content_a = f"Same account balance question here {tag}.\nlabel: 0"
        content_b = f"Same account balance question here {tag}.\nlabel: 1"

        # A and C use the IDENTICAL content string -> the real (deterministic,
        # hash-seeded) mock embedder already gives them equal vectors, no
        # forcing needed. B differs only in its label suffix, so the real
        # embedder would give it an uncorrelated vector; force it NEAR (not
        # identical to) A's real embedding so _find_duplicate surfaces it as
        # a would-be dupe of A, while still ranking strictly below C's exact
        # (cosine 1.0) match against A -- deterministic regardless of any
        # distance tie-breaking.
        vec_b_near = await heart._embeddings.embed_near(content_a, noise=0.02)
        real_embed = heart._embeddings.embed

        async def _forced_embed_batch(texts):
            out = []
            for t in texts:
                if t == content_b:
                    out.append(vec_b_near)
                else:
                    out.append(await real_embed(t))
            return out

        monkeypatch.setattr(heart._embeddings, "embed_batch", _forced_embed_batch)

        stream_a = f"Same account balance question here {tag}.\nlabel: 0\n"
        stream_b = f"Same account balance question here {tag}.\nlabel: 1\n"
        stream_c = stream_a  # same utterance AND same label as A -> true duplicate

        n_a = await ingest_exemplars(heart, settings, stream_a, episode_id, heart.agent_id, logger)
        n_b = await ingest_exemplars(heart, settings, stream_b, episode_id, heart.agent_id, logger)
        # n_c's return value is telemetry only (spec-review I1b) -- a
        # dedup-confirm still counts as "stored" from ingest_exemplars'
        # perspective even though no new row is created. Assert on DB rows.
        await ingest_exemplars(heart, settings, stream_c, episode_id, heart.agent_id, logger)

        assert n_a == 1
        assert n_b == 1  # different label -> stored as new, not dropped

        rows = await _fact_rows(heart, episode_id)
        # A and B both persisted as distinct rows; C deduped against A.
        assert len(rows) == 2
        labels = sorted(r.content.splitlines()[-1] for r in rows)
        assert labels == ["label: 0", "label: 1"]

    async def test_cap_truncates_loudly(self, heart, settings, caplog):
        episode_id = await _insert_episode(heart)
        tag = str(episode_id)
        stream = (
            f"how do I reset my card pin {tag}\nlabel: 21\n"
            f"my card is lost {tag}\nlabel: 41\n"
            f"what is the exchange rate {tag}\nlabel: 32\n"
        )
        capped = settings.model_copy(update={"exemplar_max_per_episode": 2})
        with caplog.at_level("WARNING"):
            n = await ingest_exemplars(heart, capped, stream, episode_id, heart.agent_id, logger)
        assert n == 2
        rows = await _fact_rows(heart, episode_id)
        assert len(rows) == 2
        assert any("truncat" in r.message.lower() for r in caplog.records)


# ---------------------------------------------------------------------------
# Task 2: modal routing seam in FactExtractor.extract_and_store (no DB)
# ---------------------------------------------------------------------------


class _StubResult:
    def __init__(self):
        self.id = uuid4()


def _stub_heart():
    captured = []

    class _StubHeart:
        agent_id = "test-agent"

        async def learn(self, fact_input, **kw):
            captured.append(fact_input)
            return _StubResult()

    h = _StubHeart()
    h._captured = captured
    return h


_VALID_SUMMARY = {
    "candidate_facts": [
        {
            "content": "Tim likes coffee " + "x" * 20,
            "subject": "Tim",
            "category": "preference",
            "confidence": 0.9,
        }
    ]
}


@pytest.mark.asyncio
async def test_extractor_routes_modal_before_r1(monkeypatch):
    """Flag on + exemplar-shaped transcript: the exemplar leg stores and both
    R1 and the legacy candidate path are skipped (modal routing)."""
    r1_sentinel = AsyncMock(side_effect=AssertionError("R1 ran despite exemplar routing"))
    monkeypatch.setattr(
        "nous.handlers.enumerative_extractor.EnumerativeExtractor.process_transcript",
        r1_sentinel,
    )

    from nous.handlers import fact_extractor as fe_mod

    heart = _stub_heart()
    ext = fe_mod.FactExtractor.__new__(fe_mod.FactExtractor)
    ext._heart = heart
    ext._settings = SimpleNamespace(
        exemplar_extraction_enabled=True,
        exemplar_density_threshold=0.8,
        exemplar_max_per_episode=5000,
        extraction_enumerative_enabled=False,
    )
    ext._dedup_via_search = False
    ext._llm = object()

    result = await ext.extract_and_store(
        summary=_VALID_SUMMARY,
        episode_id=str(uuid4()),
        transcript=PURE_STREAM,
    )

    r1_sentinel.assert_not_called()
    assert len(heart._captured) == 3
    assert all(fi.source == "exemplar_extractor" for fi in heart._captured)
    # Legacy candidate ("Tim likes coffee...") must be ABSENT -- modal, not additive.
    assert not any("Tim likes coffee" in fi.content for fi in heart._captured)
    # arch-review I2: the seam returns [] (not the stored ids, not None) on
    # success -- ingest_exemplars' contract is a count, not list[UUID]; []
    # satisfies extract_and_store's list[UUID] return type.
    assert result == []


@pytest.mark.asyncio
async def test_extractor_flag_off_exemplar_leg_never_runs(monkeypatch):
    """GOLDEN / flag-off invariant: exemplar-shaped transcript still runs the
    legacy candidate path unchanged when the flag is off."""
    ingest_sentinel = AsyncMock(side_effect=AssertionError("exemplar leg ran with flag off"))
    monkeypatch.setattr("nous.handlers.fact_extractor.ingest_exemplars", ingest_sentinel)

    from nous.handlers import fact_extractor as fe_mod

    heart = _stub_heart()
    ext = fe_mod.FactExtractor.__new__(fe_mod.FactExtractor)
    ext._heart = heart
    ext._settings = SimpleNamespace(
        exemplar_extraction_enabled=False,
        exemplar_density_threshold=0.8,
        extraction_enumerative_enabled=False,
    )
    ext._dedup_via_search = False
    ext._llm = object()

    result = await ext.extract_and_store(
        summary=_VALID_SUMMARY,
        episode_id=str(uuid4()),
        transcript=PURE_STREAM,
    )

    ingest_sentinel.assert_not_called()
    assert len(heart._captured) == 1
    assert len(result) == 1
