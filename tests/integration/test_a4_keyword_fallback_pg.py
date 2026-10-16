"""A4 Step 2 against real Postgres: the SQL the mocked tests cannot run.

Two gates, both required: pytest's ``--integration`` flag (tests/conftest.py
skips everything under tests/integration/ without it, whatever :5433 says) and
the eval Postgres on 127.0.0.1:5433 (database nous_eval_scratch). Missing either,
it is SKIPPED -- including on CI. Run it by hand before the fallback is ever
enabled:

    uv run --extra dev pytest tests/integration/test_a4_keyword_fallback_pg.py --integration

Rows are written under a throwaway agent id and deleted afterwards (chunks
cascade from the episode), mirroring test_r2_chunk_hybrid_e2e.py.

WARNING: the `eval_db` fixture uses `os.environ.setdefault`, so an exported
DB_HOST / DB_PORT / DB_NAME / DB_USER / DB_PASSWORD redirects this test's WRITES
to whatever database those name. Unset them (or point them at the scratch DB)
before running; never run it with prod credentials exported.

Fixture: the query is "alpha bravo charlie delta". Only chunk A contains all four
words (the all-words match). B has one of them, C has one, D none. No chunk has
an embedding, so the vector leg is empty and the result is the keyword list.
"""
from __future__ import annotations

import asyncio
import os
import time
import uuid

import pytest


def _eval_db_reachable() -> bool:
    import socket
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(1.0)
            return s.connect_ex(("127.0.0.1", 5433)) == 0
    except OSError:
        return False


pytestmark = pytest.mark.skipif(
    not _eval_db_reachable(),
    reason="nous-eval-scratch container not reachable on 127.0.0.1:5433",
)

QUERY = "alpha bravo charlie delta"
PUNCT = "it's \"quoted\" a:b c&d e|f (g) ! \\ <-> 日本語"


@pytest.fixture
async def eval_db():
    for k, v in {
        "DB_HOST": "127.0.0.1", "DB_PORT": "5433", "DB_NAME": "nous_eval_scratch",
        "DB_USER": "nous", "DB_PASSWORD": "nous_eval",
        "NOUS_HEARTBEAT_ENABLED": "false", "NOUS_SUBTASK_ENABLED": "false",
        "NOUS_SCHEDULE_ENABLED": "false", "NOUS_SLEEP_ENABLED": "false",
        "NOUS_EVENT_BUS_ENABLED": "false",
        "NOUS_ACTIONABILITY_BACKFILL_ON_STARTUP": "false",
        "NOUS_TELEGRAM_BOT_TOKEN": "",
    }.items():
        os.environ.setdefault(k, v)

    from nous.config import Settings
    from nous.storage.database import Database

    db = Database(Settings())
    await db.connect()
    yield db
    await db.disconnect()


@pytest.fixture
async def chunks(eval_db):
    from sqlalchemy import text

    import nous.heart.search as search_mod

    search_mod._DF_CACHE.clear()
    search_mod._DF_REFRESHING.clear()
    agent_id = f"a4-test-{uuid.uuid4().hex[:8]}"
    episode_id = uuid.uuid4()
    ids = {k: uuid.uuid4() for k in "ABCD"}
    content = {
        "A": "alpha bravo charlie delta echo foxtrot",
        "B": "alpha was mentioned once in passing",
        "C": "a single bravo appears here",
        "D": "nothing relevant in this chunk at all",
    }
    async with eval_db.session() as s:
        await s.execute(text(
            "INSERT INTO heart.episodes (id, agent_id, summary) VALUES (:id, :a, 'a4 test')"
        ), {"id": episode_id, "a": agent_id})
        for i, k in enumerate("ABCD"):
            await s.execute(text(
                "INSERT INTO heart.episode_chunks (id, agent_id, episode_id, chunk_index, content) "
                "VALUES (:id, :a, :ep, :i, :c)"
            ), {"id": ids[k], "a": agent_id, "ep": episode_id, "i": i, "c": content[k]})
        await s.commit()
        bind = s.bind
    # A recall never loads the document-frequency cache itself (a cold key skips
    # the fallback), so warm it the way the background refresh would.
    df = await search_mod._load_doc_freq(bind, "heart.episode_chunks", agent_id)
    search_mod._DF_CACHE[("heart.episode_chunks", agent_id)] = (time.monotonic(), df)
    yield agent_id, ids
    async with eval_db.session() as s:
        await s.execute(text("DELETE FROM heart.episodes WHERE id = :id"), {"id": episode_id})
        await s.commit()
    search_mod._DF_CACHE.clear()


async def _search(eval_db, agent_id, query=QUERY, **kw):
    from nous.heart.search import hybrid_search, with_keyword_stats

    sink: list = []
    async with eval_db.session() as s:
        out = await with_keyword_stats(sink, hybrid_search(
            s, "heart.episode_chunks", [0.01] * 1536, query, agent_id,
            limit=10, active_filter=False, **kw))
    return [d for d, _ in out], sink


@pytest.mark.asyncio
async def test_all_words_alone_matches_only_the_chunk_with_every_word(eval_db, chunks, monkeypatch):
    agent_id, ids = chunks
    monkeypatch.setattr("nous.heart.search._resolve_keyword_or_fallback_enabled", lambda: True)
    got, sink = await _search(eval_db, agent_id)          # no opt-in
    assert got == [ids["A"]]
    assert sink[0]["n_keyword"] == 1 and sink[0]["n_keyword_fallback"] is None


