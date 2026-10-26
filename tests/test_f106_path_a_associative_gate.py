"""F106 Task 1: Path A's associative query gate and the associative fetch multiplier.

Associative edges (``co_mention``, ``co_occurred``: ``graph_constants.is_associative``)
reach Path A neighbours that are often unrelated to the query. With
``graph_associative_query_gate_enabled``, Stage 2b reads ``cos(neighbour, q)`` for
each neighbour such an edge reached, before the best-path comparison:

- ``raw`` mode (the default) drops the neighbour when that cosine is below
  ``graph_associative_query_gate_min_cosine`` and otherwise leaves it unscaled;
- ``seed_relative`` mode scales its score by F105's ``gate_factor`` and drops it
  below ``graph_associative_query_gate_drop_below``.

``graph_associative_edge_multiplier`` scales an associative edge's weight in
``Brain._neighbors``' fetch order and in ``_score_memory_neighbor``. Every default
reproduces today's behaviour.

The pipeline tests run ``run_recall_pipeline`` over a real ``Brain`` (real
``_neighbors``, real ``query_cosines``) on rows committed under a per-test agent
id, since the pipeline opens its own sessions. Fact recall is the double (a
mocked Heart returns the seeds); the query embedder is a stub returning the first
basis vector, so a fact embedded as ``c*e1 + sqrt(1 - c^2)*e_k`` has cosine ``c``
to the query.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from pydantic import ValidationError
from sqlalchemy import delete, text
from sqlalchemy.dialects import postgresql
from test_retrieval_pipeline import _make_heart, _make_settings  # noqa: E402  (test-local helper reuse)

from nous.api.retrieval_pipeline import _score_memory_neighbor, run_recall_pipeline
from nous.brain import spreading_activation
from nous.brain.brain import Brain
from nous.brain.graph_constants import associative_edge_sql, is_associative
from nous.brain.schemas import NeighborResult
from nous.config import Settings
from nous.heart.schemas import RecallResult
from nous.observability.retrieval_trace import Expansion, RetrievalTrace
from nous.storage.models import Fact, GraphEdge

DIM = 1536
AT = datetime(2026, 1, 5, tzinfo=UTC)
PATH_A_STAGE = "stage2b_heart_graph_memory"
GATE_ERR = "path_a_associative_gate"

# The six F106 settings at their Settings defaults (without the prefix).
F106_DEFAULTS = {
    "query_gate_enabled": False,
    "query_gate_mode": "raw",
    "query_gate_min_cosine": 0.31,
    "query_gate_seed_min": 0.19,
    "query_gate_drop_below": 0.0,
    "edge_multiplier": 1.0,
}
SEED_RELATIVE = {"query_gate_enabled": True, "query_gate_mode": "seed_relative"}
RAW = {"query_gate_enabled": True, "query_gate_mode": "raw"}


def _vec(cos_q: float, axis: int) -> str:
    """A unit 1536-dim pgvector literal with cosine ``cos_q`` to the query (e1);
    the rest of its length lies on basis vector ``axis`` (2 or more)."""
    v = [0.0] * DIM
    v[0] = cos_q
    v[axis - 1] = math.sqrt(max(0.0, 1.0 - cos_q * cos_q))
    return "[" + ",".join(f"{x:.6f}" for x in v) + "]"


class _Embedder:
    """The query embedder: always e1. ``error`` makes ``embed`` raise."""

    def __init__(self, error: Exception | None = None) -> None:
        self.calls = 0
        self.error = error

    async def embed(self, _text: str) -> list[float]:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return [1.0] + [0.0] * (DIM - 1)

    async def close(self) -> None:
        pass


# ---------------------------------------------------------------------------
# Fixtures: a real Brain on its own agent id, committed rows, cleanup
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def world(db):
    agent = f"f106-{uuid4().hex[:8]}"
    settings = Settings().model_copy(update={"agent_id": agent})
    w = SimpleNamespace(agent_id=agent, db=db, brain=Brain(database=db, settings=settings), axis=2)
    yield w
    w.brain.embeddings = None
    await w.brain.close()
    async with db.session() as s:
        for model in (GraphEdge, Fact):
            await s.execute(delete(model).where(model.agent_id == agent))
        await s.commit()


async def _fact(session, agent: str, cos_q: float | None, axis: int, content: str = "f106 fact") -> UUID:
    """An active fact; ``cos_q=None`` leaves its embedding NULL."""
    fid = uuid4()
    await session.execute(
        text("INSERT INTO heart.facts (id, agent_id, content, active) VALUES (:id, :a, :c, true)"),
        {"id": fid, "a": agent, "c": f"{content} {fid}"},
    )
    if cos_q is not None:
        await session.execute(
            text("UPDATE heart.facts SET embedding = CAST(:v AS vector) WHERE id = :id"),
            {"v": _vec(cos_q, axis), "id": fid},
        )
    return fid


async def _edge(
    session,
    agent: str,
    src: UUID,
    tgt: UUID,
    weight: float,
    relation: str = "related_to",
    method: str = "inferred",
) -> None:
    await session.execute(
        text(
            "INSERT INTO brain.graph_edges (source_id, target_id, source_type, target_type, agent_id, "
            "relation, weight, auto_linked, extraction_method) "
            "VALUES (:s, :t, 'fact', 'fact', :a, :r, :w, true, :m)"
        ),
        {"s": src, "t": tgt, "a": agent, "r": relation, "w": weight, "m": method},
    )


class _Facts:
    """Commits facts and edges for ``world``'s agent; each fact gets its own axis."""

    def __init__(self, world) -> None:
        self.world = world

    async def fact(self, cos_q: float | None, content: str = "f106 fact") -> UUID:
        self.world.axis += 1
        async with self.world.db.session() as s:
            fid = await _fact(s, self.world.agent_id, cos_q, self.world.axis, content)
            await s.commit()
        return fid

    async def edge(
        self, src: UUID, tgt: UUID, weight: float, relation: str = "related_to", method: str = "inferred"
    ) -> None:
        async with self.world.db.session() as s:
            await _edge(s, self.world.agent_id, src, tgt, weight, relation, method)
            await s.commit()


