"""F106 Task 3: co-occurrence builder v2 (``cooccurrence_v2_enabled``, default off).

Under v2 two active facts of one source episode link only when they share a thread
signal: a pair cosine of at least ``cooccurrence_min_cosine``, a shared
``heart.fact_entity_keys`` key, or enumerated facts of one transcript chunk at most
``cooccurrence_ordinal_window`` positions apart. A pair at or above
``graph_threshold_fact_fact`` is left to the cosine densifier. There is no
episode-size gate, each fact's co_occurrence edges are capped over its lifetime, and
a bounded maintenance pass deletes or restores the builder's own edges.
``plan_cooccurrence_changes`` computes all of it without writing;
``build_cooccurrence_edges`` applies the plan and returns the rows actually inserted.

With v2 off the builder is the Gap-1 one (episodes of 2..6 facts, every pair), apart
from the honest insert count, which applies to both.

DB tests commit rows under a per-test agent id, since the builder opens its own
sessions.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import text

from nous.brain.graph_densifier import ComentionPlan, CooccurrencePlan, GraphDensifier
from nous.brain.graph_linker import GraphLinker
from nous.config import Settings
from nous.events import EventBus
from nous.handlers.sleep_handler import SleepHandler

W = 0.9  # cooccurrence_weight default
T0 = datetime(2026, 1, 1, tzinfo=UTC)
DIM = 1536
CHUNK = 1_000_000


def _settings(**over) -> Settings:
    return Settings(_env_file=None, **{"cooccurrence_linking_enabled": True, **over})


def _v2(**over) -> Settings:
    return _settings(cooccurrence_v2_enabled=True, **over)


def _densifier(db, s: Settings, agent: str) -> GraphDensifier:
    return GraphDensifier(db, GraphLinker(db, None, s, agent), None, s, agent)


def _vec(c: float, base: int, other: int) -> str:
    """A unit vector at cosine ``c`` from ``_axis(base)``, in the (base, other) plane."""
    v = [0.0] * DIM
    v[base - 1] = c
    v[other - 1] = math.sqrt(1.0 - c * c)
    return "[" + ",".join(str(x) for x in v) + "]"


def _axis(k: int) -> str:
    """The unit basis vector e_k (1-based) as a pgvector literal."""
    return _vec(1.0, k, k + 1)


def _canon(a: str, b: str) -> tuple[str, str]:
    return (a, b) if a < b else (b, a)


async def _episode(db, agent: str) -> str:
    eid = uuid4()
    async with db.session() as session:
        await session.execute(
            text("INSERT INTO heart.episodes (id, agent_id, summary) VALUES (:id, :a, 'ep')"),
            {"id": eid, "a": agent},
        )
        await session.commit()
    return str(eid)


async def _fact(
    db, agent: str, episode: str | None, minute: int, *, active: bool = True,
    embedding: str | None = None, ordinal: int | None = None, keys: tuple[str, ...] = (),
) -> str:
    """Insert a fact learned ``minute`` minutes after T0 (larger = newer), with its
    entity keys. No embedding or ordinal unless given."""
    fid = uuid4()
    async with db.session() as session:
        await session.execute(
            text(
                "INSERT INTO heart.facts (id, agent_id, content, active, learned_at, embedding, "
                "source_episode_id, source_ordinal) "
                "VALUES (:id, :a, :c, :act, :t, CAST(:e AS vector), :ep, :o)"
            ),
            {"id": fid, "a": agent, "c": f"fact {minute}", "act": active,
             "t": T0 + timedelta(minutes=minute), "e": embedding, "ep": episode, "o": ordinal},
        )
        for key in keys:
            await session.execute(
                text(
                    "INSERT INTO heart.fact_entity_keys (fact_id, entity_key, agent_id) "
                    "VALUES (:f, :k, :a)"
                ),
                {"f": fid, "k": key, "a": agent},
            )
        await session.commit()
    return str(fid)


async def _edge(
    db, agent: str, a: str, b: str, weight: float | None, *,
    relation: str = "co_occurred", method: str = "co_occurrence",
) -> str:
    """Insert a canonical fact-fact edge (by default a co_occurrence one)."""
    s, t = _canon(a, b)
    async with db.session() as session:
        eid = (await session.execute(
            text(
                "INSERT INTO brain.graph_edges (agent_id, source_id, target_id, source_type, "
                "target_type, relation, weight, auto_linked, extraction_method) "
                "VALUES (:a, :s, :t, 'fact', 'fact', :r, :w, true, :m) RETURNING id"
            ),
            {"a": agent, "s": s, "t": t, "r": relation, "w": weight, "m": method},
        )).scalar()
        await session.commit()
    return str(eid)


async def _edges(db, agent: str) -> dict[tuple[str, str], float | None]:
    """This agent's co_occurrence edges: canonical pair -> weight."""
    async with db.session() as session:
        rows = (await session.execute(
            text(
                "SELECT source_id, target_id, weight FROM brain.graph_edges "
                "WHERE agent_id = :a AND extraction_method = 'co_occurrence'"
            ),
            {"a": agent},
        )).all()
    return {_canon(str(s), str(t)): w for s, t, w in rows}


