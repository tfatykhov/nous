"""Tests for Reciprocal Rank Fusion (RRF) hybrid search (F025)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from nous.config import Settings
from nous.heart.search import _rrf_merge, _rrf_merge_n
from nous.runtime_config import RuntimeConfig


class TestRRFConfig:
    def test_rrf_k_default(self):
        s = Settings()
        assert s.rrf_k == 60

    def test_rrf_k_from_env(self, monkeypatch):
        monkeypatch.setenv("NOUS_RRF_K", "40")
        s = Settings()
        assert s.rrf_k == 40


class TestRRFRuntimeConfig:
    def setup_method(self):
        RuntimeConfig.reset()

    def teardown_method(self):
        RuntimeConfig.reset()

    def test_get_rrf_k_default(self):
        rc = RuntimeConfig.get()
        s = Settings()
        assert rc.get_rrf_k(s) == 60

    def test_set_and_get_rrf_k(self):
        rc = RuntimeConfig.get()
        rc.set_rrf_k(40)
        s = Settings()
        assert rc.get_rrf_k(s) == 40
        assert rc.get_rrf_k_source(s) == "runtime_override"

    def test_clear_rrf_k(self):
        rc = RuntimeConfig.get()
        rc.set_rrf_k(40)
        rc.clear_rrf_k()
        s = Settings()
        assert rc.get_rrf_k(s) == 60
        assert rc.get_rrf_k_source(s) == "default"


class TestRRFMerge:
    """Test the pure RRF merge function (no DB needed)."""

    def test_both_lists_same_doc(self):
        """Doc appearing in both lists gets scores from both."""
        from nous.heart.search import _rrf_merge

        doc_a = uuid4()
        vector_ranked = [(doc_a, 0.95)]
        keyword_ranked = [(doc_a, 0.08)]
        result = _rrf_merge(vector_ranked, keyword_ranked, k=60, vector_weight=0.5, limit=10)
        assert len(result) == 1
        assert result[0][0] == doc_a
        # Normalized: (0.5/60 + 0.5/60) / (1/60) = 1.0
        assert abs(result[0][1] - 1.0) < 1e-9

    def test_disjoint_lists(self):
        """Docs in only one list get penalty rank for the other."""
        from nous.heart.search import _rrf_merge

        doc_v = uuid4()
        doc_k = uuid4()
        vector_ranked = [(doc_v, 0.9)]
        keyword_ranked = [(doc_k, 0.08)]
        result = _rrf_merge(vector_ranked, keyword_ranked, k=60, vector_weight=0.5, limit=10)
        assert len(result) == 2
        # penalty rank = limit + 1 = 11
        # Normalized: raw / (1/k) = raw * k
        raw_v = 0.5 / 60 + 0.5 / 71
        raw_k = 0.5 / 71 + 0.5 / 60
        max_score = 1.0 / 60
        assert abs(result[0][1] - raw_v / max_score) < 1e-9
        assert abs(result[1][1] - raw_k / max_score) < 1e-9

    def test_vector_only(self):
        """Empty keyword list — all docs use penalty rank for keyword."""
        from nous.heart.search import _rrf_merge

        doc = uuid4()
        result = _rrf_merge([(doc, 0.9)], [], k=60, vector_weight=0.7, limit=5)
        assert len(result) == 1
        raw = 0.7 / 60 + 0.3 / (60 + 6)
        expected = raw / (1.0 / 60)
        assert abs(result[0][1] - expected) < 1e-9

    def test_keyword_only(self):
        """Empty vector list — all docs use penalty rank for vector."""
        from nous.heart.search import _rrf_merge

        doc = uuid4()
        result = _rrf_merge([], [(doc, 0.05)], k=60, vector_weight=0.7, limit=5)
        assert len(result) == 1
        raw = 0.7 / (60 + 6) + 0.3 / 60
        expected = raw / (1.0 / 60)
        assert abs(result[0][1] - expected) < 1e-9

    def test_both_empty(self):
        """Both lists empty — returns empty."""
        from nous.heart.search import _rrf_merge

        result = _rrf_merge([], [], k=60, vector_weight=0.5, limit=10)
        assert result == []

    def test_limit_respected(self):
        """Result count capped at limit."""
        from nous.heart.search import _rrf_merge

        docs = [(uuid4(), 0.9 - i * 0.01) for i in range(20)]
        result = _rrf_merge(docs, [], k=60, vector_weight=0.7, limit=5)
        assert len(result) == 5

    def test_ordering_by_rrf_score(self):
        """Results sorted by RRF score descending."""
        from nous.heart.search import _rrf_merge

        doc_a, doc_b, doc_c = uuid4(), uuid4(), uuid4()
        vector = [(doc_a, 0.9), (doc_b, 0.8), (doc_c, 0.7)]
        keyword = [(doc_b, 0.08), (doc_c, 0.06), (doc_a, 0.04)]
        result = _rrf_merge(vector, keyword, k=60, vector_weight=0.5, limit=10)
        scores = [s for _, s in result]
        assert scores == sorted(scores, reverse=True)

    def test_normalization_rank1_both_is_1(self):
        """Doc ranked #1 in both lists normalizes to 1.0."""
        from nous.heart.search import _rrf_merge

        doc = uuid4()
        result = _rrf_merge([(doc, 0.9)], [(doc, 0.1)], k=60, vector_weight=0.7, limit=10)
        assert abs(result[0][1] - 1.0) < 1e-9

    def test_normalization_preserves_order(self):
        """Normalization doesn't change relative ranking."""
        from nous.heart.search import _rrf_merge

        docs = [uuid4() for _ in range(5)]
        vector = [(docs[i], 0.9 - i * 0.1) for i in range(5)]
        keyword = [(docs[i], 0.05 - i * 0.01) for i in range(5)]
        result = _rrf_merge(vector, keyword, k=60, vector_weight=0.7, limit=10)
        # Order should match input order since both lists agree
        assert [doc_id for doc_id, _ in result] == docs

    def test_normalization_scores_in_range(self):
        """All normalized scores are between 0 and 1 (before downstream boosts)."""
        from nous.heart.search import _rrf_merge

        docs = [uuid4() for _ in range(10)]
        vector = [(docs[i], 0.9 - i * 0.05) for i in range(10)]
        keyword = [(docs[i], 0.1 - i * 0.005) for i in range(8)]  # fewer keyword results
        result = _rrf_merge(vector, keyword, k=60, vector_weight=0.7, limit=10)
        for _, score in result:
            assert 0.0 <= score <= 1.0, f"Score {score} out of [0,1] range"


