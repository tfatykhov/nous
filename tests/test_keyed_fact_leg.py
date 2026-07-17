"""R3.3 (F085): keyed retrieval leg - land-dark, additive-only, bounded.

Reconciliation note (brief vs. code): the brief's test snippets read
``off.results`` / ``out.stats.n_keyed``, but ``run_recall_pipeline`` returns a
plain ``(results, stats)`` tuple (confirmed at retrieval_pipeline.py:199 and
every real caller, e.g. tools.py:951 ``results, stats = await
run_recall_pipeline(...)``) — not an object with those attributes. Tests below
unpack the tuple directly.
"""
import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
import pytest_asyncio

from nous.api.retrieval_pipeline import run_recall_pipeline
from nous.brain.brain import Brain
from nous.heart.keys import extract_entity_candidates
from nous.heart.schemas import FactInput
from nous.storage.models import Fact, FactEntityKey

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def brain(db, settings):
    """Brain without embeddings (keyword-only mode).

    No conftest `brain` fixture exists — per-file pattern copied from
    tests/test_spreading_activation.py:26 (review arch-P1-A).
    """
    b = Brain(database=db, settings=settings)
    yield b
    await b.close()


@pytest_asyncio.fixture
async def seed_keyed_corpus(heart):
    """One active gold fact keyed {"marriage of figaro", "thomas kyd"}, plus
    one active=False fact sharing a key.

    Content is deliberately unrelated (no shared words) to every query used
    against this fixture in this file, and no embedding is stored, so Stage 1
    (heart.recall's vector + keyword legs) cannot surface these facts by
    accident — only the keyed leg's entity_key match can find them.

    Seeded via heart.db.session()+commit (own, already-committed connection)
    per review arch-P1-C: the keyed leg reads through its own pooled
    connection, so rows seeded on the conftest `session` fixture's
    rollback-isolated connection would be invisible to it.
    """
    agent_id = heart.agent_id
    gold_id = uuid.uuid4()
    superseded_id = uuid.uuid4()
    async with heart.db.session() as s:
        s.add(Fact(
            id=gold_id,
            agent_id=agent_id,
            content="A quiet archive contains records unrelated to opera or playwrights, filed under miscellany.",
            active=True,
            # codex P2: subject + event_date so the keyed leg's metadata
            # (fetch_by_entity_keys / _keyed_to_pipeline) can be asserted.
            subject="marriage of figaro",
            event_date=date(2026, 3, 15),
        ))
        s.add(Fact(
            id=superseded_id,
            agent_id=agent_id,
            content="An outdated ledger entry, since replaced, concerning miscellaneous archival matters.",
            active=False,
        ))
        await s.flush()
        s.add(FactEntityKey(fact_id=gold_id, entity_key="marriage of figaro", agent_id=agent_id))
        s.add(FactEntityKey(fact_id=gold_id, entity_key="thomas kyd", agent_id=agent_id))
        s.add(FactEntityKey(fact_id=superseded_id, entity_key="marriage of figaro", agent_id=agent_id))
        await s.commit()

    yield {"gold_id": gold_id, "superseded_id": superseded_id}

    async with heart.db.session() as cleanup:
        for fid in (gold_id, superseded_id):
            f = await cleanup.get(Fact, fid)
            if f is not None:
                await cleanup.delete(f)
        await cleanup.commit()


@pytest_asyncio.fixture
async def seed_keyed_corpus_many(heart):
    """5 active facts keyed "belgium" with distinct learned_at, content
    unrelated to the "Facts about Belgium" query text (no embedding either)
    so Stage 1 cannot surface them — only the keyed leg can.
    """
    agent_id = heart.agent_id
    ids = [uuid.uuid4() for _ in range(5)]
    base = datetime.now(UTC)
    async with heart.db.session() as s:
        for i, fid in enumerate(ids):
            s.add(Fact(
                id=fid,
                agent_id=agent_id,
                content=f"Unrelated historical footnote number {i} regarding a distant kingdom.",
                active=True,
                learned_at=base - timedelta(minutes=i),
            ))
        await s.flush()
        for fid in ids:
            s.add(FactEntityKey(fact_id=fid, entity_key="belgium", agent_id=agent_id))
        await s.commit()

    yield {"ids": ids}

    async with heart.db.session() as cleanup:
        for fid in ids:
            f = await cleanup.get(Fact, fid)
            if f is not None:
                await cleanup.delete(f)
        await cleanup.commit()


# ---------------------------------------------------------------------------
# Keyed leg behavior
# ---------------------------------------------------------------------------