def _pairs(plan: CooccurrencePlan) -> set[tuple[str, str]]:
    return {(a, b) for a, b, _ in plan.inserts}


# ---------------------------------------------------------------------------
# Settings (pure)
# ---------------------------------------------------------------------------


def test_new_settings_defaults() -> None:
    s = Settings(_env_file=None)
    assert s.cooccurrence_v2_enabled is False
    assert s.cooccurrence_min_cosine == 0.30
    assert s.cooccurrence_ordinal_window == 3
    assert s.cooccurrence_max_edges_per_fact == 10
    assert s.cooccurrence_prune_max_per_cycle == 2000


@pytest.mark.parametrize(
    "over",
    [
        {"cooccurrence_min_cosine": -0.1},
        {"cooccurrence_min_cosine": 1.1},
        {"cooccurrence_ordinal_window": -1},
        {"cooccurrence_max_edges_per_fact": 0},
        {"cooccurrence_prune_max_per_cycle": -1},
    ],
)
def test_new_settings_bounds(over: dict) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **over)


# ---------------------------------------------------------------------------
# v2: each thread signal alone, the floor and the ceiling
# ---------------------------------------------------------------------------


@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_cosine_signal_and_floor(db) -> None:
    """One episode per pair. cos 0.5 links; cos 0.2 is below the 0.30 floor. Two facts
    on the same axis in different episodes (cos 1) are never a pair."""
    agent = f"test-f106co-cos-{uuid4().hex[:8]}"
    e1, e2 = await _episode(db, agent), await _episode(db, agent)
    p = await _fact(db, agent, e1, 1, embedding=_axis(1))
    q = await _fact(db, agent, e1, 2, embedding=_vec(0.5, 1, 2))
    r = await _fact(db, agent, e2, 3, embedding=_axis(1))
    t = await _fact(db, agent, e2, 4, embedding=_vec(0.2, 1, 3))

    plan = await _densifier(db, _v2(), agent).plan_cooccurrence_changes()
    assert plan.inserts == [(*_canon(p, q), W)]
    assert plan.deletes == [] and plan.reweights == []

    loose = await _densifier(db, _v2(cooccurrence_min_cosine=0.15), agent).plan_cooccurrence_changes()
    assert _pairs(loose) == {_canon(p, q), _canon(r, t)}


@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_cosine_ceiling(db) -> None:
    """cos 0.9 >= graph_threshold_fact_fact (0.82) never links, even with a shared key;
    cos 0.7 links. A pair with a missing embedding cannot be checked against the
    ceiling, so it passes it, and links on a non-cosine signal only."""
    agent = f"test-f106co-ceil-{uuid4().hex[:8]}"
    eps = [await _episode(db, agent) for _ in range(4)]
    a = await _fact(db, agent, eps[0], 1, embedding=_axis(1), keys=("dara velen",))
    b = await _fact(db, agent, eps[0], 2, embedding=_vec(0.9, 1, 2), keys=("dara velen",))
    c = await _fact(db, agent, eps[1], 3, embedding=_axis(3))
    d = await _fact(db, agent, eps[1], 4, embedding=_vec(0.7, 3, 4))
    e = await _fact(db, agent, eps[2], 5, keys=("orion vale",))
    f = await _fact(db, agent, eps[2], 6, embedding=_axis(5), keys=("orion vale",))
    await _fact(db, agent, eps[3], 7)
    await _fact(db, agent, eps[3], 8, embedding=_axis(6))

    plan = await _densifier(db, _v2(), agent).plan_cooccurrence_changes()
    assert _pairs(plan) == {_canon(c, d), _canon(e, f)}

    raised = await _densifier(db, _v2(graph_threshold_fact_fact=0.95), agent).plan_cooccurrence_changes()
    assert _pairs(raised) == {_canon(a, b), _canon(c, d), _canon(e, f)}


