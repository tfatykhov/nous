# Identity & User-Profile Dashboard UI Implementation Plan (v2 — post 3-agent review)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A new "Identity" view in the dashboard SPA (Svelte v2) that displays and edits (a) the six agent-identity sections stored in `nous_system.agent_identity` and (b) the Tier-1 user-profile facts (`heart.facts` with category preference/person/rule), plus the three REST endpoints the fact side needs.

**Architecture:** Identity editing needs NO backend work — `GET /identity` + `PUT /identity/{section}` exist (rest.py:734/750, versioned via `IdentityManager.update_section`). Facts get three new REST routes wrapping existing Heart primitives: `GET /profile/facts` → `heart.list_facts_by_category`; `PUT /facts/{fact_id}` → target-fact pre-fetch + `heart.supersede_fact` (content edits are supersessions — new versioned row, auto re-embedded; never in-place); `DELETE /facts/{fact_id}` → `heart.deactivate_fact` (soft delete). PUT/DELETE are **scoped to Tier-1 target facts** (devil-P2: a generic any-fact mutator invites category typos and nonsensical edits of bulk-ingested exemplar/enumerative rows). Frontend follows the ONE existing editor idiom (Browser.svelte censor editing).

**Review status:** v1 reviewed by 3 agents 2026-07-24 (correctness / devil / tests) — all APPROVE WITH REVISIONS; every P1/P2 integrated here. The devil's P1 (dedup-swallow, below) was personally re-verified in code by the lead before acceptance.

**Tech Stack:** Starlette (closures in `create_app`, bare-dict JSON envelope), Svelte 5 runes, hand-rolled hash router, vitest + @testing-library/svelte, pytest + httpx ASGITransport + real Postgres.

## The dedup-swallow hazard (devil-P1, verified — shapes the PUT contract)

`Heart.supersede_fact` → `_supersede` → `_learn(bypass_input, [old_fact_id], False, session)` (facts.py:2099). The 2nd arg excludes ONLY the old fact from dedup; the 3rd disables contradiction routing. If the edited content lands ≥ `fact_native_cosine_threshold` similar to a **different** active fact, `_find_duplicate` (facts.py:757-783) surfaces it, band routing short-circuits (check_contradictions gates at facts.py:807/833), and `_learn` returns `_confirm_duplicate(third_fact)` (facts.py:836-838). `_supersede` then deactivates the old fact and links `superseded_by` to the THIRD fact — the user's typed text is never stored. **Prod runs `NOUS_FACT_NATIVE_COSINE_THRESHOLD=0.80`** (verified in .env.prod-snapshot), so sibling profile facts can realistically collide. The heart-side outcome is coherent (old entry retired in favor of the matched near-duplicate) — the fix is HONESTY at the API: the handler compares the returned fact's content to the submitted content and reports `"status": "merged_into_existing"` (with the actually-stored content) instead of a false plain success, and the UI surfaces it. We deliberately do NOT change heart-core dedup semantics (blast radius: sleep-handler + F031/F027 merge callers).

## Key design decisions (locked, revised)

