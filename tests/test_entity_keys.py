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
                    entity_keys=["observability platform", "service uptime"],
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
                assert {r.entity_key for r in rows} == {"observability platform", "service uptime"}
                assert len(rows) == 2, "pre-seeded key must not be duplicated"
        finally:
            async with heart.db.session() as cleanup:
                dbfact = await cleanup.get(Fact, fact_id)
                if dbfact is not None:
                    await cleanup.delete(dbfact)
                    await cleanup.commit()
