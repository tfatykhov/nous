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
DIM = 1536


def _settings(**over) -> Settings:
    return Settings(_env_file=None, **{"comention_linking_enabled": True, **over})


def _v2(**over) -> Settings:
    return _settings(comention_v2_enabled=True, **over)


def _densifier(db, s: Settings, agent: str) -> GraphDensifier:
    return GraphDensifier(db, GraphLinker(db, None, s, agent), None, s, agent)


def _axis(k: int) -> str:
    """The unit basis vector e_k (1-based) as a pgvector literal."""
    v = [0.0] * DIM
    v[k - 1] = 1.0
    return "[" + ",".join(str(x) for x in v) + "]"


async def _fact(
    db, agent: str, content: str, minute: int, *, active: bool = True,
    embedding: str | None = None,
) -> str:
    """Insert a fact learned ``minute`` minutes after T0 (larger = newer). No
    embedding unless given (a pgvector literal)."""
    fid = uuid4()
    async with db.session() as session:
        await session.execute(
            text(
                "INSERT INTO heart.facts (id, agent_id, content, active, learned_at, embedding) "
                "VALUES (:id, :a, :c, :act, :t, CAST(:e AS vector))"
            ),
            {"id": fid, "a": agent, "c": content, "act": active,
             "t": T0 + timedelta(minutes=minute), "e": embedding},
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
    ("posted by Build Bot Mon Sep 14", "build bot mon sep"),  # the month ends the phrase
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
    # The date rules need the month / time zone to END the phrase.
    ("the Rising Sun May Festival opened", "rising sun may festival"),
    ("the AM UTC Window shifted", "am utc window"),
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
    assert s.comention_v2_max_degree == 20
    assert s.comention_min_cosine == 0.35


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
    name it too. v1 sees df=2 and links the newest pair; v2 counts df=13 over the
    whole index, above its hub cap (pinned at 10 here)."""
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

    v2 = await _densifier(
        db, _v2(comention_max_facts_per_cycle=5, comention_v2_max_degree=10), agent,
    ).plan_comention_changes()
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
    dens = _densifier(db, _v2(comention_v2_max_degree=10), agent)

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
    dens = _densifier(
        db, _v2(comention_prune_max_per_cycle=bound, comention_v2_max_degree=10), agent,
    )

    plan = await dens.plan_comention_changes()
    assert (len(plan.deletes), len(plan.reweights)) == (n_deletes, n_reweights)

    await dens.build_comention_edges()
    after = await _cm_edges(db, agent)
    assert len(after) == 5 - n_deletes
    assert sum(1 for w in after.values() if w == pytest.approx(0.01)) == 1 - n_reweights


@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_weight_zero_is_not_a_link(db) -> None:
    """An entity named by every indexed fact (df == N) has rarity weight 0: it neither
    links nor keeps a co_mention edge."""
    agent = f"test-f106cm-w0-{uuid4().hex[:8]}"
    a = await _fact(db, agent, "Dara Velen leads Project Helios", 1)
    b = await _fact(db, agent, "Dara Velen studied at the Halvorsen Institute", 2)
    await _fact(db, agent, "Dara Velen moved to the coast", 3)
    eid = await _cm_edge(db, agent, a, b, BASE)

    plan = await _densifier(db, _v2(), agent).plan_comention_changes()
    assert plan.inserts == []
    assert plan.deletes == [eid] and plan.reweights == []


@pytest.mark.postgres_only
@pytest.mark.asyncio
@pytest.mark.parametrize(("named", "v2_links"), [(20, 190), (21, 0)])
async def test_v2_hub_cap_defaults_to_20(db, named: int, v2_links: int) -> None:
    """v2 counts hubs at comention_v2_max_degree (20): an entity in 20 facts links
    all 190 pairs, one in 21 is a hub. v1 keeps comention_max_degree (10)."""
    agent = f"test-f106cm-h20-{uuid4().hex[:8]}"
    for i in range(named):
        await _fact(db, agent, f"Orion Vale archive note {i}", i)
    await _fact(db, agent, "a plain note", 100)  # N > df, so the weight is above 0

    v2 = await _densifier(db, _v2(), agent).plan_comention_changes()
    assert len(v2.inserts) == v2_links
    v1 = await _densifier(db, _settings(), agent).plan_comention_changes()
    assert v1.inserts == []


@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_cosine_floor_on_new_links(db) -> None:
    """A new link needs cosine >= comention_min_cosine (0.35); a fact without an
    embedding passes; an existing edge is still re-weighted, whatever its cosine."""
    agent = f"test-f106cm-cos-{uuid4().hex[:8]}"
    lo1 = await _fact(db, agent, "Dara Velen leads Project Helios", 1, embedding=_axis(2))
    lo2 = await _fact(db, agent, "Dara Velen studied abroad", 2, embedding=_axis(3))
    hi1 = await _fact(db, agent, "the Lumen Array came online", 3, embedding=_axis(4))
    hi2 = await _fact(db, agent, "the Lumen Array went offline", 4, embedding=_axis(4))
    no1 = await _fact(db, agent, "Orion Vale shipped the release", 5)
    no2 = await _fact(db, agent, "Orion Vale fixed the build", 6, embedding=_axis(5))
    ex1 = await _fact(db, agent, "the Cedar Point lab opened", 7, embedding=_axis(6))
    ex2 = await _fact(db, agent, "the Cedar Point lab closed", 8, embedding=_axis(7))
    for i in range(4):
        await _fact(db, agent, f"a plain note {i}", 10 + i)
    ex_id = await _cm_edge(db, agent, ex1, ex2, 0.01)  # cosine 0, decayed

    plan = await _densifier(db, _v2(), agent).plan_comention_changes()
    assert _pairs(plan) == {_canon(hi1, hi2), _canon(no1, no2)}
    assert _canon(lo1, lo2) not in _pairs(plan)
    assert plan.deletes == []
    assert [eid for eid, _ in plan.reweights] == [ex_id]

    # Settings at 0 let the orthogonal pair through.
    loose = await _densifier(db, _v2(comention_min_cosine=0.0), agent).plan_comention_changes()
    assert _canon(lo1, lo2) in _pairs(loose)


@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_cosine_floor_rejects_before_the_cap_slot(db) -> None:
    """X has 19 co_mention edges, so one slot left. Its rarer pair (Alder Point, first
    by name at equal df) fails the floor and must not use that slot, so the passing
    pair (Birch Point) gets it."""
    agent = f"test-f106cm-slot-{uuid4().hex[:8]}"
    x = await _fact(db, agent, "Alder Point and Birch Point both report here", 100,
                    embedding=_axis(2))
    y = await _fact(db, agent, "the Alder Point office is new", 101, embedding=_axis(3))
    z = await _fact(db, agent, "the Birch Point office is old", 102, embedding=_axis(2))
    for i in range(19):
        partner = await _fact(db, agent, f"retired note {i}", i, active=False)
        await _cm_edge(db, agent, x, partner, BASE)

    plan = await _densifier(db, _v2(), agent).plan_comention_changes()
    assert _pairs(plan) == {_canon(x, z)}
    assert _canon(x, y) not in _pairs(plan)


@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_capped_pairs_skip_the_cosine_read(db) -> None:
    """X is at the lifetime cap (20 edges to retired facts). Its pairs can never link,
    so only Y-Z is sent to the cosine query, and only Y-Z is planned."""
    agent = f"test-f106cm-capcos-{uuid4().hex[:8]}"
    x = await _fact(db, agent, "Dara Velen leads Project Helios", 100)
    y = await _fact(db, agent, "Dara Velen studied at the Halvorsen Institute", 101)
    z = await _fact(db, agent, "Dara Velen moved to the coast", 102)
    await _fact(db, agent, "a plain note", 103)  # N > df, so the weight is above 0
    for i in range(20):
        partner = await _fact(db, agent, f"retired note {i}", i, active=False)
        await _cm_edge(db, agent, x, partner, BASE)
    dens = _densifier(db, _v2(), agent)
    sent: list[set[tuple[str, str]]] = []
    real = dens._fact_pair_cosines

    async def spy(pairs: set[tuple[str, str]]) -> dict[tuple[str, str], float]:
        sent.append(set(pairs))
        return await real(pairs)

    dens._fact_pair_cosines = spy
    plan = await dens.plan_comention_changes()
    assert sent == [{_canon(y, z)}]
    assert _pairs(plan) == {_canon(y, z)}


@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_reweights_down_to_the_rarity_weight(db) -> None:
    """A 0.9 edge whose only shared entity is named by 4 of the 10 indexed facts is
    lowered to that entity's rarity weight."""
    agent = f"test-f106cm-down-{uuid4().hex[:8]}"
    facts = [await _fact(db, agent, f"the Halvorsen Institute note {i}", i) for i in range(4)]
    for i in range(6):
        await _fact(db, agent, f"a plain note {i}", 10 + i)
    eid = await _cm_edge(db, agent, facts[0], facts[1], BASE)
    w4 = BASE * math.log(10 / 4) / math.log(10 / 2)
    dens = _densifier(db, _v2(), agent)

    plan = await dens.plan_comention_changes()
    assert plan.deletes == []
    assert [e for e, _ in plan.reweights] == [eid]
    assert plan.reweights[0][1] == pytest.approx(w4)
    assert w4 < BASE

    await dens.build_comention_edges()
    assert (await _cm_edges(db, agent))[_canon(facts[0], facts[1])] == pytest.approx(w4)


@pytest.mark.postgres_only
@pytest.mark.asyncio
@pytest.mark.parametrize("v2", [False, True])
async def test_insert_count_excludes_conflicts(db, v2: bool) -> None:
    """A planned pair that another writer linked first hits ON CONFLICT and is not
    counted (v1 and v2). Re-applying the same plan inserts and counts nothing."""
    agent = f"test-f106cm-cnt-{uuid4().hex[:8]}"
    a = await _fact(db, agent, "Dara Velen leads Project Helios", 1)
    b = await _fact(db, agent, "Dara Velen studied at the Halvorsen Institute", 2)
    c = await _fact(db, agent, "Dara Velen moved to the coast", 3)
    await _fact(db, agent, "a plain note", 4)  # N > df, so the v2 weight is above 0
    dens = _densifier(db, _v2() if v2 else _settings(), agent)

    plan = await dens.plan_comention_changes()
    assert _pairs(plan) == {_canon(a, b), _canon(a, c), _canon(b, c)}
    await _cm_edge(db, agent, a, b, BASE)  # a concurrent cycle got there first

    assert await dens.build_comention_edges(plan=plan) == 2
    assert set(await _cm_edges(db, agent)) == _pairs(plan)
    assert await dens.build_comention_edges(plan=plan) == 0


@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_v1_ignores_the_v2_settings(db) -> None:
    agent = f"test-f106cm-v1s-{uuid4().hex[:8]}"
    a = await _fact(db, agent, "Dara Velen leads Project Helios", 1, embedding=_axis(2))
    b = await _fact(db, agent, "Dara Velen studied abroad", 2, embedding=_axis(3))
    for i in range(11):
        await _fact(db, agent, f"Orion Vale archive note {i}", 10 + i)

    default = await _densifier(db, _settings(), agent).plan_comention_changes()
    tight = await _densifier(
        db, _settings(comention_v2_max_degree=2, comention_min_cosine=1.0), agent,
    ).plan_comention_changes()
    assert default == tight
    assert _pairs(default) == {_canon(a, b)}  # cosine 0 still links; 11 is a v1 hub


@pytest.mark.postgres_only
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("relation", "method"), [("contradicts", "heuristic"), ("related_to", "inferred")],
)
async def test_non_comention_edges_untouched_and_still_block(db, relation: str, method: str) -> None:
    """Maintenance covers only co_mention edges. Another edge between indexed facts
    sharing a hub is neither deleted (hub-only pair) nor re-weighted (pair that also
    shares a qualifying entity), and it still blocks a co_mention link."""
    agent = f"test-f106cm-nc-{uuid4().hex[:8]}"
    hub = [await _fact(db, agent, f"Orion Vale archive note {i}", i) for i in range(9)]
    p = await _fact(db, agent, "Orion Vale hired Dara Velen", 20)
    q = await _fact(db, agent, "Orion Vale fired Dara Velen", 21)
    async with db.session() as session:
        for a, b in [(hub[0], hub[1]), (p, q)]:
            s, t = _canon(a, b)
            await session.execute(
                text(
                    "INSERT INTO brain.graph_edges (agent_id, source_id, target_id, "
                    "source_type, target_type, relation, weight, auto_linked, "
                    "extraction_method) "
                    "VALUES (:a, :s, :t, 'fact', 'fact', :r, 0.5, true, :m)"
                ),
                {"a": agent, "s": s, "t": t, "r": relation, "m": method},
            )
        await session.commit()

    dens = _densifier(db, _v2(comention_v2_max_degree=10), agent)
    plan = await dens.plan_comention_changes()
    assert plan == ComentionPlan(shared_entities=1, facts_scanned=11)

    await dens.build_comention_edges()
    async with db.session() as session:
        rows = (await session.execute(
            text(
                "SELECT relation, extraction_method, weight FROM brain.graph_edges "
                "WHERE agent_id = :a"
            ),
            {"a": agent},
        )).all()
    assert sorted(rows) == [(relation, method, 0.5)] * 2


