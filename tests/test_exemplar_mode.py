"""F086 ICL exemplar mode tests."""

import logging
import math
import random as _random
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select, text

from nous.api.retrieval_pipeline import (
    PipelineResult,
    PipelineStats,
    _is_classification_shaped,
    run_recall_pipeline,
)
from nous.api.tools import _format_pipeline_text
from nous.brain.brain import Brain
from nous.handlers.exemplar_ingest import ingest_exemplars
from nous.heart.exemplars import (
    ExemplarPair,  # noqa: F401 -- imported to assert the exported type exists
    exemplar_density,
    is_exemplar_stream,
    parse_exemplars,
    parse_label,
)
from nous.heart.facts import ExemplarHit, FactManager
from nous.heart.schemas import FactInput, FactRejected
from nous.storage.models import Episode, Fact

logger = logging.getLogger(__name__)


def _vec_at_cosine(base: list[float], cos: float, seed: str) -> list[float]:
    """Construct a unit vector with EXACT cosine ``cos`` to unit vector ``base``.

    ``v = cos*base + sqrt(1-cos^2)*orthonormal`` where ``orthonormal`` is a unit
    vector orthogonal to ``base``. The dot product is deterministically ``cos``
    (callers verify it), so a near-dupe fixture can pin BOTH the 0.80
    supersession band AND the 0.95 native-cosine dedup gate at once — unlike
    ``embed_near``'s per-dim gaussian, which lands ~0.787 and straddles 0.80.
    """
    rng = _random.Random(seed)
    raw = [rng.gauss(0, 1) for _ in base]
    dot_rb = sum(r * b for r, b in zip(raw, base))
    orth = [r - dot_rb * b for r, b in zip(raw, base)]
    onorm = math.sqrt(sum(x * x for x in orth))
    orth = [x / onorm for x in orth]
    v = [cos * b + math.sqrt(1 - cos * cos) * o for b, o in zip(base, orth)]
    vnorm = math.sqrt(sum(x * x for x in v))
    return [x / vnorm for x in v]


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


