# F053 Episode-Lifecycle Prune Fix + Edge Restore Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop F053's dead-edge prune from erasing the episode graph layer (it treats every normally-closed episode as a dead node), make F040 able to heal episode edges, and restore the deterministically-reconstructable edges that were wrongly deleted.

**Architecture:** `heart.episodes.active` is semantically overloaded: on facts/procedures `active=false` means soft-deleted, but on episodes it is the normal *closed* lifecycle state (008.3 — `Episode._end()` flips it on every session close). The HT-1 audit (2026-06-09) hit this trap in episode search and fixed only search. F053's prune (designed for F031/F027-deactivated *facts*) inherited `active=false` as its episode dead-node predicate and has been deleting the edges of every closed episode nightly; F040's backfill (`t.active = true`) then can never rebuild them. Prod audit 2026-07-12 (FORGE b8578ed9/2a5fd57a): 657 closed episodes hold **6 edges total**; chunk→episode `part_of` is down to 3/3,060; the orphan-gate ratchet makes the loss permanent. The fix: (1) centralize episode-liveness SQL predicates in `graph_constants.py` (the existing home for cross-consumer graph rules); (2) F053 prunes only *genuinely deleted* episodes (trivial discards + abandoned); (3) F040 orphan-eligibility and candidate queries use the liveness predicate so closed episodes heal; (4) a `GraphDensifier.restore_episode_anchor_edges()` method + thin CLI script restores the two deterministic edge classes (`part_of`, `extracted_from`) directly from FK ground truth, and the script can optionally drain the newly-eligible episode orphans immediately instead of waiting ~22 nightly cycles.

**Tech Stack:** Python 3.12+, SQLAlchemy 2.0 async (`text()` SQL), PostgreSQL 17, pytest + pytest-asyncio (mock tests + `NOUS_TEST_DB=postgres` integration tests).

## Global Constraints

- No new feature flags. The prune-predicate change is a correctness fix to an existing default-ON feature (`dead_edge_pruning_enabled` remains the kill-switch). The restore is operator-run (script), which is its own gate.
- No schema change, no migration. Predicate-only fixes + INSERT-only restore.
- All new SQL must be agent-scoped (`agent_id = :agent_id`) on both the entity side and the edges side.
- Preserve F053's existing invariants: `supersedes` lineage exclusion, `LIMIT :max_per_cycle` bound, commit-only-on-success, `dead_edges_prune_error` stat on failure.
- Restored edges must be byte-compatible with what the original writers produce: `part_of` = weight 1.0 / `extraction_method='deterministic'` (mirrors `graph_densifier.py:578-588`), `extracted_from` = fact→episode direction, weight 1.0, `deterministic` (mirrors `graph_linker.py:393-408` `link_episode_deterministic`).
- Idempotency everywhere: `ON CONFLICT (source_id, target_id, relation) DO NOTHING`.
- Tests follow existing conventions: mock-based control-flow tests + `@pytest.mark.integration @pytest.mark.postgres_only` classes with commit-then-cleanup fixtures and unique `agent_id` per test (see `tests/test_f053_dead_edge_prune.py::TestF053Integration`).
- Work on branch `fix/f053-episode-lifecycle-prune` in a worktree; conventional commits (`fix:`, `test:`, `docs:`).

## Definitions used throughout

Episode liveness (mirrors the HT-1 search predicate at `nous/heart/episodes.py:530-533`):

- **LIVE** episode: `(active = true OR ended_at IS NOT NULL) AND outcome IS DISTINCT FROM 'abandoned'` — ongoing sessions and normally-closed episodes.
- **DEAD** episode: `(active = false AND ended_at IS NULL) OR outcome = 'abandoned'` — trivial discards (`deactivate_episode`: `active=false`, never ended) and F060.2 abandoned marks. NOTE (review F3): F060.2 sets `ended_at = COALESCE(ended_at, now())` (`sleep_handler.py:2047`), so **prod abandoned rows have `ended_at` SET** — they are classified DEAD via the `outcome='abandoned'` branch, not the trivial-discard branch. Test fixtures must use this prod shape (`active=false, ended_at=now, outcome='abandoned'`) so the abandoned branch is exercised behaviorally.

These are boolean complements for row selection under SQL three-valued logic for all rows with non-NULL `active` (a NULL `outcome` row is LIVE-selectable and never DEAD-selectable). Caveat (review F6): `episodes.active` is nullable (`models.py:463`, server_default `'true'`); a hypothetical `active IS NULL` row is selected by neither predicate — fail-safe (never pruned, merely invisible to backfill), and no writer produces it.

**Writer census backing the predicate (review-verified, no data-loss path):** `active=false` is written at exactly three sites — `Episode._end()` (`episodes.py:230-235`, always sets `ended_at` + `outcome`; only caller passes `outcome="success"`), `deactivate()` (`episodes.py:597`, only the trivial-discard path calls it), and F060.2 mark-abandoned (`sleep_handler.py:2043-2066`). Abandoned is terminal: F060 recovery selects `active = true` only (`sleep_handler.py:1986`), so no abandoned-then-recovered window exists. No writer flips a closed episode to trivial/abandoned, so kept edges never need retro-pruning.

## Prod rollout (documented here, executed by operator after merge+deploy)

1. Deploy `main` (prune stops deleting closed-episode edges immediately).
2. **Pin `NOUS_SPREADING_ACTIVATION_ENABLED=false` before restoring** (review P2): the restore adds ~5,000 edges that COUNT toward the spreading-activation density gate (`spreading_activation.py:43-60` counts `part_of`/`extracted_from`/`discussed_in` — none are in the exclusion set). Prod density is 2.745 vs the 3.0 auto-enable threshold; the restore will likely flip auto-spreading ON, and the 2026-06-29 A/B measured spreading as −3.3pp MRR / −7.8pp recall on prod. Pinning off is the standing recommendation from that finding regardless of this work.
3. `uv run python scripts/backfill_f053_episode_edges.py --agent-id nous-default --dry-run --densify` — expect ~3,057 `part_of` + up to ~1,390 `extracted_from` + `discussed_in` candidates, and the orphan-eligible episode count.
4. Run with `--densify` (recommended): the script drains cosine healing FIRST, then restores deterministic anchors — see ORDERING note in Task 6; the anchors de-orphan episodes, so running the drain after (or skipping it and relying on the nightly cycle) would forfeit cosine `episode↔episode` healing for the anchored population. Without `--densify` the anchors still land but semantic episode edges are foregone for historical episodes.
5. Rollback key: restored rows are `relation IN ('part_of','extracted_from','discussed_in') AND extraction_method='deterministic'` with `created_at >=` the run timestamp; the run prints its start time for this purpose. `--densify` edges are `inferred` episode-incident edges after the same timestamp.

Ranking-risk note (for the reviewer): restored edges feed the adjacency boost (ON in prod) and Path A expansion. Assessed low-risk for ranking — the 2026-06-22 live `co_occurred` backfill (425 edges) had no regression and edge-layer reweighting has a 4-way measured null — but this restore is ~10× that volume, hence the density pin in step 2 (the one auto-behavior it can flip). F044 note: the deterministic anchors are exempt from the tinyhippo α-downscale, but cosine edges built by `--densify`/nightly F040 are `inferred` and will decay if prod still runs LITE+DOWNSCALE ON (config drift noted 2026-06-29); edge existence, not weight, is what retrieval consumes, so this is acceptable.

---

### Task 1: Episode-liveness predicates in `graph_constants.py`

**Files:**
- Modify: `nous/brain/graph_constants.py` (append after `autobehavior_exclusion_sql`, line 55)
- Test: `tests/test_graph_constants_episode_liveness.py` (create)

**Interfaces:**
- Produces: `episode_dead_sql(col_prefix: str = "") -> str` and `episode_live_sql(col_prefix: str = "") -> str`, both importable as `from nous.brain.graph_constants import episode_dead_sql, episode_live_sql`. Tasks 2–5 consume these.

- [ ] **Step 1: Write the failing test**

Create `tests/test_graph_constants_episode_liveness.py`:

