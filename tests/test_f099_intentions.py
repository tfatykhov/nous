"""F099 Phase 1: brain.intentions — schema, the flag, and nous/brain/intentions.py."""

from __future__ import annotations

import logging
import uuid

import pytest
from sqlalchemy import text

from nous.config import Settings

INTENTION_COLUMNS = {
    "id", "agent_id", "root_id", "parent_id", "depth", "source_kind", "source_id", "intent",
    "origin_kind", "origin_session_id", "origin_channel", "origin_decision_id", "wake_policy",
    "authority", "expected_result", "assumptions", "deadline", "state", "close_reason",
    "root_cancelled_at", "root_expired_at", "claimed_at", "claim_token", "attempts",
    "created_at", "result_at", "closed_at", "updated_at",
}  # fmt: skip


def _agent() -> str:
    return f"f099-int-{uuid.uuid4().hex[:8]}"


def test_the_flag_defaults_off():
    assert Settings(_env_file=None).intentions_enabled is False


def test_the_flag_needs_the_result_inbox(caplog):
    with caplog.at_level(logging.WARNING, logger="nous.config"):
        s = Settings(_env_file=None, intentions_enabled=True, result_inbox_enabled=False)
    assert s.intentions_enabled is False
    assert "NOUS_RESULT_INBOX_ENABLED" in caplog.text


def test_the_flag_stays_on_with_the_inbox():
    assert Settings(_env_file=None, intentions_enabled=True, result_inbox_enabled=True).intentions_enabled is True


@pytest.mark.postgres_only
async def test_the_migration_creates_the_table_and_the_inbox_column(db):
    async with db.engine.connect() as conn:
        cols = {
            r[0]
            for r in (
                await conn.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema = 'brain' AND table_name = 'intentions'"
                    )
                )
            ).all()
        }
        inbox = (
            await conn.execute(
                text(
                    "SELECT 1 FROM information_schema.columns WHERE table_schema = 'heart' "
                    "AND table_name = 'result_inbox' AND column_name = 'intention_id'"
                )
            )
        ).first()
    assert cols == INTENTION_COLUMNS
    assert inbox is not None