1. **Edit = supersede, delete = deactivate.** After an edit the fact normally gets a NEW id (UI reloads after every save). When the edit dedups into an existing third fact, the API says so (`merged_into_existing` + stored content) — never a silent false success.
2. **PUT/DELETE scoped to Tier-1 targets.** Handler pre-fetches the target fact: 404 if missing, 409 if already superseded (id-chain moved) or if its category ∉ `TIER1_FACT_CATEGORIES`. Body `category` if present must be Tier-1 (400 otherwise), defaults to the target's category. Body `confidence` defaults to the **target's existing confidence** (never a hardcoded 1.0 clobber).
3. **Scope: view + edit + deactivate.** No create-fact button, no identity version-history UI, no `POST /reinitiate` in the UI. Identity saves are versioned in the DB (`previous_version_id` chain) but have **no rollback UI** — mitigated with a `confirm()` on identity Save; stated in the PR body.
4. **Tier-1 categories from the shared constant** `TIER1_FACT_CATEGORIES` (nous/cognitive/context.py:29) — no duplicated list. No circular-import risk (verified: nous.cognitive already imported at rest.py top).
5. **Auth: none exists on the REST API** (pre-existing posture; `PUT /identity/{section}` already open). Stated in PR body; adding auth is out of scope.
6. **`dist/` is gitignored** — never commit build output. `npm test` / `npm run check` / `npm run build` are gates only.
7. **Known edit side-effects (accepted, documented):** each fact edit costs one embedding call, possibly one Haiku F047 actionability call (facts.py:935 — not gated off for supersede), and emits `fact_learned` + `fact_superseded` events. Identity edits propagate to the live prompt within ≤60s (IdentityManager TTL cache); fact edits next turn. Editing the `preferences` identity section ALSO changes which profile facts the prompt shows (PR #569 per-line dedup coupling) — surfaced as a UI note.

## Global Constraints

- Branch `feat/identity-profile-ui` in worktree `E:\Projects\nous-worktrees\identity-profile-ui` (already created off origin/main @ 5bda098; `uv sync --extra dev --extra runtime --extra agent` + `npm ci` done). Subagents MUST `cd` into the worktree and verify `git branch --show-current` prints `feat/identity-profile-ui` before any edit.
- Backend tests: real Postgres (`db` fixture; docker postgres on :5432 is up). **The new test module MUST define LOCAL fixtures with a unique agent id** (tests-P1-2: conftest's `settings`/`heart` use the default shared agent; committed facts persist across the session-scoped db — re-runs would dedup-collide). Recipe in Task 1.
- Frontend checks from `dashboard-app/`: `npm test`, `npm run check`, `npm run build`. **Known baseline failure on clean main (verified by live run 2026-07-24):** `router.test.ts > lists all 15 routes` fails `expected 16 to be 15` (routes were added without updating the hardcoded count). Task 3 fixes it in passing — post-change count is **17**.
- Starlette routes: register `"/facts/{fact_id}"` adjacent to `Route("/facts", search_facts)` (distinct compiled regexes — no shadowing either way; keep path-param first per repo convention note at rest.py:2694).
- `FactInput` is NOT in scope in rest.py (only a local import at rest.py:1857) — new handlers need `from nous.heart import FactInput` (exported, verified nous/heart/__init__.py:18,45). The new test module needs the same import.
- Every new REST endpoint gets a CLAUDE.md REST-table row (Task 5).
- Commit style: `feat:` / `test:` / `docs:`; one logical change per commit.
- Backend full-suite diff vs origin/main baseline at the end (sqlite default; pre-existing failures not yours).

---

### Task 1: Backend — `GET /profile/facts` + isolated test scaffolding

**Files:**
- Modify: `nous/api/rest.py` (handler + route registration in the routes list ~rest.py:2603-2713)
- Test: `tests/test_profile_facts_api.py` (new)

**Interfaces:**
- Consumes: `Heart.list_facts_by_category(categories, active_only=True, limit=20, session=None) -> list[FactSummary]` (heart.py:419, orders `confidence DESC, learned_at DESC`); `TIER1_FACT_CATEGORIES` (context.py:29).
- Produces: `GET /profile/facts?limit=100&active=true` → `{"facts": [FactSummary.model_dump(mode="json")...], "total": int}`; test fixtures `settings`/`heart`/`brain`/`client` (module-local, unique agent) that Task 2 reuses.

- [ ] **Step 1: Create the test module with ISOLATED fixtures + first tests**

`tests/test_profile_facts_api.py`. Structure: copy `tests/test_identity_api.py`'s app/client construction (create_app call at :44-54, ASGITransport client at :57-62, `AsyncMock(spec=AgentRunner)` runner) **but shadow the settings/heart/brain fixtures locally** so everything is scoped to a fresh agent:

```python
"""REST tests for /profile/facts + /facts/{id} edit endpoints (dashboard identity UI)."""
import uuid as _uuid

import pytest
import pytest_asyncio

from nous.heart import FactInput, Heart

_PROFILE_AGENT = f"test-profile-{_uuid.uuid4().hex[:8]}"


@pytest.fixture
def settings(request):
    # Shadow conftest's settings with a unique-agent copy (tests-P1-2: the
    # conftest heart/settings use the shared default agent on a session-scoped
    # Postgres — committed facts persist and re-runs dedup-collide).
    base = request.getfixturevalue("_base_settings") if False else None  # see note below
    from nous.config import Settings
    s = Settings()
    return s.model_copy(update={"agent_id": _PROFILE_AGENT})


@pytest_asyncio.fixture(autouse=True)
async def _ensure_agent(db):
    from sqlalchemy import text
    async with db.session() as session:
        await session.execute(
            text("INSERT INTO nous_system.agents (id, name, config) VALUES (:id, :n, '{}'::jsonb) ON CONFLICT (id) DO NOTHING"),
            {"id": _PROFILE_AGENT, "n": "Profile API Test Agent"},
        )
        await session.commit()


@pytest_asyncio.fixture
async def heart(db, mock_embeddings, settings):
    h = Heart(db, settings, embedding_provider=mock_embeddings)
    yield h
    await h.close()
```

IMPLEMENTER NOTES on the scaffolding:
- Delete the dead `request.getfixturevalue` line above — construct `Settings()` directly (mirror how conftest's settings fixture builds it; if conftest applies test-critical overrides, start from `request.getfixturevalue("settings").model_copy(...)` of the CONFTEST fixture under a different local name instead — read conftest first and pick the variant that preserves its overrides).
- Then replicate `test_identity_api.py`'s remaining fixtures (`brain`, `cognitive`, `identity_manager` if needed by `create_app`'s signature, `app`, `client`) EXACTLY as that file does, except every component that takes settings/agent gets THIS module's `settings` fixture. Read `create_app`'s parameter list (rest.py:65-81) and satisfy it the same way test_identity_api.py does.
- The `_ensure_agent` autouse insert satisfies the agents FK before any `heart.learn`.

First tests:

```python
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
```

- [ ] **Step 2:** `NOUS_TEST_DB=postgres uv run pytest tests/test_profile_facts_api.py -v` → Expected: FAIL — 404 (route absent). (If fixtures error first, fix scaffolding until the failure is the 404.)

- [ ] **Step 3: Implement the handler** (inside `create_app`, near `search_facts`; `TIER1_FACT_CATEGORIES` imported at module top of rest.py alongside the existing nous.cognitive imports):

```python
    async def list_profile_facts(request: Request) -> JSONResponse:
        """GET /profile/facts — Tier-1 user-profile facts (preference/person/rule).

        Same accessor + ordering (confidence DESC, learned_at DESC) as the
        system-prompt User Profile section, so the dashboard shows exactly
        what the agent can draw from.
        """
        try:
            limit = int(request.query_params.get("limit", "100"))
            if limit < 1:
                raise ValueError
        except ValueError:
            return JSONResponse({"error": "invalid limit"}, status_code=400)
        active_only = request.query_params.get("active", "true").lower() != "false"
        try:
            facts = await heart.list_facts_by_category(
                categories=TIER1_FACT_CATEGORIES,
                active_only=active_only,
                limit=limit,
            )
            return JSONResponse(
                {"facts": [f.model_dump(mode="json") for f in facts], "total": len(facts)}
            )
        except Exception as e:
            logger.error("list_profile_facts failed: %s", e)
            return JSONResponse({"error": str(e)}, status_code=500)
```

Register: `Route("/profile/facts", list_profile_facts)`.

- [ ] **Step 4:** Run the test file → PASS.
- [ ] **Step 5:** Commit: `feat: GET /profile/facts — Tier-1 user-profile fact listing for dashboard`

---

### Task 2: Backend — `PUT /facts/{fact_id}` + `DELETE /facts/{fact_id}` (Tier-1-scoped, merge-honest)

**Files:**
- Modify: `nous/api/rest.py` (two handlers + routes)
- Test: `tests/test_profile_facts_api.py` (extend; fixtures from Task 1)

**Interfaces:**
- Consumes: `Heart.get_current_fact(fact_id, session=None) -> FactDetail` (heart.py:429 — follows the superseded_by chain; ValueError if missing); `Heart.supersede_fact(old_id, new_fact, session=None) -> FactDetail` (heart.py:354); `Heart.deactivate_fact(fact_id)` (heart.py:452 — ValueError if missing); `FactInput` (`from nous.heart import FactInput` — NOT already in rest.py scope); `TIER1_FACT_CATEGORIES`; `settings.fact_min_content_chars`.
- Produces:
  - `PUT /facts/{id}` body `{"content": str required, "category"?: str, "subject"?: str|null, "confidence"?: float}` →
    200 `{"status": "superseded", "new_fact_id": uuid}` |
    200 `{"status": "merged_into_existing", "new_fact_id": uuid, "stored_content": str}` (dedup-swallow case — old fact retired, edit text NOT stored) |
    400 (bad id/JSON, empty content, content < min floor, category not Tier-1) |
    404 (no such fact) | 409 (`already_superseded` — chain moved | `not_profile_fact` — target category ∉ Tier-1).
  - `DELETE /facts/{id}` → 200 `{"status": "deactivated"}` | 400 bad id | 404 missing | 409 `not_profile_fact`.

**Verified error contracts (do not re-derive):** missing fact → ValueError (facts.py:2093-2095, 3659-3661) → 404; too-short content → FactRejected → RuntimeError inside supersede (facts.py:685-696, 2100-2101) → MUST be pre-validated to a clean 400; dedup-swallow → supersede returns the third fact's detail (facts.py:836-838 confirm path) → detected by content comparison.

- [ ] **Step 1: Write the tests** (extend the module; all use the Task-1 isolated fixtures):

```python
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
```

And the dedup-swallow contract test — deterministic via a constant-embedding Heart (every distinct content → identical vector → similarity 1.0 ≥ any threshold):

```python
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
        h = Heart(db, settings, embedding_provider=_ConstEmbeddings())
        yield h
        await h.close()

    @pytest.mark.asyncio
    async def test_edit_colliding_with_third_fact_reports_merge(self, db, settings, const_heart):
        # Build an app around const_heart (same create_app wiring as the module's
        # app fixture, heart swapped) — implementer: factor the app construction
        # into a helper `_make_client(heart)` so both fixtures share it.
        client = await _make_client_for_heart(const_heart, db, settings)
        async with db.session() as session:
            a = await const_heart.learn(FactInput(content="Tim prefers coffee brewed strong in the morning", category="preference", subject="merge-a"), session=session)
            await session.commit()
        # NOTE: fact B must be learned with dedup unable to swallow it — with
        # constant embeddings every learn after the first collides. Insert B via
        # raw SQL (copy the INSERT columns from an existing facts-table test) so
        # two distinct active facts exist despite identical vectors.
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
```

IMPLEMENTER NOTES for this class: `_make_client_for_heart` and `_insert_fact_raw` are small module helpers you write (app-construction shared with the main fixtures; raw INSERT needs id/agent_id/content/category/subject/embedding/active/confidence/learned_at — copy column handling from any existing raw-SQL facts test, e.g. grep `INSERT INTO heart.facts` in tests/). If `_ConstEmbeddings` needs more of the provider protocol (check what Heart calls: `embed`, `embed_batch`, attribute names), mirror conftest's mock_embeddings class shape. The assertions are the contract — do NOT weaken them; if the mechanics fight you, report back instead.

- [ ] **Step 2:** Run → red set: the 200-path tests fail (route absent → 404); note the two weak-red 404 assertions pass vacuously (documented inline).

- [ ] **Step 3: Implement:**

```python
    async def update_fact(request: Request) -> JSONResponse:
        """PUT /facts/{fact_id} — edit a Tier-1 fact's content via supersession.

        Nous never edits fact content in place: the old fact is deactivated
        with superseded_by set, the replacement is re-embedded. If the new
        content dedups into a DIFFERENT existing fact (native cosine gate),
        heart confirms that fact instead of storing the edit — we report that
        honestly as merged_into_existing (never a silent false success).
        """
        from uuid import UUID as _UUID

        from nous.heart import FactInput

        try:
            fact_id = _UUID(request.path_params["fact_id"])
        except ValueError:
            return JSONResponse({"error": "invalid fact id"}, status_code=400)
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        content = (body.get("content") or "").strip()
        if not content:
            return JSONResponse({"error": "content is required"}, status_code=400)
        min_chars = settings.fact_min_content_chars
        if min_chars and len(content) < min_chars:
            return JSONResponse(
                {"error": f"content too short (min {min_chars} chars)"}, status_code=400
            )
        body_category = (body.get("category") or "").strip() or None
        if body_category is not None and body_category not in TIER1_FACT_CATEGORIES:
            return JSONResponse(
                {"error": f"category must be one of {TIER1_FACT_CATEGORIES}"}, status_code=400
            )
        try:
            target = await heart.get_current_fact(fact_id)
        except ValueError:
            return JSONResponse({"error": "fact not found"}, status_code=404)
        if str(target.id) != str(fact_id):
            return JSONResponse(
                {"error": "already_superseded", "current_fact_id": str(target.id)},
                status_code=409,
            )
        if target.category not in TIER1_FACT_CATEGORIES:
            return JSONResponse({"error": "not_profile_fact"}, status_code=409)
        new_input = FactInput(
            content=content,
            category=body_category or target.category,
            subject=body.get("subject") if "subject" in body else target.subject,
            confidence=body.get("confidence") if body.get("confidence") is not None else target.confidence,
        )
        try:
            new_fact = await heart.supersede_fact(fact_id, new_input)
        except ValueError:
            return JSONResponse({"error": "fact not found"}, status_code=404)
        except Exception as e:
            logger.error("update_fact failed: %s", e)
            return JSONResponse({"error": str(e)}, status_code=500)
        if (new_fact.content or "").strip() != content:
            return JSONResponse({
                "status": "merged_into_existing",
                "new_fact_id": str(new_fact.id),
                "stored_content": new_fact.content,
            })
        return JSONResponse({"status": "superseded", "new_fact_id": str(new_fact.id)})
```

`delete_fact`: parse UUID (400) → `get_current_fact` (ValueError → 404; category ∉ Tier-1 → 409 `not_profile_fact`; skip the already-superseded check — deactivating the chain head is harmless but if `target.id != fact_id` return 409 `already_superseded` for symmetry) → `heart.deactivate_fact(fact_id)` → `{"status": "deactivated"}`; ValueError → 404; Exception → 500.

IMPLEMENTER NOTE: verify `FactDetail` exposes `.category`/`.subject`/`.confidence`/`.content` (nous/heart/schemas.py — FactDetail) before wiring; if `get_current_fact`'s chain-walk raises something other than ValueError for a fully-missing id, adjust the except to the real type (read `FactManager.get_current`, facts.py:~3505).

Registration:
```python
        Route("/facts/{fact_id}", update_fact, methods=["PUT"]),
        Route("/facts/{fact_id}", delete_fact, methods=["DELETE"]),
        Route("/facts", search_facts),
```

- [ ] **Step 4:** Run the file → PASS. Also `NOUS_TEST_DB=postgres uv run pytest tests/test_rest.py tests/test_identity_api.py -q` → no regressions.
- [ ] **Step 5:** Commit: `feat: PUT/DELETE /facts/{fact_id} — Tier-1-scoped supersede-edit + deactivate with merge-honest contract`

---

### Task 3: Frontend — route, nav, types

**Files:**
- Modify: `dashboard-app/src/lib/router.ts:3-6`, `src/App.svelte` (imports ~4-20, switch ~164-196), `src/lib/ui/Nav.svelte:16-98`, `src/lib/types/api.ts`
- Modify tests: `src/lib/router.test.ts` (currently asserts 15 — ALREADY RED on main, actual is 16; post-change target **17**), `src/lib/ui/Nav.test.ts` (dynamic off ROUTES — verify it stays green)
- Create stub: `src/views/Identity.svelte` (placeholder heading; Task 4 fills it)

**Interfaces — types to add in `types/api.ts`:**

```typescript
export interface IdentityResponse {
  agent_id: string;
  is_initiated: boolean | null;
  sections: Record<string, string>;
}

export interface FactUpdateResponse {
  status: 'superseded' | 'merged_into_existing';
  new_fact_id: string;
  stored_content?: string;
}

export interface ProfileFactsResponse {
  facts: BrowserFact[];  // superset cast — endpoint omits some BrowserFact fields; view reads none of them
  total: number;
}
```

- [ ] **Step 1:** Update `router.test.ts`: fix the stale count to **17** and assert `ROUTES` includes `'identity'` (membership assert beats a bare count). Run `npm test` → router suite red (it was already red at 16≠15; now red for the missing route).
- [ ] **Step 2:** Add `'identity'` to ROUTES; Nav item `{ id: 'identity', label: 'Identity', icon: '<path d="M10 2a4 4 0 100 8 4 4 0 000-8zM3 18a7 7 0 0114 0H3z"/>' }`; App.svelte import + `{:else if $currentRoute === 'identity'}<Identity />` arm; the three types; stub view.
- [ ] **Step 3:** `npm test && npm run check && npm run build` → ALL green (this commit also fixes the pre-existing router.test.ts baseline failure — say so in the commit body).
- [ ] **Step 4:** Commit: `feat: identity route + nav + API types (dashboard); fixes stale route-count test`

---

### Task 4: Frontend — Identity.svelte view

**Files:**
- Rewrite: `dashboard-app/src/views/Identity.svelte`

**Interfaces:**
- Consumes: `apiGet`/`apiSend` (`api.ts` — `apiSend(path, body, method)`; DELETE with `undefined` body verified safe: `JSON.stringify(undefined)` → no body sent); `DataTable` (`columns/rows/rowKey/detail snippet/onrowclick`; NOTE correctness-P3-2: DataTable renders `{row[c.key]}` verbatim — NO formatters; precompute display fields); types from Task 3; endpoints from Tasks 1-2 + `GET /identity` / `PUT /identity/{section}`.

**Panel 1 — "Agent Identity":** six cards for `['character','values','protocols','preferences','boundaries','environment']`; `<textarea rows={8}>` bound to `sectionDrafts[name]` (seeded from GET /identity, '' if absent); Save button gated `confirm('Overwrite the "'+name+'" identity section? There is no undo UI.')` (devil-P3-6) → `apiSend('/identity/'+name, {content, updated_by: 'dashboard'})` → reload identity, status 'saved'. Save disabled while saving / empty / unchanged. Info note under the panel: *"Identity edits reach the agent's prompt within ~60s. Editing Preferences also changes which User Profile facts below are shown to the agent (overlap dedup)."* (devil-P3-5.)

**Panel 2 — "User Profile Facts (Tier-1)":** load `GET /profile/facts?limit=200` (+`&active=false` when the include-inactive checkbox is on). Precompute display rows via `$derived`: `{...f, content_display: truncate(f.content, 120), confidence_display: f.confidence.toFixed(2), active_display: f.active ? 'yes' : 'no'}`; DataTable columns subject / category / content_display / confidence_display / active_display, `rowKey: (r) => r.id`. Detail snippet (stopPropagation wrapper, Browser.svelte:556 idiom): textarea bound to `factDrafts[row.id]`, Save → `apiSend('/facts/'+row.id, {content: draft, subject: row.subject, confidence: row.confidence}, 'PUT')` (no category — backend inherits the target's), Deactivate (red, `confirm()`) → `apiSend('/facts/'+row.id, undefined, 'DELETE')`.

**Save-response handling (devil-P2-4 id churn + merge honesty):** on ANY successful PUT/DELETE: `delete factDrafts[row.id]; delete factStatus[row.id];` collapse the expanded row, then reload the facts list — never leave a row editable against a retired id. If the PUT response `status === 'merged_into_existing'`, show an info status on the panel (not the row — it's about to disappear): *"Your edit matched an existing fact — the old entry was retired and linked to it. Stored: '<stored_content truncated 120>'"*.

Status/error handling per section and per row: the Browser idiom — `$state<Record<string, 'idle'|'saving'|'saved'|'error'>>` + message map; copy the scoped CSS you use (`.btn`, `.btn-sm`, `.save-status` + its `.save-ok`/`.save-err` compounds, `.status-msg`, `.empty` — Browser.svelte:754-880; they are locally scoped, NOT global — correctness-P3-3 name correction).

- [ ] **Step 1:** Implement per the structure above (Svelte 5 runes, plain onMount load, no stores).
- [ ] **Step 2:** `npm test && npm run check && npm run build` → all green.
- [ ] **Step 3:** Manual smoke (REQUIRED): with local Postgres up, run the app (`uv run python -m nous.main` from the worktree; if startup is blocked on missing API keys, set the minimal env it demands or fall back to a vitest render test of Identity.svelte with `fetch` mocked + `httpx` calls against a `create_app` instance for the API side — and SAY SO in the report). Verify: sections load + a save round-trips; facts list loads; an edit round-trips (new id appears, old row gone); deactivate removes a row; the merged_into_existing path is exercised only if it occurs naturally (don't force it in smoke — it's covered by the backend test).
- [ ] **Step 4:** Commit: `feat: Identity dashboard view — identity section editor + Tier-1 profile fact editor`

---

### Task 5: Docs + suites + PR

- [ ] **Step 1:** CLAUDE.md REST table rows:

```markdown
| GET | `/profile/facts` | Tier-1 user-profile facts (preference/person/rule), prompt-order |
| PUT | `/facts/{fact_id}` | Edit a Tier-1 fact via supersession (new versioned fact, re-embedded; reports merged_into_existing on dedup-swallow) |
| DELETE | `/facts/{fact_id}` | Deactivate (soft-delete) a Tier-1 fact |
```

- [ ] **Step 2:** Backend suite diff vs origin/main baseline (`uv run pytest tests/ -q`, sqlite default; new module's Postgres-only tests may add expected sqlite errors — verify the module separately under `NOUS_TEST_DB=postgres`). Frontend `npm test && npm run check && npm run build` — note the suite is now GREENER than baseline (router count fix).
- [ ] **Step 3:** Commit docs: `docs: REST rows for /profile/facts + fact edit endpoints`
- [ ] **Step 4:** Push + PR. PR body MUST include: both panels described; the three endpoints + semantics (edit=supersede with NEW id; **merged_into_existing contract** with the verified dedup-swallow trace and prod's 0.80 threshold; delete=soft; Tier-1 target scoping); side-effect note (per edit: one embedding + possible Haiku F047 call + fact_learned/fact_superseded events); identity saves versioned but NO history/rollback UI (confirm() guard only); identity↔profile-fact prompt coupling note; REST API has no auth (pre-existing posture); `dist/` not committed; test evidence (backend endpoint tests incl. merge-contract test, frontend test/check/build, manual smoke result, and the baseline router-test fix).
- [ ] **Step 5:** Codex review rounds until clean (👍 reaction counts).
