# F043 CE Rerank for Sleep-Cycle Backfill — Implementation Plan

**Date:** 2026-04-14
**Author:** lead (Nous forge)
**Spec:** `docs/features/F043-ce-rerank-sleep-backfill.md`
**Status:** v2 — revised after 3-agent review (arch `2ae17477`, impl `1c8eed8a`, devil `65b4a55c`). APPROVED WITH REVISIONS, ready to implement.

## Review findings incorporated

**P1 — all three reviewers converged on the same issue:**

- `nous/handlers/sleep_handler.py:893` does `total_edges = sum(result.values())` and `graph_densifier.py:453` does `total = sum(results.values())`. Adding `ce_kept`/`ce_pruned` as top-level keys would silently inflate edge totals, break `sleep_stats["orphan_edges_created"]`, and flunk the existing `tests/test_sleep_densification.py::test_graph_densification_phase_runs` assertion (`orphan_edges_created == 5`).
- **Fix:** nest CE stats under a distinct, non-numeric-summing key `_ce_stats`. The sleep handler pops it before summing; `run_backfill_cycle` also pops before its own sum. Existing tests that use `in`/key-containment keep passing; existing exact-sum assertions stay correct.

**P2 addressed:**
- **`max_candidates=top_k`** (not `top_k * 2`). Reviewer impl-043 confirmed the doubling pays for CE inference that gets sliced off anyway, because `hybrid_search` already caps at 10.
- **Rename `ce_kept` → `ce_survived`.** Devil-043's point: "kept" implies edges finalized, but cosine gate can still reject post-CE. Use `ce_survived` (passed CE rerank) and track `ce_pruned` separately. Do NOT add a third counter for post-cosine edges — the existing per-type counts already capture that.
- **`fetch_candidate_content` adds `AND t.agent_id = :agent_id`** — defense-in-depth. All three reviewers flagged.
- **Adapter takes `entity_type` instead of raw `table`/`content_col`**, does the `_ENTITY_CONFIG` lookup internally (arch-043 P2-2). Cleaner signature, less injection surface.
- **Whitespace-only content is dropped**: `if row.content and row.content.strip()` in `fetch_candidate_content` (impl-043 P2-2).
- **Risk table row corrected:** the "`weight = min(rrf_score, 1.0)` else branch never hit under F043" claim is wrong — `find_orphans` does NOT guarantee `embedding IS NOT NULL` in every entity_type (devil-043 P2-2, verified: the `WHERE t.embedding IS NOT NULL` clause is conditional). CE sigmoid outputs (0,1) make it numerically safe regardless, so the overwrite is still OK — but document the real reason, not the wrong one.
- **Unit tests restricted to `heart.facts` content_col** (plain `t.content`). Episode uses a JSON path (`structured_summary->>'summary'`) that sqlite doesn't parse the same way as Postgres. For non-fact entity types, integration tests either use the real Postgres fixture or mock `fetch_candidate_content` directly (impl-043 P2-4).
- **Cross-type single-candidate bypass** documented in the graceful-degradation matrix. F042 reranker short-circuits on `len(candidates) <= 1`, so a lone cross-type survivor skips `ce_backfill_min_score` — cosine gate remains the correctness floor (devil-043 P2-3).
- **Test suite gains:** duplicate candidate IDs, `len < top_k` under-fill, None/whitespace content, single-candidate bypass note, agent_id filter verification.

**P3 addressed:**
- **Cross-type candidate iteration order deterministic**: use `sorted(candidate_ids)` so tied CE scores break ties on UUID, not set-iteration order.
- **`sleep_stats.get("ce_backfill_*", 0)` defensive reads** in the log-line formatting so future refactors that drop the counters don't crash.
- **Empty-content drop** in `ce_rerank_backfill_candidates` is a no-op for cross-type (content_map already filtered) — comment the intent so it's not "removed as dead code" later.
- **Module docstring** on `backfill_rerank.py` notes that F042 config (`cross_encoder_model`, `cross_encoder_text_limit`) is intentionally shared.
- **`_ce_stats` as method-local, returned in result dict** rather than instance state — eliminates reset and future re-entrancy concerns (impl-043 P3-2, devil-043 P2-4). Now a `run_backfill_cycle`-local dict passed down via a new internal `_ctx` parameter (or via the session-scoped stat-accumulator pattern).