class TestClassificationShapedTrigger:
    """Codex r2: the memory-referential blocklist must require a real stored-memory
    verb after the ambiguous `did i/we/you` / `what did` / `what have i/we` prefixes,
    so ordinary banking77-shaped past-tense questions still trigger the leg."""

    @pytest.mark.parametrize(
        "query",
        [
            "did I get charged twice",  # banking77 shape — must NOT be blocked
            "did I make a cash withdrawal",
            "what is the capital of france",  # trec shape
            "did you increase my limit",
        ],
    )
    def test_classification_shapes_trigger(self, query):
        assert _is_classification_shaped(query, 64) is True

    @pytest.mark.parametrize(
        "query",
        [
            "what did I say about my card",
            "did you mention the deadline",
            "did we discuss the refund",
            "remind me what we discussed",
            "last time we talked",
            "you said the account was closed",
        ],
    )
    def test_memory_referential_excluded(self, query):
        assert _is_classification_shaped(query, 64) is False


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

    @pytest_asyncio.fixture(autouse=True)
    async def _cleanup_shared_agent_rows(self, heart):
        """Hard-delete this class's writes from the SHARED ``nous-default`` agent
        after each test. Unlike ``TestExemplarFetch`` (T3), this class cannot use
        a throwaway agent_id — ``ingest_exemplars`` stores via ``heart.learn``
        under the passed heart's own agent_id, and every heart-family fixture
        (``heart`` / ``heart_with_strict_admission``) shares the ``Settings()``
        default agent. Left behind, these exemplar rows crowd the LIMIT-10 fact
        search of later-alphabetical suites (``test_write_path_adjudication``) in
        a fresh full-suite run. Facts are deleted before their marker episodes
        so the ``facts.source_episode_id`` FK never trips."""
        yield
        marker = "F086 exemplar ingest test episode."
        async with heart.db.session() as s:
            # Delete every fact tied to a marker episode (any source — the FIX 4
            # test also writes a fact_extractor fact into its episode) BEFORE the
            # episodes, so the facts.source_episode_id FK never trips.
            await s.execute(
                text(
                    "DELETE FROM heart.facts WHERE source_episode_id IN "
                    "(SELECT id FROM heart.episodes WHERE agent_id = :a AND summary = :summary)"
                ),
                {"a": heart.agent_id, "summary": marker},
            )
            await s.execute(
                text("DELETE FROM heart.episodes WHERE agent_id = :a AND summary = :summary"),
                {"a": heart.agent_id, "summary": marker},
            )
            await s.commit()

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
        # forcing needed. B differs only in its label suffix; force its vector
        # to an EXACT cosine of 0.97 to A's -- deterministically >= 0.95 (so it
        # reaches the native-cosine dedup gate and the label-guard MUST fire)
        # AND > 0.80 (so it also enters the legacy subject-supersession band,
        # pinning FIX 1's exemplar exemption -- pre-fix this row got
        # active=False + superseded_by set). embed_near(noise=0.02) landed
        # ~0.787: below the dedup gate (guard untested) and straddling 0.80
        # (flaky). A and B share the SAME utterance text -> the SAME subject, so
        # the legacy supersession path is genuinely reachable here.
        base_a = await heart._embeddings.embed(content_a)
        vec_b_near = _vec_at_cosine(base_a, 0.97, "f086-fix3-b")
        assert abs(sum(x * b for x, b in zip(vec_b_near, base_a)) - 0.97) < 1e-9
        assert 0.97 >= 0.95 and 0.97 > 0.80
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
        # FIX 1: both A and B remain ACTIVE with NO supersession link -- the
        # legacy subject-supersession is exempted for exemplars even at
        # cosine 0.97 (> 0.80 band). C deduped against A (confirm, no new row).
        active_rows = [r for r in rows if r.active]
        assert len(active_rows) == 2
        assert all(r.superseded_by is None for r in active_rows)
        labels = sorted(r.content.splitlines()[-1] for r in active_rows)
        assert labels == ["label: 0", "label: 1"]

    async def test_normal_fact_not_dedup_dropped_into_exemplar(self, heart, settings):
        """FIX 4 (two-sided guard): a genuine conversational fact whose nearest
        neighbor is an exemplar row must NOT dedup-confirm into that exemplar
        and vanish. Pre-fix the guard checked only ``input.source``, so a
        non-exemplar input dedup-dropped into an exemplar dupe."""
        episode_id = await _insert_episode(heart)
        tag = str(episode_id)

        # Store an exemplar row first.
        exemplar_stream = f"how do I reset my card pin {tag}\nlabel: 21\n"
        n_ex = await ingest_exemplars(heart, settings, exemplar_stream, episode_id, heart.agent_id, logger)
        assert n_ex == 1

        # A normal fact engineered at EXACT cosine 0.97 to the stored exemplar's
        # embedding (>= the 0.95 native-cosine dedup gate). parse_label(normal)
        # is None != "21", so the two-sided guard (dupe side is exemplar) must
        # clear `found` and INSERT the normal fact rather than confirm-drop it.
        exemplar_content = f"how do I reset my card pin {tag}\nlabel: 21"
        base_ex = await heart._embeddings.embed(exemplar_content)
        normal_content = f"The user asked how to reset the card pin during session {tag}."
        vec_normal = _vec_at_cosine(base_ex, 0.97, "f086-fix4-normal")
        assert abs(sum(x * b for x, b in zip(vec_normal, base_ex)) - 0.97) < 1e-9

        result = await heart.learn(
            FactInput(
                content=normal_content,
                subject="card pin reset help",
                category="general",
                confidence=0.9,
                source="fact_extractor",
                source_episode_id=episode_id,
            ),
            precomputed_embedding=vec_normal,
        )
        assert not isinstance(result, FactRejected)

        rows = await _fact_rows(heart, episode_id)
        active_rows = [r for r in rows if r.active]
        # Both the exemplar AND the normal fact are present and active.
        assert len(active_rows) == 2
        sources = sorted(r.source for r in active_rows)
        assert sources == ["exemplar_extractor", "fact_extractor"]

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

    async def test_unembeddable_pairs_skipped_loudly(self, heart, settings, monkeypatch, caplog):
        # Codex r3: when batch AND per-pair embed both fail, an exemplar must
        # NOT be persisted with a NULL embedding (invisible to retrieval + dedup
        # -> silently duplicated on rerun). It is skipped, counted, WARNED.
        episode_id = await _insert_episode(heart)
        tag = str(episode_id)
        stream = (
            f"how do I reset my card pin {tag}\nlabel: 21\n"
            f"my card is lost {tag}\nlabel: 41\n"
            f"what is the exchange rate {tag}\nlabel: 32\n"
        )

        async def _raise_batch(texts):
            raise RuntimeError("batch embed down")

        async def _raise_embed(text):
            raise RuntimeError("embed down")

        monkeypatch.setattr(heart._embeddings, "embed_batch", _raise_batch)
        monkeypatch.setattr(heart._embeddings, "embed", _raise_embed)

        with caplog.at_level("WARNING"):
            n = await ingest_exemplars(heart, settings, stream, episode_id, heart.agent_id, logger)

        assert n == 0
        rows = await _fact_rows(heart, episode_id)
        assert len(rows) == 0  # nothing persisted with a NULL embedding
        assert any("SKIPPED 3" in r.getMessage() for r in caplog.records)

    async def test_per_pair_embed_recovers_batch_failure(self, heart, settings, monkeypatch):
        # Codex r3: a batch-embed failure alone must NOT lose pairs — the
        # per-pair retry recovers them and all three store normally.
        episode_id = await _insert_episode(heart)
        tag = str(episode_id)
        stream = (
            f"how do I reset my card pin {tag}\nlabel: 21\n"
            f"my card is lost {tag}\nlabel: 41\n"
            f"what is the exchange rate {tag}\nlabel: 32\n"
        )

        async def _raise_batch(texts):
            raise RuntimeError("batch embed down")

        monkeypatch.setattr(heart._embeddings, "embed_batch", _raise_batch)
        # embed (per-pair) stays real -> recovery path stores all 3.

        n = await ingest_exemplars(heart, settings, stream, episode_id, heart.agent_id, logger)
        assert n == 3
        rows = await _fact_rows(heart, episode_id)
        assert len(rows) == 3
        assert all(r.embedding is not None for r in rows)


# ---------------------------------------------------------------------------
# Task 2: modal routing seam in FactExtractor.extract_and_store (no DB)
# ---------------------------------------------------------------------------


class _StubResult:
    def __init__(self):
        self.id = uuid4()


class _StubEmbeddings:
    """Minimal embedder so the exemplar store path (codex r3: no NULL-embedding
    rows, skip when no vector) actually reaches learn in the stub-heart routing
    tests — the modal-routing assertions are about WHICH inputs get stored, so a
    constant non-None vector is enough."""

    async def embed_batch(self, texts):
        return [[0.1, 0.2, 0.3, 0.4] for _ in texts]

    async def embed(self, text):
        return [0.1, 0.2, 0.3, 0.4]


def _stub_heart():
    captured = []

    class _StubHeart:
        agent_id = "test-agent"
        _embeddings = _StubEmbeddings()

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


# ---------------------------------------------------------------------------
# Task 3: read-side data layer -- source-filtered vector fetch + exists-probe
# ---------------------------------------------------------------------------


async def _delete_facts_for_agent(heart, agent_id):
    async with heart.db.session() as s:
        await s.execute(text("DELETE FROM heart.facts WHERE agent_id = :a"), {"a": agent_id})
        await s.commit()