@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_shared_entity_key_signal(db) -> None:
    """No embeddings or ordinals: a shared key links. Different keys do not, nor does
    a key shared with an inactive fact, nor a key row under another agent id."""
    agent = f"test-f106co-key-{uuid4().hex[:8]}"
    ep = await _episode(db, agent)
    a = await _fact(db, agent, ep, 1, keys=("dara velen", "halvorsen institute"))
    b = await _fact(db, agent, ep, 2, keys=("dara velen",))
    await _fact(db, agent, ep, 3, keys=("orion vale",))
    await _fact(db, agent, ep, 4, keys=("lumen array",))
    await _fact(db, agent, ep, 5, active=False, keys=("lumen array", "orion vale"))
    c = await _fact(db, agent, ep, 6, keys=("cedar point",))
    async with db.session() as session:
        g = uuid4()
        await session.execute(
            text(
                "INSERT INTO heart.facts (id, agent_id, content, active, learned_at, "
                "source_episode_id) VALUES (:id, :a, 'g', true, :t, :ep)"
            ),
            {"id": g, "a": agent, "t": T0 + timedelta(minutes=7), "ep": ep},
        )
        await session.execute(
            text(
                "INSERT INTO heart.fact_entity_keys (fact_id, entity_key, agent_id) "
                "VALUES (:f, 'cedar point', :other)"
            ),
            {"f": g, "other": f"{agent}-other"},
        )
        await session.commit()

    plan = await _densifier(db, _v2(), agent).plan_cooccurrence_changes()
    assert plan.inserts == [(*_canon(a, b), W)]
    assert _canon(c, str(g)) not in _pairs(plan)


@pytest.mark.postgres_only
@pytest.mark.asyncio
@pytest.mark.parametrize(("window", "expect"), [(3, {"12"}), (4, {"12", "23"}), (0, set())])
async def test_ordinal_signal(db, window: int, expect: set[str]) -> None:
    """Chunk 5 positions 10, 13, 17: 3 apart links at the default window, 4 apart
    needs window 4. Positions 999,999 of chunk 5 and 0 of chunk 6 are 1 apart but in
    different chunks. A fact without an ordinal never links on this signal."""
    agent = f"test-f106co-ord-{uuid4().hex[:8]}"
    ep = await _episode(db, agent)
    o1 = await _fact(db, agent, ep, 1, ordinal=5 * CHUNK + 10)
    o2 = await _fact(db, agent, ep, 2, ordinal=5 * CHUNK + 13)
    o3 = await _fact(db, agent, ep, 3, ordinal=5 * CHUNK + 17)
    await _fact(db, agent, ep, 4, ordinal=5 * CHUNK + 999_999)
    await _fact(db, agent, ep, 5, ordinal=6 * CHUNK)
    await _fact(db, agent, ep, 6)
    names = {"12": _canon(o1, o2), "23": _canon(o2, o3)}

    plan = await _densifier(db, _v2(cooccurrence_ordinal_window=window), agent).plan_cooccurrence_changes()
    assert _pairs(plan) == {names[k] for k in expect}


@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_no_episode_size_gate(db) -> None:
    """A 30-fact episode: v2 links its three threaded pairs (a shared key, adjacent
    ordinals, a cosine) and nothing else; v1 skips the episode (more than 6 facts)."""
    agent = f"test-f106co-big-{uuid4().hex[:8]}"
    ep = await _episode(db, agent)
    facts: list[str] = []
    for i in range(30):
        kw: dict = {"ordinal": i * 10}
        if i in (0, 1):
            kw["keys"] = ("dara velen",)
        if i == 11:
            kw["ordinal"] = 102  # 2 after fact 10
        if i == 20:
            kw["embedding"] = _axis(1)
        if i == 21:
            kw["embedding"] = _vec(0.6, 1, 2)
        facts.append(await _fact(db, agent, ep, i, **kw))

    v2 = await _densifier(db, _v2(), agent).plan_cooccurrence_changes()
    assert _pairs(v2) == {
        _canon(facts[0], facts[1]), _canon(facts[10], facts[11]), _canon(facts[20], facts[21]),
    }
    v1 = await _densifier(db, _settings(), agent).plan_cooccurrence_changes()
    assert v1.inserts == []


# ---------------------------------------------------------------------------
# v2: lifetime cap and link order
# ---------------------------------------------------------------------------


