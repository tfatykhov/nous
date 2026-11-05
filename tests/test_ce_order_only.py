"""cross_encoder_order_only: the cross-encoder orders Heart's served rows but no longer selects them.

With the CE on, heart.recall reranks the merged head before its [:limit] cut, so the CE's order decides which rows
are served. With order_only, Heart selects exactly the CE-off rows and the CE reorders only those.

The CE here is the real ``cross_encoder_rerank`` with a deterministic stand-in model (a logit per text from its
hash), so the tests need no model download. Real Postgres, on the seeded corpus of ``test_recall_deep_precision``
(fixed ids, an agent of its own per test). ``tests/fixtures/recall_deep_ce_golden.txt`` is recall_deep's text with
that CE on and order_only off, written by ``render_corpus`` on aaa432eb, before the setting existed. Imports of
names this change adds sit inside the tests.
"""

from __future__ import annotations

import hashlib
import math
import re
from pathlib import Path

import pytest
from conftest import MockEmbeddingProvider
from test_recall_deep_precision import QUERY, _agent, _clear, _seed, corpus_settings, render_corpus

CE_GOLDEN = Path(__file__).parent / "fixtures" / "recall_deep_ce_golden.txt"
CE = {"cross_encoder_enabled": True, "cross_encoder_max_candidates": 20, "cross_encoder_model": "stand-in"}


def logit(text: str) -> float:
    """The stand-in model's raw score for a text: fixed, and unrelated to the RRF order."""
    return int(hashlib.sha256(text.encode("utf-8")).hexdigest()[:8], 16) / 0xFFFFFFFF * 8 - 4


class _StandInModel:
    def predict(self, pairs):
        return [logit(text) for _, text in pairs]


@pytest.fixture
def stand_in_ce(monkeypatch):
    """The real rerank code path with the stand-in model, and no runtime CE override left by another test."""
    import nous.heart.heart as heart_mod
    import nous.heart.reranker as reranker
    from nous.runtime_config import RuntimeConfig

    monkeypatch.setattr(reranker, "CROSS_ENCODER_AVAILABLE", True)
    monkeypatch.setattr(heart_mod, "CROSS_ENCODER_AVAILABLE", True)
    monkeypatch.setattr(reranker, "_load_cross_encoder", lambda _name: _StandInModel())
    monkeypatch.delitem(RuntimeConfig.get()._overrides, "cross_encoder_enabled", raising=False)


async def _heart_recall(db, agent: str, limit: int = 10, **overrides) -> list:
    """heart.recall(QUERY, limit=limit) on the seeded corpus under ``overrides``."""
    from nous.heart import Heart

    settings = corpus_settings(agent, **overrides)
    heart = Heart(db, settings, embedding_provider=MockEmbeddingProvider())
    try:
        return await heart.recall(QUERY, limit=limit)
    finally:
        await heart.close()


async def _three_ways(db) -> tuple[list, list, list]:
    """(CE off, CE on as today, CE on with order_only), all on one seeded corpus."""
    agent = _agent()
    await _seed(db, agent, MockEmbeddingProvider())
    try:
        off = await _heart_recall(db, agent, cross_encoder_enabled=False)
        on = await _heart_recall(db, agent, **CE)
        only = await _heart_recall(db, agent, **CE, cross_encoder_order_only=True)
    finally:
        await _clear(db, agent)
    return off, on, only


@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_order_only_serves_the_ce_off_set(db, stand_in_ce):
    off, on, only = await _three_ways(db)
    ids = lambda rows: {(r.type, r.id) for r in rows}  # noqa: E731
    assert ids(on) != ids(off), "the stand-in CE must change today's selection, or this test proves nothing"
    assert ids(only) == ids(off)


@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_order_only_orders_by_the_ce(db, stand_in_ce):
    off, _, only = await _three_ways(db)
    # The CE's order of the CE-off rows: by its score, the CE-off order on a tie (a stable sort).
    expected = sorted(off, key=lambda r: logit(r.summary or ""), reverse=True)
    assert [(r.type, r.id) for r in only] == [(r.type, r.id) for r in expected]
    assert [(r.type, r.id) for r in only] != [(r.type, r.id) for r in off], "the CE must reorder something"
    # Each row carries its CE sigmoid, the score today's pre-cut rerank gives the same row.
    for r in only:
        assert r.score == pytest.approx(1 / (1 + math.exp(-logit(r.summary or ""))))