@pytest.mark.postgres_only
class TestExemplarFetch:
    """HNSW/pgvector-backed coverage for fetch_exemplars_by_vector + has_exemplars.

    Each test uses a freshly-tagged, throwaway agent_id rather than the
    shared `heart` fixture's default agent -- the shared local Postgres
    already carries persisted source='exemplar_extractor' rows for the
    default agent from TestExemplarIngest's non-rollback-isolated writes,
    so an "empty store" assertion against the shared agent would be flaky.
    Rows are deleted in a finally block so this suite doesn't grow the
    shared dev DB across runs either.
    """

    async def test_fetch_orders_by_cosine_and_filters_source(self, heart):
        agent_id = f"exemplar-fetch-{uuid4()}"
        try:
            query = "how do I reset the pin on my lost debit card"
            query_vec = await heart._embeddings.embed(query)
            close_vec = await heart._embeddings.embed_near(query, noise=0.01)
            mid_vec = await heart._embeddings.embed_near(query, noise=0.05)
            far_vec = await heart._embeddings.embed_near(query, noise=0.1)

            close_id, mid_id, far_id, normal_id = uuid4(), uuid4(), uuid4(), uuid4()
            async with heart.db.session() as s:
                s.add(
                    Fact(
                        id=close_id,
                        agent_id=agent_id,
                        content="closest exemplar utterance content here\nlabel: 1",
                        source="exemplar_extractor",
                        active=True,
                        embedding=close_vec,
                    )
                )
                s.add(
                    Fact(
                        id=mid_id,
                        agent_id=agent_id,
                        content="middling exemplar utterance content here\nlabel: 2",
                        source="exemplar_extractor",
                        active=True,
                        embedding=mid_vec,
                    )
                )
                s.add(
                    Fact(
                        id=far_id,
                        agent_id=agent_id,
                        content="farthest exemplar utterance content here\nlabel: 3",
                        source="exemplar_extractor",
                        active=True,
                        embedding=far_vec,
                    )
                )
                # Non-exemplar fact engineered CLOSER than any exemplar above --
                # proves the source filter (not just ranking) drives exclusion.
                s.add(
                    Fact(
                        id=normal_id,
                        agent_id=agent_id,
                        content="an ordinary non-exemplar fact about card pins",
                        source="fact_extractor",
                        active=True,
                        embedding=query_vec,
                    )
                )
                await s.commit()

            fm = FactManager(heart.db, heart._embeddings, agent_id, settings=heart.settings)
            hits = await fm.fetch_exemplars_by_vector(query_vec, limit=25)

            assert all(isinstance(h, ExemplarHit) for h in hits)
            assert [h.id for h in hits] == [close_id, mid_id, far_id]
            assert normal_id not in [h.id for h in hits]
            assert hits[0].similarity >= hits[1].similarity >= hits[2].similarity

            limited = await fm.fetch_exemplars_by_vector(query_vec, limit=2)
            assert [h.id for h in limited] == [close_id, mid_id]
        finally:
            await _delete_facts_for_agent(heart, agent_id)

    async def test_fetch_excludes_inactive(self, heart):
        agent_id = f"exemplar-fetch-{uuid4()}"
        try:
            query = "what is the current exchange rate for euros"
            query_vec = await heart._embeddings.embed(query)
            active_vec = await heart._embeddings.embed_near(query, noise=0.02)
            # Engineered CLOSER than the active row -- proves exclusion is
            # driven by active=false, not by ranking it out naturally.
            inactive_vec = await heart._embeddings.embed_near(query, noise=0.01)

            active_id, inactive_id = uuid4(), uuid4()
            async with heart.db.session() as s:
                s.add(
                    Fact(
                        id=active_id,
                        agent_id=agent_id,
                        content="active exemplar utterance about exchange rates\nlabel: 4",
                        source="exemplar_extractor",
                        active=True,
                        embedding=active_vec,
                    )
                )
                s.add(
                    Fact(
                        id=inactive_id,
                        agent_id=agent_id,
                        content="deactivated exemplar utterance about exchange rates\nlabel: 5",
                        source="exemplar_extractor",
                        active=False,
                        embedding=inactive_vec,
                    )
                )
                await s.commit()

            fm = FactManager(heart.db, heart._embeddings, agent_id, settings=heart.settings)
            hits = await fm.fetch_exemplars_by_vector(query_vec, limit=25)

            assert [h.id for h in hits] == [active_id]
        finally:
            await _delete_facts_for_agent(heart, agent_id)

    async def test_has_exemplars_probe_and_invalidation(self, heart):
        agent_id = f"exemplar-fetch-{uuid4()}"
        try:
            fm = FactManager(heart.db, heart._embeddings, agent_id, settings=heart.settings)

            assert await fm.has_exemplars() is False  # empty store for this fresh agent, cached

            result = await fm.learn(
                FactInput(
                    content="a freshly learned exemplar utterance for cache invalidation\nlabel: 6",
                    subject_key=None,
                    attribute_key="label",
                    category="exemplar",
                    confidence=1.0,
                    source="exemplar_extractor",
                )
            )
            assert not isinstance(result, FactRejected)

            assert await fm.has_exemplars() is True  # invalidated post-commit, no TTL wait
        finally:
            await _delete_facts_for_agent(heart, agent_id)


# ---------------------------------------------------------------------------
# Task 4: read-path Stage 1.7 exemplar leg in run_recall_pipeline
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def brain(db, settings):
    """Brain without embeddings (keyword-only mode) -- per-file fixture,
    mirroring tests/test_keyed_fact_leg.py:29 (no conftest `brain` fixture
    exists)."""
    b = Brain(database=db, settings=settings)
    yield b
    await b.close()


def _mk_hit(idx: int, similarity: float) -> ExemplarHit:
    return ExemplarHit(
        id=uuid4(),
        content=f"a synthetic exemplar utterance {idx}\nlabel: {idx}",
        similarity=similarity,
    )