async def _recall(
    world,
    seeds: list[tuple[UUID, float]],
    f106: dict[str, Any] | None = None,
    *,
    embedder: _Embedder | None = None,
    seed_score_on: bool = True,
) -> tuple[list, Any, RetrievalTrace, _Embedder]:
    """Run the pipeline over the real Brain. ``f106`` holds the F106 settings
    (without the ``graph_associative_`` prefix); None: the settings lack them."""
    settings = _make_settings(heart_graph_all_types_enabled=True)
    settings.graph_neighbor_seed_score_enabled = seed_score_on
    settings.graph_inferred_edge_penalty = 1.0
    for key, value in (f106 or {}).items():
        setattr(settings, f"graph_associative_{key}", value)
    embedder = embedder or _Embedder()
    world.brain.embeddings = embedder
    heart = _make_heart(
        recall_results=[RecallResult(type="fact", id=sid, summary=f"seed {sid}", score=score) for sid, score in seeds]
    )
    tr = RetrievalTrace(query="q")
    results, stats = await run_recall_pipeline(
        query="q",
        heart=heart,
        brain=world.brain,
        settings=settings,
        limit=10,
        memory_types=["fact"],
        trace=tr,
    )
    return results, stats, tr, embedder


def _path_a(tr: RetrievalTrace) -> list[tuple[str, float, int]]:
    """Path A's candidates as (id, score, rank), in entry order."""
    return [
        (c["id"], c["entry_score"], c["entry_rank"])
        for c in tr.to_dict()["candidates"]
        if c["entry_leg"] == "heart_graph_memory"
    ]


def _scores(tr: RetrievalTrace) -> dict[str, float]:
    return {cid: score for cid, score, _rank in _path_a(tr)}


def _expansions(tr: RetrievalTrace) -> list[dict]:
    return [e for e in tr.to_dict()["expansions"] if e["stage"] == PATH_A_STAGE]


def _expansion(tr: RetrievalTrace, nid: UUID, seed: UUID | None = None) -> dict:
    rows = [e for e in _expansions(tr) if e["neighbor_id"] == str(nid) and (seed is None or e["seed_id"] == str(seed))]
    assert len(rows) == 1, rows
    return rows[0]


