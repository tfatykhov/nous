"""A4 Step 2: the any-word keyword fallback (lands dark behind a flag).

The hard constraint under test: a fallback row's KEYWORD TERM never falls below
the absent penalty. `_rrf_merge` scores an absent document at `penalty_rank =
limit + 1`, and past that rank an observed rank scores WORSE than absence -- so
the number of appended rows is bounded, and the bound is tight. (This is a claim
about the keyword term. A boosted fallback document can still push another
document off a per-variant slice; `test_a_fallback_document_can_displace_...`
pins that so the claim is not read as more than it is.) These tests run the real
`_rrf_merge`; the SQL itself is exercised on real Postgres in
tests/integration/test_a4_keyword_fallback_pg.py, which cannot run here.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import UUID

import pytest

import nous.heart.search as search_mod
from nous.heart.search import (
    _FINGERPRINT_VARS,
    _fallback_slots,
    _or_tsquery,
    _rrf_merge,
    hybrid_search,
    hybrid_search_multi,
    with_keyword_stats,
)

ON = "nous.heart.search._resolve_keyword_or_fallback_enabled"


@pytest.fixture(autouse=True)
def _fresh_df_cache():
    search_mod._DF_CACHE.clear()
    search_mod._DF_REFRESHING.clear()
    yield
    for task in list(search_mod._DF_REFRESHING.values()):
        task.cancel()
    search_mod._DF_CACHE.clear()
    search_mod._DF_REFRESHING.clear()


async def _settle():
    for _ in range(10):
        await asyncio.sleep(0)


def _ids(n: int, base: int = 1) -> list[UUID]:
    return [UUID(int=base + i) for i in range(n)]


LEXEMES = ["alpha", "bravo", "charlie", "delta"]


class _Sess:
    """Routes on statement text. The fallback family carries markers
    (`or_lexemes`, `or_stat`, `or_fallback`) that are checked BEFORE `ts_rank_cd`,
    which the fallback statement also contains.

    Constructing one PRIMES the document-frequency cache for ("heart.facts",
    "agent"), because a recall never loads it itself (a cold key skips the
    fallback); tests of the cold / stale paths clear or age the entry after."""

    def __init__(self, vector, keyword, fallback=(), lexemes=LEXEMES, df=None):
        self.vector, self.keyword, self.fallback = vector, keyword, fallback
        self.lexemes = lexemes
        self.df = df if df is not None else {w: 10 + i for i, w in enumerate(lexemes)}
        self.bind = object()
        self.sqls: list[str] = []
        self.params: list[dict] = []
        search_mod._DF_CACHE[("heart.facts", "agent")] = (time.monotonic(), self.df)

    def calls(self, marker: str) -> list[dict]:
        return [p for s, p in zip(self.sqls, self.params) if marker in s]

    @property
    def fallback_calls(self) -> list[dict]:
        return self.calls("or_fallback")

    async def execute(self, sql, params=None):
        text = str(sql)
        self.sqls.append(text)
        self.params.append(dict(params or {}))
        if "or_lexemes" in text:
            rows = [SimpleNamespace(lexemes=list(self.lexemes))]
        elif "or_fallback" in text:
            rows = [SimpleNamespace(id=i, sc=s) for i, s in self.fallback]
        elif "ts_rank_cd" in text:
            rows = [SimpleNamespace(id=i, score=s) for i, s in self.keyword]
        elif "<=>" in text:
            rows = [SimpleNamespace(id=i, score=s) for i, s in self.vector]
        else:
            rows = []
        return MagicMock(all=lambda: rows)


# ---------------------------------------------------------------------------
# The cap
# ---------------------------------------------------------------------------


class TestSlots:
    @pytest.mark.parametrize(
        ("n_and", "limit", "base", "expected"),
        [
            (0, 20, 20, 22),    # indices 0..21 -> penalty_rank is 21
            (5, 20, 20, 17),
            (19, 20, 20, 3),
            (20, 20, 20, 0),    # the all-words query already filled `limit`
            (30, 20, 20, 0),
            (0, 60, 20, 22),    # pinned: the cap follows the PIN, not the row count
            (30, 60, 20, 0),    # beyond the pinned horizon there is nothing legal
        ],
    )
    def test_slots(self, n_and, limit, base, expected):
        assert _fallback_slots(n_and, limit, base) == expected


def _merge(vector, keyword_docs, k, vw, base, pinned, return_limit=10_000):
    return dict(_rrf_merge(
        vector, [(d, 1.0) for d in keyword_docs], k, vw, base,
        return_limit=return_limit, cap_ranks_at_penalty=pinned,
    ))


class TestKeywordTermNeverBelowAbsent:
    @pytest.mark.parametrize("pinned", [False, True], ids=["unpinned", "pinned"])
    def test_property_over_random_lists(self, pinned):
        """Compares SCORES of the same document with and without the fallback
        rows (`return_limit` is large only so every score is observable; the
        claim is about the keyword term, not about which rows survive a slice)."""
        rng = random.Random(20261016)
        in_vector = keyword_only = 0
        for _ in range(400):
            limit = rng.randint(3, 30)
            base = rng.randint(3, 30) if pinned else limit
            k = rng.choice([30, 60])
            vw = rng.choice([0.5, 0.7, 0.9])
            pool = _ids(4 * max(limit, base) + 12)
            penalty_rank = base + 1
            vector_docs = rng.sample(pool, rng.randint(penalty_rank + 1, 3 * base))
            vector = [(d, 1.0) for d in vector_docs]
            n_and = rng.randint(0, limit - 1)
            and_docs = rng.sample(pool, n_and)
            slots = _fallback_slots(n_and, limit, base)
            rest = [d for d in pool if d not in set(and_docs)]
            fb_docs = rng.sample(rest, min(slots, len(rest)))

            off = _merge(vector, and_docs, k, vw, base, pinned)
            on = _merge(vector, and_docs + fb_docs, k, vw, base, pinned)

            # a document absent from BOTH lists scores like the vector document
            # sitting exactly at the penalty rank and absent from keyword
            probe = vector_docs[penalty_rank]
            probe_ok = probe not in set(and_docs) | set(fb_docs)
            for d in fb_docs:
                if d in dict(vector):
                    assert on[d] >= off[d] - 1e-12
                    in_vector += 1
                elif probe_ok:
                    assert on[d] >= off[probe] - 1e-12
                    keyword_only += 1
            # nothing else may move: all-words rows keep their rank, and the
            # fallback is appended after them
            for d in off:
                if d not in fb_docs:
                    assert on[d] == pytest.approx(off[d])
        assert in_vector > 100 and keyword_only > 100, "property must not be vacuous"

    def test_the_bound_is_tight_one_more_row_is_strictly_worse(self):
        """Presence at index penalty_rank ties absence; one index later loses."""
        limit = base = 5
        k, vw = 30, 0.7
        target = UUID(int=1)
        vector = [(target, 1.0)] + [(d, 1.0) for d in _ids(10, base=100)]
        absent = _merge(vector, [], k, vw, base, False)[target]
        slots = _fallback_slots(0, limit, base)           # 7: indices 0..6
        pad = _ids(slots - 1, base=1000)                  # fills indices 0..5

        at_last_legal = _merge(vector, pad + [target], k, vw, base, False)[target]
        one_too_many = _merge(vector, pad + [UUID(int=2000), target], k, vw, base, False)[target]

        assert at_last_legal == pytest.approx(absent)     # index 6 == penalty_rank
        assert one_too_many < absent - 1e-9               # index 7 is past it
        assert slots == base + 2

    def test_a_fallback_document_can_displace_another_under_a_slice(self):
        """What the cap does NOT promise: with a per-variant slice (the default
        `return_limit == limit`, as under query expansion) a boosted fallback
        document can take a slot from a vector-only document. That is the
        experiment's intended effect, not a violation of the keyword-term bound."""
        limit = base = 3
        k, vw = 30, 0.5
        vector = [(d, 1.0) for d in _ids(8, base=100)]
        x = UUID(int=1)                                    # keyword-only fallback doc
        off = _merge(vector, [], k, vw, base, False, return_limit=limit)
        on = _merge(vector, [x], k, vw, base, False, return_limit=limit)
        assert x in on and x not in off
        assert set(off) - set(on), "a previously returned document was pushed off"


