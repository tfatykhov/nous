"""F106 Task 2: co-mention builder v2 (``comention_v2_enabled``, default off).

Under v2 the builder indexes the newest ``comention_index_max_facts`` active facts,
counts each entity's frequency (``df``) over that whole index, drops stoplisted
phrases, caps each fact's co_mention degree over its lifetime, weights an edge by
the rarity of its rarest shared entity, and runs a bounded maintenance pass that
deletes or re-weights its own edges. ``plan_comention_changes`` computes all of it
without writing; ``build_comention_edges`` applies the plan.

With v2 off the builder is the F076 one: ``tests/test_graph_densifier.py`` and
``tests/test_comention_linking.py`` pin it, unchanged.

DB tests commit rows under a per-test agent id, since the builder opens its own
sessions.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy import text

from nous.brain.entity_extraction import extract_entities
from nous.brain.graph_densifier import ComentionPlan, GraphDensifier, _comention_rarity_weight
from nous.brain.graph_linker import GraphLinker
from nous.config import Settings
from nous.events import EventBus
from nous.handlers.sleep_handler import SleepHandler

BASE = 0.9  # comention_weight default
T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _settings(**over) -> Settings:
    return Settings(_env_file=None, **{"comention_linking_enabled": True, **over})


def _v2(**over) -> Settings:
    return _settings(comention_v2_enabled=True, **over)


def _densifier(db, s: Settings, agent: str) -> GraphDensifier:
    return GraphDensifier(db, GraphLinker(db, None, s, agent), None, s, agent)


async def _fact(db, agent: str, content: str, minute: int, *, active: bool = True) -> str:
    """Insert a fact learned ``minute`` minutes after T0 (larger = newer)."""
    fid = uuid4()
    async with db.session() as session:
        await session.execute(
            text(
                "INSERT INTO heart.facts (id, agent_id, content, active, learned_at) "
                "VALUES (:id, :a, :c, :act, :t)"
            ),
            {"id": fid, "a": agent, "c": content, "act": active,
             "t": T0 + timedelta(minutes=minute)},
        )
        await session.commit()
    return str(fid)


def _canon(a: str, b: str) -> tuple[str, str]:
    return (a, b) if a < b else (b, a)


async def _cm_edge(db, agent: str, a: str, b: str, weight: float) -> str:
    """Insert a canonical co_mention edge, as the builder writes it."""
    s, t = _canon(a, b)
    async with db.session() as session:
        eid = (await session.execute(
            text(
                "INSERT INTO brain.graph_edges (agent_id, source_id, target_id, source_type, "
                "target_type, relation, weight, auto_linked, extraction_method) "
                "VALUES (:a, :s, :t, 'fact', 'fact', 'related_to', :w, true, 'co_mention') "
                "RETURNING id"
            ),
            {"a": agent, "s": s, "t": t, "w": weight},
        )).scalar()
        await session.commit()
    return str(eid)


async def _cm_edges(db, agent: str) -> dict[tuple[str, str], float]:
    """This agent's co_mention edges: canonical pair -> weight."""
    async with db.session() as session:
        rows = (await session.execute(
            text(
                "SELECT source_id, target_id, weight FROM brain.graph_edges "
                "WHERE agent_id = :a AND extraction_method = 'co_mention'"
            ),
            {"a": agent},
        )).all()
    return {_canon(str(s), str(t)): w for s, t, w in rows}


def _pairs(plan: ComentionPlan) -> set[tuple[str, str]]:
    return {(a, b) for a, b, _ in plan.inserts}


# ---------------------------------------------------------------------------
# Stoplist (pure)
# ---------------------------------------------------------------------------

STOPPED = [
    ("Mon Sep 14 deploy went fine", "mon sep"),
    ("Tuesday October 3 was the cutover", "tuesday october"),
    ("posted by Build Bot Mon Sep 14", "build bot mon sep"),  # anywhere in the phrase
    ("the call is at 10:30 AM EDT sharp", "am edt"),
    ("runs at 9 PM UTC daily", "pm utc"),
    ("DO NOT push to main", "do not"),
    ("you MUST NOT skip review", "must not"),
    ("this is a HARD RULE here", "hard rule"),
    ("Standing Rule Alpha applies to merges", "standing rule alpha"),
    ("we chose Option B after review", "option b"),
    ("fixed the Codex P1 finding", "codex p1"),
]

