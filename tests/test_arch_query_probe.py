"""Tests for nous_eval.probes.arch_query.

The probe itself needs a live eval DB to produce real numbers; these
tests cover the probe-construction logic and gold-matching contract
without booting a database. Mock-based for determinism.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from nous_eval.probes.arch_query import (
    PROBES,
    _TOP3_FLOOR,
    _TOP10_FLOOR,
    ArchProbe,
    _gold_ids,
)


def test_probes_are_well_formed():
    """Every probe must have a non-empty query and at least one gold fragment."""
    assert len(PROBES) >= 6
    for probe in PROBES:
        assert isinstance(probe, ArchProbe)
        assert probe.query.strip()
        assert len(probe.gold_fragments) >= 1
        for frag in probe.gold_fragments:
            assert isinstance(frag, str)
            assert len(frag) >= 5  # not a trivial substring


def test_probes_cover_distinct_query_classes():
    """The 6 probes must exercise different query patterns — feature-prefix,
    concept, system-summary — so the probe catches regressions across
    classes, not just one."""
    queries = [p.query.lower() for p in PROBES]
    # System-summary "tell me about X"
    assert any("tell me about" in q for q in queries), (
        "missing system-summary probe ('tell me about X')"
    )
    # Capability "how does X work" / "how do X"
    assert any(q.startswith("how ") for q in queries), (
        "missing how-does-X-work probe"
    )
    # Concept query
    assert any("what does" in q for q in queries), (
        "missing what-does-X-do probe"
    )


def test_regression_floors_have_headroom():
    """Floors should leave one-scenario headroom over baseline so a
    single flake doesn't break CI. 2026-05-02 baseline: TOP-3 5/6,
    TOP-10 6/6, so floors must be <= 4 and <= 5 respectively."""
    n_probes = len(PROBES)
    assert _TOP3_FLOOR <= n_probes - 1, (
        f"TOP3 floor {_TOP3_FLOOR} leaves no headroom for {n_probes}-probe set"
    )
    assert _TOP10_FLOOR <= n_probes - 1, (
        f"TOP10 floor {_TOP10_FLOOR} leaves no headroom for {n_probes}-probe set"
    )


@pytest.mark.asyncio
async def test_gold_ids_unions_across_fragments():
    """_gold_ids must return the UNION of fact IDs matching ANY fragment,
    not the intersection. A foundational fact may match only one of
    several phrasings of the gold answer."""
    fact_id_a = uuid4()
    fact_id_b = uuid4()
    fact_id_c = uuid4()

    # Mock: fragment "foo" matches A,B; fragment "bar" matches B,C.
    # Union must include {A, B, C}.
    rows_by_fragment = {
        "%foo%": [
            MagicMock(__getitem__=lambda self, k: fact_id_a),
            MagicMock(__getitem__=lambda self, k: fact_id_b),
        ],
        "%bar%": [
            MagicMock(__getitem__=lambda self, k: fact_id_b),
            MagicMock(__getitem__=lambda self, k: fact_id_c),
        ],
    }

    raw_conn = MagicMock()

    async def _fetch(_sql, _agent_id, fragment):
        return rows_by_fragment.get(fragment, [])
    raw_conn.fetch = AsyncMock(side_effect=_fetch)

    ids = await _gold_ids(raw_conn, "test-agent", ("foo", "bar"))
    assert ids == {fact_id_a, fact_id_b, fact_id_c}


@pytest.mark.asyncio
async def test_gold_ids_empty_when_no_fragment_matches():
    raw_conn = MagicMock()
    raw_conn.fetch = AsyncMock(return_value=[])
    ids = await _gold_ids(raw_conn, "test-agent", ("nonexistent-fragment",))
    assert ids == set()