# ---------------------------------------------------------------------------
# hybrid_search wiring
# ---------------------------------------------------------------------------


def _fixture():
    ids = _ids(30)
    vector = [(i, 0.9 - 0.01 * n) for n, i in enumerate(ids)]
    keyword = [(ids[3], 0.5), (ids[1], 0.4)]                       # 2 all-words rows
    fallback = [(i, 3.0 - 0.1 * n) for n, i in enumerate(_ids(8, base=500))]
    return vector, keyword, fallback


async def _go(sess, *, flag=True, sink=None, **kw):
    kw.setdefault("limit", 5)
    kw.setdefault("embedding", [0.1, 0.2])
    with patch(ON, return_value=flag):
        return await with_keyword_stats(sink, hybrid_search(
            sess, "heart.facts", kw.pop("embedding"), "a b c d", "agent", **kw))


class TestWiring:
    @pytest.mark.asyncio
    async def test_flag_off_runs_exactly_the_statements_it_ran_before(self):
        vector, keyword, fallback = _fixture()
        s = _Sess(vector, keyword, fallback)
        await _go(s, flag=False, any_word_fallback=True)
        assert len(s.sqls) == 2 and not s.fallback_calls

    @pytest.mark.asyncio
    async def test_flag_on_without_the_call_site_opt_in_changes_nothing(self):
        """The graph densifier and every other caller that did not ask."""
        vector, keyword, fallback = _fixture()
        s = _Sess(vector, keyword, fallback)
        await _go(s, flag=True)
        assert len(s.sqls) == 2

    @pytest.mark.asyncio
    async def test_flag_on_and_opted_in_tops_up_thin_keyword_results(self):
        vector, keyword, fallback = _fixture()
        s = _Sess(vector, keyword, fallback)
        sink: list = []
        await _go(s, any_word_fallback=True, sink=sink)
        (call,) = s.fallback_calls
        assert call["fallback_cap"] == 5                    # 5 + 2 - 2 all-words rows
        assert call["and_ids"] == [i for i, _ in keyword]
        # rarest-first, all four usable lexemes
        assert call["or_query"] == "'alpha' | 'bravo' | 'charlie' | 'delta'"
        # n_keyword stays the ALL-WORDS count; the experiment never changes
        # what the zero-hit metric means.
        assert sink == [{"n_keyword": 2, "n_keyword_fallback": 5}]

    @pytest.mark.asyncio
    async def test_a_fallback_row_only_ever_helps_the_documents_it_adds(self):
        vector, keyword, fallback = _fixture()
        off = dict(await _go(_Sess(vector, keyword, fallback), limit=100,
                             any_word_fallback=True, flag=False))
        on = dict(await _go(_Sess(vector, keyword, fallback), limit=100,
                            any_word_fallback=True, flag=True))
        added = {i for i, _ in fallback[:_fallback_slots(2, 100, 100)]}
        assert added & set(on)
        for doc, score in off.items():
            assert on[doc] >= score - 1e-12
            if doc not in added:
                assert on[doc] == pytest.approx(score)

    @pytest.mark.asyncio
    async def test_an_all_words_document_returned_again_is_not_duplicated(self):
        """The SQL excludes the all-words ids, but correctness of the cap must not
        rest on that clause: a document taken twice would score by its LATER
        index, which can be past the penalty rank."""
        vector, keyword, fallback = _fixture()
        and_id = keyword[0][0]
        leaky = [(and_id, 9.0)] + fallback          # SQL "failed" to exclude it
        base = dict(await _go(_Sess(vector, keyword, fallback), limit=100,
                              any_word_fallback=True, flag=False))
        s = _Sess(vector, keyword, leaky)
        sink: list = []
        out = await _go(s, limit=100, any_word_fallback=True, sink=sink)
        ids = [d for d, _ in out]
        assert len(ids) == len(set(ids))
        assert dict(out)[and_id] == pytest.approx(base[and_id])    # score unchanged
        assert sink[0]["n_keyword_fallback"] == len(fallback)       # leaked row not counted

    @pytest.mark.asyncio
    async def test_require_keyword_hit_keeps_all_words_semantics(self):
        """The Session Profile leg's relevance gate. Any-word would pass it on
        any shared word."""
        vector, keyword, fallback = _fixture()
        s = _Sess(vector, keyword, fallback)
        out = await _go(s, any_word_fallback=True, require_keyword_hit=True)
        assert len(s.sqls) == 2
        assert {d for d, _ in out} <= {d for d, _ in keyword}

    @pytest.mark.asyncio
    async def test_keyword_only_exit_is_excluded(self):
        vector, keyword, fallback = _fixture()
        s = _Sess([], keyword, fallback)
        await _go(s, any_word_fallback=True, embedding=None)
        assert not s.fallback_calls

    @pytest.mark.asyncio
    async def test_a_full_all_words_result_is_left_alone(self):
        vector, _, fallback = _fixture()
        full = [(i, 0.5) for i in _ids(5, base=900)]        # == limit
        s = _Sess(vector, full, fallback)
        await _go(s, any_word_fallback=True)
        assert len(s.sqls) == 2

    @pytest.mark.asyncio
    async def test_keyword_leg_disabled_means_no_fallback_either(self):
        vector, keyword, fallback = _fixture()
        s = _Sess(vector, keyword, fallback)
        with patch("nous.heart.search._resolve_keyword_enabled", return_value=False):
            await _go(s, any_word_fallback=True)
        assert len(s.sqls) == 1

    @pytest.mark.asyncio
    async def test_pinned_cap_follows_the_pin_not_the_row_count(self):
        vector, keyword, fallback = _fixture()
        s = _Sess(vector, keyword, fallback)
        await _go(s, any_word_fallback=True, limit=60, penalty_limit=10)
        (call,) = s.fallback_calls
        assert call["fallback_cap"] == 10 + 2 - 2

    @pytest.mark.asyncio
    async def test_multi_forwards_the_opt_in_to_every_variant(self):
        vector, keyword, fallback = _fixture()
        s = _Sess(vector, keyword, fallback)
        queries = [("a b c d", [0.1, 0.2]), ("e f g h", [0.2, 0.1]), ("i j k l", [0.3, 0.3])]
        with patch(ON, return_value=True):
            await hybrid_search_multi(s, "heart.facts", queries, "agent", limit=5,
                                      any_word_fallback=True)
        assert len(s.fallback_calls) == 3