@pytest.mark.postgres_only
@pytest.mark.asyncio
@pytest.mark.parametrize(("interrupt_at_call", "loop"), [(1, "index"), (2, "edges")])
async def test_interrupt_during_the_scan_plans_nothing(
    db, monkeypatch, interrupt_at_call: int, loop: str,
) -> None:
    """The index and edge scans yield every 1000 rows (the first row included); an
    interrupt seen there gives an empty plan and the build writes nothing."""
    import asyncio as real_asyncio
    from types import SimpleNamespace

    from nous.brain import graph_densifier as gd

    agent = f"test-f106cm-int-{uuid4().hex[:8]}"
    await _fact(db, agent, "Dara Velen leads Project Helios", 1)
    await _fact(db, agent, "Dara Velen studied at the Halvorsen Institute", 2)
    g = await _fact(db, agent, "the Lumen Array came online", 3)
    h = await _fact(db, agent, "the Lumen Array went offline", 4)
    await _cm_edge(db, agent, g, h, 0.01)
    before = await _cm_edges(db, agent)
    dens = _densifier(db, _v2(), agent)

    calls = 0

    async def sleep(delay: float) -> None:
        nonlocal calls
        calls += 1
        if calls == interrupt_at_call:
            dens._interrupted = True
        await real_asyncio.sleep(delay)

    monkeypatch.setattr(gd, "asyncio", SimpleNamespace(sleep=sleep))
    assert await dens.build_comention_edges() == 0
    assert calls == interrupt_at_call, loop
    assert await _cm_edges(db, agent) == before


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


