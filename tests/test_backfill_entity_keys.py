"""R3.2 backfill: re-normalize keys in place; seed subject rows; value-side extract."""
import datetime as dt
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

    async def test_normalize_phase_pass2_resolves_collision_without_deleting_canonical(self, session, make_fact):
        """db-P3-5 guard: two old-format entity_key rows for the same fact that
        BOTH normalize to the same canonical form, one of which is ALREADY
        canonical. After phase_normalize exactly one canonical row survives
        (insert-before-delete + ON CONFLICT DO NOTHING must not delete the
        already-canonical row after its insert conflicts), and
        entity_rows_rewritten counts only the row that actually changed.

        codex P2 round 8: also seeds an independent, NON-colliding raw key
        ("author_name", no pre-existing canonical counterpart) with an
        explicit OLD created_at, to assert the rewrite INSERT carries the
        original row's created_at forward. Without this, the replacement
        row's created_at defaults to NOW() (the column's server_default),
        which would make a later `--phase rollback --watermark <ts>` run
        incorrectly delete this pre-existing row's replacement (it would
        look like a row created BY the run being rolled back).
        """
        old_ts = dt.datetime(2020, 1, 1, tzinfo=dt.timezone.utc)
        f = await make_fact()
        session.add(FactEntityKey(fact_id=f.id, entity_key="thomas_kyd", agent_id=f.agent_id))
        session.add(FactEntityKey(fact_id=f.id, entity_key="thomas kyd", agent_id=f.agent_id))
        session.add(FactEntityKey(
            fact_id=f.id, entity_key="author_name", agent_id=f.agent_id, created_at=old_ts,
        ))
        await session.flush()
        from scripts.backfill_r3_entity_keys import phase_normalize
        counts = await phase_normalize(session, agent_id=f.agent_id, dry_run=False)
        await session.flush()
        rows = (await session.execute(
            select(FactEntityKey).where(FactEntityKey.fact_id == f.id))).scalars().all()
        assert {r.entity_key for r in rows} == {"thomas kyd", "author name"}
        assert len(rows) == 2
        assert counts["entity_rows_rewritten"] == 2

        author_row = next(r for r in rows if r.entity_key == "author name")
        assert author_row.created_at == old_ts, (
            "rewrite INSERT must carry forward the original row's created_at, "
            "not default to NOW() via the column's server_default"
        )

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

    async def test_extract_phase_reports_zero_stamped_when_all_indices_malformed(self, session, make_fact, monkeypatch):
        """Stuck-round guard precondition: when every LLM item this round is
        unusable (out-of-bounds index), phase_extract must report
        facts_scanned>0 but facts_stamped==0 -- exactly the signal
        _is_stuck_round uses to stop the CLI extract loop -- and the fact
        stays unstamped (resumable), not silently marked done."""
        f = await make_fact(subject_key="key one", content="The capital of Belgium is Brussels.")
        from unittest.mock import AsyncMock
        import scripts.backfill_r3_entity_keys as bf
        monkeypatch.setattr(bf, "call_background_llm_structured", AsyncMock(return_value={
            "items": [{"index": 7, "entities": ["Belgium"]}]  # out of bounds: only 1 row (index 0)
        }))
        counts = await bf.phase_extract(session, agent_id=f.agent_id, settings=bf_settings(),
                                        llm_client=object(), llm_batch=40, max_llm_calls=1, dry_run=False)
        assert counts["facts_scanned"] == 1
        assert counts["facts_stamped"] == 0
        assert counts["warnings"] == 1
        from scripts.backfill_r3_entity_keys import _is_stuck_round
        assert _is_stuck_round(counts) is True
        await session.refresh(f)
        assert f.entity_keys_extracted_at is None

    async def test_phase_rollback_deletes_post_watermark_only(self, session, make_fact):
        """codex P2 round 8: phase_rollback must undo ONLY what a run at/after
        the watermark did -- entity rows created at/after it are deleted,
        AND entity_keys_extracted_at is reset only on facts stamped at/after
        it (otherwise extract's IS NULL predicate would never revisit them).
        Pre-watermark data must survive untouched; dry_run must write nothing.
        """
        from scripts.backfill_r3_entity_keys import phase_rollback

        pre_ts = dt.datetime(2020, 1, 1, tzinfo=dt.timezone.utc)
        watermark = dt.datetime(2025, 1, 1, tzinfo=dt.timezone.utc)
        post_ts = dt.datetime(2025, 6, 1, tzinfo=dt.timezone.utc)

        f_pre = await make_fact(entity_keys_extracted_at=pre_ts)
        f_post = await make_fact(entity_keys_extracted_at=post_ts)
        session.add(FactEntityKey(
            fact_id=f_pre.id, entity_key="pre key", agent_id=f_pre.agent_id, created_at=pre_ts,
        ))
        session.add(FactEntityKey(
            fact_id=f_post.id, entity_key="post key", agent_id=f_post.agent_id, created_at=post_ts,
        ))
        await session.flush()

        # dry_run: counts only, writes nothing.
        dry_counts = await phase_rollback(
            session, agent_id=f_pre.agent_id, watermark=watermark, dry_run=True,
        )
        assert dry_counts["entity_rows_deleted"] == 1
        assert dry_counts["facts_watermark_reset"] == 1
        rows = (await session.execute(
            select(FactEntityKey).where(FactEntityKey.agent_id == f_pre.agent_id)
        )).scalars().all()
        assert {r.entity_key for r in rows} == {"pre key", "post key"}, "dry-run must write nothing"

        # Live rollback: only the post-watermark row/stamp are touched.
        counts = await phase_rollback(
            session, agent_id=f_pre.agent_id, watermark=watermark, dry_run=False,
        )
        assert counts["entity_rows_deleted"] == 1
        assert counts["facts_watermark_reset"] == 1

        rows = (await session.execute(
            select(FactEntityKey).where(FactEntityKey.agent_id == f_pre.agent_id)
        )).scalars().all()
        assert {r.entity_key for r in rows} == {"pre key"}, "only the post-watermark row must be deleted"

        await session.refresh(f_pre)
        await session.refresh(f_post)
        assert f_pre.entity_keys_extracted_at == pre_ts, "pre-watermark stamp must survive"
        assert f_post.entity_keys_extracted_at is None, "post-watermark stamp must be reset"


class TestStuckRoundGuard:
    """Pure-function tests for the CLI extract loop's budget-burn guard --
    no DB, no monkeypatching, runs on both backends."""

    def test_zero_stamps_with_pending_facts_is_stuck(self):
        from scripts.backfill_r3_entity_keys import _is_stuck_round
        assert _is_stuck_round({"facts_scanned": 5, "facts_stamped": 0}) is True

    def test_partial_progress_is_not_stuck(self):
        from scripts.backfill_r3_entity_keys import _is_stuck_round
        assert _is_stuck_round({"facts_scanned": 5, "facts_stamped": 3}) is False

    def test_full_progress_is_not_stuck(self):
        from scripts.backfill_r3_entity_keys import _is_stuck_round
        assert _is_stuck_round({"facts_scanned": 5, "facts_stamped": 5}) is False

    def test_empty_round_is_not_stuck(self):
        # facts_scanned==0 means "no more facts pending" -- a different,
        # already-handled branch (natural completion), not a stuck round.
        from scripts.backfill_r3_entity_keys import _is_stuck_round
        assert _is_stuck_round({"facts_scanned": 0, "facts_stamped": 0}) is False
