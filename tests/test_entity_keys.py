"""R3 (F085): canonical key normalization + entity-candidate extraction."""
import unicodedata
import uuid
from datetime import UTC, datetime, timedelta

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
        # codex P2 round 12: possessive 's collapses to the base entity
        # ("tim's laptop" -> "tim laptop"), not "tims laptop" -- otherwise a
        # query mentioning "Tim's ..." can never exact-match a stored "tim".
        assert normalize_key("Tim's Laptop") == "tim laptop"
        assert normalize_key("  RED   Car!! ") == "red car"

    def test_possessive_suffixes(self):
        """codex P2 round 12: singular 's is dropped entirely; a plural
        possessive (already ending in s) only loses its apostrophe."""
        assert normalize_key("Tim's trip") == "tim trip"
        assert normalize_key("students' union") == "students union"
        assert normalize_key("boss's office") == "boss office"
        assert normalize_key("James’s book") == "james book"  # curly apostrophe

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
            "students' union", "boss's office", "James’s book",  # codex round 12
        ]
        for raw in cases:
            once = normalize_key(raw)
            assert normalize_key(once) == once, raw


class TestFactInputKeyNormalization:
    """codex P2 round 7: subject_key/attribute_key canonicalized at the
    FactInput boundary (pydantic field validators), so every producer —
    extractor, backfill, or a direct Heart.learn caller — ends up storing
    the SAME normalized form. Without this, a direct caller's raw
    "api_gateway" would never exact-match the extractor's normalized
    "api gateway", silently missing R2 same-key supersession.
    """
    def test_subject_key_and_attribute_key_normalized_on_construction(self):
        fi = FactInput(
            content="The api gateway owner is the platform team.",
            subject_key="api_gateway",
            attribute_key="The_Owner",
        )
        assert fi.subject_key == "api gateway"
        assert fi.attribute_key == "owner"

    def test_none_keys_stay_none(self):
        fi = FactInput(content="A fact with no conflict-slot keys at all.")
        assert fi.subject_key is None
        assert fi.attribute_key is None

    def test_already_normalized_keys_are_unchanged(self):
        # Fixpoint property: normalize_key(normalize_key(x)) == normalize_key(x).
        fi = FactInput(
            content="The extractor already normalized these keys upstream.",
            subject_key="marriage of figaro",
            attribute_key="author",
        )
        assert fi.subject_key == "marriage of figaro"
        assert fi.attribute_key == "author"


