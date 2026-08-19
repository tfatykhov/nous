"""F091: the LIVE (graph-primary) procedure path must be traced.

`proc_selection_graph_primary` is ON in prod, which means the passive/embedding
procedure leg never runs and `_select_procedures` is the only path that puts
procedures into context. It performs its own graph traversal — separate from
the pipeline's Stage 2/2b/4 — and was entirely uncaptured. The local corpus has
only 5 fact/decision->procedure edges out of 18,616, so this is pinned with
stubs rather than left to a corpus that cannot exercise it.
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from nous.cognitive.context import ContextEngine
from nous.observability.retrieval_trace import RetrievalTrace


class _Neighbor:
    def __init__(self, nid, weight, relation="uses"):
        self.id = nid
        self.node_type = "procedure"
        self.edge_weight = weight
        self.edge_relation = relation
        self.extraction_method = "inferred"


def _engine(neighbors_by_seed, procedures):
    brain = SimpleNamespace(
        embeddings=None,
        neighbors=lambda seed, node_type, neighbor_type, limit, session=None:
            _async(neighbors_by_seed.get(str(seed), [])),
    )
    heart = SimpleNamespace(
        get_procedure=lambda pid, session=None: _async(procedures[pid]),
    )
    settings = SimpleNamespace(
        proc_graph_neighbors_per_seed=3,
        critic_skill_slots=2,
        embedding_skill_slots=3,
    )
    return ContextEngine(brain, heart, settings, identity_prompt="")


def _async(value):
    async def _coro():
        return value
    return _coro()


@pytest.mark.asyncio
async def test_kline_traversal_is_captured_as_graph_expansion():
    seed = uuid4()
    proc_id = uuid4()
    detail = SimpleNamespace(id=proc_id, active=True, name="deploy",
                             description="d", body="b")

    engine = _engine({str(seed): [_Neighbor(proc_id, 0.8)]}, {proc_id: detail})
    trace = RetrievalTrace(query="q", path="context")

    selected = await engine._select_procedures(
        slots=3, critic_skills=[],
        recalled_ids={"fact": [str(seed)], "decision": []},
        recalled_score_map={str(seed): 0.5},
        session=None, query="q", trace=trace,
    )

    assert [p.id for p in selected] == [proc_id]
    exps = trace.to_dict()["expansions"]
    assert len(exps) == 1
    e = exps[0]
    assert e["seed_id"] == str(seed)
    assert e["seed_type"] == "fact"
    assert e["neighbor_id"] == str(proc_id)
    assert e["neighbor_type"] == "procedure"
    assert e["stage"] == "context_procedure_kline"
    assert e["edge_relation"] == "uses"
    assert e["composed_score"] == pytest.approx(0.8 * 0.5)


@pytest.mark.asyncio
async def test_losing_seed_is_still_recorded_as_traversed():
    """Two seeds reach the same procedure; the weaker path must still appear,
    flagged as not winning — otherwise the trace hides traversal that happened."""
    strong, weak = uuid4(), uuid4()
    proc_id = uuid4()
    detail = SimpleNamespace(id=proc_id, active=True, name="p", description="",
                             body="b")

    engine = _engine(
        {str(strong): [_Neighbor(proc_id, 0.9)], str(weak): [_Neighbor(proc_id, 0.1)]},
        {proc_id: detail},
    )
    trace = RetrievalTrace(query="q", path="context")

    await engine._select_procedures(
        slots=3, critic_skills=[],
        recalled_ids={"fact": [str(strong), str(weak)], "decision": []},
        recalled_score_map={str(strong): 0.9, str(weak): 0.9},
        session=None, query="q", trace=trace,
    )

    exps = trace.to_dict()["expansions"]
    assert len(exps) == 2, "both traversals must be recorded, not just the winner"
    assert sum(1 for e in exps if e["won_best_path"]) == 1


@pytest.mark.asyncio
async def test_trace_none_keeps_selection_working():
    """Telemetry must be optional — the selector runs identically untraced."""
    seed, proc_id = uuid4(), uuid4()
    detail = SimpleNamespace(id=proc_id, active=True, name="p", description="",
                             body="b")
    engine = _engine({str(seed): [_Neighbor(proc_id, 0.8)]}, {proc_id: detail})

    selected = await engine._select_procedures(
        slots=3, critic_skills=[],
        recalled_ids={"fact": [str(seed)], "decision": []},
        recalled_score_map={str(seed): 0.5},
        session=None, query="q", trace=None,
    )
    assert [p.id for p in selected] == [proc_id]