@pytest.mark.asyncio
async def test_sleep_handler_audits_maintenance_as_edge_prune() -> None:
    s = Settings(_env_file=None, graph_backfill_enabled=True)
    handler = SleepHandler(MagicMock(), MagicMock(), s, EventBus())
    densifier = MagicMock()
    densifier.discover_clusters = AsyncMock(return_value=0)
    handler._graph_densifier = densifier
    handler._auditor = MagicMock()

    densifier.run_backfill_cycle = AsyncMock(return_value={
        "facts": 0, "_co_mention": 1, "_co_mention_deleted": 5, "_co_mention_reweighted": 7,
    })
    assert await handler._phase_graph_densification({}) is True
    handler._auditor.record.assert_called_once_with(
        "graph_densify",
        "edge_prune",
        after={"counts": {"comention_edges_deleted": 5, "comention_edges_reweighted": 7}},
        rationale="F106 co_mention maintenance",
    )

    # Nothing to record: zero counts (v2) or no counts at all (v1).
    for result in (
        {"facts": 0, "_co_mention": 1, "_co_mention_deleted": 0, "_co_mention_reweighted": 0},
        {"facts": 0, "_co_mention": 1},
    ):
        handler._auditor = MagicMock()
        densifier.run_backfill_cycle = AsyncMock(return_value=result)
        assert await handler._phase_graph_densification({}) is True
        handler._auditor.record.assert_not_called()
