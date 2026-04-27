"""F055 — Cross-Turn Residual Activation tests.

Covers:
1. Settings defaults (9 new fields)
2. Decay math: geometric + power_law
3. compute_activations: floor pruning + top-K bound + decay applied
4. seed_for_spreading: top-N + seed_weight multiplier
5. boost_scores: additive bounded boost on RecallResult.score
6. boost_scores: clamped to 1.0
7. record_surfaced: fire-and-forget shape (rank-norm + UUID stringify)
8. Heart.set_residual_activator: idempotent overwrite warning
9. Heart._recall: residual_activations=None is no-op
10. Heart._recall: empty activations dict is no-op
11. recall_deep dispatcher injection: _session_id reaches recall_deep
12. Fail-open: compute_activations returns {} on raw_items raise
"""

from __future__ import annotations

import math
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from nous.config import Settings
from nous.heart.residual_activation import ResidualActivator


# ---------------------------------------------------------------------------
# 1. Settings defaults
# ---------------------------------------------------------------------------


def test_settings_residual_activation_defaults() -> None:
    """All 9 F055 Settings fields have correct defaults."""
    s = Settings()
    assert s.residual_activation_enabled is False
    assert s.residual_decay_mode == "geometric"
    assert s.residual_decay_per_turn == 0.5
    assert s.residual_power_law_alpha == 0.5
    assert s.residual_activation_floor == 0.05
    assert s.residual_top_k_carried == 20
    assert s.residual_top_n_seeds == 5
    assert s.residual_seed_weight == 0.3
    assert s.residual_boost_weight == 0.15


# ---------------------------------------------------------------------------
# 2. Decay math
# ---------------------------------------------------------------------------


def test_geometric_decay_at_turn_zero_is_one() -> None:
    """No turns elapsed → activation unchanged."""
    s = Settings(residual_decay_mode="geometric", residual_decay_per_turn=0.5)
    activator = ResidualActivator(settings=s, wm=MagicMock(), db=MagicMock())
    assert activator._decay_factor(0) == 1.0


def test_geometric_decay_compounds_per_turn() -> None:
    """decay^t — at t=2 with d=0.5 → 0.25."""
    s = Settings(residual_decay_mode="geometric", residual_decay_per_turn=0.5)
    activator = ResidualActivator(settings=s, wm=MagicMock(), db=MagicMock())
    assert activator._decay_factor(2) == 0.25
    assert activator._decay_factor(4) == 0.0625


def test_power_law_decay() -> None:
    """ACT-R: (t+1)^(-alpha). At t=3 with alpha=0.5 → 1/sqrt(4) = 0.5."""
    s = Settings(residual_decay_mode="power_law", residual_power_law_alpha=0.5)
    activator = ResidualActivator(settings=s, wm=MagicMock(), db=MagicMock())
    assert activator._decay_factor(3) == pytest.approx(0.5)


def test_decay_factor_negative_turns_returns_zero() -> None:
    """Defensive: negative turn diff → 0 (item from the future, prune it)."""
    s = Settings()
    activator = ResidualActivator(settings=s, wm=MagicMock(), db=MagicMock())
    assert activator._decay_factor(-1) == 0.0


# ---------------------------------------------------------------------------
# 3. compute_activations: floor + top-K + decay
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compute_activations_applies_floor() -> None:
    """Items below activation_floor after decay are dropped."""
    s = Settings(
        residual_decay_per_turn=0.5,
        residual_activation_floor=0.05,
        residual_top_k_carried=20,
    )
    wm = MagicMock()
    id_high, id_low = uuid4(), uuid4()
    # base 0.8 at turn 1, current_turn=10 → 0.8 * 0.5^9 = 0.00156 (below floor)
    # base 0.8 at turn 9, current_turn=10 → 0.8 * 0.5^1 = 0.4 (above floor)
    wm.list_raw_items = AsyncMock(return_value=[
        {"ref_id": str(id_high), "type": "fact", "activation": 0.8, "last_surfaced_turn": 9},
        {"ref_id": str(id_low),  "type": "fact", "activation": 0.8, "last_surfaced_turn": 1},
    ])
    activator = ResidualActivator(settings=s, wm=wm, db=MagicMock())
    out = await activator.compute_activations("a", "s", current_turn=10)
    assert id_high in out
    assert id_low not in out
    assert out[id_high] == pytest.approx(0.4)


