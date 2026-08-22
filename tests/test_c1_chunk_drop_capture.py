"""C1: chunk-leg drop capture — the RRF discard set becomes visible to F091.

`hybrid_search` fetches `limit_expanded` (up to 3x) per leg and returns at most
`limit`, so most of what it retrieves dies in memory, unreported. Before C1 the
chunk leg's losses read identically to "never retrieved at all" — which made
"the merge cut it" and "the embedding could not reach it" the same bucket, and
those have different remedies.

Telemetry only: no served byte moves. Test 1 is the blocking gate for that, and
it has to be, because the committed `recall_deep` snapshot canNOT catch a
regression here — `_make_settings()` defaults `episode_chunks_enabled=False`,
so the chunk leg never runs in it, and it passes no trace, so every capture
site is a no-op. A green snapshot is not evidence for this change.
"""

from __future__ import annotations

import contextlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

import nous.heart.search as search_mod
from nous.api.retrieval_pipeline import (
    _CHUNK_DROP_DISPOSITIONS,
    _search_episode_chunks,
)
from nous.observability.retrieval_trace import (
    FILTER_DROPPED,
    SLICED_OFF,
    NULL_TRACE,
    RetrievalTrace,
)


# ---------------------------------------------------------------------------
# Fixtures — fake session, real `hybrid_search` body.
#
# Half the existing chunk coverage monkeypatches `hybrid_search` ITSELF
# (test_r2_chunk_hybrid.py:104/138/153) or `_rrf_merge` (test_rrf_search.py:238),
# and neither can exercise a capture that lives inside the function they
# replace. Faking only the SQL session is what runs the real merge.
# ---------------------------------------------------------------------------

def _ids(n: int) -> list[UUID]:
    return [UUID(int=i + 1) for i in range(n)]


def _leg_rows(ids_scores):
    """Rows shaped like the SQL cursor: `.id` / `.score`."""
    return [SimpleNamespace(id=i, score=s) for i, s in ids_scores]


class _SplitSess:
    """Returns different rows to the vector leg and the keyword leg.

    Dispatches on the SQL text the same way `_heart_shim` does — the vector
    query orders by the embedding operator, the keyword query calls ts_rank_cd.
    """

    def __init__(self, vector, keyword, content_ids=None):
        self.vector = vector
        self.keyword = keyword
        self.content_ids = content_ids

    async def execute(self, sql, params=None):
        text = str(sql)
        if "ts_rank_cd" in text:
            rows = _leg_rows(self.keyword)
        elif "embedding <=>" in text and "SELECT id, content" not in text:
            rows = _leg_rows(self.vector)
        else:
            # batch content fetch: (id, content, episode_id)
            wanted = self.content_ids
            ids = params["ids"] if params else []
            rows = [
                (cid, f"body-{cid.int}", UUID(int=999))
                for cid in ids
                if wanted is None or cid in wanted
            ]
        return MagicMock(all=lambda: rows)


def _heart(sess, embedder=True):
    db = MagicMock()

    @contextlib.asynccontextmanager
    async def _ctx():
        yield sess

    db.session = _ctx
    emb = MagicMock()
    emb.embed = AsyncMock(return_value=[0.1] * 4)
    return SimpleNamespace(
        db=db, _embeddings=emb if embedder else None, agent_id="a",
    )


def _settings(**over):
    base = dict(chunk_hybrid_search_enabled=True, chunk_rrf_penalty_limit=None)
    base.update(over)
    return SimpleNamespace(**base)


# ---------------------------------------------------------------------------
# Test 1 (BLOCKING) — the served set does not move.
# ---------------------------------------------------------------------------