async def _seed_with_two_neighbours(
    world, *, cos_m: float | None = 0.2, relation: str = "related_to", method: str = "co_mention"
) -> tuple[UUID, UUID, UUID]:
    """Seed S (cos 0.8 to the query) with an associative neighbour M (weight 0.9,
    cos ``cos_m``) and a cosine-edge neighbour C (related_to / inferred, 0.6)."""
    rows = _Facts(world)
    s = await rows.fact(0.8, "seed")
    m = await rows.fact(cos_m, "associative")
    c = await rows.fact(0.5, "cosine")
    await rows.edge(s, m, 0.9, relation, method)
    await rows.edge(s, c, 0.6)
    return s, m, c


# ---------------------------------------------------------------------------
# Settings, is_associative, the trace fields
# ---------------------------------------------------------------------------


def test_settings_defaults_and_bounds():
    s = Settings(_env_file=None)
    assert {k: getattr(s, f"graph_associative_{k}") for k in F106_DEFAULTS} == F106_DEFAULTS
    for good in (
        {"graph_associative_query_gate_mode": "seed_relative"},
        {"graph_associative_query_gate_min_cosine": 0.0},
        {"graph_associative_query_gate_min_cosine": 1.0},
        {"graph_associative_query_gate_drop_below": 1.0},
        {"graph_associative_edge_multiplier": 0.5},
    ):
        Settings(_env_file=None, **good)
    for bad in (
        {"graph_associative_query_gate_mode": "quantile"},
        {"graph_associative_query_gate_min_cosine": -0.1},
        {"graph_associative_query_gate_min_cosine": 1.1},
        {"graph_associative_query_gate_seed_min": 0.0},
        {"graph_associative_query_gate_seed_min": 1.1},
        {"graph_associative_query_gate_drop_below": -0.1},
        {"graph_associative_query_gate_drop_below": 1.1},
        {"graph_associative_edge_multiplier": 0.0},
        {"graph_associative_edge_multiplier": 1.5},
    ):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, **bad)


def test_settings_read_from_env(monkeypatch):
    monkeypatch.setenv("NOUS_GRAPH_ASSOCIATIVE_QUERY_GATE_ENABLED", "true")
    monkeypatch.setenv("NOUS_GRAPH_ASSOCIATIVE_QUERY_GATE_MODE", "seed_relative")
    monkeypatch.setenv("NOUS_GRAPH_ASSOCIATIVE_QUERY_GATE_MIN_COSINE", "0.4")
    monkeypatch.setenv("NOUS_GRAPH_ASSOCIATIVE_QUERY_GATE_SEED_MIN", "0.2")
    monkeypatch.setenv("NOUS_GRAPH_ASSOCIATIVE_QUERY_GATE_DROP_BELOW", "0.3")
    monkeypatch.setenv("NOUS_GRAPH_ASSOCIATIVE_EDGE_MULTIPLIER", "0.5")
    s = Settings(_env_file=None)
    assert (
        s.graph_associative_query_gate_enabled,
        s.graph_associative_query_gate_mode,
        s.graph_associative_query_gate_min_cosine,
        s.graph_associative_query_gate_seed_min,
        s.graph_associative_query_gate_drop_below,
        s.graph_associative_edge_multiplier,
    ) == (True, "seed_relative", 0.4, 0.2, 0.3, 0.5)


_EDGE_KINDS = [
    (rel, method)
    for rel in ("co_occurred", "related_to", "evidence_for", "supersedes", None)
    for method in ("co_mention", "inferred", "heuristic", "co_occurrence", "deterministic", None)
]


def test_is_associative():
    assert is_associative("co_occurred", "co_occurrence")
    assert is_associative("related_to", "co_mention")
    assert is_associative(None, "co_mention")
    assert is_associative("co_occurred", None)
    assert not is_associative("related_to", "inferred")
    assert not is_associative("co_mention", "inferred"), "co_mention is a method, not a relation"
    assert not is_associative(None, None)


@pytest.mark.postgres_only
async def test_is_associative_matches_the_sql_fragment(session):
    """The Python predicate agrees with ``associative_edge_sql`` on every
    combination, NULLs included (a NULL result counts as false)."""
    values = ", ".join(
        f"({'NULL' if r is None else repr(r)}::text, {'NULL' if m is None else repr(m)}::text)" for r, m in _EDGE_KINDS
    )
    rows = (
        await session.execute(
            text(
                f"SELECT relation, extraction_method, COALESCE({associative_edge_sql()}, false) AS assoc "
                f"FROM (VALUES {values}) AS t(relation, extraction_method)"
            )
        )
    ).all()
    assert len(rows) == len(_EDGE_KINDS)
    for row in rows:
        assert row.assoc is is_associative(row.relation, row.extraction_method), row


