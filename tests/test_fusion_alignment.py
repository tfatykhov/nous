"""PR-R fusion alignment: date leg and query-variant fusion, plus the bug fixes
that ride with them.

- C-1: the F075 date leg can join the per-type vector/keyword RRF as a third
  weighted list (``date_leg_inline_rrf``) instead of a second RRF layer.
- C-4: the original query can carry half the cross-variant weight
  (``query_expansion_original_weighted``).
- D-6: a search that runs the variant fusion does not embed the base query.
- C-5: only an explicit date pattern triggers the date-window LLM parse.
- C-10: a datetime string from the parser is a date, not a malformed answer.

Pure-function and mock tests; the Postgres path for C-1 lives in
``tests/heart/test_date_window_leg.py``.
"""

from __future__ import annotations

import datetime
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest

from nous.heart.date_window import DateWindow, DateWindowParser, has_temporal_signal
from nous.heart.search import _rrf_merge, _rrf_merge_n, hybrid_search_multi


def _ranked(ids: list[UUID]) -> list[tuple[UUID, float]]:
    return [(uid, 1.0 - 0.01 * i) for i, uid in enumerate(ids)]


# ---------------------------------------------------------------------------
# C-1: the date leg as a third weighted list inside the per-type merge
# ---------------------------------------------------------------------------


class TestDateListInsideMerge:
    K = 30
    VW = 0.7
    LIMIT = 20  # penalty_rank 21

    def test_out_of_window_top_doc_scores_above_the_stacked_layer(self) -> None:
        """A doc at rank 0 in vector and keyword but outside the date window.

        Stacked (today): the per-type merge gives it rank 0, then the date
        layer fuses [primary, date] at equal weight, so it caps at
        0.5 + 0.5 * k / (k + penalty_rank). Inline it is one list short of
        the maximum, not half of the fusion short.
        """
        top, in_window = uuid4(), uuid4()
        vector = _ranked([top, in_window])
        keyword = _ranked([top])
        date = _ranked([in_window])

        primary = _rrf_merge(vector, keyword, self.K, self.VW, self.LIMIT)
        stacked = dict(_rrf_merge_n([primary, date], self.K, self.LIMIT))
        inline = dict(_rrf_merge(vector, keyword, self.K, self.VW, self.LIMIT, date_ranked=date, date_weight=0.3))

        c = self.K / (self.K + self.LIMIT + 1)
        assert stacked[top] == pytest.approx(0.5 + 0.5 * c)
        assert inline[top] == pytest.approx((1.0 + 0.3 * c) / 1.3)
        assert inline[top] > stacked[top]

    def test_vector_only_out_of_window_doc_scores_no_lower_than_stacked(self) -> None:
        """At weight 0.3 a vector-only doc outside the window loses nothing."""
        top, in_window = uuid4(), uuid4()
        vector = _ranked([top, in_window])
        date = _ranked([in_window])

        primary = _rrf_merge(vector, [], self.K, self.VW, self.LIMIT)
        stacked = dict(_rrf_merge_n([primary, date], self.K, self.LIMIT))
        inline = dict(_rrf_merge(vector, [], self.K, self.VW, self.LIMIT, date_ranked=date, date_weight=0.3))

        assert inline[top] >= stacked[top]

    def test_full_match_scores_one(self) -> None:
        """Rank 0 in all three lists is the maximum, so scores stay in [0, 1]."""
        doc = uuid4()
        out = dict(
            _rrf_merge(
                _ranked([doc]), _ranked([doc]), self.K, self.VW, self.LIMIT, date_ranked=_ranked([doc]), date_weight=0.3
            )
        )
        assert out[doc] == pytest.approx(1.0)

    def test_date_only_doc_enters_the_merge(self) -> None:
        """An in-window doc neither leg returned is still a candidate."""
        a, dated = uuid4(), uuid4()
        out = dict(
            _rrf_merge(_ranked([a]), [], self.K, self.VW, self.LIMIT, date_ranked=_ranked([dated]), date_weight=0.3)
        )
        assert dated in out

    @pytest.mark.parametrize("date_ranked", [None, []])
    def test_no_date_list_is_byte_identical(self, date_ranked) -> None:
        ids = [uuid4() for _ in range(6)]
        vector = _ranked(ids[:5])
        keyword = _ranked([ids[4], ids[5], ids[0]])
        before = _rrf_merge(vector, keyword, self.K, self.VW, self.LIMIT)
        after = _rrf_merge(vector, keyword, self.K, self.VW, self.LIMIT, date_ranked=date_ranked, date_weight=0.3)
        assert after == before

    def test_pinned_cap_applies_to_the_date_list(self) -> None:
        """Past penalty_rank a date rank scores like absence when the base is pinned."""
        docs = [uuid4() for _ in range(30)]
        deep = docs[-1]
        out = dict(
            _rrf_merge(
                [],
                [],
                self.K,
                self.VW,
                5,
                return_limit=100,
                cap_ranks_at_penalty=True,
                date_ranked=_ranked(docs),
                date_weight=0.3,
            )
        )
        absent_term = (self.VW + 0.3 + 0.3) * self.K / (self.K + 6) / 1.3
        assert out[deep] == pytest.approx(absent_term)

    @pytest.mark.asyncio
    async def test_multi_forwards_the_date_list_to_every_variant(self) -> None:
        date = _ranked([uuid4()])
        seen: list[dict] = []

        async def fake_hybrid_search(**kwargs):
            seen.append(kwargs)
            return []

        with patch("nous.heart.search.hybrid_search", new=AsyncMock(side_effect=fake_hybrid_search)):
            await hybrid_search_multi(
                session=AsyncMock(),
                table="heart.facts",
                queries=[("q", [0.1] * 4), ("v1", [0.2] * 4), ("v2", [0.3] * 4)],
                agent_id="nous-test",
                limit=10,
                date_ranked=date,
                date_weight=0.3,
            )
        assert len(seen) == 3
        assert all(kw["date_ranked"] == date and kw["date_weight"] == 0.3 for kw in seen)