```python
"""Episode-liveness SQL predicates (2026-07-12 F053 audit).

`heart.episodes.active` is overloaded: `active=false` is the normal CLOSED
lifecycle state (008.3), not a deletion marker. These predicates are the
single source of truth for which episodes count as dead nodes in graph
consumers. Unit tests pin the SQL text; the integration tests in
test_f053_dead_edge_prune.py / test_graph_densifier.py verify behavior
against real Postgres.
"""

from __future__ import annotations

from nous.brain.graph_constants import episode_dead_sql, episode_live_sql


class TestEpisodeLivenessSql:
    def test_dead_sql_selects_trivial_discards_and_abandoned_only(self):
        sql = episode_dead_sql()
        assert "active = false AND ended_at IS NULL" in sql
        assert "outcome = 'abandoned'" in sql

    def test_dead_sql_does_not_treat_bare_inactive_as_dead(self):
        """The bug under fix: bare `active = false` must NOT appear as a
        standalone dead-condition — it must always be conjoined with
        `ended_at IS NULL`."""
        sql = episode_dead_sql()
        # Every occurrence of `active = false` is followed by the
        # ended_at conjunction.
        for fragment in sql.split("active = false")[1:]:
            assert fragment.lstrip().startswith("AND"), sql

    def test_live_sql_mirrors_ht1_search_predicate(self):
        sql = episode_live_sql()
        assert "active = true OR" in sql
        assert "ended_at IS NOT NULL" in sql
        assert "outcome IS DISTINCT FROM 'abandoned'" in sql

    def test_col_prefix_is_applied_to_every_column(self):
        sql = episode_dead_sql("ep.")
        assert "ep.active" in sql and "ep.ended_at" in sql and "ep.outcome" in sql
        assert " active" not in sql.replace("ep.active", "")
        sql_live = episode_live_sql("t.")
        assert "t.active" in sql_live and "t.ended_at" in sql_live and "t.outcome" in sql_live
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_graph_constants_episode_liveness.py -v`
Expected: FAIL with `ImportError: cannot import name 'episode_dead_sql'`

- [ ] **Step 3: Write minimal implementation**

Append to `nous/brain/graph_constants.py`:

```python
# --- episode lifecycle vs deletion (2026-07-12 F053 audit) ---
# `heart.episodes.active` is OVERLOADED: on facts/procedures `active=false`
# means soft-deleted, but on episodes it is the normal CLOSED lifecycle
# state (008.3 — `Episode._end()` sets it on every session close). Graph
# consumers that treat `active=false` as "dead node" erase the episode
# graph layer (prod 2026-07-12: 657 closed episodes held 6 edges). HT-1
# hit the same trap in episode search; these fragments mirror its fixed
# predicate (heart/episodes.py::search). Genuinely-deleted episodes are
# only: trivial discards (deactivated without ever ending) and F060.2
# abandoned marks.


def episode_dead_sql(col_prefix: str = "") -> str:
    """SQL boolean fragment selecting genuinely-deleted episodes only."""
    p = col_prefix
    return (
        f"(({p}active = false AND {p}ended_at IS NULL) "
        f"OR {p}outcome = 'abandoned')"
    )


def episode_live_sql(col_prefix: str = "") -> str:
    """Complement of :func:`episode_dead_sql` for row selection: ongoing
    or genuinely-closed episodes, excluding abandoned."""
    p = col_prefix
    return (
        f"(({p}active = true OR {p}ended_at IS NOT NULL) "
        f"AND {p}outcome IS DISTINCT FROM 'abandoned')"
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_graph_constants_episode_liveness.py -v`
Expected: 4 PASS

- [ ] **Step 5: Commit**

```bash
git add nous/brain/graph_constants.py tests/test_graph_constants_episode_liveness.py
git commit -m "fix(graph): centralize episode-liveness SQL predicates (active=false is closed, not deleted)"
```

---

### Task 2: F053 prune — only genuinely-deleted episodes are dead nodes

**Files:**
- Modify: `nous/handlers/sleep_handler.py:1786-1799` (the `inactive_nodes` CTE inside `_phase_prune_dead_edges`)
- Test: `tests/test_f053_dead_edge_prune.py` (extend)

**Interfaces:**
- Consumes: `episode_dead_sql` from Task 1.
- Produces: nothing new — same phase signature, changed SQL semantics.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_f053_dead_edge_prune.py`, inside `class TestF053DeadEdgePrune`:

```python
    @pytest.mark.asyncio
    async def test_sql_does_not_treat_closed_episodes_as_dead(self):
        """2026-07-12 audit: episodes.active=false is the normal CLOSED
        lifecycle state (008.3), not deletion. The old predicate erased the
        entire episode graph layer (657 closed prod episodes held 6 edges).
        The episode branch must select only trivial discards
        (active=false AND ended_at IS NULL) and abandoned marks."""
        handler, mock_session = _make_handler()
        await handler._phase_prune_dead_edges({})
        sql_str = str(mock_session.execute.await_args.args[0])
        assert "ended_at IS NULL" in sql_str, (
            "episode dead-node branch must require ended_at IS NULL "
            "alongside active=false (trivial-discard shape)"
        )
        assert "outcome = 'abandoned'" in sql_str, (
            "episode dead-node branch must include F060.2 abandoned marks"
        )
```

And add to `class TestF053Integration` (same file), a sibling of
`test_phase_deletes_only_inactive_endpoint_edges`:

```python
    @pytest.mark.asyncio
    async def test_closed_episode_edges_survive_prune(
        self, db, mock_embeddings,
    ):
        """Fixture: one normally-closed episode (active=false,
        ended_at set, outcome='success'), one trivial-discard episode
        (active=false, ended_at NULL), one abandoned episode
        (outcome='abandoned'), each with one incident edge from an active
        fact. Run the phase. Only the trivial + abandoned episodes' edges
        are deleted; the closed episode's edge survives."""
        from datetime import UTC, datetime
        from unittest.mock import AsyncMock, MagicMock
        from uuid import uuid4

        from sqlalchemy import text as sql_text

        from nous.config import Settings
        from nous.events import EventBus
        from nous.handlers.sleep_handler import SleepHandler
        from nous.heart import Heart

        agent_id = f"f053-ep-{uuid4().hex[:8]}"
        now = datetime.now(UTC)

        fact_id = uuid4()
        ep_closed = uuid4()
        ep_trivial = uuid4()
        ep_abandoned = uuid4()
        e_closed = uuid4()      # SURVIVES
        e_trivial = uuid4()     # DELETED
        e_abandoned = uuid4()   # DELETED

        try:
            async with db.session() as fs:
                await fs.execute(sql_text(
                    "INSERT INTO nous_system.agents (id, name) "
                    "VALUES (:aid, :name) ON CONFLICT (id) DO NOTHING"
                ), {"aid": agent_id, "name": "F053 episode IT agent"})
                await fs.execute(sql_text(
                    "INSERT INTO heart.facts (id, agent_id, content, active) "
                    "VALUES (:id, :aid, 'anchor fact', true)"
                ), {"id": fact_id, "aid": agent_id})
                episodes = [
                    # (id, active, ended_at, outcome)
                    (ep_closed, False, now, "success"),
                    (ep_trivial, False, None, None),
                    # Prod shape (F060.2 sets ended_at=COALESCE(ended_at, now())):
                    # ended_at SET — dead only via the outcome='abandoned' branch.
                    (ep_abandoned, False, now, "abandoned"),
                ]
                for eid, active, ended, outcome in episodes:
                    await fs.execute(sql_text(
                        "INSERT INTO heart.episodes "
                        "(id, agent_id, title, summary, active, started_at, "
                        " ended_at, outcome) "
                        "VALUES (:id, :aid, 'f053 ep fixture', 'summary', "
                        "        :act, :st, :en, :oc)"
                    ), {
                        "id": eid, "aid": agent_id, "act": active,
                        "st": now, "en": ended, "oc": outcome,
                    })
                for eid, ep in [
                    (e_closed, ep_closed),
                    (e_trivial, ep_trivial),
                    (e_abandoned, ep_abandoned),
                ]:
                    await fs.execute(sql_text(
                        "INSERT INTO brain.graph_edges "
                        "(id, source_id, source_type, target_id, target_type, "
                        " agent_id, relation, weight) "
                        "VALUES (:id, :s, 'fact', :t, 'episode', :aid, "
                        "        'extracted_from', 1.0)"
                    ), {"id": eid, "s": fact_id, "t": ep, "aid": agent_id})
                await fs.commit()

            settings = Settings()
            object.__setattr__(settings, "agent_id", agent_id)
            object.__setattr__(settings, "dead_edge_pruning_enabled", True)
            object.__setattr__(settings, "dead_edge_pruning_max_per_cycle", 1000)

            heart = Heart(db, settings, embedding_provider=mock_embeddings)
            brain = AsyncMock()
            bus = MagicMock(spec=EventBus)
            bus.on = MagicMock()
            bus.emit = AsyncMock()
            handler = SleepHandler(brain, heart, settings, bus, AsyncMock())

            sleep_stats: dict = {}
            assert await handler._phase_prune_dead_edges(sleep_stats) is True
            assert sleep_stats.get("dead_edges_pruned") == 2

            async with db.session() as vs:
                rows = await vs.execute(sql_text(
                    "SELECT id FROM brain.graph_edges WHERE agent_id = :aid"
                ), {"aid": agent_id})
                surviving = {r.id for r in rows}
            assert surviving == {e_closed}, (
                f"closed episode's edge must survive; got {surviving}"
            )
            await heart.close()
        finally:
            async with db.session() as cs:
                await cs.execute(sql_text(
                    "DELETE FROM brain.graph_edges WHERE agent_id = :aid"
                ), {"aid": agent_id})
                await cs.execute(sql_text(
                    "DELETE FROM heart.facts WHERE agent_id = :aid"
                ), {"aid": agent_id})
                await cs.execute(sql_text(
                    "DELETE FROM heart.episodes WHERE agent_id = :aid"
                ), {"aid": agent_id})
                await cs.execute(sql_text(
                    "DELETE FROM nous_system.agents WHERE id = :aid"
                ), {"aid": agent_id})
                await cs.commit()
