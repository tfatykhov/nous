"""F043 — Cross-encoder rerank adapter unit tests.

Deterministic tests using a FakeModel + monkeypatched `_load_cross_encoder`
so the real `sentence-transformers` dependency is never imported. Follows
the pattern established by ``tests/test_f042_reranker.py``.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest

import nous.heart.reranker as reranker_mod
from nous.brain import backfill_rerank as br


# ---------------------------------------------------------------------------
# FakeModel + helpers
# ---------------------------------------------------------------------------


class FakeModel:
    """Fake CrossEncoder returning deterministic floats."""

    def __init__(self, score_fn=None, raises: Exception | None = None):
        self.score_fn = score_fn or (lambda pairs: [0.5] * len(pairs))
        self.raises = raises
        self.pairs_seen: list[tuple[str, str]] = []
        self.call_count = 0

    def predict(self, pairs):
        self.call_count += 1
        pairs_list = list(pairs)
        self.pairs_seen.extend(pairs_list)
        if self.raises is not None:
            raise self.raises
        return self.score_fn(pairs_list)


def make_settings(**overrides):
    """Build a minimal settings SimpleNamespace carrying only the fields the adapter reads."""
    defaults = dict(
        ce_backfill_enabled=True,
        ce_backfill_top_k=10,
        ce_backfill_min_score=0.30,
        # F045: permissive default so legacy tests using short fake content
        # (e.g. "alpha", "beta") aren't dropped by the content-length guard.
        # F045-specific tests override this to exercise the guard.
        ce_backfill_min_content_chars=0,
        cross_encoder_model="fake-model",
        cross_encoder_text_limit=512,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


@pytest.fixture
def install_fake(monkeypatch):
    """Force CROSS_ENCODER_AVAILABLE=True and stub the loader to return the given fake."""

    def _install(fake: FakeModel):
        # Both modules cache CROSS_ENCODER_AVAILABLE as an imported constant.
        monkeypatch.setattr(reranker_mod, "CROSS_ENCODER_AVAILABLE", True)
        monkeypatch.setattr(br, "CROSS_ENCODER_AVAILABLE", True)
        monkeypatch.setattr(reranker_mod, "_load_cross_encoder", lambda name: fake)

    return _install


def _uuid(n: int) -> UUID:
    return UUID(int=n)


# ---------------------------------------------------------------------------
# 1. Disabled passthrough
# ---------------------------------------------------------------------------


async def test_ce_rerank_disabled_passthrough(monkeypatch):
    """ce_backfill_enabled=False → returns input unchanged."""
    # Even if CE were available, disabled wins the short-circuit.
    monkeypatch.setattr(reranker_mod, "CROSS_ENCODER_AVAILABLE", True)
    settings = make_settings(ce_backfill_enabled=False)

    rows = [(_uuid(1), 0.42), (_uuid(2), 0.33)]
    out = await br.ce_rerank_backfill_candidates(
        query_text="anything",
        candidate_rows=rows,
        content_map={_uuid(1): "alpha", _uuid(2): "beta"},
        settings=settings,
    )
    assert out == rows  # list(candidate_rows) is a shallow copy w/ same tuples


# ---------------------------------------------------------------------------
# 2. Unavailable passthrough
# ---------------------------------------------------------------------------


async def test_ce_rerank_unavailable_passthrough(monkeypatch):
    """CROSS_ENCODER_AVAILABLE=False → input returned unchanged."""
    monkeypatch.setattr(reranker_mod, "CROSS_ENCODER_AVAILABLE", False)
    monkeypatch.setattr(br, "CROSS_ENCODER_AVAILABLE", False)
    settings = make_settings()

    rows = [(_uuid(1), 0.42), (_uuid(2), 0.33)]
    out = await br.ce_rerank_backfill_candidates(
        query_text="anything",
        candidate_rows=rows,
        content_map={_uuid(1): "alpha", _uuid(2): "beta"},
        settings=settings,
    )
    assert out == rows


# ---------------------------------------------------------------------------
# 3. Empty candidates
# ---------------------------------------------------------------------------


async def test_ce_rerank_empty_candidates(install_fake):
    """[] in → [] out (short-circuit before any model load)."""
    fake = FakeModel()
    install_fake(fake)
    settings = make_settings()

    out = await br.ce_rerank_backfill_candidates(
        query_text="anything",
        candidate_rows=[],
        content_map={},
        settings=settings,
    )
    assert out == []
    assert fake.call_count == 0


# ---------------------------------------------------------------------------
# 4. Empty query
# ---------------------------------------------------------------------------


async def test_ce_rerank_empty_query(install_fake):
    """Empty query_text → input returned unchanged."""
    fake = FakeModel()
    install_fake(fake)
    settings = make_settings()

    rows = [(_uuid(1), 0.4), (_uuid(2), 0.3)]
    out = await br.ce_rerank_backfill_candidates(
        query_text="",
        candidate_rows=rows,
        content_map={_uuid(1): "alpha", _uuid(2): "beta"},
        settings=settings,
    )
    assert out == rows
    assert fake.call_count == 0


# ---------------------------------------------------------------------------
# 5. Drops rows with missing / empty content
# ---------------------------------------------------------------------------


async def test_ce_rerank_drops_empty_content(install_fake):
    """Rows whose content is missing from content_map are dropped before reranking."""
    fake = FakeModel(score_fn=lambda pairs: [2.0] * len(pairs))
    install_fake(fake)
    settings = make_settings()

    rows = [(_uuid(1), 0.4), (_uuid(2), 0.3), (_uuid(3), 0.2)]
    content_map = {_uuid(1): "alpha", _uuid(2): "beta"}  # _uuid(3) missing

    out = await br.ce_rerank_backfill_candidates(
        query_text="q",
        candidate_rows=rows,
        content_map=content_map,
        settings=settings,
    )
    # Only 2 rows reached the reranker.
    assert len(fake.pairs_seen) == 2
    # And output has at most 2 survivors.
    out_ids = {cid for cid, _ in out}
    assert _uuid(3) not in out_ids
    assert len(out) == 2


# ---------------------------------------------------------------------------
# 6. Respects top_k
# ---------------------------------------------------------------------------


async def test_ce_rerank_respects_top_k(install_fake):
    """20 candidates with top_k=5 → returns exactly 5 survivors."""
    # Fake emits raw logits 20,19,...1 so sigmoid is near 1.0 for all (all above floor).
    fake = FakeModel(score_fn=lambda pairs: [float(20 - i) for i in range(len(pairs))])
    install_fake(fake)
    settings = make_settings(ce_backfill_top_k=5, ce_backfill_min_score=0.1)

    rows = [(_uuid(i), 0.5) for i in range(1, 21)]
    content_map = {_uuid(i): f"doc{i}" for i in range(1, 21)}

    out = await br.ce_rerank_backfill_candidates(
        query_text="q",
        candidate_rows=rows,
        content_map=content_map,
        settings=settings,
    )
    assert len(out) == 5


# ---------------------------------------------------------------------------
# 7. Min score floor
# ---------------------------------------------------------------------------


async def test_ce_rerank_applies_min_score_floor(install_fake):
    """Raw logits that sigmoid to ~[0.8,0.7,0.4,0.2,0.1] with floor=0.5 → keep top 2."""
    import math

    # Compute inverse sigmoid (logit) of target sigmoid values.
    def logit(p: float) -> float:
        return math.log(p / (1 - p))

    targets = [0.8, 0.7, 0.4, 0.2, 0.1]
    raw_logits = [logit(p) for p in targets]

    fake = FakeModel(score_fn=lambda pairs: list(raw_logits))
    install_fake(fake)
    settings = make_settings(ce_backfill_top_k=10, ce_backfill_min_score=0.5)

    rows = [(_uuid(i + 1), 0.5) for i in range(5)]
    content_map = {_uuid(i + 1): f"doc{i+1}" for i in range(5)}

    out = await br.ce_rerank_backfill_candidates(
        query_text="q",
        candidate_rows=rows,
        content_map=content_map,
        settings=settings,
    )
    assert len(out) == 2
    # All survivors above the floor.
    for _, score in out:
        assert score >= 0.5


# ---------------------------------------------------------------------------
# 8. Preserves RRF when short-circuited
# ---------------------------------------------------------------------------


async def test_ce_rerank_preserves_rrf_when_short_circuited(monkeypatch):
    """Short-circuit path does NOT mutate the RRF score."""
    monkeypatch.setattr(reranker_mod, "CROSS_ENCODER_AVAILABLE", False)
    monkeypatch.setattr(br, "CROSS_ENCODER_AVAILABLE", False)
    settings = make_settings()

    rows = [(_uuid(1), 0.42)]
    out = await br.ce_rerank_backfill_candidates(
        query_text="q",
        candidate_rows=rows,
        content_map={_uuid(1): "alpha"},
        settings=settings,
    )
    assert out == [(_uuid(1), 0.42)]
    # RRF preserved exactly
    assert out[0][1] == 0.42


# ---------------------------------------------------------------------------
# 9. Replaces RRF with sigmoid score
# ---------------------------------------------------------------------------


async def test_ce_rerank_replaces_score_with_sigmoid(install_fake):
    """When CE actually runs, output tuples carry sigmoid(raw) NOT the input RRF."""
    # Two candidates so F042 does NOT short-circuit on len<=1; both raw=2.0.
    fake = FakeModel(score_fn=lambda pairs: [2.0] * len(pairs))
    install_fake(fake)
    settings = make_settings(ce_backfill_min_score=0.0)

    rows = [(_uuid(1), 0.42), (_uuid(2), 0.11)]
    content_map = {_uuid(1): "alpha", _uuid(2): "beta"}

    out = await br.ce_rerank_backfill_candidates(
        query_text="q",
        candidate_rows=rows,
        content_map=content_map,
        settings=settings,
    )
    expected_sig = 1.0 / (1.0 + pow(2.718281828, -2.0))
    assert len(out) == 2
    for _, score in out:
        assert abs(score - expected_sig) < 1e-3
        assert score != 0.42
        assert score != 0.11


# ---------------------------------------------------------------------------
# 10. Single candidate bypass (F042 len<=1 short-circuit)
# ---------------------------------------------------------------------------


async def test_ce_rerank_single_candidate_bypass(install_fake):
    """With one candidate, F042 short-circuits → fake model never called; tuple passes through."""
    fake = FakeModel()
    install_fake(fake)
    settings = make_settings(ce_backfill_min_score=0.0)

    rows = [(_uuid(1), 0.42)]
    out = await br.ce_rerank_backfill_candidates(
        query_text="q",
        candidate_rows=rows,
        content_map={_uuid(1): "alpha"},
        settings=settings,
    )
    # F042 short-circuits for len<=1 → adapter never invokes model.predict.
    assert fake.call_count == 0
    # Output contains the sole candidate with its original RRF score intact.
    assert len(out) == 1
    assert out[0][0] == _uuid(1)
    assert out[0][1] == 0.42


# ---------------------------------------------------------------------------
# 11. Under-filled top_k
# ---------------------------------------------------------------------------


async def test_ce_rerank_under_filled_top_k(install_fake):
    """3 candidates, top_k=10, all above floor → returns 3 (not padded)."""
    fake = FakeModel(score_fn=lambda pairs: [3.0] * len(pairs))
    install_fake(fake)
    settings = make_settings(ce_backfill_top_k=10, ce_backfill_min_score=0.1)

    rows = [(_uuid(1), 0.5), (_uuid(2), 0.5), (_uuid(3), 0.5)]
    content_map = {_uuid(1): "a", _uuid(2): "b", _uuid(3): "c"}

    out = await br.ce_rerank_backfill_candidates(
        query_text="q",
        candidate_rows=rows,
        content_map=content_map,
        settings=settings,
    )
    assert len(out) == 3


# ---------------------------------------------------------------------------
# 12. Duplicate candidate IDs — dedup is not the adapter's job
# ---------------------------------------------------------------------------


async def test_ce_rerank_duplicate_candidate_ids_in_input(install_fake):
    """Same UUID appearing twice in candidate_rows is NOT deduped by the adapter."""
    fake = FakeModel(score_fn=lambda pairs: [2.0] * len(pairs))
    install_fake(fake)
    settings = make_settings(ce_backfill_min_score=0.0)

    dup = _uuid(1)
    rows = [(dup, 0.5), (dup, 0.3)]
    content_map = {dup: "alpha"}

    out = await br.ce_rerank_backfill_candidates(
        query_text="q",
        candidate_rows=rows,
        content_map=content_map,
        settings=settings,
    )
    # Both entries wrapped and scored.
    assert len(fake.pairs_seen) == 2
    assert len(out) == 2
    assert all(cid == dup for cid, _ in out)


# ---------------------------------------------------------------------------
# 13. fetch_candidate_content agent_id filter
# ---------------------------------------------------------------------------


async def test_fetch_candidate_content_agent_id_filter():
    """fetch_candidate_content passes agent_id as a bound param (defense-in-depth)."""
    id_a = uuid4()
    id_b = uuid4()

    # Synthetic row class with .id / .content attributes.
    def make_row(rid, content):
        return SimpleNamespace(id=rid, content=content)

    # Mock session: capture params; return only the agent=A row.
    captured = {}

    class FakeResult:
        def __init__(self, rows):
            self._rows = rows

        def __iter__(self):
            return iter(self._rows)

    async def fake_execute(sql, params):
        captured["sql"] = str(sql)
        captured["params"] = params
        # Simulate the DB returning ONLY agent A's row.
        return FakeResult([make_row(id_a, "shared content")])

    session = SimpleNamespace(execute=fake_execute)

    out = await br.fetch_candidate_content(
        session=session,
        agent_id="agent-A",
        entity_type="fact",
        candidate_ids=[id_a, id_b],
    )

    assert out == {id_a: "shared content"}
    # agent_id bound into params (defense-in-depth) and SQL carries the filter.
    assert captured["params"]["agent_id"] == "agent-A"
    assert "agent_id" in captured["sql"]


# ---------------------------------------------------------------------------
# 14. fetch_candidate_content drops whitespace-only content
# ---------------------------------------------------------------------------


async def test_fetch_candidate_content_drops_whitespace():
    """Rows with NULL / empty / whitespace-only content are omitted from the result map."""
    id_keep = uuid4()
    id_ws = uuid4()
    id_none = uuid4()

    def make_row(rid, content):
        return SimpleNamespace(id=rid, content=content)

    class FakeResult:
        def __init__(self, rows):
            self._rows = rows

        def __iter__(self):
            return iter(self._rows)

    async def fake_execute(sql, params):
        return FakeResult(
            [
                make_row(id_keep, "hello"),
                make_row(id_ws, "   "),
                make_row(id_none, None),
            ]
        )

    session = SimpleNamespace(execute=fake_execute)

    out = await br.fetch_candidate_content(
        session=session,
        agent_id="agent-A",
        entity_type="fact",
        candidate_ids=[id_keep, id_ws, id_none],
    )
    assert out == {id_keep: "hello"}


# ---------------------------------------------------------------------------
# 15. F045 content-length guard — drops short content
# ---------------------------------------------------------------------------


async def test_content_guard_drops_short(install_fake):
    """Candidates with content shorter than ce_backfill_min_content_chars are dropped."""
    fake = FakeModel(score_fn=lambda pairs: [5.0] * len(pairs))
    install_fake(fake)

    short_id = _uuid(1)
    long_id = _uuid(2)
    settings = make_settings(ce_backfill_min_content_chars=80)

    rows = [(short_id, 0.5), (long_id, 0.5)]
    content_map = {
        short_id: "too short — 30 char placeholder",  # ~30 chars, below 80
        long_id: "A" * 200,  # well above the floor
    }

    out = await br.ce_rerank_backfill_candidates(
        query_text="test query",
        candidate_rows=rows,
        content_map=content_map,
        settings=settings,
    )

    # Only the long candidate survives the guard.
    assert len(out) == 1
    assert out[0][0] == long_id


# ---------------------------------------------------------------------------
# 16. F045 content-length guard — whitespace stripped before counting
# ---------------------------------------------------------------------------


async def test_content_guard_respects_whitespace(install_fake):
    """Whitespace-padded short content is still dropped (len measured after strip)."""
    fake = FakeModel(score_fn=lambda pairs: [5.0] * len(pairs))
    install_fake(fake)

    padded_id = _uuid(1)
    real_id = _uuid(2)
    settings = make_settings(ce_backfill_min_content_chars=80)

    rows = [(padded_id, 0.5), (real_id, 0.5)]
    content_map = {
        padded_id: "   short   " + " " * 200,  # strips to 'short' (5 chars)
        real_id: "A" * 150,
    }

    out = await br.ce_rerank_backfill_candidates(
        query_text="test query",
        candidate_rows=rows,
        content_map=content_map,
        settings=settings,
    )

    assert len(out) == 1
    assert out[0][0] == real_id


# ---------------------------------------------------------------------------
# 17. F045 content-length guard — configurable floor
# ---------------------------------------------------------------------------


async def test_content_guard_configurable(install_fake):
    """Raising ce_backfill_min_content_chars drops medium-length content."""
    fake = FakeModel(score_fn=lambda pairs: [5.0] * len(pairs))
    install_fake(fake)

    med_id = _uuid(1)
    long_id = _uuid(2)
    settings = make_settings(ce_backfill_min_content_chars=250)

    rows = [(med_id, 0.5), (long_id, 0.5)]
    content_map = {
        med_id: "M" * 150,  # passes default 80 but not raised 250
        long_id: "L" * 300,
    }

    out = await br.ce_rerank_backfill_candidates(
        query_text="test query",
        candidate_rows=rows,
        content_map=content_map,
        settings=settings,
    )

    assert len(out) == 1
    assert out[0][0] == long_id


# ---------------------------------------------------------------------------
# 18. F045 content-length guard — runs BEFORE cross_encoder_rerank (P2-1)
# ---------------------------------------------------------------------------


async def test_content_guard_runs_before_ce(install_fake):
    """The guard must filter candidates *before* CE inference, not after.

    This is the P2-1 fix from the F045 plan review: ensures the guard is a
    pre-filter, not a post-filter. We seed 3 candidates (1 short + 2 long)
    so the F042 reranker does not trip its ``len<=1`` short-circuit, then
    inspect ``fake.pairs_seen`` to prove the short candidate never reached
    the model.
    """
    fake = FakeModel(score_fn=lambda pairs: [5.0] * len(pairs))
    install_fake(fake)

    short_id = _uuid(1)
    long_id_a = _uuid(2)
    long_id_b = _uuid(3)
    short_content = "URL-ONLY-FACT-40-chars-barely-barely-hi"  # <80 chars
    long_content_a = "A" * 200 + " alpha prose fact"
    long_content_b = "B" * 200 + " bravo prose fact"

    settings = make_settings(ce_backfill_min_content_chars=80)

    out = await br.ce_rerank_backfill_candidates(
        query_text="test query",
        candidate_rows=[(short_id, 0.5), (long_id_a, 0.5), (long_id_b, 0.5)],
        content_map={
            short_id: short_content,
            long_id_a: long_content_a,
            long_id_b: long_content_b,
        },
        settings=settings,
    )

    # Short candidate is not in the output set.
    out_ids = {cid for cid, _ in out}
    assert short_id not in out_ids
    assert long_id_a in out_ids and long_id_b in out_ids

    # F042 reranker was invoked (len>1 so no short-circuit).
    assert fake.call_count == 1
    pair_docs = [doc for _, doc in fake.pairs_seen]
    # Short candidate's content must NEVER have reached the model.
    assert short_content not in pair_docs
    # Both long candidates were scored.
    assert long_content_a in pair_docs
    assert long_content_b in pair_docs
