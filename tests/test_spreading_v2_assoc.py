"""F105 Task 4: associative edges (``co_occurred`` / ``co_mention``) in the
spreading activation traversal, at a reduced weight, behind a flag.

With the flag off the traversal refuses them, as before. With it on, a hop over
one is multiplied by ``spreading_associative_weight_mult`` on top of the usual
``weight * decay``; lineage (``supersedes``), negative (``contradicts``) and
temporal (``happened_before``) edges stay refused. Seed S has score 0.8 and the
decay is 0.5 throughout. Nodes are bare UUIDs (``graph_edges`` has no foreign
key to the node tables), and every multi-row comparison uses ``limit=100`` and
``{id: (activation, depth)}`` dicts, because the CTE's final ORDER BY has no tie
key.
"""

from __future__ import annotations

import inspect
import random
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from spreading_v2_helpers import SNAPSHOT, insert_edge, render

from nous.brain.spreading_activation import compute_graph_density, spreading_activation_search
from nous.config import Settings

ASSOC_FIELDS = {"spreading_associative_edges_enabled", "spreading_associative_weight_mult"}


def _settings(**over: Any) -> Settings:
    """Settings defaults with no .env file (decay 0.5, max depth 2)."""
    return Settings(_env_file=None, **over)


def _assoc(**over: Any) -> Settings:
    return _settings(spreading_associative_edges_enabled=True, **over)


def _agent() -> str:
    return f"assoc-{uuid4().hex[:8]}"


async def _act(session, agent: str, seeds, settings, **kwargs: Any) -> dict[UUID, tuple[float, int]]:
    """``node -> (activation, depth)`` over the whole (limit=100) result."""
    rows = await spreading_activation_search(session, agent, seeds, settings, limit=100, **kwargs)
    return {r[0]: (r[2], r[3]) for r in rows}


def test_assoc_settings_bounds():
    """The multiplier is in (0, 1]: above 1 would amplify, 0 would be the flag off."""
    s = _settings()
    assert (s.spreading_associative_edges_enabled, s.spreading_associative_weight_mult) == (False, 0.5)
    for bad in (0.0, -0.1, 1.01):
        with pytest.raises(ValidationError):
            _settings(spreading_associative_weight_mult=bad)


@pytest.mark.postgres_only
async def test_assoc_off_unchanged(session):
    """Flag off is today's code path: a co_mention edge is not crossed, and the
    rows equal those of settings that predate the associative fields."""
    assert ASSOC_FIELDS <= set(Settings.model_fields), "the fields this test leaves out must exist"
    agent = _agent()
    s, n, a, b = uuid4(), uuid4(), uuid4(), uuid4()
    await insert_edge(session, agent, s, n, weight=0.9, method="co_mention")
    await insert_edge(session, agent, s, a, weight=0.7)
    await insert_edge(session, agent, a, b, weight=0.6)
    seeds = [(s, "decision", 0.8)]
    base = _settings()
    lacking = SimpleNamespace(**{k: getattr(base, k) for k in Settings.model_fields if k not in ASSOC_FIELDS})

    off = await _act(session, agent, seeds, _settings(spreading_associative_weight_mult=1.0))
    old = await _act(session, agent, seeds, lacking)
    assert off == old
    assert n not in off
    assert set(off) == {s, a, b}
    assert off[b][0] == pytest.approx(0.8 * 0.7 * 0.5 * 0.6 * 0.5)
    snapshot = SNAPSHOT.read_text(encoding="utf-8")
    assert await render(lacking) == snapshot
    assert await render(_settings(spreading_associative_weight_mult=0.3)) == snapshot


@pytest.mark.postgres_only
async def test_assoc_comention_reduced(session):
    agent = _agent()
    s, n = uuid4(), uuid4()
    await insert_edge(session, agent, s, n, weight=0.9, method="co_mention")
    on = await _act(session, agent, [(s, "decision", 0.8)], _assoc())
    assert on[n] == (pytest.approx(0.8 * 0.9 * 0.5 * 0.5), 1)  # 0.18


@pytest.mark.postgres_only
async def test_assoc_cooccurred_reduced(session):
    """``co_occurred`` is matched by relation (its builder writes method ``co_occurrence``)."""
    agent = _agent()
    s, n = uuid4(), uuid4()
    await insert_edge(session, agent, s, n, weight=0.8, relation="co_occurred", method="co_occurrence")
    on = await _act(session, agent, [(s, "decision", 0.8)], _assoc())
    assert on[n] == (pytest.approx(0.8 * 0.8 * 0.5 * 0.5), 1)  # 0.16


@pytest.mark.postgres_only
async def test_assoc_mult_applies_per_associative_hop(session):
    """S -co_mention-> A -inferred-> B -co_occurred-> C at max depth 3: each
    associative hop pays the multiplier once, the inferred hop does not."""
    agent = _agent()
    s, a, b, c = uuid4(), uuid4(), uuid4(), uuid4()
    await insert_edge(session, agent, s, a, weight=0.9, method="co_mention")
    await insert_edge(session, agent, a, b, weight=0.8)
    await insert_edge(session, agent, b, c, weight=1.0, relation="co_occurred", method="co_occurrence")
    on = await _act(
        session, agent, [(s, "decision", 0.8)],
        _assoc(spreading_associative_weight_mult=0.6, spreading_activation_max_depth=3),
    )
    assert on[a][0] == pytest.approx(0.8 * 0.9 * 0.5 * 0.6)
    assert on[b][0] == pytest.approx(0.8 * 0.9 * 0.5 * 0.6 * 0.8 * 0.5)
    assert on[c][0] == pytest.approx(0.8 * 0.9 * 0.5 * 0.6 * 0.8 * 0.5 * 1.0 * 0.5 * 0.6)