@pytest.mark.postgres_only
class TestKeyedFactLeg:
    async def test_flag_on_without_candidates_matches_flag_off(self, heart, brain, settings, seed_keyed_corpus):
        # review devil-P3-3: comparing default-off vs explicit-off is tautological.
        # The real invariant: flag ON with a query yielding zero entity candidates
        # (no capitals, no quotes, nothing in vocab) takes the same path as OFF.
        q = "nothing here matches any indexed entity at all"
        off_results, _off_stats = await run_recall_pipeline(q, heart, brain, settings)
        on_settings = settings.model_copy(update={"keyed_fact_leg_enabled": True})
        on_results, _on_stats = await run_recall_pipeline(q, heart, brain, on_settings)
        assert [(r.id, r.score, r.metadata) for r in off_results] == \
               [(r.id, r.score, r.metadata) for r in on_results]
        # NOTE: the corpus vocab contains "marriage of figaro"/"thomas kyd"/"belgium";
        # none of these n-grams occur in q. True flag-OFF byte-identity vs the
        # pre-feature baseline is pinned by the existing recall_deep snapshot test.

    async def test_keyed_hit_retrieved_with_provenance(self, heart, brain, settings, seed_keyed_corpus):
        # seed_keyed_corpus stores a fact whose embedding will NOT rank for the
        # query (no embedding stored -> vector leg excludes it; content shares
        # no words with the query -> keyword leg excludes it too -> Stage 1
        # empty for this fact) but which carries entity keys
        # {"marriage of figaro", "thomas kyd"}.
        s = settings.model_copy(update={"keyed_fact_leg_enabled": True})
        results, stats = await run_recall_pipeline(
            'Who is the author of "The Marriage of Figaro"?', heart, brain, s
        )
        keyed = [r for r in results if r.metadata.get("retrieval_leg") == "keyed"]
        assert keyed and keyed[0].id == seed_keyed_corpus["gold_id"]
        assert keyed[0].type == "fact" and keyed[0].source == "heart"
        assert stats.keyed_leg_used and stats.n_keyed >= 1
        # codex P2: subject + event_date must reach metadata in the same
        # convention _heart_results_to_pipeline uses, so the recency
        # resolver can group keyed-only dated facts by subject.
        assert keyed[0].metadata.get("subject") == "marriage of figaro"
        assert keyed[0].metadata.get("event_date") == "2026-03-15"

    async def test_superseded_fact_not_returned(self, heart, brain, settings, seed_keyed_corpus):
        # seed an inactive fact sharing the entity key
        s = settings.model_copy(update={"keyed_fact_leg_enabled": True})
        results, _stats = await run_recall_pipeline(
            'Tell me about "The Marriage of Figaro"', heart, brain, s
        )
        ids = {r.id for r in results}
        assert seed_keyed_corpus["superseded_id"] not in ids

    async def test_k_cap_and_scores_bounded(self, heart, brain, settings, seed_keyed_corpus_many):
        s = settings.model_copy(update={"keyed_fact_leg_enabled": True, "keyed_fact_leg_k": 3})
        results, _stats = await run_recall_pipeline('Facts about "Belgium"', heart, brain, s)
        keyed = [r for r in results if r.metadata.get("retrieval_leg") == "keyed"]
        assert len(keyed) == 3
        assert all(0.0 <= r.score <= s.keyed_fact_leg_score for r in keyed)

    async def test_dedup_skips_existing_ids(self, heart, brain, settings):
        # When Stage 1 already returns the fact (engineered here via an
        # embedding identical to the query's own embedding, since
        # MockEmbeddingProvider is deterministic per exact text -> cosine
        # 1.0), the keyed leg must not add a second PipelineResult with the
        # same id: single occurrence overall + stats.n_keyed_dup == 1.
        s = settings.model_copy(update={"keyed_fact_leg_enabled": True})
        query = 'Tell me about "The Marriage of Figaro" and its disputed authorship.'
        fact_id = uuid.uuid4()
        vec = await heart._embeddings.embed(query)
        async with heart.db.session() as seed:
            seed.add(Fact(
                id=fact_id,
                agent_id=heart.agent_id,
                content="A fact whose embedding is engineered to rank first for this dedup test query.",
                active=True,
                embedding=vec,
            ))
            await seed.flush()
            seed.add(FactEntityKey(
                fact_id=fact_id, entity_key="marriage of figaro", agent_id=heart.agent_id,
            ))
            await seed.commit()

        try:
            results, stats = await run_recall_pipeline(query, heart, brain, s)
            occurrences = [r for r in results if r.id == fact_id]
            assert len(occurrences) == 1
            assert occurrences[0].metadata.get("retrieval_leg") != "keyed"
            assert stats.n_keyed_dup == 1
        finally:
            async with heart.db.session() as cleanup:
                f = await cleanup.get(Fact, fact_id)
                if f is not None:
                    await cleanup.delete(f)
                await cleanup.commit()

    async def test_memory_types_gates_keyed_leg(self, heart, brain, settings, seed_keyed_corpus):
        # codex P2: the keyed leg must only run when the requested scope
        # includes facts (search_all or "fact" in memory_types), mirroring
        # Stage 1.5's chunk-leg gate.
        s = settings.model_copy(update={"keyed_fact_leg_enabled": True})
        query = 'Who is the author of "The Marriage of Figaro"?'

        results, stats = await run_recall_pipeline(
            query, heart, brain, s, memory_types=["episode"]
        )
        assert not stats.keyed_leg_used
        assert not any(r.metadata.get("retrieval_leg") == "keyed" for r in results)

        results, stats = await run_recall_pipeline(
            query, heart, brain, s, memory_types=["fact"]
        )
        keyed = [r for r in results if r.metadata.get("retrieval_leg") == "keyed"]
        assert stats.keyed_leg_used
        assert keyed and keyed[0].id == seed_keyed_corpus["gold_id"]

    async def test_vocab_cache_invalidated_after_learn(self, heart, brain, settings):
        # codex P2: the entity-key vocabulary now caches on the FactManager
        # instance (TTL 300s) and must be invalidated the moment a new fact
        # is learned, or a fact learned mid-TTL stays invisible to the keyed
        # leg for up to 300s in this same process.
        s = settings.model_copy(update={"keyed_fact_leg_enabled": True})

        # Warm the cache BEFORE the new key exists.
        await heart.facts.entity_key_vocabulary()

        # heart.learn() runs against the real shared dev DB (postgres_only
        # tests share it), so Stage 1's vector leg may independently surface
        # this fact by pure noise-level cosine among a large corpus — that's
        # orthogonal to what's under test here. The invariant we assert is
        # that the KEYED LEG itself saw the fresh key: without invalidation,
        # `extract_entity_candidates` would find no candidates (vocab still
        # missing the new key), `keyed_leg_used` would stay False, and
        # `fetch_by_entity_keys` would never even run.
        result = await heart.learn(FactInput(
            content="An old town council once discussed unrelated matters about riverbank drainage systems.",
            entity_keys=["wobblecrank industrial device"],
            source="enumerative_extractor",
        ))
        try:
            results, stats = await run_recall_pipeline(
                "Please explain the wobblecrank industrial device thoroughly.",
                heart, brain, s,
            )
            assert stats.keyed_leg_used
            assert stats.n_keyed >= 1 or stats.n_keyed_dup >= 1
            assert any(r.id == result.id for r in results)
        finally:
            fact_id = getattr(result, "id", None)
            if fact_id is not None:
                async with heart.db.session() as cleanup:
                    dbfact = await cleanup.get(Fact, fact_id)
                    if dbfact is not None:
                        await cleanup.delete(dbfact)
                        await cleanup.commit()


