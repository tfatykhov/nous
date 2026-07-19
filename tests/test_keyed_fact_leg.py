"""R3.3 (F085): keyed retrieval leg - land-dark, additive-only, bounded.

Reconciliation note (brief vs. code): the brief's test snippets read
``off.results`` / ``out.stats.n_keyed``, but ``run_recall_pipeline`` returns a
plain ``(results, stats)`` tuple (confirmed at retrieval_pipeline.py:199 and
every real caller, e.g. tools.py:951 ``results, stats = await
run_recall_pipeline(...)``) — not an object with those attributes. Tests below
unpack the tuple directly.
"""
import asyncio
import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
import pytest_asyncio

from nous.api.retrieval_pipeline import PipelineResult, PipelineStats, run_recall_pipeline
from nous.api.tools import _format_pipeline_text
from nous.brain.brain import Brain
from nous.heart.keys import extract_entity_candidates
from nous.heart.schemas import FactInput
from nous.storage.models import Episode, Fact, FactEntityKey

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
    episode_id = uuid.uuid4()
    async with heart.db.session() as s:
        # codex P2 round 6: real Episode row so the gold fact's
        # source_episode_id FK (heart.facts -> heart.episodes) is valid,
        # letting the keyed leg's session-grouping metadata be asserted.
        s.add(Episode(
            id=episode_id,
            agent_id=agent_id,
            summary="An unrelated archival review session, filed for record-keeping.",
        ))
        s.add(Fact(
            id=gold_id,
            agent_id=agent_id,
            content="A quiet archive contains records unrelated to opera or playwrights, filed under miscellany.",
            active=True,
            # codex P2: subject + event_date so the keyed leg's metadata
            # (fetch_by_entity_keys / _keyed_to_pipeline) can be asserted.
            subject="marriage of figaro",
            event_date=date(2026, 3, 15),
            source_episode_id=episode_id,
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

    yield {"gold_id": gold_id, "superseded_id": superseded_id, "episode_id": episode_id}

    async with heart.db.session() as cleanup:
        for fid in (gold_id, superseded_id):
            f = await cleanup.get(Fact, fid)
            if f is not None:
                await cleanup.delete(f)
        ep = await cleanup.get(Episode, episode_id)
        if ep is not None:
            await cleanup.delete(ep)
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
        # codex P2 round 6: source_episode_id must reach metadata too, or
        # the formatter's session-grouping buckets keyed hits into
        # "-- Other --" instead of their real episode.
        assert keyed[0].metadata.get("source_episode_id") == str(seed_keyed_corpus["episode_id"])

    async def test_keyed_recall_bumps_access_tracking(self, heart, seed_keyed_corpus):
        """codex P2 round 10: facts surfaced ONLY via fetch_by_entity_keys
        (never reached through FactManager.search) must still get
        recall_count/last_recalled_at updates — otherwise a fact in active
        keyed use looks unused to _phase_stale_scan and can be deactivated.
        """
        gold_id = seed_keyed_corpus["gold_id"]
        async with heart.db.session() as before_session:
            before = await before_session.get(Fact, gold_id)
            before_count = before.recall_count or 0
            before_recalled_at = before.last_recalled_at

        results = await heart.facts.fetch_by_entity_keys(["marriage of figaro"])
        assert any(r.id == gold_id for r in results)

        async with heart.db.session() as after_session:
            after = await after_session.get(Fact, gold_id)
            assert after.recall_count == before_count + 1
            assert after.last_recalled_at is not None
            if before_recalled_at is not None:
                assert after.last_recalled_at > before_recalled_at

    async def test_fetch_track_false_skips_access_tracking(self, heart, seed_keyed_corpus):
        # arch-P1: _get_fact does NOT exist in this file — use its own idiom
        # (heart.db.session + s.get(Fact, id), as at :189-192)
        gold = seed_keyed_corpus["gold_id"]
        async with heart.db.session() as s:
            before = (await s.get(Fact, gold)).recall_count
        rows = await heart.facts.fetch_by_entity_keys(["marriage of figaro"], limit=8, track=False)
        assert [r.id for r in rows] == [gold]
        async with heart.db.session() as s:
            after = (await s.get(Fact, gold)).recall_count
        assert after == before                                     # NOT tracked
        assert rows[0].attribute_key is not None or rows[0].attribute_key is None  # column present (no AttributeError)

    async def test_entity_keys_for_facts_groups_and_sorts(self, heart, seed_keyed_corpus):
        gold = seed_keyed_corpus["gold_id"]
        m = await heart.facts.entity_keys_for_facts([gold])
        assert m == {gold: ["marriage of figaro", "thomas kyd"]}   # alphabetical
        assert await heart.facts.entity_keys_for_facts([]) == {}

    async def test_entity_keys_for_facts_excludes_inactive(self, heart, seed_keyed_corpus):
        # the corpus's superseded (active=False) fact shares a key — must not appear
        sup = seed_keyed_corpus["superseded_id"]
        m = await heart.facts.entity_keys_for_facts([sup])
        assert m == {}

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

    async def test_vocab_cache_invalidation_survives_mid_transaction_race(self, heart, brain, settings):
        # codex P2 round 2: _learn's in-txn invalidation (cache=None, right
        # after the entity-key rows are added) runs INSIDE the still-open
        # write transaction — learn() doesn't commit until _learn returns.
        # A concurrent recall that rebuilds the vocab in that window runs on
        # a separate connection and, under READ COMMITTED isolation, can't
        # see the uncommitted row — it re-caches a STALE vocab that then
        # looks "fresh" for the rest of the TTL, with nothing left to
        # invalidate it again.
        #
        # Simulate this deterministically: `_emit_event` runs unconditionally
        # near the end of `_learn`, after the entity-key block and before
        # `_learn` returns to `learn()` (which then commits) — hooking it
        # lets us force a vocab rebuild from inside the still-open
        # transaction without any real concurrency/threading.
        orig_emit_event = heart.facts._emit_event

        async def racing_emit_event(session, event_type, data):
            if event_type == "fact_learned":
                await heart.facts.entity_key_vocabulary()
            return await orig_emit_event(session, event_type, data)

        heart.facts._emit_event = racing_emit_event

        result = await heart.learn(FactInput(
            content="A separate unrelated notice mentions nothing about the topic below.",
            entity_keys=["glimmerforge assembly protocol"],
            source="enumerative_extractor",
        ))
        try:
            # Without the round-2 post-commit re-invalidation, this would
            # return the vocab the racing rebuild poisoned mid-transaction
            # (missing the new key) for the rest of the 300s TTL.
            vocab = await heart.facts.entity_key_vocabulary()
            assert "glimmerforge assembly protocol" in vocab
        finally:
            heart.facts._emit_event = orig_emit_event
            fact_id = getattr(result, "id", None)
            if fact_id is not None:
                async with heart.db.session() as cleanup:
                    dbfact = await cleanup.get(Fact, fact_id)
                    if dbfact is not None:
                        await cleanup.delete(dbfact)
                        await cleanup.commit()

    async def test_late_rebuild_does_not_overwrite_cache_after_concurrent_write(self, heart, brain, settings):
        # codex P2 round 3: round 2's dirty flag only gates the WRITE side's
        # own post-commit re-invalidation — it does nothing to stop a
        # concurrently in-flight READ from storing a stale result after the
        # fact. A rebuild that STARTS (captures `gen`) before a write bumps
        # the generation counter, but FINISHES (would otherwise store) after,
        # must detect the mismatch and skip the store.
        #
        # Simulate the interleaving deterministically by wrapping db.session
        # so that, immediately after the query executes (but still inside
        # entity_key_vocabulary's `async with` block, before its gen
        # comparison), we bump heart.facts._entity_vocab_gen exactly as a
        # concurrent write's invalidation would.
        orig_db_session = heart.facts.db.session

        class _RacingSessionCM:
            def __init__(self, real_cm):
                self._real_cm = real_cm

            async def __aenter__(self):
                session = await self._real_cm.__aenter__()
                orig_execute = session.execute

                async def racing_execute(*args, **kwargs):
                    result = await orig_execute(*args, **kwargs)
                    heart.facts._entity_vocab_gen += 1
                    return result

                session.execute = racing_execute
                return session

            async def __aexit__(self, *exc_info):
                return await self._real_cm.__aexit__(*exc_info)

        def racing_session():
            return _RacingSessionCM(orig_db_session())

        heart.facts.db.session = racing_session
        try:
            vocab = await heart.facts.entity_key_vocabulary()
        finally:
            heart.facts.db.session = orig_db_session

        # The in-flight query's own result is still returned to the caller...
        assert isinstance(vocab, frozenset)
        # ...but must NOT have been stored, since a "write" landed mid-flight.
        assert heart.facts._entity_vocab_cache is None

        # A subsequent (unpatched) call rebuilds fresh and caches normally.
        vocab2 = await heart.facts.entity_key_vocabulary()
        assert heart.facts._entity_vocab_cache is not None
        assert heart.facts._entity_vocab_cache[0] == vocab2

    async def test_concurrent_learns_both_visible_in_vocab(self, heart, brain, settings):
        # codex P2 round 5: a shared self._entity_vocab_dirty boolean broke
        # under two overlapping learn() calls with entity_keys — whichever
        # call's post-commit `finally` ran first would clear the flag the
        # OTHER call had set, silently skipping that other call's own
        # post-commit invalidation. Real writer/writer interleaving is hard
        # to force deterministically, so this tests the user-visible
        # contract instead: after two concurrent learns (asyncio.gather)
        # with distinct entity_keys both complete, BOTH keys must be visible
        # in entity_key_vocabulary() — regardless of internal interleaving
        # order or which call's finally ran first.
        fi_a = FactInput(
            content="A council once discussed unrelated riverbank drainage systems entirely.",
            entity_keys=["quixotic harbor apparatus"],
            source="enumerative_extractor",
        )
        fi_b = FactInput(
            content="A separate archive holds unrelated records about miscellaneous filing systems.",
            entity_keys=["nebulous orchard registry"],
            source="enumerative_extractor",
        )
        result_a, result_b = await asyncio.gather(heart.learn(fi_a), heart.learn(fi_b))
        try:
            vocab = await heart.facts.entity_key_vocabulary()
            assert "quixotic harbor apparatus" in vocab
            assert "nebulous orchard registry" in vocab
        finally:
            for result in (result_a, result_b):
                fact_id = getattr(result, "id", None)
                if fact_id is not None:
                    async with heart.db.session() as cleanup:
                        dbfact = await cleanup.get(Fact, fact_id)
                        if dbfact is not None:
                            await cleanup.delete(dbfact)
                            await cleanup.commit()


# ---------------------------------------------------------------------------
# R3v2: bounded round-2 keyed retrieval
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def seed_hop_corpus(heart):
    """Two-hop shape for round-2 composition tests.

    Query used throughout ``TestKeyedR2``: ``'Report about "Alpha Station"
    relocation city'``. ``normalize_key`` strips the quotes/punctuation and
    folds it to the 6-token set {"report", "about", "alpha", "station",
    "relocation", "city"}.

    A (round-1 hit): entity keys {"alpha station", "bridge person",
    "zzz filler key"}. The query's own round-1 candidates are exactly
    ["alpha station"] (quoted span + capitalized span dedup to one key;
    "Report" is sentence-initial so it's excluded by the CAP_SPAN
    lookbehind) -- fetch_by_entity_keys on that single key returns only A.
    A's content shares zero tokens with the query and has no quoted/
    capitalized spans, so round-2's content-scan step (step 2) contributes
    nothing for A -- every round-2 key below comes from step 1 (A's own
    entity rows). Removing "alpha station" (already in seen_k from round 1)
    leaves 2 keys in alphabetical order: "bridge person", "zzz filler key".
    Both are produced unconditionally by step 1 (which is not itself capped),
    so with ``keyed_fact_leg_r2_max_keys=1`` the post-step total is always 2,
    guaranteeing truncation regardless of A's content.

    B (the hop, reachable only via "bridge person"): attribute_key=
    "relocation city" folds to {"relocation", "city"} -- both are query
    tokens, so attr_overlap(B) = 2. Content shares zero tokens with the
    query, so content_overlap(B) = 0.

    C (decoy, reachable the same way via "bridge person"): attribute_key=
    "unrelated" folds to {"unrelated"} -- zero overlap with the query
    tokens, so attr_overlap(C) = 0. Content also shares zero tokens with
    the query, so content_overlap(C) = 0.

    Ranking: B's sort key leads with -2 (attr_overlap=2) vs C's -0 -- B
    always outranks C on the first sort-key component alone, independent of
    content overlap, learned_at, or id.

    No embeddings are stored and no content overlaps the query, so Stage 1
    (heart.recall's vector + keyword legs) cannot surface A/B/C by
    accident -- only the keyed leg's entity_key match can find them.
    """
    agent_id = heart.agent_id
    a_id, b_id, c_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    base = datetime.now(UTC)
    async with heart.db.session() as s:
        s.add(Fact(
            id=a_id, agent_id=agent_id,
            content="Facility records mention routine filing procedures only.",
            active=True, learned_at=base - timedelta(minutes=2),
        ))
        s.add(Fact(
            id=b_id, agent_id=agent_id,
            content="A regional liaison relocated to new administrative duties elsewhere.",
            active=True, attribute_key="relocation city",
            learned_at=base - timedelta(minutes=1),
        ))
        s.add(Fact(
            id=c_id, agent_id=agent_id,
            content="An archival assistant filed unrelated administrative paperwork.",
            active=True, attribute_key="unrelated",
            learned_at=base,
        ))
        await s.flush()
        s.add(FactEntityKey(fact_id=a_id, entity_key="alpha station", agent_id=agent_id))
        s.add(FactEntityKey(fact_id=a_id, entity_key="bridge person", agent_id=agent_id))
        s.add(FactEntityKey(fact_id=a_id, entity_key="zzz filler key", agent_id=agent_id))
        s.add(FactEntityKey(fact_id=b_id, entity_key="bridge person", agent_id=agent_id))
        s.add(FactEntityKey(fact_id=b_id, entity_key="target city", agent_id=agent_id))
        s.add(FactEntityKey(fact_id=c_id, entity_key="bridge person", agent_id=agent_id))
        await s.commit()

    yield {"A": a_id, "B": b_id, "C": c_id}

    async with heart.db.session() as cleanup:
        for fid in (a_id, b_id, c_id):
            f = await cleanup.get(Fact, fid)
            if f is not None:
                await cleanup.delete(f)
        await cleanup.commit()


_HOP_QUERY = 'Report about "Alpha Station" relocation city'


@pytest.mark.postgres_only
class TestKeyedR2:
    async def test_rounds_1_default_byte_identical(self, heart, brain, settings, seed_hop_corpus):
        s1 = settings.model_copy(update={"keyed_fact_leg_enabled": True})  # rounds default 1
        base_results, base_stats = await run_recall_pipeline(_HOP_QUERY, heart, brain, s1)
        again_results, _ = await run_recall_pipeline(_HOP_QUERY, heart, brain, s1)
        assert [(r.id, r.score, r.metadata) for r in base_results] == \
               [(r.id, r.score, r.metadata) for r in again_results]
        assert base_stats.n_keyed_r2 == 0 and base_stats.keyed_r2_truncated is False
        assert not any(r.metadata.get("retrieval_leg") == "keyed_r2" for r in base_results)

    async def test_two_hop_composition(self, heart, brain, settings, seed_hop_corpus):
        s2 = settings.model_copy(update={"keyed_fact_leg_enabled": True, "keyed_fact_leg_rounds": 2})
        results, stats = await run_recall_pipeline(_HOP_QUERY, heart, brain, s2)
        r1 = [r for r in results if r.metadata.get("retrieval_leg") == "keyed"]
        r2 = [r for r in results if r.metadata.get("retrieval_leg") == "keyed_r2"]
        assert seed_hop_corpus["A"] in {r.id for r in r1}
        assert seed_hop_corpus["B"] in {r.id for r in r2}          # the hop
        assert stats.n_keyed_r2 >= 1 and stats.keyed_leg_used
        # band: every r2 score strictly below every r1 keyed score; r2 after r1 positionally
        assert max(x.score for x in r2) < min(x.score for x in r1)
        assert min(results.index(x) for x in r2) > max(results.index(x) for x in r1)

    async def test_ranking_attribute_overlap_beats_content_and_decoy(self, heart, brain, settings, seed_hop_corpus):
        # B (attribute_key overlaps 2 query tokens) must outrank decoy C
        # (0 overlap) when both are round-2 candidates. K2=1 keeps only the
        # top-ranked survivor.
        s = settings.model_copy(update={
            "keyed_fact_leg_enabled": True, "keyed_fact_leg_rounds": 2,
            "keyed_fact_leg_k2": 1,
        })
        results, _stats = await run_recall_pipeline(_HOP_QUERY, heart, brain, s)
        r2_ids = {r.id for r in results if r.metadata.get("retrieval_leg") == "keyed_r2"}
        assert r2_ids == {seed_hop_corpus["B"]}
        assert seed_hop_corpus["C"] not in r2_ids

    async def test_fanout_truncation_is_loud(self, heart, brain, settings, seed_hop_corpus, caplog):
        s = settings.model_copy(update={
            "keyed_fact_leg_enabled": True, "keyed_fact_leg_rounds": 2,
            "keyed_fact_leg_r2_max_keys": 1,
        })
        with caplog.at_level("INFO"):
            _, stats = await run_recall_pipeline(_HOP_QUERY, heart, brain, s)
        assert stats.keyed_r2_truncated is True
        assert any("keyed_r2" in rec.message for rec in caplog.records)   # surfaced telemetry

    async def test_r2_dedups_against_r1_and_survivors_only_tracked(self, heart, brain, settings, seed_hop_corpus):
        # A (an r1 hit) is also reachable via r2 keys -> must appear ONCE
        # (leg='keyed'), never duplicated as keyed_r2. With K2=1, decoy C is
        # fetched as an (untracked) round-2 candidate but not selected -> its
        # recall_count must stay unchanged; B is selected -> tracked once.
        s = settings.model_copy(update={
            "keyed_fact_leg_enabled": True, "keyed_fact_leg_rounds": 2,
            "keyed_fact_leg_k2": 1,
        })
        a_id, b_id, c_id = seed_hop_corpus["A"], seed_hop_corpus["B"], seed_hop_corpus["C"]

        async def _recall_count(fid):
            async with heart.db.session() as sess:
                return (await sess.get(Fact, fid)).recall_count or 0

        b_before, c_before = await _recall_count(b_id), await _recall_count(c_id)

        results, _stats = await run_recall_pipeline(_HOP_QUERY, heart, brain, s)

        occurrences_a = [r for r in results if r.id == a_id]
        assert len(occurrences_a) == 1
        assert occurrences_a[0].metadata.get("retrieval_leg") == "keyed"

        r2_ids = {r.id for r in results if r.metadata.get("retrieval_leg") == "keyed_r2"}
        assert r2_ids == {b_id}
        assert c_id not in {r.id for r in results}

        b_after, c_after = await _recall_count(b_id), await _recall_count(c_id)
        assert b_after == b_before + 1
        assert c_after == c_before


# ---------------------------------------------------------------------------
# _via_tag provenance rendering (pure formatter test, no DB) — final review fix 1
# ---------------------------------------------------------------------------


def _tagged_fact(retrieval_leg: str) -> PipelineResult:
    return PipelineResult(
        id=uuid.uuid4(), type="fact", description="Some fact content",
        score=0.5, source="heart", metadata={"retrieval_leg": retrieval_leg},
    )


class TestViaTagKeyedProvenance:
    def test_keyed_r1_tag(self):
        text = _format_pipeline_text([_tagged_fact("keyed")], PipelineStats(), ["all"])
        assert "[via keyed] Some fact content" in text
        assert "[via keyed-hop]" not in text

    def test_keyed_r2_tag_distinct_from_r1(self):
        text = _format_pipeline_text([_tagged_fact("keyed_r2")], PipelineStats(), ["all"])
        assert "[via keyed-hop] Some fact content" in text
        assert "[via keyed] " not in text  # r2 must not fall through to the r1 tag


# ---------------------------------------------------------------------------
# codex round 1 (P2): vocab-only content scan for round-2 key derivation
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def seed_junk_content_hop_corpus(heart):
    """Reproduces codex round-1 P2: ``extract_entity_candidates``'s internal
    ``out[:max_candidates]`` slice is quoted/cap-span-first, so >=
    ``keyed_fact_leg_r2_max_keys`` non-indexed junk spans in a round-1 hit's
    content can exhaust the cap before the vocab leg's real match is ever
    reached — even though the vocab leg DOES find it and append it to
    ``out``, it lands past the slice and never survives to the call site's
    own ``k in vocab`` filter.

    A is keyed ONLY on "alpha station" (the round-1 query key) — NOT on
    "bridge person" — so the round-2 hop key can ONLY come from step 2
    (content-scan), isolating the bug from step 1's entity-rows path. A's
    content packs 5 distinct quoted junk spans ("Junk One".."Junk Five",
    none of them vocab members) BEFORE a lowercase "bridge person" mention
    recoverable only by the vocab n-gram leg. With
    ``keyed_fact_leg_r2_max_keys=3`` (< 5 junk spans), the pre-fix
    extractor call (``extract_entity_candidates(content, vocab=vocab,
    max_candidates=3)``) returns exactly ["junk one", "junk two", "junk
    three"] — "bridge person" IS found internally but sits at out-index 5,
    past the ``[:3]`` slice, so it never reaches the call site.

    B is keyed on "bridge person" and is the only candidate reachable via
    that key — unambiguous once the key correctly reaches round 2.
    """
    agent_id = heart.agent_id
    a_id, b_id = uuid.uuid4(), uuid.uuid4()
    async with heart.db.session() as s:
        s.add(Fact(
            id=a_id, agent_id=agent_id,
            content=(
                '"Junk One" "Junk Two" "Junk Three" "Junk Four" "Junk Five" '
                "mentions bridge person eventually."
            ),
            active=True,
        ))
        s.add(Fact(
            id=b_id, agent_id=agent_id,
            content="A quiet office building underwent minor renovations recently.",
            active=True,
        ))
        await s.flush()
        s.add(FactEntityKey(fact_id=a_id, entity_key="alpha station", agent_id=agent_id))
        s.add(FactEntityKey(fact_id=b_id, entity_key="bridge person", agent_id=agent_id))
        await s.commit()

    yield {"A": a_id, "B": b_id}

    async with heart.db.session() as cleanup:
        for fid in (a_id, b_id):
            f = await cleanup.get(Fact, fid)
            if f is not None:
                await cleanup.delete(f)
        await cleanup.commit()


@pytest.mark.postgres_only
class TestCodexR1VocabOnlyContentScan:
    async def test_junk_spans_do_not_exhaust_r2_key_cap(
        self, heart, brain, settings, seed_junk_content_hop_corpus,
    ):
        s = settings.model_copy(update={
            "keyed_fact_leg_enabled": True, "keyed_fact_leg_rounds": 2,
            "keyed_fact_leg_r2_max_keys": 3,
        })
        results, stats = await run_recall_pipeline(_HOP_QUERY, heart, brain, s)
        r2_ids = {r.id for r in results if r.metadata.get("retrieval_leg") == "keyed_r2"}
        assert seed_junk_content_hop_corpus["B"] in r2_ids


class TestExtractEntityCandidatesVocabOnly:
    def test_vocab_only_skips_quoted_and_cap_spans(self):
        vocab = frozenset({"bridge person"})
        text = '"Junk One" "Junk Two" Alpha Beta mentions bridge person eventually.'
        got = extract_entity_candidates(text, vocab=vocab, vocab_only=True)
        assert got == ["bridge person"]
        assert "junk one" not in got
        assert "alpha beta" not in got

    def test_vocab_only_cap_respected(self):
        vocab = frozenset({"bridge person", "target city"})
        text = "mentions bridge person and target city both right here."
        got = extract_entity_candidates(
            text, vocab=vocab, vocab_only=True, max_candidates=1,
        )
        assert len(got) == 1

    def test_vocab_only_false_is_default_query_side_path_unchanged(self):
        vocab = frozenset({"marriage of figaro"})
        text = "who wrote the marriage of figaro?"
        assert extract_entity_candidates(text, vocab=vocab) == \
            extract_entity_candidates(text, vocab=vocab, vocab_only=False)


# ---------------------------------------------------------------------------
# codex round 2 (P2): exact key-budget hits must still report truncated
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def seed_exact_cap_hop_corpus(heart):
    """Reproduces codex round-2 P2: when round-1's own entity-key
    derivation (step 1) lands EXACTLY on ``keyed_fact_leg_r2_max_keys``, the
    content-scan loop's early-break (``if len(r2_keys) >= max_keys:
    break``) fires on its very FIRST iteration — before ANY r1 hit's
    content is examined — yet the final ``if len(r2_keys) > max_keys``
    check (strict greater-than) is False, so ``keyed_r2_truncated`` stayed
    False even though real content (and a real reachable third key) was
    never looked at.

    A1 and A2 are BOTH round-1 hits (both keyed on "alpha station", the
    query's own round-1 key). A1's own extra entity key is "widget alpha";
    A2's is "widget beta" — together (after removing "alpha station",
    already in seen_k from round 1) step 1 alone yields exactly 2 keys.
    With ``keyed_fact_leg_r2_max_keys=2``, the content-scan loop's FIRST
    iteration already sees ``len(r2_keys) == 2 >= 2`` and breaks
    immediately — neither A1's nor A2's content is ever scanned.

    A2's content contains a lowercase "bridge person" mention — a real
    vocab member (B is keyed on it) that step 2's content-scan WOULD have
    found had the loop not broken early. B never surfaces in the merged
    results either way (the fix only corrects the stats flag, not what
    gets examined) — asserted below to confirm the skip was real, not a
    coincidental no-op.
    """
    agent_id = heart.agent_id
    a1_id, a2_id, b_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    base = datetime.now(UTC)
    async with heart.db.session() as s:
        s.add(Fact(
            id=a1_id, agent_id=agent_id,
            content="A routine facility memo about nothing notable.",
            active=True, learned_at=base - timedelta(minutes=2),
        ))
        s.add(Fact(
            id=a2_id, agent_id=agent_id,
            content="Additional notes mention bridge person as a contact.",
            active=True, learned_at=base - timedelta(minutes=1),
        ))
        s.add(Fact(
            id=b_id, agent_id=agent_id,
            content="An unrelated office memo about routine matters.",
            active=True, learned_at=base,
        ))
        await s.flush()
        s.add(FactEntityKey(fact_id=a1_id, entity_key="alpha station", agent_id=agent_id))
        s.add(FactEntityKey(fact_id=a1_id, entity_key="widget alpha", agent_id=agent_id))
        s.add(FactEntityKey(fact_id=a2_id, entity_key="alpha station", agent_id=agent_id))
        s.add(FactEntityKey(fact_id=a2_id, entity_key="widget beta", agent_id=agent_id))
        s.add(FactEntityKey(fact_id=b_id, entity_key="bridge person", agent_id=agent_id))
        await s.commit()

    yield {"A1": a1_id, "A2": a2_id, "B": b_id}

    async with heart.db.session() as cleanup:
        for fid in (a1_id, a2_id, b_id):
            f = await cleanup.get(Fact, fid)
            if f is not None:
                await cleanup.delete(f)
        await cleanup.commit()


@pytest.mark.postgres_only
class TestCodexR2ExactCapTruncation:
    async def test_exact_cap_hit_reports_truncated(
        self, heart, brain, settings, seed_exact_cap_hop_corpus,
    ):
        s = settings.model_copy(update={
            "keyed_fact_leg_enabled": True, "keyed_fact_leg_rounds": 2,
            "keyed_fact_leg_r2_max_keys": 2,
        })
        results, stats = await run_recall_pipeline(_HOP_QUERY, heart, brain, s)
        assert stats.keyed_r2_truncated is True
        # Confirms the skip was real: B (reachable only via the unexamined
        # "bridge person" content mention) never surfaces.
        assert seed_exact_cap_hop_corpus["B"] not in {r.id for r in results}


# ---------------------------------------------------------------------------
# codex round 3 (P2): K2 selection must happen at assembly, not in the stage
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def seed_cross_leg_dup_hop_corpus(heart):
    """Reproduces codex round-3 P2: pre-fix, K2 selection happened INSIDE
    Stage 1.6, before ``existing_ids`` (the cross-leg dedup set) was
    complete. A candidate already surfaced by another leg (D, forced into
    Stage 1 via an embedding matching the query) could still win the only
    K2 slot at stage time (it ranks first: ``attribute_key="relocation
    city"`` overlaps 2 query tokens), only to be dropped as a duplicate at
    assembly — silently wasting the slot a fresh, never-before-seen hop
    candidate (E) could have filled.

    A is the round-1 hit (keyed on "alpha station", the query's own
    round-1 key) and also owns the hop key "bridge person" directly (step
    1 alone supplies it — no content-scan complexity needed here). D and E
    are both reachable via "bridge person": D ranks first (attribute_key
    overlap) but duplicates a Stage-1 hit; E ranks second and is otherwise
    unreachable except via the hop.
    """
    agent_id = heart.agent_id
    a_id, d_id, e_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    vec = await heart._embeddings.embed(_HOP_QUERY)
    async with heart.db.session() as s:
        s.add(Fact(
            id=a_id, agent_id=agent_id,
            content="Facility records mention routine filing procedures only.",
            active=True,
        ))
        s.add(Fact(
            id=d_id, agent_id=agent_id,
            content="A separate memo describes routine intake procedures.",
            active=True, embedding=vec, attribute_key="relocation city",
        ))
        s.add(Fact(
            id=e_id, agent_id=agent_id,
            content="A different memo covers unrelated inventory matters.",
            active=True,
        ))
        await s.flush()
        s.add(FactEntityKey(fact_id=a_id, entity_key="alpha station", agent_id=agent_id))
        s.add(FactEntityKey(fact_id=a_id, entity_key="bridge person", agent_id=agent_id))
        s.add(FactEntityKey(fact_id=d_id, entity_key="bridge person", agent_id=agent_id))
        s.add(FactEntityKey(fact_id=e_id, entity_key="bridge person", agent_id=agent_id))
        await s.commit()

    yield {"A": a_id, "D": d_id, "E": e_id}

    async with heart.db.session() as cleanup:
        for fid in (a_id, d_id, e_id):
            f = await cleanup.get(Fact, fid)
            if f is not None:
                await cleanup.delete(f)
        await cleanup.commit()


@pytest.mark.postgres_only
class TestCodexR3K2SelectionAtAssembly:
    async def test_cross_leg_duplicate_does_not_waste_k2_slot(
        self, heart, brain, settings, seed_cross_leg_dup_hop_corpus,
    ):
        s = settings.model_copy(update={
            "keyed_fact_leg_enabled": True, "keyed_fact_leg_rounds": 2,
            "keyed_fact_leg_k2": 1,
        })
        results, stats = await run_recall_pipeline(_HOP_QUERY, heart, brain, s)
        d_id = seed_cross_leg_dup_hop_corpus["D"]
        e_id = seed_cross_leg_dup_hop_corpus["E"]

        # D surfaces exactly once (via Stage 1, not re-added as keyed_r2).
        d_occurrences = [r for r in results if r.id == d_id]
        assert len(d_occurrences) == 1
        assert d_occurrences[0].metadata.get("retrieval_leg") != "keyed_r2"

        # E -- the fresh hop candidate -- must merge despite K2=1, since D
        # (which ranks ahead of it) is a cross-leg duplicate that must not
        # consume the only slot.
        e_hit = [r for r in results if r.id == e_id]
        assert e_hit and e_hit[0].metadata.get("retrieval_leg") == "keyed_r2"
        assert stats.n_keyed_r2_dup >= 1

    async def test_duplicate_survivor_not_double_tracked(
        self, heart, brain, settings, seed_cross_leg_dup_hop_corpus,
    ):
        # The round-2 assembly step must track_access ONLY its own K2
        # survivor set. Verified by spying on the call shape rather than
        # recall_count deltas: Stage 1's own fact search ALSO tracks access
        # via a fire-and-forget asyncio task (facts.py's _fire_track_access,
        # scheduled via loop.create_task, not awaited) -- its completion
        # relative to run_recall_pipeline returning is not deterministic, so
        # asserting recall_count deltas for D would be racy. The round-2
        # assembly call is awaited inline, so its exact argument shape IS
        # deterministic: it passes ONLY the K2 survivor ids.
        s = settings.model_copy(update={
            "keyed_fact_leg_enabled": True, "keyed_fact_leg_rounds": 2,
            "keyed_fact_leg_k2": 1,
        })
        e_id = seed_cross_leg_dup_hop_corpus["E"]

        tracked_calls: list[list] = []
        orig_track_access = heart.facts.track_access

        async def _spy_track_access(fact_ids):
            tracked_calls.append(list(fact_ids))
            return await orig_track_access(fact_ids)

        heart.facts.track_access = _spy_track_access
        try:
            await run_recall_pipeline(_HOP_QUERY, heart, brain, s)
        finally:
            heart.facts.track_access = orig_track_access

        # A call shaped exactly [e_id] can only be round-2's own survivor
        # tracking -- Stage 1's fire-and-forget call (if it races into this
        # window at all) would carry every leg-1 hit's id, never e_id alone,
        # since E has no embedding/content overlap and Stage 1 never
        # returns it.
        assert [e_id] in tracked_calls


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

    def test_vocab_recovers_long_lowercase_key_beyond_old_4_token_cap(self):
        # codex P2 round 2: the n-gram window used to be a fixed 4 tokens;
        # this 6-token key would previously never match via the vocab leg
        # (the only path that can recover a lowercase mention at all).
        vocab = frozenset({"national museum of african american history"})
        got = extract_entity_candidates(
            "I visited the national museum of african american history yesterday.",
            vocab=vocab,
        )
        assert "national museum of african american history" in got

    def test_vocab_ngram_window_capped_at_8_tokens(self):
        # The window is derived from the vocab's longest key but capped at 8
        # to bound the scan — a 9-token key is documented as NOT matched
        # rather than left as an implicit gap.
        long_key = "one two three four five six seven eight nine"  # 9 tokens
        vocab = frozenset({long_key})
        got = extract_entity_candidates(
            f"reference to {long_key} appears in the text", vocab=vocab
        )
        assert long_key not in got

    def test_possessive_query_recovers_vocab_key(self):
        """codex P2 round 12: normalize_key("Tim's") used to yield "tims",
        so a query mentioning "Tim's ..." could never match a stored key
        "tim" — every leg here (capitalized-span, vocab n-gram) routes
        through normalize_key. Possessive-suffix stripping fixes both."""
        vocab = frozenset({"tim", "belgium"})
        got = extract_entity_candidates("when was Tim's trip to Belgium?", vocab=vocab)
        assert "tim" in got
        assert "belgium" in got

    def test_possessive_and_contraction_do_not_produce_quoted_junk(self):
        """codex P2 round 15: the single-quote _QUOTED alternative used to
        match bare across ANY two apostrophes regardless of word-boundary
        context — "what's Tim's kitchen" gave it a contraction's apostrophe
        and a possessive's apostrophe to pair up, capturing the junk span
        between them ("s tim") as if it were a genuine quoted mention.
        Guarded with non-word-context delimiters, no such junk span is
        produced; "Tim" and "Riverside Cafe" still arrive via CAP_SPAN
        exactly as before (unaffected by the single-quote guard).
        """
        got = extract_entity_candidates("what's Tim's kitchen at the Riverside Cafe?")
        assert "s tim" not in got
        assert "riverside cafe" in got

    def test_straight_single_quoted_span_still_extracted(self):
        """The paired-delimiter guard must not break a genuine single-quoted
        span bounded by non-word context (space/punctuation) on both
        sides."""
        got = extract_entity_candidates("He read 'Belgium' aloud")
        assert "belgium" in got

    def test_possessive_junk_no_longer_exhausts_max_candidates_budget(self):
        """codex P2 round 15: quoted-first insertion order means junk quote
        spans used to occupy the FRONT of the candidate list — since
        max_candidates truncates the FINAL list rather than gating
        collection, 3 possessive/contraction pairs could fill the entire
        budget before the vocab leg (which runs LAST) ever got a chance to
        add a real match. All three subjects here are lowercase so
        CAP_SPAN can't independently recover them either — isolating the
        effect to the quoted-junk fix.
        """
        vocab = frozenset({"marriage of figaro"})
        text = (
            "what's everyone's excuse, who's anybody's guess, and where's "
            "nobody's answer regarding the marriage of figaro?"
        )
        got = extract_entity_candidates(text, vocab=vocab, max_candidates=3)
        assert "marriage of figaro" in got
