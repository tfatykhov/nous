"""REST tests for /profile/facts + /facts/{id} edit endpoints (dashboard identity UI)."""
import uuid as _uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from nous.heart import FactInput, Heart

# Codex r2: the fixtures use Postgres-specific SQL (nous_system schema, ::jsonb,
# vector casts, advisory locks) — skip cleanly under the default sqlite backend
# instead of erroring (conftest skips postgres_only when NOUS_TEST_DB != postgres).
pytestmark = pytest.mark.postgres_only

_PROFILE_AGENT = f"test-profile-{_uuid.uuid4().hex[:8]}"


# ---------------------------------------------------------------------------
# Isolated fixtures — unique agent id so committed facts on the session-scoped
# Postgres don't persist/dedup-collide across re-runs (tests-P1-2).
# ---------------------------------------------------------------------------


@pytest.fixture
def settings():
    # conftest's settings fixture is a bare Settings() with no test-critical
    # overrides, so construct directly and pin a unique agent id.
    from nous.config import Settings

    return Settings().model_copy(update={"agent_id": _PROFILE_AGENT})


@pytest_asyncio.fixture(autouse=True)
async def _ensure_agent(db):
    from sqlalchemy import text

    async with db.session() as session:
        await session.execute(
            text(
                "INSERT INTO nous_system.agents (id, name, config) "
                "VALUES (:id, :n, '{}'::jsonb) ON CONFLICT (id) DO NOTHING"
            ),
            {"id": _PROFILE_AGENT, "n": "Profile API Test Agent"},
        )
        await session.commit()


@pytest_asyncio.fixture
async def heart(db, mock_embeddings, settings):
    h = Heart(db, settings, embedding_provider=mock_embeddings)
    yield h
    await h.close()


@pytest_asyncio.fixture
async def brain(db, settings):
    from nous.brain.brain import Brain

    b = Brain(database=db, settings=settings)
    yield b
    await b.close()


@pytest_asyncio.fixture
async def cognitive(brain, heart, settings):
    from nous.cognitive import CognitiveLayer

    return CognitiveLayer(brain, heart, settings, identity_prompt="You are Nous.")


@pytest_asyncio.fixture
async def identity_manager(db, settings):
    from nous.identity.manager import IdentityManager

    return IdentityManager(db, settings.agent_id)


def _build_app(brain, heart, cognitive, db, settings, identity_manager):
    from unittest.mock import AsyncMock

    from nous.api.rest import create_app
    from nous.api.runner import AgentRunner

    mock_runner = AsyncMock(spec=AgentRunner)
    return create_app(
        mock_runner, brain, heart, cognitive, db, settings,
        identity_manager=identity_manager,
    )


@pytest.fixture
def app(brain, heart, cognitive, db, settings, identity_manager):
    return _build_app(brain, heart, cognitive, db, settings, identity_manager)


@pytest_asyncio.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _make_client_for_heart(heart, db, settings):
    """Build an AsyncClient over a create_app wired to the given heart."""
    from nous.brain.brain import Brain
    from nous.cognitive import CognitiveLayer
    from nous.identity.manager import IdentityManager

    brain = Brain(database=db, settings=settings)
    cognitive = CognitiveLayer(brain, heart, settings, identity_prompt="You are Nous.")
    identity_manager = IdentityManager(db, settings.agent_id)
    app = _build_app(brain, heart, cognitive, db, settings, identity_manager)
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


async def _insert_fact_raw(db, agent_id, content, category, subject):
    """Insert an active fact directly (bypassing dedup) with the const embedding
    [1.0, 0.0, ...] so it collides under a constant-embedding Heart."""
    from sqlalchemy import text

    fid = _uuid.uuid4()
    emb = "[" + ",".join(["1.0"] + ["0.0"] * 1535) + "]"
    async with db.session() as session:
        await session.execute(
            text(
                "INSERT INTO heart.facts (id, agent_id, content, category, subject, "
                "confidence, embedding, active, learned_at) "
                "VALUES (:id, :aid, :content, :cat, :subj, 1.0, CAST(:emb AS vector), true, NOW())"
            ),
            {"id": fid, "aid": agent_id, "content": content, "cat": category, "subj": subject, "emb": emb},
        )
        await session.commit()
    return fid


# ---------------------------------------------------------------------------
# GET /profile/facts (Task 1)
# ---------------------------------------------------------------------------