KEPT = [
    ("Dara Velen leads the team", "dara velen"),
    ("we chose Option Bravo after review", "option bravo"),
    ("the Codex Review bot ran", "codex review"),
    ("Monday Night Football is on", "monday night football"),
    ("the Standing Committee met", "standing committee"),
]


@pytest.mark.parametrize(("text_in", "phrase"), STOPPED)
def test_stoplist_drops_phrase(text_in: str, phrase: str) -> None:
    assert phrase in extract_entities(text_in)  # stoplist=False: unchanged
    assert phrase not in extract_entities(text_in, stoplist=True)


@pytest.mark.parametrize(("text_in", "phrase"), KEPT)
def test_stoplist_keeps_entities(text_in: str, phrase: str) -> None:
    assert phrase in extract_entities(text_in)
    assert phrase in extract_entities(text_in, stoplist=True)


def test_stoplist_false_is_the_default() -> None:
    sample = "Mon Sep 14: Dara Velen met Orion Vale at 10:30 AM EDT. DO NOT forget Option B."
    assert extract_entities(sample) == extract_entities(sample, stoplist=False)
    assert extract_entities(sample, stoplist=True) == {"dara velen", "orion vale"}


# ---------------------------------------------------------------------------
# Rarity weight (pure)
# ---------------------------------------------------------------------------


def test_rarity_weight_formula() -> None:
    assert _comention_rarity_weight(BASE, 10, 2) == pytest.approx(BASE)
    assert _comention_rarity_weight(BASE, 10, 4) == pytest.approx(
        BASE * math.log(10 / 4) / math.log(10 / 2)
    )
    assert _comention_rarity_weight(BASE, 10, 10) == pytest.approx(0.0)
    # Rarer is heavier.
    assert _comention_rarity_weight(BASE, 100, 3) > _comention_rarity_weight(BASE, 100, 7)
    # N <= 2: the base weight.
    assert _comention_rarity_weight(BASE, 2, 2) == BASE
    assert _comention_rarity_weight(0.5, 1, 2) == 0.5


def test_new_settings_defaults() -> None:
    s = Settings(_env_file=None)
    assert s.comention_v2_enabled is False
    assert s.comention_index_max_facts == 50000
    assert s.comention_prune_max_per_cycle == 2000


# ---------------------------------------------------------------------------
# v1 (flag off): the plan is today's insert list
# ---------------------------------------------------------------------------


@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_v1_plan_is_todays_inserts_without_maintenance(db) -> None:
    agent = f"test-f106cm-v1-{uuid4().hex[:8]}"
    s = _settings()
    dens = _densifier(db, s, agent)
    a = await _fact(db, agent, "Dara Velen leads Project Helios", 1)
    b = await _fact(db, agent, "Dara Velen studied at the Halvorsen Institute", 2)
    c = await _fact(db, agent, "Mon Sep 14 standup was short", 3)
    d = await _fact(db, agent, "Mon Sep 14 retro ran long", 4)
    # A decayed co_mention edge on a stoplist-only pair: v1 neither deletes nor
    # re-weights it.
    await _cm_edge(db, agent, c, d, 0.01)

    plan = await dens.plan_comention_changes()
    assert plan.inserts == [(*_canon(a, b), BASE)]
    assert plan.deletes == [] and plan.reweights == []

    assert await dens.build_comention_edges(dry_run=True) == 1
    assert await dens.build_comention_edges() == 1
    assert await _cm_edges(db, agent) == {_canon(a, b): BASE, _canon(c, d): 0.01}


