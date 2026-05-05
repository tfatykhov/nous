"""F060 + F060.1 + F060.2 — abandoned-episode recovery sleep phase.

Verifies `SleepHandler._phase_recover_abandoned_episodes`:

  Recovery loop (F060 + F060.1):
    - Disabled flag → no-op, returns True.
    - No summarizer wired → no-op, returns True.
    - max_per_cycle=0 → no-op.
    - Transcript >= min_transcript → full recovery (recovered_full).
    - Transcript missing/short, summary >= min_summary → fallback (recovered_summary_only).
    - Both transcript and summary missing → skipped_no_data.
    - Fallback disabled + no transcript → skipped_no_data.
    - summarize raises → counted in errors, doesn't fail the phase.
    - Interrupt set → loop short-circuits.

  Mark-abandoned loop (F060.2):
    - mark_enabled=true → SQL UPDATE fires, marked_abandoned counter set.
    - mark_enabled=false → UPDATE not run.
    - Interrupt → mark UPDATE skipped.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
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
    object.__setattr__(s, "abandoned_recovery_summary_fallback_enabled", True)
    object.__setattr__(s, "abandoned_recovery_min_summary_chars", 20)
    object.__setattr__(s, "abandoned_recovery_mark_abandoned_enabled", True)
    object.__setattr__(s, "abandoned_recovery_mark_age_days", 7)
    object.__setattr__(s, "abandoned_recovery_mark_max_per_cycle", 200)
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
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _FakeUpdateResult:
    def __init__(self, rowcount):
        self.rowcount = rowcount


def _make_session_cm(select_rows, update_rowcount=0):
    """Build a session context manager whose execute() returns query rows for
    SELECT and a result with `rowcount` for UPDATE.

    The phase opens TWO sessions (one for the recovery SELECT, one for the
    mark-abandoned UPDATE), so the returned factory yields a fresh session
    on each `db.session()` call to avoid AsyncMock state bleed.
    """
    def factory():
        session = AsyncMock()

        call_state = {"calls": 0}

        async def execute(stmt, params=None):
            call_state["calls"] += 1
            # Distinguish SELECT vs UPDATE by call ordering — both go through
            # the same execute() but the UPDATE only fires inside the second
            # context manager. Each session sees only its own one execute().
            sql_str = str(stmt)
            if "UPDATE heart.episodes" in sql_str:
                return _FakeUpdateResult(update_rowcount)
            return _FakeRows(select_rows)

        session.execute = AsyncMock(side_effect=execute)
        session.commit = AsyncMock()
        sess_cm = MagicMock()
        sess_cm.__aenter__ = AsyncMock(return_value=session)
        sess_cm.__aexit__ = AsyncMock(return_value=None)
        return sess_cm

    return factory


# ---------------------------------------------------------------------
# Disable / no-op short circuits
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_disabled_flag_short_circuits():
    settings = _make_settings(abandoned_recovery_enabled=False)
    summarizer = SimpleNamespace(summarize_episode=AsyncMock())
    handler = _make_handler(settings, summarizer=summarizer)
    sleep_stats: dict = {}

    ok = await handler._phase_recover_abandoned_episodes(sleep_stats)

    assert ok is True
    summarizer.summarize_episode.assert_not_awaited()
    assert sleep_stats == {}


@pytest.mark.asyncio
async def test_no_summarizer_wired_is_noop():
    settings = _make_settings()
    handler = _make_handler(settings, summarizer=None)
    sleep_stats: dict = {}

    ok = await handler._phase_recover_abandoned_episodes(sleep_stats)

    assert ok is True
    assert sleep_stats == {}


@pytest.mark.asyncio
async def test_max_per_cycle_zero_short_circuits():
    settings = _make_settings(abandoned_recovery_max_per_cycle=0)
    summarizer = SimpleNamespace(summarize_episode=AsyncMock())
    handler = _make_handler(settings, summarizer=summarizer)
    sleep_stats: dict = {}

    ok = await handler._phase_recover_abandoned_episodes(sleep_stats)

    assert ok is True
    summarizer.summarize_episode.assert_not_awaited()


# ---------------------------------------------------------------------
# F060 base — full transcript recovery
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recovers_with_full_transcript():
    settings = _make_settings()
    summarizer = SimpleNamespace(
        summarize_episode=AsyncMock(return_value={"title": "ok"}),
    )
    handler = _make_handler(settings, summarizer=summarizer)

    ep1, ep2 = uuid4(), uuid4()
    long_transcript = "user: hello" + ("x" * 100)
    factory = _make_session_cm(
        [(ep1, long_transcript, "short summary"),
         (ep2, long_transcript, None)],
        update_rowcount=0,
    )
    handler._heart.db.session = MagicMock(side_effect=factory)

    sleep_stats: dict = {}
    ok = await handler._phase_recover_abandoned_episodes(sleep_stats)

    assert ok is True
    assert summarizer.summarize_episode.await_count == 2
    assert sleep_stats["episodes_recovered"] == 2
    assert sleep_stats["episodes_recovered_full_transcript"] == 2
    assert "episodes_recovered_summary_only" not in sleep_stats


# ---------------------------------------------------------------------
# F060.1 — summary fallback
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fallback_to_summary_when_transcript_missing():
    settings = _make_settings()
    summarizer = SimpleNamespace(
        summarize_episode=AsyncMock(return_value={"title": "ok"}),
    )
    handler = _make_handler(settings, summarizer=summarizer)

    ep1 = uuid4()
    plain_summary = "User asked about NOUS_MAX_TOKENS configuration"
    factory = _make_session_cm(
        [(ep1, None, plain_summary)],
        update_rowcount=0,
    )
    handler._heart.db.session = MagicMock(side_effect=factory)

    sleep_stats: dict = {}
    await handler._phase_recover_abandoned_episodes(sleep_stats)

    summarizer.summarize_episode.assert_awaited_once()
    # Confirm the fallback used `summary` as the transcript argument
    call_args = summarizer.summarize_episode.await_args.kwargs
    assert call_args["transcript"] == plain_summary
    assert sleep_stats["episodes_recovered"] == 1
    assert sleep_stats["episodes_recovered_summary_only"] == 1
    assert "episodes_recovered_full_transcript" not in sleep_stats


@pytest.mark.asyncio
async def test_fallback_disabled_skips_when_only_summary():
    settings = _make_settings(
        abandoned_recovery_summary_fallback_enabled=False,
    )
    summarizer = SimpleNamespace(summarize_episode=AsyncMock())
    handler = _make_handler(settings, summarizer=summarizer)

    ep1 = uuid4()
    factory = _make_session_cm(
        [(ep1, None, "User asked about NOUS_MAX_TOKENS configuration")],
        update_rowcount=0,
    )
    handler._heart.db.session = MagicMock(side_effect=factory)

    sleep_stats: dict = {}
    await handler._phase_recover_abandoned_episodes(sleep_stats)

    summarizer.summarize_episode.assert_not_awaited()
    assert sleep_stats["abandoned_recovery_skipped_no_data"] == 1


@pytest.mark.asyncio
async def test_no_transcript_no_summary_skipped_no_data():
    settings = _make_settings()
    summarizer = SimpleNamespace(summarize_episode=AsyncMock())
    handler = _make_handler(settings, summarizer=summarizer)

    ep1 = uuid4()
    factory = _make_session_cm([(ep1, None, None)], update_rowcount=0)
    handler._heart.db.session = MagicMock(side_effect=factory)

    sleep_stats: dict = {}
    await handler._phase_recover_abandoned_episodes(sleep_stats)

    summarizer.summarize_episode.assert_not_awaited()
    assert sleep_stats["abandoned_recovery_skipped_no_data"] == 1


@pytest.mark.asyncio
async def test_short_summary_skipped_no_data():
    settings = _make_settings(abandoned_recovery_min_summary_chars=20)
    summarizer = SimpleNamespace(summarize_episode=AsyncMock())
    handler = _make_handler(settings, summarizer=summarizer)

    ep1 = uuid4()
    factory = _make_session_cm(
        [(ep1, None, "tiny")],  # 4 chars < 20
        update_rowcount=0,
    )
    handler._heart.db.session = MagicMock(side_effect=factory)

    sleep_stats: dict = {}
    await handler._phase_recover_abandoned_episodes(sleep_stats)

    summarizer.summarize_episode.assert_not_awaited()
    assert sleep_stats["abandoned_recovery_skipped_no_data"] == 1


@pytest.mark.asyncio
async def test_mixed_recovery_paths_in_one_cycle():
    settings = _make_settings()
    summarizer = SimpleNamespace(
        summarize_episode=AsyncMock(return_value={"title": "ok"}),
    )
    handler = _make_handler(settings, summarizer=summarizer)

    ep1, ep2, ep3 = uuid4(), uuid4(), uuid4()
    long_transcript = "user: hello" + ("x" * 100)
    factory = _make_session_cm(
        [
            (ep1, long_transcript, "summary one"),  # full
            (ep2, None, "User asked about config" * 3),  # fallback
            (ep3, None, None),  # skip
        ],
        update_rowcount=0,
    )
    handler._heart.db.session = MagicMock(side_effect=factory)

    sleep_stats: dict = {}
    await handler._phase_recover_abandoned_episodes(sleep_stats)

    assert summarizer.summarize_episode.await_count == 2
    assert sleep_stats["episodes_recovered"] == 2
    assert sleep_stats["episodes_recovered_full_transcript"] == 1
    assert sleep_stats["episodes_recovered_summary_only"] == 1
    assert sleep_stats["abandoned_recovery_skipped_no_data"] == 1


# ---------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_summarize_exception_counted_no_phase_failure():
    settings = _make_settings()
    summarizer = SimpleNamespace(
        summarize_episode=AsyncMock(side_effect=RuntimeError("boom")),
    )
    handler = _make_handler(settings, summarizer=summarizer)

    ep1 = uuid4()
    long_transcript = "user: hi" + ("x" * 100)
    factory = _make_session_cm(
        [(ep1, long_transcript, None)],
        update_rowcount=0,
    )
    handler._heart.db.session = MagicMock(side_effect=factory)

    sleep_stats: dict = {}
    ok = await handler._phase_recover_abandoned_episodes(sleep_stats)

    assert ok is True  # phase tolerates per-row errors
    assert sleep_stats["abandoned_recovery_errors"] == 1
    assert sleep_stats.get("episodes_recovered", 0) == 0


@pytest.mark.asyncio
async def test_interrupt_short_circuits_loop():
    settings = _make_settings()
    summarizer = SimpleNamespace(
        summarize_episode=AsyncMock(return_value={"title": "ok"}),
    )
    handler = _make_handler(settings, summarizer=summarizer)
    handler._interrupted = True

    ep1, ep2 = uuid4(), uuid4()
    long_transcript = "user: hi" + ("x" * 100)
    factory = _make_session_cm(
        [(ep1, long_transcript, None), (ep2, long_transcript, None)],
        update_rowcount=0,
    )
    handler._heart.db.session = MagicMock(side_effect=factory)

    sleep_stats: dict = {}
    await handler._phase_recover_abandoned_episodes(sleep_stats)

    summarizer.summarize_episode.assert_not_awaited()


# ---------------------------------------------------------------------
# F060.2 — mark abandoned
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mark_abandoned_fires_when_enabled():
    settings = _make_settings()
    summarizer = SimpleNamespace(
        summarize_episode=AsyncMock(return_value={"title": "ok"}),
    )
    handler = _make_handler(settings, summarizer=summarizer)

    factory = _make_session_cm([], update_rowcount=7)  # no recovery; 7 marked
    handler._heart.db.session = MagicMock(side_effect=factory)

    sleep_stats: dict = {}
    ok = await handler._phase_recover_abandoned_episodes(sleep_stats)

    assert ok is True
    assert sleep_stats["episodes_marked_abandoned"] == 7
    summarizer.summarize_episode.assert_not_awaited()


@pytest.mark.asyncio
async def test_mark_abandoned_disabled_does_not_fire():
    settings = _make_settings(abandoned_recovery_mark_abandoned_enabled=False)
    summarizer = SimpleNamespace(summarize_episode=AsyncMock())
    handler = _make_handler(settings, summarizer=summarizer)

    factory = _make_session_cm([], update_rowcount=7)
    handler._heart.db.session = MagicMock(side_effect=factory)

    sleep_stats: dict = {}
    await handler._phase_recover_abandoned_episodes(sleep_stats)

    # When disabled, the second session is never opened — counter not set.
    assert "episodes_marked_abandoned" not in sleep_stats


@pytest.mark.asyncio
async def test_recovery_and_mark_abandoned_both_fire_in_one_cycle():
    settings = _make_settings()
    summarizer = SimpleNamespace(
        summarize_episode=AsyncMock(return_value={"title": "ok"}),
    )
    handler = _make_handler(settings, summarizer=summarizer)

    ep1 = uuid4()
    long_transcript = "user: hi" + ("x" * 100)
    factory = _make_session_cm(
        [(ep1, long_transcript, None)],
        update_rowcount=3,
    )
    handler._heart.db.session = MagicMock(side_effect=factory)

    sleep_stats: dict = {}
    await handler._phase_recover_abandoned_episodes(sleep_stats)

    assert sleep_stats["episodes_recovered"] == 1
    assert sleep_stats["episodes_recovered_full_transcript"] == 1
    assert sleep_stats["episodes_marked_abandoned"] == 3