def test_expansion_serializes_the_gate_fields_only_when_set():
    base = {"seed_id": "s", "seed_type": "fact", "neighbor_id": "n", "neighbor_type": "fact", "stage": "x"}
    plain = Expansion(**base).to_dict()
    assert "query_gate_factor" not in plain and "query_cosine" not in plain
    gated = Expansion(**base, query_gate_factor=0.0, query_cosine=0.25).to_dict()
    assert (gated["query_gate_factor"], gated["query_cosine"]) == (0.0, 0.25)


# ---------------------------------------------------------------------------
# _score_memory_neighbor: the factor and the multiplier, both branches
# ---------------------------------------------------------------------------


def _nr(
    relation: str = "related_to", method: str = "co_mention", *, weight: float = 0.9, factor: float | None = None
) -> NeighborResult:
    return NeighborResult(
        id=uuid4(),
        node_type="fact",
        description="n",
        edge_relation=relation,
        edge_weight=weight,
        created_at=AT,
        extraction_method=method,
        seed_score=0.8,
        query_gate_factor=factor,
    )


@pytest.mark.parametrize("seed_score_on", [True, False])
def test_score_memory_neighbor_applies_multiplier(seed_score_on):
    def score(n: NeighborResult, mult: float | None) -> float:
        s = SimpleNamespace(
            graph_neighbor_seed_score_enabled=seed_score_on,
            graph_inferred_edge_penalty=0.8,
            graph_recall_decay=0.7,
        )
        if mult is not None:
            s.graph_associative_edge_multiplier = mult
        return _score_memory_neighbor(n, s)  # type: ignore[arg-type]

    co_mention, co_occurred, cosine = _nr(), _nr("co_occurred", "co_occurrence"), _nr(method="inferred", weight=0.6)
    for n in (co_mention, co_occurred):
        assert score(n, 0.5) == pytest.approx(score(n, 1.0) * 0.5)
        assert score(n, 1.0) == score(n, None), "1.0 and an absent setting agree"
    assert score(cosine, 0.5) == score(cosine, 1.0) == score(cosine, None), "not associative: unchanged"
    expected = 0.8 * 0.9 if seed_score_on else 0.9 * 0.7
    assert score(co_mention, None) == pytest.approx(expected)
    # The gate factor multiplies in both branches; 0.0 is a value, not "unset".
    assert score(_nr(factor=0.25), 1.0) == pytest.approx(expected * 0.25)
    assert score(_nr(factor=0.0), 1.0) == 0.0
    assert score(_nr(factor=0.25), 0.5) == pytest.approx(expected * 0.25 * 0.5)


# ---------------------------------------------------------------------------
# Brain._neighbors: the fetch order
# ---------------------------------------------------------------------------


def _brain(db, agent: str, mult: float | None) -> Brain:
    settings = Settings().model_copy(update={"agent_id": agent})
    brain = Brain(database=db, settings=settings)
    if mult is not None:
        brain.settings = settings.model_copy(update={"graph_associative_edge_multiplier": mult})
    return brain


@pytest.mark.postgres_only
async def test_multiplier_orders_fetch(db, session):
    """At 1.0 the 0.9 co_mention edge fills a one-row window; at 0.5 (0.45 < 0.6)
    the cosine edge does. The returned edge_weight stays raw. co_occurred is
    associative too."""
    agent = f"f106-{uuid4().hex[:8]}"
    seed = await _fact(session, agent, None, 2)
    m = await _fact(session, agent, None, 3)
    c = await _fact(session, agent, None, 4)
    o = await _fact(session, agent, None, 5)
    await _edge(session, agent, seed, m, 0.9, method="co_mention")
    await _edge(session, agent, seed, c, 0.6)
    await _edge(session, agent, o, seed, 0.8, relation="co_occurred", method="co_occurrence")
    await session.flush()

    async def fetch(mult: float | None, limit: int) -> list[tuple[UUID, float]]:
        out = await _brain(db, agent, mult).neighbors(
            seed, node_type="fact", limit=limit, session=session, neighbor_type="fact"
        )
        return [(n.id, n.edge_weight) for n in out]

    assert await fetch(1.0, 1) == [(m, 0.9)]
    assert await fetch(0.5, 1) == [(c, 0.6)]
    assert await fetch(None, 3) == await fetch(1.0, 3) == [(m, 0.9), (o, 0.8), (c, 0.6)]
    assert await fetch(0.5, 3) == [(c, 0.6), (m, 0.9), (o, 0.8)], "raw weights, multiplied order"