# ---------------------------------------------------------------------------
# C-4: the original query carries half the cross-variant weight
# ---------------------------------------------------------------------------


class TestOriginalWeightedFusion:
    K = 30
    LIMIT = 20

    def _lists(self):
        orig_only = uuid4()
        everywhere = uuid4()
        fillers = [[uuid4() for _ in range(10)] for _ in range(3)]
        lists = [
            _ranked([orig_only] + fillers[0][:9] + [everywhere]),
            _ranked(fillers[1] + [everywhere]),
            _ranked(fillers[2] + [everywhere]),
        ]
        return lists, orig_only, everywhere

    def test_equal_weights_let_the_variants_outvote_the_original(self) -> None:
        """Today: rank 0 in the original only loses to rank 10 in all three."""
        lists, orig_only, everywhere = self._lists()
        out = dict(_rrf_merge_n(lists, self.K, self.LIMIT))
        assert out[orig_only] < out[everywhere]

    def test_original_weighted_keeps_the_original_top_hit_ahead(self) -> None:
        lists, orig_only, everywhere = self._lists()
        out = dict(_rrf_merge_n(lists, self.K, self.LIMIT, weights=[0.5, 0.25, 0.25]))
        c = self.K / (self.K + self.LIMIT + 1)
        assert out[orig_only] == pytest.approx(0.5 + 0.5 * c)
        assert out[orig_only] > out[everywhere]

    def test_weights_none_is_byte_identical(self) -> None:
        lists, _, _ = self._lists()
        assert _rrf_merge_n(lists, self.K, self.LIMIT, weights=None) == _rrf_merge_n(lists, self.K, self.LIMIT)

    def test_full_match_scores_one_with_weights(self) -> None:
        doc = uuid4()
        out = dict(_rrf_merge_n([_ranked([doc])] * 3, self.K, self.LIMIT, weights=[0.5, 0.25, 0.25]))
        assert out[doc] == pytest.approx(1.0)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("n_variants", [2, 3])
    async def test_multi_gives_the_first_query_half_the_weight(self, n_variants) -> None:
        orig_only = uuid4()
        everywhere = uuid4()
        per_text = {"q": _ranked([orig_only] + [uuid4() for _ in range(9)] + [everywhere])}
        for i in range(1, n_variants):
            per_text[f"v{i}"] = _ranked([uuid4() for _ in range(10)] + [everywhere])

        async def fake_hybrid_search(**kwargs):
            return per_text[kwargs["query_text"]]

        queries = [(t, [0.1] * 4) for t in per_text]
        with (
            patch("nous.heart.search.hybrid_search", new=AsyncMock(side_effect=fake_hybrid_search)),
            patch("nous.heart.search._resolve_rrf_k", return_value=self.K),
        ):
            weighted = dict(
                await hybrid_search_multi(
                    session=AsyncMock(),
                    table="heart.facts",
                    queries=queries,
                    agent_id="nous-test",
                    limit=self.LIMIT,
                    original_weighted=True,
                )
            )
            equal = dict(
                await hybrid_search_multi(
                    session=AsyncMock(),
                    table="heart.facts",
                    queries=queries,
                    agent_id="nous-test",
                    limit=self.LIMIT,
                )
            )
        c = self.K / (self.K + self.LIMIT + 1)
        assert weighted[orig_only] == pytest.approx(0.5 + 0.5 * c)
        assert weighted[orig_only] > weighted[everywhere]
        assert equal[orig_only] == pytest.approx(1 / n_variants + (n_variants - 1) / n_variants * c)