@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_linking_disabled_plans_nothing(db) -> None:
    agent = f"test-f106cm-off-{uuid4().hex[:8]}"
    s = _v2(comention_linking_enabled=False)
    await _fact(db, agent, "Dara Velen leads Project Helios", 1)
    await _fact(db, agent, "Dara Velen studied at the Halvorsen Institute", 2)
    plan = await _densifier(db, s, agent).plan_comention_changes()
    assert plan == ComentionPlan()


# ---------------------------------------------------------------------------
# v2: index, hub gate, lifetime cap, weights
# ---------------------------------------------------------------------------


@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_hub_gate_counts_the_whole_index(db) -> None:
    """11 old facts name Orion Vale, outside v1's 5-fact window; 2 of the 5 newest
    name it too. v1 sees df=2 and links the newest pair; v2 counts df=13 > 10."""
    agent = f"test-f106cm-hub-{uuid4().hex[:8]}"
    for i in range(11):
        await _fact(db, agent, f"Orion Vale archive note {i}", i)
    n1 = await _fact(db, agent, "Orion Vale shipped the release", 100)
    n2 = await _fact(db, agent, "Orion Vale fixed the build", 101)
    d1 = await _fact(db, agent, "Dara Velen leads Project Helios", 102)
    d2 = await _fact(db, agent, "Dara Velen studied at the Halvorsen Institute", 103)
    await _fact(db, agent, "a plain note", 104)

    v1 = await _densifier(db, _settings(comention_max_facts_per_cycle=5), agent).plan_comention_changes()
    assert _pairs(v1) == {_canon(n1, n2), _canon(d1, d2)}

    v2 = await _densifier(db, _v2(comention_max_facts_per_cycle=5), agent).plan_comention_changes()
    assert _pairs(v2) == {_canon(d1, d2)}


@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_links_old_and_new_fact_across_the_window(db) -> None:
    agent = f"test-f106cm-old-{uuid4().hex[:8]}"
    old = await _fact(db, agent, "the Halvorsen Institute opened a lab", 1)
    for i in range(4):
        await _fact(db, agent, f"a plain note {i}", 10 + i)
    new = await _fact(db, agent, "funding for the Halvorsen Institute arrived", 20)

    v1 = await _densifier(db, _settings(comention_max_facts_per_cycle=5), agent).plan_comention_changes()
    assert v1.inserts == []

    dens = _densifier(db, _v2(comention_max_facts_per_cycle=5), agent)
    plan = await dens.plan_comention_changes()
    assert plan.inserts == [(*_canon(old, new), BASE)]
    assert await dens.build_comention_edges() == 1
    assert await _cm_edges(db, agent) == {_canon(old, new): BASE}


@pytest.mark.postgres_only
@pytest.mark.asyncio
@pytest.mark.parametrize(("existing", "expect_link"), [(19, True), (20, False)])
async def test_lifetime_per_fact_cap(db, existing: int, expect_link: bool) -> None:
    """A fact's existing co_mention edges count toward comention_max_edges_per_node
    (20). The partners are inactive, so outside the index: maintenance leaves those
    edges alone and they still count."""
    agent = f"test-f106cm-cap-{uuid4().hex[:8]}"
    x = await _fact(db, agent, "Dara Velen leads Project Helios", 100)
    y = await _fact(db, agent, "Dara Velen studied at the Halvorsen Institute", 101)
    for i in range(existing):
        partner = await _fact(db, agent, f"retired note {i}", i, active=False)
        await _cm_edge(db, agent, x, partner, BASE)

    v2 = await _densifier(db, _v2(), agent).plan_comention_changes()
    assert v2.deletes == [] and v2.reweights == []
    assert (_canon(x, y) in _pairs(v2)) is expect_link

    # v1 caps per cycle only, so it links the pair either way.
    v1 = await _densifier(db, _settings(), agent).plan_comention_changes()
    assert _canon(x, y) in _pairs(v1)