async def _retired_partners(db, agent: str, x: str, n: int) -> None:
    """``n`` co_occurrence edges from ``x`` to inactive facts: maintenance leaves them
    alone (dead-edge prune's job) and they count toward x's cap."""
    for i in range(n):
        partner = await _fact(db, agent, None, 1000 + i, active=False)
        await _edge(db, agent, x, partner, W)


@pytest.mark.postgres_only
@pytest.mark.asyncio
@pytest.mark.parametrize(("existing", "expect_link"), [(9, True), (10, False)])
async def test_lifetime_per_fact_cap(db, existing: int, expect_link: bool) -> None:
    agent = f"test-f106co-cap-{uuid4().hex[:8]}"
    ep = await _episode(db, agent)
    x = await _fact(db, agent, ep, 1, embedding=_axis(1))
    y = await _fact(db, agent, ep, 2, embedding=_vec(0.5, 1, 2))
    await _retired_partners(db, agent, x, existing)

    v2 = await _densifier(db, _v2(), agent).plan_cooccurrence_changes()
    assert v2.deletes == [] and v2.reweights == []
    assert (_canon(x, y) in _pairs(v2)) is expect_link

    # v1 has no cap.
    v1 = await _densifier(db, _settings(), agent).plan_cooccurrence_changes()
    assert _canon(x, y) in _pairs(v1)


@pytest.mark.postgres_only
@pytest.mark.asyncio
@pytest.mark.parametrize(("cap", "expect"), [(10, "z"), (11, "zy"), (12, "zyw")])
async def test_cap_fills_strongest_cosine_first(db, cap: int, expect: str) -> None:
    """X has 9 edges. Its partners: z (cos 0.6), y (cos 0.4), w (shared key, no
    embedding, so no cosine: last). y and z do not qualify with each other (cos 0.24)."""
    agent = f"test-f106co-ord1-{uuid4().hex[:8]}"
    ep = await _episode(db, agent)
    x = await _fact(db, agent, ep, 1, embedding=_axis(1), keys=("dara velen",))
    y = await _fact(db, agent, ep, 2, embedding=_vec(0.4, 1, 2))
    z = await _fact(db, agent, ep, 3, embedding=_vec(0.6, 1, 3))
    w = await _fact(db, agent, ep, 4, keys=("dara velen",))
    await _retired_partners(db, agent, x, 9)
    partner = {"z": z, "y": y, "w": w}

    plan = await _densifier(db, _v2(cooccurrence_max_edges_per_fact=cap), agent).plan_cooccurrence_changes()
    assert plan.inserts == [(*_canon(x, partner[k]), W) for k in expect]


# ---------------------------------------------------------------------------
# v2 maintenance
# ---------------------------------------------------------------------------


async def _maintenance_corpus(db, agent: str) -> dict[str, str]:
    """Edges covering each maintenance case. Returns each case's edge id. Pair k uses
    axes 2k+1 and 2k+2, so facts of different cases are orthogonal."""
    new, old = await _episode(db, agent), await _episode(db, agent)
    ax = iter(range(1, 200, 2))

    async def pair(cos: float | None, ep_a: str, ep_b: str, minute: int, *,
                   keys: tuple[str, ...] = (), b_active: bool = True) -> tuple[str, str]:
        k = next(ax)
        ea = _axis(k) if cos is not None else None
        eb = _vec(cos, k, k + 1) if cos is not None else None
        a = await _fact(db, agent, ep_a, minute, embedding=ea, keys=keys)
        b = await _fact(db, agent, ep_b, minute + 1, embedding=eb, keys=keys, active=b_active)
        return a, b

    ids: dict[str, str] = {}
    # Newer episode (minutes 100+).
    ids["decayed"] = await _edge(db, agent, *await pair(0.5, new, new, 100), 0.05)
    ids["at_weight"] = await _edge(db, agent, *await pair(0.5, new, new, 102), W)
    ids["above"] = await _edge(db, agent, *await pair(0.5, new, new, 104), 0.95)
    ids["no_signal"] = await _edge(db, agent, *await pair(0.0, new, new, 106), W)
    ids["near_dup"] = await _edge(db, agent, *await pair(0.9, new, new, 108), W)
    ids["inactive"] = await _edge(db, agent, *await pair(0.0, new, new, 110, b_active=False), 0.3)
    ids["null_weight"] = await _edge(db, agent, *await pair(None, new, new, 112, keys=("uv",)), None)
    ids["det_no_signal"] = await _edge(
        db, agent, *await pair(0.0, new, new, 114), W, method="deterministic",
    )
    ids["det_decayed"] = await _edge(
        db, agent, *await pair(None, new, new, 116, keys=("wx",)), 0.05, method="deterministic",
    )
    # Older episode (minutes < 100), and a pair across the two episodes.
    ids["old_decayed"] = await _edge(db, agent, *await pair(None, old, old, 10, keys=("op",)), 0.05)
    ids["old_no_signal"] = await _edge(db, agent, *await pair(0.0, old, old, 12), W)
    ids["cross_episode"] = await _edge(db, agent, *await pair(None, new, old, 14, keys=("st",)), W)
    return ids


