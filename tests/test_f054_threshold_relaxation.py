"""F054 — Selective CE-threshold relaxation tests.

Covers:
1. New default values for the four same-type CE-mode thresholds.
2. Cross-type fact_decision / fact_episode thresholds STAY at strict 0.55.
3. New ``ce_backfill_min_decision_chars`` Settings field exists and defaults to 40.
4. ``fetch_candidate_content`` drops decision rows below the min-chars guard
   when ``settings`` is passed.
5. ``fetch_candidate_content`` is backward-compatible: when ``settings`` is
   omitted (or for non-decision entity types), no min-chars filtering happens
   beyond the existing whitespace-only drop.
6. Threshold routing (``_get_threshold``) returns the new defaults under
   CE-mode and falls back to strict ``graph_threshold_*`` defaults otherwise.

Tests #1-3 + #6 are pure-Settings unit tests (no DB). Tests #4 + #5 stub
``session.execute`` to avoid the Postgres dependency, focusing on the
content-guard branch logic.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from nous.config import Settings


# ---------------------------------------------------------------------------
# 1. Same-type CE-mode threshold defaults
# ---------------------------------------------------------------------------


def test_same_type_thresholds_relaxed_by_f054() -> None:
    """F054: fact_fact / decision_decision / episode_episode / procedure_any
    defaults are now relaxed compared to F045's pre-2026-04-26 values."""
    s = Settings()
    # fact_fact was 0.65 (F045 2026-04-14 A/B at 80% precision), F054 → 0.55
    assert s.ce_backfill_threshold_fact_fact == 0.55
    # decision_decision was 0.60, F054 → 0.50
    assert s.ce_backfill_threshold_decision_decision == 0.50
    # episode_episode was 0.58, F054 → 0.50
    assert s.ce_backfill_threshold_episode_episode == 0.50
    # procedure_any was 0.55, F054 → 0.45
    assert s.ce_backfill_threshold_procedure_any == 0.45


# ---------------------------------------------------------------------------
# 2. Cross-type thresholds STAY STRICT at 0.55 (F054 invariant)
# ---------------------------------------------------------------------------


def test_cross_type_thresholds_unchanged_by_f054() -> None:
    """F054: fact_decision and fact_episode KEPT STRICT at 0.55 because
    F053 audit showed evidence_for precision regresses from 0.57 → 0.47
    when these are loosened (corpus-quality issue with empty decision context;
    threshold can't fix the corpus)."""
    s = Settings()
    assert s.ce_backfill_threshold_fact_decision == 0.55
    assert s.ce_backfill_threshold_fact_episode == 0.55


# ---------------------------------------------------------------------------
# 3. New ce_backfill_min_decision_chars Field exists with default 40
# ---------------------------------------------------------------------------


def test_min_decision_chars_field_default() -> None:
    """F054: new content-length guard for decisions, mirroring F045's
    ce_backfill_min_content_chars=80 for facts. Decisions are naturally
    shorter (default 40)."""
    s = Settings()
    assert hasattr(s, "ce_backfill_min_decision_chars")
    assert s.ce_backfill_min_decision_chars == 40


def test_min_decision_chars_env_override(monkeypatch) -> None:
    """Operators can override via NOUS_CE_BACKFILL_MIN_DECISION_CHARS."""
    monkeypatch.setenv("NOUS_CE_BACKFILL_MIN_DECISION_CHARS", "100")
    s = Settings()
    assert s.ce_backfill_min_decision_chars == 100


# ---------------------------------------------------------------------------
# 4. fetch_candidate_content drops short decisions when settings passed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_candidate_content_decision_guard_drops_short() -> None:
    """F054 decision-content guard fires when entity_type=='decision' and
    settings is passed. Rows with stripped content < min_decision_chars
    are dropped silently (not raised — matches F045's silent-drop behavior)."""
    from nous.brain.backfill_rerank import fetch_candidate_content

    id_long = uuid4()
    id_short = uuid4()
    id_whitespace = uuid4()

    # Mock async session.execute returning 3 rows
    fake_rows = [
        SimpleNamespace(id=id_long, content="A" * 50),  # 50 chars > 40 → keep
        SimpleNamespace(id=id_short, content="too short"),  # 9 chars < 40 → drop
        SimpleNamespace(id=id_whitespace, content="    "),  # whitespace → drop
    ]

    # Mock the SQLAlchemy async session
    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=fake_rows)

    settings = Settings()  # ce_backfill_min_decision_chars=40 by default

    out = await fetch_candidate_content(
        mock_session,
        agent_id="test-agent",
        entity_type="decision",
        candidate_ids=[id_long, id_short, id_whitespace],
        settings=settings,
    )
    # Only the 50-char row survives
    assert id_long in out
    assert id_short not in out
    assert id_whitespace not in out
    assert len(out) == 1