class TestServedSetUnchanged:
    """The one gate that must never go red. `limit` and `penalty_limit` are
    test parameters, so `limit_expanded = max(fetch_base*3, limit)` is 15 at
    limit=5 — 20 canned rows is a complete over-window fixture. The eval
    config's 90 is an operating point, not a test requirement.
    """

    @pytest.mark.asyncio
    async def test_identical_with_and_without_sink(self):
        ids = _ids(20)
        vector = [(i, 0.9 - 0.01 * n) for n, i in enumerate(ids)]
        keyword = [(i, 0.5 - 0.01 * n) for n, i in enumerate(reversed(ids))]

        async def run(sink):
            return await _search_episode_chunks(
                heart=_heart(_SplitSess(vector, keyword)),
                query="q", agent_id="a", limit=5,
                settings=_settings(), dropped_out=sink,
            )

        without = await run(None)
        sink: list = []
        with_sink = await run(sink)

        assert without == with_sink, "capture changed what the caller is served"
        assert len(without) == 5
        assert sink, "sink supplied but nothing captured — the instrument reads zero"

    @pytest.mark.asyncio
    async def test_identical_on_the_keyword_only_exit(self):
        """Same guarantee on the no-embedder branch, which R1 added."""
        ids = _ids(20)
        keyword = [(i, 0.5 - 0.01 * n) for n, i in enumerate(ids)]

        async def run(sink):
            return await search_mod.hybrid_search(
                _SplitSess([], keyword), "heart.episode_chunks",
                None, "q", "a", limit=5, active_filter=False, dropped_out=sink,
            )

        without = await run(None)
        sink: list = []
        assert without == await run(sink)
        assert len(sink) == 15


# ---------------------------------------------------------------------------
# Test 2 — the discard set is exactly the complement, per-tuple.
#
# Set-equality on ids alone is near-tautological here (merged IS the union, and
# served is its prefix), and would pass for 0-based ranks, swapped legs, a
# `min` best-score, or duplicate entries. So assert the tuples.
# ---------------------------------------------------------------------------

class TestComplementIdentity:
    @pytest.mark.asyncio
    async def test_tuples_for_vector_only_keyword_only_and_overlap(self):
        v_only, k_only, both = _ids(6)[:2], _ids(6)[2:4], _ids(6)[4:]
        # Deliberately DIFFERENT per-leg ranks for the overlap ids, so a
        # swapped-legs implementation cannot pass.
        vector = [(v_only[0], 0.90), (both[0], 0.80),
                  (v_only[1], 0.70), (both[1], 0.60)]
        keyword = [(both[1], 0.40), (k_only[0], 0.30),
                   (both[0], 0.20), (k_only[1], 0.10)]

        sink: list = []
        merged = await search_mod.hybrid_search(
            _SplitSess(vector, keyword), "heart.episode_chunks",
            [0.1] * 4, "q", "a", limit=2, active_filter=False, dropped_out=sink,
        )

        served = {d for d, _ in merged}
        union = {d for d, _ in vector} | {d for d, _ in keyword}
        assert {e[0] for e in sink} == union - served
        # No duplicates: assert on the RAW list, since add/drop are first-wins
        # and would silently dedupe a double-emit if asserted through the trace.
        assert len(sink) == len(union - served)

        by_id = {e[0]: e for e in sink}

        # vector-only id: 1-based vector rank, no keyword rank.
        if v_only[1] in by_id:
            e = by_id[v_only[1]]
            assert e[1] == 3 and e[2] is None
            assert e[3] == pytest.approx(0.70)
            assert e[4] == "rrf_merge"

        # keyword-only id: no vector rank, 1-based keyword rank.
        if k_only[1] in by_id:
            e = by_id[k_only[1]]
            assert e[1] is None and e[2] == 4
            assert e[3] == pytest.approx(0.10)

        # overlap id: BOTH ranks, and they must differ (catches leg swap);
        # best_leg_score is the MAX, not the min.
        for oid in both:
            if oid in by_id:
                e = by_id[oid]
                assert e[1] is not None and e[2] is not None
                v = dict(vector)[oid]
                k = dict(keyword)[oid]
                assert e[3] == pytest.approx(max(v, k))
                assert e[3] != pytest.approx(min(v, k)), "best_leg_score is min"

    @pytest.mark.asyncio
    async def test_ranks_are_one_based(self):
        """1-based matches every other rank in the trace. A 0-based
        implementation passes set-equality unchanged, so pin it directly."""
        ids = _ids(10)
        vector = [(i, 1.0 - 0.01 * n) for n, i in enumerate(ids)]
        sink: list = []
        await search_mod.hybrid_search(
            _SplitSess(vector, []), "heart.episode_chunks",
            [0.1] * 4, "q", "a", limit=1, active_filter=False, dropped_out=sink,
        )
        assert min(e[1] for e in sink) >= 1, "ranks must be 1-based"
        # ids[0] is served (rank 1), so the first DROPPED is rank 2.
        assert sorted(e[1] for e in sink) == list(range(2, 11))