@pytest.mark.postgres_only
async def test_multiplier_picks_the_edge_per_neighbour(db, session):
    """A neighbour linked by both a co_mention edge (0.9) and a cosine edge (0.6)
    is represented by the co_mention one at 1.0 and by the cosine one at 0.5:
    the per-neighbour window uses the multiplied weight too."""
    agent = f"f106-{uuid4().hex[:8]}"
    seed = await _fact(session, agent, None, 2)
    y = await _fact(session, agent, None, 3)
    await _edge(session, agent, seed, y, 0.9, method="co_mention")
    await _edge(session, agent, y, seed, 0.6)
    await session.flush()

    async def edge_of_y(mult: float) -> tuple[str, float]:
        (n,) = await _brain(db, agent, mult).neighbors(
            seed, node_type="fact", limit=1, session=session, neighbor_type="fact"
        )
        assert n.id == y
        return n.extraction_method, n.edge_weight

    assert await edge_of_y(1.0) == ("co_mention", 0.9)
    assert await edge_of_y(0.5) == ("inferred", 0.6)


class _Statements:
    """Fake session: records each statement and returns no rows."""

    def __init__(self) -> None:
        self.statements: list[Any] = []

    async def execute(self, stmt: Any, params: Any = None) -> Any:
        self.statements.append(stmt)
        return SimpleNamespace(all=lambda: [])


async def _neighbors_sql(db, settings: Any) -> tuple[str, dict]:
    brain = Brain(database=db, settings=Settings())
    brain.settings = settings
    rec = _Statements()
    seed = UUID("00000000-0000-0000-0000-0000000000b1")
    assert await brain.neighbors(seed, node_type="fact", limit=3, session=rec, neighbor_type="fact") == []  # type: ignore[arg-type]
    (stmt,) = rec.statements
    compiled = stmt.compile(dialect=postgresql.dialect())
    return str(compiled), compiled.params


async def test_multiplier_one_sql_unchanged(db):
    """At 1.0 ``_neighbors`` sends exactly the SQL it sends without the setting:
    no CASE term is added."""
    base = Settings()
    absent = SimpleNamespace(**{k: v for k, v in base.model_dump().items() if k != "graph_associative_edge_multiplier"})
    at_one = base.model_copy(update={"graph_associative_edge_multiplier": 1.0})
    at_half = base.model_copy(update={"graph_associative_edge_multiplier": 0.5})
    sql_absent, params_absent = await _neighbors_sql(db, absent)
    sql_one, params_one = await _neighbors_sql(db, at_one)
    assert (sql_one, params_one) == (sql_absent, params_absent)
    assert not {"co_mention", "co_occurred"} & set(map(str, params_one.values())), "no associative term at 1.0"
    sql_half, params_half = await _neighbors_sql(db, at_half)
    assert sql_half != sql_one, "non-vacuous: 0.5 adds the CASE term"
    assert sql_half.count("CASE WHEN") == sql_one.count("CASE WHEN") + 2, "both orderings"
    assert "co_mention" in params_half.values() and "co_occurred" in params_half.values()


# ---------------------------------------------------------------------------
# Stage 2b: flags off
# ---------------------------------------------------------------------------