# ---------------------------------------------------------------------------
# 5. fetch_candidate_content backward-compatible without settings
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_candidate_content_backward_compat_no_settings() -> None:
    """When settings is omitted, no min-chars filter applies (only the
    existing whitespace-only drop). Preserves F040/F043 behavior for
    callers that haven't been updated."""
    from nous.brain.backfill_rerank import fetch_candidate_content

    id_long = uuid4()
    id_short = uuid4()

    fake_rows = [
        SimpleNamespace(id=id_long, content="A" * 50),
        SimpleNamespace(id=id_short, content="too short"),  # 9 chars
    ]

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=fake_rows)

    # No settings kwarg → pre-F054 behavior: both kept (whitespace not empty)
    out = await fetch_candidate_content(
        mock_session,
        agent_id="test-agent",
        entity_type="decision",
        candidate_ids=[id_long, id_short],
    )
    assert id_long in out
    assert id_short in out  # F054 guard NOT applied without settings


@pytest.mark.asyncio
async def test_fetch_candidate_content_guard_only_for_decision_type() -> None:
    """F054 guard is type-aware: only fires for entity_type='decision',
    not for facts/episodes/procedures (each has its own char floor)."""
    from nous.brain.backfill_rerank import fetch_candidate_content

    id_short = uuid4()
    fake_rows = [SimpleNamespace(id=id_short, content="too short")]
    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=fake_rows)

    settings = Settings()
    # entity_type='fact' → F054 decision guard does NOT apply
    out = await fetch_candidate_content(
        mock_session,
        agent_id="test-agent",
        entity_type="fact",
        candidate_ids=[id_short],
        settings=settings,
    )
    # Short content kept (F045's facts content guard fires LATER, at rerank time)
    assert id_short in out


# ---------------------------------------------------------------------------
# 6. _get_threshold routing returns the new F054 defaults under CE-mode
# ---------------------------------------------------------------------------


def test_get_threshold_routes_to_f054_defaults_when_ce_enabled() -> None:
    """When ce_backfill_enabled=True, _get_threshold returns the new
    F054-relaxed values for same-type relations."""
    from nous.brain.graph_densifier import _get_threshold

    s = Settings(ce_backfill_enabled=True)
    assert _get_threshold(s, "fact", "fact") == 0.55
    assert _get_threshold(s, "decision", "decision") == 0.50
    assert _get_threshold(s, "episode", "episode") == 0.50
    # procedure_any covers any pair containing "procedure"
    assert _get_threshold(s, "procedure", "fact") == 0.45
    assert _get_threshold(s, "procedure", "decision") == 0.45


def test_get_threshold_cross_type_stays_strict_under_f054() -> None:
    """Cross-type fact_decision and fact_episode stay at 0.55 under CE-mode."""
    from nous.brain.graph_densifier import _get_threshold

    s = Settings(ce_backfill_enabled=True)
    # _get_threshold sorts the type pair, so order doesn't matter
    assert _get_threshold(s, "fact", "decision") == 0.55
    assert _get_threshold(s, "decision", "fact") == 0.55
    assert _get_threshold(s, "fact", "episode") == 0.55
    assert _get_threshold(s, "episode", "fact") == 0.55


def test_get_threshold_falls_back_to_strict_when_ce_disabled() -> None:
    """When ce_backfill_enabled=False, _get_threshold returns the strict
    graph_threshold_* defaults (untouched by F054)."""
    from nous.brain.graph_densifier import _get_threshold

    s = Settings(ce_backfill_enabled=False)
    # graph_threshold_fact_fact default is 0.82 (F045 strict)
    assert _get_threshold(s, "fact", "fact") == 0.82