@pytest.mark.postgres_only
class TestExemplarLeg:
    """Task 4: Stage 1.7 exemplar retrieval leg.

    ``heart.facts.has_exemplars``/``fetch_exemplars_by_vector`` are
    monkeypatched throughout -- Task 3 (``TestExemplarFetch`` above) already
    covers their real DB behavior. Mocking them here keeps these
    pipeline-level tests fast and immune to the shared, ever-growing dev
    Postgres accidentally surfacing pre-existing exemplar rows (persisted by
    ``TestExemplarIngest``) via Stage 1's own unfiltered fact search -- a real
    seeded exemplar fact similar enough to clear the leg's own similarity
    floor would also rank in Stage 1's plain vector search (which has no
    absolute floor), making "the leg found something Stage 1 didn't"
    impossible to isolate with real embeddings alone.
    """

    _QUERY = "what is the capital of france"

    async def test_flag_off_byte_identical(self, heart, brain, settings, monkeypatch):
        # Baseline flag-off run (exemplar_mode_enabled defaults False).
        baseline_results, baseline_stats = await run_recall_pipeline(self._QUERY, heart, brain, settings)

        # Second flag-off run with has_exemplars sentinel'd: proves the leg
        # never executes AND that flag-off output stays byte-identical to the
        # baseline (full ordered results + exemplar stats fields), the Gate-4
        # composite claim. Mirrors the floor test's ordered-id comparison.
        sentinel = AsyncMock(side_effect=AssertionError("exemplar leg ran with flag off"))
        monkeypatch.setattr(heart.facts, "has_exemplars", sentinel)

        results, stats = await run_recall_pipeline(self._QUERY, heart, brain, settings)

        sentinel.assert_not_called()
        assert stats.exemplar_leg_used is False
        assert stats.n_exemplar == 0
        assert not any(r.metadata.get("retrieval_leg") == "exemplar" for r in results)
        # Full ordered results identical to the baseline flag-off run.
        assert [(r.id, r.type, r.score) for r in results] == [(r.id, r.type, r.score) for r in baseline_results]
        # Exemplar-leg stats fields inert and equal to baseline.
        assert (stats.exemplar_leg_used, stats.n_exemplar, stats.n_exemplar_dup) == (
            baseline_stats.exemplar_leg_used,
            baseline_stats.n_exemplar,
            baseline_stats.n_exemplar_dup,
        )

    async def test_trigger_gates(self, heart, brain, settings, monkeypatch):
        s = settings.model_copy(update={"exemplar_mode_enabled": True})

        # Long query (> exemplar_max_query_words) -> skipped before has_exemplars.
        monkeypatch.setattr(
            heart.facts,
            "has_exemplars",
            AsyncMock(side_effect=AssertionError("must not be called for a too-long query")),
        )
        long_query = " ".join(["word"] * (s.exemplar_max_query_words + 1))
        _, stats = await run_recall_pipeline(long_query, heart, brain, s)
        assert stats.exemplar_leg_used is False

        # Memory-referential query -> skipped before has_exemplars.
        monkeypatch.setattr(
            heart.facts,
            "has_exemplars",
            AsyncMock(side_effect=AssertionError("must not be called for a memory-referential query")),
        )
        _, stats = await run_recall_pipeline("what did I say about my card", heart, brain, s)
        assert stats.exemplar_leg_used is False

        # Plain question -- trec-shape MUST trigger.
        monkeypatch.setattr(heart.facts, "has_exemplars", AsyncMock(return_value=True))
        monkeypatch.setattr(heart.facts, "fetch_exemplars_by_vector", AsyncMock(return_value=[]))
        _, stats = await run_recall_pipeline(self._QUERY, heart, brain, s)
        assert stats.exemplar_leg_used is True

        # Empty exemplar store -> skipped via the exists-probe; fetch never runs.
        monkeypatch.setattr(heart.facts, "has_exemplars", AsyncMock(return_value=False))
        monkeypatch.setattr(
            heart.facts,
            "fetch_exemplars_by_vector",
            AsyncMock(side_effect=AssertionError("fetch must not run against an empty store")),
        )
        _, stats = await run_recall_pipeline(self._QUERY, heart, brain, s)
        assert stats.exemplar_leg_used is False

    async def test_leg_merges_banded_not_tail(self, heart, brain, settings, monkeypatch):
        s = settings.model_copy(update={"exemplar_mode_enabled": True})
        hits = [_mk_hit(i, 0.9 - 0.05 * i) for i in range(3)]
        fetch_mock = AsyncMock(return_value=hits)
        monkeypatch.setattr(heart.facts, "has_exemplars", AsyncMock(return_value=True))
        monkeypatch.setattr(heart.facts, "fetch_exemplars_by_vector", fetch_mock)

        results, stats = await run_recall_pipeline(self._QUERY, heart, brain, s)

        # Codex r1: the leg fetches without a fetch-side exclusion (Stage-1
        # exemplar rows are stripped from the accumulator instead, then
        # re-fetched into the banded examples block). Pin the call shape.
        fetch_mock.assert_awaited_once()
        assert fetch_mock.await_args.kwargs["limit"] == s.exemplar_top_k
        assert "exclude_fact_ids" not in fetch_mock.await_args.kwargs

        exemplar_rows = [r for r in results if r.metadata.get("retrieval_leg") == "exemplar"]
        assert len(exemplar_rows) == 3
        assert stats.n_exemplar == 3
        assert stats.exemplar_leg_used is True
        # Banded, not tail-appended: every result strictly BEFORE an exemplar
        # row must have a score >= it (the -5.0pp wasted-slot lesson -- a
        # dumb list.append() would let a lower-scoring existing result sit
        # ahead of a higher-scoring exemplar row). The shared dev Postgres
        # may hold arbitrarily many higher-scoring unrelated results, so this
        # checks the insertion invariant directly rather than a fixed top-K
        # window.
        for er in exemplar_rows:
            idx = results.index(er)
            assert all((results[i].score or 0.0) >= er.score for i in range(idx))
        for rank, hit in enumerate(hits):
            row = next(r for r in exemplar_rows if r.id == hit.id)
            assert row.type == "fact" and row.source == "heart"
            assert row.metadata["label"] == parse_label(hit.content)
            assert row.metadata["similarity"] == hit.similarity
            assert row.score == max(0.0, s.exemplar_leg_score - 0.005 * rank)

    async def test_similarity_floor_bounds_false_trigger(self, heart, brain, settings, monkeypatch):
        # spec-review I3a: hits below exemplar_min_similarity must not merge,
        # AND the non-exemplar result ordering must be untouched.
        off_results, _ = await run_recall_pipeline(self._QUERY, heart, brain, settings)

        s = settings.model_copy(update={"exemplar_mode_enabled": True})
        far_hits = [_mk_hit(i, s.exemplar_min_similarity - 0.05) for i in range(2)]
        monkeypatch.setattr(heart.facts, "has_exemplars", AsyncMock(return_value=True))
        monkeypatch.setattr(heart.facts, "fetch_exemplars_by_vector", AsyncMock(return_value=far_hits))

        on_results, stats = await run_recall_pipeline(self._QUERY, heart, brain, s)

        assert not any(r.metadata.get("retrieval_leg") == "exemplar" for r in on_results)
        assert stats.n_exemplar == 0
        assert [r.id for r in on_results] == [r.id for r in off_results]

    async def test_merged_exemplars_do_not_displace(self, heart, brain, settings, monkeypatch):
        # spec-review I3b: additive-only -- the non-exemplar SUBSEQUENCE must
        # keep the exact membership+order of the flag-off run.
        fact_id = uuid4()
        query_vec = await heart._embeddings.embed(self._QUERY)
        async with heart.db.session() as seed:
            seed.add(
                Fact(
                    id=fact_id,
                    agent_id=heart.agent_id,
                    content="A baseline fact engineered to rank for the displacement test query.",
                    active=True,
                    embedding=query_vec,
                )
            )
            await seed.commit()
        try:
            off_results, _ = await run_recall_pipeline(self._QUERY, heart, brain, settings)

            s = settings.model_copy(update={"exemplar_mode_enabled": True})
            hits = [_mk_hit(i, 0.8 - 0.05 * i) for i in range(2)]
            monkeypatch.setattr(heart.facts, "has_exemplars", AsyncMock(return_value=True))
            monkeypatch.setattr(heart.facts, "fetch_exemplars_by_vector", AsyncMock(return_value=hits))
            on_results, _ = await run_recall_pipeline(self._QUERY, heart, brain, s)

            exemplar_ids = {r.id for r in on_results if r.metadata.get("retrieval_leg") == "exemplar"}
            assert exemplar_ids  # the leg actually merged something
            non_exemplar_on = [r.id for r in on_results if r.id not in exemplar_ids]
            assert non_exemplar_on == [r.id for r in off_results]
        finally:
            async with heart.db.session() as cleanup:
                f = await cleanup.get(Fact, fact_id)
                if f is not None:
                    await cleanup.delete(f)
                await cleanup.commit()

    async def test_dedup_against_existing_results(self, heart, brain, settings, monkeypatch):
        fact_id = uuid4()
        query_vec = await heart._embeddings.embed(self._QUERY)
        async with heart.db.session() as seed:
            seed.add(
                Fact(
                    id=fact_id,
                    agent_id=heart.agent_id,
                    content="A fact engineered to be found by Stage 1 AND returned by the exemplar leg.",
                    active=True,
                    embedding=query_vec,
                )
            )
            await seed.commit()
        try:
            s = settings.model_copy(update={"exemplar_mode_enabled": True})
            dup_hit = ExemplarHit(id=fact_id, content="dup\nlabel: 0", similarity=0.95)
            fresh_hit = _mk_hit(99, 0.9)
            monkeypatch.setattr(heart.facts, "has_exemplars", AsyncMock(return_value=True))
            monkeypatch.setattr(
                heart.facts,
                "fetch_exemplars_by_vector",
                AsyncMock(return_value=[dup_hit, fresh_hit]),
            )
            results, stats = await run_recall_pipeline(self._QUERY, heart, brain, s)

            occurrences = [r for r in results if r.id == fact_id]
            assert len(occurrences) == 1
            assert occurrences[0].metadata.get("retrieval_leg") != "exemplar"
            assert stats.n_exemplar_dup == 1
            assert any(r.id == fresh_hit.id and r.metadata.get("retrieval_leg") == "exemplar" for r in results)
        finally:
            async with heart.db.session() as cleanup:
                f = await cleanup.get(Fact, fact_id)
                if f is not None:
                    await cleanup.delete(f)
                await cleanup.commit()

    async def test_stage_error_isolated(self, heart, brain, settings, monkeypatch):
        fact_id = uuid4()
        query_vec = await heart._embeddings.embed(self._QUERY)
        async with heart.db.session() as seed:
            seed.add(
                Fact(
                    id=fact_id,
                    agent_id=heart.agent_id,
                    content="A baseline fact that must survive an exemplar leg failure untouched.",
                    active=True,
                    embedding=query_vec,
                )
            )
            await seed.commit()
        try:
            s = settings.model_copy(update={"exemplar_mode_enabled": True})
            monkeypatch.setattr(heart.facts, "has_exemplars", AsyncMock(return_value=True))
            monkeypatch.setattr(
                heart.facts,
                "fetch_exemplars_by_vector",
                AsyncMock(side_effect=RuntimeError("boom")),
            )
            results, stats = await run_recall_pipeline(self._QUERY, heart, brain, s)

            assert stats.n_stage_errors.get("exemplar") == 1
            assert any(r.id == fact_id for r in results)
        finally:
            async with heart.db.session() as cleanup:
                f = await cleanup.get(Fact, fact_id)
                if f is not None:
                    await cleanup.delete(f)
                await cleanup.commit()

    async def test_stage1_exemplar_routed_to_examples_block(self, heart, brain, settings, monkeypatch):
        # Codex r1: both flags on + trigger fires + a REAL exemplar fact that
        # Stage 1 surfaces (untagged) -> it appears ONCE, in the dedicated
        # examples block (retrieval_leg=="exemplar"), NOT in Heart Memory, and
        # the non-exemplar Stage-1 subsequence is order-identical to flag-off.
        # The real exemplar_ids_among SELECT runs against the seeded row.
        exemplar_id = uuid4()
        query_vec = await heart._embeddings.embed(self._QUERY)
        async with heart.db.session() as seed:
            seed.add(
                Fact(
                    id=exemplar_id,
                    agent_id=heart.agent_id,
                    content="how do I reset my card pin\nlabel: 21",
                    source="exemplar_extractor",
                    active=True,
                    embedding=query_vec,
                )
            )
            await seed.commit()
        try:
            # Flag off: the exemplar fact surfaces in Heart Memory, untagged.
            off_results, _ = await run_recall_pipeline(self._QUERY, heart, brain, settings)
            off_occ = [r for r in off_results if r.id == exemplar_id]
            assert len(off_occ) == 1 and off_occ[0].metadata.get("retrieval_leg") != "exemplar"

            s = settings.model_copy(update={"exemplar_mode_enabled": True})
            hit = ExemplarHit(id=exemplar_id, content="how do I reset my card pin\nlabel: 21", similarity=0.95)
            monkeypatch.setattr(heart.facts, "has_exemplars", AsyncMock(return_value=True))
            monkeypatch.setattr(heart.facts, "fetch_exemplars_by_vector", AsyncMock(return_value=[hit]))
            on_results, stats = await run_recall_pipeline(self._QUERY, heart, brain, s)

            occ = [r for r in on_results if r.id == exemplar_id]
            assert len(occ) == 1  # appears exactly once
            assert occ[0].metadata.get("retrieval_leg") == "exemplar"  # ONLY in the examples block
            # Non-exemplar subsequence == flag-off minus the now-routed exemplar row.
            non_ex_on = [r.id for r in on_results if r.metadata.get("retrieval_leg") != "exemplar"]
            non_ex_off = [r.id for r in off_results if r.id != exemplar_id]
            assert non_ex_on == non_ex_off
        finally:
            async with heart.db.session() as cleanup:
                f = await cleanup.get(Fact, exemplar_id)
                if f is not None:
                    await cleanup.delete(f)
                await cleanup.commit()

    async def test_mode_on_trigger_unmet_stage1_untouched(self, heart, brain, settings, monkeypatch):
        # Codex r1: mode ON but the trigger is NOT met (memory-referential query)
        # -> the leg never runs, has_exemplars is never called, and Stage-1
        # results are byte-identical to the flag-off run (no exemplar stripping).
        mem_query = "what did I say about my card"
        exemplar_id = uuid4()
        query_vec = await heart._embeddings.embed(mem_query)
        async with heart.db.session() as seed:
            seed.add(
                Fact(
                    id=exemplar_id,
                    agent_id=heart.agent_id,
                    content="how do I reset my card pin\nlabel: 21",
                    source="exemplar_extractor",
                    active=True,
                    embedding=query_vec,
                )
            )
            await seed.commit()
        try:
            off_results, _ = await run_recall_pipeline(mem_query, heart, brain, settings)

            s = settings.model_copy(update={"exemplar_mode_enabled": True})
            monkeypatch.setattr(
                heart.facts,
                "has_exemplars",
                AsyncMock(side_effect=AssertionError("leg must not fire on an unmet trigger")),
            )
            on_results, stats = await run_recall_pipeline(mem_query, heart, brain, s)

            assert stats.exemplar_leg_used is False
            assert [(r.id, r.type, r.score) for r in on_results] == [(r.id, r.type, r.score) for r in off_results]
            # The exemplar fact is still present in Heart Memory, untouched.
            assert any(r.id == exemplar_id and r.metadata.get("retrieval_leg") != "exemplar" for r in on_results)
        finally:
            async with heart.db.session() as cleanup:
                f = await cleanup.get(Fact, exemplar_id)
                if f is not None:
                    await cleanup.delete(f)
                await cleanup.commit()

    async def test_stage1_exemplar_survives_fetch_failure(self, heart, brain, settings, monkeypatch):
        # Codex r2: a non-fatal leg error must NOT delete a successful Stage-1
        # result. The strip happens only AFTER fetch + floor succeed, so a fetch
        # that raises leaves the Stage-1 exemplar in Heart Memory (untagged).
        exemplar_id = uuid4()
        query_vec = await heart._embeddings.embed(self._QUERY)
        async with heart.db.session() as seed:
            seed.add(
                Fact(
                    id=exemplar_id,
                    agent_id=heart.agent_id,
                    content="how do I reset my card pin\nlabel: 21",
                    source="exemplar_extractor",
                    active=True,
                    embedding=query_vec,
                )
            )
            await seed.commit()
        try:
            s = settings.model_copy(update={"exemplar_mode_enabled": True})
            monkeypatch.setattr(heart.facts, "has_exemplars", AsyncMock(return_value=True))
            monkeypatch.setattr(heart.facts, "fetch_exemplars_by_vector", AsyncMock(side_effect=RuntimeError("boom")))
            results, stats = await run_recall_pipeline(self._QUERY, heart, brain, s)

            assert stats.n_stage_errors.get("exemplar") == 1
            occ = [r for r in results if r.id == exemplar_id]
            assert len(occ) == 1 and occ[0].metadata.get("retrieval_leg") != "exemplar"
        finally:
            async with heart.db.session() as cleanup:
                f = await cleanup.get(Fact, exemplar_id)
                if f is not None:
                    await cleanup.delete(f)
                await cleanup.commit()

    async def test_stage1_exemplar_survives_all_below_floor(self, heart, brain, settings, monkeypatch):
        # Codex r2: if every fetched hit falls below the floor, nothing replaces
        # the Stage-1 exemplar -> it must remain in Heart Memory (no strip).
        exemplar_id = uuid4()
        query_vec = await heart._embeddings.embed(self._QUERY)
        async with heart.db.session() as seed:
            seed.add(
                Fact(
                    id=exemplar_id,
                    agent_id=heart.agent_id,
                    content="how do I reset my card pin\nlabel: 21",
                    source="exemplar_extractor",
                    active=True,
                    embedding=query_vec,
                )
            )
            await seed.commit()
        try:
            s = settings.model_copy(update={"exemplar_mode_enabled": True})
            below = ExemplarHit(
                id=exemplar_id,
                content="how do I reset my card pin\nlabel: 21",
                similarity=s.exemplar_min_similarity - 0.05,
            )
            monkeypatch.setattr(heart.facts, "has_exemplars", AsyncMock(return_value=True))
            monkeypatch.setattr(heart.facts, "fetch_exemplars_by_vector", AsyncMock(return_value=[below]))
            results, stats = await run_recall_pipeline(self._QUERY, heart, brain, s)

            assert stats.n_exemplar == 0
            occ = [r for r in results if r.id == exemplar_id]
            assert len(occ) == 1 and occ[0].metadata.get("retrieval_leg") != "exemplar"
        finally:
            async with heart.db.session() as cleanup:
                f = await cleanup.get(Fact, exemplar_id)
                if f is not None:
                    await cleanup.delete(f)
                await cleanup.commit()

    async def test_surfaced_exemplars_tracked(self, heart, brain, settings, monkeypatch):
        # Codex r3: post-floor survivors get recall tracked (retrieval == access)
        # so stale_scan can't reap an actively-used exemplar; below-floor hits do not.
        above_id, below_id = uuid4(), uuid4()
        # Give both facts the SAME embedding so ordinary Stage-1 recall (which
        # also tracks access on its own hits, with no similarity floor) treats
        # them identically — the exemplar leg's extra +1 on the survivor is then
        # the ONLY difference between the two recall_counts, robust to whatever
        # Stage-1 does.
        query_vec = await heart._embeddings.embed(self._QUERY)
        async with heart.db.session() as seed:
            for fid, content in (
                (above_id, "tracking exemplar above floor\nlabel: 1"),
                (below_id, "tracking exemplar below floor\nlabel: 2"),
            ):
                seed.add(
                    Fact(
                        id=fid,
                        agent_id=heart.agent_id,
                        content=content,
                        source="exemplar_extractor",
                        active=True,
                        embedding=query_vec,
                        recall_count=0,
                    )
                )
            await seed.commit()
        try:
            s = settings.model_copy(update={"exemplar_mode_enabled": True})
            floor = s.exemplar_min_similarity
            hits = [
                ExemplarHit(id=above_id, content="tracking exemplar above floor\nlabel: 1", similarity=floor + 0.1),
                ExemplarHit(id=below_id, content="tracking exemplar below floor\nlabel: 2", similarity=floor - 0.1),
            ]
            monkeypatch.setattr(heart.facts, "has_exemplars", AsyncMock(return_value=True))
            monkeypatch.setattr(heart.facts, "fetch_exemplars_by_vector", AsyncMock(return_value=hits))
            await run_recall_pipeline(self._QUERY, heart, brain, s)

            async with heart.db.session() as check:
                above = await check.get(Fact, above_id)
                below = await check.get(Fact, below_id)
                # The leg tracked the survivor exactly once MORE than the
                # below-floor hit (which the leg never tracks).
                assert above.recall_count == below.recall_count + 1
                assert above.last_recalled_at is not None
        finally:
            async with heart.db.session() as cleanup:
                for fid in (above_id, below_id):
                    f = await cleanup.get(Fact, fid)
                    if f is not None:
                        await cleanup.delete(f)
                await cleanup.commit()