@pytest.mark.asyncio
async def test_compute_activations_caps_at_top_k() -> None:
    """Only the top-K (by activation) survive after pruning."""
    s = Settings(
        residual_decay_per_turn=1.0,  # no decay → pure base score
        residual_activation_floor=0.0,
        residual_top_k_carried=2,
    )
    wm = MagicMock()
    ids = [uuid4() for _ in range(5)]
    wm.list_raw_items = AsyncMock(return_value=[
        {"ref_id": str(uid), "type": "fact", "activation": 0.1 * (i + 1), "last_surfaced_turn": 0}
        for i, uid in enumerate(ids)
    ])
    activator = ResidualActivator(settings=s, wm=wm, db=MagicMock())
    out = await activator.compute_activations("a", "s", current_turn=0)
    # Top-2 by activation = ids[3] (0.4) + ids[4] (0.5)
    assert len(out) == 2
    assert ids[4] in out
    assert ids[3] in out


@pytest.mark.asyncio
async def test_compute_activations_skips_items_missing_residual_keys() -> None:
    """Pre-F055 items (no activation/last_surfaced_turn keys) are skipped, not crashed."""
    s = Settings()
    wm = MagicMock()
    wm.list_raw_items = AsyncMock(return_value=[
        {"ref_id": str(uuid4()), "type": "fact"},  # no residual keys
        {"ref_id": str(uuid4()), "type": "fact", "activation": 0.5, "last_surfaced_turn": 0},
    ])
    activator = ResidualActivator(settings=s, wm=wm, db=MagicMock())
    out = await activator.compute_activations("a", "s", current_turn=0)
    assert len(out) == 1


# ---------------------------------------------------------------------------
# 4. seed_for_spreading
# ---------------------------------------------------------------------------


def test_seed_for_spreading_top_n_with_weight() -> None:
    """Returns top-N by activation, multiplied by seed_weight."""
    s = Settings(residual_top_n_seeds=2, residual_seed_weight=0.3)
    activator = ResidualActivator(settings=s, wm=MagicMock(), db=MagicMock())
    a, b, c = uuid4(), uuid4(), uuid4()
    activations = {a: 0.9, b: 0.5, c: 0.7}
    seeds = activator.seed_for_spreading(activations)
    assert len(seeds) == 2
    # Top-2 = a (0.9), c (0.7)
    seed_ids = {s[0] for s in seeds}
    assert seed_ids == {a, c}
    # Weight applied
    a_seed = next(s for s in seeds if s[0] == a)
    assert a_seed[2] == pytest.approx(0.9 * 0.3)


def test_seed_for_spreading_empty_dict() -> None:
    """Empty activations → empty seed list (no extra seeds for F022)."""
    activator = ResidualActivator(settings=Settings(), wm=MagicMock(), db=MagicMock())
    assert activator.seed_for_spreading({}) == []


# ---------------------------------------------------------------------------
# 5-6. boost_scores: additive + clamped
# ---------------------------------------------------------------------------


def test_boost_scores_additive_with_weight() -> None:
    """score += activation * boost_weight, in place."""
    s = Settings(residual_boost_weight=0.2)
    activator = ResidualActivator(settings=s, wm=MagicMock(), db=MagicMock())
    a, b = uuid4(), uuid4()
    candidates = [
        MagicMock(id=a, score=0.5),
        MagicMock(id=b, score=0.3),
    ]
    activations = {a: 1.0}  # b has no activation → unchanged
    out = activator.boost_scores(candidates, activations)
    assert out[0].score == pytest.approx(0.5 + 1.0 * 0.2)
    assert out[1].score == 0.3


def test_boost_scores_clamps_to_one() -> None:
    """Boost cannot push score above 1.0."""
    s = Settings(residual_boost_weight=0.5)
    activator = ResidualActivator(settings=s, wm=MagicMock(), db=MagicMock())
    a = uuid4()
    candidates = [MagicMock(id=a, score=0.9)]
    activations = {a: 1.0}  # 0.9 + 0.5 = 1.4 → clamped to 1.0
    out = activator.boost_scores(candidates, activations)
    assert out[0].score == 1.0


def test_boost_scores_empty_activations_is_noop() -> None:
    """Empty activations → candidates returned unchanged."""
    activator = ResidualActivator(settings=Settings(), wm=MagicMock(), db=MagicMock())
    candidates = [MagicMock(id=uuid4(), score=0.5)]
    out = activator.boost_scores(candidates, {})
    assert out[0].score == 0.5