# ---------------------------------------------------------------------------
# Entity-candidate extraction (NER-lite) — vocab leg + carry-forward coverage
# ---------------------------------------------------------------------------


class TestEntityCandidateVocabLeg:
    def test_vocab_recovers_lowercase_entity(self):
        vocab = frozenset({"marriage of figaro", "thomas kyd"})
        got = extract_entity_candidates("who wrote the marriage of figaro?", vocab=vocab)
        assert "marriage of figaro" in got

    def test_straight_quoted_span(self):
        got = extract_entity_candidates('He mentioned "Belgium" briefly.')
        assert "belgium" in got

    def test_curly_quoted_span(self):
        got = extract_entity_candidates("She read “The Great Gatsby” last week.")
        assert "great gatsby" in got

    def test_capitalized_span_with_connectors_mid_sentence(self):
        got = extract_entity_candidates("Mozart composed The Marriage of Figaro in his final years.")
        assert "marriage of figaro" in got

    def test_capitalized_span_with_curly_apostrophe(self):
        # final-review issue 1: _CAP_SPAN must accept U+2019 inside TitleCase
        # words so the span survives as one entity; normalize_key then strips
        # the apostrophe exactly as the write side does.
        got = extract_entity_candidates("Everyone discussed Don’t Look Now at dinner.")
        assert "dont look now" in got

    def test_two_coordinated_titles_yield_two_spans(self):
        got = extract_entity_candidates(
            "Mozart composed The Marriage of Figaro and The Barber of Seville during his career."
        )
        assert "marriage of figaro" in got
        assert "barber of seville" in got

    def test_max_candidates_cap_respected(self):
        text = " ".join(f'"Entity{i}"' for i in range(10))
        got = extract_entity_candidates(text, max_candidates=8)
        assert len(got) == 8

    def test_quoted_first_ordering(self):
        got = extract_entity_candidates(
            'He studied "The Art of War" after meeting General Sun Tzu.'
        )
        assert "art of war" in got
        assert "general sun tzu" in got
        assert got.index("art of war") < got.index("general sun tzu")