DELETED = {"no_signal", "near_dup", "old_no_signal", "cross_episode"}
RESTORED = {"decayed", "null_weight", "old_decayed"}


async def _weights_by_id(db, agent: str) -> dict[str, float | None]:
    async with db.session() as session:
        rows = (await session.execute(
            text("SELECT id, weight FROM brain.graph_edges WHERE agent_id = :a"), {"a": agent},
        )).all()
    return {str(i): w for i, w in rows}


@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_maintenance_deletes_and_restores(db) -> None:
    """Covers every co_occurrence edge of the agent, the episode window included
    (pinned to the newest episode here): non-qualifying edges are deleted, qualifying
    ones below the weight (or NULL) restored. An edge above the weight, one with an
    inactive endpoint and the deterministic co_occurred edges are left alone."""
    agent = f"test-f106co-mnt-{uuid4().hex[:8]}"
    ids = await _maintenance_corpus(db, agent)
    by_id = {v: k for k, v in ids.items()}
    before = await _weights_by_id(db, agent)
    dens = _densifier(db, _v2(cooccurrence_max_episodes_per_cycle=1), agent)

    plan = await dens.plan_cooccurrence_changes()
    assert {by_id[e] for e in plan.deletes} == DELETED
    assert {by_id[e] for e, _ in plan.reweights} == RESTORED
    assert all(w == W for _, w in plan.reweights)
    # The deterministic edge on a qualifying pair blocks a co_occurrence link there.
    assert plan.inserts == []

    assert await dens.build_cooccurrence_edges(dry_run=True) == 0
    assert await _weights_by_id(db, agent) == before  # a dry run writes nothing

    assert await dens.build_cooccurrence_edges() == 0
    after = await _weights_by_id(db, agent)
    assert set(after) == set(before) - {ids[k] for k in DELETED}
    for k in RESTORED:
        assert after[ids[k]] == pytest.approx(W)
    for k in ("at_weight", "above", "inactive", "det_no_signal", "det_decayed"):
        assert after[ids[k]] == before[ids[k]], k

    again = await dens.plan_cooccurrence_changes()
    assert again == CooccurrencePlan()  # converged


@pytest.mark.postgres_only
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("bound", "n_deletes", "n_restores"),
    [(0, 0, 0), (2, 2, 0), (4, 4, 0), (5, 4, 1), (7, 4, 3), (100, 4, 3)],
)
async def test_maintenance_respects_the_per_cycle_bound(
    db, bound: int, n_deletes: int, n_restores: int,
) -> None:
    """Deletes plus restores stop at cooccurrence_prune_max_per_cycle, deletes first,
    each in (source_id, target_id) order."""
    agent = f"test-f106co-bnd-{uuid4().hex[:8]}"
    ids = await _maintenance_corpus(db, agent)
    async with db.session() as session:
        order = [str(r[0]) for r in (await session.execute(
            text(
                "SELECT id FROM brain.graph_edges WHERE agent_id = :a "
                "ORDER BY source_id, target_id, id"
            ),
            {"a": agent},
        )).all()]
    want_del = [e for e in order if e in {ids[k] for k in DELETED}][:n_deletes]
    want_res = [e for e in order if e in {ids[k] for k in RESTORED}][:n_restores]
    dens = _densifier(db, _v2(cooccurrence_prune_max_per_cycle=bound), agent)

    plan = await dens.plan_cooccurrence_changes()
    assert plan.deletes == want_del
    assert [e for e, _ in plan.reweights] == want_res

    await dens.build_cooccurrence_edges()
    after = await _weights_by_id(db, agent)
    assert len(after) == len(ids) - n_deletes
    assert sum(1 for k in RESTORED if after[ids[k]] == pytest.approx(W)) == n_restores