# ---------------------------------------------------------------------------
# Test 3 — hybrid off is DECLARED, not silent.
# ---------------------------------------------------------------------------

class TestVectorOnlyDeclaresItsZero:
    @pytest.mark.asyncio
    async def test_skip_reason_set_and_leg_not_marked_attempted(self):
        """An empty discard list on this branch is CORRECT — the cut is pushed
        into SQL. But empty reads as "nothing was dropped", so the reason must
        be recorded. Assert the reason, not the emptiness.

        `attempted=False` is load-bearing: `leg()` defaults it True and ORs it
        stickily, and the no-embedder path returns without querying at all.
        """
        tr = RetrievalTrace(capture_candidates=True)
        sink: list = []
        rows = [(UUID(int=1), "body", 0.9, UUID(int=9))]

        class _VecSess:
            async def execute(self, sql, params=None):
                return MagicMock(all=lambda: rows)

        out = await _search_episode_chunks(
            heart=_heart(_VecSess()), query="q", agent_id="a", limit=5,
            settings=_settings(chunk_hybrid_search_enabled=False),
            dropped_out=sink,
        )
        assert out and sink == []

        tr.leg("chunk", attempted=False,
               skip_reason="vector-only path: cut pushed into SQL, no in-memory surplus")
        leg = tr.to_dict()["legs"][0]
        assert leg["skip_reason"], "the zero must be declared, not implied"
        assert leg["attempted"] is False


# ---------------------------------------------------------------------------
# Test 4 — the keyword-only exit (R1) is covered and carries usable ranks.
# ---------------------------------------------------------------------------

class TestKeywordOnlyExit:
    @pytest.mark.asyncio
    async def test_captures_with_keyword_ranks_not_none(self):
        """`vector_results` is [] by construction here (the vector query sits
        inside `if embedding is not None`), so a capture keyed only on the
        vector rank would record entry_rank=None for EVERY row this exit
        exists to report — the defect R1 was written to prevent, re-entering
        via the rank field."""
        ids = _ids(12)
        keyword = [(i, 0.9 - 0.01 * n) for n, i in enumerate(ids)]
        sink: list = []

        await search_mod.hybrid_search(
            _SplitSess([], keyword), "heart.episode_chunks",
            None, "q", "a", limit=4, active_filter=False, dropped_out=sink,
        )

        assert len(sink) == 8
        assert all(e[1] is None for e in sink), "no vector leg ran"
        assert all(e[2] is not None for e in sink), "keyword rank must survive"
        assert {e[4] for e in sink} == {"keyword_only_limit"}
        assert sorted(e[2] for e in sink) == list(range(5, 13))


# ---------------------------------------------------------------------------
# Test 5 — require_keyword_hit attributes the gate that actually fired.
# ---------------------------------------------------------------------------