@pytest.mark.postgres_only
@pytest.mark.parametrize("seed_score_on", [True, False])
async def test_flags_off_path_a_identical(world, seed_score_on):
    """All six settings at their defaults: the same Path A candidates, scores,
    order and expansions as settings without them, and the hand-computed
    values of today's formula. The embedder is never called."""
    s, m, c = await _seed_with_two_neighbours(world)
    runs = [await _recall(world, [(s, 0.8)], f106, seed_score_on=seed_score_on) for f106 in (None, F106_DEFAULTS)]
    (_, _, absent, emb_absent), (_, stats, defaults, emb_defaults) = runs
    assert _path_a(defaults) == _path_a(absent)
    assert _expansions(defaults) == _expansions(absent)
    assert all("query_gate_factor" not in e and "query_cosine" not in e for e in _expansions(defaults))
    assert emb_absent.calls == emb_defaults.calls == 0
    assert GATE_ERR not in stats.n_stage_errors
    if seed_score_on:
        expected = [(str(m), 0.8 * 0.9, 1), (str(c), 0.8 * 0.6, 2)]
    else:
        expected = [(str(m), 0.9 * 0.7, 1), (str(c), 0.6 * 0.7, 2)]
    assert [(i, pytest.approx(sc), r) for i, sc, r in expected] == _path_a(defaults)


# ---------------------------------------------------------------------------
# Stage 2b: seed_relative mode
# ---------------------------------------------------------------------------


@pytest.mark.postgres_only
@pytest.mark.parametrize(
    ("relation", "method"),
    [("related_to", "co_mention"), ("co_occurred", "co_occurrence")],
    ids=["co_mention", "co_occurred"],
)
async def test_gate_scales_associative_neighbour(world, relation, method):
    """cos(seed, q) = 0.8 and cos(M, q) = 0.2: M's score is the flag-off score
    x 0.25; the cosine neighbour's is unchanged. Covers co_occurred too."""
    s, m, c = await _seed_with_two_neighbours(world, relation=relation, method=method)
    _, _, off, _ = await _recall(world, [(s, 0.8)])
    _, stats, on, embedder = await _recall(world, [(s, 0.8)], SEED_RELATIVE)
    assert _scores(on)[str(m)] == pytest.approx(_scores(off)[str(m)] * 0.25)
    assert _scores(on)[str(c)] == _scores(off)[str(c)]
    exp = _expansion(on, m)
    assert exp["query_gate_factor"] == pytest.approx(0.25)
    assert exp["query_cosine"] == pytest.approx(0.2, abs=1e-5)
    assert "query_gate_factor" not in _expansion(on, c), "a cosine-edge neighbour is not gated"
    assert embedder.calls == 1
    assert GATE_ERR not in stats.n_stage_errors


@pytest.mark.postgres_only
async def test_gate_covers_co_occurred(world):
    """The brief's separate case, kept explicit: a co_occurred edge is gated."""
    s, m, _c = await _seed_with_two_neighbours(world, relation="co_occurred", method="co_occurrence")
    _, _, on, _ = await _recall(world, [(s, 0.8)], SEED_RELATIVE)
    assert _expansion(on, m)["query_gate_factor"] == pytest.approx(0.25)


@pytest.mark.postgres_only
async def test_drop_below_excludes_and_traces(world):
    """drop_below 0.5, factor 0.25: M is not a Path A result, and its expansion
    is still traced with its factor."""
    s, m, c = await _seed_with_two_neighbours(world)
    _, _, on, _ = await _recall(world, [(s, 0.8)], {**SEED_RELATIVE, "query_gate_drop_below": 0.5})
    assert str(m) not in _scores(on)
    assert str(c) in _scores(on)
    assert _expansion(on, m)["query_gate_factor"] == pytest.approx(0.25)


async def _two_seeds_one_target(world, *, cos_x: float, w_assoc: float = 0.9, w_cos: float = 0.6):
    """Seed A (cos 0.8) reaches X by co_mention; seed B by a cosine edge."""
    rows = _Facts(world)
    a = await rows.fact(0.8, "seed a")
    b = await rows.fact(0.3, "seed b")
    x = await rows.fact(cos_x, "target")
    await rows.edge(a, x, w_assoc, method="co_mention")
    await rows.edge(b, x, w_cos)
    return a, b, x


def _first_seed(tr: RetrievalTrace) -> str:
    return _expansions(tr)[0]["seed_id"]