class TestPenaltyRankDecoupling:
    """``_rrf_merge`` scores a doc missing from one leg at ``limit + 1``, so a
    row-count limit is also a scoring input. ``hybrid_search`` passed its own
    ``limit`` straight through, which made ``episode_chunk_recall_limit`` a
    scoring knob: raising it 20 -> 30 at NOUS_RRF_K=30 dropped every
    single-leg chunk by ~0.029 and DEMOTED chunks (measured over 60 queries:
    -0.83 chunks in top-10, 0/60 of the newly admitted chunks reaching
    top-10). ``penalty_limit`` pins the penalty base so the two are separable.
    """

    @staticmethod
    def _vector_only_candidates(n=40):
        """n vector hits, zero keyword hits -> every doc takes penalty_rank."""
        return [(uuid4(), 0.9) for _ in range(n)], []

    @pytest.mark.parametrize("limit", [10, 20, 30, 50])
    def test_score_is_invariant_across_limits_when_penalty_pinned(self, limit):
        """THE regression test. With the penalty base pinned, a document's
        score does not move when the row allotment changes."""
        vector, keyword = self._vector_only_candidates()
        target = vector[0][0]

        merged = _rrf_merge(vector, keyword, 30, 0.7, 10, return_limit=limit)

        score = dict(merged)[target]
        assert score == pytest.approx(0.9195, abs=1e-4), (
            "pinning the penalty base must hold the score steady regardless "
            f"of how many rows are returned (limit={limit})"
        )

    def test_score_varies_with_limit_when_not_pinned(self):
        """Negative case — pins the CURRENT coupled behaviour so it cannot be
        silently reintroduced, and proves the parametrized test above is
        actually exercising ``penalty_limit`` rather than passing trivially."""
        vector, keyword = self._vector_only_candidates()
        target = vector[0][0]

        scores = {limit: dict(_rrf_merge(vector, keyword, 30, 0.7, limit))[target] for limit in (10, 20, 30, 50)}

        assert len(set(scores.values())) == 4, (
            f"unpinned, each limit must yield a DIFFERENT score — that coupling is the defect being fixed; got {scores}"
        )
        # Monotonically decreasing: a bigger limit means a worse penalty rank.
        assert scores[10] > scores[20] > scores[30] > scores[50]
        # The specific prod-shaped delta that produced the measured regression.
        assert scores[20] - scores[30] == pytest.approx(0.029, abs=5e-4)

    def test_return_limit_still_controls_row_count(self):
        """Pinning the penalty base must not shrink the result set."""
        vector, keyword = self._vector_only_candidates(n=40)

        assert len(_rrf_merge(vector, keyword, 30, 0.7, 10, return_limit=30)) == 30
        assert len(_rrf_merge(vector, keyword, 30, 0.7, 10)) == 10

    @pytest.mark.asyncio
    async def test_hybrid_search_penalty_limit_defaults_to_coupled(self):
        """``penalty_limit=None`` must leave the other five hybrid_search call
        sites byte-identical — the merge is called with the row limit only."""
        import nous.heart.search as search_mod

        captured: dict = {}

        def _spy(vector, keyword, k, vw, limit, return_limit=None, **kw):
            captured["limit"] = limit
            captured["return_limit"] = return_limit
            captured["cap"] = kw.get("cap_ranks_at_penalty", False)
            return []

        session = MagicMock()
        session.execute = AsyncMock(return_value=MagicMock(all=lambda: []))

        with patch.object(search_mod, "_rrf_merge", _spy):
            await search_mod.hybrid_search(
                session, "heart.episode_chunks", [0.1, 0.2], "q", "a", limit=30, active_filter=False
            )

        assert captured["limit"] == 30
        assert captured["return_limit"] is None
        assert captured["cap"] is False, "unpinned must NOT cap (byte-identical)"

    @pytest.mark.asyncio
    async def test_hybrid_search_penalty_limit_pins_merge_base(self):
        """With ``penalty_limit`` set, the merge gets the pinned base as its
        penalty ``limit`` and the row count moves to ``return_limit``."""
        import nous.heart.search as search_mod

        captured: dict = {}

        def _spy(vector, keyword, k, vw, limit, return_limit=None, **kw):
            captured["limit"] = limit
            captured["return_limit"] = return_limit
            captured["cap"] = kw.get("cap_ranks_at_penalty", False)
            return []

        session = MagicMock()
        session.execute = AsyncMock(return_value=MagicMock(all=lambda: []))

        with patch.object(search_mod, "_rrf_merge", _spy):
            await search_mod.hybrid_search(
                session, "heart.episode_chunks", [0.1, 0.2], "q", "a", limit=30, active_filter=False, penalty_limit=10
            )

        assert captured["limit"] == 10, "penalty base must be the pinned value"
        assert captured["return_limit"] == 30, "row count must survive pinning"
        assert captured["cap"] is True, "pinned mode must also cap observed ranks"

    @pytest.mark.asyncio
    async def test_chunk_leg_threads_setting_through(self):
        """Wiring test: the F067 chunk leg must actually pass
        ``chunk_rrf_penalty_limit`` down to hybrid_search. Without this the
        setting exists but does nothing — the failure mode that produced the
        original inert-knob bug in the first place."""
        import contextlib
        from types import SimpleNamespace

        import nous.heart.search as search_mod
        from nous.api.retrieval_pipeline import _search_episode_chunks

        captured: dict = {}

        async def _spy(*args, **kwargs):
            captured.update(kwargs)
            return []

        @contextlib.asynccontextmanager
        async def _session():
            yield MagicMock(execute=AsyncMock())

        heart = SimpleNamespace(
            db=SimpleNamespace(session=_session),
            _embeddings=SimpleNamespace(embed=AsyncMock(return_value=[0.1, 0.2])),
            agent_id="a",
        )

        with patch.object(search_mod, "hybrid_search", _spy):
            await _search_episode_chunks(
                heart=heart,
                query="q",
                agent_id="a",
                limit=30,
                settings=SimpleNamespace(
                    chunk_hybrid_search_enabled=True,
                    chunk_rrf_penalty_limit=10,
                ),
            )

        assert captured.get("penalty_limit") == 10, "chunk leg must thread the setting through, else the knob is inert"
        assert captured.get("limit") == 30, "row allotment must be unaffected"

    def test_negative_penalty_limit_is_rejected_at_startup(self):
        """Codex P2. ``penalty_limit`` becomes ``penalty_rank = limit + 1``,
        which is a divisor term in ``1 / (k + penalty_rank)``. At prod's
        NOUS_RRF_K=30 a value of -31 zeroes that denominator and takes the
        whole chunk-recall stage down with ZeroDivisionError; other negatives
        yield scores outside [0,1] or silently empty the result list via
        ``scored[:limit]``. Must fail closed at config load."""
        import pydantic

        with pytest.raises(pydantic.ValidationError):
            Settings(chunk_rrf_penalty_limit=-31)
        with pytest.raises(pydantic.ValidationError):
            Settings(chunk_rrf_penalty_limit=0)

        assert Settings(chunk_rrf_penalty_limit=20).chunk_rrf_penalty_limit == 20
        assert Settings().chunk_rrf_penalty_limit is None

    def test_the_crash_that_bound_guards_against(self):
        """Pins WHY the bound exists — if someone widens it back, this shows
        the concrete failure rather than an abstract 'validation' rule."""
        vector, keyword = self._vector_only_candidates(n=1)

        with pytest.raises(ZeroDivisionError):
            _rrf_merge(vector, keyword, 30, 0.7, -31)

    @pytest.mark.asyncio
    async def test_penalty_limit_also_pins_the_sql_fetch_window(self):
        """Codex r2. Pinning penalty_rank alone is not enough: the SQL legs
        fetch ``limit * 3``, so raising ``limit`` widens the candidate SET and
        a document surfacing beyond the penalty rank scores WORSE than one
        absent from the leg. The fetch window must be pinned too, else the
        setting only partially decouples scoring from the allotment."""
        import nous.heart.search as search_mod

        async def _capture(limit, penalty_limit):
            seen = []

            class _Sess:
                async def execute(self, sql, params):
                    seen.append(params["limit_expanded"])
                    return MagicMock(all=lambda: [])

            with patch.object(search_mod, "_rrf_merge", lambda *a, **k: []):
                await search_mod.hybrid_search(
                    _Sess(),
                    "heart.episode_chunks",
                    [0.1, 0.2],
                    "q",
                    "a",
                    limit=limit,
                    active_filter=False,
                    penalty_limit=penalty_limit,
                )
            return seen

        pinned = {lim: await _capture(lim, 20) for lim in (10, 20, 30, 50)}
        assert len({tuple(v) for v in pinned.values()}) == 1, (
            f"pinned, the fetch window must not move with limit; got {pinned}"
        )
        assert all(v[0] == 60 for v in pinned.values()), pinned

        # Unpinned: the window still tracks limit (today's behaviour, and the
        # reason the candidate set moved in the first place).
        unpinned = {lim: (await _capture(lim, None))[0] for lim in (10, 20, 30)}
        assert unpinned == {10: 30, 20: 60, 30: 90}, unpinned

    @pytest.mark.asyncio
    async def test_pinned_window_never_starves_the_row_count(self):
        """The max() guard: a small penalty base must not shrink the fetch
        window below the rows the caller asked for."""
        import nous.heart.search as search_mod

        seen = []

        class _Sess:
            async def execute(self, sql, params):
                seen.append(params["limit_expanded"])
                return MagicMock(all=lambda: [])

        with patch.object(search_mod, "_rrf_merge", lambda *a, **k: []):
            await search_mod.hybrid_search(
                _Sess(), "heart.episode_chunks", [0.1, 0.2], "q", "a", limit=50, active_filter=False, penalty_limit=10
            )

        assert seen[0] == 50, f"window must cover the requested rows, got {seen[0]}"