# ---------------------------------------------------------------------------
# Task 5: recall_deep rendering block + telemetry
# ---------------------------------------------------------------------------


def _exemplar_result(idx: int, similarity: float) -> PipelineResult:
    return PipelineResult(
        id=uuid4(),
        type="fact",
        description=f"a synthetic exemplar utterance {idx}\nlabel: {idx}",
        score=0.55 - 0.005 * idx,
        source="heart",
        metadata={"retrieval_leg": "exemplar", "label": str(idx), "similarity": similarity},
    )


class TestExemplarRendering:
    def test_exemplars_render_in_dedicated_block_not_heart_memory(self):
        normal_fact = PipelineResult(
            id=uuid4(),
            type="fact",
            description="An ordinary recalled fact.",
            score=0.9,
            source="heart",
            metadata={},
        )
        ex1 = _exemplar_result(1, 0.91)
        ex2 = _exemplar_result(2, 0.80)
        results = [normal_fact, ex1, ex2]
        stats = PipelineStats()

        text = _format_pipeline_text(results, stats, ["all"])

        assert "=== Nearest stored examples ===" in text
        assert "you may override" in text

        before, after = text.split("=== Nearest stored examples ===", 1)
        # Exemplar rows are absent from the Heart Memory section (everything
        # before the dedicated block) and present after it.
        assert "synthetic exemplar utterance 1" not in before
        assert "synthetic exemplar utterance 2" not in before
        assert "a synthetic exemplar utterance 1\nlabel: 1" in after
        assert "a synthetic exemplar utterance 2\nlabel: 2" in after
        assert "[sim 0.91]" in after
        assert "[sim 0.80]" in after

        # The normal fact still renders in the Heart Memory section.
        assert "An ordinary recalled fact." in before

    def test_no_exemplars_no_block(self):
        normal_fact = PipelineResult(
            id=uuid4(),
            type="fact",
            description="An ordinary recalled fact.",
            score=0.9,
            source="heart",
            metadata={},
        )
        results = [normal_fact]
        stats = PipelineStats()

        text = _format_pipeline_text(results, stats, ["all"])

        assert "=== Nearest stored examples ===" not in text