@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_rarity_weights_from_the_rarest_shared_entity(db) -> None:
    """N=10 indexed facts. Dara Velen is in 2 (df=2), Halvorsen Institute in 4.
    P and Q share both, so their edge takes df=2's weight."""
    agent = f"test-f106cm-w-{uuid4().hex[:8]}"
    p = await _fact(db, agent, "Dara Velen joined the Halvorsen Institute", 1)
    q = await _fact(db, agent, "Dara Velen left the Halvorsen Institute", 2)
    r = await _fact(db, agent, "the Halvorsen Institute hired staff", 3)
    t = await _fact(db, agent, "the Halvorsen Institute moved buildings", 4)
    for i in range(6):
        await _fact(db, agent, f"a plain note {i}", 10 + i)

    dens = _densifier(db, _v2(), agent)
    plan = await dens.plan_comention_changes()
    w4 = BASE * math.log(10 / 4) / math.log(10 / 2)
    expected = {_canon(p, q): BASE}
    for pair in [(p, r), (p, t), (q, r), (q, t), (r, t)]:
        expected[_canon(*pair)] = w4
    assert {(a, b): w for a, b, w in plan.inserts} == pytest.approx(expected)

    assert await dens.build_comention_edges() == 6
    assert await _cm_edges(db, agent) == pytest.approx(expected)


# ---------------------------------------------------------------------------
# v2 maintenance
# ---------------------------------------------------------------------------


async def _maintenance_corpus(db, agent: str) -> dict[str, tuple[str, str]]:
    """Facts and co_mention edges covering each maintenance case. Returns the
    canonical pair of each case's edge."""
    hub = [await _fact(db, agent, f"Orion Vale archive note {i}", i) for i in range(11)]
    c = await _fact(db, agent, "Mon Sep 14 standup was short", 20)
    d = await _fact(db, agent, "Mon Sep 14 retro ran long", 21)
    e = await _fact(db, agent, "Dara Velen leads Project Helios", 22)
    f = await _fact(db, agent, "Dara Velen studied at the Halvorsen Institute", 23)
    g = await _fact(db, agent, "the Lumen Array came online", 24)
    h = await _fact(db, agent, "the Lumen Array went offline", 25)
    i = await _fact(db, agent, "a plain active note", 26)
    j = await _fact(db, agent, "a retired note", 27, active=False)
    await _cm_edge(db, agent, hub[0], hub[1], BASE)  # shares only a hub (df=11)
    await _cm_edge(db, agent, c, d, BASE)            # shares only a stoplisted phrase
    await _cm_edge(db, agent, e, f, 0.01)            # qualifying, decayed
    await _cm_edge(db, agent, g, h, BASE)            # qualifying, at its weight
    await _cm_edge(db, agent, i, j, 0.3)             # an endpoint outside the index
    return {
        "hub": _canon(hub[0], hub[1]), "stop": _canon(c, d), "decayed": _canon(e, f),
        "ok": _canon(g, h), "outside": _canon(i, j),
    }


@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_maintenance_deletes_and_restores(db) -> None:
    agent = f"test-f106cm-mnt-{uuid4().hex[:8]}"
    pairs = await _maintenance_corpus(db, agent)
    before = await _cm_edges(db, agent)
    dens = _densifier(db, _v2(), agent)

    plan = await dens.plan_comention_changes()
    assert len(plan.deletes) == 2
    assert [w for _, w in plan.reweights] == pytest.approx([BASE])  # df=2 -> the base
    assert plan.inserts == []

    assert await dens.build_comention_edges(dry_run=True) == 0
    assert await _cm_edges(db, agent) == before  # a dry run writes nothing

    assert await dens.build_comention_edges() == 0
    after = await _cm_edges(db, agent)
    assert pairs["hub"] not in after and pairs["stop"] not in after
    assert after[pairs["decayed"]] == pytest.approx(BASE)
    assert after[pairs["ok"]] == pytest.approx(BASE)
    assert after[pairs["outside"]] == pytest.approx(0.3)

    # Converged: a second plan has nothing to do.
    again = await dens.plan_comention_changes()
    assert again.deletes == [] and again.reweights == [] and again.inserts == []


