"""Tests for F039 correction extractor handler."""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nous.events import Event


class _AsyncCtx:
    def __init__(self, s):
        self._s = s

    async def __aenter__(self):
        return self._s

    async def __aexit__(self, *a):
        pass


class TestCorrectionExtractor:
    @pytest.mark.asyncio
    async def test_skip_when_disabled(self):
        from nous.handlers.correction_extractor import CorrectionExtractor

        settings = MagicMock()
        settings.correction_extraction_enabled = False
        bus = MagicMock()
        bus.on = MagicMock()
        db = MagicMock()
        heart = MagicMock()

        extractor = CorrectionExtractor(
            db=db,
            settings=settings,
            bus=bus,
            llm_client=None,
            heart=heart,
            agent_id="test",
        )
        event = Event(
            type="outcome_signals_detected",
            agent_id="test",
            data={
                "episode_id": str(uuid.uuid4()),
                "signals": [{"type": "corrected", "confidence": 0.9, "evidence": "User corrected"}],
            },
        )
        await extractor.handle(event)
        # heart.learn should not be called when disabled
        heart.learn.assert_not_called()

    @pytest.mark.asyncio
    async def test_skip_when_no_corrected_signals(self):
        from nous.handlers.correction_extractor import CorrectionExtractor

        settings = MagicMock()
        settings.correction_extraction_enabled = True
        bus = MagicMock()
        bus.on = MagicMock()
        db = MagicMock()
        heart = MagicMock()

        extractor = CorrectionExtractor(
            db=db,
            settings=settings,
            bus=bus,
            llm_client=None,
            heart=heart,
            agent_id="test",
        )
        event = Event(
            type="outcome_signals_detected",
            agent_id="test",
            data={
                "episode_id": str(uuid.uuid4()),
                "signals": [{"type": "completed", "confidence": 0.8, "evidence": "Task done"}],
            },
        )
        await extractor.handle(event)
        heart.learn.assert_not_called()

    @pytest.mark.asyncio
    async def test_skip_when_no_episode_id(self):
        from nous.handlers.correction_extractor import CorrectionExtractor

        settings = MagicMock()
        settings.correction_extraction_enabled = True
        bus = MagicMock()
        bus.on = MagicMock()
        db = MagicMock()
        heart = MagicMock()

        extractor = CorrectionExtractor(
            db=db,
            settings=settings,
            bus=bus,
            llm_client=None,
            heart=heart,
            agent_id="test",
        )
        event = Event(
            type="outcome_signals_detected",
            agent_id="test",
            data={
                "signals": [{"type": "corrected", "confidence": 0.9, "evidence": "correction"}],
            },
        )
        await extractor.handle(event)
        heart.learn.assert_not_called()

    @pytest.mark.asyncio
    async def test_extract_fact_from_correction(self):
        from nous.handlers.correction_extractor import CorrectionExtractor

        settings = MagicMock()
        settings.correction_extraction_enabled = True
        settings.background_model = "test-model"
        bus = MagicMock()
        bus.on = MagicMock()

        # Mock DB session to return an episode
        mock_episode = MagicMock()
        mock_episode.transcript = "User: Do X\nAssistant: Did Y\nUser: No, I meant X"
        mock_episode.structured_summary = {"outcome": "corrected"}
        mock_episode.summary = "User corrected the AI"

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_episode

        mock_session = AsyncMock()
        mock_session.execute = AsyncMock(return_value=mock_result)

        db = MagicMock()
        db.session = MagicMock(return_value=_AsyncCtx(mock_session))

        heart = MagicMock()
        heart.learn = AsyncMock()
        heart.add_censor = AsyncMock()

        llm_client = AsyncMock()

        extractor = CorrectionExtractor(
            db=db,
            settings=settings,
            bus=bus,
            llm_client=llm_client,
            heart=heart,
            agent_id="test",
        )

        with patch("nous.handlers.correction_extractor.call_background_llm", new_callable=AsyncMock) as mock_llm:
            mock_llm.return_value = '{"principle": "Always ask for clarification when the request is ambiguous before proceeding", "subject": "communication", "is_censor": false, "censor_pattern": null, "confidence": 0.85}'

            event = Event(
                type="outcome_signals_detected",
                agent_id="test",
                data={
                    "episode_id": str(uuid.uuid4()),
                    "signals": [{"type": "corrected", "confidence": 0.9, "evidence": "User said no"}],
                },
            )
            await extractor.handle(event)

            heart.learn.assert_called_once()
            fact_input = heart.learn.call_args[0][0]
            assert "clarification" in fact_input.content.lower()
            assert fact_input.source == "correction_extraction"
            assert fact_input.category == "rule"
            assert "correction" in fact_input.tags

    @pytest.mark.asyncio
    async def test_create_censor_for_never_do_pattern(self):
        from nous.handlers.correction_extractor import CorrectionExtractor

        settings = MagicMock()
        settings.correction_extraction_enabled = True
        settings.background_model = "test-model"
        bus = MagicMock()
        bus.on = MagicMock()

        mock_episode = MagicMock()
        mock_episode.transcript = "User: Don't ever do that again"
        mock_episode.structured_summary = {}
        mock_episode.summary = "User told AI never to do something"

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_episode

        mock_session = AsyncMock()
        mock_session.execute = AsyncMock(return_value=mock_result)

        db = MagicMock()
        db.session = MagicMock(return_value=_AsyncCtx(mock_session))

        heart = MagicMock()
        heart.learn = AsyncMock()
        heart.add_censor = AsyncMock()

        llm_client = AsyncMock()

        extractor = CorrectionExtractor(
            db=db,
            settings=settings,
            bus=bus,
            llm_client=llm_client,
            heart=heart,
            agent_id="test",
        )

        with patch("nous.handlers.correction_extractor.call_background_llm", new_callable=AsyncMock) as mock_llm:
            mock_llm.return_value = '{"principle": "Never make assumptions about user preferences without asking first and confirming", "subject": "user interaction", "is_censor": true, "censor_pattern": "assume user preference", "confidence": 0.9}'

            event = Event(
                type="outcome_signals_detected",
                agent_id="test",
                data={
                    "episode_id": str(uuid.uuid4()),
                    "signals": [{"type": "corrected", "confidence": 0.95, "evidence": "Don't ever"}],
                },
            )
            await extractor.handle(event)

            heart.learn.assert_called_once()
            heart.add_censor.assert_called_once()
            censor_input = heart.add_censor.call_args[0][0]
            assert censor_input.trigger_pattern == "assume user preference"
            assert censor_input.action == "warn"

    @pytest.mark.asyncio
    async def test_skip_short_principle(self):
        from nous.handlers.correction_extractor import CorrectionExtractor

        settings = MagicMock()
        settings.correction_extraction_enabled = True
        settings.background_model = "test-model"
        bus = MagicMock()
        bus.on = MagicMock()

        mock_episode = MagicMock()
        mock_episode.transcript = "short"
        mock_episode.structured_summary = {}
        mock_episode.summary = ""

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_episode

        mock_session = AsyncMock()
        mock_session.execute = AsyncMock(return_value=mock_result)

        db = MagicMock()
        db.session = MagicMock(return_value=_AsyncCtx(mock_session))

        heart = MagicMock()
        heart.learn = AsyncMock()

        llm_client = AsyncMock()

        extractor = CorrectionExtractor(
            db=db,
            settings=settings,
            bus=bus,
            llm_client=llm_client,
            heart=heart,
            agent_id="test",
        )

        with patch("nous.handlers.correction_extractor.call_background_llm", new_callable=AsyncMock) as mock_llm:
            # Return a principle that's too short
            mock_llm.return_value = '{"principle": "Be careful", "subject": "general", "is_censor": false, "censor_pattern": null, "confidence": 0.5}'

            event = Event(
                type="outcome_signals_detected",
                agent_id="test",
                data={
                    "episode_id": str(uuid.uuid4()),
                    "signals": [{"type": "corrected", "confidence": 0.7, "evidence": "wrong"}],
                },
            )
            await extractor.handle(event)

            # Short principle should be skipped
            heart.learn.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_llm_returns_none(self):
        from nous.handlers.correction_extractor import CorrectionExtractor

        settings = MagicMock()
        settings.correction_extraction_enabled = True
        bus = MagicMock()
        bus.on = MagicMock()

        mock_episode = MagicMock()
        mock_episode.transcript = "some transcript"
        mock_episode.structured_summary = {}
        mock_episode.summary = ""

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_episode

        mock_session = AsyncMock()
        mock_session.execute = AsyncMock(return_value=mock_result)

        db = MagicMock()
        db.session = MagicMock(return_value=_AsyncCtx(mock_session))

        heart = MagicMock()
        heart.learn = AsyncMock()

        # No LLM client
        extractor = CorrectionExtractor(
            db=db,
            settings=settings,
            bus=bus,
            llm_client=None,
            heart=heart,
            agent_id="test",
        )

        event = Event(
            type="outcome_signals_detected",
            agent_id="test",
            data={
                "episode_id": str(uuid.uuid4()),
                "signals": [{"type": "corrected", "confidence": 0.9, "evidence": "wrong"}],
            },
        )
        await extractor.handle(event)
        heart.learn.assert_not_called()

    @pytest.mark.asyncio
    async def test_registers_on_bus(self):
        from nous.handlers.correction_extractor import CorrectionExtractor

        settings = MagicMock()
        settings.correction_extraction_enabled = True
        bus = MagicMock()
        bus.on = MagicMock()
        db = MagicMock()
        heart = MagicMock()

        CorrectionExtractor(
            db=db,
            settings=settings,
            bus=bus,
            llm_client=None,
            heart=heart,
            agent_id="test",
        )

        bus.on.assert_called_once()
        assert bus.on.call_args[0][0] == "outcome_signals_detected"
