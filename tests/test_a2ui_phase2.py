"""F092 Phase 2: callAgentFunction RPC, decision/DAG verbs, submitted-model
shape re-validation, self-sourcing push_surface templates, new builders.

Postgres-only for the same reason as test_a2ui_actions.py: allowlist and
surface rows use Postgres ARRAY columns. CI runs NOUS_TEST_DB=postgres.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import delete, select

from nous.a2ui.actions import ActionRouter, _submitted_model_error
from nous.a2ui.builders import dag_monitor, decision_sweep, memory_graph
from nous.storage.models import A2uiAction, A2uiSurface

pytestmark = pytest.mark.postgres_only

JSON = "application/json"

DECISION_A = "3f2a1b4c-5d6e-4f70-8192-a3b4c5d6e7f8"
DECISION_B = "aa11bb22-cc33-4d44-8e55-ff6677889900"

SWEEP_PARAMS = {
    "decisions": [
        {
            "id": DECISION_A,
            "description": "Ship the eval harness behind a flag",
            "confidence": 0.8,
            "stakes": "medium",
            "category": "architecture",
        },
        {
            "id": DECISION_B,
            "description": "Use RRF for the merge",
            "confidence": 0.6,
            "stakes": "low",
            "category": "tooling",
        },
    ]
}

DAG_PARAMS = {
    "dag_id": "0e9d8c7b-6a5f-4e3d-8c2b-1a0f9e8d7c6b",
    "name": "nightly-audit",
    "status": "running",
    "nodes": [
        {"name": "collect", "status": "completed", "node_type": "subtask"},
        {"name": "analyze", "status": "failed", "node_type": "subtask"},
        {"name": "report", "status": "pending", "node_type": "callback"},
    ],
    "edges": [{"from": "collect", "to": "analyze"}, {"from": "analyze", "to": "report"}],
}

GRAPH_PARAMS = {
    "node_id": "b1c2d3e4-f5a6-4b7c-8d9e-0f1a2b3c4d5e",
    "node_type": "fact",
    "label": "prod embeddings are text-embedding-3-large",
}


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeBrain:
    """Brain reduced to what Phase 2 touches."""

    def __init__(self) -> None:
        self.reviews: list[tuple[str, str, str | None, str | None]] = []
        self.neighbor_calls: list[tuple[str, str, int]] = []
        self._detail: Any = SimpleNamespace(
            id=uuid.UUID(DECISION_A),
            description="Ship the eval harness behind a flag",
            confidence=0.8,
            stakes="medium",
            category="architecture",
            outcome=None,
            reasons=[SimpleNamespace(type="analysis", text="measured on the harness")],
        )

    async def review(self, decision_id, outcome, result=None, reviewer=None, **_):
        self.reviews.append((str(decision_id), outcome, result, reviewer))

    async def neighbors(self, node_id, node_type="decision", limit=10, **_):
        self.neighbor_calls.append((str(node_id), node_type, limit))
        return [
            SimpleNamespace(
                id=uuid.UUID("11111111-2222-4333-8444-555555555555"),
                node_type="decision",
                description="A neighboring decision " + "x" * 200,
                edge_relation="informed_by",
                edge_weight=0.7,
                extraction_method="inferred",
            )
        ]

    async def get(self, decision_id):
        return self._detail if str(decision_id) == DECISION_A else None

    async def get_unreviewed(self, max_age_days=30, stakes=None, limit=None):
        self.unreviewed_limits = getattr(self, "unreviewed_limits", [])
        self.unreviewed_limits.append(limit)
        return [
            SimpleNamespace(
                id=uuid.UUID(DECISION_A),
                description="Ship the eval harness behind a flag",
                confidence=0.8,
                stakes="medium",
                category="architecture",
            )
        ]


class FailingBrain(FakeBrain):
    async def review(self, *args, **kwargs):
        raise RuntimeError("db down")


class FakeOrchestrator:
    def __init__(self) -> None:
        self.retries: list[tuple[str, str]] = []
        self.cancels: list[tuple[str, str]] = []

    async def retry_node(self, dag_id, node_name):
        self.retries.append((str(dag_id), node_name))

    async def cancel_dag(self, dag_id, reason=""):
        self.cancels.append((str(dag_id), reason))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def a2ui_agent_id() -> str:
    return f"test-a2ui-p2-{uuid.uuid4().hex[:12]}"


@pytest.fixture
def a2ui_settings(settings, a2ui_agent_id: str):
    return settings.model_copy(
        update={
            "agent_id": a2ui_agent_id,
            "telegram_bot_token": None,
            "telegram_chat_id": None,
        }
    )


@pytest_asyncio.fixture
async def service(db, a2ui_settings, a2ui_agent_id: str):
    from nous.a2ui.service import SurfaceService

    svc = SurfaceService(db, a2ui_settings)
    yield svc
    async with db.session() as session:
        await session.execute(delete(A2uiAction).where(A2uiAction.agent_id == a2ui_agent_id))
        await session.execute(delete(A2uiSurface).where(A2uiSurface.agent_id == a2ui_agent_id))
        await session.commit()


@pytest.fixture
def brain() -> FakeBrain:
    return FakeBrain()


@pytest.fixture
def orchestrator() -> FakeOrchestrator:
    return FakeOrchestrator()


@pytest.fixture
def router(db, a2ui_settings, service, brain: FakeBrain, orchestrator: FakeOrchestrator):
    return ActionRouter(
        db,
        a2ui_settings,
        service,
        brain=brain,
        dag_orchestrator=orchestrator,
    )


async def _surface_row(db, surface_id: str) -> A2uiSurface:
    async with db.session() as session:
        row = await session.get(A2uiSurface, surface_id)
        assert row is not None
        return row


async def _audits(db, agent_id: str) -> list[A2uiAction]:
    async with db.session() as session:
        result = await session.execute(
            select(A2uiAction)
            .where(A2uiAction.agent_id == agent_id)
            .order_by(A2uiAction.created_at)
        )
        return list(result.scalars().all())


def _action_body(name: str, surface_id: str, nonce: str, context: dict | None = None) -> dict:
    return {
        "version": "v1.0",
        "action": {
            "name": name,
            "surfaceId": surface_id,
            "context": dict(context or {}),
            "metadata": {"extensions": {"com_nous_nonce": nonce}},
        },
    }


def _call_body(
    surface_id: str,
    nonce: str | None,
    call: str,
    args: dict | None = None,
    *,
    function_call_id: str = "fc-test-1",
) -> dict:
    body: dict[str, Any] = {
        "version": "v1.0",
        "callAgentFunction": {
            "surfaceId": surface_id,
            "functionCallId": function_call_id,
            "callFunction": {"call": call, "args": dict(args or {})},
        },
    }
    if nonce is not None:
        body["metadata"] = {"extensions": {"com_nous_nonce": nonce}}
    return body


# ---------------------------------------------------------------------------
# handle_call gates
# ---------------------------------------------------------------------------


async def test_call_refuses_non_json_content_type(router: ActionRouter) -> None:
    status, payload = await router.handle_call(
        _call_body("whatever", "n", "expandGraphNode"), content_type="text/plain"
    )

    assert status == 415
    assert payload["agentFunctionResponse"]["error"]["code"] == "INVALID_FUNCTION_CALL"


async def test_call_refuses_missing_fields(router: ActionRouter) -> None:
    status, payload = await router.handle_call(
        {"version": "v1.0", "callAgentFunction": {}}, content_type=JSON
    )

    assert status == 400
    assert payload["agentFunctionResponse"]["error"]["code"] == "INVALID_FUNCTION_CALL"


async def test_call_unknown_surface_is_404(router: ActionRouter) -> None:
    status, payload = await router.handle_call(
        _call_body("nous:nope:nope:000000", "n", "expandGraphNode"), content_type=JSON
    )

    assert status == 404
    assert payload["agentFunctionResponse"]["error"]["code"] == "INVALID_FUNCTION_CALL"


async def test_call_nonce_mismatch_is_403(router: ActionRouter, service, db) -> None:
    surface_id = await service.push_built(memory_graph(GRAPH_PARAMS))

    status, payload = await router.handle_call(
        _call_body(surface_id, "stale-nonce", "expandGraphNode"), content_type=JSON
    )

    assert status == 403
    assert payload["agentFunctionResponse"]["error"]["code"] == "INVALID_FUNCTION_CALL"


async def test_call_unknown_function_is_404(router: ActionRouter, service, db) -> None:
    surface_id = await service.push_built(memory_graph(GRAPH_PARAMS))
    nonce = (await _surface_row(db, surface_id)).nonce

    status, payload = await router.handle_call(
        _call_body(surface_id, nonce, "dropAllTables"), content_type=JSON
    )

    assert status == 404
    assert payload["agentFunctionResponse"]["error"]["code"] == "UNKNOWN_FUNCTION"


async def test_call_is_rate_limited(
    db, a2ui_settings, service, brain: FakeBrain
) -> None:
    throttled = a2ui_settings.model_copy(update={"a2ui_action_rate_per_minute": 1})
    router = ActionRouter(db, throttled, service, brain=brain)
    surface_id = await service.push_built(memory_graph(GRAPH_PARAMS))
    nonce = (await _surface_row(db, surface_id)).nonce
    body = _call_body(
        surface_id, nonce, "expandGraphNode", {"nodeId": GRAPH_PARAMS["node_id"]}
    )

    first_status, _ = await router.handle_call(body, content_type=JSON)
    second_status, payload = await router.handle_call(body, content_type=JSON)

    assert first_status == 200
    assert second_status == 429
    assert payload["agentFunctionResponse"]["error"]["code"] == "RATE_LIMITED"


# ---------------------------------------------------------------------------
# Agent functions
# ---------------------------------------------------------------------------


async def test_expand_graph_node_returns_merged_neighborhood(
    router: ActionRouter, service, db, brain: FakeBrain
) -> None:
    surface_id = await service.push_built(memory_graph(GRAPH_PARAMS))
    nonce = (await _surface_row(db, surface_id)).nonce

    status, payload = await router.handle_call(
        _call_body(
            surface_id,
            nonce,
            "expandGraphNode",
            {"nodeId": GRAPH_PARAMS["node_id"], "nodeType": "fact"},
            function_call_id="fc-42",
        ),
        content_type=JSON,
    )

    assert status == 200
    afr = payload["agentFunctionResponse"]
    assert afr["functionCallId"] == "fc-42"
    value = afr["value"]
    assert value["nodes"][0]["id"] == "11111111-2222-4333-8444-555555555555"
    assert len(value["nodes"][0]["label"]) <= 120, "labels are truncated server-side"
    assert value["edges"][0]["source"] == GRAPH_PARAMS["node_id"]
    assert value["edges"][0]["relation"] == "informed_by"
    assert brain.neighbor_calls == [(GRAPH_PARAMS["node_id"], "fact", 8)]


async def test_expand_graph_node_caps_the_limit(
    router: ActionRouter, service, db, brain: FakeBrain
) -> None:
    surface_id = await service.push_built(memory_graph(GRAPH_PARAMS))
    nonce = (await _surface_row(db, surface_id)).nonce

    status, _ = await router.handle_call(
        _call_body(
            surface_id,
            nonce,
            "expandGraphNode",
            {"nodeId": GRAPH_PARAMS["node_id"], "limit": 500},
        ),
        content_type=JSON,
    )

    assert status == 200
    assert brain.neighbor_calls[0][2] == 20, "client cannot demand an unbounded fan-out"


@pytest.mark.parametrize(
    "args",
    [
        {"nodeId": "not-a-uuid"},
        {"nodeId": GRAPH_PARAMS["node_id"], "nodeType": "user_table"},
    ],
)
async def test_expand_graph_node_rejects_bad_args(
    router: ActionRouter, service, db, args: dict
) -> None:
    surface_id = await service.push_built(memory_graph(GRAPH_PARAMS))
    nonce = (await _surface_row(db, surface_id)).nonce

    status, payload = await router.handle_call(
        _call_body(surface_id, nonce, "expandGraphNode", args), content_type=JSON
    )

    assert status == 422
    assert payload["agentFunctionResponse"]["error"]["code"] == "INVALID_FUNCTION_CALL"


async def test_load_decision_detail_round_trips(
    router: ActionRouter, service, db
) -> None:
    surface_id = await service.push_built(decision_sweep(SWEEP_PARAMS))
    nonce = (await _surface_row(db, surface_id)).nonce

    status, payload = await router.handle_call(
        _call_body(surface_id, nonce, "loadDecisionDetail", {"decisionId": DECISION_A}),
        content_type=JSON,
    )

    assert status == 200
    value = payload["agentFunctionResponse"]["value"]
    assert value["id"] == DECISION_A
    assert value["reasons"] == [{"type": "analysis", "text": "measured on the harness"}]


async def test_load_decision_detail_unknown_id_is_422(
    router: ActionRouter, service, db
) -> None:
    surface_id = await service.push_built(decision_sweep(SWEEP_PARAMS))
    nonce = (await _surface_row(db, surface_id)).nonce

    status, payload = await router.handle_call(
        _call_body(surface_id, nonce, "loadDecisionDetail", {"decisionId": DECISION_B}),
        content_type=JSON,
    )

    assert status == 422
    assert "not found" in payload["agentFunctionResponse"]["error"]["message"]


# ---------------------------------------------------------------------------
# decision.resolve
# ---------------------------------------------------------------------------


async def test_decision_resolve_records_patches_and_keeps_surface_live(
    router: ActionRouter, service, db, brain: FakeBrain, a2ui_agent_id: str
) -> None:
    surface_id = await service.push_built(decision_sweep(SWEEP_PARAMS))
    nonce = (await _surface_row(db, surface_id)).nonce

    status, payload = await router.handle(
        _action_body(
            "decision.resolve",
            surface_id,
            nonce,
            {"decisionId": DECISION_A, "outcome": "success"},
        ),
        content_type=JSON,
    )

    assert status == 200
    assert payload["resolved"] is False, "one decision left — the sweep stays live"
    assert brain.reviews == [
        (DECISION_A, "success", "resolved via companion sweep", "a2ui")
    ]
    surface = await _surface_row(db, surface_id)
    assert surface.status == "live"
    assert surface.data_model["decisions"][DECISION_A] == "success"
    assert surface.data_model["decisions"][DECISION_B] == "pending"
    assert [a.status for a in await _audits(db, a2ui_agent_id)] == ["completed"]


async def test_decision_resolve_resolves_surface_on_last_decision(
    router: ActionRouter, service, db
) -> None:
    surface_id = await service.push_built(decision_sweep(SWEEP_PARAMS))
    nonce = (await _surface_row(db, surface_id)).nonce

    await router.handle(
        _action_body(
            "decision.resolve", surface_id, nonce,
            {"decisionId": DECISION_A, "outcome": "success"},
        ),
        content_type=JSON,
    )
    status, payload = await router.handle(
        _action_body(
            "decision.resolve", surface_id, nonce,
            {"decisionId": DECISION_B, "outcome": "noise"},
        ),
        content_type=JSON,
    )

    assert status == 200
    assert payload["resolved"] is True
    assert (await _surface_row(db, surface_id)).status == "resolved"


async def test_decision_resolve_rejects_a_decision_not_on_the_surface(
    router: ActionRouter, service, db, brain: FakeBrain
) -> None:
    """A client with the nonce still cannot review arbitrary decisions —
    the surface's own data model is the allowlist (the approval.choose
    lesson)."""
    surface_id = await service.push_built(decision_sweep(SWEEP_PARAMS))
    nonce = (await _surface_row(db, surface_id)).nonce
    foreign = "99999999-8888-4777-8666-555544443333"

    status, payload = await router.handle(
        _action_body(
            "decision.resolve", surface_id, nonce,
            {"decisionId": foreign, "outcome": "success"},
        ),
        content_type=JSON,
    )

    assert status == 422
    assert "not on this surface" in payload["error"]["message"]
    assert brain.reviews == []


async def test_decision_resolve_rejects_an_invented_outcome(
    router: ActionRouter, service, db, brain: FakeBrain
) -> None:
    surface_id = await service.push_built(decision_sweep(SWEEP_PARAMS))
    nonce = (await _surface_row(db, surface_id)).nonce

    status, payload = await router.handle(
        _action_body(
            "decision.resolve", surface_id, nonce,
            {"decisionId": DECISION_A, "outcome": "meh"},
        ),
        content_type=JSON,
    )

    assert status == 422
    assert "outcome must be one of" in payload["error"]["message"]
    assert brain.reviews == []


async def test_decision_resolve_reports_a_failed_review_not_a_silent_200(
    db, a2ui_settings, service
) -> None:
    router = ActionRouter(db, a2ui_settings, service, brain=FailingBrain())
    surface_id = await service.push_built(decision_sweep(SWEEP_PARAMS))
    nonce = (await _surface_row(db, surface_id)).nonce

    status, payload = await router.handle(
        _action_body(
            "decision.resolve", surface_id, nonce,
            {"decisionId": DECISION_A, "outcome": "success"},
        ),
        content_type=JSON,
    )

    assert status == 422
    assert "could not record" in payload["error"]["message"]
    surface = await _surface_row(db, surface_id)
    assert surface.data_model["decisions"][DECISION_A] == "pending", "no phantom progress"


# ---------------------------------------------------------------------------
# dag.retry / dag.cancel
# ---------------------------------------------------------------------------


async def test_dag_retry_delegates_and_patches_the_banner(
    router: ActionRouter, service, db, orchestrator: FakeOrchestrator
) -> None:
    surface_id = await service.push_built(dag_monitor(DAG_PARAMS))
    nonce = (await _surface_row(db, surface_id)).nonce

    status, payload = await router.handle(
        _action_body("dag.retry", surface_id, nonce, {"node": "analyze"}),
        content_type=JSON,
    )

    assert status == 200
    assert orchestrator.retries == [(DAG_PARAMS["dag_id"], "analyze")]
    surface = await _surface_row(db, surface_id)
    assert surface.status == "live"
    assert "analyze" in surface.data_model["banner"]


async def test_dag_retry_rejects_a_node_not_on_the_surface(
    router: ActionRouter, service, db, orchestrator: FakeOrchestrator
) -> None:
    surface_id = await service.push_built(dag_monitor(DAG_PARAMS))
    nonce = (await _surface_row(db, surface_id)).nonce

    status, payload = await router.handle(
        _action_body("dag.retry", surface_id, nonce, {"node": "ghost"}),
        content_type=JSON,
    )

    assert status == 422
    assert "not on this surface" in payload["error"]["message"]
    assert orchestrator.retries == []


async def test_dag_cancel_delegates_and_resolves(
    router: ActionRouter, service, db, orchestrator: FakeOrchestrator
) -> None:
    surface_id = await service.push_built(dag_monitor(DAG_PARAMS))
    nonce = (await _surface_row(db, surface_id)).nonce

    status, payload = await router.handle(
        _action_body("dag.cancel", surface_id, nonce), content_type=JSON
    )

    assert status == 200
    assert payload["resolved"] is True
    assert orchestrator.cancels == [(DAG_PARAMS["dag_id"], "cancelled via companion")]
    assert (await _surface_row(db, surface_id)).status == "resolved"


# ---------------------------------------------------------------------------
# Submitted-model shape re-validation (spec §10.9)
# ---------------------------------------------------------------------------


def test_submitted_model_accepts_value_changes() -> None:
    auth = {"correction": "", "count": 3, "flag": True, "nested": {"note": "x"}}
    sub = {"correction": "typed text", "count": 7.5, "flag": False, "nested": {"note": "y"}}

    assert _submitted_model_error(auth, sub) is None


def test_submitted_model_rejects_unknown_keys() -> None:
    assert _submitted_model_error({"a": 1}, {"a": 1, "b": 2}) == "unknown key /b"


def test_submitted_model_rejects_primitive_type_flips() -> None:
    assert "expected a number" in _submitted_model_error({"a": 1}, {"a": "one"})
    assert "expected a string" in _submitted_model_error({"a": "x"}, {"a": 3})
    assert "expected a boolean" in _submitted_model_error({"a": True}, {"a": 1})
    assert "expected a number" in _submitted_model_error({"a": 1}, {"a": True})


def test_submitted_model_rejects_nested_shape_breaks() -> None:
    auth = {"outer": {"inner": "x"}}

    assert _submitted_model_error(auth, {"outer": "flat"}) == "expected an object at /outer"
    assert _submitted_model_error(auth, {"outer": {"evil": 1}}) == "unknown key /outer/evil"


def test_submitted_model_treats_lists_as_opaque_values() -> None:
    auth = {"items": [1, 2]}

    assert _submitted_model_error(auth, {"items": [9, 9, 9, 9]}) is None
    assert "expected an array" in _submitted_model_error(auth, {"items": "nope"})


async def test_tampered_submitted_model_is_rejected_and_audited(
    router: ActionRouter, service, db, a2ui_agent_id: str, brain: FakeBrain
) -> None:
    """The wire-level check: a form submit whose model grew an unknown key
    dies at 422 before any handler reads it."""
    surface_id = await service.push_built(decision_sweep(SWEEP_PARAMS))
    nonce = (await _surface_row(db, surface_id)).nonce
    body = _action_body(
        "decision.resolve", surface_id, nonce,
        {"decisionId": DECISION_A, "outcome": "success"},
    )
    body["a2uiRendererDataModel"] = {
        "version": "v1.0",
        "surfaces": {surface_id: {"decisions": {DECISION_A: "pending"}, "evil": "payload"}},
    }

    status, payload = await router.handle(body, content_type=JSON)

    assert status == 422
    assert payload["error"]["code"] == "VALIDATION_FAILED"
    assert "unknown key /evil" in payload["error"]["message"]
    assert brain.reviews == []
    audits = await _audits(db, a2ui_agent_id)
    assert [a.status for a in audits] == ["rejected"]


# ---------------------------------------------------------------------------
# Phase 2 builders
# ---------------------------------------------------------------------------


def test_decision_sweep_validates_and_wires_every_outcome_button() -> None:
    built = decision_sweep(SWEEP_PARAMS)
    built.validate()

    assert built.kind == "decision_sweep"
    assert built.allowed_actions == ["decision.resolve"]
    assert built.data_model["decisions"] == {DECISION_A: "pending", DECISION_B: "pending"}
    contexts = [
        c["action"]["event"]["context"]
        for c in built.components
        if isinstance(c.get("action"), dict)
    ]
    assert {ctx["decisionId"] for ctx in contexts} == {DECISION_A, DECISION_B}
    assert {ctx["outcome"] for ctx in contexts} == {"success", "partial", "failure", "noise"}


def test_decision_sweep_renders_an_empty_state() -> None:
    built = decision_sweep({"decisions": []})
    built.validate()

    assert any(c["id"] == "empty" for c in built.components)


def test_memory_graph_seeds_the_focus_node_and_offers_no_actions() -> None:
    built = memory_graph(GRAPH_PARAMS)
    built.validate()

    assert built.allowed_actions == []
    assert built.data_model["nodes"][0]["id"] == GRAPH_PARAMS["node_id"]
    assert built.data_model["focus"] == GRAPH_PARAMS["node_id"]
    graph = next(c for c in built.components if c["component"] == "MemoryGraph")
    assert graph["nodes"] == {"path": "/nodes"}
    assert graph["focusNodeId"] == GRAPH_PARAMS["node_id"]


def test_dag_monitor_offers_retry_only_when_a_node_failed() -> None:
    built = dag_monitor(DAG_PARAMS)
    built.validate()
    assert set(built.allowed_actions) == {"dag.cancel", "dag.retry"}

    healthy = {**DAG_PARAMS, "nodes": [{"name": "a", "status": "completed"}], "edges": []}
    built_healthy = dag_monitor(healthy)
    built_healthy.validate()
    assert built_healthy.allowed_actions == ["dag.cancel"]


# ---------------------------------------------------------------------------
# push_surface self-sourcing
# ---------------------------------------------------------------------------


class _CapturingService:
    def __init__(self) -> None:
        self.pushed: list[tuple[Any, str | None]] = []

    async def push_built(self, built, dedup_key=None, session_id=None, notify=None):
        self.pushed.append((built, dedup_key))
        return "nous:test:kind:0001"


class _FakeDagStore:
    async def get_dag(self, dag_id):
        return SimpleNamespace(
            id=dag_id,
            name="nightly-audit",
            status="running",
            nodes=[
                SimpleNamespace(id=1, name="collect", status="completed", node_type="subtask"),
                SimpleNamespace(id=2, name="analyze", status="failed", node_type="subtask"),
            ],
            # Parallel edges between the same pair are legal in the store
            # (DAGEdge uniqueness includes edge_type) — the projection must
            # emit the pair ONCE or the renderer's keyed each crashes.
            edges=[
                SimpleNamespace(from_node_id=1, to_node_id=2),
                SimpleNamespace(from_node_id=1, to_node_id=2),
            ],
        )


@pytest.fixture
def dispatcher_env(brain: FakeBrain):
    from nous.a2ui.tools import register_a2ui_tools
    from nous.api.tools import ToolDispatcher

    dispatcher = ToolDispatcher()
    service = _CapturingService()
    register_a2ui_tools(dispatcher, service, brain=brain, dag_store=_FakeDagStore())
    return dispatcher, service


async def test_push_surface_self_sources_the_decision_sweep(dispatcher_env) -> None:
    dispatcher, service = dispatcher_env

    text, is_error = await dispatcher.dispatch(
        "push_surface", {"template": "decision_sweep", "params": {}}
    )

    assert not is_error, text
    built, dedup_key = service.pushed[0]
    assert dedup_key == "sweep:decisions"
    assert built.data_model["decisions"] == {DECISION_A: "pending"}


async def test_push_surface_self_sources_the_dag_monitor(dispatcher_env) -> None:
    dispatcher, service = dispatcher_env
    dag_id = "0e9d8c7b-6a5f-4e3d-8c2b-1a0f9e8d7c6b"

    text, is_error = await dispatcher.dispatch(
        "push_surface", {"template": "dag_monitor", "params": {"dag_id": dag_id}}
    )

    assert not is_error, text
    built, dedup_key = service.pushed[0]
    assert dedup_key == f"dag:{dag_id}"
    assert {n["name"] for n in built.data_model["nodes"]} == {"collect", "analyze"}
    assert built.data_model["edges"] == [
        {"from": "collect", "to": "analyze"}
    ], "parallel store edges project to ONE distinct pair"
    assert "dag.retry" in built.allowed_actions, "the failed node makes retry available"


async def test_push_surface_overwrites_caller_supplied_decisions(dispatcher_env) -> None:
    """Codex P1: the brain is the ONLY source of decision rows. A caller-
    supplied list — fabricated text over a real id — must be discarded, or
    the user's click records an outcome for something they never read."""
    dispatcher, service = dispatcher_env

    _, is_error = await dispatcher.dispatch(
        "push_surface",
        {
            "template": "decision_sweep",
            "params": {
                "decisions": [
                    {
                        "id": DECISION_B,
                        "description": "FABRICATED: totally harmless decision",
                        "confidence": 0.99,
                    }
                ]
            },
        },
    )

    assert not is_error
    built, _ = service.pushed[0]
    assert built.data_model["decisions"] == {DECISION_A: "pending"}, (
        "the surface must carry the brain's rows, not the caller's"
    )
    assert "FABRICATED" not in str(built.components)


