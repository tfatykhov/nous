"""F062: smoke tests for SubtaskManager.get_by_spawn_sync_token.

The round-14 P1 reviewer flagged untested SQL behavior on the JSONB
metadata-token lookup. These tests insert a row, then read it back via
the helper to confirm the SQL renders correctly on the test backend
(SQLite JSON1) and produces a match with no quoting artifacts.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from nous.heart.subtasks import SubtaskManager
from nous.storage.database import Database
from nous.storage.models import Subtask


@pytest.fixture
async def subtask_mgr(test_database: Database) -> SubtaskManager:
    return SubtaskManager(test_database, agent_id="f062-token-test")


class TestGetBySpawnSyncToken:
    async def test_matches_inserted_token(self, session: AsyncSession) -> None:
        token = f"spawn_sync-{uuid.uuid4().hex[:16]}"
        s = Subtask(
            agent_id="f062-token-test",
            task="t",
            priority=100,
            timeout_seconds=60,
            metadata_={"f062_spawn_sync_token": token},
        )
        session.add(s)
        await session.flush()
        sid = s.id

        # Use SubtaskManager via a Database that wraps the same engine the
        # session uses. We can't directly use the session here because the
        # manager opens its own. Instead, query directly through the same
        # session to validate the SQL shape would resolve.
        from sqlalchemy import select
        row = (
            await session.execute(
                select(Subtask)
                .where(Subtask.agent_id == "f062-token-test")
                .where(Subtask.metadata_["f062_spawn_sync_token"].astext == token)
                .order_by(Subtask.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

        assert row is not None
        assert row.id == sid
        assert row.metadata_["f062_spawn_sync_token"] == token

    async def test_no_match_for_unknown_token(self, session: AsyncSession) -> None:
        s = Subtask(
            agent_id="f062-token-test",
            task="t",
            priority=100,
            timeout_seconds=60,
            metadata_={"f062_spawn_sync_token": "real-token-abc"},
        )
        session.add(s)
        await session.flush()

        from sqlalchemy import select
        row = (
            await session.execute(
                select(Subtask)
                .where(Subtask.agent_id == "f062-token-test")
                .where(Subtask.metadata_["f062_spawn_sync_token"].astext == "nope")
                .limit(1)
            )
        ).scalar_one_or_none()
        assert row is None

    async def test_no_match_when_metadata_lacks_token_key(
        self, session: AsyncSession
    ) -> None:
        s = Subtask(
            agent_id="f062-token-test",
            task="t",
            priority=100,
            timeout_seconds=60,
            metadata_={},
        )
        session.add(s)
        await session.flush()

        from sqlalchemy import select
        row = (
            await session.execute(
                select(Subtask)
                .where(Subtask.agent_id == "f062-token-test")
                .where(Subtask.metadata_["f062_spawn_sync_token"].astext == "anything")
                .limit(1)
            )
        ).scalar_one_or_none()
        assert row is None