@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_downstream_sees_the_ce_scores_and_order_within_the_set(db, stand_in_ce):
    """recall_deep merges the legs by score (rerank_by_score) and 'ranked' prints that order: a Heart row both modes
    serve prints the same CE score in both, and those rows keep the same relative order.

    A consistency guard only: it would also pass if order_only did nothing (both renders would be today's text).
    ``test_order_only_serves_the_ce_off_set`` is the test that proves the two modes differ."""
    on_txt, on_em = await render_corpus(db, corpus_settings(_agent(), **CE, recall_deep_section_order="ranked"))
    oo_txt, oo_em = await render_corpus(
        db, corpus_settings(_agent(), **CE, cross_encoder_order_only=True, recall_deep_section_order="ranked")
    )
    score = re.compile(r"\(id: ([0-9a-f-]{36}), score: ([0-9.]+) \[rrf\]\)")
    on_scores, oo_scores = dict(score.findall(on_txt)), dict(score.findall(oo_txt))
    common = [i for i in on_scores if i in oo_scores]
    assert len(common) >= 3
    assert {i: on_scores[i] for i in common} == {i: oo_scores[i] for i in common}
    order = lambda em: [str(i) for i, _ in em if str(i) in common]  # noqa: E731
    assert order(on_em) == order(oo_em)


@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_order_only_scores_a_lone_selected_row(db, stand_in_ce):
    """limit=1: the one served row is CE-off's, and it carries its CE sigmoid, as the pre-cut rerank would give it."""
    agent = _agent()
    await _seed(db, agent, MockEmbeddingProvider())
    try:
        off = await _heart_recall(db, agent, limit=1, cross_encoder_enabled=False)
        only = await _heart_recall(db, agent, limit=1, **CE, cross_encoder_order_only=True)
    finally:
        await _clear(db, agent)
    assert [(r.type, r.id) for r in only] == [(r.type, r.id) for r in off]
    assert only[0].score == pytest.approx(1 / (1 + math.exp(-logit(only[0].summary or ""))))
    assert only[0].score != pytest.approx(off[0].score)


class _FailingModel:
    def predict(self, pairs):
        raise RuntimeError("stand-in predict failure")


@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_order_only_keeps_the_mmr_order_when_the_ce_fails(db, stand_in_ce, monkeypatch):
    """A failed rerank leaves the selection as it was: MMR's order and scores, not a re-sort by score."""
    import nous.heart.reranker as reranker

    monkeypatch.setattr(reranker, "_load_cross_encoder", lambda _name: _FailingModel())
    agent = _agent()
    await _seed(db, agent, MockEmbeddingProvider())
    try:
        off = await _heart_recall(db, agent, cross_encoder_enabled=False, mmr_enabled=True)
        only = await _heart_recall(db, agent, **CE, cross_encoder_order_only=True, mmr_enabled=True)
    finally:
        await _clear(db, agent)
    rows = lambda rs: [(r.type, r.id, r.score) for r in rs]  # noqa: E731
    by_score = sorted(off, key=lambda r: r.score, reverse=True)
    assert rows(off) != rows(by_score), "MMR must reorder something, or this test proves nothing"
    assert rows(only) == rows(off)


@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_order_only_off_is_byte_identical_with_the_ce_on(db, stand_in_ce):
    txt, _ = await render_corpus(db, corpus_settings(_agent(), **CE))
    assert txt == CE_GOLDEN.read_text(encoding="utf-8")


@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_order_only_without_the_ce_changes_nothing(db, stand_in_ce):
    txt, _ = await render_corpus(db, corpus_settings(_agent(), cross_encoder_order_only=True))
    assert txt == (Path(__file__).parent / "fixtures" / "recall_deep_p1_golden.txt").read_text(encoding="utf-8")


def test_setting_defaults_off():
    from nous.config import Settings

    assert Settings(_env_file=None).cross_encoder_order_only is False
