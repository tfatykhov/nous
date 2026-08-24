"""Phase A of the spreading-activation remediation.

Deliberately scoped to the DELTA. `tests/test_retrieval_pipeline.py` already
covers the result cap, the resolve-drops-everything fallback, and the
`exclude_ids` pushdown; those are not repeated here.

What had no coverage at all before this file:

  * the activation floor — the gate that discards the largest single share of
    what the CTE returns (prod: 428 of ~1,891 activated candidates), and which
    was a bare `0.1` literal at two sites;
  * the fact that a successful spreading run SUPPRESSES the 1-hop leg — the
    behaviour that lets a leg be replaced by one scoring 2.86x lower without a
    single test going red;
  * the score a spreading row actually ends up with, which is why an extra
    `graph_recall_decay` multiplication survived a scoring rework;
  * the hop depth reported to F091, hardcoded to 2 before A8.
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest
import pytest_asyncio
from pydantic import ValidationError
from test_retrieval_pipeline import (  # noqa: E402  (test-local helper reuse)
    _make_brain,
    _make_decision_summaries,
    _make_heart,
    _make_settings,
)

from nous.api.retrieval_pipeline import run_recall_pipeline
from nous.brain.brain import Brain
from nous.brain.schemas import ReasonInput, RecordInput
from nous.brain.spreading_activation import spreading_activation_search
from nous.config import Settings

SPREAD_A = UUID("aaaaaaaa-0000-0000-0000-000000000001")
SPREAD_B = UUID("aaaaaaaa-0000-0000-0000-000000000002")
RESOLVED_AT = datetime(2026, 1, 5, tzinfo=UTC)


@pytest_asyncio.fixture
async def brain(db, settings):
    """Brain without embeddings — mirrors tests/test_spreading_activation.py.

    Defined here rather than imported because that fixture is module-local.
    """
    b = Brain(database=db, settings=settings)
    yield b
    await b.close()


def _reasons():
    return [ReasonInput(type="analysis", text="Phase A spreading test decision")]


async def _edge(session, agent_id, src, tgt):
    """Insert a traversable decision->decision edge. `flush`, never `commit`:
    the `session` fixture gives rollback isolation and a commit would leak
    rows into whatever database the suite is pointed at."""
    from sqlalchemy import text
    await session.execute(text(
        "INSERT INTO brain.graph_edges (source_id,target_id,source_type,target_type,"
        "agent_id,relation,weight,auto_linked,extraction_method) "
        "VALUES (:s,:t,'decision','decision',:a,'related_to',1.0,true,'inferred')"),
        {"s": str(src), "t": str(tgt), "a": agent_id})


def _fixtures(resolved):
    heart = _make_heart(recall_results=[])
    brain = _make_brain(
        neighbors_by_node={},
        contradictions=[],
        decision_results=_make_decision_summaries(),
    )
    brain._resolve_node_descriptions = AsyncMock(return_value=resolved)
    return heart, brain


async def _run(settings, activated, resolved):
    heart, brain = _fixtures(resolved)
    with patch(
        "nous.brain.spreading_activation.spreading_activation_search",
        AsyncMock(return_value=activated),
    ), patch(
        "nous.brain.spreading_activation.compute_graph_density",
        AsyncMock(return_value=5.0),
    ):
        results, stats = await run_recall_pipeline(
            query="anything", heart=heart, brain=brain,
            settings=settings, limit=10,
        )
    return results, stats, brain


# ---------------------------------------------------------------------------
# A1 / A4 / A7 — configuration
# ---------------------------------------------------------------------------


class TestConfig:
    def test_alpha_beta_gamma_are_gone(self):
        """BR-26: F022's never-built combined scorer. Dead since 2026-06-09.

        Asserted as ABSENT rather than deleted-by-grep so a well-meaning
        re-add has to argue with a test.
        """
        s = Settings()
        for dead in ("spreading_activation_alpha",
                     "spreading_activation_beta",
                     "spreading_activation_gamma"):
            assert not hasattr(s, dead), f"{dead} was re-added; it has no consumer"

    def test_floor_default_is_the_prior_hardcoded_constant(self):
        """A4 must be an exact no-op: 0.1 is what the literal was."""
        assert Settings().spreading_activation_floor == 0.1

    @pytest.mark.parametrize("depth", [0, -1, 4, 10])
    def test_max_depth_is_bounded(self, depth):
        """max_depth is the exponent of an exponential traversal."""
        with pytest.raises(ValidationError):
            Settings(spreading_activation_max_depth=depth)

    @pytest.mark.parametrize("decay", [0.0, -0.5, 1.5, 2.0])
    def test_decay_is_bounded(self, decay):
        """decay > 1.0 AMPLIFIES per hop, breaking the `activation <= seed`
        invariant that MAX aggregation relies on to keep spreading rows on the
        candidate score scale."""
        with pytest.raises(ValidationError):
            Settings(spreading_activation_decay=decay)

    def test_bounds_admit_the_documented_range(self):
        assert Settings(spreading_activation_max_depth=3, spreading_activation_decay=1.0)


# ---------------------------------------------------------------------------
# A4 — the activation floor, previously untested at any layer
# ---------------------------------------------------------------------------


class TestActivationFloor:
    @pytest.mark.asyncio
    async def test_below_floor_nodes_never_render(self):
        settings = _make_settings(spreading_activation_enabled="true")
        results, _stats, _brain = await _run(
            settings,
            activated=[(SPREAD_A, "fact", 0.5, 1), (SPREAD_B, "fact", 0.05, 2)],
            resolved={SPREAD_A: ("above floor", RESOLVED_AT),
                      SPREAD_B: ("below floor", RESOLVED_AT)},
        )
        ids = {r.id for r in results}
        assert SPREAD_A in ids
        assert SPREAD_B not in ids, "0.05 is below the 0.1 floor"

    @pytest.mark.asyncio
    async def test_floor_is_read_from_settings_not_hardcoded(self):
        """Raising the floor must drop a node that clears the default.

        Mutation check: this fails if either site reverts to a `0.1` literal.
        """
        settings = _make_settings(spreading_activation_enabled="true")
        settings.spreading_activation_floor = 0.6
        results, _stats, _brain = await _run(
            settings,
            activated=[(SPREAD_A, "fact", 0.5, 1)],
            resolved={SPREAD_A: ("clears 0.1, not 0.6", RESOLVED_AT)},
        )
        assert SPREAD_A not in {r.id for r in results}

    @pytest.mark.asyncio
    async def test_floor_is_exclusive_at_the_boundary(self):
        """`> floor`, not `>=` — pins which side the boundary falls on."""
        settings = _make_settings(spreading_activation_enabled="true")
        settings.spreading_activation_floor = 0.5
        results, _stats, _brain = await _run(
            settings,
            activated=[(SPREAD_A, "fact", 0.5, 1)],
            resolved={SPREAD_A: ("exactly at the floor", RESOLVED_AT)},
        )
        assert SPREAD_A not in {r.id for r in results}


# ---------------------------------------------------------------------------
# The suppression invariant — nothing asserted this before
# ---------------------------------------------------------------------------


class TestSpreadingSuppressesOneHop:
    @pytest.mark.asyncio
    async def test_successful_spreading_skips_the_one_hop_leg(self):
        """Stage 4 is either/or: spreading REPLACES 1-hop, it does not augment it.

        This is the behaviour that let a leg scoring `seed x edge_weight` be
        swapped for one scoring `seed x edge_weight x 0.5 x 0.7` with no test
        going red. Pinning it means any future change to the branch has to be
        deliberate.
        """
        settings = _make_settings(spreading_activation_enabled="true")
        _results, _stats, brain = await _run(
            settings,
            activated=[(SPREAD_A, "fact", 0.5, 1)],
            resolved={SPREAD_A: ("spreading won", RESOLVED_AT)},
        )
        brain.neighbors.assert_not_called()

    @pytest.mark.asyncio
    async def test_one_hop_runs_when_spreading_is_off(self):
        """The complement — otherwise the assertion above passes for the wrong
        reason (e.g. a fixture that never calls neighbors at all)."""
        settings = _make_settings(spreading_activation_enabled="false")
        heart, brain = _fixtures({})
        await run_recall_pipeline(
            query="anything", heart=heart, brain=brain,
            settings=settings, limit=10,
        )
        assert brain.neighbors.called


# ---------------------------------------------------------------------------
# Score pinning — baseline for the Phase C double-decay change
# ---------------------------------------------------------------------------


class TestSpreadingRowScore:
    @pytest.mark.asyncio
    async def test_score_is_activation_times_graph_recall_decay(self):
        """Pins TODAY's behaviour, including the second decay.

        The CTE already composed `spreading_activation_decay` per hop; the
        pipeline then multiplies by `graph_recall_decay` again. That is a
        retained legacy carve-out, not a defect — but it is the reason a
        spreading row cannot reach the top-k cutline, and Phase C changes it.
        This test must FAIL when that lands, loudly and on purpose.
        """
        settings = _make_settings(
            spreading_activation_enabled="true", graph_recall_decay=0.7,
        )
        results, _stats, _brain = await _run(
            settings,
            activated=[(SPREAD_A, "fact", 0.5, 1)],
            resolved={SPREAD_A: ("scored row", RESOLVED_AT)},
        )
        row = next(r for r in results if r.id == SPREAD_A)
        assert row.score == pytest.approx(0.5 * 0.7)


# ---------------------------------------------------------------------------
# A8 — real hop depth reaches F091
# ---------------------------------------------------------------------------


class _CapturingTrace:
    """Minimal stand-in that records only what A8 is about."""

    enabled = True

    def __init__(self):
        self.expansions: list[dict] = []

    def expansion(self, **kw):
        self.expansions.append(kw)

    def __getattr__(self, _name):
        return lambda *a, **k: None


class TestHopDepthTelemetry:
    @pytest.mark.asyncio
    async def test_hop_is_the_depth_the_cte_reported(self):
        """Before A8 this was hardcoded 2 for every spreading expansion, so
        production telemetry could not tell a one-hop neighbour from a two-hop
        one — the exact split the depth question turns on."""
        settings = _make_settings(spreading_activation_enabled="true")
        heart, brain = _fixtures({
            SPREAD_A: ("one hop away", RESOLVED_AT),
            SPREAD_B: ("two hops away", RESOLVED_AT),
        })
        tr = _CapturingTrace()
        with patch(
            "nous.brain.spreading_activation.spreading_activation_search",
            AsyncMock(return_value=[
                (SPREAD_A, "fact", 0.5, 1),
                (SPREAD_B, "fact", 0.4, 2),
            ]),
        ), patch(
            "nous.brain.spreading_activation.compute_graph_density",
            AsyncMock(return_value=5.0),
        ):
            await run_recall_pipeline(
                query="anything", heart=heart, brain=brain,
                settings=settings, limit=10, trace=tr,
            )

        spread = {
            e["neighbor_id"]: e for e in tr.expansions
            if e.get("stage") == "stage4_spreading_activation"
        }
        assert spread[SPREAD_A]["hop"] == 1
        assert spread[SPREAD_B]["hop"] == 2, (
            "a hardcoded hop would report 2 for both and pass a weaker assertion"
        )


# ---------------------------------------------------------------------------
# A6 / A8 at the CTE layer — needs a real database
# ---------------------------------------------------------------------------


def _inp(description):
    return RecordInput(description=description, confidence=0.8,
                       category="architecture", stakes="low", reasons=_reasons())


@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_cte_returns_depth_per_node(brain, session):
    """A8 end-to-end: the seed is depth 0, a linked neighbour is depth 1."""
    d1 = await brain.record(_inp("Depth test decision one for spreading traversal"),
                            session=session)
    d2 = await brain.record(_inp("Depth test decision two for spreading traversal"),
                            session=session)
    await _edge(session, brain.agent_id, d1.id, d2.id)
    await session.flush()

    activated = await spreading_activation_search(
        session, brain.agent_id, [(d1.id, "decision", 0.9)], Settings(),
    )
    depth_by_id = {r[0]: r[3] for r in activated}
    assert depth_by_id[d1.id] == 0, "the seed itself is depth 0"
    assert depth_by_id[d2.id] == 1, "a directly linked neighbour is depth 1"


@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_cte_excludes_decisions_the_resolver_would_refuse(brain, session):
    """A6: a demoted-outcome decision can never be rendered, so it must not
    consume a slot in the result window either."""
    from sqlalchemy import text

    seed = await brain.record(_inp("Outcome pushdown seed decision for traversal"),
                              session=session)
    demoted = await brain.record(
        _inp("Outcome pushdown superseded neighbour decision"), session=session)
    await _edge(session, brain.agent_id, seed.id, demoted.id)
    await session.execute(
        text("UPDATE brain.decisions SET outcome='superseded' WHERE id=:i"),
        {"i": demoted.id},
    )
    await session.flush()

    with_filter = await spreading_activation_search(
        session, brain.agent_id, [(seed.id, "decision", 0.9)], Settings(),
    )
    assert demoted.id not in {r[0] for r in with_filter}

    # Kill switch: `{}` disables the demotion policy, so the row comes back.
    # Without this the test would also pass if the CTE simply never returned
    # the neighbour for an unrelated reason — i.e. it pins the FILTER, not the
    # absence.
    off = Settings(decision_outcome_score_factors={})
    without_filter = await spreading_activation_search(
        session, brain.agent_id, [(seed.id, "decision", 0.9)], off,
    )
    assert demoted.id in {r[0] for r in without_filter}


# ---------------------------------------------------------------------------
# C-S — depth-1 parity with the 1-hop leg spreading replaces
# ---------------------------------------------------------------------------


class TestDepth1Parity:
    """The CTE decays every hop INCLUDING the first, and the pipeline then
    multiplied by `graph_recall_decay` again — so a spreading row scored 2.857x
    below the identical (seed, edge, neighbour) triple reached by the 1-hop leg
    that spreading suppresses. The fix divides out one decay factor, giving
    `decay^(depth-1)`.
    """

    def _spread_row(self, activation):
        from nous.brain.schemas import NeighborResult
        return NeighborResult(
            id=SPREAD_A, node_type="fact", description="n",
            edge_relation="spreading_activation", edge_weight=activation,
            created_at=RESOLVED_AT,
        )

    def _one_hop_row(self, seed_score, weight):
        from nous.brain.schemas import NeighborResult
        n = NeighborResult(
            id=SPREAD_B, node_type="fact", description="n",
            edge_relation="related_to", edge_weight=weight,
            created_at=RESOLVED_AT, extraction_method="inferred",
        )
        n.seed_score = seed_score
        return n

    def test_flag_off_is_todays_behaviour(self):
        from nous.api.retrieval_pipeline import _score_memory_neighbor
        s = _make_settings(graph_recall_decay=0.7)
        s.spreading_score_depth1_parity = False
        s.spreading_activation_decay = 0.5
        assert _score_memory_neighbor(self._spread_row(0.25), s) == pytest.approx(0.175)

    def _parity_settings(self):
        """Settings where parity is DEFINED — the prod policy."""
        s = _make_settings(graph_recall_decay=0.7)
        s.spreading_score_depth1_parity = True
        s.spreading_activation_decay = 0.5
        s.graph_neighbor_seed_score_enabled = True
        s.graph_inferred_edge_penalty = 1.0
        return s

    def test_flag_on_divides_out_one_decay(self):
        from nous.api.retrieval_pipeline import _score_memory_neighbor
        s = self._parity_settings()
        # activation 0.25 = seed 0.5 * w 1.0 * decay 0.5 at depth 1
        assert _score_memory_neighbor(self._spread_row(0.25), s) == pytest.approx(0.5)

    def test_inert_when_one_hop_is_not_using_the_seed_score_branch(self):
        """codex P2. With `graph_neighbor_seed_score_enabled=false` the 1-hop leg
        scores `w * graph_recall_decay`, not `seed * w` — so dividing out the SA
        decay would be a PROMOTION, not parity (0.72 vs 0.63 at seed 0.8/w 0.9).
        The flag must go inert rather than silently promote."""
        from nous.api.retrieval_pipeline import _score_memory_neighbor
        s = self._parity_settings()
        s.graph_neighbor_seed_score_enabled = False
        assert _score_memory_neighbor(self._spread_row(0.25), s) == pytest.approx(0.175)

    def test_inert_when_an_inferred_edge_penalty_is_active(self):
        """A spreading activation composes several edges of mixed provenance, so
        there is no single `extraction_method` to price — the penalty cannot be
        mirrored. With F065 active the 1-hop leg drops to 0.5040 while parity
        would still return 0.7200, so the flag goes inert instead."""
        from nous.api.retrieval_pipeline import _score_memory_neighbor
        s = self._parity_settings()
        s.graph_inferred_edge_penalty = 0.7
        assert _score_memory_neighbor(self._spread_row(0.25), s) == pytest.approx(0.175)

    def test_parity_holds_across_seeds_and_weights_under_the_prod_policy(self):
        """The gate must not merely make one hand-picked pair agree."""
        from nous.api.retrieval_pipeline import _score_memory_neighbor
        s = self._parity_settings()
        for seed in (0.3, 0.55, 0.8, 1.0):
            for w in (0.4, 0.75, 1.0):
                one_hop = _score_memory_neighbor(self._one_hop_row(seed, w), s)
                spread = _score_memory_neighbor(
                    self._spread_row(seed * w * s.spreading_activation_decay), s)
                assert spread == pytest.approx(one_hop), f"seed={seed} w={w}"

    def test_depth1_reaches_parity_with_the_one_hop_leg(self):
        """The POINT of the change, not just its arithmetic.

        Same seed, same edge weight, same neighbour — reached by spreading vs by
        the 1-hop leg — must score the same. Before the fix the ratio is 2.857.
        """
        from nous.api.retrieval_pipeline import _score_memory_neighbor
        seed, weight, sa_decay = 0.8, 0.9, 0.5
        s = _make_settings(graph_recall_decay=0.7)
        s.graph_neighbor_seed_score_enabled = True
        s.graph_inferred_edge_penalty = 1.0
        s.spreading_activation_decay = sa_decay

        one_hop = _score_memory_neighbor(self._one_hop_row(seed, weight), s)

        s.spreading_score_depth1_parity = False
        before = _score_memory_neighbor(
            self._spread_row(seed * weight * sa_decay), s)
        s.spreading_score_depth1_parity = True
        after = _score_memory_neighbor(
            self._spread_row(seed * weight * sa_decay), s)

        assert one_hop / before == pytest.approx(2.857, abs=0.01), "the measured gap"
        assert after == pytest.approx(one_hop), "parity is the target"

    def test_depth2_keeps_exactly_one_decay(self):
        """Parity must not flatten depth — an extra hop is still discounted."""
        from nous.api.retrieval_pipeline import _score_memory_neighbor
        seed, w = 0.8, 1.0
        s = self._parity_settings()
        d = s.spreading_activation_decay
        depth1 = _score_memory_neighbor(self._spread_row(seed * w * d), s)
        depth2 = _score_memory_neighbor(self._spread_row(seed * w * d * w * d), s)
        assert depth2 == pytest.approx(depth1 * d)

    def test_bound_holds_at_the_ceiling(self):
        """MAX aggregation needs activation <= seed <= 1 to keep spreading on
        the candidate score scale. Parity must not breach it."""
        from nous.api.retrieval_pipeline import _score_memory_neighbor
        s = self._parity_settings()
        # strongest possible depth-1: seed 1.0, weight 1.0
        assert _score_memory_neighbor(self._spread_row(1.0 * 1.0 * 0.5), s) <= 1.0

    def test_non_spreading_rows_are_untouched_by_the_flag(self):
        from nous.api.retrieval_pipeline import _score_memory_neighbor
        s = _make_settings(graph_recall_decay=0.7)
        s.graph_neighbor_seed_score_enabled = False
        s.graph_inferred_edge_penalty = 1.0
        s.spreading_activation_decay = 0.5
        row = self._one_hop_row(None, 0.9)
        row.seed_score = None
        s.spreading_score_depth1_parity = False
        off = _score_memory_neighbor(row, s)
        s.spreading_score_depth1_parity = True
        assert _score_memory_neighbor(row, s) == pytest.approx(off)

    def test_zero_decay_falls_back_rather_than_dividing(self):
        """Defensive: config bounds decay to (0,1], but a SimpleNamespace in a
        test could carry 0 — never raise ZeroDivisionError in the scorer."""
        from nous.api.retrieval_pipeline import _score_memory_neighbor
        s = self._parity_settings()
        s.spreading_activation_decay = 0.0
        assert _score_memory_neighbor(self._spread_row(0.25), s) == pytest.approx(0.175)