# ---------------------------------------------------------------------------
# Task 6: backfill pure-function logic (chunk grouping, qualification,
# ordinal continuation). DB e2e (embed/learn/dedup/cap) is already covered
# by TestExemplarIngest above -- these target only the backfill's own new
# logic: turning a flat episode_chunks row set into per-episode,
# ordinal-continuous ExemplarPair lists.
# ---------------------------------------------------------------------------

from scripts.backfill_exemplar_facts import (  # noqa: E402
    ChunkRow,
    build_episode_pairs,
    episode_qualifies,
    group_chunks_by_episode,
)


class TestExemplarBackfill:
    def test_group_chunks_by_episode_orders_by_chunk_index(self):
        ep_a, ep_b = uuid4(), uuid4()
        rows = [
            ChunkRow(episode_id=ep_a, chunk_index=1, content="a1"),
            ChunkRow(episode_id=ep_b, chunk_index=0, content="b0"),
            ChunkRow(episode_id=ep_a, chunk_index=0, content="a0"),
        ]

        grouped = group_chunks_by_episode(rows)

        assert grouped[ep_a] == ["a0", "a1"]
        assert grouped[ep_b] == ["b0"]

    def test_group_chunks_by_episode_input_order_irrelevant(self):
        """Rows arriving out of chunk_index order (e.g. an unordered result
        set) still group into the correct per-episode content order."""
        ep = uuid4()
        rows = [
            ChunkRow(episode_id=ep, chunk_index=2, content="c2"),
            ChunkRow(episode_id=ep, chunk_index=0, content="c0"),
            ChunkRow(episode_id=ep, chunk_index=1, content="c1"),
        ]

        grouped = group_chunks_by_episode(rows)

        assert grouped[ep] == ["c0", "c1", "c2"]

    def test_episode_qualifies_on_concatenated_density(self):
        # A chunk boundary can split a stream such that no SINGLE chunk
        # clears the density threshold alone, but the CONCATENATED text
        # across all of an episode's chunks does.
        chunk1 = "how do I reset my card pin\nlabel: 21\nmy card is lost\n"
        chunk2 = "label: 41\nwhat's the exchange rate\nlabel: 32\n"

        assert episode_qualifies([chunk1, chunk2], threshold=0.8)
        assert not episode_qualifies(["just chatting about the weather today"], threshold=0.8)

    def test_build_episode_pairs_continues_ordinal_across_chunks(self):
        chunk1 = "how do I reset my card pin\nlabel: 21\nmy card is lost\nlabel: 41\n"
        chunk2 = "what's the exchange rate\nlabel: 32\nwhen is the bank open\nlabel: 10\n"

        pairs = build_episode_pairs([chunk1, chunk2])

        assert [p.label for p in pairs] == ["21", "41", "32", "10"]
        # Ordinals form ONE continuous sequence across the chunk boundary,
        # not [0, 1] then [0, 1] again.
        assert [p.ordinal for p in pairs] == [0, 1, 2, 3]

    def test_build_episode_pairs_chunk_boundary_fragment_is_harmless(self):
        # An utterance split mid-text across the chunk boundary: chunk1's
        # dangling "my card is" has no label line and is dropped (parse_
        # exemplars already skips label-less utterances); chunk2's "lost"
        # pairs with label 41 as its own (fragment) utterance. The
        # fragment does not corrupt ordinal continuation.
        chunk1 = "how do I reset my card pin\nlabel: 21\nmy card is"
        chunk2 = "lost\nlabel: 41\n"

        pairs = build_episode_pairs([chunk1, chunk2])

        assert [p.label for p in pairs] == ["21", "41"]
        assert [p.ordinal for p in pairs] == [0, 1]

    def test_build_episode_pairs_empty_chunk_list(self):
        assert build_episode_pairs([]) == []