@pytest.mark.postgres_only
@pytest.mark.parametrize("a_first", [True, False])
async def test_dedup_uses_gated_score(world, a_first):
    """X is reached from A by co_mention (gated to factor 0.1) and from B by a
    cosine edge. Ungated, the co_mention path wins (0.72 > 0.42); gated, the
    cosine path does (0.072 < 0.42), in either seed order. Its score is exactly
    the cosine path's, so the replaced path's factor did not survive."""
    a, b, x = await _two_seeds_one_target(world, cos_x=0.08)
    seeds = [(a, 0.8), (b, 0.7)] if a_first else [(b, 0.7), (a, 0.8)]
    _, _, off, _ = await _recall(world, seeds)
    _, _, on, _ = await _recall(world, seeds, SEED_RELATIVE)
    assert _first_seed(on) == str(a if a_first else b), "the seed order really differs"
    assert _scores(off)[str(x)] == pytest.approx(0.8 * 0.9), "non-vacuous: ungated, co_mention wins"
    assert _scores(on)[str(x)] == pytest.approx(0.7 * 0.6), "gated, the cosine path is stored"
    assert _expansion(on, x, a)["query_gate_factor"] == pytest.approx(0.1)


@pytest.mark.postgres_only
@pytest.mark.parametrize("a_first", [True, False])
async def test_dropped_path_never_replaces(world, a_first):
    """Gated but kept, A's co_mention path (0.9 x 0.9 x 0.25 = 0.2025) beats B's
    cosine path (0.3 x 0.6 = 0.18). With drop_below 0.5 it is dropped, so the
    cosine path stays stored, in either seed order."""
    a, b, x = await _two_seeds_one_target(world, cos_x=0.2)
    seeds = [(a, 0.9), (b, 0.3)] if a_first else [(b, 0.3), (a, 0.9)]
    _, _, kept, _ = await _recall(world, seeds, SEED_RELATIVE)
    _, _, dropped, _ = await _recall(world, seeds, {**SEED_RELATIVE, "query_gate_drop_below": 0.5})
    assert _first_seed(dropped) == str(a if a_first else b)
    assert _scores(kept)[str(x)] == pytest.approx(0.9 * 0.9 * 0.25), "non-vacuous: kept, it wins"
    assert _scores(dropped)[str(x)] == pytest.approx(0.3 * 0.6)
    assert _expansion(dropped, x, a)["query_gate_factor"] == pytest.approx(0.25)


# ---------------------------------------------------------------------------
# Stage 2b: raw mode (the default mode)
# ---------------------------------------------------------------------------


@pytest.mark.postgres_only
async def test_raw_mode_drops_below_min_cosine_and_keeps_the_rest_unscaled(world):
    """min_cosine 0.31: a co_mention neighbour at cos 0.25 is dropped and traced
    with factor 0.0 and its cosine; one at cos 0.40 is kept, score unchanged."""
    rows = _Facts(world)
    s = await rows.fact(0.8, "seed")
    low = await rows.fact(0.25, "low")
    high = await rows.fact(0.40, "high")
    await rows.edge(s, low, 0.9, method="co_mention")
    await rows.edge(s, high, 0.85, method="co_mention")
    _, _, off, _ = await _recall(world, [(s, 0.8)])
    _, stats, on, _ = await _recall(world, [(s, 0.8)], RAW)
    assert str(low) not in _scores(on)
    assert _scores(on)[str(high)] == _scores(off)[str(high)]
    e_low, e_high = _expansion(on, low), _expansion(on, high)
    assert (e_low["query_gate_factor"], e_low["query_cosine"]) == (0.0, pytest.approx(0.25, abs=1e-5))
    assert (e_high["query_gate_factor"], e_high["query_cosine"]) == (1.0, pytest.approx(0.40, abs=1e-5))
    assert GATE_ERR not in stats.n_stage_errors


@pytest.mark.postgres_only
async def test_raw_mode_is_the_default_mode(world):
    """Gate on with no mode set: raw (a cos 0.2 neighbour is dropped, not scaled)."""
    s, m, c = await _seed_with_two_neighbours(world)
    _, _, on, _ = await _recall(world, [(s, 0.8)], {"query_gate_enabled": True})
    assert str(m) not in _scores(on) and str(c) in _scores(on)
    assert _expansion(on, m)["query_gate_factor"] == 0.0


