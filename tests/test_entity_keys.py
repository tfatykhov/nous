"""R3 (F085): canonical key normalization + entity-candidate extraction."""
import unicodedata
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import func, select

from nous.heart.keys import extract_entity_candidates, is_keyable_entity, normalize_key
from nous.heart.schemas import FactInput, FactRejected
from nous.storage.models import Fact, FactEntityKey


class TestNormalizeKeyV2:
    def test_underscores_become_spaces(self):
        assert normalize_key("thomas_kyd") == "thomas kyd"

    def test_leading_article_stripped(self):
        assert normalize_key("The Marriage of Figaro") == "marriage of figaro"
        assert normalize_key("a red car") == "red car"
        assert normalize_key("An Apple") == "apple"

    def test_article_strip_iterates_to_fixpoint(self):
        # single-pass stripping would return "a red car" here
        assert normalize_key("the a red car") == "red car"

    def test_bare_article_is_preserved(self):
        # the whole key IS the article -> keep it rather than return None
        assert normalize_key("the") == "the"

    def test_intra_word_hyphen_preserved(self):
        assert normalize_key("cross-encoder") == "cross-encoder"

    def test_dangling_hyphen_removed(self):
        assert normalize_key("cross -encoder") == "cross encoder"
        assert normalize_key("- leading") == "leading"

    def test_nfc_unicode(self):
        composed = "café"                                   # U+00E9
        decomposed = unicodedata.normalize("NFD", "café")   # e + U+0301
        assert normalize_key(composed) == normalize_key(decomposed) == "café"

    def test_possessive_and_punctuation(self):
        assert normalize_key("Tim's Laptop") == "tims laptop"
        assert normalize_key("  RED   Car!! ") == "red car"

    def test_empty_none(self):
        assert normalize_key(None) is None
        assert normalize_key("") is None
        assert normalize_key("   ") is None
        assert normalize_key("!!!") is None

    def test_max_len_cap(self):
        assert len(normalize_key("x" * 300)) == 200
        assert len(normalize_key("x" * 300, max_len=100)) == 100

    def test_idempotent_property(self):
        cases = [
            "The Marriage of Figaro", "thomas_kyd", "the a red car",
            "cross-encoder", "Tim's Laptop", "café",
            unicodedata.normalize("NFD", "café"),
            "the " + "ab-" * 80,   # truncation lands mid-token -> dangling hyphen
            "A  b__c--d", "-", "the the the", "an an apple",
        ]
        for raw in cases:
            once = normalize_key(raw)
            assert normalize_key(once) == once, raw


@pytest_asyncio.fixture
async def make_fact(session):
    """Function-scoped factory for a bare heart.facts row.

    No `make_fact` fixture exists anywhere in the suite (other test files
    insert facts via heart.learn() instead) — this local factory just needs
    a row to hang FactEntityKey rows off of, so it skips the Heart pipeline.
    """
    async def _make_fact(**overrides):
        defaults = {
            "id": uuid.uuid4(),
            "agent_id": "test-entity-keys-agent",
            "content": "default fact content for entity key schema tests",
            "active": True,
        }
        defaults.update(overrides)
        fact = Fact(**defaults)
        session.add(fact)
        await session.flush()
        return fact

    return _make_fact


@pytest.mark.postgres_only
class TestFactEntityKeysSchema:
    async def test_insert_and_cascade_delete(self, session, make_fact):
        # make_fact: use the existing fixture pattern from test_write_path_adjudication.py
        # (insert a Fact row directly); if no fixture exists, create the Fact inline.
        fact = await make_fact(content="The author of X is Thomas Kyd.")
        session.add(FactEntityKey(fact_id=fact.id, entity_key="thomas kyd", agent_id=fact.agent_id))
        session.add(FactEntityKey(fact_id=fact.id, entity_key="x", agent_id=fact.agent_id))
        await session.flush()
        rows = (await session.execute(
            select(FactEntityKey).where(FactEntityKey.fact_id == fact.id)
        )).scalars().all()
        assert {r.entity_key for r in rows} == {"thomas kyd", "x"}
        await session.delete(fact)   # hard delete only in tests: FK must CASCADE
        await session.flush()
        rows = (await session.execute(
            select(FactEntityKey).where(FactEntityKey.fact_id == fact.id)
        )).scalars().all()
        assert rows == []


