# F042 Cross-Encoder Reranking — Implementation Plan

**Date:** 2026-04-13
**Author:** lead (Nous forge)
**Spec:** `docs/features/F042-cross-encoder-reranking.md`
**Status:** v2 — revised after 3-agent review (`78ef3a3d` arch, `393d5533` impl, `fcf9dcdb` devil). APPROVED, ready to implement.

## Review findings incorporated

**P1 (must-fix) — all addressed below:**
1. Return-type contract: reranker returns a **list** only; mutates `candidate.score` in place. No tuple.
2. MMR does NOT consume `r.score` as its relevance term (verified at `nous/heart/search.py:319` — cosine-based). Rationale updated: CE acts as a pruning + reorder stage upstream of MMR; MMR is unaffected by the score-mutation itself, but CE's head-of-list reorder changes which candidates survive into MMR.
3. RuntimeConfig bypass: heart.py integration must call `RuntimeConfig.get().get_cross_encoder_enabled(self.settings)` — NOT `self.settings.cross_encoder_enabled` directly. Mirrors the `vector_weight` resolution pattern.
4. **Blocking I/O in event loop:** `CrossEncoder.predict()` is synchronous CPU-bound (~30-200ms). Reranker becomes `async def` and wraps `model.predict` in `asyncio.to_thread(...)`. Non-negotiable.
5. **Pre-existing bug in `runtime_config.persist_to_db`:** `str(True)` → `"True"` is invalid JSON (needs lowercase). Must fix with `json.dumps(value)`. Affects existing float/int path too (it happens to work because numeric strings are valid JSON literals). Scope creep but required for bool persistence.
6. **Import-time crash guard:** `nous/heart/reranker.py` must wrap `from sentence_transformers import CrossEncoder` in module-level `try/except ImportError` so `heart.py`'s `from nous.heart.reranker import CROSS_ENCODER_AVAILABLE` cannot fail even when the dep is absent.
7. **Score scale / sigmoid normalization:** CE logits span ~[-10, +10] and often go negative. They leak to:
   - `nous/api/tools.py:358` — `(score: {r.score:.3f})` formatted into LLM tool output
   - F031 censor `unblock_pattern` regexes (`nous/heart/censor_actions.py:95`) that probe score text
   - future `_apply_relevance_filter` if recall is ever routed through context engine
   **Mitigation:** reranker sigmoid-normalizes `1/(1+exp(-ce))` before writing to `r.score`. Monotonic, bounded to (0,1), preserves CE ordering, backwards-compatible with any score-range assumption.

**P2 (addressed):**
- `cross_encoder_enabled` default flipped to **`False`**. Opt-in via env or runtime override. Safer rollout, no silent passthrough on existing deployments.
- `RuntimeConfig.load_from_db` gets an explicit bool branch.
- `max_candidates` semantics now defined: **head-truncation**. The top `max_candidates` by RRF order are reranked; any tail is appended **after** the reranked head, with `.score` untouched.
- Test suite expanded: scoring ties, max_candidates boundary, mutation-visible-in-returned-list, pre_ce_order branch, the `-inf` empty-summary contract.
- Logging split: one log line for CE reorder, one for MMR reorder.
- Empty-summary candidates now scored `float("-inf")` (not 0.0) so they sink to the tail.

**P3 (addressed):**
- `limit=max(limit*2, limit)` simplified to `limit * 2`.
- `@lru_cache(maxsize=1)`.
- Missing-dep log raised to `WARNING` at startup.
- Redundant import inside heart.py if-block removed.

---

## Scope (MVP only)

Insert a cross-encoder reranking stage into `Heart._recall()` between per-type merge and MMR diversity reranking. Feature-flagged; graceful degradation when `sentence-transformers` is absent. **Skip future phases (fine-tuning, distillation, adaptive) — those need signal pipelines we don't have yet.**

## Key finding (diverges from spec)

The spec proposes a polymorphic `text_extractor(item)` that inspects `content / summary / description` attributes. **This is unnecessary.** By the time candidates reach the insertion point at `heart.py:820`, they are already `RecallResult` objects with a normalized `.summary` field (populated by `_to_recall_result` at lines 872–920: facts use `content`, episodes use `summary`, procedures use `f"{name}: {description}"`, censors use `f"{pattern}: {reason}"`). The reranker only needs `r.summary` — no polymorphism.