```

NOTE for implementer: check `heart.episodes` NOT NULL columns before finalizing the INSERT (e.g. `title`/`summary`/`started_at` — mirror whatever the existing integration test inserts for episodes elsewhere in the suite, or `sql/init.sql`). Adjust the INSERT column list so the fixture satisfies the real schema; the assertion logic must not change.

- [ ] **Step 2: Run the mock test to verify it fails**

Run: `uv run pytest tests/test_f053_dead_edge_prune.py::TestF053DeadEdgePrune::test_sql_does_not_treat_closed_episodes_as_dead -v`
Expected: FAIL — `"ended_at IS NULL" not in sql_str`

- [ ] **Step 3: Implement the SQL change**

In `nous/handlers/sleep_handler.py`, `_phase_prune_dead_edges`:

Add import near the function's other local imports (the function already does `from sqlalchemy import text` inline; put this import beside it):

```python
            from nous.brain.graph_constants import episode_dead_sql
```

Replace the episode branch of the CTE (currently lines 1791-1794):

```python
                sql = text(f"""
                    WITH inactive_nodes AS (
                        SELECT id, 'fact'::text AS node_type
                        FROM heart.facts
                        WHERE agent_id = :agent_id AND active = false
                        UNION ALL
                        -- Episodes: active=false is the NORMAL closed lifecycle
                        -- state (008.3 — Episode._end() flips it on every session
                        -- close), NOT a deletion marker. Only genuinely-deleted
                        -- episodes are dead nodes: trivial discards (deactivated
                        -- without ever ending) and F060.2 abandoned marks.
                        -- 2026-07-12 prod audit: the old bare `active = false`
                        -- predicate erased the entire episode graph layer
                        -- (657 closed episodes held 6 edges).
                        SELECT id, 'episode'
                        FROM heart.episodes
                        WHERE agent_id = :agent_id AND {episode_dead_sql()}
                        UNION ALL
                        SELECT id, 'procedure'
                        FROM heart.procedures
                        WHERE agent_id = :agent_id AND active = false
                    ),
```

(The rest of the statement — `victim_edges`, supersedes exclusion, `LIMIT :max_per_cycle`, `DELETE ... RETURNING id` — is unchanged. The only other mechanical change: the `text("""...""")` literal becomes `text(f"""...""")`. `episode_dead_sql()` interpolates fixed literals only, no user input.)

- [ ] **Step 4: Run mock tests, then integration tests**

Run: `uv run pytest tests/test_f053_dead_edge_prune.py -v`
Expected: all mock tests PASS (including both pre-existing regression tests).

Run (requires local Postgres via `docker compose up -d postgres`):
`$env:NOUS_TEST_DB='postgres'; uv run pytest tests/test_f053_dead_edge_prune.py -m integration --integration -v`
Expected: both integration tests PASS.

- [ ] **Step 5: Commit**

```bash
git add nous/handlers/sleep_handler.py tests/test_f053_dead_edge_prune.py
git commit -m "fix(sleep): F053 prune no longer treats closed episodes as dead nodes"
```

---

### Task 3: F040 orphan eligibility — closed episodes can be backfilled

**Files:**
- Modify: `nous/brain/_entity_config.py:22-34` (episode row)
- Test: `tests/test_graph_densifier.py` (extend)

**Interfaces:**
- Consumes: `episode_live_sql` from Task 1.
- Produces: `_ENTITY_CONFIG["episode"]` whose `extra_where` is the liveness predicate. Filtering consumers (`find_orphans`, `_backfill_cross_type` Source-1 vector query via `{target_where}`) pick this up automatically. Non-filtering consumers are unaffected by design: `backfill_rerank.fetch_candidate_content` and the `discover_clusters` hub-content fetch (`graph_densifier.py:1562`) read only the table/content-column entries and discard `extra_where` (batch-by-ID lookups).

- [ ] **Step 1: Write the failing test**

Add to `tests/test_graph_densifier.py` (mirror the file's existing test style; if the file has a unit-test section for `_ENTITY_CONFIG`/`find_orphans`, place it there):

```python
class TestEpisodeOrphanEligibility:
    """2026-07-12: closed episodes (active=false, ended_at set) must be
    orphan-eligible so F040 can heal the layer F053 wrongly pruned;
    trivial discards and abandoned episodes must stay excluded."""

    def test_entity_config_episode_uses_liveness_predicate(self):
        from nous.brain._entity_config import _ENTITY_CONFIG

        _, _, _, extra_where = _ENTITY_CONFIG["episode"]
        assert "ended_at IS NOT NULL" in extra_where
        assert "IS DISTINCT FROM 'abandoned'" in extra_where
        assert extra_where.count("t.active = true") == 1

    def test_entity_config_fact_and_procedure_unchanged(self):
        from nous.brain._entity_config import _ENTITY_CONFIG

        assert _ENTITY_CONFIG["fact"][3] == "t.active = true"
        assert _ENTITY_CONFIG["procedure"][3] == "t.active = true"
```

Plus an integration test in the same file's postgres-marked section (create the class if the file has none, following `TestF053Integration`'s skip-marker pattern):

```python
@pytest.mark.integration
@pytest.mark.postgres_only
class TestFindOrphansEpisodeLiveness:
    @pytest.mark.asyncio
    async def test_find_orphans_includes_closed_excludes_deleted(
        self, db, mock_embeddings,
    ):
        """A closed edge-less episode IS an orphan; a trivial-discard and
        an abandoned episode are NOT (they're genuinely deleted)."""
        from datetime import UTC, datetime
        from uuid import uuid4

        from sqlalchemy import text as sql_text

        from nous.brain.graph_densifier import GraphDensifier
        from nous.brain.graph_linker import GraphLinker
        from nous.config import Settings

        agent_id = f"f040-ep-{uuid4().hex[:8]}"
        now = datetime.now(UTC)
        ep_closed = uuid4()
        ep_trivial = uuid4()
        ep_abandoned = uuid4()

        try:
            async with db.session() as fs:
                await fs.execute(sql_text(
                    "INSERT INTO nous_system.agents (id, name) VALUES "
                    "(:aid, 'x') ON CONFLICT (id) DO NOTHING"
                ), {"aid": agent_id})
                for eid, active, ended, outcome in [
                    (ep_closed, False, now, "success"),
                    (ep_trivial, False, None, None),
                    # Prod shape: F060.2 abandoned rows have ended_at SET.
                    (ep_abandoned, False, now, "abandoned"),
                ]:
                    await fs.execute(sql_text(
                        "INSERT INTO heart.episodes "
                        "(id, agent_id, title, summary, active, started_at, "
                        " ended_at, outcome) "
                        "VALUES (:id, :aid, 'f040 fixture', 'summary text', "
                        "        :act, :st, :en, :oc)"
                    ), {
                        "id": eid, "aid": agent_id, "act": active,
                        "st": now, "en": ended, "oc": outcome,
                    })
                await fs.commit()

            settings = Settings()
            object.__setattr__(settings, "agent_id", agent_id)
            linker = GraphLinker(
                db=db, embedder=mock_embeddings,
                settings=settings, agent_id=agent_id,
            )
            densifier = GraphDensifier(
                db=db, graph_linker=linker, embedder=mock_embeddings,
                settings=settings, agent_id=agent_id,
            )
            async with db.session() as s:
                orphans = await densifier.find_orphans(
                    "episode", 50, s, require_embedding=False,
                )
            orphan_ids = {oid for oid, _ in orphans}
            assert ep_closed in orphan_ids
            assert ep_trivial not in orphan_ids
            assert ep_abandoned not in orphan_ids
        finally:
            async with db.session() as cs:
                await cs.execute(sql_text(
                    "DELETE FROM heart.episodes WHERE agent_id = :aid"
                ), {"aid": agent_id})
                await cs.execute(sql_text(
                    "DELETE FROM nous_system.agents WHERE id = :aid"
                ), {"aid": agent_id})
                await cs.commit()
```

NOTE for implementer: same schema caveat as Task 2 — verify `heart.episodes` NOT NULL columns and the GraphDensifier constructor signature against the file (`tests/test_graph_densifier.py` already constructs densifiers; copy its exact construction).

- [ ] **Step 2: Run unit test to verify it fails**

Run: `uv run pytest tests/test_graph_densifier.py::TestEpisodeOrphanEligibility -v`
Expected: FAIL — `"ended_at IS NOT NULL" not in extra_where`

- [ ] **Step 3: Implement**

In `nous/brain/_entity_config.py`:

```python
from nous.brain.graph_constants import episode_live_sql
```

and replace the episode row's `extra_where` (currently the string `"t.active = true"` on line 33):

```python
    "episode": (
        "heart.episodes",
        "episode",
        # F058 (2026-05-04): fall back to plain `summary` when
        # `structured_summary` is NULL. Stuck-open sessions never receive
        # `episode_ended` → episode_summarizer never fires → structured_summary
        # stays NULL forever. Plain `summary` (set at episode start, often the
        # first user message) is always populated. F040 was excluding 76/76
        # eval-scratch orphans because of the IS NOT NULL filter; same pattern
        # on prod (78 active orphans, all NULL structured_summary).
        "COALESCE(t.structured_summary->>'summary', t.summary)",
        # 2026-07-12: episodes.active=false is the normal CLOSED state
        # (008.3), not deletion — bare `t.active = true` excluded every
        # completed episode from backfill, so F053's over-prune could never
        # heal. Liveness predicate mirrors HT-1's search fix.
        episode_live_sql("t."),
    ),
```

(`graph_constants` imports only stdlib — no circular import: `_entity_config` is itself a leaf.)

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_graph_densifier.py -v`
Expected: unit tests PASS.
Run: `$env:NOUS_TEST_DB='postgres'; uv run pytest tests/test_graph_densifier.py -m integration --integration -v`
Expected: integration tests PASS.

- [ ] **Step 5: Commit**

```bash
git add nous/brain/_entity_config.py tests/test_graph_densifier.py
git commit -m "fix(graph): F040 orphan eligibility includes closed episodes (liveness, not active flag)"
```

---

### Task 4: F040 candidate-side — episode targets filtered by liveness, not `active`

**Files:**
- Modify: `nous/brain/graph_densifier.py:212-226` (`_backfill_same_type` hybrid_search call) and `nous/brain/graph_densifier.py:348-359` (`_backfill_cross_type` Source-2 keyword hybrid_search call)
- Test: `tests/test_graph_densifier.py` (extend)

**Interfaces:**
- Consumes: `episode_live_sql` from Task 1; `hybrid_search(..., extra_where=..., active_filter=...)` from `nous/heart/search.py:187-242`.
- Produces: closed episodes appear as link *targets* in same-type and cross-type backfill.

Context for the implementer: `hybrid_search(active_filter=True)` injects `AND t.active = true` (`search.py:241`). For episode tables that excludes every closed episode from the candidate pool, so even after Task 3 an orphan episode could only ever link to *ongoing* episodes. Cross-type Source 1 (the vector query at `graph_densifier.py:330-339`) already interpolates `{target_where}` from `_ENTITY_CONFIG`, so Task 3 fixed that path — only the two `hybrid_search` calls remain.

**DEPENDENCY:** this task's test only fails-for-the-right-reason once Task 3 is merged (before Task 3 the fixture has zero orphans and the test fails with `created == 0` for a different cause). Execute Tasks 3 and 4 in order, not in parallel.

**Scope note (review F9):** the `_backfill_cross_type` Source-2 carve-out is consistency-hardening — no current caller passes `target_type="episode"` (callers: fact→decision `:462`, decision→fact `:489`, episode→fact `:516`, procedure→* `:1059`). It gets no dedicated test; it exists so a future episode-target caller doesn't silently re-inherit the `active=true` filter. The integration test below covers the `_backfill_same_type` leg, which is live.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_graph_densifier.py`'s postgres-marked section:

```python
@pytest.mark.integration
@pytest.mark.postgres_only
class TestBackfillTargetsClosedEpisodes:
    @pytest.mark.asyncio
    async def test_same_type_backfill_links_orphan_to_closed_episode(
        self, db, mock_embeddings,
    ):
        """Two closed episodes with near-identical embeddings and no edges:
        backfilling one must create an episode↔episode related_to edge to
        the other, even though both have active=false (closed)."""
        from datetime import UTC, datetime
        from uuid import uuid4

        from sqlalchemy import text as sql_text

        from nous.brain.graph_densifier import GraphDensifier
        from nous.brain.graph_linker import GraphLinker
        from nous.config import Settings

        agent_id = f"f040-tgt-{uuid4().hex[:8]}"
        now = datetime.now(UTC)
        ep_a, ep_b = uuid4(), uuid4()

        # Identical embedding → cosine 1.0, passes any threshold.
        emb = "[" + ",".join(["0.1"] * 1536) + "]"

        try:
            async with db.session() as fs:
                await fs.execute(sql_text(
                    "INSERT INTO nous_system.agents (id, name) VALUES "
                    "(:aid, 'x') ON CONFLICT (id) DO NOTHING"
                ), {"aid": agent_id})
                for eid in (ep_a, ep_b):
                    await fs.execute(sql_text(
                        "INSERT INTO heart.episodes "
                        "(id, agent_id, title, summary, active, started_at, "
                        " ended_at, outcome, embedding) "
                        "VALUES (:id, :aid, 'target fixture', "
                        "        'deploying the nous agent to production', "
                        "        false, :st, :en, 'success', "
                        "        CAST(:emb AS vector))"
                    ), {
                        "id": eid, "aid": agent_id,
                        "st": now, "en": now, "emb": emb,
                    })
                await fs.commit()

            settings = Settings()
            object.__setattr__(settings, "agent_id", agent_id)
            object.__setattr__(settings, "ce_backfill_enabled", False)
            linker = GraphLinker(
                db=db, embedder=mock_embeddings,
                settings=settings, agent_id=agent_id,
            )
            densifier = GraphDensifier(
                db=db, graph_linker=linker, embedder=mock_embeddings,
                settings=settings, agent_id=agent_id,
            )
            created = await densifier.backfill_orphan_episodes(max_count=5)
            assert created >= 1, (
                "orphan closed episode must link to the other closed episode"
            )
        finally:
            async with db.session() as cs:
                await cs.execute(sql_text(
                    "DELETE FROM brain.graph_edges WHERE agent_id = :aid"
                ), {"aid": agent_id})
                await cs.execute(sql_text(
                    "DELETE FROM heart.episodes WHERE agent_id = :aid"
                ), {"aid": agent_id})
                await cs.execute(sql_text(
                    "DELETE FROM nous_system.agents WHERE id = :aid"
                ), {"aid": agent_id})
                await cs.commit()
```

NOTE for implementer: `backfill_orphan_episodes` also calls `_backfill_cross_type("episode", ..., "fact", ...)` — with no facts in the fixture that path returns 0 and is harmless. If `mock_embeddings.embed` returns a fixed vector, the same-type path uses the STORED embedding (fetched at `graph_densifier.py:202-210`), so the identical-stored-embedding fixture is what makes cosine pass deterministically.

- [ ] **Step 2: Run test to verify it fails**

Run: `$env:NOUS_TEST_DB='postgres'; uv run pytest tests/test_graph_densifier.py::TestBackfillTargetsClosedEpisodes -m integration --integration -v`
Expected: FAIL — `created == 0` (hybrid_search's `AND t.active = true` excludes the closed candidate; note the orphan itself IS found thanks to Task 3).

- [ ] **Step 3: Implement**

In `nous/brain/graph_densifier.py`, add to the existing `graph_constants` import (line 19):

```python
from nous.brain.graph_constants import autobehavior_exclusion_sql, episode_live_sql
```

`_backfill_same_type` — replace lines 212-226:

```python
        # Hybrid search: vector + keyword via RRF
        # brain.decisions has no `active` column — disable active filter for
        # it. Episodes: `active=false` is the closed lifecycle state, not
        # deletion (HT-1) — filter by the liveness predicate instead of the
        # raw flag, or closed episodes can never be link targets.
        extra_where = "AND t.id != :orphan_id"
        has_active = entity_type != "decision"
        if entity_type == "episode":
            has_active = False
            extra_where += f" AND {episode_live_sql('t.')}"
        candidates = await hybrid_search(
            session=session,
            table=table,
            embedding=orphan_embedding,
            query_text=orphan_content[:500] if orphan_content else "",
            agent_id=self._agent_id,
            extra_where=extra_where,
            extra_params={"orphan_id": orphan_id},
            limit=10,
            vector_weight=0.6,  # 60% vector, 40% keyword — gives FTS more weight than default
            active_filter=has_active,
        )
```

`_backfill_cross_type` Source 2 — replace lines 349-359:

```python
        # Source 2: Keyword search via hybrid_search (keyword-only, no embedding)
        if orphan_content:
            has_active = target_type != "decision"
            kw_extra_where = ""
            if target_type == "episode":
                # Same liveness carve-out as _backfill_same_type.
                has_active = False
                kw_extra_where = f"AND {episode_live_sql('t.')}"
            keyword_hits = await hybrid_search(
                session=session,
                table=target_table,
                embedding=None,  # keyword-only
                query_text=orphan_content[:500],
                agent_id=self._agent_id,
                extra_where=kw_extra_where,
                limit=5,
                active_filter=has_active,
            )
            for cand_id, _ in keyword_hits:
                candidate_ids.add(cand_id)
```

- [ ] **Step 4: Run tests**

Run: `$env:NOUS_TEST_DB='postgres'; uv run pytest tests/test_graph_densifier.py -m integration --integration -v`
Expected: PASS.
Run full densifier suite without markers: `uv run pytest tests/test_graph_densifier.py -v`
Expected: PASS (no regressions in mock tests).

- [ ] **Step 5: Commit**

```bash
git add nous/brain/graph_densifier.py tests/test_graph_densifier.py
git commit -m "fix(graph): F040 candidate search reaches closed episodes via liveness predicate"
```

---

### Task 5: `GraphDensifier.restore_episode_anchor_edges()` — deterministic edge restore

**Files:**
- Modify: `nous/brain/graph_densifier.py` (new public method, place after `backfill_orphan_chunks`)
- Test: `tests/test_graph_densifier.py` (extend)

**Interfaces:**
- Consumes: `episode_live_sql` (Task 1).
- Produces: `async def restore_episode_anchor_edges(self, *, dry_run: bool = False) -> dict[str, int]` returning `{"part_of": <int>, "extracted_from": <int>, "discussed_in": <int>}` (counts inserted, or would-insert when `dry_run`). Task 6's script calls this.

Three deterministic edge classes, each mirroring its original writer byte-for-byte:

| relation | direction | FK ground truth | original writer |
|---|---|---|---|
| `part_of` | chunk → episode | `episode_chunks.episode_id` | `backfill_orphan_chunks` step 1 (`graph_densifier.py:578-588`) |
| `extracted_from` | fact → episode | `facts.source_episode_id` (active facts) | `link_episode_deterministic` (`graph_linker.py:393-408`) |
| `discussed_in` | episode → decision | `heart.episode_decisions` join table (`models.py:529-545`) | `link_episode_deterministic` (`graph_linker.py:367-390`) — review F2: F053 destroyed these too, and no other mechanism can rebuild them (F057 relink is `active=true`-only; F040 never targets decisions from episodes) |

All three: weight 1.0, `auto_linked=TRUE`, `extraction_method='deterministic'`, LIVE parent episode only, `ON CONFLICT (source_id, target_id, relation) DO NOTHING`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_graph_densifier.py`'s postgres-marked section:

IMPORTANT (review finding #2, test-infra): the new test class MUST take the existing `_fix_stale_relation_constraint` fixture from `tests/test_graph_densifier.py:100-107` as a parameter — bare `sql/init.sql` lacks `part_of` in the relation CHECK and `chunk` in the type CHECKs (they arrive via migrations 016/051), and `heart.episode_chunks` itself only exists post-migration. Every other edge-inserting postgres class in that file already does this; copy the pattern. The suite prerequisite is a migrated DB (run the app once or the migrator against the test DB).

```python
@pytest.mark.integration
@pytest.mark.postgres_only
class TestRestoreEpisodeAnchorEdges:
    @pytest.mark.asyncio
    async def test_restore_is_complete_scoped_idempotent_and_prune_safe(
        self, db, mock_embeddings, _fix_stale_relation_constraint,
    ):
        """The full F053 damage shape, plus the invariants around it:

        Agent A fixture:
          - LIVE closed episode: 2 chunks, 1 active fact, 1 inactive fact,
            1 linked decision (episode_decisions row), 1 fact with NULL
            source_episode_id (must contribute nothing).
          - DEAD (trivial) episode: 1 chunk, 1 active fact (all skipped).
        Agent B fixture: identical minimal live shape (1 chunk) — must be
        untouched by agent A's run and excluded from A's dry-run counts.

        Asserts: dry_run counts == real-run counts and dry_run writes
        nothing; restored rows have exact endpoints, weight 1.0,
        extraction_method='deterministic'; re-run inserts 0 (idempotent);
        and — the invariant this whole plan exists for — running
        _phase_prune_dead_edges AFTER the restore deletes none of the
        restored edges (episode_dead_sql and episode_live_sql stay
        complements)."""
        from datetime import UTC, datetime
        from unittest.mock import AsyncMock, MagicMock
        from uuid import uuid4

        from sqlalchemy import text as sql_text

        from nous.brain.graph_densifier import GraphDensifier
        from nous.brain.graph_linker import GraphLinker
        from nous.config import Settings
        from nous.events import EventBus
        from nous.handlers.sleep_handler import SleepHandler
        from nous.heart import Heart

        agent_a = f"f053-ra-{uuid4().hex[:8]}"
        agent_b = f"f053-rb-{uuid4().hex[:8]}"
        now = datetime.now(UTC)
        ep_live, ep_dead, ep_b = uuid4(), uuid4(), uuid4()
        chunk_l1, chunk_l2, chunk_d, chunk_b = uuid4(), uuid4(), uuid4(), uuid4()
        fact_live, fact_inactive, fact_dead_ep, fact_null_ep = (
            uuid4(), uuid4(), uuid4(), uuid4(),
        )
        decision_id = uuid4()

        try:
            async with db.session() as fs:
                for aid in (agent_a, agent_b):
                    await fs.execute(sql_text(
                        "INSERT INTO nous_system.agents (id, name) VALUES "
                        "(:aid, 'x') ON CONFLICT (id) DO NOTHING"
                    ), {"aid": aid})
                for eid, aid, ended, outcome in [
                    (ep_live, agent_a, now, "success"),
                    (ep_dead, agent_a, None, None),
                    (ep_b, agent_b, now, "success"),
                ]:
                    await fs.execute(sql_text(
                        "INSERT INTO heart.episodes "
                        "(id, agent_id, title, summary, active, started_at, "
                        " ended_at, outcome) "
                        "VALUES (:id, :aid, 'restore fixture', 's', false, "
                        "        :st, :en, :oc)"
                    ), {
                        "id": eid, "aid": aid, "st": now, "en": ended,
                        "oc": outcome,
                    })
                for cid, aid, eid, idx in [
                    (chunk_l1, agent_a, ep_live, 0),
                    (chunk_l2, agent_a, ep_live, 1),
                    (chunk_d, agent_a, ep_dead, 0),
                    (chunk_b, agent_b, ep_b, 0),
                ]:
                    await fs.execute(sql_text(
                        "INSERT INTO heart.episode_chunks "
                        "(id, agent_id, episode_id, chunk_index, content) "
                        "VALUES (:id, :aid, :eid, :idx, 'chunk content')"
                    ), {"id": cid, "aid": aid, "eid": eid, "idx": idx})
                for fid, eid, active in [
                    (fact_live, ep_live, True),
                    (fact_inactive, ep_live, False),
                    (fact_dead_ep, ep_dead, True),
                    (fact_null_ep, None, True),
                ]:
                    await fs.execute(sql_text(
                        "INSERT INTO heart.facts "
                        "(id, agent_id, content, active, source_episode_id) "
                        "VALUES (:id, :aid, 'restore fact fixture', :a, :eid)"
                    ), {"id": fid, "aid": agent_a, "a": active, "eid": eid})
                await fs.execute(sql_text(
                    "INSERT INTO brain.decisions "
                    "(id, agent_id, description, confidence, category, stakes) "
                    "VALUES (:id, :aid, 'restore decision fixture', 0.8, "
                    "        'process', 'low')"
                ), {"id": decision_id, "aid": agent_a})
                await fs.execute(sql_text(
                    "INSERT INTO heart.episode_decisions "
                    "(episode_id, decision_id) VALUES (:eid, :did)"
                ), {"eid": ep_live, "did": decision_id})
                await fs.commit()

            settings = Settings()
            object.__setattr__(settings, "agent_id", agent_a)
            linker = GraphLinker(
                db=db, embedder=mock_embeddings,
                settings=settings, agent_id=agent_a,
            )
            densifier = GraphDensifier(
                db=db, graph_linker=linker, embedder=mock_embeddings,
                settings=settings, agent_id=agent_a,
            )

            expected = {"part_of": 2, "extracted_from": 1, "discussed_in": 1}

            # dry_run: report without writing, agent-scoped
            dry = await densifier.restore_episode_anchor_edges(dry_run=True)
            assert dry == expected
            async with db.session() as vs:
                n = (await vs.execute(sql_text(
                    "SELECT count(*) FROM brain.graph_edges "
                    "WHERE agent_id IN (:a, :b)"
                ), {"a": agent_a, "b": agent_b})).scalar()
            assert n == 0, "dry_run must not write"

            # real run
            created = await densifier.restore_episode_anchor_edges()
            assert created == expected

            async with db.session() as vs:
                rows = (await vs.execute(sql_text(
                    "SELECT source_id, target_id, relation, weight, "
                    "       extraction_method, agent_id "
                    "FROM brain.graph_edges "
                    "WHERE agent_id IN (:a, :b)"
                ), {"a": agent_a, "b": agent_b})).all()
            assert all(r.agent_id == agent_a for r in rows), (
                "agent B must be untouched"
            )
            by_rel: dict = {}
            for r in rows:
                by_rel.setdefault(r.relation, []).append(r)
            assert {r.source_id for r in by_rel["part_of"]} == {chunk_l1, chunk_l2}
            assert all(r.target_id == ep_live for r in by_rel["part_of"])
            assert by_rel["extracted_from"][0].source_id == fact_live
            assert by_rel["extracted_from"][0].target_id == ep_live
            assert by_rel["discussed_in"][0].source_id == ep_live
            assert by_rel["discussed_in"][0].target_id == decision_id
            assert all(
                float(r.weight) == 1.0 and r.extraction_method == "deterministic"
                for r in rows
            )

            # idempotent re-run
            again = await densifier.restore_episode_anchor_edges()
            assert again == {"part_of": 0, "extracted_from": 0, "discussed_in": 0}

            # restore → prune interplay: the prune must NOT delete what the
            # restore just wrote (dead/live predicates stay complements).
            object.__setattr__(settings, "dead_edge_pruning_enabled", True)
            object.__setattr__(settings, "dead_edge_pruning_max_per_cycle", 1000)
            heart = Heart(db, settings, embedding_provider=mock_embeddings)
            bus = MagicMock(spec=EventBus)
            bus.on = MagicMock()
            bus.emit = AsyncMock()
            handler = SleepHandler(AsyncMock(), heart, settings, bus, AsyncMock())
            assert await handler._phase_prune_dead_edges({}) is True
            async with db.session() as vs:
                n_after = (await vs.execute(sql_text(
                    "SELECT count(*) FROM brain.graph_edges WHERE agent_id = :a"
                ), {"a": agent_a})).scalar()
            assert n_after == sum(expected.values()), (
                "prune deleted restored edges — dead/live predicates drifted"
            )
            await heart.close()
        finally:
            async with db.session() as cs:
                for table, col in (
                    ("brain.graph_edges", "agent_id"),
                    ("heart.episode_chunks", "agent_id"),
                    ("heart.facts", "agent_id"),
                ):
                    await cs.execute(sql_text(
                        f"DELETE FROM {table} WHERE {col} IN (:a, :b)"
                    ), {"a": agent_a, "b": agent_b})
                await cs.execute(sql_text(
                    "DELETE FROM heart.episode_decisions WHERE decision_id = :d"
                ), {"d": decision_id})
                await cs.execute(sql_text(
                    "DELETE FROM brain.decisions WHERE agent_id = :a"
                ), {"a": agent_a})
                await cs.execute(sql_text(
                    "DELETE FROM heart.episodes WHERE agent_id IN (:a, :b)"
                ), {"a": agent_a, "b": agent_b})
                await cs.execute(sql_text(
                    "DELETE FROM nous_system.agents WHERE id IN (:a, :b)"
                ), {"a": agent_a, "b": agent_b})
                await cs.commit()
```

NOTE for implementer: `heart.episode_decisions` deletes must run before `brain.decisions` (FK), and episodes last among heart tables (chunk/fact FKs). Verify `heart.episode_decisions` has no extra NOT NULL columns beyond the two keys (`models.py:529-545`).

- [ ] **Step 2: Run test to verify it fails**

Run: `$env:NOUS_TEST_DB='postgres'; uv run pytest tests/test_graph_densifier.py::TestRestoreEpisodeAnchorEdges -m integration --integration -v`
Expected: FAIL — `AttributeError: 'GraphDensifier' object has no attribute 'restore_episode_anchor_edges'`

- [ ] **Step 3: Implement**

Add to `GraphDensifier` (after `backfill_orphan_chunks`):

```python
    async def restore_episode_anchor_edges(
        self, *, dry_run: bool = False,
    ) -> dict[str, int]:
        """One-shot remediation for the F053 episode-prune bug (2026-07-12).

        The old prune predicate treated every closed episode as a dead node
        and deleted its incident edges; the orphan gate (chunks keep their
        chunk↔chunk edges → never re-orphan) made the loss permanent. This
        restores the three DETERMINISTIC edge classes directly from their FK
        ground truth — no embeddings, no LLM:

          - chunk   → episode  ``part_of``        (episode_chunks.episode_id;
            mirrors backfill_orphan_chunks step 1: weight 1.0, structural)
          - fact    → episode  ``extracted_from`` (facts.source_episode_id,
            active facts only; mirrors GraphLinker.link_episode_deterministic)
          - episode → decision ``discussed_in``   (heart.episode_decisions;
            mirrors GraphLinker.link_episode_deterministic — F053 destroyed
            these too, and no other mechanism rebuilds them)

        Cosine-inferred classes (episode↔episode related_to, episode→fact)
        are NOT restored here — they heal via
        ``scripts/backfill_f053_episode_edges.py --densify``, which MUST run
        BEFORE this method for the historical population: these anchors
        de-orphan every episode they touch, and F040's orphan gate then
        skips them forever (the same ratchet this plan diagnoses).

        Idempotent (ON CONFLICT DO NOTHING); only targets LIVE episodes.
        Returns inserted counts per relation (would-insert counts when
        ``dry_run``).
        """
        live = episode_live_sql("ep.")
        # ep.agent_id scoping is defense-in-depth (FKs cannot cross agents),
        # per the repo rule: agent-scope every side of every new query.
        selects = {
            "part_of": f"""
                SELECT c.id AS source_id, c.episode_id AS target_id,
                       'chunk' AS source_type, 'episode' AS target_type,
                       c.agent_id, 'part_of' AS relation
                FROM heart.episode_chunks c
                JOIN heart.episodes ep
                  ON ep.id = c.episode_id AND ep.agent_id = :agent_id
                WHERE c.agent_id = :agent_id AND {live}
            """,
            "extracted_from": f"""
                SELECT f.id AS source_id, f.source_episode_id AS target_id,
                       'fact' AS source_type, 'episode' AS target_type,
                       f.agent_id, 'extracted_from' AS relation
                FROM heart.facts f
                JOIN heart.episodes ep
                  ON ep.id = f.source_episode_id AND ep.agent_id = :agent_id
                WHERE f.agent_id = :agent_id AND f.active = TRUE AND {live}
            """,
            "discussed_in": f"""
                SELECT ed.episode_id AS source_id, ed.decision_id AS target_id,
                       'episode' AS source_type, 'decision' AS target_type,
                       ep.agent_id, 'discussed_in' AS relation
                FROM heart.episode_decisions ed
                JOIN heart.episodes ep
                  ON ep.id = ed.episode_id AND ep.agent_id = :agent_id
                WHERE {live}
            """,
        }
        results: dict[str, int] = {}
        async with self.db.session() as session:
            for relation, select_sql in selects.items():
                if dry_run:
                    count_sql = text(f"""
                        SELECT count(*) FROM ({select_sql}) cand
                        WHERE NOT EXISTS (
                            SELECT 1 FROM brain.graph_edges e
                            WHERE e.agent_id = :agent_id
                              AND e.source_id = cand.source_id
                              AND e.target_id = cand.target_id
                              AND e.relation = cand.relation
                        )
                    """)
                    row = await session.execute(
                        count_sql, {"agent_id": self._agent_id},
                    )
                    results[relation] = int(row.scalar() or 0)
                    continue
                insert_sql = text(f"""
                    INSERT INTO brain.graph_edges
                        (source_id, target_id, source_type, target_type,
                         agent_id, relation, weight, auto_linked,
                         extraction_method)
                    SELECT source_id, target_id, source_type, target_type,
                           agent_id, relation, 1.0, TRUE, 'deterministic'
                    FROM ({select_sql}) cand
                    ON CONFLICT (source_id, target_id, relation) DO NOTHING
                """)
                result = await session.execute(
                    insert_sql, {"agent_id": self._agent_id},
                )
                results[relation] = result.rowcount or 0
            if not dry_run:
                await session.commit()
        if not dry_run and any(results.values()):
            logger.info(
                "F053 restore: %d part_of + %d extracted_from + "
                "%d discussed_in edges re-anchored for agent_id=%s",
                results["part_of"], results["extracted_from"],
                results["discussed_in"], self._agent_id,
            )
        return results
```

- [ ] **Step 4: Run test to verify it passes**

Run: `$env:NOUS_TEST_DB='postgres'; uv run pytest tests/test_graph_densifier.py::TestRestoreEpisodeAnchorEdges -m integration --integration -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add nous/brain/graph_densifier.py tests/test_graph_densifier.py
git commit -m "feat(graph): restore_episode_anchor_edges — deterministic part_of/extracted_from/discussed_in re-anchor"
```

---

### Task 6: CLI script `scripts/backfill_f053_episode_edges.py`

**Files:**
- Create: `scripts/backfill_f053_episode_edges.py`
- Test: covered by Task 5's tests (the script is a thin CLI over `restore_episode_anchor_edges` + the already-tested `backfill_orphan_episodes`); the smoke-check in Step 2 exercises arg parsing and the dry-run path end-to-end.

**Interfaces:**
- Consumes: `GraphDensifier.restore_episode_anchor_edges` (Task 5), `GraphDensifier.backfill_orphan_episodes` (existing), construction pattern from `scripts/backfill_f070_chunks.py:201-225` (teardown is `await db.disconnect()` — `Database` has `connect()/disconnect()/session()`, NO `close()`; review P1).

**ORDERING (review F1 — load-bearing):** the deterministic anchors de-orphan every episode they touch; `find_orphans` counts ANY non-excluded incident edge (`graph_densifier.py:159-172`), and `part_of`/`extracted_from`/`discussed_in` are not excluded. If anchors land first, neither `--densify` nor the nightly F040 cycle will ever build cosine `episode↔episode` edges for the historical population — the exact ratchet this plan diagnoses. Therefore the script runs the `--densify` drain (Phase 1) BEFORE the anchor restore (Phase 2), and dry-run computes both counts before any write.

- [ ] **Step 1: Write the script**

```python
"""F053 remediation — restore episode graph edges wrongly pruned.

Background (prod audit 2026-07-12, FORGE 2a5fd57a): F053's dead-edge prune
treated every normally-closed episode (`active=false` = 008.3 lifecycle
close) as a dead node and deleted its incident edges nightly; F040 could
not rebuild them (`t.active = true` orphan filter). 657 closed prod
episodes held 6 edges total; chunk→episode part_of was down to 3/3,060.

ORDERING IS LOAD-BEARING: the deterministic anchors de-orphan every
episode they touch (find_orphans counts ANY non-excluded incident edge),
so the cosine drain must run FIRST or F040 can never build semantic
episode↔episode edges for the historical population.

  Phase 1 (--densify, optional): drain the orphan-eligible closed
      episodes through the normal F040 backfill (episode↔episode
      related_to + episode→fact cross-type; embedding + optional CE
      cost) instead of waiting ~NOUS_GRAPH_BACKFILL_MAX_EPISODES per
      nightly sleep cycle. Skipping this phase permanently forgoes
      cosine healing for episodes the anchors then de-orphan.
  Phase 2 (always): deterministic re-anchor from FK ground truth via
      GraphDensifier.restore_episode_anchor_edges():
        * chunk   → episode  part_of         (weight 1.0, deterministic)
        * fact    → episode  extracted_from  (active facts, weight 1.0)
        * episode → decision discussed_in    (episode_decisions join table)

Run AFTER deploying the prune fix, or the next sleep cycle re-deletes
everything this restores. Pin NOUS_SPREADING_ACTIVATION_ENABLED=false
first (the ~5k restored edges count toward the auto-spreading density
gate; prod sits at 2.745 vs threshold 3.0 and spreading measured
negative on prod).

Usage:
    # counts only (both phases, computed before any write)
    uv run python scripts/backfill_f053_episode_edges.py \
        --agent-id nous-default --dry-run --densify

    # full remediation (recommended): drain, then anchor
    uv run python scripts/backfill_f053_episode_edges.py \
        --agent-id nous-default --densify --max-batches 30

    # anchors only (accepts forgoing cosine healing)
    uv run python scripts/backfill_f053_episode_edges.py \
        --agent-id nous-default
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from datetime import UTC, datetime

from sqlalchemy import text

from nous.brain.embeddings import EmbeddingProvider
from nous.brain.graph_densifier import GraphDensifier
from nous.brain.graph_linker import GraphLinker
from nous.config import Settings
from nous.storage.database import Database

logger = logging.getLogger("f053-restore")


async def _count_orphan_episodes(db: Database, agent_id: str) -> int:
    """Count orphan-eligible episodes (mirrors find_orphans('episode') with
    the Task-3 liveness extra_where; require_embedding matches the
    backfill's default True)."""
    from nous.brain.graph_constants import (
        autobehavior_exclusion_sql,
        episode_live_sql,
    )

    excl = autobehavior_exclusion_sql("e.")
    live = episode_live_sql("t.")
    async with db.engine.begin() as conn:
        r = await conn.execute(
            text(
                f"SELECT COUNT(*) FROM heart.episodes t "
                f"WHERE t.agent_id = :a AND {live} "
                f"  AND t.embedding IS NOT NULL "
                f"  AND NOT EXISTS ("
                f"    SELECT 1 FROM brain.graph_edges e "
                f"    WHERE e.agent_id = :a AND {excl} AND ("
                f"      (e.source_id = t.id AND e.source_type = 'episode')"
                f"      OR (e.target_id = t.id AND e.target_type = 'episode')"
                f"    )"
                f"  )"
            ),
            {"a": agent_id},
        )
        return int(r.scalar() or 0)


async def run(
    *, agent_id: str, dry_run: bool, densify: bool,
    max_batches: int | None,
) -> int:
    settings = Settings()

    db = Database(settings)
    await db.connect()
    try:
        embedder = EmbeddingProvider(settings)
        linker = GraphLinker(
            db=db, embedder=embedder, settings=settings, agent_id=agent_id,
        )
        densifier = GraphDensifier(
            db=db, graph_linker=linker, embedder=embedder,
            settings=settings, agent_id=agent_id,
        )

        run_started = datetime.now(UTC).isoformat()
        print(f"Run start (rollback key: created_at >= this): {run_started}")

        # Phase 1 — cosine drain (MUST precede the anchors: they de-orphan).
        if densify:
            # Gate applies to this phase only — the deterministic anchor
            # restore (Phase 2) does not depend on the backfill flag.
            if not settings.graph_backfill_enabled:
                print(
                    "WARN: NOUS_GRAPH_BACKFILL_ENABLED is False — "
                    "backfill_orphan_episodes short-circuits to 0. "
                    "Set the env var, or drop --densify.",
                    file=sys.stderr,
                )
                return 1
            if dry_run:
                n = await _count_orphan_episodes(db, agent_id)
                print(f"Phase 1 (dry-run): {n} orphan-eligible episodes")
            else:
                batch_n, total = 0, 0
                start = time.time()
                while True:
                    if max_batches is not None and batch_n >= max_batches:
                        print(f"--max-batches={max_batches} hit; stopping.")
                        break
                    before = await _count_orphan_episodes(db, agent_id)
                    if before == 0:
                        break
                    batch_n += 1
                    created = await densifier.backfill_orphan_episodes()
                    total += created
                    after = await _count_orphan_episodes(db, agent_id)
                    print(
                        f"  batch {batch_n}: {created} edges, "
                        f"orphans {before} -> {after}"
                    )
                    if after >= before:
                        # find_orphans is newest-first with a fixed LIMIT: a
                        # head of below-threshold orphans blocks the queue, so
                        # re-running will NOT drain the remainder. Honest stop.
                        print(
                            f"No orphan progress — {after} episodes have no "
                            f"above-threshold candidates (stuck head; re-runs "
                            f"won't help; they'll be anchored by Phase 2).",
                        )
                        break
                print(
                    f"Phase 1: {total} edges in {batch_n} batches "
                    f"({time.time() - start:.0f}s)"
                )

        # Phase 2 — deterministic anchors
        counts = await densifier.restore_episode_anchor_edges(dry_run=dry_run)
        label = "would restore" if dry_run else "restored"
        print(
            f"Phase 2 ({label}): part_of={counts['part_of']} "
            f"extracted_from={counts['extracted_from']} "
            f"discussed_in={counts['discussed_in']}"
        )
        return 0
    finally:
        await db.disconnect()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent-id", required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--densify", action="store_true",
        help="Also drain orphan-eligible episodes through F040 backfill "
             "(embedding + optional CE cost).",
    )
    parser.add_argument(
        "--max-batches", type=int, default=None,
        help="Cap on --densify batches (default: run until drained).",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    return asyncio.run(run(
        agent_id=args.agent_id, dry_run=args.dry_run,
        densify=args.densify, max_batches=args.max_batches,
    ))


if __name__ == "__main__":
    sys.exit(main())
```

NOTE for implementer: teardown is `await db.disconnect()` — verified: `Database` has `connect()/disconnect()/session()` and NO `close()` (`nous/storage/database.py:27-47`; the F070 script uses `disconnect()` at `scripts/backfill_f070_chunks.py:359`). `backfill_orphan_episodes` takes `max_count`/`ce_stats` kwargs; calling it bare uses `graph_backfill_max_episodes` — intended.

- [ ] **Step 2: Smoke-check the script parses and dry-runs against local dev DB**

Run: `docker compose up -d postgres` then
`uv run python scripts/backfill_f053_episode_edges.py --agent-id smoke-nonexistent --dry-run --densify`
Expected: `Phase 1 (dry-run): 0 orphan-eligible episodes` then `Phase 2 (would restore): part_of=0 extracted_from=0 discussed_in=0`, exit 0. (If `NOUS_GRAPH_BACKFILL_ENABLED=false` in the local env, expect the WARN + exit 1 — that path is also correct behavior; re-run without `--densify` to smoke Phase 2 alone.)

- [ ] **Step 3: Commit**

```bash
git add scripts/backfill_f053_episode_edges.py
git commit -m "feat(scripts): F053 remediation script — restore + optional episode-orphan drain"
```

---

### Task 7: Documentation corrections discovered during the audit

**Files:**
- Modify: `nous/config.py:1762-1771` (`chunk_consolidation_enabled` description)
- Modify: `CLAUDE.md` (env-var table rows for `NOUS_GRAPH_THRESHOLD_CHUNK_CHUNK_CROSS`)

**Interfaces:** none (docs only).

- [ ] **Step 1: Fix the `chunk_consolidation_enabled` description**

The current text claims "sleep cycle and EpisodeSummarizer build graph edges" — the flag's only consumers are `GraphDensifier.backfill_orphan_chunks` / `backfill_orphan_chunks_cross_episode`; the summarizer writes chunk *rows* (F067), never edges. Replace the description:

```python
    chunk_consolidation_enabled: bool = Field(
        default=False,
        description=(
            "F070 (2026-05-25). When true, the sleep-cycle graph backfill "
            "(GraphDensifier.backfill_orphan_chunks + the F070.1 "
            "cross-episode pass) builds graph edges to/from "
            "heart.episode_chunks rows. Fixes the gap that chunks have "
            "zero edges (audit 2026-05-25 found 1,775 edges, all "
            "fact↔fact / procedure↔procedure). Required for adjacency "
            "boost and F022 spreading activation to reach chunks. "
            "(The EpisodeSummarizer writes chunk ROWS via F067; it never "
            "writes chunk edges.)"
        ),
    )
```

- [ ] **Step 2: Fix the stale CLAUDE.md F070.1 note**

In the CLAUDE.md env-var table, the `NOUS_GRAPH_THRESHOLD_CHUNK_CHUNK_CROSS` row says "Reserved; v1 does not yet write these (deferred to F070.1)." — F070.1 shipped (`backfill_orphan_chunks_cross_episode`, prod has 156 cross-episode edges). Replace that row's description with:

```
F070.1 cosine threshold for cross-episode chunk↔chunk edges (written by the sleep-cycle cross-episode pass; see also NOUS_GRAPH_THRESHOLD_CHUNK_FACT_CROSS).
```

- [ ] **Step 3: Commit**

```bash
git add nous/config.py CLAUDE.md
git commit -m "docs: correct F070 flag description + stale F070.1 CLAUDE.md note"
```

---

### Task 8: Full-suite verification + PR

- [ ] **Step 1: Run the full test suite**

Run: `uv run pytest tests/ -x -q`
Expected: all pass (1750+ tests).

Run integration set: `$env:NOUS_TEST_DB='postgres'; uv run pytest tests/test_f053_dead_edge_prune.py tests/test_graph_densifier.py -m integration --integration -v`
Expected: all pass.

- [ ] **Step 2: Push branch, open PR**

PR title: `fix(sleep): F053 prune erased the episode graph layer — liveness predicate + restore`
PR body must include: the prod evidence table (657 closed episodes / 6 edges, part_of 3/3,060, both-direction census), the semantic-overload root cause with the HT-1 precedent, the four code changes, the operator rollout steps from this plan's header, and the rollback key description.

- [ ] **Step 3: Codex review loop**

Address every codex round; re-request until clean. Then merge (squash) per repo convention — do NOT use `--delete-branch` if any stacked PR exists (feedback memory: stacked-PR footgun).

## Explicit non-goals

- No de-overload of the `active` column (adding an explicit `deleted_at`/`deleted` marker touches dozens of sites — separate spec if wanted).
- No restore of cosine-inferred episode edges by script SQL — those go through the normal F040 quality gates (thresholds, CE rerank) via the `--densify` drain (which runs BEFORE the anchors — see Task 6 ORDERING) or the nightly cycle for episodes that remain orphans.
- No fix for the `--densify` stuck-head limitation (review F4): `find_orphans` is newest-first with a fixed LIMIT, so orphans whose candidates all fall below the 0.75 episode↔episode threshold block the queue head and the drain stops with a drainable older tail untouched. The script reports this honestly; the nightly cycle has the identical behavior for facts (accepted precedent). Oldest-first/offset paging is a separate improvement if the residual matters.
- No change to `episode_summarizer._link_similar_episodes` (it has no active filter — pre-existing behavior, now beneficial: its edges survive once the prune stops deleting them). Its lack of a trivial/abandoned exclusion is pre-existing and out of scope.
- No change to F053's fact/procedure branches, the supersedes preservation, or the per-cycle bound.
- No dedicated test for the `_backfill_cross_type` Source-2 episode carve-out (review F9: no live caller passes `target_type="episode"`; the change is consistency-hardening for future callers).
- No dashboard changes (`_active_clause` orphan metrics treat closed episodes as excluded — cosmetic, defer).