@pytest.mark.asyncio
@pytest.mark.parametrize("flag", [True, False])
async def test_heart_threads_original_weighted_to_every_hybrid_type(heart, flag) -> None:
    """Heart reads the flag from its own Settings, so an eval's model_copy reaches it."""
    heart.settings = heart.settings.model_copy(
        update={"query_expansion_enabled": True, "query_expansion_original_weighted": flag}
    )
    heart._query_expander = SimpleNamespace(expand=AsyncMock(return_value=["q one two", "v one", "v two"]))
    mocks = {name: AsyncMock(return_value=[]) for name in ("episodes", "facts", "procedures")}
    for name, mock in mocks.items():
        getattr(heart, name).search = mock

    await heart.recall("q one two", limit=5, types=["episode", "fact", "procedure"])

    for name, mock in mocks.items():
        assert mock.await_count == 1, name
        assert mock.await_args.kwargs["original_weighted"] is flag, name
        assert len(mock.await_args.kwargs["variant_pairs"]) == 3, name


# ---------------------------------------------------------------------------
# D-6: the variant fusion carries its own vectors; no base-query embed
# ---------------------------------------------------------------------------


class _CountingEmbeddings:
    def __init__(self) -> None:
        self.embed_calls: list[str] = []

    async def embed(self, text: str, fail_fast: bool = False) -> list[float]:
        self.embed_calls.append(text)
        return [0.1] * 4


_VARIANTS = [("the query", [0.1] * 4), ("a variant", [0.2] * 4), ("another one", [0.3] * 4)]


@pytest.mark.asyncio
async def test_episode_search_with_variants_does_not_embed_the_base_query() -> None:
    from nous.heart.episodes import EpisodeManager

    emb = _CountingEmbeddings()
    mgr = EpisodeManager(None, emb, "nous-test")
    with patch("nous.heart.episodes.hybrid_search_multi", new=AsyncMock(return_value=[])) as multi:
        assert await mgr._search("the query", 10, AsyncMock(), variant_pairs=_VARIANTS) == []
    assert multi.await_count == 1
    assert emb.embed_calls == []


@pytest.mark.asyncio
async def test_procedure_search_with_variants_does_not_embed_the_base_query() -> None:
    from nous.heart.procedures import ProcedureManager

    emb = _CountingEmbeddings()
    mgr = ProcedureManager(None, emb, "nous-test")
    with patch("nous.heart.procedures.hybrid_search_multi", new=AsyncMock(return_value=[])) as multi:
        assert await mgr._search("the query", 10, None, None, AsyncMock(), variant_pairs=_VARIANTS) == []
    assert multi.await_count == 1
    assert emb.embed_calls == []


@pytest.mark.asyncio
async def test_fact_search_with_variants_and_no_window_does_not_embed_the_base_query() -> None:
    from nous.heart.facts import FactManager

    emb = _CountingEmbeddings()
    mgr = FactManager(None, emb, "nous-test")
    with patch("nous.heart.facts.hybrid_search_multi", new=AsyncMock(return_value=[])) as multi:
        out = await mgr._search("the query", 10, None, True, None, AsyncMock(), variant_pairs=_VARIANTS)
    assert out == []
    assert multi.await_count == 1
    assert emb.embed_calls == []


@pytest.mark.asyncio
async def test_fact_search_with_variants_and_a_window_still_embeds_for_the_date_leg() -> None:
    """The date leg ranks by the base query's vector, so that embed is live."""
    from nous.heart.facts import FactManager

    emb = _CountingEmbeddings()
    mgr = FactManager(None, emb, "nous-test")
    mgr._date_window_leg = AsyncMock(return_value=[])
    window = DateWindow(start=datetime.date(2026, 4, 1), end=datetime.date(2026, 4, 30))
    with patch("nous.heart.facts.hybrid_search_multi", new=AsyncMock(return_value=[])):
        await mgr._search("the query", 10, None, True, None, AsyncMock(), variant_pairs=_VARIANTS, date_window=window)
    assert emb.embed_calls == ["the query"]
    assert mgr._date_window_leg.await_count == 1


@pytest.mark.asyncio
async def test_single_query_search_still_embeds() -> None:
    from nous.heart.episodes import EpisodeManager

    emb = _CountingEmbeddings()
    mgr = EpisodeManager(None, emb, "nous-test")
    with patch("nous.heart.episodes.hybrid_search", new=AsyncMock(return_value=[])):
        await mgr._search("the query", 10, AsyncMock(), variant_pairs=None)
    assert emb.embed_calls == ["the query"]


# ---------------------------------------------------------------------------
# C-5: only an explicit date pattern reaches the date-window LLM parse
# ---------------------------------------------------------------------------