class TestStopPolicy:
    def test_scalars_rejected(self):
        assert not is_keyable_entity("1876", min_chars=3)      # numeric
        assert not is_keyable_entity("12.5", min_chars=3)
        assert not is_keyable_entity("ab", min_chars=3)        # too short
        assert not is_keyable_entity("red", min_chars=3)       # scalar stoplist
        assert not is_keyable_entity("true", min_chars=3)

    def test_entities_accepted(self):
        assert is_keyable_entity("thomas kyd", min_chars=3)
        assert is_keyable_entity("belgium", min_chars=3)
        assert is_keyable_entity("cross-encoder", min_chars=3)


class TestExtractorEntityEmission:
    def test_to_fact_inputs_builds_entity_keys(self):
        from types import SimpleNamespace

        from nous.handlers.enumerative_extractor import EnumerativeExtractor

        settings = SimpleNamespace(
            temporal_extraction_enabled=False,
            entity_keys_max_per_fact=8,
            entity_key_min_chars=3,
        )
        ex = EnumerativeExtractor(heart=None, settings=settings, llm_client=None, embedder=None)
        raw = [{
            "content": "The author of The Marriage of Figaro is Thomas Kyd.",
            "subject_key": "The Marriage of Figaro",
            "attribute_key": "author",
            "entities": ["The Marriage of Figaro", "Thomas Kyd", "1876", "red"],
        }]
        (fi,) = ex._to_fact_inputs(raw, chunk_index=0, episode_id=None)
        assert fi.subject_key == "marriage of figaro"
        # subject key unioned; scalars dropped; all normalized
        assert fi.entity_keys == ["marriage of figaro", "thomas kyd"]

    def test_entity_keys_capped(self):
        from types import SimpleNamespace

        from nous.handlers.enumerative_extractor import EnumerativeExtractor

        settings = SimpleNamespace(
            temporal_extraction_enabled=False,
            entity_keys_max_per_fact=3,
            entity_key_min_chars=3,
        )
        ex = EnumerativeExtractor(heart=None, settings=settings, llm_client=None, embedder=None)
        raw = [{
            "content": "c.", "subject_key": "subj",
            "attribute_key": "attr",
            "entities": [f"entity number {i}" for i in range(10)],
        }]
        (fi,) = ex._to_fact_inputs(raw, chunk_index=0, episode_id=None)
        assert len(fi.entity_keys) == 3
        assert fi.entity_keys[0] == "subj"   # subject first (when it passes the stop-policy)

    def test_entities_key_absent_falls_back_to_subject_only(self):
        """Backward-compat pin: a raw fact dict with no "entities" key at all
        (pre-R3.1 extraction output, or any producer that omits it) must not
        raise — `raw_entities = f.get("entities") or []` degrades to
        subject-only entity_keys.
        """
        from types import SimpleNamespace

        from nous.handlers.enumerative_extractor import EnumerativeExtractor

        settings = SimpleNamespace(
            temporal_extraction_enabled=False,
            entity_keys_max_per_fact=8,
            entity_key_min_chars=3,
        )
        ex = EnumerativeExtractor(heart=None, settings=settings, llm_client=None, embedder=None)
        raw = [{
            "content": "The Marriage of Figaro was composed by Mozart.",
            "subject_key": "The Marriage of Figaro",
            "attribute_key": "composer",
            # no "entities" key
        }]
        (fi,) = ex._to_fact_inputs(raw, chunk_index=0, episode_id=None)
        assert fi.entity_keys == ["marriage of figaro"]
        # codex P2 round 6: KEY absent (not present-but-empty) means entity
        # extraction is NOT complete for this fact's object/value side.
        assert fi.entity_extraction_complete is False

    def test_entities_key_present_empty_marks_extraction_complete(self):
        """codex P2 round 6 sibling case: an explicit "entities": [] means
        the LLM reported its full (empty) participating-entity set — entity
        extraction for this fact IS complete, unlike the absent-key case
        above.
        """
        from types import SimpleNamespace

        from nous.handlers.enumerative_extractor import EnumerativeExtractor

        settings = SimpleNamespace(
            temporal_extraction_enabled=False,
            entity_keys_max_per_fact=8,
            entity_key_min_chars=3,
        )
        ex = EnumerativeExtractor(heart=None, settings=settings, llm_client=None, embedder=None)
        raw = [{
            "content": "The observability platform reports high uptime consistently.",
            "subject_key": "observability platform",
            "attribute_key": "uptime",
            "entities": [],
        }]
        (fi,) = ex._to_fact_inputs(raw, chunk_index=0, episode_id=None)
        assert fi.entity_extraction_complete is True

    def test_entities_key_absent_scalar_subject_yields_empty(self):
        """Same backward-compat case, but the subject itself fails the
        stop-policy (scalar) — entity_keys must come back empty, not raise.
        """
        from types import SimpleNamespace

        from nous.handlers.enumerative_extractor import EnumerativeExtractor

        settings = SimpleNamespace(
            temporal_extraction_enabled=False,
            entity_keys_max_per_fact=8,
            entity_key_min_chars=3,
        )
        ex = EnumerativeExtractor(heart=None, settings=settings, llm_client=None, embedder=None)
        raw = [{
            "content": "The car is red.",
            "subject_key": "red",
            "attribute_key": "color",
            # no "entities" key
        }]
        (fi,) = ex._to_fact_inputs(raw, chunk_index=0, episode_id=None)
        assert fi.entity_keys == []


