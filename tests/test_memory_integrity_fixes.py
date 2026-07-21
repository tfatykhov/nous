"""Tests for the 2026-06-09 memory-integrity audit fixes.

Covers:
- S3: in-band dupe classification before confirm (_classify_dupe_in_band,
  _apply_band_action) + event_date merge on confirm (_confirm_duplicate)
- E2: working-memory residual merge (upsert_residual_items) + real summaries
  in record_surfaced 4-tuples
- D2/S7: EmbeddingProvider LRU cache (embed + cache-aware embed_batch)
- R1: §14 cosine-leg procedure score floor

S1 (cosine dedup probe) is covered in test_f377_dedup_tiebreaker.py;
E1 (chunk MAX+1 under advisory lock), D1 (edge cleanup), D4/D9 (index-shape
SQL) are exercised against real Postgres in the eval-instance validation.
"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from nous.heart.facts import FactManager
from nous.heart.schemas import FactInput


# ---------------------------------------------------------------------------
# S3: band routing
# ---------------------------------------------------------------------------


def _fm_with_llm(classification: dict | None) -> FactManager:
    fm = FactManager(db=MagicMock(), embeddings=None, agent_id="test")
    fm._llm = object()  # non-None gates the classifier path
    fm._classify_fact_pair = AsyncMock(return_value=classification)
    return fm


def _dupe(content: str = "old fact content here", **kw) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(), content=content, confidence=1.0,
        superseded_by=None, active=True, contradiction_of=None,
        event_date=kw.get("event_date"), event_date_classified_at=None,
        # codex P2 round 9: _confirm_duplicate's stamp check now reads this
        # unconditionally (no longer short-circuited behind `input.entity_keys`
        # truthy) -- a real Fact ORM row always has this attribute (default
        # None), so the test double needs it too.
        entity_keys_extracted_at=kw.get("entity_keys_extracted_at"),
    )


def _input(content: str = "new fact content here", **kw) -> FactInput:
    return FactInput(subject="thing", content=content, source="test", **kw)


class TestClassifyDupeInBand:
    @pytest.mark.asyncio
    async def test_no_llm_returns_none(self):
        fm = FactManager(db=MagicMock(), embeddings=None, agent_id="test")
        assert await fm._classify_dupe_in_band(_dupe(), 0.90, _input(), True) is None

    @pytest.mark.asyncio
    async def test_above_band_true_duplicate_confirms(self):
        """similarity >= 0.95 is a true near-exact duplicate — no classification."""
        fm = _fm_with_llm({"relation": "CONTRADICTION"})
        assert await fm._classify_dupe_in_band(_dupe(), 0.96, _input(), True) is None
        fm._classify_fact_pair.assert_not_called()

    @pytest.mark.asyncio
    async def test_below_old_band_now_classifies(self):
        """1a (2026-06-13 audit): a hit in [threshold, 0.85) reaches the
        classifier here only because the caller (_find_duplicate) returned it
        (similarity >= the operator threshold, e.g. prod 0.80). The old 0.85
        lower bound blind-confirmed this range, swallowing contradictions. Now a
        0.82 CONTRADICTION is classified+routed, not confirmed."""
        fm = _fm_with_llm({"relation": "CONTRADICTION", "confidence": 0.9})
        assert await fm._classify_dupe_in_band(_dupe(), 0.82, _input(), True) == "contradiction"
        fm._classify_fact_pair.assert_called_once()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("relation, current, expected", [
        ("CONTRADICTION", "new", "contradiction"),  # real conflict — routes
        ("UPDATE", "new", "supersede_old"),         # state change — supersedes (codex P1 r2)
        ("UPDATE", "old", None),                    # old is current — confirm (dedup)
        ("UNRELATED", "new", None),                 # paraphrase below band — confirm
        ("REFINEMENT", "new", None),
    ])
    async def test_extended_range_routes_contradiction_and_update_new(self, relation, current, expected):
        """codex P1 (PR #519 r1+r2): in [threshold, 0.85) only a real state-change
        (CONTRADICTION or UPDATE-new supersede) may act; UNRELATED/REFINEMENT/
        UPDATE-old confirm (dedup), preserving aggressive low-threshold dedup."""
        fm = _fm_with_llm({"relation": relation, "current_fact": current, "confidence": 0.9})
        assert await fm._classify_dupe_in_band(_dupe(), 0.82, _input(), True) == expected

    @pytest.mark.asyncio
    async def test_true_band_still_full_routes(self):
        """Inside [0.85, 0.95) full routing is unchanged — a REFINEMENT routes."""
        fm = _fm_with_llm({"relation": "REFINEMENT", "confidence": 0.9})
        assert await fm._classify_dupe_in_band(_dupe(), 0.90, _input(), True) == "refines"

    @pytest.mark.asyncio
    async def test_band_budget_exhaustion_falls_open(self):
        """1a: when the hourly classifier budget is spent, the band falls open
        to confirm (returns None) rather than making the Haiku call."""
        fm = _fm_with_llm({"relation": "CONTRADICTION", "confidence": 0.9})
        fm._band_budget_ok = lambda: False
        assert await fm._classify_dupe_in_band(_dupe(), 0.82, _input(), True) is None
        fm._classify_fact_pair.assert_not_called()

    @pytest.mark.asyncio
    async def test_check_contradictions_false_skips(self):
        fm = _fm_with_llm({"relation": "CONTRADICTION"})
        assert await fm._classify_dupe_in_band(_dupe(), 0.90, _input(), False) is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "classification, expected",
        [
            ({"relation": "UNRELATED", "confidence": 0.9}, "unrelated"),
            ({"relation": "REFINEMENT", "confidence": 0.9}, "refines"),
            ({"relation": "UPDATE", "current_fact": "new", "confidence": 0.9}, "supersede_old"),
            ({"relation": "UPDATE", "current_fact": "old", "confidence": 0.9}, None),
            ({"relation": "UPDATE", "current_fact": "new", "confidence": 0.5}, None),
            ({"relation": "CONTRADICTION", "confidence": 0.9}, "contradiction"),
            # codex P2 (round 2): low-confidence verdicts fail open to
            # confirm for EVERY relation, not just UPDATE — acting on a
            # 0.1-confidence CONTRADICTION mutates the existing fact.
            ({"relation": "CONTRADICTION", "confidence": 0.1}, None),
            ({"relation": "UNRELATED", "confidence": 0.4}, None),
            ({"relation": "REFINEMENT"}, None),  # missing conf -> 0.0 -> confirm
            ({"relation": "CONTRADICTION", "confidence": "high"}, None),  # malformed
            (None, None),  # classifier failure -> fail open to dedup
            ({}, None),
        ],
    )
    async def test_in_band_routing(self, classification, expected):
        fm = _fm_with_llm(classification)
        result = await fm._classify_dupe_in_band(_dupe(), 0.90, _input(), True)
        assert result == expected


class TestEmbedWithRetry:
    """1b (2026-06-13 audit): embedding failure retries once then persists a
    NULL-embed row (never hard-reject); no provider is a silent None."""

    @pytest.mark.asyncio
    async def test_success_returns_embedding(self):
        fm = FactManager(db=MagicMock(), embeddings=MagicMock(), agent_id="test")
        fm.embeddings.embed = AsyncMock(return_value=[0.1, 0.2])
        assert await fm._embed_with_retry("x") == [0.1, 0.2]
        fm.embeddings.embed.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_retries_then_persists_null(self):
        fm = FactManager(db=MagicMock(), embeddings=MagicMock(), agent_id="test")
        fm.embeddings.embed = AsyncMock(side_effect=RuntimeError("embed outage"))
        assert await fm._embed_with_retry("x", attempts=2) is None  # NULL, not raised
        assert fm.embeddings.embed.await_count == 2  # retried

    @pytest.mark.asyncio
    async def test_no_provider_is_silent_none(self):
        fm = FactManager(db=MagicMock(), embeddings=None, agent_id="test")
        assert await fm._embed_with_retry("x") is None


class TestConsolidationExcludeIds:
    """1c: consolidation MERGE/cluster learn() must pass exclude_ids so dedup
    can't confirm the merged restatement AS a still-active source."""

    def test_merge_and_cluster_pass_exclude_ids(self):
        import inspect
        from nous.handlers.sleep_handler import SleepHandler
        src = inspect.getsource(SleepHandler)
        # F031 MERGE
        merge = src[src.index('source="contradiction_resolution"'):]
        merge = merge[:merge.index("exclude_ids=") + 200]
        assert "exclude_ids=[fact1_id, fact2_id]" in merge
        # F027 cluster
        cluster = src[src.index('source="cluster_consolidation"'):]
        cluster = cluster[:cluster.index("exclude_ids=") + 200]
        assert "exclude_ids=[f.id for f in facts]" in cluster


class TestKnowledgeExtractorTiebreaker:
    """1d (revised after codex PR #519): the paraphrase pre-check stays (learn()
    dedups only at the 0.95 default) but is gated by the F377 distinct-vs-dup
    tiebreaker so semantic opposites are stored, not collapsed."""

    def test_prefilter_gated_by_tiebreaker(self):
        import inspect
        from nous.handlers.knowledge_extractor import KnowledgeExtractor
        src = inspect.getsource(KnowledgeExtractor)
        # multi-hit probe (codex P2 r2); r11 added exclude_sources so the call
        # is now multi-line — pin the probe args including the exemplar exclusion.
        assert "find_similar_facts(" in src
        assert 'content, limit=5, exclude_sources=("exemplar_extractor",)' in src
        assert "is_distinct_fact" in src                       # gated by tiebreaker
        assert "fact_dedup_tiebreaker_enabled" in src
        # codex P1/P2: accumulate every DISTINCT hit id into learn(exclude_ids=...)
        assert "exclude_ids.append(cand.id)" in src
        assert "exclude_ids=exclude_ids or None" in src


class TestApplyBandAction:
    def _fm(self) -> FactManager:
        fm = FactManager(db=MagicMock(), embeddings=None, agent_id="test")
        fm._create_graph_edge = AsyncMock()
        return fm

    @pytest.mark.asyncio
    async def test_supersede_old_deactivates_dupe(self):
        fm = self._fm()
        new, dupe = _dupe("new"), _dupe("old")
        result = await fm._apply_band_action(new, dupe, "supersede_old", 0.90, MagicMock())
        assert result is None
        assert dupe.active is False
        assert dupe.superseded_by == new.id
        assert dupe.confidence == pytest.approx(0.3)
        edge = fm._create_graph_edge.call_args.args
        assert edge[4] == "supersedes"

    @pytest.mark.asyncio
    async def test_contradiction_links_and_warns(self):
        fm = self._fm()
        new, dupe = _dupe("new"), _dupe("old")
        warning = await fm._apply_band_action(new, dupe, "contradiction", 0.88, MagicMock())
        assert warning is not None
        assert warning.existing_fact_id == dupe.id
        assert warning.similarity == pytest.approx(0.88)
        assert new.contradiction_of == dupe.id
        assert dupe.confidence == pytest.approx(0.8)
        # the stale fact stays ACTIVE (warning, not supersession)
        assert dupe.active is True
        edge = fm._create_graph_edge.call_args.args
        assert edge[4] == "contradicts"

    @pytest.mark.asyncio
    async def test_refines_links_keeps_both(self):
        fm = self._fm()
        new, dupe = _dupe("new"), _dupe("old")
        result = await fm._apply_band_action(new, dupe, "refines", 0.90, MagicMock())
        assert result is None
        assert dupe.active is True and dupe.superseded_by is None
        edge = fm._create_graph_edge.call_args.args
        assert edge[4] == "refines"

    @pytest.mark.asyncio
    async def test_unrelated_is_noop(self):
        fm = self._fm()
        new, dupe = _dupe("new"), _dupe("old")
        result = await fm._apply_band_action(new, dupe, "unrelated", 0.90, MagicMock())
        assert result is None
        assert dupe.active is True
        fm._create_graph_edge.assert_not_called()


class TestConfirmDuplicateDateMerge:
    @pytest.mark.asyncio
    async def test_dated_candidate_merges_onto_undated_dupe(self):
        """S3: a dated fact colliding with an undated paraphrase must not be
        silently de-dated — the date is merged onto the confirmed fact."""
        fm = FactManager(db=MagicMock(), embeddings=None, agent_id="test")
        sentinel = object()
        fm._confirm = AsyncMock(return_value=sentinel)
        dupe = _dupe()
        assert dupe.event_date is None
        result = await fm._confirm_duplicate(
            dupe, _input(event_date=date(2026, 3, 10)), MagicMock()
        )
        assert result is sentinel
        assert dupe.event_date == date(2026, 3, 10)
        assert dupe.event_date_classified_at is not None

    @pytest.mark.asyncio
    async def test_dated_dupe_keeps_its_own_date(self):
        fm = FactManager(db=MagicMock(), embeddings=None, agent_id="test")
        fm._confirm = AsyncMock(return_value=object())
        dupe = _dupe(event_date=date(2026, 1, 1))
        await fm._confirm_duplicate(dupe, _input(event_date=date(2026, 3, 10)), MagicMock())
        assert dupe.event_date == date(2026, 1, 1)

    @pytest.mark.asyncio
    async def test_undated_candidate_changes_nothing(self):
        fm = FactManager(db=MagicMock(), embeddings=None, agent_id="test")
        fm._confirm = AsyncMock(return_value=object())
        dupe = _dupe()
        await fm._confirm_duplicate(dupe, _input(), MagicMock())
        assert dupe.event_date is None


# ---------------------------------------------------------------------------
# E2: working-memory residual merge
# ---------------------------------------------------------------------------


class _FakeResult:
    def __init__(self, obj):
        self._obj = obj

    def scalars(self):
        return self

    def first(self):
        return self._obj


class _FakeSession:
    def __init__(self, existing):
        self._existing = existing
        self.added = []
        self.committed = False

    async def execute(self, *_a, **_k):
        return _FakeResult(self._existing)

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.committed = True

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _wm_manager(existing):
    from nous.heart.working_memory import WorkingMemoryManager

    session = _FakeSession(existing)
    db = MagicMock()
    db.session = MagicMock(return_value=session)
    mgr = WorkingMemoryManager(db=db, agent_id="test")
    return mgr, session


def _residual(ref_id, activation, summary="residual fact"):
    return {
        "type": "fact", "ref_id": str(ref_id), "summary": summary,
        "relevance": activation, "loaded_at": "2026-06-09T00:00:00+00:00",
        "activation": activation, "last_surfaced_turn": 1,
    }


def _curated(ref_id, summary="real curated summary"):
    return {
        "type": "fact", "ref_id": str(ref_id), "summary": summary,
        "relevance": 0.9, "loaded_at": "2026-06-09T00:00:00+00:00",
    }


class TestUpsertResidualItemsMerge:
    @pytest.mark.asyncio
    async def test_preserves_curated_items(self):
        """E2 regression: the old wholesale replace clobbered load_item
        entries with residual stubs."""
        curated = _curated(uuid4())
        existing = SimpleNamespace(items=[curated])
        mgr, session = _wm_manager(existing)

        new_residual = _residual(uuid4(), 0.8)
        await mgr.upsert_residual_items("test", "s1", [new_residual])

        assert curated in existing.items
        assert new_residual in existing.items
        assert session.committed

    @pytest.mark.asyncio
    async def test_carried_residuals_kept_for_readside_decay(self):
        """Entries not in this recall's surfaced set stay — load_activations
        decays them by turn distance; dropping them collapsed F055's decay
        window to a single recall."""
        carried = _residual(uuid4(), 0.5)
        existing = SimpleNamespace(items=[carried])
        mgr, _ = _wm_manager(existing)

        await mgr.upsert_residual_items("test", "s1", [_residual(uuid4(), 0.9)])
        assert carried in existing.items
        assert len(existing.items) == 2

    @pytest.mark.asyncio
    async def test_resurfaced_ref_id_is_refreshed(self):
        rid = uuid4()
        old = _residual(rid, 0.3)
        existing = SimpleNamespace(items=[old])
        mgr, _ = _wm_manager(existing)

        fresh = _residual(rid, 0.95, summary="fresh summary")
        await mgr.upsert_residual_items("test", "s1", [fresh])
        assert old not in existing.items
        assert fresh in existing.items
        assert len(existing.items) == 1

    @pytest.mark.asyncio
    async def test_residual_cap_keeps_highest_activation(self):
        low, high = _residual(uuid4(), 0.1), _residual(uuid4(), 0.9)
        existing = SimpleNamespace(items=[low])
        mgr, _ = _wm_manager(existing)

        await mgr.upsert_residual_items("test", "s1", [high], max_residual_items=1)
        assert existing.items == [high]

    @pytest.mark.asyncio
    async def test_creates_row_when_missing(self):
        mgr, session = _wm_manager(existing=None)
        await mgr.upsert_residual_items("test", "s1", [_residual(uuid4(), 0.8)])
        assert len(session.added) == 1
        assert len(session.added[0].items) == 1

    @pytest.mark.asyncio
    async def test_cap_ranks_by_decayed_activation(self):
        """codex P2: a stale turn-1 entry stored at 1.0 must lose the cap
        slot to a fresh surface at 0.9 once decay is applied — otherwise
        new recall results stop entering working memory entirely."""
        stale = _residual(uuid4(), 1.0)
        stale["last_surfaced_turn"] = 1
        existing = SimpleNamespace(items=[stale])
        mgr, _ = _wm_manager(existing)

        fresh = _residual(uuid4(), 0.9)
        fresh["last_surfaced_turn"] = 8
        await mgr.upsert_residual_items(
            "test", "s1", [fresh], max_residual_items=1,
            current_turn=8, decay_fn=lambda age: 0.5 ** age,
        )
        assert existing.items == [fresh]

    @pytest.mark.asyncio
    async def test_combined_list_respects_row_capacity(self):
        """codex P2 (round 2): curated + residual together must not exceed
        the row's max_items — a full curated set leaves no residual space."""
        curated = [_curated(uuid4()) for _ in range(20)]
        existing = SimpleNamespace(items=list(curated), max_items=20)
        mgr, _ = _wm_manager(existing)

        await mgr.upsert_residual_items(
            "test", "s1", [_residual(uuid4(), 0.9)], max_residual_items=20,
        )
        assert len(existing.items) == 20
        assert all("activation" not in d for d in existing.items)  # curated kept

    @pytest.mark.asyncio
    async def test_residuals_fill_remaining_capacity_only(self):
        curated = [_curated(uuid4()) for _ in range(18)]
        existing = SimpleNamespace(items=list(curated), max_items=20)
        mgr, _ = _wm_manager(existing)

        residuals = [_residual(uuid4(), 0.5 + i / 100) for i in range(5)]
        await mgr.upsert_residual_items(
            "test", "s1", residuals, max_residual_items=20,
        )
        assert len(existing.items) == 20  # 18 curated + 2 residual
        kept = [d for d in existing.items if "activation" in d]
        assert len(kept) == 2
        # the two highest-activation residuals won the remaining slots
        assert {d["ref_id"] for d in kept} == {residuals[-1]["ref_id"], residuals[-2]["ref_id"]}

    @pytest.mark.asyncio
    async def test_residual_twin_of_curated_item_dropped(self):
        """codex P2 (round 4): an item already curated in WM must not gain
        a residual twin — both copies rendered into the prompt."""
        rid = uuid4()
        curated = _curated(rid)
        stale_twin = _residual(rid, 0.7)  # pre-fix data: twin already stored
        existing = SimpleNamespace(items=[curated, stale_twin], max_items=20)
        mgr, _ = _wm_manager(existing)

        await mgr.upsert_residual_items("test", "s1", [_residual(rid, 0.9)])
        assert existing.items == [curated]  # twin gone, no new duplicate

    @pytest.mark.asyncio
    async def test_no_decay_fn_keeps_stored_ranking(self):
        """Back-compat: without current_turn/decay_fn the stored activation
        ranks as-is (the pre-fix behavior other callers may rely on)."""
        stale = _residual(uuid4(), 1.0)
        existing = SimpleNamespace(items=[stale])
        mgr, _ = _wm_manager(existing)

        fresh = _residual(uuid4(), 0.9)
        await mgr.upsert_residual_items("test", "s1", [fresh], max_residual_items=1)
        assert existing.items == [stale]


class TestRecordSurfacedSnippets:
    def _activator(self):
        from nous.heart.residual_activation import ResidualActivator

        wm = MagicMock()
        wm.upsert_residual_items = AsyncMock()
        settings = SimpleNamespace(residual_top_k_carried=20)
        return ResidualActivator(settings=settings, wm=wm, db=MagicMock()), wm

    @pytest.mark.asyncio
    async def test_four_tuple_snippet_becomes_summary(self):
        activator, wm = self._activator()
        nid = uuid4()
        await activator.record_surfaced(
            "a", "s", current_turn=2,
            surfaced=[(nid, "fact", 0.9, "Tim prefers dark mode in all editors")],
        )
        items = wm.upsert_residual_items.call_args.kwargs["items"]
        assert items[0]["summary"] == "Tim prefers dark mode in all editors"

    @pytest.mark.asyncio
    async def test_three_tuple_falls_back_to_stub(self):
        activator, wm = self._activator()
        await activator.record_surfaced(
            "a", "s", current_turn=2, surfaced=[(uuid4(), "fact", 0.9)],
        )
        items = wm.upsert_residual_items.call_args.kwargs["items"]
        assert items[0]["summary"] == "residual fact"

    @pytest.mark.asyncio
    async def test_cap_threaded_to_upsert(self):
        activator, wm = self._activator()
        await activator.record_surfaced(
            "a", "s", current_turn=2, surfaced=[(uuid4(), "fact", 0.9)],
        )
        assert wm.upsert_residual_items.call_args.kwargs["max_residual_items"] == 20


# ---------------------------------------------------------------------------
# D2/S7: EmbeddingProvider LRU cache
# ---------------------------------------------------------------------------


def _provider(cache_size: int, api_calls: list) -> "object":
    from nous.brain.embeddings import EmbeddingProvider

    provider = EmbeddingProvider(api_key="test", cache_size=cache_size)

    async def fake_post(payload):
        api_calls.append(payload)
        inp = payload["input"]
        texts = [inp] if isinstance(inp, str) else inp
        data = [
            {"index": i, "embedding": [float(len(t)), float(i)]}
            for i, t in enumerate(texts)
        ]
        response = MagicMock()
        response.json = MagicMock(return_value={"data": data})
        return response

    provider._post_with_retry = fake_post
    return provider


class TestEmbeddingCache:
    @pytest.mark.asyncio
    async def test_repeat_embed_hits_cache(self):
        calls: list = []
        p = _provider(16, calls)
        v1 = await p.embed("same query")
        v2 = await p.embed("same query")
        assert v1 == v2
        assert len(calls) == 1
        assert p.cache_hits == 1

    @pytest.mark.asyncio
    async def test_distinct_texts_miss(self):
        calls: list = []
        p = _provider(16, calls)
        await p.embed("query one")
        await p.embed("query two!")
        assert len(calls) == 2

    @pytest.mark.asyncio
    async def test_cache_disabled_with_size_zero(self):
        calls: list = []
        p = _provider(0, calls)
        await p.embed("same query")
        await p.embed("same query")
        assert len(calls) == 2
        assert p.cache_hits == 0

    @pytest.mark.asyncio
    async def test_lru_eviction(self):
        calls: list = []
        p = _provider(1, calls)
        await p.embed("first")
        await p.embed("second-text")  # evicts "first"
        await p.embed("first")
        assert len(calls) == 3

    @pytest.mark.asyncio
    async def test_batch_requests_only_misses_in_order(self):
        calls: list = []
        p = _provider(16, calls)
        warm = await p.embed("warm text")
        result = await p.embed_batch(["warm text", "cold text!"])
        # second call only requested the miss
        assert calls[-1]["input"] == ["cold text!"]
        assert result[0] == warm
        assert result[1] == [float(len("cold text!")), 0.0]

    @pytest.mark.asyncio
    async def test_batch_collapses_duplicates(self):
        calls: list = []
        p = _provider(16, calls)
        result = await p.embed_batch(["dup text", "dup text"])
        assert calls[-1]["input"] == ["dup text"]
        assert result[0] == result[1]

    @pytest.mark.asyncio
    async def test_batch_empty_no_api_call(self):
        calls: list = []
        p = _provider(16, calls)
        assert await p.embed_batch([]) == []
        assert calls == []

    @pytest.mark.asyncio
    async def test_model_change_does_not_cross_spaces(self):
        calls: list = []
        p = _provider(16, calls)
        await p.embed("same query")
        p.model = "text-embedding-3-large"
        await p.embed("same query")
        assert len(calls) == 2

    @pytest.mark.asyncio
    async def test_caller_mutation_cannot_poison_cache(self):
        """The defensive copies in _cache_get/_cache_put are load-bearing:
        without them a caller mutating a returned vector silently corrupts
        every later consumer of that cache entry."""
        calls: list = []
        p = _provider(16, calls)
        v1 = await p.embed("poison probe")
        original = list(v1)
        v1[0] = 9999.0
        v2 = await p.embed("poison probe")
        assert v2 == original

    @pytest.mark.asyncio
    async def test_batch_failure_after_partial_hit_keeps_cache_clean(self):
        """API failure mid-batch: the call raises, warm entries stay served,
        and the failed text is never cached."""
        calls: list = []
        p = _provider(16, calls)
        await p.embed("warm entry")

        async def failing_post(payload):
            raise RuntimeError("api down")

        p._post_with_retry = failing_post
        with pytest.raises(RuntimeError):
            await p.embed_batch(["warm entry x", "warm entry"])  # one miss forces the call
        # warm entry still serves from cache without the API
        assert await p.embed("warm entry") == await p.embed("warm entry")

    @pytest.mark.asyncio
    async def test_batch_reassembles_out_of_order_indices(self):
        """The provider sorts by the API's index field — shuffled responses
        must still land on the right inputs."""
        from nous.brain.embeddings import EmbeddingProvider

        p = EmbeddingProvider(api_key="t", cache_size=16)

        async def shuffled_post(payload):
            texts = payload["input"]
            data = [
                {"index": i, "embedding": [float(len(t)), float(i)]}
                for i, t in enumerate(texts)
            ]
            data.reverse()  # out-of-order response
            response = MagicMock()
            response.json = MagicMock(return_value={"data": data})
            return response

        p._post_with_retry = shuffled_post
        result = await p.embed_batch(["ab", "abcd"])
        assert result[0] == [2.0, 0.0]
        assert result[1] == [4.0, 1.0]

    @pytest.mark.asyncio
    async def test_short_api_response_raises_and_does_not_cache(self):
        """Review P2: a short response must raise BEFORE caching — a silent
        zip-truncation would cache misaligned vectors."""
        from nous.brain.embeddings import EmbeddingProvider

        p = EmbeddingProvider(api_key="t", cache_size=16)

        async def short_post(payload):
            response = MagicMock()
            response.json = MagicMock(return_value={"data": [
                {"index": 0, "embedding": [1.0, 0.0]},
            ]})
            return response

        p._post_with_retry = short_post
        with pytest.raises(RuntimeError, match="returned 1 vectors for 2"):
            await p.embed_batch(["first text", "second text"])
        assert p.cache_stats["entries"] == 0

    @pytest.mark.asyncio
    async def test_batch_duplicate_misses_count_once(self):
        calls: list = []
        p = _provider(16, calls)
        await p.embed_batch(["dup text", "dup text"])
        assert p.cache_misses == 1


# ---------------------------------------------------------------------------
# S11: _supersede_by_subject exclude_ids (review gap 1)
# ---------------------------------------------------------------------------


class _ScriptedSession:
    """Fake session returning queued results for successive execute calls.

    SET LOCAL statements (ef_search tuning) don't consume the queue — they
    return an empty result like a real session would."""

    def __init__(self, results):
        self._results = list(results)
        self.committed = False

    async def execute(self, statement=None, *_a, **_k):
        if statement is not None and "SET LOCAL" in str(statement):
            return _RowsResult([])
        return self._results.pop(0)

    async def flush(self):
        pass

    async def commit(self):
        self.committed = True


class _ScalarsResult:
    def __init__(self, objs):
        self._objs = list(objs)

    def scalars(self):
        return self

    def all(self):
        return self._objs

    def first(self):
        return self._objs[0] if self._objs else None


class _RowsResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows

    def first(self):
        return self._rows[0] if self._rows else None


def _orm_fact(content="some fact", subject="subj", embedding=None, **kw):
    return SimpleNamespace(
        id=uuid4(), content=content, subject=subject, confidence=1.0,
        superseded_by=None, active=True, contradiction_of=None,
        embedding=embedding, event_date=kw.get("event_date"),
        event_date_classified_at=None,
    )


class TestSupersedeBySubjectExcludes:
    @pytest.mark.asyncio
    async def test_excluded_fact_not_superseded(self):
        """S11: a fact the tiebreaker/band classifier ruled DISTINCT must not
        be superseded — at sim > 0.95 this path skips LLM disambiguation
        entirely, so the exclude is the only guard."""
        fm = FactManager(db=MagicMock(), embeddings=None, agent_id="test")
        emb = [1.0, 0.0, 0.0]
        fact_a = _orm_fact("ruled distinct", embedding=list(emb))
        fact_b = _orm_fact("true stale fact", embedding=list(emb))
        session = _ScriptedSession([_ScalarsResult([fact_a, fact_b])])
        new_id = uuid4()

        await fm._supersede_by_subject(
            new_id, "subj", emb, session,
            new_content="new content", exclude_ids=[fact_a.id],
        )
        assert fact_a.active is True and fact_a.superseded_by is None
        assert fact_b.active is False and fact_b.superseded_by == new_id

    @pytest.mark.asyncio
    async def test_without_excludes_both_superseded(self):
        """Control proving the test above bites."""
        fm = FactManager(db=MagicMock(), embeddings=None, agent_id="test")
        emb = [1.0, 0.0, 0.0]
        fact_a = _orm_fact("a", embedding=list(emb))
        fact_b = _orm_fact("b", embedding=list(emb))
        session = _ScriptedSession([_ScalarsResult([fact_a, fact_b])])

        await fm._supersede_by_subject(uuid4(), "subj", emb, session)
        assert fact_a.active is False
        assert fact_b.active is False


# ---------------------------------------------------------------------------
# S10/F075: _find_duplicate Python-side selection (review gap 3)
# ---------------------------------------------------------------------------


class TestFindDuplicateSelection:
    def _fm(self, threshold=0.92):
        return FactManager(
            db=MagicMock(), embeddings=None, agent_id="test",
            settings=SimpleNamespace(fact_native_cosine_threshold=threshold),
        )

    @staticmethod
    def _row(similarity, event_date=None):
        # codex r16: real rows always carry `source`; the mock was stale.
        return SimpleNamespace(id=uuid4(), event_date=event_date, similarity=similarity, source=None)

    @pytest.mark.asyncio
    async def test_same_date_preferred_over_nearer_different_date(self):
        """F075: a March-10 candidate must match the March-10 fact even when a
        March-12 fact is nearer — otherwise the date bypass fires and inserts
        a duplicate (the PR #461 P2 bug)."""
        nearer = self._row(0.97, date(2026, 3, 12))
        same_date = self._row(0.96, date(2026, 3, 10))
        orm = _orm_fact("the march 10 fact")
        orm.id = same_date.id
        session = _ScriptedSession([
            _RowsResult([nearer, same_date, self._row(0.50)]),
            _ScalarsResult([orm]),
        ])
        result = await self._fm()._find_duplicate(
            [0.1] * 8, [], session, candidate_event_date=date(2026, 3, 10),
        )
        assert result is not None
        fact, sim = result
        assert fact.id == same_date.id
        assert sim == pytest.approx(0.96)

    @pytest.mark.asyncio
    async def test_all_below_threshold_returns_none(self):
        session = _ScriptedSession([_RowsResult([self._row(0.80), self._row(0.60)])])
        assert await self._fm(0.92)._find_duplicate([0.1] * 8, [], session) is None

    @pytest.mark.asyncio
    async def test_no_date_match_falls_back_to_nearest(self):
        top = self._row(0.97, date(2026, 1, 1))
        orm = _orm_fact("nearest")
        orm.id = top.id
        session = _ScriptedSession([
            _RowsResult([top, self._row(0.95, date(2026, 2, 2))]),
            _ScalarsResult([orm]),
        ])
        result = await self._fm()._find_duplicate(
            [0.1] * 8, [], session, candidate_event_date=date(2026, 3, 10),
        )
        assert result is not None and result[0].id == top.id

    @pytest.mark.asyncio
    async def test_undated_candidate_prefers_undated_existing(self):
        """NULL == NULL counts as a date match (IS NOT DISTINCT semantics)."""
        dated = self._row(0.97, date(2026, 1, 1))
        undated = self._row(0.96, None)
        orm = _orm_fact("undated")
        orm.id = undated.id
        session = _ScriptedSession([
            _RowsResult([dated, undated]),
            _ScalarsResult([orm]),
        ])
        result = await self._fm()._find_duplicate(
            [0.1] * 8, [], session, candidate_event_date=None,
        )
        assert result is not None and result[0].id == undated.id


# ---------------------------------------------------------------------------
# Production wiring: Heart.find_similar_facts + _learn band orchestration
# (review gaps 2 + 4 — real Heart fixture, runs under NOUS_TEST_DB=postgres)
# ---------------------------------------------------------------------------


@pytest.mark.postgres_only
class TestFindSimilarFactsWiring:
    @pytest.mark.asyncio
    async def test_raw_cosine_scores_active_only(self, heart, session):
        stored = await heart.learn(
            FactInput(
                subject="typing",
                content="Python is a dynamically typed programming language",
                source="user_direct",
            ),
            session=session,
        )
        hits = await heart.find_similar_facts(
            "Python is a dynamically typed programming language", session=session
        )
        assert hits, "identical text must be found"
        top = hits[0]
        assert top.id == stored.id
        assert top.score is not None and 0.99 <= top.score <= 1.0001  # raw cosine
        scores = [h.score for h in hits if h.score is not None]
        assert scores == sorted(scores, reverse=True)
        # raw cosine lives in [-1, 1] (mock embeddings DO produce negative
        # cosines for unrelated text — real OpenAI vectors rarely do)
        assert all(-1.0001 <= s <= 1.0001 for s in scores)

    @pytest.mark.asyncio
    async def test_inactive_facts_never_returned(self, heart, session):
        stored = await heart.learn(
            FactInput(
                subject="db",
                content="The project database is PostgreSQL seventeen with pgvector",
                source="user_direct",
            ),
            session=session,
        )
        orm = await heart.facts._get_fact_orm(stored.id, session)
        orm.active = False
        await session.flush()
        hits = await heart.find_similar_facts(
            "The project database is PostgreSQL seventeen with pgvector",
            session=session,
        )
        assert all(h.id != stored.id for h in hits)


@pytest.mark.postgres_only
class TestLearnBandOrchestration:
    @pytest.mark.asyncio
    async def test_unrelated_verdict_inserts_and_skips_reclassification(self, heart, session):
        """Review gap 2: a routed verdict must INSERT (not confirm), keep the
        dupe unconfirmed, and exclude it from the post-insert contradiction
        scan (classifier awaited exactly once)."""
        first = await heart.learn(
            FactInput(
                subject="staging health",
                content="The staging health endpoint returns HTTP 200 with status ok",
                source="user_direct",
            ),
            session=session,
        )
        fm = heart.facts
        dupe_orm = await fm._get_fact_orm(first.id, session)
        orig_fd, orig_llm = fm._find_duplicate, fm._llm
        fm._find_duplicate = AsyncMock(return_value=(dupe_orm, 0.90))
        fm._llm = object()
        fm._classify_fact_pair = AsyncMock(
            return_value={"relation": "UNRELATED", "confidence": 0.9}
        )
        try:
            second = await heart.learn(
                FactInput(
                    subject="staging health",
                    content="The staging health endpoint returns HTTP 500 internal errors",
                    source="user_direct",
                ),
                session=session,
            )
        finally:
            fm._find_duplicate, fm._llm = orig_fd, orig_llm
        assert second.id != first.id, "routed verdict must insert, not confirm"
        refreshed = await fm._get_fact_orm(first.id, session)
        assert (refreshed.confirmation_count or 0) == 0
        assert refreshed.active is True
        assert fm._classify_fact_pair.await_count == 1

    @pytest.mark.asyncio
    async def test_contradiction_verdict_links_and_warns(self, heart, session):
        first = await heart.learn(
            FactInput(
                subject="api status",
                content="The production API is fully operational and serving traffic",
                source="user_direct",
            ),
            session=session,
        )
        fm = heart.facts
        dupe_orm = await fm._get_fact_orm(first.id, session)
        orig_fd, orig_llm = fm._find_duplicate, fm._llm
        fm._find_duplicate = AsyncMock(return_value=(dupe_orm, 0.88))
        fm._llm = object()
        fm._classify_fact_pair = AsyncMock(
            return_value={"relation": "CONTRADICTION", "confidence": 0.9}
        )
        try:
            second = await heart.learn(
                FactInput(
                    subject="api status",
                    content="The production API is down and returning errors to all clients",
                    source="user_direct",
                ),
                session=session,
            )
        finally:
            fm._find_duplicate, fm._llm = orig_fd, orig_llm
        assert second.id != first.id
        assert second.contradiction_warning is not None
        assert second.contradiction_warning.existing_fact_id == first.id
        refreshed = await fm._get_fact_orm(first.id, session)
        assert refreshed.active is True  # warning, not supersession
        assert refreshed.confidence == pytest.approx(0.8)


# ---------------------------------------------------------------------------
# R1: §14 cosine-leg score floor
# ---------------------------------------------------------------------------


class TestCosineLegScoreFloor:
    def _engine(self, summaries):
        from nous.cognitive.context import ContextEngine
        from nous.config import Settings

        settings = Settings(_env_file=None, proc_selection_graph_primary=True)
        brain = MagicMock()
        brain.neighbors = AsyncMock(return_value=[])
        heart = MagicMock()
        heart.find_similar_procedures = AsyncMock(return_value=summaries)
        heart.get_procedure_by_name = AsyncMock()
        return ContextEngine(brain, heart, settings, identity_prompt="Test"), heart

    @staticmethod
    def _summary(score):
        return SimpleNamespace(id=uuid4(), score=score)

    @pytest.mark.asyncio
    async def test_sub_floor_candidates_not_loaded(self):
        """R1: cosine candidates below procedure_score_floor (0.40) must not
        have their bodies fetched — previously there was no score check."""
        engine, heart = self._engine([self._summary(0.10), self._summary(0.05)])
        heart.get_procedure = AsyncMock()

        selected = await engine._select_procedures(
            slots=3, critic_skills=[], recalled_ids={},
            recalled_score_map={}, session=None, query="off topic chitchat",
        )
        assert selected == []
        heart.get_procedure.assert_not_called()

    @pytest.mark.asyncio
    async def test_above_floor_candidates_still_selected(self):
        above = self._summary(0.80)
        proc = SimpleNamespace(id=above.id, active=True)
        engine, heart = self._engine([above, self._summary(0.10)])
        heart.get_procedure = AsyncMock(return_value=proc)

        selected = await engine._select_procedures(
            slots=3, critic_skills=[], recalled_ids={},
            recalled_score_map={}, session=None, query="how do I deploy",
        )
        assert [p.id for p in selected] == [above.id]
        # the sub-floor candidate never had its body fetched
        assert heart.get_procedure.await_count == 1

    @pytest.mark.asyncio
    async def test_non_monotonic_ordering_does_not_drop_valid_candidates(self):
        """The floor check uses `continue`, not `break` — a sub-floor hit
        ordered first (utility boost can reorder) must not end the scan."""
        low, high = self._summary(0.10), self._summary(0.80)
        proc = SimpleNamespace(id=high.id, active=True)
        engine, heart = self._engine([low, high])
        heart.get_procedure = AsyncMock(return_value=proc)

        selected = await engine._select_procedures(
            slots=3, critic_skills=[], recalled_ids={},
            recalled_score_map={}, session=None, query="deploy steps",
        )
        assert [p.id for p in selected] == [high.id]

    @pytest.mark.asyncio
    async def test_none_score_passes_floor(self):
        """No-embeddings deployments return score=None — the floor must not
        filter those (mirrors the passive path's _has_embeddings gate)."""
        summ = self._summary(None)
        proc = SimpleNamespace(id=summ.id, active=True)
        engine, heart = self._engine([summ])
        heart.get_procedure = AsyncMock(return_value=proc)

        selected = await engine._select_procedures(
            slots=3, critic_skills=[], recalled_ids={},
            recalled_score_map={}, session=None, query="anything",
        )
        assert [p.id for p in selected] == [summ.id]
