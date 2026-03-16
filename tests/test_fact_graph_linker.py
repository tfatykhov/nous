"""Tests for F022 Phase 2 gap fix: fact-to-decision auto-linking."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from nous.events import Event, EventBus
from nous.heart.schemas import FactDetail, FactInput


def _fake_fact_detail(**overrides):
    """Create a FactDetail with all required fields populated."""
    defaults = dict(
        id=uuid4(),
        agent_id="test-agent",
        content="PostgreSQL uses MVCC",
        category="technical",
        subject="PostgreSQL",
        confidence=0.9,
        source="test",
        source_episode_id=None,
        source_decision_id=None,
        learned_at=datetime.now(UTC),
        last_confirmed=None,
        confirmation_count=0,
        superseded_by=None,
        contradiction_of=None,
        active=True,
        tags=[],
        created_at=datetime.now(UTC),
    )
    defaults.update(overrides)
    return FactDetail(**defaults)


class TestHeartBusEmission:
    """Verify Heart.learn() emits fact_learned on the EventBus."""

    @pytest.mark.asyncio
    async def test_learn_emits_fact_learned_on_bus(self):
        """When Heart._bus is set, learn() should emit fact_learned with content."""
        from nous.heart.heart import Heart

        heart = MagicMock(spec=Heart)
        heart._bus = MagicMock(spec=EventBus)
        heart._bus.emit = AsyncMock()

        fake_detail = _fake_fact_detail(
            content="PostgreSQL uses MVCC",
            subject="PostgreSQL",
        )
        heart.facts = AsyncMock()
        heart.facts.learn = AsyncMock(return_value=fake_detail)
        heart.agent_id = "test-agent"

        # Call the real learn method with mocked internals
        result = await Heart.learn(heart, FactInput(content="PostgreSQL uses MVCC", subject="PostgreSQL"))

        assert result == fake_detail
        heart._bus.emit.assert_called_once()
        emitted = heart._bus.emit.call_args[0][0]
        assert emitted.type == "fact_learned"
        assert emitted.data["fact_id"] == str(fake_detail.id)
        assert emitted.data["content"] == "PostgreSQL uses MVCC"
        assert emitted.data["category"] == "technical"
        assert emitted.data["subject"] == "PostgreSQL"

    @pytest.mark.asyncio
    async def test_learn_works_without_bus(self):
        """When Heart._bus is None, learn() should still work (no emission)."""
        from nous.heart.heart import Heart

        heart = MagicMock(spec=Heart)
        heart._bus = None

        fake_detail = _fake_fact_detail(content="test fact", subject=None)
        heart.facts = AsyncMock()
        heart.facts.learn = AsyncMock(return_value=fake_detail)

        result = await Heart.learn(heart, FactInput(content="test fact"))
        assert result == fake_detail
