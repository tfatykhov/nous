"""A job lesson is read only on its own job's runs: no general read returns one.

A job lesson is a ``heart.procedures`` row with ``kind='job_lesson'``. These tests pin
that the catalog, search, cosine selection, name lookup, graph neighbours, the resolver,
hubs, spreading activation, the orphan backfill, context selection and the get_procedure
tool all leave it out, while the dashboard browse and the single-row read keep it. Every
assertion has the how-to present where the job lesson is absent.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from job_support import lesson_heart, lesson_input  # noqa: F401
from sqlalchemy import text, update

from nous.api.rest import create_app
from nous.api.tools import create_nous_tools
from nous.brain._entity_config import _ENTITY_CONFIG
from nous.brain.brain import Brain
from nous.brain.schemas import NeighborResult
from nous.brain.spreading_activation import spreading_activation_search
from nous.cognitive.context import ContextEngine
from nous.cognitive.layer import CognitiveLayer
from nous.config import Settings
from nous.heart.procedures import _build_embed_text
from nous.heart.schemas import ProcedureDetail, ProcedureInput, ProcedureSummary
from nous.storage.models import GraphEdge, Procedure

pytestmark = pytest.mark.postgres_only

HEX = uuid4().hex
WORDS = "zebra quartz meridian"
LESSON_NAME = "[job ab12cd34] Report refusal (1f2e3d4c)"


class _NoRunner:
    _conversations: dict = {}


def _edge(agent_id, proc_id, seed, weight):
    return GraphEdge(
        agent_id=agent_id, source_id=proc_id, source_type="procedure", target_id=seed, target_type="decision",
        relation="caused_by", weight=weight, auto_linked=True, extraction_method="deterministic",
    )


def _detail(pid, name, *, kind=None) -> ProcedureDetail:
    return ProcedureDetail(
        id=pid, agent_id="a", name=name, domain=None, description=None, goals=[], core_patterns=[],
        core_tools=[], core_concepts=[], implementation_notes=[], activation_count=0, success_count=0,
        failure_count=0, neutral_count=0, last_activated=None, effectiveness=None, tags=[], active=True,
        created_at=datetime.now(UTC), kind=kind,
    )


@pytest_asyncio.fixture
async def env(lesson_heart, db, session):  # noqa: F811
    agent_id = lesson_heart.agent_id
    brain = Brain(database=db, settings=lesson_heart.settings)
    howto = await lesson_heart.store_procedure(
        ProcedureInput(
            name="Run the report by hand", domain="ops", description=WORDS,
            core_patterns=["run it"], implementation_notes=[WORDS],
        ),
        session,
    )
    lesson = await lesson_heart.store_procedure(lesson_input(HEX, LESSON_NAME, body=WORDS), session)
    (await session.get(Procedure, lesson.id)).embedding = (await session.get(Procedure, howto.id)).embedding
    # A decision id. The heavier edge belongs to the lesson, so a filter applied after the
    # LIMIT would return nothing at limit 1.
    seed = uuid4()
    session.add_all([_edge(agent_id, howto.id, seed, 0.5), _edge(agent_id, lesson.id, seed, 0.95)])
    await session.flush()
    yield SimpleNamespace(
        heart=lesson_heart, brain=brain, agent_id=agent_id, howto=howto, lesson=lesson, seed=seed,
        query=_build_embed_text(
            howto.name, howto.description, howto.core_patterns, howto.goals,
            howto.core_tools, howto.core_concepts, howto.implementation_notes,
        ),
    )
    await brain.close()


async def test_general_reads_never_return_a_job_lesson(env, session):
    h = env.heart
    rows, total = await h.list_procedures(limit=50, session=session)
    assert [p.id for p in rows] == [env.howto.id] and total == 1
    rows, total = await h.list_procedures(limit=50, session=session, include_job_lessons=True)
    assert {p.id for p in rows} == {env.howto.id, env.lesson.id} and total == 2
    assert {p.id for p in await h.search_procedures(WORDS, limit=10, session=session)} == {env.howto.id}
    assert {p.id for p in await h.find_similar_procedures(env.query, limit=10, session=session)} == {env.howto.id}
    assert (await h.get_procedure_by_name(env.howto.name, session)).id == env.howto.id
    assert await h.get_procedure_by_name(env.lesson.name, session) is None
    assert (await h.get_procedure(env.lesson.id, session)).kind == "job_lesson"  # the unfiltered single-row read stays


async def test_a_superseded_job_lesson_name_is_not_reported_superseded(env, session):
    await session.execute(
        update(Procedure).where(Procedure.id.in_([env.howto.id, env.lesson.id])).values(superseded_by=env.howto.id)
    )
    assert await env.heart.is_procedure_name_superseded(env.howto.name, session) is True
    assert await env.heart.is_procedure_name_superseded(env.lesson.name, session) is False


async def test_low_effectiveness_skips_a_job_lesson_because_it_never_gets_outcomes(env, session):
    await session.execute(update(Procedure).where(Procedure.id == env.howto.id).values(failure_count=5))
    low = await env.heart.procedures.get_low_effectiveness(threshold=0.5, min_scored=5, session=session)
    assert [p.id for p in low] == [env.howto.id]


async def test_evolution_candidates_skip_job_lessons(env, session):
    await session.execute(
        update(Procedure).where(Procedure.id.in_([env.howto.id, env.lesson.id]))
        .values(activation_count=20, success_count=0, failure_count=20)
    )
    cands = await env.heart.procedures.get_evolution_candidates(session)
    assert {c.id for c in cands} == {env.howto.id}


async def test_graph_reads_never_return_a_job_lesson(env, session):
    for neighbor_type in ("procedure", None):
        found = await env.brain.neighbors(
            env.seed, node_type="decision", neighbor_type=neighbor_type, limit=1, session=session
        )
        assert [n.id for n in found] == [env.howto.id]
    ids = {"procedure": [env.howto.id, env.lesson.id]}
    assert set(await env.brain._resolve_node_descriptions(session, ids)) == {env.howto.id}
    assert set(await env.brain._resolve_node_descriptions(session, ids, include_strategy_cards=True)) == {env.howto.id}
    session.add_all(
        GraphEdge(
            agent_id=env.agent_id, source_id=env.lesson.id, source_type="procedure", target_id=uuid4(),
            target_type="fact", relation="related_to", weight=0.8, auto_linked=True, extraction_method="heuristic",
        )
        for _ in range(2)
    )
    await session.flush()  # the lesson now has the highest degree of any procedure
    hubs = await env.brain.top_hubs(limit=10, node_type="procedure", session=session)
    assert {h["node_id"]: h["label"] for h in hubs} == {
        str(env.lesson.id): f"[procedure] {env.lesson.id}",
        str(env.howto.id): env.howto.name,
    }
    activated = await spreading_activation_search(
        session, env.agent_id, [(env.seed, "decision", 1.0)], Settings(_env_file=None), limit=20
    )
    assert env.howto.id in {r[0] for r in activated} and env.lesson.id not in {r[0] for r in activated}


async def test_the_orphan_backfill_config_leaves_job_lessons_out(env, session):
    where = _ENTITY_CONFIG["procedure"][3]
    ids = (
        await session.execute(
            text(f"SELECT t.id FROM heart.procedures t WHERE t.agent_id = :a AND {where}"), {"a": env.agent_id}
        )
    ).scalars().all()
    assert set(ids) == {env.howto.id}


async def test_the_get_procedure_tool_treats_a_job_lesson_id_as_not_found(lesson_heart):  # noqa: F811
    howto = await lesson_heart.store_procedure(ProcedureInput(name="Run the report by hand", description=WORDS))
    lesson = await lesson_heart.store_procedure(lesson_input(HEX, LESSON_NAME, body=WORDS))  # committed rows
    tools = create_nous_tools(MagicMock(), lesson_heart, Settings(_env_file=None))
    out = await tools["get_procedure"](str(lesson.id))
    assert out["content"][0]["text"] == f"No procedure found for '{lesson.id}'."
    assert howto.name in (await tools["get_procedure"](str(howto.id)))["content"][0]["text"]


async def test_context_selection_drops_a_job_lesson_the_graph_or_cosine_leg_offers():
    seed, lesson_id, howto_id = uuid4(), uuid4(), uuid4()
    details = {lesson_id: _detail(lesson_id, "lesson", kind="job_lesson"), howto_id: _detail(howto_id, "howto")}
    brain, heart = MagicMock(), MagicMock()
    brain.neighbors = AsyncMock(
        return_value=[
            NeighborResult(
                id=lesson_id, node_type="procedure", description="d", edge_relation="caused_by",
                edge_weight=0.9, created_at=datetime.now(UTC),
            )
        ]
    )
    heart.get_procedure = AsyncMock(side_effect=lambda pid, **_: details[pid])
    heart.find_similar_procedures = AsyncMock(
        return_value=[
            ProcedureSummary(id=i, name="n", domain=None, activation_count=0, effectiveness=None, score=0.9)
            for i in (lesson_id, howto_id)
        ]
    )
    engine = ContextEngine(
        brain, heart, Settings(_env_file=None, relevance_floor_enabled=False, proc_catalog_enabled=False),
        identity_prompt="Test",
    )
    selected = await engine._select_procedures(
        slots=3, critic_skills=[], recalled_ids={"decision": [str(seed)]},
        recalled_score_map={str(seed): 0.9}, session=None, query="report",
    )
    assert [d.id for d in selected] == [howto_id]  # the graph rung offered only the lesson, the cosine rung both


async def test_the_dashboard_browse_still_lists_job_lessons(lesson_heart, db):  # noqa: F811
    howto = await lesson_heart.store_procedure(ProcedureInput(name="Run the report by hand"))  # committed rows
    lesson = await lesson_heart.store_procedure(lesson_input(HEX, LESSON_NAME))
    brain = Brain(database=db, settings=lesson_heart.settings)
    cognitive = CognitiveLayer(brain, lesson_heart, lesson_heart.settings, identity_prompt="Test")
    app = create_app(_NoRunner(), brain, lesson_heart, cognitive, db, lesson_heart.settings)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/procedures?limit=50&offset=0")
    finally:
        await brain.close()
    data = resp.json()
    assert resp.status_code == 200
    assert {p["name"] for p in data["procedures"]} == {howto.name, lesson.name} and data["total"] == 2