@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_deletes_free_cap_slots(db) -> None:
    """X is at the cap (10): 9 retired partners plus one live edge that no longer
    qualifies. Deleting that edge frees the slot for X's qualifying pair."""
    agent = f"test-f106co-free-{uuid4().hex[:8]}"
    ep = await _episode(db, agent)
    x = await _fact(db, agent, ep, 1, embedding=_axis(1))
    y = await _fact(db, agent, ep, 2, embedding=_vec(0.5, 1, 2))
    stale = await _fact(db, agent, ep, 3, embedding=_axis(3))
    await _retired_partners(db, agent, x, 9)
    eid = await _edge(db, agent, x, stale, W)

    plan = await _densifier(db, _v2(), agent).plan_cooccurrence_changes()
    assert plan.deletes == [eid]
    assert _pairs(plan) == {_canon(x, y)}
    # With maintenance off the slot stays taken.
    off = await _densifier(db, _v2(cooccurrence_prune_max_per_cycle=0), agent).plan_cooccurrence_changes()
    assert off == CooccurrencePlan()


@pytest.mark.postgres_only
@pytest.mark.asyncio
@pytest.mark.parametrize("loop", ["edges", "candidates"])
async def test_interrupt_during_the_scan_plans_nothing(db, monkeypatch, loop: str) -> None:
    """The edge and candidate scans yield every 1000 rows (the first row included);
    an interrupt seen there gives an empty plan and the build writes nothing."""
    import asyncio as real_asyncio
    from types import SimpleNamespace

    from nous.brain import graph_densifier as gd

    agent = f"test-f106co-int-{uuid4().hex[:8]}"
    ep = await _episode(db, agent)
    a = await _fact(db, agent, ep, 1, keys=("dara velen",))
    b = await _fact(db, agent, ep, 2, keys=("dara velen",))
    await _fact(db, agent, ep, 3, keys=("dara velen",))
    await _edge(db, agent, a, b, 0.05)
    before = await _edges(db, agent)
    dens = _densifier(db, _v2(), agent)
    interrupt_at_call = {"edges": 1, "candidates": 2}[loop]

    calls = 0

    async def sleep(delay: float) -> None:
        nonlocal calls
        calls += 1
        if calls == interrupt_at_call:
            dens._interrupted = True
        await real_asyncio.sleep(delay)

    monkeypatch.setattr(gd, "asyncio", SimpleNamespace(sleep=sleep))
    assert await dens.build_cooccurrence_edges() == 0
    assert calls == interrupt_at_call
    assert await _edges(db, agent) == before


# ---------------------------------------------------------------------------
# v1 (flag off): today's builder
# ---------------------------------------------------------------------------


async def _v1_corpus(db, agent: str) -> dict[str, list[str]]:
    """Three focused episodes (3 facts each, no signals), one 7-fact episode, oldest
    first; a contradicts edge in the newest focused episode."""
    out: dict[str, list[str]] = {}
    minute = 0
    for name, n in (("old", 3), ("big", 7), ("mid", 3), ("new", 3)):
        ep = await _episode(db, agent)
        out[name] = []
        for _ in range(n):
            out[name].append(await _fact(db, agent, ep, minute))
            minute += 1
    await _edge(db, agent, out["new"][0], out["new"][1], 0.5,
                relation="contradicts", method="heuristic")
    return out


def _all_pairs(fids: list[str]) -> set[tuple[str, str]]:
    return {_canon(a, b) for i, a in enumerate(fids) for b in fids[i + 1:]}


@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_v1_links_every_pair_of_focused_episodes(db) -> None:
    agent = f"test-f106co-v1-{uuid4().hex[:8]}"
    c = await _v1_corpus(db, agent)
    dens = _densifier(db, _settings(), agent)

    plan = await dens.plan_cooccurrence_changes()
    expected = (
        _all_pairs(c["old"]) | _all_pairs(c["mid"]) | _all_pairs(c["new"])
    ) - {_canon(c["new"][0], c["new"][1])}
    assert _pairs(plan) == expected
    assert {w for _, _, w in plan.inserts} == {W}
    assert plan.deletes == [] and plan.reweights == []

    # The episode window counts focused episodes only, newest first.
    two = await _densifier(
        db, _settings(cooccurrence_max_episodes_per_cycle=2), agent,
    ).plan_cooccurrence_changes()
    assert _pairs(two) == expected - _all_pairs(c["old"])

    assert await dens.build_cooccurrence_edges(dry_run=True) == 8
    assert await dens.build_cooccurrence_edges() == 8
    assert set(await _edges(db, agent)) == expected
    assert await dens.build_cooccurrence_edges() == 0  # idempotent