class TestRequireKeywordHitAttribution:
    @pytest.mark.asyncio
    async def test_filter_and_truncation_are_distinct_and_not_the_merge(self):
        """On this branch `merge_limit` is inflated past the candidate count,
        so `_rrf_merge` slices NOTHING. Attributing these to `rrf_merge` would
        name a gate that never fired.

        No chunk caller sets this flag, so the test lives at helper level —
        a chunk-level version would be fiction.
        """
        v_only, both = _ids(6)[:3], _ids(6)[3:]
        vector = [(i, 0.9) for i in v_only + both]
        keyword = [(i, 0.5) for i in both]
        sink: list = []

        out = await search_mod.hybrid_search(
            _SplitSess(vector, keyword), "heart.episode_chunks",
            [0.1] * 4, "q", "a", limit=1, active_filter=False,
            require_keyword_hit=True, dropped_out=sink,
        )

        assert len(out) == 1
        stages = {e[0]: e[4] for e in sink}
        assert "rrf_merge" not in set(stages.values()), (
            "the merge did not slice on this branch"
        )
        for i in v_only:
            assert stages[i] == "keyword_filter"
        # Exactly one of `both` survived; the rest fell to the post-filter cut.
        assert sorted(stages.values()).count("keyword_filter_limit") == len(both) - 1

    @pytest.mark.asyncio
    async def test_no_row_is_emitted_twice(self):
        """Two gates on one branch: the second must be scoped to the first's
        survivors, or every filter casualty is emitted again under the
        truncation stage and both counts double."""
        vector = [(i, 0.9) for i in _ids(8)]
        keyword = [(i, 0.5) for i in _ids(8)[4:]]
        sink: list = []
        await search_mod.hybrid_search(
            _SplitSess(vector, keyword), "heart.episode_chunks",
            [0.1] * 4, "q", "a", limit=1, active_filter=False,
            require_keyword_hit=True, dropped_out=sink,
        )
        assert len(sink) == len({e[0] for e in sink}), "double-counted a drop"


# ---------------------------------------------------------------------------
# Test 6 — cap ordering. Presence alone proves nothing; provenance does.
# ---------------------------------------------------------------------------

class TestCapOrdering:
    def test_finalize_backstop_makes_presence_a_non_discriminator(self):
        """Guard the guard. `finalize` force-creates a Candidate for anything
        in `results` that the cap excluded, so a wrong implementation (drops
        registered ahead of survivors) still shows every served chunk as
        present and `rendered`. The discriminator is `entry_leg` + snippet.
        """
        served = [SimpleNamespace(id=UUID(int=1), type="chunk",
                                  score=0.9, description="kept")]
        tr = RetrievalTrace(max_candidates=1, capture_candidates=True)
        # WRONG ORDER: a loser claims the only slot first.
        tr.add(UUID(int=2), "chunk", "chunk", score=0.1)
        tr.drop(UUID(int=2), "chunk", SLICED_OFF, "chunk_rrf_merge")
        tr.finalize(served)

        cands = {c["id"]: c for c in tr.to_dict()["candidates"]}
        # Presence passes in the WRONG arm — which is why test 6 asserts more.
        assert str(UUID(int=1)) in cands
        assert cands[str(UUID(int=1))]["disposition"] == "rendered"
        # Provenance is what actually fails.
        assert cands[str(UUID(int=1))]["entry_leg"] == "(unrecorded — capture cap reached)"
        assert cands[str(UUID(int=1))]["snippet"] == ""

    def test_right_order_keeps_leg_and_snippet(self):
        """The correct arm: survivors registered by assembly's
        `_tr_entries("chunk", ...)` before the drain, so they keep both."""
        served = [SimpleNamespace(id=UUID(int=1), type="chunk",
                                  score=0.9, description="kept")]
        tr = RetrievalTrace(max_candidates=1, capture_candidates=True)
        tr.add(UUID(int=1), "chunk", "chunk", score=0.9, rank=1, content="kept")
        tr.add(UUID(int=2), "chunk", "chunk", score=0.1)  # capped out
        tr.finalize(served)

        c = {x["id"]: x for x in tr.to_dict()["candidates"]}[str(UUID(int=1))]
        assert c["entry_leg"] == "chunk"
        assert c["snippet"] == "kept"


# ---------------------------------------------------------------------------
# Invariants that keep the instrument honest.
# ---------------------------------------------------------------------------