## Files

### New

1. **`nous/heart/reranker.py`** (~110 LOC) — reranker module
   - Module-level `try: from sentence_transformers import CrossEncoder; CROSS_ENCODER_AVAILABLE=True except ImportError: CROSS_ENCODER_AVAILABLE=False` — this guarantees the module itself always imports
   - `@lru_cache(maxsize=1) def _load_cross_encoder(model_name: str)` — returns loaded model or raises (callers catch)
   - `async def cross_encoder_rerank(query, candidates, text_fn, *, model_name, max_candidates, text_limit) -> list:`
     - Returns `candidates` unchanged if: `not CROSS_ENCODER_AVAILABLE`, `len(candidates) <= 1`, or `query` empty
     - Splits `head = candidates[:max_candidates]` / `tail = candidates[max_candidates:]`
     - Builds `(query, text_fn(c)[:text_limit])` pairs for head; empty-text candidates get score `float("-inf")`
     - Calls `scores = await asyncio.to_thread(model.predict, pairs)` — unblocks event loop
     - Applies sigmoid: `sigmoid(x) = 1/(1+exp(-x))`
     - Mutates `c.score = sigmoid_score` in place for each head candidate
     - Sorts head DESC by new score
     - Returns `head + tail`
     - On any exception inside: logs WARNING, returns `candidates` unchanged

2. **`tests/test_f042_reranker.py`** (~220 LOC, async)
   - `test_reranker_unavailable_passthrough` — with `CROSS_ENCODER_AVAILABLE=False`, returns input list unchanged (monkeypatch the module flag)
   - `test_reranker_empty_list` — returns `[]`
   - `test_reranker_single_item` — returns the single item unchanged
   - `test_reranker_empty_query` — `query=""` → passthrough
   - `test_reranker_reorders_by_score` — fake model scores by substring match; verify head order is sigmoid-DESC
   - `test_reranker_writes_sigmoid_scores_in_place` — `0 < c.score < 1` for all head items after call, original list object mutated
   - `test_reranker_text_truncation_honored` — pair text length ≤ `text_limit`
   - `test_reranker_empty_summary_sinks_to_tail` — candidate with `.summary=""` scored `-inf`, ends up last in head
   - `test_reranker_max_candidates_head_split` — with 10 items and `max_candidates=3`, only the first 3 are reranked; items 4-10 remain in original order with untouched `.score`
   - `test_reranker_tie_scores_stable` — two items with identical fake scores preserve input order (stable sort)
   - `test_reranker_runs_in_thread` — verify `asyncio.to_thread` used (fake model with sleep; test event loop not blocked via sentinel task)
   - `test_reranker_predict_exception_passthrough` — fake model raises inside `predict`; reranker logs + returns original list unchanged
   - `test_reranker_load_failure_passthrough` — `_load_cross_encoder` raises → passthrough

### Modified

3. **`nous/config.py`** (~6 LOC added)
   Add to `Settings` (default-off — opt-in):
   ```python
   cross_encoder_enabled: bool = False
   cross_encoder_model: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"
   cross_encoder_max_candidates: int = 30
   cross_encoder_text_limit: int = 512
   ```

4. **`nous/runtime_config.py`** (~50 LOC added + 1 pre-existing bug fix)
   - **Pre-existing bug fix:** `persist_to_db` currently uses `{"value": str(value)}` which produces `"True"` (invalid JSON) for bools. Fix with `import json; {"value": json.dumps(value)}`. This also fixes a latent bug for float/int (their str-form happens to be valid JSON literals).
   - Add `_KEY_CROSS_ENCODER_ENABLED = "cross_encoder_enabled"`
   - Add matching methods mirroring `get_vector_weight`/`set_vector_weight`/`clear_vector_weight`/`get_vector_weight_source`: `get_cross_encoder_enabled(settings) -> bool`, `set_cross_encoder_enabled(value: bool)`, `clear_cross_encoder_enabled()`, `get_cross_encoder_enabled_source(settings) -> str`
   - Add branch in `load_from_db`:
     ```python
     if key == _KEY_CROSS_ENCODER_ENABLED and value is not None:
         # value is already python-deserialized by asyncpg from jsonb
         if isinstance(value, bool):
             self._overrides[key] = value
             logger.info("Loaded runtime override: %s = %s", key, value)
     ```
   - Unit test: set/get/clear/persist roundtrip with bool.