@pytest.mark.postgres_only
async def test_raw_mode_reads_no_seed_cosine(world):
    """raw reads only the associative neighbours' cosines; seed_relative also
    reads the seed's."""
    s, m, _c = await _seed_with_two_neighbours(world, cos_m=0.5)
    calls: dict[str, list] = {}
    real = spreading_activation.query_cosines
    for mode, f106 in (("raw", RAW), ("seed_relative", SEED_RELATIVE)):
        spy = AsyncMock(side_effect=real)
        with patch("nous.brain.spreading_activation.query_cosines", spy):
            await _recall(world, [(s, 0.8)], f106)
        calls[mode] = [sorted(call.args[2]) for call in spy.await_args_list]
    assert calls["raw"] == [[(m, "fact")]]
    assert calls["seed_relative"] == [sorted([(m, "fact"), (s, "fact")])]


# ---------------------------------------------------------------------------
# Stage 2b: neutral cases, failure and cost (both modes)
# ---------------------------------------------------------------------------


@pytest.mark.postgres_only
@pytest.mark.parametrize("mode", [RAW, SEED_RELATIVE], ids=["raw", "seed_relative"])
async def test_missing_embedding_is_neutral(world, mode):
    """M has no embedding: factor 1.0, kept, score unchanged; no cosine traced."""
    s, m, _c = await _seed_with_two_neighbours(world, cos_m=None)
    _, _, off, _ = await _recall(world, [(s, 0.8)])
    _, _, on, _ = await _recall(world, [(s, 0.8)], {**mode, "query_gate_drop_below": 0.5})
    assert _scores(on)[str(m)] == _scores(off)[str(m)]
    exp = _expansion(on, m)
    assert exp["query_gate_factor"] == 1.0
    assert "query_cosine" not in exp


@pytest.mark.postgres_only
@pytest.mark.parametrize("mode", [RAW, SEED_RELATIVE], ids=["raw", "seed_relative"])
async def test_gate_fails_open(world, mode):
    """The embedder raises. Two seeds each hold an associative neighbour, so two
    batches would be gated: the embedder is called once, the error is counted
    once, and Path A returns the ungated scores."""
    s, m, c = await _seed_with_two_neighbours(world)
    rows = _Facts(world)
    s2 = await rows.fact(0.7, "seed two")
    m2 = await rows.fact(0.1, "associative two")
    await rows.edge(s2, m2, 0.9, method="co_mention")
    seeds = [(s, 0.8), (s2, 0.7)]
    _, _, off, _ = await _recall(world, seeds)
    _, stats, on, embedder = await _recall(world, seeds, mode, embedder=_Embedder(RuntimeError("down")))
    assert _path_a(on) == _path_a(off)
    assert {str(m), str(c), str(m2)} <= set(_scores(on))
    assert stats.n_stage_errors[GATE_ERR] == 1
    assert embedder.calls == 1, "a failed embed is not retried"
    assert all("query_gate_factor" not in e for e in _expansions(on))


@pytest.mark.postgres_only
async def test_gate_sql_failure_fails_open(world):
    """A failing cosine read leaves the batch ungated and is counted."""
    s, m, _c = await _seed_with_two_neighbours(world)
    _, _, off, _ = await _recall(world, [(s, 0.8)])
    with patch("nous.brain.spreading_activation.query_cosines", AsyncMock(side_effect=RuntimeError("pgvector"))):
        _, stats, on, _ = await _recall(world, [(s, 0.8)], RAW)
    assert _path_a(on) == _path_a(off)
    assert stats.n_stage_errors[GATE_ERR] == 1
    assert "query_gate_factor" not in _expansion(on, m)


@pytest.mark.postgres_only
async def test_gate_cost_is_bounded(world):
    """The query is embedded once per recall and the cosines are read only for
    batches holding an associative neighbour: seed one has one, seed two only a
    cosine-edge neighbour. With no associative neighbour at all, nothing runs."""
    s, _m, _c = await _seed_with_two_neighbours(world, cos_m=0.5)
    rows = _Facts(world)
    s2 = await rows.fact(0.7, "seed two")
    c2 = await rows.fact(0.5, "cosine two")
    await rows.edge(s2, c2, 0.6)
    spy = AsyncMock(side_effect=spreading_activation.query_cosines)
    with patch("nous.brain.spreading_activation.query_cosines", spy):
        _, _, _, embedder = await _recall(world, [(s, 0.8), (s2, 0.7)], RAW)
        assert (embedder.calls, spy.await_count) == (1, 1)
        _, _, _, embedder = await _recall(world, [(s2, 0.7)], RAW)
        assert (embedder.calls, spy.await_count) == (0, 1)