class TestStageMapCompleteness:
    def test_every_producer_stage_has_a_disposition(self):
        """A stage added in search.py without a mapping must fail the build,
        not degrade silently to `unaccounted` at runtime."""
        assert search_mod.HYBRID_DROP_STAGES == set(_CHUNK_DROP_DISPOSITIONS), (
            "HYBRID_DROP_STAGES and _CHUNK_DROP_DISPOSITIONS have drifted"
        )

    def test_dispositions_are_the_real_constants(self):
        assert _CHUNK_DROP_DISPOSITIONS["rrf_merge"] == SLICED_OFF
        assert _CHUNK_DROP_DISPOSITIONS["keyword_filter"] == FILTER_DROPPED


class TestLegCountIsExactAndUnsampled:
    def test_n_dropped_recorded_even_when_candidates_are_not(self):
        """The split is the whole point: `n_dropped` answers "how many" on
        every row, while the candidate array answers "which ones" on a sample.
        If `n_dropped` were gated on capture too, the count would vanish on
        ~90% of retrievals and an empty array would be indistinguishable from
        a leg that dropped nothing."""
        tr = RetrievalTrace(capture_candidates=False)
        tr.leg("chunk", attempted=True, n_dropped=150)
        tr.add(uuid4(), "chunk", "chunk", score=0.5)

        d = tr.to_dict()
        assert d["candidates"] is None, "unsampled row must not fake an array"
        assert d["legs"][0]["n_dropped"] == 150, "the count must survive sampling"


