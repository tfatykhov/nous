"""Tests for F055 inline correction detection in MonitorEngine."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestCorrectionPatternMatching:
    """Test that correction patterns are properly detected."""

    @pytest.mark.asyncio
    async def test_detects_no_actually(self):
        from nous.cognitive.monitor import MonitorEngine

        brain = MagicMock()
        heart = MagicMock()
        heart.learn = AsyncMock()
        settings = MagicMock()
        settings.correction_extraction_enabled = True
        settings.background_model = "test-model"

        monitor = MonitorEngine(brain, heart, settings)
        monitor._llm_client = AsyncMock()

        with patch("nous.handlers.call_background_llm", new_callable=AsyncMock) as mock_llm:
            mock_llm.return_value = '{"principle": "Always confirm the specific file path before making changes to avoid errors", "subject": "file operations", "is_censor": false, "censor_pattern": null, "confidence": 0.8}'

            result = await monitor.detect_and_extract_correction(
                user_message="No, actually I meant the other file",
                ai_response="I'll update the other file instead",
                session_id="test-session",
            )

            assert result is not None
            assert "principle" in result
            heart.learn.assert_called_once()

    @pytest.mark.asyncio
    async def test_detects_thats_wrong(self):
        from nous.cognitive.monitor import MonitorEngine

        brain = MagicMock()
        heart = MagicMock()
        heart.learn = AsyncMock()
        settings = MagicMock()
        settings.correction_extraction_enabled = True
        settings.background_model = "test-model"

        monitor = MonitorEngine(brain, heart, settings)
        monitor._llm_client = AsyncMock()

        with patch("nous.handlers.call_background_llm", new_callable=AsyncMock) as mock_llm:
            mock_llm.return_value = '{"principle": "Check database connection status before running complex queries to prevent timeout failures", "subject": "database", "is_censor": false, "censor_pattern": null, "confidence": 0.7}'

            result = await monitor.detect_and_extract_correction(
                user_message="That's wrong, the database is not connected",
                ai_response="I apologize, let me check the connection first",
                session_id="test-session",
            )

            assert result is not None
            heart.learn.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_match_returns_none(self):
        from nous.cognitive.monitor import MonitorEngine

        brain = MagicMock()
        heart = MagicMock()
        heart.learn = AsyncMock()
        settings = MagicMock()
        settings.correction_extraction_enabled = True

        monitor = MonitorEngine(brain, heart, settings)
        monitor._llm_client = AsyncMock()

        result = await monitor.detect_and_extract_correction(
            user_message="Thanks, that looks great!",
            ai_response="You're welcome!",
            session_id="test-session",
        )

        assert result is None
        heart.learn.assert_not_called()

    @pytest.mark.asyncio
    async def test_disabled_returns_none(self):
        from nous.cognitive.monitor import MonitorEngine

        brain = MagicMock()
        heart = MagicMock()
        heart.learn = AsyncMock()
        settings = MagicMock()
        settings.correction_extraction_enabled = False

        monitor = MonitorEngine(brain, heart, settings)

        result = await monitor.detect_and_extract_correction(
            user_message="No, actually that's wrong",
            ai_response="Sorry about that",
            session_id="test-session",
        )

        assert result is None
        heart.learn.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_llm_client_returns_none(self):
        from nous.cognitive.monitor import MonitorEngine

        brain = MagicMock()
        heart = MagicMock()
        heart.learn = AsyncMock()
        settings = MagicMock()
        settings.correction_extraction_enabled = True

        monitor = MonitorEngine(brain, heart, settings)
        # _llm_client is None by default

        result = await monitor.detect_and_extract_correction(
            user_message="No, actually that's wrong",
            ai_response="Sorry about that",
            session_id="test-session",
        )

        assert result is None
        heart.learn.assert_not_called()

    @pytest.mark.asyncio
    async def test_creates_censor_for_never_pattern(self):
        from nous.cognitive.monitor import MonitorEngine

        brain = MagicMock()
        heart = MagicMock()
        heart.learn = AsyncMock()
        heart.add_censor = AsyncMock()
        settings = MagicMock()
        settings.correction_extraction_enabled = True
        settings.background_model = "test-model"

        monitor = MonitorEngine(brain, heart, settings)
        monitor._llm_client = AsyncMock()

        with patch("nous.handlers.call_background_llm", new_callable=AsyncMock) as mock_llm:
            mock_llm.return_value = '{"principle": "Never delete user data without explicit confirmation from the user first", "subject": "data safety", "is_censor": true, "censor_pattern": "delete user data", "confidence": 0.95}'

            result = await monitor.detect_and_extract_correction(
                user_message="Never do that again, don't delete my data",
                ai_response="I understand, I won't delete without asking first",
                session_id="test-session",
            )

            assert result is not None
            assert result["is_censor"] is True
            heart.learn.assert_called_once()
            heart.add_censor.assert_called_once()
            censor_input = heart.add_censor.call_args[0][0]
            assert censor_input.trigger_pattern == "delete user data"

    @pytest.mark.asyncio
    async def test_case_insensitive_matching(self):
        from nous.cognitive.monitor import MonitorEngine

        brain = MagicMock()
        heart = MagicMock()
        heart.learn = AsyncMock()
        settings = MagicMock()
        settings.correction_extraction_enabled = True
        settings.background_model = "test-model"

        monitor = MonitorEngine(brain, heart, settings)
        monitor._llm_client = AsyncMock()

        with patch("nous.handlers.call_background_llm", new_callable=AsyncMock) as mock_llm:
            mock_llm.return_value = '{"principle": "Verify file existence before attempting read operations to handle missing files gracefully", "subject": "file handling", "is_censor": false, "censor_pattern": null, "confidence": 0.75}'

            result = await monitor.detect_and_extract_correction(
                user_message="THAT'S WRONG, check the file first",
                ai_response="Let me verify the file exists",
                session_id="test-session",
            )

            assert result is not None
            heart.learn.assert_called_once()

    @pytest.mark.asyncio
    async def test_llm_failure_returns_none(self):
        from nous.cognitive.monitor import MonitorEngine

        brain = MagicMock()
        heart = MagicMock()
        heart.learn = AsyncMock()
        settings = MagicMock()
        settings.correction_extraction_enabled = True
        settings.background_model = "test-model"

        monitor = MonitorEngine(brain, heart, settings)
        monitor._llm_client = AsyncMock()

        with patch("nous.handlers.call_background_llm", new_callable=AsyncMock) as mock_llm:
            mock_llm.return_value = None  # LLM returns nothing

            result = await monitor.detect_and_extract_correction(
                user_message="No, actually that's wrong",
                ai_response="Sorry about that",
                session_id="test-session",
            )

            assert result is None
            heart.learn.assert_not_called()

    @pytest.mark.asyncio
    async def test_fact_source_is_inline_correction(self):
        from nous.cognitive.monitor import MonitorEngine

        brain = MagicMock()
        heart = MagicMock()
        heart.learn = AsyncMock()
        settings = MagicMock()
        settings.correction_extraction_enabled = True
        settings.background_model = "test-model"

        monitor = MonitorEngine(brain, heart, settings)
        monitor._llm_client = AsyncMock()

        with patch("nous.handlers.call_background_llm", new_callable=AsyncMock) as mock_llm:
            mock_llm.return_value = '{"principle": "Always double check mathematical calculations before presenting results to the user", "subject": "math", "is_censor": false, "censor_pattern": null, "confidence": 0.8}'

            await monitor.detect_and_extract_correction(
                user_message="That's incorrect, 2+2 is 4 not 5",
                ai_response="You're right, sorry",
                session_id="test-session",
            )

            fact_input = heart.learn.call_args[0][0]
            assert fact_input.source == "inline_correction"
            assert "auto:f055" in fact_input.tags