class TestLexemeSelection:
    @pytest.mark.asyncio
    async def test_a_single_lexeme_query_runs_no_fallback_and_no_stat_query(self):
        vector, keyword, fallback = _fixture()
        s = _Sess(vector, keyword, fallback, lexemes=["alpha"])
        await _go(s, any_word_fallback=True)
        assert not s.fallback_calls

    @pytest.mark.asyncio
    async def test_an_all_stopword_query_has_no_lexemes(self):
        vector, keyword, fallback = _fixture()
        s = _Sess(vector, keyword, fallback, lexemes=[])
        await _go(s, any_word_fallback=True)
        assert not s.fallback_calls

    @pytest.mark.asyncio
    async def test_lexemes_absent_from_the_table_are_dropped(self):
        vector, keyword, fallback = _fixture()
        s = _Sess(vector, keyword, fallback, lexemes=["alpha", "ghost"], df={"alpha": 4})
        await _go(s, any_word_fallback=True)
        (call,) = s.fallback_calls
        assert call["or_query"] == "'alpha'"

    @pytest.mark.asyncio
    async def test_none_in_the_table_means_no_fallback_statement(self):
        vector, keyword, fallback = _fixture()
        s = _Sess(vector, keyword, fallback, lexemes=["ghost", "wraith"], df={})
        await _go(s, any_word_fallback=True)
        assert not s.fallback_calls

    @pytest.mark.asyncio
    async def test_only_the_eight_rarest_lexemes_are_used(self):
        words = [f"w{i:02d}" for i in range(12)]
        df = {w: 100 - i for i, w in enumerate(words)}        # w11 is rarest
        vector, keyword, fallback = _fixture()
        s = _Sess(vector, keyword, fallback, lexemes=words, df=df)
        await _go(s, any_word_fallback=True)
        (call,) = s.fallback_calls
        used = [t.strip("'") for t in call["or_query"].split(" | ")]
        assert used == [f"w{i:02d}" for i in range(11, 3, -1)]
        assert search_mod._OR_MAX_LEXEMES == 8

    def test_quoting(self):
        assert _or_tsquery(["a b", "o'x", "c\\d"]) == "'a b' | 'o''x' | 'c\\\\d'"