class TestProfileFactsEndpoint:
    @pytest.mark.asyncio
    async def test_returns_tier1_categories_only(self, client, heart, db):
        async with db.session() as session:
            await heart.learn(FactInput(content="Tim prefers Celsius for temperature readings", category="preference", subject="Tim"), session=session)
            await heart.learn(FactInput(content="Tim lives in Silver Spring Maryland United States", category="person", subject="Tim"), session=session)
            await heart.learn(FactInput(content="Postgres seventeen with pgvector extension is the datastore", category="technical", subject="stack"), session=session)
            await session.commit()
        resp = await client.get("/profile/facts")
        assert resp.status_code == 200
        data = resp.json()
        cats = {f["category"] for f in data["facts"]}
        assert cats == {"preference", "person"}  # exact — agent is module-unique
        assert data["total"] == len(data["facts"]) == 2

    @pytest.mark.asyncio
    async def test_limit_active_and_validation(self, client, heart, db):
        resp = await client.get("/profile/facts?limit=1")
        assert resp.status_code == 200
        assert len(resp.json()["facts"]) <= 1
        # include-inactive path (tests-P3 coverage)
        resp = await client.get("/profile/facts?active=false")
        assert resp.status_code == 200
        resp = await client.get("/profile/facts?limit=notanumber")
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# PUT / DELETE /facts/{id} (Task 2)
# ---------------------------------------------------------------------------


class TestFactEditEndpoints:
    @pytest.mark.asyncio
    async def test_put_supersedes_and_new_content_listed(self, client, heart, db):
        async with db.session() as session:
            old = await heart.learn(FactInput(content="Tim prefers Fahrenheit for all temperature readings", category="preference", subject="Tim-temp", confidence=0.8), session=session)
            await session.commit()
        resp = await client.put(f"/facts/{old.id}", json={
            "content": "Tim prefers Celsius for all temperature readings",
            "subject": "Tim-temp",
        })
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "superseded"
        assert body["new_fact_id"] != str(old.id)
        listing = (await client.get("/profile/facts")).json()
        by_id = {f["id"]: f for f in listing["facts"]}
        new = by_id[body["new_fact_id"]]
        assert new["content"] == "Tim prefers Celsius for all temperature readings"
        assert new["category"] == "preference"      # inherited from target (no body category)
        assert abs(new["confidence"] - 0.8) < 1e-6  # preserved, NOT clobbered to 1.0
        assert str(old.id) not in by_id             # old is inactive

    @pytest.mark.asyncio
    async def test_put_validation_and_scope(self, client, heart, db):
        import uuid as _u
        assert (await client.put("/facts/not-a-uuid", json={"content": "x" * 40})).status_code == 400
        assert (await client.put(f"/facts/{_u.uuid4()}", json={})).status_code == 400  # no content
        # unknown fact -> 404 (NOTE: weak-red — a missing route is also 404; the
        # red signal for this task is the 200-path test above)
        assert (await client.put(f"/facts/{_u.uuid4()}", json={"content": "Valid replacement content over thirty characters"})).status_code == 404
        # bad body category -> 400
        async with db.session() as session:
            f = await heart.learn(FactInput(content="Tim reviews all plans before implementation begins", category="rule", subject="scope-r"), session=session)
            await session.commit()
        assert (await client.put(f"/facts/{f.id}", json={"content": "Valid replacement content over thirty characters", "category": "technical"})).status_code == 400
        # non-Tier-1 TARGET -> 409
        async with db.session() as session:
            t = await heart.learn(FactInput(content="Postgres runs with the pgvector extension enabled always", category="technical", subject="scope-t"), session=session)
            await session.commit()
        assert (await client.put(f"/facts/{t.id}", json={"content": "Valid replacement content over thirty characters"})).status_code == 409

    @pytest.mark.asyncio
    async def test_put_too_short_content_400_not_500(self, client, heart, db, settings):
        floor = settings.fact_min_content_chars
        if not floor:
            pytest.skip("min-content floor disabled in this environment")
        async with db.session() as session:
            old = await heart.learn(FactInput(content="Tim always reviews plans before implementation starts", category="rule", subject="Tim-rule"), session=session)
            await session.commit()
        resp = await client.put(f"/facts/{old.id}", json={"content": "x" * (floor - 1)})
        assert resp.status_code == 400
        assert "error" in resp.json()

    @pytest.mark.asyncio
    async def test_delete_deactivates_and_scope(self, client, heart, db):
        import uuid as _u
        async with db.session() as session:
            f = await heart.learn(FactInput(content="Tim enjoys weekend hiking in national parks nearby", category="person", subject="Tim-hike"), session=session)
            t = await heart.learn(FactInput(content="The build pipeline uses uv for dependency management", category="technical", subject="scope-d"), session=session)
            await session.commit()
        assert (await client.delete("/facts/not-a-uuid")).status_code == 400
        assert (await client.delete(f"/facts/{_u.uuid4()}")).status_code == 404
        assert (await client.delete(f"/facts/{t.id}")).status_code == 409  # non-Tier-1 target
        resp = await client.delete(f"/facts/{f.id}")
        assert resp.status_code == 200
        listing = (await client.get("/profile/facts")).json()
        assert str(f.id) not in [x["id"] for x in listing["facts"]]