@pytest.mark.postgres_only
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("bound", "n_deletes", "n_reweights"),
    [(0, 0, 0), (1, 1, 0), (2, 2, 0), (3, 2, 1)],
)
async def test_maintenance_respects_the_per_cycle_bound(
    db, bound: int, n_deletes: int, n_reweights: int,
) -> None:
    """Deletes plus reweights stop at comention_prune_max_per_cycle, deletes first."""
    agent = f"test-f106cm-bnd-{uuid4().hex[:8]}"
    await _maintenance_corpus(db, agent)
    dens = _densifier(db, _v2(comention_prune_max_per_cycle=bound), agent)

    plan = await dens.plan_comention_changes()
    assert (len(plan.deletes), len(plan.reweights)) == (n_deletes, n_reweights)

    await dens.build_comention_edges()
    after = await _cm_edges(db, agent)
    assert len(after) == 5 - n_deletes
    assert sum(1 for w in after.values() if w == pytest.approx(0.01)) == 1 - n_reweights


# ---------------------------------------------------------------------------
# Reporting: run_backfill_cycle -> sleep_stats
# ---------------------------------------------------------------------------


def _stub_cycle(s: Settings) -> GraphDensifier:
    """A densifier whose run_backfill_cycle reaches the co-mention step with every
    other pass stubbed to 0 and a fixed plan."""
    dens = GraphDensifier(MagicMock(), MagicMock(), None, s, "test-f106cm-cycle")
    for name in (
        "backfill_orphan_facts", "backfill_orphan_decisions", "backfill_orphan_episodes",
        "backfill_orphan_procedures", "backfill_orphan_chunks",
        "_build_happened_before_edges", "build_cooccurrence_edges",
    ):
        setattr(dens, name, AsyncMock(return_value=0))
    dens.plan_comention_changes = AsyncMock(return_value=ComentionPlan(
        inserts=[("a", "b", 0.5)] * 4, deletes=["d1", "d2"], reweights=[("r1", 0.4)],
    ))
    dens.build_comention_edges = AsyncMock(return_value=4)
    return dens


@pytest.mark.asyncio
async def test_backfill_cycle_reports_maintenance_under_v2_only() -> None:
    v2 = await _stub_cycle(_v2()).run_backfill_cycle()
    assert v2["_co_mention"] == 4
    assert v2["_co_mention_deleted"] == 2 and v2["_co_mention_reweighted"] == 1

    v1 = await _stub_cycle(_settings()).run_backfill_cycle()
    assert v1["_co_mention"] == 4
    assert "_co_mention_deleted" not in v1 and "_co_mention_reweighted" not in v1


@pytest.mark.asyncio
async def test_backfill_cycle_build_failure_reports_no_maintenance() -> None:
    dens = _stub_cycle(_v2())
    dens.build_comention_edges = AsyncMock(side_effect=RuntimeError("db down"))
    result = await dens.run_backfill_cycle()
    assert result["_co_mention"] == 0
    assert "_co_mention_deleted" not in result


@pytest.mark.asyncio
async def test_sleep_handler_pops_maintenance_counts() -> None:
    s = Settings(_env_file=None, graph_backfill_enabled=True)
    handler = SleepHandler(MagicMock(), MagicMock(), s, EventBus())
    densifier = MagicMock()
    densifier.discover_clusters = AsyncMock(return_value=0)
    handler._graph_densifier = densifier

    densifier.run_backfill_cycle = AsyncMock(return_value={
        "facts": 3, "_co_mention": 2, "_co_mention_deleted": 5, "_co_mention_reweighted": 7,
    })
    stats: dict = {}
    assert await handler._phase_graph_densification(stats) is True
    assert stats["orphan_edges_created"] == 3  # the maintenance counts are not edges
    assert stats["comention_edges"] == 2
    assert stats["comention_edges_deleted"] == 5
    assert stats["comention_edges_reweighted"] == 7

    densifier.run_backfill_cycle = AsyncMock(return_value={"facts": 3, "_co_mention": 2})
    stats = {}
    assert await handler._phase_graph_densification(stats) is True
    assert "comention_edges_deleted" not in stats and "comention_edges_reweighted" not in stats
