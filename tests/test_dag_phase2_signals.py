"""Tests for F090.4 Phase-2 gate instrumentation.

sibling_overlap_rate measures whether parallel DAG siblings — mutually deaf
today, since _build_predecessor_context only reads already-terminated
predecessors — actually duplicate work. callback_executed / gate_nodes say
whether those node types run at all. Together these are the go/no-go
evidence for building a Phase 2 worklog/blackboard.
"""

from __future__ import annotations

import uuid

import pytest

from datetime import UTC, datetime

from nous.api.dashboard_queries import _shingle_overlap, get_dag_phase2_signals
from nous.config import Settings
from nous.dag.schemas import DAGCreateRequest, DAGEdgeSpec, DAGNodeSpec, DAGNodeType
from nous.dag.store import DAGStore
from nous.storage.models import Subtask


class TestShingleOverlap:
    def test_identical_text_is_total_overlap(self):
        t = "the build succeeded with zero errors and all tests passing"
        assert _shingle_overlap(t, t) == pytest.approx(1.0)

    def test_disjoint_text_is_no_overlap(self):
        a = "the build succeeded with zero errors and all tests passing"
        b = "database migration applied cleanly across every configured shard"
        assert _shingle_overlap(a, b) == pytest.approx(0.0)

    def test_short_text_cannot_form_shingles(self):
        assert _shingle_overlap("too short", "also short") == 0.0

    def test_partial_overlap_is_between(self):
        a = "the build succeeded with zero errors and all tests passing"
        b = "the build succeeded with zero errors but coverage dropped sharply"
        assert 0.0 < _shingle_overlap(a, b) < 1.0

    def test_exactly_six_words_identical_is_total_overlap(self):
        """Boundary case: a text of exactly 6 words forms exactly one shingle.

        All four cases above use 10-word inputs, leaving the len==6 boundary
        (range(len(words) - 5) with len==6 -> range(1), one shingle) unpinned
        — an off-by-one in that range (e.g. -6 instead of -5) silently makes
        exactly-6-word text produce zero shingles and score 0.0 instead of
        1.0, and none of the other cases would catch it.
        """
        t = "alpha beta gamma delta epsilon zeta"
        assert _shingle_overlap(t, t) == pytest.approx(1.0)