@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_v1_ignores_the_v2_settings_and_never_maintains(db) -> None:
    agent = f"test-f106co-v1s-{uuid4().hex[:8]}"
    c = await _v1_corpus(db, agent)
    # A co_occurrence edge v2 would delete (no signal), and a decayed one.
    await _edge(db, agent, c["big"][0], c["big"][1], W)
    await _edge(db, agent, c["big"][2], c["big"][3], 0.05)

    default = await _densifier(db, _settings(), agent).plan_cooccurrence_changes()
    tight = await _densifier(db, _settings(
        cooccurrence_min_cosine=1.0, cooccurrence_ordinal_window=0,
        cooccurrence_max_edges_per_fact=1, cooccurrence_prune_max_per_cycle=0,
    ), agent).plan_cooccurrence_changes()
    assert default == tight
    assert default.deletes == [] and default.reweights == []
    assert len(default.inserts) == 8


@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_linking_disabled_plans_nothing(db) -> None:
    agent = f"test-f106co-off-{uuid4().hex[:8]}"
    ep = await _episode(db, agent)
    await _fact(db, agent, ep, 1, keys=("dara velen",))
    await _fact(db, agent, ep, 2, keys=("dara velen",))
    for s in (_settings(cooccurrence_linking_enabled=False), _v2(cooccurrence_linking_enabled=False)):
        dens = _densifier(db, s, agent)
        assert await dens.plan_cooccurrence_changes() == CooccurrencePlan()
        assert await dens.build_cooccurrence_edges() == 0


# ---------------------------------------------------------------------------
# The insert count is honest (v1 and v2)
# ---------------------------------------------------------------------------


@pytest.mark.postgres_only
@pytest.mark.asyncio
@pytest.mark.parametrize("v2", [False, True])
async def test_insert_count_excludes_conflicts(db, v2: bool) -> None:
    """A planned pair that another writer linked first hits ON CONFLICT and is not
    counted. Re-applying the same plan inserts and counts nothing."""
    agent = f"test-f106co-cnt-{uuid4().hex[:8]}"
    ep = await _episode(db, agent)
    a = await _fact(db, agent, ep, 1, keys=("dara velen",))
    b = await _fact(db, agent, ep, 2, keys=("dara velen",))
    c = await _fact(db, agent, ep, 3, keys=("dara velen",))
    dens = _densifier(db, _v2() if v2 else _settings(), agent)

    plan = await dens.plan_cooccurrence_changes()
    assert _pairs(plan) == {_canon(a, b), _canon(a, c), _canon(b, c)}
    await _edge(db, agent, a, b, W)  # a concurrent cycle got there first

    assert await dens.build_cooccurrence_edges(plan=plan) == 2
    assert set(await _edges(db, agent)) == _pairs(plan)
    assert await dens.build_cooccurrence_edges(plan=plan) == 0


# ---------------------------------------------------------------------------
# Reporting: run_backfill_cycle -> sleep_stats
# ---------------------------------------------------------------------------


def _stub_cycle(s: Settings) -> GraphDensifier:
    """A densifier whose run_backfill_cycle reaches the co-occurrence step with every
    other pass stubbed and a fixed plan."""
    dens = GraphDensifier(MagicMock(), MagicMock(), None, s, "test-f106co-cycle")
    for name in (
        "backfill_orphan_facts", "backfill_orphan_decisions", "backfill_orphan_episodes",
        "backfill_orphan_procedures", "backfill_orphan_chunks",
        "_build_happened_before_edges", "build_comention_edges",
    ):
        setattr(dens, name, AsyncMock(return_value=0))
    dens.plan_comention_changes = AsyncMock(return_value=ComentionPlan())
    dens.plan_cooccurrence_changes = AsyncMock(return_value=CooccurrencePlan(
        inserts=[("a", "b", 0.9)] * 4, deletes=["d1", "d2"], reweights=[("r1", 0.9)],
    ))
    dens.build_cooccurrence_edges = AsyncMock(return_value=3)
    return dens


@pytest.mark.asyncio
async def test_backfill_cycle_reports_maintenance_under_v2_only() -> None:
    v2 = await _stub_cycle(_v2()).run_backfill_cycle()
    assert v2["_co_occurrence"] == 3  # the build's count, not the plan's
    assert v2["_co_occurrence_deleted"] == 2 and v2["_co_occurrence_reweighted"] == 1

    v1 = await _stub_cycle(_settings()).run_backfill_cycle()
    assert v1["_co_occurrence"] == 3
    assert "_co_occurrence_deleted" not in v1 and "_co_occurrence_reweighted" not in v1


