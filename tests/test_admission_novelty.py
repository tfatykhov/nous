"""Admission novelty when there is nothing to compare against.

``FactManager._find_max_similarity`` feeds admission's novelty term
(``1 - max similarity``). Without an embedding the novelty is unknown and
admission scores it 0.5. With an embedding but no comparable active fact
(an empty agent, or an empty tier-1 pool for a person/preference/rule
input), nothing similar exists and the novelty is 1.0. Scoring that case
0.5 instead locked a new agent out: a low-confidence first fact was
rejected, the agent stayed empty, and the next one was rejected the same way.

Each test runs on an agent of its own, in the rolled-back ``session``.
"""

from __future__ import annotations

import math
from uuid import uuid4

import pytest

from nous.heart.admission import AdmissionConfig, AdmissionController
from nous.heart.schemas import FactInput, FactRejected
from nous.storage.models import Episode, Fact

pytestmark = pytest.mark.postgres_only

DIM = 1536


def _vec(cos_to_e0: float) -> list[float]:
    """A unit vector with cosine ``cos_to_e0`` to e0; the rest lies on e1."""
    v = [0.0] * DIM
    v[0] = cos_to_e0
    v[1] = math.sqrt(max(0.0, 1.0 - cos_to_e0 * cos_to_e0))
    return v


async def _own_heart(db, embedding_provider):
    from nous.config import Settings
    from nous.heart import Heart

    settings = Settings().model_copy(update={"agent_id": f"novelty-{uuid4().hex[:8]}"})
    heart = Heart(db, settings, embedding_provider=embedding_provider)
    heart.facts._admission_controller = AdmissionController(
        config=AdmissionConfig(shadow_mode=False, utility_llm_enabled=False)
    )
    return heart


def _capture_admission(heart, monkeypatch) -> list:
    """Record every AdmissionResult the controller returns."""
    controller = heart.facts._admission_controller
    real_score = controller.score
    results: list = []

    async def _score(*args, **kwargs):
        result = await real_score(*args, **kwargs)
        results.append(result)
        return result

    monkeypatch.setattr(controller, "score", _score)
    return results


async def _episode(session, agent_id: str):
    ep = Episode(agent_id=agent_id, summary="Episode for the admission novelty tests.")
    session.add(ep)
    await session.flush()
    return ep.id


async def test_first_fact_of_an_empty_agent_is_fully_novel_and_admitted(db, session, mock_embeddings, monkeypatch):
    """No other fact exists, so novelty is 1.0. This input (utility 0.55, no
    source text, so confidence 0) is admitted at 1.0 and rejected at 0.5."""
    heart = await _own_heart(db, mock_embeddings)
    try:
        results = _capture_admission(heart, monkeypatch)
        ep_id = await _episode(session, heart.agent_id)
        r = await heart.learn(
            FactInput(
                content="The author of Work Y is Carla First.",
                subject_key="work y",
                attribute_key="author",
                source_episode_id=ep_id,
                source_ordinal=1,
            ),
            session=session,
            precomputed_embedding=_vec(1.0),
        )
        assert len(results) == 1
        assert results[0].scores["novelty"] == 1.0
        assert not isinstance(r, FactRejected), r.explanation
    finally:
        await heart.close()


async def test_no_embedding_keeps_novelty_unknown(db, session, monkeypatch):
    """Without an embedding nothing can be compared, so novelty stays at the
    neutral 0.5, even on an empty agent where an embedded fact would get 1.0."""
    heart = await _own_heart(db, None)
    try:
        results = _capture_admission(heart, monkeypatch)
        await heart.learn(
            FactInput(content="The deploy window for the billing service is Tuesday evenings."),
            session=session,
        )
        assert len(results) == 1
        assert results[0].scores["novelty"] == 0.5
    finally:
        await heart.close()


async def test_first_tier1_fact_beside_only_technical_facts_is_fully_novel(db, session, mock_embeddings, monkeypatch):
    """A person/preference/rule input is compared with tier-1 facts only. With
    none on the agent, novelty is 1.0, even though a technical fact sits at
    cosine 0.9 (which would score 0.1 if it were compared)."""
    heart = await _own_heart(db, mock_embeddings)
    try:
        session.add(
            Fact(
                agent_id=heart.agent_id,
                content="The build server for Work Y runs on the second rack.",
                category="technical",
                active=True,
                embedding=_vec(0.9),
            )
        )
        await session.flush()
        results = _capture_admission(heart, monkeypatch)
        await heart.learn(
            FactInput(content="Carla First is the author of Work Y.", category="person", subject="Carla First"),
            session=session,
            precomputed_embedding=_vec(1.0),
        )
        assert len(results) == 1
        assert results[0].scores["novelty"] == 1.0
    finally:
        await heart.close()