class TestPhase2Signals:
    @pytest.mark.asyncio
    async def test_signals_shape_on_empty_db(self, db):
        async with db.session() as session:
            out = await get_dag_phase2_signals(session, "nobody")
        assert out["sibling_pairs"] == 0
        assert out["overlapping_sibling_pairs"] == 0
        assert out["sibling_overlap_rate"] == 0.0
        assert out["callback_nodes"] == 0
        assert out["callback_executed"] == 0
        assert out["gate_nodes"] == 0

    @pytest.mark.asyncio
    async def test_overlapping_siblings_are_counted(self, db):
        """Two wave-0 siblings with near-identical results register as an overlap.

        This is the case a return-all-zeros stub would still pass on the
        brief's own empty-DB test but must fail here.
        """
        agent_id = f"test-p2-{uuid.uuid4().hex[:8]}"
        store = DAGStore(db, agent_id=agent_id, settings=Settings(_env_file=None))
        req = DAGCreateRequest(
            name="overlap-dag",
            nodes=[
                DAGNodeSpec(name="a", type=DAGNodeType.subtask, instructions="A"),
                DAGNodeSpec(name="b", type=DAGNodeType.subtask, instructions="B"),
            ],
        )
        dag = await store.create(req)
        by_name = {n.name: n.id for n in dag.nodes}
        text_a = "the build succeeded with zero errors and all tests passing"
        text_b = "the build succeeded with zero errors and all tests green"
        await store.update_node(by_name["a"], status="completed", result=text_a)
        await store.update_node(by_name["b"], status="completed", result=text_b)

        async with db.session() as session:
            out = await get_dag_phase2_signals(session, agent_id)

        assert out["sibling_pairs"] == 1
        assert out["overlapping_sibling_pairs"] == 1
        assert out["sibling_overlap_rate"] == pytest.approx(1.0)

    @pytest.mark.asyncio
    async def test_disjoint_siblings_are_not_overlapping(self, db):
        """Two wave-0 siblings with unrelated results form a pair but not an overlap.

        This is the discriminating case: it proves the metric can produce a
        pair count without also inflating the overlap count.
        """
        agent_id = f"test-p2-{uuid.uuid4().hex[:8]}"
        store = DAGStore(db, agent_id=agent_id, settings=Settings(_env_file=None))
        req = DAGCreateRequest(
            name="disjoint-dag",
            nodes=[
                DAGNodeSpec(name="a", type=DAGNodeType.subtask, instructions="A"),
                DAGNodeSpec(name="b", type=DAGNodeType.subtask, instructions="B"),
            ],
        )
        dag = await store.create(req)
        by_name = {n.name: n.id for n in dag.nodes}
        text_a = "the build succeeded with zero errors and all tests passing"
        text_b = "database migration applied cleanly across every configured shard"
        await store.update_node(by_name["a"], status="completed", result=text_a)
        await store.update_node(by_name["b"], status="completed", result=text_b)

        async with db.session() as session:
            out = await get_dag_phase2_signals(session, agent_id)

        assert out["sibling_pairs"] == 1
        assert out["overlapping_sibling_pairs"] == 0
        assert out["sibling_overlap_rate"] == 0.0

    @pytest.mark.asyncio
    async def test_different_waves_are_not_siblings(self, db):
        """A node forced into a different wave is excluded from pairing, even on exact text match."""
        agent_id = f"test-p2-{uuid.uuid4().hex[:8]}"
        store = DAGStore(db, agent_id=agent_id, settings=Settings(_env_file=None))
        req = DAGCreateRequest(
            name="wave-split-dag",
            nodes=[
                DAGNodeSpec(name="a", type=DAGNodeType.subtask, instructions="A"),
                DAGNodeSpec(name="b", type=DAGNodeType.subtask, instructions="B"),
                DAGNodeSpec(name="c", type=DAGNodeType.subtask, instructions="C"),
            ],
        )
        dag = await store.create(req)
        by_name = {n.name: n.id for n in dag.nodes}
        text = "the build succeeded with zero errors and all tests passing"
        await store.update_node(by_name["a"], status="completed", result=text)
        await store.update_node(by_name["b"], status="completed", result=text)
        # All three land in wave 0 by default (no edges) — force 'c' into a
        # distinct wave so a correct implementation must exclude it from
        # pairing despite the exact text match.
        await store.update_node(by_name["c"], status="completed", result=text, wave=1)

        async with db.session() as session:
            out = await get_dag_phase2_signals(session, agent_id)

        assert out["sibling_pairs"] == 1
        assert out["overlapping_sibling_pairs"] == 1

    @pytest.mark.asyncio
    async def test_different_dags_are_not_siblings(self, db):
        """Wave-0 nodes in two separate DAGs are never paired with each other.

        Falsifies a grouping-by-wave-only bug: if dag_id were dropped from the
        group key, these two identical-text, same-agent, wave-0 nodes would
        wrongly form a pair.
        """
        agent_id = f"test-p2-{uuid.uuid4().hex[:8]}"
        store = DAGStore(db, agent_id=agent_id, settings=Settings(_env_file=None))
        text = "the build succeeded with zero errors and all tests passing"
        for dag_name in ("dag-one", "dag-two"):
            req = DAGCreateRequest(
                name=dag_name,
                nodes=[DAGNodeSpec(name="solo", type=DAGNodeType.subtask, instructions="X")],
            )
            dag = await store.create(req)
            await store.update_node(dag.nodes[0].id, status="completed", result=text)

        async with db.session() as session:
            out = await get_dag_phase2_signals(session, agent_id)

        assert out["sibling_pairs"] == 0
        assert out["overlapping_sibling_pairs"] == 0

    @pytest.mark.asyncio
    async def test_callback_executed_counts_only_nonnull_subtask_id(self, db):
        """callback_nodes counts every callback node; callback_executed only
        those F090.1 actually ran — a real subtask that a worker dequeued
        (started_at set), not merely a stamped subtask_id. See
        test_callback_executed_requires_the_subtask_to_have_started for the
        queued-but-never-dequeued discriminating case this test doesn't
        cover.
        """
        agent_id = f"test-p2-{uuid.uuid4().hex[:8]}"
        store = DAGStore(db, agent_id=agent_id, settings=Settings(_env_file=None))
        req = DAGCreateRequest(
            name="callback-dag",
            nodes=[
                DAGNodeSpec(name="a", type=DAGNodeType.subtask, instructions="A"),
                DAGNodeSpec(name="cb1", type=DAGNodeType.callback, instructions="CB1"),
                DAGNodeSpec(name="cb2", type=DAGNodeType.callback, instructions="CB2"),
                DAGNodeSpec(name="gate1", type=DAGNodeType.gate, instructions="G1"),
            ],
            edges=[
                DAGEdgeSpec(from_node="a", to_node="cb1"),
                DAGEdgeSpec(from_node="a", to_node="cb2"),
            ],
        )
        dag = await store.create(req)
        by_name = {n.name: n.id for n in dag.nodes}
        # cb1 was actually dispatched by F090.1 AND dequeued by a worker
        # (started_at set); cb2 never ran.
        async with db.session() as session:
            ran = Subtask(agent_id=agent_id, task="dequeued and executing",
                          status="running", started_at=datetime.now(UTC))
            session.add(ran)
            await session.commit()
            await session.refresh(ran)
        await store.update_node(by_name["cb1"], subtask_id=ran.id)

        async with db.session() as session:
            out = await get_dag_phase2_signals(session, agent_id)

        assert out["callback_nodes"] == 2
        assert out["callback_executed"] == 1
        assert out["gate_nodes"] == 1

    @pytest.mark.asyncio
    async def test_callback_executed_requires_the_subtask_to_have_started(self, db):
        """codex P2: `subtask_id` is stamped by `_launch_subtask_node` right
        after `SubtaskManager.create()` inserts a PENDING row — that is not
        evidence the subtask ever ran. `dequeue()` (heart/subtasks.py) is
        what sets `started_at`, and only a subtask a worker actually
        dequeued gets a `started_at`. A callback that is queued but never
        dequeued (or cancelled before dequeue) must not count toward
        `callback_executed` — under queue pressure that is exactly the state
        callbacks pile up in, and this metric is the go/no-go evidence for
        Phase 2. Must fail before the fix (which only checked
        `subtask_id IS NOT NULL`, true the instant a row is queued).
        """
        agent_id = f"test-p2-{uuid.uuid4().hex[:8]}"
        store = DAGStore(db, agent_id=agent_id, settings=Settings(_env_file=None))
        req = DAGCreateRequest(
            name="queued-vs-run-dag",
            nodes=[
                DAGNodeSpec(name="a", type=DAGNodeType.subtask, instructions="A"),
                DAGNodeSpec(name="cb_queued", type=DAGNodeType.callback, instructions="Q"),
                DAGNodeSpec(name="cb_run", type=DAGNodeType.callback, instructions="R"),
            ],
            edges=[
                DAGEdgeSpec(from_node="a", to_node="cb_queued"),
                DAGEdgeSpec(from_node="a", to_node="cb_run"),
            ],
        )
        dag = await store.create(req)
        by_name = {n.name: n.id for n in dag.nodes}

        async with db.session() as session:
            # Mirrors real heart.subtasks shapes: `queued` is a row a worker
            # has not yet picked up (dequeue() never ran) — status stays
            # 'pending', started_at stays NULL. `ran` is post-dequeue.
            queued = Subtask(agent_id=agent_id, task="queued but never dequeued")
            ran = Subtask(agent_id=agent_id, task="dequeued and executing",
                          status="running", started_at=datetime.now(UTC))
            session.add_all([queued, ran])
            await session.commit()
            await session.refresh(queued)
            await session.refresh(ran)

        await store.update_node(by_name["cb_queued"], subtask_id=queued.id)
        await store.update_node(by_name["cb_run"], subtask_id=ran.id)

        async with db.session() as session:
            out = await get_dag_phase2_signals(session, agent_id)

        assert out["callback_nodes"] == 2
        assert out["callback_executed"] == 1

    @pytest.mark.asyncio
    async def test_unexecuted_callback_siblings_are_excluded(self, db):
        """Two never-executed callbacks (flag-OFF shape: subtask_id NULL,
        near-identical instructions-as-result) must contribute ZERO sibling
        pairs — they never ran, so any overlap is shared authoring
        boilerplate, not duplicated work. The same pair WITH subtask_id set
        (F090.1 actually dispatched them) DOES contribute, proving the
        exclusion is scoped to `subtask_id IS NULL`, not to node_type alone.

        A same-dag, same-wave gate node (completed, non-NULL result) is also
        present throughout and must never become a sibling participant,
        regardless of the callbacks' execution state — gates always auto-pass
        with the same constant result string, so pairing them in would be
        pure noise, never a signal of duplicated work.
        """
        agent_id = f"test-p2-{uuid.uuid4().hex[:8]}"
        store = DAGStore(db, agent_id=agent_id, settings=Settings(_env_file=None))
        text_a = "the build succeeded with zero errors and all tests passing"
        text_b = "the build succeeded with zero errors and all tests green"
        gate_text = "database migration applied cleanly across every configured shard"
        req = DAGCreateRequest(
            name="unexecuted-callback-dag",
            nodes=[
                DAGNodeSpec(name="a", type=DAGNodeType.subtask, instructions="A"),
                DAGNodeSpec(name="cb1", type=DAGNodeType.callback, instructions="CB1"),
                DAGNodeSpec(name="cb2", type=DAGNodeType.callback, instructions="CB2"),
                DAGNodeSpec(name="gt", type=DAGNodeType.gate, instructions="GT"),
            ],
            edges=[
                DAGEdgeSpec(from_node="a", to_node="cb1"),
                DAGEdgeSpec(from_node="a", to_node="cb2"),
                DAGEdgeSpec(from_node="a", to_node="gt"),
            ],
        )
        dag = await store.create(req)
        by_name = {n.name: n.id for n in dag.nodes}
        # Gate: completed with a non-NULL result from the start, same dag +
        # wave as the callback pair (wave 1, via the edges above) — the
        # shape a filter-less query would happily pair.
        await store.update_node(by_name["gt"], status="completed", result=gate_text)
        # Flag-OFF shape: completed with instructions-as-result, subtask_id
        # never set (mirrors orchestrator.py:2119-2126's instant-completion path).
        await store.update_node(by_name["cb1"], status="completed", result=text_a)
        await store.update_node(by_name["cb2"], status="completed", result=text_b)

        async with db.session() as session:
            out = await get_dag_phase2_signals(session, agent_id)

        assert out["sibling_pairs"] == 0
        assert out["overlapping_sibling_pairs"] == 0

        # Same pair, now genuinely executed (subtask_id stamped) — must count.
        # The gate sits in the same (dag_id, wave) group as both but must
        # still be excluded: sibling_pairs stays 1 (cb1-cb2 only), not 3
        # (which pairing the gate in against cb1 and cb2 would produce).
        await store.update_node(by_name["cb1"], subtask_id=uuid.uuid4())
        await store.update_node(by_name["cb2"], subtask_id=uuid.uuid4())

        async with db.session() as session:
            out = await get_dag_phase2_signals(session, agent_id)

        assert out["sibling_pairs"] == 1
        assert out["overlapping_sibling_pairs"] == 1

    @pytest.mark.asyncio
    async def test_agent_scoping(self, db):
        """Signals are scoped to the requesting agent — another agent's DAG rows never leak in."""
        agent_a = f"test-p2-a-{uuid.uuid4().hex[:8]}"
        agent_b = f"test-p2-b-{uuid.uuid4().hex[:8]}"
        store_b = DAGStore(db, agent_id=agent_b, settings=Settings(_env_file=None))

        text = "the build succeeded with zero errors and all tests passing"
        req = DAGCreateRequest(
            name="scoped-dag",
            nodes=[
                DAGNodeSpec(name="a", type=DAGNodeType.subtask, instructions="A"),
                DAGNodeSpec(name="b", type=DAGNodeType.subtask, instructions="B"),
                DAGNodeSpec(name="cb", type=DAGNodeType.callback, instructions="CB"),
            ],
        )
        dag_b = await store_b.create(req)
        by_name = {n.name: n.id for n in dag_b.nodes}
        await store_b.update_node(by_name["a"], status="completed", result=text)
        await store_b.update_node(by_name["b"], status="completed", result=text)
        async with db.session() as session:
            ran = Subtask(agent_id=agent_b, task="dequeued and executing",
                          status="running", started_at=datetime.now(UTC))
            session.add(ran)
            await session.commit()
            await session.refresh(ran)
        await store_b.update_node(by_name["cb"], subtask_id=ran.id)

        async with db.session() as session:
            out_a = await get_dag_phase2_signals(session, agent_a)
            out_b = await get_dag_phase2_signals(session, agent_b)

        assert out_a["sibling_pairs"] == 0
        assert out_a["overlapping_sibling_pairs"] == 0
        assert out_a["callback_nodes"] == 0
        assert out_a["callback_executed"] == 0

        assert out_b["sibling_pairs"] == 1
        assert out_b["overlapping_sibling_pairs"] == 1
        assert out_b["callback_nodes"] == 1
        assert out_b["callback_executed"] == 1