@pytest.mark.asyncio
async def test_backfill_cycle_build_failure_reports_no_maintenance() -> None:
    dens = _stub_cycle(_v2())
    dens.build_cooccurrence_edges = AsyncMock(side_effect=RuntimeError("db down"))
    result = await dens.run_backfill_cycle()
    assert result["_co_occurrence"] == 0
    assert "_co_occurrence_deleted" not in result


def _handler_with(result: dict) -> SleepHandler:
    s = Settings(_env_file=None, graph_backfill_enabled=True)
    handler = SleepHandler(MagicMock(), MagicMock(), s, EventBus())
    densifier = MagicMock()
    densifier.discover_clusters = AsyncMock(return_value=0)
    densifier.run_backfill_cycle = AsyncMock(return_value=dict(result))
    handler._graph_densifier = densifier
    return handler


@pytest.mark.asyncio
async def test_sleep_handler_pops_the_cooccurrence_count() -> None:
    """Before F106 the co-occurrence count was summed into orphan_edges_created."""
    handler = _handler_with({"facts": 3, "_co_mention": 2, "_co_occurrence": 4})
    stats: dict = {}
    assert await handler._phase_graph_densification(stats) is True
    assert stats["orphan_edges_created"] == 3
    assert stats["comention_edges"] == 2
    assert stats["cooccurrence_edges"] == 4
    assert "cooccurrence_edges_deleted" not in stats
    assert "cooccurrence_edges_reweighted" not in stats

    handler = _handler_with({
        "facts": 3, "_co_occurrence": 4,
        "_co_occurrence_deleted": 5, "_co_occurrence_reweighted": 7,
    })
    stats = {}
    assert await handler._phase_graph_densification(stats) is True
    assert stats["orphan_edges_created"] == 3  # the maintenance counts are not edges
    assert stats["cooccurrence_edges_deleted"] == 5
    assert stats["cooccurrence_edges_reweighted"] == 7


@pytest.mark.asyncio
async def test_sleep_handler_audits_maintenance_as_edge_prune() -> None:
    handler = _handler_with({
        "facts": 0, "_co_occurrence": 1,
        "_co_occurrence_deleted": 5, "_co_occurrence_reweighted": 7,
    })
    handler._auditor = MagicMock()
    assert await handler._phase_graph_densification({}) is True
    handler._auditor.record.assert_called_once_with(
        "graph_densify",
        "edge_prune",
        after={"counts": {"cooccurrence_edges_deleted": 5, "cooccurrence_edges_reweighted": 7}},
        rationale="F106 co_occurrence maintenance",
    )

    for result in (
        {"facts": 0, "_co_occurrence": 1, "_co_occurrence_deleted": 0,
         "_co_occurrence_reweighted": 0},
        {"facts": 0, "_co_occurrence": 1},
    ):
        handler = _handler_with(result)
        handler._auditor = MagicMock()
        assert await handler._phase_graph_densification({}) is True
        handler._auditor.record.assert_not_called()


@pytest.mark.asyncio
async def test_densify_summary_audit_counts_cooccurrence_edges() -> None:
    """The graph_densify / edge_add summary row lists cooccurrence_edges."""
    from test_sleep_handler import _make_event, _make_sleep_handler

    handler, *_ = _make_sleep_handler()
    for phase in (
        "_phase_review_decisions", "_phase_prune", "_phase_compress", "_phase_reflect",
        "_phase_resolve_contradictions", "_phase_sweep_key_conflicts",
        "_phase_recover_abandoned_episodes", "_phase_generalize", "_phase_evolve_rubric",
        "_phase_prune_hub_snapshots",
    ):
        setattr(handler, phase, AsyncMock(return_value=True))
    captured: dict = {}

    async def spy(label, op, coro_factory, sleep_stats, count_keys):
        captured[label] = (op, tuple(count_keys))
        return True

    handler._run_audited_phase = spy
    await handler._run_sleep(_make_event("sleep_started"))
    op, keys = captured["graph_densify"]
    assert op == "edge_add" and "cooccurrence_edges" in keys

    # With those keys, a cycle that built co-occurrence edges records them.
    real = _handler_with({"facts": 0, "_co_occurrence": 4})
    real._auditor = MagicMock()
    stats: dict = {}
    await real._run_audited_phase(
        "graph_densify", "edge_add", lambda: real._phase_graph_densification(stats), stats, keys,
    )
    real._auditor.record.assert_called_once_with(
        "graph_densify", "edge_add", after={"counts": {"cooccurrence_edges": 4}},
        rationale="graph_densify phase summary",
    )
