"""F044 tinyHippo-Lite v1 — STC promotion gate, telemetry, reinforcement hooks.

The Postgres-lane tests exercise the gate/telemetry/increment SQL against a real
``brain.graph_edges`` (the aggregate uses FILTER / now() - interval, which sqlite
cannot run). The drift-guard test is pure-source and runs everywhere.
"""

from __future__ import annotations

import re
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import text

from nous.brain.tinyhippo_lite import (
    _RECALL_TOUCH_BUFFER,
    flush_recall_touches,
    increment_ltp_on_rederivation,
    record_recall_touches,
    stc_promote_and_measure,
)

_AGENT = "f044-test-agent"


# ---------------------------------------------------------------------------
# Drift guard (pure source — no DB). Implements Nous's CI tripwire: a new
# edge-upsert producer that bypasses the LTP counter must force a conscious
# update here rather than silently starving the reinforcement signal.
# ---------------------------------------------------------------------------

def test_stc_hook_site_count_is_pinned():
    """Exactly the two LIVE similarity-linker sites carry the F044-STC-HOOK.

    Deterministic sleep-time rebuilders (graph_densifier raw-SQL builders,
    F070 structural anchors) are intentionally NOT hooked. If this count
    changes, a producer was added/removed — update deliberately and confirm
    it is a live re-derivation source, not a deterministic rebuild.
    """
    brain_dir = Path(__file__).resolve().parent.parent / "nous" / "brain"
    hits = []
    for py in brain_dir.glob("*.py"):
        for i, line in enumerate(py.read_text(encoding="utf-8").splitlines(), 1):
            if "F044-STC-HOOK" in line:
                hits.append(f"{py.name}:{i}")
    assert len(hits) == 2, f"expected 2 STC hook sites, found {len(hits)}: {hits}"
    files = {h.split(":")[0] for h in hits}
    assert files == {"brain.py", "graph_linker.py"}, files


# ---------------------------------------------------------------------------
# Postgres-lane: gate, telemetry, increment
# ---------------------------------------------------------------------------

async def _insert_edge(session, *, ltp=0, state="tagged", relation="related_to"):
    src, tgt = uuid4(), uuid4()
    await session.execute(
        text(
            """
            INSERT INTO brain.graph_edges
                (source_id, target_id, source_type, target_type, agent_id,
                 relation, weight, auto_linked, extraction_method,
                 consolidation_state, ltp_count)
            VALUES (:s, :t, 'decision', 'decision', :a, :rel, 0.9, true,
                    'heuristic', :state, :ltp)
            """
        ),
        {"s": src, "t": tgt, "a": _AGENT, "rel": relation, "state": state, "ltp": ltp},
    )
    return src, tgt


@pytest.mark.postgres_only
async def test_promotion_gate_promotes_at_threshold_and_is_idempotent(session):
    # ltp 0,2 stay tagged; ltp 3,5 promote (PRP=3).
    await _insert_edge(session, ltp=0)
    await _insert_edge(session, ltp=2)
    await _insert_edge(session, ltp=3)
    await _insert_edge(session, ltp=5)

    s1 = await stc_promote_and_measure(session, _AGENT, prp_threshold=3)
    assert s1["f044_promoted"] == 2
    assert s1["f044_n_consolidated"] == 2
    assert s1["f044_n_tagged"] == 2
    assert s1["f044_n_edges"] == 4
    # ltp histogram: >=1 -> {2,3,5}=3 ; >=2 -> {2,3,5}=3 ; >=3 -> {3,5}=2
    assert s1["f044_ltp_ge1"] == 3
    assert s1["f044_ltp_ge3"] == 2

    # Idempotent: re-running promotes nothing new.
    s2 = await stc_promote_and_measure(session, _AGENT, prp_threshold=3)
    assert s2["f044_promoted"] == 0
    assert s2["f044_n_consolidated"] == 2


@pytest.mark.postgres_only
async def test_increment_on_rederivation_bumps_once_per_conflict(session):
    """One conflict event = one increment (no debounce): the instrument check
    Nous asked for before the multi-cycle run."""
    src, tgt = await _insert_edge(session, ltp=0)
    await increment_ltp_on_rederivation(session, src, tgt, "related_to")
    row = (await session.execute(
        text("SELECT ltp_count, last_ltp_at FROM brain.graph_edges "
             "WHERE source_id=:s AND target_id=:t AND relation='related_to'"),
        {"s": src, "t": tgt},
    )).mappings().one()
    assert row["ltp_count"] == 1
    assert row["last_ltp_at"] is not None

    # A second conflict increments again (raw rate, no debounce).
    await increment_ltp_on_rederivation(session, src, tgt, "related_to")
    again = (await session.execute(
        text("SELECT ltp_count FROM brain.graph_edges "
             "WHERE source_id=:s AND target_id=:t AND relation='related_to'"),
        {"s": src, "t": tgt},
    )).scalar()
    assert again == 2


@pytest.mark.postgres_only
async def test_recall_touch_buffer_flushes_to_ltp(session):
    """v1.1: buffered recall reactivations flush to ltp_count by their count,
    then clear. An edge touched twice in the buffer gains +2."""
    _RECALL_TOUCH_BUFFER.clear()
    a, b = await _insert_edge(session, ltp=0, relation="related_to")
    c, d = await _insert_edge(session, ltp=0, relation="related_to")
    record_recall_touches([
        (str(a), str(b), "related_to"),
        (str(a), str(b), "related_to"),
        (str(c), str(d), "related_to"),
    ])
    n_distinct = await flush_recall_touches(session)
    assert n_distinct == 2
    assert len(_RECALL_TOUCH_BUFFER) == 0
    ab = (await session.execute(
        text("SELECT ltp_count FROM brain.graph_edges WHERE source_id=:s AND target_id=:t"),
        {"s": a, "t": b})).scalar()
    cd = (await session.execute(
        text("SELECT ltp_count FROM brain.graph_edges WHERE source_id=:s AND target_id=:t"),
        {"s": c, "t": d})).scalar()
    assert ab == 2
    assert cd == 1


@pytest.mark.postgres_only
async def test_three_rederivations_then_gate_promotes(session):
    """End-to-end: an edge re-derived to the PRP threshold consolidates."""
    src, tgt = await _insert_edge(session, ltp=0)
    for _ in range(3):
        await increment_ltp_on_rederivation(session, src, tgt, "related_to")
    stats = await stc_promote_and_measure(session, _AGENT, prp_threshold=3)
    assert stats["f044_promoted"] == 1
    state = (await session.execute(
        text("SELECT consolidation_state FROM brain.graph_edges "
             "WHERE source_id=:s AND target_id=:t AND relation='related_to'"),
        {"s": src, "t": tgt},
    )).scalar()
    assert state == "consolidated"