5. **`nous/heart/heart.py`** (~30 LOC added)
   Add module-level import near top with other heart imports:
   ```python
   from nous.heart.reranker import CROSS_ENCODER_AVAILABLE, cross_encoder_rerank
   from nous.runtime_config import RuntimeConfig
   ```
   After line 818 (end of merge loop, before MMR block at 820), insert:
   ```python
   # F042: Cross-encoder reranking (between RRF merge and MMR)
   ce_enabled = RuntimeConfig.get().get_cross_encoder_enabled(self.settings)
   if ce_enabled and len(merged) > 1 and CROSS_ENCODER_AVAILABLE:
       try:
           pre_ce_head = [r.id for r in merged[: self.settings.cross_encoder_max_candidates]]
           merged = await cross_encoder_rerank(
               query=query,
               candidates=merged,
               text_fn=lambda r: r.summary or "",
               model_name=self.settings.cross_encoder_model,
               max_candidates=self.settings.cross_encoder_max_candidates,
               text_limit=self.settings.cross_encoder_text_limit,
           )
           post_ce_head = [r.id for r in merged[: self.settings.cross_encoder_max_candidates]]
           ce_reordered = pre_ce_head != post_ce_head
           logger.info(
               "Cross-encoder: reranked %d candidates (head=%d), reordered=%s",
               len(merged), len(pre_ce_head), ce_reordered,
           )
       except Exception as exc:
           logger.warning("Cross-encoder rerank failed, keeping RRF order: %s", exc)
   ```
   The MMR block that follows is unchanged (logging its own `reordered` flag separately).

6. **`pyproject.toml`** (~3 LOC added)
   Add optional dep group:
   ```toml
   rerank = [
       "sentence-transformers>=3.0.0",
   ]
   ```
   **Not** added to core `dependencies`. Operator installs via `pip install ".[rerank]"`. Docker image stays lean by default.

7. **`CLAUDE.md`** — add 4 env var rows to the table (under a new F042 block), add F042 entry to the "What's Shipped" table.

8. **`docs/features/F042-cross-encoder-reranking.md`** — flip `Status: Draft` → `Status: Shipped`, add PR link after merge.

9. **`docs/features/INDEX.md`** — mark F042 shipped (only if it has a row; verify during implementation).

## Pipeline position

```
per-type searches → merge (heart.py:802) → cross-encoder rerank (NEW, 820) → MMR diversity (was 820, now ~850) → return top-k
```

**Score semantics (corrected from v1):** MMR's relevance term is `cosine(query_embedding, candidate_embedding)` (`nous/heart/search.py:319`), not `r.score`. CE therefore does **not** inject into MMR's relevance math. CE's actual effect is two-fold:
1. **Head reorder:** top `max_candidates` by RRF are resorted by CE sigmoid score. MMR then operates on this resorted pool.
2. **Displayed score:** `r.score` is mutated to the sigmoid of the CE logit so downstream consumers (`tools.py:358`, `mcp.py`, `censor_actions.py`) see a bounded [0,1] score consistent with the prior RRF range. Sigmoid keeps ordering monotonic in the CE logit, so regex probes against score text remain well-behaved.

## Graceful degradation matrix

| Condition | Behavior |
|---|---|
| `sentence-transformers` not installed | `CROSS_ENCODER_AVAILABLE=False`, CE block skipped, log once at startup |
| `cross_encoder_enabled=False` | CE block skipped |
| Model load fails (network, disk) | caught by outer `try/except`, logged, RRF order preserved |
| `predict()` raises | same — logged, RRF order preserved |
| `len(merged) <= 1` | CE skipped (nothing to rerank) |
| Empty `.summary` on a candidate | scored 0.0 but kept in list |

## Test strategy