## Resolved open questions

1. **Per-cycle `_ce_stats`?** Yes — method-local, not instance state. All three reviewers agreed.
2. **Share `ce_backfill_top_k` with F042 `cross_encoder_max_candidates`?** No — different budgets. F042's 30 is user-visible recall; backfill's 10 right-sizes `hybrid_search`'s 10-candidate output.
3. **`agent_id` filter on `fetch_candidate_content`?** Yes — defense-in-depth, zero cost.
4. **Adapter for `discover_clusters`?** MVP cut correct — that path uses hub-to-hub hand-rolled cosine, not `hybrid_search`. Defer.

---

## Scope

Insert F042's cross-encoder reranker into F040's graph-densification backfill paths (same-type and cross-type) during the sleep cycle. Reuses `nous/heart/reranker.cross_encoder_rerank()` — no new reranker code. Adds a thin adapter (`nous/brain/backfill_rerank.py`) to bridge the shape mismatch between `hybrid_search()`'s `list[tuple[UUID, float]]` and the reranker's mutable-`.score` contract. Feature-flagged off by default. Ship MVP only — contradiction-path filtering is explicitly deferred to Phase 2 in the spec.

## Key decisions

1. **Adapter module rather than polymorphic reranker.** F042's reranker accepts arbitrary objects via `text_fn`. We wrap `(UUID, rrf_score)` tuples in a `RerankCandidate` dataclass with a mutable `.score`, batch-fetch content in a single `IN (...)` query, and drop empty-content rows up front. Keeps graph_densifier.py clean.

2. **CE before cosine verification, not after.** Cosine is the correctness floor; CE is a precision pre-filter. Saves DB round-trips too — we only cosine-verify CE survivors. If CE ever misbehaves, cosine catches it.

3. **Replace RRF score with sigmoid CE score post-rerank.** The downstream cosine loop doesn't actually read the RRF score (it computes its own `weight = float(sim_row.similarity)`), so score overwrite is safe — verified by reading `_backfill_same_type` end-to-end.

4. **Same F042 `cross_encoder_model` + `text_limit` settings.** Three new F043 settings only: enable flag, top_k, min_score. No per-stage model choice in MVP.

5. **Telemetry via `GraphDensifier._ce_stats` dict.** Per-cycle `ce_kept` / `ce_pruned` counters accumulate, then `run_backfill_cycle()` returns them in the stats dict alongside per-type counts. Sleep handler logs them.

## Files

### New

1. **`nous/brain/backfill_rerank.py`** (~130 LOC)
   - `@dataclass RerankCandidate` (id/content/score) — mutable (no `slots=True`, tradeoff documented in module docstring).
   - `async fetch_candidate_content(session, agent_id, entity_type, candidate_ids) -> dict[UUID, str]` — takes `entity_type` and looks up `_ENTITY_CONFIG` internally for `(table, content_col)`. Filters `AND t.agent_id = :agent_id` for defense-in-depth. One SQL `IN` query. Drops rows whose content is None/empty/whitespace-only.
   - `async ce_rerank_backfill_candidates(query_text, candidate_rows, content_map, *, settings, log_context) -> list[tuple[UUID, float]]` — the main entry point. Short-circuits cleanly on: `settings.ce_backfill_enabled=False`, `CROSS_ENCODER_AVAILABLE=False`, empty `candidate_rows`, empty `query_text`. Logs kept/pruned counts at DEBUG.
   - Passes `max_candidates=settings.ce_backfill_top_k` (NOT `top_k * 2`) to the F042 reranker.
   - **Important:** does NOT import `sentence_transformers` directly. Only imports `CROSS_ENCODER_AVAILABLE` and `cross_encoder_rerank` from `nous.heart.reranker`, which already has the ImportError guard.
   - Module docstring notes: F042 config (`cross_encoder_model`, `cross_encoder_text_limit`) is intentionally shared.
   - Import `_ENTITY_CONFIG` from `nous.brain.graph_densifier` — this creates a `backfill_rerank → graph_densifier` import edge. To avoid circularity (graph_densifier imports from backfill_rerank), move `_ENTITY_CONFIG` to a small leaf module `nous/brain/_entity_config.py` that both import from. Alternatively, the adapter can accept an injected dict parameter. **Decision: introduce `nous/brain/_entity_config.py`** — one-time refactor, fully backward-compat for graph_densifier which re-exports via `from nous.brain._entity_config import _ENTITY_CONFIG`.