@pytest.mark.postgres_only
class TestExemplarBackfillWatermark:
    """The ROLLBACK KEY watermark must come from the DATABASE's own clock
    (`SELECT now()`), never the app host's `datetime.now()` -- clock skew
    between host and DB would otherwise make a later `--phase rollback
    --watermark <printed-value>`'s `created_at >= :watermark` predicate
    silently miss rows THIS run itself writes (the exact bug
    scripts/backfill_r3_entity_keys.py's `fetch_db_now` already fixed for
    R3; this pins the same fix for F086)."""

    async def test_fetch_db_now_returns_tz_aware_db_clock(self, session):
        from scripts.backfill_exemplar_facts import fetch_db_now

        result = await fetch_db_now(session)

        assert isinstance(result, datetime)
        assert result.tzinfo is not None

    async def test_run_backfill_watermark_not_sourced_from_app_clock(self, monkeypatch, capsys):
        import scripts.backfill_exemplar_facts as bf

        # Poison the app-host clock to an obviously-wrong, recognizable
        # value. A correct (DB-clock-sourced) implementation's printed
        # watermark must NOT reflect this value at all.
        class _PoisonedDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime(1999, 1, 1, tzinfo=UTC)

        monkeypatch.setattr(bf, "datetime", _PoisonedDatetime)

        rc = await bf._run_backfill(
            agent_id=f"exemplar-watermark-{uuid4()}",
            since=None,
            max_episodes=1,
            density_threshold=None,
            dry_run=True,
        )

        assert rc == 0
        out = capsys.readouterr().out
        assert "ROLLBACK KEY" in out
        assert "1999-01-01" not in out