@pytest.mark.asyncio
async def test_fallback_adds_partial_matches_after_the_all_words_row(eval_db, chunks, monkeypatch):
    agent_id, ids = chunks
    monkeypatch.setattr("nous.heart.search._resolve_keyword_or_fallback_enabled", lambda: True)
    got, sink = await _search(eval_db, agent_id, any_word_fallback=True)
    assert got[0] == ids["A"]                              # all-words row stays first
    assert set(got) == {ids["A"], ids["B"], ids["C"]}      # D shares no word
    assert len(got) == len(set(got)), "an all-words row must not be duplicated"
    assert sink[0]["n_keyword"] == 1                       # the metric is the AND count
    assert sink[0]["n_keyword_fallback"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("query", "expected", "first", "n_fallback"),
    [
        ("alpha bravo yankee zulu", "ABC", "A", 3),        # A has two of the words
        ("bravo yankee zulu", "AC", None, 2),              # A and C tie on one word
    ],
)
async def test_zero_all_words_hits_runs_the_fallback_with_empty_and_ids(
    eval_db, chunks, monkeypatch, query, expected, first, n_fallback
):
    """The dominant production path: a long query matches NO row on every word,
    so the fallback statement runs with ``:and_ids == []`` (asyncpg must still
    type it as uuid[]). 'yankee' and 'zulu' occur in no chunk."""
    agent_id, ids = chunks
    monkeypatch.setattr("nous.heart.search._resolve_keyword_or_fallback_enabled", lambda: True)
    without, sink_off = await _search(eval_db, agent_id, query=query)     # no opt-in
    assert without == []
    assert sink_off == [{"n_keyword": 0, "n_keyword_fallback": None}]

    got, sink = await _search(eval_db, agent_id, query=query, any_word_fallback=True)
    assert set(got) == {ids[k] for k in expected}          # D shares no word
    assert len(got) == len(set(got))
    if first is not None:
        assert got[0] == ids[first]
    assert sink == [{"n_keyword": 0, "n_keyword_fallback": n_fallback}]


@pytest.mark.asyncio
async def test_a_cold_cache_skips_the_fallback_then_the_background_fill_enables_it(
    eval_db, chunks, monkeypatch
):
    agent_id, ids = chunks
    monkeypatch.setattr("nous.heart.search._resolve_keyword_or_fallback_enabled", lambda: True)
    import nous.heart.search as search_mod

    search_mod._DF_CACHE.clear()
    got, sink = await _search(eval_db, agent_id, any_word_fallback=True)
    assert got == [ids["A"]]                       # recall did not wait for ts_stat
    assert sink[0]["n_keyword_fallback"] is None
    await asyncio.gather(*list(search_mod._DF_REFRESHING.values()))
    got, sink = await _search(eval_db, agent_id, any_word_fallback=True)
    assert set(got) == {ids["A"], ids["B"], ids["C"]}
    assert sink[0]["n_keyword_fallback"] == 2


@pytest.mark.asyncio
async def test_flag_off_is_the_old_behaviour(eval_db, chunks, monkeypatch):
    agent_id, ids = chunks
    monkeypatch.setattr("nous.heart.search._resolve_keyword_or_fallback_enabled", lambda: False)
    got, _ = await _search(eval_db, agent_id, any_word_fallback=True)
    assert got == [ids["A"]]


@pytest.mark.asyncio
async def test_require_keyword_hit_stays_all_words(eval_db, chunks, monkeypatch):
    """The Session Profile leg's gate: a shared word must NOT pass it."""
    agent_id, ids = chunks
    monkeypatch.setattr("nous.heart.search._resolve_keyword_or_fallback_enabled", lambda: True)
    got, _ = await _search(eval_db, agent_id, any_word_fallback=True, require_keyword_hit=True)
    assert got == [ids["A"]]


@pytest.mark.asyncio
async def test_only_the_rarest_lexemes_are_searched(eval_db, chunks, monkeypatch):
    """With K shrunk to 1 only the rarest query lexeme is searched: 'alpha' is in
    2 chunks (A, B), 'bravo' in 2 (A, C), 'charlie'/'delta' in 1 (A) -- so the
    rarest is 'charlie' or 'delta', which only A contains and nothing is added."""
    agent_id, ids = chunks
    monkeypatch.setattr("nous.heart.search._resolve_keyword_or_fallback_enabled", lambda: True)
    monkeypatch.setattr("nous.heart.search._OR_MAX_LEXEMES", 1)
    got, sink = await _search(eval_db, agent_id, any_word_fallback=True)
    assert got == [ids["A"]]
    assert sink[0]["n_keyword_fallback"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("query", ["the and of", "", "alpha", PUNCT])
async def test_degenerate_queries_neither_error_nor_add_rows(eval_db, chunks, monkeypatch, query):
    """All-stopword, empty, single-lexeme and punctuation-heavy queries: no
    error, and the result with the fallback equals the result without it (none of
    these words occurs in the fixture, or only one does)."""
    agent_id, ids = chunks
    monkeypatch.setattr("nous.heart.search._resolve_keyword_or_fallback_enabled", lambda: True)
    on, _ = await _search(eval_db, agent_id, query=query, any_word_fallback=True)
    monkeypatch.setattr("nous.heart.search._resolve_keyword_or_fallback_enabled", lambda: False)
    off, _ = await _search(eval_db, agent_id, query=query, any_word_fallback=True)
    assert on == off
    assert set(on) <= set(ids.values())
    if query != "alpha":
        assert on == []