class TestMergedIntoExisting:
    """devil-P1: an edit whose content dedups to a THIRD active fact must be
    reported honestly, never as a plain success."""

    @pytest_asyncio.fixture
    async def const_heart(self, db, settings):
        class _ConstEmbeddings:
            dimensions = 1536

            async def embed(self, text: str):
                return [1.0] + [0.0] * 1535

            async def embed_batch(self, texts):
                return [[1.0] + [0.0] * 1535 for _ in texts]

            async def close(self):
                pass

        h = Heart(db, settings, embedding_provider=_ConstEmbeddings())
        yield h
        await h.close()

    @pytest.mark.asyncio
    async def test_edit_colliding_with_third_fact_reports_merge(self, db, settings, const_heart):
        client = await _make_client_for_heart(const_heart, db, settings)
        async with db.session() as session:
            a = await const_heart.learn(FactInput(content="Tim prefers coffee brewed strong in the morning", category="preference", subject="merge-a"), session=session)
            await session.commit()
        # With constant embeddings every learn after the first collides. Insert B
        # via raw SQL so two distinct active facts exist despite identical vectors.
        b_id = await _insert_fact_raw(db, settings.agent_id, "Tim drinks tea in the afternoon most days", "preference", "merge-b")
        resp = await client.put(f"/facts/{a.id}", json={"content": "Completely different edited wording goes right here"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "merged_into_existing"
        assert body["new_fact_id"] == str(b_id)
        assert body["stored_content"] == "Tim drinks tea in the afternoon most days"
        # old fact retired either way
        listing = (await client.get("/profile/facts")).json()
        assert str(a.id) not in [x["id"] for x in listing["facts"]]


class TestAlreadySuperseded:
    """rev2-branch P2-1: the 409 already_superseded branch (stale-tab double
    submit on a retired id) is part of the public contract — pin it."""

    @pytest.mark.asyncio
    async def test_put_and_delete_on_superseded_fact_409(self, client, heart, db):
        async with db.session() as session:
            a = await heart.learn(
                FactInput(
                    content="Tim schedules deep work in the early morning hours",
                    category="preference",
                    subject="chain-a",
                ),
                session=session,
            )
            await session.commit()
        first = await client.put(
            f"/facts/{a.id}",
            json={"content": "Tim schedules deep work in the late evening hours"},
        )
        assert first.status_code == 200
        new_id = first.json()["new_fact_id"]
        # Stale-tab double submit against the retired id: PUT then DELETE.
        resp = await client.put(
            f"/facts/{a.id}",
            json={"content": "Another perfectly valid replacement content string here"},
        )
        assert resp.status_code == 409
        assert resp.json()["error"] == "already_superseded"
        assert resp.json()["current_fact_id"] == new_id
        resp = await client.delete(f"/facts/{a.id}")
        assert resp.status_code == 409
        assert resp.json()["error"] == "already_superseded"
        assert resp.json()["current_fact_id"] == new_id


class TestCodexRound1Contracts:
    """Codex r1 P2s: exact-duplicate edits must report merged_into_existing
    (content equality masks the dedup-confirm), and concurrent stale edits on
    the same fact must serialize (advisory xact lock) so the loser gets the
    advertised 409 instead of double-superseding."""

    @pytest.mark.asyncio
    async def test_exact_duplicate_edit_reports_merge(self, client, heart, db):
        # Hash-seeded mock embeddings: identical content -> identical vector ->
        # cosine 1.0 >= threshold, so the dedup-confirm path fires
        # deterministically without a constant-embedding stub.
        dupe_content = "Tim keeps his calendar blocked for focus time on Fridays"
        async with db.session() as session:
            a = await heart.learn(
                FactInput(content="Tim prefers asynchronous communication over meetings", category="preference", subject="codex-a"),
                session=session,
            )
            b = await heart.learn(
                FactInput(content=dupe_content, category="preference", subject="codex-b"),
                session=session,
            )
            await session.commit()
        resp = await client.put(f"/facts/{a.id}", json={"content": dupe_content})
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "merged_into_existing"
        assert body["new_fact_id"] == str(b.id)
        assert body["stored_content"] == dupe_content
        listing = (await client.get("/profile/facts")).json()
        assert str(a.id) not in [x["id"] for x in listing["facts"]]

    @pytest.mark.asyncio
    async def test_concurrent_edits_serialize_one_wins(self, client, heart, db):
        import asyncio

        async with db.session() as session:
            a = await heart.learn(
                FactInput(content="Tim writes project notes in markdown files locally", category="preference", subject="codex-race"),
                session=session,
            )
            await session.commit()
        r1, r2 = await asyncio.gather(
            client.put(f"/facts/{a.id}", json={"content": "Tim writes project notes in Obsidian vaults nowadays"}),
            client.put(f"/facts/{a.id}", json={"content": "Tim writes project notes in Notion databases nowadays"}),
        )
        codes = sorted([r1.status_code, r2.status_code])
        assert codes == [200, 409], f"expected one winner + one stale 409, got {codes}"
        loser = r1 if r1.status_code == 409 else r2
        assert loser.json()["error"] == "already_superseded"