class TestDrainAtAssembly:
    """The pipeline-level half: WHEN the buffered discard set is registered,
    and what it must refuse to claim."""

    @staticmethod
    def _pipeline_bits(chunk_rows, dropped, *, chunk_raises=False):
        from nous.api.retrieval_pipeline import run_recall_pipeline

        heart = MagicMock()
        heart.recall = AsyncMock(return_value=[])
        heart.agent_id = "a"
        brain = MagicMock()
        brain.neighbors = AsyncMock(return_value=[])
        brain.query = AsyncMock(return_value=[])
        brain._query = AsyncMock(return_value=[])
        brain.get_contradictions = AsyncMock(return_value=[])

        async def _fake_chunks(**kw):
            sink = kw.get("dropped_out")
            if sink is not None:
                sink.extend(dropped)
            if chunk_raises:
                # Mirrors the real ordering: hybrid_search fills the sink, THEN
                # the content fetch runs and may blow up.
                raise RuntimeError("content fetch exploded")
            return chunk_rows

        settings = SimpleNamespace(
            graph_recall_enabled=False, cross_type_linking_enabled=False,
            spreading_activation_enabled="false", contradiction_detection=False,
            graph_recall_decay=0.7, graph_recall_max_expand=5,
            graph_recall_max_neighbors=3, heart_graph_all_types_enabled=False,
            heart_graph_neighbors_per_seed=3, episode_chunks_enabled=True,
            episode_chunk_recall_limit=5, coherent_ranking_enabled=False,
            chunk_hybrid_search_enabled=True, chunk_rrf_penalty_limit=None,
        )
        return run_recall_pipeline, heart, brain, settings, _fake_chunks

    @pytest.mark.asyncio
    async def test_exception_clears_the_buffer(self, monkeypatch):
        """F2: the sink is filled INSIDE hybrid_search, before the content
        fetch. If that fetch raises, a stale buffer would report ~150
        candidates as `sliced_off@chunk_rrf_merge` for a retrieval that
        actually died on a DB error — filed under a DIFFERENT leg name. An
        operator reading the disposition histogram would raise the chunk
        allotment to fix a fetch failure. A bound that fabricates a confident
        wrong reading is worse than no bound.
        """
        dropped = [(UUID(int=n), n, None, 0.5, "rrf_merge") for n in range(2, 12)]
        run, heart, brain, settings, fake = self._pipeline_bits(
            [], dropped, chunk_raises=True,
        )
        monkeypatch.setattr(
            "nous.api.retrieval_pipeline._search_episode_chunks", fake,
        )
        tr = RetrievalTrace(capture_candidates=True)
        await run(query="q", heart=heart, brain=brain, settings=settings,
                  limit=5, memory_types=["all"], trace=tr)

        d = tr.to_dict()
        assert not [c for c in d["candidates"] if c["type"] == "chunk"], (
            "a failed fetch was reported as a merge cut"
        )
        chunk_legs = [lg for lg in d["legs"] if lg["name"] == "chunk"]
        assert all(lg["n_dropped"] == 0 for lg in chunk_legs)

    @pytest.mark.asyncio
    async def test_already_served_ids_are_not_dropped(self, monkeypatch):
        """Live under NOUS_HEART_GRAPH_ALL_TYPES_ENABLED=true: Stage 2b can
        surface a chunk the merge cut, so it reaches the model by another
        road. Dropping it here would make `finalize` override to `rendered`
        with `restored_from="sliced_off@chunk_rrf_merge"` — a rescue badge
        naming a leg the item never came back through. The cut still happened
        and `n_dropped` still counts it; only the misattribution is suppressed.
        """
        served_id = UUID(int=1)
        chunk_rows = [(served_id, "body", 0.9, UUID(int=99))]
        # The SAME id appears in the discard set (cut by the merge) and in the
        # served rows (re-surfaced by another leg).
        dropped = [(served_id, 7, None, 0.4, "rrf_merge"),
                   (UUID(int=2), 8, None, 0.3, "rrf_merge")]
        run, heart, brain, settings, fake = self._pipeline_bits(chunk_rows, dropped)
        monkeypatch.setattr(
            "nous.api.retrieval_pipeline._search_episode_chunks", fake,
        )
        tr = RetrievalTrace(capture_candidates=True)
        await run(query="q", heart=heart, brain=brain, settings=settings,
                  limit=5, memory_types=["all"], trace=tr)

        by_id = {c["id"]: c for c in tr.to_dict()["candidates"]}
        survivor = by_id[str(served_id)]
        assert survivor["disposition"] == "rendered"
        assert survivor["restored_from"] is None, (
            "false rescue badge: item entered by another leg, not a merge rescue"
        )
        assert by_id[str(UUID(int=2))]["disposition"] == SLICED_OFF
        # The count is unaffected — both were genuinely cut by the merge.
        leg = [lg for lg in tr.to_dict()["legs"] if lg["name"] == "chunk"][0]
        assert leg["n_dropped"] == 2

    @pytest.mark.asyncio
    async def test_survivors_registered_before_the_drain(self, monkeypatch):
        """R2/T1: the drain runs after assembly, so served chunks already hold
        their slots WITH snippets. Provenance is the only discriminator — see
        TestCapOrdering for why presence is not."""
        rows = [(UUID(int=1), "the served body", 0.9, UUID(int=99))]
        dropped = [(UUID(int=n), n, None, 0.2, "rrf_merge") for n in range(2, 9)]
        run, heart, brain, settings, fake = self._pipeline_bits(rows, dropped)
        monkeypatch.setattr(
            "nous.api.retrieval_pipeline._search_episode_chunks", fake,
        )
        tr = RetrievalTrace(capture_candidates=True)
        await run(query="q", heart=heart, brain=brain, settings=settings,
                  limit=5, memory_types=["all"], trace=tr)

        c = {x["id"]: x for x in tr.to_dict()["candidates"]}[str(UUID(int=1))]
        assert c["entry_leg"] == "chunk"
        assert c["snippet"] == "the served body", "first-wins ate the snippet"


class TestCapturingGate:
    def test_capturing_mirrors_sampling_and_null_trace_agrees(self):
        """Producers gate the expensive complement build on `capturing`, not
        `enabled` — every real trace is enabled, but capture is sampled."""
        assert RetrievalTrace(capture_candidates=True).capturing is True
        assert RetrievalTrace(capture_candidates=False).capturing is False
        assert RetrievalTrace(capture_candidates=False).enabled is True
        assert NULL_TRACE.capturing is False
        assert NULL_TRACE.enabled is False