# The trigger as it stood before C-5, kept verbatim so the subset property
# below is checked against the real old behaviour.
_OLD_MONTHS = (
    r"jan(uary)?|feb(ruary)?|mar(ch)?|apr(il)?|may|jun(e)?|jul(y)?|"
    r"aug(ust)?|sep(tember)?|oct(ober)?|nov(ember)?|dec(ember)?"
)
_OLD_TEMPORAL_RE = re.compile(
    r"\b(" + _OLD_MONTHS + r")\b"
    r"|\b(19|20)\d{2}\b"
    r"|\d{4}-\d{2}-\d{2}"
    r"|\b(yesterday|today|tomorrow|last|this|next|recent(ly)?|ago|"
    r"early|mid|late|around|when|during|after|before|since|"
    r"week|month|quarter)\b",
    re.IGNORECASE,
)

_NO_DATE = [
    "when did we deploy the scorer?",
    "what happened after the merge?",
    "what should I do next?",
    "What may help me focus?",
    "is this the right approach for the cache?",
    "what was the last error in the job runner?",
    "remind me of the recent decision about caching",
    "what changed before the refactor?",
    "since the outage, how is the retry loop?",
    "how do I set the monthly budget?",
    "summarize the week",
    "how early can the sleep cycle start?",
    "around how many facts are active?",
    "it worked a while ago",
]

_EXPLICIT = [
    "What happened in late April 2026?",
    "changes around mid-May",
    "events on 2026-06-24",
    "what happened this week?",
    "what changed next month?",
    "summarize this quarter",
    "something about last quarter's vibe",
    "what happened yesterday?",
    "plan for today",
    "what did we ship last tuesday?",
    "notes from 3 days ago",
    "a few weeks ago we changed the scorer",
    "what did we decide in May?",
    "the May 5 meeting",
    "the 5th of May release",
    "over the past week",
    "in the last two months",
    "what failed since monday?",
    "recent weeks of deploys",
    "budget for 2025",
    "this morning's run",
]


@pytest.mark.parametrize("query", _NO_DATE)
def test_vague_words_without_a_date_do_not_trigger(query: str) -> None:
    assert has_temporal_signal(query) is False


@pytest.mark.parametrize("query", _EXPLICIT)
def test_explicit_dates_still_trigger(query: str) -> None:
    assert has_temporal_signal(query) is True


@pytest.mark.parametrize("query", _NO_DATE + _EXPLICIT)
def test_trigger_only_narrows(query: str) -> None:
    """Every query the trigger passes also passed the old one: no new LLM calls."""
    if has_temporal_signal(query):
        assert _OLD_TEMPORAL_RE.search(query)


class _FakeClient:
    def __init__(self, tool_input) -> None:
        self._input = tool_input
        self.calls: list[dict] = []

    async def call(self, payload):
        self.calls.append(payload)
        return SimpleNamespace(content=[{"type": "tool_use", "name": "emit_window", "input": self._input}])


def _parser_settings(**kw):
    base = dict(
        date_leg_model="claude-haiku-5-5", date_leg_timeout_seconds=2.0, date_leg_max_per_hour=500, date_leg_pad_days=2
    )
    base.update(kw)
    return SimpleNamespace(**base)


TODAY = datetime.date(2026, 7, 1)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query", ["when did we deploy the scorer?", "what happened after the merge?", "what should I do next?"]
)
async def test_no_date_words_means_no_llm_call(query: str) -> None:
    client = _FakeClient({"has_date": True, "start_date": "2026-01-01", "end_date": "2026-01-02"})
    p = DateWindowParser(client, _parser_settings())
    assert await p.parse(query, TODAY) is None
    assert client.calls == []


# ---------------------------------------------------------------------------
# C-10: datetime strings from the parser are dates
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "start,end",
    [
        ("2026-04-20T00:00:00", "2026-04-30T23:59:59"),
        ("2026-04-20T00:00:00Z", "2026-04-30T00:00:00Z"),
        ("2026-04-20 09:00", "2026-04-30 18:00"),
    ],
)
async def test_datetime_strings_parse_and_the_window_is_cached(start: str, end: str) -> None:
    client = _FakeClient({"has_date": True, "start_date": start, "end_date": end})
    p = DateWindowParser(client, _parser_settings())
    q = "what happened in late April 2026?"
    expected = DateWindow(start=datetime.date(2026, 4, 18), end=datetime.date(2026, 5, 2))

    assert await p.parse(q, TODAY) == expected
    assert await p.parse(q, TODAY) == expected  # served from the cache
    assert len(client.calls) == 1


# ---------------------------------------------------------------------------
# Settings: both new flags land dark
# ---------------------------------------------------------------------------


def test_new_settings_default_off() -> None:
    from nous.config import Settings

    s = Settings(_env_file=None)
    assert s.date_leg_inline_rrf is False
    assert s.date_leg_rrf_weight == 0.3
    assert s.query_expansion_original_weighted is False
