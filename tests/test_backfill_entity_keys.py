"""R3.2 backfill: re-normalize keys in place; seed subject rows; value-side extract."""
import uuid
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import func, select

from nous.storage.models import Fact, FactEntityKey


def bf_settings():
    """Tiny settings stand-in for phase_extract (mirrors real Settings defaults)."""
    return SimpleNamespace(
        background_model="claude-haiku-4-5-20251001",
        entity_keys_max_per_fact=8,
        entity_key_min_chars=3,
    )


@pytest_asyncio.fixture
async def make_fact(session):
    """Function-scoped factory for a bare heart.facts row (copied from test_entity_keys.py)."""
    async def _make_fact(**overrides):
        defaults = {
            "id": uuid.uuid4(),
            "agent_id": "test-backfill-entity-keys-agent",
            "content": "default fact content for backfill entity key tests",
            "active": True,
        }
        defaults.update(overrides)
        fact = Fact(**defaults)
        session.add(fact)
        await session.flush()
        return fact

    return _make_fact


@pytest.mark.postgres_only
class TestBackfillPhases:
    async def test_normalize_phase_rewrites_old_format_keys(self, session, make_fact):
        f = await make_fact(subject_key="thomas_kyd", attribute_key="the_author")
        from scripts.backfill_r3_entity_keys import phase_normalize
        counts = await phase_normalize(session, agent_id=f.agent_id, dry_run=False)
        await session.flush()
        await session.refresh(f)
        assert f.subject_key == "thomas kyd"
        assert f.attribute_key == "author"
        assert counts["facts_updated"] == 1
        # idempotent: second run is a no-op
        counts2 = await phase_normalize(session, agent_id=f.agent_id, dry_run=False)
        assert counts2["facts_updated"] == 0

    async def test_normalize_phase_dry_run_writes_nothing(self, session, make_fact):
        f = await make_fact(subject_key="thomas_kyd")
        from scripts.backfill_r3_entity_keys import phase_normalize
        counts = await phase_normalize(session, agent_id=f.agent_id, dry_run=True)
        await session.refresh(f)
        assert f.subject_key == "thomas_kyd"
        assert counts["facts_updated"] == 1  # counted, not written

    async def test_seed_phase_inserts_subject_rows_idempotently(self, session, make_fact):
        f = await make_fact(subject_key="thomas kyd")
        from scripts.backfill_r3_entity_keys import phase_seed
        await phase_seed(session, agent_id=f.agent_id, dry_run=False)
        await phase_seed(session, agent_id=f.agent_id, dry_run=False)  # ON CONFLICT DO NOTHING
        n = (await session.execute(
            select(func.count()).select_from(FactEntityKey).where(FactEntityKey.fact_id == f.id)
        )).scalar_one()
        assert n == 1

    async def test_extract_phase_resumes_via_watermark(self, session, make_fact, monkeypatch):
        f1 = await make_fact(subject_key="key one", content="The capital of Belgium is Brussels.")
        f2 = await make_fact(subject_key="key two", content="The author of X is Thomas Kyd.")
        from unittest.mock import AsyncMock
        import scripts.backfill_r3_entity_keys as bf
        monkeypatch.setattr(bf, "call_background_llm_structured", AsyncMock(return_value={
            "items": [
                {"index": 0, "entities": ["Belgium", "Brussels"]},
                {"index": 1, "entities": ["Thomas Kyd", "X"]},
            ]
        }))
        counts = await bf.phase_extract(session, agent_id=f1.agent_id, settings=bf_settings(),
                                        llm_client=object(), llm_batch=40, max_llm_calls=0, dry_run=False)
        await session.flush()
        for f, expected in ((f1, {"key one", "belgium", "brussels"}), (f2, {"key two", "thomas kyd"})):
            rows = {r.entity_key for r in (await session.execute(
                select(FactEntityKey).where(FactEntityKey.fact_id == f.id))).scalars().all()}
            assert expected <= rows
            await session.refresh(f)
            assert f.entity_keys_extracted_at is not None
        # resume: second call finds no NULL-watermark facts, zero LLM calls
        mock2 = AsyncMock()
        monkeypatch.setattr(bf, "call_background_llm_structured", mock2)
        await bf.phase_extract(session, agent_id=f1.agent_id, settings=bf_settings(),
                               llm_client=object(), llm_batch=40, max_llm_calls=0, dry_run=False)
        mock2.assert_not_awaited()

    async def test_extract_phase_applies_stop_policy_to_subject_key(self, session, make_fact, monkeypatch):
        """Amendment 3 (review devil-P2-1): no stop-policy exemption for subject
        keys -- a scalar/short subject gets no entity row, but the fact is still
        stamped extracted (it appeared in an LLM item)."""
        f = await make_fact(subject_key="ab", content="The widget color is ab.")
        from unittest.mock import AsyncMock
        import scripts.backfill_r3_entity_keys as bf
        monkeypatch.setattr(bf, "call_background_llm_structured", AsyncMock(return_value={
            "items": [{"index": 0, "entities": []}]
        }))
        await bf.phase_extract(session, agent_id=f.agent_id, settings=bf_settings(),
                               llm_client=object(), llm_batch=40, max_llm_calls=0, dry_run=False)
        await session.flush()
        rows = (await session.execute(
            select(FactEntityKey).where(FactEntityKey.fact_id == f.id))).scalars().all()
        assert rows == []
        await session.refresh(f)
        assert f.entity_keys_extracted_at is not None