class TestEntityExtractionCompleteDefault:
    """codex P2 round 9: entity_extraction_complete's default flipped from
    True to False. A bare FactInput — the shape a non-entity-aware legacy
    producer (fact_extractor, the learn_fact tool, the REST endpoint) would
    construct, with no entity awareness at all — must not implicitly claim
    entity extraction completed. Only an entity-aware producer that
    explicitly sets this True (the enumerative extractor, via
    _to_fact_inputs) gets its facts stamped.
    """
    def test_defaults_false(self):
        fi = FactInput(content="A legacy fact with no entity awareness at all.")
        assert fi.entity_extraction_complete is False

    def test_defaults_false_even_with_entity_keys_set(self):
        # A direct caller can set entity_keys without being "entity-aware"
        # in the full producer sense (e.g. hand-constructing a FactInput in
        # a script or test) — the watermark stamp requires the EXPLICIT
        # opt-in, not just a non-empty entity_keys list.
        fi = FactInput(
            content="A fact with entity_keys but no explicit opt-in.",
            entity_keys=["some key"],
        )
        assert fi.entity_extraction_complete is False


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
        # codex P2 round 9: entity_extraction_complete now defaults False
        # (a direct FactInput() constructor is not, by itself, evidence an
        # entity-aware producer ran) -- pass True explicitly since this test
        # simulates the enumerative extractor's entity-aware write and
        # asserts the watermark IS stamped.
        fi = FactInput(
            content="The author of The Marriage of Figaro is Thomas Kyd.",
            subject="marriage of figaro",
            subject_key="marriage of figaro",
            attribute_key="author",
            entity_keys=["marriage of figaro", "thomas kyd"],
            entity_extraction_complete=True,
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

    async def test_entities_present_subject_fails_stop_policy_zero_rows_still_stamped(
        self, heart, session, settings,
    ):
        """codex P2 round 9: entity-aware extraction where EVERY candidate,
        including the subject, fails the stop-policy ends with
        entity_keys=[] entirely (not just a subset filtered). Under round
        6's stamp-only-if-entity_keys-non-empty rule this fact would NEVER
        be stamped, so every backfill run would re-send it to the LLM
        forever even though extraction genuinely completed with zero
        accepted keys. entity_extraction_complete=True (from the "entities"
        key being present) must still stamp the watermark.
        """
        from nous.handlers.enumerative_extractor import EnumerativeExtractor

        ex = EnumerativeExtractor(heart=heart, settings=settings, llm_client=None, embedder=None)
        raw = [{
            "content": "The car in the driveway is red and freshly parked.",
            "subject_key": "red",  # scalar -> fails is_keyable_entity
            "attribute_key": "color",
            "entities": [],  # entity-aware, reported zero object-side entities
        }]
        (fi,) = ex._to_fact_inputs(raw, chunk_index=0, episode_id=None)
        assert fi.entity_keys == []
        assert fi.entity_extraction_complete is True

        result = await heart.learn(fi)
        try:
            rows = (await session.execute(
                select(FactEntityKey).where(FactEntityKey.fact_id == result.id)
            )).scalars().all()
            assert rows == []
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

        codex P2 round 9: entity_extraction_complete=True passed explicitly
        (default flipped to False) since this test asserts the watermark IS
        stamped despite 3 of the 4 candidates being filtered out.
        """
        fi = FactInput(
            content="The author of The Marriage of Figaro is Thomas Kyd.",
            subject="marriage of figaro",
            entity_keys=["red", "1876", "ab", "thomas kyd"],
            entity_extraction_complete=True,
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

        codex P2 round 9: entity_extraction_complete=True passed explicitly
        (default flipped to False) since this test asserts the watermark IS
        stamped on the dupe.
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
                    entity_extraction_complete=True,
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


@pytest.mark.postgres_only
class TestInheritConflictSlotKeys:
    async def test_replacement_inherits_subject_key_and_entity_union(self, heart):
        """codex P2 round 9: sleep_handler's F031/F027 merge sites create a
        replacement fact from a bare FactInput (subject/content/source/
        confidence/category only) — subject_key, attribute_key, and
        entity_keys rows never carried over from the merged sources. This
        drives FactManager.inherit_conflict_slot_keys the way both merge
        sites do: AFTER the replacement row exists, pass it the source ids
        being merged away.

        Asserts: subject_key/attribute_key copied from the NEWEST source
        that has both set; entity_keys rows are the copied subject_key PLUS
        the DISTINCT UNION of every source's rows (codex P2 round 10: the
        subject_key is always reserved its own row, even when it wasn't
        itself among the sources' own fact_entity_keys — that's what makes
        the replacement findable via the keyed leg on its own subject);
        entity_keys_extracted_at stays NULL on the replacement (merged
        content is new text — the backfill should re-derive it); the source
        facts' own rows/active state are untouched (this helper never
        deactivates anything itself — that's the caller's job, same as
        apply_supersession).
        """
        agent_id = heart.agent_id
        older_id = uuid.uuid4()
        newer_id = uuid.uuid4()
        replacement_id = uuid.uuid4()

        async with heart.db.session() as s:
            s.add(Fact(
                id=older_id, agent_id=agent_id,
                content="An older source fact about a shared topic.",
                subject_key="topic", attribute_key="older attr",
                learned_at=datetime.now(UTC) - timedelta(hours=2),
                active=True,
            ))
            s.add(Fact(
                id=newer_id, agent_id=agent_id,
                content="A newer source fact about the same shared topic.",
                subject_key="topic", attribute_key="newer attr",
                learned_at=datetime.now(UTC) - timedelta(hours=1),
                active=True,
            ))
            s.add(Fact(
                id=replacement_id, agent_id=agent_id,
                content="The LLM-merged replacement content.",
                active=True,
            ))
            await s.flush()
            s.add(FactEntityKey(fact_id=older_id, entity_key="shared key", agent_id=agent_id))
            s.add(FactEntityKey(fact_id=older_id, entity_key="older only key", agent_id=agent_id))
            s.add(FactEntityKey(fact_id=newer_id, entity_key="shared key", agent_id=agent_id))
            s.add(FactEntityKey(fact_id=newer_id, entity_key="newer only key", agent_id=agent_id))
            await s.commit()

        try:
            async with heart.db.session() as s:
                await heart.facts.inherit_conflict_slot_keys(
                    replacement_id, [older_id, newer_id], s,
                )
                await s.commit()

            async with heart.db.session() as check:
                replacement = await check.get(Fact, replacement_id)
                # newest complete source (newer_id) wins the subject/attribute pair.
                assert replacement.subject_key == "topic"
                assert replacement.attribute_key == "newer attr"
                assert replacement.entity_keys_extracted_at is None

                rows = (await check.execute(
                    select(FactEntityKey).where(FactEntityKey.fact_id == replacement_id)
                )).scalars().all()
                assert {r.entity_key for r in rows} == {
                    "topic", "shared key", "older only key", "newer only key",
                }

                # Sources are untouched: still active, own rows intact.
                older = await check.get(Fact, older_id)
                newer = await check.get(Fact, newer_id)
                assert older.active is True and newer.active is True
                older_rows = (await check.execute(
                    select(FactEntityKey).where(FactEntityKey.fact_id == older_id)
                )).scalars().all()
                assert {r.entity_key for r in older_rows} == {"shared key", "older only key"}
        finally:
            async with heart.db.session() as cleanup:
                for fid in (older_id, newer_id, replacement_id):
                    f = await cleanup.get(Fact, fid)
                    if f is not None:
                        await cleanup.delete(f)
                await cleanup.commit()

    async def test_subject_key_survives_cap(self, heart):
        """codex P2 round 10: an unordered ``distinct().limit(max_keys)``
        can return any subset when a source has more than max_keys distinct
        entity keys, silently dropping the subject key just copied onto the
        replacement's own subject_key column. With entity_keys_max_per_fact
        set below the source's actual key count (max_keys+2 here), the
        replacement must still end up with exactly max_keys rows AND the
        subject key must be among them. Rerun-stable: calling the helper
        twice must not change the outcome (ON CONFLICT DO NOTHING).
        """
        heart.facts._settings = heart.facts._settings.model_copy(
            update={"entity_keys_max_per_fact": 3}
        )
        agent_id = heart.agent_id
        source_id = uuid.uuid4()
        replacement_id = uuid.uuid4()

        async with heart.db.session() as s:
            s.add(Fact(
                id=source_id, agent_id=agent_id,
                content="A source fact with many distinct entity keys.",
                subject_key="topic", attribute_key="attr",
                learned_at=datetime.now(UTC),
                active=True,
            ))
            s.add(Fact(
                id=replacement_id, agent_id=agent_id,
                content="The merged replacement content.",
                active=True,
            ))
            await s.flush()
            # "topic" (the subject key) is ALSO indexed as one of the
            # source's own entity-key rows (matching the real write path);
            # plus 4 more distinct keys -- 5 total, max_keys(3)+2.
            for key in ["topic", "alpha key", "beta key", "gamma key", "delta key"]:
                s.add(FactEntityKey(fact_id=source_id, entity_key=key, agent_id=agent_id))
            await s.commit()

        try:
            for _ in range(2):  # rerun-stable
                async with heart.db.session() as s:
                    await heart.facts.inherit_conflict_slot_keys(
                        replacement_id, [source_id], s,
                    )
                    await s.commit()

                async with heart.db.session() as check:
                    rows = (await check.execute(
                        select(FactEntityKey).where(FactEntityKey.fact_id == replacement_id)
                    )).scalars().all()
                    assert len(rows) == 3
                    assert "topic" in {r.entity_key for r in rows}
        finally:
            async with heart.db.session() as cleanup:
                for fid in (source_id, replacement_id):
                    f = await cleanup.get(Fact, fid)
                    if f is not None:
                        await cleanup.delete(f)
                await cleanup.commit()

    async def test_budget_only_spent_on_real_inserts(self, heart):
        """codex P2 round 15: round 13's cap fix counted only HOW MANY rows
        the replacement already owned, not WHICH ones -- reserving a slot
        for the subject key even when it's ALREADY among the replacement's
        existing rows burned budget on an ON CONFLICT DO NOTHING no-op (no
        row actually added), which could crowd out a genuinely new source
        key under a tight cap.

        entity_keys_max_per_fact=2; the replacement PRE-OWNS its subject
        row ("topic") already, leaving exactly 1 slot remaining. The
        source has "topic" (subject key) plus one distinct object key
        ("poetry"). Without this fix, the subject-key reservation would
        still spend the 1 remaining slot on "topic" (a no-op, since it's
        already present), leaving 0 for the union fill -- "poetry" would
        never be copied. With the fix, the already-present subject key
        costs nothing, so "poetry" claims the 1 real remaining slot.
        """
        heart.facts._settings = heart.facts._settings.model_copy(
            update={"entity_keys_max_per_fact": 2}
        )
        agent_id = heart.agent_id
        source_id = uuid.uuid4()
        replacement_id = uuid.uuid4()

        async with heart.db.session() as s:
            s.add(Fact(
                id=source_id, agent_id=agent_id,
                content="A source fact for the round-15 real-inserts-only budget test.",
                subject_key="topic", attribute_key="attr",
                learned_at=datetime.now(UTC),
                active=True,
            ))
            s.add(Fact(
                id=replacement_id, agent_id=agent_id,
                content="The replacement content, pre-owning its subject row.",
                active=True,
            ))
            await s.flush()
            for key in ["topic", "poetry"]:
                s.add(FactEntityKey(fact_id=source_id, entity_key=key, agent_id=agent_id))
            # Replacement PRE-OWNS the subject key's row already (1 of the
            # 2-slot cap), leaving exactly 1 slot remaining.
            s.add(FactEntityKey(fact_id=replacement_id, entity_key="topic", agent_id=agent_id))
            await s.commit()

        try:
            async with heart.db.session() as s:
                await heart.facts.inherit_conflict_slot_keys(
                    replacement_id, [source_id], s,
                )
                await s.commit()

            async with heart.db.session() as check:
                rows = (await check.execute(
                    select(FactEntityKey).where(FactEntityKey.fact_id == replacement_id)
                )).scalars().all()
                keys = {r.entity_key for r in rows}
                assert len(rows) == 2, "total rows must not exceed the configured cap"
                assert "poetry" in keys, "the real remaining slot must go to a genuinely new key"
                assert "topic" in keys
        finally:
            async with heart.db.session() as cleanup:
                for fid in (source_id, replacement_id):
                    f = await cleanup.get(Fact, fid)
                    if f is not None:
                        await cleanup.delete(f)
                await cleanup.commit()

    async def test_cap_counts_existing_rows_on_replacement(self, heart):
        """codex P2 round 13: the entity-key cap used to start a FRESH
        max_keys budget every call, ignoring rows the replacement already
        owns (a keyed FactInput's own entity_keys through supersede, or a
        merge-learn call that confirmed an existing fact) -- total rows
        could reach 2x the configured cap. With entity_keys_max_per_fact=3
        and the replacement PRE-SEEDED with 2 (max_keys-1) existing rows,
        the source's 3 distinct keys must fill only the ONE remaining slot
        -- the subject key claims it first (displacing potential "filler"
        union keys, not the other way around), landing the replacement at
        EXACTLY max_keys (3) total rows with the subject key among them.
        """
        heart.facts._settings = heart.facts._settings.model_copy(
            update={"entity_keys_max_per_fact": 3}
        )
        agent_id = heart.agent_id
        source_id = uuid.uuid4()
        replacement_id = uuid.uuid4()

        async with heart.db.session() as s:
            s.add(Fact(
                id=source_id, agent_id=agent_id,
                content="A source fact for the round-13 existing-rows cap test.",
                subject_key="topic", attribute_key="attr",
                learned_at=datetime.now(UTC),
                active=True,
            ))
            s.add(Fact(
                id=replacement_id, agent_id=agent_id,
                content="The replacement content, pre-seeded with existing rows.",
                active=True,
            ))
            await s.flush()
            # Source has 3 distinct keys (subject key included, matching the
            # real write path).
            for key in ["topic", "alpha key", "beta key"]:
                s.add(FactEntityKey(fact_id=source_id, entity_key=key, agent_id=agent_id))
            # Replacement is PRE-SEEDED with max_keys-1 = 2 existing rows —
            # simulating rows the replacement already owns before this call
            # (e.g. from its own FactInput.entity_keys through supersede).
            for key in ["existing filler one", "existing filler two"]:
                s.add(FactEntityKey(fact_id=replacement_id, entity_key=key, agent_id=agent_id))
            await s.commit()

        try:
            async with heart.db.session() as s:
                await heart.facts.inherit_conflict_slot_keys(
                    replacement_id, [source_id], s,
                )
                await s.commit()

            async with heart.db.session() as check:
                rows = (await check.execute(
                    select(FactEntityKey).where(FactEntityKey.fact_id == replacement_id)
                )).scalars().all()
                assert len(rows) == 3, "total rows must not exceed the configured cap"
                assert "topic" in {r.entity_key for r in rows}
        finally:
            async with heart.db.session() as cleanup:
                for fid in (source_id, replacement_id):
                    f = await cleanup.get(Fact, fid)
                    if f is not None:
                        await cleanup.delete(f)
                await cleanup.commit()


@pytest.mark.postgres_only
class TestSupersedeInheritsConflictSlotKeys:
    """codex P2 round 11: sleep_handler._handle_updates_prefix calls
    Heart.supersede_fact with a bare FactInput (subject/content/source/
    confidence/category only, mirrored here) — the same key-loss class
    fixed at the F031/F027 merge sites in round 9, but at the generic
    supersede path (FactManager._supersede). Without wiring
    inherit_conflict_slot_keys into _supersede, the replacement would be
    stored keyless while the keyed source gets deactivated, permanently
    dropping the conflict slot out of exact-key recall.
    """

    async def test_bare_replacement_inherits_subject_key_and_entities(self, heart):
        agent_id = heart.agent_id
        source_id = uuid.uuid4()

        async with heart.db.session() as s:
            s.add(Fact(
                id=source_id, agent_id=agent_id,
                content="Nous prefers uv over pip for dependency management.",
                subject_key="package manager", attribute_key="preference",
                learned_at=datetime.now(UTC),
                active=True,
            ))
            await s.flush()
            s.add(FactEntityKey(fact_id=source_id, entity_key="package manager", agent_id=agent_id))
            s.add(FactEntityKey(fact_id=source_id, entity_key="poetry", agent_id=agent_id))
            await s.commit()

        detail = None
        try:
            # Same bare-FactInput shape sleep_handler._handle_updates_prefix
            # passes to supersede_fact — no subject_key/entity_keys at all.
            detail = await heart.supersede_fact(
                source_id,
                FactInput(
                    subject="package manager",
                    content="Nous now strongly prefers uv over both pip and poetry.",
                    source="sleep_reflection",
                    confidence=0.8,
                    category="concept",
                ),
            )

            async with heart.db.session() as check:
                replacement = await check.get(Fact, detail.id)
                assert replacement.subject_key == "package manager"
                assert replacement.attribute_key == "preference"
                assert replacement.entity_keys_extracted_at is None

                rows = (await check.execute(
                    select(FactEntityKey).where(FactEntityKey.fact_id == detail.id)
                )).scalars().all()
                assert {r.entity_key for r in rows} == {"package manager", "poetry"}

                source = await check.get(Fact, source_id)
                assert source.active is False
                assert source.superseded_by == detail.id
                source_rows = (await check.execute(
                    select(FactEntityKey).where(FactEntityKey.fact_id == source_id)
                )).scalars().all()
                assert {r.entity_key for r in source_rows} == {"package manager", "poetry"}
        finally:
            async with heart.db.session() as cleanup:
                ids = [source_id] + ([detail.id] if detail is not None else [])
                for fid in ids:
                    f = await cleanup.get(Fact, fid)
                    if f is not None:
                        await cleanup.delete(f)
                await cleanup.commit()

    async def test_already_keyed_replacement_is_not_overwritten(self, heart):
        """A supersede caller CAN pass a FactInput with its own subject_key
        (unlike the merge sites, which never do) — that keyed replacement
        must win over the inherited source key, not be clobbered by it.
        The entity-row union stays additive regardless.
        """
        agent_id = heart.agent_id
        source_id = uuid.uuid4()

        async with heart.db.session() as s:
            s.add(Fact(
                id=source_id, agent_id=agent_id,
                content="Nous prefers uv over pip for dependency management, take two.",
                subject_key="package manager", attribute_key="preference",
                learned_at=datetime.now(UTC),
                active=True,
            ))
            await s.flush()
            s.add(FactEntityKey(fact_id=source_id, entity_key="package manager", agent_id=agent_id))
            s.add(FactEntityKey(fact_id=source_id, entity_key="poetry", agent_id=agent_id))
            await s.commit()

        detail = None
        try:
            detail = await heart.supersede_fact(
                source_id,
                FactInput(
                    subject="dependency tooling",
                    subject_key="its own distinct key",
                    content="A keyed replacement fact for the already-keyed supersede test.",
                    source="sleep_reflection",
                    confidence=0.8,
                    category="concept",
                ),
            )

            async with heart.db.session() as check:
                replacement = await check.get(Fact, detail.id)
                # The caller's own key wins -- not overwritten by the
                # source's "package manager"/"preference" pair.
                assert replacement.subject_key == "its own distinct key"
                assert replacement.attribute_key is None

                # Entity-row union from the source is still additive+capped
                # even though the subject/attribute pair copy was skipped.
                rows = (await check.execute(
                    select(FactEntityKey).where(FactEntityKey.fact_id == detail.id)
                )).scalars().all()
                assert {r.entity_key for r in rows} == {"package manager", "poetry"}
        finally:
            async with heart.db.session() as cleanup:
                ids = [source_id] + ([detail.id] if detail is not None else [])
                for fid in ids:
                    f = await cleanup.get(Fact, fid)
                    if f is not None:
                        await cleanup.delete(f)
                await cleanup.commit()

    async def test_matching_subject_but_different_attribute_key_not_overwritten(self, heart):
        """codex P2 round 12: round 11's guard only compared subject_key —
        a replacement with the SAME subject_key as the source but a
        DIFFERENT, caller-supplied attribute_key still got that
        attribute_key silently clobbered by the source's, since the pair
        was copied whenever subject_key alone matched. Either slot
        differing from what the source would produce must now block the
        WHOLE pair-copy — never mix a caller-owned attribute_key with an
        inherited subject_key (or vice versa). The entity-row union stays
        additive regardless.
        """
        agent_id = heart.agent_id
        source_id = uuid.uuid4()

        async with heart.db.session() as s:
            s.add(Fact(
                id=source_id, agent_id=agent_id,
                content="Nous prefers uv over pip for dependency management, take three.",
                subject_key="package manager", attribute_key="preference",
                learned_at=datetime.now(UTC),
                active=True,
            ))
            await s.flush()
            s.add(FactEntityKey(fact_id=source_id, entity_key="package manager", agent_id=agent_id))
            s.add(FactEntityKey(fact_id=source_id, entity_key="poetry", agent_id=agent_id))
            await s.commit()

        detail = None
        try:
            detail = await heart.supersede_fact(
                source_id,
                FactInput(
                    subject="dependency tooling",
                    subject_key="package manager",  # matches the source
                    attribute_key="explicit different attribute",  # does NOT
                    content="A jointly-keyed replacement fact for the round-12 slot-guard test.",
                    source="sleep_reflection",
                    confidence=0.8,
                    category="concept",
                ),
            )

            async with heart.db.session() as check:
                replacement = await check.get(Fact, detail.id)
                # Neither slot is overwritten -- the mismatched attribute_key
                # blocks the WHOLE pair-copy, not just its own slot.
                assert replacement.subject_key == "package manager"
                assert replacement.attribute_key == "explicit different attribute"

                # Entity-row union from the source is still additive.
                rows = (await check.execute(
                    select(FactEntityKey).where(FactEntityKey.fact_id == detail.id)
                )).scalars().all()
                assert {r.entity_key for r in rows} == {"package manager", "poetry"}
        finally:
            async with heart.db.session() as cleanup:
                ids = [source_id] + ([detail.id] if detail is not None else [])
                for fid in ids:
                    f = await cleanup.get(Fact, fid)
                    if f is not None:
                        await cleanup.delete(f)
                await cleanup.commit()

    async def test_inherited_key_visible_immediately_no_ttl_wait(self, heart):
        """codex P2 round 13: inherit_conflict_slot_keys's own in-txn cache
        invalidation only protects a reader racing the STILL-OPEN
        transaction -- once supersede()'s OWN commit lands, nothing
        re-invalidates the cache for that commit unless supersede() does it
        itself. Mirrors test_vocab_cache_invalidation_survives_mid_transaction_race's
        exact technique (test_keyed_fact_leg.py) but for the supersede path:
        hooking _emit_event (called near the end of _supersede, after
        inherit_conflict_slot_keys has written the new row but BEFORE
        supersede()'s outer commit) forces a vocab rebuild from inside the
        still-open transaction. Under READ COMMITTED that rebuild can't see
        the uncommitted row, so it caches a STALE vocab -- exactly the
        window supersede()'s post-commit invalidation must close. (A plain
        sequential warm-before/check-after, with no forced race, would NOT
        catch this: the in-txn bump already nulls the cache, so a check run
        strictly after the real commit rebuilds correctly either way.)

        The source's subject_key ("round13 vocab visibility key") is set
        as a Fact COLUMN with NO pre-existing FactEntityKey row anywhere --
        the row inherit_conflict_slot_keys reserves onto the replacement is
        the ONLY place this key can ever appear in the vocab.
        """
        agent_id = heart.agent_id
        source_id = uuid.uuid4()

        async with heart.db.session() as s:
            s.add(Fact(
                id=source_id, agent_id=agent_id,
                content="A source fact for the round-13 post-commit vocab visibility test.",
                subject_key="round13 vocab visibility key", attribute_key="preference",
                learned_at=datetime.now(UTC),
                active=True,
            ))
            await s.commit()

        orig_emit_event = heart.facts._emit_event

        async def racing_emit_event(session, event_type, data):
            if event_type == "fact_superseded":
                await heart.facts.entity_key_vocabulary()
            return await orig_emit_event(session, event_type, data)

        heart.facts._emit_event = racing_emit_event

        detail = None
        try:
            detail = await heart.supersede_fact(
                source_id,
                FactInput(
                    subject="round13 vocab visibility key",
                    content="A replacement fact for the round-13 post-commit vocab visibility test.",
                    source="sleep_reflection",
                    confidence=0.8,
                    category="concept",
                ),
            )
            # Without supersede()'s post-commit re-invalidation, this would
            # return the vocab the racing rebuild poisoned mid-transaction
            # (missing the inherited key) for the rest of the 300s TTL.
            vocab = await heart.facts.entity_key_vocabulary()
            assert "round13 vocab visibility key" in vocab
        finally:
            heart.facts._emit_event = orig_emit_event
            async with heart.db.session() as cleanup:
                ids = [source_id] + ([detail.id] if detail is not None else [])
                for fid in ids:
                    f = await cleanup.get(Fact, fid)
                    if f is not None:
                        await cleanup.delete(f)
                await cleanup.commit()