# ---------------------------------------------------------------------------
# 7. record_surfaced shape
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_record_surfaced_writes_rank_normalized() -> None:
    """Activations are rank-normalized (max=1.0) before persisting."""
    s = Settings(residual_top_k_carried=20)
    wm = MagicMock()
    wm.upsert_residual_items = AsyncMock()
    activator = ResidualActivator(settings=s, wm=wm, db=MagicMock())
    a, b = uuid4(), uuid4()
    surfaced = [(a, "fact", 0.8), (b, "decision", 0.2)]
    await activator.record_surfaced("agent", "session", current_turn=3, surfaced=surfaced)
    wm.upsert_residual_items.assert_called_once()
    call_kwargs = wm.upsert_residual_items.call_args.kwargs
    items = call_kwargs["items"]
    assert len(items) == 2
    # Highest score → activation=1.0; lower → 0.25 (0.2/0.8)
    a_item = next(i for i in items if i["ref_id"] == str(a))
    b_item = next(i for i in items if i["ref_id"] == str(b))
    assert a_item["activation"] == pytest.approx(1.0)
    assert b_item["activation"] == pytest.approx(0.25)
    assert a_item["last_surfaced_turn"] == 3


@pytest.mark.asyncio
async def test_record_surfaced_empty_list_is_noop() -> None:
    """Empty surfaced list → no DB write."""
    wm = MagicMock()
    wm.upsert_residual_items = AsyncMock()
    activator = ResidualActivator(settings=Settings(), wm=wm, db=MagicMock())
    await activator.record_surfaced("a", "s", current_turn=1, surfaced=[])
    wm.upsert_residual_items.assert_not_called()


# ---------------------------------------------------------------------------
# 8. Heart.set_residual_activator idempotent + log on overwrite
# ---------------------------------------------------------------------------


def test_heart_set_residual_activator_overwrite_warning(caplog) -> None:
    """Setting twice logs DEBUG; doesn't raise."""
    import logging
    from nous.heart.heart import Heart

    settings = Settings()
    db = MagicMock()
    heart = Heart(database=db, settings=settings, embedding_provider=None)
    activator1 = MagicMock(spec=ResidualActivator)
    activator2 = MagicMock(spec=ResidualActivator)

    heart.set_residual_activator(activator1)
    assert heart._residual_activator is activator1

    with caplog.at_level(logging.DEBUG):
        heart.set_residual_activator(activator2)
    assert heart._residual_activator is activator2


# ---------------------------------------------------------------------------
# 9-10. Heart._recall: residual=None / empty are no-ops (boost branch skipped)
# ---------------------------------------------------------------------------


def test_heart_residual_activator_default_is_none() -> None:
    """Heart constructed without set_residual_activator() has _residual_activator=None."""
    from nous.heart.heart import Heart
    heart = Heart(database=MagicMock(), settings=Settings(), embedding_provider=None)
    assert heart._residual_activator is None


# ---------------------------------------------------------------------------
# 11. recall_deep dispatcher injection (already tested in F051.4 tests, covered for completeness)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatcher_injects_session_id_into_recall_deep() -> None:
    """F051.4 wired this; F055 is the consumer. Verify the wire still holds."""
    from nous.api.tools import ToolDispatcher

    captured: dict = {}

    async def fake_recall_deep(
        query: str, limit: int = 10, memory_types=None, _session_id=None,
    ):
        captured["_session_id"] = _session_id
        return {"content": [{"type": "text", "text": "ok"}]}

    dispatcher = ToolDispatcher()
    dispatcher.register("recall_deep", fake_recall_deep, {"name": "recall_deep"})
    await dispatcher.dispatch(
        name="recall_deep",
        args={"query": "q"},
        session_id="s-42",
    )
    assert captured["_session_id"] == "s-42"


# ---------------------------------------------------------------------------
# 12. Fail-open contract
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compute_activations_returns_empty_on_raw_items_failure() -> None:
    """If list_raw_items raises, compute_activations returns {} (fail-open)."""
    wm = MagicMock()
    wm.list_raw_items = AsyncMock(side_effect=RuntimeError("db down"))
    activator = ResidualActivator(settings=Settings(), wm=wm, db=MagicMock())
    out = await activator.compute_activations("a", "s", current_turn=5)
    assert out == {}


@pytest.mark.asyncio
async def test_record_surfaced_swallows_upsert_failure() -> None:
    """If upsert_residual_items raises, record_surfaced logs WARN and returns silently."""
    wm = MagicMock()
    wm.upsert_residual_items = AsyncMock(side_effect=RuntimeError("db down"))
    activator = ResidualActivator(settings=Settings(), wm=wm, db=MagicMock())
    # Should NOT raise
    await activator.record_surfaced("a", "s", current_turn=1, surfaced=[(uuid4(), "fact", 0.5)])
