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
    homeostatic_downscale,
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
    """The four LIVE similarity-linker upsert sites carry the F044-STC-HOOK.

    The live re-derivation sources are: brain._auto_link, graph_linker.create_edge,
    and the two FactGraphLinker upserts (link_fact_to_decisions /
    link_fact_to_facts) — the latter two own the ON CONFLICT path for every
    ``fact_learned`` event and bypass create_edge() (codex P1b, PR #531).
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
    assert len(hits) == 4, f"expected 4 STC hook sites, found {len(hits)}: {hits}"
    files = {h.split(":")[0] for h in hits}
    assert files == {"brain.py", "graph_linker.py"}, files
    # The two fact-linker upserts must both be hooked (P1b regression lock).
    gl_hits = sum(1 for h in hits if h.startswith("graph_linker.py"))
    assert gl_hits == 3, f"expected 3 hooks in graph_linker.py, found {gl_hits}"


def test_linker_commit_gates_are_f044_aware():
    """Every live-linker handler commits reinforcement-only sessions (codex P1a).

    A re-derivation can increment LTP counters without creating any new edge.
    If the handler still commits only when ``edges_created > 0`` / ``if all_edges``,
    those increments roll back. Each of the four handlers must therefore OR the
    F044 flag into its commit gate. Source-pinned (the same discipline as the
    hook-site guard) so a regression to ``if edges_created > 0:`` trips CI.
    """
    handlers_dir = Path(__file__).resolve().parent.parent / "nous" / "handlers"
    expected = {
        "fact_graph_linker.py",
        "decision_graph_linker.py",
        "procedure_graph_linker.py",
        "episode_summarizer.py",
    }
    for name in expected:
        text_src = (handlers_dir / name).read_text(encoding="utf-8")
        assert "tinyhippo_lite_enabled" in text_src and "await" in text_src, (
            f"{name} commit gate is not F044-aware (P1a regression)"
        )


# ---------------------------------------------------------------------------
# Postgres-lane: gate, telemetry, increment
# ---------------------------------------------------------------------------

async def _insert_edge(
    session, *, ltp=0, state="tagged", relation="related_to",
    extraction_method="heuristic", weight=0.9,
):
    src, tgt = uuid4(), uuid4()
    await session.execute(
        text(
            """
            INSERT INTO brain.graph_edges
                (source_id, target_id, source_type, target_type, agent_id,
                 relation, weight, auto_linked, extraction_method,
                 consolidation_state, ltp_count)
            VALUES (:s, :t, 'decision', 'decision', :a, :rel, :w, true,
                    :em, :state, :ltp)
            """
        ),
        {"s": src, "t": tgt, "a": _AGENT, "rel": relation, "w": weight,
         "em": extraction_method, "state": state, "ltp": ltp},
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
    ], _AGENT)
    n_distinct = await flush_recall_touches(session, _AGENT)
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


# ---------------------------------------------------------------------------
# Codex round-2 (HEAD 0a150bb) findings — telemetry-only contract + hardening
# ---------------------------------------------------------------------------

def test_alpha_validator_rejects_out_of_band():
    """tinyhippo_alpha must be a (0.0, 1.0] decay factor (codex P2).

    A typo like 75 or a negative value must fail config init rather than
    silently corrupting every tagged edge weight on the next downscale.
    Experiment values down to 0.42 stay valid.
    """
    from pydantic import ValidationError

    from nous.config import Settings

    for bad in (75.0, 0.0, -0.5, 1.5):
        with pytest.raises(ValidationError):
            Settings(NOUS_TINYHIPPO_ALPHA=bad)
    # In-band values are accepted (default + documented band + experiment low).
    for ok in (0.42, 0.5, 0.75, 0.9, 1.0):
        assert Settings(NOUS_TINYHIPPO_ALPHA=ok).tinyhippo_alpha == ok


def test_consolidated_boost_is_opt_in_not_master_flag():
    """The consolidated ranking boost must NOT ride on the master flag (codex P1).

    Default-off so tinyhippo_lite_enabled ALONE (shadow/telemetry mode) leaves
    recall ranking byte-identical even when graph_adjacency_boost_enabled is on.
    Source-pinned: the retrieval gate must AND the dedicated boost flag.
    """
    from nous.config import Settings

    assert Settings().tinyhippo_consolidated_boost_enabled is False
    # Flipping only the master flag does not enable the boost.
    s = Settings(NOUS_TINYHIPPO_LITE_ENABLED=True)
    assert s.tinyhippo_consolidated_boost_enabled is False

    pipeline_src = (
        Path(__file__).resolve().parent.parent
        / "nous" / "api" / "retrieval_pipeline.py"
    ).read_text(encoding="utf-8")
    assert "tinyhippo_consolidated_boost_enabled" in pipeline_src, (
        "consolidated boost gate must reference the opt-in flag (P1 regression)"
    )


def test_recall_reactivation_excludes_deterministic_edges():
    """The recall-touch reactivation query must skip structural edges.

    All three F044 reinforcement touchpoints (write-side LTP hook, sleep
    downscale, recall-touch) must operate on the same eligible set — the
    associative tier, never the deterministic/structural one. Source-pinned so
    the recall-touch read can't silently drop the exemption and start promoting
    F070 part_of anchors (codex round-5 P2).
    """
    pipeline_src = (
        Path(__file__).resolve().parent.parent
        / "nous" / "api" / "retrieval_pipeline.py"
    ).read_text(encoding="utf-8")
    reactivation = pipeline_src.split("_record_recall_reactivation", 2)[2]
    body = reactivation.split("async def ", 1)[0]
    assert "extraction_method IS DISTINCT FROM 'deterministic'" in body, (
        "recall reactivation read must exclude the deterministic tier"
    )


@pytest.mark.postgres_only
async def test_flush_preserves_touch_recorded_during_writes(session):
    """A recall that records a touch mid-flush must survive (codex P2 race).

    flush snapshots the buffer, awaits DB writes, then must NOT blanket-clear —
    a touch recorded during those awaits is not in the snapshot and has to
    remain for the next flush. Simulated via a session whose execute() appends a
    concurrent touch.
    """
    _RECALL_TOUCH_BUFFER.clear()
    a, b = await _insert_edge(session, ltp=0, relation="related_to")
    concurrent = ("concurrent-src", "concurrent-tgt", "related_to")
    concurrent_key = (_AGENT, *concurrent)
    record_recall_touches([(str(a), str(b), "related_to")], _AGENT)

    class _RacingSession:
        def __init__(self, inner):
            self._inner = inner
            self._fired = False

        async def execute(self, *args, **kwargs):
            if not self._fired:
                self._fired = True
                # A concurrent recall lands while this write is in flight.
                record_recall_touches([concurrent], _AGENT)
            return await self._inner.execute(*args, **kwargs)

    n = await flush_recall_touches(_RacingSession(session), _AGENT)
    assert n == 1  # one snapshotted edge written
    # The concurrent touch survived the flush (was not clobbered by clear()).
    assert _RECALL_TOUCH_BUFFER.get(concurrent_key) == 1
    _RECALL_TOUCH_BUFFER.clear()


@pytest.mark.postgres_only
async def test_flush_is_agent_scoped(session):
    """flush drains only the sleeping agent's touches (codex round-7 P2).

    A multi-agent process must not let agent B's sleep apply/clear agent A's
    buffered recall touches.
    """
    _RECALL_TOUCH_BUFFER.clear()
    a, b = await _insert_edge(session, ltp=0, relation="related_to")
    record_recall_touches([(str(a), str(b), "related_to")], _AGENT)
    record_recall_touches([("other-src", "other-tgt", "related_to")], "other-agent")

    n = await flush_recall_touches(session, _AGENT)
    assert n == 1  # only this agent's edge
    ab = (await session.execute(
        text("SELECT ltp_count FROM brain.graph_edges WHERE source_id=:s AND target_id=:t"),
        {"s": a, "t": b})).scalar()
    assert ab == 1
    # The other agent's touch is untouched, awaiting its own flush.
    assert _RECALL_TOUCH_BUFFER.get(("other-agent", "other-src", "other-tgt", "related_to")) == 1
    _RECALL_TOUCH_BUFFER.clear()


def test_prp_threshold_rejects_non_positive():
    """tinyhippo_prp_threshold must be >= 1 (codex round-3 P2).

    A 0/negative threshold promotes every edge on the first sleep (all edges
    init ltp_count=0), collapsing the experiment and exempting the graph from
    downscale.
    """
    from pydantic import ValidationError

    from nous.config import Settings

    for bad in (0, -1):
        with pytest.raises(ValidationError):
            Settings(NOUS_TINYHIPPO_PRP_THRESHOLD=bad)
    assert Settings(NOUS_TINYHIPPO_PRP_THRESHOLD=1).tinyhippo_prp_threshold == 1


@pytest.mark.postgres_only
async def test_downscale_exempts_deterministic_structural_edges(session):
    """Downscale must skip the deterministic (structural) tier (codex round-3 P2).

    F070 structural anchors never reinforce via the LTP hook, so decaying them
    every sleep walks their weight toward zero. They must be exempt; cosine
    (heuristic) tagged edges still decay.
    """
    h_src, h_tgt = await _insert_edge(
        session, state="tagged", extraction_method="heuristic", weight=0.80
    )
    d_src, d_tgt = await _insert_edge(
        session, state="tagged", extraction_method="deterministic", weight=0.80
    )
    await homeostatic_downscale(session, _AGENT, 0.5)

    h_w = (await session.execute(
        text("SELECT weight FROM brain.graph_edges WHERE source_id=:s AND target_id=:t"),
        {"s": h_src, "t": h_tgt})).scalar()
    d_w = (await session.execute(
        text("SELECT weight FROM brain.graph_edges WHERE source_id=:s AND target_id=:t"),
        {"s": d_src, "t": d_tgt})).scalar()
    assert h_w == pytest.approx(0.40)  # heuristic decayed by alpha
    assert d_w == pytest.approx(0.80)  # deterministic exempt