class TestHeartLegPenaltyPinning:
    """F1 (R2 follow-ups): the same coupling #580 fixed for chunks is live on
    the HEART legs, and larger. ``Heart.recall`` sets ``fetch_limit = limit*2``
    (heart.py) and hands it to episodes/facts/procedures, which pass it to
    ``hybrid_search`` where ``penalty_rank = limit + 1``. ``recall_deep``'s
    ``limit`` is LLM-controlled over 1..50, so the heart legs' penalty base
    swings between 3 and 101 — every single-leg fact/episode/procedure is
    rescored by a parameter meant to control row count only.

    Query expansion (ON in prod) stacks TWO penalty layers: a per-variant
    ``_rrf_merge`` inside each ``hybrid_search``, then a cross-variant
    ``_rrf_merge_n``. Both must be pinned or the setting only half-works.
    """

    @staticmethod
    def _lists(n=40):
        ids = [uuid4() for _ in range(n)]
        # Two lists that agree on the head but diverge in the tail, so the
        # tail docs take the penalty rank in one list.
        return ids, [[(i, 0.9) for i in ids[:20]], [(i, 0.8) for i in ids[10:30]]]

    @pytest.mark.parametrize("limit", [10, 20, 30, 50])
    def test_merge_n_score_invariant_when_penalty_pinned(self, limit):
        ids, lists = self._lists()
        target = ids[0]  # present in list 0, absent from list 1 -> takes penalty
        merged = _rrf_merge_n(lists, 30, 20, return_limit=limit)
        assert dict(merged)[target] == pytest.approx(
            dict(_rrf_merge_n(lists, 30, 20, return_limit=10))[target], abs=1e-9
        ), "pinned penalty base must hold the score steady across row counts"

    def test_merge_n_score_varies_when_not_pinned(self):
        """Negative case — pins today's coupling so it cannot silently return."""
        ids, lists = self._lists()
        target = ids[0]
        scores = {lim: dict(_rrf_merge_n(lists, 30, lim))[target] for lim in (10, 20, 30, 50)}
        assert len(set(scores.values())) == 4, scores
        assert scores[10] > scores[20] > scores[30] > scores[50]

    def test_merge_n_return_limit_controls_row_count(self):
        _ids, lists = self._lists()
        assert len(_rrf_merge_n(lists, 30, 20, return_limit=25)) == 25
        assert len(_rrf_merge_n(lists, 30, 20)) == 20

    @pytest.mark.asyncio
    async def test_multi_pins_BOTH_layers(self):
        """Expansion stacks two merges. Pinning only one leaves the other
        coupled, so assert the per-variant calls AND the fusion both receive
        the pinned base."""
        import nous.heart.search as search_mod

        per_variant_penalties = []
        fusion = {}

        async def _fake_hybrid(**kw):
            per_variant_penalties.append(kw.get("penalty_limit"))
            return [(uuid4(), 0.9)]

        def _fake_merge_n(lists, k, limit, return_limit=None, **kw):
            fusion["limit"] = limit
            fusion["return_limit"] = return_limit
            fusion["cap"] = kw.get("cap_ranks_at_penalty", False)
            return []

        with (
            patch.object(search_mod, "hybrid_search", _fake_hybrid),
            patch.object(search_mod, "_rrf_merge_n", _fake_merge_n),
        ):
            await search_mod.hybrid_search_multi(
                MagicMock(), "heart.facts", [("a", [0.1]), ("b", [0.2])], "agent", limit=30, penalty_limit=20
            )

        assert per_variant_penalties == [20, 20], per_variant_penalties
        assert fusion["limit"] == 20, "fusion penalty base must be pinned"
        assert fusion["return_limit"] == 30, "row count must survive pinning"
        assert fusion["cap"] is True, "fusion must cap observed ranks too"

    @pytest.mark.asyncio
    async def test_multi_unpinned_is_byte_identical(self):
        """penalty_limit=None must leave the multi path exactly as it was."""
        import nous.heart.search as search_mod

        seen = []
        fusion = {}

        async def _fake_hybrid(**kw):
            seen.append(kw.get("penalty_limit"))
            return [(uuid4(), 0.9)]

        def _fake_merge_n(lists, k, limit, return_limit=None, **kw):
            fusion["limit"] = limit
            fusion["return_limit"] = return_limit
            fusion["cap"] = kw.get("cap_ranks_at_penalty", False)
            return []

        with (
            patch.object(search_mod, "hybrid_search", _fake_hybrid),
            patch.object(search_mod, "_rrf_merge_n", _fake_merge_n),
        ):
            await search_mod.hybrid_search_multi(
                MagicMock(), "heart.facts", [("a", [0.1]), ("b", [0.2])], "agent", limit=30
            )

        assert seen == [None, None]
        assert fusion["limit"] == 30
        assert fusion["return_limit"] is None
        assert fusion["cap"] is False

    def test_config_bound_and_default(self):
        import pydantic

        assert Settings().heart_rrf_penalty_limit is None
        assert Settings(heart_rrf_penalty_limit=20).heart_rrf_penalty_limit == 20
        with pytest.raises(pydantic.ValidationError):
            Settings(heart_rrf_penalty_limit=0)
        with pytest.raises(pydantic.ValidationError):
            Settings(heart_rrf_penalty_limit=-31)

    @pytest.mark.asyncio
    async def test_heart_recall_threads_setting_to_all_three_legs(self):
        """Wiring test. A setting that exists but never reaches the leg is
        exactly the inert-knob failure this whole line of work started from
        (#579), so assert the value actually arrives at episodes, facts AND
        procedures — and that fetch_limit stays limit*2 independently."""
        from types import SimpleNamespace

        from nous.heart.heart import Heart

        heart = Heart.__new__(Heart)
        heart.settings = SimpleNamespace(
            query_expansion_enabled=False,
            heart_rrf_penalty_limit=20,
            cross_encoder_enabled=False,
            mmr_enabled=False,
        )
        heart.agent_id = "nous-test"
        heart._embeddings = None
        heart._owns_embeddings = False
        heart._bus = None
        heart._query_expander = None
        heart._residual_activator = None
        for name in ("episodes", "facts", "procedures", "censors"):
            mgr = MagicMock()
            mgr.search = AsyncMock(return_value=[])
            setattr(heart, name, mgr)

        await heart._recall("a query", limit=10, types=None, session=AsyncMock())

        for mgr in (heart.episodes.search, heart.facts.search, heart.procedures.search):
            assert mgr.call_args is not None, "leg was not searched"
            assert mgr.call_args.kwargs.get("penalty_limit") == 20, (
                f"leg did not receive the pinned base: {mgr.call_args.kwargs}"
            )
        # Row count is still limit*2 — pinning the penalty must not shrink it.
        assert heart.facts.search.call_args.args[1] == 20

    @pytest.mark.asyncio
    async def test_heart_recall_unset_setting_passes_none(self):
        """Default must leave the legs byte-identical."""
        from types import SimpleNamespace

        from nous.heart.heart import Heart

        heart = Heart.__new__(Heart)
        heart.settings = SimpleNamespace(
            query_expansion_enabled=False,
            cross_encoder_enabled=False,
            mmr_enabled=False,
        )  # attribute absent entirely -> getattr default
        heart.agent_id = "nous-test"
        heart._embeddings = None
        heart._owns_embeddings = False
        heart._bus = None
        heart._query_expander = None
        heart._residual_activator = None
        for name in ("episodes", "facts", "procedures", "censors"):
            mgr = MagicMock()
            mgr.search = AsyncMock(return_value=[])
            setattr(heart, name, mgr)

        await heart._recall("a query", limit=10, types=None, session=AsyncMock())

        for mgr in (heart.episodes.search, heart.facts.search, heart.procedures.search):
            assert mgr.call_args.kwargs.get("penalty_limit") is None

    def test_rank_beyond_pin_scores_same_as_absent(self):
        """Codex P1 (#581). Pinning the penalty base is not sufficient on its
        own: the legs over-fetch, so a doc ABSENT from a list at one row count
        can APPEAR deep in it at a larger one. Past penalty_rank an observed
        rank contributes LESS than the missing-leg penalty, so the doc is
        demoted for having been found — and the score moves with the row count
        despite the pin. Capping makes deep-presence and absence identical."""
        target = uuid4()
        others = [uuid4() for _ in range(60)]

        # Arm A: target absent from the keyword leg entirely.
        absent = _rrf_merge(
            [(target, 0.9)],
            [(o, 0.5) for o in others],
            30,
            0.7,
            20,
            return_limit=100,
            cap_ranks_at_penalty=True,
        )
        # Arm B: identical, except the target now surfaces at keyword rank 40.
        deep = _rrf_merge(
            [(target, 0.9)],
            [(o, 0.5) for o in others[:40]] + [(target, 0.1)],
            30,
            0.7,
            20,
            return_limit=100,
            cap_ranks_at_penalty=True,
        )

        assert dict(absent)[target] == pytest.approx(dict(deep)[target], abs=1e-9), (
            "with the cap, a doc found beyond the pinned base must score exactly as if it were absent"
        )

    def test_rank_beyond_pin_is_a_DEMOTION_without_the_cap(self):
        """The pathology itself — pins why the cap exists."""
        target = uuid4()
        others = [uuid4() for _ in range(60)]

        absent = dict(
            _rrf_merge(
                [(target, 0.9)],
                [(o, 0.5) for o in others],
                30,
                0.7,
                20,
                return_limit=100,
            )
        )[target]
        deep = dict(
            _rrf_merge(
                [(target, 0.9)],
                [(o, 0.5) for o in others[:40]] + [(target, 0.1)],
                30,
                0.7,
                20,
                return_limit=100,
            )
        )[target]

        assert deep < absent, (
            "uncapped, being FOUND at rank 40 scores worse than being absent "
            f"({deep:.6f} < {absent:.6f}) — that is the defect"
        )

    @pytest.mark.parametrize("recall_limit", [1, 10, 20, 31, 40, 50])
    def test_pin_holds_across_the_full_recall_deep_range(self, recall_limit):
        """Codex P2 (#581, heart.py). recall_deep advertises limit 1..50, so
        fetch_limit reaches 100 and the SQL window keeps widening past the
        pinned base — surfacing docs at ever-deeper ranks. The cap is what
        makes the pin hold across that whole range rather than only up to
        penalty_limit * 3.

        Simulates the widening window: as the row count grows, the keyword leg
        returns more rows, so the target appears at a progressively deeper rank.
        """
        target = uuid4()
        fetch = recall_limit * 2 * 3  # heart fetch_limit * the leg's 3x window
        others = [uuid4() for _ in range(max(fetch, 1))]

        # Target appears in the keyword leg only once the window is wide enough.
        keyword = [(o, 0.5) for o in others[:fetch]]
        if fetch > 40:
            keyword = keyword[:40] + [(target, 0.1)] + keyword[40:]

        scored = dict(
            _rrf_merge(
                [(target, 0.9)],
                keyword,
                30,
                0.7,
                20,
                return_limit=10_000,
                cap_ranks_at_penalty=True,
            )
        )

        # Pinned base 20 -> penalty_rank 21 -> vector rank 0, keyword capped 21.
        expected = (0.7 / 30 + 0.3 / 51) * 30
        assert scored[target] == pytest.approx(expected, abs=1e-9), (
            f"score moved at recall_deep limit={recall_limit} (fetch window "
            f"{fetch}) — the pin does not hold across the advertised range"
        )
