"""F091 reporting for the graph-expansion mechanisms.

Spreading activation and the 1-hop fallback both append to one list whose
candidates are counted under the ``graph_expanded`` leg. The mechanism legs
(``spreading_activation`` / ``brain_graph``) used to carry no count, which
``Leg`` stored as 0 and the dashboard showed as "ran, found nothing" on 373 of
376 prod retrievals that used spreading (week of 2026-10-25), while the same
retrieval's ``graph_expanded`` leg held all of spreading's rows.

And every spreading expansion was recorded under the first seed with
``seed_type="multi"``, so the dashboard rooted all of them under that seed.
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest
import pytest_asyncio
from test_retrieval_pipeline import (  # noqa: E402  (test-local helper reuse)
    DECISION_ONE_ID,
    DECISION_TWO_ID,
    _make_brain,
    _make_decision_summaries,
    _make_heart,
    _make_settings,
)

from nous.api.retrieval_pipeline import run_recall_pipeline
from nous.brain.brain import Brain
from nous.brain.schemas import NeighborResult, ReasonInput, RecordInput
from nous.brain.spreading_activation import spreading_activation_search
from nous.config import Settings
from nous.observability.retrieval_trace import Leg, RetrievalTrace

SPREAD_A = UUID("aaaaaaaa-0000-0000-0000-000000000001")
SPREAD_B = UUID("aaaaaaaa-0000-0000-0000-000000000002")
HOP_NEIGHBOUR = UUID("bbbbbbbb-0000-0000-0000-000000000001")
AT = datetime(2026, 1, 5, tzinfo=UTC)


def _legs(tr: RetrievalTrace) -> dict[str, dict]:
    return {leg["name"]: leg for leg in tr.to_dict()["legs"]}


async def _run(*, spreading: str, activated=None, resolved=None,
               neighbors_by_node=None, seeds_out=None):
    """Run the pipeline with a real trace. ``seeds_out`` maps a node to the
    seed the mocked CTE reports for its winning path (None = report none)."""
    heart = _make_heart(recall_results=[])
    brain = _make_brain(
        neighbors_by_node=neighbors_by_node or {},
        contradictions=[],
        decision_results=_make_decision_summaries(),
    )
    brain._resolve_node_descriptions = AsyncMock(return_value=resolved or {})

    async def fake_search(*_a, seed_out=None, **_k):
        if seed_out is not None and seeds_out:
            seed_out.update(seeds_out)
        return activated or []

    tr = RetrievalTrace(query="q")
    with patch(
        "nous.brain.spreading_activation.spreading_activation_search",
        AsyncMock(side_effect=fake_search),
    ), patch(
        "nous.brain.spreading_activation.compute_graph_density",
        AsyncMock(return_value=5.0),
    ):
        await run_recall_pipeline(
            query="q", heart=heart, brain=brain,
            settings=_make_settings(spreading_activation_enabled=spreading),
            limit=10, trace=tr,
        )
    return tr


class TestLegSerialization:
    def test_counted_in_is_omitted_when_unset(self):
        """Every leg but the two mechanisms, and every row written before this
        field, must serialize exactly as before."""
        assert "counted_in" not in Leg(name="heart_primary", n_returned=3).to_dict()

    def test_counted_in_is_serialized_when_set(self):
        d = Leg(name="spreading_activation", n_returned=15,
                counted_in="graph_expanded").to_dict()
        assert d["counted_in"] == "graph_expanded"
        assert d["n_returned"] == 15

    def test_a_later_call_without_it_leaves_it_alone(self):
        tr = RetrievalTrace(query="q")
        tr.leg("spreading_activation", n_returned=4, counted_in="graph_expanded")
        tr.leg("spreading_activation", error="1 error(s)")
        assert _legs(tr)["spreading_activation"]["counted_in"] == "graph_expanded"


class TestMechanismYield:
    @pytest.mark.asyncio
    async def test_spreading_reports_the_rows_it_appended(self):
        """The bug: this leg read 0, "ran, found nothing", while graph_expanded
        held every one of its rows."""
        tr = await _run(
            spreading="true",
            activated=[(SPREAD_A, "fact", 0.5, 1), (SPREAD_B, "fact", 0.3, 2)],
            resolved={SPREAD_A: ("a", AT), SPREAD_B: ("b", AT)},
        )
        legs = _legs(tr)
        assert legs["graph_expanded"]["n_returned"] == 2
        assert legs["spreading_activation"]["n_returned"] == 2
        assert legs["spreading_activation"]["counted_in"] == "graph_expanded"
        assert "brain_graph" not in legs, "spreading replaced the 1-hop leg"

    @pytest.mark.asyncio
    async def test_spreading_that_resolved_nothing_reports_a_true_zero(self):
        """It ran, every hit was unresolvable, and the 1-hop fallback took over.
        Now the 0 means what the dashboard says it means, and the fallback
        owns the rows."""
        tr = await _run(
            spreading="true",
            activated=[(SPREAD_A, "fact", 0.5, 1)],
            resolved={},  # nothing resolves -> fallback
            neighbors_by_node={DECISION_ONE_ID: [NeighborResult(
                id=HOP_NEIGHBOUR, node_type="fact", description="one hop",
                edge_relation="related_to", edge_weight=0.8, created_at=AT,
            )]},
        )
        legs = _legs(tr)
        assert legs["spreading_activation"]["n_returned"] == 0
        assert legs["spreading_activation"]["counted_in"] == "graph_expanded"
        assert legs["brain_graph"]["n_returned"] == legs["graph_expanded"]["n_returned"]
        assert legs["brain_graph"]["n_returned"] >= 1
        assert legs["brain_graph"]["counted_in"] == "graph_expanded"

    @pytest.mark.asyncio
    async def test_one_hop_alone_reports_its_yield(self):
        tr = await _run(
            spreading="false",
            neighbors_by_node={DECISION_ONE_ID: [NeighborResult(
                id=HOP_NEIGHBOUR, node_type="fact", description="one hop",
                edge_relation="related_to", edge_weight=0.8, created_at=AT,
            )]},
        )
        legs = _legs(tr)
        assert "spreading_activation" not in legs
        assert legs["brain_graph"]["n_returned"] == legs["graph_expanded"]["n_returned"] >= 1


class TestSeedAttribution:
    @pytest.mark.asyncio
    async def test_each_row_is_recorded_under_its_winning_seed(self):
        """Seeds here are the two decisions: DECISION_ONE (score 0.75) and
        DECISION_TWO (score None -> 0.5 as a spreading seed)."""
        tr = await _run(
            spreading="true",
            activated=[(SPREAD_A, "fact", 0.5, 1), (SPREAD_B, "fact", 0.3, 2)],
            resolved={SPREAD_A: ("a", AT), SPREAD_B: ("b", AT)},
            seeds_out={SPREAD_A: DECISION_ONE_ID, SPREAD_B: DECISION_TWO_ID},
        )
        rows = {
            e["neighbor_id"]: e for e in tr.to_dict()["expansions"]
            if e["stage"] == "stage4_spreading_activation"
        }
        assert rows[str(SPREAD_A)]["seed_id"] == str(DECISION_ONE_ID)
        assert rows[str(SPREAD_A)]["seed_type"] == "decision"
        assert rows[str(SPREAD_A)]["seed_score"] == pytest.approx(0.75)
        assert rows[str(SPREAD_B)]["seed_id"] == str(DECISION_TWO_ID), (
            "recording every row under seeds[0] would put B under DECISION_ONE"
        )

    @pytest.mark.asyncio
    async def test_a_search_that_reports_no_seed_keeps_the_placeholder(self):
        tr = await _run(
            spreading="true",
            activated=[(SPREAD_A, "fact", 0.5, 1)],
            resolved={SPREAD_A: ("a", AT)},
        )
        row = next(e for e in tr.to_dict()["expansions"]
                   if e["stage"] == "stage4_spreading_activation")
        assert row["seed_type"] == "multi"
        assert row["seed_score"] is None


# ---------------------------------------------------------------------------
# The CTE reports the winning path's seed — needs a real database
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def brain(db, settings):
    b = Brain(database=db, settings=settings)
    yield b
    await b.close()


def _inp(description):
    return RecordInput(
        description=description, confidence=0.8, category="architecture",
        stakes="low", reasons=[ReasonInput(type="analysis", text="seed attribution test")],
    )


async def _edge(session, agent_id, src, tgt, weight):
    from sqlalchemy import text
    await session.execute(text(
        "INSERT INTO brain.graph_edges (source_id,target_id,source_type,target_type,"
        "agent_id,relation,weight,auto_linked,extraction_method) "
        "VALUES (:s,:t,'decision','decision',:a,'related_to',:w,true,'inferred')"),
        {"s": str(src), "t": str(tgt), "a": agent_id, "w": weight})


@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_cte_reports_the_seed_of_the_winning_path(brain, session):
    """d3 hangs off seed d1 and d4 off seed d2. d5 is reachable from both;
    d1's edge is stronger, so d1's path wins and d1 is reported. d6 is two
    hops out (d1 -> d3 -> d6): its seed is d1, not the d3 it was reached from."""
    d = [await brain.record(_inp(f"Seed attribution decision {i} for spreading"),
                            session=session) for i in range(1, 7)]
    d1, d2, d3, d4, d5, d6 = (x.id for x in d)
    await _edge(session, brain.agent_id, d1, d3, 1.0)
    await _edge(session, brain.agent_id, d2, d4, 1.0)
    await _edge(session, brain.agent_id, d1, d5, 0.9)
    await _edge(session, brain.agent_id, d2, d5, 0.2)
    await _edge(session, brain.agent_id, d3, d6, 1.0)
    await session.flush()

    seeds = [(d1, "decision", 0.9), (d2, "decision", 0.9)]
    plain = await spreading_activation_search(session, brain.agent_id, seeds, Settings())
    seed_of: dict = {}
    reported = await spreading_activation_search(
        session, brain.agent_id, seeds, Settings(), seed_out=seed_of,
    )
    assert reported == plain, "reporting the seed must not change activation or depth"
    assert seed_of[d3] == d1
    assert seed_of[d4] == d2
    assert seed_of[d5] == d1, "the stronger path wins MAX, so its seed is reported"
    depth = {r[0]: r[3] for r in reported}
    assert depth[d6] == 2
    assert seed_of[d6] == d1, "the seed is carried along the path, not the parent node"
