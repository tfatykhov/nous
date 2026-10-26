"""F105 Task 3: fan (degree) normalisation inside the spreading activation CTE.

With the flag on, activation leaving a node of eligible-edge degree ``d`` is
multiplied by ``1`` when ``d <= D0`` and by ``(D0 / d) ** gamma`` above it, so
a hub no longer passes full activation to each of its many neighbours. The hub
graph used throughout: seed S (score 0.8) -- w=1 -> hub H, and H has 39 leaf
edges plus one edge to T, so deg(H) = 41 and deg(S) = 1. With decay 0.5, T's
undamped activation is 0.8 * 0.5 * 0.5 = 0.2.

Every hub-graph search passes ``limit=100``: the 39 leaves and T tie, and the
CTE's ``ORDER BY total_activation DESC`` has no tie key, so the default limit
of 20 would return an arbitrary 20 of the 40 and T could be missing. Nodes are
bare UUIDs; ``graph_edges`` has no foreign key to the node tables.
"""

from __future__ import annotations

import random
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from spreading_v2_helpers import SNAPSHOT, insert_edge, render

from nous.brain.spreading_activation import spreading_activation_search
from nous.config import Settings

FAN_FIELDS = {
    "spreading_fan_normalisation_enabled",
    "spreading_fan_free_degree",
    "spreading_fan_exponent",
}
DAMPED_T = 0.8 * 0.5 * 0.5 * (10 / 41) ** 0.5  # 0.0988


def _settings(**over: Any) -> Settings:
    """Settings defaults with no .env file (decay 0.5, max depth 2)."""
    return Settings(_env_file=None, **over)


def _fan(**over: Any) -> Settings:
    return _settings(spreading_fan_normalisation_enabled=True, **over)


def _agent() -> str:
    return f"fan-{uuid4().hex[:8]}"


async def _hub_graph(session, agent: str) -> tuple[UUID, UUID, UUID, list[UUID]]:
    """S -> H; H -> T and H -> 39 leaves. Returns (S, H, T, leaves)."""
    s, h, t = uuid4(), uuid4(), uuid4()
    leaves = [uuid4() for _ in range(39)]
    await insert_edge(session, agent, s, h)
    await insert_edge(session, agent, h, t)
    for leaf in leaves:
        await insert_edge(session, agent, h, leaf)
    return s, h, t, leaves


async def _act(session, agent: str, seeds, settings, **kwargs: Any) -> dict[UUID, tuple[float, int]]:
    """``node -> (activation, depth)`` over the whole (limit=100) result."""
    rows = await spreading_activation_search(session, agent, seeds, settings, limit=100, **kwargs)
    return {r[0]: (r[2], r[3]) for r in rows}


def test_fan_settings_bounds():
    """gamma > 0 and D0 >= 1 are what keep the factor <= 1 (never amplify)."""
    s = _settings()
    assert (s.spreading_fan_normalisation_enabled, s.spreading_fan_free_degree, s.spreading_fan_exponent) == (
        False, 10, 0.5)
    for bad in ({"spreading_fan_free_degree": 0}, {"spreading_fan_exponent": 0.0},
                {"spreading_fan_exponent": 2.5}):
        with pytest.raises(ValidationError):
            _settings(**bad)


@pytest.mark.postgres_only
async def test_fan_off_is_identical(session):
    """Flag off is today's code path: same rows, activations, depths and order as
    settings that predate the fan fields altogether."""
    assert FAN_FIELDS <= set(Settings.model_fields), "the fields this test leaves out must exist"
    agent = _agent()
    s, _, _, _ = await _hub_graph(session, agent)
    seeds = [(s, "decision", 0.8)]
    base = _settings()
    lacking = SimpleNamespace(**{k: getattr(base, k) for k in Settings.model_fields if k not in FAN_FIELDS})
    off = await spreading_activation_search(
        session, agent, seeds,
        _settings(spreading_fan_free_degree=1, spreading_fan_exponent=2.0), limit=100,
    )
    old = await spreading_activation_search(session, agent, seeds, lacking, limit=100)  # type: ignore[arg-type]
    assert off == old
    assert len(off) == 42, "S, H, T and the 39 leaves"
    assert await render(lacking) == SNAPSHOT.read_text(encoding="utf-8")


@pytest.mark.postgres_only
async def test_fan_dampens_hub_outflow(session):
    agent = _agent()
    s, h, t, _ = await _hub_graph(session, agent)
    seeds = [(s, "decision", 0.8)]
    on = await _act(session, agent, seeds, _fan())
    off = await _act(session, agent, seeds, _settings())
    assert on[t][0] == pytest.approx(DAMPED_T)
    assert off[t][0] == pytest.approx(0.2)
    assert on[h][0] == pytest.approx(0.4), "S has degree 1: its outflow is undamped"


@pytest.mark.postgres_only
async def test_fan_gamma_one_arm(session):
    agent = _agent()
    s, _, t, _ = await _hub_graph(session, agent)
    on = await _act(session, agent, [(s, "decision", 0.8)], _fan(spreading_fan_exponent=1.0))
    assert on[t][0] == pytest.approx(0.2 * 10 / 41)  # 0.0488