KEY = ("heart.facts", "agent")


def _loader(result=None, error=None, gate=None, calls=None):
    async def load(bind, table, agent):
        if calls is not None:
            calls.append((table, agent))
        if gate is not None:
            await gate.wait()
        if error is not None:
            raise error
        return result

    return load


class TestDocFreqCacheNeverBlocksARecall:
    """ts_stat costs 25-125 ms and grows with the table: it must never run inside
    a user-facing recall."""

    @pytest.mark.asyncio
    async def test_a_stale_entry_is_served_while_a_refresh_is_scheduled(self):
        vector, keyword, fallback = _fixture()
        s = _Sess(vector, keyword, fallback)
        old_df = s.df
        search_mod._DF_CACHE[KEY] = (time.monotonic() - search_mod._DF_TTL_SECONDS - 1, old_df)
        gate, calls = asyncio.Event(), []
        with patch.object(search_mod, "_load_doc_freq", _loader({"zulu": 3}, gate=gate, calls=calls)):
            await _go(s, any_word_fallback=True)
            assert len(s.fallback_calls) == 1                   # served the stale df
            assert KEY in search_mod._DF_REFRESHING
            assert search_mod._DF_CACHE[KEY][1] is old_df       # not replaced mid-recall
            gate.set()
            await _settle()
        assert search_mod._DF_CACHE[KEY][1] == {"zulu": 3}
        assert KEY not in search_mod._DF_REFRESHING
        assert calls == [KEY]

    @pytest.mark.asyncio
    async def test_a_cold_key_skips_the_fallback_and_schedules_a_fill(self):
        vector, keyword, fallback = _fixture()
        s = _Sess(vector, keyword, fallback)
        search_mod._DF_CACHE.clear()
        sink: list = []
        with patch.object(search_mod, "_load_doc_freq", _loader(s.df)):
            await _go(s, any_word_fallback=True, sink=sink)
            assert not s.fallback_calls and not s.calls("or_lexemes")
            assert sink == [{"n_keyword": 2, "n_keyword_fallback": None}]
            assert KEY in search_mod._DF_REFRESHING
            await _settle()
            await _go(s, any_word_fallback=True)                # warm now
        assert len(s.fallback_calls) == 1

    @pytest.mark.asyncio
    async def test_concurrent_calls_start_only_one_refresh(self):
        vector, keyword, fallback = _fixture()
        sessions = [_Sess(vector, keyword, fallback) for _ in range(3)]
        search_mod._DF_CACHE.clear()
        gate, calls = asyncio.Event(), []
        with patch.object(search_mod, "_load_doc_freq", _loader({"alpha": 1}, gate=gate, calls=calls)):
            await asyncio.gather(*(_go(x, any_word_fallback=True) for x in sessions))
            assert len(calls) == 1
            gate.set()
            await _settle()
        assert len(calls) == 1 and KEY in search_mod._DF_CACHE

    @pytest.mark.asyncio
    async def test_a_refresh_error_leaves_the_old_entry_in_place(self, caplog):
        vector, keyword, fallback = _fixture()
        s = _Sess(vector, keyword, fallback)
        stale = (time.monotonic() - search_mod._DF_TTL_SECONDS - 1, s.df)
        search_mod._DF_CACHE[KEY] = stale
        caplog.set_level(logging.WARNING)
        with patch.object(search_mod, "_load_doc_freq", _loader(error=RuntimeError("db down"))):
            await _go(s, any_word_fallback=True)                # must not raise
            await _settle()
        assert search_mod._DF_CACHE[KEY][1] is s.df             # the old df is still served
        assert search_mod._DF_CACHE[KEY][0] > stale[0]          # ...its retry pushed back
        assert KEY not in search_mod._DF_REFRESHING             # a later call can retry
        assert "doc-frequency refresh failed" in caplog.text

    @pytest.mark.asyncio
    async def test_a_failed_first_fill_backs_off_with_an_empty_entry(self):
        vector, keyword, fallback = _fixture()
        s = _Sess(vector, keyword, fallback)
        search_mod._DF_CACHE.clear()
        with patch.object(search_mod, "_load_doc_freq", _loader(error=RuntimeError("x"))):
            await _go(s, any_word_fallback=True)
            await _settle()
            assert search_mod._DF_CACHE[KEY][1] == {}           # warm but EMPTY
            await _go(s, any_word_fallback=True)                # still just skips
        assert not s.fallback_calls and not s.calls("or_lexemes")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("had_entry", [False, True], ids=["cold", "stale"])
    async def test_a_failed_refresh_backs_off_then_retries(self, had_entry, caplog):
        """With the flag on and a ts_stat that always fails, every recall used to
        schedule a new refresh and log a new traceback. A failure now stamps the
        key fresh for `_DF_RETRY_SECONDS`; once that window passes, ONE refresh is
        scheduled again. A previous df keeps being served throughout."""
        vector, keyword, fallback = _fixture()
        s = _Sess(vector, keyword, fallback)
        old_df = s.df
        if had_entry:
            search_mod._DF_CACHE[KEY] = (time.monotonic() - search_mod._DF_TTL_SECONDS - 1, old_df)
        else:
            search_mod._DF_CACHE.clear()
        caplog.set_level(logging.WARNING)
        calls: list = []
        with patch.object(search_mod, "_load_doc_freq", _loader(error=RuntimeError("x"), calls=calls)):
            await _go(s, any_word_fallback=True)
            await _settle()
            assert calls == [KEY]
            loaded_at, df = search_mod._DF_CACHE[KEY]
            assert (df is old_df) if had_entry else (df == {})
            age = time.monotonic() - loaded_at
            assert search_mod._DF_TTL_SECONDS - search_mod._DF_RETRY_SECONDS <= age < search_mod._DF_TTL_SECONDS

            await _go(s, any_word_fallback=True)                # immediately after: backing off
            await _settle()
            assert calls == [KEY] and KEY not in search_mod._DF_REFRESHING
            assert caplog.text.count("doc-frequency refresh failed") == 1

            search_mod._DF_CACHE[KEY] = (loaded_at - search_mod._DF_RETRY_SECONDS, df)   # window passes
            await _go(s, any_word_fallback=True)
            await _settle()
        assert calls == [KEY, KEY]
        # the old df was served on every call; a cold key ran nothing at all
        assert len(s.fallback_calls) == (3 if had_entry else 0)
        assert search_mod._DF_RETRY_SECONDS == 300.0

    @pytest.mark.asyncio
    async def test_a_session_without_a_bind_cannot_schedule_and_just_skips(self):
        vector, keyword, fallback = _fixture()
        s = _Sess(vector, keyword, fallback)
        s.bind = None
        search_mod._DF_CACHE.clear()
        await _go(s, any_word_fallback=True)
        assert not s.fallback_calls and not search_mod._DF_REFRESHING

    @pytest.mark.asyncio
    async def test_the_cache_is_per_table_and_agent(self):
        vector, keyword, fallback = _fixture()
        s = _Sess(vector, keyword, fallback)
        search_mod._DF_CACHE.clear()
        calls: list = []
        with patch.object(search_mod, "_load_doc_freq", _loader({"alpha": 1}, calls=calls)):
            with patch(ON, return_value=True):
                for table, agent in (("heart.facts", "a"), ("heart.facts", "b"),
                                     ("heart.episodes", "a")):
                    await hybrid_search(s, table, [0.1, 0.2], "a b c d", agent, limit=5,
                                        any_word_fallback=True)
            await _settle()
        assert sorted(calls) == [("heart.episodes", "a"), ("heart.facts", "a"), ("heart.facts", "b")]


# ---------------------------------------------------------------------------
# Flag plumbing
# ---------------------------------------------------------------------------


class TestFlag:
    def test_default_is_off(self):
        from nous.config import Settings

        assert Settings(_env_file=None).hybrid_keyword_or_fallback_enabled is False

    def test_the_resolver_reads_the_environment_live(self, monkeypatch):
        monkeypatch.setenv("NOUS_HYBRID_KEYWORD_OR_FALLBACK_ENABLED", "true")
        assert search_mod._resolve_keyword_or_fallback_enabled() is True
        monkeypatch.setenv("NOUS_HYBRID_KEYWORD_OR_FALLBACK_ENABLED", "false")
        assert search_mod._resolve_keyword_or_fallback_enabled() is False

    def test_the_flag_is_part_of_the_cache_fingerprint(self):
        assert "NOUS_HYBRID_KEYWORD_OR_FALLBACK_ENABLED" in _FINGERPRINT_VARS

    def test_a_settings_failure_fails_closed(self, monkeypatch):
        monkeypatch.setenv("NOUS_RRF_K", "31")       # a fingerprint nothing cached
        with patch("nous.config.Settings", side_effect=Exception("boom")):
            assert search_mod._resolve_keyword_or_fallback_enabled() is False