@pytest.mark.postgres_only
async def test_assoc_still_excludes_lineage(session):
    """With the flag on, supersedes / contradicts / happened_before stay refused,
    also when such an edge carries method co_mention; the co_mention edge to N
    shows the flag is really on."""
    agent = _agent()
    s, n = uuid4(), uuid4()
    lineage = {rel: uuid4() for rel in ("supersedes", "contradicts", "happened_before")}
    for rel, target in lineage.items():
        await insert_edge(session, agent, s, target, weight=1.0, relation=rel)
    disguised = uuid4()
    await insert_edge(session, agent, s, disguised, weight=1.0, relation="supersedes", method="co_mention")
    await insert_edge(session, agent, s, n, weight=0.9, method="co_mention")
    on = await _act(session, agent, [(s, "decision", 0.8)], _assoc())
    assert set(on) == {s, n}


@pytest.mark.postgres_only
async def test_assoc_never_amplifies(session):
    """Mult 1.0 on a random graph mixing associative and inferred edges (some
    weights above 1, which the CTE clamps): every activation stays <= its
    winning seed's score, and the flag only adds reach."""
    rng = random.Random(4)
    agent = _agent()
    nodes = [uuid4() for _ in range(30)]
    pairs: set[tuple[int, int]] = set()
    while len(pairs) < 90:
        i, j = rng.randrange(30), rng.randrange(30)
        if i != j and (i, j) not in pairs and (j, i) not in pairs:
            pairs.add((i, j))
    for k, (i, j) in enumerate(sorted(pairs)):
        weight = round(rng.uniform(0.3, 1.4), 3)
        kind = k % 3
        if kind == 0:
            await insert_edge(session, agent, nodes[i], nodes[j], weight=weight, method="co_mention")
        elif kind == 1:
            await insert_edge(session, agent, nodes[i], nodes[j], weight=weight,
                              relation="co_occurred", method="co_occurrence")
        else:
            await insert_edge(session, agent, nodes[i], nodes[j], weight=weight)
    seeds = [(nodes[0], "decision", 0.9), (nodes[7], "fact", 0.6)]
    score = {sid: sc for sid, _, sc in seeds}

    seed_of: dict[UUID, UUID] = {}
    on = await _act(session, agent, seeds, _assoc(spreading_associative_weight_mult=1.0), seed_out=seed_of)
    off = await _act(session, agent, seeds, _settings())
    assert set(off) <= set(on)
    assert len(on) > len(off), "non-vacuous: associative edges add reach"
    for nid, (act, _) in on.items():
        assert act <= score[seed_of[nid]] + 1e-12
        if nid in off:
            assert act >= off[nid][0] - 1e-12, "mult 1.0 only adds paths, so MAX cannot fall"


@pytest.mark.postgres_only
async def test_assoc_counts_in_fan_degree(session):
    """The fan degree count shares the traversal's predicate: S -> H -> T, with
    39 co_mention leaves on H. Flag on, deg(H) = 41 and T is damped; flag off,
    deg(H) = 2 (the leaves are not eligible) and T is not."""
    agent = _agent()
    s, h, t = uuid4(), uuid4(), uuid4()
    await insert_edge(session, agent, s, h)
    await insert_edge(session, agent, h, t)
    for _ in range(39):
        await insert_edge(session, agent, h, uuid4(), method="co_mention")
    seeds = [(s, "decision", 0.8)]
    fan = {"spreading_fan_normalisation_enabled": True}
    on = await _act(session, agent, seeds, _assoc(**fan))
    off = await _act(session, agent, seeds, _settings(**fan))
    assert on[t][0] == pytest.approx(0.8 * 0.5 * 0.5 * (10 / 41) ** 0.5)
    assert off[t][0] == pytest.approx(0.2)


@pytest.mark.postgres_only
async def test_density_gate_ignores_assoc_flag(session):
    """compute_graph_density counts no associative edge, and it takes no settings,
    so the traversal flag cannot reach the auto-mode gate. 20 associative edges
    beside 2 inferred ones on 3 nodes: density stays 2 / 3."""
    assert list(inspect.signature(compute_graph_density).parameters) == ["session", "agent_id"]
    agent = _agent()
    a, b, c = uuid4(), uuid4(), uuid4()
    await insert_edge(session, agent, a, b)
    await insert_edge(session, agent, b, c)
    for k in range(20):
        if k % 2:
            await insert_edge(session, agent, a, uuid4(), method="co_mention")
        else:
            await insert_edge(session, agent, c, uuid4(), relation="co_occurred", method="co_occurrence")
    assert await compute_graph_density(session, agent) == pytest.approx(2 / 3)
