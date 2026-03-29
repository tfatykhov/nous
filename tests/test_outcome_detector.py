"""Tests for F024 Phase 3b outcome signal detector."""
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


class TestOutcomeDetector:
    @pytest.mark.asyncio
    async def test_skip_when_no_episode_id(self):
        from nous.handlers.outcome_detector import OutcomeDetector
        settings = MagicMock()
        settings.rubric_outcome_detection_enabled = True
        settings.rubric_outcome_model = "test-model"
        bus = MagicMock()
        bus.on = MagicMock()
        db = MagicMock()

        detector = OutcomeDetector(db=db, settings=settings, bus=bus, llm_client=None, agent_id="test")
        event = Event(type="session_ended", agent_id="test", data={})
        await detector.handle(event)

    @pytest.mark.asyncio
    async def test_skip_when_disabled(self):
        from nous.handlers.outcome_detector import OutcomeDetector
        settings = MagicMock()
        settings.rubric_outcome_detection_enabled = False
        bus = MagicMock()
        bus.on = MagicMock()
        db = MagicMock()

        detector = OutcomeDetector(db=db, settings=settings, bus=bus, llm_client=None, agent_id="test")
        event = Event(
            type="session_ended", agent_id="test",
            data={"episode_id": str(uuid.uuid4()), "transcript": "A long enough transcript for testing purposes here."},
        )
        await detector.handle(event)

    @pytest.mark.asyncio
    async def test_skip_when_transcript_too_short(self):
        from nous.handlers.outcome_detector import OutcomeDetector
        settings = MagicMock()
        settings.rubric_outcome_detection_enabled = True
        bus = MagicMock()
        bus.on = MagicMock()
        db = MagicMock()

        detector = OutcomeDetector(db=db, settings=settings, bus=bus, llm_client=None, agent_id="test")
        event = Event(
            type="session_ended", agent_id="test",
            data={"episode_id": str(uuid.uuid4()), "transcript": "hi"},
        )
        await detector.handle(event)

    @pytest.mark.asyncio
    async def test_detect_correction_signal(self):
        from nous.handlers.outcome_detector import OutcomeDetector

        settings = MagicMock()
        settings.rubric_outcome_detection_enabled = True
        settings.rubric_outcome_model = "test-model"
        bus = MagicMock()
        bus.on = MagicMock()
        bus.emit = AsyncMock()

        mock_session = AsyncMock()
        mock_session.add = MagicMock()
        mock_session.commit = AsyncMock()
        db = MagicMock()
        db.session = MagicMock(return_value=_AsyncCtx(mock_session))

        llm_client = AsyncMock()

        detector = OutcomeDetector(
            db=db, settings=settings, bus=bus,
            llm_client=llm_client, agent_id="test",
        )

        with patch("nous.handlers.outcome_detector.call_background_llm", new_callable=AsyncMock) as mock_llm:
            mock_llm.return_value = '{"signals": [{"type": "corrected", "confidence": 0.9, "evidence": "User said no actually"}]}'

            event = Event(
                type="session_ended", agent_id="test",
                data={
                    "episode_id": str(uuid.uuid4()),
                    "transcript": "User: Do X\nAssistant: Did Y\nUser: No, actually I meant X not Y\nAssistant: Sorry, doing X now",
                },
            )
            await detector.handle(event)
            mock_session.add.assert_called()

    @pytest.mark.asyncio
    async def test_heuristic_fallback_completed(self):
        from nous.handlers.outcome_detector import OutcomeDetector

        settings = MagicMock()
        settings.rubric_outcome_detection_enabled = True
        bus = MagicMock()
        bus.on = MagicMock()
        bus.emit = AsyncMock()

        mock_session = AsyncMock()
        mock_session.add = MagicMock()
        mock_session.commit = AsyncMock()
        db = MagicMock()
        db.session = MagicMock(return_value=_AsyncCtx(mock_session))

        detector = OutcomeDetector(
            db=db, settings=settings, bus=bus,
            llm_client=None, agent_id="test",
        )

        event = Event(
            type="session_ended", agent_id="test",
            data={
                "episode_id": str(uuid.uuid4()),
                "transcript": "User: Please help me with X.\nAssistant: Here is the solution for X.\nUser: Thanks!",
                "summary": {"outcome": "resolved"},
            },
        )
        await detector.handle(event)
        mock_session.add.assert_called()
        added = mock_session.add.call_args[0][0]
        assert added.signal_type == "completed"