2. **`tests/test_backfill_rerank.py`** (~250 LOC, 14 unit tests)
   - `test_ce_rerank_disabled_passthrough` — `ce_backfill_enabled=False` → input returned unchanged
   - `test_ce_rerank_unavailable_passthrough` — monkeypatch `CROSS_ENCODER_AVAILABLE=False`
   - `test_ce_rerank_empty_candidates` — `[]` in, `[]` out
   - `test_ce_rerank_empty_query` — `query_text=""` → input returned unchanged
   - `test_ce_rerank_drops_empty_content` — candidates with no matching `content_map` entry are dropped
   - `test_ce_rerank_respects_top_k` — 20 candidates, `top_k=5` → at most 5 returned
   - `test_ce_rerank_applies_min_score_floor` — fake model scores straddle floor; only those above survive
   - `test_ce_rerank_preserves_rrf_when_short_circuited` — CE unavailable → original tuple list verbatim
   - `test_ce_rerank_replaces_score_with_sigmoid` — CE runs → returned tuples carry sigmoid scores
   - `test_ce_rerank_single_candidate_bypass` — one candidate, CE short-circuits; verify min_score floor is NOT applied (documents the F042 behavior so it's not a surprise). Cosine gate downstream is the correctness floor for this case.
   - `test_ce_rerank_under_filled_top_k` — 3 candidates, `top_k=10` → returns 3 (not zero, not error)
   - `test_ce_rerank_duplicate_candidate_ids_in_input` — same UUID appears twice in `candidate_rows`; verify both wrapper instances are processed (dedup is NOT the adapter's job; caller guarantees uniqueness today via `set`/dict)
   - `test_fetch_candidate_content_agent_id_filter` — insert a row for agent A and agent B with identical id (different row UUIDs); verify fetch returns only the caller's agent_id
   - `test_fetch_candidate_content_drops_whitespace` — row with `content="   "` or `content=None` omitted from result
   - **Sqlite limitation:** unit tests for `fetch_candidate_content` only exercise `entity_type="fact"` (plain `t.content`). Other entity types are integration-tested against Postgres OR mocked at the adapter boundary.

### Modified

3. **`nous/brain/graph_densifier.py`** (~55 LOC added, no deletions)
   - Import `ce_rerank_backfill_candidates`, `fetch_candidate_content` at top.
   - **No `self._ce_stats` instance state.** Instead, `run_backfill_cycle` creates a local dict `ce_stats = {"survived": 0, "pruned": 0}` and threads it through to the two backfill helpers via a new `ce_stats: dict | None = None` kwarg.
   - `_backfill_same_type(..., ce_stats=None)` — after `hybrid_search` returns candidates, insert the CE rerank block. If `ce_stats is not None`, accumulate counts there.
   - `_backfill_cross_type(..., ce_stats=None)` — after `candidate_content = {...}`, iterate over `sorted(candidate_content.keys())` (deterministic), CE-rerank, filter map, optionally accumulate `ce_stats`.
   - **`run_backfill_cycle` return shape:** `{"facts": int, "decisions": int, "episodes": int, "procedures": int, "_ce_stats": {"survived": int, "pruned": int}}`. The `_` prefix signals "not a per-type edge count — do not sum me." Inside the function, the existing `total = sum(results.values())` becomes:
     ```python
     edges = {k: v for k, v in results.items() if not k.startswith("_")}
     total = sum(edges.values())
     ```
   - `_ENTITY_CONFIG` extraction to `nous/brain/_entity_config.py`: move the dict, re-import from here for back-compat.

4. **`nous/config.py`** (~4 LOC)
   Add in the F040 graph-backfill section:
   ```python
   # F043: CE reranking for sleep-cycle backfill
   ce_backfill_enabled: bool = False
   ce_backfill_top_k: int = 10
   ce_backfill_min_score: float = 0.30
   ```

5. **`nous/handlers/sleep_handler.py`** (~10 LOC)
   - `_phase_graph_densification`: pop `_ce_stats` from the result dict BEFORE `sum(result.values())`:
     ```python
     ce_stats = result.pop("_ce_stats", {"survived": 0, "pruned": 0})
     total_edges = sum(result.values())  # unchanged semantics
     sleep_stats["ce_backfill_survived"] = ce_stats["survived"]
     sleep_stats["ce_backfill_pruned"] = ce_stats["pruned"]
     ```
   - Update the existing log line to (defensive `.get` for future-proofing):
     ```
     F040 graph densification: %d backfill edges (CE survived=%d pruned=%d), %d bridge edges
     ```
     via `sleep_stats.get("ce_backfill_survived", 0)` / `sleep_stats.get("ce_backfill_pruned", 0)`.

6. **`tests/test_graph_densifier.py`** (~70 LOC added)
   - `test_backfill_same_type_with_ce_rerank` — seed 5 facts, enable `ce_backfill_enabled`, monkeypatch `nous.heart.reranker._load_cross_encoder` with a fake model whose `predict` scores by substring; verify only CE-top candidates become edges, and `_ce_stats["survived"]/["pruned"]` accumulate.
   - `test_backfill_ce_disabled_matches_baseline` — `ce_backfill_enabled=False` → behavior identical to pre-F043 baseline (regression guard). Assert `_ce_stats == {"survived": 0, "pruned": 0}`.
   - `test_run_backfill_cycle_returns_ce_stats` — returned dict includes `_ce_stats` nested key and does NOT leak CE counters into `sum(result.values())` or per-type counts.
   - `test_run_backfill_cycle_sum_values_unchanged` — **regression guard for the P1 bug**: compute `sum(v for k, v in result.items() if not k.startswith("_"))` and verify it equals the sum of per-type edge counts, independent of CE stats.

7. **`tests/test_sleep_densification.py`** (~25 LOC added)
   - Extend the existing `test_graph_densification_phase_runs` to have the mocked `run_backfill_cycle` return `{"facts": 3, "decisions": 2, "episodes": 0, "procedures": 0, "_ce_stats": {"survived": 5, "pruned": 3}}`.
   - Assert `sleep_stats["orphan_edges_created"] == 5` (UNCHANGED — this is the regression guard for the P1 bug).
   - Assert `sleep_stats["ce_backfill_survived"] == 5` and `sleep_stats["ce_backfill_pruned"] == 3`.
   - Use `caplog` (pytest fixture) to verify the log line contains `CE survived=5 pruned=3`.

8. **`CLAUDE.md`** — 3 env var rows (`NOUS_CE_BACKFILL_ENABLED`, `_TOP_K`, `_MIN_SCORE`), plus a new row in the What's Shipped table: `| F043 | CE reranking applied to F040 graph backfill during sleep (precision pre-filter before cosine gate, feature-flagged, shared reranker with F042) | — |`

9. **`docs/features/F043-ce-rerank-sleep-backfill.md`** — `Status: Draft` → `Status: Shipped`, PR link filled post-merge

10. **`docs/features/INDEX.md`** — add F043 row following the F040/F042 format

### Not modified

- `nous/heart/reranker.py` — reused as-is
- `pyproject.toml`, `Dockerfile`, `docker-compose.yml` — no new deps, F042 already installed `[rerank]`
- `nous/runtime_config.py` — F043 does not need a runtime toggle path in MVP (sleep phase re-reads settings each cycle; flipping env var + restart is fine)

## Pipeline positions (concrete)

### `_backfill_same_type` (nous/brain/graph_densifier.py:143)

```python
candidates = await hybrid_search(...)          # existing line 174-185
if not candidates:
    return 0

# F043 insertion: rerank before cosine verify
if self._settings.ce_backfill_enabled:
    content_map = await fetch_candidate_content(
        session, table, content_col,
        [c[0] for c in candidates],
    )
    before = len(candidates)
    candidates = await ce_rerank_backfill_candidates(
        query_text=orphan_content,
        candidate_rows=candidates,
        content_map=content_map,
        settings=self._settings,
        log_context=f"{entity_type}-same:{orphan_id}",
    )
    after = len(candidates)
    self._ce_stats["ce_kept"] += after
    self._ce_stats["ce_pruned"] += max(before - after, 0)
    if not candidates:
        return 0

# existing cosine verification loop below — unchanged
for cand_id, rrf_score in candidates:
    ...
```

### `_backfill_cross_type` (nous/brain/graph_densifier.py:232)

```python
candidate_content: dict[UUID, str] = {       # existing line 309-311
    row.id: row.content for row in result if row.content
}

# F043 insertion: rerank cross-type survivors before re-embed loop
if self._settings.ce_backfill_enabled and candidate_content:
    synthetic_rows = [(cid, 0.0) for cid in candidate_content.keys()]
    before = len(synthetic_rows)
    ranked = await ce_rerank_backfill_candidates(
        query_text=orphan_content,
        candidate_rows=synthetic_rows,
        content_map=candidate_content,
        settings=self._settings,
        log_context=f"{source_type}->{target_type}:{orphan_id}",
    )
    surviving = {cid for cid, _ in ranked}
    candidate_content = {
        cid: txt for cid, txt in candidate_content.items() if cid in surviving
    }
    after = len(candidate_content)
    self._ce_stats["ce_kept"] += after
    self._ce_stats["ce_pruned"] += max(before - after, 0)

# existing re-embed + cosine loop below — unchanged
```

## Implementation phases

**Phase A — python-eng-043 (sequential, foundation):**
1. Create `nous/brain/backfill_rerank.py` with dataclass + two functions
2. Add 3 settings fields to `nous/config.py`
3. Wire CE rerank into `_backfill_same_type` and `_backfill_cross_type`
4. Add `_ce_stats` init + reset + return in `run_backfill_cycle`
5. Propagate stats through `_phase_graph_densification`
6. Import smoke: `python -c "from nous.brain.graph_densifier import GraphDensifier; from nous.brain.backfill_rerank import ce_rerank_backfill_candidates"` → no ImportError

**Phase B — test-eng-043 (after Phase A):**
7. `tests/test_backfill_rerank.py` with 10 unit tests, deterministic fake model via monkeypatch
8. Extend `tests/test_graph_densifier.py` with 3 integration tests
9. Extend `tests/test_sleep_densification.py` with the stat-propagation assertion
10. Run: `pytest tests/test_backfill_rerank.py tests/test_graph_densifier.py tests/test_sleep_densification.py -v`

**Phase C — docs-eng-043 (after Phase A, parallelizable with B):**
11. Update `CLAUDE.md` env var table + shipped row
12. Update `docs/features/F043-*.md` Status (will flip to Shipped post-merge, plan leaves as Draft)
13. Update `docs/features/INDEX.md`

**Phase D — lead:**
14. Run code-reviewer subagent on the full diff
15. Address any P1/P2 findings inline; P3s optional
16. Commit, push branch, open PR
17. Merge after CI green
18. `review_outcome` on every subagent + lead decision

Each subagent follows forge protocol with unique `agent_id`, creates a decision FIRST via `pre_action`, streams ≥10 thoughts, finalizes via `update_decision`, and reports back.

## Test strategy

**Unit (test_backfill_rerank.py):** 10 tests, no DB, fake model via monkeypatch on `nous.heart.reranker._load_cross_encoder` + `CROSS_ENCODER_AVAILABLE=True`. Verifies adapter semantics in isolation — contract, score overwrite, top-K, min-score floor, graceful degradation.

**Integration (test_graph_densifier.py):** 3 tests, real in-memory sqlite + real `GraphDensifier`, fake reranker. Verifies the insertion point actually runs, only survivors hit the cosine loop, and `_ce_stats` accumulates.

**Sleep handler (test_sleep_densification.py):** 1 extended test, mocked densifier, verifies stat propagation into `sleep_stats` and the log line.

Monkeypatch strategy (same as F042 tests): set `CROSS_ENCODER_AVAILABLE=True` on the imported module, then replace `_load_cross_encoder` with a fake that returns a stub whose `predict(pairs)` returns a deterministic list. This avoids any real torch import.

## Graceful degradation matrix

| Condition | Behavior |
|---|---|
| `sentence-transformers` not installed | adapter returns candidates unchanged, no-op |
| `ce_backfill_enabled=False` (default) | adapter short-circuits before any DB work |
| `CROSS_ENCODER_AVAILABLE=False` | same as above — reranker returns input |
| `candidate_rows` empty | returns `[]` |
| `content_map` empty / all candidates have NULL content | returns `[]`, logs DEBUG |
| `query_text` empty | returns candidates unchanged |
| Reranker raises | caught inside F042 reranker, returns candidates unchanged |
| `top_k < 1` | safeguard in adapter: `top_k = max(top_k, 1)` |
| `min_score` outside [0,1] | no clamp needed; sigmoid output is already in (0,1) |
| Single cross-type candidate | F042 reranker short-circuits on `len <= 1` → bypasses `min_score` floor. Cosine gate is the backstop. |

## Risk / Mitigation

| Risk | Mitigation |
|---|---|
| CE prunes true positives | Cosine gate unchanged — precision floor preserved. Flag defaults off. |
| DB round-trip cost for content fetch | Single `IN` query per orphan, N≤10; batched, no N+1 |
| Test fixture shape mismatch with existing `test_graph_densifier` conftest | Mirror existing `_insert_fact` helper; test-eng must read conftest first |
| `_ENTITY_CONFIG[entity_type]` content_col is an f-string-injected identifier | Existing codebase relies on this; we're not making it worse — but note in code review |
| Sleep phase test count regression | No new phase; inside existing `graph_densification`. Existing phase-count tests untouched. |
| Score overwrite surprises downstream reader | `_backfill_same_type` reads `rrf_score` in the `else` branch when `orphan_embedding` is None. That branch IS reachable (find_orphans does not hard-require non-null embedding for every type). However: CE sigmoid output is in (0, 1), which is within the valid `weight` range for `create_edge()` — so the overwrite is numerically safe even when the branch fires. Verified by devil-043. |
| Cross-type has no RRF score to overwrite | We pass synthetic `0.0` RRF scores; CE overwrites with sigmoid; downstream only uses the CE-survival set, not the scores. Safe. |
| **P1 regression: sum-of-values double counts CE stats** | Fixed by nesting CE stats under `_ce_stats` key and filtering `not k.startswith("_")` in both sum sites. New regression-guard test `test_run_backfill_cycle_sum_values_unchanged`. |
| Single cross-type candidate bypasses `ce_backfill_min_score` | F042 reranker short-circuits on `len <= 1`. Cosine gate downstream remains the correctness floor. Documented in graceful-degradation matrix; `test_ce_rerank_single_candidate_bypass` pins the behavior. |

## Open questions (for review team)

1. **Does `_ce_stats` need to be per-run_backfill_cycle or cumulative?** Plan says per-cycle (reset at start). Review confirm.
2. **Should `ce_backfill_top_k` and `cross_encoder_max_candidates` (F042) share a setting?** Plan says separate — sleep budget differs from recall budget. Review confirm.
3. **Does `fetch_candidate_content` need an agent_id filter?** The candidate IDs came from `hybrid_search()` which already filtered by agent_id, so IDs are agent-scoped. But defense-in-depth would add `AND t.agent_id = :agent_id`. Review decide.
4. **Should we also add an adapter for `discover_clusters()`?** That's F040's bridge-edge discovery. Out of MVP scope but worth flagging. Review confirm MVP cut.

## Resolved (from exploration)

- `hybrid_search` returns `list[tuple[UUID, float]]` — confirmed, adapter needed.
- Cosine gate at line 201-212 is the correctness floor — CE is additive.
- `_ENTITY_CONFIG[entity_type]` exposes `content_col` — we have a reliable text source.
- No new sleep phase; F043 is inside existing `graph_densification` phase 8.
- F042 reranker signature confirmed: `async cross_encoder_rerank(query, candidates, text_fn, *, model_name, max_candidates, text_limit) -> list` — matches our adapter expectation.

## Out of scope (explicit)

- Contradiction-detection candidate filtering — spec Phase 2
- Fact-at-ingest dedup via CE — separate effort
- Fine-tuning on link outcomes — F042 Phase 2 territory
- Dashboard UI for CE stats — log-based observability first
- Cluster-discovery (`discover_clusters`) reranking — defer
- Runtime config override (`RuntimeConfig.get_ce_backfill_enabled`) — sleep phase restart is acceptable for MVP

## LOC estimate

| Area | LOC |
|---|---|
| `backfill_rerank.py` | ~120 |
| `graph_densifier.py` | ~40 |
| `config.py` | ~4 |
| `sleep_handler.py` | ~8 |
| `test_backfill_rerank.py` | ~180 |
| `test_graph_densifier.py` | ~60 |
| `test_sleep_densification.py` | ~20 |
| docs | ~15 |
| **Total** | **~447** |
