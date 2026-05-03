"""Tests for nous_eval.probes.sleep_filter_dryrun.

Mock-based tests covering verdict-decision logic — the probe's value
is in classifying corpus state correctly (filter regression vs.
healthy-no-stale-facts vs. corpus-too-young).

The actual SQL execution is exercised by the live run against the
eval DB; these tests pin the classification rules so future edits
can't accidentally restore the over-strict "any 0 candidates = RED"
behavior the live first-run had before fixing.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from nous_eval.probes.sleep_filter_dryrun import (
    FilterResult,
    check_cluster_consolidation,
    check_procedure_recency,
    check_stale_scan,
    overall_exit_code,
)


def _make_conn(
    *,
    oldest_days: int | None = 100,
    candidate_rows: list | None = None,
    old_never_recalled: int = 0,
    old_recalled_long_ago: int = 0,
    cluster_rows: list | None = None,
    above_cap: int = 0,
    proc_in_window: int = 0,
    proc_total_eligible: int = 0,
) -> MagicMock:
    """Build an asyncpg.Connection mock that returns scripted values
    for the queries the probe issues."""
    conn = MagicMock()

    # Match by semantic SQL fragments rather than whitespace-sensitive
    # substrings. The previous version's two
    # "AND b.function IS NOT NULL ..." keys differed only by trailing
    # whitespace — an autoformat run on the probe would silently route
    # both queries to the same response. Each key here is unique enough
    # to survive routine SQL reformatting.
    def _classify(sql: str) -> str:
        if "MIN(created_at)" in sql:
            return "oldest_days"
        if "last_recalled_at IS NULL" in sql:
            return "old_never_recalled"
        if "last_recalled_at < " in sql:
            return "old_recalled_long_ago"
        if "GROUP BY subject HAVING" in sql.replace("\n", " "):
            return "above_cap"
        if "d.created_at >" in sql:
            return "proc_in_window"
        if "AND b.function IS NOT NULL" in sql and "d.created_at" not in sql:
            return "proc_total_eligible"
        return "unknown"

    fetchval_responses = {
        "oldest_days": oldest_days,
        "old_never_recalled": old_never_recalled,
        "old_recalled_long_ago": old_recalled_long_ago,
        "above_cap": above_cap,
        "proc_in_window": proc_in_window,
        "proc_total_eligible": proc_total_eligible,
    }

    async def _fetchval(sql, *args):
        return fetchval_responses.get(_classify(sql), 0)

    async def _fetch(sql, *args):
        if "GROUP BY subject" in sql:
            return cluster_rows or []
        return candidate_rows or []

    conn.fetchval = AsyncMock(side_effect=_fetchval)
    conn.fetch = AsyncMock(side_effect=_fetch)
    return conn


# ---------------------------------------------------------------------------
# stale_scan verdict logic
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stale_scan_yellow_when_corpus_too_young():
    """Corpus oldest fact < age_days → YELLOW, not RED. Filter is
    structurally untestable on too-young corpora."""
    conn = _make_conn(oldest_days=10)  # younger than 60-day threshold
    result = await check_stale_scan(conn, "test-agent", 60, ["rule"])
    assert result.verdict == "YELLOW"


@pytest.mark.asyncio
async def test_stale_scan_green_when_old_facts_all_recently_recalled():
    """The first-run live verdict bug: corpus has facts older than
    age_days but all have recent recall, so genuinely 0 are stale.
    This is HEALTHY agent housekeeping, NOT a filter regression."""
    conn = _make_conn(
        oldest_days=100,
        candidate_rows=[],  # filter selects 0
        old_never_recalled=0,  # no stale-by-never-recalled
        old_recalled_long_ago=0,  # no stale-by-long-ago-recall
    )
    result = await check_stale_scan(conn, "test-agent", 60, ["rule"])
    assert result.verdict == "GREEN", (
        "0 candidates AND 0 truly-stale facts = healthy, not regression"
    )


@pytest.mark.asyncio
async def test_stale_scan_red_when_stale_facts_exist_but_filter_selects_zero():
    """The actual regression pattern: stale facts exist but filter
    catches none — same shape as the original
    `superseded_by IS NOT NULL` impossibility."""
    conn = _make_conn(
        oldest_days=100,
        candidate_rows=[],  # filter selects 0
        old_never_recalled=12,  # but stale facts DO exist
        old_recalled_long_ago=5,
    )
    result = await check_stale_scan(conn, "test-agent", 60, ["rule"])
    assert result.verdict == "RED"
    assert "17 stale facts exist" in result.verdict_reason or "12" in result.verdict_reason


def test_stale_scan_primary_query_uses_null_aware_exclusion():
    """Codex P1 on PR #408: the primary candidate query and the
    fallback truly_stale counts must apply the same NULL-aware
    category exclusion. Without symmetry, NULL-category stale facts
    are excluded from the primary count (because `NOT IN (...)`
    evaluates UNKNOWN on NULL) but included in fallback truly_stale,
    causing a false RED verdict.
    """
    import inspect

    from nous_eval.probes import sleep_filter_dryrun

    src = inspect.getsource(sleep_filter_dryrun.check_stale_scan)
    # The primary excluded_clause must match the fallback excl_clause:
    # both should include `IS NULL OR category NOT IN`.
    assert src.count("category IS NULL OR category NOT IN") >= 2, (
        "primary candidate query and fallback truly_stale queries "
        "must both use NULL-aware category exclusion"
    )


@pytest.mark.asyncio
async def test_stale_scan_green_when_filter_selects_candidates():
    conn = _make_conn(
        oldest_days=100,
        candidate_rows=[
            {"id": "x", "content": "stale fact 1", "category": "concept",
             "created_at": "2025-01-01"},
        ],
    )
    result = await check_stale_scan(conn, "test-agent", 60, ["rule"])
    assert result.verdict == "GREEN"
    assert result.candidate_count == 1


# ---------------------------------------------------------------------------
# cluster_consolidation verdict logic
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cluster_consolidation_red_when_only_above_cap_subjects():
    """Bug pattern: subjects exist but all are accumulating mega-subjects
    above the cap. Filter selects nothing despite data — regression."""
    conn = _make_conn(
        cluster_rows=[],  # 0 in [3, 10] range
        above_cap=2,  # but 2 subjects > 10 facts (the lesson_learned/Tim case)
    )
    result = await check_cluster_consolidation(conn, "test-agent", 3, 10)
    assert result.verdict == "RED"


@pytest.mark.asyncio
async def test_cluster_consolidation_green_with_eligible_clusters():
    conn = _make_conn(
        cluster_rows=[
            {"subject": "subject_a", "cnt": 5},
            {"subject": "subject_b", "cnt": 4},
        ],
        above_cap=1,
    )
    result = await check_cluster_consolidation(conn, "test-agent", 3, 10)
    assert result.verdict == "GREEN"
    assert result.candidate_count == 2


@pytest.mark.asyncio
async def test_cluster_consolidation_no_data_when_corpus_empty():
    conn = _make_conn(cluster_rows=[], above_cap=0)
    result = await check_cluster_consolidation(conn, "test-agent", 3, 10)
    assert result.verdict == "NO_DATA"


# ---------------------------------------------------------------------------
# procedure_recency verdict logic
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_procedure_recency_red_when_pct_below_5():
    """The original bug: 7-day window matched ~3 of 200 = 1.5% — below
    the 5% threshold the probe RED-flags."""
    conn = _make_conn(proc_in_window=3, proc_total_eligible=200)
    result = await check_procedure_recency(conn, "test-agent", 7)
    assert result.verdict == "RED"
    assert "1.5%" in result.verdict_reason


@pytest.mark.asyncio
async def test_procedure_recency_green_when_pct_above_5():
    """The fix: 30-day window matches ~71 of 200 = 35.5% → GREEN."""
    conn = _make_conn(proc_in_window=71, proc_total_eligible=200)
    result = await check_procedure_recency(conn, "test-agent", 30)
    assert result.verdict == "GREEN"


@pytest.mark.asyncio
async def test_procedure_recency_no_data_when_eligible_set_empty():
    conn = _make_conn(proc_in_window=0, proc_total_eligible=0)
    result = await check_procedure_recency(conn, "test-agent", 30)
    assert result.verdict == "NO_DATA"


# ---------------------------------------------------------------------------
# overall_exit_code
# ---------------------------------------------------------------------------


def test_overall_exit_code_red_blocks_strict():
    results = [
        FilterResult(name="a", candidate_count=5, verdict="GREEN"),
        FilterResult(name="b", candidate_count=0, verdict="RED"),
    ]
    assert overall_exit_code(results) == 1


def test_overall_exit_code_yellow_no_data_dont_block():
    results = [
        FilterResult(name="a", candidate_count=0, verdict="YELLOW"),
        FilterResult(name="b", candidate_count=0, verdict="NO_DATA"),
        FilterResult(name="c", candidate_count=5, verdict="GREEN"),
    ]
    assert overall_exit_code(results) == 0
