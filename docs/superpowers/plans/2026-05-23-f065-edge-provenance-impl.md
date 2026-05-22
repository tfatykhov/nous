# F065 Implementation Plan

**Spec:** `docs/features/F065-edge-provenance-godnodes.md` (corrected in this branch)
**Branch:** `feat/F065-edge-provenance-impl`
**Date:** 2026-05-23
**LOE:** ~8h focused work (revised up from spec's 6h due to writer-site count of 8, not 4)

---

## Pre-flight invariants (verified 2026-05-23)

- `Brain.link()` at `nous/brain/brain.py:1045` has zero callers; only `_link` is reached. Every production writer hard-codes `auto_linked=True`. Backfill discriminator is `relation`, NOT `auto_linked` (see spec correction).
- `brain.graph_edges` exists with `weight FLOAT`, `auto_linked BOOLEAN`. Migration 047 is the next free slot (043-046 occupied).
- `NeighborResult` at `nous/brain/schemas.py:152` currently has fields `id`, `node_type`, `description`, `edge_relation`, `edge_weight`, `created_at`.
- `_graph_expanded_to_pipeline` at `nous/api/retrieval_pipeline.py:502` and `_heart_graph_to_pipeline` at `:460` both apply `graph_recall_decay`. Both must apply the F065 penalty.
- `pre_turn` at `nous/cognitive/layer.py` is the session-start hook. F026 / F059 patterns show `asyncio.create_task` + try/except inside the task for fire-and-forget persistence.
- 8 GraphEdge construction sites total (audited; see spec backfill table for line numbers).

If any of these invariants fails when the implementer runs, STOP and re-verify before coding.

---

## Commit sequence

### Commit A — Migration 047 + ORM + classify() helper

**Files:**
- `sql/migrations/047_f065_edge_provenance.sql` (new) — exactly as in spec § Migration.
- `nous/storage/models.py` — `GraphEdge.extraction_method: Mapped[str]` (`String(20)`, `nullable=False`, `server_default="'heuristic'"`); new `GraphHubSnapshot` ORM model under `brain` schema with the table definition from the spec.
- `nous/brain/edge_provenance.py` (new) — single function `classify(relation: str) -> str` returning `'deterministic'` for `supersedes`, `'inferred'` for `contradicts`, `'heuristic'` otherwise. Plus a `VALID_METHODS: Final[frozenset[str]] = frozenset({"deterministic", "heuristic", "inferred"})` for the CHECK constraint mirror.
- `tests/test_f065_storage.py` — three tests: classify() exhaustive (every existing relation maps somewhere); GraphEdge row default = 'heuristic' when no extraction_method passed; GraphHubSnapshot ORM round-trip.

**Acceptance:** all three new tests pass; `uv run pytest tests/test_f065_*.py` green.

### Commit B — Writer plumbing (all 8 sites)

**Files:**
- `nous/brain/brain.py:1074` — `Brain._link` constructor adds `extraction_method=classify(relation)`.
- `nous/brain/brain.py:1280` — direct `GraphEdge(...)` in `_auto_link` adds the same.
- `nous/heart/facts.py:166-186` — fact↔fact edge insert; classify there.
- `nous/brain/graph_linker.py:99` — `GraphLinker.create_edge` constructor.
- `nous/brain/graph_linker.py:188, 266, 304, 326` — four direct `pg_insert(GraphEdge)` calls, each gets `extraction_method=classify(relation)`.
- `tests/test_f065_writer_coverage.py` (new) — CI grep-guard. Scan `nous/brain/` and `nous/heart/` for `GraphEdge(` and `pg_insert(GraphEdge)`. Assert count == `EXPECTED_WRITER_COUNT = 8`. Comment in the test explains how to register a new site.
- `tests/test_f065_writer_classification.py` (new) — for each of the 8 sites, write an edge through that code path and verify `extraction_method` matches `classify(relation)` for the relation actually written. For sites that write `supersedes`, expect `'deterministic'`; for `contradicts`, `'inferred'`; for everything else, `'heuristic'`. Use real Postgres backend via the existing test fixtures.

**Acceptance:** writer-coverage test passes; per-site classification tests pass; existing graph tests (`tests/test_graph_*.py`, `tests/test_brain_*.py`) still pass.

### Commit C — NeighborResult + _neighbors SELECT + pipeline penalty wiring

**Files:**
- `nous/brain/schemas.py::NeighborResult` — add `extraction_method: str = "heuristic"` (str, not Optional; default is the fail-open tier; comment cites the F065 P0-3 NULL-handling rationale).
- `nous/brain/brain.py::_neighbors` — `source_q` and `target_q` SELECT `GraphEdge.extraction_method.label("extraction_method")`. `edge_map[r.neighbor_id]` tuple extended to carry `extraction_method`. All `NeighborResult(...)` constructors set it.
- `nous/api/retrieval_pipeline.py::_graph_expanded_to_pipeline` and `:_heart_graph_to_pipeline` — apply penalty:
  ```python
  method = neighbor.extraction_method or "heuristic"
  penalty = settings.graph_inferred_edge_penalty if method == "inferred" else 1.0
  score = base_score * decay * penalty
  ```
  Both call sites use the same helper `_apply_provenance_penalty(neighbor, base_score, decay, settings, source)` defined once at module scope. The helper short-circuits when `source == "spreading_activation"` (defense-in-depth per spec).
- `nous/config.py` — `graph_inferred_edge_penalty: float = 1.0` with env var `NOUS_GRAPH_INFERRED_EDGE_PENALTY`. Dark-launch default.
- `tests/test_f065_pipeline_penalty.py` (new) — test plan items 3, 4, 4b verbatim. Use mocked `NeighborResult` objects with explicit `extraction_method`. No live DB needed.

**Acceptance:** all new tests pass; `tests/test_retrieval_pipeline.py` (existing) still passes byte-identical when penalty=1.0; ruff clean.

### Commit D — top_hubs + recall_hubs tool

**Files:**
- `nous/brain/brain.py` — add `Brain.top_hubs(limit=10, node_type=None)` and a private `_top_hubs_with_breakdown(hub_ids)` for the single-query breakdown. Two SQL passes (top-N aggregation then four-type label resolution lookup matching `_neighbors`'s pattern), plus one breakdown query for `extraction_method_breakdown`.
- `nous/api/tools.py` — `recall_hubs` tool schema + closure. Frame access via `FRAME_TOOLS` mirroring `recall_recent` (all frames).
- `nous/api/runner.py::FRAME_TOOLS` — add `recall_hubs` to all frame tool lists.
- `tests/test_f065_top_hubs.py` (new) — seed 20 nodes with known degree distribution, call top_hubs, assert top-5 are exactly the 5 highest-degree. Test node_type filter, breakdown correctness, label resolution for each of 4 types.
- `tests/test_f065_recall_hubs_tool.py` (new) — invoke tool via dispatcher; assert JSON-shaped response.

**Acceptance:** all new tests pass; existing tool tests unchanged.

### Commit E — pre_turn hub-shift + snapshot writes + retention

**Files:**
- `nous/brain/hub_snapshots.py` (new) — `HubSnapshotManager` with:
  - `get_latest(agent_id, node_ids) -> dict[UUID, GraphHubSnapshot]` (uses `DISTINCT ON` query from spec).
  - `record_snapshot(agent_id, node_id, node_type, degree, rank)` — single-row INSERT.
  - `prune_older_than(days)` — DELETE for sleep handler.
- `nous/cognitive/layer.py::pre_turn` — at the end of working-memory construction (gated on `settings.graph_hub_autosurface_enabled`):
  1. Call `brain.top_hubs(limit=10)`.
  2. Spawn `asyncio.create_task(self._hub_shift_detect(...))` so the rest of pre_turn is not blocked.
  3. The task: fetch most-recent snapshots, compare ranks, emit notices for nodes that crossed the top-10 boundary, insert new snapshots only for boundary-crossers (not for in-place rank shifts), silently baseline new hubs without notice.
- `nous/config.py` — `graph_hub_autosurface_enabled: bool = False`, `graph_hub_snapshot_retention_days: int = 90`.
- `nous/handlers/sleep_handler.py` — add a prune step that calls `HubSnapshotManager.prune_older_than(settings.graph_hub_snapshot_retention_days)`. Gate it behind the same env flag so it stays no-op until autosurface is enabled.
- `tests/test_f065_hub_shift.py` (new) — test plan items 6, 7, 8 verbatim.

**Acceptance:** all new tests pass; full suite green; ruff clean; manual smoke that pre_turn with autosurface disabled is byte-identical to main.

---

## Risk register

| Risk | Mitigation |
|---|---|
| Migration step 5 (`ALTER ... SET NOT NULL`) fails because a row escaped the three backfill UPDATEs | Step 4 is a catch-all (`extraction_method IS NULL → heuristic`), so step 5 cannot fail. Adding `COALESCE(auto_linked, FALSE)` is unnecessary because we don't use `auto_linked` in the backfill anymore (relation-only). |
| A new GraphEdge writer is added in a different PR and bypasses classify() | `test_f065_writer_coverage.py` greps the codebase and fails CI if the count drifts. |
| `NeighborResult.extraction_method` arrives as None despite the str-typed field | Default `"heuristic"` at the schema level prevents None values from existing. Pipeline penalty wrapper still does `neighbor.extraction_method or "heuristic"` as defense in depth and logs WARN once per agent. |
| pre_turn hub-shift detection blocks session start | `asyncio.create_task` makes the whole computation fire-and-forget; pre_turn never awaits the task. |
| F040 nightly densification triggers hub-shift notices for every hub | Rank-based shift (top-10 entry/exit) replaces raw degree %. Densification typically uplifts all hubs proportionally, so ranks rarely change — false-positive storm avoided. |
| `recall_hubs` tool exposed before useful, makes noise in tool catalog | Tool is opt-in (agent must call it explicitly). No autosurface in Phase 1. |

---

## Out-of-PR follow-ups (record but don't ship here)

1. Flip `NOUS_GRAPH_HUB_AUTOSURFACE_ENABLED=true` after one release of bake time.
2. Run F051 harness to measure MRR delta when `NOUS_GRAPH_INFERRED_EDGE_PENALTY=0.7`. Choose operating value based on results.
3. Phase 4 tune is its own follow-up PR.

---

## Review checkpoints

Before each commit lands:
- Commit A: storage + helper — `feature-dev:code-reviewer` on the migration SQL + ORM.
- Commit B: writer plumbing — `pr-review-toolkit:silent-failure-hunter` (this is the place silent classification failures hide).
- Commit C: pipeline penalty — `code-reviewer` on the helper-with-source-guard.
- Commit D: top_hubs — review of the single-pass SQL aggregation.
- Commit E: pre_turn — `silent-failure-hunter` on the fire-and-forget task.

After commit E: one consolidated review against the full diff before opening the PR.