- **Unit (test_f042_reranker.py):** 7 tests covering passthrough, ordering, mutation, truncation, edge cases. No real model — monkeypatch `_load_cross_encoder` to return a fake with `predict(pairs)` returning deterministic scores.
- **Integration (test_heart.py, extend existing):** 2 tests
  - `test_recall_with_cross_encoder_disabled` — default settings (if sentence-transformers missing), recall pipeline still works
  - `test_recall_with_cross_encoder_mocked` — monkeypatch `CROSS_ENCODER_AVAILABLE=True` and `_load_cross_encoder`, verify recall returns CE-reordered results
- **Graceful degradation:** `test_reranker_unavailable_passthrough` covers the ImportError path.

## Order of operations (implementation team pipeline)

Phase A (sequential, foundation) — `python-eng-042`:
  1. Create `nous/heart/reranker.py`
  2. Add settings fields to `nous/config.py`
  3. Add runtime_config methods
  4. Integrate in `heart.py`
  5. Add optional dep to `pyproject.toml`

Phase B (parallel with A's tail) — `test-eng-042`:
  6. Write `tests/test_f042_reranker.py`
  7. Extend `tests/test_heart.py` with 2 integration tests

Phase C (after A+B complete) — `docs-eng-042`:
  8. Update CLAUDE.md env var table + shipped list
  9. Update F042 spec status → Shipped
  10. Update INDEX.md if applicable

Phase D — `lead`:
  11. Run tests, verify green
  12. Run code-reviewer subagent
  13. Address findings
  14. Commit, branch, push, PR
  15. review_outcome for all decisions

Each subagent **must** follow forge protocol (get_session_context → pre_action → record_thought ≥10 → update_decision → review_outcome). Each uses a unique `agent_id`.

## Risk/Mitigation

| Risk | Mitigation |
|---|---|
| sentence-transformers unavailable at runtime (default Docker) | Optional dep + `CROSS_ENCODER_AVAILABLE` flag; default `enabled=True` silently no-ops |
| First-call cold-start (2-3s model load) | `lru_cache` on loader; docs note one-time cost; future eager-load is a phase-2 concern |
| CE score ranges differ from RRF scores (breaks relevance_floor downstream) | CE scores are in [~-10, +10] range from MiniLM. Confirm `_apply_relevance_filter` runs on recall_deep or not — if yes, may need sigmoid normalization. **Open question for devil agent to verify.** |
| Increased recall_deep latency for all queries | Feature-flagged. Integration test asserts latency budget. |
| `merged` mutated in place — future refactor might return fresh list and lose score | Reranker accepts list, mutates `.score`, returns list. Doc-comment the contract. |

## Resolved questions (after review)

1. **Relevance floor interaction:** ✅ `_apply_relevance_filter` in `nous/cognitive/context.py` runs on per-type search output, NOT on `heart.recall()` output. All recall_deep consumers (`tools.py:353`, `mcp.py:263,278`, `censor_actions.py:90`) read `.score` only as display text. **No collision.** But we still sigmoid-normalize (defense-in-depth against future refactors + censor regex compat).
2. **bool-as-JSON in load_from_db:** ❌ NOT safe with current code. Must add explicit bool branch AND fix `persist_to_db` to use `json.dumps()`.
3. **Log volume:** per-item score delta at DEBUG only; INFO emits the aggregate reorder flag once per recall.

## Out-of-scope

- Phase 2 (domain fine-tuning)
- Phase 3 (knowledge distillation)
- Phase 4 (adaptive reranking / score-gap gating)
- ONNX export / CPU-only torch variant
- API-based reranker fallback (Cohere/Jina/Voyage)
- Observability F035 trace_id propagation (log-only for now)

These are valuable but each needs a separate spec and signal pipeline.

## Estimated LOC

| Area | LOC |
|---|---|
| reranker.py | ~90 |
| heart.py integration | ~35 |
| config.py | ~6 |
| runtime_config.py | ~35 |
| tests | ~200 |
| docs (CLAUDE.md, spec status, INDEX) | ~20 |
| **Total** | **~386** |

(Spec estimated ~200 LOC; difference is tests + runtime_config plumbing + docs that spec omitted.)
