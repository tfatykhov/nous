"""F060 — abandoned-episode recovery sleep phase.

Verifies `SleepHandler._phase_recover_abandoned_episodes`:

  - Disabled flag → no-op, returns True.
  - No summarizer wired → no-op, returns True.
  - Active + NULL summary + transcript → summarize_episode called.
  - Transcript too short → skipped, counter incremented.
  - Summarize raises → counted in errors, doesn't fail the phase.
  - Bounded by `abandoned_recovery_max_per_cycle`.

Pure unit tests with a mock SleepHandler — no DB required.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from nous.config import Settings
from nous.handlers.sleep_handler import SleepHandler


def _make_settings(**overrides) -> Settings:
    s = Settings(_env_file=None)
    object.__setattr__(s, "abandoned_recovery_enabled", True)
    object.__setattr__(s, "abandoned_recovery_min_age_hours", 24)
    object.__setattr__(s, "abandoned_recovery_max_per_cycle", 50)
    object.__setattr__(s, "abandoned_recovery_min_transcript_chars", 50)
    object.__setattr__(s, "agent_id", "test-agent")
    for k, v in overrides.items():
        object.__setattr__(s, k, v)
    return s


def _make_handler(settings: Settings, summarizer=None) -> SleepHandler:
    brain = MagicMock()
    heart = MagicMock()
    heart.db = MagicMock()
    bus = MagicMock()
    bus.on = MagicMock()
    handler = SleepHandler(brain, heart, settings, bus, llm_client=None)
    handler._episode_summarizer = summarizer
    return handler


class _FakeRows:
    """Mimics the SQLAlchemy result.all() return shape we use here."""

    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


@pytest.mark.asyncio
async def test_disabled_flag_short_circuits():
    settings = _make_settings(abandoned_recovery_enabled=False)
    summarizer = SimpleNamespace(summarize_episode=AsyncMock())
    handler = _make_handler(settings, summarizer=summarizer)
    sleep_stats: dict = {}

    ok = await handler._phase_recover_abandoned_episodes(sleep_stats)

    assert ok is True
    summarizer.summarize_episode.assert_not_awaited()
    assert "episodes_recovered" not in sleep_stats


@pytest.mark.asyncio
async def test_no_summarizer_wired_is_noop():
    settings = _make_settings()
    handler = _make_handler(settings, summarizer=None)
    sleep_stats: dict = {}

    ok = await handler._phase_recover_abandoned_episodes(sleep_stats)

    assert ok is True
    assert "episodes_recovered" not in sleep_stats


@pytest.mark.asyncio
async def test_max_per_cycle_zero_short_circuits():
    settings = _make_settings(abandoned_recovery_max_per_cycle=0)
    summarizer = SimpleNamespace(summarize_episode=AsyncMock())
    handler = _make_handler(settings, summarizer=summarizer)
    sleep_stats: dict = {}

    ok = await handler._phase_recover_abandoned_episodes(sleep_stats)

    assert ok is True
    summarizer.summarize_episode.assert_not_awaited()


@pytest.mark.asyncio
async def test_recovers_episodes_with_transcript():
    settings = _make_settings()
    summarizer = SimpleNamespace(
        summarize_episode=AsyncMock(return_value={"title": "ok"})
    )
    handler = _make_handler(settings, summarizer=summarizer)

    ep1, ep2 = uuid4(), uuid4()
    transcript_long = "user: hello\nassistant: hi" + "x" * 100

    async def fake_session():
        sess = AsyncMock()
        sess.execute = AsyncMock(return_value=_FakeRows([
            (ep1, transcript_long),
            (ep2, transcript_long),
        ]))
        return sess

    sess_cm = MagicMock()
    sess_cm.__aenter__ = AsyncMock(return_value=await fake_session())
    sess_cm.__aexit__ = AsyncMock(return_value=None)
    handler._heart.db.session = MagicMock(return_value=sess_cm)

    sleep_stats: dict = {}
    ok = await handler._phase_recover_abandoned_episodes(sleep_stats)

    assert ok is True
    assert summarizer.summarize_episode.await_count == 2
    assert sleep_stats["episodes_recovered"] == 2


@pytest.mark.asyncio
async def test_skips_short_transcript():
    settings = _make_settings(abandoned_recovery_min_transcript_chars=50)
    summarizer = SimpleNamespace(
        summarize_episode=AsyncMock(return_value={"title": "ok"})
    )
    handler = _make_handler(settings, summarizer=summarizer)

    ep1 = uuid4()
    short_transcript = "too short"

    sess = AsyncMock()
    sess.execute = AsyncMock(return_value=_FakeRows([(ep1, short_transcript)]))
    sess_cm = MagicMock()
    sess_cm.__aenter__ = AsyncMock(return_value=sess)
    sess_cm.__aexit__ = AsyncMock(return_value=None)
    handler._heart.db.session = MagicMock(return_value=sess_cm)

    sleep_stats: dict = {}
    await handler._phase_recover_abandoned_episodes(sleep_stats)

    summarizer.summarize_episode.assert_not_awaited()
    assert sleep_stats.get("episodes_recovered", 0) == 0
    assert sleep_stats["abandoned_recovery_skipped_no_transcript"] == 1


@pytest.mark.asyncio
async def test_skips_null_transcript():
    settings = _make_settings()
    summarizer = SimpleNamespace(
        summarize_episode=AsyncMock(return_value={"title": "ok"})
    )
    handler = _make_handler(settings, summarizer=summarizer)

    ep1 = uuid4()
    sess = AsyncMock()
    sess.execute = AsyncMock(return_value=_FakeRows([(ep1, None)]))
    sess_cm = MagicMock()
    sess_cm.__aenter__ = AsyncMock(return_value=sess)
    sess_cm.__aexit__ = AsyncMock(return_value=None)
    handler._heart.db.session = MagicMock(return_value=sess_cm)

    sleep_stats: dict = {}
    await handler._phase_recover_abandoned_episodes(sleep_stats)

    summarizer.summarize_episode.assert_not_awaited()
    assert sleep_stats["abandoned_recovery_skipped_no_transcript"] == 1


@pytest.mark.asyncio
async def test_summarize_exception_counted_no_phase_failure():
    settings = _make_settings()
    summarizer = SimpleNamespace(
        summarize_episode=AsyncMock(side_effect=RuntimeError("boom")),
    )
    handler = _make_handler(settings, summarizer=summarizer)

    ep1 = uuid4()
    transcript_long = "user: hello\nassistant: hi" + "x" * 100

    sess = AsyncMock()
    sess.execute = AsyncMock(return_value=_FakeRows([(ep1, transcript_long)]))
    sess_cm = MagicMock()
    sess_cm.__aenter__ = AsyncMock(return_value=sess)
    sess_cm.__aexit__ = AsyncMock(return_value=None)
    handler._heart.db.session = MagicMock(return_value=sess_cm)

    sleep_stats: dict = {}
    ok = await handler._phase_recover_abandoned_episodes(sleep_stats)

    # Phase should NOT fail — errors are counted, not raised.
    assert ok is True
    assert sleep_stats["abandoned_recovery_errors"] == 1
    assert sleep_stats["episodes_recovered"] == 0


@pytest.mark.asyncio
async def test_interrupt_short_circuits_loop():
    settings = _make_settings()
    summarizer = SimpleNamespace(
        summarize_episode=AsyncMock(return_value={"title": "ok"}),
    )
    handler = _make_handler(settings, summarizer=summarizer)
    handler._interrupted = True  # simulate concurrent message_received

    ep1, ep2, ep3 = uuid4(), uuid4(), uuid4()
    transcript_long = "user: hi" + "x" * 100

    sess = AsyncMock()
    sess.execute = AsyncMock(return_value=_FakeRows([
        (ep1, transcript_long),
        (ep2, transcript_long),
        (ep3, transcript_long),
    ]))
    sess_cm = MagicMock()
    sess_cm.__aenter__ = AsyncMock(return_value=sess)
    sess_cm.__aexit__ = AsyncMock(return_value=None)
    handler._heart.db.session = MagicMock(return_value=sess_cm)

    sleep_stats: dict = {}
    await handler._phase_recover_abandoned_episodes(sleep_stats)

    # Interrupt set before loop body runs → no episodes processed.
    summarizer.summarize_episode.assert_not_awaited()