@pytest.mark.postgres_only
async def test_fan_pure_synapse_arm(session):
    """D0=1, gamma=1 is SYNAPSE's plain 1/d (S, degree 1, is still free)."""
    agent = _agent()
    s, h, t, _ = await _hub_graph(session, agent)
    on = await _act(
        session, agent, [(s, "decision", 0.8)],
        _fan(spreading_fan_free_degree=1, spreading_fan_exponent=1.0),
    )
    assert on[h][0] == pytest.approx(0.4)
    assert on[t][0] == pytest.approx(0.8 * 0.5 * 0.5 / 41)


@pytest.mark.postgres_only
async def test_fan_leaves_low_degree_untouched(session):
    """Every degree <= D0 (one exactly at D0 = 10): the flag changes nothing."""
    agent = _agent()
    s, a, b, c, m = (uuid4() for _ in range(5))
    await insert_edge(session, agent, s, a, weight=0.9)
    await insert_edge(session, agent, s, b, weight=0.7)
    await insert_edge(session, agent, a, c, weight=0.6)
    await insert_edge(session, agent, s, m, weight=0.8)
    for _ in range(9):  # deg(m) = 10 = D0
        await insert_edge(session, agent, m, uuid4(), weight=0.5)
    seeds = [(s, "decision", 0.8)]
    on = await _act(session, agent, seeds, _fan())
    off = await _act(session, agent, seeds, _settings())
    assert len(off) == 14
    assert on == off


@pytest.mark.postgres_only
async def test_fan_never_amplifies(session):
    """Random graph with hubs: each activation stays <= its winning seed's score
    and <= the flag-off activation of the same node; reachability is unchanged."""
    rng = random.Random(105)
    agent = _agent()
    nodes = [uuid4() for _ in range(40)]
    hubs = nodes[:3]
    pairs: set[tuple[int, int]] = set()
    while len(pairs) < 160:
        i = rng.randrange(3) if rng.random() < 0.5 else rng.randrange(40)
        j = rng.randrange(40)
        if i != j and (i, j) not in pairs and (j, i) not in pairs:
            pairs.add((i, j))
    for i, j in sorted(pairs):
        await insert_edge(session, agent, nodes[i], nodes[j], weight=round(rng.uniform(0.2, 1.0), 3))
    seeds = [(nodes[3], "decision", 0.9), (nodes[11], "fact", 0.7), (hubs[1], "decision", 0.5)]
    score = {sid: sc for sid, _, sc in seeds}

    seed_of: dict[UUID, UUID] = {}
    on = await _act(session, agent, seeds, _fan(), seed_out=seed_of)
    off = await _act(session, agent, seeds, _settings())
    assert set(on) == set(off)
    for nid, (act, _) in on.items():
        assert act <= score[seed_of[nid]]
        assert act <= off[nid][0]
    assert any(on[n][0] < off[n][0] for n in on), "non-vacuous: some winning path leaves a hub"


@pytest.mark.postgres_only
async def test_fan_degree_is_agent_scoped(session):
    """Another agent's 60 edges on H do not count: deg(H) stays 41 (101 would give 0.063)."""
    agent = _agent()
    s, h, t, _ = await _hub_graph(session, agent)
    for _ in range(60):
        await insert_edge(session, f"{agent}-other", h, uuid4())
    on = await _act(session, agent, [(s, "decision", 0.8)], _fan())
    assert on[t][0] == pytest.approx(DAMPED_T)


@pytest.mark.postgres_only
async def test_fan_degree_uses_eligibility(session):
    """Edges the traversal refuses are not fan: 30 `supersedes` edges (refused by
    relation) and 30 `co_mention` edges (refused by method) leave deg(H) at 41."""
    agent = _agent()
    s, h, t, _ = await _hub_graph(session, agent)
    for _ in range(30):
        await insert_edge(session, agent, h, uuid4(), relation="supersedes")
        await insert_edge(session, agent, h, uuid4(), method="co_mention")
    on = await _act(session, agent, [(s, "decision", 0.8)], _fan())
    assert on[t][0] == pytest.approx(DAMPED_T)


@pytest.mark.postgres_only
async def test_fan_degree_zero_division_safe(session):
    """A seed with no edges counts 0, takes the d <= D0 branch, returns itself."""
    agent = _agent()
    lone = uuid4()
    rows = await spreading_activation_search(
        session, agent, [(lone, "decision", 0.7)],
        _fan(spreading_fan_free_degree=1, spreading_fan_exponent=1.0), limit=100,
    )
    assert rows == [(lone, "decision", 0.7, 0)]


@pytest.mark.postgres_only
async def test_parent_out_reports_via_node(session):
    """parent_out maps each row to the node its winning path's last hop left
    (a seed to itself), and does not change which path wins."""
    agent = _agent()
    s, h, t, leaves = await _hub_graph(session, agent)
    seeds = [(s, "decision", 0.8)]
    parents: dict[UUID, UUID] = {}
    with_parents = await _act(session, agent, seeds, _settings(), parent_out=parents)
    assert with_parents == await _act(session, agent, seeds, _settings())
    assert parents[s] == s
    assert parents[h] == s
    assert parents[t] == h
    assert all(parents[leaf] == h for leaf in leaves)
    assert set(parents) == set(with_parents)

    assert await render(_settings(), parent_out=None) == SNAPSHOT.read_text(encoding="utf-8")
    assert "parent_id" in await render(_settings(), parent_out={})