async def test_push_surface_overwrites_caller_supplied_dag_nodes(dispatcher_env) -> None:
    """Codex P1: same class as the sweep — a fabricated monitor over a real
    dag_id would let a user click cancel a DAG they never actually saw."""
    dispatcher, service = dispatcher_env
    dag_id = "0e9d8c7b-6a5f-4e3d-8c2b-1a0f9e8d7c6b"

    _, is_error = await dispatcher.dispatch(
        "push_surface",
        {
            "template": "dag_monitor",
            "params": {
                "dag_id": dag_id,
                "name": "FABRICATED-harmless-dag",
                "status": "completed",
                "nodes": [{"name": "fake", "status": "completed"}],
                "edges": [],
            },
        },
    )

    assert not is_error
    built, _ = service.pushed[0]
    assert {n["name"] for n in built.data_model["nodes"]} == {"collect", "analyze"}
    assert "FABRICATED" not in built.title
    assert built.data_model["dag_id"] == dag_id


async def test_push_surface_dag_monitor_requires_a_uuid(dispatcher_env) -> None:
    dispatcher, service = dispatcher_env

    _, is_error = await dispatcher.dispatch(
        "push_surface", {"template": "dag_monitor", "params": {"dag_id": "not-a-uuid"}}
    )

    assert is_error is True
    assert service.pushed == []


async def test_push_surface_decision_sweep_without_brain_errors() -> None:
    from nous.a2ui.tools import register_a2ui_tools
    from nous.api.tools import ToolDispatcher

    dispatcher = ToolDispatcher()
    service = _CapturingService()
    register_a2ui_tools(dispatcher, service)  # no brain, no dag_store

    _, is_error = await dispatcher.dispatch(
        "push_surface", {"template": "decision_sweep", "params": {}}
    )

    assert is_error is True
    assert service.pushed == []