@pytest.mark.postgres_only
class TestLearnWritesEntityRows:
    async def test_entity_rows_same_txn_and_stamp(self, heart, session):
        fi = FactInput(
            content="The author of The Marriage of Figaro is Thomas Kyd.",
            subject="marriage of figaro",
            subject_key="marriage of figaro",
            attribute_key="author",
            entity_keys=["marriage of figaro", "thomas kyd"],
            source="enumerative_extractor",
        )
        result = await heart.learn(fi)
        try:
            rows = (await session.execute(
                select(FactEntityKey).where(FactEntityKey.fact_id == result.id)
            )).scalars().all()
            assert {r.entity_key for r in rows} == {"marriage of figaro", "thomas kyd"}
            fact = await session.get(Fact, result.id)
            assert fact.entity_keys_extracted_at is not None
        finally:
            # heart.learn() commits on its OWN connection (bypasses the
            # session fixture's rollback isolation), so the row survives
            # the test unless hard-deleted here. Entity rows CASCADE.
            # result may be a FactRejected (no .id) if learn() unexpectedly
            # rejects — guard so cleanup itself never masks the real failure.
            fact_id = getattr(result, "id", None)
            if fact_id is not None:
                async with heart.db.session() as cleanup:
                    dbfact = await cleanup.get(Fact, fact_id)
                    if dbfact is not None:
                        await cleanup.delete(dbfact)
                        await cleanup.commit()

    async def test_entities_key_absent_leaves_watermark_null_after_learn(self, heart, session, settings):
        """codex P2 round 6 end-to-end: a raw LLM fact dict that omits the
        "entities" field entirely produces entity_extraction_complete=False
        (see TestExtractorEntityEmission above); heart.learn() must still
        insert whatever entity rows it has (the subject key), but leave
        entity_keys_extracted_at NULL so a future backfill pass revisits
        this fact for value-side extraction.
        """
        from nous.handlers.enumerative_extractor import EnumerativeExtractor

        ex = EnumerativeExtractor(heart=heart, settings=settings, llm_client=None, embedder=None)
        raw = [{
            "content": "The author of The Marriage of Figaro is Thomas Kyd.",
            "subject_key": "The Marriage of Figaro",
            "attribute_key": "author",
            # no "entities" key at all
        }]
        (fi,) = ex._to_fact_inputs(raw, chunk_index=0, episode_id=None)
        assert fi.entity_extraction_complete is False

        result = await heart.learn(fi)
        try:
            rows = (await session.execute(
                select(FactEntityKey).where(FactEntityKey.fact_id == result.id)
            )).scalars().all()
            assert {r.entity_key for r in rows} == {"marriage of figaro"}
            fact = await session.get(Fact, result.id)
            assert fact.entity_keys_extracted_at is None
        finally:
            fact_id = getattr(result, "id", None)
            if fact_id is not None:
                async with heart.db.session() as cleanup:
                    dbfact = await cleanup.get(Fact, fact_id)
                    if dbfact is not None:
                        await cleanup.delete(dbfact)
                        await cleanup.commit()

    async def test_entities_key_present_empty_stamps_watermark_after_learn(self, heart, session, settings):
        """Sibling case: raw dict WITH "entities": [] (LLM explicitly
        reported no additional entities) produces
        entity_extraction_complete=True — the watermark IS stamped since
        this fact's entity extraction is genuinely done.
        """
        from nous.handlers.enumerative_extractor import EnumerativeExtractor

        ex = EnumerativeExtractor(heart=heart, settings=settings, llm_client=None, embedder=None)
        raw = [{
            "content": "The observability platform reports high uptime consistently.",
            "subject_key": "observability platform",
            "attribute_key": "uptime",
            "entities": [],
        }]
        (fi,) = ex._to_fact_inputs(raw, chunk_index=0, episode_id=None)
        assert fi.entity_extraction_complete is True

        result = await heart.learn(fi)
        try:
            fact = await session.get(Fact, result.id)
            assert fact.entity_keys_extracted_at is not None
        finally:
            fact_id = getattr(result, "id", None)
            if fact_id is not None:
                async with heart.db.session() as cleanup:
                    dbfact = await cleanup.get(Fact, fact_id)
                    if dbfact is not None:
                        await cleanup.delete(dbfact)
                        await cleanup.commit()

    async def test_junk_entity_keys_filtered_at_learn(self, heart, session):
        """codex P2 round 4: FactInput.entity_keys can be passed directly to
        Heart.learn, bypassing the extractor's is_keyable_entity stop-policy
        entirely (enumerative_extractor.py:327). _learn's insert loop must
        enforce the same gate itself, or a caller can persist junk keys
        ("red", "1876", 2-char values).
        """
        fi = FactInput(
            content="The author of The Marriage of Figaro is Thomas Kyd.",
            subject="marriage of figaro",
            entity_keys=["red", "1876", "ab", "thomas kyd"],
            source="enumerative_extractor",
        )
        result = await heart.learn(fi)
        try:
            rows = (await session.execute(
                select(FactEntityKey).where(FactEntityKey.fact_id == result.id)
            )).scalars().all()
            assert {r.entity_key for r in rows} == {"thomas kyd"}
            fact = await session.get(Fact, result.id)
            # Stamped even though 3 of the 4 candidates were filtered out —
            # the fact WAS processed for entity extraction; nothing left to
            # retry.
            assert fact.entity_keys_extracted_at is not None
        finally:
            fact_id = getattr(result, "id", None)
            if fact_id is not None:
                async with heart.db.session() as cleanup:
                    dbfact = await cleanup.get(Fact, fact_id)
                    if dbfact is not None:
                        await cleanup.delete(dbfact)
                        await cleanup.commit()

    async def test_rejected_fact_writes_no_rows(self, heart, session):
        fi = FactInput(content="x", subject="s", entity_keys=["thomas kyd"])  # below min-content floor
        result = await heart.learn(fi)
        assert isinstance(result, FactRejected)
        n = (await session.execute(
            select(func.count()).select_from(FactEntityKey)
            .where(FactEntityKey.entity_key == "thomas kyd")
        )).scalar_one()
        assert n == 0

    async def test_confirm_duplicate_backfills_seeded_entity_key_on_conflict(self, heart):
        """R3 review (Minor, riskiest new path): _confirm_duplicate's entity-key
        backfill (facts.py ~1257-1274) must tolerate a PK collision when the
        dupe row already carries a FactEntityKey seeded by phase_seed's
        backfill — which writes subject-key rows WITHOUT stamping
        entity_keys_extracted_at. on_conflict_do_nothing must absorb the
        collision rather than aborting the whole learn txn, and the new
        (non-colliding) key must still land alongside it.

        codex P2 round 4: also carries a junk candidate ("1876") to confirm
        the stop-policy gate applies on this path too, mirroring _learn's.
        """
        vec = [1.0] + [0.0] * 1535  # unit vector: identical vec -> cosine 1.0 dupe match
        content = "The observability platform reports a service uptime of ninety nine point nine percent."
        fact_id = uuid.uuid4()

        # Seed the dupe row + one phase_seed-style entity row in a SEPARATE,
        # already-committed transaction (mirrors a sleep-cycle backfill that
        # ran before the live learn() call below).
        async with heart.db.session() as seed:
            seed.add(Fact(
                id=fact_id,
                agent_id=heart.agent_id,
                content=content,
                subject="observability platform",
                embedding=vec,
            ))
            await seed.flush()
            # phase_seed's would-be subject key — watermark deliberately NOT stamped.
            seed.add(FactEntityKey(
                fact_id=fact_id, entity_key="observability platform", agent_id=heart.agent_id,
            ))
            await seed.commit()

        try:
            result = await heart.learn(
                FactInput(
                    content=content,
                    subject="observability platform",
                    subject_key="observability platform",
                    attribute_key="uptime",
                    entity_keys=["observability platform", "service uptime", "1876"],
                    source="enumerative_extractor",
                ),
                precomputed_embedding=vec,
            )
            assert result.id == fact_id, "identical content+vector must hit the confirm-dupe path"

            async with heart.db.session() as check:
                dbfact = await check.get(Fact, fact_id)
                assert dbfact.entity_keys_extracted_at is not None
                rows = (await check.execute(
                    select(FactEntityKey).where(FactEntityKey.fact_id == fact_id)
                )).scalars().all()
                # "1876" is a junk numeric candidate — the stop-policy gate
                # (codex P2 round 4) must filter it before it ever reaches
                # the on_conflict_do_nothing insert.
                assert {r.entity_key for r in rows} == {"observability platform", "service uptime"}
                assert len(rows) == 2, "pre-seeded key must not be duplicated, junk key must be absent"
        finally:
            async with heart.db.session() as cleanup:
                dbfact = await cleanup.get(Fact, fact_id)
                if dbfact is not None:
                    await cleanup.delete(dbfact)
                    await cleanup.commit()

    async def test_confirm_duplicate_filters_before_capping(self, heart):
        """codex P2 round 6: _confirm_duplicate's entity-key loop previously
        sliced `input.entity_keys[:max_keys]` BEFORE normalize/stop-policy
        filtering — junk candidates in early list positions ("1876", "red")
        could crowd out valid keys past the cap position even though the
        junk keys themselves never insert a row. The loop must filter the
        FULL list first and cap only on ACCEPTED keys, mirroring _learn.
        """
        heart.facts._settings = heart.facts._settings.model_copy(
            update={"entity_keys_max_per_fact": 2}
        )
        vec = [1.0] + [0.0] * 1535  # unit vector: identical vec -> cosine 1.0 dupe match
        content = "The archival index records a single reference entry for this matter."
        fact_id = uuid.uuid4()

        async with heart.db.session() as seed:
            seed.add(Fact(
                id=fact_id,
                agent_id=heart.agent_id,
                content=content,
                subject="x archive",
                embedding=vec,
            ))
            await seed.commit()

        try:
            result = await heart.learn(
                FactInput(
                    content=content,
                    subject="x archive",
                    # 2 junk candidates FIRST, then 2 valid ones. With the
                    # old pre-slice bug (entity_keys[:2]), only "1876" and
                    # "red" would ever reach the loop — both junk, zero rows.
                    entity_keys=["1876", "red", "thomas kyd", "x archive"],
                    source="enumerative_extractor",
                ),
                precomputed_embedding=vec,
            )
            assert result.id == fact_id, "identical content+vector must hit the confirm-dupe path"

            async with heart.db.session() as check:
                rows = (await check.execute(
                    select(FactEntityKey).where(FactEntityKey.fact_id == fact_id)
                )).scalars().all()
                assert {r.entity_key for r in rows} == {"thomas kyd", "x archive"}
        finally:
            async with heart.db.session() as cleanup:
                dbfact = await cleanup.get(Fact, fact_id)
                if dbfact is not None:
                    await cleanup.delete(dbfact)
                    await cleanup.commit()
